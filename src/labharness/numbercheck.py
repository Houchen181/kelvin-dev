"""Numbers in text Kelvin's AI wrote, checked by code against the records the text cites (phase 6 of the ELN plan,
2026-11-07): a number that none of the cited records holds is flagged, so no draft passes on a number nobody measured.

A number matches a record's value when it rounds to it as written ("1.5 K" matches 1.4999), in the unit's SI prefix too
("50 mV" matches 0.05 V), and in the same unit when the record says its own ("50 mV" never matches 50 V; a record's
value without a unit matches any). Both ends of a range ("1.5-9.9 kOhm") and an uncertainty ("4.2 ± 0.1 K") are
checked. Dates, times, run ids, citations and list numbers are not checked, nor are a record's own bookkeeping
fields (an event's line number matched any invented whole number: audit 0.3.52, D5)."""
from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9}
UNITS = ("V", "A", "K", "T", "Hz", "W", "s", "Ω", "Ohm", "ohm", "F", "H", "m", "eV", "dB", "dBm", "rad", "deg", "Pa",
         "bar", "mbar", "Torr", "S", "G", "Oe")
SAME_UNIT = {"Ω": "Ohm", "ohm": "Ohm", "°": "deg"}
_NUM = r"\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
_UNIT = r"([a-zA-Zµμ°Ω%]*)"
NUMBER = re.compile(r"(?<![\w.:/\-\[])([-+−]?" + _NUM + r")(?:\s*[×x]\s*10\^?([-+−]?\d+))?"
                    r"(?:\s*(±|\+/-)\s*(" + _NUM + r"))?\s*" + _UNIT)
# a range, its unit after its second end: the number after the hyphen was never looked at, and the first was checked
# without its unit
RANGE = re.compile(r"(?<![\w.:/\-\[])([-+−]?" + _NUM + r")\s*(?:-|–|—|\.\.)\s*(" + _NUM + r")\s*" + _UNIT)
# not numbers: citations, run ids, dates, times, list numbers, and ids of hex digits (a run's uuid, a seal's SHA-256),
# which read as numbers otherwise: a uuid's 2123e834-… as 2123e834, or its 3abcdef0-… as 3
SKIP = re.compile(r"\[[^\]]*\]|\b\d{8}-\d{6}[\w.-]*|\b\d{4}-\d{2}-\d{2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?)?|"
                  r"\b\d{1,2}:\d{2}(?::\d{2})?\b|^\s*\d+[.)]\s|"
                  r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b|\b(?=\d*[a-fA-F])[0-9a-fA-F]{16,}\b", re.M)
# a record's own bookkeeping, never a measurement: an event's line numbers, times, links and whose it is
BOOKKEEPING = ("seq", "own_seq", "prev", "t", "wall", "user", "whose", "ai", "sha256", "uuid")

Found = Tuple[str, float, float, float, Optional[str]]   # as written, value, its SI scale, its rounding, its unit


def _float(text: str) -> float:
    return float(text.replace("−", "-"))


def unit_of(unit: str) -> Tuple[bool, float, Optional[str]]:
    """(known, SI scale, the unit without its prefix) of a unit as written: "mV" is (True, 1e-3, "V")."""
    if unit in UNITS or unit in ("%", "°", "°C"):
        return True, 1.0, SAME_UNIT.get(unit, unit)
    if len(unit) > 1 and unit[0] in PREFIX and unit[1:] in UNITS:
        return True, PREFIX[unit[0]], SAME_UNIT.get(unit[1:], unit[1:])
    return False, 1.0, None


def _step(raw: str) -> float:
    """Half the last digit written: what rounds to it."""
    low = raw.lower()
    if "e" in low:
        mant, power = low.split("e")
        decimals = len(mant.split(".")[1]) if "." in mant else 0
        return 0.5 * 10.0 ** (int(power) - decimals)
    return 0.5 * 10.0 ** (-(len(raw.split(".")[1]) if "." in raw else 0))


def numbers_in(text: str, counts: bool = True) -> List[Found]:
    """(as written, value, its SI scale, its rounding, its unit without prefix or None) of each number worth checking
    in text (counts: leave out small whole numbers without a unit, which count things): both ends of a range, and an
    uncertainty after ±, each in the unit written after them."""
    out: List[Found] = []
    clean = SKIP.sub(lambda m: " " * len(m.group(0)), str(text or ""))

    def add(written: str, raw: str, exp: Optional[str], unit: str) -> None:
        try:
            v, step = _float(raw), _step(raw)
            if exp:
                v *= 10.0 ** int(exp.replace("−", "-"))
                step *= 10.0 ** int(exp.replace("−", "-"))
        except (ValueError, OverflowError):   # not a number, or one no float holds (1e999)
            return
        if not (math.isfinite(v) and math.isfinite(step)):
            return
        known, scale, base = unit_of(unit)
        is_int = "." not in raw and not exp and "e" not in raw.lower()
        if is_int and not known and abs(v) <= 12 and counts:   # a count, a step's number: not a measurement
            return
        out.append((written, v, scale, step, base))

    def ranges(m: "re.Match[str]") -> str:
        a, b, unit = m.group(1), m.group(2), m.group(3) or ""
        add(("%s %s" % (a, unit)).strip(), a, None, unit)
        add(("%s %s" % (b, unit)).strip(), b, None, unit)
        return " " * len(m.group(0))
    clean = RANGE.sub(ranges, clean)
    for m in NUMBER.finditer(clean):
        raw, exp, pm, err, unit = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5) or ""
        if pm and err:
            add(("%s %s" % (m.group(0)[:m.start(3) - m.start(0)].strip(), unit)).strip(), raw, exp, unit)
            add(("± %s %s" % (err, unit)).strip(), err, exp, unit)
        else:
            add(m.group(0).strip(), raw, exp, unit)
    return out


def without_bookkeeping(record: Any) -> Any:
    """A record (an event) without its own bookkeeping fields (BOOKKEEPING)."""
    return {k: v for k, v in record.items() if k not in BOOKKEEPING} if isinstance(record, dict) else record


def values_in(record: Any) -> List[Tuple[float, Optional[str]]]:
    """Every number a record holds, with its unit when the record says it (a dict's "unit" for the numbers beside
    it, a unit written after a number in text; None: not said): in its fields, and written in its text (a plan's, a
    step's). A number in a prefixed unit is held in SI ({"value": 50, "unit": "mV"} holds 0.05 V)."""
    out: List[Tuple[float, Optional[str]]] = []

    def walk(v: Any, unit: Tuple[bool, float, Optional[str]] = (False, 1.0, None)) -> None:
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            if math.isfinite(float(v)):
                out.append((float(v) * unit[1], unit[2]))
        elif isinstance(v, str):
            for _, x, scale, _, base in numbers_in(v, counts=False):
                out.append((x * scale, base))
        elif isinstance(v, dict):
            said = v.get("unit")
            here = unit_of(said) if isinstance(said, str) and said else (False, 1.0, None)
            for x in v.values():
                walk(x, here if here[0] else (False, 1.0, None))
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
    walk(record)
    return out


def check(text: str, records: Iterable[Any]) -> Dict[str, Any]:
    """{"checked": how many numbers, "found": [as written], "missing": [as written: no cited record holds it]}."""
    units: Dict[float, Set[Optional[str]]] = {}
    for x, base in values_in(list(records)):
        units.setdefault(x, set()).add(base)
    values = sorted(units)
    found: List[str] = []
    missing: List[str] = []
    for written, v, scale, step, base in numbers_in(text):
        target = v * scale
        tol = abs(step * scale) + 1e-12 * max(1.0, abs(target))

        def fits(x: float) -> bool:   # a value the record holds in no unit, or in this one
            return base is None or None in units[x] or base in units[x]
        ok = any(abs(x - target) <= tol and fits(x) for x in _near(values, target, tol)) or \
            any(abs(abs(x) - abs(target)) <= tol and fits(x) for x in _near(values, -target, tol))   # a sign in words
        (found if ok else missing).append(written)
    return {"checked": len(found) + len(missing), "found": found, "missing": missing}


def _near(values: List[float], target: float, tol: float) -> List[float]:
    import bisect
    lo = bisect.bisect_left(values, target - tol)
    hi = bisect.bisect_right(values, target + tol)
    return values[lo:hi]


def said(result: Dict[str, Any]) -> str:
    if not result["checked"]:
        return "no numbers to check"
    if not result["missing"]:
        return "every number (%d) is in the records it cites" % result["checked"]
    return "%d of %d numbers are in the records it cites; not found: %s" % (
        len(result["found"]), result["checked"], ", ".join(result["missing"][:12]))


def as_json(result: Dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False)
