"""GPT models, local models behind any OpenAI-compatible server (Ollama, vLLM, LM Studio), and other services that
speak the same API (OpenRouter, OpenCode, DeepSeek, Groq, Mistral, xAI, gateways; see services.py).

OpenAI's own models (an API key, here or through the Kelvin hub) go through the Responses API, which carries their
reasoning from one step to the next: encrypted, sent again to the model that wrote it, nothing stored at OpenAI
(responses.py, as on the ChatGPT plan). The others use the Chat Completions API, which all of them speak, and which
carries no reasoning between tool calls. Pictures go to OpenAI's own models, and to other services' models that see
them, model by model (vision.py); one that refuses a picture gets the message without it.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional

import openai

from . import responses
from .base import (DOCUMENTS_NOTE, EFFORT, PICTURES_NOTE, Emit, ModelInfo, Provider, TurnResult, data_url,
                   opencode_session, safe_id, session_key, text_of, user_text)

_SKIP = ("audio", "realtime", "tts", "transcribe", "image", "search", "embedding", "instruct", "moderation",
         "dall-e", "whisper", "davinci", "babbage")
# OpenCode's services, which route by conversation and send x-opencode-session (base.opencode_session): by their
# preset (services.py), or by their address for one added as another service
OPENCODE = ("opencode", "opencode-go")
OPENCODE_HOST = "opencode.ai"
REFUSALS = (400, 404, 415, 422)   # how services say a model takes no pictures (OpenRouter: 404, no such endpoint)
# what such a refusal says (DeepSeek: unknown variant `image_url`; vLLM: not a multimodal model), unlike a request
# that was only too long: a model is remembered as seeing no pictures only for these
PICTURE_WORDS = ("image", "vision", "multimodal", "multi-modal", "modalit", "picture", "visual")
REFUSED_NOTE = "This model can't see pictures, so they weren't shown to it."
LEFT_OUT_NOTE = "The service refused this message with its pictures, so they weren't shown to the model this time."


def _has_pictures(messages: List[Dict[str, Any]]) -> bool:
    return any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"])
               for m in messages)


def _about_pictures(e: Exception) -> bool:
    said = "%s %s" % (e, json.dumps(getattr(e, "body", None), default=str))
    return any(w in said.lower() for w in PICTURE_WORDS)


def _without_pictures(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The request again for a model that refused its pictures: each message says how many it doesn't show."""
    out = []
    for m in messages:
        parts = m.get("content")
        n = sum(1 for p in parts if p.get("type") == "image_url") if isinstance(parts, list) else 0
        if n:
            kept = [p for p in parts if p.get("type") != "image_url"] + [{"type": "text", "text": "(%s not shown: "
                    "the model didn't take pictures)" % ("a picture" if n == 1 else "%d pictures" % n)}]
            m = dict(m, content=kept if any(p.get("type") != "text" for p in kept)
                     else "\n\n".join(p["text"] for p in kept))
        out.append(m)
    return out


def _file(doc: Dict[str, str]) -> Dict[str, Any]:
    return {"type": "file", "file": {"filename": doc.get("name") or "document.pdf", "file_data": data_url(doc)}}


def to_messages(system: str, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = [{"role": "system", "content": system}]
    n = 0
    for m in history:
        if m["role"] == "user":
            images, docs = m.get("images") or [], m.get("documents") or []
            out.append({"role": "user", "content": [{"type": "text", "text": user_text(m)}] + [
                {"type": "image_url", "image_url": {"url": data_url(i)}} for i in images] + [_file(d) for d in docs]
                if images or docs else user_text(m)})
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
            images = [i for r in m["results"] for i in r.get("images") or []]
            docs = [d for r in m["results"] for d in r.get("documents") or []]
            parts: List[Dict[str, Any]] = []
            if images:   # a tool message holds text only: its pictures and PDFs follow as the user's
                parts += [{"type": "text", "text": PICTURES_NOTE}] + [
                    {"type": "image_url", "image_url": {"url": data_url(i)}} for i in images]
            if docs:
                parts += [{"type": "text", "text": DOCUMENTS_NOTE}] + [_file(d) for d in docs]
            if parts:
                out.append({"role": "user", "content": parts})
    return out


MAX_LISTED = 60   # a gateway can list hundreds; the newest that fit are shown, and any can be named in Settings


def _fetch_json(url: str, api_key: Optional[str], extra: Optional[Dict[str, str]] = None) -> Any:
    """GET a service's JSON (its list of models). extra: headers it wants beyond the key (OpenCode's session)."""
    from ..._version import __version__
    headers = {"User-Agent": "Kelvin/%s" % __version__, "Accept": "application/json"}   # some refuse Python's own
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
    headers.update(extra or {})
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:   # the service's own words, for modelerrors (not "HTTP Error 403")
        try:
            e.body = json.loads(e.read())
        except (ValueError, OSError):
            pass
        raise


class OpenAIProvider(Provider):
    def __init__(self, name: str, label: str, api_key: Optional[str], base_url: Optional[str] = None,
                 models: Optional[List[str]] = None, models_url: Optional[str] = None, exclude: Iterable[str] = (),
                 tools_only: bool = False, headers: Optional[Dict[str, str]] = None,
                 fetch: Callable[[str, Optional[str], Dict[str, str]], Any] = _fetch_json, preset: str = "",
                 pictures: str = "", vision: Any = None, api: str = ""):
        """preset: the service Kelvin knows it as (services.py; for the hub's list of models that see pictures).
        pictures: the person's switch for the service, "on" or "off" ("": model by model). vision: vision.Vision.
        api: "openai" for OpenAI's own API at another address (through the Kelvin hub: hubmodels.py).
        fetch(url, key, headers): reads the list of models at models_url."""
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
        self.own = api == "openai" or (name == "openai" and not base_url)   # OpenAI's own API, here or via the hub
        self.can_search = self.own    # OpenAI's own web search (local services have none)
        self.sees_images = self.own   # all of OpenAI's own models; another service's, model by model
        self.sees_pdfs = self.sees_images                    # OpenAI's models read PDFs as files
        self.preset = preset or name
        self.pictures = pictures if pictures in ("on", "off") else ""
        self.vision = vision
        self._inputs: Dict[str, bool] = {}   # what the service's own list says: does the model take pictures?
        self.no_include: set = set()   # OpenAI's own models that give no encrypted reasoning back (responses.relax)
        self.no_effort: set = set()    # and those that take no reasoning effort (one refusing xhigh takes high)
        host = (urllib.parse.urlparse(base_url or "").hostname or "").lower()
        # an OpenCode service's session for its requests outside any chat (each chat has its own: extra_headers)
        self._outside = secrets.token_hex(12) if self.preset in OPENCODE or host == OPENCODE_HOST \
            or host.endswith("." + OPENCODE_HOST) else ""

    def extra_headers(self) -> Dict[str, str]:
        """Headers a service wants with each request beyond its key: an OpenCode service's session, one per chat as
        the hub's OpenCode connections send (providers/hub.py), and this connection's own outside any chat."""
        return opencode_session(self._outside) if self._outside else {}

    def model_sees_images(self, model: str) -> bool:
        """Whether Kelvin sends this model pictures (vision.py): unknown ones get them, and fall back if they refuse."""
        if self.sees_images or self.pictures:
            return self.sees_images or self.pictures == "on"
        if self.vision is not None and self.vision.refused(self.name, model):
            return False
        if model in self._inputs:
            return self._inputs[model]
        return (self.vision.hub_says(self.preset, model) if self.vision is not None else None) is not False

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

    async def _listed(self) -> List[Any]:
        """The service's own list of models: each with its id, and what else the service says of it."""
        extra = self.extra_headers()
        if self.models_url:   # some services list their models somewhere else than <base>/models
            data = await asyncio.to_thread(self._fetch, self.models_url, self._key, extra)
            return [type("M", (), m) for m in (data.get("data") if isinstance(data, dict) else data) or []
                    if isinstance(m, dict) and m.get("id")]
        return [m async for m in self.client.models.list(**({"extra_headers": extra} if extra else {}))]

    async def not_offered(self) -> List[Dict[str, str]]:
        """The model names typed for this service (Settings) that its own list lacks, each with the nearest it has
        (services.not_offered); none when no name was typed or the list is empty. Raises when the list can't be read."""
        from ..services import not_offered
        if not self._static:
            return []
        offered = [m.id for m in await self._listed()]
        return not_offered(self._static, offered) if offered else []

    async def models(self) -> List[ModelInfo]:
        if self._static:
            return [ModelInfo(m, m, self.name) for m in self._static]
        if self._cache is None:
            found = []
            for m in await self._listed():
                mid = m.id
                if self.own and (not mid.startswith(("gpt-", "o1", "o3", "o4", "o5", "chatgpt"))
                                              or any(s in mid for s in _SKIP)):
                    continue
                if self.exclude and mid.split("/")[-1].lower().startswith(self.exclude):
                    continue
                supported = getattr(m, "supported_parameters", None)
                if self.tools_only and supported is not None and "tools" not in supported:
                    continue   # Kelvin works through tools; a model that cannot call them is no use here
                arch = getattr(m, "architecture", None)   # OpenRouter's: the inputs each model takes
                inputs = arch.get("input_modalities") if isinstance(arch, dict) else None
                if isinstance(inputs, list):
                    self._inputs[mid] = "image" in inputs
                found.append((getattr(m, "created", 0) or 0, mid))
            ordered = sorted(found, reverse=True)
            if not self.own and self.name != "local":
                ordered = ordered[:MAX_LISTED]
            self._cache = [ModelInfo(mid, mid, self.name) for _, mid in ordered]
        return self._cache

    async def stream(self, model: str, system: str, history: List[Dict[str, Any]],
                     tools: List[Dict[str, Any]], emit: Emit) -> TurnResult:
        if self.own:   # OpenAI's own models: their reasoning goes with the conversation
            return await self._respond(model, system, history, tools, emit)
        request: Dict[str, Any] = {"model": model, "messages": to_messages(system, history), "stream": True}
        if tools:   # an empty list is refused by OpenAI and others: leave it out (e.g. for a second opinion)
            request["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                                  "parameters": t["schema"]}} for t in tools]
        extra = self.extra_headers()
        if extra:
            request["extra_headers"] = extra
        key = session_key() if self.own else ""
        if key:   # OpenAI's own: a chat's requests go to the cache holding its prompt (from GPT-5.6, one of its own)
            request["extra_body"] = {"prompt_cache_key": key}
        left_out = ""
        try:
            response = await self.client.chat.completions.create(**request)
        except openai.APIStatusError as e:   # a model that takes no pictures: the same request without them
            if e.status_code not in REFUSALS or self.sees_images or not _has_pictures(request["messages"]):
                raise
            request["messages"] = _without_pictures(request["messages"])
            response = await self.client.chat.completions.create(**request)   # its own error, if it fails too
            left_out = LEFT_OUT_NOTE
            if _about_pictures(e):   # it said so: remembered, so the model gets captions from now on
                left_out = REFUSED_NOTE
                if self.vision is not None:
                    self.vision.refuse(self.name, model)
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
            result.blocks = [b for b in blocks if b["type"] != "tool_call"]   # never run, so never sent again
        elif result.tool_calls:
            result.stop_reason = "tool_calls"
        if left_out:
            result.note = "%s %s" % (result.note, left_out) if result.note else left_out
        return result

    async def _respond(self, model: str, system: str, history: List[Dict[str, Any]],
                       tools: List[Dict[str, Any]], emit: Emit) -> TurnResult:
        """OpenAI's own models, through the Responses API (responses.py): the whole conversation each time, nothing
        stored at OpenAI (store=false), each answer's reasoning back encrypted and sent again to the model that wrote
        it, the turn's effort as its reasoning effort, PDFs whole, and a chat's requests to the cache holding its
        prompt (prompt_cache_key). What a model refuses of the reasoning is dropped, and remembered for that model."""
        body: Dict[str, Any] = {"model": model, "instructions": system, "store": False, "stream": True,
                                "input": responses.to_input(history, self.name, model, documents=self.sees_pdfs)}
        if tools:   # an empty list: left out (a summary's, a second opinion's)
            # not strict: strict, the Responses API would make every optional argument a must (or null, which a lab
            # tool refuses)
            body["tools"] = [{"type": "function", "name": t["name"], "description": t["description"],
                              "parameters": t["schema"], "strict": False} for t in tools]
        if model not in self.no_include:
            body["include"] = list(responses.INCLUDE)
        effort = EFFORT.get()
        if model not in self.no_effort and effort in responses.EFFORTS:
            body["reasoning"] = {"effort": effort}
        key = session_key()
        if key:   # a chat's requests go to the cache holding its prompt (from GPT-5.6, one of its own)
            body["prompt_cache_key"] = key
        extra = self.extra_headers()
        if extra:
            body["extra_headers"] = extra
        for _ in range(5):   # each refusal takes one thing away, once
            try:
                events = await self.client.responses.create(**body)
                break
            except openai.BadRequestError as e:
                if not responses.relax(body, e, model, self.no_effort, self.no_include):
                    raise
        else:
            raise RuntimeError("%s refused the request in every form Kelvin knows." % self.label)
        final, incomplete = None, False
        async for ev in events:
            kind = getattr(ev, "type", "")
            if kind == "response.output_text.delta":
                await emit({"type": "text", "delta": ev.delta})
            elif kind == "response.completed":
                final = ev.response
            elif kind == "response.incomplete":
                final, incomplete = ev.response, True
            elif kind in ("response.failed", "error"):
                err = getattr(getattr(ev, "response", None), "error", None) or ev
                said = getattr(err, "message", None) or (err.get("message") if isinstance(err, dict) else None)
                raise RuntimeError("%s could not answer: %s" % (self.label, said or "unknown error"))
        if final is None:
            raise RuntimeError("%s's answer stopped before it was complete; try again." % self.label)
        blocks, raw, declined = responses.read(getattr(final, "output", None), model)
        result = TurnResult(blocks, raw, model=getattr(final, "model", None) or model)
        why = getattr(getattr(final, "incomplete_details", None), "reason", None) if incomplete else None
        if declined or why == "content_filter":
            result.stop_reason, result.note = "refusal", "The model declined this request."
        elif incomplete:
            result.stop_reason, result.note = "max_tokens", "The answer hit the length limit."
        elif result.tool_calls:
            result.stop_reason = "tool_calls"
        if result.stop_reason in ("refusal", "max_tokens"):   # a call that never ran is never sent again, nor the
            result.blocks = [b for b in blocks if b["type"] != "tool_call"]   # reasoning that led to it
            result.raw = responses.without_calls(raw)
        return result
