"""A Keithley 2450 run as a potentiostat (Keithley's 2450-EC electrochemistry kit) on an electrochemical cell.

The instrument side follows the 2450 reference manual's SCPI command set for hardware-timed sweeps:
    SOURce[1]:SWEep:VOLTage:LINear <start>, <stop>, <points>, <delay>, <count>, <rangeType>, <failAbort>, <dual>,
                                   "<bufferName>"
    SOURce[1]:SWEep:VOLTage:LINear:STEP <start>, <stop>, <step>, <delay>, <count>, ...
        set up a sweep (dual ON: from start to stop and back, count: how many times); INITiate runs it on the
        instrument's own clock, one source-measure point every delay + NPLC/60 s, into the reading buffer; ABORt
        ends it; *OPC? answers once it is done
    TRACe:ACTual? ["<bufferName>"]                 how many readings the buffer holds now (it fills as the sweep runs)
    TRACe:DATA? <start>, <end>, ["<bufferName>"], <elements: READing SOURce RELative>   the readings, 1-based
    TRACe:CLEar ["<bufferName>"]
A sweep applies its levels only while the output is on (otherwise the cell is open and reads noise).

The cell: a reversible one-electron couple (ferrocene-like: E0 0.2 V, 1 mM, D 2.4e-5 cm2/s on a 3 mm disc) and the
double layer. The faradaic current is n F A sqrt(D) times the semi-derivative of the oxidised species' surface
concentration, which the Nernst equation sets from the potential at each moment (Grunwald-Letnikov weights, by FFT):
the peak current follows Randles-Sevcik (0.4463 n F A C sqrt(n F v D / R T)), the peaks sit 57-59 mV apart around
E0, and the charging current Cdl dE/dt adds its offset. No iR drop and no slow kinetics: the physics is qualitative."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import numpy as np

from ..instrument import ILLEGAL, OUT_OF_RANGE, command, fail
from .keithley import Keithley2450

F, R_GAS = 96485.33212, 8.314462618
BUFFERS = ("defbuffer1", "defbuffer2")
ELEMENTS = ("READING", "SOURCE", "RELATIVE")


class Cell:
    """An electrochemical cell's response to the potential it is given (see the module's notes)."""

    def __init__(self, c: Dict[str, Any]):
        self.e0 = float(c.get("E0_V", 0.2))
        self.n = int(c.get("n", 1))
        self.conc = float(c.get("conc_mM", 1.0))             # mM = mol/m3
        self.d = float(c.get("D_cm2_s", 2.4e-5)) * 1e-4      # m2/s
        self.area = float(c.get("area_cm2", 0.0707)) * 1e-4  # m2
        self.cdl = float(c.get("Cdl_uF", 20.0)) * 1e-6       # F
        self.t = float(c.get("T_K", 298.15))
        self.noise = float(c.get("noise_A", 2e-9))

    def current(self, e: Any, dt: float) -> Any:
        """The current (A, oxidation positive) at each of these potentials, applied dt apart from rest (all of the
        couple reduced)."""
        e = np.asarray(e, dtype=float)
        n = len(e)
        if n == 0:
            return e
        f = self.n * F / (R_GAS * self.t)
        oxidised = self.conc / (1.0 + np.exp(-f * (e - self.e0)))   # at the surface, Nernstian
        w = np.empty(n)
        w[0] = 1.0
        if n > 1:
            j = np.arange(1, n)
            w[1:] = np.cumprod((j - 1.5) / j)
        size = 1 << int(math.ceil(math.log2(2 * n)))
        semi = np.fft.irfft(np.fft.rfft(oxidised, size) * np.fft.rfft(w, size), size)[:n] / math.sqrt(dt)
        faradaic = self.n * F * self.area * math.sqrt(self.d) * semi
        charging = self.cdl * np.gradient(e, dt) if n > 1 else np.zeros(1)
        return faradaic + charging

    def peak(self, rate: float) -> float:
        """Randles-Sevcik's peak current at this scan rate (V/s), for checking."""
        return 0.4463 * self.n * F * self.area * self.conc * math.sqrt(self.n * F * rate * self.d / (R_GAS * self.t))


class Keithley2450EC(Keithley2450):
    model = "keithley_2450ec"

    def setup(self) -> None:
        super().setup()
        self.cell = Cell(self.cfg.get("cell") or {})

    def reset(self) -> None:
        super().reset()
        self.sweep: Optional[Dict[str, Any]] = None      # what SOURce:SWEep set up
        self.buffers: Dict[str, List[List[float]]] = {b: [] for b in BUFFERS}   # [relative s, source V, reading A]
        self.running: Optional[Dict[str, Any]] = None    # the sweep INITiate started: its schedule and start
        self.busy_until = 0.0

    def status(self) -> Dict[str, Any]:
        out = dict(super().status())
        out["sweep"] = {"set_up": bool(self.sweep), "running": self.running is not None and self.now < self.busy_until,
                        "points": len(self.running["levels"]) if self.running else 0}
        return out

    # -- the sweep
    def _buffer(self, a: Any, i: int) -> str:
        name = a.text(i).lower() if len(a) > i else "defbuffer1"
        if name not in self.buffers:
            raise fail(ILLEGAL, "no buffer %s" % name)
        return name

    def _set_up(self, a: Any, levels: List[float]) -> None:
        delay = a.num(3, 0.0, 10000.0, default=0.0)
        count = a.int(4, 1, 268435455, default=1)
        if len(a) > 5:
            a.word(5, ["AUTO", "BEST", "FIXed"])
        dual = a.bool(7) if len(a) > 7 else False
        one = list(levels) + (list(reversed(levels)) if dual else [])
        self.sweep = {"levels": one * count, "delay": delay, "buffer": self._buffer(a, 8)}

    @command("SOURce#:SWEep:VOLTage:LINear", query=False)
    def _sweep_points(self, q, a, ch):
        a.need(3, 9)
        start, stop = a.num(0, -210.0, 210.0), a.num(1, -210.0, 210.0)
        points = a.int(2, 2, 1000000)
        self._set_up(a, [start + (stop - start) * k / (points - 1) for k in range(points)])

    @command("SOURce#:SWEep:VOLTage:LINear:STEP", query=False)
    def _sweep_step(self, q, a, ch):
        a.need(3, 9)
        start, stop, step = a.num(0, -210.0, 210.0), a.num(1, -210.0, 210.0), abs(a.num(2))
        if step <= 0:
            raise fail(OUT_OF_RANGE, "step")
        points = int(round(abs(stop - start) / step)) + 1
        if points > 1000000:
            raise fail(OUT_OF_RANGE, "too many points")
        sign = 1.0 if stop >= start else -1.0
        self._set_up(a, [start + sign * step * k for k in range(points)])

    @command("INITiate[:IMMediate]", query=False)
    def _initiate(self, q, a, ch):
        if self.sweep is None:   # no sweep set up: one reading, as the 2400 does
            return super()._init(q, a, ch)
        levels = np.asarray(self.sweep["levels"], dtype=float)
        dt = self.sweep["delay"] + self.nplc / 60.0
        if self.output:
            current = self.cell.current(levels, dt)
            noise = self.cell.noise
        else:   # the output relay is open: the cell sees nothing
            current, noise = np.zeros(len(levels)), 3e-12
        current = current + np.array([self.noise(noise) for _ in range(len(levels))])
        self.running = {"start": self.now, "dt": dt, "levels": levels, "current": current,
                        "buffer": self.sweep["buffer"], "kept": 0}
        self.busy_until = self.now + dt * len(levels)

    @command("ABORt", query=False)
    def _abort_sweep(self, q, a, ch):
        self._fill(self.now)
        self.running = None
        self.busy_until = self.now

    def _fill(self, now: float) -> None:
        """Move the points the sweep has taken by now into its buffer."""
        run = self.running
        if run is None:
            return
        done = min(len(run["levels"]), int((now - run["start"]) / run["dt"] + 1e-9))
        buf = self.buffers[run["buffer"]]
        for k in range(run["kept"], done):
            buf.append([k * run["dt"], float(run["levels"][k]) + self.noise(1e-6), float(run["current"][k])])
        run["kept"] = done
        if done >= len(run["levels"]):
            self.running = None

    def tick(self, dt: float, now: float) -> None:
        super().tick(dt, now)
        self._fill(now)

    # -- the buffer
    @command("TRACe:ACTual", setter=False)
    def _actual(self, q, a, ch):
        self._fill(self.now)
        return str(len(self.buffers[self._buffer(a, 0)]))

    @command("TRACe:CLEar", query=False)
    def _clear(self, q, a, ch):
        self.buffers[self._buffer(a, 0)] = []

    @command("TRACe:DATA", setter=False)
    def _data(self, q, a, ch):
        self._fill(self.now)
        a.need(2)
        first, last = a.int(0), a.int(1)
        name, i = "defbuffer1", 2
        if len(a) > 2 and a.text(2).lower() in self.buffers:
            name, i = a.text(2).lower(), 3
        buf = self.buffers[name]
        if not 1 <= first <= last <= len(buf):
            raise fail(OUT_OF_RANGE, "%d to %d of %d readings" % (first, last, len(buf)))
        elements = [a.word(k, ["READing", "SOURce", "RELative"]) for k in range(i, len(a))] or ["READING"]
        col = {"READING": 2, "SOURCE": 1, "RELATIVE": 0}
        return ",".join("%.7E" % buf[k][col[e]] for k in range(first - 1, last) for e in elements)

    @command("TRIGger:STATe", setter=False)
    def _trigger_state(self, q, a, ch):
        self._fill(self.now)
        state = "RUNNING" if self.running is not None else "IDLE"
        return "%s;%s;%d" % (state, state, 0)
