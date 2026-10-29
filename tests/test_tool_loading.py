"""Kelvin's rarer tools are loaded when a chat needs them (find_tools, or by calling one by name), so each step carries
fewer; lookups that touch no instrument run side by side."""
import asyncio

import pytest

pytest.importorskip("mcp")

from simlab import build_virtual_lab  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.gate import ScriptedApprover  # noqa: E402
from labharness.web.agent import DEFERRED_NAMES, ChatAgent  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from test_web import Scripted  # noqa: E402


def day(tmp_path, provider, texts=("set up the new lock-in",), patch=None):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    store = ChatStore(tmp_path / "chats")
    agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
    if patch:
        patch(agent)
    chat = store.create("scripted:m1")
    events = []

    async def emit(e):
        events.append(e)

    async def go():
        await agent.start()
        try:
            for text in texts:
                agent.claim(chat["id"], "scripted:m1")
                await agent.run_turn(chat["id"], text, "scripted:m1", provider, emit)
        finally:
            await agent.close()
    asyncio.run(go())
    return store.load(chat["id"]), events


def call(name, **args):
    return [{"type": "tool_call", "id": name + "1", "name": name, "input": args}]


def test_rarer_tools_load_when_a_chat_needs_them(tmp_path):
    provider = Scripted([call("find_tools", query="add_instrument, try_card"), [{"type": "text", "text": "Ready."}],
                         [{"type": "text", "text": "Still ready."}]])
    chat, events = day(tmp_path, provider, ("set up the new lock-in", "and now?"))
    first, second, third = provider.tool_names
    assert "add_instrument" not in first and {"find_tools", "run_plan", "get_value"} <= set(first)
    assert len(first) < len(second) and {"add_instrument", "try_card"} <= set(second)
    assert chat["tools"] == ["add_instrument", "try_card"] and set(third) >= {"add_instrument", "try_card"}
    assert "# More tools" in provider.systems[0] and "add_instrument" in provider.systems[0]
    assert "add_instrument" not in provider.systems[2].split("# More tools")[1].split("#")[0]   # listed until loaded
    found = next(e for e in events if e["type"] == "tool_end")
    assert found["content"].startswith("loaded, from the next step on:\n- add_instrument: ")


def test_by_words_or_by_calling_one_by_name(tmp_path):
    provider = Scripted([call("find_tools", query="watch a reading"), call("watches"),
                         [{"type": "text", "text": "No watches."}]])
    chat, events = day(tmp_path, provider)
    assert {"watch", "unwatch", "watches"} <= set(chat["tools"])
    provider = Scripted([call("queue_pause"), [{"type": "text", "text": "Paused."}]])
    chat, events = day(tmp_path / "b", provider)
    assert chat["tools"] == ["queue_pause"]                                     # called by name: it ran, and stays
    result = next(e for e in events if e["type"] == "tool_end")
    assert not result["content"].startswith("unknown tool")


def test_every_rarer_tool_is_a_tool_kelvin_has(tmp_path):
    provider = Scripted([[{"type": "text", "text": "Hi."}]])
    day(tmp_path, provider)
    assert DEFERRED_NAMES <= set(provider.tool_names[0]) | DEFERRED_NAMES   # (names only)
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "x")

    async def names():
        from mcp import Client
        async with Client(build_server(rt)) as c:
            r = await c.list_tools()
            return {t.name for t in getattr(r, "tools", r)}
    assert DEFERRED_NAMES <= asyncio.run(names())                               # none misspelt or gone


def test_lookups_in_one_step_run_side_by_side(tmp_path):
    started = []

    def patch(agent):
        both = asyncio.Event()

        async def search(args):   # waits for the other: run one after the other, it would time out
            started.append(args["query"])
            if len(started) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 3)
            return "found " + args["query"], False
        agent._web_search = search
    two = [{"type": "tool_call", "id": "s1", "name": "web_search", "input": {"query": "SR830 manual"}},
           {"type": "tool_call", "id": "s2", "name": "web_search", "input": {"query": "SR860 manual"}}]
    provider = Scripted([two, [{"type": "text", "text": "Both found."}]])
    chat, events = day(tmp_path, provider, patch=patch)
    results = next(m for m in chat["messages"] if m["role"] == "tool")["results"]
    assert [r["content"] for r in results] == ["found SR830 manual", "found SR860 manual"]   # in the calls' order
    assert sorted(started) == ["SR830 manual", "SR860 manual"]
