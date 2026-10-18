"""The run engine: validate → preflight → dry run on twins → one approval → execute → log.

Every set goes through the safety gate, so limits, ramps, guards and
interlocks apply inside plans exactly as they do for direct commands.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ._version import __version__
from .eventlog import EventLog, RunStore
from .gate import ApprovalRequest, SafetyGate, SimulationApprover, _dur
from .plans import Plan, PlanError, Step, load_plan


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
        for axis in plan.axes:
            inst, spec = settable(axis.instr, axis.param, "sweep")
            if spec:
                before = len(errors)
                axis.values = [check_value(inst, spec, v, "sweep") for v in axis.values]
                del errors[before + 1:]  # one message per axis is enough
        for instr, param in plan.measure + [(c.instr, c.param) for c in plan.preconditions]:
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
                + [(a.key, v) for a in plan.axes for v in a.values] + [(s.key, s.value) for s in plan.after]:
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
        report = RunEngine(gate, RunStore(None)).run(plan, dry_run=False)
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
        report = self._execute(plan, grants)
        report.dry_run = sim
        return report

    def _refuse(self, name: str, why: str, sim: Optional[RunReport] = None) -> RunReport:
        self.gate.log.write("run_refused", by=self.gate.operator, plan=name, reason=why)
        return RunReport("refused", name, "refused: %s" % why, dry_run=sim)

    def _execute(self, plan: Plan, grants: Dict[str, Any]) -> RunReport:
        gate, lab, clock = self.gate, self.lab, self.lab.clock
        gen = gate.stop_generation
        rec = self.store.create(plan.name, plan.source, gate.operator)
        rec.snapshot("start", lab.snapshot())
        gate.log.write("run_start", by=gate.operator, run=rec.run_id, plan=plan.name, points=plan.points)
        t0 = clock.now()
        status, reason, n = "done", None, 0

        def apply(instr: str, param: str, value: Any) -> None:
            r = gate._set(instr, param, value, True, "plan:" + plan.name)
            if not r.ok:
                raise _Abort(r.message)

        try:
            with gate.preapproved(grants):
                for step in plan.setup:
                    apply(step.instr, step.param, step.value)
                prev: List[Any] = [None] * len(plan.axes)
                for combo in itertools.product(*[a.values for a in plan.axes]):
                    for j, (axis, v) in enumerate(zip(plan.axes, combo)):
                        if prev[j] is not None and v == prev[j]:
                            continue
                        apply(axis.instr, axis.param, v)
                        clock.sleep(axis.settle if axis.settle is not None else plan.settle)
                    prev = list(combo)
                    row: Dict[str, Any] = {"i": n, "t": round(clock.now() - t0, 6)}
                    for axis, v in zip(plan.axes, combo):
                        row[axis.key] = v
                    for instr, param in plan.measure:
                        key = "%s.%s" % (instr, param)
                        row[key] = lab.read(instr, param)
                        axis = lab.instrument(instr).spec(param).axis
                        if axis:  # record the trace's x-axis as it was when measured
                            row[key + ".axis"] = [lab.read(instr, axis[0]), lab.read(instr, axis[1])]
                    rec.add(row)
                    n += 1
                    if gate.stop_generation != gen:
                        raise _Abort("stop requested")
                for step in plan.after:
                    apply(step.instr, step.param, step.value)
        except _Abort as e:
            status, reason = "aborted", str(e)
        except Exception as e:
            status, reason = "aborted", "error: %s" % e
        cleanup: List[str] = []
        if status == "aborted":
            cleanup = self._on_abort(plan)
        duration = clock.now() - t0
        try:
            rec.snapshot("end", lab.snapshot())
        except Exception:
            pass
        rec.finish(status=status, reason=reason, points=n, planned_points=plan.points,
                   duration_s=round(duration, 3), cleanup=cleanup, labharness=__version__)
        gate.log.write("run_end", by=gate.operator, run=rec.run_id, status=status, reason=reason, points=n,
                       duration_s=round(duration, 3))
        if status == "done":
            msg = "run %s done: %d points, %s" % (rec.run_id, n, _dur(duration))
        else:
            msg = "run %s aborted after %d/%d points: %s" % (rec.run_id, n, plan.points, reason)
            if cleanup:
                msg += "; then " + "; ".join(cleanup)
        return RunReport(status, plan.name, msg, rec.run_id, n, duration, rec.rows,
                         str(rec.path) if rec.path else None)

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
