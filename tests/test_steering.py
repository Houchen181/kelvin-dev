"""What the person writes while Kelvin works reaches the model at its next step, as their own message; what comes
too late goes back to the portal, which sends it as the next message."""
import asyncio

import pytest

pytest.importorskip("mcp")

from labharness import build_virtual_lab  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.gate import ScriptedApprover  # noqa: E402
from labharness.web.agent import ChatAgent, _Turn  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from test_web import Scripted, client_for, parts  # noqa: E402,F401

LOOK = {"type": "tool_call", "id": "c1", "name": "lab_status", "input": {}}


class Steered(Scripted):
    """Writes on the person's behalf while it answers: at the steps named in `at`."""

    def __init__(self, turns, at):
        super().__init__(turns)
        self.at, self.agent, self.chat_id, self.calls = at, None, None, 0

    async def stream(self, model, system, history, tools, emit):
        self.calls += 1
        if self.calls in self.at:
            assert self.agent.steer(self.chat_id, self.at[self.calls])
        return await super().stream(model, system, history, tools, emit)


def day(tmp_path, provider):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    store = ChatStore(tmp_path / "chats")
    agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
    chat = store.create("scripted:m1")
    provider.agent, provider.chat_id = agent, chat["id"]
    events = []

    async def emit(e):
        events.append(e)

    async def go():
        await agent.start()
        try:
            agent.claim(chat["id"], "scripted:m1")
            await agent.run_turn(chat["id"], "check the lab", "scripted:m1", provider, emit)
        finally:
            await agent.close()
    asyncio.run(go())
    return agent, store.load(chat["id"]), events


def test_a_message_written_meanwhile_reaches_the_next_step(tmp_path):
    provider = Steered([[LOOK], [{"type": "text", "text": "And lockin.Y too."}]], at={1: "also record lockin.Y"})
    agent, chat, events = day(tmp_path, provider)
    roles = [m["role"] for m in chat["messages"]]
    assert roles == ["user", "assistant", "tool", "user", "assistant"]
    assert chat["messages"][3] == {"role": "user", "text": "also record lockin.Y", "steer": True}
    assert provider.prompts[1][-1] == "also record lockin.Y"           # the model read it before answering
    assert {"type": "steered", "texts": ["also record lockin.Y"]} in events
    assert chat["id"] not in agent._steer


def test_one_written_too_late_is_left_for_the_portal(tmp_path):
    provider = Steered([[{"type": "text", "text": "All fine."}]], at={1: "thanks"})
    agent, chat, events = day(tmp_path, provider)
    assert [m["role"] for m in chat["messages"]] == ["user", "assistant"]   # the answer had ended: not read
    assert not any(e["type"] == "steered" for e in events) and chat["id"] not in agent._steer


def test_the_services_get_it_in_the_same_turn_as_the_tool_results():
    from labharness.web.providers import anthropic_provider
    history = [{"role": "user", "text": "check"},
               {"role": "assistant", "provider": "x", "model": "m", "raw": None,
                "blocks": [{"type": "tool_call", "id": "c1", "name": "lab_status", "input": {}}]},
               {"role": "tool", "results": [{"id": "c1", "name": "lab_status", "content": "ok", "is_error": False}]},
               {"role": "user", "text": "also Y", "steer": True}]
    a = anthropic_provider.to_messages(history)
    assert [m["role"] for m in a] == ["user", "assistant", "user"]
    assert [b["type"] for b in a[2]["content"]] == ["tool_result", "text"] and a[2]["content"][1]["text"] == "also Y"
    pytest.importorskip("google.genai")
    from labharness.web.providers import gemini_provider
    g = gemini_provider.to_contents(history)
    assert [c.role for c in g] == ["user", "model", "user"]
    assert g[2].parts[0].function_response is not None and g[2].parts[1].text == "also Y"


def test_the_portal_takes_it_only_while_kelvin_works_on_that_chat(parts):
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        chat = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        idle = client.post("/api/chats/%s/steer" % chat["id"], json={"text": "hello"}, headers=headers)
        assert idle.status_code == 409 and "new message" in idle.json()["error"]
        agent = client.app.app.state.agent
        agent.turns[chat["id"]] = _Turn({"chat_id": chat["id"], "model": "scripted:m1"})   # Kelvin answering it
        assert client.post("/api/chats/%s/steer" % chat["id"], json={"text": "also Y"}, headers=headers).json() == {
            "ok": True}
        assert agent._steer[chat["id"]] == ["also Y"]
        del agent.turns[chat["id"]]
