"""The monitor never waits behind an instrument (review of 0.3.45, 2.1 and 2.2). A network analyzer's sweep holds its
connection for up to an hour (an HP 8720's card allows 20 minutes, a PNA's 60): the monitor's check that it still
answered waited behind it, and with it every watch, interlock and alarm, and the heartbeat. A drive a watch or an
interlock sent to safe ramped on the monitor's own thread (8 V at 0.5 V/s: 17 s with nothing else checked). And a look
at the lab held up for any other reason said nothing."""
import contextlib
import threading
import time
import types

import pytest

from labharness import monitor as monmod
from labharness.cards import parse_card
from labharness.instrument import Instrument
from labharness.labconfig import InstrumentConfig
from labharness.monitor import Monitor, Watch

CARD = parse_card({
    "card": 1, "id": "test/slow-vna", "model": "Slow VNA", "vendor": "test", "kind": "vna",
    "identify": {"idn": "^SLOW,VNA"},
    "parameters": {"s21": {"label": "S21", "kind": "measure", "unit": "dB",
                           "get": ["INIT", "*OPC?", "FETCH?"], "acquires": True, "wait": 600},
                   "power": {"label": "Power", "kind": "measure", "unit": "dBm", "get": "POW?"}}})


class Visa:
    """As adapters.VisaBackend: one re-entrant lock per connection, which an acquisition holds for its whole sweep."""
    simulated = False
    quiet_close = True

    def __init__(self):
        self.lock = threading.RLock()
        self.res = types.SimpleNamespace(timeout=3000)
        self.sent = []

    def write(self, command):
        with self.lock:
            self.sent.append(command)

    def query(self, command):
        with self.lock:
            self.sent.append(command)
            return "SLOW,VNA,1,1" if command.startswith("*IDN") else "-3.0"

    def close(self):
        pass


class WithLock:
    """A twin behind a connection with a lock of its own, as a VISA instrument's is."""

    def __init__(self, twin):
        self.twin, self.lock = twin, threading.RLock()

    def __getattr__(self, name):
        return getattr(self.twin, name)

    def query(self, command):
        with self.lock:
            return self.twin.query(command)

    def write(self, command):
        with self.lock:
            self.twin.write(command)


class SlowWrites:
    """A twin whose every command takes a while, as on GPIB: a ramp of 80 steps takes seconds."""

    def __init__(self, twin, seconds):
        self.twin, self.seconds = twin, seconds

    def __getattr__(self, name):
        return getattr(self.twin, name)

    def write(self, command):
        time.sleep(self.seconds)
        self.twin.write(command)


@contextlib.contextmanager
def held(lock, most=5.0):
    """Another thread has the connection, as a run's acquisition has it for its whole sweep."""
    got, done = threading.Event(), threading.Event()

    def sweep():
        with lock:
            got.set()
            done.wait(most)
    t = threading.Thread(target=sweep, daemon=True)
    t.start()
    assert got.wait(5)
    try:
        yield
    finally:
        done.set()
        t.join(5)


def add_vna(lab, name="vna2"):
    lab.config.instruments[name] = InstrumentConfig(name, "test/slow-vna", "TCPIP0::10.0.0.5::inst0::INSTR")
    backend = Visa()
    lab.instruments[name] = Instrument(name, CARD, backend, lab.config.instruments[name], stops=lab.stops)
    return lab.instruments[name], backend


def timed(fn):
    t0 = time.monotonic()
    fn()
    return time.monotonic() - t0


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(monmod, "WAIT_S", 0.1, raising=False)
    monkeypatch.setattr(monmod, "SAFE_WAIT_S", 0.2, raising=False)


# ------------------------------------------------------------------------------- 2.1 a sweep never holds the monitor

def test_a_sweep_that_has_an_instrument_never_holds_the_monitor_up(lab, make_gate):
    gate, _ = make_gate([])
    notices = []
    monitor = Monitor(lab, gate, lambda kind, title, text="": notices.append((kind, title)))
    vna, backend = add_vna(lab)
    watch = monitor.add(Watch("tc.T_sample", above=400, every=0))
    gate._asked = {}   # a plan runs: each real instrument is asked whether it answers
    with held(backend.lock):
        took = timed(monitor.tick)
    assert took < 2.0                                              # not the sweep's length
    assert watch.value is not None                                 # the cryostat was watched meanwhile
    assert backend.sent == []                                      # nothing went in between the sweep's commands
    assert vna.health.fails == 0 and vna.health.losses == 0 and not monitor.alarms   # busy is not "doesn't answer"
    monitor.tick()                                                 # the sweep is over: asked again as usual
    assert monitor.last_tick is not None and monitor.watching()


def test_a_busy_instruments_watch_takes_the_works_own_fresh_reading_and_says_when_it_goes_unread(lab, make_gate):
    gate, _ = make_gate([])
    notices = []
    monitor = Monitor(lab, gate, lambda kind, title, text="": notices.append((kind, title)))
    vna, backend = add_vna(lab)
    watch = monitor.add(Watch("vna2.power", above=10, every=0))
    assert vna.read("power") == -3.0                               # the run read it a moment ago
    with held(backend.lock):
        monitor.tick()
        assert watch.value == -3.0 and not monitor.alarms          # the run's own reading stands in
        vna._seen["power"] = (50.0, time.time())                   # the run's next reading is out of range
        monitor.tick()
        [alarm] = monitor.active()
        assert alarm.kind == "reading" and alarm.text.startswith("vna2.power = 50 dBm is above 10")
        monitor._clear(alarm.id, "test")
        watch.alarm = None
        vna._seen["power"] = (-3.0, time.time() - 100)             # nothing fresh: not read, not taken as safe
        monitor.tick()
        assert watch.unread_since is not None and not monitor.active()
        lab.clock.sleep(monmod.UNREAD_S)
        monitor.tick()
        [unread] = monitor.active()
        assert unread.kind == "silent" and unread.text.startswith("vna2.power has not been read for 5 min: vna2 is "
                                                                  "busy")
        assert notices[-1][1].startswith("Alarm: vna2.power has not been read for 5 min")
    monitor.tick()                                                 # the sweep ended: read again
    assert unread.cleared and unread.cleared_text.startswith("vna2.power is read again")
    assert watch.unread_since is None


def test_an_interlock_whose_reading_is_busy_takes_the_works_reading_and_fails_when_it_has_none_for_long(
        lab, make_gate):
    gate, _ = make_gate([])
    monitor = Monitor(lab, gate, lambda *a: None)
    tc = lab.instruments["tc"]
    tc.backend = WithLock(tc.backend)
    lab.instruments["magnet"].backend.state.update(B=1.0, B_actual=1.0)   # magnet.B needs tc.T_sample < 10
    tc.backend.state["T_sample"] = 2.0
    lab.read("tc", "T_sample")                                     # the run read it a moment ago: cold
    gen = gate.stop_generation
    with held(tc.backend.lock):
        assert timed(monitor.tick) < 2.0
        assert not monitor.alarms and gate.stop_generation == gen   # its fresh reading holds: nothing to do
        tc._seen["T_sample"] = (2.0, time.time() - 100)             # nothing fresh: not checked, not passed either
        lab.clock.sleep(5)
        monitor.tick()
        assert not monitor.alarms and monitor._unchecked
        lab.clock.sleep(monmod.INTERLOCK_UNREAD_S)
        monitor.tick()
        [alarm] = monitor.active()
        assert alarm.kind == "interlock" and "tc.T_sample could not be read for 60 s (tc is busy" in alarm.text
        assert gate.stop_generation != gen and alarm.actions[0] == "stopped the running plan"
    assert monitor.safe_done(5)
    assert lab.read("magnet", "B") == 0                            # sent to safe, as for one that can't be read


def test_an_interlock_acts_on_the_works_own_reading_while_its_instrument_is_busy(lab, make_gate):
    gate, _ = make_gate([])
    monitor = Monitor(lab, gate, lambda *a: None)
    tc = lab.instruments["tc"]
    tc.backend = WithLock(tc.backend)
    lab.instruments["magnet"].backend.state.update(B=1.0, B_actual=1.0)
    tc.backend.state["T_sample"] = 25.0
    lab.read("tc", "T_sample")                                     # the run's latest reading: warm
    with held(tc.backend.lock):
        monitor.tick()
    [alarm] = monitor.active()
    assert alarm.kind == "interlock" and "which needs tc.T_sample < 10 (now 25" in alarm.text
    assert monitor.safe_done(5) and lab.read("magnet", "B") == 0


class NoLock:
    """A connection with no lock of its own to wait on."""
    simulated = False

    def __init__(self):
        self.asked = []

    def query(self, command):
        self.asked.append(command)
        return "-3.0"

    def write(self, command):
        pass

    def close(self):
        pass


def test_an_instrument_with_no_lock_to_wait_on_is_left_to_the_work_that_uses_it(lab, make_gate):
    """A connection with no lock to wait on: while the work under way uses its instrument (in_use: the runtime's lab
    lock), the monitor sends it nothing, and doesn't count that as a failure."""
    gate, _ = make_gate([])
    vna, _ = add_vna(lab)
    vna.backend = NoLock()
    monitor = Monitor(lab, gate, lambda *a: None, in_use=lambda name: name == "vna2")
    watch = monitor.add(Watch("vna2.power", above=10, every=0))
    monitor.tick()
    assert vna.backend.asked == [] and watch.unread_since is not None and vna.health.fails == 0
    monitor.in_use = lambda name: False
    lab.clock.sleep(1)
    monitor.tick()
    assert vna.backend.asked == ["POW?"] and watch.unread_since is None and watch.value == -3.0


# ------------------------------------------------------------------------------ 2.2 to safe on a thread of its own

def test_a_drive_sent_to_safe_ramps_on_its_own_thread_and_its_alarm_says_how_it_ended(lab, make_gate):
    gate, _ = make_gate([])
    notices = []
    monitor = Monitor(lab, gate, lambda kind, title, text="": notices.append((kind, title, text)))
    smu = lab.instruments["smu"]
    smu.backend.state.update(V=8.0, output=True)
    smu._last["V"] = 8.0
    smu.backend = SlowWrites(smu.backend, 0.02)                    # 80 steps of 0.1 V at 0.5 V/s: ~1.6 s here
    leak = monitor.add(Watch("smu.I", below=1.0, every=0, then=[("smu", "V", 0.0)], source="lab"))
    cold = monitor.add(Watch("tc.T_sample", above=400, every=0))
    assert timed(monitor.tick) < 1.0                               # not the ramp's length
    [alarm] = monitor.active()
    assert alarm.actions == ["smu.V going to safe"] and "smu.V going to safe" in notices[-1][2]
    lab.clock.sleep(1)
    before = cold.last_read
    assert timed(monitor.tick) < 1.0 and cold.last_read != before   # the monitor goes on meanwhile
    assert monitor._to_safe(alarm, "smu.V", lambda: [])[1] is None  # never sent twice while on its way
    order = []
    second = monitor._to_safe(alarm, "smu.output", lambda: order.append(lab.read("smu", "V")) or [])[1]
    assert second is not None                                      # one instrument's go one after another
    assert monitor.safe_done(10)
    assert order == [0.0] and lab.read("smu", "V") == 0
    assert alarm.actions[0].startswith("smu.V 8 V → 0 V") and "smu.V going to safe" not in alarm.actions
    [said] = [e for e in gate.log.events() if e["type"] == "alarm_actions"]
    assert said["alarm"] == alarm.id and said["ok"] and said["actions"][0].startswith("smu.V 8 V → 0 V")
    assert leak.alarm == alarm.id


def test_a_drive_that_could_not_go_to_safe_is_told_to_people(lab, make_gate, monkeypatch):
    gate, _ = make_gate([])
    notices = []
    monitor = Monitor(lab, gate, lambda kind, title, text="": notices.append((kind, title, text)))
    let_go = threading.Event()

    def stuck(drive):
        let_go.wait(5)
        return [(False, "could not bring %s to safe: it doesn't answer" % drive)]
    monkeypatch.setattr(monitor, "_abort_to_safe", stuck)
    lab.instruments["magnet"].backend.state.update(B=1.0, B_actual=1.0)
    lab.instruments["tc"].backend.state["T_sample"] = 25.0
    monitor.tick()
    [alarm] = monitor.active()
    assert alarm.actions == ["stopped the running plan", "magnet.B going to safe"]
    let_go.set()
    assert monitor.safe_done(5)
    assert alarm.actions[1] == "could not bring magnet.B to safe: it doesn't answer"
    assert notices[-1][:2] == ("alarm", "Alarm: magnet.B did not go to safe")


def test_stopping_the_monitor_waits_for_a_drive_on_its_way_to_safe_but_not_for_ever(lab, make_gate, monkeypatch):
    gate, _ = make_gate([])
    monkeypatch.setattr(monmod, "SAFE_STOP_S", 0.3, raising=False)
    monitor = Monitor(lab, gate, lambda *a: None)
    alarm = monitor._new("reading", "smu.I", "test")
    never = threading.Event()
    monitor._to_safe(alarm, "smu.V", lambda: never.wait(10) and [])
    assert timed(monitor.stop) < 2.0
    [said] = [e for e in gate.log.events() if e["type"] == "alarm_actions"]
    assert said["actions"] == ["smu.V was still on its way to safe as Kelvin stopped watching"] and not said["ok"]
    never.set()


# ---------------------------------------------------------------------------- a look held up says so, while it is

class Stuck:
    """A maker's driver whose call never returns (no lock to wait on, and the work under way doesn't use it)."""
    simulated = False

    def __init__(self):
        self.let_go = threading.Event()

    def query(self, command):
        self.let_go.wait(10)
        return "1.0"

    def write(self, command):
        pass

    def close(self):
        pass


def test_a_look_at_the_lab_held_up_is_said_while_it_is_and_clears_after(lab, make_gate, monkeypatch):
    gate, _ = make_gate([])
    monkeypatch.setattr(monmod, "LATE_S", 0.3, raising=False)
    monkeypatch.setattr(monmod, "LATE_EVERY", 0.05, raising=False)
    notices = []
    monitor = Monitor(lab, gate, lambda kind, title, text="": notices.append((kind, title)), poll_s=0.05)
    vna, _ = add_vna(lab, "vna3")
    stuck = vna.backend = Stuck()
    monitor.add(Watch("vna3.power", above=10, every=1e9))          # read once
    monitor.start()
    try:
        deadline = time.monotonic() + 5
        while not notices and time.monotonic() < deadline:
            time.sleep(0.05)
        [alarm] = monitor.active()
        assert alarm.kind == "delay" and alarm.text.startswith("Kelvin's watching is held up: reading vna3.power "
                                                               "has taken")
        assert notices[-1][1].startswith("Alarm: Kelvin's watching is held up")
        stuck.let_go.set()
        deadline = time.monotonic() + 5
        while not alarm.cleared and time.monotonic() < deadline:
            time.sleep(0.05)
        assert alarm.cleared and alarm.cleared_text.startswith("Kelvin's watching is on time again")
    finally:
        stuck.let_go.set()
        monitor.stop()


def test_one_check_that_fails_never_keeps_the_others_from_their_turn(lab, make_gate, monkeypatch):
    gate, _ = make_gate([])
    monitor = Monitor(lab, gate, lambda *a: None)
    lab.instruments["magnet"].backend.state.update(B=1.0, B_actual=1.0)
    lab.instruments["tc"].backend.state["T_sample"] = 25.0

    def broken(now):
        raise RuntimeError("a bug in one check")
    monkeypatch.setattr(monitor, "_check_setpoints", broken)
    with pytest.raises(RuntimeError, match="a bug in one check"):
        monitor.tick()
    assert [a.kind for a in monitor.active()] == ["interlock"]     # the interlocks had their turn all the same
