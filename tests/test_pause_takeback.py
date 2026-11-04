"""Pause as an armed action (2026-11-03, the brief's 1.3 and K8): the Sequence's Pause reads Pausing… while the queue
finishes the item that is running, and a second press takes the pause back, the queue going on as it was approved;
nothing starts that was not running. The press carries want, the state the button showed, so a screen a poll out of
date never flips it: the server refuses (409) and says why, in a few words. Only a person's own pause can be taken
back, never Kelvin's or one a failure, a guard trip, a declined check, a block's Pause after or a stop made. The pause
and its taking back are both logged: the lab's record, and a line to the chats. A fake runner drives the queue's own
loop: its items run nothing, each running until the test ends it."""
import json
import re
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from labharness import eln  # noqa: E402
from labharness.web.notices import queue_said  # noqa: E402

from test_scan_library import portal, virtual  # noqa: E402

STATIC = Path(eln.__file__).parent / "web" / "static"


class FakeRunner:
    """The queue's own loop (_queue_loop) over items that run nothing: each is running until the test ends it, done,
    failed, or failed with a guard trip. A failure is told (queue_heard) before the loop goes on, and the loop waits
    there while `hold` is clear: the queue is still pausing then, its reason the failure's."""

    def __init__(self, rt):
        self.rt, self.go, self.how = rt, {}, {}
        self.told = threading.Event()
        self.hold = threading.Event()
        self.hold.set()
        rt._queue_item = self.item       # how the loop runs an item
        rt.queue_heard = self.heard

    def _gate(self, item):
        return self.go.setdefault(item.id, threading.Event())

    def item(self, item, position, grants, by, since=None):
        self._gate(item).wait(20)
        ok, message, trip = self.how.get(item.id, (True, "", False))
        if trip:
            self.rt.gate.trip_count += 1
        self.rt.queue.mark(item, "done" if ok else "failed", message)
        return ok, message

    def heard(self, item, message):
        self.told.set()
        self.hold.wait(20)

    def start(self, approved=None):
        """approved: the items the start's approval covered ({id: text}); one changed since is checked again."""
        rt = self.rt
        with rt.queue.lock:
            rt.queue.state, rt.queue.reason = "running", ""
        rt._queue_thread = threading.Thread(target=rt._queue_loop, args=({}, "human", approved), daemon=True,
                                            name="kelvin-queue")
        rt._queue_thread.start()
        return self

    def end(self, item, ok=True, message="", trip=False):
        self.how[item.id] = (ok, message, trip)
        self._gate(item).set()

    def finish(self):
        self.hold.set()
        for item in self.rt.queue.items:
            self._gate(item).set()
        self.rt._queue_thread.join(20)
        assert not self.rt._queue_thread.is_alive(), "the queue did not finish"


def until(check, seconds=10.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        got = check()
        if got:
            return got
        time.sleep(0.02)
    raise AssertionError("not within %s s" % seconds)


def steps(rt, *names):
    return [rt.queue.add("step: %s\nwait: 1" % n) for n in names]


def logged(rt):
    return [(e["type"], e.get("by")) for e in rt.gate.log.events() if str(e.get("type")).startswith("queue_pause")]


# ------------------------------------------------------------------ taken back before it takes effect

def test_a_pause_taken_back_lets_the_running_queue_go_on(tmp_path):
    rt = virtual(tmp_path, [])
    one, two = steps(rt, "one", "two")
    runner = FakeRunner(rt).start()
    try:
        until(lambda: one.status == "running")
        assert rt.queue_pause("human", want="running") == "the queue pauses after the item that is running"
        assert (rt.queue.state, rt.queue.reason) == ("pausing", "paused by human")
        assert rt.queue_pause("human", want="pausing") == "the pause is taken back: the queue goes on"
        assert (rt.queue.state, rt.queue.reason) == ("running", "")
        assert one.status == "running" and two.status == "waiting"   # nothing started: the same item goes on
        runner.end(one)
        until(lambda: two.status == "running")                      # then the next, as if never paused
        runner.end(two)
        runner.finish()
        assert (rt.queue.state, rt.queue.reason) == ("idle", "finished")
    finally:
        runner.finish()
    assert logged(rt) == [("queue_pause", "human"), ("queue_pause_taken_back", "human")]
    assert {"queue_pause", "queue_pause_taken_back"} <= eln.DAY_TYPES   # the day's lab record keeps both


def test_a_pause_left_alone_pauses_after_the_item_and_then_cannot_be_taken_back(tmp_path):
    rt = virtual(tmp_path, [])
    one, two = steps(rt, "one", "two")
    runner = FakeRunner(rt).start()
    try:
        until(lambda: one.status == "running")
        rt.queue_pause("human", want="running")
        runner.end(one)
        until(lambda: rt.queue.state == "paused")
        rt._queue_thread.join(10)
        assert two.status == "waiting" and rt.queue.reason == "paused by human"
        assert rt.queue_pause("human", want="pausing") == "not changed: already paused"
        assert rt.queue.state == "paused" and two.status == "waiting"   # never starts what wasn't running
        assert not rt._queue_thread.is_alive()
    finally:
        runner.finish()
    assert logged(rt) == [("queue_pause", "human")]


def test_kelvins_pause_is_not_taken_back_from_the_page(tmp_path):
    rt = virtual(tmp_path, [])
    one, two = steps(rt, "one", "two")
    runner = FakeRunner(rt).start()
    try:
        until(lambda: one.status == "running")
        assert rt.queue_pause() == "the queue pauses after the item that is running"   # Kelvin's tool, without want
        assert rt.queue.reason == "paused by agent"
        assert rt.queue_pause("human", want="pausing") == "not changed: Kelvin made this pause"
        assert rt.queue_pause("human", want="running") == "not changed: already pausing"
        assert (rt.queue.state, rt.queue.reason) == ("pausing", "paused by agent")
        runner.hold.clear()
        runner.end(one, ok=False, message="no lock")                  # a failure meanwhile still makes it its own
        until(runner.told.is_set)
        assert rt.queue.reason == "one: no lock"
        runner.hold.set()
        until(lambda: rt.queue.state == "paused")
        assert two.status == "waiting"
    finally:
        runner.finish()
    assert logged(rt) == [("queue_pause", "agent")]


# ------------------------------------------------------------------ a stale screen

def test_a_stale_want_changes_nothing_and_says_why(tmp_path):
    rt = virtual(tmp_path, [])
    one, two = steps(rt, "one", "two")
    assert rt.queue_pause("human", want="running") == "not changed: not running"
    assert rt.queue_pause("human", want="pausing") == "not changed: not running"
    assert rt.queue_pause("human").startswith("the queue is not running\n")   # without want, as ever
    with pytest.raises(ValueError, match="running or pausing"):
        rt.queue_pause("human", want="paused")
    runner = FakeRunner(rt).start()
    try:
        until(lambda: one.status == "running")
        assert rt.queue_pause("human", want="pausing") == "not changed: already running"
        assert rt.queue.state == "running"
        rt.queue_pause("human", want="running")
        changed = rt.queue.changed
        assert rt.queue_pause("human", want="running") == "not changed: already pausing"   # not taken back
        assert (rt.queue.state, rt.queue.reason, rt.queue.changed) == ("pausing", "paused by human", changed)
    finally:
        runner.finish()
    assert logged(rt) == [("queue_pause", "human")]


def test_a_pause_whose_queue_is_gone_is_not_taken_back(tmp_path):
    rt = virtual(tmp_path, [])
    steps(rt, "one")
    rt.queue.state, rt.queue.reason = "pausing", "paused by human"   # no loop behind it: nothing to go on
    assert rt.queue_pause("human", want="pausing") == "not changed: not running"
    assert rt.queue.state == "pausing" and logged(rt) == []


def test_each_refusal_is_a_few_words_cause_first(tmp_path):
    """The page shows a refusal on one line in the state's place (seq.js say), beside the bar's buttons."""
    rt = virtual(tmp_path, [])
    steps(rt, "one", "two")
    going = threading.Event()
    rt._queue_thread = threading.Thread(target=going.wait, args=(20,), daemon=True)   # a loop still on its item
    rt._queue_thread.start()
    said = []
    try:
        for reason, cause in [("paused by agent", "Kelvin"), ("stopped by human", "Stop"),
                              ("paused after one, as asked", "Pause after"),
                              ("declined: two (added or changed after the queue was approved)", "a change"),
                              ("one: smu.V out of range", "a failure"),
                              ("one: leak; a guard tripped, so the queue pauses whatever on a failure says", "a failure")]:
            rt.queue.state, rt.queue.reason = "pausing", reason
            said.append(rt.queue_pause("human", want="pausing"))
            assert said[-1] == "not changed: %s made this pause" % cause
        for state, want, words in [("running", "pausing", "already running"), ("pausing", "running", "already pausing"),
                                   ("paused", "pausing", "already paused"), ("paused", "running", "already paused"),
                                   ("idle", "pausing", "not running"), ("idle", "running", "not running")]:
            rt.queue.state, rt.queue.reason = state, "paused by human" if state == "pausing" else ""
            said.append(rt.queue_pause("human", want=want))
            assert said[-1] == "not changed: " + words
    finally:
        going.set()
    # 27 characters are about 160 px, which the bar leaves them in its narrowest (a Sequence 341 px wide, Pause hidden),
    # the name giving way first (app.css fit P)
    assert max(len(s) - len("not changed: ") for s in said) <= 27, said
    assert logged(rt) == []


# ------------------------------------------------------------------ pauses that are not taken back

def test_a_failure_makes_the_pause_its_own_and_it_is_not_taken_back(tmp_path):
    rt = virtual(tmp_path, [])
    one, two = steps(rt, "one", "two")
    runner = FakeRunner(rt).start()
    try:
        until(lambda: one.status == "running")
        runner.hold.clear()
        runner.end(one, ok=False, message="smu.V out of range")
        until(runner.told.is_set)                                     # the loop is past the failure, still pausing
        assert (rt.queue.state, rt.queue.reason) == ("pausing", "one: smu.V out of range")
        assert rt.queue_pause("human", want="pausing") == "not changed: a failure made this pause"
        assert rt.queue.state == "pausing"
        runner.hold.set()
        until(lambda: rt.queue.state == "paused")
        assert two.status == "waiting"
    finally:
        runner.finish()
    assert logged(rt) == []


def test_a_failure_while_a_pause_waits_takes_it_over(tmp_path):
    rt = virtual(tmp_path, [])
    one, two = steps(rt, "one", "two")
    runner = FakeRunner(rt).start()
    try:
        until(lambda: one.status == "running")
        rt.queue_pause("human", want="running")
        runner.hold.clear()
        runner.end(one, ok=False, message="lockin stopped answering")
        until(runner.told.is_set)
        assert rt.queue.reason == "one: lockin stopped answering"     # the pause is the failure's now: Retry, not Resume
        assert rt.queue_pause("human", want="pausing") == "not changed: a failure made this pause"
        runner.hold.set()
        until(lambda: rt.queue.state == "paused")
        assert two.status == "waiting"
    finally:
        runner.finish()


def test_a_guard_trip_pauses_whatever_on_a_failure_says_and_is_not_taken_back(tmp_path):
    rt = virtual(tmp_path, [])
    rt.queue_set_on_error("continue")
    one, two = steps(rt, "one", "two")
    runner = FakeRunner(rt).start()
    try:
        until(lambda: one.status == "running")
        rt.queue_pause("human", want="running")
        runner.hold.clear()
        runner.end(one, ok=False, message="gate leakage over 2 nA", trip=True)
        until(runner.told.is_set)
        assert rt.queue.state == "pausing" and "a guard tripped" in rt.queue.reason
        assert rt.queue_pause("human", want="pausing") == "not changed: a failure made this pause"
        runner.hold.set()
        until(lambda: rt.queue.state == "paused")
        assert two.status == "waiting"
    finally:
        runner.finish()


def test_a_stop_is_not_taken_back(tmp_path):
    rt = virtual(tmp_path, [])
    one, two = steps(rt, "one", "two")
    runner = FakeRunner(rt).start()
    try:
        until(lambda: one.status == "running")
        rt.queue_pause("human", want="running")
        rt.stop("human", report=False)                                # Stop: its reason replaces the pause's
        assert rt.queue.reason == "stopped by human"
        assert rt.queue_pause("human", want="pausing") == "not changed: Stop made this pause"
        runner.end(one)
        until(lambda: rt.queue.state == "paused")
        assert two.status == "waiting"
    finally:
        runner.finish()


def test_with_a_failure_going_on_a_taken_back_pause_goes_on_past_it(tmp_path):
    rt = virtual(tmp_path, [])
    rt.queue_set_on_error("continue")
    one, two = steps(rt, "one", "two")
    runner = FakeRunner(rt).start()
    try:
        until(lambda: one.status == "running")
        rt.queue_pause("human", want="running")
        runner.hold.clear()
        runner.end(one, ok=False, message="no lock")                  # on a failure: go on, so the pause stays the person's
        until(runner.told.is_set)
        assert rt.queue.reason == "paused by human"
        assert rt.queue_pause("human", want="pausing") == "the pause is taken back: the queue goes on"
        runner.hold.set()
        until(lambda: two.status == "running")
    finally:
        runner.finish()
    assert rt.queue.state == "idle"


@pytest.mark.parametrize("ok", [True, False])
def test_a_blocks_pause_after_takes_over_a_pause_that_waits(tmp_path, ok):
    """A block with Pause after ends while the person's pause waits (done, or failed with On a failure: Go on): the
    pause is the block's, so a press as the loop goes on (here while it tells the chats of the failure) can't take it
    back and run the queue past the block's Pause after."""
    rt = virtual(tmp_path, [])
    rt.queue_set_on_error("continue")
    one, two = steps(rt, "one", "two")
    runner = FakeRunner(rt).start()
    try:
        until(lambda: one.status == "running")
        rt.queue.set_then_pause(one.id, True)                         # Pause after, switched on as it runs
        rt.queue_pause("human", want="running")
        runner.hold.clear()
        runner.end(one, ok=ok, message="" if ok else "no lock")
        if not ok:
            until(runner.told.is_set)                                 # the gap a press can find
            assert (rt.queue.state, rt.queue.reason) == ("pausing", "paused after one, as asked")
            assert rt.queue_pause("human", want="pausing") == "not changed: Pause after made this pause"
            runner.hold.set()
        until(lambda: rt.queue.state == "paused")
        rt._queue_thread.join(10)
        assert two.status == "waiting" and rt.queue.reason == "paused after one, as asked"   # Resume, never past it
    finally:
        runner.finish()
    assert logged(rt) == [("queue_pause", "human")]


def test_a_declined_check_takes_over_a_pause_that_waits(tmp_path):
    """A block changed after the queue's approval is checked again before it runs; declined while the person's pause
    waits, the pause is the decline's (_queue_ask_again), and a press then can't take it back."""
    rt = virtual(tmp_path, [])
    one, two = steps(rt, "one", "two")
    asked, taken = [], []

    def approve(items, by, said, again=False):   # the check of the changed block: the person presses Pause meanwhile
        asked.append([i.id for i in items])
        rt.queue_pause("human", want="running")
        return {"ok": False, "said": "declined: %s" % said}
    rt._queue_approve = approve
    ask_again = rt._queue_ask_again

    def asked_again(items, by, grants):   # the press as the decline lands, before the loop goes round
        got = ask_again(items, by, grants)
        taken.append(rt.queue_pause("human", want="pausing"))
        return got
    rt._queue_ask_again = asked_again
    runner = FakeRunner(rt).start(approved={one.id: one.text, two.id: "step: two\nwait: 9"})   # two changed since
    try:
        until(lambda: one.status == "running")
        runner.end(one)
        until(lambda: rt.queue.state == "paused")
        rt._queue_thread.join(10)
    finally:
        runner.finish()
    assert asked == [[two.id]] and two.status == "waiting"
    assert rt.queue.reason == "declined: the queue paused before them (added or changed after the queue was approved)"
    assert taken == ["not changed: a change made this pause"]
    assert logged(rt) == [("queue_pause", "human")]


# ------------------------------------------------------------------ the portal

def test_over_http_the_press_carries_want_and_a_stale_one_is_409(tmp_path):
    rt = virtual(tmp_path, [])
    client, _ = portal(rt, tmp_path)
    one, two = steps(rt, "one", "two")
    runner = FakeRunner(rt).start()

    def pause(**body):
        return client.post("/api/queue", json=dict(op="pause", **body))
    try:
        until(lambda: one.status == "running")
        r = pause(want="running")
        assert r.status_code == 200 and r.json()["state"] == "pausing"
        assert r.json()["message"] == "the queue pauses after the item that is running"
        r = pause(want="running")                                     # a second screen that still showed Pause
        assert r.status_code == 409 and r.json()["error"] == "not changed: already pausing"
        assert rt.queue.state == "pausing"
        r = pause(want="pausing")
        assert r.status_code == 200 and r.json()["state"] == "running" and r.json()["reason"] == ""
        assert r.json()["message"] == "the pause is taken back: the queue goes on"
        r = pause(want="pausing")
        assert r.status_code == 409 and r.json()["error"] == "not changed: already running"
        assert pause(want="sideways").status_code == 400
        assert rt.queue.state == "running" and one.status == "running"
    finally:
        runner.finish()
    said = [json.loads(line)["text"] for line in (tmp_path / "chats" / "_events.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert said == ["You paused the sequence after one.", "You took back the pause after one; the sequence goes on."]
    assert logged(rt) == [("queue_pause", "human"), ("queue_pause_taken_back", "human")]


def test_the_take_back_in_words():
    before = {"state": "pausing", "items": [{"id": "a", "name": "gate_sweep", "status": "running", "chat": "c1"},
                                            {"id": "b", "name": "wait", "status": "waiting", "chat": "c2"}]}
    assert queue_said("pause", {"want": "pausing"}, before, dict(before, state="running"), "c9") == (
        "You took back the pause after gate_sweep; the sequence goes on.", ["c9", "c1", "c2"], False)
    assert queue_said("pause", {"want": "running"}, dict(before, state="running"), before)[0] == \
        "You paused the sequence after gate_sweep."


# ------------------------------------------------------------------ the page

def _fn(js, name):
    start = js.index("async function %s(" % name)
    return js[start:js.index("\n  }\n", start)]


def test_the_sequences_pause_is_an_armed_action():
    js = (STATIC / "seq.js").read_text(encoding="utf-8")
    press = _fn(js, "pauseSeq")
    assert 'op: "pause", want' in press and "dataset.want" in press   # the state the button showed
    assert "say(why" in press and "e.message" in press and "loadQueue()" in press   # refused: why, then the queue
    assert "holdSent" in press                                       # one press at a time
    assert 'guard(op, { op: "pause" })' not in js
    # a double-click's second click, a press within 0.4 s of the button changing and a held key's repeats are ignored
    assert 'pause.addEventListener("click", (e) => { if (e.detail < 2 && Date.now() - pauseAt >= 400) guard(pauseSeq); })' in js
    assert 'pause.addEventListener("keydown", (e) => { if (e.repeat) e.preventDefault(); })' in js
    assert "if (came || pause.dataset.want !== want) pauseAt = Date.now();" in js
    assert 'own ? "Pausing…" : "Pause"' in js and 'classList.toggle("armed", own)' in js
    assert 'pause.hidden = st !== "running" && !own' in js             # Kelvin's, a failure's or a stop's: no button
    # the state chip says Running while the person's own pause waits (the button says Pausing…), else Pausing
    assert 'if (q.state === "running" || ownPause(q)) {' in js
    assert 'if (q.state === "pausing") return ["Pausing", "warn", q.reason || ""];' in js
    # the keyboard on Pause as it went: ⋯ holds it (never Stop), then Resume or Retry once offered
    assert "if (held && pause.hidden) pauseFocus = true;" in js
    assert "start.focus({ preventScroll: true })" in js and "more.focus({ preventScroll: true })" in js
    assert "stop.focus(" not in js
    # the page's rule is the server's: the person's own pause ("paused by human"), taken back only while pausing
    assert 'q.state === "pausing" && /^paused by human/.test(q.reason' in js
    from labharness.agent import runtime
    src = Path(runtime.__file__).read_text(encoding="utf-8")
    assert 'self._pause_asked() and self.queue.reason.startswith("paused by human")' in src
    assert 'q.state == "pausing" and q.reason.startswith("paused by ")' in src   # any asked pause, for the takeovers
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    part = css[css.index("/* ===== fit P"):css.index("/* ===== end fit P ===== */")]
    armed = re.search(r"\.seq-hold\.armed \{([^}]*)\}", part).group(1)
    assert "var(--warn-bg)" in armed and "var(--warn)" in armed and "#" not in armed   # colours through tokens
    # a refusal's words are read whole: the name gives way first while the bar says something (not while renamed)
    assert ".seq-head-main.saying .seq-name:not(:focus) { flex-shrink: 1000; }" in part
    # a narrow pane shows Pause by its icon, but Pausing… by its word too (not by colour alone)
    narrow = part[part.index("@container seqview (max-width: 460px)"):]
    assert ".seq-bar .btn.seq-hold.armed > span { display: inline; }" in narrow
