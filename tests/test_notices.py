"""A person's changes to the sequence or Home, told to Kelvin's chats (notices.py): to the chat they concern, as a
line in its thread read with its next message; at its next step when it is answering; to every other chat of the
person's as a digest with its next message; never to an incognito chat; and, with the person's switch on, a stop or a
failure continues the chat at once (wakeups.py)."""
import threading
import time

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

from labharness.web import notices  # noqa: E402

from test_lab_memory import portal  # noqa: E402
from test_web import DONE, Scripted, stream_turn  # noqa: E402

HELLO = [{"type": "text", "text": "Hello."}]


def chat(client, headers, **kw):
    return client.post("/api/chats", json=dict({"model": "scripted:m1"}, **kw), headers=headers).json()


def test_a_chat_reads_its_lines_with_its_next_message(tmp_path):
    bot = Scripted([HELLO, DONE, DONE])
    rt, store, client, headers = portal(tmp_path, {"scripted": bot})
    agent = client.app.app.state.agent
    with client:
        c = chat(client, headers)
        stream_turn(client, headers, c["id"], "queue a sweep", "scripted:m1")
        agent.notices.tell("You skipped #3 gate_sweep.", [c["id"]])
        shown = client.get("/api/chats/%s" % c["id"], headers=headers).json()["messages"]
        assert shown[-1]["role"] == "event" and shown[-1]["text"] == "You skipped #3 gate_sweep."
        assert not shown[-1].get("read")
        stream_turn(client, headers, c["id"], "what next?", "scripted:m1")
        seen = bot.prompts[-1][-1]
        assert seen.startswith("<lab_events>\n- You skipped #3 gate_sweep.\n</lab_events>") and seen.endswith("what next?")
        kept = store.load(c["id"])["messages"]
        assert [m for m in kept if m["role"] == "event"][0]["read"] is True
        stream_turn(client, headers, c["id"], "and now?", "scripted:m1")
    assert "lab_events" not in bot.prompts[-1][-1]   # read once
    assert "<lab_events> block" in bot.systems[-1]   # the model is told what the block is


def test_a_change_no_chat_made_reaches_every_chat_once(tmp_path):
    bot = Scripted([DONE, DONE, DONE])
    rt, store, client, headers = portal(tmp_path, {"scripted": bot})
    agent = client.app.app.state.agent
    with client:
        a, b = chat(client, headers), chat(client, headers)
        stream_turn(client, headers, a["id"], "hi", "scripted:m1")
        agent.notices.tell("You stopped the sequence at #2 cool to 2 K.", [])   # the person's own blocks
        agent.notices.tell("You took 'Peak' off Home.", [a["id"]], about="home")
        stream_turn(client, headers, b["id"], "status?", "scripted:m1")
        digest = bot.prompts[-1][-1]
        assert "You stopped the sequence at #2 cool to 2 K." in digest and "You took 'Peak' off Home." in digest
        stream_turn(client, headers, a["id"], "status?", "scripted:m1")
        mine = bot.prompts[-1][-1]
    assert mine.count("You took 'Peak' off Home.") == 1 and "You stopped the sequence" in mine
    assert (tmp_path / "chats" / notices.FILE).exists()   # the person's own log, in their chat folder


class Telling(Scripted):
    """A model during whose step number `at` (0 = the first) the person changes the sequence."""

    def __init__(self, turns, agent_of, chat_of, text, at=0):
        super().__init__(turns)
        self.agent_of, self.chat_of, self.text, self.at = agent_of, chat_of, text, at

    async def stream(self, model, system, history, tools, emit):
        if len(self.seen) == self.at:
            self.agent_of().notices.tell(self.text, [self.chat_of()])
        return await super().stream(model, system, history, tools, emit)


def test_a_chat_answering_reads_it_at_its_next_step(tmp_path):
    look = [{"type": "tool_call", "id": "w1", "name": "lab_status", "input": {}}]
    box = {}
    bot = Telling([look, DONE], lambda: box["agent"], lambda: box["chat"], "You skipped #2 field_gate_map.")
    rt, store, client, headers = portal(tmp_path, {"scripted": bot})
    box["agent"] = client.app.app.state.agent
    with client:
        box["chat"] = chat(client, headers)["id"]
        events = stream_turn(client, headers, box["chat"], "check the lab", "scripted:m1")
    assert any(e["type"] == "note" and e["text"] == "You skipped #2 field_gate_map." for e in events)
    kept = store.load(box["chat"])["messages"]
    line = [m for m in kept if m["role"] == "event"][0]
    assert line["read"] is True
    assert any(m.get("notice") and m["events"] == ["You skipped #2 field_gate_map."] for m in kept)
    assert "You skipped #2 field_gate_map." in bot.prompts[-1][-1]   # its second step read it


def test_a_change_told_during_the_last_step_waits_in_the_thread(tmp_path):
    box = {}
    bot = Telling([DONE, DONE], lambda: box["agent"], lambda: box["chat"], "You paused the sequence after #1.")
    rt, store, client, headers = portal(tmp_path, {"scripted": bot})
    box["agent"] = client.app.app.state.agent
    with client:
        box["chat"] = chat(client, headers)["id"]
        stream_turn(client, headers, box["chat"], "hi", "scripted:m1")
        line = [m for m in store.load(box["chat"])["messages"] if m["role"] == "event"]
        assert len(line) == 1 and not line[0].get("read")   # after the answer: in the thread, unread
        assert box["agent"].notices.pending == {}
        stream_turn(client, headers, box["chat"], "and?", "scripted:m1")
    assert bot.prompts[-1][-1].startswith("<lab_events>\n- You paused the sequence after #1.")


def test_an_incognito_chat_gets_nothing(tmp_path):
    bot = Scripted([DONE, DONE])
    rt, store, client, headers = portal(tmp_path, {"scripted": bot})
    agent = client.app.app.state.agent
    with client:
        hidden = chat(client, headers, incognito=True)
        stream_turn(client, headers, hidden["id"], "hi", "scripted:m1")
        agent.notices.tell("You skipped #1 gate_sweep.", [hidden["id"]])
        stream_turn(client, headers, hidden["id"], "and?", "scripted:m1")
        shown = client.get("/api/chats/%s" % hidden["id"], headers=headers).json()["messages"]
    assert not [m for m in shown if m["role"] == "event"] and "lab_events" not in bot.prompts[-1][-1]


def test_with_the_switch_on_a_stop_continues_the_chat_at_once(tmp_path):
    bot = Scripted([DONE])
    rt, store, client, headers = portal(tmp_path, {"scripted": bot})
    agent = client.app.app.state.agent
    with client:
        c = chat(client, headers)
        agent.notices.tell("You stopped the sequence at #1 gate_sweep.", [c["id"]], urgent=True)
        assert c["id"] not in agent.waker.pending   # off: a line in the chat, no model call
        assert client.post("/api/settings", json={"sequence_react": "on"}, headers=headers).json()[
            "settings"]["sequence_react"] is True
        agent.notices.tell("You stopped the sequence at #1 gate_sweep.", [c["id"]], urgent=True)
        wake = agent.waker.pending[c["id"]]
        assert wake.kind == "event" and agent.waker._check(wake).startswith("the person changed the sequence")
        agent.notices.tell("You stopped it again.", [c["id"]], urgent=True)
        assert agent.waker.pending[c["id"]] is wake   # one wake-up at a time; the line waits in the chat
        agent.waker.pending.clear()
        agent.waker.history[c["id"]] = [time.time()] * 12
        agent.notices.tell("And again.", [c["id"]], urgent=True)
        assert c["id"] not in agent.waker.pending   # within the chat's daily wake-ups


# ---------------------------------------------------------------------- the person's changes to the sequence

def test_the_chat_that_queued_a_block_hears_it_skipped_and_others_read_the_rest(tmp_path):
    from simlab.demo import GATE_SWEEP
    queue = [{"type": "tool_call", "id": "q1", "name": "queue_add", "input": {"item": GATE_SWEEP}}]
    bot = Scripted([queue, DONE, DONE])
    rt, store, client, headers = portal(tmp_path, {"scripted": bot})
    with client:
        a, b = chat(client, headers), chat(client, headers)
        stream_turn(client, headers, a["id"], "queue a gate sweep", "scripted:m1")
        mine = client.get("/api/queue", headers=headers).json()["items"][0]
        assert mine["chat"] == a["id"]   # filed under the chat that queued it
        step = "step: cool to 2 K\nset: { tc.setpoint: 2 }\nwait: 1"
        assert client.post("/api/queue", json={"op": "add", "text": step}, headers=headers).status_code == 200
        assert client.post("/api/queue", json={"op": "skip", "item": mine["id"]}, headers=headers).status_code == 200
        lines = [m["text"] for m in store.load(a["id"])["messages"] if m["role"] == "event"]
        assert lines == ["You skipped #1 gate_sweep."]   # the person's own step goes to no chat at once
        assert not [m for m in store.load(b["id"])["messages"] if m["role"] == "event"]
        stream_turn(client, headers, b["id"], "what's queued?", "scripted:m1")
    digest = bot.prompts[-1][-1]
    assert "You added #2 cool to 2 K." in digest and "You skipped #1 gate_sweep." in digest


def test_a_suggestion_taken_from_home_is_told_to_its_chat(tmp_path):
    rt, store, client, headers = portal(tmp_path, {"scripted": Scripted([DONE])})
    with client:
        a = chat(client, headers)
        block, _ = rt.home_board.add({"kind": "next", "title": "Zoom in on the peak", "actions": [
            {"do": "queue", "text": "step: wait a minute\nwait: 60"}]}, chat=a["id"])
        r = client.post("/api/queue", json={"op": "add", "text": block["actions"][0]["text"], "from_block": block["id"]},
                        headers=headers)
        assert r.status_code == 200
        lines = [m["text"] for m in store.load(a["id"])["messages"] if m["role"] == "event"]
    assert lines == ["You added 'Zoom in on the peak' from Home to the sequence as #1."]


def test_the_changes_in_words():
    from labharness.web.notices import queue_said, stop_said
    before = {"name": "Night", "state": "running", "items": [
        {"id": "a", "name": "gate_sweep", "kind": "plan", "status": "running", "chat": "c1"},
        {"id": "b", "name": "sweep.py", "kind": "script", "status": "waiting", "chat": "c2",
         "params": {"VMAX": 0.4}},
        {"id": "c", "name": "wait 10 min", "kind": "step", "status": "done", "chat": None, "wait": 600}]}
    after = {"name": "Night", "state": "running", "items": [
        dict(before["items"][0]), dict(before["items"][1], params={"VMAX": 0.6}), dict(before["items"][2])]}
    assert queue_said("params", {"item": "b"}, before, after) == (
        "You changed #2 sweep.py: VMAX 0.4 → 0.6.", ["c2"], False)
    assert queue_said("skip", {"item": "#1"}, before, before, "c9") == (
        "You skipped #1 gate_sweep while it ran; the sequence went on.", ["c1", "c9"], False)
    moved = dict(after, items=[after["items"][1], after["items"][0], after["items"][2]])
    assert queue_said("move", {"item": "b", "position": 1}, before, moved)[0] == "You moved #2 sweep.py to #1."
    cleared = dict(after, items=after["items"][:2])
    assert queue_said("clear", {}, before, cleared) == ("You cleared 1 finished block.", [None], False)
    assert queue_said("on_error", {"value": "continue"}, before, before) is None
    assert queue_said("rename", {"name": "Day"}, before, dict(before, name="Day"), "c9")[0] == \
        "You renamed the sequence 'Day'."
    text, chats, urgent = stop_said(before, "c9")
    assert text == "You stopped the sequence at #1 gate_sweep; it paused." and urgent and set(chats) >= {"c9", "c1", "c2"}
    assert stop_said(dict(before, state="idle")) is None
