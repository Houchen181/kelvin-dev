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
    sent.clear()
    gate.set("g", "amp", 1e-7)
    lab.instruments["g"]._last.pop("amp")   # Kelvin restarted: the output's level is unknown
    assert all(r.ok for r in gate.to_safe("g")) and "AMPL 0" in sent   # going to safe never skips it


# ------------------------------------------------------------------ 14: Kelvin's own cards for what testers needed


def lab_of(reg, instruments, backends):
    cfg = load_lab_config({"lab": "x", "instruments": instruments}, reg)
    return Lab(cfg, reg, lambda name, card, address: backends[name], SimClock())


class Fake6221:
    """A 6221 as its reference manual describes it and labsim models it: no Delta commands known (labsim lacks them),
    so an unknown header gets -113 and no reply."""
    simulated = False

    def __init__(self):
        self.sent, self.errors = [], []
        self.state = {":OUTPut?": "0", ":SOURce:CURRent?": "+0.000000E+00", ":SOURce:CURRent:RANGe?": "2.000000E-03",
                      ":SOURce:CURRent:RANGe:AUTO?": "1", ":SOURce:CURRent:COMPliance?": "+1.000000E+01",
                      ":SOURce:WAVE:FUNCtion?": "SIN", ":SOURce:WAVE:AMPLitude?": "1.000000E-03",
                      ":SOURce:WAVE:OFFSet?": "0", ":SOURce:WAVE:FREQuency?": "1.000000E+03",
                      ":SOURce:WAVE:RANGing?": "BEST", ":SOURce:WAVE:DURation:TIME?": "9.9E37",
                      ":SOURce:WAVE:PMARker:STATe?": "0", ":SOURce:WAVE:PMARker?": "180",
                      ":SOURce:WAVE:PMARker:OLINe?": "3"}

    def write(self, cmd):
        self.sent.append(cmd)
        head, _, value = cmd.partition(" ")
        if head + "?" in self.state:
            self.state[head + "?"] = value
        elif "WAVE" not in head:
            self.errors.append('-113,"Undefined header"')

    def query(self, cmd):
        self.sent.append(cmd)
        if cmd == "*IDN?":
            return "KEITHLEY INSTRUMENTS INC.,MODEL 6221,4123960,D04  /700x "
        if cmd == ":SYSTem:ERRor?":
            return self.errors.pop(0) if self.errors else '0,"No error"'
        if cmd in self.state:
            return self.state[cmd]
        self.errors.append('-113,"Undefined header"')
        raise TimeoutError("VI_ERROR_TMO")

    def close(self):
        pass


def test_the_6221_card_sources_dc_and_a_sine_with_its_phase_marker():
    """Five testers needed a 6221 and drafted cards that measured at 2 pA or 0 A: Kelvin brings one, from the
    reference manual. Delta's reading is taken only when asked (a 6221 without its 2182A, or labsim, answers -113),
    so the health check passes; starting a waveform or Delta asks a person."""
    from labharness.discovery import match_card
    reg = builtin_registry()
    card = reg.get("keithley/6221")
    assert match_card("KEITHLEY INSTRUMENTS INC.,MODEL 6221,4123960,D04  /700x ", reg).id == "keithley/6221"
    assert match_card("KEITHLEY INSTRUMENTS INC., MODEL 6221,1234567,A03/B01/2", reg).id == "keithley/6221"
    assert match_card("KEITHLEY INSTRUMENTS INC.,MODEL 6220,4321,D04 /700x", reg) is None   # DC only: no waveforms
    k = Fake6221()
    lab = lab_of(reg, {"isrc": {"card": "keithley/6221", "address": "GPIB0::12::INSTR"}}, {"isrc": k})
    assert lab.health_check()[0].ok, lab.health_check()[0].problems
    assert not any("DELT" in c or "SENS" in c for c in k.sent)                  # nothing of Delta was asked
    approver = ScriptedApprover([True] * 4)
    asked = approver.requests
    gate = SafetyGate(lab, approver)
    assert gate.set("isrc", "wave_amplitude", 5e-8).ok and not asked           # from its 1 mA default: toward safe
    assert gate.set("isrc", "wave_amplitude", 1e-7).ok and len(asked) == 1     # away from safe: asks
    assert gate.set("isrc", "phase_marker", True).ok and gate.set("isrc", "phase_marker_line", 1).ok
    assert gate.act("isrc", "wave_start").ok and len(asked) == 2               # the waveform reaches the sample
    assert [c for c in k.sent if "WAVE:ARM" in c or "WAVE:INIT" in c] == [":SOURce:WAVE:ARM", ":SOURce:WAVE:INITiate"]
    assert gate.act("isrc", "wave_stop").ok and ":SOURce:WAVE:ABORt" in k.sent
    assert gate.set("isrc", "delta_high", 0.2).action == "refused"              # beyond the 6221's 105 mA
    assert card.params["wave_amplitude"].limits == (2e-12, 0.105) and card.params["compliance_V"].limits == (0.1, 105)
    assert card.actions["delta_start"].send == [":SOURce:DELTa:ARM", ":INITiate"] and card.actions["delta_start"].drives


class SerialLine:
    """A serial instrument at one framing: other framings get nothing back, as on a real line."""

    def __init__(self, framing, answer):
        self.framing, self.answer = framing, answer
        self.timeout = self.read_termination = self.write_termination = None
        self.baud_rate = self.data_bits = self.parity = self.stop_bits = None
        self.pending, self.sent = [], []

    def ok(self):
        parity = str(getattr(self.parity, "name", self.parity)).lower()
        stop = {"one": 1.0, "one_and_a_half": 1.5, "two": 2.0}.get(getattr(self.stop_bits, "name", None), self.stop_bits)
        return (self.baud_rate, self.data_bits, parity, stop) == self.framing   # PyVISA's constants, or plain numbers

    def write(self, cmd):
        self.sent.append(cmd)
        reply = self.answer(cmd) if self.ok() else None
        if reply is not None:
            self.pending.append(reply)

    def write_raw(self, data):
        self.write(data.decode())

    def read(self):
        if not self.pending:
            raise TimeoutError("VI_ERROR_TMO")
        return self.pending.pop(0)

    def query(self, cmd, delay=None):
        self.write(cmd)
        return self.read()

    def close(self):
        pass


class Ports:
    def __init__(self, lines):
        self.lines = lines

    def list_resources(self, query="?*::INSTR"):
        return tuple(self.lines)

    def open_resource(self, name):
        return self.lines[name]


def smc100():
    """An SMC100 at 57600 8N1 as its manual describes it: not referenced until homed, then a move goes a step closer
    each time it is asked where it is."""
    state = {"state": "0A", "pos": 0.0, "target": 0.0, "err": "@"}

    def step():
        if state["state"] == "28":
            state["pos"] = min(state["target"], state["pos"] + 2.0)
            if state["pos"] == state["target"]:
                state["state"] = "33"

    def answer(cmd):
        if cmd == "1VE":
            return "1VE SMC_CC - Controller-driver version 3. 0. 0"
        if cmd in ("1TS", "1TP"):
            step()
            return "1TS0000%s" % state["state"] if cmd == "1TS" else "1TP%.6f" % state["pos"]
        if cmd == "1TE":
            e, state["err"] = state["err"], "@"
            return "1TE%s" % e
        if cmd == "1VA?":
            return "1VA2.500000"
        if cmd == "1PA?":
            return "1PA%.6f" % state["target"]
        if cmd == "1ID?":
            return "1IDLTA-HS"
        if cmd == "1OR":
            state["state"] = "32"
        elif cmd.startswith("1PA"):
            if state["state"] not in ("32", "33"):
                state["err"] = "H"
            else:
                state["target"], state["state"] = float(cmd[3:]), "28"
        return None
    return state, SerialLine((57600, 8, "none", 1.0), answer)


def test_the_smc100_card_identifies_homes_and_waits_for_a_move():
    """l06, l07: drafted SMC100 cards guessed the identity, took 1TE@ for an error and left moves unawaited, so runs
    aborted at point 0. Kelvin's card asks 1VE at 57600 8N1, knows 1TE@, and a move returns once the stage is there."""
    from labharness.discovery import identify
    reg = builtin_registry()
    _, line = smc100()
    found = identify("ASRL6::INSTR", reg, Ports({"ASRL6::INSTR": line}))
    assert found.card_id == "newport/smc100" and found.settings == "57600 8N1"
    state, line = smc100()
    lab = lab_of(reg, {"stage": {"card": "newport/smc100", "address": "ASRL6::INSTR", "limits": {"position": [0, 25]}}},
                 {"stage": VisaBackend("stage", reg.get("newport/smc100"), "ASRL6::INSTR", OneLine(line))})
    assert lab.health_check()[0].ok and lab.instruments["stage"].instrument_error() is None   # 1TE@: no error
    gate = SafetyGate(lab, ScriptedApprover([True]))
    assert gate.act("stage", "home").ok and state["state"] == "32"             # asks first: the stage moves
    r = gate.set("stage", "position", 12.0)
    assert r.ok, r.message
    assert lab.read("stage", "position_actual") == 12.0 and lab.read("stage", "state") == "ready (from moving)"
    assert gate.raw("stage", "1PW1").action == "refused"                       # configuration state: never
    assert gate.raw("stage", "1SR30").action == "refused"                      # its software limits: never
    assert gate.set("stage", "position", 30).action == "refused"               # the lab's travel


def test_a_move_the_smc100_refuses_fails_at_once():
    """Cards to gate: before homing the SMC100 refuses a move (1TEH), and Kelvin waited out the card's whole 63 s
    timeout for it. With the card's busy status and the gate's error check after the write, the refusal comes back at
    once, and the next good move isn't blamed for it."""
    reg = builtin_registry()
    state, line = smc100()
    lab = lab_of(reg, {"stage": {"card": "newport/smc100", "address": "ASRL6::INSTR", "limits": {"position": [0, 25]}}},
                 {"stage": VisaBackend("stage", reg.get("newport/smc100"), "ASRL6::INSTR", OneLine(line))})
    assert lab.health_check()[0].ok
    gate = SafetyGate(lab, ScriptedApprover([True] * 3))
    start = lab.clock.now()
    r = gate.set("stage", "position", 5.0)
    assert not r.ok and "1TEH" in r.message and lab.clock.now() - start < 5, r.message
    assert gate.act("stage", "home").ok
    r = gate.set("stage", "position", 12.0)
    assert r.ok, r.message


def test_the_tpg36x_card_reads_both_gauges_with_their_status():
    """l06, l07: the gauge was drafted twice and lost its identity check; Kelvin's card asks AYT through the ENQ
    protocol, reads each gauge with its status (a missing gauge reads 2.0E-2 with status 5) and checks the unit."""
    from labharness.discovery import identify
    reg = builtin_registry()
    replies = {"AYT": "TPG362,PTG28290,44990000,010100,010100", "PR1": "0,7.9954E-07", "PR2": "5,2.0000E-02",
               "UNI": "0", "TID": "PKR,noSen", "ERR": "0000"}
    pending = {}

    def answer(cmd):
        if cmd in replies:
            pending["next"] = replies[cmd]
            return "\x06"
        if cmd == "\x05":
            return pending.pop("next", None)
        return "\x15"
    found = identify("ASRL3::INSTR", reg, Ports({"ASRL3::INSTR": SerialLine((9600, 8, "none", 1.0), answer)}))
    assert found.card_id == "pfeiffer/tpg36x" and found.serial["enq"] is True
    lab = lab_of(reg, {"gauge": {"card": "pfeiffer/tpg36x", "address": "ASRL3::INSTR"}},
                 {"gauge": VisaBackend("gauge", reg.get("pfeiffer/tpg36x"), "ASRL3::INSTR",
                                       OneLine(SerialLine((9600, 8, "none", 1.0), answer)))})
    assert lab.health_check()[0].ok, lab.health_check()[0].problems
    assert lab.read("gauge", "P1") == pytest.approx(7.9954e-7) and lab.read("gauge", "status1") == "ok"
    assert lab.read("gauge", "status2") == "no sensor"
    replies["UNI"] = "1"                                                       # Torr: the readings' unit is wrong
    assert not lab.health_check()[0].ok


def test_the_ami_430_card_knows_its_greeting_rate_and_units():
    """l01, l08, l10: the 430's LAN greeting shifted every reply; there was no ramp rate for how long a ramp takes,
    and CONF:FIELD:UNITS 0 went out unasked and switched the magnet to kG."""
    reg = builtin_registry()
    card = reg.get("ami/430")
    assert card.connection["greeting_lines"] == 2
    spec = card.params["B_target"]
    assert spec.rate_param == "ramp_rate" and card.params["ramp_rate"].get_cmd.render() == "RAMP:RATE:FIELD:1?"
    replies = {"*IDN?": "AMERICAN MAGNETICS INC.,MODEL 430,2.50", "FIELD:UNITS?": "1", "RAMP:RATE:UNITS?": "0",
               "RAMP:RATE:FIELD:1?": "0.010000,9.0000", "FIELD:MAGnet?": "0.000000", "CURRent:MAGnet?": "0.0",
               "FIELD:TARGet?": "0.000000", "STATE?": "8", "SYSTem:ERRor?": "0,\"No error\""}
    line = Line(lambda cmd: replies.get(cmd), greeting=["American Magnetics Model 430 IP Interface", "Hello."])
    lab = lab_of(reg, {"magnet": {"card": "ami/430", "address": "TCPIP0::10.0.0.5::7180::SOCKET"}},
                 {"magnet": VisaBackend("magnet", card, "TCPIP0::10.0.0.5::7180::SOCKET", OneLine(line))})
    assert lab.health_check()[0].ok, lab.health_check()[0].problems
    gate = SafetyGate(lab, ScriptedApprover([]))
    assert "~10 s" in gate._describe_move(lab.instruments["magnet"], spec, 0.0, 0.1)   # 0.1 T at 0.01 T/s
    assert gate.raw("magnet", "CONF:FIELD:UNITS 0").action == "refused"
    replies["RAMP:RATE:UNITS?"] = "1"                                         # per minute: the estimate would be off
    assert not lab.health_check()[0].ok


def test_the_sr830_card_has_its_reference_source_and_slope():
    """l01, l06: locking an SR830 to a 6221's phase marker or a chopper needed FMOD and RSLP, which the card lacked."""
    card = builtin_registry().get("srs/sr830")
    ref, slope = card.params["reference"], card.params["reference_slope"]
    assert ref.set_cmd.render(ref.to_wire("external")) == "FMOD 0" and ref.from_wire("1") == "internal"
    assert slope.set_cmd.render(slope.to_wire("TTL rising")) == "RSLP 1" and slope.from_wire("2") == "TTL falling"


def test_a_draft_for_a_model_kelvins_card_does_not_fit_gets_its_own_id(tmp_path):
    """The collection's 6221 entry also covers the 6220; Kelvin's 6221 card doesn't fit a 6220 (no waveforms), so a
    draft for one takes keithley/6220, with the 6220's own identity, and never Kelvin's id or its instruments."""
    import re
    from labharness import autocard
    from labharness.virtual_lab import kelvin_ids
    entry = {"id": "keithley/6221", "maker": "Keithley (Tektronix)", "model": "6221", "kind": "current-source",
             "name": "Keithley 6221 AC and DC current source",
             "identify": {"idn": "^KEITHLEY INSTRUMENTS INC\\.,\\s*MODEL 622[01],"},
             "manuals": [{"url": "https://download.tek.com/manual/622x.pdf", "access": "public"}]}

    class Catalog:
        def instrument_for(self, idn):
            return entry
    page = ("<html><body><h1>Model 6220 and 6221 reference manual</h1>%s</body></html>" % (
        "<p>SOURce:CURRent:AMPLitude &lt;n&gt; sets the amplitude; SOURce:CURRent:AMPLitude? reads it. OUTPut:STATe "
        "ON|OFF switches the output. SYSTem:ERRor? reads the error queue.</p>" * 60)).encode()
    drafted = ("```yaml\ncard: 1\nid: keithley/6221\nmodel: Model 6220\nkind: current-source\nstatus: draft\n"
               "error_query: \"SYSTem:ERRor?\"\nparameters:\n  I: {kind: drive, unit: A, set: \"SOURce:CURRent:AMPLitude "
               "{value}\", get: \"SOURce:CURRent:AMPLitude?\", limits: [-0.105, 0.105], safe: 0}\n```")
    r = autocard.make_card({"address": "GPIB0::12::INSTR", "idn": "KEITHLEY INSTRUMENTS INC.,MODEL 6220,4321,D04"},
                           tmp_path / "cards", lambda system, prompt: (drafted, "test:model"), lambda url: page,
                           kelvin_ids(), catalog=Catalog())
    assert r.get("card_id") == "keithley/6220", r
    card, problems, _ = check((tmp_path / "cards" / "keithley_6220.yaml").read_text(encoding="utf-8"))
    assert problems == [] and re.search(card.idn_pattern, "KEITHLEY INSTRUMENTS INC.,MODEL 6220,4321,D04", re.I)
    assert not re.search(card.idn_pattern, "KEITHLEY INSTRUMENTS INC.,MODEL 6221,1,D04", re.I)   # never Kelvin's 6221


# ------------------------------------------------------------------ 16: reconnecting takes the card as it is now

PLATE = """card: 1
id: student/hotplate
model: Student hot plate
kind: temperature-controller
status: draft
identify: { idn: "^HEATER" }
connection: { ack: OK }
parameters:
  T: { label: Temperature, kind: measure, unit: degC, get: "T?" }
  setpoint: { label: Setpoint, kind: drive, unit: degC, set: "SET {value}", get: "SET?", limits: [0, 100], safe: 0 }
"""


@pytest.fixture
def plate(tmp_path):
    """l07's hot plate, as it is: OK after each set only, and a card that said ack (an OK after every command)."""
    from labharness.adapters import set_resource_manager
    sent, state = [], {"SET?": "0", "T?": "22.4", "*IDN?": "HEATER-ARDUINO v2"}

    def answer(cmd):
        if cmd in state:
            return state[cmd]
        if cmd.startswith("SET "):
            state["SET?"] = cmd.split()[1]
            return "OK"
        return None
    line = Line(answer)
    line.sent = sent

    class Rm:
        def open_resource(self, name):
            line.pending.clear()
            return line
    set_resource_manager(Rm())
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "student_hotplate.yaml").write_text(PLATE, encoding="utf-8")
    (tmp_path / "lab.yaml").write_text("lab: Bench\ninstruments:\n  hotplate: { card: student/hotplate, address: "
                                       "\"ASRL4::INSTR\", limits: { setpoint: [0, 60] } }\n", encoding="utf-8")
    yield sent, state
    set_resource_manager(None)


def test_reconnecting_an_instrument_uses_its_card_as_it_is_now(tmp_path, plate):
    """l01, l03, l06, l07: every card fix needed a Kelvin restart, as reconnect_instrument kept the card the
    instrument connected with; one run went out with the old card and heated the plate. Reconnecting takes the card
    from disk, and connects again when its connection settings changed."""
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    from labharness.virtual_lab import build_lab, lab_registry
    sent, state = plate
    lab = build_lab(tmp_path / "lab.yaml", registry=lab_registry(tmp_path))
    rt = LabRuntime(lab, ScriptedApprover([]), tmp_path / "data")
    rt.home, rt.cards_dir = tmp_path, tmp_path / "cards"
    with pytest.raises(Exception):
        lab.read("hotplate", "T")                                              # ack after a query: never comes
    fixed = PLATE.replace("connection: { ack: OK }", "connection: { set_reply: \"^OK$\" }")
    (tmp_path / "cards" / "student_hotplate.yaml").write_text(fixed, encoding="utf-8")
    said = rt.reconnect_instrument("hotplate")
    assert "card student/hotplate as it is now" in said and "connected again" in said, said
    assert lab.read("hotplate", "T") == pytest.approx(22.4)
    assert lab.instruments["hotplate"].card.connection == {"set_reply": "^OK$"}
    assert rt.reconnect_instrument("hotplate") == "hotplate is connected, with its card as it is now"   # no change
    broken = fixed.replace("  setpoint:", "  target:")                         # the lab config limits setpoint
    (tmp_path / "cards" / "student_hotplate.yaml").write_text(broken, encoding="utf-8")
    said = rt.reconnect_instrument("hotplate")
    assert "keeps the card it connected with" in said and "setpoint" in said, said
    assert "setpoint" in lab.instruments["hotplate"].card.params


# ------------------------------------------------------------------ 15: Kelvin finds the cards it has


class Catalog:
    """The instrument collection's copy: one instrument with a card Kelvin also has, one without a card."""

    def index(self):
        return {"instruments": [
            {"id": "ami/430", "name": "American Magnetics Model 430", "maker": "American Magnetics", "model": "430",
             "kind": "magnet-supply", "card": {"path": "instruments/x/card.yaml", "sha256": "0" * 64}},
            {"id": "keysight/b2987", "name": "Keysight B2987B electrometer", "maker": "Keysight", "model": "B2987B",
             "kind": "electrometer", "identify": {"idn": "^Keysight Technologies,B2987"},
             "manuals": [{"url": "https://www.keysight.com/b2980-programming.pdf", "access": "public"}]}]}

    def instrument_for(self, idn):
        return self.index()["instruments"][1] if idn.startswith("Keysight Technologies,B2987") else None

    def cards(self):
        return []


def test_cards_are_found_by_scheme_name_and_identity():
    """l03, l05, l09, l10: Kelvin said there was no card for qd://, ami/430 and pi/gcs-piezo, and drafted worse ones.
    A driver address, a maker and model, or an identity reply find the card Kelvin has."""
    from labharness.discovery import by_scheme, card_for, cards_named, find_cards
    reg = builtin_registry()
    assert [c.id for c in by_scheme("qd://127.0.0.1:5000", reg)] == ["qd/multivu"]
    assert [c.id for c in by_scheme("andor://0?sdk=2", reg)] == ["andor/ccd"]
    assert [c.id for c in by_scheme("andor://0?sdk=3", reg)] == ["andor/scmos"]
    assert by_scheme("http://10.0.0.7/v1", reg) == [] and by_scheme("GPIB0::8::INSTR", reg) == []
    assert [c.id for c in cards_named("american-magnetics/430", reg)] == ["ami/430"]
    assert [c.id for c in cards_named("AMI 430", reg)] == ["ami/430"]
    assert [c.id for c in cards_named("Oxford Instruments Mercury iTC", reg)] == ["oxford/mercury-itc"]
    assert [c.id for c in cards_named("Newport SMC100", reg)] == ["newport/smc100"]   # not R&S's SMC100A
    card, how = card_for("qd://127.0.0.1:5000", reg)
    assert card.id == "qd/multivu" and "qd://" in how
    card, how = card_for("GPIB0::8::INSTR", reg, idn="Stanford_Research_Systems,SR830,s/n86422,ver1.07")
    assert card.id == "srs/sr830" and how == "its identity reply"
    assert card_for("GPIB0::8::INSTR", reg, name="acme/nothing")[0] is None
    found = find_cards("AMI 430", reg, Catalog())
    assert [c.id for c in found["cards"]] == ["ami/430"] and found["entries"] == []   # its card: no entry twice
    found = find_cards("electrometer", reg, Catalog())
    assert found["cards"] == [] and [e["id"] for e in found["entries"]] == ["keysight/b2987"]
    found = find_cards("Keysight Technologies,B2987B,MY123,1.0", reg, Catalog())
    assert [e["id"] for e in found["entries"]] == ["keysight/b2987"]


def test_kelvin_uses_its_card_before_drafting_or_shadowing_one(tmp_path):
    """l07: Kelvin drafted its own oxford/mercury-itc and saved it as this lab's version of the built-in card, which
    lacked the card's set_reply and broke a run. find_card shows the card, draft_card won't write one Kelvin has, and
    save_card takes a Kelvin card's id only when asked to make this lab's version (version: true)."""
    pytest.importorskip("mcp")
    from pathlib import Path
    from labharness import build_virtual_lab
    from labharness.agent.runtime import LabRuntime
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    rt.home, rt.cards_dir, rt.collection = tmp_path, tmp_path / "cards", Catalog()
    rt.card_writer = lambda found: pytest.fail("a card Kelvin has was drafted again")
    said = rt.find_card("Mercury iTC")
    assert "oxford/mercury-itc" in said and "Kelvin's" in said and "add_instrument" in said
    said = rt.draft_card("TCPIP0::192.168.1.60::7020::SOCKET", maker="Oxford Instruments", model="Mercury iTC")
    assert said.startswith("Kelvin already has a card for it: oxford/mercury-itc")
    assert "keysight/b2987" in rt.find_card("Keysight B2987") and "b2980-programming.pdf" in rt.find_card("B2987")
    mercury = Path(builtin_registry().get("oxford/mercury-itc").source).read_text(encoding="utf-8")
    worse = mercury.replace("status: community", "status: draft").replace(', set_reply: ":VALID$"', "")
    said = rt.save_card(worse)
    assert said.startswith("not saved: Kelvin already has a card oxford/mercury-itc: use it"), said
    assert not (tmp_path / "cards").exists() or not list((tmp_path / "cards").glob("*.yaml"))
    assert rt.save_card(worse, version=True).startswith("saved draft card oxford/mercury-itc")


def test_add_instrument_takes_the_card_a_name_means(tmp_path):
    """l10: Kelvin guessed card ids (american-magnetics/430) and, finding none, drafted. add_instrument takes the
    card a maker and model name means, and says which several fit."""
    pytest.importorskip("mcp")
    from labharness.adapters import set_resource_manager
    from test_add_instrument import SMU_ONLY, file_runtime
    set_resource_manager(Bus({"GPIB0::24::INSTR": Device("keithley/2400", name="k2400"),
                              "GPIB0::8::INSTR": Device("srs/sr830", name="sr830")}))
    try:
        rt = file_runtime(tmp_path, SMU_ONLY)
        rt.gate.approver = ScriptedApprover([True])
        said = rt.add_instrument("GPIB0::8::INSTR", card="Stanford SR830", name="lockin")
        assert said.startswith("added lockin (Stanford Research SR830") and "card srs/sr830" in said, said
        assert "found by its name" in said
        said = rt.add_instrument("GPIB0::9::INSTR", card="Keithley 24")
        assert said.startswith("not added: ") and "find_card" in said, said
    finally:
        set_resource_manager(None)


# ------------------------------------------------------------------ 13b: the instrument collection, used


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows' 260-character path limit")
def test_the_collection_is_kept_under_a_long_home_folder(tmp_path):
    """l08, l09, l10: five of ten Kelvins could not keep the collection (its deepest file reached 253 characters,
    longer home folders went over Windows' 260), and one path aborted the whole update, saying so only in the log."""
    from labharness.collection import Collection
    from test_collection import A, WIDGET, GitHub
    home = tmp_path / ("a-rather-long-folder-name-" * 6) / ("for-a-tester-home-" * 3)
    gh = GitHub()
    gh.publish(A, cards=[WIDGET])
    c = Collection(home)
    deepest = max(len(str(c.dir / A[:12] / p)) for p in gh.commits[A] if p != "index.json")
    assert deepest > 260
    status = c.sync(gh)
    assert status["error"] is None and [card.id for card in c.cards()] == ["acme/widget"]


def test_a_file_the_collection_cant_keep_leaves_out_its_card_and_is_said(tmp_path, monkeypatch):
    """One file this computer can't write leaves out that card only, and the portal hears of it once; a failing
    update is said there too, once, as it starts to fail."""
    from labharness import collection as coll
    from test_collection import A, B, WIDGET, GitHub
    other = WIDGET.replace("acme/widget", "acme/gadget").replace("ACME Widget source", "ACME Gadget").replace(
        "^ACME,WIDGET", "^ACME,GADGET")
    gh = GitHub()
    gh.publish(A, cards=[WIDGET, other])
    write = coll._write

    def refuse(path, data):
        if "acme-gadget" in str(path):
            raise OSError("[WinError 3] The system cannot find the path specified")
        write(path, data)
    monkeypatch.setattr(coll, "_write", refuse)
    c = coll.Collection(tmp_path)
    told = []
    c.notify = lambda title, text: told.append((title, text))
    status = c.sync(gh)
    assert [card.id for card in c.cards()] == ["acme/widget"]                  # the rest of the collection works
    assert "could not be stored" in status["error"] and "acme-gadget" in status["error"]
    assert any("acme/gadget" in x for x in status["left_out"]) and len(told) == 1
    monkeypatch.setattr(coll, "_write", write)
    gh.publish(B, cards=[WIDGET, other])
    assert c.sync(gh)["error"] is None and len(c.cards()) == 2
    gh.down = True
    c.sync(gh)
    c.sync(gh)
    assert len(told) == 2 and "Kelvin uses the copy it has meanwhile" in told[-1][1]   # once, when it starts


def test_first_start_takes_the_collections_card_before_drafting(tmp_path):
    """l08: the first-run card writer started before the collection had arrived, guessed manuals on the web and never
    tried the collection's. Setting up, Kelvin brings the collection up to date first and uses its card."""
    pytest.importorskip("mcp")
    from labharness.adapters import set_resource_manager
    from labharness.autosetup import AutoSetup
    from test_autosetup import FakeSettings, runtime_for
    from test_collection import WIDGET

    class Fresh:   # the collection: its copy arrives with this sync
        def __init__(self):
            self.synced = 0

        def sync(self):
            self.synced += 1
            return {"error": None}

        def cards(self):
            return [parse_card(__import__("yaml").safe_load(WIDGET), "collection")] if self.synced else []

        def index(self):
            return {}
    widget = Device(idn="ACME,WIDGET 9000,1,1.0")
    widget.twin = type("Answers", (), {"query": staticmethod(lambda cmd: "0"), "write": staticmethod(lambda c: None)})()
    set_resource_manager(Bus({"GPIB0::5::INSTR": widget}))
    try:
        rt = runtime_for(tmp_path)
        rt.collection = Fresh()
        unknown = [{"address": "GPIB0::5::INSTR", "idn": "ACME,WIDGET 9000,1,1.0", "card_id": None, "model": "",
                    "kind": "", "how": "no card matches the reply"}]
        setup = AutoSetup(rt, FakeSettings(), tmp_path, scan=lambda: unknown,
                          make_card=lambda found: pytest.fail("drafted a card the collection has"))
        status = setup.run()
        assert rt.collection.synced == 1 and status["state"] == "done", status
        assert "card: acme/widget" in (tmp_path / "lab.yaml").read_text(encoding="utf-8")
    finally:
        set_resource_manager(None)


# ------------------------------------------------------------------ 17: trying a card, and a draft card's sets


class Source6221Short:
    """A source that takes its commands in short or long SCPI form, but not the long form a draft invented
    (SOURce:WAVeform: -113, Undefined header, and nothing changes)."""

    def __init__(self):
        self.function, self.errors, self.sent = "SIN", [], []

    def answer(self, cmd):
        self.sent.append(cmd)
        if cmd == "*IDN?":
            return "KEITHLEY INSTRUMENTS INC.,MODEL 6221,1,D04"
        if cmd == "SYST:ERR?":
            return self.errors.pop(0) if self.errors else "0,\"No error\""
        if cmd == "SOUR:WAVE:FUNC?":
            return self.function
        if cmd.startswith("SOUR:WAVE:FUNC "):
            self.function = cmd.split()[1][:3].upper()
            return None
        if "?" in cmd:
            self.errors.append("-113,\"Undefined header\"")
            raise TimeoutError("VI_ERROR_TMO")
        self.errors.append("-113,\"Undefined header\"")
        return None


def card_6221(set_cmd, status="draft"):
    return parse_card({"card": 1, "id": "lab/6221", "model": "6221 (lab draft)", "kind": "current-source",
                       "status": status, "error_query": "SYST:ERR?",
                       "identify": {"idn": "MODEL 6221"},
                       "parameters": {
                           "function": {"kind": "measure", "type": "enum", "values": {"sine": "SIN", "square": "SQU"},
                                        "set": set_cmd, "get": "SOUR:WAVE:FUNC?"},
                           "V": {"kind": "measure", "unit": "V", "get": "MEAS:VOLT?"}}})


def test_trying_a_card_checks_its_set_command_and_never_shows_a_skip_as_a_check():
    """l01: a draft card's long forms (SOURce:WAVeform:FUNCtion) got -113 from the 6221, and the first run reported
    the change as made; try_card had shown only readings, its skipped reads as green checks. Trying writes one
    setting with the value it has and reads the error queue, and a read it skips is listed as not tried."""
    from labharness import cardtry
    from labharness.adapters import set_resource_manager
    source = Source6221Short()
    set_resource_manager(OneLine(Line(source.answer)))
    try:
        bad = cardtry.try_card(card_6221("SOURce:WAVeform:FUNCtion {value}"), "GPIB0::12::INSTR")
        assert bad["write_check"]["ok"] is False and "-113" in bad["write_check"]["error"]
        said = cardtry.summary(bad)
        assert "set command: function FAILED" in said and "V: NOT TRIED" in said and "1 not tried" in said
        good = cardtry.try_card(card_6221("SOUR:WAVE:FUNC {value}"), "GPIB0::12::INSTR")
        assert good["write_check"] == {"name": "function", "value": "sine", "unit": "", "ok": True,
                                       "sent": "SOUR:WAVE:FUNC SIN"}
        assert source.function == "SIN"                                       # nothing changed
        assert not any(c.startswith("MEAS") for c in source.sent)             # a read that measures: not sent
    finally:
        set_resource_manager(None)


def test_a_draft_cards_switch_or_mode_is_read_back_before_it_counts_as_set():
    """On a draft card, a mode its instrument didn't take is reported, not claimed (the card has no error query
    here, which was the case on l01's draft)."""
    card = parse_card(dict(card_6221("SOURce:WAVeform:FUNCtion {value}").data, error_query=None))
    reg = CardRegistry()
    reg.add(card)
    source = Source6221Short()

    class Backend:
        simulated = False

        def write(self, cmd):
            source.answer(cmd)

        def query(self, cmd):
            return source.answer(cmd)

        def close(self):
            pass
    lab = lab_of(reg, {"cs": {"card": "lab/6221", "address": "GPIB0::12::INSTR"}}, {"cs": Backend()})
    gate = SafetyGate(lab, ScriptedApprover([True]))
    r = gate.set("cs", "function", "square")
    assert not r.ok and "the draft card's command may not be one it takes" in r.message, r.message
    good = parse_card(dict(card_6221("SOUR:WAVE:FUNC {value}").data, error_query=None))
    reg.put(good)
    lab = lab_of(reg, {"cs": {"card": "lab/6221", "address": "GPIB0::12::INSTR"}}, {"cs": Backend()})
    assert SafetyGate(lab, ScriptedApprover([True])).set("cs", "function", "square").ok and source.function == "SQU"


# ------------------------------------------------------------------ 19: the first-run scan


class Board:
    """A port the scan opens: an Arduino resets each time (counted), an ANC300's command line echoes."""

    def __init__(self, answer, echo=False, framing=(9600, 8, "none", 1.0)):
        self.opened, self.answer, self.echo, self.framing = 0, answer, echo, framing

    def open_resource(self, name):
        self.opened += 1
        line = SerialLine(self.framing, lambda cmd: self.answer(cmd))
        if self.echo:
            original = line.write

            def write(cmd):
                line.pending.append(cmd)   # the command, echoed first
                original(cmd)
            line.write = write
        return line


def test_an_arduino_is_opened_once_and_an_echoing_command_line_still_answers():
    """l07: the scan reset a student's Arduino hot plate once per framing tried (seven times on COM4, heater off each
    time); l05: the ANC300's command line echoes, and the echo was taken for its identity."""
    from labharness.discovery import identify
    reg = builtin_registry()
    arduino = Board(lambda cmd: "HEATER-ARDUINO v2" if cmd == "*IDN?" else None)
    found = identify("ASRL4::INSTR", reg, arduino, usb={"ASRL4::INSTR": "2341:0043"})
    assert arduino.opened == 1 and found.idn == "HEATER-ARDUINO v2"
    plain = Board(lambda cmd: None)
    identify("ASRL5::INSTR", reg, plain, usb={"ASRL5::INSTR": "0403:6001"})   # an FTDI cable: every framing, as before
    assert plain.opened > 5
    anc = Board(lambda cmd: "attocube ANC300 controller version 1.1.0-1304 2013-10-17 08:16" if cmd == "ver" else None,
                echo=True, framing=(38400, 8, "none", 1.0))
    card = reg.get("attocube/anc300")
    reg.put(parse_card(dict(card.data, connection={"baud_rate": 38400, "framing": "8N1",
                                                   "read_termination": "\r\n", "write_termination": "\r\n"})))
    found = identify("ASRL7::INSTR", reg, anc)
    assert found.card_id == "attocube/anc300" and found.idn.startswith("attocube ANC300")


class ArduinoSketch(SerialLine):
    """A student's sketch, as labsim's arduino_heater: opening the port resets the board, which loses what is sent
    until it has started and printed READY; it answers ID? and refuses anything else with ERR."""

    def __init__(self):
        super().__init__((9600, 8, "none", 1.0), lambda cmd: "HEATER-ARDUINO v2" if cmd == "ID?" else "ERR")
        self.booted = False

    def write(self, cmd):
        if not self.booted:
            self.sent.append(cmd)   # lost in the bootloader
            return
        super().write(cmd)

    def read(self):
        if not self.booted:   # waited through the boot: its banner
            self.booted = True
            return "READY"
        return super().read()


def test_an_arduino_sketchs_banner_and_refusal_are_not_its_identity():
    """The orchestrator's labsim check: the scan sent *IDN? into the Arduino's bootloader and took the sketch's READY
    banner for its identity, so first-run setup went on to draft an instrument named READY. The banner is read as it
    starts, ERR is no answer, and the board is reported as there, without an identity, opened once."""
    from labharness.discovery import identify
    opened = []

    class Port:
        def open_resource(self, name):
            opened.append(name)
            return ArduinoSketch()
    found = identify("ASRL4::INSTR", builtin_registry(), Port(), usb={"ASRL4::INSTR": "2341:0043"})
    assert opened == ["ASRL4::INSTR"] and found.idn == "" and found.card_id is None
    assert found.how == ("an Arduino-type board (USB 2341:0043): it printed READY as it started and didn't answer "
                         "*IDN?; give Kelvin its commands to make a card"), found.how
    assert found.how in found.line() and found.serial["baud_rate"] == 9600   # the framing it talks at, for its card


def test_a_laser_gets_its_own_rating_as_its_limit():
    """l05, l06: first-run lab configs gave 20 mW and 10 mW OBIS lasers the card's 0.1 W."""
    from labharness.discovery import instrument_block, rated_limits
    reg = builtin_registry()
    obis = reg.get("coherent/obis")
    assert rated_limits(obis, "Coherent, Inc - OBIS 520nm LX 20mW - V1.0.1 - Dec 14 2010") == {"power": (0.0, 0.02)}
    assert rated_limits(obis, "Coherent, Inc - OBIS 488nm LX 150mW - V1.0.1") == {}        # beyond the card: as is
    block = "\n".join(instrument_block({"name": "pump", "card": "coherent/obis", "address": "ASRL8::INSTR",
                                        "note": "Coherent, Inc - OBIS 520nm LX 20mW - V1.0.1"}, reg))
    assert "power: [0, 0.02]" in block and "its own rating" in block


def test_the_manual_search_passes_over_listing_pages_and_brochures():
    """l07: the search took a page listing many makers' manuals and a brochure for the 6221's manual. A page of
    document links is followed, never taken for the manual; a brochure that isn't a command reference is passed over."""
    pytest.importorskip("pypdf")
    from labharness import autocard
    from test_autocard import COMMANDS
    from test_autosetup import tiny_pdf
    listing = "https://manuals.example/keithley"
    brochure = "https://www.maker.example/6221-brochure.pdf"
    manual = "https://www.maker.example/622x-reference.pdf"
    links = "".join('<a href="https://manuals.example/m%d.pdf">Manual %d</a>' % (i, i) for i in range(12))
    pages = {
        listing: ("<html><body><h1>Keithley manuals: Model 6221, 6220, 2400...</h1><p>%s</p>%s"
                  '<a href="%s">Model 6221 brochure</a><a href="%s">Model 6221 reference manual</a></body></html>'
                  % (" ".join(COMMANDS) * 40, links, brochure, manual)).encode(),
        brochure: tiny_pdf([["Model 6221 Brochure: AC and DC current source", "Data sheet"] + COMMANDS * 2]),
        manual: tiny_pdf([["Model 6220/6221 Reference Manual"] + COMMANDS * 8]),
    }
    m = autocard.find_manual("KEITHLEY INSTRUMENTS INC.", "MODEL 6221", lambda system, prompt: (listing, "m"),
                             lambda url: pages[url] if url in pages else (_ for _ in ()).throw(OSError("404")))
    assert m is not None and m["url"] == manual, m and m["url"]


def test_the_scan_finds_a_time_tagger_through_its_windows_module(monkeypatch):
    """Swabian's Windows installer puts TimeTagger on the path, not Swabian.TimeTagger; the driver took either, the
    scan only the second, and missed the Time Tagger (l05)."""
    import sys
    import types
    from labharness.drivers import discover_swabian
    monkeypatch.setitem(sys.modules, "Swabian", None)
    monkeypatch.setitem(sys.modules, "pulsestreamer", None)
    monkeypatch.setitem(sys.modules, "TimeTagger", types.SimpleNamespace(scanTimeTagger=lambda: ["2138000XYZ"]))
    assert discover_swabian({}) == [("timetagger://2138000XYZ", "Swabian Instruments,Time Tagger,2138000XYZ")]
