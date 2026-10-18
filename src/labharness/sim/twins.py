"""Twins with simple physics for the virtual lab's common instruments.

The physics is a toy: it is there so procedures (limits, order, timing,
guards, data shape) can be exercised, not to predict a real sample.
"""
from __future__ import annotations

import math
from typing import List

from .twin import Twin


class SourceMeterTwin(Twin):
    def measure_I(self) -> float:
        self.take_time(self.state["nplc"] / 60.0)
        if not self.state["output"]:
            return self.world.noise(1e-14)
        i = self.world.gate_leakage(self.state["V"]) + self.world.noise(2e-13)
        c = self.state["compliance_I"]
        return max(-c, min(c, i))


class LockInTwin(Twin):
    def _signal(self):
        v = self.state["amplitude"] / 1e7 * self.world.resistance()  # 10 MΩ series resistor
        sigma = 2e-9 / math.sqrt(max(self.state["tc"], 1e-5))
        s = self.state["sensitivity"]
        x = max(-s, min(s, v + self.world.noise(sigma)))
        y = max(-s, min(s, 0.02 * v + self.world.noise(sigma)))
        return x, y

    def measure_X(self) -> float:
        return self._signal()[0]

    def measure_Y(self) -> float:
        return self._signal()[1]

    def measure_R(self) -> float:
        return math.hypot(*self._signal())


class TemperatureControllerTwin(Twin):
    BASE = 1.6
    TAU = 15.0
    CEILING = {"off": 1.6, "low": 10.0, "medium": 60.0, "high": 325.0}

    def evolve(self, dt: float) -> None:
        rng = self.state["heater_range"]
        target = self.BASE if rng == "off" else max(self.BASE, min(self.state["setpoint"], self.CEILING[rng]))
        target += 0.02 * self.world.laser_on_sample()  # K per mW
        t = self.state["T_sample"]
        self.state["T_sample"] = t + (target - t) * (1 - math.exp(-dt / self.TAU))

    def measure_T_sample(self) -> float:
        return self.state["T_sample"] + self.world.noise(0.002)


class MagnetSupplyTwin(Twin):
    def evolve(self, dt: float) -> None:
        b, target = self.state["B_actual"], self.state["B"]
        step = self.state["rate"] / 60.0 * dt
        self.state["B_actual"] = target if abs(target - b) <= step else b + math.copysign(step, target - b)


class VNATwin(Twin):
    def measure_S21(self) -> List[float]:
        n, ifbw, avg = self.state["points"], self.state["ifbw"], self.state["averages"]
        self.take_time(n / ifbw * avg)
        f1, f2 = self.state["f_start"], self.state["f_stop"]
        if not self.state["rf"]:
            return [-110 + self.world.noise(1.0) for _ in range(n)]
        b = self.world.field()
        f0 = 4.0e9 * (1 - 3e-4 * b * b)
        kappa = 2e6
        sigma = min(5.0, 0.05 * math.sqrt(1e3 / (ifbw * avg)) * 10 ** ((-10 - self.state["power"]) / 20))
        out = []
        for k in range(n):
            f = f1 + (f2 - f1) * k / (n - 1) if n > 1 else f1
            out.append(-40 + 25 / (1 + (2 * (f - f0) / kappa) ** 2) + self.world.noise(sigma))
        return out


class LaserTwin(Twin):
    def measure_P_detected(self) -> float:
        return 0.5 * self.world.laser_on_sample() + self.world.noise(1e-3)


class MotionStageTwin(Twin):
    def evolve(self, dt: float) -> None:
        x, target = self.state["position_actual"], self.state["position"]
        step = self.state["velocity"] * dt
        self.state["position_actual"] = target if abs(target - x) <= step else x + math.copysign(step, target - x)
