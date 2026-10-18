"""What the agent can do, as short plain-text answers (feature #9: concise).

All instrument access goes through the safety gate and one lock, so an
agent call and a plan never talk to an instrument at the same time. The approval page reads cached state and never waits on the lock.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from ..cards import ParamSpec, num
from ..engine import RunEngine, RunReport
from ..eventlog import EventLog, RunStore
from ..gate import Approver, SafetyGate, _dur
from ..lab import Lab
from ..labconfig import Allowed, Envelope
from ..literature import Literature
from ..monitor import Monitor, Watch
from ..notify import Notifier
from ..plans import Plan, PlanError, load_plan
from ..reports import Collector, pick_runs, write_pdf, write_pptx
from ..yamlio import load_yaml

MAX_WAIT_S = 3600


def tail_events(path: Path, n: int) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        block = min(size, 256 * 1024)
        f.seek(size - block)
        lines = f.read().decode("utf-8", "replace").splitlines()
    if block < size:
        lines = lines[1:]  # the first line may be cut
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def event_line(e: Dict[str, Any]) -> str:
    t = str(e.get("wall", ""))[11:19]
    kind = e.get("type")
    if kind in ("command", "trip"):
        text = e.get("message", "")
    elif kind == "ask":
        lines = str(e.get("request", "")).splitlines()
        text = "%s%s: %s" % (e.get("answer"), " (%s)" % e["note"] if e.get("note") else "",
                             lines[1].strip(" -") if len(lines) > 1 else (lines[0] if lines else ""))
    elif kind == "run_start":
        text = "run %s started, %s points" % (e.get("run"), e.get("points"))
    elif kind == "run_end":
        text = "run %s %s, %s points" % (e.get("run"), e.get("status"), e.get("points"))
    elif kind == "run_refused":
        text = "plan %s refused: %s" % (e.get("plan"), e.get("reason"))
    elif kind == "run_declined":
        text = "plan %s declined" % e.get("plan")
    elif kind == "preflight":
        text = "preflight ok" if e.get("ok") else "preflight FAILED: " + "; ".join(
            r for r in e.get("results", []) if "FAIL" in r)
    elif kind == "safe_state":  # older logs
        text = "SAFE STATE: " + ("; ".join(e.get("results") or []) or "all drives already safe")
    elif kind == "stop":
        text = "stopped the current ramp or plan" + (
            "; declined %d open request(s)" % e["declined"] if e.get("declined") else "")
    elif kind == "alarm":
        text = "ALARM: %s%s" % (e.get("text"), " -> " + "; ".join(e["actions"]) if e.get("actions") else "")
    elif kind == "alarm_cleared":
        text = "cleared: %s" % e.get("text")
    elif kind == "watch":
        text = "%s watch %s: %s" % (e.get("change"), e.get("watch"), e.get("rule"))
    elif kind == "review":
        text = "second opinion on %s: %s" % (e.get("plan"), e.get("verdict"))
    elif kind == "report":
        text = "report %s (%s run%s)" % (e.get("name"), e.get("runs"), "" if e.get("runs") == 1 else "s")
    elif kind == "lab_changed":
        text = "now controlling %s (%s)" % (e.get("lab"), ", ".join(e.get("instruments") or []))
    elif kind == "approval_mode":
        text = "approvals: " + ("auto mode on (low-risk changes run without asking)" if e.get("mode") == "auto"
                                else "ask before every drive increase")
    else:
        text = str(kind)
    by = e.get("by")
    return "%s %s%s" % (t, by + ": " if by and kind != "command" else "", text)


def trace_summary(spec: ParamSpec, values: List[float]) -> str:
    if not values:
        return "empty trace"
    lo = min(range(len(values)), key=values.__getitem__)
    hi = max(range(len(values)), key=values.__getitem__)
    u = " " + spec.unit if spec.unit else ""
    return "%d points; min %s%s at #%d; max %s%s at #%d" % (len(values), num(values[lo]), u, lo, num(values[hi]), u, hi)


class LabRuntime:
    def __init__(self, lab: Lab, approver: Approver, data_dir: Any, envelope: Optional[Envelope] = None):
        self.lab = lab
        self.approver = approver
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.data_dir / "events.jsonl"
        self.gate = SafetyGate(lab, approver, EventLog(self.events_path, lab.clock), envelope, operator="agent")
        self.engine = RunEngine(self.gate, RunStore(self.data_dir / "runs"))
        self.engine.progress = self._on_progress
        self.lock = threading.RLock()
        self.busy: Optional[str] = None
        self._status: Dict[str, Dict[str, Any]] = {}
        self._status_at = 0.0
        self.live: Optional[Dict[str, Any]] = None   # the run in progress (or the last one), for live displays
        self._live_lock = threading.Lock()
        self.lab_info: Dict[str, Any] = {"kind": "virtual" if all(i.simulated for i in lab.instruments.values()) else "file",
                                         "path": None, "error": None, "has_file": False}
        self.notifier = Notifier()
        self.literature = Literature()
        self.monitor = self._new_monitor()
        subscribe = getattr(approver, "subscribe", None)
        if subscribe:  # a person is needed: tell them, wherever they are
            subscribe(lambda r: None if r.get("auto") else self.notifier(
                "approval", "Approval needed: " + r["title"], (r.get("items") or [""])[0]))
        with self.lock:
            self._refresh()

    def _on_progress(self, event: Dict[str, Any]) -> None:
        """Keep a light copy of the running plan's data (scalars only, latest trace) for live displays."""
        with self._live_lock:
            if event["event"] == "start":
                self.live = {"run_id": event["run_id"], "plan": event["plan"], "total": event["total"], "done": 0,
                             "eta_s": None, "status": "running", "phase": "setup", "axes": event["axes"],
                             "measures": event["measures"], "rows": [], "trace": None}
            elif self.live is None or self.live["run_id"] != event["run_id"]:
                return
            elif event["event"] == "point":
                row = event["row"]
                keep = {"i", "t"} | {a["key"] for a in self.live["axes"]} | \
                       {m["key"] for m in self.live["measures"] if m["kind"] == "scalar"}
                self.live["rows"].append({k: v for k, v in row.items() if k in keep})
                for m in self.live["measures"]:
                    if m["kind"] == "trace" and isinstance(row.get(m["key"]), list):
                        ys = row[m["key"]]
                        x0, x1 = row.get(m["key"] + ".axis") or (0, len(ys) - 1)
                        step = max(1, -(-len(ys) // 400))
                        n = len(ys)
                        self.live["trace"] = {"key": m["key"], "unit": m["unit"], "x_unit": m.get("x_unit", ""),
                                              "x": [x0 + (x1 - x0) * i / max(1, n - 1) for i in range(0, n, step)],
                                              "y": ys[::step]}
                self.live["done"], self.live["eta_s"], self.live["phase"] = event["done"], event["eta_s"], "measuring"
            elif event["event"] == "phase":  # after the last point: final values, or the stop/abort steps
                self.live["phase"], self.live["eta_s"] = event["phase"], event.get("eta_s")
            elif event["event"] == "end":
                self.live["status"], self.live["eta_s"], self.live["phase"] = event["status"], 0, "end"

    def live_view(self, since: int = 0) -> Optional[Dict[str, Any]]:
        """The live run with the rows recorded since `since` (thread-safe copy)."""
        with self._live_lock:
            if self.live is None:
                return None
            view = {k: v for k, v in self.live.items() if k != "rows"}
            view["rows"] = list(self.live["rows"][since:])
            view["since"] = since
            return view

    # ------------------------------------------------------------------ plumbing

    def _refresh(self) -> None:
        try:
            self._status = self.lab.snapshot()
            self._status_at = time.time()
        except Exception:
            pass

    def _do(self, what: str, fn: Callable[[], str]) -> str:
        with self.lock:
            self.busy = what
            try:
                return fn()
            finally:
                self.busy = None
                self._refresh()

    def _spec(self, key: str) -> Optional[ParamSpec]:
        try:
            return self.lab.resolve(key)[1]
        except KeyError:
            return None

    def _plan(self, text: str) -> Plan:
        """Plans arrive as YAML/JSON text only, never as a file path."""
        try:
            data = load_yaml(text)
        except yaml.YAMLError as e:
            raise PlanError(["not valid YAML: %s" % str(e).splitlines()[0]])
        if not isinstance(data, dict):
            raise PlanError(["a plan must be a YAML mapping"])
        return load_plan(data)

    # ------------------------------------------------------------------ agent operations

    def lab_status(self) -> str:
        def run() -> str:
            self._refresh()
            sim = all(i.simulated for i in self.lab.instruments.values())
            lines = ["%s%s" % (self.lab.config.name, " (simulated)" if sim else "")]
            if self.gate.auto:
                lines.append("approvals: auto mode (a person turned it on): low-risk drive changes and runs go ahead "
                             "without asking; the rest still wait for a human")
            lines += ["ALARM: %s%s" % (a.text, " (%s)" % "; ".join(a.actions) if a.actions else "")
                      for a in self.monitor.active()]
            lines += self._status_lines()
            lines.append("* = drive away from its safe value")
            return "\n".join(lines)
        return self._do("reading status", run)

    def _status_lines(self) -> List[str]:
        lines = []
        for name, values in self._status.items():
            card = self.lab.instruments[name].card
            parts = []
            for pname, v in values.items():
                spec = card.params[pname]
                if isinstance(v, str) and v.startswith("ERROR"):
                    parts.append("%s ERROR" % pname)
                    continue
                parts.append("%s %s%s" % (pname, spec.show(v), "*" if spec.is_drive and spec.distance(v) > 0 else ""))
            lines.append("%s: %s" % (name, ", ".join(parts)))
        return lines

    def describe(self, instrument: str) -> str:
        return self._do("describing " + instrument, lambda: self.gate.describe(instrument))

    def get(self, instrument: str, parameter: str) -> str:
        def run() -> str:
            r = self.gate.get(instrument, parameter)
            if r.ok and isinstance(r.value, list):
                return "%s.%s: %s" % (instrument, parameter, trace_summary(self._spec(r.target), r.value))
            return r.message
        return self._do("reading %s.%s" % (instrument, parameter), run)

    def set(self, instrument: str, parameter: str, value: Any) -> str:
        return self._do("setting %s.%s" % (instrument, parameter),
                        lambda: self.gate.set(instrument, parameter, value).message)

    def raw(self, instrument: str, command: str) -> str:
        return self._do("raw command to " + instrument, lambda: self.gate.raw(instrument, command).message)

    def undo(self) -> str:
        return self._do("undo", lambda: self.gate.undo().message)

    def stop(self, by: str = "human") -> int:
        """A person's stop: decline open requests and stop the current ramp or plan. Drives stay where they are
        (a stopped plan still runs its own on_abort steps). Returns how many requests were declined."""
        decline_all = getattr(self.approver, "decline_all", None)
        declined = decline_all("stop button") if decline_all else 0
        self.gate.request_abort()
        self.gate.log.write("stop", by=by, declined=declined)
        return declined

    def health(self, instruments: Optional[List[str]] = None) -> str:
        return self._do("health check", lambda: "\n".join(str(r) for r in self.engine.preflight(instruments)))

    def wait(self, seconds: float) -> str:
        seconds = max(0.0, min(float(seconds), MAX_WAIT_S))
        def run() -> str:
            self.lab.clock.sleep(seconds)
            return "waited %s" % _dur(seconds)
        return self._do("waiting", run)

    def validate(self, plan_text: str) -> str:
        def run() -> str:
            try:
                plan = self._plan(plan_text)
            except PlanError as e:
                return "invalid plan: %s" % e
            errors = self.engine.validate(plan)
            if errors:
                return "invalid plan: " + "; ".join(errors[:5])
            items, _, problems = self.gate.plan_approval(self.engine.drive_values(plan))
            lines = ["ok: %s, %d points, instruments %s" % (plan.name, plan.points, ", ".join(plan.requires))]
            lines += ["will be refused: " + p for p in problems]
            lines += ["needs approval: " + i for i in items] or ["no approval needed (measurement only or inside the envelope)"]
            return "\n".join(lines)
        return self._do("validating plan", run)

    def simulate(self, plan_text: str) -> str:
        def run() -> str:
            try:
                plan = self._plan(plan_text)
            except PlanError as e:
                return "invalid plan: %s" % e
            report = self.engine.simulate(plan)
            lines = [report.message]
            lines += ["would ask: " + w.replace("\n", " | ") for w in report.would_ask]
            lines += self._preview(plan, report.rows)
            return "\n".join(lines)
        return self._do("dry run", run)

    def run(self, plan_text: str) -> str:
        def run() -> str:
            try:
                plan = self._plan(plan_text)
            except PlanError as e:
                return "invalid plan: %s" % e
            report = self.engine.run(plan)
            if report.run_id:
                summary = (report.analysis or {}).get("assessment") or report.message
                self.notifier("run", ("Run finished: " if report.status == "done" else "Run stopped: ") + plan.name,
                              summary[:300])
            lines = []
            if report.dry_run is not None and report.status in ("done", "aborted"):
                lines.append(report.dry_run.message)
            lines.append(report.message)
            if report.analysis:
                lines += self._analysis_lines(report.analysis)
            else:
                lines += self._preview(plan, report.rows)
            lines += ["warning: " + w for w in report.warnings]
            if report.run_id and report.rows:
                lines.append("rows: get_run_data(run_id=%r)" % report.run_id)
            return "\n".join(lines)
        return self._do("running plan", run)

    @staticmethod
    def _analysis_lines(a: Dict[str, Any]) -> List[str]:
        lines = ["assessment: " + a["assessment"], "next: " + a["next"]]
        for s in a["series"][:6]:
            label = s["y"] + (" (" + s["group"] + ")" if s.get("group") else "")
            if s["results"]:
                lines.append("%s [%s]: %s" % (label, s["kind"], "; ".join("%s %s" % (r["name"], r["value"]) for r in s["results"][:5])))
        return lines

    def analyze(self, run_id: str, column: Optional[str] = None, model: Optional[str] = None) -> str:
        """Re-analyse a recorded run, optionally one column and/or with a chosen fit model."""
        from ..analysis import MODELS, analyze_run
        if model and model not in MODELS and model != "linear":
            return "unknown model %r (choose: linear, %s)" % (model, ", ".join(MODELS))
        try:
            rows = self._rows(run_id)
            meta = json.loads((self._run_dir(run_id) / "meta.json").read_text(encoding="utf-8"))
            plan = self._plan((self._run_dir(run_id) / "plan.yaml").read_text(encoding="utf-8"))
        except (KeyError, OSError, ValueError, PlanError) as e:
            return "cannot analyse %s: %s" % (run_id, e)
        cols = self.engine.columns(plan)
        if column and column not in [m["key"] for m in cols["measures"]]:
            return "unknown column %r (have: %s)" % (column, ", ".join(m["key"] for m in cols["measures"]))
        a = analyze_run(rows, cols["axes"], cols["measures"], meta, force=model, only=column)
        return "\n".join(["%s: %d rows" % (run_id, len(rows))] + self._analysis_lines(a))

    def _preview(self, plan: Plan, rows: List[Dict[str, Any]]) -> List[str]:
        if not rows:
            return []
        axes = [a.key for a in plan.axes]
        out = []
        for instr, param in plan.measure:
            key = "%s.%s" % (instr, param)
            spec = self._spec(key)
            vals = [(r[key], r) for r in rows if isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool)]
            if not vals:
                if rows and isinstance(rows[0].get(key), list):
                    out.append("%s: %d traces of %d points (get_trace)" % (key, len(rows), len(rows[0][key])))
                continue
            lo = min(vals, key=lambda x: x[0])
            hi = max(vals, key=lambda x: x[0])
            where = lambda r: ", ".join("%s=%s" % (a, num(r[a])) for a in axes) or "row %d" % r["i"]
            out.append("%s: min %s at %s; max %s at %s" % (key, spec.show(lo[0]), where(lo[1]), spec.show(hi[0]), where(hi[1])))
        return out

    # ------------------------------------------------------------------ data

    def _run_dir(self, run_id: str) -> Path:
        d = (self.data_dir / "runs" / run_id).resolve()
        if d.parent != (self.data_dir / "runs").resolve() or not d.is_dir():
            raise KeyError("no run %r (see list_runs)" % run_id)
        return d

    def _rows(self, run_id: str) -> List[Dict[str, Any]]:
        path = self._run_dir(run_id) / "data.jsonl"
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def runs_index(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Most recent runs first: run_id, plan, status, points, planned_points, reason."""
        root = self.data_dir / "runs"
        dirs = sorted((d for d in root.iterdir() if d.is_dir()), reverse=True)[:limit] if root.exists() else []
        out = []
        for d in dirs:
            entry: Dict[str, Any] = {"run_id": d.name, "plan": d.name.split("-", 2)[-1], "status": "unfinished"}
            try:
                meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
                entry.update(status=meta.get("status"), points=meta.get("points"),
                             planned_points=meta.get("planned_points"), reason=meta.get("reason"))
            except (OSError, ValueError):
                live = self.live_view(10 ** 9)
                if live and live["run_id"] == d.name and live["status"] == "running":
                    entry.update(status="running", points=live["done"], planned_points=live["total"])
            out.append(entry)
        return out

    def list_runs(self, limit: int = 10) -> str:
        lines = []
        for r in self.runs_index(limit):
            if r["status"] in ("unfinished", "running"):
                lines.append("%s: %s" % (r["run_id"], r["status"]))
            else:
                lines.append("%s: %s, %s/%s points%s" % (r["run_id"], r["status"], r["points"], r["planned_points"],
                                                         " (%s)" % r["reason"] if r.get("reason") else ""))
        return "\n".join(lines) or "no runs yet"

    def stop_run(self) -> bool:
        """Stop the plan that is running (its on_abort steps then run). False if none is running."""
        live = self.live_view(10 ** 9)
        if not live or live["status"] != "running":
            return False
        self.gate.request_abort()
        return True

    def run_data(self, run_id: str, columns: Optional[List[str]] = None, max_rows: int = 50) -> str:
        try:
            rows = self._rows(run_id)
        except KeyError as e:
            return e.args[0]
        if not rows:
            return "run %s has no data" % run_id
        cols = columns or [k for k in rows[0] if not k.endswith(".axis")]
        missing = [c for c in cols if c not in rows[0]]
        if missing:
            return "unknown columns %s (have: %s)" % (", ".join(missing), ", ".join(rows[0]))
        max_rows = max(1, int(max_rows))
        step = max(1, -(-len(rows) // max_rows))
        picked = rows[::step]
        if picked[-1] is not rows[-1]:
            picked.append(rows[-1])

        def cell(v: Any) -> str:
            if isinstance(v, list):
                return "trace[%d]" % len(v)
            if isinstance(v, float):
                return "%.6g" % v
            return str(v)

        head = "%s: %d rows%s" % (run_id, len(rows), "" if step == 1 else ", every %dth shown" % step)
        return "\n".join([head, ",".join(cols)] + [",".join(cell(r.get(c)) for c in cols) for r in picked])

    def trace(self, run_id: str, row: int, column: str, max_points: int = 200) -> str:
        try:
            rows = self._rows(run_id)
        except KeyError as e:
            return e.args[0]
        if not 0 <= row < len(rows):
            return "row must be 0…%d" % (len(rows) - 1)
        values = rows[row].get(column)
        if not isinstance(values, list):
            return "%s is not a trace column" % column
        n = len(values)
        step = max(1, -(-n // max(2, int(max_points))))
        axis = rows[row].get(column + ".axis")
        spec = self._spec(column)
        lines = ["%s row %d: %s%s" % (run_id, row, trace_summary(spec, values) if spec else "%d points" % n,
                                      "" if step == 1 else "; every %dth point shown" % step)]
        if axis:
            x0, x1 = axis
            lines.append("x,y")
            lines += ["%.12g,%.6g" % (x0 + (x1 - x0) * i / (n - 1 if n > 1 else 1), values[i]) for i in range(0, n, step)]
        else:
            lines.append("index,y")
            lines += ["%d,%.6g" % (i, values[i]) for i in range(0, n, step)]
        return "\n".join(lines)

    def events(self, n: int = 20) -> str:
        return "\n".join(event_line(e) for e in tail_events(self.events_path, max(1, min(int(n), 200)))) or "no events yet"

    # ------------------------------------------------------------------ for the approval page

    def instruments(self) -> List[Dict[str, Any]]:
        """Each instrument for the lab panel: its readings, outputs (drives) and settings, and a state."""
        out = []
        for name, values in dict(self._status).items():
            card = self.lab.instruments[name].card
            readings, drives, settings = [], [], []
            errors = away = 0
            for pname, value in values.items():
                spec = card.params[pname]
                bad = isinstance(value, str) and value.startswith("ERROR")
                errors += bad
                item = {"name": pname, "label": spec.title, "value": "no response" if bad else spec.show(value),
                        "error": bad}
                if spec.is_drive:
                    off_safe = (not bad) and spec.distance(value) > 0
                    applied = True
                    if spec.applies_when:  # e.g. a voltage only reaches the sample while the output is on
                        switch, gspec = values.get(spec.applies_when), card.params[spec.applies_when]
                        applied = switch is not None and not isinstance(switch, str) and gspec.distance(switch) > 0
                        item["switch"] = gspec.title
                    item["away"] = off_safe and applied
                    item["armed"] = off_safe and not applied
                    item["safe"] = spec.show(spec.safe)
                    away += item["away"]
                    drives.append(item)
                elif spec.set_cmd is None:
                    readings.append(item)
                else:
                    settings.append(item)
            headline = card.headline if card.headline in values else (readings[0]["name"] if readings else None)
            readings.sort(key=lambda r: r["name"] != headline)
            out.append({"name": name, "model": card.model, "kind": card.kind,
                        "state": "error" if errors else "active" if away else "safe", "active": away,
                        "headline": headline, "readings": readings, "drives": drives, "settings": settings})
        return out

    # ------------------------------------------------------------------ instruments on this computer

    def scan_found(self) -> List[Dict[str, Any]]:
        """Connected instruments as data, each with a suggested name. Raises AdapterError without VISA."""
        from ..discovery import scan
        in_lab = {ic.address: name for name, ic in self.lab.config.instruments.items()
                  if not ic.address.startswith("sim://")}
        with self.lock:
            self.busy = "scanning for instruments"
            try:
                found = scan(self.lab.registry, in_lab=in_lab)
            finally:
                self.busy = None
        taken = [n for n in self.lab.config.instruments if not self.lab.config.instruments[n].address.startswith("sim://")]
        out = []
        for f in found:
            name = f.suggest_name(taken) if f.card_id and not f.in_lab else None
            if name:
                taken.append(name)
            out.append(dict(vars(f), suggested=name))
        return out

    # ------------------------------------------------------------------ literature

    def search_papers(self, query: str, limit: int = 8, since_year: Optional[int] = None) -> str:
        if not self.literature.enabled:
            return "literature search is switched off in Settings"
        try:
            papers, problems = self.literature.search(query, limit, since_year)
        except ValueError as e:
            return "not searched: %s" % e
        lines = [p.line(i) for i, p in enumerate(papers, 1)] or ["no papers found for %r" % query]
        lines += ["note: " + p for p in problems]
        if papers:
            lines.append("read one with read_paper(paper=<id>); cite as [Author Year](url)")
        return "\n".join(lines)

    def read_paper(self, paper: str, start: int = 0, max_chars: int = 12000) -> str:
        if not self.literature.enabled:
            return "literature search is switched off in Settings"
        try:
            return self.literature.read(paper, start, max_chars)
        except KeyError as e:
            return "not found: %s" % (e.args[0] if e.args else e)
        except Exception as e:
            return "could not read it: %s" % (str(e).splitlines()[0][:200] if str(e) else type(e).__name__)

    # ------------------------------------------------------------------ reports

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    def pick_runs(self, runs: Optional[List[str]] = None, day: str = "", last: int = 0) -> List[str]:
        return pick_runs(self.data_dir / "runs", runs, day, last)

    def report(self, run_ids: List[str], title: str = "", summary: str = "", fmt: str = "pdf",
               notes: Any = (), by: str = "human", references: Any = ()) -> List[Dict[str, Any]]:
        """Write a report of these runs (oldest first) as PDF and/or PowerPoint into reports/. Reads only files:
        it never talks to an instrument, so it can run while a plan runs."""
        if not run_ids:
            raise ValueError("no runs to report")
        fmts = ["pdf", "pptx"] if fmt == "both" else [fmt]
        if any(f not in ("pdf", "pptx") for f in fmts):
            raise ValueError("format must be pdf, pptx or both")

        def columns(run_id: str) -> Optional[Dict[str, List[Dict[str, str]]]]:
            try:
                return self.engine.columns(self._plan((self._run_dir(run_id) / "plan.yaml").read_text(encoding="utf-8")))
            except Exception:   # the run's instruments are not in this lab any more
                return None

        metas = [self._run_meta(r) for r in run_ids]
        labs = sorted({m["lab"] for m in metas if m.get("lab")})
        lab_name = ", ".join(labs) if labs else self.lab.config.name
        simulated = all(m.get("simulated", True) for m in metas) if labs else \
            all(i.simulated for i in self.lab.instruments.values())
        report = Collector(self.data_dir / "runs", self.lab, columns, self.gate.log.events, event_line).report(
            run_ids, title.strip()[:120], summary.strip()[:4000], notes, lab_name, simulated, references)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        base = "%s-%s" % (datetime.now().strftime("%Y%m%d-%H%M%S"),
                          re.sub(r"[^\w-]+", "_", report.title).strip("_")[:48] or "report")
        out = []
        for f in fmts:
            path = self.reports_dir / ("%s.%s" % (base, f))
            n = write_pdf(report, path) if f == "pdf" else write_pptx(report, path)
            out.append({"name": path.name, "path": str(path), "size": path.stat().st_size, "format": f,
                        "pages" if f == "pdf" else "slides": n})
            self.gate.log.write("report", by=by, name=path.name, runs=len(run_ids))
        return out

    def _run_meta(self, run_id: str) -> Dict[str, Any]:
        try:
            return json.loads((self._run_dir(run_id) / "meta.json").read_text(encoding="utf-8"))
        except (KeyError, OSError, ValueError):
            return {}

    def make_report(self, runs: Optional[List[str]] = None, day: str = "", last: int = 0, title: str = "",
                    summary: str = "", fmt: str = "pdf", notes: Any = (), by: str = "agent",
                    references: Any = ()) -> str:
        """The make_report tool's answer. With nothing chosen: today's runs, or else the latest run."""
        try:
            ids = self.pick_runs(runs, day, last)
            if not (runs or day or last):
                ids = self.pick_runs(day="today") or self.pick_runs(last=1)
        except (KeyError, ValueError) as e:
            return "no report: %s" % (e.args[0] if e.args else e)
        if not ids:
            return "no report: no runs %s" % ("on %s" % day if day else "yet")
        try:
            files = self.report(ids, title, summary, fmt, notes, by, references)
        except (ValueError, OSError) as e:
            return "no report: %s" % e
        lines = ["report: %s (%d run%s, %s)" % (f["name"], len(ids), "" if len(ids) == 1 else "s",
                                                "%d pages" % f["pages"] if "pages" in f else "%d slides" % f["slides"])
                 for f in files]
        lines.append("saved in %s; the user can open it from the chat" % self.reports_dir)
        return "\n".join(lines)

    def reports_index(self, limit: int = 30) -> List[Dict[str, Any]]:
        d = self.reports_dir
        files = sorted((p for p in d.iterdir() if p.suffix in (".pdf", ".pptx")), key=lambda p: p.name, reverse=True) \
            if d.exists() else []
        return [{"name": p.name, "size": p.stat().st_size, "format": p.suffix[1:]} for p in files[:limit]]

    def report_path(self, name: str) -> Path:
        """A report file by name; KeyError for anything that is not one."""
        if not re.fullmatch(r"[\w.-]+\.(pdf|pptx)", name or "") or ".." in name:
            raise KeyError("no report %r" % name)
        path = self.reports_dir / name
        if not path.is_file():
            raise KeyError("no report %r" % name)
        return path

    # ------------------------------------------------------------------ watching instruments

    def _new_monitor(self) -> Monitor:
        monitor = Monitor(self.lab, self.gate, self.notifier)
        for w in self.lab.config.watches:
            monitor.add(Watch(w["read"], w["above"], w["below"], w["for"], w["every"], list(w["then"]),
                              w["stop_run"], "lab", w["note"]))
        return monitor

    def add_watch(self, key: str, above: Optional[float] = None, below: Optional[float] = None, for_s: float = 0.0,
                  note: str = "", source: str = "person") -> Dict[str, Any]:
        """A notify-only watch (actions on alarm are for the lab owner's watches in the lab config)."""
        spec = self._spec(key)
        if spec is None:
            raise KeyError("%r is not a parameter in this lab" % key)
        if not spec.numeric or spec.get_cmd is None:
            raise ValueError("%s is not a numeric reading, so it has no range to watch" % key)
        w = self.monitor.add(Watch(key, above, below, max(0.0, float(for_s)), 10.0, [], False, source, note[:200]))
        view = next(v for v in self.monitor.list() if v["id"] == w.id)
        self.gate.log.write("watch", by=source, change="added", watch=w.id, rule=view["rule"])
        return view

    def remove_watch(self, watch_id: str, source: str = "person") -> None:
        w = self.monitor.remove(watch_id, source)   # the lab owner's watches change only in the lab config
        self.gate.log.write("watch", by=source, change="removed", watch=w.id, rule=w.rule())

    def watches_text(self) -> str:
        lines = []
        for v in self.monitor.list():
            state = "ALARM" if v["alarm"] else "ok"
            value = "" if v["value"] is None else " (now %s)" % self._spec(v["key"]).show(v["value"])
            lines.append("%s [%s, %s] %s%s%s" % (v["id"], v["source"], state, v["rule"], value,
                                                 " -> " + "; ".join(v["then"]) if v["then"] else ""))
        for a in self.monitor.active():
            lines.append("active alarm %s: %s" % (a.id, a.text))
        return "\n".join(lines) or "nothing is watched"

    def switch_lab(self, lab: Lab, envelope: Optional[Envelope], info: Dict[str, Any], by: str = "human") -> List[str]:
        """Control another lab (the user's instruments, or the virtual lab). Refused while a plan runs. The approval
        mode goes back to asking, and the new lab is health-checked right away."""
        with self.lock:
            if self.live and self.live.get("status") == "running":
                raise RuntimeError("a plan is running; wait for it to finish or stop it first")
            old = self.lab
            self.lab = lab
            self.gate = SafetyGate(lab, self.approver, EventLog(self.events_path, lab.clock), envelope, operator="agent")
            self.engine = RunEngine(self.gate, RunStore(self.data_dir / "runs"))
            self.engine.progress = self._on_progress
            self.lab_info = dict(info)
            running = self.monitor._thread is not None
            self.monitor.stop()
            self.monitor = self._new_monitor()   # watches name instruments of the old lab; start again
            if running:
                self.monitor.start()
            self._status = {}
            self._refresh()
            self.gate.log.write("lab_changed", by=by, lab=lab.config.name, instruments=sorted(lab.instruments),
                                kind=info.get("kind"))
            results = [str(r) for r in self.engine.preflight()]
        if old is not lab:
            old.close()
        return results

    def scan(self) -> str:
        """Which instruments are connected, matched to cards. Read-only: it only asks who they are."""
        def run() -> str:
            from ..adapters import AdapterError
            from ..discovery import scan
            in_lab = {ic.address: name for name, ic in self.lab.config.instruments.items()
                      if not ic.address.startswith("sim://")}
            try:
                found = scan(self.lab.registry, in_lab=in_lab)
            except AdapterError as e:
                return str(e)
            if not found:
                return ("no instruments found (is VISA installed - NI-VISA or Keysight IO Libraries - and are they on "
                        "and connected?)")
            lines = [f.line() for f in found]
            lines.append("Only a person can add an instrument to the lab (lab config); suggest it, do not assume it.")
            return "\n".join(lines)
        return self._do("scanning for instruments", run)

    # ------------------------------------------------------------------ approval mode (people only)

    def approval_mode(self) -> Dict[str, Any]:
        """The current mode, and what auto mode would approve in this lab, in words for the person choosing."""
        cfg = self.lab.config
        never, ranges = [], []
        for name, ic in cfg.instruments.items():
            card = self.lab.instruments[name].card
            for param, rule in ic.auto.items():
                key = name if param == "*" else "%s.%s" % (name, param)
                if rule == "never":
                    never.append(key)
                elif isinstance(rule, Allowed):
                    ranges.append("%s %s" % (key, rule.show(card.params[param])))
        rule = ("drive changes and runs go ahead without asking when every drive stays within %.0f%% of the way from "
                "its safe value to its lab limit and switches move at most one step from safe; runs also need a clean "
                "dry run" % (100 * cfg.auto_fraction))
        if ranges:
            rule += ". Own auto ranges: " + ", ".join(ranges)
        if never:
            rule += ". Never automatic: " + ", ".join(never)
        return {"mode": "auto" if self.gate.auto else "ask", "fraction": cfg.auto_fraction, "rule": rule + "."}

    def set_approval_mode(self, mode: str, by: str = "human") -> Dict[str, Any]:
        """Only the portal and the approval page call this; no agent tool can. It resets to ask on every start."""
        if mode not in ("ask", "auto"):
            raise ValueError("mode must be ask or auto")
        if (mode == "auto") != self.gate.auto:
            self.gate.auto = mode == "auto"
            self.gate.log.write("approval_mode", by=by, mode=mode)
        return self.approval_mode()

    def ui_state(self) -> Dict[str, Any]:
        # keep the panel live while Kelvin is idle; never wait for a running command
        if time.time() - self._status_at > 2.0 and self.lock.acquire(blocking=False):
            try:
                self._refresh()
            finally:
                self.lock.release()
        status = {}
        for name, values in dict(self._status).items():
            card = self.lab.instruments[name].card
            row = []
            for pname, v in values.items():
                spec = card.params[pname]
                bad = isinstance(v, str) and v.startswith("ERROR")
                row.append({"name": pname, "value": "ERROR" if bad else spec.show(v),
                            "away": (not bad) and spec.is_drive and spec.distance(v) > 0})
            status[name] = row
        pending = getattr(self.approver, "pending_list", lambda: [])()
        history = getattr(self.approver, "history_list", lambda: [])()
        return {"lab": self.lab.config.name,
                "simulated": all(i.simulated for i in self.lab.instruments.values()),
                "busy": self.busy, "pending": pending, "history": history, "status": status,
                "approval_mode": "auto" if self.gate.auto else "ask", "approval_rule": self.approval_mode()["rule"],
                "lab_info": self.lab_info,
                "watches": self.monitor.list(),
                "alarms": [a.view() for a in self.monitor.alarms[:10]],
                "notices": list(self.notifier.notices)[-20:],
                "instruments": self.instruments(),
                "live": {k: v for k, v in (self.live_view(10 ** 9) or {}).items()
                         if k in ("run_id", "plan", "done", "total", "eta_s", "status")} or None,
                "events": [event_line(e) for e in reversed(tail_events(self.events_path, 25))]}
