"""ChatGPT models on the user's ChatGPT plan (Sign in with ChatGPT; see ../chatgpt.py).

Requests go to the public Responses API with the account's access token, store=false and stream=true, and carry the
whole conversation in `input` each time (the route keeps no server-side history). The system prompt goes in
`instructions`; parameters the route refuses (temperature, max_output_tokens, previous_response_id...) are never sent.
Function tools are offered grouped in a namespace, as the route asks; if it refuses that form, plain function tools are
tried instead and the form that works is remembered.

The model's reasoning goes with the conversation, as Codex sends it: each answer's reasoning comes back encrypted
(include reasoning.encrypted_content, since nothing is stored) and is sent again with the turns after it, so a step
that follows a tool call builds on what the model thought before the call instead of starting over. The turn's
effort (Settings → Models → Thinking) goes as the reasoning effort. Either one the route refuses is dropped, and
remembered."""
from __future__ import annotations

import asyncio
import json
import urllib.request
from typing import Any, Dict, List, Optional

import openai

from ..chatgpt import MANAGE_USAGE, RESOURCE, NotSignedIn, SignIn
from .base import (EFFORT, PICTURES_NOTE, Emit, ModelInfo, Provider, TurnResult, data_url, safe_id, text_of,
                   user_text)

NAMESPACE = "kelvin"
LIMITS = {"subscription_sharing_usage_limit_exceeded": "Your ChatGPT plan's usage limit is reached for now.",
          "subscription_sharing_usage_unavailable": "ChatGPT plan usage is not available for this account right now."}


def _plain(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        return item
    dump = getattr(item, "model_dump", None)
    return dump(exclude_none=True) if dump else dict(vars(item))


def to_input(history: List[Dict[str, Any]], provider: str, model: Optional[str] = None) -> List[Dict[str, Any]]:
    """model: the model answering now; reasoning goes back only to the model that wrote it (another can't read it)."""
    out: List[Dict[str, Any]] = []
    for m in history:
        if m["role"] == "user":
            images = m.get("images") or []
            out.append({"role": "user", "content": [{"type": "input_text", "text": user_text(m)}] + [
                {"type": "input_image", "image_url": data_url(i)} for i in images] if images else user_text(m)})
        elif m["role"] == "assistant":
            if m.get("provider") == provider and isinstance(m.get("raw"), list):
                for item in m["raw"]:   # our own earlier output, replayed as it was (without server ids)
                    if item.get("type") == "reasoning" and model and item.get("for_model") not in (None, model):
                        continue   # another model's reasoning (for_model: the model Kelvin asked, as the route names
                        # its answering model as it likes, a dated name say)
                    out.append({k: v for k, v in item.items() if k not in ("id", "status", "for_model")})
                continue
            text = text_of(m["blocks"])
            if text:
                out.append({"role": "assistant", "content": text})
            n = 0
            for b in m["blocks"]:
                if b["type"] == "tool_call":
                    n += 1
                    out.append({"type": "function_call", "call_id": safe_id(b["id"], n), "name": b["name"],
                                "arguments": json.dumps(b["input"] or {})})
        elif m["role"] == "tool":
            for r in m["results"]:
                out.append({"type": "function_call_output", "call_id": safe_id(r["id"], 0),
                            "output": r["content"] or "(empty)"})
            images = [i for r in m["results"] for i in r.get("images") or []]
            if images:   # the pictures follow as the user's
                out.append({"role": "user", "content": [{"type": "input_text", "text": PICTURES_NOTE}] + [
                    {"type": "input_image", "image_url": data_url(i)} for i in images]})
    return out


def tool_list(tools: List[Dict[str, Any]], style: str) -> List[Dict[str, Any]]:
    functions = [{"type": "function", "name": t["name"], "description": t["description"], "parameters": t["schema"]}
                 for t in tools]
    if style == "namespace":
        return [{"type": "namespace", "name": NAMESPACE, "description": "Kelvin's lab tools", "tools": functions}]
    return functions


class ChatGPTProvider(Provider):
    name, label = "chatgpt", "ChatGPT plan"
    sees_images = True

    def __init__(self, signin: SignIn, client_factory: Any = None, http_get: Any = None):
        self.signin = signin
        self.tool_style = "namespace"
        self.no_include: set = set()   # the models the route gives no encrypted reasoning for
        self.no_effort: set = set()    # the models that refused any reasoning effort (one refusing xhigh takes high)
        self._client = client_factory or (lambda token: openai.AsyncOpenAI(api_key=token, base_url=RESOURCE,
                                                                           max_retries=0))
        self._get = http_get or self._get_json
        self._cache: Optional[List[ModelInfo]] = None

    def available(self) -> bool:
        return bool(self.signin.status()["plan"])

    @staticmethod
    def _get_json(url: str, token: str) -> Any:
        from ..._version import __version__
        req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % token, "Accept": "application/json",
                                                   "User-Agent": "Kelvin/%s" % __version__})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    async def models(self) -> List[ModelInfo]:
        if self._cache is None:
            token = await asyncio.to_thread(self.signin.access_token)
            data = await asyncio.to_thread(self._get, RESOURCE + "/models", token)
            items = (data.get("models") or data.get("data") or []) if isinstance(data, dict) else []
            self._cache = [ModelInfo(m.get("slug") or m["id"], m.get("display_name") or m.get("slug") or m["id"], self.name)
                           for m in items if isinstance(m, dict) and (m.get("slug") or m.get("id"))
                           and m.get("visibility", "list") == "list"]
        return self._cache

    async def stream(self, model: str, system: str, history: List[Dict[str, Any]],
                     tools: List[Dict[str, Any]], emit: Emit) -> TurnResult:
        try:
            token = await asyncio.to_thread(self.signin.access_token)
        except NotSignedIn as e:
            raise RuntimeError(str(e))
        client = self._client(token)
        body: Dict[str, Any] = {"model": model, "instructions": system, "input": to_input(history, self.name, model),
                                "store": False, "stream": True}
        if tools:
            body["tools"] = tool_list(tools, self.tool_style)
        if model not in self.no_include:
            body["include"] = ["reasoning.encrypted_content"]
        effort = EFFORT.get()
        if model not in self.no_effort and effort in ("low", "medium", "high", "xhigh"):
            body["reasoning"] = {"effort": effort}
        for _ in range(6):   # each refusal below takes one thing away, once
            try:
                events = await client.responses.create(**body)
                break
            except openai.BadRequestError as e:
                said = str(e).lower()
                param = str(getattr(e, "param", None) or "").lower()
                sent_back = any(i.get("type") == "reasoning" for i in body["input"])
                if sent_back and (param.startswith("input") or "encrypted" in said and not param.startswith("include")):
                    # earlier reasoning the route can't read now (another sign-in): this request without it; the turns
                    # after it carry their own
                    body["input"] = [i for i in body["input"] if i.get("type") != "reasoning"]
                elif "reasoning" in body and ("effort" in param or "effort" in said):
                    if body["reasoning"].get("effort") == "xhigh":
                        body["reasoning"] = {"effort": "high"}   # the deepest this model takes, for this request
                    else:
                        self.no_effort.add(model)   # this model takes no effort at all
                        del body["reasoning"]
                elif "include" in body and ("include" in param or "include" in said or "encrypted" in said):
                    self.no_include.add(model)   # the route gives this model's reasoning back to nobody
                    del body["include"]
                elif tools and self.tool_style == "namespace" and "tool" in said:
                    self.tool_style = "function"   # this route takes plain function tools after all
                    body["tools"] = tool_list(tools, self.tool_style)
                else:
                    raise
        else:
            raise RuntimeError("ChatGPT refused the request in every form Kelvin knows.")
        final = None
        incomplete = False
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
                code = getattr(err, "code", None) or (err.get("code") if isinstance(err, dict) else None)
                if code in LIMITS:
                    raise RuntimeError("%s Manage usage: %s" % (LIMITS[code], MANAGE_USAGE))
                raise RuntimeError("ChatGPT could not answer: %s" % (getattr(err, "message", None) or code or "unknown error"))
        if final is None:
            raise RuntimeError("ChatGPT's answer stopped before it was complete; try again.")
        blocks: List[Dict[str, Any]] = []
        raw: List[Dict[str, Any]] = []
        for item in getattr(final, "output", None) or []:
            item = _plain(item)
            kind = item.get("type")
            if kind == "message":
                text = "".join(c.get("text", "") for c in item.get("content") or [] if c.get("type") == "output_text")
                if text:
                    blocks.append({"type": "text", "text": text})
                    raw.append({"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": text}]})
            elif kind == "function_call":
                name = str(item.get("name") or "")
                if name.startswith(NAMESPACE + "."):
                    name = name[len(NAMESPACE) + 1:]
                try:
                    args = json.loads(item.get("arguments") or "{}")
                    args = args if isinstance(args, dict) else None
                except ValueError:
                    args = None
                blocks.append({"type": "tool_call", "id": item.get("call_id") or item.get("id"), "name": name, "input": args})
                raw.append({k: v for k, v in item.items() if k in ("type", "call_id", "name", "arguments", "namespace")})
            elif kind == "reasoning" and item.get("encrypted_content"):   # sent again with the turns after it
                raw.append({"type": "reasoning", "summary": item.get("summary") or [],
                            "encrypted_content": item["encrypted_content"], "for_model": model})
        while raw and raw[-1]["type"] == "reasoning":   # reasoning goes back only with the item it led to
            raw.pop()
        result = TurnResult(blocks, raw, model=getattr(final, "model", None) or model)
        if incomplete:
            result.stop_reason, result.note = "max_tokens", "The answer was cut short."
            result.blocks = [b for b in blocks if b["type"] != "tool_call"]
            # nor sent again: a call that never ran has no output to follow it, which the route refuses; nor the
            # reasoning that led to it
            result.raw = [i for i in raw if i["type"] not in ("function_call", "reasoning")]
        elif result.tool_calls:
            result.stop_reason = "tool_calls"
        return result
