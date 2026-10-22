"""Spectrometers and spectrographs.

    seabreeze://<serial>     Ocean Insight / Ocean Optics USB spectrometers (USB2000+, USB4000, Flame, HR4000, QE Pro,
                             Maya...) through python-seabreeze (pip install seabreeze; seabreeze_os_setup once for its
                             USB driver). seabreeze:// is the first one found.
    shamrock://<index>       Andor Shamrock and Kymera spectrographs (also monochromators) through pylablib
                             (pip install pylablib) and Andor's spectrograph SDK. shamrock:// is the first.

Connecting changes nothing on either: a spectrometer keeps its integration time until Kelvin sets one (it can't be
read back, so Kelvin shows it once it has set it); a spectrograph keeps its grating, wavelength, slits and shutter."""
from __future__ import annotations

import time
import urllib.parse
from typing import Any, List, Optional, Tuple

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
                raise AdapterError("%s needs python-seabreeze: pip install seabreeze, then run seabreeze_os_setup "
                                   "once for its USB driver" % name) from e
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


# ------------------------------------------------------------------ Andor Shamrock / Kymera (pylablib)

class AndorSpectrograph(DriverBackend):
    """Paths: wavelength (centre, nm), grating (1-based), slit_input (input side slit width, um, motorized slits
    only), shutter (on = open), flipper_output (side = 1, direct = 0)."""

    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        if module is None:
            try:
                from pylablib.devices import Andor as module   # type: ignore
            except ImportError as e:
                raise AdapterError("%s needs pylablib and Andor's spectrograph SDK: pip install pylablib (the SDK "
                                   "comes with Andor Solis or the Andor SDK)" % name) from e
        target = _target(address) or "0"
        try:
            self.idx = int(target)
        except ValueError:
            raise AdapterError("%s: a spectrograph address is shamrock://<index>, e.g. shamrock://0" % name)
        try:
            self.dev = module.ShamrockSpectrograph(self.idx)   # opens a handle; changes nothing
        except Exception as e:
            raise AdapterError("%s: could not open spectrograph %d: %s" % (name, self.idx, e)) from e

    def get(self, path: str) -> Any:
        try:
            if path == "wavelength":
                return self.dev.get_wavelength() * 1e9
            if path == "grating":
                return int(self.dev.get_grating())
            if path == "slit_input":
                return self.dev.get_slit_width("input_side") * 1e6
            if path == "shutter":
                return 1 if self.dev.get_shutter() == "opened" else 0
            if path == "flipper_output":
                return 1 if self.dev.get_flipper_port("output") == "side" else 0
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError("%s: %s: %s" % (self.name, path, str(e).splitlines()[0])) from e
        raise AdapterError("%s: no %r" % (self.name, path))

    def set(self, path: str, value: Any) -> None:
        try:
            if path == "wavelength":
                self.dev.set_wavelength(float(value) * 1e-9)
            elif path == "grating":
                self.dev.set_grating(int(value))
            elif path == "slit_input":
                self.dev.set_slit_width("input_side", float(value) * 1e-6)
            elif path == "shutter":
                self.dev.set_shutter("opened" if value else "closed")
            elif path == "flipper_output":
                self.dev.set_flipper_port("output", "side" if value else "direct")
            else:
                raise AdapterError("%s: %r can't be set" % (self.name, path))
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError("%s: %s: %s" % (self.name, path, str(e).splitlines()[0])) from e

    def identity(self) -> str:
        try:
            return "Andor,Shamrock,%s" % self.dev.get_device_info().serial_number
        except Exception:
            return "Andor,Shamrock,%d" % self.idx

    def close(self) -> None:
        try:
            self.dev.close()
        except Exception:
            pass


def discover_shamrock(module: Any = None) -> List[Tuple[str, str]]:
    if module is None:
        try:
            from pylablib.devices import Andor as module   # type: ignore
        except Exception:
            return []
    try:
        return [("shamrock://%d" % i, "Andor,Shamrock,%d" % i) for i in range(module.get_spectrographs_number())]
    except Exception:
        return []
