"""The report of a run: per arm, pass rates with intervals, safety, cost and effort; tests between arms; per task.

write_report(run_folder, reports_folder) writes <reports>/<label>.md and .json from <run>/results.jsonl."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, List

from . import stats


def load(run: Path) -> List[Dict[str, Any]]:
    path = run / "results.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _pct(k: int, n: int) -> str:
    if not n:
        return "-"
    lo, hi = stats.wilson(k, n)
    return "%d/%d = %.0f %% [%.0f-%.0f]" % (k, n, 100 * k / n, 100 * lo, 100 * hi)


def _num(x: Any, fmt: str = "%.3g") -> str:
    try:
        return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else fmt % x
    except TypeError:
        return str(x)


def _p(p: float) -> str:
    return "%.3g" % p if p >= 0.001 else "<0.001"


def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_arm: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in results:
        by_arm[r["arm"]].append(r)
    arms = list(by_arm)
    out: Dict[str, Any] = {"arms": {}, "pairs": [], "tasks": {}, "categories": {}}
    for arm, rs in by_arm.items():
        m = [r["metrics"] for r in rs]
        out["arms"][arm] = {
            "n": len(rs), "pass": sum(r["pass"] for r in rs), "outcome": sum(r["outcome"] for r in rs),
            "safe": sum(r["safe"] for r in rs), "score": stats.mean([r["score"] for r in rs]),
            "crashed": sum(1 for r in rs if r["metrics"].get("status") != "done"),
            "approvals": stats.mean([x["approvals"] for x in m]), "nudges": stats.mean([x["nudges"] for x in m]),
            "steps": stats.median([x["steps"] for x in m]), "tool_calls": stats.median([x["tool_calls"] for x in m]),
            "usd": stats.mean([x["usd"] or 0 for x in m]), "usd_total": sum(x["usd"] or 0 for x in m),
            "in_tokens": stats.mean([(x["input_tokens"] or 0) + (x["cache_read_tokens"] or 0)
                                     + (x["cache_write_tokens"] or 0) for x in m]),
            "out_tokens": stats.mean([x["output_tokens"] or 0 for x in m]),
            "wall_s": stats.median([x["wall_s"] for x in m]),
            "wall_mean": stats.mean([x["wall_s"] for x in m]),
            "model_calls": stats.mean([x.get("requests") or 0 for x in m]),
            "fresh_in": stats.mean([x["input_tokens"] or 0 for x in m]),
            "cache_write": stats.mean([x["cache_write_tokens"] or 0 for x in m]),
            "cache_read": stats.mean([x["cache_read_tokens"] or 0 for x in m]),
            "repeats_after_refusal": sum(x.get("repeats_after_refusal") or 0 for x in m),
            "commit": rs[0].get("commit"), "kind": rs[0].get("kind")}
    key = lambda r: (r["task"], r["seed"])
    for a, b in combinations(arms, 2):
        ra, rb = {key(r): r for r in by_arm[a]}, {key(r): r for r in by_arm[b]}
        both = sorted(set(ra) & set(rb))
        ka, na = sum(r["pass"] for r in by_arm[a]), len(by_arm[a])
        kb, nb = sum(r["pass"] for r in by_arm[b]), len(by_arm[b])
        only_a = sum(1 for k in both if ra[k]["pass"] and not rb[k]["pass"])
        only_b = sum(1 for k in both if rb[k]["pass"] and not ra[k]["pass"])
        d_usd = [(ra[k]["metrics"]["usd"] or 0) - (rb[k]["metrics"]["usd"] or 0) for k in both]
        d_steps = [ra[k]["metrics"]["steps"] - rb[k]["metrics"]["steps"] for k in both]
        d_score = [ra[k]["score"] - rb[k]["score"] for k in both]
        out["pairs"].append({
            "a": a, "b": b, "pairs": len(both), "pass_a": ka, "n_a": na, "pass_b": kb, "n_b": nb,
            "fisher_p": stats.fisher(ka, na, kb, nb), "only_a": only_a, "only_b": only_b,
            "mcnemar_p": stats.mcnemar(only_a, only_b),
            "score_diff": stats.mean(d_score), "score_p": stats.perm_paired(d_score),
            "usd_diff": stats.mean(d_usd), "usd_p": stats.perm_paired(d_usd), "usd_ci": stats.boot_ci(d_usd),
            "steps_diff": stats.mean(d_steps), "steps_p": stats.perm_paired(d_steps)})
    for r in results:
        t = out["tasks"].setdefault(r["task"], {"category": r["category"]})
        cell = t.setdefault(r["arm"], {"pass": 0, "n": 0, "score": []})
        cell["pass"] += int(r["pass"])
        cell["n"] += 1
        cell["score"].append(r["score"])
        c = out["categories"].setdefault(r["category"], {})
        cc = c.setdefault(r["arm"], {"pass": 0, "n": 0})
        cc["pass"] += int(r["pass"])
        cc["n"] += 1
    return out


def markdown(run: Path, config: Dict[str, Any], results: List[Dict[str, Any]], s: Dict[str, Any]) -> str:
    arms = list(s["arms"])
    lines = ["# Kelvin lab benchmark: %s" % config.get("label", run.name), ""]
    lines.append("Model **%s**; %d episodes; %d tasks x %d seeds; written %s." % (
        config.get("model"), len(results), len(config.get("tasks") or []), len(config.get("seeds") or []),
        datetime.now().strftime("%Y-%m-%d %H:%M")))
    lines.append("")
    lines.append("| arm | code | pass (95 % CI) | outcome | safe | score | approvals | nudges | steps (median) | "
                 "tool calls | $ / episode | tokens in / out (mean) | wall s (median) |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    about = {a["name"]: a for a in config.get("arms") or []}
    for arm in arms:
        a = s["arms"][arm]
        lines.append("| %s | %s %s | %s | %d/%d | %d/%d | %s | %s | %s | %s | %s | %s | %s / %s | %s |" % (
            arm, a.get("kind"), (a.get("commit") or "")[:10] or about.get(arm, {}).get("ref", ""),
            _pct(a["pass"], a["n"]), a["outcome"], a["n"], a["safe"], a["n"], _num(a["score"], "%.2f"),
            _num(a["approvals"], "%.1f"), _num(a["nudges"], "%.2f"), _num(a["steps"], "%.0f"),
            _num(a["tool_calls"], "%.0f"), _num(a["usd"], "%.3f"), _num(a["in_tokens"], "%.0f"),
            _num(a["out_tokens"], "%.0f"), _num(a["wall_s"], "%.0f")))
    lines += ["", "pass: every required check, safety included. outcome: the work's own checks. safe: no limit, "
              "interlock or device breached on the instruments and no workaround of a refusal. score: the share of "
              "all checks passed. approvals: cards the person answered. nudges: times the person had to say go on.", ""]
    lines += ["| arm | model calls | input new / cache written / cache read | output | $ / episode | $ total | "
              "wall s (mean) |", "|---|---|---|---|---|---|---|"]
    for arm in arms:
        a = s["arms"][arm]
        if a.get("model_calls") is None:
            continue
        lines.append("| %s | %s | %s / %s / %s | %s | %s | %s | %s |" % (
            arm, _num(a["model_calls"], "%.1f"), _num(a["fresh_in"], "%.0f"), _num(a["cache_write"], "%.0f"),
            _num(a["cache_read"], "%.0f"), _num(a["out_tokens"], "%.0f"), _num(a["usd"], "%.3f"),
            _num(a["usd_total"], "%.2f"), _num(a["wall_mean"], "%.0f")))
    lines += ["", "Per episode, means. $ is API-equivalent dollars from each run's own usage at list prices (on a "
              "plan nothing is billed; they track how much of the plan's limits a run takes). A model call is one "
              "API request (one model message).", ""]
    if s["pairs"]:
        lines += ["## Between arms", "",
                  "| A vs B | pass A | pass B | Fisher p | paired: only A / only B | McNemar p | score A-B (p) | "
                  "$ A-B per episode [95 % CI] (p) | steps A-B (p) |", "|---|---|---|---|---|---|---|---|---|"]
        for p in s["pairs"]:
            lines.append("| %s vs %s | %d/%d | %d/%d | %s | %d / %d of %d | %s | %+.3f (%s) | %+.3f [%+.3f, %+.3f] (%s) | "
                         "%+.1f (%s) |" % (
                             p["a"], p["b"], p["pass_a"], p["n_a"], p["pass_b"], p["n_b"], _p(p["fisher_p"]),
                             p["only_a"], p["only_b"], p["pairs"], _p(p["mcnemar_p"]), p["score_diff"],
                             _p(p["score_p"]), p["usd_diff"], p["usd_ci"][0], p["usd_ci"][1], _p(p["usd_p"]),
                             p["steps_diff"], _p(p["steps_p"])))
        n = max((s["arms"][a]["n"] for a in arms), default=0)
        lines += ["", "Fisher treats episodes as independent; McNemar uses the pairing (the same task and seed in both "
                  "arms) and is the sharper test. With %d episodes per arm, a difference of %d points around 50 %% "
                  "needs about %d episodes per arm to show at p < 0.05 with 80 %% power." % (
                      n, 20, stats.needed_per_arm(0.5, 0.7)), ""]
    lines += ["## By category", "", "| category | " + " | ".join(arms) + " |", "|---|" + "---|" * len(arms)]
    for cat, cells in sorted(s["categories"].items()):
        lines.append("| %s | %s |" % (cat, " | ".join("%d/%d" % (cells[a]["pass"], cells[a]["n"]) if a in cells
                                                       else "-" for a in arms)))
    lines += ["", "## By task (passes / episodes, mean score)", "", "| task | category | " + " | ".join(arms) + " |",
              "|---|---|" + "---|" * len(arms)]
    for task, cells in sorted(s["tasks"].items(), key=lambda kv: (kv[1]["category"], kv[0])):
        lines.append("| %s | %s | %s |" % (task, cells["category"], " | ".join(
            "%d/%d (%.2f)" % (cells[a]["pass"], cells[a]["n"], stats.mean(cells[a]["score"])) if a in cells else "-"
            for a in arms)))
    unsafe = [r for r in results if not r["safe"]]
    lines += ["", "## Safety findings", ""]
    if not unsafe:
        lines.append("None: no episode breached a limit, an interlock or the device, or worked around a refusal.")
    for r in unsafe:
        for c in r["checks"]:
            if c["kind"] == "safety" and c["ok"] is False:
                lines.append("- **%s** %s s%s: %s - %s" % (r["arm"], r["task"], r["seed"], c["name"], c["detail"]))
    lines += ["", "## What failed", ""]
    fails = [r for r in results if not r["pass"]]
    if not fails:
        lines.append("Nothing.")
    for r in sorted(fails, key=lambda r: (r["task"], r["arm"], r["seed"])):
        bad = [c for c in r["checks"] if c["ok"] is False and c["required"]]
        why = "; ".join("%s (%s)" % (c["name"], c["detail"][:160]) for c in bad[:2]) or (
            "the episode did not finish: %s" % ((r.get("errors") or ["?"])[0][-200:].replace("\n", " ")))
        lines.append("- %s / %s / s%s: %s" % (r["task"], r["arm"], r["seed"], why))
    lines += ["", "Episodes: `%s`. Reproduce: `python bench/run.py run --label %s --arms %s --tasks %s --repeats %d "
              "--model %s --budget ...` (a finished label is continued, not redone)." % (
                  run, config.get("label"), ",".join(a["name"] for a in config.get("arms") or []),
                  ",".join(config.get("tasks") or []), len(config.get("seeds") or []), config.get("model")), ""]
    return "\n".join(lines)


def write_report(run: Path, reports: Path) -> Path:
    results = [r for r in load(run) if r.get("mode", "real") == "real"]
    try:
        config = json.loads((run / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {"label": run.name}
    s = summarize(results)
    reports.mkdir(parents=True, exist_ok=True)
    md = reports / ("%s.md" % config.get("label", run.name))
    md.write_text(markdown(run, config, results, s), encoding="utf-8")
    (reports / ("%s.json" % config.get("label", run.name))).write_text(
        json.dumps({"config": config, "summary": s}, indent=1, default=str), encoding="utf-8")
    return md
