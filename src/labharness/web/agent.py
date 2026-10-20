"""Runs one chat turn: the model talks, calls lab tools, and approvals appear inline.

The model reaches the lab only through the same MCP tools external agents use
(connected in-process), so the safety gate and the approval rules are identical.

Memory has three levels that never mix: the user's global instructions and notes (every chat), a project's
instructions and notes (only the chats filed under that project), and the conversation itself. Notes are recalled
per message (memory.py); incognito chats use and keep no memory.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import sys
import time
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional, Tuple

from mcp import Client

from ..agent.runtime import LabRuntime
from ..agent.server import INSTRUCTIONS
from ..gate import ApprovalRequest
from .providers.base import Emit, ModelInfo, Provider
from . import memory, review, rules as rulebook
from .settings import Settings
from .store import MAX_NOTE, ChatStore

SYSTEM = INSTRUCTIONS + """
You are Kelvin, the lab assistant in this portal, working with an experimental physicist.
Write in Markdown. Lead with the result in one or two sentences, then only what helps the next decision.
Use units. Prefer a short list or small table over paragraphs. While a plan runs, the portal shows its
progress and plots live in a Runs pane beside the chat; when it finishes, the conversation shows the automatic
assessment and the pane shows the key results, plots with fits and the data. So do not repeat them: add what
they cannot (interpretation, anything suspicious, the next step).
When you cite a paper, link it: [Author Year](url), using only papers you found with search_papers or the
project's reading list; never invent a reference. Queries to search_papers leave the computer: describe the
physics, not confidential sample or project details.
A user message may begin with a <second_opinion> block: another model's review of your previous answer, asked for by
the user. Weigh it on its merits and say briefly where you agree or not. A run_plan result may start with a second
opinion on the plan; mention its concerns if they matter."""
COMPARE = """# Comparing models
The user is asking several models this same message and will compare the answers side by side. In this answer you
may read the lab (status, values, runs, dry runs) but not change it. If something should be set or run, say exactly
what you would do; the user picks one answer to continue with."""
CONFIDENTIAL = """# Confidential project
This project's rules: {rules}. Keep its details inside this conversation: never suggest pasting them into outside
services."""
WEB_TOOLS = {"search_papers", "read_paper"}
COMPARE_STEPS = 8
MAX_LANES = 3
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


def plan_name(args: Any) -> str:
    m = re.search(r"plan:\s*([^\n]+)", str((args or {}).get("plan") or "") if isinstance(args, dict) else "")
    return m.group(1).strip() if m else "(unnamed)"


def run_id_of(text: str) -> Optional[str]:
    m = _RUN_ID.search(text or "")
    return m.group(1) if m else None


def runs_in(chats: List[Dict[str, Any]]) -> List[str]:
    """Run ids that run_plan started in these chats, in the order they ran."""
    ids: List[str] = []
    for chat in chats:
        for m in chat.get("messages", []):
            for r in m.get("results", []) if m.get("role") == "tool" else []:
                rid = run_id_of(r.get("content") or "") if r.get("name") == "run_plan" else None
                if rid and rid not in ids:
                    ids.append(rid)
    return sorted(ids)


SAVE_PAPER = {
    "name": "save_paper",
    "description": "Put a paper on this project's reading list (the user sees it in the project's settings, and it is "
                   "listed as references in the project's reports). Use the id that search_papers gave. Save papers "
                   "the project will rely on, not every search result.",
    "schema": {"type": "object", "required": ["paper"], "properties": {
        "paper": {"type": "string", "description": "arXiv:... or doi:... as search_papers lists it"},
        "why": {"type": "string", "description": "One line: what the project uses it for (optional)"}}},
}


DEVICES = {
    "name": "devices",
    "description": "The samples (devices) of this conversation's project, and which one is mounted in the setup now. "
                   "Every scan is filed under the mounted device, and each device has its own report.",
    "schema": {"type": "object", "properties": {}},
}
ADD_DEVICE = {
    "name": "add_device",
    "description": "Add a sample to this project's device list, as the person describes it.",
    "schema": {"type": "object", "required": ["name"], "properties": {
        "name": {"type": "string", "description": "A short name, e.g. D3 or Chip 2 Hall bar A"},
        "material": {"type": "string", "description": "e.g. graphene/hBN, EuIn2As2 flake, NV diamond"},
        "description": {"type": "string", "description": "Geometry, contacts, fabrication: what the person said"}}},
}
MOUNT_DEVICE = {
    "name": "mount_device",
    "description": "Record which sample is in the setup now, only when the person says so; every scan from now on is "
                   "filed under it. An empty device records that nothing is mounted.",
    "schema": {"type": "object", "required": ["device"], "properties": {
        "device": {"type": "string", "description": "The device's name (add_device first if it is new), or empty"}}},
}
DEVICE_TOOLS = {"devices", "add_device", "mount_device"}

# Claude Code bridge (temporary, see claudecode.py): Claude works the screen through Kelvin's screen tools
OPERATE_COMPUTER = {
    "name": "operate_computer",
    "description": "Have Claude, through the Claude Code on this computer, use the screen, mouse and keyboard to do a "
                   "task in another program: vendor software without an API, a web page, a file dialog. Say what to "
                   "do, what done looks like, and what must not be touched. It returns what was done. Settings changed "
                   "this way bypass Kelvin's safety gate: use Kelvin's own instrument tools whenever they can do it, "
                   "and name in the task exactly which instrument settings may change.",
    "schema": {"type": "object", "required": ["task"], "properties": {
        "task": {"type": "string", "description": "The task, in plain words, with what done looks like"},
        "minutes": {"type": "number", "description": "Time limit in minutes (default 10, at most 30)"}}},
}
COMPUTER_RULES = """You are operating a lab computer for Kelvin, a lab assistant, with the screen tools (screenshot,
click_at, type_into, press_keys, scroll_at, drag_from_to, wait_seconds).
- Do only the task you were given. Take a screenshot first, and again after each step that changes the screen.
- Never click in Kelvin's own window (its approvals, settings or stop button) and never change Claude Code's or the
  computer's settings.
- Change instrument settings only as the task says. If something looks unsafe, unexpected or different from the
  task, stop and say so.
- End with a short report: what you did, what you saw (with any numbers shown), and anything left undone."""


def reference(entry: Dict[str, Any]) -> str:
    """A reading-list entry as a reference line."""
    names = entry.get("authors") or []
    who = names[0] + (" et al." if len(names) > 2 else " and " + names[1] if len(names) == 2 else "") if names else "?"
    return "%s (%s). %s. %s%s" % (who, entry.get("year") or "n.d.", entry.get("title", ""),
                                  entry.get("venue") + ". " if entry.get("venue") else "", entry.get("url", ""))


REPORT_SCOPE = (" In this portal, with no runs, day or last chosen, the report covers the runs started in this "
                "conversation, or in all of this project's conversations if it is in a project, and includes the "
                "project's notes.")


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
        self.read_only: set = set()
        self._lanes: List[asyncio.Future] = []

    async def start(self) -> None:
        self.client = await self._exit.enter_async_context(Client(self.mcp_server))
        listed = (await self.client.list_tools()).tools
        self.read_only = {t.name for t in listed if t.annotations is not None and t.annotations.read_only_hint}
        self.tools = [{"name": t.name, "description": (t.description or "") + (REPORT_SCOPE if t.name == "make_report"
                                                                             else ""), "schema": t.input_schema}
                      for t in listed]

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
            papers = project.get("papers") or []
            if papers:   # changes only when a paper is added, so the cached prefix mostly holds
                parts.append("## Reading list (read_paper takes the id)\n" + "\n".join(
                    "- %s [id %s]%s" % (reference(e), e["paper"], " — " + e["why"] if e.get("why") else "")
                    for e in papers[-40:]))
        learn = self.settings.learning if self.settings else True
        parts.append(MEMORY_RULES + ("\n" + LEARN_RULES if learn else ""))
        rules = self.chat_rules(chat)
        if rulebook.restrictive(rules):
            parts.append(CONFIDENTIAL.format(rules=rulebook.describe(rules)))
        tools = self.tools + ([remember_tool(bool(project))] if learn else []) + ([SAVE_PAPER] if project else []) \
            + [DEVICES, ADD_DEVICE, MOUNT_DEVICE]
        if "claudecode" in self.providers and rulebook.allows(rules, "claudecode:opus"):   # Claude Code bridge
            tools = tools + [OPERATE_COMPUTER]
        if not rules["literature"]:
            tools = [t for t in tools if t["name"] not in WEB_TOOLS]
        return {"system": "\n\n".join(parts), "tools": tools, "project": project["id"] if project else None,
                "learn": learn, "rules": rules}

    # ------------------------------------------------------------------ who may see a project

    def chat_rules(self, chat: Dict[str, Any]) -> Dict[str, Any]:
        """The chat's own rules (kept from any confidential project it ran in) combined with its project's now."""
        project = self.store.memory_of(chat["id"])
        return rulebook.merge(chat.get("rules"), (project or {}).get("rules"))

    def _stamp(self, chat: Dict[str, Any]) -> None:
        """A chat that runs under a project's rules keeps them, wherever it is filed later."""
        project = self.store.memory_of(chat["id"])
        if project and rulebook.restrictive(project.get("rules")):
            chat["rules"] = rulebook.merge(chat.get("rules"), project["rules"])

    def check_model(self, chat: Dict[str, Any], model_key: str, what: str = "") -> None:
        rules = self.chat_rules(chat)
        if not rulebook.allows(rules, model_key):
            raise ValueError("%s is not allowed here: this project's rules say %s.%s" % (
                self.label(model_key), rulebook.describe(rules), " " + what if what else ""))

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

    # ------------------------------------------------------------------ reports

    def chats_for_report(self, chat: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """This chat, or every chat of its project; and the project (None for an unfiled or incognito chat)."""
        project = self.store.memory_of(chat["id"])
        if not project:
            return [chat], None
        chats = []
        for c in self.store.list():
            if c["project"] == project["id"]:
                try:
                    chats.append(chat if c["id"] == chat["id"] else self.store.load(c["id"]))
                except KeyError:
                    pass
        return chats, project

    def report_notes(self, project: Optional[Dict[str, Any]]) -> List[Tuple[str, str]]:
        return [(n.get("kind") or "note", n["text"]) for n in (project or {}).get("notes", [])
                if n.get("kind") in memory.PROJECT_KINDS]

    async def _save_paper(self, chat: Dict[str, Any], context: Dict[str, Any], args: Any) -> Tuple[str, bool]:
        if not isinstance(args, dict) or not args.get("paper"):
            return "say which paper (its id from search_papers)", True
        if not self.runtime.literature.enabled:
            return "literature search is switched off in Settings", True
        try:
            paper = await asyncio.to_thread(self.runtime.literature.lookup, str(args["paper"]))
            entry, what = self.store.add_paper(context["project"], paper.as_dict(), str(args.get("why") or ""), chat["id"])
        except KeyError as e:
            return "not saved: %s" % (e.args[0] if e.args else e), True
        except Exception as e:
            return "not saved: %s" % e, True
        return "%s the reading list: %s" % ("already on" if what == "known" else "added to", reference(entry)), False

    # ------------------------------------------------------------------ devices and the reports' view of projects

    def project_info(self, project_id: Optional[str]) -> Dict[str, Any]:
        """What reports need about a project; None is the General workspace (work outside every project)."""
        project = None
        if project_id:
            try:
                project = self.store.project(project_id)
            except KeyError:
                return {"id": project_id, "name": project_id, "devices": [], "notes": [], "references": [],
                        "summary": "", "private": False}
        return {"id": project_id, "name": project["name"] if project else "General",
                "devices": self.store.devices(project_id), "notes": self.report_notes(project),
                "references": self.report_references(project), "summary": self.store.summary_of(project_id),
                "private": bool(project and rulebook.restrictive(project.get("rules")))}

    def project_chat_runs(self, project_id: Optional[str]) -> List[str]:
        chats = []
        for c in self.store.list():
            if (c["project"] or None) == (project_id or None):
                try:
                    chats.append(self.store.load(c["id"]))
                except KeyError:
                    pass
        return runs_in(chats)

    def _device_tool(self, chat: Dict[str, Any], name: str, args: Any) -> Tuple[str, bool]:
        pid = self.store.project_of(chat["id"])
        args = args if isinstance(args, dict) else {}
        try:
            if name == "add_device":
                d = self.store.add_device(pid, args.get("name"), args.get("material") or "", args.get("description") or "")
                return "added device %s to %s" % (d["name"], self.project_info(pid)["name"]), False
            if name == "mount_device":
                ref = str(args.get("device") or "").strip()
                if not ref:
                    self.runtime.unmount(by="agent")
                    return "nothing is mounted now; scans are filed under the project only", False
                d = self.store.find_device(pid, ref)
                m = self.runtime.mount(pid, d, by="agent")
                return "mounted %s (%s); every scan from now on is filed under it" % (m["device_name"],
                                                                                    m["project_name"]), False
            devices = self.store.devices(pid)
            m = self.runtime.mounted
            lines = ["%s%s%s" % (d["name"], " (%s)" % d["material"] if d.get("material") else "",
                                 ", retired" if d.get("retired") else "") for d in devices] or ["no devices yet"]
            lines.append("mounted now: %s" % ("%s (%s)" % (m["device_name"], m["project_name"]) if m else "nothing"))
            return "\n".join(lines), False
        except (KeyError, ValueError) as e:
            return str(e.args[0] if e.args else e), True

    def report_references(self, project: Optional[Dict[str, Any]]) -> List[str]:
        return [reference(e) for e in (project or {}).get("papers", [])]

    async def _report(self, chat: Dict[str, Any], args: Any) -> Tuple[str, bool]:
        if not isinstance(args, dict):
            return "the arguments were not valid JSON; send the call again", True
        try:
            last = int(args.get("last") or 0)
        except (TypeError, ValueError):
            return "no report: last must be a whole number", True
        runs = [str(r) for r in args.get("runs") or []] or None
        day = str(args.get("day") or "")
        chats, project = self.chats_for_report(chat)
        if not (runs or day or last):
            runs = [r for r in runs_in(chats) if (self.runtime.data_dir / "runs" / r).is_dir()] or None
        title = str(args.get("title") or "") or ("%s report" % project["name"] if project else "")
        kind = str(args.get("kind") or "runs")
        if kind == "project" and args.get("summary"):   # Kelvin's words open the master report until rewritten
            self.store.set_summary(project["id"] if project else None, str(args["summary"]))
        text = await asyncio.to_thread(self.runtime.make_report, runs, day, last, title, str(args.get("summary") or ""),
                                       str(args.get("format") or "pdf"), self.report_notes(project), "agent",
                                       self.report_references(project), kind, str(args.get("device") or ""),
                                       project["id"] if project else None)
        return text, text.startswith("no report")

    # ------------------------------------------------------------------ turns

    def provider_for(self, model_key: str) -> Tuple[Provider, str]:
        provider_name, _, model_id = str(model_key or "").partition(":")
        provider = self.providers.get(provider_name)
        if provider is None or not provider.available() or not model_id:
            raise ValueError("That model is not available. Add its API key in Settings.")
        return provider, model_id

    def label(self, model_key: str) -> str:
        provider = self.providers.get(str(model_key).partition(":")[0])
        return "%s %s" % (provider.label, model_key.partition(":")[2]) if provider else model_key

    def claim(self, chat_id: str, model_key: str) -> Provider:
        if self.active:
            raise Busy("Another conversation is still running.")
        provider, _ = self.provider_for(model_key)
        self.check_model(self.store.load(chat_id), model_key, "Pick one of the allowed models.")   # KeyError: no chat
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
        for lane in self._lanes:
            lane.cancel()
        return True

    async def run_turn(self, chat_id: str, text: str, model_key: str, provider: Provider, emit: Emit) -> None:
        """Call claim() first. Always releases the claim."""
        model_id = model_key.partition(":")[2]
        try:
            chat = self.store.load(chat_id)
            # the project scans started in this turn belong to, when no device is mounted
            self.runtime.attribution = {"project": None if chat.get("incognito") else self.store.project_of(chat_id),
                                        "chat": chat_id}
            message: Dict[str, Any] = {"role": "user", "text": text}
            self.recall(chat, message)
            self._attach_review(chat, message)
            self._stamp(chat)
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
            self.runtime.attribution = {}
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
            opinion: Optional[Dict[str, Any]] = None
            if self._stop:
                content, is_error = "not run: the user pressed stop", True
            else:
                await emit({"type": "tool_start", "id": call["id"], "name": call["name"], "input": call["input"]})
                if call["name"] == "remember" and context["learn"]:
                    content, is_error = self._remember(chat, context, call["input"])
                elif call["name"] in DEVICE_TOOLS and not chat.get("incognito"):
                    content, is_error = self._device_tool(chat, call["name"], call["input"])
                elif call["name"] == "save_paper" and context["project"]:
                    content, is_error = await self._save_paper(chat, context, call["input"])
                elif call["name"] in WEB_TOOLS and not context["rules"]["literature"]:
                    content, is_error = "not available: this project keeps its work off the internet", True
                elif call["name"] == "make_report":
                    content, is_error = await self._report(chat, call["input"])
                elif call["name"] == "operate_computer":   # Claude Code bridge
                    content, is_error = await self._operate_computer(call, emit)
                elif call["name"] == "run_plan" and self.settings and self.settings.get("reviewer"):
                    content, is_error, opinion = await self._reviewed_run(chat, call, emit)
                else:
                    content, is_error = await self._call(call, emit)
                await emit({"type": "tool_end", "id": call["id"], "name": call["name"], "content": content,
                            "is_error": is_error, "run_id": run_id_of(content) if call["name"] == "run_plan" else None})
            entry = {"id": call["id"], "name": call["name"], "content": content, "is_error": is_error}
            if opinion:
                entry["review"] = opinion   # shown again when the chat is reopened
            results.append(entry)
        chat["messages"].append({"role": "tool", "results": results})
        self.store.save(chat)
        if self._stop:
            await emit({"type": "turn_end", "reason": "stopped"})
            return False
        return True

    # ------------------------------------------------------------------ Claude Code bridge (temporary)

    async def _operate_computer(self, call: Dict[str, Any], emit: Any) -> Tuple[str, bool]:
        """Claude Code does a task on the screen with Kelvin's screen tools (screen.py), every step shown and
        recorded. While it runs, Kelvin's own approvals and settings are locked (the guard in app.py), so a click
        can never stand in for a person's yes. Stop ends it."""
        from .. import claudecode, screen
        provider = self.providers.get("claudecode")
        if provider is None:
            return "not available: Claude Code is not set up on this computer", True
        args = call["input"] if isinstance(call["input"], dict) else {}
        task = str(args.get("task") or "").strip()
        if not task:
            return "no task given", True
        try:
            minutes = max(1.0, min(30.0, float(args.get("minutes") or 10)))
        except (TypeError, ValueError):
            minutes = 10.0
        model = (self.active or {}).get("model") or ""
        alias = model.split(":", 1)[1] if model.startswith("claudecode:") else "opus"
        config = provider.cwd / "screen-mcp.json"
        command = [sys.executable, "--screen-mcp"] if getattr(sys, "frozen", False) else \
            [sys.executable, "-m", "labharness.screen"]
        config.write_text(json.dumps({"mcpServers": {"screen": {"type": "stdio", "command": command[0],
                                                                "args": command[1:]}}}), encoding="utf-8")
        argv = ["-p", task, "--output-format", "stream-json", "--verbose", "--tools", "", "--mcp-config", str(config),
                "--strict-mcp-config", "--allowedTools", "mcp__screen__*", "--permission-mode", "dontAsk",
                "--no-session-persistence", "--max-turns", "100", "--model", alias,
                "--append-system-prompt", COMPUTER_RULES]
        log = self.runtime.gate.log
        log.write("computer_task", by="Claude Code", task=task)
        self.runtime.operating = task
        steps: List[str] = []
        ends: List[Dict[str, Any]] = []
        try:
            async for ev in claudecode.stream(provider.cmd, argv, "", provider.cwd, should_stop=lambda: self._stop,
                                              timeout=minutes * 60):
                if ev.get("type") in ("result", "exit"):
                    ends.append(ev)
                if ev.get("type") != "assistant":
                    continue
                for b in (ev.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        line = screen.describe(str(b.get("name") or ""), b.get("input") or {})
                        steps.append(line)
                        log.write("computer", by="Claude Code", action=line)
                        await emit({"type": "computer_step", "call_id": call["id"], "text": line})
        except claudecode.StopRequested:
            return "stopped by the person after %d steps" % len(steps), True
        except (TimeoutError, OSError) as e:
            return "Claude Code did not finish: %s (%d steps done)" % (e, len(steps)), True
        finally:
            self.runtime.operating = None
            log.write("computer_task_end", by="Claude Code", steps=len(steps))
        problem = claudecode.failure(ends)
        if problem:
            return "Claude Code could not finish: %s (%d steps done)" % (problem, len(steps)), True
        report = next((str(e.get("result") or "") for e in ends if e.get("type") == "result"), "")
        return "Claude operated the computer (%d steps).\n%s" % (len(steps), report.strip()), False

    # ------------------------------------------------------------------ second opinions

    def _attach_review(self, chat: Dict[str, Any], message: Dict[str, Any]) -> None:
        """A second opinion the user asked for on the previous answer goes to the model with the next message."""
        for m in reversed(chat["messages"]):
            if m.get("role") == "user":
                return
            r = m.get("review") if m.get("role") == "assistant" else None
            if r and not r.get("given"):
                message["review"] = {k: r[k] for k in ("model", "label", "verdict", "text") if k in r}
                r["given"] = True
                return

    async def _reviewed_run(self, chat: Dict[str, Any], call: Dict[str, Any],
                            emit: Emit) -> Tuple[str, bool, Optional[Dict[str, Any]]]:
        """run_plan with a second opinion first. The reviewer only advises: a "stop" asks the user, who decides."""
        opinion = await self._review_plan(chat, call, emit)
        if opinion and opinion["verdict"] == "stop":
            reasons = [line.lstrip("-• ").strip() for line in opinion["text"].splitlines() if line.strip()][:5]
            go = await self._ask_human(call["id"], emit, ApprovalRequest(
                "Second opinion: %s advises not to run plan %s. Run it anyway?" % (
                    opinion["label"], plan_name(call["input"])), reasons or ["(no reasons given)"]))
            if not go:
                return ("not run: the second opinion (%s) advised stopping and the user agreed.\n%s"
                        % (opinion["label"], opinion["text"])), True, opinion
        content, is_error = await self._call(call, emit)
        if opinion:
            content = review.as_text(opinion, opinion["label"]) + "\n\n" + content
        return content, is_error, opinion

    async def _review_plan(self, chat: Dict[str, Any], call: Dict[str, Any], emit: Emit) -> Optional[Dict[str, Any]]:
        key = self.settings.get("reviewer") if self.settings else ""
        if not key or not isinstance(call["input"], dict):
            return None
        plan = str(call["input"].get("plan") or "")
        await emit({"type": "review_start", "call_id": call["id"], "kind": "plan", "model": key})
        if not rulebook.allows(self.chat_rules(chat), key):
            await emit({"type": "review", "call_id": call["id"], "kind": "plan", "model": key, "label": self.label(key),
                        "verdict": "", "text": "No second opinion: %s may not see this project (%s)." % (
                            self.label(key), rulebook.describe(self.chat_rules(chat)))})
            return None
        try:
            provider, model_id = self.provider_for(key)
            status = await asyncio.to_thread(self.runtime.lab_status)
            checks = await asyncio.to_thread(self.runtime.validate, plan)
            prompt = review.plan_prompt(review.last_request(chat["messages"]), status, checks, plan)
            text = await asyncio.wait_for(review.ask(provider, model_id, review.PLAN_SYSTEM, prompt), 180)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            reason = describe_error(self.providers[key.partition(":")[0]], e) if key.partition(":")[0] in self.providers \
                else str(e)
            await emit({"type": "review", "call_id": call["id"], "kind": "plan", "model": key, "label": self.label(key),
                        "verdict": "", "text": "No second opinion: %s" % reason})
            return None
        verdict, body = review.parse(text, "plan")
        opinion = {"kind": "plan", "model": key, "label": self.label(key), "verdict": verdict, "text": body,
                   "at": time.time()}
        self.runtime.gate.log.write("review", by=key, plan=plan_name(call["input"]), verdict=verdict, text=body[:500])
        await emit(dict(opinion, type="review", call_id=call["id"]))
        return opinion

    async def review_answer(self, chat_id: str, model_key: str) -> Dict[str, Any]:
        """A second opinion on the chat's latest answer, kept with it; the main model sees it with the next message."""
        if self.active:
            raise Busy("Wait until Kelvin has finished answering.")
        provider, model_id = self.provider_for(model_key)
        chat = self.store.load(chat_id)
        self.check_model(chat, model_key)
        prompt = review.answer_context(chat["messages"])   # ValueError when there is nothing to review
        text = await asyncio.wait_for(review.ask(provider, model_id, review.ANSWER_SYSTEM, prompt), 180)
        verdict, body = review.parse(text, "answer")
        opinion = {"kind": "answer", "model": model_key, "label": self.label(model_key), "verdict": verdict,
                   "text": body, "at": time.time()}
        chat = self.store.load(chat_id)   # it may have been saved meanwhile
        target = next((m for m in reversed(chat["messages"]) if m.get("role") == "assistant"
                       and any(b.get("type") == "text" and b.get("text", "").strip() for b in m.get("blocks") or [])), None)
        if target is not None:
            target["review"] = opinion
            self.store.save(chat)
        return opinion

    async def _ask_human(self, call_id: str, emit: Emit, request: ApprovalRequest) -> bool:
        """Ask the user through the approver (inline in the chat), outside any tool call."""
        loop = asyncio.get_running_loop()
        requests: asyncio.Queue = asyncio.Queue()
        subscribe = getattr(self.runtime.approver, "subscribe", None)
        unsubscribe = subscribe(lambda r: loop.call_soon_threadsafe(requests.put_nowait, r)) if subscribe else None
        task = asyncio.ensure_future(asyncio.to_thread(self.runtime.approver.approve, request))
        shown: List[str] = []
        try:
            while not task.done():
                getter = asyncio.ensure_future(requests.get())
                done, _ = await asyncio.wait({task, getter}, timeout=0.4, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    r = getter.result()
                    shown.append(r["id"])
                    await emit(dict(r, type="approval", call_id=call_id))
                else:
                    getter.cancel()
            await asyncio.sleep(0)
            while not requests.empty():
                r = requests.get_nowait()
                shown.append(r["id"])
                await emit(dict(r, type="approval", call_id=call_id))
        finally:
            if unsubscribe:
                unsubscribe()
        for request_id in shown:
            await emit({"type": "approval_closed", "id": request_id})
        return bool(task.result())

    # ------------------------------------------------------------------ several models at once

    def claim_compare(self, chat_id: str, model_keys: List[str]) -> List[Tuple[str, Provider, str]]:
        if self.active:
            raise Busy("Another conversation is still running.")
        keys = list(dict.fromkeys(str(k) for k in model_keys if k))
        if not 2 <= len(keys) <= MAX_LANES:
            raise ValueError("Pick two or three different models to compare.")
        lanes = [(k,) + self.provider_for(k) for k in keys]
        chat = self.store.load(chat_id)  # raises KeyError for an unknown chat
        for k in keys:
            self.check_model(chat, k)
        self.active = {"chat_id": chat_id, "model": keys[0], "compare": keys}
        self._stop = False
        return lanes

    async def run_compare(self, chat_id: str, text: str, lanes: List[Tuple[str, Provider, str]], emit: Emit) -> None:
        """Every model answers the same message at once, able to read the lab but not change it. The first answer
        continues the chat unless the user picks another (choose)."""
        keys = [k for k, _, _ in lanes]
        try:
            chat = self.store.load(chat_id)
            message: Dict[str, Any] = {"role": "user", "text": text,
                                       "compare": {"models": keys, "lanes": [], "chosen": 0}}
            self.recall(chat, message)
            self._attach_review(chat, message)
            self._stamp(chat)
            chat["messages"].append(message)
            if chat.get("title", "New chat") == "New chat":
                first = text.strip().splitlines()[0]
                chat["title"] = first[:48] + ("…" if len(first) > 48 else "")
            self.store.save(chat)
            context = self.context(chat)
            tools = [t for t in context["tools"] if t["name"] in self.read_only]   # already without web tools if barred
            system = context["system"] + "\n\n" + COMPARE
            base = list(chat["messages"])
            await emit({"type": "compare_start", "models": keys, "labels": [self.label(k) for k in keys],
                        "title": chat["title"]})
            if message.get("memory"):
                await emit({"type": "memory", "notes": message["memory"]})
            self._lanes = [asyncio.ensure_future(self._lane(i, key, provider, model_id, system, base, tools, emit))
                           for i, (key, provider, model_id) in enumerate(lanes)]
            done = await asyncio.gather(*self._lanes, return_exceptions=True)
            results = []
            for i, r in enumerate(done):
                if isinstance(r, dict):
                    results.append(r)
                else:
                    stopped = {"role": "assistant", "provider": lanes[i][1].name, "model": lanes[i][2], "raw": None,
                               "blocks": [{"type": "text", "text": "(stopped)"}]}
                    results.append({"model": keys[i], "messages": [stopped], "error": "stopped"})
            chosen = next((i for i, r in enumerate(results) if not r.get("error")), 0)
            chat = self.store.load(chat_id)
            last = chat["messages"][-1]
            last["compare"] = {"models": keys, "lanes": results, "chosen": chosen}
            chat["messages"] += copy.deepcopy(results[chosen]["messages"])
            chat["model"] = keys[chosen]
            self.store.save(chat)
            await emit({"type": "compare_end", "chosen": chosen})
            await emit({"type": "turn_end", "reason": "stopped" if self._stop else "end"})
        except Exception as e:
            await emit({"type": "error", "message": "Could not compare: %s" % e})
            await emit({"type": "turn_end", "reason": "error"})
        finally:
            self._lanes = []
            self.active = None

    async def _lane(self, i: int, key: str, provider: Provider, model_id: str, system: str,
                    base: List[Dict[str, Any]], tools: List[Dict[str, Any]], emit: Emit) -> Dict[str, Any]:
        out: List[Dict[str, Any]] = []

        async def lane_emit(event: Dict[str, Any]) -> None:
            await emit(dict(event, lane=i))

        try:
            for _ in range(COMPARE_STEPS):
                result = await provider.stream(model_id, system, base + out, tools, lane_emit)
                out.append({"role": "assistant", "provider": provider.name, "model": result.model or model_id,
                            "blocks": result.blocks, "raw": result.raw})
                if result.stop_reason != "tool_calls":
                    break
                results = []
                for call in result.tool_calls:
                    await lane_emit({"type": "tool_start", "id": call["id"], "name": call["name"], "input": call["input"]})
                    if call["name"] not in {t["name"] for t in tools}:
                        content, is_error = ("not available while comparing models: only tools that read the lab are "
                                             "(and only those this chat allows). Say what you would do instead."), True
                    elif call["input"] is None:
                        content, is_error = "the arguments were not valid JSON; send the call again", True
                    else:
                        try:
                            r = await self.client.call_tool(call["name"], call["input"])
                            content = "\n".join(c.text for c in r.content if getattr(c, "type", "") == "text")
                            is_error = bool(r.is_error)
                        except Exception as e:
                            content, is_error = "tool failed: %s" % e, True
                    await lane_emit({"type": "tool_end", "id": call["id"], "name": call["name"], "content": content,
                                     "is_error": is_error})
                    results.append({"id": call["id"], "name": call["name"], "content": content, "is_error": is_error})
                out.append({"role": "tool", "results": results})
            else:
                out.append({"role": "assistant", "provider": provider.name, "model": model_id, "raw": None,
                            "blocks": [{"type": "text", "text": "(paused after %d steps)" % COMPARE_STEPS}]})
            await lane_emit({"type": "lane_end", "reason": "end"})
            return {"model": key, "messages": out}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            reason = describe_error(provider, e)
            await lane_emit({"type": "error", "message": reason})
            await lane_emit({"type": "lane_end", "reason": "error"})
            out.append({"role": "assistant", "provider": provider.name, "model": model_id, "raw": None,
                        "blocks": [{"type": "text", "text": "(no answer: %s)" % reason}]})
            return {"model": key, "messages": out, "error": reason}

    def choose(self, chat_id: str, index: int) -> Dict[str, Any]:
        """Continue the chat with another model's answer from the last comparison."""
        if self.active:
            raise Busy("Wait until the answers are finished.")
        chat = self.store.load(chat_id)
        at = max((i for i, m in enumerate(chat["messages"]) if m.get("role") == "user"), default=-1)
        comp = chat["messages"][at].get("compare") if at >= 0 else None
        if not comp or not comp.get("lanes"):
            raise ValueError("The last message was not a comparison.")
        lanes = comp["lanes"]
        if not 0 <= index < len(lanes):
            raise ValueError("No such answer.")
        if len(chat["messages"]) != at + 1 + len(lanes[comp["chosen"]]["messages"]):
            raise ValueError("The conversation has moved on; pick an answer before replying.")
        chat["messages"] = chat["messages"][:at + 1] + copy.deepcopy(lanes[index]["messages"])
        comp["chosen"] = index
        chat["model"] = comp["models"][index]
        self.store.save(chat)
        return {"chosen": index, "model": chat["model"]}

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
