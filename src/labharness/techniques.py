"""Hardware-timed, buffered measurements a card declares (techniques:) and a plan runs: cyclic voltammetry on a
potentiostat, an impedance sweep, a scope's burst or segmented capture. The instrument runs the technique on its own
clock and keeps the points in its buffer; Kelvin arms it, starts it, waits until it is done, then fetches the buffer
and keeps each quantity as a trace.

    techniques:
      cv:
        label: Cyclic voltammetry
        drives: true                       # it applies something to the sample (the default): a person approves it
        parameters:
          start:  { unit: V, drive: V, label: Start and end potential }   # a level of the card's drive V: the
          vertex: { unit: V, drive: V, label: Vertex potential }          #   lab's and the device's limits on V hold
          rate:   { unit: V/s, limits: [1.0e-4, 5], default: 0.05 }
          step:   { unit: V, limits: [1.0e-4, 0.05], default: 0.002 }
          cycles: { type: int, limits: [1, 100], default: 1 }
        let:                               # what its commands need, worked out from its parameters
          points: "round(abs(vertex - start) / step) + 1"
          delay: "step / rate"
        duration: "cycles * 2 * abs(vertex - start) / rate"   # seconds: on the card, and how long Kelvin waits
        arm:
          - ':TRACe:CLEar "defbuffer1"'
          - ':SOURce:SWEep:VOLTage:LINear {start}, {vertex}, {points}, {delay}, {cycles}, BEST, OFF, ON, "defbuffer1"'
        trigger: [":INITiate"]
        wait: "*OPC?"                      # answered once it is done; or done: {get: a query, match: a regex}
        count: ':TRACe:ACTual? "defbuffer1"'   # how many points it took: {n} in fetch
        fetch:
          E: { get: ':TRACe:DATA? 1, {n}, "defbuffer1", SOURce', unit: V }
          I: { get: ':TRACe:DATA? 1, {n}, "defbuffer1", READing', unit: A }
        x: E                               # the other traces are kept against this one (I against E)
        abort: [":ABORt"]                  # what ends it early, after a stop

A plan runs it at every point, after its triggers and before its readings, and may sweep its parameters:

    technique: { pstat.cv: { start: -0.2, vertex: 0.6, cycles: 2 } }
    sweep: { pstat.cv.rate: { values: [0.02, 0.05, 0.1] } }

The safety gate can't ramp or watch guards inside a technique: the instrument runs it alone. So its levels are checked
before the run against the limits of the drive they are levels of (the lab's, the mounted device's), its commands
may set no parameter of the card and none the gate keeps to itself (a source's function, protection, units,
calibration), one that drives the sample is approved by a person with the run (never by auto mode), and a stop ends
the wait at once, sends its abort commands and takes the drives the plan touched back to safe."""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

NAME = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
QUANTITY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,30}$")   # what it fetches: E, I, t, Z_re
PLACE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
TYPES = ("float", "int", "enum")
MAX_S = 24 * 3600.0     # the longest a technique may take
SPARE_S = 30.0          # how much longer than its duration Kelvin waits for it (and 20 % more)
POLL_S = 0.5            # how often a done: query is asked while it runs
_KEYS = {"label", "description", "drives", "parameters", "let", "duration", "arm", "trigger", "wait", "done", "count",
         "fetch", "x", "abort"}
_PARAM_KEYS = {"unit", "type", "limits", "default", "drive", "rate_of", "label", "values", "description"}


class TechniqueError(RuntimeError):
    """A technique that didn't run as asked: the instrument said an error, a fetch didn't read, it didn't finish."""


class Refused(TechniqueError):
    """A technique Kelvin won't start: a level beyond the limits, hands on the sample, a blocked command. Never
    tried again by a plan's retry:."""


def _number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def wire_number(x: float) -> str:
    """A number as a command carries it: 401 for a count, 0.04 for a delay."""
    if abs(x - round(x)) < 1e-9 * max(1.0, abs(x)) and abs(x) < 1e15:
        return "%d" % round(x)
    return "%.10g" % x


@dataclass
class Param:
    name: str
    unit: str = ""
    type: str = "float"
    limits: Optional[Tuple[float, float]] = None
    default: Any = None
    drive: Optional[str] = None            # the card's drive this is a level of: that drive's limits hold
    rate_of: Optional[str] = None          # the card's drive this is the rate of: that drive's rate limit holds
    label: str = ""
    values: Optional[Dict[str, str]] = None   # an enum's names and what is sent for each
    description: str = ""

    def coerce(self, value: Any) -> Any:
        if self.type == "enum":
            s = ("on" if value else "off") if isinstance(value, bool) else str(value).strip().lower()
            for name in self.values or {}:
                if s == name.lower():
                    return name
            raise ValueError("%s is one of %s, not %r" % (self.name, ", ".join(self.values or {}), value))
        if isinstance(value, bool):
            raise ValueError("%s needs a number, not %r" % (self.name, value))
        try:
            x = float(value)
        except (TypeError, ValueError):
            raise ValueError("%s needs a number, not %r" % (self.name, value)) from None
        if not math.isfinite(x):
            raise ValueError("%s needs a finite number" % self.name)
        if self.type == "int":
            if x != int(x):
                raise ValueError("%s needs a whole number, not %r" % (self.name, value))
            return int(x)
        return x

    def wire(self, value: Any) -> str:
        if self.type == "enum":
            return str((self.values or {})[value])
        return wire_number(float(value))

    def show(self, value: Any) -> str:
        """In words, with its unit: 50 mV/s for a rate in V/s, 2 mV for a step in V."""
        if self.type == "enum" or not _number(value):
            return str(value)
        from .cards import SI_UNITS, si
        top, slash, per = self.unit.partition("/")
        if slash and top in SI_UNITS:
            return si(float(value), top) + "/" + per
        return si(float(value), self.unit) if self.unit else wire_number(float(value))


@dataclass
class Fetch:
    name: str
    get: str
    unit: str = ""
    label: str = ""


@dataclass
class Technique:
    name: str
    label: str
    params: Dict[str, Param]
    duration: str
    trigger: List[str]
    fetch: Dict[str, Fetch]
    drives: bool = True
    description: str = ""
    let: List[Tuple[str, str]] = field(default_factory=list)
    arm: List[str] = field(default_factory=list)
    wait: Optional[str] = None
    done: Optional[Tuple[str, str]] = None
    count: Optional[str] = None
    x: Optional[str] = None
    abort: List[str] = field(default_factory=list)

    def values(self, given: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], List[str]]:
        """Its parameters' values: those given, as their types, the rest their defaults; and what is wrong (an
        unknown name, a value missing or beyond its limits)."""
        out: Dict[str, Any] = {}
        problems = []
        given = dict(given or {})
        for name in sorted(set(given) - set(self.params)):
            problems.append("%s has no parameter %r (it has: %s)" % (self.name, name, ", ".join(self.params)))
        for name, p in self.params.items():
            value = given.get(name, p.default)
            if value is None:
                problems.append("%s.%s: give it a value (%s)" % (self.name, name, p.label or name))
                continue
            try:
                value = p.coerce(value)
            except ValueError as e:
                problems.append("%s.%s" % (self.name, e))
                continue
            if p.limits is not None and not p.limits[0] <= value <= p.limits[1]:
                problems.append("%s.%s %s is outside its limits [%s, %s] (the card's)" % (
                    self.name, name, p.show(value), p.show(p.limits[0]), p.show(p.limits[1])))
                continue
            out[name] = value
        return out, problems

    def worked_out(self, values: Dict[str, Any]) -> Dict[str, float]:
        """Its parameters as numbers (an enum as its place) and its let: values, in order. ValueError in words."""
        env = {k: float(v) if _number(v) else float(list((self.params[k].values or {})).index(v))
               for k, v in values.items()}
        for name, text in self.let:
            env[name] = _evaluate(text, env, "%s.let.%s" % (self.name, name))
        return env

    def seconds(self, values: Dict[str, Any]) -> float:
        env = self.worked_out(values)
        s = _evaluate(self.duration, env, "%s.duration" % self.name)
        if s < 0 or s > MAX_S:
            raise ValueError("%s would take %s; a technique takes from 0 to %d h" % (self.name, _dur(s),
                                                                                  MAX_S / 3600))
        return s

    def render(self, command: str, values: Dict[str, Any], env: Dict[str, float], n: Optional[int] = None) -> str:
        """A command with its {names} filled in: parameters as the instrument takes them, let: values, {n}."""
        def one(m: Any) -> str:
            key = m.group(1)
            if key in self.params:
                return self.params[key].wire(values[key])
            if key == "n":
                if n is None:
                    raise ValueError("{n} is only known once the technique is done (in fetch)")
                return "%d" % n
            return wire_number(env[key])
        return PLACE.sub(one, command)

    def commands(self) -> List[Tuple[str, str]]:
        """Every command it sends, with where it is written: (arm[0], text)..."""
        out = [("arm[%d]" % i, c) for i, c in enumerate(self.arm)] + \
            [("trigger[%d]" % i, c) for i, c in enumerate(self.trigger)] + \
            [("abort[%d]" % i, c) for i, c in enumerate(self.abort)]
        if self.wait:
            out.append(("wait", self.wait))
        if self.done:
            out.append(("done.get", self.done[0]))
        if self.count:
            out.append(("count", self.count))
        out += [("fetch.%s" % f.name, f.get) for f in self.fetch.values()]
        return out


def _dur(seconds: float) -> str:
    from .gate import _dur as dur
    return dur(seconds)


def _names(text: str) -> List[str]:
    import ast
    tree = ast.parse(text, mode="eval")
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    return sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and id(n) not in called})


def _evaluate(text: str, env: Dict[str, float], where: str) -> float:
    import numpy as np
    from . import expr

    def column(name: str) -> Any:
        if name in env:
            return np.float64(env[name])
        raise KeyError("%r is not one of its parameters nor a let: name" % name)
    try:
        value = float(expr.evaluate(text, column, np))
    except (expr.ExprError, TypeError, ValueError) as e:
        raise ValueError("%s: %s" % (where, e)) from None
    if not math.isfinite(value):
        raise ValueError("%s: %s is not a finite number" % (where, text))
    return value


# ------------------------------------------------------------------ a card's techniques: section

def parse(raw: Any, params: Dict[str, Any], card_vars: Iterable[str], actions: Iterable[str],
          err: Callable[[str, str], None]) -> Dict[str, Technique]:
    """The card's techniques, checked when the card is read: every name a command uses is declared, a level is a
    level of a numeric drive of the card, every parameter has limits, and no command sets a parameter of the card or
    changes what the gate keeps to itself. err(path, message) says what is wrong."""
    from .cards import CONSTANTS, as_sent, num
    from .gate import RESTRICTED, _restricted
    out: Dict[str, Technique] = {}
    if raw is None:
        return out
    if not isinstance(raw, dict):
        err("techniques", "a mapping of name: {parameters, duration, arm, trigger, wait, fetch...}")
        return out
    taken = set(params) | set(actions) | set(card_vars)
    for name, spec in raw.items():
        path = "techniques.%s" % name
        name = str(name)
        if not NAME.match(name) or name in taken:
            err(path, "a lower-case name of its own (not one of the card's parameters, actions or vars)")
            continue
        if not isinstance(spec, dict):
            err(path, "a mapping: label, parameters, duration, arm, trigger, wait (or done), count, fetch, x, abort")
            continue
        for key in sorted(set(spec) - _KEYS, key=str):
            err(path + "." + str(key), "unknown (a technique has %s)" % ", ".join(sorted(_KEYS)))
        tparams: Dict[str, Param] = {}
        raw_params = spec.get("parameters") or {}
        if not isinstance(raw_params, dict):
            err(path + ".parameters", "a mapping of name: {unit, limits, default, drive, type, label}")
            raw_params = {}
        for pname, p in raw_params.items():
            where = "%s.parameters.%s" % (path, pname)
            pname = str(pname)
            if not NAME.match(pname) or pname == "n" or pname in CONSTANTS or pname in card_vars:
                err(where, "a lower-case name of its own (not n, a constant such as pi, or one of the card's vars)")
                continue
            if not isinstance(p, dict):
                err(where, "a mapping: unit, limits, default, drive, type, label")
                continue
            for key in sorted(set(p) - _PARAM_KEYS, key=str):
                err(where + "." + str(key), "unknown (a parameter has %s)" % ", ".join(sorted(_PARAM_KEYS)))
            typ = str(p.get("type", "float"))
            if typ not in TYPES:
                err(where + ".type", "float, int or enum")
                continue
            param = Param(pname, str(p.get("unit", "")), typ, label=str(p.get("label", "")),
                          description=str(p.get("description", "")))
            if typ == "enum":
                v = p.get("values")
                if isinstance(v, list) and v:
                    param.values = {str(x): str(x) for x in v}
                elif isinstance(v, dict) and v:
                    param.values = {str(k): str(c) for k, c in v.items()}
                else:
                    err(where + ".values", "an enum needs a list or mapping of its values")
                    continue
            drive = p.get("drive")
            if drive is not None:
                d = params.get(str(drive))
                if d is None or not getattr(d, "is_drive", False) or not getattr(d, "numeric", False) or typ == "enum":
                    err(where + ".drive", "%r is not a numeric drive on this card (a level the technique applies is "
                                          "a level of one, so its limits hold)" % drive)
                    continue
                param.drive = str(drive)
                if not param.unit:
                    param.unit = d.unit
            rate_of = p.get("rate_of")
            if rate_of is not None:
                d = params.get(str(rate_of))
                if d is None or not getattr(d, "is_drive", False) or not getattr(d, "numeric", False) or typ == "enum" \
                        or drive is not None:
                    err(where + ".rate_of", "%r is not a numeric drive on this card (a rate the technique sweeps a "
                                            "level at is that drive's rate, so its rate limit holds)" % rate_of)
                    continue
                param.rate_of = str(rate_of)
            lim = p.get("limits")
            if lim is not None:
                if typ == "enum" or not (isinstance(lim, (list, tuple)) and len(lim) == 2 and all(_number(x) for x in lim)
                                         and lim[0] < lim[1]):
                    err(where + ".limits", "[low, high] with low < high")
                    continue
                param.limits = (float(lim[0]), float(lim[1]))
            elif typ != "enum" and param.drive is None:
                err(where + ".limits", "every number a technique sends has limits (or is a level of a drive: drive:)")
                continue
            if p.get("default") is not None:
                try:
                    param.default = param.coerce(p["default"])
                except ValueError as e:
                    err(where + ".default", str(e))
                    continue
                if param.limits and not param.limits[0] <= param.default <= param.limits[1]:
                    err(where + ".default", "%s is outside its limits [%s, %s]" % (num(param.default),
                                                                                    num(param.limits[0]),
                                                                                    num(param.limits[1])))
                    continue
            tparams[pname] = param
        known = set(tparams)
        lets: List[Tuple[str, str]] = []
        raw_let = spec.get("let") or {}
        if not isinstance(raw_let, dict):
            err(path + ".let", "a mapping of name: expression of its parameters")
            raw_let = {}

        def expression(text: Any, where: str) -> Optional[str]:
            if text is None or isinstance(text, (bool, dict, list)):
                err(where, "an expression of its parameters (and let: names), e.g. abs(vertex - start) / rate")
                return None
            text = repr(float(text)) if _number(text) else str(text).strip()
            try:
                unknown = [n for n in _names(text) if n not in known and n not in CONSTANTS]
            except SyntaxError as e:
                err(where, "not an expression: %s (%s)" % (text, e.msg))
                return None
            if unknown:
                err(where, "%s %s not one of its parameters nor a let: name" % (", ".join(unknown),
                                                                               "is" if len(unknown) == 1 else "are"))
                return None
            return text
        for lname, text in raw_let.items():
            lname = str(lname)
            if not NAME.match(lname) or lname in tparams or lname == "n" or lname in CONSTANTS or lname in card_vars:
                err("%s.let.%s" % (path, lname), "a name of its own (not a parameter's, n, a constant or a var)")
                continue
            text = expression(text, "%s.let.%s" % (path, lname))
            if text is not None:
                lets.append((lname, text))
                known.add(lname)
        duration = expression(spec.get("duration"), path + ".duration") if spec.get("duration") is not None else None
        if spec.get("duration") is None:
            err(path + ".duration", "how long it takes, in seconds, from its parameters: on the card, and how long "
                                    "Kelvin waits for it")

        def commands(key: str, need: bool) -> List[str]:
            value = spec.get(key)
            value = [value] if isinstance(value, str) else value
            if value is None and not need:
                return []
            if not isinstance(value, list) or not value or not all(isinstance(c, str) and c.strip() for c in value):
                err(path + "." + key, "a command, or a list of commands")
                return []
            return [as_sent(c) for c in value]
        arm, trigger, abort = commands("arm", False), commands("trigger", True), commands("abort", False)
        wait = as_sent(spec["wait"]) if isinstance(spec.get("wait"), str) and spec["wait"].strip() else None
        done = None
        if spec.get("done") is not None:
            d = spec["done"]
            if not isinstance(d, dict) or set(d) != {"get", "match"} or not str(d.get("get") or "").strip():
                err(path + ".done", "{get: a query, match: the regex its reply matches once it is done}")
            else:
                try:
                    re.compile(str(d["match"]))
                    done = (as_sent(str(d["get"])), str(d["match"]))
                except re.error as e:
                    err(path + ".done.match", "not a valid regex: %s" % e)
        if spec.get("wait") is not None and wait is None:
            err(path + ".wait", "a query the instrument answers once it is done, such as *OPC?")
        if (wait is None) == (done is None) and not (spec.get("wait") is not None and wait is None):
            err(path + ".wait", "say how Kelvin knows it is done: wait: a query answered then (*OPC?), or done: "
                                "{get, match}" if wait is None else "wait: or done:, not both")
        count = as_sent(spec["count"]) if isinstance(spec.get("count"), str) and spec["count"].strip() else None
        if spec.get("count") is not None and count is None:
            err(path + ".count", "a query that says how many points it took")
        fetch: Dict[str, Fetch] = {}
        raw_fetch = spec.get("fetch")
        if not isinstance(raw_fetch, dict) or not raw_fetch:
            err(path + ".fetch", "what to read once it is done: name: {get: a query, unit: ...}")
            raw_fetch = {}
        for fname, f in raw_fetch.items():
            where = "%s.fetch.%s" % (path, fname)
            f = {"get": f} if isinstance(f, str) else f
            if not QUANTITY.match(str(fname)) or not isinstance(f, dict) or not str(f.get("get") or "").strip() \
                    or set(f) - {"get", "unit", "label"}:
                err(where, "a name (letters, digits and _): {get: a query that reads the buffer, unit, label}")
                continue
            fetch[str(fname)] = Fetch(str(fname), as_sent(str(f["get"])), str(f.get("unit", "")),
                                      str(f.get("label", "")))
        x = str(spec["x"]) if spec.get("x") is not None else None
        if x is not None and x not in fetch:
            err(path + ".x", "%r is not one of what it fetches (%s)" % (x, ", ".join(fetch)))
        drives = spec.get("drives", True)
        if not isinstance(drives, bool):
            err(path + ".drives", "true when it applies anything to the sample (the default), false when it only "
                                  "listens (a scope's capture)")
            drives = True
        tech = Technique(name, str(spec.get("label") or name), tparams, duration or "0", trigger, fetch, drives,
                         str(spec.get("description", "")), lets, arm, wait, done, count, x, abort)
        for where, command in tech.commands():
            used = set(PLACE.findall(command))
            bad = sorted(used - known - ({"n"} if where.startswith("fetch") and count else set()))
            if bad:
                err("%s.%s" % (path, where), "{%s} %s not declared (its parameters, let: names%s)" % (
                    "}, {".join(bad), "is" if len(bad) == 1 else "are",
                    ", and {n} in fetch once count: says how many points" if "n" in bad else ""))
                continue
            sent = PLACE.sub("0", command)
            for other in params.values():
                if other.set_cmd is not None and other.family().match(sent) is not None:
                    err("%s.%s" % (path, where), "%r would set %s; set it as the parameter, in the plan's setup, "
                                                 "where its limits hold" % (command, other.name))
                    break
            kind = _restricted(sent)
            if kind is not None:
                err("%s.%s" % (path, where), "%r changes %s, which only the card's own parameters may (with their "
                                             "checks)" % (command, RESTRICTED[kind]))
        out[name] = tech
    return out


# ------------------------------------------------------------------ in a plan

def of(lab: Any, instr: str, name: str) -> Tuple[Any, Technique]:
    """The instrument and its card's technique; KeyError in words."""
    inst = lab.instrument(instr)
    tech = getattr(inst.card, "techniques", {}).get(name)
    if tech is None:
        have = ", ".join(getattr(inst.card, "techniques", {})) or "none"
        raise KeyError("%s has no technique %s (its card has: %s)" % (instr, name, have))
    return inst, tech


def swept(plan: Any, step: Any) -> Dict[str, List[Any]]:
    """The technique's parameters the plan sweeps, and their values."""
    out: Dict[str, List[Any]] = {}
    for axis in getattr(plan, "axes", []):
        if getattr(axis, "kind", "") == "technique" and axis.instr == step.instr:
            tech, _, param = axis.param.partition(".")
            if tech == step.name:
                out[param] = list(axis.values)
    return out


def _variants(base: Dict[str, Any], sweep: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
    """The values to check: as given, and each swept value with the others as given (or their first)."""
    first = dict(base, **{k: v[0] for k, v in sweep.items() if v})
    out = [first]
    for k, vals in sweep.items():
        out += [dict(first, **{k: v}) for v in vals[1:]]
    return out


def check(gate: Any, plan: Any) -> List[str]:
    """What is wrong with a plan's techniques before it runs: an unknown one, its parameters (known, given, within
    their limits), a level beyond its drive's limits (the lab's, the mounted device's, the source's fixed range), a
    duration it can't have, at every value the sweep gives it; and a swept parameter it doesn't have."""
    lab = gate.lab
    errors: List[str] = []
    steps = list(getattr(plan, "techniques", []))
    for axis in getattr(plan, "axes", []):
        if getattr(axis, "kind", "") != "technique":
            continue
        name, _, param = axis.param.partition(".")
        if not any(s.instr == axis.instr and s.name == name for s in steps):
            errors.append("sweep.%s: the plan doesn't run %s.%s (technique: { %s.%s: {...} })"
                          % (axis.key, axis.instr, name, axis.instr, name))
    for step in steps:
        try:
            inst, tech = of(lab, step.instr, step.name)
        except KeyError as e:
            errors.append("technique: %s" % (e.args[0] if e.args else e))
            continue
        sweep = swept(plan, step)
        for param in sweep:
            if param not in tech.params:
                errors.append("sweep.%s.%s.%s: %s has no parameter %s (it has: %s)" % (
                    step.instr, step.name, param, step.name, param, ", ".join(tech.params)))
        if errors:
            continue
        seen = set()
        for values in _variants(dict(step.values or {}), sweep):
            got, problems = tech.values(values)
            if not problems:
                problems = level_problems(gate, inst, tech, got)
            if not problems:
                try:
                    tech.seconds(got)
                except ValueError as e:
                    problems = [str(e)]
            for p in problems:
                if p not in seen:
                    seen.add(p)
                    errors.append("technique %s.%s: %s" % (step.instr, step.name, p))
    return errors


def level_problems(gate: Any, inst: Any, tech: Technique, values: Dict[str, Any]) -> List[str]:
    """A level beyond the limits of the drive it is a level of, or beyond the source's fixed range; a rate beyond
    the drive's rate limit (the lab's, the mounted device's); a drive with guards, which nothing could watch inside
    the technique."""
    out = []
    for name, p in tech.params.items():
        if name not in values:
            continue
        if p.rate_of is not None:
            limit = inst.rate(p.rate_of)
            if limit and float(values[name]) > limit * (1 + 1e-9):
                spec = inst.spec(p.rate_of)
                out.append("%s.%s.%s %s is faster than %s.%s's rate limit, %s %s/s (%s)" % (
                    inst.name, tech.name, name, p.show(values[name]), inst.name, p.rate_of, wire_number(limit),
                    spec.unit, "the mounted device's" if p.rate_of in inst.device_rates else
                    "the lab config's" if p.rate_of in inst.config.rates else "its card's"))
            continue
        if p.drive is None:
            continue
        spec = inst.spec(p.drive)
        problem = gate._limit_problem(inst, spec, float(values[name]))
        if not problem and spec.range_param:
            try:
                problem = gate._fixed_range_problem(inst, spec, float(values[name]))
            except Exception:
                problem = None
        if problem:
            out.append("%s (%s, a level of %s.%s)" % (problem, name, inst.name, p.drive))
    for drive in sorted({p.drive for p in tech.params.values() if p.drive} | {p.rate_of for p in tech.params.values()
                                                                             if p.rate_of}):
        guards = inst.guards(drive)
        if guards and tech.drives:
            out.append("%s.%s has guards (%s) that nothing could watch inside %s, which the instrument runs alone: "
                       "sweep it point by point instead" % (inst.name, drive, ", ".join(g.key for g in guards),
                                                            tech.name))
    return out


def away_problems(gate: Any, inst: Any, tech: Technique, values: Dict[str, Any]) -> List[str]:
    """Why its drives can't move away from safe now, as the gate would say for a change of them: hands on the sample,
    an interlock that doesn't hold, a guard's instrument away."""
    if not tech.drives:
        return []
    out = []
    for drive in sorted({p.drive for n, p in tech.params.items() if p.drive and n in values}):
        problem = gate._away_problem(inst, inst.spec(drive))
        if problem:
            out.append(problem)
    if not out:
        hands = gate._hands_problem("%s.%s" % (inst.name, tech.name))
        if hands:
            out.append(hands)
    return out


def drive_values(lab: Any, plan: Any) -> List[Tuple[str, Any]]:
    """The levels a plan's techniques apply, as values of the drives they are levels of (instr.drive, value): the
    run's one card shows and approves them as it does a sweep's."""
    out = []
    for step in getattr(plan, "techniques", []):
        try:
            inst, tech = of(lab, step.instr, step.name)
        except KeyError:
            continue
        for values in _variants(dict(step.values or {}), swept(plan, step)):
            got, _ = tech.values(values)
            for name, p in tech.params.items():
                if p.drive is not None and name in got:
                    out.append(("%s.%s" % (step.instr, p.drive), got[name]))
    return out


def driving(lab: Any, plan: Any) -> List[str]:
    """The plan's techniques that drive the sample (instr.technique)."""
    out = []
    for step in getattr(plan, "techniques", []):
        try:
            _, tech = of(lab, step.instr, step.name)
        except KeyError:
            continue
        if tech.drives and step.key not in out:
            out.append(step.key)
    return out


def step_seconds(lab: Any, plan: Any, step: Any) -> Tuple[float, float]:
    """One technique of a plan: how long it takes at the sweep's first values, and in all (at every point, each
    swept value counted as often as it comes)."""
    try:
        _, tech = of(lab, step.instr, step.name)
    except KeyError:
        return 0.0, 0.0
    sweep = swept(plan, step)
    base = dict(step.values or {}, **{k: v[0] for k, v in sweep.items() if v})
    points = max(1, int(getattr(plan, "points", 1) or 1))

    def at(values: Dict[str, Any]) -> Optional[float]:
        got, problems = tech.values(values)
        if problems:
            return None
        try:
            return tech.seconds(got)
        except ValueError:
            return None
    first = at(base)
    if first is None:
        return 0.0, 0.0
    if not sweep:
        return first, first * points
    combos = 1
    for vals in sweep.values():
        combos *= max(1, len(vals))
    total, counted = 0.0, 0
    import itertools
    keys = list(sweep)
    for combo in itertools.islice(itertools.product(*(sweep[k] for k in keys)), 2000):
        s = at(dict(base, **dict(zip(keys, combo))))
        if s is not None:
            total += s
            counted += 1
    mean = total / counted if counted else first
    return first, mean * points


def seconds(lab: Any, plan: Any) -> float:
    """The time a plan's techniques take, in all: each at every point, at the values the sweep gives it."""
    return sum(step_seconds(lab, plan, step)[1] for step in getattr(plan, "techniques", []))


def lines(gate: Any, plan: Any) -> List[str]:
    """The run's card: each technique, what it applies and how long it takes, and what the gate can't do inside it."""
    lab = gate.lab
    out = []
    setup = {(s.instr, s.param): s.value for s in getattr(plan, "setup", [])}
    for step in getattr(plan, "techniques", []):
        try:
            inst, tech = of(lab, step.instr, step.name)
        except KeyError:
            continue
        sweep = swept(plan, step)
        got, problems = tech.values(dict(step.values or {}, **{k: v[0] for k, v in sweep.items() if v}))
        if problems:
            continue
        shown = []
        for name, p in tech.params.items():
            if name in sweep:
                vals = [p.coerce(v) for v in sweep[name]]
                shown.append("%s swept %s to %s" % (name, p.show(min(vals)), p.show(max(vals))))
            else:
                shown.append("%s %s" % (name, p.show(got[name])))
        first, total = step_seconds(lab, plan, step)
        took = "; ~%s each%s, ~%s in all" % (_dur(first), " at the first values" if sweep else "", _dur(total)) \
            if total else ""
        line = "At every point: %s (%s), run by the instrument on its own clock: %s%s" % (
            step.key, tech.label, ", ".join(shown), took)
        levels = [(name, p) for name, p in tech.params.items() if p.drive is not None]
        if tech.drives and levels:
            drives = sorted({p.drive for _, p in levels})
            guard = []
            for d in drives:
                limit = inst.spec(d).current_limit
                if limit:
                    value = setup.get((inst.name, limit))
                    try:
                        value = inst.spec(limit).coerce(value) if value is not None else gate._current(inst,
                                                                                                inst.spec(limit))
                        guard.append("%s.%s %s" % (inst.name, limit, inst.spec(limit).show(value)))
                    except Exception:
                        guard.append("%s.%s" % (inst.name, limit))
            line += ". It applies %s itself; Kelvin can't ramp or watch guards inside it%s" % (
                " and ".join("%s.%s" % (inst.name, d) for d in drives),
                ": the instrument's own limit holds (%s)" % ", ".join(guard) if guard else "")
        elif tech.drives:
            line += ". It drives the sample; Kelvin can't watch guards inside it"
        out.append(line)
    return out


def account(forecast: Any, lab: Any, plan: Any) -> None:
    """A forecast walks a plan without its techniques: their time goes into its duration, and they are no longer
    'not counted'."""
    try:
        steps = list(getattr(plan, "techniques", []))
        if not steps:
            return
        forecast.seconds += seconds(lab, plan)
        name = getattr(plan, "name", "the plan")
        axes = {"%s.%s" % (a.instr, a.param) for a in getattr(plan, "axes", []) if getattr(a, "kind", "") == "technique"}
        forecast.unknown = [u for u in forecast.unknown if u != "%s's technique" % name
                            and not any(u == "%s's technique sweep" % k for k in axes)]
    except Exception:
        return


def columns(lab: Any, plan: Any) -> List[Dict[str, str]]:
    """The traces a plan's techniques keep (instr.technique.quantity), each against its technique's x, which is
    kept with them (pstat.cv.I.x) rather than as a column of its own (record)."""
    out = []
    for step in getattr(plan, "techniques", []):
        try:
            _, tech = of(lab, step.instr, step.name)
        except KeyError:
            continue
        x = tech.fetch.get(tech.x) if tech.x else None
        for f in tech.fetch.values():
            if x is not None and f.name == x.name and len(tech.fetch) > 1:
                continue
            m = {"key": "%s.%s" % (step.key, f.name), "unit": f.unit, "kind": "trace"}
            if x is not None and f.name != x.name:
                m["x_unit"] = x.unit
            out.append(m)
    return out


def record(row: Dict[str, Any], key: str, tech: Technique, buffers: Dict[str, List[float]]) -> None:
    """A technique's buffers in a run's row: each quantity as a trace (key.quantity) with its x values beside it
    (key.quantity.x); x as a trace of its own only where a quantity's length doesn't match it."""
    x = buffers.get(tech.x) if tech.x else None
    lonely = x is None or len(buffers) == 1
    for name, ys in buffers.items():
        if name == tech.x:
            continue
        row["%s.%s" % (key, name)] = ys
        if x is not None and len(x) == len(ys):
            row["%s.%s.x" % (key, name)] = x
        else:
            lonely = True
    if x is not None and lonely:
        row["%s.%s" % (key, tech.x)] = x


def axis_unit(lab: Any, axis: Any) -> str:
    name, _, param = axis.param.partition(".")
    try:
        return of(lab, axis.instr, name)[1].params[param].unit
    except (KeyError, AttributeError):
        return ""


# ------------------------------------------------------------------ running one

def run(gate: Any, inst: Any, tech: Technique, given: Dict[str, Any], stops_since: Optional[int] = None,
        by: str = "plan") -> Dict[str, List[float]]:
    """Arm, start, wait until done, fetch: the buffer, one trace per quantity. The checks again first (fail closed):
    its values, its levels against their drives' limits, hands on the sample, blocked commands. A stop ends the wait
    at once (Stopped) and sends its abort commands. Refused: it won't start; TechniqueError: it didn't run as asked."""
    from .cards import trace_numbers
    from .instrument import Stopped
    key = "%s.%s" % (inst.name, tech.name)
    values, problems = tech.values(given)
    if not problems:
        problems = level_problems(gate, inst, tech, values) or away_problems(gate, inst, tech, values)
    if problems:
        raise Refused("%s: %s" % (key, "; ".join(problems)))
    env = tech.worked_out(values)
    took = tech.seconds(values)
    sent = [tech.render(c, values, env) for c in tech.arm + tech.trigger]
    for c in sent + tech.abort:
        if any(b.match(c) is not None for b in inst.card.blocked):
            raise Refused("%s: %r is blocked on %s" % (key, c, inst.name))
    stops = gate.lab.stops
    since = stops.count if stops_since is None else stops_since
    t0 = time.monotonic()
    if inst.simulated:   # a twin: what it can simulate, or the time it would take
        sim = getattr(inst.backend, "run_technique", None)
        if sim is not None:
            data = sim(tech, values, env)
        else:
            if not stops.sleep(gate.lab.clock, took, since):
                raise Stopped("%s: stopped by a person" % key)
            data = {}
        gate.log.write("technique", by=by, target=key, values=values, simulated=True)
        return data
    backend = inst.backend
    lock = getattr(backend, "lock", None)
    held = lock if lock is not None and hasattr(lock, "_is_owned") else None   # re-entrant: held throughout
    if held is not None:
        held.acquire()
    try:
        if inst.card.error_query is not None:   # what is queued now isn't this technique's (each change checked its own)
            try:
                inst.drain_errors()
            except Exception:
                pass
        try:
            for c in sent:
                if stops.since(since):
                    raise Stopped("%s: stopped by a person before it started" % key)
                _send(inst, c)
            _wait(inst, tech, values, env, took, lambda: stops.since(since), gate)
        except Exception as e:
            _abort(inst, tech, values, env)   # whatever happened, the instrument stops what it runs
            if isinstance(e, Stopped):
                raise
            if isinstance(e, TechniqueError):
                raise
            raise TechniqueError("%s: %s" % (key, str(e).splitlines()[0] if str(e) else type(e).__name__)) from e
        n = None
        if tech.count:
            reply = inst._io(backend.query, tech.render(tech.count, values, env))
            try:
                n = int(round(float(str(reply).strip().split(",")[0])))
            except ValueError:
                raise TechniqueError("%s: %s answered %r, not how many points it took" % (key, tech.count,
                                                                                        str(reply)[:60])) from None
        data: Dict[str, List[float]] = {}
        if n == 0:
            data = {f.name: [] for f in tech.fetch.values()}
        else:
            query = getattr(backend, "query_trace", backend.query)
            for f in tech.fetch.values():
                command = tech.render(f.get, values, env, n)
                try:
                    data[f.name] = trace_numbers(inst._io(query, command))
                except ValueError as e:
                    raise TechniqueError("%s: %s: %s" % (key, command, e)) from None
        errors, _ = inst.drain_errors() if inst.card.error_query is not None else ([], False)
        if errors:   # the buffer may not be what was asked: never kept as if it were
            raise TechniqueError("%s: the instrument says %s" % (key, "; ".join(errors[:3])))
    finally:
        if held is not None:
            held.release()
    gate.log.write("technique", by=by, target=key, values=values, points=n, seconds=round(time.monotonic() - t0, 3))
    return data


def _send(inst: Any, command: str) -> str:
    from .scpi import is_query
    if is_query(command, bool(inst.card.connection.get("enq"))):
        return str(inst._io(inst.backend.query, command))
    inst._io(inst.backend.write, command)
    return ""


def _wait(inst: Any, tech: Technique, values: Dict[str, Any], env: Dict[str, float], took: float,
          stopped: Callable[[], bool], gate: Any) -> None:
    """Until the instrument says it is done: its wait: query answered (a stop ends the wait), or its done: query
    matched, asked every POLL_S. Longer than its duration and SPARE_S more is a TechniqueError."""
    from .instrument import Stopped
    limit = took * 1.2 + SPARE_S
    key = "%s.%s" % (inst.name, tech.name)
    backend = inst.backend
    if tech.wait:
        command = tech.render(tech.wait, values, env)
        waiting = getattr(backend, "query_waiting", None)
        if waiting is not None:
            inst._io(waiting, command, limit, stopped)
            return
        res = getattr(backend, "res", None)
        before = getattr(res, "timeout", None)
        try:
            if before is not None:
                res.timeout = limit * 1000.0
            inst._io(backend.query, command)
        finally:
            if before is not None:
                try:
                    res.timeout = before
                except Exception:
                    pass
        return
    query, match = tech.done
    command = tech.render(query, values, env)
    end = time.monotonic() + limit
    while True:
        if re.search(match, str(inst._io(backend.query, command))):
            return
        if stopped():
            raise Stopped("%s: stopped by a person while it was still running" % key)
        if time.monotonic() > end:
            raise TechniqueError("%s did not finish within %s" % (key, _dur(limit)))
        gate.lab.stops.sleep(gate.lab.clock, POLL_S, gate.lab.stops.count)


def _abort(inst: Any, tech: Technique, values: Dict[str, Any], env: Dict[str, float]) -> None:
    """Its abort commands, whatever the instrument answers: a stop must end what it runs."""
    for c in tech.abort:
        try:
            _send(inst, tech.render(c, values, env))
        except Exception:
            continue


def describe(card: Any) -> List[str]:
    """A card's techniques, for describe_instrument: each with its parameters, units, limits and defaults."""
    out = []
    for name, tech in getattr(card, "techniques", {}).items():
        bits = []
        for pname, p in tech.params.items():
            text = pname + (" " + p.unit if p.unit else "")
            if p.limits:
                text += " [%s, %s]" % (p.show(p.limits[0]), p.show(p.limits[1]))
            if p.drive:
                text += " (a level of %s: its limits)" % p.drive
            if p.rate_of:
                text += " (the rate of %s: its rate limit)" % p.rate_of
            if p.values:
                text += " {%s}" % ", ".join(p.values)
            if p.default is not None:
                text += " default %s" % p.show(p.default)
            bits.append(text)
        out.append("  technique %s (%s%s; a plan's technique: runs it at every point, sweep: %s.<param> sweeps it): "
                   "%s; fetches %s" % (name, tech.label, ", drives the sample" if tech.drives else "", name,
                                       "; ".join(bits), ", ".join("%s%s" % (f.name, " (%s)" % f.unit if f.unit else "")
                                                                  for f in tech.fetch.values())))
    return out
