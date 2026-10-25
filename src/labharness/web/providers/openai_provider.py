"""GPT models, local models behind any OpenAI-compatible server (Ollama, vLLM, LM Studio), and other services that
speak the same API (OpenRouter, OpenCode, DeepSeek, Groq, Mistral, xAI, gateways; see services.py).

Uses the Chat Completions API, which all of them speak.
"""
from __future__ import annotations

import asyncio
import json
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional

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


MAX_LISTED = 60   # a gateway can list hundreds; the newest that fit are shown, and any can be named in Settings


def _fetch_json(url: str, api_key: Optional[str]) -> Any:
    from ..._version import __version__
    headers = {"User-Agent": "Kelvin/%s" % __version__, "Accept": "application/json"}   # some refuse Python's own
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


class OpenAIProvider(Provider):
    def __init__(self, name: str, label: str, api_key: Optional[str], base_url: Optional[str] = None,
                 models: Optional[List[str]] = None, models_url: Optional[str] = None, exclude: Iterable[str] = (),
                 tools_only: bool = False, headers: Optional[Dict[str, str]] = None,
                 fetch: Callable[[str, Optional[str]], Any] = _fetch_json):
        self.name, self.label = name, label
        self._static = [m for m in (models or []) if m]
        self.models_url = models_url
        self.exclude = tuple(e.lower() for e in exclude)
        self.tools_only = tools_only
        self._key = api_key
        self._fetch = fetch
        self.client = None
        if api_key or base_url:
            self.client = openai.AsyncOpenAI(api_key=api_key or "local", base_url=base_url or None,
                                             default_headers=headers or None)
        self._cache: Optional[List[ModelInfo]] = None
        self.can_search = name == "openai" and not base_url   # OpenAI's own web search (local services have none)

    async def search(self, model: str, query: str, limit: int = 8) -> List[Dict[str, str]]:
        """OpenAI's web search (the Responses API's web_search tool): the pages it cited, with its notes."""
        from ..websearch import PROMPT, merge, results_in
        response = await self.client.responses.create(model=model, tools=[{"type": "web_search"}],
                                                      input=PROMPT.format(query=query, limit=limit))
        hits: List[Dict[str, str]] = []
        for item in response.output or []:
            if getattr(item, "type", "") != "message":
                continue
            for part in getattr(item, "content", None) or []:
                for a in getattr(part, "annotations", None) or []:
                    if getattr(a, "type", "") == "url_citation":
                        hits.append({"title": a.title or "", "url": a.url, "snippet": ""})
        return merge(results_in(response.output_text or ""), hits, limit)

    def available(self) -> bool:
        return self.client is not None

    async def models(self) -> List[ModelInfo]:
        if self._static:
            return [ModelInfo(m, m, self.name) for m in self._static]
        if self._cache is None:
            if self.models_url:   # some services list their models somewhere else than <base>/models
                data = await asyncio.to_thread(self._fetch, self.models_url, self._key)
                listed = [type("M", (), m) for m in (data.get("data") if isinstance(data, dict) else data) or []
                          if isinstance(m, dict) and m.get("id")]
            else:
                listed = [m async for m in self.client.models.list()]
            found = []
            for m in listed:
                mid = m.id
                if self.name == "openai" and (not mid.startswith(("gpt-", "o1", "o3", "o4", "o5", "chatgpt"))
                                              or any(s in mid for s in _SKIP)):
                    continue
                if self.exclude and mid.split("/")[-1].lower().startswith(self.exclude):
                    continue
                supported = getattr(m, "supported_parameters", None)
                if self.tools_only and supported is not None and "tools" not in supported:
                    continue   # Kelvin works through tools; a model that cannot call them is no use here
                found.append((getattr(m, "created", 0) or 0, mid))
            ordered = sorted(found, reverse=True)
            if self.name not in ("openai", "local"):
                ordered = ordered[:MAX_LISTED]
            self._cache = [ModelInfo(mid, mid, self.name) for _, mid in ordered]
        return self._cache

    async def stream(self, model: str, system: str, history: List[Dict[str, Any]],
                     tools: List[Dict[str, Any]], emit: Emit) -> TurnResult:
        request: Dict[str, Any] = {"model": model, "messages": to_messages(system, history), "stream": True}
        if tools:   # an empty list is refused by OpenAI and others: leave it out (e.g. for a second opinion)
            request["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                                  "parameters": t["schema"]}} for t in tools]
        response = await self.client.chat.completions.create(**request)
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
