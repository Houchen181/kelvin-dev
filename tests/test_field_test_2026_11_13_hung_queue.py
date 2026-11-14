"""Field report 2026-11-13: a hung GPIB instrument froze the queue. GPIB0 (an NI GPIB-USB-HS with a 34401A and four
lock-ins) stopped answering at 16:33 (the dmm's VI_ERROR_CONN_LOST); at 18:28 the queue started a read-only plan of
the attoDRY's pressures, on no GPIB instrument, and it never took a point. Stop (five times), Skip and stop_run changed
nothing, removing it was refused ("running; stop it first"), and the person's Clear GPIB0 was refused ("busy with the
queue"). Only a restart of Kelvin got the lab back, with nobody in the lab.

What held it: the run's record of the lab as it starts (Lab.snapshot) read every instrument, the hung ones too, and a
read of an instrument waited with no end for its connection while another exchange with it hung in the driver; a Stop
was only looked at between points."""
import threading
import time

import pytest

from labharness.adapters import BUSES, set_resource_manager
from test_field_test_2026_11_06 import runtime
from test_field_test_2026_11_06_bus import HangingBus
from test_hardware import Device


class Stuck(Device):
    """An instrument whose exchanges hang in its driver: each one waits there, holding its connection, until the test
    lets it go, as an exchange with a dead GPIB-USB adapter did."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.hung, self.free, self.inside = threading.Event(), threading.Event(), threading.Event()

    def answers(self, res):
        if self.hung.is_set():
            self.inside.set()
            self.free.wait(30)
            raise TimeoutError("VI_ERROR_CONN_LOST")
        return super().answers(res)


LAB = """lab: Cryostat
instruments:
  cryo:
    card: lakeshore/336
    address: serial://COM3
    connection: { baud_rate: 57600, data_bits: 7, parity: odd }
  dmm:
    card: keysight/34465a
    address: GPIB0::27::INSTR
  lockin:
    card: srs/sr830
    address: GPIB0::8::INSTR
"""

PLAN = "plan: pressures\ntime: { every: 0.3, points: 40 }\nmeasure: [cryo.T_A]\n"   # a read-only log, on no GPIB


@pytest.fixture
def board():
    dmm = Stuck("keysight/34465a")
    bus = HangingBus({"ASRL3::INSTR": Device("lakeshore/336", serial={"baud_rate": 57600, "data_bits": 7,
                                                                       "parity": "odd"}),
                      "GPIB0::27::INSTR": dmm, "GPIB0::8::INSTR": Device("srs/sr830")})
    set_resource_manager(bus)
    yield bus, dmm
    dmm.free.set()
    set_resource_manager(None)
    BUSES.hung.clear()


def wait_until(check, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def hang_the_dmm(rt, dmm):
    """Another exchange with the dmm hangs in its driver and holds its connection (the monitor's look, say)."""
    assert wait_until(lambda: _answers(rt, "dmm", "V_dc"), 10), "the dmm never connected"
    dmm.hung.set()
    threading.Thread(target=lambda: _quietly(rt.lab.read, "dmm", "V_dc"), daemon=True).start()
    assert dmm.inside.wait(5), "nothing hung in the dmm's driver"


def _answers(rt, name, param):
    try:
        rt.lab.read(name, param)
        return True
    except Exception:
        return False


def _quietly(fn, *a):
    try:
        fn(*a)
    except Exception:
        pass


def points(rt):
    return (rt.live_view() or {}).get("done") or 0


def test_a_queued_read_only_plan_never_waits_on_a_hung_instrument_it_doesnt_use(tmp_path, board):
    bus, dmm = board
    rt = runtime(tmp_path, LAB)
    hang_the_dmm(rt, dmm)
    rt.queue_add(PLAN)
    assert rt.queue_start().startswith("queue started")
    assert wait_until(lambda: points(rt) >= 2, 8), "the attoDRY's log took no point while the dmm hung"
    rt.stop("human (portal)")
    assert wait_until(lambda: rt.queue.state == "paused", 5)


DMM_PLAN = "plan: dmm log\ntime: { every: 0.3, points: 40 }\nmeasure: [dmm.V_dc]\n"


def hold(rt, name):
    """Another exchange has this instrument's connection and doesn't give it back (hung in its driver): a thread
    holds its lock until the returned event is set."""
    lock, held, let_go = rt.lab.instruments[name].backend.lock, threading.Event(), threading.Event()

    def holder():
        with lock:
            held.set()
            let_go.wait(60)
    threading.Thread(target=holder, daemon=True).start()
    assert held.wait(5)
    return let_go


def test_stop_ends_a_queued_run_that_waits_for_its_own_hung_instrument(tmp_path, board):
    """The run's own instrument has another exchange hung in its driver: its wait for the connection ends at a Stop,
    and the queue pauses with the item stopped, in seconds, though that other exchange is still hung; the stopped
    item can be taken out."""
    bus, dmm = board
    rt = runtime(tmp_path, LAB)
    assert wait_until(lambda: _answers(rt, "dmm", "V_dc"), 10)
    rt.queue_add(DMM_PLAN)
    rt.queue_add(PLAN)
    assert rt.queue_start().startswith("queue started")
    assert wait_until(lambda: points(rt) >= 1, 8)
    let_go = hold(rt, "dmm")
    try:
        time.sleep(1.0)   # its next point waits for the dmm's connection
        done = points(rt)
        t0 = time.monotonic()
        rt.stop("human (portal)")
        assert wait_until(lambda: rt.queue.state == "paused", 5), (rt.queue.state,
                                                                   [i.status for i in rt.queue.items])
        assert time.monotonic() - t0 < 5 and points(rt) <= done + 1
        assert [i.status for i in rt.queue.items] == ["stopped", "waiting"],             [(i.status, i.message) for i in rt.queue.items]
        said = rt.queue_change("remove", rt.queue.items[0].id)   # no longer "running; stop it first"
        assert not said.startswith("not changed") and [i.name for i in rt.queue.items] == ["pressures"], said
    finally:
        let_go.set()


def test_a_person_clears_a_hung_bus_while_the_queue_runs_on_another_instrument(tmp_path, board):
    """Clear GPIB0 was refused, "busy with the queue": a bus that isn't answering is cleared beside the work that has
    the lab, and that work goes on."""
    bus, dmm = board
    rt = runtime(tmp_path, LAB)
    assert wait_until(lambda: _answers(rt, "dmm", "V_dc") and _answers(rt, "lockin", "X"), 10)
    rt.queue_add(PLAN)
    assert rt.queue_start().startswith("queue started")
    assert wait_until(lambda: points(rt) >= 1, 8)
    bus.hang()
    for name, param in [("dmm", "V_dc"), ("lockin", "X")] * 2:
        _quietly(rt.lab.read, name, param)
    rt.monitor.tick()
    assert "GPIB0" in BUSES.hung
    said = rt.clear_bus("GPIB0", by="human (portal)")
    assert said.startswith("cleared GPIB0 beside the running plan pressures"), said
    assert bus.ifc == 1 and "GPIB0" not in BUSES.hung
    before = points(rt)
    assert wait_until(lambda: points(rt) > before, 5), "the queue's run went on"
    rt.stop("human (portal)")
    assert wait_until(lambda: rt.queue.state == "paused", 5)


def test_a_stop_ends_a_run_whose_own_exchange_hangs_in_the_driver_at_once(tmp_path, board):
    """The run's own exchange hangs in the driver (a dead GPIB-USB adapter): a person's Stop ends the run's wait at once
    (the call goes on, unwaited, on its session's own thread), the queue pauses with the item stopped, and stop_run
    says stopped, truly, without waiting."""
    bus, dmm = board
    rt = runtime(tmp_path, LAB)
    assert wait_until(lambda: _answers(rt, "dmm", "V_dc"), 10)
    rt.queue_add(DMM_PLAN)
    assert rt.queue_start().startswith("queue started")
    assert wait_until(lambda: points(rt) >= 1, 8)
    dmm.hung.set()   # its next exchange hangs in the driver, on the run's own thread
    assert dmm.inside.wait(5)
    time.sleep(3.5)   # it has waited a while (the field test's stop_run came minutes on)
    t0 = time.monotonic()
    said = rt.stop_work(by="agent")
    assert time.monotonic() - t0 < 1.0   # it never waits long
    assert "stopped the running plan dmm log" in said and "hasn't ended" not in said, said
    assert wait_until(lambda: rt.queue.state == "paused", 3)
    assert rt.queue.items[0].status == "stopped"


def test_stop_run_says_what_the_work_still_waits_for_when_a_stop_cant_end_it(tmp_path, board, monkeypatch):
    """Work a Stop can't end at once (a maker's call, which only its deadline ends): stop_run says what it waits for,
    rather than "stopped"."""
    bus, dmm = board
    rt = runtime(tmp_path, LAB)
    me = threading.get_ident()
    inst = rt.lab.instruments["dmm"]
    held = threading.Event()

    def stuck():   # work that has the lab, in an exchange no Stop reaches
        with rt.lock:
            inst.asking[threading.get_ident()] = ("MEAS:VOLT:DC?", time.time() - 10)
            held.set()
            time.sleep(3)
            inst.asking.clear()
    threading.Thread(target=stuck, daemon=True).start()
    assert held.wait(5)
    said = rt.stop_work(by="agent")
    assert "hasn't ended yet: it waits for dmm's reply to MEAS:VOLT:DC?" in said and "clear_bus" in said, said
    del me


QUICK_LAB = LAB.replace("""  dmm:
    card: keysight/34465a
    address: GPIB0::27::INSTR
""", """  dmm:
    card: keysight/34465a
    address: GPIB0::27::INSTR
    connection: { timeout_ms: 200 }
""")


def test_a_driver_call_that_never_returns_lets_go_of_the_run_and_the_lab_in_its_time(tmp_path, board, monkeypatch):
    """The run's own exchange hangs in the driver for good: within twice its timeout and a grace, the run gets the
    instrument's loss (nothing waits on that call any more), the queue pauses saying so, every other exchange with the
    instrument is refused at once, the lab is free, and reconnecting makes a fresh session."""
    from labharness import adapters
    monkeypatch.setattr(adapters, "CALL_FLOOR_S", 1.0)
    monkeypatch.setattr(adapters, "CALL_GRACE_S", 0.5)
    bus, dmm = board
    rt = runtime(tmp_path, QUICK_LAB)
    assert wait_until(lambda: _answers(rt, "dmm", "V_dc"), 10)
    rt.queue_add(DMM_PLAN)
    assert rt.queue_start().startswith("queue started")
    assert wait_until(lambda: points(rt) >= 1, 8)
    dmm.hung.set()   # never let go (until the test ends)
    assert dmm.inside.wait(5)
    t0 = time.monotonic()
    assert wait_until(lambda: rt.queue.state == "paused", 8), (rt.queue.state, [i.status for i in rt.queue.items])
    assert time.monotonic() - t0 < 6
    item = rt.queue.items[0]
    assert item.status == "failed" and "didn't return" in item.message, item.message
    assert rt.lock.owner()[0] is None                               # the lab is free
    t0 = time.monotonic()
    assert not _answers(rt, "dmm", "V_dc") and time.monotonic() - t0 < 0.5   # refused at once, nothing waited for
    dmm.hung.clear()                                                # the adapter is back
    said = rt.reconnect_instrument("dmm", by="human (portal)")
    assert "reconnected" in said or "connected" in said, said
    assert _answers(rt, "dmm", "V_dc")


def stuck_work(rt, name="dmm", seconds=4.0):
    """Work that has the lab and is in an exchange with an instrument that no Stop ends (a maker's call): a thread
    holding the lab lock, its exchange on record, for `seconds`."""
    inst, held = rt.lab.instruments[name], threading.Event()

    def work():
        with rt.lock:
            inst.asking[threading.get_ident()] = ("MEAS:VOLT:DC?", time.time() - 10)
            held.set()
            time.sleep(seconds)
            inst.asking.clear()
    threading.Thread(target=work, daemon=True).start()
    assert held.wait(5)


def test_bring_to_safe_never_waits_on_work_that_doesnt_let_go_of_the_lab(tmp_path, board, monkeypatch):
    """Bring to safe waited with no end behind a run stuck on a hung bus, then acted, unannounced, when that run let
    go: it waits RESCUE_WAIT_S, then goes ahead beside the stuck work, and says so."""
    from labharness.agent import runtime as rtmod
    monkeypatch.setattr(rtmod, "RESCUE_WAIT_S", 0.5)
    bus, dmm = board
    rt = runtime(tmp_path, LAB)
    assert wait_until(lambda: _answers(rt, "cryo", "T_A"), 10)
    stuck_work(rt)
    t0 = time.monotonic()
    lines = rt.bring_to_safe("human (portal)")
    assert time.monotonic() - t0 < 3.0, lines
    assert "hadn't let go of the lab" in lines[0] and "went ahead beside it" in lines[0], lines


def test_stops_report_never_holds_the_lab(tmp_path, board):
    """Every Stop's report of what is still on took the whole lab and read every instrument: with one hung, the lab
    froze again behind it. Now it only looks, and takes no lock."""
    bus, dmm = board
    rt = runtime(tmp_path, LAB)
    assert wait_until(lambda: _answers(rt, "dmm", "V_dc") and _answers(rt, "cryo", "T_A"), 10)
    let_go = hold(rt, "dmm")   # an exchange with the dmm that doesn't end
    try:
        rt.queue_add(PLAN)
        assert rt.queue_start().startswith("queue started")
        assert wait_until(lambda: points(rt) >= 1, 8)
        rt.stop("human (portal)")
        assert wait_until(lambda: rt.queue.state == "paused", 5)
        for _ in range(10):   # the report's thread never takes the lab: it is free whenever looked at
            assert rt.lock.acquire(timeout=1.0), "something held the lab after the stop"
            rt.lock.release()
            time.sleep(0.1)
    finally:
        let_go.set()


def test_the_queues_wait_for_the_lab_ends_at_a_stop(tmp_path, board):
    """The queue's next item waited for the lab with no end, "starting", so it could be neither skipped nor removed:
    a Stop ends that wait, and the item never starts."""
    bus, dmm = board
    rt = runtime(tmp_path, LAB)
    held, release = threading.Event(), threading.Event()

    def other_work():
        with rt.lock:
            held.set()
            release.wait(10)
    threading.Thread(target=other_work, daemon=True).start()
    assert held.wait(5)
    out = {}
    gen = rt.gate.stop_generation

    def queue_waits():
        with rt._queue_lab(gen) as took:
            out["took"] = took
    t = threading.Thread(target=queue_waits, daemon=True)
    t.start()
    time.sleep(0.5)
    assert t.is_alive()   # it waits for the lab
    rt.stop("human (portal)")
    t.join(2)
    release.set()
    assert out == {"took": False}


def test_a_makers_call_that_never_returns_ends_its_connection_and_a_stop_ends_the_wait_at_once(monkeypatch):
    """A maker's driver's call that never returns (Zurich Instruments', MultiVu's...) is waited for call_s at most,
    then its connection ends (gone); a person's Stop ends the wait at once, the call going on unwaited."""
    from labharness import adapters, drivers
    from labharness.homelab import builtin_registry
    from labharness.instrument import Stopped

    class Hanging(drivers.DriverBackend):
        call_s = 0.6

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.free = threading.Event()

        def get(self, path):
            self.free.wait(30)
            return 1.0

        def identity(self):
            return "Fake,Hanging,1"
    card = builtin_registry().get("zhinst/mfli")
    b = Hanging("lockin9", card, "zhinst://dev1234")
    try:
        stopped = threading.Event()
        threading.Timer(0.2, stopped.set).start()
        t0 = time.monotonic()
        with adapters.stoppable(stopped.is_set):
            with pytest.raises(Stopped):
                b.query("GET demods/0/sample")
        assert time.monotonic() - t0 < 1.0 and not b.gone
        t0 = time.monotonic()
        with pytest.raises(adapters.LinkLost, match="didn't return"):
            b.query("GET demods/0/sample")
        assert time.monotonic() - t0 < 3.0 and b.gone
        t0 = time.monotonic()
        with pytest.raises(adapters.LinkLost):   # refused at once from then on
            b.query("GET demods/0/sample")
        assert time.monotonic() - t0 < 0.2
    finally:
        b.free.set()


class Dead(Device):
    """An instrument that stopped answering, as a real one does: each question waits out its session's timeout (as
    set when it is asked), then gets nothing."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.dead = False
        self.waited = []   # each question's timeout (ms) while it was dead

    def answers(self, res):
        if self.dead:
            self.waited.append(res.timeout)
            time.sleep((res.timeout or 1000) / 1000.0)
            return False
        return super().answers(res)


SLOW_LAB = LAB.replace("""  lockin:
    card: srs/sr830
    address: GPIB0::8::INSTR
""", """  lockin:
    card: srs/sr830
    address: GPIB0::8::INSTR
    connection: { timeout_ms: 3000 }
""")


def test_the_monitor_never_waits_on_an_instrument_that_stopped_answering(tmp_path):
    """The field test's monitor spent 31 s at a time asking a lock-in that didn't answer, its watches and interlocks
    waiting behind (and its GPIB bus held with them). Once one has stopped answering, the monitor's looks leave it
    alone; it is asked again whether it is back on a thread of its own, on its health's schedule, waiting a second for
    its identity, not its card's whole timeout; once it answers, it is watched as before."""
    from labharness.monitor import Watch
    lockin = Dead("srs/sr830")
    set_resource_manager(HangingBus({"ASRL3::INSTR": Device("lakeshore/336", serial={
        "baud_rate": 57600, "data_bits": 7, "parity": "odd"}), "GPIB0::27::INSTR": Device("keysight/34465a"),
        "GPIB0::8::INSTR": lockin}))
    try:
        rt = runtime(tmp_path, SLOW_LAB)
        inst, monitor = rt.lab.instruments["lockin"], rt.monitor
        monitor.add(Watch("lockin.X", above=1, every=0))
        monitor.tick()
        assert inst.health.fails == 0
        lockin.dead = True
        monitor.tick()                                    # the first look finds it silent: its timeout, once
        assert inst.health.fails == 1 and lockin.waited and lockin.waited[0] == 3000
        asked = len(lockin.waited)
        for _ in range(3):                                # from then on the looks leave it alone
            t0 = time.monotonic()
            monitor.tick()
            assert time.monotonic() - t0 < 0.5, "a look waited on the silent lock-in"
        assert len(lockin.waited) == asked
        inst.health.lost_at -= 1000                       # its next question is due now (Health.resting)
        t0 = time.monotonic()
        monitor.tick()
        assert time.monotonic() - t0 < 0.5 and "lockin" in monitor._reopening   # asked on a thread of its own
        assert wait_until(lambda: "lockin" not in monitor._reopening, 5)
        assert lockin.waited[asked:] and set(lockin.waited[asked:]) == {1000}   # a second, not 3
        assert inst.health.fails == 2
        assert wait_until(lambda: inst.backend.res._raw.timeout == 3000, 2)    # its own timeout is back
        lockin.dead = False                               # back
        inst.health.lost_at -= 1000
        monitor.tick()
        assert wait_until(lambda: "lockin" not in monitor._reopening, 5)
        assert inst.health.fails == 0
        before = len(lockin.sent)
        monitor.tick()                                    # watched as before
        assert len(lockin.sent) > before
    finally:
        set_resource_manager(None)
        BUSES.hung.clear()


def test_a_stop_during_a_short_wait_never_leaves_the_instruments_timeout_short(tmp_path, board):
    """A question asked briefly sets the session's timeout short and puts it back after. A Stop that ends the wait
    while the driver still has the call must not take the putting back away: the instrument would answer every
    later command against a second's timeout. The setting goes in its turn, after the call, Stop or no Stop."""
    from labharness.instrument import Stopped, briefly
    bus, dmm = board
    rt = runtime(tmp_path, LAB)
    assert wait_until(lambda: _answers(rt, "dmm", "V_dc"), 10)
    inst = rt.lab.instruments["dmm"]
    raw = inst.backend.res._raw
    before = inst.backend.res.timeout
    assert before and before != 500 and raw.timeout == before
    dmm.hung.set()
    out = {}

    def ask():
        try:
            with briefly(500):
                inst.query_raw("*IDN?")
        except BaseException as e:
            out["error"] = e
    t = threading.Thread(target=ask, daemon=True)
    t.start()
    assert dmm.inside.wait(5)
    assert raw.timeout == 500                             # set short for the question
    rt.stop("human (portal)")
    t.join(2)
    assert not t.is_alive() and isinstance(out.get("error"), Stopped), out
    assert inst.backend.res.timeout == before             # what every later call is waited for by
    dmm.hung.clear()
    dmm.free.set()                                        # the driver lets the call go: the setting goes next
    assert wait_until(lambda: raw.timeout == before, 3), raw.timeout


def test_a_look_waits_on_a_makers_driver_a_minute_at_most_and_leaves_its_call_be(monkeypatch):
    """A maker's driver's call is waited for ten minutes (an acquisition may take that); the monitor's look, a minute
    at most: past it the look stops waiting (busy) and the call goes on, the connection the work uses left as it is (a
    look that ended it would end a slow reading's run). A look into a call that never returned held every watch and
    interlock with it. The call is found hung by the next caller, once past its own limit."""
    from labharness import adapters, drivers
    from labharness.homelab import builtin_registry
    from labharness.instrument import Busy
    monkeypatch.setattr(drivers, "LOOK_CALL_S", 0.5)

    class Hanging(drivers.DriverBackend):
        call_s = 3.0

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.free = threading.Event()

        def get(self, path):
            self.free.wait(30)
            return 1.0

        def identity(self):
            return "Fake,Hanging,1"
    b = Hanging("lockin9", builtin_registry().get("zhinst/mfli"), "zhinst://dev1234")
    try:
        t0 = time.monotonic()
        with adapters.stoppable(lambda: False, 1.0):     # the monitor's look
            with pytest.raises(Busy, match="a look waits no longer"):
                b.query("GET demods/0/sample")
        assert time.monotonic() - t0 < 2.0 and not b.gone
        with pytest.raises(adapters.LinkLost, match="didn't return"):   # the call went on: hung past its own 3 s
            b.query("GET demods/0/sample")
        assert time.monotonic() - t0 < 6.0 and b.gone
    finally:
        b.free.set()


def test_a_stop_never_lets_another_axis_into_a_controllers_driver_beside_a_call_still_in_it():
    """A controller's axes share its connection, and each exchange holds its lock. A Stop ends an axis's wait for its
    call at once, the call going on in the driver; the lock is let go of then. Another axis's call must go into the
    driver after that call, not beside it: pylablib's ANC350 (and an ANC300's command line) can't take two at once."""
    from labharness import adapters, positioners
    from labharness.homelab import builtin_registry
    from labharness.instrument import Stopped
    free, inside, overlap, busy = threading.Event(), threading.Event(), [], threading.Lock()

    class Controller:
        def __init__(self, conn=0):
            pass

        def _in(self, fn):
            if not busy.acquire(False):
                overlap.append(fn)   # a second call while one was still in the driver
                return 0.0
            try:
                return fn()
            finally:
                busy.release()

        def get_position(self, axis):
            def slow():
                if axis == 0:
                    inside.set()
                    free.wait(10)
                return 1e-6
            return self._in(slow)

        def get_hardware_id(self):
            return 1

        def close(self):
            pass

    class Module:
        ANC350 = Controller
    card = builtin_registry().get("attocube/anc350")
    x = positioners.ANC350("x", card.bind({"axis": "0"}), "anc350://7", module=Module)
    y = positioners.ANC350("y", card.bind({"axis": "1"}), "anc350://7", module=Module)
    try:
        assert x.lock is y.lock and x._courier is y._courier
        stopped = threading.Event()
        threading.Timer(0.3, stopped.set).start()
        with adapters.stoppable(stopped.is_set):
            with pytest.raises(Stopped):
                with x.lock:
                    x._driver_call("reading position0", x.get, "position0")
        assert inside.is_set()
        out = {}

        def read_y():
            with y.lock:
                out["y"] = y._driver_call("reading position1", y.get, "position1")
        t = threading.Thread(target=read_y, daemon=True)
        t.start()
        time.sleep(0.5)
        assert t.is_alive() and not overlap   # it waits for x's call to leave the driver
        free.set()
        t.join(5)
        assert out.get("y") == pytest.approx(1.0) and not overlap
    finally:
        free.set()
        x.close()
        y.close()


def test_a_call_to_a_closed_connection_is_refused_at_once_never_left_waiting():
    """A connection made anew that failed (the instrument still off) kept the session it had closed; the next try
    closed that one again through its thread, which had ended, and waited for ever: the reconnector, and everything
    behind it. A call to a connection whose thread is gone is refused at once, however it came to be gone."""
    from labharness import adapters
    c = adapters.Courier("smu")
    assert c.call(5, "its identity", lambda: 1) == 1
    c.end()
    t0 = time.monotonic()
    with pytest.raises(adapters.LinkLost, match="closed"):
        c.call(5, "closing it", lambda: None)
    assert time.monotonic() - t0 < 0.5
    raced = adapters.Courier("smu2")
    raced._jobs.put(None)            # its end came between a call's look at it and the call being asked
    raced._thread.join(2)
    t0 = time.monotonic()
    with pytest.raises(adapters.LinkLost, match="closed"):
        raced.call(5, "its identity", lambda: 1)
    assert time.monotonic() - t0 < 1.0


# ---------------------------------------------------------------------- an independent review's findings, kept fixed

class Slow(Device):
    """An instrument each exchange with which takes `slow` seconds (a slow reading, a busy bus)."""
    slow = 0.0

    def answers(self, res):
        if self.slow:
            time.sleep(self.slow)
        return super().answers(res)


class Flaky(Device):
    """An instrument that misses `fail` replies, then answers as ever (a serial hiccup)."""
    fail = 0

    def answers(self, res):
        if self.fail:
            self.fail -= 1
            return False
        return super().answers(res)


SMU_LAB = """lab: Bench
instruments:
  smu:
    card: keithley/2400
    address: GPIB0::24::INSTR
"""

INTERLOCK_LAB = """lab: Bench
instruments:
  smu:
    card: keithley/2400
    address: GPIB0::24::INSTR
  lockin:
    card: srs/sr830
    address: GPIB0::8::INSTR
interlocks:
  - { drive: smu.V, require: lockin.X < 1 }
"""


def _at_safe(inst, *params):
    return all(inst.spec(p).distance(inst.read(p)) == 0 for p in params)


def test_a_stop_never_cuts_a_drives_way_to_safe_short(tmp_path):
    """A second Stop (or Bring to safe's own, an interlock's trip) while drives go to safe ended the exchange under
    way: the ramp to safe stopped half way, and the instrument's later safe steps (its output off) were skipped."""
    from test_hardware import Bus
    smu = Slow("keithley/2400")
    set_resource_manager(Bus({"GPIB0::24::INSTR": smu}))
    try:
        rt = runtime(tmp_path, SMU_LAB)
        inst = rt.lab.instruments["smu"]
        inst.write("output", 1)
        inst.write("V", 0.5)
        smu.slow = 0.3
        out = {}
        t = threading.Thread(target=lambda: out.update(r=rt.gate.abort_to_safe(["smu.V", "smu.output"], by="test",
                                                                              hold=False)), daemon=True)
        t.start()
        time.sleep(0.8)
        rt.gate.request_abort("")            # a second Stop while the drives go to safe
        t.join(60)
        smu.slow = 0.0
        assert not t.is_alive() and all(r.ok for r in out["r"]), [r.message for r in out.get("r", [])]
        assert _at_safe(inst, "V", "output")
    finally:
        set_resource_manager(None)


def test_a_stop_during_the_monitors_interlock_reading_never_trips_it(tmp_path):
    """A person's Stop that came while the monitor read an interlock's condition ended that reading, and the
    interlock tripped as one that can't be read: an alarm, and its drive to safe even where the lab config holds."""
    from test_hardware import Bus
    lockin = Slow("srs/sr830")
    set_resource_manager(Bus({"GPIB0::24::INSTR": Device("keithley/2400"), "GPIB0::8::INSTR": lockin}))
    try:
        rt = runtime(tmp_path, INTERLOCK_LAB)
        smu, m = rt.lab.instruments["smu"], rt.monitor
        smu.write("output", 1)
        smu.write("V", 0.5)
        m._last_interlock_check = -1e18
        m.tick()
        assert not m.alarms
        lockin.slow = 0.8
        m._last_interlock_check = -1e18
        t = threading.Thread(target=m.tick, daemon=True)
        t.start()
        time.sleep(0.5)
        rt.stop("human (portal)")            # nothing runs: a person's Stop
        t.join(30)
        lockin.slow = 0.0
        assert not [a for a in m.alarms if a.kind == "interlock"], [a.text for a in m.alarms]
        assert smu.read("V") == pytest.approx(0.5)
    finally:
        set_resource_manager(None)


def test_one_missed_reply_never_trips_an_interlock_that_reads_well_now(tmp_path):
    """One reply missed once (to the Lab panel's read, a run's) made the monitor leave the instrument alone, and the
    interlock that needs it then failed as one that can't be read. An interlock's readings are always asked."""
    from test_hardware import Bus
    lockin = Flaky("srs/sr830")
    set_resource_manager(Bus({"GPIB0::24::INSTR": Device("keithley/2400"), "GPIB0::8::INSTR": lockin}))
    try:
        rt = runtime(tmp_path, INTERLOCK_LAB)
        smu, m = rt.lab.instruments["smu"], rt.monitor
        smu.write("output", 1)
        smu.write("V", 0.5)
        m._last_interlock_check = -1e18
        m.tick()
        lockin.fail = 1
        _quietly(rt.lab.read, "lockin", "X")   # the Lab panel's read: no reply this once
        assert rt.lab.instruments["lockin"].health.fails == 1
        m._last_interlock_check = -1e18
        m.tick()
        assert not [a for a in m.alarms if a.kind == "interlock"], [a.text for a in m.alarms]
        assert smu.read("V") == pytest.approx(0.5)
    finally:
        set_resource_manager(None)


def test_stops_report_says_a_drive_it_couldnt_read_now_never_nothing_left_on(tmp_path):
    """Right after a Stop the stopped call may still be in the driver: the report's look at that instrument found it
    busy, left the drive out, and said "nothing is left on" of a source at 0.5 V with its output on."""
    from test_hardware import Bus
    dev = Slow("keithley/2400")
    set_resource_manager(Bus({"GPIB0::24::INSTR": dev}))
    try:
        rt = runtime(tmp_path, SMU_LAB)
        smu = rt.lab.instruments["smu"]
        smu.write("output", 1)
        smu.write("V", 0.5)
        assert any(line.startswith("smu.V") for line in rt.left_on(look=True))
        dev.slow = 4.0
        t = threading.Thread(target=lambda: _quietly(smu.read, "V"), daemon=True)
        t.start()
        time.sleep(0.3)
        rt.stop("human (portal)")            # its wait ends; its call goes on in the driver
        t.join(2)
        left = rt.left_on(look=True)
        assert any("couldn't be read now" in line for line in left), left
    finally:
        dev.slow = 0.0
        set_resource_manager(None)


def test_switching_an_instrument_off_never_waits_behind_a_call_still_in_its_driver(tmp_path, board):
    """Closing a session queued its close behind the call a Stop had left in the driver, for that call's whole limit
    (30 s for a 34465a; 130 s for a card with a 60 s timeout)."""
    bus, dmm = board
    rt = runtime(tmp_path, LAB)
    assert wait_until(lambda: _answers(rt, "dmm", "V_dc"), 10)
    dmm.hung.set()
    t = threading.Thread(target=lambda: _answers(rt, "dmm", "V_dc"), daemon=True)
    t.start()
    assert dmm.inside.wait(5)
    rt.stop("human (portal)")
    t.join(3)
    t0 = time.monotonic()
    rt.lab.switch("dmm", False)              # a person switches it off in the Lab panel
    assert time.monotonic() - t0 < 3.0


def test_a_scan_whose_visa_call_never_returns_never_holds_the_lab(tmp_path, board, monkeypatch):
    """A Scan (a natural thing to try on a hung adapter) made raw VISA calls holding the lab: one that never returned
    held it for good, and no Stop ended it. The scan runs on a thread of its own; a Stop ends the wait for it."""
    from labharness import discovery
    rt = runtime(tmp_path, LAB)
    hang = threading.Event()

    def stuck_survey(*a, **k):
        if k.get("progress") is not None:
            k["progress"]("asking GPIB0::5::INSTR who it is")
        hang.wait(30)                         # a raw VISA call that never returns
        return [], None
    monkeypatch.setattr(discovery, "scan_survey", stuck_survey)
    out = {}
    t = threading.Thread(target=lambda: out.update(said=rt.scan()), daemon=True)
    t.start()
    try:
        assert wait_until(lambda: rt.lock.owner()[0] is not None, 5)
        rt.stop("human (portal)")
        t.join(3)
        assert not t.is_alive() and "stopped by a person" in out.get("said", ""), out
        assert "GPIB0::5::INSTR" in out["said"] and rt.lock.owner()[0] is None
    finally:
        hang.set()


def test_bring_to_safe_waits_for_a_stopped_runs_own_way_to_safe_rather_than_ramp_beside_it(tmp_path, board,
                                                                                            monkeypatch):
    """Bring to safe went beside any work still holding the lab after 15 s: a stopped run's own ramp to safe (an
    8 V gate at 0.5 V/s takes 16 s) then had a second ramp of the same drive beside it. Work that is on its way, not
    stuck on an instrument, is waited for."""
    from labharness.agent import runtime as rtmod
    monkeypatch.setattr(rtmod, "RESCUE_WAIT_S", 0.5)
    rt = runtime(tmp_path, LAB)

    def on_its_way():   # the stopped run's own way to safe: going to safe (gate.going_to_safe), its exchanges short
        with rt.lock, rt.gate._to_safe_counted():
            time.sleep(2.0)
    threading.Thread(target=on_its_way, daemon=True).start()
    assert wait_until(lambda: rt.lock.owner()[0] is not None, 5)
    t0 = time.monotonic()
    lines = rt.bring_to_safe("human (portal)")
    assert time.monotonic() - t0 >= 1.5 and not any("went ahead beside" in line for line in lines), lines
