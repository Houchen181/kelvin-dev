"""What a run will do, for its approval card: every drive and setting it changes (from what, to what, and how fast,
as the instrument reports it), how long it takes, and where it leaves the lab when it ends.

Field tests found cards that hid what mattered: no ramp rate or duration, a cryostat cooling left off because it was
"toward safe", an output a queue never switched on, an output switched on into current mode, and nothing about what
stayed on at the end. A forecast walks a plan (or a queue) the way the run engine does it, setup in the engine's order,
the sweep, then the after steps, reading instruments but never sending them anything, so the person sees the whole run
before saying yes. Its duration counts ramps, settling and waits; what it can't count (a rate the instrument doesn't
tell, a stabilize that may wait longer, readings that take their own time, a reading taken again after a fault) the
card says, and so does what may end the run sooner (until: and while:), which makes its time the most it takes.

A ramp: sweep (plans.py) is walked as the engine runs it: each leg sets its rate (when it gives one) and its target
without waiting, then measures every `every` seconds until the reading gets there, and hold: seconds more. A leg takes
|Δ|/rate + hold, at the leg's rate, else the rate as the run has set it by then, else as the instrument reads now; it
records about duration/every points, and the plan's estimate of its points (Axis.estimate) is the forecast's."""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .gate import _dur, _same

# what a plan step that names one of the card's actions says: run it, once (a waveform started, a stage homed)
RUN_WORDS = {"run", "start", "do", "go", "yes", "on", "true", "1"}
# the parts of a plan this walk knows; another (a newer kind of step or condition) is shown with its time unknown
PLAN_KEYS = {"plan", "description", "device", "requires", "preconditions", "setup", "sweep", "repeat", "time", "settle",
             "measure", "after", "on_abort", "trigger", "until", "while", "retry"}
# words in a setting's name or label that say it decides how an output applies its levels
_MODE_WORDS = ("function", "func", "mode", "wave", "shape", "source", "polarity")


def runs(value: Any) -> bool:
    """A plan step's value that runs an action: run (or yes, on, true)."""
    return value is True or (not isinstance(value, bool) and str(value).strip().lower() in RUN_WORDS)


def action_of(lab: Any, instr: str, name: str) -> Any:
    """The card's action a plan step names (instr.action, not a parameter), or None."""
    inst = getattr(lab, "instruments", {}).get(instr)
    if inst is None or name in inst.card.params:
        return None
    return inst.card.actions.get(name)


def _and(words: List[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _read(gate: Any, inst: Any, spec: Any) -> Any:
    """As the gate sees it now; None when it can't be read (unknown)."""
    try:
        return gate._current(inst, spec)
    except Exception:
        return None


def read_now(gate: Any, key: str) -> Any:
    """instr.param as the gate sees it now; None when it can't be read."""
    try:
        return _read(gate, *gate.lab.resolve(key))
    except KeyError:
        return None


def rate_setting(inst: Any, spec: Any) -> Optional[Tuple[Any, float]]:
    """The setting that says how fast the instrument moves this set point by itself, and its time unit in seconds:
    the card's rate_param, else a readable setting in the set point's own unit per second or per minute (a PPMS's
    T_rate in K/min for T_set). None when the card has none, or several that could be it."""
    params = inst.card.params
    if spec.rate_param:
        rspec = params.get(spec.rate_param)
        return (rspec, 60.0 if spec.rate_per == "min" else 1.0) if rspec is not None else None
    if not spec.unit:
        return None
    found = [p for p in params.values() if p is not spec and p.numeric and p.get_cmd is not None
             and p.unit in (spec.unit + "/s", spec.unit + "/min")]
    if len(found) > 1:   # T_rate for T_set, over another setting in K/min: the one that shares its name
        stem = spec.name.split("_")[0].lower()
        found = [p for p in found if p.name.lower().startswith(stem)]
    if len(found) != 1:
        return None
    return found[0], 60.0 if found[0].unit.endswith("/min") else 1.0


def rate_parts(gate: Any, inst: Any, spec: Any, value: Any = None, source: str = "") -> Tuple[Optional[float], str, str]:
    """rate_words in pieces: (units per second, None when Kelvin can't tell; the rate as shown, "20 K/min"; where it
    comes from, or why it can't be told)."""
    found = rate_setting(inst, spec)
    if found is None:
        return None, "", "its card gives no rate Kelvin can read"
    rspec, per = found
    rkey = "%s.%s" % (inst.name, rspec.name)
    if value is None and not source:
        value, source = _read(gate, inst, rspec), "%s, read now" % rkey
    if not _number(value) or value <= 0:
        return None, "", "%s reads %s" % (rkey, rspec.show(value))
    return value / per, rspec.show(value), source


def rate_words(gate: Any, inst: Any, spec: Any, value: Any = None, source: str = "") -> Tuple[Optional[float], str]:
    """How fast the instrument moves a set point by itself, in units per second (None: Kelvin can't tell), and in
    words for a card: "at 20 K/min (ppms.T_rate, read now)". value, source: the rate as a run sets it, and who does."""
    per_s, shown, source = rate_parts(gate, inst, spec, value, source)
    if per_s is None:
        return None, "at its own rate (%s)" % source
    return per_s, "at %s (%s)" % (shown, source)


def mode_settings(inst: Any) -> List[Any]:
    """The settings that decide what an instrument's output applies (its source function, a waveform, a modulation
    source): the readable choices on its card that aren't drives, the likeliest first, at most four."""
    choices = [p for p in inst.card.params.values() if p.type == "enum" and not p.is_drive and p.get_cmd is not None]
    choices.sort(key=lambda p: not any(w in (p.name + " " + p.label).lower() for w in _MODE_WORDS))
    return choices[:4]


def mode_words(inst: Any, value_of: Callable[[str], Any], set_by: Set[str] = frozenset(), who: str = "") -> str:
    """How the instrument is set to apply its output, for a card that switches it on: "gate: source function current"
    (an output once went on into current mode with nothing on its card to say so)."""
    bits = []
    for p in mode_settings(inst):
        key = "%s.%s" % (inst.name, p.name)
        value = value_of(key)
        if value is not None:
            bits.append("%s %s%s" % (p.label.lower() if p.label else p.name, p.show(value),
                                     " (%s sets it)" % who if key in set_by else ""))
    return "%s: %s" % (inst.name, ", ".join(bits)) if bits else ""


def applies(inst: Any, spec: Any, value_of: Callable[[str], Any], set_by: Set[str] = frozenset()) -> str:
    """For a switch going on, what it applies to the sample: the levels it enables, which the gate sets to their safe
    values first unless the run sets them (a plan puts its own back), and the other drives that apply while it is on
    (a compliance), as they are now."""
    bits = []
    for d in [inst.spec(n) for n in spec.enables] + [p for p in inst.card.drives
                                                     if p.applies_when == spec.name and p.name not in spec.enables]:
        key = "%s.%s" % (inst.name, d.name)
        if key in set_by:
            bits.append(key)   # its own line says what the run sets it to
        elif d.name in spec.enables:
            bits.append("%s at %s (its safe value, set first)" % (key, d.show(d.safe)))
        else:
            now = value_of(key)
            bits.append("%s at %s (as it is now)" % (key, d.show(now)) if now is not None else key)
    return "it applies " + _and(bits) if bits else ""


@dataclass
class Change:
    """One drive or setting a run changes."""
    key: str
    start: Any                                         # before the run (None: unknown)
    values: List[Any] = field(default_factory=list)    # every value the run sets it to, in order (Forecast.uses)
    seconds: float = 0.0                               # time spent moving it (a ramp, the instrument's own move)
    untimed: bool = False                              # it moves at a rate Kelvin can't read: not in the duration


class Forecast:
    """A run walked through before it starts, for its approval card. Forecast(engine).plan(plan) for a plan; for a
    queue (who="the queue") plan() and step() in turn, each starting where the one before leaves the lab."""

    def __init__(self, engine: Any, who: str = "the plan"):
        self.engine, self.gate, self.lab, self.who = engine, engine.gate, engine.lab, who
        self.before: Dict[str, Any] = {}        # each key as it is now, read once
        self.now: Dict[str, Any] = {}           # each key as the run leaves it so far
        self.changes: Dict[str, Change] = {}    # what the run changes
        self.uses: Dict[str, List[Any]] = {}    # every value the run sets each key to
        self.order: List[str] = []              # the keys as the run's text names them, in order
        self.set_by: Set[str] = set()           # every key the run sets (a rate it sets is its own, not read now)
        self.seconds = 0.0
        self.waits: List[str] = []              # stabilize waits: counted at their least, may take longer
        self.slow_reads: List[str] = []         # readings that take their own time (traces, frames, acquisitions)
        self.retries: List[str] = []            # readings a plan takes again after a fault (retry:): time unknown
        self.sooner: List[str] = []             # what may end a loop or the run early (until:, while:)
        self.actions: List[Tuple[str, str, str]] = []   # (setup or after, instrument, action), run once
        self.unknown: List[str] = []            # parts of the run the walk doesn't know: shown, their time unknown
        self.asked: List[str] = []              # the card's lines a person approves (gate.plan_approval sets them)
        self._asked: Dict[str, Any] = {}        # the levels the plan being walked has set (a switch puts them back)
        self._untimed: Dict[str, None] = {}     # keys moved at a rate Kelvin can't tell, in order: not in the time
        self._on_stop: Dict[str, List[Any]] = {}   # the settings plans' on_abort make after a stop (a queue covers them)
        self._rates: Dict[str, List[Tuple[str, str]]] = {}   # each instrument-ramped key's rates: (shown, from where)
        self._name = "the plan"                 # the plan being walked

    # ------------------------------------------------------------------ the walk

    def value(self, key: str) -> Any:
        if key not in self.now:
            self.before[key] = self.now[key] = read_now(self.gate, key)
        return self.now[key]

    def start(self, key: str) -> Any:
        """The key's value before the run, as this forecast read it."""
        if key not in self.before:
            self.value(key)
        return self.before[key]

    def rate(self, inst: Any, spec: Any) -> Tuple[Optional[float], str]:
        """rate_words, with the rate as the run has set it by now, else as read from the instrument."""
        per_s, shown, source = self._rate_parts(inst, spec)
        return (per_s, "at %s (%s)" % (shown, source)) if per_s is not None else (None, "at its own rate (%s)" % source)

    def _rate_parts(self, inst: Any, spec: Any) -> Tuple[Optional[float], str, str]:
        found = rate_setting(inst, spec)
        if found is None:
            return rate_parts(self.gate, inst, spec)
        rkey = "%s.%s" % (inst.name, found[0].name)
        source = "%s sets %s" % (self.who, rkey) if rkey in self.set_by else "%s, read now" % rkey
        return rate_parts(self.gate, inst, spec, self.value(rkey), source)

    def travel(self, inst: Any, spec: Any, a: Any, b: Any) -> Tuple[float, bool]:
        """Seconds to move a drive or setting from a to b as the gate does (ramped at the lab's rate, or followed while
        the instrument moves it at its own), and whether Kelvin could count them. 0 for a switch or a choice."""
        if not spec.numeric or b is None or (a is not None and _same(spec, a, b)):
            return 0.0, True
        if spec.ramp == "gate":
            rate = inst.rate(spec.name)
            return (abs(b - a) / rate if rate and a is not None else 0.0), True
        if spec.ramp == "instrument":
            rate, shown, source = self._rate_parts(inst, spec)
            if rate and a is not None:
                moves = self._rates.setdefault("%s.%s" % (inst.name, spec.name), [])
                if (shown, source) not in moves:   # the rates it moves at, in order, for its line on the card
                    moves.append((shown, source))
                return abs(b - a) / rate, True
            return 0.0, False
        return 0.0, True

    def _note(self, key: str) -> None:
        if key not in self.order:
            self.order.append(key)

    def _record(self, inst: Any, spec: Any, value: Any, seconds: float = 0.0, timed: bool = True) -> None:
        key = "%s.%s" % (inst.name, spec.name)
        start = self.start(key)
        self.set_by.add(key)
        if not timed:   # its time isn't in the duration, even when the set changes nothing Kelvin can see
            self._untimed[key] = None
        used = self.uses.setdefault(key, [])   # every value it is set to, the first one that changes nothing too
        used.append(value)
        change = self.changes.get(key)
        if change is None and start is not None and _same(spec, start, value) and not seconds:
            self.now[key] = value   # set to what it already is: no change
            return
        if change is None:
            change = self.changes[key] = Change(key, start, used)
        change.seconds += seconds
        change.untimed = change.untimed or not timed
        self.now[key] = value

    def _set(self, inst: Any, spec: Any, target: Any) -> float:
        """One change as the gate makes it (SafetyGate._set, _move, _reapply): a switch going on parks its levels at
        safe and puts back the ones the plan set; going off, it brings them down first. The seconds it takes."""
        key = "%s.%s" % (inst.name, spec.name)
        current = self.value(key)
        seconds = 0.0
        if spec.is_drive and spec.enables and not (current is not None and _same(spec, current, target)):
            on = spec.distance(target) > 0
            for name in spec.enables:
                dep = inst.spec(name)
                dkey = "%s.%s" % (inst.name, name)
                now = self.value(dkey)
                level = self._asked.get(dkey)
                if on and level is not None and dep.distance(level) > 0:   # parked, then put back
                    t, timed = self.travel(inst, dep, dep.safe, level)
                    self._record(inst, dep, level, t, timed)
                    seconds += t
                elif on and (now is None or dep.distance(now) > 0):         # parked at safe
                    self._record(inst, dep, dep.safe)
                elif not on and now is not None and dep.distance(now) > 0:  # brought down first
                    t, timed = self.travel(inst, dep, now, dep.safe)
                    self._record(inst, dep, dep.safe, t, timed)
                    seconds += t
        t, timed = self.travel(inst, spec, current, target)
        self._record(inst, spec, target, t, timed)
        if spec.is_drive:
            self._asked[key] = target
        return seconds + t

    def _step(self, instr: str, param: str, value: Any, where: str) -> None:
        if action_of(self.lab, instr, param) is not None:
            self.actions.append((where, instr, param))
            return
        try:
            inst, spec = self.lab.resolve("%s.%s" % (instr, param))
            target = spec.coerce(value)
        except (KeyError, ValueError):
            return
        self.seconds += self._set(inst, spec, target)

    def plan(self, plan: Any) -> "Forecast":
        """A plan as the engine runs it: its setup (switches before the levels they apply), its sweep, its after
        steps. Readings that take their own time are noted, not counted. A part of the plan this walk doesn't know (a
        newer kind of step or condition) never stops the card: it is shown, and its time is unknown."""
        self._asked = {}
        self._name = getattr(plan, "name", "") or "the plan"
        try:
            self._walk(plan)
        except Exception as e:   # the card still shows what the gate found; the time is the walk's so far
            self.unknown.append("the rest of %s (%s)" % (self._name,
                                                         str(e).splitlines()[0][:120] if str(e) else type(e).__name__))
        self.unknown += ["%s's %s" % (self._name, key)
                         for key in getattr(plan, "source", None) or {} if key not in PLAN_KEYS]
        self._asked = {}
        return self

    def _walk(self, plan: Any) -> None:
        for key in [s.key for s in plan.setup] + [a.key for a in plan.axes if a.kind in ("instrument", "ramp")] \
                + [s.key for s in plan.after]:
            self._note(key)
        for step in self.engine.ordered(plan.setup):
            self._step(step.instr, step.param, step.value, "setup")
        outer = 1
        for axis in plan.axes:
            n = len(axis.values)
            if axis.kind == "instrument":
                self._axis(plan, axis, outer)
            elif axis.kind == "ramp":   # always the innermost loop
                self._ramp(axis, outer)
            elif axis.kind == "time":
                if n > 1:   # it waits `every` before each of its points but the first
                    self.seconds += outer * (n - 1) * axis.every
                self._ends(axis, "the time series%s ends" % self._in())
            elif axis.kind != "repeat":
                self._unknown_axis(axis)
            outer *= max(n, 1)
        self._ends(plan, "the sweep%s ends" % self._in())
        retry = getattr(plan, "retry", None)
        if retry is not None:   # only when a reading fails: how often, nobody can tell
            self.retries.append("readings taken again after a fault (%sretry: up to %d time%s, %s apart)" % (
                "" if self.who == "the plan" else self._name + "'s ", retry.times, "" if retry.times == 1 else "s",
                _dur(retry.wait)))
        for step in plan.after:
            self._step(step.instr, step.param, step.value, "after")
        self._after_a_stop(plan)
        for instr, param in plan.measure:
            try:
                spec = self.lab.resolve("%s.%s" % (instr, param))[1]
            except KeyError:
                continue
            if spec.type in ("trace", "image") or spec.acquires or spec.get_before:
                self.slow_reads.append("%s.%s" % (instr, param))

    def _after_a_stop(self, plan: Any) -> None:
        """The settings a plan's on_abort makes after a stop (its drives are a queue's whole path already): kept apart
        from the run's own values, for a queue's card (settings(every=True)), so a stop in the night doesn't ask about
        a draft card's setting in its clean-up. Nothing is walked: a stop is not the run's path."""
        for step in getattr(plan, "on_abort", None) or []:
            if action_of(self.lab, step.instr, step.param) is not None:
                continue
            try:
                spec = self.lab.resolve(step.key)[1]
                value = spec.coerce(step.value)
            except (KeyError, ValueError):
                continue
            if not spec.is_drive:
                self.start(step.key)
                self._on_stop.setdefault(step.key, []).append(value)

    def _unknown_axis(self, axis: Any) -> None:
        """A kind of sweep this walk doesn't know: the drive it names is shown with its values, its time unknown."""
        key = "%s.%s" % (axis.instr, axis.param) if getattr(axis, "instr", "") else getattr(axis, "key", "") or ""
        try:
            inst, spec = self.lab.resolve(key)
            self._note(key)
            for v in getattr(axis, "values", None) or []:
                self._record(inst, spec, spec.coerce(v), 0.0, timed=False)
        except Exception:
            pass
        self.unknown.append("%s's %s sweep" % (key or "a drive", getattr(axis, "kind", "new")))

    def _in(self) -> str:
        """Which plan a line is about, on a queue's card (a plan's own card names it in its title)."""
        return "" if self.who == "the plan" else " (in %s)" % self._name

    def _ends(self, part: Any, what: str) -> None:
        """A loop's or the plan's until: or while:, which may end it early: the run's time is then the most it
        takes, and the card says what may cut it short."""
        until, while_ = getattr(part, "until", None), getattr(part, "while_", None)
        if until is not None:
            self.sooner.append("%s once %s" % (what, until.text))
        if while_ is not None:
            self.sooner.append("%s once %s no longer holds" % (what, while_.text))

    def _wait_words(self, st: Any, where: str) -> str:
        """A stabilize, for the card's line of what the time doesn't count: counted at its least; with near:, it
        waits out its timeout wherever the reading doesn't come near its set point."""
        if st.near is None:
            return "%s's stabilize (counted at its %s%s, up to %s)" % (st.key, _dur(st.duration), where,
                                                                      _dur(st.timeout))
        try:
            near = self.lab.resolve(st.key)[1].show(st.near)
        except KeyError:
            near = "%g" % st.near
        return "%s's stabilize (counted at its %s%s; up to its %s timeout wherever %s doesn't come within %s of its " \
               "set point)" % (st.key, _dur(st.duration), where, _dur(st.timeout), st.key, near)

    def _axis(self, plan: Any, axis: Any, outer: int) -> None:
        """A sweep axis: set at each of its values once per pass, a pass for every point of the axes outside it, each
        followed by its stabilize and its settle (its own for a step that has one: settle_at). It travels its values
        in order, and back to the first between passes. A value the same as the one before is not set again, so it
        doesn't settle again either."""
        try:
            inst, spec = self.lab.resolve(axis.key)
            values = [spec.coerce(v) for v in axis.values]
        except (KeyError, ValueError):
            return
        if not values:
            return
        n = len(values)
        moves = [k for k, v in enumerate(values) if not (k and v == values[k - 1])]   # as the engine sets them
        holds = [axis.settle_at(k) if hasattr(axis, "settle_at") else axis.settle for k in moves]
        self.seconds += outer * sum(h if h is not None else (plan.settle or 0.0) for h in holds)
        if axis.stabilize is not None:
            st = axis.stabilize
            self.seconds += outer * len(moves) * st.duration
            self.waits.append(self._wait_words(st, " at each point"))
        self._ends(axis, "%s%s skips its other values" % (axis.key, self._in()))
        if not spec.numeric:   # a switch or a choice: set as the gate does, once per value
            for v in values:
                self.seconds += self._set(inst, spec, v)
            return
        first, timed = self.travel(inst, spec, self.value(axis.key), values[0])
        steps = [self.travel(inst, spec, a, b) for a, b in zip(values, values[1:])]
        back = self.travel(inst, spec, values[-1], values[0]) if n > 1 else (0.0, True)
        t = first + outer * sum(s for s, _ in steps) + (outer - 1) * back[0]
        timed = timed and all(ok for _, ok in steps) and back[1]
        self.seconds += t
        for k, v in enumerate(values):
            self._record(inst, spec, v, t if k == 0 else 0.0, timed)
        if spec.is_drive:
            self._asked[axis.key] = values[-1]

    def _ramp(self, axis: Any, outer: int) -> None:
        """A ramp: sweep, the innermost loop, once per pass of the loops outside it. Each leg sets its rate on the
        card's rate parameter (a drive itself on a magnet supply, so on the card with the run's other changes), then
        its target without waiting, and measures until the reading gets there and hold: seconds more: |Δ|/rate + hold,
        at the leg's rate, else the rate as the run has set it by then, else as the instrument reads now. The first
        pass starts where the reading is (or where the run has left the set point); every other one where the last
        leg ended. It records about duration/every points, one more for each leg: the plan's estimate (Axis.estimate)
        of its points, once the forecast can time every leg."""
        inst, spec = self.lab.resolve(axis.key)
        rspec = inst.card.params.get(spec.rate_param) if spec.rate_param else None
        legs = [(spec.coerce(leg.to), rspec.coerce(leg.rate) if leg.rate is not None and rspec is not None else None,
                 float(leg.hold or 0.0)) for leg in axis.segments]
        if not legs:
            return
        self._ends(axis, "%s's ramp%s ends" % (axis.key, self._in()))
        start = self._ramp_start(axis, inst, spec)
        timed, points = True, 0
        for first, times in ((True, 1), (False, outer - 1)):   # the passes after the first are all alike
            if times <= 0:
                continue
            at, took = (start if first else legs[-1][0]), 0.0
            for to, rate, hold in legs:
                if rate is not None:   # the leg's rate, set before its target
                    self.seconds += times * self._set(inst, rspec, rate)
                t, ok = self.travel(inst, spec, at, to)
                timed = timed and ok
                self._record(inst, spec, to, times * t, ok)
                self.seconds += times * (t + hold)
                took, at = took + t + hold, to
            if axis.every:   # a point every `every` seconds, and one where each leg begins
                points += times * (int(took / axis.every) + len(legs))
        if spec.is_drive:
            self._asked[axis.key] = legs[-1][0]
        if timed and points:
            axis.estimate = max(1, int(round(points / max(outer, 1))))

    def _ramp_start(self, axis: Any, inst: Any, spec: Any) -> Any:
        """Where a ramp's first leg starts: where the run has left its set point (a set point in setup waits for its
        reading; so does a queue step's), else the reading the ramp follows as it is now (a cryostat at 300 K whose
        set point says 2 K), else the set point."""
        if axis.key in self.set_by:
            return self.now.get(axis.key)
        followed = getattr(self.engine, "followed", None)
        reading = followed(axis) if followed is not None else None
        if reading:
            try:
                unit = self.lab.resolve(reading)[1].unit
            except KeyError:
                unit = None
            if unit is not None and (not unit or not spec.unit or unit == spec.unit):
                now = read_now(self.gate, reading)
                if _number(now):
                    return now
        return self.value(axis.key)

    def queue_step(self, step: Any) -> "Forecast":
        """A queue step (scanqueue.Step) between scans. A part of it this walk doesn't know is shown, its time
        unknown."""
        known = {"name", "sets", "stabilize", "wait"}
        for f in dataclasses.fields(step) if dataclasses.is_dataclass(step) else []:
            if f.name not in known and getattr(step, f.name) not in (None, False, 0, "", [], {}, ()):
                self.unknown.append("%s's %s" % (getattr(step, "name", "a step"), f.name))
        try:
            return self.step(step.sets, step.stabilize, step.wait)
        except Exception:
            self.unknown.append("%s" % getattr(step, "name", "a step"))
            return self

    def step(self, sets: List[Tuple[str, str, Any]], stabilize: Any = None, wait: float = 0.0) -> "Forecast":
        """A queue step between scans: its settings in order, its stabilize (counted at its least) and its wait."""
        for instr, param, _ in sets:
            self._note("%s.%s" % (instr, param))
        for instr, param, value in sets:
            self._step(instr, param, value, "setup")
        if stabilize is not None:
            self.seconds += stabilize.duration
            self.waits.append(self._wait_words(stabilize, ""))
        self.seconds += wait or 0.0
        return self

    # ------------------------------------------------------------------ the card

    def _kind(self, drives: bool) -> Dict[str, Change]:
        out = {}
        for key in self.order + [k for k in self.changes if k not in self.order]:
            if key not in self.changes or key in out:
                continue
            try:
                if self.lab.resolve(key)[1].is_drive == drives:
                    out[key] = self.changes[key]
            except KeyError:
                continue
        return out

    def drives(self) -> Dict[str, Change]:
        """The drives the run changes, in the order its text names them."""
        return self._kind(True)

    def settings(self, every: bool = False) -> Dict[str, Change]:
        """The settings (not drives) the run changes: on a draft card each asks, so the run's card covers them.
        every: also those it only sets to what they are now, and those an on_abort sets after a stop, for a queue's
        card (its whole path): in the night an item may find one changed (at the front panel), and setting it back, or
        a clean-up after a trip, must not ask."""
        out = self._kind(False)
        for key in (list(self.uses) + [k for k in self._on_stop if k not in self.uses]) if every else ():
            try:
                if self.lab.resolve(key)[1].is_drive:
                    continue
            except KeyError:
                continue
            values = self.uses.get(key, []) + self._on_stop.get(key, [])
            if key not in out or len(values) > len(out[key].values):   # every value it may set, after a stop too
                out[key] = Change(key, out[key].start if key in out else self.before.get(key), values)
        return out

    def detail(self, key: str) -> str:
        """What a line on the card adds for this drive or setting: the rate the instrument moves it at, read from the
        instrument (or as the run sets it), and for how long; for a switch going on, what it applies and how the
        instrument is set to apply it. Never more than the line, whatever goes wrong."""
        try:
            return self._detail(key)
        except Exception:
            return ""

    def _detail(self, key: str) -> str:
        change = self.changes.get(key)
        if change is None:
            return ""
        inst, spec = self.lab.resolve(key)
        bits = []
        if spec.ramp == "instrument" and spec.numeric:
            words = self._rate_text(key) or self.rate(inst, spec)[1]
            if change.seconds >= 1 and not change.untimed:
                words += ", ~%s" % _dur(change.seconds)
            bits.append(words)
        if spec.is_drive and spec.enables and any(spec.distance(v) > 0 for v in change.values):
            bits.append(applies(inst, spec, self.start, self.set_by))
            bits.append(mode_words(inst, self.value, self.set_by, self.who))
        return "; ".join(b for b in bits if b)

    def _rate_text(self, key: str) -> str:
        """The rates the run moves an instrument-ramped key at, in order, each group with where it comes from: "at
        0.5 T/min, then 0.25 T/min (the plan sets magnet.rate)" for a ramp whose legs set their own. "" when the walk
        timed none of its moves."""
        groups: List[Tuple[List[str], str]] = []
        for shown, source in self._rates.get(key, []):
            if groups and groups[-1][1] == source:
                if shown not in groups[-1][0]:
                    groups[-1][0].append(shown)
            else:
                groups.append(([shown], source))
        return "at " + ", then ".join("%s (%s)" % (", then ".join(shown), source) for shown, source in groups) \
            if groups else ""

    def driving_actions(self) -> List[str]:
        """The actions the run starts once that drive the sample (a waveform started, a stage homed): instr.action."""
        out = []
        for _, instr, name in self.actions:
            action = action_of(self.lab, instr, name)
            if action is not None and action.drives and "%s.%s" % (instr, name) not in out:
                out.append("%s.%s" % (instr, name))
        return out

    def action_lines(self, keys: Optional[List[str]] = None) -> List[str]:
        """A line for each of these driving actions (default: all of them): a person approves it with the run, as
        do_action would ask on its own."""
        keys = self.driving_actions() if keys is None else keys
        out = []
        for where, instr, name in self.actions:
            key = "%s.%s" % (instr, name)
            if key in keys:
                out.append("%s: %s (%s), which drives the sample" % (
                    "At the start" if where == "setup" else "At the end", key, action_of(self.lab, instr, name).label
                    or name))
        return out

    def closing(self) -> List[str]:
        """The card's last lines: drives whose switch stays off (they never reach the sample), what the run leaves on
        and what is back at safe when it ends, what its duration doesn't count, and what may end it sooner. A card is
        never lost to them."""
        lines: List[str] = []
        for part in (self._dark, self._end, self._uncounted, self._sooner):
            try:
                lines += part()
            except Exception:
                continue
        return lines

    def _dark(self) -> List[str]:
        """Drives the run sets away from safe whose switch is off and stays off: they never reach the sample (a queue
        swept a gate whose output was off, and its card didn't say)."""
        dark: Dict[str, List[str]] = {}
        started = {key.split(".")[0] for key in self.driving_actions()}   # an action it runs may switch it on
        for key, change in self.drives().items():
            inst, spec = self.lab.resolve(key)
            switch = self.gate._switch_of(inst, spec)
            if switch is None or inst.name in started or not any(spec.distance(v) > 0 for v in change.values):
                continue
            skey = "%s.%s" % (inst.name, switch.name)
            state = self.start(skey)
            on_later = skey in self.changes and any(switch.distance(v) > 0 for v in self.changes[skey].values)
            if state is not None and switch.distance(state) == 0 and not on_later:
                dark.setdefault(skey, []).append(key)
        out = []
        for skey, keys in dark.items():
            switch = self.lab.resolve(skey)[1]
            out.append("%s is %s, and %s doesn't switch it on: %s won't reach the sample" % (
                skey, switch.show(switch.safe), self.who, _and(keys)))
        return out

    def _end(self) -> List[str]:
        """What the run leaves away from safe when it ends, and what is back at safe."""
        away, safe = [], []
        for key in self.drives():
            spec = self.lab.resolve(key)[1]
            end = self.now.get(key)
            if end is not None:
                (safe if spec.distance(end) == 0 else away).append("%s %s" % (key, spec.show(end)))
        if away:
            ends = ["it leaves " + _and(away)] + (["back at safe: " + ", ".join(safe)] if safe else [])
        else:
            ends = ["everything it changed is back at safe: " + ", ".join(safe)] if safe else []
        for where, instr, name in self.actions:   # what an action does, Kelvin can't tell: it says which ran last
            if where == "after":
                label = getattr(action_of(self.lab, instr, name), "label", "") or name
                ends.append("%sit runs %s.%s (%s)" % ("then " if ends else "", instr, name, label))
        return ["At the end " + "; ".join(ends)] if ends else []

    def not_counted(self) -> List[str]:
        """What the duration leaves out: a ramp at a rate Kelvin can't read (or from where it can't read), stabilize
        waits beyond their least, readings that take their own time, readings taken again after a fault, and parts of
        the run the walk doesn't know."""
        missing = []
        for key in self._untimed:
            if not any(u.startswith(key + "'s ") for u in self.unknown):
                rate, _, why = self._rate_parts(*self.lab.resolve(key))
                missing.append("%s's ramp (%s)" % (key, why if rate is None else "where it starts can't be read"))
        missing += self.waits
        if self.slow_reads:
            missing.append("the time reading %s takes" % _and(self.slow_reads))
        return missing + self.retries + self.unknown

    def _uncounted(self) -> List[str]:
        missing = self.not_counted()
        if not missing:
            return []
        return ["Not in %s: %s" % ("the ~" + _dur(self.seconds) if self.seconds >= 1 else "its time", "; ".join(missing))]

    def _sooner(self) -> List[str]:
        return ["It may end sooner: " + "; ".join(self.sooner)] if self.sooner else []

    def duration(self) -> str:
        """For a card's title: ", ~13 min"; ", at least ~13 min" when something isn't counted (not_counted); ", at
        most ~13 min" when an until: or while: may end it sooner; both: ", ~13 min (it may end sooner or wait
        longer)"; ", time not known" when nothing is counted; "" for a run that takes no time Kelvin knows of."""
        try:
            missing = bool(self.not_counted())
        except Exception:
            missing = True
        if self.seconds >= 1:
            if missing and self.sooner:
                return ", ~%s (it may end sooner or wait longer)" % _dur(self.seconds)
            return (", at least ~%s" if missing else ", at most ~%s" if self.sooner else ", ~%s") % _dur(self.seconds)
        return ", time not known" if missing else ""
