"""Clocks. The simulator runs on SimClock, so a 20-minute sweep takes milliseconds."""
from __future__ import annotations

import time


class RealClock:
    simulated = False

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class ScaledClock:
    """Real time sped up by `speed`: a 10-minute magnet ramp takes 1 minute at speed 10."""
    simulated = True

    def __init__(self, speed: float):
        if speed <= 0:
            raise ValueError("speed must be > 0")
        self.speed = float(speed)
        self._t0 = time.monotonic()

    def now(self) -> float:
        return (time.monotonic() - self._t0) * self.speed

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds / self.speed)


class SimClock:
    simulated = True

    def __init__(self, start: float = 0.0):
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._t += seconds
