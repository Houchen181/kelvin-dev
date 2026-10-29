"""Other model services (OpenRouter, OpenCode, DeepSeek...): set up, listed, kept apart from local models."""
import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("starlette")
pytest.importorskip("openai")

from labharness.web import rules  # noqa: E402
from labharness.web.providers.openai_provider import MAX_LISTED, OpenAIProvider  # noqa: E402
from labharness.web.services import check_url, make  # noqa: E402
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

    def fetch(url, key):
        fetched.append((url, key))
        return {"object": "list", "data": [{"id": "big-pickle", "created": 5}, {"id": "claude-opus-4-6", "created": 5},
                                           {"id": "gpt-5", "created": 5}, {"id": "qwen3-coder", "created": 6}]}
    oc = OpenAIProvider("opencode", "OpenCode", "oc-key", base_url="https://opencode.ai/inference/openai/v1",
                        models_url="https://opencode.ai/inference/v1/models", exclude=["claude", "gpt"], fetch=fetch)
    assert [x.id for x in asyncio.run(oc.models())] == ["qwen3-coder", "big-pickle"]
    assert fetched == [("https://opencode.ai/inference/v1/models", "oc-key")]
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
