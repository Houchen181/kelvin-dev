"""The run engine: validate → preflight → dry run on twins → one approval → execute → log.

Every set goes through the safety gate, so limits, ramps, guards and
interlocks apply inside plans exactly as they do for direct commands.
"""
from __future__ import annotations

import itertools
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ._version import __version__
from .analysis import analyze_run
from .eventlog import EventLog, RunStore
from .gate import ApprovalRequest, SafetyGate, SimulationApprover, _dur
from .plans import Plan, PlanError, Stabilize, Step, load_plan


class _Abort(Exception):
    pass


@dataclass
class RunReport:
    status: str                     # done | aborted | refused | declined
    plan: str
    message: str
    run_id: Optional[str] = None
    points: int = 0
    duration_s: float = 0.0
    rows: List[Dict[str, Any]] = field(default_factory=list)
    path: Optional[str] = None
    would_ask: List[str] = field(default_factory=list)
    dry_run: Optional["RunReport"] = None
    analysis: Optional[Dict[str, Any]] = None
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "done"

    def __str__(self) -> str:
        return self.message


class RunEngine:
    def __init__(self, gate: SafetyGate, store: Optional[RunStore] = None):
        self.gate = gate
        self.lab = gate.lab
        self.store = store or RunStore(None)
        # called with {"event": "start" | "point" | "end", ...} so a UI can follow a run live
        self.progress: Optional[Callable[[Dict[str, Any]], None]] = None
        self.analyze = True

    def _emit(self, **event: Any) -> None:
        if self.progress is not None:
            try:
                self.progress(event)
            except Exception:
                pass  # a broken display must never stop an experiment

    def columns(self, plan: Plan) -> Dict[str, List[Dict[str, str]]]:
        """Axis and measured columns with units, as analysis and plots need them."""
        def unit(key: str) -> str:
            try:
                return self.lab.resolve(key)[1].unit
            except KeyError:
                return ""
        axes = [{"key": a.key, "unit": "s" if a.kind == "time" else ("" if a.kind == "repeat" else unit(a.key)),
                 "kind": a.kind} for a in plan.axes]
        measures = []
        for instr, param in plan.measure:
            key = "%s.%s" % (instr, param)
            spec = self.lab.resolve(key)[1]
            m = {"key": key, "unit": spec.unit, "kind": "trace" if spec.type == "trace" else "scalar"}
            if spec.axis:
                m["x_unit"] = unit("%s.%s" % (instr, spec.axis[0]))
            measures.append(m)
        return {"axes": axes, "measures": measures}

    # ------------------------------------------------------------------ checks

    def validate(self, plan: Plan) -> List[str]:
        """Static checks; also converts values to each parameter's type."""
        errors: List[str] = []
        lab = self.lab
        for name in plan.requires:
            if name not in lab.instruments:
                errors.append("unknown instrument %r" % name)

        def settable(instr: str, param: str, where: str):
            try:
                inst, spec = lab.resolve("%s.%s" % (instr, param))
            except KeyError as e:
                errors.append("%s: %s" % (where, e.args[0]))
                return None, None
            if spec.set_cmd is None:
                errors.append("%s: %s.%s is read-only" % (where, instr, param))
                return None, None
            return inst, spec

        def check_value(inst, spec, value, where):
            try:
                v = spec.coerce(value)
            except ValueError as e:
                errors.append("%s: %s" % (where, e))
                return value
            problem = self.gate._limit_problem(inst, spec, v)
            if problem:
                errors.append("%s: %s" % (where, problem))
            return v

        for group, steps in (("setup", plan.setup), ("after", plan.after), ("on_abort", plan.on_abort or [])):
            for step in steps:
                inst, spec = settable(step.instr, step.param, group)
                if spec:
                    step.value = check_value(inst, spec, step.value, group)
        for axis in plan.instrument_axes:
            inst, spec = settable(axis.instr, axis.param, "sweep")
            if spec:
                before = len(errors)
                axis.values = [check_value(inst, spec, v, "sweep") for v in axis.values]
                del errors[before + 1:]  # one message per axis is enough
        reads = plan.measure + [(c.instr, c.param) for c in plan.preconditions] \
            + [(a.stabilize.instr, a.stabilize.param) for a in plan.axes if a.stabilize]
        for instr, param in reads:
            try:
                _, spec = lab.resolve("%s.%s" % (instr, param))
                if spec.get_cmd is None:
                    errors.append("%s.%s cannot be read" % (instr, param))
            except KeyError as e:
                errors.append(e.args[0])
        return errors

    def drive_values(self, plan: Plan) -> Dict[str, List[Any]]:
        out: Dict[str, List[Any]] = {}
        for key, value in [(s.key, s.value) for s in plan.setup] \
                + [(a.key, v) for a in plan.instrument_axes for v in a.values] + [(s.key, s.value) for s in plan.after]:
            _, spec = self.lab.resolve(key)
            if spec.is_drive:
                out.setdefault(key, []).append(value)
        return out

    def preflight(self, names: Optional[List[str]] = None):
        results = self.lab.health_check(names)
        self.gate.log.write("preflight", by=self.gate.operator, ok=all(r.ok for r in results),
                            results=[str(r) for r in results])
        return results

    # ------------------------------------------------------------------ dry run and run

    def simulate(self, plan: Any) -> RunReport:
        """Run the plan on a simulated copy of the lab. The real lab is untouched."""
        plan = plan if isinstance(plan, Plan) else load_plan(plan)
        twin = self.lab.make_twin()
        approver = SimulationApprover()
        gate = SafetyGate(twin, approver, EventLog(None, twin.clock), envelope=self.gate.envelope, operator="dry-run")
        engine = RunEngine(gate, RunStore(None))
        engine.analyze = False  # the twin's numbers are not the sample's
        report = engine.run(plan, dry_run=False)
        report.would_ask = [str(r) for r in approver.requests]
        report.message = "dry run: " + report.message
        return report

    def run(self, plan: Any, dry_run: bool = True) -> RunReport:
        try:
            plan = plan if isinstance(plan, Plan) else load_plan(plan)
        except PlanError as e:
            return self._refuse("?", "plan invalid: %s" % e)
        errors = self.validate(plan)
        if errors:
            return self._refuse(plan.name, "plan invalid: " + "; ".join(errors[:5]))
        bad = [r for r in self.preflight(plan.requires) if not r.ok]
        if bad:
            return self._refuse(plan.name, "preflight failed: " + "; ".join(str(r) for r in bad))
        for cond in plan.preconditions:
            ok, actual = cond.check(self.lab.read)
            if not ok:
                _, spec = self.lab.resolve(cond.key)
                return self._refuse(plan.name, "precondition %s not met (now %s)" % (cond.text, spec.show(actual)))
        sim = None
        if dry_run:
            sim = self.simulate(plan)
            if sim.status == "refused":
                return self._refuse(plan.name, "refused by %s" % sim.message, sim)
        items, grants, problems = self.gate.plan_approval(self.drive_values(plan))
        if problems:
            return self._refuse(plan.name, "; ".join(problems), sim)
        if items:
            title = "Run plan %s: %d points" % (plan.name, plan.points)
            if sim is not None:
                title += ", ~%s" % _dur(sim.duration_s) if sim.ok else "; %s" % sim.message
            request = ApprovalRequest(title, items)
            if not self.gate.ask(request):
                why = " (%s)" % request.note if request.note else ""
                report = RunReport("declined", plan.name, "declined: plan %s not run%s" % (plan.name, why), dry_run=sim)
                self.gate.log.write("run_declined", by=self.gate.operator, plan=plan.name)
                return report
        report = self._execute(plan, grants, sim.duration_s if sim is not None and sim.ok else None)
        report.dry_run = sim
        return report

    def _refuse(self, name: str, why: str, sim: Optional[RunReport] = None) -> RunReport:
        self.gate.log.write("run_refused", by=self.gate.operator, plan=name, reason=why)
        return RunReport("refused", name, "refused: %s" % why, dry_run=sim)

    def _stabilize(self, st: Stabilize, gen: int, warnings: List[str]) -> None:
        """Wait until st's reading moves by less than st.within over st.duration (or give up at st.timeout)."""
        clock, lab = self.lab.clock, self.lab
        t0 = clock.now()
        window: deque = deque()
        spec = lab.resolve(st.key)[1]
        while True:
            now, value = clock.now(), lab.read(st.instr, st.param)
            window.append((now, value))
            while window and now - window[0][0] > st.duration:
                window.popleft()
            values = [v for _, v in window]
            if now - t0 >= st.duration and max(values) - min(values) <= st.within:
                return
            if now - t0 > st.timeout:
                warnings.append("%s did not settle within %s in %s (last swing %s); measured anyway"
                                % (st.key, spec.show(st.within), _dur(st.timeout), spec.show(max(values) - min(values))))
                return
            if self.gate.stop_generation != gen:
                raise _Abort("stop requested")
            clock.sleep(max(0.05, min(2.0, st.duration / 10)))

    def _execute(self, plan: Plan, grants: Dict[str, Any], expected_s: Optional[float] = None) -> RunReport:
        """expected_s: the dry run's duration (lab time), for an honest time-left estimate that counts ramps."""
        gate, lab, clock = self.gate, self.lab, self.lab.clock
        gen = gate.stop_generation
        rec = self.store.create(plan.name, plan.source, gate.operator)
        rec.snapshot("start", lab.snapshot())
        gate.log.write("run_start", by=gate.operator, run=rec.run_id, plan=plan.name, points=plan.points)
        cols = self.columns(plan)
        self._emit(event="start", run_id=rec.run_id, plan=plan.name, total=plan.points, **cols)
        t0, wall0 = clock.now(), time.time()
        status, reason, n = "done", None, 0
        warnings: List[str] = []

        def apply(instr: str, param: str, value: Any) -> None:
            r = gate._set(instr, param, value, True, "plan:" + plan.name)
            if not r.ok:
                raise _Abort(r.message)

        def time_left(done: int) -> float:
            wall, lab_t = time.time() - wall0, clock.now() - t0
            if expected_s and lab_t > 0:
                return max(0.0, expected_s - lab_t) * wall / lab_t
            return wall / done * (plan.points - done) if done else 0.0

        try:
            with gate.preapproved(grants):
                for step in plan.setup:
                    apply(step.instr, step.param, step.value)
                prev: List[Any] = [None] * len(plan.axes)
                for combo in itertools.product(*[a.values for a in plan.axes]):
                    for j, (axis, v) in enumerate(zip(plan.axes, combo)):
                        if prev[j] is not None and v == prev[j] and all(p == c for p, c in zip(prev[:j], combo[:j])):
                            continue
                        if axis.kind == "instrument":
                            apply(axis.instr, axis.param, v)
                            if axis.stabilize:
                                self._stabilize(axis.stabilize, gen, warnings)
                            clock.sleep(axis.settle if axis.settle is not None else plan.settle)
                        elif axis.kind == "time" and v:
                            clock.sleep(axis.every)
                    prev = list(combo)
                    row: Dict[str, Any] = {"i": n, "t": round(clock.now() - t0, 6)}
                    for axis, v in zip(plan.axes, combo):
                        row[axis.key] = v
                    for instr, param in plan.measure:
                        key = "%s.%s" % (instr, param)
                        row[key] = lab.read(instr, param)
                        trace_axis = lab.instrument(instr).spec(param).axis
                        if trace_axis:  # record the trace's x-axis as it was when measured
                            row[key + ".axis"] = [lab.read(instr, trace_axis[0]), lab.read(instr, trace_axis[1])]
                    rec.add(row)
                    n += 1
                    self._emit(event="point", run_id=rec.run_id, done=n, total=plan.points, row=row, eta_s=time_left(n))
                    if gate.stop_generation != gen:
                        raise _Abort("stop requested")
                if plan.after:
                    self._emit(event="phase", run_id=rec.run_id, phase="after", eta_s=time_left(n))
                for step in plan.after:
                    apply(step.instr, step.param, step.value)
        except _Abort as e:
            status, reason = "aborted", str(e)
        except Exception as e:
            status, reason = "aborted", "error: %s" % e
        cleanup: List[str] = []
        if status == "aborted":
            self._emit(event="phase", run_id=rec.run_id, phase="stopping", eta_s=None)
            cleanup = self._on_abort(plan)
        duration = clock.now() - t0
        try:
            rec.snapshot("end", lab.snapshot())
        except Exception:
            pass
        meta = dict(status=status, reason=reason, points=n, planned_points=plan.points,
                    duration_s=round(duration, 3), cleanup=cleanup, warnings=warnings, labharness=__version__)
        analysis = None
        if self.analyze and rec.rows:
            try:
                analysis = analyze_run(rec.rows, cols["axes"], cols["measures"], meta)
                rec.save_json("analysis.json", analysis)
            except Exception as e:  # analysis is a convenience; the data are already safe on disk
                warnings.append("automatic analysis failed: %s" % e)
        rec.finish(**meta)
        gate.log.write("run_end", by=gate.operator, run=rec.run_id, status=status, reason=reason, points=n,
                       duration_s=round(duration, 3))
        self._emit(event="end", run_id=rec.run_id, status=status, done=n, total=plan.points)
        if status == "done":
            msg = "run %s done: %d points, %s" % (rec.run_id, n, _dur(duration))
        else:
            msg = "run %s aborted after %d/%d points: %s" % (rec.run_id, n, plan.points, reason)
            if cleanup:
                msg += "; then " + "; ".join(cleanup)
        return RunReport(status, plan.name, msg, rec.run_id, n, duration, rec.rows,
                         str(rec.path) if rec.path else None, analysis=analysis, warnings=warnings)

    def _on_abort(self, plan: Plan) -> List[str]:
        steps = plan.on_abort
        if steps is None:  # default: every drive the plan touched goes to safe, unless the lab says hold
            seen: Dict[str, Step] = {}
            for key in self.drive_values(plan):
                _, spec = self.lab.resolve(key)
                if self.lab.config.safe_actions.get(key) != "hold":
                    instr, _, param = key.partition(".")
                    seen[key] = Step(instr, param, spec.safe)
            steps = list(seen.values())
        out = []
        for step in steps:
            r = self.gate._set(step.instr, step.param, step.value, True, "abort:" + plan.name)
            if r.action != "no-change":
                out.append(r.message)
        return out
