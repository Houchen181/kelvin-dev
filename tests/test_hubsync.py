"""Sharing with the Kelvin team in the portal (hubsync.py): over the person's Kelvin account sign-in, on unless they
switch it off (which takes back what was shared), run records without their files, nothing before the hub's terms
are agreed to, and never what is unpublished, confidential, model-limited or incognito."""
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")
pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402

import labharness.web.app as web_app  # noqa: E402
from simlab import build_virtual_lab, demo_envelope  # noqa: E402
from labharness.accounts import Accounts  # noqa: E402
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.hub.client import collect  # noqa: E402
from labharness.hub.db import Hub  # noqa: E402
from labharness.hub.server import create_app as hub_app  # noqa: E402
from labharness.web import hubsync  # noqa: E402
from labharness.web.providers.base import ModelInfo, Provider, TurnResult  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402

try:
    import cryptography  # noqa: F401
    CRYPTO = True
except ImportError:
    CRYPTO = False

HUB = "https://hub.test"
ADA = {"email": "ada@example.org", "password": "correct horse"}
BEN = {"email": "ben@example.org", "password": "battery staple"}

SWEEP = """plan: gate_sweep
setup: { lockin.amplitude: 0.05, smu.output: on }
sweep: { smu.V: {from: 0, to: 2, step: 0.5} }
settle: 0.1
measure: [lockin.X, smu.I]
after: { smu.V: 0 }"""


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


def the_hub(tmp_path, monkeypatch, *people):
    """The hub with sharing on and these people registered there (they agreed to its terms), and every way Kelvin
    reaches it (signing in, sharing) going to its test app."""
    hub = Hub(tmp_path / "hub")
    hub.config["sharing"] = True
    browser = TestClient(hub_app(hub, hosts=["testserver"], backups=False))
    for p in people or (ADA,):
        hub.register(p["email"], p["email"].split("@")[0].title(), p["password"], True)
    monkeypatch.setattr("labharness.hub.client._http", through(browser))
    return hub, browser


def portal(tmp_path, monkeypatch, browser, calls=None, providers=None, accounts=True, hub_address=HUB):
    """A Kelvin whose people sign in with Kelvin accounts on the test hub (accounts=False: one person, as before
    accounts, with nobody to share for)."""
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    settings = Settings(tmp_path / "settings.json")
    if hub_address:
        settings.set_account_hub(hub_address)
    acc = Accounts(tmp_path) if accounts else None
    store = ChatStore(tmp_path / "lab" / "chats", create=False) if accounts else ChatStore(tmp_path / "chats")
    guard = web_app.create_app(runtime, settings, store, hosts=["testserver"], accounts=acc, providers=providers)
    link = guard.app.state.hub
    link.http = through(browser, calls)
    return SimpleNamespace(client=TestClient(guard), h={"X-Kelvin-Token": settings.access_key}, runtime=runtime,
                           settings=settings, store=store, link=link, home=tmp_path)


def sign_in(k, who=ADA):
    r = k.client.post("/api/session", headers=k.h, json=who)
    assert r.status_code == 200, r.text
    return r.json()


def mine(k, name="hub-sync.json"):
    """A file in the signed-in person's own folder."""
    return k.home / "users" / k.runtime.user / name


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


def hub_files(tmp_path):
    return [p for p in (tmp_path / "hub" / "files").rglob("*") if p.is_file()]


def test_sharing_goes_over_the_sign_in_and_never_sends_unshared_work(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path, monkeypatch)
    calls = []
    k = portal(tmp_path, monkeypatch, browser, calls=calls)
    assert k.client.get("/api/hub", headers=k.h).status_code == 401      # nobody signed in yet
    sign_in(k)                                                          # and nothing else to do: no connecting
    st = k.client.get("/api/hub", headers=k.h).json()
    assert st["signed_in"] and st["sharing"] and st["url"] == HUB
    assert st["account"]["email"] == ADA["email"] and st["account"]["needs_consent"] is False
    assert st["account"]["account_url"] == HUB + "/account"
    token = k.settings.get("account_token")
    assert token.startswith("khub_")
    for path in ("/api/hub", "/api/settings", "/api/bootstrap", "/api/state"):
        assert token not in k.client.get(path, headers=k.h).text              # the token never goes to the browser
    assert k.client.post("/api/hub/connect", headers=k.h, json={"url": HUB}).status_code != 200   # no connecting
    assert k.client.get("/hub/callback?state=x").status_code != 200
    assert k.client.get("/api/settings", headers=k.h).json()["settings"]["hub_share"] is True   # on by default

    store, rt, me = k.store, k.runtime, k.runtime.user
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
    run(rt, "20261020-100000-iv", crsbr["id"], user=me)
    run(rt, "20261020-100100-general", None, user=me)
    run(rt, "20261020-100200-secret", secret["id"], user=me)
    run(rt, "20261020-100300-incognito", None, user=me, incognito=True)
    run(rt, "20261020-100400-gone", "p0000dead", user=me)              # its project was deleted: can't tell
    (rt.data_dir / "runs" / "20261020-100500-running").mkdir()        # not finished yet

    calls.clear()
    st = k.client.post("/api/hub/sync", headers=k.h).json()
    s = st["sync"]
    assert s["ok"] and s["error"] is None and st["synced_at"] and s["sharing"] is True
    assert s["sent"] == {"projects": 1, "chats": 2, "runs": 2} and s["withheld"] == 2
    assert [c for c in calls if c[0] != "POST" or c[1] != "/api/sync"] == []   # records only: never a file
    seen = hub.shared_with(1)
    assert sorted(c["title"] for c in seen["chats"]) == ["Gate sweep", "Unfiled question"]
    assert sorted(r["local_id"] for r in seen["runs"]) == ["20261020-100000-iv", "20261020-100100-general"]
    assert [p["name"] for p in seen["projects"]] == ["CrSBr"]
    everything = on_the_hub(hub)
    for never in ("Secret", "Local only", "Local chat", "Incognito", "incognito", "gone", "running"):
        assert never not in everything
    assert hub_files(tmp_path) == []                                    # the data files stay on this computer
    state = mine(k).read_text(encoding="utf-8")
    assert token not in state and '"files"' not in state and st["on_hub"] == {"chats": 2, "runs": 2}

    calls.clear()
    assert k.link.sync_once()["ok"] and calls == []                    # nothing changed: nothing sent

    store.delete(unfiled["id"])                                        # removed here, removed there
    store.assign(hidden["id"], None)                                   # out of the unpublished project: still not shared
    k.client.patch("/api/projects/" + crsbr["id"], headers=k.h, json={"sharing": "confidential"})
    s = k.client.post("/api/hub/sync", headers=k.h).json()["sync"]
    assert s["ok"] and s["removed"] >= 1
    seen = hub.shared_with(1)
    assert seen["chats"] == [] and seen["projects"] == []
    assert [r["local_id"] for r in seen["runs"]] == ["20261020-100100-general"]
    assert "CrSBr" not in on_the_hub(hub) and "Secret chat" not in on_the_hub(hub)
    assert collect(store)["chats"] == []

    k.client.delete("/api/session", headers=k.h)
    assert k.link.sync_once() is None                                  # nobody signed in: nothing goes
    sign_in(k)                                                          # a new token, the same place on the hub:
    assert k.settings.get("account_token") not in ("", None, token)
    calls.clear()
    s = k.link.sync_once()
    assert s["ok"] and s["sent"] == {"projects": 0, "chats": 0, "runs": 0} and calls == []   # nothing again


def test_switching_sharing_off_takes_back_what_was_shared(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path, monkeypatch)
    calls = []
    k = portal(tmp_path, monkeypatch, browser, calls=calls)
    sign_in(k)
    crsbr = k.store.create_project("CrSBr")
    chat(k.store, "Gate sweep", crsbr["id"])
    chat(k.store, "Unfiled")
    run(k.runtime, "20261020-100000-iv", crsbr["id"], user=k.runtime.user)
    assert k.link.sync_once()["ok"] and len(hub.shared_with(1)["chats"]) == 2

    gen = k.link.gen
    assert k.client.post("/api/settings", headers=k.h, json={"hub_share": "off"}).status_code == 200
    assert k.link.gen > gen                                            # a sync going on stops at its next request
    assert k.client.get("/api/settings", headers=k.h).json()["settings"]["hub_share"] is False
    st = k.client.get("/api/hub", headers=k.h).json()
    assert st["sharing"] is False and st["taking_back"] and st["on_hub"] == {"chats": 2, "runs": 1}
    s = k.client.post("/api/hub/sync", headers=k.h).json()["sync"]   # "Take back now" (or the next look)
    assert s["ok"] and s["sharing"] is False and s["sent"] == {"projects": 0, "chats": 0, "runs": 0}
    seen = hub.shared_with(1)
    assert seen["chats"] == [] and seen["runs"] == [] and seen["projects"] == []
    assert "Gate sweep" not in on_the_hub(hub) and "CrSBr" not in on_the_hub(hub)
    st = k.client.get("/api/hub", headers=k.h).json()
    assert not st["taking_back"] and st["on_hub"] == {"chats": 0, "runs": 0}

    calls.clear()
    chat(k.store, "Written while it is off")
    assert k.link.sync_once() is None and calls == []                  # off, with nothing there: no request at all
    assert "Written while it is off" not in on_the_hub(hub)

    k.client.post("/api/settings", headers=k.h, json={"hub_share": "on"})
    s = k.link.sync_once()                                             # on again: everything once more
    assert s["ok"] and s["sent"] == {"projects": 1, "chats": 3, "runs": 1}
    assert sorted(c["title"] for c in hub.shared_with(1)["chats"]) == ["Gate sweep", "Unfiled", "Written while it is off"]


def test_a_run_goes_with_its_card_and_again_when_the_card_changes(tmp_path, monkeypatch):
    from labharness import rundata
    from labharness.eventlog import RunRecord
    hub, browser = the_hub(tmp_path, monkeypatch)
    k = portal(tmp_path, monkeypatch, browser)
    sign_in(k)
    name = "20261108-100000-gate_sweep"
    rec = RunRecord(name, k.runtime.runs.home / name, {"plan": "gate_sweep"}, k.runtime.user)
    rec.set_columns({"axes": [{"key": "smu.V", "unit": "V", "kind": "instrument"}],
                     "measures": [{"key": "lockin.X", "unit": "V", "kind": "scalar"}]})
    for i in range(5):
        rec.add({"i": i, "t": float(i), "smu.V": 0.5 * i, "lockin.X": 0.01 * i})
    rec.finish(status="done", points=5, user=k.runtime.user)
    folder = k.runtime.runs.home / name
    rundata.set_analysis(folder, {"assessment": "A clean linear rise.",
                                  "series": [{"y": "lockin.X", "results": [{"name": "slope", "value": "0.02 V/V"}]}]})
    s = k.link.sync_once()
    assert s["ok"] and s["sent"]["runs"] == 1

    def on_hub():
        with hub.connect() as con:
            return json.loads(con.execute("SELECT meta FROM runs WHERE local_id = ?", (name,)).fetchone()["meta"])
    card = on_hub()["card"]
    assert card["assessment"] == "A clean linear rise." and card["results"] == ["lockin.X: slope 0.02 V/V"]
    assert card["plan"] == "gate_sweep" and card["swept"][0]["key"] == "smu.V" and card["measured"][0]["key"] == "lockin.X"
    assert hub_files(tmp_path) == []                                    # the record, never run.h5

    assert k.link.sync_once()["sent"]["runs"] == 0                     # unchanged: not again
    rundata.set_analysis(folder, {"assessment": "Linear, with a kink at 1 V.",   # a fit or re-analysis afterwards
                                  "series": [{"y": "lockin.X", "results": [{"name": "kink", "value": "1.0 V"}]}]})
    s = k.link.sync_once()
    assert s["ok"] and s["sent"]["runs"] == 1                          # the card changed: the record goes again
    assert on_hub()["card"]["results"] == ["lockin.X: kink 1.0 V"]

    items, _ = hubsync.run_items(k.runtime.runs, set(), k.runtime.user)
    bare = dict(items[0], meta={key: v for key, v in items[0]["meta"].items() if key != "card"})
    monkeypatch.setattr(hubsync, "MAX_META", len(json.dumps(bare, sort_keys=True, ensure_ascii=False).encode()) + 10)
    items, _ = hubsync.run_items(k.runtime.runs, set(), k.runtime.user)
    assert [i["id"] for i in items] == [name] and "card" not in items[0]["meta"]   # too big: the card stays out, not the run
    s = k.link.sync_once()
    assert s["ok"] and s["sent"]["runs"] == 1 and "card" not in on_hub()


def test_the_hub_says_new_terms_either_way():
    from labharness.hub.client import HubClientError
    assert hubsync.wants_terms(HubClientError("x", 403, {"error": "Agree first", "consent_url": HUB + "/consent"}))
    assert hubsync.wants_terms(HubClientError("x", 403, {"error": "The hub's terms changed", "needs_consent": True}))
    assert not hubsync.wants_terms(HubClientError("x", 403, {"error": "Confirm your email address before sharing."}))
    assert not hubsync.wants_terms(HubClientError("x", 401, {"error": "not connected", "needs_consent": True}))


def test_a_new_sign_in_is_used_at_once_and_a_sync_with_the_old_token_stops(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path, monkeypatch)
    k = portal(tmp_path, monkeypatch, browser)
    sign_in(k)
    chat(k.store, "First")
    old = k.settings.get("account_token")
    base = through(browser)
    at_sync, go, out = threading.Event(), threading.Event(), []

    def held_at_first_sync(method, url, body=None, headers=None):
        if method == "POST" and url.endswith("/api/sync") and not at_sync.is_set():
            at_sync.set()
            go.wait(20)
        return base(method, url, body, headers)
    k.link.http = held_at_first_sync
    syncing = threading.Thread(target=lambda: out.append(k.link.sync_once()))
    syncing.start()
    assert at_sync.wait(20)
    sign_in(k)                                                          # signed in again meanwhile: a new token
    new = k.settings.get("account_token")
    assert new and new != old
    go.set()                                                            # the old one goes on: the hub refuses it
    syncing.join(20)
    assert out and not out[0]["ok"] and "Interrupted" in out[0]["error"]
    assert k.settings.get("account_token") == new                     # the old sync's 401 forgot nothing
    k.link.http = base
    s = k.link.sync_once()
    assert s["ok"] and s["sent"]["chats"] == 1 and [c["title"] for c in hub.shared_with(1)["chats"]] == ["First"]


def test_a_sign_in_the_hub_ended_is_kept_and_said(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path, monkeypatch)
    k = portal(tmp_path, monkeypatch, browser)
    sign_in(k)
    chat(k.store, "Before")
    assert k.link.sync_once()["ok"]
    token = k.settings.get("account_token")
    assert browser.post("/api/signout", headers={"Authorization": "Bearer " + token}).status_code == 204
    chat(k.store, "After")
    s = k.link.sync_once()
    assert not s["ok"] and s["signed_out"] and "signed this computer out" in s["error"]
    assert k.settings.get("account_token") == token                   # the sign-in's to keep: never forgotten here
    st = k.client.get("/api/hub", headers=k.h).json()
    assert st["signed_in"] and "signed this computer out" in st["problem"]
    sign_in(k)                                                          # signing in again: sharing goes on
    s = k.link.sync_once()
    assert s["ok"] and s["sent"]["chats"] == 1

    def down(method, url, body=None, headers=None):
        raise OSError("connection refused")
    k.link.http = down
    chat(k.store, "Offline")
    s = k.link.sync_once()
    assert not s["ok"] and "can't reach the hub" in s["error"]
    st = k.client.get("/api/hub", headers=k.h).json()
    assert st["signed_in"] and "can't reach the hub" in st["problem"] and k.settings.get("account_token")


def test_nothing_is_shared_before_the_hubs_new_terms_are_agreed_to(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path, monkeypatch)
    k = portal(tmp_path, monkeypatch, browser)
    sign_in(k)
    assert k.client.get("/api/state", headers=k.h).json()["hub_terms"] is None
    chat(k.store, "Waiting for the terms")
    hub.config["consent_version"] = "2099-01-01"                       # new terms on the hub
    s = k.link.sync_once()
    assert not s["ok"] and s["consent"] and "terms changed" in s["error"]
    assert hub.shared_with(1)["chats"] == [] and "Waiting" not in on_the_hub(hub)   # nothing went
    assert k.client.get("/api/state", headers=k.h).json()["hub_terms"] == {"consent_url": HUB + "/consent"}

    k.client.delete("/api/session", headers=k.h)
    assert sign_in(k)["needs_consent"] is True                         # the sign-in says so too, before any sync
    assert k.client.get("/api/state", headers=k.h).json()["hub_terms"] == {"consent_url": HUB + "/consent"}
    k.client.post("/api/settings", headers=k.h, json={"hub_share": "off"})
    assert k.client.get("/api/state", headers=k.h).json()["hub_terms"] is None   # off: nothing waits for them
    k.client.post("/api/settings", headers=k.h, json={"hub_share": "on"})
    assert k.client.get("/api/state", headers=k.h).json()["hub_terms"]

    hub.give_consent(1)                                                # agreed on the hub's page
    st = k.client.get("/api/hub", headers=k.h).json()                  # Kelvin looks again (after Read and agree)
    assert st["account"]["needs_consent"] is False
    assert k.client.get("/api/state", headers=k.h).json()["hub_terms"] is None
    s = k.link.sync_once()
    assert s["ok"] and s["sent"]["chats"] == 1


def test_switching_off_takes_back_what_was_shared_before_the_new_terms_are_agreed_to(tmp_path, monkeypatch):
    """Taking one's work off the hub never waits on terms: the hub takes a sync that only removes."""
    hub, browser = the_hub(tmp_path, monkeypatch)
    k = portal(tmp_path, monkeypatch, browser)
    sign_in(k)
    chat(k.store, "Shared before the new terms")
    assert k.link.sync_once()["ok"] and "Shared before" in on_the_hub(hub)
    hub.config["consent_version"] = "2099-01-01"                       # new terms, not agreed to yet
    chat(k.store, "Written after")
    s = k.link.sync_once()
    assert not s["ok"] and s["consent"] and "Written after" not in on_the_hub(hub)   # nothing new goes
    k.client.post("/api/settings", headers=k.h, json={"hub_share": "off"})
    s = k.link.sync_once()                                             # taking it back needs no agreement
    assert s["ok"] and "Shared before" not in on_the_hub(hub)
    assert k.link.sync_once() is None


def test_a_hub_that_takes_no_shared_work_is_waited_for_quietly(tmp_path, monkeypatch, caplog):
    hub, browser = the_hub(tmp_path, monkeypatch)
    hub.config["sharing"] = False                                      # an older hub, or sharing off there: 404
    k = portal(tmp_path, monkeypatch, browser)
    sign_in(k)
    chat(k.store, "Kept here")
    caplog.set_level(logging.INFO, logger="labharness.hubsync")
    s = k.link.sync_once()
    assert not s["ok"] and s["error"] is None and s["waiting"] == hubsync.NOT_TAKEN
    assert not [r for r in caplog.records if r.name == "labharness.hubsync"]   # nothing in the log
    st = k.client.get("/api/hub", headers=k.h).json()
    assert st["signed_in"] and st["problem"] is None
    assert k.client.get("/api/state", headers=k.h).json()["hub_terms"] is None
    hub.config["sharing"] = True                                       # the hub takes it now
    assert k.link.sync_once()["ok"] and [c["title"] for c in hub.shared_with(1)["chats"]] == ["Kept here"]


def test_each_person_shares_as_themselves_and_their_token_is_encrypted(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path, monkeypatch, ADA, BEN)
    k = portal(tmp_path, monkeypatch, browser)
    sign_in(k, ADA)
    ada = k.runtime.user
    chat(k.store, "Ada's chat")
    assert k.link.sync_once()["ok"]
    token = k.settings.get("account_token")
    raw = (k.home / "users" / ada / "settings.json").read_text(encoding="utf-8")
    if CRYPTO:
        assert token not in raw and "enc:" in raw                        # sealed with her password
    sign_in(k, BEN)                                                     # Ben, on the same computer
    assert k.runtime.user != ada
    chat(k.store, "Ben's chat")
    assert k.link.sync_once()["ok"]
    assert [c["title"] for c in hub.shared_with(1)["chats"]] == ["Ada's chat"]
    assert [c["title"] for c in hub.shared_with(2)["chats"]] == ["Ben's chat"]
    sign_in(k, ADA)
    s = k.link.sync_once()
    assert s["ok"] and s["sent"] == {"projects": 0, "chats": 0, "runs": 0}   # hers are there already


def test_without_a_kelvin_account_nothing_is_shared(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path, monkeypatch)
    Accounts(tmp_path).create("Cat", "")                               # an account on this computer only
    k = portal(tmp_path, monkeypatch, browser, hub_address="")
    chat(k.store, "Cat's chat")
    st = k.client.get("/api/hub", headers=k.h).json()
    assert st["signed_in"] is False and st["account"] is None
    assert k.client.post("/api/hub/sync", headers=k.h).status_code == 409
    assert k.link.sync_once() is None and "Cat" not in on_the_hub(hub)
    assert k.client.get("/api/state", headers=k.h).json()["hub_terms"] is None

    solo = portal(tmp_path / "solo", monkeypatch, browser, accounts=False)   # no accounts at all
    chat(solo.store, "Solo chat")
    assert solo.link.sync_once() is None and "Solo" not in on_the_hub(hub)


def test_create_account_says_what_kelvin_shares_before_anyone_agrees(tmp_path, monkeypatch):
    """Kelvin's own Create account says what goes to the Kelvin team, by the box that agrees to the hub's terms; a
    computer whose people sign in to no hub says nothing of it."""
    hub, browser = the_hub(tmp_path, monkeypatch)
    k = portal(tmp_path, monkeypatch, browser)
    assert k.client.get("/api/session", headers=k.h).json()["hub_sharing"] is True
    static = Path(web_app.__file__).parent / "static"
    page = (static / "index.html").read_text(encoding="utf-8")
    note = page[page.index('id="cn-sharing"'):page.index('id="cn-consent"')]
    assert "Kelvin shares your chats and the records of your runs with the Kelvin team" in note
    assert "Settings → Account" in note and "data files never go" in note
    assert '$("cn-sharing").hidden = !session.hub_sharing' in (static / "app.js").read_text(encoding="utf-8")
    local = portal(tmp_path / "local", monkeypatch, browser, hub_address="")
    assert local.client.get("/api/session", headers=local.h).json()["hub_sharing"] is False


def test_a_connection_of_its_own_from_before_is_dropped(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path, monkeypatch)
    k = portal(tmp_path, monkeypatch, browser)
    sign_in(k)
    path = mine(k, "settings.json")
    k.client.delete("/api/session", headers=k.h)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(hub_url="https://hub.example.org", hub_token="khub_old", hub_installations=["urn:uuid:x:0123456789ab"])
    path.write_text(json.dumps(data), encoding="utf-8")                # as Kelvin 0.3.12 left it
    sign_in(k)
    kept = json.loads(path.read_text(encoding="utf-8"))
    assert not {"hub_url", "hub_token", "hub_installations"} & set(kept) and kept.get("account_token")


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
    hub, browser = the_hub(tmp_path, monkeypatch, BEN)
    k = portal(tmp_path, monkeypatch, browser)
    sign_in(k, BEN)
    ben = k.runtime.user
    run(k.runtime, "20261020-100000-bens", None, user=ben)
    run(k.runtime, "20261020-100100-adas", None, user="Ada")          # a data folder Ada and Ben share
    run(k.runtime, "20261020-100200-older", None, old=True)            # from before Kelvin said who made runs
    run(k.runtime, "20261020-100300-no-accounts", None, user=None)     # ...or from before accounts
    run(k.runtime, "20261020-100400-incognito", None, user=ben, incognito=True)
    run(k.runtime, "20261020-100500-queued-then", None, user=ben, legacy=True)   # an old queue item's run
    s = k.client.post("/api/hub/sync", headers=k.h).json()["sync"]
    assert s["ok"] and s["older_runs"] == 3
    assert [r["local_id"] for r in hub.shared_with(1)["runs"]] == ["20261020-100000-bens"]
    assert k.client.get("/api/settings", headers=k.h).json()["settings"]["hub_old_runs"] is False
    gen = k.link.gen
    k.client.post("/api/settings", headers=k.h, json={"hub_old_runs": "on"})   # "Also share older runs"
    assert k.link.gen > gen                                            # looked at again at once
    s = k.client.post("/api/hub/sync", headers=k.h).json()["sync"]
    assert s["ok"] and s["older_runs"] == 0
    assert sorted(r["local_id"] for r in hub.shared_with(1)["runs"]) == [
        "20261020-100000-bens", "20261020-100200-older", "20261020-100300-no-accounts", "20261020-100500-queued-then"]
    assert "adas" not in on_the_hub(hub) and "incognito" not in on_the_hub(hub)   # never anyone else's
    gen = k.link.gen
    k.client.post("/api/settings", headers=k.h, json={"hub_old_runs": "on", "learn": "on"})
    assert k.link.gen == gen                                           # nothing of sharing changed: left alone
    assert hub_files(tmp_path) == []                                    # records only, here too

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


def test_a_redirect_never_carries_the_token():
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


def test_unshared_work_keeps_its_place_and_its_notes(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path, monkeypatch)
    k = portal(tmp_path, monkeypatch, browser, accounts=False)
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
    said, failed = agent._remember(store.load(hidden["id"]), context,   # nor in the lab's memory, everyone's
                                   {"scope": "lab", "kind": "fact", "text": "the secret sample is CrSBr"})
    assert failed and "lab's memory" in said and rt.lab_notes() == []
    opened = chat(store, "open chat", shared["id"])
    scopes = next(t for t in agent.context(store.load(opened["id"]))["tools"] if t["name"] == "remember")
    assert scopes["schema"]["properties"]["scope"]["enum"] == ["global", "lab", "project"]


class Recorder(Provider):
    """A model that answers "ok"."""
    name, label = "scripted", "Scripted"

    def available(self):
        return True

    async def models(self):
        return [ModelInfo("m1", "Model One", self.name)]

    async def stream(self, model, system, history, tools, emit):
        await emit({"type": "text", "delta": "ok"})
        return TurnResult([{"type": "text", "text": "ok"}], stop_reason="end", model=model)


def turn(k, chat_id, text="measure something"):
    with k.client.stream("POST", "/api/chats/%s/turn" % chat_id, json={"text": text, "model": "scripted:m1"},
                         headers=k.h) as r:
        assert r.status_code == 200
        list(r.iter_lines())


def test_runs_from_a_chat_that_keeps_unshared_marks_stay_here(tmp_path, monkeypatch):
    hub, browser = the_hub(tmp_path, monkeypatch)
    recorder = Recorder()
    k = portal(tmp_path, monkeypatch, browser, providers={"scripted": recorder})
    sign_in(k)
    agent = k.client.app.app.state.agent
    store, me = k.store, k.runtime.user
    secret = store.create_project("Secret")
    store.set_sharing(secret["id"], "confidential")
    left = chat(store, "left the secret", secret["id"])
    store.assign(left["id"], None)                                      # unfiled now, and still unshared
    quiet = store.create_project("No search")
    store.set_rules(quiet["id"], {"providers": None, "literature": False})
    ran = chat(store, "ran without search", quiet["id"])                # it runs under the limit: it keeps it
    plain = chat(store, "plain")
    metas = []
    for c in (left, ran, plain):
        turn(k, c["id"])   # a turn notes the limits a chat ran under
        k.runtime.attribution = agent._attribution(store.load(c["id"]))   # what its steps on the lab are filed under
        metas.append(k.runtime._attribute(SimpleNamespace(device=None)))
        k.runtime.attribution = {}
    assert [m.get("unshared", False) for m in metas] == [True, True, False]
    assert metas[0]["project"] is None                                  # filed under General, yet never shared
    k.runtime.attribution = {"project": None, "chat": left["id"], "unshared": True}
    assert k.runtime.queue_add(SWEEP).startswith("added") and k.runtime.queue.items[-1].unshared   # queued: too
    k.runtime.attribution = {}

    run(k.runtime, "20261020-100000-from-left", None, user=me, unshared=True)
    run(k.runtime, "20261020-100100-plain", None, user=me)
    assert k.link.sync_once()["ok"]
    assert [r["local_id"] for r in hub.shared_with(1)["runs"]] == ["20261020-100100-plain"]
    assert sorted(c["title"] for c in hub.shared_with(1)["chats"]) == ["plain"]
