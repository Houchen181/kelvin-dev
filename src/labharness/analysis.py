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
  Gaussian, instead of row by row ("no signal" across rows that held a 59k counts/s spot); a "spot" wider than the
  map is none, and the map is told along each axis instead (a magnetoresistance was once a "2-D Gaussian spot");
- a peak or dip must stand out from the noise by more than noise reaches somewhere along the sweep, span more than
  one point and fit inside the sweep: a curve that only bends is said to ("rises as B²", never a dip with an FWHM of
  117 T), and two resolved lines are two (an ODMR doublet was once "does not depend on frequency");
- an oscillation needs turning points well inside the sweep (a peak filling the window is no cycle of a cosine), a
  decay a time the data pin down, and on an axis that isn't time neither is told in time's words ("τ in GHz");
- a resonance recorded in dB is fitted on linear power (a Lorentzian on dB values gave a Q 2.4 times too high), and
  says so;
- the rows of a run (a trace at each field, a sweep at each temperature) are fitted with one model, so a change along
  the outer loop is the sample's, not the fits' (a Lorentzian on one row and a Gaussian on the next made a 14 % "drop"
  in Q), and a line that moves out of the window is said to;
- what the run measured sets what is looked for and what leads: a pulse protocol (rabi, ramsey...) the shape, a ratio
  of two readings (a contrast) before its parts, a guard reading (a leakage current) only when nothing else was
  measured, and a reading the measurement chain turns into the sample's resistance is told in ohms;
- a first point far outside all the others (a counter read before it settled) is said, and left out;
- a run without numbers, or without any signal, says that, never "no problems found"."""
from __future__ import annotations

import math
import time
import warnings
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .cards import si, si_parts, trace_x

FIT_POINTS = 200
SELECT_POINTS = 2000      # a longer curve is decimated to about this many points to choose its model
TIME_LIMIT = 10.0         # seconds for a whole run's analysis (it runs while the lab is held)
MAP_MIN = 8               # values on each of two axes that make a sweep a map
TRACE_ROWS = 12           # a run's traces looked at: spread over its rows, the first and the last among them
RISE = 2 * math.atanh(0.8)   # a tanh step's rise from 10 % to 90 % of its change, in its widths
DB_UNITS = ("dB", "dBm", "dBc", "dBV", "dBW")
PEAKS = ("lorentzian", "gaussian")
LINES = PEAKS + ("double_lorentzian",)
# what a pulse protocol (pulses.PROTOCOLS) shows along the axis it sweeps (told by its unit)
EXPECT = {"cw": ("lines", "Hz"), "pulsed_odmr": ("lines", "Hz"), "rabi": ("oscillation", "s"),
          "ramsey": ("oscillation", "s"), "hahn_echo": ("decay", "s"), "t1": ("decay", "s")}

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


def _at(value: float, unit: str, resolution: float) -> str:
    """A value with the digits that tell values `resolution` apart: 3.9999985 GHz, not 4 GHz."""
    if not (math.isfinite(value) and math.isfinite(resolution) and resolution > 0):
        return si(value, unit)
    shown, factor, shown_unit = si_parts(value if value else resolution, unit)
    digits = max(0, min(12, int(math.ceil(-math.log10(resolution / factor)))))
    return "%.*f%s" % (digits, shown if value else 0.0, " " + shown_unit if shown_unit else "")


def _numeric(value: float, err: Optional[float], unit: str) -> Dict[str, Any]:
    """A result's number, error and unit as plain values, for reports that follow it across runs (JSON has no NaN)."""
    ok = lambda v: isinstance(v, (int, float)) and math.isfinite(v)   # noqa: E731
    return {"num": float(value) if ok(value) else None, "err": float(err) if ok(err) else None, "unit": unit}


def _np() -> Any:
    import numpy
    return numpy


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


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


def _double(u: Any, p: Sequence[float]) -> Any:
    """Two Lorentzian lines on one background (an ODMR doublet, a split resonance)."""
    b, a1, c1, w1, a2, c2, w2 = p
    return b + a1 / (1 + ((u - c1) / (abs(w1) / 2 + 1e-12)) ** 2) + a2 / (1 + ((u - c2) / (abs(w2) / 2 + 1e-12)) ** 2)


def _step(u: Any, p: Sequence[float]) -> Any:
    """A step (a superconducting transition, a switch): the level before, the change, the midpoint and the width (a
    tanh's: 10 % to 90 % of the change over 2.2 of it)."""
    b, a, c, w = p
    return b + a * 0.5 * (1 + _np().tanh((u - c) / (abs(w) + 1e-12)))


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
    "double_lorentzian": (_double, ("background", "amplitude 1", "center 1", "FWHM 1", "amplitude 2", "center 2",
                                    "FWHM 2")),
    "step": (_step, ("level before", "change", "midpoint", "width")),
    "exponential": (_exponential, ("final value", "amplitude", "time constant")),
    "oscillation": (_oscillation, ("offset", "amplitude", "frequency", "phase", "decay time")),
}


def _scale(x: Any, y: Any) -> Tuple[float, float, float, float]:
    """(x center, x half-range, y level, y scale) the fits work in: data = level + scale * model(normalised x)."""
    np = _np()
    x0, x1 = float(x.min()), float(x.max())
    return (x0 + x1) / 2, (x1 - x0) / 2 or 1.0, float(y.mean()), float(np.ptp(y)) / 2 or 1.0


def fit(model: str, xs: Sequence[float], ys: Sequence[float], p0: Optional[Sequence[float]] = None,
        deadline: Optional[float] = None, db: bool = False) -> Optional[Dict[str, Any]]:
    """Fit `model` to (xs, ys). Parameters come back in data units with one-sigma errors. db: ys are the dB values of
    a line that is the model in linear power (a resonance on a VNA): it is fitted to 10^(dB/10), so its width is a
    linewidth; r2_db and rms_db say how it tells the curve as recorded, to weigh it against fits made on the dB
    values and against their noise."""
    np = _np()
    from .fitting import FitError, least_squares
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    n = x.size
    if n < 6:
        return None
    if model == "linear":
        return _linear(x, y)
    if model == "quadratic":
        return _quadratic(x, y)
    f, names = MODELS[model]
    x0, x1 = float(x.min()), float(x.max())
    data = 10 ** (y / 10) if db else y
    xc, xs_, yc, ys_ = _scale(x, data)
    us = (x - xc) / xs_
    vs = (data - yc) / ys_
    if p0 is not None:
        guess = list(p0)
    elif db:   # started from the curve smoothed in dB: a noise spike, ten times higher in power, misleads less
        from .expr import moving_average
        guess = _guess(model, us, (10 ** (moving_average(np, y, max(1, n // 50)) / 10) - yc) / ys_)
    else:
        guess = _guess(model, us, vs)
    if n <= len(guess):
        return None
    try:
        sol = least_squares(lambda p: f(us, p) - vs, guess, max_iter=300, deadline=deadline)
    except (FitError, ZeroDivisionError, OverflowError, ValueError, np.linalg.LinAlgError):
        return None
    p, chi = sol.p, sol.cost
    dof = max(1, n - len(p))
    errs = np.sqrt(np.clip(np.diag(sol.cov) * chi / dof, 0, None))
    if not np.all(np.isfinite(p)) or not np.all(np.isfinite(errs)):
        return None
    r2 = 1 - chi / (float((vs * vs).sum()) or 1e-300)
    extra = {}
    if db:   # the same fit, against the dB values it came from (none for a power below zero: no such line)
        level = yc + ys_ * f(us, p)
        with np.errstate(all="ignore"):
            shown = float(((y - 10 * np.log10(level)) ** 2).sum()) if level.min() > 0 else math.inf
        extra = {"r2_db": 1 - shown / (float(((y - y.mean()) ** 2).sum()) or 1e-300), "rms_db": math.sqrt(shown / n),
                 "physical": bool(level.min() > 0)}
    # back to data units
    if model == "step":   # the level after it, b + a: its error with the two's correlation (they share points)
        extra["after_error"] = math.sqrt(max(0.0, float(sol.cov[0][0] + sol.cov[1][1] + 2 * sol.cov[0][1]) * chi
                                             / dof)) * ys_
    if model in PEAKS or model == "step":
        b, a, c, w = p
        vals = [yc + b * ys_, a * ys_, xc + c * xs_, abs(w) * xs_]
        errv = [errs[0] * ys_, errs[1] * ys_, errs[2] * xs_, errs[3] * xs_]
    elif model == "double_lorentzian":
        b, a1, c1, w1, a2, c2, w2 = p
        vals = [yc + b * ys_, a1 * ys_, xc + c1 * xs_, abs(w1) * xs_, a2 * ys_, xc + c2 * xs_, abs(w2) * xs_]
        errv = [errs[0] * ys_, errs[1] * ys_, errs[2] * xs_, errs[3] * xs_, errs[4] * ys_, errs[5] * xs_,
                errs[6] * xs_]
    elif model == "oscillation":
        b, a, fr, ph, tau = p
        vals = [yc + b * ys_, abs(a) * ys_, abs(fr) / xs_, ph, abs(tau) * xs_]
        errv = [errs[0] * ys_, errs[1] * ys_, errs[2] / xs_, errs[3], errs[4] * xs_]
    else:
        b, a, tau = p
        vals = [yc + b * ys_, a * ys_, abs(tau) * xs_]
        errv = [errs[0] * ys_, errs[1] * ys_, errs[2] * xs_]
    grid = np.linspace(x0, x1, FIT_POINTS)
    curve = yc + ys_ * f((grid - xc) / xs_, p)   # in linear power for a line in dB
    rms = math.sqrt(chi / n) * ys_
    return dict({"model": model, "names": list(names), "values": [float(v) for v in vals],
                 "errors": [float(e) for e in errv], "r2": float(r2), "rms": rms, "on_power": db,
                 "curve": {"x": grid.tolist(), "y": curve.tolist()}, "_p": list(p), "_norm": (xc, xs_, yc, ys_)},
                **extra)


def _peak_guess(us: Any, vs: Any) -> List[float]:
    """(background, amplitude, center, FWHM) of the largest excursion from the edges' level."""
    np = _np()
    n = us.size
    edge = max(2, n // 7)
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


def _step_starts(us: Any, vs: Any) -> List[List[float]]:
    """Where to start a step (normalised): the levels of its two ends, the change halfway between them where the
    curve (lightly smoothed) crosses it, and a sharp, a middling and a broad width."""
    np = _np()
    from .expr import moving_average
    edge = max(2, us.size // 7)
    lo, hi = float(np.median(vs[:edge])), float(np.median(vs[-edge:]))
    smooth = moving_average(np, vs, max(1, us.size // 20))
    k = int(np.nanargmin(np.abs(smooth - (lo + hi) / 2)))
    return [[lo, hi - lo, float(us[k]), w] for w in (0.02, 0.1, 0.4)]


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
    if model == "step":
        return _step_starts(us, vs)[0]
    b, a, c, w = _peak_guess(us, vs)
    if model == "double_lorentzian":   # the second line where the first leaves most behind
        rest = vs - _lorentzian(us, [0.0, a, c, w])
        _, a2, c2, _ = _peak_guess(us, rest)
        return [b, a, c, w, a2, c2, w]
    return [b, a, c, w]


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


def _quadratic(x: Any, y: Any) -> Optional[Dict[str, Any]]:
    """A parabola, y = y_v + k (x - x_v)², by linear least squares: its vertex (x_v, y_v) and curvature k, each with
    its error from the coefficients' covariance."""
    np = _np()
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = x.size
    if n < 5:
        return None
    x0, x1 = float(x.min()), float(x.max())
    xc, xs_ = (x0 + x1) / 2, (x1 - x0) / 2 or 1.0
    u = (x - xc) / xs_
    a = np.column_stack([np.ones(n), u, u * u])
    coef, *_ = np.linalg.lstsq(a, y, rcond=None)
    c0, c1, c2 = (float(c) for c in coef)
    if c2 == 0:
        return None
    resid = y - a @ coef
    chi = float(resid @ resid)
    cov = np.linalg.pinv(a.T @ a) * chi / max(1, n - 3)
    grads = [np.array([0.0, -1 / (2 * c2), c1 / (2 * c2 * c2)]) * xs_,      # the vertex's x
             np.array([1.0, -c1 / (2 * c2), c1 * c1 / (4 * c2 * c2)]),       # the level there
             np.array([0.0, 0.0, 1 / xs_ ** 2])]                             # the curvature
    errs = [math.sqrt(max(float(g @ cov @ g), 0.0)) for g in grads]
    ss_tot = float(((y - y.mean()) ** 2).sum()) or 1e-300
    grid = np.linspace(x0, x1, FIT_POINTS)
    gu = (grid - xc) / xs_
    return {"model": "quadratic", "names": ["vertex", "level at the vertex", "curvature"],
            "values": [xc - c1 / (2 * c2) * xs_, c0 - c1 * c1 / (4 * c2), c2 / xs_ ** 2], "errors": errs,
            "r2": 1 - chi / ss_tot, "rms": math.sqrt(chi / n),
            "curve": {"x": grid.tolist(), "y": (c0 + c1 * gu + c2 * gu * gu).tolist()}}


def _refit(model: str, chosen: Dict[str, Any], x: Any, y: Any, deadline: Optional[float]) -> Dict[str, Any]:
    """The model a decimated curve showed, fitted to every point (started from where the decimated fit ended)."""
    if model == "linear":
        return _linear(x, y) or chosen
    if model == "quadratic":
        return _quadratic(x, y) or chosen
    db = bool(chosen.get("on_power"))
    nxc, nxs, nyc, nys = _scale(x, 10 ** (y / 10) if db else y)
    vals = chosen["values"]
    if model in PEAKS or model == "step":
        p0 = [(vals[0] - nyc) / nys, vals[1] / nys, (vals[2] - nxc) / nxs, vals[3] / nxs]
    elif model == "double_lorentzian":
        p0 = [(vals[0] - nyc) / nys, vals[1] / nys, (vals[2] - nxc) / nxs, vals[3] / nxs, vals[4] / nys,
              (vals[5] - nxc) / nxs, vals[6] / nxs]
    elif model == "oscillation":
        p0 = [(vals[0] - nyc) / nys, vals[1] / nys, vals[2] * nxs, chosen["_p"][3], vals[4] / nxs]
    else:
        p0 = [(vals[0] - nyc) / nys, vals[1] / nys, vals[2] / nxs]
    return fit(model, x, y, p0, deadline, db) or chosen


def _p_flat(f: Optional[Dict[str, Any]], n: int, trials: float = 1.0) -> float:
    """How often noise alone would let this fit beat a flat line by as much (an F test on its R²), times the
    places its feature could have been (look-elsewhere)."""
    from .fitting import f_sf
    k = len(f["values"]) if f else 0
    if not f or k < 2 or n <= k or not math.isfinite(f["r2"]) or f["r2"] <= 0:
        return 1.0
    if f["r2"] >= 1:
        return 0.0
    return min(1.0, f_sf((f["r2"] / (k - 1)) / ((1 - f["r2"]) / (n - k)), k - 1, n - k) * max(1.0, trials))


def _p_more(simple: Dict[str, Any], rich: Dict[str, Any], n: int) -> float:
    """How often noise alone would let the richer of two nested fits (two lines for one) improve this much."""
    from .fitting import f_sf
    k1, k2 = len(simple["values"]), len(rich["values"])
    rss1, rss2 = max(1 - simple["r2"], 0.0), max(1 - rich["r2"], 1e-300)
    if n <= k2 or rss1 <= rss2:
        return 1.0
    return f_sf(((rss1 - rss2) / (k2 - k1)) / (rss2 / (n - k2)), k2 - k1, n - k2)


def _turns(f: Dict[str, Any], noise: float) -> int:
    """An oscillation's turning points (maxima and minima) at least a quarter period inside both ends of the sweep,
    where its decayed amplitude still reaches the noise: a peak that fills the window has one."""
    b, a, fr, ph, tau = f["_p"]   # normalised: s runs from 0 to 2 over the sweep
    fr = abs(fr)
    if fr <= 0:
        return 0
    floor = noise / (f["_norm"][3] or 1.0)
    q = 0.25 / fr
    lo = math.ceil((2 * math.pi * fr * q + ph) / math.pi)
    hi = math.floor((2 * math.pi * fr * (2 - q) + ph) / math.pi)
    count = 0
    for k in range(lo, min(hi, lo + 10) + 1):
        s = (k * math.pi - ph) / (2 * math.pi * fr)
        if abs(a) * math.exp(-s / (abs(tau) + 1e-12)) >= floor:
            count += 1
    return count

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
                  deadline: Optional[float] = None, protocol: Optional[str] = None,
                  models: Optional[Sequence[str]] = None, tail: bool = False) -> Dict[str, Any]:
    """Classify one y(x) curve and quantify it. Returns kind, summary, results, fit, flags. force: fit this model
    whatever the curve shows; models: the only shapes to look for (one model for every row of a run); protocol: the
    pulse protocol the run played, which says what its sweep shows (EXPECT); tail: the curve after its first point
    (a decay's slow part, below)."""
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
    reach = float(xs_s[-1] - xs_s[0])   # the sweep's own range along x
    sigma = noise_estimate(ys_s)
    steps = np.abs(np.diff(xs_s))
    steps = steps[steps > 0]
    step = float(np.median(steps)) if steps.size else 0.0
    db = _db(y_unit)
    timed = x_unit == "s"
    expect, along = EXPECT.get(protocol or "", (None, None))
    expect = expect if along == x_unit else None

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
    quad = _quadratic(xs_s, ys_s) if force == "quadratic" or (force is None and models is None) else None

    def adequate(f: Optional[Dict[str, Any]], noise: float = sigma, m: int = n) -> bool:
        """The fit leaves only noise behind (residuals at the point-to-point noise level, which is itself
        only known to ~1/sqrt(n))."""
        return bool(f) and noise > 0 and f["rms"] <= noise * (1.25 + 2.5 / math.sqrt(m))

    def told(f: Optional[Dict[str, Any]]) -> bool:
        """A fit that leaves only noise behind on the values as recorded (dB for a line fitted in linear power)."""
        return bool(f) and adequate({"rms": f.get("rms_db", f["rms"])}, sigma, min(n, SELECT_POINTS))

    def constant(noise: float, maybe: str = "") -> Dict[str, Any]:
        """No systematic dependence on x: a steady value, or no signal at all. maybe: a line too weak to call one,
        said as such (then never "does not depend")."""
        mean = float(ys_s.mean())
        depends = "shows no clear dependence on " if maybe else "does not depend on "
        if abs(mean) > 5 * noise and not db:
            out["kind"] = "steady"
            out["summary"] = "%s %s: %s (fluctuations %s rms)" % (
                y_name, "is stable over " + si(float(xs_s[-1] - xs_s[0]), x_unit) if x_name == "time" else
                depends + x_name, _fmt_pm(mean, noise / math.sqrt(n), y_unit), si(noise, y_unit))
        elif db:
            out["kind"] = "steady"
            out["summary"] = "%s %s%s: %s (fluctuations %s rms)" % (
                y_name, depends, x_name, _fmt_pm(mean, noise / math.sqrt(n), y_unit), si(noise, y_unit))
        else:
            out["kind"] = "flat"
            out["summary"] = "%s shows no signal above the noise (%s rms)" % (y_name, si(noise, y_unit))
        if maybe:
            out["flags"].append(maybe)
        res("mean", mean, noise / math.sqrt(n), y_unit)
        res("noise (rms)", noise, None, y_unit)
        if lin:
            res("drift (slope)", lin["values"][1], lin["errors"][1], "")
            out["results"][-1]["value"] += " %s/%s" % (y_unit or "1", x_unit or "1")
            out["results"][-1]["unit"] = "%s/%s" % (y_unit or "1", x_unit or "1")
        return out

    if force is None and (span == 0 or (sigma > 0 and span < 5 * sigma)):
        return constant(sigma or _mad_sigma(ys_s))
    # no trend a line can see: only a line (or two), or what the protocol looks for, could still stand out
    level = force is None and adequate(lin) and abs(lin["values"][1]) < 3 * lin["errors"][1]

    # a long curve: its model chosen on a decimated copy, then fitted to every point
    xd, yd = _decimate(np, xs_s, ys_s, SELECT_POINTS)
    # a resonance in dB is a Lorentzian in linear power, not in dB: fitted to 10^(dB/10) (fit(db=True))
    if force:
        order = [force]
    elif models is not None:
        order = list(models)
    elif expect == "oscillation":   # its minima are no pair of lines
        order = ["oscillation", "lorentzian", "gaussian", "exponential"]
    elif expect == "decay":
        order = ["exponential", "oscillation", "lorentzian", "gaussian"]
    elif level or expect == "lines":
        order = ["lorentzian", "gaussian", "double_lorentzian"]
    else:
        order = ["lorentzian", "gaussian", "step", "exponential", "oscillation", "double_lorentzian"]
    candidates: Dict[str, Dict[str, Any]] = {"linear": lin} if lin else {}
    if quad:
        candidates["quadratic"] = quad
    for model in order:
        if model in candidates or model not in MODELS:
            continue
        if deadline is not None and time.monotonic() > deadline:
            out["flags"].append("the analysis ran out of time before trying every model")
            break
        on_power = db and model in LINES
        if model == "double_lorentzian" and not force:   # two lines only where one doesn't tell the curve well
            single = max((candidates[m] for m in PEAKS if m in candidates), key=lambda f: f["r2"], default=None)
            if n < 12 or single is None or (single["r2"] >= 0.9 and told(single)):
                continue
            starts = _double_starts(np, single, xd, yd)
        elif model == "step":   # a sharp, a middling and a broad start: a sharp one has no slope to follow
            xc, xs_, yc, ys_ = _scale(xd, yd)
            starts = _step_starts((xd - xc) / xs_, (yd - yc) / ys_)
        else:
            starts = [None]
        best = None
        for p0 in starts:
            f = fit(model, xd, yd, p0, deadline, on_power)
            if f and math.isfinite(f["r2"]) and (best is None or f["r2"] > best["r2"]):
                best = f
        if best:
            candidates[model] = best

    def trials(w: float) -> float:
        return max(2.0, reach / max(w, 1e-300))

    def r2_shown(f: Dict[str, Any]) -> float:   # R² on the values as recorded (dB too): comparable across models
        return f.get("r2_db", f["r2"])

    def stands_out(a: float, ea: float, w: float) -> bool:
        """A peak or dip, not noise: more than one point wide, and its height above what noise reaches somewhere
        along a sweep of this many line widths (look-elsewhere)."""
        if a == 0 or not ea > 0 or (step and w < step):
            return False
        return abs(a) / ea >= 3 + math.sqrt(2 * math.log(trials(w)))

    def inside(c: float) -> bool:
        """Within the sweep, with a measured point on each side of the one nearest it."""
        return xs_s[0] < c < xs_s[-1] and 1 <= int(np.argmin(np.abs(xs_s - c))) <= n - 2

    def physical(f: Dict[str, Any]) -> bool:
        """A line fitted in linear power has power above zero (a negative background is no line, and its height in
        "dB" would be linear values)."""
        return not f.get("on_power") or bool(f.get("physical") and f["values"][0] > 0)

    def near(f: Dict[str, Any]) -> bool:
        """Most of the curve told, and little more than noise left (twice it at most, on the values as recorded):
        a line shape a little off at moderate signal (a resonance in dB, fitted in power)."""
        return f["r2"] >= 0.9 and f.get("rms_db", f["rms"]) <= 2 * sigma

    def peak_ok(f: Optional[Dict[str, Any]]) -> bool:
        """One line inside the sweep that tells the curve: nearly all of it (a line shape a little off at high
        signal), or with its residuals near the noise. Half a line beside a step, or one across a staircase, leaves
        far more than noise. Its width is weighed after the shape that tells the curve best is chosen (lines(), and
        wide below)."""
        if not f or not physical(f):
            return False
        b, a, c, w = f["values"]
        good = f["r2"] >= 0.98 or near(f) or (told(f) and r2_shown(f) >= 0.75 and abs(a) > 5 * f["errors"][1])
        return good and w > 0 and inside(c) and stands_out(a, f["errors"][1], w)

    def double_ok(f: Optional[Dict[str, Any]]) -> bool:
        """Two lines of one kind (two dips or two peaks), each standing out inside the sweep, with a valley between
        them (a tenth of the lower one: else it is one flat-topped line), and better than one line by more than
        noise does."""
        if not f or not physical(f):
            return False
        b, a1, c1, w1, a2, c2, w2 = f["values"]
        e = f["errors"]
        each = all(0 < w <= reach and inside(c) and stands_out(a, ea, w)
                   for a, ea, c, w in ((a1, e[1], c1, w1), (a2, e[4], c2, w2)))
        single = candidates.get("lorentzian")
        better = single is None or _p_more(single, f, len(yd)) * trials(min(w1, w2)) < 1e-3
        good = f["r2"] >= 0.99 or near(f) or told(f)
        return each and (a1 > 0) == (a2 > 0) and _valley(f) >= 0.1 and better and good

    def osc_ok(f: Optional[Dict[str, Any]]) -> bool:
        """At least one cycle, with its turning points well inside the sweep (three, or two where the protocol
        says it oscillates), decaying no faster than a third of a period, and more than noise would make."""
        if not f:
            return False
        b, a, fr, ph, tau = f["values"]
        if not (fr * reach >= 1.0 and a > 4 * f["errors"][1] and tau > 0.3 / max(fr, 1e-300)):
            return False
        quality = f["r2"] >= 0.8 or adequate(f, sigma, len(yd))
        return quality and _p_flat(f, len(yd), len(yd) / 4) < 1e-3 and \
            _turns(f, sigma) >= (2 if expect == "oscillation" else 3)

    def expo_ok(f: Optional[Dict[str, Any]]) -> bool:
        """A decay whose time the points pin down: longer than half a step, and known to better than a third."""
        if not f:
            return False
        b, a, tau = f["values"]
        known = tau > 3 * f["errors"][2] and abs(a) > 5 * f["errors"][1] and (not step or tau >= 0.5 * step)
        good = f["r2"] >= 0.98 or adequate(f, sigma, len(yd)) or (expect == "decay" and f["r2"] >= 0.9)
        return known and good and (not lin or f["r2"] > lin["r2"] + 0.01)

    def step_ok(f: Optional[Dict[str, Any]]) -> bool:
        """A step: its change stands out, its rise (10 % to 90 %) lies inside the sweep with points on both levels and
        takes at most half of it (a broader one is a curve), and the fit leaves only noise behind."""
        if not f:
            return False
        b, a, c, w = f["values"]
        rise = RISE * w
        k = int(np.argmin(np.abs(xs_s - c)))
        return abs(a) > 5 * f["errors"][1] and abs(a) > 5 * sigma and 3 <= k <= n - 4 and rise <= reach / 2 and \
            xs_s[0] + rise / 2 <= c <= xs_s[-1] - rise / 2 and (told(f) or f["r2"] >= 0.995)

    def quad_ok(q: Optional[Dict[str, Any]]) -> bool:
        """A bend a straight line misses: the parabola leaves only noise behind, and its curvature stands out."""
        return bool(q) and abs(q["values"][2]) > 5 * q["errors"][2] and (q["r2"] >= 0.99 or adequate(q)) and \
            not (lin and adequate(lin))

    def lines() -> Optional[Dict[str, Any]]:
        """The line shape that tells the curve best (a Gaussian fits a broad Lorentzian narrower, never instead of
        it), or two lines."""
        best = max((candidates[m] for m in PEAKS if m in candidates and physical(candidates[m])),
                   key=lambda f: f["r2"], default=None)
        double = candidates.get("double_lorentzian")
        return double if double_ok(double) else best if peak_ok(best) else None

    def possible() -> str:
        """The strongest line short of standing out (4σ and more): worth a longer look, though not a finding."""
        weak = [(abs(f["values"][1]) / f["errors"][1], f) for f in (candidates.get(m) for m in PEAKS)
                if f and f["errors"][1] > 0 and inside(f["values"][2]) and (not step or f["values"][3] >= step)
                and f["values"][3] <= reach]
        z, f = max(weak, key=lambda zf: zf[0], default=(0.0, None))
        osc = candidates.get("oscillation")
        if expect == "oscillation" and osc and osc["errors"][1] > 0 and osc["values"][1] >= 3 * osc["errors"][1] \
                and osc["values"][2] * reach >= 1 and _turns(osc, sigma) >= 2:   # what the protocol looks for
            return "a possible oscillation at %s (%.1fσ): average longer to tell" % (
                _fmt_pm(osc["values"][2], osc["errors"][2], "Hz" if timed else "1/" + (x_unit or "1")),
                osc["values"][1] / osc["errors"][1])
        if f is None or z < 4:
            return ""
        reach_z = 3 + math.sqrt(2 * math.log(trials(f["values"][3])))
        why = "short of the %.1fσ noise alone reaches somewhere along this sweep" % reach_z if z < reach_z else \
            "but it holds only %.0f%% of the curve's variation" % max(0.0, 100 * r2_shown(f))
        return "a possible %s at %s = %s (%.1fσ, %s): average longer, or sweep finer around it, to tell" % (
            "peak" if f["values"][1] > 0 else "dip", x_name, _fmt_pm(f["values"][2], f["errors"][2], x_unit), z, why)

    osc, expo = candidates.get("oscillation"), candidates.get("exponential")
    chosen = None
    if force:
        chosen = candidates.get(force)
    elif expect == "oscillation" and osc_ok(osc):   # what the protocol looks for comes first
        chosen = osc
    elif expect == "decay" and expo_ok(expo):
        chosen = expo
    elif level:
        chosen = lines()
        if chosen is None:
            return constant(sigma or _mad_sigma(ys_s), possible())
    elif lin and (lin["r2"] >= 0.995 or adequate(lin)):
        chosen = lin
    if chosen is None and not force:
        line = lines()
        shown = r2_shown(line) if line else 0.0
        stepped = candidates.get("step")
        if step_ok(stepped) and (line is None or r2_shown(stepped) >= shown):
            chosen = stepped   # a step, not half a line beside it (or two)
        elif line is not None and (told(line) or not osc_ok(osc) or osc["r2"] <= shown + 0.03):
            chosen = line   # one extremum fitted well is a peak, not a cycle of a cosine
        elif osc_ok(osc) and osc["r2"] > max(shown, expo["r2"] if expo else 0.0) + 0.03:
            chosen = osc
        if chosen is not None and chosen["model"] in PEAKS and chosen["values"][3] > reach / 2 and quad_ok(quad):
            chosen = quad   # a broad top a parabola tells as well: the simpler shape
        if chosen is None and expo_ok(expo):
            chosen = expo
        if chosen is None and quad_ok(quad):
            chosen = quad
        if chosen is None and lin and lin["r2"] >= 0.9:
            chosen = lin
            if lin["r2"] < 0.98:
                out["flags"].append("%s deviates from a straight line beyond the noise (R² %.3f): look for curvature, "
                                    "steps or settling transients" % (y_name, lin["r2"]))

    best_peak = max((candidates[m] for m in PEAKS if m in candidates), key=lambda f: f["r2"], default=None)
    if chosen is None and best_peak and step and best_peak["values"][3] < step and \
            inside(best_peak["values"][2]) and _height(best_peak) > 5 * max(sigma, 1e-300):
        # one point apart from the rest: a glitch, or a feature narrower than the step (not a fitted peak)
        k = int(np.argmax(np.abs(ys_s - np.median(ys_s))))
        out["flags"].append("one point stands out: %s = %s at %s = %s, narrower than the step %s (a glitch, or a "
                            "feature finer than the sweep: refine around it to tell)"
                            % (y_name, si(float(ys_s[k]), y_unit), x_name, si(float(xs_s[k]), x_unit),
                               si(step, x_unit)))

    if chosen is None and force is None and lin and abs(lin["values"][1]) < 3 * lin["errors"][1] \
            and max(f["r2"] for m, f in candidates.items() if m not in ("double_lorentzian", "step")) < 0.75:
        # slow fluctuations (correlated noise) around a constant: no model explains them well
        constant(float(ys_s.std(ddof=1)), possible())
        mid, spread = float(np.median(ys_s)), _mad_sigma(ys_s)
        k = int(np.argmax(np.abs(ys_s - mid)))
        if spread > 0 and abs(float(ys_s[k]) - mid) > 5 * spread:
            out["flags"].append("one excursion to %s at %s = %s (%.0f× the typical spread)"
                                % (si(float(ys_s[k]), y_unit), x_name, si(float(xs_s[k]), x_unit),
                                   abs(float(ys_s[k]) - mid) / spread))
        return out

    if chosen is None and force is None and models is None and not tail and n >= 8:
        # a decay whose fast part is over by the second point (photoluminescence at time zero): that first point
        # is kept and said, and the slow part told from the rest
        rest = ys_s[1:]
        spread, noise = float(np.ptp(rest)), noise_estimate(rest)
        if abs(ys_s[0] - (2 * rest[0] - rest[1])) > 10 * max(noise, 0.02 * spread, 1e-300) and \
                abs(ys_s[0] - float(np.median(rest))) > 3 * max(spread, noise, 1e-300):
            slow = analyze_curve(xs_s[1:], rest, x_name, y_name, x_unit, y_unit, None, deadline, protocol, None, True)
            if slow["kind"] == "relaxation":
                falls = bool(ys_s[0] > rest[0])
                slow["summary"] = "%s %s from %s at %s = %s to %s within one step (%s), then %s" % (
                    y_name, "drops" if falls else "rises", si(float(ys_s[0]), y_unit), x_name,
                    si(float(xs_s[0]), x_unit), si(float(rest[0]), y_unit), si(step, x_unit),
                    slow["summary"][len(y_name) + 1:])
                slow["results"].insert(0, {"name": "first point", "value": si(float(ys_s[0]), y_unit),
                                           **_numeric(float(ys_s[0]), None, y_unit)})
                slow["flags"].append("part of the %s is faster than the %s step: a finer step near %s = %s "
                                     "would show it" % ("decay" if falls else "rise", si(step, x_unit), x_name,
                                                        si(float(xs_s[0]), x_unit)))
                return slow

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
        k_ext = int(np.argmax(np.abs(ys_s - np.median(ys_s))))
        if k_ext in (0, n - 1) and abs(float(ys_s[k_ext]) - float(np.median(ys_s))) > 5 * max(sigma, 1e-300):
            out["flags"].append("the largest change is at the edge of the sweep; the feature may extend beyond %s"
                                % si(float(xs_s[k_ext]), x_unit))
        return out

    on_power = bool(chosen.get("on_power"))
    if xd.size < n and chosen["model"] not in ("linear", "quadratic"):   # fitted to every point now
        chosen = _refit(chosen["model"], chosen, xs_s, ys_s, deadline)
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
    elif chosen["model"] == "quadratic":
        _bend(out, res, chosen, xs_s, x_name, y_name, x_unit, y_unit, sigma)
    elif chosen["model"] == "step":
        _step_said(out, res, chosen, step, x_name, y_name, x_unit, y_unit)
    elif chosen["model"] in PEAKS and chosen["values"][3] > reach:   # its width would come from the shape assumed
        _wide_line(out, res, chosen, ys_s, x_name, y_name, x_unit, y_unit, reach)
    elif chosen["model"] in PEAKS:
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
            level_db = 10 * math.log10(b)
            edge = (b + a) / b
            if edge > 0:
                change = 10 * math.log10(edge)
                res("height" if a > 0 else "depth", abs(change), abs(10 / math.log(10) * ea / (b + a)), "dB")
            res("background", level_db, 10 / math.log(10) * eb / b, y_unit)
            out["params"] = {"center": [c, ec, x_unit], width: [w, ew, x_unit], "background": [level_db, None, y_unit]}
        else:   # (a line in dB forced on a power below zero: its values stay linear, never labelled dB)
            unit = "" if on_power else y_unit
            res("height" if a > 0 else "depth", abs(a), ea, unit)
            res("background", b, eb, unit)
            out["params"] = {"center": [c, ec, x_unit], "FWHM": [w, ew, x_unit],
                             ("height" if a > 0 else "depth"): [abs(a), ea, unit], "background": [b, eb, unit]}
        if x_unit == "Hz" and w > 0:
            q = c / w
            qe = q * math.sqrt((ec / c) ** 2 + (ew / w) ** 2) if c else None
            res("Q loaded (center/FWHM of the linear power)" if on_power else "Q (center/FWHM)", q, qe, "")
            out["summary"] += ", Q ≈ %s" % _fmt_pm(q, qe, "")
            out["params"]["Q"] = [q, qe, ""]
        elif db and not on_power:
            out["flags"].append("the width was fitted on dB values: it is the width of the dB curve, not a linewidth")
        if sigma > 0 and not on_power:
            res("signal / noise", abs(a) / sigma, None, "")
        _width_flags(out, [(c, w)], step, xs_s, x_unit)
    elif chosen["model"] == "double_lorentzian":
        _two_lines(out, res, chosen, on_power, x_name, y_name, x_unit, y_unit)
        _width_flags(out, [(chosen["values"][2], chosen["values"][3]), (chosen["values"][5], chosen["values"][6])],
                     step, xs_s, x_unit)
    elif chosen["model"] == "oscillation":
        b, a, fr, ph, tau = v
        eb, ea, ef, eph, etau = e
        out["kind"] = "oscillation"
        per = "Hz" if timed else "1/" + (x_unit or "1")
        decays = 2 * etau < tau < 5 * reach   # a decay the points pin down
        out["summary"] = "%s oscillates with %s at %s (period %s)" % (
            y_name, x_name, _fmt_pm(fr, ef, per), si(1 / fr, x_unit))
        if decays:
            out["summary"] += (", decaying with τ = %s" if timed else ", falling by 1/e over %s") % \
                _fmt_pm(tau, etau, x_unit)
        out["summary"] += " (R² %.3f)" % chosen["r2"]
        res("frequency", fr, ef, per)
        res("period", 1 / fr, ef / fr ** 2, x_unit)
        if timed:   # driven spins: half a period is the pi pulse
            res("half period (π time)" if protocol == "rabi" else "half period", 0.5 / fr, 0.5 * ef / fr ** 2, x_unit)
            if protocol == "rabi":
                out["summary"] += "; half period (π time) %s" % _fmt_pm(0.5 / fr, 0.5 * ef / fr ** 2, x_unit)
        if decays:
            res("decay time τ" if timed else "decay (1/e) over", tau, etau, x_unit)
        res("amplitude", a, ea, y_unit)
        res("offset", b, eb, y_unit)
        out["params"] = {"frequency": [fr, ef, per], "amplitude": [a, ea, y_unit]}
        if decays:
            out["params"]["τ"] = [tau, etau, x_unit]
        if step and step > 0.25 / fr:
            out["flags"].append("fewer than 4 points per period; use a step ≤ %s" % si(0.125 / fr, x_unit))
    else:
        b, a, tau = v
        out["kind"] = "relaxation"
        name = {"t1": " (T1)", "hahn_echo": " (T2)"}.get(protocol or "", "") if timed else ""
        if timed:
            out["summary"] = "%s relaxes exponentially toward %s with τ = %s%s (R² %.3f)" % (
                y_name, _fmt_pm(b, e[0], y_unit), _fmt_pm(tau, e[2], x_unit), name, chosen["r2"])
        else:
            out["summary"] = "%s %s exponentially with %s toward %s, by 1/e over %s (R² %.3f)" % (
                y_name, "falls" if a > 0 else "rises", x_name, _fmt_pm(b, e[0], y_unit), _fmt_pm(tau, e[2], x_unit),
                chosen["r2"])
        res(("time constant τ" + name) if timed else "1/e over", tau, e[2], x_unit)
        res("final value", b, e[0], y_unit)
        res("amplitude", a, e[1], y_unit)
        out["params"] = {"τ": [tau, e[2], x_unit], "final value": [b, e[0], y_unit]}
        if xs_s[-1] - xs_s[0] < 3 * tau:
            out["flags"].append("the %s covers only %.1f τ; measure ≥ 5 τ to pin down the final value"
                                % ("record" if timed else "sweep", (xs_s[-1] - xs_s[0]) / tau))
    out["noise"] = chosen["rms"]
    return out


def _step_said(out: Dict[str, Any], res: Callable[..., None], f: Dict[str, Any], spacing: float, x_name: str,
               y_name: str, x_unit: str, y_unit: str) -> None:
    """A step (a superconducting transition, a switch): from what level to what, where (its midpoint) and over how
    much of x it rises (10 % to 90 %); one sharper than the points can follow is placed within a step of x."""
    b, a, c, w = f["values"]
    eb, ea, ec, ew = f["errors"]
    rise, erise = RISE * w, RISE * ew
    sharp = bool(spacing) and rise < spacing
    if sharp:   # between two points: no fit says better where (its own error is then of any size)
        ec = spacing / 2
    out["kind"] = "step"
    over = "sharper than the %s step between points" % si(spacing, x_unit) if sharp else \
        "10–90%% over %s" % _fmt_pm(rise, erise, x_unit)
    out["summary"] = "%s steps %s from %s to %s at %s = %s (%s; tanh fit, R² %.3f)" % (
        y_name, "up" if a > 0 else "down", si(b, y_unit), si(b + a, y_unit), x_name, _fmt_pm(c, ec, x_unit), over,
        f["r2"])
    res("midpoint", c, ec, x_unit)
    res("from", b, eb, y_unit)
    res("to", b + a, f.get("after_error", math.hypot(eb, ea)), y_unit)
    out["params"] = {"center": [c, ec, x_unit], "change": [a, ea, y_unit]}
    if not sharp:
        res("rise (10–90%)", rise, erise, x_unit)
        out["params"]["rise"] = [rise, erise, x_unit]
    if spacing and rise < 2 * spacing:
        out["flags"].append("the step is under-resolved (its rise is under 2 steps); use a step ≤ %s between %s and %s"
                            % (si(spacing / 4, x_unit), _at(c - 3 * spacing, x_unit, spacing / 4),
                               _at(c + 3 * spacing, x_unit, spacing / 4)))


def _wide_line(out: Dict[str, Any], res: Callable[..., None], f: Dict[str, Any], ys: Any, x_name: str, y_name: str,
               x_unit: str, y_unit: str, reach: float) -> None:
    """A line wider than the sweep: where it is, but no width or Q. Those would come from the shape assumed, not from
    the points (a Gaussian fits a Lorentzian as wide as the sweep 0.7 times as wide, and a Q 1.4 times too high)."""
    b, a, c, w = f["values"]
    out["kind"] = "peak" if a > 0 else "dip"
    out["summary"] = "%s has a %s at %s = %s, wider than the sweep: its FWHM%s a wider one (%s fit%s, R² %.3f)" \
        % (y_name, out["kind"], x_name, _fmt_pm(c, f["errors"][2], x_unit),
           " and Q need" if x_unit == "Hz" else " needs", f["model"], " on 10^(dB/10)" if f.get("on_power") else "",
           f["r2"])
    res("center", c, f["errors"][2], x_unit)
    res("maximum" if a > 0 else "minimum", float(ys.max() if a > 0 else ys.min()), None, y_unit)
    out["params"] = {"center": [c, f["errors"][2], x_unit]}
    out["flags"].append("the %s is wider than the %s sweep: widen the range to measure its width%s" % (
        out["kind"], si(reach, x_unit), " and Q" if x_unit == "Hz" else ""))


def _valley(f: Dict[str, Any]) -> float:
    """How deep two lines dip between their centers, as a share of the lower one: about 0 where they merge into one
    (a flat-topped line fitted as two)."""
    np = _np()
    b, a1, c1, w1, a2, c2, w2 = f["_p"]
    sign = 1.0 if a1 > 0 else -1.0
    between = sign * (_double(np.linspace(min(c1, c2), max(c1, c2), 101), f["_p"]) - b)
    low = float((sign * (_double(np.array([c1, c2]), f["_p"]) - b)).min())
    return (low - float(between.min())) / low if low > 0 else 0.0


def _height(f: Dict[str, Any]) -> float:
    """A line's height in the curve's own units (dB for a line in dB, fitted in linear power)."""
    b, a = f["values"][0], f["values"][1]
    if not f.get("on_power"):
        return abs(a)
    return abs(10 * math.log10((b + a) / b)) if b > 0 and b + a > 0 else math.inf


def _double_starts(np: Any, single: Dict[str, Any], x: Any, y: Any) -> List[List[float]]:
    """Where to start two lines from the best one (normalised as fit() will): the second where the one leaves most
    behind, or the one split in two (a doublet fitted as one broad line)."""
    xc, xs_, yc, ys_ = single["_norm"]
    b, a, c, w = single["_p"]
    w = abs(w)
    us, vs = (x - xc) / xs_, ((10 ** (y / 10) if single.get("on_power") else y) - yc) / ys_
    rest = vs - b - a / (1 + ((us - c) / (w / 2 + 1e-12)) ** 2)
    k = int(np.argmax(np.abs(rest)))
    return [[b, a, c, w, float(rest[k]), float(us[k]), w],
            [b, 0.8 * a, c - w / 4, w / 2, 0.8 * a, c + w / 4, w / 2]]


def _width_flags(out: Dict[str, Any], lines: Sequence[Tuple[float, float]], step: float, xs: Any,
                 x_unit: str) -> None:
    """A line narrower than a few steps (with where to refine, in digits that tell the ends apart), or one that
    reaches past the sweep."""
    for c, w in lines:
        if step and w < 4 * step:
            out["flags"].append("the %s is under-resolved (FWHM %s ≈ %.1f steps); use a step ≤ %s between %s and %s"
                                % (out["kind"], si(w, x_unit), w / step, si(w / 5, x_unit),
                                   _at(c - 2 * w, x_unit, w / 5), _at(c + 2 * w, x_unit, w / 5)))
            break
    if any(c - w / 2 < xs[0] or c + w / 2 > xs[-1] for c, w in lines):
        out["flags"].append("the %s reaches the edge of the sweep; widen the range to see its full shape" % out["kind"])


def _two_lines(out: Dict[str, Any], res: Callable[..., None], f: Dict[str, Any], on_power: bool, x_name: str,
               y_name: str, x_unit: str, y_unit: str) -> None:
    """Two lines: where each is, how far apart, how wide and deep."""
    v, e = f["values"], f["errors"]
    b, eb = v[0], e[0]
    (c1, ec1, w1, ew1, a1, ea1), (c2, ec2, w2, ew2, a2, ea2) = sorted(
        [(v[2], e[2], v[3], e[3], v[1], e[1]), (v[5], e[5], v[6], e[6], v[4], e[4])])
    out["kind"] = "peak" if a1 > 0 else "dip"
    out["lines"] = 2
    mid, emid = (c1 + c2) / 2, 0.5 * math.hypot(ec1, ec2)
    gap, egap = c2 - c1, math.hypot(ec1, ec2)
    width = "FWHM of the linear power" if on_power else "FWHM"
    out["summary"] = "%s has two %ss, at %s = %s and %s (%s apart, centred at %s; %s %s and %s; two Lorentzians%s, " \
                     "R² %.3f)" % (y_name, out["kind"], x_name, _fmt_pm(c1, ec1, x_unit), _fmt_pm(c2, ec2, x_unit),
                                   _fmt_pm(gap, egap, x_unit), _fmt_pm(mid, emid, x_unit), width, si(w1, x_unit),
                                   si(w2, x_unit), " on 10^(dB/10)" if on_power else "", f["r2"])
    depth = "height" if a1 > 0 else "depth"
    linear = "" if on_power else y_unit   # a power below zero (forced) stays in linear values, never labelled dB
    for k, (c, ec, w, ew, a, ea) in enumerate(((c1, ec1, w1, ew1, a1, ea1), (c2, ec2, w2, ew2, a2, ea2)), 1):
        res("center %d" % k, c, ec, x_unit)
        res("%s %d" % (width, k), w, ew, x_unit)
        if on_power and b > 0 and (b + a) / b > 0:
            res("%s %d" % (depth, k), abs(10 * math.log10((b + a) / b)), abs(10 / math.log(10) * ea / (b + a)), "dB")
        else:
            res("%s %d" % (depth, k), abs(a), ea, linear)
    res("splitting", gap, egap, x_unit)
    if on_power and b > 0:
        res("background", 10 * math.log10(b), 10 / math.log(10) * eb / b, y_unit)
    else:
        res("background", b, eb, linear)
    out["params"] = {"center": [mid, emid, x_unit], "splitting": [gap, egap, x_unit], width + " 1": [w1, ew1, x_unit],
                     width + " 2": [w2, ew2, x_unit]}


def _bend(out: Dict[str, Any], res: Callable[..., None], q: Dict[str, Any], xs: Any, x_name: str, y_name: str,
          x_unit: str, y_unit: str, noise: float) -> None:
    """A parabola: where it turns and from what level, and how much it changes across the sweep (for a
    magnetoresistance: rises as B², +125 % at 2.5 T)."""
    (xv, yv, k), (exv, eyv, ek) = q["values"], q["errors"]
    lo, hi = float(xs[0]), float(xs[-1])
    far = lo if abs(lo - xv) > abs(hi - xv) else hi
    at_far = yv + k * (far - xv) ** 2
    out["kind"] = "quadratic"
    turn = "minimum" if k > 0 else "maximum"
    grows = "rises" if k > 0 else "falls"
    relative = abs(yv) > 5 * max(noise, 1e-300) and not _db(y_unit)   # a share of a level in dB means nothing
    change = "%+.0f%% at %s = %s" % (100 * (at_far - yv) / abs(yv), x_name, si(far, x_unit)) if relative else \
        "%s at %s = %s" % (si(at_far, y_unit), x_name, si(far, x_unit))
    if lo < xv < hi:
        if abs(xv) < max(3 * exv, 0.01 * (hi - lo)):   # turning at 0: even in x (a magnetoresistance in B)
            out["summary"] = "%s %s as %s² from %s at %s = 0: %s (quadratic fit, R² %.4f)" % (
                y_name, grows, x_name, si(yv, y_unit), x_name, change, q["r2"])
        else:
            out["summary"] = "%s %s quadratically on both sides of a %s of %s at %s = %s: %s (quadratic fit, " \
                             "R² %.4f)" % (y_name, grows, turn, si(yv, y_unit), x_name, _fmt_pm(xv, exv, x_unit),
                                           change, q["r2"])
        res(turn, yv, eyv, y_unit)
        res("at " + x_name, xv, exv, x_unit)
    else:
        rising = (k > 0) == (lo > xv)   # past a minimum, or before a maximum
        out["summary"] = "%s %s with %s from %s to %s, curving (quadratic fit, R² %.4f)" % (
            y_name, "rises" if rising else "falls", x_name, si(yv + k * (lo - xv) ** 2, y_unit),
            si(yv + k * (hi - xv) ** 2, y_unit), q["r2"])
    curvature = "%s/%s²" % (y_unit or "1", x_unit or "1")
    res("curvature", k, ek, "")
    out["results"][-1]["value"] += " " + curvature
    out["results"][-1]["unit"] = curvature
    if relative:
        res("change at %s = %s" % (x_name, si(far, x_unit)), 100 * (at_far - yv) / abs(yv), None, "%")
    out["params"] = {"curvature": [k, ek, curvature], "vertex": [xv, exv, x_unit], turn: [yv, eyv, y_unit]}


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


def _profiles(np: Any, outs: Any, ins: Any, z: Any, x_axis: Dict[str, str], y_axis: Dict[str, str],
              measure: Dict[str, str], deadline: Optional[float]) -> List[str]:
    """How a map varies along each axis, averaged over the other: the profiles that show more than noise, each in
    the words of its own analysis ("along magnet.B it rises as magnet.B²...")."""
    out = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)   # a row or column with no numbers averages to NaN
        means = ((x_axis, y_axis, ins, np.nanmean(z, axis=0)), (y_axis, x_axis, outs, np.nanmean(z, axis=1)))
    for along, over, values, profile in means:
        s = analyze_curve(list(values), profile.tolist(), along["key"], measure["key"], along.get("unit", ""),
                          measure.get("unit", ""), deadline=deadline)
        if s["kind"] in ("flat", "steady", "none"):
            continue
        text = s["summary"]
        text = text[len(measure["key"]) + 1:] if text.startswith(measure["key"] + " ") else text
        out.append("along %s (averaged over %s) it %s" % (along["key"], over["key"], text))
    return out


def analyze_map(outs: Sequence[float], ins: Sequence[float], z: Any, x_axis: Dict[str, str], y_axis: Dict[str, str],
                measure: Dict[str, str], deadline: Optional[float] = None) -> Dict[str, Any]:
    """A measured value over two swept axes, looked at as an image (x: the inner axis, y: the outer). Spots are
    places a 3 x 3 neighbourhood stands out from the map's own noise by more than noise alone would reach anywhere in
    a map of this many points (look-elsewhere); the brightest is fitted with a 2-D Gaussian. A "spot" wider than the
    map is the map's trend, told along each axis instead."""
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

    def structured() -> Dict[str, Any]:
        """No localised spot: what the map does along each axis, else its largest and lowest place."""
        k = np.unravel_index(int(np.nanargmax(np.abs(np.where(finite, z - bg, np.nan)))), z.shape)
        out["kind"] = "structured map"
        along = _profiles(np, outs_a, ins_a, z, x_axis, y_axis, measure, deadline)
        if along:
            out["summary"] = "%s varies across the map without a localised spot: %s" % (zk, "; ".join(along))
        else:
            out["summary"] = ("%s varies across the map without a localised spot: %s at %s = %s, %s = %s; range %s "
                              "to %s" % (zk, "largest" if z[k] >= bg else "lowest", xk, si(xi(k[1]), xu), yk,
                                         si(yi(k[0]), yu), si(float(np.nanmin(z)), zu), si(float(np.nanmax(z)), zu)))
        res("lowest", float(np.nanmin(z)), None, zu)
        res("largest", float(np.nanmax(z)), None, zu)
        return out
    if not spots:
        yy, xx = np.nonzero(finite)
        a = np.column_stack([xx, yy, np.ones_like(xx)]).astype(float)
        coef, *_ = np.linalg.lstsq(a, z[finite], rcond=None)
        rest = z[finite] - a @ coef
        biggest = float(t.max())
        if float(rest.std()) < 2 * sigma and abs(coef[0] * w) + abs(coef[1] * h) < 5 * sigma:
            out["kind"] = "flat"
            out["summary"] = ("%s shows no feature above the noise across the map (largest excursion %.1fσ; noise "
                              "alone reaches about %.1fσ somewhere in %d points)" % (zk, biggest,
                                                                                    threshold - 3, count))
            return out
        return structured()
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
    if fitted is not None and (abs(fitted[0][4]) * dx > float(np.ptp(ins_a)) or
                               abs(fitted[0][5]) * dy > float(np.ptp(outs_a))):
        out["results"] = out["results"][:2]   # wider than the map along an axis: its trend, not a spot
        return structured()
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

# ---------------------------------------------------------------------------- what the run measured


def _recorded(fit: Dict[str, Any], rows: List[Dict[str, Any]], swept: str, x_key: str, start: float
              ) -> List[Any]:
    """A fit in ohms, in the reading's own units to draw over it: R over start (the chain's factor), or where the run
    swept the excitation itself (swept: its key), R times the excitation's setting over start: the curve's one
    setting, or each x's where the excitation is x. Else nothing is drawn."""
    if not swept:
        return [y / start if _number(y) else y for y in fit["y"]]
    own = {abs(r[swept]) for r in rows if _number(r.get(swept))}
    if len(own) == 1:
        return [y * min(own) / start if _number(y) else y for y in fit["y"]]
    if swept == x_key:
        xs, ys = fit["x"], fit["y"]
        if len(xs) == 2 and all(_number(v) for v in xs + ys) and xs[1] != xs[0]:
            # a straight line in R curves in the reading: drawn at many points, not its two ends
            grid = _np().linspace(xs[0], xs[1], FIT_POINTS)
            ys = (ys[0] + (ys[1] - ys[0]) * (grid - xs[0]) / (xs[1] - xs[0])).tolist()
            xs = fit["x"] = grid.tolist()
        return [y * abs(x) / start if _number(y) and _number(x) else None for x, y in zip(xs, ys)]
    fit["x"] = []
    return []


def _first_point(rows: List[Dict[str, Any]], measures: List[Dict[str, str]], axes: List[Dict[str, str]]
                 ) -> Tuple[Set[str], List[str]]:
    """Instruments whose first reading along the first sweep is an empty one, 0 where the rest are not (a counter
    read before its first acquisition was in: 0 counts at every pulse-generator run's first point): those
    instruments, and what to say. Their first point is left out of the analysis."""
    np = _np()
    if len(rows) < 7:
        return set(), []
    outer = axes[0]["key"] if len(axes) > 1 else None
    first = [r for r in rows if outer is None or r.get(outer) == rows[0].get(outer)]
    found: Dict[str, List[str]] = {}
    for m in measures:
        if m.get("kind", "scalar") != "scalar" or not _number(first[0].get(m["key"])):
            continue
        v0 = float(first[0][m["key"]])
        rest = np.array([float(r[m["key"]]) for r in first[1:] if _number(r.get(m["key"]))])
        if rest.size < 5:
            continue
        spread, noise = float(np.ptp(rest)), noise_estimate(rest.tolist())
        trend = 2 * rest[0] - rest[1]   # where the next points say the first should be
        # nothing read yet: 0 where the rest are far from it and the next points don't lead there (a sweep of a
        # light's power from 0 W reads 0 there too). A large first point is kept: a decay's fast part starts there
        if v0 == 0 and float(np.abs(rest).min()) > 10 * max(noise, 1e-300) and \
                abs(v0 - trend) > 10 * max(noise, 0.02 * spread, 1e-300):
            found.setdefault(m["key"].split(".")[0], []).append(
                "%s on %s (the rest %s to %s)" % (si(v0, m.get("unit", "")), m["key"], si(float(rest.min()),
                                                                                        m.get("unit", "")),
                                                  si(float(rest.max()), m.get("unit", ""))))
    said = ["the first point reads %s: %s may not have settled there, so that point is left out of its fits"
            % (" and ".join(readings), inst) for inst, readings in found.items()]
    return set(found), said


def _ratios(rows: List[Dict[str, Any]], measures: List[Dict[str, str]]) -> Dict[str, Tuple[str, str]]:
    """Readings that are the ratio of two others of the same instrument in one unit (a counter's contrast, signal /
    reference: a plain number): {ratio: (numerator, denominator)}. A ratio leads over its parts, which drift with the
    light where it doesn't."""
    np = _np()
    by: Dict[str, List[str]] = {}
    units = {m["key"]: m.get("unit") or "" for m in measures}
    for m in measures:
        if m.get("kind", "scalar") == "scalar":
            by.setdefault(m["key"].split(".")[0], []).append(m["key"])
    out: Dict[str, Tuple[str, str]] = {}
    for keys in by.values():
        if len(keys) < 3:
            continue
        col = {k: np.array([float(r[k]) if _number(r.get(k)) else np.nan for r in rows]) for k in keys}
        for c in keys:
            for a in keys:
                for b in keys:
                    if len({a, b, c}) < 3 or units[c] or units[a] != units[b] or \
                            any(k in out or k in pair for pair in out.values() for k in (a, b, c)):
                        continue
                    with np.errstate(all="ignore"):
                        ratio = col[a] / col[b]
                    ok = np.isfinite(ratio) & np.isfinite(col[c])
                    if ok.sum() >= max(3, 0.8 * len(rows)) and np.allclose(col[c][ok], ratio[ok], rtol=2e-3, atol=0):
                        out[c] = (a, b)
    return out


def _spread(n: int, most: int) -> List[int]:
    """Up to `most` indices spread evenly over n, the first and the last among them."""
    if n <= most:
        return list(range(n))
    return sorted({int(round(i * (n - 1) / (most - 1))) for i in range(most)})


def _unify(found: List[Dict[str, Any]], again: Callable[[int, Sequence[str]], Dict[str, Any]]) -> None:
    """One model for every row of a run: where most rows that were fitted show a line, the line's shape most of
    them took (Lorentzian or Gaussian) is fitted to all of them (or two such lines where two are resolved), so the
    rows compare; a row where it isn't found says so instead of taking another shape."""
    fitted = [s["fit"]["model"] for s in found if s.get("fit") and s["kind"] not in ("linear", "tracking")]
    lines = [m for m in fitted if m in LINES]
    if len(found) < 2 or not lines or 2 * len(lines) < len(fitted):
        return
    singles = [m for m in lines if m in PEAKS]
    shape = max(PEAKS, key=lambda m: (singles.count(m), -PEAKS.index(m)))   # most rows' shape; a tie: Lorentzian
    for i, s in enumerate(found):
        if (s.get("fit") or {}).get("model") in (shape, "double_lorentzian") or \
                s["kind"] in ("flat", "steady", "none", "tracking", "linear", "value"):
            continue
        found[i] = again(i, (shape, "double_lorentzian"))


def _cap(s: str) -> str:
    if not s or "." in s.split(" ", 1)[0]:  # starts with an instrument key such as lockin.X: leave it as written
        return s
    return s[:1].upper() + s[1:]


def _trend(series: List[Dict[str, Any]], key: str) -> Optional[str]:
    """How the fitted feature of `key` changes along an outer loop: from the first row that shows it to the last
    (or the most it changes between them), and the rows where it is missing (a line that left the window)."""
    rows = [s for s in series if s["y"] == key and s.get("group")]
    shown = [s for s in rows if s.get("params")]
    if len(shown) < 2:
        return None
    look = [(s["kind"], (s.get("fit") or {}).get("model")) for s in shown]
    kind, model = max(look, key=lambda km: (look.count(km), -look.index(km)))
    tracked = [s for s in shown if (s["kind"], (s.get("fit") or {}).get("model")) == (kind, model)]
    if len(tracked) < 2:
        return None
    first, last = tracked[0], tracked[-1]
    what = first.get("name") or key
    moves, still = [], []
    width = next((v for k, v in first["params"].items() if k.startswith("FWHM")), None)   # a line's (the first's)
    for name, (v0, e0, unit) in first["params"].items():
        if name in ("background", "intercept") or e0 is None or \
                any(name not in s["params"] or s["params"][name][1] is None for s in tracked):
            continue
        scale = 0.02 * width[0] if name == "center" and width else \
            0.005 * max(abs(s["params"][name][0]) for s in tracked)

        def change(s: Dict[str, Any]) -> Tuple[float, float]:
            v, e, _ = s["params"][name]
            return v - v0, math.hypot(e0, e)

        def moved(s: Dict[str, Any]) -> bool:
            d, de = change(s)
            return abs(d) > 4 * de and abs(d) > scale
        if moved(last):
            d, de = change(last)
            text = "shifts by %s" % _fmt_pm(d, de, unit) if name == "center" else \
                "%s by %s" % ("rises" if d > 0 else "falls", _fmt_pm(abs(d), de, unit))
            if name != "center" and v0:
                text += " (%+.0f%%)" % (100 * d / abs(v0))
            moves.append("%s %s" % (name, text))
            continue
        far = max(tracked[1:-1], key=lambda s: abs(change(s)[0]), default=None)
        if far is not None and moved(far):   # out and back: a change the ends alone would hide (even in B)
            d, de = change(far)
            moves.append("%s changes by up to %s (at %s)" % (name, _fmt_pm(d, de, unit), far["group"].split(" = ")[-1]))
        else:
            still.append(name)
    span = "from %s to %s" % (first["group"], last["group"].split(" = ")[-1])
    if not moves:
        text = "%s, the %s %s does not change within the errors" % (span, what, kind)
    else:
        text = "%s, the %s %s %s" % (span, what, kind, " and ".join(moves[:2]))
        if "center" in still:
            text += "; its center stays put"
    end = next(i for i, s in enumerate(rows) if s is last)
    gone = [s["group"].split(" = ")[-1] for s in rows[end + 1:] if not any(s is t for t in tracked)]
    if gone and kind in ("peak", "dip"):
        text += "; no %s inside the sweep at %s" % (kind, ", ".join(gone[:4]))
    return text


def analyze_run(rows: List[Dict[str, Any]], axes: List[Dict[str, str]], measures: List[Dict[str, str]],
                meta: Optional[Dict[str, Any]] = None, force: Optional[str] = None,
                only: Optional[str] = None, readbacks: Optional[Dict[str, str]] = None,
                time_limit: float = TIME_LIMIT,
                derive: Optional[Dict[str, Tuple[float, str, str, str]]] = None) -> Dict[str, Any]:
    """axes/measures: [{key, unit, kind}] in plan order (kind 'scalar' or 'trace' for measures). readbacks: each set
    point's read-back reading, where the cards say. meta: the run's, with what it measured with where it says: the
    pulse protocol a generator played (protocol) and the readings that guarded its drives (guards). derive: the
    readings its measurement chain makes the sample's resistance ({key: (factor, name, unit, how)},
    chain.derived_readings). Returns {series: [...], quality: [...], issues: [...], assessment, next}."""
    meta = meta or {}
    derive = derive or {}
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
    protocol = meta.get("protocol") if isinstance(meta.get("protocol"), str) else None
    guards = set(meta.get("guards") or [])

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
    unsettled, said = _first_point(rows, measures, real_axes) if x_axis is not None else (set(), [])
    issues += said
    ratios = _ratios(rows, measures) if rows else {}
    parts = {k for pair in ratios.values() for k in pair}
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
            series += _trace_series(rows, m, x_axis, force, deadline, protocol)
            trend = _trend(series, m["key"])
            if trend:
                trends.append(trend)
            continue
        if m.get("kind") == "image":
            continue   # camera frames: look_at shows them
        derived = derive.get(m["key"])
        factor, name, unit, how = derived or (1.0, m["key"], m["unit"], "")
        mine = rows
        if m["key"].split(".")[0] in unsettled:   # its instrument's first point, left out
            mine = [dict(rows[0], **{m["key"]: None})] + rows[1:]
        swept, start = "", factor   # R = reading * start / the excitation's own setting, where that was swept
        source, used = getattr(derived, "source", ""), getattr(derived, "value", 0.0)
        if derived and used and len({r.get(source) for r in rows if _number(r.get(source))}) > 1:
            # the excitation itself swept (a linearity check): each point over its own current, not the start's
            swept, start = source, factor * used
            mine = [dict(r, **{m["key"]: r[m["key"]] * start / abs(r[source])
                               if _number(r.get(m["key"])) and _number(r.get(source)) and r[source] else None})
                    for r in mine]
            factor, how = 1.0, how.split(", I = ")[0] + ", I from each point's own " + source
        values = [r.get(m["key"]) for r in mine]
        numbers = [v for v in values if _number(v)]
        if not numbers:
            issues.append("%s: no numbers at any of its %d points" % (m["key"], len(rows)))
            continue
        if x_axis is None:
            mean = sum(numbers) / len(numbers) * factor
            sd = _mad_sigma(numbers) * factor if len(numbers) > 2 else 0.0
            err = sd / math.sqrt(len(numbers)) if sd else None
            series.append({"x": None, "y": m["key"], "kind": "value", "fit": None, "flags": [],
                           "summary": "%s = %s" % (name, _fmt_pm(mean, err, unit)),
                           "results": [{"name": "mean", "value": _fmt_pm(mean, None, unit),
                                        **_numeric(mean, err, unit)}]})
            continue
        followed = next((a for a in real_axes if tracks(a, m, [r.get(a["key"]) for r in rows],
                                                        [r.get(m["key"]) for r in rows], readbacks)), None)
        if followed is not None:   # a set point's read-back: checked for following it, never fitted as a finding
            s = _tracking([r.get(followed["key"]) for r in rows], [r.get(m["key"]) for r in rows], followed, m)
            series.append(s)
            continue
        grid = map_grid(mine, outer["key"], x_axis["key"], m["key"]) if outer is not None else None
        if grid is not None:   # a map: looked at as an image, not row by row
            outs, ins, z = grid
            s = analyze_map(outs, ins, z * factor, x_axis, outer, {"key": name, "unit": unit}, deadline)
            s["y"] = m["key"]
            if how:
                s["name"], s["chain"] = name, how
            series.append(s)
            continue
        groups: Dict[Any, List[Dict[str, Any]]] = {}
        for r in mine:
            groups.setdefault(r.get(outer["key"]) if outer else None, []).append(r)
        items = list(groups.items())[:12]

        def one(k: int, models: Optional[Sequence[str]] = None) -> Dict[str, Any]:
            gval, grows = items[k]
            xs = [r.get(x_axis["key"]) for r in grows]
            ys = [r.get(m["key"]) * factor if _number(r.get(m["key"])) else None for r in grows]
            s = analyze_curve(xs, ys, x_axis["key"], name, x_axis.get("unit", ""), unit, force, deadline, protocol,
                              models)
            s["y"] = m["key"]
            if how:   # told in ohms; drawn over the reading as recorded
                s["name"], s["chain"] = name, how
                if s.get("fit"):
                    s["fit"]["y"] = _recorded(s["fit"], grows, swept, x_axis["key"], start)
            if outer is not None:
                s["group"] = "%s = %s" % (outer["key"], si(gval, outer.get("unit", ""))) if _number(gval) else str(gval)
            h = hysteresis([x for x in xs if _number(x)], [y for y in ys if _number(y)], x_axis.get("unit", ""), unit)
            if h:
                s["flags"].append(h)
            return s
        found = [one(k) for k in range(len(items))]
        if outer is not None and force is None:
            _unify(found, one)
        series += found
        if outer is not None:
            trend = _trend(series, m["key"])
            if trend:
                trends.append(trend)

    # the most telling curve leads: a resonance, a spot or an oscillation, then a relaxation, then a line, then the
    # rest; a read-back only when there is nothing else. A ratio leads over its parts, and a guard reading (the
    # leakage a ramp watches) over nothing a person measured for its own sake
    rank = {"peak": 3, "dip": 3, "oscillation": 3, "step": 3, "spot": 3, "spots": 3, "dark spot": 3, "relaxation": 2,
            "quadratic": 2, "linear": 1, "structured map": 1, "tracking": -1, "none": -2}

    def lead(ks: Tuple[int, Dict[str, Any]]) -> Tuple[Any, ...]:
        k, s = ks
        r = rank.get(s["kind"], 0)
        return (r >= 0, s["y"] not in guards, s["y"] not in parts, r, -k)
    primary = max(enumerate(series), key=lead)[1] if series else None

    def said(of: List[Dict[str, Any]]) -> List[str]:
        return ["%s%s: %s" % (s["y"], " (" + s["group"] + ")" if s.get("group") else "", f) for s in of
                for f in s["flags"]]
    issues += said([s for s in series if s is primary] + [s for s in series if s is not primary and
                                                           s["y"] not in parts])   # the leading curve's first
    if skipped:
        issues.append("not analysed in the %.0f s the automatic analysis takes: %s (analyze_run with column analyses "
                      "one)" % (time_limit, ", ".join(skipped)))
    findings = [s for s in series if s["kind"] not in ("tracking", "none")]
    if rows and measures and series and findings and all(s["kind"] in ("flat",) for s in findings):
        issues.append("no measured column shows a signal above the noise")
    shown = issues[:1]   # a ratio's parts' own quirks (the counts falling as the sequence lengthens) come last,
    issues += said([s for s in series if s is not primary and s["y"] in parts])   # never in the headline

    sentences = []
    if primary:
        sentences.append(_cap(primary["summary"]) + ("" if primary["summary"].endswith(".") else "."))
    if trends:
        lead_trend = next((t for t in trends if primary and primary["y"] in t), trends[0])
        sentences.append(_cap(lead_trend) + ".")
    if shown:
        sentences.append(_cap(shown[0]) + ("." if not shown[0].endswith(".") else ""))
    else:
        sentences.append("%s%s." % (_cap(quality[0] if quality else "run complete"),
                                    "" if issues else "; no problems found"))
    nxt = _next_step(primary, issues, trends, protocol)
    return {"series": series, "quality": quality, "issues": issues, "trends": trends,
            "assessment": " ".join(sentences), "next": nxt}


def _trace_series(rows: List[Dict[str, Any]], m: Dict[str, str], x_axis: Optional[Dict[str, str]],
                  force: Optional[str], deadline: Optional[float] = None,
                  protocol: Optional[str] = None) -> List[Dict[str, Any]]:
    """A run's traces (one per point), up to TRACE_ROWS of them spread over the run, with one model for all."""
    items = []
    for i in _spread(len(rows), TRACE_ROWS):
        ys = rows[i].get(m["key"])
        if isinstance(ys, list) and len(ys) >= 6:
            items.append((rows[i], ys))

    def one(k: int, models: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        r, ys = items[k]
        xs = trace_x(r, m["key"], len(ys))
        s = analyze_curve(xs, ys, "frequency" if m.get("x_unit") == "Hz" else "x", m["key"], m.get("x_unit", ""),
                          m["unit"], force, deadline, protocol, models)
        if x_axis is not None and len(rows) > 1:
            s["group"] = "%s = %s" % (x_axis["key"], si(r.get(x_axis["key"]), x_axis.get("unit", "")))
        # keep curves light for the browser
        if s.get("fit"):
            s["fit"]["x"], s["fit"]["y"] = s["fit"]["x"][::2], s["fit"]["y"][::2]
        return s
    out = []
    for k in range(len(items)):
        if deadline is not None and time.monotonic() > deadline:
            break
        out.append(one(k))
    if force is None and x_axis is not None:
        _unify(out, one)
    return out


def _next_step(primary: Optional[Dict[str, Any]], issues: List[str], trends: Sequence[str] = (),
               protocol: Optional[str] = None) -> str:
    if primary is None:
        if any(i.startswith("no data") for i in issues):
            return "Find out why no point was recorded (the run's reason and the event log), then run it again."
        return "Record at least one measured quantity to analyse."
    for flag in primary["flags"]:
        if "under-resolved" in flag and "; use a step" in flag:
            return "Refine the sweep: " + flag.split("; ", 1)[-1] + "."
        if "under-resolved" in flag:
            return "Refine the map: use a finer step around the spot."
        if "edge of the sweep" in flag or "extend beyond" in flag or "edge of the map" in flag or \
                "wider than the" in flag:
            return "Widen the sweep range to capture the whole feature."
        if "clipped" in flag:
            return "Change the range or sensitivity so the signal is not clipped, then repeat."
        if "did not reach it" in flag:
            return "Find out why the set point was not reached (ramp rate, the instrument's mode or limits) before " \
                   "measuring again."
        if flag.startswith("a possible "):
            return "Average longer (or sweep finer around it) to tell whether the possible %s is real." \
                % flag[len("a possible "):].split(" (")[0]
    if any("stopped after" in i for i in issues):
        return "Fix the cause of the stop, then rerun the remaining range."
    kind = primary["kind"]
    if kind in ("spot", "spots", "dark spot"):
        return "Center a finer map, or a line scan, on the spot to resolve its shape."
    if kind == "structured map":
        return "Look at the map (look_at) and cut through its structure with a line sweep."
    if kind == "tracking":
        return "Measure a quantity of the sample, not only the read-back of what was set."
    if primary.get("group"):
        outer = primary["group"].split(" = ")[0]
        trend = next((t for t in trends if primary["y"] in t or (primary.get("name") and primary["name"] in t)), None)
        if trend and "inside the sweep at" in trend:
            return "Re-centre the sweep on the %s at each %s value: it moves out of the window." % (kind, outer)
        if trend and "does not change" in trend:
            return "Nothing changes with %s over this range: widen it, or try another parameter." % outer
        if trend:
            return "Add more %s values (and a finer step where it changes fastest) to map the dependence." % outer
        if kind in ("peak", "dip", "linear", "relaxation", "quadratic", "oscillation", "step"):
            return "Compare the curves at each %s value (plot_runs with group: outer): no one fit follows them all." \
                % outer
    if kind == "steady":
        if primary["x"] == "time":
            return "Stable: go ahead with the measurement."
        return "No dependence on %s within the noise: widen its range, or check that it reaches the sample." % primary["x"]
    if kind == "flat":
        return "No signal: raise the excitation or averaging, or check the wiring, before sweeping again."
    if kind in ("peak", "dip"):
        if protocol in ("cw", "pulsed_odmr"):
            centers = [r["value"] for r in primary["results"] if r["name"].startswith("center")]
            return "Set the microwave to a line's center (%s) for the pulsed measurements." % " or ".join(centers)
        if primary.get("lines") == 2:
            return "The two %ss are resolved; next, track how they move with a second parameter (field, " \
                   "temperature)." % kind
        return "The %s is well resolved; next, track how it moves with a second parameter (field, temperature)." % kind
    if kind == "linear":
        return "The response is linear; extend the range to look for where it stops being linear."
    if kind == "step":
        return "The step is resolved; next, see how its midpoint moves with a second parameter (field, current, " \
               "temperature)."
    if kind == "quadratic":
        return "The response is quadratic in %s; next, see how its curvature changes with a second parameter." \
            % primary["x"]
    if kind == "relaxation":
        if protocol in ("t1", "hahn_echo"):
            return "τ is the %s; average longer to narrow its error, or see how it changes with a second parameter." \
                % ("T1" if protocol == "t1" else "echo's T2")
        if any(r["name"].startswith("time constant") for r in primary["results"]):
            return "Use the fitted τ to set the wait time (about 5 τ) before each point of the next sweep."
        return "The exponential scale is resolved; next, see how it changes with a second parameter."
    if kind == "oscillation":
        if protocol == "rabi":
            return ("Use the half period as the π pulse (and half of it for π/2), then run pulsed ODMR or Ramsey "
                    "with it.")
        if protocol == "ramsey":
            return ("Its frequency is the detuning from resonance and its decay gives T2* (fit_run damped_cosine with "
                    "n: 2 for a Gaussian decay).")
        return "The oscillation is resolved; next, see how its frequency changes with a second parameter."
    return "Look at the plot for structure the simple fits miss, then decide on a finer sweep."
