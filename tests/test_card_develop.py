"""Developing a card: drafting it from the maker's example scripts, and trying it on the instrument (reads only)
until its readings agree with the front panel."""
import json

import pytest

from labharness import builtin_registry, carddraft
from labharness.adapters import AdapterError, set_resource_manager
from labharness.cards import parse_card
from labharness.cardtry import summary, try_card
from labharness.virtual_lab import build_lab
from labharness.yamlio import load_yaml
from test_hardware import Bus, Device


def test_example_scripts_are_gathered_for_the_model(tmp_path):
    sdk = tmp_path / "attoDRY SDK"
    (sdk / "examples").mkdir(parents=True)
    (sdk / "__pycache__").mkdir()
    (sdk / "README.txt").write_text("Call begin, then Connect with the COM port.", encoding="utf-8")
    (sdk / "attoDRYLib.h").write_text("int32_t __cdecl AttoDRY_Interface_getSampleTemperature(float *T);", encoding="utf-8")
    (sdk / "examples" / "example.py").write_text("AttoDRY.begin(1)\nT = AttoDRY.getSampleTemperature()", encoding="utf-8")
    (sdk / "examples" / "demo.ipynb").write_text(json.dumps({"cells": [
        {"cell_type": "code", "source": ["AttoDRY.setUserTemperature(4.2)\n"], "outputs": [{"text": "huge output"}]},
    ]}), encoding="utf-8")
    (sdk / "attoDRYLib.dll").write_bytes(b"MZ\x00binary")
    (sdk / "__pycache__" / "x.py").write_text("ignored", encoding="utf-8")
    text = carddraft.read_sources(str(sdk))
    assert text.index("README.txt") < text.index("attoDRYLib.h") < text.index("example.py")   # README, header first
    assert "setUserTemperature(4.2)" in text and "huge output" not in text and "binary" not in text
    assert "ignored" not in text
    assert "=== examples" in text
    with pytest.raises(ValueError, match="no file or folder"):
        carddraft.read_sources(str(tmp_path / "nothing"))
    assert "example scripts" in carddraft.draft_prompt(text, "attoDRY") and "library:" in carddraft.guide()


@pytest.fixture
def bus():
    b = Bus({"GPIB0::8::INSTR": Device("srs/sr830", name="li")})
    set_resource_manager(b)
    yield b
    set_resource_manager(None)


def test_trying_a_card_asks_and_reads_but_never_sets(bus):
    card = builtin_registry().get("srs/sr830")
    result = try_card(card, "GPIB0::8::INSTR")
    assert result["matches"] is True and not result["reused"]
    values = {r["name"]: r for r in result["readings"]}
    assert "value" in values["X"] and "error" not in values["X"]
    sent = bus.devices["GPIB0::8::INSTR"].sent
    assert sent and all("?" in c.split()[0] for c in sent)                 # only queries went out
    assert "readings answered" in summary(result)

    data = load_yaml(open(card.source, encoding="utf-8"))
    data = dict(data, id="test/sr830", parameters=dict(data["parameters"], beep={
        "label": "Beep", "kind": "measure", "get": "*RST"}, meas={"label": "M", "kind": "measure", "get": "MEAS:VOLT?"}))
    draft = parse_card(data, "draft")
    n = len(sent)
    result = try_card(draft, "GPIB0::8::INSTR")
    rows = {r["name"]: r for r in result["readings"]}
    assert "not a query" in rows["beep"]["skipped"] and "start a measurement" in rows["meas"]["skipped"]
    assert "*RST" not in sent[n:] and "MEAS:VOLT?" not in sent[n:]


def test_a_try_uses_the_labs_connection_for_a_text_instrument(bus):
    lab = build_lab("""
lab: Bench
instruments:
  li: { card: srs/sr830, address: "GPIB0::8::INSTR" }
""")
    result = try_card(builtin_registry().get("srs/sr830"), "GPIB0::8::INSTR", lab)
    assert result["reused"] and result["matches"]
    assert lab.read("li", "X") is not None                                  # the lab's connection is still open
    with pytest.raises(AdapterError, match="simulated"):
        try_card(builtin_registry().get("srs/sr830"), "sim://x")


def test_kelvin_tries_cards_only_where_a_person_connected_or_the_scan_found(bus, tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    lab = build_lab("""
lab: Bench
instruments:
  li: { card: srs/sr830, address: "GPIB0::8::INSTR" }
""")
    rt = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", None)
    text = open(builtin_registry().get("srs/sr830").source, encoding="utf-8").read().replace("id: srs/sr830", "id: test/li")
    out = rt.try_card(text, "li")
    assert "matches the card" in out and "readings answered" in out
    refused = rt.try_card(text, "lib://COM1?python=C:/evil.exe")
    assert refused.startswith("Kelvin tries a card only on an instrument in the lab")
    rt.last_found = ["GPIB0::9::INSTR"]
    assert "GPIB0::9::INSTR" in rt.try_card(text, "nowhere")
    assert "not read" in rt.read_examples(str(tmp_path / "missing"))
