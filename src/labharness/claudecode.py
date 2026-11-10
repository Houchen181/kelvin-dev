"""TEMPORARY, to be removed: Kelvin through the Claude Code on this computer (the person's own signed-in `claude`).

Two uses:
- chats on the person's Claude plan (web/providers/claudecode_provider.py), with Kelvin's own tools;
- operate_computer: Claude Code works the screen, mouse and keyboard through Kelvin's screen tools (screen.py), for
  programs without an API (vendor software, web pages, dialogs).

Every Kelvin has it since 0.3.50: from source, the private build and the published app. Kelvin starts the unmodified
`claude` program the person installed and signed in to themselves, and never reads, stores or passes on Claude
credentials. Removing it: delete this file, screen.py, the provider, and the code marked "Claude Code bridge"
(web/app.py, web/agent.py, web/settings.py, the Settings section, packaging/kelvin_app.py)."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional


def enabled() -> bool:
    """Is the bridge part of this copy of Kelvin? Yes, in every build; published builds have it since 0.3.50."""
    return True


MAC_BIN = ("/opt/homebrew/bin", "/usr/local/bin")   # where Homebrew (Apple silicon, Intel) and npm put programs


def find_cli(configured: str = "") -> Optional[str]:
    """The `claude` program: the path chosen in Settings, else on PATH, else where the native installer puts it, and
    on a Mac where Homebrew and npm put it (an app there starts with a short PATH)."""
    exe = "claude.exe" if sys.platform.startswith("win") else "claude"
    places = [configured.strip(), shutil.which("claude"), str(Path.home() / ".local" / "bin" / exe)]
    if sys.platform == "darwin":
        places += [d + "/claude" for d in MAC_BIN]
    for c in places:
        if c and Path(c).is_file():
            return c
    return None


def child_env() -> Dict[str, str]:
    """The person's own sign-in, not an API key that happens to be in Kelvin's environment. On a Mac, Homebrew's and
    npm's places join PATH: an npm install's `claude` runs `node` from there."""
    env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    if sys.platform == "darwin":
        have = [p for p in env.get("PATH", "").split(os.pathsep) if p]
        env["PATH"] = os.pathsep.join(have + [d for d in MAC_BIN if d not in have])
    return env


def workdir(home: Optional[Path] = None) -> Path:
    """An empty folder to run in, so no project's .mcp.json, hooks or CLAUDE.md come along."""
    d = Path(home or Path.home() / "Kelvin") / "claude-code"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0


def _started() -> Dict[str, Any]:
    """How the program is started: without a window on Windows; elsewhere in a process group of its own, so what it
    starts ends with it (kill_tree)."""
    if sys.platform.startswith("win"):
        return {"creationflags": _flags()}
    return {"start_new_session": True}


def kill_tree(proc: Any) -> None:
    """The program ended, and every process it started. An npm install's `claude` is a launcher (claude.CMD) running
    node: Stop and the time limit killed the launcher alone, and node finished its answer, or went on working the
    screen. Windows: taskkill /T over the tree, while the launcher (which waits for node) still runs; elsewhere the
    process group it was started in."""
    if sys.platform.startswith("win"):
        if proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                               creationflags=_flags())
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def status(cmd: List[str], timeout: float = 20) -> Dict[str, Any]:
    """Is Claude Code signed in? (`claude auth status`; says how, never with what). Waits for the program, within
    `timeout`, and never for its output's pipe: on Windows a child of the launcher kept that open, and waiting for it
    held the Settings page's look for as long as the child lived (subprocess.run's timeout waits for it again)."""
    try:
        proc = subprocess.Popen(cmd + ["auth", "status"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=child_env(), **_started())
    except OSError as e:
        return {"signed_in": False, "error": str(e)}
    chunks: List[bytes] = []

    def read() -> None:   # what it says, as it says it: a child left holding the pipe doesn't keep it back
        try:
            for chunk in iter(lambda: proc.stdout.read1(65536), b""):
                chunks.append(chunk)
        except (OSError, ValueError):
            pass
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        return {"signed_in": False, "error": "`claude auth status` didn't answer within %d s" % round(timeout)}
    reader.join(2.0)   # the rest of its output; then whatever it said is what counts
    try:
        data = json.loads(b"".join(chunks).decode("utf-8", "replace") or "{}")   # UTF-8 whatever the locale
    except ValueError:
        data = {}
    method = data.get("authMethod") if isinstance(data, dict) else None
    return {"signed_in": code == 0 and method not in (None, "none"), "method": method}


async def stream(cmd: List[str], args: List[str], stdin_text: str, cwd: Path,
                 should_stop: Callable[[], bool] = lambda: False, timeout: float = 600) -> AsyncIterator[Dict[str, Any]]:
    """Run `claude <args>` and yield its stream-json events. Stops (and ends it, with everything it started:
    kill_tree) on should_stop(), the timeout, or when the consumer is cancelled. Uses threads, not asyncio
    subprocesses, so it works on any event loop."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    proc = subprocess.Popen(cmd + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=str(cwd), env=child_env(), **_started())
    err: List[bytes] = []
    finished = False

    def feed() -> None:
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass

    def read() -> None:
        try:
            for line in proc.stdout:
                loop.call_soon_threadsafe(queue.put_nowait, line)
            loop.call_soon_threadsafe(queue.put_nowait, None)
        except RuntimeError:   # the loop closed meanwhile (the consumer stopped and went): nobody is left to tell
            pass

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
        finished = True
        if code not in (0, None):
            text = b"".join(err).decode("utf-8", "replace").strip()
            yield {"type": "exit", "code": code, "stderr": text[-500:]}
    finally:
        if not finished or proc.poll() is None:   # stopped, out of time or cancelled: nothing it started goes on
            kill_tree(proc)


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
