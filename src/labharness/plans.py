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
    measure: [lockin.X, lockin.Y, smu.I]
    after:   { smu.V: 0 }
    on_abort: { smu.V: 0 }                   # default: every drive the plan touched goes to safe
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .conditions import Condition, parse_condition
from .labconfig import read_yaml

MAX_POINTS = 1_000_000
_KEYS = {"plan", "description", "device", "requires", "preconditions", "setup", "sweep", "repeat", "time",
         "settle", "measure", "after", "on_abort"}


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
    """Before measuring, wait until a reading changes by less than `within` over `duration` seconds."""
    instr: str
    param: str
    within: float
    duration: float
    timeout: float = 900.0

    @property
    def key(self) -> str:
        return "%s.%s" % (self.instr, self.param)


@dataclass
class Axis:
    instr: str
    param: str
    values: List[Any]
    settle: Optional[float] = None
    kind: str = "instrument"            # instrument | repeat | time
    every: float = 0.0                  # time axis: seconds between points
    stabilize: Optional[Stabilize] = None

    @property
    def key(self) -> str:
        return "%s.%s" % (self.instr, self.param) if self.kind == "instrument" else self.param


@dataclass
class Plan:
    name: str
    description: str = ""
    requires: List[str] = field(default_factory=list)
    preconditions: List[Condition] = field(default_factory=list)
    setup: List[Step] = field(default_factory=list)
    axes: List[Axis] = field(default_factory=list)
    measure: List[Tuple[str, str]] = field(default_factory=list)
    settle: float = 0.0
    after: List[Step] = field(default_factory=list)
    on_abort: Optional[List[Step]] = None
    source: Dict[str, Any] = field(default_factory=dict)
    device: str = ""                  # the sample this run measures, when it is not the mounted one

    @property
    def points(self) -> int:
        n = 1
        for a in self.axes:
            n *= len(a.values)
        return n

    @property
    def instrument_axes(self) -> List[Axis]:
        return [a for a in self.axes if a.kind == "instrument"]

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


def _axis_values(spec: Any, path: str, errors: List[str]) -> List[Any]:
    if isinstance(spec, list):
        return list(spec)
    if not isinstance(spec, dict):
        errors.append("%s: use {from, to, step}, {from, to, points}, {from, to, points, log: true} or {values: [...]}" % path)
        return []
    if "values" in spec:
        values = list(spec["values"])
    else:
        try:
            start, stop = float(spec["from"]), float(spec["to"])
            if spec.get("log"):
                n = int(spec["points"])
                if n < 2 or start <= 0 or stop <= 0:
                    raise ValueError
                ratio = (stop / start) ** (1.0 / (n - 1))
                values = [start * ratio ** i for i in range(n)]
                values[-1] = stop
            elif "points" in spec:
                n = int(spec["points"])
                if n < 1:
                    raise ValueError
                values = [start + (stop - start) * i / (n - 1) for i in range(n)] if n > 1 else [start]
            else:
                step = abs(float(spec["step"]))
                if step == 0:
                    raise ValueError
                n = int(math.floor(abs(stop - start) / step + 1e-9))
                if n + 1 > MAX_POINTS:
                    errors.append("%s: more than %d points" % (path, MAX_POINTS))
                    return []
                sign = 1.0 if stop >= start else -1.0
                values = [start + sign * step * i for i in range(n + 1)]
                if not math.isclose(values[-1], stop, rel_tol=1e-9, abs_tol=1e-12):
                    values.append(stop)
        except (KeyError, TypeError, ValueError):
            errors.append("%s: needs from, to and a nonzero step or points (log sweeps: points ≥ 2 and positive ends)" % path)
            return []
    if spec.get("back") and len(values) > 1:  # there and back, without repeating the turning point
        values = values + values[-2::-1]
    return values


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
    return Stabilize(ip[0], ip[1], within, duration, timeout) if ip else None


def _time_axis(spec: Any, errors: List[str]) -> Optional[Axis]:
    try:
        every = float(spec["every"])
        if every <= 0:
            raise ValueError
        n = int(spec["points"]) if "points" in spec else int(math.floor(float(spec["for"]) / every + 1e-9)) + 1
        if n < 1:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        errors.append("time: use {every: seconds, for: seconds} or {every: seconds, points: n}")
        return None
    return Axis("", "time", [round(i * every, 9) for i in range(n)], kind="time", every=every)


def load_plan(source: Any) -> Plan:
    """From a mapping, a .yaml path or YAML text."""
    data, _ = read_yaml(source)
    errors: List[str] = []
    if not isinstance(data, dict):
        raise PlanError(["a plan must be a mapping"])
    for key in data:
        if key not in _KEYS:
            errors.append("unknown key %r (allowed: %s)" % (key, ", ".join(sorted(_KEYS))))
    name = str(data.get("plan") or "")
    if not name:
        errors.append("plan: a name is required")

    axes = []
    if data.get("repeat") is not None:
        try:
            n = int(data["repeat"])
            if n < 1:
                raise ValueError
            if n > 1:
                axes.append(Axis("", "repeat", list(range(1, n + 1)), kind="repeat"))
        except (TypeError, ValueError):
            errors.append("repeat: must be a whole number ≥ 1")
    sweep = data.get("sweep") or {}
    if not isinstance(sweep, dict):
        errors.append("sweep: must be a mapping (several keys = nested, outer first)")
        sweep = {}
    for key, spec in sweep.items():
        ip = _split(key, "sweep", errors)
        values = _axis_values(spec, "sweep.%s" % key, errors)
        if ip and values:
            settle = spec.get("settle") if isinstance(spec, dict) else None
            stab = _stabilize(spec.get("stabilize"), "sweep.%s.stabilize" % key, errors) if isinstance(spec, dict) else None
            axes.append(Axis(ip[0], ip[1], values, float(settle) if settle is not None else None, stabilize=stab))
    if data.get("time") is not None:
        t = _time_axis(data["time"], errors) if isinstance(data["time"], dict) else None
        if t is None and not isinstance(data["time"], dict):
            errors.append("time: use {every: seconds, for: seconds}")
        if t:
            axes.append(t)

    measure = []
    for key in data.get("measure") or []:
        ip = _split(key, "measure", errors)
        if ip:
            measure.append(ip)

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

    plan = Plan(name=name, description=str(data.get("description", "")), preconditions=preconditions,
                setup=setup, axes=axes, measure=measure, settle=settle, after=after, on_abort=on_abort,
                source=data, device=" ".join(str(data.get("device") or "").split())[:60])
    if plan.points > MAX_POINTS:
        errors.append("plan has %d points (max %d)" % (plan.points, MAX_POINTS))
    used = [s.instr for s in plan.all_steps()] + [a.instr for a in plan.instrument_axes] \
        + [a.stabilize.instr for a in axes if a.stabilize] + [i for i, _ in measure] + [c.instr for c in preconditions]
    plan.requires = [str(r) for r in data.get("requires") or []] or list(dict.fromkeys(used))
    if errors:
        raise PlanError(errors)
    return plan
