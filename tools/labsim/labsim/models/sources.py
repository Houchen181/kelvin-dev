"""DC and RF sources: Yokogawa GS200, Rohde & Schwarz SMB100A, Stanford Research SG384."""
from __future__ import annotations

import math

from ..instrument import ILLEGAL, Instrument, OUT_OF_RANGE, command, fail, fmt_e, onoff
from ..world import Source
from .common import Wired


class YokogawaGS200(Wired, Instrument):
    model = "yokogawa_gs200"
    idn = "YOKOGAWA,GS210,{serial},2.02"
    default_serial = "91T810992"
    error_format = '{code:d},"{text}"'
    V_RANGES = (10e-3, 100e-3, 1.0, 10.0, 30.0)
    I_RANGES = (1e-3, 10e-3, 100e-3, 200e-3)

    def reset(self) -> None:
        self.func, self.rng, self.level, self.on = "VOLT", 10.0, 0.0, False
        self.vprot, self.iprot = 30.0, 0.2
        self.limited = False
        self._apply()

    def status(self):
        return {"func": self.func, "level": self.level, "range": self.rng, "output": self.on, "limited": self.limited}

    def _apply(self) -> None:
        if not self.on:
            self.put("out", None)
            return
        term, series = self.terminal("out")
        if self.func == "CURR":
            self.put("out", Source("I", dc=self.level))
        else:
            r = series or (1000.0 if term == "flux" else 1e12)
            i = self.level / r
            self.limited = abs(i) > self.iprot
            v = math.copysign(self.iprot * r, self.level) if self.limited else self.level
            self.put("out", Source("V", dc=v))

    def _ranges(self):
        return self.V_RANGES if self.func == "VOLT" else self.I_RANGES

    @command("[SOURce:]FUNCtion")
    def _func(self, q, a, ch):
        if q:
            return self.func
        f = a.word(0, ["VOLTage", "CURRent"])[:4]
        if f != self.func:
            self.func, self.level = f, 0.0
            self.rng = 10.0 if f == "VOLT" else 100e-3
            self._apply()

    @command("[SOURce:]RANGe")
    def _range(self, q, a, ch):
        if q:
            return fmt_e(self.rng, 1)
        v = abs(a.num(0, minmax=(min(self._ranges()), max(self._ranges()), max(self._ranges()))))
        fits = [r for r in self._ranges() if r >= v * 0.999]
        if not fits:
            raise fail(OUT_OF_RANGE)
        self.rng = fits[0]
        if abs(self.level) > self.rng * 1.2:
            self.level = 0.0
        self._apply()

    @command("[SOURce:]LEVel[:FIX]")
    def _level(self, q, a, ch):
        if q:
            return fmt_e(self.level, 5)
        v = a.num(0, minmax=(-1.2 * self.rng, 1.2 * self.rng, 0.0))
        if abs(v) > self.rng * 1.2:
            raise fail(OUT_OF_RANGE, "level beyond the range")
        self.level = v
        self._apply()

    @command("[SOURce:]LEVel:AUTO", query=False)
    def _level_auto(self, q, a, ch):
        v = a.num(0)
        fits = [r for r in self._ranges() if r * 1.2 >= abs(v)]
        if not fits:
            raise fail(OUT_OF_RANGE)
        self.rng, self.level = fits[0], v
        self._apply()

    @command("[SOURce:]PROTection:VOLTage")
    def _vprot(self, q, a, ch):
        if q:
            return fmt_e(self.vprot, 1)
        self.vprot = a.num(0, 1.0, 30.0)

    @command("[SOURce:]PROTection:CURRent")
    def _iprot(self, q, a, ch):
        if q:
            return fmt_e(self.iprot, 1)
        self.iprot = a.num(0, 1e-3, 0.2)
        self._apply()

    @command("OUTPut[:STATe]")
    def _out(self, q, a, ch):
        if q:
            return onoff(self.on)
        self.on = a.bool(0)
        self._apply()

    @command("SYSTem:DISPlay", "SYSTem:BEEPer[:STATe]", "SENSe[:STATe]", "[SOURce:]DELay", "SYSTem:LOCal",
             "SYSTem:REMote", "OUTPut:PROGram")
    def _misc(self, q, a, ch):
        return "0" if q else None


class RsSMB100A(Wired, Instrument):
    model = "rs_smb100a"
    idn = "Rohde&Schwarz,SMB100A,1406.6000k03/{serial},3.1.18.2-3.01.203.09"
    default_serial = "180921"
    error_format = '{code:d},"{text}"'
    FMAX = 20e9

    def reset(self) -> None:
        self.freq, self.power, self.on = 1e9, -30.0, False
        self.pulm, self.pulm_src, self.pulm_pol = False, "INT", "NORM"
        self._drive()

    def _drive(self) -> None:
        self.put("rf", Source("RF", dc=self.power, ac={self.freq: 1.0}) if self.on else None)
        if self.terminal("rf")[0] == "mw":
            self.world.optics.mw = (self.freq, self.power) if self.on else None

    def status(self):
        return {"freq_Hz": self.freq, "power_dBm": self.power, "output": self.on}

    @command("[SOURce:]FREQuency[:CW]", "[SOURce:]FREQuency:FIXed")
    def _freq(self, q, a, ch):
        if q:
            return "%.10g" % self.freq
        self.freq = a.num(0, 100e3, self.FMAX)
        self._drive()

    @command("[SOURce:]POWer[:LEVel][:IMMediate][:AMPLitude]")
    def _pow(self, q, a, ch):
        if q:
            return "%.2f" % self.power
        self.power = a.num(0, -145.0, 30.0)
        self._drive()

    @command("OUTPut[:STATe]")
    def _out(self, q, a, ch):
        if q:
            return onoff(self.on)
        self.on = a.bool(0)
        self._drive()

    @command("[SOURce:]PULM:STATe")
    def _pulm(self, q, a, ch):
        if q:
            return onoff(self.pulm)
        self.pulm = a.bool(0)

    @command("[SOURce:]PULM:SOURce")
    def _pulm_src(self, q, a, ch):
        if q:
            return self.pulm_src
        self.pulm_src = a.word(0, ["INTernal", "EXTernal"])[:3]

    @command("[SOURce:]PULM:POLarity")
    def _pulm_pol(self, q, a, ch):
        if q:
            return self.pulm_pol
        self.pulm_pol = a.word(0, ["NORMal", "INVerted"])[:4].replace("INVE", "INV")

    @command("[SOURce:]FREQuency:MODE", "[SOURce:]POWer:MODE", "[SOURce:]MODulation[:ALL][:STATe]",
             "SYSTem:DISPlay:UPDate")
    def _misc(self, q, a, ch):
        return "CW" if q else None


class SrsSG384(Wired, Instrument):
    model = "srs_sg384"
    idn = "Stanford Research Systems,SG384,s/n{serial},ver1.02.0E"
    default_serial = "001523"
    has_syst_err = False
    terminators = (b"\n", b"\r")

    def reset(self) -> None:
        self.freq, self.ampr, self.enbr, self.ampl, self.enbl = 1e9, -10.0, False, 0.0, False
        self.last_err = 0
        self._drive()

    def _drive(self) -> None:
        on = self.enbr and self.freq >= 950e3
        self.put("rf", Source("RF", dc=self.ampr, ac={self.freq: 1.0}) if on else None)
        if self.terminal("rf")[0] == "mw":
            self.world.optics.mw = (self.freq, self.ampr) if on else None

    def push_error(self, code: int, text: str) -> None:
        super().push_error(code, text)
        self.last_err = 110 if code == -113 else 200

    @command("FREQ")
    def _freq(self, q, a, ch):
        if q:
            return "%.6f" % self.freq
        self.freq = a.num(0, 0.0, 4.05e9)
        self._drive()

    @command("AMPR")
    def _ampr(self, q, a, ch):
        if q:
            return "%.2f" % self.ampr
        self.ampr = a.num(0, -110.0, 16.5)
        self._drive()

    @command("ENBR")
    def _enbr(self, q, a, ch):
        if q:
            return onoff(self.enbr)
        self.enbr = a.bool(0)
        self._drive()

    @command("AMPL")
    def _ampl(self, q, a, ch):
        if q:
            return "%.2f" % self.ampl
        self.ampl = a.num(0, -47.0, 13.0)

    @command("ENBL")
    def _enbl(self, q, a, ch):
        if q:
            return onoff(self.enbl)
        self.enbl = a.bool(0)

    @command("LERR", setter=False)
    def _lerr(self, q, a, ch):
        v, self.last_err = self.last_err, 0
        return str(v)

    @command("MODL", "TYPE", "PHAS", "MFNC", "MODF", "FDEV", "PDEV", "ADEP", "DISP", "REMT", "LCAL", "PFNC", "QFNC",
             "PDTY", "PPER", "PWID", "SFNC")
    def _misc(self, q, a, ch):
        return "0" if q else None
