"""When Kelvin's card doesn't fit the lab's instrument, Kelvin makes it fit: an attoDRY on its eNSPIRE network
controller, which the lab added with the touch-screen attoDRY's DLL card, is moved to the card for its own interface
(change_instrument); a lab's version of a card takes the instrument over; a sine out read at 0 V is off, not on;
and a scan's menus show what each parameter is, a lock-in's X, Y, R and θ first in what it records."""
import json

import pytest

from labharness import builtin_registry
from labharness.adapters import set_resource_manager
from labharness.cards import parse_card
from labharness.discovery import change_entry
from labharness.drivers import MODULES
from labharness.gate import ScriptedApprover
from labharness.homelab import build_lab, lab_registry
from test_hardware import Bus, Device
from test_jsonrpc import FakeEnspire

OLD_CRYO = """lab: Magnet lab
instruments:
  # the cryostat, as the lab first set it up
  cryo:
    card: attocube/attodry        # attoDRY, through the DLL
    address: "lib://COM4?env=attodry"
    limits: { T_set: [1.65, 300], B_set: [-2, 2] }
  lockin:
    card: srs/sr860
    address: "GPIB0::8::INSTR"
"""


@pytest.fixture
def bench():
    bus = Bus({"GPIB0::8::INSTR": Device("srs/sr860", name="sr860"), "GPIB0::24::INSTR": Device("keithley/2400")})
    set_resource_manager(bus)
    yield bus
    set_resource_manager(None)
    MODULES.clear()


def runtime_on(tmp_path, text, answers=()):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    (tmp_path / "lab.yaml").write_text(text, encoding="utf-8")
    lab = build_lab(tmp_path / "lab.yaml", registry=lab_registry(tmp_path), partial=True)
    rt = LabRuntime(lab, ScriptedApprover(list(answers)), tmp_path / "data")
    rt.home, rt.cards_dir, rt.scan_network = tmp_path, tmp_path / "cards", False
    rt.lab_info = {"kind": "file", "path": str(tmp_path / "lab.yaml"), "error": None, "has_file": True}
    return rt


def events(tmp_path, kind):
    lines = (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e.get("type") == kind]


def test_kelvin_moves_an_attodry_to_the_card_for_its_network_controller(tmp_path, bench):
    rt = runtime_on(tmp_path, OLD_CRYO)
    assert "cryo" in rt.lab.unavailable   # the DLL card can't reach it
    fake = MODULES["jsonrpc"] = FakeEnspire()
    said = rt.change_instrument("cryo", address="jsonrpc://10.0.0.40:9090", card="attocube/attodry-enspire")
    assert said.startswith("changed cryo (attocube/attodry-enspire)"), said
    assert "health: ok" in said and fake.connected_to == ("10.0.0.40", 9090)
    assert "cryo" not in rt.lab.unavailable and rt.lab.read("cryo", "T") == pytest.approx(4.2)
    text = (tmp_path / "lab.yaml").read_text(encoding="utf-8")
    assert "card: attocube/attodry-enspire" in text and 'address: "jsonrpc://10.0.0.40:9090"' in text
    assert "limits: { T_set: [1.65, 300], B_set: [-2, 2] }" in text      # its limits stay
    assert "# the cryostat, as the lab first set it up" in text           # and the person's comments
    assert rt.lab.instruments["cryo"].config.limits["B_set"] == (-2, 2)
    assert "added_by: kelvin" in text and rt.lab.instruments["cryo"].config.added_by == "kelvin"   # another card:
    assert "Its outputs ask a person until they review it" in said         # a person reviews it before outputs move
    assert events(tmp_path, "instrument_changed")[-1]["card"] == "attocube/attodry-enspire"
    assert rt.lab.instruments["cryo"].card.status == "draft"   # each setting and output change still asks a person


def test_a_change_that_cannot_connect_or_finds_another_instrument_leaves_it_as_it_was(tmp_path, bench):
    rt = runtime_on(tmp_path, OLD_CRYO)
    before = (tmp_path / "lab.yaml").read_text(encoding="utf-8")

    class Nobody:   # nothing answers at that address (never a real connection from a test)
        @staticmethod
        def create_connection(address, timeout):
            raise ConnectionRefusedError("refused")
    MODULES["jsonrpc"] = Nobody
    said = rt.change_instrument("cryo", address="jsonrpc://10.0.0.41:9090", card="attocube/attodry-enspire")
    assert said.startswith("not changed: Kelvin could not connect cryo that way") and "as it was" in said
    assert "cryo" in rt.lab.unavailable and rt.lab.instruments["cryo"].card.id == "attocube/attodry"
    said = rt.change_instrument("lockin", address="GPIB0::24::INSTR")   # a source-meter answers there
    assert said.startswith("not changed: identity") and "does not match card srs/sr860" in said
    assert rt.lab.instruments["lockin"].config.address == "GPIB0::8::INSTR"
    assert rt.lab.read("lockin", "R") is not None                        # connected again as it was
    assert (tmp_path / "lab.yaml").read_text(encoding="utf-8") == before
    assert "names a program" in rt.change_instrument("cryo", address="lib://COM4?python=C:/x/python.exe")
    assert rt.change_instrument("cryo", card="no/such-card").startswith("not changed: there is no card")
    rt.lab.switch("lockin", False)
    assert "Kelvin's connection to lockin is off (Lab panel)" in rt.change_instrument("lockin", address="GPIB0::9::INSTR")


def test_this_labs_version_of_a_card_takes_the_instrument_over(tmp_path, bench):
    rt = runtime_on(tmp_path, OLD_CRYO)
    reg = builtin_registry()
    text = open(reg.get("srs/sr860").source, encoding="utf-8").read()
    mine = text.replace("status: community", "status: draft").replace(
        "    limits: [1.0e-9, 2.0]\n    safe: 1.0e-9", "    limits: [0, 2.0]\n    safe: 0")
    assert "saved draft card srs/sr860" in rt.save_card(mine, version=True)
    said = rt.change_instrument("lockin", card="srs/sr860")
    assert "card srs/sr860 as it is now" in said, said
    card = rt.lab.instruments["lockin"].card
    assert card.status == "draft" and card.params["amplitude"].limits == (0, 2.0)
    assert "already has that entry" in rt.change_instrument("lockin", card="srs/sr860")


def test_the_lab_config_entry_is_changed_in_place():
    block = ("lab: L\ninstruments:\n  cryo:\n    card: a/b    # old\n    address: \"lib://COM4\"\n"
             "    connection: { timeout_ms: 1 }\n    limits: { T: [0, 1] }\n  other: { card: c/d, address: \"X\" }\n")
    out = change_entry(block, "cryo", {"card": "a/c", "address": "jsonrpc://h:9090", "connection": None,
                                       "vars": {"channel": "1"}})
    assert out == ("lab: L\ninstruments:\n  cryo:\n    card: a/c\n    address: \"jsonrpc://h:9090\"\n"
                   "    vars: { channel: \"1\" }\n    limits: { T: [0, 1] }\n  other: { card: c/d, address: \"X\" }\n")
    one = change_entry(block, "other", {"address": "Y"})
    assert '  other: { card: "c/d", address: "Y" }' in one and "card: a/b    # old" in one
    marked = change_entry(block, "cryo", {"card": "a/c", "added_by": "kelvin"})
    assert marked.split("\n")[7].startswith("    added_by: kelvin   # each output change asks you")
    assert change_entry(block, "cryo", {"added_by": ""}) == block   # never taken off by Kelvin
    with pytest.raises(KeyError):
        change_entry(block, "nobody", {"address": "Y"})


def test_a_sine_out_read_at_zero_volts_is_off(tmp_path, bench):
    reg = builtin_registry()
    amp = reg.get("srs/sr860").params["amplitude"]
    assert amp.distance(0.0) == 0 and amp.distance(1e-9) == 0 and amp.distance(0.5) > 0
    gate = parse_card({"card": 1, "id": "x/dac", "model": "X", "kind": "dac", "parameters": {
        "V": {"kind": "drive", "unit": "V", "set": "V {value}", "get": "V?", "limits": [0, 10], "safe": 0}}})
    assert gate.params["V"].distance(-0.5) == 0.5             # a gate held below its limits is still on
    t_set = reg.get("attocube/attodry-enspire").params["T_set"]
    assert t_set.distance(1.65 + 1e-12) == 0                  # a read-back's last digits
    rt = runtime_on(tmp_path, OLD_CRYO)
    rt._status = {"lockin": {"amplitude": 0.0, "offset": 0.0, "R": 1e-6}}
    lockin = next(i for i in rt.instruments() if i["name"] == "lockin")
    assert lockin["state"] == "safe" and lockin["active"] == 0
    assert not any(d["away"] for d in lockin["drives"])
    dev = bench.devices["GPIB0::8::INSTR"]
    real = dev.twin

    class OffAtZero:   # a unit whose sine out reads 0 V, under the manual's least: its health check passes
        write = staticmethod(real.write)

        @staticmethod
        def query(cmd):
            return "0.000000e+00" if cmd.strip().upper().startswith("SLVL?") else real.query(cmd)
    dev.twin = OffAtZero()
    assert rt.lab.read("lockin", "amplitude") == 0.0
    health = rt.lab.health_check(["lockin"])[0]
    assert health.ok, health.problems
    restore, notes = rt._to_restore([{"key": "lockin.amplitude", "settable": True, "differs": True, "then": 0.0}],
                                    True)
    assert restore == {"lockin.amplitude": 1e-9} and not notes   # put back as off: its least, the safe value


def test_a_scans_menus_say_what_each_parameter_is(tmp_path, bench):
    rt = runtime_on(tmp_path, OLD_CRYO)
    lockin = next(i for i in rt._scan_choices()["instruments"] if i["name"] == "lockin")
    groups = {p["param"]: p["group"] for p in lockin["read"]}
    assert [p for p, g in groups.items() if g == "reading"] == ["X", "Y", "R", "theta"]
    assert groups["amplitude"] == "output" and groups["time_constant"] == "setting"
    assert {p["param"]: p["group"] for p in lockin["set"]}["reference"] == "setting"
    assert not any("type" in p for p in lockin["read"])   # a frame or a spectrum says so: a camera's scan records it
    cam = next(c for c in builtin_registry() if c.id == "andor/ccd")
    assert [p.name for p in cam.params.values() if p.type in ("image", "trace")][0] == "frame"
