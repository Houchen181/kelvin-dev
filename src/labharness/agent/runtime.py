"""What the agent can do, as short plain-text answers (feature #9: concise).

All instrument access goes through the safety gate and one lock, so an
agent call and a plan never talk to an instrument at the same time. The approval page reads cached state and never waits on the lock.
"""
from __future__ import annotations

import json
import re
import threading
import time
from contextvars import copy_context
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from ..cards import ParamSpec, num
from ..engine import RunEngine, RunReport
from ..eventlog import EventLog, RunStore
from ..gate import ApprovalRequest, Approver, SafetyGate, _dur
from ..lab import Lab
from ..labconfig import Allowed, Envelope
from .. import carddraft, datascope
from ..literature import Literature
from ..monitor import Monitor, Watch
from ..notify import Notifier
from ..plans import Plan, PlanError, load_plan
from ..scanqueue import Item, ScanQueue, load_named, load_step, parse_item, save_named, saved_queues
from ..reports import Collector, pick_runs, write_pdf, write_pptx
from ..yamlio import load_yaml

MAX_WAIT_S = 3600


def tail_events(path: Path, n: int, predicate: Optional[Callable[[Dict[str, Any]], bool]] = None) -> List[Dict[str, Any]]:
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
    for line in reversed(lines):
        if len(out) >= n:
            break
        try:
            event = json.loads(line)
            if predicate is None or predicate(event):
                out.append(event)
        except ValueError:
            pass
    return list(reversed(out))


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
    elif kind == "card":
        text = "card %s saved (%s)" % (e.get("card"), e.get("status"))
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
        self.cards_dir: Optional[Path] = None   # the user's own cards (~/Kelvin/cards); set by the app
        self.home: Optional[Path] = None        # ~/Kelvin, where each instrument has its folder; set by the app
        self.setup_status: Dict[str, Any] = {"state": "idle"}   # setting up connected instruments (autosetup.py)
        self.data_problem: Optional[str] = None   # why the chosen data folder is not in use, if it is not
        self.scan_network = True                    # scans also look on the local network (a Settings switch)
        self.last_survey: Optional[Dict[str, Any]] = None   # what the last scan looked through, and why (survey.py)
        self.last_found: List[str] = []                     # the addresses it found: where Kelvin may try a card
        self.queue = ScanQueue(self.data_dir / "queue.json")   # scans lined up to run one after another
        self._queue_thread: Optional[threading.Thread] = None
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
        if not self.lock.acquire(timeout=2.0):
            if self.queue.state in ("running", "pausing"):   # a queued scan may take hours: answer instead of waiting
                return ("Kelvin is running the queue (%s). Ask again when this item is done, or pause or stop the "
                        "queue." % ((self.busy or "a scan") if datascope.current() is None else "a scan"))
            self.lock.acquire()
        try:
            self.busy = what
            try:
                return fn()
            finally:
                self.busy = None
                self._refresh()
        finally:
            self.lock.release()

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

    # ------------------------------------------------------------------ where the data go (people only)

    DATA = ("runs", "reports", "events.jsonl")   # what a data folder holds; chats and settings stay with Kelvin

    def data_summary(self) -> Dict[str, Any]:
        runs = self.data_dir / "runs"
        return {"path": str(self.data_dir), "runs": sum(1 for d in runs.iterdir() if d.is_dir()) if runs.exists() else 0,
                "reports": sum(1 for f in (self.data_dir / "reports").glob("*") if f.is_file())
                if (self.data_dir / "reports").exists() else 0}

    def set_data_dir(self, path: Any, move: bool = False, by: str = "human") -> Dict[str, Any]:
        """Keep runs, the lab record (events.jsonl) and reports in another folder: a person's choice, from Settings.
        Refused while Kelvin is busy. With move, what is saved now goes along, but nothing there is overwritten:
        a run or report whose name is taken, or a record the new folder already has, stays where it was."""
        import shutil
        new = Path(str(path)).expanduser()
        if not new.is_absolute():
            raise ValueError("give the folder's full path, such as D:\\Data\\Kelvin or /Users/me/Data/Kelvin")
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("Kelvin is busy (%s); try again when it is idle" % (self.busy or "working"))
        try:
            if self.live and self.live.get("status") == "running":
                raise RuntimeError("a plan is running; change the data folder after it finishes")
            try:
                new.mkdir(parents=True, exist_ok=True)
                probe = new / ".kelvin-write-test"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            except OSError as e:
                raise ValueError("Kelvin cannot write to %s: %s" % (new, e.strerror or e))
            old = self.data_dir
            if new.resolve() == old.resolve():
                return dict(self.data_summary(), moved=0, kept=[])
            moved, kept = 0, []
            if move:
                for sub in ("runs", "reports"):
                    src = old / sub
                    if not src.is_dir():
                        continue
                    (new / sub).mkdir(exist_ok=True)
                    for item in sorted(src.iterdir()):
                        target = new / sub / item.name
                        if target.exists():
                            kept.append("%s/%s" % (sub, item.name))
                            continue
                        shutil.move(str(item), str(target))
                        moved += 1
                if (old / "events.jsonl").exists():
                    if (new / "events.jsonl").exists():
                        kept.append("events.jsonl")
                    else:
                        shutil.move(str(old / "events.jsonl"), str(new / "events.jsonl"))
                        moved += 1
            if not move or kept:
                self.gate.log.write("data_folder", by=by, moved_to=str(new),
                                    note="what stays here was not moved" if move else "earlier data stay here")
            self.data_dir = new
            self.queue.move_to(new / "queue.json")
            self.events_path = new / "events.jsonl"
            self.gate.log = EventLog(self.events_path, self.lab.clock)
            self.engine.store = RunStore(new / "runs")
            self.gate.log.write("data_folder", by=by, previous=str(old), moved=moved, kept=len(kept))
            return dict(self.data_summary(), moved=moved, kept=kept, previous=str(old))
        finally:
            self.lock.release()

    # ------------------------------------------------------------------ a person's terminal (people only)

    def terminal(self, instrument: str, op: str, arg: str = "", value: Any = None,
                 by: str = "human (terminal)") -> Dict[str, Any]:
        """A command a person typed in the portal's terminal. It goes through the same gate as everything Kelvin
        does (limits, interlocks, blocked commands, approvals) and is logged as theirs. op: raw (a command for the
        instrument, e.g. SCPI), get, set, describe or manual (search its manual). Refused, not queued, while Kelvin
        is busy, so a typed command never lands in the middle of a run."""
        if instrument not in self.lab.instruments:
            return {"ok": False, "action": "refused", "message": "no instrument %r (have: %s)"
                    % (instrument, ", ".join(self.lab.instruments))}
        if op == "manual":
            return {"ok": True, "action": "read", "message": self.search_manual(instrument, arg)}
        if op == "describe":
            return {"ok": True, "action": "read", "message": self.gate.describe(instrument)}
        if op not in ("raw", "get", "set"):
            return {"ok": False, "action": "refused", "message": "unknown terminal operation %r" % op}
        if not arg.strip():
            return {"ok": False, "action": "refused", "message": "nothing to send"}
        if not self.lock.acquire(blocking=False):
            return {"ok": False, "action": "busy",
                    "message": "Kelvin is busy (%s); wait for it, or stop it first" % (self.busy or "working")}
        try:
            self.busy = "terminal: %s %s" % (instrument, arg)
            with self.gate.acting_as(by):
                if op == "raw":
                    r = self.gate.raw(instrument, arg.strip())
                elif op == "get":
                    r = self.gate.get(instrument, arg.strip())
                else:
                    r = self.gate.set(instrument, arg.strip(), value)
            shown = r.value if r.value is None or isinstance(r.value, (str, int, float, bool)) else str(r.value)[:200]
            return {"ok": r.ok, "action": r.action, "message": r.message, "value": shown}
        finally:
            self.busy = None
            self._refresh()
            self.lock.release()

    def stop(self, by: str = "human") -> int:
        """A person's stop: decline open requests and stop the current ramp or plan. Drives stay where they are
        (a stopped plan still runs its own on_abort steps). Returns how many requests were declined."""
        decline_all = getattr(self.approver, "decline_all", None)
        declined = decline_all("stop button") if decline_all else 0
        with self.queue.lock:   # a stop also stops the queue: nothing else starts after the stopped scan
            if self.queue.state in ("running", "pausing"):
                self.queue.state, self.queue.reason = "pausing", "stopped by %s" % by
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
        if datascope.current() is not None:
            try:
                owner = json.loads((d / "meta.json").read_text(encoding="utf-8")).get("scope")
            except (OSError, ValueError):
                owner = None
            if not datascope.visible(owner):
                raise KeyError("no run %r in this conversation's project" % run_id)
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
        dirs = sorted((d for d in root.iterdir() if d.is_dir()), reverse=True) if root.exists() else []
        out = []
        for d in dirs:
            try:
                self._run_dir(d.name)
            except KeyError:
                continue
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
            if len(out) >= max(1, int(limit)):
                break
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

    # ------------------------------------------------------------------ the scan queue

    def queue_show(self) -> str:
        return self.queue.text()

    def queue_view(self) -> Dict[str, Any]:
        return dict(self.queue.view(), saved=saved_queues(self._queues_dir()))

    def _queues_dir(self) -> Path:
        scope = datascope.current()
        folder = self.data_dir / "queues"
        return folder / (scope.get("project") or scope["chat"]) if scope else folder

    def _queue_problems(self, text: str) -> List[str]:
        """What stops an item from running on this lab, before it joins the queue."""
        kind, _ = parse_item(text)
        if kind == "plan":
            return self.engine.validate(self._plan(text))
        step, errors = load_step(text), []
        for instr, param, value in step.sets:
            try:
                inst, spec = self.lab.resolve("%s.%s" % (instr, param))
                if spec.set_cmd is None:
                    errors.append("%s.%s is read-only" % (instr, param))
                    continue
                target = spec.coerce(value)
                lim = inst.limits(param)
                if spec.numeric and lim and not lim[0] <= target <= lim[1]:
                    errors.append("%s.%s %s is outside the limits [%s, %s]" % (instr, param, spec.show(target),
                                                                            num(lim[0]), num(lim[1])))
            except (KeyError, ValueError) as e:
                errors.append(e.args[0] if e.args else str(e))
        if step.stabilize:
            spec = self._spec(step.stabilize.key)
            if spec is None or spec.get_cmd is None:
                errors.append("%s cannot be read" % step.stabilize.key)
        return errors

    def queue_add(self, text: str, position: Optional[int] = None, note: str = "", by: str = "agent") -> str:
        try:
            problems = self._queue_problems(text)
        except PlanError as e:
            return "not added: %s" % e
        if problems:
            return "not added: " + "; ".join(problems[:5])
        item = self.queue.add(text, position, note, by)
        pos = self.queue.items.index(item) + 1
        self.gate.log.write("queue_add", by=by, item=item.id, kind=item.kind, name=item.name, position=pos)
        return "added %s %s as #%d\n%s" % (item.kind, item.name, pos, self.queue.text())

    def queue_edit(self, ref: Any, text: str, by: str = "agent") -> str:
        try:
            problems = self._queue_problems(text)
            if problems:
                return "not changed: " + "; ".join(problems[:5])
            item = self.queue.edit(ref, text)
        except (PlanError, KeyError, ValueError) as e:
            return "not changed: %s" % (e.args[0] if e.args else e)
        self.gate.log.write("queue_edit", by=by, item=item.id, name=item.name)
        return "changed %s\n%s" % (item.name, self.queue.text())

    def queue_change(self, op: str, ref: Any = None, position: Optional[int] = None, by: str = "agent") -> str:
        """move, remove, again (put a finished item back in line), clear (remove finished items)."""
        try:
            if op == "move":
                item = self.queue.move(ref, int(position or 1))
            elif op == "remove":
                item = self.queue.remove(ref)
            elif op == "again":
                item = self.queue.again(ref)
            elif op == "clear":
                n = self.queue.clear_finished()
                self.gate.log.write("queue_clear", by=by, removed=n)
                return "removed %d finished items\n%s" % (n, self.queue.text())
            else:
                return "unknown queue change %r" % op
        except (KeyError, ValueError, TypeError) as e:
            return "not changed: %s" % (e.args[0] if e.args else e)
        self.gate.log.write("queue_" + op, by=by, item=item.id, name=item.name)
        return self.queue.text()

    def queue_set_on_error(self, value: str, by: str = "human") -> None:
        if value not in ("pause", "continue"):
            raise ValueError("on_error is pause or continue")
        with self.queue.lock:
            self.queue.on_error = value
            self.queue.save()
        self.gate.log.write("queue_on_error", by=by, value=value)

    def queue_save(self, name: str) -> str:
        try:
            path = save_named(self._queues_dir(), name, self.queue.waiting() or self.queue.visible_items())
        except (ValueError, OSError) as e:
            return "not saved: %s" % e
        return "saved the queue as %s" % path.name

    def queue_load(self, name: str, by: str = "agent") -> str:
        try:
            entries = load_named(self._queues_dir(), name)
        except KeyError as e:
            return e.args[0]
        added, problems = 0, []
        for text, note in entries:
            out = self.queue_add(text, None, note, by)
            if out.startswith("not added"):
                problems.append(out)
            else:
                added += 1
        return "\n".join(["added %d items from %s" % (added, name)] + problems + [self.queue.text()])

    def _queue_drives(self, items: List[Item]) -> Dict[str, List[Any]]:
        out: Dict[str, List[Any]] = {}
        for item in items:
            if item.kind == "plan":
                values = self.engine.drive_values(self._plan(item.text))
            else:
                values = {}
                for instr, param, value in load_step(item.text).sets:
                    key = "%s.%s" % (instr, param)
                    spec = self.lab.resolve(key)[1]
                    if spec.is_drive:
                        values.setdefault(key, []).append(spec.coerce(value))
            for key, vals in values.items():
                out.setdefault(key, []).extend(vals)
        return out

    def _rehearse(self, items: List[Item]) -> Tuple[float, List[str]]:
        """The whole queue, in order, on a simulated copy of the lab: (lab seconds it takes, what went wrong)."""
        from ..gate import SimulationApprover
        twin = self.lab.make_twin()
        gate = SafetyGate(twin, SimulationApprover(), EventLog(None, twin.clock), envelope=self.gate.envelope,
                          operator="dry-run")
        engine = RunEngine(gate, RunStore(None))
        engine.analyze = False
        t0, problems, warnings = twin.clock.now(), [], []
        for n, item in enumerate(items, 1):
            if item.kind == "plan":
                r = engine.run(self._plan(item.text), dry_run=False)
                if r.status != "done":
                    problems.append("#%d %s: %s" % (n, item.name, r.message))
                continue
            step = load_step(item.text)
            for instr, param, value in step.sets:
                r = gate._set(instr, param, value, True, "dry-run")
                if not r.ok:
                    problems.append("#%d %s: %s" % (n, item.name, r.message))
            if step.stabilize:
                engine._stabilize(step.stabilize, gate.stop_generation, warnings)
            twin.clock.sleep(step.wait)
        return twin.clock.now() - t0, problems

    def queue_start(self, by: str = "agent") -> str:
        """Check and dry-run every waiting item in order, ask a person once for every drive range, then run the
        queue in the background. Returns at once after the approval; queue_show follows it."""
        with self.queue.lock:
            if self.queue.state in ("running", "pausing"):
                return "the queue is already running\n" + self.queue.text()
        items = self.queue.waiting()
        if not items:
            return "nothing is waiting in the queue"

        def prepare() -> str:
            problems = []
            for n, item in enumerate(items, 1):
                try:
                    problems += ["#%d %s: %s" % (n, item.name, p) for p in self._queue_problems(item.text)]
                except PlanError as e:
                    problems.append("#%d %s: %s" % (n, item.name, e))
            if problems:
                return "the queue did not start:\n" + "\n".join(problems[:8])
            lines, grants, refusals = self.gate.plan_approval(self._queue_drives(items))
            if refusals:
                return "the queue did not start; these would be refused:\n" + "\n".join(refusals[:8])
            duration, failed = self._rehearse(items)
            if failed:
                return "the queue did not start; its dry run failed:\n" + "\n".join(failed[:8])
            scans = sum(1 for i in items if i.kind == "plan")
            steps = len(items) - scans
            title = "Run the queue: %d scan%s%s, ~%s" % (scans, "" if scans == 1 else "s", " and %d step%s" % (
                steps, "" if steps == 1 else "s") if steps else "", _dur(duration))
            if lines:
                request = ApprovalRequest(title, lines + [
                    "This approval covers every item now waiting. Each scan is still dry-run before it runs, and "
                    "anything outside these ranges asks again."])
                if not self.gate.ask(request):
                    note = " (%s)" % request.note if request.note else ""
                    self.gate.log.write("queue_declined", by=by)
                    return "declined: the queue did not start%s" % note
            with self.queue.lock:
                self.queue.state, self.queue.reason = "running", ""
                self.queue.save()
            self.gate.log.write("queue_start", by=by, items=len(items), expected_s=round(duration))
            context = copy_context()
            self._queue_thread = threading.Thread(target=context.run, args=(self._queue_loop, grants, by), daemon=True,
                                                  name="kelvin-queue")
            self._queue_thread.start()
            return "queue started: %s\n%s" % (title[len("Run the queue: "):], self.queue.text())
        return self._do("starting the queue", prepare)

    def queue_pause(self, by: str = "agent") -> str:
        """Finish the item that is running, then stop there. Stop (the Stop button) also ends the running scan."""
        with self.queue.lock:
            if self.queue.state != "running":
                return "the queue is not running\n" + self.queue.text()
            self.queue.state, self.queue.reason = "pausing", "paused by %s" % by
            self.queue.save()
        self.gate.log.write("queue_pause", by=by)
        return "the queue pauses after the item that is running"

    def _queue_loop(self, grants: Dict[str, Allowed], by: str) -> None:
        q = self.queue
        while True:
            with q.lock:
                item = q.next_waiting() if q.state == "running" else None
                if item is None:
                    finished = q.state == "running"
                    q.state = "idle" if finished else "paused"
                    if finished:
                        q.reason = "finished"
                    q.save()
                    break
                q.mark(item, "running")
                position = q.items.index(item) + 1
            ok, message = self._queue_item(item, position, grants, by)
            with q.lock:
                if not ok and q.state == "running" and q.on_error == "pause":
                    q.state, q.reason = "pausing", "%s: %s" % (item.name, message)
                    q.save()
        done = sum(1 for i in q.items if i.status == "done")
        self.gate.log.write("queue_end", by=by, state=q.state, reason=q.reason, done=done)
        if q.state == "idle":
            self.notifier("run", "Queue finished", "%d items done" % done)
        else:
            self.notifier("run", "Queue paused", q.reason[:300])

    def _queue_item(self, item: Item, position: int, grants: Dict[str, Allowed], by: str) -> Tuple[bool, str]:
        with datascope.use(item.scope):
            return self._scoped_queue_item(item, position, grants, by)

    def _scoped_queue_item(self, item: Item, position: int, grants: Dict[str, Allowed], by: str) -> Tuple[bool, str]:
        """Run one item under the queue's approval. (ok, message)"""
        q = self.queue
        with self.lock:
            self.busy = "queue %d/%d: %s" % (position, len(q.items), item.name)
            try:
                with self.gate.standing(grants), self.gate.acting_as("queue (started by %s)" % by):
                    if item.kind == "plan":
                        report = self.engine.run(self._plan(item.text), meta={"queue": {
                            "item": item.id, "position": position, "note": item.note}})
                        status = {"done": "done", "aborted": "stopped"}.get(report.status, "failed")
                        if report.status == "aborted" and "stop" not in (report.message or "").lower():
                            status = "failed"
                        q.mark(item, status, report.message if status != "done" else "", report.run_id)
                        return status == "done", report.message
                    return self._queue_step(item)
            except Exception as e:   # an instrument that stopped answering, a plan that no longer parses...
                q.mark(item, "failed", str(e)[:300])
                return False, str(e)
            finally:
                self.busy = None
                self._refresh()

    def _queue_step(self, item: Item) -> Tuple[bool, str]:
        from ..engine import _Abort
        step, q = load_step(item.text), self.queue
        gen = self.gate.stop_generation
        for instr, param, value in step.sets:
            r = self.gate._set(instr, param, value, True, "queue:" + item.name)
            if not r.ok:
                q.mark(item, "failed", r.message)
                return False, r.message
        warnings: List[str] = []
        try:
            if step.stabilize:
                self.engine._stabilize(step.stabilize, gen, warnings)
            end = self.lab.clock.now() + step.wait
            while self.lab.clock.now() < end:
                if self.gate.stop_generation != gen:
                    raise _Abort("stop requested")
                self.lab.clock.sleep(min(1.0, end - self.lab.clock.now()))
        except _Abort:
            q.mark(item, "stopped", "stopped while waiting")
            return False, "stopped"
        q.mark(item, "done", "; ".join(warnings))
        return True, ""

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
        events = tail_events(self.events_path, max(1, min(int(n), 200)),
                             lambda e: datascope.visible(e.get("scope")))
        return "\n".join(event_line(e) for e in events) or "no events yet"

    def _scoped_events(self) -> List[Dict[str, Any]]:
        return [e for e in self.gate.log.events() if datascope.visible(e.get("scope"))]

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
        """Connected instruments as data, each with a suggested name. Raises AdapterError without VISA.
        What the scan looked through, and why it found what it did, goes to last_survey."""
        from ..discovery import scan_survey
        from ..survey import reason
        in_lab = {ic.address: name for name, ic in self.lab.config.instruments.items()
                  if not ic.address.startswith("sim://")}
        with self.lock:
            self.busy = "scanning for instruments"
            try:
                found, looked = scan_survey(self.lab.registry, in_lab=in_lab, network=self.scan_network)
            finally:
                self.busy = None
        self.last_survey = dict(looked.as_dict(), reason=reason(found, looked), at=time.time())
        self.last_found = [f.address for f in found]
        taken = [n for n in self.lab.config.instruments if not self.lab.config.instruments[n].address.startswith("sim://")]
        out = []
        for f in found:
            name = f.suggest_name(taken) if f.card_id and not f.in_lab else None
            if name:
                taken.append(name)
            out.append(dict(vars(f), suggested=name))
        return out

    # ------------------------------------------------------------------ instrument folders and manuals

    def _real(self, instrument: str) -> Any:
        inst = self.lab.instruments.get(instrument)
        if inst is None:
            raise KeyError("no instrument %r (have: %s)" % (instrument, ", ".join(self.lab.instruments)))
        return inst

    def instrument_info(self, instrument: str) -> str:
        """An instrument's folder: who it is, how Kelvin talks to it, its settings at setup and its manual."""
        from .. import profiles
        try:
            inst = self._real(instrument)
        except KeyError as e:
            return e.args[0]
        if inst.simulated:
            return ("%s is simulated (%s): it has no folder or manual; describe_instrument lists its parameters"
                    % (instrument, inst.card.model))
        if self.home is None:
            return "instrument folders are not kept in this Kelvin"
        p = profiles.info(self.home, instrument)
        if not p.get("card"):
            return ("%s has no folder yet; Kelvin makes it when it connects (a person can press Set up again in Lab "
                    "setup)" % instrument)
        lines = ["%s: %s (%s), card %s" % (instrument, p.get("model"), p.get("kind"), p.get("card"))]
        parsed = p.get("parsed") or {}
        if p.get("identity"):
            lines.append("identity: %s%s" % (p["identity"], " (%s)" % ", ".join("%s %s" % kv for kv in parsed.items())
                                             if parsed else ""))
        conn = ", ".join("%s %r" % kv for kv in (p.get("connection") or {}).items())
        lines.append("address: %s%s%s" % (p.get("address"), "; connection: " + conn if conn else "",
                                          "; answered with " + p["answered_with"] if p.get("answered_with") else ""))
        lines.append("folder: %s" % p["folder"])
        m = p.get("manual") or {}
        if p.get("has_manual"):
            lines.append("manual: %s, %s pages%s; search it with search_manual" % (
                m.get("title") or p.get("card_manual") or "manual", m.get("pages", "?"),
                " (from %s)" % m["url"] if m.get("url") else " (put there by hand)"))
        else:
            lines.append("manual: none yet (%s). A person can fetch it in Lab setup or put the PDF in the folder as "
                         "manual.pdf" % (p.get("card_manual") or "the card names no manual"))
        st = profiles.settings_of(self.home, instrument)
        if st.get("settings"):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.get("read_at") or 0))
            vals = ", ".join("%s = %s" % (k, v.get("shown", v.get("value"))) for k, v in st["settings"].items())
            lines.append("settings read at %s (get_value for the current value): %s" % (when, vals))
        return "\n".join(lines)

    def search_manual(self, instrument: str, query: str, max_passages: int = 4) -> str:
        from .. import profiles
        try:
            inst = self._real(instrument)
        except KeyError as e:
            return e.args[0]
        if inst.simulated or self.home is None:
            return "%s is simulated: it has no manual; describe_instrument lists its parameters" % instrument
        try:
            hits = profiles.search_manual(self.home, instrument, query, max(1, min(int(max_passages), 8)))
        except FileNotFoundError:
            return ("no manual for %s yet: a person can fetch it in Lab setup, or put the maker's PDF in %s as "
                    "manual.pdf" % (instrument, profiles.folder(self.home, instrument)))
        except (ValueError, RuntimeError) as e:
            return str(e)
        if not hits:
            return "nothing in the %s manual matches %r; try other words (the manual's own terms)" % (instrument, query)
        return "\n\n".join("[%s manual, p. %d]\n%s" % (instrument, h["page"], h["text"]) for h in hits)

    # ------------------------------------------------------------------ pulse sequences (spin experiments)

    def pulse_sequence(self, protocol: str, times: Dict[str, Any], generator: str = "") -> str:
        """The timing a pulse generator would play, checked: the diagram, period, duty cycles and any problem.
        Missing times come from the generator's current settings (if named), else from pulses.DEFAULTS."""
        from .. import pulses
        current: Dict[str, Any] = {}
        inst = self.lab.instruments.get(generator) if generator else None
        if generator and inst is None:
            return "no instrument %r (have: %s)" % (generator, ", ".join(self.lab.instruments))
        if inst is not None:
            for k in ("t_pi", "tau", "t_laser", "t_gate", "t_wait"):
                if k in inst.card.params:
                    try:
                        current[k] = inst.read(k)
                    except Exception:
                        pass
        given = {k: v for k, v in (times or {}).items() if v is not None}
        try:
            seq = pulses.build(protocol, dict(current, **given))
        except (ValueError, TypeError) as e:
            return str(e)
        problems = pulses.check(seq)
        return pulses.describe(seq) + ("\nproblems: " + "; ".join(problems) if problems else "\nchecks: ok")

    # ------------------------------------------------------------------ cards for new instruments

    def check_card(self, text: str) -> str:
        card, problems, warnings = carddraft.check(text)
        if card is None:
            return "not valid:\n" + "\n".join("- " + p for p in problems)
        drives = [p.name for p in card.params.values() if p.is_drive]
        lines = ["valid: %s (%s), %d parameters; drives: %s" % (card.id, card.model, len(card.params),
                                                              ", ".join(drives) or "none")]
        return "\n".join(lines + ["warning: " + w for w in warnings])

    def read_examples(self, path: str) -> str:
        try:
            return carddraft.read_sources(path)
        except (OSError, ValueError) as e:
            return "not read: %s" % e

    def try_card(self, text: str, instrument: str) -> str:
        """Kelvin's own try: only on an instrument a person put in the lab, or one the last scan found."""
        from .. import cardtry
        from ..adapters import AdapterError
        card, problems, _ = carddraft.check(text)
        if card is None:
            return "not valid:\n" + "\n".join("- " + p for p in problems)
        targets = cardtry.addresses_for_agent(self.lab, self.last_found)
        address = targets.get(instrument)
        if address is None:
            return ("Kelvin tries a card only on an instrument in the lab or one the last scan found (%s). A person can "
                    "try it on any address in Lab, Cards." % (", ".join(sorted(targets)) or "none yet: scan first"))

        def run() -> str:
            try:
                result = cardtry.try_card(card, address, self.lab)
            except (AdapterError, OSError, ValueError) as e:
                return "could not try it: %s" % e
            self.gate.log.write("card_try", by="agent", card=card.id, address=address,
                                answered=sum(1 for r in result["readings"] if "value" in r))
            return cardtry.summary(result)
        return self._do("trying card %s" % card.id, run)

    def save_card(self, text: str, by: str = "agent") -> str:
        if self.cards_dir is None:
            return "not saved: this Kelvin has no folder for your cards"
        from ..virtual_lab import builtin_ids
        try:
            card, path, warnings = carddraft.save(self.cards_dir, text, builtin_ids(), by)
        except ValueError as e:
            return "not saved: %s" % e
        self.lab.registry.put(card)
        self.gate.log.write("card", by=by, card=card.id, status=card.status, file=path.name)
        return "\n".join(["saved %s card %s as %s. A person adds it to the lab (Lab, Set up) and tries its readings; "
                          "its drives stay simulation-only on real hardware until they mark it reviewed (Lab, Cards)."
                          % (card.status, card.id, path)] + ["warning: " + w for w in warnings])

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
        if datascope.current() is None:
            return pick_runs(self.data_dir / "runs", runs, day, last)
        if runs:
            for run_id in runs:
                self._run_dir(run_id)  # explicit requests must not bypass ownership checks
        ids = pick_runs(self.data_dir / "runs", runs, day, 10 ** 9 if last and not (runs or day) else 0)
        visible = []
        for run_id in ids:
            try:
                self._run_dir(run_id)
            except KeyError:
                continue
            visible.append(run_id)
        return visible[-last:] if last and not (runs or day) else visible

    def report(self, run_ids: List[str], title: str = "", summary: str = "", fmt: str = "pdf",
               notes: Any = (), by: str = "human", references: Any = ()) -> List[Dict[str, Any]]:
        """Write a report of these runs (oldest first) as PDF and/or PowerPoint into reports/. Reads only files:
        it never talks to an instrument, so it can run while a plan runs."""
        if not run_ids:
            raise ValueError("no runs to report")
        for run_id in run_ids:
            self._run_dir(run_id)
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
        report = Collector(self.data_dir / "runs", self.lab, columns, self._scoped_events, event_line).report(
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
            from ..discovery import scan_survey
            from ..survey import reason
            in_lab = {ic.address: name for name, ic in self.lab.config.instruments.items()
                      if not ic.address.startswith("sim://")}
            try:
                found, looked = scan_survey(self.lab.registry, in_lab=in_lab, network=self.scan_network)
            except AdapterError as e:
                return str(e)
            self.last_survey = dict(looked.as_dict(), reason=reason(found, looked), at=time.time())
            self.last_found = [f.address for f in found]
            if not found:
                return "\n".join([reason(found, looked)] + looked.lines())
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
                "busy": self.busy, "pending": pending, "history": history, "status": status, "queue": self.queue_view(),
                "approval_mode": "auto" if self.gate.auto else "ask", "approval_rule": self.approval_mode()["rule"],
                "lab_info": self.lab_info,
                "observe_only": self.lab.config.observe_only, "setup": self.setup_status, "survey": self.last_survey,
                "watches": self.monitor.list(),
                "alarms": [a.view() for a in self.monitor.alarms[:10]],
                "notices": list(self.notifier.notices)[-20:],
                "instruments": self.instruments(),
                "live": {k: v for k, v in (self.live_view(10 ** 9) or {}).items()
                         if k in ("run_id", "plan", "done", "total", "eta_s", "status")} or None,
                "events": [event_line(e) for e in reversed(tail_events(self.events_path, 25))]}
