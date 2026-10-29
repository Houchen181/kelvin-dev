"""Pictures model by model on services whose models differ (web/vision.py, openai_provider.py): the person's switch,
models that refused before, the service's own list, the Kelvin hub's list, and otherwise sending them, with the
message sent again without them if the model refuses."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("openai")
pytest.importorskip("httpx")

import httpx  # noqa: E402
import openai  # noqa: E402

from labharness.web import vision as vision_mod  # noqa: E402
from labharness.web.providers.base import sees_images  # noqa: E402
from labharness.web.providers.openai_provider import LEFT_OUT_NOTE, REFUSED_NOTE, OpenAIProvider  # noqa: E402
from labharness.web.vision import Vision  # noqa: E402

PIXEL = {"mime": "image/png", "data": "iVBORw0KGgo="}


def service(vision=None, **kw):
    return OpenAIProvider("oc", "OpenCode", "k", base_url="https://opencode.example/v1", preset="opencode",
                          vision=vision, **kw)


def test_the_first_that_knows_decides(tmp_path):
    book = Vision(tmp_path)
    book._data["hub"] = {"at": time.time(), "services": {"opencode": {"yes": ["kimi-k3"], "no": ["glm-5.3"]}}}
    p = service(book)
    assert sees_images(p, "kimi-k3") and not sees_images(p, "glm-5.3")         # the hub's list
    assert sees_images(p, "brand-new")                                          # nobody knows: Kelvin sends them
    p._inputs["glm-5.3"] = True                                                 # the service's own list comes first
    assert sees_images(p, "glm-5.3")
    book.refuse("oc", "glm-5.3")                                                # a refusal here comes before that
    assert not sees_images(p, "glm-5.3")
    assert json.loads((tmp_path / "pictures.json").read_text())["refused"]["oc/glm-5.3"] > 0   # kept for next time
    assert not Vision(tmp_path).refused("oc", "kimi-k3") and Vision(tmp_path).refused("oc", "glm-5.3")
    assert sees_images(service(book, pictures="on"), "glm-5.3")                 # the person's switch above all
    assert not sees_images(service(book, pictures="off"), "kimi-k3")
    assert sees_images(OpenAIProvider("openai", "GPT", "k"), "gpt-anything")   # OpenAI's own: every model
    assert not sees_images(SimpleNamespace(), "m") and sees_images(SimpleNamespace(sees_images=True), "m")


def test_a_refusal_is_asked_again_after_a_month(tmp_path, monkeypatch):
    book = Vision(tmp_path)
    book.refuse("oc", "m")
    later = time.time() + (vision_mod.REMEMBER_DAYS + 1) * 86400
    monkeypatch.setattr(vision_mod.time, "time", lambda: later)
    assert not book.refused("oc", "m")


def test_openrouters_list_says_which_models_take_pictures():
    listed = {"data": [
        {"id": "deepseek/deepseek-v4.1-flash", "created": 3, "supported_parameters": ["tools"],
         "architecture": {"input_modalities": ["text", "image"]}},
        {"id": "z-ai/glm-5.3", "created": 2, "supported_parameters": ["tools"],
         "architecture": {"input_modalities": ["text"]}},
        {"id": "old/one", "created": 1, "supported_parameters": ["tools"]}]}
    p = OpenAIProvider("openrouter", "OpenRouter", "k", base_url="https://openrouter.ai/api/v1",
                       models_url="https://openrouter.ai/api/v1/models", tools_only=True,
                       fetch=lambda url, key: listed, preset="openrouter")
    assert [m.id for m in asyncio.run(p.models())] == ["deepseek/deepseek-v4.1-flash", "z-ai/glm-5.3", "old/one"]
    assert sees_images(p, "deepseek/deepseek-v4.1-flash") and not sees_images(p, "z-ai/glm-5.3")
    assert sees_images(p, "old/one")   # it says nothing: Kelvin sends them


def test_the_hubs_list_is_fetched_once_a_day_and_kept(tmp_path, monkeypatch):
    asked = []

    def fetch(url, token):
        asked.append((url, token))
        return {"services": {"deepseek": {"yes": ["deepseek-flash"], "no": ["deepseek-v4-pro"]}}}
    book = Vision(tmp_path, lambda: None, fetch)
    assert book.hub_says("deepseek", "deepseek-flash") is None and not asked   # not signed in: no hub
    book = Vision(tmp_path, lambda: ("https://hub.test", "khub_t"), fetch)
    started = []
    monkeypatch.setattr(vision_mod, "threading", SimpleNamespace(Lock=vision_mod.threading.Lock, Thread=lambda target,
                        args, **kw: SimpleNamespace(start=lambda: started.append(target(*args)))))
    assert book.hub_says("deepseek", "deepseek-flash") is True               # fetched (here at once), then read
    assert asked == [("https://hub.test", "khub_t")] and started == [True]
    assert book.hub_says("deepseek", "deepseek-v4-pro") is False and book.hub_says("opencode", "x") is None
    assert len(asked) == 1                                                    # not again within the day
    assert Vision(tmp_path).hub_says("deepseek", "deepseek-flash") is True     # kept beside the settings

    def down(url, token):
        raise OSError("offline")
    old = Vision(tmp_path, lambda: ("https://hub.test", "khub_t"), down)
    old._data["hub"]["at"] = 0                                                # a day old: asked again, fails
    assert not old.fetch_now("https://hub.test", "khub_t")
    assert old.hub_says("deepseek", "deepseek-flash") is True                  # the list it had stays


class Chunks:
    """An OpenAI-style stream of one text answer."""

    def __init__(self, text):
        self.items = [SimpleNamespace(model="m", choices=[SimpleNamespace(
            delta=SimpleNamespace(content=text, tool_calls=None), finish_reason="stop")])]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.items:
            raise StopAsyncIteration
        return self.items.pop(0)


class Service:
    """chat.completions.create as a service that refuses pictures (with status and what it says) and answers
    otherwise."""

    def __init__(self, status=400, said="unknown variant `image_url`, expected `text`"):
        self.status, self.said = status, said
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **request):
        self.requests.append(request)
        if any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"])
               for m in request["messages"]) or self.status == 401:
            response = httpx.Response(self.status, request=httpx.Request("POST", "https://opencode.example/v1"))
            raise openai.APIStatusError(self.said, response=response, body={"error": {"message": self.said}})
        return Chunks("I read the caption.")


def ask(p, history):
    async def emit(e):
        pass
    return asyncio.run(p.stream("glm-5.3", "system", history, [], emit))


@pytest.mark.parametrize("status", [400, 404, 422])
def test_a_model_that_refuses_pictures_gets_the_message_without_them(tmp_path, status):
    book = Vision(tmp_path)
    p = service(book)
    p.client = Service(status)
    history = [{"role": "user", "text": "how does the wiring look?", "images": [PIXEL, PIXEL]}]
    result = ask(p, history)
    assert result.blocks == [{"type": "text", "text": "I read the caption."}] and result.note == REFUSED_NOTE
    first, again = p.client.requests
    assert any(part.get("type") == "image_url" for part in first["messages"][1]["content"])
    assert again["messages"][1]["content"] == "how does the wiring look?\n\n(2 pictures not shown: the model didn't " \
                                              "take pictures)"
    assert book.refused("oc", "glm-5.3") and not sees_images(p, "glm-5.3")    # remembered: captions from now on


def test_a_refusal_that_isnt_about_pictures_is_not_remembered(tmp_path):
    book = Vision(tmp_path)
    p = service(book)
    p.client = Service(400, "This model's maximum context length is 131072 tokens")
    result = ask(p, [{"role": "user", "text": "and now?", "images": [PIXEL]}])
    assert result.note == LEFT_OUT_NOTE and len(p.client.requests) == 2      # this message went without them...
    assert not book.refused("oc", "glm-5.3") and sees_images(p, "glm-5.3")    # ...but the model still gets pictures


def test_other_errors_are_not_taken_for_a_refusal(tmp_path):
    book = Vision(tmp_path)
    p = service(book)
    p.client = Service(401)                                                   # a wrong key, pictures or not
    with pytest.raises(openai.APIStatusError):
        ask(p, [{"role": "user", "text": "hi", "images": [PIXEL]}])
    assert len(p.client.requests) == 1 and not book.refused("oc", "glm-5.3")
    p.client = Service(400)
    p.sees_images = True                                                      # OpenAI's own: a 400 is a real error
    with pytest.raises(openai.APIStatusError):
        ask(p, [{"role": "user", "text": "hi", "images": [PIXEL]}])


def test_the_switch_is_kept_with_the_service(tmp_path):
    from labharness.web.services import make, view
    from labharness.web.settings import Settings
    entry = make({"preset": "custom", "name": "Lab vLLM", "base_url": "http://127.0.0.1:8000/v1",
                  "pictures": "on"}, [])
    assert entry["pictures"] == "on" and view(entry, None)["pictures"] == "on"
    with pytest.raises(ValueError):
        make({"preset": "custom", "name": "X", "base_url": "http://127.0.0.1:8000/v1", "pictures": "maybe"}, [])
    settings = Settings(tmp_path / "settings.json")
    settings.add_service({"preset": "custom", "name": "Lab vLLM", "base_url": "http://127.0.0.1:8000/v1"})
    sid = settings.services[0]["id"]
    assert "pictures" not in settings.services[0] and settings.service_views()[0]["pictures"] == ""
    settings.service_pictures(sid, "off")
    assert Settings(tmp_path / "settings.json").services[0]["pictures"] == "off"   # saved
    settings.service_pictures(sid, "")
    assert "pictures" not in settings.services[0]
    with pytest.raises(KeyError):
        settings.service_pictures("nope", "on")
    with pytest.raises(ValueError):
        settings.service_pictures(sid, "sometimes")


def test_kelvin_builds_each_service_with_its_switch(tmp_path):
    pytest.importorskip("starlette")
    from labharness.web.app import make_providers
    from labharness.web.settings import Settings
    settings = Settings(tmp_path / "settings.json")
    settings.add_service({"preset": "custom", "name": "Lab vLLM", "base_url": "http://127.0.0.1:8000/v1",
                          "pictures": "off"})
    p = make_providers(settings)["lab-vllm"]
    assert p.pictures == "off" and p.preset == "custom" and p.vision.path == tmp_path / "pictures.json"
    assert p.vision.hub() is None                                             # no one signed in to the hub
    assert make_providers(settings)["openai"].vision is None                  # OpenAI's own: every model sees


def test_the_portal_switches_pictures_for_a_service(parts):
    pytest.importorskip("mcp")
    from test_web import Scripted, client_for
    client, headers = client_for(parts, {"scripted": Scripted([])})
    r = client.post("/api/services", headers=headers, json={"preset": "custom", "name": "Lab vLLM",
                                                             "base_url": "http://127.0.0.1:8000/v1"})
    sid = r.json()["services"][0]["id"]
    r = client.patch("/api/services/" + sid, headers=headers, json={"pictures": "off"})
    assert r.status_code == 200 and r.json()["services"][0]["pictures"] == "off"
    assert client.patch("/api/services/" + sid, headers=headers, json={"pictures": "maybe"}).status_code == 400
    assert client.patch("/api/services/nope", headers=headers, json={"pictures": "on"}).status_code == 404
    assert client.patch("/api/services/" + sid, headers=headers, json=["on"]).status_code == 400


@pytest.fixture
def parts(tmp_path):   # as test_web's
    pytest.importorskip("mcp")
    from simlab import build_virtual_lab, demo_envelope
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    return runtime, Settings(tmp_path / "settings.json"), ChatStore(tmp_path / "chats")
