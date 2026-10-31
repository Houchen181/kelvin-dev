"""The run engine: validate → preflight → dry run on twins → one approval → execute → log.

Every set goes through the safety gate, so limits, ramps, guards and
interlocks apply inside plans exactly as they do for direct commands.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import frames, pulses, techniques
from ._version import __version__
from .analysis import analyze_run
from .chain import derived_readings
from .eventlog import EventLog, RunStore
from .forecast import Forecast, action_of, runs
from .gate import ApprovalRequest, SafetyGate, SimulationApprover, _dur, feeds_of, paces
from .instrument import InstrumentOff, SensorError, Stopped
from .monitor import Approach, rate_per_s, switch_words
from .plans import MAX_POINTS, Axis, Plan, PlanError, Stabilize, Step, load_plan
from .timing import Took, away as away_from_safe, settings_of, steps as timing_steps

# An instrument that passed its health check is checked again only after this long (or after a run on it fails):
# checked before it is used, as at the bench, not before every scan of a series or a queue.
HEALTHY_FOR = 600.0
# A set point in a plan's setup that holds the run longer than this before its first point is said in the report:
# nothing is measured on the way (a ramp: sweep measures while it ramps).
LONG_WAIT_S = 120.0


class _Abort(Exception):
    pass


def _transient(e: BaseException) -> bool:
    """A fault that can pass, worth a plan's retry: an instrument that didn't answer in time, a garbled reply, a link
    that dropped for a moment. Not a sensor that says it is faulty, a stop, an instrument switched off, or a bug."""
    return not isinstance(e, (_Abort, SensorError, Stopped, InstrumentOff, KeyError, TypeError, AttributeError,
                              NameError))


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


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

    def followed(self, axis: Axis) -> Optional[str]:
        """The reading a ramp follows (instr.param): its read:, else its card's readback; None when there is none."""
        if axis.read:
            return "%s.%s" % axis.read
        try:
            readback = self.lab.resolve(axis.key)[1].readback
        except KeyError:
            return None
        return "%s.%s" % (axis.instr, readback) if readback else None

    def held(self, plan: Plan, snapshot: Dict[str, Any]) -> Dict[str, str]:
        """The conditions a run was taken in, with units: every drive away from its safe value that the plan
        doesn't sweep, and the temperature and field readings (for the run's card and catalog)."""
        swept = {a.key for a in plan.axes} | {self.followed(a) for a in plan.axes if a.kind == "ramp"}
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

    def measured_with(self, plan: Plan, measuring: Dict[str, Any]) -> Dict[str, Any]:
        """What the automatic analysis needs to know about how a run measured, kept with the run so a re-analysis
        knows it too: the protocol a pulse generator played (protocol: rabi, ramsey...; not a sequence written out,
        whose meaning Kelvin doesn't know), and the readings that guarded its drives (guards: a gate's leakage
        current, which leads the assessment only when nothing else was measured). measuring: the settings it
        measured with (its start, with the plan's setup applied)."""
        out: Dict[str, Any] = {}
        for name, inst in self.lab.instruments.items():
            values = measuring.get(name) if isinstance(measuring.get(name), dict) else {}
            if inst.card.kind != "pulse-generator" or "protocol" not in inst.card.params:
                continue
            playing = values.get("running") in (True, 1, "1", "on", "ON", "true") or \
                any(a.instr == name and a.param == "running" for a in plan.axes)
            written = str(values.get("sequence") or pulses.NONE).strip().lower() not in (pulses.NONE, "")
            if playing and not written and isinstance(values.get("protocol"), str):
                out["protocol"] = values["protocol"].strip().lower()
        guards = set()
        for key in self.drive_values(plan):
            try:
                inst, spec = self.lab.resolve(key)
            except KeyError:
                continue
            guards.update(g.key for g in inst.guards(spec.name))
        if guards:
            out["guards"] = sorted(guards)
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
        """Axis and measured columns with units, as analysis and plots need them. A ramp's column is the reading it
        follows (R against the temperature reached, not against the set point)."""
        def unit(key: str) -> str:
            try:
                return self.lab.resolve(key)[1].unit
            except KeyError:
                return ""
        axes = []
        for a in plan.axes:
            key = (self.followed(a) or a.key) if a.kind == "ramp" else a.key
            axes.append({"key": key, "unit": "s" if a.kind == "time" else ("" if a.kind == "repeat" else
                                                                           techniques.axis_unit(self.lab, a)
                                                                           if a.kind == "technique" else unit(key)),
                         "kind": a.kind})
        ramped = {c["key"] for c in axes if c["kind"] == "ramp"}
        measures = []
        for instr, param in plan.measure:
            key = "%s.%s" % (instr, param)
            if key in ramped:
                continue   # it is the ramp's own column, read at every point
            spec = self.lab.resolve(key)[1]
            m = {"key": key, "unit": spec.unit, "kind": "trace" if spec.type == "trace" else
                 "image" if spec.type == "image" else "scalar"}
            if spec.axis or spec.axis_step or spec.x_values:
                m["x_unit"] = unit("%s.%s" % (instr, (spec.axis or spec.axis_step or (spec.x_values,))[0]))
            measures.append(m)
        measures += techniques.columns(self.lab, plan)   # a technique's buffers: traces, against its x
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
            st = axis.stabilize
            if spec and st and st.near is not None and self._set_point_of(st) is None and not self._same_unit(
                    axis.key, st.key):
                errors.append("sweep.%s.stabilize: near: compares %s with its set point, but neither %s has a set "
                              "point it reads back nor does %s set one in its unit" % (axis.key, st.key, st.instr,
                                                                                         axis.key))
        ramp = plan.ramp
        if ramp is not None:
            self._check_ramp(ramp, settable, check_value, errors)
        reads = plan.measure + [(c.instr, c.param) for c in plan.preconditions] \
            + [(a.stabilize.instr, a.stabilize.param) for a in plan.axes if a.stabilize] \
            + [tuple(k.split(".", 1)) for r in plan.rules() for k in r.keys]
        for instr, param in reads:
            try:
                _, spec = lab.resolve("%s.%s" % (instr, param))
                if spec.get_cmd is None:
                    errors.append("%s.%s cannot be read" % (instr, param))
            except KeyError as e:
                errors.append(e.args[0])
        if ramp is not None and not errors:
            ramp.estimate = self._estimate(ramp)
        if not errors:   # a technique's values and levels, at every value its sweep gives them
            errors += techniques.check(self.gate, plan)
        if not errors:   # a generator it plays, at every value its sweep gives it, before anything plays
            errors += pulses.plan_problems(lab, plan)
        return errors

    def _check_ramp(self, axis: Axis, settable: Any, check_value: Any, errors: List[str]) -> None:
        """A ramp sweep's set point, the reading it follows, how close counts as there, and its legs' rates."""
        where = "sweep.%s" % axis.key
        inst, spec = settable(axis.instr, axis.param, where)
        if not spec:
            return
        if spec.ramp == "gate" or not spec.numeric:
            errors.append("%s: %s" % (where, "Kelvin ramps %s itself, a step at a time, so nothing is measured on "
                                             "the way: sweep it with {from, to, step} instead" % axis.key
                                      if spec.numeric else "only a number can be ramped"))
            return
        before = len(errors)
        axis.values = [check_value(inst, spec, v, where) for v in axis.values]
        del errors[before + 1:]
        for leg, value in zip(axis.segments, axis.values):
            leg.to = value
        reading = self.followed(axis)
        if reading is None:
            errors.append("%s: say which reading to follow while it ramps (read: instrument.parameter); %s's card "
                          "names none" % (where, axis.key))
        else:
            try:
                rspec = self.lab.resolve(reading)[1]
                if rspec.get_cmd is None or not rspec.numeric:
                    errors.append("%s: %s can't be followed: it is not a reading of a number" % (where, reading))
            except KeyError as e:
                errors.append("%s: %s" % (where, e.args[0]))
        tol = axis.within if axis.within is not None else spec.tol
        if not tol or tol <= 0:
            errors.append("%s: say how close to each target counts as there (within: in %s)" % (
                where, spec.unit or "its unit"))
        rates = [leg.rate for leg in axis.segments if leg.rate is not None]
        if rates and not spec.rate_param:
            errors.append("%s: rate: needs %s's card to name the parameter its rate is set with (rate_param); set the "
                          "rate in setup: instead" % (where, axis.key))
        elif rates:
            rinst, rspec = settable(axis.instr, spec.rate_param, where + ".rate")
            if rspec:
                before = len(errors)
                for leg in axis.segments:
                    if leg.rate is not None:
                        leg.rate = check_value(rinst, rspec, leg.rate, where + ".rate")
                del errors[before + 1:]

    def _estimate(self, axis: Axis) -> Optional[int]:
        """How many points a ramp records: its legs' ramp times (from the reading now, at each leg's rate or the
        instrument's) and holds, over its interval; None when a rate or the reading is unknown."""
        try:
            inst, spec = self.lab.resolve(axis.key)
            at = self.lab.read(*self.followed(axis).split(".", 1))
        except Exception:
            return None
        seconds, rate = 0.0, None
        for leg in axis.segments:
            rate = leg.rate if leg.rate is not None else rate
            per_s = rate_per_s(inst, spec, rate)
            if per_s is None or not _num(at) or not _num(leg.to):
                return None
            seconds += abs(leg.to - at) / per_s + leg.hold
            at = leg.to
        return int(seconds / axis.every) + len(axis.segments)

    def _set_point_of(self, st: Stabilize) -> Optional[str]:
        """The set point a stabilize's reading reads back (instr.param), if its instrument has one."""
        try:
            inst = self.lab.instrument(st.instr)
        except KeyError:
            return None
        drive = next((p for p in inst.card.params.values() if p.is_drive and p.readback == st.param), None)
        return "%s.%s" % (st.instr, drive.name) if drive is not None else None

    def _same_unit(self, a: str, b: str) -> bool:
        try:
            ua, ub = self.lab.resolve(a)[1].unit, self.lab.resolve(b)[1].unit
        except KeyError:
            return False
        return bool(ua) and ua == ub

    def ordered(self, steps: List[Step]) -> List[Step]:
        """Steps in the order they can be applied: a switch that applies other drives to the sample (an output, an
        RF or laser emission: its card's enables; a heater range, which a set point's applies_when names) is switched
        on before the levels it applies. Switching an output on parks those levels at safe first, so a level set before
        its switch would end up at safe, and the run measure dark; a set point set while its heater is off is never
        reached, and a setup that set it first was stopped for it. A rate goes before the set point it paces (a field
        doesn't move at a rate of 0). Otherwise as written."""
        where: Dict[str, int] = {}
        for i, s in enumerate(steps):
            where.setdefault(s.key, i)
        ranked = []
        for i, s in enumerate(steps):
            rank: float = i
            try:
                inst, spec = self.lab.resolve(s.key)
                applies = list(spec.enables) + ([p.name for p in inst.card.drives if p.applies_when == spec.name]
                                                if spec.type in ("bool", "enum") else [])   # a switch (gate._switch_of)
                on = bool(applies) and spec.is_drive and spec.distance(spec.coerce(s.value)) > 0
                paced = paces(inst.card, spec.name)
            except (KeyError, ValueError):
                on, paced, applies = False, None, []
            if on:
                levels = [where[k] for k in ("%s.%s" % (s.instr, d) for d in applies) if k in where]
                if levels and min(levels) < i:
                    rank = min(levels) - 0.5
            elif paced is not None and where.get("%s.%s" % (s.instr, paced.name), i) < i:
                rank = where["%s.%s" % (s.instr, paced.name)] - 0.5
            ranked.append((rank, i, s))
        return [s for _, _, s in sorted(ranked, key=lambda r: (r[0], r[1]))]

    def interlock_problems(self, plan: Optional[Plan], state: Optional[Dict[str, Any]] = None, who: str = "the plan",
                           sets: Optional[List[Tuple[str, str, Any]]] = None) -> List[str]:
        """Where a plan would break an interlock, foreseen before it runs (a temperature series with a 12 K step at
        -2.5 T ran 1.6 h before its interlock, T < 10 K, stopped it): a moment when an interlock's drive is away from
        safe while the plan has its condition failing, by setting it or the set point that drives it (tc.setpoint
        drives tc.T_sample: labs write interlocks on the reading). Setup and after steps in the engine's order; every
        value of a sweep meets every other (its loops are nested). A condition that fails already, before the plan, is
        the gate's to refuse (plan_approval). state: what the walk starts from, updated as it goes (a queue's items,
        each from where the one before leaves the lab); who: what the lines name; sets: a queue's step between scans
        (instr, param, value), in place of a plan."""
        locks = self.lab.config.interlocks
        state = {} if state is None else state
        if not locks:
            return []
        problems: List[str] = []
        done: set = set()

        def now(key: str) -> Tuple[Any, Optional[str]]:
            """(its value, what in the walk set it: None for the lab as it is now)."""
            if key not in state:
                try:
                    state[key] = (self.lab.read(*key.split(".", 1)), None)
                except Exception:
                    state[key] = (None, None)
            return state[key]

        def values(changes: List[Tuple[str, Any, str]]) -> Dict[str, List[Tuple[Any, Optional[str]]]]:
            """Each key these changes decide, with every value it takes and what sets it."""
            out: Dict[str, List[Tuple[Any, Optional[str]]]] = {}
            for key, value, where in changes:
                try:
                    inst, spec = self.lab.resolve(key)
                    v = spec.coerce(value)
                except (KeyError, ValueError):
                    continue
                cause = "%s sets %s to %s" % (where, key, spec.show(v))
                for k in feeds_of(inst, spec):
                    out.setdefault(k, []).append((v, cause))
            return out

        def check(moment: Dict[str, List[Tuple[Any, Optional[str]]]]) -> None:
            for i, lock in enumerate(locks):
                try:
                    dspec = self.lab.resolve(lock.drive)[1]
                except KeyError:
                    continue
                if i in done:
                    continue
                drives = moment.get(lock.drive) or [now(lock.drive)]
                conds = moment.get(lock.require.key) or [now(lock.require.key)]
                up = next((d for d, _ in drives if d is not None and dspec.distance(d) > 0), None)
                bad = next(((c, cause) for c, cause in conds if c is not None and cause is not None
                            and not lock.require.check(lambda _i, _p: c)[0]), None)
                if up is not None and bad is not None:
                    done.add(i)
                    problems.append("interlock: %s needs %s, but it would be at %s while %s is at %s (%s)" % (
                        lock.drive, lock.require.text, dspec.show(up), lock.require.key,
                        self.lab.resolve(lock.require.key)[1].show(bad[0]), bad[1]))

        def go(changes: List[Tuple[str, Any, str]], together: bool = False) -> None:
            """Setup or after steps one at a time, as the engine sets them; a sweep's values all together."""
            if not changes:
                return
            for moment in [values(changes)] if together else [values([one]) for one in changes]:
                check(moment)
                for k, vs in moment.items():
                    state[k] = vs[-1]   # as it leaves it

        if plan is None:
            go([("%s.%s" % (instr, param), value, who) for instr, param, value in sets or []])
            return problems
        go([(s.key, s.value, "%s's setup" % who) for s in self.ordered(plan.setup)
            if action_of(self.lab, s.instr, s.param) is None])
        go([(a.key, v, "%s's sweep" % who) for a in plan.axes if a.kind in ("instrument", "ramp") for v in a.values],
           together=True)
        go([(s.key, s.value, "%s's after steps" % who) for s in plan.after
            if action_of(self.lab, s.instr, s.param) is None])
        return problems

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
                + self._ramp_values(plan) + techniques.drive_values(self.lab, plan) + settings(plan.after):
            _, spec = self.lab.resolve(key)
            if spec.is_drive:
                out.setdefault(key, []).append(value)
        return out

    def _ramp_values(self, plan: Plan) -> List[Tuple[str, Any]]:
        """A ramp's targets, and its legs' rates on the card's rate parameter (a drive itself on a magnet supply, so
        the plan's one approval covers them)."""
        ramp = plan.ramp
        if ramp is None:
            return []
        rate_param = self.lab.resolve(ramp.key)[1].rate_param
        return [(ramp.key, v) for v in ramp.values] + [("%s.%s" % (ramp.instr, rate_param), leg.rate)
                                                        for leg in ramp.segments if leg.rate is not None and rate_param]

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
            folder: Any = None, since: Optional[Tuple[int, int]] = None, notes: Iterable[str] = ()) -> RunReport:
        """Validate, check the instruments, ask once for the drive ranges, run, log, analyse. No dry run unless asked
        (dry_run), or in auto mode for a run nobody approves: there the dry run stands in for the person's look.
        Caution for drives comes from the limits, the approval, ramps and guard readings, not a rehearsal.
        folder: where the run's folder goes (its chat's folder; else the data folder's runs/). since: (the gate's stop
        generation, the lab's stops) from when the caller began (a queue picking its item): a stop since then ends
        it. notes: lines its card adds, if it asks (a dry run of it that just failed)."""
        # a stop while it is checked or approved ends it too
        since = since or (self.gate.stop_generation, self.lab.stops.count)
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
        techniques.account(forecast, self.lab, plan)   # its techniques' time, which the walk doesn't count
        # a technique that drives the sample runs at every point on the instrument's own clock: on this card, a
        # person's to approve (a queue's card that showed it covers it)
        running = [k for k in techniques.driving(self.lab, plan) if not self.gate.action_covered(k)]
        firing = ["%s.%s (%s)" % (i, a, self.lab.instruments[i].card.actions[a].label) for i, a in plan.trigger
                  if self.lab.instruments[i].card.actions[a].drives]
        # set-up that drives the sample (a waveform started) is on this card, not a card of its own; a queue's card
        # that showed it covers it
        starting = [k for k in forecast.driving_actions() if not self.gate.action_covered(k)]
        items, grants, problems = self.gate.plan_approval(drives, forecast=forecast,
                                                          asking=bool(firing or starting or running))
        problems += self.interlock_problems(plan)   # a step that would break an interlock: refused now, not hours in
        if problems:
            return self._refuse(plan.name, "; ".join(problems))
        if firing:   # an action that drives the sample at every point: the person approves the run
            items = list(items) + ["At every point, after settling: %s, which drive the sample" % ", ".join(firing)]
        if starting:
            items = list(items) + forecast.action_lines(starting)
        if running or (items and plan.techniques):
            items = list(items) + techniques.lines(self.gate, plan)
        if items:
            items = list(items) + forecast.closing() + list(notes)
        sim = None
        # auto mode needs a clean dry run, unless a drive is on an instrument Kelvin isn't sure about (a person asks)
        could_auto = self.gate.auto and not any(self.gate.unsure(self.lab.resolve(k)[0]) for k in grants)
        if dry_run or (items and could_auto):
            sim = self.simulate(plan)
            if sim.status == "refused":
                return self._refuse(plan.name, "refused by %s" % sim.message, sim)
        if items:
            title = "Run plan %s: %s" % (plan.name, self.shape(plan))
            if sim is not None:
                title += ", ~%s" % _dur(sim.duration_s) if sim.ok else "; %s" % sim.message
            else:   # no dry run: the forecast's ramps, settling and waits
                title += forecast.duration()
            request = ApprovalRequest(title, items)
            pulses.illustrate_plan(request, self.lab, plan)   # a generator it plays: its timing, drawn and exact
            auto, why = self.gate.auto_plan(grants, sim)
            if firing:
                auto, why = False, list(why or []) + ["it fires %s" % ", ".join(firing)]
            if starting:   # as do_action: an action that drives the sample is a person's to start
                auto, why = False, list(why or []) + ["it starts %s" % ", ".join(starting)]
            if running:   # nothing watches inside it: a person approves it
                auto, why = False, list(why or []) + ["it runs %s, which drives the sample on the instrument's own "
                                                      "clock" % ", ".join(running)]
            if auto:
                self.gate.auto_approve(request, why)
            elif why:
                request.items.append("Auto mode does not cover this run: " + "; ".join(why))
            if not auto and not self.gate.ask(request):
                why = " (%s)" % request.note if request.note else ""
                report = RunReport("declined", plan.name, "declined: plan %s not run%s" % (plan.name, why), dry_run=sim)
                self.gate.log.write("run_declined", by=self.gate.operator, plan=plan.name)
                return report
        # the time left counts from the dry run's duration, else the card's estimate (timing.py), not points alone
        expected = sim.duration_s if sim is not None and sim.ok else forecast.seconds or None
        report = self._execute(plan, grants, expected, meta, folder, since)
        report.dry_run = sim
        if report.status != "done":   # something went wrong on them: check them again before the next run
            for name in plan.requires:
                self._healthy.pop(name, None)
        return report

    def shape(self, plan: Plan) -> str:
        """Its size in words: "21 points"; a ramp says what it does, its points an estimate."""
        ramp = plan.ramp
        if ramp is None:
            return "%d points" % plan.points
        try:
            inst, spec = self.lab.resolve(ramp.key)
            per = inst.spec(spec.rate_param).unit if spec.rate_param else ""
            legs = ", then ".join(spec.show(leg.to) + (" at %g %s" % (leg.rate, per) if leg.rate is not None else "")
                                  for leg in ramp.segments)
        except KeyError:
            legs = ", then ".join(str(leg.to) for leg in ramp.segments)
        return "measures every %s while %s ramps to %s (%s)" % (
            _dur(ramp.every), ramp.key, legs, "~%d points" % plan.points if ramp.estimate else
            "as many points as it takes")

    def _refuse(self, name: str, why: str, sim: Optional[RunReport] = None) -> RunReport:
        self.gate.log.write("run_refused", by=self.gate.operator, plan=name, reason=why)
        return RunReport("refused", name, "refused: %s" % why, dry_run=sim)

    def _stabilize(self, st: Stabilize, gen: int, warnings: List[str], target: Any = None,
                   record: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
        """Wait until st's reading moves by less than st.within over st.duration (or give up at st.timeout, measure
        anyway and say so). With st.near, it must also be that close to its set point: the one its instrument reads
        back, else `target` (a sweep's value in the reading's unit); a steady reading short of it, or a frozen dead
        sensor, has not settled. record: takes a point, every st.every seconds while it waits. Returns when it
        settled (or didn't), for the run's record."""
        clock, lab = self.lab.clock, self.lab
        t0 = clock.now()
        window: deque = deque()
        spec = lab.resolve(st.key)[1]
        goal = target
        if st.near is not None:
            own = self._set_point_of(st)
            goal = lab.read(*own.split(".", 1)) if own else target
        logged = None
        while True:
            now, value = clock.now(), lab.read(st.instr, st.param)
            window.append((now, value))
            while window and now - window[0][0] > st.duration:
                window.popleft()
            values = [v for _, v in window]
            numbers = all(_num(v) for v in values)
            swing = max(values) - min(values) if numbers else math.inf
            close = st.near is None or (_num(goal) and _num(value) and abs(value - goal) <= st.near)
            if now - t0 >= st.duration and swing <= st.within and close:
                return {"read": st.key, "settled": True, "after_s": round(now - t0, 3)}
            if now - t0 > st.timeout:
                far = "" if close else "; it reads %s, not within %s of %s" % (
                    spec.show(value) if _num(value) else repr(value), spec.show(st.near),
                    spec.show(goal) if _num(goal) else "its set point")
                warnings.append("%s did not settle within %s in %s (last swing %s%s); measured anyway"
                                % (st.key, spec.show(st.within), _dur(st.timeout),
                                   spec.show(swing) if numbers else "not a number", far))
                return {"read": st.key, "settled": False, "after_s": round(now - t0, 3)}
            if self.gate.stop_generation != gen:
                raise _Abort(self.gate.stop_words())
            pause = max(0.05, min(2.0, st.duration / 10))
            if record is not None and st.every:
                if logged is None or now - logged >= st.every - 1e-9:
                    record()   # a point while it waits, marked stabilizing
                    logged = now
                pause = max(0.0, min(pause, logged + st.every - clock.now()))
            self.gate._pause(pause, gen)   # a stop wakes it at once

    def _execute(self, plan: Plan, grants: Dict[str, Any], expected_s: Optional[float] = None,
                 meta: Optional[Dict[str, Any]] = None, folder: Any = None,
                 since: Optional[Tuple[int, int]] = None) -> RunReport:
        """expected_s: the dry run's duration (lab time), else the card's estimate, for an honest time-left estimate
        that counts ramps. A real run measures how long its readings, steps and points take (timing.py), for the
        estimates after it; a dry run's twins take what this lab's runs measured (lab.pace), so it counts what the card
        counts."""
        gate, lab, clock = self.gate, self.lab, self.lab.clock
        gen, stops = since if since is not None else (gate.stop_generation, lab.stops.count)
        timing, pace = getattr(lab, "timing", None), getattr(lab, "pace", None)
        took = Took()
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
                raise _Abort(gate.stop_words())
            if action_of(lab, instr, param) is not None:   # the card's action, run once; on the run's card if it drives
                if lab.instrument(instr).simulated:
                    return   # a twin has nothing to start
                r = gate.act(instr, param, in_plan=True)
            else:
                inst = lab.instrument(instr)
                before, began = inst._last.get(param), clock.now()
                r = gate._set(instr, param, value, True, "plan:" + plan.name, gen=gen)   # a stop ends its waits too
                if r.ok:
                    stepped(inst, param, before, r.value, clock.now() - began)
            if not r.ok:
                raise _Abort(r.message)

        def stepped(inst: Any, param: str, before: Any, after: Any, seconds: float) -> None:
            """A drive or setting it stepped: what each step took beyond its dwell; a rehearsal takes as long."""
            spec = inst.spec(param)
            n, dwell = timing_steps(inst, spec, before, after)
            if not n:
                return
            key, away = "%s.%s" % (inst.name, param), away_from_safe(spec, before, after)
            took.step(key, (seconds - dwell) / n, away)
            if pace is not None:
                clock.sleep(n * pace.step_s(key, away))

        def read(key: str, get: Callable[[], Any]) -> Any:
            """One reading, timed; a rehearsal takes as long as this lab's runs measured it (with these settings)."""
            began = clock.now()
            value = get()
            spent = clock.now() - began
            took.read(key, spent)
            if pace is not None:
                inst = lab.instrument(key.split(".", 1)[0])
                s = pace.read_s(key, inst._last.get)
                if s is not None and s > spent:
                    clock.sleep(s - spent)
            return value

        def aim(instr: str, param: str, value: Any) -> None:
            """A ramp's set point, set without waiting for its reading: the ramp measures while it gets there."""
            if gate.stop_generation != gen:
                raise _Abort(gate.stop_words())
            r = gate._set(instr, param, value, False, "plan:" + plan.name, gen=gen)
            if not r.ok:
                raise _Abort(r.message)

        def time_left(done: int) -> float:
            wall, lab_t = time.time() - wall0, clock.now() - t0
            by_points = wall / done * max(0, plan.points - done) if done else 0.0
            if expected_s and lab_t > 0:   # a run past its estimate goes by its points still to take
                left = max(0.0, expected_s - lab_t) * wall / lab_t
                return left if left > 0 or done >= plan.points else by_points
            return by_points

        # what the sweep changes again: its axes, and the levels a swept switch parks and puts back
        swept = {a.key for a in plan.axes if a.kind in ("instrument", "ramp")}
        for a in plan.axes:
            if a.kind in ("instrument", "ramp"):
                swept.update("%s.%s" % (a.instr, d) for d in lab.resolve(a.key)[1].enables)
        ramp = plan.ramp
        followed = self.followed(ramp) if ramp is not None else None   # the reading a ramp follows: its column
        logs_waits = any(a.stabilize is not None and a.stabilize.every for a in plan.axes)
        state: Dict[str, Any] = {"last": {}}
        tvalues = {t.key: dict(t.values) for t in plan.techniques}   # each technique's values, as its sweep steps them
        gaps: List[Dict[str, Any]] = []      # points a plan's retry took again, and the time lost
        settled: List[Dict[str, Any]] = []   # when each stabilize settled (or didn't)
        legs: List[Dict[str, Any]] = []      # a ramp's legs: when each began, got there and ended
        ended: List[str] = []                # why loops ended before their last value (until: or while:)

        def readings(row: Dict[str, Any]) -> None:
            """One point's reading: a ramp's own reading, the cards' actions, then what the plan measures."""
            if followed:
                row[followed] = read(followed, lambda: lab.read(*followed.split(".", 1)))
            for instr, action in plan.trigger:   # the cards' actions: arm, trigger, fire
                if lab.instrument(instr).simulated:
                    read("%s.%s" % (instr, action), lambda: None)   # a twin has no trigger to fire: it takes as long
                    continue
                done = read("%s.%s" % (instr, action), lambda: gate.act(instr, action, in_plan=True))
                if not done.ok:
                    raise _Abort("%s.%s: %s" % (instr, action, done.message))
            for step in plan.techniques:   # the cards' techniques: their buffers, as traces
                inst, tech = techniques.of(lab, step.instr, step.name)
                try:
                    buffers = techniques.run(gate, inst, tech, tvalues[step.key], stops, "plan:" + plan.name)
                except techniques.Refused as e:
                    raise _Abort(str(e))
                techniques.record(row, step.key, tech, buffers)
            for instr, param in plan.measure:
                key = "%s.%s" % (instr, param)
                if key == followed:
                    continue   # read already, as the ramp's column
                row[key] = read(key, lambda: lab.read(instr, param))
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

        def measured(row: Dict[str, Any]) -> float:
            """readings(row), tried again on a fault that can pass when the plan says retry:. Returns the time lost
            (0: none). After a fault the instrument may have restarted (a power blip puts its outputs back to their
            power-on state), so the setup's levels are read back before the point counts: never measured dark."""
            tries, first, why = 0, None, ""
            while True:
                row["t"] = round(clock.now() - t0, 6)
                try:
                    readings(row)
                    break
                except Exception as e:
                    if plan.retry is None or tries >= plan.retry.times or not _transient(e):
                        if tries:
                            raise _Abort("%s (tried %d times)" % (str(e).splitlines()[0] if str(e) else
                                                                  type(e).__name__, tries + 1))
                        raise
                    tries += 1
                    first = clock.now() if first is None else first
                    why = why or (str(e).splitlines()[0][:160] if str(e) else type(e).__name__)
                    gate.log.write("retry", by=gate.operator, run=rec.run_id, point=n, attempt=tries,
                                   error=str(e).splitlines()[0][:200] if str(e) else type(e).__name__)
                    if not lab.stops.sleep(clock, plan.retry.wait, stops) or gate.stop_generation != gen:
                        raise _Abort(gate.stop_words())
            if first is None:
                return 0.0
            wrong = self.readback_problems(plan.setup, swept)
            if wrong:
                raise _Abort("after %s, %s, so the run would not measure at what was approved (did it restart?)"
                             % (why, "; ".join(wrong)))
            lost = round(clock.now() - first, 3)
            gaps.append({"point": n, "t": row["t"], "lost_s": lost, "tries": tries, "error": why})
            return lost

        def take(at: Dict[str, Any], waiting: bool = False) -> Dict[str, Any]:
            """Measure and record one point at these loop values; waiting: logged while a stabilize waits."""
            nonlocal n
            if n >= MAX_POINTS:
                raise _Abort("the run has %d points, the most one run keeps" % MAX_POINTS)
            row: Dict[str, Any] = {"i": n, "t": 0.0}
            row.update(at)
            lost = measured(row)
            began = clock.now()
            if plan.retry is not None:
                row["gap_s"] = lost   # the time lost to a fault just before this point (0: none)
            if logs_waits:
                row["stabilizing"] = 1 if waiting else 0
            rec.add(row)
            n += 1
            state["last"] = row
            self._emit(event="point", run_id=rec.run_id, done=n, total=max(plan.points, n), row=row,
                       eta_s=time_left(n))
            took.point(clock.now() - began)   # recording it, beyond its readings
            if pace is not None:
                clock.sleep(pace.point_s())
            if gate.stop_generation != gen:
                raise _Abort(gate.stop_words())
            return row

        def ends(until: Any, while_: Any, row: Dict[str, Any]) -> Optional[str]:
            """Why a loop ends after this point: its until: holds, or its while: no longer does; also when that can't
            be told (a reading that isn't a number): a condition nobody can check ends the loop."""
            def read(instr: str, param: str) -> Any:
                key = "%s.%s" % (instr, param)
                return row[key] if key in row else lab.read(instr, param)
            if until is not None:
                holds = until.check(read)
                if holds is None or holds:
                    return "until %s%s" % (until.text, "" if holds else " could not be told (not a number)")
            if while_ is not None:
                holds = while_.check(read)
                if not holds:
                    return "while %s %s" % (while_.text, "no longer held" if holds is False else
                                            "could not be told (not a number)")
            return None

        def point(at: Dict[str, Any]) -> bool:
            why = ends(plan.until, plan.while_, take(at))
            if why:
                ended.append("%s, at point %d" % (why, n))
            return not why

        def set_axis(axis: Axis, k: int, v: Any, at: Dict[str, Any]) -> None:
            if axis.kind == "instrument":
                apply(axis.instr, axis.param, v)
                st = axis.stabilize
                if st:
                    log = (lambda: take(dict(at, **{axis.key: v}), waiting=True)) if st.every else None
                    info = self._stabilize(st, gen, warnings, v if self._same_unit(axis.key, st.key) else None, log)
                    settled.append(dict(info, axis=axis.key, value=v, t=round(clock.now() - t0, 3)))
                hold = axis.settle_at(k)
                if not lab.stops.sleep(clock, hold if hold is not None else plan.settle, stops):
                    raise _Abort(gate.stop_words())   # a stop ends a settle at once
            elif axis.kind == "time" and v:
                if not lab.stops.sleep(clock, axis.every, stops):
                    raise _Abort(gate.stop_words())
            elif axis.kind == "technique":   # one of a technique's parameters: its next runs use this value
                name, _, param = axis.param.partition(".")
                tvalues["%s.%s" % (axis.instr, name)][param] = v
                hold = axis.settle_at(k)
                if not lab.stops.sleep(clock, hold if hold is not None else plan.settle, stops):
                    raise _Abort(gate.stop_words())

        def loop(j: int, at: Dict[str, Any]) -> bool:
            """Loop j and the ones inside it, at the outer loops' values; False once the plan's until: or while:
            has ended the whole sweep."""
            if j == len(plan.axes):
                return point(at)
            axis = plan.axes[j]
            if axis.kind == "ramp":
                return ramped(axis, at)
            for k, v in enumerate(axis.values):
                if not (k and v == axis.values[k - 1]):   # the same value again right after: nothing to set
                    set_axis(axis, k, v, at)
                if not loop(j + 1, dict(at, **{axis.key: v})):
                    return False
                why = ends(axis.until, axis.while_, state["last"]) if (axis.until or axis.while_) else None
                if why:   # this loop's other values are skipped; the loops outside it go on
                    ended.append("%s: %s, at point %d" % (axis.key, why, n))
                    break
            return True

        def ramped(axis: Axis, at: Dict[str, Any]) -> bool:
            """A ramp: each leg's rate and target set without waiting, then a point every axis.every seconds until
            its reading is within tolerance of the target, and hold: seconds more. A leg whose reading stops getting
            closer for axis.stall stops the run (fail closed: a stuck cryostat isn't waited on for hours)."""
            inst, spec = lab.resolve(axis.key)
            tol = axis.within if axis.within is not None else spec.tol
            for leg in axis.segments:
                if leg.rate is not None:
                    apply(axis.instr, spec.rate_param, leg.rate)
                aim(axis.instr, axis.param, leg.to)
                began = clock.now()
                way = Approach(leg.to, tol, began, rate_per_s(inst, spec, leg.rate), axis.stall or None)
                info: Dict[str, Any] = {"to": leg.to, "rate": leg.rate, "hold": leg.hold,
                                        "began_t": round(began - t0, 3)}
                legs.append(info)
                due, there = began, None
                while True:
                    row = take(at)
                    now, value = clock.now(), row.get(followed)
                    stuck = way.update(value, now)
                    if there is None and way.there(value):
                        there = now
                        info["arrived_t"] = round(now - t0, 3)
                    why = ends(plan.until, plan.while_, row)
                    if why:
                        ended.append("%s, at point %d" % (why, n))
                        info["ended_t"] = round(now - t0, 3)
                        return False
                    why = ends(axis.until, axis.while_, row)
                    if why:   # this ramp is over; the loops outside it go on
                        ended.append("%s: %s, at point %d" % (axis.key, why, n))
                        info["ended_t"] = round(now - t0, 3)
                        return True
                    if there is not None and now - there >= leg.hold - 1e-9:
                        break
                    if there is None and axis.stall and stuck >= axis.stall:
                        switch = switch_words(inst, spec)   # a heater range too low, not a stuck instrument
                        raise _Abort("%s has not got closer to %s for %s (it reads %s%s)%s" % (
                            followed, spec.show(leg.to), _dur(stuck), spec.show(value) if _num(value) else repr(value),
                            "; " + switch if switch else "", "" if switch else ": the instrument may be stuck"))
                    due = max(due + axis.every, clock.now())
                    if not lab.stops.sleep(clock, max(0.0, due - clock.now()), stops):
                        raise _Abort(gate.stop_words())   # a stop ends the wait for the next point at once
                info["ended_t"] = round(clock.now() - t0, 3)
            return True

        try:
            with gate.preapproved(grants):
                if gate.stop_generation != gen:   # stopped before it began: not even its setup
                    raise _Abort(gate.stop_words())
                for step in self.ordered(plan.setup):
                    began = clock.now()
                    apply(step.instr, step.param, step.value)
                    self._long_wait(plan, step, clock.now() - began, warnings)
                wrong = self.readback_problems(plan.setup, swept)
                if wrong:   # never measure at a level other than the one approved (and shown on the run's card)
                    raise _Abort("before the first point, %s, so the run would not measure at what was approved"
                                 % "; ".join(wrong))
                # the conditions it measured in: the settings at its start with the plan's setup applied
                measuring = {name: dict(values) if isinstance(values, dict) else values for name, values in start.items()}
                for step in plan.setup:
                    if isinstance(measuring.get(step.instr), dict):
                        measuring[step.instr][step.param] = step.value
                try:
                    known = self.measured_with(plan, measuring)
                except Exception:   # what the analysis would like to know never stops a run
                    known = {}
                rec.note(held=self.held(plan, measuring),
                         setup={"%s.%s" % (s.instr, s.param): s.value for s in plan.setup}, **known)
                loop(0, {})
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
            status, reason = "aborted", gate.stop_words()
        except Exception as e:
            status, reason = "aborted", "error: %s" % e
        if gaps:   # the data say where: each such point's gap_s
            warnings.append("%d reading%s failed for a moment and %s taken again (%s lost in all, marked on %s as "
                            "gap_s): %s" % (len(gaps), "s" if len(gaps) > 1 else "", "were" if len(gaps) > 1 else "was",
                                            _dur(sum(g["lost_s"] for g in gaps)),
                                            "those points" if len(gaps) > 1 else "that point", gaps[0]["error"]))
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
        # how it went, where a plan asked for more than a fixed grid: a ramp's legs, each stabilize, the readings
        # taken again, and the loops its until: or while: ended early
        meta.update({k: v for k, v in (("legs", legs), ("stabilized", settled), ("gaps", gaps), ("ended", ended)) if v})
        analysis = None
        points = [r for r in rec.rows if not r.get("stabilizing")]   # not the ones logged while it waited
        if self.analyze and points:
            try:   # with how it measured (protocol, guards) and the device's measurement chain (readings in ohms)
                known = {k: rec.meta[k] for k in ("protocol", "guards") if k in rec.meta}
                derive = derived_readings(rec.meta.get("chain"), rec.meta.get("setup"), start, cols["measures"])
                analysis = analyze_run(points, cols["axes"], cols["measures"], dict(meta, **known), derive=derive)
                rec.set_analysis(analysis)
            except Exception as e:  # analysis is a convenience; the data are already safe on disk
                warnings.append("automatic analysis failed: %s" % e)
        if took.reads or took.steps:   # what its readings, steps and points took: the estimates after it count them
            meta["timing"] = took.summary()
            if timing is not None and n:
                try:
                    timing.learn(rec.run_id, took, self._ran_with(plan, start, {k.split(".")[0] for k in took.reads}))
                except Exception:   # an estimate is a convenience; the run is kept whatever happens here
                    pass
        rec.finish(**meta)
        gate.log.write("run_end", by=gate.operator, run=rec.run_id, status=status, reason=reason, points=n,
                       duration_s=round(duration, 3))
        self._emit(event="end", run_id=rec.run_id, status=status, done=n, total=max(plan.points, n))
        if status == "done":
            msg = "run %s done: %d points, %s" % (rec.run_id, n, _dur(duration))
            if ended:
                msg += " (ended early: %s)" % "; ".join(ended[:3])
        else:
            msg = "run %s aborted after %d/%d points: %s" % (rec.run_id, n, plan.points, reason)
            if cleanup:
                msg += "; then " + "; ".join(cleanup)
        return RunReport(status, plan.name, msg, rec.run_id, n, duration, rec.rows,
                         str(rec.path) if rec.path else None, analysis=analysis, warnings=warnings)

    def _ran_with(self, plan: Plan, start: Dict[str, Any], names: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """These instruments' settings while the plan ran (as at its start, with its setup), kept with its readings'
        times: a reading measured at other settings (another integration time) isn't taken for one now. A setting it
        swept had no one value."""
        out: Dict[str, Dict[str, Any]] = {}
        for name in names:
            inst = self.lab.instruments.get(name)
            if inst is None:
                continue
            values = dict(start.get(name)) if isinstance(start.get(name), dict) else {}
            for step in plan.setup:
                if step.instr == name and step.param in inst.card.params:
                    try:
                        values[step.param] = inst.spec(step.param).coerce(step.value)
                    except ValueError:
                        values.pop(step.param, None)
            for axis in plan.axes:
                if getattr(axis, "instr", None) == name and axis.kind in ("instrument", "ramp"):
                    values[axis.param] = "swept"
            out[name] = settings_of(inst, values)
        return out

    def _long_wait(self, plan: Plan, step: Step, waited: float, warnings: List[str]) -> None:
        """A set point in setup that the instrument ramps to by itself held a time series until its reading got
        there: say how long, and that nothing was measured on the way, so the agent learns to write a ramp: sweep when
        the points on the way are the measurement ("log while it cools" once recorded only at 2 K). A plan that only
        cools, then measures at the end, wanted the wait."""
        if waited < LONG_WAIT_S or not any(a.kind == "time" for a in plan.axes):
            return
        try:
            inst, spec = self.lab.resolve(step.key)
        except KeyError:
            return
        if spec.ramp != "instrument" or not spec.readback:
            return
        warnings.append("setup: %s waited %s for %s.%s to reach %s before the first point; nothing was measured on "
                        "the way (a ramp: sweep measures while it ramps)" % (
                            step.key, _dur(waited), step.instr, spec.readback, spec.show(spec.coerce(step.value))))

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
                if getattr(self.lab.instruments.get(step.instr), "simulated", True):
                    continue   # a twin has nothing to stop
                try:   # the clean-up goes on whatever this instrument answers
                    out.append(self.gate.act(step.instr, step.param, in_plan=True).message)
                except Exception as e:
                    out.append("%s: %s" % (step.key, str(e).splitlines()[0][:160] if str(e) else type(e).__name__))
                continue
            if self.lab.config.safe_actions.get(step.key) == "hold":
                out.append("%s stays held: the lab config holds it on a stop (on_abort asked for %s)"
                           % (step.key, self.lab.resolve(step.key)[1].show(step.value)))
                continue
            r = self.gate._set(step.instr, step.param, step.value, False, by, record_undo=False)
            if r.action != "no-change":
                out.append(r.message)
        return out
