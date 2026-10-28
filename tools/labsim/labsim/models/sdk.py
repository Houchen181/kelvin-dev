"""Devices reached through their makers' libraries (bench address sdk:<kind>:<serial>). The shims in shims/ give
programs the libraries' own names and calls; each call arrives here as sdk_call(method, args, kwargs) and returns
(result, seconds until it is ready). A result that is a function is evaluated after the wait, so an exposure or a
count reflects the world as it is at its end."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from ..instrument import Instrument
from .common import ENBW_FACTOR, Wired
from ..world import Source


class SdkError(Exception):
    def __init__(self, text: str, code: Any = None):
        super().__init__(text)
        self.code = code


class SdkDevice(Wired, Instrument):
    has_488 = False
    has_syst_err = False
    kind = ""

    def setup(self) -> None:
        self.opened_by: Optional[str] = None

    def sdk_info(self) -> Dict[str, Any]:
        return {}

    def sdk_call(self, method: str, args: List[Any], kwargs: Dict[str, Any], now: float) -> Tuple[Any, float]:
        fn = getattr(self, "do_" + method, None)
        if fn is None:
            raise SdkError("%s: no such call %r" % (self.name, method))
        self.now = now
        self.traffic.append(("", "<", "%s%r" % (method, tuple(args))))
        out = fn(*args, **kwargs)
        return out if isinstance(out, tuple) and len(out) == 2 and isinstance(out[1], (int, float)) and \
            getattr(out, "_is_wait", False) else (out, 0.0)

    def handle(self, text: str):
        return None


def wait(result: Any, seconds: float) -> Tuple[Any, float]:
    class _W(tuple):
        _is_wait = True
    return _W((result, seconds))


# ---------------------------------------------------------------- cameras
class BaslerCamera(SdkDevice):
    """A Basler ace (pylon): GenICam nodes ExposureTime (us), Gain (dB), Width, Height, PixelFormat."""
    model = "basler_ace"
    kind = "pylon"
    default_serial = "40012345"

    def reset(self) -> None:
        self.nodes: Dict[str, Any] = {"ExposureTime": 5000.0, "Gain": 0.0, "Width": int(self.opt("width", 640)),
                                      "Height": int(self.opt("height", 480)), "PixelFormat": "Mono8",
                                      "OffsetX": 0, "OffsetY": 0, "AcquisitionFrameRate": 30.0}

    def sdk_info(self):
        return {"model": self.opt("model_name", "acA1920-40um"), "class": "BaslerUsb",
                "friendly": "Basler %s (%s)" % (self.opt("model_name", "acA1920-40um"), self.serial_no)}

    def do_open(self, owner: str) -> None:
        if self.opened_by and self.opened_by != owner:
            raise SdkError("Device is exclusively opened by another client.", "RuntimeException")
        self.opened_by = owner

    def do_close(self, owner: str) -> None:
        if self.opened_by == owner:
            self.opened_by = None

    def do_get(self, node: str) -> Any:
        if node not in self.nodes:
            raise SdkError("Node not existing (file 'genicam', node %s)" % node, "LogicalErrorException")
        return self.nodes[node]

    def do_limits(self, node: str) -> Tuple[float, float]:
        return {"ExposureTime": (20.0, 1e7), "Gain": (0.0, 36.0), "Width": (16, 1920), "Height": (16, 1200),
                "AcquisitionFrameRate": (0.1, 40.0)}.get(node, (0, 1e9))

    def do_set(self, node: str, value: Any) -> None:
        lo, hi = self.do_limits(node)
        if node in ("ExposureTime", "Gain", "Width", "Height", "AcquisitionFrameRate") and not lo <= float(value) <= hi:
            raise SdkError("Value = %s must be within %s..%s : OutOfRangeException" % (value, lo, hi),
                           "OutOfRangeException")
        if node == "PixelFormat" and value not in ("Mono8", "Mono12", "Mono12p"):
            raise SdkError("Value %s is not a valid entry" % value, "InvalidArgumentException")
        self.nodes[node] = value

    def do_grab(self, timeout_ms: int) -> Any:
        exp = self.nodes["ExposureTime"] * 1e-6
        if exp * 1000 > timeout_ms:
            return wait(lambda: {"ok": False, "error": "Grab timed out"}, timeout_ms / 1000.0)
        w, h = int(self.nodes["Width"]), int(self.nodes["Height"])

        def frame() -> Dict[str, Any]:
            img = self.world.optics.image(w, h) * exp * 1e3 * 10 ** (self.nodes["Gain"] / 20.0) * 40.0
            full = 4095 if self.nodes["PixelFormat"].startswith("Mono12") else 255
            img = np.random.default_rng().poisson(np.clip(img, 0, 1e7) * full / 255.0) + 2
            img = np.clip(img, 0, full).astype(np.uint16 if full > 255 else np.uint8)
            return {"ok": True, "array": img}
        return wait(frame, exp + 0.012)


class AndorSdk3Camera(SdkDevice):
    """An sCMOS (Zyla) through pyAndorSDK3: features as attributes; cooling takes minutes, as on the real one."""
    model = "andor_zyla"
    kind = "andor3"
    default_serial = "VSC-01234"

    def reset(self) -> None:
        self.feat: Dict[str, Any] = {"ExposureTime": 0.01, "SensorCooling": False, "CameraModel": "ZYLA-5.5-USB3",
                                     "AOIWidth": 640, "AOIHeight": 540, "PixelEncoding": "Mono16",
                                     "TargetSensorTemperature": 0.0}
        self.temp = 22.0

    def tick(self, dt: float, now: float) -> None:
        target = -10.0 if self.feat["SensorCooling"] else 22.0
        self.temp += (target - self.temp) * (1 - math.exp(-dt / 40.0))

    def do_get(self, feature: str) -> Any:
        if feature == "SensorTemperature":
            return round(self.temp + self.noise(0.02), 2)
        if feature == "TemperatureStatus":
            if not self.feat["SensorCooling"]:
                return "Cooler Off"
            return "Stabilised" if abs(self.temp + 10.0) < 0.2 else "Not Stabilised"
        if feature == "SerialNumber":
            return self.serial_no
        if feature not in self.feat:
            raise SdkError("ATCore error 2 (AT_ERR_NOTIMPLEMENTED) on %s" % feature, 2)
        return self.feat[feature]

    def do_set(self, feature: str, value: Any) -> None:
        if feature in ("SensorTemperature", "TemperatureStatus", "SerialNumber", "CameraModel"):
            raise SdkError("ATCore error 4 (AT_ERR_READONLY) on %s" % feature, 4)
        if feature == "ExposureTime" and not 1e-5 <= float(value) <= 30.0:
            raise SdkError("ATCore error 19 (AT_ERR_OUTOFRANGE) on ExposureTime", 19)
        self.feat[feature] = value

    def do_acquire(self, timeout_ms: int) -> Any:
        exp = float(self.feat["ExposureTime"])
        w, h = int(self.feat["AOIWidth"]), int(self.feat["AOIHeight"])
        if exp * 1000 > timeout_ms:
            return wait(lambda: (_ for _ in ()).throw(SdkError("ATCore error 13 (AT_ERR_TIMEDOUT)", 13)),
                        timeout_ms / 1000.0)

        def frame():
            dark = 0.1 * math.exp((self.temp - 0.0) / 7.0) * exp
            img = self.world.optics.image(w, h, 0.1) * exp * 1e3 * 400.0 + 100.0 + dark
            img = np.random.default_rng().poisson(np.clip(img, 0, 1e8))
            return np.clip(img, 0, 65535).astype(np.uint16)
        return wait(frame, exp + 0.01)


class AndorSdk2Camera(SdkDevice):
    """A CCD (Newton / iDus) through pyAndorSDK2's atmcd: calls return (code, values...); 20002 is success.
    Behind a spectrograph (bench `spectrograph: <name>`), full vertical binning gives the spectrum."""
    model = "andor_ccd"
    kind = "andor2"
    default_serial = "12345"
    OK = 20002

    def reset(self) -> None:
        self.initialized = False
        self.width, self.height = int(self.opt("width", 1024)), int(self.opt("height", 255))
        self.exposure, self.acq_mode, self.read_mode = 0.1, 1, 4
        self.cooler, self.setpoint, self.temp = False, -20.0, 20.0
        self.data: Optional[np.ndarray] = None
        self.ready_at = 0.0

    def status(self):
        return {"initialized": self.initialized, "T_C": round(self.temp, 1), "cooler": self.cooler,
                "setpoint_C": self.setpoint, "exposure_s": self.exposure}

    def tick(self, dt: float, now: float) -> None:
        target = max(self.setpoint, -100.0) if self.cooler else 20.0
        rate = 0.25 if self.cooler else 0.08               # K/s: a CCD cools over minutes
        d = target - self.temp
        self.temp += math.copysign(min(abs(d), rate * dt), d)

    def _need_init(self) -> None:
        if not self.initialized:
            raise SdkError("not initialized", 20075)      # DRV_NOT_INITIALIZED

    def do_Initialize(self, path: str = "") -> int:
        self.initialized = True
        return wait(self.OK, 1.5)

    def do_ShutDown(self) -> int:
        self.initialized = False
        self.cooler = False
        return self.OK

    def do_GetDetector(self):
        return (self.OK, self.width, self.height) if self.initialized else (20075, 0, 0)

    def do_GetHeadModel(self):
        return (self.OK, str(self.opt("head_model", "DU401A-BV"))) if self.initialized else (20075, "")

    def do_GetCameraSerialNumber(self):
        return (self.OK, int(self.serial_no)) if self.initialized else (20075, 0)

    def do_SetTemperature(self, t: int) -> int:
        if not self.initialized:
            return 20075
        if not -100 <= int(t) <= 30:
            return 20021                                    # DRV_P1INVALID
        self.setpoint = float(t)
        return self.OK

    def do_CoolerON(self) -> int:
        if not self.initialized:
            return 20075
        self.cooler = True
        return self.OK

    def do_CoolerOFF(self) -> int:
        if not self.initialized:
            return 20075
        self.cooler = False
        return self.OK

    def do_IsCoolerOn(self):
        return (self.OK, 1 if self.cooler else 0) if self.initialized else (20075, 0)

    def do_GetTemperatureF(self):
        if not self.initialized:
            return (20075, 0.0)
        if not self.cooler:
            code = 20034                                    # DRV_TEMP_OFF
        elif abs(self.temp - self.setpoint) < 0.5:
            code = 20036                                    # DRV_TEMP_STABILIZED
        elif self.temp > self.setpoint + 0.5:
            code = 20037                                    # DRV_TEMP_NOT_REACHED
        else:
            code = 20035
        return (code, round(self.temp + self.noise(0.05), 2))

    def do_GetTemperature(self):
        code, t = self.do_GetTemperatureF()
        return (code, int(round(t)))

    def do_SetExposureTime(self, t: float) -> int:
        if not self.initialized:
            return 20075
        if not 1e-5 <= float(t) <= 3600:
            return 20021
        self.exposure = float(t)
        return self.OK

    def do_GetAcquisitionTimings(self):
        return (self.OK, self.exposure, self.exposure + 0.02, self.exposure + 0.05)

    def do_SetAcquisitionMode(self, m: int) -> int:
        self.acq_mode = int(m)
        return self.OK

    def do_SetReadMode(self, m: int) -> int:
        if int(m) not in (0, 1, 2, 3, 4):
            return 20021
        self.read_mode = int(m)
        return self.OK

    def do_SetImage(self, hbin, vbin, hstart, hend, vstart, vend) -> int:
        return self.OK

    def do_SetShutter(self, *a) -> int:
        return self.OK

    def do_GetStatus(self):
        return (self.OK, 20072 if self.now < self.ready_at else 20073)   # ACQUIRING / IDLE

    def _wavelengths(self) -> np.ndarray:
        spec = self.server.instruments.get(str(self.opt("spectrograph", ""))) if hasattr(self, "server") else None
        if spec is not None and hasattr(spec, "calibration"):
            return spec.calibration(self.width, 26.0)
        return np.linspace(500.0, 900.0, self.width)

    def do_GetWavelengths(self):
        return (self.OK, self._wavelengths().tolist())

    def do_StartAcquisition(self) -> int:
        if not self.initialized:
            return 20075
        if self.now < self.ready_at:
            return 20072                                    # DRV_ACQUIRING
        self.ready_at = self.now + self.exposure + 0.03 + (0.25 if self.read_mode == 4 else 0.01)
        spec_dev = self.server.instruments.get(str(self.opt("spectrograph", ""))) if hasattr(self, "server") else None
        shutter_open = spec_dev is None or getattr(spec_dev, "shutter", 1) == 1
        nm = self._wavelengths()
        flux = self.world.optics.spectrum(nm) * (1.0 if shutter_open else 0.0)
        slit = getattr(spec_dev, "slit_um", 100.0) / 100.0 if spec_dev is not None else 1.0
        dark = 0.002 * 2.0 ** ((self.temp + 100.0) / 6.3)  # e-/px/s: ~1000 at +20 C, ~0.2 at -60 C
        rng = np.random.default_rng()
        rows = self.height if self.read_mode == 0 else 1  # full vertical binning sums every row's dark charge
        electrons = flux * slit * self.exposure * 0.6 * 2.0 + dark * self.exposure * rows
        counts = rng.poisson(np.clip(electrons, 0, 1e9)) / 2.0 + 300   # 2 e- per count, 300 counts of bias
        counts = counts + rng.normal(0, 4.0, self.width)
        if rng.random() < min(0.9, self.exposure / 20.0):   # a cosmic ray
            k = rng.integers(0, self.width)
            counts[k:k + 2] += rng.uniform(2e3, 2e4)
        if self.read_mode == 4:
            prof = np.exp(-0.5 * ((np.arange(self.height) - self.height / 2) / 8.0) ** 2)
            img = np.outer(prof / prof.sum(), counts) + rng.normal(0, 4.0, (self.height, self.width)) + 300
            self.data = np.clip(img, 0, 65535).astype(np.int32).ravel()
        else:
            self.data = np.clip(counts, 0, 65535).astype(np.int32)
        return self.OK

    def do_WaitForAcquisition(self):
        return wait(self.OK, max(0.0, self.ready_at - self.now))

    def do_AbortAcquisition(self) -> int:
        self.ready_at = self.now
        return self.OK

    def do_GetAcquiredData(self, size: int):
        if self.data is None or self.now < self.ready_at:
            return (20024, [])                              # DRV_NO_NEW_DATA
        if int(size) != len(self.data):
            return (20067, [])                              # DRV_P2INVALID: wrong array size
        return (self.OK, self.data)


class Shamrock(SdkDevice):
    """An Andor Shamrock / Kymera spectrograph: gratings 150, 600, 1200 l/mm; turning the turret takes seconds."""
    model = "andor_shamrock"
    kind = "shamrock"
    default_serial = "SR-3195"
    GRATINGS = {1: 150.0, 2: 600.0, 3: 1200.0}
    OK = 20202

    def reset(self) -> None:
        self.wavelength, self.grating, self.slit_um, self.shutter, self.flipper = 700.0, 2, 100.0, 1, 0
        self.busy_until = 0.0

    def status(self):
        return {"center_nm": self.wavelength, "grating_l_mm": self.GRATINGS[self.grating], "slit_um": self.slit_um}

    def calibration(self, pixels: int, pixel_um: float) -> np.ndarray:
        nm_per_mm = 1e6 / (self.GRATINGS[self.grating] * 300.0)    # focal length ~300 mm
        return self.wavelength + (np.arange(pixels) - pixels / 2 + 0.5) * pixel_um * 1e-3 * nm_per_mm

    def do_SetWavelength(self, nm: float) -> int:
        if not 0 <= float(nm) <= 1500:
            return 20249                                    # SHAMROCK_P2INVALID
        travel = abs(float(nm) - self.wavelength) / 300.0 + 0.4
        self.wavelength = float(nm)
        return wait(self.OK, travel)

    def do_GetWavelength(self) -> Tuple[int, float]:
        return self.OK, self.wavelength

    def do_SetGrating(self, g: int) -> int:
        if int(g) not in self.GRATINGS:
            return 20249
        changed = int(g) != self.grating
        self.grating = int(g)
        return wait(self.OK, 6.0 if changed else 0.1)

    def do_GetGrating(self) -> Tuple[int, int]:
        return self.OK, self.grating

    def do_SetAutoSlitWidth(self, slit: int, um: float) -> int:
        if not 10.0 <= float(um) <= 2500.0:
            return 20249
        self.slit_um = float(um)
        return wait(self.OK, 0.5)

    def do_GetAutoSlitWidth(self, slit: int) -> Tuple[int, float]:
        return self.OK, self.slit_um

    def do_SetShutter(self, mode: int) -> int:
        self.shutter = int(mode)
        return self.OK

    def do_GetShutter(self) -> Tuple[int, int]:
        return self.OK, self.shutter

    def do_SetFlipperMirror(self, which: int, pos: int) -> int:
        self.flipper = int(pos)
        return wait(self.OK, 1.0)

    def do_GetFlipperMirror(self, which: int) -> Tuple[int, int]:
        return self.OK, self.flipper

    def do_GetSerialNumber(self) -> Tuple[int, str]:
        return self.OK, self.serial_no


class OceanSpectrometer(SdkDevice):
    """An Ocean Insight USB4000 / Flame through python-seabreeze: 3648 pixels, 16-bit, fibre-coupled."""
    model = "ocean_usb4000"
    kind = "seabreeze"
    default_serial = "USB4F12345"

    def reset(self) -> None:
        self.int_us = 10000
        self.nm = np.linspace(345.0, 1040.0, 3648)

    def sdk_info(self):
        return {"model": self.opt("model_name", "USB4000")}

    def do_wavelengths(self):
        return self.nm

    def do_integration_time_micros(self, us: int) -> None:
        if not 10 <= int(us) <= 65_000_000:
            raise SdkError("integration time out of range")
        self.int_us = int(us)

    def do_limits(self):
        return (10, 65_000_000)

    def do_intensities(self):
        t = self.int_us * 1e-6

        def spec():
            s = self.world.optics.spectrum(self.nm) * t * 0.02 + 1500 + np.random.default_rng().normal(0, 12, len(self.nm))
            return np.clip(s, 0, 65535)
        return wait(spec, t + 0.005)


# ---------------------------------------------------------------- counting and lock-in
class TimeTaggerDevice(SdkDevice):
    """A Swabian Time Tagger: inputs wired to detectors (bench `channels: {1: apd}`)."""
    model = "swabian_timetagger"
    kind = "timetagger"
    default_serial = "1910000123"

    def reset(self) -> None:
        self.levels: Dict[int, float] = {}

    def sdk_info(self):
        return {"model": self.opt("model_name", "Time Tagger Ultra")}

    def _rate(self, ch: int) -> float:
        what = (self.opt("channels", {1: "apd"}) or {}).get(ch) or (self.opt("channels", {}) or {}).get(str(ch))
        if what == "apd":
            return self.world.optics.count_rate()
        if what == "apd2":
            return 0.97 * self.world.optics.count_rate()
        return 0.0

    def do_counts(self, channels: List[int], seconds: float, bins: int = 1):
        """Counts per channel per bin over `seconds`, as the measurement ends."""
        def result():
            rng = np.random.default_rng()
            return [[int(x) for x in rng.poisson(self._rate(int(c)) * seconds / bins, bins)] for c in channels]
        return wait(result, seconds)

    def do_rates(self, channels: List[int]) -> List[float]:
        return [self._rate(int(c)) for c in channels]

    def do_g2(self, binwidth_ps: float, n_bins: int, seconds: float):
        """Coincidences between two detectors behind a beam splitter: antibunched for one emitter in focus."""
        def result():
            r = self.world.optics.count_rate()
            near = self.world.optics._near()
            single = len(near) == 1 and near[0][1] > 0.3
            t = (np.arange(n_bins) - n_bins // 2) * binwidth_ps * 1e-12
            base = (r / 2) ** 2 * binwidth_ps * 1e-12 * seconds
            dip = (1 - 0.85 * np.exp(-np.abs(t) / 4e-9)) if single else np.ones(n_bins)
            return np.random.default_rng().poisson(np.clip(base * dip, 0, 1e12)).tolist()
        return wait(result, seconds)

    def do_set_level(self, ch: int, v: float) -> None:
        self.levels[int(ch)] = float(v)

    def do_get_level(self, ch: int) -> float:
        return self.levels.get(int(ch), 0.5)


class ZurichMFLI(SdkDevice):
    """An MFLI lock-in through LabOne nodes: sigouts drive the bench terminal `out` through its series resistor,
    demodulator 0 reads `in` at oscillator 0's frequency."""
    model = "zhinst_mfli"
    kind = "zhinst"
    default_serial = "dev5678"

    def reset(self) -> None:
        self.nodes: Dict[str, Any] = {"/oscs/0/freq": 1000.0, "/sigouts/0/on": 0, "/sigouts/0/amplitudes/1": 0.0,
                                      "/sigouts/0/enables/1": 1, "/sigouts/0/range": 1.0,
                                      "/demods/0/timeconstant": 0.01, "/demods/0/order": 3, "/demods/0/enable": 1,
                                      "/demods/0/rate": 1674.0, "/demods/0/harmonic": 1, "/demods/0/phaseshift": 0.0,
                                      "/sigins/0/range": 1.0, "/sigins/0/ac": 0, "/sigins/0/imp50": 0,
                                      "/features/devtype": "MFLI", "/features/serial": self.serial_no}
        self.z = 0j
        self._drive()

    def sdk_info(self):
        return {"devicetype": "MFLI", "connected": "", "serveraddress": "localhost", "serverport": 8004}

    def _drive(self) -> None:
        on = int(self.nodes["/sigouts/0/on"]) and int(self.nodes["/sigouts/0/enables/1"])
        amp = float(self.nodes["/sigouts/0/amplitudes/1"]) / math.sqrt(2)   # peak -> RMS
        f = float(self.nodes["/oscs/0/freq"])
        self.put("out", Source("V", ac={f: complex(amp)}) if on and amp > 0 else None)

    def tick(self, dt: float, now: float) -> None:
        self._drive()
        term = self.terminal("in")[0]
        sig = self.world.voltage(term) if term else {}
        f = float(self.nodes["/oscs/0/freq"]) * int(self.nodes["/demods/0/harmonic"])
        z = sum((a for fc, a in sig.items() if fc > 0 and abs(fc - f) <= 1e-6 * f), 0j) * math.sqrt(2)  # ZI: peak
        z *= complex(math.cos(math.radians(-self.nodes["/demods/0/phaseshift"])),
                     math.sin(math.radians(-self.nodes["/demods/0/phaseshift"])))
        tau = max(float(self.nodes["/demods/0/timeconstant"]), 1e-6)
        self.z += (z - self.z) * (1 - math.exp(-dt / tau))

    def do_connect(self, interface: str) -> None:
        return None

    def do_set(self, path: str, value: Any) -> None:
        p = path.lower()
        if p.startswith("/features/"):
            raise SdkError("ZIAPIException with status code: 16387. Node is read-only.")
        self.nodes[p] = value
        self._drive()

    def do_get(self, path: str) -> Any:
        p = path.lower()
        if p not in self.nodes:
            raise SdkError("ZIAPIException with status code: 16387. Path not found: %s" % path)
        return self.nodes[p]

    def do_sample(self, path: str) -> Dict[str, Any]:
        tau = max(float(self.nodes["/demods/0/timeconstant"]), 1e-6)
        enbw = ENBW_FACTOR[min(3, int(self.nodes["/demods/0/order"]) - 1)] / tau
        n = 2.5e-9 * math.sqrt(enbw) * math.sqrt(2)
        z = self.z + complex(self.noise(n), self.noise(n))
        return {"x": [z.real], "y": [z.imag], "frequency": [float(self.nodes["/oscs/0/freq"])],
                "timestamp": [int(self.now * 60e6)]}


# ---------------------------------------------------------------- a PPMS and an NI DAQ card
class QuantumDesignPPMS(SdkDevice):
    """A PPMS / DynaCool behind MultiVu: it controls the bench's cryostat and magnet itself; set points approach at
    the rate given (K/min, Oe/s); the resistivity bridge measures the sample (channel 1)."""
    model = "qd_ppms"
    kind = "ppms"
    default_serial = "127.0.0.1:5000"

    def reset(self) -> None:
        self.t_set = self.world.cryo.stage
        self.t_rate = 10.0
        self.b_set_oe, self.b_rate_oe, self.b_oe = 0.0, 100.0, 0.0
        self.t_target = self.t_set
        self.chamber = "Purged and Sealed"

    def status(self):
        return {"T_K": round(self.world.cryo.sample, 3), "T_set": self.t_set, "B_Oe": round(self.b_oe, 2),
                "B_set_Oe": self.b_set_oe}

    def tick(self, dt: float, now: float) -> None:
        cryo = self.world.cryo
        step = self.t_rate / 60.0 * dt
        d = self.t_set - self.t_target
        self.t_target = self.t_set if abs(d) <= step else self.t_target + math.copysign(step, d)
        cryo.stage += (self.t_target - cryo.stage) * (1 - math.exp(-dt / 4.0))     # the PPMS follows its ramp
        cryo.heaters.pop(self.name, None)
        d = self.b_set_oe - self.b_oe
        step = self.b_rate_oe * dt
        self.b_oe = self.b_set_oe if abs(d) <= step else self.b_oe + math.copysign(step, d)
        self.world.magnet.set("z", self.b_oe / 1e4, min(self.b_rate_oe / 1e4, self.world.magnet.quench_rate))

    def _t_status(self) -> str:
        if abs(self.world.cryo.sample - self.t_set) < max(0.01, 0.002 * self.t_set):
            return "Stable"
        return "Tracking" if self.t_target != self.t_set else "Near"

    def do_get_temperature(self):
        return (round(self.world.cryo.sample + self.noise(2e-3), 4), self._t_status())

    def do_set_temperature(self, setpoint: float, rate: float, mode: str) -> None:
        if not 1.8 <= float(setpoint) <= 400.0:
            raise SdkError("temperature set point out of range (1.8-400 K)")
        if not 0 < float(rate) <= 50:
            raise SdkError("rate out of range (0-50 K/min)")
        self.t_set, self.t_rate = float(setpoint), float(rate)

    def do_get_field(self):
        state = "Holding (Driven)" if self.b_oe == self.b_set_oe else "Charging"
        return (round(self.b_oe + self.noise(0.05), 2), state)

    def do_set_field(self, field_oe: float, rate_oe_s: float, approach: str, mode: str = "driven") -> None:
        lim = float(self.opt("max_field_Oe", 90000.0))
        if abs(float(field_oe)) > lim:
            raise SdkError("field set point out of range (+-%g Oe)" % lim)
        if not 0 < float(rate_oe_s) <= 220:
            raise SdkError("field rate out of range (0-220 Oe/s)")
        self.b_set_oe, self.b_rate_oe = float(field_oe), float(rate_oe_s)

    def do_get_chamber(self) -> str:
        return self.chamber

    def do_set_chamber(self, mode: str) -> None:
        self.chamber = {"seal": "Sealed", "purge_seal": "Purged and Sealed", "vent_seal": "Vented and Sealed",
                        "pump_continuous": "Pumping Continuously", "vent_continuous": "Venting Continuously",
                        "high_vacuum": "High Vacuum"}.get(str(mode), str(mode))

    def do_get_resistance(self, bridge: int):
        if int(bridge) != int(self.opt("bridge_channel", 1)):
            return (0.0, "Off")
        rxx, _ = self.world.sample.resistances(self.world.cryo.sample, self.world.magnet.B, self.world.gate(), 1e-5)
        return (rxx * (1 + self.noise(2e-4)), "Ok")


class NiDaq(SdkDevice):
    """An NI DAQ card: analogue inputs read bench terminals, analogue outputs drive them (galvo mirrors move the
    optics' x/y at `galvo_um_per_V`), a counter counts the APD."""
    model = "ni_daq"
    kind = "nidaq"
    default_serial = "Dev1"

    def reset(self) -> None:
        self.ao: Dict[int, float] = {}

    def sdk_info(self):
        return {"product_type": self.opt("product_type", "USB-6363"), "serial": int(self.opt("serial", 0x1F2E3D4C))}

    def _ai(self, ch: int) -> float:
        term = (self.opt("ai", {}) or {}).get(ch) or (self.opt("ai", {}) or {}).get(str(ch))
        if not term:
            return self.noise(1e-3)
        if term in ("vxx", "vxy", "pd"):
            return self.world.voltage(term).get(0.0, 0j).real + self.noise(2e-4)
        return self.world.dc(term) + self.noise(2e-4)

    def do_read_ai(self, channels: List[int], n: int, rate: float):
        def result():
            return [[self._ai(int(c)) for _ in range(int(n))] for c in channels]
        return wait(result, max(0.0, int(n) / max(float(rate), 1.0)))

    def do_write_ao(self, channels: List[int], values: List[float]) -> None:
        um = float(self.opt("galvo_um_per_V", 20.0))
        for c, v in zip(channels, values):
            if not -10.0 <= float(v) <= 10.0:
                raise SdkError("Requested value is not a supported value for this property. Property: AO.Max",
                               -200077)
            self.ao[int(c)] = float(v)
            term = (self.opt("ao", {}) or {}).get(int(c)) or (self.opt("ao", {}) or {}).get(str(c))
            if term in ("galvo_x", "galvo_y"):
                self.world.optics.fine[0 if term == "galvo_x" else 1] = 50.0 + um * float(v)
            elif term:
                self.put("ao%d" % int(c), Source("V", dc=float(v)))

    def do_count(self, seconds: float):
        return wait(lambda: int(np.random.default_rng().poisson(self.world.optics.count_rate() * seconds)), seconds)
