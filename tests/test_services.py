"""Other model services (OpenRouter, OpenCode, DeepSeek...): set up, listed, kept apart from local models; a person's
own OpenCode sending its session with every request, the models named for a service checked against its list, and a
service's refusal in its own words."""
import asyncio
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace

import pytest

pytest.importorskip("starlette")
pytest.importorskip("openai")

from labharness.web import rules  # noqa: E402
from labharness.web.providers.openai_provider import MAX_LISTED, OpenAIProvider  # noqa: E402
from labharness.web.services import check_url, make, not_offered, say_not_offered  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402


def test_service_entries():
    s = make({"preset": "openrouter", "api_key": "sk-or-1"}, [])
    assert s == {"id": "openrouter", "preset": "openrouter", "name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
                 "models": "", "api_key": "sk-or-1"}
    assert make({"preset": "opencode", "api_key": "k"}, [])["base_url"] == "https://opencode.ai/inference/openai/v1"
    c = make({"preset": "custom", "name": "Lab gateway", "base_url": "http://192.168.1.40:4000/v1/",
              "models": " qwen3 , llama4 ,"}, [])
    assert (c["id"], c["base_url"], c["models"], c["api_key"]) == ("lab-gateway", "http://192.168.1.40:4000/v1", "qwen3,llama4", "")
    with pytest.raises(ValueError, match="needs an API key"):
        make({"preset": "deepseek"}, [])
    with pytest.raises(ValueError, match="give the service a name"):
        make({"preset": "custom", "base_url": "https://x.example/v1", "api_key": "k"}, [])
    with pytest.raises(ValueError, match="already a service"):
        make({"preset": "custom", "name": "OpenAI", "base_url": "https://x.example/v1", "api_key": "k"}, [])
    with pytest.raises(ValueError, match="already a service"):
        make({"preset": "groq", "api_key": "k"}, ["groq"])
    with pytest.raises(ValueError, match="use https"):
        check_url("http://api.example.com/v1")             # a key must not cross the internet unencrypted
    assert check_url("http://localhost:11434/v1") == "http://localhost:11434/v1"
    with pytest.raises(ValueError):
        check_url("ftp://x")


def test_settings_keep_keys_out_of_sight(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "settings.json")
    settings.add_service({"preset": "openrouter", "api_key": "sk-or-secret"})
    monkeypatch.setenv("GROQ_API_KEY", "gsk-env")
    settings.add_service({"preset": "groq", "api_key": "gsk-form"})
    settings._user["services"][1]["api_key"] = ""      # as if only the environment had it
    views = settings.service_views()
    assert [(v["id"], v["key"]) for v in views] == [("openrouter", "settings"), ("groq", "environment")]
    assert "sk-or-secret" not in json.dumps(views) and settings.service_key(settings.services[1]) == "gsk-env"
    settings.remove_service("groq")
    with pytest.raises(KeyError):
        settings.remove_service("groq")


class Listing:
    def __init__(self, items):
        self.items = items

    def __aiter__(self):
        async def gen():
            for m in self.items:
                yield m
        return gen()


def provider_with(models, **kw):
    p = OpenAIProvider(kw.pop("name", "openrouter"), "OpenRouter", "k", base_url="https://openrouter.ai/api/v1", **kw)
    p.client = SimpleNamespace(models=SimpleNamespace(list=lambda: Listing(models)))
    return p


def test_gateway_model_lists():
    m = lambda i, created, tools=True: SimpleNamespace(id=i, created=created,  # noqa: E731
                                                       supported_parameters=["tools", "temperature"] if tools else ["temperature"])
    p = provider_with([m("vendor/old", 1), m("vendor/no-tools", 3, tools=False), m("vendor/new", 2)], tools_only=True)
    assert [x.id for x in asyncio.run(p.models())] == ["vendor/new", "vendor/old"]   # newest first, tools only
    many = provider_with([m("v/m%03d" % i, i) for i in range(200)], tools_only=True)
    assert len(asyncio.run(many.models())) == MAX_LISTED
    fetched = []

    def fetch(url, key, headers):
        fetched.append((url, key, headers))
        return {"object": "list", "data": [{"id": "big-pickle", "created": 5}, {"id": "claude-opus-4-6", "created": 5},
                                           {"id": "gpt-5", "created": 5}, {"id": "qwen3-coder", "created": 6}]}
    oc = OpenAIProvider("opencode", "OpenCode", "oc-key", base_url="https://opencode.ai/inference/openai/v1",
                        models_url="https://opencode.ai/inference/v1/models", exclude=["claude", "gpt"], fetch=fetch)
    assert [x.id for x in asyncio.run(oc.models())] == ["qwen3-coder", "big-pickle"]
    assert [(url, key) for url, key, _ in fetched] == [("https://opencode.ai/inference/v1/models", "oc-key")]
    assert fetched[0][2]["x-opencode-session"].startswith("kelvin-")   # OpenCode's list too, with its session
    named = OpenAIProvider("deepseek", "DeepSeek", "k", base_url="https://api.deepseek.com/v1", models=["deepseek-chat"])
    assert [x.id for x in asyncio.run(named.models())] == ["deepseek-chat"]       # named models are used as they are


def test_no_empty_tool_list_is_sent():
    sent = []

    class Chunks:
        def __aiter__(self):
            async def gen():
                yield SimpleNamespace(model="m", choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="verdict: go", tool_calls=None), finish_reason="stop")])
            return gen()

    async def create(**kw):
        sent.append(kw)
        return Chunks()

    p = OpenAIProvider("openrouter", "OpenRouter", "k", base_url="https://openrouter.ai/api/v1")
    p.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    async def quiet(e):
        return None
    result = asyncio.run(p.stream("m", "review", [{"role": "user", "text": "plan"}], [], quiet))
    assert result.blocks == [{"type": "text", "text": "verdict: go"}] and "tools" not in sent[0]
    asyncio.run(p.stream("m", "s", [{"role": "user", "text": "x"}],
                         [{"name": "lab_status", "description": "d", "schema": {"type": "object"}}], quiet))
    assert sent[1]["tools"][0]["function"]["name"] == "lab_status"


def test_services_in_the_portal(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from simlab import build_virtual_lab, demo_envelope
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.web.app import create_app
    from labharness.web.providers.base import ModelInfo
    from labharness.web.store import ChatStore

    async def listed(self):
        return [ModelInfo("anthropic/claude-sonnet-4.5", "anthropic/claude-sonnet-4.5", self.name)] if self._key else []
    monkeypatch.setattr(OpenAIProvider, "models", listed)
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    settings = Settings(tmp_path / "settings.json")
    app = create_app(runtime, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"])
    headers = {"X-Kelvin-Token": settings.access_key}
    with TestClient(app) as client:
        r = client.get("/api/services", headers=headers).json()
        assert r["services"] == [] and {p["id"] for p in r["presets"]} >= {"openrouter", "opencode", "custom"}
        bad = client.post("/api/services", json={"preset": "openrouter"}, headers=headers)
        assert bad.status_code == 400 and "API key" in bad.json()["error"]
        r = client.post("/api/services", json={"preset": "openrouter", "api_key": "sk-or-v1-secret"}, headers=headers)
        assert r.status_code == 200 and "sk-or-v1-secret" not in r.text
        group = next(g for g in r.json()["models"]["groups"] if g["provider"] == "openrouter")
        assert group["label"] == "OpenRouter" and group["models"][0]["key"] == "openrouter:anthropic/claude-sonnet-4.5"
        assert "sk-or-v1-secret" not in client.get("/api/settings", headers=headers).text
        assert client.post("/api/services/openrouter/test", json={}, headers=headers).json() == \
            {"ok": True, "message": "1 models that Kelvin can use"}

        project = client.post("/api/projects", json={"name": "Shared work"}, headers=headers).json()
        ok = client.patch("/api/projects/" + project["id"], json={"rules": {"providers": ["openrouter", "local"]}},
                          headers=headers)
        assert ok.json()["rules"]["providers"] == ["local", "openrouter"]
        assert client.patch("/api/projects/" + project["id"], json={"rules": {"providers": ["skynet"]}},
                            headers=headers).status_code == 400
        assert rules.allows({"providers": ["local"]}, "openrouter:anthropic/claude-sonnet-4.5") is False   # a cloud service is not local

        r = client.delete("/api/services/openrouter", headers=headers).json()
        assert r["services"] == [] and not any(g["provider"] == "openrouter" for g in r["models"]["groups"])
        assert client.delete("/api/services/openrouter", headers=headers).status_code == 404


# --- OpenCode Go, as it answers -----------------------------------------------------------------------------------

GO_MODELS = ["deepseek-v4.1-flash", "deepseek-v4-pro", "glm-5.3", "kimi-k3", "qwen3.8-max"]   # its list, in part
MISSING = "Request is missing x-opencode-session and cannot be routed efficiently."


class OpenCodeGo:
    """OpenCode Go as it answers, with no network: a key not its own is refused (401), and so is a request without
    x-opencode-session (400 MissingSessionID) and a model it doesn't offer (400 or the status given, saying so in the
    shape given, a web page as text); it lists its models (lists: False, its list out of reach) and answers the rest.
    The session each request carried is kept."""

    def __init__(self, refusal=None, status=400):
        self.refusal = refusal or {"type": "ModelError", "message": "Model is unavailable"}
        self.status = status
        self.lists = True
        self.sessions = []

    def answer(self, path, headers, body):
        self.sessions.append(headers.get("x-opencode-session"))
        if headers.get("authorization") != "Bearer go-secret-key":
            return 401, {"type": "AuthError", "message": "Invalid API key."}
        if not headers.get("x-opencode-session"):
            return 400, {"type": "MissingSessionID", "message": MISSING}
        if path.endswith("/models"):
            return (200, {"object": "list", "data": [{"id": m, "created": 1} for m in GO_MODELS]}) if self.lists \
                else (404, {"type": "NotFound", "message": "Not found"})
        if body.get("model") not in GO_MODELS:
            return self.status, self.refusal
        return 200, None

    def client(self):
        """A connection's client talking to it: OpenAI's SDK as Kelvin uses it, its HTTP faked."""
        import openai
        httpx2 = pytest.importorskip("httpx2")

        def handle(request):
            status, said = self.answer(request.url.path, request.headers, json.loads(request.content or b"{}"))
            if isinstance(said, str):
                return httpx2.Response(status, html=said)
            if said is not None:
                return httpx2.Response(status, json=said)
            chunk = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "glm-5.3", "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": "Six points."}, "finish_reason": "stop"}]}
            return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                                   content=("data: %s\n\ndata: [DONE]\n\n" % json.dumps(chunk)).encode())
        return openai.AsyncOpenAI(api_key="go-secret-key", base_url="https://opencode.ai/inference/go/openai/v1",
                                  max_retries=0, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)))

    def urlopen(self, request, timeout=20):
        """Its list of models as Kelvin reads it (urllib); any other address is out of reach."""
        if not request.full_url.startswith("https://opencode.ai/inference/go/"):
            raise urllib.error.URLError("offline")
        status, said = self.answer(urllib.parse.urlparse(request.full_url).path,
                                   {k.lower(): v for k, v in request.header_items()}, {})
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "refused", {}, io.BytesIO(json.dumps(said).encode()))
        return io.BytesIO(json.dumps(said).encode())


async def quiet(event):
    return None


def test_a_persons_own_opencode_sends_its_session_with_every_request(tmp_path, monkeypatch):
    """OpenCode Go refuses a request without x-opencode-session (what the person saw). Their own OpenCode and OpenCode
    Go send one with every request, as the hub's connections do: in a chat its own, the hub's for that chat, in its
    answers and its summary; outside any chat (a project's opening, a card, the list of models) the connection's own.
    Other services send none."""
    import openai
    from labharness.web import compaction, review
    from labharness.web.agent import describe_error
    from labharness.web.app import make_providers
    from labharness.web.hubmodels import hub_providers
    from labharness.web.providers.base import SESSION
    settings = Settings(tmp_path / "settings.json")
    by_address = {"preset": "custom", "name": "Go by address", "api_key": "k",
                  "base_url": "https://opencode.ai/inference/go/openai/v1"}
    for entry in ({"preset": "opencode-go", "api_key": "go-secret-key"}, {"preset": "opencode", "api_key": "zen-key"},
                  by_address, {"preset": "openrouter", "api_key": "sk-or-1"}):
        settings.add_service(entry)
    providers = make_providers(settings)
    go, service = providers["opencode-go"], OpenCodeGo()
    go.client = service.client()
    monkeypatch.setattr(urllib.request, "urlopen", service.urlopen)
    hub_go = hub_providers(("https://hub.test", "khub_s"))["hub-opencode-go"]

    async def in_chat(chat_id):
        held = SESSION.set(chat_id)
        try:
            answer = await go.stream("glm-5.3", "Be brief.", [{"role": "user", "text": "hi"}], [], quiet)
            day = {"messages": [{"role": "user", "text": "set the gate"},
                                {"role": "assistant", "blocks": [{"type": "text", "text": "Set."}]}] * 3}
            assert await compaction.compact(day, go, "glm-5.3") == "Six points."   # a long day's summary
            return answer, hub_go.extra_headers()
        finally:
            SESSION.reset(held)
    answer, hub_said = asyncio.run(in_chat("chat-1"))
    assert answer.blocks == [{"type": "text", "text": "Six points."}]
    assert service.sessions == [hub_said["x-opencode-session"]] * 2   # the chat's own, as the hub's OpenCode Go sends
    asyncio.run(in_chat("chat-2"))
    assert service.sessions[2] != service.sessions[0] and "chat-2" not in service.sessions[2]   # each chat its own

    del service.sessions[:]
    assert asyncio.run(review.ask(go, "glm-5.3", "Write the opening.", "The project's scans.")) == "Six points."
    assert {m.id for m in asyncio.run(go.models())} == set(GO_MODELS)              # its list, read with it too
    outside = service.sessions
    assert len(outside) == 2 and outside[0] == outside[1] and outside[0].startswith("kelvin-")   # the connection's
    assert outside[0] != hub_said["x-opencode-session"] and hub_go.extra_headers() == {}   # the hub sends the person's
    assert providers["opencode"].extra_headers()["x-opencode-session"].startswith("kelvin-")   # OpenCode Zen too
    assert providers["go-by-address"].extra_headers()                       # OpenCode's address under another name
    assert providers["openrouter"].extra_headers() == {}                    # other services: none

    plain = OpenAIProvider("gateway", "OpenCode Go", "k", base_url="https://gateway.example/v1")   # sends none
    plain.client = service.client()
    with pytest.raises(openai.BadRequestError) as refused:
        asyncio.run(plain.stream("glm-5.3", "Be brief.", [{"role": "user", "text": "hi"}], [], quiet))
    assert describe_error(plain, refused.value) == "OpenCode Go: " + MISSING


@pytest.fixture
def parts(tmp_path):   # as test_web's
    pytest.importorskip("mcp")
    from simlab import build_virtual_lab, demo_envelope
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.web.store import ChatStore
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    return runtime, Settings(tmp_path / "settings.json"), ChatStore(tmp_path / "chats")


UNAVAILABLE = "OpenCode Go: Model is unavailable"


@pytest.mark.parametrize("refusal, status, said", [
    ({"type": "ModelError", "message": "Model is unavailable"}, 400, UNAVAILABLE),
    ({"error": {"type": "ModelError", "message": "Model is unavailable"}}, 400, UNAVAILABLE),
    ({"error": "Model is unavailable"}, 400, UNAVAILABLE),
    ({"detail": "Model is unavailable"}, 400, UNAVAILABLE),                     # FastAPI's, which gateways are built on
    ({"detail": [{"loc": ["body", "model"], "msg": "Model is unavailable", "type": "value_error"}]}, 422, UNAVAILABLE),
    ({"type": "ModelError", "message": ""}, 400, "OpenCode Go refused the request (error 400)."),   # no words: its code
    ("<html><body><h1>400 Bad Request</h1></body></html>", 400, "OpenCode Go refused the request (error 400).")])
def test_a_services_refusal_reaches_the_chat_in_its_own_words(parts, refusal, status, said):
    """A model OpenCode Go doesn't offer: the chat says what OpenCode said, after the service's name, on one line,
    whatever the shape it said it in, or its error's number when it said nothing; no stack, no "Error code: 400",
    never the key. A second opinion asked of it says the same."""
    from labharness.web.app import make_providers
    from test_web import DONE, Scripted, client_for, stream_turn
    parts[1].add_service({"preset": "opencode-go", "api_key": "go-secret-key"})
    go, service = make_providers(parts[1])["opencode-go"], OpenCodeGo(refusal, status)
    go.client = service.client()
    client, headers = client_for(parts, {"opencode-go": go, "scripted": Scripted([DONE])})
    with client:
        chat = client.post("/api/chats", json={"model": "opencode-go:glm-5.3"}, headers=headers).json()
        events = stream_turn(client, headers, chat["id"], "Plot the gate sweep.", "opencode-go:deepseek-flash")
        assert [e["message"] for e in events if e["type"] == "error"] == [said]
        assert events[-1] == {"type": "turn_end", "reason": "error"}
        shown = json.dumps(events)
        assert "go-secret-key" not in shown and "Traceback" not in shown and "Error code" not in shown
        events = stream_turn(client, headers, chat["id"], "With one it offers, then.", "opencode-go:glm-5.3")
        assert "".join(e["delta"] for e in events if e["type"] == "text") == "Six points."   # its session: answered
        stream_turn(client, headers, chat["id"], "And the gate?", "scripted:m1")   # an answer for a second opinion
        r = client.post("/api/chats/%s/review" % chat["id"], json={"model": "opencode-go:deepseek-flash"},
                        headers=headers)
        assert r.status_code == 502
        assert r.json()["error"] == "The reviewer could not answer: " + said


def test_the_models_named_for_a_service_are_checked_against_its_list(parts, monkeypatch):
    """Named models a service's own list lacks: Settings says so with the nearest it has, when the service is added
    and on Test, and keeps the names as typed (a service may serve a name it doesn't list). When its list can't be
    read, the service is added as before and nothing is said; when the service turns the key down asking for it, Add
    and Test say so, never "Works"."""
    from starlette.testclient import TestClient
    from labharness.web.app import create_app
    assert not_offered(["deepseek-flash", "glm-5.3", "GLM-5.3", "gpt-9"], GO_MODELS) == [
        {"name": "deepseek-flash", "nearest": "deepseek-v4.1-flash"}, {"name": "GLM-5.3", "nearest": "glm-5.3"},
        {"name": "gpt-9", "nearest": ""}]
    # names a service serves as it lists them: Ollama's llama3.3 (llama3.3:latest), OpenRouter's with a variant
    assert not_offered(["llama3.3", "qwen3:32b", "openai/gpt-5:online", "openai/gpt-5:nitro"],
                       ["llama3.3:latest", "qwen3:latest", "openai/gpt-5"]) == [
        {"name": "qwen3:32b", "nearest": "qwen3:latest"}]                     # a size it hasn't pulled is not
    assert say_not_offered("OpenCode Go", not_offered(["kimi-k9", "deepseek-flash", "gpt-9"], GO_MODELS)) == (
        "OpenCode Go doesn't offer kimi-k9 (nearest: kimi-k3), deepseek-flash (nearest: deepseek-v4.1-flash) or gpt-9.")
    service = OpenCodeGo()
    monkeypatch.setattr(urllib.request, "urlopen", service.urlopen)
    runtime, settings, store = parts
    headers = {"X-Kelvin-Token": settings.access_key}
    named = {"preset": "opencode-go", "api_key": "go-secret-key", "models": "deepseek-flash, glm-5.3"}
    said = "OpenCode Go doesn't offer deepseek-flash (nearest: deepseek-v4.1-flash)."
    with TestClient(create_app(runtime, settings, store, hosts=["testserver"])) as client:
        r = client.post("/api/services", json=named, headers=headers)
        assert r.status_code == 200 and r.json()["names"] == said and "go-secret-key" not in r.text
        assert r.json()["services"][0]["models"] == "deepseek-flash,glm-5.3"     # kept as typed: the save never waits
        assert service.sessions and all(s.startswith("kelvin-") for s in service.sessions)   # its list, with a session
        assert client.post("/api/services/opencode-go/test", json={}, headers=headers).json() == \
            {"ok": False, "message": said}
        client.delete("/api/services/opencode-go", headers=headers)
        r = client.post("/api/services", json=dict(named, models="glm-5.3"), headers=headers)
        assert r.status_code == 200 and "names" not in r.json()                   # all there: nothing to say
        client.delete("/api/services/opencode-go", headers=headers)
        service.lists = False                                                     # its list out of reach
        r = client.post("/api/services", json=named, headers=headers)
        assert r.status_code == 200 and "names" not in r.json()                   # added as before
        assert client.post("/api/services/opencode-go/test", json={}, headers=headers).json() == \
            {"ok": True, "message": "2 models that Kelvin can use"}
        client.delete("/api/services/opencode-go", headers=headers)
        wrong = ("OpenCode Go rejected the API key: it is wrong, or it was revoked. Copy it again into Settings → "
                 "Models.")
        r = client.post("/api/services", json=dict(named, api_key="wrong-key"), headers=headers)   # 401 for its list
        assert r.status_code == 200 and r.json()["names"] == wrong and "wrong-key" not in r.text
        assert client.post("/api/services/opencode-go/test", json={}, headers=headers).json() == \
            {"ok": False, "message": wrong}                                       # not "Works" for a key turned down


COMMANDS = ("SOURce:VOLTage:LEVel <n> sets the level; SOURce:VOLTage:LEVel? reads it. OUTPut:STATe ON|OFF switches "
            "the output. MEASure:CURRent? reads the current.\n")
MANUAL = "Acme Gizmo 7 programming manual\n" + COMMANDS * 10   # a command reference a card can be written from


def test_a_refusal_inside_a_tool_reaches_the_chat_in_plain_words(parts, tmp_path):
    """draft_card and translate_vi ask the model outside any turn, and their results quote why it failed: in plain
    words on one line, as the chat says it, never the key a service echoes back or the SDK's "Error code: 401"."""
    import openai
    httpx2 = pytest.importorskip("httpx2")
    from labharness import vicheck
    from test_web import client_for
    echoed = {"error": {"message": "Incorrect API key provided: sk-proj-AbCd****************wxYZ. You can find your "
                                   "API key at https://platform.openai.com/account/api-keys.",
                        "type": "invalid_request_error", "code": "invalid_api_key"}}
    gpt = OpenAIProvider("openai", "GPT", "sk-proj-AbCdEfGh0123456789wxYZ", models=["gpt-5"])
    gpt.client = openai.AsyncOpenAI(api_key="sk-proj-AbCdEfGh0123456789wxYZ", max_retries=0,
                                    http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(
                                        lambda request: httpx2.Response(401, json=echoed))))
    manual = tmp_path / "gizmo-7.txt"
    manual.write_text(MANUAL, encoding="utf-8")
    said = ("GPT rejected the API key: it is wrong, or it was revoked. Copy it again from platform.openai.com into "
            "Settings → Models.")
    runtime = parts[0]
    client, _ = client_for(parts, {"openai": gpt})
    with client:
        assert runtime.draft_card("", "Acme", "Gizmo 7", str(manual)) == \
            "no card yet: Kelvin could not write the Acme Gizmo 7 card: " + said      # the card's model: the default
        ask = runtime.vi_model("translate", {})
        with pytest.raises(ValueError) as failed:   # translate_vi says it as it is ("not translated: ...")
            ask("Translate it.", "The VI.")
        assert str(failed.value) == said
        assert [f.text for f in vicheck.review(ask, "The VI.", "x = 1")] == ["the second opinion failed: " + said]
