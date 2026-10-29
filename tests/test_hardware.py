"""Real instruments: VISA/serial adapters, discovery and the instrument cards, on a fake VISA bus.

The fake bus answers like real instruments (it wraps twins of the real cards), insists on the right serial
framing where a real instrument would, and records every command, so these tests need no hardware and no PyVISA.
"""
import pytest

from labharness import builtin_registry
from labharness.adapters import AdapterError, VisaBackend, resource_name, set_resource_manager
from labharness.cards import CardError, parse_card
from labharness.clock import ScaledClock, SimClock
from labharness.discovery import draft_lab_config, identify, match_card, scan
from labharness.gate import SafetyGate, ScriptedApprover
from labharness.labconfig import ConfigError, load_lab_config
from labharness.sim.twin import World, make_twin_backend
from labharness.homelab import build_lab


class Device:
    """One instrument on the fake bus."""

    def __init__(self, card_id=None, idn=None, serial=None, ack=None, silent=False, name="dev", replies=None, vars=None):
        reg = builtin_registry()
        self.card = reg.get(card_id).bind(vars) if card_id else None   # vars: the channel it is set up for
        self.twin = make_twin_backend(self.card, World(SimClock()), name) if self.card else None
        self.idn = idn
        self.serial = serial or {}      # settings it insists on, e.g. {"baud_rate": 57600, "parity": "odd"}
        self.ack = ack
        self.replies = replies        # answers every set with replies(command), e.g. Oxford Mercury's ...:VALID
        self.silent = silent
        self.sent = []

    def answers(self, res):
        for key, want in self.serial.items():
            got = getattr(res, key, None)
            if key == "parity":
                got = str(getattr(got, "name", got)).lower()   # a PyVISA enum, or a plain string without PyVISA
            if got != want:
                return False
        return not self.silent


class Resource:
    def __init__(self, dev):
        self.dev = dev
        self.timeout = self.read_termination = self.write_termination = None
        self.pending = []

    def write(self, cmd):
        if not self.dev.answers(self):
            return
        self.dev.sent.append(cmd)
        if self.dev.twin:
            self.dev.twin.write(cmd)
        if self.dev.ack:
            self.pending.append(self.dev.ack)
        elif self.dev.replies:
            self.pending.append(self.dev.replies(cmd))

    def query(self, cmd, delay=None):
        if not self.dev.answers(self):
            raise TimeoutError("VI_ERROR_TMO")
        self.dev.sent.append(cmd)
        if cmd == "*IDN?" and self.dev.idn:
            reply = self.dev.idn
        else:
            reply = self.dev.twin.query(cmd)
        if self.dev.ack:
            self.pending.append(self.dev.ack)
        return reply

    def read(self):
        if not self.pending:
            raise TimeoutError("VI_ERROR_TMO")
        return self.pending.pop(0)

    def close(self):
        pass


class Bus:
    def __init__(self, devices):
        self.devices = devices

    def list_resources(self, query="?*::INSTR"):
        return tuple(self.devices)

    def open_resource(self, name):
        if name not in self.devices:
            raise OSError("VI_ERROR_RSRC_NFOUND")
        return Resource(self.devices[name])


@pytest.fixture
def bus():
    b = Bus({
        "GPIB0::24::INSTR": Device("keithley/2400", name="k2400"),
        "GPIB0::8::INSTR": Device("srs/sr830", name="sr830"),
        "ASRL3::INSTR": Device("lakeshore/336", serial={"baud_rate": 57600, "data_bits": 7, "parity": "odd"}, name="ls"),
        "GPIB0::5::INSTR": Device(idn="ACME,WIDGET 9000,1,1.0"),
        "ASRL4::INSTR": Device(silent=True),
    })
    set_resource_manager(b)
    yield b
    set_resource_manager(None)


def test_scan_identifies_instruments_by_asking_only(bus):
    reg = builtin_registry()
    found = {f.address: f for f in scan(reg, bus, in_lab={"GPIB0::8::INSTR": "lockin"})}
    assert found["GPIB0::24::INSTR"].card_id == "keithley/2400" and found["GPIB0::24::INSTR"].kind == "source-meter"
    tc = found["ASRL3::INSTR"]
    assert tc.card_id == "lakeshore/336" and tc.settings.startswith("57600 7O")   # its own serial framing
    assert found["GPIB0::5::INSTR"].card_id is None and "no card matches" in found["GPIB0::5::INSTR"].line()
    assert "no answer" in found["ASRL4::INSTR"].line()
    assert found["GPIB0::8::INSTR"].in_lab == "lockin" and bus.devices["GPIB0::8::INSTR"].sent == []  # left alone
    for dev in bus.devices.values():   # discovery never sets anything
        assert all("?" in cmd for cmd in dev.sent), dev.sent


def test_draft_lab_config_is_valid_and_asks_for_limits(bus):
    reg = builtin_registry()
    text = draft_lab_config(scan(reg, bus), reg, "Cryostat 2")
    assert "smu:" in text and "lockin:" in text and "tc:" in text and "tighten" in text
    cfg = load_lab_config(text, reg)
    assert cfg.instruments["smu"].address == "GPIB0::24::INSTR" and cfg.instruments["smu"].card == "keithley/2400"
    assert cfg.instruments["smu"].limits["V"] == (-210, 210)


def test_identity_matching_prefers_the_most_specific_card():
    reg = builtin_registry()
    assert match_card("KEITHLEY INSTRUMENTS INC.,MODEL 2401,1,C30", reg).id == "keithley/2400"
    assert match_card("KEITHLEY INSTRUMENTS,MODEL 2450,0,1.7", reg).id == "keithley/2450"
    assert match_card("Stanford_Research_Systems,SR865,1,v1", reg).id == "srs/sr860"
    assert match_card("NOPE,NOTHING", reg) is None
    for card in reg:   # every card recognises its own example identity
        if card.idn_pattern:
            assert match_card(card.idn_example, reg).id == card.id, card.id


def test_real_instruments_through_the_gate(bus):
    lab = build_lab("""
lab: Real bench
instruments:
  smu: { card: keithley/2400, address: "GPIB0::24::INSTR", limits: { V: [-5, 5] }, rates: { V: 5 } }
  lockin: { card: srs/sr830, address: "GPIB0::8::INSTR" }
  sim_tc: { card: virtual/temperature-controller }
""")
    assert not lab.instrument("smu").simulated and lab.instrument("sim_tc").simulated
    assert type(lab.clock).__name__ == "RealClock"      # real hardware runs on real time
    k = bus.devices["GPIB0::24::INSTR"]
    gate = SafetyGate(lab, ScriptedApprover([True, True]))
    assert gate.get("smu", "I").value is None             # output off: :READ? would switch it on, so no query
    assert ":READ?" not in k.sent
    assert gate.set("smu", "output", True).ok and gate.set("smu", "V", 0.1).ok
    assert any(c.startswith(":SOURce:VOLTage:LEVel:IMMediate:AMPLitude 0.1") or c.startswith(":SOURce:VOLTage") for c in k.sent)
    assert gate.get("smu", "I").ok and ":READ?" in k.sent  # now it may read
    assert gate.set("smu", "V", 7).action == "refused"      # lab limit
    assert gate.set("lockin", "time_constant", "300 ms").ok and "OFLT 9" in bus.devices["GPIB0::8::INSTR"].sent


def test_real_labs_refuse_simulated_time(bus):
    with pytest.raises(ValueError):
        build_lab({"lab": "x", "instruments": {"smu": {"card": "keithley/2400", "address": "GPIB0::24::INSTR"}}},
                  clock=ScaledClock(10))


def test_acknowledged_and_listen_only_instruments():
    reg = builtin_registry()
    obis = Device("coherent/obis", ack="OK", name="obis")
    b = Bus({"ASRL7::INSTR": obis})
    be = VisaBackend("laser", reg.get("coherent/obis"), "serial://COM7", b)
    be.write("SOURce:AM:STATe ON")
    assert be.query("SOURce:AM:STATe?").strip() == "ON"    # the OK after each command is read, not left behind
    obis.ack = "ERR-400"
    with pytest.raises(AdapterError):
        be.write("SOURce:AM:STATe OFF")

    pre = Device("srs/sr560", name="sr560")
    b = Bus({"ASRL2::INSTR": pre})
    be = VisaBackend("preamp", reg.get("srs/sr560"), "ASRL2::INSTR", b)
    assert pre.sent == ["LALL"]                             # its wake-up command, sent on connect
    with pytest.raises(AdapterError):
        be.query("GAIN?")


def test_lab_connection_settings_override_the_card(bus):
    reg = builtin_registry()
    cfg = load_lab_config({"lab": "x", "instruments": {"lockin": {"card": "srs/sr830", "address": "ASRL1::INSTR",
                                                                   "connection": {"read_termination": "\r"}}}}, reg)
    assert cfg.instruments["lockin"].connection == {"read_termination": "\r"}
    with pytest.raises(ConfigError):
        load_lab_config({"lab": "x", "instruments": {"lockin": {"card": "srs/sr830", "connection": {"speed": 9}}}}, reg)
    bus.devices["ASRL1::INSTR"] = Device("srs/sr830", name="sr830b")
    be = VisaBackend("lockin", reg.get("srs/sr830"), "ASRL1::INSTR", bus, overrides={"read_termination": "\r"})
    assert be.res.read_termination == "\r" and be.res.baud_rate == 9600


def test_every_real_card_works_on_its_twin():
    """Each card's commands round-trip: drives go to safe and read back; every reading parses."""
    reg = builtin_registry()
    for card in reg:
        if card.id.startswith("virtual/"):
            continue
        twin = make_twin_backend(card, World(SimClock()), card.id)
        for spec in card.params.values():
            if spec.set_cmd is not None:
                value = spec.safe if spec.safe is not None else (spec.default if spec.default is not None else None)
                if value is not None:
                    twin.write(spec.set_cmd.render(spec.to_wire(value)))
                    assert not twin.errors, (card.id, spec.name, twin.errors)
            if spec.get_cmd is not None and spec.type != "trace":
                spec.from_wire(twin.query(spec.get_cmd.render()))


def test_reply_parsing_and_card_checks():
    reg = builtin_registry()
    i = reg.get("keithley/2400").params["I"]
    assert i.from_wire("+1.000000E-01,+2.345678E-09,+9.910000E+37,+1.234E+03,+3.94600E+04") == pytest.approx(2.345678e-9)
    base = {"card": 1, "id": "t/x", "model": "X", "kind": "thing", "status": "draft"}
    t = parse_card(dict(base, parameters={"T": {"kind": "measure", "unit": "K", "get": "READ:T?",
                                                "parse": "TEMP:([-0-9.eE+]+)K"}}))
    assert t.params["T"].from_wire("STAT:DEV:MB1:TEMP:SIG:TEMP:1.5000K") == 1.5
    with pytest.raises(CardError):   # connecting may only configure, never move a drive
        parse_card(dict(base, on_connect=["VOLT 5"], parameters={
            "V": {"kind": "drive", "unit": "V", "set": "VOLT {value}", "get": "VOLT?", "limits": [0, 10], "safe": 0}}))
    with pytest.raises(CardError):
        parse_card(dict(base, parameters={"I": {"kind": "measure", "get": "I?", "read_when": "nothing"}}))
    with pytest.raises(CardError):
        parse_card(dict(base, connection={"baud": 9600}, parameters={"I": {"kind": "measure", "get": "I?"}}))
    assert resource_name("serial://COM3") == "ASRL3::INSTR" and resource_name("visa://GPIB0::1::INSTR") == "GPIB0::1::INSTR"


def test_identify_one_address(bus):
    f = identify("GPIB0::24::INSTR", builtin_registry(), bus)
    assert f.card_id == "keithley/2400" and f.idn.startswith("KEITHLEY")
    assert identify("GPIB0::99::INSTR", builtin_registry(), bus).card_id is None
