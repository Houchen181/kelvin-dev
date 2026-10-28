"""Keysight / HP: the 34401A and 34465A multimeters, the PNA network analyzer, the MXG N5183B generator."""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional

import numpy as np

from ..instrument import CONFLICT, ILLEGAL, Instrument, OUT_OF_RANGE, command, fail, fmt_e, onoff
from ..world import Source, cernox_ohm
from .common import Wired


class HP34401A(Wired, Instrument):
    model = "hp_34401a"
    idn = "HEWLETT-PACKARD,34401A,0,11-5-2"
    error_format = '{code:+d},"{text}"'
    terminators = (b"\n", b"\r")
    framing = (9600, 8, "none", 2.0)
    digits = 8
    NPLCS = (0.02, 0.2, 1.0, 10.0, 100.0)

    def reset(self) -> None:
        self.func, self.rng, self.auto, self.nplc, self.azero = "VOLT", 10.0, True, 10.0, True
        self.trig_count, self.samples = 1, 1
        self.initiated = False
        self.last: List[float] = []

    def _one(self) -> float:
        if self.func == "RES" or self.func == "FRES":
            term, _ = self.terminal("res")
            if term and term.startswith("rtd:"):
                r = cernox_ohm(self.world.cryo.sensor(term[4:]))
            else:
                r = float(term) if term else 9.9e37
            return r * (1 + self.noise(2e-6)) if r < 1e9 else 9.9e37
        if self.func == "CURR":
            return self.noise(5e-10)
        if self.func == "VOLT:AC":
            term, _ = self.terminal("in")
            sig = self.world.voltage(term) if term in ("vxx", "vxy", "pd") else {}
            return math.sqrt(sum(abs(a) ** 2 for f, a in sig.items() if f > 0)) + abs(self.noise(2e-6))
        v = self.dc_on("in") + self.noise(3e-7 * math.sqrt(10.0 / self.nplc))
        if not self.auto and abs(v) > 1.2 * self.rng:
            return 9.9e37
        return v

    def _measure(self) -> str:
        n = self.trig_count * self.samples
        self.delay(n * (self.nplc / 60.0 * (2.0 if self.azero else 1.0) + 0.004))
        self.last = [self._one() for _ in range(n)]
        return ",".join(fmt_e(v, self.digits) for v in self.last)

    def _conf(self, func: str, a) -> None:
        self.func = func
        if len(a) and a[0].strip().upper() not in ("AUTO", "DEF", "DEFAULT"):
            self.rng, self.auto = abs(a.num(0, minmax=(0.1, 1000.0, 10.0))), False
        else:
            self.auto = True

    @command("MEASure:VOLTage[:DC]", setter=False)
    def _meas_v(self, q, a, ch):
        self._conf("VOLT", a)
        return self._measure()

    @command("MEASure:VOLTage:AC", setter=False)
    def _meas_vac(self, q, a, ch):
        self._conf("VOLT:AC", a)
        return self._measure()

    @command("MEASure:CURRent[:DC]", setter=False)
    def _meas_i(self, q, a, ch):
        self._conf("CURR", a)
        return self._measure()

    @command("MEASure:RESistance", "MEASure:FRESistance", setter=False)
    def _meas_r(self, q, a, ch):
        self._conf("RES", a)
        return self._measure()

    @command("CONFigure:VOLTage[:DC]", query=False)
    def _conf_v(self, q, a, ch):
        self._conf("VOLT", a)

    @command("CONFigure:VOLTage:AC", query=False)
    def _conf_vac(self, q, a, ch):
        self._conf("VOLT:AC", a)

    @command("CONFigure:CURRent[:DC]", query=False)
    def _conf_i(self, q, a, ch):
        self._conf("CURR", a)

    @command("CONFigure:RESistance", "CONFigure:FRESistance", query=False)
    def _conf_r(self, q, a, ch):
        self._conf("RES", a)

    @command("CONFigure", setter=False)
    def _conf_q(self, q, a, ch):
        return '"%s %s,%s"' % (self.func, fmt_e(self.rng), fmt_e(3e-6 * self.rng))

    @command("READ", setter=False)
    def _read(self, q, a, ch):
        return self._measure()

    @command("INITiate[:IMMediate]", query=False)
    def _init(self, q, a, ch):
        self.initiated = True
        self.busy_until = self.now + self.trig_count * self.samples * (self.nplc / 60.0 * 2 + 0.004)
        self.last = [self._one() for _ in range(self.trig_count * self.samples)]

    @command("FETCh", setter=False)
    def _fetch(self, q, a, ch):
        if not self.initiated:
            raise fail((-230, "Data stale"))
        self.delay(max(0.0, self.busy_until - self.now))
        return ",".join(fmt_e(v, self.digits) for v in self.last)

    @command("[SENSe:]VOLTage[:DC]:NPLCycles", "[SENSe:]RESistance:NPLCycles", "[SENSe:]CURRent[:DC]:NPLCycles",
             "[SENSe:]FRESistance:NPLCycles")
    def _nplc(self, q, a, ch):
        if q:
            return fmt_e(self.nplc, self.digits)
        v = a.num(0, minmax=(0.02, 100.0, 10.0))
        if v not in self.NPLCS:
            v = min(self.NPLCS, key=lambda x: abs(x - v))
        self.nplc = v

    @command("[SENSe:]VOLTage[:DC]:RANGe", "[SENSe:]RESistance:RANGe")
    def _range(self, q, a, ch):
        if q:
            return fmt_e(self.rng, self.digits)
        self.rng, self.auto = abs(a.num(0, minmax=(0.1, 1000.0, 10.0))), False

    @command("[SENSe:]VOLTage[:DC]:RANGe:AUTO", "[SENSe:]RESistance:RANGe:AUTO")
    def _auto(self, q, a, ch):
        if q:
            return onoff(self.auto)
        self.auto = a.bool(0)

    @command("[SENSe:]ZERO:AUTO", "[SENSe:]VOLTage[:DC]:ZERO:AUTO")
    def _azero(self, q, a, ch):
        if q:
            return onoff(self.azero)
        t = a.text(0).upper()
        self.azero = t not in ("OFF", "0", "ONCE")

    @command("TRIGger:COUNt")
    def _tcount(self, q, a, ch):
        if q:
            return fmt_e(self.trig_count, self.digits)
        self.trig_count = a.int(0, 1, 50000)

    @command("SAMPle:COUNt")
    def _scount(self, q, a, ch):
        if q:
            return fmt_e(self.samples, self.digits)
        self.samples = a.int(0, 1, 50000)

    @command("[SENSe:]FUNCtion")
    def _func(self, q, a, ch):
        if q:
            return '"%s"' % self.func
        self.func = a.text(0).upper().split(":")[0][:4]

    @command("TRIGger:SOURce", "TRIGger:DELay", "TRIGger:DELay:AUTO", "SYSTem:REMote", "SYSTem:LOCal",
             "DISPlay[:STATe]", "DISPlay:TEXT[:DATA]", "DISPlay:TEXT:CLEar", "SYSTem:BEEPer[:STATe]",
             "[SENSe:]VOLTage[:DC]:IMPedance:AUTO", "INPut:IMPedance:AUTO", "[SENSe:]VOLTage[:DC]:RESolution",
             "DATA:POINts", "ABORt", "CALCulate:STATe", "CALCulate:FUNCtion")
    def _misc(self, q, a, ch):
        return "0" if q else None


class Keysight34465A(HP34401A):
    model = "keysight_34465a"
    idn = "Keysight Technologies,34465A,{serial},A.03.01-02.40-03.01-00.52-04-01"
    default_serial = "MY57501234"
    digits = 14
    NPLCS = (0.001, 0.002, 0.006, 0.02, 0.06, 0.2, 1.0, 10.0, 100.0)


class KeysightPNA(Wired, Instrument):
    """N52xxB: channel 1, measurements by name, LIN or CW sweeps; S21 of the chip it is wired to."""
    model = "keysight_pna"
    idn = "Keysight Technologies,N5222B,{serial},A.17.20.05"
    default_serial = "MY57311204"

    def reset(self) -> None:
        self.meas: Dict[str, str] = {"CH1_S11_1": "S11"}
        self.fmt: Dict[str, str] = {"CH1_S11_1": "MLOG"}
        self.sel: Optional[str] = "CH1_S11_1"
        self.start, self.stop, self.points, self.ifbw = 10e6, 26.5e9, 201, 100e3
        self.cw, self.sweep_type, self.power, self.rf_on = 1e9, "LIN", -10.0, True
        self.avg_on, self.avg_count, self.continuous, self.trig = False, 1, True, "IMM"
        self.data_format, self.byte_order = "ASC", "NORM"
        self.pstart, self.pstop = -30.0, 0.0
        self.mode = "CONT"

    def status(self):
        return {"meas": self.meas, "sel": self.sel, "span_Hz": (self.start, self.stop), "points": self.points,
                "ifbw": self.ifbw, "power_dBm": self.power}

    def sweep_time(self) -> float:
        return self.points * (1.1 / self.ifbw + 2e-5) + 0.008

    def freqs(self) -> np.ndarray:
        if self.sweep_type in ("CW", "POW"):
            return np.full(self.points, self.cw)
        if self.sweep_type == "LOG":
            return np.logspace(math.log10(self.start), math.log10(self.stop), self.points)
        return np.linspace(self.start, self.stop, self.points)

    def trace(self, param: str) -> np.ndarray:
        f = self.freqs()
        if self.sweep_type == "POW":
            powers = np.linspace(self.pstart, self.pstop, self.points)
        else:
            powers = np.full(self.points, self.power)
        rng = np.random.default_rng(self.random.randrange(1 << 30))
        wired = self.terminal("port1")[0] == "chip_in" and self.terminal("port2")[0] == "chip_out"
        n_avg = self.avg_count if self.avg_on else 1
        if param in ("S21", "S12") and wired and self.rf_on:
            drive = None
            for s in self.world.sources("qubit_drive"):
                if s.kind == "RF" and s.ac:
                    drive = (next(iter(s.ac)), s.dc)
            chip = self.world.chip
            flux, T = self.world.flux(), self.world.cryo.sample
            if self.sweep_type == "POW":
                s = np.array([chip.s21(np.array([fi]), p, flux, T, drive)[0] for fi, p in zip(f, powers)])
                rel = np.array([chip.noise_rel(p, self.ifbw) for p in powers])
            else:
                s = chip.s21(f, self.power, flux, T, drive)
                rel = chip.noise_rel(self.power, self.ifbw)
            base = np.abs(s).mean()
            sigma = rel * base / math.sqrt(n_avg) + 10 ** (-110 / 20) * math.sqrt(self.ifbw / 10.0)
            return s + sigma * (rng.standard_normal(len(f)) + 1j * rng.standard_normal(len(f))) / math.sqrt(2)
        if param in ("S11", "S22"):
            refl = 0.18 * np.exp(-2j * math.pi * f * 3.1e-9) * (1 + 0.3 * np.cos(f / 0.7e9))
            return refl + 1e-3 * (rng.standard_normal(len(f)) + 1j * rng.standard_normal(len(f)))
        floor = 10 ** (-100 / 20) * math.sqrt(self.ifbw / 1e3)
        return floor * (rng.standard_normal(len(f)) + 1j * rng.standard_normal(len(f)))

    def _format(self, values: np.ndarray, pairs: bool) -> bytes:
        if self.data_format == "ASC":
            return ",".join("%+.12E" % v for v in values).encode()
        dtype = (">" if self.byte_order == "NORM" else "<") + ("f8" if self.data_format == "REAL64" else "f4")
        raw = values.astype(dtype).tobytes()
        n = str(len(raw))
        return b"#" + str(len(n)).encode() + n.encode() + raw

    def _name(self, a, i: int = 0) -> str:
        return a.text(i)

    # -- measurements
    @command("CALCulate#:PARameter:DEFine[:EXTended]", query=False)
    def _def(self, q, a, ch):
        a.need(2, 2)
        name, param = self._name(a), a.text(1).upper()
        if not re.fullmatch(r"S[1-4][1-4]", param):
            raise fail(ILLEGAL, param)
        if name in self.meas:
            raise fail((-200, "Execution error"), "a measurement with that name exists")
        self.meas[name], self.fmt[name] = param, "MLOG"

    @command("CALCulate#:PARameter:CATalog[:EXTended]", setter=False)
    def _cat(self, q, a, ch):
        return '"%s"' % ",".join("%s,%s" % kv for kv in self.meas.items()) if self.meas else '"NO CATALOG"'

    @command("CALCulate#:PARameter:SELect", query=False)
    def _sel(self, q, a, ch):
        name = self._name(a)
        if name not in self.meas:
            raise fail((-200, "Execution error"), "Parameter not found")
        self.sel = name

    @command("CALCulate#:PARameter:SELect", setter=False)
    def _sel_q(self, q, a, ch):
        return '"%s"' % (self.sel or "")

    @command("CALCulate#:PARameter:DELete[:NAME]", query=False)
    def _del(self, q, a, ch):
        self.meas.pop(self._name(a), None)
        if self.sel not in self.meas:
            self.sel = next(iter(self.meas), None)

    @command("CALCulate#:PARameter:DELete:ALL", query=False)
    def _del_all(self, q, a, ch):
        self.meas.clear()
        self.sel = None

    @command("CALCulate#:MEASure#:PARameter", "CALCulate#:MEASure#:DEFine")
    def _meas_def(self, q, a, ch):
        name = "CH%d_MEAS%d" % (ch[0] or 1, ch[1] or 1)
        if q:
            return '"%s"' % self.meas.get(name, "")
        self.meas[name], self.fmt[name] = a.text(0).upper(), "MLOG"
        self.sel = name

    @command("CALCulate#:FORMat", "CALCulate#:MEASure#:FORMat")
    def _calcfmt(self, q, a, ch):
        if self.sel is None:
            raise fail((-200, "Execution error"), "No measurement selected")
        if q:
            return self.fmt[self.sel]
        self.fmt[self.sel] = a.word(0, ["MLOGarithmic", "PHASe", "UPHase", "REAL", "IMAGinary", "MLINear", "SMITh",
                                         "POLar", "SWR", "GDELay"])[:4].replace("MLOG", "MLOG")

    @command("CALCulate#:DATA", "CALCulate#:MEASure#:DATA:FDATA", "CALCulate#:MEASure#:DATA:SDATA", setter=False)
    def _data(self, q, a, ch):
        if self.sel is None:
            raise fail((-200, "Execution error"), "No measurement selected")
        kind = a.text(0).upper() if a else "FDATA"
        s = self.trace(self.meas[self.sel])
        if kind == "SDATA":
            vals = np.empty(2 * len(s))
            vals[0::2], vals[1::2] = s.real, s.imag
            return self._format(vals, True)
        if kind != "FDATA":
            raise fail(ILLEGAL, kind)
        f = self.fmt[self.sel]
        if f.startswith("MLOG"):
            vals = 20 * np.log10(np.abs(s) + 1e-30)
        elif f.startswith("PHAS"):
            vals = np.degrees(np.angle(s))
        elif f.startswith("UPH"):
            vals = np.degrees(np.unwrap(np.angle(s)))
        elif f == "REAL":
            vals = s.real
        elif f.startswith("IMAG"):
            vals = s.imag
        elif f.startswith("MLIN"):
            vals = np.abs(s)
        else:
            vals = np.empty(2 * len(s))
            vals[0::2], vals[1::2] = s.real, s.imag
        return self._format(vals, False)

    @command("CALCulate#:X[:VALues]", "SENSe#:X[:VALues]", "CALCulate#:MEASure#:X[:VALues]", setter=False)
    def _x(self, q, a, ch):
        return self._format(self.freqs() if self.sweep_type != "POW" else np.linspace(self.pstart, self.pstop,
                                                                                     self.points), False)

    # -- the sweep
    @command("SENSe#:FREQuency:STARt")
    def _start(self, q, a, ch):
        if q:
            return "%+.12E" % self.start
        self.start = a.num(0, 10e6, 26.5e9, minmax=(10e6, 26.5e9, 10e6))
        self.stop = max(self.stop, self.start)

    @command("SENSe#:FREQuency:STOP")
    def _stop(self, q, a, ch):
        if q:
            return "%+.12E" % self.stop
        self.stop = a.num(0, 10e6, 26.5e9, minmax=(10e6, 26.5e9, 26.5e9))
        self.start = min(self.start, self.stop)

    @command("SENSe#:FREQuency:CENTer")
    def _center(self, q, a, ch):
        span = self.stop - self.start
        if q:
            return "%+.12E" % (0.5 * (self.start + self.stop))
        c = a.num(0, 10e6, 26.5e9)
        self.start, self.stop = max(10e6, c - span / 2), min(26.5e9, c + span / 2)

    @command("SENSe#:FREQuency:SPAN")
    def _span(self, q, a, ch):
        c = 0.5 * (self.start + self.stop)
        if q:
            return "%+.12E" % (self.stop - self.start)
        sp = a.num(0, 0, 26.49e9)
        self.start, self.stop = max(10e6, c - sp / 2), min(26.5e9, c + sp / 2)

    @command("SENSe#:FREQuency[:CW]", "SENSe#:FREQuency:FIXed")
    def _cw(self, q, a, ch):
        if q:
            return "%+.12E" % self.cw
        self.cw = a.num(0, 10e6, 26.5e9)

    @command("SENSe#:SWEep:POINts")
    def _points(self, q, a, ch):
        if q:
            return "%d" % self.points
        self.points = a.int(0, 1, 100003)

    @command("SENSe#:SWEep:TYPE")
    def _stype(self, q, a, ch):
        if q:
            return self.sweep_type
        self.sweep_type = a.word(0, ["LINear", "LOGarithmic", "POWer", "CW", "SEGMent", "PHASe"])[:4].rstrip("E")
        self.sweep_type = {"LINE": "LIN", "LOGA": "LOG", "SEGM": "SEGM"}.get(self.sweep_type, self.sweep_type)

    @command("SENSe#:SWEep:TIME")
    def _stime(self, q, a, ch):
        return "%+.12E" % self.sweep_time() if q else None

    @command("SENSe#:SWEep:MODE")
    def _smode(self, q, a, ch):
        if q:
            return self.mode
        self.mode = a.word(0, ["HOLD", "CONTinuous", "SINGle", "GROups"])[:4]
        if self.mode in ("SING", "GROU"):
            n = self.avg_count if (self.avg_on and self.mode == "GROU") else 1
            self.busy_until = self.now + self.sweep_time() * n

    @command("SENSe#:SWEep:GROups:COUNt")
    def _gcount(self, q, a, ch):
        return "1" if q else None

    @command("SENSe#:BANDwidth[:RESolution]", "SENSe#:BWIDth[:RESolution]")
    def _ifbw(self, q, a, ch):
        if q:
            return "%+.12E" % self.ifbw
        self.ifbw = a.num(0, 1.0, 15e6, minmax=(1.0, 15e6, 100e3))

    @command("SENSe#:AVERage[:STATe]")
    def _avg(self, q, a, ch):
        if q:
            return onoff(self.avg_on)
        self.avg_on = a.bool(0)

    @command("SENSe#:AVERage:COUNt")
    def _avgc(self, q, a, ch):
        if q:
            return "%d" % self.avg_count
        self.avg_count = a.int(0, 1, 65536)

    @command("SENSe#:AVERage:CLEar", "SENSe#:AVERage:MODE", "SENSe#:CORRection[:STATe]", "DISPlay:ENABle",
             "DISPlay:WINDow#[:STATe]", "DISPlay:WINDow#:TRACe#:FEED", "DISPlay:WINDow#:TRACe#:Y:SCALe:AUTO",
             "DISPlay:MEASure#:FEED", "SOURce#:POWer#:MODE", "SOURce#:POWer:COUPle", "SENSe#:SWEep:TIME:AUTO",
             "SYSTem:CHANnels:CATalog", "SENSe#:SWEep:GENeration", "SOURce#:POWer#:ALC[:MODE]")
    def _misc(self, q, a, ch):
        return "0" if q else None

    @command("SOURce#:POWer#[:LEVel][:IMMediate][:AMPLitude]")
    def _power(self, q, a, ch):
        if q:
            return "%+.12E" % self.power
        self.power = a.num(0, -95.0, 20.0, minmax=(-95.0, 20.0, -10.0))

    @command("SOURce#:POWer#:STARt")
    def _pstart(self, q, a, ch):
        if q:
            return "%+.12E" % self.pstart
        self.pstart = a.num(0, -95.0, 20.0)

    @command("SOURce#:POWer#:STOP")
    def _pstop(self, q, a, ch):
        if q:
            return "%+.12E" % self.pstop
        self.pstop = a.num(0, -95.0, 20.0)

    @command("OUTPut[:STATe]")
    def _outp(self, q, a, ch):
        if q:
            return onoff(self.rf_on)
        self.rf_on = a.bool(0)

    @command("FORMat[:DATA]")
    def _form(self, q, a, ch):
        if q:
            return {"ASC": "ASC,+0", "REAL64": "REAL,+64", "REAL32": "REAL,+32"}[self.data_format]
        kind = a.word(0, ["ASCii", "REAL"])
        if kind == "ASCII":
            self.data_format = "ASC"
        else:
            bits = a.int(1, 32, 64, default=64)
            if bits not in (32, 64):
                raise fail(ILLEGAL)
            self.data_format = "REAL%d" % bits

    @command("FORMat:BORDer")
    def _bord(self, q, a, ch):
        if q:
            return self.byte_order
        self.byte_order = a.word(0, ["NORMal", "SWAPped"])[:4]

    @command("INITiate#[:IMMediate]", query=False)
    def _init(self, q, a, ch):
        n = self.avg_count if self.avg_on else 1
        self.busy_until = self.now + self.sweep_time() * n

    @command("INITiate#:CONTinuous")
    def _cont(self, q, a, ch):
        if q:
            return onoff(self.continuous)
        self.continuous = a.bool(0)

    @command("TRIGger[:SEQuence]:SOURce")
    def _trig(self, q, a, ch):
        if q:
            return self.trig
        self.trig = a.word(0, ["IMMediate", "EXTernal", "MANual"])[:3]

    @command("ABORt", query=False)
    def _abort(self, q, a, ch):
        self.busy_until = self.now

    @command("SYSTem:PRESet", "SYSTem:FPReset", query=False)
    def _preset(self, q, a, ch):
        self.reset()


class KeysightN5183B(Wired, Instrument):
    model = "keysight_n5183b"
    idn = "Keysight Technologies, N5183B, {serial}, B.01.86"
    default_serial = "MY53050221"

    def reset(self) -> None:
        self.freq, self.power, self.on, self.mod = 1e9, -144.0, False, False
        self.pulm, self.pulm_src, self.pulm_pol = False, "INT", "NORM"
        self._drive()

    def _drive(self) -> None:
        self.put("rf", Source("RF", dc=self.power, ac={self.freq: 1.0}) if self.on else None)
        term, _ = self.terminal("rf")
        if term == "mw":
            self.world.optics.mw = (self.freq, self.power) if self.on else None

    def status(self):
        return {"freq_Hz": self.freq, "power_dBm": self.power, "output": self.on}

    @command("[SOURce:]FREQuency[:CW]", "[SOURce:]FREQuency:FIXed")
    def _freq(self, q, a, ch):
        if q:
            return "%+.10E" % self.freq
        self.freq = a.num(0, 9e3, 20e9)
        self._drive()

    @command("[SOURce:]POWer[:LEVel][:IMMediate][:AMPLitude]")
    def _pow(self, q, a, ch):
        if q:
            return "%+.2f" % self.power
        self.power = a.num(0, -144.0, 30.0)
        self._drive()

    @command("OUTPut[:STATe]")
    def _out(self, q, a, ch):
        if q:
            return onoff(self.on)
        self.on = a.bool(0)
        self._drive()

    @command("OUTPut:MODulation[:STATe]")
    def _mod(self, q, a, ch):
        if q:
            return onoff(self.mod)
        self.mod = a.bool(0)

    @command("[SOURce:]PULM:STATe")
    def _pulm(self, q, a, ch):
        if q:
            return onoff(self.pulm)
        self.pulm = a.bool(0)

    @command("[SOURce:]PULM:SOURce")
    def _pulm_src(self, q, a, ch):
        if q:
            return self.pulm_src
        self.pulm_src = a.word(0, ["INTernal", "EXTernal", "SCALar"])[:3]

    @command("[SOURce:]PULM:EXTernal:POLarity")
    def _pulm_pol(self, q, a, ch):
        if q:
            return self.pulm_pol
        self.pulm_pol = a.word(0, ["NORMal", "INVerted"])[:4].replace("INVE", "INV")

    @command("[SOURce:]FREQuency:MODE", "[SOURce:]POWer:MODE", "[SOURce:]POWer:ALC[:STATe]", "DISPlay:REMote")
    def _misc(self, q, a, ch):
        return "CW" if q else None
