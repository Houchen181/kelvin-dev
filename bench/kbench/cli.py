"""python bench/run.py COMMAND ... (see bench/README.md)

    list                       the tasks and the arms
    check                      play every task's oracle (and a do-nothing model, and a wrong solution where the task
                               has one) through Kelvin: free; shows each task works and its checks tell good from bad
    run                        real episodes: arms x tasks x repeats, in parallel, within a budget
    grade LABEL                grade a run's episodes again (after a check changed)
    report LABEL               write the report of a run (Markdown and JSON)
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import arms as armmod
from .checks import grade
from .tasks import all_tasks, get, select

BENCH = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path(os.environ.get("KBENCH_ROOT") or Path.home() / "kelvin-bench")
KEY_ENV = "KBENCH_ANTHROPIC_API_KEY"


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def worker_env(arm: armmod.Arm) -> Dict[str, str]:
    """The worker's environment: the arm's Kelvin first on the path, then the bench; nothing of a Claude session
    (a claude -p started inside one would belong to it), and no API key but the bench's own."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE", "MCP_", "ANTHROPIC")) and k not in ("PYTHONPATH", "PYTHONHOME")}
    env["PYTHONPATH"] = os.pathsep.join([str(arm.src), str(BENCH)])
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def run_episode(arm: armmod.Arm, task: Any, seed: int, folder: Path, mode: str, args: Any) -> Dict[str, Any]:
    """One episode in its own process; its graded result."""
    folder.mkdir(parents=True, exist_ok=True)
    auth = getattr(args, "auth", "plan")
    cmd = [args.python, "-m", "kbench.worker", "--task", task.id, "--seed", str(seed), "--arm", arm.name,
           "--kind", arm.kind, "--mode", mode, "--model", args.model, "--key-env", KEY_ENV,
           "--cap-usd", str(args.cap), "--out", str(folder), "--auth", auth,
           "--provider", "anthropic" if auth == "key" else "claudecode", "--claude", args.claude or ""]
    if arm.kind == "claude-code":
        cmd += ["--effort", args.cc_effort or "", "--tools", args.cc_tools]
    log = open(folder / "worker.log", "w", encoding="utf-8")
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=str(BENCH), env=worker_env(arm), stdout=log, stderr=subprocess.STDOUT,
                              timeout=task.max_minutes * 60 + 300)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        code = "timeout"
    finally:
        log.close()
    result = grade(folder, task)
    result.update(arm=arm.name, commit=arm.commit, kind=arm.kind, seed=seed, mode=mode, exit=code,
                  elapsed=round(time.time() - t0, 1))
    return result


class Budget:
    """Spending across the run's episodes: an episode starts only while what was spent, plus what every episode
    running (this one too) may still spend, stays within the budget. strict (dollars on an API key): each running
    episode counts at its cap, so the budget is never passed. Otherwise (a plan's limits): at the average cost of the
    episodes finished so far (half the cap until one has), so the budget is a ceiling it may pass by a little."""

    def __init__(self, usd: float, cap: float, strict: bool = True):
        self.usd, self.cap, self.strict = float(usd), float(cap), strict
        self.spent, self.running = 0.0, 0
        self.costs: List[float] = []
        self.lock = threading.Lock()

    def each(self) -> float:
        if self.strict:
            return self.cap
        return min(self.cap, sum(self.costs) / len(self.costs)) if self.costs else self.cap / 2

    def take(self) -> bool:
        with self.lock:
            if self.usd and self.spent + (self.running + 1) * self.each() > self.usd:
                return False
            self.running += 1
            return True

    def wait_take(self, stopped: Any) -> bool:
        """Take room for an episode, waiting while running ones may still free it; False once it can't fit even with
        nothing running (the budget is spent), or when stopped() says so."""
        while not stopped():
            if self.take():
                return True
            with self.lock:
                if self.running == 0:
                    return False
            time.sleep(2.0)
        return False

    def give(self, spent: float) -> None:
        with self.lock:
            self.running -= 1
            self.spent += float(spent or 0.0)
            self.costs.append(float(spent or 0.0))


def cmd_list(args: Any) -> int:
    tasks = select(args.tasks)
    for t in tasks:
        print("%-28s %-10s %s%s" % (t.id, t.category, t.title, "  [%s]" % ",".join(t.tags) if t.tags else ""))
    print("\n%d tasks. Arms:" % len(tasks))
    for name, d in armmod.DEFAULT_ARMS.items():
        print("  %-12s %-12s %-14s %s" % (name, d["kind"], d["ref"], d["about"]))
    return 0


def cmd_check(args: Any) -> int:
    """Oracle, null and bad runs of every task, with the arms' code: what passes must, what fails must."""
    root = Path(args.root)
    arms = [armmod.snapshot(a, root) for a in armmod.parse(args.arms)]
    tasks = select(args.tasks)
    label = "check-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    out = root / "runs" / label
    jobs = []
    for arm in arms:
        if arm.kind != "kelvin":
            continue
        for task in tasks:
            modes = ["oracle"] if task.oracle is not None else []
            modes += ["null"] if not args.oracle_only else []
            modes += ["bad"] if getattr(task, "bad_oracle", None) is not None and not args.oracle_only else []
            for mode in modes:
                jobs.append((arm, task, mode))
    print("checking %d tasks on %s: %d episodes, no model" % (len(tasks), ", ".join(a.describe() for a in arms),
                                                                len(jobs)))
    problems: List[str] = []
    results: List[Dict[str, Any]] = []

    def one(job: Any) -> Dict[str, Any]:
        arm, task, mode = job
        return run_episode(arm, task, args.seed, out / arm.name / task.id / mode, mode, args)

    with cf.ThreadPoolExecutor(args.jobs) as pool:
        for r in pool.map(one, jobs):
            results.append(r)
            task = get(r["task"])
            expect = {"oracle": True, "bad": False, "null": None}[r["mode"]]
            failed = [c for c in r["checks"] if c["ok"] is False and c["required"]]
            line = "%-8s %-26s %-6s %s %s" % (r["arm"], r["task"], r["mode"], "PASS" if r["pass"] else "fail",
                                              "" if r["pass"] else "| " + "; ".join(
                                                  "%s: %s" % (c["name"], c["detail"][:90]) for c in failed[:2]))
            print(line + ("" if r["exit"] == 0 else "  [worker exit %s: %s]" % (r["exit"], (r["errors"] or [""])[0][-300:])))
            if expect is True and not r["pass"]:
                problems.append("%s %s: the oracle fails" % (r["arm"], r["task"]))
            if expect is False and r["pass"]:
                problems.append("%s %s: the wrong solution passes" % (r["arm"], r["task"]))
            if r["mode"] == "null" and r["outcome"] and task.category != "safety":
                problems.append("%s %s: doing nothing passes" % (r["arm"], r["task"]))
    (out / "results.jsonl").write_text("".join(json.dumps(r, default=str) + "\n" for r in results), encoding="utf-8")
    print("\n%s\nepisodes in %s" % ("\n".join("PROBLEM " + p for p in problems) or "every task checks out", out))
    return 1 if problems else 0


def load_key(args: Any) -> bool:
    """The API key the benchmark spends: $KBENCH_ANTHROPIC_API_KEY, else the first line of --key-file. Only the
    workers get it (as the Anthropic provider's key, or Claude Code's ANTHROPIC_API_KEY); it is never printed."""
    if os.environ.get(KEY_ENV):
        return True
    path = Path(args.key_file).expanduser()
    if path.is_file():
        key = path.read_text(encoding="utf-8-sig").strip().splitlines()[0].strip() if path.stat().st_size else ""
        if key:
            os.environ[KEY_ENV] = key
            return True
    return False


def cmd_run(args: Any) -> int:
    root = Path(args.root)
    if args.auth == "key" and not load_key(args):
        print("No API key: set %s, or put the key on the first line of %s (--key-file). It goes only to the "
              "benchmark's workers." % (KEY_ENV, args.key_file))
        return 2
    arms = [armmod.snapshot(a, root) for a in armmod.parse(args.arms)]
    tasks = select(args.tasks)
    label = args.label or datetime.now().strftime("%Y%m%d-%H%M%S")
    out = root / "runs" / label
    out.mkdir(parents=True, exist_ok=True)
    seeds = [args.seed + r for r in range(args.repeats)]
    plan = [(arm, task, seed) for seed in seeds for task in tasks for arm in arms]   # every arm sees each task in turn
    done = set()
    results_file = out / "results.jsonl"
    if results_file.exists():   # a run continued: what was graded stays
        for line in results_file.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            done.add((r["arm"], r["task"], r["seed"]))
    config = {"label": label, "model": args.model, "arms": [vars(a) | {"src": str(a.src)} for a in arms],
              "tasks": [t.id for t in tasks], "seeds": seeds, "budget_usd": args.budget, "cap_usd": args.cap,
              "jobs": args.jobs, "started": datetime.now().isoformat(timespec="seconds"),
              "cc_effort": args.cc_effort, "cc_tools": args.cc_tools, "auth": args.auth}
    (out / "run.json").write_text(json.dumps(config, indent=1, default=str), encoding="utf-8")
    todo = [p for p in plan if (p[0].name, p[1].id, p[2]) not in done]
    stop = out / "STOP"
    stop.unlink(missing_ok=True)
    print("run %s: %d episodes (%d arms x %d tasks x %d seeds), %d to go; model %s on %s; budget $%.2f, cap $%.2f "
          "each%s; create %s to stop starting episodes" % (
              label, len(plan), len(arms), len(tasks), len(seeds), len(todo), args.model,
              "the API key" if args.auth == "key" else "the Claude plan", args.budget, args.cap,
              "" if args.auth == "key" else " (API-equivalent dollars: each run's own usage at list prices)", stop))
    budget = Budget(args.budget, args.cap, strict=args.auth == "key")
    lock = threading.Lock()
    skipped = []

    def one(job: Any) -> Optional[Dict[str, Any]]:
        arm, task, seed = job
        if not budget.wait_take(stop.exists):
            skipped.append(job)
            return None
        r: Optional[Dict[str, Any]] = None
        try:
            r = run_episode(arm, task, seed, out / arm.name / task.id / ("s%d" % seed), "real", args)
            return r
        finally:
            budget.give((r or {}).get("metrics", {}).get("usd", 0.0) if r else 0.0)
            if r is not None:
                with lock:
                    with open(results_file, "a", encoding="utf-8") as f:
                        f.write(json.dumps(r, default=str) + "\n")
                    m = r["metrics"]
                    print("%-12s %-26s s%-3d %s score %.2f  $%.3f  %d steps  %s" % (
                        r["arm"], r["task"], seed, "PASS" if r["pass"] else "fail", r["score"], m["usd"] or 0,
                        m["steps"], "" if r["exit"] == 0 else "worker exit %s" % r["exit"]), flush=True)

    with cf.ThreadPoolExecutor(args.jobs) as pool:
        list(pool.map(one, todo))
    print("spent $%.2f of $%.2f%s" % (budget.spent, args.budget, "; %d episodes not run (budget or STOP): run the "
                                      "same command again to finish them" % len(skipped) if skipped else ""))
    from .report import write_report
    path = write_report(out, Path(args.reports))
    print("report: %s" % path)
    return 0


def cmd_grade(args: Any) -> int:
    out = Path(args.root) / "runs" / args.label
    lines = (out / "results.jsonl").read_text(encoding="utf-8").splitlines()
    fresh = []
    for line in lines:
        r = json.loads(line)
        g = grade(r["folder"], get(r["task"]))
        g.update({k: r[k] for k in ("arm", "commit", "kind", "seed", "mode", "exit", "elapsed") if k in r})
        fresh.append(g)
    (out / "results.jsonl").write_text("".join(json.dumps(r, default=str) + "\n" for r in fresh), encoding="utf-8")
    print("graded %d episodes again" % len(fresh))
    return 0


def cmd_report(args: Any) -> int:
    from .report import write_report
    print(write_report(Path(args.root) / "runs" / args.label, Path(args.reports)))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    _utf8()
    ap = argparse.ArgumentParser(prog="python bench/run.py", description="Kelvin lab benchmark")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="where arms' code and episodes go (default ~/kelvin-bench; "
                                                               "not under a .claude folder: Kelvin won't read files there)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--tasks", default="all", help="ids, categories or tags, comma-separated (pilot: the pilot set)")
    common.add_argument("--jobs", type=int, default=4, help="episodes at once")
    common.add_argument("--seed", type=int, default=1, help="first seed (repeat r uses seed + r)")
    common.add_argument("--python", default=sys.executable, help="the Python that runs Kelvin in the workers")
    common.add_argument("--model", default="claude-opus-5-5")
    common.add_argument("--auth", choices=["plan", "key"], default="plan",
                        help="plan: the person's signed-in Claude Code (Kelvin through its Claude Code bridge, the "
                             "reference arm on the same login); key: an API key (Kelvin's Anthropic provider, "
                             "claude --bare)")
    common.add_argument("--cap", type=float, default=3.0, help="dollars at most per episode")
    common.add_argument("--claude", default="", help="the claude CLI (default: found on PATH or ~/.local/bin)")
    common.add_argument("--cc-effort", default="", help="Claude Code's --effort (default: its own)")
    common.add_argument("--cc-tools", default="Read,Glob,Grep",
                        help="Claude Code's own tools beside Kelvin's MCP tools (read-only by default: no shell, "
                             "no writing files; it runs in dontAsk mode with only these and Kelvin's tools allowed)")
    common.add_argument("--reports", default=str(BENCH / "reports"))
    p = sub.add_parser("list", parents=[common])
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("check", parents=[common])
    p.add_argument("--arms", default="main")
    p.add_argument("--oracle-only", action="store_true")
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser("run", parents=[common])
    p.add_argument("--arms", default="main,harness,claude-code")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--budget", type=float, required=True, help="dollars at most for the whole run")
    p.add_argument("--label", default="")
    p.add_argument("--key-file", default=str(DEFAULT_ROOT / "anthropic-key.txt"))
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("grade", parents=[common])
    p.add_argument("label")
    p.set_defaults(fn=cmd_grade)
    p = sub.add_parser("report", parents=[common])
    p.add_argument("label")
    p.set_defaults(fn=cmd_report)
    args = ap.parse_args(argv)
    return int(args.fn(args) or 0)
