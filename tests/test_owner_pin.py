"""The lab owner's PIN (field tests: anyone could raise limits to the card's maximum, delete interlocks, switch on auto
mode or unlock Hands on the sample, and Kelvin explained the clicks to a student). Optional; once set, what widens
Kelvin's reach over the instruments needs it, and narrowing never does."""
import pytest

from labharness import build_virtual_lab
from labharness.accounts import OwnerPin

pytest.importorskip("mcp")
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402


def test_the_pin_is_optional_kept_hashed_and_locks_after_wrong_tries(tmp_path, monkeypatch):
    pin = OwnerPin(tmp_path)
    assert not pin.is_set and pin.check(None) is None                 # no PIN: everyone, as before
    with pytest.raises(ValueError, match="4 to 12 digits"):
        pin.set("12ab")
    pin.set("2468")
    assert pin.is_set and "2468" not in (tmp_path / "owner.json").read_text(encoding="utf-8")
    assert pin.check(None) == "this needs the lab owner's PIN" and pin.check("2468") is None
    with pytest.raises(ValueError, match="needs the lab owner's PIN"):
        pin.set("1357")                                                # changing it takes the PIN
    for _ in range(OwnerPin.TRIES):
        assert pin.check("0000") == "that is not the lab owner's PIN"
    assert pin.check("2468").startswith("too many wrong PINs")        # locked for a while, even the right one
    monkeypatch.setattr(OwnerPin, "LOCKED_S", 0.0)
    assert pin.check("2468") is None
    pin.set("", old="2468")
    assert not pin.is_set


def test_with_a_pin_auto_mode_and_unlocking_need_it_and_narrowing_never_does(tmp_path):
    rt = LabRuntime(build_virtual_lab(seed=1), WebApprover(timeout=5), tmp_path / "data")
    rt.owner = OwnerPin(tmp_path / "Kelvin")
    rt.owner.set("2468")
    with pytest.raises(PermissionError, match="needs the lab owner's PIN"):
        rt.set_approval_mode("auto", "human (approval page)")
    assert rt.set_approval_mode("auto", "human (portal)", pin="2468")["mode"] == "auto"
    assert rt.set_approval_mode("ask", "human (portal)")["mode"] == "ask"   # back to asking: no PIN
    rt.hands_on("human (phone watch)")                                       # locking: no PIN
    with pytest.raises(PermissionError):
        rt.hands_off("human (portal)", pin="1111")
    assert rt.hands is not None
    rt.hands_off("human (portal)", pin="2468")
    assert rt.hands is None


def test_the_portal_asks_for_the_pin_before_anything_widens(tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    rt = LabRuntime(build_virtual_lab(seed=1), WebApprover(timeout=5), tmp_path / "data")
    home = tmp_path / "Kelvin"
    settings = Settings(home / "settings.json")
    client = TestClient(create_app(rt, settings, ChatStore(home / "chats"), hosts=["testserver"], providers={}))
    h = {"X-Kelvin-Token": settings.access_key}
    with client:
        assert client.get("/api/owner-pin", headers=h).json() == {"set": False}
        assert client.post("/api/mode", json={"mode": "auto"}, headers=h).json()["mode"] == "auto"   # none set
        client.post("/api/mode", json={"mode": "ask"}, headers=h)
        assert client.post("/api/owner-pin", json={"pin": "2468"}, headers=h).json() == {"set": True}
        r = client.post("/api/owner-pin", json={"pin": "1111"}, headers=h)          # not without the PIN
        assert r.status_code == 403 and r.json()["pin"]
        r = client.post("/api/mode", json={"mode": "auto"}, headers=h)
        assert r.status_code == 403 and r.json()["pin"] and not rt.gate.auto
        assert client.post("/api/mode", json={"mode": "auto", "pin": "2468"}, headers=h).json()["mode"] == "auto"
        r = client.post("/api/lab", json={"yaml": "lab: wide open\ninstruments: {}\n"}, headers=h)
        assert r.status_code == 403 and r.json()["pin"] and not (home / "lab.yaml").exists()
        assert client.post("/api/hands", json={"on": True}, headers=h).status_code == 200
        r = client.post("/api/hands", json={"on": False}, headers=h)
        assert r.status_code == 403 and rt.hands is not None
        assert client.post("/api/hands", json={"on": False, "pin": "2468"}, headers=h).status_code == 200
        assert rt.hands is None
    events = [e["type"] for e in rt.gate.log.events()]
    assert "owner_pin" in events
    js = (__import__("labharness.web.app", fromlist=["STATIC"]).STATIC / "app.js").read_text(encoding="utf-8")
    assert "function askPin(why, label)" in js and "data.pin && opts.body" in js


def test_marking_a_card_reviewed_and_lifting_a_protection_need_the_pin_too(tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    gate = {"gates": [{"drive": "smu.V", "dielectric": "hBN", "thickness_nm": 20, "leak": "smu.I"}]}
    rt = LabRuntime(build_virtual_lab(seed=1), WebApprover(timeout=5), tmp_path / "data")
    home = tmp_path / "Kelvin"
    settings = Settings(home / "settings.json")
    client = TestClient(create_app(rt, settings, ChatStore(home / "chats"), hosts=["testserver"], providers={}))
    h = {"X-Kelvin-Token": settings.access_key}
    with client:
        p = client.post("/api/projects", json={"name": "Graphene"}, headers=h).json()
        d4 = client.post("/api/projects/%s/devices" % p["id"], json={"name": "D4"}, headers=h).json()["devices"][0]
        url = "/api/projects/%s/devices/%s" % (p["id"], d4["id"])
        assert client.patch(url, json={"protection": gate}, headers=h).status_code == 200   # protecting: no PIN
        client.post("/api/owner-pin", json={"pin": "2468"}, headers=h)
        r = client.patch(url, json={"protection": {}}, headers=h)                         # lifting it: the PIN
        assert r.status_code == 403 and r.json()["pin"]
        assert client.patch(url, json={"protection": {}, "pin": "2468"}, headers=h).status_code == 200
        client.patch(url, json={"protection": gate, "pin": "2468"}, headers=h)
        assert client.post("/api/mount", json={"project": p["id"], "device": "D4"}, headers=h).status_code == 200
        r = client.post("/api/mount", json={}, headers=h)                                 # taking it out: the PIN
        assert r.status_code == 403 and rt.mounted.get("device_name") == "D4"
        assert client.post("/api/mount", json={"pin": "2468"}, headers=h).status_code == 200 and not rt.mounted
        r = client.post("/api/cards/status", json={"id": "virtual/source-meter", "status": "lab-verified"}, headers=h)
        assert r.status_code == 403 and r.json()["pin"]                                    # marking reviewed: the PIN
        r = client.post("/api/cards/status", json={"id": "nobody/card", "status": "draft"}, headers=h)
        assert r.status_code != 403                                                        # back to draft: never
