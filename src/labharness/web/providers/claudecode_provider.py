"""TEMPORARY (Claude Code bridge, see claudecode.py): chats answered by the Claude Code on this computer.

Claude Code runs with its own tools switched off and Kelvin's system prompt. Kelvin's tools are described in that
prompt and called as <tool_call name="...">{json}</tool_call>; Kelvin runs them in its own loop, through the
safety gate like any model's calls, and sends the results back in the next turn. Each turn is one `claude -p`
run that is not saved as a Claude Code session.

Claude models often write a call in the form they are trained on instead, <invoke name="..."><parameter
name="...">...</parameter></invoke> (sometimes inside <function_calls>), and with no tools of its own in the run
nothing stops the model after a call, so it may go on to write the result itself. So Kelvin reads both forms, shows
none of the markup, ends the turn where the model starts writing a result (or the person's next message), and asks
once more when a turn had call markup but no call Kelvin could run.

Pictures (a photo the person attaches, look_at's plots and frames) go in as pictures: a conversation that has any is
sent as one stream-json user message (`--input-format stream-json`, how Claude Code takes messages from a program),
the transcript's text with each picture where it came, as Claude's own image blocks. Without pictures the transcript
goes in as plain text, as before."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ... import claudecode
from .base import Emit, ModelInfo, Provider, TurnResult, user_text

log = logging.getLogger("kelvin")

MODELS = [("opus", "Claude Opus (Claude Code)"), ("sonnet", "Claude Sonnet (Claude Code)"),
          ("haiku", "Claude Haiku (Claude Code)")]
ASK = "Continue the conversation on standard input as Kelvin: write Kelvin's next turn."
PROTOCOL = """# How to call Kelvin's tools here
You have no built-in tools in this conversation. To use one of the tools below, write on its own lines:
<tool_call name="TOOL_NAME">{"argument": "value"}</tool_call>
with the arguments as one JSON object that fits the tool's schema (Kelvin also reads a call written as
<invoke name="TOOL_NAME"><parameter name="argument">value</parameter></invoke>). You may call several tools in a row;
then stop writing. Kelvin runs them in order and continues the conversation with each result in a <tool_result>
element. Never write a <tool_result> yourself and never state a tool's result before you have it: Kelvin drops
everything from a <tool_result> of yours on.

# Tools"""
# Once more, after a turn with call markup but nothing Kelvin could run (after </conversation>, for the model only).
RETRY = """<kelvin_note>
Your last reply had %s, so no tool ran and the person saw none of it. Write your reply again. Call each tool on its
own lines as <tool_call name="TOOL_NAME">{"argument": "value"}</tool_call>, then stop writing: Kelvin runs the calls
and continues the conversation with their results.
</kelvin_note>"""

# The markup a turn can hold: calls in either form; results and the person's messages, which only Kelvin writes
# (transcript() below); the transcript's own elements. Claude's own form may come with the "antml:" prefix.
CALL_TAGS = ("tool_call", "invoke", "function_calls", "parameter")
RESULT_TAGS = ("tool_result", "tool_results", "tool_response", "function_results", "function_result")
TALK_TAGS = ("message", "conversation")
TAG = re.compile(r"<(/?)(?:antml:)?(%s)(?![\w:-])([^<>]*)>" % "|".join(CALL_TAGS + RESULT_TAGS + TALK_TAGS))
HEADS = [p + n for n in CALL_TAGS + RESULT_TAGS + TALK_TAGS for p in ("", "antml:")]
CLOSE = re.compile(r"</(?:antml:)?(?:tool_call|invoke)\s*>")
OPENER = re.compile(r"<(?:antml:)?(?:tool_call|invoke)(?![\w:-])[^<>]*>")
MADE_UP = re.compile(r"<(?:antml:)?(?:%s)(?![\w:-])" % "|".join(RESULT_TAGS))
STRAY = re.compile(r"</(?:antml:)?(?:tool_call|invoke|parameter|function_calls)\s*>")
PARAM = re.compile(r"""<(?:antml:)?parameter\s+name\s*=\s*["']([^"']+)["']\s*>(.*?)</(?:antml:)?parameter\s*>""", re.S)
NAME = re.compile(r"""name\s*=\s*["']([^"']+)["']""")
SPEAKERS = ("user", "human", "assistant", "kelvin")


def tool_catalog(tools: List[Dict[str, Any]]) -> str:
    return "\n\n".join("## %s\n%s\nArguments (JSON schema): %s" % (t["name"], t.get("description") or "",
                                                                      json.dumps(t.get("schema") or {}))
                       for t in tools)


def pieces(history: List[Dict[str, Any]]) -> List[Any]:
    """The conversation as Claude reads it: its text, and each picture ({"mime", "data"}) where it came, inside the
    person's message it was attached to or the tool result that showed it."""
    out: List[Any] = ["<conversation>"]
    for m in history:
        if m["role"] == "user":
            if m.get("images"):
                out += ['<message role="user">\n%s\n' % user_text(m)] + list(m["images"]) + ["</message>"]
            else:
                out.append('<message role="user">\n%s\n</message>' % user_text(m))
        elif m["role"] == "assistant":
            parts = []
            for b in m.get("blocks") or []:
                if b["type"] == "text" and b["text"]:
                    parts.append(b["text"])
                elif b["type"] == "tool_call":
                    parts.append('<tool_call name="%s">%s</tool_call>' % (b["name"], json.dumps(b.get("input") or {})))
            if parts:
                out.append('<message role="assistant">\n%s\n</message>' % "\n".join(parts))
        elif m["role"] == "tool":
            for r in m["results"]:
                head = '<tool_result name="%s" error="%s">\n%s\n' % (
                    r["name"], "true" if r.get("is_error") else "false", r.get("content") or "(empty)")
                if r.get("images"):
                    out += [head] + list(r["images"]) + ["</tool_result>"]
                else:
                    out.append(head + "</tool_result>")
    out.append("</conversation>")
    return out


def transcript(history: List[Dict[str, Any]]) -> str:
    return "\n".join(p for p in pieces(history) if isinstance(p, str))


def has_pictures(history: List[Dict[str, Any]]) -> bool:
    return any(m.get("images") for m in history if m["role"] == "user") or \
        any(r.get("images") for m in history if m["role"] == "tool" for r in m["results"])


def stream_message(parts: List[Any]) -> str:
    """One user message for `claude -p --input-format stream-json` (Claude Code's own wire form, as its SDKs send
    it): the text runs as text blocks, each picture as an image block where it came."""
    content: List[Dict[str, Any]] = []
    text: List[str] = []
    for p in parts:
        if isinstance(p, str):
            text.append(p)
            continue
        if text:
            content.append({"type": "text", "text": "\n".join(text)})
            text = []
        content.append({"type": "image", "source": {"type": "base64", "media_type": p["mime"], "data": p["data"]}})
    if text:
        content.append({"type": "text", "text": "\n".join(text)})
    return json.dumps({"type": "user", "message": {"role": "user", "content": content},
                       "parent_tool_use_id": None, "session_id": "default"}) + "\n"


class Reading:
    """What Kelvin makes of a turn Claude wrote."""

    def __init__(self) -> None:
        self.words = ""       # what the person sees: the turn without markup, up to where it stops counting
        self.calls: List[Tuple[str, str, Optional[Dict[str, Any]]]] = []   # (form, tool, arguments; None: unreadable)
        self.markup = False   # call or result markup was there, read or not
        self.made_up = False  # the model began writing a tool's result itself: the turn ends there
        self.stopped = False  # the turn stopped counting at made-up text (a result, the person's message)
        self.upto = 0         # how far the text is settled: the words from it can't change, whatever comes next
        self.raw = ""         # the words from the settled text, before a speaker's name is held back (_speaker)

    @property
    def run(self) -> List[Tuple[str, str, Optional[Dict[str, Any]]]]:
        """The calls Kelvin runs. Calls in the bridge's own form, if any: models that slip into <invoke> often write
        the same calls again as <tool_call> ("I'll use the right format"), and both must not run. Otherwise the
        <invoke> calls, each once (a model that gets no result may write the same call again and again)."""
        own = [c for c in self.calls if c[0] == "tool_call"]
        if own:
            return own
        out, seen = [], set()
        for c in self.calls:
            key = (c[1], json.dumps(c[2], sort_keys=True, default=str))
            if key not in seen:
                seen.add(key)
                out.append(c)
        return out

    def blocks(self) -> List[Dict[str, Any]]:
        """Its words, then the calls Kelvin runs."""
        words = self.words.strip()
        return ([{"type": "text", "text": words}] if words else []) + [
            {"type": "tool_call", "id": "cc_" + uuid.uuid4().hex[:10], "name": name, "input": args}
            for _, name, args in self.run]


def read(text: str, final: bool = True, schemas: Optional[Dict[str, Any]] = None, start: int = 0) -> Reading:
    """A turn as Kelvin takes it. final=False reads a turn still arriving: whatever might still turn out to be
    markup (a tag half written, a call not yet closed) is left out of the words for now, and nothing shown so far
    changes later. schemas: the tools' argument schemas, so a <parameter> gets its type (5 a number, "smu" text).
    start: read on from there (a place an earlier reading settled, Reading.upto)."""
    r, words, pos = Reading(), [], start
    while pos < len(text):
        lt = text.find("<", pos)
        if lt < 0:
            words.append(text[pos:])
            pos = len(text)
            break
        code = _in_code(text, lt)
        m = None if code else TAG.match(text, lt)
        if m is None:
            if not final and not code and _maybe_tag(text, lt):   # a tag may be arriving
                words.append(text[pos:lt])
                pos = lt
                break
            words.append(text[pos:lt + 1])
            pos = lt + 1
            continue
        words.append(text[pos:lt])
        pos = lt
        closing, tag, attrs = m.group(1) == "/", m.group(2), m.group(3)
        if not closing and (tag in RESULT_TAGS or tag == "message" and "user" in attrs):
            # only Kelvin writes results, and only the person their messages: what follows is made up
            r.made_up = r.made_up or tag in RESULT_TAGS
            r.markup = r.markup or tag in RESULT_TAGS
            r.stopped = True
            break
        if tag in CALL_TAGS + RESULT_TAGS:
            r.markup = True
        if closing or tag not in ("tool_call", "invoke"):
            pos = m.end()   # a wrapper, a stray closer, the transcript's own tags: dropped
            continue
        name = (NAME.search(attrs) or [None, ""])[1].strip()
        if attrs.rstrip().endswith("/"):   # <invoke name="lab_status"/>: no arguments
            if name:
                r.calls.append((tag, name, {}))
            pos = m.end()
            continue
        end, made, after = CLOSE.search(text, m.end()), _find(MADE_UP, text, m.end()), _find(OPENER, text, m.end())
        if end is not None and all(x is None or end.start() < x.start() for x in (made, after)):
            if name:
                props = ((schemas or {}).get(name) or {}).get("properties") or {}
                r.calls.append((tag, name, _arguments(text[m.end():end.start()], props)))
            pos = end.end()
            continue
        nxt = min((x for x in (made, after) if x is not None), key=lambda x: x.start(), default=None)
        if not final and nxt is None:   # the call is still arriving
            break
        # never closed (cut short, or followed by a result or another call): not run, but the model hears so
        if name:
            r.calls.append((tag, name, None))
        if nxt is None:
            pos = len(text)
            break
        pos = nxt.start()
    r.upto, r.raw = pos, "".join(words)
    r.words = _held(r.raw, r.stopped, final)
    return r


def _held(words: str, stopped: bool, final: bool) -> str:
    """The words without a last line that is only a speaker's name, when the turn stopped at made-up text just
    after it; while the turn is arriving, also without a last line that may still become one."""
    cut = _speaker(words, whole=True) if stopped else -1 if final else _speaker(words, whole=False)
    return words[:cut] if cut >= 0 else words


def _find(rx: "re.Pattern[str]", text: str, at: int) -> Optional["re.Match[str]"]:
    """rx's first match from at on that isn't inside inline code."""
    m = rx.search(text, at)
    while m is not None and _in_code(text, m.start()):
        m = rx.search(text, m.end())
    return m


def _in_code(text: str, lt: int) -> bool:
    """Is the "<" at lt inside inline code, as in words about the markup ("each result comes in a `<tool_result>`")
    or a call quoted as an example? An odd number of backticks before it on its line says so (a fence's ``` aside);
    calls are written on lines of their own."""
    return text[text.rfind("\n", 0, lt) + 1:lt].replace("```", "").count("`") % 2 == 1


def _maybe_tag(text: str, lt: int) -> bool:
    """Could the end of the turn, from the "<" at lt on, still become one of the tags above?"""
    if len(text) - lt > 303:   # a tag would have ended by now
        return False
    tail = text[lt + 2:] if text.startswith("</", lt) else text[lt + 1:]
    if "<" in tail or ">" in tail:
        return False
    for head in HEADS:
        if head.startswith(tail):
            return True
        if tail.startswith(head) and not re.match(r"[\w:-]", tail[len(head):len(head) + 1]):
            return True   # the name is there, its attributes are arriving
    return False


def _speaker(words: str, whole: bool) -> int:
    """Where a last line that is only a speaker's name ("user", "Assistant:") starts, else -1. Models that go on
    writing the transcript put one before the result or message they make up. whole=False: also a line that may
    still become one."""
    end = len(words.rstrip())
    start = words.rfind("\n", 0, end) + 1
    line = words[start:end].strip().rstrip(":").rstrip().lower()
    if not line or len(line) > 10:
        return -1
    return start if line in SPEAKERS or not whole and any(s.startswith(line) for s in SPEAKERS) else -1


def _arguments(body: str, props: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A call's arguments: one JSON object, or <parameter> elements (each as its schema wants it). None when the
    body is neither, so the call isn't run and the model is told to send it again."""
    text = body.strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            value, end = json.JSONDecoder().raw_decode(text)
        except ValueError:
            return None
        # a closer of the other form may trail it (<tool_call ...>{...}</parameter></invoke>)
        return value if isinstance(value, dict) and not STRAY.sub("", text[end:]).strip() else None
    params = PARAM.findall(text)
    if not params or STRAY.sub("", PARAM.sub("", text)).strip():
        return None
    return {key: _value(value, props.get(key)) for key, value in params}


def _value(text: str, spec: Any) -> Any:
    """A <parameter>'s text as its schema wants it: as written for a string, else as JSON (a number, true, a list)
    when it reads as one of the types the schema allows."""
    text = text[1:] if text.startswith("\n") else text   # the line breaks around a long value are layout
    text = text[:-1] if text.endswith("\n") else text
    kinds = _kinds(spec)
    if kinds == {"string"}:
        return text
    try:
        value = json.loads(text)
    except ValueError:
        return text
    return value if not kinds or _kind(value) & kinds else text


def _kinds(spec: Any) -> set:
    if not isinstance(spec, dict):
        return set()
    t = spec.get("type")
    out = {t} if isinstance(t, str) else set(t) if isinstance(t, list) else set()
    for alt in (spec.get("anyOf") or []) + (spec.get("oneOf") or []):
        out |= _kinds(alt)
    return out


def _kind(value: Any) -> set:
    if isinstance(value, bool):
        return {"boolean"}
    if isinstance(value, int):
        return {"integer", "number"}
    if isinstance(value, float):
        return {"number"} | ({"integer"} if value.is_integer() else set())
    return {{str: "string", list: "array", dict: "object", type(None): "null"}[type(value)]}


def parse(text: str, tools: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Blocks from Claude's turn: its words, then the tool calls Kelvin runs."""
    return read(text, schemas={t["name"]: t.get("schema") or {} for t in tools or []}).blocks()


class Shown:
    """Streams Claude's words to the page as they come. Markup is held back until it is complete (a call shows as
    its own step), and nothing from a result the model writes itself on is shown."""

    def __init__(self) -> None:
        self.text = ""         # the turn so far
        self.base = 0          # how far it is settled: each delta is read from there on, not from the start
        self.settled = ""      # the words from the settled text
        self.stopped = False   # at made-up text: nothing after it counts
        self.sent = ""         # the words on the page

    def feed(self, delta: str) -> str:
        self.text += delta
        return self._more(self._words(final=False)) or ""

    def end(self, text: Optional[str] = None) -> Optional[str]:
        """The rest of the words, the turn being complete (text: the whole turn as finally sent, if no deltas came).
        None if the turn as sent isn't the one that streamed."""
        if text is not None and text != self.text:
            if self.text:
                return None
            self.text = text
        return self._more(self._words(final=True))

    def _words(self, final: bool) -> str:
        if not self.stopped:
            r = read(self.text, final, start=self.base)
            self.base, self.settled, self.stopped = r.upto, self.settled + r.raw, r.stopped
        return _held(self.settled, self.stopped, final)

    def _more(self, words: str) -> Optional[str]:
        if not words.startswith(self.sent):
            return None
        new, self.sent = words[len(self.sent):], words
        return new


class ClaudeCodeProvider(Provider):
    name = "claudecode"
    label = "Claude Code"
    can_search = True
    sees_images = True   # through stream-json input (stream_message); F1, F2, F3: the bridge once passed none

    def __init__(self, cmd: Optional[List[str]], cwd: Optional[Path] = None, timeout: float = 600):
        self.cmd = cmd
        self.cwd = cwd or claudecode.workdir()
        self.timeout = timeout

    async def search(self, model: str, query: str, limit: int = 8) -> List[Dict[str, str]]:
        """Claude Code's WebSearch on the person's plan: one `claude -p` run with only that tool allowed."""
        from ..websearch import PROMPT, results_in
        args = ["-p", PROMPT.format(query=query, limit=limit), "--output-format", "stream-json", "--verbose",
                "--tools", "WebSearch", "--allowedTools", "WebSearch", "--permission-mode", "dontAsk",
                "--strict-mcp-config", "--no-session-persistence", "--max-turns", "8", "--model", model or "sonnet"]
        words: List[str] = []
        ends: List[Dict[str, Any]] = []
        async for ev in claudecode.stream(self.cmd, args, "", self.cwd, timeout=300):
            if ev.get("type") in ("result", "exit"):
                ends.append(ev)
            elif ev.get("type") == "assistant" and not ev.get("parent_tool_use_id"):
                words += [b.get("text") or "" for b in ((ev.get("message") or {}).get("content") or [])
                          if isinstance(b, dict) and b.get("type") == "text"]
        problem = claudecode.failure(ends)
        if problem:
            raise RuntimeError(problem)
        final = next((str(e.get("result")) for e in ends if e.get("type") == "result" and e.get("result")), "")
        return results_in(final or "\n".join(words))[:limit]

    def available(self) -> bool:
        return bool(self.cmd)

    async def models(self) -> List[ModelInfo]:
        return [ModelInfo(mid, label, self.name) for mid, label in MODELS]

    async def stream(self, model: str, system: str, history: List[Dict[str, Any]],
                     tools: List[Dict[str, Any]], emit: Emit) -> TurnResult:
        prompt = system + ("\n\n" + PROTOCOL + "\n\n" + tool_catalog(tools) if tools else "")
        schemas = {t["name"]: t.get("schema") or {} for t in tools}
        fd, path = tempfile.mkstemp(prefix="system-", suffix=".md", dir=str(self.cwd))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prompt)
        common = ["--output-format", "stream-json", "--verbose", "--include-partial-messages", "--system-prompt-file",
                  path, "--tools", "", "--strict-mcp-config", "--no-session-persistence", "--model", model]
        pictures = has_pictures(history)
        if pictures:   # the conversation, pictures and all, as one message on standard input; the request ends it
            args = ["-p", "--input-format", "stream-json"] + common
            said: List[Any] = pieces(history)

            def stdin(extra: str = "") -> str:
                return stream_message(said + ([extra] if extra else []) + [ASK])
        else:
            args = ["-p", ASK] + common
            said_text = transcript(history)

            def stdin(extra: str = "") -> str:
                return said_text + ("\n" + extra if extra else "")
        try:
            reading = await self._turn(args, stdin(), schemas, emit)
            if tools and reading.markup and not reading.run:   # nothing Kelvin can run: once more, told why
                why = ("a tool result you wrote yourself" if reading.made_up else
                       "tool-call markup Kelvin could not read")
                log.info("Claude Code bridge: the model's turn had %s; asking once more", why)
                await emit({"type": "reset"})
                reading = await self._turn(args, stdin(RETRY % why), schemas, emit)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        result = TurnResult(reading.blocks(), model=model)
        if result.tool_calls:
            result.stop_reason = "tool_calls"
        elif tools and reading.markup:   # said plainly: a turn that looks like it did something but ran nothing
            result.note = ("The model wrote a tool's result itself instead of calling the tool, so nothing ran; "
                           "Kelvin left that part out." if reading.made_up else
                           "The model wrote tool calls Kelvin couldn't read, so nothing ran.")
        if reading.made_up:
            log.info("Claude Code bridge: dropped a tool result the model wrote itself")
        return result

    async def _turn(self, args: List[str], stdin: str, schemas: Dict[str, Any], emit: Emit) -> Reading:
        """One `claude -p` run: its words streamed to the page as they come, the whole turn read at its end."""
        shown, streamed, final, ends = Shown(), [], [], []
        async for ev in claudecode.stream(self.cmd, args, stdin, self.cwd, timeout=self.timeout):
            if ev.get("type") in ("result", "exit"):
                ends.append(ev)
            if ev.get("type") == "stream_event":
                delta = (ev.get("event") or {}).get("delta") or {}
                if delta.get("type") == "text_delta":
                    streamed.append(delta.get("text") or "")
                    text = shown.feed(delta.get("text") or "")
                    if text:
                        await emit({"type": "text", "delta": text})
                elif delta.get("type") == "thinking_delta":
                    await emit({"type": "thinking", "delta": delta.get("thinking") or ""})
            elif ev.get("type") == "assistant" and not (ev.get("parent_tool_use_id")):
                for b in ((ev.get("message") or {}).get("content") or []):
                    if isinstance(b, dict) and b.get("type") == "text":
                        final.append(b.get("text") or "")
        problem = claudecode.failure(ends)
        if problem:
            raise RuntimeError(problem)
        text = "".join(final) or "".join(streamed) or str(ends[0].get("result") or "")
        rest = shown.end(text)   # no partial messages from this version: the whole turn at once
        if rest is None:   # the turn as sent differs from what streamed: show it again, whole
            await emit({"type": "reset"})
            rest = read(text).words
        if rest:
            await emit({"type": "text", "delta": rest})
        return read(text, schemas=schemas)
