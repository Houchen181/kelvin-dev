"""The physics the instruments share: a sample in a cryostat and a magnet, a superconducting chip, an optical table.

Instruments drive terminals (a current source on "bias", a voltage source on "gate", a coil current on "flux") and
read them back ("vxx", "vxy", the gate's leakage current, S21 of the chip, photon counts). Signals are DC values plus
AC components {frequency: complex RMS amplitude}, so a lock-in finds a signal only at the frequency the sample is
actually excited at. The sample can be damaged: too much gate voltage breaks the dielectric, too much current burns
the device, a magnet ramped too fast quenches. Time constants are shorter than in a real lab (a cool-down takes
minutes, not hours) but every instrument still has to wait for what it waits for.
"""
from __future__ import annotations

import math
import random
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

E = 1.602176634e-19
H = 6.62607015e-34
HBAR = H / (2 * math.pi)
KB = 1.380649e-23
RK = H / E ** 2   # 25812.807 ohm
C_LIGHT = 299792458.0

Signal = Dict[float, complex]   # frequency (0 = DC) -> RMS amplitude


class Source:
    """What one instrument output puts on a terminal."""
    __slots__ = ("kind", "dc", "ac", "series")

    def __init__(self, kind: str = "V", dc: float = 0.0, ac: Optional[Signal] = None, series: float = 0.0):
        self.kind, self.dc, self.ac, self.series = kind, dc, dict(ac or {}), series


# ---------------------------------------------------------------- cryostat
class Cryostat:
    """A stage with a heater, cooled toward base; the sample follows the stage. A heater range on a temperature
    controller is a fraction of heater_max_W, so a low range cannot reach a high temperature. An instrument that runs
    its own temperature control (a PPMS) can own the stage: then it sets the stage, and the cooling and heaters
    here don't pull against it."""

    def __init__(self, c: Dict[str, Any]):
        self.base = float(c.get("base_K", 1.6))
        self.span = float(c.get("span_K", 320.0))
        self.tau = float(c.get("tau_s", 60.0))
        self.link_tau = float(c.get("sample_tau_s", 8.0))
        self.p_max = float(c.get("heater_max_W", 50.0))
        self.stage = self.sample = float(c.get("start_K", 295.0))
        self.heaters: Dict[str, float] = {}
        self.pulse = 0.0          # heat from a quench, decaying
        self.helium = float(c.get("helium_pct", 78.0))
        self.owner: Optional[str] = None   # the instrument that sets the stage itself, if one does

    def t_eq(self) -> float:
        frac = min(1.0, sum(self.heaters.values()) / self.p_max)
        return self.base + self.span * frac ** 0.6 + self.pulse

    def step(self, dt: float) -> None:
        if self.owner is None:   # else its owner puts the stage where it wants it, in its own tick
            tau = self.tau * min(2.0, max(0.15, math.sqrt(max(self.stage, 0.1) / 50.0)))
            self.stage += (self.t_eq() - self.stage) * (1.0 - math.exp(-dt / tau))
        self.sample += (self.stage + 0.004 - self.sample) * (1.0 - math.exp(-dt / self.link_tau))
        self.pulse *= math.exp(-dt / 40.0)

    def sensor(self, where: str) -> float:
        return {"sample": self.sample, "stage": self.stage, "magnet": 4.2 + 0.02 * self.pulse,
                "shield": 40.0 + 0.1 * (self.stage - self.base), "room": 295.0}.get(where, self.stage)


def cernox_ohm(T: float) -> float:
    return 48.0 * math.exp(6.1 / math.sqrt(max(T, 0.05)))


# ---------------------------------------------------------------- magnet
class Magnet:
    """Field per axis. A supply sets the field it ramps to; ramping faster than quench_T_s quenches the magnet."""

    def __init__(self, c: Dict[str, Any], world: "World"):
        self.world = world
        self.max_T = float(c.get("max_T", 9.0))
        self.quench_rate = float(c.get("quench_T_s", 0.02))
        self.field: Dict[str, float] = {"z": 0.0, "x": 0.0, "y": 0.0}
        self.quenched: Dict[str, bool] = {}

    @property
    def B(self) -> float:
        return self.field["z"]

    def set(self, axis: str, value: float, rate: float) -> bool:
        """The supply moved the field; False if this made the magnet quench."""
        if abs(rate) > self.quench_rate * 1.05 or abs(value) > self.max_T * 1.02:
            self.quench(axis, "ramped at %.4g T/s (limit %.4g T/s)" % (abs(rate), self.quench_rate)
                        if abs(rate) > self.quench_rate * 1.05 else "driven past %.2f T" % self.max_T)
            return False
        self.field[axis] = value
        return True

    def quench(self, axis: str, why: str) -> None:
        if self.quenched.get(axis):
            return
        self.quenched[axis] = True
        energy = abs(self.field[axis]) / max(self.max_T, 1e-9)
        self.field[axis] = 0.0
        self.world.cryo.pulse += 25.0 * energy + 2.0
        self.world.cryo.helium = max(0.0, self.world.cryo.helium - 12.0 * energy)
        self.world.note("magnet %s axis QUENCHED: %s" % (axis, why))


# ---------------------------------------------------------------- samples
class Sample:
    kind = "open"
    damaged: Optional[str] = None
    gate_leak = 1e13
    breakdown_V = 1e9
    burn_A = 1e9
    contact = 1000.0

    def __init__(self, c: Dict[str, Any]):
        self.cfg = c
        self.gate_leak = float(c.get("gate_leak_ohm", self.gate_leak))
        self.breakdown_V = float(c.get("breakdown_V", self.breakdown_V))
        self.burn_A = float(c.get("max_current_A", self.burn_A))
        self.contact = float(c.get("contact_ohm", self.contact))
        self.emf = float(c.get("thermal_emf_V", 3e-7))

    def resistances(self, T: float, B: float, vg: float, i: float = 0.0) -> Tuple[float, float]:
        return 1e12, 0.0

    def r2(self, T: float, B: float, vg: float) -> float:
        """Two-probe resistance a source sees: the channel plus two contacts."""
        if self.damaged:
            return 5e11
        rxx, _ = self.resistances(T, B, vg)
        return 1.6 * rxx + 2 * self.contact

    def leak_current(self, vg: float) -> float:
        i = vg / self.gate_leak
        if abs(vg) > 0.8 * self.breakdown_V:   # Fowler-Nordheim-like rise before breakdown
            i += math.copysign(1e-12 * math.exp(min(40.0, 25.0 * (abs(vg) / self.breakdown_V - 0.8))), vg)
        return i


class HallBar(Sample):
    """Graphene (Dirac point, plateaus at 2, 6, 10...) or a GaAs 2DEG (integer plateaus), on a back gate."""
    kind = "hallbar"

    def __init__(self, c: Dict[str, Any]):
        super().__init__(c)
        self.material = c.get("material", "graphene")
        g = self.material == "graphene"
        self.mu = float(c.get("mobility_m2_Vs", 1.5 if g else 80.0))
        self.n_res = float(c.get("n_residual_m2", 3e14 if g else 0.0))
        self.n0 = float(c.get("n0_m2", 0.0 if g else 2.4e15))
        self.v_dirac = float(c.get("dirac_V", 0.8))
        eps, d = float(c.get("gate_eps", 3.9)), float(c.get("gate_nm", 285.0)) * 1e-9
        self.c_gate = 8.854e-12 * eps / d
        self.aspect = float(c.get("L_over_W", 2.5))
        self.gate_leak = float(c.get("gate_leak_ohm", 2e13))
        self.breakdown_V = float(c.get("breakdown_V", 95.0 if g else 6.0))
        self.burn_A = float(c.get("max_current_A", 1e-3 if g else 1e-4))
        self.contact = float(c.get("contact_ohm", 1200.0 if g else 300.0))

    def density(self, vg: float) -> float:
        if self.material == "graphene":
            return self.c_gate * (vg - self.v_dirac) / E       # electrons positive above the Dirac point
        return self.n0 + self.c_gate * vg / E

    def resistances(self, T: float, B: float, vg: float, i: float = 0.0) -> Tuple[float, float]:
        if self.damaged:
            return 5e11, 0.0
        g = self.material == "graphene"
        n = self.density(vg)
        if g:
            n_eff = math.hypot(n, self.n_res)
            mu = self.mu / (1.0 + (T / 120.0) ** 1.5)
        else:
            if n < 2e13:
                return 2e9 * (1 + 10 * math.exp(-T / 5)), 0.0   # depleted under the gate
            n_eff = n
            mu = self.mu / (1.0 + (T / 25.0) ** 2)
        rho_cl = 1.0 / (E * n_eff * mu)
        if g:
            rho_cl *= 1.0 + (mu * B) ** 2 * (self.n_res / n_eff) ** 2
        ns = n * abs(n) / n_eff if g else n                 # signed effective density, 0 at the Dirac point
        rxy = -B * ns / (E * n_eff ** 2) if n_eff else 0.0
        rho = rho_cl
        aB = abs(B)
        if aB > 0.25 and abs(ns) > 1e12:
            nu = H * abs(ns) / (E * aB)
            if g:
                k = max(0, int(round((nu - 2.0) / 4.0)))
                p = 4 * k + 2
                gap_K = 420.0 * math.sqrt(aB) * (math.sqrt(k + 1) - math.sqrt(k)) if k else 420.0 * math.sqrt(aB)
            else:
                p = max(1, int(round(nu)))
                gap_K = (20.0 if p % 2 == 0 else 2.5) * aB
            s = (1.0 - math.exp(-gap_K / (2.0 * max(T, 0.01)))) * (1.0 - math.exp(-(mu * aB) / 3.0))
            d = nu - p
            w = s * math.exp(-(d / (1.3 if g else 0.35)) ** 4)
            nu_eff = nu - d * w
            if nu_eff > 0.05:
                rxy = math.copysign(RK / nu_eff, rxy if rxy else -B)
            sdh = 1.0 + 0.6 * s * math.cos(2 * math.pi * nu / (4.0 if g else 1.0))
            rho = rho_cl * sdh * (1.0 - 0.995 * w)
        if g and aB > 2.0:                                 # nu = 0 turns insulating in a strong field
            rho += RK * 3.0 * math.exp(-(ns / 2.5e14) ** 2) * min(1.0, (aB - 2.0) / 6.0) / (1 + T / 30.0)
        return rho * self.aspect, rxy


class Superconductor(Sample):
    """A thin film: R(T) drops at Tc(B); above the critical current it turns resistive."""
    kind = "superconductor"

    def __init__(self, c: Dict[str, Any]):
        super().__init__(c)
        self.tc0 = float(c.get("Tc_K", 8.6))
        self.rn = float(c.get("Rn_ohm", 420.0))
        self.width = float(c.get("width_K", 0.12))
        self.bc2 = float(c.get("Bc2_T", 11.0))
        self.ic0 = float(c.get("Ic_A", 3e-4))
        self.burn_A = float(c.get("max_current_A", 0.02))
        self.contact = float(c.get("contact_ohm", 5.0))

    def tc(self, B: float) -> float:
        return self.tc0 * math.sqrt(max(0.0, 1.0 - abs(B) / self.bc2))

    def resistances(self, T: float, B: float, vg: float, i: float = 0.0) -> Tuple[float, float]:
        if self.damaged:
            return 5e11, 0.0
        tc = self.tc(B)
        rn = self.rn * (1.0 + 0.0008 * max(0.0, T - 20.0))
        f = 0.5 * (1.0 + math.tanh((T - tc) / self.width)) if tc > 0 else 1.0
        ic = self.ic0 * max(0.0, 1.0 - T / tc) ** 1.5 if tc > 0 and T < tc else 0.0
        if abs(i) > ic:
            f = max(f, 1.0 - (ic / abs(i)) ** 2 if i else 1.0)
        return rn * max(f, 1e-9), B * 2e-3 * f


class Metal(Sample):
    kind = "metal"

    def __init__(self, c: Dict[str, Any]):
        super().__init__(c)
        self.r0 = float(c.get("R0_ohm", 120.0))
        self.a = float(c.get("A_ohm_K2", 0.05))
        self.mr = float(c.get("MR_per_T2", 0.002))
        self.contact = float(c.get("contact_ohm", 2.0))

    def resistances(self, T: float, B: float, vg: float, i: float = 0.0) -> Tuple[float, float]:
        if self.damaged:
            return 5e11, 0.0
        r = (self.r0 + self.a * T ** 2 / (1.0 + T / 150.0)) * (1.0 + self.mr * B * B)
        return r, B * 0.01


SAMPLES = {"hallbar": HallBar, "superconductor": Superconductor, "metal": Metal, "open": Sample}


# ---------------------------------------------------------------- superconducting chip (VNA)
class Chip:
    """Notch-coupled resonators behind ~70 dB of attenuation and a HEMT; one is a SQUID-tunable resonator coupled
    dispersively to a transmon, so a second tone at the qubit frequency shifts it (two-tone spectroscopy)."""

    def __init__(self, c: Dict[str, Any]):
        self.resonators = c.get("resonators") or [
            {"f0": 7.142e9, "qi": 4.0e5, "qc": 5.5e4, "period_A": 2.6e-3, "offset_A": 0.35e-3, "d": 0.3, "qubit": True},
            {"f0": 7.613e9, "qi": 9.0e5, "qc": 1.2e5},
            {"f0": 6.874e9, "qi": 2.2e5, "qc": 3.0e4}]
        self.qubit = c.get("qubit") or {"fmax": 5.62e9, "chi": 1.4e6, "linewidth": 0.6e6, "period_A": 2.6e-3,
                                        "offset_A": 0.35e-3, "d": 0.3}
        self.atten_dB = float(c.get("input_attenuation_dB", 70.0))
        self.gain_dB = float(c.get("gain_dB", 62.0))
        self.delay = float(c.get("cable_delay_s", 58e-9))
        self.t_noise = float(c.get("noise_temperature_K", 4.0))
        self.tls = float(c.get("tls_loss", 1.2e-5))
        self.nc = float(c.get("tls_photons", 8.0))

    @staticmethod
    def _tune(fmax: float, flux_a: float, period: float, offset: float, d: float) -> float:
        phi = math.pi * (flux_a - offset) / period
        return fmax * (math.cos(phi) ** 2 + d * d * math.sin(phi) ** 2) ** 0.25

    def qubit_freq(self, flux_a: float) -> float:
        q = self.qubit
        return self._tune(q["fmax"], flux_a, q["period_A"], q["offset_A"], q.get("d", 0.3))

    def s21(self, f: np.ndarray, p_dbm: float, flux_a: float, T: float,
            drive: Optional[Tuple[float, float]] = None) -> np.ndarray:
        p_dev_w = 1e-3 * 10 ** ((p_dbm - self.atten_dB) / 10.0)
        s = np.ones_like(f, dtype=complex)
        pe = 0.0
        if drive is not None:
            fd, pd = drive
            q = self.qubit
            rabi = 4e6 * 10 ** ((pd + 30.0) / 20.0)        # drive strength at the qubit (Hz)
            det = fd - self.qubit_freq(flux_a)
            g = q["linewidth"]
            pe = 0.5 * rabi ** 2 / (rabi ** 2 + det ** 2 + g ** 2)
        for r in self.resonators:
            fr = r["f0"]
            if "period_A" in r:
                fr = self._tune(r["f0"], flux_a, r["period_A"], r["offset_A"], r.get("d", 0.3))
            if r.get("qubit"):
                fr -= 2.0 * self.qubit["chi"] * pe
            qc = r["qc"]
            n_ph = 2.0 * p_dev_w * qc / (HBAR * (2 * math.pi * fr) ** 2) * (1.0 / (1.0 + qc / r["qi"])) ** 2 + 1e-3
            th = math.tanh(H * fr / (2 * KB * max(T, 0.005)))
            inv_qi = 1.0 / r["qi"] + self.tls * th / math.sqrt(1.0 + n_ph / self.nc)
            inv_qi += 2e-4 * math.exp(-1.76 * 1.2 / max(T, 0.005))       # quasiparticles above ~150 mK
            qt = 1.0 / (inv_qi + 1.0 / qc)
            s *= 1.0 - (qt / qc) * np.exp(1j * 0.05) / (1.0 + 2j * qt * (f / fr - 1.0))
        baseline = 10 ** ((self.gain_dB - self.atten_dB - 0.6 * (f / 1e9 - 7.0)) / 20.0)
        return s * baseline * np.exp(-2j * math.pi * f * self.delay)

    def noise_rel(self, p_dbm: float, ifbw: float) -> float:
        p_dev_w = 1e-3 * 10 ** ((p_dbm - self.atten_dB) / 10.0)
        return math.sqrt(KB * self.t_noise * max(ifbw, 1e-3) / max(p_dev_w, 1e-30)) + 1e-4


# ---------------------------------------------------------------- optics
class Optics:
    """Lasers onto a sample with quantum emitters and a silicon substrate; an objective on piezo and coarse
    stages; detectors: an APD, a spectrometer, a camera, a power meter; a pump-probe photodiode."""

    def __init__(self, c: Dict[str, Any], rng: random.Random):
        self.rng = rng
        self.lasers: Dict[str, Dict[str, Any]] = {}
        self.shutter_open: Dict[str, bool] = {}
        self.fine = [50.0, 50.0, 25.0]      # piezo x, y, z (um)
        self.coarse = [0.0, 0.0, 0.0]       # positioner offsets (um)
        self.focus_z = float(c.get("focus_um", 23.7))
        self.psf = float(c.get("psf_um", 0.35))
        self.depth = float(c.get("depth_um", 0.9))
        self.transmission = float(c.get("transmission", 0.4))
        self.pm_fraction = float(c.get("power_meter_fraction", 1.0))
        n = int(c.get("emitters", 60))
        r = random.Random(int(c.get("seed", 7)))
        self.emitters = [dict(x=r.uniform(0, 100), y=r.uniform(0, 100), dz=r.gauss(0, 0.3),
                              cps=r.uniform(4e4, 3e5), psat=r.uniform(0.4e-3, 2e-3),
                              nm=r.gauss(718, 12), fwhm=r.uniform(0.3, 2.5), alive=True) for _ in range(n)]
        self.flakes = [dict(x=r.uniform(10, 90), y=r.uniform(10, 90), a=r.uniform(4, 15), b=r.uniform(2, 8),
                            th=r.uniform(0, math.pi), refl=r.uniform(0.6, 1.4)) for _ in range(5)]
        self.dark = float(c.get("dark_cps", 180.0))
        self.background = float(c.get("background_cps_per_mW", 2500.0))
        self.bleach_W = float(c.get("bleach_W", 4e-3))
        pp = c.get("pump_probe") or {}
        self.pp = dict(t1=float(pp.get("tau1_ps", 0.8)), t2=float(pp.get("tau2_ps", 25.0)), a1=0.7, a2=0.3,
                       irf=float(pp.get("irf_ps", 0.15)), amp=float(pp.get("amp", 1e-3)),
                       zero_mm=float(pp.get("time_zero_mm", 12.345)), chop=float(pp.get("chop_Hz", 1170.0)))
        self.delay_mm = 0.0
        self.mw: Optional[Tuple[float, float]] = None   # an NV's microwave tone (Hz, dBm), if a source drives one

    def position(self) -> Tuple[float, float, float]:
        return (self.fine[0] + self.coarse[0], self.fine[1] + self.coarse[1], self.fine[2] + self.coarse[2])

    def laser_power(self, path: Optional[str] = None) -> Tuple[float, float]:
        """Total power reaching the sample (W) and its weighted wavelength (nm)."""
        p = nmw = 0.0
        for name, l in self.lasers.items():
            if not l.get("on") or (path and l.get("path", "excitation") != path):
                continue
            if not self.shutter_open.get(l.get("shutter", ""), True):
                continue
            w = l.get("W", 0.0)
            p += w
            nmw += w * l.get("nm", 532.0)
        return p * self.transmission, (nmw / p if p else 532.0)

    def step(self, dt: float) -> None:
        p, _ = self.laser_power()
        if p > self.bleach_W:
            x, y, z = self.position()
            for e in self.emitters:
                if e["alive"] and math.hypot(e["x"] - x, e["y"] - y) < 1.0 and \
                        self.rng.random() < dt * (p / self.bleach_W - 1.0) * 0.5:
                    e["alive"] = False

    def _near(self) -> List[Tuple[Dict[str, Any], float]]:
        x, y, z = self.position()
        out = []
        for e in self.emitters:
            if not e["alive"]:
                continue
            r2 = (e["x"] - x) ** 2 + (e["y"] - y) ** 2
            if r2 > 9 * self.psf ** 2 * 4:
                continue
            wz = math.exp(-((z - self.focus_z - e["dz"]) / self.depth) ** 2)
            out.append((e, math.exp(-r2 / (2 * self.psf ** 2)) * wz))
        return out

    def count_rate(self) -> float:
        p, _ = self.laser_power()
        x, y, z = self.position()
        rate = self.dark
        for e, w in self._near():
            rate += w * e["cps"] * p / (p + e["psat"])
        focus = math.exp(-((z - self.focus_z) / (3 * self.depth)) ** 2)
        rate += self.background * p * 1e3 * (0.3 + 0.7 * focus)
        if self.mw is not None:
            f, pdbm = self.mw
            dip = 0.12 * (1.0 / (1.0 + ((f - 2.870e9) / 6e6) ** 2)) * min(1.0, 10 ** ((pdbm + 10) / 20))
            rate *= 1.0 - dip
        return rate

    def spectrum(self, nm: np.ndarray) -> np.ndarray:
        """Counts per second per pixel-width at each wavelength (before the camera's efficiency)."""
        p, lam = self.laser_power()
        out = np.zeros_like(nm, dtype=float)
        if p <= 0:
            return out
        x, y, z = self.position()
        focus = math.exp(-((z - self.focus_z) / (3 * self.depth)) ** 2)
        raman_nm = 1.0 / (1.0 / lam - 520.7e-7)            # silicon's 520.7 cm^-1 line
        out += 2.0e5 * p * 1e3 * focus * np.exp(-0.5 * ((nm - raman_nm) / 0.45) ** 2)
        out += 300.0 * p * 1e3 * focus * np.exp(-0.5 * ((nm - 760.0) / 60.0) ** 2)
        for e, w in self._near():
            s = e["fwhm"] / 2.355
            out += w * e["cps"] * p / (p + e["psat"]) * 0.05 * np.exp(-0.5 * ((nm - e["nm"]) / s) ** 2) / s
        out[np.abs(nm - lam) < 0.6] += 5e7 * p                  # laser leaking past the filter
        return out

    def image(self, w: int, h: int, um_per_px: float = 0.25) -> np.ndarray:
        """Reflected white light and the laser spot, 0..1 for an exposure of 1 ms at unit gain."""
        x0, y0, z = self.position()
        xs = x0 + (np.arange(w) - w / 2) * um_per_px
        ys = y0 + (np.arange(h) - h / 2) * um_per_px
        X, Y = np.meshgrid(xs, ys)
        defocus = abs(z - self.focus_z) / 6.0
        img = np.full((h, w), 0.25)
        for f in self.flakes:
            c, s = math.cos(f["th"]), math.sin(f["th"])
            u, v = (X - f["x"]) * c + (Y - f["y"]) * s, -(X - f["x"]) * s + (Y - f["y"]) * c
            inside = 1.0 / (1.0 + np.exp(np.clip(((u / f["a"]) ** 2 + (v / f["b"]) ** 2 - 1.0) * (12.0 / (1 + defocus)), -50, 50)))
            img += 0.15 * (f["refl"] - 1.0) * inside
        img += 0.03 * np.sin(X * 0.7) * np.sin(Y * 0.9) / (1 + defocus)
        p, _ = self.laser_power()
        if p > 0:
            r = (self.psf * (1 + 4 * defocus)) / um_per_px
            yy, xx = np.mgrid[0:h, 0:w]
            img += (p * 1e3) * 40.0 / (1 + 4 * defocus) ** 2 * np.exp(-((xx - w / 2) ** 2 + (yy - h / 2) ** 2) / (2 * r * r))
        return img

    def pump_probe(self) -> float:
        """dR/R at the stage's delay."""
        pp = self.pp
        t = 2.0 * (self.delay_mm - pp["zero_mm"]) * 1e-3 / C_LIGHT * 1e12      # ps
        def resp(tau: float) -> float:
            s = pp["irf"]
            if t < -8 * s:
                return 0.0
            return 0.5 * math.exp(s * s / (2 * tau * tau) - t / tau) * math.erfc((s / tau - t / s) / math.sqrt(2))
        return pp["amp"] * (pp["a1"] * resp(pp["t1"]) + pp["a2"] * resp(pp["t2"]))


# ---------------------------------------------------------------- vacuum and a hot plate
class Vacuum:
    def __init__(self, c: Dict[str, Any]):
        self.base = float(c.get("base_mbar", 2e-7))
        self.p = float(c.get("start_mbar", 3e-6))
        self.tau = float(c.get("tau_s", 300.0))
        self.leak = 0.0

    def step(self, dt: float) -> None:
        target = self.base + self.leak
        self.p += (target - self.p) * (1.0 - math.exp(-dt / self.tau))

    def gauge(self, which: int) -> float:
        return self.p if which == 1 else 2.3e-3 + 0.2 * self.p


class Hotplate:
    """A small heater an Arduino switches: heats toward ambient + gain * duty."""

    def __init__(self, c: Dict[str, Any]):
        self.ambient = float(c.get("ambient_C", 22.4))
        self.gain = float(c.get("gain_C", 95.0))
        self.tau = float(c.get("tau_s", 45.0))
        self.T = self.ambient
        self.duty = 0.0

    def step(self, dt: float) -> None:
        self.T += (self.ambient + self.gain * self.duty - self.T) * (1.0 - math.exp(-dt / self.tau))


# ---------------------------------------------------------------- the world
class World:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg or {}
        self.rng = random.Random(int(self.cfg.get("seed", 1)))
        self.cryo = Cryostat(self.cfg.get("cryostat") or {})
        self.magnet = Magnet(self.cfg.get("magnet") or {}, self)
        sc = dict(self.cfg.get("sample") or {"model": "hallbar"})
        self.sample: Sample = SAMPLES[sc.get("model", "hallbar")](sc)
        self.chip = Chip(self.cfg.get("chip") or {})
        self.optics = Optics(self.cfg.get("optics") or {}, self.rng)
        self.vacuum = Vacuum(self.cfg.get("vacuum") or {})
        self.hotplate = Hotplate(self.cfg.get("hotplate") or {})
        self.drives: Dict[str, Dict[str, Source]] = {}
        self.events: deque = deque(maxlen=200)
        self.t0 = time.time()

    def note(self, text: str) -> None:
        self.events.append((time.strftime("%H:%M:%S"), text))

    # -- terminals
    def drive(self, terminal: Optional[str], key: str, source: Optional[Source]) -> None:
        if not terminal:
            return
        slot = self.drives.setdefault(terminal, {})
        if source is None:
            slot.pop(key, None)
        else:
            slot[key] = source

    def sources(self, terminal: str) -> List[Source]:
        return list(self.drives.get(terminal, {}).values())

    def dc(self, terminal: str) -> float:
        return sum(s.dc for s in self.sources(terminal) if s.kind == "V")

    def frequency_on(self, terminal: str) -> Optional[float]:
        if terminal == "chopper":            # the optical chopper's reference output
            return self.optics.pp["chop"]
        for s in self.sources(terminal):
            for f in s.ac:
                if f > 0:
                    return f
        return None

    def gate(self) -> float:
        return self.dc("gate")

    def r2(self) -> float:
        return self.sample.r2(self.cryo.sample, self.magnet.B, self.gate())

    def bias(self) -> Signal:
        """The current through the sample, per frequency."""
        r2 = self.r2()
        out: Signal = {}
        for s in self.sources("bias"):
            comps = {0.0: complex(s.dc)} if s.dc else {}
            comps.update(s.ac)
            for f, a in comps.items():
                i = a if s.kind == "I" else a / (s.series + r2)
                out[f] = out.get(f, 0j) + i
        return out

    def flux(self) -> float:
        total = 0.0
        for s in self.sources("flux"):
            total += s.dc if s.kind == "I" else s.dc / (s.series or 1000.0)
        return total

    def voltage(self, terminal: str) -> Signal:
        """A voltage probe on the sample (vxx, vxy) or the photodiode (pd), per frequency."""
        if terminal == "pd":
            f = self.optics.pp["chop"]
            probe = self.optics.laser_power("probe")[0] or 0.0
            pump = self.optics.laser_power("pump")[0] or 0.0
            v = 0.8 * probe * 1e3                                   # photodiode, V per mW of probe
            return {0.0: complex(v), f: complex(v * self.optics.pump_probe() * min(1.0, pump * 1e3 / 5.0) * 0.45)}
        if terminal not in ("vxx", "vxy"):
            return {}
        T, B, vg = self.cryo.sample, self.magnet.B, self.gate()
        out: Signal = {}
        for f, i in self.bias().items():
            rxx, rxy = self.sample.resistances(T, B, vg, abs(i) * (1.414 if f else 1.0))
            r = rxx if terminal == "vxx" else rxy
            rc = 1.0 / (1.0 + 2j * math.pi * f * r * 1.2e-9)        # cable capacitance
            out[f] = i * r * rc
        out[0.0] = out.get(0.0, 0j) + (self.sample.emf if terminal == "vxx" else 0.4 * self.sample.emf)
        return out

    # -- time
    def step(self, dt: float) -> None:
        self.cryo.step(dt)
        self.vacuum.step(dt)
        self.hotplate.step(dt)
        self.optics.step(dt)
        s = self.sample
        if not s.damaged:
            vg = self.gate()
            if abs(vg) > s.breakdown_V:
                s.damaged = "gate dielectric broke down at %.1f V" % vg
                s.gate_leak = 2e4
                self.note("SAMPLE DAMAGED: " + s.damaged)
            else:
                for f, i in self.bias().items():
                    if abs(i) * (1.414 if f else 1.0) > s.burn_A:
                        s.damaged = "burned by %.3g A through it" % (abs(i) * (1.414 if f else 1.0))
                        self.note("SAMPLE DAMAGED: " + s.damaged)
                        break

    def summary(self) -> Dict[str, Any]:
        T, B, vg = self.cryo.sample, self.magnet.B, self.gate()
        rxx, rxy = self.sample.resistances(T, B, vg)
        return {"T_sample_K": round(T, 4), "T_stage_K": round(self.cryo.stage, 4), "B_T": dict(self.magnet.field),
                "gate_V": vg, "sample": self.sample.kind, "Rxx_ohm": rxx, "Rxy_ohm": rxy,
                "sample_damaged": self.sample.damaged, "helium_pct": round(self.cryo.helium, 1),
                "laser_on_sample_W": self.optics.laser_power()[0], "stage_um": self.optics.position(),
                "pressure_mbar": self.vacuum.p, "events": list(self.events)[-12:]}
