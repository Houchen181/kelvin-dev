"""Per-instrument locks (fix plan item 12). One lock for the whole lab froze every chat for the length of a run or a
queue: no reading, no card lookup, no plot, no queue edit and no stop from another chat, and the refusal blamed the
cable (F1 t01 t02, F2 l02 l06, F3 #11). Changes to the lab still go one at a time, as the gate's approvals are the
lab's; readings, lookups, the queue and Stop go beside them, and a reading never sends a command to an instrument a
run is using (lablock.py)."""
import asyncio
import threading
import time

import pytest

from labharness import ScriptedApprover, build_virtual_lab
from labharness.clock import ScaledClock
from labharness.lablock import HELD, SLOW, LabLock
from labsim_bench import labsim_bench  # noqa: F401  (the fixture)


def elsewhere(fn):
    """fn's answer on another thread (the lock's owner reads what it likes)."""
    out = []
    t = threading.Thread(target=lambda: out.append(fn()))
    t.start()
    t.join(5)
    return out[0]


def test_a_change_holds_the_instruments_it_uses_and_readings_go_beside_it():
    lock = LabLock()
    whole, narrowed, done = threading.Event(), threading.Event(), threading.Event()
    seen = {}

    def change():   # a run that uses the lock-in
        with lock:
            seen["own"] = lock.holds("gauge")   # its own thread reads as it likes
            whole.set()
            narrowed.wait(5)
            with lock.only(["li"]):
                narrowed.clear()
                whole.set()
                done.wait(5)
    t = threading.Thread(target=change)
    t.start()
    assert whole.wait(5)
    with lock.reading("gauge", 5.0) as why:   # before it says what it uses: it holds them all, said at once
        t0 = time.monotonic()
        assert why == HELD and time.monotonic() - t0 < 0.5
    whole.clear()
    narrowed.set()
    assert whole.wait(5)
    assert lock.holds("li") and not lock.holds("gauge")
    with lock.reading("gauge", 1.0) as why:
        assert why is None                      # read now, beside the run
    with lock.reading("li", 1.0) as why:
        assert why == HELD                      # never a command between the run's own
    done.set()
    t.join(5)
    assert seen["own"] is False and not lock.holds("li") and lock.owner() == (None, None)


def test_a_change_waits_for_the_readings_under_way_and_readings_never_wait_for_a_change():
    lock = LabLock()
    reading, order = threading.Event(), []

    def read():
        with lock.reading("smu", 1.0) as why:
            assert why is None
            reading.set()
            time.sleep(0.4)
            order.append("read")
    t = threading.Thread(target=read)
    t.start()
    assert reading.wait(5)
    with lock:   # it starts once the reading of its instrument has finished
        order.append("change")
        with lock.reading("smu", 0.1) as why:   # its own thread reads
            assert why is None
    t.join(5)
    assert order == ["read", "change"]


def test_taken_again_inside_a_narrowed_change_it_holds_every_instrument_until_released():
    lock = LabLock()
    with lock:
        with lock.only(["li"]):
            assert not elsewhere(lambda: lock.holds("gauge"))
            with lock:   # code that knows nothing of only(): the whole lab again
                assert elsewhere(lambda: lock.holds("gauge"))
            assert not elsewhere(lambda: lock.holds("gauge"))
            with lock.only(["gauge"]):   # within a narrowed change it adds to what that holds
                assert elsewhere(lambda: lock.holds("gauge")) and elsewhere(lambda: lock.holds("li"))
        assert elsewhere(lambda: lock.holds("gauge"))
    assert not elsewhere(lambda: lock.holds("gauge"))
    with pytest.raises(RuntimeError):
        with lock.only(["li"]):   # only the work that has the lock says what it uses
            pass
    with pytest.raises(RuntimeError):
        lock.release()


def test_a_reading_that_hangs_holds_up_a_change_only_so_long(monkeypatch):
    from labharness import lablock
    monkeypatch.setattr(lablock, "SETTLE_S", 0.3)
    lock = LabLock()
    hung, release = threading.Event(), threading.Event()

    def read():   # an instrument that stopped answering
        with lock.reading("smu", 1.0):
            hung.set()
            release.wait(5)
    t = threading.Thread(target=read, daemon=True)
    t.start()
    assert hung.wait(5)
    with lock.reading("smu", 0.2) as why:   # another reading of it gives up, and says why
        assert why == SLOW
    t0 = time.monotonic()
    with lock:
        waited = time.monotonic() - t0
    release.set()
    t.join(5)
    assert 0.25 <= waited < 2


# ---------------------------------------------------------------------- the runtime, on the virtual lab

SWEEP = """plan: long_sweep
setup: { smu.output: on }
sweep: { smu.V: {from: 0, to: 4, step: 0.05} }
settle: 1
measure: [lockin.X, smu.I]
after: { smu.V: 0 }"""

QUEUED = """plan: next_sweep
sweep: { smu.V: {from: 0, to: 1, step: 0.5} }
measure: [smu.I]
after: { smu.V: 0 }"""

LOOK = "plan: look\nrepeat: 3\nmeasure: [lockin.X]\n"


def runtime(tmp_path, answers, clock=None):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    lab = build_virtual_lab(clock=clock, seed=1) if clock is not None else build_virtual_lab(seed=1)
    return LabRuntime(lab, ScriptedApprover(list(answers)), tmp_path / "data", None)


def spy(monkeypatch, lab, names):
    """Every command sent to these instruments, with the thread that sent it."""
    sent = []
    for name in names:
        backend = lab.instruments[name].backend
        for verb in ("query", "write"):
            def record(cmd, *a, _send=getattr(backend, verb), _name=name, **k):
                sent.append((threading.get_ident(), _name, cmd))
                return _send(cmd, *a, **k)
            monkeypatch.setattr(backend, verb, record)
    return sent


def wait_until(check, what, seconds=20.0):
    deadline = time.time() + seconds
    while not check():
        assert time.time() < deadline, what
        time.sleep(0.02)


def test_readings_lookups_and_the_queue_go_on_while_a_run_holds_the_lab(tmp_path, monkeypatch):
    from labharness.agent import runtime as runtime_module
    monkeypatch.setattr(runtime_module, "BUSY_WAIT_S", 0.5)
    rt = runtime(tmp_path, [True], clock=ScaledClock(10))   # one approval: the run's
    lab = rt.lab
    sent = spy(monkeypatch, lab, ["smu", "lockin"])
    out = {}
    run = threading.Thread(target=lambda: out.setdefault("text", rt.run(SWEEP)), daemon=True)
    run.start()
    wait_until(lambda: rt.live and rt.live["status"] == "running" and rt.live["done"] >= 3, "the run never got going")
    me = threading.get_ident()

    v, x, t = rt.get("smu", "V"), rt.get("lockin", "X"), rt.get("tc", "T_sample")
    assert v.startswith("smu.V = ") and "set by the run" in v, v
    assert "(not read now: it is in use: the running plan long_sweep (point " in v
    assert x.startswith("lockin.X = ") and "measured by the run" in x, x
    assert t.startswith("tc.T_sample = ") and "not read now" not in t, t   # an instrument the run doesn't use: read
    status = rt.lab_status()
    assert "busy now: the running plan long_sweep" in status and "using lockin, smu" in status, status
    assert "\nsmu (not read now: it is in use" in status and "\ntc: " in status

    assert "V  drive  V" in rt.describe("smu")                      # lookups never wait for the lab
    assert "collection" in rt.experiment_guide()
    assert rt.find_card("lock-in")
    picture, caption = rt.look(run_id=rt.live["run_id"])            # the running run's plots so far
    assert picture and caption.startswith("Run %s" % rt.live["run_id"])
    health = rt.health(["tc", "smu"])
    assert "tc ok" in health and "smu not checked now: it is in use" in health, health

    added = rt.queue_add(QUEUED)   # checked beside the run: the source's range is not asked of it now
    assert added.startswith("added plan next_sweep as #1"), added
    assert "next_sweep" in rt.queue_show() and rt.queue_pause() == "the queue is not running\n" + rt.queue.text()

    t0 = time.time()
    refused = rt.set("magnet", "B", 0.1)   # a change waits for none: it is said at once, and why
    assert time.time() - t0 < 3.5 and refused.startswith("not done: Kelvin is busy with the running plan long_sweep")
    assert "cable" not in refused

    assert rt.live["status"] == "running"   # all of it happened while the run went on
    mine = [s for s in sent if s[0] == me]
    assert mine == [] and any(s[0] != me for s in sent)   # none of it reached the instruments the run uses

    t0 = time.time()
    said = rt.stop_work("enough for now")
    run.join(10)
    assert time.time() - t0 < 5 and not run.is_alive()
    assert said.startswith("stopped the running plan long_sweep (point ") and "go to safe" in said, said
    assert "aborted after" in out["text"] and rt.live["status"] == "aborted"
    assert lab.read("smu", "V") == 0
    assert "agent: stopped the current ramp or plan (asked: enough for now)" in rt.events(200)


def test_a_queue_item_is_filed_under_the_chat_that_queued_it_never_the_one_working_the_lab(tmp_path):
    from labharness import rundata
    rt = runtime(tmp_path, [])
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    rt.attribution = {"project": "pA", "chat": "A", "folder": str(tmp_path / "a")}   # chat A's call works the lab
    with rt.attributed({"project": None, "chat": "B", "folder": str(tmp_path / "b"), "incognito": True}):
        assert rt.queue_add(LOOK).startswith("added")              # chat B adds to the queue meanwhile
    item = rt.queue.items[0]
    assert item.folder == str(tmp_path / "b") and item.incognito and item.project is None
    rt.queue.state = "running"
    rt._queue_loop({}, "test")   # and the queue's scan is filed as the item says, not under chat A's project
    assert item.status == "done", item.message
    meta = rundata.meta(rt.runs / item.run_id)
    assert meta.get("project") is None and meta.get("incognito") and meta.get("project_name") == "General"
    assert rt.attribution["project"] == "pA"   # chat A's own call is still filed under its own


def test_a_stop_from_anywhere_never_waits_for_the_lab(tmp_path):
    rt = runtime(tmp_path, [])
    assert rt.stop_work() == "nothing to stop: no run, ramp, wait or queue was going"
    held, release = threading.Event(), threading.Event()

    def busy():   # a change holds the lab
        with rt.lock:
            rt.busy = "setting smu.V"
            held.set()
            release.wait(5)
            rt.busy = None
    t = threading.Thread(target=busy, daemon=True)
    t.start()
    assert held.wait(5)
    t0 = time.time()
    said = rt.stop_work("stop it", by="agent (in “fan”)")
    assert time.time() - t0 < 0.5 and said == "stopped setting smu.V: it ends now (a ramp stops where it is)"
    release.set()
    t.join(5)
    assert any("agent (in “fan”)" in line and "stop" in line for line in rt.events(5).splitlines())



def test_a_chat_waiting_for_a_reading_never_asks_the_instrument_a_run_uses(tmp_path, monkeypatch):
    """wake_me's condition is checked in the background: on an instrument a run uses, with the run's own latest
    measurement of it, or not at all until the run ends; on the others, read as any reading is."""
    pytest.importorskip("mcp")
    from labharness.agent.server import build_server
    from labharness.web.agent import ChatAgent
    from labharness.web.store import ChatStore
    rt = runtime(tmp_path, [])
    store = ChatStore(tmp_path / "chats")
    waker = ChatAgent(rt, store, build_server(rt), {}).waker
    chat = store.create("scripted:m1")
    sent = spy(monkeypatch, rt.lab, ["lockin"])
    holding, done = threading.Event(), threading.Event()

    def run():   # a run that uses the lock-in, and has just measured its X
        with rt.lock, rt.lock.only(["lockin"]):
            rt._keep_point({"lockin.X": 2.5e-6}, set())
            holding.set()
            done.wait(10)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert holding.wait(5)
    try:
        waker.add(chat["id"], {"until": "lockin.X > 1e-6", "note": "fit the peak"})
        assert waker._check(waker.pending[chat["id"]]).startswith("lockin.X > 1e-6 is reached (now 2.5")   # the run's
        waker.add(chat["id"], {"until": "lockin.amplitude > 0.5", "note": "x", "within_hours": 1})
        w = waker.pending[chat["id"]]
        assert waker._check(w) is None   # the run doesn't measure it: not asked, and the chat waits
        w.due = 0
        assert "lockin.amplitude isn't read now (it is in use" in waker._check(w)
        waker.add(chat["id"], {"until": "tc.T_sample < 300", "note": "x"})
        assert waker._check(waker.pending[chat["id"]]).startswith("tc.T_sample < 300 is reached")   # read now
    finally:
        done.set()
        t.join(5)
    assert sent == []

# ---------------------------------------------------------------------- the plan's check, on labsim

PUMP_PROBE = """lab: labsim pump-probe
instruments:
  delay:
    card: newport/smc100
    address: "ASRL6::INSTR"
  li:
    card: srs/sr830
    address: "GPIB0::8::INSTR"
  gauge:
    card: pfeiffer/tpg36x
    address: "ASRL3::INSTR"
"""

SCAN = """plan: delay_scan
sweep: { delay.position: {from: 0.0, to: 1.0, step: 0.05} }
settle: 0.5
measure: [li.X, li.Y]"""

NEXT = """plan: next_scan
sweep: { delay.position: {from: 1.0, to: 1.2, step: 0.1} }
measure: [li.X]"""


def call(call_id, name, **args):
    return {"type": "tool_call", "id": call_id, "name": name, "input": args}


def received(bench, name, prefix):
    """How many commands starting with prefix the bench's instrument has received."""
    return sum(1 for line in bench.ctl("log", name, 400)["lines"] if line[1] == "<" and line[2].startswith(prefix))


def test_a_second_chat_reads_the_gauge_and_edits_the_queue_while_a_scan_runs(labsim_bench, tmp_path, monkeypatch):
    """The plan's check, on simulated hardware Kelvin takes for real (labsim's pump-probe bench). While chat A's delay
    scan runs, chat B reads the vacuum gauge (read now: the scan doesn't use it), gets the lock-in's latest values
    without a command reaching the lock-in, queues the next scan under its own chat, hears at once that a change
    waits for the scan, and stops the scan."""
    pytest.importorskip("mcp")
    anyio = pytest.importorskip("anyio")
    from test_web import DONE, Scripted, emitter
    from labharness.agent.runtime import LabRuntime
    from labharness.agent.server import build_server
    from labharness.virtual_lab import build_lab
    from labharness.web import agent as agent_module
    from labharness.web.agent import ChatAgent
    from labharness.web.store import ChatStore
    monkeypatch.setattr(agent_module, "LAB_WAIT_S", 1.0)
    bench = labsim_bench("pumpprobe")
    (tmp_path / "lab.yaml").write_text(PUMP_PROBE, encoding="utf-8")
    lab = build_lab(tmp_path / "lab.yaml", partial=True)
    try:
        assert lab.unavailable == {}
        lab.instrument("delay").act("home")   # not referenced after power-up: it refuses moves until homed
        wait_until(lambda: str(lab.read("delay", "state")).startswith("ready"), "the stage never homed", 10)
        rt = LabRuntime(lab, ScriptedApprover([]), tmp_path / "data", None)   # nothing here asks a person
        store = ChatStore(tmp_path / "chats")
        (tmp_path / "b").mkdir()
        providers = {
            "a": Scripted([[call("a1", "run_plan", plan=SCAN)], DONE]),
            "b": Scripted([[call("b1", "get_value", instrument="gauge", parameter="P1")],
                           [call("b2", "get_value", instrument="li", parameter="X")],
                           [call("b3", "get_value", instrument="li", parameter="sensitivity")],
                           [call("b4", "queue_add", item=NEXT, note="after the scan")],
                           [call("b5", "queue_show")],
                           [call("b6", "set_value", instrument="li", parameter="time_constant", value="300 ms")],
                           DONE,
                           [call("b7", "stop_run", reason="that's enough")], DONE])}

        async def main():
            agent = ChatAgent(rt, store, build_server(rt), providers)
            await agent.start()
            try:
                events, a, b = {}, store.create("a:m1"), store.create("b:m1")
                b["folder"] = str(tmp_path / "b")   # where chat B's runs, and the scans it queues, go
                store.save(b)
                scan = asyncio.ensure_future(agent.run_turn(a["id"], "scan the delay", "a:m1",
                                                            agent.claim(a["id"], "a:m1"), emitter(events, a["id"])))
                await asyncio.to_thread(wait_until, lambda: rt.live and rt.live["status"] == "running"
                                        and rt.live["done"] >= 2, "the scan never got going")
                before = [await asyncio.to_thread(received, bench, "gauge", "PR1"),
                          await asyncio.to_thread(received, bench, "li", "SENS?")]
                await agent.run_turn(b["id"], "how is the vacuum? and queue the next scan", "b:m1",
                                     agent.claim(b["id"], "b:m1"), emitter(events, b["id"]))
                during = [rt.live["status"], await asyncio.to_thread(received, bench, "gauge", "PR1"),
                          await asyncio.to_thread(received, bench, "li", "SENS?")]
                t0 = time.time()
                await agent.run_turn(b["id"], "stop the scan", "b:m1", agent.claim(b["id"], "b:m1"),
                                     emitter(events, b["id"]))
                await asyncio.wait_for(scan, 30)
                return events, a["id"], b["id"], before, during, time.time() - t0
            finally:
                await agent.close()

        events, a_id, b_id, before, during, stopped_in = anyio.run(main)
        said = {e["id"]: e["content"] for e in events[b_id] if e["type"] == "tool_end"}
        assert during[0] == "running", during   # all of B's first answer came while the scan ran
        assert said["b1"].startswith("gauge.P1 = ") and "not read now" not in said["b1"], said["b1"]
        assert during[1] > before[0]            # read now: the gauge answered B
        assert "measured by the run" in said["b2"] and "in use: the running plan delay_scan" in said["b2"], said["b2"]
        assert said["b3"].startswith("li.sensitivity = ") and "not read now" in said["b3"], said["b3"]
        assert during[2] == before[1]           # and no command of B's reached the lock-in in the scan
        assert said["b4"].startswith("added plan next_scan as #1"), said["b4"]
        assert "next_scan" in said["b5"] and rt.queue.items[0].folder == str(tmp_path / "b")   # filed under B
        assert said["b6"].startswith("not run: another conversation (“scan the delay”) is working the lab (the "
                                     "running plan delay_scan"), said["b6"]
        assert said["b7"].startswith("stopped the running plan delay_scan"), said["b7"]
        scan_end = next(e for e in events[a_id] if e["type"] == "tool_end")
        assert "aborted after" in scan_end["content"] and stopped_in < 15, (stopped_in, scan_end["content"])
        assert "agent (in “how is the vacuum? and queue the next scan”): stopped the current ramp or plan" in rt.events(50)
    finally:
        lab.close()
