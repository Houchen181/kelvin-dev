"""The Analysis panel's work: what a physicist does to data after measuring them, on kept runs.

Asked for 2026-10-29: a panel of its own where post-processing is done. A run's column (or an expression of its
columns, expr.py) against another, through the usual steps, then a model fitted to the result. Nothing changes a run
until a person keeps the fit, into the run's run.h5 as fit_run keeps Kelvin's (with the steps that made its data);
Kelvin's analyze tool works out the same spec and opens it in the panel, where the person sees it and changes it.

A spec, as the panel sends it and the analyze tool writes it:
    runs     run ids (MAX_RUNS at most), drawn together, every one through the same steps
    x, y     a column or an expression of the run's columns; y a trace column takes one point's trace (row: which
             point, default the last that has one) against the trace's own x
    where    a condition on the points (smu.V > 0)
    group    a column (or several, comma-separated): one curve per value of it. Left out, a map is a curve per value
             of its outer axes (those x isn't); "" is one curve
    steps    in order, each {op, ...}:
        crop {lo, hi}              keep x in [lo, hi]
        bin {width}                the mean in each x bin of this width
        smooth {points}            Savitzky-Golay, quadratic, over this many points (odd)
        deriv                      dy/dx
        integrate                  the running integral, by trapezoids from the first point
        background {order, lo, hi} subtract a polynomial of this order fitted to x in [lo, hi] (all points if neither)
        normalize {how}            max: |y| at most 1; range: 0 to 1; first: the first point is 1; mean: the mean is 1
        scale {factor, offset, unit} y * factor + offset, in unit (default: y's, re-prefixed by a power of ten)
        symmetrize, antisymmetrize (y(x) + y(-x)) / 2 and (y(x) - y(-x)) / 2 about x = 0, on x >= 0 only (each |x|
                                   once, so a fit counts every point once): magnetoresistance, Hall
        fft                        the amplitude spectrum, x resampled evenly first
        abs, log10                 |y|, log10 |y|
    fit      {model, lo, hi, start, fix}: one of fitting.MODELS or an expression in x (a*exp(-x/tau) + c), fitted to
             each curve in x in [lo, hi]

Units follow the columns through expressions and steps (units.py); where one can't be told, the axis has none and a
note says so."""
from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import csv
import functools
import io
import math
import re
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import datatools, expr, fitting, rundata, units

MAX_RUNS = 8
MAX_CURVES = 24       # curves drawn at once (runs times groups)
MAX_TABLE = 1000      # curves in one CSV: every one of them, never fewer
MAX_STEPS = 16
MAX_BINS = 100000
SHOWN = 4000          # points of a curve sent to the panel; every point is used for steps and fits
FIT_S = 20.0          # one curve's fit, at most
FITS_S = 45.0         # all the curves' fits of one spec, at most
OPS = ("crop", "bin", "smooth", "deriv", "integrate", "background", "normalize", "scale", "symmetrize",
       "antisymmetrize", "fft", "abs", "log10")


class AnalysisError(ValueError):
    """What is wrong with a spec, in words."""


class KeptAlready(AnalysisError):
    """A fit of that name is kept with the run already (keep replaces it only when asked to)."""

    def __init__(self, name: str):
        super().__init__("a fit named %r is kept with this run already" % name)
        self.name = name


_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="analysis")


async def off_thread(fn: Callable[..., Any], *args: Any) -> Any:
    """Analysis on threads of its own, so a long fit never holds up Stop, approvals or the lab's state, which share
    asyncio's default threads."""
    return await asyncio.get_running_loop().run_in_executor(_POOL, functools.partial(fn, *args))


def _np() -> Any:
    return datatools._np()


def _num(v: Any, what: str) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise AnalysisError("%s must be a number" % what)
    if not math.isfinite(f):
        raise AnalysisError("%s must be a finite number" % what)
    return f


def _finite(np: Any, x: Any, y: Any) -> Tuple[Any, Any]:
    keep = np.isfinite(x) & np.isfinite(y)
    return x[keep], y[keep]


def _window(np: Any, x: Any, lo: Optional[float], hi: Optional[float]) -> Any:
    keep = np.ones(x.size, dtype=bool)
    if lo is not None:
        keep &= x >= lo
    if hi is not None:
        keep &= x <= hi
    return keep


def _sorted_unique(np: Any, x: Any, y: Any) -> Tuple[Any, Any]:
    """x ascending, the points at one x averaged."""
    ux, inv = np.unique(x, return_inverse=True)
    inv = inv.reshape(-1)
    return ux, np.bincount(inv, weights=y) / np.bincount(inv)


def _mirrored(np: Any, x: Any, y: Any, odd: bool) -> Tuple[Any, Any]:
    """The even (or odd) part of y about x = 0, once for each |x|: on the side of 0 with fewer points (x >= 0 when
    both have as many), each taken with y at -x interpolated from the other side. Every point is used about once, so
    a fit's errors are not halved by counting each point at x and at -x."""
    ux, uy = _sorted_unique(np, x, y)
    lo, hi = max(float(ux.min()), -float(ux.max())), min(float(ux.max()), -float(ux.min()))
    inside = (ux >= lo) & (ux <= hi)
    pos, neg = inside & (ux > 0), inside & (ux < 0)
    if lo >= hi or not pos.any() or not neg.any():
        raise AnalysisError("to %s, x must run on both sides of 0" % ("antisymmetrize" if odd else "symmetrize"))
    side = neg if neg.sum() < pos.sum() else pos
    at = np.sort(np.abs(ux[side]))
    if not odd and (inside & (ux == 0)).any():   # at x = 0 the odd part is 0 by its definition: no point
        at = np.concatenate([[0.0], at])
    up, down = np.interp(at, ux, uy), np.interp(-at, ux, uy)
    return at, (up - down) / 2 if odd else (up + down) / 2


def _smooth(np: Any, y: Any, points: int) -> Any:
    h = max(1, int(points) // 2)
    if y.size < 2 * h + 1:
        raise AnalysisError("smoothing over %d points needs at least that many points" % (2 * h + 1))
    a = np.vander(np.arange(-h, h + 1, dtype=float), 3, increasing=True)
    c = np.linalg.pinv(a)[0]
    padded = np.concatenate([np.full(h, y[0]), y, np.full(h, y[-1])])
    return np.convolve(padded, c[::-1], mode="valid")


def step(np: Any, x: Any, y: Any, xu: Optional[str], yu: Optional[str], xn: str, yn: str, s: Dict[str, Any]
         ) -> Tuple[Any, Any, Optional[str], Optional[str], str, str]:
    """One step on a curve: (x, y, x unit, y unit, x name, y name) after it. A unit is None when it can't be told."""
    op = str(s.get("op") or "").strip().lower()
    if op == "crop":
        keep = _window(np, x, _num(s.get("lo"), "crop's low end"), _num(s.get("hi"), "crop's high end"))
        x, y = x[keep], y[keep]
    elif op == "bin":
        width = _num(s.get("width"), "the bin width")
        if not width or width <= 0:
            raise AnalysisError("a bin width above 0, in %s" % (xu or "x's unit"))
        if (float(x.max()) - float(x.min())) / width > MAX_BINS:
            raise AnalysisError("bins of %g make more than %d bins: take wider ones" % (width, MAX_BINS))
        idx = np.floor((x - x.min()) / width).astype(int)
        counts = np.bincount(idx)
        have = counts > 0
        x = np.bincount(idx, weights=x)[have] / counts[have]
        y = np.bincount(idx, weights=y)[have] / counts[have]
    elif op == "smooth":
        points = int(_num(s.get("points"), "the points to smooth over") or 5)
        if not 3 <= points <= 1001:
            raise AnalysisError("smooth over 3 to 1001 points")
        y = _smooth(np, y, points)
    elif op == "deriv":
        if x.size < 3:
            raise AnalysisError("a derivative needs 3 points or more")
        dx = np.gradient(x)
        with np.errstate(divide="ignore", invalid="ignore"):
            y = np.where(dx != 0, np.gradient(y) / dx, np.nan)
        yu, yn = units.per(yu, xu), "d%s/d%s" % (yn, xn)
    elif op == "integrate":
        if x.size < 2:
            raise AnalysisError("an integral needs 2 points or more")
        y = np.concatenate([[0.0], np.cumsum((y[1:] + y[:-1]) / 2 * np.diff(x))])
        yu, yn = units.times(yu, xu), "∫%s d%s" % (yn, xn)
    elif op == "background":
        order = int(_num(s.get("order"), "the background's order") or 0)
        if not 0 <= order <= 5:
            raise AnalysisError("a background of order 0 to 5")
        sel = _window(np, x, _num(s.get("lo"), "the background's low end"),
                      _num(s.get("hi"), "the background's high end"))
        if sel.sum() <= order:
            raise AnalysisError("the background window has %d points; order %d needs more" % (int(sel.sum()), order))
        mid, span = float(x[sel].mean()), float(x[sel].std()) or 1.0
        coeff = np.polyfit((x[sel] - mid) / span, y[sel], order)
        y = y - np.polyval(coeff, (x - mid) / span)
        yn = "%s − background" % yn
    elif op == "normalize":
        how = str(s.get("how") or "max")
        if how == "max":
            d = float(np.max(np.abs(y)))
            y = y / d if d else y
        elif how == "range":
            lo, hi = float(y.min()), float(y.max())
            y = (y - lo) / (hi - lo) if hi > lo else y * 0
        elif how == "first":
            if not y[0]:
                raise AnalysisError("the first point is 0: normalize another way")
            y = y / y[0]
        elif how == "mean":
            m = float(y.mean())
            if not m:
                raise AnalysisError("the mean is 0: normalize another way")
            y = y / m
        else:
            raise AnalysisError("normalize by max, range, first or mean")
        yu, yn = "", "%s (normalized)" % yn
    elif op == "scale":
        factor = _num(s.get("factor"), "the factor")
        offset = _num(s.get("offset"), "the offset") or 0.0
        unit = str(s.get("unit") or "").strip()
        if len(unit) > 40:
            raise AnalysisError("a unit of 40 characters at most")
        if factor is not None and factor != 1:
            y, yu = y * factor, units.scaled(yu, factor)
        y = y + offset
        yu = unit or yu
    elif op in ("symmetrize", "antisymmetrize"):
        x, y = _mirrored(np, x, y, op == "antisymmetrize")
        yn = "%s (%s)" % (yn, "symmetric part" if op == "symmetrize" else "antisymmetric part")
    elif op == "fft":
        ux, uy = _sorted_unique(np, x, y)
        if ux.size < 8:
            raise AnalysisError("a spectrum needs 8 points or more")
        grid = np.linspace(float(ux.min()), float(ux.max()), ux.size)
        even = np.interp(grid, ux, uy)
        amp = 2 * np.abs(np.fft.rfft(even - even.mean())) / even.size
        freq = np.fft.rfftfreq(even.size, d=float(grid[1] - grid[0]))
        x, y = freq[1:], amp[1:]
        xu = units.inverse(xu)
        base = (units.split(xu or "") or (0, ""))[1]
        xn, yn = {"Hz": "frequency", "s": "time"}.get(base, "1/%s" % xn), "|FFT %s|" % yn
    elif op == "abs":
        y, yn = np.abs(y), "|%s|" % yn
    elif op == "log10":
        with np.errstate(divide="ignore"):
            y = np.log10(np.abs(y))
        yu, yn = "", "log10 |%s|" % yn
    else:
        raise AnalysisError("no step %r (steps: %s)" % (op, ", ".join(OPS)))
    x, y = _finite(np, np.asarray(x, dtype=float), np.asarray(y, dtype=float))
    return x, y, xu, yu, xn, yn


def _shown(np: Any, x: Any, y: Any) -> Tuple[List[float], List[float]]:
    """A curve for the panel: every point, or for a long one each stretch's lowest and highest (a narrow peak stays)."""
    if x.size <= SHOWN:
        return x.tolist(), y.tolist()
    edges = np.linspace(0, x.size, SHOWN // 2 + 1).astype(int)
    keep: List[int] = []
    for a, b in zip(edges[:-1], edges[1:]):
        if b > a:
            seg = y[a:b]
            keep += sorted({a + int(np.argmin(seg)), a + int(np.argmax(seg))})
    return x[keep].tolist(), y[keep].tolist()


def run_ids(spec: Any) -> List[str]:
    """The runs a spec reads, as every part of Kelvin reads them (the hub relay's check too): a list of run ids, the
    last run when it names none."""
    runs = spec.get("runs") if isinstance(spec, dict) else None
    if runs is None:
        runs = []
    if not isinstance(runs, list) or not all(isinstance(r, str) for r in runs):
        raise AnalysisError("runs: a list of run ids")
    return [r.strip() for r in runs if r.strip()][:MAX_RUNS] or ["last"]


def _unit_of(src: datatools.Source) -> Callable[[str], str]:
    """A column's unit, for units.of (KeyError for a name that is no column of the run)."""
    def unit_of(key: str) -> str:
        if key not in src.table.numbers:
            raise KeyError(key)
        return src.units.get(key, "")
    return unit_of


def _values(src: datatools.Source, text: str) -> Tuple[Any, Optional[str]]:
    """A column's or an expression's values at every point, and its unit (None when it can't be told)."""
    try:
        v, unit = src.values(text)
    except datatools.ToolError as e:
        raise AnalysisError(str(e))
    if str(text).strip() in src.table.numbers:
        return v, unit
    return v, units.of(text, _unit_of(src))


def _names(text: str) -> List[str]:
    """The names an expression uses."""
    try:
        tree = ast.parse(str(text or "").strip(), mode="eval")
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Name, ast.Attribute)):
            try:
                out.append(expr._dotted(node))
            except expr.ExprError:
                pass
    return out


def default_group(src: datatools.Source, x: str) -> str:
    """What a run is drawn per when the spec leaves group out: a map a curve per value of its outer axes (the swept
    axes x doesn't use; all but the innermost when it uses none), anything else one curve ("")."""
    used = set(_names(x)) & set(src.axes)
    return ", ".join([a for a in src.axes if a not in used] if used else src.axes[:-1])


def _split(group: str) -> List[str]:
    """A group's columns: several are comma-separated (a comma inside brackets is an expression's)."""
    parts, depth, part = [], 0, ""
    for ch in group:
        depth += 1 if ch in "([" else -1 if ch in ")]" else 0
        if ch == "," and depth == 0:
            parts.append(part)
            part = ""
        else:
            part += ch
    return [p.strip() for p in parts + [part] if p.strip()]


def _groups(src: datatools.Source, group: str, mask: Any, most: int) -> Tuple[List[Tuple[str, Any]], int]:
    """One curve per value of the group's column (per set of values, for several), in the order they were measured:
    (its label, its points' indices) for the first `most`, and how many there are."""
    from .cards import si
    np = _np()
    cols = [(k,) + _values(src, k) for k in _split(group)]
    ok = mask.copy()
    for _, v, _ in cols:
        ok &= np.isfinite(v)
    at = np.flatnonzero(ok)
    if not at.size:
        return [], 0
    values = np.column_stack([v[at] for _, v, _ in cols]) + 0.0   # -0.0 is 0
    _, first, inverse = np.unique(values, axis=0, return_index=True, return_inverse=True)
    inverse = inverse.reshape(-1)
    counts = np.bincount(inverse, minlength=first.size)
    ends = np.cumsum(counts)
    members = np.argsort(inverse, kind="stable")   # each group's points together, in the order measured
    out = []
    for g in np.argsort(first, kind="stable")[:most]:
        idx = at[members[ends[g] - counts[g]:ends[g]]]
        out.append((", ".join("%s = %s" % (k, si(float(v[idx[0]]), unit or "")) for k, v, unit in cols), idx))
    return out, int(first.size)


def _curves(rt: Any, spec: Dict[str, Any], notes: List[str], most: int = MAX_CURVES
            ) -> Tuple[List[Dict[str, Any]], int]:
    """Each run's curves before the steps, the first `most` of them: {run, label, group (what sets the curve apart:
    magnet.B = 1 T, row 3), by (the group asked for, or the run's own), row (a trace's point), x, y, x and y units
    (None: not known) and names}; and how many there are."""
    np = _np()
    out: List[Dict[str, Any]] = []
    total = 0
    for run in run_ids(spec):
        try:
            src = datatools.Source(rt, run)
        except datatools.ToolError as e:
            raise AnalysisError(str(e))
        y_text = str(spec.get("y") or "").strip()
        if y_text and y_text in src.traces:
            row = spec.get("row")
            try:
                xv, yv, xu, yu, r = src.trace(y_text, None if row in (None, "") else int(row))
            except (datatools.ToolError, ValueError) as e:
                raise AnalysisError(str(e))
            if str(spec.get("group") or "").strip():
                notes.append("%s is one point's trace: row picks the point, group doesn't apply" % y_text)
            total += 1
            if len(out) < most:
                out.append({"run": src.run_id, "label": "%s · row %d" % (src.run_id, r), "group": "row %d" % r,
                            "by": "", "row": r, "x": xv, "y": yv, "xu": xu, "yu": yu,
                            "xn": "frequency" if xu == "Hz" else "x", "yn": y_text})
            continue
        xn = str(spec.get("x") or "").strip() or (src.axes[-1] if src.axes else "t")
        yn = y_text or (src.scalars[0] if src.scalars else "")
        if not yn:
            raise AnalysisError("%s measured nothing to plot: say y" % src.run_id)
        xv, xu = _values(src, xn)
        yv, yu = _values(src, yn)
        try:
            mask = src.mask(str(spec.get("where") or "").strip())
        except datatools.ToolError as e:
            raise AnalysisError(str(e))
        group = default_group(src, xn) if spec.get("group") is None else str(spec["group"]).strip()
        if group:
            parts, count = _groups(src, group, mask, max(0, most - len(out)))
        else:
            parts, count = [("", np.flatnonzero(mask))][:max(0, most - len(out))], 1
        total += count
        for label, idx in parts:
            out.append({"run": src.run_id, "label": src.run_id + (" · " + label if label else ""), "group": label,
                        "by": group, "x": xv[idx], "y": yv[idx], "xu": xu, "yu": yu, "xn": xn, "yn": yn})
    if total > len(out):
        notes.append("%d curves; the first %d are drawn" % (total, len(out)))
    return out, total


def _worked(np: Any, c: Dict[str, Any], steps: Sequence[Any]
            ) -> Tuple[Any, Any, Optional[str], Optional[str], str, str]:
    """A curve through the steps: (x, y, x unit, y unit, x name, y name)."""
    x, y = _finite(np, np.asarray(c["x"], dtype=float), np.asarray(c["y"], dtype=float))
    if not x.size:
        raise AnalysisError("%s has no points to work on" % c["label"])
    xu, yu, xn, yn = c["xu"], c["yu"], c["xn"], c["yn"]
    for s in steps:
        if not isinstance(s, dict):
            raise AnalysisError("each step is {op, ...}")
        x, y, xu, yu, xn, yn = step(np, x, y, xu, yu, xn, yn, s)
        if not x.size:
            raise AnalysisError("no points are left after %s" % s.get("op"))
    return x, y, xu, yu, xn, yn


def _fitted(np: Any, x: Any, y: Any, xu: Optional[str], yu: Optional[str], f: Dict[str, Any],
            deadline: Optional[float] = None) -> Tuple[Any, Dict[str, Any], List[str], bool]:
    """The fit asked for on these points, in its window: (the model, the result, notes, whether a resonance in dB
    was fitted on linear power, as fit_run fits it)."""
    model = str(f.get("model") or "").strip()
    for k in ("start", "fix"):
        if f.get(k) is not None and not isinstance(f.get(k), dict):
            raise AnalysisError("%s: {name: value}" % k)
    key = fitting.ALIASES.get(model.lower(), model.lower())
    keep = _window(np, x, _num(f.get("lo"), "the fit's low end"), _num(f.get("hi"), "the fit's high end"))
    x, y = x[keep], y[keep]
    notes = []
    db = False
    if key == "resonator":
        raise AnalysisError("a resonator's fit takes its phase too: ask Kelvin (fit_run)")
    if key in fitting.MODELS:
        m = fitting.MODELS[key]()
        if yu in datatools.DB_UNITS and key in ("lorentzian", "gaussian", "double_lorentzian"):
            y, yu, db = 10 ** (y / 10), "", True
            notes.append("fitted on linear power, 10^(dB/10): its widths are linewidths")
    else:
        try:
            m = fitting.expression_model(model, [], "x")
        except (fitting.FitError, ValueError, TypeError) as e:
            raise AnalysisError(str(e))
    try:
        result = fitting.fit(m, x, y, None, f.get("start") or None, f.get("fix") or None, None, None, xu or "",
                             yu or "", deadline=min(time.monotonic() + FIT_S, deadline or math.inf))
    except (fitting.FitError, ValueError, TypeError, ArithmeticError) as e:   # ExprError is a ValueError
        raise AnalysisError(str(e) or e.__class__.__name__)
    if key not in fitting.MODELS:   # the person's own model: its parameters' units, where its form fixes them
        found = units.model_units(m.formula, m.params, xu, yu)
        for p in result["params"]:
            p["unit"] = p["unit"] or found.get(p["name"], "")
    return m, result, notes, db


def _fit(np: Any, x: Any, y: Any, xu: Optional[str], yu: Optional[str], what: str, f: Dict[str, Any],
         deadline: Optional[float] = None) -> Dict[str, Any]:
    if deadline is not None and time.monotonic() > deadline:
        return {"error": "not fitted: the fits took %d s; fit fewer curves at once" % FITS_S}
    try:
        m, result, notes, db = _fitted(np, x, y, xu, yu, f, deadline)
    except AnalysisError as e:
        return {"error": str(e)}
    cx, cy = fitting.curve(m, result)
    if db:   # drawn over the data, in dB again
        with np.errstate(divide="ignore", invalid="ignore"):
            cy = (10 * np.log10(np.asarray(cy, dtype=float))).tolist()
    return {"model": result["model"], "formula": result["formula"], "n": result["n"],
            "chi2_red": result["chi2_red"], "r2": result["r2"], "converged": result["converged"],
            "params": [{k: p[k] for k in ("name", "value", "error", "unit", "held")} for p in result["params"]],
            "derived": result.get("derived") or [], "warnings": list(result.get("warnings") or []) + notes,
            "curve": {"x": list(cx), "y": [float(v) for v in cy]},
            "text": "\n".join(fitting.fit_text(result, what) + ["note: " + n for n in notes])}


def _safe(v: Any) -> Any:
    """JSON the panel can read: no NaN or infinity (None instead), NumPy's numbers as Python's."""
    if isinstance(v, dict):
        return {k: _safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_safe(x) for x in v]
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int) and not hasattr(v, "dtype"):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f if math.isfinite(f) else None


def _steps(spec: Any) -> List[Any]:
    if not isinstance(spec, dict):
        raise AnalysisError("the spec must be an object")
    steps = spec.get("steps") or []
    if not isinstance(steps, list) or len(steps) > MAX_STEPS:
        raise AnalysisError("steps: a list of at most %d" % MAX_STEPS)
    return steps


def _no_unit(axis: str, name: str) -> str:
    return "the unit of %s (%s) can't be told%s" % (axis, name, ": a scale step's unit gives it" if axis == "y" else "")


def compute(rt: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
    """The spec worked out: each curve after the steps (and its fit), with the axes' names and units, and the group
    the curves are drawn per."""
    np = _np()
    steps = _steps(spec)
    f = spec.get("fit") or {}
    fit_on = isinstance(f, dict) and bool(str(f.get("model") or "").strip())
    notes: List[str] = []
    curves = []
    axes: Dict[str, str] = {}
    unknown: List[str] = []
    deadline = time.monotonic() + FITS_S
    found, _ = _curves(rt, spec, notes)
    for c in found:
        x, y, xu, yu, xn, yn = _worked(np, c, steps)
        axes = {"x": xn, "x_unit": xu or "", "y": yn, "y_unit": yu or ""}
        for axis, u, n in (("x", xu, xn), ("y", yu, yn)):
            if u is None and _no_unit(axis, n) not in unknown:
                unknown.append(_no_unit(axis, n))
        sx, sy = _shown(np, x, y)
        out = {"run": c["run"], "label": c["label"], "n": int(x.size), "x": sx, "y": sy}
        if fit_on:
            out["fit"] = _fit(np, x, y, xu, yu, "%s vs %s" % (yn, xn), f, deadline)
        curves.append(out)
    return _safe({"curves": curves, "axes": axes, "notes": notes + unknown,
                  "group": next((c["by"] for c in found if c["by"]), "")})


def table(rt: Any, spec: Dict[str, Any]) -> Tuple[str, str]:
    """The worked-out curves as CSV, every point of every curve, for the panel's download: (a file name, the text).
    Two columns a curve, its x and its y, named with their units."""
    np = _np()
    steps = _steps(spec)
    found, total = _curves(rt, spec, [], MAX_TABLE)
    if total > len(found):   # never a file that leaves curves out without saying so
        raise AnalysisError("%d curves: a CSV takes %d at most; take fewer with where or group" % (total, MAX_TABLE))
    head: List[str] = []
    cols: List[List[float]] = []
    for c in found:
        x, y, xu, yu, xn, yn = _worked(np, c, steps)
        tag = " (%s)" % c["label"] if len(found) > 1 else ""
        head += [xn + (" [%s]" % xu if xu else "") + tag, yn + (" [%s]" % yu if yu else "") + tag]
        cols += [x.tolist(), y.tolist()]
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(head)
    for i in range(max((len(c) for c in cols), default=0)):
        w.writerow([repr(c[i]) if i < len(c) else "" for c in cols])
    name = re.sub(r"[^\w.-]+", "_", found[0]["run"] if found else "analysis") + "-analysis.csv"
    return name, out.getvalue()


def columns(rt: Any, run_id: str) -> Dict[str, Any]:
    """What a run offers the panel: its swept axes, its readings and traces, each with its unit; its other columns of
    numbers (i: each point's number, t: its time, a repeated sweep's repeat); its kept fits."""
    try:
        src = datatools.Source(rt, run_id)
    except datatools.ToolError as e:
        raise AnalysisError(str(e))
    meta: Dict[str, Any] = {}
    try:
        meta = rundata.meta(src.folder)
    except (KeyError, OSError, ValueError):
        pass
    listed = set(src.axes) | set(src.scalars) | set(src.traces)
    return _safe({"run": src.run_id, "plan": meta.get("plan") or src.run_id.split("-", 2)[-1],
                  "status": meta.get("status"), "points": src.table.n,
                  "axes": [{"key": k, "unit": src.units.get(k, "")} for k in src.axes],
                  "readings": [{"key": k, "unit": src.units.get(k, "")} for k in src.scalars],
                  "traces": [{"key": k, "unit": src.units.get(k, ""), "x_unit": src.x_units.get(k, "")}
                             for k in src.traces],
                  "others": [{"key": k, "unit": src.units.get(k, "")} for k in src.table.order
                             if k in src.table.numbers and k not in listed],
                  "kept": [k.get("name") for k in fitting.stored(src.folder)],
                  "models": [name for name, make in fitting.MODELS.items()
                             if name != "resonator" and not getattr(make(), "needs", ())]})


def keep(rt: Any, spec: Dict[str, Any], curve: int = 0, name: str = "", by: str = "human (portal)",
         account: Optional[str] = None, replace: bool = False) -> str:
    """Keep one curve's fit in its run's run.h5 (/fits/<name>), with the steps that made its data; the lab's record
    says who kept it, and the run's scan report shows it. Its name says its curve (a map's "magnet.B = 1 T"), and a fit
    already kept under that name is replaced only when asked (KeptAlready otherwise)."""
    np = _np()
    found, _ = _curves(rt, spec, [])
    try:
        c = found[int(curve)]
    except (IndexError, TypeError, ValueError):
        raise AnalysisError("no curve %s" % curve)
    f = spec.get("fit") or {}
    if not isinstance(f, dict) or not str(f.get("model") or "").strip():
        raise AnalysisError("fit a model first")
    x, y, xu, yu, xn, yn = _worked(np, c, spec.get("steps") or [])
    _, result, _, _ = _fitted(np, x, y, xu, yu, f, time.monotonic() + FITS_S)
    folder = rt._run_dir(c["run"])
    if not (folder / rundata.RUN_FILE).exists() or not rundata.have_h5():
        raise AnalysisError("this run has no run.h5 yet")
    if rundata.being_recorded(folder):
        raise AnalysisError("the run is still being measured; keep the fit once it has finished")
    lo, hi = f.get("lo"), f.get("hi")
    group = c["group"]
    tail = " @ " + fitting.fit_name(group)[-40:] if group else ""   # its value at the end: never the part cut
    head = fitting.fit_name("%s %s" % (yn, result["model"] if result["model"] != "expression" else "model"))
    name = fitting.fit_name(name) or (head[:80 - len(tail)] + tail) or result["model"]   # as run.h5 will name it
    if not replace and name in [k.get("name") for k in fitting.stored(folder)]:
        raise KeptAlready(name)
    try:
        kept = fitting.store(folder, name, result, {
            "model": str(f.get("model")).strip(), "x": spec.get("x"), "y": spec.get("y"), "where": spec.get("where"),
            "group": c["by"], "curve": c["label"], "steps": spec.get("steps") or None,
            "x_range": [lo, hi] if lo not in (None, "") or hi not in (None, "") else None,
            "from": "the Analysis panel"}, by=by, account=account)
    except (fitting.FitError, OSError) as e:
        raise AnalysisError(str(e))
    rt.gate.log.write("fit_kept", by=by, account=account, run=c["run"], fit=kept, model=result["model"])
    return "kept in %s as /fits/%s%s" % (folder / rundata.RUN_FILE, kept, datatools._report_again(rt, c["run"]))


def describe(result: Dict[str, Any]) -> str:
    """The worked-out spec in words, for the model (the analyze tool): each curve's points and fit."""
    a = result.get("axes") or {}
    lines = ["%s vs %s%s%s" % (a.get("y", "y") + (" [%s]" % a["y_unit"] if a.get("y_unit") else ""),
                               a.get("x", "x"), " [%s]" % a["x_unit"] if a.get("x_unit") else "",
                               ", a curve per %s" % result["group"] if result.get("group") else "")]
    for c in result.get("curves") or []:
        lines.append("- %s: %d points" % (c["label"], c["n"]))
        f = c.get("fit")
        if f and f.get("error"):
            lines.append("  not fitted: %s" % f["error"])
        elif f:
            lines.append("  " + f["text"].replace("\n", "\n  "))
    lines += ["note: " + n for n in result.get("notes") or []]
    return "\n".join(lines)
