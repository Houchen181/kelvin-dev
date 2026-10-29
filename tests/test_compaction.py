"""Long lab days: near the model's context limit, the earlier turns become a lab handoff the model sees in their place;
the chat keeps everything, and chat_history searches it."""
import asyncio

import pytest

pytest.importorskip("mcp")

from simlab import build_virtual_lab  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.gate import ScriptedApprover  # noqa: E402
from labharness.web import compaction  # noqa: E402
from labharness.web.agent import ChatAgent  # noqa: E402
from labharness.web.providers.base import user_text  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from test_web import Scripted  # noqa: E402

HANDOFF = "Gate sweep run 20261025-101500-gate: Dirac point at 1.2 V. The person wants the field next."


class SmallModel(Scripted):
    """A model that reads only a few thousand tokens at once."""

    def __init__(self, turns):
        super().__init__(turns)
        self.handoffs = []

    def context_window(self, model_id):
        return 3000

    async def stream(self, model, system, history, tools, emit):
        from labharness.web.providers.base import TurnResult
        if system == compaction.HANDOFF:
            self.handoffs.append(history[0]["text"])
            return TurnResult([{"type": "text", "text": HANDOFF}], model=model)
        if not tools:   # Kelvin shortening a long answer: a short one
            return TurnResult([{"type": "text", "text": "Dirac point at 1.2 V."}], model=model)
        return await super().stream(model, system, history, tools, emit)


def test_a_long_day_is_summarised_for_the_model_and_kept_whole(tmp_path):
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    store = ChatStore(tmp_path / "chats")
    long = "The gate sweep showed the Dirac point at 1.2 V. " * 60
    provider = SmallModel([[{"type": "text", "text": long}] for _ in range(3)] + [
        [{"type": "tool_call", "id": "h1", "name": "chat_history", "input": {"query": "Dirac point"}}],
        [{"type": "text", "text": "Found it."}]])
    agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
    chat = store.create("scripted:m1")
    events = []

    async def emit(e):
        events.append(e)

    async def day():
        await agent.start()
        try:
            for text in ("sweep the gate", "and again", "once more", "where was the Dirac point?"):
                agent.claim(chat["id"], "scripted:m1")
                await agent.run_turn(chat["id"], text, "scripted:m1", provider, emit)
        finally:
            await agent.close()
    asyncio.run(day())
    saved = store.load(chat["id"])
    assert saved["compact"]["summary"] == HANDOFF and saved["compact"]["upto"] > 0
    assert len([m for m in saved["messages"] if m["role"] == "user"]) == 4        # the chat keeps every turn
    assert "Person: sweep the gate" in provider.handoffs[0]
    last_seen = provider.prompts[-1]                                                  # what the model read last
    assert last_seen[0].startswith("<lab_handoff>\n" + HANDOFF) and len(last_seen) <= 3
    assert any(e["type"] == "note" and "summarised the earlier part" in e["text"] for e in events) or \
        provider.handoffs
    found = next(r for m in saved["messages"] if m["role"] == "tool" for r in m["results"])
    assert found["name"] == "chat_history" and "Dirac point at 1.2 V" in found["content"]


def test_the_view_and_its_pieces():
    chat = {"messages": [{"role": "user", "text": "a"}, {"role": "assistant", "blocks": [{"type": "text", "text": "bee"}]},
                         {"role": "user", "text": "c"}, {"role": "user", "text": "d"}]}
    assert compaction.view(chat) is chat["messages"]
    chat["compact"] = {"upto": 2, "summary": "S"}
    seen = compaction.view(chat)
    assert len(seen) == 2 and user_text(seen[0]) == "<lab_handoff>\nS\n</lab_handoff>\n\nc"
    assert compaction.cut_point(chat["messages"]) == 2 and compaction.cut_point(chat["messages"][:2]) == 0
    assert compaction.search(chat, "zzz").startswith("nothing in this conversation")
    assert "#1 Kelvin: …bee…" in compaction.search(chat, "bee")
