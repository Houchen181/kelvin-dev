"""The statistics the report uses, in plain Python (no SciPy): confidence intervals and tests between arms.

    wilson(k, n)               95 % interval of a pass rate
    fisher(k1, n1, k2, n2)     two-sided Fisher exact test of two pass rates (as DeepSeek Harness decides prompt
                               changes), episodes taken as independent
    mcnemar(b, c)              exact test on paired episodes (same task, same seed): b = only arm 1 passed,
                               c = only arm 2 passed; it uses the pairing, so it is the sharper test here
    perm_paired(d)             sign-flip permutation test of paired differences (cost, steps, time)
    boot_ci(d)                 bootstrap 95 % interval of a mean
"""
from __future__ import annotations

import math
import random
from typing import List, Sequence, Tuple


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - s) / d), min(1.0, (c + s) / d)


def _hyper(a: int, r1: int, r2: int, c1: int) -> float:
    n = r1 + r2
    return math.comb(r1, a) * math.comb(r2, c1 - a) / math.comb(n, c1)


def fisher(k1: int, n1: int, k2: int, n2: int) -> float:
    """Two-sided Fisher exact p for passes k1 of n1 against k2 of n2."""
    if n1 == 0 or n2 == 0:
        return 1.0
    c1 = k1 + k2
    lo, hi = max(0, c1 - n2), min(n1, c1)
    p0 = _hyper(k1, n1, n2, c1)
    return min(1.0, sum(p for p in (_hyper(a, n1, n2, c1) for a in range(lo, hi + 1)) if p <= p0 * (1 + 1e-9)))


def mcnemar(b: int, c: int) -> float:
    """Exact two-sided McNemar p from the discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def perm_paired(d: Sequence[float], rounds: int = 20000, seed: int = 0) -> float:
    """Two-sided sign-flip permutation p for the mean of paired differences."""
    d = [x for x in d if x is not None and not math.isnan(x)]
    if not d:
        return 1.0
    obs = abs(sum(d) / len(d))
    if len(d) <= 16:   # exact
        hits, total = 0, 0
        for mask in range(2 ** len(d)):
            s = sum(x if (mask >> i) & 1 else -x for i, x in enumerate(d))
            hits += abs(s / len(d)) >= obs - 1e-12
            total += 1
        return hits / total
    rng = random.Random(seed)
    hits = 0
    for _ in range(rounds):
        s = sum(x if rng.random() < 0.5 else -x for x in d)
        hits += abs(s / len(d)) >= obs - 1e-12
    return (hits + 1) / (rounds + 1)


def boot_ci(d: Sequence[float], rounds: int = 5000, seed: int = 0) -> Tuple[float, float]:
    d = [x for x in d if x is not None and not math.isnan(x)]
    if not d:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(d) for _ in d) / len(d) for _ in range(rounds))
    return means[int(0.025 * rounds)], means[int(0.975 * rounds) - 1]


def median(xs: Sequence[float]) -> float:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return float("nan")
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def mean(xs: Sequence[float]) -> float:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else float("nan")


def needed_per_arm(p1: float, p2: float, alpha: float = 0.05, power: float = 0.8) -> int:
    """Episodes per arm a two-sided test of two proportions needs to tell p1 from p2 (normal approximation)."""
    za, zb = 1.959964, {0.8: 0.841621, 0.9: 1.281552}.get(power, 0.841621)
    pbar = (p1 + p2) / 2
    num = (za * math.sqrt(2 * pbar * (1 - pbar)) + zb * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
    return int(math.ceil(num / (p1 - p2) ** 2)) if p1 != p2 else 10 ** 9
