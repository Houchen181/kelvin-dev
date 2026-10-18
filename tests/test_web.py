"""The chat portal: providers' message conversion, the agent loop, and the web API (no network, no API keys)."""
import asyncio
import json
import threading

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")
pytest.importorskip("httpx")
anyio = pytest.importorskip("anyio")

from starlette.testclient import TestClient  # noqa: E402

from labharness import build_virtual_lab, demo_envelope  # noqa: E402
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.web.agent import ChatAgent  # noqa: E402
from labharness.web.app import create_app  # noqa: E402
from labharness.web.providers.base import ModelInfo, Provider, TurnResult  # noqa: E402
from labharness.web.providers.demo import DemoProvider  # noqa: E402
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
        assert len(data["rows"]) == 21
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
