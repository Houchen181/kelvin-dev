"""Phones: what a phone may do with its own token, and the network listener. Phones connect through the Kelvin account
only: no pairing code, QR code or phone page."""
import json
import socket
import urllib.error
import urllib.request

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")

from starlette.testclient import TestClient  # noqa: E402

from labharness.web.app import create_app  # noqa: E402
from labharness.web import remote  # noqa: E402
from labharness.web.remote import allowed, remote_host_ok  # noqa: E402
from test_web import Scripted, parts  # noqa: E402,F401


# ------------------------------------------------------------------ roles

def test_what_each_role_may_do():
    for method, path in (("GET", "/api/state"), ("POST", "/api/stop"), ("POST", "/api/runs/stop"),
                         ("POST", "/api/alarms/a3/ack"), ("POST", "/api/approvals/r1"), ("GET", "/api/runs/x")):
        assert allowed("watch", method, path) and allowed("control", method, path)
    for method, path in (("GET", "/api/chats"), ("POST", "/api/chats/c1/turn"), ("GET", "/api/reports/x.pdf"),
                         ("POST", "/api/watches")):
        assert allowed("control", method, path) and not allowed("watch", method, path)
    for method, path in (("POST", "/api/settings"), ("POST", "/api/lab"), ("POST", "/api/mode"),
                         ("POST", "/api/instruments/scan"), ("POST", "/api/phone"),
                         ("GET", "/api/phone"), ("POST", "/api/quit"), ("POST", "/api/reports/x.pdf/open"),
                         ("POST", "/api/notify/test"), ("DELETE", "/api/devices/d1"), ("GET", "/api/settingsX")):
        assert not allowed("control", method, path) and not allowed("watch", method, path), path
    assert allowed("owner", "POST", "/api/settings")
    assert remote_host_ok("192.168.1.23:8771") and remote_host_ok("[fe80::1]:8771") and remote_host_ok("lab.tail1.ts.net:8771")
    assert not remote_host_ok("evil.example.com:8771") and not remote_host_ok("")


# ------------------------------------------------------------------ phones' tokens through the API

@pytest.fixture
def app(parts):
    runtime, settings, store = parts
    guard = create_app(runtime, settings, store, hosts=["testserver"], providers={"scripted": Scripted([])})
    return guard, settings


def test_a_phones_token_and_its_role(app):
    guard, settings = app
    owner = {"X-Kelvin-Token": settings.access_key}
    with TestClient(guard) as local:
        guard.remote.port = 8771                       # as if the network listener were running
        phone = TestClient(guard, base_url="http://192.168.1.50:8771")
        assert phone.get("/api/state", headers=owner).status_code == 401      # the access key never works there
        assert phone.get("/", headers={"Host": "evil.example.com:8771"}).status_code == 403
        assert phone.get("/api/state", headers={"Origin": "http://evil.example.com"}).status_code == 403

        # a phone signed in to the Kelvin account gets its own token through the hub (POST /api/phone/direct)
        device, token = guard.remote.issue("  Ana's   phone ", "control", "ios-ana")
        assert device["name"] == "Ana's phone" and len(token) > 30
        ctl = {"X-Kelvin-Token": token}
        me = phone.get("/api/me", headers=ctl).json()
        assert {k: me[k] for k in ("role", "name", "lab", "api")} == \
            {"role": "control", "name": "Ana's phone", "lab": "Virtual Lab", "api": 1}
        assert me["computer"] == socket.gethostname() and me["version"]
        assert all(u.startswith("http://") and u.endswith(":8771") for u in me["addresses"])
        assert [r["url"] for r in me["routes"]] == me["addresses"]
        assert {r["kind"] for r in me["routes"]} <= {"tailscale", "network", "public"}
        assert "computer" not in local.get("/api/me", headers=owner).json()   # only the phone app needs it
        assert phone.post("/api/phone/firewall", json={}, headers=ctl).status_code == 403   # this computer only
        assert phone.get("/api/state", headers=ctl).status_code == 200
        assert phone.get("/api/chats", headers=ctl).status_code == 200
        denied = phone.post("/api/settings", json={"learn": "off"}, headers=ctl)
        assert denied.status_code == 403 and "Use the Kelvin computer" in denied.json()["error"]
        assert phone.get("/api/phone", headers=ctl).status_code == 403

        _, tablet = guard.remote.issue("Lab tablet", "watch", "ios-tablet")   # watch only, as a phone paired before
        watch = {"X-Kelvin-Token": tablet}
        assert phone.get("/api/chats", headers=watch).status_code == 403
        assert phone.post("/api/approvals/r1", json={"approve": True}, headers=watch).status_code == 403
        assert phone.post("/api/approvals/r1", json={"approve": False}, headers=watch).status_code == 409   # no such request
        assert phone.post("/api/runs/stop", json={}, headers=watch).status_code == 200
        assert phone.get("/api/runs/nope/files/report.pdf", headers=watch).status_code == 404   # allowed; no such run
        assert phone.post("/api/runs/nope/files/report.pdf/open", json={}, headers=watch).status_code == 403

        view = local.get("/api/phone", headers=owner).json()
        assert [(d["name"], d["role"]) for d in view["devices"]] == [("Ana's phone", "control"), ("Lab tablet", "watch")]
        assert "hash" not in json.dumps(view) and token not in json.dumps(settings._user)   # only a hash is kept
        assert local.delete("/api/devices/" + view["devices"][1]["id"], headers=owner).json() == {"ok": True}
        assert phone.get("/api/state", headers=watch).status_code == 401
        assert phone.post("/api/me/forget", json={}, headers=ctl).json() == {"ok": True}
        assert phone.get("/api/state", headers=ctl).status_code == 401
        assert local.get("/api/phone", headers=owner).json()["devices"] == []


def test_pairing_is_gone(app):
    """Phones connect through the Kelvin account only: no pairing code, no QR code, no phone page in a browser."""
    guard, settings = app
    owner = {"X-Kelvin-Token": settings.access_key}
    with TestClient(guard) as local:
        guard.remote.port = 8771
        phone = TestClient(guard, base_url="http://192.168.1.50:8771")
        for path in ("/m", "/pair", "/static/mobile.js"):
            assert local.get(path).status_code == 404 and phone.get(path).status_code == 404, path
        assert local.post("/api/devices", json={"name": "Ana's phone"}, headers=owner).status_code == 404
        assert local.get("/api/devices/pairing/123456", headers=owner).status_code == 404
        assert local.post("/api/pair", json={"code": "123456"}, headers=owner).status_code == 404
        assert phone.post("/api/pair", json={"code": "123456"}).status_code == 401   # no token, no way in
        page = local.get("/").text
        assert "Pair a phone" not in page and "pair-box" not in page and 'id="phone-on"' in page
        assert "/api/devices/pairing" not in local.get("/static/app.js").text


def test_the_network_listener_starts_and_stops(app):
    guard, settings = app
    owner = {"X-Kelvin-Token": settings.access_key}
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = "http://127.0.0.1:%d" % port

    def get(path, token=None):
        req = urllib.request.Request(base + path, headers={"X-Kelvin-Token": token} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    guard.remote.bind_host = "127.0.0.1"   # the real thing listens on every interface
    with TestClient(guard) as local:
        view = local.post("/api/phone", json={"on": True, "port": port}, headers=owner).json()
        assert view["on"] and view["listening"] and view["port"] == port and view["error"] is None
        assert settings.phone == (True, port)
        assert get("/")[0] == 200
        assert get("/api/state")[0] == 401
        assert get("/api/state", settings.access_key)[0] == 401
        _, token = guard.remote.issue("phone", "control", "ios-phone")
        status, body = get("/api/state", token)
        assert status == 200 and json.loads(body)["lab"] == "Virtual Lab"
        view = local.post("/api/phone", json={"on": False}, headers=owner).json()
        assert not view["listening"] and guard.remote.port is None
        with pytest.raises(urllib.error.URLError):
            urllib.request.urlopen(base + "/", timeout=3)
        assert local.post("/api/phone", json={"on": True, "port": 80}, headers=owner).status_code == 400


# ------------------------------------------------------------------ reaching the computer

class FakeSocket:
    def __init__(self, *a):
        pass

    def connect(self, where):
        pass

    def getsockname(self):
        return ("100.103.144.14", 50000)        # the default route: Xfinity Wi-Fi hands out 100.64.0.0/10 addresses

    def close(self):
        pass


def test_a_carrier_network_is_not_tailscale(monkeypatch):
    monkeypatch.setattr(remote.socket, "socket", FakeSocket)
    monkeypatch.setattr(remote.socket, "getaddrinfo", lambda *a: [
        (2, 1, 6, "", (ip, 0)) for ip in ("100.103.144.14", "100.101.7.9", "192.168.1.20", "127.0.0.1", "169.254.3.4")])
    found = remote.addresses(8771)
    assert [(a["ip"], a["kind"]) for a in found] == [
        ("100.101.7.9", "tailscale"), ("100.103.144.14", "network"), ("192.168.1.20", "network")]
    assert found[0]["url"] == "http://100.101.7.9:8771"


def test_letting_phones_through_the_firewall(app, monkeypatch):
    guard, settings = app
    owner = {"X-Kelvin-Token": settings.access_key}
    monkeypatch.setattr(remote.sys, "platform", "linux")
    assert remote.firewall_state() is None and remote.allow_through_firewall(8771, "Kelvin.exe")[0] is False
    asked = []
    monkeypatch.setattr("labharness.web.app.allow_through_firewall",
                        lambda port, program: asked.append(port) or (len(asked) > 1, "declined" if len(asked) == 1 else "ok"))
    with TestClient(guard) as local:
        assert local.post("/api/phone/firewall", json={}, headers=owner).status_code == 400   # phone access is off
        guard.remote.port = 8771
        r = local.post("/api/phone/firewall", json={}, headers=owner)
        assert r.status_code == 409 and r.json()["error"] == "declined"
        r = local.post("/api/phone/firewall", json={}, headers=owner)
        assert r.status_code == 200 and r.json()["listening"] and asked == [8771, 8771]
