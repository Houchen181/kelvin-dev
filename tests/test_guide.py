"""Kelvin knows its own portal: the models get a map of where things are (data/portal.md) and kelvin_help searches
Kelvin's documentation, so they send people to the right place instead of guessing at a menu."""
import html
import json
import re
from pathlib import Path

import pytest

from labharness import guide

STATIC = Path(guide.__file__).parent / "web" / "static"


def test_every_name_on_the_map_is_in_the_portal():
    ui = html.unescape((STATIC / "index.html").read_text(encoding="utf-8") +
                       (STATIC / "app.js").read_text(encoding="utf-8"))
    names = set(re.findall(r"\*\*(.+?)\*\*", guide.portal_map()))
    assert len(names) > 60 and not sorted(n for n in names if n not in ui)   # renamed in the portal: fix the map
    assert guide.portal_map().startswith("# Where things are in Kelvin's portal")


def test_kelvin_help_finds_where_to_go():
    found = guide.search("how do I review a draft card")
    assert found.startswith("[the portal's map › Where things are in Kelvin's portal › A draft card]")
    assert "**Lab** → **Cards…**" in found and "**Mark reviewed**" in found
    found = guide.search("change the limits of an instrument Kelvin added")
    assert "**Limits reviewed…**" in found.split("\n\n")[0] and "Save and connect" in found
    assert guide.search("what does auto mode allow").startswith("[safety.md › Safety › The rules › Auto mode]")
    assert guide.search("how").startswith("say what to look up")
    assert guide.search("zzzz qqqq").startswith("nothing in Kelvin's documentation")


def test_long_passages_come_clipped(monkeypatch):
    monkeypatch.setattr(guide, "MAX_PASSAGE", 50)
    found = guide.search("review a draft card", 1)
    assert found.endswith(" …") and len(found.split("\n", 1)[1]) == 52


def test_the_portal_model_has_the_map_and_kelvin_help(tmp_path):
    pytest.importorskip("mcp")
    import anyio
    from mcp import Client
    from labharness import ScriptedApprover, build_virtual_lab
    from labharness.agent.runtime import LabRuntime
    from labharness.agent.server import INSTRUCTIONS, build_server
    from labharness.web.agent import SYSTEM
    assert SYSTEM.endswith(guide.portal_map()) and "look it up with kelvin_help, never guess" in " ".join(
        INSTRUCTIONS.split())
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")

    async def ask():
        async with Client(build_server(rt)) as client:
            return (await client.call_tool("kelvin_help", {"question": "review a draft card"})).content[0].text
    assert "**Mark reviewed**" in anyio.run(ask)


def test_the_lab_panel_names_instruments_on_draft_cards(tmp_path):
    pytest.importorskip("mcp")
    from labharness import ScriptedApprover, build_virtual_lab
    from labharness.agent.runtime import LabRuntime
    lab = build_virtual_lab()
    rt = LabRuntime(lab, ScriptedApprover([]), tmp_path / "data")
    assert rt.ui_state()["drafts"] == []                                   # simulated instruments never count
    smu = lab.instruments["smu"]
    smu.backend.simulated, smu.card.status = False, "draft"                # a real instrument on a draft card
    assert rt.ui_state()["drafts"] == ["smu"]


def test_a_person_reviews_one_of_kelvins_own_draft_cards(tmp_path):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    from labharness import build_virtual_lab, carddraft
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.virtual_lab import lab_registry
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from test_web import client_for
    lab = build_virtual_lab(registry=lab_registry(tmp_path))
    rt = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data")
    tc = lab.instruments["tc"]
    tc.backend.simulated, tc.card.status = False, "draft"           # a real instrument on a card that came as a draft
    assert rt.gate.unsure(tc).startswith("its card is a draft")
    client, headers = client_for((rt, Settings(tmp_path / "settings.json"), ChatStore(tmp_path / "chats")), {})
    with client:
        listed = client.get("/api/cards", headers=headers).json()["kelvins"]
        kelvins = next(c for c in listed if c["id"] == "virtual/temperature-controller")
        assert kelvins["instruments"] == ["tc"] and kelvins["reviewable"]
        assert kelvins["status"] == "draft" and "virtual/temperature-controller" in kelvins["yaml"]
        assert {c["id"] for c in listed} >= {"virtual/source-meter", "virtual/lock-in"}   # any can get a lab version
        r = client.post("/api/cards/status", json={"id": kelvins["id"], "status": "lab-verified"}, headers=headers)
        assert r.json() == {"id": "virtual/temperature-controller", "status": "lab-verified"}
    assert rt.gate.unsure(tc) is None                                  # at once, without reconnecting
    reviews = carddraft.builtin_reviews(tmp_path / "cards")
    assert reviews["virtual/temperature-controller"]["status"] == "lab-verified"
    assert lab_registry(tmp_path).get("virtual/temperature-controller").status == "lab-verified"   # next start
    reviews["virtual/temperature-controller"]["sha"] = "an older card"
    (tmp_path / "cards" / carddraft.REVIEWS).write_text(json.dumps(reviews), encoding="utf-8")
    assert lab_registry(tmp_path).get("virtual/temperature-controller").status == "community"      # changed: asks
