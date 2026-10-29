"""The run engine: validate → preflight → dry run on twins → one approval → execute → log.

Every set goes through the safety gate, so limits, ramps, guards and
interlocks apply inside plans exactly as they do for direct commands.
"""
from __future__ import annotations

import itertools
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import frames
from ._version import __version__
from .analysis import analyze_run
from .eventlog import EventLog, RunStore
from .forecast import Forecast, action_of, runs
from .gate import ApprovalRequest, SafetyGate, SimulationApprover, _dur
from .instrument import Stopped
from .plans import Plan, PlanError, Stabilize, Step, load_plan

# An instrument that passed its health check is checked again only after this long (or after a run on it fails):
# checked before it is used, as at the bench, not before every scan of a series or a queue.
HEALTHY_FOR = 600.0


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
        self._healthy: Dict[str, float] = {}   # instrument -> when (monotonic) it last passed a health check

    def _emit(self, **event: Any) -> None:
        if self.progress is not None:
            try:
                self.progress(event)
            except Exception:
                pass  # a broken display must never stop an experiment

    def held(self, plan: Plan, snapshot: Dict[str, Any]) -> Dict[str, str]:
        """The conditions a run was taken in, with units: every drive away from its safe value that the plan
        doesn't sweep, and the temperature and field readings (for the run's card and catalog)."""
        swept = {a.key for a in plan.axes}
        out: Dict[str, str] = {}
        for name, values in (snapshot or {}).items():
            inst = self.lab.instruments.get(name)
            if inst is None or not isinstance(values, dict):
                continue
            for pname, v in values.items():
                key = "%s.%s" % (name, pname)
                if key in swept or isinstance(v, (list, dict)) or v is None:
                    continue
                try:
                    spec = inst.spec(pname)
                    if (spec.is_drive and spec.distance(v) > 0) or (not spec.is_drive and spec.unit in ("K", "T")):
                        out[key] = spec.show(v)
                except Exception:
                    continue
        return out

    def units(self, snapshot: Dict[str, Any]) -> Dict[str, str]:
        """The unit of every setting and reading in a snapshot (instrument.parameter: unit), so a run's conditions
        read without the lab's cards."""
        out: Dict[str, str] = {}
        for name, values in (snapshot or {}).items():
            inst = self.lab.instruments.get(name)
            if inst is None or not isinstance(values, dict):
                continue
            for pname in values:
                try:
                    unit = inst.spec(pname).unit
                except Exception:
                    continue
                if unit:
                    out["%s.%s" % (name, pname)] = unit
        return out

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
            m = {"key": key, "unit": spec.unit, "kind": "trace" if spec.type == "trace" else
                 "image" if spec.type == "image" else "scalar"}
            if spec.axis or spec.axis_step or spec.x_values:
                m["x_unit"] = unit("%s.%s" % (instr, (spec.axis or spec.axis_step or (spec.x_values,))[0]))
            measures.append(m)
        return {"axes": axes, "measures": measures}

    # ------------------------------------------------------------------ checks

    def validate(self, plan: Plan) -> List[str]:
        """Static checks; also converts values to each parameter's type."""
        errors: List[str] = []
        lab = self.lab
        for instr, name in plan.trigger:
            if instr in lab.instruments and name not in lab.instruments[instr].card.actions:
                errors.append("trigger: %s has no action %s (it has: %s)" % (
                    instr, name, ", ".join(lab.instruments[instr].card.actions) or "none"))
        for name in plan.requires:
            if name not in lab.instruments:
                errors.append("unknown instrument %r" % name)
            elif lab.instruments[name].unavailable:
                errors.append("%s is not connected (%s); reconnect_instrument once that is fixed"
                              % (name, lab.instruments[name].unavailable))
            elif not lab.instruments[name].enabled:
                errors.append("%s is switched off in the Lab panel; a person switches it on there to use it" % name)

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

        ranges: Dict[str, Optional[float]] = {}

        def fixed_range(inst, spec):
            """The range a source runs the plan in, while it is fixed: as the plan's setup sets it, else as it is."""
            key = "%s.%s" % (inst.name, spec.name)
            if key not in ranges:
                planned = {s.param: s.value for s in plan.setup if s.instr == inst.name}
                auto = planned.get(spec.autorange_param) if spec.autorange_param else None
                try:
                    if auto is not None and inst.spec(spec.autorange_param).coerce(auto):
                        ranges[key] = None
                    elif spec.range_param in planned:
                        ranges[key] = float(planned[spec.range_param])
                    else:
                        ranges[key] = self.gate.fixed_range(inst, spec) if spec.range_param else None
                except Exception:   # can't tell now: the gate checks each change as it comes
                    ranges[key] = None
            return ranges[key]

        def check_value(inst, spec, value, where):
            try:
                v = spec.coerce(value)
            except ValueError as e:
                errors.append("%s: %s" % (where, e))
                return value
            problem = self.gate._limit_problem(inst, spec, v)
            if not problem and spec.is_drive and spec.range_param:
                rng = fixed_range(inst, spec)
                problem = self.gate._fixed_range_problem(inst, spec, v, rng) if rng is not None else None
            if problem:
                errors.append("%s: %s" % (where, problem))
            return v

        for group, steps in (("setup", plan.setup), ("after", plan.after), ("on_abort", plan.on_abort or [])):
            for step in steps:
                action = action_of(lab, step.instr, step.param)
                if action is not None:   # the card's action, run once (a waveform started), on the run's own card
                    if not runs(step.value):
                        errors.append("%s: %s is one of %s's actions: write %s: run" % (group, step.key, step.instr,
                                                                                       step.key))
                    elif group == "on_abort" and action.drives:
                        errors.append("on_abort: %s drives the sample; after a stop only actions that don't can run"
                                      % step.key)
                    continue
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

    def ordered(self, steps: List[Step]) -> List[Step]:
        """Steps in the order they can be applied: a switch that applies other drives to the sample (an output, an
        RF or laser emission: its card's enables) is switched on before the levels it applies. Switching it on parks
        those levels at safe first, so a level set before its switch would end up at safe, and the run measure dark.
        Otherwise as written."""
        where: Dict[str, int] = {}
        for i, s in enumerate(steps):
            where.setdefault(s.key, i)
        ranked = []
        for i, s in enumerate(steps):
            rank: float = i
            try:
                spec = self.lab.resolve(s.key)[1]
                on = bool(spec.enables) and spec.is_drive and spec.distance(spec.coerce(s.value)) > 0
            except (KeyError, ValueError):
                on = False
            if on:
                levels = [where[k] for k in ("%s.%s" % (s.instr, d) for d in spec.enables) if k in where]
                if levels and min(levels) < i:
                    rank = min(levels) - 0.5
            ranked.append((rank, i, s))
        return [s for _, _, s in sorted(ranked, key=lambda r: (r[0], r[1]))]

    def readback_problems(self, steps: List[Step], skip: Any = ()) -> List[str]:
        """The drives that don't read back what these steps set (a switch parked its level at safe, the instrument
        clipped or refused it): one line each, saying which drive and what it reads. The run must not measure at a
        level other than the one approved and shown. skip: keys the plan changes again (its sweep axes)."""
        last: Dict[str, Step] = {}
        for s in steps:
            if action_of(self.lab, s.instr, s.param) is None:   # an action reads back nothing
                last[s.key] = s
        out = []
        for key, s in last.items():
            if key in skip:
                continue
            try:
                inst, spec = self.lab.resolve(key)
                if not spec.is_drive:
                    continue
                want = spec.coerce(s.value)
                now = inst.read(spec.name)
            except Exception as e:
                out.append("%s could not be read back: %s" % (key, str(e).splitlines()[0][:120] if str(e) else e))
                continue
            if now is None:   # read only while its switch is on, and that is off: nothing to compare
                continue
            if spec.numeric:   # a set point reads back as set, give or take its rounding: 2 pA is not 50 nA
                lim = inst.limits(spec.name)
                close = max(spec.tol, 0.01 * abs(want), 1e-9 * (lim[1] - lim[0]) if lim else 0.0, 1e-15)
                same = isinstance(now, (int, float)) and abs(now - want) <= close
            else:
                try:
                    same = spec.coerce(now) == want
                except ValueError:
                    same = False
            if not same:
                out.append("%s reads %s, not the %s the plan set" % (key, spec.show(now), spec.show(want)))
        return out

    def drive_values(self, plan: Plan) -> Dict[str, List[Any]]:
        out: Dict[str, List[Any]] = {}

        def settings(steps: List[Step]) -> List[Tuple[str, Any]]:   # not the card's actions a step may name
            return [(s.key, s.value) for s in steps if action_of(self.lab, s.instr, s.param) is None]
        for key, value in settings(plan.setup) + [(a.key, v) for a in plan.instrument_axes for v in a.values] \
                + settings(plan.after):
            _, spec = self.lab.resolve(key)
            if spec.is_drive:
                out.setdefault(key, []).append(value)
        return out

    def preflight(self, names: Optional[List[str]] = None, fresh: bool = False):
        """Health-check these instruments (all if none are named). One that passed within HEALTHY_FOR seconds is not
        asked again unless fresh (a person's or the agent's own health check always asks)."""
        wanted = list(names) if names else [n for n, i in self.lab.instruments.items() if i.enabled]
        now = time.monotonic()
        todo = wanted if fresh else [n for n in wanted if now - self._healthy.get(n, float("-inf")) > HEALTHY_FOR]
        results = self.lab.health_check(todo) if todo else []
        for r in results:
            if r.ok:
                self._healthy[r.instr] = now
            else:
                self._healthy.pop(r.instr, None)
        if todo:
            self.gate.log.write("preflight", by=self.gate.operator, ok=all(r.ok for r in results),
                                results=[str(r) for r in results],
                                recent=sorted(set(wanted) - set(todo)) or None)
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

    def run(self, plan: Any, dry_run: bool = False, meta: Optional[Dict[str, Any]] = None,
            folder: Any = None) -> RunReport:
        """Validate, check the instruments, ask once for the drive ranges, run, log, analyse. No dry run unless asked
        (dry_run), or in auto mode for a run nobody approves: there the dry run stands in for the person's look.
        Caution for drives comes from the limits, the approval, ramps and guard readings, not a rehearsal.
        folder: where the run's folder goes (its chat's folder; else the data folder's runs/)."""
        since = self.gate.stop_generation, self.lab.stops.count   # a stop while it is checked or approved ends it too
        try:
            plan = plan if isinstance(plan, Plan) else load_plan(plan)
        except PlanError as e:
            return self._refuse("?", "plan invalid: %s" % e)
        errors = self.validate(plan)
        if errors:
            return self._refuse(plan.name, "plan invalid: " + "; ".join(errors[:5]))
        drives = self.drive_values(plan)
        # a plan that drives the sample always checks its instruments now; one that only measures trusts a check
        # from the last HEALTHY_FOR seconds (if one has died since, its reading fails and the scan just stops)
        bad = [r for r in self.preflight(plan.requires, fresh=bool(drives)) if not r.ok]
        if bad:
            return self._refuse(plan.name, "preflight failed: " + "; ".join(str(r) for r in bad))
        for cond in plan.preconditions:
            ok, actual = cond.check(self.lab.read)
            if not ok:
                _, spec = self.lab.resolve(cond.key)
                return self._refuse(plan.name, "precondition %s not met (now %s)" % (cond.text, spec.show(actual)))
        forecast = Forecast(self).plan(plan)   # what the run changes, how fast, how long, and what it leaves on
        firing = ["%s.%s (%s)" % (i, a, self.lab.instruments[i].card.actions[a].label) for i, a in plan.trigger
                  if self.lab.instruments[i].card.actions[a].drives]
        # set-up that drives the sample (a waveform started) is on this card, not a card of its own; a queue's card
        # that showed it covers it
        starting = [k for k in forecast.driving_actions() if not self.gate.action_covered(k)]
        items, grants, problems = self.gate.plan_approval(drives, forecast=forecast, asking=bool(firing or starting))
        if problems:
            return self._refuse(plan.name, "; ".join(problems))
        if firing:   # an action that drives the sample at every point: the person approves the run
            items = list(items) + ["At every point, after settling: %s, which drive the sample" % ", ".join(firing)]
        if starting:
            items = list(items) + forecast.action_lines(starting)
        if items:
            items = list(items) + forecast.closing()
        sim = None
        # auto mode needs a clean dry run, unless a drive is on an instrument Kelvin isn't sure about (a person asks)
        could_auto = self.gate.auto and not any(self.gate.unsure(self.lab.resolve(k)[0]) for k in grants)
        if dry_run or (items and could_auto):
            sim = self.simulate(plan)
            if sim.status == "refused":
                return self._refuse(plan.name, "refused by %s" % sim.message, sim)
        if items:
            title = "Run plan %s: %d points" % (plan.name, plan.points)
            if sim is not None:
                title += ", ~%s" % _dur(sim.duration_s) if sim.ok else "; %s" % sim.message
            else:   # no dry run: the forecast's ramps, settling and waits
                title += forecast.duration()
            request = ApprovalRequest(title, items)
            auto, why = self.gate.auto_plan(grants, sim)
            if firing:
                auto, why = False, list(why or []) + ["it fires %s" % ", ".join(firing)]
            if starting:   # as do_action: an action that drives the sample is a person's to start
                auto, why = False, list(why or []) + ["it starts %s" % ", ".join(starting)]
            if auto:
                self.gate.auto_approve(request, why)
            elif why:
                request.items.append("Auto mode does not cover this run: " + "; ".join(why))
            if not auto and not self.gate.ask(request):
                why = " (%s)" % request.note if request.note else ""
                report = RunReport("declined", plan.name, "declined: plan %s not run%s" % (plan.name, why), dry_run=sim)
                self.gate.log.write("run_declined", by=self.gate.operator, plan=plan.name)
                return report
        report = self._execute(plan, grants, sim.duration_s if sim is not None and sim.ok else None, meta, folder,
                               since)
        report.dry_run = sim
        if report.status != "done":   # something went wrong on them: check them again before the next run
            for name in plan.requires:
                self._healthy.pop(name, None)
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
            self.gate._pause(max(0.05, min(2.0, st.duration / 10)), gen)   # a stop wakes it at once

    def _execute(self, plan: Plan, grants: Dict[str, Any], expected_s: Optional[float] = None,
                 meta: Optional[Dict[str, Any]] = None, folder: Any = None,
                 since: Optional[Tuple[int, int]] = None) -> RunReport:
        """expected_s: the dry run's duration (lab time), for an honest time-left estimate that counts ramps."""
        gate, lab, clock = self.gate, self.lab, self.lab.clock
        gen, stops = since if since is not None else (gate.stop_generation, lab.stops.count)
        rec = self.store.create(plan.name, plan.source, gate.operator, folder)
        rec.meta["lab"] = lab.config.name   # reports say where a run was taken, even after the lab changes
        rec.meta["simulated"] = all(i.simulated for i in lab.instruments.values())
        rec.meta.update(meta or {})   # e.g. which queue item this run was
        start = lab.snapshot()
        rec.snapshot("start", start)
        gate.log.write("run_start", by=gate.operator, run=rec.run_id, plan=plan.name, points=plan.points)
        cols = self.columns(plan)
        rec.set_columns(cols)
        rec.note(units=self.units(start), planned_points=plan.points)
        self._emit(event="start", run_id=rec.run_id, plan=plan.name, total=plan.points, **cols)
        t0, wall0 = clock.now(), time.time()
        status, reason, n = "done", None, 0
        warnings: List[str] = [rec.moved] if getattr(rec, "moved", None) else []

        def apply(instr: str, param: str, value: Any) -> None:
            if gate.stop_generation != gen:   # a stop that came between steps (or while the run was starting)
                raise _Abort("stop requested")
            if action_of(lab, instr, param) is not None:   # the card's action, run once; on the run's card if it drives
                if lab.instrument(instr).simulated:
                    return   # a twin has nothing to start
                r = gate.act(instr, param, in_plan=True)
            else:
                r = gate._set(instr, param, value, True, "plan:" + plan.name, gen=gen)   # a stop ends its waits too
            if not r.ok:
                raise _Abort(r.message)

        def time_left(done: int) -> float:
            wall, lab_t = time.time() - wall0, clock.now() - t0
            if expected_s and lab_t > 0:
                return max(0.0, expected_s - lab_t) * wall / lab_t
            return wall / done * (plan.points - done) if done else 0.0

        # what the sweep changes again: its axes, and the levels a swept switch parks and puts back
        swept = {a.key for a in plan.instrument_axes}
        for a in plan.instrument_axes:
            swept.update("%s.%s" % (a.instr, d) for d in lab.resolve(a.key)[1].enables)
        try:
            with gate.preapproved(grants):
                if gate.stop_generation != gen:   # stopped before it began: not even its setup
                    raise _Abort("stop requested")
                for step in self.ordered(plan.setup):
                    apply(step.instr, step.param, step.value)
                wrong = self.readback_problems(plan.setup, swept)
                if wrong:   # never measure at a level other than the one approved (and shown on the run's card)
                    raise _Abort("before the first point, %s, so the run would not measure at what was approved"
                                 % "; ".join(wrong))
                # the conditions it measured in: the settings at its start with the plan's setup applied
                measuring = {name: dict(values) if isinstance(values, dict) else values for name, values in start.items()}
                for step in plan.setup:
                    if isinstance(measuring.get(step.instr), dict):
                        measuring[step.instr][step.param] = step.value
                rec.note(held=self.held(plan, measuring),
                         setup={"%s.%s" % (s.instr, s.param): s.value for s in plan.setup})
                prev: List[Any] = [None] * len(plan.axes)
                for combo in itertools.product(*[a.values for a in plan.axes]):
                    for j, (axis, v) in enumerate(zip(plan.axes, combo)):
                        if prev[j] is not None and v == prev[j] and all(p == c for p, c in zip(prev[:j], combo[:j])):
                            continue
                        if axis.kind == "instrument":
                            apply(axis.instr, axis.param, v)
                            if axis.stabilize:
                                self._stabilize(axis.stabilize, gen, warnings)
                            if not lab.stops.sleep(clock, axis.settle if axis.settle is not None else plan.settle,
                                                   stops):
                                raise _Abort("stop requested")   # a stop ends a settle at once
                        elif axis.kind == "time" and v:
                            if not lab.stops.sleep(clock, axis.every, stops):
                                raise _Abort("stop requested")
                    prev = list(combo)
                    row: Dict[str, Any] = {"i": n, "t": round(clock.now() - t0, 6)}
                    for axis, v in zip(plan.axes, combo):
                        row[axis.key] = v
                    for instr, action in plan.trigger:   # the cards' actions: arm, trigger, fire
                        if lab.instrument(instr).simulated:
                            continue   # a twin has no trigger to fire
                        done = gate.act(instr, action, in_plan=True)
                        if not done.ok:
                            raise _Abort("%s.%s: %s" % (instr, action, done.message))
                    for instr, param in plan.measure:
                        key = "%s.%s" % (instr, param)
                        row[key] = lab.read(instr, param)
                        mspec = lab.instrument(instr).spec(param)
                        if mspec.axis:  # record the trace's x-axis as it was when measured
                            row[key + ".axis"] = [lab.read(instr, mspec.axis[0]), lab.read(instr, mspec.axis[1])]
                        elif mspec.axis_step and row[key]:   # an oscilloscope's: origin and the step between points
                            x0, dx = lab.read(instr, mspec.axis_step[0]), lab.read(instr, mspec.axis_step[1])
                            row[key + ".axis"] = [x0, x0 + dx * (len(row[key]) - 1)]
                        elif mspec.x_values:   # its x values (a spectrometer's wavelengths), as they were then
                            row[key + ".x"] = lab.read(instr, mspec.x_values)
                        if mspec.type == "image" and row[key] and rec.path is not None:   # the frame's file
                            row[key] = frames.keep(row[key], rec.path, key, n)
                    rec.add(row)
                    n += 1
                    self._emit(event="point", run_id=rec.run_id, done=n, total=plan.points, row=row, eta_s=time_left(n))
                    if gate.stop_generation != gen:
                        raise _Abort("stop requested")
                late = self.readback_problems(plan.setup, swept)
                if late:   # something reset a level during the sweep: the assessment must not say all was well
                    warnings.append("at the end of the sweep %s: points may not have been measured at the approved "
                                    "level" % "; ".join(late))
                if plan.after:
                    self._emit(event="phase", run_id=rec.run_id, phase="after", eta_s=time_left(n))
                for step in plan.after:
                    apply(step.instr, step.param, step.value)
        except _Abort as e:
            status, reason = "aborted", str(e)
        except Stopped:   # a stop that came while an instrument was still taking a reading
            status, reason = "aborted", "stop requested"
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
                rec.set_analysis(analysis)
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
        """After a stop or a failure, first every drive the plan touched goes to safe, in its card's safe order, or
        holds where the lab config's safe_actions says hold. Then the plan's own on_abort steps, which add to that
        and never replace it: a plan's on_abort that only zeroed a voltage once left the output on, and one ramped a
        magnet the lab holds. A set point the instrument ramps to by itself is set and not waited for, so a stop never
        hangs on a cool-down (gate.abort_to_safe)."""
        by = "abort:" + plan.name
        keys = list(self.drive_values(plan))
        for step in plan.setup + plan.after:   # an action that drives (a waveform started) switched its output on
            action = action_of(self.lab, step.instr, step.param)
            inst = self.lab.instruments.get(step.instr)
            if action is not None and action.drives and inst is not None:
                keys += ["%s.%s" % (inst.name, p.name) for p in inst.card.drives
                         if p.enables and "%s.%s" % (inst.name, p.name) not in keys]
        out = [r.message for r in self.gate.abort_to_safe(keys, by)]
        for step in plan.on_abort or []:
            if action_of(self.lab, step.instr, step.param) is not None:   # one that doesn't drive (validate)
                r = self.gate.act(step.instr, step.param, in_plan=True)
                out.append(r.message)
                continue
            if self.lab.config.safe_actions.get(step.key) == "hold":
                out.append("%s stays held: the lab config holds it on a stop (on_abort asked for %s)"
                           % (step.key, self.lab.resolve(step.key)[1].show(step.value)))
                continue
            r = self.gate._set(step.instr, step.param, step.value, False, by)
            if r.action != "no-change":
                out.append(r.message)
        return out
