"""Nothing a person or a chat does waits out other work (review of 0.3.45, 3.3-3.8).

- 3.3: the Lab panel's switch, Unlock, Mark reviewed and reconnecting waited for the lab with no limit: during a run they
  hung for its whole length, and the switch then acted as the run ended, though its own check said no.
- 3.4: Save and connect switched the lab under a running queue (its next item was "stopped before it started", blamed
  on a Stop from that morning), and Scan swept the GPIB board the queue used.
- 3.5: instruments still connecting at start-up stayed "connecting" for the whole of a run started meanwhile.
- 3.6: one chat's reading of a long acquisition made every change in any chat wait about 71 s.
- 3.7: every change ended with a read of every instrument while it held the lab (54 reads for one set).
- 3.8: Set up's Try read and wrote a lab instrument in the middle of a run, and another spelling of its address opened
  a second session to it.
"""
import collections
import threading
import time
import types

import pytest

from labharness import lablock
from labharness.agent import runtime as rtmod
from labharness.agent.approval import WebApprover
from labharness.agent.runtime import LabBusy, LabRuntime, Viewer
from labharness.cards import parse_card
from labharness.clock import RealClock
from labharness.gate import ScriptedApprover
from labharness.homelab import build_lab, builtin_registry
from labharness.instrument import Instrument, Stopped, Unavailable
from labharness.lab import WAITS
from labharness.labconfig import InstrumentConfig
from labharness.lablock import LabLock
from simlab import build_virtual_lab, demo_envelope

INFO = {"kind": "file", "path": None, "error": None, "has_file": True}


def wait_until(check, what="", seconds=10.0):
    end = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < end, what
        time.sleep(0.02)


def elsewhere(fn):
    """fn's answer on another thread (the lock's owner reads what it likes)."""
    out = []
    t = threading.Thread(target=lambda: out.append(fn()))
    t.start()
    t.join(5)
    return out[0]


def runtime(tmp_path, answers=(), clock=None, lab=None):
    lab = lab if lab is not None else build_virtual_lab(clock=clock or RealClock(), seed=1)
    return LabRuntime(lab, ScriptedApprover(list(answers)), tmp_path / "data")


class Holding:
    """Work that has the lab (a run: live says so) until released; it takes no notice of a Stop."""

    def __init__(self, rt, run=True, busy=None):
        self.rt, self.run, self.busy = rt, run, busy
        self.held, self.release = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._work, daemon=True)

    def _work(self):
        rt = self.rt
        with rt.lock:
            if self.run:
                rt.live = {"status": "running", "run_id": "x", "plan": "overnight_sweep", "done": 3, "total": 400,
                           "eta_s": 7200}
            if self.busy:
                rt.busy = self.busy
            self.held.set()
            self.release.wait(20)
            if self.run:
                rt.live = dict(rt.live, status="done")
            rt.busy = None

    def __enter__(self):
        self.thread.start()
        assert self.held.wait(5)
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.thread.join(10)


# ---------------------------------------------------------------------- 3.3: a person's action never waits out a run

def test_a_switch_during_a_run_is_refused_at_once_and_never_acts_as_the_run_ends(tmp_path):
    rt = runtime(tmp_path)
    with Holding(rt):
        t0 = time.monotonic()
        with pytest.raises(RuntimeError, match="A plan or the queue is running"):
            rt.switch_instrument("smu", False, "human (portal)")
        assert time.monotonic() - t0 < 1.0
    assert rt.lab.instruments["smu"].enabled                                   # nothing switched once the run ended


def test_a_switch_behind_other_work_says_what_has_the_lab_within_its_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(rtmod, "BUSY_WAIT_S", 0.5)
    rt = runtime(tmp_path)
    with Holding(rt, run=False, busy="setting magnet.B"):                     # a ramp: no run, no queue
        t0 = time.monotonic()
        with pytest.raises(LabBusy) as e:
            rt.switch_instrument("smu", False, "human (portal)")
        assert time.monotonic() - t0 < 5.0
    assert str(e.value).startswith("Kelvin is busy with setting magnet.B (this waited ")
    assert rt.lab.instruments["smu"].enabled
    rt.switch_instrument("smu", False, "human (portal)")                       # once it is done: switched
    assert not rt.lab.instruments["smu"].enabled


def test_unlocking_and_marking_reviewed_never_wait_for_the_lab(tmp_path):
    rt = runtime(tmp_path)
    rt.hands = rt.lab.hands_on = {"by": "Ada", "since": "now"}
    with Holding(rt):
        t0 = time.monotonic()
        rt.hands_off("human (portal)")
        rt.mark_reviewed("human (portal)")
        assert time.monotonic() - t0 < 1.0
        assert rt.hands is None and rt.lab.hands_on is None


def test_reconnecting_behind_a_run_says_so_at_once(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    rt.lab.instruments["tc"].backend = Unavailable("tc", "it was off")       # switched on again since
    with Holding(rt):
        t0 = time.monotonic()
        said = rt.reconnect_instrument("tc")
        assert time.monotonic() - t0 < 3.0
    assert said.startswith("not done: Kelvin is busy with the running plan overnight_sweep"), said
    assert "connected now" in rt.reconnect_instrument("tc")                    # once the run is done


def test_hands_on_finishes_in_the_background_when_the_stopped_work_takes_long(tmp_path, monkeypatch):
    monkeypatch.setattr(rtmod, "HANDS_WAIT_S", 0.3)
    rt = runtime(tmp_path, [True] * 5)
    rt.set("laser", "emission", True)
    assert rt.lab.read("laser", "emission") is True
    with Holding(rt) as work:                                                  # its safe-stop steps take a while
        t0 = time.monotonic()
        lines = rt.hands_on("human (portal)")
        assert time.monotonic() - t0 < 3.0
        assert lines == ["Kelvin stopped the running plan overnight_sweep (point 3 of 400, ~2.0 h left), which is "
                         "still ending: everything that reaches the sample goes to safe as soon as it has. Nothing "
                         "moves away from safe meanwhile."], lines
        assert rt.lab.hands_on is not None                                     # held at once, all the same
        assert rt.lab.read("laser", "emission") is True
        work.release.set()
    wait_until(lambda: rt.lab.read("laser", "emission") is False, "the laser never went to safe")
    [event] = [e for e in rt.gate.log.events() if e["type"] == "hands_on"]
    assert any("laser" in s for s in event["steps"]), event


def test_hands_on_in_the_background_does_nothing_once_a_person_has_unlocked(tmp_path, monkeypatch):
    monkeypatch.setattr(rtmod, "HANDS_WAIT_S", 0.2)
    rt = runtime(tmp_path, [True] * 5)
    rt.set("laser", "emission", True)
    with Holding(rt):
        rt.hands_on("human (portal)")
        rt.hands_off("human (portal)")                                          # changed their mind meanwhile
    time.sleep(0.5)
    assert rt.lab.read("laser", "emission") is True
    assert [e for e in rt.gate.log.events() if e["type"] == "hands_on"] == []


def portal(tmp_path, lab=None):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    pytest.importorskip("httpx")
    from starlette.testclient import TestClient
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    settings = Settings(tmp_path / "settings.json")
    lab = lab if lab is not None else build_virtual_lab(seed=1, switched_off=settings.switched_off)
    rt = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    client = TestClient(create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"]))
    return rt, client, {"X-Kelvin-Token": settings.access_key}


def test_hands_on_runs_on_a_thread_of_its_own(tmp_path):
    """It only makes the lab safer, as Stop does: never behind other work for one of the server's shared threads."""
    rt, client, h = portal(tmp_path)
    seen = []
    rt.hands_on = lambda by: seen.append(threading.current_thread().name) or []
    assert client.post("/api/hands", headers=h, json={"on": True}).status_code == 200
    assert seen == ["kelvin-safe"]


def test_the_lab_panel_switch_answers_409_saying_what_has_the_lab(tmp_path, monkeypatch):
    monkeypatch.setattr(rtmod, "BUSY_WAIT_S", 0.3)
    rt, client, h = portal(tmp_path)
    with Holding(rt, run=False, busy="setting magnet.B"):
        t0 = time.monotonic()
        r = client.post("/api/instruments/smu/switch", headers=h, json={"on": False})
        assert time.monotonic() - t0 < 5.0
    assert r.status_code == 409 and r.json()["error"].startswith("Kelvin is busy with setting magnet.B"), r.text


# ---------------------------------------------------------------------- 3.4: Save and connect, Scan, the queue

PLAN_IV = "plan: iv\nsweep: { smu.V: {from: 0, to: 0.2, step: 0.1} }\nmeasure: [smu.I]\n"


def test_save_and_connect_is_refused_while_the_queue_runs_and_the_queue_runs_on(tmp_path):
    rt = runtime(tmp_path, [True] * 20)
    rt.stop("human (portal)", report=False)                                    # a Stop pressed earlier that day
    rt.queue_add("step: settle_a_bit\nwait: 1.5\n", by="human")
    rt.queue_add(PLAN_IV, by="human")
    assert rt.queue_start(by="human").startswith("queue started"), rt.queue.reason
    wait_until(lambda: rt.queue.items[0].status == "running", "the queue never started")
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="the queue is running"):
        rt.switch_lab(build_virtual_lab(clock=RealClock(), seed=1), None, INFO, "human (portal)")
    assert time.monotonic() - t0 < 1.0
    rt._queue_thread.join(30)
    assert [i.status for i in rt.queue.items] == ["done", "done"], [(i.status, i.message) for i in rt.queue.items]


def test_a_new_gate_goes_on_from_the_old_ones_stops_and_the_queues_actions(tmp_path):
    rt = runtime(tmp_path)
    rt.stop("human (portal)", report=False)
    rt.stop("human (portal)", report=False)
    rt.gate.queue_actions = frozenset({"pg.fire"})
    rt.gate.trip_count = 3
    counted = rt.lab.stops.count
    new = build_virtual_lab(clock=RealClock(), seed=1)
    rt.switch_lab(new, None, INFO, "human (portal)")
    assert rt.lab is new and rt.gate.stop_generation == 2 and rt.gate.trip_count == 3
    assert rt.gate.queue_actions == frozenset({"pg.fire"})
    assert new.stops.count == counted
    assert not rt.gate.auto                                                    # a new lab starts in ask mode


def test_the_portal_refuses_scan_and_save_and_connect_during_the_queue_or_the_setup(tmp_path):
    rt, client, h = portal(tmp_path)
    scanned = []
    rt.scan_found = lambda: scanned.append(1) or []
    assert client.post("/api/instruments/scan", headers=h, json={}).status_code == 200 and scanned == [1]
    rt.queue.state = "running"
    r = client.post("/api/instruments/scan", headers=h, json={})
    assert r.status_code == 409 and r.json()["error"] == "The queue is running; pause it or stop it first."
    r = client.post("/api/lab", headers=h, json={"use": "mine"})
    assert r.status_code == 409 and "queue is running" in r.json()["error"]
    rt.queue.state = "idle"
    rt.setup_status = {"state": "scanning", "message": "Looking for connected instruments (asking each who it is)"}
    r = client.post("/api/instruments/scan", headers=h, json={})
    assert r.status_code == 409 and r.json()["error"] == ("Kelvin is setting up the instruments (Looking for connected "
                                                          "instruments (asking each who it is)); wait for it to finish.")
    assert scanned == [1]


def test_the_runtimes_scan_is_refused_during_a_run_and_says_why(tmp_path):
    rt = runtime(tmp_path)
    with Holding(rt):
        t0 = time.monotonic()
        with pytest.raises(LabBusy, match="the running plan overnight_sweep"):
            rt.scan_found()
        assert time.monotonic() - t0 < 1.0


# ---------------------------------------------------------------------- 3.5: connecting while work has the lab

CFG = {"lab": "t", "instruments": {"smu": {"card": "keithley/2400", "address": "sim://smu"},
                                    "lockin": {"card": "srs/sr830", "address": "sim://lockin"},
                                    "tc": {"card": "lakeshore/336", "address": "sim://tc"}}}


def later_lab(tmp_path):
    lab = build_lab(CFG, registry=builtin_registry(), later=True)
    return lab, LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data")


def test_instruments_connect_beside_a_run_that_doesnt_use_them(tmp_path, monkeypatch):
    monkeypatch.setattr(rtmod, "CONNECT_WAIT_S", 0.2)
    lab, rt = later_lab(tmp_path)
    lab.reconnect("lockin")                                                    # a run starts on the lock-in at once
    held, done = threading.Event(), threading.Event()

    def a_run():
        with rt.lock, rt.lock.only(["lockin"]):
            held.set()
            done.wait(20)
    t = threading.Thread(target=a_run, daemon=True)
    t.start()
    assert held.wait(5)
    t0 = time.monotonic()
    out = rt.connect_pending()
    assert time.monotonic() - t0 < 5 and t.is_alive()                         # while the run still has the lab
    assert out == {"connected": ["smu", "tc"], "not_connected": {}}
    assert lab.instruments["tc"].enabled and lab.connecting == []
    done.set()
    t.join(5)


def test_connecting_beside_a_run_leaves_the_devices_limits_as_they_are(tmp_path, monkeypatch):
    """Holding the lab to the mounted device's protection again clears its limits first, then sets them: never beside
    a run, which sets values against them meanwhile. They hold for the instruments still connecting already (set as
    the lab opened, kept by a reconnect), and the caps are written into the instruments once Kelvin is idle."""
    from labharness import protection
    monkeypatch.setattr(rtmod, "CONNECT_WAIT_S", 0.2)
    lab, rt = later_lab(tmp_path)
    rt.mounted = {"protection": {"limits": {"smu.V": [-1, 1]}}, "device_name": "chip 7"}
    applied = []
    monkeypatch.setattr(protection, "apply", lambda *a, **k: applied.append(threading.get_ident()) or [])
    lab.reconnect("lockin")
    held, done = threading.Event(), threading.Event()

    def a_run():
        with rt.lock, rt.lock.only(["lockin"]):
            held.set()
            done.wait(20)
    t = threading.Thread(target=a_run, daemon=True)
    t.start()
    assert held.wait(5)
    rt.caps_waiting = False
    assert rt.connect_pending()["connected"] == ["smu", "tc"]
    assert applied == [] and rt.caps_waiting                                   # nothing cleared beside the run
    done.set()
    t.join(5)
    rt.connect_pending()
    assert applied == [threading.get_ident()]                                  # with the lab free, as before


def test_an_instrument_the_work_holds_says_what_it_waits_for_and_connects_after(tmp_path, monkeypatch):
    monkeypatch.setattr(rtmod, "CONNECT_WAIT_S", 0.2)
    lab, rt = later_lab(tmp_path)
    progress = []
    with Holding(rt, run=False, busy="setting magnet.B") as work:             # the whole lab
        worker = threading.Thread(target=lambda: rt.connect_pending(lambda *a: progress.append(a)), daemon=True)
        worker.start()
        wait_until(lambda: lab.unavailable.get("tc", "").startswith(WAITS), "tc never said what it waits for")
        assert lab.unavailable["tc"] == WAITS + "setting magnet.B" and "tc" in lab.connecting
        assert lab.away("tc") == "not connected (%s)" % (WAITS + "setting magnet.B")
        wait_until(lambda: any(len(a) == 4 and a[3] == "setting magnet.B" for a in progress))
        work.release.set()
        worker.join(10)
    assert not worker.is_alive() and lab.connecting == [] and lab.unavailable == {}
    assert all(lab.instruments[n].enabled for n in ("smu", "lockin", "tc"))


def test_the_setups_progress_says_what_the_connecting_waits_for(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from labharness.autosetup import AutoSetup
    monkeypatch.setattr(rtmod, "CONNECT_WAIT_S", 0.2)
    lab, rt = later_lab(tmp_path)
    rt.lab_info = {"kind": "file"}
    setup = AutoSetup(rt, {"auto_setup": "off"}, tmp_path / "home")
    with Holding(rt, run=False, busy="setting magnet.B"):
        setup.start("connect")
        wait_until(lambda: "waiting for setting magnet.B, which has the lab" in (setup.status.get("message") or ""),
                   "the set-up never said what it waits for")
        assert setup.status["message"].startswith("Connecting ") and setup.status["state"] == "connecting"
    setup.join(10)
    assert setup.status["state"] == "done" and lab.connecting == []


# ---------------------------------------------------------------------- 3.6: one chat's long reading

SLOW = parse_card({
    "card": 1, "id": "test/slow-vna", "model": "Slow VNA", "vendor": "test", "kind": "vna",
    "identify": {"idn": "^SLOW,VNA"},
    "parameters": {"s21": {"label": "S21", "kind": "measure", "unit": "dB",
                           "get": ["INIT", "*OPC?", "FETCH?"], "acquires": True, "wait": 600}}})


class SlowVisa:
    """A VISA-like connection whose acquisition (*OPC?) takes sweep_s, holding its lock as adapters.VisaBackend does,
    and ending early when stopped() says so."""
    simulated = False
    quiet_close = True

    def __init__(self, sweep_s):
        self.sweep_s = sweep_s
        self.lock = threading.RLock()
        self.res = types.SimpleNamespace(timeout=3000)
        self.sweeping = False

    def write(self, c):
        with self.lock:
            pass

    def query(self, c):
        with self.lock:
            return "SLOW,VNA,1,1" if c.startswith("*IDN") else "-3.0"

    def query_waiting(self, c, seconds, stopped):
        with self.lock:
            self.sweeping = True
            try:
                end = time.monotonic() + self.sweep_s
                while time.monotonic() < end:
                    if stopped():
                        raise Stopped("stopped")
                    time.sleep(0.02)
                return "1"
            finally:
                self.sweeping = False

    def close(self):
        pass


def add_slow_vna(lab, name="vna2", sweep_s=30.0):
    lab.config.instruments[name] = InstrumentConfig(name, "test/slow-vna", "TCPIP0::10.0.0.5::inst0::INSTR")
    backend = SlowVisa(sweep_s)
    lab.instruments[name] = Instrument(name, SLOW, backend, lab.config.instruments[name], stops=lab.stops)
    return backend


def test_a_change_waits_only_for_the_readings_of_its_own_instruments(monkeypatch):
    monkeypatch.setattr(lablock, "SETTLE_S", 3.0)
    lock = LabLock()
    reading, release = threading.Event(), threading.Event()

    def read():   # another chat's long acquisition on the analyzer
        with lock.reading("vna", 1.0) as why:
            assert why is None
            reading.set()
            release.wait(10)
    t = threading.Thread(target=read, daemon=True)
    t.start()
    assert reading.wait(5)
    t0 = time.monotonic()
    assert lock.acquire(timeout=1.0, names=["li"])
    try:
        assert time.monotonic() - t0 < 0.5
        assert elsewhere(lambda: lock.holds("li")) and not elsewhere(lambda: lock.holds("vna"))
        assert lock.acquire(names=["gauge"])                                  # taken again: adds what it names
        assert elsewhere(lambda: lock.holds("gauge")) and not elsewhere(lambda: lock.holds("vna"))
        lock.release()
        assert not elsewhere(lambda: lock.holds("gauge"))
    finally:
        lock.release()
    release.set()
    t.join(5)
    assert lock.owner() == (None, None)


def test_leaving_only_never_waits_for_the_readings_that_began_meanwhile(monkeypatch):
    monkeypatch.setattr(lablock, "SETTLE_S", 3.0)
    lock = LabLock()
    began, release = threading.Event(), threading.Event()

    def read():   # a reading of an instrument the narrowed work doesn't use
        with lock.reading("gauge", 1.0) as why:
            assert why is None
            began.set()
            release.wait(10)
    with lock:
        with lock.only(["li"]):
            t = threading.Thread(target=read, daemon=True)
            t.start()
            assert began.wait(5)
            t0 = time.monotonic()
        assert time.monotonic() - t0 < 0.5
        assert elsewhere(lambda: lock.holds("gauge"))                          # the whole lab again
    release.set()
    t.join(5)


def test_another_chats_long_sweep_never_holds_up_a_set(tmp_path, monkeypatch):
    monkeypatch.setattr(lablock, "SETTLE_S", 4.0)
    monkeypatch.setattr(rtmod, "READ_WAIT_S", 2.0)
    lab = build_virtual_lab(clock=RealClock(), seed=1)
    backend = add_slow_vna(lab, sweep_s=30.0)
    rt = runtime(tmp_path, [True] * 5, lab=lab)
    got = {}
    reader = threading.Thread(target=lambda: got.setdefault("get", rt.get("vna2", "s21")), daemon=True)
    reader.start()
    wait_until(lambda: backend.sweeping, "the sweep never started")
    try:
        t0 = time.monotonic()
        said = rt.set("lockin", "tc", 0.1)
        took = time.monotonic() - t0
        assert took < 1.5, (took, said)
        assert "not done" not in said and "busy" not in said, said
        assert backend.sweeping                                                # it went on, untouched
    finally:
        rt.gate.request_abort()
        reader.join(5)


def test_a_stop_in_one_chat_ends_that_chats_reading_and_nothing_else(tmp_path):
    lab = build_virtual_lab(clock=RealClock(), seed=1)
    a, b = add_slow_vna(lab, "vna2"), add_slow_vna(lab, "vna3")
    rt = runtime(tmp_path, lab=lab)
    got = {}

    def read(chat, name):
        with rt.viewing(Viewer(chat=chat)):
            got[chat] = rt.get(name, "s21")
    ta = threading.Thread(target=read, args=("A", "vna2"), daemon=True)
    tb = threading.Thread(target=read, args=("B", "vna3"), daemon=True)
    ta.start()
    tb.start()
    wait_until(lambda: a.sweeping and b.sweeping, "the sweeps never started")
    gen, count = rt.gate.stop_generation, lab.stops.count
    try:
        t0 = time.monotonic()
        assert rt.stop_readings("A") == 1
        ta.join(3)
        assert not ta.is_alive() and time.monotonic() - t0 < 2
        assert "stopped" in got["A"], got["A"]
        assert tb.is_alive() and b.sweeping                                    # chat B's sweep goes on
        assert rt.gate.stop_generation == gen and lab.stops.count == count     # and nothing else was stopped
        assert rt.stop_readings("A") == 0 and rt.stop_readings("nobody") == 0
    finally:
        rt.gate.request_abort()
        tb.join(5)


def test_a_reading_behind_an_acquisition_says_so_rather_than_blame_the_instrument(tmp_path, monkeypatch):
    monkeypatch.setattr(rtmod, "READ_WAIT_S", 1.0)
    lab = build_virtual_lab(clock=RealClock(), seed=1)
    backend = add_slow_vna(lab, sweep_s=30.0)
    rt = runtime(tmp_path, lab=lab)
    reader = threading.Thread(target=lambda: rt.get("vna2", "s21"), daemon=True)
    reader.start()
    wait_until(lambda: backend.sweeping, "the sweep never started")
    try:
        said = rt.get("vna2", "s21")
        assert said.startswith("vna2.s21: not read now (another reading of it is under way: vna2.s21, an "
                               "acquisition its card lets take up to 10.0 min ("), said
        assert "is it on and connected" not in said
    finally:
        rt.gate.request_abort()
        reader.join(5)


def test_get_value_waits_as_long_as_the_cards_acquisition_may_take(tmp_path, monkeypatch):
    """A sweep its card lets take 10 minutes was said not to answer once the tool had waited 60 s."""
    pytest.importorskip("mcp")
    import anyio
    from mcp import Client
    from labharness.agent import server as server_mod
    monkeypatch.setattr(server_mod, "TOOL_IO_S", 0.3)
    lab = build_virtual_lab(clock=RealClock(), seed=1)
    add_slow_vna(lab, sweep_s=1.0)
    rt = runtime(tmp_path, lab=lab)
    assert rt.acquisition_s("vna2", "s21") == 600 and rt.acquisition_s("tc", "T_sample") == 0

    async def ask():
        async with Client(server_mod.build_server(rt)) as client:
            return (await client.call_tool("get_value", {"instrument": "vna2", "parameter": "s21"})).content[0].text
    said = anyio.run(ask)
    assert said.startswith("vna2.s21 = ") and "doesn't answer" not in said, said


# ---------------------------------------------------------------------- 3.7: the look as a change ends

def test_a_set_reads_only_what_it_touched_as_it_ends(tmp_path, monkeypatch):
    rt = runtime(tmp_path, [True] * 5)
    counts = collections.Counter()
    real = Instrument.read

    def read(self, param, replies=None):
        counts[self.name] += 1
        return real(self, param, replies)
    monkeypatch.setattr(Instrument, "read", read)
    said = rt.set("lockin", "tc", 0.1)
    assert "lockin" in said and "not done" not in said, said
    assert set(counts) == {"lockin"}, counts                                  # 54 reads of every instrument before
    assert "lockin" in rt._status and "tc" in rt._status                      # the rest as the last look found them


# ---------------------------------------------------------------------- 3.8: Set up's Try

CARD_6221 = parse_card({"card": 1, "id": "lab/6221", "model": "6221 (lab draft)", "kind": "current-source",
                        "status": "draft", "error_query": "SYST:ERR?", "identify": {"idn": "MODEL 6221"},
                        "parameters": {
                            "function": {"kind": "measure", "type": "enum",
                                         "values": {"sine": "SIN", "square": "SQU"},
                                         "set": "SOUR:WAVE:FUNC {value}", "get": "SOUR:WAVE:FUNC?"}}})


class Source6221:
    simulated = False
    quiet_close = True

    def __init__(self):
        self.lock = threading.RLock()
        self.sent = []

    def query(self, c):
        self.sent.append(c)
        return {"*IDN?": "KEITHLEY INSTRUMENTS INC.,MODEL 6221,1,D04", "SYST:ERR?": "0,\"No error\"",
                "SOUR:WAVE:FUNC?": "SIN"}.get(c, "0")

    def write(self, c):
        self.sent.append(c)

    def close(self):
        pass


def lab_with_6221():
    lab = build_virtual_lab(clock=RealClock(), seed=1)
    lab.config.instruments["src"] = InstrumentConfig("src", "lab/6221", "GPIB0::12::INSTR")
    source = Source6221()
    lab.instruments["src"] = Instrument("src", CARD_6221, source, lab.config.instruments["src"], stops=lab.stops)
    return lab, source


def test_try_uses_the_labs_connection_however_the_address_is_spelled(tmp_path, monkeypatch):
    from labharness import cardtry
    lab, source = lab_with_6221()
    monkeypatch.setattr(cardtry, "open_backend", lambda *a, **k: pytest.fail("a second session to a lab instrument"))
    for spelling in ("GPIB::12", "gpib0::12::instr", "GPIB0::12::INSTR"):
        result = cardtry.try_card(CARD_6221, spelling, lab)
        assert result["reused"] and result["identity"].startswith("KEITHLEY"), result
    rt = runtime(tmp_path, lab=lab)
    result = rt.card_try(CARD_6221, "GPIB::12")
    assert result["reused"] and "*IDN?" in source.sent


def test_try_never_goes_beside_a_run(tmp_path):
    lab, source = lab_with_6221()
    rt = runtime(tmp_path, lab=lab)
    source.sent.clear()                                                        # (the first look at the lab)
    with Holding(rt):
        t0 = time.monotonic()
        with pytest.raises(LabBusy, match="the running plan overnight_sweep"):
            rt.card_try(CARD_6221, "GPIB::12")
        assert time.monotonic() - t0 < 1.0
    assert source.sent == []                                                   # nothing reached it meanwhile


def test_the_portals_try_answers_409_during_a_run(tmp_path):
    lab, source = lab_with_6221()
    rt, client, h = portal(tmp_path, lab=lab)
    yaml_text = ("card: 1\nid: lab/6221\nmodel: 6221 (lab draft)\nkind: current-source\nstatus: draft\n"
                 "error_query: \"SYST:ERR?\"\nidentify: {idn: MODEL 6221}\nparameters:\n"
                 "  function: {kind: measure, type: enum, values: {sine: SIN, square: SQU}, "
                 "set: \"SOUR:WAVE:FUNC {value}\", get: \"SOUR:WAVE:FUNC?\"}\n")
    source.sent.clear()                                                        # (the first look at the lab)
    with Holding(rt):
        r = client.post("/api/cards/try", headers=h, json={"yaml": yaml_text, "address": "GPIB::12"})
    assert r.status_code == 409 and "the running plan overnight_sweep" in r.json()["error"], r.text
    assert source.sent == []
    r = client.post("/api/cards/try", headers=h, json={"yaml": yaml_text, "address": "GPIB::12"})
    assert r.status_code == 200 and r.json()["reused"] is True, r.text
