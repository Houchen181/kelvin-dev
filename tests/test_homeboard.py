"""The person's Home (homeboard.py): the blocks Kelvin puts there as it works, checked, kept in order, kept up to date
by their key, ticked, pinned and removed by a person, and a broken file set aside rather than stopping Kelvin."""
import json

import pytest

from labharness.homeboard import MAX_BLOCKS, HomeBoard, clean, describe


def board(tmp_path):
    return HomeBoard(lambda: tmp_path / "home.json")


def test_a_result_block_with_its_number(tmp_path):
    b, added = board(tmp_path).add({"kind": "result", "title": "Peak position", "value": 2.41, "error": 0.02,
                                    "unit": "V", "run_id": "20261102-140200-gate_sweep"})
    assert added and b["id"].startswith("b-") and b["by"] == "kelvin"
    assert describe(b) == "%s result 'Peak position' 2.41 ± 0.02 V" % b["id"]
    stored = json.loads((tmp_path / "home.json").read_text(encoding="utf-8"))
    assert stored["blocks"][0]["value"] == 2.41 and stored["changed"] > 0


def test_a_key_keeps_one_block_up_to_date(tmp_path):
    home = board(tmp_path)
    first, added = home.add({"kind": "watch", "title": "Sample temperature", "watch": "tc.T_sample", "key": "temp"})
    again, added_again = home.add({"kind": "watch", "title": "Sample temperature (cooling)", "watch": "tc.T_sample",
                                   "key": "temp", "tone": "warn"})
    assert added and not added_again and again["id"] == first["id"]
    assert [b["title"] for b in home.blocks()] == ["Sample temperature (cooling)"]
    assert home.update("temp", {"tone": "ok"})["tone"] == "ok"


def test_what_a_block_must_say(tmp_path):
    home = board(tmp_path)
    for fields, words in [({"title": ""}, "needs a title"), ({"title": "x", "kind": "chart"}, "kind is one of"),
                          ({"title": "x", "kind": "watch"}, "names the reading"),
                          ({"title": "x", "kind": "watch", "watch": "not a key"}, "instrument.parameter"),
                          ({"title": "x", "kind": "check"}, "lists its items"),
                          ({"title": "x" * 81}, "at most 80"),
                          ({"title": "x", "actions": [{"do": "run"}]}, "needs its run_id"),
                          ({"title": "x", "actions": [{"do": "queue"}]}, "needs its text"),
                          ({"title": "x", "actions": [{"do": "pane", "pane": "settings"}]}, "opens one of"),
                          ({"title": "x", "actions": [{"do": "delete"}]}, "do is one of"),
                          ({"title": "x", "value": float("nan")}, "finite"),
                          ({"title": "x", "actions": [{"do": "ask", "text": "a"}] * 3}, "at most 2")]:
        with pytest.raises(ValueError, match=words):
            home.add(fields)
    assert home.blocks() == []


def test_a_suggestion_offers_buttons_with_default_labels(tmp_path):
    b, _ = board(tmp_path).add({"kind": "next", "title": "Narrower sweep around the peak",
                                "text": "2.0 → 2.8 V in 10 mV steps.",
                                "actions": [{"do": "queue", "text": "plan: zoom\nsweep: {smu.V: {from: 2, to: 2.8, step: 0.01}}"},
                                            {"do": "ask", "label": "Why?", "text": "Why that range?"}]})
    assert [a["label"] for a in b["actions"]] == ["Add to sequence", "Why?"]
    assert b["actions"][0]["text"].startswith("plan: zoom")


def test_checklist_ticks_survive_an_update(tmp_path):
    home = board(tmp_path)
    b, _ = home.add({"kind": "check", "title": "Before cooling", "items": ["Pump on", "Valve closed"], "key": "cool"})
    home.tick(b["id"], 0, True)
    changed = home.update("cool", {"items": ["Pump on", "Valve closed", "Heater off"]})
    assert [(i["text"], i["done"]) for i in changed["items"]] == [
        ("Pump on", True), ("Valve closed", False), ("Heater off", False)]
    with pytest.raises(KeyError):
        home.tick(b["id"], 7, True)


def test_order_pins_and_the_oldest_unpinned_go(tmp_path):
    home = board(tmp_path)
    first, _ = home.add({"title": "first"})
    home.pin(first["id"], True)
    for n in range(MAX_BLOCKS + 3):
        home.add({"title": "note %d" % n})
    titles = [b["title"] for b in home.blocks()]
    assert titles[0] == "first" and len(titles) == MAX_BLOCKS + 1
    assert "note 0" not in titles and titles[1] == "note %d" % (MAX_BLOCKS + 2)
    assert home.clear() == MAX_BLOCKS and [b["title"] for b in home.blocks()] == ["first"]


def test_remove_by_id_or_key_and_unknown_refs(tmp_path):
    home = board(tmp_path)
    a, _ = home.add({"title": "a", "key": "k1"})
    home.add({"title": "b"})
    assert home.remove("k1")["id"] == a["id"]
    with pytest.raises(KeyError, match="no block"):
        home.remove(a["id"])
    assert [b["title"] for b in home.blocks()] == ["b"]


def test_a_broken_file_is_set_aside(tmp_path):
    (tmp_path / "home.json").write_text("{not json", encoding="utf-8")
    home = board(tmp_path)
    assert home.blocks() == []
    assert list(tmp_path.glob("home.json.broken-*"))
    home.add({"title": "fresh"})
    assert [b["title"] for b in home.blocks()] == ["fresh"]


def test_the_file_follows_the_data_folder(tmp_path):
    folder = {"now": tmp_path / "ada"}
    home = HomeBoard(lambda: folder["now"] / "home.json")
    home.add({"title": "Ada's"})
    folder["now"] = tmp_path / "ben"
    assert home.blocks() == []
    home.add({"title": "Ben's"})
    folder["now"] = tmp_path / "ada"
    assert [b["title"] for b in home.blocks()] == ["Ada's"]


def test_clean_keeps_what_is_not_given():
    old = clean({"kind": "result", "title": "T", "value": 4.2, "unit": "K"})
    new = clean({"value": 4.1}, old)
    assert (new["title"], new["value"], new["unit"]) == ("T", 4.1, "K")
    assert clean({"text": ""}, new)["text"] == ""
