"""TEMPORARY (Claude Code bridge, see claudecode.py): chats answered by the Claude Code on this computer.

Claude Code runs with its own tools switched off and Kelvin's system prompt. Kelvin's tools are described in that
prompt and called as <tool_call name="...">{json}</tool_call>; Kelvin runs them in its own loop, through the
safety gate like any model's calls, and sends the results back in the next turn. Each turn is one `claude -p`
run that is not saved as a Claude Code session."""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from ... import claudecode
from .base import Emit, ModelInfo, Provider, TurnResult, user_text

MODELS = [("opus", "Claude Opus (Claude Code)"), ("sonnet", "Claude Sonnet (Claude Code)"),
          ("haiku", "Claude Haiku (Claude Code)")]
CALL = re.compile(r'<tool_call\s+name="([^"]+)"\s*>(.*?)</tool_call>', re.S)
OPEN = "<tool_call"
ASK = "Continue the conversation on standard input as Kelvin: write Kelvin's next turn."
PROTOCOL = """# How to call Kelvin's tools here
You have no built-in tools in this conversation. To use one of the tools below, write on its own lines:
<tool_call name="TOOL_NAME">{"argument": "value"}</tool_call>
with the arguments as one JSON object that fits the tool's schema. You may call several tools in a row; then stop
writing. Kelvin runs them in order and continues the conversation with each result in a <tool_result> element.
Never write a <tool_result> yourself and never state a tool's result before you have it.

# Tools"""


def tool_catalog(tools: List[Dict[str, Any]]) -> str:
    return "\n\n".join("## %s\n%s\nArguments (JSON schema): %s" % (t["name"], t.get("description") or "",
                                                                      json.dumps(t.get("schema") or {}))
                       for t in tools)


def transcript(history: List[Dict[str, Any]]) -> str:
    out = ["<conversation>"]
    for m in history:
        if m["role"] == "user":
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
                out.append('<tool_result name="%s" error="%s">\n%s\n</tool_result>'
                           % (r["name"], "true" if r.get("is_error") else "false", r.get("content") or "(empty)"))
    out.append("</conversation>")
    return "\n".join(out)


def parse(text: str) -> List[Dict[str, Any]]:
    """Blocks from Claude's turn: its words, then the tool calls it wrote."""
    blocks: List[Dict[str, Any]] = []
    words = CALL.sub("", text).strip()
    if words:
        blocks.append({"type": "text", "text": words})
    for name, body in CALL.findall(text):
        try:
            args = json.loads(body.strip() or "{}")
        except ValueError:
            args = None
        blocks.append({"type": "tool_call", "id": "cc_" + uuid.uuid4().hex[:10], "name": name.strip(),
                       "input": args if isinstance(args, dict) else None})
    return blocks


class Shown:
    """Streams Claude's words to the page, holding back the tool-call markup (a call shows as its own step)."""

    def __init__(self) -> None:
        self.buf = ""
        self.hiding = False

    def feed(self, delta: str) -> str:
        self.buf += delta
        out = ""
        while self.buf:
            if self.hiding:
                end = self.buf.find("</tool_call>")
                if end < 0:
                    return out
                self.buf, self.hiding = self.buf[end + len("</tool_call>"):], False
                continue
            start = self.buf.find(OPEN)
            if start >= 0:
                out, self.buf, self.hiding = out + self.buf[:start], self.buf[start:], True
                continue
            keep = next((k for k in range(min(len(OPEN) - 1, len(self.buf)), 0, -1)
                         if OPEN.startswith(self.buf[-k:])), 0)   # a tag may be arriving
            out, self.buf = out + self.buf[:len(self.buf) - keep], self.buf[len(self.buf) - keep:]
            return out
        return out


class ClaudeCodeProvider(Provider):
    name = "claudecode"
    label = "Claude Code"

    def __init__(self, cmd: Optional[List[str]], cwd: Optional[Path] = None, timeout: float = 600):
        self.cmd = cmd
        self.cwd = cwd or claudecode.workdir()
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.cmd)

    async def models(self) -> List[ModelInfo]:
        return [ModelInfo(mid, label, self.name) for mid, label in MODELS]

    async def stream(self, model: str, system: str, history: List[Dict[str, Any]],
                     tools: List[Dict[str, Any]], emit: Emit) -> TurnResult:
        prompt = system + ("\n\n" + PROTOCOL + "\n\n" + tool_catalog(tools) if tools else "")
        fd, path = tempfile.mkstemp(prefix="system-", suffix=".md", dir=str(self.cwd))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prompt)
        args = ["-p", ASK, "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                "--system-prompt-file", path, "--tools", "", "--strict-mcp-config", "--no-session-persistence",
                "--model", model]
        shown, streamed, final, ends = Shown(), [], [], []
        try:
            async for ev in claudecode.stream(self.cmd, args, transcript(history), self.cwd, timeout=self.timeout):
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
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        problem = claudecode.failure(ends)
        if problem:
            raise RuntimeError(problem)
        text = "".join(final) or "".join(streamed) or str(ends[0].get("result") or "")
        if not streamed:   # no partial messages from this version: show the turn at once
            shown = Shown()
            rest = shown.feed(text)
        else:
            rest = ""
        if not shown.hiding:
            rest += shown.buf   # the end held back in case a tag was starting
        if rest:
            await emit({"type": "text", "delta": rest})
        blocks = parse(text)
        result = TurnResult(blocks, model=model)
        if result.tool_calls:
            result.stop_reason = "tool_calls"
        return result
