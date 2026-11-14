"""Makers' links and drivers never hold Kelvin for good (review of the hung-bus fix, 2026-11-13): a GPIB-USB stuck in its
driver, a controller's DLL that never returns from opening, a USB-serial driver that stalls, a server that never answers
a request but keeps talking, a library lock behind a call that hung. Each wait is bounded, and said."""
import sys
import threading
import time
import types

import pytest

from fake_prologix import Adapters, says
from labharness import builtin_registry, positioners, prologix, spectra
from labharness.adapters import AdapterError, NoAnswer, stoppable
from labharness.instrument import Stopped

KEITHLEY = b"KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30   Mar 17 2006 09:29:29/A02  /K/J\n"


def finishes(fn, seconds):
    """fn() on a thread: (it ended within `seconds`, what it returned or raised)."""
    out = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as e:   # noqa: BLE001 - what it raised is what is checked
            out["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    return not t.is_alive(), out


# ---------------------------------------------------------------------- a Prologix adapter stuck in its driver

@pytest.fixture
def adapters(monkeypatch):
    fakes = Adapters()
    monkeypatch.setattr(prologix, "OPEN", fakes.open)
    monkeypatch.setattr(prologix, "MARGIN_S", 0.05)
    monkeypatch.setattr(prologix, "LOCK_S", 0.3)
    monkeypatch.setattr(prologix, "CLOSE_JOIN_S", 0.3)
    yield fakes
    prologix._adapters.clear()


def wedge(fakes, adapter, frees_on_close=True):
    """The adapter's link from now on: a send that never returns (a GPIB-USB whose bus hung stops taking bytes),
    until its port is closed (which ends a call stuck in it, as pyserial's does) or, frees_on_close=False, never."""
    real, stuck, forever = fakes.open, threading.Event(), threading.Event()

    def opening(name, seconds):
        link = real(name, seconds)
        if name != adapter:
            return link
        closed, send, close = threading.Event(), link.send, link.close

        def stuck_send(data):
            if stuck.is_set():
                while not forever.is_set():
                    if closed.wait(0.05) and frees_on_close:
                        raise OSError("the port was closed")
            return send(data)

        def closing():
            closed.set()
            close()
        link.send, link.close = stuck_send, closing
        return link
    return opening, stuck, forever


def test_a_clear_frees_a_prologix_exchange_stuck_in_its_driver_and_goes_on(adapters, monkeypatch):
    """Clear the bus waited with no end for the adapter's one exchange at a time, held by an exchange stuck in its
    driver, and held the lab with it. It closes the stuck connection beside that exchange (which then fails), and
    sends its ++ifc on a new one."""
    fake = adapters.add("COM5", {24: says(KEITHLEY)})
    opening, stuck, forever = wedge(adapters, "COM5")
    monkeypatch.setattr(prologix, "OPEN", opening)
    try:
        res = prologix.open_resource("prologix://COM5/24")
        assert res.query("*IDN?").startswith("KEITHLEY")
        stuck.set()
        threading.Thread(target=lambda: finishes(lambda: res.write("*RST"), 30), daemon=True).start()
        time.sleep(0.2)                               # the exchange is in the driver, holding the adapter
        stuck.clear()                                 # (the new connection's sends go through)
        ended, out = finishes(res.adapter.interface_clear, 5)
        assert ended and "error" not in out, out
        assert fake.commands[-1] == "ifc" and fake.links == 2   # on a connection made anew
        res.close()
    finally:
        forever.set()


def test_a_clear_says_so_when_the_stuck_call_never_lets_go(adapters, monkeypatch):
    adapters.add("COM5", {24: says(KEITHLEY)})
    opening, stuck, forever = wedge(adapters, "COM5", frees_on_close=False)
    monkeypatch.setattr(prologix, "OPEN", opening)
    try:
        res = prologix.open_resource("prologix://COM5/24")
        stuck.set()
        threading.Thread(target=lambda: finishes(lambda: res.write("*RST"), 30), daemon=True).start()
        time.sleep(0.2)
        ended, out = finishes(res.adapter.interface_clear, 5)
        assert ended and isinstance(out.get("error"), prologix.Stuck), out
        assert "unplug it" in str(out["error"])
    finally:
        forever.set()


def test_letting_go_of_a_stuck_prologix_never_holds_up_the_others(adapters, monkeypatch):
    """Closing the instrument behind a stuck GPIB-USB parked its close inside the lock every adapter's connections
    take: no Prologix, healthy or not, could be opened, cleared or asked its version again."""
    adapters.add("COM5", {24: says(KEITHLEY)})
    adapters.add("COM7", {5: says(KEITHLEY)})
    opening, stuck, forever = wedge(adapters, "COM5", frees_on_close=False)
    monkeypatch.setattr(prologix, "OPEN", opening)
    try:
        res = prologix.open_resource("prologix://COM5/24")
        stuck.set()
        threading.Thread(target=lambda: finishes(lambda: res.write("*RST"), 30), daemon=True).start()
        time.sleep(0.2)
        ended, _ = finishes(res.close, 3)            # a switch-off, a reconnect, a clear setting it aside
        assert ended and not prologix._adapters_lock.locked()
        ended, out = finishes(lambda: prologix.open_resource("prologix://COM7/5"), 3)
        assert ended and "value" in out, out
        assert out["value"].query("*IDN?").startswith("KEITHLEY")
        assert prologix.version_of("COM7").startswith("Prologix")
        out["value"].close()
    finally:
        forever.set()


def test_an_exchange_whose_instrument_was_closed_while_it_waited_is_never_sent(adapters):
    """An exchange waited for the adapter, then went out long after its caller had given up on it: a command sent
    late. One whose instrument's connection was closed meanwhile sends nothing."""
    fake = adapters.add("COM5", {24: says(KEITHLEY), 6: says(KEITHLEY)})
    res, other = prologix.open_resource("prologix://COM5/24"), prologix.open_resource("prologix://COM5/6")
    held, release = threading.Event(), threading.Event()

    def holder():
        with res.adapter.lock:
            held.set()
            release.wait(5)
    threading.Thread(target=holder, daemon=True).start()
    assert held.wait(2)
    out = {}
    t = threading.Thread(target=lambda: out.update(finishes(lambda: res.write("*RST"), 10)[1]), daemon=True)
    t.start()
    time.sleep(0.2)
    res.close()                                       # its caller gave up: the connection is let go of
    release.set()
    t.join(5)
    assert isinstance(out.get("error"), ConnectionError), out
    assert not any(b"*RST" in h for h in fake.instruments[24].heard)
    other.close()


def test_the_gpib_usb_never_waits_for_good_for_the_adapter_to_take_its_bytes(monkeypatch):
    """pyserial's flush() waits with no end while the adapter takes no bytes (its bus hung)."""
    monkeypatch.setattr(prologix, "SEND_S", 0.2)

    class Port:
        out_waiting = 12   # never taken

        def write(self, data):
            return len(data)
    ended, out = finishes(lambda: prologix._Serial(Port()).send(b"++ver\n"), 2)
    assert ended and isinstance(out.get("error"), TimeoutError), out


def test_a_reply_that_never_ends_ends_the_read(monkeypatch):
    """The read restarted its wait at each byte: an instrument left talking held the adapter for good."""
    monkeypatch.setattr(prologix, "READ_MAX_S", 0.3)

    class Talking:
        def send(self, data):
            pass

        def recv(self, seconds):
            time.sleep(0.01)
            return b"1,2,3,"   # never its END

        def drop_input(self):
            pass

        def close(self):
            pass
    a = prologix.Adapter("COM9")
    a.link, a.version = Talking(), "Prologix GPIB-USB Controller version 6.107"
    ended, out = finishes(lambda: a.read(prologix.Where("COM9", 5), 100), 3)
    assert ended and isinstance(out.get("error"), ConnectionError) and "kept sending" in str(out["error"]), out


# ---------------------------------------------------------------------- positioners: a controller's open that hangs

class FakeLink:
    """An ANC300's command line (positioners.Link's place)."""

    def __init__(self, target, pwd=None):
        self.closed = False

    def command(self, line, timeout=None):
        return ["attocube ANC300 controller version 1.1.0"] if line == "ver" else ["voltage = 30.000000 V"]

    def close(self):
        self.closed = True


def test_a_controller_whose_open_never_returns_holds_up_no_other_positioner(monkeypatch):
    """The lock every attocube controller's axes share was held across opening one: an ANC350 whose DLL never
    returned from its open blocked every other positioner's connect and every switch-off, with the lab held."""
    monkeypatch.setattr(positioners, "OPEN_WAIT_S", 0.5)
    forever = threading.Event()

    class Anc350Module:
        class ANC350:
            def __init__(self, idx):
                forever.wait(30)   # attocube's DLL never returns from opening a controller in a bad state

            def close(self):
                pass
    reg = builtin_registry()
    try:
        threading.Thread(target=lambda: finishes(lambda: positioners.ANC350(
            "x", reg.get("attocube/anc350").bind({"axis": "0"}), "anc350://0", module=Anc350Module), 30),
            daemon=True).start()
        time.sleep(0.2)
        assert not positioners._shared_lock.locked()
        ended, out = finishes(lambda: positioners.ANC300("z", reg.get("attocube/anc300").bind({"axis": "1"}),
                                                         "anc300://COM9", module=FakeLink), 2)
        assert ended and "value" in out, out
        assert finishes(out["value"].close, 2)[0]       # its switch-off
        ended, out = finishes(lambda: positioners.ANC350(   # another axis of the stuck controller: said, not waited for
            "y", reg.get("attocube/anc350").bind({"axis": "1"}), "anc350://0", module=Anc350Module), 3)
        assert ended and isinstance(out.get("error"), AdapterError) and "still opening" in str(out["error"]), out
    finally:
        forever.set()
        time.sleep(0.1)
        positioners._shared.clear()


def test_a_switch_off_never_waits_for_a_controllers_close_stuck_in_its_driver(monkeypatch):
    monkeypatch.setattr(positioners, "CLOSE_JOIN_S", 0.3)
    forever = threading.Event()

    class Hanging(FakeLink):
        def close(self):
            forever.wait(30)
    try:
        axis = positioners.ANC300("z", builtin_registry().get("attocube/anc300").bind({"axis": "1"}),
                                  "anc300://COM8", module=Hanging)
        ended, _ = finishes(axis.close, 2)
        assert ended and not positioners._shared_lock.locked()
    finally:
        forever.set()
        positioners._shared.clear()


# ---------------------------------------------------------------------- serial writes that a stalled driver holds

class Recorder:
    """pyserial, as far as opening a port: what it was opened with; the port isn't there."""
    PARITY_NONE, PARITY_EVEN, PARITY_ODD = "N", "E", "O"

    def __init__(self):
        self.opened = []

    def Serial(self, *args, **kwargs):   # noqa: N802 - pyserial's name
        self.opened.append(kwargs)
        raise OSError("could not open port (a test)")


def test_serial_writes_of_the_anc300_and_modbus_rtu_are_bounded(monkeypatch):
    """A USB-serial (RS-485) adapter's driver that stalls held a write with no end: pyserial with no write_timeout."""
    fake = Recorder()
    monkeypatch.setitem(sys.modules, "serial", fake)
    with pytest.raises(Exception):
        positioners.Link("COM9")
    from labharness.modbus import ModbusInstrument
    card = type("C", (), {"connection": {"timeout_ms": 300}, "idn_query": "*IDN?", "model": "CP2800",
                          "vendor": "Cryomech", "on_connect": []})()
    with pytest.raises(Exception):
        ModbusInstrument("comp", card, "modbus://COM6?unit=1")
    assert len(fake.opened) == 2 and all(kw.get("write_timeout") for kw in fake.opened), fake.opened


# ---------------------------------------------------------------------- Andor's spectrograph library, one call at a time

def test_a_call_into_andors_spectrograph_library_never_waits_for_good_behind_one_that_hung(monkeypatch):
    """A CCD's run reading its wavelengths waited with no end behind a spectrograph call that never returned."""
    monkeypatch.setattr(spectra, "SHAMROCK_WAIT_S", 0.3)
    held, release = threading.Event(), threading.Event()

    def stuck_call():
        with spectra.SHAMROCK_LOCK:
            held.set()
            release.wait(10)
    threading.Thread(target=stuck_call, daemon=True).start()
    assert held.wait(2)
    lib = types.SimpleNamespace(ShamrockGetNumberDevices=lambda n: spectra.SHAMROCK_OK)
    try:
        ended, out = finishes(lambda: spectra.shamrock_count(lib), 3)
        assert ended and isinstance(out.get("error"), AdapterError) and "still in another call" in str(out["error"])
        with stoppable(lambda: True):                 # and a person's Stop ends the wait at once
            t0 = time.monotonic()
            with pytest.raises(Stopped):
                spectra.shamrock_count(lib)
            assert time.monotonic() - t0 < 0.3 + 0.2
    finally:
        release.set()


def test_an_ocean_spectrum_is_waited_for_as_long_as_its_averages_take(monkeypatch):
    """One spectrum at the card's longest integration (600 s) was taken for a driver that won't return (600 s)."""
    from labharness.drivers import DRIVER_CALL_S

    class Spec:
        model, serial_number = "QEPro", "QEP0001"
        integration_time_micros_limits = (8000, 600_000_000)

        @classmethod
        def from_first_available(cls):
            return cls()

        def integration_time_micros(self, us):
            self.us = us
    sb = types.SimpleNamespace(Spectrometer=Spec, list_devices=lambda: [])
    o = spectra.OceanSpectrometer("spec", builtin_registry().get("ocean/spectrometer"), "seabreeze://", module=sb)
    assert o.call_s >= 600 * 1.5                     # until set: as long as it may be
    o.write("SET integration 0.1")
    assert o.call_s == DRIVER_CALL_S
    o.write("SET integration 360")
    o.write("SET averages 2")
    assert o.call_s >= 720


# ---------------------------------------------------------------------- servers that keep talking

def test_a_json_rpc_server_that_only_ever_sends_notifications_is_given_up_on_in_its_time():
    """The read for a request's answer passed over every other message: a server sending only notifications held the
    run's thread for good."""
    from labharness.jsonrpc import JsonRpcInstrument

    class Chatty:
        def create_connection(self, address, timeout):
            return self

        def settimeout(self, seconds):
            pass

        def sendall(self, data):
            pass

        def recv(self, n):
            time.sleep(0.01)
            return b'{"jsonrpc": "2.0", "method": "status", "params": [1]}\n'

        def close(self):
            pass
    card = builtin_registry().get("attocube/attodry-enspire")
    dry = JsonRpcInstrument("dry", card, "jsonrpc://10.0.0.40:9090", module=Chatty())
    dry.timeout = 0.3
    ended, out = finishes(lambda: dry.query("GET com.attocube.cryostat.interface.sample.getTemperature"), 3)
    assert ended and isinstance(out.get("error"), NoAnswer), out


def test_a_web_instruments_answer_that_never_ends_is_given_up_on(monkeypatch):
    from labharness import webapi
    monkeypatch.setattr(webapi, "MAX_BODY", 1000)

    class Endless:
        def read(self, n=-1):
            time.sleep(0.001)
            return b"x" * 100
    ended, out = finishes(lambda: webapi._body(Endless(), 0.2, "fs"), 3)
    assert ended and isinstance(out.get("error"), AdapterError) and "longer than" in str(out["error"]), out
    monkeypatch.setattr(webapi, "MAX_BODY", 10 ** 9)

    class Slow:
        def read(self, n=-1):
            time.sleep(0.05)
            return b"x"
    ended, out = finishes(lambda: webapi._body(Slow(), 0.0, "fs"), 8)   # its timeout and 5 s of margin
    assert ended and isinstance(out.get("error"), NoAnswer), out
    inst = webapi.HttpInstrument("fs", _web_card(), "http://10.0.0.5:20022", module=lambda *a, **k: None)
    assert hasattr(inst.lock, "_is_owned")   # re-entrant: a wait for it is one a person's Stop ends (Instrument._turn)


def _web_card():
    from labharness.cards import parse_card
    return parse_card({"card": 1, "id": "test/web", "model": "Web thing", "vendor": "test", "kind": "laser",
                       "parameters": {"state": {"label": "State", "kind": "measure", "type": "str",
                                                "get": "GET /v1/Status#laser.state"}}})


# ---------------------------------------------------------------------- readings that are acquisitions

def test_counts_and_pulsed_readouts_are_acquisitions_never_run_by_a_look():
    """The monitor's check that an instrument answers read its headline: on a Time Tagger a count for the whole
    integration time (up to an hour), on a QICK board an acquisition that plays the pulse once per rep."""
    from labharness.monitor import Monitor
    reg = builtin_registry()
    tagger, qick = reg.get("swabian/time-tagger"), reg.get("qick/rfsoc")
    assert all(tagger.params[n].acquires for n in ("rate", "signal", "reference", "contrast"))
    assert all(qick.params[n].acquires for n in ("i", "q", "magnitude", "phase"))
    for card, cheap in ((tagger, "integration"), (qick, "frequency")):
        read = []
        inst = types.SimpleNamespace(card=card, backend=types.SimpleNamespace(identity=lambda: ""),
                                     read=lambda p: read.append(p))
        Monitor._probe(None, inst)()
        assert read == [cheap], (card.id, read)
