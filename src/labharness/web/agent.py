"""Runs one chat turn: the model talks, calls lab tools, and approvals appear inline.

The model reaches the lab only through the same MCP tools external agents use
(connected in-process), so the safety gate and the approval rules are identical.

Memory has three levels that never mix: the user's global instructions and notes (every chat), a project's
instructions and notes (only the chats filed under that project), and the conversation itself. Notes are recalled
per message (memory.py); incognito chats use and keep no memory.
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
from . import memory
from .settings import Settings
from .store import MAX_NOTE, ChatStore

SYSTEM = INSTRUCTIONS + """
You are Kelvin, the lab assistant in this portal, working with an experimental physicist.
Write in Markdown. Lead with the result in one or two sentences, then only what helps the next decision.
Use units. Prefer a short list or small table over paragraphs. While a plan runs, the portal shows its
progress and plots live in a Runs pane beside the chat; when it finishes, the conversation shows the automatic
assessment and the pane shows the key results, plots with fits and the data. So do not repeat them: add what
they cannot (interpretation, anything suspicious, the next step)."""
PROJECT = """# Project “{name}”
This conversation is filed under the project “{name}”. The project's instructions below come from the user and
apply to this project only."""
MEMORY_RULES = """# Memory
A user message may begin with a <memory> block: notes saved in earlier conversations. They are background, not part
of this conversation. Use a note only if it bears on the question, and say so if one looks out of date."""
LEARN_RULES = """Use the remember tool when something durable is settled. For the user (scope global): a lasting
preference or convention, such as units or how cautious to be. For this project (scope project): a decision, a working
procedure, an instrument quirk, or a key result with units and run ID. Never save questions, one-off requests,
chit-chat, guesses, or anything already in the notes. To correct an outdated note, pass its id as replaces."""
INCOGNITO = """# Incognito chat
No saved notes or instructions are used here, nothing from this chat is remembered, and it is gone when the user
leaves it. Answer from this conversation alone."""


def remember_tool(in_project: bool) -> Dict[str, Any]:
    scopes = ["global", "project"] if in_project else ["global"]
    kinds = list(memory.GLOBAL_KINDS) + (list(memory.PROJECT_KINDS) if in_project else [])
    return {
        "name": "remember",
        "description": "Keep one short, durable fact for later conversations. global: the user's lasting preferences "
                       "and conventions (used in every conversation)." + (
                           " project: a decision, procedure, instrument quirk or key result, used only in this "
                           "project's conversations." if in_project else " This chat is in no project, so "
                           "experiment facts cannot be kept.") + " The user sees every note and can delete it.",
        "schema": {"type": "object", "required": ["scope", "kind", "text"], "properties": {
            "scope": {"type": "string", "enum": scopes},
            "kind": {"type": "string", "enum": kinds,
                     "description": "global: preference, convention or profile; project: decision, procedure, quirk "
                                    "or result"},
            "text": {"type": "string", "description": "The fact in one sentence, at most %d characters." % MAX_NOTE},
            "replaces": {"type": "string", "description": "id of an outdated note this one corrects (optional)"}}},
    }
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
    def __init__(self, runtime: LabRuntime, store: ChatStore, mcp_server: Any, providers: Dict[str, Provider],
                 settings: Optional[Settings] = None):
        self.runtime = runtime
        self.store = store
        self.settings = settings
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

    # ------------------------------------------------------------------ memory

    def context(self, chat: Dict[str, Any]) -> Dict[str, Any]:
        """The system prompt and tools for one turn in this chat. Only stable text goes here (Kelvin's rules, the
        user's instructions, the project's instructions), so the cached prompt prefix survives; notes are recalled
        per message instead (see recall). A chat outside every project gets no project memory; an incognito chat
        gets no memory at all and cannot save any."""
        if chat.get("incognito"):
            return {"system": SYSTEM + "\n\n" + INCOGNITO, "tools": self.tools, "project": None, "learn": False}
        parts = [SYSTEM]
        mine = self.settings.get("instructions") if self.settings else None
        if mine:
            parts.append("# The user's instructions for every conversation\n" + mine)
        project = self.store.memory_of(chat["id"])
        if project:
            parts.append(PROJECT.format(name=project["name"]))
            if project["instructions"]:
                parts.append("## Instructions\n" + project["instructions"])
        learn = self.settings.learning if self.settings else True
        parts.append(MEMORY_RULES + ("\n" + LEARN_RULES if learn else ""))
        return {"system": "\n\n".join(parts), "tools": self.tools + ([remember_tool(bool(project))] if learn else []),
                "project": project["id"] if project else None, "learn": learn}

    def recall(self, chat: Dict[str, Any], message: Dict[str, Any]) -> None:
        """Attach the notes that bear on this message (and were not given earlier in the chat) to the message."""
        if chat.get("incognito"):
            return
        project = self.store.memory_of(chat["id"])
        notes = memory.recall(self.store.global_notes(), project, message["text"], chat.get("recalled", []))
        if notes:
            message["memory"] = notes
            if project:
                message["memory_project"] = project["name"]
            chat["recalled"] = chat.get("recalled", []) + [n["id"] for n in notes]

    def _remember(self, chat: Dict[str, Any], context: Dict[str, Any], args: Any) -> Tuple[str, bool]:
        if not isinstance(args, dict):
            return "the arguments were not valid JSON; send the call again", True
        scope = str(args.get("scope") or "")
        try:
            note, what = self.store.remember(scope, args.get("text"), str(args.get("kind") or ""),
                                             project_id=context["project"], chat_id=chat["id"],
                                             replaces=str(args["replaces"]) if args.get("replaces") else None)
        except KeyError as e:
            return "not saved: %s" % (e.args[0] if e.args else "not found"), True
        except ValueError as e:
            return "not saved: %s" % e, True
        where = "your notes for every conversation" if scope == "global" else "this project's notes"
        verb = {"saved": "kept in", "known": "already in", "updated": "updated in"}[what]
        return "%s %s: [%s] %s (id %s)" % (verb, where, note["kind"], note["text"], note["id"]), False

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
            message: Dict[str, Any] = {"role": "user", "text": text}
            self.recall(chat, message)
            chat["messages"].append(message)
            if chat.get("title", "New chat") == "New chat":
                first = text.strip().splitlines()[0]
                chat["title"] = first[:48] + ("…" if len(first) > 48 else "")
            chat["model"] = model_key
            self.store.save(chat)
            context = self.context(chat)  # fixed for the whole turn, so every step reuses the cached prefix
            await emit({"type": "turn_start", "model": model_key, "title": chat["title"]})
            if message.get("memory"):
                await emit({"type": "memory", "notes": message["memory"]})
            for _ in range(MAX_STEPS):
                if not await self._step(chat, provider, model_id, context, emit):
                    return
            await emit({"type": "note", "text": "Paused after %d steps. Say “continue” to go on." % MAX_STEPS})
            await emit({"type": "turn_end", "reason": "step_limit"})
        except Exception as e:
            await emit({"type": "error", "message": describe_error(provider, e)})
            await emit({"type": "turn_end", "reason": "error"})
        finally:
            self.active = None
            self._stream_task = None

    async def _step(self, chat: Dict[str, Any], provider: Provider, model_id: str, context: Dict[str, Any],
                    emit: Emit) -> bool:
        """One model response plus its tool calls. Returns True if the turn continues."""
        partial: List[str] = []

        async def relay(event: Dict[str, Any]) -> None:
            if event["type"] == "text":
                partial.append(event["delta"])
            elif event["type"] == "reset":
                partial.clear()
            await emit(event)

        self._stream_task = asyncio.ensure_future(
            provider.stream(model_id, context["system"], chat["messages"], context["tools"], relay))
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
                if call["name"] == "remember" and context["learn"]:
                    content, is_error = self._remember(chat, context, call["input"])
                else:
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

        before = self.runtime.live_view(10 ** 9)
        old_run = before["run_id"] if before else None
        sent: Dict[str, Any] = {}

        async def progress() -> None:
            """Say how a run that started during this call is going. Its data go to the runs pane (/api/live)."""
            view = self.runtime.live_view(10 ** 9)  # no rows, just the counters
            if not view or view["run_id"] == old_run:
                return
            event = {"type": "run_progress", "call_id": call["id"], "run_id": view["run_id"], "plan": view["plan"],
                     "done": view["done"], "total": view["total"], "eta_s": view["eta_s"], "status": view["status"]}
            key = (view["run_id"], view["done"], view["status"])
            if sent.get("key") != key:
                sent["key"] = key
                await emit(event)

        task = asyncio.ensure_future(self.client.call_tool(call["name"], call["input"]))
        try:
            while not task.done():
                getter = asyncio.ensure_future(requests.get())
                done, _ = await asyncio.wait({task, getter}, timeout=0.4, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    await show(getter.result())
                else:
                    getter.cancel()
                await progress()
            await asyncio.sleep(0)  # let a last notification from the worker thread land
            while not requests.empty():
                await show(requests.get_nowait())
            await progress()
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
