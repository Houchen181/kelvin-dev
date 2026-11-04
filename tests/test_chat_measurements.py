"""A measurement made from the chat reaches the Runs and Analysis panes, whatever ran it (user report, 2026-11-01: "once
you run a measurement inside the chat, the measured data is not correctly appearing in the run panel as well as the
analysis panel"). A lab script's run never reached the live view the Runs pane follows, and the chat tied a run to its
step only for run_plan: a script's measurement got no card and no tab, and the Analysis pane never heard of it. The
Analysis pane also stayed on an older run after a new one, even untouched; it now follows the newest run until the
person makes the analysis theirs."""
import re

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

from labharness import ScriptedApprover, scriptrun  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.web import agent as web_agent  # noqa: E402
from labharness.web.agent import RUN_TOOLS, runs_in, runs_named  # noqa: E402
from labharness.web.app import STATIC, create_app  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from simlab import build_virtual_lab  # noqa: E402

from test_scan_library import SCRIPT, script_runtime, scripts, write  # noqa: E402,F401
from test_web import Scripted, stream_turn  # noqa: E402

JS = (STATIC / "app.js").read_text(encoding="utf-8")


def _js(name):
    """One function of app.js, by its name."""
    start = JS.index("function %s(" % name)
    return JS[start:JS.index("\n  }\n", start)]


def _method(name):
    """One method of the chat's reply (class Reply), by its name."""
    start = JS.index("\n    %s(" % name)
    return JS[start:JS.index("\n    }\n", start)]


# ------------------------------------------------------------------ a lab script's run, from the chat

def test_a_lab_scripts_run_from_the_chat_follows_live_and_gets_its_card(tmp_path, scripts):  # noqa: F811
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([True]))
    write(scripts, "sweep.py", SCRIPT)
    turns = [[{"type": "tool_call", "id": "c1", "name": "run_script", "input": {"script": "sweep.py"}}],
             [{"type": "text", "text": "Done."}]]
    settings, store = Settings(tmp_path / "settings.json"), ChatStore(tmp_path / "chats")
    folders, python = rt.scripts_folders, rt.scripts_python
    app = create_app(rt, settings, store, hosts=["testserver"], providers={"scripted": Scripted(turns)})
    rt.scripts_folders, rt.scripts_python = folders, python   # the test's own, as Settings would have them
    client, headers = TestClient(app), {"X-Kelvin-Token": settings.access_key}
    with client:
        chat = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        events = stream_turn(client, headers, chat["id"], "run my sweep script", "scripted:m1")
        progress = [e for e in events if e["type"] == "run_progress"]
        assert progress, [e["type"] for e in events]   # the chat's card follows it, as a plan's
        run_id = progress[-1]["run_id"]
        assert progress[-1]["status"] == "done" and progress[-1]["done"] == 2 and progress[-1]["total"] is None
        assert next(e for e in events if e["type"] == "tool_end")["run_id"] == run_id   # its card, its tab

        live = client.get("/api/live?since=0", headers=headers).json()["live"]   # what the Runs pane drew
        assert live["run_id"] == run_id and live["status"] == "done" and live["script"] is True
        assert live["total"] is None and live["done"] == 2
        assert [a["key"] for a in live["axes"]] == ["smu.V"] and [m["key"] for m in live["measures"]] == ["lockin.X"]
        assert [r["smu.V"] for r in live["rows"]] == [0, 0.4]   # the first row's too, before it was an axis
        assert all(isinstance(r["lockin.X"], float) for r in live["rows"])

        runs = client.get("/api/runs", headers=headers).json()["runs"]   # the Runs and Analysis panes' lists
        assert runs[0]["run_id"] == run_id and runs[0]["status"] == "done" and runs[0]["points"] == 2
        data = client.get("/api/runs/" + run_id, headers=headers).json()
        assert [a["key"] for a in data["axes"]] == ["smu.V"] and [r["smu.V"] for r in data["rows"]] == [0, 0.4]
    assert runs_in([store.load(chat["id"])]) == [run_id]   # this chat's report has it too


def test_a_scripts_run_that_fails_on_its_way_out_ends_in_the_live_view(tmp_path, scripts, monkeypatch):  # noqa: F811
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([True]))
    write(scripts, "sweep.py", SCRIPT)

    def broken(self):
        self.rec_id = "20261101-120000-sweep.py"
        self._emit(event="start", run_id=self.rec_id, plan="sweep.py", total=None, axes=[], measures=[], script=True)
        raise RuntimeError("the disk went away")
    monkeypatch.setattr(scriptrun.ScriptRun, "execute", broken)
    with pytest.raises(RuntimeError):
        rt.run_script("sweep.py")
    live = rt.live_view()
    assert live["run_id"] == "20261101-120000-sweep.py" and live["status"] == "aborted"   # nobody follows it forever


def test_a_scripts_live_view_grows_its_axes_and_says_it_is_a_script(tmp_path):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    reads = [{"key": "lockin.X", "unit": "V", "kind": "scalar"}]
    rt._on_progress({"event": "start", "run_id": "r1", "plan": "sweep.py", "total": None, "axes": [], "measures": [],
                     "script": True})
    first = {"i": 0, "t": 0.1, "smu.V": 0.0, "lockin.amplitude": 0.05, "lockin.X": 1e-6}
    rt._on_progress({"event": "point", "run_id": "r1", "done": 1, "total": None, "row": first, "eta_s": None,
                     "columns": {"axes": [], "measures": reads}})
    assert rt.held_by() == "the running script sweep.py (point 1)"
    second = dict(first, i=1, t=0.2, **{"smu.V": 0.5, "lockin.X": 2e-6})
    rt._on_progress({"event": "point", "run_id": "r1", "done": 2, "total": None, "row": second, "eta_s": None,
                     "columns": {"axes": [{"key": "smu.V", "unit": "V", "kind": "instrument"}], "measures": reads}})
    live = rt.live_view()
    assert [a["key"] for a in live["axes"]] == ["smu.V"] and live["total"] is None and live["script"] is True
    assert [r["smu.V"] for r in live["rows"]] == [0.0, 0.5] and live["rows"][0]["lockin.amplitude"] == 0.05
    assert rt._kept("smu")["V"][2] == "set by the run" and rt._kept("lockin")["amplitude"][2] == "set by the run"
    assert rt._kept("lockin")["X"][2] == "measured by the run"


def test_a_stopped_scripts_assessment_counts_its_points():
    from labharness.analysis import analyze_run
    rows = [{"i": k, "t": 0.1 * k, "smu.V": 0.1 * k, "lockin.X": 1e-6 * k} for k in range(20)]
    axes, reads = [{"key": "smu.V", "unit": "V"}], [{"key": "lockin.X", "unit": "V", "kind": "scalar"}]
    a = analyze_run(rows, axes, reads, {"status": "aborted", "points": 20, "reason": "stop requested"})
    assert a["issues"][0] == "run stopped after 20 points: stop requested"   # not "of 20": a script plans none
    a = analyze_run(rows, axes, reads, {"status": "aborted", "points": 20, "planned_points": 51, "reason": "x"})
    assert a["issues"][0] == "run stopped after 20 of 51 points: x"


def test_a_plans_live_view_is_as_before(tmp_path):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    rt._on_progress({"event": "start", "run_id": "r2", "plan": "gate_sweep", "total": 51, "done": 0,
                     "axes": [{"key": "smu.V", "unit": "V"}], "measures": [{"key": "lockin.X", "unit": "V",
                                                                            "kind": "scalar"}]})
    rt._on_progress({"event": "point", "run_id": "r2", "done": 1, "total": 51, "eta_s": 30,
                     "row": {"i": 0, "t": 0.1, "smu.V": 0.0, "lockin.X": 1e-6, "lockin.Y": 3e-7}})
    assert rt.held_by() == "the running plan gate_sweep (point 1 of 51, ~30 s left)"
    live = rt.live_view()
    assert live["script"] is False and set(live["rows"][0]) == {"i", "t", "smu.V", "lockin.X"}   # only its columns


# ------------------------------------------------------------------ which runs a run tool's answer names

def test_every_run_tool_names_its_runs_and_no_other_tool_does():
    plan = ("run 20261101-151803-gate_sweep done: 51 points, 48 s.\nsaved in C:\\data\\runs\\20261101-151803-gate_sweep: "
            "run.h5 (every point)\nrows: get_run_data(run_id='20261101-151803-gate_sweep')")
    script = ("script sweep.py done: 8 s, 66 instrument commands through the gate\nsaved in C:\\data\\runs\\"
              "20261101-153641-sweep.py: script.py (as it ran), output.txt\nrows: get_run_data(run_id='20261101-153641-sweep.py')")
    recipe = ("recipe cooldown (2 stages), values: T=10\nstage find: run 20261101-160000-find passed (peak > 0)\n"
              "stage fine: run 20261101-160500-fine passed\ndone; values: T=10")
    assert runs_named("run_plan", plan) == ["20261101-151803-gate_sweep"]
    assert runs_named("run_script", script) == ["20261101-153641-sweep.py"]
    assert runs_named("run_recipe", recipe) == ["20261101-160000-find", "20261101-160500-fine"]
    assert runs_named("describe_run", plan) == [] and runs_named("find_runs", recipe) == []   # it reads runs
    chat = {"messages": [{"role": "tool", "results": [{"name": n, "content": c} for n, c in (
        ("run_recipe", recipe), ("run_script", script), ("describe_run", plan), ("run_plan", plan))]}]}
    assert runs_in([chat]) == ["20261101-151803-gate_sweep", "20261101-153641-sweep.py", "20261101-160000-find",
                               "20261101-160500-fine"]


def test_the_portal_reads_run_tools_as_the_server_does():
    """The chat read back (replayMessage) finds a step's run as tool_end gave it while it ran."""
    tools = re.search(r"const RUN_TOOLS = \[([^\]]*)\];", JS).group(1)
    assert tuple(re.findall(r'"(\w+)"', tools)) == RUN_TOOLS
    named = _js("runNamed")
    assert "matchAll(/%s/g)" % web_agent._RUN_NAMED.pattern in named
    assert "ids[ids.length - 1]" in named   # the last: a recipe's last stage, as tool_end's
    assert "runNamed(r.name, r.content)" in _js("replayMessage")


# ------------------------------------------------------------------ the chat's card and the Runs pane

def test_a_run_its_step_followed_or_that_ended_unseen_gets_its_card_and_tab():
    end = _method("toolEnd")
    assert "toolEnd(id, content, isError, runId, images, live)" in end
    assert "runId = runId || (step.card && step.card.runId);" in end   # a script that read nothing Kelvin knows
    assert "const fresh = live && !RUNS.tabs.has(runId) && !RUNS.dismissed.has(runId);" in end
    # over before the Runs pane saw it: shown as a run that starts is (showNewRun), never putting a pane away
    assert "if (fresh) showNewRun(runId, tab.plan);" in end and "openRun" not in end
    assert 'setTabStatus(tab, "finishing"); loadFinal(tab);' in end   # loadFinal tells the Analysis pane
    assert "reply.toolEnd(ev.id, ev.content, ev.is_error, ev.run_id, ev.images, true);" in JS   # as it happens
    assert ", true)" not in _js("replayMessage")   # a chat read back opens no tabs


def test_a_run_with_no_total_counts_its_points():
    view = _js("renderRunView")
    assert 'done + (total ? " / " + total : "") + " points"' in view
    assert "const open = !tab.final && !total;" in view and 'v.barBox.classList.toggle("open", open && running);' in view
    assert '(m.planned_points ? " of " + m.planned_points : "")' in view   # "Stopped after 21 points", not "of 21"
    card = _js("runCard")
    assert 'count.textContent = ev.total ? ev.done + " / " + ev.total : ev.done + " points";' in card
    assert '(ev.total ? " / " + ev.total : "") + " points"' in _js("termEvent")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".bar.open i { animation: pulse" in css and ".chat-dot, .bar.open i { animation: none !important; }" in css


# ------------------------------------------------------------------ the Analysis pane follows the newest run

def test_the_analysis_pane_follows_the_newest_run_until_the_person_makes_it_theirs():
    # since 2026-11-03 the person makes it theirs by picking a dataset (the subagent decides the rest), and a run the
    # pane follows while it is still measuring is analyzed once it ends
    assert "if (!ANA.run || ANA.follow) return startAnalysis();" in _js("refreshAnalysisRuns")
    assert "if (!ANA.run || ANA.follow) startAnalysis();" in _js("toggleAnalysis")
    assert "ANA.follow = true;" in _js("startAnalysis")
    assert "ANA.follow = false;" in _js("openAnalysis")   # what Kelvin's analyze, or a run's Analyze, asked for
    picked = JS[JS.index('$("an-run").addEventListener("change"'):]
    assert "ANA.follow = false;" in picked[:picked.index("});")]   # the person's pick
    run = _js("analyzeRun")
    assert 'if (!again && ANA.follow && r && r.status === "running")' in run and "once it ends" in run
