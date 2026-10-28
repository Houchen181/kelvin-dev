"""Keithley source-meters, the 6221 AC/DC current source and the 2182A nanovoltmeter (SCPI, as in their manuals)."""
from __future__ import annotations

import math
import time
from typing import List, Optional, Tuple

from ..instrument import CONFLICT, CommandError, Instrument, OUT_OF_RANGE, command, fail, fmt_e, onoff
from ..world import Source
from .common import Wired, limit_source


def _load(world, term: Optional[str], series: float):
    """Current a voltage v drives into what the port is wired to."""
    if term == "gate":
        return world.sample.leak_current
    if term in ("bias",):
        return lambda v: v / (series + world.r2())
    if term == "flux":
        return lambda v: v / (series or 1000.0)
    return lambda v: v / 1e12


def _solve_v(load, i: float, vmax: float) -> float:
    """The voltage at which the load draws current i (the load rises with v)."""
    lo, hi = -vmax, vmax
    if load(hi) < i:
        return hi * 10
    if load(lo) > i:
        return lo * 10
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if load(mid) < i:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


class Keithley2400(Wired, Instrument):
    model = "keithley_2400"
    idn = "KEITHLEY INSTRUMENTS INC.,MODEL 2400,{serial},C30   Mar 17 2006 09:29:29/A02  /K/J"
    default_serial = "1287421"
    error_format = '{code:d},"{text}"'
    terminators = (b"\n", b"\r")
    VMAX, IMAX = 210.0, 1.05
    V_RANGES = (0.21, 2.1, 21.0, 210.0)
    I_RANGES = (1.05e-6, 1.05e-5, 1.05e-4, 1.05e-3, 1.05e-2, 0.105, 1.05)
    STATUS = 21504

    def reset(self) -> None:
        self.func, self.vlev, self.ilev = "VOLT", 0.0, 0.0
        self.vrange, self.vauto, self.irange_src, self.iauto_src = 21.0, True, 1.05e-4, True
        self.icomp, self.vcomp = 105e-6, 21.0
        self.irange, self.iauto = 1.05e-4, True
        self.nplc = 1.0
        self.output = False
        self.elements = ["VOLT", "CURR", "RES", "TIME", "STAT"]
        self.terminals = "FRON"
        self.auto_off = False
        self.count = 1
        self.compl = False
        self.t_on = time.monotonic()
        self.applied = (0.0, 0.0)
        self._apply()

    def status(self):
        return {"output": self.output, "source": self.func, "level": self.vlev if self.func == "VOLT" else self.ilev,
                "applied_V_I": self.applied, "compliance": self.compl}

    # -- the output
    def _wired(self) -> bool:
        return self.terminals == ("REAR" if str(self.opt("wired_to", "front")).lower() == "rear" else "FRON")

    def _solve(self) -> Tuple[float, float, bool]:
        term, series = self.terminal("out")
        load = _load(self.world, term if self._wired() else None, series)
        if self.func == "VOLT":
            return limit_source(load, self.vlev, self.icomp)
        v = _solve_v(load, self.ilev, self.VMAX)
        if abs(v) > self.vcomp:
            v = math.copysign(self.vcomp, v)
            return v, load(v), True
        return v, self.ilev, False

    def _apply(self) -> None:
        if not self.output:
            self.put("out", None)
            self.compl = False
            self.applied = (0.0, 0.0)
            return
        v, i, self.compl = self._solve()
        self.applied = (v, i)
        if not self._wired():
            self.put("out", None)
        elif self.func == "VOLT" or self.compl:
            self.put("out", Source("V", dc=v))
        else:
            self.put("out", Source("I", dc=i))

    def tick(self, dt: float, now: float) -> None:
        if self.output:
            self._apply()

    def _reading(self) -> List[float]:
        v, i = self.applied if self.output else (0.0, 0.0)
        v += self.noise(2e-6 + 1e-5 * abs(v))
        i += self.noise(3e-12 + 2e-5 * abs(i))
        r = v / i if abs(i) > 1e-15 else 9.91e37
        stat = self.STATUS + (8 if self.compl else 0)
        values = {"VOLT": v, "CURR": i, "RES": r, "TIME": time.monotonic() - self.t_on, "STAT": float(stat)}
        out = []
        for _ in range(self.count):
            out += [values[e] for e in self.elements]
        return out

    def _read(self, measure_on_demand: bool = False) -> str:
        if not self.output and not (self.auto_off or measure_on_demand):
            raise CommandError(803, "Output disabled")
        if not self.output:   # auto output-off: the output comes on for the measurement
            self.output = True
            self._apply()
            values = self._reading()
            self.output = False
            self._apply()
        else:
            values = self._reading()
        self.delay(self.count * (self.nplc / 60.0 * 1.4 + 0.004))
        return ",".join(fmt_e(x) for x in values)

    # -- commands
    @command("OUTPut[:STATe]")
    def _outp(self, q, a, ch):
        if q:
            return onoff(self.output)
        self.output = a.bool(0)
        self._apply()

    @command("SOURce:FUNCtion[:MODE]")
    def _func(self, q, a, ch):
        if q:
            return self.func
        f = a.word(0, ["VOLTage", "CURRent", "MEMory"])
        self.func = {"VOLTAGE": "VOLT", "CURRENT": "CURR"}.get(f, "VOLT")
        self._apply()

    @command("SOURce:VOLTage[:LEVel][:IMMediate][:AMPLitude]")
    def _vlev(self, q, a, ch):
        if q:
            return fmt_e(self.vlev)
        v = a.num(0, minmax=(-self.VMAX, self.VMAX, 0.0))
        if abs(v) > self.VMAX:
            raise fail(OUT_OF_RANGE)
        if not self.vauto and abs(v) > self.vrange:
            raise fail(OUT_OF_RANGE, "level above the fixed source range")
        self.vlev = v
        self._apply()

    @command("SOURce:CURRent[:LEVel][:IMMediate][:AMPLitude]")
    def _ilev(self, q, a, ch):
        if q:
            return fmt_e(self.ilev)
        v = a.num(0, minmax=(-self.IMAX, self.IMAX, 0.0))
        if abs(v) > self.IMAX:
            raise fail(OUT_OF_RANGE)
        self.ilev = v
        self._apply()

    @command("SOURce:VOLTage:RANGe")
    def _vrange(self, q, a, ch):
        if q:
            return fmt_e(self.vrange)
        v = abs(a.num(0, minmax=(0.21, self.VMAX, 21.0)))
        self.vrange = min([r for r in self.V_RANGES if r >= v * 0.999] or [self.V_RANGES[-1]])
        self.vauto = False

    @command("SOURce:VOLTage:RANGe:AUTO")
    def _vauto(self, q, a, ch):
        if q:
            return onoff(self.vauto)
        self.vauto = a.bool(0)

    @command("SOURce:CURRent:RANGe")
    def _irange_src(self, q, a, ch):
        if q:
            return fmt_e(self.irange_src)
        self.irange_src = abs(a.num(0, minmax=(1e-6, self.IMAX, 1e-4)))

    @command("SOURce:CURRent:RANGe:AUTO")
    def _iauto_src(self, q, a, ch):
        if q:
            return onoff(self.iauto_src)
        self.iauto_src = a.bool(0)

    @command("SOURce:VOLTage:MODE", "SOURce:CURRent:MODE")
    def _mode(self, q, a, ch):
        if q:
            return "FIX"
        a.word(0, ["FIXed", "SWEep", "LIST"])

    @command("SOURce:DELay", "SOURce:DELay:AUTO")
    def _sdelay(self, q, a, ch):
        return "0" if q else None

    @command("SOURce:CLEar[:IMMediate]", query=False)
    def _clear(self, q, a, ch):
        self.output = False
        self._apply()

    @command("SOURce:CLEar:AUTO")
    def _clear_auto(self, q, a, ch):
        if q:
            return onoff(self.auto_off)
        self.auto_off = a.bool(0)

    @command("SENSe:CURRent[:DC]:PROTection[:LEVel]")
    def _icomp(self, q, a, ch):
        if q:
            return fmt_e(self.icomp)
        self.icomp = abs(a.num(0, minmax=(1e-9, self.IMAX, 105e-6)))
        if self.icomp > self.IMAX:
            raise fail(OUT_OF_RANGE)
        self._apply()

    @command("SENSe:VOLTage[:DC]:PROTection[:LEVel]")
    def _vcomp(self, q, a, ch):
        if q:
            return fmt_e(self.vcomp)
        self.vcomp = abs(a.num(0, minmax=(0.2, self.VMAX, 21.0)))
        self._apply()

    @command("SENSe:CURRent[:DC]:PROTection:TRIPped", "SENSe:VOLTage[:DC]:PROTection:TRIPped", setter=False)
    def _tripped(self, q, a, ch):
        return onoff(self.compl)

    @command("SENSe:CURRent[:DC]:RANGe[:UPPer]")
    def _irange(self, q, a, ch):
        if q:
            return fmt_e(self.irange)
        self.irange = abs(a.num(0, minmax=(1e-6, self.IMAX, 1e-4)))
        self.iauto = False

    @command("SENSe:CURRent[:DC]:RANGe:AUTO", "SENSe:VOLTage[:DC]:RANGe:AUTO")
    def _iauto(self, q, a, ch):
        if q:
            return onoff(self.iauto)
        self.iauto = a.bool(0)

    @command("SENSe:VOLTage[:DC]:RANGe[:UPPer]")
    def _vsense_range(self, q, a, ch):
        return fmt_e(21.0) if q else None

    @command("SENSe:CURRent[:DC]:NPLCycles", "SENSe:VOLTage[:DC]:NPLCycles", "SENSe:RESistance:NPLCycles")
    def _nplc(self, q, a, ch):
        if q:
            return fmt_e(self.nplc, 2)
        self.nplc = a.num(0, 0.01, 10.0, minmax=(0.01, 10.0, 1.0))

    @command("SENSe:FUNCtion[:ON]", "SENSe:FUNCtion:OFF", "SENSe:FUNCtion:CONCurrent", "SENSe:RESistance:MODE",
             "SENSe:AVERage[:STATe]", "SENSe:AVERage:COUNt", "SENSe:AVERage:TCONtrol")
    def _sense_misc(self, q, a, ch):
        if q:
            return '"VOLT:DC","CURR:DC"'
        a.need(1)

    @command("FORMat:ELEMents[:SENSe#]")
    def _elements(self, q, a, ch):
        if q:
            return ",".join(self.elements)
        a.need(1)
        self.elements = [a.word(i, ["VOLTage", "CURRent", "RESistance", "TIME", "STATus"])[:4] for i in range(len(a))]

    @command("FORMat[:DATA]")
    def _format(self, q, a, ch):
        return "ASC" if q else None

    @command("ROUTe:TERMinals")
    def _term(self, q, a, ch):
        if q:
            return self.terminals
        self.terminals = a.word(0, ["FRONt", "REAR"])[:4]
        self._apply()

    @command("SYSTem:BEEPer[:STATe]", "SYSTem:AZERo[:STATe]", "SYSTem:RSENse", "DISPlay:ENABle", "SYSTem:LOCal",
             "SYSTem:REMote", "SYSTem:RWLock", "SYSTem:PRESet", "TRIGger:DELay", "TRIGger:SOURce", "ARM:SOURce",
             "ARM:COUNt", "SYSTem:KEY", "SYSTem:GUARd", "SYSTem:TIME:RESet")
    def _misc(self, q, a, ch):
        return "0" if q else None

    @command("TRIGger:COUNt")
    def _count(self, q, a, ch):
        if q:
            return str(self.count)
        self.count = a.int(0, 1, 2500)

    @command("READ", setter=False)
    def _read_q(self, q, a, ch):
        return self._read()

    @command("FETCh", setter=False)
    def _fetch(self, q, a, ch):
        return ",".join(fmt_e(x) for x in self._reading())

    @command("INITiate[:IMMediate]", "ABORt", query=False)
    def _init(self, q, a, ch):
        if not self.output and not self.auto_off:
            raise CommandError(803, "Output disabled")
        self.delay(self.nplc / 60.0)

    @command("MEASure", "MEASure:CURRent[:DC]", "MEASure:VOLTage[:DC]", "MEASure:RESistance", setter=False)
    def _meas(self, q, a, ch):
        return self._read(measure_on_demand=True)

    @command("CONFigure:CURRent[:DC]", "CONFigure:VOLTage[:DC]", "CONFigure:RESistance", query=False)
    def _conf(self, q, a, ch):
        self.auto_off = True


class Keithley2450(Keithley2400):
    model = "keithley_2450"
    idn = "KEITHLEY INSTRUMENTS,MODEL 2450,{serial},1.7.3b"
    default_serial = "04412345"
    error_format = '{code:d},"{text}"'

    _icomp = _vcomp = _tripped = _elements = _count = _clear_auto = _fetch = None   # 2400 commands a 2450 lacks

    def reset(self) -> None:
        super().reset()
        self.sense_func = "CURR"
        self.readback = True

    def _read(self, measure_on_demand: bool = False) -> str:
        """The 2450 measures with its output off too (it reads what is on its terminals)."""
        v, i = self.applied if self.output else (0.0, 0.0)
        val = {"CURR": i + self.noise(5e-12 + 2e-5 * abs(i)), "VOLT": v + self.noise(3e-6 + 1e-5 * abs(v)),
               "RES": (v / i if abs(i) > 1e-15 else 9.9e37)}[self.sense_func]
        self.delay(self.nplc / 60.0 * 1.3 + 0.006)
        return "%.6E" % val

    @command("SOURce:VOLTage:ILIMit[:LEVel]")
    def _ilim(self, q, a, ch):
        if q:
            return "%.6E" % self.icomp
        self.icomp = abs(a.num(0, 1e-9, 1.05, minmax=(1e-9, 1.05, 105e-6)))
        self._apply()

    @command("SOURce:CURRent:VLIMit[:LEVel]")
    def _vlim(self, q, a, ch):
        if q:
            return "%.6E" % self.vcomp
        self.vcomp = abs(a.num(0, 0.02, 210.0, minmax=(0.02, 210.0, 21.0)))
        self._apply()

    @command("SOURce:VOLTage:ILIMit[:LEVel]:TRIPped", "SOURce:CURRent:VLIMit[:LEVel]:TRIPped", setter=False)
    def _trip(self, q, a, ch):
        return onoff(self.compl)

    @command("SOURce:VOLTage:READ:BACK", "SOURce:CURRent:READ:BACK")
    def _readback(self, q, a, ch):
        if q:
            return onoff(self.readback)
        self.readback = a.bool(0)

    @command("SENSe:FUNCtion[:ON]")
    def _sense_func(self, q, a, ch):
        if q:
            return '"%s:DC"' % self.sense_func
        self.sense_func = a.word(0, ["CURRent", "VOLTage", "RESistance", "CURRent:DC", "VOLTage:DC"])[:4]

    @command("READ", "MEASure", "MEASure:CURRent[:DC]", "MEASure:VOLTage[:DC]", "MEASure:RESistance", setter=False)
    def _read2450(self, q, a, ch):
        return self._read()

    @command("COUNt", "TRACe:CLEar", "TRACe:POINts", "TRACe:MAKE", "SENSe:CURRent[:DC]:RANGe:AUTO:LLIMit",
             "SENSe:CURRent[:DC]:RSENse", "SENSe:VOLTage[:DC]:RSENse", "SOURce:VOLTage:DELay",
             "SOURce:VOLTage:DELay:AUTO", "OUTPut[:SMU]:SMODe", "SOURce:VOLTage:PROTection[:LEVel]",
             "SENSe:CURRent[:DC]:AZERo[:STATe]", "SENSe:CURRent[:DC]:UNIT", "DISPlay:SCReen")
    def _misc2450(self, q, a, ch):
        return "1" if q else None


class Keithley6221(Wired, Instrument):
    """DC and AC current source. A running sine (SOUR:WAVE) drives the sample at its frequency; with the phase
    marker on, its trigger link carries the reference a lock-in can lock to."""
    model = "keithley_6221"
    idn = "KEITHLEY INSTRUMENTS INC.,MODEL 6221,{serial},D04  /700x "
    default_serial = "4123960"
    error_format = '{code:d},"{text}"'
    terminators = (b"\n", b"\r")

    def reset(self) -> None:
        self.level, self.comp, self.rng_i, self.auto = 0.0, 10.0, 2e-3, True
        self.output = False
        self.wave = dict(func="SIN", ampl=2e-12, freq=1e3, offs=0.0, pmar=False, pmar_deg=0.0, olin=2, dur=9.9e37,
                         armed=False, running=False, rng="BEST")
        self.compl = False
        self._apply()

    def status(self):
        return {"output": self.output, "level_A": self.level, "wave": dict(self.wave), "compliance": self.compl}

    def _apply(self) -> None:
        if not self.output:
            self.put("out", None)
            self.put("ref", None)
            self.compl = False
            return
        w = self.wave
        r2 = self.world.r2() if self.terminal("out")[0] == "bias" else 1e12
        if w["running"]:
            peak = w["ampl"] + abs(w["offs"])
            scale = min(1.0, self.comp / max(peak * r2, 1e-30))
            self.compl = scale < 1.0
            self.put("out", Source("I", dc=w["offs"] * scale, ac={w["freq"]: w["ampl"] * scale / math.sqrt(2)}))
            self.put("ref", Source("V", ac={w["freq"]: 1.0}) if w["pmar"] else None)
        else:
            i = self.level
            if abs(i) * r2 > self.comp:
                i = math.copysign(self.comp / r2, i)
                self.compl = True
            else:
                self.compl = False
            self.put("out", Source("I", dc=i))
            self.put("ref", None)

    def tick(self, dt: float, now: float) -> None:
        if self.output:
            self._apply()

    @command("OUTPut[:STATe]")
    def _outp(self, q, a, ch):
        if q:
            return onoff(self.output)
        self.output = a.bool(0)
        if not self.output:
            self.wave["running"] = False
        self._apply()

    @command("SOURce:CURRent[:LEVel][:IMMediate][:AMPLitude]")
    def _lev(self, q, a, ch):
        if q:
            return fmt_e(self.level)
        self.level = a.num(0, -0.105, 0.105, minmax=(-0.105, 0.105, 0.0))
        self._apply()

    @command("SOURce:CURRent:COMPliance")
    def _comp(self, q, a, ch):
        if q:
            return fmt_e(self.comp)
        self.comp = a.num(0, 0.1, 105.0, minmax=(0.1, 105.0, 10.0))
        self._apply()

    @command("SOURce:CURRent:RANGe")
    def _range(self, q, a, ch):
        if q:
            return fmt_e(self.rng_i)
        self.rng_i = abs(a.num(0, 0, 0.105, minmax=(2e-9, 0.105, 2e-3)))
        self.auto = False

    @command("SOURce:CURRent:RANGe:AUTO")
    def _auto(self, q, a, ch):
        if q:
            return onoff(self.auto)
        self.auto = a.bool(0)

    @command("SOURce:CLEar[:IMMediate]", query=False)
    def _clear(self, q, a, ch):
        self.output = False
        self.wave["running"] = False
        self._apply()

    @command("SOURce:WAVE:FUNCtion")
    def _wfunc(self, q, a, ch):
        if q:
            return self.wave["func"]
        self.wave["func"] = a.word(0, ["SINusoid", "SQUare", "RAMP", "ARBitrary0", "ARB0"])[:3]

    @command("SOURce:WAVE:AMPLitude")
    def _wampl(self, q, a, ch):
        if q:
            return fmt_e(self.wave["ampl"])
        self.wave["ampl"] = a.num(0, 2e-12, 0.105, minmax=(2e-12, 0.105, 2e-12))
        self._apply()

    @command("SOURce:WAVE:FREQuency")
    def _wfreq(self, q, a, ch):
        if q:
            return fmt_e(self.wave["freq"])
        self.wave["freq"] = a.num(0, 1e-3, 1e5, minmax=(1e-3, 1e5, 1e3))
        self._apply()

    @command("SOURce:WAVE:OFFSet")
    def _woffs(self, q, a, ch):
        if q:
            return fmt_e(self.wave["offs"])
        self.wave["offs"] = a.num(0, -0.105, 0.105, minmax=(-0.105, 0.105, 0.0))
        self._apply()

    @command("SOURce:WAVE:PMARker:STATe")
    def _pmar_state(self, q, a, ch):
        if q:
            return onoff(self.wave["pmar"])
        self.wave["pmar"] = a.bool(0)
        self._apply()

    @command("SOURce:WAVE:PMARker[:LEVel]")
    def _pmar(self, q, a, ch):
        if q:
            return fmt_e(self.wave["pmar_deg"])
        self.wave["pmar_deg"] = a.num(0, 0, 360)

    @command("SOURce:WAVE:PMARker:OLINe")
    def _olin(self, q, a, ch):
        if q:
            return str(self.wave["olin"])
        self.wave["olin"] = a.int(0, 1, 6)

    @command("SOURce:WAVE:DURation:TIME", "SOURce:WAVE:DURation:CYCLes")
    def _dur(self, q, a, ch):
        if q:
            return fmt_e(self.wave["dur"])
        self.wave["dur"] = a.num(0, minmax=(1e-3, 9.9e37, 9.9e37))

    @command("SOURce:WAVE:RANGing")
    def _wrange(self, q, a, ch):
        if q:
            return self.wave["rng"]
        self.wave["rng"] = a.word(0, ["BEST", "FIXed"])[:4]

    @command("SOURce:WAVE:ARM", query=False)
    def _arm(self, q, a, ch):
        self.wave["armed"] = True

    @command("SOURce:WAVE:ARM", setter=False)
    def _armq(self, q, a, ch):
        return onoff(self.wave["armed"])

    @command("SOURce:WAVE:INITiate", query=False)
    def _winit(self, q, a, ch):
        if not self.wave["armed"]:
            raise fail(CONFLICT, "waveform not armed")
        self.wave["running"] = True
        self.output = True
        self._apply()

    @command("SOURce:WAVE:ABORt", query=False)
    def _wabort(self, q, a, ch):
        self.wave["running"] = False
        self.wave["armed"] = False
        self.output = False
        self._apply()

    @command("OUTPut:LTEarth", "OUTPut:ISHield", "OUTPut:RESPonse", "SOURce:DELay", "SOURce:SWEep:SPACing",
             "SYSTem:BEEPer[:STATe]", "SOURce:CURRent:FILTer[:STATe]", "DISPlay:ENABle", "TRIGger:SOURce",
             "SOURce:WAVE:EXTRigger[:ENABle]")
    def _misc(self, q, a, ch):
        return "0" if q else None


class Keithley2182A(Wired, Instrument):
    model = "keithley_2182a"
    idn = "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,{serial},C02  /A02  "
    default_serial = "1179311"
    error_format = '{code:d},"{text}"'
    terminators = (b"\n", b"\r")
    RANGES = (0.01, 0.1, 1.0, 10.0, 100.0)

    def reset(self) -> None:
        self.func, self.chan, self.nplc, self.rng, self.auto = "VOLT", 1, 5.0, 100.0, True
        self.azero, self.filt, self.fcount = True, True, 10
        self.last = 0.0

    def _value(self) -> float:
        port = "in" if self.chan == 1 else "in2"
        v = self.dc_on(port) + self.noise(1.5e-9 * math.sqrt(5.0 / self.nplc) / (math.sqrt(self.fcount) if self.filt else 1))
        rng = max(self.RANGES) if self.auto else self.rng
        if self.chan == 2:
            rng = min(rng, 10.0)
        if abs(v) > rng * 1.2:
            return 9.9e37
        return v

    def _read(self) -> str:
        self.delay(self.nplc / 60.0 * (2.0 if self.azero else 1.0) + 0.015)
        self.last = self._value()
        return fmt_e(self.last, 8)

    @command("READ", "MEASure[:VOLTage][:DC]", setter=False)
    def _read_q(self, q, a, ch):
        return self._read()

    @command("FETCh", setter=False)
    def _fetch(self, q, a, ch):
        return fmt_e(self.last, 8)

    @command("SENSe:FUNCtion")
    def _func(self, q, a, ch):
        if q:
            return '"%s"' % ("VOLT" if self.func == "VOLT" else "TEMP")
        self.func = a.word(0, ["VOLTage", "VOLTage:DC", "TEMPerature"])[:4]

    @command("SENSe:CHANnel")
    def _chan(self, q, a, ch):
        if q:
            return str(self.chan)
        self.chan = a.int(0, 0, 2)

    @command("SENSe:VOLTage[:DC]:NPLCycles", "SENSe:VOLTage[:DC]:CHANnel#:NPLCycles")
    def _nplc(self, q, a, ch):
        if q:
            return fmt_e(self.nplc, 2)
        self.nplc = a.num(0, 0.01, 60.0, minmax=(0.01, 60.0, 5.0))

    @command("SENSe:VOLTage[:DC]:CHANnel#:RANGe[:UPPer]", "SENSe:VOLTage[:DC]:RANGe[:UPPer]")
    def _range(self, q, a, ch):
        if q:
            return fmt_e(self.rng, 2)
        v = abs(a.num(0, 0, 120.0, minmax=(0.01, 100.0, 100.0)))
        self.rng = min([r for r in self.RANGES if r >= v] or [100.0])
        self.auto = False

    @command("SENSe:VOLTage[:DC]:CHANnel#:RANGe:AUTO", "SENSe:VOLTage[:DC]:RANGe:AUTO")
    def _auto(self, q, a, ch):
        if q:
            return onoff(self.auto)
        self.auto = a.bool(0)

    @command("SENSe:VOLTage[:DC]:DFILter[:STATe]", "SENSe:VOLTage[:DC]:CHANnel#:DFILter[:STATe]")
    def _filt(self, q, a, ch):
        if q:
            return onoff(self.filt)
        self.filt = a.bool(0)

    @command("SENSe:VOLTage[:DC]:DFILter:COUNt", "SENSe:VOLTage[:DC]:CHANnel#:DFILter:COUNt")
    def _fcount(self, q, a, ch):
        if q:
            return str(self.fcount)
        self.fcount = a.int(0, 1, 100)

    @command("SYSTem:AZERo[:STATe]")
    def _azero(self, q, a, ch):
        if q:
            return onoff(self.azero)
        self.azero = a.bool(0)

    @command("INITiate[:IMMediate]", "INITiate:CONTinuous", "ABORt", "TRIGger:SOURce", "TRIGger:COUNt",
             "TRIGger:DELay", "SAMPle:COUNt", "SENSe:VOLTage[:DC]:LPASs[:STATe]", "SYSTem:FAZero[:STATe]",
             "SYSTem:LSYNc[:STATe]", "DISPlay:ENABle", "SYSTem:BEEPer[:STATe]", "SENSe:VOLTage[:DC]:DIGits",
             "SENSe:VOLTage[:DC]:DFILter:TCONtrol", "SENSe:VOLTage[:DC]:DFILter:WINDow")
    def _misc(self, q, a, ch):
        return "0" if q else None
