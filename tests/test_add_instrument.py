"""Kelvin sets up an instrument someone names, by itself: finds it, writes its card from the manual if it has none,
adds it to the lab marked as added by Kelvin (every output change on it asks a person until they review its limits),
and asks a person only where it isn't sure: an address the scan didn't find, leaving a virtual lab they chose,
switching on an instrument they switched off."""
import json

import pytest

from labharness import build_virtual_lab, builtin_registry
from labharness.adapters import set_resource_manager
from labharness.carddraft import save as save_card
from labharness.discovery import add_to_config, instrument_block
from labharness.gate import ScriptedApprover
from labharness.labconfig import load_lab_config, same_setup
from labharness.virtual_lab import build_lab, lab_registry
from test_hardware import Bus, Device

SMU_ONLY = """lab: Bench
instruments:
  smu:
    card: keithley/2400
    address: "GPIB0::24::INSTR"
    limits: { V: [-5, 5] }
"""
LOCKIN_ONLY = """lab: Bench
instruments:
  lockin:
    card: srs/sr830
    address: "GPIB0::8::INSTR"
"""
WIDGET = """card: 1
id: acme/widget-9000
model: ACME Widget 9000
vendor: ACME
kind: source-meter
status: draft
identify: { idn: "^ACME,WIDGET 9000" }
parameters:
  V: { label: Voltage, kind: drive, unit: V, set: "VOLT {value}", get: "VOLT?", limits: [-10, 10], safe: 0,
       ramp: gate, max_rate: 1 }
  I: { label: Current, kind: measure, unit: A, get: "CURR?" }
"""


@pytest.fixture
def bench():
    widget = Device(idn="ACME,WIDGET 9000,1,1.0")
    widget.twin = type("Answers", (), {"query": staticmethod(lambda cmd: "0"), "write": staticmethod(lambda cmd: None)})()
    bus = Bus({"GPIB0::24::INSTR": Device("keithley/2400", name="k2400"),
               "GPIB0::8::INSTR": Device("srs/sr830", name="sr830"),
               "GPIB0::5::INSTR": widget})
    set_resource_manager(bus)
    yield bus
    set_resource_manager(None)


def runtime_on(tmp_path, lab, kind):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    rt = LabRuntime(lab, ScriptedApprover([]), tmp_path / "data")
    rt.home, rt.cards_dir, rt.scan_network = tmp_path, tmp_path / "cards", False
    rt.lab_info = {"kind": kind, "path": str(tmp_path / "lab.yaml"), "error": None,
                   "has_file": (tmp_path / "lab.yaml").exists()}
    return rt


def file_runtime(tmp_path, text):
    """Kelvin on the person's own lab (lab.yaml)."""
    (tmp_path / "lab.yaml").write_text(text, encoding="utf-8")
    return runtime_on(tmp_path, build_lab(tmp_path / "lab.yaml", registry=lab_registry(tmp_path)), "file")


def events(tmp_path, kind):
    lines = (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e.get("type") == kind]


def test_kelvin_adds_a_named_instrument_and_asks_before_its_outputs(tmp_path, bench):
    rt = file_runtime(tmp_path, SMU_ONLY)
    changed = []
    rt.lab_changed = changed.append
    found = rt.scan()
    assert "card srs/sr830" in found and "add_instrument" in found
    lab, smu = rt.lab, rt.lab.instruments["smu"].backend
    out = rt.add_instrument("GPIB0::8::INSTR")                              # the card the scan matched
    assert out.startswith("added lockin (") and "health: ok" in out and "asks them" in out, out
    assert rt.lab is lab and rt.lab.instruments["smu"].backend is smu      # the others stay connected as they are
    text = (tmp_path / "lab.yaml").read_text(encoding="utf-8")
    assert text.startswith(SMU_ONLY) and text.count("added_by: kelvin") == 1 and "  lockin:\n    card: srs/sr830" in text
    assert rt.lab.config.unreviewed() == ["lockin"] and changed == ["added"]
    assert any(n["title"] == "Kelvin added lockin to the lab" for n in rt.notifier.notices)
    assert [(e["instrument"], e["by"]) for e in events(tmp_path, "instrument_added")] == [("lockin", "agent")]
    assert "not reviewed by a person yet" in rt.lab_status() and "not sure yet" in rt.describe("lockin")

    rt.set("lockin", "time_constant", "300 ms")                             # configuring: no question
    rt.gate.approver = ScriptedApprover([False])
    assert rt.set("lockin", "amplitude", 0.1).startswith("declined")        # an output: the person decides
    assert "Kelvin added it to the lab itself" in str(rt.gate.approver.requests[0])

    assert "in the lab already, as lockin" in rt.add_instrument("GPIB0::8::INSTR")
    assert "the lab has a smu already" in rt.add_instrument("GPIB0::5::INSTR", card="keithley/2400", name="smu")
    none = rt.add_instrument("GPIB0::5::INSTR")
    assert "no card for GPIB0::5::INSTR yet" in none and "draft_card" in none
    assert "there is no card 'acme/nope'" in rt.add_instrument("GPIB0::5::INSTR", card="acme/nope")
    wrong = rt.add_instrument("GPIB0::5::INSTR", card="keithley/2400", name="smu2")   # the widget is no 2400
    assert wrong.startswith("not added: identity") and "smu2" not in rt.lab.instruments
    assert "smu2" not in rt.lab.config.instruments and (tmp_path / "lab.yaml").read_text(encoding="utf-8") == text

    bench.devices["GPIB0::9::INSTR"] = Device("srs/sr830", name="sr830b")   # plugged in after the scan
    rt.gate.approver = ScriptedApprover([False, True])
    assert rt.add_instrument("GPIB0::9::INSTR", card="srs/sr830").startswith("declined")
    assert "did not find an instrument at GPIB0::9::INSTR" in str(rt.gate.approver.requests[0])
    assert rt.add_instrument("GPIB0::9::INSTR", card="srs/sr830").startswith("added lockin2")
    assert sorted(rt.lab.config.unreviewed()) == ["lockin", "lockin2"]
    assert sorted(load_lab_config((tmp_path / "lab.yaml").read_text(encoding="utf-8"),
                                  lab_registry(tmp_path)).unreviewed()) == ["lockin", "lockin2"]


def test_from_the_virtual_lab_kelvin_moves_to_the_persons_instruments(tmp_path, bench):
    rt = runtime_on(tmp_path, build_virtual_lab(seed=1, registry=lab_registry(tmp_path)), "virtual")   # no lab yet
    rt.scan()
    assert rt.add_instrument("GPIB0::24::INSTR").startswith("added smu")
    assert rt.lab_info["kind"] == "file" and list(rt.lab.instruments) == ["smu"]
    assert "added_by: kelvin" in (tmp_path / "lab.yaml").read_text(encoding="utf-8")

    chose = tmp_path / "chose-virtual"
    chose.mkdir()
    (chose / "lab.yaml").write_text(LOCKIN_ONLY, encoding="utf-8")
    rt = runtime_on(chose, build_virtual_lab(seed=1, registry=lab_registry(chose)), "virtual")
    rt.scan()
    rt.gate.approver = ScriptedApprover([False, True])
    assert rt.add_instrument("GPIB0::24::INSTR").startswith("declined")
    asked = str(rt.gate.approver.requests[0])
    assert "on the virtual lab, as you chose" in asked and "(lockin, smu)" in asked
    assert rt.lab_info["kind"] == "virtual" and (chose / "lab.yaml").read_text(encoding="utf-8") == LOCKIN_ONLY
    assert rt.add_instrument("GPIB0::24::INSTR").startswith("added smu")
    assert rt.lab_info["kind"] == "file" and sorted(rt.lab.instruments) == ["lockin", "smu"]
    assert rt.lab.config.unreviewed() == ["smu"]


def test_a_card_for_a_named_instrument_then_adding_it_keeps_its_manual(tmp_path, bench):
    pytest.importorskip("pypdf")
    from test_autosetup import MANUAL, tiny_pdf
    rt = file_runtime(tmp_path, SMU_ONLY)
    assert "write it yourself" in rt.draft_card("GPIB0::5::INSTR")         # outside the Kelvin app
    manual = tiny_pdf(MANUAL)
    asked = []

    def writer(found):
        asked.append(dict(found))
        save_card(tmp_path / "cards", WIDGET, [c.id for c in builtin_registry()], by="kelvin")
        return {"card_id": "acme/widget-9000", "model": "ACME Widget 9000", "kind": "source-meter", "changed": [],
                "parameters": "V (output, V), I (reading, A)",
                "manual": {"url": "https://www.acme.example/m.pdf", "data": manual, "kind": "pdf"}}
    rt.card_writer = writer
    bench.devices["ASRL5::INSTR"] = Device(silent=True)                    # a serial instrument that says nothing
    rt.scan()
    out = rt.draft_card("GPIB0::5::INSTR")
    assert out.startswith("drafted card acme/widget-9000") and "try_card acme/widget-9000 on GPIB0::5::INSTR" in out
    assert asked[0]["idn"] == "ACME,WIDGET 9000,1,1.0" and "V (output, V)" in out
    silent = rt.draft_card("ASRL5::INSTR")
    assert "did not answer who it is" in silent and "maker and the model" in silent and len(asked) == 1
    rt.draft_card("ASRL5::INSTR", "ACME", "Widget 9000")                    # as the person named it
    assert asked[-1] == {"address": "ASRL5::INSTR", "idn": "", "maker": "ACME", "model": "Widget 9000", "manual": "",
                         "specs": "", "serial": {}}
    assert "already matches card srs/sr830" in rt.draft_card("GPIB0::8::INSTR")

    tried = rt.try_card("acme/widget-9000", "GPIB0::5::INSTR")              # the saved draft, by its id
    assert "matches the card" in tried and "I = 0" in tried, tried
    out = rt.add_instrument("GPIB0::5::INSTR", card="acme/widget-9000")
    assert out.startswith("added smu2 (ACME Widget 9000, card acme/widget-9000, a draft)"), out
    assert (tmp_path / "instruments" / "smu2" / "manual.pdf").read_bytes() == manual   # not downloaded again
    assert "GPIB0::5::INSTR" not in rt.manuals_found
    rt.gate.approver = ScriptedApprover([False])
    assert rt.set("smu2", "V", 1).startswith("declined")
    assert "its card is a draft" in str(rt.gate.approver.requests[0])

    rt.card_writer = lambda f: {"error": "Kelvin could not find the ACME GIZMO 1 manual online: choose its PDF"}
    assert rt.draft_card("ASRL5::INSTR", "ACME", "GIZMO 1").startswith("no card yet: Kelvin could not find")

    # what the model left out reaches the person; ranges Kelvin could not get are the person's to give
    rt.card_writer = lambda f: dict(writer(f), notes=["Left out: setting the amplitude (no ranges given)",
                                                      "Ranges in: Widget 9000 specifications; for: amplitude"],
                                    ranges_in="Widget 9000 specifications")
    out = rt.draft_card("ASRL5::INSTR", "ACME", "Widget 9000", specs="C:/manuals/w9000-specs.pdf")
    assert "the model's notes: Left out: setting the amplitude" in out and asked[-1]["specs"] == "C:/manuals/w9000-specs.pdf"
    assert "ranges are in 'Widget 9000 specifications', which Kelvin could not get" in out and "with specs" in out


def test_try_card_asks_before_an_address_the_scan_did_not_find(tmp_path, bench):
    rt = file_runtime(tmp_path, SMU_ONLY)
    rt.gate.approver = ScriptedApprover([False, True])
    assert rt.try_card("srs/sr830", "GPIB0::8::INSTR").startswith("declined")           # no scan yet
    assert "Trying the" in str(rt.gate.approver.requests[0]) and "it sets nothing" in str(rt.gate.approver.requests[0])
    assert "matches the card" in rt.try_card("srs/sr830", "GPIB0::8::INSTR")
    assert rt.try_card("srs/sr830", "lockin").startswith("Kelvin tries a card on an instrument in the lab")
    assert "matches the card" in rt.try_card("keithley/2400", "smu")                     # in the lab: no question
    assert len(rt.gate.approver.requests) == 2


def test_kelvin_asks_to_switch_on_an_instrument_a_person_switched_off(tmp_path):
    rt = runtime_on(tmp_path, build_virtual_lab(seed=1, switched_off=lambda name: ["lockin"]), "virtual")
    changed = []
    rt.lab_changed = changed.append
    assert "ask_to_switch_on asks the person" in rt.lab_status()
    rt.gate.approver = ScriptedApprover([False, True])
    assert rt.ask_to_switch_on("lockin", "to read the signal").startswith("declined: lockin stays off")
    request = rt.gate.approver.requests[0]
    assert request.title == "Switch lockin on?" and "to read the signal" in request.items[0]
    assert not rt.lab.instruments["lockin"].enabled and changed == []
    assert rt.ask_to_switch_on("lockin", "to read the signal") == "lockin is on again (the person approved)"
    assert rt.lab.instruments["lockin"].enabled and changed == ["switched"]
    assert rt.ask_to_switch_on("lockin", "again") == "lockin is on"
    assert rt.ask_to_switch_on("nope", "x").startswith("no instrument 'nope'")
    assert [e["by"] for e in events(tmp_path, "instrument_switched")] == ["human (approved Kelvin's request)"]


def test_the_entry_kelvin_writes_and_what_counts_as_the_same_setup():
    reg = builtin_registry()
    lines = instrument_block({"name": "tc", "card": "lakeshore/336", "address": "serial://COM3", "added_by": "kelvin",
                              "connection": {"baud_rate": 57600, "read_termination": "\r\n"},
                              "vars": {"sensor": "A"}}, reg)
    text = add_to_config(SMU_ONLY, [{"name": "tc", "card": "lakeshore/336", "address": "serial://COM3",
                                     "added_by": "kelvin", "connection": {"baud_rate": 57600, "read_termination": "\r\n"}}],
                         reg)
    assert any(line.strip().startswith("added_by: kelvin") for line in lines)
    tc = load_lab_config(text, reg).instruments["tc"]
    assert tc.connection == {"baud_rate": 57600, "read_termination": "\r\n"} and tc.added_by == "kelvin"
    reviewed = load_lab_config(text.replace("    added_by: kelvin", "    # reviewed"), reg)
    assert same_setup(load_lab_config(text, reg), reviewed) and not reviewed.unreviewed()
    assert not same_setup(reviewed, load_lab_config(text.replace("V: [-5, 5]", "V: [-1, 1]"), reg))


def test_the_new_tools_over_mcp(tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    from labharness.agent.server import INSTRUCTIONS
    from test_agent import call
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")   # no home: not the app
    added, drafted, switched = call(rt, ("add_instrument", {"address": "GPIB0::24::INSTR"}),
                                    ("draft_card", {"address": "GPIB0::5::INSTR"}),
                                    ("ask_to_switch_on", {"instrument": "lockin", "why": "to read the signal"}))
    assert added.startswith("not added: this Kelvin keeps no lab config")
    assert "write it yourself" in drafted and switched == "lockin is on"
    assert "add_instrument" in INSTRUCTIONS and "draft_card" in INSTRUCTIONS and "Only a person can add" not in INSTRUCTIONS


def test_the_app_keeps_what_kelvin_changed(tmp_path):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True]), tmp_path / "data")
    settings = Settings(tmp_path / "settings.json")
    settings.set_switched_off(rt.lab.config.name, ["lockin"])
    rt.lab.switch("lockin", False)
    create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"])
    assert rt.card_writer is not None and rt.switched_off_for == settings.switched_off
    assert rt.ask_to_switch_on("lockin", "to read the signal").startswith("lockin is on again")
    assert settings.switched_off(rt.lab.config.name) == []                  # kept across restarts
    rt.lab_changed("added")
    assert settings.get("lab") == "mine"


def test_an_address_that_names_a_program_is_a_persons_to_add(tmp_path, bench):
    rt = file_runtime(tmp_path, SMU_ONLY)
    evil = "lib://COM1?python=C:/evil.exe&path=C:/x"
    assert "names a program for Kelvin to run" in rt.add_instrument(evil, card="keithley/2400", name="cryo")
    assert "names a program for Kelvin to run" in rt.try_card("keithley/2400", evil)
    assert rt.gate.approver.requests == [] and "cryo" not in rt.lab.instruments    # refused without asking anyone


def test_in_a_chat_kelvin_sets_up_an_instrument_it_has_no_card_for(tmp_path, bench, monkeypatch):
    """The person names it; Kelvin scans, writes its card from the maker's manual with the app's model, tries it and
    adds it, through the MCP tools any model uses. Nobody is asked: the scan found it, and nothing is driven."""
    pytest.importorskip("starlette")
    pytest.importorskip("pypdf")
    from labharness import profiles
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from test_autocard import CARD, MANUAL as ACME_MANUAL
    from test_autosetup import tiny_pdf
    from test_web import Scripted, stream_turn
    from starlette.testclient import TestClient
    pages = {"https://www.acme.example/products/widget-9000":
             b'<html><body><a href="/docs/W9000-prog.pdf">Programming manual</a></body></html>',
             "https://www.acme.example/docs/W9000-prog.pdf": tiny_pdf(ACME_MANUAL)}

    def fetch(url):
        if url not in pages:
            raise OSError("HTTP Error 404: Not Found")
        return pages[url]
    monkeypatch.setattr(profiles, "http_get", fetch)
    call = lambda n, name, args: [{"type": "tool_call", "id": "c%d" % n, "name": name, "input": args}]  # noqa: E731
    model = Scripted([call(1, "scan_instruments", {}),
                      call(2, "draft_card", {"address": "GPIB0::5::INSTR"}),
                      [{"type": "text", "text": "https://www.acme.example/products/widget-9000"}],   # where its manual is
                      [{"type": "text", "text": CARD}],                                            # its card, from it
                      call(3, "try_card", {"card": "acme/widget-9000", "instrument": "GPIB0::5::INSTR"}),
                      call(4, "add_instrument", {"address": "GPIB0::5::INSTR", "card": "acme/widget-9000"}),
                      [{"type": "text", "text": "Added the ACME Widget 9000 as smu2."}]])
    rt = file_runtime(tmp_path, SMU_ONLY)
    settings = Settings(tmp_path / "settings.json")
    app = create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"], providers={"scripted": model})
    client, headers = TestClient(app), {"X-Kelvin-Token": settings.access_key}
    with client:
        chat = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        events = stream_turn(client, headers, chat["id"], "The ACME widget on GPIB 5 is connected: add it to the lab",
                             "scripted:m1")
    ends = {e["name"]: e for e in events if e["type"] == "tool_end"}
    assert not any(e.get("is_error") for e in ends.values()), ends
    assert "answered 'ACME,WIDGET 9000" in ends["scan_instruments"]["content"]
    drafted = ends["draft_card"]["content"]   # written from the maker's manual: text from outside the lab
    assert drafted.startswith('<outside_text from="the maker\'s manual') and "\ndrafted card acme/widget-9000" in drafted
    assert "matches the card" in ends["try_card"]["content"]
    assert ends["add_instrument"]["content"].startswith("added smu2 (ACME Widget 9000 source, card acme/widget-9000, a draft)")
    assert rt.lab.instruments["smu2"].card.status == "draft" and rt.lab.config.unreviewed() == ["smu2"]
    assert (tmp_path / "instruments" / "smu2" / "manual.pdf").exists() and settings.get("lab") == "mine"
    assert not any(e["type"] == "approval" for e in events)


def test_auto_mode_adds_and_tries_at_an_address_the_scan_missed(tmp_path, bench):
    rt = file_runtime(tmp_path, SMU_ONLY)
    rt.scan()
    rt.gate.auto = True                                                     # nobody answers: asking would fail
    bench.devices["GPIB0::9::INSTR"] = Device("srs/sr830", name="sr830b")   # plugged in after the scan
    tried = rt.try_card("srs/sr830", "GPIB0::9::INSTR")
    assert "sr830" in tried.lower() and not tried.startswith("declined")   # trying only reads
    assert rt.add_instrument("GPIB0::9::INSTR", card="srs/sr830").startswith("added lockin")
    asks = [e for e in events(tmp_path, "ask")]
    assert [e["answer"] for e in asks] == ["auto-approved", "auto-approved"]
    assert "did not find an instrument at GPIB0::9::INSTR" in asks[1]["request"]
    rt.gate.approver = ScriptedApprover([False])
    assert rt.set("lockin", "amplitude", 0.1).startswith("declined")        # its outputs still ask
    rt.gate.note_outside("a web page")                                      # after text from the web: a person
    bench.devices["GPIB0::12::INSTR"] = Device("srs/sr830", name="sr830c")
    rt.gate.approver = ScriptedApprover([False])
    assert rt.add_instrument("GPIB0::12::INSTR", card="srs/sr830").startswith("declined")
    assert "read text from the web (a web page)" in str(rt.gate.approver.requests[0])
