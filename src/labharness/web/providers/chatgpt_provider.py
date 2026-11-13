"""ChatGPT models on the user's ChatGPT plan (Sign in with ChatGPT; see ../chatgpt.py).

Requests go to the public Responses API with the account's access token, store=false and stream=true, and carry the
whole conversation in `input` each time (the route keeps no server-side history). The system prompt goes in
`instructions`; parameters the route refuses (temperature, max_output_tokens, previous_response_id...) are never sent.
Function tools are offered grouped in a namespace, as the route asks; if it refuses that form, plain function tools are
tried instead and the form that works is remembered.

The model's reasoning goes with the conversation, as Codex sends it, and the turn's effort as its reasoning effort
(responses.py, which GPT with an API key shares). Either one the route refuses is dropped, and remembered."""
from __future__ import annotations

import asyncio
import json
import urllib.request
from typing import Any, Dict, List, Optional

import openai

from ..chatgpt import MANAGE_USAGE, RESOURCE, NotSignedIn, SignIn
from . import responses
from .base import EFFORT, Emit, ModelInfo, Provider, TurnResult

NAMESPACE = "kelvin"
LIMITS = {"subscription_sharing_usage_limit_exceeded": "Your ChatGPT plan's usage limit is reached for now.",
          "subscription_sharing_usage_unavailable": "ChatGPT plan usage is not available for this account right now."}


def to_input(history: List[Dict[str, Any]], provider: str, model: Optional[str] = None) -> List[Dict[str, Any]]:
    """model: the model answering now; reasoning goes back only to the model that wrote it (another can't read it)."""
    return responses.to_input(history, provider, model)


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
            body["include"] = list(responses.INCLUDE)
        effort = EFFORT.get()
        if model not in self.no_effort and effort in responses.EFFORTS:
            body["reasoning"] = {"effort": effort}
        for _ in range(6):   # each refusal below takes one thing away, once
            try:
                events = await client.responses.create(**body)
                break
            except openai.BadRequestError as e:
                if responses.relax(body, e, model, self.no_effort, self.no_include):
                    continue
                if tools and self.tool_style == "namespace" and "tool" in str(e).lower():
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
        blocks, raw, declined = responses.read(getattr(final, "output", None), model, NAMESPACE)
        result = TurnResult(blocks, raw, model=getattr(final, "model", None) or model)
        if incomplete or declined:
            result.stop_reason, result.note = ("refusal", "The model declined this request.") if declined and \
                not incomplete else ("max_tokens", "The answer was cut short.")
            result.blocks = [b for b in blocks if b["type"] != "tool_call"]
            # nor sent again: a call that never ran has no output to follow it, which the route refuses; nor the
            # reasoning that led to it
            result.raw = responses.without_calls(raw)
        elif result.tool_calls:
            result.stop_reason = "tool_calls"
        return result
