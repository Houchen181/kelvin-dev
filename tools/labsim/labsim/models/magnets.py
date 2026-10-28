"""Magnet power supplies: the AMI 430 programmer (LAN port 7180 or RS-232). It ramps the magnet itself at its own
ramp rate; a rate above what the magnet tolerates quenches it. Over LAN it greets each new connection first."""
from __future__ import annotations

import math

from ..instrument import ILLEGAL, Instrument, OUT_OF_RANGE, command, fail, onoff
from .common import Wired

STATES = {1: "RAMPING", 2: "HOLDING", 3: "PAUSED", 4: "MANUAL UP", 5: "MANUAL DOWN", 6: "ZEROING", 7: "QUENCH",
          8: "AT ZERO", 9: "HEATING SWITCH", 10: "COOLING SWITCH"}


class AMI430(Wired, Instrument):
    model = "ami_430"
    idn = "AMERICAN MAGNETICS INC.,MODEL 430,{serial},2.50"
    default_serial = "S/N 3120"
    error_format = '{code:d},"{text}"'
    terminators = (b"\r\n", b"\n", b"\r")
    reply_end = b"\r\n"
    framing = (115200, 8, "none", 1.0)
    greeting = b"American Magnetics Model 430 IP Interface\r\nHello.\r\n"

    def setup(self) -> None:
        self.axis = str(self.opt("axis", "z"))
        self.coil = float(self.opt("coil_T_per_A", 0.1))          # T/A
        self.i_limit = float(self.opt("current_limit_A", 90.0))
        self.inductance = float(self.opt("inductance_H", 60.0))
        self.units = int(self.opt("field_units", 1))              # 0 kG, 1 T (front panel setting, kept)
        self.rate_units = int(self.opt("ramp_rate_units", 0))     # 0 per second, 1 per minute
        self.pswitch = bool(self.opt("persistent_switch", False))

    def reset(self) -> None:
        self.target_A = 0.0
        self.segments = [[0.005 / self.coil, self.i_limit]]       # A/s up to |I| <= bound
        self.state = 8 if abs(self.current()) < 1e-6 else 2
        self.switch_heated = not self.pswitch
        self.quenched = False

    def current(self) -> float:
        return self.world.magnet.field.get(self.axis, 0.0) / self.coil

    def status(self):
        return {"state": STATES.get(self.state), "field_T": self.world.magnet.field.get(self.axis, 0.0),
                "target_T": self.target_A * self.coil, "rate_T_s": self.rate_A_s(self.current()) * self.coil}

    def rate_A_s(self, i: float) -> float:
        for rate, bound in self.segments:
            if abs(i) <= bound:
                return rate
        return self.segments[-1][0]

    def tick(self, dt: float, now: float) -> None:
        if self.world.magnet.quenched.get(self.axis):
            if self.state != 7:
                self.state, self.quenched = 7, True
            return
        if self.state not in (1, 6):
            return
        i = self.current()
        goal = 0.0 if self.state == 6 else self.target_A
        rate = self.rate_A_s(i)
        step = rate * dt
        if abs(goal - i) <= step:
            new, done = goal, True
        else:
            new, done = i + math.copysign(step, goal - i), False
        if not self.world.magnet.set(self.axis, new * self.coil, rate * self.coil):
            self.state, self.quenched = 7, True
            return
        if done:
            self.state = 8 if self.state == 6 else 2

    # -- units: the 430 speaks kG or T, per second or per minute, as set
    def to_A(self, field: float) -> float:
        return field / (self.coil * (10.0 if self.units == 0 else 1.0))

    def from_A(self, amps: float) -> float:
        return amps * self.coil * (10.0 if self.units == 0 else 1.0)

    def rate_out(self, a_per_s: float) -> float:
        return self.from_A(a_per_s) * (60.0 if self.rate_units == 1 else 1.0)

    def rate_in(self, v: float) -> float:
        return self.to_A(v) / (60.0 if self.rate_units == 1 else 1.0)

    @command("CONFigure:FIELD:TARGet", query=False)
    def _conf_target(self, q, a, ch):
        amps = self.to_A(a.num(0))
        if abs(amps) > self.i_limit:
            raise fail(OUT_OF_RANGE, "beyond the current limit")
        self.target_A = amps

    @command("FIELD:TARGet", setter=False)
    def _target(self, q, a, ch):
        return "%.6f" % self.from_A(self.target_A)

    @command("CONFigure:CURRent:TARGet", query=False)
    def _conf_itarget(self, q, a, ch):
        amps = a.num(0)
        if abs(amps) > self.i_limit:
            raise fail(OUT_OF_RANGE)
        self.target_A = amps

    @command("CURRent:TARGet", setter=False)
    def _itarget(self, q, a, ch):
        return "%.6f" % self.target_A

    @command("FIELD:MAGnet", setter=False)
    def _field(self, q, a, ch):
        return "%.6f" % (self.from_A(self.current()) + self.noise(2e-6))

    @command("CURRent:MAGnet", "CURRent:SUPPly", setter=False)
    def _imag(self, q, a, ch):
        return "%.6f" % (self.current() + self.noise(1e-4))

    @command("VOLTage:MAGnet", "VOLTage:SUPPly", setter=False)
    def _vmag(self, q, a, ch):
        ramping = self.state in (1, 6)
        return "%.4f" % ((self.inductance * self.rate_A_s(self.current()) if ramping else 0.0) + self.noise(2e-3))

    @command("CONFigure:RAMP:RATE:FIELD", query=False)
    def _conf_rate_field(self, q, a, ch):
        a.need(3, 3)
        seg = a.int(0, 1, 10)
        rate, bound = self.rate_in(a.num(1)), abs(self.to_A(a.num(2)))
        if rate <= 0:
            raise fail(ILLEGAL)
        while len(self.segments) < seg:
            self.segments.append(list(self.segments[-1]))
        self.segments[seg - 1] = [rate, bound]

    @command("CONFigure:RAMP:RATE:CURRent", query=False)
    def _conf_rate_i(self, q, a, ch):
        a.need(3, 3)
        seg = a.int(0, 1, 10)
        rate = a.num(1) / (60.0 if self.rate_units == 1 else 1.0)
        while len(self.segments) < seg:
            self.segments.append(list(self.segments[-1]))
        self.segments[seg - 1] = [rate, abs(a.num(2))]

    @command("RAMP:RATE:FIELD:<n>", setter=False)
    def _rate_field(self, q, a, ch):
        seg = ch[0] or 1
        if seg > len(self.segments):
            raise fail(ILLEGAL)
        r, b = self.segments[seg - 1]
        return "%.6f,%.4f" % (self.rate_out(r), self.from_A(b))

    @command("RAMP:RATE:CURRent:<n>", setter=False)
    def _rate_i(self, q, a, ch):
        seg = ch[0] or 1
        r, b = self.segments[min(seg, len(self.segments)) - 1]
        return "%.6f,%.4f" % (r * (60.0 if self.rate_units == 1 else 1.0), b)

    @command("RAMP:RATE:SEGments", setter=False)
    def _nseg(self, q, a, ch):
        return str(len(self.segments))

    @command("CONFigure:RAMP:RATE:SEGments", query=False)
    def _conf_nseg(self, q, a, ch):
        n = a.int(0, 1, 10)
        self.segments = (self.segments + [list(self.segments[-1])] * n)[:n]

    @command("RAMP:RATE:UNITS", setter=False)
    def _rate_units(self, q, a, ch):
        return str(self.rate_units)

    @command("CONFigure:RAMP:RATE:UNITS", query=False)
    def _conf_rate_units(self, q, a, ch):
        self.rate_units = a.int(0, 0, 1)

    @command("FIELD:UNITS", setter=False)
    def _units(self, q, a, ch):
        return str(self.units)

    @command("CONFigure:FIELD:UNITS", query=False)
    def _conf_units(self, q, a, ch):
        self.units = a.int(0, 0, 1)

    @command("COILconst", setter=False)
    def _coil(self, q, a, ch):
        return "%.6f" % (self.coil * (10.0 if self.units == 0 else 1.0))

    @command("CURRent:LIMit", setter=False)
    def _ilim(self, q, a, ch):
        return "%.4f" % self.i_limit

    @command("RAMP", query=False)
    def _ramp(self, q, a, ch):
        if self.state == 7:
            raise fail((-200, "Execution error"), "quench condition; clear it with QUench 0")
        if not self.switch_heated:
            raise fail((-200, "Execution error"), "persistent switch not heated")
        self.state = 1

    @command("PAUSE", query=False)
    def _pause(self, q, a, ch):
        if self.state != 7:
            self.state = 3

    @command("ZERO", query=False)
    def _zero(self, q, a, ch):
        if self.state == 7:
            raise fail((-200, "Execution error"), "quench condition")
        self.state = 6

    @command("STATE", setter=False)
    def _state(self, q, a, ch):
        return str(self.state)

    @command("QUench")
    def _quench(self, q, a, ch):
        if q:
            return onoff(self.state == 7)
        if not a.bool(0) and self.state == 7:
            self.world.magnet.quenched[self.axis] = False
            self.state, self.quenched = 8, False

    @command("PSwitch")
    def _ps(self, q, a, ch):
        if q:
            return onoff(self.switch_heated and self.pswitch)
        if not self.pswitch:
            raise fail((-200, "Execution error"), "no persistent switch installed")
        self.switch_heated = a.bool(0)
        self.state = 9 if self.switch_heated else 10
        self.busy_until = self.now + 20.0

    @command("PSwitch:INSTalled", setter=False)
    def _ps_inst(self, q, a, ch):
        return onoff(self.pswitch)

    @command("SYSTem:LOCal", "SYSTem:REMote", "CONFigure:QUench:DETect", "QUench:DETect", "CONFigure:STABility",
             "STABility", "CONFigure:COILconst", "CONFigure:CURRent:LIMit", "CONFigure:PSwitch", "PSwitch:CURRent",
             "CONFigure:FIELD:SUPPly", "FIELD:SUPPly")
    def _misc(self, q, a, ch):
        return "0" if q else None
