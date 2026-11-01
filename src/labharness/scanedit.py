"""The scan editor's parts (the Runs panel's Edit, as Special Measure's scan editor shows a scan): a plan as a person
edits it, its loops outer first, what it records at each point, what it sets first and at the end, its settle,
repeat and time series; and back to the plan, losslessly: to_plan(to_parts(plan)) == plan, in the order it was
written. What the editor doesn't show as fields (preconditions, until: and while:, a stabilize, triggers, retries,
techniques, a hold per value, a ramp's read:, keys it doesn't know) is kept as YAML text, shown read-only and put back
as it was; so is a part whose value isn't of the kind its field takes (a loop's from: in quotes), whole. Nothing here
reads or drives an instrument: an edited scan runs through the queue, its gate and its approval as any scan does.

    parts = {name, description, loops: [loop, ...], measure: [key, ...], setup: [{key, value}], after: [{key, value}],
             settle, repeat, time: {every, for, points, order} | None,
             kept: YAML text of the plan's other keys, fixed: those keys (the editor's fields for them are read-only),
             order: the plan's keys as written}
    loop  = {key, mode: range | values | ramp | kept, its fields, kept: YAML text of its other keys, fixed, order}
      range:  from, to, and step or points (by: the one the loop gives), log, back, settle
      values: values, back, settle; form: "list" for a bare list of values
      ramp:   legs [{to, rate, hold, bare: a target alone, order}], every; one: a single leg, not a list
      kept:   text, the whole loop as written

A field left empty (None) is not written; a flag (log, back) is written when it is on, or was written. A part the
editor shows only read-only (fixed) wins over its field."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .yamlio import dump_yaml, load_yaml

# the plan's keys in the order Kelvin writes them: where a part the editor adds goes among those the plan has
ORDER = ("plan", "description", "device", "requires", "preconditions", "setup", "sweep", "repeat", "time", "settle",
         "trigger", "technique", "measure", "until", "while", "retry", "after", "on_abort")
SPEC_ORDER = ("values", "from", "to", "step", "points", "log", "back", "ramp", "rate", "every", "settle")
TIME_ORDER = ("every", "for", "points")
LEG_ORDER = ("to", "rate", "hold")
MODES = ("range", "values", "ramp", "kept")
FIELDS = {"plan": "name", "description": "description", "sweep": "loops", "measure": "measure", "setup": "setup",
          "after": "after", "settle": "settle", "repeat": "repeat", "time": "time"}
_LOOP_KEYS = {"values", "from", "to", "step", "points", "log", "back", "ramp", "every", "settle"}   # a field, in a mode
_KEPT = object()


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _plain(v: Any) -> bool:
    """A value a field shows as it is: a word, a switch, a finite number."""
    return isinstance(v, (str, bool)) or _number(v)


def _text(data: Any) -> str:
    return dump_yaml(data) if data not in ({}, None) else ""


def _mapping(text: Any) -> Dict[Any, Any]:
    """Kept YAML text back to its mapping ({} for none)."""
    if not text:
        return {}
    data = load_yaml(str(text))
    if not isinstance(data, dict):
        raise ValueError("what is kept as written is a mapping of its keys")
    return data


# ------------------------------------------------------------------ the plan → its parts

def to_parts(data: Any) -> Dict[str, Any]:
    """A plan (a mapping, as load_plan takes it) as the editor's parts."""
    if not isinstance(data, dict):
        raise ValueError("a plan is a mapping of its keys")
    parts: Dict[str, Any] = {"name": "", "description": "", "loops": [], "measure": [], "setup": [], "after": [],
                             "settle": None, "repeat": None, "time": None, "order": [str(k) for k in data]}
    kept: Dict[Any, Any] = {}
    for key, value in data.items():
        shown = _shown(key, value)
        if shown is _KEPT:
            kept[key] = value
        else:
            parts[FIELDS[key]] = shown
    for key in kept:   # read-only in the editor: not its field
        if key in FIELDS:
            parts[FIELDS[key]] = None
    parts["kept"] = _text(kept)
    parts["fixed"] = [str(k) for k in kept]
    return parts


def _shown(key: Any, value: Any) -> Any:
    """A key of the plan as its field shows it, or _KEPT."""
    if key in ("plan", "description"):
        return value if isinstance(value, str) else _KEPT
    if key in ("setup", "after"):
        if isinstance(value, dict) and all(isinstance(k, str) and _plain(v) for k, v in value.items()):
            return [{"key": k, "value": v} for k, v in value.items()]
        return _KEPT
    if key == "measure":
        return list(value) if isinstance(value, list) and all(isinstance(m, str) for m in value) else _KEPT
    if key in ("settle", "repeat"):
        return value if _number(value) else _KEPT
    if key == "time":
        if isinstance(value, dict) and value and all(k in TIME_ORDER and _number(v) for k, v in value.items()):
            return dict({k: value.get(k) for k in TIME_ORDER}, order=list(value))
        return _KEPT
    if key == "sweep":
        if isinstance(value, dict) and all(isinstance(k, str) for k in value):
            return [_loop(k, spec) for k, spec in value.items()]
        return _KEPT
    return _KEPT


def _blank(key: str, mode: str) -> Dict[str, Any]:
    return {"key": key, "mode": mode, "from": None, "to": None, "step": None, "points": None, "by": "step",
            "log": False, "back": False, "values": [], "form": "", "legs": [], "one": False, "every": None,
            "settle": None, "kept": "", "fixed": [], "order": []}


def _loop(key: str, spec: Any) -> Dict[str, Any]:
    """One loop of the sweep as its fields; one the editor can't show exactly is kept whole, as written."""
    whole = dict(_blank(key, "kept"), text=dump_yaml(spec))
    if isinstance(spec, list):
        return dict(_blank(key, "values"), values=list(spec), form="list") if all(map(_plain, spec)) else whole
    if not isinstance(spec, dict) or not spec or not all(isinstance(k, str) for k in spec):
        return whole
    mode = "ramp" if "ramp" in spec else "values" if "values" in spec else "range" if (
        "from" in spec or "to" in spec) else None
    if mode is None:
        return whole
    loop = dict(_blank(key, mode), order=list(spec))
    kept: Dict[str, Any] = {}
    for k, v in spec.items():
        took = _take(loop, mode, k, v, spec)
        if took is None:
            return whole
        if not took:
            kept[k] = v
    loop["kept"], loop["fixed"] = _text(kept), list(kept)
    return loop


def _take(loop: Dict[str, Any], mode: str, k: str, v: Any, spec: Dict[str, Any]) -> Optional[bool]:
    """A key of a loop's spec into its field (True), kept as written (False), or None: the loop is kept whole (a field
    of another kind than it takes, or of another mode, which the editor would write over)."""
    if k == "settle" and mode != "ramp":
        if _number(v):
            loop["settle"] = v
            return True
        return False                                  # a hold per value: kept
    if mode == "range":
        if k in ("from", "to"):
            loop[k] = v
            return _number(v) or None
        if k == "points":
            loop["points"], loop["by"] = v, "points"
            return _number(v) or None
        if k == "step":
            if "points" in spec:                      # points wins in load_plan: this one is kept as it is
                return False
            loop["step"] = v
            return _number(v) or None
        if k in ("log", "back"):
            loop[k] = v
            return isinstance(v, bool) or None
    elif mode == "values":
        if k == "values":
            loop["values"] = v
            return (isinstance(v, list) and all(map(_plain, v))) or None
        if k == "back":
            loop["back"] = v
            return isinstance(v, bool) or None
    elif mode == "ramp":
        if k == "ramp":
            legs = _legs(v)
            if legs is None:
                return None
            loop["legs"], loop["one"] = legs, not isinstance(v, list)
            return True
        if k == "every":
            loop["every"] = v
            return _number(v) or None
    return None if k in _LOOP_KEYS else False         # stabilize, until, while, read, within, rate, stall: kept


def _legs(ramp: Any) -> Optional[List[Dict[str, Any]]]:
    """A ramp's legs as rows (to, rate, hold); None when one isn't a target alone or {to, rate, hold} of numbers."""
    legs = []
    for leg in ramp if isinstance(ramp, list) else [ramp]:
        if _number(leg):
            legs.append({"to": leg, "rate": None, "hold": None, "bare": True, "order": []})
            continue
        if not isinstance(leg, dict) or not leg or not set(leg) <= set(LEG_ORDER) \
                or not all(map(_number, leg.values())):
            return None
        legs.append({"to": leg.get("to"), "rate": leg.get("rate"), "hold": leg.get("hold"), "bare": False,
                     "order": list(leg)})
    return legs if legs and all(leg["to"] is not None for leg in legs) else None


# ------------------------------------------------------------------ the parts → the plan

def to_plan(parts: Any) -> Dict[str, Any]:
    """The editor's parts as the plan (a mapping, for load_plan); ValueError: they can't be one (a loop with nothing to
    step, a parameter stepped by two loops or set twice, parts not as the editor sends them)."""
    if not isinstance(parts, dict):
        raise ValueError("send the scan's parts")
    try:
        return _to_plan(parts)
    except (TypeError, AttributeError, yaml.YAMLError) as e:
        raise ValueError("the scan's parts are not as the editor sends them (%s)"
                         % (str(e).splitlines() or [type(e).__name__])[0]) from None


def _to_plan(parts: Dict[str, Any]) -> Dict[str, Any]:
    order = [str(k) for k in parts.get("order") or []]
    kept = _mapping(parts.get("kept"))
    fixed = {str(k) for k in kept}
    pieces: Dict[Any, Any] = {}

    def put(key: str, value: Any, written: bool) -> None:
        if key not in fixed and written:
            pieces[key] = value
    for key in ("name", "description"):
        text = parts.get(key)
        if isinstance(text, str):
            plan_key = "plan" if key == "name" else key
            put(plan_key, text, text != "" or plan_key in order)
    if isinstance(parts.get("loops"), list):
        sweep = _sweep(parts["loops"])
        put("sweep", sweep, bool(sweep) or "sweep" in order)
    if isinstance(parts.get("measure"), list):
        measure = [m for m in parts["measure"] if m not in (None, "")]
        put("measure", measure, bool(measure) or "measure" in order)
    for key in ("setup", "after"):
        if isinstance(parts.get(key), list):
            rows = _rows(parts[key], key)
            put(key, rows, bool(rows) or key in order)
    for key in ("repeat", "settle"):
        if parts.get(key) not in (None, ""):
            put(key, parts[key], True)
    time = parts.get("time")
    if isinstance(time, dict):
        given = {k: time[k] for k in TIME_ORDER if time.get(k) not in (None, "")}
        put("time", _ordered(given, time.get("order") or [], TIME_ORDER), bool(given))
    pieces.update(kept)
    return _ordered(pieces, order, ORDER)


def _sweep(loops: List[Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for n, loop in enumerate(loops, 1):
        if not isinstance(loop, dict):
            raise ValueError("loop %d: send its parts" % n)
        key = " ".join(str(loop.get("key") or "").split())
        if not key:
            raise ValueError("loop %d: choose what it steps (instrument.parameter)" % n)
        if key in out:
            raise ValueError("%s is stepped by two loops" % key)
        out[key] = _spec(loop)
    return out


def _spec(loop: Dict[str, Any]) -> Any:
    mode = loop.get("mode")
    if mode not in MODES:
        raise ValueError("%s: a loop is a range, values, a ramp or kept as written" % loop.get("key"))
    if mode == "kept":
        return load_yaml(str(loop.get("text") or ""))
    kept = _mapping(loop.get("kept"))
    order = [str(k) for k in loop.get("order") or []]
    fields: Dict[str, Any] = {}
    if mode == "range":
        fields.update({"from": loop.get("from"), "to": loop.get("to")})
        fields["points" if loop.get("by") == "points" else "step"] = \
            loop.get("points" if loop.get("by") == "points" else "step")
    elif mode == "values":
        fields["values"] = list(loop.get("values") or [])
    else:
        fields.update({"ramp": _ramp(loop.get("legs") or [], bool(loop.get("one"))), "every": loop.get("every")})
    if mode != "ramp":
        fields["settle"] = loop.get("settle")
    fields = {k: v for k, v in fields.items() if v not in (None, "")}
    for flag in ("log", "back") if mode == "range" else ("back",) if mode == "values" else ():
        if loop.get(flag) or flag in order:
            fields[flag] = bool(loop.get(flag))
    if mode == "values" and loop.get("form") == "list" and set(fields) == {"values"} and not kept:
        return fields["values"]   # a bare list, as it was written
    fields.update(kept)
    return _ordered(fields, order, SPEC_ORDER)


def _ramp(legs: List[Any], one: bool) -> Any:
    out = []
    for leg in legs:
        if not isinstance(leg, dict):
            raise ValueError("a ramp's leg is {to, rate, hold}")
        values = {k: leg.get(k) for k in LEG_ORDER if leg.get(k) not in (None, "")}
        if leg.get("bare") and set(values) == {"to"}:
            out.append(values["to"])
        else:
            out.append(_ordered(values, leg.get("order") or [], LEG_ORDER))
    return out[0] if one and len(out) == 1 else out


def _rows(rows: List[Any], where: str) -> Dict[str, Any]:
    """Setup or after rows as their mapping; a row with no key yet is left out."""
    out: Dict[str, Any] = {}
    for row in rows:
        key = " ".join(str((row or {}).get("key") or "").split()) if isinstance(row, dict) else ""
        if not key:
            continue
        if key in out:
            raise ValueError("%s is set twice in %s" % (key, where))
        out[key] = row.get("value")
    return out


def _ordered(pieces: Dict[Any, Any], order: List[Any], canon: Tuple[str, ...]) -> Dict[Any, Any]:
    """pieces in the order they were written (order: their keys, as text), a new one where it goes among them by
    canon (before the first that canon puts after it)."""
    names = {str(k): k for k in pieces}
    keys: List[Any] = []
    for name in order:
        if str(name) in names and names[str(name)] not in keys:
            keys.append(names[str(name)])
    rank = {k: i for i, k in enumerate(canon)}
    for k in pieces:
        if k in keys:
            continue
        r = rank.get(str(k), len(canon))
        keys.insert(next((i for i, have in enumerate(keys) if rank.get(str(have), len(canon)) > r), len(keys)), k)
    return {k: pieces[k] for k in keys}
