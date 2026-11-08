"""The lab notebook's integrity (phase 1 of the ELN plan; asked for 2026-11-07: "Kelvin's data saving protocol and
experimental procedure recording should follow the practice of modern ELN/LIMS system, and be ai-native").

A kept run.h5 is sealed (its SHA-256 beside it and in the record) and never changes again; what comes of the run
afterwards (a fit, a re-analysis) goes into derived.h5. The lab's record is hash-chained, so a line changed or taken
out shows where; its files stay open while a run measures; and it says which AI did what."""
import hashlib
import json
import os
import stat

import pytest

from labharness import EventLog, integrity, rundata
from labharness.eventlog import GENESIS, append_chained, line_hash, verify_chain

needs_h5 = pytest.mark.skipif(not rundata.have_h5(), reason="run.h5 needs h5py")

SWEEP = """
plan: gate_sweep
setup: {smu.output: on}
sweep:
  smu.V: {from: 0, to: 1, step: 0.25}
measure: [lockin.X]
after: {smu.V: 0}
"""


def kept_run(make_engine, tmp_path):
    engine, gate, _ = make_engine([True])
    report = engine.run(SWEEP)
    assert report.ok, report.message
    return tmp_path / "runs" / report.run_id, gate


def writable(path):
    os.chmod(str(path), stat.S_IREAD | stat.S_IWRITE)


@needs_h5
def test_a_kept_run_is_sealed_and_its_seal_goes_into_the_record(make_engine, tmp_path):
    import h5py
    d, gate = kept_run(make_engine, tmp_path)
    digest = hashlib.sha256((d / "run.h5").read_bytes()).hexdigest()
    assert (d / "run.h5.sha256").read_bytes() == ("%s *run.h5\n" % digest).encode()   # as sha256sum -c reads it
    assert rundata.sealed(d) == digest and rundata.check_seal(d) == ("intact", digest)
    for name in ("run.h5", "run.h5.sha256"):
        assert not os.stat(str(d / name)).st_mode & stat.S_IWRITE, name   # read-only: nothing changes it by mistake
    uid = rundata.meta(d)["uuid"]
    with h5py.File(str(d / "run.h5"), "r") as f:
        assert f.attrs["uuid"] == uid and len(uid) == 36
    end = [e for e in gate.log.events() if e["type"] == "run_end"][-1]
    assert end["sha256"] == digest and end["uuid"] == uid
    assert rundata.seal(d) == {"sha256": digest, "bytes": (d / "run.h5").stat().st_size, "new": False}   # once


@needs_h5
def test_a_fit_and_a_reanalysis_go_into_derived_h5_and_run_h5_never_changes(make_engine, tmp_path):
    import h5py
    from labharness import fitting
    d, _ = kept_run(make_engine, tmp_path)
    digest = rundata.sealed(d)
    rec = rundata.load(d)
    x = [r["smu.V"] for r in rec.rows]
    result = fitting.fit(fitting.MODELS["linear"](), x, [2 * v + 1 for v in x])
    assert fitting.store(d, "line", result, {"x": "smu.V", "y": "lockin.X"}) == "line"
    rundata.set_analysis(d, {"assessment": "looked at again", "series": []})
    assert rundata.check_seal(d) == ("intact", digest)   # run.h5 is as it was kept
    with h5py.File(str(d / "derived.h5"), "r") as f:
        assert "line" in f["fits"] and f.attrs["run_sha256"] == digest
        assert f.attrs["run_uuid"] == rundata.meta(d)["uuid"] and f.attrs["run_id"] == d.name
    assert [k["name"] for k in fitting.stored(d)] == ["line"]
    assert rundata.load(d).analysis["assessment"] == "looked at again"   # the latest analysis comes first
    assert rundata.read_h5(d, with_rows=False).analysis["assessment"] == "looked at again"
    fitting.store(d, "line", result, {"x": "smu.V", "y": "lockin.X"})   # kept again: replaced, still one
    assert [k["name"] for k in fitting.stored(d)] == ["line"]


@needs_h5
def test_fits_an_older_kelvin_kept_in_run_h5_still_read(make_engine, tmp_path):
    import h5py
    d, _ = kept_run(make_engine, tmp_path)
    writable(d / "run.h5")
    with h5py.File(str(d / "run.h5"), "r+") as f:   # as an older Kelvin kept a fit
        g = f.create_group("fits").create_group("old")
        g.attrs["model"], g.attrs["stored"] = "linear", "2026-10-01T00:00:00"
    from labharness import fitting
    assert [k["name"] for k in fitting.stored(d)] == ["old"]


def test_the_record_is_chained_and_a_changed_or_removed_line_shows(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    for i in range(5):
        log.write("t", i=i)
    lines = path.read_bytes().splitlines(keepends=True)
    first = json.loads(lines[0])
    assert first["prev"] == GENESIS and json.loads(lines[1])["prev"] == line_hash(lines[0])
    assert "prev" not in log.events()[0]   # the rest of Kelvin reads events as before
    ok = verify_chain(path)
    assert (ok["lines"], ok["chained"], ok["breaks"]) == (5, 5, [])
    changed = lines[:2] + [lines[2].replace(b'"i": 2', b'"i": 7')] + lines[3:]
    path.write_bytes(b"".join(changed))
    assert [b["line"] for b in verify_chain(path)["breaks"]] == [4]   # the line after the changed one
    path.write_bytes(b"".join(lines[:2] + lines[3:]))
    assert [b["line"] for b in verify_chain(path)["breaks"]] == [3]   # where a line was taken out
    path.write_bytes(b"".join(lines[1:]))
    assert [b["line"] for b in verify_chain(path)["breaks"]] == [1]   # the record's start was taken out


def test_an_older_record_goes_on_chained_and_a_line_from_elsewhere_is_named(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"seq": 1, "type": "old"}\n{"seq": 2, "type": "old"}\n', encoding="utf-8")   # before chaining
    log = EventLog(path)
    log.write("new")
    with open(path, "a", encoding="utf-8") as f:   # something that doesn't chain writes a line
        f.write('{"seq": 9, "type": "elsewhere"}\n')
    log.write("after")   # chained on from that line, as the file is now
    r = verify_chain(path)
    assert (r["lines"], r["chained"]) == (5, 2)
    assert [(b["line"], b["why"].split(",")[0]) for b in r["breaks"]] == [(4, "a line without its link")]
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert rows[2]["seq"] == 3 and rows[4]["prev"] == line_hash(path.read_bytes().splitlines()[3])


def test_the_lab_log_and_the_config_history_are_chained_too(tmp_path):
    shared = tmp_path / "instrument-log.jsonl"
    a = EventLog(tmp_path / "a" / "events.jsonl", shared=shared, user="ann")
    b = EventLog(tmp_path / "b" / "events.jsonl", shared=shared, user="bo")
    for i in range(3):
        a.write("command", i=i)
        b.write("command", i=i)
    r = verify_chain(shared)
    assert (r["lines"], r["chained"], r["breaks"]) == (6, 6, [])
    audit = tmp_path / "config-history" / "audit.jsonl"
    for i in range(3):
        append_chained(audit, {"version": i})
    assert verify_chain(audit)["chained"] == 3 and not verify_chain(audit)["breaks"]


def test_while_a_run_measures_the_record_is_not_opened_line_by_line(tmp_path, monkeypatch):
    from labharness import eventlog
    log = EventLog(tmp_path / "events.jsonl", shared=tmp_path / "instrument-log.jsonl")
    log.write("before")
    opened = []
    real = open

    def counting(file, *args, **kwargs):
        opened.append(str(file))
        return real(file, *args, **kwargs)
    monkeypatch.setattr(eventlog, "open", counting, raising=False)
    with log.holding():
        for i in range(200):
            log.write("command", i=i)
    assert len(opened) == 2   # its own record and the lab's log, once each for the run
    opened.clear()
    log.write("after")
    assert len(opened) == 2   # outside a run, a line opens and closes them as before
    assert not verify_chain(tmp_path / "events.jsonl")["breaks"]
    assert not verify_chain(tmp_path / "instrument-log.jsonl")["breaks"]


@needs_h5
def test_a_plan_holds_the_record_open_while_it_measures(make_engine, tmp_path, monkeypatch):
    from labharness import eventlog
    engine, gate, _ = make_engine([True])
    gate.log.write("before")
    opened = []
    real = open
    monkeypatch.setattr(eventlog, "open", lambda f, *a, **k: opened.append(str(f)) or real(f, *a, **k),
                        raising=False)
    n0 = len(gate.log.events())
    opened.clear()
    report = engine.run(SWEEP)
    writes = len([p for p in opened if p.endswith("events.jsonl")])
    monkeypatch.undo()
    assert report.ok and report.points == 5
    written = len(gate.log.events()) - n0
    assert written >= 10 and writes <= 4, (written, writes)   # not a line at a time


@needs_h5
def test_verify_finds_a_changed_run_and_one_changed_with_its_seal(make_engine, tmp_path):
    d, gate = kept_run(make_engine, tmp_path)
    recorded = integrity.recorded_seals(gate.log.events())
    folders = integrity.run_folders((tmp_path / "runs").iterdir())
    result = integrity.check_runs(folders, recorded)
    assert result["intact"] == [d.name]
    text = integrity.report(integrity.check_logs({"your record": tmp_path / "events.jsonl"}), result)
    assert "1 run sealed and unchanged" in text and "No problems" in text
    writable(d / "run.h5")
    with open(d / "run.h5", "ab") as f:
        f.write(b"\0")
    result = integrity.check_runs(folders, recorded)
    assert [r[0] for r in result["changed"]] == [d.name]
    assert "run.h5 changed since it was kept" in integrity.report({}, result)
    writable(d / "run.h5.sha256")   # and its seal written again to match: the record's own seal shows it
    (d / "run.h5.sha256").write_text("%s *run.h5\n" % rundata.sha256_file(d / "run.h5"), encoding="ascii")
    result = integrity.check_runs(folders, recorded)
    assert [r[0] for r in result["reseal"]] == [d.name]
    assert "run.h5 and its seal were both changed" in integrity.report({}, result)


@needs_h5
def test_an_older_run_is_sealed_late_and_says_so(tmp_path):
    from labharness.homelab import build_lab
    from test_remove_instrument import runtime_on
    pytest.importorskip("mcp")
    rt = runtime_on(tmp_path, "lab: x\ninstruments:\n  a: {card: srs/sr830}\n")
    d = rt.runs / "20261001-120000-old"
    d.mkdir(parents=True)
    rec = rundata.Recorded(finished=True)
    rec.meta = {"run_id": d.name, "status": "done", "plan": "old"}
    rec.rows = [{"i": 0, "t": 0.0, "a.X": 1.0}]
    rec.columns = {"axes": [], "measures": [{"key": "a.X", "unit": "V"}]}
    rundata.write_h5(d, rec)   # kept by a Kelvin from before seals
    assert rundata.sealed(d) is None
    done = rt.tidy_runs()
    assert done["sealed"] == 1 and rundata.check_seal(d)[0] == "intact"
    sealed = [e for e in rt.gate.log.events() if e["type"] == "run_sealed"]
    assert sealed[-1]["run"] == d.name and sealed[-1]["late"] is True
    said = rt.verify_records()
    assert "1 run sealed and unchanged" in said and "No problems" in said, said
    assert build_lab   # (imported for the lab the runtime is built on)


def test_the_record_says_which_ai_did_what(tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import Viewer
    from test_remove_instrument import runtime_on
    rt = runtime_on(tmp_path, "lab: x\ninstruments:\n  a: {card: srs/sr830}\n")
    with rt.viewing(Viewer(chat="c1", model="anthropic:claude-opus-5-5")):
        rt.gate.log.write("note", text="by the chat's model")
    rt.gate.log.write("note", text="by a person at the portal")
    notes = [e for e in rt.gate.log.events() if e["type"] == "note"]
    assert notes[0]["ai"] == {"model": "anthropic:claude-opus-5-5", "chat": "c1"}
    assert "ai" not in notes[1]
