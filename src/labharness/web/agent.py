"""Runs one chat turn: the model talks, calls lab tools, and approvals appear inline.

The model reaches the lab only through the same MCP tools external agents use
(connected in-process), so the safety gate and the approval rules are identical.
"""
from __future__ import annotations

import asyncio
import re
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional, Tuple

from mcp import Client

from ..agent.runtime import LabRuntime
from ..agent.server import INSTRUCTIONS
from .providers.base import Emit, ModelInfo, Provider
from .store import ChatStore

SYSTEM = INSTRUCTIONS + """
You are the lab assistant in the Lab Harness portal, working with an experimental physicist.
Write in Markdown. Lead with the result in one or two sentences, then only what helps the next decision.
Use units. Prefer a short list or small table over paragraphs. When a run finishes, the portal draws its
plots under your message, so do not repeat every number."""
MAX_STEPS = 25
_RUN_ID = re.compile(r"run_id='([^']+)'")


class Busy(Exception):
    pass


def run_id_of(text: str) -> Optional[str]:
    m = _RUN_ID.search(text or "")
    return m.group(1) if m else None


def describe_error(provider: Provider, e: Exception) -> str:
    status = getattr(e, "status_code", None)
    if status == 401 or "Authentication" in type(e).__name__:
        return "%s rejected the API key. Check it in Settings." % provider.label
    if status == 404 or "NotFound" in type(e).__name__:
        return "%s does not offer that model to this key." % provider.label
    if status == 429 or "RateLimit" in type(e).__name__:
        return "%s is rate-limiting this key. Wait a moment and try again." % provider.label
    text = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return "%s error: %s" % (provider.label, text[:300])


class ChatAgent:
    def __init__(self, runtime: LabRuntime, store: ChatStore, mcp_server: Any, providers: Dict[str, Provider]):
        self.runtime = runtime
        self.store = store
        self.mcp_server = mcp_server
        self.providers = providers
        self.tools: List[Dict[str, Any]] = []
        self.active: Optional[Dict[str, Any]] = None
        self._stop = False
        self._stream_task: Optional[asyncio.Future] = None
        self._exit = AsyncExitStack()
        self.client: Any = None

    async def start(self) -> None:
        self.client = await self._exit.enter_async_context(Client(self.mcp_server))
        listed = (await self.client.list_tools()).tools
        self.tools = [{"name": t.name, "description": t.description or "", "schema": t.input_schema} for t in listed]

    async def close(self) -> None:
        await self._exit.aclose()

    async def models(self) -> Dict[str, Any]:
        """Available models grouped by provider; a provider that fails to list says why."""
        groups = []
        for p in self.providers.values():
            if not p.available():
                groups.append({"provider": p.name, "label": p.label, "models": [], "missing_key": True})
                continue
            try:
                models: List[ModelInfo] = await p.models()
                groups.append({"provider": p.name, "label": p.label,
                               "models": [{"key": "%s:%s" % (m.provider, m.id), "label": m.label} for m in models]})
            except Exception as e:
                groups.append({"provider": p.name, "label": p.label, "models": [], "error": describe_error(p, e)})
        return {"groups": groups}

    # ------------------------------------------------------------------ turns

    def claim(self, chat_id: str, model_key: str) -> Provider:
        if self.active:
            raise Busy("Another conversation is still running.")
        provider_name, _, model_id = model_key.partition(":")
        provider = self.providers.get(provider_name)
        if provider is None or not provider.available() or not model_id:
            raise ValueError("That model is not available. Add its API key in Settings.")
        self.store.load(chat_id)  # raises KeyError for an unknown chat
        self.active = {"chat_id": chat_id, "model": model_key}
        self._stop = False
        return provider

    def stop(self) -> bool:
        if not self.active:
            return False
        self._stop = True
        decline_all = getattr(self.runtime.approver, "decline_all", None)
        if decline_all:
            decline_all("stop button")
        self.runtime.gate.request_abort()
        if self._stream_task is not None:
            self._stream_task.cancel()
        return True

    async def run_turn(self, chat_id: str, text: str, model_key: str, provider: Provider, emit: Emit) -> None:
        """Call claim() first. Always releases the claim."""
        model_id = model_key.partition(":")[2]
        try:
            chat = self.store.load(chat_id)
            chat["messages"].append({"role": "user", "text": text})
            if chat.get("title", "New chat") == "New chat":
                first = text.strip().splitlines()[0]
                chat["title"] = first[:48] + ("…" if len(first) > 48 else "")
            chat["model"] = model_key
            self.store.save(chat)
            await emit({"type": "turn_start", "model": model_key, "title": chat["title"]})
            for _ in range(MAX_STEPS):
                if not await self._step(chat, provider, model_id, emit):
                    return
            await emit({"type": "note", "text": "Paused after %d steps. Say “continue” to go on." % MAX_STEPS})
            await emit({"type": "turn_end", "reason": "step_limit"})
        except Exception as e:
            await emit({"type": "error", "message": describe_error(provider, e)})
            await emit({"type": "turn_end", "reason": "error"})
        finally:
            self.active = None
            self._stream_task = None

    async def _step(self, chat: Dict[str, Any], provider: Provider, model_id: str, emit: Emit) -> bool:
        """One model response plus its tool calls. Returns True if the turn continues."""
        partial: List[str] = []

        async def relay(event: Dict[str, Any]) -> None:
            if event["type"] == "text":
                partial.append(event["delta"])
            elif event["type"] == "reset":
                partial.clear()
            await emit(event)

        self._stream_task = asyncio.ensure_future(
            provider.stream(model_id, SYSTEM, chat["messages"], self.tools, relay))
        try:
            result = await self._stream_task
        except asyncio.CancelledError:
            if not self._stop:
                raise
            said = "".join(partial).strip()
            chat["messages"].append({"role": "assistant", "provider": provider.name, "model": model_id, "raw": None,
                                     "blocks": [{"type": "text", "text": (said + " …" if said else "") + "(stopped)"}]})
            self.store.save(chat)
            await emit({"type": "turn_end", "reason": "stopped"})
            return False
        finally:
            self._stream_task = None

        chat["messages"].append({"role": "assistant", "provider": provider.name, "model": result.model or model_id,
                                 "blocks": result.blocks, "raw": result.raw})
        self.store.save(chat)
        if result.note:
            await emit({"type": "note", "text": result.note})
        if result.stop_reason == "pause":
            return True
        if result.stop_reason != "tool_calls":
            await emit({"type": "turn_end", "reason": result.stop_reason})
            return False

        results = []
        for call in result.tool_calls:  # one at a time: the lab has one set of instruments
            if self._stop:
                content, is_error = "not run: the user pressed stop", True
            else:
                await emit({"type": "tool_start", "id": call["id"], "name": call["name"], "input": call["input"]})
                content, is_error = await self._call(call, emit)
                await emit({"type": "tool_end", "id": call["id"], "name": call["name"], "content": content,
                            "is_error": is_error, "run_id": run_id_of(content) if call["name"] == "run_plan" else None})
            results.append({"id": call["id"], "name": call["name"], "content": content, "is_error": is_error})
        chat["messages"].append({"role": "tool", "results": results})
        self.store.save(chat)
        if self._stop:
            await emit({"type": "turn_end", "reason": "stopped"})
            return False
        return True

    async def _call(self, call: Dict[str, Any], emit: Emit) -> Tuple[str, bool]:
        """Run one tool; while it waits for a human, show its approval requests inline."""
        if call["input"] is None:
            return "the arguments were not valid JSON; send the call again", True
        loop = asyncio.get_running_loop()
        requests: asyncio.Queue = asyncio.Queue()
        subscribe = getattr(self.runtime.approver, "subscribe", None)
        unsubscribe = subscribe(lambda r: loop.call_soon_threadsafe(requests.put_nowait, r)) if subscribe else None
        shown: List[str] = []

        async def show(request: Dict[str, Any]) -> None:
            shown.append(request["id"])
            await emit(dict(request, type="approval", call_id=call["id"]))

        task = asyncio.ensure_future(self.client.call_tool(call["name"], call["input"]))
        try:
            while not task.done():
                getter = asyncio.ensure_future(requests.get())
                done, _ = await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    await show(getter.result())
                else:
                    getter.cancel()
            await asyncio.sleep(0)  # let a last notification from the worker thread land
            while not requests.empty():
                await show(requests.get_nowait())
        finally:
            if unsubscribe:
                unsubscribe()
        for request_id in shown:
            await emit({"type": "approval_closed", "id": request_id})
        try:
            result = task.result()
        except Exception as e:  # a tool must never leave the history without its result
            return "tool failed: %s" % e, True
        text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        return text, bool(result.is_error)
