"""Where a run came from, and who did what when: read from what Kelvin keeps, so its AI states them instead of
guessing.

F2 l10: Kelvin said run conditions weren't stored (run.h5 keeps them) and wrote "50 nA rms" for a 6221 set to 50 nA
peak. F3 (P8, 8 personas): it invented gaps and actors in a complete record, and mixed up wall-clock and lab time.

run_provenance: a run's plan as it ran, its conditions at the start and the end, its measurement chain, every
approval it needed with who gave it, and the stops, trips and alarms while it ran. activity: the lab record over a
stretch of wall-clock time, each line with who did it."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import chain as chains
from . import rundata

PLAN_LINES = 60   # a longer plan is cut here: the whole is in run.h5 (/plan)

# what the activity tool's kinds stand for
GROUPS = {"approvals": {"ask", "answer"},
          "stops": {"stop", "call_cancelled", "hands_on", "hands_off", "incident", "idle_off"},
          "runs": {"run_start", "run_end", "run_refused", "run_declined", "preflight"},
          "settings": {"command"},
          "alarms": {"alarm", "alarm_cleared", "trip", "watch"},
          "lab": {"lab_changed", "lab_connected", "instrument_switched", "mount", "unmount", "protection", "card",
                  "data_folder", "approval_mode", "chain", "report"},
          "people": {"signed_in", "signed_out"},
          "computer": {"computer_task", "computer", "computer_task_end"}}


def _when(iso: Any) -> str:
    try:
        return datetime.fromisoformat(str(iso)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(iso or "?")


def value_in_run(key: str, plan: Dict[str, Any], start: Dict[str, Any]) -> Any:
    """A setting's value while the run measured: the plan's setup if it set it, else as the run found it."""
    setup = plan.get("setup") if isinstance(plan.get("setup"), dict) else {}
    if key in setup:
        return setup[key]
    inst, _, param = key.partition(".")
    v = (start.get(inst) or {}).get(param) if isinstance(start.get(inst), dict) else None
    return None if isinstance(v, str) else v


def run_provenance(run_dir: Path, lab: Any, events: Sequence[Dict[str, Any]],
                   files: Iterable[str] = ()) -> str:
    """The run's provenance as text (see the module's docstring)."""
    import yaml
    from .agent.runtime import event_line
    from .reports import Collector
    rec = rundata.load(run_dir, with_rows=False)   # its record without its points: quick for any size of run
    meta = rec.meta
    try:
        plan = yaml.safe_load(rec.plan_text) or {}
    except yaml.YAMLError:
        plan = {}
    plan = plan if isinstance(plan, dict) else {}
    run_id = run_dir.name
    c = Collector(run_dir.parent, lab, lambda r: None, lambda: [], event_line)   # the reports' reading of conditions
    start, end = rec.snapshots.get("start") or {}, rec.snapshots.get("end") or {}
    status = meta.get("status") or "unfinished"
    out = ["run %s: %s, %s%s" % (run_id, meta.get("plan") or plan.get("plan") or "?", status,
                                 " (%s)" % meta["reason"] if meta.get("reason") and status != "done" else "")]
    out.append("when (this computer's clock): %s to %s%s" % (
        _when(meta.get("started")), _when(meta.get("finished")) if meta.get("finished") else "still running",
        ", %s s measured" % meta["duration_s"] if meta.get("duration_s") is not None else ""))
    out.append("points: %s of %s" % (meta.get("points", "?"), meta.get("planned_points", "?")))
    who = [("started by", meta.get("operator")), ("signed-in person", meta.get("user")),
           ("project", meta.get("project_name")), ("device", meta.get("device_name"))]
    out.append("; ".join("%s %s" % (k, v) for k, v in who if v))
    q = meta.get("queue")
    if isinstance(q, dict):
        out.append("queue item %s%s" % (q.get("position", "?"), ": " + q["note"] if q.get("note") else ""))
    out.append("lab: %s (%s instruments); Kelvin %s" % (meta.get("lab") or "?", "simulated" if meta.get(
        "simulated", True) else "real", meta.get("labharness") or "?"))
    if meta.get("held"):
        out.append("held while it measured: " + "; ".join("%s %s" % kv for kv in meta["held"].items()))

    kept = meta.get("chain") if isinstance(meta.get("chain"), dict) else None
    out.append("")
    if kept:
        current = chains.excitation_current(kept, lambda k: value_in_run(k, plan, start))
        out.append("measurement chain (as stamped into the run, the %s's):" % (meta.get("chain_of") or "device"))
        out += ["  " + line for line in chains.lines(kept, current)]
        for m in (rec.columns.get("measures") or []):
            d = chains.derived(kept, m["key"], m.get("unit") or "", current)
            if d:
                out.append("  derived: %s (multiply %s by %s)" % (d[3], m["key"], "%.6g" % d[0]))
    else:
        out.append("measurement chain: none recorded for this run (measurement_chain records one for the device)")

    out.append("")
    out.append("conditions at the start (run.h5 /conditions/start has every setting):")
    for name, values in c._found(start, plan):
        out.append("  %s (settings the plan left as they were): %s" % (name, values))
    setup = plan.get("setup") if isinstance(plan.get("setup"), dict) else {}
    if setup:
        out.append("  set by the plan: " + "; ".join("%s %s" % (k, c._show(k, v)) for k, v in setup.items()))
    sweep = c._sweep_lines(plan)
    if sweep:
        out.append("  swept: " + "; ".join(sweep))
    away = c._away(start)
    if away:
        out.append("  drives away from safe at the start: " + "; ".join(away))
    out.append("conditions at the end (run.h5 /conditions/end):")
    if end:
        changes = c._changes(start, end)
        out.append("  left changed: " + ("; ".join(changes) if changes else "nothing away from safe"))
    else:
        out.append("  not kept (the run did not end)")
    unread = c._unread_at_end(end)
    if unread:
        out.append("  not read at the end: " + "; ".join(unread))
    for line in meta.get("cleanup") or []:
        out.append("  after the stop: " + str(line))
    for w in meta.get("warnings") or []:
        out.append("  warning: " + str(w))

    out.append("")
    out.append("approvals, stops and alarms:")
    lines = _run_events(meta, plan, events)
    out += ["  " + line for line in lines] or ["  none recorded (a plan that only measures, or inside an approved "
                                              "range, asks nobody)"]

    out.append("")
    text = (rec.plan_text or "").rstrip()
    rows = text.splitlines()
    out.append("plan as it ran%s:" % (" (first %d lines; all of it in run.h5 /plan)" % PLAN_LINES
                                      if len(rows) > PLAN_LINES else ""))
    out += ["  " + r for r in rows[:PLAN_LINES]] or ["  (none kept)"]
    names = sorted(set(files))
    out.append("")
    out.append("files: %s, in %s" % (", ".join(names) or "none yet", run_dir))
    return "\n".join(out)


def _run_events(meta: Dict[str, Any], plan: Dict[str, Any], events: Sequence[Dict[str, Any]]) -> List[str]:
    """The run's own approval (just before it started), its queue's (when it was a queue item), and every ask,
    answer, stop, trip and alarm while it ran, each with who."""
    from .agent.runtime import event_line
    t0, t1 = str(meta.get("started") or ""), str(meta.get("finished") or "9999")
    if not t0:
        return []
    name = str(meta.get("plan") or plan.get("plan") or "")
    try:
        before = (datetime.fromisoformat(t0) - timedelta(minutes=15)).isoformat()
    except ValueError:
        before = t0
    out: List[Tuple[str, str]] = []
    own = [e for e in events if e.get("type") == "ask" and before <= str(e.get("wall", "")) <= t0
           and str(e.get("request", "")).startswith("Run plan %s:" % name)]
    if own:
        out.append((own[-1]["wall"], "its approval: " + _ask(own[-1])))
    if isinstance(meta.get("queue"), dict):
        queue = [e for e in events if e.get("type") == "ask" and str(e.get("wall", "")) <= t0
                 and str(e.get("request", "")).startswith("Run the queue")]
        if queue:
            out.append((queue[-1]["wall"], "the queue's approval, which covered it: " + _ask(queue[-1])))
    for e in events:
        wall = str(e.get("wall", ""))
        if not (t0 <= wall <= t1) or e.get("type") not in ("ask", "answer", "stop", "trip", "alarm", "alarm_cleared",
                                                           "call_cancelled", "hands_on"):
            continue
        out.append((wall, _ask(e) if e.get("type") == "ask" else _who(e) + event_line(e)[9:]))
    return ["%s %s" % (_when(w)[11:], text) for w, text in sorted(out, key=lambda x: x[0])]


def _ask(e: Dict[str, Any]) -> str:
    first = str(e.get("request", "")).splitlines()[0] if e.get("request") else "?"
    note = str(e.get("note") or "")
    said = note if note.startswith(str(e.get("answer"))) else "%s%s" % (e.get("answer"), " (%s)" % note if note
                                                                       else "")
    return "%s: %s; asked by %s" % (first, said, e.get("by") or "?")


def _who(e: Dict[str, Any]) -> str:
    """Who an event is from, when the record says more than its by: the signed-in person, and where they answered
    from (an approval's channel and device; a mount's device is the sample, not who)."""
    where = (e.get("channel"), e.get("device")) if e.get("channel") else ()
    bits = [str(x) for x in (e.get("account") or e.get("user"),) + tuple(where) if x]
    return "[%s] " % ", ".join(bits) if bits else ""


# ------------------------------------------------------------------ the lab record over a stretch of time

_REL = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|sec|m|min|h|hr|hours?|d|days?)\s*(ago)?\s*$", re.I)


def when(text: str, now: datetime, end: bool = False) -> Optional[datetime]:
    """A time a person or the model gives: 2026-10-28, 2026-10-28T11:00, 11:00 (today), today, yesterday, now, or
    a while ago (2h, 30m, 1d). None for an empty text. end: a day alone means its end."""
    t = str(text or "").strip().lower()
    if not t:
        return None
    if t == "now":
        return now
    if t in ("today", "yesterday"):
        day = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1 if t == "yesterday" else 0)
        return day + timedelta(days=1) - timedelta(microseconds=1) if end else day
    m = _REL.match(t)
    if m:
        n, unit = float(m.group(1)), m.group(2)[0]
        return now - timedelta(seconds=n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit])
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", t):
        h, mi, *s = (int(x) for x in t.split(":"))
        return now.replace(hour=h, minute=mi, second=s[0] if s else 0, microsecond=0)
    try:
        d = datetime.fromisoformat(t.upper().replace(" ", "T"))
    except ValueError:
        raise ValueError("a time is 2026-10-28, 2026-10-28T11:00, 11:00, today, yesterday or a while ago (2h, 30m), "
                         "not %r" % text)
    if end and re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        return d + timedelta(days=1) - timedelta(microseconds=1)
    return d


def read_events(paths: Sequence[Path]) -> List[Dict[str, Any]]:
    """Every event in these records, oldest first (one record: each line once; the lab's instrument log has
    everyone's, with who was signed in)."""
    out: List[Dict[str, Any]] = []
    for p in paths:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.strip():
                        try:
                            out.append(json.loads(line))
                        except ValueError:
                            pass
        except OSError:
            continue
    out.sort(key=lambda e: str(e.get("wall", "")))
    return out


def activity(events: Sequence[Dict[str, Any]], since: str = "", until: str = "", who: str = "", kinds: str = "",
             limit: int = 60, now: Optional[datetime] = None) -> str:
    """The lab record from since to until (wall-clock time; default the last 24 hours), each line with who: the
    signed-in person, where they answered, or Kelvin's AI, a queue or a plan. who: only events whose actor matches
    (a name, agent, human, phone...); kinds: groups (approvals, stops, runs, settings, alarms, lab, people,
    computer) or event types, comma-separated."""
    from .agent.runtime import event_line
    now = now or datetime.now()
    t0 = when(since, now) or now - timedelta(hours=24)
    t1 = when(until, now, end=True) or now
    if t1 < t0:
        raise ValueError("until (%s) is before since (%s)" % (t1.isoformat(" ", "seconds"), t0.isoformat(" ", "seconds")))
    wanted: set = set()
    for k in [k.strip().lower() for k in str(kinds or "").split(",") if k.strip()]:
        wanted |= GROUPS.get(k, {k})
    needle = str(who or "").strip().lower()
    a, b = t0.isoformat(timespec="milliseconds"), t1.isoformat(timespec="milliseconds")
    picked = []
    for e in events:
        wall = str(e.get("wall", ""))
        if not (a <= wall <= b) or (wanted and e.get("type") not in wanted):
            continue
        actor = " ".join(str(e.get(k) or "") for k in ("by", "account", "user", "channel", "device")).lower()
        if needle and needle not in actor:
            continue
        picked.append(e)
    head = "the lab record from %s to %s (this computer's clock, not the simulated lab's time)%s%s: %d event%s" % (
        t0.strftime("%Y-%m-%d %H:%M"), t1.strftime("%Y-%m-%d %H:%M"), ", by %s" % who if needle else "",
        ", %s" % kinds if wanted else "", len(picked), "" if len(picked) == 1 else "s")
    limit = max(1, min(int(limit or 60), 400))
    shown = picked[-limit:]
    lines = [head]
    if len(picked) > len(shown):
        lines.append("(the %d before these are left out: narrow since/until, who or kinds to see them)"
                     % (len(picked) - len(shown)))
    day = ""
    for e in shown:
        wall = str(e.get("wall", ""))
        if wall[:10] != day:
            day = wall[:10]
            lines.append(day + ":")
        text = event_line(e)
        if e.get("type") == "ask":   # what was asked (its title), the answer and who asked
            text = text[:8] + " " + _ask(e)
        elif e.get("type") == "command" and e.get("by"):   # who sent it: Kelvin's AI, a plan, a person's terminal
            text = "%s %s: %s" % (text[:8], e["by"], text[9:])
        lines.append("  %s %s%s" % (text[:8], _who(e) if e.get("type") != "answer" else "", text[9:]))
    if not picked:
        lines.append("nothing recorded then%s" % (" that matches" if needle or wanted else ""))
    return "\n".join(lines)
