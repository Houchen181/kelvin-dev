"""Gather a report from what Kelvin already keeps: run folders (plan, data, snapshots, analysis) and the event log."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from ..cards import ParamSpec, num, si
from .chart import PALETTE, Chart, Series, Table
from .model import Report, RunPart

MAX_CHARTS = 5
MAX_GROUPS = 8
LOG_TYPES = {"ask", "run_start", "run_end", "run_refused", "run_declined", "trip", "alarm", "alarm_cleared", "stop",
             "lab_changed", "approval_mode", "preflight"}
RUN_EVENT_TYPES = {"ask", "trip", "alarm", "alarm_cleared", "stop"}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _when(started: Optional[str], finished: Optional[str]) -> str:
    try:
        a = datetime.fromisoformat(started) if started else None
        b = datetime.fromisoformat(finished) if finished else None
    except ValueError:
        return started or ""
    if a is None:
        return ""
    text = a.strftime("%b %d %Y, %H:%M").replace(" 0", " ")
    if b is not None:
        text += "–" + (b.strftime("%H:%M") if b.date() == a.date() else b.strftime("%b %d, %H:%M").replace(" 0", " "))
    return text


def _duration(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return ""
    if seconds < 90:
        return "%.0f s" % seconds
    if seconds < 5400:
        return "%.1f min" % (seconds / 60)
    return "%.1f h" % (seconds / 3600)


def _numbers(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class Collector:
    """Turns run ids into report parts. `lab` resolves units and safe values; `columns` gives a run's axes and
    measured columns (None when the run's instruments are not in this lab any more); `event_line` formats events."""

    def __init__(self, runs_dir: Path, lab: Any, columns: Callable[[str], Optional[Dict[str, List[Dict[str, str]]]]],
                 events: Callable[[], List[Dict[str, Any]]], event_line: Callable[[Dict[str, Any]], str]):
        self.runs_dir = runs_dir
        self.lab = lab
        self.columns = columns
        self.events = events
        self.event_line = event_line

    # ------------------------------------------------------------------ helpers

    def _spec(self, key: str) -> Optional[ParamSpec]:
        try:
            return self.lab.resolve(key)[1]
        except (KeyError, ValueError, AttributeError):
            return None

    def _show(self, key: str, value: Any) -> str:
        spec = self._spec(key)
        if spec is not None:
            try:
                return spec.show(value)
            except Exception:
                pass
        if isinstance(value, bool):
            return "on" if value else "off"
        return num(value) if _numbers(value) else str(value)

    def _line(self, e: Dict[str, Any]) -> Tuple[str, str]:
        if e.get("type") == "ask":   # the request's first line says what was asked
            first = str(e.get("request", "")).splitlines()[0] if e.get("request") else ""
            return str(e.get("wall", ""))[11:19], "%s %s: %s%s" % (
                e.get("by", ""), e.get("answer", "asked"), first, " (%s)" % e["note"] if e.get("note") else "")
        text = self.event_line(e)
        return text[:8], text[9:]

    # ------------------------------------------------------------------ one run

    def run(self, run_id: str, events: Sequence[Dict[str, Any]]) -> RunPart:
        d = self.runs_dir / run_id
        if not d.is_dir():
            raise KeyError("no run %r" % run_id)
        meta = _read_json(d / "meta.json") or {}
        analysis = _read_json(d / "analysis.json") or {}
        plan_text = (d / "plan.yaml").read_text(encoding="utf-8") if (d / "plan.yaml").exists() else ""
        try:
            plan = yaml.safe_load(plan_text) or {}
        except yaml.YAMLError:
            plan = {}
        rows = []
        if (d / "data.jsonl").exists():
            with open(d / "data.jsonl", encoding="utf-8") as f:
                rows = [json.loads(line) for line in f if line.strip()]
        status = meta.get("status") or "unfinished"
        part = RunPart(run_id=run_id, plan=str(plan.get("plan") or meta.get("plan") or run_id.split("-", 2)[-1]),
                       status=status, when=_when(meta.get("started"), meta.get("finished")),
                       duration=_duration(meta.get("duration_s")),
                       points="%s of %s" % (meta.get("points", len(rows)), meta.get("planned_points", len(rows))),
                       operator=str(meta.get("operator") or "?"), reason=str(meta.get("reason") or ""),
                       plan_text=plan_text)
        part.assessment = str(analysis.get("assessment") or "")
        part.next = str(analysis.get("next") or "")
        part.issues = [str(i) for i in analysis.get("issues") or []]
        part.setup = ["%s %s" % (k, self._show(k, v)) for k, v in (plan.get("setup") or {}).items()]
        part.sweep = self._sweep_lines(plan)
        part.measured = [str(m) for m in plan.get("measure") or []]
        cols = self.columns(run_id)
        part.charts = self._charts(rows, cols, plan, analysis)
        part.results = self._results(analysis)
        start = _read_json(d / "snapshot_start.json") or {}
        end = _read_json(d / "snapshot_end.json") or {}
        part.conditions = self._conditions(start)
        part.changes = self._changes(start, end)
        t0, t1 = meta.get("started"), meta.get("finished")
        if t0:
            part.events = [self._line(e) for e in events if e.get("type") in RUN_EVENT_TYPES
                           and t0 <= str(e.get("wall", "")) <= (t1 or "9999")]
            asked = [e for e in events if e.get("type") == "ask" and str(e.get("wall", "")) <= t0
                     and "Run plan %s" % part.plan in str(e.get("request", ""))]
            if asked:   # the plan's own approval comes just before it starts
                part.events.insert(0, self._line(asked[-1]))
        return part

    def _sweep_lines(self, plan: Dict[str, Any]) -> List[str]:
        out = []
        for key, spec in (plan.get("sweep") or {}).items():
            if not isinstance(spec, dict):
                out.append("%s: %s" % (key, spec))
                continue
            if "values" in spec and isinstance(spec["values"], list):
                vals = spec["values"]
                shown = ", ".join(self._show(key, v) for v in vals[:6]) + (", …" if len(vals) > 6 else "")
                out.append("%s: %s (%d values)" % (key, shown, len(vals)))
            elif "from" in spec and "to" in spec:
                step = spec.get("step") or spec.get("points")
                out.append("%s: %s to %s%s" % (key, self._show(key, spec["from"]), self._show(key, spec["to"]),
                                                (", step %s" % self._show(key, spec["step"])) if spec.get("step")
                                                else (", %s points" % step if step else "")))
            else:
                out.append("%s: %s" % (key, ", ".join("%s %s" % kv for kv in spec.items())))
        if plan.get("repeat"):
            out.append("repeated %s times" % plan["repeat"])
        return out

    def _charts(self, rows: List[Dict[str, Any]], cols: Optional[Dict[str, List[Dict[str, str]]]],
                plan: Dict[str, Any], analysis: Dict[str, Any]) -> List[Chart]:
        if not rows:
            return []
        if cols is None:   # the run's instruments are not in this lab any more: plot without units
            axes = [{"key": k, "unit": ""} for k in (plan.get("sweep") or {})]
            measures = [{"key": str(m), "unit": "", "kind": "trace" if isinstance(rows[-1].get(str(m)), list)
                         else "scalar"} for m in plan.get("measure") or []]
        else:
            axes = [a for a in cols["axes"] if a.get("kind") != "repeat"]
            measures = cols["measures"]
        fits = [s for s in analysis.get("series") or [] if s.get("fit") and s["fit"].get("x")]
        charts: List[Chart] = []
        if axes and len(rows) > 1:
            xa = axes[-1]
            outer = axes[0] if len(axes) > 1 else None
            for m in [m for m in measures if m.get("kind") != "trace"]:
                groups: Dict[Any, List[Dict[str, Any]]] = {}
                for r in rows:
                    groups.setdefault(r.get(outer["key"]) if outer else None, []).append(r)
                series: List[Series] = []
                model = ""
                for k, (gval, grows) in enumerate(list(groups.items())[:MAX_GROUPS]):
                    if outer:   # the same label the analysis gives this group
                        name = "%s = %s" % (outer["key"], si(gval, outer.get("unit", "")) if _numbers(gval) else gval)
                    else:
                        name = m["key"]
                    color = PALETTE[k % len(PALETTE)] if outer else ""
                    series.append(Series(name, [r.get(xa["key"]) for r in grows], [r.get(m["key"]) for r in grows],
                                         color=color))
                    fit = next((f for f in fits if f["y"] == m["key"]
                                and (f.get("group") == name if outer else not f.get("group"))), None)
                    if fit:
                        model = fit["fit"].get("model") or "model"
                        series.append(Series("fit", fit["fit"]["x"], fit["fit"]["y"], fit=True, color=color))
                caption = "Dashed: %s fit%s." % (model, "s" if outer and len(groups) > 1 else "") if model else ""
                charts.append(Chart(m["key"], xa["key"], xa.get("unit", ""), m.get("unit", ""), series, caption))
        for m in [m for m in measures if m.get("kind") == "trace"]:
            last = next((r for r in reversed(rows) if isinstance(r.get(m["key"]), list)), None)
            if not last:
                continue
            ys = last[m["key"]]
            x0, x1 = last.get(m["key"] + ".axis") or (0, len(ys) - 1)
            n = len(ys)
            xs = [x0 + (x1 - x0) * i / (n - 1 if n > 1 else 1) for i in range(n)]
            series = [Series(m["key"], xs, ys)]
            fit = next((f for f in fits if f["y"] == m["key"]), None)
            if fit:
                series.append(Series("fit", fit["fit"]["x"], fit["fit"]["y"], fit=True))
            x_unit = m.get("x_unit", "")
            charts.append(Chart(m["key"] + (" (last of %d)" % len(rows) if len(rows) > 1 else ""),
                                "frequency" if x_unit == "Hz" else "x", x_unit, m.get("unit", ""), series,
                                "Dashed: %s fit." % fit["fit"].get("model", "model") if fit else ""))
        return charts[:MAX_CHARTS]

    def _results(self, analysis: Dict[str, Any]) -> List[Table]:
        tables: List[Table] = []
        by_y: Dict[str, List[Dict[str, Any]]] = {}
        for s in analysis.get("series") or []:
            if s.get("results"):
                by_y.setdefault(s["y"], []).append(s)
        for y, series in by_y.items():
            if len(series) == 1 and not series[0].get("group"):
                s = series[0]
                tables.append(Table(["Quantity", "Value"], [[r["name"], str(r["value"])] for r in s["results"]],
                                    "%s vs %s (%s)" % (y, s.get("x") or "", s.get("kind", "")) if s.get("x") else y))
                continue
            names: List[str] = []
            for s in series:
                for r in s["results"]:
                    if r["name"] not in names:
                        names.append(r["name"])
            names = names[:4]
            rows = []
            for s in series[:MAX_GROUPS]:
                values = {r["name"]: str(r["value"]) for r in s["results"]}
                rows.append([s.get("group") or ""] + [values.get(n, "") for n in names])
            first = series[0]
            tables.append(Table([""] + names, rows, "%s vs %s (%s)" % (y, first.get("x") or "", first.get("kind", ""))))
        return tables

    def _conditions(self, snapshot: Dict[str, Any]) -> List[Tuple[str, str]]:
        out = []
        for inst, values in snapshot.items():
            if not isinstance(values, dict):
                continue
            parts = []
            for name, v in values.items():
                if isinstance(v, list) or v is None or (isinstance(v, str) and v.startswith("ERROR")):
                    continue
                key = "%s.%s" % (inst, name)
                spec = self._spec(key)
                away = spec is not None and spec.is_drive and spec.distance(v) > 0
                parts.append("%s %s%s" % (name, self._show(key, v), "*" if away else ""))
            out.append((inst, ", ".join(parts)))
        return out

    def _changes(self, start: Dict[str, Any], end: Dict[str, Any]) -> List[str]:
        out = []
        for inst, values in end.items():
            if not isinstance(values, dict):
                continue
            for name, v in values.items():
                key = "%s.%s" % (inst, name)
                spec = self._spec(key)
                if spec is None or not spec.is_drive:
                    continue
                before = (start.get(inst) or {}).get(name)
                if before != v and spec.distance(v) > 0:
                    out.append("%s %s → %s" % (key, self._show(key, before), self._show(key, v)))
        return out

    # ------------------------------------------------------------------ the whole report

    def report(self, run_ids: Sequence[str], title: str = "", summary: str = "", notes: Iterable[Tuple[str, str]] = (),
               lab_name: str = "", simulated: bool = False) -> Report:
        events = self.events()
        runs = [self.run(r, events) for r in run_ids]
        days = sorted({r[:8] for r in run_ids if re.match(r"\d{8}-", r)})

        def day(s: str) -> str:
            return datetime.strptime(s, "%Y%m%d").strftime("%b %d %Y").replace(" 0", " ")
        period = (day(days[0]) if len(days) == 1 else "%s to %s" % (day(days[0]), day(days[-1]))) if days else ""
        subtitle = "%d run%s%s" % (len(runs), "" if len(runs) == 1 else "s", ", " + period if period else "")
        log: List[Tuple[str, str]] = []
        chosen = set(run_ids)
        windows = []   # each run's own stretch of the log: its approval just before, through its end
        for r, part in zip(run_ids, runs):
            meta = _read_json(self.runs_dir / r / "meta.json") or {}
            try:
                t0 = datetime.fromisoformat(meta["started"])
                t1 = datetime.fromisoformat(meta["finished"]) if meta.get("finished") else datetime.max
            except (KeyError, ValueError):
                continue
            windows.append(((t0 - timedelta(minutes=2)).isoformat(), meta["started"],
                            (t1 + timedelta(minutes=1)).isoformat() if t1 != datetime.max else "9999", part.plan))
        for e in events:
            kind, wall = e.get("type"), str(e.get("wall", ""))
            if kind not in LOG_TYPES or (kind == "preflight" and e.get("ok")):
                continue
            if kind in ("run_start", "run_end"):
                keep = e.get("run") in chosen   # never another project's runs
            elif kind == "ask":
                keep = any((t0 <= wall <= end and "Run plan %s:" % plan in str(e.get("request", "")))
                           or (started <= wall <= end) for t0, started, end, plan in windows)
            else:
                keep = any(t0 <= wall <= end for t0, _, end, _ in windows)
            if keep:
                t, text = self._line(e)
                log.append((wall[5:10] + " " + t[:5] if len(days) > 1 else t, text))
        return Report(title=title or "Experiment report", subtitle=subtitle,
                      created=datetime.now().strftime("%b %d %Y, %H:%M").replace(" 0", " "),
                      lab=lab_name, simulated=simulated, summary=[p.strip() for p in summary.split("\n\n") if p.strip()],
                      runs=runs, log=log[-200:], notes=list(notes))


def pick_runs(runs_dir: Path, runs: Optional[Sequence[str]] = None, day: str = "", last: int = 0) -> List[str]:
    """Run ids, oldest first: the ones named, the ones from a day (YYYY-MM-DD, today, yesterday), or the last N."""
    every = sorted(d.name for d in runs_dir.iterdir() if d.is_dir()) if runs_dir.exists() else []
    if runs:
        missing = [r for r in runs if r not in every]
        if missing:
            raise KeyError("no run %s" % ", ".join(repr(m) for m in missing))
        return sorted(dict.fromkeys(runs))
    if day:
        word = day.strip().lower()
        if word in ("today", "yesterday"):
            d = date.today() - timedelta(days=1 if word == "yesterday" else 0)
        else:
            try:
                d = date.fromisoformat(word)
            except ValueError:
                raise ValueError("day must be YYYY-MM-DD, today or yesterday")
        return [r for r in every if r.startswith(d.strftime("%Y%m%d"))]
    if last:
        return every[-int(last):]
    return []
