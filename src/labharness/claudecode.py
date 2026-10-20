"""TEMPORARY, to be removed: Kelvin through the Claude Code on this computer (the person's own signed-in `claude`).

Two uses:
- chats on the person's Claude plan (web/providers/claudecode_provider.py), with Kelvin's own tools;
- operate_computer: Claude Code works the screen, mouse and keyboard through Kelvin's screen tools (screen.py), for
  programs without an API (vendor software, web pages, dialogs).

Anthropic does not let third-party apps route requests through people's Claude plans, so this exists only on the
developer's own computer: when Kelvin runs from source, or in a private build that has _private_bridge.py (made by
packaging/private_build.md, never published). Kelvin starts the unmodified `claude` program and never reads, stores
or passes on Claude credentials. Removing it: delete this file, screen.py, the provider, and the code marked
"Claude Code bridge" (web/app.py, web/agent.py, web/settings.py, the Settings section, packaging/kelvin_app.py)."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional


def enabled() -> bool:
    """Is the bridge part of this copy of Kelvin? From source yes; frozen only in the private build."""
    if not getattr(sys, "frozen", False):
        return True
    try:
        from . import _private_bridge  # noqa: F401  (written only for the private build)
        return True
    except ImportError:
        return False


def find_cli(configured: str = "") -> Optional[str]:
    """The `claude` program: the path chosen in Settings, else on PATH, else where the native installer puts it."""
    exe = "claude.exe" if sys.platform.startswith("win") else "claude"
    for c in (configured.strip(), shutil.which("claude"), str(Path.home() / ".local" / "bin" / exe)):
        if c and Path(c).is_file():
            return c
    return None


def child_env() -> Dict[str, str]:
    """The person's own sign-in, not an API key that happens to be in Kelvin's environment."""
    env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    return env


def workdir(home: Optional[Path] = None) -> Path:
    """An empty folder to run in, so no project's .mcp.json, hooks or CLAUDE.md come along."""
    d = Path(home or Path.home() / "Kelvin") / "claude-code"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0


def status(cmd: List[str], timeout: float = 20) -> Dict[str, Any]:
    """Is Claude Code signed in? (`claude auth status`; says how, never with what)."""
    try:
        p = subprocess.run(cmd + ["auth", "status"], capture_output=True, text=True, timeout=timeout, env=child_env(),
                           creationflags=_flags())
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"signed_in": False, "error": str(e)}
    try:
        data = json.loads(p.stdout or "{}")
    except ValueError:
        data = {}
    method = data.get("authMethod") if isinstance(data, dict) else None
    return {"signed_in": p.returncode == 0 and method not in (None, "none"), "method": method}


async def stream(cmd: List[str], args: List[str], stdin_text: str, cwd: Path,
                 should_stop: Callable[[], bool] = lambda: False, timeout: float = 600) -> AsyncIterator[Dict[str, Any]]:
    """Run `claude <args>` and yield its stream-json events. Stops (and kills it) on should_stop(), the timeout, or
    when the consumer is cancelled. Uses threads, not asyncio subprocesses, so it works on any event loop."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    proc = subprocess.Popen(cmd + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=str(cwd), env=child_env(), creationflags=_flags())
    err: List[bytes] = []

    def feed() -> None:
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass

    def read() -> None:
        for line in proc.stdout:
            loop.call_soon_threadsafe(queue.put_nowait, line)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    for target in (feed, read, lambda: err.append(proc.stderr.read())):
        threading.Thread(target=target, daemon=True).start()
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                line = await asyncio.wait_for(queue.get(), 0.5)
            except asyncio.TimeoutError:
                if should_stop():
                    raise StopRequested()
                if time.monotonic() > deadline:
                    raise TimeoutError("Claude Code took longer than %d s" % timeout)
                continue
            if line is None:
                break
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event
        code = await asyncio.to_thread(proc.wait)
        if code not in (0, None):
            text = b"".join(err).decode("utf-8", "replace").strip()
            yield {"type": "exit", "code": code, "stderr": text[-500:]}
    finally:
        if proc.poll() is None:
            proc.kill()


class StopRequested(Exception):
    """The person pressed Stop."""


def failure(ends: List[Dict[str, Any]]) -> Optional[str]:
    """Why a run failed, from its result and exit events: the result's own message first (such as "Not logged
    in"), else the program's exit."""
    for event in ends:
        if event.get("type") == "result" and (event.get("is_error") or str(event.get("subtype", "success")) != "success"):
            return str(event.get("result") or event.get("subtype") or "Claude Code failed")
    for event in ends:
        if event.get("type") == "exit":
            return "Claude Code stopped (exit %s): %s" % (event.get("code"), event.get("stderr") or "no message")
    return None if any(e.get("type") == "result" for e in ends) else "Claude Code gave no answer"
