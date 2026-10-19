"""Automatic analysis of a run: what the data show, how good they are, and what to try next.

Pure Python (no numpy), so it runs wherever the core runs. Curves are fitted with
Levenberg-Marquardt on normalised data; uncertainties come from the covariance
matrix scaled by the residual variance (one standard error).
"""
from __future__ import annotations

import math
from statistics import median
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .cards import si, si_parts

FIT_POINTS = 200

# ---------------------------------------------------------------------------- numbers


def _fmt_pm(value: float, err: Optional[float], unit: str) -> str:
    """1.5023 ± 0.0041 V -> '1.502 ± 0.004 V' (prefix chosen from the value, 2 significant digits of error)."""
    if err is None or not math.isfinite(err) or err <= 0 or not math.isfinite(value):
        return si(value, unit)
    shown, factor, shown_unit = si_parts(value if value != 0 else err, unit)
    if value == 0:
        shown = 0.0
    e = err / factor
    tail = " " + shown_unit if shown_unit else ""
    exp = int(math.floor(math.log10(abs(shown)))) if shown else 0
    if factor == 1.0 and (exp >= 4 or exp <= -4):  # no SI prefix available: scientific notation
        m, me = shown / 10 ** exp, e / 10 ** exp
        digits = max(0, min(9, 1 - int(math.floor(math.log10(me))))) if me > 0 else 3
        return "(%.*f ± %.*f)e%d%s" % (digits, m, digits, me, exp, tail)
    digits = max(0, min(9, 1 - int(math.floor(math.log10(e)))))
    return "%.*f ± %.*f%s" % (digits, shown, digits, e, tail)


def _numeric(value: float, err: Optional[float], unit: str) -> Dict[str, Any]:
    """A result's number, error and unit as plain values, for reports that follow it across runs (JSON has no NaN)."""
    ok = lambda v: isinstance(v, (int, float)) and math.isfinite(v)   # noqa: E731
    return {"num": float(value) if ok(value) else None, "err": float(err) if ok(err) else None, "unit": unit}


def _mad_sigma(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = median(values)
    return 1.4826 * median([abs(v - m) for v in values])


def noise_estimate(ys: Sequence[float]) -> float:
    """Point-to-point noise from second differences (insensitive to smooth trends)."""
    if len(ys) < 5:
        return 0.0
    d2 = [ys[i + 1] - 2 * ys[i] + ys[i - 1] for i in range(1, len(ys) - 1)]
    return _mad_sigma(d2) / math.sqrt(6)


# ---------------------------------------------------------------------------- fitting


def _solve(a: List[List[float]], b: List[float]) -> List[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-300:
            raise ZeroDivisionError("singular")
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def _inverse(a: List[List[float]]) -> List[List[float]]:
    n = len(a)
    cols = [_solve(a, [1.0 if i == j else 0.0 for i in range(n)]) for j in range(n)]
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def _lorentzian(u: float, p: Sequence[float]) -> float:
    b, a, c, w = p
    return b + a / (1 + ((u - c) / (abs(w) / 2 + 1e-12)) ** 2)


def _gaussian(u: float, p: Sequence[float]) -> float:
    b, a, c, w = p
    return b + a * math.exp(-4 * math.log(2) * (u - c) ** 2 / (w * w + 1e-24))


def _exponential(u: float, p: Sequence[float]) -> float:
    b, a, tau = p
    arg = -(u + 1) / (abs(tau) + 1e-12)  # u runs from -1 at the first point
    return b + a * math.exp(max(-700.0, arg))


def _oscillation(u: float, p: Sequence[float]) -> float:
    """A damped cosine (Rabi, Ramsey): offset, amplitude, frequency (cycles per unit), phase, decay time."""
    b, a, f, ph, tau = p
    s = u + 1   # runs from 0 at the first point
    return b + a * math.exp(max(-700.0, -s / (abs(tau) + 1e-12))) * math.cos(2 * math.pi * f * s + ph)


MODELS: Dict[str, Tuple[Callable[[float, Sequence[float]], float], Tuple[str, ...]]] = {
    "lorentzian": (_lorentzian, ("background", "amplitude", "center", "FWHM")),
    "gaussian": (_gaussian, ("background", "amplitude", "center", "FWHM")),
    "exponential": (_exponential, ("final value", "amplitude", "time constant")),
    "oscillation": (_oscillation, ("offset", "amplitude", "frequency", "phase", "decay time")),
}


def _lm(f: Callable, us: List[float], vs: List[float], p0: List[float], iters: int = 300):
    p = list(p0)
    k = len(p)

    def chi2(q):
        return sum((v - f(u, q)) ** 2 for u, v in zip(us, vs))

    cur = chi2(p)
    lam = 1e-3
    jtj = None
    for _ in range(iters):
        jac = []
        base = [f(u, p) for u in us]
        for j in range(k):
            h = 1e-6 * max(1.0, abs(p[j]))
            q = list(p)
            q[j] += h
            jac.append([(f(u, q) - b0) / h for u, b0 in zip(us, base)])
        res = [v - b0 for v, b0 in zip(vs, base)]
        jtj = [[sum(jac[i][n] * jac[j][n] for n in range(len(us))) for j in range(k)] for i in range(k)]
        jtr = [sum(jac[i][n] * res[n] for n in range(len(us))) for i in range(k)]
        improved = False
        for _ in range(12):
            a = [[jtj[i][j] + (lam * jtj[i][i] if i == j else 0.0) for j in range(k)] for i in range(k)]
            try:
                step = _solve(a, jtr)
            except ZeroDivisionError:
                lam *= 10
                continue
            q = [p[i] + step[i] for i in range(k)]
            new = chi2(q)
            if math.isfinite(new) and new < cur:
                done = (cur - new) <= 1e-12 * max(cur, 1e-300)
                p, cur, lam, improved = q, new, max(lam / 10, 1e-12), True
                break
            lam *= 10
        if not improved or done:
            break
    return p, cur, jtj


def fit(model: str, xs: Sequence[float], ys: Sequence[float], p0: Optional[Sequence[float]] = None) -> Optional[Dict[str, Any]]:
    """Fit `model` to (xs, ys). Parameters come back in data units with one-sigma errors."""
    n = len(xs)
    if n < 6:
        return None
    if model == "linear":
        return _linear(xs, ys)
    f, names = MODELS[model]
    x0, x1 = min(xs), max(xs)
    xc, xs_ = (x0 + x1) / 2, (x1 - x0) / 2 or 1.0
    yc = sum(ys) / n
    ys_ = (max(ys) - min(ys)) / 2 or 1.0
    us = [(x - xc) / xs_ for x in xs]
    vs = [(y - yc) / ys_ for y in ys]
    guess = list(p0) if p0 is not None else _guess(model, us, vs)
    try:
        p, chi, jtj = _lm(f, us, vs, guess)
        dof = max(1, n - len(p))
        cov = [[c * chi / dof for c in row] for row in _inverse(jtj)]
        errs = [math.sqrt(abs(cov[i][i])) for i in range(len(p))]
    except (ZeroDivisionError, OverflowError, ValueError):
        return None
    ss_tot = sum(v * v for v in vs) or 1e-300
    r2 = 1 - chi / ss_tot
    # back to data units
    if model in ("lorentzian", "gaussian"):
        b, a, c, w = p
        vals = [yc + b * ys_, a * ys_, xc + c * xs_, abs(w) * xs_]
        errv = [errs[0] * ys_, errs[1] * ys_, errs[2] * xs_, errs[3] * xs_]
    elif model == "oscillation":
        b, a, fr, ph, tau = p
        vals = [yc + b * ys_, abs(a) * ys_, abs(fr) / xs_, ph, abs(tau) * xs_]
        errv = [errs[0] * ys_, errs[1] * ys_, errs[2] / xs_, errs[3], errs[4] * xs_]
    else:
        b, a, tau = p
        vals = [yc + b * ys_, a * ys_, abs(tau) * xs_]
        errv = [errs[0] * ys_, errs[1] * ys_, errs[2] * xs_]
    grid = [x0 + (x1 - x0) * i / (FIT_POINTS - 1) for i in range(FIT_POINTS)]
    curve = [yc + ys_ * f((g - xc) / xs_, p) for g in grid]
    rms = math.sqrt(chi / n) * ys_
    return {"model": model, "names": list(names), "values": vals, "errors": errv, "r2": r2, "rms": rms,
            "curve": {"x": grid, "y": curve}}


def _guess(model: str, us: List[float], vs: List[float]) -> List[float]:
    n = len(us)
    edge = max(2, n // 7)
    if model == "oscillation":   # the frequency whose cosine and sine explain most of the curve
        mean = sum(vs) / n
        best = (0.0, 0.5, 0.0, 0.0)
        for k in range(1, 4 * n):
            f = k / 8.0                     # cycles per unit; the record spans 2 units
            if f > n / 4:
                break
            c = sum((v - mean) * math.cos(2 * math.pi * f * (u + 1)) for u, v in zip(us, vs))
            s = sum((v - mean) * math.sin(2 * math.pi * f * (u + 1)) for u, v in zip(us, vs))
            if c * c + s * s > best[0]:
                best = (c * c + s * s, f, c, s)
        _, f, c, s = best
        return [mean, 2 * math.hypot(c, s) / n, f, math.atan2(-s, c), 4.0]
    if model == "exponential":
        return [median(vs[-edge:]), vs[0] - median(vs[-edge:]), 0.6]
    base = median(vs[:edge] + vs[-edge:])
    k = max(range(n), key=lambda i: abs(vs[i] - base))
    amp = vs[k] - base
    half = base + amp / 2
    left = k
    while left > 0 and (vs[left] - half) * amp > 0:
        left -= 1
    right = k
    while right < n - 1 and (vs[right] - half) * amp > 0:
        right += 1
    width = max(abs(us[right] - us[left]), 2 * abs(us[1] - us[0]) if n > 1 else 0.1)
    return [base, amp, us[k], width]


def _linear(xs: Sequence[float], ys: Sequence[float]) -> Optional[Dict[str, Any]]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    icpt = my - slope * mx
    resid = [y - (icpt + slope * x) for x, y in zip(xs, ys)]
    chi = sum(r * r for r in resid)
    s2 = chi / max(1, n - 2)
    ss_tot = sum((y - my) ** 2 for y in ys) or 1e-300
    x0, x1 = min(xs), max(xs)
    return {"model": "linear", "names": ["intercept", "slope"], "values": [icpt, slope],
            "errors": [math.sqrt(s2 * (1 / n + mx * mx / sxx)), math.sqrt(s2 / sxx)],
            "r2": 1 - chi / ss_tot, "rms": math.sqrt(chi / n), "curve": {"x": [x0, x1], "y": [icpt + slope * x0, icpt + slope * x1]}}


# ---------------------------------------------------------------------------- one curve


def analyze_curve(xs: Sequence[float], ys: Sequence[float], x_name: str, y_name: str,
                  x_unit: str = "", y_unit: str = "", force: Optional[str] = None) -> Dict[str, Any]:
    """Classify one y(x) curve and quantify it. Returns kind, summary, results, fit, flags."""
    pairs = [(x, y) for x, y in zip(xs, ys) if isinstance(x, (int, float)) and isinstance(y, (int, float))
             and math.isfinite(x) and math.isfinite(y)]
    out: Dict[str, Any] = {"x": x_name, "y": y_name, "kind": "none", "summary": "", "results": [], "flags": [], "fit": None}
    if len(pairs) < 3:
        out["summary"] = "%s: too few points to analyse" % y_name
        return out
    xs_o = [p[0] for p in pairs]
    ys_o = [p[1] for p in pairs]
    order = sorted(range(len(xs_o)), key=lambda i: xs_o[i])
    xs_s, ys_s = [xs_o[i] for i in order], [ys_o[i] for i in order]
    span = max(ys_s) - min(ys_s)
    sigma = noise_estimate(ys_s)
    steps = [abs(b - a) for a, b in zip(xs_s, xs_s[1:]) if b != a]
    step = median(steps) if steps else 0.0
    xu = " " + x_unit if x_unit else ""

    def res(name: str, value: float, err: Optional[float], unit: str) -> None:
        out["results"].append({"name": name, "value": _fmt_pm(value, err, unit), **_numeric(value, err, unit)})

    # clipping: several points pinned at the same extreme value
    for extreme in (max(ys_s), min(ys_s)):
        pinned = sum(1 for y in ys_s if y == extreme)
        if pinned >= 3 and span > 0:
            out["flags"].append("%s sits at %s for %d points: possibly clipped (check range or sensitivity)"
                                % (y_name, si(extreme, y_unit), pinned))
    lin = _linear(xs_s, ys_s) if force in (None, "linear") else None

    def adequate(f: Optional[Dict[str, Any]]) -> bool:
        """The fit leaves only noise behind (residuals at the point-to-point noise level, which is itself
        only known to ~1/sqrt(n))."""
        return bool(f) and sigma > 0 and f["rms"] <= sigma * (1.25 + 2.5 / math.sqrt(len(ys_s)))

    def constant(noise: float) -> Dict[str, Any]:
        """No systematic dependence on x: a steady value, or no signal at all."""
        mean = sum(ys_s) / len(ys_s)
        if abs(mean) > 5 * noise:
            out["kind"] = "steady"
            out["summary"] = "%s %s: %s (fluctuations %s rms)" % (
                y_name, "is stable over " + si(xs_s[-1] - xs_s[0], x_unit) if x_name == "time" else "does not depend on " + x_name,
                _fmt_pm(mean, noise / math.sqrt(len(ys_s)), y_unit), si(noise, y_unit))
        else:
            out["kind"] = "flat"
            out["summary"] = "%s shows no signal above the noise (%s rms)" % (y_name, si(noise, y_unit))
        res("mean", mean, noise / math.sqrt(len(ys_s)), y_unit)
        res("noise (rms)", noise, None, y_unit)
        if lin:
            res("drift (slope)", lin["values"][1], lin["errors"][1], "")
            out["results"][-1]["value"] += " %s/%s" % (y_unit or "1", x_unit or "1")
            out["results"][-1]["unit"] = "%s/%s" % (y_unit or "1", x_unit or "1")
        return out

    if force is None and (span == 0 or (sigma > 0 and span < 5 * sigma)
                          or (adequate(lin) and abs(lin["values"][1]) < 3 * lin["errors"][1])):
        return constant(sigma or _mad_sigma(ys_s))

    candidates: Dict[str, Dict[str, Any]] = {"linear": lin} if lin else {}
    for model in ([force] if force else ["lorentzian", "gaussian", "exponential", "oscillation"]):
        f = candidates.get(model) or fit(model, xs_s, ys_s)
        if f and math.isfinite(f["r2"]):
            candidates[model] = f
    peaks = [candidates[m] for m in ("lorentzian", "gaussian") if m in candidates]
    best_peak = max(peaks, key=lambda f: f["r2"]) if peaks else None
    expo = candidates.get("exponential")
    k_ext = max(range(len(ys_s)), key=lambda i: abs(ys_s[i] - median(ys_s)))
    interior = 1 <= k_ext <= len(ys_s) - 2

    osc = candidates.get("oscillation")
    osc_ok = bool(osc) and (osc["r2"] >= 0.8 or adequate(osc)) and osc["values"][2] * (xs_s[-1] - xs_s[0]) >= 1.0 \
        and osc["values"][1] > 4 * osc["errors"][1] and osc["values"][4] > 0.3 / max(osc["values"][2], 1e-300)
    chosen = None
    if force:
        chosen = candidates.get(force)
    elif lin and (lin["r2"] >= 0.995 or adequate(lin)):
        chosen = lin
    elif osc_ok and osc["r2"] > max([best_peak["r2"] if best_peak else 0.0, expo["r2"] if expo else 0.0]) + 0.03:
        chosen = osc   # at least one full cycle, clearly better than a peak or a relaxation
    elif best_peak and (best_peak["r2"] >= 0.9 or (adequate(best_peak) and best_peak["r2"] >= 0.75
                                                    and abs(best_peak["values"][1]) > 5 * best_peak["errors"][1])) \
            and interior and best_peak["values"][1] != 0 and xs_s[0] < best_peak["values"][2] < xs_s[-1]:
        chosen = best_peak
    elif expo and (expo["r2"] >= 0.98 or adequate(expo)) and (not lin or expo["r2"] > lin["r2"] + 0.01):
        chosen = expo
    elif lin and lin["r2"] >= 0.9:
        chosen = lin
        if lin["r2"] < 0.98:
            out["flags"].append("%s deviates from a straight line beyond the noise (R² %.3f): look for curvature, "
                                "steps or settling transients" % (y_name, lin["r2"]))

    if chosen is None and force is None and lin and abs(lin["values"][1]) < 3 * lin["errors"][1] \
            and max(f["r2"] for f in candidates.values()) < 0.75:
        # slow fluctuations (correlated noise) around a constant: no model explains them well
        mean = sum(ys_s) / len(ys_s)
        constant(math.sqrt(sum((y - mean) ** 2 for y in ys_s) / (len(ys_s) - 1)))
        mid, spread = median(ys_s), _mad_sigma(ys_s)
        k = max(range(len(ys_s)), key=lambda i: abs(ys_s[i] - mid))
        if spread > 0 and abs(ys_s[k] - mid) > 5 * spread:
            out["flags"].append("one excursion to %s at %s = %s (%.0f× the typical spread)"
                                % (si(ys_s[k], y_unit), x_name, si(xs_s[k], x_unit), abs(ys_s[k] - mid) / spread))
        return out

    if chosen is None:
        diffs = [b - a for a, b in zip(ys_s, ys_s[1:])]
        up = sum(1 for d in diffs if d > 0) / max(1, len(diffs))
        k_max = max(range(len(ys_s)), key=lambda i: ys_s[i])
        k_min = min(range(len(ys_s)), key=lambda i: ys_s[i])
        if up >= 0.85 or up <= 0.15:
            out["kind"] = "monotonic"
            out["summary"] = "%s %s monotonically from %s to %s over %s %s to %s%s" % (
                y_name, "rises" if up >= 0.85 else "falls", si(ys_s[0], y_unit), si(ys_s[-1], y_unit),
                x_name, si(xs_s[0], x_unit), si(xs_s[-1], x_unit), "")
        else:
            out["kind"] = "structured"
            out["summary"] = "%s varies non-monotonically: max %s at %s = %s, min %s at %s = %s" % (
                y_name, si(ys_s[k_max], y_unit), x_name, si(xs_s[k_max], x_unit),
                si(ys_s[k_min], y_unit), x_name, si(xs_s[k_min], x_unit))
        res("maximum", ys_s[k_max], None, y_unit)
        res("at " + x_name, xs_s[k_max], None, x_unit)
        res("minimum", ys_s[k_min], None, y_unit)
        res("at " + x_name, xs_s[k_min], None, x_unit)
        if not interior and abs(ys_s[k_ext] - median(ys_s)) > 5 * max(sigma, 1e-300):
            out["flags"].append("the largest change is at the edge of the sweep; the feature may extend beyond %s"
                                % si(xs_s[k_ext], x_unit))
        return out

    out["fit"] = {"model": chosen["model"], "r2": chosen["r2"], "x": chosen["curve"]["x"], "y": chosen["curve"]["y"]}
    v, e = chosen["values"], chosen["errors"]
    if chosen["model"] == "linear":
        out["kind"] = "linear"
        slope_unit = "%s/%s" % (y_unit or "1", x_unit or "1")
        out["summary"] = "%s is linear in %s: slope %s %s (R² %.4f)" % (
            y_name, x_name, _fmt_pm(v[1], e[1], ""), slope_unit, chosen["r2"])
        res("slope", v[1], e[1], "")
        out["results"][-1]["value"] += " " + slope_unit
        out["results"][-1]["unit"] = slope_unit
        res("intercept", v[0], e[0], y_unit)
        out["params"] = {"slope": [v[1], e[1], slope_unit], "intercept": [v[0], e[0], y_unit]}
        if x_unit == "V" and y_unit == "A" and v[1] != 0:
            r, re = 1 / v[1], e[1] / v[1] ** 2
            res("resistance (1/slope)", r, re, "Ω")
            out["summary"] += "; R = %s" % _fmt_pm(r, re, "Ω")
        elif x_unit == "A" and y_unit == "V":
            res("resistance (slope)", v[1], e[1], "Ω")
            out["summary"] += "; R = %s" % _fmt_pm(v[1], e[1], "Ω")
    elif chosen["model"] in ("lorentzian", "gaussian"):
        b, a, c, w = v
        eb, ea, ec, ew = e
        out["kind"] = "peak" if a > 0 else "dip"
        out["summary"] = "%s has a %s at %s = %s (FWHM %s; %s fit, R² %.3f)" % (
            y_name, out["kind"], x_name, _fmt_pm(c, ec, x_unit), _fmt_pm(w, ew, x_unit), chosen["model"], chosen["r2"])
        res("center", c, ec, x_unit)
        res("FWHM", w, ew, x_unit)
        res("height" if a > 0 else "depth", abs(a), ea, y_unit)
        res("background", b, eb, y_unit)
        out["params"] = {"center": [c, ec, x_unit], "FWHM": [w, ew, x_unit],
                         ("height" if a > 0 else "depth"): [abs(a), ea, y_unit], "background": [b, eb, y_unit]}
        if x_unit == "Hz" and w > 0:
            q = c / w
            qe = q * math.sqrt((ec / c) ** 2 + (ew / w) ** 2) if c else None
            res("Q (center/FWHM)", q, qe, "")
            out["summary"] += ", Q ≈ %s" % _fmt_pm(q, qe, "")
        if sigma > 0:
            res("signal / noise", abs(a) / sigma, None, "")
        if step and w < 4 * step:
            out["flags"].append("the %s is under-resolved (FWHM %s ≈ %.1f steps); use a step ≤ %s between %s and %s"
                                % (out["kind"], si(w, x_unit), w / step, si(w / 5, x_unit),
                                   si(c - 2 * w, x_unit), si(c + 2 * w, x_unit)))
        if c - w / 2 < xs_s[0] or c + w / 2 > xs_s[-1]:
            out["flags"].append("the %s reaches the edge of the sweep; widen the range to see its full shape" % out["kind"])
    elif chosen["model"] == "oscillation":
        b, a, fr, ph, tau = v
        eb, ea, ef, eph, etau = e
        out["kind"] = "oscillation"
        out["summary"] = "%s oscillates with %s at %s (period %s), decaying with τ = %s (R² %.3f)" % (
            y_name, x_name, _fmt_pm(fr, ef, "1/" + x_unit if x_unit != "s" else "Hz"),
            si(1 / fr, x_unit), _fmt_pm(tau, etau, x_unit), chosen["r2"])
        res("frequency", fr, ef, "Hz" if x_unit == "s" else "1/" + (x_unit or "1"))
        res("period", 1 / fr, ef / fr ** 2, x_unit)
        if x_unit == "s":   # driven spins: half a period is the pi pulse
            res("half period (π time for Rabi)", 0.5 / fr, 0.5 * ef / fr ** 2, x_unit)
            out["summary"] += "; half period (π time) %s" % _fmt_pm(0.5 / fr, 0.5 * ef / fr ** 2, x_unit)
        res("decay time τ", tau, etau, x_unit)
        res("amplitude", a, ea, y_unit)
        res("offset", b, eb, y_unit)
        out["params"] = {"frequency": [fr, ef, "Hz" if x_unit == "s" else ""], "τ": [tau, etau, x_unit],
                         "amplitude": [a, ea, y_unit]}
        if step and step > 0.25 / fr:
            out["flags"].append("fewer than 4 points per period; use a step ≤ %s" % si(0.125 / fr, x_unit))
    else:
        b, a, tau = v
        out["kind"] = "relaxation"
        out["summary"] = "%s relaxes exponentially toward %s with τ = %s (R² %.3f)" % (
            y_name, _fmt_pm(b, e[0], y_unit), _fmt_pm(tau, e[2], x_unit), chosen["r2"])
        res("time constant τ", tau, e[2], x_unit)
        res("final value", b, e[0], y_unit)
        res("amplitude", a, e[1], y_unit)
        out["params"] = {"τ": [tau, e[2], x_unit], "final value": [b, e[0], y_unit]}
        if xs_s[-1] - xs_s[0] < 3 * tau:
            out["flags"].append("the record covers only %.1f τ; measure ≥ 5 τ to pin down the final value"
                                % ((xs_s[-1] - xs_s[0]) / tau))
    out["noise"] = chosen["rms"]
    return out


def hysteresis(xs: Sequence[float], ys: Sequence[float], x_unit: str, y_unit: str) -> Optional[str]:
    """For a there-and-back sweep: the largest up/down difference at the same x, if any."""
    if len(xs) < 6:
        return None
    turn = max(range(len(xs)), key=lambda i: xs[i]) if xs[1] >= xs[0] else min(range(len(xs)), key=lambda i: xs[i])
    if turn in (0, len(xs) - 1):
        return None
    up = {round(x, 12): y for x, y in zip(xs[:turn + 1], ys[:turn + 1])}
    down = {round(x, 12): y for x, y in zip(xs[turn:], ys[turn:])}
    common = [x for x in up if x in down]
    if len(common) < 3:
        return None
    diffs = [(abs(up[x] - down[x]), x) for x in common]
    d, at = max(diffs)
    span = max(ys) - min(ys) or 1e-300
    sigma = noise_estimate([up[x] for x in sorted(up)])
    if d <= max(5 * sigma, 0.02 * span):
        return None
    return "hysteresis: up and down sweeps differ by up to %s (%.0f%% of the signal) at %s" % (
        si(d, y_unit), 100 * d / span, si(at, x_unit))


# ---------------------------------------------------------------------------- a whole run


def analyze_run(rows: List[Dict[str, Any]], axes: List[Dict[str, str]], measures: List[Dict[str, str]],
                meta: Optional[Dict[str, Any]] = None, force: Optional[str] = None,
                only: Optional[str] = None) -> Dict[str, Any]:
    """axes/measures: [{key, unit, kind}] in plan order (kind 'scalar' or 'trace' for measures).
    Returns {series: [...], quality: [...], issues: [...], assessment, next}."""
    meta = meta or {}
    series: List[Dict[str, Any]] = []
    issues: List[str] = []
    quality: List[str] = []
    trends: List[str] = []
    real_axes = [a for a in axes if a["key"] not in ("repeat",)]
    x_axis = real_axes[-1] if real_axes else None
    outer = real_axes[0] if len(real_axes) > 1 else None

    status, done, planned = meta.get("status"), meta.get("points", len(rows)), meta.get("planned_points", len(rows))
    if status == "aborted":
        issues.append("run stopped after %s of %s points: %s" % (done, planned, meta.get("reason")))
    else:
        quality.append("all %s points recorded" % done)
    for w in meta.get("warnings") or []:
        issues.append(w)

    for m in measures:
        if only and m["key"] != only:
            continue
        if m.get("kind") == "trace":
            series += _trace_series(rows, m, x_axis, force)
            trend = _trend(series, m["key"])
            if trend:
                trends.append(trend)
            continue
        if x_axis is None:
            vals = [r[m["key"]] for r in rows if isinstance(r.get(m["key"]), (int, float))]
            if vals:
                mean = sum(vals) / len(vals)
                sd = _mad_sigma(vals) if len(vals) > 2 else 0.0
                series.append({"x": None, "y": m["key"], "kind": "value", "fit": None, "flags": [],
                               "summary": "%s = %s" % (m["key"], _fmt_pm(mean, sd / math.sqrt(len(vals)) if sd else None, m["unit"])),
                               "results": [{"name": "mean", "value": _fmt_pm(mean, None, m["unit"]),
                                            **_numeric(mean, sd / math.sqrt(len(vals)) if sd else None, m["unit"])}]})
            continue
        groups: Dict[Any, List[Dict[str, Any]]] = {}
        for r in rows:
            groups.setdefault(r.get(outer["key"]) if outer else None, []).append(r)
        for gval, grows in list(groups.items())[:12]:
            xs = [r.get(x_axis["key"]) for r in grows]
            ys = [r.get(m["key"]) for r in grows]
            s = analyze_curve(xs, ys, x_axis["key"], m["key"], x_axis.get("unit", ""), m["unit"], force)
            if outer is not None:
                s["group"] = "%s = %s" % (outer["key"], si(gval, outer.get("unit", ""))) if isinstance(gval, (int, float)) else str(gval)
            h = hysteresis([x for x in xs if isinstance(x, (int, float))], [y for y in ys if isinstance(y, (int, float))],
                           x_axis.get("unit", ""), m["unit"])
            if h:
                s["flags"].append(h)
            series.append(s)
        if outer is not None:
            trend = _trend(series, m["key"])
            if trend:
                trends.append(trend)

    for s in series:
        issues += ["%s%s: %s" % (s["y"], " (" + s["group"] + ")" if s.get("group") else "", f) for f in s["flags"]]

    # the most telling curve leads: a resonance or an oscillation, then a relaxation, then a line, then the rest
    rank = {"peak": 3, "dip": 3, "oscillation": 3, "relaxation": 2, "linear": 1}
    primary = max(enumerate(series), key=lambda kv: (rank.get(kv[1]["kind"], 0), -kv[0]))[1] if series else None
    sentences = []
    if primary:
        sentences.append(_cap(primary["summary"]) + ("" if primary["summary"].endswith(".") else "."))
    if trends:
        sentences.append(_cap(trends[0]) + ".")
    if issues:
        sentences.append(_cap(issues[0]) + ("." if not issues[0].endswith(".") else ""))
    else:
        sentences.append("%s; no problems found." % _cap(quality[0] if quality else "run complete"))
    nxt = _next_step(primary, issues, trends)
    return {"series": series, "quality": quality, "issues": issues, "trends": trends,
            "assessment": " ".join(sentences), "next": nxt}


def _cap(s: str) -> str:
    if not s or "." in s.split(" ", 1)[0]:  # starts with an instrument key such as lockin.X: leave it as written
        return s
    return s[:1].upper() + s[1:]


def _trend(series: List[Dict[str, Any]], key: str) -> Optional[str]:
    """How the fitted feature of `key` changes from the first to the last group of an outer loop."""
    tracked = [s for s in series if s["y"] == key and s.get("group") and s.get("params")]
    if len(tracked) < 2 or tracked[0]["kind"] != tracked[-1]["kind"]:
        return None
    first, last = tracked[0], tracked[-1]
    moves, still = [], []
    for name, (v0, e0, unit) in first["params"].items():
        if name not in last["params"] or name in ("background", "intercept"):
            continue
        v1, e1, _ = last["params"][name]
        d, de = v1 - v0, math.hypot(e0, e1)
        # a center shift matters on the scale of the line width; other changes relative to their size
        if name == "center" and "FWHM" in first["params"]:
            scale = 0.02 * first["params"]["FWHM"][0]
        else:
            scale = 0.005 * max(abs(v0), abs(v1))
        if abs(d) > 4 * de and abs(d) > scale:
            if name == "center":
                change = "shifts by %s" % _fmt_pm(d, de, unit)
            else:
                change = "%s by %s" % ("rises" if d > 0 else "falls", _fmt_pm(abs(d), de, unit))
            if name != "center" and v0:
                change += " (%+.0f%%)" % (100 * d / abs(v0))
            moves.append("%s %s" % (name, change))
        else:
            still.append(name)
    span = "from %s to %s" % (first["group"], last["group"].split(" = ")[-1])
    if not moves:
        return "%s, the %s %s does not change within the errors" % (span, key, first["kind"])
    text = "%s, the %s %s %s" % (span, key, first["kind"], " and ".join(moves[:2]))
    if "center" in still:
        text += "; its center stays put"
    return text


def _trace_series(rows: List[Dict[str, Any]], m: Dict[str, str], x_axis: Optional[Dict[str, str]],
                  force: Optional[str]) -> List[Dict[str, Any]]:
    out = []
    picked = rows if len(rows) <= 6 else [rows[0], rows[len(rows) // 2], rows[-1]]
    for r in picked:
        ys = r.get(m["key"])
        if not isinstance(ys, list) or len(ys) < 6:
            continue
        x0, x1 = r.get(m["key"] + ".axis") or (0, len(ys) - 1)
        n = len(ys)
        xs = [x0 + (x1 - x0) * i / (n - 1) for i in range(n)]
        s = analyze_curve(xs, ys, "frequency" if m.get("x_unit") == "Hz" else "x", m["key"], m.get("x_unit", ""), m["unit"], force)
        if x_axis is not None and len(rows) > 1:
            s["group"] = "%s = %s" % (x_axis["key"], si(r.get(x_axis["key"]), x_axis.get("unit", "")))
        # keep curves light for the browser
        if s.get("fit"):
            s["fit"]["x"], s["fit"]["y"] = s["fit"]["x"][::2], s["fit"]["y"][::2]
        out.append(s)
    return out


def _next_step(primary: Optional[Dict[str, Any]], issues: List[str], trends: Sequence[str] = ()) -> str:
    if primary is None:
        return "Record at least one measured quantity to analyse."
    for flag in primary["flags"]:
        if "under-resolved" in flag:
            return "Refine the sweep: " + flag.split("; ", 1)[-1] + "."
        if "edge of the sweep" in flag or "extend beyond" in flag:
            return "Widen the sweep range to capture the whole feature."
        if "clipped" in flag:
            return "Change the range or sensitivity so the signal is not clipped, then repeat."
    if any("stopped after" in i for i in issues):
        return "Fix the cause of the stop, then rerun the remaining range."
    kind = primary["kind"]
    if primary.get("group") and kind in ("peak", "dip", "linear", "relaxation"):
        outer = primary["group"].split(" = ")[0]
        if trends and "does not change" not in trends[0]:
            return "Add more %s values (and a finer step where it changes fastest) to map the dependence." % outer
        return "Nothing changes with %s over this range: widen it, or try another parameter." % outer
    if kind == "steady":
        if primary["x"] == "time":
            return "Stable: go ahead with the measurement."
        return "No dependence on %s within the noise: widen its range, or check that it reaches the sample." % primary["x"]
    if kind == "flat":
        return "No signal: raise the excitation or averaging, or check the wiring, before sweeping again."
    if kind in ("peak", "dip"):
        return "The %s is well resolved; next, track how it moves with a second parameter (field, temperature)." % kind
    if kind == "linear":
        return "The response is linear; extend the range to look for where it stops being linear."
    if kind == "relaxation":
        return "Use the fitted τ to set the wait time (about 5 τ) before each point of the next sweep."
    if kind == "oscillation":
        if primary["x"].endswith("tau") or "half period" in " ".join(r["name"] for r in primary["results"]):
            return ("Use the half period as the π pulse (and half of it for π/2), then run pulsed ODMR or Ramsey "
                    "with it.")
        return "The oscillation is resolved; next, see how its frequency changes with a second parameter."
    return "Look at the plot for structure the simple fits miss, then decide on a finer sweep."
