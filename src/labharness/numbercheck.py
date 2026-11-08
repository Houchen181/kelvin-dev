"""Numbers in text Kelvin's AI wrote, checked by code against the records the text cites (phase 6 of the ELN plan,
2026-11-07): a number that none of the cited records holds is flagged, so no draft passes on a number nobody measured.

A number matches a record's value when it rounds to it as written ("1.5 K" matches 1.4999), in the unit's SI prefix too
("50 mV" matches 0.05 V). Dates, times, run ids, citations and list numbers are not checked."""
from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Iterable, List, Tuple

PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9}
UNITS = ("V", "A", "K", "T", "Hz", "W", "s", "Ω", "Ohm", "ohm", "F", "H", "m", "eV", "dB", "dBm", "rad", "deg", "Pa",
         "bar", "mbar", "Torr", "S", "G", "Oe")
NUMBER = re.compile(r"(?<![\w.:/\-\[])([-+−]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(?:\s*[×x]\s*10\^?([-+−]?\d+))?"
                    r"(?:\s*(±|\+/-)\s*\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)?\s*([a-zA-Zµμ°Ω%]*)")
# not numbers: citations, run ids, dates, times, list numbers, and ids of hex digits (a run's uuid, a seal's SHA-256),
# which read as numbers otherwise: a uuid's 2123e834-… as 2123e834, or its 3abcdef0-… as 3
SKIP = re.compile(r"\[[^\]]*\]|\b\d{8}-\d{6}[\w.-]*|\b\d{4}-\d{2}-\d{2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?)?|"
                  r"\b\d{1,2}:\d{2}(?::\d{2})?\b|^\s*\d+[.)]\s|"
                  r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b|\b(?=\d*[a-fA-F])[0-9a-fA-F]{16,}\b", re.M)


def _float(text: str) -> float:
    return float(text.replace("−", "-"))


def numbers_in(text: str, counts: bool = True) -> List[Tuple[str, float, float, float]]:
    """(as written, value, its SI scale, its rounding) of each number worth checking in text (counts: leave out
    small whole numbers without a unit, which count things)."""
    out: List[Tuple[str, float, float, float]] = []
    clean = SKIP.sub(lambda m: " " * len(m.group(0)), str(text or ""))
    for m in NUMBER.finditer(clean):
        raw, exp, _, unit = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        try:
            v = _float(raw)
            decimals = len(raw.split(".")[1].split("e")[0].split("E")[0]) if "." in raw else 0
            if "e" in raw.lower():
                mant = raw.lower().split("e")[0]
                decimals = len(mant.split(".")[1]) if "." in mant else 0
                step = 0.5 * 10.0 ** (int(raw.lower().split("e")[1]) - decimals)
            else:
                step = 0.5 * 10.0 ** (-decimals)
            if exp:
                v *= 10.0 ** int(exp.replace("−", "-"))
                step *= 10.0 ** int(exp.replace("−", "-"))
        except (ValueError, OverflowError):   # not a number, or one no float holds (1e999)
            continue
        if not (math.isfinite(v) and math.isfinite(step)):
            continue
        known = unit in UNITS or unit in ("%", "°", "°C") or (len(unit) > 1 and unit[0] in PREFIX
                                                              and unit[1:] in UNITS)
        is_int = "." not in raw and not exp and "e" not in raw.lower()
        if is_int and not known and abs(v) <= 12 and counts:   # a count, a step's number: not a measurement
            continue
        scale = 1.0
        if known and unit not in UNITS and unit[0] in PREFIX:
            scale = PREFIX[unit[0]]
        out.append((m.group(0).strip(), v, scale, step))
    return out


def values_in(record: Any) -> List[float]:
    """Every number a record holds: in its fields, and written in its text (a plan's, a step's)."""
    out: List[float] = []

    def walk(v: Any) -> None:
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            if math.isfinite(float(v)):
                out.append(float(v))
        elif isinstance(v, str):
            for _, x, scale, _ in numbers_in(v, counts=False):
                out.append(x)
                out.append(x * scale)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
    walk(record)
    return out


def check(text: str, records: Iterable[Any]) -> Dict[str, Any]:
    """{"checked": how many numbers, "found": [as written], "missing": [as written: no cited record holds it]}."""
    values = sorted(set(values_in(list(records))))
    found: List[str] = []
    missing: List[str] = []
    for written, v, scale, step in numbers_in(text):
        ok = False
        for target, tol in ((v, step), (v * scale, step * scale)):
            tol = abs(tol) + 1e-12 * max(1.0, abs(target))
            if any(abs(x - target) <= tol for x in _near(values, target, tol)):
                ok = True
                break
            if any(abs(abs(x) - abs(target)) <= tol for x in _near(values, -target, tol)):   # a sign said in words
                ok = True
                break
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
