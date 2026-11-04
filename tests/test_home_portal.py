"""Home in the portal: the person's page of blocks (homeboard.py) through /api/home, its stamp in /api/state, the
blocks Kelvin's AI puts there with its home tools, and none of them from an incognito chat."""
import json

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")
anyio = pytest.importorskip("anyio")

from simlab import build_virtual_lab  # noqa: E402
from labharness import ScriptedApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.homeboard import HomeBoard  # noqa: E402
from labharness.web.agent import DEFERRED_NAMES, HOME_TOOLS, LOOKUPS, OFF_LAB  # noqa: E402

from test_lab_memory import portal  # noqa: E402
from test_web import DONE, Scripted, stream_turn  # noqa: E402


def call(rt, *calls):
    """Kelvin's tools over a real MCP client: [(name, arguments)] → their answers' text."""
    from mcp import Client

    async def go():
        async with Client(build_server(rt)) as client:
            out = []
            for name, args in calls:
                out.append((await client.call_tool(name, args)).content[0].text)
            return out
    return anyio.run(go)


def test_the_page_reads_and_a_person_pins_ticks_and_removes(tmp_path):
    rt, store, client, headers = portal(tmp_path, {})
    with client:
        assert client.get("/api/home", headers=headers).json() == {"blocks": [], "changed": 0.0}
        before = client.get("/api/state", headers=headers).json()["home"]
        check, _ = rt.home_board.add({"kind": "check", "title": "Before cooling", "items": ["Pump on", "Valve shut"]})
        result, _ = rt.home_board.add({"kind": "result", "title": "Peak", "value": 2.41, "error": 0.02, "unit": "V"})
        assert (rt.data_dir / "home.json").exists()   # beside the person's queue
        assert client.get("/api/state", headers=headers).json()["home"] != before
        blocks = client.get("/api/home", headers=headers).json()["blocks"]
        assert [b["title"] for b in blocks] == ["Peak", "Before cooling"]
        ticked = client.post("/api/home/blocks/%s" % check["id"], json={"tick": 1, "done": True}, headers=headers)
        assert [i["done"] for i in ticked.json()["blocks"][1]["items"]] == [False, True]
        pinned = client.post("/api/home/blocks/%s" % check["id"], json={"pin": True}, headers=headers).json()
        assert pinned["blocks"][0]["id"] == check["id"]   # pinned first
        gone = client.delete("/api/home/blocks/%s" % result["id"], headers=headers)
        assert [b["id"] for b in gone.json()["blocks"]] == [check["id"]]
        assert client.delete("/api/home/blocks/%s" % result["id"], headers=headers).status_code == 404
        assert client.post("/api/home/blocks/%s" % check["id"], json={"tick": 9}, headers=headers).status_code == 404
        assert client.post("/api/home/blocks/%s" % check["id"], json={}, headers=headers).status_code == 400
        cleared = client.post("/api/home", json={"op": "clear"}, headers=headers).json()
        assert cleared["message"] == "0 blocks taken off Home" and len(cleared["blocks"]) == 1   # the pinned one stays
        assert client.get("/api/home", headers={"X-Kelvin-Token": "wrong"}).status_code == 401


def test_kelvins_tools_put_blocks_on_home(tmp_path):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    assert call(rt, ("home_list", {}))[0].startswith("Home is a page of Kelvin's portal")   # no Home over plain MCP
    rt.home_board = HomeBoard(lambda: rt.data_dir / "home.json")
    added, again, wrong, listed, changed, removed, missing = call(rt, (
        "home_add", {"title": "Sample temperature", "kind": "watch", "watch": "tc.T_sample", "key": "temp"}), (
        "home_add", {"title": "Sample temperature", "kind": "watch", "watch": "tc.T_sample", "key": "temp",
                     "tone": "warn"}), (
        "home_add", {"title": "Next", "kind": "next", "actions": [{"do": "queue"}]}), (
        "home_list", {}), (
        "home_update", {"block": "temp", "title": "Sample T"}), (
        "home_remove", {"block": "temp"}), (
        "home_update", {"block": "temp", "title": "x"}))
    assert added.startswith("on Home: b-") and "watch tc.T_sample" in added
    assert again.startswith("changed on Home: ") and added.split()[2] == again.split()[3]   # the same block
    assert wrong == "not on Home: a queue button needs its text (the scan's plan or a step)"
    assert listed.count("\n") == 0 and "(key temp)" in listed
    assert changed.startswith("changed on Home:") and "'Sample T'" in changed
    assert removed.startswith("taken off Home:") and missing.startswith("not changed: no block 'temp' on Home")
    assert rt.home_board.blocks() == []


def test_home_tools_work_beside_the_lab_and_only_home_add_is_always_loaded():
    assert HOME_TOOLS <= OFF_LAB and "home_list" in LOOKUPS
    assert {"home_update", "home_remove", "home_list"} <= DEFERRED_NAMES and "home_add" not in DEFERRED_NAMES


def test_an_incognito_chat_puts_nothing_on_home(tmp_path):
    add = [{"type": "tool_call", "id": "h1", "name": "home_add", "input": {"title": "Peak", "value": 2.4}}]
    bot = Scripted([add, DONE, add, DONE])
    rt, store, client, headers = portal(tmp_path, {"scripted": bot})
    with client:
        hidden = client.post("/api/chats", json={"model": "scripted:m1", "incognito": True}, headers=headers).json()
        end = [e for e in stream_turn(client, headers, hidden["id"], "keep it", "scripted:m1")
               if e["type"] == "tool_end"][0]
        assert "home_add" not in bot.tool_names[0]
        assert end["is_error"] and end["content"].startswith("not in an incognito chat")
        assert rt.home_board.blocks() == []
        saved = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        said = [e for e in stream_turn(client, headers, saved["id"], "keep it", "scripted:m1")
                if e["type"] == "tool_end"][0]
    assert not said["is_error"] and said["content"].startswith("on Home:")
    block = rt.home_board.blocks()[0]
    assert block["title"] == "Peak" and block["chat"] == saved["id"]   # where it came from
    assert "home_add" in bot.tool_names[-1] and "Home" in bot.systems[-1]
    assert json.loads((rt.data_dir / "home.json").read_text(encoding="utf-8"))["blocks"][0]["by"] == "kelvin"
