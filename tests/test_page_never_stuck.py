"""The page always shows a way forward, and a chat's Stop is that chat's (review of 0.3.45, wave 2: 1.6-1.8, 3.9,
7.1-7.3).

- The terminal's Esc before it had a conversation sent a chat_id of null, which the server took as the Stop for
  everything: another chat's run and the queue stopped (1.7).
- A chat's Stop left its cards for tools that never hold the lab open, and a Stop in a chat whose call only waited
  for the lab aborted whoever had it (1.8).
- Saving a chat blocked the server's loop (3.9).
- No Stop for an answer the window didn't start; a refused message vanished (7.1).
- A card answered on a phone stayed live in the chat until its call ended; request ids began again with each start
  (7.2).
- The Sequence's Start was held open until its approval was answered; polls piled up with no timeout (7.3).
- After a Stop the page sent what the person had added meanwhile as a new message (1.6).
"""
import asyncio
import json
import threading
import time

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")
pytest.importorskip("httpx")

from simlab import build_virtual_lab  # noqa: E402
from labharness.clock import RealClock  # noqa: E402
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.gate import ApprovalRequest  # noqa: E402
from labharness.web.agent import ChatAgent  # noqa: E402
from labharness.web.app import STATIC  # noqa: E402
from labharness.web.providers.base import ModelInfo, Provider, TurnResult  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from test_stop import portal, queue_running, stopped  # noqa: E402

JS = (STATIC / "app.js").read_text(encoding="utf-8")


def js_function(name):
    start = JS.index("function %s(" % name)
    return JS[start:JS.index("\n  }\n", start)]


def wait_until(check, seconds=10.0):
    t0 = time.time()
    while not check() and time.time() - t0 < seconds:
        time.sleep(0.05)
    return check()


def call(call_id, tool, **args):
    return [{"type": "tool_call", "id": call_id, "name": tool, "input": args}]


class Turns(Provider):
    """Replays model turns; a turn may be a coroutine function, awaited when the model would answer."""
    name, label = "scripted", "Scripted"

    def __init__(self, turns):
        self.turns = list(turns)

    def available(self):
        return True

    async def models(self):
        return [ModelInfo("m1", "Model One", self.name)]

    async def stream(self, model, system, history, tools, emit):
        blocks = self.turns.pop(0) if self.turns else [{"type": "text", "text": "ok"}]
        blocks = await blocks() if callable(blocks) else blocks
        for b in blocks:
            if b["type"] == "text":
                await emit({"type": "text", "delta": b["text"]})
        calls = [b for b in blocks if b["type"] == "tool_call"]
        return TurnResult(blocks, stop_reason="tool_calls" if calls else "end", model=model)


def real(tmp_path, timeout=20):
    """The virtual lab in real time with no envelope: raising smu.V asks a person."""
    return LabRuntime(build_virtual_lab(clock=RealClock(), seed=1), WebApprover(timeout=timeout), tmp_path / "data")


# ------------------------------------------------------------------ 1.7 a chat_id that names none stops nothing

def test_a_stop_naming_no_chat_stops_nothing_and_the_lab_stop_leaves_other_chats_answering(tmp_path):
    """The terminal's Esc before its first message sent {chat_id: null}: the server took it as the Stop for
    everything, and another chat's 2-hour run and the queue stopped. Only {} is that Stop now; {lab: true} stops the
    lab's work (the terminal's /stop with no conversation) and not another chat's answer."""
    rt = real(tmp_path)
    release = threading.Event()

    async def thinking():   # a model still answering
        await asyncio.to_thread(release.wait, 15)
        return [{"type": "text", "text": "done thinking"}]
    client, headers, store = portal(tmp_path, rt, {"scripted": Turns([thinking])})
    with client:
        chat = store.create("scripted:m1")
        said = []
        turn = threading.Thread(target=lambda: said.extend(client.post(
            "/api/chats/%s/turn" % chat["id"], json={"text": "think about it", "model": "scripted:m1"},
            headers=headers).text.splitlines()), daemon=True)
        turn.start()
        answering = lambda: chat["id"] in [t["chat_id"] for t in client.get("/api/state", headers=headers).json()[
            "answering"]]
        assert wait_until(answering)
        queue_running(rt)

        r = client.post("/api/stop", json={"chat_id": None}, headers=headers)   # the terminal's Esc, as it was
        assert r.status_code == 200 and r.json()["ok"] is False and "Nothing stopped" in r.json()["message"]
        assert client.post("/api/stop", json={"chat_id": ""}, headers=headers).json()["ok"] is False
        time.sleep(0.3)
        assert answering() and rt.queue.state == "running"   # nothing stopped

        r = client.post("/api/stop", json={"lab": True}, headers=headers)   # the terminal's /stop, no conversation
        assert r.json()["ok"] is True
        assert stopped(rt)                                                  # the lab's work: the queue stopped
        assert answering()                                                  # and the chat goes on answering

        assert client.post("/api/stop", json={}, headers=headers).json()["ok"] is True   # the Stop for everything
        assert wait_until(lambda: not answering())
        release.set()
        turn.join(10)
    ends = [json.loads(line) for line in said if '"turn_end"' in line]
    assert ends == [{"type": "turn_end", "reason": "stopped"}]


def test_the_terminal_never_sends_a_chat_id_that_names_no_chat():
    key = js_function("termKey")
    assert 'api("/api/stop"' not in key and "termInterrupt();" in key
    interrupt = js_function("termInterrupt")
    assert interrupt.index('TERM.busy === "chat" && TERM.chatId') < interrupt.index('api("/api/stop"')
    assert "can't be \" +\n      \"cut off here. /stop stops the lab's work" in interrupt   # an instrument's command: said
    slash = js_function("termSlash")
    assert "body: TERM.chatId ? { chat_id: TERM.chatId, lab: true } : { lab: true }" in slash
    assert 'TERM.busy = "call"' in js_function("termCall") and 'TERM.busy = "chat"' in js_function("termChat")
    # the message box's typed STOP with no chat open is the Stop for everything ({}), never a null chat
    assert "body: S.chatId ? { chat_id: S.chatId, lab: true } : {}" in js_function("hardStop")
    senders = set()   # every Stop the page sends, by the function that sends it: each guards its chat_id
    for at in [i for i in range(len(JS)) if JS.startswith('api("/api/stop"', i)]:
        senders.add(JS[JS.rindex("function ", 0, at) + len("function "):].split("(")[0])
    assert senders == {"stopAnswer", "hardStop", "termInterrupt", "termSlash"}
    assert "if (!chatId || S.stopping.has(chatId)) return;" in js_function("stopAnswer")


# ------------------------------------------------------------------ 1.8 a chat's Stop is that chat's

def test_a_chats_stop_declines_its_card_for_a_tool_that_never_holds_the_lab(tmp_path, monkeypatch):
    """Keeping a skill asks a person outside any lab call: a per-chat Stop left that card open (and an open card
    holds the lab for everyone: busy_for) until it declined itself."""
    rt = real(tmp_path)
    monkeypatch.setattr(rt.gate, "auto_simple", lambda *a, **k: False)   # a person is asked (as after a web page)
    store = ChatStore(tmp_path / "chats")
    provider = Turns([call("s1", "save_skill", name="fridge-cooldown", description="cool the fridge",
                           steps="1. close valve V3", scope="global")])
    events = []

    async def emit(e):
        events.append(e)

    async def day():
        agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
        await agent.start()
        try:
            chat = store.create("scripted:m1")
            agent.claim(chat["id"], "scripted:m1")
            turn = asyncio.ensure_future(agent.run_turn(chat["id"], "keep the procedure", "scripted:m1", provider,
                                                        emit))
            for _ in range(200):
                if rt.approver.pending_list():
                    break
                await asyncio.sleep(0.05)
            asked = rt.approver.pending_list()
            assert asked, "the card never came"
            assert rt.busy_for(1.0)                    # an open card: the lab is busy for every chat
            assert agent.stop(chat["id"])
            assert rt.approver.pending_list() == []    # declined at once
            await asyncio.wait_for(turn, 10)
            return asked[0]["id"]
        finally:
            await agent.close()
    card = asyncio.run(day())
    assert rt.approver.outcome(card) == "declined" and not rt.busy_for(1.0)
    closed = [e for e in events if e["type"] == "approval_closed"]
    assert closed == [{"type": "approval_closed", "id": card, "answer": "declined"}]   # the card closes in the chat
    assert events[-1] == {"type": "turn_end", "reason": "stopped"}


def test_a_stop_in_a_chat_that_only_waits_for_the_lab_leaves_whoever_has_it_alone(tmp_path):
    """Another client's scan has the lab; this chat's drive change waits for it. The chat's Stop aborted the scan (it
    was the chat that 'held' the portal's lab turn); now the scan goes on, and the chat's change never runs."""
    rt = real(tmp_path)
    store = ChatStore(tmp_path / "chats")
    provider = Turns([call("v1", "set_value", instrument="smu", parameter="V", value=2)])
    hold, out = threading.Event(), {}
    scan = threading.Thread(target=lambda: out.update(scan=rt._do("a scan from the Set up page",
                                                                  lambda: "done" if hold.wait(15) else "",
                                                                  stoppable=True)), daemon=True)
    events = []

    async def emit(e):
        events.append(e)

    async def day():
        agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
        await agent.start()
        try:
            chat = store.create("scripted:m1")
            agent.claim(chat["id"], "scripted:m1")
            turn = asyncio.ensure_future(agent.run_turn(chat["id"], "raise the gate", "scripted:m1", provider, emit))
            for _ in range(100):   # its call is in, waiting for the lab
                if any(e["type"] == "tool_start" for e in events):
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(2.5)
            stops = rt.gate.stop_generation
            assert agent.stop(chat["id"])
            await asyncio.wait_for(turn, 10)
            assert rt.gate.stop_generation == stops   # the scan was not stopped
            return stops
        finally:
            await agent.close()
    scan.start()
    assert wait_until(lambda: rt.lock.owner()[0] is not None)
    asyncio.run(day())
    hold.set()
    scan.join(10)
    assert out["scan"] == "done"                  # it ran to its end
    time.sleep(0.5)                               # the chat's call had the lab after it: not run
    assert rt.lab.read("smu", "V") == 0
    assert [e["type"] for e in events][-1] == "turn_end"


def test_a_stop_in_a_chat_whose_call_has_the_lab_stops_that_work(tmp_path):
    """The chat's own wait has the lab: its Stop ends it at once (through the call's own cancel), and the record
    says a person's Stop in a chat ended it, not an MCP client."""
    rt = real(tmp_path)
    store = ChatStore(tmp_path / "chats")
    provider = Turns([call("w1", "wait", seconds=60)])
    events = []

    async def emit(e):
        events.append(e)

    async def day():
        agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
        await agent.start()
        try:
            chat = store.create("scripted:m1")
            agent.claim(chat["id"], "scripted:m1")
            turn = asyncio.ensure_future(agent.run_turn(chat["id"], "wait a minute", "scripted:m1", provider, emit))
            for _ in range(100):
                if rt.busy == "waiting":
                    break
                await asyncio.sleep(0.05)
            assert rt.busy == "waiting"
            t0 = time.time()
            agent.stop(chat["id"])
            await asyncio.wait_for(turn, 10)
            for _ in range(100):   # the wait, in its own thread, ends at the abort
                if rt.busy is None:
                    break
                await asyncio.sleep(0.05)
            return time.time() - t0
        finally:
            await agent.close()
    took = asyncio.run(day())
    assert took < 3 and rt.busy is None
    record = (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8")
    assert '"call_cancelled"' in record and "a person's Stop in a chat" in record
    assert "a person's Stop in a chat ended wait; its work stopped" in rt.events(20)


# ------------------------------------------------------------------ 7.2 cards close as soon as they are answered

def test_a_card_answered_on_a_phone_closes_in_the_chat_while_its_call_goes_on(tmp_path):
    """repro_answered_elsewhere: approved on a phone, the change then took 6 s, and the card stayed live in the chat
    (the title said "Approval needed") until the call ended: an hour, for a run."""
    from simlab import demo_envelope
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, WebApprover(timeout=30), tmp_path / "data", demo_envelope(lab))
    own_set = rt.set

    def slow_set(*a, **k):   # the approved change takes a while (a ramp, a run)
        out = own_set(*a, **k)
        time.sleep(2.5)
        return out
    rt.set = slow_set
    client, headers, store = portal(tmp_path, rt, {"scripted": Turns([call("c1", "set_value", instrument="smu",
                                                                           parameter="V", value=3)])})
    marks = {}

    def phone():
        entry = rt.approver.wait_for_pending(timeout=10)
        time.sleep(0.3)
        rt.approver.answer(entry["id"], True)
        marks["answered"] = time.monotonic()
    with client:
        chat = store.create("scripted:m1")
        threading.Thread(target=phone, daemon=True).start()
        with client.stream("POST", "/api/chats/%s/turn" % chat["id"], json={"text": "Raise the gate to 3 V",
                                                                            "model": "scripted:m1"},
                           headers=headers) as r:
            for line in r.iter_lines():
                if line.strip():
                    e = json.loads(line)
                    marks.setdefault(e["type"], time.monotonic())
                    if e["type"] == "approval_closed":
                        marks["closed"] = e
    assert marks["approval_closed"] - marks["answered"] < 1.0           # closed at once, not when the call ended
    assert marks["tool_end"] - marks["approval_closed"] > 1.5
    assert marks["closed"].get("answer") == "approved"


def test_request_ids_are_this_processs_own_and_the_history_says_which_was_which():
    """r1, r2… began again at each start: a card a phone kept across a restart could answer another request."""
    first, second = WebApprover(timeout=5), WebApprover(timeout=5)
    ids = []
    for approver in (first, second):
        threading.Thread(target=lambda a=approver: a.approve(ApprovalRequest("Approve drive change on smu?", [])),
                         daemon=True).start()
        entry = approver.wait_for_pending(5)
        ids.append(entry["id"])
        assert approver.open_ids() == {entry["id"]} and approver.outcome(entry["id"]) is None
        assert approver.decline_ids([entry["id"]], "stop button") == 1
        assert approver.outcome(entry["id"]) == "declined"
        assert wait_until(lambda a=approver: a.history_list())
        assert approver.history_list()[0]["id"] == entry["id"] and approver.history_list()[0]["by"] == "stop button"
    assert ids[0] != ids[1] and all(i.startswith("r1-") for i in ids)


def test_the_page_closes_a_card_its_poll_no_longer_lists_with_its_answer():
    read = js_function("readState")
    loop = read[read.index("for (const [id, card] of [...S.approvals])"):]
    assert "open.has(id) || card.decided || card.at > sent" in loop   # one drawn after the poll was sent: next time
    assert 'was.answer === "approved" ? "Approved" : "Declined"' in loop
    assert "at: Date.now()" in js_function("makeApproval")
    handle = js_function("handle")
    closed = handle[handle.index('case "approval_closed"'):]
    assert 'ev.answer ? ev.answer === "approved"' in closed[:closed.index("break;")]


# ------------------------------------------------------------------ 3.9 a chat's save never holds the server up

def test_a_slow_save_of_a_chat_never_holds_the_servers_loop(tmp_path, monkeypatch):
    """ChatStore.save tries again for up to 2 s while Windows says the file is in use (a virus scanner, a backup):
    on the loop, every Stop and poll waited meanwhile."""
    rt = real(tmp_path)
    store = ChatStore(tmp_path / "chats")
    own_save = store.save

    def slow_save(chat):
        time.sleep(0.4)   # the file in use for a moment
        own_save(chat)
    monkeypatch.setattr(store, "save", slow_save)
    provider = Turns([[{"type": "text", "text": "hello"}]])
    lags = []

    async def day():
        agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
        await agent.start()
        try:
            chat = await asyncio.to_thread(store.create, "scripted:m1")
            agent.claim(chat["id"], "scripted:m1")
            done = asyncio.Event()

            async def ticker():   # the loop's other work: how late each tick comes
                while not done.is_set():
                    t0 = time.monotonic()
                    await asyncio.sleep(0.02)
                    lags.append(time.monotonic() - t0 - 0.02)

            async def answer():
                try:
                    await agent.run_turn(chat["id"], "hi", "scripted:m1", provider, lambda e: asyncio.sleep(0))
                finally:
                    done.set()
            await asyncio.gather(ticker(), answer())
        finally:
            await agent.close()
    asyncio.run(day())
    assert lags and max(lags) < 0.25, max(lags)


def test_no_chat_is_written_on_the_servers_loop():
    import ast
    import inspect
    from labharness.web import agent as agent_module
    tree = ast.parse(inspect.getsource(agent_module))
    on_loop = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef):
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("save", "load") and isinstance(node.func.value, ast.Attribute)
                        and node.func.value.attr == "store"):
                    on_loop.append("%s:%d" % (fn.name, node.lineno))
    assert on_loop == []


# ------------------------------------------------------------------ 7.3 no request is held open for a person

RAMP = "plan: ramp\nsweep: { smu.V: {from: 0, to: 1, step: 0.5} }\nmeasure: [smu.I]\n"


@pytest.fixture
def soon(monkeypatch):
    """A person's action is answered with 202 {later} after 0.5 s here (5 s in the portal)."""
    from labharness.web import app as app_module
    monkeypatch.setattr(app_module, "SOON_S", 0.5)


def test_the_sequences_start_is_answered_at_once_while_its_approval_card_waits(tmp_path, soon):
    """Start was answered only once a person answered the queue's card (a phone may answer hours later): the request
    held one of the browser's six connections to Kelvin, and Stop and Approve queued behind such requests."""
    rt = real(tmp_path)
    client, headers, _ = portal(tmp_path, rt)
    with client:
        assert client.post("/api/queue", json={"op": "add", "text": RAMP}, headers=headers).status_code == 200
        t0 = time.time()
        r = client.post("/api/queue", json={"op": "start"}, headers=headers)
        assert r.status_code == 202 and time.time() - t0 < 4
        job = r.json()["later"]
        assert "approval card" in r.json()["message"]
        assert client.get("/api/later/%s" % job, headers=headers).json() == {"done": False}
        assert client.get("/api/state", headers=headers).status_code == 200   # the portal answers meanwhile
        card = rt.approver.wait_for_pending(5)
        assert card and rt.approver.answer(card["id"], True)
        assert wait_until(lambda: client.get("/api/later/%s" % job, headers=headers).json()["done"])
        done = client.get("/api/later/%s" % job, headers=headers).json()
        assert done["status"] == 200 and done["body"]["message"].startswith("queue started")
        assert client.get("/api/later/nope", headers=headers).status_code == 404
        rt.stop("test")
        assert wait_until(lambda: rt.queue.state != "running")


def test_a_start_refused_after_its_card_says_why_when_fetched(tmp_path, soon):
    rt = real(tmp_path)
    client, headers, _ = portal(tmp_path, rt)
    with client:
        client.post("/api/queue", json={"op": "add", "text": RAMP}, headers=headers)
        job = client.post("/api/queue", json={"op": "start"}, headers=headers).json()["later"]
        card = rt.approver.wait_for_pending(5)
        rt.approver.answer(card["id"], False)
        assert wait_until(lambda: client.get("/api/later/%s" % job, headers=headers).json()["done"])
        done = client.get("/api/later/%s" % job, headers=headers).json()
        assert done["status"] == 400 and "did not start" in done["body"]["error"]
    assert rt.queue.state != "running"


def test_a_terminal_command_waiting_for_its_approval_is_answered_at_once(tmp_path, soon):
    """The terminal's /set that drives the sample harder asks y/n through the state: its request waited meanwhile."""
    rt = real(tmp_path)
    client, headers, _ = portal(tmp_path, rt)
    with client:
        r = client.post("/api/terminal", json={"instrument": "smu", "op": "set", "arg": "V", "value": 0.5},
                        headers=headers)
        assert r.status_code == 202
        card = rt.approver.wait_for_pending(5)
        assert card and rt.approver.answer(card["id"], True)
        assert wait_until(lambda: client.get("/api/later/%s" % r.json()["later"], headers=headers).json()["done"])
        done = client.get("/api/later/%s" % r.json()["later"], headers=headers).json()
        assert done["status"] == 200 and done["body"]["ok"] and "approved" in done["body"]["message"]
    assert rt.lab.read("smu", "V") == 0.5


def test_the_page_waits_for_a_later_answer_without_holding_a_request():
    api = js_function("api")
    assert "res.status === 202" in api and "answerLater(first, opts)" in api
    later = js_function("answerLater")
    assert 'api("/api/later/" + encodeURIComponent(first.later), { timeout: 15000 })' in later
    assert "await sleep(1500)" in later and "err.status = r.status" in later
    assert "timer = setTimeout(() => { gaveUp = true; stop.abort(); }, opts.timeout)" in api


def test_one_poll_of_the_state_at_a_time_and_none_waited_for_for_ever():
    poll = js_function("pollState")
    assert "if (!POLL.now)" in poll and "POLL.next = POLL.now.then(" in poll
    assert '{ timeout: POLL_MS }' in js_function("readState") and "const POLL_MS = 15000;" in JS
    assert "{ timeout: POLL_MS }" in js_function("pollLive")


def test_the_analysis_stream_ends_when_its_pane_closes_and_starts_again_when_it_opens():
    toggle = js_function("toggleAnalysis")
    closing = toggle[toggle.index("if (!ANA.open) {"):toggle.index("return;", toggle.index("if (!ANA.open) {"))]
    assert "ANA.ctl.abort();" in closing and "cut: true" in closing
    shown = js_function("anViewShown")   # what the Analysis view starts as it shows (the pane opened, or chosen)
    assert "anViewShown();" in toggle and "if (!ANA.run || ANA.follow) startAnalysis();" in shown
    assert "else if (ANA.job && ANA.job.cut && ANA.job.run === ANA.run) analyzeRun(ANA.run);" in shown


def test_stop_says_stopping_at_once_and_a_stop_that_failed_is_said_and_tried_again():
    stop = js_function("stopAnswer")
    assert stop.index("S.stopping.set(chatId, pressed);") < stop.index("refreshComposer();") < stop.index("await api(")
    assert "timeout: 8000" in stop and "trying again…" in stop and "tries >= 3" in stop and ".catch(() => {})" not in stop
    send = js_function("updateSend")
    assert 'stopping ? "Stopping…"' in send
    assert '"Stopping Kelvin…"' in js_function("refreshComposer")
    assert ".send.stop.stopping:disabled" in (STATIC / "app.css").read_text(encoding="utf-8")
    assert "stopAnswer(S.chatId)" in JS[JS.index('$("composer").addEventListener("submit"'):][:300]
    assert '$("wake-stop").addEventListener("click", () => stopAnswer(S.chatId));' in JS


# ------------------------------------------------------------------ 7.1 a Stop for an answer this window didn't start

def test_the_stop_shows_for_an_answer_this_window_did_not_start():
    assert "const answeringHere = () => !!S.chatId && (S.live.has(S.chatId) || S.answering.has(S.chatId));" in JS
    assert "const busy = answeringHere(), stop = busy && !typed" in js_function("updateSend")
    assert "if (answeringHere()) return steerMessage(text);" in js_function("sendMessage")
    steer = js_function("steerMessage")
    assert "if (!text || !chatId) return;" in steer and "S.steered.set(chatId" in steer   # no live answer here: kept
    assert "e.status === 409" in steer and "putBack(chatId, text" in steer
    wake = js_function("wakeState")
    assert "for (const id of ended) endedElsewhere(id);" in wake and "refreshComposer();" in wake
    ended = js_function("endedElsewhere")
    assert "steersIn(chat, chatId, s.text) > s.seen" in ended and "putBack(chatId, unread.map" in ended
    assert "keepSteered(chat);" in js_function("renderChat")


def test_a_refused_message_comes_back_into_the_message_box():
    send = js_function("sendMessage")
    assert "refused = msg;" in send and 'putBack(chatId, text, "Not sent: " + refused, files);' in send
    assert "bubble.remove();" in send and "live.box.remove();" in send
    assert 'putBack(chatId, text, "Not sent: " + refused, files);' in js_function("sendCompare")
    back = js_function("putBack")
    assert "ATT.items = files.slice()" in back and "openChat(chatId).then(" in back


def test_a_stream_that_fails_is_let_go_of():
    assert "if (reader) reader.cancel().catch(() => {});" in js_function("sendMessage")
    events = js_function("readEvents")
    assert "reader.cancel().catch(() => {});" in events[events.index("catch (e)"):]


def test_a_turn_refused_while_the_chat_answers_elsewhere_is_409_and_its_steer_is_taken(tmp_path):
    """After a reload the page sent a new message to a chat still answering: refused, and the message vanished. It
    goes to Kelvin's next step instead (/steer), which takes it while the chat answers."""
    rt = real(tmp_path)
    release = threading.Event()

    async def thinking():
        await asyncio.to_thread(release.wait, 15)
        return [{"type": "text", "text": "done"}]
    client, headers, store = portal(tmp_path, rt, {"scripted": Turns([thinking])})
    with client:
        chat = store.create("scripted:m1")
        threading.Thread(target=lambda: client.post("/api/chats/%s/turn" % chat["id"], json={
            "text": "think", "model": "scripted:m1"}, headers=headers), daemon=True).start()
        assert wait_until(lambda: client.get("/api/state", headers=headers).json()["answering"])
        again = client.post("/api/chats/%s/turn" % chat["id"], json={"text": "and 5 T", "model": "scripted:m1"},
                            headers=headers)
        assert again.status_code == 409
        assert client.post("/api/chats/%s/steer" % chat["id"], json={"text": "and 5 T"},
                           headers=headers).status_code == 200
        client.post("/api/stop", json={"chat_id": chat["id"]}, headers=headers)
        release.set()


# ------------------------------------------------------------------ 1.6 after a Stop, nothing is sent by itself

def test_what_was_added_before_a_stop_comes_back_into_the_box_and_is_never_sent():
    send = js_function("sendMessage")
    assert 'if (ev.type === "turn_end") live.ended = ev.reason || "end";' in send
    finish = js_function("finishLive")
    assert 'live.ended === "stopped" ? "you stopped Kelvin"' in finish
    assert '!live.ended ? "the connection to Kelvin\'s answer was lost"' in finish
    assert finish.index("if (why) putBack(") < finish.index("else if (here) sendMessage(text);")
    assert "S.stopping.delete(chatId);" in finish
