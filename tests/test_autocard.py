"""Kelvin writes the card for an instrument it does not know: finds the manual, drafts the card, keeps it safe."""
import json
import types

import pytest

from labharness import autocard, builtin_registry, profiles
from labharness.adapters import set_resource_manager
from labharness.carddraft import check
from test_hardware import Bus, Device

pytest.importorskip("pypdf")
from test_autosetup import FakeSettings, runtime_for, tiny_pdf  # noqa: E402

IDN = "ACME,WIDGET 9000,SN12345,1.0"
COMMANDS = ["VOLTage <v> sets the output voltage; VOLT? reads it back.", "MEASure:CURRent? measures the current.",
            "RANGe <r> selects the range; RANG? reads it.", "*IDN? returns ACME,WIDGET 9000,<serial>,<firmware>.",
            "SYSTem:ERRor? reads the error queue.", "OUTPut:STATe ON|OFF switches the output; OUTP? reads it."]
MANUAL = [["ACME WIDGET 9000 Programming Manual", "Introduction to remote control."] + ["Front panel overview."] * 30,
          ["Chapter 4 Command reference"] + COMMANDS * 4,
          ["Chapter 4 (continued)"] + COMMANDS * 4]
CARD = """```yaml
card: 1
id: acme/widget-9000
model: ACME Widget 9000 source
kind: source-meter
status: community
identify: { idn: "^NOT-THIS" }
error_query: "SYSTem:ERRor?"
on_connect: ["SYSTem:REMote"]
parameters:
  V:
    label: Voltage
    kind: drive
    unit: V
    set: "VOLTage {value}"
    get: "VOLTage?"
    limits: [-10, 10]
    safe: 0
    ramp: gate
    max_rate: 1
  I:
    label: Current
    kind: measure
    unit: A
    get: "MEASure:CURRent?"
  range:
    label: Range
    kind: measure
    unit: V
    set: "RANGe {value}"
    get: "RANGe?"
display: { headline: I }
```
I was unsure about the range values."""


@pytest.fixture
def web():
    pdf = tiny_pdf(MANUAL)
    other = tiny_pdf([["Another product's manual"] + COMMANDS * 8])
    pages = {
        "https://www.acme.example/products/widget-9000":
            b'<html><body><a href="/docs/W9000-prog.pdf">Programming manual</a> <a href="/docs/brochure.pdf">'
            b'Brochure</a> <a href="https://mirror.example/W9000.pdf">elsewhere</a></body></html>',
        "https://www.acme.example/docs/W9000-prog.pdf": pdf,
        "https://www.acme.example/docs/brochure.pdf": other,
    }
    asked = []

    def fetch(url):
        asked.append(url)
        if url not in pages:
            raise OSError("HTTP Error 404: Not Found")
        return pages[url]
    fetch.asked = asked
    return fetch


def fake_model(prompts):
    def ask(system, prompt):
        prompts.append(prompt)
        if system == autocard.SUGGEST_SYSTEM:
            return ("https://www.acme.example/support/old-manual.pdf\n"
                    "https://www.acme.example/products/widget-9000", "test:model")
        return CARD, "test:model"
    return ask


def test_the_manual_is_found_through_the_makers_page(web):
    prompts = []
    m = autocard.find_manual("ACME", "WIDGET 9000", fake_model(prompts), web)
    assert m["url"] == "https://www.acme.example/docs/W9000-prog.pdf" and m["kind"] == "pdf" and m["score"] >= 40
    assert "https://mirror.example/W9000.pdf" not in web.asked                  # only the maker's own site
    assert "WIDGET 9000" in prompts[0] and "SN12345" not in prompts[0]
    assert autocard.find_manual("ACME", "GIZMO 1", None, web) is None             # nothing names it
    srs = autocard.find_manual("Stanford_Research_Systems", "SR999", None, lambda url: (_ for _ in ()).throw(OSError(url)))
    assert srs is None
    assert "SR999" in autocard.KNOWN[0][1].format(MODEL="SR999")


def test_the_command_pages_go_to_the_model():
    pages = ["intro " * 50, "VOLT? MEAS:CURR? *IDN? " * 10, "warranty", "SYST:ERR? OUTP? " * 5]
    excerpt = autocard.command_excerpt(pages)
    assert excerpt.startswith("[page 2]") and "[page 4]" in excerpt and "warranty" not in excerpt
    assert autocard.names_model("The SR865A lock-in", "SR865A") and not autocard.names_model("The SR860", "SR865A")
    assert autocard.urls_in("see https://a.example/x.pdf, and (https://b.example/y).") == [
        "https://a.example/x.pdf", "https://b.example/y"]


def test_whatever_the_model_writes_the_card_is_safe_to_try():
    text, changed = autocard.make_safe(CARD, "ACME", "WIDGET 9000", IDN, "https://www.acme.example/m.pdf", "test:model")
    card, problems, _ = check(text)
    assert problems == [] and card.status == "draft" and card.on_connect == []
    assert card.idn_pattern and __import__("re").search(card.idn_pattern, IDN)      # from the instrument's own reply
    assert "I" not in card.params and card.params["V"].get_cmd is not None           # MEASure? left out, VOLT? kept
    assert card.manual_url == "https://www.acme.example/m.pdf" and text.startswith("# Drafted by Kelvin")
    assert any("MEASure:CURRent?" in c for c in changed) and any("on connect" in c for c in changed)
    assert any(t.text.startswith("CAL") for t in card.blocked)


def test_a_card_is_written_and_kept_as_a_draft(tmp_path, web):
    prompts = []
    r = autocard.make_card({"address": "GPIB0::5::INSTR", "idn": IDN}, tmp_path / "cards", fake_model(prompts), web,
                           builtin_ids=[c.id for c in builtin_registry()])
    assert r["card_id"] == "acme/widget-9000" and r["kind"] == "source-meter", r
    assert r["manual"]["url"].endswith("W9000-prog.pdf") and r["manual"]["data"][:5] == b"%PDF-"
    saved = (tmp_path / "cards" / "acme_widget-9000.yaml").read_text(encoding="utf-8")
    assert "status: draft" in saved and "on_connect" not in saved
    assert not any("SN12345" in p for p in prompts)                                   # never the serial number
    assert "Command reference" in prompts[1] and "Front panel overview" not in prompts[1]

    assert "add an API key" in autocard.make_card({"idn": IDN}, tmp_path, None, web)["error"]
    lost = autocard.make_card({"idn": "ACME,GIZMO 1,1,1"}, tmp_path, fake_model([]), web)["error"]
    assert "could not find the ACME GIZMO 1 manual" in lost and "choose its PDF" in lost


def test_first_start_writes_the_card_and_sets_the_instrument_up(tmp_path, web):
    from labharness.autosetup import AutoSetup
    widget = Device(idn=IDN)
    widget.twin = types.SimpleNamespace(query=lambda cmd: "0", write=lambda cmd: None)   # answers every read with 0
    set_resource_manager(Bus({"GPIB0::5::INSTR": widget}))
    try:
        rt = runtime_for(tmp_path)
        rt.scan_network = False
        ids = [c.id for c in builtin_registry()]
        make = lambda f: autocard.make_card(f, tmp_path / "cards", fake_model([]), web, builtin_ids=ids)  # noqa: E731
        status = AutoSetup(rt, FakeSettings(), tmp_path, fetch=web, make_card=make).run()
        assert status["state"] == "done" and status["written"] == ["ACME Widget 9000 source"], status
        assert "wrote draft cards" in status["message"]
        lab = (tmp_path / "lab.yaml").read_text(encoding="utf-8")
        assert "acme/widget-9000" in lab and "observe_only: true" in lab
        assert rt.lab.instruments["smu"].card.status == "draft"
        folder = tmp_path / "instruments" / "smu"
        assert (folder / "manual.pdf").exists() and (folder / "manual.txt").exists()   # kept, not fetched again
        assert web.asked.count("https://www.acme.example/docs/W9000-prog.pdf") == 1
        assert json.loads((folder / "instrument.json").read_text())["parsed"]["model"] == "WIDGET 9000"
        assert any(n["title"].startswith("Kelvin wrote a card") for n in rt.notifier.notices)
        assert "refused" in rt.set("smu", "V", 1)                                       # outputs locked

        off = AutoSetup(runtime_for(tmp_path / "b"), FakeSettings(auto_cards="off"), tmp_path / "b",
                        make_card=lambda f: pytest.fail("switched off")).run()
        assert off["state"] == "done"
    finally:
        set_resource_manager(None)


def test_lab_setup_writes_a_card_with_the_users_model(tmp_path, web, monkeypatch):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from starlette.testclient import TestClient
    from test_web import Scripted
    monkeypatch.setattr(profiles, "http_get", web)
    model = Scripted([[{"type": "text", "text": "https://www.acme.example/products/widget-9000"}],
                      [{"type": "text", "text": CARD}]])
    rt = runtime_for(tmp_path)
    settings = Settings(tmp_path / "settings.json")
    app = create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"], providers={"scripted": model})
    headers = {"X-Kelvin-Token": settings.access_key}
    with TestClient(app) as client:
        r = client.post("/api/instruments/card", json={"address": "GPIB0::5::INSTR", "idn": IDN}, headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["card_id"] == "acme/widget-9000" and body["by"] == "scripted:m1"
        assert body["manual_url"].endswith("W9000-prog.pdf")
        cards = {c["id"]: c for c in client.get("/api/lab", headers=headers).json()["cards"]}
        assert cards["acme/widget-9000"]["status"] == "draft"
        bad = client.post("/api/instruments/card", json={"address": "ASRL9::INSTR", "idn": ""}, headers=headers)
        assert bad.status_code == 400


def test_without_a_model_kelvin_says_so(tmp_path, web):
    def no_model(system, prompt):
        raise ValueError("no model to write the card with: add an API key in Settings")
    err = autocard.make_card({"idn": IDN}, tmp_path, no_model, web)["error"]
    assert "could not ask a model" in err and "add an API key" in err and "find" not in err


def test_the_start_up_message_names_a_card_it_could_not_write(tmp_path):
    from labharness.autosetup import AutoSetup
    set_resource_manager(Bus({"GPIB0::24::INSTR": Device("keithley/2400", name="k"), "GPIB0::5::INSTR": Device(idn=IDN)}))
    try:
        rt = runtime_for(tmp_path)
        rt.scan_network = False
        status = AutoSetup(rt, FakeSettings(), tmp_path, fetch=lambda url: b"",
                           make_card=lambda f: {"error": "Kelvin could not find the ACME WIDGET 9000 manual online"}).run()
        assert status["message"].startswith("1 instrument set up; Kelvin could not find the ACME WIDGET 9000 manual")
    finally:
        set_resource_manager(None)
