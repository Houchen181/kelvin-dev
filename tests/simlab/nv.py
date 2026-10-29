"""A diamond with NV centres for the test lab: what a photon counter sees under each pulse protocol.

The physics is a fair caricature of an NV ensemble, there to exercise the procedure (CW ODMR, then Rabi for the pi
time, then pulsed ODMR, Ramsey, echo, T1) with realistic numbers:
    resonances  f = D -/+ sqrt(E^2 + (gamma B)^2), D = 2.870 GHz, E = 2 MHz strain, gamma = 28 GHz/T (the magnet)
    drive       Rabi frequency 5 MHz at 0 dBm, scaling with the MW amplitude (so a pi pulse is 100 ns at 0 dBm)
    coherence   Rabi decay 1.5 us, T2* = 1 us, T2 = 40 us (echo), T1 = 3 ms
    light       up to 400 kcounts/s, half of it at 2 mW (saturation), 10 % darker when flipped to ms = +/-1
Counts are Poisson-noisy, so the fits have honest uncertainties."""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

from labharness import pulses
from labharness.sim.twin import Twin

D, E, GAMMA = 2.870e9, 2e6, 28.0e9
RABI_0DBM = 5e6
RABI_DECAY, T2STAR, T2, T1 = 1.5e-6, 1.0e-6, 40e-6, 3e-3
R_MAX, P_SAT = 4e5, 2.0
CONTRAST = 0.10
CW_WIDTH = 1e6


def resonances(b_tesla: float) -> Tuple[float, float]:
    s = math.sqrt(E * E + (GAMMA * b_tesla) ** 2)
    return D - s, D + s


def rabi_frequency(p_dbm: float) -> float:
    return RABI_0DBM * 10 ** (p_dbm / 20.0)


def _driven(t: float, omega: float, delta: float) -> float:
    """Probability of a flip after a square pulse of length t, Rabi frequency omega, detuning delta (Hz)."""
    if t <= 0 or omega <= 0:
        return 0.0
    eff = math.hypot(omega, delta)
    amp = (omega / eff) ** 2
    return amp * 0.5 * (1 - math.cos(2 * math.pi * eff * t) * math.exp(-t / RABI_DECAY))


def flip_probability(protocol: str, p: Dict[str, float], f_mw: float, p_dbm: float, mw_on: bool,
                     b_tesla: float) -> float:
    """How likely the NVs are out of ms = 0 when the signal window opens."""
    lines = resonances(b_tesla)
    tau = p.get("tau", 0.0)
    if protocol == "t1":
        return (2.0 / 3.0) * (1 - math.exp(-tau / T1))
    if not mw_on:
        return 0.0
    omega = rabi_frequency(p_dbm)
    deltas = [f_mw - f for f in lines]
    if protocol == "pulsed_odmr":
        return min(1.0, sum(_driven(p.get("t_pi", 0.0), omega, d) for d in deltas))
    if protocol == "rabi":
        return min(1.0, sum(_driven(tau, omega, d) for d in deltas))
    weight = sum(omega ** 2 / (omega ** 2 + d * d) for d in deltas)   # how well the pi/2 pulses rotate
    weight = min(1.0, weight)
    d = min(deltas, key=abs)
    if protocol == "ramsey":
        return weight * 0.5 * (1 - math.cos(2 * math.pi * d * tau) * math.exp(-(tau / T2STAR) ** 2))
    if protocol == "hahn_echo":
        return weight * 0.5 * (1 - math.exp(-(tau / T2) ** 3))
    if protocol == "cw":
        return min(1.0, sum(omega ** 2 / (omega ** 2 + d * d + CW_WIDTH ** 2) for d in deltas))
    return 0.0


class PulseGeneratorTwin(Twin):
    """Plays one protocol, or a written sequence (pulses.LIBRARY), in a loop while `running`; refuses a sequence
    that fails pulses.check, as Kelvin's drivers do on real generators."""
    TIMES = ("t_pi", "tau", "t_laser", "t_gate", "t_wait")

    def sequence(self) -> pulses.Sequence:
        return pulses.make(str(self.state["protocol"]), self.state.get("sequence"),
                           {k: self.state[k] for k in self.TIMES})

    def apply(self, param: str, value: Any) -> None:
        if param == "sequence":
            value = str(value).strip().lower() or pulses.NONE
            if value != pulses.NONE and value not in pulses.LIBRARY:
                self.errors.append('-224,"Illegal parameter value; no sequence %s"' % value)
                return
        old = self.state.get(param)
        self.state[param] = value
        if self.state.get("running"):
            try:
                problems = pulses.check(self.sequence(), {"max_mw_duty": float(self.state.get("max_mw_duty", 0.5))})
            except (KeyError, ValueError) as e:
                problems = [str(e.args[0] if e.args else e)]
            if problems:
                self.state[param] = old
                self.errors.append('-221,"Settings conflict; %s"' % problems[0].replace('"', "'"))

    def measure_period(self) -> float:
        return self.sequence().period

    def measure_mw_duty(self) -> float:
        return self.sequence().mw_duty()


class PhotonCounterTwin(Twin):
    """Counts in the gate's signal and reference windows over `integration` seconds, and the plain count rate.
    One acquisition serves one reading of each of signal, reference and contrast; reading one again acquires anew."""

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._last: Optional[Dict[str, float]] = None
        self._read: set = set()

    def _parts(self) -> Tuple[Any, Any]:
        return self.world.of_kind("pulse-generator"), self.world.of_kind("microwave-source")

    def _rate(self) -> float:
        p = self.world.laser_on_sample()
        return R_MAX * p / (p + P_SAT) if p > 0 else 0.0

    def _mw(self) -> Tuple[float, float, bool]:
        mw = self._parts()[1]
        if mw is None:
            return 0.0, -100.0, False
        return float(mw.state.get("frequency", 0.0)), float(mw.state.get("power", -100.0)), bool(mw.state.get("output"))

    def _acquire(self) -> Dict[str, float]:
        t = float(self.state["integration"])
        self.take_time(t)
        pg = self._parts()[0]
        if pg is None or not pg.state.get("running"):
            return {"signal": 0.0, "reference": 0.0, "contrast": 0.0}   # nothing in the windows
        seq = pg.sequence()
        f, p, on = self._mw()
        flip = flip_probability(seq.protocol, seq.params, f, p, on, self.world.field())
        n = t / seq.period
        windows = {label: b - a for label, a, b in seq.windows}
        ref_mean = self._rate() * windows["reference"] * n
        sig_mean = self._rate() * windows["signal"] * n * (1 - CONTRAST * flip)
        sig = max(0.0, sig_mean + self.world.noise(math.sqrt(max(sig_mean, 1e-9))))
        ref = max(0.0, ref_mean + self.world.noise(math.sqrt(max(ref_mean, 1e-9))))
        return {"signal": round(sig), "reference": round(ref), "contrast": sig / ref if ref > 0 else 0.0}

    def _field(self, name: str) -> float:
        if self._last is None or name in self._read:
            self._last, self._read = self._acquire(), set()
        self._read.add(name)
        return self._last[name]

    def measure_signal(self) -> float:
        return self._field("signal")

    def measure_reference(self) -> float:
        return self._field("reference")

    def measure_contrast(self) -> float:
        return self._field("contrast")

    def measure_rate(self) -> float:
        """Counts per second with the light on continuously: CW ODMR dips while the MW is on resonance."""
        t = float(self.state["integration"])
        self.take_time(t)
        f, p, on = self._mw()
        flip = flip_probability("cw", {}, f, p, on, self.world.field())
        mean = self._rate() * (1 - CONTRAST * flip) * t
        return max(0.0, mean + self.world.noise(math.sqrt(max(mean, 1e-9)))) / t
