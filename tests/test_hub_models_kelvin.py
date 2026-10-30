"""Kelvin's side of the hub's models (web/hubmodels.py, providers/hub.py): the hub's Claude, GPT, Gemini and OpenCode
in the model picker while the hub offers them, with the month so far; the user version (edition.py) with no keys of
its own; confidential work never through the hub; and Kelvin's real model connections talking through a test hub to
pretend services."""
import asyncio
import json
import time

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")
pytest.importorskip("httpx")
pytest.importorskip("cryptography")
pytest.importorskip("anthropic")
pytest.importorskip("openai")
pytest.importorskip("google.genai")

import httpx  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from simlab import build_virtual_lab  # noqa: E402
from labharness import edition  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.gate import ScriptedApprover  # noqa: E402
from labharness.hub import catalog, llm  # noqa: E402
from labharness.hub.db import Hub  # noqa: E402
from labharness.hub.server import create_app as hub_app  # noqa: E402
from labharness.web import hubmodels  # noqa: E402
from labharness.web.agent import ChatAgent  # noqa: E402
from labharness.web.app import make_providers  # noqa: E402
from labharness.web.hubmodels import HubModels, hub_providers  # noqa: E402
from labharness.web.modelerrors import describe  # noqa: E402
from labharness.web.settings import Settings, resolve_model  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from test_hub_models import KELVIN, PRICES, Services  # noqa: E402

OFFERED = {"services": [{"id": "anthropic", "name": "Claude", "models": ["claude-opus-5-5", "claude-sonnet-5-5"]},
                        {"id": "opencode", "name": "OpenCode", "models": None}],
           "month": "2026-10", "spent": 3.2, "budget": 20.0, "renews": "November 1", "lab": None}


def told(answer):
    asked = []

    def fetch(url, token):
        asked.append((url, token))
        if isinstance(answer, Exception):
            raise answer
        return answer
    return fetch, asked


def test_what_the_hub_offers_is_asked_every_ten_minutes(monkeypatch):
    fetch, asked = told(OFFERED)
    hub = HubModels("https://hub.test/", "khub_t", fetch)
    assert not hub.offers("anthropic") and hub.status() is None
    hub.refresh()
    assert asked == [("https://hub.test", "khub_t")] and hub.offers("anthropic") and not hub.offers("gemini")
    assert hub.models_of("anthropic") == ["claude-opus-5-5", "claude-sonnet-5-5"] and hub.models_of("opencode") is None
    hub.refresh()
    assert len(asked) == 1                                                    # not again within ten minutes
    again = HubModels("https://hub.test", "khub_t", told(RuntimeError("offline"))[0])
    assert again.offers("anthropic")                                          # the same sign-in: what was said stays
    later = time.time() + hubmodels.EVERY + 1
    monkeypatch.setattr(hubmodels.time, "time", lambda: later)
    again.refresh()
    assert again.offers("anthropic")                                          # the hub out of reach: kept a while
    other = HubModels("https://hub.test", "khub_someone_else", told(OFFERED)[0])
    assert not other.offers("anthropic")                                      # never another person's


def kelvin(tmp_path, providers):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    store = ChatStore(tmp_path / "chats")
    return ChatAgent(rt, store, build_server(rt), providers), store


def test_the_hubs_models_show_while_it_offers_them(tmp_path):
    fetch, asked = told(OFFERED)
    providers = hub_providers(("https://hub.test", "khub_t"))
    assert list(providers) == ["hub-anthropic", "hub-openai", "hub-gemini", "hub-opencode", "hub-opencode-go"]
    for p in providers.values():
        p.hub.fetch = fetch
    providers["hub-opencode"].models = lambda: asyncio.sleep(0, [])          # no network for OpenCode's list here
    agent, _ = kelvin(tmp_path, providers)
    listed = asyncio.run(agent.models())
    groups = {g["provider"]: g for g in listed["groups"]}
    assert set(groups) == {"hub-anthropic", "hub-opencode"}                    # GPT and Gemini: no key on the hub
    assert [m["key"] for m in groups["hub-anthropic"]["models"]] == ["hub-anthropic:claude-opus-5-5",
                                                                      "hub-anthropic:claude-sonnet-5-5"]
    assert groups["hub-anthropic"]["label"] == "Claude · Kelvin hub" and "missing_key" not in groups["hub-anthropic"]
    assert listed["hub"] == {"month": "2026-10", "spent": 3.2, "budget": 20.0, "renews": "November 1", "lab": None,
                             "needs_consent": None, "consent_url": None}   # an older hub doesn't say
    assert len(asked) == 1                                                    # one asking for all four
    assert resolve_model("opus", lambda s: s in groups) == "hub-anthropic:claude-opus-5-5"


def test_confidential_work_never_goes_through_the_hub(tmp_path):
    agent, store = kelvin(tmp_path, {})
    secret = store.create_project("Sample X")["id"]
    store.set_sharing(secret, "confidential")
    chat = store.load(store.create("hub-anthropic:claude-opus-5-5", project=secret)["id"])
    with pytest.raises(ValueError, match="confidential work never does"):
        agent.check_model(chat, "hub-anthropic:claude-opus-5-5")
    agent.check_model(chat, "local:qwen3:32b")                                # a model on this computer may
    store.assign(chat["id"], None)                                            # leaving keeps the mark
    with pytest.raises(ValueError):
        agent.check_model(store.load(chat["id"]), "hub-gemini:gemini-3.1-pro")
    plain = store.create("hub-anthropic:claude-opus-5-5")
    agent.check_model(plain, "hub-anthropic:claude-opus-5-5")
    shy = store.create_project("Not yet published")["id"]
    store.set_sharing(shy, "unpublished")
    agent.check_model(store.create("hub-anthropic:claude-opus-5-5", project=shy), "hub-anthropic:claude-opus-5-5")


def test_the_hub_says_why_in_plain_words():
    class Refused(Exception):
        def __init__(self, status, message):
            super().__init__(message)
            self.status_code, self.body = status, {"type": "error", "error": {"type": "x", "message": message}}
    assert "sign out of Kelvin and in again" in describe("Claude · Kelvin hub", "hub-anthropic",
                                                         Refused(401, "Sign in again"))
    said = describe("Claude · Kelvin hub", "hub-anthropic",
                    Refused(402, "This month's model budget on the Kelvin hub ($20.00) is used up."))
    assert said == "Claude · Kelvin hub: This month's model budget on the Kelvin hub ($20.00) is used up."
    said = describe("Gemini · Kelvin hub", "hub-gemini", Refused(400, "API key not valid. Please pass a valid API key."))
    assert "refused the Kelvin hub's key" in said and "Settings" not in said


def test_the_app_takes_no_keys_of_its_own(tmp_path, monkeypatch):
    """The Kelvin app, the private build too, uses the hub's models; only Kelvin from source takes keys."""
    from labharness import claudecode
    settings = Settings(tmp_path / "settings.json")
    settings.update({"anthropic_api_key": "sk-ant-mine-1234", "local_base_url": "http://localhost:11434/v1",
                     "local_models": "qwen3:32b"})
    settings.add_service({"preset": "custom", "name": "Lab vLLM", "base_url": "http://127.0.0.1:8000/v1"})
    assert {"anthropic", "openai", "gemini", "local", "lab-vllm"} <= set(make_providers(settings))   # from source
    monkeypatch.setattr(edition.sys, "frozen", True, raising=False)          # the app: private build or user version
    monkeypatch.setattr(claudecode, "enabled", lambda: False)                # (the bridge: test_claude_code.py)
    assert not edition.own_keys()
    providers = make_providers(settings)
    assert set(providers) == {"local"} and settings.public()["own_keys"] is False
    monkeypatch.setattr(settings, "get", lambda name, _get=settings.get: "khub_t" if name == "account_token" else
                        _get(name))
    monkeypatch.setattr(Settings, "account_hub", property(lambda self: "https://hub.test"))
    assert set(make_providers(settings)) == {"hub-anthropic", "hub-openai", "hub-gemini", "hub-opencode",
                                             "hub-opencode-go", "local"}


def test_the_private_build_keeps_the_bridge_but_not_keys(monkeypatch):
    import sys
    import types
    monkeypatch.setattr(edition.sys, "frozen", True, raising=False)
    monkeypatch.setitem(sys.modules, "labharness._private_bridge", types.ModuleType("labharness._private_bridge"))
    assert edition.private() and not edition.own_keys()


def test_the_user_versions_settings_refuse_keys(parts, monkeypatch):
    from test_web import Scripted, client_for
    monkeypatch.setattr(edition, "own_keys", lambda: False)
    client, headers = client_for(parts, {"scripted": Scripted([])})
    r = client.post("/api/settings", headers=headers, json={"anthropic_api_key": "sk-ant-mine-1234",
                                                            "local_models": "qwen3:32b"})
    assert r.status_code == 200 and r.json()["settings"]["keys"]["anthropic_api_key"] is None
    assert r.json()["settings"]["local_models"] == "qwen3:32b" and r.json()["settings"]["own_keys"] is False
    r = client.post("/api/services", headers=headers, json={"preset": "custom", "name": "X",
                                                             "base_url": "http://127.0.0.1:8000/v1"})
    assert r.status_code == 400 and "Kelvin account" in r.json()["error"]


@pytest.fixture
def parts(tmp_path):   # as test_web's
    from simlab import demo_envelope
    from labharness.agent.approval import WebApprover
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    return runtime, Settings(tmp_path / "settings.json"), ChatStore(tmp_path / "chats")


# ------------------------------------------------------------------ Kelvin's own connections, through a test hub

class WithModelLists(Services):
    def __call__(self, request):
        if request.url.host == "api.openai.com" and request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "gpt-6-sol", "object": "model", "created": 3, "owned_by": "openai"},
                {"id": "dall-e-4", "object": "model", "created": 2, "owned_by": "openai"}]})
        return Services.__call__(self, request)


def test_kelvins_own_connections_through_a_test_hub(tmp_path, monkeypatch):
    import anthropic
    import openai
    from google.genai import types
    monkeypatch.setattr(catalog, "_read_models_dev", lambda: PRICES)
    hub = Hub(tmp_path / "hub")
    services = WithModelLists()
    app = hub_app(hub, hosts=["testserver"], backups=False, llm_transport=httpx.MockTransport(services))
    token = TestClient(app).post("/api/register", json={
        "name": "Ada", "email": "ada@example.org", "password": "correct horse", "consent": True,
        "installation": KELVIN}).json()["access_token"]
    owner = llm.Gateway(hub, catalog.Catalog(hub.home))
    for s in ("anthropic", "openai", "gemini"):
        owner.set_key(s, services.keys[s])
    providers = hub_providers(("http://testserver", token))
    said = []

    async def emit(e):
        said.append(e)

    async def day():
        import httpx2   # Anthropic's and OpenAI's SDKs; Google's uses httpx
        def asgi(lib):
            return lib.AsyncClient(transport=lib.ASGITransport(app=app))
        claude, gpt, gemini = providers["hub-anthropic"], providers["hub-openai"], providers["hub-gemini"]
        claude.client = anthropic.AsyncAnthropic(api_key=token, base_url="http://testserver/llm/anthropic",
                                                 http_client=asgi(httpx2))
        gpt.client = openai.AsyncOpenAI(api_key=token, base_url="http://testserver/llm/openai/v1",
                                        http_client=asgi(httpx2))
        from google import genai
        gemini.client = genai.Client(api_key=token, http_options=types.HttpOptions(
            base_url="http://testserver/llm/gemini", httpx_async_client=asgi(httpx)))
        history = [{"role": "user", "text": "how does it look?"}]
        a = await claude.stream("claude-opus-5-5", "Be brief.", history, [], emit)
        b = await gpt.stream("gpt-6-sol", "Be brief.", history, [], emit)
        c = await gemini.stream("gemini-3.1-pro", "Be brief.", history, [], emit)
        return a, b, c, [m.id for m in await gpt.models()]
    a, b, c, gpts = asyncio.run(day())
    assert [r.blocks[-1]["text"] for r in (a, b, c)] == ["A clean peak.", "Six points.", "Settled."]
    assert gpts == ["gpt-6-sol"]                                             # OpenAI's own list, through the hub
    first = services.seen[0]
    assert first[1].startswith("https://api.anthropic.com/v1/messages") and first[2]["x-api-key"] == \
        services.keys["anthropic"] and token not in json.dumps(first[2])
    assert first[3]["messages"][-1]["content"][-1]["text"].startswith("how does it look?")
    for _ in range(50):
        with hub.connect() as con:
            used = [(r["service"], r["model"]) for r in con.execute("SELECT service, model FROM llm_usage ORDER BY id")]
        if len(used) >= 3:
            break
        time.sleep(0.05)
    assert used == [("anthropic", "claude-opus-5-5"), ("openai", "gpt-6-sol"), ("gemini", "gemini-3.1-pro")]
    assert owner.spent(1) > 0


def test_a_hub_with_nothing_yet_is_asked_again_in_a_minute(monkeypatch):
    empty = dict(OFFERED, services=[])
    fetch, asked = told(empty)
    hub = HubModels("https://hub.test", "khub_new", fetch)
    hub.refresh()
    soon = time.time() + hubmodels.AGAIN + 1
    monkeypatch.setattr(hubmodels.time, "time", lambda: soon)
    hub.fetch = told(OFFERED)[0]
    hub.refresh()                                                             # the owner has set a key meanwhile
    assert len(asked) == 1 and hub.offers("anthropic")


def test_a_model_list_refused_says_the_hubs_own_words(monkeypatch):
    import io
    import urllib.error
    import urllib.request
    from labharness.web.providers import openai_provider

    def refuse(req, timeout=20):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(json.dumps(
            {"type": "error", "error": {"message": "The Kelvin hub's terms changed. Agree to them at "
                                                  "https://hub.test/consent first."}}).encode()))
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(urllib.error.HTTPError) as caught:
        openai_provider._fetch_json("https://hub.test/llm/opencode/v1/models", "khub_t")
    said = describe("OpenCode · Kelvin hub", "hub-opencode", caught.value)
    assert said == ("OpenCode · Kelvin hub: The Kelvin hub's terms changed. Agree to them at https://hub.test/consent "
                    "first.")


def test_new_hub_terms_are_said_beside_the_hubs_models(tmp_path):
    waiting = dict(OFFERED, needs_consent=True, consent_url="https://hub.test/consent")
    providers = hub_providers(("https://hub.test", "khub_terms"))
    for p in providers.values():
        p.hub.fetch = told(waiting)[0]
    providers["hub-opencode"].models = lambda: asyncio.sleep(0, [])
    agent, _ = kelvin(tmp_path, providers)
    listed = asyncio.run(agent.models())
    claude = next(g for g in listed["groups"] if g["provider"] == "hub-anthropic")
    assert claude["models"] and "agree to the hub's new terms first" in claude["error"]   # still listed
    assert listed["hub"]["needs_consent"] and listed["hub"]["consent_url"] == "https://hub.test/consent"


def test_each_chat_is_one_opencode_session():
    """OpenCode routes and caches by conversation: the hub's OpenCode connections send one session per chat, not
    the chat's id itself; outside a chat they send none, and the hub sends the person's."""
    from types import SimpleNamespace
    from labharness.web.providers.base import SESSION
    from test_pictures_by_model import Chunks
    providers = hub_providers(("https://hub.test", "khub_s"))
    go, zen = providers["hub-opencode-go"], providers["hub-opencode"]
    assert go.extra_headers() == {} and providers["hub-anthropic"].name == "hub-anthropic"
    sent = []

    class Completions:
        async def create(self, **request):
            sent.append(request)
            return Chunks("Six points.")
    go.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))

    async def emit(e):
        pass

    async def chat(chat_id):
        held = SESSION.set(chat_id)
        try:
            await go.stream("glm-5.3", "Be brief.", [{"role": "user", "text": "hi"}], [], emit)
            return zen.extra_headers()
        finally:
            SESSION.reset(held)
    one, again, other = (asyncio.run(chat(c)) for c in ("chat-1", "chat-1", "chat-2"))
    sessions = [r["extra_headers"]["x-opencode-session"] for r in sent]
    assert sessions[0] == sessions[1] != sessions[2] and sessions[0].startswith("kelvin-")
    assert "chat-1" not in sessions[0] and one == again == {"x-opencode-session": sessions[0]} != other
