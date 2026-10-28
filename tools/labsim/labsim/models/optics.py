"""The optical table's instruments: Coherent OBIS lasers, a Thorlabs PM100D power meter, a PI E-727 piezo
controller (GCS), attocube's ANC300 positioner controller, a Newport SMC100 delay stage."""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional

from ..instrument import UNDEFINED, Args, Chunk, CommandError, Instrument, command, fail, fmt_e, onoff
from .common import Wired


def _optics_axis(world, axis: str):
    """('fine'|'coarse', index) for x, y, z."""
    return {"x": 0, "y": 1, "z": 2}[axis]


class CoherentOBIS(Wired, Instrument):
    """A laser on a serial port (115200 8N1) that answers every command with OK (handshake on)."""
    model = "coherent_obis"
    idn = "Coherent, Inc - OBIS {nm}nm LS {mw}mW - V1.0.1 - Dec 14 2010"
    default_serial = "A1B2C3"
    has_syst_err = False
    drops_unread = False
    terminators = (b"\r\n", b"\n", b"\r")
    reply_end = b"\r\n"
    framing = (115200, 8, "none", 1.0)
    usb_ids = "0d4d:003d"

    def setup(self) -> None:
        self.nm = float(self.opt("wavelength_nm", 532))
        self.pmax = float(self.opt("max_power_W", 0.02))
        self.interlock_ok = True

    def identity(self) -> str:
        return str(self.cfg.get("idn") or self.idn.format(nm=int(self.nm), mw=int(round(self.pmax * 1e3))))

    def reset(self) -> None:
        self.on, self.setpoint, self.handshake, self.mode = False, 0.0, True, "CWP"
        self.actual = 0.0
        self._emit()

    def fault(self, name: str, on: bool):
        if name == "interlock_open":
            self.interlock_ok = not on
            if on:
                self.on = False
            self._emit()
            return "key interlock %s" % ("open: emission off" if on else "closed")
        return None

    def _emit(self) -> None:
        self.actual = self.setpoint if self.on and self.interlock_ok else 0.0
        self.world.optics.lasers[self.name] = {"nm": self.nm, "W": self.actual, "on": self.actual > 0,
                                               "path": self.opt("path", "excitation"),
                                               "shutter": self.opt("shutter", "")}

    def status(self):
        return {"emission": self.on, "setpoint_W": self.setpoint, "emitting_W": self.actual}

    def handle(self, text: str):
        """Its replies: the value (for a query) and then OK; ERR-xxx for what it doesn't know."""
        try:
            reply = super().handle(text)
        except CommandError as e:
            return "ERR-100" if e.code == UNDEFINED[0] else "ERR-%03d" % abs(e.code % 1000)
        if not self.handshake:
            return reply
        return "OK" if reply is None else "%s\r\nOK" % reply

    @command("SOURce:AM:STATe")
    def _state(self, q, a, ch):
        if q:
            return "ON" if self.on else "OFF"
        on = a.bool(0)
        if on and not self.interlock_ok:
            raise CommandError(-350, "interlock")
        self.on = on
        self._emit()

    @command("SOURce:POWer:LEVel:IMMediate:AMPLitude")
    def _setp(self, q, a, ch):
        if q:
            return "%.5f" % self.setpoint
        v = a.num(0)
        if not 0.0 <= v <= self.pmax * 1.1:
            raise CommandError(-222, "out of range")
        self.setpoint = v
        self._emit()

    @command("SOURce:POWer:LEVel", setter=False)
    def _actual(self, q, a, ch):
        return "%.5f" % max(0.0, self.actual * (1 + self.noise(0.002)))

    @command("SOURce:POWer:LIMit:HIGH", setter=False)
    def _pmax(self, q, a, ch):
        return "%.5f" % self.pmax

    @command("SOURce:POWer:LIMit:LOW", setter=False)
    def _pmin(self, q, a, ch):
        return "%.5f" % 0.0

    @command("SYSTem:INFormation:WAVelength", setter=False)
    def _wl(self, q, a, ch):
        return "%d" % int(self.nm)

    @command("SYSTem:COMMunicate:HANDshaking")
    def _hand(self, q, a, ch):
        if q:
            return "ON" if self.handshake else "OFF"
        self.handshake = a.bool(0)

    @command("SOURce:AM:INTernal", query=False)
    def _int(self, q, a, ch):
        self.mode = a.word(0, ["CWP", "CWC"])

    @command("SOURce:AM:SOURce", setter=False)
    def _src(self, q, a, ch):
        return self.mode

    @command("SYSTem:STATus", setter=False)
    def _stat(self, q, a, ch):
        return "%08X" % ((0x1 if self.on else 0) | (0 if self.interlock_ok else 0x4000))

    @command("SYSTem:FAULt", setter=False)
    def _fault(self, q, a, ch):
        return "%08X" % (0 if self.interlock_ok else 0x4000)

    @command("SYSTem:LOCK", setter=False)
    def _lock(self, q, a, ch):
        return "ON" if self.interlock_ok else "OFF"

    @command("SOURce:TEMPerature:BASeplate", "SOURce:TEMPerature:DIODe", setter=False)
    def _temp(self, q, a, ch):
        return "%.2fC" % (25.0 + 3.0 * self.actual / max(self.pmax, 1e-9))

    @command("SYSTem:INFormation:MODel", "SYSTem:INFormation:SNUMber", setter=False)
    def _info(self, q, a, ch):
        return self.serial_no


class ThorlabsPM100D(Wired, Instrument):
    model = "thorlabs_pm100d"
    idn = "Thorlabs,PM100D,{serial},2.6.0"
    default_serial = "P0015055"

    def reset(self) -> None:
        self.wl, self.avg, self.unit, self.auto = 633.0, 1, "W", True

    @staticmethod
    def responsivity(nm: float) -> float:
        """A silicon photodiode (S120C), A/W."""
        return max(1e-3, 0.62 * (nm / 960.0) * math.exp(-((nm - 930.0) / 380.0) ** 4))

    def _power(self) -> float:
        opt = self.world.optics
        p = nmw = 0.0
        for l in opt.lasers.values():
            if l.get("on") and opt.shutter_open.get(l.get("shutter", ""), True):
                p += l["W"]
                nmw += l["W"] * l["nm"]
        if p <= 0:
            return abs(self.noise(2e-9)) + 3e-9
        actual_nm = nmw / p
        p *= opt.pm_fraction * self.responsivity(actual_nm) / self.responsivity(self.wl)
        return p * (1 + self.noise(0.003 / math.sqrt(self.avg))) + self.noise(1e-9)

    def _read(self) -> str:
        self.delay(0.003 * self.avg + 0.01)
        p = self._power()
        return "%.9E" % (10 * math.log10(max(p, 1e-15) / 1e-3) if self.unit == "DBM" else p)

    @command("MEASure[:SCALar][:POWer]", "READ", "FETCh", setter=False)
    def _meas(self, q, a, ch):
        return self._read()

    @command("[SENSe:]CORRection:WAVelength")
    def _wl(self, q, a, ch):
        if q:
            return "%.9E" % self.wl
        self.wl = a.num(0, 400.0, 1100.0, minmax=(400.0, 1100.0, 633.0))

    @command("[SENSe:]AVERage[:COUNt]")
    def _avg(self, q, a, ch):
        if q:
            return str(self.avg)
        self.avg = a.int(0, 1, 100000)

    @command("[SENSe:]POWer[:DC]:UNIT")
    def _unit(self, q, a, ch):
        if q:
            return self.unit
        self.unit = a.word(0, ["W", "DBM"])

    @command("[SENSe:]POWer[:DC]:RANGe:AUTO")
    def _auto(self, q, a, ch):
        if q:
            return onoff(self.auto)
        self.auto = a.bool(0)

    @command("SYSTem:SENSor:IDN", setter=False)
    def _sensor(self, q, a, ch):
        return "S120C,15051234,07-Mar-2019,1,18,289"

    @command("CONFigure[:SCALar]:POWer", "INITiate[:IMMediate]", "ABORt", "[SENSe:]CORRection:COLLect:ZERO[:INITiate]",
             "[SENSe:]CORRection[:LOSS][:INPut][:MAGNitude]", "SYSTem:LFRequency", "DISPlay:BRIGhtness",
             "INPut[:PDIode]:FILTer[:LPASs][:STATe]", "[SENSe:]POWer[:DC]:RANGe[:UPPer]")
    def _misc(self, q, a, ch):
        return "0" if q else None


class PIE727(Wired, Instrument):
    """PI's GCS 2.0 on TCP 50000: servo off at power-up, so MOV is refused (error 5) until SVO; answers are
    "axis=value", and the lines of a multi-line answer end in a space before the line feed."""
    model = "pi_e727"
    idn = "(c)2015 Physik Instrumente (PI) GmbH & Co. KG, E-727, {serial}, 13.21.00.08"
    default_serial = "0119019744"
    has_syst_err = False
    drops_unread = False
    param_split = None

    def setup(self) -> None:
        self.axes: Dict[int, str] = {int(k): v for k, v in (self.opt("axes", {1: "x", 2: "y", 3: "z"})).items()}
        self.travel = {k: (0.0, float(self.opt("travel_um", 100.0)) if v != "z" else float(self.opt("z_travel_um", 50.0)))
                       for k, v in self.axes.items()}

    def reset(self) -> None:
        self.err = 0
        self.servo = {k: bool(self.opt("servo_on_at_start", False)) for k in self.axes}
        opt = self.world.optics
        self.target = {k: opt.fine[_optics_axis(self.world, v)] for k, v in self.axes.items()}
        self.vel = {k: 10000.0 for k in self.axes}

    def status(self):
        opt = self.world.optics
        return {"servo": self.servo, "pos_um": {k: round(opt.fine[_optics_axis(self.world, v)], 4)
                                                for k, v in self.axes.items()}}

    def tick(self, dt: float, now: float) -> None:
        opt = self.world.optics
        for k, v in self.axes.items():
            if not self.servo[k]:
                continue
            i = _optics_axis(self.world, v)
            d = self.target[k] - opt.fine[i]
            step = max(self.vel[k] * dt, abs(d) * (1 - math.exp(-dt / 0.004)))
            opt.fine[i] = self.target[k] if abs(d) <= step else opt.fine[i] + math.copysign(step, d)

    def handle(self, text: str):
        try:
            return super().handle(text)
        except CommandError as e:
            self.err = e.code if e.code > 0 else 2
            return None

    def _pairs(self, a: Args) -> List[tuple]:
        if len(a) % 2:
            raise CommandError(2, "pairs")
        return [(int(a[i]), a[i + 1]) for i in range(0, len(a), 2)]

    def _axes_of(self, a: Args) -> List[int]:
        out = [int(x) for x in a] if len(a) else sorted(self.axes)
        for k in out:
            if k not in self.axes:
                raise CommandError(15, "Invalid axis identifier")
        return out

    def _answer(self, values: Dict[int, str]) -> str:
        lines = ["%d=%s" % kv for kv in values.items()]
        return " \n".join(lines)

    @command("SAI", setter=False)
    def _sai(self, q, a, ch):
        return " \n".join(str(k) for k in sorted(self.axes))

    @command("ERR", setter=False)
    def _errq(self, q, a, ch):
        v, self.err = self.err, 0
        return str(v)

    @command("SVO")
    def _svo(self, q, a, ch):
        if q:
            return self._answer({k: "1" if self.servo[k] else "0" for k in self._axes_of(a)})
        for k, v in self._pairs(a):
            if k not in self.axes:
                raise CommandError(15, "Invalid axis identifier")
            self.servo[k] = v.strip() in ("1", "ON")
            if self.servo[k]:
                self.target[k] = self.world.optics.fine[_optics_axis(self.world, self.axes[k])]

    @command("MOV")
    def _mov(self, q, a, ch):
        if q:
            return self._answer({k: "%.7f" % self.target[k] for k in self._axes_of(a)})
        moves = self._pairs(a)
        for k, v in moves:
            if k not in self.axes:
                raise CommandError(15, "Invalid axis identifier")
            if not self.servo[k]:
                raise CommandError(5, "Unallowable move attempted on unreferenced axis, or move with servo off")
            lo, hi = self.travel[k]
            if not lo <= float(v) <= hi:
                raise CommandError(7, "Position out of limits")
        for k, v in moves:
            self.target[k] = float(v)

    @command("MVR", query=False)
    def _mvr(self, q, a, ch):
        for k, v in self._pairs(a):
            if not self.servo.get(k):
                raise CommandError(5, "servo off")
            self.target[k] = min(self.travel[k][1], max(self.travel[k][0], self.target[k] + float(v)))

    @command("POS", setter=False)
    def _pos(self, q, a, ch):
        opt = self.world.optics
        return self._answer({k: "%.7f" % (opt.fine[_optics_axis(self.world, self.axes[k])] + self.noise(2e-4))
                             for k in self._axes_of(a)})

    @command("ONT", setter=False)
    def _ont(self, q, a, ch):
        opt = self.world.optics
        return self._answer({k: "1" if abs(opt.fine[_optics_axis(self.world, self.axes[k])] - self.target[k]) < 0.01
                             else "0" for k in self._axes_of(a)})

    @command("TMN", setter=False)
    def _tmn(self, q, a, ch):
        return self._answer({k: "%.7f" % self.travel[k][0] for k in self._axes_of(a)})

    @command("TMX", setter=False)
    def _tmx(self, q, a, ch):
        return self._answer({k: "%.7f" % self.travel[k][1] for k in self._axes_of(a)})

    @command("VEL")
    def _vel(self, q, a, ch):
        if q:
            return self._answer({k: "%.7f" % self.vel[k] for k in self._axes_of(a)})
        for k, v in self._pairs(a):
            self.vel[k] = float(v)

    @command("CSV", setter=False)
    def _csv(self, q, a, ch):
        return "2"

    @command("HLT", "STP", query=False)
    def _stop(self, q, a, ch):
        opt = self.world.optics
        for k, v in self.axes.items():
            self.target[k] = opt.fine[_optics_axis(self.world, v)]
        self.err = 10


class AttocubeANC300(Wired, Instrument):
    """The ANC300's command line on its serial port (38400 8N1) or TCP 7230 (password first): it echoes commands
    unless `echo off`, answers in lines ending with OK or ERROR, and steps only in stp mode. Steps move the coarse
    positioner by about 50 nm at 30 V."""
    model = "attocube_anc300"
    idn = "attocube ANC300 controller version 1.1.0-1304 2013-10-17 08:16"
    default_serial = "ANC300B-C-1514-3006076"
    has_488 = False
    has_syst_err = False
    drops_unread = False
    terminators = (b"\r\n", b"\n", b"\r")
    reply_end = b"\r\n"
    param_split = None
    framing = (38400, 8, "none", 1.0)
    usb_ids = "0403:6001"

    def setup(self) -> None:
        self.axes: Dict[int, str] = {int(k): v for k, v in (self.opt("axes", {1: "x", 2: "y", 3: "z"})).items()}
        self.um_per_step = float(self.opt("um_per_step_30V", 0.05))

    def reset(self) -> None:
        self.mode = {k: "gnd" for k in self.axes}
        self.volt = {k: 30.0 for k in self.axes}
        self.freq = {k: 1000.0 for k in self.axes}
        self.offs = {k: 0.0 for k in self.axes}
        self.pending = {k: 0.0 for k in self.axes}   # steps still to make (signed)
        self.echo_on = True
        self.authorized = False

    def status(self):
        return {"modes": self.mode, "coarse_um": [round(v, 3) for v in self.world.optics.coarse],
                "moving": {k: v for k, v in self.pending.items() if v}}

    def on_open(self, bus: str, now: float) -> None:
        self.authorized = bus != "socket"
        if bus == "socket":
            self.out.append(Chunk(b"Authorization code: ", now, False))

    def tick(self, dt: float, now: float) -> None:
        for k, n in self.pending.items():
            if not n:
                continue
            m = min(abs(n), self.freq[k] * dt)
            self.pending[k] = n - math.copysign(m, n) if abs(n) > m else 0.0
            i = _optics_axis(self.world, self.axes[k])
            self.world.optics.coarse[i] += math.copysign(m, n) * self.um_per_step * self.volt[k] / 30.0

    def handle(self, text: str):
        if not self.authorized:
            if text.strip() == str(self.opt("password", "123456")):
                self.authorized = True
                return "Authorization success\r\nOK"
            return "Authorization failed\r\nERROR"
        echo = (text + "\r\n") if self.echo_on else ""
        words = text.split()
        if not words:
            return None
        cmd, args = words[0].lower(), words[1:]
        try:
            body = self.run(cmd, args)
            return echo + (body + "\r\n" if body else "") + "OK"
        except CommandError as e:
            return echo + e.text + "\r\nERROR"

    def _axis(self, args: List[str]) -> int:
        try:
            k = int(args[0])
        except (IndexError, ValueError):
            raise CommandError(1, "Wrong axis number")
        if k not in self.axes:
            raise CommandError(1, "Wrong axis type")
        return k

    def run(self, cmd: str, args: List[str]) -> str:
        if cmd == "echo":
            self.echo_on = (args or ["on"])[0] == "on"
            return ""
        if cmd == "ver":
            return self.identity()
        if cmd == "getserial":
            return self.serial_no
        if cmd in ("getm", "getv", "getf", "geta", "geto"):
            k = self._axis(args)
            if cmd == "getm":
                return "mode = %s" % self.mode[k]
            if cmd == "getv":
                return "voltage = %.6f V" % self.volt[k]
            if cmd == "getf":
                return "frequency = %d Hz" % int(self.freq[k])
            if cmd == "geta":
                return "voltage = %.6f V" % self.offs[k]
            return "voltage = %.6f V" % (self.offs[k] if self.mode[k] in ("offs", "stp+", "stp-") else 0.0)
        if cmd in ("setm", "setv", "setf", "seta"):
            k = self._axis(args)
            if len(args) < 2:
                raise CommandError(2, "Missing parameter")
            v = args[1]
            if cmd == "setm":
                if v not in ("gnd", "stp", "cap", "offs", "stp+", "stp-", "inp"):
                    raise CommandError(3, "Wrong mode")
                self.mode[k] = v
                if v == "gnd":
                    self.pending[k] = 0.0
            else:
                try:
                    x = float(v)
                except ValueError:
                    raise CommandError(2, "Wrong parameter")
                lim = {"setv": (0, 150), "setf": (1, 10000), "seta": (0, 150)}[cmd]
                if not lim[0] <= x <= lim[1]:
                    raise CommandError(4, "Value out of range")
                {"setv": self.volt, "setf": self.freq, "seta": self.offs}[cmd][k] = x
            return ""
        if cmd in ("stepu", "stepd"):
            k = self._axis(args)
            if self.mode[k] not in ("stp", "stp+", "stp-"):
                raise CommandError(5, "Axis not in stepping mode")
            n = int(args[1]) if len(args) > 1 and args[1] != "c" else 10 ** 9
            self.pending[k] += n if cmd == "stepu" else -n
            return ""
        if cmd == "stepw":
            k = self._axis(args)
            self.delay(abs(self.pending[k]) / self.freq[k])
            return ""
        if cmd == "stop":
            self.pending[self._axis(args)] = 0.0
            return ""
        if cmd == "getc":
            k = self._axis(args)
            return "capacitance = %d nF" % int(1020 + 30 * k)
        if cmd in ("capw", "setdci", "setaci", "getdci", "getaci", "settu", "settd", "setpatu", "setpatd"):
            self._axis(args)
            return ""
        raise CommandError(9, "Unknown command: %s" % cmd)


class NewportSMC100(Wired, Instrument):
    """A delay line on an SMC100CC (57600 8N1): addressed commands (1TP, 1PA10.5), not referenced after power-up
    so it refuses moves until homed (1OR); 1TE gives the last error letter, 1TH the position a move is going to."""
    model = "newport_smc100"
    idn = "SMC100CC"
    default_serial = "B092345"
    has_488 = False
    has_syst_err = False
    drops_unread = False
    terminators = (b"\r\n", b"\n", b"\r")
    reply_end = b"\r\n"
    framing = (57600, 8, "none", 1.0)
    usb_ids = "104d:3008"

    def setup(self) -> None:
        self.addr = int(self.opt("controller_address", 1))
        self.travel = (0.0, float(self.opt("travel_mm", 25.0)))

    def reset(self) -> None:
        self.state, self.err = "0A", "@"
        self.target = self.pos = 0.0
        self.vel = 2.5
        self.world.optics.delay_mm = self.pos

    def status(self):
        return {"state": self.state, "position_mm": round(self.pos, 5), "target_mm": self.target}

    def tick(self, dt: float, now: float) -> None:
        if self.state in ("28", "1E"):
            d = self.target - self.pos
            step = self.vel * dt
            if abs(d) <= step:
                self.pos = self.target
                self.state = "33" if self.state == "28" else "32"
            else:
                self.pos += math.copysign(step, d)
            self.world.optics.delay_mm = self.pos

    def handle(self, text: str):
        m = re.fullmatch(r"\s*(\d*)\s*([A-Za-z]{2})\s*(\?)?\s*([-+0-9.Ee]*)\s*", text)
        if not m or (m.group(1) and int(m.group(1)) != self.addr):
            return None
        cmd, q, val = m.group(2).upper(), m.group(3), m.group(4)
        a = str(self.addr)
        if cmd == "TS":
            return "%sTS0000%s" % (a, self.state)
        if cmd == "TE":
            e, self.err = self.err, "@"
            return "%sTE%s" % (a, e)
        if cmd == "TP":
            return "%sTP%.6f" % (a, self.pos)
        if cmd == "TH":   # the set-point position: where a move is going (TP is where the stage is now)
            return "%sTH%.6f" % (a, self.target)
        if cmd == "ID" and q:
            return "%sID%s" % (a, self.opt("stage", "LTA-HS"))
        if cmd == "VE":
            return "%sVE SMC_CC - Controller-driver version 3. 0. 0" % a
        if cmd == "OR":
            if self.state not in ("0A", "0B", "0C", "0D", "0E", "0F", "10"):
                self.err = "H"
                return None
            self.state, self.target = "1E", 0.0
            return None
        if cmd == "PA":
            if q:
                return "%sPA%.6f" % (a, self.target)
            if self.state not in ("32", "33", "34"):
                self.err = "H"   # not allowed in this state (not referenced)
                return None
            try:
                v = float(val)
            except ValueError:
                self.err = "A"
                return None
            if not self.travel[0] <= v <= self.travel[1]:
                self.err = "C"
                return None
            self.target, self.state = v, "28"
            return None
        if cmd == "PR":
            if self.state not in ("32", "33", "34"):
                self.err = "H"
                return None
            self.target, self.state = min(self.travel[1], max(self.travel[0], self.pos + float(val or 0))), "28"
            return None
        if cmd == "VA":
            if q:
                return "%sVA%.6f" % (a, self.vel)
            self.vel = float(val)
            return None
        if cmd == "ST":
            self.target, self.state = self.pos, "33"
            return None
        if cmd == "RS":
            self.reset()
            return None
        if cmd in ("SL", "SR"):
            return "%s%s%.6f" % (a, cmd, self.travel[0] if cmd == "SL" else self.travel[1]) if q else None
        self.err = "A"   # unknown message code
        return None
