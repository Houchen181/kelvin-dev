"""Numbers as reports and slides show them (the user, 2026-11-15: no need to display so many digits; saving them is
fine). The run keeps every digit, in run.h5 and its fit results; only what a report or slide shows is rounded:

- an uncertainty to one significant figure, two when it starts with 1 (0.000043 -> 0.00004, 0.014 -> 0.014);
- its value to the same place, but to at most FIGURES significant figures; when that leaves the uncertainty below
  the last digit shown, the ± goes (1.500058 ± 0.000043 V -> 1.500 V);
- a value without an uncertainty to BARE significant figures (signal / noise 524.9 -> 525);
- SI prefixes on compound units too: (1.010 ± 0.024)e-12 A/V -> 1.01 ± 0.02 pA/V."""
from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Iterable, Optional, Tuple

from ..cards import si, si_parts

FIGURES = 4     # most significant figures of a value shown with its uncertainty
BARE = 3        # significant figures of a value shown without one
# results a report lists but does not lead with (its key numbers are centres, widths, slopes, a steady level)
SECONDARY = {"background", "signal / noise", "noise (rms)", "intercept", "drift (slope)", "mean"}


def prefixed(x: float, unit: str) -> Tuple[float, str]:
    """(divisor, shown unit) for a value in unit: as cards.si_parts, and for a compound unit such as A/V or V/mm the
    prefix goes on its numerator (1.01e-12 A/V -> pA/V)."""
    top, slash, rest = unit.partition("/")
    _, factor, shown = si_parts(x, top if slash and top else unit)
    if factor == 1e9 and shown.startswith("G") and abs(x) >= 1e12 * 0.99995:   # cards stop at G: 1.00 TΩ, not 1004 GΩ
        factor, shown = 1e12, "T" + shown[1:]
    return factor, shown + (slash + rest if slash and top else "")


def _round(x: float, place: int) -> str:
    """x rounded half up at the decimal place 10**place, as text: never "-0" and never an exponent."""
    d = Decimal(repr(float(x))).quantize(Decimal(1).scaleb(place), rounding=ROUND_HALF_UP)
    if d.is_zero():
        d = d.copy_abs()
    return format(d, "f")


def _place(err: float) -> int:
    """The decimal place of an uncertainty's last digit: one significant figure, two when it starts with 1."""
    mantissa, exp = ("%.1e" % err).split("e")
    if mantissa.startswith("1"):
        return int(exp) - 1
    one, exp1 = ("%.0e" % err).split("e")      # 0.0097 rounds up to 0.01: two figures of that, 0.010
    return int(exp1) - (1 if one == "1" else 0)


def _lead(x: float, figures: int = 0) -> int:
    """The decimal place of x's first significant digit; with figures, of x rounded to that many (0.99996 -> 0)."""
    if figures:
        x = float("%.*e" % (figures - 1, x))
    return int(math.floor(math.log10(abs(x)))) if x else 0


def _bare(v: float) -> str:
    if not v:
        return "0"
    lead = _lead(v, BARE)
    if lead <= -4 or lead >= 6:
        return "%.*e" % (BARE - 1, v)
    return _round(v, min(0, lead - BARE + 1))   # whole numbers stay whole: 1234.5 -> 1235, not 1230


def shown(value: Optional[float], err: Optional[float] = None, unit: str = "",
          scale: Optional[Tuple[float, str]] = None) -> str:
    """A value (and its uncertainty) in unit, as a report shows it; scale (divisor, shown unit, from prefixed()) puts
    several values in one unit, as a line comparing them needs (1.01 then 1.00 pA/V, not 1.01 pA/V then 996 fA/V)."""
    if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "-"
    has_err = isinstance(err, (int, float)) and math.isfinite(err) and err > 0
    factor, u = scale or prefixed(max(abs(value), err) if has_err else value, unit)   # 0.4 ± 4 mV, not 400 ± 4000 µV
    v, e = value / factor, (err / factor if has_err else None)
    tail = " " + u if u else ""
    if factor == 1.0 and v and (_lead(v) <= -4 or _lead(v) >= 6):   # no prefix for this unit: a power of ten
        exp = _lead(v)
        inner = shown(v / 10 ** exp, e / 10 ** exp if e else None, "")
        return ("(%s)e%d" % (inner, exp) if " ± " in inner else "%se%d" % (inner, exp)) + tail
    if e is None:
        text, top = _bare(v), _bare(v)
    else:
        place = _place(e)
        if v and _lead(v, FIGURES) - place + 1 > FIGURES:   # the uncertainty is below the last digit shown
            text = top = _round(v, _lead(v, FIGURES) - FIGURES + 1)
        else:
            top = _round(v, place)
            text = "%s ± %s" % (top, _round(e, place))
    if scale is None and "e" not in top and abs(float(top)) >= 1000:   # rounded up into the next prefix: 996 fA/V
        bigger = prefixed(float(top) * factor, unit)                     # to 1000 fA/V reads 1.00 pA/V
        if bigger[0] != factor:
            return shown(value, err, unit, bigger)
    return text + tail


def result(r: Dict[str, Any]) -> str:
    """A fit result (analysis.py's res(): name, value as text, num, err, unit) as a report shows it; a result kept
    only as text (runs before the numbers were kept) is read back from it."""
    if r.get("num") is not None:
        return shown(r.get("num"), r.get("err"), str(r.get("unit") or ""))
    return prose(str(r.get("value") if r.get("value") is not None else ""))


_NUMBER = r"[-+]?\d+(?:\.\d+)?"
_UNIT = r"(?: ([^\s,;:()]+))?"
_PM = re.compile(r"\((%s) ± (%s)\)e([-+]?\d+)%s|(%s) ± (%s)%s" % (_NUMBER, _NUMBER, _UNIT, _NUMBER, _NUMBER, _UNIT))
_PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
_BASE = ("V", "A", "Ω", "Hz", "T", "K", "s", "W", "F")


def _unprefixed(unit: str) -> Tuple[float, str]:
    """(1e9, "Ω") for "GΩ": the prefix a sentence's number was written with, taken off so the number is shown as a
    table shows it (R = 1000 ± 30 GΩ is 1.00 ± 0.03 TΩ); on a compound unit, its numerator's (pA/V)."""
    top, slash, rest = unit.partition("/")
    if len(top) > 1 and top[0] in _PREFIX and top[1:] in _BASE:
        return _PREFIX[top[0]], top[1:] + slash + rest
    return 1.0, unit


def prose(text: str, results: Iterable[Dict[str, Any]] = ()) -> str:
    """A sentence with its "value ± uncertainty unit" numbers rounded as reports show them; everything else in it,
    settings and counts included, stays as written. results: the analysis's results the sentence was written from;
    its noise, written to four figures ("fluctuations 3.965 nV rms"), is then shown as the results table shows it
    (3.96 nV, rounded from every digit, never from the four written)."""
    def one(m: "re.Match[str]") -> str:   # a word after the numbers that is no unit comes back as it was
        if m.group(1) is not None:
            factor, unit = _unprefixed(m.group(4) or "")
            scale = 10.0 ** int(m.group(3)) * factor
            return shown(float(m.group(1)) * scale, float(m.group(2)) * scale, unit)
        factor, unit = _unprefixed(m.group(7) or "")
        return shown(float(m.group(5)) * factor, float(m.group(6)) * factor, unit)
    out = _PM.sub(one, text or "")
    for r in results:
        value, unit = r.get("num"), str(r.get("unit") or "")
        if r.get("name") == "noise (rms)" and isinstance(value, (int, float)) and math.isfinite(value) and value:
            out = out.replace(si(value, unit) + " rms", shown(value, None, unit) + " rms")
    return out
