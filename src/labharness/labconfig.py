"""Lab configuration (set by the instrument owner) and project envelopes.

Limits nest: vendor limits (card) ⊇ lab limits (this file) ⊇ project
envelope. The model can read all of them and change none of them. It can add
an instrument connected to the computer, with its card's own limits and marked
`added_by: kelvin`: until a person reviews the limits and removes the mark,
every output change on it asks them.

Auto mode (switched on by a person, never by the agent) approves drive changes
that stay within `auto_fraction` of the way from safe to the lab limit. The
owner can exclude an instrument or parameter (`auto: never`) or give a
parameter its own auto range.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from .cards import CONNECTION_KEYS, Card, CardError, CardRegistry, ParamSpec, num
from .conditions import Condition, parse_condition
from .yamlio import load_yaml

TRUST = ("ask", "envelope")
AUTO_FRACTION = 0.25  # default: auto mode covers the first quarter of the way from safe to each limit
ON_TRIP = ("safe", "hold")


class ConfigError(ValueError):
    def __init__(self, source: str, errors: List[str]):
        self.errors = list(errors)
        super().__init__("%s: %s" % (source, "; ".join(self.errors)))


def read_yaml(source: Any) -> Tuple[Any, str]:
    """Accept a mapping, a path to a .yaml file, or YAML text."""
    if isinstance(source, dict):
        return source, "<dict>"
    if isinstance(source, Path) or (isinstance(source, str) and "\n" not in source
                                    and source.strip().lower().endswith((".yaml", ".yml"))):
        with open(source, encoding="utf-8") as f:
            return load_yaml(f), str(source)
    return load_yaml(source), "<text>"


@dataclass
class Allowed:
    """A numeric range or a set of allowed values."""
    lo: Optional[float] = None
    hi: Optional[float] = None
    values: Optional[FrozenSet[Any]] = None

    def covers(self, value: Any) -> bool:
        if self.values is not None:
            return value in self.values
        eps = 1e-9 * max(1.0, abs(self.lo), abs(self.hi))
        return self.lo - eps <= value <= self.hi + eps

    @classmethod
    def of(cls, spec: ParamSpec, values: List[Any]) -> "Allowed":
        if spec.numeric:
            return cls(lo=min(values), hi=max(values))
        return cls(values=frozenset(values))

    def show(self, spec: ParamSpec) -> str:
        if self.values is not None:
            return ", ".join(sorted(spec.show(v) for v in self.values))
        if self.lo == self.hi:
            return spec.show(self.lo)
        return "%s … %s" % (num(self.lo), spec.show(self.hi))


@dataclass
class Guard:
    instr: str
    param: str
    max_abs: float
    on_trip: str = "safe"

    @property
    def key(self) -> str:
        return "%s.%s" % (self.instr, self.param)


@dataclass
class InstrumentConfig:
    name: str
    card: str
    address: str
    trust: str = "ask"
    limits: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    rates: Dict[str, float] = field(default_factory=dict)
    guards: Dict[str, List[Guard]] = field(default_factory=dict)
    auto: Dict[str, Any] = field(default_factory=dict)  # "*" or param -> "never" | Allowed (its own auto range)
    connection: Dict[str, Any] = field(default_factory=dict)  # overrides the card's (e.g. RS-232 terminators)
    vars: Dict[str, str] = field(default_factory=dict)        # the card's per-installation names (sensor, channel)
    added_by: str = ""   # "kelvin": Kelvin added it and no person has reviewed its limits yet (gate.py asks)


@dataclass
class Interlock:
    drive: str
    require: Condition


@dataclass
class LabConfig:
    name: str
    instruments: Dict[str, InstrumentConfig]
    interlocks: List[Interlock] = field(default_factory=list)
    safe_actions: Dict[str, str] = field(default_factory=dict)
    source: str = ""
    auto_fraction: float = AUTO_FRACTION
    watches: List[Dict[str, Any]] = field(default_factory=list)  # watches written in the lab config (see monitor.py)
    observe_only: bool = False   # older auto-setup labs: every instrument counts as added by Kelvin (see added_by)

    def unreviewed(self) -> List[str]:
        """Real instruments whose limits no person has reviewed: Kelvin added them (or set up the whole lab)."""
        return [n for n, ic in self.instruments.items()
                if (self.observe_only or ic.added_by) and not ic.address.startswith("sim://")]


def _pair(x: Any) -> bool:
    return (isinstance(x, (list, tuple)) and len(x) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in x) and x[0] <= x[1])


def _lookup(cards: Dict[str, Card], key: str) -> Optional[ParamSpec]:
    instr, _, param = key.partition(".")
    card = cards.get(instr)
    return card.params.get(param) if card else None


def load_lab_config(source: Any, registry: CardRegistry) -> LabConfig:
    data, src = read_yaml(source)
    errors: List[str] = []

    def err(path: str, msg: str) -> None:
        errors.append("%s: %s" % (path, msg))

    if not isinstance(data, dict):
        raise ConfigError(src, ["a lab config must be a mapping"])
    raw_instruments = data.get("instruments")
    if not isinstance(raw_instruments, dict) or not raw_instruments:
        raise ConfigError(src, ["instruments: at least one instrument is required"])

    instruments: Dict[str, InstrumentConfig] = {}
    cards: Dict[str, Card] = {}
    raw_guards: Dict[str, Any] = {}
    for name, d in raw_instruments.items():
        name = str(name)
        path = "instruments.%s" % name
        if not name.isidentifier():
            err(path, "names must be simple identifiers")
            continue
        if not isinstance(d, dict):
            err(path, "must be a mapping")
            continue
        try:
            card = registry.get(str(d.get("card")))
        except KeyError as e:
            err(path + ".card", e.args[0])
            continue
        cards[name] = card
        trust = d.get("trust", "ask")
        if trust not in TRUST:
            err(path + ".trust", "must be ask or envelope")
        limits: Dict[str, Tuple[float, float]] = {}
        for p, lim in (d.get("limits") or {}).items():
            spec = card.params.get(p)
            if spec is None or not spec.numeric:
                err(path + ".limits." + str(p), "not a numeric parameter of %s" % card.id)
            elif not _pair(lim):
                err(path + ".limits." + str(p), "must be [low, high]")
            elif spec.limits and (lim[0] < spec.limits[0] or lim[1] > spec.limits[1]):
                err(path + ".limits." + str(p), "[%s, %s] exceeds vendor limits [%s, %s]"
                    % (num(lim[0]), num(lim[1]), num(spec.limits[0]), num(spec.limits[1])))
            else:
                limits[p] = (float(lim[0]), float(lim[1]))
        rates: Dict[str, float] = {}
        for p, r in (d.get("rates") or {}).items():
            spec = card.params.get(p)
            if spec is None or spec.ramp != "gate":
                err(path + ".rates." + str(p), "only gate-ramped parameters have rates")
            elif not isinstance(r, (int, float)) or r <= 0 or (spec.max_rate and r > spec.max_rate):
                err(path + ".rates." + str(p), "must be > 0 and ≤ the card's max_rate")
            else:
                rates[p] = float(r)
        raw_guards[name] = d.get("guards") or {}
        auto: Dict[str, Any] = {}
        raw_auto = d.get("auto")
        if raw_auto == "never":
            auto["*"] = "never"
        elif isinstance(raw_auto, dict):
            for p, rule in raw_auto.items():
                apath = "%s.auto.%s" % (path, p)
                spec = card.params.get(str(p))
                if spec is None or not spec.is_drive:
                    err(apath, "auto rules belong to drives")
                elif rule == "never":
                    auto[str(p)] = "never"
                elif spec.numeric:
                    lim = limits.get(str(p)) or spec.limits
                    if not _pair(rule):
                        err(apath, "must be never or [low, high]")
                    elif lim and (rule[0] < lim[0] or rule[1] > lim[1]):
                        err(apath, "[%s, %s] exceeds limits [%s, %s]" % (num(rule[0]), num(rule[1]), num(lim[0]), num(lim[1])))
                    else:
                        auto[str(p)] = Allowed(lo=float(rule[0]), hi=float(rule[1]))
                else:
                    try:
                        auto[str(p)] = Allowed(values=frozenset(spec.coerce(v) for v in (rule if isinstance(rule, list) else [rule])))
                    except ValueError as e:
                        err(apath, str(e))
        elif raw_auto is not None:
            err(path + ".auto", "must be never or a mapping of parameter: never | [low, high]")
        connection: Dict[str, Any] = {}
        for key, value in (d.get("connection") or {}).items():
            kind = CONNECTION_KEYS.get(key)
            if kind is None:
                err("%s.connection.%s" % (path, key), "unknown setting (known: %s)" % ", ".join(sorted(CONNECTION_KEYS)))
            elif kind is bool and not isinstance(value, bool):
                err("%s.connection.%s" % (path, key), "must be true or false")
            else:
                try:
                    connection[key] = kind(value)
                except (TypeError, ValueError):
                    err("%s.connection.%s" % (path, key), "must be a %s" % kind.__name__)
        card_vars: Dict[str, str] = {}
        raw_vars = d.get("vars") or {}
        if not isinstance(raw_vars, dict):
            err(path + ".vars", "must be a mapping, e.g. {sensor: MB1.T1}")
        elif raw_vars:
            card_vars = {str(k): str(v) for k, v in raw_vars.items()}
            try:
                card.bind(card_vars)
            except CardError as e:
                for problem in e.errors:
                    err(path + "." + problem.split(":", 1)[0], problem.split(":", 1)[-1].strip())
        added_by = d.get("added_by") or ""
        if added_by not in ("", "kelvin"):
            err(path + ".added_by", "only Kelvin writes this (added_by: kelvin); delete the line once you have "
                                    "reviewed the limits")
            added_by = "kelvin"
        instruments[name] = InstrumentConfig(
            name=name, card=card.id, address=str(d.get("address", "sim://" + name)),
            trust=trust, limits=limits, rates=rates, auto=auto, connection=connection, vars=card_vars,
            added_by=added_by)

    def readable(key: str, path: str) -> bool:
        spec = _lookup(cards, key)
        if spec is None or spec.get_cmd is None:
            err(path, "%s is not a readable parameter" % key)
            return False
        return True

    for name, gmap in raw_guards.items():
        card = cards[name]
        for p, glist in gmap.items():
            path = "instruments.%s.guards.%s" % (name, p)
            spec = card.params.get(p)
            if spec is None or not spec.is_drive:
                err(path, "guards belong to drives")
                continue
            out = []
            for g in glist if isinstance(glist, list) else [glist]:
                if not isinstance(g, dict) or "read" not in g or "max_abs" not in g:
                    err(path, "a guard needs read and max_abs")
                    continue
                key = str(g["read"]) if "." in str(g["read"]) else "%s.%s" % (name, g["read"])
                on_trip = g.get("on_trip", "safe")
                if on_trip not in ON_TRIP:
                    err(path, "on_trip must be safe or hold")
                if readable(key, path):
                    gi, _, gp = key.partition(".")
                    out.append(Guard(gi, gp, float(g["max_abs"]), on_trip))
            instruments[name].guards[p] = out

    interlocks: List[Interlock] = []
    for i, lk in enumerate(data.get("interlocks") or []):
        path = "interlocks[%d]" % i
        try:
            cond = parse_condition(lk.get("require"))
        except (ValueError, AttributeError) as e:
            err(path, str(e))
            continue
        spec = _lookup(cards, str(lk.get("drive")))
        if spec is None or not spec.is_drive:
            err(path, "%r is not a drive" % lk.get("drive"))
        elif readable(cond.key, path):
            interlocks.append(Interlock(str(lk["drive"]), cond))

    safe_actions: Dict[str, str] = {}
    for key, action in (data.get("safe_actions") or {}).items():
        spec = _lookup(cards, str(key))
        if spec is None or not spec.is_drive:
            err("safe_actions." + str(key), "not a drive")
        elif action not in ("hold", "safe"):
            err("safe_actions." + str(key), "must be hold or safe")
        else:
            safe_actions[str(key)] = action

    watches: List[Dict[str, Any]] = []
    for i, w in enumerate(data.get("watch") or []):
        path = "watch[%d]" % i
        if not isinstance(w, dict):
            err(path, "must be a mapping: {read, above, below, for, every, then}")
            continue
        spec = _lookup(cards, str(w.get("read")))
        if spec is None or spec.get_cmd is None or not spec.numeric:
            err(path + ".read", "%r is not a numeric reading in this lab" % w.get("read"))
            continue
        nums = {}
        for k in ("above", "below", "for", "every"):
            if w.get(k) is not None:
                if isinstance(w[k], bool) or not isinstance(w[k], (int, float)):
                    err("%s.%s" % (path, k), "must be a number")
                else:
                    nums[k] = float(w[k])
        if "above" not in nums and "below" not in nums:
            err(path, "needs above, below or both")
        then = []
        for key, value in (w.get("then") or {}).items():
            dspec = _lookup(cards, str(key))
            if dspec is None or not dspec.is_drive:
                err("%s.then.%s" % (path, key), "not a drive")
                continue
            try:
                v = dspec.coerce(value)
            except ValueError as e:
                err("%s.then.%s" % (path, key), str(e))
                continue
            if v != dspec.safe:  # a watch may only make things safer
                err("%s.then.%s" % (path, key), "a watch may only send a drive to its safe value (%s)" % dspec.show(dspec.safe))
                continue
            instr, _, param = str(key).partition(".")
            then.append((instr, param, v))
        watches.append({"read": str(w["read"]), "above": nums.get("above"), "below": nums.get("below"),
                        "for": nums.get("for", 0.0), "every": nums.get("every", 10.0), "then": then,
                        "stop_run": bool(w.get("stop_run", bool(then))), "note": str(w.get("note") or "")})

    fraction = data.get("auto_fraction", AUTO_FRACTION)
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0 <= fraction <= 1:
        err("auto_fraction", "must be a number from 0 to 1")
        fraction = AUTO_FRACTION

    observe = data.get("observe_only", False)
    if not isinstance(observe, bool):
        err("observe_only", "must be true or false")
        observe = True

    if errors:
        raise ConfigError(src, errors)
    return LabConfig(str(data.get("lab", "Lab")), instruments, interlocks, safe_actions, src, float(fraction), watches,
                     observe)


def same_setup(a: LabConfig, b: LabConfig) -> bool:
    """Do two lab configs set up the same instruments with the same limits and rules, Kelvin's added_by marks (and
    observe_only) aside? Then marking them reviewed changes nothing to reconnect."""
    def allowed(rule: Any) -> Any:
        if isinstance(rule, Allowed):
            return rule.lo, rule.hi, sorted(map(str, rule.values)) if rule.values is not None else None
        return rule

    def signature(cfg: LabConfig) -> Any:
        instruments = [(n, ic.card, ic.address, ic.trust, sorted(ic.limits.items()), sorted(ic.rates.items()),
                        sorted((p, [(g.instr, g.param, g.max_abs, g.on_trip) for g in gs]) for p, gs in ic.guards.items()),
                        sorted((p, allowed(r)) for p, r in ic.auto.items()), sorted(ic.connection.items()),
                        sorted(ic.vars.items())) for n, ic in sorted(cfg.instruments.items())]
        return (cfg.name, instruments, [(lk.drive, lk.require.text) for lk in cfg.interlocks],
                sorted(cfg.safe_actions.items()), cfg.auto_fraction, json.dumps(cfg.watches, sort_keys=True, default=str))
    return signature(a) == signature(b)


@dataclass
class Envelope:
    """Per-project region where drive increases run without asking, for
    instruments whose trust level is `envelope`."""
    project: str
    ranges: Dict[str, Allowed]

    def covers(self, key: str, value: Any) -> bool:
        a = self.ranges.get(key)
        return a is not None and a.covers(value)


def load_envelope(source: Any, config: LabConfig, registry: CardRegistry) -> Envelope:
    data, src = read_yaml(source)
    errors: List[str] = []
    ranges: Dict[str, Allowed] = {}
    for key, spec_range in ((data or {}).get("envelope") or {}).items():
        key = str(key)
        instr, _, param = key.partition(".")
        ic = config.instruments.get(instr)
        spec = registry.get(ic.card).params.get(param) if ic else None
        if spec is None or not spec.is_drive:
            errors.append("%s: not a drive in this lab" % key)
            continue
        if spec.numeric:
            lim = ic.limits.get(param) or spec.limits
            if not _pair(spec_range):
                errors.append("%s: must be [low, high]" % key)
            elif spec_range[0] < lim[0] or spec_range[1] > lim[1]:
                errors.append("%s: [%s, %s] exceeds limits [%s, %s]"
                              % (key, num(spec_range[0]), num(spec_range[1]), num(lim[0]), num(lim[1])))
            else:
                ranges[key] = Allowed(lo=float(spec_range[0]), hi=float(spec_range[1]))
        else:
            try:
                vals = spec_range if isinstance(spec_range, list) else [spec_range]
                ranges[key] = Allowed(values=frozenset(spec.coerce(v) for v in vals))
            except ValueError as e:
                errors.append("%s: %s" % (key, e))
    if errors:
        raise ConfigError(src, errors)
    return Envelope(str((data or {}).get("project", "project")), ranges)
