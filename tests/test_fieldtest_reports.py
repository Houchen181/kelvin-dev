"""Reports and run folders after the field tests (plan item 24): a run cut short by a lost instrument still gets its
report, and a run's folder name stays short enough for Windows paths."""
import os

import pytest

from labharness import ScriptedApprover, build_virtual_lab
from labharness.eventlog import NAME_MAX, RUN_NAME, RunFolderError, RunStore, run_name
from test_reports import check_pdf, pdf_strings

SWEEP = """plan: gate_sweep
setup: { smu.output: on }
sweep: { smu.V: {from: 0, to: 2, step: 0.2} }
measure: [lockin.X, smu.I]
after: { smu.V: 0 }"""


def runtime(tmp_path, answers=20):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * answers), tmp_path / "data", None)
    rt.report_delay = 3600
    return rt


def lose(inst, after):
    """The instrument stops answering after `after` more queries, as an unplugged GPIB instrument does."""
    real, left = inst.backend.query, [after]

    def query(command):
        left[0] -= 1
        if left[0] < 0:
            raise OSError("VI_ERROR_NLISTENERS (-1073807265): No listeners condition is detected")
        return real(command)
    inst.backend.query = query


def test_a_run_cut_short_by_a_lost_instrument_still_gets_its_report(tmp_path):
    """F2 l08, l09: make_report and every automatic scan report of an aborted run crashed ('str' - 'float'): the end
    snapshot held "ERROR: ..." for the lost instrument's drives, and the report took it for a value."""
    rt = runtime(tmp_path)
    lose(rt.lab.instruments["smu"], 40)
    answer = rt.run(SWEEP)
    assert "aborted" in answer
    run_id = rt.runs_index(1)[0]["run_id"]
    folder = rt._run_dir(run_id)
    assert not [e for e in rt.gate.log.events() if e["type"] == "report_failed"]
    assert check_pdf(folder / "report.pdf") == 1
    text = " ".join(pdf_strings(folder / "report.pdf"))
    assert "Not read at the end" in text and "smu" in text and "VI_ERROR_NLISTENERS" in text
    assert "no report" not in rt.make_report([run_id])   # a report of it on request too
    assert "no report" not in rt.make_report([run_id], kind="scan")


def test_a_garbled_reply_in_a_snapshot_does_not_stop_a_report(tmp_path):
    """A garbled enum reply ('0$No!errov' for an output's state) can't be placed against its safe value either."""
    rt = runtime(tmp_path)
    rt.run(SWEEP)
    run_id = rt.runs_index(1)[0]["run_id"]
    from labharness.reports import Collector
    from labharness.agent.runtime import event_line
    part = Collector(rt.runs, rt.lab, lambda r: None, lambda: [], event_line).run(run_id, [])
    assert part.changes   # smu.output was left on
    c = Collector(rt.runs, rt.lab, lambda r: None, lambda: [], event_line)
    assert c._changes({"smu": {"output": False}}, {"smu": {"output": "0$No!errov", "V": "ERROR: not read"}}) == []
    assert c._away({"smu": {"output": "garbled", "V": None}}) == []
    assert c._unread_at_end({"smu": {"V": "ERROR: not read: smu did not answer", "nplc": 1}}) == [
        "smu V (not read: smu did not answer)"]


def test_run_folder_names_stay_short(tmp_path):
    """F2 l09, l10: a run's folder was named after the whole plan title (70+ characters), which took a deep data
    folder past Windows' 260-character paths: run_plan failed with a bare "tool error"."""
    title = "GHB-J5 gate sweep B=0 refine Dirac peak 1.6 K 50 nA AC 13.333 Hz, second try"
    name = run_name(title)
    assert len(name) <= NAME_MAX and name.startswith("GHB-J5_gate_sweep")
    assert run_name(title + "!") != name                               # two long titles stay apart
    assert run_name("gate sweep") == "gate_sweep"                       # a short one as before
    store = RunStore(tmp_path / "runs")
    rec = store.create(title, {"plan": title}, "agent")
    assert RUN_NAME.match(rec.run_id) and len(rec.run_id) <= 16 + NAME_MAX
    again = store.create(title, {"plan": title}, "agent")
    assert again.run_id != rec.run_id and again.path.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows paths")
def test_a_deep_data_folder_shortens_the_name_and_never_fails_silently(tmp_path):
    deep = tmp_path
    while len(str(deep)) < 170:
        deep = deep / "a_rather_long_folder_name"
    store = RunStore(deep / "runs")
    rec = store.create("R_T sanity check at 300 K with the 6221 on, first cool-down", {}, "agent")
    assert len(str(rec.path)) + 48 <= 259 and rec.path.is_dir()
    deeper = tmp_path
    while len(str(deeper)) < 250:
        deeper = deeper / "b"
    with pytest.raises(RunFolderError) as e:
        RunStore(deeper / "runs").create("gate sweep", {}, "agent")
    assert "did not start" in str(e.value) and "too long" in str(e.value)


def test_run_plan_says_why_a_run_has_no_folder(tmp_path, monkeypatch):
    rt = runtime(tmp_path)

    def nowhere(*a, **k):
        raise RunFolderError("Kelvin could not make the run's folder X (Access is denied), so the run did not start "
                             "and nothing was set")
    monkeypatch.setattr(rt.engine.store, "create", nowhere)
    answer = rt.run(SWEEP)
    assert answer.startswith("not run: Kelvin could not make the run's folder")
    assert rt.lab.read("smu", "output") is False   # nothing was set


def test_a_report_keeps_to_the_length_asked(tmp_path):
    """F2 l10, F3 P8: "a one-page report" came out at 5 to 22 pages: make_report had no length at all."""
    from test_reports import check_pptx
    rt = runtime(tmp_path, answers=40)
    for _ in range(4):
        rt.run(SWEEP)
    ids = [r["run_id"] for r in rt.runs_index(10)]
    full = rt.make_report(ids, title="Gate sweeps")
    assert "1 pages" not in full and int(full.split(", ")[1].split(" ")[0]) > 1
    one = rt.make_report(ids, title="Gate sweeps for my advisor", pages=1)
    assert "(4 runs, 1 pages)" in one
    path = rt.reports_dir / one.split("report: ")[1].split(" (")[0]
    assert check_pdf(path) == 1
    text = " ".join(pdf_strings(path))
    assert "Shortened to 1 page, as asked" in text and "gate_sweep" in text
    deck = rt.make_report(ids, fmt="pptx", pages=3)
    assert "3 slides" in deck
    assert len(check_pptx(rt.reports_dir / deck.split("report: ")[1].split(" (")[0])) == 3


def test_reports_plot_the_resistance_the_measurement_chain_gives(tmp_path):
    """F2 l10, F3 P8: reports plotted raw volts where a person wanted R. With the setup's chain (50 mV rms through
    10 MOhm: 5 nA), lockin.X becomes R_xx in ohms."""
    rt = runtime(tmp_path)
    rt.measurement_chain("setup", {"excitation": {"source": "lockin.amplitude", "form": "rms",
                                                  "series_resistor": 10e6},
                                   "readings": {"lockin.X": {"gain": 1, "name": "R_xx"}}})
    rt.run(SWEEP.replace("setup: { smu.output: on }", "setup: { smu.output: on, lockin.amplitude: 0.05 }"))
    run_id = rt.runs_index(1)[0]["run_id"]
    from labharness.agent.runtime import event_line
    from labharness.reports import Collector
    part = Collector(rt.runs, rt.lab, lambda r: None, lambda: [], event_line).run(run_id, [])
    chart = next(c for c in part.charts if c.title == "R_xx")
    assert chart.y_unit == "Ω" and "5 nA rms" in chart.caption
    from labharness import rundata
    rows = rundata.load(rt._run_dir(run_id)).rows
    assert chart.series[0].ys[0] == pytest.approx(rows[0]["lockin.X"] / 5e-9)
    text = " ".join(pdf_strings(rt._run_dir(run_id) / "report.pdf"))
    assert "R_xx" in text and "From the measurement chain" in text


def test_an_accepted_fit_is_the_one_its_report_shows(tmp_path):
    """F3 (P5 P7 P8 P12): a fit chosen after the run wasn't kept, and the report kept showing the automatic one."""
    rt = runtime(tmp_path)
    rt.run(SWEEP)
    run_id = rt.runs_index(1)[0]["run_id"]
    answer = rt.analyze(run_id, "smu.I", "linear", accept=True)
    assert "kept as the run's fit (in run.h5); its scan report shows it now" in answer
    from labharness import rundata
    kept = [s for s in rundata.load(rt._run_dir(run_id), with_rows=False).analysis["series"] if s["y"] == "smu.I"]
    assert kept and kept[0]["accepted"]["model"] == "linear" and kept[0]["accepted"]["by"] == "agent"
    text = " ".join(pdf_strings(rt._run_dir(run_id) / "report.pdf"))
    assert "accepted by agent" in text and "the accepted fit" in text
    assert any(e["type"] == "fit_accepted" for e in rt.gate.log.events())
    assert "kept as" not in rt.analyze(run_id, "smu.I", "linear")   # looking again keeps nothing
