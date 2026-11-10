"""The Analysis pane's subagent (web/analyst.py), asked for 2026-11-03: the pane's own fields went, and when a dataset is
picked a model decides by itself how to process its data and how to show them; the pane shows each figure with the
processing steps it took, a few words each. It only reads (this dataset alone, as the chat's model may see it), it
never drives the lab, and with no model to ask the pane shows the dataset as measured."""
import asyncio
import json
import re

import pytest

pytest.importorskip("numpy")
pytest.importorskip("h5py")
pytest.importorskip("mcp")
pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

from labharness import ScriptedApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.web import analyst  # noqa: E402
from labharness.web.agent import ChatAgent  # noqa: E402
from labharness.web.app import STATIC, create_app  # noqa: E402
from labharness.web.providers.base import TurnResult  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from labharness.workbench import compute  # noqa: E402
from simlab import build_virtual_lab  # noqa: E402
from test_web import Scripted  # noqa: E402

GATE = ("plan: gate\nsetup: {lockin.amplitude: 0.05, smu.output: on}\nsweep: {smu.V: {from: 0, to: 3, points: 61}}\n"
        "settle: 0.5\nmeasure: [lockin.X, smu.I]\nafter: {smu.V: 0}")
MAP = ("plan: map\nsetup: {lockin.amplitude: 0.05, smu.output: on, magnet.rate: 0.5}\n"
       "sweep:\n  magnet.B: {values: [0, 0.5, 1]}\n  smu.V: {from: 0, to: 3, step: 0.1}\nsettle: 0.5\n"
       "measure: [lockin.X]\nafter: {smu.V: 0, magnet.B: 0}")
VNA = ("plan: vna\nsetup: {vna.rf: on, vna.power: -20, vna.f_start: 3.99e9, vna.f_stop: 4.01e9, vna.points: 401}\n"
       "measure: [vna.S21]")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
PAGE = (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def rt(tmp_path):
    return LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 200), tmp_path / "data")


def last(rt):
    return rt.pick_runs(last=1)[-1]


def call(i, name, args):
    return {"type": "tool_call", "id": "c%d" % i, "name": name, "input": args}


class Seeing(Scripted):
    """The scripted model, keeping what each request showed it: the tools' answers and the first message."""

    def __init__(self, turns, fail=None):
        super().__init__(turns)
        self.results, self.fail = [], fail

    async def stream(self, model, system, history, tools, emit):
        if self.fail:
            raise self.fail
        if history[-1]["role"] == "tool":
            self.results.append({r["name"]: r for r in history[-1]["results"]})
        return await super().stream(model, system, history, tools, emit)


def analyze(tmp_path, rt, run, provider, chat=None, model="scripted:m1", store=None):
    """The subagent on one dataset, from the chat agent (its MCP client up): the events the pane would get."""
    store = store or ChatStore(tmp_path / "chats")
    agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
    events = []

    async def emit(e):
        events.append(e)

    async def go():
        await agent.start()
        try:
            await agent.analyze_dataset(chat, run, model, emit)
        finally:
            await agent.close()
    asyncio.run(go())
    return events


def test_the_subagent_decides_reads_this_dataset_only_and_keeps_what_it_says(rt, tmp_path):
    rt.run(GATE)
    run = last(rt)
    before = rt.lab.instrument("smu").read("V")
    provider = Seeing([
        [call(1, "get_run_data", {"run_id": "nope", "columns": ["smu.V", "lockin.X"], "max_rows": 5}),
         call(2, "set_value", {"instrument": "smu", "parameter": "V", "value": 3})],
        [call(3, "show", {"title": "dX/dV against gate voltage", "y": "lockin.X", "steps": [{"op": "deriv"}]})],
        [call(4, "show", {"title": "The peak in lockin.X", "y": "lockin.X", "x": "smu.V",
                          "steps": [{"op": "background", "order": 1, "lo": 0, "hi": 0.5}, {"op": "smooth", "points": 5}],
                          "fit": {"model": "lorentzian"}})],
        [{"type": "text", "text": 'Done.\n{"keep": [2, 9, 2], "summary": "A peak at 1.5 V, 1.6 V wide."}'}]])
    events = analyze(tmp_path, rt, run, provider)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "start" and kinds[-1] == "done" and kinds.count("figure") == 2 and "error" not in kinds
    # it decides alone: the dataset and what it measured come first, and it has only the lookups and show
    assert set(provider.tool_names[0]) == set(analyst.READS) | {"show"} and provider.tool_names[0][-1] == "show"
    first = provider.prompts[0][0]
    assert first.startswith("The dataset: run %s." % run) and "lockin.X" in first and "measured:" in first
    assert "you never change the lab" in provider.systems[0] and "at most %d" % analyst.MAX_FIGURES in provider.systems[0]
    # its lookups read this dataset whatever they name, and a tool that would drive the lab isn't there
    seen = provider.results[0]
    assert run in seen["get_run_data"]["content"] or "smu.V" in seen["get_run_data"]["content"]
    assert not seen["get_run_data"]["is_error"]
    assert seen["set_value"]["is_error"] and "not available to the analyst" in seen["set_value"]["content"]
    assert rt.lab.instrument("smu").read("V") == before
    steps = [e["text"] for e in events if e["type"] == "step"]
    assert steps[:2] == ["Reading what was measured", "Reading the values of smu.V, lockin.X"]
    assert "Drawing The peak in lockin.X" in steps
    # each figure with its processing steps in a few words, its fit worked out by the pane's own code
    one, two = [e for e in events if e["type"] == "figure"]
    assert (one["n"], two["n"]) == (1, 2) and two["title"] == "The peak in lockin.X"
    assert one["steps"] == ["Plotted lockin.X (V) against smu.V (V)", "Took the derivative d(lockin.X)/d(smu.V)"]
    assert two["steps"] == ["Plotted lockin.X (V) against smu.V (V)",
                            "Subtracted a linear background, fitted from 0 V to 500 mV", "Smoothed over 5 points",
                            "Fitted a Lorentzian"]
    assert two["result"]["curves"][0]["run"] == run and two["result"]["curves"][0]["fit"]["model"] == "lorentzian"
    assert "figure 2: The peak in lockin.X" in provider.results[2]["show"]["content"]
    assert re.search(r"x from 0 V to 3 V, y from", provider.results[2]["show"]["content"])
    done = events[-1]
    assert done == {"type": "done", "figures": [2], "summary": "A peak at 1.5 V, 1.6 V wide.", "complete": True}


def test_with_no_model_or_one_that_fails_the_dataset_shows_as_measured(rt, tmp_path):
    rt.run(GATE)
    gate = last(rt)
    events = analyze(tmp_path, rt, gate, Scripted([]), model="")
    (note,) = [e for e in events if e["type"] == "note"]
    assert "No model to analyze with: add one in Settings → Models" in note["text"]
    (fig,) = [e for e in events if e["type"] == "figure"]
    assert fig["title"] == "As measured" and fig["steps"] == ["Plotted lockin.X (V) against smu.V (V)"]
    assert events[-1] == {"type": "done", "figures": [1], "summary": "", "complete": False}   # asked again next time
    # a run that measured only a trace: its trace against the trace's own x
    rt.run(VNA)
    vna = last(rt)
    events = analyze(tmp_path, rt, vna, Seeing([], fail=RuntimeError("the service is down")))
    (note,) = [e for e in events if e["type"] == "note"]
    assert note["text"].startswith("The model could not analyze it:") and "the service is down" in note["text"]
    (fig,) = [e for e in events if e["type"] == "figure"]
    assert fig["steps"] == ["Plotted vna.S21 (dB) against frequency (Hz), the trace of point 0"]
    assert fig["result"]["curves"][0]["n"] == 401
    # a model that answers without drawing anything: as measured, with its line
    events = analyze(tmp_path, rt, gate, Scripted([[{"type": "text", "text": '{"keep": [], "summary": "Flat."}'}]]))
    assert [e["title"] for e in events if e["type"] == "figure"] == ["As measured"]
    assert events[-1]["summary"] == "Flat." and "drew nothing" in [e for e in events if e["type"] == "note"][0]["text"]


def test_a_map_keeps_a_curve_per_outer_value_and_says_so(rt, tmp_path):
    rt.run(MAP)
    run = last(rt)
    events = analyze(tmp_path, rt, run, Scripted([
        [call(1, "show", {"title": "lockin.X per field", "y": "lockin.X", "steps": [{"op": "normalize", "how": "max"}]})],
        [{"type": "text", "text": '{"keep": [1], "summary": "The peak does not move with the field."}'}]]))
    (fig,) = [e for e in events if e["type"] == "figure"]
    assert fig["steps"] == ["Plotted lockin.X (V) against smu.V (V)", "A curve per magnet.B",
                            "Normalized to its largest value"]
    assert len(fig["result"]["curves"]) == 3


def test_what_the_subagent_sees_of_runs_follows_the_chat_and_its_rules(rt, tmp_path):
    rt.run(GATE)
    shared = last(rt)
    rt.attribution = {"incognito": True}
    rt.run(GATE)
    rt.attribution = {}
    hidden = last(rt)
    # with no chat: shared runs only; an unshared one is said, never read or drawn
    events = analyze(tmp_path, rt, hidden, Scripted([]))
    assert [e["type"] for e in events] == ["start", "step", "error"] and "unshared" in events[-1]["message"]
    # a chat whose project allows only local models: another model is refused before anything is read
    settings = Settings(tmp_path / "settings.json")
    store = ChatStore(tmp_path / "chats")
    project = store.create_project("Acme NDA")
    store.set_rules(project["id"], {"providers": ["local"], "literature": False})
    chat = store.create("scripted:m1", project["id"])
    client = TestClient(create_app(rt, settings, store, hosts=["testserver"], providers={"scripted": Scripted([])}))
    h = {"X-Kelvin-Token": settings.access_key}
    r = client.post("/api/analysis/auto", json={"run": shared, "chat": chat["id"], "model": "scripted:m1"}, headers=h)
    assert r.status_code == 400 and "not allowed" in r.json()["error"]
    assert client.post("/api/analysis/auto", json={"run": shared, "chat": "gone"}, headers=h).status_code == 404
    assert client.post("/api/analysis/auto", json={"chat": ""}, headers=h).status_code == 400


def test_the_route_streams_the_work_and_the_pane_gets_it_as_it_comes(rt, tmp_path):
    rt.run(GATE)
    run = last(rt)
    settings = Settings(tmp_path / "settings.json")
    store = ChatStore(tmp_path / "chats")
    provider = Scripted([[call(1, "show", {"title": "lockin.X", "y": "lockin.X", "fit": {"model": "lorentzian",
                                                                                         "lo": 0.5, "hi": 2.5}})],
                         [{"type": "text", "text": '{"keep": [1], "summary": "A peak at 1.5 V."}'}]])
    chat = store.create("scripted:m1")
    h = {"X-Kelvin-Token": settings.access_key}
    with TestClient(create_app(rt, settings, store, hosts=["testserver"], providers={"scripted": provider})) as client:
        r = client.post("/api/analysis/auto", json={"run": run, "chat": chat["id"], "model": "scripted:m1"},
                        headers=h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    (fig,) = [e for e in events if e["type"] == "figure"]
    assert fig["steps"][-1] == "Fitted a Lorentzian from 500 mV to 2.5 V"
    assert events[-1] == {"type": "done", "figures": [1], "summary": "A peak at 1.5 V.", "complete": True}
    assert store.load(chat["id"])["messages"] == []   # the pane's work, not the chat's: nothing is written there


def test_a_newer_analysis_for_the_same_chat_stops_the_one_before(rt, tmp_path):
    rt.run(GATE)
    run = last(rt)
    slow = Scripted([[{"type": "text", "text": "{}"}]], delay=5)
    quick = Scripted([[call(1, "show", {"title": "lockin.X", "y": "lockin.X"})], [{"type": "text", "text": "{}"}]])
    agent = ChatAgent(rt, ChatStore(tmp_path / "chats"), build_server(rt), {"slow": slow, "quick": quick})
    said = {"a": [], "b": []}

    async def go():
        await agent.start()
        try:
            async def one(key, model):
                async def emit(e):
                    said[key].append(e)
                await agent.analyze_dataset(None, run, model, emit)
            a = asyncio.ensure_future(one("a", "slow:m1"))
            await asyncio.sleep(0.5)
            b = asyncio.ensure_future(one("b", "quick:m1"))
            done = await asyncio.gather(a, b, return_exceptions=True)
            assert isinstance(done[0], asyncio.CancelledError) and done[1] is None
        finally:
            await agent.close()
    asyncio.run(go())
    assert "done" not in [e["type"] for e in said["a"]] and said["b"][-1]["type"] == "done"
    assert not agent._analysts   # nothing left at work


def test_a_complete_analysis_is_told_again_while_the_run_is_unchanged_unless_asked_again(rt, tmp_path):
    """A page opened again, or another window, shows what was found without asking the model twice; Analyze again
    asks anew. The dataset as measured (no model) is never kept: the model is asked once there is one."""
    rt.run(GATE)
    run = last(rt)
    provider = Scripted([[call(1, "show", {"title": "lockin.X", "y": "lockin.X"})],
                         [{"type": "text", "text": '{"keep": [1], "summary": "A peak."}'}],
                         [call(2, "show", {"title": "smu.I", "y": "smu.I"})],
                         [{"type": "text", "text": '{"keep": [1], "summary": "A line."}'}]])
    agent = ChatAgent(rt, ChatStore(tmp_path / "chats"), build_server(rt), {"scripted": provider})
    told, asked = [], []

    async def go():
        await agent.start()
        try:
            for model, again in (("", False), ("scripted:m1", False), ("scripted:m1", False), ("scripted:m1", True)):
                events = []

                async def emit(e):
                    events.append(e)
                await agent.analyze_dataset(None, run, model, emit, again)
                told.append(events)
                asked.append(len(provider.seen))   # the model's requests so far
        finally:
            await agent.close()
    asyncio.run(go())
    plain, first, second, third = told
    assert plain[-1]["complete"] is False and first[-1]["summary"] == "A peak."   # the model asked once there is one
    assert second == first and asked == [0, 2, 2, 4]   # told again as it was, the model not asked; then asked anew
    assert third[-1]["summary"] == "A line."
    assert 'again: !!again' in _js("analyzeRun")


def test_the_steps_in_words_follow_the_names_and_units_at_each_step(rt):
    rt.run(GATE)
    run = last(rt)
    spec = {"runs": [run], "y": "lockin.X", "where": "smu.V > 0.2",
            "steps": [{"op": "crop", "lo": 0.5}, {"op": "bin", "width": 0.1}, {"op": "scale", "factor": 1e6, "unit": "µV"}],
            "fit": {"model": "a*x + b", "fix": {"b": 0}}}
    out = compute(rt, spec)
    assert analyst.steps(spec, out) == [
        "Plotted lockin.X (V) against smu.V (V)", "Only the points where smu.V > 0.2", "Kept smu.V from 500 mV on",
        "Averaged in bins of 100 mV of smu.V", "Scaled: multiplied by 1e+06, in µV", "Fitted y = a*x + b with b = 0 held"]
    spectrum = {"runs": [run], "y": "lockin.X", "steps": [{"op": "fft"}, {"op": "abs"}, {"op": "log10"}]}
    assert analyst.steps(spectrum, compute(rt, spectrum))[1:] == [
        "Took the FFT amplitude spectrum", "Took |FFT lockin.X|", "Took log10 |FFT lockin.X|"]
    sym = {"runs": [run], "y": "smu.I", "x": "smu.V - 1.5", "steps": [{"op": "antisymmetrize"}, {"op": "integrate"},
                                                                     {"op": "normalize", "how": "range"}],
           "fit": {"model": "line", "hi": 1}}
    words = analyst.steps(sym, compute(rt, sym))
    assert words[0] == "Plotted smu.I (A) against smu.V - 1.5 (V)"
    assert words[1:] == ["Took the antisymmetric part about smu.V - 1.5 = 0",
                         "Integrated smu.I (antisymmetric part) over smu.V - 1.5", "Rescaled to run from 0 to 1",
                         "Fitted a straight line up to 1 V"]


def test_the_last_answer_says_what_is_kept_whatever_surrounds_it():
    assert analyst.final('{"keep": [3, 1], "summary": "  Two   peaks. "}', 3) == ([3, 1], "Two peaks.")
    assert analyst.final('Here it is:\n{"keep": [7, "2"], "summary": "x"}\nThanks', 2) == ([2], "x")
    assert analyst.final("I drew the gate sweep.", 5) == ([3, 4, 5], "I drew the gate sweep.")
    assert analyst.final("", 0) == ([], "")   # nothing drawn: the caller shows the dataset as measured
    keep, summary = analyst.final('{"keep": [1], "summary": "%s"}' % ("y" * 500), 1)
    assert keep == [1] and len(summary) == analyst.SUMMARY_CHARS and summary.endswith("…")


# ------------------------------------------------------------------ the pane itself

def _js(name):
    start = JS.index("function %s(" % name)
    return JS[start:JS.index("\n  }\n", start)]


def test_the_pane_has_a_dataset_picker_and_what_the_subagent_made_of_it():
    pane = PAGE[PAGE.index('<section id="anpane"'):PAGE.index("</section>", PAGE.index('<section id="anpane"'))]
    for part in ('<select id="an-run" class="an-run" aria-label="Dataset">', 'id="an-status"', 'id="an-note"',
                 'id="an-summary"', 'id="an-figures"', 'id="an-again"', 'id="close-analysis"'):
        assert part in pane, part
    for gone in ("an-y", "an-x", "an-where", "an-group", "an-steps", "an-add-step", "an-model", "an-expr", "an-lo",
                 "an-hi", "an-fitparams", "an-keep", "an-csv", "an-ask", "an-more-run", "an-chips", "an-cols"):
        assert 'id="%s"' % gone not in PAGE and '$("%s")' % gone not in JS, gone
    view = pane[pane.index('<div id="an-view"'):pane.index('<div id="nb-view"')]   # the Notebook: the other view
    assert len(re.findall(r"<(input|textarea)\b", view)) == 0 and pane.count("<select") == 1
    # a pick asks the subagent (the chat's model, as that chat may see the run); picked again, what it found shows
    run = _js("analyzeRun")
    assert 'fetch("/api/analysis/auto"' in run and 'model: $("model").value || ""' in run and 'chat: S.chatId || ""' in run
    assert "ANA.ctl.abort()" in run and "if (!again && kept && kept.key === fresh.key) { drawAnalysis(kept); return; }" in run
    assert 'r.status === "running"' in run
    assert "if (!job.error && job.kept && job.complete) ANA.done.set(id, job);" in run   # never the fallback
    assert "job.complete = !!ev.complete;" in _js("anEvent")
    assert "ANA.follow = false;" in JS[JS.index('$("an-run").addEventListener("change"'):][:200]   # the person's pick
    assert 'analyzeRun(ANA.run, true)' in _js("initAnalysis")
    # each figure: its title, the plot, the steps it took (a list), its fit; Kelvin's analyze shows the same way
    fig = _js("anFigureEl")
    assert 'h("figcaption", { class: "an-fig-title" }' in fig and 'h("ol", { class: "an-taken"' in fig
    assert "lineChart(" in fig and "anFits(curves)" in fig
    opened = _js("openAnalysis")
    assert "analyzeRun(run)" in opened and "steps: ev.steps || []" in opened
    assert 'case "analysis": if (here) openAnalysis(ev); break;' in JS
    assert "openAnalysis({ run: runId, asked: true })" in JS   # a run's Analyze, to the screen even in full screen
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    for rule in (".an-figure {", ".an-taken {", ".an-status {", ".an-summary {"):
        assert rule in css, rule
    assert ".an-fitparams" not in css and ".an-field" not in css
