"""The 0.3.52 audit's findings on instrument setup, cards and drivers (I1-I5, I7, I9), kept fixed: each test is the
audit's repro (audit-repros/instruments) cut to what it showed."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from labharness import discovery, drivers
from labharness.adapters import VisaBackend
from labharness.cards import CardError, CardRegistry, load_card, parse_card
from labharness.instrument import Instrument, SensorError, SensorFlagged
from labharness.labconfig import InstrumentConfig
from labharness.yamlio import load_yaml

CARDS = Path(__file__).resolve().parents[1] / "src" / "labharness" / "data" / "cards"


# ------------------------------------------------------------------ I1: a pulse generator's stop

class FakeStreamer:
    REPEAT_INFINITELY = -1

    def __init__(self, host):
        self.streaming, self.link_down, self.calls = False, False, []

    def createSequence(self):
        return SimpleNamespace(setDigital=lambda ch, pattern: None)

    def stream(self, seq, n, final):
        self.calls.append("stream")
        self.streaming = True

    def constant(self, state):
        self.calls.append("constant(ZERO)")
        if self.link_down:
            raise OSError("connection reset by peer")
        self.streaming = False

    def getSerial(self):
        if self.link_down:
            raise OSError("connection reset by peer")
        return "00-26-32-f0-3b-1b"

    def getFirmwareVersion(self):
        return "1.7.2"


class FakeModule:
    PulseStreamer = FakeStreamer

    class OutputState:
        @staticmethod
        def ZERO():
            return 0


@pytest.fixture
def streamer(monkeypatch):
    monkeypatch.setitem(drivers.MODULES, "pulsestreamer", FakeModule)
    card = load_card(CARDS / "instruments" / "swabian_pulse_streamer.yaml")
    backend = drivers.open_driver("pg", card, "pulsestreamer://169.254.8.2")
    inst = Instrument("pg", card, backend, InstrumentConfig("pg", card.id, "pulsestreamer://169.254.8.2"))
    try:
        yield inst, backend.ps
    finally:
        backend.ps.link_down = False
        backend.close()


def test_i1_a_stop_that_failed_leaves_the_generator_running_and_the_next_stop_is_sent(streamer):
    """The driver noted "stopped" before it called the hardware and never took that back: a stop that failed once
    (a network blip) showed the generator stopped, and the next stop sent nothing while it went on streaming."""
    inst, hw = streamer
    inst.write("running", True)
    assert hw.streaming and inst.read("running")
    hw.link_down = True
    with pytest.raises(Exception):
        inst.write("running", False)
    hw.link_down = False
    assert hw.streaming and inst.read("running")   # it may still play: said so
    before = len(hw.calls)
    inst.write("running", False)
    assert hw.calls[before:] == ["constant(ZERO)"] and not hw.streaming and not inst.read("running")
    inst.write("running", False)   # asked again, sent again
    assert hw.calls[-1] == "constant(ZERO)"


# ------------------------------------------------------------------ I2: a scan asks an unknown instrument *IDN? only

class Tmo(Exception):
    error_code = -1073807339

    def __str__(self):
        return "VI_ERROR_TMO"


class Nobody(Exception):
    error_code = -1073807265

    def __str__(self):
        return "VI_ERROR_NLISTENERS"


class GpibBus:
    def __init__(self, at):
        self.at, self.log = at, []

    def open_resource(self, name, **kw):
        bus = self

        class Res:
            timeout = 0
            read_termination = write_termination = "\n"

            def query(self, q):
                bus.log.append((name, q))
                said = bus.at.get(name)
                if said is None:
                    raise Nobody()
                if said == "":
                    raise Tmo()
                return said

            def write(self, q):
                bus.log.append((name, q))

            def clear(self):
                bus.log.append((name, "DEVICE CLEAR"))

            def close(self):
                pass
        return Res()


def test_i2_an_unknown_gpib_instrument_is_asked_idn_and_nothing_else():
    """One that didn't answer *IDN? within 300 ms got a Selected Device Clear (which aborts what another program
    does with it, and may restore an older one's power-on settings), then every card's identity query."""
    registry = CardRegistry()
    registry.load_dir(CARDS)
    old = "GPIB0::12::INSTR"
    bus = GpibBus({old: "", "GPIB0::24::INSTR": "KEITHLEY INSTRUMENTS INC.,MODEL 2400,1234567,C30 Mar 17 2006"})
    assert dict(discovery.gpib_listeners(bus, "GPIB0"))[old] == ""
    assert [q for n, q in bus.log if n == old] == ["*IDN?"]
    bus.log.clear()
    found = discovery.identify(old, registry, rm=bus, usb={})
    assert [q for n, q in bus.log if n == old] == ["*IDN?"] and "didn't answer *IDN?" in found.how


# ------------------------------------------------------------------ I3: a serial number a driver couldn't read

def test_i3_a_drivers_placeholder_serial_is_never_compared_nor_kept(tmp_path):
    """A Pulse Streamer whose serial read failed once answered "...,?,?": "?" differed from the kept serial, so the
    healthy instrument was set aside (a stop included); accepted, "?" then set the real one aside next time."""
    from labharness import profiles
    from labharness.lab import identity_problem
    card = load_card(CARDS / "instruments" / "swabian_pulse_streamer.yaml")
    inst = SimpleNamespace(card=card, kept_serial="00-26-32-f0-3b-1b", name="pg",
                           config=SimpleNamespace(address="pulsestreamer://169.254.8.2"))
    assert identity_problem(inst, "Swabian Instruments,Pulse Streamer 8/2,?,?") is None
    assert "set up with serial number" in identity_problem(inst, "Swabian Instruments,Pulse Streamer 8/2,00-99,1.7")
    inst.kept_serial = "?"   # one an earlier Kelvin kept
    assert identity_problem(inst, "Swabian Instruments,Pulse Streamer 8/2,00-26-32-f0-3b-1b,1.7.2") is None
    profiles.accept_serial(tmp_path, "pg", "?", card.id, "pulsestreamer://169.254.8.2")
    assert profiles.kept_serial(tmp_path, "pg", card.id, "pulsestreamer://169.254.8.2") == ""


# ------------------------------------------------------------------ I4, I9: what a card may say

CARD = r"""
card: 1
id: acme/sm-2000
model: ACME SM-2000 Source-Meter
kind: source-meter
status: draft
identify: { idn: "^ACME,SM-2000" }
on_connect: %s
parameters:
  V: { kind: drive, unit: V, set: "SOURce:VOLTage {value}", get: "SOURce:VOLTage?", limits: [-200, 200], safe: 0 }
  output: { kind: drive, type: bool, set: "OUTPut {value}", get: "OUTPut?", safe: off }
  T_acq: { kind: measure, unit: K, get: ["INITiate", "FETCh?"], acquires: true, plausible: [0.01, 1600] }
raw:
  blocked: ["DIAGnostic"]
  reset: ["*RST", "SYSTem:PRESet"]
"""


@pytest.mark.parametrize("command", ["*RST", ":SYST:PRES", "*RCL 1", ":SOURce:FUNCtion CURRent", "DIAG:RUN",
                                     "SYST:BEEP OFF;*RST", ":CAL:ALL"])
def test_i4_a_cards_on_connect_never_resets_or_sends_what_the_gate_would_refuse(command):
    with pytest.raises(CardError, match="on_connect"):
        parse_card(load_yaml(CARD % ('["%s"]' % command)), "audit")
    card = parse_card(load_yaml(CARD % '[":SYST:REM", ":FORM:ELEM READ"]'), "audit")   # setting up the connection
    assert card.on_connect == [":SYST:REM", ":FORM:ELEM READ"]


class Res:
    timeout = 0
    read_termination = write_termination = "\n"

    def __init__(self, reply="0.0"):
        self.reply, self.log = reply, []

    def write(self, command):
        self.log.append(command)

    def query(self, command):
        self.log.append(command)
        return self.reply

    def close(self):
        pass


def test_i9_a_reading_that_runs_an_acquisition_is_checked_as_any_other():
    card = parse_card(load_yaml(CARD % "[]"), "audit")
    res = Res("0.0")
    backend = VisaBackend("smu", card, "GPIB0::5::INSTR", rm=SimpleNamespace(open_resource=lambda name, **k: res))
    inst = Instrument("smu", card, backend, InstrumentConfig("smu", card.id, "GPIB0::5::INSTR"))
    with pytest.raises(SensorError, match="can't really be"):
        inst.read("T_acq")
    res.reply = "4.2"
    assert inst.read("T_acq") == 4.2


# ------------------------------------------------------------------ I5: SCPI's overflow value

@pytest.mark.parametrize("file, reply", [("keithley_2182a.yaml", "+9.9E37\n"),
                                         ("keysight_34465a.yaml", "+9.90000000E+37\n"),
                                         ("keysight_34465a.yaml", "+9.91000000E+37\n")])
def test_i5_scpis_overflow_value_is_no_reading(file, reply):
    """A 2182A or a 344xxA over range answers +9.9E37: it was kept as 9.9e37 V ("9.9e+28 GV"), and a watch for
    "below" never fired on it."""
    card = load_card(CARDS / "instruments" / file)
    backend = SimpleNamespace(simulated=False, query=lambda command: reply, write=lambda command: None)
    inst = Instrument("dmm", card, backend, InstrumentConfig("dmm", card.id, "GPIB0::7::INSTR"))
    spec = next(p for p in card.params.values() if p.kind == "measure" and p.get_cmd is not None
                and p.set_cmd is None and p.numeric)
    with pytest.raises(SensorFlagged, match="over its range"):
        inst.read(spec.name)


# ------------------------------------------------------------------ I7: numbers on the wire

def test_i7_numbers_go_to_an_instrument_as_plain_decimals_and_a_sweeps_zero_is_zero():
    """A sweep from -0.3 to 0.3 by 0.1 sent 5.55111512313e-17 where 0 was meant, and 0.00005 went as 5e-05: an
    instrument that reads digits and a point (an Arduino's parseFloat) set 5.55 and 5."""
    from labharness.cards import plain_number
    from labharness.plans import load_plan
    assert [plain_number(x) for x in (0.00005, 0.00001234, 2.5e-7, 1e15, -0.2, 3.0, 1500000.0)] == \
        ["0.00005", "0.00001234", "0.00000025", "1000000000000000", "-0.2", "3", "1500000"]
    plan = load_plan("plan: gate\nsweep: { smu.V: {from: -0.3, to: 0.3, step: 0.1} }\nmeasure: [smu.I]\n")
    values = list(plan.axes[0].values)
    assert values == [-0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3], values
