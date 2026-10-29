"""A run's or a day's record as Markdown, with its figures, for an electronic lab notebook (F3 #20: no ELN export).

    <data folder>/eln/<name>/
        <name>.md      the record: what ran, when and who approved it, the conditions at its start and each
                       instrument's calibration then, what it set and swept and measured, the results with their fits,
                       the assessment and its issues, what it left on, and the plan; a day's adds the day's lab record
        figures/       each run's plots as PNG, linked from the Markdown

Written by code from what Kelvin keeps (the run folders and the lab record, as reports gather them), never through a
model, so every number is the run's own. Markdown pastes into most notebooks (eLabFTW, LabArchives, Benchling,
OneNote, Obsidian), and the folder, or its .zip from the portal, uploads whole."""
from __future__ import annotations

import datetime
import io
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

FOLDER = "eln"
MAX_RUNS = 40          # a day's record past this many runs keeps the latest
MAX_DAY_LINES = 400    # the day's lab record, its latest lines
# the lab record's events a day's page keeps: what was run, approved, stopped, changed; not every command sent
DAY_TYPES = {"ask", "answer", "run_start", "run_end", "run_refused", "run_declined", "trip", "alarm", "alarm_cleared",
             "stop", "lab_changed", "approval_mode", "preflight", "queue_start", "queue_end", "queue_declined",
             "queue_pause", "mount", "unmount", "protection", "limits", "lab_reviewed", "hands_on", "hands_off",
             "owner_pin", "calibration", "instrument_switched", "instrument_added", "incident", "incident_safe",
             "idle_off", "data_folder", "report", "watch", "review"}


def _cell(text: Any) -> str:
    """A table cell: one line, its pipes escaped."""
    return " ".join(str(text if text is not None else "").split()).replace("|", "\\|") or " "


def _table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> List[str]:
    rows = list(rows)
    if not rows:
        return []
    return ["| %s |" % " | ".join(_cell(h) for h in header), "|%s|" % "|".join("---" for _ in header)] + \
        ["| %s |" % " | ".join(_cell(c) for c in row) for row in rows]


def _safe_name(text: str) -> str:
    return re.sub(r"[^\w.-]+", "_", text).strip("._")[:60] or "record"


def _calibration_lines(home: Optional[Path], instruments: Iterable[str], started: str) -> List[str]:
    """Each instrument's calibration in force when the run started, as recorded (profiles.py)."""
    if home is None:
        return []
    from . import profiles
    try:
        when = datetime.datetime.fromisoformat(started)
    except (TypeError, ValueError):
        return []
    out = []
    for name in instruments:
        try:
            record = profiles.calibration_at(home, name, when)
        except KeyError:
            continue
        if not record:
            continue
        state = record.get("state") or {}
        words = state.get("words") or ""
        if state.get("state") == "overdue":
            words = "**out of date at the time of the run** (due %s)%s" % (
                record.get("due"), ", certificate %s" % record["certificate"] if record.get("certificate") else "")
        out.append("- %s: %s%s" % (name, words, ", serial %s" % record["serial"] if record.get("serial") else ""))
    return out


def run_markdown(part: Any, figures: Sequence[Tuple[str, str, str]], meta: Dict[str, Any], level: int = 2,
                 calibration: Sequence[str] = ()) -> List[str]:
    """One run's section. part: its report part (reports.model.RunPart); figures: (file, title, caption) of its
    plots, as written; meta: its folder and lab; level: its heading's level."""
    h = "#" * level
    out = ["%s %s, run %s" % (h, part.plan, part.run_id), ""]
    out += _table(["", ""], [r for r in (
        ("When", part.when), ("Duration", part.duration), ("Points", part.points), ("Status", part.status),
        ("Why it stopped", part.reason if part.status != "done" else ""), ("Operator", part.operator),
        ("Lab", meta.get("lab") or ""), ("Instruments", "simulated" if meta.get("simulated") else ""),
        ("Run folder", meta.get("folder") or "")) if r[1]])
    did = [("Set first", part.setup), ("Swept", part.sweep), ("Measured", part.measured)]
    if any(v for _, v in did):
        out += ["", "%s# What it did" % h, ""]
        out += ["- %s: %s" % (k, "; ".join(v)) for k, v in did if v]
    if part.conditions:
        out += ["", "%s# Conditions at the start" % h, ""]
        out += _table(["Instrument", "Values"], part.conditions)
        if part.away:
            out += ["", "Away from safe at the start: %s." % ", ".join(part.away)]
    if calibration:
        out += ["", "%s# Calibration" % h, ""] + list(calibration)
    if figures or part.results or part.assessment:
        out += ["", "%s# Results" % h]
        for file, title, caption in figures:
            out += ["", "![%s](%s)" % (title.replace("]", ")"), file), "", "*%s*" % caption.replace("*", "\\*")]
        for t in part.results:
            out += [""] + (["**%s**" % t.title, ""] if t.title else []) + _table(t.header, t.rows)
        if part.assessment:
            out += ["", "**Assessment.** %s" % part.assessment]
        if part.issues:
            out += ["", "**Issues:**"] + ["- %s" % i for i in part.issues]
        if part.next:
            out += ["", "**Next.** %s" % part.next]
    if part.events:
        out += ["", "%s# Approvals and events" % h, ""] + ["- %s %s" % (t, text) for t, text in part.events]
    if part.changes or part.unread:
        out += ["", "%s# At the end" % h, ""]
        out += ["- Ended somewhere else than it began: %s" % c for c in part.changes]
        out += ["- Couldn't be read at the end: %s" % u for u in part.unread]
    if part.plan_text.strip():
        out += ["", "%s# Plan" % h, "", "```yaml", part.plan_text.strip(), "```"]
    return out


def markdown(report: Any, figures: Dict[str, List[Tuple[str, str, str]]], metas: Dict[str, Dict[str, Any]],
             calibration: Dict[str, List[str]], day_log: Sequence[str] = (), exported_by: str = "") -> str:
    """The record as one Markdown page: a run's, or several runs' (a day's) with that day's lab record."""
    many = len(report.runs) > 1 or bool(day_log)
    lines = ["# %s" % report.title, ""]
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines.append("Kelvin lab record%s. %s. Exported %s%s." % (", %s" % report.lab if report.lab else "",
                                                             report.subtitle, stamp,
                                                             " by %s" % exported_by if exported_by else ""))
    if report.simulated:
        lines += ["", "> Simulated instruments: these runs measured no real sample."]
    if many:
        lines += ["", "## Runs", ""]
        lines += _table(["When", "Run", "Plan", "Status", "Points"],
                        [(r.when, r.run_id, r.plan, r.status, r.points) for r in report.runs]) or ["No runs."]
    for part in report.runs:
        lines += [""] + run_markdown(part, figures.get(part.run_id, []), metas.get(part.run_id, {}),
                                     3 if many else 2, calibration.get(part.run_id, []))
    if day_log:
        lines += ["", "## The day's lab record", "", "Approvals, runs, stops, alarms and changes to the lab, as "
                  "Kelvin's record (events.jsonl) keeps them; every command is there too.", ""]
        lines += ["- %s" % line for line in day_log]
    return "\n".join(lines).rstrip() + "\n"


def _figures(part: Any, folder: Path) -> List[Tuple[str, str, str]]:
    """Each of a run's plots as a PNG in figures/: (file as linked, title, caption in words)."""
    from .pictures import charts_caption, charts_png
    out = []
    for n, chart in enumerate(part.charts, 1):
        try:
            png = charts_png([chart])
        except Exception:   # a plot that can't be drawn never loses the record: its words stay
            continue
        name = "%s-%d.png" % (_safe_name(part.run_id), n)
        (folder / "figures").mkdir(parents=True, exist_ok=True)
        (folder / "figures" / name).write_bytes(png)
        out.append(("figures/" + name, chart.title, charts_caption([chart])))
    return out


def _day_log(events: Sequence[Dict[str, Any]], day: str, line: Any) -> List[str]:
    """That day's lab record in words, oldest first."""
    out = []
    for e in events:
        if not str(e.get("wall", "")).startswith(day) or e.get("type") not in DAY_TYPES:
            continue
        if e.get("type") == "preflight" and e.get("ok"):
            continue
        if e.get("type") == "calibration":
            out.append("%s %s: %s calibration recorded, due %s%s" % (
                str(e.get("wall", ""))[11:19], e.get("by") or "a person", e.get("instrument"), e.get("due"),
                " (certificate %s)" % e["certificate"] if e.get("certificate") else ""))
            continue
        out.append(line(e))
    return out[-MAX_DAY_LINES:]


def export(rt: Any, runs: Optional[List[str]] = None, day: str = "", last: int = 0, by: str = "agent",
           target: Optional[Path] = None) -> Dict[str, Any]:
    """Write the record of these runs (or of a day: its runs and its lab record) into the data folder's eln/, or
    target. Returns where (path, the .md file), its name and how many runs and figures. KeyError, ValueError: no
    such run or day."""
    from .agent.runtime import event_line
    words = str(day or "").strip().lower()
    if words in ("today", "yesterday"):
        day = (datetime.date.today() - datetime.timedelta(days=1 if words == "yesterday" else 0)).isoformat()
    elif day:
        try:
            day = datetime.date.fromisoformat(str(day).strip()).isoformat()
        except ValueError:
            raise ValueError("day must be YYYY-MM-DD, today or yesterday")
    ids = rt.pick_runs(runs, day, last) if (runs or day or last) else rt.pick_runs(last=1)
    if not ids and not day:
        raise KeyError("no runs yet")
    ids = ids[-MAX_RUNS:]
    events = rt.gate.log.events()
    if ids:   # as the reports gather runs (reports.collect): the runtime's collector finds every run's folder
        report = rt._collector().report(ids, "", "", (), rt.lab.config.name, False)
    else:   # a day with no runs: its lab record alone
        from .reports.model import Report
        report = Report(title="", subtitle="no runs", created="", lab=rt.lab.config.name, simulated=False)
    metas = {}
    for r in ids:
        try:
            meta = rt._run_meta(r)
        except Exception:
            meta = {}
        try:
            folder = str(rt._run_dir(r))
        except KeyError:
            folder = ""
        metas[r] = {"lab": meta.get("lab") or "", "simulated": meta.get("simulated"), "folder": folder,
                    "started": meta.get("started") or ""}
    report.simulated = bool(ids) and all(m.get("simulated") for m in metas.values())
    when = datetime.date.fromisoformat(day).strftime("%b %d %Y").replace(" 0", " ") if day else ""
    if day:
        report.title = "Lab record, %s" % when
        name = "%s-lab-record" % day
    elif len(ids) == 1:
        report.title = "Run %s" % ids[0]
        name = ids[0]
    else:
        report.title = "Runs %s to %s" % (ids[0], ids[-1])
        name = "%s-to-%s" % (ids[0][:15], ids[-1][:15])
    name = _safe_name(name)
    folder = (target or rt.data_dir / FOLDER) / name
    folder.mkdir(parents=True, exist_ok=True)
    figures = {part.run_id: _figures(part, folder) for part in report.runs}
    home = getattr(rt, "home", None)
    calibration = {}
    for part in report.runs:
        used = [n for n, _ in part.conditions]   # the instruments its start snapshot read
        calibration[part.run_id] = _calibration_lines(home, used, metas.get(part.run_id, {}).get("started", ""))
    log = _day_log(events, day, event_line) if day else []
    text = markdown(report, figures, metas, calibration, log, exported_by=getattr(rt, "user", None) or by)
    path = folder / (name + ".md")
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    rt.gate.log.write("eln_export", by=by, name=name, runs=len(ids), day=day or None)
    return {"path": str(path), "folder": str(folder), "name": name, "runs": len(ids),
            "figures": sum(len(f) for f in figures.values())}


def zipped(folder: Any) -> bytes:
    """The record's folder as a .zip: its Markdown and its figures, as an ELN takes them."""
    folder = Path(folder)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(folder.rglob("*")):
            if f.is_file() and not f.name.endswith(".tmp"):
                z.write(f, Path(folder.name) / f.relative_to(folder))
    return buf.getvalue()


def answer(result: Dict[str, Any]) -> str:
    """The export_eln tool's answer."""
    return ("ELN record: %s (%d run%s, %d figure%s in figures/ beside it). Paste the Markdown into the notebook, or "
            "upload the folder; the portal's Runs pane downloads it as a .zip." % (
                result["path"], result["runs"], "" if result["runs"] == 1 else "s", result["figures"],
                "" if result["figures"] == 1 else "s"))
