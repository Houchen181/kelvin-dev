"""Cameras: a frame is an image reading, kept as a .npy file (frames.py); its mean, max, min and sum are readings.

    pylon://<serial>         Basler cameras (ace, dart, boost, pulse...) through pypylon.
                             pylon:// is the first one found.
    andor://<index>?sdk=3    Andor sCMOS cameras (Zyla, Neo, Sona, Marana) through Andor's pyAndorSDK3
    andor://<index>?sdk=2    Andor CCD and EMCCD cameras (iXon, Newton, iDus, Luca, Clara) through pyAndorSDK2
                             (both come with Andor's SDK; sdk=3 is the default).
    uvc://<index>            USB video cameras (webcams, microscope cameras) through OpenCV
    genicam://<serial>       GenICam industrial cameras through Harvesters and the maker's GenTL producer (.cti)
                             (OpenCV and Harvesters: Kelvin installs them once a person approves)

Connecting opens the camera and sets nothing. A Basler camera keeps every setting. Andor's SDK2 starts each session
from its own defaults (the SDK, not Kelvin, does that; cooling is off until someone turns it on); Kelvin then changes
only what a person or plan sets. Readings of a frame (mean, max...) taken within 0.2 s of a frame use that frame;
otherwise they snap a new one (not kept as a file)."""
from __future__ import annotations

import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from . import envs
from . import frames
from .adapters import AdapterError
from .cards import Card
from .drivers import DriverBackend

FRESH_S = 0.2
STATS = ("mean", "max", "min", "sum")


def _target(address: str) -> Tuple[str, Dict[str, str]]:
    url = urllib.parse.urlparse(address)
    return (url.netloc or url.path).strip("/"), dict(urllib.parse.parse_qsl(url.query))


class Camera(DriverBackend):
    """What every camera shares: frame (saved, its path), and the latest frame's statistics."""

    def __init__(self, name: str, card: Card, address: str):
        super().__init__(name, card, address)
        self._last: Optional[Tuple[float, Any]] = None

    def snap(self) -> Any:
        raise NotImplementedError

    def _frame(self, fresh: bool) -> Any:
        if not fresh and self._last is not None and time.monotonic() - self._last[0] < FRESH_S:
            return self._last[1]
        try:
            frame = self.snap()
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError("%s: no frame: %s" % (self.name, str(e).splitlines()[0])) from e
        self._last = (time.monotonic(), frame)
        return frame

    def get(self, path: str) -> Any:
        if path == "frame":
            return frames.save(self._frame(fresh=True), self.name)
        if path in STATS:
            return frames.stats(self._frame(fresh=False))[path]
        return self.get_setting(path)

    def get_setting(self, path: str) -> Any:
        raise AdapterError("%s: no %r" % (self.name, path))


# ------------------------------------------------------------------ Basler (pypylon)

class BaslerCamera(Camera):
    """Paths: exposure (s), gain (dB; raw counts on cameras that only have GainRaw), width and height (pixels)."""

    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        if module is None:
            try:
                from pypylon import pylon as module   # type: ignore
            except ImportError as e:
                raise AdapterError("%s needs pypylon: %s" % (name, envs.missing("pypylon"))) from e
        self.pylon = module
        serial, _ = _target(address)
        tl = module.TlFactory.GetInstance()
        devices = list(tl.EnumerateDevices())
        pick = next((d for d in devices if not serial or d.GetSerialNumber() == serial), None)
        if pick is None:
            raise AdapterError("%s: no Basler camera %s (pylon sees: %s)" % (
                name, serial, ", ".join(d.GetSerialNumber() for d in devices) or "none"))
        try:
            self.cam = module.InstantCamera(tl.CreateDevice(pick))
            self.cam.Open()   # opens; every setting stays as it is
        except Exception as e:
            raise AdapterError("%s: could not open the camera: %s" % (name, e)) from e

    def _node(self, *names: str) -> Any:
        for n in names:
            node = getattr(self.cam, n, None)
            if node is not None:
                try:
                    if node.IsReadable() if hasattr(node, "IsReadable") else True:
                        return node
                except Exception:
                    continue
        raise AdapterError("%s: this camera has no %s" % (self.name, " or ".join(names)))

    def get_setting(self, path: str) -> Any:
        if path == "exposure":
            return float(self._node("ExposureTime", "ExposureTimeAbs").GetValue()) * 1e-6
        if path == "gain":
            return float(self._node("Gain", "GainRaw").GetValue())
        if path in ("width", "height"):
            return int(self._node(path.capitalize()).GetValue())
        return super().get_setting(path)

    def set(self, path: str, value: Any) -> None:
        if path == "exposure":
            self._node("ExposureTime", "ExposureTimeAbs").SetValue(float(value) * 1e6)
        elif path == "gain":
            node = self._node("Gain", "GainRaw")
            node.SetValue(int(value) if node is getattr(self.cam, "GainRaw", None) else float(value))
        else:
            raise AdapterError("%s: %r can't be set" % (self.name, path))
        self._last = None

    def snap(self) -> Any:
        timeout_ms = int(self.get_setting("exposure") * 1000) + 5000
        res = self.cam.GrabOne(timeout_ms)
        try:
            if not res.GrabSucceeded():
                raise AdapterError("%s: the grab failed: %s" % (self.name, res.GetErrorDescription()))
            return res.Array.copy() if hasattr(res.Array, "copy") else res.Array
        finally:
            res.Release()

    def identity(self) -> str:
        info = self.cam.GetDeviceInfo()
        return "Basler,%s,%s" % (info.GetModelName(), info.GetSerialNumber())

    def close(self) -> None:
        try:
            self.cam.Close()
        except Exception:
            pass


def discover_pylon(module: Any = None) -> List[Tuple[str, str]]:
    if module is None:
        try:
            from pypylon import pylon as module   # type: ignore
        except ImportError:
            return []
    try:
        return [("pylon://%s" % d.GetSerialNumber(), "Basler,%s,%s" % (d.GetModelName(), d.GetSerialNumber()))
                for d in module.TlFactory.GetInstance().EnumerateDevices()]
    except Exception:
        return []


# ------------------------------------------------------------------ Andor (Andor's own SDK wrappers)

DRV_SUCCESS = 20002
TEMP_STATUS = {20034: "cooler off", 20035: "not stabilized", 20036: "stabilized", 20037: "not reached",
               20040: "drifted"}
READ_FVB, READ_IMAGE, SINGLE_SCAN = 0, 4, 1


class AndorCamera(Camera):
    """Paths: exposure (s), temperature (sensor, C), cooler (on/off), setpoint (C; SDK2), spectrum (full vertical
    binning, counts per column; SDK2 spectroscopy cameras), plus the frame and its statistics."""

    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        target, q = _target(address)
        try:
            self.idx = int(target or 0)
        except ValueError:
            raise AdapterError("%s: an Andor address is andor://<index>?sdk=3 (or sdk=2)" % name)
        self.sdk_version = q.get("sdk", "3")
        if self.sdk_version not in ("2", "3"):
            raise AdapterError("%s: sdk is 2 (iXon, Newton, iDus...) or 3 (Zyla, Neo, Sona, Marana)" % name)
        if module is None:
            try:
                if self.sdk_version == "3":
                    import pyAndorSDK3 as module   # type: ignore
                else:
                    import pyAndorSDK2 as module   # type: ignore
            except ImportError as e:
                raise AdapterError("%s needs Andor's pyAndorSDK%s, from Andor's SDK: Kelvin installs it from the SDK's "
                                   "Python folder into its drivers environment once a person approves "
                                   "(install_for_instrument, env %s, files <that folder>)"
                                   % (name, self.sdk_version, envs.DRIVERS)) from e
        self.mod = module
        try:
            if self.sdk_version == "3":
                self.sdk = module.AndorSDK3()
                self.cam = self.sdk.GetCamera(self.idx)
            else:
                self.sdk = module.atmcd()
                self._ok(self.sdk.Initialize(""), "Initialize")
                ret, self.width, self.height = self.sdk.GetDetector()
                self._ok(ret, "GetDetector")
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError("%s: could not open Andor camera %d: %s" % (name, self.idx, e)) from e

    def _ok(self, ret: Any, what: str) -> None:
        code = ret[0] if isinstance(ret, tuple) else ret
        if code != DRV_SUCCESS:
            raise AdapterError("%s: Andor %s failed (code %s)" % (self.name, what, code))

    def get_setting(self, path: str) -> Any:
        if self.sdk_version == "3":
            if path == "exposure":
                return float(self.cam.ExposureTime)
            if path == "temperature":
                return float(self.cam.SensorTemperature)
            if path == "cooler":
                return 1 if self.cam.SensorCooling else 0
            if path == "temperature_status":
                return str(self.cam.TemperatureStatus)
        else:
            if path == "exposure":
                ret, exposure, _, _ = self.sdk.GetAcquisitionTimings()
                self._ok(ret, "GetAcquisitionTimings")
                return float(exposure)
            if path == "temperature":
                ret, t = self.sdk.GetTemperatureF()
                return float(t)
            if path == "temperature_status":
                ret, _ = self.sdk.GetTemperatureF()
                return TEMP_STATUS.get(ret, str(ret))
            if path == "cooler":
                ret, on = self.sdk.IsCoolerOn()
                self._ok(ret, "IsCoolerOn")
                return 1 if on else 0
            if path == "spectrum":
                return self._acquire(READ_FVB)
        return super().get_setting(path)

    def set(self, path: str, value: Any) -> None:
        if self.sdk_version == "3":
            if path == "exposure":
                self.cam.ExposureTime = float(value)
            elif path == "cooler":
                self.cam.SensorCooling = bool(value)
            else:
                raise AdapterError("%s: %r can't be set on an SDK3 camera" % (self.name, path))
        else:
            if path == "exposure":
                self._ok(self.sdk.SetExposureTime(float(value)), "SetExposureTime")
            elif path == "cooler":
                self._ok(self.sdk.CoolerON() if value else self.sdk.CoolerOFF(), "Cooler")
            elif path == "setpoint":
                self._ok(self.sdk.SetTemperature(int(round(float(value)))), "SetTemperature")
            else:
                raise AdapterError("%s: %r can't be set" % (self.name, path))
        self._last = None

    def _acquire(self, read_mode: int) -> Any:
        """SDK2: one single scan, as an image or a full-vertical-binning spectrum (the acquisition is set up for
        each snap: SDK2 has no other way to take one)."""
        s = self.sdk
        self._ok(s.SetAcquisitionMode(SINGLE_SCAN), "SetAcquisitionMode")
        self._ok(s.SetReadMode(read_mode), "SetReadMode")
        if read_mode == READ_IMAGE:
            self._ok(s.SetImage(1, 1, 1, self.width, 1, self.height), "SetImage")
        self._ok(s.StartAcquisition(), "StartAcquisition")
        self._ok(s.WaitForAcquisition(), "WaitForAcquisition")
        size = self.width * (self.height if read_mode == READ_IMAGE else 1)
        ret, data = s.GetAcquiredData(size)
        self._ok(ret, "GetAcquiredData")
        data = list(data) if not hasattr(data, "reshape") else data
        if read_mode == READ_FVB:
            return [float(x) for x in data]
        if hasattr(data, "reshape"):
            return data.reshape(self.height, self.width)
        return [data[r * self.width:(r + 1) * self.width] for r in range(self.height)]

    def snap(self) -> Any:
        if self.sdk_version == "3":
            timeout_ms = int(float(self.cam.ExposureTime) * 1000) + 5000
            return self.cam.acquire(timeout=timeout_ms).image
        return self._acquire(READ_IMAGE)

    def identity(self) -> str:
        try:
            if self.sdk_version == "3":
                return "Andor,%s,%s" % (self.cam.CameraModel, self.cam.SerialNumber)
            ret, model = self.sdk.GetHeadModel()
            ret2, serial = self.sdk.GetCameraSerialNumber()
            return "Andor,%s,%s" % (model, serial)
        except Exception:
            return "Andor,camera,%d" % self.idx

    def close(self) -> None:
        try:
            if self.sdk_version == "3":
                self.cam.close()
                return
            ret, on = self.sdk.IsCoolerOn()
            if ret == DRV_SUCCESS and on:   # Andor: let a cooled SDK2 camera warm up before ShutDown; Kelvin won't
                return                      # decide that, so the session stays open
            self.sdk.ShutDown()
        except Exception:
            pass


def discover_andor(modules: Optional[Dict[str, Any]] = None) -> List[Tuple[str, str]]:
    """Andor sCMOS cameras through pyAndorSDK3 (counting them opens none). SDK2 cameras are not listed: finding them
    means initializing one, which resets it; add them by address (andor://0?sdk=2)."""
    mod = (modules or {}).get("andor")
    if mod is None:
        try:
            import pyAndorSDK3 as mod   # type: ignore
        except ImportError:
            return []
    try:
        return [("andor://%d?sdk=3" % i, "Andor,sCMOS camera,%d" % i) for i in range(int(mod.AndorSDK3().DeviceCount))]
    except Exception:
        return []


# ------------------------------------------------------------------ USB video cameras (webcams, microscope cameras)

class UsbCamera(Camera):
    """A USB video camera (UVC: webcams, microscope and inspection cameras) through OpenCV, which Kelvin installs once
    a person approves. Address uvc://<index> (uvc://0 for the first). Paths: exposure and gain in the camera driver's
    own units (as OpenCV passes them), brightness, width and height. A frame is grey: its colours averaged, 0-255."""
    PROPS = {"exposure": "CAP_PROP_EXPOSURE", "gain": "CAP_PROP_GAIN", "brightness": "CAP_PROP_BRIGHTNESS",
             "width": "CAP_PROP_FRAME_WIDTH", "height": "CAP_PROP_FRAME_HEIGHT"}

    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        if module is None:
            try:
                import cv2 as module   # type: ignore
            except ImportError as e:
                raise AdapterError("%s: %s" % (name, envs.missing("opencv-python-headless"))) from e
        self.cv2 = module
        index, _ = _target(address)
        try:
            self.index = int(index or 0)
        except ValueError:
            raise AdapterError("%s: a USB camera's address is uvc://<index> (uvc://0 for the first)" % name)
        import sys
        api = getattr(module, "CAP_DSHOW", 0) if sys.platform.startswith("win") else getattr(module, "CAP_ANY", 0)
        self.cap = module.VideoCapture(self.index, api)
        if not self.cap.isOpened():
            raise AdapterError("%s: no USB camera %d, or another program is using it" % (name, self.index))

    def get_setting(self, path: str) -> Any:
        if path in self.PROPS:
            return float(self.cap.get(getattr(self.cv2, self.PROPS[path])))
        return super().get_setting(path)

    def set(self, path: str, value: Any) -> None:
        if path not in ("exposure", "gain", "brightness"):
            raise AdapterError("%s: %r can't be set" % (self.name, path))
        if path == "exposure" and hasattr(self.cv2, "CAP_PROP_AUTO_EXPOSURE"):
            self.cap.set(self.cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)   # manual exposure, so the value holds
        if not self.cap.set(getattr(self.cv2, self.PROPS[path]), float(value)):
            raise AdapterError("%s: the camera did not take %s %s" % (self.name, path, value))
        self._last = None

    def snap(self) -> Any:
        self.cap.grab()   # the frame waiting in the driver's buffer is an old one
        ok, image = self.cap.read()
        if not ok or image is None:
            raise AdapterError("%s: the camera gave no frame" % self.name)
        if getattr(image, "ndim", 2) == 3:
            image = self.cv2.cvtColor(image, self.cv2.COLOR_BGR2GRAY)
        return image

    def identity(self) -> str:
        return "USB camera,UVC,%d" % self.index

    def close(self) -> None:
        try:
            self.cap.release()
        except Exception:
            pass


# ------------------------------------------------------------------ GenICam cameras (Harvesters + the maker's GenTL)

class GenicamCamera(Camera):
    """Industrial cameras that follow GenICam (Teledyne FLIR, Allied Vision, IDS, The Imaging Source, Lucid...)
    through Harvesters, which Kelvin installs once a person approves, and the camera maker's GenTL producer: the .cti
    file its SDK installs. Address genicam://<serial>?cti=<that .cti file> (genicam:// for the first camera; without
    cti, the producers the makers' SDKs list in GENICAM_GENTL64_PATH). Paths: exposure (s), gain (dB), width, height."""

    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        if module is None:
            try:
                from harvesters.core import Harvester as module   # type: ignore
            except ImportError as e:
                raise AdapterError("%s: %s; it also needs the camera maker's GenTL producer (.cti), from its SDK"
                                   % (name, envs.missing("harvesters"))) from e
        serial, q = _target(address)
        producers = [q["cti"]] if q.get("cti") else self.producers()
        if not producers:
            raise AdapterError("%s: no GenTL producer: install the camera maker's SDK (it brings a .cti file), or "
                               "give one with ?cti=<path to the .cti file>" % name)
        self.h = module()
        try:
            for cti in producers:
                self.h.add_file(cti)
            self.h.update()
            infos = list(self.h.device_info_list)
            pick = next((i for i, d in enumerate(infos) if not serial or getattr(d, "serial_number", "") == serial),
                        None)
            if pick is None:
                raise AdapterError("%s: no GenICam camera %s (the producers see: %s)" % (
                    name, serial, ", ".join(str(getattr(d, "serial_number", "?")) for d in infos) or "none"))
            self.info = infos[pick]
            self.ia = self.h.create(pick)   # opens it; every setting stays as it is
        except AdapterError:
            self.h.reset()
            raise
        except Exception as e:
            self.h.reset()
            raise AdapterError("%s: could not open the camera: %s" % (name, str(e).splitlines()[0])) from e

    @staticmethod
    def producers() -> List[str]:
        """The GenTL producers the makers' SDKs installed (GENICAM_GENTL64_PATH or GENICAM_GENTL32_PATH)."""
        import os
        import struct
        out: List[str] = []
        var = "GENICAM_GENTL64_PATH" if struct.calcsize("P") == 8 else "GENICAM_GENTL32_PATH"
        for folder in filter(None, os.environ.get(var, "").split(os.pathsep)):
            if os.path.isdir(folder):
                out += sorted(os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(".cti"))
        return out

    def _node(self, *names: str) -> Any:
        nodes = self.ia.remote_device.node_map
        for n in names:
            node = getattr(nodes, n, None)
            if node is not None:
                return node
        raise AdapterError("%s: this camera has no %s" % (self.name, " or ".join(names)))

    def get_setting(self, path: str) -> Any:
        if path == "exposure":
            return float(self._node("ExposureTime", "ExposureTimeAbs").value) * 1e-6
        if path == "gain":
            return float(self._node("Gain", "GainRaw").value)
        if path in ("width", "height"):
            return int(self._node(path.capitalize()).value)
        return super().get_setting(path)

    def set(self, path: str, value: Any) -> None:
        if path == "exposure":
            self._node("ExposureTime", "ExposureTimeAbs").value = float(value) * 1e6
        elif path == "gain":
            self._node("Gain", "GainRaw").value = float(value)
        else:
            raise AdapterError("%s: %r can't be set" % (self.name, path))
        self._last = None

    def snap(self) -> Any:
        timeout = self.get_setting("exposure") + 5.0
        self.ia.start()
        try:
            with self.ia.fetch(timeout=timeout) as buffer:
                c = buffer.payload.components[0]
                return c.data.reshape(c.height, c.width).copy()
        finally:
            self.ia.stop()

    def identity(self) -> str:
        return "%s,%s,%s" % (getattr(self.info, "vendor", "") or "GenICam", getattr(self.info, "model", "") or "camera",
                             getattr(self.info, "serial_number", ""))

    def close(self) -> None:
        try:
            self.ia.destroy()
            self.h.reset()
        except Exception:
            pass
