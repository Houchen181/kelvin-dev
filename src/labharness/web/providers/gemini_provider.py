"""Gemini, through the official google-genai SDK."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from google import genai
from google.genai import types

from .base import Emit, ModelInfo, Provider, TurnResult, text_of, user_text

_SKIP = ("image", "tts", "embedding", "live", "audio", "aqa", "learnlm")
_OWN_ID = "lh_call_"


def to_contents(history: List[Dict[str, Any]]) -> List[types.Content]:
    out: List[types.Content] = []
    for m in history:
        if m["role"] == "user":
            out.append(types.Content(role="user", parts=[types.Part(text=user_text(m))]))
        elif m["role"] == "assistant":
            if m.get("provider") == "gemini" and m.get("raw"):
                out.append(types.Content.model_validate(m["raw"]))  # keeps thought signatures
                continue
            parts = []
            text = text_of(m["blocks"])
            if text:
                parts.append(types.Part(text=text))
            for b in m["blocks"]:
                if b["type"] == "tool_call":
                    parts.append(types.Part(function_call=types.FunctionCall(name=b["name"], args=b["input"] or {})))
            if parts:
                out.append(types.Content(role="model", parts=parts))
        elif m["role"] == "tool":
            parts = []
            for r in m["results"]:
                key = "error" if r["is_error"] else "result"
                call_id = None if str(r["id"]).startswith(_OWN_ID) else r["id"]
                parts.append(types.Part(function_response=types.FunctionResponse(
                    id=call_id, name=r["name"], response={key: r["content"]})))
            out.append(types.Content(role="user", parts=parts))
    return out


class GeminiProvider(Provider):
    name = "gemini"
    label = "Gemini"
    can_search = True

    def __init__(self, api_key: Optional[str]):
        self.client = genai.Client(api_key=api_key) if api_key else None
        self._cache: Optional[List[ModelInfo]] = None

    async def search(self, model: str, query: str, limit: int = 8) -> List[Dict[str, str]]:
        """Gemini with Google Search: the pages its answer is grounded on (addresses through Google's redirect,
        which read_web_page follows), then those it named."""
        from ..websearch import PROMPT, merge, results_in
        response = await self.client.aio.models.generate_content(
            model=model, contents=PROMPT.format(query=query, limit=limit),
            config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]))
        hits: List[Dict[str, str]] = []
        for candidate in response.candidates or []:
            meta = candidate.grounding_metadata
            for chunk in (meta.grounding_chunks or []) if meta is not None else []:
                web = chunk.web
                if web is not None and web.uri:
                    hits.append({"title": web.title or web.domain or "", "url": web.uri, "snippet": ""})
        return merge(hits, results_in(response.text or ""), limit)

    def available(self) -> bool:
        return self.client is not None

    async def models(self) -> List[ModelInfo]:
        if self._cache is None:
            found = []
            async for m in await self.client.aio.models.list():
                name = (m.name or "").split("/")[-1]
                actions = m.supported_actions or []
                if name.startswith("gemini") and "generateContent" in actions and not any(s in name for s in _SKIP):
                    found.append(ModelInfo(name, m.display_name or name, self.name))
            self._cache = sorted(found, key=lambda x: x.id, reverse=True)
        return self._cache

    async def stream(self, model: str, system: str, history: List[Dict[str, Any]],
                     tools: List[Dict[str, Any]], emit: Emit) -> TurnResult:
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t["name"], description=t["description"],
                                          parameters_json_schema=t["schema"]) for t in tools])],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        parts: List[types.Part] = []
        finish = None
        async for chunk in await self.client.aio.models.generate_content_stream(
                model=model, contents=to_contents(history), config=config):
            cand = chunk.candidates[0] if chunk.candidates else None
            if cand is None:
                continue
            if cand.finish_reason:
                finish = str(cand.finish_reason.value if hasattr(cand.finish_reason, "value") else cand.finish_reason)
            for p in (cand.content.parts if cand.content and cand.content.parts else []):
                parts.append(p)
                if p.text and not p.thought:
                    await emit({"type": "text", "delta": p.text})
                elif p.text and p.thought:
                    await emit({"type": "thinking", "delta": p.text})

        content = types.Content(role="model", parts=parts)
        raw = content.model_dump(mode="json", exclude_none=True)
        blocks: List[Dict[str, Any]] = []
        text = "".join(p.text for p in parts if p.text and not p.thought)
        thought = "".join(p.text for p in parts if p.text and p.thought)
        if thought:
            blocks.append({"type": "thinking", "text": thought})
        if text:
            blocks.append({"type": "text", "text": text})
        n = 0
        for p in parts:
            if p.function_call is not None:
                n += 1
                args = p.function_call.args
                blocks.append({"type": "tool_call", "id": p.function_call.id or "%s%d" % (_OWN_ID, n),
                               "name": p.function_call.name, "input": dict(args) if isinstance(args, dict) else {}})
        result = TurnResult(blocks, raw, model=model)
        if finish == "MAX_TOKENS":
            result.stop_reason, result.note = "max_tokens", "The answer hit the length limit."
            result.blocks = [b for b in blocks if b["type"] != "tool_call"]
        elif finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"):
            result.stop_reason, result.note = "refusal", "The model declined this request."
            result.blocks = [b for b in blocks if b["type"] != "tool_call"]
        elif result.tool_calls:
            result.stop_reason = "tool_calls"
        return result
