"""What trying Kelvin on new kinds of instruments taught it: the pages of a 200-page programming manual a card is
written from, a generator whose ranges are in another document, the model's notes, Pfeiffer's ACK/ENQ protocol, and
values an instrument clips without saying so."""
import types

import pytest

from labharness import autocard, builtin_registry
from labharness.adapters import AdapterError, VisaBackend, set_resource_manager
from labharness.cards import parse_card
from labharness.discovery import identify
from labharness.gate import SafetyGate, ScriptedApprover
from labharness.instrument import Instrument
from labharness.labconfig import InstrumentConfig
from labharness.scpi import Template, is_query
from labharness.virtual_lab import build_lab
from test_hardware import Bus, Device, Resource

pytest.importorskip("pypdf")
from test_autocard import CARD, MANUAL  # noqa: E402
from test_autosetup import tiny_pdf  # noqa: E402


def test_the_pages_that_define_outputs_rank_above_the_rest():
    """As in Tektronix's AFG31000 manual: OUTPut[:STATe] is an output (STATe does not make it housekeeping), a
    channel number (OUTPut1, ELEM[n]) is read as the bare command, deep commands count less, contents lines not at
    all; Lake Shore names its commands by label and heading."""
    output = ("OUTPut[1|2][:STATe]\nThis command sets or queries the output.\nSyntax\nOUTPut[1|2][:STATe]{ON|OFF|<NR1>}\n"
              "OUTPut[1|2][:STATe]?\nExamples\nOUTPut1:STATe ON\n")
    sequence = ("SEQuence:ELEM[n]:GOTO:INDex\nThis command sets the jump target.\nSyntax\nSEQuence:ELEM[n]:GOTO:INDex "
                "<target>\nSEQuence:ELEM[n]:GOTO:INDex?\nExamples\nSEQuence:ELEM1:GOTO:INDEX 6\n")
    status = "STATus:OPERation:ENABle\nSets the enable register.\nSyntax\nSTATus:OPERation:ENABle <mask>\n"
    assert autocard.detail_score(output) > autocard.detail_score(sequence) > autocard.detail_score(status) > 0
    assert autocard.detail_score("SETP Control Setpoint Command\nInput SETP <output>,<value>[term]\n") > 0
    assert autocard.detail_score("OUTPut[1|2][:STATe] (on page 61) Set or query the output\n" * 5) == 0


def test_the_pages_on_how_to_talk_to_it_go_to_the_model():
    """Pfeiffer's protocol pages have no command on them, yet without them a card can't say ENQ or 8N1."""
    protocol = ("1 start bit, 8 data bits, no parity bit, 1 stop bit, no hardware handshake. After each string wait "
                "for <ACK><CR><LF>; <ENQ> requests the data.")
    pages = ["Introduction", protocol, "PR1 <CR> measurement data gauge 1\nReceive: <ACK><CR><LF>\n" * 3, "Warranty"]
    excerpt = autocard.command_excerpt(pages)
    assert "[page 2]" in excerpt and "[page 3]" in excerpt and "Warranty" not in excerpt
    assert autocard.interface_score(protocol) >= 4 and autocard.interface_score("Warranty") == 0


def test_the_specification_pages_on_what_the_model_asked_about():
    pages = ["Table 5: Frequency specifications\n" + "1 Hz to 10 MHz\n" * 20,
             "Table 9: Amplitude specifications\n1 mVpp to 5 Vpp into 50 Ohm\n",
             "Table 12: Harmonic distortion\n" + "-60 dBc at 1 MHz\n" * 40]
    excerpt = autocard.specs_excerpt(pages, "W9000", ["amplitude"], limit=200)
    assert "[specifications, page 2]" in excerpt and "page 1]" not in excerpt and "page 3]" not in excerpt
    assert autocard.ranges_named(["Left out: x", "Ranges in: AFG31000 Specification; for: amplitude, offset and "
                                  "frequency"]) == ("AFG31000 Specification", ["amplitude", "offset", "frequency"])
    assert autocard.ranges_named(["Unsure: nothing"]) == ("", [])


FIRST = CARD + "\nRanges in: Widget 9000 specifications; for: amplitude"
WITH_SPECS = """```yaml
card: 1
id: acme/widget-9000
model: ACME Widget 9000 source
kind: generator
status: draft
identify: { idn: "^ACME,WIDGET 9000" }
parameters:
  amp:
    label: Amplitude
    kind: drive
    unit: V
    set: "VOLTage {value}"
    get: "VOLTage?"
    limits: [0.001, 5]
    safe: 0.001
    ramp: gate
    max_rate: 1
```
Limits from Table 9 of the specifications."""


@pytest.fixture
def maker():
    """The maker's site: a product page linking the programming manual and the specifications."""
    pages = {
        "https://www.acme.example/products/widget-9000":
            b'<html><body><a href="/docs/W9000-prog.pdf">Programming manual</a>'
            b'<a href="/docs/W9000-specs.pdf">Widget 9000 specifications</a></body></html>',
        "https://www.acme.example/docs/W9000-prog.pdf": tiny_pdf(MANUAL),
        "https://www.acme.example/docs/W9000-specs.pdf": tiny_pdf([["ACME WIDGET 9000 Specifications",
                                                                     "Table 9: Amplitude specifications",
                                                                     "Amplitude 1 mV to 5 V into 50 Ohm",
                                                                     "Frequency 1 Hz to 10 MHz"]]),
    }

    def fetch(url):
        if url not in pages:
            raise OSError("HTTP Error 404: Not Found")
        return pages[url]
    fetch.pages = pages
    return fetch


def test_kelvin_finds_the_document_the_manual_leaves_the_ranges_to(tmp_path, maker):
    prompts = []

    def model(system, prompt):
        prompts.append(prompt)
        if system == autocard.SUGGEST_SYSTEM:
            return "https://www.acme.example/products/widget-9000", "test:model"
        return (WITH_SPECS if "Its specifications" in prompt else FIRST), "test:model"
    ids = [c.id for c in builtin_registry()]
    r = autocard.make_card({"address": "GPIB0::5::INSTR", "idn": "ACME,WIDGET 9000,SN1,1.0"}, tmp_path / "cards", model,
                           maker, builtin_ids=ids)
    assert r["specs"] == "https://www.acme.example/docs/W9000-specs.pdf" and r["ranges_in"] == "", r
    assert "Widget 9000 specifications" in prompts[2] and "specifications" in prompts[2]   # the second search
    assert "[specifications, page 1]" in prompts[3] and "Table 9: Amplitude" in prompts[3]
    saved = (tmp_path / "cards" / "acme_widget-9000.yaml").read_text(encoding="utf-8")
    assert "(ranges from https://www.acme.example/docs/W9000-specs.pdf)" in saved and "amp:" in saved
    assert "Limits from Table 9" in saved and r["notes"] == ["Limits from Table 9 of the specifications."]

    del maker.pages["https://www.acme.example/docs/W9000-specs.pdf"]           # nowhere to be found
    r = autocard.make_card({"address": "GPIB0::5::INSTR", "idn": "ACME,WIDGET 9000,SN1,1.0"}, tmp_path / "c2", model,
                           maker, builtin_ids=ids)
    assert r["ranges_in"] == "Widget 9000 specifications" and r["specs"] == "" and "I was unsure" in " ".join(r["notes"])
    assert "# What the model left out or was unsure of:\n#   I was unsure about the range values." in (
        tmp_path / "c2" / "acme_widget-9000.yaml").read_text(encoding="utf-8")


class Pfeiffer(Resource):
    """A gauge controller on the fake bus: <ACK> or <NAK> to each mnemonic, its data on <ENQ>, and the readings it
    sends by itself from power-up still waiting in the buffer."""
    DATA = {"PR1": "0,1.2300E-06", "AYT": "TPG362,PTG28290,44990000,010100,010100", "ERR": "0000", "SEN,1,0": "1,0"}

    def __init__(self, dev):
        super().__init__(dev)
        self.pending = ["0,1.2300E-06,0,2.1000E-02"] * 2 if dev.streaming else []
        self.data = None

    def write(self, cmd):
        self.dev.sent.append(cmd)
        self.dev.streaming = False
        self.data = self.DATA.get(cmd)
        self.pending.append("\x06" if self.data else "\x15")

    def write_raw(self, raw):
        self.dev.sent.append("<ENQ>" if raw == b"\x05" else raw)
        self.pending.append(self.data or "0001")

    def query(self, cmd, delay=None):
        self.write(cmd)
        return self.read()


class PfeifferBus(Bus):
    def open_resource(self, name):
        return Pfeiffer(self.devices[name])


TPG = {"card": 1, "id": "pfeiffer/tpg362", "model": "Pfeiffer TPG 362", "kind": "vacuum-gauge", "status": "draft",
       "identify": {"query": "AYT", "idn": "^TPG36[12]"}, "error_query": "ERR",
       "connection": {"baud_rate": 9600, "read_termination": "\r\n", "write_termination": "\r", "enq": True},
       "parameters": {"p1": {"kind": "measure", "unit": "hPa", "get": "PR1", "field": 1}}}


def test_a_gauge_controller_that_answers_on_enq():
    tpg = Device(name="tpg")
    tpg.streaming = True
    bus = PfeifferBus({"ASRL6::INSTR": tpg})
    reg = builtin_registry()
    reg.remove("pfeiffer/tpg36x")
    found = identify("ASRL6::INSTR", reg, bus)                                 # no card that asks AYT
    assert found.idn == "" and found.card_id is None                           # its readings are not who it is
    card = parse_card(TPG, "test")
    tpg.streaming = True                                                       # switched off and on again
    found = identify("ASRL6::INSTR", builtin_registry(), bus)                  # Kelvin's own card asks AYT
    assert found.card_id == "pfeiffer/tpg36x" and found.idn.startswith("TPG362") and found.serial["enq"] is True

    tpg.streaming = True
    be = VisaBackend("tpg", card, "ASRL6::INSTR", bus)
    assert be.query("PR1") == "0,1.2300E-06"                                   # past its readings, then on ENQ
    assert tpg.sent[-2:] == ["PR1", "<ENQ>"]
    be.write("SEN,1,0")                                                        # acknowledged; no ENQ needed
    with pytest.raises(AdapterError, match="NAK"):
        be.write("XYZ")
    inst = Instrument("tpg", card, be, InstrumentConfig(name="tpg", card=card.id, address="ASRL6::INSTR"))
    assert inst.read("p1") == pytest.approx(1.23e-6) and inst.instrument_error() is None   # 0000: no error
    assert is_query("PR1", enq=True) and not is_query("SEN,1,0", enq=True) and not is_query("PR1")


def test_replies_in_short_form_and_commands_on_another_channel():
    shape = parse_card({"card": 1, "id": "t/g", "model": "G", "kind": "generator", "status": "draft",
                        "parameters": {"shape": {"kind": "measure", "type": "enum", "set": "FUNCtion {value}",
                                                 "get": "FUNCtion?", "values": {"sine": "SINusoid", "ramp": "RAMP"}}}})
    assert shape.params["shape"].from_wire("SIN") == "sine" and shape.params["shape"].from_wire("RAMP") == "ramp"
    family = Template("[SOURce]:VOLTage:LIMit", prefix=True)                    # blocked on every channel
    assert family.match("SOURce2:VOLTage:LIMit:HIGH 5") is not None and family.match("VOLT:LIM:LOW -1") is not None
    assert family.match("SOURce2:VOLTage 5") is None
    assert Template("Vs", prefix=True).match("V0 10") is None                  # Novatech's Vs is no family of V0
    assert Template("SOURce:VOLTage {value}").match("SOURce2:VOLTage 5") is None   # a setting keeps its channel


def test_a_value_the_instrument_clips_is_reported_not_claimed():
    """A generator takes at most 5 Vpp into 50 ohm whatever its card says, and its card has no error queue."""
    level = {"v": 0.0}

    def write(cmd):
        level["v"] = min(float(cmd.split()[1]), 5.0)
    gen = Device(name="gen")
    gen.twin = types.SimpleNamespace(write=write, query=lambda cmd: "%.3E" % level["v"])
    card = parse_card({"card": 1, "id": "acme/gen", "model": "Generator", "kind": "generator", "status": "community",
                       "parameters": {"amp": {"kind": "drive", "unit": "V", "set": "VOLT {value}", "get": "VOLT?",
                                              "limits": [0, 10], "safe": 0}}}, "test")
    reg = builtin_registry()
    reg.put(card)
    set_resource_manager(Bus({"GPIB0::9::INSTR": gen}))
    try:
        lab = build_lab({"lab": "x", "instruments": {"gen": {"card": "acme/gen", "address": "GPIB0::9::INSTR"}}},
                        registry=reg)
        gate = SafetyGate(lab, ScriptedApprover([True, True, True]))
        assert gate.set("gen", "amp", 2.3456).ok                               # read back rounded: 2.346
        r = gate.set("gen", "amp", 7)
        assert not r.ok and "5 V, not 7 V" in r.message and "refused or clipped" in r.message
        assert gen.sent.count("VOLT?") >= 3                                    # asked again before saying so
    finally:
        set_resource_manager(None)
