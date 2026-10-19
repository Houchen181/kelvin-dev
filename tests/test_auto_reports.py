"""Every run keeps its data as CSV and gets its report without anyone asking; a queue gets one report of its runs."""
import csv

import pytest

from labharness import ScriptedApprover, build_virtual_lab
from labharness.eventlog import RunStore

SWEEP = """plan: gate_sweep
setup: { smu.output: on }
sweep: { smu.V: {from: 0, to: 1, step: 0.5} }
measure: [lockin.X, smu.I]
after: { smu.V: 0 }"""


def runtime(tmp_path, answers=()):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    return LabRuntime(build_virtual_lab(seed=1), ScriptedApprover(list(answers)), tmp_path / "data", None)


def test_a_finished_run_is_also_kept_as_csv(tmp_path):
    rec = RunStore(tmp_path).create("spectrum", {"plan": "spectrum"}, "test")
    rec.add({"i": 0, "t": 0.0, "tc.T": 4.2, "sa.trace": [1.0, 2.0, 3.0], "sa.trace.axis": (100.0, 300.0)})
    rec.add({"i": 1, "t": 1.5, "tc.T": 4.3, "sa.trace": [4.0, 5.0, 6.0], "sa.trace.axis": (100.0, 300.0)})
    rec.finish(status="done")
    rows = list(csv.reader(open(rec.path / "data.csv", encoding="utf-8")))
    assert rows[0] == ["i", "t", "tc.T"] and rows[2] == ["1", "1.5", "4.3"]     # traces go to their own file
    trace = list(csv.reader(open(rec.path / "sa.trace.csv", encoding="utf-8")))
    assert trace[0] == ["i", "x", "sa.trace"] and trace[1] == ["0", "100", "1.0"] and trace[6] == ["1", "300", "6.0"]


def test_every_run_gets_its_report_in_its_own_folder(tmp_path):
    rt = runtime(tmp_path, [True, True])
    out = rt.run(SWEEP)
    run_id = rt.runs_index(1)[0]["run_id"]
    names = [f["name"] for f in rt.run_files(run_id)]
    assert names == ["report.pdf", "report.pptx", "data.csv"]
    assert "saved in the run folder: data.csv, report.pdf and report.pptx" in out
    assert (rt._run_dir(run_id) / "report.pdf").read_bytes().startswith(b"%PDF")
    rt.auto_report = "off"                                                   # only when asked
    rt.run(SWEEP)
    second = rt.runs_index(1)[0]["run_id"]
    assert [f["name"] for f in rt.run_files(second)] == ["data.csv"]         # the data is always kept
    with pytest.raises(KeyError):
        rt.run_file(run_id, "../events.jsonl")
    with pytest.raises(KeyError):
        rt.run_file(run_id, "plan.yaml")                                     # only reports and CSV are offered


def test_a_queue_ends_with_one_report_of_all_its_runs(tmp_path):
    rt = runtime(tmp_path, [True])
    rt.auto_report = "pdf"
    rt.queue_add(SWEEP)
    rt.queue_add(SWEEP.replace("to: 1,", "to: 2,"))
    assert rt.queue_start().startswith("queue started")
    rt._queue_thread.join(120)
    assert [i.status for i in rt.queue.items] == ["done", "done"]
    for item in rt.queue.items:
        assert [f["name"] for f in rt.run_files(item.run_id)] == ["report.pdf", "data.csv"]
    reports = [r["name"] for r in rt.reports_index()]
    assert len(reports) == 1 and "Queue_of" in reports[0] and reports[0].endswith(".pdf")


def test_the_report_setting():
    from labharness.web.settings import Settings
    import tempfile
    from pathlib import Path
    s = Settings(Path(tempfile.mkdtemp()) / "settings.json")
    assert s.public()["auto_report"] == "both"                                # on by default
    s.update({"auto_report": "pptx"})
    assert s.get("auto_report") == "pptx" and s.public()["auto_report"] == "pptx"
    s.update({"auto_report": "nonsense"})
    assert s.public()["auto_report"] == "both"
    s.update({"auto_report": "off"})
    assert s.public()["auto_report"] == "off"
