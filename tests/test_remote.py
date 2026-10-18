"""Phones: QR codes, pairing, what a paired device may do, and the network listener."""
import json
import socket
import urllib.error
import urllib.request

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")

from starlette.testclient import TestClient  # noqa: E402

from labharness.web.app import create_app  # noqa: E402
from labharness.web.qr import QR, format_bits, rs_divisor, rs_remainder  # noqa: E402
from labharness.web.remote import MAX_FAILURES, allowed, remote_host_ok  # noqa: E402
from test_web import Scripted, parts  # noqa: E402,F401


# ------------------------------------------------------------------ QR codes

def read_qr(q):
    """Read a code back: format bits, unmask, zigzag, de-interleave, byte mode. (Version 1-6, level M.)"""
    from labharness.web.qr import _ECC_M, _capacity
    n, m = q.size, q.modules
    fmt = 0
    cells = [(8, i) for i in range(6)] + [(8, 7), (8, 8), (7, 8)] + [(14 - i, 8) for i in range(9, 15)]
    for i, (x, y) in enumerate(cells):
        fmt |= int(m[y][x]) << i
    mask = next(k for k in range(8) if format_bits(k) == fmt)
    raw = []
    right = n - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(n):
            for j in range(2):
                x = right - j
                y = n - 1 - vert if ((right + 1) & 2) == 0 else vert
                if not q.function[y][x]:
                    raw.append(int(m[y][x]) ^ int(QR.mask_bit(mask, x, y)))
        right -= 2
    words = [int("".join(map(str, raw[i:i + 8])), 2) for i in range(0, len(raw) - 7, 8)]
    ec, blocks = _ECC_M[q.version]
    cap = _capacity(q.version)
    per = cap // blocks
    data = [words[i * blocks + b] for b in range(blocks) for i in range(per)]
    for b in range(blocks):   # every block's error correction checks out
        block = data[b * per:(b + 1) * per]
        assert rs_remainder(block, rs_divisor(ec)) == [words[cap + i * blocks + b] for i in range(ec)]
    stream = "".join(format(w, "08b") for w in data)
    assert stream[:4] == "0100"
    length = int(stream[4:12], 2)
    return bytes(int(stream[12 + 8 * i:20 + 8 * i], 2) for i in range(length)).decode("utf-8"), mask


def test_qr_codes():
    assert rs_remainder([32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17], rs_divisor(10)) == \
        [196, 35, 39, 119, 235, 215, 231, 226, 93, 23]                       # ISO 18004 / Thonky's 1-M example
    assert format(format_bits(0), "015b") == "101010000010010"                # level M, mask 0
    assert [format_bits(k, 1) for k in (0, 7)] == [0b111011111000100, 0b110100101110110]   # level L
    for text in ("http://192.168.1.23:8771/pair#482915", "x", "http://lab-pc.tail1234.ts.net:8771/pair#000001",
                 "é" * 40, "a" * 106):                                       # 106 bytes: version 6-M is full
        q = QR(text)
        assert read_qr(q)[0] == text and q.size == 17 + 4 * q.version
        for cx, cy in ((3, 3), (q.size - 4, 3), (3, q.size - 4)):            # the three finder patterns
            assert q.modules[cy][cx] and not q.modules[cy - 2][cx] and q.modules[cy - 3][cx]
    assert QR("http://192.168.1.23:8771/pair#482915").version == 3 and QR("a" * 106).version == 6
    with pytest.raises(ValueError):
        QR("a" * 107)
    svg = QR("hi").svg()
    assert svg.startswith("<svg") and 'role="img"' in svg


# ------------------------------------------------------------------ roles

def test_what_each_role_may_do():
    for method, path in (("GET", "/api/state"), ("POST", "/api/stop"), ("POST", "/api/runs/stop"),
                         ("POST", "/api/alarms/a3/ack"), ("POST", "/api/approvals/r1"), ("GET", "/api/runs/x")):
        assert allowed("watch", method, path) and allowed("control", method, path)
    for method, path in (("GET", "/api/chats"), ("POST", "/api/chats/c1/turn"), ("GET", "/api/reports/x.pdf"),
                         ("POST", "/api/watches")):
        assert allowed("control", method, path) and not allowed("watch", method, path)
    for method, path in (("POST", "/api/settings"), ("POST", "/api/lab"), ("POST", "/api/mode"),
                         ("POST", "/api/instruments/scan"), ("POST", "/api/devices"), ("POST", "/api/phone"),
                         ("GET", "/api/phone"), ("POST", "/api/quit"), ("POST", "/api/reports/x.pdf/open"),
                         ("POST", "/api/notify/test"), ("DELETE", "/api/devices/d1"), ("GET", "/api/settingsX")):
        assert not allowed("control", method, path) and not allowed("watch", method, path), path
    assert allowed("owner", "POST", "/api/settings")
    assert remote_host_ok("192.168.1.23:8771") and remote_host_ok("[fe80::1]:8771") and remote_host_ok("lab.tail1.ts.net:8771")
    assert not remote_host_ok("evil.example.com:8771") and not remote_host_ok("")


# ------------------------------------------------------------------ pairing through the API

@pytest.fixture
def app(parts):
    runtime, settings, store = parts
    guard = create_app(runtime, settings, store, hosts=["testserver"], providers={"scripted": Scripted([])})
    return guard, settings


def test_pairing_and_roles(app):
    guard, settings = app
    owner = {"X-Kelvin-Token": settings.access_key}
    with TestClient(guard) as local:
        assert local.post("/api/devices", json={"name": "Ana's phone"}, headers=owner).status_code == 400   # phone access off
        guard.remote.port = 8771                       # as if the network listener were running
        phone = TestClient(guard, base_url="http://192.168.1.50:8771")
        assert phone.get("/m").status_code == 200 and b"Pair this phone" in phone.get("/pair").content
        assert phone.get("/api/state", headers=owner).status_code == 401      # the access key never works there
        assert phone.get("/m", headers={"Host": "evil.example.com:8771"}).status_code == 403
        assert phone.get("/api/state", headers={"Origin": "http://evil.example.com"}).status_code == 403

        r = local.post("/api/devices", json={"name": "  Ana's   phone ", "role": "control"}, headers=owner).json()
        assert r["name"] == "Ana's phone" and len(r["code"]) == 6 and r["expires_in"] > 500
        assert phone.post("/api/pair", json={"code": "000000" if r["code"] != "000000" else "111111"}).status_code == 403
        paired = phone.post("/api/pair", json={"code": r["code"]}).json()
        assert paired["role"] == "control" and len(paired["token"]) > 30
        assert phone.post("/api/pair", json={"code": r["code"]}).status_code == 403   # once only
        assert local.get("/api/devices/pairing/" + r["code"], headers=owner).json()["device"]
        ctl = {"X-Kelvin-Token": paired["token"]}
        assert phone.get("/api/me", headers=ctl).json() == {"role": "control", "name": "Ana's phone", "lab": "Virtual Lab"}
        assert phone.get("/api/state", headers=ctl).status_code == 200
        assert phone.get("/api/chats", headers=ctl).status_code == 200
        denied = phone.post("/api/settings", json={"learn": "off"}, headers=ctl)
        assert denied.status_code == 403 and "Use the Kelvin computer" in denied.json()["error"]
        assert phone.get("/api/phone", headers=ctl).status_code == 403

        w = local.post("/api/devices", json={"name": "Lab tablet", "role": "watch"}, headers=owner).json()
        watch = {"X-Kelvin-Token": phone.post("/api/pair", json={"code": w["code"]}).json()["token"]}
        assert phone.get("/api/chats", headers=watch).status_code == 403
        assert phone.post("/api/approvals/r1", json={"approve": True}, headers=watch).status_code == 403
        assert phone.post("/api/approvals/r1", json={"approve": False}, headers=watch).status_code == 409   # no such request
        assert phone.post("/api/runs/stop", json={}, headers=watch).status_code == 200

        view = local.get("/api/phone", headers=owner).json()
        assert [(d["name"], d["role"]) for d in view["devices"]] == [("Ana's phone", "control"), ("Lab tablet", "watch")]
        assert "hash" not in json.dumps(view) and paired["token"] not in json.dumps(settings._data)   # only a hash is kept
        tablet = view["devices"][1]["id"]
        assert local.delete("/api/devices/" + tablet, headers=owner).json() == {"ok": True}
        assert phone.get("/api/state", headers=watch).status_code == 401
        assert phone.post("/api/me/forget", json={}, headers=ctl).json() == {"ok": True}
        assert phone.get("/api/state", headers=ctl).status_code == 401
        assert local.get("/api/phone", headers=owner).json()["devices"] == []
        assert local.post("/api/pair", json={"code": "123456"}, headers=owner).status_code == 404   # phones only


def test_wrong_codes_lock_pairing(app):
    guard, settings = app
    owner = {"X-Kelvin-Token": settings.access_key}
    with TestClient(guard) as local:
        guard.remote.port = 8771
        phone = TestClient(guard, base_url="http://10.0.0.7:8771")
        code = local.post("/api/devices", json={"name": "x"}, headers=owner).json()["code"]
        wrong = "%06d" % ((int(code) + 1) % 10 ** 6)
        for _ in range(MAX_FAILURES):
            assert phone.post("/api/pair", json={"code": wrong}).status_code == 403
        locked = phone.post("/api/pair", json={"code": code})
        assert locked.status_code == 429 and "locked" in locked.json()["error"]


def test_the_network_listener_starts_and_stops(app):
    guard, settings = app
    owner = {"X-Kelvin-Token": settings.access_key}
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = "http://127.0.0.1:%d" % port

    def get(path, token=None, data=None):
        req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                                     headers=dict({"Content-Type": "application/json"}, **({"X-Kelvin-Token": token} if token else {})))
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
        assert get("/m")[0] == 200
        assert get("/api/state")[0] == 401
        assert get("/api/state", settings.access_key)[0] == 401
        code = local.post("/api/devices", json={"name": "phone"}, headers=owner).json()["code"]
        status, body = get("/api/pair", data={"code": code})
        token = json.loads(body)["token"]
        status, body = get("/api/state", token)
        assert status == 200 and json.loads(body)["lab"] == "Virtual Lab"
        view = local.post("/api/phone", json={"on": False}, headers=owner).json()
        assert not view["listening"] and guard.remote.port is None
        with pytest.raises(urllib.error.URLError):
            urllib.request.urlopen(base + "/m", timeout=3)
        assert local.post("/api/phone", json={"on": True, "port": 80}, headers=owner).status_code == 400
