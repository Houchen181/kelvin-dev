"""A lab's VI translated into a Kelvin experiment (Plan 3): what each LabVIEW call becomes (vimap), the checks a
translation must pass (vicheck), and the translator's model call and repair rounds (vitranslate)."""
from pathlib import Path

import pytest

from labharness import videsc, vimap
from labharness.homelab import builtin_registry

ROOT = Path(__file__).resolve().parents[1]
KEYSIGHT = ROOT / "tests" / "fixtures" / "labview" / "keysight_u2722"


@pytest.fixture
def registry():
    return builtin_registry()


# ------------------------------------------------------------------ what each LabVIEW call becomes (Task 2)

def test_a_named_call_maps_to_its_card_parameters():
    t = vimap.load()
    m = t.find("Keithley 24XX.lvlib:Enable Output.vi", {"kind": "driver"})
    assert (m.becomes, m.card, m.sets, m.source) == ("card", "keithley/2400", ["output"], "kelvin")
    assert t.find("MG17Motor.MoveAbsoluteRot", {}).card == "thorlabs/kdc101"


def test_the_labs_own_map_comes_first(tmp_path):
    own = tmp_path / "labview-map.yaml"
    own.write_text('- {call: "Time Delay", becomes: python, python: "kelvin_wait(s)"}\n', encoding="utf-8")
    assert vimap.load(own).find("Time Delay", {"kind": "unknown"}).python == "kelvin_wait(s)"
    assert vimap.load(own).find("Time Delay", {"kind": "unknown"}).source == "lab"


def test_a_bad_entry_in_the_labs_map_is_skipped_with_a_note(tmp_path):
    own = tmp_path / "labview-map.yaml"
    own.write_text('- {call: "X.vi", becomes: magic}\n- {call: "Time Delay", becomes: nothing, why: "w"}\n',
                   encoding="utf-8")
    t = vimap.load(own)
    assert t.find("X.vi", {}) is None and t.notes and t.find("Time Delay", {}).becomes == "nothing"


def test_a_driver_with_no_entry_is_matched_by_the_commands_it_sends(registry):
    sends = [{"text": "SOUR:VOLT {v}", "values": {}}, {"text": "OUTP {on}", "values": {}}]
    m = vimap.by_commands(sends, [registry.get("keithley/2400")])
    assert m.card == "keithley/2400" and set(m.sets) >= {"V", "output"} and m.source == "commands"
    assert vimap.by_commands([{"text": "FROB 3", "values": {}}], [registry.get("keithley/2400")]) is None


def test_every_call_and_object_node_of_a_vi_is_mapped_or_said_unmapped(registry):
    d = videsc.describe(KEYSIGHT / "SMU_IV single.vi")
    maps = vimap.map_description(d, vimap.load(), list(registry))
    assert set(maps) >= set(d["calls"])
    assert maps["Write Delimited Spreadsheet.vi"].becomes == "python"
    assert all(m.becomes in ("card", "python", "nothing", "unmapped") for m in maps.values())
    assert "Write Delimited Spreadsheet.vi" in vimap.text(maps)


def test_accepting_a_proposed_mapping_writes_the_labs_map(tmp_path):
    f = tmp_path / "labview-map.yaml"
    said = vimap.accept(f, [{"call": "Acme.lvlib:Arm.vi", "becomes": "card", "card": "acme/x", "does": ["arm"]}],
                        "ana")
    assert said and vimap.load(f).find("Acme.lvlib:Arm.vi", {}).card == "acme/x"
    assert vimap.accept(f, [{"call": "Bad.vi", "becomes": "card"}], "ana") == []    # no card: not taken


def test_the_map_ships_with_kelvin():
    spec = (ROOT / "packaging" / "kelvin.spec").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "map.yaml" in spec and '"data/labview/*.yaml"' in pyproject
