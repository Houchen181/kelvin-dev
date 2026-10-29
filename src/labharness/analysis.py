"""Automatic analysis of a run: what the data show, how good they are, and what to try next.

Curves are fitted with Levenberg-Marquardt (fitting.py) on normalised data; uncertainties come from the covariance
matrix scaled by the residual variance (one standard error). Everything is NumPy-vectorised: a long trace is first
looked at decimated (each stretch's lowest and highest point kept, so a narrow resonance stays in) and the model it
shows is then fitted to every point, and the whole analysis of a run stops after TIME_LIMIT seconds (an analysis
that took 11 minutes on a 16 001-point trace once held the lab).

It says only what the data support:
- a reading that is the read-back of the swept set point (or the swept axis itself) is checked for following it,
  never "fitted" as a finding ("magnet.B is linear in magnet.B_target");
- a sweep over two axes is a map, looked at as an image: spots against the map's own noise, with the threshold
  raised for the number of places a spot could have been (look-elsewhere), and the brightest one fitted with a 2-D
  Gaussian, instead of row by row ("no signal" across rows that held a 59k counts/s spot);
- a peak or dip must stand out from the noise by more than noise reaches somewhere along the sweep, and span more
  than one point;
- a resonance recorded in dB is fitted on linear power (a Lorentzian on dB values gave a Q 2.4 times too high), and
  says so;
- a run without numbers, or without any signal, says that, never "no problems found"."""
from __future__ import annotations

import math
import time
from statistics import median
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .cards import si, si_parts, trace_x

FIT_POINTS = 200
SELECT_POINTS = 2000      # a longer curve is decimated to about this many points to choose its model
TIME_LIMIT = 10.0         # seconds for a whole run's analysis (it runs while the lab is held)
MAP_MIN = 8               # values on each of two axes that make a sweep a map
DB_UNITS = ("dB", "dBm", "dBc", "dBV", "dBW")

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


def _np() -> Any:
    import numpy
    return numpy


def _mad_sigma(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    np = _np()
    v = np.asarray(values, dtype=float)
    return float(1.4826 * np.median(np.abs(v - np.median(v))))


def noise_estimate(ys: Sequence[float]) -> float:
    """Point-to-point noise from second differences (insensitive to smooth trends)."""
    if len(ys) < 5:
        return 0.0
    np = _np()
    y = np.asarray(ys, dtype=float)
    return _mad_sigma(y[2:] - 2 * y[1:-1] + y[:-2]) / math.sqrt(6)


def _db(unit: str) -> bool:
    return (unit or "").strip() in DB_UNITS


def _decimate(np: Any, x: Any, y: Any, most: int) -> Tuple[Any, Any]:
    """At most about `most` points: the first, the last, and in each stretch its lowest and highest (vectorised
    cards.thin), so a narrow peak or dip is never stepped over."""
    n = y.size
    if n <= most:
        return x, y
    size = -(-n // max(1, (most - 2) // 2))
    m = -(-n // size)
    pad = m * size - n
    blocks = np.concatenate([y, np.full(pad, np.nan)]).reshape(m, size)
    with np.errstate(invalid="ignore"):
        lo = np.nanargmin(np.where(np.isnan(blocks), np.inf, blocks), axis=1)
        hi = np.nanargmax(np.where(np.isnan(blocks), -np.inf, blocks), axis=1)
    keep = np.unique(np.concatenate([[0, n - 1], np.arange(m) * size + lo, np.arange(m) * size + hi]))
    keep = keep[keep < n]
    return x[keep], y[keep]

# ---------------------------------------------------------------------------- fitting (normalised curves)


def _lorentzian(u: Any, p: Sequence[float]) -> Any:
    b, a, c, w = p
    return b + a / (1 + ((u - c) / (abs(w) / 2 + 1e-12)) ** 2)


def _gaussian(u: Any, p: Sequence[float]) -> Any:
    b, a, c, w = p
    return b + a * _np().exp(-4 * math.log(2) * (u - c) ** 2 / (w * w + 1e-24))


def _exponential(u: Any, p: Sequence[float]) -> Any:
    b, a, tau = p
    np = _np()
    arg = -(u + 1) / (abs(tau) + 1e-12)  # u runs from -1 at the first point
    return b + a * np.exp(np.maximum(-700.0, arg))


def _oscillation(u: Any, p: Sequence[float]) -> Any:
    """A damped cosine (Rabi, Ramsey): offset, amplitude, frequency (cycles per unit), phase, decay time."""
    b, a, f, ph, tau = p
    np = _np()
    s = u + 1   # runs from 0 at the first point
    return b + a * np.exp(np.maximum(-700.0, -s / (abs(tau) + 1e-12))) * np.cos(2 * math.pi * f * s + ph)


MODELS: Dict[str, Tuple[Callable[[Any, Sequence[float]], Any], Tuple[str, ...]]] = {
    "lorentzian": (_lorentzian, ("background", "amplitude", "center", "FWHM")),
    "gaussian": (_gaussian, ("background", "amplitude", "center", "FWHM")),
    "exponential": (_exponential, ("final value", "amplitude", "time constant")),
    "oscillation": (_oscillation, ("offset", "amplitude", "frequency", "phase", "decay time")),
}


def fit(model: str, xs: Sequence[float], ys: Sequence[float], p0: Optional[Sequence[float]] = None,
        deadline: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Fit `model` to (xs, ys). Parameters come back in data units with one-sigma errors."""
    np = _np()
    from .fitting import FitError, least_squares
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    n = x.size
    if n < 6:
        return None
    if model == "linear":
        return _linear(x, y)
    f, names = MODELS[model]
    x0, x1 = float(x.min()), float(x.max())
    xc, xs_ = (x0 + x1) / 2, (x1 - x0) / 2 or 1.0
    yc = float(y.mean())
    ys_ = float(np.ptp(y)) / 2 or 1.0
    us = (x - xc) / xs_
    vs = (y - yc) / ys_
    guess = list(p0) if p0 is not None else _guess(model, us, vs)
    try:
        sol = least_squares(lambda p: f(us, p) - vs, guess, max_iter=300, deadline=deadline)
    except (FitError, ZeroDivisionError, OverflowError, ValueError, np.linalg.LinAlgError):
        return None
    p, chi = sol.p, sol.cost
    dof = max(1, n - len(p))
    errs = np.sqrt(np.clip(np.diag(sol.cov) * chi / dof, 0, None))
    if not np.all(np.isfinite(p)) or not np.all(np.isfinite(errs)):
        return None
    ss_tot = float((vs * vs).sum()) or 1e-300
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
    grid = np.linspace(x0, x1, FIT_POINTS)
    curve = yc + ys_ * f((grid - xc) / xs_, p)
    rms = math.sqrt(chi / n) * ys_
    return {"model": model, "names": list(names), "values": [float(v) for v in vals],
            "errors": [float(e) for e in errv], "r2": float(r2), "rms": rms,
            "curve": {"x": grid.tolist(), "y": curve.tolist()}, "_p": list(p), "_norm": (xc, xs_, yc, ys_)}


def _guess(model: str, us: Any, vs: Any) -> List[float]:
    np = _np()
    n = us.size
    edge = max(2, n // 7)
    if model == "oscillation":   # the frequency whose cosine and sine explain most of the curve (an FFT)
        mean = float(vs.mean())
        m = min(8192, max(64, 4 * n))
        grid = np.linspace(-1, 1, m)
        v = np.interp(grid, us, vs) - mean
        spec = np.fft.rfft(v)
        freqs = np.fft.rfftfreq(m, d=2.0 / (m - 1))   # cycles per unit; the record spans 2 units
        top = freqs <= n / 4
        k = 1 + int(np.argmax(np.abs(spec[1:][top[1:]]))) if spec.size > 1 and top[1:].any() else 1
        c = spec[k]
        f = float(freqs[k]) or 0.5
        return [mean, 2 * abs(c) / m, f, float(np.angle(c)), 4.0]   # the grid starts at s = u + 1 = 0
    if model == "exponential":
        tail = float(np.median(vs[-edge:]))
        return [tail, float(vs[0]) - tail, 0.6]
    base = float(np.median(np.concatenate([vs[:edge], vs[-edge:]])))
    k = int(np.argmax(np.abs(vs - base)))
    amp = float(vs[k] - base)
    inside = (vs - (base + amp / 2)) * (1.0 if amp >= 0 else -1.0) > 0
    out = np.flatnonzero(~inside[:k])
    left = int(out[-1]) if out.size else 0
    out = np.flatnonzero(~inside[k:])
    right = k + int(out[0]) if out.size else n - 1
    width = max(abs(float(us[right] - us[left])), 2 * abs(float(us[1] - us[0])) if n > 1 else 0.1)
    return [base, amp, float(us[k]), width]


def _linear(x: Any, y: Any) -> Optional[Dict[str, Any]]:
    np = _np()
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = x.size
    if n < 2:
        return None
    mx, my = float(x.mean()), float(y.mean())
    sxx = float(((x - mx) ** 2).sum())
    if sxx == 0:
        return None
    slope = float(((x - mx) * (y - my)).sum()) / sxx
    icpt = my - slope * mx
    resid = y - (icpt + slope * x)
    chi = float((resid * resid).sum())
    s2 = chi / max(1, n - 2)
    ss_tot = float(((y - my) ** 2).sum()) or 1e-300
    x0, x1 = float(x.min()), float(x.max())
    return {"model": "linear", "names": ["intercept", "slope"], "values": [icpt, slope],
            "errors": [math.sqrt(s2 * (1 / n + mx * mx / sxx)), math.sqrt(s2 / sxx)],
            "r2": 1 - chi / ss_tot, "rms": math.sqrt(chi / n), "curve": {"x": [x0, x1], "y": [icpt + slope * x0, icpt + slope * x1]}}


def _refit(model: str, chosen: Dict[str, Any], x: Any, y: Any, deadline: Optional[float]) -> Dict[str, Any]:
    """The model a decimated curve showed, fitted to every point (started from where the decimated fit ended)."""
    if model == "linear":
        return _linear(x, y) or chosen
    np = _np()
    xc, xs_, yc, ys_ = chosen["_norm"]
    x0, x1 = float(x.min()), float(x.max())
    nxc, nxs = (x0 + x1) / 2, (x1 - x0) / 2 or 1.0
    nyc, nys = float(y.mean()), float(np.ptp(y)) / 2 or 1.0
    vals = chosen["values"]
    if model in ("lorentzian", "gaussian"):
        p0 = [(vals[0] - nyc) / nys, vals[1] / nys, (vals[2] - nxc) / nxs, vals[3] / nxs]
    elif model == "oscillation":
        p0 = [(vals[0] - nyc) / nys, vals[1] / nys, vals[2] * nxs, chosen["_p"][3], vals[4] / nxs]
    else:
        p0 = [(vals[0] - nyc) / nys, vals[1] / nys, vals[2] / nxs]
    return fit(model, x, y, p0, deadline) or chosen

# ---------------------------------------------------------------------------- one curve


def _clean(xs: Sequence[Any], ys: Sequence[Any]) -> Tuple[Any, Any, int]:
    """The finite (x, y) pairs as float arrays sorted by x, and how many points there were at all."""
    np = _np()

    def numbers(vs: Sequence[Any]) -> Any:
        return np.array([float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else math.nan
                         for v in vs], dtype=float)
    x, y = numbers(xs), numbers(ys)
    k = min(x.size, y.size)
    x, y = x[:k], y[:k]
    ok = np.isfinite(x) & np.isfinite(y)
    order = np.argsort(x[ok], kind="stable")
    return x[ok][order], y[ok][order], k


def analyze_curve(xs: Sequence[float], ys: Sequence[float], x_name: str, y_name: str,
                  x_unit: str = "", y_unit: str = "", force: Optional[str] = None,
                  deadline: Optional[float] = None) -> Dict[str, Any]:
    """Classify one y(x) curve and quantify it. Returns kind, summary, results, fit, flags."""
    np = _np()
    out: Dict[str, Any] = {"x": x_name, "y": y_name, "kind": "none", "summary": "", "results": [], "flags": [], "fit": None}
    xs_s, ys_s, total = _clean(xs, ys)
    n = xs_s.size
    if n < 3:
        out["summary"] = "%s: too few points to analyse (%d with numbers%s)" % (
            y_name, n, " of %d" % total if total != n else "")
        if total and not n:
            out["flags"].append("%s has no numbers at any of its %d points" % (y_name, total))
        return out
    span = float(np.ptp(ys_s))
    sigma = noise_estimate(ys_s)
    steps = np.abs(np.diff(xs_s))
    steps = steps[steps > 0]
    step = float(np.median(steps)) if steps.size else 0.0
    db = _db(y_unit)

    def res(name: str, value: float, err: Optional[float], unit: str) -> None:
        out["results"].append({"name": name, "value": _fmt_pm(value, err, unit), **_numeric(value, err, unit)})

    # clipping: several points pinned at the same extreme value (not a reading that only takes a few values)
    quantized = np.unique(ys_s).size < 0.5 * n   # a reading that takes few values repeats its extremes too
    for extreme in (float(ys_s.max()), float(ys_s.min())):
        pinned = int((ys_s == extreme).sum())
        if pinned >= 3 and span > 0 and (not quantized or pinned >= max(5, 0.15 * n)):
            out["flags"].append("%s sits at %s for %d points: possibly clipped (check range or sensitivity)"
                                % (y_name, si(extreme, y_unit), pinned))
    lin = _linear(xs_s, ys_s) if force in (None, "linear") else None

    def adequate(f: Optional[Dict[str, Any]], noise: float = sigma, m: int = n) -> bool:
        """The fit leaves only noise behind (residuals at the point-to-point noise level, which is itself
        only known to ~1/sqrt(n))."""
        return bool(f) and noise > 0 and f["rms"] <= noise * (1.25 + 2.5 / math.sqrt(m))

    def constant(noise: float) -> Dict[str, Any]:
        """No systematic dependence on x: a steady value, or no signal at all."""
        mean = float(ys_s.mean())
        if abs(mean) > 5 * noise and not db:
            out["kind"] = "steady"
            out["summary"] = "%s %s: %s (fluctuations %s rms)" % (
                y_name, "is stable over " + si(float(xs_s[-1] - xs_s[0]), x_unit) if x_name == "time" else
                "does not depend on " + x_name, _fmt_pm(mean, noise / math.sqrt(n), y_unit), si(noise, y_unit))
        elif db:
            out["kind"] = "steady"
            out["summary"] = "%s does not depend on %s: %s (fluctuations %s rms)" % (
                y_name, x_name, _fmt_pm(mean, noise / math.sqrt(n), y_unit), si(noise, y_unit))
        else:
            out["kind"] = "flat"
            out["summary"] = "%s shows no signal above the noise (%s rms)" % (y_name, si(noise, y_unit))
        res("mean", mean, noise / math.sqrt(n), y_unit)
        res("noise (rms)", noise, None, y_unit)
        if lin:
            res("drift (slope)", lin["values"][1], lin["errors"][1], "")
            out["results"][-1]["value"] += " %s/%s" % (y_unit or "1", x_unit or "1")
            out["results"][-1]["unit"] = "%s/%s" % (y_unit or "1", x_unit or "1")
        return out

    if force is None and (span == 0 or (sigma > 0 and span < 5 * sigma)
                          or (adequate(lin) and abs(lin["values"][1]) < 3 * lin["errors"][1])):
        return constant(sigma or _mad_sigma(ys_s))

    # a long curve: its model chosen on a decimated copy, then fitted to every point
    xd, yd = _decimate(np, xs_s, ys_s, SELECT_POINTS)
    # a resonance in dB is a Lorentzian in linear power, not in dB
    pd = 10 ** (yd / 10) if db else yd
    p_sigma = noise_estimate(10 ** (ys_s / 10)) if db else sigma
    candidates: Dict[str, Dict[str, Any]] = {"linear": lin} if lin else {}
    for model in ([force] if force else ["lorentzian", "gaussian", "exponential", "oscillation"]):
        if model in candidates:
            continue
        if deadline is not None and time.monotonic() > deadline:
            out["flags"].append("the analysis ran out of time before trying every model")
            break
        on_power = db and model in ("lorentzian", "gaussian")
        f = fit(model, xd, pd if on_power else yd, deadline=deadline)
        if f and math.isfinite(f["r2"]):
            f["on_power"] = on_power
            candidates[model] = f
    peaks = [candidates[m] for m in ("lorentzian", "gaussian") if m in candidates]
    best_peak = max(peaks, key=lambda f: f["r2"]) if peaks else None
    expo = candidates.get("exponential")
    k_ext = int(np.argmax(np.abs(ys_s - np.median(ys_s))))
    interior = 1 <= k_ext <= n - 2

    def stands_out(f: Optional[Dict[str, Any]]) -> bool:
        """A peak or dip, not noise: more than one point wide, and its height above what noise reaches somewhere
        along a sweep of this many line widths (look-elsewhere)."""
        if not f or f["values"][1] == 0:
            return False
        a, ea, w = abs(f["values"][1]), f["errors"][1], f["values"][3]
        if step and w < step:
            return False
        trials = max(2.0, float(xs_s[-1] - xs_s[0]) / max(w, 1e-300))
        return ea > 0 and a / ea >= 3 + math.sqrt(2 * math.log(trials))

    osc = candidates.get("oscillation")
    osc_ok = bool(osc) and (osc["r2"] >= 0.8 or adequate(osc, sigma, len(yd))) and \
        osc["values"][2] * float(xs_s[-1] - xs_s[0]) >= 1.0 and osc["values"][1] > 4 * osc["errors"][1] and \
        osc["values"][4] > 0.3 / max(osc["values"][2], 1e-300)
    chosen = None
    if force:
        chosen = candidates.get(force)
    elif lin and (lin["r2"] >= 0.995 or adequate(lin)):
        chosen = lin
    elif osc_ok and osc["r2"] > max([best_peak["r2"] if best_peak else 0.0, expo["r2"] if expo else 0.0]) + 0.03:
        chosen = osc   # at least one full cycle, clearly better than a peak or a relaxation
    elif best_peak and (best_peak["r2"] >= 0.9 or (adequate(best_peak, p_sigma, len(yd)) and best_peak["r2"] >= 0.75
                                                   and abs(best_peak["values"][1]) > 5 * best_peak["errors"][1])) \
            and interior and best_peak["values"][1] != 0 and xs_s[0] < best_peak["values"][2] < xs_s[-1] \
            and stands_out(best_peak):
        chosen = best_peak
    elif expo and (expo["r2"] >= 0.98 or adequate(expo, sigma, len(yd))) and (not lin or expo["r2"] > lin["r2"] + 0.01):
        chosen = expo
    elif lin and lin["r2"] >= 0.9:
        chosen = lin
        if lin["r2"] < 0.98:
            out["flags"].append("%s deviates from a straight line beyond the noise (R² %.3f): look for curvature, "
                                "steps or settling transients" % (y_name, lin["r2"]))

    if chosen is None and best_peak and interior and step and best_peak["values"][3] < step and \
            abs(best_peak["values"][1]) > 5 * max(p_sigma, 1e-300):
        # one point apart from the rest: a glitch, or a feature narrower than the step (not a fitted peak)
        k = k_ext
        out["flags"].append("one point stands out: %s = %s at %s = %s, narrower than the step %s (a glitch, or a "
                            "feature finer than the sweep: refine around it to tell)"
                            % (y_name, si(float(ys_s[k]), y_unit), x_name, si(float(xs_s[k]), x_unit),
                               si(step, x_unit)))

    if chosen is None and force is None and lin and abs(lin["values"][1]) < 3 * lin["errors"][1] \
            and max(f["r2"] for f in candidates.values()) < 0.75:
        # slow fluctuations (correlated noise) around a constant: no model explains them well
        constant(float(ys_s.std(ddof=1)))
        mid, spread = float(np.median(ys_s)), _mad_sigma(ys_s)
        k = int(np.argmax(np.abs(ys_s - mid)))
        if spread > 0 and abs(float(ys_s[k]) - mid) > 5 * spread:
            out["flags"].append("one excursion to %s at %s = %s (%.0f× the typical spread)"
                                % (si(float(ys_s[k]), y_unit), x_name, si(float(xs_s[k]), x_unit),
                                   abs(float(ys_s[k]) - mid) / spread))
        return out

    if chosen is None:
        diffs = np.diff(ys_s)
        up = float((diffs > 0).sum()) / max(1, diffs.size)
        k_max, k_min = int(np.argmax(ys_s)), int(np.argmin(ys_s))
        if up >= 0.85 or up <= 0.15:
            out["kind"] = "monotonic"
            out["summary"] = "%s %s monotonically from %s to %s over %s %s to %s" % (
                y_name, "rises" if up >= 0.85 else "falls", si(float(ys_s[0]), y_unit), si(float(ys_s[-1]), y_unit),
                x_name, si(float(xs_s[0]), x_unit), si(float(xs_s[-1]), x_unit))
        else:
            out["kind"] = "structured"
            out["summary"] = "%s varies non-monotonically: max %s at %s = %s, min %s at %s = %s" % (
                y_name, si(float(ys_s[k_max]), y_unit), x_name, si(float(xs_s[k_max]), x_unit),
                si(float(ys_s[k_min]), y_unit), x_name, si(float(xs_s[k_min]), x_unit))
        res("maximum", float(ys_s[k_max]), None, y_unit)
        res("at " + x_name, float(xs_s[k_max]), None, x_unit)
        res("minimum", float(ys_s[k_min]), None, y_unit)
        res("at " + x_name, float(xs_s[k_min]), None, x_unit)
        if not interior and abs(float(ys_s[k_ext]) - float(np.median(ys_s))) > 5 * max(sigma, 1e-300):
            out["flags"].append("the largest change is at the edge of the sweep; the feature may extend beyond %s"
                                % si(float(xs_s[k_ext]), x_unit))
        return out

    on_power = bool(chosen.get("on_power"))
    if xd.size < n and chosen["model"] != "linear":   # fitted to every point now
        refined = _refit(chosen["model"], chosen, xs_s, 10 ** (ys_s / 10) if on_power else ys_s, deadline)
        refined["on_power"] = on_power
        chosen = refined
    fit_y = chosen["curve"]["y"]
    if on_power:   # the model drawn in the curve's own dB
        fit_y = [10 * math.log10(v) if v > 0 else None for v in fit_y]
    out["fit"] = {"model": chosen["model"], "r2": chosen["r2"], "x": chosen["curve"]["x"], "y": fit_y}
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
        width = "FWHM of the linear power" if on_power else "FWHM"
        out["summary"] = "%s has a %s at %s = %s (%s %s; %s fit%s, R² %.3f)" % (
            y_name, out["kind"], x_name, _fmt_pm(c, ec, x_unit), width, _fmt_pm(w, ew, x_unit), chosen["model"],
            " on 10^(dB/10)" if on_power else "", chosen["r2"])
        res("center", c, ec, x_unit)
        res(width, w, ew, x_unit)
        if on_power and b > 0:
            level = 10 * math.log10(b)
            edge = (b + a) / b
            if edge > 0:
                change = 10 * math.log10(edge)
                res("height" if a > 0 else "depth", abs(change), abs(10 / math.log(10) * ea / (b + a)), "dB")
            res("background", level, 10 / math.log(10) * eb / b, y_unit)
            out["params"] = {"center": [c, ec, x_unit], width: [w, ew, x_unit], "background": [level, None, y_unit]}
        else:
            res("height" if a > 0 else "depth", abs(a), ea, y_unit)
            res("background", b, eb, y_unit)
            out["params"] = {"center": [c, ec, x_unit], "FWHM": [w, ew, x_unit],
                             ("height" if a > 0 else "depth"): [abs(a), ea, y_unit], "background": [b, eb, y_unit]}
        if x_unit == "Hz" and w > 0:
            q = c / w
            qe = q * math.sqrt((ec / c) ** 2 + (ew / w) ** 2) if c else None
            res("Q loaded (center/FWHM of the linear power)" if on_power else "Q (center/FWHM)", q, qe, "")
            out["summary"] += ", Q ≈ %s" % _fmt_pm(q, qe, "")
        elif db and not on_power:
            out["flags"].append("the width was fitted on dB values: it is the width of the dB curve, not a linewidth")
        if p_sigma > 0 and not on_power:
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

# ---------------------------------------------------------------------------- a read-back of the swept set point


def tracks(axis: Dict[str, str], measure: Dict[str, str], xs: Sequence[Any], ys: Sequence[Any],
           readbacks: Optional[Dict[str, str]] = None) -> bool:
    """Whether a reading is the swept axis itself, or the read-back of its set point: the card says so (readbacks),
    or it is the same instrument's reading in the same unit that follows the set value about one to one."""
    if measure["key"] == axis["key"] or (readbacks or {}).get(axis["key"]) == measure["key"]:
        return True
    if axis["key"].split(".")[0] != measure["key"].split(".")[0] or not axis.get("unit") or \
            axis.get("unit") != measure.get("unit"):
        return False
    x, y, _ = _clean(xs, ys)
    lin = _linear(x, y) if x.size >= 3 else None
    return bool(lin) and 0.8 <= lin["values"][1] <= 1.25 and lin["r2"] > 0.9


def _tracking(xs: Sequence[Any], ys: Sequence[Any], axis: Dict[str, str], measure: Dict[str, str]) -> Dict[str, Any]:
    """A read-back against its set point: how closely it followed, not a fit (a fit of a set point's read-back
    against it says only that the instrument did what it was told)."""
    np = _np()
    x, y, total = _clean(xs, ys)
    xn, yn, unit = axis["key"], measure["key"], measure.get("unit", "")
    out: Dict[str, Any] = {"x": xn, "y": yn, "kind": "tracking", "results": [], "flags": [], "fit": None}
    if x.size < 2:
        out["summary"] = "%s: too few points to compare with %s" % (yn, xn)
        return out
    d = y - x
    k = int(np.argmax(np.abs(d)))
    noise = noise_estimate(y)
    same = yn == xn
    out["summary"] = "%s %s %s: largest difference %s at %s = %s, mean %s" % (
        yn, "(as read back) follows" if same else "is the read-back of", xn + (" (as set)" if same else ""),
        si(float(d[k]), unit), xn, si(float(x[k]), axis.get("unit", "")), si(float(d.mean()), unit))
    out["results"] = [{"name": "largest difference from the set value", "value": si(float(d[k]), unit),
                       **_numeric(float(d[k]), None, unit)},
                      {"name": "mean difference", "value": si(float(d.mean()), unit),
                       **_numeric(float(d.mean()), None, unit)}]
    span_x, span_y = float(np.ptp(x)), float(np.ptp(y))
    if span_x > 0 and span_y < 0.1 * span_x:
        out["flags"].append("%s stayed between %s and %s while %s went from %s to %s: the sweep did not reach it" % (
            yn, si(float(y.min()), unit), si(float(y.max()), unit), xn, si(float(x[0]), axis.get("unit", "")),
            si(float(x[-1]), axis.get("unit", ""))))
    elif abs(float(d[k])) > max(0.05 * span_x, 5 * noise):
        out["flags"].append("%s lagged or missed the set value by up to %s (at %s = %s): points may not have been "
                            "measured where the plan says" % (yn, si(abs(float(d[k])), unit), xn,
                                                               si(float(x[k]), axis.get("unit", ""))))
    return out

# ---------------------------------------------------------------------------- a map (two swept axes)


def _box3(np: Any, a: Any) -> Tuple[Any, Any]:
    """3 x 3 sums of the finite values and how many there were."""
    ok = np.isfinite(a)
    v = np.where(ok, a, 0.0)
    pv, pk = np.pad(v, 1), np.pad(ok.astype(float), 1)
    h, w = a.shape
    s = sum(pv[i:i + h, j:j + w] for i in range(3) for j in range(3))
    c = sum(pk[i:i + h, j:j + w] for i in range(3) for j in range(3))
    return s, c


def analyze_map(outs: Sequence[float], ins: Sequence[float], z: Any, x_axis: Dict[str, str], y_axis: Dict[str, str],
                measure: Dict[str, str], deadline: Optional[float] = None) -> Dict[str, Any]:
    """A measured value over two swept axes, looked at as an image (x: the inner axis, y: the outer). Spots are
    places a 3 x 3 neighbourhood stands out from the map's own noise by more than noise alone would reach anywhere in
    a map of this many points (look-elsewhere); the brightest is fitted with a 2-D Gaussian."""
    np = _np()
    from .fitting import FitError, least_squares
    xk, yk, zk = x_axis["key"], y_axis["key"], measure["key"]
    xu, yu, zu = x_axis.get("unit", ""), y_axis.get("unit", ""), measure.get("unit", "")
    ins_a, outs_a = np.asarray(ins, dtype=float), np.asarray(outs, dtype=float)
    out: Dict[str, Any] = {"x": xk, "y": zk, "kind": "map", "results": [], "flags": [], "fit": None,
                           "map": {"x": xk, "y": yk}}

    def res(name: str, value: float, err: Optional[float], unit: str) -> None:
        out["results"].append({"name": name, "value": _fmt_pm(value, err, unit), **_numeric(value, err, unit)})
    finite = np.isfinite(z)
    count = int(finite.sum())
    if count < 16:
        out["kind"] = "none"
        out["summary"] = "%s: too few points with numbers to look at as a map (%d)" % (zk, count)
        return out
    bg = float(np.median(z[finite]))
    d2 = z[:, 2:] - 2 * z[:, 1:-1] + z[:, :-2]
    sigma = _mad_sigma(d2[np.isfinite(d2)]) / math.sqrt(6) if np.isfinite(d2).any() else 0.0
    if sigma <= 0:   # a map without point-to-point noise (simulated): any real structure counts
        sigma = max(float(np.ptp(z[finite])) * 1e-9, 1e-300)
    s, c = _box3(np, z - bg)
    with np.errstate(invalid="ignore", divide="ignore"):
        sig = np.where(c > 0, s / np.maximum(c, 1) / (sigma / np.sqrt(np.maximum(c, 1))), 0.0)
    sig = np.where(finite, sig, 0.0)
    threshold = math.sqrt(2 * math.log(count)) + 3   # noise alone reaches about sqrt(2 ln N) somewhere
    sign = 1.0 if sig.max() >= -sig.min() else -1.0
    t = sign * sig
    pad = np.pad(t, 1, constant_values=-np.inf)
    h, w = t.shape
    neighbours = np.max([pad[i:i + h, j:j + w] for i in range(3) for j in range(3) if (i, j) != (1, 1)], axis=0)
    peaks = np.argwhere((t >= neighbours) & (t > threshold))
    order = np.argsort(-t[peaks[:, 0], peaks[:, 1]]) if peaks.size else []
    spots = [tuple(peaks[i]) for i in order][:10]
    res("background (median)", bg, None, zu)
    res("noise (rms, point to point)", sigma, None, zu)
    xi = lambda i: float(np.interp(i, np.arange(ins_a.size), ins_a))     # noqa: E731  index -> axis value
    yi = lambda i: float(np.interp(i, np.arange(outs_a.size), outs_a))   # noqa: E731
    if not spots:
        yy, xx = np.nonzero(finite)
        a = np.column_stack([xx, yy, np.ones_like(xx)]).astype(float)
        coef, *_ = np.linalg.lstsq(a, z[finite], rcond=None)
        rest = z[finite] - a @ coef
        k = np.unravel_index(int(np.nanargmax(np.abs(np.where(finite, z - bg, np.nan)))), z.shape)
        biggest = float(t.max())
        if float(rest.std()) < 2 * sigma and abs(coef[0] * w) + abs(coef[1] * h) < 5 * sigma:
            out["kind"] = "flat"
            out["summary"] = ("%s shows no feature above the noise across the map (largest excursion %.1fσ; noise "
                              "alone reaches about %.1fσ somewhere in %d points)" % (zk, biggest,
                                                                                    threshold - 3, count))
        else:
            out["kind"] = "structured map"
            out["summary"] = ("%s varies across the map without a localised spot: %s at %s = %s, %s = %s; range %s "
                              "to %s" % (zk, "largest" if z[k] >= bg else "lowest", xk, si(xi(k[1]), xu), yk,
                                         si(yi(k[0]), yu), si(float(np.nanmin(z)), zu), si(float(np.nanmax(z)), zu)))
        return out
    i0, j0 = spots[0]
    out["kind"] = "spot" if sign > 0 else "dark spot"
    # a window around the brightest spot, as wide as the part above half its height (at least 9 x 9 points)
    above = (sign * (z - bg) > 0.5 * sign * (z[i0, j0] - bg)) & finite
    half = max(4, int(3 * math.sqrt(max(1, int(above[max(0, i0 - 10):i0 + 11, max(0, j0 - 10):j0 + 11].sum())) / math.pi)))
    r0, r1, c0, c1 = max(0, i0 - half), min(h, i0 + half + 1), max(0, j0 - half), min(w, j0 + half + 1)
    sub = z[r0:r1, c0:c1]
    yy, xx = np.mgrid[r0:r1, c0:c1]
    ok = np.isfinite(sub)
    X, Y, Z = xx[ok].astype(float), yy[ok].astype(float), sub[ok]
    amp0 = float(z[i0, j0] - bg)
    width0 = max(1.5, 2 * math.sqrt(max(1, int(above[r0:r1, c0:c1].sum())) / math.pi))
    p0 = [bg, amp0, float(j0), float(i0), width0, width0]

    def model(p: Any) -> Any:
        return p[0] + p[1] * np.exp(-4 * math.log(2) * (((X - p[2]) / p[4]) ** 2 + ((Y - p[3]) / p[5]) ** 2))
    fitted = None
    if Z.size >= 8:
        try:
            sol = least_squares(lambda p: model(p) - Z, p0, lower=[-np.inf, -np.inf, c0 - 1, r0 - 1, 0.3, 0.3],
                                upper=[np.inf, np.inf, c1, r1, 4.0 * half, 4.0 * half], deadline=deadline)
            dof = max(1, Z.size - 6)
            err = np.sqrt(np.clip(np.diag(sol.cov) * sol.cost / dof, 0, None))
            if np.all(np.isfinite(sol.p)) and np.all(np.isfinite(err)):
                fitted = (sol.p, err)
        except (FitError, ValueError, np.linalg.LinAlgError):
            fitted = None
    dx = float(np.median(np.abs(np.diff(ins_a)))) if ins_a.size > 1 else 1.0
    dy = float(np.median(np.abs(np.diff(outs_a)))) if outs_a.size > 1 else 1.0
    if fitted is not None:
        p, e = fitted
        cx, cy = xi(p[2]), yi(p[3])
        out["summary"] = ("%s has a %s at %s = %s, %s = %s (FWHM %s × %s; 2-D Gaussian, %.0fσ above the noise, which "
                          "alone reaches about %.1fσ somewhere in %d points)" % (
                              zk, out["kind"], xk, _fmt_pm(cx, e[2] * dx, xu), yk, _fmt_pm(cy, e[3] * dy, yu),
                              si(abs(p[4]) * dx, xu), si(abs(p[5]) * dy, yu), float(t[i0, j0]), threshold - 3, count))
        res("center " + xk, cx, e[2] * dx, xu)
        res("center " + yk, cy, e[3] * dy, yu)
        res("FWHM along " + xk, abs(p[4]) * dx, e[4] * dx, xu)
        res("FWHM along " + yk, abs(p[5]) * dy, e[5] * dy, yu)
        res("height" if p[1] > 0 else "depth", abs(p[1]), e[1], zu)
        out["params"] = {"center": [cx, e[2] * dx, xu], "height": [abs(p[1]), e[1], zu]}
        if min(abs(p[4]), abs(p[5])) < 2:
            out["flags"].append("the %s is under-resolved (FWHM under 2 points): use a finer step around it" % out["kind"])
        if not (1 <= p[2] <= w - 2 and 1 <= p[3] <= h - 2):
            out["flags"].append("the %s sits at the edge of the map; widen it to see the whole spot" % out["kind"])
    else:
        out["summary"] = "%s has a %s at %s = %s, %s = %s (%.0fσ above the noise, which alone reaches about %.1fσ " \
                         "somewhere in %d points; the 2-D Gaussian did not fit)" % (
                             zk, out["kind"], xk, si(xi(j0), xu), yk, si(yi(i0), yu), float(t[i0, j0]),
                             threshold - 3, count)
        res("at " + xk, xi(j0), None, xu)
        res("at " + yk, yi(i0), None, yu)
    res("significance (σ, 3 x 3 points)", float(t[i0, j0]), None, "")
    if len(spots) > 1:
        out["kind"] = "spots"
        out["summary"] += "; %d more above the threshold, at %s" % (len(spots) - 1, "; ".join(
            "%s = %s, %s = %s" % (xk, si(xi(j), xu), yk, si(yi(i), yu)) for i, j in spots[1:4]))
        res("spots found", float(len(spots)), None, "")
    return out


def map_grid(rows: List[Dict[str, Any]], outer: str, inner: str, key: str) -> Optional[Tuple[List[float], List[float], Any]]:
    """A run's points as a grid for analyze_map, when both axes take at least MAP_MIN values and the points fill at
    least half of it."""
    from .pictures import grid_of
    grid = grid_of(rows, outer, inner, key)
    if grid is None:
        return None
    outs, ins, z = grid
    if len(outs) < MAP_MIN or len(ins) < MAP_MIN or len(rows) < 0.5 * len(outs) * len(ins):
        return None
    return grid

# ---------------------------------------------------------------------------- a whole run


def analyze_run(rows: List[Dict[str, Any]], axes: List[Dict[str, str]], measures: List[Dict[str, str]],
                meta: Optional[Dict[str, Any]] = None, force: Optional[str] = None,
                only: Optional[str] = None, readbacks: Optional[Dict[str, str]] = None,
                time_limit: float = TIME_LIMIT) -> Dict[str, Any]:
    """axes/measures: [{key, unit, kind}] in plan order (kind 'scalar' or 'trace' for measures). readbacks: each set
    point's read-back reading, where the cards say. Returns {series: [...], quality: [...], issues: [...],
    assessment, next}."""
    meta = meta or {}
    series: List[Dict[str, Any]] = []
    issues: List[str] = []
    quality: List[str] = []
    trends: List[str] = []
    try:
        _np()
    except ImportError:
        return {"series": [], "quality": [], "issues": ["not analysed: the automatic analysis needs NumPy"],
                "trends": [], "assessment": "Not analysed: the automatic analysis needs NumPy, which this Kelvin does "
                "not have.", "next": "Look at the data with get_run_data."}
    deadline = time.monotonic() + time_limit
    real_axes = [a for a in axes if a["key"] not in ("repeat",) and a.get("kind") != "repeat"]
    x_axis = real_axes[-1] if real_axes else None
    outer = real_axes[0] if len(real_axes) > 1 else None

    status, done, planned = meta.get("status"), meta.get("points", len(rows)), meta.get("planned_points", len(rows))
    if not rows:
        issues.append("no data: the run recorded no points")
    elif status == "aborted":
        issues.append("run stopped after %s of %s points: %s" % (done, planned, meta.get("reason")))
    else:
        quality.append("all %s points recorded" % done)
    for w in meta.get("warnings") or []:
        issues.append(w)
    if rows and not measures:
        issues.append("nothing was measured: the plan has no measure")
    skipped: List[str] = []

    for m in measures:
        if only and m["key"] != only:
            continue
        if not rows:
            break
        if time.monotonic() > deadline:
            skipped.append(m["key"])
            continue
        if m.get("kind") == "trace":
            series += _trace_series(rows, m, x_axis, force, deadline)
            trend = _trend(series, m["key"])
            if trend:
                trends.append(trend)
            continue
        if m.get("kind") == "image":
            continue   # camera frames: look_at shows them
        values = [r.get(m["key"]) for r in rows]
        numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)]
        if not numbers:
            issues.append("%s: no numbers at any of its %d points" % (m["key"], len(rows)))
            continue
        if x_axis is None:
            mean = sum(numbers) / len(numbers)
            sd = _mad_sigma(numbers) if len(numbers) > 2 else 0.0
            series.append({"x": None, "y": m["key"], "kind": "value", "fit": None, "flags": [],
                           "summary": "%s = %s" % (m["key"], _fmt_pm(mean, sd / math.sqrt(len(numbers)) if sd else None, m["unit"])),
                           "results": [{"name": "mean", "value": _fmt_pm(mean, None, m["unit"]),
                                        **_numeric(mean, sd / math.sqrt(len(numbers)) if sd else None, m["unit"])}]})
            continue
        followed = next((a for a in real_axes if tracks(a, m, [r.get(a["key"]) for r in rows], values, readbacks)),
                        None)
        if followed is not None:   # a set point's read-back: checked for following it, never fitted as a finding
            s = _tracking([r.get(followed["key"]) for r in rows], values, followed, m)
            series.append(s)
            continue
        grid = map_grid(rows, outer["key"], x_axis["key"], m["key"]) if outer is not None else None
        if grid is not None:   # a map: looked at as an image, not row by row
            outs, ins, z = grid
            series.append(analyze_map(outs, ins, z, x_axis, outer, m, deadline))
            continue
        groups: Dict[Any, List[Dict[str, Any]]] = {}
        for r in rows:
            groups.setdefault(r.get(outer["key"]) if outer else None, []).append(r)
        for gval, grows in list(groups.items())[:12]:
            xs = [r.get(x_axis["key"]) for r in grows]
            ys = [r.get(m["key"]) for r in grows]
            s = analyze_curve(xs, ys, x_axis["key"], m["key"], x_axis.get("unit", ""), m["unit"], force, deadline)
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
    if skipped:
        issues.append("not analysed in the %.0f s the automatic analysis takes: %s (analyze_run with column analyses "
                      "one)" % (time_limit, ", ".join(skipped)))
    findings = [s for s in series if s["kind"] not in ("tracking", "none")]
    if rows and measures and series and findings and all(s["kind"] in ("flat",) for s in findings):
        issues.append("no measured column shows a signal above the noise")

    # the most telling curve leads: a resonance, a spot or an oscillation, then a relaxation, then a line, then the
    # rest; a read-back only when there is nothing else
    rank = {"peak": 3, "dip": 3, "oscillation": 3, "spot": 3, "spots": 3, "dark spot": 3, "relaxation": 2,
            "linear": 1, "structured map": 1, "tracking": -1, "none": -2}
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
        if name not in last["params"] or name in ("background", "intercept") or e0 is None:
            continue
        v1, e1, _ = last["params"][name]
        if e1 is None:
            continue
        d, de = v1 - v0, math.hypot(e0, e1)
        # a center shift matters on the scale of the line width; other changes relative to their size
        width = first["params"].get("FWHM") or first["params"].get("FWHM of the linear power")
        if name == "center" and width:
            scale = 0.02 * width[0]
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
                  force: Optional[str], deadline: Optional[float] = None) -> List[Dict[str, Any]]:
    out = []
    picked = rows if len(rows) <= 6 else [rows[0], rows[len(rows) // 2], rows[-1]]
    for r in picked:
        ys = r.get(m["key"])
        if not isinstance(ys, list) or len(ys) < 6:
            continue
        if deadline is not None and time.monotonic() > deadline:
            break
        xs = trace_x(r, m["key"], len(ys))
        s = analyze_curve(xs, ys, "frequency" if m.get("x_unit") == "Hz" else "x", m["key"], m.get("x_unit", ""),
                          m["unit"], force, deadline)
        if x_axis is not None and len(rows) > 1:
            s["group"] = "%s = %s" % (x_axis["key"], si(r.get(x_axis["key"]), x_axis.get("unit", "")))
        # keep curves light for the browser
        if s.get("fit"):
            s["fit"]["x"], s["fit"]["y"] = s["fit"]["x"][::2], s["fit"]["y"][::2]
        out.append(s)
    return out


def _next_step(primary: Optional[Dict[str, Any]], issues: List[str], trends: Sequence[str] = ()) -> str:
    if primary is None:
        if any(i.startswith("no data") for i in issues):
            return "Find out why no point was recorded (the run's reason and the event log), then run it again."
        return "Record at least one measured quantity to analyse."
    for flag in primary["flags"]:
        if "under-resolved" in flag and "; use a step" in flag:
            return "Refine the sweep: " + flag.split("; ", 1)[-1] + "."
        if "under-resolved" in flag:
            return "Refine the map: use a finer step around the spot."
        if "edge of the sweep" in flag or "extend beyond" in flag or "edge of the map" in flag:
            return "Widen the sweep range to capture the whole feature."
        if "clipped" in flag:
            return "Change the range or sensitivity so the signal is not clipped, then repeat."
        if "did not reach it" in flag:
            return "Find out why the set point was not reached (ramp rate, the instrument's mode or limits) before " \
                   "measuring again."
    if any("stopped after" in i for i in issues):
        return "Fix the cause of the stop, then rerun the remaining range."
    kind = primary["kind"]
    if kind in ("spot", "spots", "dark spot"):
        return "Center a finer map, or a line scan, on the spot to resolve its shape."
    if kind == "structured map":
        return "Look at the map (look_at) and cut through its structure with a line sweep."
    if kind == "tracking":
        return "Measure a quantity of the sample, not only the read-back of what was set."
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
