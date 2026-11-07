"""A lab config Kelvin can trust (review of 0.3.45, 6.1 and 6.3).

6.1: a misspelled key in lab.yaml was ignored as if it weren't there: `limit:` left a Keithley 2400 at its maker's
±210 V, `interlock:` meant no interlocks at all, and `adress:` (or no address) made a simulated twin that read 0.0, sent
nothing to the real instrument, was recorded in runs and never asked before a change. Now every key Kelvin doesn't read
is refused, saying what it may have meant; at start, one in an instrument's entry sets that instrument aside (never
connected) and the rest of the lab works, and one anywhere else keeps the lab from opening, saying why.

6.3: one entry whose card Kelvin no longer has (deleted in Lab, Cards; the instrument collection switched off; a card
file it can't read) emptied the whole lab. Now that entry alone is left out, saying why, with whatever leaned on it.
"""
import re
from pathlib import Path

import pytest

from labharness.homelab import builtin_registry, open_home_lab
from labharness.labconfig import (ENTRY_KEYS, TOP_KEYS, ConfigError, load_lab_config)

from test_hardware import Bus, Device

REG = builtin_registry()
DOCS = Path(__file__).resolve().parents[1] / "docs"


def errors_of(text):
    with pytest.raises(ConfigError) as e:
        load_lab_config(text, REG)
    return e.value.errors


def start(tmp_path, text, devices=None):
    """Kelvin's start on this lab.yaml, its instruments on a fake GPIB bus: (lab, info, bus)."""
    from labharness.adapters import set_resource_manager
    bus = Bus(devices if devices is not None else {"GPIB0::24::INSTR": Device("keithley/2400", name="k2400"),
                                                   "GPIB0::8::INSTR": Device("srs/sr830", name="sr830")})
    set_resource_manager(bus)
    try:
        (tmp_path / "lab.yaml").write_text(text, encoding="utf-8")
        lab, _, info = open_home_lab(tmp_path, connect=True)
        return lab, info, bus
    finally:
        set_resource_manager(None)


# ---------------------------------------------------------------------- 6.1: keys Kelvin doesn't read

TYPOS = """lab: My lab
instruments:
  smu:
    card: keithley/2400
    address: GPIB0::24::INSTR
    limit: { V: [-1, 1] }          # singular: the person means ±1 V
  tc:
    card: lakeshore/336
    address: GPIB0::12::INSTR
interlock:                          # singular
  - drive: smu.V
    require: "tc.T < 300"
"""


def test_a_misspelled_key_is_refused_saying_what_it_may_have_meant():
    errors = errors_of(TYPOS)
    entry = next(e for e in errors if e.startswith("instruments.smu.limit:"))
    assert "did you mean limits?" in entry and "a note to yourself goes after a #" in entry
    top = next(e for e in errors if e.startswith("interlock:"))
    assert "did you mean interlocks?" in top and "lab, instruments, interlocks" in top
    assert len(errors) == 2


def test_misspelled_keys_in_interlocks_watches_and_guards_are_refused():
    errors = errors_of("""lab: My lab
instruments:
  smu:
    card: keithley/2400
    address: GPIB0::24::INSTR
    guards: { V: [{read: I, max_abs: 1.0e-6, on_trp: hold}] }
  tc: { card: lakeshore/336, address: "GPIB0::12::INSTR" }
interlocks:
  - drive: smu.V
    requires: "tc.T < 300"
watch:
  - read: tc.T
    above: 10
    thne: { smu.V: 0 }
""")
    said = {e.split(":")[0]: e for e in errors}
    assert "did you mean on_trip?" in said["instruments.smu.guards.V.on_trp"]
    assert "did you mean require?" in said["interlocks[0].requires"]
    assert "did you mean then?" in said["watch[0].thne"]


def test_an_address_left_out_beside_real_instruments_is_refused():
    errors = errors_of("""lab: My lab
instruments:
  smu: { card: keithley/2400, address: "GPIB0::24::INSTR" }
  lockin:
    card: srs/sr830
    adress: GPIB0::8::INSTR     # a typo
""")
    assert any(e.startswith("instruments.lockin.adress:") and "did you mean address?" in e for e in errors)
    assert any(e.startswith("instruments.lockin.address: missing") for e in errors)


def test_an_empty_address_is_refused_even_in_a_simulated_lab():
    errors = errors_of("""lab: Sim
instruments:
  smu: { card: keithley/2400, address: }
""")
    assert errors == ["instruments.smu.address: empty: say where Kelvin reaches the instrument, as in "
                      "GPIB0::24::INSTR or serial://COM3"]


def test_a_lab_that_is_all_simulated_may_leave_addresses_out():
    cfg = load_lab_config("""lab: Sim
instruments:
  smu: { card: keithley/2400 }
  tc: { card: lakeshore/336, address: "sim://tc" }
""", REG)
    assert cfg.instruments["smu"].address == "sim://smu" and cfg.instruments["tc"].address == "sim://tc"


def test_every_key_kelvin_reads_is_accepted():
    cfg = load_lab_config("""lab: Every key
auto_fraction: 0.2
observe_only: false
names: { Vg: smu.V }
instruments:
  smu:
    card: keithley/2400
    address: GPIB0::24::INSTR
    trust: ask
    limits: { V: [-1, 1] }
    rates: { V: 0.5 }
    guards: { V: [{read: I, max_abs: 1.0e-6, on_trip: hold}] }
    auto: { V: [-0.5, 0.5] }
    connection: { timeout_ms: 5000 }
    added_by: kelvin
  tc:
    card: lakeshore/336
    address: GPIB0::12::INSTR
  itc:
    card: oxford/mercury-itc
    address: "TCPIP0::10.0.0.9::7020::SOCKET"
    vars: { sensor: DB6.T1 }
interlocks:
  - drive: smu.V
    require: "tc.T_A < 300"
safe_actions: { smu.V: safe }
watch:
  - { read: tc.T_A, above: 10, for: 5, every: 2, then: { smu.V: 0 }, stop_run: true, note: warm }
""", REG)
    assert cfg.instruments["smu"].limits == {"V": (-1.0, 1.0)} and len(cfg.interlocks) == 1 and cfg.watches
    assert set(TOP_KEYS) == {"lab", "instruments", "interlocks", "safe_actions", "watch", "auto_fraction",
                             "observe_only", "names"}
    assert set(ENTRY_KEYS) == {"card", "address", "trust", "limits", "rates", "guards", "auto", "connection", "vars",
                               "added_by"}


def test_the_lab_configs_kelvin_writes_and_the_docs_show_still_load():
    from labharness.discovery import draft_from
    text = draft_from([{"name": "smu", "card": "keithley/2400", "address": "GPIB0::24::INSTR", "added_by": "kelvin",
                        "connection": {"timeout_ms": 5000}},
                       {"name": "tc", "card": "lakeshore/336", "address": "GPIB0::12::INSTR"},
                       {"name": "laser", "card": "coherent/obis", "address": "serial://COM7"}], REG)
    load_lab_config(text, REG)
    blocks = re.findall(r"```ya?ml\n(.*?)```", (DOCS / "instruments.md").read_text(encoding="utf-8"), re.S)
    configs = [b for b in blocks if re.search(r"^instruments:", b, re.M) and re.search(r"^lab:", b, re.M)]
    assert len(configs) >= 2
    for block in configs:
        load_lab_config(block, REG)


def test_at_start_a_misspelled_address_sets_that_instrument_aside_and_sends_it_nothing(tmp_path):
    lab, info, bus = start(tmp_path, """lab: My lab
instruments:
  smu:
    card: keithley/2400
    address: GPIB0::24::INSTR
  lockin:
    card: srs/sr830
    adress: GPIB0::8::INSTR     # a typo
""")
    assert info["error"] is None and lab.instruments["smu"].enabled
    lockin = lab.instruments["lockin"]
    assert not lockin.enabled and not lockin.simulated                        # never a twin reading 0.0
    assert "adress: not something Kelvin reads" in info["set_aside"]["lockin"]
    assert "address: missing" in info["set_aside"]["lockin"]
    assert "set aside" in lockin.unavailable and "did you mean address?" in lockin.unavailable
    assert bus.devices["GPIB0::8::INSTR"].sent == []                          # nothing reached the real SR830


def test_at_start_a_misspelled_limits_key_sets_that_instrument_aside(tmp_path):
    lab, info, _ = start(tmp_path, """lab: My lab
instruments:
  smu:
    card: keithley/2400
    address: GPIB0::24::INSTR
    limit: { V: [-1, 1] }
  lockin: { card: srs/sr830, address: "GPIB0::8::INSTR" }
""")
    assert not lab.instruments["smu"].enabled and "did you mean limits?" in info["set_aside"]["smu"]
    assert lab.instruments["lockin"].enabled                                  # the rest of the lab works


def test_at_start_an_unknown_key_of_the_lab_config_itself_keeps_it_from_opening(tmp_path):
    lab, info, _ = start(tmp_path, """lab: My lab
lab_nmae: Cryostat 2
instruments:
  smu: { card: keithley/2400, address: "GPIB0::24::INSTR" }
""")
    assert lab.instruments == {} and "lab_nmae: not something Kelvin reads in a lab config" in info["error"]


def test_at_start_a_misspelled_interlocks_key_keeps_the_lab_from_opening_and_says_why(tmp_path):
    lab, info, _ = start(tmp_path, TYPOS.replace("    limit: { V: [-1, 1] }          # singular: the person means ±1 V\n",
                                                 ""))
    assert lab.instruments == {}                                              # never a lab without its interlocks
    assert "could not be opened" in info["error"] and "did you mean interlocks?" in info["error"]


# ---------------------------------------------------------------------- 6.3: a card Kelvin no longer has

LAB3 = """lab: My lab
instruments:
  smu:
    card: keithley/2400
    address: GPIB0::24::INSTR
  lockin:
    card: srs/sr830
    address: GPIB0::8::INSTR
  laser:
    card: mylab/laser-draft     # a draft card the person deleted in Lab, Cards
    address: ASRL5::INSTR
"""


def test_an_entry_whose_card_kelvin_no_longer_has_is_left_out_and_the_rest_works(tmp_path):
    lab, info, _ = start(tmp_path, LAB3)
    assert info["error"] is None and sorted(lab.instruments) == ["lockin", "smu"]
    assert lab.instruments["smu"].enabled and lab.instruments["lockin"].enabled
    assert info["left_out"]["laser"].startswith("left out of the lab: Kelvin has no card mylab/laser-draft (deleted")
    assert info["set_aside"] == {}


def test_what_leaned_on_a_left_out_instrument_goes_with_it_and_its_drives_are_set_aside(tmp_path):
    lab, info, bus = start(tmp_path, LAB3 + """interlocks:
  - drive: smu.V                       # held by the laser's reading: never moves unguarded
    require: "laser.power < 0.01"
  - drive: laser.power                 # on the laser itself: nothing drives it in this lab
    require: "smu.I < 1"
watch:
  - read: laser.power
    above: 0.5
    then: { smu.V: 0 }
  - read: lockin.X
    above: 1
    then: { laser.power: 0 }           # the laser's step goes, the watch stays
safe_actions: { laser.power: hold }
""")
    assert info["error"] is None and sorted(lab.instruments) == ["lockin", "smu"]
    smu = info["set_aside"]["smu"]
    assert "its interlock needs laser.power < 0.01, and laser is left out of the lab" in smu
    assert "a watch on laser.power sends smu.V to safe, and laser is left out of the lab" in smu
    assert not lab.instruments["smu"].enabled and lab.instruments["lockin"].enabled
    assert lab.config.interlocks == [] and [w["read"] for w in lab.config.watches] == ["lockin.X"]
    assert lab.config.watches[0]["then"] == [] and lab.config.safe_actions == {}
    assert bus.devices["GPIB0::24::INSTR"].sent == []                         # nothing reached the set-aside smu


def test_an_unknown_card_beside_a_problem_elsewhere_still_keeps_the_lab_from_opening(tmp_path):
    lab, info, _ = start(tmp_path, LAB3 + "interlock:\n  - { drive: smu.V, require: \"lockin.X < 1\" }\n")
    assert lab.instruments == {} and "did you mean interlocks?" in info["error"]


def test_the_lab_panel_lists_a_left_out_instrument_with_those_not_connected(tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    from labharness.gate import ScriptedApprover
    lab, info, _ = start(tmp_path, LAB3)
    rt = LabRuntime(lab, ScriptedApprover([]), tmp_path / "data")
    rt.lab_info = info
    rows = {r["name"]: r for r in rt._unavailable_view()}
    assert set(rows) == {"laser"} and "Kelvin has no card mylab/laser-draft" in rows["laser"]["why"]


HEATER = """card: 1
id: mylab/heater
model: Bench heater
vendor: Us
kind: source
status: draft
parameters:
  P:
    kind: drive
    unit: W
    set: "POW {value}"
    get: "POW?"
    limits: [0, 2]
    safe: 0
"""


def portal(home, instruments, extra=()):
    """The portal on a lab of these instruments (simulated), with the lab's own cards in home/cards (and `extra`
    cards, as the instrument collection's would be while it is on)."""
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    from starlette.testclient import TestClient
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.homelab import build_lab, lab_registry
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    registry = lab_registry(home)
    for card in extra:
        registry.put(card)
    lab = build_lab({"lab": "Bench", "instruments": instruments}, registry=registry)
    rt = LabRuntime(lab, WebApprover(timeout=5), home / "data")
    settings = Settings(home / "settings.json")
    app = create_app(rt, settings, ChatStore(home / "chats"), hosts=["testserver"], providers={})
    return TestClient(app), {"X-Kelvin-Token": settings.access_key}, settings


def test_deleting_a_card_the_lab_uses_that_kelvin_has_no_other_of_is_refused_naming_them(tmp_path):
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "mylab_heater.yaml").write_text(HEATER, encoding="utf-8")
    client, h, _ = portal(tmp_path, {"heater": {"card": "mylab/heater"}, "smu": {"card": "keithley/2400"}})
    with client:
        r = client.post("/api/cards/delete", json={"id": "mylab/heater"}, headers=h)
        assert r.status_code == 409
        assert r.json()["error"].startswith("heater uses this card, and Kelvin has no other card mylab/heater: "
                                            "deleting it would leave it out of your lab from its next start")
        assert (tmp_path / "cards" / "mylab_heater.yaml").exists()            # nothing deleted


def test_deleting_the_labs_version_of_one_of_kelvins_cards_goes_ahead(tmp_path):
    (tmp_path / "cards").mkdir()
    import re as _re
    from labharness.homelab import builtin_registry as _reg
    own = Path(_reg().get("keithley/2400").source).read_text(encoding="utf-8")
    (tmp_path / "cards" / "keithley_2400.yaml").write_text(_re.sub(r"(?m)^status:.*$", "status: draft", own, count=1),
                                                          encoding="utf-8")
    client, h, _ = portal(tmp_path, {"smu": {"card": "keithley/2400"}})
    with client:
        r = client.post("/api/cards/delete", json={"id": "keithley/2400"}, headers=h)
        assert r.status_code == 200 and r.json()["reconnect"] == ["smu"]      # Kelvin's own card applies again


def test_switching_the_collection_off_while_the_lab_needs_its_cards_is_refused_naming_them(tmp_path):
    from labharness.cards import parse_card
    from labharness.yamlio import load_yaml
    # a card the collection brought (it is in the lab's cards while the collection is on), Kelvin's own lacks
    widget = parse_card(load_yaml(HEATER.replace("mylab/heater", "acme/widget").replace("Bench heater", "Widget")))
    client, h, settings = portal(tmp_path, {"src": {"card": "acme/widget"}, "smu": {"card": "keithley/2400"}},
                                 extra=[widget])
    with client:
        r = client.post("/api/settings", json={"collection": "off"}, headers=h)
        assert r.status_code == 409
        assert r.json()["error"].startswith("src uses the instrument collection's card acme/widget, which Kelvin "
                                            "doesn't have itself: with the collection switched off, it would be left "
                                            "out of your lab from its next start")
        assert settings.get("collection") != "off"                             # nothing changed
        assert client.post("/api/settings", json={"collection": "on"}, headers=h).status_code == 200


def test_switching_the_collection_off_goes_ahead_when_the_lab_needs_none_of_its_cards(tmp_path):
    client, h, settings = portal(tmp_path, {"smu": {"card": "keithley/2400"}})
    with client:
        assert client.post("/api/settings", json={"collection": "off"}, headers=h).status_code == 200
        assert settings.get("collection") == "off"
