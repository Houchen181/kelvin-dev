"""Fits: a model fitted to data, with weights, a window and parameters held fixed; its chi-squared, covariance and
residuals kept with the run (run.h5: /fits/<name>).

A model is one of Kelvin's (MODELS: peaks, decays, oscillations, a resonator's notch with its cable delay, decays
convolved with the instrument response, graphene's R(n), Hall density and mobility, Hc2 after Ginzburg-Landau and
WHH) or the person's own expression in x, e.g. "a*exp(-x/tau) + c": every name in it that isn't x, a column of the
run, a function or a named constant (expr.py) is a parameter.

NumPy only (the app brings no SciPy): weighted Levenberg-Marquardt with bounds and a numerical Jacobian. Errors are
one standard error from the covariance at the minimum: scaled by the reduced chi-squared when the points carry no sigma
of their own (their scatter sets the errors), taken as they are when they do (then the reduced chi-squared says
whether those sigma were right)."""
from __future__ import annotations

import ast
import json
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import expr

QE = 1.602176634e-19
PHI0 = 2.067833848e-15
MAX_POINTS = 200_000          # a fit's points (a long trace is fitted whole up to this)
CURVE_POINTS = 400            # the model drawn over the data's range
_STORE = threading.Lock()     # one writer of run.h5 at a time in this Kelvin


class FitError(ValueError):
    pass


def _np() -> Any:
    try:
        import numpy as np
    except ImportError:
        raise FitError("fitting needs NumPy, which this Kelvin does not have")
    return np

# ------------------------------------------------------------------ the minimiser


@dataclass
class Solution:
    p: Any                 # every parameter, held ones too
    cov: Any               # (JᵀJ)⁻¹ over the free ones, not yet scaled
    cost: float            # sum of the squared (weighted) residuals
    resid: Any
    iterations: int
    converged: bool
    message: str
    rank: int
    free: Any


def least_squares(fun: Callable[[Any], Any], p0: Sequence[float], free: Optional[Sequence[int]] = None,
                  lower: Optional[Sequence[float]] = None, upper: Optional[Sequence[float]] = None,
                  scale: Optional[Sequence[float]] = None, max_iter: int = 200,
                  deadline: Optional[float] = None) -> Solution:
    """Minimise sum(fun(p)**2) over the free parameters: Levenberg-Marquardt with Marquardt's scaling, every step
    kept inside the bounds. scale: each parameter's size, for the finite-difference steps (a parameter that starts at
    0 has no size of its own)."""
    np = _np()
    p = np.array(p0, dtype=float)
    k_all = p.size
    free = np.arange(k_all) if free is None else np.asarray(list(free), dtype=int)
    lo = np.full(k_all, -np.inf) if lower is None else np.asarray(lower, dtype=float)
    hi = np.full(k_all, np.inf) if upper is None else np.asarray(upper, dtype=float)
    p = np.clip(p, lo, hi)
    size = np.ones(k_all) if scale is None else np.abs(np.asarray(scale, dtype=float))

    def residual(q: Any) -> Any:
        with np.errstate(all="ignore"):
            return np.asarray(fun(q), dtype=float).ravel()

    def jacobian(q: Any, r: Any) -> Any:
        jac = np.empty((r.size, free.size))
        for col, j in enumerate(free):
            h = 1.5e-8 * max(abs(q[j]), size[j], 1e-300)
            for _ in range(3):   # a step the model can't feel (or one past a bound): another size
                step = h if q[j] + h <= hi[j] else -h
                t = q.copy()
                t[j] += step
                d = (residual(t) - r) / step
                if np.all(np.isfinite(d)) and np.any(d != 0):
                    break
                h *= 1e3
            jac[:, col] = np.where(np.isfinite(d), d, 0.0)
        return jac

    r = residual(p)
    if not np.all(np.isfinite(r)):
        raise FitError("the model gives no finite value at the starting parameters for %d of %d points"
                       % (int((~np.isfinite(r)).sum()), r.size))
    cost = float(r @ r)
    if free.size == 0:
        return Solution(p, np.zeros((0, 0)), cost, r, 0, True, "", 0, free)
    def scaled(jac: Any) -> Tuple[Any, Any]:
        """The Jacobian with unit columns, and the columns' norms: parameters of any size weigh alike (Marquardt's
        scaling), so neither the steps nor the covariance lose a small one to a large one."""
        norms = np.sqrt((jac * jac).sum(axis=0))
        norms[~(norms > 0)] = 1.0
        return jac / norms, norms

    lam, it, converged, message = 1e-3, 0, False, ""
    jac = jacobian(p, r)
    while it < max_iter:
        it += 1
        js, norms = scaled(jac)
        g = js.T @ r
        a = js.T @ js
        eye = np.eye(a.shape[0])
        accepted = False
        while lam < 1e16:
            try:
                step = -np.linalg.solve(a + lam * eye, g) / norms
            except np.linalg.LinAlgError:
                lam *= 10
                continue
            q = p.copy()
            q[free] = np.clip(p[free] + step, lo[free], hi[free])
            rq = residual(q)
            cq = float(rq @ rq) if np.all(np.isfinite(rq)) else math.inf
            if cq < cost:
                accepted = True
                break
            lam *= 10
        if not accepted:   # no step lowers chi-squared: at a minimum
            converged = True
            break
        drop = (cost - cq) / max(cost, 1e-300)
        moved = np.max(np.abs(q - p)[free] / np.maximum(np.abs(p[free]), size[free]))
        p, r, cost = q, rq, cq
        lam = max(lam / 10, 1e-15)
        if drop < 1e-12 or moved < 1e-12 or cost == 0:
            converged = True
            break
        if deadline is not None and time.monotonic() > deadline:
            message = "stopped at the time limit before it converged"
            break
        jac = jacobian(p, r)
    if not converged and not message:
        message = "did not converge in %d steps" % max_iter
    jac = jacobian(p, r)
    js, norms = scaled(jac)
    sv = np.linalg.svd(js, compute_uv=False) if js.size else np.zeros(0)
    rank = int((sv > sv[0] * 1e-8).sum()) if sv.size and sv[0] > 0 else 0
    cov = np.linalg.pinv(js.T @ js, rcond=1e-14) / np.outer(norms, norms) if js.size else np.zeros((0, 0))
    return Solution(p, cov, cost, r, it, converged, message, rank, free)

# ------------------------------------------------------------------ special functions NumPy lacks


def erfcx(x: Any) -> Any:
    """exp(x²)·erfc(x) for x >= 0 (Numerical Recipes' erfcc, relative error below 1.2e-7)."""
    np = _np()
    x = np.asarray(x, dtype=float)
    t = 1.0 / (1.0 + 0.5 * x)
    return t * np.exp(-1.26551223 + t * (1.00002368 + t * (0.37409196 + t * (0.09678418 + t * (
        -0.18628806 + t * (0.27886807 + t * (-1.13520398 + t * (1.48851587 + t * (-0.82215223 + t * 0.17087277)))))))))


def erfc(x: Any) -> Any:
    np = _np()
    x = np.asarray(x, dtype=float)
    a = np.abs(x)
    with np.errstate(over="ignore", under="ignore"):
        pos = erfcx(a) * np.exp(-a * a)
    return np.where(x >= 0, pos, 2.0 - pos)


def digamma(x: Any) -> Any:
    """ψ(x) for x > 0: shifted up past 6, then the asymptotic series."""
    np = _np()
    x = np.array(x, dtype=float)
    acc = np.zeros_like(x)
    for _ in range(8):
        small = x < 6
        if not small.any():
            break
        acc = acc - np.where(small, 1.0 / np.where(small, x, 1.0), 0.0)
        x = np.where(small, x + 1, x)
    inv = 1.0 / (x * x)
    return acc + np.log(x) - 0.5 / x - inv * (1 / 12 - inv * (1 / 120 - inv * (1 / 252 - inv * (1 / 240 - inv / 132))))

# ------------------------------------------------------------------ models


@dataclass
class Model:
    name: str
    params: Tuple[str, ...]
    f: Callable[[Any, Dict[str, Any]], Any]      # f(x, {parameter: value}) -> y (complex for a complex model)
    formula: str
    guess: Callable[[Any, Any], Dict[str, float]]
    units: Callable[[str, str], Dict[str, str]]
    about: str = ""
    lower: Dict[str, float] = field(default_factory=dict)
    upper: Dict[str, float] = field(default_factory=dict)
    held: Dict[str, float] = field(default_factory=dict)    # held at these values unless start: frees them
    needs: Tuple[str, ...] = ()                              # values the person must give (fix:)
    constants: Tuple[str, ...] = ()                          # never fitted: given with fix (or their held value)
    derived: Optional[Callable[[Dict[str, float], str, str], List[Tuple[str, float, str]]]] = None
    complex: bool = False
    x_ref: bool = False                                      # x is counted from the first x of the window
    columns: Tuple[str, ...] = ()                            # run columns an expression model reads
    sizes: Optional[Callable[[Any, Any], Dict[str, float]]] = None   # how much each parameter matters, for steps


def _median(np: Any, a: Any) -> float:
    return float(np.median(a)) if np.size(a) else 0.0


def _step(np: Any, x: Any) -> float:
    d = np.abs(np.diff(x))
    d = d[d > 0]
    return float(np.median(d)) if d.size else 1.0


def _peak(np: Any, x: Any, y: Any) -> Tuple[float, float, float, float, int]:
    """(background, amplitude, center, FWHM, index) of the largest excursion from the edges' level."""
    n = y.size
    edge = max(2, n // 7)
    base = _median(np, np.concatenate([y[:edge], y[-edge:]]))
    k = int(np.argmax(np.abs(y - base)))
    amp = float(y[k] - base)
    inside = (y - (base + amp / 2)) * np.sign(amp or 1.0) > 0
    left = k
    out = np.flatnonzero(~inside[:k])
    left = int(out[-1]) if out.size else 0
    out = np.flatnonzero(~inside[k:])
    right = k + int(out[0]) if out.size else n - 1
    width = max(abs(float(x[right] - x[left])), 2 * _step(np, x))
    return base, amp, float(x[k]), width, k


def _lorentz(x: Any, c: float, w: float) -> Any:
    return 1.0 / (1.0 + ((x - c) / (abs(w) / 2 + 1e-300)) ** 2)


def _gauss(np: Any, x: Any, c: float, w: float) -> Any:
    return np.exp(-4 * math.log(2) * (x - c) ** 2 / (w * w + 1e-300))


def _same(xu: str, yu: str) -> Callable[[str, str], Dict[str, str]]:
    return lambda x_unit, y_unit: {}


def _per(y_unit: str, x_unit: str) -> str:
    if not y_unit and not x_unit:
        return ""
    return "%s/%s" % (y_unit or "1", x_unit or "1")


def _q(v: Dict[str, float], x_unit: str, y_unit: str) -> List[Tuple[str, float, str]]:
    return [("Q (center/FWHM)", v["x0"] / v["fwhm"], "")] if x_unit == "Hz" and v["fwhm"] else []


def _lorentzian() -> Model:
    def f(x: Any, p: Dict[str, Any]) -> Any:
        return p["y0"] + p["A"] * _lorentz(x, p["x0"], p["fwhm"])

    def guess(x: Any, y: Any) -> Dict[str, float]:
        np = _np()
        b, a, c, w, _ = _peak(np, x, y)
        return {"y0": b, "A": a, "x0": c, "fwhm": w}
    return Model("lorentzian", ("y0", "A", "x0", "fwhm"), f, "y0 + A / (1 + ((x - x0) / (fwhm/2))²)", guess,
                 lambda xu, yu: {"y0": yu, "A": yu, "x0": xu, "fwhm": xu}, "a Lorentzian peak or dip",
                 lower={"fwhm": 0.0}, derived=_q)


def _gaussian() -> Model:
    def f(x: Any, p: Dict[str, Any]) -> Any:
        return p["y0"] + p["A"] * _gauss(_np(), x, p["x0"], p["fwhm"])

    def guess(x: Any, y: Any) -> Dict[str, float]:
        np = _np()
        b, a, c, w, _ = _peak(np, x, y)
        return {"y0": b, "A": a, "x0": c, "fwhm": w}
    return Model("gaussian", ("y0", "A", "x0", "fwhm"), f, "y0 + A·exp(-4 ln2 (x - x0)²/fwhm²)", guess,
                 lambda xu, yu: {"y0": yu, "A": yu, "x0": xu, "fwhm": xu}, "a Gaussian peak or dip",
                 lower={"fwhm": 0.0}, derived=_q)


def _double_lorentzian() -> Model:
    def f(x: Any, p: Dict[str, Any]) -> Any:
        return p["y0"] + p["A1"] * _lorentz(x, p["x1"], p["w1"]) + p["A2"] * _lorentz(x, p["x2"], p["w2"])

    def guess(x: Any, y: Any) -> Dict[str, float]:
        np = _np()
        b, a, c, w, k = _peak(np, x, y)
        rest = y - a * _lorentz(x, c, w)
        _, a2, c2, w2, _ = _peak(np, x, rest)
        return {"y0": b, "A1": a, "x1": c, "w1": w, "A2": a2, "x2": c2, "w2": w2}
    return Model("double_lorentzian", ("y0", "A1", "x1", "w1", "A2", "x2", "w2"), f,
                 "y0 + A1/(1 + ((x - x1)/(w1/2))²) + A2/(1 + ((x - x2)/(w2/2))²)", guess,
                 lambda xu, yu: {"y0": yu, "A1": yu, "x1": xu, "w1": xu, "A2": yu, "x2": xu, "w2": xu},
                 "two Lorentzian peaks (FWHM w1, w2)", lower={"w1": 0.0, "w2": 0.0})


def _decay_guess(np: Any, x: Any, y: Any) -> Tuple[float, float, float]:
    """(final value, amplitude at the first x, time constant) of a decay counted from x[0]."""
    edge = max(2, y.size // 7)
    final = _median(np, y[-edge:])
    amp = float(y[:max(1, edge // 2)].mean() - final)
    dist = np.abs(y - final)
    below = np.flatnonzero(dist < abs(amp) / math.e)
    tau = float(x[below[0]] - x[0]) if below.size and below[0] > 0 else float(x[-1] - x[0]) / 3
    return final, amp, max(tau, _step(np, x))


def _exponential() -> Model:
    def f(x: Any, p: Dict[str, Any]) -> Any:
        np = _np()
        return p["y_inf"] + p["A"] * np.exp(np.maximum(-(x / p["tau"]), -700.0))

    def guess(x: Any, y: Any) -> Dict[str, float]:
        final, amp, tau = _decay_guess(_np(), x, y)
        return {"y_inf": final, "A": amp, "tau": tau}
    return Model("exponential", ("y_inf", "A", "tau"), f, "y_inf + A·exp(-(x - x_start)/tau)", guess,
                 lambda xu, yu: {"y_inf": yu, "A": yu, "tau": xu}, "an exponential decay or rise",
                 x_ref=True)


def _stretched() -> Model:
    def f(x: Any, p: Dict[str, Any]) -> Any:
        np = _np()
        return p["y_inf"] + p["A"] * np.exp(-np.abs(x / p["tau"]) ** p["beta"])

    def guess(x: Any, y: Any) -> Dict[str, float]:
        final, amp, tau = _decay_guess(_np(), x, y)
        return {"y_inf": final, "A": amp, "tau": tau, "beta": 1.0}
    return Model("stretched_exponential", ("y_inf", "A", "tau", "beta"), f,
                 "y_inf + A·exp(-((x - x_start)/tau)^beta)", guess,
                 lambda xu, yu: {"y_inf": yu, "A": yu, "tau": xu, "beta": ""}, "a stretched exponential",
                 lower={"tau": 0.0, "beta": 0.05}, upper={"beta": 5.0}, x_ref=True)


def _biexponential() -> Model:
    def f(x: Any, p: Dict[str, Any]) -> Any:
        np = _np()
        return p["y_inf"] + p["A1"] * np.exp(np.maximum(-x / p["tau1"], -700)) + \
            p["A2"] * np.exp(np.maximum(-x / p["tau2"], -700))

    def guess(x: Any, y: Any) -> Dict[str, float]:
        final, amp, tau = _decay_guess(_np(), x, y)
        return {"y_inf": final, "A1": 0.6 * amp, "tau1": tau * 1.6, "A2": 0.4 * amp, "tau2": tau / 4}
    return Model("biexponential", ("y_inf", "A1", "tau1", "A2", "tau2"), f,
                 "y_inf + A1·exp(-(x - x_start)/tau1) + A2·exp(-(x - x_start)/tau2)", guess,
                 lambda xu, yu: {"y_inf": yu, "A1": yu, "tau1": xu, "A2": yu, "tau2": xu}, "two exponential decays",
                 lower={"tau1": 0.0, "tau2": 0.0}, x_ref=True)


def _with_irf(np: Any, t: Any, t0: float, tau: float, fwhm: float) -> Any:
    """exp(-(t - t0)/tau) for t > t0, convolved with a Gaussian of this FWHM (unit area), without overflow."""
    s = max(abs(fwhm), 1e-300) / 2.3548200450309493
    tau = max(abs(tau), 1e-300)
    d = t - t0
    u = (s / tau - d / s) / math.sqrt(2)
    with np.errstate(over="ignore", under="ignore", invalid="ignore"):
        wide = np.exp(-d * d / (2 * s * s)) * erfcx(np.maximum(u, 0))      # u >= 0: the scaled form
        narrow = np.exp(np.minimum(s * s / (2 * tau * tau) - d / tau, 700)) * erfc(np.minimum(u, 0))
    return 0.5 * np.where(u >= 0, wide, narrow)


def _irf_guess(np: Any, x: Any, y: Any, two: bool) -> Dict[str, float]:
    k = int(np.argmax(np.abs(y - _median(np, y[: max(2, y.size // 10)]))))
    base = _median(np, y[: max(2, k // 2)]) if k > 3 else float(y.min())
    peak = float(y[k] - base)
    rise = np.flatnonzero((y[:k + 1] - base) * np.sign(peak or 1) < abs(peak) / 2)
    t_half = float(x[rise[-1]]) if rise.size else float(x[0])
    fwhm = max(2 * (float(x[k]) - t_half), 2 * _step(np, x))
    tail = slice(k, y.size)
    _, _, tau = _decay_guess(np, x[tail] - x[k], y[tail])
    g = {"y0": base, "t0": t_half + fwhm / 4, "irf": fwhm}
    if two:
        g.update(A1=0.7 * peak, tau1=tau * 1.5, A2=0.3 * peak, tau2=max(tau / 5, fwhm / 4))
    else:
        g.update(A=peak, tau=tau)
    return g


def _exp_irf() -> Model:
    def f(x: Any, p: Dict[str, Any]) -> Any:
        return p["y0"] + p["A"] * _with_irf(_np(), x, p["t0"], p["tau"], p["irf"])
    return Model("exp_irf", ("y0", "A", "tau", "t0", "irf"), f,
                 "y0 + A·[exp(-(x - t0)/tau) for x > t0] ⊗ Gaussian(FWHM irf)",
                 lambda x, y: _irf_guess(_np(), x, y, False),
                 lambda xu, yu: {"y0": yu, "A": yu, "tau": xu, "t0": xu, "irf": xu},
                 "an exponential decay starting at t0, convolved with a Gaussian instrument response",
                 lower={"tau": 0.0, "irf": 0.0})


def _biexp_irf() -> Model:
    def f(x: Any, p: Dict[str, Any]) -> Any:
        np = _np()
        return p["y0"] + p["A1"] * _with_irf(np, x, p["t0"], p["tau1"], p["irf"]) + \
            p["A2"] * _with_irf(np, x, p["t0"], p["tau2"], p["irf"])
    return Model("biexp_irf", ("y0", "A1", "tau1", "A2", "tau2", "t0", "irf"), f,
                 "y0 + [A1·exp(-(x - t0)/tau1) + A2·exp(-(x - t0)/tau2) for x > t0] ⊗ Gaussian(FWHM irf)",
                 lambda x, y: _irf_guess(_np(), x, y, True),
                 lambda xu, yu: {"y0": yu, "A1": yu, "tau1": xu, "A2": yu, "tau2": xu, "t0": xu, "irf": xu},
                 "two exponential decays starting at t0, convolved with a Gaussian instrument response (pump-probe, "
                 "time-resolved photoluminescence)", lower={"tau1": 0.0, "tau2": 0.0, "irf": 0.0})


def _damped_cosine() -> Model:
    def f(x: Any, p: Dict[str, Any]) -> Any:
        np = _np()
        env = np.exp(-np.abs(x / p["tau"]) ** p["n"])
        return p["y0"] + p["A"] * env * np.cos(2 * math.pi * p["f"] * x + p["phi"])

    def guess(x: Any, y: Any) -> Dict[str, float]:
        np = _np()
        span = float(x[-1] - x[0]) or 1.0
        m = min(4096, max(16, x.size))
        grid = np.linspace(x[0], x[-1], m)
        v = np.interp(grid, x, y) - y.mean()
        spec = np.fft.rfft(v)
        k = 1 + int(np.argmax(np.abs(spec[1:]))) if spec.size > 1 else 1
        freq = k / (span * m / (m - 1))
        c = spec[k] if spec.size > k else 0j
        return {"y0": float(y.mean()), "A": float(2 * abs(c) / m), "f": freq, "phi": float(np.angle(c)),
                "tau": 2 * span, "n": 1.0}

    def derived(v: Dict[str, float], xu: str, yu: str) -> List[Tuple[str, float, str]]:
        out = [("period", 1 / v["f"], xu)] if v["f"] else []
        if xu == "s" and v["f"]:
            out.append(("half period (π time for Rabi)", 0.5 / v["f"], "s"))
        return out
    return Model("damped_cosine", ("y0", "A", "f", "phi", "tau", "n"), f,
                 "y0 + A·exp(-((x - x_start)/tau)^n)·cos(2π f (x - x_start) + phi)", guess,
                 lambda xu, yu: {"y0": yu, "A": yu, "f": "Hz" if xu == "s" else ("1/" + xu if xu else ""),
                                 "phi": "rad", "tau": xu, "n": ""},
                 "a damped oscillation (Rabi, Ramsey); the envelope's power n is held at 1 (exponential) unless "
                 "start or fix gives it (n: 2 is a Gaussian envelope)",
                 lower={"tau": 0.0, "f": 0.0}, held={"n": 1.0}, derived=derived, x_ref=True)


def _linear() -> Model:
    def f(x: Any, p: Dict[str, Any]) -> Any:
        return p["a"] + p["b"] * x

    def guess(x: Any, y: Any) -> Dict[str, float]:
        np = _np()
        b, a = np.polyfit(x, y, 1) if x.size > 1 and np.ptp(x) > 0 else (0.0, float(y.mean()))
        return {"a": float(a), "b": float(b)}

    def derived(v: Dict[str, float], xu: str, yu: str) -> List[Tuple[str, float, str]]:
        if xu == "V" and yu == "A" and v["b"]:
            return [("resistance (1/slope)", 1 / v["b"], "Ω")]
        if xu == "A" and yu == "V":
            return [("resistance (slope)", v["b"], "Ω")]
        return []
    return Model("linear", ("a", "b"), f, "a + b·x", guess, lambda xu, yu: {"a": yu, "b": _per(yu, xu)},
                 "a straight line", derived=derived)

# ------------------------------------------------------------------ a resonator's notch, with its cable delay


def _notch(np: Any, f: Any, p: Dict[str, Any]) -> Any:
    """S21 of a notch-type resonator (Probst et al., Rev. Sci. Instrum. 86, 024706 (2015)): the environment's
    amplitude a and phase alpha (at fr: counted from 0 Hz, the phase and the delay can't be told apart), the cable
    delay tau, then the resonance (fr, loaded Ql, |Qc|, impedance mismatch phi)."""
    env = p["a"] * np.exp(1j * (p["alpha"] - 2 * math.pi * (f - p["fr"]) * p["tau"]))
    return env * (1 - (p["Ql"] / p["Qc"]) * np.exp(1j * p["phi"]) / (1 + 2j * p["Ql"] * (f / p["fr"] - 1)))


def _circle(np: Any, z: Any) -> Tuple[complex, float, float]:
    """The circle through complex points (Kasa's algebraic fit): center, radius, rms distance from it."""
    x, y = z.real, z.imag
    a = np.column_stack([x, y, np.ones_like(x)])
    c, *_ = np.linalg.lstsq(a, x * x + y * y, rcond=None)
    xc, yc = c[0] / 2, c[1] / 2
    r = math.sqrt(max(c[2] + xc * xc + yc * yc, 0.0))
    return complex(xc, yc), r, float(np.sqrt(np.mean((np.abs(z - complex(xc, yc)) - r) ** 2)))


def _notch_guess(f: Any, z: Any) -> Dict[str, float]:
    """Probst's circle fit: the cable delay that puts the data on a circle, the circle, the phase around its center
    (fr, Ql), then the off-resonant point (a, alpha) and the circle's size and tilt (|Qc|, phi)."""
    np = _np()
    n = f.size
    edge = max(3, n // 10)
    ph = np.unwrap(np.angle(z))
    sel = np.r_[0:edge, n - edge:n]
    slope = np.polyfit(f[sel], ph[sel], 1)[0] if np.ptp(f[sel]) > 0 else 0.0
    tau0 = -slope / (2 * math.pi)
    span = float(f[-1] - f[0]) or 1.0

    def spread(tau: float) -> float:
        return _circle(np, z * np.exp(2j * math.pi * f * tau))[2]
    best = min((spread(tau0 + d), tau0 + d) for d in np.linspace(-2.0, 2.0, 41) / span)[1]
    width = 4.0 / span / 40
    for _ in range(40):   # golden-section search for the tightest circle near the best delay
        a_, b_ = best - width, best + width
        g = 0.381966
        c1, c2 = a_ + g * (b_ - a_), b_ - g * (b_ - a_)
        best = c1 if spread(c1) < spread(c2) else c2
        width *= 0.7
    tau = best
    z1 = z * np.exp(2j * math.pi * f * tau)
    zc, r, _ = _circle(np, z1)
    theta = np.unwrap(np.angle(z1 - zc))
    k = int(np.argmax(np.abs(np.gradient(theta, f)))) if n > 4 else n // 2   # the phase turns fastest at fr
    fr = float(f[k])
    fwhm = max(span / 20, _step(np, f))
    p = np.array([float(theta[k]), fr, fr / fwhm])

    def phase(q: Any) -> Any:
        return q[0] + 2 * np.arctan(2 * q[2] * (1 - f / q[1])) - theta
    try:
        p = least_squares(phase, p, scale=[1.0, fwhm, p[2]], max_iter=100).p
    except FitError:
        pass
    theta0, fr, ql = float(p[0]), float(p[1]), abs(float(p[2]))
    off = zc + r * np.exp(1j * (theta0 + math.pi))
    a = abs(off)
    alpha = float(np.angle(off * np.exp(-2j * math.pi * fr * tau)))   # the environment's phase at fr
    zc_n, r_n = zc / off, r / max(a, 1e-300)
    phi = -math.asin(max(-1.0, min(1.0, zc_n.imag / max(r_n, 1e-300))))
    qc = ql / max(2 * r_n, 1e-12)
    return {"fr": fr, "Ql": ql, "Qc": qc, "phi": phi, "a": a, "alpha": alpha, "tau": tau}


def _notch_derived(v: Dict[str, float], xu: str, yu: str) -> List[Tuple[str, float, str]]:
    inv_qi = 1 / v["Ql"] - math.cos(v["phi"]) / v["Qc"]
    out = [("Qi (internal)", 1 / inv_qi if inv_qi > 0 else math.inf, ""),
           ("Qc (coupling, |Qc|/cos phi)", v["Qc"] / math.cos(v["phi"]) if math.cos(v["phi"]) else math.inf, ""),
           ("linewidth fr/Ql", v["fr"] / v["Ql"], "Hz")]
    return out


def _resonator() -> Model:
    def f(x: Any, p: Dict[str, Any]) -> Any:
        return _notch(_np(), x, p)
    return Model("resonator", ("fr", "Ql", "Qc", "phi", "a", "alpha", "tau"), f,
                 "a·exp(i(alpha - 2π f tau))·[1 - (Ql/|Qc|)·exp(i phi)/(1 + 2i Ql (f/fr - 1))]",
                 lambda x, y: _notch_guess(x, y),
                 lambda xu, yu: {"fr": xu or "Hz", "Ql": "", "Qc": "", "phi": "rad", "a": "", "alpha": "rad",
                                 "tau": "s"},
                 "a notch-type resonator's complex S21 with the cable delay (circle fit, Probst et al. 2015): give "
                 "the phase with phase (a column or trace in deg or rad); with the magnitude alone, the delay and "
                 "alpha drop out",
                 lower={"Ql": 0.0, "Qc": 0.0, "a": 0.0}, derived=_notch_derived, complex=True,
                 sizes=lambda f, z: {"tau": 0.01 / (2 * math.pi * (float(_np().ptp(f)) or 1.0)), "phi": 0.01,
                                     "alpha": 0.01})


def _resonator_magnitude() -> Model:
    """The notch from |S21| alone (linear, not dB): the dip's depth and width give Ql and |Qc|, its asymmetry phi."""
    def f(x: Any, p: Dict[str, Any]) -> Any:
        return _np().abs(_notch(_np(), x, dict(p, alpha=0.0, tau=0.0)))

    def guess(x: Any, y: Any) -> Dict[str, float]:
        np = _np()
        edge = max(3, y.size // 10)
        a = _median(np, np.concatenate([y[:edge], y[-edge:]]))
        power = (y / (a or 1.0)) ** 2
        b, amp, c, w, k = _peak(np, x, power)
        depth = min(max(float(y[k]) / (a or 1.0), 0.0), 0.99)
        ql = c / max(w, 1e-300)
        return {"fr": c, "Ql": ql, "Qc": ql / max(1 - depth, 1e-3), "phi": 0.0, "a": a}
    m = _resonator()
    return Model("resonator", ("fr", "Ql", "Qc", "phi", "a"), f, "|S21| = " + m.formula.replace("·exp(i(alpha - 2π f "
                 "tau))", ""), guess, m.units, "a notch-type resonator from its |S21| alone (no phase given)",
                 lower={"Ql": 0.0, "Qc": 0.0, "a": 0.0}, derived=_notch_derived, sizes=lambda f, z: {"phi": 0.01})

# ------------------------------------------------------------------ transport and superconductors


def _graphene() -> Model:
    def f(x: Any, p: Dict[str, Any]) -> Any:
        np = _np()
        n = p["C_g"] * (x - p["V_D"]) / QE
        return p["R_c"] + p["aspect"] / (QE * abs(p["mu"]) * np.sqrt(p["n0"] ** 2 + n * n))

    def guess(x: Any, y: Any) -> Dict[str, float]:
        np = _np()
        k = int(np.argmax(y))
        vd = float(x[k])
        far = int(np.argmax(np.abs(x - vd)))
        rc = float(y.min()) * 0.3
        return {"R_c": rc, "V_D": vd, "_far": float(abs(x[far] - vd)), "_rfar": float(y[far] - rc),
                "_rmax": float(y[k] - rc)}

    def derived(v: Dict[str, float], xu: str, yu: str) -> List[Tuple[str, float, str]]:
        return [("mobility", abs(v["mu"]) * 1e4, "cm²/(V·s)"), ("residual density n0", v["n0"] * 1e-4, "cm⁻²")]
    return Model("graphene", ("R_c", "mu", "n0", "V_D", "C_g", "aspect"), f,
                 "R_c + aspect / (e·mu·sqrt(n0² + (C_g (x - V_D)/e)²))", guess,
                 lambda xu, yu: {"R_c": yu or "Ω", "mu": "m²/(V·s)", "n0": "m⁻²", "V_D": xu or "V", "C_g": "F/m²",
                                 "aspect": ""},
                 "a graphene device's resistance against gate voltage: contact resistance, mobility, the residual "
                 "(puddle) density and the Dirac point. fix C_g (the gate's capacitance per area, F/m²; 285 nm "
                 "SiO2: 1.21e-4) and aspect (length/width of the channel)",
                 lower={"mu": 0.0, "n0": 0.0, "R_c": 0.0}, needs=("C_g", "aspect"), constants=("C_g", "aspect"),
                 derived=derived)


def _hall() -> Model:
    def f(x: Any, p: Dict[str, Any]) -> Any:
        return p["R0"] + p["R_H"] * x

    def guess(x: Any, y: Any) -> Dict[str, float]:
        np = _np()
        b, a = np.polyfit(x, y, 1) if x.size > 1 and np.ptp(x) > 0 else (0.0, float(y.mean()))
        return {"R0": float(a), "R_H": float(b)}

    def derived(v: Dict[str, float], xu: str, yu: str) -> List[Tuple[str, float, str]]:
        out: List[Tuple[str, float, str]] = []
        if v["R_H"]:
            out.append(("carrier density n = 1/(e·|R_H|)", 1e-4 / (QE * abs(v["R_H"])), "cm⁻²"))
            out.append(("carriers", -1.0 if v["R_H"] < 0 else 1.0, "electrons" if v["R_H"] < 0 else "holes"))
            if v.get("rho_xx"):
                out.append(("Hall mobility |R_H|/rho_xx", abs(v["R_H"]) / v["rho_xx"] * 1e4, "cm²/(V·s)"))
        return out
    return Model("hall", ("R0", "R_H", "rho_xx"), f, "R0 + R_H·B", guess,
                 lambda xu, yu: {"R0": yu or "Ω", "R_H": _per(yu or "Ω", xu or "T"), "rho_xx": "Ω"},
                 "the Hall resistance against field: the sheet density n = 1/(e|R_H|) (the sign says electrons or "
                 "holes) and, with fix rho_xx (the sheet resistance at zero field, Ω per square), the Hall mobility. "
                 "x must be the field in tesla",
                 held={"rho_xx": 0.0}, constants=("rho_xx",), derived=derived)


def _gl_hc2() -> Model:
    def f(x: Any, p: Dict[str, Any]) -> Any:
        return p["Bc2_0"] * (1 - (x / p["Tc"]) ** 2)

    def guess(x: Any, y: Any) -> Dict[str, float]:
        np = _np()
        k = int(np.argmax(x))
        tc = float(x[k]) * 1.1 if y[k] > 0 else float(x[k])
        return {"Tc": tc, "Bc2_0": float(y.max()) / max(1 - (float(x[int(np.argmax(y))]) / tc) ** 2, 0.05)}

    def derived(v: Dict[str, float], xu: str, yu: str) -> List[Tuple[str, float, str]]:
        return [("GL coherence length xi(0)", math.sqrt(PHI0 / (2 * math.pi * abs(v["Bc2_0"]))) if v["Bc2_0"] else
                 math.inf, "m")]
    return Model("gl_hc2", ("Bc2_0", "Tc"), f, "Bc2_0·(1 - (T/Tc)²)", guess,
                 lambda xu, yu: {"Bc2_0": yu or "T", "Tc": xu or "K"},
                 "the upper critical field against temperature, Ginzburg-Landau's empirical parabola (x: T in K, "
                 "y: Bc2 in T)", lower={"Tc": 0.0}, derived=derived)


def _whh_h(t: Any) -> Any:
    """WHH's reduced field h(t) = Bc2(T) / (Tc·|dBc2/dT| at Tc), dirty limit without spin effects: the root of
    ln(1/t) = ψ(1/2 + 2h/(π² t)) - ψ(1/2), by bisection (h(0) = 0.693)."""
    np = _np()
    t = np.clip(np.asarray(t, dtype=float), 1e-6, 1.0)
    lo, hi = np.zeros_like(t), np.full_like(t, 1.0)
    target = np.log(1 / t)
    psi_half = float(digamma(np.array([0.5]))[0])
    for _ in range(60):
        mid = (lo + hi) / 2
        g = digamma(0.5 + 2 * mid / (math.pi ** 2 * t)) - psi_half
        lo, hi = np.where(g < target, mid, lo), np.where(g < target, hi, mid)
    return (lo + hi) / 2


def _whh_hc2() -> Model:
    def f(x: Any, p: Dict[str, Any]) -> Any:
        np = _np()
        t = np.asarray(x, dtype=float) / p["Tc"]
        return np.where(t < 1, p["Tc"] * p["S"] * _whh_h(t), 0.0)

    def guess(x: Any, y: Any) -> Dict[str, float]:
        np = _np()
        top = np.argsort(x)[-max(2, x.size // 3):]
        s, b = np.polyfit(x[top], y[top], 1) if np.ptp(x[top]) > 0 else (-1.0, float(y.max()))
        tc = -b / s if s < 0 else float(x.max()) * 1.1
        return {"Tc": float(tc), "S": float(abs(s)) or 1.0}

    def derived(v: Dict[str, float], xu: str, yu: str) -> List[Tuple[str, float, str]]:
        b0 = 0.693 * v["Tc"] * v["S"]
        return [("Bc2(0), WHH (0.693·Tc·S)", b0, "T"),
                ("GL coherence length xi(0)", math.sqrt(PHI0 / (2 * math.pi * b0)) if b0 > 0 else math.inf, "m")]
    return Model("whh_hc2", ("Tc", "S"), f, "Tc·S·h_WHH(T/Tc)", guess,
                 lambda xu, yu: {"Tc": xu or "K", "S": _per(yu or "T", xu or "K")},
                 "the upper critical field against temperature after Werthamer-Helfand-Hohenberg (dirty limit, no "
                 "spin effects): S is -dBc2/dT at Tc (x: T in K, y: Bc2 in T)", lower={"Tc": 0.0, "S": 0.0},
                 derived=derived)


MODELS: Dict[str, Callable[[], Model]] = {
    "linear": _linear, "lorentzian": _lorentzian, "gaussian": _gaussian, "double_lorentzian": _double_lorentzian,
    "exponential": _exponential, "stretched_exponential": _stretched, "biexponential": _biexponential,
    "exp_irf": _exp_irf, "biexp_irf": _biexp_irf, "damped_cosine": _damped_cosine, "resonator": _resonator,
    "graphene": _graphene, "hall": _hall, "gl_hc2": _gl_hc2, "whh_hc2": _whh_hc2}
ALIASES = {"oscillation": "damped_cosine", "rabi": "damped_cosine", "ramsey": "damped_cosine", "notch": "resonator",
           "circle": "resonator", "decay": "exponential", "line": "linear", "graphene_rn": "graphene",
           "ginzburg_landau": "gl_hc2", "whh": "whh_hc2", "gl": "gl_hc2"}


def models_help() -> str:
    """One line per built-in model, for the tool's description."""
    return "; ".join("%s: %s" % (name, make().formula) for name, make in MODELS.items())


# expressions: names that are constants in a model (c and h, single letters, are left to be parameters)
MODEL_CONSTANTS = {k: v for k, v in expr.CONSTANTS.items() if len(k) > 1}


def _names(text: str) -> List[str]:
    """The names an expression uses, in order (dotted names whole), without the functions it calls."""
    try:
        tree = ast.parse(str(text or "").strip(), mode="eval")
    except SyntaxError as e:
        raise FitError("the model is not a valid expression: %s" % e.msg)
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    out: List[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, (ast.Attribute, ast.Name)):
            try:
                name = expr._dotted(node)
            except expr.ExprError:
                name = ""
            if name and name not in out and name not in called:
                out.append(name)
            return
        for child in ast.iter_child_nodes(node):
            visit(child)
    visit(tree)
    return out


def expression_model(text: str, columns: Sequence[str] = (), x_name: str = "x") -> Model:
    """The person's model: an expression in x (and the run's columns) whose other names are its parameters."""
    np = _np()
    text = str(text or "").strip()
    names = _names(text)
    funcs = expr.functions(np)
    cols = tuple(n for n in names if n in columns and n != x_name)
    params = tuple(n for n in names if n not in (x_name, "x") and n not in cols and n not in MODEL_CONSTANTS
                   and n not in funcs)
    if "x" not in names and x_name not in names and not cols:
        raise FitError("the model must use x (the x values), e.g. a*exp(-x/tau) + c")
    if not params:
        raise FitError("the model has no parameter to fit: every name in it is x, a column or a constant")

    def f(x: Any, p: Dict[str, Any], extra: Optional[Dict[str, Any]] = None) -> Any:
        def column(key: str) -> Any:
            if key in ("x", x_name):
                return x
            if key in p:
                return p[key]
            if extra is not None and key in extra:
                return extra[key]
            if key in MODEL_CONSTANTS:
                return np.float64(MODEL_CONSTANTS[key])
            raise KeyError("no column %r" % key)
        return expr.evaluate(text, column, np)
    return Model("expression", params, f, text, lambda x, y: {}, lambda xu, yu: {}, columns=cols)

# ------------------------------------------------------------------ fitting


def _as_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def fit(model: Model, x: Any, y: Any, sigma: Any = None, start: Optional[Dict[str, Any]] = None,
        fix: Optional[Dict[str, Any]] = None, bounds: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None, x_unit: str = "", y_unit: str = "",
        deadline: Optional[float] = None) -> Dict[str, Any]:
    """Fit model to the points (x, y), y complex for a complex model; sigma: each point's one-sigma error (or one
    number for all). start: starting values; fix: values held; bounds: {name: [low, high]}. Returns the result
    (fit_text writes it out): values, errors, covariance, chi-squared, residuals and what to look out for."""
    np = _np()
    start, fix, bounds = dict(start or {}), dict(fix or {}), dict(bounds or {})
    unknown = [k for k in list(start) + list(fix) + list(bounds) if k not in model.params]
    if unknown:
        raise FitError("%s has no parameter %s (its parameters: %s)" % (
            "the model" if model.name == "expression" else model.name, ", ".join(sorted(set(unknown))),
            ", ".join(model.params)))
    missing = [k for k in model.needs if k not in fix]
    if missing:
        raise FitError("%s needs %s held at known values: give fix: {%s}" % (
            model.name, " and ".join(missing), ", ".join("%s: ..." % k for k in missing)))
    loose = [k for k in model.constants if k in start or k in bounds]
    if loose:
        raise FitError("%s %s known, not fitted: give %s with fix" % (
            " and ".join(loose), "is" if len(loose) == 1 else "are", "it" if len(loose) == 1 else "them"))
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=complex if model.complex else float)
    if x.shape != y.shape:
        raise FitError("x and y have different numbers of points (%d and %d)" % (x.size, y.size))
    sig = None
    if sigma is not None:
        sig = np.broadcast_to(np.asarray(sigma, dtype=float), x.shape).astype(float)
    ok = np.isfinite(x) & np.isfinite(y.real) & np.isfinite(y.imag)
    if extra:
        for v in extra.values():
            ok &= np.isfinite(np.asarray(v, dtype=float))
    if sig is not None:
        bad = ok & ~(np.isfinite(sig) & (sig > 0))
        if bad.any():
            raise FitError("sigma must be a positive number at every point (%d points have none)" % int(bad.sum()))
    order = np.argsort(x[ok], kind="stable")
    x, y = x[ok][order], y[ok][order]
    sig = sig[ok][order] if sig is not None else None
    extra = {k: np.asarray(v, dtype=float)[ok][order] for k, v in (extra or {}).items()}
    n = x.size
    k_all = len(model.params)
    if n == 0:
        raise FitError("no points to fit (none have finite x and y)")
    if x.size > MAX_POINTS:
        raise FitError("%d points is more than a fit takes (%d): narrow the window" % (x.size, MAX_POINTS))
    x0 = float(x[0]) if model.x_ref else 0.0
    xs = x - x0
    held = {k: v for k, v in model.held.items() if k not in start}
    held.update(fix)
    for k, v in held.items():
        if _as_float(v) is None:
            raise FitError("%s must be held at a number, not %r" % (k, v))
    free_names = [k for k in model.params if k not in held]
    if n < len(free_names) + 1:
        raise FitError("%d points can't fit %d parameters" % (n, len(free_names)))
    guess = model.guess(xs, y) if model.guess else {}
    if model.name == "graphene":   # mobility and puddle density from the curve's tails and peak, given C_g and aspect
        cg, aspect = float(held["C_g"]), float(held["aspect"])
        guess["mu"] = aspect / max(cg * guess["_far"] * max(guess["_rfar"], 1e-30), 1e-300)
        guess["n0"] = aspect / max(QE * guess["mu"] * max(guess["_rmax"], 1e-30), 1e-300)
    p0 = []
    no_start = []
    for k in model.params:
        if k in held:
            p0.append(float(held[k]))
        elif k in start:
            v = _as_float(start[k])
            if v is None:
                raise FitError("start: %s must be a number" % k)
            p0.append(v)
        elif k in guess and _as_float(guess[k]) is not None:
            p0.append(float(guess[k]))
        else:
            p0.append(1.0)
            no_start.append(k)
    lower =[float(bounds[k][0]) if k in bounds and bounds[k][0] is not None else model.lower.get(k, -math.inf)
             for k in model.params]
    upper = [float(bounds[k][1]) if k in bounds and bounds[k][1] is not None else model.upper.get(k, math.inf)
             for k in model.params]
    for k, lo, hi in zip(model.params, lower, upper):
        if lo > hi:
            raise FitError("bounds for %s: the low end is above the high end" % k)
    span_x = float(np.ptp(xs)) or 1.0
    span_y = float(np.ptp(np.abs(y))) or 1.0
    sizes = model.sizes(xs, y) if model.sizes else {}
    named_units = model.units(x_unit, y_unit) if model.name != "expression" else {}
    scale = []
    for k, v in zip(model.params, p0):
        u = named_units.get(k)
        scale.append(sizes.get(k) or abs(v) or (span_x if u and u == x_unit else span_y if u and u == y_unit
                                                 else 1.0))
    free = [model.params.index(k) for k in free_names]

    def values(p: Any) -> Dict[str, Any]:
        return dict(zip(model.params, p))

    def evaluate(p: Any, at: Any = None, cols: Optional[Dict[str, Any]] = None) -> Any:
        at = xs if at is None else at
        if model.name == "expression":
            return model.f(at, values(p), extra if cols is None else cols)
        return model.f(at, values(p))

    weights = 1.0 / sig if sig is not None else 1.0

    def resid(p: Any) -> Any:
        r = (evaluate(p) - y) * weights
        if model.complex:
            return np.concatenate([np.real(r), np.imag(r)])
        return np.broadcast_to(r, (n,)) if np.ndim(r) == 0 else r

    sol = least_squares(resid, p0, free, lower, upper, scale, deadline=deadline)
    p = sol.p
    m = 2 * n if model.complex else n
    dof = max(1, m - len(free))
    chi2 = sol.cost
    red = chi2 / dof
    cov = sol.cov * (1.0 if sig is not None else red)
    errs = np.full(k_all, np.nan)
    errs[free] = np.sqrt(np.clip(np.diag(cov), 0, None)) if len(free) else []
    fitted = np.asarray(evaluate(p))
    fitted = np.broadcast_to(fitted, y.shape) if np.ndim(fitted) == 0 else fitted
    res = y - fitted
    plain = np.abs(res) ** 2 if model.complex else res ** 2
    yy = np.abs(y - y.mean()) ** 2 if model.complex else (y - y.mean()) ** 2
    r2 = 1 - float(plain.sum()) / (float(yy.sum()) or 1e-300)
    units = model.units(x_unit, y_unit) if model.name != "expression" else {}
    out: Dict[str, Any] = {
        "model": model.name, "formula": model.formula, "n": int(n), "dof": int(dof), "chi2": float(chi2),
        "chi2_red": float(red), "r2": float(r2), "weighted": sig is not None, "converged": bool(sol.converged),
        "message": sol.message, "iterations": int(sol.iterations), "x_start": x0 if model.x_ref else None,
        "aic": float(m * math.log(max(chi2, 1e-300) / m) + 2 * len(free)),
        "bic": float(m * math.log(max(chi2, 1e-300) / m) + len(free) * math.log(m)),
        "params": [], "derived": [], "free": [model.params[j] for j in free],
        "covariance": cov.tolist(), "warnings": [], "x_unit": x_unit, "y_unit": y_unit,
        "x": x.tolist(), "y": _plain(np, y), "fit": _plain(np, fitted), "residuals": _plain(np, res),
        "sigma": sig.tolist() if sig is not None else None}
    for j, k in enumerate(model.params):
        at_bound = bool(j in free and (p[j] <= lower[j] or p[j] >= upper[j]) and math.isfinite(
            lower[j] if p[j] <= lower[j] else upper[j]))
        out["params"].append({"name": k, "value": float(p[j]), "error": None if not math.isfinite(errs[j]) else
                              float(errs[j]), "unit": units.get(k, ""), "held": k in held, "at_bound": at_bound})
    _warnings(np, out, sol, model, free, cov, no_start, res, sig)
    if model.derived:
        out["derived"] = _derived(np, model, p, free, cov, x_unit, y_unit)
    return out


def _plain(np: Any, a: Any) -> Any:
    """An array as JSON can keep it: complex values as [re, im] pairs."""
    a = np.asarray(a)
    if np.iscomplexobj(a):
        return np.stack([a.real, a.imag], axis=-1).tolist()
    return a.tolist()


def _warnings(np: Any, out: Dict[str, Any], sol: Solution, model: Model, free: List[int], cov: Any,
              no_start: List[str], res: Any, sig: Any) -> None:
    w = out["warnings"]
    if not sol.converged:
        w.append("the fit %s: its numbers are where it stopped, not a minimum" % (sol.message or "did not converge"))
    if sol.rank < len(free):
        w.append("the data can't tell some parameters apart (the fit's Jacobian has rank %d for %d free parameters): "
                 "hold one fixed or measure a wider range" % (sol.rank, len(free)))
    if no_start:
        w.append("%s started from 1 (no starting value given): give start values of the right size"
                 % ", ".join(no_start))
    for prm in out["params"]:
        if prm["at_bound"]:
            w.append("%s ended at its bound (%g): the best value may lie beyond it" % (prm["name"], prm["value"]))
    d = np.sqrt(np.clip(np.diag(cov), 1e-300, None)) if cov.size else np.zeros(0)
    names = [model.params[j] for j in free]
    for a in range(len(free)):
        for b in range(a + 1, len(free)):
            rho = cov[a, b] / (d[a] * d[b]) if d[a] and d[b] else 0.0
            if abs(rho) > 0.98:
                w.append("%s and %s are %d%% correlated: the data hardly separate them" % (
                    names[a], names[b], round(100 * abs(rho))))
    if out["weighted"]:
        red = out["chi2_red"]
        if red > 3:
            w.append("reduced chi-squared %.3g: the residuals are larger than the sigma given (or the model misses "
                     "something)" % red)
        elif red < 0.2:
            w.append("reduced chi-squared %.3g: the sigma given are larger than the scatter" % red)
    if np.iscomplexobj(res):
        return
    signs = res > np.median(res)
    n1, n2 = int(signs.sum()), int((~signs).sum())
    if n1 > 5 and n2 > 5:   # Wald-Wolfowitz runs test on the residuals' signs
        runs = 1 + int(np.count_nonzero(signs[1:] != signs[:-1]))
        mu = 2.0 * n1 * n2 / (n1 + n2) + 1
        var = (mu - 1) * (mu - 2) / (n1 + n2 - 1)
        z = (runs - mu) / math.sqrt(var) if var > 0 else 0.0
        out["runs_z"] = round(z, 2)
        if z < -4:
            w.append("the residuals run in long stretches above and below zero (runs test z = %.1f): the model "
                     "misses part of the shape" % z)


def _derived(np: Any, model: Model, p: Any, free: List[int], cov: Any, xu: str, yu: str) -> List[Dict[str, Any]]:
    """The model's derived quantities, their errors propagated through the covariance (a numerical gradient)."""
    def at(q: Any) -> List[Tuple[str, float, str]]:
        try:
            return model.derived(dict(zip(model.params, q)), xu, yu) if model.derived else []
        except (ZeroDivisionError, ValueError, OverflowError):
            return []
    base = at(p)
    out = []
    for i, (name, value, unit) in enumerate(base):
        grad = np.zeros(len(free))
        for col, j in enumerate(free):
            h = 1e-6 * max(abs(p[j]), 1e-30)
            q = p.copy()
            q[j] += h
            moved = at(q)
            grad[col] = (moved[i][1] - value) / h if i < len(moved) else 0.0
        var = float(grad @ cov @ grad) if len(free) else 0.0
        err = math.sqrt(var) if math.isfinite(var) and var > 0 and unit not in ("electrons", "holes") else None
        out.append({"name": name, "value": float(value), "error": err, "unit": unit})
    return out


def curve(model: Model, result: Dict[str, Any], points: int = CURVE_POINTS) -> Tuple[List[float], List[Any]]:
    """The fitted model drawn smoothly over the data's x range (only at the data's own x for a model that reads
    other columns of the run too)."""
    np = _np()
    x = np.asarray(result["x"], dtype=float)
    if model.columns or x.size < 2:
        return list(result["x"]), list(result["fit"])
    p = {q["name"]: q["value"] for q in result["params"]}
    x0 = result.get("x_start") or 0.0
    grid = np.linspace(float(x.min()), float(x.max()), points)
    with np.errstate(all="ignore"):
        y = model.f(grid - x0, p, None) if model.name == "expression" else model.f(grid - x0, p)
    y = np.broadcast_to(np.asarray(y), grid.shape)
    return grid.tolist(), _plain(np, y)

# ------------------------------------------------------------------ in words


def fmt(value: Optional[float], err: Optional[float], unit: str) -> str:
    from .analysis import _fmt_pm
    if value is None:
        return "-"
    if unit in ("cm⁻²", "cm²/(V·s)", "m⁻²", "m²/(V·s)"):   # densities and mobilities read as powers of ten
        if err is None or not math.isfinite(err):
            return "%.4g %s" % (value, unit)
        exp = int(math.floor(math.log10(abs(value)))) if value else 0
        if -3 <= exp <= 3:
            digits = max(0, min(6, 1 - int(math.floor(math.log10(err))))) if err > 0 else 3
            return "%.*f ± %.*f %s" % (digits, value, digits, err, unit)
        m, me = value / 10 ** exp, err / 10 ** exp
        digits = max(0, min(9, 1 - int(math.floor(math.log10(me))))) if me > 0 else 3
        return "(%.*f ± %.*f)e%d %s" % (digits, m, digits, me, exp, unit)
    if unit in ("electrons", "holes"):
        return unit
    if unit in ("", "rad") and value is not None and err is None:
        return "%.6g%s" % (value, " " + unit if unit else "")
    return _fmt_pm(value, err, unit)


def fit_text(result: Dict[str, Any], what: str = "") -> List[str]:
    """A fit in lines: its parameters with errors, what follows from them, how good it is, what to watch."""
    lines = ["%s%s fit (%s) to %d points: chi² %.4g for %d degrees of freedom (reduced %.3g)%s; R² %.5f" % (
        what + ": " if what else "", result["model"], result["formula"], result["n"], result["chi2"], result["dof"],
        result["chi2_red"], ", weighted by the sigma given" if result["weighted"] else
        ", unweighted (errors from the scatter)", result["r2"])]
    if result.get("x_start"):
        lines.append("x is counted from x_start = %.6g (the window's first x)" % result["x_start"])
    for p in result["params"]:
        lines.append("  %s = %s%s" % (p["name"], fmt(p["value"], p["error"], p["unit"]),
                                      " (held)" if p["held"] else " (at its bound)" if p["at_bound"] else ""))
    for d in result["derived"]:
        lines.append("  %s = %s" % (d["name"], fmt(d["value"], d["error"], d["unit"])))
    for w in result["warnings"]:
        lines.append("warning: " + w)
    return lines

# ------------------------------------------------------------------ kept with the run (run.h5: /fits/<name>)


def _attr(v: Any) -> Any:
    if isinstance(v, (str, bool, int, float)):
        return v
    return json.dumps(v, ensure_ascii=False)


def store(folder: Path, name: str, result: Dict[str, Any], spec: Dict[str, Any], by: str = "agent") -> str:
    """Keep a fit in the run's run.h5 as /fits/<name> (replacing one of that name): the model and how it was asked
    for as attributes, the values, errors and covariance, and the points with the model and residuals at each."""
    import h5py
    np = _np()
    from .rundata import RUN_FILE
    name = "".join(c if c.isalnum() or c in "._-=@ " else "_" for c in str(name).strip())[:80] or result["model"]
    path = Path(folder) / RUN_FILE
    data = {k: np.asarray(result[k], dtype=float) for k in ("x", "y", "fit", "residuals") if result.get(k) is not None}
    if result.get("sigma") is not None:
        data["sigma"] = np.asarray(result["sigma"], dtype=float)
    attrs = {"model": result["model"], "formula": result["formula"], "n": result["n"], "dof": result["dof"],
             "chi2": result["chi2"], "chi2_red": result["chi2_red"], "r2": result["r2"], "aic": result["aic"],
             "bic": result["bic"], "weighted": result["weighted"], "converged": result["converged"],
             "message": result["message"] or "", "params": [p["name"] for p in result["params"]],
             "values": [p["value"] for p in result["params"]],
             "errors": [p["error"] for p in result["params"]], "units": [p["unit"] for p in result["params"]],
             "held": [p["name"] for p in result["params"] if p["held"]], "free": result["free"],
             "derived": result["derived"], "warnings": result["warnings"], "x_start": result.get("x_start"),
             "x_unit": result.get("x_unit", ""), "y_unit": result.get("y_unit", ""),
             "spec": {k: v for k, v in spec.items() if v not in (None, "", [], {})},
             "stored": datetime.now().isoformat(timespec="seconds"), "by": by}
    last: Optional[Exception] = None
    for _ in range(20):   # another reader may hold the file for a moment
        try:
            with _STORE, h5py.File(path, "r+") as f:
                fits = f.require_group("fits")
                if name in fits:
                    del fits[name]
                g = fits.create_group(name)
                for k, v in attrs.items():
                    if v is not None:
                        g.attrs[k] = _attr(v)
                for k, v in data.items():
                    g.create_dataset(k, data=v)
                g.create_dataset("covariance", data=np.asarray(result["covariance"], dtype=float))
            return name
        except (OSError, BlockingIOError) as e:
            last = e
            time.sleep(0.1)
    raise FitError("could not write run.h5: %s" % last)


def stored(folder: Path, with_data: bool = False) -> List[Dict[str, Any]]:
    """The fits kept in a run's run.h5, oldest first (attributes read back as values)."""
    from .rundata import RUN_FILE, have_h5
    path = Path(folder) / RUN_FILE
    if not path.exists() or not have_h5():
        return []
    import h5py
    out = []
    with h5py.File(path, "r") as f:
        for name, g in (f["fits"].items() if "fits" in f else []):
            item: Dict[str, Any] = {"name": name}
            for k, v in g.attrs.items():
                if isinstance(v, bytes):
                    v = v.decode("utf-8")
                if isinstance(v, str) and v[:1] in "[{":
                    try:
                        v = json.loads(v)
                    except ValueError:
                        pass
                item[k] = v.item() if hasattr(v, "item") else v
            if with_data:
                for k in g:
                    item[k] = g[k][()].tolist()
            out.append(item)
    return sorted(out, key=lambda i: str(i.get("stored", "")))
