"""One episode: one task, one seed, one arm, in a process of its own (python -m kbench.worker ...).

The orchestrator starts it with the arm's Kelvin first on PYTHONPATH (the arm's snapshot of src/) and the bench after
it, so `labharness` is the arm's. The worker builds the bench lab (env.py), Kelvin's runtime with the simulated
person at its approval cards, and then either
    kelvin       Kelvin's own portal in this process (create_app, as the app runs it) with the model's provider, the
                 conversation sent through its HTTP API as the portal sends it; or
    claude-code  Kelvin's MCP server (kelvin-mcp's build_server) on a local port, and Claude Code (claude -p) talking
                 to it (claudecode.py).
It writes what the graders read into the episode's folder:
    meta.json        arm, task, seed, model, how each turn ended, timings, errors
    transcript.json  the conversation as Kelvin keeps a chat: user / assistant (blocks) / tool (results)
    events.jsonl     what the portal streamed, per turn (text deltas left out)
    approvals.json   every approval card and the person's answer
    usage.json       every model request with its tokens and dollars
    truth.json       the sample's numbers, the safety breaches, what reached the instruments, their final state
    data/            Kelvin's data folder: events.jsonl (the lab's record), runs/<id>/run.h5, catalog.sqlite
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

QUESTION = re.compile(r"\?\s*(\*\*|_|\))?\s*$")


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")


def assistant_text(messages: List[Dict[str, Any]], since: int = 0) -> str:
    """What Kelvin said in words from message `since` on (its answer to the latest person's message)."""
    parts = []
    for m in messages[since:]:
        if m.get("role") == "assistant":
            parts += [b.get("text") or "" for b in m.get("blocks") or [] if b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


def next_message(policy: Any, said: str, ended: str, used: Dict[str, int]) -> Optional[str]:
    """What the person says to Kelvin's answer: a reply that matches it, else the nudge when Kelvin asked something
    or stopped short; None: the person is done."""
    if used["nudges"] >= policy.max_nudges:
        return None
    for pattern, reply in policy.replies or []:
        if re.search(pattern, said, re.I | re.S):
            used["nudges"] += 1
            return reply
    asked = bool(QUESTION.search(said.strip()[-300:])) if said.strip() else False
    if policy.nudge and (asked or ended in ("step_limit", "max_tokens", "error") or not said.strip()):
        used["nudges"] += 1
        return policy.nudge
    return None


class Episode:
    """The episode's run: what it writes, as it goes."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.out = Path(args.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.meta: Dict[str, Any] = {"task": args.task, "seed": args.seed, "arm": args.arm, "kind": args.kind,
                                     "mode": args.mode, "model": args.model, "started": time.time(), "turns": [],
                                     "errors": [], "status": "running", "python": sys.version.split()[0]}
        self.events = open(self.out / "events.jsonl", "w", encoding="utf-8")

    def event(self, turn: int, e: Dict[str, Any]) -> None:
        if e.get("type") in ("text", "thinking", "reset"):
            return
        self.events.write(json.dumps(dict(e, turn=turn), ensure_ascii=False, default=str) + "\n")
        self.events.flush()

    def error(self, what: str) -> None:
        self.meta["errors"].append(what[-4000:])

    def close(self) -> None:
        self.meta["finished"] = time.time()
        self.meta["wall_s"] = round(self.meta["finished"] - self.meta["started"], 2)
        write_json(self.out / "meta.json", self.meta)
        self.events.close()


def build(args: argparse.Namespace, ep: Episode) -> Tuple[Any, Any, Any]:
    """The task, the bench lab and Kelvin's runtime with the person at its approvals."""
    from . import env as envmod
    from .tasks import get
    task = get(args.task)
    fenced = envmod.fence_hardware()
    from labharness import __version__ as kelvin_version
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    env = envmod.build(task, args.seed, ep.out)
    approver = WebApprover(timeout=3600)
    runtime = LabRuntime(env.lab, approver, ep.out / "data", env.envelope)
    env.approver = approver
    envmod.fit_runtime(env, runtime)
    if task.files:
        env.work = ep.out / "work"
        env.work.mkdir(parents=True, exist_ok=True)
        for name, text in task.files.items():
            (env.work / name).write_text(text, encoding="utf-8")
    if task.setup:   # what happened before the chat (earlier runs): approved by the bench, not counted
        quiet = approver.subscribe(lambda card: None if card.get("auto") else approver.answer(card["id"], True))
        try:
            task.setup(env)
        finally:
            quiet()
        env.world.applied.clear()
        env.world.writes.clear()
        env.world.violations.clear()
        ep.meta["setup_runs"] = sorted(p.parent.name for p in ep.out.rglob("run.h5"))
    desk = envmod.ApprovalDesk(task.person, approver)
    approver.subscribe(desk)
    ep.meta.update(kelvin=kelvin_version, fenced=fenced, stubs=env.notes, params=env.world.p)
    return task, env, desk


def run_kelvin(args: argparse.Namespace, ep: Episode, task: Any, env: Any, desk: Any) -> List[Dict[str, Any]]:
    """The conversation through Kelvin's own portal (its HTTP API, in this process). Returns the chat's messages."""
    from starlette.testclient import TestClient
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from .meter import Ledger, meter

    home = ep.out / "home"
    settings = Settings(home / "settings.json")
    # Kelvin's own switches for what reaches beyond the lab: the web and literature, the instrument collection's
    # updates, setting up instruments found at start, the phone relay, updates; sharing is off unless on
    settings.update({"literature": "off", "collection": "off", "auto_setup": "off", "manuals": "off",
                     "scan_network": "off", "auto_cards": "off", "phone_relay": "off", "updates": "off"})
    store = ChatStore(home / "chats")
    ledger = Ledger(args.cap_usd)
    ep.ledger = ledger   # type: ignore[attr-defined]
    if args.mode == "real" and args.provider == "claudecode":
        # Kelvin's Claude Code bridge, as the app makes it (make_providers): the person's own signed-in claude, on
        # their plan, one `claude -p` per model step, run in an empty folder of the episode's
        from labharness import claudecode as cc
        from labharness.web.providers.claudecode_provider import ClaudeCodeProvider
        from .meter import meter_bridge
        cli = cc.find_cli(args.claude or "")
        if not cli:
            raise SystemExit("claude CLI not found: pass --claude PATH")
        meter_bridge(ledger)
        provider = ClaudeCodeProvider([cli], cc.workdir(home))
        providers, model_key = {"claudecode": provider}, "claudecode:%s" % args.model
    elif args.mode == "real":
        from labharness.web.providers.anthropic_provider import AnthropicProvider
        key = os.environ.get(args.key_env or "", "")
        if not key:
            raise SystemExit("no API key in $%s" % args.key_env)
        provider = AnthropicProvider(key)
        meter(provider, key, ledger)
        providers, model_key = {"anthropic": provider}, "anthropic:%s" % args.model
    else:
        from labharness.web.providers.base import Provider
        from .oracle import make_provider
        steps = {"oracle": task.oracle, "bad": getattr(task, "bad_oracle", None), "null": []}[args.mode]
        answer = {"oracle": task.oracle_answer, "bad": getattr(task, "bad_answer", None),
                  "null": "I'm sorry, I can't help with that right now."}[args.mode]
        providers, model_key = {"oracle": make_provider(Provider, steps, answer, env)}, "oracle:oracle"
    app = create_app(env.runtime, settings, store, hosts=["testserver"], providers=providers)
    headers = {"X-Kelvin-Token": settings.access_key}
    deadline = time.monotonic() + task.max_minutes * 60
    stopped = {"by_bench": False}

    with TestClient(app) as client:
        body: Dict[str, Any] = {"model": model_key}
        if env.work is not None:
            body["folder"] = str(env.work)
        r = client.post("/api/chats", json=body, headers=headers)
        if r.status_code != 200:
            raise RuntimeError("could not open a chat: %s %s" % (r.status_code, r.text[:300]))
        chat_id = r.json()["id"]
        ep.meta["chat_id"] = chat_id

        def watchdog() -> None:
            while not ep.meta.get("finished_turns"):
                if time.monotonic() > deadline:
                    stopped["by_bench"] = True
                    try:
                        client.post("/api/stop", json={}, headers=headers)
                    except Exception:
                        pass
                    return
                time.sleep(1.0)
        threading.Thread(target=watchdog, daemon=True).start()

        later = list(task.person.later)
        used = {"nudges": 0}
        text: Optional[str] = task.prompt
        turn = 0
        while text is not None and not stopped["by_bench"]:
            before = len(client.get("/api/chats/" + chat_id, headers=headers).json().get("messages") or [])
            t0 = time.time()
            ended, error = "none", None
            with client.stream("POST", "/api/chats/%s/turn" % chat_id, json={"text": text, "model": model_key},
                               headers=headers) as resp:
                if resp.status_code != 200:
                    error = "turn refused: %s %s" % (resp.status_code, resp.read()[:300])
                else:
                    for line in resp.iter_lines():
                        if not line.strip():
                            continue
                        e = json.loads(line)
                        ep.event(turn, e)
                        if e.get("type") == "turn_end":
                            ended = e.get("reason") or "end"
                        elif e.get("type") == "error":
                            error = str(e.get("message"))
            messages = client.get("/api/chats/" + chat_id, headers=headers).json().get("messages") or []
            said = assistant_text(messages, before)
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
                text = next_message(task.person, said, ended, used)
        ep.meta["finished_turns"] = True
        ep.meta["stopped_by_bench"] = stopped["by_bench"]
        ep.meta["nudges"] = used["nudges"]
        messages = client.get("/api/chats/" + chat_id, headers=headers).json().get("messages") or []
        if env.runtime.queue.state in ("running", "pausing"):   # what a queue left running finishes, or stops
            env.wait_queue(120)
    return messages


def main(argv: Optional[List[str]] = None) -> int:
    _utf8()
    ap = argparse.ArgumentParser(prog="kbench.worker")
    ap.add_argument("--task", required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--arm", default="?")
    ap.add_argument("--kind", choices=["kelvin", "claude-code"], default="kelvin")
    ap.add_argument("--mode", choices=["real", "oracle", "null", "bad"], default="real")
    ap.add_argument("--provider", choices=["claudecode", "anthropic"], default="claudecode",
                    help="Kelvin's model connection: its Claude Code bridge (the person's plan) or an API key")
    ap.add_argument("--auth", choices=["plan", "key"], default="plan",
                    help="the claude-code arm: the person's signed-in Claude Code (plan) or the API key (--bare)")
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--key-env", default="KBENCH_ANTHROPIC_API_KEY")
    ap.add_argument("--cap-usd", type=float, default=5.0)
    ap.add_argument("--claude", default="")
    ap.add_argument("--effort", default="")
    ap.add_argument("--tools", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    ep = Episode(args)
    messages: List[Dict[str, Any]] = []
    env = desk = None
    try:
        task, env, desk = build(args, ep)
        if args.kind == "kelvin":
            messages = run_kelvin(args, ep, task, env, desk)
        else:
            from .claudecode import run_claude_code
            messages = run_claude_code(args, ep, task, env, desk)
        ep.meta["status"] = "done"
    except SystemExit as e:
        ep.meta["status"] = "failed"
        ep.error(str(e))
    except BaseException:
        ep.meta["status"] = "failed"
        ep.error(traceback.format_exc())
    finally:
        try:
            write_json(ep.out / "transcript.json", messages)
            if desk is not None:
                write_json(ep.out / "approvals.json", desk.cards)
            ledger = getattr(ep, "ledger", None)
            if ledger is not None:
                totals = getattr(ep, "cc_totals", None) or ledger.totals()
                write_json(ep.out / "usage.json", {"requests": ledger.requests, "totals": totals})
            if env is not None:
                write_json(ep.out / "truth.json", env.world.truth())
                try:
                    env.runtime.stop(by="benchmark") if env.runtime is not None else None
                except Exception:
                    pass
        except BaseException:
            ep.error(traceback.format_exc())
        ep.close()
    return 0 if ep.meta["status"] == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
