"""The Kelvin hub's web side: registering, signing in, connecting a Kelvin through the browser, the sync API, and the
operator's page."""
import re
import zipfile
from io import BytesIO
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402

from labharness.hub.client import HubClient  # noqa: E402
from labharness.hub.db import Hub  # noqa: E402
from labharness.hub.server import create_app  # noqa: E402

CALLBACK = "http://127.0.0.1:8770/hub/callback"


def make(tmp_path):
    hub = Hub(tmp_path / "hub")
    return hub, TestClient(create_app(hub, hosts=["testserver"], backups=False))


def csrf(page):
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def register(client, email="ada@example.org", name="Ada", password="correct horse"):
    r = client.post("/register", data={"name": name, "email": email, "password": password, "consent": "yes"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


def through(client):
    """HubClient's http, answered by the test app."""
    def http(method, url, body=None, headers=None):
        parts = urlsplit(url)
        r = client.request(method, parts.path + ("?" + parts.query if parts.query else ""), content=body,
                           headers=headers or {})
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text
    return http


def test_register_sign_in_and_out(tmp_path):
    hub, client = make(tmp_path)
    r = client.post("/register", data={"name": "Ada", "email": "ada@example.org", "password": "correct horse"})
    assert r.status_code == 400 and "agree to the terms" in r.text            # consent is required
    r = register(client)
    assert r.headers["location"] == "/account?note=confirm"
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    page = client.get("/account")
    assert page.status_code == 200 and "Please confirm" in page.text and "Welcome!" not in page.text
    assert "Content-Security-Policy" in page.headers and page.headers["X-Frame-Options"] == "DENY"
    mail = next((tmp_path / "hub" / "outbox").glob("*.txt")).read_text(encoding="utf-8")
    code = re.search(r"/confirm\?code=([\w-]+)", mail).group(1)
    assert "Email confirmed" in client.get("/confirm", params={"code": code}).text
    assert "Please confirm" not in client.get("/account").text
    client.post("/signout", data={"csrf": csrf(page)})
    r = client.get("/account", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/signin")
    r = client.post("/signin", data={"email": "ada@example.org", "password": "nope nope"})
    assert r.status_code == 400 and "Wrong email or password" in r.text
    r = client.post("/signin", data={"email": "ada@example.org", "password": "correct horse", "next": "//evil.example"},
                    follow_redirects=False)
    assert r.headers["location"] == "/account"                                # never off to another site


def test_forms_need_the_anti_forgery_value_and_this_site(tmp_path):
    hub, client = make(tmp_path)
    register(client)
    r = client.post("/labs", data={"name": "Smith Lab"})
    assert r.status_code == 400 and "expired" in r.text and hub.labs_of(1) == []
    r = client.post("/labs", data={"name": "Smith Lab", "csrf": csrf(client.get("/account"))},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and hub.labs_of(1) == []
    r = client.post("/labs", data={"name": "Smith Lab", "csrf": csrf(client.get("/account"))}, headers={"Origin": "null"})
    assert r.status_code == 403 and hub.labs_of(1) == []
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403   # DNS rebinding
    # browsers send "Origin: null" on a site's own forms under no-referrer, so the policy must leave Origin intact
    assert client.get("/").headers["Referrer-Policy"] == "same-origin"
    r = client.post("/labs", data={"name": "Own Lab", "csrf": csrf(client.get("/account"))},
                    headers={"Origin": "http://testserver"})
    assert "Lab made" in r.text
    assert [lab["name"] for lab in hub.labs_of(1)] == ["Own Lab"]


def test_connecting_a_kelvin_through_the_browser_then_syncing(tmp_path):
    hub, client = make(tmp_path)
    register(client)
    client.post("/labs", data={"name": "Smith Lab", "csrf": csrf(client.get("/account"))})
    lab = hub.labs_of(1)[0]

    kelvin = HubClient("http://127.0.0.1:8780", http=through(client))
    url, pending = kelvin.begin(CALLBACK, "urn:uuid:5f0c1c55-0000-4000-8000-000000000001", "Lab PC", "win32", "0.3.8")
    path = url[len("http://127.0.0.1:8780"):]
    page = client.get(path)
    assert page.status_code == 200 and "Lab PC" in page.text and "Never:" in page.text
    form = {k: v[0] for k, v in parse_qs(urlsplit(path).query).items()}
    r = client.post("/connect", data=dict(form, csrf=csrf(page), lab=str(lab["id"]), action="allow"),
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(CALLBACK + "?")
    query = {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}
    assert query["state"] == pending["state"] and query["code"]
    token = kelvin.finish(query, pending)
    assert token and kelvin.me()["shares_into"] == "Smith Lab"
    assert "Lab PC" in client.get("/account").text

    done = kelvin.push(projects=[{"id": "p00000001", "name": "CrSBr"}],
                       chats=[{"id": "aaaaaaaaaaaa", "project": "p00000001", "title": "Gate sweep",
                               "messages": [{"role": "user", "text": "sweep the gate"},
                                            {"role": "assistant", "model": "m", "blocks": [
                                                {"type": "text", "text": "<b>done</b>"}]}]},
                              {"id": "bbbbbbbbbbbb", "title": "Private", "incognito": True, "messages": []}],
                       runs=[{"id": "run-1", "project": "p00000001", "device": "D1"}])
    assert (done["chats"], done["runs"], len(done["refused"])) == (1, 1, 1)
    assert kelvin.upload("run-1", "data.csv", b"V,I\n0,1\n")["bytes"] == 8
    r = client.put("/api/sync/runs/run-1/files/..%2F..%2Fescape.txt", content=b"x",
                   headers={"Authorization": "Bearer " + token})
    assert r.status_code in (400, 404) and not list(tmp_path.rglob("escape.txt"))
    hub.config["max_file_mb"] = 0.000001
    r = client.put("/api/sync/runs/run-1/files/big.bin", content=b"x" * 100, headers={"Authorization": "Bearer " + token})
    assert r.status_code == 413 and not list((tmp_path / "hub" / "tmp").iterdir())

    account = client.get("/account").text
    chat_id = re.search(r'/shared/chats/(\d+)', account).group(1)
    shown = client.get("/shared/chats/%s" % chat_id).text
    assert "sweep the gate" in shown and "&lt;b&gt;done&lt;/b&gt;" in shown           # escaped, never raw HTML
    run_id = re.search(r'/shared/runs/(\d+)', account).group(1)
    f = client.get("/shared/runs/%s/files/data.csv" % run_id)
    assert f.content == b"V,I\n0,1\n" and "attachment" in f.headers["content-disposition"]

    assert client.post("/api/sync", json={}).status_code == 401                 # no token, no sync
    kelvin.disconnect()
    assert client.get("/api/me", headers={"Authorization": "Bearer " + token}).status_code == 401


def test_cancel_and_bad_requests_never_hand_out_a_code(tmp_path):
    hub, client = make(tmp_path)
    register(client)
    kelvin = HubClient("http://127.0.0.1:8780", http=through(client))
    url, pending = kelvin.begin(CALLBACK, "urn:uuid:5f0c1c55-0000-4000-8000-000000000002")
    path = url[len("http://127.0.0.1:8780"):]
    page = client.get(path)
    form = {k: v[0] for k, v in parse_qs(urlsplit(path).query).items()}
    r = client.post("/connect", data=dict(form, csrf=csrf(page), action="cancel"), follow_redirects=False)
    assert "error=access_denied" in r.headers["location"] and "code=" not in r.headers["location"]
    bad = path.replace("127.0.0.1%3A8770", "evil.example%3A8770")
    r = client.get(bad, follow_redirects=False)
    assert r.status_code == 400 and "127.0.0.1" in r.text                     # no redirect to a foreign site
    client.cookies.clear()
    r = client.get(path, follow_redirects=False)                             # signed out: sign in, then back here
    assert r.status_code == 303 and r.headers["location"].startswith("/signin?next=%2Fconnect%3F")


def test_terms_change_asks_for_consent_again(tmp_path):
    hub, client = make(tmp_path)
    register(client)
    hub.config["consent_version"] = "2027-01-01"
    r = client.get("/account", follow_redirects=False)
    assert r.headers["location"].startswith("/consent")
    page = client.get(r.headers["location"])
    r = client.post("/consent", data={"csrf": csrf(page), "consent": "yes", "next": "/account"}, follow_redirects=False)
    assert r.headers["location"] == "/account" and hub.user(1)["consent_version"] == "2027-01-01"


def test_export_and_delete_from_the_account_page(tmp_path):
    hub, client = make(tmp_path)
    register(client)
    r = client.get("/account/export")
    with zipfile.ZipFile(BytesIO(r.content)) as z:
        assert "account.json" in z.namelist() and b"password_hash" not in z.read("account.json")
    assert not list((tmp_path / "hub" / "tmp").iterdir())                     # the export file is removed after
    token = csrf(client.get("/account"))
    r = client.post("/account/delete", data={"csrf": token, "password": "wrong password", "sure": "yes"})
    assert r.status_code == 400 and hub.stats()["people"] == 1
    r = client.post("/account/delete", data={"csrf": token, "password": "correct horse", "sure": "yes"})
    assert "deleted" in r.text and hub.stats()["people"] == 0


def test_operator_page_needs_the_operator_key(tmp_path):
    hub, client = make(tmp_path)
    key = hub.ensure_operator_key().read_text(encoding="utf-8").split()[-1]
    register(client)
    client.cookies.clear()
    assert "Operator key" in client.get("/admin").text
    r = client.post("/admin/signin", data={"key": "khop_wrong"})
    assert r.status_code == 400 and "ada@example.org" not in r.text
    page = client.post("/admin/signin", data={"key": key})
    assert "ada@example.org" in page.text and "people" in page.text
    r = client.post("/admin/users/1/disable", data={"csrf": csrf(page)})
    assert "is disabled" in r.text and hub.user(1)["disabled_at"]
    r = client.post("/signin", data={"email": "ada@example.org", "password": "correct horse"})
    assert "disabled" in r.text
    assert client.get("/api/admin/stats").status_code == 401
    assert client.get("/api/admin/stats", headers={"X-Operator-Key": key}).json()["people"] == 1
