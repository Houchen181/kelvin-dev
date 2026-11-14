"""Program-wide reviews (2026-11-13) after the hung GPIB-USB field report: the controls a person recovers the lab with
never wait on stuck work, a stalled data folder, or one instrument that holds up the others."""
import threading
import time

import pytest

from labharness.adapters import BUSES, set_resource_manager
from test_field_test_2026_11_06 import runtime
from test_field_test_2026_11_13_hung_queue import LAB, board, stuck_work, wait_until  # noqa: F401 (the fixture)
from test_hardware import Bus, Device, Resource


def test_a_stop_goes_out_at_once_whatever_the_queues_file_does(tmp_path, board, monkeypatch):
    """A Stop set the queue to pausing and wrote queue.json, under the queue's lock, before it stopped anything: on a
    data folder on a network share that stalled, the run went on driving for as long as the write took."""
    from labharness import scanqueue
    rt = runtime(tmp_path, LAB)
    real = scanqueue.os.replace

    def stalled(*a):   # a share that stalls on every write
        time.sleep(3.0)
        return real(*a)
    monkeypatch.setattr(scanqueue.os, "replace", stalled)
    with rt.queue.lock:
        rt.queue.state = "running"
    gen = rt.gate.stop_generation
    t0 = time.monotonic()
    rt.stop("human (portal)")
    assert rt.gate.stop_generation != gen and time.monotonic() - t0 < 1.5
    assert rt.queue.state == "pausing"


def test_hands_on_goes_beside_work_stuck_on_an_instrument(tmp_path, board, monkeypatch):
    """Hands on the sample waited up to 15 minutes behind work stuck on a hung adapter, sources and lasers on with
    hands on the sample: it now goes beside stuck work as Bring to safe does, and says so."""
    from labharness.agent import runtime as rtmod
    monkeypatch.setattr(rtmod, "HANDS_WAIT_S", 0.2)
    monkeypatch.setattr(rtmod, "RESCUE_WAIT_S", 0.5)
    rt = runtime(tmp_path, LAB)
    assert wait_until(lambda: rt.lab.instruments["cryo"].enabled, 5)
    stuck_work(rt, seconds=8.0)                       # an exchange that hasn't come back, holding the lab
    t0 = time.monotonic()
    rt.hands_on("human (portal)")
    assert wait_until(lambda: [e for e in rt.gate.log.events() if e["type"] == "hands_on"], 5)
    [event] = [e for e in rt.gate.log.events() if e["type"] == "hands_on"]
    assert event.get("beside") and time.monotonic() - t0 < 5.0


def test_a_queue_whose_thread_meets_an_error_pauses_saying_so(tmp_path, board, monkeypatch):
    """The queue's thread had no guard: an error Kelvin didn't foresee left the queue "running" for good, and with it
    no Start, no sign-in, no Scan, and no idle check putting a laser left on to safe."""
    rt = runtime(tmp_path, LAB)

    def broken(*a, **k):
        raise RuntimeError("something nobody foresaw")
    monkeypatch.setattr(rt, "_queue_loop", broken)
    with rt.queue.lock:
        rt.queue.state = "running"
    rt._queue_guarded({}, "test")
    assert rt.queue.state == "paused" and "something nobody foresaw" in rt.queue.reason
    assert rt.running_now() is None


def test_one_instrument_held_never_keeps_anothers_output_from_going_to_safe(tmp_path, monkeypatch):
    """Drives went to safe one instrument after another: one whose connection a hung call held kept a second source
    on for its whole wait (two minutes, up to ten behind a transfer). Each instrument now goes to safe side by side."""
    from labharness import instrument
    monkeypatch.setattr(instrument, "HELD_S", 3.0)
    set_resource_manager(Bus({"GPIB0::24::INSTR": Device("keithley/2400"), "GPIB0::25::INSTR": Device("keithley/2400")}))
    try:
        rt = runtime(tmp_path, """lab: Bench
instruments:
  smu1:
    card: keithley/2400
    address: GPIB0::24::INSTR
  smu2:
    card: keithley/2400
    address: GPIB0::25::INSTR
""")
        one, two = rt.lab.instruments["smu1"], rt.lab.instruments["smu2"]
        for inst in (one, two):
            inst.write("output", 1)
            inst.write("V", 0.5)
        held, release = threading.Event(), threading.Event()

        def holder():   # an exchange with smu1 that doesn't let go of its connection
            with one.backend.lock:
                held.set()
                release.wait(10)
        threading.Thread(target=holder, daemon=True).start()
        assert held.wait(5)
        out = {}
        t = threading.Thread(target=lambda: out.update(r=rt.gate.abort_to_safe(
            ["smu1.V", "smu1.output", "smu2.V", "smu2.output"], by="test", hold=False)), daemon=True)
        t.start()
        assert wait_until(lambda: two.spec("output").distance(two.read("output")) == 0, 2.0), \
            "smu2 waited behind smu1"
        assert t.is_alive()                          # smu1's own way to safe still waits for its connection
        release.set()
        t.join(10)
        assert not t.is_alive()
    finally:
        set_resource_manager(None)


def test_a_reconnect_whose_first_call_never_returns_is_never_tried_again_beside_it(monkeypatch):
    """Each reconnect of an instrument whose driver opened a session but never returned from its first call left one
    more thread stuck in the driver, for good: on a wedged adapter, over a hundred an hour, overnight. A reconnect is
    never tried again while the last one's call is still in the driver: refused at once, saying so."""
    from labharness import adapters, builtin_registry
    from labharness.adapters import AdapterError, VisaBackend
    monkeypatch.setattr(adapters, "CALL_FLOOR_S", 1.0)
    monkeypatch.setattr(adapters, "CALL_GRACE_S", 0.5)
    wedged, forever = threading.Event(), threading.Event()

    class Wedgeable(Resource):
        def _gate(self):
            if wedged.is_set():
                forever.wait(30)

        def query(self, cmd, delay=None):
            self._gate()
            return super().query(cmd, delay)

        def write(self, cmd):
            self._gate()
            return super().write(cmd)

        def clear(self):
            self._gate()

        def close(self):
            self._gate()

    class WBus(Bus):
        def open_resource(self, name, **kw):
            return Wedgeable(self.devices[name])   # the open itself still works

    set_resource_manager(WBus({"GPIB0::24::INSTR": Device("keithley/2400")}))
    try:
        b = VisaBackend("smu", builtin_registry().get("keithley/2400"), "GPIB0::24::INSTR")
        b.res.timeout = 200
        wedged.set()
        with pytest.raises(Exception):
            b.query("*IDN?")
        with pytest.raises(Exception):
            b.reopen()                               # opens, then its first call never comes back
        assert b._stuck_open is not None
        t0 = time.monotonic()
        with pytest.raises(AdapterError, match="still stuck"):
            b.reopen()                               # refused at once: no new thread in the driver
        assert time.monotonic() - t0 < 0.5
    finally:
        forever.set()
        set_resource_manager(None)
        BUSES.hung.clear()
