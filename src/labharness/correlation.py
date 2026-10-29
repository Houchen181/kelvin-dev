"""Photon correlations: g2(τ) from a Hanbury Brown–Twiss measurement, and the fit that says what it shows.

A single quantum emitter can't give two photons at once, so the coincidences between two detectors behind a beam
splitter dip at zero delay. The fit is the textbook one for an emitter pumped below saturation:

    g2(τ) = A · (1 − ρ · exp(−|τ − τ0| / τc))

A is the normalisation (1 when the counts are normalised to uncorrelated light), τ0 the detectors' delay offset,
τc the antibunching time (the emitter's lifetime and pump rate together), and g2(0) = 1 − ρ: below 0.5, one emitter.
ρ < 0 is bunching (g2(0) > 1). The fit weighs each delay bin by its Poisson error when the raw coincidences are
given, and its errors (one standard deviation) are scaled by the reduced χ², as a least-squares fit's are.

Pure NumPy (the Time Tagger's own library brings it), so it runs wherever correlations are taken."""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Sequence


def _model(np: Any, x: Any, p: Sequence[float]) -> Any:
    a, rho, t0, tc = p
    return a * (1.0 - rho * np.exp(-np.abs(x - t0) / tc))


def _jacobian(np: Any, x: Any, p: Sequence[float]) -> Any:
    a, rho, t0, tc = p
    e = np.exp(-np.abs(x - t0) / tc)
    return np.stack([1.0 - rho * e, -a * e, -a * rho * e * np.sign(x - t0) / tc,
                     -a * rho * e * np.abs(x - t0) / tc ** 2], axis=1)


def fit_g2(delays: Sequence[float], g2: Sequence[float], counts: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """Fit g2(τ) (delays in seconds). Returns g2_zero, g2_zero_err, tau, tau_err, offset (τ0), norm (A),
    chi2_red and points; ValueError when there is nothing to fit (too few delays, no dip or peak to speak of is fine:
    the fit then says g2(0) ≈ 1)."""
    import numpy as np
    x = np.asarray(delays, dtype=float)
    y = np.asarray(g2, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    if counts is not None:
        n = np.asarray(counts, dtype=float)
        ok &= np.isfinite(n)
        # each bin's error from its own coincidences, at least one count's worth, so an empty bin weighs as one
        sigma = np.where(n > 0, y / np.sqrt(np.maximum(n, 1.0)), np.nan)
        floor = np.nanmedian(np.where(n > 0, y / np.maximum(n, 1.0), np.nan)) if np.any(n > 0) else 1.0
        sigma = np.where(np.isfinite(sigma) & (sigma > 0), sigma, floor)
    else:
        sigma = np.ones_like(y)
    x, y, sigma = x[ok], y[ok], sigma[ok]
    if x.size < 8:
        raise ValueError("too few delays to fit g2 (%d)" % x.size)
    span = float(x.max() - x.min())
    wings = np.abs(x - np.median(x)) > span / 4
    if counts is not None:   # fewer than one coincidence a bin away from zero delay: nothing a fit could stand on
        per_bin = float(np.mean(np.asarray(counts, dtype=float)[ok][wings])) if np.any(wings) else 0.0
        if per_bin < 1.0:
            raise ValueError("too few coincidences to fit (%.2g a bin): integrate longer or use wider bins" % per_bin)
    a0 = float(np.median(y[wings])) if np.any(wings) else float(np.median(y))
    if not a0 > 0:
        raise ValueError("no coincidences to fit")
    k = max(1, x.size // 50)   # the dip's centre from a lightly smoothed curve, not one noisy bin
    smooth = np.convolve(y, np.ones(2 * k + 1) / (2 * k + 1), mode="same")
    centre = np.abs(x - np.median(x)) < span / 4   # the dip (or peak) is where the curve is furthest from the wings
    i0 = int(np.argmax(np.where(centre, np.abs(smooth - a0), -np.inf)))
    t0 = float(x[i0])
    rho0 = float(np.clip(1.0 - smooth[i0] / a0, -5.0, 0.99))
    step = float(np.median(np.diff(np.sort(x)))) if x.size > 1 else span / 100
    best = None
    for tc0 in (2 * step, 5 * step, 15 * step, span / 20):   # a few starting widths: the closest fit wins
        if tc0 <= 0:
            continue
        r = _levenberg(np, x, y, sigma, [a0, rho0, t0, min(tc0, span / 6)], step, span / 6)
        if r is not None and (best is None or r[1] < best[1]):
            best = r
    if best is None:
        raise ValueError("the g2 fit did not converge")
    p, chi2, cov = best
    dof = max(1, x.size - 4)
    scale = max(1.0, chi2 / dof) if counts is not None else chi2 / dof   # Poisson weights: never shrink the errors
    err = np.sqrt(np.maximum(np.diag(cov) * scale, 0.0))
    a, rho, t0, tc = p
    # g2(0) = 1 - ρ in units of the fitted normalisation: its error from ρ's
    return {"g2_zero": float(1.0 - rho), "g2_zero_err": float(err[1]), "tau": float(abs(tc)), "tau_err": float(err[3]),
            "offset": float(t0), "offset_err": float(err[2]), "norm": float(a), "norm_err": float(err[0]),
            "chi2_red": float(chi2 / dof), "points": int(x.size), "model": "g2 = A(1 - rho exp(-|t - t0|/tc))"}


def _levenberg(np: Any, x: Any, y: Any, sigma: Any, p0: Sequence[float], step: float, longest: float,
               iters: int = 200):
    """Levenberg–Marquardt on the weighted residuals; (parameters, χ², covariance) or None. τc stays between a tenth
    of a bin and `longest` (a sixth of the window): a dip wider than that can't be told from the wings, and a flat
    curve would otherwise trade ρ against τc without end."""
    p = np.array(p0, dtype=float)
    w = 1.0 / sigma

    def chi2(q: Any) -> float:
        return float(np.sum(((y - _model(np, x, q)) * w) ** 2))

    def fix(q: Any) -> Any:
        q = q.copy()
        q[3] = min(max(abs(q[3]), step / 10), longest)
        return q
    lam, c = 1e-3, chi2(p)
    for _ in range(iters):
        j = _jacobian(np, x, p) * w[:, None]
        r = (y - _model(np, x, p)) * w
        jtj, jtr = j.T @ j, j.T @ r
        try:
            delta = np.linalg.solve(jtj + lam * np.diag(np.diag(jtj) + 1e-30), jtr)
        except np.linalg.LinAlgError:
            return None
        trial = fix(p + delta)
        ct = chi2(trial)
        if ct < c:
            done = abs(c - ct) <= 1e-10 * max(c, 1e-30)
            p, c, lam = trial, ct, max(lam / 5, 1e-12)
            if done:
                break
        else:
            lam *= 5
            if lam > 1e12:
                break
    j = _jacobian(np, x, p) * w[:, None]
    try:
        cov = np.linalg.inv(j.T @ j)
    except np.linalg.LinAlgError:
        return None
    if not all(math.isfinite(v) for v in p):
        return None
    return p, c, cov


def verdict(fit: Dict[str, Any]) -> str:
    """What the fit says, in a line: one emitter, several, or uncorrelated or bunched light."""
    g0, err = fit["g2_zero"], fit["g2_zero_err"]
    shown = "g2(0) = %.2f ± %.2f, τc = %.2g ns" % (g0, err, fit["tau"] * 1e9)
    if g0 + 2 * err < 0.5:
        return shown + ": antibunched below 0.5, a single emitter"
    if g0 + 2 * err < 1:
        return shown + ": antibunched, but not below 0.5 (more than one emitter, or background)"
    if g0 - 2 * err > 1:
        return shown + ": bunched"
    return shown + ": no dip (uncorrelated light)"
