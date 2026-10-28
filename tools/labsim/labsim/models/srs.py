"""Stanford Research lock-ins: the SR830 (GPIB / RS-232) and the SR860.

A lock-in demodulates its input at reference x harmonic: it finds a signal only where the sample is excited at
that frequency. Its outputs settle through 1-4 filter stages of the time constant; past the sensitivity they
overload; with an external reference and nothing on the reference input it is unlocked.
"""
from __future__ import annotations

import cmath
import math
from typing import List, Optional

from ..instrument import ILLEGAL, Instrument, command, fail, onoff
from ..world import Source
from .common import ENBW_FACTOR, SENS_830, TAU_830, Wired, g6

CR, LF = bytes([13]), bytes([10])
SCAL_860 = [1.0, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01, 5e-3, 2e-3, 1e-3, 5e-4, 2e-4, 1e-4, 5e-5, 2e-5, 1e-5, 5e-6, 2e-6,
            1e-6, 5e-7, 2e-7, 1e-7, 5e-8, 2e-8, 1e-8, 5e-9, 2e-9, 1e-9]
TAU_860 = [1e-6, 3e-6, 10e-6, 30e-6, 100e-6, 300e-6, 1e-3, 3e-3, 10e-3, 30e-3, 100e-3, 300e-3, 1.0, 3.0, 10.0, 30.0,
           100.0, 300.0, 1e3, 3e3, 10e3, 30e3]


class LockIn(Wired, Instrument):
    has_syst_err = False
    terminators = (b"\n", b"\r")
    noise_density = 6e-9

    def lockin_reset(self) -> None:
        self.internal, self.freq, self.harm, self.phase = True, 1000.0, 1, 0.0
        self.amp, self.offset = 1.0, 0.0
        self.isrc, self.slope, self.sync = 0, 1, False
        self.stages = [0j, 0j, 0j, 0j]
        self.lias = 0
        self.aux = [0.0, 0.0, 0.0, 0.0]

    # -- what the model's sensitivity / time constant tables give
    def sens(self) -> float:
        raise NotImplementedError

    def tau(self) -> float:
        raise NotImplementedError

    def status(self):
        return {"ref_Hz": self.ref(), "internal": self.internal, "amplitude_V": self.amp, "sens_V": self.sens(),
                "tau_s": self.tau(), "XY": self.stages[self.slope]}

    def ref(self) -> Optional[float]:
        if self.internal:
            return self.freq
        term, _ = self.terminal("ref_in")
        return self.world.frequency_on(term) if term else None

    def drive_sine(self) -> None:
        ac = {self.freq: complex(self.amp)} if self.internal and self.amp > 0 else {}
        self.put("sine_out", Source("V", dc=self.offset, ac=ac) if ac or self.offset else None)
        if self.terminal("ref_out")[0]:   # the TTL reference output, for another lock-in's reference input
            f = self.ref()
            self.put("ref_out", Source("V", ac={f: 1.0}) if f else None)
        for k in range(4):
            if self.terminal("aux%d" % (k + 1))[0]:
                self.put("aux%d" % (k + 1), Source("V", dc=self.aux[k]))

    def _input(self):
        term, _ = self.terminal("A" if self.terminal("A")[0] else "in")
        if not term:
            return {}
        if self.isrc >= 2 and term == "bias":            # current input: the current through the sample
            return {f: i for f, i in self.world.bias().items()}
        return self.world.voltage(term)

    def target(self) -> complex:
        f = self.ref()
        sig = self._input()
        enbw = ENBW_FACTOR[self.slope] / self.tau()
        noise = self.noise_density * (1e6 if self.isrc >= 2 else 1.0) * math.sqrt(enbw)
        if f is None:
            self.lias |= 8
            return complex(self.noise(3 * noise + 1e-6), self.noise(3 * noise + 1e-6))
        fd = f * self.harm
        z = 0j
        for fc, a in sig.items():
            if fc > 0 and abs(fc - fd) <= 1e-6 * fd:
                z += a
        peak = sum(abs(a) * (1.414 if fc else 1.0) for fc, a in sig.items())
        if peak > (1.0 if self.isrc < 2 else 1e-6):
            self.lias |= 1
        return z * cmath.exp(-1j * math.radians(self.phase)) + complex(self.noise(noise), self.noise(noise))

    def tick(self, dt: float, now: float) -> None:
        self.drive_sine()
        tgt = self.target()
        k = 1.0 - math.exp(-dt / max(self.tau(), 1e-6))
        prev = tgt
        for i in range(self.slope + 1):
            self.stages[i] += (prev - self.stages[i]) * k
            prev = self.stages[i]
        if abs(prev.real) > 1.09 * self.sens() or abs(prev.imag) > 1.09 * self.sens():
            self.lias |= 4

    def xy(self) -> complex:
        z = self.stages[self.slope]
        enbw = ENBW_FACTOR[self.slope] / self.tau()
        z += complex(self.noise(0.2 * self.noise_density * math.sqrt(enbw)),
                     self.noise(0.2 * self.noise_density * math.sqrt(enbw)))
        s = 1.09 * self.sens()
        return complex(max(-s, min(s, z.real)), max(-s, min(s, z.imag)))

    def output(self, which: str) -> float:
        z = self.xy()
        return {"X": z.real, "Y": z.imag, "R": abs(z), "T": math.degrees(cmath.phase(z)) if z else 0.0}[which]


class SR830(LockIn):
    model = "srs_sr830"
    idn = "Stanford_Research_Systems,SR830,s/n{serial},ver1.07"
    default_serial = "48231"

    def setup(self) -> None:
        self.outx = int(self.opt("outx", 1))   # replies go to GPIB (1) or RS-232 (0); *RST leaves it

    def reset(self) -> None:
        self.lockin_reset()
        self.amp = 1.0
        self.sens_i, self.oflt_i, self.slope = 26, 8, 1
        self.rmod, self.ilin, self.icpl, self.ignd = 1, 0, 0, 0
        self.rslp = 0   # the external reference's trigger: sine zero crossing (1: TTL rising, 2: TTL falling edge)

    def sens(self) -> float:
        return SENS_830[self.sens_i] * (1e-6 if self.isrc >= 2 else 1.0)

    def tau(self) -> float:
        return TAU_830[self.oflt_i]

    def send(self, reply, now=None, delay=None) -> None:
        if reply is not None and (self.bus == "asrl") != (self.outx == 0):
            self.traffic.append(("", "x", "reply went to the %s interface (OUTX %d), not this one"
                                 % ("RS-232" if self.outx == 0 else "GPIB", self.outx)))
            return
        self.reply_end = CR if self.bus == "asrl" else LF
        super().send(reply, now, delay)

    @command("OUTX")
    def _outx(self, q, a, ch):
        if q:
            return str(self.outx)
        self.outx = a.int(0, 0, 1)

    @command("PHAS")
    def _phas(self, q, a, ch):
        if q:
            return "%.2f" % self.phase
        self.phase = a.num(0, -360.0, 729.99)

    @command("FMOD")
    def _fmod(self, q, a, ch):
        if q:
            return "1" if self.internal else "0"
        self.internal = a.int(0, 0, 1) == 1

    @command("FREQ")
    def _freq(self, q, a, ch):
        if q:
            f = self.ref()
            return g6(f if f else 0.0)
        if not self.internal:
            return None   # ignored with an external reference
        self.freq = a.num(0, 0.001, 102000.0 / self.harm)

    @command("RSLP")
    def _rslp(self, q, a, ch):
        if q:
            return str(self.rslp)
        self.rslp = a.int(0, 0, 2)

    @command("HARM")
    def _harm(self, q, a, ch):
        if q:
            return str(self.harm)
        self.harm = a.int(0, 1, 19999)

    @command("SLVL")
    def _slvl(self, q, a, ch):
        if q:
            return "%.3f" % self.amp
        self.amp = round(a.num(0, 0.004, 5.0), 3)

    @command("ISRC")
    def _isrc(self, q, a, ch):
        if q:
            return str(self.isrc)
        self.isrc = a.int(0, 0, 3)

    @command("IGND")
    def _ignd(self, q, a, ch):
        if q:
            return str(self.ignd)
        self.ignd = a.int(0, 0, 1)

    @command("ICPL")
    def _icpl(self, q, a, ch):
        if q:
            return str(self.icpl)
        self.icpl = a.int(0, 0, 1)

    @command("ILIN")
    def _ilin(self, q, a, ch):
        if q:
            return str(self.ilin)
        self.ilin = a.int(0, 0, 3)

    @command("RMOD")
    def _rmod(self, q, a, ch):
        if q:
            return str(self.rmod)
        self.rmod = a.int(0, 0, 2)

    @command("SENS")
    def _sens(self, q, a, ch):
        if q:
            return str(self.sens_i)
        self.sens_i = a.int(0, 0, 26)

    @command("OFLT")
    def _oflt(self, q, a, ch):
        if q:
            return str(self.oflt_i)
        self.oflt_i = a.int(0, 0, 19)

    @command("OFSL")
    def _ofsl(self, q, a, ch):
        if q:
            return str(self.slope)
        self.slope = a.int(0, 0, 3)

    @command("SYNC")
    def _sync(self, q, a, ch):
        if q:
            return onoff(self.sync)
        self.sync = a.int(0, 0, 1) == 1

    @command("OUTP", setter=False)
    def _outp(self, q, a, ch):
        i = a.int(0, 1, 4)
        return g6(self.output("XYRT"[i - 1]))

    @command("OUTR", setter=False)
    def _outr(self, q, a, ch):
        i = a.int(0, 1, 2)
        return g6(self.output("X" if i == 1 else "Y"))

    @command("SNAP", setter=False)
    def _snap(self, q, a, ch):
        a.need(2, 6)
        z = self.xy()
        vals = []
        for k in range(len(a)):
            i = a.int(k, 1, 11)
            vals.append({1: z.real, 2: z.imag, 3: abs(z), 4: math.degrees(cmath.phase(z)) if z else 0.0,
                         9: self.ref() or 0.0}.get(i, self.aux[i - 5] if 5 <= i <= 8 else 0.0))
        return ",".join(g6(v) for v in vals)

    @command("AUXV")
    def _auxv(self, q, a, ch):
        k = a.int(0, 1, 4)
        if q:
            return "%.3f" % self.aux[k - 1]
        a.need(2, 2)
        self.aux[k - 1] = a.num(1, -10.5, 10.5)
        self.drive_sine()

    @command("OAUX", setter=False)
    def _oaux(self, q, a, ch):
        k = a.int(0, 1, 4)
        return "%.3f" % (self.dc_on("auxin%d" % k) if self.terminal("auxin%d" % k)[0] else self.noise(1e-3))

    @command("LIAS", setter=False)
    def _lias(self, q, a, ch):
        if len(a):
            bit = a.int(0, 0, 7)
            v = (self.lias >> bit) & 1
            self.lias &= ~(1 << bit)
            return str(v)
        v, self.lias = self.lias, 0
        return str(v)

    @command("ERRS", setter=False)
    def _errs(self, q, a, ch):
        return "0"

    @command("APHS", query=False)
    def _aphs(self, q, a, ch):
        z = self.stages[self.slope]
        if z:
            self.phase = (self.phase + math.degrees(cmath.phase(z)) + 180.0) % 360.0 - 180.0
        self.delay(2.0 * self.tau())

    @command("AGAN", query=False)
    def _agan(self, q, a, ch):
        r = abs(self.stages[self.slope])
        self.sens_i = next((i for i, s in enumerate(SENS_830) if s > 1.25 * r), 26)
        self.busy_until = self.now + 1.0 + 5 * self.tau()

    @command("AOFF", "AREF", "OEXP", "DDEF", "FPOP", "LOCL", "OVRM", "ALRM", "KCLK", "SRAT", "SEND", "TSTR",
             "STRT", "PAUS", "REST", "TRIG", "*PSC", "ERRE", "LIAE")
    def _misc(self, q, a, ch):
        return "0" if q else None


class SR860(LockIn):
    model = "srs_sr860"
    idn = "Stanford_Research_Systems,SR860,{serial},v1.51"
    default_serial = "003456"
    noise_density = 2.5e-9

    def reset(self) -> None:
        self.lockin_reset()
        self.amp = float(self.opt("power_on_amplitude", 0.1))
        self.scal_i, self.oflt_i, self.slope = 0, 10, 1
        self.irng, self.ivmd = 0, 0

    def sens(self) -> float:
        return SCAL_860[self.scal_i] * (1e-6 if self.isrc >= 2 else 1.0)

    def tau(self) -> float:
        return TAU_860[self.oflt_i]

    def _out_index(self, text: str) -> int:
        t = text.strip().upper()
        names = {"X": 0, "Y": 1, "R": 2, "THETA": 3, "THET": 3, "TH": 3, "AUXIN0": 4, "AUXIN1": 5, "AUXIN2": 6,
                 "AUXIN3": 7, "XNOISE": 8, "YNOISE": 9, "AUXOUT0": 10, "AUXOUT1": 11, "PHASE": 12, "SAMP": 13,
                 "FREQ": 15, "FREQEXT": 15}
        if t in names:
            return names[t]
        try:
            return int(t)
        except ValueError:
            raise fail(ILLEGAL, text)

    def _value(self, i: int, z: complex) -> float:
        return {0: z.real, 1: z.imag, 2: abs(z), 3: math.degrees(cmath.phase(z)) if z else 0.0,
                12: self.phase, 13: self.amp, 15: self.ref() or 0.0}.get(i, 0.0)

    @command("OUTP", setter=False)
    def _outp(self, q, a, ch):
        return "%.6e" % self._value(self._out_index(a.text(0)), self.xy())

    @command("SNAP", setter=False)
    def _snap(self, q, a, ch):
        a.need(2, 3)
        z = self.xy()
        return ",".join("%.6e" % self._value(self._out_index(a.text(k)), z) for k in range(len(a)))

    @command("SLVL")
    def _slvl(self, q, a, ch):
        if q:
            return "%.6e" % self.amp
        self.amp = a.num(0, 1e-9, 2.0)

    @command("SOFF")
    def _soff(self, q, a, ch):
        if q:
            return "%.6e" % self.offset
        self.offset = a.num(0, -5.0, 5.0)

    @command("FREQ")
    def _freq(self, q, a, ch):
        if q:
            return "%.6e" % (self.ref() or 0.0)
        self.freq = a.num(0, 1e-3, 500e3)

    @command("FREQINT", setter=False)
    def _freqint(self, q, a, ch):
        return "%.6e" % self.freq

    @command("FREQEXT", "FREQDET", setter=False)
    def _freqext(self, q, a, ch):
        return "%.6e" % (self.ref() or 0.0)

    @command("PHAS")
    def _phas(self, q, a, ch):
        if q:
            return "%.6e" % self.phase
        self.phase = a.num(0, -360000.0, 360000.0)

    @command("HARM")
    def _harm(self, q, a, ch):
        if q:
            return str(self.harm)
        self.harm = a.int(0, 1, 99)

    @command("RSRC")
    def _rsrc(self, q, a, ch):
        if q:
            return "0" if self.internal else "1"
        t = a.text(0).upper()
        v = {"INT": 0, "INTERNAL": 0, "EXT": 1, "EXTERNAL": 1, "DUAL": 2, "CHOP": 3}.get(t)
        if v is None:
            v = a.int(0, 0, 3)
        self.internal = v != 1

    @command("SCAL")
    def _scal(self, q, a, ch):
        if q:
            return str(self.scal_i)
        self.scal_i = a.int(0, 0, 27)

    @command("OFLT")
    def _oflt(self, q, a, ch):
        if q:
            return str(self.oflt_i)
        self.oflt_i = a.int(0, 0, 21)

    @command("OFSL")
    def _ofsl(self, q, a, ch):
        if q:
            return str(self.slope)
        self.slope = a.int(0, 0, 3)

    @command("ISRC")
    def _isrc(self, q, a, ch):
        if q:
            return str(self.isrc if self.isrc < 2 else 0)
        self.isrc = a.int(0, 0, 1) + (2 if self.ivmd else 0)

    @command("IVMD")
    def _ivmd(self, q, a, ch):
        if q:
            return str(self.ivmd)
        self.ivmd = a.int(0, 0, 1)
        self.isrc = (self.isrc % 2) + (2 if self.ivmd else 0)

    @command("IRNG")
    def _irng(self, q, a, ch):
        if q:
            return str(self.irng)
        self.irng = a.int(0, 0, 4)

    @command("ENBW", setter=False)
    def _enbw(self, q, a, ch):
        return "%.6e" % (ENBW_FACTOR[self.slope] / self.tau())

    @command("AUXV")
    def _auxv(self, q, a, ch):
        k = a.int(0, 0, 3)
        if q:
            return "%.6e" % self.aux[k]
        self.aux[k] = a.num(1, -10.5, 10.5)
        self.drive_sine()

    @command("LIAS", "CUROVLDSTAT", setter=False)
    def _lias(self, q, a, ch):
        v, self.lias = self.lias, 0
        return str(v)

    @command("ICPL", "IGND", "SYNC", "REFM", "RTRG", "REFZ", "TBMODE", "TBSOURCE", "ICUR", "DDEF", "CDSP",
             "ADVFILT", "OUTX", "LOCL", "ERRS", "CAPTURELEN", "CAPTURECFG", "CAPTURERATE")
    def _misc(self, q, a, ch):
        return "0" if q else None
