"""The Kelvin hub in the portal: connecting from Settings through the browser, the token kept secret, and the
background sync of what a person shares (never what is unpublished, confidential, model-limited or incognito)."""
import json
import re
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
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402

try:
    import cryptography  # noqa: F401
    CRYPTO = True
except ImportError:
    CRYPTO = False

HUB = "http://127.0.0.1:8780"
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
    browser = TestClient(hub_app(hub, hosts=["testserver"], backups=False))
    r = browser.post("/register", data={"name": email.split("@")[0].title(), "email": email,
                                        "password": "correct horse", "consent": "yes"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    if lab:
        browser.post("/labs", data={"name": lab, "csrf": csrf(browser.get("/account"))})
    return hub, browser


def portal(tmp_path, monkeypatch, browser, accounts=None, calls=None):
    opened = []
    monkeypatch.setattr(web_app, "open_url", opened.append)
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    settings = Settings(tmp_path / "settings.json")
    store = ChatStore(tmp_path / "lab" / "chats" if accounts else tmp_path / "chats", create=not accounts)
    guard = web_app.create_app(runtime, settings, store, hosts=["testserver"], demo=True, accounts=accounts)
    guard.app.state.hub.http = through(browser, calls)
    return SimpleNamespace(client=TestClient(guard), h={"X-Kelvin-Token": settings.access_key}, runtime=runtime,
                           settings=settings, store=store, opened=opened, link=guard.app.state.hub)


def connect(k, browser, lab="Smith Lab", action="allow"):
    """Settings → Account → Kelvin hub → Connect, then the person's yes on the hub's page in their browser."""
    url = k.client.post("/api/hub/connect", headers=k.h, json={"url": HUB}).json()["url"]
    assert k.opened[-1] == url and url.startswith(HUB + "/connect?")
    path = url[len(HUB):]
    page = browser.get(path)
    assert page.status_code == 200 and "wants to connect" in page.text
    form = {key: v[0] for key, v in parse_qs(urlsplit(path).query).items()}
    labs = dict(re.findall(r'<option value="(\d+)">([^<]+)</option>', page.text))
    chosen = next((i for i, name in labs.items() if name == lab), "")
    r = browser.post("/connect", data=dict(form, csrf=csrf(page), lab=chosen, action=action), follow_redirects=False)
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


def run(runtime, name, project, **meta):
    d = runtime.data_dir / "runs" / name
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(dict(run_id=name, project=project, device_name="D1", status="done",
                                                 started="2026-10-20T10:00:00.000", **meta)), encoding="utf-8")
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
