"""A sample's pictures (asked for 2026-11-08: "sample kept seperately for each person is fine, but it should be able to
have images"): kept with the sample as given, read-only and by SHA-256, never changed; added from a picture the person
attached in a chat (keep_sample_picture) or in Project settings; shown by sample_history; checked by Verify."""
import asyncio
import os
import stat
import struct
import zlib

import pytest

from labharness.samples import SampleBook

pytest.importorskip("mcp")


def png(seed=0):
    """A small real PNG (2 x 2 pixels), different for each seed."""
    raw = b"".join(b"\0" + bytes([seed % 256, 10, 20] * 2) for _ in range(2))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.fixture
def rt(tmp_path):
    from test_remove_instrument import runtime_on
    return runtime_on(tmp_path, "lab: x\ninstruments:\n  a: {card: srs/sr830}\n")


def test_a_picture_is_kept_with_the_sample_as_given_and_shown_with_its_page(rt):
    said = rt.add_sample_picture("D3", png(1), "png", "after wire bonding, 50x")
    assert said.startswith("kept a picture of D3: after wire bonding, 50x (") and said.endswith(" kB)"), said
    pics = rt.sample_pictures("D3")
    assert len(pics) == 1 and pics[0]["shown"] and pics[0]["mime"] == "image/png"
    path = pics[0]["path"]
    assert open(path, "rb").read() == png(1) and not os.stat(path).st_mode & stat.S_IWRITE   # as given, read-only
    _, s = SampleBook(rt.data_dir).find(None, "D3")
    assert s["pictures"][0]["sha256"] and s["pictures"][0]["caption"] == "after wire bonding, 50x"
    page = rt.sample_history("D3")
    assert page.startswith("D3; 1 picture") and "picture added: %s: after wire bonding, 50x" % pics[0]["file"] in page
    assert rt.add_sample_picture("D3", png(1), "png").startswith("not kept: D3 already has this picture")
    assert rt.add_sample_picture("D3", b"x", "svg").startswith("not kept: a sample's picture is PNG, JPEG")
    assert rt.add_sample_picture("", png(2), "png") == "not kept: name the sample (none is mounted)"
    rt.add_sample_picture("D3", png(2), "jpg", "in the puck")
    assert [p["caption"] for p in rt.sample_pictures("D3")] == ["in the puck", "after wire bonding, 50x"]   # newest first
    e = [x for x in rt.gate.log.events() if x["type"] == "sample_picture"][-1]
    assert e["sample"] == "D3" and e["caption"] == "in the puck" and len(e["sha256"]) == 64


def test_verify_finds_a_picture_changed_after_it_was_kept(rt):
    rt.add_sample_picture("D3", png(3), "png", "the flake")
    assert "Sample pictures: 1 as they were kept" in rt.verify_records()
    path = rt.sample_pictures("D3")[0]["path"]
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    with open(path, "ab") as f:
        f.write(b"\0")
    assert "changed since it was kept" in rt.verify_records()


def test_a_picture_the_person_attached_in_a_chat_is_kept_with_the_sample(tmp_path):
    from labharness import ScriptedApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.agent.server import build_server
    from labharness.web.agent import ChatAgent
    from labharness.web.store import ChatStore
    from simlab import build_virtual_lab
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    store = ChatStore(tmp_path / "chats")
    chat = store.create("scripted:m1")
    first, second = store.put_media(chat["id"], png(4), "png"), store.put_media(chat["id"], png(5), "png")
    chat = store.load(chat["id"])
    chat["messages"] += [{"role": "user", "text": "the chip", "attachments": [
        {"name": "chip.png", "file": first, "mime": "image/png"}]},
        {"role": "user", "text": "and the bonding", "attachments": [
            {"name": "bonding.png", "file": second, "mime": "image/png"}]}]
    agent = ChatAgent(rt, store, build_server(rt), {})
    said, failed = agent._keep_picture(chat, {"sample": "D3", "caption": "bonded"})   # the latest attached
    assert not failed and said.startswith("kept a picture of D3: bonded"), said
    said, failed = agent._keep_picture(chat, {"sample": "D3", "picture": "chip.png", "caption": "the chip"})
    assert not failed
    assert [open(p["path"], "rb").read() for p in rt.sample_pictures("D3")] == [png(4), png(5)]
    assert rt.sample_pictures("D3")[0]["source"] == "attached in a chat as chip.png"
    chat["messages"].append({"role": "user", "text": "D4 and D5", "attachments": [
        {"name": "a.png", "file": store.put_media(chat["id"], png(7), "png"), "mime": "image/png"},
        {"name": "b.png", "file": store.put_media(chat["id"], png(8), "png"), "mime": "image/png"}]})
    agent._keep_picture(chat, {"sample": "D4", "picture": "1"})   # by its place in the latest message
    agent._keep_picture(chat, {"sample": "D5", "picture": "2"})
    assert open(rt.sample_pictures("D4")[0]["path"], "rb").read() == png(7)
    assert open(rt.sample_pictures("D5")[0]["path"], "rb").read() == png(8)
    assert agent._keep_picture(chat, {"sample": "D5", "picture": "3"})[1]   # there is no third
    said, failed = agent._keep_picture(chat, {"sample": "D3", "picture": "nope.png"})
    assert failed and said.startswith("no picture named 'nope.png' attached in this chat")
    asyncio.run(asyncio.sleep(0))


def test_project_settings_adds_and_shows_a_devices_pictures(rt, tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from labharness.web.app import STATIC, create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    settings = Settings(tmp_path / "settings.json")
    client = TestClient(create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"]))
    h = {"X-Kelvin-Token": settings.access_key}
    r = client.post("/api/samples/pictures?device=d1a2b3c4&name=D3&caption=on%20the%20stage", content=png(6),
                    headers=dict(h, **{"X-File-Name": "stage%20photo.png"}))
    assert r.status_code == 200, r.text
    pics = r.json()["pictures"]
    assert [p["caption"] for p in pics] == ["on the stage"] and pics[0]["shown"]
    got = client.get("/api/samples/picture?device=d1a2b3c4&name=D3&file=" + pics[0]["file"], headers=h)
    assert got.status_code == 200 and got.content == png(6) and got.headers["content-type"] == "image/png"
    assert client.get("/api/samples/picture?device=d1a2b3c4&name=D3&file=x.png", headers=h).status_code == 404
    bad = client.post("/api/samples/pictures?device=d1a2b3c4&name=D3", content=b"<svg/>",
                      headers=dict(h, **{"X-File-Name": "x.svg"}))
    assert bad.status_code == 400 and "PNG, JPEG" in bad.json()["error"]
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'rowButton("image", "Add a picture of " + d.name' in js and "/api/samples/pictures?device=" in js
