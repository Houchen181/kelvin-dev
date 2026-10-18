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

from labharness import build_virtual_lab, demo_envelope  # noqa: E402
from labharness.clock import ScaledClock  # noqa: E402
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.web.agent import ChatAgent  # noqa: E402
from labharness.web.app import create_app  # noqa: E402
from labharness.web.providers.base import ModelInfo, Provider, TurnResult  # noqa: E402
from labharness.web.providers.demo import GATE_SWEEP, DemoProvider  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402


class Scripted(Provider):
    """Replays a fixed list of model turns."""
    name, label = "scripted", "Scripted"

    def __init__(self, turns, delay=0.0):
        self.turns = list(turns)
        self.delay = delay
        self.seen = []

    def available(self):
        return True

    async def models(self):
        return [ModelInfo("m1", "Model One", self.name)]

    async def stream(self, model, system, history, tools, emit):
        self.seen.append([m["role"] for m in history])
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
