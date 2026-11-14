"""The instrument layer, re-checked after the merge (2026-11-14): no look, Try, close, way to safe or connect waits on a
maker's library or an adapter that never returns, and a person's Stop ends what it should."""
import threading
import time

import pytest

from labharness import adapters, drivers, instrument
from labharness.homelab import builtin_registry
from labharness.instrument import Busy, Instrument, Stopped, patience, unstoppable
from labharness.labconfig import InstrumentConfig


class WedgedCamera(drivers.DriverBackend):
    """A camera's library (call_s None, as every camera's) whose read never comes back until freed."""
    call_s = None

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.free = threading.Event()

    def get(self, path):
        self.free.wait(30)    # Andor SDK3's temperature read that never comes back
        return -70.0

    def identity(self):
        return "Andor,Zyla,1"


def _inst(backend, card_id="andor/scmos", address="andor://0"):
    card = builtin_registry().get(card_id).bind({})
    backend.card = card
    return Instrument(backend.name, card, backend, InstrumentConfig(backend.name, card_id, address))


def test_a_look_at_a_camera_whose_library_never_returns_gives_up(monkeypatch):
    """A look (the monitor's, a run's record of an instrument it doesn't use) went straight into a camera's library on
    the looking thread: one that never returned held it for good, and a run on another instrument never started. It
    goes through the connection's own thread now, and the look gives up after LOOK_CALL_S."""
    monkeypatch.setattr(drivers, "LOOK_CALL_S", 0.5)
    cam = WedgedCamera("cam", builtin_registry().get("andor/scmos"), "andor://0")
    inst = _inst(cam)
    try:
        t0 = time.monotonic()
        with patience(1.0), unstoppable():   # the monitor's look
            with pytest.raises(Busy, match="a look waits no longer"):
                inst.read("temperature")
        assert time.monotonic() - t0 < 3.0
    finally:
        cam.free.set()


def test_a_stop_ends_a_runs_wait_on_a_camera_call():
    """A run's call into a camera's library (call_s None) could not be ended by a Stop: it ran on the run's thread."""
    cam = WedgedCamera("cam", builtin_registry().get("andor/scmos"), "andor://0")
    inst = _inst(cam)
    out = {}

    def run():
        try:
            inst.read("temperature")
        except Exception as e:
            out["error"] = e
    t = threading.Thread(target=run, daemon=True)
    try:
        t.start()
        time.sleep(0.4)
        t0 = time.monotonic()
        inst.stops.pulse()
        t.join(3)
        assert not t.is_alive() and isinstance(out.get("error"), Stopped) and time.monotonic() - t0 < 1.0
    finally:
        cam.free.set()


def test_a_driver_call_that_calls_the_driver_again_never_waits_on_itself():
    """A maker's get that asks the driver again (query inside a call) is made there and then, on the connection's own
    thread, never queued behind itself."""
    class Nested(drivers.DriverBackend):
        call_s = None

        def get(self, path):
            if path == "outer":
                return float(self.query("GET inner"))   # into the driver again, from inside a call
            return 2.5

        def identity(self):
            return "Fake,Nested,1"
    b = Nested("n", builtin_registry().get("andor/scmos"), "andor://0")
    t0 = time.monotonic()
    assert float(b.query("GET outer")) == pytest.approx(2.5)
    assert time.monotonic() - t0 < 1.0


def test_a_cards_try_gives_up_an_open_that_never_returns(monkeypatch):
    """A card's Try opened a maker's driver on its own thread with no deadline, holding the lab: one whose open never
    returned held the lab for good. It opens as the lab does now (lab.open_within), and gives up past its deadline."""
    from labharness import cardtry, lab as labmod
    free = threading.Event()

    class OpenNeverReturns(drivers.DriverBackend):
        def __init__(self, name, card, address, module=None):
            super().__init__(name, card, address)
            free.wait(30)   # pylon's Open(), Andor's AT_Open, LabOne's connectDevice... that never returns

        def identity(self):
            return "Zurich Instruments,MFLI,dev1234"
    monkeypatch.setitem(drivers.DRIVERS, "zhinst", OpenNeverReturns)
    monkeypatch.setattr(labmod, "open_deadline", lambda card, address: 0.5)
    try:
        t0 = time.monotonic()
        with pytest.raises(labmod.OpenTimedOut):
            cardtry.try_card(builtin_registry().get("zhinst/mfli"), "zhinst://dev1235")
        assert time.monotonic() - t0 < 3.0
    finally:
        free.set()


def test_the_way_to_safe_waits_behind_a_call_left_in_the_driver_held_s_at_most(monkeypatch):
    """A drive on its way to safe (no Stop ends it) waited behind a call a Stop had left in the driver for that call's
    whole limit (ten minutes for a maker's driver): HELD_S at most now, then Busy, nothing sent."""
    monkeypatch.setattr(instrument, "HELD_S", 0.5)

    class Hanging(drivers.DriverBackend):
        call_s = 30.0

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.free, self.sets = threading.Event(), []

        def get(self, path):
            self.free.wait(30)   # a MultiVu / LabOne call that never comes back
            return 1.0

        def set(self, path, value):
            self.sets.append((path, value))

        def identity(self):
            return "Zurich Instruments,MFLI,dev1234"
    backend = Hanging("lockin", builtin_registry().get("zhinst/mfli"), "zhinst://dev1234")
    inst = _inst(backend, "zhinst/mfli", "zhinst://dev1234")
    t = threading.Thread(target=lambda: pytest.raises(Exception, inst.read, "R"), daemon=True)
    try:
        t.start()
        time.sleep(0.4)
        inst.stops.pulse()              # the run lets go at once; its call stays in the driver
        t.join(2)
        t0 = time.monotonic()
        with unstoppable():             # its way to safe
            with pytest.raises(Busy):
                inst.write("output", 0)
        assert time.monotonic() - t0 < 2.0 and backend.sets == []   # nothing sent beside the call
    finally:
        backend.free.set()


def test_closing_a_connection_whose_library_never_returns_never_holds_the_lab(monkeypatch):
    """Closing at once went into a maker's library on the caller's thread, under the lab's lock (a camera's Close on a
    changed address): it is closed on a thread of its own now, waited for a moment at most."""
    from labharness import lab as labmod
    monkeypatch.setattr(labmod, "ENDED_CLOSE_S", 0.5)
    free = threading.Event()

    class CloseNeverReturns:
        def close(self):
            free.wait(30)
    try:
        t0 = time.monotonic()
        labmod._close_now(CloseNeverReturns())
        assert time.monotonic() - t0 < 2.0
    finally:
        free.set()


def test_a_stop_ends_the_wait_for_a_connect():
    """A connect (a person's switch on, a reconnect) waited its whole open deadline (up to minutes) under the lab's
    lock, whatever a person pressed: a Stop ends the wait now, the open going on unwaited and closed if it opens."""
    from labharness import lab as labmod
    from labharness.clock import Stops
    free, closed = threading.Event(), []

    class Late:
        def close(self):
            closed.append(True)

    def factory(name, card, address):
        free.wait(10)
        return Late()
    stops = Stops()
    card = builtin_registry().get("keithley/2400")
    threading.Timer(0.3, stops.pulse).start()
    t0 = time.monotonic()
    with pytest.raises(labmod.OpenTimedOut, match="pressed Stop"):
        labmod.open_within(factory, "smu", card, "GPIB0::24::INSTR", 30.0, stops=stops)
    assert time.monotonic() - t0 < 2.0
    free.set()
    end = time.monotonic() + 3
    while not closed and time.monotonic() < end:
        time.sleep(0.02)
    assert closed   # what it opened after all is closed, unused


def test_an_instrument_waiting_behind_anothers_long_read_on_a_prologix_is_busy_not_hung(monkeypatch):
    """Behind one Prologix adapter, an exchange waited for another instrument's long read inside its own call: past its
    session's limit it was taken for a hung driver, its session set aside and the bus flagged. It waits half of what its
    call has left now, then is busy, and answers once the other's read is done."""
    from fake_prologix import Adapters, says
    from labharness import prologix
    keithley = b"KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30   Mar 17 2006 09:29:29/A02  /K/J\n"
    monkeypatch.setattr(adapters, "CALL_FLOOR_S", 1.0)
    monkeypatch.setattr(adapters, "CALL_GRACE_S", 0.5)
    fakes = Adapters()
    monkeypatch.setattr(prologix, "OPEN", fakes.open)
    fakes.add("COM7", {24: says(keithley), 5: says(keithley)})
    b = adapters.VisaBackend("smu_b", builtin_registry().get("keithley/2400"), "prologix://COM7/5", prologix.MANAGER)
    try:
        b.res.timeout = 300
        assert b.query("*IDN?").strip().startswith("KEITHLEY")
        adapter = prologix._adapters["COM7"]
        done = threading.Event()

        def long_transfer():   # instrument A's deep-memory waveform holds the adapter for 3 s
            with adapter.lock:
                done.wait(3.0)
        threading.Thread(target=long_transfer, daemon=True).start()
        time.sleep(0.1)
        with pytest.raises(Busy):
            b.query("*IDN?")
        assert not b.broken and b.hung_at is None
        done.set()
        time.sleep(0.3)
        assert b.query("*IDN?").strip().startswith("KEITHLEY")
    finally:
        b.close()


def test_an_open_a_stop_let_go_of_is_never_opened_beside_and_is_closed_once_it_opens():
    """A reconnect whose open a Stop let go of left the open in the driver: the next connect opened another beside it,
    and what the first opened was never closed."""
    from test_hardware import Bus, Device, Resource
    gate, opened = threading.Event(), []

    class Slow(Bus):
        hold = False

        def open_resource(self, name, **kw):
            if self.hold:
                gate.wait(10)
            r = Resource(self.devices[name])
            opened.append(r)
            r.closed = False
            real = r.close

            def close():
                r.closed = True
                return real()
            r.close = close
            return r
    bus = Slow({"GPIB0::24::INSTR": Device("keithley/2400")})
    adapters.set_resource_manager(bus)
    try:
        b = adapters.VisaBackend("smu", builtin_registry().get("keithley/2400"), "GPIB0::24::INSTR")
        bus.hold = True
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        with adapters.stoppable(stop.is_set):
            with pytest.raises(Stopped):
                b.reopen()
        assert b._stuck_open is not None
        t0 = time.monotonic()
        with pytest.raises(adapters.AdapterError, match="still stuck"):
            b.reopen()                        # never one more beside it
        assert time.monotonic() - t0 < 0.5
        gate.set()                            # the first open returns after all: closed, unused
        end = time.monotonic() + 3
        while not (opened and opened[-1].closed) and time.monotonic() < end:
            time.sleep(0.02)
        assert opened[-1].closed
    finally:
        gate.set()
        adapters.set_resource_manager(None)


def test_a_pending_stop_never_cancels_a_sessions_close():
    """Closing a session with a Stop pending on the thread took the close back: the session was never closed."""
    from test_hardware import Device, Resource
    raw = Resource(Device("keithley/2400"))
    closed = []
    raw.close = lambda: closed.append(True)
    session = adapters.Session(raw, adapters.Courier("smu"))
    with adapters.stoppable(lambda: True):    # a person's Stop already pressed
        session.discard()
    assert closed == [True]


def test_a_modbus_exchange_a_stop_ends_at_once():
    """A Modbus exchange went into the USB-RS485 driver on the run's own thread: one that stalled held the run with no
    Stop. It goes through the connection's own thread now."""
    from labharness.modbus import ModbusInstrument
    free = threading.Event()

    def stalled(frame):
        free.wait(10)
        return b""
    m = ModbusInstrument("pump", builtin_registry().get("keithley/2400"), "modbus://COM9?unit=1",
                         module={"transport": stalled, "rtu": True})
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    try:
        t0 = time.monotonic()
        with adapters.stoppable(stop.is_set):
            with pytest.raises(Stopped):
                m._exchange(bytes([3, 0, 0, 0, 1]))
        assert time.monotonic() - t0 < 1.5
    finally:
        free.set()


def test_closing_visa_as_kelvin_quits_never_waits_on_a_wedged_visa(monkeypatch):
    """Closing VISA as Kelvin quit took its lock with no limit and closed it on the quitting thread."""
    free = threading.Event()

    class Wedged:
        def close(self):
            free.wait(10)
    monkeypatch.setattr(adapters, "CLOSE_CALL_S", 0.5)
    monkeypatch.setattr(adapters, "_rm", Wedged())
    monkeypatch.setattr(adapters, "_rm_own", True)
    try:
        t0 = time.monotonic()
        adapters.close_resource_manager()
        assert time.monotonic() - t0 < 2.0
    finally:
        free.set()
