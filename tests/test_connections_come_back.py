"""Connections that come back by themselves (review of 0.3.45, 4.1, 4.3, 4.4, 4.6).

- 4.1: a USB or LAN instrument that was power-cycled or replugged (or a USB-serial adapter replugged) kept a dead VISA
  session: VI_ERROR_CONN_LOST on every exchange, and only switching it off and on in the Lab panel brought it back.
  Now the session is marked broken, nothing is sent on it, and the monitor makes it anew once the instrument is back,
  on a thread of its own, checking it is the same instrument before anything else is sent.
- 4.3: reconnect_instrument said "connected" of an instrument whose connection had ended, without asking it.
- 4.4: a lost device in pyvisa-py's, pyserial's, pyusb's or a server's own words went by as no loss at all.
- 4.6: a serial *OPC? cut by a Stop answered the next query; a socket whose instrument had closed it was read for ever;
  query_waiting's write was outside the timeout handling; a failed start-up left a Prologix adapter's connection held.
"""
import errno
import threading
import time
import types
import urllib.error

import pytest

from labharness import adapters
from labharness.adapters import (AdapterError, LinkLost, NoAnswer, VisaBackend, lost_kind, names_device,
                                 session_dead)
from labharness.cards import parse_card
from labharness.eventlog import EventLog
from labharness.gate import SafetyGate, ScriptedApprover
from labharness.homelab import build_lab, builtin_registry
from labharness.instrument import InstrumentLost, Stopped
from labharness.monitor import Monitor

CONN_LOST, RSRC_NFOUND, NLISTENERS = -1073807194, -1073807343, -1073807265
IDN = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1083680,C27"
USB = "USB0::0x05E6::0x2400::04412345::INSTR"
LAN = "TCPIP0::10.0.0.5::inst0::INSTR"


def wait_until(check, what="", seconds=10.0):
    end = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < end, what
        time.sleep(0.02)


class VisaIOError(Exception):
    def __init__(self, code, text="VISA error"):
        super().__init__("%s (%d)" % (text, code))
        self.error_code = code


class Device:
    """The instrument behind the address: whether it is there, who it answers as, and what reached it."""

    def __init__(self, idn=IDN):
        self.idn, self.on, self.sent = idn, True, []


class Res:
    """A VISA session to it, as NI-VISA and pyvisa-py give one: dead for good once the device went away under it."""

    def __init__(self, device):
        self.device, self.dead, self.closed = device, False, False
        self.timeout, self.read_termination, self.write_termination = 3000, "\n", "\n"
        self.out = []

    def _check(self):
        if self.dead or self.closed:
            raise VisaIOError(CONN_LOST, "The connection for the given session has been lost")

    def write(self, cmd):
        self._check()
        self.device.sent.append(cmd)
        if cmd.endswith("?"):
            self.out.append(self.device.idn if cmd == "*IDN?" else "1.0")

    def read(self):
        self._check()
        if not self.out:
            raise TimeoutError("VI_ERROR_TMO")
        return self.out.pop(0)

    def query(self, cmd, delay=None):
        self.write(cmd)
        return self.read()

    def clear(self):
        self._check()
        self.out = []

    def close(self):
        self.closed = True


class RM:
    def __init__(self, device, slow=0.0):
        self.device, self.sessions, self.slow = device, [], slow

    def open_resource(self, name):
        if self.slow:
            time.sleep(self.slow)
        if not self.device.on:
            raise VisaIOError(RSRC_NFOUND, "the requested device is not present")
        res = Res(self.device)
        self.sessions.append(res)
        return res

    def power_off(self):   # switched off, or its USB cable pulled: every session to it dies for good
        self.device.on = False
        for res in self.sessions:
            res.dead = True


def a_lab(monkeypatch, address=USB, card="keithley/2400", device=None, slow=0.0):
    device = device or Device()
    rm = RM(device, slow)
    monkeypatch.setattr(adapters, "_rm", rm)
    lab = build_lab('lab: T\ninstruments:\n  smu: { card: %s, address: "%s" }\n' % (card, address))
    return lab, rm, device


def watching(lab, tmp_path, in_use=None):
    gate = SafetyGate(lab, ScriptedApprover([]), EventLog(tmp_path / "events.jsonl", lab.clock))
    told = []
    m = Monitor(lab, gate, lambda kind, title, text="": told.append((kind, title, text)), in_use=in_use)
    return m, told


def due(inst):
    """Its next try is due now (Health.resting's schedule, without waiting it out here)."""
    inst.health.lost_at -= 1000


def settled(m, name="smu"):
    wait_until(lambda: name not in m._reopening, "the reconnector never finished")


# ---------------------------------------------------------------------- 4.1: a session that died is made anew

def test_a_session_that_died_is_made_anew_by_itself_once_the_instrument_is_back(tmp_path, monkeypatch):
    lab, rm, device = a_lab(monkeypatch)
    inst = lab.instruments["smu"]
    assert inst.query_raw("*IDN?") == IDN and len(rm.sessions) == 1
    inst._last["V"] = 2.0                                                     # what Kelvin set on it
    m, told = watching(lab, tmp_path)
    rm.power_off()
    with pytest.raises(InstrumentLost):
        inst.query_raw("*IDN?")
    assert inst.backend.broken == "VI_ERROR_CONN_LOST" and inst.health.gone and inst.health.fails == 1
    sent = len(device.sent)
    with pytest.raises(InstrumentLost, match="connects to it again by itself once it is back"):
        inst.query_raw("*IDN?")                                               # refused at once, nothing sent
    assert len(device.sent) == sent and inst.health.fails == 1                # nor counted: the schedule holds
    m.tick()
    alarm = next(a for a in m.alarms if a.key == "smu" and a.kind == "silent")
    assert any("connects to it again by itself once it is back, checking it is the same instrument" in t[2]
               for t in told), told
    due(inst)                                                                 # still off: tried, and backed off
    m.tick()
    settled(m)
    assert len(rm.sessions) == 1 and inst.health.fails == 2 and inst.health.resting() is not None
    m.tick()
    settled(m)
    assert inst.health.fails == 2                                             # not before its time
    device.on = True                                                          # back, at its power-on settings
    due(inst)
    m.tick()
    settled(m)
    assert len(rm.sessions) == 2 and not inst.backend.broken and inst.health.fails == 0
    assert inst._last == {} and inst.health.reopened_as == IDN               # what Kelvin had set: forgotten
    assert device.sent[sent:] == ["*IDN?"]                                    # asked who it is, nothing else
    m.tick()
    assert alarm.cleared is not None
    assert alarm.cleared_text.startswith("smu is connected again after") and "it answered as itself" in \
        alarm.cleared_text and "check its outputs" in alarm.cleared_text, alarm.cleared_text
    assert inst.query_raw(":SOUR:VOLT?") == "1.0"                             # and used as before


def test_another_instrument_answering_there_is_sent_nothing(tmp_path, monkeypatch):
    lab, rm, device = a_lab(monkeypatch, address=LAN)
    inst = lab.instruments["smu"]
    m, _ = watching(lab, tmp_path)
    rm.power_off()
    with pytest.raises(InstrumentLost):
        inst.query_raw("*IDN?")
    device.idn, device.on = "Rohde&Schwarz,SMB100A,1406.6000k02/104,4.30", True   # DHCP gave it to another one
    sent = len(device.sent)
    due(inst)
    m.tick()
    settled(m)
    assert device.sent[sent:] == ["*IDN?"]                                    # asked who it is: nothing more
    assert inst.backend.broken.startswith("answers as 'Rohde&Schwarz,SMB100A")
    assert "answers again, but not as itself" in inst.health.lost_text and rm.sessions[-1].closed
    with pytest.raises(InstrumentLost):
        inst.query_raw(":SOUR:VOLT?")
    assert device.sent[sent:] == ["*IDN?"]


def test_the_lab_decides_whether_it_is_the_same_instrument(tmp_path, monkeypatch):
    """The reopen asks lab.identity_problem, where the serial number is checked (6.2): one it refuses is never used."""
    from labharness import lab as labmod
    lab, rm, device = a_lab(monkeypatch)
    inst = lab.instruments["smu"]
    rm.power_off()
    with pytest.raises(InstrumentLost):
        inst.query_raw("*IDN?")
    device.on, device.idn = True, IDN.replace("1083680", "4120001")          # another 2400, same model
    monkeypatch.setattr(labmod, "identity_problem",
                        lambda i, idn: None if "1083680" in idn else "its serial number isn't 1083680")
    with pytest.raises(InstrumentLost):
        inst.reopen()
    assert inst.backend.broken and "serial number" in inst.health.lost_text
    device.idn = IDN
    assert inst.reopen() == IDN and not inst.backend.broken


def test_a_gpib_instrument_nothing_listens_at_keeps_its_session(tmp_path, monkeypatch):
    lab, rm, device = a_lab(monkeypatch, address="GPIB0::24::INSTR")
    inst = lab.instruments["smu"]
    session = rm.sessions[-1]
    session.write = lambda cmd: (_ for _ in ()).throw(VisaIOError(NLISTENERS, "No listeners"))
    with pytest.raises(InstrumentLost):
        inst.query_raw("*IDN?")
    assert inst.health.gone and not inst.backend.broken                       # GPIB's session outlives it
    del session.write                                                         # switched on again
    assert inst.query_raw("*IDN?") == IDN and len(rm.sessions) == 1


def test_a_card_without_identity_comes_back_by_itself_only_where_its_address_names_it(tmp_path, monkeypatch):
    assert names_device(USB) and names_device("USB0::0x1AB1::0x04CE::DS1ZA170000000::0::INSTR")
    assert not names_device(LAN) and not names_device("ASRL3::INSTR") and not names_device("GPIB0::5::INSTR")
    card = parse_card({"card": 1, "id": "test/no-idn", "model": "Plain source", "vendor": "test", "kind": "source",
                       "parameters": {"V": {"label": "V", "kind": "measure", "unit": "V", "get": "VOLT?"}}})
    reg = builtin_registry()
    reg.put(card)
    for address, by_itself in ((LAN, False), (USB, True)):
        device = Device()
        rm = RM(device)
        monkeypatch.setattr(adapters, "_rm", rm)
        lab = build_lab('lab: T\ninstruments:\n  src: { card: test/no-idn, address: "%s" }\n' % address,
                        registry=reg)
        inst = lab.instruments["src"]
        rm.power_off()
        with pytest.raises(InstrumentLost):
            inst.read("V")
        device.on = True
        m, told = watching(lab, tmp_path)
        m.tick()
        assert any(("by itself once it is back" if by_itself else "switch it off and on in the Lab panel") in t[2]
                   for t in told), (address, told)
        due(inst)
        m.tick()
        settled(m, "src")
        assert bool(inst.backend.broken) is not by_itself, address
        if not by_itself:                                                     # a person's reconnect does it
            assert lab.reconnect("src").endswith("its connection made anew (it answered as itself)")
            assert not inst.backend.broken and inst.read("V") == pytest.approx(1.0)


def test_making_a_connection_anew_never_holds_up_the_look_at_the_lab(tmp_path, monkeypatch):
    lab, rm, device = a_lab(monkeypatch, address=LAN)
    inst = lab.instruments["smu"]
    m, _ = watching(lab, tmp_path)
    rm.power_off()
    with pytest.raises(InstrumentLost):
        inst.query_raw("*IDN?")
    device.on, rm.slow = True, 2.0                                            # its connection takes seconds to open
    due(inst)
    t0 = time.monotonic()
    m.tick()
    assert time.monotonic() - t0 < 1.0 and "smu" in m._reopening
    m.tick()                                                                  # one try at a time
    settled(m)
    assert len(rm.sessions) == 2 and not inst.backend.broken


def test_an_instrument_the_work_holds_is_left_to_it(tmp_path, monkeypatch):
    lab, rm, device = a_lab(monkeypatch)
    inst = lab.instruments["smu"]
    m, _ = watching(lab, tmp_path, in_use=lambda name: True)
    rm.power_off()
    with pytest.raises(InstrumentLost):
        inst.query_raw("*IDN?")
    device.on = True
    due(inst)
    m.tick()
    assert "smu" not in m._reopening and len(rm.sessions) == 1


# ---------------------------------------------------------------------- 4.3: reconnect never says "connected" unchecked

@pytest.fixture
def runtime_of(tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime

    def make(lab):
        rt = LabRuntime(lab, ScriptedApprover([]), tmp_path / "data")
        rt.home = None
        return rt
    return make


def test_reconnect_makes_a_dead_session_anew_and_asks_it_first(monkeypatch, runtime_of):
    import labharness.homelab as homelab
    monkeypatch.setattr(homelab, "card_now", lambda *a, **k: None)           # its card is as it was
    lab, rm, device = a_lab(monkeypatch)
    inst = lab.instruments["smu"]
    rt = runtime_of(lab)
    rm.power_off()
    with pytest.raises(InstrumentLost):
        inst.query_raw("*IDN?")
    said = rt.reconnect_instrument("smu")
    assert said.startswith("smu is still not connected:") and "connects to it again by itself" in said, said
    device.on = True
    said = rt.reconnect_instrument("smu")
    assert said.startswith("smu is connected now, with its connection made anew (it answered as itself)"), said
    assert len(rm.sessions) == 2 and not inst.backend.broken
    said = rt.reconnect_instrument("smu")                                     # nothing to make anew: asked
    assert said.startswith("smu is connected, with its card as it is now") and len(rm.sessions) == 2, said
    rm.power_off()                                                            # off again, nothing tried it since
    device.on = True
    rm.sessions[-1].dead = True
    said = rt.reconnect_instrument("smu")                                     # asked: its session had died
    assert "its connection made anew" in said and len(rm.sessions) == 3, said


def test_reconnect_brings_an_instrument_out_of_step_back(monkeypatch, runtime_of):
    import labharness.homelab as homelab
    monkeypatch.setattr(homelab, "card_now", lambda *a, **k: None)
    lab, rm, device = a_lab(monkeypatch, address=LAN)
    inst = lab.instruments["smu"]
    rt = runtime_of(lab)
    inst.backend.owed = True
    rm.sessions[-1].out += ["+2.5E+00", "+1.0E-03", "0"]                      # stale replies in front of its own
    real = rm.sessions[-1].clear
    rm.sessions[-1].clear = lambda: None                                      # a clear that didn't drop it
    with pytest.raises(Exception, match="out of step"):
        inst.query_raw(":SOUR:VOLT?")
    rm.sessions[-1].clear = real
    said = rt.reconnect_instrument("smu")
    assert said.startswith("smu is connected now, with its connection made anew (it answered as itself)"), said
    assert inst.query_raw(":SOUR:VOLT?") == "1.0" and not inst.backend.owed


def test_reconnect_reaches_a_camera_whose_helper_kelvin_ended(monkeypatch, runtime_of):
    import labharness.homelab as homelab
    monkeypatch.setattr(homelab, "card_now", lambda *a, **k: None)
    lab = build_lab('lab: T\ninstruments:\n  cam: { card: keithley/2400, address: "sim://cam" }\n')
    made, real = [], lab._factory
    lab._factory = lambda name, card, address: made.append(name) or real(name, card, address)

    class EndedCamera:
        simulated, quiet_close = False, True

        def __init__(self):
            self.helper = types.SimpleNamespace(gone="it didn't answer within 10 s, reading exposure")

        def query(self, cmd):
            raise RuntimeError("not connected any more: Kelvin ended PICam's helper")

        write = query

        def close(self):
            pass

    lab.instruments["cam"].backend = EndedCamera()
    said = runtime_of(lab).reconnect_instrument("cam")
    assert made == ["cam"] and not isinstance(lab.instruments["cam"].backend, EndedCamera), said
    assert said.startswith("cam is connected now"), said
