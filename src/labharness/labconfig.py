"""Lab configuration (set by the instrument owner) and project envelopes.

Limits nest: vendor limits (card) ⊇ lab limits (this file) ⊇ project
envelope. The model can read all of them and change none of them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from .cards import Card, CardRegistry, ParamSpec, num
from .conditions import Condition, parse_condition
from .yamlio import load_yaml

TRUST = ("ask", "envelope")
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
        instruments[name] = InstrumentConfig(
            name=name, card=card.id, address=str(d.get("address", "sim://" + name)),
            trust=trust, limits=limits, rates=rates)

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

    if errors:
        raise ConfigError(src, errors)
    return LabConfig(str(data.get("lab", "Lab")), instruments, interlocks, safe_actions, src)


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
