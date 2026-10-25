"""Spectrometers and spectrographs.

    seabreeze://<serial>     Ocean Insight / Ocean Optics USB spectrometers (USB2000+, USB4000, Flame, HR4000, QE Pro,
                             Maya...) through python-seabreeze (Kelvin installs it once a person approves;
                             seabreeze_os_setup once for its USB driver). seabreeze:// is the first one found.
    shamrock://<index>       Andor Shamrock and Kymera spectrographs (also monochromators) through Andor's
                             spectrograph library (atspectrograph or ShamrockCIF DLL, installed with Solis or the
                             Andor SDK), called directly. shamrock:// is the first.

Connecting changes nothing on either: a spectrometer keeps its integration time until Kelvin sets one (it can't be
read back, so Kelvin shows it once it has set it); a spectrograph keeps its grating, wavelength, slits and shutter."""
from __future__ import annotations

import ctypes
import os
import sys
import time
import urllib.parse
from typing import Any, List, Optional, Tuple

from . import envs
from .adapters import AdapterError
from .cards import Card
from .drivers import DriverBackend

FRESH_S = 0.2   # readings of a spectrum (peak...) taken within this long of it use it, not a new one


def _target(address: str) -> str:
    url = urllib.parse.urlparse(address)
    return (url.netloc or url.path).strip("/")


# ------------------------------------------------------------------ Ocean Insight (seabreeze)

class OceanSpectrometer(DriverBackend):
    """Paths: integration (s; set only), averages (spectra averaged; Kelvin's own), dark_correction and
    nonlinearity_correction (on/off, applied by seabreeze where the model supports them), spectrum (counts),
    wavelengths (nm), peak (counts) and peak_wavelength (nm) of the latest spectrum."""

    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        if module is None:
            try:
                import seabreeze.spectrometers as module   # type: ignore
            except ImportError as e:
                raise AdapterError("%s needs python-seabreeze (and seabreeze_os_setup once, for its USB driver): %s"
                                   % (name, envs.missing("seabreeze"))) from e
        self.sb = module
        serial = _target(address)
        try:
            self.spec = module.Spectrometer.from_serial_number(serial) if serial else \
                module.Spectrometer.from_first_available()
        except Exception as e:
            seen = [getattr(d, "serial_number", "?") for d in _devices(module)]
            raise AdapterError("%s: could not open the spectrometer %s (%s); seabreeze sees: %s"
                               % (name, serial or "", e, ", ".join(seen) or "none")) from e
        self.averages, self.dark, self.nonlinear = 1, False, False
        self._last: Optional[Tuple[float, List[float]]] = None

    def _spectrum(self, fresh: bool = True) -> List[float]:
        if not fresh and self._last is not None and time.monotonic() - self._last[0] < FRESH_S:
            return self._last[1]
        total: List[float] = []
        for _ in range(self.averages):
            try:
                ys = self.spec.intensities(correct_dark_counts=self.dark, correct_nonlinearity=self.nonlinear)
            except Exception as e:
                raise AdapterError("%s: %s" % (self.name, str(e).splitlines()[0])) from e
            ys = [float(y) for y in ys]
            total = ys if not total else [a + b for a, b in zip(total, ys)]
        ys = [y / self.averages for y in total]
        self._last = (time.monotonic(), ys)
        return ys

    def get(self, path: str) -> Any:
        if path == "spectrum":
            return self._spectrum()
        if path == "wavelengths":
            return [float(x) for x in self.spec.wavelengths()]
        if path in ("peak", "peak_wavelength"):
            ys = self._spectrum(fresh=False)
            i = max(range(len(ys)), key=ys.__getitem__) if ys else 0
            return ys[i] if path == "peak" else float(self.spec.wavelengths()[i])
        if path == "averages":
            return self.averages
        if path in ("dark_correction", "nonlinearity_correction"):
            return 1 if (self.dark if path == "dark_correction" else self.nonlinear) else 0
        raise AdapterError("%s: no %r" % (self.name, path))

    def set(self, path: str, value: Any) -> None:
        if path == "integration":
            low, high = self.spec.integration_time_micros_limits
            us = int(round(float(value) * 1e6))
            if not low <= us <= high:
                raise AdapterError("%s: integration time must be %g to %g s" % (self.name, low / 1e6, high / 1e6))
            self.spec.integration_time_micros(us)
            self._last = None
        elif path == "averages":
            if not 1 <= int(value) <= 10000:
                raise AdapterError("%s: averages is 1 to 10000" % self.name)
            self.averages = int(value)
        elif path == "dark_correction":
            self.dark = bool(value)
        elif path == "nonlinearity_correction":
            self.nonlinear = bool(value)
        else:
            raise AdapterError("%s: %r can't be set" % (self.name, path))

    def identity(self) -> str:
        return "Ocean Insight,%s,%s" % (getattr(self.spec, "model", "?"), getattr(self.spec, "serial_number", "?"))

    def close(self) -> None:
        try:
            self.spec.close()
        except Exception:
            pass


def _devices(module: Any) -> List[Any]:
    try:
        return list(module.list_devices())
    except Exception:
        return []


def discover_seabreeze(module: Any = None) -> List[Tuple[str, str]]:
    """(address, identity) of each Ocean spectrometer seabreeze sees. Listing only; nothing is opened."""
    if module is None:
        try:
            import seabreeze.spectrometers as module   # type: ignore
        except ImportError:
            return []
    return [("seabreeze://%s" % d.serial_number, "Ocean Insight,%s,%s" % (d.model, d.serial_number))
            for d in _devices(module)]


# ------------------------------------------------------------------ Andor Shamrock / Kymera (Andor's DLL)

SHAMROCK_OK = 20202
SLITS = {"input_side": 1, "input_direct": 2, "output_side": 3, "output_direct": 4}
INPUT_FLIPPER, OUTPUT_FLIPPER = 1, 2
SHUTTER_OPEN = 1   # 0 closed, 1 open, 2 BNC
_shamrock_users = [0]   # spectrographs open: the library is initialized once for all (a scan leaves it alone meanwhile)


def load_shamrock() -> Any:
    """Andor's spectrograph library, from Solis or the Andor SDK (or wherever Windows finds it), its calls declared."""
    if sys.platform != "win32" or not hasattr(ctypes, "WinDLL"):
        raise AdapterError("Andor's spectrograph library runs on Windows")
    bits = "64" if ctypes.sizeof(ctypes.c_void_p) == 8 else "32"
    folders = [os.path.join(os.environ.get(v, ""), sub) for v in ("ProgramFiles", "ProgramFiles(x86)")
               for sub in ("Andor SOLIS", os.path.join("Andor SOLIS", "Shamrock"), "Andor SDK",
                           os.path.join("Andor SDK", "Shamrock64"))]
    for f in folders:
        if os.path.isdir(f):
            os.add_dll_directory(f)
    names = ["atspectrograph.dll", "ShamrockCIF%s.dll" % bits, "ShamrockCIF.dll"]
    for name in [os.path.join(f, n) for f in folders for n in names] + names:
        try:
            lib = ctypes.WinDLL(name)
            break
        except OSError:
            continue
    else:
        raise AdapterError("Andor's spectrograph library isn't here (atspectrograph.dll or ShamrockCIF): install Andor "
                           "Solis or the Andor SDK")
    i, f, p = ctypes.c_int, ctypes.c_float, ctypes.POINTER
    for fn, args in (("ShamrockInitialize", [ctypes.c_char_p]), ("ShamrockClose", []),
                     ("ShamrockGetNumberDevices", [p(i)]), ("ShamrockGetSerialNumber", [i, ctypes.c_char_p]),
                     ("ShamrockGetWavelength", [i, p(f)]), ("ShamrockSetWavelength", [i, f]),
                     ("ShamrockGetGrating", [i, p(i)]), ("ShamrockSetGrating", [i, i]),
                     ("ShamrockGetAutoSlitWidth", [i, i, p(f)]), ("ShamrockSetAutoSlitWidth", [i, i, f]),
                     ("ShamrockGetShutter", [i, p(i)]), ("ShamrockSetShutter", [i, i]),
                     ("ShamrockGetFlipperMirror", [i, i, p(i)]), ("ShamrockSetFlipperMirror", [i, i, i])):
        func = getattr(lib, fn)
        func.argtypes, func.restype = args, ctypes.c_uint
    return lib


class AndorSpectrograph(DriverBackend):
    """Paths: wavelength (centre, nm), grating (1-based), slit_input (input side slit width, um, motorized slits
    only), shutter (on = open), flipper_output (side = 1, direct = 0)."""

    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        target = _target(address) or "0"
        try:
            self.idx = int(target)
        except ValueError:
            raise AdapterError("%s: a spectrograph address is shamrock://<index>, e.g. shamrock://0" % name)
        self.lib = module if module is not None else load_shamrock()
        if not _shamrock_users[0]:
            self._call("ShamrockInitialize", b"")   # opens Andor's link; sets nothing on the spectrograph
        _shamrock_users[0] += 1
        n = ctypes.c_int()
        try:
            self._call("ShamrockGetNumberDevices", ctypes.byref(n))
            if not 0 <= self.idx < n.value:
                raise AdapterError("%s: no spectrograph %d (Andor's library sees %d)" % (name, self.idx, n.value))
        except AdapterError:
            self.close()
            raise

    def _call(self, fn: str, *args: Any) -> None:
        code = getattr(self.lib, fn)(*args)
        if code != SHAMROCK_OK:
            raise AdapterError("%s: Andor %s failed (code %s)" % (self.name, fn.replace("Shamrock", ""), code))

    def get(self, path: str) -> Any:
        if path in ("wavelength", "slit_input"):
            v = ctypes.c_float()
            if path == "wavelength":
                self._call("ShamrockGetWavelength", self.idx, ctypes.byref(v))
            else:
                self._call("ShamrockGetAutoSlitWidth", self.idx, SLITS["input_side"], ctypes.byref(v))
            return float(v.value)
        n = ctypes.c_int()
        if path == "grating":
            self._call("ShamrockGetGrating", self.idx, ctypes.byref(n))
            return n.value
        if path == "shutter":
            self._call("ShamrockGetShutter", self.idx, ctypes.byref(n))
            return 1 if n.value == SHUTTER_OPEN else 0
        if path == "flipper_output":
            self._call("ShamrockGetFlipperMirror", self.idx, OUTPUT_FLIPPER, ctypes.byref(n))
            return n.value
        raise AdapterError("%s: no %r" % (self.name, path))

    def set(self, path: str, value: Any) -> None:
        if path == "wavelength":
            self._call("ShamrockSetWavelength", self.idx, ctypes.c_float(float(value)))
        elif path == "grating":
            self._call("ShamrockSetGrating", self.idx, int(value))
        elif path == "slit_input":
            self._call("ShamrockSetAutoSlitWidth", self.idx, SLITS["input_side"], ctypes.c_float(float(value)))
        elif path == "shutter":
            self._call("ShamrockSetShutter", self.idx, SHUTTER_OPEN if value else 0)
        elif path == "flipper_output":
            self._call("ShamrockSetFlipperMirror", self.idx, OUTPUT_FLIPPER, 1 if value else 0)
        else:
            raise AdapterError("%s: %r can't be set" % (self.name, path))

    def identity(self) -> str:
        buf = ctypes.create_string_buffer(64)
        try:
            self._call("ShamrockGetSerialNumber", self.idx, buf)
            return "Andor,Shamrock,%s" % buf.value.decode(errors="replace")
        except AdapterError:
            return "Andor,Shamrock,%d" % self.idx

    def close(self) -> None:
        _shamrock_users[0] = max(0, _shamrock_users[0] - 1)
        if not _shamrock_users[0]:
            try:
                self.lib.ShamrockClose()
            except Exception:
                pass


def discover_shamrock(module: Any = None) -> List[Tuple[str, str]]:
    """Andor spectrographs, counted by Andor's library (nothing moves). Left alone while Kelvin has one open."""
    if _shamrock_users[0]:
        return []
    try:
        lib = module if module is not None else load_shamrock()
    except Exception:   # not installed here (or not Windows): nothing to list
        return []
    try:
        if lib.ShamrockInitialize(b"") != SHAMROCK_OK:
            return []
        n = ctypes.c_int()
        if lib.ShamrockGetNumberDevices(ctypes.byref(n)) != SHAMROCK_OK:
            return []
        return [("shamrock://%d" % i, "Andor,Shamrock,%d" % i) for i in range(n.value)]
    except Exception:
        return []
    finally:
        try:
            lib.ShamrockClose()
        except Exception:
            pass
