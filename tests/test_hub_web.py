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
    assert r.status_code == 400 and "code=" not in r.headers.get("location", "")   # no password, no code
    r = client.post("/connect", data=dict(form, csrf=csrf(page), lab=str(lab["id"]), action="allow",
                                          password="correct horse"), follow_redirects=False)
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


def connect_form(client, inst="urn:uuid:5f0c1c55-0000-4000-8000-0000000000aa", person=""):
    kelvin = HubClient("http://127.0.0.1:8780", http=through(client))
    url, pending = kelvin.begin(CALLBACK, inst, "Shared PC", "win32", "0.3.8")
    path = url[len("http://127.0.0.1:8780"):] + ("&person=" + person if person else "")
    page = client.get(path)
    return kelvin, pending, page, {k: v[0] for k, v in parse_qs(urlsplit(path).query).items()}


def test_connecting_needs_the_password_of_the_account_signed_in(tmp_path):
    """Review P5: on a shared PC the browser may still be signed in as someone else."""
    hub, client = make(tmp_path)
    register(client, "ben@example.org", "Ben", "bens password")    # Ben used this browser last
    kelvin, pending, page, form = connect_form(client, person="Ada")
    assert "Ben" in page.text and "ben@example.org" in page.text and "used by <b>Ada</b>" in page.text
    assert "Not Ben? Sign in as yourself" in page.text
    r = client.post("/connect", data=dict(form, csrf=csrf(page), action="allow", password="adas password"),
                    follow_redirects=False)
    assert r.status_code == 400 and "the password of ben@example.org" in r.text   # Ada can't take Ben's
    r = client.post("/signout", data={"csrf": csrf(page), "next": "/connect?" + urlsplit(str(page.url)).query},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/connect?")              # "Not Ben?" goes back to the request
    r = client.get(r.headers["location"], follow_redirects=False)
    assert r.headers["location"].startswith("/signin?next=%2Fconnect%3F")
    assert hub.connections(1) == []


def test_bodies_are_capped_before_they_are_read(tmp_path, monkeypatch):
    """Review S1."""
    from labharness.hub import server
    hub, client = make(tmp_path)
    big = b"email=a%40b.c&password=" + b"x" * (70 * 1024)
    assert client.post("/signin", content=big, headers={"Content-Type": "application/x-www-form-urlencoded"}
                       ).status_code == 413
    assert client.post("/api/token", content=b"{" + b" " * (20 * 1024) + b"}").status_code == 413

    def chunks():   # no Content-Length: refused while it streams in
        for _ in range(100):
            yield b"x" * 1024
    assert client.post("/register", content=chunks(),
                       headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 413
    register(client)
    kelvin, pending, page, form = connect_form(client)
    r = client.post("/connect", data=dict(form, csrf=csrf(page), action="allow", password="correct horse"),
                    follow_redirects=False)
    kelvin.finish({k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}, pending)
    monkeypatch.setattr(server, "MAX_SYNC", 1000)
    r = client.post("/api/sync", content=b'{"chats": []' + b" " * 2000 + b"}",
                    headers={"Authorization": "Bearer " + kelvin.token})
    assert r.status_code == 413
    r = client.put("/api/sync/runs/run1/files/a.csv", content=chunks(), headers={"Authorization": "Bearer " + kelvin.token})
    assert r.status_code == 411                                         # uploads need a Content-Length


def test_odd_times_never_break_the_account_page(tmp_path):
    """Review S2: even times already stored before the check came in."""
    import sqlite3
    hub, client = make(tmp_path)
    register(client)
    lab_page = client.post("/labs", data={"name": "L", "csrf": csrf(client.get("/account"))})
    assert lab_page.status_code == 200
    kelvin, pending, page, form = connect_form(client)
    r = client.post("/connect", data=dict(form, csrf=csrf(page), action="allow", password="correct horse",
                                          lab=str(hub.labs_of(1)[0]["id"])), follow_redirects=False)
    kelvin.finish({k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}, pending)
    kelvin.push(chats=[{"id": "aaaaaaaaaaaa", "title": "t", "messages": [], "updated": 1e300}],
                runs=[{"id": "run1", "created": -1}])
    with sqlite3.connect(str(tmp_path / "hub" / "hub.sqlite3")) as con:
        con.execute("UPDATE chats SET updated_at = 1e300")
        con.execute("UPDATE runs SET created_at = -1e12")
    assert client.get("/account").status_code == 200


def test_exports_are_rate_limited(tmp_path):
    """Review S3: each export can be gigabytes."""
    hub, client = make(tmp_path)
    register(client)
    assert client.get("/account/export").status_code == 200
    r = client.get("/account/export")
    assert r.status_code == 429 and "once every 10 minutes" in r.text


def test_a_new_operator_key_ends_the_old_ones_sessions(tmp_path):
    """Review S5: `kelvin-hub operator-key` runs in another process while the hub serves."""
    hub, client = make(tmp_path)
    old_key = hub.ensure_operator_key().read_text(encoding="utf-8").split()[-1]
    page = client.post("/admin/signin", data={"key": old_key})
    assert "Recent activity" in page.text
    new_key = Hub(tmp_path / "hub").new_operator_key().read_text(encoding="utf-8").split()[-1]   # another process
    assert "Operator key" in client.get("/admin").text                # the old session is over
    r = client.post("/admin/signin", data={"key": old_key})
    assert r.status_code == 400 and "Recent activity" not in r.text
    assert client.get("/api/admin/stats", headers={"X-Operator-Key": old_key}).status_code == 401
    assert "Recent activity" in client.post("/admin/signin", data={"key": new_key}).text


def test_an_account_can_be_deleted_without_agreeing_to_new_terms(tmp_path):
    hub, client = make(tmp_path)
    register(client)
    hub.config["consent_version"] = "2027-01-01"
    assert client.get("/account", follow_redirects=False).headers["location"].startswith("/consent")
    assert "delete your account" in client.get("/consent").text
    page = client.get("/account/delete")
    assert page.status_code == 200
    r = client.post("/account/delete", data={"csrf": csrf(page), "password": "correct horse", "sure": "yes"})
    assert "deleted" in r.text and hub.stats()["people"] == 0


def test_reset_answers_the_same_whether_or_not_the_account_exists(tmp_path):
    hub, client = make(tmp_path)
    register(client)
    a = client.post("/reset", data={"email": "ada@example.org"})
    b = client.post("/reset", data={"email": "nobody@example.org"})
    assert a.status_code == b.status_code == 200 and a.text == b.text
    mails = [m.read_text(encoding="utf-8") for m in (tmp_path / "hub" / "outbox").glob("*.txt")]
    assert sum("/reset/new?code=" in m for m in mails) == 1            # sent after the answer, to Ada only


def test_one_persons_flood_never_holds_up_anyone_else(tmp_path, monkeypatch):
    """Review H1: while one person's turn is held (a long sync), their extra requests are turned away at once, and
    everyone else's requests still get a worker thread."""
    import threading
    import time
    from labharness.hub import db as hubdb
    from labharness.hub.db import challenge_of
    monkeypatch.setattr(hubdb, "LOCK_WAIT", 4.0)   # longer than the other person may wait
    hub = Hub(tmp_path / "hub")
    with TestClient(create_app(hub, hosts=["testserver"], backups=False)) as client:   # one event loop, one pool
        register(client)                                                # Ada, in the browser
        m = hub.register("m@example.org", "M", "correct horse", consent=True)
        inst = {"id": "urn:uuid:5f0c1c55-0000-4000-8000-0000000000ee", "name": "PC", "platform": "", "version": ""}
        code = hub.connect_code(m, CALLBACK, challenge_of("v" * 50), inst, None)
        bearer = {"Authorization": "Bearer " + hub.connect_app(code, "v" * 50, CALLBACK)["access_token"]}
        assert client.post("/api/sync", json={"runs": [{"id": "run1"}]}, headers=bearer).status_code == 200
        lock = hub._lock_for(m["id"])
        lock.acquire()                                                  # as during one of M's long syncs
        statuses = []

        def put(i):
            statuses.append(client.put("/api/sync/runs/run1/files/f%d.bin" % i, content=b"x",
                                       headers=bearer).status_code)

        flood = [threading.Thread(target=put, args=(i,)) for i in range(45)]
        for t in flood:
            t.start()
        time.sleep(0.3)
        got = {}
        other = threading.Thread(target=lambda: got.update(page=client.get("/account")))
        started = time.time()
        other.start()
        other.join(8)
        try:
            assert "page" in got and got["page"].status_code == 200 and time.time() - started < 2
        finally:
            lock.release()
            for t in flood:
                t.join(10)
        assert len(statuses) == 45 and statuses.count(429) >= 42 and 500 not in statuses


def test_connect_passes_on_earlier_installation_ids(tmp_path):
    """Review A1: /connect takes `previous` (checked) and keeps it through the form."""
    hub, client = make(tmp_path)
    register(client)
    kelvin = HubClient("http://127.0.0.1:8780", http=through(client))
    url, pending = kelvin.begin(CALLBACK, "urn:uuid:5f0c1c55-0000-4000-8000-0000000000ff:0123456789ab")
    path = url[len("http://127.0.0.1:8780"):]
    assert client.get(path + "&previous=" + "bad%20id!").status_code == 400
    page = client.get(path + "&previous=urn:uuid:5f0c1c55-0000-4000-8000-000000000011")
    assert page.status_code == 200 and 'name="previous" value="urn:uuid:5f0c1c55-0000-4000-8000-000000000011"' in page.text


def test_limits_count_an_ipv4_mapped_address_as_ipv4():
    """Review H5."""
    from labharness.hub.server import net_of
    assert net_of("::ffff:192.0.2.7") == "192.0.2.7"
    assert net_of("::ffff:192.0.2.8") != net_of("::ffff:192.0.2.7")
    assert net_of("2001:db8::1") == net_of("2001:db8::2") == "2001:db8::/64"
    assert net_of("198.51.100.4") == "198.51.100.4" and net_of("?") == "?"
