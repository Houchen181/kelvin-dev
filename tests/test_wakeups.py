"""Waiting without freezing every chat: wake_me ends the turn, and Kelvin continues the chat later, when the time
comes, a reading holds a condition, or the run ends. Long waits inside a turn are refused in the portal."""
import asyncio

import pytest

pytest.importorskip("mcp")

from labharness import build_virtual_lab  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.gate import ScriptedApprover  # noqa: E402
from labharness.web import wakeups  # noqa: E402
from labharness.web.agent import ChatAgent  # noqa: E402
from labharness.web.review import last_request  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from test_web import Scripted  # noqa: E402


def call(name, **args):
    return [{"type": "tool_call", "id": name + "1", "name": name, "input": args}]


def say(text):
    return [{"type": "text", "text": text}]


def setup(tmp_path, turns):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    notices = []
    rt.notifier = lambda kind, title, text="": notices.append((kind, title, text))
    store = ChatStore(tmp_path / "chats")
    provider = Scripted(turns)
    agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
    return rt, store, provider, agent, notices


async def quiet(event):
    pass


def test_a_chat_is_continued_when_the_time_comes(tmp_path):
    rt, store, provider, agent, notices = setup(tmp_path, [
        call("wake_me", after_minutes=30, note="read the sample temperature and start the sweep"),
        say("The fridge needs about 30 minutes. I'll continue then."),
        say("Continuing: the sample is cold, starting the sweep.")])
    chat = store.create("scripted:m1")

    async def day():
        await agent.start()
        try:
            agent.claim(chat["id"], "scripted:m1")
            await agent.run_turn(chat["id"], "cool down, then sweep", "scripted:m1", provider, quiet)
            assert agent.active is None                       # the turn ended: other chats can run
            [pending] = agent.waker.view()
            assert pending["chat_id"] == chat["id"] and pending["text"] == "30 min have passed"
            await agent.waker.tick()                         # not yet
            assert agent.waker.view() and len(provider.turns) == 1
            agent.waker.pending[chat["id"]].due = 0          # the time comes
            await agent.waker.tick()
        finally:
            await agent.close()
    asyncio.run(day())
    saved = store.load(chat["id"])
    woke = [m for m in saved["messages"] if m.get("wake")]
    assert len(woke) == 1 and woke[0]["text"].startswith("<wake>30 min have passed")
    assert woke[0]["wake"] == "30 min have passed. Next: read the sample temperature and start the sweep"
    assert "read the sample temperature" in woke[0]["text"]
    assert saved["messages"][-1]["blocks"][0]["text"].startswith("Continuing")
    assert notices and notices[-1][1] == "Kelvin continued “cool down, then sweep”"
    assert agent.waker.view() == [] and agent.waker.latest()["chat_id"] == chat["id"]
    assert agent.waker.latest()["done"] and "root" not in agent.waker.latest()
    assert last_request(saved["messages"]) == "cool down, then sweep"   # the person's request, not the wake
    assert "wake_me" in provider.tool_names[0] and "<wake>" in provider.systems[0]


def test_conditions_runs_and_limits(tmp_path):
    rt, store, provider, agent, notices = setup(tmp_path, [])
    waker = agent.waker
    chat = store.create("scripted:m1")
    text, bad = waker.add(chat["id"], {"until": "tc.T_sample < 300", "hold_minutes": 2, "note": "sweep"})
    assert not bad and "tc.T_sample < 300 for 2 min" in text
    w = waker.pending[chat["id"]]
    assert waker._check(w) is None and w.since is not None          # holds, but not for 2 minutes yet
    w.since -= 121
    assert waker._check(w).startswith("tc.T_sample < 300 for 2 min is reached (now")
    waker.add(chat["id"], {"until": "tc.T_sample > 1000", "note": "sweep", "within_hours": 1})
    w = waker.pending[chat["id"]]
    assert waker._check(w) is None
    w.due = 0
    assert "is not reached" in waker._check(w)                        # gave up waiting: continues anyway
    assert waker.add(chat["id"], {"until": "tc.nothing < 3", "note": "x"})[1]
    assert waker.add(chat["id"], {"run_ends": True, "note": "x"}) == ("nothing is running now: start the run or "
                                                                       "queue first", True)
    assert waker.add(chat["id"], {"note": "x"})[1] and waker.add(chat["id"], {"after_minutes": 5})[1]
    waker.history[chat["id"]] = [__import__("time").time()] * wakeups.WAKES_PER_DAY
    assert "12 times today" in waker.add(chat["id"], {"after_minutes": 5, "note": "x"})[0]
    assert waker.cancel(chat["id"]) and not waker.cancel(chat["id"])


def test_only_the_owners_chats_are_continued(tmp_path):
    rt, store, provider, agent, notices = setup(tmp_path, [])
    chat = store.create("scripted:m1")
    agent.waker.add(chat["id"], {"after_minutes": 1, "note": "x"})
    agent.waker.pending[chat["id"]].due = 0
    store.use(tmp_path / "someone-else")                                  # another person signs in
    assert agent.waker.view() == []
    asyncio.run(agent.waker.tick())
    assert chat["id"] in agent.waker.pending and not notices              # kept for its owner
    store.use(tmp_path / "chats")
    assert [w["chat_id"] for w in agent.waker.view()] == [chat["id"]]


def test_long_waits_inside_a_turn_are_refused(tmp_path):
    rt, store, provider, agent, notices = setup(tmp_path, [call("wait", seconds=1800), say("I'll use wake_me.")])
    chat = store.create("scripted:m1")

    async def turn():
        await agent.start()
        try:
            agent.claim(chat["id"], "scripted:m1")
            await agent.run_turn(chat["id"], "wait half an hour", "scripted:m1", provider, quiet)
        finally:
            await agent.close()
    asyncio.run(turn())
    [result] = next(m for m in store.load(chat["id"])["messages"] if m["role"] == "tool")["results"]
    assert result["is_error"] and "call wake_me" in result["content"]


def test_incognito_chats_are_never_continued(tmp_path):
    rt, store, provider, agent, notices = setup(tmp_path, [])
    chat = store.create("scripted:m1", incognito=True)
    assert "wake_me" not in [t["name"] for t in agent.context(chat)["tools"]]


def test_people_read_a_wake_up_in_words():
    pytest.importorskip("starlette")
    from labharness.web.app import shown
    m = shown({"role": "user", "text": "<wake>x</wake>", "wake": "30 min have passed. Next: sweep", "raw": [1]})
    assert m["text"] == "Kelvin continued this chat: 30 min have passed. Next: sweep" and "raw" not in m
    assert shown({"role": "user", "text": "hi"}) == {"role": "user", "text": "hi"}


def test_a_wake_up_is_kept_over_a_restart(tmp_path):
    rt, store, provider, agent, notices = setup(tmp_path, [])
    chat = store.create("scripted:m1")
    agent.waker.add(chat["id"], {"until": "tc.T_sample < 300", "hold_minutes": 5, "note": "sweep"})
    assert (store.root / "_wakes.json").exists()
    again = wakeups.Waker(agent)                                          # Kelvin started again
    asyncio.run(again.tick())
    [w] = again.view()
    assert w["chat_id"] == chat["id"] and w["text"] == "tc.T_sample < 300 for 5 min" and 43000 < w["left_s"] <= 43200
    assert again.pending[chat["id"]].restored and again.pending[chat["id"]].condition.key == "tc.T_sample"
    assert again.cancel(chat["id"]) and not (store.root / "_wakes.json").exists()
