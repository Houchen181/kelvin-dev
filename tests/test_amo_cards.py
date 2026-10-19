"""Cards for common AMO instruments (microwave sources, AOM drivers, a delay generator, an AWG), on a fake bus that
answers like each instrument, and the card rules they rely on."""
import pytest

from labharness import EventLog, SafetyGate, ScriptedApprover, builtin_registry
from labharness.adapters import set_resource_manager
from labharness.cards import CardError, parse_card
from labharness.virtual_lab import build_lab
from labharness.yamlio import load_yaml
from test_hardware import Bus, Device

AMO = ["srs/sg384", "rs/smb100a", "keysight/n5183b", "windfreak/synthhd", "srs/dg645", "rigol/dg1000z",
       "moglabs/xrf", "novatech/409b", "swabian/pulse-streamer", "spincore/pulseblaster", "swabian/time-tagger"]


def test_the_amo_cards_load_and_block_what_must_never_be_sent_blindly():
    reg = builtin_registry()
    for card_id in AMO:
        card = reg.get(card_id)
        assert card.kind in ("microwave-source", "delay-generator", "awg", "aom-driver", "pulse-generator",
                             "photon-counter"), card_id
    assert reg.get("windfreak/synthhd").status == "draft"                 # its guide predates the v2 hardware
    blocked = lambda card_id, cmd: any(b.match(cmd) is not None for b in reg.get(card_id).blocked)   # noqa: E731
    assert blocked("srs/sg384", "*SAV 1") and blocked("srs/dg645", "*CAL?") and blocked("srs/dg645", "CALL 3,1")
    assert blocked("rs/smb100a", ":SYST:FPR") and blocked("rs/smb100a", ":POW:LIM 30")
    assert blocked("moglabs/xrf", "LIMIT,1,36") and blocked("moglabs/xrf", "SET,ipaddr,10.0.0.9")
    assert blocked("novatech/409b", "S") and blocked("novatech/409b", "CLR") and not blocked("novatech/409b", "V0 10")
    assert not blocked("rs/smb100a", ":POW -20")                          # the power itself goes through the gate


def gate_for(lab, tmp_path, answers=()):
    return SafetyGate(lab, ScriptedApprover(answers), EventLog(tmp_path / "events.jsonl", lab.clock))


@pytest.fixture
def bench():
    bus = Bus({
        "TCPIP0::10.0.0.20::5025::SOCKET": Device("srs/sg384", name="sg"),
        "TCPIP0::10.0.0.30::7802::SOCKET": Device("moglabs/xrf", name="xrf", replies=lambda cmd: "OK", vars={"ch": "2"}),
        "ASRL7::INSTR": Device("novatech/409b", name="dds", replies=lambda cmd: "OK", vars={"ch": "3"}),
        "GPIB0::15::INSTR": Device("srs/dg645", name="dg"),
    })
    set_resource_manager(bus)
    yield bus
    set_resource_manager(None)


AMO_LAB = """
lab: AMO
instruments:
  mw:  { card: srs/sg384, address: "TCPIP0::10.0.0.20::5025::SOCKET", limits: { power: [-60, 0] } }
  aom: { card: moglabs/xrf, address: "TCPIP0::10.0.0.30::7802::SOCKET", vars: { ch: "2" }, limits: { power: [-50, 30] } }
  dds: { card: novatech/409b, address: "ASRL7::INSTR", vars: { ch: "3" } }
  dg:  { card: srs/dg645, address: "GPIB0::15::INSTR" }
"""


def test_an_amo_bench_through_the_gate(bench, tmp_path):
    lab = build_lab(AMO_LAB)
    assert bench.devices["TCPIP0::10.0.0.30::7802::SOCKET"].sent[0] == "OFF,2"   # switched off on connect: a known state
    assert bench.devices["ASRL7::INSTR"].sent[0] == "V3 0"
    gate = gate_for(lab, tmp_path, [True] * 8)
    assert gate.set("mw", "frequency", 2.87e9).ok
    assert gate.set("mw", "mod_type", "pulse").ok and gate.set("mw", "pulse_source", "external").ok
    assert "PFNC 5" in bench.devices["TCPIP0::10.0.0.20::5025::SOCKET"].sent
    assert gate.set("mw", "output", True).ok                             # asked first
    assert gate.set("mw", "power", 5).action == "refused"                # the lab's 0 dBm cap
    assert gate.set("mw", "power", -10).ok
    assert gate.set("aom", "output", True).ok
    assert "ON,2" in bench.devices["TCPIP0::10.0.0.30::7802::SOCKET"].sent
    assert gate.set("aom", "frequency", 80).ok and lab.read("aom", "frequency") == pytest.approx(80)
    assert gate.raw("aom", "ON,2,POW").action == "refused"              # no safe range can be checked for this variant
    assert "ON,2,POW" not in bench.devices["TCPIP0::10.0.0.30::7802::SOCKET"].sent
    assert gate.raw("aom", "LIMIT,2,36").action == "refused"
    assert gate.set("dds", "amplitude", 500).ok and lab.read("dds", "amplitude") == 500   # remembered: no read-back
    assert gate.set("dds", "amplitude", 2000).action == "refused"         # 1023 is full scale
    assert gate.set("dg", "delay_A", 1e-6).ok and gate.set("dg", "trigger_source", "internal").ok
    assert lab.read("dg", "delay_A") == pytest.approx(1e-6)              # DLAY?2 answers "0,<t>": the second field
    assert gate.raw("dg", "*CAL?").action == "refused"


def test_a_connect_command_may_only_bring_a_drive_to_safe():
    card = load_yaml(open(builtin_registry().get("moglabs/xrf").source, encoding="utf-8"))
    card = dict(card, id="test/xrf", on_connect=["ON,{ch}"])
    with pytest.raises(CardError, match="safe value"):
        parse_card(card, "test")
    assert parse_card(dict(card, on_connect=["OFF,{ch}", "FREQ,{ch},80"]), "test").on_connect == ["OFF,1", "FREQ,1,80"]
