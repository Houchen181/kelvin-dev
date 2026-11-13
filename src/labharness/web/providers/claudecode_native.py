"""TEMPORARY (Claude Code bridge, see claudecode.py): chats through Claude Code, with Kelvin's tools as its own.

An answer runs one `claude -p` that stays up from the answer's first step to its last. Kelvin's tools are an MCP
server inside Kelvin ("sdk" in --mcp-config): Claude Code asks for it on its own standard output, in the control
messages its SDKs answer (Live._handle), so the model calls Kelvin's tools as it calls any tool, and Claude Code waits
for each call's answer. Meanwhile Kelvin's loop runs the calls its own way (agent.py: _run_call, _work, _dispatch: the
gate, the approval cards, Stop, the lab lock, the run's attribution, outside text marked), and the answer's next step
sends the results back as the calls' answers, with the person's messages from meanwhile (steering). The model goes
on from where it was: its thinking stays in Claude Code's conversation, not cut off at every step. A step is one
message of the model's, all its calls together, as with every other model.

Between answers the conversation is a Claude Code session, which Claude Code keeps on this computer (not for an
incognito chat: base.RESUMABLE). The chat's next answer goes on in it (--resume) when the chat is as that session left
it, with what the session hasn't taken in since (the person's message; Kelvin's results for calls an answer that ended
early left waiting; never what it took in already: Record); otherwise (another model answered meanwhile, earlier
turns were summarised, another Claude model) it starts a session with the conversation so far as its first message.

Claude Code needs a few things said (claude 2.1.304, 2026-11-12): a resumed session keeps the system prompt it began
with unless told not to (--system-prompt-snapshot off); an MCP server's tools wait behind Claude Code's own tool search
unless each says it is always loaded (Kelvin loads its rarer tools itself: find_tools); a tool result over 50,000
characters goes to a file the model can't open, with 2 KB of it shown, unless the tool allows more (up to 500,000);
and it asks for a message's calls one at a time unless the tools say they may run together (readOnlyHint, its only
word for that). Kelvin's tools say all three (mcp_tool); a result longer than a `claude` takes whole is cut by Kelvin,
saying so (capped)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ... import __version__, claudecode
from .base import Emit, text_of, user_text

SERVER = "kelvin"
PREFIX = "mcp__%s__" % SERVER                # how Claude Code names Kelvin's tools to the model
ALLOW = "mcp__" + SERVER                     # --allowedTools: every tool of Kelvin's server, and nothing else
CONFIG = json.dumps({"mcpServers": {SERVER: {"type": "sdk", "name": SERVER}}})
INIT = "kelvin_init"                         # the id of Kelvin's own first control request
RESULT_CHARS = 40000   # longer, Claude Code puts a tool's result in a file (from 50,000 characters, as JSON)...
ALLOWED_CHARS = 500000   # ...unless the tool allows more (its _meta "anthropic/maxResultSizeChars", up to this)
WHOLE_CHARS = 400000     # what Kelvin passes whole to a `claude` that takes that (WHOLE_FROM on), the rest cut
WHOLE_FROM = (2, 1, 304)   # the first `claude` seen to take it (2026-11-12); an older one gets RESULT_CHARS
ADRIFT_S = 900.0       # a run of a second opinion's, an analysis's (no chat's answer: nothing ends it) left waiting
OWNED_S = 86400.0      # a chat's answer's run left waiting this long: its answer's end (end_answer) didn't come
RELIST_S = 5.0         # for Claude Code to take Kelvin's tool list again
SETTLE_S = 0.05        # for more calls of the same message to come (Claude Code asks for them together)
ALL_ASKED_S = 1.0      # after a message, for Claude Code to ask for each of its calls (or answer it itself)
QUEUED_S = 5.0         # after the last answer, for a turn of a message of the person's not yet taken in to begin
STALL_S = 10.0         # a message still arriving that says nothing this long while calls wait: taken as complete
POLL_S = 0.02
KEPT_SESSIONS = 300    # sessions whose taken-in record Kelvin keeps (Record)

EARLIER = ("The conversation so far, from before this session, for context. Your tools are your own here: call them "
           "directly, never write a call out as text.")
MISSED = ("Kelvin's results for your last calls, as Kelvin recorded them (your answer ended before they reached "
          "you):")
UNHEARD = "(A message of the person's from during your last answer, which didn't reach you then:)"
STOPPED = "(The person stopped your answer %s.)"
CONTINUE = "(Continue from here.)"
CUT = ("\n… (Kelvin cut this result here: %d characters in all, more than Claude Code passes on whole. Ask for a "
       "smaller part: fewer rows or columns, a narrower range, a page at a time.)")
UNKNOWN = re.compile(r"unknown option '(--[\w-]+)'")
TUNABLE = ("--effort", "--system-prompt-snapshot")   # flags a `claude` without them runs without

RUNS: List["Live"] = []   # the answers' runs going on, every provider object's (a Settings save makes new ones)
CLAIMED: Set[str] = set()   # sessions a run goes on in now: another run at the same time gets a copy (--fork-session)
RECORDS: Dict[str, "Record"] = {}   # one Record per file, every provider object's


class NoStart(RuntimeError):
    """The run ended before the model said or did anything, so nothing showed and nothing ran: why. option: a flag
    this `claude` doesn't know; unable: this `claude` can't take Kelvin's tools as its own at all."""

    def __init__(self, why: str, unable: bool = False):
        super().__init__(why)
        found = UNKNOWN.search(why)
        self.option = found.group(1) if found else ""
        self.unable = unable or bool(self.option and self.option not in TUNABLE)


def first_mark(history: List[Dict[str, Any]]) -> str:
    """The conversation's first message, as a mark: a summary of earlier turns (compaction) puts another there."""
    m = history[0] if history else {}
    seed = json.dumps([m.get("role"), m.get("text"), m.get("sent"), m.get("handoff")], ensure_ascii=False, default=str)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def entry_key(m: Dict[str, Any]) -> str:
    """A message of the person's, as the record of what a session took in names it (the same however it is shown)."""
    seed = json.dumps([m.get("sent_at"), m.get("sent"), m.get("at"), m.get("text"), m.get("events"), m.get("wake")],
                      ensure_ascii=False, default=str)
    return "u:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def result_key(r: Dict[str, Any]) -> str:
    return "r:%s" % r.get("id")


def stopped(m: Dict[str, Any]) -> bool:
    """A bridge answer the person stopped (agent.py writes it: what was said so far, then "(stopped)")."""
    return m.get("role") == "assistant" and m.get("provider") == "claudecode" and m.get("raw") is None and \
        text_of(m.get("blocks") or []).endswith("(stopped)")


def resumable(history: List[Dict[str, Any]], model: str) -> Optional[Tuple[str, int]]:
    """The Claude Code session this conversation goes on in, and where in it that session's last message is: the
    latest answer the bridge gave natively, when nothing but the person's messages, Kelvin's results and a stopped
    answer came after it, with the same model (thinking is the model's own), and no summary of earlier turns put in
    meanwhile."""
    for i in range(len(history) - 1, -1, -1):
        m = history[i]
        if m.get("role") != "assistant":
            continue
        raw = m.get("raw")
        if isinstance(raw, dict) and raw.get("bridge") == "native":
            ok = raw.get("kept") and raw.get("session") and raw.get("model") == model and \
                raw.get("first") == first_mark(history)
            return (str(raw["session"]), i) if ok else None
        if not stopped(m):   # another model's answer, or the text protocol's
            return None
    return None


def answered(history: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """The tool results the conversation ends with, and the person's messages after them (none, or those Kelvin took
    in between steps: steering, the person's changes meanwhile); (None, ...) when it doesn't end with results."""
    i, after = len(history) - 1, []
    while i >= 0 and history[i].get("role") in ("user", "event"):   # "event": Kelvin's own line in the thread
        if history[i]["role"] == "user":
            after.insert(0, history[i])
        i -= 1
    return (history[i] if i >= 0 and history[i].get("role") == "tool" else None), after


def _said(m: Dict[str, Any]) -> List[Any]:
    """A message of the person's: its text as the model reads it, and its pictures."""
    return [user_text(m)] + list(m.get("images") or [])


def _result(r: Dict[str, Any]) -> List[Any]:
    head = '<tool_result name="%s" error="%s">\n%s' % (r.get("name"), "true" if r.get("is_error") else "false",
                                                      r.get("content") or "(empty)")
    return [head] + list(r.get("images") or []) + ["</tool_result>"]


def _record(history: List[Dict[str, Any]]) -> List[Any]:
    """Earlier turns for a new session: what was said, the calls made, their results, the pictures where they came.
    A call shows as a line of the record, not as markup a model might copy."""
    out: List[Any] = []
    for m in history:
        if m.get("role") == "user":
            out += ['<message role="user">\n%s' % user_text(m)] + list(m.get("images") or []) + ["</message>"]
        elif m.get("role") == "assistant":
            parts = []
            for b in m.get("blocks") or []:
                if b.get("type") == "text" and b.get("text"):
                    parts.append(b["text"])
                elif b.get("type") == "tool_call":
                    parts.append("(called %s %s)" % (b.get("name"), json.dumps(b.get("input") or {}, ensure_ascii=False,
                                                                              default=str)))
            if parts:
                out.append('<message role="assistant">\n%s\n</message>' % "\n".join(parts))
        elif m.get("role") == "tool":
            for r in m.get("results") or []:
                out += _result(r)
    return out


def opening(history: List[Dict[str, Any]]) -> Tuple[List[Any], List[str]]:
    """A new session's first message: the conversation so far as a record, then the person's latest message(s); and
    the keys of what it carries (entry_key, result_key)."""
    n = len(history)
    while n and history[n - 1].get("role") in ("user", "event"):
        n -= 1
    out: List[Any] = []
    if n:
        out += ["<earlier_conversation>\n" + EARLIER] + _record(history[:n]) + ["</earlier_conversation>", ""]
    tail = [m for m in history[n:] if m.get("role") == "user"]
    for i, m in enumerate(tail):
        out += ([""] if i else []) + _said(m)
    return out + ([] if tail else [CONTINUE]), _keys(history)


def since(history: List[Dict[str, Any]], at: int, taken: Set[str],
          unheard: Optional[Set[str]] = None) -> Tuple[List[Any], List[str]]:
    """A resumed session's first message: what it hasn't taken in since its last message (at), and its keys. What it
    took in already (`taken`: Kelvin's results it was answered with, the person's messages it read before an answer
    was stopped or failed) isn't sent again: a request the person stopped must not arrive a second time. A message
    of the person's written to a run that ended before taking it in (`unheard`) comes first, said as such."""
    out: List[Any] = []
    late = [m for m in history[:at] if m.get("role") == "user" and entry_key(m) in (unheard or set())
            and entry_key(m) not in taken]
    for m in late:
        out += ["", UNHEARD] + _said(m)
    missed = False
    rest = history[at + 1:]
    for m in rest:
        if m.get("role") == "tool":
            for r in m.get("results") or []:
                if result_key(r) in taken:
                    continue
                if not missed:
                    out.append(MISSED)
                    missed = True
                out += _result(r)
        elif m.get("role") == "assistant":   # a stopped answer (resumable lets no other through)
            said = text_of(m.get("blocks") or [])[:-len("(stopped)")].rstrip(" …").strip()
            out += ["", STOPPED % ("after writing: “%s”" % said if said else "before it said anything")]
        elif m.get("role") == "user" and entry_key(m) not in taken:
            out += [""] + _said(m)
    while out and out[0] == "":
        out.pop(0)
    return out or [CONTINUE], [k for k in _keys(late + rest) if k not in taken]


def _keys(history: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for m in history:
        if m.get("role") == "user":
            out.append(entry_key(m))
        elif m.get("role") == "tool":
            out += [result_key(r) for r in m.get("results") or []]
    return out


def content(parts: List[Any]) -> List[Dict[str, Any]]:
    """A message's content blocks: the text as text blocks, each picture ({"mime", "data"}) as an image where it
    came. Never opening with "/", which Claude Code would take for one of its own commands."""
    out: List[Dict[str, Any]] = []
    text: List[str] = []
    for p in parts:
        if isinstance(p, str):
            text.append(p)
            continue
        if text:
            out.append({"type": "text", "text": "\n".join(text)})
            text = []
        out.append({"type": "image", "source": {"type": "base64", "media_type": p["mime"], "data": p["data"]}})
    if text:
        out.append({"type": "text", "text": "\n".join(text)})
    out = [b for b in out if b["type"] != "text" or b["text"].strip()]   # the API takes no empty text
    if out and out[0]["type"] == "text" and out[0]["text"].lstrip().startswith("/"):
        out[0]["text"] = "The person wrote:\n" + out[0]["text"]
    return out or [{"type": "text", "text": CONTINUE}]


def capped(text: str, limit: int = RESULT_CHARS) -> str:
    """A result as Claude Code passes it on whole (limit, measured as the JSON it sizes), cut with a note."""
    size = len(json.dumps(text, ensure_ascii=False))
    if size <= limit:
        return text
    note = CUT % len(text)
    keep = int(len(text) * (limit - len(note) - 100) / size)
    while keep > 0 and len(json.dumps(text[:keep], ensure_ascii=False)) + len(note) > limit:
        keep = int(keep * 0.95)
    return text[:keep] + note


def whole_chars(version: str) -> int:
    """How long a result this `claude` (its version, as its first event says) passes on whole."""
    seen = tuple(int(x) for x in re.findall(r"\d+", str(version))[:3])
    return WHOLE_CHARS if len(seen) == 3 and seen >= WHOLE_FROM else RESULT_CHARS


def mcp_result(r: Dict[str, Any], limit: int = RESULT_CHARS) -> Dict[str, Any]:
    """Kelvin's result for a call, as the answer to Claude Code's tools/call: its text (with Kelvin's notes on it, as
    every model gets them) and its pictures (look_at's plots and frames)."""
    blocks: List[Dict[str, Any]] = [{"type": "text", "text": capped(str(r.get("content") or "") or "(empty)", limit)}]
    blocks += [{"type": "image", "data": p["data"], "mimeType": p["mime"]}
               for p in r.get("images") or [] if isinstance(p, dict) and p.get("data") and p.get("mime")]
    return {"content": blocks, "isError": bool(r.get("is_error"))}


def mcp_tool(t: Dict[str, Any]) -> Dict[str, Any]:
    """A tool of Kelvin's as Claude Code lists it: always in the model's tool list (never behind Claude Code's tool
    search: Kelvin loads its rarer tools itself), its results passed on whole up to ALLOWED_CHARS, and free to run
    beside the message's other calls, so Claude Code asks for them all at once and Kelvin has the message's calls as
    one step. readOnlyHint is Claude Code's only word for that (it allows Kelvin's tools all the same: --allowedTools,
    dontAsk); what a call may change in the lab Kelvin's gate decides, and Kelvin runs calls that change the lab one at
    a time itself."""
    schema = dict(t.get("schema") or {})
    schema.setdefault("type", "object")
    return {"name": t["name"], "description": t.get("description") or "", "inputSchema": schema,
            "annotations": {"readOnlyHint": True},
            "_meta": {"anthropic/alwaysLoad": True, "anthropic/maxResultSizeChars": ALLOWED_CHARS}}


def mcp_answer(message: Dict[str, Any], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Kelvin's MCP server, for all Claude Code asks of it but calls: who it is and its tools."""
    if "id" not in message:   # a notification: taken
        return {"jsonrpc": "2.0", "result": {}}
    method = message.get("method")
    if method == "initialize":
        result: Dict[str, Any] = {
            "protocolVersion": (message.get("params") or {}).get("protocolVersion") or "2025-06-18",
            "capabilities": {"tools": {"listChanged": True}}, "serverInfo": {"name": SERVER, "version": __version__}}
    elif method == "tools/list":
        result = {"tools": [mcp_tool(t) for t in tools]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": message["id"],
                "error": {"code": -32601, "message": "Kelvin's tools have no %s" % method}}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def child_env() -> Dict[str, str]:
    """The person's own sign-in (claudecode.child_env); a long call stays one the model waits for, never sent to the
    background, as Claude Code may do with a slow MCP call (not in print mode, said here for sure); and no tool search
    of Claude Code's own (a `claude` from before the tools could say they are always loaded)."""
    env = claudecode.child_env()
    env["CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS"] = "0"
    env["ENABLE_TOOL_SEARCH"] = "false"
    return env


class Record:
    """What each Claude Code session took in after the bridge's last answer in it (keys: entry_key, result_key), so a
    resumed session isn't sent it again: Claude Code says so as it takes it in (the echo of a message, the result it
    answered a call with). And the person's messages a run was sent but ended without taking in ("unheard"), so the
    next one gets them. Kept in a file beside the runs (keys only, no words), the last KEPT_SESSIONS."""

    def __init__(self, path: Path):
        self.path = path
        self.sessions: Optional[Dict[str, Any]] = None

    @staticmethod
    def at(path: Path) -> "Record":
        """The one Record of this file: a provider object made later (a Settings save) sees what earlier ones noted."""
        key = os.path.normcase(os.path.abspath(str(path)))
        if key not in RECORDS:
            RECORDS[key] = Record(path)
        return RECORDS[key]

    def _load(self) -> Dict[str, Any]:
        if self.sessions is None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.sessions = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self.sessions = {}
        return self.sessions

    def taken(self, session: str) -> Set[str]:
        return set((self._load().get(session) or {}).get("taken") or [])

    def unheard(self, session: str) -> Set[str]:
        return set((self._load().get(session) or {}).get("unheard") or [])

    def add(self, session: str, taken: Set[str], unheard: Set[str] = frozenset()) -> None:
        if not session or not (taken or unheard):
            return
        sessions = self._load()
        entry = sessions.pop(session, None) or {}
        have, missed = set(entry.get("taken") or []), set(entry.get("unheard") or [])
        if taken <= have and unheard <= missed:
            sessions[session] = entry
            return
        sessions[session] = {"taken": sorted(have | taken), "unheard": sorted((missed | unheard) - taken - have),
                             "at": time.time()}
        while len(sessions) > KEPT_SESSIONS:
            sessions.pop(next(iter(sessions)))
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(sessions), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass


class Live:
    """One answer's `claude` run, kept between the answer's steps. Its reading thread answers Claude Code's questions
    to Kelvin's MCP server at once (_handle) and notes what Claude Code takes in; its calls of Kelvin's tools wait in
    `calls` until the answer's next step brings their results (answer)."""

    def __init__(self, cmd: List[str], args: List[str], cwd: Path, system: str, tools: List[Dict[str, Any]],
                 key: str, model: str, first: str, kept: bool, owned: bool, resumed: str = ""):
        self.tools = list(tools)
        self.key, self.model, self.first, self.kept, self.resumed = key, model, first, kept, resumed
        self.owned = owned       # a chat's answer's: ended when that answer ends (end_answer)
        self.session = resumed or ""
        self.whole = RESULT_CHARS   # how long a result it takes whole: by its version (whole_chars)
        self.lock = threading.Lock()
        self.calls: List[Dict[str, Any]] = []   # Kelvin's tools called, as Claude Code asks, until answered
        self.given: List[str] = []              # the calls Kelvin has from the last step: their ids, awaiting results
        self.uses: List[str] = []               # the ids of the tool calls in the model's message being read
        self.names: Dict[str, str] = {}         # ...and their tools (for a `claude` that doesn't say a call's id)
        self.done: Set[str] = set()             # calls Claude Code answered itself (a tool it doesn't have, say)
        self.answered: Set[str] = set()         # calls Kelvin answered
        self.sent: List[Tuple[str, List[str]]] = []   # messages written, not yet taken in: (as JSON, keys carried)
        self.taken: Set[str] = set()            # keys of what Claude Code has taken in (Record)
        self.written = 0         # messages written to it
        self.heard = 0           # ...and taken in, as the step reads its events (echoes: --replay-user-messages)
        self.open = False        # a message of the model's is arriving
        self.ended_at = 0.0      # when the last one ended
        self.heard_at = time.monotonic()   # when it last said anything
        self.partial = False     # this claude streams the model's messages as they come
        self.said = False        # the model said or did something in this run
        self.left = False        # it ended with a message of the person's not taken in
        self.busy = False        # a step is being read
        self.since = time.monotonic()   # not read from since
        self.listed = threading.Event()
        self.asked = 0
        fd, self.prompt = tempfile.mkstemp(prefix="system-", suffix=".md", dir=str(cwd))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(system)
        try:
            self.run = claudecode.Running(cmd, args + ["--system-prompt-file", self.prompt], cwd,
                                          handle=self._handle, env=child_env())
        except OSError:   # no such program (any more)
            self._forget()
            raise

    # ------------------------------------------------------------------ on the reading thread

    def _handle(self, event: Dict[str, Any]) -> bool:
        """Claude Code asking its host (Kelvin) something: answered here, now, all but calls of Kelvin's tools. And
        what it took in, noted as it says so (the events go on to the step)."""
        kind = event.get("type")
        if kind == "user":
            self._took(event)
            return False
        if kind == "system" and event.get("subtype") == "init":
            self.session = str(event.get("session_id") or self.session)
            return False
        if kind != "control_request":
            return False
        rid, request = event.get("request_id"), event.get("request") or {}
        sub = request.get("subtype")
        if sub == "mcp_message" and request.get("server_name") == SERVER:
            message = request.get("message") or {}
            method = message.get("method")
            if method == "tools/call" and "id" in message:
                params = message.get("params") or {}
                with self.lock:
                    self.calls.append({"request": rid, "rpc": message["id"], "name": str(params.get("name") or ""),
                                       "input": params.get("arguments"),
                                       "id": str((params.get("_meta") or {}).get("claudecode/toolUseId") or ""),
                                       "at": time.monotonic()})
                return True
            if method == "notifications/cancelled":   # Claude Code gave up a call: one Kelvin doesn't have yet never
                gone = (message.get("params") or {}).get("requestId")   # runs
                with self.lock:
                    self.calls = [c for c in self.calls if c["rpc"] != gone or (c["id"] and c["id"] in self.given)]
            self._reply(rid, {"mcp_response": mcp_answer(message, self.tools)})
            if method == "tools/list":
                self.listed.set()
            return True
        if sub == "can_use_tool":   # asked only if Claude Code's own settings say ask: Kelvin's tools (its gate
            name = str(request.get("tool_name") or "")   # decides), nothing else
            allow = {"behavior": "allow", "updatedInput": request.get("input") or {}}
            self._reply(rid, allow if name.startswith(PREFIX) else
                        {"behavior": "deny", "message": "Only Kelvin's tools run here."})
            return True
        self.run.send({"type": "control_response", "response": {"subtype": "error", "request_id": rid,
                                                                "error": "Kelvin doesn't answer %s" % sub}})
        return True

    def _took(self, event: Dict[str, Any]) -> None:
        """A user event: the echo of a message Kelvin wrote (taken in now), or the results Claude Code goes on with
        (Kelvin's, or its own answer to a call it didn't ask Kelvin for)."""
        body = (event.get("message") or {}).get("content")
        with self.lock:
            if event.get("isReplay"):   # the message as written (claude 2.1.304), else the oldest not taken in yet
                if self.sent:
                    echoed = json.dumps(body, sort_keys=True)
                    at = next((i for i, (sent, _) in enumerate(self.sent) if sent == echoed), 0)
                    self.taken.update(self.sent.pop(at)[1])
                return
            for b in body if isinstance(body, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    use = str(b.get("tool_use_id") or "")
                    if use in self.answered:
                        self.taken.add("r:%s" % use)
                    elif not any(c["id"] == use for c in self.calls):
                        self.done.add(use)

    def _reply(self, rid: Any, response: Dict[str, Any]) -> None:
        self.run.send({"type": "control_response",
                       "response": {"subtype": "success", "request_id": rid, "response": response}})

    # ------------------------------------------------------------------ Kelvin's side

    def start(self, parts: List[Any], keys: List[str]) -> None:
        """Claude Code's SDK handshake (which lets it reach Kelvin's MCP server), then the run's first message."""
        self.run.send({"type": "control_request", "request_id": INIT,
                       "request": {"subtype": "initialize", "hooks": None}})
        self.say(parts, keys)

    def say(self, parts: List[Any], keys: List[str]) -> None:
        body = content(parts)
        self.written += 1
        with self.lock:
            self.sent.append((json.dumps(body, sort_keys=True), list(keys)))
        self.run.send({"type": "user", "session_id": "", "message": {"role": "user", "content": body},
                       "parent_tool_use_id": None})

    async def answer(self, results: Dict[str, Any], after: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> None:
        """The results of the calls Kelvin has (`results`, the conversation's last tool message) as the answers Claude
        Code waits for; the person's messages from meanwhile go first, so it takes them in with the results. A tool
        list that grew (find_tools) is told first, so the model can call the new ones from its next message on."""
        if [t["name"] for t in tools] != [t["name"] for t in self.tools]:
            self.tools = list(tools)
            self.listed.clear()
            self.asked += 1
            self.run.send({"type": "control_request", "request_id": "kelvin_tools_%d" % self.asked, "request": {
                "subtype": "mcp_message", "server_name": SERVER,
                "message": {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}}})
            until = time.monotonic() + RELIST_S
            while not self.listed.is_set() and time.monotonic() < until and self.run.alive():
                await asyncio.sleep(POLL_S)
        for m in after:
            self.say(_said(m), [entry_key(m)])
        by_id = {str(r.get("id")): r for r in results.get("results") or []}
        with self.lock:
            waiting = {c["id"]: c for c in self.calls}
        for use in self.given:
            call, r = waiting.get(use), by_id.get(use)
            if call is None or r is None:
                continue
            with self.lock:
                self.answered.add(use)
                self.calls.remove(call)
            self._reply(call["request"], {"mcp_response": {"jsonrpc": "2.0", "id": call["rpc"],
                                                           "result": mcp_result(r, self.whole)}})
        self.given = []

    async def step(self, emit: Emit, timeout: float) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Reads on to the answer's next step: (the step's blocks, None) once the model's message is complete and
        Claude Code waits for its calls of Kelvin's tools; (its last words, why it ended) once the answer is over."""
        self.busy = True
        try:
            return await self._step(emit, timeout)
        finally:
            self.busy = False
            self.since = time.monotonic()

    def _ready(self, waiting: List[Dict[str, Any]]) -> bool:
        """Is it Kelvin's turn? The model's message is complete, Claude Code asked for each of its calls of Kelvin's
        tools (or answered it itself), or ALL_ASKED_S went by, and no more came for SETTLE_S. Asked only once every
        event come so far is read, so what the step knows of the message is as new as the calls. A message that stays
        "arriving" without a word for STALL_S while calls wait (a stream cut off) counts as complete."""
        now = time.monotonic()
        if not waiting or (self.open and self.partial and now - self.heard_at < STALL_S):
            return False
        if now - max(c["at"] for c in waiting) < SETTLE_S:
            return False
        with self.lock:
            asked = {c["id"] for c in self.calls} | self.done | self.answered
        return all(u in asked for u in self.uses) or not self.partial or self.open or \
            now - self.ended_at >= ALL_ASKED_S

    async def _step(self, emit: Emit, timeout: float) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        words: List[str] = []   # what the model said in this step, as it came
        whole: List[str] = []   # the same from its finished messages (for a claude that doesn't stream them)
        gap = False             # a new message after words: a paragraph between
        quiet = time.monotonic() + timeout
        ending: Optional[Tuple[List[Dict[str, Any]], str]] = None   # the answer as it ended, if no turn follows
        until = 0.0
        while True:
            event = self.run.next()
            if event is None:   # all read: Kelvin's turn, or wait for more
                waiting = self._waiting()
                if self._ready(waiting):
                    blocks = self._hand(waiting, "".join(words) if self.partial else "\n\n".join(whole))
                    if any(b["type"] == "tool_call" for b in blocks):
                        return blocks, None
                if ending is not None and time.monotonic() > until:   # no turn of its own came: the message
                    self.left = True                                 # goes with the next answer (the run ends)
                    return ending
                if time.monotonic() > quiet:
                    raise TimeoutError("Claude Code said nothing for %d s" % timeout)
                await asyncio.sleep(POLL_S)
                continue
            quiet = time.monotonic() + timeout
            self.heard_at = time.monotonic()
            kind = event.get("type")
            if kind in ("stream_event", "assistant"):   # the model goes on
                ending = None
            if kind == "stream_event" and not event.get("parent_tool_use_id"):
                self.partial = True
                e = event.get("event") or {}
                if e.get("type") == "message_start":
                    self.open, gap = True, bool(words)
                    self.uses, self.names = [], {}
                elif e.get("type") == "message_stop":
                    self.open, self.ended_at = False, time.monotonic()
                elif e.get("type") == "content_block_delta":
                    d = e.get("delta") or {}
                    if d.get("type") == "text_delta" and d.get("text"):
                        text, gap = ("\n\n" if gap else "") + d["text"], False
                        words.append(text)
                        self.said = True
                        await emit({"type": "text", "delta": text})
                    elif d.get("type") == "thinking_delta":
                        self.said = True
                        await emit({"type": "thinking", "delta": d.get("thinking") or ""})
            elif kind == "assistant" and not event.get("parent_tool_use_id"):
                for b in (event.get("message") or {}).get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        use = str(b.get("id") or "")
                        self.uses.append(use)
                        self.names[use] = str(b.get("name") or "")
                        self.said = True
                    elif b.get("type") == "text" and b.get("text"):
                        whole.append(b["text"])
                        self.said = True
                        if not self.partial:
                            await emit({"type": "text", "delta": ("\n\n" if len(whole) > 1 else "") + b["text"]})
            elif kind == "user" and event.get("isReplay"):
                self.heard += 1
            elif kind == "system" and event.get("subtype") == "init":
                self.whole = whole_chars(event.get("claude_code_version") or "")
                server = next((s for s in event.get("mcp_servers") or [] if s.get("name") == SERVER), None)
                if server is None or server.get("status") in ("failed", "needs-auth", "disabled"):
                    raise NoStart("Kelvin's tools didn't reach Claude Code (%s)" % (
                        (server or {}).get("status") or "no such server"), unable=True)
            elif kind == "control_response":
                r = event.get("response") or {}
                if r.get("request_id") == INIT and r.get("subtype") == "error":
                    raise NoStart("Claude Code refused Kelvin's handshake: %s" % r.get("error"), unable=True)
            elif kind == "result":
                self.session = str(event.get("session_id") or self.session)
                problem = claudecode.failure([event])
                if problem:
                    raise NoStart(problem) if not self.said else RuntimeError(problem)
                text = "".join(words) if self.partial else "\n\n".join(whole)
                done = ([{"type": "text", "text": text.strip()}] if text.strip() else []), \
                    str(event.get("stop_reason") or "end_turn")
                if self.heard >= self.written:
                    return done
                # a message of the person's not taken in yet: a turn of its own follows, its words with these
                ending, until = done, time.monotonic() + QUEUED_S
            elif kind == "exit":
                if ending is not None:
                    self.left = True
                    return ending
                why = "Claude Code stopped (exit %s): %s" % (event.get("code"), event.get("stderr") or "no message")
                raise NoStart(why) if not self.said else RuntimeError(why)

    def _waiting(self) -> List[Dict[str, Any]]:
        """Calls Claude Code waits on that Kelvin doesn't have yet."""
        with self.lock:
            return [c for c in self.calls if not c["id"] or c["id"] not in self.given]

    def _hand(self, calls: List[Dict[str, Any]], words: str) -> List[Dict[str, Any]]:
        """The step for Kelvin's loop: the model's words, then the calls it waits on in the order the model wrote
        them (by the ids of the model's own tool calls, so their results go back to them; a `claude` that doesn't
        say which: by name, in order). Atomic with the reading thread: a call Claude Code gave up meanwhile isn't
        handed on."""
        with self.lock:
            calls = [c for c in calls if any(c is d for d in self.calls)]
            named = {c["id"] for c in calls if c["id"]}
            for c in calls:
                if not c["id"]:
                    use = next((u for u in self.uses if self.names.get(u) == PREFIX + c["name"] and u not in named),
                               "")
                    c["id"] = use or "cc_" + uuid.uuid4().hex[:12]
                    named.add(c["id"])
            order = {u: i for i, u in enumerate(self.uses)}
            calls = sorted(calls, key=lambda c: order.get(c["id"], len(order)))
            self.given = [c["id"] for c in calls]
        blocks = [{"type": "text", "text": words.strip()}] if words.strip() else []
        return blocks + [{"type": "tool_call", "id": c["id"], "name": c["name"],
                          "input": c["input"] if isinstance(c["input"], dict) else None} for c in calls]

    def raw(self) -> Dict[str, Any]:
        """What the chat keeps of the run with each answer of it, so its next answer can go on in the session."""
        return {"bridge": "native", "session": self.session, "model": self.model, "kept": self.kept,
                "first": self.first}

    def what_it_took(self) -> Set[str]:
        with self.lock:
            return set(self.taken)

    def what_it_missed(self) -> Set[str]:
        """The person's messages written to it that it hasn't taken in (yet: at its end, never)."""
        with self.lock:
            return {k for _, keys in self.sent for k in keys if k.startswith("u:")}

    def finish(self) -> None:
        """The answer is over: the run ends by itself once its input does (Claude Code keeps the session)."""
        self.run.end_input()
        threading.Thread(target=self._wind_up, daemon=True).start()

    def _wind_up(self) -> None:
        try:
            self.run.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.run.stop()
        self._forget()

    def close(self) -> None:
        """Ends the run now (Stop, an error, an answer that ended between its steps)."""
        self.run.stop()
        self._forget()

    def _forget(self) -> None:
        try:
            os.unlink(self.prompt)
        except OSError:
            pass
