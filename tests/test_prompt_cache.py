"""What a request costs when the person comes back after a pause (asked for 2026-11-05).

Every request sends the whole prompt again. Claude caches it 5 minutes unless asked for an hour, and a person at the
bench comes back after longer: a chat now keeps its prompt an hour (the system prompt by a mark of its own, which every
chat shares), and a one-off request keeps the 5-minute default. The hub's meter prices an hour's cache writes at twice
the input price, and GPT-5.6's writes at the cache's own price. OpenAI's own models get a cache key for each chat. A
conversation is summarised at 60% of the model's window or 120k tokens, whichever comes first, with the windows of
today's models (Claude 5, GPT-6, DeepSeek V4: about 1M)."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from labharness.web import compaction

HOUR = {"type": "ephemeral", "ttl": "1h"}
TOOLS = [{"name": "lab_status", "description": "The lab now.", "schema": {"type": "object", "properties": {}}}]


async def quiet(event):
    pass


class FakeClaude:
    """Claude's client as the provider uses it: what each request asked for, and an empty answer."""

    def __init__(self):
        self.sent = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.sent.append(kwargs)
        return _Answer(kwargs["model"])


class _Answer:
    def __init__(self, model):
        self.model = model

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def get_final_message(self):
        return SimpleNamespace(content=[], stop_reason="end_turn", model=self.model)


def test_a_chat_keeps_its_prompt_an_hour_and_a_one_off_request_five_minutes():
    pytest.importorskip("anthropic")
    from labharness.web.providers.anthropic_provider import AnthropicProvider
    provider = AnthropicProvider("test-key")
    provider.client = claude = FakeClaude()
    hi = [{"role": "user", "text": "hi"}]
    asyncio.run(provider.stream("claude-opus-5-5", "Kelvin's rules", hi, TOOLS, quiet))
    sent = claude.sent[-1]
    assert sent["system"] == [{"type": "text", "text": "Kelvin's rules", "cache_control": HOUR}]   # every chat's
    assert sent["cache_control"] == HOUR   # the conversation, up to its latest message
    asyncio.run(provider.stream("claude-opus-5-5", "Summarise the day.", hi, [], quiet))   # a handoff, no tools
    sent = claude.sent[-1]
    assert sent["system"] == "Summarise the day." and sent["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize("model, tokens", [
    ("claude-opus-5-5", 120_000), ("claude-haiku-5-5", 120_000), ("claude-sonnet-4-5", 120_000),
    ("gpt-6-sol", 120_000), ("gpt-5.5", 120_000), ("deepseek-v4-pro", 120_000), ("deepseek-v4.1-flash", 120_000),
    ("deepseek-flash", 120_000), ("deepseek-chat", 76_800), ("qwen3-coder", 19_200), ("some-new-model", 76_800)])
def test_a_conversation_is_summarised_at_120k_tokens_or_sooner_in_a_small_window(model, tokens):
    assert compaction.limit(SimpleNamespace(name="openrouter"), model) == tokens


def test_claude_5_gpt_6_and_deepseek_v4_have_their_whole_windows():
    service = SimpleNamespace(name="openai")
    assert [compaction.window(service, m) for m in ("claude-opus-5-5", "claude-fable-5-1", "gpt-6-sol",
                                                     "deepseek-v4-pro", "claude-haiku-4-5")] == \
        [1_000_000] * 4 + [200_000]


def test_a_model_that_says_its_window_is_summarised_within_it():
    small = SimpleNamespace(name="local", context_window=lambda model_id: 3000)
    assert compaction.limit(small, "anything") == 1800


def _meter(service, *events):
    pytest.importorskip("anyio")
    pytest.importorskip("starlette")
    from labharness.hub import llm
    m = llm.Meter(service, True)
    m.feed("".join("data: %s\n\n" % json.dumps(e) for e in events).encode())
    m.close()
    return llm, m


def test_the_hub_prices_an_hours_cache_writes_at_twice_the_input():
    start = {"type": "message_start", "message": {"usage": {
        "input_tokens": 10, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 1000,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1000}, "output_tokens": 1}}}
    llm, m = _meter("anthropic", start, {"type": "message_delta", "usage": {"output_tokens": 50}})
    assert (m.used["cache_write"], m.used["cache_write_1h"]) == (1000, 1000)
    price = {"input": 4, "output": 20, "cache_read": 0.2, "cache_write": 5}   # Opus 5.5; 5 is the 5-minute write
    assert llm.cost(price, m.used) == pytest.approx((10 * 4 + 50 * 20 + 2000 * 0.2 + 1000 * 8) / 1e6)
    five = {"type": "message_start", "message": {"usage": {
        "input_tokens": 10, "cache_creation_input_tokens": 1000,
        "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 0}, "output_tokens": 50}}}
    llm, m = _meter("anthropic", five)
    assert llm.cost(price, m.used) == pytest.approx((10 * 4 + 50 * 20 + 1000 * 5) / 1e6)


def test_the_hub_counts_gpts_cache_writes_apart_from_its_input():
    usage = {"prompt_tokens": 3000, "completion_tokens": 10,
             "prompt_tokens_details": {"cached_tokens": 1000, "cache_write_tokens": 1500}}
    llm, m = _meter("openai", {"choices": [], "usage": usage})
    assert (m.used["input"], m.used["cache_read"], m.used["cache_write"], m.used["output"]) == (500, 1000, 1500, 10)
    llm, m = _meter("openai", {"type": "response.completed", "response": {"usage": {
        "input_tokens": 3000, "output_tokens": 10,
        "input_tokens_details": {"cached_tokens": 1000, "cache_write_tokens": 1500}}}})
    assert (m.used["input"], m.used["cache_read"], m.used["cache_write"]) == (500, 1000, 1500)


class FakeChat:
    """The Chat Completions client as the provider uses it: what each request asked for, and a one-line answer."""

    def __init__(self):
        self.sent = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **request):
        self.sent.append(request)

        async def chunks():
            yield SimpleNamespace(model=request["model"], choices=[
                SimpleNamespace(delta=SimpleNamespace(content="ok", tool_calls=None), finish_reason="stop")])
        return chunks()


class FakeResponses:
    """The Responses API client as the provider uses it for OpenAI's own models: what each request asked for, and a
    one-line answer."""

    def __init__(self):
        self.sent = []
        self.responses = SimpleNamespace(create=self._create)

    async def _create(self, **body):
        self.sent.append(body)

        async def events():
            yield SimpleNamespace(type="response.output_text.delta", delta="ok")
            yield SimpleNamespace(type="response.completed", response=SimpleNamespace(model=body["model"], output=[
                {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]))
        return events()


def test_openais_own_models_get_a_cache_key_for_each_chat_and_other_services_none():
    pytest.importorskip("openai")
    from labharness.web.providers.base import SESSION, session_key
    from labharness.web.providers.openai_provider import OpenAIProvider
    gpt = OpenAIProvider("openai", "OpenAI", "test-key")
    deepseek = OpenAIProvider("deepseek", "DeepSeek", "test-key", base_url="https://api.deepseek.com/v1")
    hi = [{"role": "user", "text": "hi"}]
    gpt.client, deepseek.client = FakeResponses(), FakeChat()
    token = SESSION.set("chat-1")
    try:
        key = session_key()
        asyncio.run(gpt.stream("gpt-6-sol", "rules", hi, TOOLS, quiet))
        asyncio.run(deepseek.stream("deepseek-v4-pro", "rules", hi, TOOLS, quiet))
    finally:
        SESSION.reset(token)
    assert key.startswith("kelvin-") and "chat-1" not in key   # never the chat's own id
    assert gpt.client.sent[-1]["prompt_cache_key"] == key      # OpenAI's own: the Responses API's cache key
    assert "extra_body" not in deepseek.client.sent[-1]   # DeepSeek caches by itself, for hours
    asyncio.run(gpt.stream("gpt-6-sol", "rules", hi, TOOLS, quiet))   # outside any chat: no key
    assert "prompt_cache_key" not in gpt.client.sent[-1]
