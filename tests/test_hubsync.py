"""The Kelvin hub in the portal: connecting from Settings through the browser, the token kept secret, and the
background sync of what a person shares (never what is unpublished, confidential, model-limited or incognito)."""
import json
import re
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")
pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402

import labharness.web.app as web_app  # noqa: E402
from labharness import build_virtual_lab, demo_envelope  # noqa: E402
from labharness.accounts import Accounts  # noqa: E402
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.hub.client import collect  # noqa: E402
from labharness.hub.db import Hub  # noqa: E402
from labharness.hub.server import create_app as hub_app  # noqa: E402
from labharness.web import hubsync  # noqa: E402
from labharness.web.hubsync import installation_for, previous_installations  # noqa: E402
from labharness.web.providers.base import ModelInfo, Provider, TurnResult  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402

try:
    import cryptography  # noqa: F401
    CRYPTO = True
except ImportError:
    CRYPTO = False

HUB = "http://127.0.0.1:8780"


@pytest.fixture(autouse=True)
def sharing_on(monkeypatch):
    """Sharing work with the hub is switched off in Kelvin for now (the hub is for accounts); these tests keep it
    working for when it comes back."""
    monkeypatch.setattr(web_app, "HUB_SHARING", True)

SWEEP = """plan: gate_sweep
setup: { lockin.amplitude: 0.05, smu.output: on }
sweep: { smu.V: {from: 0, to: 2, step: 0.5} }
settle: 0.1
measure: [lockin.X, smu.I]
after: { smu.V: 0 }"""


def csrf(page):
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def through(client, calls=None):
    """Kelvin's requests to the hub, answered by the hub's test app."""
    def http(method, url, body=None, headers=None):
        parts = urlsplit(url)
        if calls is not None:
            calls.append((method, parts.path))
        r = client.request(method, parts.path + ("?" + parts.query if parts.query else ""), content=body,
                           headers=headers or {})
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text
    return http


def the_hub(tmp_path, email="ada@example.org", lab="Smith Lab"):
    """The hub, and the person's browser on it: registered, signed in, in a lab."""
    hub = Hub(tmp_path / "hub")
    hub.config["sharing"] = True
    browser = TestClient(hub_app(hub, hosts=["testserver"], backups=False))
    r = browser.post("/register", data={"name": email.split("@")[0].title(), "email": email,
                                        "password": "correct horse", "consent": "yes"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    if lab:
        browser.post("/labs", data={"name": lab, "csrf": csrf(browser.get("/account"))})
    return hub, browser


def portal(tmp_path, monkeypatch, browser, accounts=None, calls=None, providers=None):
    opened = []
    monkeypatch.setattr(web_app, "open_url", opened.append)
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    settings = Settings(tmp_path / "settings.json")
    store = ChatStore(tmp_path / "lab" / "chats" if accounts else tmp_path / "chats", create=not accounts)
    guard = web_app.create_app(runtime, settings, store, hosts=["testserver"], demo=True, accounts=accounts,
                               providers=providers)
    guard.app.state.hub.http = through(browser, calls)
    return SimpleNamespace(client=TestClient(guard), h={"X-Kelvin-Token": settings.access_key}, runtime=runtime,
                           settings=settings, store=store, opened=opened, link=guard.app.state.hub)


def connect(k, browser, lab="Smith Lab", action="allow", password="correct horse"):
    """Settings → Account → Kelvin hub → Connect, then the person's yes (with their hub password) on the hub's
    page in their browser."""
    url = k.client.post("/api/hub/connect", headers=k.h, json={"url": HUB}).json()["url"]
    assert k.opened[-1] == url and url.startswith(HUB + "/connect?")
    path = url[len(HUB):]
    page = browser.get(path)
    assert page.status_code == 200 and "wants to connect" in page.text
    form = {key: v[0] for key, v in parse_qs(urlsplit(path).query).items()}
    labs = dict(re.findall(r'<option value="(\d+)">([^<]+)</option>', page.text))
    chosen = next((i for i, name in labs.items() if name == lab), "")
    r = browser.post("/connect", data=dict(form, csrf=csrf(page), lab=chosen, action=action, password=password),
                     follow_redirects=False)
    back = urlsplit(r.headers["location"])
    assert back.netloc.startswith("127.0.0.1:") and back.path == "/hub/callback"   # back to Kelvin, nowhere else
    return k.client.get("/hub/callback?" + back.query)


def chat(store, title, project=None, incognito=False):
    c = store.create("demo:x", project=project, incognito=incognito)
    c["title"] = title
    c["messages"] = [{"role": "user", "text": title + ", please"},
                     {"role": "assistant", "model": "demo:x", "blocks": [{"type": "text", "text": "done"}]}]
    store.save(c)
    return c


def run(runtime, name, project, user=None, old=False, **meta):
    """A finished run in the data folder, made by user (None: without accounts), or by an older Kelvin (old)."""
    d = runtime.data_dir / "runs" / name
    d.mkdir(parents=True)
    meta = dict(run_id=name, project=project, device_name="D1", status="done", started="2026-10-20T10:00:00.000",
                **meta)
    if not old:
        meta["user"] = user
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / "data.csv").write_text("V,I\n0,1\n", encoding="utf-8")
    (d / "plan.yaml").write_text("plan: %s\n" % name, encoding="utf-8")
    return d


def on_the_hub(hub):
    """Everything the hub keeps of what Kelvins sent, as text."""
    with hub.connect() as con:
        rows = {t: [dict(r) for r in con.execute("SELECT * FROM %s" % t)] for t in ("projects", "chats", "runs")}
    return json.dumps(rows, ensure_ascii=False)


def test_connecting_from_settings_then_sharing_in_the_background(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path)
    calls = []
    k = portal(tmp_path, monkeypatch, browser, calls=calls)
    assert k.client.get("/api/hub", headers=k.h).json()["connected"] is False
    assert k.client.post("/api/hub/connect", headers=k.h, json={"url": "http://hub.example.org"}).status_code == 400

    page = connect(k, browser)
    assert page.status_code == 200 and "connected to the hub" in page.text and "Smith Lab" in page.text
    st = k.client.get("/api/hub", headers=k.h).json()
    assert st["connected"] and st["url"] == HUB
    assert st["account"]["email"] == "ada@example.org" and st["account"]["shares_into"] == "Smith Lab"
    assert st["account"]["account_url"] == HUB + "/account"
    token = k.settings.get("hub_token")
    assert token.startswith("khub_") and k.settings.get("hub_url") == HUB
    for path in ("/api/hub", "/api/settings", "/api/bootstrap"):
        assert token not in k.client.get(path, headers=k.h).text              # the token never goes to the browser
    k.client.post("/api/settings", headers=k.h, json={"hub_token": "khub_mine", "hub_url": "https://evil.example"})
    assert k.settings.get("hub_token") == token and k.settings.get("hub_url") == HUB   # ...nor comes from it
    assert "Kelvin" in browser.get("/account").text and k.client.get("/hub/callback?state=x").status_code == 400

    store, rt = k.store, k.runtime
    crsbr = store.create_project("CrSBr")
    secret = store.create_project("Secret sample")
    assert k.client.patch("/api/projects/" + secret["id"], headers=k.h,
                          json={"sharing": "unpublished"}).json()["unpublished"] is True
    assert k.client.patch("/api/projects/" + secret["id"], headers=k.h, json={"sharing": "maybe"}).status_code == 400
    local = store.create_project("Local only")
    store.set_rules(local["id"], {"providers": ["local"], "literature": False})
    unfiled = chat(store, "Unfiled question")
    chat(store, "Gate sweep", crsbr["id"])
    hidden = chat(store, "Secret chat", secret["id"])
    chat(store, "Local chat", local["id"])
    chat(store, "Incognito chat", incognito=True)
    run(rt, "20261020-100000-iv", crsbr["id"])
    run(rt, "20261020-100100-general", None)
    run(rt, "20261020-100200-secret", secret["id"])
    run(rt, "20261020-100300-incognito", None, incognito=True)
    run(rt, "20261020-100400-gone", "p0000dead")                      # its project was deleted: can't tell
    (rt.data_dir / "runs" / "20261020-100500-running").mkdir()        # not finished yet

    st = k.client.post("/api/hub/sync", headers=k.h).json()
    s = st["sync"]
    assert s["ok"] and s["error"] is None and st["synced_at"]
    assert s["sent"] == {"projects": 1, "chats": 2, "runs": 2} and s["withheld"] == 2 and s["files"] == 6
    seen = hub.shared_with(1)
    assert sorted(c["title"] for c in seen["chats"]) == ["Gate sweep", "Unfiled question"]
    assert sorted(r["local_id"] for r in seen["runs"]) == ["20261020-100000-iv", "20261020-100100-general"]
    assert [p["name"] for p in seen["projects"]] == ["CrSBr"]
    everything = on_the_hub(hub)
    for never in ("Secret", "Local only", "Local chat", "Incognito", "incognito", "gone", "running"):
        assert never not in everything
    files = sorted(p.name for p in (tmp_path / "hub" / "files").rglob("*") if p.is_file())
    assert files == ["data.csv", "data.csv", "meta.json", "meta.json", "plan.yaml", "plan.yaml"]
    assert "Gate sweep" in browser.get("/account").text                # on the person's account page
    assert token not in (tmp_path / "hub-sync.json").read_text(encoding="utf-8")

    calls.clear()
    assert k.link.sync_once()["ok"] and calls == []                    # nothing changed: nothing sent

    store.delete(unfiled["id"])                                        # removed here, removed there
    store.assign(hidden["id"], None)                                   # out of the unpublished project: still not shared
    k.client.patch("/api/projects/" + crsbr["id"], headers=k.h, json={"sharing": "confidential"})
    (rt.data_dir / "runs" / "20261020-100100-general" / "fit.csv").write_text("a,b\n", encoding="utf-8")
    s = k.client.post("/api/hub/sync", headers=k.h).json()["sync"]
    assert s["ok"] and s["files"] == 1 and s["removed"] >= 1
    seen = hub.shared_with(1)
    assert seen["chats"] == [] and seen["projects"] == []
    assert [r["local_id"] for r in seen["runs"]] == ["20261020-100100-general"]
    assert "CrSBr" not in on_the_hub(hub) and "Secret chat" not in on_the_hub(hub)
    assert sorted(c["title"] for c in collect(store)["chats"]) == []

    r = k.client.post("/api/hub/disconnect", headers=k.h).json()
    assert r["revoked"] is True and r["connected"] is False
    assert k.settings.get("hub_token") is None and k.settings.get("hub_url") == HUB   # the address stays
    assert browser.get("/api/me", headers={"Authorization": "Bearer " + token}).status_code == 401
    assert k.client.post("/api/hub/sync", headers=k.h).status_code == 409


def test_unpublished_marks_stay_with_chats_that_leave(tmp_path):
    store = ChatStore(tmp_path / "chats")
    secret = store.create_project("Secret")
    store.set_sharing(secret["id"], "confidential")
    a = chat(store, "a", secret["id"])
    b = chat(store, "b", secret["id"])
    store.assign(a["id"], None)
    store.delete_project(secret["id"])                                 # b is left unfiled, still unshared
    assert {c["id"] for c in store.list() if c.get("confidential")} == {a["id"], b["id"]}
    assert collect(store)["chats"] == []
    other = store.create_project("Open")
    store.set_sharing(other["id"], "unpublished")
    store.set_sharing(other["id"], "")                                 # published now: shared again
    assert "unpublished" not in store.project(other["id"])
    chat(store, "c", other["id"])
    assert [c["title"] for c in collect(store)["chats"]] == ["c"]
    store.delete(a["id"])
    assert a["id"] not in json.loads((tmp_path / "chats" / "_projects.json").read_text())["marks"]


def test_runs_from_incognito_chats_say_so(tmp_path):
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    plan = SimpleNamespace(device=None)
    assert "incognito" not in runtime._attribute(plan)
    runtime.attribution = {"project": None, "chat": "abcdefabcdef", "incognito": True}
    assert runtime._attribute(plan)["incognito"] is True
    assert runtime.queue_add(SWEEP).startswith("added")
    assert runtime.queue.items[-1].incognito is True                    # its runs, later, say so too
    runtime.attribution = {}
    assert runtime._attribute(plan, None, True)["incognito"] is True
    assert "incognito" not in runtime._attribute(plan, None, False)


def test_each_person_has_their_own_hub_and_its_token_is_encrypted(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path)
    acc = Accounts(tmp_path)
    acc.create("Ada", "pw1234")
    acc.create("Ben", "")
    k = portal(tmp_path, monkeypatch, browser, accounts=acc)
    assert k.client.get("/api/hub", headers=k.h).status_code == 401      # nobody signed in yet
    k.client.post("/api/session", headers=k.h, json={"name": "Ada", "password": "pw1234"})
    assert connect(k, browser).status_code == 200
    token = k.settings.get("hub_token")
    raw = (tmp_path / "users" / "Ada" / "settings.json").read_text(encoding="utf-8")
    if CRYPTO:
        assert token not in raw and "enc:" in raw                        # sealed with Ada's password
    installation = json.loads((tmp_path / "settings.json").read_text())["hub_installation"]
    assert installation.startswith("urn:uuid:")                         # this Kelvin's, for everyone on it

    k.client.post("/api/session", headers=k.h, json={"name": "Ben"})
    assert k.client.get("/api/hub", headers=k.h).json()["connected"] is False
    chat(k.store, "Ben's chat")
    assert k.client.post("/api/hub/sync", headers=k.h).status_code == 409
    assert k.link.sync_once() is None and "Ben" not in on_the_hub(hub)

    k.client.post("/api/session", headers=k.h, json={"name": "Ada", "password": "pw1234"})
    chat(k.store, "Ada's chat")
    st = k.client.post("/api/hub/sync", headers=k.h).json()
    assert st["connected"] and st["sync"]["sent"]["chats"] == 1
    assert [c["title"] for c in hub.shared_with(1)["chats"]] == ["Ada's chat"]
    assert json.loads((tmp_path / "settings.json").read_text())["hub_installation"] == installation


def test_cancelled_ended_and_unreachable_connections(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path, lab=None)
    k = portal(tmp_path, monkeypatch, browser)
    page = connect(k, browser, action="cancel")
    assert page.status_code == 400 and "cancelled" in page.text and k.settings.get("hub_token") is None

    page = connect(k, browser, lab="")
    assert page.status_code == 200 and "only you" in page.text
    assert k.client.get("/api/hub", headers=k.h).json()["account"]["shares_into"] is None

    def down(method, url, body=None, headers=None):
        raise OSError("connection refused")
    k.link.http = down
    st = k.client.get("/api/hub", headers=k.h).json()
    assert st["connected"] and "can't reach the hub" in st["problem"]
    chat(k.store, "Waiting")
    s = k.client.post("/api/hub/sync", headers=k.h).json()["sync"]
    assert not s["ok"] and "can't reach the hub" in s["error"]
    assert k.settings.get("hub_token")                                  # kept: it tries again later

    k.link.http = through(browser)
    s = k.client.post("/api/hub/sync", headers=k.h).json()["sync"]
    assert s["ok"] and s["sent"]["chats"] == 1
    hub.revoke(1, hub.connections(1)[0]["id"])                          # disconnected on the hub's account page
    st = k.client.get("/api/hub", headers=k.h).json()
    assert st["connected"] is False and "ended this connection" in st["problem"]
    assert k.settings.get("hub_token") is None

    k.client.app.remote.port = 8771                                     # as if phone access were on
    phone = TestClient(k.client.app, base_url="http://192.168.1.50:8771")
    assert phone.get("/hub/callback", params={"state": "x"}).status_code == 403   # only on this computer


# ------------------------------------------------------------------ after review: what must never leave, and races


def test_model_limited_chats_keep_the_rules_they_leave_with(tmp_path):
    store = ChatStore(tmp_path / "chats")
    local = store.create_project("Local only")
    early = chat(store, "talked before the limits", local["id"])
    store.set_rules(local["id"], {"providers": None, "literature": False})       # off and straight back on:
    store.set_rules(local["id"], {"providers": None, "literature": True})        # no harm done, nothing locked
    assert "rules" not in store.load(early["id"]) and [c["id"] for c in collect(store)["chats"]] == [early["id"]]
    store.set_rules(local["id"], {"providers": ["local"], "literature": False})
    assert collect(store)["chats"] == []                                          # in it: not shared
    store.assign(early["id"], None)                                               # it leaves with the rules of now
    store.set_rules(local["id"], {"providers": None, "literature": True})
    assert store.load(early["id"])["rules"] == {"providers": ["local"], "literature": False}   # models too
    gone = store.create_project("Gone")
    store.set_rules(gone["id"], {"providers": ["local"], "literature": True})
    left = chat(store, "left behind", gone["id"])
    moved = chat(store, "moved out", gone["id"])
    store.assign(moved["id"], None)
    store.delete_project(gone["id"])
    assert store.load(left["id"])["rules"]["providers"] == ["local"]
    assert [c["title"] for c in collect(store)["chats"]] == []                   # none of them is shared
    open_chat = chat(store, "open")
    store.set_rules(local["id"], {"providers": ["local"], "literature": False})
    store.create_project("Open")
    out = collect(store)
    sent, = out["chats"]
    assert sent["id"] == open_chat["id"] and sent["incognito"] is False and sent["unpublished"] is False
    assert sent["confidential"] is False and "rules" in sent                      # for the hub to check again
    assert {"id": local["id"], "shared": False} in out["projects"]               # unshared: still only that
    shared, = [p for p in out["projects"] if p.get("name") == "Open"]
    assert shared["unpublished"] is False and shared["confidential"] is False and shared["rules"]


def test_only_this_persons_newer_runs_go_and_older_ones_on_request(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path)
    acc = Accounts(tmp_path)
    acc.create("Ada", "")
    acc.create("Ben", "")
    k = portal(tmp_path, monkeypatch, browser, accounts=acc)
    k.client.post("/api/session", headers=k.h, json={"name": "Ben"})
    connect(k, browser)
    run(k.runtime, "20261020-100000-bens", None, user="Ben")
    run(k.runtime, "20261020-100100-adas", None, user="Ada")          # a data folder Ada and Ben share
    run(k.runtime, "20261020-100200-older", None, old=True)            # from before Kelvin said who made runs
    run(k.runtime, "20261020-100300-no-accounts", None, user=None)     # ...or from before accounts
    run(k.runtime, "20261020-100400-incognito", None, user="Ben", incognito=True)
    run(k.runtime, "20261020-100500-queued-then", None, user="Ben", legacy=True)   # an old queue item's run
    s = k.client.post("/api/hub/sync", headers=k.h).json()["sync"]
    assert s["ok"] and s["older_runs"] == 3
    assert [r["local_id"] for r in hub.shared_with(1)["runs"]] == ["20261020-100000-bens"]
    assert k.client.get("/api/settings", headers=k.h).json()["settings"]["hub_old_runs"] is False
    k.client.post("/api/settings", headers=k.h, json={"hub_old_runs": "on"})   # "Also share older runs"
    s = k.client.post("/api/hub/sync", headers=k.h).json()["sync"]
    assert s["ok"] and s["older_runs"] == 0
    assert sorted(r["local_id"] for r in hub.shared_with(1)["runs"]) == [
        "20261020-100000-bens", "20261020-100200-older", "20261020-100300-no-accounts", "20261020-100500-queued-then"]
    assert "adas" not in on_the_hub(hub) and "incognito" not in on_the_hub(hub)   # never anyone else's

    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "solo", demo_envelope(lab))
    runtime.user = "Ben"
    assert runtime._attribute(SimpleNamespace(device=None))["user"] == "Ben"     # new runs say who made them
    (tmp_path / "queue.json").write_text(json.dumps({"items": [
        {"id": "a1", "kind": "plan", "name": "gate_sweep", "text": SWEEP},
        {"id": "a2", "kind": "plan", "name": "gate_sweep", "text": SWEEP, "project": "p00000001"}]}))
    from labharness.scanqueue import ScanQueue
    old_queue = ScanQueue(tmp_path / "queue.json")                     # queued before Kelvin noted incognito chats
    assert [i.legacy for i in old_queue.items] == [True, False]        # held like older runs, and said so on disk
    assert [i.incognito for i in old_queue.items] == [False, False]
    old_queue.save()
    saved = json.loads((tmp_path / "queue.json").read_text())["items"][0]
    assert saved["legacy"] is True and saved["incognito"] is False
    assert runtime._attribute(SimpleNamespace(device=None), None, False, False, True)["legacy"] is True


def test_two_people_on_one_computer_keep_their_own_connections(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path)
    acc = Accounts(tmp_path)
    acc.create("Ada", "")
    acc.create("Ben", "")
    k = portal(tmp_path, monkeypatch, browser, accounts=acc)
    tokens = {}
    for name in ("Ada", "Ben"):        # both use the same hub account here, the case that clashed before
        k.client.post("/api/session", headers=k.h, json={"name": name})
        assert connect(k, browser).status_code == 200
        query = parse_qs(urlsplit(k.opened[-1]).query)
        assert query["person"] == [name]                                # the hub shows whose Kelvin it is
        assert re.match(r"^[A-Za-z0-9:_.-]{8,80}$", query["installation"][0])
        tokens[name] = (k.settings.get("hub_token"), query["installation"][0])
    computer = {tokens[name][1].rsplit(":", 1)[0] for name in tokens}
    assert tokens["Ada"][1] != tokens["Ben"][1] and len(computer) == 1           # one computer, two installations
    assert len(hub.connections(1)) == 2
    for name in ("Ada", "Ben"):
        k.client.post("/api/session", headers=k.h, json={"name": name})
        assert k.client.get("/api/hub", headers=k.h).json()["connected"]
        assert browser.get("/api/me", headers={"Authorization": "Bearer " + tokens[name][0]}).status_code == 200


def test_a_redirect_never_carries_the_token():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from labharness.hub.client import HubClient, HubClientError
    seen = []

    class Redirecting(BaseHTTPRequestHandler):
        def answer(self):
            seen.append((self.command, self.path, self.headers.get("Authorization")))
            if int(self.headers.get("Content-Length") or 0):
                self.rfile.read(int(self.headers["Content-Length"]))
            if self.path.startswith("/api/"):   # off to somewhere else, as a hijacked or misconfigured hub might
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/elsewhere" % self.server.server_port)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
        do_GET = do_POST = do_PUT = do_DELETE = answer

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirecting)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = HubClient("http://127.0.0.1:%d" % server.server_port, token="khub_secret")
        with pytest.raises(HubClientError) as e:
            client.me()
        assert e.value.status == 302
        with pytest.raises(HubClientError):
            client.push(chats=[])
        with pytest.raises(HubClientError):
            client.upload("run-1", "data.csv", b"x")
        assert client.disconnect() is False                             # not confirmed: not a 204
        assert [path for _, path, _ in seen] == ["/api/me", "/api/sync", "/api/sync/runs/run-1/files/data.csv",
                                                 "/api/token"]          # never followed
    finally:
        server.shutdown()
        server.server_close()


def test_disconnecting_or_reconnecting_during_a_sync(tmp_path, monkeypatch):
    import threading
    hub, browser = the_hub(tmp_path)
    k = portal(tmp_path, monkeypatch, browser)
    connect(k, browser)
    for i in range(3):
        run(k.runtime, "20261020-10000%d-scan" % i, None)
    base = through(browser)
    puts = []

    def disconnect_at_first_file(method, url, body=None, headers=None):
        if method == "PUT":
            puts.append(url)
            if len(puts) == 1:
                k.link.disconnect()        # pressed while the first file goes up
        return base(method, url, body, headers)
    k.link.http = disconnect_at_first_file
    s = k.link.sync_once()
    assert not s["ok"] and "Interrupted" in s["error"] and len(puts) == 1   # nothing more after that
    assert k.settings.get("hub_token") is None and not k.link.connected

    k.link.http = base
    connect(k, browser)
    old = k.settings.get("hub_token")
    at_upload, go, out, again = threading.Event(), threading.Event(), [], []

    def held_at_first_file(method, url, body=None, headers=None):
        if method == "PUT" and not at_upload.is_set():
            at_upload.set()
            go.wait(20)
        return base(method, url, body, headers)
    k.link.http = held_at_first_file
    syncing = threading.Thread(target=lambda: out.append(k.link.sync_once()))
    syncing.start()
    assert at_upload.wait(20)
    reconnecting = threading.Thread(target=lambda: again.append(connect(k, browser)))
    reconnecting.start()
    reconnecting.join(20)               # the page answers at once, while that upload still hangs
    new = k.link.token
    assert not reconnecting.is_alive() and again and again[0].status_code == 200 and new not in ("", old)
    assert k.client.get("/api/hub", headers=k.h).json()["sync"] is None    # this connection hasn't synced yet
    go.set()
    syncing.join(20)
    assert out and not out[0]["ok"] and "Interrupted" in out[0]["error"]
    assert k.settings.get("hub_token") == new and k.link.connected   # the old sync's 401 didn't wipe the new one
    k.link.http = base
    s = k.link.sync_once()                                            # a new connection sends everything again
    assert s["ok"] and s["sent"]["runs"] == 3 and s["files"] == 9


def test_a_file_the_hub_keeps_failing_on_waits_while_the_others_go(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path)
    k = portal(tmp_path, monkeypatch, browser)
    connect(k, browser)
    d = run(k.runtime, "20261020-100000-scan", None)
    (d / "bad.csv").write_text("x", encoding="utf-8")
    base = through(browser)
    tries = []

    def bad_fails(method, url, body=None, headers=None):
        if method == "PUT" and url.endswith("/bad.csv"):
            tries.append(url)
            return 500, {"error": "the hub fell over"}   # as a 524 from Cloudflare on a slow big upload would
        return base(method, url, body, headers)
    k.link.http = bad_fails
    s = k.link.sync_once()
    assert s["ok"] and s["files"] == 3 and len(tries) == 1               # the others went
    assert [r["again"] for r in s["refused"] if r.get("file") == "bad.csv"] == [300]
    assert k.link.sync_once()["ok"] and len(tries) == 1                 # not again so soon
    state_file = tmp_path / "hub-sync.json"

    def due_now():
        state = json.loads(state_file.read_text(encoding="utf-8"))
        for files in state["retry"].values():
            for entry in files.values():
                entry[0] = 0
        state_file.write_text(json.dumps(state), encoding="utf-8")
        return state
    due_now()
    k.link.sync_once()
    retry = json.loads(state_file.read_text(encoding="utf-8"))["retry"]
    assert len(tries) == 2 and retry["20261020-100000-scan"]["bad.csv"][1] == 600   # waits twice as long now

    for i in range(5):
        run(k.runtime, "20261020-1001%02d-more" % i, None)
    k.link.http = lambda m, u, b=None, h=None: (503, {"error": "down"}) if m == "PUT" else base(m, u, b, h)
    s = k.link.sync_once()                                              # a hub failing on everything:
    assert not s["ok"] and "down" in s["error"]                         # the sync ends after a few files
    assert len([r for r in s["refused"] if "down" in r["why"]]) == hubsync.FAILURES
    k.link.http = base
    due_now()
    s = k.link.sync_once()
    assert s["ok"] and s["files"] == 5 * 3 + 1                         # all of them, bad.csv too, once it works
    assert json.loads(state_file.read_text(encoding="utf-8"))["retry"] == {}


def test_a_connection_that_breaks_on_one_file_holds_back_only_that_file(tmp_path, monkeypatch):
    """A big upload that times out, or a hub that closes the connection after refusing it, is a network error, not
    an answer: that file waits, the others go (it used to end every sync at that file)."""
    hub, browser = the_hub(tmp_path)
    k = portal(tmp_path, monkeypatch, browser)
    connect(k, browser)
    d = run(k.runtime, "20261020-100000-scan", None)
    (d / "big.csv").write_text("x", encoding="utf-8")
    run(k.runtime, "20261020-100100-other", None)
    base = through(browser)
    tries = []

    def big_times_out(method, url, body=None, headers=None):
        if method == "PUT" and url.endswith("/big.csv"):
            tries.append(url)
            raise TimeoutError("timed out")
        return base(method, url, body, headers)
    k.link.http = big_times_out
    s = k.link.sync_once()
    assert s["ok"] and s["files"] == 6 and len(tries) == 1               # both runs' other files went
    again = [r for r in s["refused"] if r.get("file") == "big.csv"]
    assert again[0]["again"] == 300 and "connection broke" in again[0]["why"]
    assert k.link.sync_once()["ok"] and len(tries) == 1                 # and it waits before trying again


def test_unshared_work_keeps_its_place_and_its_notes(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path)
    k = portal(tmp_path, monkeypatch, browser)
    store, rt = k.store, k.runtime
    shared, secret = store.create_project("Shared sample"), store.create_project("Secret")
    store.set_sharing(secret["id"], "confidential")
    rt.mounted = {"project": shared["id"], "device": "d1", "device_name": "D1"}
    rt.attribution = {"project": secret["id"], "chat": "abcdefabcdef"}
    meta = rt._attribute(SimpleNamespace(device=None))
    assert meta["project"] == secret["id"]                              # the confidential chat's project wins,
    assert meta["device"] is None and meta["device_name"] is None       # without the other project's sample
    rt.attribution = {"project": None, "chat": "abcdefabcdef"}
    meta = rt._attribute(SimpleNamespace(device=None))
    assert (meta["project"], meta["device"], meta["device_name"]) == (shared["id"], "d1", "D1")   # else the sample's
    rt.mounted = {}

    agent = k.client.app.app.state.agent
    hidden = chat(store, "secret chat", secret["id"])
    context = agent.context(store.load(hidden["id"]))
    remember = next(t for t in context["tools"] if t["name"] == "remember")
    assert remember["schema"]["properties"]["scope"]["enum"] == ["project"]   # no notes for every conversation
    said, failed = agent._remember(store.load(hidden["id"]), context,
                                   {"scope": "global", "kind": "preference", "text": "the secret sample is CrSBr"})
    assert failed and store.global_notes() == []
    opened = chat(store, "open chat", shared["id"])
    scopes = next(t for t in agent.context(store.load(opened["id"]))["tools"] if t["name"] == "remember")
    assert scopes["schema"]["properties"]["scope"]["enum"] == ["global", "project"]


class Recorder(Provider):
    """A model that answers "ok", noting how a run started now would be filed."""
    name, label = "scripted", "Scripted"
    runtime = None

    def __init__(self):
        self.metas = []

    def available(self):
        return True

    async def models(self):
        return [ModelInfo("m1", "Model One", self.name)]

    async def stream(self, model, system, history, tools, emit):
        self.metas.append(self.runtime._attribute(SimpleNamespace(device=None)))
        await emit({"type": "text", "delta": "ok"})
        return TurnResult([{"type": "text", "text": "ok"}], stop_reason="end", model=model)


def turn(k, chat_id, text="measure something"):
    with k.client.stream("POST", "/api/chats/%s/turn" % chat_id, json={"text": text, "model": "scripted:m1"},
                         headers=k.h) as r:
        assert r.status_code == 200
        list(r.iter_lines())


def test_runs_from_a_chat_that_keeps_unshared_marks_stay_here(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path)
    recorder = Recorder()
    k = portal(tmp_path, monkeypatch, browser, providers={"scripted": recorder})
    recorder.runtime = k.runtime
    store = k.store
    secret = store.create_project("Secret")
    store.set_sharing(secret["id"], "confidential")
    left = chat(store, "left the secret", secret["id"])
    store.assign(left["id"], None)                                      # unfiled now, and still unshared
    quiet = store.create_project("No search")
    store.set_rules(quiet["id"], {"providers": None, "literature": False})
    ran = chat(store, "ran without search", quiet["id"])                # it runs under the limit: it keeps it
    plain = chat(store, "plain")
    for c in (left, ran, plain):
        turn(k, c["id"])
    assert [m.get("unshared", False) for m in recorder.metas] == [True, True, False]
    assert recorder.metas[0]["project"] is None                         # filed under General, yet never shared
    k.runtime.attribution = {"project": None, "chat": left["id"], "unshared": True}
    assert k.runtime.queue_add(SWEEP).startswith("added") and k.runtime.queue.items[-1].unshared   # queued: too
    k.runtime.attribution = {}

    connect(k, browser)
    run(k.runtime, "20261020-100000-from-left", None, unshared=True)
    run(k.runtime, "20261020-100100-plain", None)
    assert k.link.sync_once()["ok"]
    assert [r["local_id"] for r in hub.shared_with(1)["runs"]] == ["20261020-100100-plain"]
    assert sorted(c["title"] for c in hub.shared_with(1)["chats"]) == ["plain"]


def test_connecting_again_names_the_earlier_installations(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path)
    k = portal(tmp_path, monkeypatch, browser)
    computer, folder = k.settings.installation_id(), tmp_path          # without accounts: the settings' folder
    connect(k, browser)
    query = parse_qs(urlsplit(k.opened[-1]).query)
    mine = query["installation"][0]
    assert mine == installation_for(computer, folder) and k.settings.hub_installations() == [mine]
    sent = query["previous"][0].split(",")
    assert sent == previous_installations(computer, folder) and computer in sent and mine not in sent
    assert all(re.match(r"^[A-Za-z0-9:_.-]{8,80}$", i) for i in sent) and len(sent) <= 5
    assert installation_for(computer, folder / "x" / "..") == mine      # the same folder, spelled otherwise
    if sys.platform == "win32":
        assert installation_for(computer, str(folder).upper()) == mine
    later = folder / "users" / "Ada"                                     # say, accounts are turned on later
    assert previous_installations(computer, later, k.settings.hub_installations())[0] == mine
    connect(k, browser)                                                  # connecting again: still one id
    assert k.settings.hub_installations() == [mine]


def test_the_hub_folds_in_what_earlier_installations_shared(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path)
    k = portal(tmp_path, monkeypatch, browser)
    real = hubsync.installation_for
    # connected before under another digest of the folder (a short 8.3 name, say): only Kelvin knows that id
    monkeypatch.setattr(hubsync, "installation_for", lambda computer, folder: computer + ":0123456789ab")
    connect(k, browser)
    old = k.settings.get("hub_token")
    crsbr = k.store.create_project("CrSBr")
    chat(k.store, "Unfiled once")
    chat(k.store, "In CrSBr", crsbr["id"])
    assert k.link.sync_once()["ok"]
    monkeypatch.setattr(hubsync, "installation_for", real)
    connect(k, browser)                                                  # this version's id, naming the old one
    assert k.link.sync_once()["ok"]
    mine = real(k.settings.installation_id(), tmp_path)
    with hub.connect() as con:
        chats = [dict(r) for r in con.execute("SELECT installation_id, title FROM chats")]
    assert sorted(c["title"] for c in chats) == ["In CrSBr", "Unfiled once"]   # one copy each,
    assert {c["installation_id"] for c in chats} == {mine}                     # under the new id
    assert browser.get("/api/me", headers={"Authorization": "Bearer " + old}).status_code == 401
    k.client.patch("/api/projects/" + crsbr["id"], headers=k.h, json={"sharing": "confidential"})
    assert k.link.sync_once()["ok"]
    assert "CrSBr" not in on_the_hub(hub) and len(hub.connections(1)) == 1
