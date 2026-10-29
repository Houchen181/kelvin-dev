"""Kelvin has no virtual lab (2026-10-29): it controls the person's own lab config, and until there is one, a lab with
no instruments that Set up fills. The simulated lab of common instruments lives in the tests only (tests/simlab), and
the app ships none of it: no virtual cards, no virtual lab config, no toy physics, no scripted demo model."""
import importlib.util
from pathlib import Path

import labharness
from labharness import homelab
from labharness.clock import RealClock

SRC = Path(labharness.__file__).parent


def test_the_app_ships_no_virtual_lab():
    assert not (SRC / "data" / "virtual_lab.yaml").exists() and not (SRC / "data" / "demo_project.yaml").exists()
    assert not (SRC / "sim" / "twins.py").exists() and not (SRC / "sim" / "nv.py").exists()
    cards = [p.read_text(encoding="utf-8") for p in (SRC / "data" / "cards").rglob("*.yaml")]
    assert cards and not [c for c in cards if "id: virtual/" in c or "twin:" in c]
    assert not hasattr(labharness, "build_virtual_lab") and not hasattr(labharness, "demo_envelope")
    assert importlib.util.find_spec("labharness.virtual_lab") is None
    assert importlib.util.find_spec("labharness.web.providers.demo") is None


def test_no_lab_config_yet_is_a_lab_with_no_instruments(tmp_path):
    lab, envelope, info = homelab.open_home_lab(tmp_path)
    assert lab.config.name == homelab.NO_LAB and not lab.instruments and envelope is None
    assert info["kind"] == "none" and not info["error"] and not info["has_file"]
    assert isinstance(lab.clock, RealClock)   # no simulated time: a wait is a real wait


def test_a_lab_config_kelvin_cant_read_leaves_no_instruments_and_says_why(tmp_path):
    (tmp_path / "lab.yaml").write_text("lab: Bench 2\ninstruments:\n  smu: {card: no/such-card, address: "
                                       "\"GPIB0::24::INSTR\"}\n", encoding="utf-8")
    lab, _, info = homelab.open_home_lab(tmp_path)
    assert not lab.instruments and info["kind"] == "none" and info["has_file"]
    assert info["error"].startswith("Your lab (lab.yaml) could not be opened") and "no/such-card" in info["error"]


def test_a_lab_none_of_whose_instruments_connect_stays_the_lab(tmp_path, monkeypatch):
    """Every instrument unavailable, each with why: the lab is still the person's, never another one."""
    from labharness import adapters

    def nothing_there(name, card, address, overrides=None):
        raise adapters.AdapterError("VI_ERROR_RSRC_NFOUND: nothing at %s" % address)
    monkeypatch.setattr(adapters, "open_backend", nothing_there)
    (tmp_path / "lab.yaml").write_text("lab: Bench 2\ninstruments:\n  smu: {card: keithley/2400, address: "
                                       "\"GPIB0::24::INSTR\"}\n", encoding="utf-8")
    lab, _, info = homelab.open_home_lab(tmp_path)
    assert lab.config.name == "Bench 2" and info["kind"] == "file" and not info["error"]
    assert list(lab.instruments) == ["smu"] and "nothing at GPIB0::24::INSTR" in lab.unavailable["smu"]
