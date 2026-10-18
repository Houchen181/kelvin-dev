"""What the agent can do, as short plain-text answers (feature #9: concise).

All instrument access goes through the safety gate and one lock, so an
agent call, a plan and the kill switch never talk to an instrument at the
same time. The approval page reads cached state and never waits on the lock.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from ..cards import ParamSpec, num
from ..engine import RunEngine, RunReport
from ..eventlog import EventLog, RunStore
from ..gate import Approver, SafetyGate, _dur
from ..lab import Lab
from ..labconfig import Envelope
from ..plans import Plan, PlanError, load_plan
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
    elif kind == "safe_state":
        text = "SAFE STATE: " + ("; ".join(e.get("results") or []) or "all drives already safe")
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
        self.lock = threading.RLock()
        self.busy: Optional[str] = None
        self._status: Dict[str, Dict[str, Any]] = {}
        self._status_at = 0.0
        with self.lock:
            self._refresh()

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

    def safe_state(self, by: str = "agent") -> str:
        def run() -> str:
            results = self.gate.safe_state(by=by)
            return "\n".join(r.message for r in results) or "all drives already at safe values"
        return self._do("safe state", run)

    def kill(self, by: str = "human") -> List[str]:
        """The page's kill switch: decline open requests, stop ramps and plans, then go safe."""
        decline_all = getattr(self.approver, "decline_all", None)
        if decline_all:
            decline_all("kill switch")
        self.gate.request_abort()
        return self.safe_state(by=by).splitlines()

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
            lines = []
            if report.dry_run is not None and report.status in ("done", "aborted"):
                lines.append(report.dry_run.message)
            lines.append(report.message)
            lines += self._preview(plan, report.rows)
            if report.run_id and report.rows:
                lines.append("rows: get_run_data(run_id=%r)" % report.run_id)
            return "\n".join(lines)
        return self._do("running plan", run)

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

    def list_runs(self, limit: int = 10) -> str:
        root = self.data_dir / "runs"
        dirs = sorted((d for d in root.iterdir() if d.is_dir()), reverse=True)[:limit] if root.exists() else []
        lines = []
        for d in dirs:
            try:
                meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
                lines.append("%s: %s, %s/%s points%s" % (d.name, meta.get("status"), meta.get("points"),
                                                         meta.get("planned_points"),
                                                         " (%s)" % meta["reason"] if meta.get("reason") else ""))
            except (OSError, ValueError):
                lines.append("%s: unfinished" % d.name)
        return "\n".join(lines) or "no runs yet"

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
                "instruments": self.instruments(),
                "events": [event_line(e) for e in reversed(tail_events(self.events_path, 25))]}
