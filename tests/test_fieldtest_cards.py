"""Cards after the field tests: the problems physicists met with Kelvin's cards and connections (labsim test, 2026-10),
each reproduced on a fake bus or with labsim, so they stay fixed."""
import pytest

from labharness import builtin_registry
from labharness.adapters import AdapterError, VisaBackend
from labharness.cards import CardError, CardRegistry, parse_card
from labharness.carddraft import check
from labharness.clock import SimClock
from labharness.gate import SafetyGate, ScriptedApprover
from labharness.lab import Lab
from labharness.labconfig import load_lab_config

from test_hardware import Bus, Device


def card_with(**more):
    data = {"card": 1, "id": "acme/box", "model": "Acme box", "kind": "multimeter", "status": "draft",
            "parameters": {"V": {"kind": "measure", "unit": "V", "get": "MEAS:VOLT?"}}}
    data.update(more)
    return parse_card(data)


class Line:
    """A serial line or socket: replies queue up and are read in order, as on the real thing."""

    def __init__(self, answer=None, greeting=(), acks_sets=None):
        self.answer = answer or (lambda cmd: None)
        self.pending = list(greeting)
        self.acks_sets = acks_sets
        self.sent = []
        self.timeout = self.read_termination = self.write_termination = None

    def write(self, cmd):
        self.sent.append(cmd)
        reply = self.answer(cmd)
        if reply is not None:
            self.pending.append(reply)
        elif self.acks_sets and "?" not in cmd:
            self.pending.append(self.acks_sets)

    def read(self):
        if not self.pending:
            raise TimeoutError("VI_ERROR_TMO")
        return self.pending.pop(0)

    def query(self, cmd, delay=None):
        self.write(cmd)
        return self.read()

    def close(self):
        pass


class OneLine:
    def __init__(self, line):
        self.line = line

    def open_resource(self, name):
        return self.line


# ------------------------------------------------------------------ 17: the card format


def test_parity_is_taken_as_the_manual_writes_it():
    """l07: parity: N passed check_card, then connecting failed with a PyVISA enum error. The manual's letters and
    "8N1" mean what they say; anything else is refused when the card is checked, not when it connects."""
    card = card_with(connection={"baud_rate": 9600, "parity": "N"})
    assert card.connection["parity"] == "none"
    card = card_with(connection={"baud_rate": 57600, "framing": "7O1"})
    assert (card.connection["data_bits"], card.connection["parity"], card.connection["stop_bits"]) == (7, "odd", 1.0)
    for bad in ({"parity": "X"}, {"framing": "8Q1"}, {"data_bits": 9}, {"stop_bits": 3}):
        with pytest.raises(CardError):
            card_with(connection=dict(bad, baud_rate=9600))
    _, problems, _ = check("card: 1\nid: a/b\nmodel: B\nkind: x\nconnection: {parity: Z}\n"
                           "parameters: {v: {kind: measure, get: 'V?'}}\n")
    assert any("parity" in p for p in problems)

    dev = Device("lakeshore/336", serial={"baud_rate": 57600, "data_bits": 7, "parity": "odd"}, name="ls")
    bus = Bus({"ASRL3::INSTR": dev})
    lake = builtin_registry().get("lakeshore/336")
    # a lab config's framing (as a person writes it) wins over the card's, letters and all
    be = VisaBackend("tc", lake, "ASRL3::INSTR", bus, overrides={"framing": "7O1"})
    assert be.query("*IDN?").startswith("LSCI")
    with pytest.raises(AdapterError):
        VisaBackend("tc", lake, "ASRL3::INSTR", bus, overrides={"parity": "Q"})


def test_bracketed_commands_go_out_without_their_optional_nodes():
    """l04: the SMB100A's health check sent :SYSTem:ERRor[:NEXT]? as written: the generator answered -113 and no
    reply, which raised a standing alarm and failed every preflight. A card's commands go out as the manual means
    them, wherever they are sent: health checks, the identity query, actions, a reading's arming, on_connect."""
    smb = builtin_registry().get("rs/smb100a")
    assert [h.query for h in smb.health if h.query] == [":SYSTem:ERRor?"]
    card = card_with(identify={"query": "*IDN[:QUERy]?", "idn": "ACME"},
                     actions={"trigger": {"send": ["INITiate[:IMMediate]", "*TRG"]}},
                     on_connect=[":SYSTem[:COMMunicate]:HEADer OFF"],
                     parameters={"V": {"kind": "measure", "get": ["INITiate[:IMMediate]", "*OPC?", "FETCh[:VOLTage]?"],
                                       "acquires": True}})
    assert card.idn_query == "*IDN?"
    assert card.actions["trigger"].send == ["INITiate", "*TRG"]
    assert card.on_connect == [":SYSTem:HEADer OFF"]
    assert card.params["V"].get_before == ["INITiate", "*OPC?"]

    reg = builtin_registry()
    sent = []

    class Generator:   # answers like the real one: an unknown header gets -113 and no reply
        simulated = False

        def query(self, cmd):
            sent.append(cmd)
            if "[" in cmd:
                raise TimeoutError("VI_ERROR_TMO")
            if cmd.upper().startswith(":SYST"):
                return "0,\"No error\""
            if cmd == "*IDN?":
                return "Rohde&Schwarz,SMB,1412.0000K02/000000,03.01.158"
            return {":PULM:SOURce?": "INT", ":PULM:POLarity?": "NORM"}.get(cmd, "0")

        def write(self, cmd):
            sent.append(cmd)

        def close(self):
            pass
    cfg = load_lab_config({"lab": "x", "instruments": {"mw": {"card": "rs/smb100a", "address": "TCPIP0::mw::INSTR"}}},
                          reg)
    lab = Lab(cfg, reg, lambda n, c, a: Generator(), SimClock())
    health = lab.health_check(["mw"])[0]
    assert health.ok, health.problems
    assert not any("[" in c for c in sent), sent


def test_an_error_query_reply_that_means_no_error():
    """l06/l07: the SMC100 answers 1TE@ when it has no error, which Kelvin took for an error: a standing alarm and a
    failed preflight. A card says how its instrument says "no error"."""
    card = card_with(error_query="1TE", no_error="^1TE@$")
    reg = CardRegistry()
    reg.add(card)
    replies = iter(["1TE@", "1TEC"])

    class Stage:
        simulated = False

        def query(self, cmd):
            return next(replies)

        def write(self, cmd):
            pass

        def close(self):
            pass
    cfg = load_lab_config({"lab": "x", "instruments": {"stage": {"card": "acme/box", "address": "ASRL6::INSTR"}}}, reg)
    lab = Lab(cfg, reg, lambda n, c, a: Stage(), SimClock())
    inst = lab.instruments["stage"]
    assert inst.instrument_error() is None
    assert inst.instrument_error() == "1TEC"
    with pytest.raises(CardError):
        card_with(no_error="^1TE@$")   # without an error query it means nothing
    with pytest.raises(CardError):
        card_with(error_query="1TE", no_error="(")


def test_a_greeting_is_read_before_the_first_command():
    """l01, l08, l10: the AMI 430 greets each LAN connection with two lines; unread, every reply ran two lines late
    for the whole session ("8 T" when the field was 0). The card's greeting_lines are read and dropped on connect."""
    card = card_with(connection={"greeting_lines": 2})
    line = Line(lambda cmd: "1.2345" if cmd == "MEAS:VOLT?" else None,
                greeting=["American Magnetics Model 430 IP Interface", "Hello."])
    be = VisaBackend("magnet", card, "TCPIP0::10.0.0.5::7180::SOCKET", OneLine(line))
    assert be.query("MEAS:VOLT?") == "1.2345"
    quiet = Line(lambda cmd: "0.5" if cmd == "MEAS:VOLT?" else None)   # the same instrument on RS-232: no greeting
    be = VisaBackend("magnet", card, "ASRL1::INSTR", OneLine(quiet))
    assert be.query("MEAS:VOLT?") == "0.5"
    with pytest.raises(CardError):
        card_with(connection={"greeting_lines": 99})


def test_an_ack_missing_after_a_query_says_to_use_set_reply():
    """l07: the hot plate answers OK to its set commands only; its card said ack (which the guide described as a set's
    reply), so every reading timed out and Kelvin blamed the USB cable. ack is the line after every command (OBIS);
    when a query's never comes, the error says set_reply is what the card wants. set_reply is in the card guide."""
    from labharness.carddraft import GUIDE
    assert "set_reply" in GUIDE and "queries too" in GUIDE
    card = card_with(connection={"ack": "OK"})
    plate = Line(lambda cmd: "22.4" if cmd == "MEAS:VOLT?" else None, acks_sets="OK")
    be = VisaBackend("plate", card, "ASRL4::INSTR", OneLine(plate))
    with pytest.raises(AdapterError) as e:
        be.query("MEAS:VOLT?")
    assert "set_reply" in str(e.value)
    card = card_with(connection={"set_reply": "^OK$"},
                     parameters={"sp": {"kind": "measure", "set": "SET {value}", "get": "SET?"}})
    plate = Line(lambda cmd: "40" if cmd == "SET?" else None, acks_sets="OK")
    be = VisaBackend("plate", card, "ASRL4::INSTR", OneLine(plate))
    be.write("SET 40")
    assert be.query("SET?") == "40"


def test_settings_with_no_read_back_can_be_set():
    """l01, l05: a card's setting with a set command and no get (the 6221 draft's frequency, the iDus setpoint) failed
    with "must be real number, not NoneType" before anything was sent: an unknown value is not the same as the target.
    Nothing is read back, and the error queue still is."""
    card = parse_card({"card": 1, "id": "acme/gen", "model": "Acme gen", "kind": "generator", "status": "draft",
                       "error_query": "SYST:ERR?",
                       "parameters": {"freq": {"kind": "measure", "unit": "Hz", "set": "FREQ {value}", "limits": [1, 1e6]},
                                      "func": {"kind": "measure", "type": "enum", "values": {"sine": "SIN"},
                                               "set": "FUNC {value}"},
                                      "amp": {"kind": "drive", "unit": "A", "set": "AMPL {value}", "limits": [0, 1e-6],
                                              "safe": 0}}})
    reg = CardRegistry()
    reg.add(card)
    sent = []

    class Gen:
        simulated = False

        def write(self, cmd):
            sent.append(cmd)

        def query(self, cmd):
            sent.append(cmd)
            if cmd == "SYST:ERR?":
                return "0,No error"
            raise TimeoutError("VI_ERROR_TMO")

        def close(self):
            pass
    cfg = load_lab_config({"lab": "x", "instruments": {"g": {"card": "acme/gen", "address": "GPIB0::1::INSTR"}}}, reg)
    lab = Lab(cfg, reg, lambda n, c, a: Gen(), SimClock())
    gate = SafetyGate(lab, ScriptedApprover([True] * 4))
    for param, value in (("freq", 1000), ("func", "sine"), ("amp", 1e-7), ("amp", 0)):
        r = gate.set("g", param, value)
        assert r.ok, r.message
    assert [c for c in sent if c != "SYST:ERR?"] == ["FREQ 1000", "FUNC SIN", "AMPL 1e-07", "AMPL 0"]
    assert lab.read("g", "freq") == 1000   # what Kelvin set, as it can't be read
