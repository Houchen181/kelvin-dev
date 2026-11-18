"""Connecting an instrument Kelvin has no card for, end to end (2026-11-18): one that says nothing of who it is but
the instrument collection knows by its USB ID gets its card drafted from the collection's entry, and draft_card reads
a manual on this computer only where the file tools may read, save_manual's folder included."""
from __future__ import annotations

import pytest

from labharness import autocard
from test_driver_cards_2026_11_17 import CARD, SHA1, WIDGET, WIDGET_DRIVER, Catalog, FakeGitHub, driver_model


class NamedCatalog(Catalog):
    """The collection's copy, as discovery names an instrument by its USB ID: its entry by id."""

    def instrument(self, iid):
        return next((e for e in self.entries if e["id"] == iid), None)


def test_an_instrument_named_by_its_usb_id_is_drafted_from_the_collections_entry(tmp_path):
    hub = FakeGitHub()
    hub.add("acme-labs/widget-driver", WIDGET_DRIVER, SHA1)
    prompts = []
    found = {"address": "ASRL5::INSTR", "idn": "", "collection": WIDGET["id"]}   # no reply, no maker or model said
    r = autocard.make_card(found, tmp_path / "cards", driver_model(prompts), hub, catalog=NamedCatalog(WIDGET))
    assert r.get("card_id") == WIDGET["id"], r                       # the entry's id, maker and model
    assert r["driver"]["repo"] == "acme-labs/widget-driver"          # and the driver the entry lists
    assert "Kelvin has no identity reply from it" in prompts[-1][1]  # the model is told it said nothing

    nothing = autocard.make_card({"address": "ASRL5::INSTR", "idn": "", "collection": "acme/unknown"},
                                 tmp_path / "c2", driver_model([]), hub, catalog=NamedCatalog(WIDGET))
    assert nothing["error"] == "Kelvin needs the maker and the model to find the manual"


def _runtime(tmp_path):
    pytest.importorskip("mcp")
    from simlab import build_virtual_lab
    from labharness.agent.runtime import LabRuntime
    from labharness.gate import ScriptedApprover
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    rt.home, rt.cards_dir = tmp_path / "Kelvin", tmp_path / "Kelvin" / "cards"
    asked = []

    def writer(found):
        asked.append(found)
        return {"error": "stopped here by the test"}
    rt.card_writer = writer
    return rt, asked


def test_draft_card_for_an_instrument_the_collection_named_by_its_usb_id(tmp_path):
    from labharness.adapters import resource_name
    rt, asked = _runtime(tmp_path)
    rt.found_info[resource_name("ASRL5::INSTR")] = {"address": "ASRL5::INSTR", "idn": "", "collection": WIDGET["id"],
                                                    "usb": "1313:80f0"}
    assert rt.draft_card("ASRL5::INSTR") == "no card yet: stopped here by the test"
    assert asked[-1]["collection"] == WIDGET["id"] and asked[-1]["idn"] == ""
    rt.found_info[resource_name("ASRL6::INSTR")] = {"address": "ASRL6::INSTR", "idn": ""}   # nothing names it
    assert "did not answer who it is" in rt.draft_card("ASRL6::INSTR") and len(asked) == 1


def test_draft_card_reads_a_manual_here_only_where_the_file_tools_may(tmp_path):
    rt, asked = _runtime(tmp_path)
    kept = rt.home / "manuals" / "ACME-Widget-9000.pdf"   # save_manual's folder, in Kelvin's own: read
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"%PDF-1.4 the manual")
    assert rt.draft_card("", "ACME", "Widget 9000", manual=str(kept)) == "no card yet: stopped here by the test"
    assert asked[-1]["manual"] == str(kept)

    chat = rt.home / "chats" / "c1.json"   # anything else in Kelvin's own folder: never sent to the card's model
    chat.parent.mkdir(parents=True)
    chat.write_text('{"messages": ["ACME Widget 9000 notes"]}', encoding="utf-8")
    for given in ({"manual": str(chat)}, {"specs": str(chat)}):
        said = rt.draft_card("", "ACME", "Widget 9000", **given)
        assert said.startswith("no card yet: ") and "Kelvin's own folder" in said, said
        assert said.endswith("(%s)" % next(iter(given))) and len(asked) == 1

    elsewhere = tmp_path / "Downloads" / "widget.pdf"   # a file the person has: read as before
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"%PDF-1.4 the manual")
    rt.draft_card("", "ACME", "Widget 9000", manual=str(elsewhere))
    assert asked[-1]["manual"] == str(elsewhere)
    rt.draft_card("", "ACME", "Widget 9000", manual="https://www.acme.example/widget.pdf")   # a web address: as before
    assert asked[-1]["manual"] == "https://www.acme.example/widget.pdf"
    assert CARD   # the shared fixtures are the driver tests' own
