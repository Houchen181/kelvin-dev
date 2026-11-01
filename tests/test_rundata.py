"""Each run's data: the record kept while measuring (nothing measured is lost to a crash), run.h5, older runs
upgraded, the catalog of every run, and the AI's tools to find runs and compute from them."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import labharness
from labharness import ScriptedApprover, rundata
from simlab import build_virtual_lab

SWEEP = """
plan: gate_sweep
setup: { smu.output: on }
sweep: { smu.V: {from: 0, to: 2, step: 0.1} }
measure: [lockin.X, smu.I]
after: { smu.V: 0 }"""


def crashed_run(root, points=5, torn=False):
    """A run as a crash leaves it: its record without a finish frame, the lock let go (as the system does when a
    process dies) and its file left behind."""
    folder = root / "20261024-101500-gate_sweep"
    folder.mkdir(parents=True)
    r = rundata.Recorder(folder)
    r.write("start", {"plan": "plan: gate_sweep\n", "meta": {"run_id": folder.name, "plan": "gate_sweep",
                                                            "started": "2026-10-24T10:15:00.000", "planned_points": 10}})
    r.write("columns", {"columns": {"axes": [{"key": "smu.V", "unit": "V", "kind": "instrument"}],
                                    "measures": [{"key": "lockin.X", "unit": "V", "kind": "scalar"},
                                                 {"key": "sa.trace", "unit": "dBm", "kind": "trace"}]}})
    for i in range(points):
        r.write("row", values={"i": i, "t": 0.5 * i, "smu.V": i / 4, "lockin.X": i * 0.5,
                               "sa.trace": [float(i), 2.0, None]})
    r.close()
    if torn:   # the crash came while the next point was being written
        with open(folder / rundata.RECORD, "ab") as f:
            f.write(rundata._frame("row", values={"i": points, "t": 9.5, "smu.V": 9.0})[:-7])
    rundata._let_go(r._lock)
    return folder


def runtime(tmp_path):
    pytest.importorskip("mcp")
    pytest.importorskip("numpy")
    from labharness.agent.runtime import LabRuntime
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 10), tmp_path / "data", None)
    rt.auto_report = "off"
    return rt


def test_the_record_keeps_every_point_and_drops_a_torn_last_one(tmp_path):
    folder = crashed_run(tmp_path, points=5, torn=True)
    rec = rundata.read_record(folder)
    assert rec.torn and not rec.finished and len(rec.rows) == 5
    assert rec.rows[3] == {"i": 3, "t": 1.5, "smu.V": 0.75, "lockin.X": 1.5, "sa.trace": [3.0, 2.0, None]}
    assert rundata.load(folder).meta["status"] == "interrupted"     # no Kelvin is recording it


def test_a_run_being_recorded_is_never_taken_for_a_crashed_one(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir()
    r = rundata.Recorder(folder)
    r.write("start", {"plan": "", "meta": {"run_id": "run"}})
    assert rundata.being_recorded(folder) and rundata.load(folder).meta["status"] == "running"
    r.release()
    assert not rundata.being_recorded(folder) and not (folder / rundata.LOCK).exists()


def test_a_run_a_crash_ended_is_kept_as_run_h5_marked_interrupted(tmp_path):
    pytest.importorskip("h5py")
    folder = crashed_run(tmp_path, points=5, torn=True)
    assert rundata.finish(folder)
    assert sorted(p.name for p in folder.iterdir()) == ["run.h5"]                  # the record and its lock gone
    rec = rundata.load(folder)
    assert rec.meta["status"] == "interrupted" and rec.meta["points"] == 5 and "crash" in rec.meta["reason"]
    assert rec.meta["duration_s"] == 2.0 and rec.meta["finished"]
    assert [r["i"] for r in rec.rows] == [0, 1, 2, 3, 4] and rec.rows[4]["sa.trace"] == [4.0, 2.0, None]
    card = rundata.card(folder)
    assert card["status"] == "interrupted" and card["points"] == "5 of 10"
    assert card["swept"] == [{"key": "smu.V", "unit": "V", "from": 0.0, "to": 1.0, "min": 0.0, "max": 1.0}]


def test_any_key_and_value_a_run_records_is_kept_in_run_h5_and_read_back(tmp_path, monkeypatch):
    """Bug hunt of 2026-10-31: a / in a key (a card's dI/dV) made groups of it in run.h5, and the run unreadable once
    its record was removed; a NUL in a reading or in the stop reason, a list of names or a list of lists kept run.h5
    from being written at all. An older run.h5 with such groups reads back by its keys."""
    h5py = pytest.importorskip("h5py")
    from labharness.eventlog import RunRecord
    folder = tmp_path / "20261031-100000-mixed"
    rec = RunRecord(folder.name, folder, {"plan": "p"}, "op")
    rec.set_columns({"axes": [{"key": "smu.V", "unit": "V"}], "measures": [{"key": "lockin.dI/dV", "unit": "S"}]})
    rows = [{"i": 0, "t": 0.0, "smu.V": 1.0, "lockin.dI/dV": 0.5, "dev.status": "OK\x00", "dev.names": ["a", "b"],
             "spec.xy": [[1, 2], [3, 4]], "sa.trace": [1.0, None, 3.0], "rh.%2F": 40.0},
            {"i": 1, "t": 1.0, "smu.V": 2.0, "lockin.dI/dV": 0.25, "dev.status": "OK", "dev.names": ["c"],
             "spec.xy": [[5, 6]], "sa.trace": [4.0, 5.0], "rh.%2F": 41.0}]
    for r in rows:
        rec.add(r)
    rec.finish(status="aborted", points=2, reason="error: got 'OK\x00'")
    assert not rec.meta.get("warnings") and sorted(p.name for p in folder.iterdir()) == ["run.h5"]
    back = rundata.load(folder)
    assert back.rows == [dict(rows[0], **{"dev.status": "OK"}), rows[1]]   # the NUL left out, the rest as it was
    assert back.meta["reason"] == "error: got 'OK\x00'"   # its meta keeps it (JSON); run.h5's attribute can't
    assert rundata.columns(folder).order == ["i", "t", "smu.V", "lockin.dI/dV", "rh.%2F", "dev.names", "dev.status",
                                             "spec.xy", "sa.trace"]
    assert rundata.trace_row(folder, "sa.trace", 1) == {"sa.trace": [4.0, 5.0]}

    old = tmp_path / "20261030-100000-old"   # as an older Kelvin kept a key with a /: groups inside groups
    old.mkdir()
    with h5py.File(old / rundata.RUN_FILE, "w") as f:
        f.attrs["meta_json"] = '{"run_id": "20261030-100000-old", "status": "done", "points": 2}'
        f.create_dataset("points/i", data=[0.0, 1.0])
        f.create_dataset("points/lockin.dI/dV", data=[0.5, 0.25])
    assert rundata.load(old).rows == [{"i": 0, "lockin.dI/dV": 0.5}, {"i": 1, "lockin.dI/dV": 0.25}]

    again = crashed_run(tmp_path / "again")   # a run.h5 that doesn't read back never stands in for the record
    monkeypatch.setattr(rundata, "columns", lambda f: rundata.Columns(0, {}, {}, {}, {}, []))
    with pytest.raises(ValueError, match="did not read back as written"):
        rundata.finish(again)
    assert sorted(p.name for p in again.iterdir()) == ["record.bin", "record.lock"]


def test_without_h5py_a_crashed_record_is_closed_as_interrupted(tmp_path, monkeypatch):
    monkeypatch.setattr(rundata, "have_h5", lambda: False)                    # Kelvin's core alone
    folder = crashed_run(tmp_path, points=4, torn=True)
    assert rundata.finish(folder) is None
    rec = rundata.read_record(folder)
    assert rec.finished and not rec.torn and len(rec.rows) == 4            # the torn point cut off, closed
    assert rec.meta["status"] == "interrupted" and rundata.load(folder).meta["status"] == "interrupted"


def test_runs_from_before_run_h5_are_upgraded_and_their_files_kept(tmp_path):
    pytest.importorskip("h5py")
    crashed = tmp_path / "20260901-120000-gate_sweep"                      # Kelvin 0.3.19, ended by a crash
    crashed.mkdir()
    (crashed / "plan.yaml").write_text("plan: gate_sweep\nsweep:\n  smu.V: {from: 0, to: 1, step: 0.5}\n"
                                       "measure: [lockin.X]\n", encoding="utf-8")
    (crashed / "data.jsonl").write_text('{"i": 0, "t": 0.0, "smu.V": 0.0, "lockin.X": 1.0}\n'
                                        '{"i": 1, "t": 1.0, "smu.V": 0.5, "lockin.X": 2.0}\n'
                                        '{"i": 2, "t": 2.0, "smu.V": 1', encoding="utf-8")   # cut mid-line
    (crashed / "snapshot_start.json").write_text('{"smu": {"V": 0.0, "output": true}}', encoding="utf-8")
    assert rundata.load(crashed).rows[-1]["i"] == 1                        # readable even before the upgrade
    assert rundata.upgrade(crashed)
    assert sorted(p.name for p in crashed.iterdir()) == ["legacy", "run.h5"]
    assert sorted(p.name for p in (crashed / "legacy").iterdir()) == ["data.jsonl", "plan.yaml",
                                                                       "snapshot_start.json"]
    rec = rundata.load(crashed)
    assert rec.meta["status"] == "interrupted" and rec.meta["started"] == "2026-09-01T12:00:00.000"
    assert rec.meta["plan"] == "gate_sweep" and [r["lockin.X"] for r in rec.rows] == [1.0, 2.0]
    assert rec.columns["axes"][0]["key"] == "smu.V" and rec.snapshots["start"]["smu"]["output"] is True

    done = tmp_path / "20260902-090000-gate_sweep"                           # one that ended normally
    done.mkdir()
    (done / "data.jsonl").write_text('{"i": 0, "t": 0.0, "lockin.X": 1.0}\n', encoding="utf-8")
    (done / "meta.json").write_text('{"run_id": "20260902-090000-gate_sweep", "status": "done", "points": 1}',
                                    encoding="utf-8")
    (done / "analysis.json").write_text('{"assessment": "flat", "series": []}', encoding="utf-8")
    assert rundata.upgrade(done, {"axes": [], "measures": [{"key": "lockin.X", "unit": "V", "kind": "scalar"}]})
    rec = rundata.load(done)
    assert rec.meta["status"] == "done" and rec.analysis["assessment"] == "flat"
    assert rundata.card(done)["measured"][0]["unit"] == "V"


def test_a_new_analysis_updates_run_h5_and_its_card(tmp_path):
    pytest.importorskip("h5py")
    folder = crashed_run(tmp_path)
    rundata.finish(folder)
    rundata.set_analysis(folder, {"assessment": "a clear peak", "series": [
        {"y": "lockin.X", "results": [{"name": "center", "value": "0.25 ± 0.01 V"}]}]})
    assert rundata.load(folder, with_rows=False).analysis["assessment"] == "a clear peak"
    card = rundata.card(folder)
    assert card["assessment"] == "a clear peak" and card["results"] == ["lockin.X: center 0.25 ± 0.01 V"]


def test_as_python_exits_a_run_h5_open_is_finished_and_none_is_opened_after(tmp_path, monkeypatch):
    """Python's exit (before h5py's own, which takes away how a run.h5's text is read) waits for a run.h5 a thread
    has open, and no run.h5 is opened after it: a thread on its way to one is told Kelvin is closing. Then it takes
    h5py's lock for good (not here: the tests go on)."""
    pytest.importorskip("h5py")
    monkeypatch.setattr(rundata, "_closing", False)   # as it was again after this test, which ends Kelvin "exiting"
    held = []
    monkeypatch.setattr(rundata, "_hold_h5py", held.append)   # h5py's lock taken for good would stop these tests
    folder = crashed_run(tmp_path)
    rundata.finish(folder)
    inside, leave, read = threading.Event(), threading.Event(), []

    def reading():
        with rundata.h5_file(folder / rundata.RUN_FILE) as f:
            inside.set()
            leave.wait(10)
            read.append(json.loads(f.attrs["meta_json"])["status"])

    reader = threading.Thread(target=reading)
    reader.start()
    assert inside.wait(10)
    exiting = threading.Thread(target=rundata._at_exit)
    exiting.start()
    deadline = time.monotonic() + 10
    while not rundata._closing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rundata._closing and exiting.is_alive()                  # it waits for the run.h5 open
    with pytest.raises(OSError, match="Kelvin is closing"):
        rundata.meta(folder)                                        # and opens none now
    assert held == []                                               # h5py's lock only once that run.h5 is closed
    leave.set()
    reader.join(10)
    exiting.join(10)
    assert not exiting.is_alive() and read == ["interrupted"] and len(held) == 1


def test_runs_read_and_written_in_the_background_as_python_exits_never_crash_it(tmp_path):
    """Kelvin 0.3.36 in CI on Linux: every test passed, then Python said "Failed to find converter for 8 ->
    b'PYTHON:OBJECT'" three times and crashed as it exited (a segmentation fault, exit 139). A thread in the
    background was still reading runs' run.h5 after h5py's own exit had taken away how their text is read: the
    automatic reports a run rewrites 20 s after it ends (flush_reports), as the tests' last plans did; a tidy, the
    catalog or a queue ending its run can do the same. Such a thread now finishes the run.h5 it has open first, opens
    no other, and waits at h5py's lock while h5py's exit runs HDF5 (a dataset it released then crashed Python inside
    H5Tunregister): Python exits as it should, where it hung or crashed (on Windows too)."""
    pytest.importorskip("h5py")
    folders = []
    for k in range(3):
        folder = crashed_run(tmp_path / str(k))
        rundata.finish(folder)
        folders.append(str(folder))
    code = ("import sys, threading, time\n"
            "from labharness import rundata\n"
            "def work():\n"
            "    n = 0\n"
            "    while True:\n"
            "        for folder in sys.argv[1:]:\n"
            "            n += 1\n"
            "            try:\n"
            "                rundata.meta(folder)\n"
            "                rundata.set_analysis(folder, {'assessment': 'pass %d' % n, 'series': []})\n"
            "            except Exception:\n"
            "                pass   # as the catalog does: on to the next run\n"
            "threading.Thread(target=work, daemon=True).start()\n"
            "time.sleep(1.0)\n")
    src = str(Path(labharness.__file__).resolve().parents[1])   # this Kelvin, wherever it is
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in (src, os.environ.get("PYTHONPATH")) if p))
    done = subprocess.run([sys.executable, "-c", code] + folders, capture_output=True, text=True, errors="replace",
                          timeout=60, env=env)
    said = done.stdout + done.stderr
    assert done.returncode == 0 and "Failed to find converter" not in said, (done.returncode, said[-3000:])
    assert [rundata.meta(f)["status"] for f in folders] == ["interrupted"] * 3   # each run.h5 whole


def test_the_catalog_finds_runs_and_answers_read_only_sql(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    rt.run(SWEEP)
    run_id = rt.runs_index(1)[0]["run_id"]
    found = rt.find_runs(quantity="voltage")
    assert run_id in found and "swept smu.V 0→2 V" in found and "lockin.X [V]" in found
    day = "%s-%s-%s" % (run_id[:4], run_id[4:6], run_id[6:8])
    assert run_id in rt.find_runs(since=day, until=day) and run_id in rt.find_runs(device="", text="gate")
    assert "no run matches" in rt.find_runs(status="interrupted")
    table = rt.query_runs("SELECT c.unit, c.min, c.max FROM runs r JOIN columns c USING (run_id) "
                          "WHERE c.key = 'smu.V' AND r.status = 'done'")
    assert table.splitlines() == ["unit,min,max", "V,0,2"]
    assert rt.query_runs("SELECT value, text, unit FROM conditions WHERE key = 'smu.output'").splitlines() == \
        ["value,text,unit", "1,on,"]                                        # each setting at the start
    for bad in ("DELETE FROM runs", "ATTACH DATABASE 'x.db' AS x", "PRAGMA table_info(runs)", "SELECT 1; SELECT 2",
                "UPDATE runs SET status = 'done'", "CREATE TEMP TABLE x (a)", "SELECT randomblob(1000000000)"):
        assert rt.query_runs(bad).startswith("query not run"), bad
    from labharness import catalog
    monkeypatch.setattr(catalog, "QUERY_SECONDS", 0.2)
    assert "longer than" in rt.query_runs("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
                                          "SELECT count(*) FROM c")
    assert rt.query_runs("SELECT count(*) AS n FROM runs").splitlines() == ["n", "1"]   # nothing was changed
    rt.catalog.path.write_bytes(b"not a database")                          # damaged: it is only an index
    assert run_id in rt.find_runs()
    text = rt.describe_run(run_id)
    for want in ("status: done", "- key: smu.V", "unit: V", "quantity: voltage", "plan as run:", "files: run.h5"):
        assert want in text, want


def test_find_runs_takes_times_as_people_say_them(tmp_path):
    """since and until as activity takes them (today, yesterday, 2h, a date and time with a space or a T), compared
    as times; until takes in the whole minute given; what isn't a time is said, never taken for one."""
    from datetime import datetime, timedelta
    rt = runtime(tmp_path)
    rt.run(SWEEP)
    run_id = rt.runs_index(1)[0]["run_id"]
    assert run_id in rt.find_runs()
    started = datetime.fromisoformat(rt.catalog.find()[0]["started"])
    at, before, after = (t.strftime("%Y-%m-%d %H:%M") for t in (started, started - timedelta(minutes=1),
                                                                 started + timedelta(minutes=1)))
    for t in (at, at.replace(" ", "T"), at[11:]):
        assert run_id in rt.find_runs(until=t)                              # the minute it started, inclusive
    for t in (after, after.replace(" ", "T")):
        assert "no run matches" in rt.find_runs(since=t)
    assert run_id in rt.find_runs(since=before) and "no run matches" in rt.find_runs(until=before)
    assert run_id in rt.find_runs(since="today") and run_id in rt.find_runs(since="2h", until="today")
    assert "no run matches" in rt.find_runs(until="yesterday")
    assert rt.find_runs(since="last week").startswith("not searched: a time is 2026-10-28")


def test_compute_and_where_work_on_a_runs_columns_and_nothing_else(tmp_path):
    rt = runtime(tmp_path)
    rt.run(SWEEP)
    run_id = rt.runs_index(1)[0]["run_id"]
    assert rt.compute(run_id, "max(smu.V)") == "max(smu.V) = 2 (over 21 points)"
    lines = rt.compute(run_id, "smu.V * 1000", where="smu.V > 1.45").splitlines()
    assert lines[0] == "smu.V * 1000 over 6 points where smu.V > 1.45: 6 values"
    assert lines[1].startswith("min 1500 at smu.V=1.5; max 2000 at smu.V=2;")
    assert lines[2] == "i,smu.V,value" and lines[-1] == "20,2,2000"
    assert "21 values" in rt.compute(run_id, "deriv(lockin.X, smu.V)")
    data = rt.run_data(run_id, ["smu.V", "lockin.X"], 50, where="smu.V > 1.85").splitlines()
    assert data[0] == "%s: 21 rows; 2 where smu.V > 1.85" % run_id and data[1] == "smu.V,lockin.X"
    assert [line.split(",")[0] for line in data[2:]] == ["1.9", "2"]
    for bad in ("__import__('os').system('echo hi')", "smu.V.__class__", "().__class__", "[1, 2]", "lambda: 1",
                "'text'", "open('x')", "smu.V if 1", "x" * 600, "nothing.here"):
        assert rt.compute(run_id, bad).startswith("not computed"), bad
    assert rt.compute(run_id, "9 ** 9 ** 9") == "9 ** 9 ** 9 = inf (over 21 points)"   # floats: nothing runs away
    assert rt.compute(run_id, "smu.V", where="smu.V").startswith("not computed: where needs a condition")


def test_the_ai_reads_numbers_to_twelve_figures_where_a_sweep_steps_finely(tmp_path):
    """A dip at 4.0000027 GHz swept in 200 Hz steps (bug hunt of 2026-10-31): compute, get_run_data and query_runs
    gave six significant figures (the dip "at 4e+09", 101 points read as three frequencies)."""
    from labharness.eventlog import RunRecord
    rt = runtime(tmp_path)
    run_id = "20261031-120000-saw_dip"
    rec = RunRecord(run_id, rt.runs.home / run_id, {"plan": "saw_dip"}, "op")
    rec.set_columns({"axes": [{"key": "sg.freq", "unit": "Hz"}], "measures": [{"key": "det.V", "unit": "V"}]})
    for i in range(101):
        f = 3.99999e9 + 200.0 * i
        rec.add({"i": i, "t": float(i), "sg.freq": f, "det.V": 1 - 0.5 / (1 + ((f - 4.0000027e9) / 1500.0) ** 2)})
    rec.finish(status="done", points=101)
    assert rt.compute(run_id, "at_min(det.V, sg.freq)") == "at_min(det.V, sg.freq) = 4000002600 (over 101 points)"
    assert "min 0.502212389381 at sg.freq=4000002600;" in rt.compute(run_id, "det.V")
    freqs = [line.split(",")[2] for line in rt.run_data(run_id, max_rows=200).splitlines()[2:]]
    assert len(set(freqs)) == 101 and freqs[63] == "4000002600"
    assert rt.query_runs("SELECT min, max FROM columns WHERE key = 'sg.freq'").splitlines() == \
        ["min,max", "3999990000,4000010000"]


def test_a_trace_is_read_alone_from_run_h5(tmp_path):
    pytest.importorskip("h5py")
    rt = runtime(tmp_path)
    rt.run("plan: s21\nsweep: { smu.V: {from: 0, to: 1, step: 0.5} }\nmeasure: [vna.S21, smu.I]")
    run_id = rt.runs_index(1)[0]["run_id"]
    data = rt.run_data(run_id).splitlines()
    assert data[1] == "i,t,smu.V,vna.S21,smu.I" and ",trace[401]," in data[2]
    trace = rt.trace(run_id, 1, "vna.S21", 10).splitlines()
    assert trace[1] == "x,y" and 2 + 4 <= len(trace) <= 2 + 10 and "of them shown" in trace[0]   # 401 points:
    assert trace[2].startswith("3990000000,") and trace[-1].startswith("4010000000,")   # the ends, each stretch's extremes
    assert rt.trace(run_id, 7, "vna.S21") == "row must be 0…2" and "not a trace" in rt.trace(run_id, 0, "smu.I")
    assert "vna.S21 is a trace" in rt.compute(run_id, "mean(vna.S21)")


def test_kelvin_keeps_crashed_and_older_runs_when_it_starts(tmp_path):
    pytest.importorskip("h5py")
    rt = runtime(tmp_path)
    crashed_run(rt.data_dir / "runs")
    old = rt.data_dir / "runs" / "20260901-120000-gate_sweep"
    old.mkdir()
    (old / "plan.yaml").write_text(SWEEP, encoding="utf-8")
    (old / "data.jsonl").write_text('{"i": 0, "t": 0.0, "smu.V": 0.0, "lockin.X": 1e-6, "smu.I": 0.0}\n',
                                    encoding="utf-8")
    assert rt.tidy_runs() == {"interrupted": 1, "upgraded": 1, "failed": 0}
    assert rt.tidy_runs() == {"interrupted": 0, "upgraded": 0, "failed": 0}          # once is enough
    found = rt.find_runs(status="interrupted")
    assert found.startswith("2 runs") and "20261024-101500-gate_sweep" in found and old.name in found
    assert rundata.card(old)["swept"][0]["unit"] == "V"                     # units from this lab's cards
    assert any(e["type"] == "runs_kept" for e in rt.gate.log.events())


def test_runs_in_a_chats_own_folder_are_found_with_the_rest(tmp_path):
    from labharness.eventlog import RunFolders, RunStore
    folders = RunFolders(tmp_path / "data")
    store = RunStore(tmp_path / "data" / "runs", folders)
    mine = tmp_path / "Sample 7"
    made = []
    for folder in (mine, None):
        rec = store.create("gate sweep", {"plan": "gate sweep"}, "test", folder)
        rec.add({"i": 0, "t": 0.0, "smu.V": 0.1})
        rec.finish(status="done")
        made.append(rec)
    ours, home = made
    assert ours.path.parent == mine and home.path.parent == tmp_path / "data" / "runs"
    assert ours.run_id != home.run_id                                  # one id per run, in every folder
    (mine / "notes").mkdir()                                           # the person's own files there
    (mine / "notes" / "meta.json").write_text("{}", encoding="utf-8")
    assert sorted(d.name for d in folders.iterdir()) == sorted([ours.run_id, home.run_id])
    assert folders / ours.run_id == ours.path and folders.find("../notes") is None
    assert folders.chat_folders() == [mine]
