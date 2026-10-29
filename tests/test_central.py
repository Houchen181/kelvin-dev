"""Central Kelvin accounts: one account on the Kelvin hub signs a person in on every computer (central.py)."""
import json

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")
pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402

from labharness import central  # noqa: E402
from simlab import build_virtual_lab, demo_envelope  # noqa: E402
from labharness.accounts import Accounts  # noqa: E402
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.hub.db import Hub  # noqa: E402
from labharness.hub.server import create_app as hub_app  # noqa: E402
from labharness.web.app import create_app  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402

from test_hub_web import through  # noqa: E402

try:
    import cryptography  # noqa: F401
    CRYPTO = True
except ImportError:
    CRYPTO = False

HUB = "https://hub.test"
ADA = {"email": "ada@example.org", "password": "correct horse"}


@pytest.fixture(autouse=True)
def no_keys_in_the_environment(monkeypatch):
    from labharness.web.settings import ENV
    for name in list(ENV.values()) + ["GOOGLE_API_KEY"]:
        monkeypatch.delenv(name, raising=False)


class Line:
    """The way to the hub: up (answered by the test hub) or down (as if offline)."""

    def __init__(self, hub_client):
        self.up = True
        self.http = through(hub_client)

    def __call__(self, *args, **kw):
        if not self.up:
            raise OSError("offline")
        return self.http(*args, **kw)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    h = Hub(tmp_path / "hub")
    line = Line(TestClient(hub_app(h, hosts=["testserver"], backups=False)))
    monkeypatch.setattr("labharness.hub.client._http", line)
    return h, line


def computer(home):
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), home / "lab", demo_envelope(lab))
    settings = Settings(home / "settings.json")
    store = ChatStore(home / "lab" / "chats", create=False)
    app = create_app(runtime, settings, store, hosts=["testserver"], accounts=Accounts(home))
    client = TestClient(app)
    client.headers["X-Kelvin-Token"] = settings.access_key
    return client, settings


def test_the_client_tells_down_from_no(hub):
    h, line = hub
    c = central.Central(HUB)
    with pytest.raises(central.CentralError) as no:
        c.sign_in("nobody@example.org", "whatever pw", "urn:uuid:x")
    assert no.value.status == 401
    line.up = False
    with pytest.raises(central.Unreachable):
        c.sign_in("nobody@example.org", "whatever pw", "urn:uuid:x")
    with pytest.raises(central.Unreachable):
        central.Central("http://hub.example.org")      # plain http only on this computer
    with pytest.raises(central.Unreachable):           # a tunnel whose hub is down answers with a page of its own
        central.Central(HUB, http=lambda *a: (530, "<html>tunnel down</html>")).reset("a@b.c")


@pytest.mark.skipif(not CRYPTO, reason="needs the cryptography package")
def test_an_account_from_before_joins_a_central_one_and_keeps_its_keys(tmp_path, hub):
    h, line = hub
    acc = Accounts(tmp_path)
    acc.create("Ada", "pw1234")
    client, settings = computer(tmp_path)
    assert client.post("/api/session", json={"name": "Ada", "password": "pw1234"}).status_code == 200
    settings.update({"anthropic_api_key": "sk-ada"})
    client.delete("/api/session")

    assert not client.get("/api/session").json()["central"]
    s = client.post("/api/session/hub", json={"url": HUB}).json()       # nobody here has a central account yet
    assert s["central"] and s["hub"] == HUB and s["unlinked"] == ["Ada"]
    r = client.post("/api/accounts", json={"name": "Ada Lovelace", "consent": True, **ADA})
    assert r.status_code == 200 and r.json()["link"] == ["Ada"]           # made on the hub; which one here is hers?
    assert h.authenticate(ADA["email"], ADA["password"])["name"] == "Ada Lovelace"
    r = client.post("/api/session", json={"link": "Ada", "link_password": "wrong", **ADA})
    assert r.status_code == 401
    r = client.post("/api/session", json={"link": "Ada", "link_password": "pw1234", **ADA})
    assert r.status_code == 200, r.text
    me = r.json()["user"]
    assert me == {"name": "Ada Lovelace", "password": True, "email": ADA["email"], "central": True}
    assert settings.get("anthropic_api_key") == "sk-ada"                  # sealed again with the central password
    assert (settings.get("account_token") or "").startswith("khub_")
    mine = (tmp_path / "users" / "Ada" / "settings.json").read_text()
    assert "sk-ada" not in mine and "khub_" not in mine
    assert client.post("/api/session/hub", json={"url": ""}).status_code == 200   # she may change it, signed in
    client.post("/api/session/hub", json={"url": HUB})
    assert client.request("DELETE", "/api/account", json={}).status_code == 401      # her password, to take her off

    client.delete("/api/session")
    assert client.post("/api/session/hub", json={"url": ""}).status_code == 401   # not by just anyone now
    line.up = False                                                       # offline: this computer knows her password
    r = client.post("/api/session", json=ADA)
    assert r.status_code == 200 and r.json()["offline"] and settings.get("anthropic_api_key") == "sk-ada"
    client.delete("/api/session")
    assert client.post("/api/session", json=dict(ADA, password="wrong pw!")).status_code == 401
    r = client.post("/api/session", json={"email": "ben@example.org", "password": "anything"})
    assert r.status_code == 503 and "first sign-in" in r.json()["error"]
    line.up = True
    assert client.post("/api/session", json=dict(ADA, password="wrong pw!")).status_code == 401


@pytest.mark.skipif(not CRYPTO, reason="needs the cryptography package")
def test_one_password_on_every_computer(tmp_path, hub):
    h, line = hub
    h.register(ADA["email"], "Ada", ADA["password"], True)
    lab1, s1 = computer(tmp_path / "one")
    lab1.post("/api/session/hub", json={"url": HUB})
    r = lab1.post("/api/session", json=ADA)                               # nobody here before: straight in
    assert r.status_code == 200 and r.json()["user"]["name"] == "Ada" and not r.json()["keys_sealed"]
    s1.update({"openai_api_key": "sk-one"})
    lab2, s2 = computer(tmp_path / "two")
    lab2.post("/api/session/hub", json={"url": HUB})
    assert lab2.post("/api/session", json=ADA).status_code == 200          # the same account on another computer
    assert s2.get("openai_api_key") is None                                # keys stay on each computer

    r = lab2.post("/api/account", json={"old": "wrong", "new": "battery staple"})
    assert r.status_code == 401
    r = lab2.post("/api/account", json={"old": ADA["password"], "new": "battery staple"})
    assert r.status_code == 200, r.text
    assert h.authenticate(ADA["email"], "battery staple")                 # changed on the hub
    lab2.delete("/api/session")
    assert lab2.post("/api/session", json=dict(ADA, password="battery staple")).status_code == 200

    lab1.delete("/api/session")
    assert lab1.post("/api/session", json=ADA).status_code == 401          # the old one no longer works online
    r = lab1.post("/api/session", json=dict(ADA, password="battery staple"))
    assert r.status_code == 200 and r.json()["keys_sealed"]                # sealed with the old password
    assert s1.get("openai_api_key") is None
    assert lab1.post("/api/account/rekey", json={"old": "battery staple"}).status_code == 400
    r = lab1.post("/api/account/rekey", json={"old": ADA["password"]})
    assert r.status_code == 200 and r.json()["rekeyed"]
    assert s1.get("openai_api_key") == "sk-one"
    lab1.delete("/api/session")
    r = lab1.post("/api/session", json=dict(ADA, password="battery staple"))
    assert not r.json()["keys_sealed"] and s1.get("openai_api_key") == "sk-one"

    assert lab1.post("/api/session/reset", json={"email": ADA["email"]}).json() == {"ok": True}
    mail = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "hub" / "outbox").glob("*.txt"))
    assert "/reset/new?code=" in mail


def test_someone_new_on_a_computer_with_old_accounts_can_start_fresh(tmp_path, hub):
    h, line = hub
    Accounts(tmp_path).create("Ada", "")
    client, settings = computer(tmp_path)
    client.post("/api/session/hub", json={"url": HUB})
    ben = {"email": "ben@example.org", "password": "ben's password"}
    r = client.post("/api/accounts", json={"name": "Ben", "consent": False, **ben})
    assert r.status_code == 400                                            # the hub's terms first
    r = client.post("/api/accounts", json={"name": "Ben", "consent": True, **ben})
    assert r.json()["link"] == ["Ada"]
    r = client.post("/api/session", json={"link": "Ben", **ben})
    assert r.status_code == 400                                            # no such account here
    r = client.post("/api/session", json={"link": "", **ben})              # none of these: start fresh
    assert r.status_code == 200 and r.json()["user"]["name"] == "Ben"
    accounts = json.loads((tmp_path / "accounts.json").read_text())["accounts"]
    assert [(a["name"], a.get("email")) for a in accounts] == [("Ada", None), ("Ben", "ben@example.org")]
    client.delete("/api/session")
    assert client.post("/api/session", json=ben).json()["user"]["name"] == "Ben"   # no question the second time
    client.delete("/api/session")
    cy = {"email": "cy@example.org", "password": "cy's password"}
    h.register(cy["email"], "Cy", cy["password"], True)
    r = client.post("/api/session", json={"link": "Ben", "link_password": ben["password"], **cy})
    assert r.status_code == 400 and "already linked" in r.json()["error"]   # Ben's is Ben's


@pytest.mark.skipif(not CRYPTO, reason="needs the cryptography package")
def test_someone_is_removed_before_signing_in_and_their_measurements_stay(tmp_path, hub):
    h, line = hub
    client, settings = computer(tmp_path)
    client.post("/api/session/hub", json={"url": HUB})
    assert client.post("/api/accounts", json={"name": "Ada Lovelace", "consent": True, **ADA}).status_code == 200
    folder = tmp_path / "users" / "Ada_Lovelace"
    (folder / "data" / "runs" / "20261024-101500-sweep").mkdir(parents=True)          # a measurement of hers
    client.delete("/api/session")
    assert [a["name"] for a in client.get("/api/session").json()["accounts"]] == ["Ada Lovelace"]
    r = client.post("/api/accounts/remove", json={"name": "Ada Lovelace", "password": "wrong pw!"})
    assert r.status_code == 401 and (folder / "settings.json").exists()
    r = client.post("/api/accounts/remove", json={"name": "Ada Lovelace", "password": ADA["password"]})
    assert r.status_code == 200, r.text
    assert r.json()["accounts"] == [] and r.json()["removed"] == "Ada Lovelace" and not r.json()["everywhere"]
    assert sorted(p.name for p in folder.iterdir()) == ["data"]                      # settings, keys and chats gone
    assert any(k.endswith("runs") for k in r.json()["kept"])
    assert h.authenticate(ADA["email"], ADA["password"])                            # her Kelvin account stays
    r = client.post("/api/session", json=ADA)                                       # and signs her in here afresh,
    assert r.status_code == 200 and r.json()["user"]["name"] == "Ada Lovelace"
    mine = json.loads((tmp_path / "accounts.json").read_text())["accounts"][0]
    assert mine["folder"] == "Ada_Lovelace_2"                                        # in a folder of her own again
    assert client.post("/api/accounts/remove", json={"name": "Ada Lovelace"}).status_code == 409   # not while signed in


@pytest.mark.skipif(not CRYPTO, reason="needs the cryptography package")
def test_a_kelvin_account_is_deleted_everywhere_from_its_settings(tmp_path, hub):
    h, line = hub
    client, settings = computer(tmp_path)
    client.post("/api/session/hub", json={"url": HUB})
    client.post("/api/accounts", json={"name": "Ada Lovelace", "consent": True, **ADA})
    r = client.request("DELETE", "/api/account", json={"password": "wrong pw!", "everywhere": True})
    assert r.status_code == 401 and h.find_users(ADA["email"])                       # nothing deleted
    line.up = False
    r = client.request("DELETE", "/api/account", json={"password": ADA["password"], "everywhere": True})
    assert r.status_code == 503 and "this computer only" in r.json()["error"]        # offline: say what still works
    line.up = True
    r = client.request("DELETE", "/api/account", json={"password": ADA["password"], "everywhere": True})
    assert r.status_code == 200, r.text
    assert r.json()["user"] is None and r.json()["accounts"] == [] and r.json()["everywhere"]
    assert not h.find_users(ADA["email"])                                           # gone on the hub
    assert client.post("/api/session", json=ADA).status_code == 401                 # so on every computer


def test_an_older_hub_says_where_to_delete_the_account(hub):
    h, line = hub

    def older(method, url, body=None, headers=None):   # a hub from before Delete account in the apps
        return (404, {"error": "not found"}) if url.endswith("/api/account/delete") else line(method, url, body, headers)
    with pytest.raises(central.CentralError) as no:
        central.Central(HUB, http=older).delete_account(ADA["email"], ADA["password"])
    assert no.value.status == 404 and HUB + "/account" in str(no.value)


def test_kelvin_says_why_it_cant_reach_the_hub():
    import socket
    import ssl
    import urllib.error
    cert = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    cert.verify_message = "unable to get local issuer certificate"
    cases = [(cert, "doesn't trust the hub's certificate (unable to get local issuer certificate)"),
             (socket.gaierror(11001, "getaddrinfo failed"), "can't look up the hub's address"),
             (TimeoutError("timed out"), "didn't answer in time"),
             (ConnectionRefusedError(10061, "refused"), "refused or cut off")]
    for reason, said in cases:
        def http(*a, reason=reason, **k):
            raise urllib.error.URLError(reason)
        with pytest.raises(central.Unreachable) as e:
            central.Central(HUB, http=http).sign_in("a@b.c", "pw", "x")
        assert said in str(e.value) and "URLError" not in str(e.value), str(e.value)


def test_kelvin_trusts_its_own_list_of_certificates_too(monkeypatch):
    pytest.importorskip("certifi")
    import ssl
    from labharness.hub import client
    # a Windows that never fetched Let's Encrypt's root: its browser has its own list, and now Kelvin too
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    names = {dict(x[0] for x in c["subject"]).get("commonName") for c in client.ssl_context().get_ca_certs()}
    assert "ISRG Root X1" in names


def test_removing_the_first_account_never_hands_its_data_from_before_to_someone_new(tmp_path):
    old = tmp_path / "virtual-lab"
    (old / "runs" / "20261017-225927-gate_sweep").mkdir(parents=True)              # runs from before accounts
    client, settings = computer(tmp_path)
    assert client.post("/api/accounts", json={"name": "Ada", "password": "pw1234"}).status_code == 200
    assert json.loads((tmp_path / "accounts.json").read_text())["accounts"][0]["data"] == "virtual-lab"
    client.delete("/api/session")
    assert client.post("/api/accounts/remove", json={"name": "Ada", "password": "pw1234"}).status_code == 200
    assert (old / "runs" / "20261017-225927-gate_sweep").is_dir()                  # her measurements stay
    s = client.get("/api/session").json()
    assert s["accounts"] == [] and not s["legacy"]                                  # nobody new is told they come along
    assert client.post("/api/accounts", json={"name": "Ben", "password": ""}).status_code == 200
    ben = json.loads((tmp_path / "accounts.json").read_text())["accounts"][0]
    assert ben["name"] == "Ben" and "data" not in ben                               # his own folder, not hers
    client.delete("/api/session")
    assert client.post("/api/accounts/remove", json={"name": "Ben"}).status_code == 200   # no password: none asked
