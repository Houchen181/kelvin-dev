"""Gather a report from what Kelvin already keeps: run folders (plan, data, snapshots, analysis) and the event log."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from ..cards import ParamSpec, num, si, trace_x
from .chart import PALETTE, Chart, Series, Table
from .model import Report, RunPart

MAX_CHARTS = 5
MAX_GROUPS = 8
MAX_KEPT = 4    # kept fits a run's report draws, the newest first; the rest are listed (run.h5 has them all)
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
    text = "%s %d %d, %s" % (a.strftime("%b"), a.day, a.year, a.strftime("%H:%M"))
    if b is not None and b.strftime("%Y%m%d%H%M") != a.strftime("%Y%m%d%H%M"):   # a span, unless one minute
        text += "–" + (b.strftime("%H:%M") if b.date() == a.date()
                       else "%s %d, %s" % (b.strftime("%b"), b.day, b.strftime("%H:%M")))
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


def _stamp(by: Any, account: Any, when: Any) -> str:
    """Who kept or accepted a run's fit and when: "by agent for ada on Oct 29, 03:12" (account: the person signed in,
    when people sign in)."""
    when = str(when or "")
    try:
        at = datetime.fromisoformat(when)
        when = at.strftime("%b %d").replace(" 0", " ") + at.strftime(", %H:%M")
    except ValueError:
        pass
    return "by %s%s%s" % (by or "?", " for %s" % account if account else "", " on " + when if when else "")


def _fit_words(s: Dict[str, Any]) -> str:
    """A fit's model, and what makes it reproducible where the run kept it: who accepted it and when, its window and
    its reduced chi-squared (a fit chosen after the run is the one its report shows: F3 P5 P7 P8 P12)."""
    fit = s.get("fit") or {}
    bits = [str(fit.get("model") or "model")]
    acc = s.get("accepted")
    if isinstance(acc, dict):
        bits.append("accepted " + _stamp(acc.get("by"), acc.get("account"), acc.get("at")))
    window = fit.get("window")
    if isinstance(window, (list, tuple)) and len(window) == 2:
        bits.append("window %s to %s" % (num(window[0]), num(window[1])) if all(map(_numbers, window)) else
                    "window %s" % (window,))
    chi2 = fit.get("chi2_red", fit.get("chi2r"))
    if _numbers(chi2):
        bits.append("reduced chi-squared %s" % num(chi2))
    return ", ".join(bits[:1]) + (" (%s)" % "; ".join(bits[1:]) if len(bits) > 1 else "")


def _unread(v: Any) -> bool:
    """A setting the snapshot couldn't read: an instrument that stopped answering leaves "ERROR: ..." in its place."""
    return v is None or isinstance(v, list) or (isinstance(v, str) and v.startswith("ERROR"))


def _away_from_safe(spec: Optional[ParamSpec], v: Any) -> Optional[bool]:
    """Whether a drive's recorded value is away from its safe value; None when that can't be told: not a drive, not
    read (an error), or a reply the card doesn't know (a garbled one). A run cut short by a lost instrument has all
    of these in its end snapshot, and its report must still be written (l08, l09: every aborted run's failed)."""
    if spec is None or not spec.is_drive or _unread(v) or (spec.type == "bool" and not isinstance(v, (bool, int))):
        return None
    try:
        return spec.distance(v) > 0
    except (TypeError, ValueError):
        return None


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
        if e.get("type") == "ask":   # the request's first line says what was asked; its note, who answered and when
            first = str(e.get("request", "")).splitlines()[0] if e.get("request") else ""
            note = str(e.get("note") or "")
            said = note if note.startswith(str(e.get("answer"))) else "%s%s" % (
                e.get("answer", "asked"), " (%s)" % note if note else "")
            return str(e.get("wall", ""))[11:19], "%s: %s (asked by %s)" % (first, said, e.get("by") or "?")
        text = self.event_line(e)
        return text[:8], text[9:]

    # ------------------------------------------------------------------ one run

    def run(self, run_id: str, events: Sequence[Dict[str, Any]]) -> RunPart:
        from .. import rundata
        d = self.runs_dir / run_id
        if not d.is_dir():
            raise KeyError("no run %r" % run_id)
        rec = rundata.load(d)
        meta, analysis, plan_text, rows = rec.meta, rec.analysis or {}, rec.plan_text, rec.rows
        try:
            plan = yaml.safe_load(plan_text) or {}
        except yaml.YAMLError:
            plan = {}
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
        start = rec.snapshots.get("start") or {}
        part.charts = self._charts(rows, cols, plan, analysis, self._derived(meta, plan, start, rec.columns))
        part.results = self._results(analysis)
        kept_charts, kept_tables = self._kept(d)   # after the run's own result: the fits someone kept besides
        part.charts += kept_charts
        part.results += kept_tables
        end = rec.snapshots.get("end") or {}
        part.conditions = self._conditions(start)
        part.away = self._away(start)
        part.found = self._found(start, plan)
        part.changes = self._changes(start, end)
        part.unread = self._unread_at_end(end)
        t0, t1 = meta.get("started"), meta.get("finished")
        if t0:
            part.events = [self._line(e) for e in events if e.get("type") in RUN_EVENT_TYPES
                           and t0 <= str(e.get("wall", "")) <= (t1 or "9999")]
            asked = [e for e in events if e.get("type") == "ask" and str(e.get("wall", "")) <= t0
                     and "Run plan %s" % part.plan in str(e.get("request", ""))]
            if asked and self._line(asked[-1]) not in part.events:   # its approval, just before (or as) it starts
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

    @staticmethod
    def _derived(meta: Dict[str, Any], plan: Dict[str, Any], start: Dict[str, Any],
                 columns: Dict[str, Any]) -> Dict[str, Tuple[float, str, str, str]]:
        """The readings the run's measurement chain turns into the sample's resistance: {key: (factor, name, unit,
        how)}, from the chain stamped into the run and the excitation it measured with."""
        from ..chain import derived_readings
        return derived_readings(meta.get("chain"), plan.get("setup"), start, columns.get("measures"))

    def _charts(self, rows: List[Dict[str, Any]], cols: Optional[Dict[str, List[Dict[str, str]]]],
                plan: Dict[str, Any], analysis: Dict[str, Any],
                derive: Optional[Dict[str, Tuple[float, str, str, str]]] = None) -> List[Chart]:
        """Each measured column against the innermost axis (one line per outer value, with its fit), and each trace.
        derive: readings to plot as the sample's resistance, from the measurement chain (in ohms, not volts)."""
        derive = derive or {}
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
            for m in [m for m in measures if m.get("kind") not in ("trace", "image")   # frames are files
                      and any(_numbers(r.get(m["key"])) for r in rows)]:
                groups: Dict[Any, List[Dict[str, Any]]] = {}
                for r in rows:
                    groups.setdefault(r.get(outer["key"]) if outer else None, []).append(r)
                series: List[Series] = []
                model = ""
                factor, title, y_unit, how = derive.get(m["key"]) or (1.0, m["key"], m.get("unit", ""), "")

                def scaled(ys: List[Any]) -> List[Any]:
                    return [y * factor if _numbers(y) else y for y in ys] if factor != 1.0 else ys
                for k, (gval, grows) in enumerate(list(groups.items())[:MAX_GROUPS]):
                    if outer:   # the same label the analysis gives this group
                        name = "%s = %s" % (outer["key"], si(gval, outer.get("unit", "")) if _numbers(gval) else gval)
                    else:
                        name = title
                    color = PALETTE[k % len(PALETTE)] if outer else ""
                    series.append(Series(name, [r.get(xa["key"]) for r in grows],
                                         scaled([r.get(m["key"]) for r in grows]), color=color))
                    fit = next((f for f in fits if f["y"] == m["key"]
                                and (f.get("group") == name if outer else not f.get("group"))), None)
                    if fit:
                        model = _fit_words(fit)
                        series.append(Series("fit", fit["fit"]["x"], scaled(fit["fit"]["y"]), fit=True, color=color))
                caption = ("Dashed: %s fit%s to %s." % (model, "s" if outer and len(groups) > 1 else "", m["key"])
                           if model else "")
                if how:   # a derived quantity says how it was worked out
                    caption = ("%s From the measurement chain: %s." % (caption, how)).strip()
                charts.append(Chart(title, xa["key"], xa.get("unit", ""), y_unit, series, caption))
        for m in [m for m in measures if m.get("kind") == "trace"]:
            last = next((r for r in reversed(rows) if isinstance(r.get(m["key"]), list)), None)
            if not last:
                continue
            ys = last[m["key"]]
            xs = trace_x(last, m["key"], len(ys))
            series = [Series(m["key"], xs, ys)]
            fit = next((f for f in fits if f["y"] == m["key"]), None)
            if fit:
                series.append(Series("fit", fit["fit"]["x"], fit["fit"]["y"], fit=True))
            x_unit = m.get("x_unit", "")
            charts.append(Chart(m["key"] + (" (last of %d)" % len(rows) if len(rows) > 1 else ""),
                                {"Hz": "frequency", "nm": "wavelength", "s": "delay"}.get(x_unit, "x"), x_unit,
                                m.get("unit", ""), series,
                                "Dashed: %s fit to %s." % (_fit_words(fit), m["key"]) if fit else ""))
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
                                    ("%s vs %s (%s)" % (y, s.get("x") or "", s.get("kind", "")) if s.get("x") else y)
                                    + (", the accepted fit" if s.get("accepted") else "")))
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

    def _kept(self, folder: Path) -> Tuple[List[Chart], List[Table]]:
        """The fits kept with the run (fit_run: run.h5 /fits/<name>), the newest first: each one's points as fitted,
        with the model dashed, and its values with their errors, saying who kept it and when. The accepted fit
        (analyze_run with accept) is the run's own result, in its plots and results above; these are the fits someone
        kept besides it. More than MAX_KEPT: the others are listed."""
        try:
            from .. import fitting
            kept = list(reversed(fitting.stored(folder, with_data=True)))
        except Exception:   # no h5py, or run.h5 can't be read just now: the report goes on without them
            return [], []
        charts: List[Chart] = []
        tables: List[Table] = []
        for f in kept[:MAX_KEPT]:
            try:
                chart, table = self._kept_fit(f, fitting)
            except Exception:   # a fit that can't be drawn never loses the report
                continue
            charts.append(chart)
            tables.append(table)
        if len(kept) > MAX_KEPT:
            tables.append(Table(["Fit", "Model", "Kept"], [
                [str(f.get("name")), str(f.get("model") or "") + (" (failed)" if f.get("failed") else ""),
                 _stamp(f.get("by"), f.get("account"), f.get("stored"))]
                for f in kept[MAX_KEPT:]], "Other fits kept with the run (in run.h5)"))
        return charts, tables

    @staticmethod
    def _kept_fit(f: Dict[str, Any], fitting: Any) -> Tuple[Chart, Table]:
        """One kept fit: a plot of its points with the model dashed, and a table of its values, each titled by its
        name (the slides pair them by it). A failed fit, kept on purpose, says so first."""
        spec = f.get("spec") if isinstance(f.get("spec"), dict) else {}
        name, model = str(f.get("name") or "fit"), str(f.get("model") or "model")
        x_key, y_key = str(spec.get("x") or "x"), str(spec.get("y") or name)
        who = "kept " + _stamp(f.get("by"), f.get("account"), f.get("stored"))
        chi2 = f.get("chi2_red")
        failed = str(f.get("failed") or "")
        title = "Kept fit “%s”%s" % (name, " (failed)" if failed else "")
        xs, ys = fitting.kept_points(f)
        fx, fy = fitting.kept_curve(f)
        chart = Chart(title, x_key, str(f.get("x_unit") or ""), str(f.get("y_unit") or ""),
                      [Series(y_key, xs, ys), Series("fit", fx, fy, fit=True)],
                      "%sDashed: the %s fit “%s”, %s%s." % (
                          "Failed (%s): its numbers are no result. " % failed if failed else "", model, name, who,
                          "; reduced chi-squared %s" % num(chi2) if _numbers(chi2) else ""))
        held = set(f.get("held") or [])
        rows = [["failed", failed]] if failed else []
        rows += [[str(p), fitting.fmt(v, e, u or "") + (" (held)" if p in held else "")]
                 for p, v, e, u in zip(f.get("params") or [], f.get("values") or [], f.get("errors") or [],
                                       f.get("units") or [])]
        rows += [[str(d.get("name")), fitting.fmt(d.get("value"), d.get("error"), d.get("unit") or "")]
                 for d in f.get("derived") or [] if isinstance(d, dict)]
        if _numbers(chi2):
            rows.append(["reduced chi-squared", num(chi2)])
        window = ["where %s" % spec["where"]] if spec.get("where") else []
        if isinstance(spec.get("x_range"), list):   # [low, high], either end open
            lo, hi = (list(spec["x_range"]) + [None, None])[:2]
            window.append("%s from %s to %s" % (x_key, num(lo) if _numbers(lo) else "its start",
                                                num(hi) if _numbers(hi) else "its end"))
        rows.append(["points", "%s%s" % (f.get("n", len(xs)), " (%s)" % "; ".join(window) if window else "")])
        return chart, Table(["Quantity", "Value"], rows, "%s vs %s: %s fit, %s" % (title, x_key, model, who))

    def _conditions(self, snapshot: Dict[str, Any]) -> List[Tuple[str, str]]:
        out = []
        for inst, values in snapshot.items():
            if not isinstance(values, dict):
                continue
            parts = []
            for name, v in values.items():
                if _unread(v):
                    continue
                key = "%s.%s" % (inst, name)
                away = _away_from_safe(self._spec(key), v)
                parts.append("%s %s%s" % (name, self._show(key, v), "*" if away else ""))
            out.append((inst, ", ".join(parts)))
        return out

    def _found(self, snapshot: Dict[str, Any], plan: Dict[str, Any]) -> List[Tuple[str, str]]:
        """The plan's instruments: each setting the plan neither set nor swept, as Kelvin found it (no readings)."""
        named: List[str] = []
        for part in ("setup", "sweep", "after"):
            if isinstance(plan.get(part), dict):
                named += [str(k) for k in plan[part]]
        named += [str(m) for m in plan.get("measure") or [] if isinstance(m, str)]
        touched = {k for part in ("setup", "sweep") if isinstance(plan.get(part), dict) for k in map(str, plan[part])}
        out = []
        for inst in dict.fromkeys(k.split(".")[0] for k in named):
            values = snapshot.get(inst)
            if not isinstance(values, dict):
                continue
            parts = []
            for name, v in values.items():
                key = "%s.%s" % (inst, name)
                spec = self._spec(key)
                if (spec is None or spec.set_cmd is None or key in touched or isinstance(v, list) or v is None
                        or (isinstance(v, str) and v.startswith("ERROR"))):
                    continue
                parts.append("%s %s" % (name, self._show(key, v)))
            if parts:
                out.append((inst, ", ".join(parts)))
        return out

    def _away(self, snapshot: Dict[str, Any]) -> List[str]:
        out = []
        for inst, values in snapshot.items():
            for name, v in (values.items() if isinstance(values, dict) else []):
                key = "%s.%s" % (inst, name)
                if _away_from_safe(self._spec(key), v):
                    out.append("%s %s" % (key, self._show(key, v)))
        return out

    def _changes(self, start: Dict[str, Any], end: Dict[str, Any]) -> List[str]:
        out = []
        for inst, values in end.items():
            if not isinstance(values, dict):
                continue
            for name, v in values.items():
                key = "%s.%s" % (inst, name)
                before = (start.get(inst) or {}).get(name)
                if before != v and _away_from_safe(self._spec(key), v):
                    out.append("%s %s → %s" % (key, self._show(key, before), self._show(key, v)))
        return out

    def _unread_at_end(self, end: Dict[str, Any]) -> List[str]:
        """The instruments whose drives the end snapshot couldn't read, with why: where the run left them is not
        known (an instrument unplugged or switched off mid-run)."""
        out = []
        for inst, values in end.items():
            if not isinstance(values, dict):
                continue
            lost = [(name, v) for name, v in values.items()
                    if isinstance(v, str) and v.startswith("ERROR")
                    and getattr(self._spec("%s.%s" % (inst, name)), "is_drive", False)]
            if lost:
                why = lost[0][1][len("ERROR"):].lstrip(": ")
                out.append("%s %s (%s)" % (inst, ", ".join(n for n, _ in lost), why[:120]))
        return out

    # ------------------------------------------------------------------ the whole report

    def report(self, run_ids: Sequence[str], title: str = "", summary: str = "", notes: Iterable[Tuple[str, str]] = (),
               lab_name: str = "", simulated: bool = False, references: Iterable[str] = ()) -> Report:
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
        from .. import rundata
        for r, part in zip(run_ids, runs):
            try:
                meta = rundata.meta(self.runs_dir / r)
            except (KeyError, OSError, ValueError):
                meta = {}
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
                      runs=runs, log=log[-200:], notes=list(notes), references=list(references))


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
