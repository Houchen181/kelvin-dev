"""Sign in with ChatGPT: PKCE sign-in, ID-token checks, token renewal and sign-out, and the ChatGPT-plan provider."""
import asyncio
import base64
import hashlib
import json
import time
import urllib.parse
from types import SimpleNamespace

import pytest

pytest.importorskip("starlette")
pytest.importorskip("openai")
crypto = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding  # noqa: E402

from labharness.web import chatgpt  # noqa: E402
from labharness.web.chatgpt import NotSignedIn, SignIn, SignInError, verify_id_token  # noqa: E402
from labharness.web.providers.chatgpt_provider import ChatGPTProvider, to_input, tool_list  # noqa: E402

KEY = crypto.generate_private_key(public_exponent=65537, key_size=2048)


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwks():
    nums = KEY.public_key().public_numbers()
    to = lambda i: b64(i.to_bytes((i.bit_length() + 7) // 8, "big"))  # noqa: E731
    return {"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256", "n": to(nums.n), "e": to(nums.e)}]}


def id_token(claims, alg="RS256", kid="k1", key=KEY):
    head = b64(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    body = b64(json.dumps(claims).encode())
    sig = key.sign(("%s.%s" % (head, body)).encode(), padding.PKCS1v15(), hashes.SHA256())
    return "%s.%s.%s" % (head, body, b64(sig))


class OpenAIAuth:
    """Answers like auth.openai.com."""

    def __init__(self):
        self.calls = []
        self.client_id = "oaiapp_test123"
        self.scope = chatgpt.SCOPE
        self.nonce = None
        self.refresh_answer = None
        self.n = 0

    def __call__(self, method, url, form=None):
        self.calls.append((method, url, dict(form or {})))
        if url == chatgpt.JWKS:
            return 200, jwks()
        if url == chatgpt.TOKEN and form["grant_type"] == "authorization_code":
            claims = {"iss": chatgpt.ISSUER, "aud": self.client_id, "sub": "user-1", "email": "ana@example.edu",
                      "nonce": self.nonce, "exp": time.time() + 3600, "iat": time.time()}
            return 200, {"access_token": "at-1", "refresh_token": "rt-1", "id_token": id_token(claims),
                         "token_type": "Bearer", "expires_in": 3600, "scope": self.scope}
        if url == chatgpt.TOKEN and form["grant_type"] == "refresh_token":
            if self.refresh_answer:
                return self.refresh_answer
            self.n += 1
            return 200, {"access_token": "at-%d" % (self.n + 1), "refresh_token": "rt-%d" % (self.n + 1), "expires_in": 3600}
        if url == chatgpt.REVOKE:
            return 200, {}
        return 404, {"error": "not_found"}


def sign_in(tmp_path, auth=None):
    auth = auth or OpenAIAuth()
    s = SignIn(tmp_path, http=auth)
    url = s.begin("http://127.0.0.1:8770/auth/callback")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    auth.nonce = q["nonce"]
    return s, auth, q


def test_the_sign_in_request(tmp_path):
    s, auth, q = sign_in(tmp_path)
    assert q["client_id"] == "dynamic_agent_client" and q["agent_name_hint"] == "Kelvin"
    assert q["scope"] == "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
    assert q["resource"] == "https://api.openai.com/v1" and q["response_type"] == "code"
    assert q["redirect_uri"] == "http://127.0.0.1:8770/auth/callback" and q["code_challenge_method"] == "S256"
    assert q["ext_agent_host_id"].startswith("urn:uuid:") and "id_token_hint" not in q
    verifier = s.pending[q["state"]]["verifier"]
    assert q["code_challenge"] == b64(hashlib.sha256(verifier.encode()).digest())
    assert SignIn(tmp_path).host_id() == q["ext_agent_host_id"]               # one id for this installation
    with pytest.raises(SignInError, match="not accept localhost"):
        s.begin("http://localhost:8770/auth/callback")


def test_signing_in_renewing_and_signing_out(tmp_path):
    s, auth, q = sign_in(tmp_path)
    st = s.finish({"state": q["state"], "code": "c0de", "client_id": auth.client_id, "scope": chatgpt.SCOPE})
    assert st == {"signed_in": True, "email": "ana@example.edu", "plan": True, "welcomed": False,
                  "manage_usage": "https://chatgpt.com/settings/usage"}
    exchange = auth.calls[0][2]
    assert exchange["client_id"] == "oaiapp_test123" and exchange["code_verifier"] and exchange["resource"] == chatgpt.RESOURCE
    saved = json.loads((tmp_path / "chatgpt.json").read_text(encoding="utf-8"))
    assert saved["client_id"] == "oaiapp_test123" and saved["refresh_token"] == "rt-1" and saved["subject"] == "user-1"
    with pytest.raises(SignInError, match="expired or was already used"):
        s.finish({"state": q["state"], "code": "c0de"})                         # each attempt works once

    assert s.access_token() == "at-1"                                            # still fresh
    s.data["expires_at"] = time.time() + 30
    assert s.access_token() == "at-2" and s.data["refresh_token"] == "rt-2"      # renewed, refresh token rotated
    refresh = auth.calls[-1][2]
    assert refresh == {"grant_type": "refresh_token", "client_id": "oaiapp_test123", "refresh_token": "rt-1",
                       "resource": chatgpt.RESOURCE}
    s.data["expires_at"] = 0
    auth.refresh_answer = (503, "busy")
    with pytest.raises(SignInError, match="try again"):
        s.access_token()
    assert s.status()["signed_in"]                                               # a hiccup does not sign you out
    auth.refresh_answer = (400, {"error": "invalid_grant"})
    with pytest.raises(NotSignedIn):
        s.access_token()
    assert not s.status()["signed_in"]

    s, auth, q = sign_in(tmp_path, auth)
    s.finish({"state": q["state"], "code": "c", "client_id": auth.client_id})
    assert s.sign_out() is True and auth.calls[-1][1] == chatgpt.REVOKE and auth.calls[-1][2]["token_type_hint"] == "refresh_token"
    assert not s.status()["signed_in"] and s.data["client_id"] == "oaiapp_test123"
    url = s.begin("http://127.0.0.1:8770/auth/callback")
    again = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert again["client_id"] == "oaiapp_test123" and "agent_name_hint" not in again and again["id_token_hint"]


def test_plan_usage_needs_its_permission(tmp_path):
    auth = OpenAIAuth()
    auth.scope = "openid profile email offline_access"
    s, auth, q = sign_in(tmp_path, auth)
    assert s.finish({"state": q["state"], "code": "c", "client_id": auth.client_id})["plan"] is False


def test_bad_callbacks_and_id_tokens(tmp_path):
    s, auth, q = sign_in(tmp_path)
    with pytest.raises(SignInError, match="cancelled"):
        s.finish({"state": q["state"], "error": "access_denied"})
    s, auth, q = sign_in(tmp_path, auth)
    with pytest.raises(SignInError, match="unexpected app registration"):
        s.finish({"state": q["state"], "code": "c", "client_id": "dynamic_agent_client"})
    good = {"iss": chatgpt.ISSUER, "aud": "oaiapp_x", "nonce": "n1", "exp": time.time() + 600}
    keys = jwks()
    assert verify_id_token(id_token(good), "oaiapp_x", "n1", keys)["aud"] == "oaiapp_x"
    for claims, client, nonce, match in ((dict(good, nonce="other"), "oaiapp_x", "n1", "this sign-in"),
                                         (good, "oaiapp_y", "n1", "another app"),
                                         (dict(good, iss="https://evil.example"), "oaiapp_x", "n1", "not OpenAI"),
                                         (dict(good, exp=time.time() - 3600), "oaiapp_x", "n1", "expired")):
        with pytest.raises(SignInError, match=match):
            verify_id_token(id_token(claims), client, nonce, keys)
    other = crypto.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(SignInError, match="signature"):
        verify_id_token(id_token(good, key=other), "oaiapp_x", "n1", keys)
    head, body, sig = id_token(good).split(".")
    unsigned = b64(json.dumps({"alg": "none"}).encode()) + "." + body + "."
    with pytest.raises(SignInError, match="RS256"):
        verify_id_token(unsigned, "oaiapp_x", "n1", keys)


# ------------------------------------------------------------------ the provider

class Events:
    def __init__(self, events):
        self.events = events

    def __aiter__(self):
        async def gen():
            for e in self.events:
                yield e
        return gen()


def done(output, model="gpt-x"):
    return SimpleNamespace(type="response.completed", response=SimpleNamespace(output=output, model=model))


class FakeClient:
    def __init__(self, runs, refuse_namespace=False):
        self.runs = list(runs)
        self.sent = []
        self.refuse_namespace = refuse_namespace
        self.responses = SimpleNamespace(create=self.create)

    async def create(self, **body):
        self.sent.append(body)
        if self.refuse_namespace and any(t.get("type") == "namespace" for t in body.get("tools", [])):
            import httpx
            import openai
            raise openai.BadRequestError("Unsupported tool type: namespace", body=None,
                                         response=httpx.Response(400, request=httpx.Request("POST", "https://api.openai.com/v1/responses")))
        return Events(self.runs.pop(0))


def signed_in_provider(tmp_path, runs, **kw):
    s, auth, q = sign_in(tmp_path)
    s.finish({"state": q["state"], "code": "c", "client_id": auth.client_id})
    client = FakeClient(runs, **kw)
    p = ChatGPTProvider(s, client_factory=lambda token: client,
                        http_get=lambda url, token: {"models": [
                            {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol", "visibility": "list"},
                            {"slug": "internal", "display_name": "Hidden", "visibility": "hide"}]})
    return p, client


TOOLS = [{"name": "lab_status", "description": "Status", "schema": {"type": "object", "properties": {}}}]


def test_chatgpt_models_and_answers(tmp_path):
    call = {"type": "function_call", "call_id": "call_1", "name": "kelvin.lab_status", "arguments": "{}",
            "namespace": "kelvin", "id": "fc_1", "status": "completed"}
    p, client = signed_in_provider(tmp_path, [
        [SimpleNamespace(type="response.output_text.delta", delta="Checking."),
         done([{"type": "message", "content": [{"type": "output_text", "text": "Checking."}]}, call,
               {"type": "reasoning", "summary": []}])],
        [done([{"type": "message", "content": [{"type": "output_text", "text": "All idle."}]}])]])
    assert p.available() and [(m.id, m.label) for m in asyncio.run(p.models())] == [("gpt-6.1-sol", "GPT-6.1 Sol")]
    seen = []

    async def emit(e):
        seen.append(e)
    first = asyncio.run(p.stream("gpt-6.1-sol", "You are Kelvin.", [{"role": "user", "text": "how is the lab?"}], TOOLS, emit))
    body = client.sent[0]
    assert body["store"] is False and body["stream"] is True and body["instructions"] == "You are Kelvin."
    assert not {"temperature", "max_output_tokens", "previous_response_id"} & set(body)   # refused by this route
    assert body["tools"] == [{"type": "namespace", "name": "kelvin", "description": "Kelvin's lab tools", "tools": [
        {"type": "function", "name": "lab_status", "description": "Status", "parameters": {"type": "object", "properties": {}}}]}]
    assert seen == [{"type": "text", "delta": "Checking."}]
    assert first.stop_reason == "tool_calls" and first.blocks[1] == {"type": "tool_call", "id": "call_1", "name": "lab_status", "input": {}}
    history = [{"role": "user", "text": "how is the lab?"},
               {"role": "assistant", "provider": "chatgpt", "model": "gpt-6.1-sol", "blocks": first.blocks, "raw": first.raw},
               {"role": "tool", "results": [{"id": "call_1", "name": "lab_status", "content": "idle", "is_error": False}]}]
    second = asyncio.run(p.stream("gpt-6.1-sol", "You are Kelvin.", history, TOOLS, emit))
    sent = client.sent[1]["input"]
    assert sent[1] == {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Checking."}]}
    assert sent[2] == {"type": "function_call", "call_id": "call_1", "name": "kelvin.lab_status", "arguments": "{}",
                       "namespace": "kelvin"}                                    # replayed without server ids
    assert sent[3] == {"type": "function_call_output", "call_id": "call_1", "output": "idle"}
    assert not any(i.get("type") == "reasoning" for i in sent) and second.blocks == [{"type": "text", "text": "All idle."}]


def test_chatgpt_falls_back_to_plain_tools_and_reports_limits(tmp_path):
    p, client = signed_in_provider(tmp_path, [
        [done([{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}])],
        [SimpleNamespace(type="response.failed", response=SimpleNamespace(
            error=SimpleNamespace(code="subscription_sharing_usage_limit_exceeded", message="limit")))]],
        refuse_namespace=True)

    async def emit(e):
        return None
    asyncio.run(p.stream("m", "s", [{"role": "user", "text": "hi"}], TOOLS, emit))
    assert client.sent[-1]["tools"][0]["type"] == "function" and p.tool_style == "function"
    with pytest.raises(RuntimeError, match="usage limit is reached.*chatgpt.com/settings/usage"):
        asyncio.run(p.stream("m", "s", [{"role": "user", "text": "hi"}], TOOLS, emit))
    assert "tools" not in to_input([{"role": "user", "text": "x"}], "chatgpt")[0] and tool_list(TOOLS, "function")[0]["name"] == "lab_status"


def test_chatgpt_in_the_portal(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from labharness import build_virtual_lab, demo_envelope
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    import labharness.web.app as web_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    opened = []
    monkeypatch.setattr(web_app, "open_url", opened.append)

    async def no_models(self):
        return []
    monkeypatch.setattr(ChatGPTProvider, "models", no_models)
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    settings = Settings(tmp_path / "settings.json")
    guard = web_app.create_app(runtime, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"], demo=True)
    auth = OpenAIAuth()
    guard.app.state.signin.http = auth
    headers = {"X-Kelvin-Token": settings.access_key}
    with TestClient(guard) as client:
        assert client.get("/api/chatgpt", headers=headers).json()["signed_in"] is False
        url = client.post("/api/chatgpt/signin", json={}, headers=headers).json()["url"]
        assert opened == [url] and url.startswith("https://auth.openai.com/api/accounts/authorize?")
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        auth.nonce = q["nonce"]
        page = client.get("/auth/callback", params={"state": q["state"], "code": "c", "client_id": auth.client_id})
        assert page.status_code == 200 and "signed in to Kelvin with ChatGPT" in page.text and "ana@example.edu" in page.text
        assert "chatgpt" in client.app.app.state.agent.providers
        assert client.get("/auth/callback", params={"state": "nope"}).status_code == 400
        assert client.post("/api/chatgpt/welcomed", json={}, headers=headers).json()["welcomed"] is True
        assert "at-1" not in client.get("/api/chatgpt", headers=headers).text           # tokens never go to the browser
        guard.remote.port = 8771
        phone = TestClient(guard, base_url="http://192.168.1.50:8771")
        assert phone.get("/auth/callback", params={"state": "x"}).status_code == 403   # only on this computer
        guard.remote.port = None
        out = client.post("/api/chatgpt/signout", json={}, headers=headers).json()
        assert out["signed_in"] is False and out["revoked"] is True
        assert "chatgpt" not in client.app.app.state.agent.providers
