"""Lake Shore 336 and 350 temperature controllers: inputs read the cryostat's thermometers, outputs run the
controller's own PID loop into the stage heater. The heater range caps the power, so a low range can't reach a
high setpoint. Over USB they are a serial port (57600 baud, 7 data bits, odd parity)."""
from __future__ import annotations

import math
from typing import Dict

from ..instrument import ILLEGAL, Instrument, OUT_OF_RANGE, command, fail
from ..world import cernox_ohm
from .common import Wired


class LakeShore336(Wired, Instrument):
    model = "lakeshore_336"
    idn = "LSCI,MODEL336,{serial},2.9"
    default_serial = "LSA1JK4/#######"
    has_syst_err = False
    drops_unread = False
    terminators = (b"\r\n", b"\n", b"\r")
    reply_end = b"\r\n"
    framing = (57600, 7, "odd", 1.0)
    usb_ids = "1fb9:0205"
    INPUTS = "ABCD"
    OUTPUTS = (1, 2, 3, 4)
    HEATERS = (1, 2)
    RANGE_FRACTION = {0: 0.0, 1: 0.01, 2: 0.1, 3: 1.0}

    def setup(self) -> None:
        self.sensors: Dict[str, str] = {k.upper(): v for k, v in (self.opt("sensors", {"A": "sample", "B": "stage"})).items()}
        self.heats = self.opt("heater", "stage")
        self.open_inputs: set = set()
        self.heater_open = False

    def reset(self) -> None:
        self.setp = {o: 0.0 for o in self.OUTPUTS}
        self.rng = {o: 0 for o in self.OUTPUTS}
        self.mode = {o: (1, o if o <= 4 else 1, 0) for o in self.OUTPUTS}   # mode, control input (1=A), powerup
        self.mode[1], self.mode[2] = (1, 1, 0), (1, 2, 0)
        self.pid = {o: [50.0, 20.0, 0.0] for o in self.OUTPUTS}
        self.ramp = {o: [0, 0.1] for o in self.OUTPUTS}
        self.sp_now = {o: 0.0 for o in self.OUTPUTS}
        self.ramping = {o: False for o in self.OUTPUTS}
        self.mout = {o: 0.0 for o in self.OUTPUTS}
        self.out_pct = {o: 0.0 for o in self.OUTPUTS}
        self.integ = {o: 0.0 for o in self.OUTPUTS}
        self.last_err = {o: 0.0 for o in self.OUTPUTS}
        self._heat()

    def status(self):
        return {"setpoints": self.setp, "ranges": self.rng, "heater_pct": {o: round(self.out_pct[o], 1)
                                                                          for o in self.HEATERS},
                "readings": {k: round(self.temp(k), 4) for k in self.sensors}}

    def fault(self, name: str, on: bool):
        if name.startswith("sensor_open"):
            ch = name[-1].upper()
            (self.open_inputs.add if on else self.open_inputs.discard)(ch)
            return "input %s %s" % (ch, "open" if on else "reconnected")
        if name == "heater_open":
            self.heater_open = on
            return "heater %s" % ("open circuit" if on else "reconnected")
        return None

    def temp(self, ch: str) -> float:
        where = self.sensors.get(ch)
        if where is None or ch in self.open_inputs:
            return 0.0
        return self.world.cryo.sensor(where) + self.noise(4e-4 + 2e-5 * self.world.cryo.sensor(where))

    def _input(self, text: str) -> str:
        t = text.strip().upper()
        if t in self.INPUTS:
            return t
        raise fail(ILLEGAL, text)

    def _output(self, a, i: int = 0, heaters_only: bool = False) -> int:
        o = a.int(i)
        if o not in (self.HEATERS if heaters_only else self.OUTPUTS):
            raise fail(ILLEGAL, str(o))
        return o

    # -- the controller's own loop
    def tick(self, dt: float, now: float) -> None:
        for o in self.HEATERS:
            if self.ramping[o]:
                step = self.ramp[o][1] / 60.0 * dt
                d = self.setp[o] - self.sp_now[o]
                self.sp_now[o] = self.setp[o] if abs(d) <= step else self.sp_now[o] + math.copysign(step, d)
                self.ramping[o] = self.sp_now[o] != self.setp[o]
            else:
                self.sp_now[o] = self.setp[o]
            mode, inp, _ = self.mode[o]
            if self.rng[o] == 0 or mode == 0:
                self.out_pct[o] = 0.0
                self.integ[o] = 0.0
                continue
            if mode == 3:   # open loop
                self.out_pct[o] = self.mout[o]
                continue
            ch = self.INPUTS[inp - 1] if 1 <= inp <= len(self.INPUTS) else "A"
            e = self.sp_now[o] - self.temp(ch)
            P, I, D = self.pid[o]
            ti = 1000.0 / I if I > 0 else 1e9
            self.integ[o] = max(-100.0 / max(P, 1e-3), min(100.0 / max(P, 1e-3), self.integ[o] + e * dt / ti))
            de = (e - self.last_err[o]) / max(dt, 1e-3)
            self.last_err[o] = e
            out = P * (e + self.integ[o] + D / 400.0 * ti * de) + self.mout[o]
            self.out_pct[o] = max(0.0, min(100.0, out))
        self._heat()

    def _heat(self) -> None:
        cryo = self.world.cryo
        for o in self.HEATERS:
            frac = (self.out_pct[o] / 100.0) ** 2 * self.RANGE_FRACTION.get(self.rng[o], 0.0)
            w = 0.0 if self.heater_open else frac * cryo.p_max * (1.0 if o == 1 else 0.02)
            cryo.heaters["%s.%d" % (self.name, o)] = w

    # -- readings
    @command("KRDG", setter=False)
    def _krdg(self, q, a, ch):
        t = a.text(0).upper() if a else "A"
        if t == "0":
            return ",".join("%+08.3f" % self.temp(c) for c in self.INPUTS)
        return "%+08.3f" % self.temp(self._input(t))

    @command("CRDG", setter=False)
    def _crdg(self, q, a, ch):
        k = self.temp(self._input(a.text(0)))
        return "%+08.3f" % (k - 273.15 if k else 0.0)

    @command("SRDG", setter=False)
    def _srdg(self, q, a, ch):
        c = self._input(a.text(0))
        t = self.temp(c)
        return "%+09.3f" % (cernox_ohm(t) if t else 0.0)

    @command("RDGST", setter=False)
    def _rdgst(self, q, a, ch):
        c = self._input(a.text(0))
        return "128" if c in self.open_inputs or c not in self.sensors else "000"

    @command("HTR", setter=False)
    def _htr(self, q, a, ch):
        return "%+07.3f" % self.out_pct[self._output(a, 0, True)]

    @command("HTRST", setter=False)
    def _htrst(self, q, a, ch):
        self._output(a, 0, True)
        return "1" if self.heater_open else "0"

    # -- the loop's settings
    @command("SETP")
    def _setp(self, q, a, ch):
        o = self._output(a)
        if q:
            return "%+08.3f" % self.setp[o]
        a.need(2, 2)
        v = a.num(1)
        if not 0.0 <= v <= 1500.0:
            raise fail(OUT_OF_RANGE)
        self.setp[o] = v
        if self.ramp[o][0]:
            self.ramping[o] = True
        else:
            self.sp_now[o] = v

    @command("RANGE")
    def _range(self, q, a, ch):
        o = self._output(a)
        if q:
            return str(self.rng[o])
        a.need(2, 2)
        r = a.int(1)
        if r not in self.RANGE_FRACTION or (o not in self.HEATERS and r > 1):
            raise fail(ILLEGAL, a[1])
        self.rng[o] = r
        self._heat()

    @command("OUTMODE")
    def _outmode(self, q, a, ch):
        o = self._output(a)
        if q:
            return "%d,%d,%d" % self.mode[o]
        a.need(4, 4)
        self.mode[o] = (a.int(1, 0, 5), a.int(2, 0, len(self.INPUTS)), a.int(3, 0, 1))

    @command("PID")
    def _pid(self, q, a, ch):
        o = self._output(a, 0, True)
        if q:
            return ",".join("%+07.1f" % v for v in self.pid[o])
        a.need(4, 4)
        self.pid[o] = [a.num(1, 0.1, 1000.0), a.num(2, 0.1, 1000.0), a.num(3, 0.0, 200.0)]

    @command("RAMP")
    def _ramp(self, q, a, ch):
        o = self._output(a, 0, True)
        if q:
            return "%d,%+08.4f" % tuple(self.ramp[o])
        a.need(3, 3)
        self.ramp[o] = [a.int(1, 0, 1), a.num(2, 0.001, 100.0)]
        if not self.ramp[o][0]:
            self.ramping[o] = False

    @command("RAMPST", setter=False)
    def _rampst(self, q, a, ch):
        return "1" if self.ramping[self._output(a, 0, True)] else "0"

    @command("MOUT")
    def _mout(self, q, a, ch):
        o = self._output(a)
        if q:
            return "%+07.2f" % self.mout[o]
        self.mout[o] = a.num(1, 0.0, 100.0)

    @command("HTRSET")
    def _htrset(self, q, a, ch):
        o = self._output(a, 0, True)
        return "1,1,+0.000,1" if q else None

    @command("INTYPE", "INNAME", "INCRV", "TLIMIT", "ALARM", "RELAY", "DISPLAY", "DISPFLD", "BRIGT", "MODE", "LOCK",
             "FILTER", "ZONE", "ANALOG", "WARMUP", "IEEE", "NET", "LEDS", "INTSEL", "DIOCUR", "CRVHDR", "CRVPT",
             "CRVDEL", "ALMRST", "ATUNE", "TUNEST", "OPST", "OPSTE", "OPSTR")
    def _misc(self, q, a, ch):
        return "0" if q else None


class LakeShore350(LakeShore336):
    model = "lakeshore_350"
    idn = "LSCI,MODEL350,{serial},1.6"
    default_serial = "LSB2280"
    usb_ids = "1fb9:0305"
    RANGE_FRACTION = {0: 0.0, 1: 1e-4, 2: 1e-3, 3: 1e-2, 4: 0.1, 5: 1.0}
