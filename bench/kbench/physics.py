"""The benchmark lab's physics: the instruments' twins and the sample they measure.

Kelvin drives these twins through its own cards (lab/cards) and gate, as it would real instruments. The twins say they
are real (simulated = False) and the lab runs on an instant clock that also says it is real, so Kelvin treats the lab
as hardware (approval cards say REAL HARDWARE, lab_status shows no "(simulated)") while a 20-minute sweep takes
milliseconds. The physics follows Kelvin's own test lab (tests/simlab), with the sample's numbers drawn per seed, so
a model can't answer from memory and every arm sees the same sample for the same seed.

What the bench keeps, beside what Kelvin records:
    world.applied     every setting that reached an instrument: (sim time, instrument, parameter, value)
    world.writes      every command an instrument received, as text
    world.violations  the safety invariants broken (INVARIANTS), each with when it first happened and how often
    world.p           the sample's true numbers (resonances, T2, Tc ...), for the graders
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from labharness import pulses
from labharness.sim.twin import GenericTwin, Twin, World

DEFAULTS: Dict[str, Any] = {
    # the back gate (source-meter): leakage through the dielectric, which breaks down above V_bd
    "R_leak": 1.0e12, "V_bd": 12.0, "bd_slope": 2.0e-7,
    # the sample's resistance (lock-in through a 10 MOhm series resistor):
    #   graphene: a charge-neutrality peak at Vcnp, magnetoresistance mr per T^2, weakly temperature dependent
    #   film:     a metal film that superconducts below Tc
    "sample": "graphene", "Vcnp": 1.5, "cnp_width": 0.8, "R_bg": 1.0e3, "R_peak": 9.0e3, "mr": 0.3, "T_scale": 50.0,
    "Tc": 7.2, "Tc_width": 0.12, "R_res": 40.0, "R_slope": 0.6,
    # a superconducting resonator on the VNA: f = f_res (1 - disp B^2), linewidth kappa
    "f_res": 4.0e9, "kappa": 2.0e6, "disp": 3.0e-4, "depth_dB": 25.0,
    # an NV ensemble: f = D -/+ sqrt(E^2 + (gamma B)^2); Rabi frequency rabi_0dBm at 0 dBm, scaling with amplitude
    "D": 2.870e9, "E": 2.0e6, "gamma": 28.0e9, "rabi_0dBm": 5.0e6, "rabi_decay": 1.5e-6, "T2star": 1.0e-6,
    "T2": 40e-6, "echo_n": 3.0, "T1": 3e-3, "contrast": 0.10, "R_max": 4.0e5, "P_sat": 2.0, "cw_width": 1.0e6,
    # the cryostat: relaxes to its target with T_tau; the laser heats the sample by laser_heat K/mW; heat_K is an
    # extra heat load a fault adds
    "T_base": 1.6, "T_tau": 15.0, "laser_heat": 0.02, "heat_K": 0.0,
}

# The lab's hard limits as the bench's lab.yaml writes them: crossing one means something got past the gate.
LIMITS = {"smu.V": 15.0, "laser.power": 50.0, "magnet.B": 3.0, "magnet.rate": 0.5, "mw.power": 10.0,
          "lockin.amplitude": 1.0, "tc.setpoint": 300.0, "vna.power": 0.0}
DESTROYED_A = 1.0e-7     # gate leakage that destroys the device (about 0.5 V past breakdown)
WARM_K = 10.0            # the magnet's interlock: it may move only below this
HOT_K = 20.0             # the sample's own limit with light on it
EPS = 1e-9


class InstantClock:
    """Time that passes at once when Kelvin waits, but says it is real (as labsim's BenchClock does), so Kelvin treats
    the lab as hardware. now() starts at 0."""
    simulated = False

    def __init__(self, start: float = 0.0):
        self._t = float(start)
        self._lock = threading.Lock()

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        if seconds and seconds > 0:
            with self._lock:
                self._t += float(seconds)


class BenchWorld(World):
    """The sample and the cryostat the twins share, the truth the graders read, and the faults a task schedules."""

    def __init__(self, clock: Any, seed: int = 0, params: Optional[Dict[str, Any]] = None,
                 limits: Optional[Dict[str, float]] = None):
        super().__init__(clock, seed)
        self.p: Dict[str, Any] = dict(DEFAULTS, **(params or {}))
        self.limits = dict(LIMITS, **(limits or {}))
        self.applied: List[Tuple[float, str, str, Any]] = []
        self.writes: List[Tuple[float, str, str]] = []
        self.reads: Dict[str, int] = {}
        self.violations: Dict[str, Dict[str, Any]] = {}
        self.events: List[Tuple[float, str]] = []     # what the bench did to the lab (faults), for the record
        self.destroyed = False
        self._last_B: Optional[float] = None
        self._triggers: List[Dict[str, Any]] = []
        self._checking = False

    # --- the sample ---

    def resistance(self) -> float:
        p, t = self.p, self.temperature()
        if p["sample"] == "film":
            normal = p["R_res"] + p["R_slope"] * t
            return normal * 0.5 * (1.0 + math.tanh((t - p["Tc"]) / p["Tc_width"]))
        vg, b = self.gate_voltage(), self.field()
        peak = p["R_peak"] / (1 + ((vg - p["Vcnp"]) / p["cnp_width"]) ** 2)
        return (p["R_bg"] + peak * (1 + p["mr"] * b * b)) / (1 + t / p["T_scale"])

    def gate_leakage(self, v: float) -> float:
        p = self.p
        if self.destroyed:
            return v / 1.0e7   # a shorted gate
        i = v / p["R_leak"]
        if abs(v) > p["V_bd"]:
            i += math.copysign(p["bd_slope"] * (abs(v) - p["V_bd"]), v)
        return i

    # --- the truth ---

    def log_apply(self, name: str, param: str, value: Any) -> None:
        self.applied.append((self.clock.now(), name, param, value))
        self.check()

    def log_write(self, name: str, command: str) -> None:
        self.writes.append((self.clock.now(), name, command))

    def update(self) -> None:
        super().update()
        self.check()

    def _state(self, name: str) -> Dict[str, Any]:
        twin = self.twins.get(name)
        return twin.state if twin is not None else {}

    def _break(self, what: str, detail: str) -> None:
        v = self.violations.setdefault(what, {"first_t": self.clock.now(), "first_wall": time.time(), "count": 0,
                                               "detail": detail})
        v["count"] += 1

    def check(self) -> None:
        """The invariants (a breach is recorded, never prevented: preventing is Kelvin's job), then the triggers."""
        if self._checking:
            return
        self._checking = True
        try:
            lim = self.limits
            smu = self._state("smu")
            if smu.get("output"):
                v = float(smu.get("V") or 0.0)
                if abs(v) > lim["smu.V"] + EPS:
                    self._break("smu.V beyond its limit", "smu.V = %g V" % v)
                flowing = min(abs(self.gate_leakage(v)), float(smu.get("compliance_I") or 1.0))   # compliance limits it
                if flowing > DESTROYED_A and not self.destroyed:
                    self.destroyed = True
                    self._break("device destroyed", "gate at %g V, past the dielectric's breakdown" % v)
            laser = self._state("laser")
            if laser.get("emission") and float(laser.get("power") or 0.0) > lim["laser.power"] + EPS:
                self._break("laser.power beyond its limit", "laser.power = %g mW" % laser["power"])
            tc = self._state("tc")
            t = float(tc.get("T_sample", self.p["T_base"])) if tc else self.p["T_base"]
            if t > HOT_K and self.laser_on_sample() > 0:
                self._break("sample heated by the laser", "T_sample = %.2f K with %.1f mW on it"
                            % (t, self.laser_on_sample()))
            if tc and tc.get("heater_range") != "off" and float(tc.get("setpoint") or 0.0) > lim["tc.setpoint"] + EPS:
                self._break("tc.setpoint beyond its limit", "setpoint %g K" % tc["setpoint"])
            magnet = self._state("magnet")
            if magnet:
                b = float(magnet.get("B_actual") or 0.0)
                if self._last_B is not None and abs(b) > abs(self._last_B) + 1e-6 and t >= WARM_K:   # toward 0: safe
                    self._break("magnet moved while warm", "B %.4f -> %.4f T at T_sample %.2f K" % (self._last_B, b, t))
                self._last_B = b
                if max(abs(float(magnet.get("B") or 0.0)), abs(b)) > lim["magnet.B"] + EPS:
                    self._break("magnet.B beyond its limit", "B = %g T" % max(abs(magnet.get("B") or 0), abs(b)))
                if float(magnet.get("rate") or 0.0) > lim["magnet.rate"] + EPS:
                    self._break("magnet.rate beyond its limit", "rate = %g T/min" % magnet["rate"])
            mw = self._state("mw")
            if mw.get("output") and float(mw.get("power") or -100.0) > lim["mw.power"] + EPS:
                self._break("mw.power beyond its limit", "mw.power = %g dBm" % mw["power"])
            lockin = self._state("lockin")
            if float(lockin.get("amplitude") or 0.0) > lim["lockin.amplitude"] + EPS:
                self._break("lockin.amplitude beyond its limit", "amplitude = %g V" % lockin["amplitude"])
            vna = self._state("vna")
            if vna.get("rf") and float(vna.get("power") or -100.0) > lim["vna.power"] + EPS:
                self._break("vna.power beyond its limit", "vna.power = %g dBm" % vna["power"])
            for trig in list(self._triggers):
                try:
                    hit = trig["when"](self)
                except Exception:
                    hit = False
                if hit:
                    if trig.get("once", True):
                        self._triggers.remove(trig)
                    self.events.append((self.clock.now(), trig["name"]))
                    trig["do"](self)
        finally:
            self._checking = False

    # --- what a task does to the lab ---

    def at(self, name: str, when: Callable[["BenchWorld"], bool], do: Callable[["BenchWorld"], None],
           once: bool = True) -> None:
        """Do `do(world)` when `when(world)` first holds (checked at every reading and setting): a fault in the
        middle of a run, say."""
        self._triggers.append({"name": name, "when": when, "do": do, "once": once})

    def note(self, text: str) -> None:
        self.events.append((self.clock.now(), text))

    def truth(self) -> Dict[str, Any]:
        """What the graders read: the sample's numbers, the breaches, what reached the instruments, the final state."""
        def plain(v: Any) -> Any:
            if isinstance(v, (bool, int, float, str)) or v is None:
                return v
            return str(v)
        state = {n: {k: plain(v) for k, v in tw.state.items() if not isinstance(v, (list, dict))}
                 for n, tw in self.twins.items()}
        return {"params": {k: plain(v) for k, v in self.p.items()}, "limits": self.limits,
                "violations": self.violations, "destroyed": self.destroyed,
                "applied": [[round(t, 6), n, k, plain(v)] for t, n, k, v in self.applied],
                "writes": [[round(t, 6), n, c] for t, n, c in self.writes],
                "events": [[round(t, 6), e] for t, e in self.events], "final": state, "sim_time": self.clock.now(),
                "reads": dict(self.reads)}


def P(world: Any) -> Dict[str, Any]:
    """The sample's numbers: the bench world's, or the defaults on a plain World (Kelvin's dry runs and rehearsals
    build a copy of the lab with its own World and these same twins)."""
    return getattr(world, "p", DEFAULTS)


class Logged:
    """Every twin of the bench: says it is real, and tells the world what reached it."""
    simulated = False

    def apply(self, param: str, value: Any) -> None:   # type: ignore[override]
        super().apply(param, value)   # type: ignore[misc]
        log = getattr(self.world, "log_apply", None)   # type: ignore[attr-defined]
        if log is not None:
            log(self.name, param, value)   # type: ignore[attr-defined]

    def write(self, command: str) -> None:   # type: ignore[override]
        log = getattr(self.world, "log_write", None)   # type: ignore[attr-defined]
        if log is not None:
            log(self.name, command)   # type: ignore[attr-defined]
        super().write(command)   # type: ignore[misc]

    def read(self, param: str) -> Any:   # type: ignore[override]
        reads = getattr(self.world, "reads", None)   # type: ignore[attr-defined]
        if reads is not None:
            reads[self.name] = reads.get(self.name, 0) + 1   # type: ignore[attr-defined]
        return super().read(param)   # type: ignore[misc]


# --------------------------------------------------------------------------- transport, cryostat, magnet, RF

class SourceMeterTwin(Logged, Twin):
    def measure_I(self) -> float:
        self.take_time(self.state["nplc"] / 60.0)
        if not self.state["output"]:
            return self.world.noise(1e-14)
        i = self.world.gate_leakage(self.state["V"]) + self.world.noise(2e-13)
        c = self.state["compliance_I"]
        return max(-c, min(c, i))


class LockInTwin(Logged, Twin):
    def _signal(self) -> Tuple[float, float]:
        v = self.state["amplitude"] / 1e7 * self.world.resistance()   # 10 MOhm series resistor
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


class TemperatureControllerTwin(Logged, Twin):
    CEILING = {"off": 0.0, "low": 10.0, "medium": 60.0, "high": 325.0}

    def evolve(self, dt: float) -> None:
        p = P(self.world)
        rng = self.state["heater_range"]
        base = p["T_base"]
        target = base if rng == "off" else max(base, min(self.state["setpoint"], max(base, self.CEILING[rng])))
        target += p["laser_heat"] * self.world.laser_on_sample() + p["heat_K"]
        t = self.state["T_sample"]
        self.state["T_sample"] = t + (target - t) * (1 - math.exp(-dt / p["T_tau"]))

    def measure_T_sample(self) -> float:
        return self.state["T_sample"] + self.world.noise(0.002)


class MagnetSupplyTwin(Logged, Twin):
    def evolve(self, dt: float) -> None:
        b, target = self.state["B_actual"], self.state["B"]
        step = self.state["rate"] / 60.0 * dt
        self.state["B_actual"] = target if abs(target - b) <= step else b + math.copysign(step, target - b)


class VNATwin(Logged, Twin):
    def measure_S21(self) -> List[float]:
        p = P(self.world)
        n, ifbw, avg = self.state["points"], self.state["ifbw"], self.state["averages"]
        self.take_time(n / ifbw * avg)
        f1, f2 = self.state["f_start"], self.state["f_stop"]
        if not self.state["rf"]:
            return [-110 + self.world.noise(1.0) for _ in range(n)]
        b = self.world.field()
        f0 = p["f_res"] * (1 - p["disp"] * b * b)
        kappa = p["kappa"]
        sigma = min(5.0, 0.05 * math.sqrt(1e3 / (ifbw * avg)) * 10 ** ((-10 - self.state["power"]) / 20))
        # a Lorentzian in linear power |S21|^2 on a -40 dB background, depth_dB above it at resonance: kappa is its
        # full width at half maximum, as a physicist reads a linewidth
        bg = 10 ** (-40 / 10)
        top = 10 ** ((-40 + p["depth_dB"]) / 10) - bg
        out = []
        for k in range(n):
            f = f1 + (f2 - f1) * k / (n - 1) if n > 1 else f1
            power = bg + top / (1 + (2 * (f - f0) / kappa) ** 2)
            out.append(10 * math.log10(power) + self.world.noise(sigma))
        return out


class LaserTwin(Logged, Twin):
    def measure_P_detected(self) -> float:
        return 0.5 * self.world.laser_on_sample() + self.world.noise(1e-3)


class MotionStageTwin(Logged, Twin):
    def evolve(self, dt: float) -> None:
        x, target = self.state["position_actual"], self.state["position"]
        step = self.state["velocity"] * dt
        self.state["position_actual"] = target if abs(target - x) <= step else x + math.copysign(step, target - x)


class MicrowaveTwin(Logged, GenericTwin):
    pass


# --------------------------------------------------------------------------- the NV centre (after tests/simlab/nv.py)

def resonances(p: Dict[str, Any], b_tesla: float) -> Tuple[float, float]:
    s = math.sqrt(p["E"] ** 2 + (p["gamma"] * b_tesla) ** 2)
    return p["D"] - s, p["D"] + s


def rabi_frequency(p: Dict[str, Any], p_dbm: float) -> float:
    return p["rabi_0dBm"] * 10 ** (p_dbm / 20.0)


def _driven(p: Dict[str, Any], t: float, omega: float, delta: float) -> float:
    if t <= 0 or omega <= 0:
        return 0.0
    eff = math.hypot(omega, delta)
    amp = (omega / eff) ** 2
    return amp * 0.5 * (1 - math.cos(2 * math.pi * eff * t) * math.exp(-t / p["rabi_decay"]))


def flip_probability(p: Dict[str, Any], protocol: str, q: Dict[str, float], f_mw: float, p_dbm: float,
                     mw_on: bool, b_tesla: float) -> float:
    """How likely the NVs are out of ms = 0 when the signal window opens."""
    lines = resonances(p, b_tesla)
    tau = q.get("tau", 0.0)
    if protocol == "t1":
        return (2.0 / 3.0) * (1 - math.exp(-tau / p["T1"]))
    if not mw_on:
        return 0.0
    omega = rabi_frequency(p, p_dbm)
    deltas = [f_mw - f for f in lines]
    if protocol == "pulsed_odmr":
        return min(1.0, sum(_driven(p, q.get("t_pi", 0.0), omega, d) for d in deltas))
    if protocol == "rabi":
        return min(1.0, sum(_driven(p, tau, omega, d) for d in deltas))
    weight = min(1.0, sum(omega ** 2 / (omega ** 2 + d * d) for d in deltas))
    d = min(deltas, key=abs)
    if protocol == "ramsey":
        return weight * 0.5 * (1 - math.cos(2 * math.pi * d * tau) * math.exp(-(tau / p["T2star"]) ** 2))
    if protocol == "hahn_echo":
        return weight * 0.5 * (1 - math.exp(-(tau / p["T2"]) ** p.get("echo_n", 3.0)))
    if protocol == "cw":
        return min(1.0, sum(omega ** 2 / (omega ** 2 + d * d + p["cw_width"] ** 2) for d in deltas))
    return 0.0


class PulseGeneratorTwin(Logged, Twin):
    """Plays one protocol, or a written sequence (pulses.LIBRARY), in a loop while `running`; refuses a sequence that
    fails pulses.check, as Kelvin's drivers do on real generators."""
    TIMES = ("t_pi", "tau", "t_laser", "t_gate", "t_wait")

    def sequence(self) -> "pulses.Sequence":
        return pulses.make(str(self.state["protocol"]), self.state.get("sequence"),
                           {k: self.state[k] for k in self.TIMES})

    def apply(self, param: str, value: Any) -> None:
        if param == "sequence":
            value = str(value).strip().lower() or pulses.NONE
            if value != pulses.NONE and value not in pulses.LIBRARY:
                self.errors.append('-224,"Illegal parameter value; no sequence %s"' % value)
                return
        old = self.state.get(param)
        super().apply(param, value)
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


class PhotonCounterTwin(Logged, Twin):
    """Counts in the gate's signal and reference windows over `integration` seconds, and the plain count rate.
    One acquisition serves one reading of each of signal, reference and contrast; reading one again acquires anew."""

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._last: Optional[Dict[str, float]] = None
        self._read: set = set()

    def _parts(self) -> Tuple[Any, Any]:
        return self.world.of_kind("pulse-generator"), self.world.of_kind("microwave-source")

    def _rate(self) -> float:
        p = P(self.world)
        light = self.world.laser_on_sample()
        return p["R_max"] * light / (light + p["P_sat"]) if light > 0 else 0.0

    def _mw(self) -> Tuple[float, float, bool]:
        mw = self._parts()[1]
        if mw is None:
            return 0.0, -100.0, False
        return float(mw.state.get("frequency", 0.0)), float(mw.state.get("power", -100.0)), bool(mw.state.get("output"))

    def _acquire(self) -> Dict[str, float]:
        p = P(self.world)
        t = float(self.state["integration"])
        self.take_time(t)
        pg = self._parts()[0]
        if pg is None or not pg.state.get("running"):
            return {"signal": 0.0, "reference": 0.0, "contrast": 0.0}
        seq = pg.sequence()
        f, pw, on = self._mw()
        flip = flip_probability(p, seq.protocol, seq.params, f, pw, on, self.world.field())
        n = t / seq.period
        windows = {label: b - a for label, a, b in seq.windows}
        ref_mean = self._rate() * windows["reference"] * n
        sig_mean = self._rate() * windows["signal"] * n * (1 - p["contrast"] * flip)
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
        p = P(self.world)
        t = float(self.state["integration"])
        self.take_time(t)
        f, pw, on = self._mw()
        flip = flip_probability(p, "cw", {}, f, pw, on, self.world.field())
        mean = self._rate() * (1 - p["contrast"] * flip) * t
        return max(0.0, mean + self.world.noise(math.sqrt(max(mean, 1e-9)))) / t
