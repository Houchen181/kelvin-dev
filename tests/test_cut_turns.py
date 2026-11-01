"""A model's turn cut short (the length limit) or declined while it wrote a tool call: the call never runs, and it is
never sent to the model again either (no result follows it, which every service refuses: the chat would be stuck).
A Gemini turn with nothing in it is said to the person, and never sent again. The services' answers are made up here
(no service is called)."""
import asyncio
from types import SimpleNamespace

import pytest


async def quiet(event):
    pass


class Items:
    """An async stream of the given items."""

    def __init__(self, items):
        self.items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.items:
            raise StopAsyncIteration
        return self.items.pop(0)


def next_request(provider, result):
    """The chat as its next request carries it: the cut turn as the chat keeps it, then the person's next message."""
    return [{"role": "user", "text": "write the fit script"},
            {"role": "assistant", "provider": provider, "model": "m", "blocks": result.blocks, "raw": result.raw},
            {"role": "user", "text": "continue"}]


@pytest.mark.parametrize("stop", ["max_tokens", "model_context_window_exceeded", "refusal"])
def test_claude(stop):
    pytest.importorskip("anthropic")
    from anthropic.types.beta import BetaMessage
    from labharness.web.providers import anthropic_provider as ap

    class Stream(Items):
        def __init__(self, message):
            super().__init__([])
            self.message = message

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get_final_message(self):
            return self.message

    def answer(content):
        final = BetaMessage.model_validate({
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": stop,
            "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 64000}, "content": content})
        p = ap.AnthropicProvider(api_key="test")
        p.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: Stream(final))))
        return asyncio.run(p.stream("claude-opus-5-5", "sys", [{"role": "user", "text": "write the fit script"}], [],
                                    quiet))

    call = {"type": "tool_use", "id": "toolu_01", "name": "write_file",
            "input": {"path": "fit.py", "content": "import numpy as np\n# ...cut"}}
    result = answer([{"type": "thinking", "thinking": "A short script.", "signature": "sig"},
                     {"type": "text", "text": "Saving the fit script."}, call])
    assert result.stop_reason in ("max_tokens", "refusal") and result.note and not result.tool_calls
    assert [b["type"] for b in result.raw] == ["thinking", "text"]           # sent again without the call
    sent = ap.to_messages(next_request("anthropic", result))
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert not any(c.get("type") == "tool_use" for m in sent for c in m["content"])
    result = answer([call])                                                   # nothing but the call: nothing is left
    assert result.raw is None and result.blocks == []
    assert [m["role"] for m in ap.to_messages(next_request("anthropic", result))] == ["user"]


def test_chatgpt_plan():
    pytest.importorskip("openai")
    from labharness.web.providers import chatgpt_provider as cp
    output = [{"type": "message", "content": [{"type": "output_text", "text": "Saving."}]},
              {"type": "function_call", "call_id": "c1", "name": "kelvin.write_file", "arguments": "{\"path\": \"fi"}]

    async def create(**body):   # the answer stopped before it was complete
        return Items([SimpleNamespace(type="response.incomplete", response=SimpleNamespace(output=output,
                                                                                            model="gpt-x"))])
    signin = SimpleNamespace(access_token=lambda: "token", status=lambda: {"plan": "plus"})
    p = cp.ChatGPTProvider(signin, client_factory=lambda token: SimpleNamespace(
        responses=SimpleNamespace(create=create)))
    result = asyncio.run(p.stream("gpt-x", "sys", [{"role": "user", "text": "write the fit script"}], [], quiet))
    assert result.stop_reason == "max_tokens" and not result.tool_calls
    assert [i["type"] for i in result.raw] == ["message"]
    sent = cp.to_input(next_request("chatgpt", result), "chatgpt")
    assert [i.get("type") or i.get("role") for i in sent] == ["user", "message", "user"]


@pytest.mark.parametrize("finish", ["length", "content_filter"])
def test_openai_compatible_services(finish):
    pytest.importorskip("openai")
    from labharness.web.providers.openai_provider import OpenAIProvider, to_messages

    def chunk(finish_reason=None, **delta):
        return SimpleNamespace(model="m", choices=[SimpleNamespace(
            delta=SimpleNamespace(**dict({"content": None, "tool_calls": None}, **delta)), finish_reason=finish_reason)])
    call = SimpleNamespace(index=0, id="call_1", function=SimpleNamespace(name="set_value",
                                                                          arguments='{"instrument": "smu"}'))

    async def create(**request):
        return Items([chunk(content="Setting it."), chunk(finish, tool_calls=[call])])
    p = OpenAIProvider("oc", "OpenCode", "k", base_url="https://opencode.example/v1", preset="opencode")
    p.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result = asyncio.run(p.stream("m", "sys", [{"role": "user", "text": "set the gate"}], [], quiet))
    assert result.stop_reason in ("max_tokens", "refusal") and result.note
    assert result.blocks == [{"type": "text", "text": "Setting it."}]
    sent = to_messages("sys", next_request("oc", result))
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"] and not sent[2].get("tool_calls")


def gemini(finish, parts):
    """Gemini's answer: one chunk with this finish reason and these parts (None: no content at all)."""
    from google.genai import types
    from labharness.web.providers import gemini_provider as gp

    class Aio:
        def __init__(self):
            self.models = self

        async def generate_content_stream(self, model, contents, config):
            return Items([types.GenerateContentResponse(candidates=[types.Candidate(
                finish_reason=finish, content=types.Content(role="model", parts=parts) if parts is not None else None)])])
    p = gp.GeminiProvider(api_key="test")
    p.client = SimpleNamespace(aio=Aio())
    return asyncio.run(p.stream("gemini-x", "sys", [{"role": "user", "text": "write the fit script"}], [], quiet))


def test_gemini():
    pytest.importorskip("google.genai")
    from google.genai import types
    from labharness.web.providers.gemini_provider import to_contents

    def sent(result):
        return [c.model_dump(mode="json", exclude_none=True) for c in to_contents(next_request("gemini", result))]
    result = gemini(types.FinishReason.MAX_TOKENS, [types.Part(text="Saving."), types.Part(
        function_call=types.FunctionCall(name="write_file", args={"path": "fit.py"}))])
    assert result.stop_reason == "max_tokens" and not result.tool_calls and result.raw["parts"] == [{"text": "Saving."}]
    assert [c["parts"] for c in sent(result)][1] == [{"text": "Saving."}]
    result = gemini(types.FinishReason.MALFORMED_FUNCTION_CALL, None)       # a turn with nothing in it
    assert result.blocks == [] and result.raw is None
    assert result.note == ("The model gave no answer (the tool call it wrote was malformed). Say “continue” to try "
                           "again.")
    assert [c["role"] for c in sent(result)] == ["user"] and all(c["parts"] for c in sent(result))
    result = gemini(types.FinishReason.STOP, [])
    assert result.raw is None and result.note == "The model gave no answer. Say “continue” to try again."
    result = gemini(types.FinishReason.STOP, [types.Part(text="All idle.")])  # an ordinary answer: as before
    assert result.stop_reason == "end" and not result.note and result.raw["parts"] == [{"text": "All idle."}]
