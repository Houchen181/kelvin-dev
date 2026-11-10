"""The lab notebook's entries (phases 3 and 6 of the ELN plan, 2026-11-07): an entry says what was done and found,
citing its runs; code checks every number against the records it cites; a person signs it and another may witness
it, and a later version is an amendment. The Notebook's subagent drafts a day's entry reading only, as a viewer
who sees shared work only, its model kept on the entry."""
import asyncio
import json

import pytest

from labharness import notebook, numbercheck
from labharness.eventlog import verify_chain

pytest.importorskip("mcp")
pytest.importorskip("h5py")

GATE = ("plan: gate\nsetup: {lockin.amplitude: 0.05, smu.output: on}\nsweep: {smu.V: {from: 0, to: 3, points: 61}}\n"
        "settle: 0.5\nmeasure: [lockin.X, smu.I]\nafter: {smu.V: 0}")


@pytest.fixture
def rt(tmp_path):
    from labharness import ScriptedApprover
    from labharness.agent.runtime import LabRuntime
    from simlab import build_virtual_lab
    return LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 50), tmp_path / "data")


def test_numbers_are_found_as_written_in_their_si_prefix_and_rounding():
    records = [{"amplitude": 0.05, "T": 1.4999, "peak": {"value": 1.523e-6, "error": 2.1e-8}}, "plan: x\nto: 3\n"]
    r = numbercheck.check("At 1.5 K the peak sits at 1.52 µV ± 0.02 µV with 50 mV excitation, swept to 3 V; "
                          "3 runs [run:20261107-100000-gate] on 2026-11-07 at 10:30, run 20261107-100000-gate, "
                          "but 3.7 K was never measured.", records)
    assert r["missing"] == ["3.7 K"], r
    assert {"1.5 K", "50 mV", "3 V"} <= set(r["found"]) and r["checked"] == len(r["found"]) + 1
    assert "1.52 µV" in " ".join(r["found"])
    assert numbercheck.said(r).startswith("%d of %d numbers are in the records it cites; not found: 3.7 K"
                                          % (r["checked"] - 1, r["checked"]))
    assert numbercheck.check("2 steps, 1 sample", []) == {"checked": 0, "found": [], "missing": []}


def test_an_entry_cites_its_runs_and_its_numbers_are_checked(rt):
    rt.run(GATE)
    run = rt.pick_runs(last=1)[-1]
    points = __import__("labharness").rundata.meta(rt._run_dir(run))["points"]
    said = rt.write_entry("Gate sweep on the virtual sample", "Swept the gate to 3 V over %d points [run:%s]; the "
                          "lock-in shows a peak. The fridge sat at 9.99 K." % (points, run), aim="Find the peak.")
    assert said.startswith("kept entry e") and "Numbers: " in said and "not found: 9.99 K" in said, said
    eid = said.split()[2]
    e = notebook.read(rt.data_dir)[eid]
    v = notebook.latest(e)
    assert v["runs"] == [run] and v["check"]["missing"] == ["9.99 K"] and v["by"] == "human" and v["version"] == 1
    assert rt.write_entry("x", "rests on [run:nope]").startswith("not kept: these runs can't be read here: nope")
    shown = rt.notebook(entry=eid)
    assert "(draft): Gate sweep on the virtual sample" in shown and "numbers: " in shown and "aim: Find the peak." in shown
    assert eid in rt.notebook(day=v["day"]) and "no entries for 1999-01-01" in rt.notebook(day="1999-01-01")
    again = rt.write_entry(body="Swept the gate to 3 V [run:%s]." % run, entry=eid)
    assert again.startswith("kept entry %s v2" % eid) and "every number" in again
    assert not verify_chain(notebook.path_of(rt.data_dir))["breaks"]


def test_a_person_signs_another_witnesses_and_a_later_version_is_an_amendment(rt):
    said = rt.write_entry("Cool-down 3", "Cooled D3 to base.")
    eid = said.split()[2]
    with pytest.raises(ValueError, match="only a person signed in"):
        rt.sign_entry(eid)
    rt.user = "ann"
    sig = rt.sign_entry(eid)
    assert sig["version"] == 1 and sig["sha256"] == notebook.latest(notebook.read(rt.data_dir)[eid])["sha256"]
    with pytest.raises(ValueError, match="ann already signed version 1"):
        rt.sign_entry(eid)
    with pytest.raises(ValueError, match="a witness is someone other than who signed it"):
        rt.sign_entry(eid, "witness")
    rt.user = "pi"
    rt.sign_entry(eid, "witness")
    e = notebook.read(rt.data_dir)[eid]
    assert notebook.status(e) == "witnessed"
    assert "witnessed by pi" in notebook.summary(eid, e) and "signed by ann" in notebook.summary(eid, e)
    rt.user = "ann"
    amended = rt.write_entry(body="Cooled D3 to base; the still heater was off.", entry=eid)
    assert amended.startswith("kept an amendment of %s v2" % eid)
    e = notebook.read(rt.data_dir)[eid]
    assert notebook.status(e) == "amended" and notebook.latest(e)["amends"] == 1
    assert e["versions"][0]["body"] == "Cooled D3 to base."   # the signed version stays as it was signed
    from labharness.agent.runtime import Viewer
    with rt.viewing(Viewer(chat="c1", model="scripted:m1")):
        with pytest.raises(ValueError, match="only a person signs"):
            rt.sign_entry(eid)


def test_the_notebook_subagent_drafts_the_day_reading_only(rt, tmp_path):
    from labharness.agent.server import build_server
    from labharness.web import notebook as subagent
    from labharness.web.agent import ChatAgent
    from labharness.web.store import ChatStore
    from test_web import Scripted
    rt.run(GATE)
    run = rt.pick_runs(last=1)[-1]
    day = run[:4] + "-" + run[4:6] + "-" + run[6:8]
    draft = {"title": "Gate sweep", "aim": "Find the peak.", "body": "One gate sweep to 3 V [run:%s]." % run,
             "runs": [run], "samples": []}
    provider = Scripted([
        [{"type": "tool_call", "id": "c1", "name": "activity", "input": {"since": day}},
         {"type": "tool_call", "id": "c2", "name": "set_value", "input": {"instrument": "smu", "parameter": "V",
                                                                           "value": 3}}],
        [{"type": "text", "text": json.dumps(draft)}]])
    agent = ChatAgent(rt, ChatStore(tmp_path / "chats"), build_server(rt), {"scripted": provider})
    events = []

    async def emit(e):
        events.append(e)

    async def go():
        await agent.start()
        try:
            await agent.draft_entry(day, "scripted:m1", emit)
        finally:
            await agent.close()
    asyncio.run(go())
    assert [e["type"] for e in events] == ["start", "step", "done"], events
    assert set(provider.tool_names[0]) <= set(subagent.READS) and "set_value" not in provider.tool_names[0]
    eid = events[-1]["entry"]
    v = notebook.latest(notebook.read(rt.data_dir)[eid])
    assert v["by"] == "agent" and v["ai"]["model"] == "scripted:m1" and v["runs"] == [run]
    assert v["check"]["missing"] == [] and "every number" in events[-1]["said"]
    assert rt.lab.instrument("smu").read("V") == 0   # it never drove anything


def test_without_a_model_the_subagent_says_so(rt, tmp_path):
    from labharness.agent.server import build_server
    from labharness.web.agent import ChatAgent
    from labharness.web.store import ChatStore
    agent = ChatAgent(rt, ChatStore(tmp_path / "chats"), build_server(rt), {})
    events = []

    async def emit(e):
        events.append(e)

    async def go():
        await agent.start()
        try:
            await agent.draft_entry("2026-11-07", "", emit)
        finally:
            await agent.close()
    asyncio.run(go())
    assert events[-1]["type"] == "error" and "No model to draft with" in events[-1]["message"]


def test_a_model_that_fails_is_said_never_silent(rt, tmp_path):
    from test_web import Scripted
    from labharness.agent.server import build_server
    from labharness.web.agent import ChatAgent
    from labharness.web.store import ChatStore

    class Refused(Scripted):
        async def stream(self, model, system, history, tools, emit):
            raise RuntimeError("the API key was refused")
    agent = ChatAgent(rt, ChatStore(tmp_path / "chats"), build_server(rt), {"scripted": Refused([])})
    events = []

    async def emit(e):
        events.append(e)

    async def go():
        await agent.start()
        try:
            await agent.draft_entry("2026-11-07", "scripted:m1", emit)
        finally:
            await agent.close()
    asyncio.run(go())
    assert events[-1] == {"type": "error", "message": "The draft stopped: the API key was refused"}, events
    assert notebook.read(rt.data_dir) == {}   # nothing kept


def test_the_notebook_panel_reads_a_day_writes_a_persons_entry_and_signs_only_for_a_person(rt, tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from labharness.web.app import STATIC, create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    settings = Settings(tmp_path / "settings.json")
    client = TestClient(create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"]))
    h = {"X-Kelvin-Token": settings.access_key}
    rt.record_step("loaded chip 2", sample="D3")
    r = client.post("/api/notebook", json={"title": "Loading", "body": "Loaded chip 2.", "day": ""}, headers=h)
    assert r.status_code == 200 and r.json()["said"].startswith("kept entry e"), r.text
    day = client.get("/api/notebook", headers=h).json()
    assert [e["title"] for e in day["entries"]] == ["Loading"] and day["entries"][0]["status"] == "draft"
    assert day["entries"][0]["by"] == "human" and any("loaded chip 2" in line for line in day["record"])
    assert day["day"] in day["days"] and day["me"] is None
    eid = day["entries"][0]["id"]
    r = client.post("/api/notebook/sign", json={"entry": eid}, headers=h)
    assert r.status_code == 400 and "Sign in to Kelvin" in r.json()["error"]
    assert client.post("/api/notebook", json={"title": "x"}, headers=h).status_code == 400
    page, js = (STATIC / "index.html").read_text(encoding="utf-8"), (STATIC / "app.js").read_text(encoding="utf-8")
    pane = page[page.index('<section id="anpane"'):page.index("</section>", page.index('<section id="anpane"'))]
    assert 'id="nb-view"' in pane and 'aria-label="Draft this day\'s entry"' in pane and "/api/notebook/draft" in js
    assert 'id="toggle-notebook"' not in page and 'id="nbpanel"' not in page and "nbpanel" not in js
