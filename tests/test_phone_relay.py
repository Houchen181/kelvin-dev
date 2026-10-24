"""Phones through the Kelvin hub: a Kelvin signed in with a Kelvin account keeps a connection to the hub, and a phone
signed in to the same account reaches it through the hub, with no pairing (hub/relay.py, web/relay.py)."""
import json
import socket
import threading
import time

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")
pytest.importorskip("websockets")
httpx = pytest.importorskip("httpx")
uvicorn = pytest.importorskip("uvicorn")

from starlette.testclient import TestClient  # noqa: E402

from labharness import build_virtual_lab, demo_envelope  # noqa: E402
from labharness.accounts import Accounts  # noqa: E402
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.hub.db import Hub  # noqa: E402
from labharness.hub.server import create_app as hub_app  # noqa: E402
from labharness.web.app import create_app  # noqa: E402
from labharness.web.remote import Remote  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402

from test_web import Scripted  # noqa: E402

ADA = {"email": "ada@example.org", "password": "correct horse"}
PHONE = {"id": "ios-0F3A9C2E-7B1D-4E5F-9A8B-1C2D3E4F5A6B", "name": "Ada's iPhone", "platform": "ios", "version": "0.1.3"}


def free_port() -> socket.socket:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    return s


@pytest.fixture
def hub(tmp_path):
    """A real hub on this computer (uvicorn on 127.0.0.1), as a Kelvin and a phone reach it."""
    sock = free_port()
    port = sock.getsockname()[1]
    h = Hub(tmp_path / "hub")
    h.config["base_url"] = "http://127.0.0.1:%d" % port
    server = uvicorn.Server(uvicorn.Config(hub_app(h, port=port, backups=False), log_level="warning",
                                           lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    yield h, "http://127.0.0.1:%d" % port
    server.should_exit = True
    thread.join(10)


def until(check, seconds=10.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("waited %s s" % seconds)


def kelvin(home, bot):
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), home / "lab", demo_envelope(lab))
    settings = Settings(home / "settings.json")
    app = create_app(runtime, settings, ChatStore(home / "lab" / "chats", create=False), hosts=["testserver"],
                     providers={"scripted": bot}, accounts=Accounts(home))
    client = TestClient(app)
    client.headers["X-Kelvin-Token"] = settings.access_key
    return client, settings, app


def phone_sign_in(hub_url):
    r = httpx.post(hub_url + "/api/signin", json=dict(ADA, installation=PHONE))
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def lines(r):
    return [json.loads(x) for x in r.text.splitlines() if x.strip()]


def test_a_phone_reaches_its_persons_kelvin_through_the_hub(tmp_path, hub, monkeypatch):
    h, url = hub
    monkeypatch.setattr(Remote, "bind_host", "127.0.0.1")   # no firewall asks
    bot = Scripted([[{"type": "text", "text": "The lab is idle."}]])
    client, settings, app = kelvin(tmp_path / "kelvin", bot)
    with client:
        assert client.post("/api/session/hub", json={"url": url}).json()["central"]
        r = client.post("/api/accounts", json=dict(ADA, name="Ada Lovelace", consent=True))
        assert r.status_code == 200, r.text
        phone = phone_sign_in(url)
        kelvins = until(lambda: [k for k in httpx.get(url + "/api/relay/kelvins", headers=phone).json()["kelvins"]
                                 if k["online"]])
        assert len(kelvins) == 1 and kelvins[0]["name"] == socket.gethostname()[:80]
        assert not kelvins[0]["direct"]                  # phone access is off here: only through the hub
        base = url + "/api/relay/kelvins/" + kelvins[0]["id"]
        assert client.get("/api/session").json()["relay"]["state"] == "online"

        state = httpx.get(base + "/api/state", headers=phone)
        assert state.status_code == 200 and state.json()["lab"] == "Virtual Lab"
        assert httpx.get(base + "/api/state").status_code == 401                         # no token, no way through
        assert httpx.post(base + "/api/settings", headers=phone, json={"learn": "off"}).status_code == 403
        assert httpx.post(base + "/api/lab", headers=phone, json={}).status_code == 403  # the lab is the computer's

        chat = httpx.post(base + "/api/chats", headers=phone, json={"model": "scripted:m1"}).json()
        turn = httpx.post(base + "/api/chats/%s/turn" % chat["id"], headers=phone,
                          json={"text": "how is the lab?", "model": "scripted:m1"}, timeout=30)
        assert turn.status_code == 200
        assert "The lab is idle." in "".join(e.get("delta", "") for e in lines(turn) if e["type"] == "text")

        # unshared work stays off the hub; the phone sees it directly, on the lab network or Tailscale
        p = client.post("/api/projects", json={"name": "Secret"}).json()
        client.patch("/api/projects/" + p["id"], json={"sharing": "confidential"})
        secret = client.post("/api/chats", json={"model": "scripted:m1", "project": p["id"]}).json()
        listed = httpx.get(base + "/api/chats", headers=phone)
        assert [c["id"] for c in listed.json()] == [chat["id"]] and listed.headers["x-kelvin-hidden"] == "1"
        refused = httpx.get(base + "/api/chats/" + secret["id"], headers=phone)
        assert refused.status_code == 403 and refused.json()["unshared"]
        assert httpx.post(base + "/api/chats", headers=phone,
                          json={"model": "scripted:m1", "project": p["id"]}).status_code == 403
        assert httpx.post(base + "/api/chats", headers=phone,
                          json={"model": "scripted:m1", "incognito": True}).status_code == 403
        boot = httpx.get(base + "/api/bootstrap", headers=phone).json()
        assert [x["id"] for x in boot["projects"]] == [] and [c["id"] for c in boot["chats"]] == [chat["id"]]

        # direct when possible: with phone access on, the phone gets its own token through the hub
        assert httpx.post(base + "/api/phone/direct", headers=phone).json()["available"] is False
        port = free_port().getsockname()[1]
        client.post("/api/phone", json={"on": True, "port": port})
        until(lambda: httpx.get(url + "/api/relay/kelvins", headers=phone).json()["kelvins"][0]["direct"])
        direct = httpx.post(base + "/api/phone/direct", headers=phone).json()
        assert direct["available"] and direct["token"] and direct["name"] == "Ada's iPhone"
        mine = httpx.get("http://127.0.0.1:%d/api/state" % port, headers={"X-Kelvin-Token": direct["token"]})
        assert mine.status_code == 200 and mine.json()["lab"] == "Virtual Lab"
        assert [d["name"] for d in settings.devices] == ["Ada's iPhone"]
        again = httpx.post(base + "/api/phone/direct", headers=phone).json()          # asked again: one token
        assert again["token"] != direct["token"] and len(settings.devices) == 1
        assert httpx.request("DELETE", base + "/api/phone/direct", headers=phone).json()["ok"]
        assert settings.devices == []
        client.post("/api/phone", json={"on": False})

        # signed out here: the phone finds it offline; signed out on the hub: the phone's token stops working
        client.delete("/api/session")
        until(lambda: not httpx.get(url + "/api/relay/kelvins", headers=phone).json()["kelvins"][0]["online"])
        gone = httpx.get(base + "/api/state", headers=phone)
        assert gone.status_code == 503 and gone.json()["offline"]
        assert httpx.post(url + "/api/signout", headers=phone).status_code == 204
        assert httpx.get(url + "/api/relay/kelvins", headers=phone).status_code == 401


def test_only_a_kelvin_opens_the_relay(tmp_path, hub):
    h, url = hub
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect
    r = httpx.post(url + "/api/register", json=dict(ADA, name="Ada", consent=True, installation=PHONE))
    phone_token = r.json()["access_token"]
    ws_url = url.replace("http://", "ws://") + "/api/relay/kelvin"
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Bearer " + phone_token}):
        with pytest.raises(InvalidStatus):
            connect(ws_url, additional_headers=headers, open_timeout=5).close()
    computer = dict(PHONE, id="urn:uuid:ce8e5cbb-b88b-434d-b17c-9c3def5849ec", platform="win32", name="LAB-PC")
    token = httpx.post(url + "/api/signin", json=dict(ADA, installation=computer)).json()["access_token"]
    with connect(ws_url, additional_headers={"Authorization": "Bearer " + token}, open_timeout=5) as ws:
        ws.send(json.dumps({"t": "hello", "name": "LAB-PC", "version": "0.3.19", "direct": False}))
        phone = {"Authorization": "Bearer " + phone_token}
        listed = until(lambda: httpx.get(url + "/api/relay/kelvins", headers=phone).json()["kelvins"])
        assert [(k["name"], k["online"]) for k in listed] == [("LAB-PC", True)]   # phones aren't listed as Kelvins
        result = {}

        def ask():
            result["r"] = httpx.get(url + "/api/relay/kelvins/%s/api/state" % computer["id"], headers=phone)

        t = threading.Thread(target=ask)
        t.start()
        frame = json.loads(ws.recv(timeout=5))
        assert frame["t"] == "req" and frame["path"] == "/api/state" and frame["phone"]["name"] == "Ada's iPhone"
        ws.send(json.dumps({"t": "head", "id": frame["id"], "status": 200,
                            "headers": [["content-type", "application/json"], ["set-cookie", "x=1"]]}))
        ws.send(frame["id"].to_bytes(4, "big") + b'{"lab": "From the Kelvin"}')
        ws.send(json.dumps({"t": "end", "id": frame["id"]}))
        t.join(10)
        assert result["r"].json() == {"lab": "From the Kelvin"} and "set-cookie" not in result["r"].headers
        bad = httpx.get(url + "/api/relay/kelvins/%s/static/app.js" % computer["id"], headers=phone)
        assert bad.status_code == 404                                            # only the phone API goes through
