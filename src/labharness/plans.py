"""Plans: declarative experiments the run engine executes without an LLM turn per point.

    plan: gate_sweep
    device: D3                               # the sample measured (default: the device mounted in the lab)
    preconditions: ["tc.T_sample < 2.5"]
    setup:   { lockin.amplitude: 0.05, smu.output: on }
    sweep:                                   # several keys = nested loops, outer first
      magnet.B: { values: [0, 0.5, 1], stabilize: { read: tc.T_sample, within: 0.02, for: 30 } }
      smu.V:    { from: 0, to: 10, step: 0.05, back: true }   # there and back (hysteresis)
      # also: { from: 1e3, to: 1e6, points: 31, log: true } and { values: [...] }
    repeat: 3                                # repeat the whole sweep (adds a `repeat` column)
    time: { every: 2, for: 600 }             # innermost time series (adds a `time` column)
    settle: 0.3
    trigger: [dmm.arm, gen.fire]             # the card's actions, at every point, after settling, before measuring
    measure: [lockin.X, lockin.Y, smu.I]
    after:   { smu.V: 0 }
    on_abort: { smu.V: 0 }                   # added after every drive the plan touched has gone to safe

A set point the instrument ramps to by itself (a cryostat's temperature, a magnet's field) waits in setup: or on a
stepped sweep until its reading gets there, and nothing is measured on the way. A ramp sweep measures on the way:

    sweep:
      ppms.T_set: { ramp: [{to: 10, rate: 10}, {to: 2, rate: 1, hold: 600}], every: 10 }

sets each leg's target (and rate, in the card's rate parameter's unit) without waiting, measures every 10 s until
the reading (the card's readback, or read:) is within its tolerance (or within:), stays hold: seconds more, then the
next leg. It is the innermost loop, and its column is the reading it follows. A ramp that stops getting closer for
stall: seconds (default 600; 0: never) stops the run. Also:

    smu.V:  { from: 0, to: 10, step: 0.1, until: "abs(smu.I) > 1e-6" }   # skip this axis's other values once true
    tc.setpoint: { values: [2, 5, 10, 50], settle: {50: 600} }           # a hold per step: a list, or value: seconds
    stabilize: { read: tc.T, within: 0.01, for: 60, every: 10, near: 0.05 }   # log while waiting; near the set point
    time: { every: 10, for: 7200, until: "tc.T_sample < 2.1" }
    until: "nvm.V < 1e-6"                    # the whole sweep ends once this holds (also while:), checked each point
    retry: { times: 3, wait: 10 }            # a reading that fails is tried again; the time lost is marked (gap_s)

A card's hardware-timed technique (techniques.py: cyclic voltammetry, a scope's burst) runs at every point, after the
triggers and before the readings, its buffer kept as traces (pstat.cv.I, against pstat.cv.E); a sweep can step one
of its parameters:

    technique: { pstat.cv: { start: -0.2, vertex: 0.6, cycles: 2 } }
    sweep:     { pstat.cv.rate: { values: [0.02, 0.05, 0.1] } }

As Special Measure's loops do, a stepped loop can move other settings with its own, read something once per value,
and wait longer after its first value (where it lands after jumping back from its last):

    sweep:
      dac.Vbg: { from: 5, to: -5, points: 21, measure: [smu.I] }      # smu.I read once per value of dac.Vbg
      stage.y: { from: 70, to: 72, points: 101, with: { stage.x: { from: 93, to: 81 } }, first_settle: 3 }

with: gives each setting {from, to} in step with the loop's own values (or values: one per value), and they move
together, in turn a ramp step at a time. A sweep key without a dot is a variable of the plan's own, and sets: works
out settings from the variables and the settings swept (Special Measure's function channels, such as a density n and
a displacement field E for two gates); every value it gives them is worked out first, checked and approved with the
rest:

    sweep: { E: { from: -0.3, to: 0.3, points: 7 }, n: { from: 0, to: 2, points: 101 } }
    sets:  { dac.Vbg: "1.18*n - 8.4*E + 2.4", dac.Vtg: "0.95*n + 7.6*E + 2.6" }
"""
from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import expr
from .conditions import Condition, Rule, parse_condition, parse_rule
from .labconfig import read_yaml

MAX_POINTS = 1_000_000
MAX_COMPUTED = 200_000   # the combinations of a plan's values its sets: are worked out at before it runs
STALL_S = 600.0     # a ramp whose reading stops getting closer to its target for this long stops the run
_KEYS = {"plan", "description", "device", "requires", "preconditions", "setup", "sweep", "repeat", "time",
         "settle", "measure", "after", "on_abort", "trigger", "until", "while", "retry", "technique", "sets"}
_STEPPED = {"values", "from", "to", "step", "points", "log", "back", "settle", "stabilize", "until", "while", "with",
            "measure", "first_settle"}
VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED = {"i", "t", "time", "repeat", "stabilizing", "gap_s"}   # a run's own columns: never a variable's name
_RAMPED = {"ramp", "rate", "every", "read", "within", "stall", "until", "while"}


class PlanError(ValueError):
    def __init__(self, errors: List[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass
class Step:
    instr: str
    param: str
    value: Any

    @property
    def key(self) -> str:
        return "%s.%s" % (self.instr, self.param)


@dataclass
class Stabilize:
    """Before measuring, wait until a reading changes by less than `within` over `duration` seconds; with near, also
    until it is that close to its set point (a steady reading short of it, or a dead sensor, is not settled). every:
    a point is recorded that often while it waits (marked stabilizing: 1), not only once it has settled."""
    instr: str
    param: str
    within: float
    duration: float
    timeout: float = 900.0
    every: float = 0.0
    near: Optional[float] = None

    @property
    def key(self) -> str:
        return "%s.%s" % (self.instr, self.param)


@dataclass
class Segment:
    """One leg of a ramp: the target it is set to, the rate (in the unit of the card's rate parameter; None: as the
    instrument is set), and how long to go on measuring once the reading is there."""
    to: Any
    rate: Optional[float] = None
    hold: float = 0.0


@dataclass
class TechniqueStep:
    """A card's technique the plan runs at every point (techniques.py), with the values it gives its parameters."""
    instr: str
    name: str
    values: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return "%s.%s" % (self.instr, self.name)


@dataclass
class Retry:
    """A reading that fails for a reason that can pass (no answer, a garbled one) is tried again, up to `times` times,
    `wait` seconds apart; the time lost is marked on the next point (gap_s)."""
    times: int
    wait: float


@dataclass
class With:
    """A setting that moves with a loop's own (Special Measure's channels set together in one loop: a stage's x and
    y along a line, a monochromator and the modulator tuned to its wavelength): a value of its own at each of the
    loop's points."""
    instr: str
    param: str
    values: List[Any]

    @property
    def key(self) -> str:
        return "%s.%s" % (self.instr, self.param)


@dataclass
class Computed:
    """A setting the plan works out at each point (sets:) from its variables and the settings it sweeps, as Special
    Measure's function channels do (two gates from a density and a field). It reads nothing, so every value it takes
    is known before the run: values, in the order the run takes them (made when there are few enough)."""
    instr: str
    param: str
    text: str
    names: List[str]
    values: List[Any] = field(default_factory=list)

    @property
    def key(self) -> str:
        return "%s.%s" % (self.instr, self.param)


@dataclass
class Axis:
    instr: str
    param: str
    values: List[Any]                   # a ramp: its legs' targets
    settle: Optional[float] = None
    kind: str = "instrument"            # instrument | ramp | repeat | time | technique | variable
    every: float = 0.0                  # time axis: seconds between points; a ramp: between points while it ramps
    stabilize: Optional[Stabilize] = None
    settles: Optional[List[Optional[float]]] = None   # a hold of its own for each value (None: the axis's settle)
    until: Optional[Rule] = None        # its remaining values are skipped once this holds after a point...
    while_: Optional[Rule] = None       # ... or once this no longer holds
    segments: List[Segment] = field(default_factory=list)   # a ramp's legs
    read: Optional[Tuple[str, str]] = None    # the reading a ramp follows (None: its card's readback)
    within: Optional[float] = None      # how close to a leg's target counts as there (None: its card's tol)
    stall: float = STALL_S              # a ramp stops the run after this long without getting closer (0: never)
    estimate: Optional[int] = None      # a ramp: how many points it is expected to record (the engine works it out)
    with_: List[With] = field(default_factory=list)   # settings that move with it, a value each at each of its points
    measure: List[Tuple[str, str]] = field(default_factory=list)   # read once at each of its values, before the
    # loops inside it, and kept on each point inside (Special Measure's readings in an outer loop)
    first_settle: Optional[float] = None   # the hold after its first value, each time the loop starts again

    @property
    def key(self) -> str:
        return "%s.%s" % (self.instr, self.param) if self.kind in ("instrument", "ramp", "technique") else self.param

    def settle_first(self, i: int) -> Optional[float]:
        """The hold after setting value i as the run takes it: first_settle for its first value, else settle_at."""
        return self.first_settle if i == 0 and self.first_settle is not None else self.settle_at(i)

    @property
    def count(self) -> int:
        """Points along this axis: a ramp's estimate once the engine has made one, else one per leg."""
        return self.estimate if self.kind == "ramp" and self.estimate else len(self.values)

    def settle_at(self, i: int) -> Optional[float]:
        """The hold after setting value i: its own (settle: as a list or value: seconds), else the axis's."""
        own = self.settles[i] if self.settles and i < len(self.settles) else None
        return own if own is not None else self.settle


@dataclass
class Plan:
    name: str
    description: str = ""
    requires: List[str] = field(default_factory=list)
    preconditions: List[Condition] = field(default_factory=list)
    setup: List[Step] = field(default_factory=list)
    axes: List[Axis] = field(default_factory=list)
    measure: List[Tuple[str, str]] = field(default_factory=list)
    trigger: List[Tuple[str, str]] = field(default_factory=list)   # the cards' actions, at every point
    settle: float = 0.0
    after: List[Step] = field(default_factory=list)
    on_abort: Optional[List[Step]] = None
    source: Dict[str, Any] = field(default_factory=dict)
    device: str = ""                  # the sample this run measures, when it is not the mounted one
    until: Optional[Rule] = None      # the sweep ends (after: runs, the run is done) once this holds after a point
    while_: Optional[Rule] = None     # ... or once this no longer holds
    retry: Optional[Retry] = None
    techniques: List[TechniqueStep] = field(default_factory=list)   # run at every point (techniques.py)
    sets: List[Computed] = field(default_factory=list)   # settings worked out at each point (sets:)

    def swept_settings(self) -> List[Tuple[str, List[Any]]]:
        """Every setting the stepped sweep moves, with each value it takes: each loop's own (a variable is the plan's,
        no setting), what moves with it (with:) and what sets: works out. A ramp's targets are its own (Plan.ramp)."""
        out: List[Tuple[str, List[Any]]] = []
        for a in self.axes:
            if a.kind == "instrument":
                out.append((a.key, a.values))
            out += [(w.key, w.values) for w in a.with_]
        return out + [(c.key, c.values) for c in self.sets]

    @property
    def points(self) -> int:
        """The points it measures: exact for stepped sweeps; with a ramp, an estimate (see Axis.count)."""
        n = 1
        for a in self.axes:
            n *= a.count
        return n

    @property
    def instrument_axes(self) -> List[Axis]:
        return [a for a in self.axes if a.kind == "instrument"]

    @property
    def ramp(self) -> Optional[Axis]:
        """The ramp sweep, if it has one (always its innermost loop)."""
        return next((a for a in self.axes if a.kind == "ramp"), None)

    def rules(self) -> List[Rule]:
        """Every until: and while: in it, the plan's own and its loops'."""
        return [r for r in [self.until, self.while_] + [x for a in self.axes for x in (a.until, a.while_)] if r]

    def all_steps(self) -> List[Step]:
        return self.setup + self.after + (self.on_abort or [])


def _split(key: Any, path: str, errors: List[str]) -> Optional[Tuple[str, str]]:
    instr, dot, param = str(key).partition(".")
    if not dot or not instr or not param:
        errors.append("%s: %r should be instrument.parameter" % (path, key))
        return None
    return instr, param


def _steps(data: Any, path: str, errors: List[str]) -> List[Step]:
    if data is None:
        return []
    if not isinstance(data, dict):
        errors.append("%s: must be a mapping of instrument.parameter: value" % path)
        return []
    out = []
    for key, value in data.items():
        ip = _split(key, path, errors)
        if ip:
            out.append(Step(ip[0], ip[1], value))
    return out


Made = Tuple[int, Callable[[], List[Any]]]   # how many values, and how they are made (once they are few enough)


def _axis_values(spec: Any, path: str, errors: List[str]) -> List[Any]:
    made = _axis_shape(spec, path, errors)
    return made[1]() if made else []


def _axis_shape(spec: Any, path: str, errors: List[str]) -> Optional[Made]:
    """A stepped axis's values, back: (there and back) included, counted from its spec before any is made."""
    made = _base_shape(spec, path, errors)
    if made is None or not (isinstance(spec, dict) and spec.get("back") and made[0] > 1):
        return made
    n, base = made

    def there_and_back() -> List[Any]:   # not repeating the turn
        values = base()
        return values + values[-2::-1]
    return 2 * n - 1, there_and_back


def _base_values(spec: Any, path: str, errors: List[str]) -> List[Any]:
    """An axis's values as written, before back: adds the way back. More than MAX_POINTS are never made."""
    made = _base_shape(spec, path, errors)
    if made is not None and made[0] > MAX_POINTS:
        errors.append("%s: more than %d points" % (path, MAX_POINTS))
        return []
    return made[1]() if made else []


def _base_shape(spec: Any, path: str, errors: List[str]) -> Optional[Made]:
    """An axis's values as written, before back: adds the way back: how many, and how they are made, so a plan's
    points are counted before any is made (a check of a million-point range made every value; a billion would not
    fit in memory). None after saying what is wrong."""
    if isinstance(spec, dict) and "values" in spec:
        try:
            spec = list(spec["values"])
        except TypeError:   # values: 5
            spec = None
    if isinstance(spec, list):
        return len(spec), lambda: list(spec)
    if not isinstance(spec, dict):
        errors.append("%s: use {from, to, step}, {from, to, points}, {from, to, points, log: true} or {values: [...]}" % path)
        return None
    try:
        start, stop = float(spec["from"]), float(spec["to"])
        if spec.get("log"):
            n = int(spec["points"])
            if n < 2 or start <= 0 or stop <= 0:
                raise ValueError
            ratio = (stop / start) ** (1.0 / (n - 1))

            def log() -> List[Any]:
                values = [start * ratio ** i for i in range(n)]
                values[-1] = stop
                return values
            return n, log
        if "points" in spec:
            n = int(spec["points"])
            if n < 1:
                raise ValueError
            return n, lambda: [start + (stop - start) * i / (n - 1) for i in range(n)] if n > 1 else [start]
        step = abs(float(spec["step"]))
        if step == 0:
            raise ValueError
        n = int(math.floor(abs(stop - start) / step + 1e-9))
        if n + 1 > MAX_POINTS:
            errors.append("%s: more than %d points" % (path, MAX_POINTS))
            return None
        sign = 1.0 if stop >= start else -1.0
        short = not math.isclose(start + sign * step * n, stop, rel_tol=1e-9, abs_tol=1e-12)   # `to` itself after

        def stepped() -> List[Any]:
            return [start + sign * step * i for i in range(n + 1)] + ([stop] if short else [])
        return n + 1 + short, stepped
    except (KeyError, TypeError, ValueError, OverflowError):   # OverflowError: an endless number (.inf)
        errors.append("%s: needs from, to and a nonzero step or points (log sweeps: points ≥ 2 and positive ends)" % path)
        return None


def _seconds(value: Any, path: str, errors: List[str], positive: bool = False, unit: str = "seconds") \
        -> Optional[float]:
    """A duration a plan gives (or, with unit "", an amount such as a tolerance or a rate): ≥ 0, > 0 when positive;
    None after saying what is wrong."""
    try:
        s = float(value)
        if isinstance(value, bool) or not math.isfinite(s) or s < 0 or (positive and s == 0):
            raise ValueError
        return s
    except (TypeError, ValueError):
        what = ("a positive number" if positive else "a number, 0 or more") if not unit else (
            "a positive number of " + unit if positive else unit + ", 0 or more")
        errors.append("%s: must be %s" % (path, what))
        return None


def _settles(spec: Any, base: List[Any], back: bool, path: str, errors: List[str]) -> Optional[List[Optional[float]]]:
    """A hold for each value of a stepped axis (a staircase's last steps often need longer): settle: as a list, one per
    value as written (mirrored with back: true), or as value: seconds for some of them; None when settle: is one
    number for all (or absent)."""
    if not isinstance(spec, (list, dict)):
        return None
    if isinstance(spec, list):
        if len(spec) != len(base):
            errors.append("%s: give one hold per value (%d values, %d holds), or value: seconds for some of them"
                          % (path, len(base), len(spec)))
            return None
        own: List[Optional[float]] = [_seconds(s, path, errors) for s in spec]
    else:
        own = [None] * len(base)
        for key, s in spec.items():
            hits = [i for i, v in enumerate(base) if v == key or _number_equal(v, key)]
            if not hits:
                errors.append("%s: %r is not one of the axis's values" % (path, key))
                continue
            for i in hits:
                own[i] = _seconds(s, path, errors)
    if back and len(own) > 1:
        own = own + own[-2::-1]
    return own


def _number_equal(a: Any, b: Any) -> bool:
    try:
        return not isinstance(a, bool) and not isinstance(b, bool) and math.isclose(float(a), float(b),
                                                                                   rel_tol=1e-9, abs_tol=1e-15)
    except (TypeError, ValueError):
        return False


def _rule(text: Any, path: str, errors: List[str]) -> Optional[Rule]:
    if text is None:
        return None
    try:
        return parse_rule(text)
    except ValueError as e:
        errors.append("%s: %s" % (path, e))
        return None


def _ramp_axis(instr: str, param: str, spec: Dict[str, Any], path: str, errors: List[str]) -> Optional[Axis]:
    """A ramp sweep: its legs ({to, rate, hold}, or bare targets), how often it measures, what it follows."""
    unknown = sorted(set(spec) - _RAMPED, key=str)
    if unknown:
        errors.append("%s: %s can't go with ramp: (a ramp has %s; to wait at a leg's target, give it hold:)"
                      % (path, ", ".join(map(str, unknown)), ", ".join(sorted(_RAMPED))))
        return None
    legs = spec["ramp"] if isinstance(spec["ramp"], list) else [spec["ramp"]]
    rate = spec.get("rate")
    segments: List[Segment] = []
    for k, leg in enumerate(legs):
        where = "%s.ramp[%d]" % (path, k)
        if isinstance(leg, dict):
            if set(leg) - {"to", "rate", "hold"} or "to" not in leg:
                errors.append("%s: a leg is {to: value, rate: per its rate parameter, hold: seconds}" % where)
                continue
            to, leg_rate, hold = leg["to"], leg.get("rate", rate), leg.get("hold", 0)
        else:
            to, leg_rate, hold = leg, rate, 0
        if leg_rate is not None:
            leg_rate = _seconds(leg_rate, where + ".rate", errors, positive=True, unit="")
            if leg_rate is None:
                continue
        held = _seconds(hold, where + ".hold", errors)
        if to is None or isinstance(to, (dict, list)) or held is None:
            if to is None or isinstance(to, (dict, list)):
                errors.append("%s: to: needs one value" % where)
            continue
        segments.append(Segment(to, leg_rate, held))
    if not legs:
        errors.append("%s: ramp: needs at least one target" % path)
    if "every" not in spec:
        errors.append("%s: say how often to measure while it ramps (every: seconds)" % path)
        return None
    every = _seconds(spec["every"], path + ".every", errors, positive=True)
    read = _split(spec["read"], path + ".read", errors) if spec.get("read") is not None else None
    within = None
    if spec.get("within") is not None:
        within = _seconds(spec["within"], path + ".within", errors, positive=True, unit="")
    stall = _seconds(spec.get("stall", STALL_S), path + ".stall", errors)
    if every is None or stall is None or len(segments) != len(legs) or (spec.get("read") is not None and not read):
        return None
    return Axis(instr, param, [s.to for s in segments], kind="ramp", every=every, segments=segments, read=read,
                within=within, stall=stall, until=_rule(spec.get("until"), path + ".until", errors),
                while_=_rule(spec.get("while"), path + ".while", errors))


def _stabilize(spec: Any, path: str, errors: List[str]) -> Optional[Stabilize]:
    if spec is None:
        return None
    if not isinstance(spec, dict) or "read" not in spec:
        errors.append("%s: use {read: instr.param, within: change, for: seconds, timeout: seconds}" % path)
        return None
    ip = _split(spec["read"], path, errors)
    try:
        within, duration = float(spec.get("within", 0)), float(spec.get("for", 10))
        timeout = float(spec.get("timeout", 900))
        if within <= 0 or duration <= 0 or timeout <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("%s: within, for and timeout must be positive numbers" % path)
        return None
    every = _seconds(spec.get("every", 0), path + ".every", errors)
    near = None
    if spec.get("near") is not None:
        near = _seconds(spec["near"], path + ".near", errors, positive=True, unit="")
        if near is None:
            return None
    if every is None:
        return None
    return Stabilize(ip[0], ip[1], within, duration, timeout, every, near) if ip else None


def _time_axis(spec: Any, errors: List[str], make: bool = True) -> Optional[Tuple[Axis, int]]:
    """The time series and its points, counted first; make=False: its times are not made (too many points)."""
    if set(spec) - {"every", "for", "points", "until", "while"}:
        errors.append("time: unknown %s (it has every, for, points, until, while)"
                      % ", ".join(map(str, sorted(set(spec) - {"every", "for", "points", "until", "while"}, key=str))))
        return None
    try:
        every = float(spec["every"])
        if every <= 0:
            raise ValueError
        n = int(spec["points"]) if "points" in spec else int(math.floor(float(spec["for"]) / every + 1e-9)) + 1
        if n < 1:
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        errors.append("time: use {every: seconds, for: seconds} or {every: seconds, points: n}, and until: or while: "
                      "to end it early")
        return None
    return Axis("", "time", [round(i * every, 9) for i in range(n)] if make else [], kind="time", every=every,
                until=_rule(spec.get("until"), "time.until", errors),
                while_=_rule(spec.get("while"), "time.while", errors)), n


def _keys(spec: Any, path: str, errors: List[str]) -> List[Tuple[str, str]]:
    """A list of instrument.parameter keys (or one), as (instrument, parameter)."""
    if spec is None:
        return []
    out = []
    for key in spec if isinstance(spec, list) else [spec]:
        ip = _split(key, path, errors)
        if ip:
            out.append(ip)
    return out


def _withs(spec: Any, base: Optional[List[Any]], count: int, back: bool, path: str, errors: List[str]) -> List[With]:
    """The settings that move with a stepped loop's own (with:): each {from, to} in step with the loop's own values
    (as Special Measure maps a loop's range onto each of its channels'), or values: one per value as written; back:
    true brings them back with it. base: the loop's values as written (None: too many to make)."""
    if not isinstance(spec, dict) or not spec:
        errors.append("%s: a mapping of instrument.parameter: {from, to} or {values: [...]}, each a setting that "
                      "moves with the loop's own" % path)
        return []
    out = []
    for key, own in spec.items():
        where = "%s.%s" % (path, key)
        ip = _split(key, path, errors)
        if not ip:
            continue
        values: List[Any] = []
        if isinstance(own, list) or isinstance(own, dict) and set(own) == {"values"}:
            try:
                values = list(own if isinstance(own, list) else own["values"])
            except TypeError:
                errors.append("%s: values: is a list" % where)
                continue
            if len(values) != count:
                errors.append("%s: give one value per value of the loop (%d values, %d here)" % (where, count,
                                                                                                 len(values)))
                continue
        elif isinstance(own, dict) and set(own) == {"from", "to"}:
            try:
                a, b = float(own["from"]), float(own["to"])
                if not (math.isfinite(a) and math.isfinite(b)) or isinstance(own["from"], bool) or \
                        isinstance(own["to"], bool):
                    raise ValueError
            except (TypeError, ValueError):
                errors.append("%s: from and to are numbers" % where)
                continue
            if base is not None:
                try:
                    first, last = float(base[0]), float(base[-1])
                    span = last - first
                except (TypeError, ValueError, IndexError):
                    first, last, span = 0.0, 0.0, 0.0
                if len(base) == 1:
                    values = [a]
                elif not span:
                    errors.append("%s: from and to follow the loop's own values, which need a first and a last value "
                                  "that differ: give values: instead" % where)
                    continue
                else:
                    values = [a + (b - a) * (float(v) - first) / span for v in base]
                    values[0], values[-1] = a, b   # exactly as written at the ends
        else:
            errors.append("%s: use {from, to} (in step with the loop's own values) or {values: [...]}" % where)
            continue
        if back and len(values) > 1:
            values = values + values[-2::-1]
        out.append(With(ip[0], ip[1], values if base is not None else []))
    return out


def _sets(spec: Any, axes: List[Axis], errors: List[str], make: bool) -> List[Computed]:
    """sets: the settings a plan works out at each point, instrument.parameter: expression (expr.py's language) of
    its variables and the settings it sweeps (each stepped loop's own and its with:). Every value each takes is
    worked out before the run (Computed.values, made when make), so the run is checked and approved on them."""
    if spec is None:
        return []
    if not isinstance(spec, dict) or not spec:
        errors.append('sets: a mapping of instrument.parameter: expression, e.g. {dac.Vbg: "1.2*n - 8*E"}')
        return []
    known: Dict[str, Tuple[int, Optional[With]]] = {}   # a name an expression may use: its loop, and its with:
    for i, a in enumerate(axes):
        if a.kind in ("instrument", "variable"):
            known[a.key] = (i, None)
            for w in a.with_:
                known[w.key] = (i, w)
    out = []
    for key, text in spec.items():
        where = "sets.%s" % key
        ip = _split(key, "sets", errors)
        if not ip:
            continue
        if key in known:
            errors.append("%s: the sweep sets %s already; sets: works out settings the sweep doesn't" % (where, key))
            continue
        if isinstance(text, bool) or not isinstance(text, (str, int, float)):
            errors.append('%s: an expression in quotes, e.g. "1.2*n - 8*E"' % where)
            continue
        try:
            names = expr.names_in(str(text))
        except expr.ExprError as e:
            errors.append("%s: %s" % (where, e))
            continue
        unknown = [n for n in names if n not in known]
        if unknown:
            errors.append("%s: %s %s not swept: an expression uses the plan's variables and the settings its loops "
                          "step" % (where, ", ".join(unknown), "is" if len(unknown) == 1 else "are"))
            continue
        c = Computed(ip[0], ip[1], str(text), names)
        if make:
            c.values = _worked_out(c, axes, known, where, errors)
        out.append(c)
    return out


def _worked_out(c: Computed, axes: List[Axis], known: Dict[str, Tuple[int, Optional[With]]], where: str,
                errors: List[str]) -> List[Any]:
    """Every value a sets: expression gives, at each combination of the loops it uses, outer first (the run's
    order)."""
    loops = sorted({known[n][0] for n in c.names})
    sizes = [len(axes[i].values) for i in loops]
    total = 1
    for s in sizes:
        total *= s
    if total > MAX_COMPUTED:
        errors.append("%s: it would be worked out at %d combinations of the loops' values, more than the %d Kelvin "
                      "checks before a run" % (where, total, MAX_COMPUTED))
        return []
    out = []
    for ks in itertools.product(*[range(s) for s in sizes]):
        at: Dict[str, Any] = {}
        for i, k in zip(loops, ks):
            a = axes[i]
            at[a.key] = a.values[k]
            for w in a.with_:
                at[w.key] = w.values[k]
        try:
            out.append(expr.scalar(c.text, at))
        except expr.ExprError as e:
            errors.append("%s: %s" % (where, e))
            return []
    return out


_TOKEN = re.compile(r"(?<![\w.])([A-Za-z_]\w*)(?![\w.(])")   # a bare name, not part of instr.param nor a call


def with_names(data: Any, names: Dict[str, str]) -> Any:
    """A plan (a mapping) with the lab's own names for its settings and readings (the lab config's names:, such as a
    Special Measure rack's channel names Vbg or X_A) put back as instrument.parameter, so the plan Kelvin keeps, checks
    and runs names each setting as its card does: keys, lists of keys, conditions and expressions. A sweep key that is
    not one of the names stays the plan's variable."""
    if not names or not isinstance(data, dict):
        return data
    sweep = data.get("sweep") if isinstance(data.get("sweep"), dict) else {}
    variables = {k for k in sweep if isinstance(k, str) and "." not in k and k not in names}

    def key(k: Any) -> Any:
        return names.get(k, k) if isinstance(k, str) and k not in variables else k

    def text(t: Any) -> Any:
        if not isinstance(t, str):
            return t
        return _TOKEN.sub(lambda m: m.group(1) if m.group(1) in variables else names.get(m.group(1), m.group(1)), t)

    def keyed(d: Any) -> Any:
        return {key(k): v for k, v in d.items()} if isinstance(d, dict) else d

    def listed(v: Any) -> Any:
        return [key(x) for x in v] if isinstance(v, list) else key(v)

    out = dict(data)
    for part in ("setup", "after", "on_abort"):
        if part in out:
            out[part] = keyed(out[part])
    if "measure" in out:
        out["measure"] = listed(out["measure"])
    for part in ("until", "while"):
        if part in out:
            out[part] = text(out[part])
    if isinstance(out.get("preconditions"), list):
        out["preconditions"] = [text(t) for t in out["preconditions"]]
    if isinstance(out.get("sets"), dict):
        out["sets"] = {key(k): text(v) for k, v in out["sets"].items()}
    if sweep:
        moved = {}
        for k, spec in sweep.items():
            if isinstance(spec, dict):
                spec = dict(spec)
                if isinstance(spec.get("with"), dict):
                    spec["with"] = keyed(spec["with"])
                if "measure" in spec:
                    spec["measure"] = listed(spec["measure"])
                if isinstance(spec.get("stabilize"), dict) and "read" in spec["stabilize"]:
                    spec["stabilize"] = dict(spec["stabilize"], read=key(spec["stabilize"]["read"]))
                if "read" in spec:
                    spec["read"] = key(spec["read"])
                for part in ("until", "while"):
                    if part in spec:
                        spec[part] = text(spec[part])
            moved[key(k)] = spec
        out["sweep"] = moved
    return out


def _axes(data: Dict[Any, Any], errors: List[str], make: bool = True) -> Tuple[List[Axis], int]:
    """The plan's loops, outer first (repeat:, sweep:'s keys, time:), and the points they make, counted from their
    shape. make=False: no value is made (they make more points than a plan may have)."""
    axes: List[Axis] = []
    points = 1
    if data.get("repeat") is not None:
        try:
            n = int(data["repeat"])
            if n < 1:
                raise ValueError
            if n > 1:
                axes.append(Axis("", "repeat", list(range(1, n + 1)) if make else [], kind="repeat"))
                points *= n
        except (TypeError, ValueError, OverflowError):
            errors.append("repeat: must be a whole number ≥ 1")
    sweep = data.get("sweep") or {}
    if not isinstance(sweep, dict):
        errors.append("sweep: must be a mapping (several keys = nested, outer first)")
        sweep = {}
    for key, spec in sweep.items():
        path = "sweep.%s" % key
        variable = isinstance(key, str) and "." not in key and bool(VARIABLE.match(key)) and key not in RESERVED
        ip = ("", key) if variable else _split(key, "sweep", errors)   # a variable: sets: works out what it sets
        if isinstance(spec, dict) and "ramp" in spec:
            if variable:
                errors.append("%s: a variable is stepped (from, to, step or values); ramp: goes on an instrument's "
                              "set point" % path)
                continue
            axis = _ramp_axis(ip[0], ip[1], spec, path, errors) if ip else None
            if axis:
                axes.append(axis)
                points *= axis.count
            continue
        if isinstance(spec, dict) and set(spec) - _STEPPED:   # a mistyped until: would never end anything
            errors.append("%s: unknown %s (a sweep axis has %s; ramp: measures while it ramps)" % (
                path, ", ".join(map(str, sorted(set(spec) - _STEPPED, key=str))), ", ".join(sorted(_STEPPED))))
            continue
        shape = _axis_shape(spec, path, errors)
        if ip and shape and shape[0]:
            settle = spec.get("settle") if isinstance(spec, dict) else None
            settles, settle_s = None, None
            if isinstance(settle, (list, dict)):   # a hold of its own for each step
                if make or isinstance(settle, list):   # a list's length is all it is checked with, uncounted
                    base = _base_values(spec, path, []) if make else range(_base_shape(spec, path, [])[0])
                    settles = _settles(settle, base, bool(spec.get("back")), path + ".settle", errors)
            elif settle is not None:
                settle_s = _seconds(settle, path + ".settle", errors)
            stab = _stabilize(spec.get("stabilize"), path + ".stabilize", errors) if isinstance(spec, dict) else None
            until = _rule(spec.get("until"), path + ".until", errors) if isinstance(spec, dict) else None
            while_ = _rule(spec.get("while"), path + ".while", errors) if isinstance(spec, dict) else None
            # instrument.technique.parameter: one of a technique's parameters, stepped (techniques.py)
            kind = "variable" if variable else "technique" if "." in ip[1] else "instrument"
            if kind == "technique" and stab is not None:
                errors.append("%s: a technique's parameter is stepped as the technique runs; stabilize: goes on "
                              "an instrument's own setting" % path)
            own = spec if isinstance(spec, dict) else {}
            withs: List[With] = []
            if own.get("with") is not None:
                if kind == "technique":
                    errors.append("%s: with: moves settings with an instrument's setting or a variable, not with a "
                                  "technique's parameter" % path)
                else:
                    count = _base_shape(spec, path, [])[0]
                    withs = _withs(own["with"], _base_values(spec, path, []) if make else None, count,
                                   bool(own.get("back")), path + ".with", errors)
            first = _seconds(own["first_settle"], path + ".first_settle", errors) \
                if own.get("first_settle") is not None else None
            axes.append(Axis(ip[0], ip[1], shape[1]() if make else [], settle_s, kind=kind, stabilize=stab,
                             settles=settles, until=until, while_=while_, with_=withs,
                             measure=_keys(own.get("measure"), path + ".measure", errors), first_settle=first))
            points *= shape[0]
    if data.get("time") is not None:
        t = _time_axis(data["time"], errors, make) if isinstance(data["time"], dict) else None
        if t is None and not isinstance(data["time"], dict):
            errors.append("time: use {every: seconds, for: seconds}")
        if t:
            axes.append(t[0])
            points *= t[1]
    return axes, points


def _retry(spec: Any, errors: List[str]) -> Optional[Retry]:
    if spec is None or spec is False:
        return None
    try:
        times, wait = (spec.get("times", 3), spec.get("wait", 10)) if isinstance(spec, dict) else (spec, 10)
        if isinstance(spec, dict) and set(spec) - {"times", "wait"}:
            raise ValueError
        n, w = int(times), float(wait)
        if isinstance(times, bool) or n != float(times) or not 1 <= n <= 20 or not 0 <= w <= 3600:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("retry: use {times: 1 to 20, wait: seconds between tries}, or a number of times")
        return None
    return Retry(n, w)


def load_plan(source: Any, names: Optional[Dict[str, str]] = None) -> Plan:
    """From a mapping, a .yaml path or YAML text. names: the lab's own names for its settings (the lab config's
    names:), put back as instrument.parameter first (with_names)."""
    data, _ = read_yaml(source)
    errors: List[str] = []
    if not isinstance(data, dict):
        raise PlanError(["a plan must be a mapping"])
    if names:
        data = with_names(data, names)
    for key in data:
        if key not in _KEYS:
            errors.append("unknown key %r (allowed: %s)" % (key, ", ".join(sorted(_KEYS))))
    name = str(data.get("plan") or "")
    if not name:
        errors.append("plan: a name is required")

    # the points first, from the loops' shape alone; their values are made only when there are few enough
    points = _axes(data, [], make=False)[1]
    axes, _ = _axes(data, errors, make=points <= MAX_POINTS)
    ramps = [a for a in axes if a.kind == "ramp"]
    if len(ramps) > 1:
        errors.append("sweep: one ramp per plan (%s); nest stepped sweeps outside it" % ", ".join(a.key for a in ramps))
    elif ramps and axes[-1] is not ramps[0]:
        errors.append("sweep.%s: a ramp measures at its own interval (every:), so it is the innermost loop: put it "
                      "last in sweep:, without time:" % ramps[0].key)

    measure = []
    for key in data.get("measure") or []:
        ip = _split(key, "measure", errors)
        if ip:
            measure.append(ip)
    trigger = []
    for key in data.get("trigger") or []:   # instrument.action, as the card names its actions
        ip = _split(key, "trigger", errors)
        if ip:
            trigger.append(ip)
    techniques: List[TechniqueStep] = []
    raw_tech = data.get("technique")
    if raw_tech is not None and not isinstance(raw_tech, dict):
        errors.append("technique: a mapping of instrument.technique: {its parameters' values}")
        raw_tech = {}
    for key, values in (raw_tech or {}).items():   # a card's hardware-timed technique, run at every point
        ip = _split(key, "technique", errors)
        if not ip:
            continue
        if values is not None and not isinstance(values, dict):
            errors.append("technique.%s: a mapping of its parameters' values, e.g. {start: -0.2, vertex: 0.6}" % key)
            continue
        techniques.append(TechniqueStep(ip[0], ip[1], dict(values or {})))

    preconditions = []
    for text in data.get("preconditions") or []:
        try:
            preconditions.append(parse_condition(text))
        except ValueError as e:
            errors.append("preconditions: %s" % e)

    setup = _steps(data.get("setup"), "setup", errors)
    after = _steps(data.get("after"), "after", errors)
    on_abort = _steps(data.get("on_abort"), "on_abort", errors) if "on_abort" in data else None
    try:
        settle = float(data.get("settle", 0) or 0)
    except (TypeError, ValueError):
        errors.append("settle: must be seconds")
        settle = 0.0

    sets = _sets(data.get("sets"), axes, errors, make=points <= MAX_POINTS)
    plan = Plan(name=name, description=str(data.get("description", "")), preconditions=preconditions,
                setup=setup, axes=axes, measure=measure, trigger=trigger, settle=settle, after=after,
                on_abort=on_abort,
                source=data, device=" ".join(str(data.get("device") or "").split())[:60],
                until=_rule(data.get("until"), "until", errors), while_=_rule(data.get("while"), "while", errors),
                retry=_retry(data.get("retry"), errors), techniques=techniques, sets=sets)
    if points > MAX_POINTS:
        errors.append("plan has %d points (max %d)" % (points, MAX_POINTS))
    moved: Dict[str, str] = {}   # each setting the sweep moves, and the loop that moves it
    for a in axes:
        for k in ([a.key] if a.kind in ("instrument", "ramp") else []) + [w.key for w in a.with_]:
            if k in moved:
                errors.append("sweep: %s moves in two loops (%s and %s)" % (k, moved[k], a.key))
            moved.setdefault(k, a.key)
    recorded = {"%s.%s" % m for m in measure}
    for a in axes:   # a reading is recorded once: at every point, or in one loop
        for m in a.measure:
            k = "%s.%s" % m
            if k in recorded:
                errors.append("sweep.%s.measure: %s is recorded once, at each point (measure:) or in one loop" % (
                    a.key, k))
            recorded.add(k)
    uses = {n for c in sets for n in c.names}
    for a in axes:
        if a.kind == "variable" and a.key not in uses:
            errors.append("sweep.%s: a sweep key is instrument.parameter, or a variable that sets: works settings out "
                          "from" % a.key)
    used = [s.instr for s in plan.all_steps()] + [t.instr for t in techniques] \
        + [a.instr for a in plan.axes if a.kind in ("instrument", "ramp", "technique")] \
        + [w.instr for a in axes for w in a.with_] + [i for a in axes for i, _ in a.measure] + [c.instr for c in sets] \
        + [a.read[0] for a in axes if a.read] \
        + [a.stabilize.instr for a in axes if a.stabilize] + [i for i, _ in measure] + [c.instr for c in preconditions] \
        + [i for i, _ in trigger] + [k.split(".")[0] for r in plan.rules() for k in r.keys]
    plan.requires = [str(r) for r in data.get("requires") or []] or list(dict.fromkeys(used))
    if errors:
        raise PlanError(errors)
    return plan
