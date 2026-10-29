"""Which model a chat starts with (Claude Opus unless Settings says otherwise, the owner's choice), model names
written without their service, and what a model service's refusal says (F3 #25: silently Opus, "claudecode:sonnet"
needed in some places, and every failure said "add its API key")."""
import json

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")
pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402

from simlab import build_virtual_lab, demo_envelope  # noqa: E402
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.web.app import create_app  # noqa: E402
from labharness.web.modelerrors import describe, unavailable  # noqa: E402
from labharness.web.providers.base import ModelInfo, Provider, TurnResult  # noqa: E402
from labharness.web.settings import Settings, bridge_alias, default_model, resolve_model  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402


class Fake(Provider):
    """A model service with Claude's model list, answering once, or failing as told."""

    def __init__(self, name, label, ids, fail=None, key=True):
        self.name, self.label, self.ids, self.fail, self.key = name, label, ids, fail, key
        self.asked = []

    def available(self):
        return self.key

    async def models(self):
        return [ModelInfo(i, i, self.name) for i in self.ids]

    async def stream(self, model, system, history, tools, emit):
        self.asked.append(model)
        if self.fail is not None:
            raise self.fail
        await emit({"type": "text", "delta": "ok"})
        return TurnResult([{"type": "text", "text": "ok"}], model=model)


CLAUDE_IDS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"]


@pytest.fixture
def parts(tmp_path):
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    return runtime, Settings(tmp_path / "settings.json"), ChatStore(tmp_path / "chats")


def client_for(parts, providers):
    runtime, settings, store = parts
    app = create_app(runtime, settings, store, hosts=["testserver"], providers=providers)
    return TestClient(app), {"X-Kelvin-Token": settings.access_key}


def turn(client, headers, chat_id, model, text="hello"):
    with client.stream("POST", "/api/chats/%s/turn" % chat_id, json={"text": text, "model": model},
                       headers=headers) as r:
        if r.status_code != 200:
            return r.status_code, json.loads(r.read())
        return 200, [json.loads(line) for line in r.iter_lines() if line.strip()]


# --- the default ----------------------------------------------------------------------------------------------

def test_opus_is_the_default_wherever_it_is_listed():
    keys = ["openai:gpt-5"] + ["anthropic:" + i for i in reversed(CLAUDE_IDS)]   # Opus last, not first
    assert default_model(keys) == "anthropic:claude-opus-5-5"
    assert default_model(keys, "anthropic:claude-sonnet-5-5") == "anthropic:claude-sonnet-5-5"   # Settings' choice
    assert default_model(keys, "gemini:gone") == "anthropic:claude-opus-5-5"   # a choice no longer offered
    bridge = ["claudecode:sonnet", "claudecode:haiku", "claudecode:opus"]
    assert default_model(bridge) == "claudecode:opus"
    assert default_model(["anthropic:claude-opus-5-5"] + bridge) == "anthropic:claude-opus-5-5"
    assert default_model(["openai:gpt-5"]) == "openai:gpt-5" and default_model([]) is None


def test_the_portal_starts_new_chats_on_opus_and_settings_can_change_it(parts):
    settings = parts[1]
    client, headers = client_for(parts, {"anthropic": Fake("anthropic", "Claude", CLAUDE_IDS),
                                         "claudecode": Fake("claudecode", "Claude Code", ["opus", "sonnet", "haiku"])})
    with client:
        boot = client.get("/api/bootstrap", headers=headers).json()
        assert boot["default_model"] == "anthropic:claude-opus-5-5" and boot["auto_model"] == boot["default_model"]
        r = client.post("/api/settings", json={"default_model": "anthropic:claude-sonnet-5-5"}, headers=headers).json()
        assert r["default_model"] == "anthropic:claude-sonnet-5-5" and r["auto_model"] == "anthropic:claude-opus-5-5"
        assert client.get("/api/bootstrap", headers=headers).json()["default_model"] == "anthropic:claude-sonnet-5-5"
        r = client.post("/api/settings", json={"default_model": ""}, headers=headers).json()   # back to Automatic
        assert r["default_model"] == "anthropic:claude-opus-5-5" and r["settings"]["default_model"] == ""
        settings.update({"default_model": "sonnet"})   # a name alone, as Claude Code takes it
        assert client.get("/api/bootstrap", headers=headers).json()["default_model"] == "anthropic:claude-sonnet-5-5"


def test_with_only_claude_code_the_default_is_its_opus(parts):
    client, headers = client_for(parts, {"anthropic": Fake("anthropic", "Claude", CLAUDE_IDS, key=False),
                                         "claudecode": Fake("claudecode", "Claude Code", ["opus", "sonnet", "haiku"])})
    with client:
        assert client.get("/api/bootstrap", headers=headers).json()["default_model"] == "claudecode:opus"


# --- model names ----------------------------------------------------------------------------------------------

def test_a_model_named_without_its_service_goes_where_it_can_run():
    both = lambda s: s in ("anthropic", "claudecode")   # noqa: E731
    bridge = lambda s: s == "claudecode"   # noqa: E731
    assert resolve_model("sonnet", both) == "anthropic:claude-sonnet-5-5"
    assert resolve_model("Sonnet", bridge) == "claudecode:sonnet"
    assert resolve_model("claude-opus-5-5", bridge) == "claudecode:opus"
    assert resolve_model("anthropic:sonnet", bridge) == "anthropic:claude-sonnet-5-5"
    assert resolve_model("claudecode:claude-haiku-5-5", both) == "claudecode:haiku"
    assert resolve_model("claudecode:sonnet", both) == "claudecode:sonnet"
    assert resolve_model("openai:gpt-5", both) == "openai:gpt-5" and resolve_model("gpt-5", both) == "gpt-5"
    assert resolve_model("sonnet", lambda s: False) == "sonnet"   # nowhere to run: said by the caller
    assert resolve_model("fable", bridge) == "fable"   # Claude Code's plans don't name it


def test_a_chat_runs_on_sonnet_written_either_way(parts):
    bridge = Fake("claudecode", "Claude Code", ["opus", "sonnet", "haiku"])
    client, headers = client_for(parts, {"claudecode": bridge})
    with client:
        chat = client.post("/api/chats", json={"model": "claudecode:sonnet"}, headers=headers).json()
        status, events = turn(client, headers, chat["id"], "sonnet")
        assert status == 200 and events[0]["model"] == "claudecode:sonnet" and bridge.asked == ["sonnet"]
        assert client.get("/api/chats/" + chat["id"], headers=headers).json()["model"] == "claudecode:sonnet"


def test_a_model_that_cant_be_used_says_why(parts):
    client, headers = client_for(parts, {"anthropic": Fake("anthropic", "Claude", CLAUDE_IDS, key=False)})
    with client:
        chat = client.post("/api/chats", json={"model": "x"}, headers=headers).json()
        status, body = turn(client, headers, chat["id"], "anthropic:claude-sonnet-5-5")
        assert status == 400 and body["error"] == "Claude has no API key here: add one in Settings → Models."
        status, body = turn(client, headers, chat["id"], "claudecode:sonnet")
        assert status == 400 and "Claude Code isn't available here" in body["error"]
        assert "API key" not in body["error"]   # it never needed one: F3 #25
        status, body = turn(client, headers, chat["id"], "gpt9")
        assert status == 400 and "no model called 'gpt9'" in body["error"]
    assert "pick one under the message box" in unavailable("", {})
    assert "signed in with ChatGPT" in unavailable("chatgpt:gpt-5", {})


def test_claude_code_works_the_screen_with_the_chats_claude_model_else_opus():
    assert bridge_alias("claudecode:haiku") == "haiku"
    assert bridge_alias("anthropic:claude-sonnet-5-5") == "sonnet"
    assert bridge_alias("openai:gpt-5") == "opus" and bridge_alias("") == "opus"   # the default's
    assert bridge_alias("anthropic:claude-fable-5-1") == "opus"   # a Claude model Claude Code doesn't name


# --- what a refusal says --------------------------------------------------------------------------------------

def _anthropic(cls, status, kind, message):
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    body = {"type": "error", "error": {"type": kind, "message": message}}
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    return getattr(anthropic, cls)("Error code: %d - %s" % (status, body), response=response, body=body)


def _openai(cls, status, body):
    openai = pytest.importorskip("openai")
    httpx2 = pytest.importorskip("httpx2")
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://api.openai.com/v1/chat/completions"))
    return getattr(openai, cls)("Error code: %d - %s" % (status, body), response=response, body=body)


def _google(status, message, reason=""):
    errors = pytest.importorskip("google.genai.errors")
    details = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}] if reason else []
    return errors.ClientError(status, {"error": {"code": status, "message": message, "status": "INVALID_ARGUMENT",
                                                 "details": details}})


def test_claudes_refusals_in_plain_words():
    said = describe("Claude", "anthropic", _anthropic("AuthenticationError", 401, "authentication_error",
                                                      "invalid x-api-key"))
    assert said.startswith("Claude rejected the API key: it is wrong, or it was revoked.") and "Settings" in said
    said = describe("Claude", "anthropic", _anthropic(
        "BadRequestError", 400, "invalid_request_error", "Your credit balance is too low to access the Anthropic API. "
        "Please go to Plans & Billing to upgrade or purchase credits."))
    assert said.startswith("Claude says this account has no credit or quota left") and "console.anthropic.com" in said
    assert "credit" in describe("Claude", "anthropic", _anthropic("APIStatusError", 402, "billing_error", "Payment"))
    said = describe("Claude", "anthropic", _anthropic("RateLimitError", 429, "rate_limit_error", "Number of requests"))
    assert "rate limit" in said and "Wait a minute" in said
    said = describe("Claude", "anthropic", _anthropic("InternalServerError", 529, "overloaded_error", "Overloaded"))
    assert "overloaded" in said and "nothing is wrong with your key" in said
    said = describe("Claude", "anthropic", _anthropic("NotFoundError", 404, "not_found_error", "model: claude-x"))
    assert "doesn't offer that model" in said
    said = describe("Claude", "anthropic", _anthropic("PermissionDeniedError", 403, "permission_error",
                                                      "Your organization does not have access to this model"))
    assert "isn't allowed" in said
    said = describe("Claude", "anthropic", _anthropic("BadRequestError", 400, "invalid_request_error",
                                                      "prompt is too long: 1200000 tokens > 1000000 maximum"))
    assert "too long" in said and "API key" not in said   # "tokens" isn't a key
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    said = describe("Claude", "anthropic", anthropic.APIConnectionError(
        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")))
    assert said.startswith("Kelvin couldn't reach Claude")


def test_other_services_refusals_never_repeat_the_key():
    said = describe("GPT", "openai", _openai("AuthenticationError", 401, {
        "message": "Incorrect API key provided: sk-proj-abcd****************wxyz. You can find your API key at "
                   "https://platform.openai.com/account/api-keys.", "type": "invalid_request_error",
        "code": "invalid_api_key"}))
    assert said.startswith("GPT rejected the API key") and "wxyz" not in said and "sk-" not in said
    said = describe("GPT", "openai", _openai("RateLimitError", 429, {
        "message": "You exceeded your current quota, please check your plan and billing details.",
        "type": "insufficient_quota", "code": "insufficient_quota"}))
    assert "no credit or quota left" in said and "rate limit" not in said   # a 429 that isn't a rate limit
    said = describe("Gemini", "gemini", _google(400, "API key expired. Please renew the API key.", "API_KEY_INVALID"))
    assert said.startswith("Gemini says the API key has expired") and "aistudio.google.com" in said
    said = describe("Gemini", "gemini", _google(400, "API key not valid. Please pass a valid API key.",
                                                "API_KEY_INVALID"))
    assert said.startswith("Gemini rejected the API key")
    said = describe("OpenRouter", "openrouter", RuntimeError("Error code: 402 - Insufficient credits"))
    assert "no credit" in said


def test_claude_codes_failures_say_what_to_do():
    said = lambda text: describe("Claude Code", "claudecode", RuntimeError(text))   # noqa: E731
    assert "isn't signed in" in said("Not logged in · Please run /login")
    assert "isn't signed in" in said("Invalid API key · Please run /login")
    assert "has expired" in said("OAuth token has expired. Please obtain a new token or refresh your existing token.")
    assert "no credit left" in said("Credit balance is too low")
    assert "usage limit is reached" in said("5-hour limit reached ∙ resets 3pm")
    assert "usage limit is reached" in said("Claude AI usage limit reached|1761700000")
    assert "overloaded" in said('API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}')
    assert "took too long" in describe("Claude Code", "claudecode", TimeoutError("Claude Code took longer than 600 s"))


def test_a_turn_that_fails_on_the_key_says_so_in_the_chat(parts):
    failing = Fake("anthropic", "Claude", CLAUDE_IDS, fail=_anthropic("AuthenticationError", 401,
                                                                      "authentication_error", "invalid x-api-key"))
    client, headers = client_for(parts, {"anthropic": failing})
    with client:
        chat = client.post("/api/chats", json={"model": "anthropic:claude-sonnet-5-5"}, headers=headers).json()
        status, events = turn(client, headers, chat["id"], "anthropic:claude-sonnet-5-5")
        error = next(e for e in events if e["type"] == "error")
        assert error["message"].startswith("Claude rejected the API key")
