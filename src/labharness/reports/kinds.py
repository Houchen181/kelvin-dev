"""The three structured reports: scan (one run), device (one sample) and project (the master report).

Each is built from what Kelvin already keeps, with no model call: run folders (meta.json, analysis.json, plan.yaml,
data), the lab record, the project's notes and reading list. The outlines are fixed (see document.py)."""
from __future__ import annotations

import json
import math
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from ..cards import num
from .chart import Chart, Series, Table
from .document import Document, Section
from .model import RunPart

# what the scan report points to for everything it leaves out, in this order
FILE_NOTES = {"run.h5": "every point, trace and camera frame, every instrument setting at the start and the end, "
                        "the plan and the fits in full (HDF5)",
              "record.bin": "every point so far (it becomes run.h5)"}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _day(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%b %d %Y").replace(" 0", " ")
    except (TypeError, ValueError):
        return "?"


def _first_sentence(text: str) -> str:
    text = (text or "").strip()
    return (text.split(". ")[0].rstrip(".") + ".") if text else ""


def safe_name(text: str) -> str:
    """A folder or file name from a project or device name: letters, digits, . _ - only."""
    return re.sub(r"[^\w.-]+", "_", str(text or "").strip()).strip("._")[:60] or "unnamed"


_NUM = re.compile(r"^\(?\s*([-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?)\s*(?:±\s*([\d.]+(?:e[-+]?\d+)?))?\s*\)?(e[-+]?\d+)?\s*(.*)$", re.I)
_PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}


def result_number(r: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], str]:
    """A fit result's value, error and unit; older runs only kept the formatted text, so read it back."""
    if r.get("num") is not None:
        return float(r["num"]), (float(r["err"]) if r.get("err") is not None else None), str(r.get("unit") or "")
    m = _NUM.match(str(r.get("value") or "").strip())
    if not m:
        return None, None, ""
    value, err, exp, unit = float(m.group(1)), float(m.group(2)) if m.group(2) else None, m.group(3), m.group(4).strip()
    if exp:
        scale = 10 ** int(exp[1:])
        value, err = value * scale, (err * scale if err is not None else None)
    if len(unit) > 1 and unit[0] in _PREFIX and unit[1:] in ("V", "A", "Hz", "T", "K", "s", "W", "Ohm", "Ω", "F"):
        f = _PREFIX[unit[0]]
        value, err, unit = value * f, (err * f if err is not None else None), unit[1:]
    return value, err, unit


@dataclass
class Digest:
    """What device and master reports need from one run, read without its data points."""
    run_id: str
    plan: str
    status: str
    started: str
    finished: str
    points: str
    operator: str
    reason: str
    project: Optional[str]
    project_name: str
    device: Optional[str]
    device_name: str
    assessment: str
    next: str
    issues: List[str] = field(default_factory=list)
    results: List[Dict[str, Any]] = field(default_factory=list)       # y, group, name, value, num, err, unit
    ranges: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    setup: Dict[str, Any] = field(default_factory=dict)
    queue: Optional[Dict[str, Any]] = None
    attributed: bool = False         # the run recorded its project (runs before Kelvin 0.3.5 did not)
    lab: str = ""
    simulated: bool = True

    @property
    def day(self) -> str:
        return self.started[:10]

    @property
    def ok(self) -> bool:
        return self.status == "done"

    def key_result(self) -> str:
        r = next((r for r in self.results if not r.get("group")), self.results[0] if self.results else None)
        return "%s %s" % (r["name"], r["value"]) if r else (_first_sentence(self.assessment) or self.reason or "")


def digest(run_dir: Path) -> Optional[Digest]:
    from .. import rundata
    try:
        rec = rundata.load(run_dir, with_rows=False)
    except (KeyError, OSError, ValueError):
        return None
    meta = rec.meta
    if not meta or meta.get("status") in (None, "running"):
        return None
    analysis = rec.analysis or {}
    try:
        plan = yaml.safe_load(rec.plan_text) or {}
    except yaml.YAMLError:
        plan = {}
    ranges: Dict[str, Tuple[float, float]] = {}
    try:
        from ..plans import load_plan
        for a in load_plan(plan).instrument_axes:
            vals = [v for v in a.values if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if vals:
                ranges[a.key] = (min(vals), max(vals))
    except Exception:
        pass
    results = []
    for s in analysis.get("series") or []:
        for r in s.get("results") or []:
            value, err, unit = result_number(r)
            results.append({"y": s.get("y"), "group": s.get("group"), "name": r.get("name"), "value": r.get("value"),
                            "num": value, "err": err, "unit": unit})
    return Digest(run_id=run_dir.name, plan=str(plan.get("plan") or meta.get("plan") or run_dir.name.split("-", 2)[-1]),
                  status=str(meta.get("status") or "unfinished"), started=str(meta.get("started") or ""),
                  finished=str(meta.get("finished") or ""),
                  points="%s of %s" % (meta.get("points", "?"), meta.get("planned_points", "?")),
                  operator=str(meta.get("operator") or "?"), reason=str(meta.get("reason") or ""),
                  project=meta.get("project"), project_name=str(meta.get("project_name") or "General"),
                  device=meta.get("device"), device_name=str(meta.get("device_name") or ""),
                  assessment=str(analysis.get("assessment") or ""), next=str(analysis.get("next") or ""),
                  issues=[str(i) for i in analysis.get("issues") or []], results=results, ranges=ranges,
                  setup={str(k): v for k, v in (plan.get("setup") or {}).items()}, queue=meta.get("queue"),
                  attributed="project" in meta, lab=str(meta.get("lab") or ""),
                  simulated=bool(meta.get("simulated", True)))


def _period(ds: Sequence[Digest]) -> str:
    days = sorted(d.day for d in ds if d.day)
    if not days:
        return "no scans yet"
    first, last = _day(days[0]), _day(days[-1])
    return first if first == last else "%s to %s" % (first, last)


def _outcomes(ds: Sequence[Digest]) -> str:
    c = Counter("finished" if d.ok else "stopped" if d.status == "aborted" else d.status for d in ds)
    return ", ".join("%d %s" % (n, k) for k, n in c.most_common())


def _named(chart: Chart, prefix: str) -> Chart:
    return Chart("%s: %s" % (prefix, chart.title), chart.x_key, chart.x_unit, chart.y_unit, chart.series, chart.caption)


SECONDARY = {"background", "signal / noise", "noise (rms)", "intercept", "drift (slope)", "mean"}


def _trends(ds: Sequence[Digest], limit: int = 4) -> Tuple[List[Chart], List[str]]:
    """A fitted quantity followed across repeated scans of the same measurement (finished scans only); the main
    results first (centre, width, slope, time constants), not backgrounds or noise."""
    series: "OrderedDict[Tuple[str, str, str], List[Tuple[datetime, float, str, str]]]" = OrderedDict()
    for d in sorted(ds, key=lambda d: d.started):
        if not d.ok:
            continue
        try:
            when = datetime.fromisoformat(d.started)
        except ValueError:
            continue
        for r in d.results:
            if r.get("group") or r.get("num") is None or not math.isfinite(r["num"]) or r["name"] in SECONDARY:
                continue
            series.setdefault((d.plan, str(r["y"]), str(r["name"])), []).append((when, r["num"], r["unit"],
                                                                                str(r["value"])))
    charts, lines = [], []
    for (plan, y, name), pts in series.items():
        if len(pts) < 2 or len(charts) >= limit:
            continue
        t0 = pts[0][0]
        span = (pts[-1][0] - t0).total_seconds()
        scale, unit_t = (60.0, "min") if span < 2 * 3600 else (3600.0, "h") if span < 3 * 86400 else (86400.0, "d")
        xs = [(t - t0).total_seconds() / scale for t, _, _, _ in pts]
        charts.append(Chart("%s: %s %s" % (plan, y, name), "time since %s" % t0.strftime("%b %d %H:%M").replace(" 0", " "),
                            unit_t, pts[-1][2], [Series(name, xs, [v for _, v, _, _ in pts])], "%d scans" % len(pts)))
        lines.append("%s, %s %s: %s, then %s (%d scans)." % (plan, y, name, pts[0][3], pts[-1][3], len(pts)))
    return charts, lines


def _ranges(ds: Sequence[Digest]) -> Table:
    seen: Dict[str, List[float]] = {}
    count: Counter = Counter()
    for d in ds:
        for key, (lo, hi) in d.ranges.items():
            seen.setdefault(key, []).extend([lo, hi])
            count[key] += 1
        for key, v in d.setup.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                seen.setdefault(key, []).append(float(v))
                count[key] += 1
    rows = [[k, num(min(v)), num(max(v)), str(count[k])] for k, v in sorted(seen.items())]
    return Table(["Quantity", "Lowest", "Highest", "Scans"], rows, "Swept or set")


def _index(ds: Sequence[Digest], with_device: bool, limit: int = 300) -> Table:
    header = ["Date", "Device", "Measurement", "Status", "Key result", "Run"] if with_device else \
        ["Date", "Measurement", "Status", "Key result", "Run"]
    rows = []
    for d in sorted(ds, key=lambda d: d.started, reverse=True)[:limit]:
        row = [_day(d.started)] + ([d.device_name or "-"] if with_device else []) + \
              [d.plan, d.status, d.key_result()[:70], d.run_id]
        rows.append(row)
    return Table(header, rows)


# ------------------------------------------------------------------ the scan report

def scan_document(part: RunPart, d: Digest, files: Iterable[str], lab: str, simulated: bool) -> Document:
    """One page: what was measured and what came out, and the conditions that bear on it. The rest stays in the
    run's folder, named at the end: every point, every instrument setting at the start and the end, the plan, the
    fits in full."""
    done, _, planned = part.points.partition(" of ")
    bits = [d.project_name or "General", part.when + (" (%s)" % part.duration if part.duration else ""),
            "%s points" % (done if done == planned else part.points)]
    if part.status != "done":
        bits.append(part.status + (": " + part.reason if part.reason else ""))
    bits.append("started by " + part.operator)
    if d.queue:
        bits.append("queue item %s" % d.queue.get("position", "?"))
    result = Section("Result", paras=[part.assessment] if part.assessment else [],
                     bullets=["Issue: " + i for i in part.issues], charts=list(part.charts), tables=list(part.results))
    used = {name for name, _ in part.found} | {k.split(".")[0] for k in part.measured}
    facts = [(k, v) for k, v in (("Swept", "; ".join(part.sweep)), ("Set", "; ".join(part.setup)),
                                ("Measured", ", ".join(part.measured))) if v]
    facts += [(name, "as found: " + values) for name, values in part.found]
    elsewhere = [a for a in part.away if a.split(".")[0] not in used]
    if elsewhere:   # what else was on while it ran (a field, a temperature, a laser)
        facts.append(("Also not at safe", "; ".join(elsewhere)))
    if part.changes:
        facts.append(("Left changed", "; ".join(part.changes)))
    conditions = Section("Conditions", facts=facts)
    events = Section("Approvals and alarms", facts=list(part.events))
    files = set(files)
    names = [n for n in FILE_NOTES if n in files]
    note = ("In the run's folder: " + "; ".join("%s (%s)" % (n, FILE_NOTES[n]) for n in names) + ".") if names else ""
    return Document("scan", "%s on %s" % (part.plan, d.device_name) if d.device_name else part.plan,
                    "  ·  ".join(bits), _now(), lab, simulated, [], [result, conditions, events], compact=True,
                    note=note)


# ------------------------------------------------------------------ the device report

def device_document(device: Dict[str, Any], project_name: str, ds: Sequence[Digest], latest: Dict[str, RunPart],
                    mounts: Sequence[Tuple[str, str]], lab: str, simulated: bool) -> Document:
    """ds: the device's scans; latest: the latest finished scan of each measurement, in full (for its plot)."""
    types = Counter(d.plan for d in ds)
    facts = [("Project", project_name), ("Material", device.get("material") or "-"),
             ("Scans", "%d (%s)" % (len(ds), _outcomes(ds)) if ds else "none yet"), ("Period", _period(ds)),
             ("Measurements", ", ".join("%s (%d)" % kv for kv in types.most_common()) or "-")]
    about = Section("The device", "material, geometry, contacts and fabrication",
                    paras=[device["description"]] if device.get("description") else [],
                    facts=[("Material", device["material"])] if device.get("material") else [],
                    empty="No description yet: add it in Project settings, Devices.")
    history = Section("Mount history", "when the sample was put in and taken out of the setup", facts=list(mounts),
                      empty="No mount recorded. Mount the device in the Lab panel so every scan is filed under it.")
    rows, charts = [], []
    for plan, n in types.most_common():
        mine = [d for d in ds if d.plan == plan]
        done = [d for d in mine if d.ok]
        last = max(done or mine, key=lambda d: d.started)
        rows.append([plan, str(n), _day(last.started), last.key_result()[:60]])
        part = latest.get(plan)
        if part and part.charts:
            charts.append(_named(part.charts[0], "%s, %s" % (plan, _day(last.started))))
    by_type = Section("Results by measurement", "each measurement's latest finished scan",
                      tables=[Table(["Measurement", "Scans", "Latest", "Key result"], rows)] if rows else [],
                      charts=charts[:6], empty="No scans on this device yet.")
    trend_charts, trend_lines = _trends(ds)
    trends = Section("Trends", "a fitted value followed across repeated scans", bullets=trend_lines,
                     charts=trend_charts, empty="Trends appear once a measurement has been repeated on this device.")
    rng = _ranges(ds)
    ranges = Section("Ranges explored", "the lowest and highest values swept or set in its scans",
                     tables=[rng] if rng.rows else [], empty="No sweeps recorded yet.")
    problems = _problems(ds)
    index = Section("Scan index", "every scan, newest first; each has its own report in its run folder",
                    tables=[_index(ds, with_device=False)] if ds else [], empty="No scans yet.")
    title = device.get("name") or "Device"
    return Document("device", title, "%s  ·  %s" % (project_name, _period(ds)), _now(), lab, simulated, facts,
                    [about, history, by_type, trends, ranges, problems, index])


def _problems(ds: Sequence[Digest]) -> Section:
    bullets = ["%s, %s: %s%s" % (_day(d.started), d.plan, d.status, ": " + d.reason if d.reason else "")
               for d in sorted(ds, key=lambda d: d.started, reverse=True) if not d.ok][:12]
    common = Counter(i.split(":")[0] for d in ds for i in d.issues)
    bullets += ["%s (%d scan%s)" % (k, n, "" if n == 1 else "s") for k, n in common.most_common(6)]
    return Section("Problems", "stopped or failed scans, and issues the analysis flagged", bullets=bullets,
                   empty="No problems recorded.")


# ------------------------------------------------------------------ the project's master report

def summary_paragraphs(project_name: str, devices: Sequence[Dict[str, Any]], ds: Sequence[Digest]) -> List[str]:
    """The master report's summary when nobody has written one: what was measured, on what, and the latest result."""
    if not ds:
        return ["No scans in %s yet. Mount a device in the Lab panel and run a measurement; this report fills itself "
                "in as you go." % project_name]
    on = sorted({d.device_name for d in ds if d.device_name})
    types = Counter(d.plan for d in ds)
    out = ["%d scan%s%s, %s: %s." % (len(ds), "" if len(ds) == 1 else "s",
                                    " on %d device%s (%s)" % (len(on), "" if len(on) == 1 else "s", ", ".join(on[:8]))
                                    if on else "", _period(ds), _outcomes(ds)),
           "Measurements: %s." % ", ".join("%s (%d)" % kv for kv in types.most_common(8))]
    last = max(ds, key=lambda d: d.started)
    if last.assessment:
        out.append("Latest (%s, %s%s): %s" % (_day(last.started), last.plan,
                                            " on " + last.device_name if last.device_name else "",
                                            _first_sentence(last.assessment)))
    return out


def project_document(project: Dict[str, Any], ds: Sequence[Digest], latest: Dict[str, Tuple[RunPart, Digest]],
                     device_files: Dict[str, str], lab: str, simulated: bool) -> Document:
    """project: id, name, devices, notes [(kind, text)], references [str], summary.
    latest: the latest finished scan of each measurement across the project, in full (for its plot)."""
    name = project.get("name") or "General"
    devices = [d for d in project.get("devices") or []]
    types = Counter(d.plan for d in ds)
    facts = [("Period", _period(ds)), ("Devices", ", ".join(d["name"] for d in devices) or "none yet"),
             ("Scans", "%d (%s)" % (len(ds), _outcomes(ds)) if ds else "none yet"),
             ("Measurements", ", ".join("%s (%d)" % kv for kv in types.most_common(6)) or "-")]
    written = [p.strip() for p in str(project.get("summary") or "").split("\n\n") if p.strip()]
    summary = Section("Summary", "written by you or Kelvin" if written else "from the data so far; write your own in "
                      "Project settings, Master report", paras=written + summary_paragraphs(name, devices, ds)[:3])
    notes = [(k, t) for k, t in project.get("notes") or []]
    goals = Section("Goals and decisions", "the project's notes: decisions, procedures, quirks and results",
                    bullets=["%s: %s" % (k.capitalize(), t) for k, t in notes],
                    empty="No project notes yet. Tell Kelvin to remember decisions and results, and they appear here.")
    compared = _devices_compared(devices, ds)
    figures = []
    rows = []
    for plan, n in types.most_common():
        mine = [d for d in ds if d.plan == plan]
        on = sorted({d.device_name for d in mine if d.device_name})
        last = max([d for d in mine if d.ok] or mine, key=lambda d: d.started)
        rows.append([plan, str(n), ", ".join(on[:4]) or "-", last.key_result()[:60]])
        if plan in latest and latest[plan][0].charts:
            part, dg = latest[plan]
            figures.append(_named(part.charts[0], "%s%s, %s" % (plan, " on " + dg.device_name if dg.device_name else "",
                                                               _day(dg.started))))
    main = Section("Main results and figures", "each measurement's latest finished scan, and what it found",
                   tables=[Table(["Measurement", "Scans", "Devices", "Latest result"], rows)] if rows else [],
                   charts=figures[:6], empty="No finished scans yet.")
    timeline = Section("Timeline", "what was measured each day, newest first", tables=[_timeline(ds)] if ds else [],
                       empty="No scans yet.")
    open_items = _problems(ds)
    open_items.title, open_items.lead = "Open questions and issues", "stopped scans, flagged issues and suggested next steps"
    nexts = []
    for d in sorted(ds, key=lambda d: d.started, reverse=True):
        if d.next and d.next not in nexts:
            nexts.append(d.next)
    open_items.bullets += ["Next (%s): %s" % (p, n) for p, n in
                           [(next(x.plan for x in ds if x.next == n), n) for n in nexts[:4]]]
    refs = Section("References", "the project's reading list", bullets=list(project.get("references") or []),
                   empty="No papers on the reading list yet.")
    index_tables = []
    if device_files:
        index_tables.append(Table(["Device", "Device report"], [[k, v] for k, v in sorted(device_files.items())],
                                  "Device reports"))
    if ds:
        index_tables.append(_index(ds, with_device=True))
    index = Section("Index", "device reports in this folder; each scan's report is in its run folder",
                    tables=index_tables, empty="No reports yet.")
    return Document("project", name, "Master report  ·  %s" % _period(ds), _now(), lab, simulated, facts,
                    [summary, goals, compared, main, timeline, open_items, refs, index])


def _devices_compared(devices: Sequence[Dict[str, Any]], ds: Sequence[Digest]) -> Section:
    # the fitted values most devices share: (measurement, quantity, result) seen on the most devices
    shared: Counter = Counter()
    for (plan, y, name), devs in _by_result(ds).items():
        shared[(plan, y, name)] = len(devs)
    columns = [k for k, n in shared.most_common(3) if n >= 1]
    by_result = _by_result(ds)
    rows = []
    names = [d["name"] for d in devices] + sorted({d.device_name for d in ds if d.device_name} - {d["name"] for d in devices})
    material = {d["name"]: d.get("material") or "" for d in devices}
    for dev in names:
        mine = [d for d in ds if d.device_name == dev]
        row = [dev, material.get(dev, "") or "-", str(len(mine)), _day(max(d.started for d in mine)) if mine else "-"]
        row += [by_result.get(c, {}).get(dev, "-") for c in columns]
        rows.append(row)
    header = ["Device", "Material", "Scans", "Last measured"] + ["%s %s" % (c[0], c[2]) for c in columns]
    return Section("Devices compared", "each device's latest finished value of the results the devices share",
                   tables=[Table(header, rows)] if rows else [],
                   empty="No devices yet. Add them in Project settings and mount one in the Lab panel before measuring.")


def _by_result(ds: Sequence[Digest]) -> Dict[Tuple[str, str, str], Dict[str, str]]:
    out: Dict[Tuple[str, str, str], Dict[str, str]] = {}
    for d in sorted(ds, key=lambda d: d.started):
        if not d.ok or not d.device_name:
            continue
        for r in d.results:
            if not r.get("group"):
                out.setdefault((d.plan, str(r["y"]), str(r["name"])), {})[d.device_name] = str(r["value"])
    return out


def _timeline(ds: Sequence[Digest]) -> Table:
    days: "OrderedDict[str, List[Digest]]" = OrderedDict()
    for d in sorted(ds, key=lambda d: d.started, reverse=True):
        days.setdefault(d.day, []).append(d)
    rows = []
    for day, items in list(days.items())[:90]:
        rows.append([_day(items[0].started), str(len(items)),
                     ", ".join(sorted({d.device_name for d in items if d.device_name})) or "-",
                     ", ".join("%s (%d)" % kv for kv in Counter(d.plan for d in items).most_common(4)), _outcomes(items)])
    return Table(["Day", "Scans", "Devices", "Measurements", "Outcome"], rows)


def write_index_csv(path: Path, ds: Sequence[Digest]) -> None:
    import csv
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["started", "device", "measurement", "status", "points", "key result", "run_id", "folder"])
        for d in sorted(ds, key=lambda d: d.started):
            w.writerow([d.started, d.device_name, d.plan, d.status, d.points, d.key_result(), d.run_id,
                        "runs/%s" % d.run_id])


def _now() -> str:
    return datetime.now().strftime("%b %d %Y, %H:%M").replace(" 0", " ")
