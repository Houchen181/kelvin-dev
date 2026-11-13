"""The chat portal: providers' message conversion, the agent loop, and the web API (no network, no API keys)."""
import asyncio
import json
import threading
import time

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")
pytest.importorskip("httpx")
anyio = pytest.importorskip("anyio")

from starlette.testclient import TestClient  # noqa: E402

from simlab import build_virtual_lab, demo_envelope  # noqa: E402
from labharness.clock import ScaledClock  # noqa: E402
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.web.agent import ChatAgent  # noqa: E402
from labharness.web.app import create_app  # noqa: E402
from labharness.web.providers.base import ModelInfo, Provider, TurnResult, user_text  # noqa: E402
from simlab.demo import GATE_SWEEP, DemoProvider  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402


class Scripted(Provider):
    """Replays a fixed list of model turns."""
    name, label = "scripted", "Scripted"

    def __init__(self, turns, delay=0.0):
        self.turns = list(turns)
        self.delay = delay
        self.seen = []
        self.systems = []
        self.tool_names = []
        # the user messages as the model sees them (with recalled memory), without the word budget and the time they
        # were sent (test_harness_2026_11_12 has that)
        self.prompts = []
        self.asked = []     # the same with the word budget, as sent (brevity.py), without the time
        self.sent = []      # as sent, whole

    def available(self):
        return True

    async def models(self):
        return [ModelInfo("m1", "Model One", self.name)]

    async def stream(self, model, system, history, tools, emit):
        self.seen.append([m["role"] for m in history])
        self.systems.append(system)
        self.tool_names.append([t["name"] for t in tools])
        self.prompts.append([user_text(dict(m, brief=None, sent=None)) for m in history if m["role"] == "user"])
        self.asked.append([user_text(dict(m, sent=None)) for m in history if m["role"] == "user"])
        self.sent.append([user_text(m) for m in history if m["role"] == "user"])
        if self.delay:
            await asyncio.sleep(self.delay)
        blocks = self.turns.pop(0)
        for b in blocks:
            if b["type"] == "text":
                await emit({"type": "text", "delta": b["text"]})
        calls = [b for b in blocks if b["type"] == "tool_call"]
        return TurnResult(blocks, stop_reason="tool_calls" if calls else "end", model=model)


SET_V = [{"type": "text", "text": "Raising the gate."},
         {"type": "tool_call", "id": "c1", "name": "set_value", "input": {"instrument": "smu", "parameter": "V", "value": 3}}]
DONE = [{"type": "text", "text": "Done: **3 V**."}]


@pytest.fixture
def parts(tmp_path):
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    return runtime, Settings(tmp_path / "settings.json"), ChatStore(tmp_path / "chats")


def human(approver, answers):
    def run():
        for answer in answers:
            entry = approver.wait_for_pending(timeout=5)
            if entry:
                approver.answer(entry["id"], answer)
    threading.Thread(target=run, daemon=True).start()


def client_for(parts, providers):
    runtime, settings, store = parts
    app = create_app(runtime, settings, store, hosts=["testserver"], providers=providers)
    return TestClient(app), {"X-Kelvin-Token": settings.access_key}


def stream_turn(client, headers, chat_id, text, model):
    events = []
    with client.stream("POST", "/api/chats/%s/turn" % chat_id, json={"text": text, "model": model}, headers=headers) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.strip():
                events.append(json.loads(line))
    return events


# --- providers: history conversion (pure functions) ----------------------------------------

HISTORY = [
    {"role": "user", "text": "raise the gate"},
    {"role": "assistant", "provider": "openai", "model": "x", "raw": None, "blocks": [
        {"type": "text", "text": "ok"}, {"type": "tool_call", "id": "call.1", "name": "set_value",
                                         "input": {"instrument": "smu", "parameter": "V", "value": 3}}]},
    {"role": "tool", "results": [{"id": "call.1", "name": "set_value", "content": "smu.V 0 V → 3 V", "is_error": False}]},
]


def test_anthropic_conversion_and_fallback_echo_rule():
    from labharness.web.providers.anthropic_provider import sanitize, to_messages
    msgs = to_messages(HISTORY)
    assert msgs[1]["content"][1] == {"type": "tool_use", "id": "call_1", "name": "set_value",
                                     "input": {"instrument": "smu", "parameter": "V", "value": 3}}
    assert msgs[2]["content"][0]["tool_use_id"] == "call_1"
    raw = [{"type": "thinking", "thinking": "a", "signature": "s"}, {"type": "text", "text": "partial"},
           {"type": "tool_use", "id": "t0", "name": "x", "input": {}}, {"type": "fallback", "from": {}, "to": {}},
           {"type": "thinking", "thinking": "b", "signature": "s2"}, {"type": "text", "text": "final"}]
    assert [b["type"] for b in sanitize(raw)] == ["text", "thinking", "text"]
    # a Claude turn is replayed exactly (thinking blocks included)
    own = [{"role": "assistant", "provider": "anthropic", "model": "m", "blocks": [], "raw": raw[4:]}]
    assert to_messages(own)[0]["content"] == raw[4:]


def test_openai_conversion():
    from labharness.web.providers.openai_provider import to_messages
    msgs = to_messages("SYS", HISTORY)
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[2]["tool_calls"][0]["function"]["arguments"] == '{"instrument": "smu", "parameter": "V", "value": 3}'
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_1", "content": "smu.V 0 V → 3 V"}


def test_gemini_conversion_replays_its_own_raw_turn():
    from google.genai import types
    from labharness.web.providers.gemini_provider import to_contents
    raw = types.Content(role="model", parts=[types.Part(text="hi", thought_signature=b"sig")]).model_dump(
        mode="json", exclude_none=True)
    contents = to_contents(HISTORY + [{"role": "assistant", "provider": "gemini", "model": "g", "blocks": [], "raw": raw}])
    assert contents[1].parts[1].function_call.name == "set_value"
    assert contents[2].parts[0].function_response.response == {"result": "smu.V 0 V → 3 V"}
    assert contents[3].parts[0].thought_signature == b"sig"


# --- the web API ---------------------------------------------------------------------------

def test_api_needs_the_access_key_and_local_host(parts):
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        assert client.get("/").status_code == 200
        assert client.get("/api/bootstrap").status_code == 401
        assert client.get("/api/bootstrap", headers={"X-Kelvin-Token": "wrong"}).status_code == 401
        assert client.get("/api/bootstrap", headers=dict(headers, Host="evil.example")).status_code == 403
        assert client.post("/api/stop", json={}, headers=dict(headers, Origin="http://evil.example")).status_code == 403
        boot = client.get("/api/bootstrap", headers=headers).json()
        assert boot["default_model"] == "scripted:m1" and boot["lab"] == "Virtual Lab"


def test_turn_with_inline_approval(parts):
    runtime = parts[0]
    provider = Scripted([SET_V, DONE])
    client, headers = client_for(parts, {"scripted": provider})
    with client:
        chat = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        human(runtime.approver, [True])
        events = stream_turn(client, headers, chat["id"], "Raise the gate to 3 V", "scripted:m1")
        kinds = [e["type"] for e in events]
        assert kinds[0] == "turn_start" and kinds[-1] == "turn_end"
        assert kinds.index("tool_start") < kinds.index("approval") < kinds.index("approval_closed") < kinds.index("tool_end")
        approval = next(e for e in events if e["type"] == "approval")
        assert approval["call_id"] == "c1" and approval["items"][0].startswith("smu.V 0 V → 3 V")
        end = next(e for e in events if e["type"] == "tool_end")
        assert end["content"].startswith("smu.V 0 V → 3 V") and "(approved)" in end["content"]
        assert runtime.lab.read("smu", "V") == 3
        saved = client.get("/api/chats/" + chat["id"], headers=headers).json()
        assert [m["role"] for m in saved["messages"]] == ["user", "assistant", "tool", "assistant"]
        assert saved["title"] == "Raise the gate to 3 V"
        assert all("raw" not in m for m in saved["messages"])     # provider internals stay on the server
        assert provider.seen[1] == ["user", "assistant", "tool"]  # the model saw the tool result


def test_demo_sweep_draws_from_run_data(parts):
    runtime = parts[0]
    client, headers = client_for(parts, {"demo": DemoProvider()})
    with client:
        chat = client.post("/api/chats", json={"model": "demo:demo"}, headers=headers).json()
        human(runtime.approver, [True])
        events = stream_turn(client, headers, chat["id"], "sweep the gate", "demo:demo")
        end = next(e for e in events if e["type"] == "tool_end")
        assert end["run_id"], end
        data = client.get("/api/runs/" + end["run_id"], headers=headers).json()
        assert data["axes"] == [{"key": "smu.V", "unit": "V"}]
        assert [s["key"] for s in data["scalars"]] == ["lockin.X", "smu.I"]
        assert len(data["rows"]) == 51
        a = data["analysis"]
        assert a["series"][0]["kind"] == "peak" and a["series"][0]["fit"]["model"] == "lorentzian"
        assert a["assessment"].startswith("lockin.X has a peak") and a["next"]
        reply = "".join(e["delta"] for e in events if e["type"] == "text")
        assert "plots and data are in the Runs pane" in reply  # the demo does not repeat the assessment
        assert client.get("/api/runs/..%2Fsecrets", headers=headers).status_code == 404


def test_settings_never_echo_keys(parts):
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        r = client.post("/api/settings", json={"anthropic_api_key": "sk-ant-test-123", "bogus": "x"}, headers=headers)
        assert r.status_code == 200 and "sk-ant-test-123" not in r.text
        assert r.json()["settings"]["keys"]["anthropic_api_key"] == "settings"
        assert "bogus" not in json.loads(parts[1].path.read_text())


def test_stop_button_ends_a_running_turn(parts):
    runtime, _, store = parts

    async def main():
        agent = ChatAgent(runtime, store, build_server(runtime), {"slow": Scripted([DONE], delay=30)})
        await agent.start()
        try:
            chat = store.create("slow:m1")
            provider = agent.claim(chat["id"], "slow:m1")
            events = []

            async def emit(e):
                events.append(e)

            task = asyncio.ensure_future(agent.run_turn(chat["id"], "hello", "slow:m1", provider, emit))
            await asyncio.sleep(0.1)
            with pytest.raises(Exception):
                agent.claim(chat["id"], "slow:m1")       # one turn at a time
            assert agent.stop()
            await asyncio.wait_for(task, 5)
            return events, store.load(chat["id"]), agent.active
        finally:
            await agent.close()

    events, chat, active = anyio.run(main)
    assert events[-1] == {"type": "turn_end", "reason": "stopped"}
    assert chat["messages"][-1]["blocks"][0]["text"] == "(stopped)"
    assert active is None


def test_stop_ends_a_turn_whose_tool_an_instrument_holds_up(parts, monkeypatch):
    """An instrument that stops answering holds a tool call in its thread: the stop button still ends the turn at
    once, and a later request answers at once with what it can read, saying which instrument hasn't answered,
    instead of hanging the chat."""
    from labharness.agent import runtime as runtime_module
    runtime, _, store = parts
    monkeypatch.setattr(runtime_module, "BUSY_WAIT_S", 0.5)
    monkeypatch.setattr(runtime_module, "READ_WAIT_S", 1.0)
    release = threading.Event()
    backend = runtime.lab.instruments["smu"].backend
    query = backend.query

    def hung(cmd, *a, **k):   # the instrument has stopped answering (released at the end of the test)
        release.wait(20)
        return query(cmd, *a, **k)
    monkeypatch.setattr(backend, "query", hung)
    runtime._looked.clear()   # its values are older than lab_status reuses: it reads the instruments
    status = [{"type": "tool_call", "id": "c1", "name": "lab_status", "input": {}}]

    async def main():
        agent = ChatAgent(runtime, store, build_server(runtime), {"s": Scripted([status, DONE])})
        await agent.start()
        try:
            chat = store.create("s:m1")
            provider = agent.claim(chat["id"], "s:m1")
            events = []

            async def emit(e):
                events.append(e)

            task = asyncio.ensure_future(agent.run_turn(chat["id"], "how is the lab?", "s:m1", provider, emit))
            await asyncio.sleep(0.5)
            assert agent.stop()
            t0 = time.time()
            await asyncio.wait_for(task, 3)
            stopped_in = time.time() - t0
            busy = await asyncio.to_thread(runtime.lab_status)   # the hung call still reads the source
            return events, stopped_in, busy, agent.active
        finally:
            release.set()
            await agent.close()

    events, stopped_in, busy, active = anyio.run(main)
    assert stopped_in < 2 and events[-1] == {"type": "turn_end", "reason": "stopped"} and active is None
    end = next(e for e in events if e["type"] == "tool_end")
    assert end["is_error"] and end["content"].startswith("stopped: the person pressed stop")
    assert "\nsmu (not read now: another reading of it has waited 1 s for its answer" in busy, busy
    assert "\nlockin: " in busy   # the others are read


LOOK_PLAN = "plan: look\nrepeat: 3\nmeasure: [lockin.X]\n"


def emitter(events, chat_id):
    async def emit(e):
        events.setdefault(chat_id, []).append(e)
    return emit


def test_two_chats_answer_at_once_and_each_run_is_filed_under_its_own(parts, tmp_path):
    runtime, _, store = parts
    plan = [{"type": "tool_call", "id": "r1", "name": "run_plan", "input": {"plan": LOOK_PLAN}}]
    providers = {"a": Scripted([plan, DONE], delay=0.2), "b": Scripted([plan, DONE], delay=0.2)}

    async def main():
        agent = ChatAgent(runtime, store, build_server(runtime), providers)
        await agent.start()
        try:
            chats, events, tasks = [], {}, []
            for key in "ab":
                chat = store.create(key + ":m1")
                (tmp_path / key).mkdir()
                chat["folder"] = str(tmp_path / key)   # where this chat's runs go
                store.save(chat)
                chats.append(chat)
                provider = agent.claim(chat["id"], key + ":m1")
                tasks.append(asyncio.ensure_future(
                    agent.run_turn(chat["id"], "measure", key + ":m1", provider, emitter(events, chat["id"]))))
            await asyncio.sleep(0.1)
            both = len(agent.answering())
            await asyncio.wait_for(asyncio.gather(*tasks), 30)
            return chats, events, both, agent.answering()
        finally:
            await agent.close()

    chats, events, both, after = anyio.run(main)
    assert both == 2 and after == []   # side by side, then neither
    for chat, key in zip(chats, "ab"):
        end = next(e for e in events[chat["id"]] if e["type"] == "tool_end")
        assert "saved in " + str(tmp_path / key) in end["content"], end["content"]
        assert events[chat["id"]][-1] == {"type": "turn_end", "reason": "end"}


def test_a_chat_waits_for_another_chats_lab_call_then_says_so(parts, monkeypatch):
    """Changes to the lab go one chat's call at a time; a reading from another chat meanwhile still answers, with
    the latest values of the instrument the change uses (lablock.py)."""
    from labharness.web import agent as agent_module
    from labharness.web.agent import Busy
    runtime, _, store = parts
    monkeypatch.setattr(agent_module, "LAB_WAIT_S", 0.5)
    release = threading.Event()
    backend = runtime.lab.instruments["smu"].backend
    query = backend.query

    def hung(cmd, *a, **k):   # chat A's lab call is held up by an instrument (released at the end)
        release.wait(20)
        return query(cmd, *a, **k)
    monkeypatch.setattr(backend, "query", hung)
    change = [{"type": "tool_call", "id": "s1", "name": "set_value",
               "input": {"instrument": "smu", "parameter": "V", "value": 0.1}}]
    other = [{"type": "tool_call", "id": "s2", "name": "set_value",
              "input": {"instrument": "lockin", "parameter": "amplitude", "value": 0.02}}]
    status = [{"type": "tool_call", "id": "s3", "name": "lab_status", "input": {}}]
    talk = [{"type": "text", "text": "Just talking."}]
    providers = {"a": Scripted([change, DONE]), "b": Scripted([other, status, DONE]), "c": Scripted([talk])}

    async def main():
        agent = ChatAgent(runtime, store, build_server(runtime), providers)
        await agent.start()
        try:
            events, ids = {}, {}
            for key in "abc":
                ids[key] = store.create(key + ":m1")["id"]

            def start(key, text):
                provider = agent.claim(ids[key], key + ":m1")
                return asyncio.ensure_future(agent.run_turn(ids[key], text, key + ":m1", provider,
                                                            emitter(events, ids[key])))
            ta = start("a", "how is the lab?")
            await asyncio.sleep(0.5)   # A's call works the lab now
            with pytest.raises(Busy):
                agent.claim(ids["a"], "a:m1")   # one answer at a time in a chat
            tb, tc = start("b", "and the source?"), start("c", "hello")
            await asyncio.wait_for(asyncio.gather(tb, tc), 10)
            waiting = agent.answering(ids["a"])
            stopped_b, stopped_a = agent.stop(ids["b"]), agent.stop(ids["a"])
            await asyncio.wait_for(ta, 5)
            return events, ids, waiting, stopped_b, stopped_a
        finally:
            release.set()
            await agent.close()

    events, ids, waiting, stopped_b, stopped_a = anyio.run(main)
    b_end, b_status = [e for e in events[ids["b"]] if e["type"] == "tool_end"]
    assert b_end["content"].startswith("not run: another conversation (“how is the lab?”) is working the lab")
    assert "\nsmu (not read now: it is in use: setting smu.V" in b_status["content"], b_status["content"]
    assert "\nlockin: " in b_status["content"]   # read now, beside A's change
    assert events[ids["c"]][-1] == {"type": "turn_end", "reason": "end"}   # talking needs no lab
    assert waiting and not stopped_b and stopped_a   # B had finished; A's stop ends A
    assert events[ids["a"]][-1] == {"type": "turn_end", "reason": "stopped"}


def test_run_progress_and_the_runs_pane_api(tmp_path):
    lab = build_virtual_lab(clock=ScaledClock(60), seed=1)   # the 48 s demo sweep takes ~0.8 s
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    parts = (runtime, Settings(tmp_path / "settings.json"), ChatStore(tmp_path / "chats"))
    client, headers = client_for(parts, {"demo": DemoProvider()})
    with client:
        chat = client.post("/api/chats", json={"model": "demo:demo"}, headers=headers).json()
        human(runtime.approver, [True])
        events = stream_turn(client, headers, chat["id"], "sweep the gate", "demo:demo")
        kinds = [e["type"] for e in events]
        progress = [e for e in events if e["type"] == "run_progress"]
        assert len(progress) >= 2, kinds
        assert kinds.index("tool_start") < kinds.index("run_progress") and max(
            i for i, k in enumerate(kinds) if k == "run_progress") < kinds.index("tool_end")
        first, last = progress[0], progress[-1]
        run_id = first["run_id"]
        assert first["call_id"] == next(e for e in events if e["type"] == "tool_start")["id"]
        assert "rows" not in first                               # the data go to the runs pane, not the chat
        assert [e["done"] for e in progress] == sorted(e["done"] for e in progress)
        assert any(e["status"] == "running" for e in progress)
        assert last["status"] == "done" and last["done"] == last["total"] == 51

        live = client.get("/api/live?since=0", headers=headers).json()["live"]
        assert live["run_id"] == run_id and live["status"] == "done"
        assert [r["i"] for r in live["rows"]] == list(range(51))  # every point once, in order
        assert set(live["rows"][0]) == {"i", "t", "smu.V", "lockin.X", "smu.I"}
        assert [a["key"] for a in live["axes"]] == ["smu.V"]
        assert len(client.get("/api/live?since=50", headers=headers).json()["live"]["rows"]) == 1

        runs = client.get("/api/runs", headers=headers).json()["runs"]
        assert runs[0]["run_id"] == run_id and runs[0]["status"] == "done" and runs[0]["points"] == 51
        assert runs[0]["plan"] == "gate_sweep"
        data = client.get("/api/runs/" + run_id, headers=headers).json()
        assert data["plan"] == "gate_sweep" and data["plan_text"].startswith("plan: gate_sweep")
        assert data["meta"]["status"] == "done" and data["meta"]["points"] == 51
        assert client.post("/api/runs/stop", json={}, headers=headers).json() == {"ok": False}  # nothing running


def test_stop_run_aborts_the_plan_and_goes_safe(tmp_path):
    lab = build_virtual_lab(clock=ScaledClock(20), seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    human(runtime.approver, [True])
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("text", runtime.run(GATE_SWEEP)), daemon=True)
    worker.start()
    deadline = time.time() + 15
    while not (runtime.live and runtime.live["status"] == "running" and runtime.live["done"] >= 3):
        assert time.time() < deadline, "the run never started"
        time.sleep(0.05)
    assert runtime.stop_run()
    worker.join(15)
    assert "aborted after" in out["text"]
    assert runtime.live["status"] == "aborted" and runtime.runs_index(1)[0]["status"] == "aborted"
    assert lab.read("smu", "V") == 0                         # the drive it touched went back to safe
    assert not runtime.stop_run()


def test_projects_organize_conversations(parts):
    _, _, store = parts
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        assert client.get("/api/projects", headers=headers).json() == []
        assert client.post("/api/projects", json={"name": "   "}, headers=headers).status_code == 400
        saw = client.post("/api/projects", json={"name": "  SAW   cavities "}, headers=headers).json()
        assert saw["name"] == "SAW cavities"
        hall = client.post("/api/projects", json={"name": "Hall bars"}, headers=headers).json()
        assert [p["name"] for p in client.get("/api/bootstrap", headers=headers).json()["projects"]] == ["SAW cavities", "Hall bars"]

        a = client.post("/api/chats", json={"model": "scripted:m1", "project": saw["id"]}, headers=headers).json()
        b = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        assert a["project"] == saw["id"] and b["project"] is None
        assert client.post("/api/chats", json={"model": "x", "project": "p00000000"}, headers=headers).status_code == 404
        listed = {c["id"]: c["project"] for c in client.get("/api/chats", headers=headers).json()}
        assert listed == {a["id"]: saw["id"], b["id"]: None}      # the index file is not listed as a chat

        moved = client.patch("/api/chats/" + b["id"], json={"project": hall["id"]}, headers=headers).json()
        assert moved["project"] == hall["id"] and moved["title"] == "New chat"
        assert client.get("/api/chats/" + b["id"], headers=headers).json()["project"] == hall["id"]
        assert client.patch("/api/chats/" + b["id"], json={"project": "p00000000"}, headers=headers).status_code == 404
        client.patch("/api/chats/" + a["id"], json={"project": None}, headers=headers)
        assert store.project_of(a["id"]) is None

        renamed = client.patch("/api/projects/" + hall["id"], json={"name": "Hall bars, batch 2"}, headers=headers).json()
        assert renamed["name"] == "Hall bars, batch 2"
        assert client.delete("/api/projects/" + hall["id"], headers=headers).json() == {"ok": True}
        assert client.get("/api/chats/" + b["id"], headers=headers).json()["project"] is None  # the chat stays
        assert client.delete("/api/projects/" + hall["id"], headers=headers).status_code == 404

        client.patch("/api/chats/" + b["id"], json={"project": saw["id"]}, headers=headers)
        client.delete("/api/chats/" + b["id"], headers=headers)
        assert store._read_index()["assign"] == {}                # deleting a chat drops its filing
        assert "project" not in store.load(a["id"])              # chat files never carry the project


def race(seconds, *jobs):
    """Each job over and over in its own thread for a while: the errors each raised, and how often each ran."""
    stop, errors, runs = time.time() + seconds, [], [0] * len(jobs)

    def loop(k):
        while time.time() < stop:
            try:
                jobs[k]()
                runs[k] += 1
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
    threads = [threading.Thread(target=loop, args=(k,)) for k in range(len(jobs))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return errors, runs


def test_a_confidential_chat_stays_filed_while_the_index_is_rewritten(tmp_path):
    """Bug hunt (Windows): the index was read without the lock while a request replaced it; a read that met the
    replace took the index for empty, so a confidential, local-only chat counted as unfiled for that turn (web
    tools offered, notes for every conversation allowed), and the replace itself failed (500)."""
    store = ChatStore(tmp_path / "chats")
    p = store.create_project("Secret")
    store.set_sharing(p["id"], "confidential")
    store.set_rules(p["id"], {"providers": ["local"], "literature": False})
    chat = store.create("local:x", project=p["id"])
    n = iter(range(10 ** 9))
    seen = []

    def read():
        memory = store.memory_of(chat["id"])
        seen.append(bool(memory and memory["confidential"] and store.projects()))
    errors, runs = race(1.5, lambda: store.set_instructions(p["id"], "text %d" % next(n)), read, read)
    assert errors == [] and all(seen) and runs[0] > 10 and runs[1] > 10


def test_a_chat_saved_while_the_sidebar_reads_it_never_fails(tmp_path):
    """Bug hunt (Windows): a turn's save failed while another request read the chat's file (2211 saves in 6 s)."""
    store = ChatStore(tmp_path / "chats")
    chat = store.create("local:x")

    def save():
        chat["messages"].append({"role": "user", "text": "x" * 200})
        store.save(chat)
    errors, runs = race(1.5, save, lambda: store.load(chat["id"]), store.list)
    assert errors == [] and min(runs) > 10
    assert len(store.load(chat["id"])["messages"]) == runs[0]


def test_an_index_kelvin_cant_read_is_never_taken_for_none(tmp_path, monkeypatch):
    """An index that is there but can't be read stops what needs it, rather than open a confidential chat to every
    model, forget the marks a chat keeps from a project it left, or be written over with no projects in it."""
    from pathlib import Path
    from labharness.web import store as stored
    store = ChatStore(tmp_path / "chats")
    p = store.create_project("Secret")
    store.set_sharing(p["id"], "confidential")
    chat = store.create("local:x", project=p["id"])
    left = store.create("local:x", project=p["id"])
    store.assign(left["id"], None)                     # it keeps the confidential mark
    index = tmp_path / "chats" / "_projects.json"
    real, busy = Path.read_text, {"n": 0}

    def read_text(path, *args, **kwargs):              # in use for a moment: a replace, a virus scanner
        if path == index and busy["n"]:
            busy["n"] -= 1
            raise PermissionError(13, "Permission denied")
        return real(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read_text)
    busy["n"] = 3
    store._kept_cache = None
    assert store.load(left["id"]).get("confidential") and store.memory_of(chat["id"])["confidential"]
    monkeypatch.setattr(stored, "IN_USE_TRIES", 3)
    busy["n"] = 10 ** 6                                # in use for good: an error, never no projects
    store._kept_cache = None
    with pytest.raises(OSError, match="_projects.json"):
        store.memory_of(chat["id"])
    with pytest.raises(OSError):
        store.load(left["id"])
    busy["n"] = 0
    damaged = '{"projects": [{"id": "%s", "name": "Secret", "confidential": true' % p["id"]
    index.write_text(damaged, encoding="utf-8")       # cut short
    for unreadable in (lambda: store.memory_of(chat["id"]), lambda: store.load(left["id"]), store.list,
                       lambda: store.create_project("Other")):
        with pytest.raises(OSError, match="move it away"):
            unreadable()
    assert index.read_text(encoding="utf-8") == damaged   # never written over


def test_a_file_held_elsewhere_never_holds_up_the_rest_and_the_list_skips_it_a_while(tmp_path, monkeypatch):
    """Review of the fixes: the store waited for a file in use while holding its lock, so a chat file a backup or a
    virus scanner held stalled every other read and save (and the event loop) for up to 2 s, on every refresh of the
    chat list. It waits without the lock now, and the list leaves such a file out a while, unless it changes."""
    from pathlib import Path
    from labharness.web import store as stored
    store = ChatStore(tmp_path / "chats")
    held, other = store.create("local:x"), store.create("local:x")
    path = tmp_path / "chats" / ("%s.json" % held["id"])
    real, tries, until = Path.read_text, [], {"t": time.time() + 0.6}

    def read_text(p, *args, **kwargs):
        if p == path and time.time() < until["t"]:
            tries.append(1)
            raise PermissionError(13, "Permission denied")
        return real(p, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read_text)
    waiting = threading.Thread(target=lambda: store.load(held["id"]), daemon=True)
    waiting.start()
    time.sleep(0.1)                                         # it is trying again and again meanwhile
    start = time.time()
    store.save(store.load(other["id"]))
    assert time.time() - start < 0.3 and waiting.is_alive()
    waiting.join(5)
    monkeypatch.setattr(stored, "IN_USE_TRIES", 2)
    until["t"] = time.time() + 3600                         # held for good
    del tries[:]
    assert [c["id"] for c in store.list()] == [other["id"]] and len(tries) == 2
    assert [c["id"] for c in store.list()] == [other["id"]] and len(tries) == 2   # not tried again yet
    until["t"] = 0                                          # free again, and changed: listed at once
    store.save(store.load(held["id"]))
    assert {c["id"] for c in store.list()} == {held["id"], other["id"]}


REMEMBER_QUIRK =[{"type": "text", "text": "Noting it."},
                  {"type": "tool_call", "id": "n1", "name": "remember",
                   "input": {"scope": "project", "kind": "quirk", "text": "Gate on D3 leaks above 6 V."}}]
REMEMBER_PREF = [{"type": "text", "text": "Noting it."},
                 {"type": "tool_call", "id": "n2", "name": "remember",
                  "input": {"scope": "global", "kind": "preference", "text": "Give fields in tesla."}}]


def test_memory_scopes_never_leak(parts):
    _, settings, store = parts
    from labharness.web.agent import SYSTEM
    a = store.create_project("Alpha")
    b = store.create_project("Beta")
    store.set_instructions(a["id"], "ALPHA-RULE: keep the gate below 5 V.")
    store.set_instructions(b["id"], "BETA-RULE: secret sample S7.")
    store.add_note(b["id"], "BETA-NOTE: S7 is fragile.", "quirk")
    settings.update({"instructions": "GLOBAL-RULE: fields in tesla."})
    bot = Scripted([REMEMBER_QUIRK, DONE, DONE, REMEMBER_PREF, DONE, DONE, DONE])
    client, headers = client_for(parts, {"scripted": bot})
    with client:
        in_a = client.post("/api/chats", json={"model": "scripted:m1", "project": a["id"]}, headers=headers).json()
        loose = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()

        stream_turn(client, headers, in_a["id"], "the gate leaks above 6 V", "scripted:m1")  # keeps a project note
        first, after_note = bot.systems
        assert first == after_note                     # fixed for the whole turn: the cached prefix holds
        assert "GLOBAL-RULE" in first and "ALPHA-RULE" in first and "Alpha" in first and "BETA" not in first
        assert first.index(SYSTEM) < first.index("GLOBAL-RULE") < first.index("ALPHA-RULE")  # stable parts first
        assert "remember" in bot.tool_names[0]
        notes = client.get("/api/projects/" + a["id"], headers=headers).json()["notes"]
        assert [(n["kind"], n["text"], n["chat"]) for n in notes] == [("quirk", "Gate on D3 leaks above 6 V.", in_a["id"])]
        assert store.project(b["id"])["notes"][0]["text"] == "BETA-NOTE: S7 is fragile."  # B untouched

        events = stream_turn(client, headers, in_a["id"], "and the gate now?", "scripted:m1")
        assert bot.systems[-1] == first                          # notes never enter the system prompt
        recalled = next(e for e in events if e["type"] == "memory")["notes"]
        assert [(n["scope"], n["text"]) for n in recalled] == [("project", "Gate on D3 leaks above 6 V.")]
        assert "<memory>" in bot.prompts[-1][-1] and "Gate on D3 leaks above 6 V." in bot.prompts[-1][-1]
        assert "<memory>" not in bot.prompts[-1][0]              # earlier messages replay unchanged

        # an unfiled chat: global memory only; it may keep a preference, not an experiment fact
        stream_turn(client, headers, loose["id"], "I like fields in tesla", "scripted:m1")
        unfiled = bot.systems[-2]
        assert "GLOBAL-RULE" in unfiled and "ALPHA" not in unfiled and "BETA" not in unfiled
        assert store.global_notes()[0]["text"] == "Give fields in tesla."
        events = stream_turn(client, headers, in_a["id"], "what units for the field?", "scripted:m1")
        recalled = next(e for e in events if e["type"] == "memory")["notes"]
        assert [(n["scope"], n["text"]) for n in recalled] == [("global", "Give fields in tesla.")]  # new to this chat
        assert "BETA-NOTE" not in json.dumps(bot.prompts) and "S7" not in json.dumps(bot.prompts)

        # a human sees and deletes every note
        note_id = notes[0]["id"]
        assert client.delete("/api/projects/%s/notes/%s" % (a["id"], note_id), headers=headers).json() == {"ok": True}
        assert client.delete("/api/projects/%s/notes/%s" % (a["id"], note_id), headers=headers).status_code == 404
        mem = client.get("/api/memory", headers=headers).json()
        assert mem["learn"] is True and mem["instructions"] == "GLOBAL-RULE: fields in tesla."
        assert client.delete("/api/memory/notes/" + mem["notes"][0]["id"], headers=headers).json() == {"ok": True}
        assert client.get("/api/memory", headers=headers).json()["notes"] == []

        # deleting a project deletes its memory; its chats go on without it
        assert client.delete("/api/projects/" + a["id"], headers=headers).json() == {"ok": True}
        assert "ALPHA-RULE" not in (store.root / "_projects.json").read_text(encoding="utf-8")


def test_project_scope_is_refused_outside_a_project(parts):
    _, _, store = parts
    bot = Scripted([REMEMBER_QUIRK, DONE])
    client, headers = client_for(parts, {"scripted": bot})
    with client:
        loose = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        events = stream_turn(client, headers, loose["id"], "the gate leaks above 6 V", "scripted:m1")
        end = next(e for e in events if e["type"] == "tool_end")
        assert end["is_error"] and "not in a project" in end["content"]
        assert store.global_notes() == []


def test_notes_are_short_typed_deduplicated_and_bounded(tmp_path):
    from labharness.web.store import MAX_GLOBAL_NOTES, MAX_NOTES
    store = ChatStore(tmp_path)
    p = store.create_project("P")
    with pytest.raises(ValueError):
        store.add_note(p["id"], "what is the time constant?", "question")   # only decisions, procedures, quirks, results
    with pytest.raises(ValueError):
        store.remember("global", "Gate leaks above 6 V", "quirk")           # experiment facts are not global
    with pytest.raises(ValueError):
        store.add_note(p["id"], "x" * 301, "result")
    with pytest.raises(ValueError):
        store.add_note(p["id"], "   ", "result")
    first = store.add_note(p["id"], "T_c  = 4.2 K  (run r12)", "result")
    assert first["text"] == "T_c = 4.2 K (run r12)"
    assert store.remember("project", "t_c = 4.2 k (run r12)", "result", project_id=p["id"]) == (first, "known")
    assert store.remember("project", "Run r12 gave T_c = 4.2 K", "result", project_id=p["id"])[1] == "known"  # same fact
    other, how = store.remember("project", "T_c = 4.5 K (run r12)", "result", project_id=p["id"])
    assert how == "saved" and other["id"] != first["id"]          # a different number is a different fact
    fixed, how = store.remember("project", "T_c = 4.3 K (run r12, refit)", "result", project_id=p["id"],
                                replaces=first["id"])
    assert how == "updated" and fixed["id"] == first["id"] and fixed["text"].startswith("T_c = 4.3 K")
    for i in range(MAX_NOTES - 2):
        store.add_note(p["id"], "fact number %d about something" % i, "decision")
    with pytest.raises(ValueError):
        store.add_note(p["id"], "one too many", "decision")
    with pytest.raises(KeyError):
        store.add_note("p00000000", "fact", "decision")
    for i in range(MAX_GLOBAL_NOTES):
        store.remember("global", "preference number %d" % i, "preference")
    with pytest.raises(ValueError):
        store.remember("global", "one more preference", "preference")


def test_big_projects_recall_only_what_bears_on_the_question():
    from labharness.web import memory
    notes = [{"id": "n%d" % i, "kind": "result", "text": "Sample S%d resistance is %d ohm at 4 K" % (i, i), "created": i}
             for i in range(20)]
    notes.append({"id": "nq", "kind": "quirk", "text": "The magnet power supply trips above 2 T/min", "created": 30})
    project = {"name": "P", "notes": notes}
    got = memory.recall([], project, "how fast can I ramp the magnet?")
    assert [n["id"] for n in got] == ["nq"]                         # unrelated results stay out
    got = memory.recall([], project, "what was the resistance of sample S7?")
    assert "n7" in [n["id"] for n in got] and len(got) <= memory.RECALL_TOP
    small = {"name": "Q", "notes": notes[:3]}
    assert len(memory.recall([], small, "anything")) == 3              # a small project recalls everything
    assert memory.recall([], small, "anything", already=["n0", "n1", "n2"]) == []
    assert memory.similarity("gate leaks above 6 V", "gate leaks above 8 V") < memory.SAME


def test_the_lab_scripts_folders_and_python_are_a_persons_settings(parts, tmp_path):
    """run_script runs only from the folders a person read in Set up or added in Settings, with the Python they set
    (scriptrun.py)."""
    from labharness.web.app import STATIC
    runtime, settings, _ = parts
    scripts = tmp_path / "lab-scripts"
    scripts.mkdir()
    (scripts / "sweep.py").write_text('import pyvisa\nsmu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")\n')
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        assert runtime.scripts_folders() == [] and runtime.scripts_python() == ""
        r = client.post("/api/settings", json={"lab_scripts_python": "  C:/lab/env/python.exe "}, headers=headers)
        assert r.json()["settings"]["lab_scripts_python"] == "C:/lab/env/python.exe"
        assert runtime.scripts_python() == "C:/lab/env/python.exe"
        assert client.post("/api/instruments/scripts", json={"folder": str(scripts)}, headers=headers).status_code == 200
        assert runtime.scripts_folders() == [str(scripts)] and settings.lab_scripts == [str(scripts)]
        assert client.get("/api/settings", headers=headers).json()["settings"]["lab_scripts"] == [str(scripts)]
        assert client.get("/api/instruments/scripts-folder", headers=headers).json() == {"folders": [str(scripts)]}
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'id="scripts-python"' in html and 'id="scripts-folders"' in html and 'id="scripts-add"' in html
    assert 'body.lab_scripts_python = $("scripts-python").value.trim();' in js


def test_incognito_is_offered_in_every_chat():
    """The Incognito button stays in the top bar: it once vanished after a chat's first message."""
    import re
    from labharness.web.app import STATIC
    assert 'id="toggle-incognito"' in (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    body = js[js.index("function applyIncognito"):js.index("function toggleIncognito")]
    assert not re.search(r"\.hidden\s*=", body)
    assert "if (S.chatId) newChat();" in js[js.index("function toggleIncognito"):]   # from a saved chat: a new one


def portal_function(name):
    """One function of the portal's app.js, by its name."""
    from labharness.web.app import STATIC
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    start = js.index("function %s(" % name)
    return js[start:js.index("\n  }\n", start)]


def test_a_message_is_sent_once_however_often_enter_is_pressed():
    """Bug hunt: two Enters in a new chat made two chats and sent the message twice, as the input emptied only once
    the chat was made. It empties at once now, and S.sending holds off another Enter until the message is on its way."""
    send, start, compare = (portal_function(n) for n in ("sendMessage", "startSend", "sendCompare"))
    assert send.index("if (S.sending) return;") < send.index("await")       # before anything waits
    assert start.index('$("input").value = ""') < start.index("S.sending = true") < start.index("await")
    assert "S.sending = false" in start[start.index("finally"):]            # and always lets go
    assert 'api("/api/chats"' not in send + compare and "startSend(" in send and "startSend(" in compare
    assert "S.sending" in portal_function("updateSend")                     # Send is greyed meanwhile


def test_an_approval_card_closes_only_when_the_request_did():
    """Bug hunt: a card said "Already closed" whatever failed (a 423 while Claude operates, no connection) though the
    request was still open, hid the server's words, and came back in the Lab panel at the next poll."""
    card = portal_function("makeApproval")
    answer = card[card.index("async function answer(approve)"):card.index("yes.addEventListener")]
    assert 'if (e.status === 409) { card.close("Already closed", false); return; }' in answer
    rest = answer[answer.index("e.status === 409"):]
    assert "yes.disabled = no.disabled = false" in rest and "card.choice = undefined" in rest and "toast(" in rest
    assert "err.status = res.status" in portal_function("api")
    assert "if (e.status !== 409) TERM.asked.delete(p.id)" in portal_function("termAnswer")   # the terminal's too


def test_the_lab_button_shows_when_its_panel_is_open():
    """Bug hunt: the Lab button never showed it was pressed, unlike the other panels' buttons."""
    import re
    from labharness.web.app import STATIC
    assert re.search(r'<button id="toggle-lab"[^>]*aria-pressed="false"', (STATIC / "index.html").read_text("utf-8"))
    lab = portal_function("toggleLab")
    assert '$("toggle-lab").setAttribute("aria-pressed", String(S.labOpen))' in lab
    assert '$("toggle-lab").classList.toggle("on", S.labOpen)' in lab


def test_incognito_uses_and_keeps_no_memory(parts):
    _, settings, store = parts
    from labharness.web.agent import INCOGNITO, SYSTEM
    from labharness.web.brevity import RULE
    p = store.create_project("Alpha")
    store.add_note(p["id"], "ALPHA-NOTE: sample is fragile", "quirk")
    store.remember("global", "Give fields in tesla", "preference")
    settings.update({"instructions": "GLOBAL-RULE"})
    bot = Scripted([DONE, REMEMBER_PREF, DONE])
    client, headers = client_for(parts, {"scripted": bot})
    with client:
        assert client.post("/api/chats", json={"model": "scripted:m1", "incognito": True, "project": p["id"]},
                           headers=headers).status_code == 400
        chat = client.post("/api/chats", json={"model": "scripted:m1", "incognito": True}, headers=headers).json()
        assert chat["incognito"] is True and chat["title"] == "Incognito chat"
        events = stream_turn(client, headers, chat["id"], "what is a lock-in amplifier?", "scripted:m1")
        head = "\n\n".join([SYSTEM, RULE.format(words=200), INCOGNITO])   # no instructions, no notes
        assert bot.systems[-1].startswith(head + "\n\nSkills are procedures") and "save_skill" not in bot.systems[-1]
        assert "(Kelvin's)" in bot.systems[-1] and "(yours)" not in bot.systems[-1]   # Kelvin's own skills only
        assert "remember" not in bot.tool_names[-1] and "set_value" in bot.tool_names[-1]
        assert not any(e["type"] == "memory" for e in events) and "<memory>" not in bot.prompts[-1][-1]
        events = stream_turn(client, headers, chat["id"], "remember that I like tesla", "scripted:m1")
        assert next(e for e in events if e["type"] == "tool_end")["is_error"]      # nowhere to keep it
        assert len(store.global_notes()) == 1
        assert chat["id"] not in [c["id"] for c in client.get("/api/chats", headers=headers).json()]
        assert not (store.root / (chat["id"] + ".json")).exists()                  # never written to disk
        assert client.get("/api/chats/" + chat["id"], headers=headers).json()["messages"][0]["text"].startswith("what")
        assert client.patch("/api/chats/" + chat["id"], json={"project": p["id"]}, headers=headers).status_code == 400
        assert client.delete("/api/chats/" + chat["id"], headers=headers).json() == {"ok": True}
        assert client.get("/api/chats/" + chat["id"], headers=headers).status_code == 404


def test_learning_can_be_switched_off(parts):
    _, settings, store = parts
    bot = Scripted([DONE])
    client, headers = client_for(parts, {"scripted": bot})
    with client:
        r = client.post("/api/settings", json={"learn": "off"}, headers=headers).json()
        assert r["settings"]["learn"] is False and settings.learning is False
        chat = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        stream_turn(client, headers, chat["id"], "hello", "scripted:m1")
        assert "remember" not in bot.tool_names[-1] and "remember tool" not in bot.systems[-1]
        assert client.post("/api/settings", json={"learn": "on"}, headers=headers).json()["settings"]["learn"] is True


def test_demo_keeps_preferences_globally_and_facts_per_project(parts):
    _, _, store = parts
    p = store.create_project("Hall bars")
    client, headers = client_for(parts, {"demo": DemoProvider()})
    with client:
        filed = client.post("/api/chats", json={"model": "demo:demo", "project": p["id"]}, headers=headers).json()
        loose = client.post("/api/chats", json={"model": "demo:demo"}, headers=headers).json()
        events = stream_turn(client, headers, filed["id"], "Remember that the gate on D3 leaks above 6 V", "demo:demo")
        assert next(e for e in events if e["type"] == "tool_end")["name"] == "remember"
        assert [(n["kind"], n["text"]) for n in store.project(p["id"])["notes"]] == [("quirk", "The gate on D3 leaks above 6 V")]
        events = stream_turn(client, headers, loose["id"], "remember that the magnet quenches at 9 T", "demo:demo")
        assert not any(e["type"] == "tool_start" for e in events)      # no project to keep it in
        stream_turn(client, headers, loose["id"], "remember that I prefer fields in tesla", "demo:demo")
        assert [(n["kind"], n["text"]) for n in store.global_notes()] == [("preference", "I prefer fields in tesla")]
        assert len(store.project(p["id"])["notes"]) == 1
