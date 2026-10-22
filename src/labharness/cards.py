"""Instrument cards: one file per instrument model.

A card says how to talk to an instrument and tags every parameter as
*measure* (read or configure freely) or *drive* (changes what reaches the
sample or a sensitive part). Each drive has a safe value; the safety gate
guards moves away from it.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .scpi import Template
from .yamlio import load_yaml

KINDS = ("measure", "drive")
TYPES = ("float", "int", "bool", "enum", "str", "trace")
RAMPS = ("gate", "instrument", "none")
STATUSES = ("community", "lab-verified", "draft")
CONNECTION_KEYS = {"read_termination": str, "write_termination": str, "baud_rate": int, "data_bits": int,
                   "stop_bits": float, "parity": str, "timeout_ms": int, "query_delay": float, "write_only": bool,
                   "ack": str,   # ack: the line an instrument sends after every command (e.g. "OK"), read and checked
                   "set_reply": str}   # a regex every set command's reply must match (e.g. ":VALID$"); read after each set
VAR_NAME = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
VAR_VALUE = re.compile(r"^[A-Za-z0-9_.+-]{1,40}$")   # never spaces, separators or line ends: no command can hide in it
_TRUE = {"1", "on", "true", "yes", "open"}
_FALSE = {"0", "off", "false", "no", "closed", "close"}


class CardError(ValueError):
    def __init__(self, source: str, errors: List[str]):
        self.errors = list(errors)
        super().__init__("%s: %s" % (source, "; ".join(self.errors)))


def num(x: float) -> str:
    return "%.4g" % x


SI_UNITS = {"V", "A", "Hz", "W", "s", "Ω", "T", "K", "F", "H", "S", "m"}
_PREFIXES = [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p"), (1e-15, "f")]
_PREFIX_OF = {p: f for f, p in _PREFIXES if p}


def si_parts(x: float, unit: str) -> Tuple[float, float, str]:
    """(value in the shown unit, divisor applied, shown unit): 3.99e9, 'Hz' -> (3.99, 1e9, 'GHz')."""
    if x != 0 and unit not in SI_UNITS and len(unit) > 1 and unit[0] in _PREFIX_OF and unit[1:] in SI_UNITS:
        base = _PREFIX_OF[unit[0]]  # an already-prefixed unit such as mW or mm
        v, f, u = si_parts(x * base, unit[1:])
        return v, f / base, u
    if unit not in SI_UNITS or x == 0 or not math.isfinite(x):
        return x, 1.0, unit
    for factor, prefix in _PREFIXES:
        if abs(x) >= factor * 0.99995:
            break
    return x / factor, factor, prefix + unit


def si(x: float, unit: str) -> str:
    """3.99e9 Hz -> '3.99 GHz', -8.83e-9 V -> '-8.83 nV', -0.0013 mW -> '-1.3 µW'; other units stay plain."""
    v, _, u = si_parts(x, unit)
    return num(v) + (" " + u if u else "")


@dataclass
class ParamSpec:
    name: str
    kind: str
    type: str = "float"
    unit: str = ""
    get_cmd: Optional[Template] = None
    set_cmd: Optional[Template] = None
    limits: Optional[Tuple[float, float]] = None   # vendor hard limits
    values: Optional[Dict[str, str]] = None        # enum name -> wire code
    safe: Any = None
    default: Any = None
    max_rate: Optional[float] = None               # unit per second
    ramp: str = "none"                             # gate | instrument | none
    ramp_step: Optional[float] = None
    readback: Optional[str] = None                 # param reporting the actual value
    tol: float = 0.0
    timeout: float = 600.0
    rate_param: Optional[str] = None               # param holding the instrument's ramp rate
    rate_per: str = "s"
    enables: Tuple[str, ...] = ()                  # drives this switch applies to the sample
    bool_text: Tuple[str, str] = ("1", "0")
    axis: Optional[Tuple[str, str]] = None         # trace x-axis: (start param, stop param)
    applies_when: Optional[str] = None             # drive that must be on for this one to reach the sample
    label: str = ""                                # human name, e.g. "Compliance"
    words: Optional[Tuple[str, str]] = None        # how a bool reads, e.g. ("open", "closed")
    field: Optional[int] = None                    # reply is comma-separated: take this element (0-based)
    parse: Optional[str] = None                    # regex whose first group is the value in a verbose reply
    read_when: Optional[str] = None                # only query while this switch is on (some reads turn outputs on)
    description: str = ""

    @property
    def title(self) -> str:
        return self.label or self.name.replace("_", " ")

    @property
    def is_drive(self) -> bool:
        return self.kind == "drive"

    @property
    def numeric(self) -> bool:
        return self.type in ("float", "int")

    def coerce(self, value: Any) -> Any:
        """Turn user or model input into this parameter's canonical value."""
        t = self.type
        if t in ("float", "int"):
            if isinstance(value, bool):
                raise ValueError("%s needs a number, got %r" % (self.name, value))
            try:
                x = float(value)
            except (TypeError, ValueError):
                raise ValueError("%s needs a number, got %r" % (self.name, value))
            if not math.isfinite(x):
                raise ValueError("%s needs a finite number" % self.name)
            if t == "int":
                if x != int(x):
                    raise ValueError("%s needs an integer, got %r" % (self.name, value))
                return int(x)
            return x
        if t == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value)
            s = str(value).strip().lower()
            if s in _TRUE or s == self.bool_text[0].lower():
                return True
            if s in _FALSE or s == self.bool_text[1].lower():
                return False
            raise ValueError("%s needs on/off, got %r" % (self.name, value))
        if t == "enum":
            if isinstance(value, bool):  # YAML reads a bare `off` as False
                value = "on" if value else "off"
            s = str(value).strip().lower()
            for name, code in (self.values or {}).items():
                if s == name.lower() or s == str(code).lower():
                    return name
            raise ValueError("%s must be one of %s, got %r" % (self.name, ", ".join(self.values or {}), value))
        if t == "str":
            return str(value)
        raise ValueError("%s is a trace and cannot be set" % self.name)

    def family(self) -> Any:
        """Every command that touches this parameter, listed or not (see Template.header_prefix)."""
        words = list(self.bool_text) if self.type == "bool" else (
            [str(v) for v in (self.values or {}).values()] if self.type == "enum" else None)
        return self.set_cmd.header_prefix(words)

    def to_wire(self, value: Any) -> str:
        if self.type == "float":
            return "%.12g" % value
        if self.type == "int":
            return str(int(value))
        if self.type == "bool":
            return self.bool_text[0] if value else self.bool_text[1]
        if self.type == "enum":
            return str((self.values or {})[value])
        if self.type == "trace":
            return ",".join("%.6g" % x for x in value)
        return str(value)

    def from_wire(self, text: str) -> Any:
        text = text.strip()
        if self.parse:
            m = re.search(self.parse, text)
            if m:
                text = (m.group(1) if m.groups() else m.group(0)).strip()
        if self.field is not None and self.type != "trace":
            parts = [x.strip() for x in text.split(",")]
            if len(parts) > self.field:
                text = parts[self.field]
        if self.type == "trace":
            return [float(x) for x in text.split(",") if x.strip()]
        if self.type == "str":
            return text
        return self.coerce(text)

    def show(self, value: Any) -> str:
        if value is None:
            return "?"
        if self.type == "bool":
            on, off = self.words or ("on", "off")
            return on if value else off
        if self.type == "trace":
            return "trace (%d points)" % len(value)
        if self.numeric and isinstance(value, (int, float)):
            return si(value, self.unit)
        return str(value) + (" " + self.unit if self.unit and self.numeric else "")

    def distance(self, value: Any) -> float:
        """How far a drive value is from safe (0 = at safe)."""
        if self.numeric:
            return abs(value - self.safe)
        if self.type == "enum":
            names = list(self.values or {})
            return abs(names.index(value) - names.index(self.safe))
        return 0.0 if value == self.safe else 1.0


@dataclass
class HealthCheck:
    query: Optional[str] = None
    expect: Optional[str] = None
    read: Optional[str] = None
    range: Optional[Tuple[float, float]] = None


@dataclass
class Card:
    id: str
    model: str
    kind: str
    status: str
    params: Dict[str, ParamSpec]
    vendor: str = ""
    idn_pattern: Optional[str] = None
    idn_example: Optional[str] = None
    error_query: Optional[Template] = None
    safe_sequence: List[str] = field(default_factory=list)
    health: List[HealthCheck] = field(default_factory=list)
    reset: List[Template] = field(default_factory=list)
    blocked: List[Template] = field(default_factory=list)
    twin: Optional[str] = None
    headline: Optional[str] = None                 # the reading to feature in the lab panel
    notes: List[str] = field(default_factory=list)
    manual: str = ""
    manual_url: str = ""                                        # where the maker publishes that manual (https)
    source: str = ""
    connection: Dict[str, Any] = field(default_factory=dict)   # terminations, serial settings, timeouts
    idn_query: str = "*IDN?"                                    # what discovery asks to identify the model
    usb_ids: List[str] = field(default_factory=list)            # "VID:PID" in hex, for serial-over-USB devices
    on_connect: List[str] = field(default_factory=list)         # configuration commands sent when connecting
    vars: Dict[str, Dict[str, str]] = field(default_factory=dict)   # per-installation names: {name: {default, label}}
    bound: Dict[str, str] = field(default_factory=dict)             # the values this copy was made with
    data: Dict[str, Any] = field(default_factory=dict, repr=False)  # as written, to bind other values
    library: Dict[str, Any] = field(default_factory=dict)            # the maker's module or DLL (library.py)

    def bind(self, values: Optional[Dict[str, Any]]) -> "Card":
        """This card with its variables set for one instrument (which sensor, channel or output it uses)."""
        if not self.vars or not values:
            return self
        return parse_card(self.data, self.source, values)

    def param(self, name: str) -> ParamSpec:
        try:
            return self.params[name]
        except KeyError:
            raise KeyError("%s has no parameter %r (has: %s)" % (self.id, name, ", ".join(self.params)))

    @property
    def drives(self) -> List[ParamSpec]:
        return [p for p in self.params.values() if p.is_drive]

    @property
    def setters(self) -> List[ParamSpec]:
        """Parameters with a set command, most specific command first (OUTPut2 before OUTPut), so a command
        that could fit two templates goes to the one written for it."""
        return sorted((p for p in self.params.values() if p.set_cmd is not None), key=lambda p: -len(p.set_cmd.text))

    @property
    def getters(self) -> List[ParamSpec]:
        return sorted((p for p in self.params.values() if p.get_cmd is not None), key=lambda p: -len(p.get_cmd.text))


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _template(text: Any, err: Callable[[str, str], None], path: str, prefix: bool = False) -> Any:
    try:
        return Template(str(text)).header_prefix() if prefix else Template(str(text))   # PUT /x: not every PUT
    except ValueError as e:
        err(path, str(e))
        return None


def _parse_param(name: str, p: Any, err: Callable[[str, str], None]) -> Optional[ParamSpec]:
    path = "parameters.%s" % name
    if not isinstance(p, dict):
        err(path, "must be a mapping")
        return None
    kind = p.get("kind")
    if kind not in KINDS:
        err(path + ".kind", "must be measure or drive")
        kind = "measure"
    typ = p.get("type", "float")
    if typ not in TYPES:
        err(path + ".type", "must be one of %s" % ", ".join(TYPES))
        typ = "float"
    get_cmd = _template(p["get"], err, path + ".get") if p.get("get") else None
    set_cmd = _template(p["set"], err, path + ".set") if p.get("set") else None
    if set_cmd is not None and not set_cmd.has_value:
        err(path + ".set", "needs {value}")
    if get_cmd is not None and get_cmd.has_value:
        err(path + ".get", "must not contain {value}")
    if not p.get("get") and not p.get("set"):
        err(path, "needs a get or set command")

    values = None
    if typ == "enum":
        v = p.get("values")
        if isinstance(v, list) and v:
            values = {str(x): str(x) for x in v}
        elif isinstance(v, dict) and v:
            values = {str(k): str(c) for k, c in v.items()}
        else:
            err(path + ".values", "an enum needs a list or mapping of values")
            values = {}

    limits = None
    if "limits" in p:
        lim = p["limits"]
        if typ not in ("float", "int"):
            err(path + ".limits", "only numbers have limits")
        elif not (isinstance(lim, (list, tuple)) and len(lim) == 2 and all(_is_number(x) for x in lim) and lim[0] < lim[1]):
            err(path + ".limits", "must be [low, high] with low < high")
        else:
            limits = (float(lim[0]), float(lim[1]))

    def opt_float(key: str) -> Optional[float]:
        if p.get(key) is None:
            return None
        if not _is_number(p[key]):
            err(path + "." + key, "must be a number")
            return None
        return float(p[key])

    spec = ParamSpec(
        name=name, kind=kind, type=typ, unit=str(p.get("unit", "")),
        get_cmd=get_cmd, set_cmd=set_cmd, limits=limits, values=values,
        max_rate=opt_float("max_rate"), ramp=p.get("ramp", "none"),
        ramp_step=opt_float("ramp_step"), readback=p.get("readback"),
        tol=opt_float("tol") or 0.0, timeout=opt_float("timeout") or 600.0,
        rate_param=p.get("rate_param"), rate_per=p.get("rate_per", "s"),
        enables=tuple(p.get("enables") or ()),
        bool_text=tuple(str(x) for x in p.get("bool_text", ("1", "0"))),
        label=str(p.get("label", "")),
        applies_when=p.get("applies_when"),
        description=str(p.get("description", "")),
    )
    if p.get("field") is not None:
        if not isinstance(p["field"], int) or isinstance(p["field"], bool) or p["field"] < 0:
            err(path + ".field", "must be a 0-based element index")
        else:
            spec.field = p["field"]
    if p.get("read_when") is not None:
        spec.read_when = str(p["read_when"])
    if p.get("parse") is not None:
        try:
            re.compile(str(p["parse"]))
            spec.parse = str(p["parse"])
        except re.error as e:
            err(path + ".parse", "not a valid regex: %s" % e)
    words = p.get("words")
    if words is not None:
        if typ != "bool" or not (isinstance(words, (list, tuple)) and len(words) == 2):
            err(path + ".words", "only bools have words: [on_word, off_word]")
        else:
            spec.words = (str(words[0]), str(words[1]))
    axis = p.get("axis")
    if axis is not None:
        if typ != "trace" or not isinstance(axis, dict) or not axis.get("start") or not axis.get("stop"):
            err(path + ".axis", "only traces have an axis: {start: param, stop: param}")
        else:
            spec.axis = (str(axis["start"]), str(axis["stop"]))
    if spec.ramp not in RAMPS:
        err(path + ".ramp", "must be gate, instrument or none")
    if spec.rate_per not in ("s", "min"):
        err(path + ".rate_per", "must be s or min")
    if len(spec.bool_text) != 2:
        err(path + ".bool_text", "must be [on_text, off_text]")
        spec.bool_text = ("1", "0")
    elif any(isinstance(x, bool) for x in p.get("bool_text") or ()):   # YAML reads a bare ON / OFF as true / false
        err(path + ".bool_text", "quote the words, e.g. [\"ON\", \"OFF\"]")

    for key in ("safe", "default"):
        if p.get(key) is None:
            continue
        try:
            val = spec.coerce(p[key])
        except ValueError as e:
            err(path + "." + key, str(e))
            continue
        if limits and not limits[0] <= val <= limits[1]:
            err(path + "." + key, "%s is outside limits [%s, %s]" % (num(val), num(limits[0]), num(limits[1])))
            continue
        setattr(spec, key, val)

    if kind == "drive":
        if p.get("set") is None:
            err(path, "a drive needs a set command")
        if typ not in ("float", "int", "bool", "enum"):
            err(path + ".type", "a drive must be float, int, bool or enum")
        if spec.safe is None:
            err(path + ".safe", "a drive needs a safe value")
        if spec.numeric and limits is None:
            err(path + ".limits", "a numeric drive needs limits")
    if spec.ramp == "gate":
        if not spec.numeric:
            err(path + ".ramp", "only numbers can be ramped")
        if not spec.max_rate or spec.max_rate <= 0:
            err(path + ".max_rate", "gate ramping needs max_rate > 0")
    return spec


def _card_vars(data: Dict[str, Any], err: Callable[[str, str], None]) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    raw = data.get("vars") or {}
    if not isinstance(raw, dict):
        err("vars", "must be a mapping of name: default (or {default, label})")
        return out
    for name, v in raw.items():
        spec = v if isinstance(v, dict) else {"default": v}
        default = str(spec.get("default", ""))
        if not VAR_NAME.match(str(name)) or name == "value":
            err("vars." + str(name), "a name is lowercase letters, digits and _ (and not value)")
        elif not VAR_VALUE.match(default):
            err("vars." + str(name), "the default %r must be letters, digits and . _ + - only" % default)
        else:
            out[str(name)] = {"default": default, "label": str(spec.get("label") or name)}
    return out


def _substitute(obj: Any, values: Dict[str, str]) -> Any:
    if isinstance(obj, str):
        for k, v in values.items():
            obj = obj.replace("{%s}" % k, v)
        return obj
    if isinstance(obj, list):
        return [_substitute(x, values) for x in obj]
    if isinstance(obj, dict):
        return {k: _substitute(v, values) for k, v in obj.items()}
    return obj


def parse_card(data: Any, source: str = "card", values: Optional[Dict[str, Any]] = None) -> Card:
    errors: List[str] = []

    def err(path: str, msg: str) -> None:
        errors.append("%s: %s" % (path, msg))

    if not isinstance(data, dict):
        raise CardError(source, ["a card must be a mapping"])
    if data.get("card") != 1:
        err("card", "schema version must be 1")
    original = data
    card_vars = _card_vars(data, err)
    chosen = {k: v["default"] for k, v in card_vars.items()}
    for k, v in (values or {}).items():
        if k not in card_vars:
            err("vars." + str(k), "the card has no variable %r (it has: %s)" % (k, ", ".join(card_vars) or "none"))
        elif not VAR_VALUE.match(str(v)):
            err("vars." + str(k), "%r: letters, digits and . _ + - only" % v)
        else:
            chosen[k] = str(v)
    if card_vars:
        data = _substitute({k: v for k, v in data.items() if k != "vars"}, chosen)
    leftover = sorted(set(re.findall(r"\{([a-z_][a-z0-9_]*)\}", repr(data))) - {"value", "address"})
    if leftover:
        err("vars", "the commands use %s, which the card does not declare under vars" % ", ".join(leftover))
    for key in ("id", "model", "kind"):
        if not data.get(key):
            err(key, "required")
    status = data.get("status", "draft")
    if status not in STATUSES:
        err("status", "must be one of %s" % ", ".join(STATUSES))

    raw_params = data.get("parameters")
    params: Dict[str, ParamSpec] = {}
    if not isinstance(raw_params, dict) or not raw_params:
        err("parameters", "at least one parameter is required")
    else:
        for name, p in raw_params.items():
            spec = _parse_param(str(name), p, err)
            if spec is not None:
                params[str(name)] = spec

    for spec in params.values():
        path = "parameters.%s" % spec.name
        if spec.readback is not None:
            rb = params.get(spec.readback)
            if rb is None or rb.get_cmd is None or not rb.numeric:
                err(path + ".readback", "must name a readable numeric parameter")
        if spec.rate_param is not None and spec.rate_param not in params:
            err(path + ".rate_param", "unknown parameter %r" % spec.rate_param)
        for dep in spec.enables:
            if dep not in params or not params[dep].is_drive:
                err(path + ".enables", "%r is not a drive on this card" % dep)
        if spec.applies_when is not None and (spec.applies_when not in params or not params[spec.applies_when].is_drive
                                              or params[spec.applies_when].get_cmd is None):
            err(path + ".applies_when", "%r is not a readable drive on this card" % spec.applies_when)
        if spec.read_when is not None and (spec.read_when not in params or params[spec.read_when].type != "bool"):
            err(path + ".read_when", "%r is not an on/off parameter on this card" % spec.read_when)
        for ax in spec.axis or ():
            if ax not in params or params[ax].get_cmd is None or not params[ax].numeric:
                err(path + ".axis", "%r is not a readable numeric parameter" % ax)

    drives = [n for n, s in params.items() if s.is_drive]
    seq = [str(n) for n in (data.get("safe_sequence") or [])]
    for n in seq:
        if n not in drives:
            err("safe_sequence", "%r is not a drive" % n)
    seq = [n for n in seq if n in drives] + [n for n in drives if n not in seq]

    health: List[HealthCheck] = []
    for i, h in enumerate(data.get("health") or []):
        path = "health[%d]" % i
        if isinstance(h, dict) and h.get("query"):
            health.append(HealthCheck(query=str(h["query"]), expect=h.get("expect")))
        elif isinstance(h, dict) and h.get("read"):
            r = h.get("range")
            if h["read"] not in params:
                err(path, "unknown parameter %r" % h["read"])
            elif not (isinstance(r, (list, tuple)) and len(r) == 2 and all(_is_number(x) for x in r)):
                err(path, "a read check needs range: [low, high]")
            else:
                health.append(HealthCheck(read=str(h["read"]), range=(float(r[0]), float(r[1]))))
        else:
            err(path, "needs query (+ expect) or read (+ range)")

    raw = data.get("raw") or {}
    reset = [t for t in (_template(x, err, "raw.reset", True) for x in raw.get("reset") or []) if t]
    blocked = [t for t in (_template(x, err, "raw.blocked", True) for x in raw.get("blocked") or []) if t]
    error_query = _template(data["error_query"], err, "error_query") if data.get("error_query") else None
    identify = data.get("identify") or {}
    if identify.get("idn"):
        try:
            re.compile(str(identify["idn"]))
        except re.error as e:
            err("identify.idn", "not a valid regex: %s" % e)
    usb_ids = [str(u).lower() for u in identify.get("usb") or []]
    for u in usb_ids:
        if not re.match(r"^[0-9a-f]{4}:[0-9a-f]{4}$", u):
            err("identify.usb", "%r is not VID:PID in hex" % u)
    connection: Dict[str, Any] = {}
    for key, value in (data.get("connection") or {}).items():
        kind = CONNECTION_KEYS.get(key)
        if kind is None:
            err("connection." + str(key), "unknown setting (known: %s)" % ", ".join(sorted(CONNECTION_KEYS)))
        elif kind is bool and not isinstance(value, bool):
            err("connection." + key, "must be true or false")
        elif kind in (int, float) and not _is_number(value):
            err("connection." + key, "must be a number")
        else:
            connection[key] = kind(value)
    on_connect = [str(c) for c in data.get("on_connect") or []]
    for c in on_connect:  # connecting leaves the instrument as it is: it may only set up the connection itself
        for spec in params.values():
            if spec.set_cmd is not None and spec.family().match(c) is not None:
                err("on_connect", "%r would set %s; connecting leaves every setting as it is (on_connect may only set "
                    "up the connection: listen mode, headers, a driver's wiring)" % (c, spec.name))
    manual_url = str(data.get("manual_url") or "").strip()
    if manual_url and not re.match(r"^https://[^\s/]+\.[^\s/]+/\S*$", manual_url):
        err("manual_url", "must be an https:// address of the maker's manual")
    headline = (data.get("display") or {}).get("headline")
    if headline is not None and (headline not in params or params[headline].get_cmd is None):
        err("display.headline", "%r is not a readable parameter" % headline)
    library = _library(data.get("library"), params, identify, err)
    if library and on_connect:
        err("on_connect", "a card with a library connects with library.open")

    if errors:
        raise CardError(source, errors)
    return Card(
        id=str(data["id"]), model=str(data["model"]), kind=str(data["kind"]), status=status,
        params=params, vendor=str(data.get("vendor", "")),
        idn_pattern=identify.get("idn"), idn_example=identify.get("example"),
        error_query=error_query, safe_sequence=seq, health=health,
        reset=reset, blocked=blocked, twin=data.get("twin"), headline=headline,
        notes=[str(n) for n in data.get("notes") or []], manual=str(data.get("manual", "")), manual_url=manual_url,
        source=source, connection=connection, idn_query=str(identify.get("query") or "*IDN?"), usb_ids=usb_ids,
        on_connect=on_connect, vars=card_vars, bound=chosen, data=original, library=library,
    )


LIB_TYPES = ("int8", "uint8", "int16", "uint16", "int32", "int", "uint32", "int64", "uint64", "float", "double", "bool")
LIB_DENY = ("os", "sys", "subprocess", "shutil", "builtins", "importlib", "runpy", "ctypes", "socket", "pathlib",
            "pickle", "marshal", "multiprocessing", "code", "codeop", "webbrowser", "urllib", "http", "asyncio")
LIB_CALL = re.compile(r"^\s*(SET|GET)\s+([A-Za-z][A-Za-z0-9_]*)\b")
LIB_READS = re.compile(r"^(get|is|has|query|read|measure|fetch|status|current|actual)", re.I)   # names that only read
LIB_ACTS = re.compile(r"^(toggle|start|stop|go|sweep|move|set|apply|run|reset|calibrat|upload|download|delete|"
                      r"write|save|store|shutdown|home|zero|exchange|pump)", re.I)


def _library(lib: Any, params: Dict[str, ParamSpec], identify: Dict[str, Any],
             err: Callable[[str, str], None]) -> Dict[str, Any]:
    """The maker's library and how to call it; see library.py. Checked so that only the maker's own functions,
    named here, can be called, and readings call only functions that read."""
    if lib is None:
        return {}
    if not isinstance(lib, dict):
        err("library", "must be a mapping")
        return {}
    name = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
    out = dict(lib)
    if bool(lib.get("module")) == bool(lib.get("dll")):
        err("library", "name either module (a Python module) or dll (a DLL file), not both")
    if lib.get("module"):
        mod = str(lib["module"])
        if not all(name.match(p) for p in mod.split(".")) or mod.split(".")[0] in LIB_DENY:
            err("library.module", "%r is not a maker's module" % mod)
        if lib.get("object") is not None and not name.match(str(lib["object"])):
            err("library.object", "must be a name")
    functions: Dict[str, Any] = {}
    if lib.get("dll"):
        dll = str(lib["dll"])
        if not re.match(r"^[A-Za-z0-9_.+-]+\.(dll|so|dylib)$", dll, re.I):
            err("library.dll", "a DLL file name in the folder the address names, e.g. attoDRYLib.dll")
        if lib.get("convention", "cdecl") not in ("cdecl", "stdcall"):
            err("library.convention", "cdecl or stdcall")
        if lib.get("returns", "error") not in ("error", "none") + LIB_TYPES:
            err("library.returns", "error (an error code, 0 when fine), none, or a number type")
        if not re.match(r"^[A-Za-z0-9_]*$", str(lib.get("prefix") or "")):
            err("library.prefix", "letters, digits and _ only")
        functions = lib.get("functions") or {}
        if not isinstance(functions, dict) or not functions:
            err("library.functions", "list each function the card calls, with its C arguments and output")
            functions = {}
        for fn, sig in functions.items():
            path = "library.functions.%s" % fn
            if not name.match(str(fn)):
                err(path, "not a function name")
            sig = sig or {}
            if not isinstance(sig, dict):
                err(path, "a mapping with args, out and ret")
                continue
            for a in sig.get("args") or []:
                if a not in LIB_TYPES + ("str",):
                    err(path + ".args", "%r is not one of %s, str" % (a, ", ".join(LIB_TYPES)))
            if sig.get("out") is not None and sig["out"] not in LIB_TYPES + ("str",):
                err(path + ".out", "%r is not one of %s, str" % (sig["out"], ", ".join(LIB_TYPES)))
            if sig.get("ret") is not None and sig["ret"] not in ("error", "none") + LIB_TYPES:
                err(path + ".ret", "error, none, or a number type")
    known = set(functions) if lib.get("dll") else None

    def check_call(path: str, text: Any) -> None:
        parts = [p for p in re.split(r"[\s,]+", str(text).strip()) if p]
        if not parts or not name.match(parts[0]):
            err(path, "%r is not a function and its arguments" % text)
        elif parts[0] != "wait" and known is not None and parts[0] not in known:
            err(path, "%s is not under library.functions" % parts[0])
        elif parts[0] == "wait" and (len(parts) != 2 or not re.match(r"^[0-9.]+$", parts[1])):
            err(path, "wait takes seconds, e.g. wait 10")

    for key in ("open", "close"):
        for i, step in enumerate(lib.get(key) or []):
            check_call("library.%s[%d]" % (key, i), step)
    toggles = lib.get("toggles") or {}
    if not isinstance(toggles, dict):
        err("library.toggles", "a mapping of name: {toggle, state}")
        toggles = {}
    for t, spec in toggles.items():
        if not isinstance(spec, dict) or not spec.get("toggle") or not spec.get("state"):
            err("library.toggles.%s" % t, "needs toggle (the function that flips it) and state (the one that reads it)")
            continue
        for key in ("toggle", "state"):
            check_call("library.toggles.%s.%s" % (t, key), spec[key])
        if not LIB_READS.match(str(spec["state"])):
            err("library.toggles.%s.state" % t, "%s does not look like a function that only reads" % spec["state"])
    for p in params.values():
        for tmpl, verb in ((p.set_cmd, "SET"), (p.get_cmd, "GET")):
            if tmpl is None:
                continue
            m = LIB_CALL.match(tmpl.text)
            path = "parameters.%s.%s" % (p.name, verb.lower())
            if not m or m.group(1).upper() != verb:
                err(path, "with a library, write %s <function> [arguments]" % verb)
                continue
            fn = m.group(2)
            if fn in toggles:
                continue
            if known is not None and fn not in known:
                err(path, "%s is not under library.functions" % fn)
            if verb == "GET" and (not LIB_READS.match(fn) or LIB_ACTS.match(fn)):
                err(path, "%s does not look like a function that only reads (get..., is..., query...); a reading must "
                    "never start anything" % fn)
    query = str(identify.get("query") or "")
    if query and query != "*IDN?":
        m = LIB_CALL.match(query)
        if not m or m.group(1).upper() != "GET" or not LIB_READS.match(m.group(2)):
            err("identify.query", "with a library, GET <a function that reads the model or serial number>")
    out["toggles"] = toggles
    return out


def load_card(path: Any) -> Card:
    path = Path(path)
    with open(path, encoding="utf-8") as f:
        return parse_card(load_yaml(f), str(path))


class CardRegistry:
    """All cards a lab can use: community cards plus the lab's own."""

    def __init__(self) -> None:
        self._cards: Dict[str, Card] = {}

    def add(self, card: Card) -> None:
        if card.id in self._cards:
            raise CardError(card.source, ["duplicate card id %r" % card.id])
        self._cards[card.id] = card

    def load_dir(self, directory: Any) -> None:
        for path in sorted(Path(directory).rglob("*.yaml")):
            self.add(load_card(path))

    def put(self, card: Card) -> None:
        """Add or replace (the user's own cards change while Kelvin runs)."""
        self._cards[card.id] = card

    def remove(self, card_id: str) -> None:
        self._cards.pop(card_id, None)

    def __contains__(self, card_id: object) -> bool:
        return card_id in self._cards

    def get(self, card_id: str) -> Card:
        try:
            return self._cards[card_id]
        except KeyError:
            raise KeyError("no card %r (known: %s)" % (card_id, ", ".join(sorted(self._cards))))

    def __iter__(self):
        return iter(self._cards.values())
