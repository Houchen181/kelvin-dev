"""The reference arm: Claude Code (claude -p) working the bench lab through Kelvin's MCP server, as a person would run
Claude Code with kelvin-mcp.

The worker serves Kelvin's MCP server (agent.server.build_server, kelvin-mcp's tools, instructions and gate) over
streamable HTTP on a local port, with the same runtime, lab and simulated person as Kelvin's own arms, and runs
claude -p once per person message, resuming the same session. It may use Kelvin's MCP tools and the read-only tools
(Read, Glob, Grep) and nothing else (--permission-mode dontAsk with --allowedTools): no shell and no writing files,
so every change to the lab goes through Kelvin's gate, as in Kelvin's own arms.

On the person's plan (--auth plan) it is their signed-in Claude Code without their user settings (no plugins or
hooks: --setting-sources project,local, in an empty folder); on an API key (--auth key) it runs --bare in a config
folder of its own.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


def find_claude(given: str = "") -> str:
    exe = "claude.exe" if os.name == "nt" else "claude"
    for place in (given, shutil.which("claude") or "", str(Path.home() / ".local" / "bin" / exe)):
        if place and Path(place).is_file():
            return place
    raise SystemExit("claude CLI not found: pass --claude PATH")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class McpServer:
    """Kelvin's MCP server on 127.0.0.1:<port>/mcp, in a thread of this process."""

    def __init__(self, runtime: Any):
        import uvicorn
        from labharness.agent.server import build_server
        self.port = _free_port()
        app = build_server(runtime).streamable_http_app(host="127.0.0.1")
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="kbench-mcp")

    def __enter__(self) -> "McpServer":
        self.thread.start()
        end = time.monotonic() + 30
        while not self.server.started and time.monotonic() < end:
            time.sleep(0.05)
        if not self.server.started:
            raise RuntimeError("Kelvin's MCP server didn't start")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        self.thread.join(10)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/mcp" % self.port


def to_messages(lines: List[Dict[str, Any]], text: str) -> List[Dict[str, Any]]:
    """Claude Code's stream-json for one person message, as Kelvin keeps a chat: user, assistant (blocks), tool."""
    out: List[Dict[str, Any]] = [{"role": "user", "text": text}]
    names: Dict[str, str] = {}
    last_id = None
    for e in lines:
        if e.get("type") == "assistant":
            msg = e.get("message") or {}
            blocks = []
            for b in msg.get("content") or []:
                if b.get("type") == "text" and b.get("text"):
                    blocks.append({"type": "text", "text": b["text"]})
                elif b.get("type") == "thinking" and b.get("thinking"):
                    blocks.append({"type": "thinking", "text": b["thinking"]})
                elif b.get("type") == "tool_use":
                    names[b.get("id")] = b.get("name") or ""
                    blocks.append({"type": "tool_call", "id": b.get("id"), "name": b.get("name"), "input": b.get("input")})
            if out and out[-1]["role"] == "assistant" and msg.get("id") and msg.get("id") == last_id:
                out[-1]["blocks"] += blocks   # one message streamed as several events
            else:
                out.append({"role": "assistant", "blocks": blocks, "model": msg.get("model")})
            last_id = msg.get("id")
        elif e.get("type") == "user":
            results = []
            for b in (e.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    content = b.get("content")
                    if isinstance(content, list):
                        content = "\n".join(str(c.get("text") or "") for c in content if isinstance(c, dict))
                    results.append({"id": b.get("tool_use_id"), "name": names.get(b.get("tool_use_id"), ""),
                                    "content": str(content or ""), "is_error": bool(b.get("is_error"))})
            if results:
                out.append({"role": "tool", "results": results})
            last_id = None
    return out


def run_claude_code(args: Any, ep: Any, task: Any, env: Any, desk: Any) -> List[Dict[str, Any]]:
    from .meter import Ledger, message_id, run_entry
    from .worker import next_message
    if args.mode != "real":
        raise SystemExit("the claude-code arm has no scripted mode: it is checked with a real model only")
    claude = find_claude(args.claude)
    work = env.work or (ep.out / "work")
    work.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(args.cap_usd)
    ep.ledger = ledger
    session = str(uuid.uuid4())
    messages: List[Dict[str, Any]] = []
    deadline = time.monotonic() + task.max_minutes * 60
    child_env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "MCP_", "ANTHROPIC"))}
    child_env.update(DISABLE_AUTOUPDATER="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    if args.auth == "key":   # bare, on the benchmark's key, in a config folder of its own
        key = os.environ.get(args.key_env or "", "")
        if not key:
            raise SystemExit("no API key in $%s" % args.key_env)
        config_dir = ep.out / "claude-config"
        config_dir.mkdir(parents=True, exist_ok=True)
        child_env.update(ANTHROPIC_API_KEY=key, CLAUDE_CONFIG_DIR=str(config_dir))
        isolation = ["--bare"]
    else:   # the person's own signed-in Claude Code, on their plan, without their user settings (plugins, hooks)
        isolation = ["--setting-sources", "project,local"]
    with McpServer(env.runtime) as mcp:
        (ep.out / "mcp.json").write_text(json.dumps({"mcpServers": {"kelvin": {"type": "http", "url": mcp.url}}}),
                                         encoding="utf-8")
        later = list(task.person.later)
        used = {"nudges": 0}
        text: Optional[str] = task.prompt
        turn = 0
        while text is not None:
            left = deadline - time.monotonic()
            spent = ledger.usd
            if left <= 0 or (args.cap_usd and spent >= args.cap_usd):
                ep.meta["stopped_by_bench"] = True
                break
            # Kelvin's MCP tools and the read-only tools are allowed; everything else is refused without asking
            # (dontAsk): no shell, no writing files, so every change to the lab goes through Kelvin's gate
            allowed = ",".join(["mcp__kelvin"] + [t for t in (args.tools or "").split(",") if t.strip()])
            cmd = [claude, "-p", text, "--output-format", "stream-json", "--verbose"] + isolation + [
                   "--strict-mcp-config", "--mcp-config", str(ep.out / "mcp.json"), "--model", args.model,
                   "--permission-mode", "dontAsk", "--tools", args.tools or "", "--allowedTools", allowed]
            cmd += ["--session-id", session] if turn == 0 else ["--resume", session]
            if args.cap_usd:
                cmd += ["--max-budget-usd", "%.2f" % max(0.05, args.cap_usd - spent)]
            if args.effort:
                cmd += ["--effort", args.effort]
            t0 = time.time()
            lines: List[Dict[str, Any]] = []
            ended, error, stderr = "none", None, ""
            raw = open(ep.out / ("claude-turn%d.jsonl" % turn), "w", encoding="utf-8")
            try:
                proc = subprocess.Popen(cmd, cwd=str(work), env=child_env, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                                        encoding="utf-8", errors="replace")
                killer = threading.Timer(max(10.0, left), proc.kill)
                killer.start()
                try:
                    for line in proc.stdout:
                        raw.write(line)
                        line = line.strip()
                        if not line.startswith("{"):
                            continue
                        try:
                            e = json.loads(line)
                        except ValueError:
                            continue
                        lines.append(e)
                        ep.event(turn, {"type": "cc_" + str(e.get("type")), "subtype": e.get("subtype")})
                    proc.wait()
                    stderr = proc.stderr.read()
                finally:
                    killer.cancel()
            finally:
                raw.close()
            result = next((e for e in reversed(lines) if e.get("type") == "result"), None)
            init = next((e for e in lines if e.get("type") == "system" and e.get("subtype") == "init"), None)
            if init and turn == 0:
                ep.meta["claude_code"] = {"version": init.get("claude_code_version"), "model": init.get("model"),
                                          "tools": len(init.get("tools") or []),
                                          "mcp": init.get("mcp_servers"), "permission": init.get("permissionMode")}
            if result is None:
                ended, error = "error", "claude -p ended without a result (exit %s): %s" % (proc.returncode, stderr[-800:])
            else:
                ended = "end" if result.get("subtype") == "success" and not result.get("is_error") else str(
                    result.get("subtype") or "error")
                if result.get("is_error"):
                    error = str(result.get("result") or result.get("subtype"))[:800]
            if result is not None or lines:   # its dollars from this turn's own usage (run_entry says why)
                calls = len({m for m in map(message_id, lines) if m})
                ledger.add(run_entry("claude -p (reference)", result, args.model, args.effort, calls, t0,
                                     turn=turn, model_usage=(result or {}).get("modelUsage")))
            said_msgs = to_messages(lines, text)
            messages += said_msgs
            said = "\n".join(b.get("text") or "" for m in said_msgs if m["role"] == "assistant"
                             for b in m["blocks"] if b.get("type") == "text").strip()
            ep.meta["turns"].append({"text": text, "ended": ended, "error": error, "seconds": round(time.time() - t0, 2),
                                     "said_chars": len(said)})
            if error:
                ep.error("turn %d: %s" % (turn, error))
            turn += 1
            text = None
            if later:
                nxt = later.pop(0)
                if nxt.before:
                    nxt.before(env)
                text = nxt.text
            else:
                text = next_message(task.person, said, "step_limit" if ended == "error_max_turns" else ended, used)
        ep.meta["nudges"] = used["nudges"]
        ep.meta.setdefault("stopped_by_bench", False)
        if env.runtime.queue.state in ("running", "pausing"):
            env.wait_queue(120)
    ep.cc_totals = ledger.totals()
    return messages
