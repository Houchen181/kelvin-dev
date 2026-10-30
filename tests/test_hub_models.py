"""The hub's model gateway (hub/llm.py): signed-in Kelvins use Claude, GPT, Gemini and OpenCode with the owner's keys,
each service's own API passed through; keys sealed; tokens counted from the answers, never the text kept; monthly
budgets, rate limits and the 80% mail; and the owner console's Models page. A pretend service stands in for each."""
import json
import re
import time

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")
pytest.importorskip("cryptography")

import httpx  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from labharness.hub import catalog, llm  # noqa: E402
from labharness.hub.db import Hub  # noqa: E402
from labharness.hub.server import create_app  # noqa: E402
from test_hub_web import csrf  # noqa: E402

KELVIN = {"id": "urn:uuid:one", "name": "Lab PC", "platform": "win32", "version": "0.4.0"}
PRICES = {"anthropic": {"models": {"claude-opus-5-5": {"cost": {"input": 4, "output": 20, "cache_read": 0.2,
                                                                "cache_write": 5}}}},
          "opencode": {"models": {"deepseek-v4.1-flash": {"cost": {"input": 0.3, "output": 1.2, "cache_read": 0.006}}}},
          "google": {"models": {"gemini-3.1-pro": {"cost": {"input": 2, "output": 12, "tiers": [
              {"input": 4, "output": 18, "tier": {"type": "context", "size": 200000}}]}}}},
          "openai": {"models": {"gpt-6-sol": {"cost": {"input": 2, "output": 10, "cache_read": 0.2}}}}}


def sse(*events):
    return "".join("event: %s\ndata: %s\n\n" % (e.get("type", "message"), json.dumps(e)) for e in events).encode()


CLAUDE_STREAM = sse(
    {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model":
                                          "claude-opus-5-5", "content": [], "stop_reason": None, "stop_sequence": None,
                                          "usage": {"input_tokens": 1000, "cache_read_input_tokens": 500,
                                                    "cache_creation_input_tokens": 100, "output_tokens": 1}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "A clean peak."}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
     "usage": {"output_tokens": 400}},
    {"type": "message_stop"})


def chat_stream(model, usage=True):
    chunks = [{"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": model, "choices": [
        {"index": 0, "delta": {"role": "assistant", "content": "Six points."}, "finish_reason": None}]},
              {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": model, "choices": [
                  {"index": 0, "delta": {}, "finish_reason": "stop"}]}]
    if usage:
        chunks.append({"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": model, "choices": [],
                       "usage": {"prompt_tokens": 2000, "completion_tokens": 300,
                                 "prompt_tokens_details": {"cached_tokens": 1500}}})
    return "".join("data: %s\n\n" % json.dumps(c) for c in chunks).encode() + b"data: [DONE]\n\n"


GEMINI_STREAM = b"".join(b"data: %s\r\n\r\n" % json.dumps(c).encode() for c in (
    {"candidates": [{"content": {"role": "model", "parts": [{"text": "Settled."}]}}],
     "usageMetadata": {"promptTokenCount": 300000, "candidatesTokenCount": 10}},
    {"candidates": [{"content": {"role": "model", "parts": [{"text": ""}]}, "finishReason": "STOP"}],
     "usageMetadata": {"promptTokenCount": 300000, "candidatesTokenCount": 50, "thoughtsTokenCount": 25,
                       "cachedContentTokenCount": 100000}}))


class Services:
    """The model services, as the gateway reaches them (httpx's MockTransport): each checks the hub's key."""

    def __init__(self):
        self.seen = []
        self.down = False
        self.keys = {"anthropic": "sk-ant-owner-1234", "openai": "sk-openai-owner-5678",
                     "gemini": "AIza-owner-9012", "opencode": "sk-oc-owner-3456"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("no route", request=request)
        body = json.loads(request.content) if request.content else None
        self.seen.append((request.method, str(request.url), dict(request.headers), body))
        host, path = request.url.host, request.url.path
        if host == "api.anthropic.com":
            if request.headers.get("x-api-key") != self.keys["anthropic"]:
                return httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error"}})
            if path == "/v1/models":
                return httpx.Response(200, json={"data": [{"id": "claude-opus-5-5"}, {"id": "claude-sonnet-5-5"}]})
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=CLAUDE_STREAM)
        if host == "api.openai.com":
            assert request.headers["authorization"] == "Bearer " + self.keys["openai"]
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=chat_stream(body["model"], (body.get("stream_options") or {}).get(
                                      "include_usage", False)))
        if host == "generativelanguage.googleapis.com":
            assert request.headers["x-goog-api-key"] == self.keys["gemini"] and request.url.params.get("key") is None
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=GEMINI_STREAM)
        if host == "opencode.ai":
            assert request.headers["authorization"] == "Bearer " + self.keys["opencode"]
            if path == "/inference/v1/models":
                return httpx.Response(200, json={"object": "list", "data": [
                    {"id": "deepseek-v4.1-flash"}, {"id": "glm-5.3"}, {"id": "kimi-k3"}]})
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=chat_stream(body["model"], body.get("stream_options", {}).get(
                                      "include_usage", False)))
        return httpx.Response(404, json={"error": {"message": "nothing here"}})


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "_read_models_dev", lambda: PRICES)
    hub = Hub(tmp_path / "hub")
    services = Services()
    client = TestClient(create_app(hub, hosts=["testserver"], backups=False,
                                   llm_transport=httpx.MockTransport(services)))
    r = client.post("/api/register", json={"name": "Ada", "email": "ada@example.org", "password": "correct horse",
                                           "consent": True, "installation": KELVIN})
    return hub, client, services, r.json()["access_token"]


def owner_keys(hub, services, which=("anthropic", "openai", "gemini", "opencode")):
    g = llm.Gateway(hub, catalog.Catalog(hub.home))
    for s in which:
        g.set_key(s, services.keys[s])
    return g


def wait_for_usage(hub, n):
    for _ in range(50):
        with hub.connect() as con:
            rows = [dict(r) for r in con.execute("SELECT * FROM llm_usage ORDER BY id")]
        if len(rows) >= n:
            return rows
        time.sleep(0.05)
    return rows


def test_keys_are_sealed_on_the_hub_and_never_shown(gateway, tmp_path):
    hub, client, services, token = gateway
    g = owner_keys(hub, services, ("anthropic",))
    assert (tmp_path / "hub" / "gateway.key").exists()
    raw = (tmp_path / "hub" / "hub.sqlite3").read_bytes()
    assert services.keys["anthropic"].encode() not in raw                     # sealed in the database
    assert g.key("anthropic") == services.keys["anthropic"]
    [claude] = [k for k in g.keys() if k["service"] == "anthropic"]
    assert claude["set"] and claude["hint"] == "1234" and "sealed" not in claude
    with pytest.raises(ValueError):
        g.set_key("anthropic", "short")
    (tmp_path / "hub" / "gateway.key").write_bytes(b"x" * 44)                 # the seal lost: no key, never a crash
    assert g.key("anthropic") is None
    g.remove_key("anthropic")
    assert not [k for k in g.keys() if k["set"]]


def test_a_kelvin_uses_claude_with_the_owners_key(gateway):
    hub, client, services, token = gateway
    owner_keys(hub, services, ("anthropic",))
    r = client.post("/llm/anthropic/v1/messages?beta=true", headers={
        "x-api-key": token, "anthropic-version": "2023-06-01", "anthropic-beta": "fallbacks-2026", "cookie": "a=b"},
        json={"model": "claude-opus-5-5", "max_tokens": 100, "stream": True,
              "messages": [{"role": "user", "content": "how does it look?"}]})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.content == CLAUDE_STREAM                                          # passed on unchanged
    method, url, headers, body = services.seen[-1]
    assert url == "https://api.anthropic.com/v1/messages?beta=true" and headers["x-api-key"] == services.keys["anthropic"]
    assert token not in json.dumps(headers) and "cookie" not in headers        # the person's token stays at the hub
    assert headers["anthropic-version"] == "2023-06-01" and headers["anthropic-beta"] == "fallbacks-2026"
    assert body["messages"][0]["content"] == "how does it look?"
    [row] = wait_for_usage(hub, 1)
    assert (row["service"], row["model"], row["input"], row["cache_read"], row["cache_write"], row["output"]) == (
        "anthropic", "claude-opus-5-5", 1000, 500, 100, 400)
    assert row["usd"] == pytest.approx((1000 * 4 + 500 * 0.2 + 100 * 5 + 400 * 20) / 1e6)   # models.dev's prices
    assert "how does it look" not in (hub.home / "hub.sqlite3").read_text("latin-1")   # never the text
    r = client.get("/api/llm", headers={"Authorization": "Bearer " + token})
    assert r.json()["services"] == [{"id": "anthropic", "name": "Claude", "models": None}]
    assert r.json()["spent"] == pytest.approx(row["usd"], abs=1e-4) and r.json()["budget"] == 20.0


def test_openai_style_answers_are_asked_for_their_token_counts(gateway):
    hub, client, services, token = gateway
    owner_keys(hub, services, ("opencode", "openai"))
    r = client.post("/llm/opencode/openai/v1/chat/completions", headers={"Authorization": "Bearer " + token},
                    json={"model": "deepseek-v4.1-flash", "stream": True, "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200 and b"Six points." in r.content
    assert services.seen[-1][1] == "https://opencode.ai/inference/openai/v1/chat/completions"
    assert services.seen[-1][3]["stream_options"] == {"include_usage": True}   # the hub asked for them
    client.post("/llm/openai/v1/chat/completions", headers={"Authorization": "Bearer " + token},
                json={"model": "gpt-6-sol", "stream": True, "stream_options": {"include_usage": True},
                      "messages": [{"role": "user", "content": "y"}]})
    rows = wait_for_usage(hub, 2)
    assert [(r["service"], r["input"], r["cache_read"], r["output"]) for r in rows] == [
        ("opencode", 500, 1500, 300), ("openai", 500, 1500, 300)]
    assert rows[0]["usd"] == pytest.approx((500 * 0.3 + 1500 * 0.006 + 300 * 1.2) / 1e6)
    r = client.get("/llm/opencode/v1/models", headers={"Authorization": "Bearer " + token})
    assert [m["id"] for m in r.json()["data"]] == ["deepseek-v4.1-flash", "glm-5.3", "kimi-k3"]


def test_gemini_through_the_hub_and_a_dearer_tier_past_its_size(gateway):
    hub, client, services, token = gateway
    owner_keys(hub, services, ("gemini",))
    r = client.post("/llm/gemini/v1beta/models/gemini-3.1-pro:streamGenerateContent?alt=sse&key=leak",
                    headers={"x-goog-api-key": token}, json={"contents": [{"role": "user", "parts": [{"text": "z"}]}]})
    assert r.status_code == 200 and b"Settled." in r.content
    assert services.seen[-1][1].endswith("/v1beta/models/gemini-3.1-pro:streamGenerateContent?alt=sse")
    [row] = wait_for_usage(hub, 1)
    assert (row["model"], row["input"], row["cache_read"], row["output"]) == ("gemini-3.1-pro", 200000, 100000, 75)
    assert row["usd"] == pytest.approx((200000 * 4 + 100000 * 4 + 75 * 18) / 1e6)   # past 200k: the dearer tier


def test_only_kelvins_calls_pass_and_only_for_a_signed_in_kelvin(gateway):
    hub, client, services, token = gateway
    auth = {"x-api-key": token}
    msg = {"model": "claude-opus-5-5", "messages": []}
    assert client.post("/llm/anthropic/v1/messages", json=msg).status_code == 401          # no hub sign-in
    assert client.post("/llm/anthropic/v1/messages", headers={"x-api-key": "khub_nope"}, json=msg).status_code == 401
    r = client.post("/llm/anthropic/v1/messages", headers=auth, json=msg)
    assert r.status_code == 503 and "no key for Claude" in r.json()["error"]["message"]    # the owner hasn't set one
    owner_keys(hub, services)
    for method, path in (("POST", "anthropic/v1/files"), ("GET", "anthropic/v1/organizations/me"),
                         ("POST", "openai/v1/images/generations"), ("POST", "openai/v1/fine_tuning/jobs"),
                         ("POST", "gemini/v1beta/cachedContents"), ("POST", "nowhere/v1/chat"),
                         ("DELETE", "anthropic/v1/messages")):
        r = client.request(method, "/llm/" + path, headers=auth, json={})
        assert r.status_code in (404, 405), (method, path)
    assert not services.seen                                                  # none of them reached a service
    hub.config["consent_version"] = "2099-01-01"
    r = client.post("/llm/anthropic/v1/messages", headers=auth, json=msg)
    assert r.status_code == 403 and "/consent" in r.json()["error"]["message"]   # new terms first


def test_the_owner_chooses_the_models(gateway):
    hub, client, services, token = gateway
    g = owner_keys(hub, services)
    g.set_offers("anthropic", "claude-sonnet-5-5")
    g.set_offers("opencode", " kimi-k3 , deepseek-v4.1-flash ")
    r = client.post("/llm/anthropic/v1/messages", headers={"x-api-key": token},
                    json={"model": "claude-opus-5-5", "messages": []})
    assert r.status_code == 403 and "doesn't offer claude-opus-5-5" in r.json()["error"]["message"]
    r = client.get("/llm/opencode/v1/models", headers={"Authorization": "Bearer " + token})
    assert [m["id"] for m in r.json()["data"]] == ["deepseek-v4.1-flash", "kimi-k3"]
    listed = client.get("/api/llm", headers={"Authorization": "Bearer " + token}).json()["services"]
    assert {s["id"]: s["models"] for s in listed}["opencode"] == ["kimi-k3", "deepseek-v4.1-flash"]
    g.set_offers("anthropic", "")
    assert g.offers("anthropic") is None


def test_budgets_stop_a_person_and_warn_them_at_80_percent(gateway):
    hub, client, services, token = gateway
    g = owner_keys(hub, services, ("anthropic",))
    g.set_budget("default", 0, "0.02")                                        # one answer costs $0.0126
    ask = {"model": "claude-opus-5-5", "stream": True, "messages": [{"role": "user", "content": "x"}]}
    assert client.post("/llm/anthropic/v1/messages", headers={"x-api-key": token}, json=ask).status_code == 200
    assert client.post("/llm/anthropic/v1/messages", headers={"x-api-key": token}, json=ask).status_code == 200
    wait_for_usage(hub, 2)
    r = client.post("/llm/anthropic/v1/messages", headers={"x-api-key": token}, json=ask)
    assert r.status_code == 402 and "budget on the Kelvin hub ($0.02) is used up" in r.json()["error"]["message"]
    assert r.json()["error"]["type"] == "budget_exceeded"
    for _ in range(50):
        mails = [p.read_text(encoding="utf-8") for p in (hub.home / "outbox").glob("*.txt")
                 if "model budget" in p.read_text(encoding="utf-8")]
        if mails:
            break
        time.sleep(0.05)
    assert len(mails) == 1 and "ada@example.org" in mails[0] and "$0.02" in mails[0]   # past 80%, once
    g.set_budget("user", 1, "5")                                              # the owner gives Ada more
    assert client.post("/llm/anthropic/v1/messages", headers={"x-api-key": token}, json=ask).status_code == 200
    assert g.usage_of(1)["own_budget"] and g.budget_of(1) == (5.0, True)
    g.set_budget("user", 1, "")
    assert g.budget_of(1) == (0.02, False)
    with pytest.raises(ValueError):
        g.set_budget("default", 0, "lots")
    g.set_budget("user", 1, "5")
    g.set_budget("lab", 7, "0.001")                                           # a lab's budget counts its members' use
    assert g.over_budget(1, 7) is None and g.lab_budget(7) == 0.001
    used = {"input": 1000, "output": 0, "cache_read": 0, "cache_write": 0, "searches": 0}
    assert g.record(hub.user(1), 7, "anthropic", "claude-new", used, 200) == pytest.approx(0.015)   # unpriced: $15/M
    g.catalog.refresh()
    assert g.record(hub.user(1), 7, "anthropic", "claude-opus-5-5", used, 200) == pytest.approx(0.004)
    assert "lab's model budget on the Kelvin hub this month ($0.00) is used up" in g.over_budget(1, 7)
    assert g.over_budget(1, None) is None


def test_too_many_at_once_or_a_minute_and_a_service_that_fails(gateway, monkeypatch):
    hub, client, services, token = gateway
    owner_keys(hub, services, ("anthropic",))
    monkeypatch.setitem(llm.LIMITS, "per_minute", 2)
    ask = {"model": "claude-opus-5-5", "stream": True, "messages": []}
    codes = [client.post("/llm/anthropic/v1/messages", headers={"x-api-key": token}, json=ask).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]
    monkeypatch.setitem(llm.LIMITS, "per_minute", 60)
    services.keys["anthropic"] = "sk-ant-rotated"                             # the owner's key stopped working
    r = client.post("/llm/anthropic/v1/messages", headers={"x-api-key": token}, json=ask)
    assert r.status_code == 502 and "refused the Kelvin hub's key" in r.json()["error"]["message"]
    services.down = True
    r = client.post("/llm/anthropic/v1/messages", headers={"x-api-key": token}, json=ask)
    assert r.status_code == 502 and "couldn't reach Claude" in r.json()["error"]["message"]
    g = llm.Gateway(hub, catalog.Catalog(hub.home))
    tickets = [g.admit(1) for _ in range(llm.LIMITS["at_once"])]
    assert all(tickets) and g.admit(1) is None                                # answers at once
    g.release(1, tickets[0])
    assert g.admit(1)


def test_the_owner_console_models_page(gateway):
    hub, client, services, token = gateway
    key = hub.ensure_operator_key().read_text(encoding="utf-8").split()[-1]
    page = client.post("/admin/signin", data={"key": key})
    assert 'href="/admin/models"' in page.text
    page = client.get("/admin/models")
    assert "Model services" in page.text and "no key" in page.text
    r = client.post("/admin/models/key", data={"csrf": csrf(page), "back": "models", "service": "anthropic",
                                               "key": services.keys["anthropic"]})
    assert "Claude&#x27;s key is set." in r.text and "…1234" in r.text and services.keys["anthropic"] not in r.text
    r = client.post("/admin/models/budget", data={"csrf": csrf(page), "back": "models", "scope": "default", "ref": "0",
                                                  "usd": "12.5"})
    assert "Budget saved." in r.text and 'value="12.5"' in r.text
    client.post("/llm/anthropic/v1/messages", headers={"x-api-key": token},
                json={"model": "claude-opus-5-5", "stream": True, "messages": []})
    wait_for_usage(hub, 1)
    page = client.get("/admin/models")
    assert "ada@example.org" in page.text and "claude-opus-5-5" in page.text and "$0.01" in page.text
    csv_text = client.get("/admin/models/usage.csv").text
    assert csv_text.splitlines()[0].startswith("month,user_id,name") and "ada@example.org" in csv_text
    person = client.get("/admin/users/1")
    assert "of $12.50 this month" in person.text
    r = client.post("/admin/users/1/budget", data={"csrf": csrf(person), "back": "person", "usd": "40"})
    assert "of $40.00 this month (their own budget)" in r.text
    r = client.post("/admin/models/key/remove", data={"csrf": csrf(page), "back": "models", "service": "anthropic"})
    assert "no longer offered" in r.text
    client.cookies.clear()
    assert client.get("/admin/models", follow_redirects=False).status_code == 303   # the operator's only


def test_a_person_sees_their_month_on_the_account_page(gateway):
    hub, client, services, token = gateway
    owner_keys(hub, services, ("anthropic",))
    client.post("/signin", data={"email": "ada@example.org", "password": "correct horse"})
    page = client.get("/account")
    assert "Models through Kelvin" in page.text and "of your $20.00 this month (Claude)" in page.text


def test_the_meter_reads_whole_answers_too():
    m = llm.Meter("openai", False)
    m.feed(json.dumps({"output": [], "usage": {"input_tokens": 70, "output_tokens": 9,
                                               "input_tokens_details": {"cached_tokens": 20}}}).encode())
    m.close()
    assert (m.used["input"], m.used["cache_read"], m.used["output"]) == (50, 20, 9)   # Responses API (web search)
    m = llm.Meter("anthropic", False)
    m.feed(json.dumps({"type": "message", "usage": {"input_tokens": 12, "output_tokens": 3,
                                                    "server_tool_use": {"web_search_requests": 2}}}).encode())
    m.close()
    assert m.used["searches"] == 2 and llm.cost({"input": 4, "output": 20}, m.used) == pytest.approx(
        (12 * 4 + 3 * 20) / 1e6 + 0.02)
    assert re.fullmatch(r"\d{4}-\d{2}", llm.month_of()) and llm.renews("2026-12") == "January 1"
