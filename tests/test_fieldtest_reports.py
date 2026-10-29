"""Reports and run folders after the field tests (plan item 24): a run cut short by a lost instrument still gets its
report, and a run's folder name stays short enough for Windows paths."""
import os

import pytest

from labharness import ScriptedApprover
from simlab import build_virtual_lab
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


def test_the_fits_fit_run_keeps_are_in_the_runs_reports_with_who_kept_them_and_when(tmp_path):
    """fit_run keeps each fit in run.h5 (/fits/<name>), but a report showed only the one accepted with analyze_run.
    A kept fit is now in the run's reports, its points with the model and its values, saying who kept it and when;
    beyond MAX_KEPT the others are listed by name."""
    from labharness import datatools, fitting
    from labharness.reports import collect
    from test_reports import check_pptx
    rt = runtime(tmp_path)
    rt.user = "ada"   # signed in to the portal: the agent keeps it for her
    rt.run(SWEEP)
    run_id = rt.runs_index(1)[0]["run_id"]
    _, text = datatools.fit_run(rt, run_id, "linear", x="smu.V", y="smu.I")
    assert "as /fits/smu.I linear" in text and "its scan report shows it now (report.pdf, report.pptx)" in text, text
    _, text = datatools.fit_run(rt, run_id, "a*x + c", x="smu.V", y="smu.I", x_range=[0.5, None], name="above 0.5 V")
    kept = {f["name"]: f for f in fitting.stored(rt._run_dir(run_id))}
    assert kept["smu.I linear"]["by"] == "agent" and kept["smu.I linear"]["account"] == "ada"
    assert kept["above 0.5 V"]["stored"][:4].isdigit()                       # when, as an ISO time
    folder = rt._run_dir(run_id)
    words = " ".join(pdf_strings(folder / "report.pdf"))
    assert "Kept fit “smu.I linear” vs smu.V: linear fit, kept by agent for ada on " in words, words
    assert "Kept fit “above 0.5 V” vs smu.V: expression fit, kept by agent for ada on " in words
    assert "resistance (1/slope)" in words and "points 8 (smu.V from 0.5 to its end)" in words
    slides = " ".join(check_pptx(folder / "report.pptx"))
    assert "Kept fit “smu.I linear”" in slides and "kept by agent for ada" in slides
    made = rt.report([run_id], title="Gate sweep", fmt="pdf")   # a report of runs someone asks for shows them too
    assert "Kept fit “above 0.5 V”" in " ".join(pdf_strings(rt.reports_dir / made[0]["name"]))
    assert [e for e in rt.gate.log.events() if e["type"] == "fit_kept"][-1]["account"] == "ada"
    for n in range(collect.MAX_KEPT):   # more than a report draws: the rest by name, with who and when
        datatools.fit_run(rt, run_id, "linear", x="smu.V", y="smu.I", name="again %d" % n)
    words = " ".join(pdf_strings(folder / "report.pdf"))
    assert "Other fits kept with the run (in run.h5)" in words and "above 0.5 V expression by agent for ada" in words
    answer = rt.analyze(run_id, "smu.I", "linear", accept=True)   # the accepted fit is the run's result, as before
    assert "kept as the run's fit" in answer
    assert "accepted by agent for ada" in " ".join(pdf_strings(folder / "report.pdf"))


def test_each_lab_keeps_its_data_in_a_folder_named_after_it(tmp_path):
    """F2 l05, l08, l09: a real lab's runs sat under ~/Kelvin/virtual-lab. A lab gets its own folder, named after
    it, recorded so a renamed lab keeps it; nothing moves, and the old virtual-lab's runs are still found."""
    from labharness import labfolders
    home = tmp_path / "Kelvin"
    mine = labfolders.folder_for(home, "Confocal B114: emitters", key="C:/K/lab.yaml")
    assert mine == home / "labs" / "Confocal_B114_emitters"
    assert labfolders.folder_for(home, "Confocal B115", key="C:/K/lab.yaml") == mine   # renamed: same folder
    other = labfolders.folder_for(home, "Confocal B114: emitters", key="D:/other/lab.yaml")
    assert other == home / "labs" / "Confocal_B114_emitters_2"                                  # never shared
    assert labfolders.safe_name("CON") == "lab_CON" and labfolders.safe_name("  ") == "lab"
    assert set(labfolders.all_folders(home)) == {home / "virtual-lab", mine, other}


def test_a_lab_that_measured_into_virtual_lab_keeps_it(tmp_path):
    """An older Kelvin put a real lab's runs in virtual-lab: that lab goes on using it, so nothing it made moves."""
    from labharness import labfolders
    from labharness.eventlog import RunStore
    home = tmp_path / "Kelvin"
    rec = RunStore(home / "virtual-lab" / "runs").create("rt_check", {"plan": "rt_check"}, "agent")
    rec.meta.update(lab="My lab", simulated=False)
    rec.finish(status="done", points=0)
    assert labfolders.folder_for(home, "My lab", key="k1") == home / "virtual-lab"
    assert labfolders.folder_for(home, "Another lab", key="k2") == home / "labs" / "Another_lab"


def test_switching_to_a_lab_switches_to_its_folder_and_old_links_still_work(tmp_path):
    """A switch to another lab mid-session (the first run's set-up): from then on runs go to the lab's own folder,
    while the old folder's runs and reports (here virtual-lab, where an older Kelvin kept them) are still found."""
    from labharness.agent.runtime import LabRuntime
    pytest.importorskip("mcp")
    home = tmp_path / "Kelvin"
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 20), home / "virtual-lab", None)
    rt.home = home
    rt.run(SWEEP)
    first = rt.runs_index(1)[0]["run_id"]
    made = rt.make_report([first])
    report = made.split("report: ")[1].split(" (")[0]
    mine = home / "labs" / "My_lab"
    rt.lab_folder = lambda lab, info: mine
    rt.switch_lab(build_virtual_lab(seed=2), None, {"kind": "file", "path": str(home / "lab.yaml")}, "test")
    assert rt.data_dir == mine and rt.events_path == mine / "events.jsonl"
    rt.run(SWEEP)
    assert (mine / "runs").is_dir() and len(list((mine / "runs").iterdir())) == 1                # new runs here
    assert rt.runs.find(first) == home / "virtual-lab" / "runs" / first                         # old ones found
    assert rt.report_path(report).is_file()                                                     # old report links
    assert rt.describe_run(first).startswith("run:") or first in rt.describe_run(first)
    assert (home / "virtual-lab" / "runs" / first / "run.h5").is_file()                         # nothing moved
    events = (home / "virtual-lab" / "events.jsonl").read_text(encoding="utf-8")
    assert '"type": "data_folder"' in events and str(mine).replace("\\", "\\\\") in events


def test_the_app_names_a_real_labs_folder_after_it(tmp_path):
    pytest.importorskip("starlette")
    from labharness.web.app import lab_data_folder
    home = tmp_path / "Kelvin"
    lab = build_virtual_lab(seed=1)
    lab.config.name = "My lab"
    assert lab_data_folder(home, lab, {"kind": "file", "path": str(home / "lab.yaml")}) == home / "labs" / "My_lab"
