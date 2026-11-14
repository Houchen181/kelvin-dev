"""The hub's findings of the 0.3.52 audit (H1 to H7), each as its repro in audit-repros/hub showed it: taking back
shared work after the hub's address or the computer's id changed, model use that went uncharged, the relay's memory,
one installation row for every account, model use on Export and Delete account, registering with an email that has an
account, and malformed input that was a 500."""
import asyncio
import gc
import json
import sqlite3
import zipfile
from urllib.parse import urlsplit

import pytest

pytest.importorskip("starlette")
httpx = pytest.importorskip("httpx")
pytest.importorskip("cryptography")

from starlette.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from labharness.hub import catalog, llm, relay as relaying  # noqa: E402
from labharness.hub.db import Hub  # noqa: E402
from labharness.hub.server import create_app  # noqa: E402
from labharness.web import hubsync  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402

PRICES = {"anthropic": {"models": {"claude-opus-5-5": {"cost": {"input": 4, "output": 20}}}},
          "openai": {"models": {"gpt-6-sol": {"cost": {"input": 2, "output": 10}}}}}
KELVIN = {"id": "urn:uuid:11111111-aaaa", "name": "Lab PC", "platform": "win32", "version": "0.3.52"}
KEYS = {"anthropic": "sk-ant-owner-1234", "openai": "sk-openai-owner-5678"}
NOTE = {}


@pytest.fixture(autouse=True)
def prices(monkeypatch):
    monkeypatch.setattr(catalog, "_read_models_dev", lambda: PRICES)


def make(tmp_path, handler=None, raise_errors=True):
    hub = Hub(tmp_path / "hub")
    app = create_app(hub, hosts=["testserver"], backups=False,
                     llm_transport=httpx.MockTransport(handler) if handler else None)
    return hub, app, TestClient(app, raise_server_exceptions=raise_errors)


def register(client, email="ada@example.org", name="Ada", installation=None):
    r = client.post("/api/register", json={"name": name, "email": email, "password": "correct horse", "consent": True,
                                           "installation": installation or KELVIN})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def owner_keys(hub):
    g = llm.Gateway(hub, catalog.Catalog(hub.home))
    for service, key in KEYS.items():
        g.set_key(service, key)
    return g


def usage(hub):
    with hub.connect() as con:
        return [dict(r) for r in con.execute("SELECT service, input, output, usd FROM llm_usage ORDER BY id")]


# ------------------------------------------------------------------ H1: taking back

def through(client):
    def http(method, url, body=None, headers=None):
        r = client.request(method, urlsplit(url).path, content=body, headers=headers or {})
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text
    return http


def on_hub(hub):
    with hub.connect() as con:
        return [tuple(r) for r in con.execute("SELECT installation_id, title FROM chats ORDER BY id")]


def kelvin(tmp_path, client, where):
    store = ChatStore(tmp_path / "chats")
    chat = store.create("some-model")
    chat["title"] = "Tc of sample B7"
    chat["messages"] = [{"role": "user", "text": "the transition is at 4.2 K"}]
    store.save(chat)
    settings = {"hub_share": "on"}
    link = hubsync.HubLink(settings, store, lambda: tmp_path / "data",
                           lambda: (where["url"], where["token"], "ada@example.org|" + where["installation"]),
                           http=through(client))
    link.use(tmp_path / "person")
    return link, settings


def test_h1_switched_off_takes_back_what_went_before_the_hubs_address_changed(tmp_path):
    hub, app, client = make(tmp_path)
    where = {"url": "https://kelvin.tail269ee5.ts.net", "token": register(client), "installation": KELVIN["id"]}
    link, settings = kelvin(tmp_path, client, where)
    assert link.sync_once()["sent"]["chats"] == 1 and len(on_hub(hub)) == 1
    settings.pop("hub_share")                             # never chose (the 0.3.51 update), or switched off
    where["url"] = "https://hub.kelvin-lab.example"       # the same hub and database, at its new address
    st = link.status()                                    # the hub is asked before Kelvin says nothing is there
    assert st["taking_back"] is True and st["on_hub"] == {"chats": 1, "runs": 0}
    last = link.sync_once()
    assert last is not None and last["ok"] and last["removed"] >= 1
    assert on_hub(hub) == []
    st = link.status()
    assert st["taking_back"] is False and st["on_hub"] == {"chats": 0, "runs": 0}
    assert link.sync_once() is None                       # nothing left: no more requests while it stays off


def test_h1_switched_off_takes_back_what_went_under_the_computers_earlier_id(tmp_path):
    hub, app, client = make(tmp_path)
    where = {"url": "https://hub.test", "token": register(client), "installation": KELVIN["id"]}
    link, settings = kelvin(tmp_path, client, where)
    link.sync_once()
    new = dict(KELVIN, id="urn:uuid:99999999-bbbb")       # ~/Kelvin/settings.json made again
    r = client.post("/api/signin", json={"email": "ada@example.org", "password": "correct horse",
                                         "installation": new})
    where.update(token=r.json()["access_token"], installation=new["id"])
    link.refresh()
    link.sync_once()
    assert sorted(i for i, _ in on_hub(hub)) == [KELVIN["id"], new["id"]]
    settings["hub_share"] = "off"
    link.refresh()
    last = link.sync_once()
    assert last["ok"] and last["removed"] == 2
    assert on_hub(hub) == []
    assert link.status()["taking_back"] is False


def test_h1_the_hub_takes_back_everything_of_the_account_and_says_what_it_holds(tmp_path):
    hub, app, client = make(tmp_path)
    token = register(client)
    h = {"Authorization": "Bearer " + token}
    r = client.post("/api/sync", headers=h, json={"projects": [{"id": "p1", "name": "CrSBr"}],
                                                  "chats": [{"id": "c1", "title": "t", "project": "p1",
                                                             "messages": []}, {"id": "c2", "title": "u",
                                                                               "messages": []}]})
    assert r.json()["chats"] == 2
    assert client.get("/api/me", headers=h).json()["on_hub"] == {"projects": 1, "chats": 2, "runs": 0}
    r = client.post("/api/sync", headers=h, json={"removed": {"everything": True}})
    assert r.status_code == 200 and r.json()["removed"] == 3
    assert client.get("/api/me", headers=h).json()["on_hub"] == {"projects": 0, "chats": 0, "runs": 0}


# ------------------------------------------------------------------ H2: model use through the hub

def chat_pieces(model):
    out = ["data: %s\n\n" % json.dumps({"id": "c1", "object": "chat.completion.chunk", "model": model, "choices": [
        {"index": 0, "delta": {"content": "word%d " % i}, "finish_reason": None}]}) for i in range(8)]
    out.append("data: %s\n\n" % json.dumps({"id": "c1", "object": "chat.completion.chunk", "model": model,
                                            "choices": [], "usage": {"prompt_tokens": 200000,
                                                                     "completion_tokens": 30000}}))
    return out + ["data: [DONE]\n\n"]


def sse(*events):
    return ["event: %s\ndata: %s\n\n" % (e.get("type", "message"), json.dumps(e)) for e in events]


def claude_pieces():
    return sse({"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant",
                                                     "model": "claude-opus-5-5", "content": [],
                                                     "usage": {"input_tokens": 1000, "output_tokens": 1}}},
               {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
               *[{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "word%d " % i}}
                 for i in range(8)],
               {"type": "content_block_stop", "index": 0},
               {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 30000}},
               {"type": "message_stop"})


def responses_pieces(model, cut):
    response = {"id": "resp_1", "object": "response", "model": model, "status": "incomplete" if cut else "completed",
                "usage": {"input_tokens": 200000, "output_tokens": 30000, "total_tokens": 230000}}
    return sse(*[{"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "content_index": 0,
                  "delta": "word%d " % i} for i in range(8)],
               {"type": "response.incomplete" if cut else "response.completed", "response": response})


class Slow(httpx.AsyncByteStream):
    def __init__(self, pieces, delay=0.01):
        self.pieces, self.delay = [p.encode() for p in pieces], delay

    async def __aiter__(self):
        for p in self.pieces:
            await asyncio.sleep(self.delay)
            yield p

    async def aclose(self):
        pass


async def service(request):
    body = json.loads(request.content) if request.content else {}
    head = {"content-type": "text/event-stream"}
    delay = NOTE.get("delay", 0.01)
    if request.url.host == "api.anthropic.com":
        return httpx.Response(200, headers=head, stream=Slow(claude_pieces(), delay))
    if request.url.path == "/v1/responses":
        return httpx.Response(200, headers=head, stream=Slow(responses_pieces(
            body["model"], bool(body.get("max_output_tokens"))), delay))
    NOTE["stream_options"] = body.get("stream_options")
    return httpx.Response(200, headers=head, stream=Slow(chat_pieces(body["model"]), delay))


async def asgi_post(app, path, token, body, drop_after=None, header="authorization"):
    """One POST as uvicorn hands it over; drop_after=N: the client goes away once it has N pieces of the answer."""
    value = ("Bearer " + token) if header == "authorization" else token
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "client": ("203.0.113.9", 4321), "server": ("testserver", 80),
             "headers": [(b"host", b"testserver"), (header.encode(), value.encode()),
                         (b"content-type", b"application/json")]}
    raw = json.dumps(body).encode()
    state = {"asked": False, "status": None, "pieces": []}
    gone = asyncio.Event()

    async def receive():
        if not state["asked"]:
            state["asked"] = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            state["status"] = message["status"]
        elif message["type"] == "http.response.body" and message.get("body"):
            state["pieces"].append(message["body"])
            if drop_after is not None and len(state["pieces"]) >= drop_after:
                gone.set()
        await asyncio.sleep(0)

    await app(scope, receive, send)
    gc.collect()               # a stream the client left is closed when its generator is collected...
    await asyncio.sleep(0.2)   # ...and its `finally` runs on the loop just after
    return state["status"], state["pieces"]


def gateway(tmp_path):
    hub, app, client = make(tmp_path, service)
    token = register(client)
    owner_keys(hub)
    return hub, app, token


def test_h2_an_answer_its_client_left_before_the_last_event_is_charged(tmp_path):
    hub, app, token = gateway(tmp_path)
    chat = {"model": "gpt-6-sol", "stream": True, "messages": [{"role": "user", "content": "x" * 4000}]}

    async def go():
        status, pieces = await asgi_post(app, "/llm/openai/v1/chat/completions", token, chat, drop_after=8)
        assert status == 200 and b"usage" not in b"".join(pieces)   # all the words, never the usage
        status, pieces = await asgi_post(app, "/llm/anthropic/v1/messages", token, {
            "model": "claude-opus-5-5", "max_tokens": 64000, "stream": True,
            "messages": [{"role": "user", "content": "x"}]}, drop_after=10, header="x-api-key")
        assert b"message_delta" not in b"".join(pieces)
    asyncio.run(go())
    rows = usage(hub)
    assert [r["service"] for r in rows] == ["openai", "anthropic"]
    assert rows[0]["input"] >= 1000 and rows[0]["output"] >= 8 and rows[0]["usd"] > 0   # what passed through
    assert rows[1]["input"] == 1000 and rows[1]["output"] >= 8                         # the prompt as Claude told it


def test_h2_an_answer_cut_at_max_output_tokens_is_charged_and_a_budget_stops_the_next(tmp_path):
    hub, app, token = gateway(tmp_path)
    llm.Gateway(hub, catalog.Catalog(hub.home)).set_budget("default", 0, "0.01")
    ask = {"model": "gpt-6-sol", "stream": True, "input": "write a long essay", "max_output_tokens": 30000}

    async def go():
        return [(await asgi_post(app, "/llm/openai/v1/responses", token, ask))[0] for _ in range(3)]
    assert asyncio.run(go()) == [200, 402, 402]
    assert [(r["input"], r["output"], r["usd"]) for r in usage(hub)] == [(200000, 30000, 0.7)]


def test_h2_answers_streaming_at_once_hold_back_the_budget(tmp_path):
    hub, app, token = gateway(tmp_path)
    llm.Gateway(hub, catalog.Catalog(hub.home)).set_budget("default", 0, "0.05")
    NOTE["delay"] = 0.05
    big = {"model": "gpt-6-sol", "stream": True, "messages": [{"role": "user", "content": "x" * 200000}]}

    async def go():
        first = asyncio.ensure_future(asgi_post(app, "/llm/openai/v1/chat/completions", token, big))
        await asyncio.sleep(0.15)                       # the first answer is streaming now
        second = await asgi_post(app, "/llm/openai/v1/chat/completions", token, big)
        return (await first)[0], second[0]
    try:
        assert asyncio.run(go()) == (200, 402)          # $0.10 of prompt held back: past the $0.05 budget
    finally:
        NOTE.pop("delay", None)


def test_h2_stream_options_that_is_not_an_object_is_not_a_500(tmp_path):
    hub, app, client = make(tmp_path, service, raise_errors=False)
    token = register(client)
    owner_keys(hub)
    r = client.post("/llm/openai/v1/chat/completions", headers={"Authorization": "Bearer " + token},
                    json={"model": "gpt-6-sol", "stream": True, "stream_options": "x", "messages": []})
    assert r.status_code == 200 and NOTE["stream_options"] == {"include_usage": True}


# ------------------------------------------------------------------ H3: the relay's memory

def test_h3_the_relay_holds_a_bounded_part_of_an_answer_its_phone_doesnt_read(tmp_path, monkeypatch):
    monkeypatch.setattr(relaying, "MAX_HELD", 1024 * 1024)
    monkeypatch.setattr(relaying, "STALL", 0.5)
    frame, frames = 64 * 1024, 64                       # 4 MB from the Kelvin, 1 MB held at most
    hub, gate, client = make(tmp_path)
    token = register(client)
    uid = hub.app(token)["user"]["id"]
    relay = gate.app.state.relay
    seen = {"held": 0}

    async def main():
        auth = [(b"host", b"testserver"), (b"authorization", ("Bearer " + token).encode())]
        base = {"asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1", "root_path": "",
                "query_string": b"", "client": ("203.0.113.9", 1), "server": ("testserver", 80), "headers": auth}
        from_kelvin, asked = asyncio.Queue(), asyncio.Queue()

        async def ws_send(message):
            if message["type"] == "websocket.send" and message.get("text"):
                asked.put_nowait(json.loads(message["text"]))

        socket = asyncio.ensure_future(gate(dict(base, type="websocket", scheme="ws", path="/api/relay/kelvin",
                                                 raw_path=b"/api/relay/kelvin", subprotocols=[]),
                                            from_kelvin.get, ws_send))
        from_kelvin.put_nowait({"type": "websocket.connect"})
        from_kelvin.put_nowait({"type": "websocket.receive", "text": json.dumps({"t": "hello", "name": "PC"})})
        while not relay.links.get(uid):
            await asyncio.sleep(0.01)
        state = {"asked": False}

        async def phone_receive():
            if not state["asked"]:
                state["asked"] = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()

        async def phone_send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                await asyncio.Event().wait()            # the phone reads one piece, then nothing more

        path = "/api/relay/kelvins/%s/api/state" % KELVIN["id"]
        phone = asyncio.ensure_future(gate(dict(base, type="http", method="GET", scheme="http", path=path,
                                                raw_path=path.encode()), phone_receive, phone_send))
        req = await asyncio.wait_for(asked.get(), 5)
        rid = req["id"]
        from_kelvin.put_nowait({"type": "websocket.receive", "text": json.dumps(
            {"t": "head", "id": rid, "status": 200, "headers": [["content-type", "application/json"]]})})
        link = relay.links[uid][KELVIN["id"]]
        for _ in range(frames):
            from_kelvin.put_nowait({"type": "websocket.receive", "bytes": rid.to_bytes(4, "big") + b"x" * frame})
        for _ in range(300):
            seen["held"] = max(seen["held"], link.held.get(rid, 0))
            if from_kelvin.empty():
                break
            await asyncio.sleep(0.01)
        seen["dropped"] = rid not in link.waiting
        seen["wire"] = from_kelvin.qsize()
        await asyncio.sleep(0.05)                       # the cancel goes out on the loop
        cancels = []
        while not asked.empty():
            cancels.append(asked.get_nowait())
        seen["cancelled"] = any(f.get("t") == "cancel" and f.get("id") == rid for f in cancels)
        for task in (phone, socket):
            task.cancel()
        await asyncio.gather(phone, socket, return_exceptions=True)

    asyncio.run(main())
    assert seen["held"] <= 1024 * 1024 + 64 * 1024     # never more than MAX_HELD and a frame
    assert seen["dropped"] and seen["cancelled"]        # the phone read nothing for STALL: dropped, the Kelvin told
    assert seen["wire"] == 0                            # and the socket read on


# ------------------------------------------------------------------ H4: one installation row, every account

def relay_try(client, token):
    try:
        with client.websocket_connect("/api/relay/kelvin", headers={"Authorization": "Bearer " + token}) as ws:
            ws.send_text(json.dumps({"t": "hello", "name": "Lab PC", "version": "0.3.52", "direct": False}))
            listed = client.get("/api/relay/kelvins", headers={"Authorization": "Bearer " + token}).json()
            return "accepted", [(k["name"], k["platform"], k["online"]) for k in listed["kelvins"]]
    except WebSocketDisconnect as e:
        return e.code, None


def test_h4_another_account_naming_a_kelvins_id_cant_make_it_a_phone(tmp_path):
    hub, app, client = make(tmp_path)
    ada = register(client)
    assert relay_try(client, ada) == ("accepted", [("Lab PC", "win32", True)])
    register(client, "mallory@example.org", "Mallory",
             installation={"id": KELVIN["id"], "name": "pwned", "platform": "ios", "version": "9"})
    assert relay_try(client, ada) == ("accepted", [("Lab PC", "win32", True)])
    assert [c["platform"] for c in hub.connections(hub.app(ada)["user"]["id"])] == ["win32"]


def test_h4_a_hub_of_schema_3_keeps_each_sign_ins_platform(tmp_path):
    con = sqlite3.connect(str(tmp_path / "old.sqlite3"))   # the tables as a hub before this had them
    con.executescript("CREATE TABLE installations (id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', platform TEXT"
                      " NOT NULL DEFAULT ''); CREATE TABLE tokens (id INTEGER PRIMARY KEY, installation_id TEXT);"
                      " INSERT INTO installations VALUES ('urn:uuid:pc', 'Lab PC', 'win32'), ('urn:uuid:ph', 'iPhone',"
                      " 'ios'); INSERT INTO tokens VALUES (1, 'urn:uuid:pc'), (2, 'urn:uuid:ph'), (3, NULL);")
    Hub._migrate_3_to_4(con)
    assert con.execute("SELECT id, platform, name FROM tokens ORDER BY id").fetchall() == [
        (1, "win32", "Lab PC"), (2, "ios", "iPhone"), (3, "", "")]
    con.close()


# ------------------------------------------------------------------ H5: model use on Export and Delete account

def test_h5_export_has_the_persons_model_use_and_delete_takes_their_budget(tmp_path):
    hub, app, client = make(tmp_path, lambda request: httpx.Response(200, json={
        "id": "m", "type": "message", "role": "assistant", "content": [],
        "usage": {"input_tokens": 1000, "output_tokens": 500}}))
    ada = register(client)
    owner_keys(hub)
    r = client.post("/llm/anthropic/v1/messages", headers={"x-api-key": ada},
                    json={"model": "claude-opus-5-5", "max_tokens": 10, "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    uid = hub.app(ada)["user"]["id"]
    llm.Gateway(hub, catalog.Catalog(hub.home)).set_budget("user", uid, "50")
    for _ in range(50):
        if usage(hub):
            break
        asyncio.run(asyncio.sleep(0.02))
    with zipfile.ZipFile(hub.export(uid)) as z:
        models = json.loads(z.read("models.json"))
        assert "models.json" in z.read("README.txt").decode()
    assert models["budget"] == 50.0 and [(c["model"], c["input"], c["output"]) for c in models["calls"]] == [
        ("claude-opus-5-5", 1000, 500)]
    hub.delete_user(uid)
    with hub.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM llm_budgets WHERE scope = 'user' AND ref = ?", (uid,)).fetchone()[0] == 0


# ------------------------------------------------------------------ H6: registering with a taken email

def test_h6_registering_with_an_email_that_has_an_account_is_limited(tmp_path):
    hub, app, client = make(tmp_path)
    register(client, "mallory@example.org", "Mallory")
    answers = []
    for i in range(25):
        r = client.post("/api/register", json={"name": "x", "email": "mallory@example.org", "password": "whatever-%d" % i,
                                               "consent": True, "installation": KELVIN})
        answers.append(r.status_code)
    assert answers[0] == 400 and answers[-1] == 429      # 20 an hour from one address, as registrations


# ------------------------------------------------------------------ H7: malformed input

def test_h7_malformed_input_is_refused_not_a_500(tmp_path):
    hub, app, client = make(tmp_path, raise_errors=False)
    head = {"content-type": "application/json"}
    r = client.post("/api/register", content=b'{"name": "A\\ud800", "email": "s1@example.org", "password": '
                    b'"long enough", "consent": true, "installation": {"id": "urn:uuid:22222222"}}', headers=head)
    assert r.status_code == 400
    register(client, "mallory@example.org", "Mallory")
    r = client.post("/api/signin", content=b'{"email": "mallory@example.org", "password": "correct horse", '
                    b'"installation": {"id": "urn:uuid:33333333", "name": "PC\\udfff"}}', headers=head)
    assert r.status_code == 400
    assert client.post("/api/signin", content=b"[" * 15000, headers=head).status_code == 400
    token = client.post("/api/signin", json={"email": "mallory@example.org", "password": "correct horse",
                                             "installation": {"id": "urn:uuid:44444444"}}).json()["access_token"]
    assert client.post("/api/sync", headers={"Authorization": "Bearer " + token},
                       content=b'{"chats": ' + b"[" * 15000).status_code == 400
    r = client.post("/api/sync", headers={"Authorization": "Bearer " + token}, json={"chats": [
        {"id": "odd", "title": "a chat", "messages": [{"role": "assistant", "blocks": 5},
                                                      {"role": "tool", "results": 3}]}]})
    assert r.json()["chats"] == 1
    key = hub.new_operator_key().read_text(encoding="utf-8").split()[-1]
    client.post("/admin/signin", data={"key": key}, follow_redirects=False)
    uid = hub.app(token)["user"]["id"]
    with hub.connect() as con:
        cid = con.execute("SELECT id FROM chats WHERE user_id = ? AND local_id = 'odd'", (uid,)).fetchone()[0]
    assert client.get("/admin/users/%d/chats/%d" % (uid, cid)).status_code == 200
