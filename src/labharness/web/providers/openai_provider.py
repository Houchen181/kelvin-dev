"""GPT models, and local models behind any OpenAI-compatible server (Ollama, vLLM, LM Studio).

Uses the Chat Completions API, which OpenAI and the local servers all speak.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import openai

from .base import Emit, ModelInfo, Provider, TurnResult, safe_id, text_of, user_text

_SKIP = ("audio", "realtime", "tts", "transcribe", "image", "search", "embedding", "instruct", "moderation",
         "dall-e", "whisper", "davinci", "babbage")


def to_messages(system: str, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = [{"role": "system", "content": system}]
    n = 0
    for m in history:
        if m["role"] == "user":
            out.append({"role": "user", "content": user_text(m)})
        elif m["role"] == "assistant":
            calls = []
            for b in m["blocks"]:
                if b["type"] == "tool_call":
                    n += 1
                    calls.append({"id": safe_id(b["id"], n), "type": "function",
                                  "function": {"name": b["name"], "arguments": json.dumps(b["input"] or {})}})
            text = text_of(m["blocks"])
            if not text and not calls:
                continue
            msg: Dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        elif m["role"] == "tool":
            for r in m["results"]:
                out.append({"role": "tool", "tool_call_id": safe_id(r["id"], 0), "content": r["content"] or "(empty)"})
    return out


class OpenAIProvider(Provider):
    def __init__(self, name: str, label: str, api_key: Optional[str], base_url: Optional[str] = None,
                 models: Optional[List[str]] = None):
        self.name, self.label = name, label
        self._static = [m for m in (models or []) if m]
        self.client = None
        if api_key or base_url:
            self.client = openai.AsyncOpenAI(api_key=api_key or "local", base_url=base_url or None)
        self._cache: Optional[List[ModelInfo]] = None

    def available(self) -> bool:
        return self.client is not None

    async def models(self) -> List[ModelInfo]:
        if self._static:
            return [ModelInfo(m, m, self.name) for m in self._static]
        if self._cache is None:
            found = []
            async for m in self.client.models.list():
                mid = m.id
                if self.name == "openai" and (not mid.startswith(("gpt-", "o1", "o3", "o4", "o5", "chatgpt"))
                                              or any(s in mid for s in _SKIP)):
                    continue
                found.append((getattr(m, "created", 0) or 0, mid))
            self._cache = [ModelInfo(mid, mid, self.name) for _, mid in sorted(found, reverse=True)]
        return self._cache

    async def stream(self, model: str, system: str, history: List[Dict[str, Any]],
                     tools: List[Dict[str, Any]], emit: Emit) -> TurnResult:
        response = await self.client.chat.completions.create(
            model=model,
            messages=to_messages(system, history),
            tools=[{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                     "parameters": t["schema"]}} for t in tools],
            stream=True,
        )
        text: List[str] = []
        calls: Dict[int, Dict[str, str]] = {}
        finish = None
        answered_by = model
        async for chunk in response:
            answered_by = getattr(chunk, "model", None) or answered_by
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta is not None and delta.content:
                text.append(delta.content)
                await emit({"type": "text", "delta": delta.content})
            for tc in (delta.tool_calls or []) if delta is not None else []:
                call = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                if tc.id:
                    call["id"] = tc.id
                if tc.function is not None:
                    if tc.function.name:
                        call["name"] = tc.function.name
                    if tc.function.arguments:
                        call["args"] += tc.function.arguments
            if choice.finish_reason:
                finish = choice.finish_reason

        blocks: List[Dict[str, Any]] = [{"type": "text", "text": "".join(text)}] if text else []
        for i in sorted(calls):
            c = calls[i]
            try:
                args = json.loads(c["args"] or "{}")
                args = args if isinstance(args, dict) else None
            except ValueError:
                args = None
            blocks.append({"type": "tool_call", "id": c["id"] or "lh_call_%d" % i, "name": c["name"], "input": args})
        result = TurnResult(blocks, None, model=answered_by)
        if finish == "length":
            result.stop_reason, result.note = "max_tokens", "The answer hit the length limit."
            result.blocks = [b for b in blocks if b["type"] != "tool_call"]
        elif finish == "content_filter":
            result.stop_reason, result.note = "refusal", "The model declined this request."
        elif result.tool_calls:
            result.stop_reason = "tool_calls"
        return result
