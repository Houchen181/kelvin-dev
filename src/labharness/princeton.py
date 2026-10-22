"""Teledyne Princeton Instruments cameras (PyLoN, PIXIS, ProEM, BLAZE, KURO, SOPHIA) through PICam, Princeton's
camera library (Picam.dll, installed with PICam or LightField). Address picam://<serial> (picam:// for the first).

No Python package is needed: Kelvin calls the DLL directly (ctypes). Close LightField first: one program at a time.

Paths: exposure (s), temperature (sensor, C), setpoint (C), temperature_status (locked / unlocked / faulted),
adc_speed (MHz), adc_gain (low / medium / high), shutter (normal / closed / open), roi ("x,width,xbin,y,height,ybin"),
frame (saved as .npy, frames.py), spectrum (the frame's rows summed per column: with the ROI binned to one row on the
chip, exactly that row), and the frame's mean, max, min and sum.

Connecting opens the camera and sets nothing. PICam itself keeps a camera's parameters on the computer: when a program
opens it, they start from PICam's values, and the first acquisition sends them (LightField works the same way)."""
from __future__ import annotations

import ctypes
import os
import sys
import time
import urllib.parse
from typing import Any, List, Optional, Tuple

from . import frames
from .adapters import AdapterError
from .cameras import Camera
from .cards import Card


def _p(constraint: int, value_type: int, n: int) -> int:   # PICam's PI_V(value type, constraint, n)
    return (constraint << 24) + (value_type << 16) + n


INTEGER, FLOAT, ENUM, ROIS, LARGE = 1, 2, 4, 5, 6
NONE, RANGE, COLLECTION, ROIS_C = 1, 2, 3, 4
EXPOSURE = _p(RANGE, FLOAT, 23)                 # ms
TEMPERATURE = _p(NONE, FLOAT, 15)               # C, read from the camera
SETPOINT = _p(RANGE, FLOAT, 14)                 # C
TEMPERATURE_STATUS = _p(NONE, ENUM, 16)
ADC_SPEED = _p(COLLECTION, FLOAT, 33)           # MHz
ADC_GAIN = _p(COLLECTION, ENUM, 35)
SHUTTER = _p(COLLECTION, ENUM, 24)
READOUT_STRIDE = _p(NONE, INTEGER, 45)
PIXEL_BIT_DEPTH = _p(NONE, INTEGER, 48)
ROIS_PARAM = _p(ROIS_C, ROIS, 37)
STATUS = {1: "unlocked", 2: "locked", 3: "faulted"}
GAINS = {1: "low", 2: "medium", 3: "high"}
SHUTTERS = {1: "normal", 2: "closed", 3: "open"}
RUNTIME = r"Princeton Instruments\PICam\Runtime"
_users = [0]   # cameras open: PICam is initialized once for all of them (and a scan leaves it alone meanwhile)


class CameraID(ctypes.Structure):
    _fields_ = [("model", ctypes.c_int), ("computer_interface", ctypes.c_int),
                ("sensor_name", ctypes.c_char * 64), ("serial_number", ctypes.c_char * 64)]


class Roi(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("width", ctypes.c_int), ("x_binning", ctypes.c_int),
                ("y", ctypes.c_int), ("height", ctypes.c_int), ("y_binning", ctypes.c_int)]


class Rois(ctypes.Structure):
    _fields_ = [("roi_array", ctypes.POINTER(Roi)), ("roi_count", ctypes.c_int)]


class AvailableData(ctypes.Structure):
    _fields_ = [("initial_readout", ctypes.c_void_p), ("readout_count", ctypes.c_longlong)]


def load_picam() -> Any:
    """Picam.dll from PICam's runtime folder (or wherever Windows finds it), its functions declared."""
    if sys.platform != "win32" or not hasattr(ctypes, "WinDLL"):
        raise AdapterError("PICam (Princeton Instruments cameras) runs on Windows")
    folders = [os.path.join(os.environ.get(v, ""), RUNTIME) for v in ("ProgramFiles", "ProgramFiles(x86)")]
    for f in folders:
        if os.path.isdir(f):
            os.add_dll_directory(f)
    for name in [os.path.join(f, "Picam.dll") for f in folders] + ["Picam.dll"]:
        try:
            lib = ctypes.WinDLL(name)
            break
        except OSError:
            continue
    else:
        raise AdapterError("Princeton Instruments' PICam isn't installed here (Picam.dll): install PICam, or LightField")
    h, i, f, p = ctypes.c_void_p, ctypes.c_int, ctypes.c_double, ctypes.POINTER
    for fn, args in (("Picam_InitializeLibrary", []), ("Picam_UninitializeLibrary", []),
                     ("Picam_GetAvailableCameraIDs", [p(p(CameraID)), p(i)]), ("Picam_DestroyCameraIDs", [p(CameraID)]),
                     ("Picam_OpenCamera", [p(CameraID), p(h)]), ("Picam_OpenFirstCamera", [p(h)]),
                     ("Picam_CloseCamera", [h]), ("Picam_GetCameraID", [h, p(CameraID)]),
                     ("Picam_GetParameterFloatingPointValue", [h, i, p(f)]),
                     ("Picam_SetParameterFloatingPointValue", [h, i, f]),
                     ("Picam_ReadParameterFloatingPointValue", [h, i, p(f)]),
                     ("Picam_GetParameterIntegerValue", [h, i, p(i)]), ("Picam_SetParameterIntegerValue", [h, i, i]),
                     ("Picam_ReadParameterIntegerValue", [h, i, p(i)]),
                     ("Picam_GetParameterRoisValue", [h, i, p(p(Rois))]), ("Picam_SetParameterRoisValue", [h, i, p(Rois)]),
                     ("Picam_DestroyRois", [p(Rois)]), ("Picam_CommitParameters", [h, p(p(i)), p(i)]),
                     ("Picam_DestroyParameters", [p(i)]), ("Picam_AreParametersCommitted", [h, p(i)]),
                     ("Picam_Acquire", [h, ctypes.c_longlong, i, p(AvailableData), p(i)])):
        func = getattr(lib, fn)
        func.argtypes, func.restype = args, ctypes.c_int
    return lib


class PicamCamera(Camera):
    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        url = urllib.parse.urlparse(address)
        self.serial = (url.netloc or url.path).strip("/")
        self.lib = module if module is not None else load_picam()
        if not _users[0]:
            self._call("Picam_InitializeLibrary")
        _users[0] += 1
        self.handle = ctypes.c_void_p()
        try:
            if self.serial:
                ids, n = ctypes.POINTER(CameraID)(), ctypes.c_int()
                self._call("Picam_GetAvailableCameraIDs", ctypes.byref(ids), ctypes.byref(n))
                found = [ids[k] for k in range(n.value)]
                pick = next((c for c in found if c.serial_number.decode(errors="replace") == self.serial), None)
                if pick is None:
                    seen = ", ".join(c.serial_number.decode(errors="replace") for c in found) or "none"
                    self.lib.Picam_DestroyCameraIDs(ids)
                    raise AdapterError("%s: no Princeton Instruments camera %s (PICam sees: %s)" % (name, self.serial, seen))
                chosen = CameraID(pick.model, pick.computer_interface, pick.sensor_name, pick.serial_number)
                self.lib.Picam_DestroyCameraIDs(ids)
                self._call("Picam_OpenCamera", ctypes.byref(chosen), ctypes.byref(self.handle))
            else:
                self._call("Picam_OpenFirstCamera", ctypes.byref(self.handle))
        except AdapterError:
            self._release()
            raise

    def _call(self, fn: str, *args: Any) -> None:
        code = getattr(self.lib, fn)(*args)
        if code != 0:
            raise AdapterError("%s: PICam %s failed (error %s)" % (self.name, fn.replace("Picam_", ""), code))

    def _float(self, param: int, read: bool = False) -> float:
        v = ctypes.c_double()
        self._call("Picam_ReadParameterFloatingPointValue" if read else "Picam_GetParameterFloatingPointValue",
                   self.handle, param, ctypes.byref(v))
        return v.value

    def _int(self, param: int, read: bool = False) -> int:
        v = ctypes.c_int()
        self._call("Picam_ReadParameterIntegerValue" if read else "Picam_GetParameterIntegerValue",
                   self.handle, param, ctypes.byref(v))
        return v.value

    def _commit(self) -> None:
        failed, n = ctypes.POINTER(ctypes.c_int)(), ctypes.c_int()
        self._call("Picam_CommitParameters", self.handle, ctypes.byref(failed), ctypes.byref(n))
        if n.value:
            self.lib.Picam_DestroyParameters(failed)
            raise AdapterError("%s: the camera did not take %d setting(s)" % (self.name, n.value))

    def _roi(self) -> Roi:
        ptr = ctypes.POINTER(Rois)()
        self._call("Picam_GetParameterRoisValue", self.handle, ROIS_PARAM, ctypes.byref(ptr))
        try:
            rois = ptr.contents
            if rois.roi_count != 1:
                raise AdapterError("%s: %d regions of interest are set; Kelvin reads one" % (self.name, rois.roi_count))
            r = rois.roi_array[0]
            return Roi(r.x, r.width, r.x_binning, r.y, r.height, r.y_binning)
        finally:
            self.lib.Picam_DestroyRois(ptr)

    def get_setting(self, path: str) -> Any:
        if path == "exposure":
            return self._float(EXPOSURE) / 1000.0
        if path == "temperature":
            return self._float(TEMPERATURE, read=True)
        if path == "setpoint":
            return self._float(SETPOINT)
        if path == "temperature_status":
            return STATUS.get(self._int(TEMPERATURE_STATUS, read=True), "?")
        if path == "adc_speed":
            return self._float(ADC_SPEED)
        if path == "adc_gain":
            return GAINS.get(self._int(ADC_GAIN), "?")
        if path == "shutter":
            return SHUTTERS.get(self._int(SHUTTER), "?")
        if path == "roi":
            r = self._roi()
            return "%d,%d,%d,%d,%d,%d" % (r.x, r.width, r.x_binning, r.y, r.height, r.y_binning)
        if path == "spectrum":
            frame = self._frame(fresh=True)
            return [float(sum(col)) for col in zip(*frame)] if not hasattr(frame, "sum") else \
                [float(x) for x in frame.sum(axis=0)]
        return super().get_setting(path)

    def set(self, path: str, value: Any) -> None:
        if path == "exposure":
            self._call("Picam_SetParameterFloatingPointValue", self.handle, EXPOSURE, float(value) * 1000.0)
        elif path == "setpoint":
            self._call("Picam_SetParameterFloatingPointValue", self.handle, SETPOINT, float(value))
        elif path == "adc_speed":
            self._call("Picam_SetParameterFloatingPointValue", self.handle, ADC_SPEED, float(value))
        elif path in ("adc_gain", "shutter"):
            table = GAINS if path == "adc_gain" else SHUTTERS
            code = next((k for k, v in table.items() if v == str(value).lower()), None)
            if code is None:
                raise AdapterError("%s: %s is one of %s" % (self.name, path, ", ".join(table.values())))
            self._call("Picam_SetParameterIntegerValue", self.handle, ADC_GAIN if path == "adc_gain" else SHUTTER, code)
        elif path == "roi":
            try:
                x, w, xb, y, h, yb = [int(v) for v in str(value).split(",")]
            except ValueError:
                raise AdapterError("%s: roi is x,width,x_binning,y,height,y_binning (pixels)" % self.name)
            rois = Rois(ctypes.pointer(Roi(x, w, xb, y, h, yb)), 1)
            self._call("Picam_SetParameterRoisValue", self.handle, ROIS_PARAM, ctypes.byref(rois))
        else:
            raise AdapterError("%s: %r can't be set" % (self.name, path))
        self._commit()
        self._last = None

    def snap(self) -> Any:
        committed = ctypes.c_int()
        self._call("Picam_AreParametersCommitted", self.handle, ctypes.byref(committed))
        if not committed.value:
            self._commit()
        roi = self._roi()
        width, height = roi.width // max(1, roi.x_binning), roi.height // max(1, roi.y_binning)
        stride, depth = self._int(READOUT_STRIDE), self._int(PIXEL_BIT_DEPTH)
        timeout_ms = int(self._float(EXPOSURE)) + 60_000   # a slow CCD readout takes seconds
        data, errors = AvailableData(), ctypes.c_int()
        self._call("Picam_Acquire", self.handle, 1, timeout_ms, ctypes.byref(data), ctypes.byref(errors))
        if errors.value or not data.readout_count:
            raise AdapterError("%s: the acquisition failed (PICam error %d)" % (self.name, errors.value))
        raw = ctypes.string_at(data.initial_readout, stride)
        size = 2 if depth <= 16 else 4
        n = width * height
        try:
            import numpy as np   # in the app; without it, rows of numbers
            return np.frombuffer(raw[: n * size], dtype="<u%d" % size).reshape(height, width).copy()
        except ImportError:
            values = memoryview(raw[: n * size]).cast("H" if size == 2 else "I")
            return [list(values[r * width:(r + 1) * width]) for r in range(height)]

    def identity(self) -> str:
        cid = CameraID()
        try:
            self._call("Picam_GetCameraID", self.handle, ctypes.byref(cid))
            return "Princeton Instruments,%s,%s" % (cid.sensor_name.decode(errors="replace") or "camera",
                                                   cid.serial_number.decode(errors="replace"))
        except AdapterError:
            return "Princeton Instruments,camera,%s" % (self.serial or "?")

    def _release(self) -> None:
        _users[0] = max(0, _users[0] - 1)
        if not _users[0]:
            self.lib.Picam_UninitializeLibrary()

    def close(self) -> None:
        try:
            self.lib.Picam_CloseCamera(self.handle)
        finally:
            self._release()


def discover_picam(module: Any = None) -> List[Tuple[str, str]]:
    """(address, identity) of each camera PICam sees. Listing opens none; while Kelvin has a camera open, PICam is
    left alone (initializing it again would fail, uninitializing it would close that camera)."""
    if _users[0]:
        return []
    try:
        lib = module if module is not None else load_picam()
    except Exception:   # not installed here (or not Windows): nothing to list
        return []
    try:
        if lib.Picam_InitializeLibrary() != 0:
            return []
        ids, n = ctypes.POINTER(CameraID)(), ctypes.c_int()
        if lib.Picam_GetAvailableCameraIDs(ctypes.byref(ids), ctypes.byref(n)) != 0:
            return []
        found = [("picam://%s" % ids[k].serial_number.decode(errors="replace"), "Princeton Instruments,%s,%s"
                  % (ids[k].sensor_name.decode(errors="replace"), ids[k].serial_number.decode(errors="replace")))
                 for k in range(n.value)]
        lib.Picam_DestroyCameraIDs(ids)
        return found
    except Exception:
        return []
    finally:
        try:
            lib.Picam_UninitializeLibrary()
        except Exception:
            pass
