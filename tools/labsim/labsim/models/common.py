"""Wiring instrument ports to the world's terminals, and helpers the models share."""
from __future__ import annotations

import math
from typing import Any, Callable, Optional, Tuple

from ..world import Source


class Wired:
    """Bench `connect: {port: terminal}` or `{port: {to: terminal, series_ohm: R}}`."""
    cfg: dict
    world: Any
    name: str

    def terminal(self, port: str) -> Tuple[Optional[str], float]:
        c = (self.cfg.get("connect") or {}).get(port)
        if isinstance(c, dict):
            return c.get("to"), float(c.get("series_ohm", 0.0))
        return (str(c) if c else None), 0.0

    def put(self, port: str, source: Optional[Source]) -> None:
        term, series = self.terminal(port)
        if source is not None and series:
            source.series = series
        self.world.drive(term, "%s.%s" % (self.name, port), source)

    def dc_on(self, port: str) -> float:
        """The DC voltage the port measures on its terminal."""
        term, _ = self.terminal(port)
        if not term:
            return 0.0
        return self.world.voltage(term).get(0.0, 0j).real if term in ("vxx", "vxy", "pd") else self.world.dc(term)


def limit_source(load: Callable[[float], float], v: float, i_limit: float) -> Tuple[float, float, bool]:
    """A voltage source with a current limit into a load i(v): (v actually applied, i, in compliance)."""
    i = load(v)
    if abs(i) <= i_limit:
        return v, i, False
    lo, hi = 0.0, v
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if abs(load(mid)) > i_limit:
            hi = mid
        else:
            lo = mid
    return lo, math.copysign(i_limit, v if v else 1.0), True


def g6(v: float) -> str:
    return "%.6g" % v


SENS_830 = [2e-9, 5e-9, 1e-8, 2e-8, 5e-8, 1e-7, 2e-7, 5e-7, 1e-6, 2e-6, 5e-6, 1e-5, 2e-5, 5e-5, 1e-4, 2e-4, 5e-4,
            1e-3, 2e-3, 5e-3, 1e-2, 2e-2, 5e-2, 0.1, 0.2, 0.5, 1.0]
TAU_830 = [10e-6, 30e-6, 100e-6, 300e-6, 1e-3, 3e-3, 10e-3, 30e-3, 100e-3, 300e-3, 1.0, 3.0, 10.0, 30.0, 100.0, 300.0,
           1e3, 3e3, 10e3, 30e3]
ENBW_FACTOR = [0.25, 0.125, 3.0 / 32.0, 5.0 / 64.0]   # ENBW * tau for 6, 12, 18, 24 dB/oct
