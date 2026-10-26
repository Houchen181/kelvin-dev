"""Clocks. The simulator runs on SimClock, so a 20-minute sweep takes milliseconds."""
from __future__ import annotations

import threading
import time
from typing import Any


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


class Stops:
    """A person's stop, as every long wait in Kelvin sees it: each stop counts one up and wakes what is waiting, so a
    settle, a pause or an instrument's sweep ends within moments of the stop instead of when it would have."""

    def __init__(self) -> None:
        self.count = 0
        self._cv = threading.Condition()

    def pulse(self) -> None:
        with self._cv:
            self.count += 1
            self._cv.notify_all()

    def since(self, count: int) -> bool:
        """Has a stop come since `count` was read?"""
        return self.count != count

    def sleep(self, clock: Any, seconds: float, count: int) -> bool:
        """The clock's sleep, cut short by a stop after `count` was read: True if the whole time passed."""
        if self.count != count:
            return False
        if seconds <= 0:
            return True
        if not isinstance(clock, (RealClock, ScaledClock)):   # simulated time passes at once
            clock.sleep(seconds)
            return self.count == count
        end = time.monotonic() + seconds / getattr(clock, "speed", 1.0)
        with self._cv:
            while self.count == count:
                left = end - time.monotonic()
                if left <= 0:
                    return True
                self._cv.wait(left)
        return False
