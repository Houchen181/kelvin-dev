"""Claude, through the official Anthropic SDK (streaming Messages API with client tools)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import anthropic

from .base import Emit, ModelInfo, Provider, TurnResult, safe_id, user_text

MODELS = [
    ("claude-opus-5-5", "Claude Opus 5.5"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
    ("claude-haiku-5-5", "Claude Haiku 5.5"),
    ("claude-fable-5-1", "Claude Fable 5.1"),
]
# Server-side refusal fallback ("default" routing) for the models that support it.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_JSON_RETRIES = 2


def sanitize(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Make a returned turn safe to echo back. Before the last fallback boundary only
    text survives (thinking and tool_use there belong to the model that declined);
    the fallback marker itself is dropped."""
    last = max((i for i, b in enumerate(raw) if b.get("type") == "fallback"), default=-1)
    return [b for i, b in enumerate(raw)
            if b.get("type") != "fallback" and (i > last or b.get("type") == "text")]


def to_messages(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    n = 0
    for m in history:
        if m["role"] == "user":
            out.append({"role": "user", "content": [{"type": "text", "text": user_text(m)}]})
        elif m["role"] == "assistant":
            if m.get("provider") == "anthropic" and m.get("raw"):
                content = m["raw"]
            else:
                content = []
                for b in m["blocks"]:
                    if b["type"] == "text" and b["text"]:
                        content.append({"type": "text", "text": b["text"]})
                    elif b["type"] == "tool_call":
                        n += 1
                        content.append({"type": "tool_use", "id": safe_id(b["id"], n), "name": b["name"],
                                        "input": b["input"] if isinstance(b["input"], dict) else {}})
            if content:
                out.append({"role": "assistant", "content": content})
        elif m["role"] == "tool":
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": safe_id(r["id"], 0), "content": r["content"] or "(empty)",
                 "is_error": bool(r["is_error"])} for r in m["results"]]})
    return out


class AnthropicProvider(Provider):
    name = "anthropic"
    label = "Claude"
    can_search = True

    def __init__(self, api_key: Optional[str]):
        self.client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else None

    async def search(self, model: str, query: str, limit: int = 8) -> List[Dict[str, str]]:
        """Claude's web search (a server tool: Anthropic runs the searches). The results it found, with Claude's
        notes on those worth opening first."""
        from ..websearch import PROMPT, merge, results_in
        message = await self.client.messages.create(
            model=model, max_tokens=4000,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=[{"role": "user", "content": PROMPT.format(query=query, limit=limit)}])
        hits: List[Dict[str, str]] = []
        words = []
        for block in message.content:
            if block.type == "web_search_tool_result":
                if not isinstance(block.content, list):   # the search itself failed
                    raise RuntimeError("Claude's web search: %s" % getattr(block.content, "error_code", "failed"))
                hits += [{"title": r.title or "", "url": r.url, "snippet": ""} for r in block.content
                         if getattr(r, "type", "") == "web_search_result"]
            elif block.type == "text":
                words.append(block.text)
        return merge(results_in("".join(words)), hits, limit)

    def available(self) -> bool:
        return self.client is not None

    async def models(self) -> List[ModelInfo]:
        return [ModelInfo(mid, label, self.name) for mid, label in MODELS]

    async def stream(self, model: str, system: str, history: List[Dict[str, Any]],
                     tools: List[Dict[str, Any]], emit: Emit) -> TurnResult:
        kwargs: Dict[str, Any] = dict(
            model=model,
            max_tokens=64000,
            system=system,
            messages=to_messages(history),
            tools=[{"name": t["name"], "description": t["description"], "input_schema": t["schema"],
                    "eager_input_streaming": True} for t in tools],
            cache_control={"type": "ephemeral"},
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": "medium"},
        )
        if model in FALLBACK_MODELS:
            kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
        retries = 0
        while True:
            try:
                async with self.client.beta.messages.stream(**kwargs) as stream:
                    async for event in stream:
                        if event.type == "content_block_delta":
                            if event.delta.type == "text_delta":
                                await emit({"type": "text", "delta": event.delta.text})
                            elif event.delta.type == "thinking_delta":
                                await emit({"type": "thinking", "delta": event.delta.thinking})
                    message = await stream.get_final_message()
                break
            except ValueError:
                # Tool-call JSON the SDK could not parse at all: re-issue the turn (bounded).
                retries += 1
                if retries > MAX_JSON_RETRIES:
                    raise
                await emit({"type": "reset"})

        raw = sanitize([b.to_dict(exclude_none=True) for b in message.content])
        blocks: List[Dict[str, Any]] = []
        for b in raw:
            if b["type"] == "text":
                blocks.append({"type": "text", "text": b["text"]})
            elif b["type"] == "thinking" and b.get("thinking"):
                blocks.append({"type": "thinking", "text": b["thinking"]})
            elif b["type"] == "tool_use":
                blocks.append({"type": "tool_call", "id": b["id"], "name": b["name"],
                               "input": b.get("input") if isinstance(b.get("input"), dict) else None})
        result = TurnResult(blocks, raw, model=message.model)
        stop = message.stop_reason
        if stop == "refusal":
            result.stop_reason = "refusal"
            result.blocks = [b for b in blocks if b["type"] != "tool_call"]
            result.note = "The model declined this request."
        elif stop == "max_tokens":
            result.stop_reason = "max_tokens"
            result.blocks = [b for b in blocks if b["type"] != "tool_call"]  # a truncated tool input must not run
            result.note = "The answer hit the length limit."
        elif stop == "pause_turn":
            result.stop_reason = "pause"
        elif result.tool_calls:
            result.stop_reason = "tool_calls"
        return result
