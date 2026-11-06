"""Teledyne Princeton Instruments cameras (PyLoN, PIXIS, ProEM, BLAZE, KURO, SOPHIA) through PICam, Princeton's
camera library (Picam.dll, installed with PICam or LightField). Address picam://<serial> (picam:// for the first).

No Python package is needed: Kelvin calls the DLL directly (ctypes), in a helper process of its own for each camera
(data/bridge/kelvin_picam.py, Kelvin's own Python), never in Kelvin's process. PICam can hang or crash, and a process
whose PICam couldn't start can keep PICam blocked for every program until that process ends (field report 2026-11-06:
another program's start waited 446 s, then failed with error 4). So each call has a time limit; a helper that misses
one, or whose PICam couldn't start, is ended at once, and connecting the camera again starts a fresh one. Close
LightField first: one program at a time (or drive the camera through LightField instead: lightfield.py).

Paths: exposure (s), temperature (sensor, C), setpoint (C), temperature_status (locked / unlocked / faulted),
adc_speed (MHz), adc_gain (low / medium / high), shutter (normal / closed / open), roi ("x,width,xbin,y,height,ybin"),
frame (saved as .npy, frames.py), spectrum (the frame's rows summed per column: with the ROI binned to one row on the
chip, exactly that row), and the frame's mean, max, min and sum.

Connecting opens the camera and sets nothing. PICam itself keeps a camera's parameters on the computer: when a program
opens it, they start from PICam's values, and the first acquisition sends them (LightField works the same way).

The helper (serve): Kelvin's import path on the first line, then one JSON request per line, {"id": 1, "op": "get",
"path": "exposure"}, each answered on a line of its own, {"id": 1, "ok": true, "value": 0.1} or {"id": 1, "ok": false,
"error": "...", "held": "camera"}. A frame's raw bytes follow its line ("bytes": how many; its value has the frame's
"shape" and "dtype"). Requests: ping, start, open (serial), get and set (path, value), snap, identity, list, close,
quit. Session is PICam's side, Helper Kelvin's end, PicamCamera the camera Kelvin's lab uses."""
from __future__ import annotations

import collections
import ctypes
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .adapters import AdapterError
from .cameras import Camera
from .cards import Card
from .envs import clean_environ

log = logging.getLogger("kelvin")


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
ERROR_TYPE, MODEL_TYPE = 1, 2      # PicamEnumeratedType_Error, _Model: PICam names its codes itself
# PICam's error codes as its header (picam.h) names them, with what they mean: for when PICam can't name one itself,
# as before it has started. Only codes checked against the header (PicamError).
ERRORS = {1: ("PicamError_LibraryNotInitialized", "PICam isn't started"),
          2: ("PicamError_InvalidParameterValue", "the camera doesn't take that value"),
          3: ("PicamError_UnexpectedNullPointer", "an unexpected error inside PICam"),
          4: ("PicamError_UnexpectedError", "an unexpected error inside PICam"),
          5: ("PicamError_LibraryAlreadyInitialized", "PICam is already started in this program"),
          7: ("PicamError_CameraAlreadyOpened", "the camera is already open"),
          8: ("PicamError_InvalidCameraID", "PICam doesn't know this camera"),
          9: ("PicamError_InvalidHandle", "PICam no longer knows this camera's connection"),
          10: ("PicamError_ParameterValueIsReadOnly", "that setting can only be read"),
          12: ("PicamError_ParameterDoesNotExist", "this camera has no such setting"),
          15: ("PicamError_DeviceCommunicationFailed", "the camera doesn't answer PICam"),
          20: ("PicamError_AcquisitionInProgress", "an acquisition is already running"),
          23: ("PicamError_DeviceDisconnected", "the camera is disconnected"),
          24: ("PicamError_DeviceOpenElsewhere", "the camera is open elsewhere"),
          29: ("PicamError_ParametersNotCommitted", "the settings weren't sent to the camera"),
          31: ("PicamError_InsufficientMemory", "not enough memory"),
          32: ("PicamError_TimeOutOccurred", "the acquisition timed out"),
          34: ("PicamError_NoCamerasAvailable", "no camera is free"),
          52: ("PicamError_ShutterOverheated", "the shutter is overheated"),
          53: ("PicamError_CameraFaulted", "the camera reports a fault")}
ACQUISITION_ERRORS = {1: "data were lost", 2: "the connection to the camera was lost"}
DOING = {"Picam_InitializeLibrary": "start", "Picam_GetAvailableCameraIDs": "list the cameras",
         "Picam_OpenCamera": "open the camera", "Picam_OpenFirstCamera": "open the camera",
         "Picam_Acquire": "acquire", "Picam_CommitParameters": "send the settings to the camera",
         "Picam_GetParameterRoisValue": "read the region of interest",
         "Picam_SetParameterRoisValue": "set the region of interest"}
SETTINGS = ("exposure", "temperature", "setpoint", "temperature_status", "adc_speed", "adc_gain", "shutter", "roi")
LAST = {"picam": ""}   # what the last scan saw through PICam, in words (drivers.library_notes)
_users = [0]   # cameras Kelvin has open, each through a helper of its own (a scan leaves PICam alone meanwhile)

HELPER = Path(__file__).parent / "data" / "bridge" / "kelvin_picam.py"
START_S = 60.0    # PICam starting: 6.8 s on a lab PC with a GigE PyLoN; behind a PICam left broken, 446 s, then error 4
OPEN_S = 30.0     # the camera opened
CALL_S = 10.0     # a setting read, the camera's identity
SET_S = 30.0      # a setting sent to the camera (committing it can take seconds)
SNAP_S = 90.0     # an acquisition, on top of its exposure: PICam's own limit is the exposure + 60 s
CLOSE_S = 4.0     # close(): the camera closed, PICam let go and the helper gone; else it is ended (KILL_S more)
KILL_S = 1.0      # waiting for an ended helper to go
ORPHAN_S = 15.0   # a helper whose Kelvin has gone (quit or crashed): closing the camera, else it exits anyway
MAX_LINE = 1 << 24
MAX_FRAME = 1 << 30
KELVIN_HOME = Path.home() / "Kelvin"   # app.json there names the Kelvin running (its port and process, desktop.py)
# Kelvin's processes that can hold instruments: the app (its helpers are Kelvin.exe too), and from source the launchers
# of its window, portal and MCP server (not the hub's, which holds none)
KELVINS = {"kelvin.exe", "kelvin", "kelvin-app.exe", "kelvin-app", "kelvin-mcp.exe", "kelvin-mcp"}


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


class PicamError(AdapterError):
    """A PICam call that failed, in words. held: "camera" when another program has the camera, "picam" when another
    program may be holding PICam itself; Kelvin's side then adds which program, when it can tell, and what helps
    (advice): the helper can't tell its own Kelvin from another."""

    def __init__(self, text: str, held: str = ""):
        super().__init__(text)
        self.held = held


class Gone(AdapterError):
    """PICam's helper has ended: it didn't answer in time (hung: Kelvin ended it), or it stopped by itself."""

    def __init__(self, text: str, hung: bool = False):
        super().__init__(text)
        self.hung = hung


def runtime_folders() -> List[str]:
    """Where Picam.dll may be: PICam's runtime folder, LightField's (which installs PICam), and PATH."""
    folders = [os.path.join(os.environ.get(v, ""), RUNTIME) for v in ("ProgramFiles", "ProgramFiles(x86)")]
    folders += [os.environ.get("LIGHTFIELD_ROOT", ""),
                os.path.join(os.environ.get("ProgramFiles", ""), "Princeton Instruments", "LightField")]
    folders += [p for p in os.environ.get("PATH", "").split(os.pathsep) if os.path.isfile(os.path.join(p, "Picam.dll"))]
    return [f for f in dict.fromkeys(folders) if f and os.path.isdir(f)]


def installed() -> bool:
    """Whether Picam.dll is on this computer, found without loading it: Kelvin's own process never loads it."""
    return sys.platform == "win32" and any(os.path.isfile(os.path.join(f, "Picam.dll")) for f in runtime_folders())


def load_picam() -> Any:
    """Picam.dll from PICam's runtime folder, LightField's, or a folder on PATH; its functions declared. Only PICam's
    helper process loads it."""
    if sys.platform != "win32" or not hasattr(ctypes, "WinDLL"):
        raise AdapterError("PICam (Princeton Instruments cameras) runs on Windows")
    folders = runtime_folders()
    for f in folders:
        os.add_dll_directory(f)
    for name in [os.path.join(f, "Picam.dll") for f in folders] + ["Picam.dll"]:
        try:
            lib = ctypes.WinDLL(name)
            break
        except OSError:
            continue
    else:
        raise AdapterError("Princeton Instruments' PICam isn't installed here (no Picam.dll in its runtime folder, "
                           "LightField's or on PATH): install PICam, or LightField")
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
                     ("Picam_GetParameterRoisValue", [h, i, p(p(Rois))]),
                     ("Picam_SetParameterRoisValue", [h, i, p(Rois)]), ("Picam_DestroyRois", [p(Rois)]),
                     ("Picam_CommitParameters", [h, p(p(i)), p(i)]),
                     ("Picam_DestroyParameters", [p(i)]), ("Picam_AreParametersCommitted", [h, p(i)]),
                     ("Picam_Acquire", [h, ctypes.c_longlong, i, p(AvailableData), p(i)]),
                     ("Picam_GetUnavailableCameraIDs", [p(p(CameraID)), p(i)]),
                     ("Picam_GetEnumerationString", [i, i, p(ctypes.c_char_p)]),
                     ("Picam_DestroyString", [ctypes.c_char_p])):
        try:
            func = getattr(lib, fn)
        except AttributeError:   # an older PICam: Kelvin does without (unavailable cameras, error names)
            continue
        func.argtypes, func.restype = args, ctypes.c_int
    return lib


def _named(lib: Any, kind: int, value: int) -> str:
    """PICam's own name for an error code or a camera model ("" if it has none)."""
    s = ctypes.c_char_p()
    try:
        if lib.Picam_GetEnumerationString(kind, value, ctypes.byref(s)) != 0 or not s.value:
            return ""
        text = s.value.decode(errors="replace")
        lib.Picam_DestroyString(s)
        return text
    except Exception:
        return ""


def failure(lib: Any, fn: str, code: int) -> PicamError:
    """A failed PICam call in words: what Kelvin was doing, the error (PICam's own name for it, else its header's in
    plain words: PICam can't name its errors before it has started), and what helps. A start that failed, and the
    camera open elsewhere, are held: Kelvin adds who has it (advice)."""
    named = _named(lib, ERROR_TYPE, code)
    if named:
        what, words = named, named
    elif code in ERRORS:
        what, words = "%s (%s, code %d)" % (ERRORS[code][1], ERRORS[code][0], code), ERRORS[code][0]
    else:
        what, words = "error %d" % code, ""
    flat = words.lower().replace(" ", "").replace("_", "")
    held, hint = "", ""
    if code == 24 or any(w in flat for w in ("elsewhere", "inuse", "alreadyopened", "unavailable")):
        held, hint = "camera", ". Another program has the camera"
    elif fn == "Picam_InitializeLibrary" or code == 4:
        held = "picam"
    elif "timeout" in flat:
        hint = ". The camera didn't deliver in time: check its cable and that it is cooled and connected"
    elif "disconnected" in flat or "communication" in flat or "notconnected" in flat:
        hint = ". Check the camera's cable and power"
    return PicamError("PICam couldn't %s: %s%s" % (DOING.get(fn, fn.replace("Picam_", "do ")), what, hint), held)


def explain(lib: Any, fn: str, code: int) -> str:
    """A failed PICam call in words (failure), without who has the camera."""
    return str(failure(lib, fn, code))


def _model(lib: Any, cid: Any) -> str:
    return _named(lib, MODEL_TYPE, cid.model) or cid.sensor_name.decode(errors="replace") or "camera"


# ------------------------------------------------------------------ who else has PICam (Kelvin's side)

def holder() -> Tuple[str, List[int]]:
    """Which other program may have PICam's cameras (PICam doesn't say which): ("kelvin", processes) for another
    Kelvin, a Kelvin process that isn't this one or one of its helpers (another Kelvin's helpers count as that Kelvin)
    or the Kelvin the Kelvin folder's app.json names; ("lightfield", []) for LightField running; ("", []) for
    neither."""
    try:
        import psutil   # in the app; a source checkout may be without it
    except ImportError:
        return "", []
    me = os.getpid()
    try:
        mine = {me} | {p.pid for p in psutil.Process(me).children(recursive=True)}
        running: Dict[int, Tuple[str, int]] = {}
        for p in psutil.process_iter(["pid", "name", "ppid"]):
            running[p.info["pid"]] = (str(p.info.get("name") or "").lower(), p.info.get("ppid") or 0)
    except psutil.Error:
        return "", []
    try:
        named = int(json.loads((KELVIN_HOME / "app.json").read_text(encoding="utf-8")).get("pid") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        named = 0
    kelvins: List[int] = []
    if named and named not in mine and any(w in running.get(named, ("", 0))[0] for w in ("kelvin", "python")):
        kelvins.append(named)   # a Kelvin run from source is a python process
    for pid, (name, ppid) in sorted(running.items()):
        if pid not in mine and pid not in kelvins and name in KELVINS and running.get(ppid, ("", 0))[0] not in KELVINS:
            kelvins.append(pid)
    if kelvins:
        return "kelvin", kelvins
    if any("lightfield" in name for name, _ in running.values()):
        return "lightfield", []
    return "", []


def advice(held: str) -> str:
    """What helps when another program has the camera (held "camera") or may be holding PICam itself ("picam"), with
    that program when Kelvin can tell (holder)."""
    kind, pids = holder()
    if kind == "kelvin":
        one = len(pids) == 1
        listed = ", ".join(str(p) for p in pids)
        tail = ". A Kelvin whose window is closed may still be running: end it in Task Manager"
        if held == "camera":
            return " (%s): quit %s, then connect the camera again%s" % (
                "another Kelvin, process %s" % listed if one else "other Kelvins, processes %s" % listed,
                "it" if one else "them", tail)
        return ". Another program may be holding PICam: %s; quit %s, then try again%s" % (
            "another Kelvin is running (process %s)" % listed if one else
            "other Kelvins are running (processes %s)" % listed, "it" if one else "them", tail)
    if kind == "lightfield":
        if held == "camera":
            return (" (LightField): close LightField for Kelvin to use it through PICam, or use it through LightField "
                    "(Teledyne LightField, lightfield://<experiment>)")
        return ". Another program may be holding PICam: LightField is running; close it, then try again"
    if held == "camera":
        return (" (LightField?): close it for Kelvin to use it through PICam, or use it through LightField (Teledyne "
                "LightField, lightfield://<experiment>)")
    return (". Another program may be holding PICam: quit other Kelvin or LightField instances, then try again. A "
            "Kelvin whose window is closed may still be running: end it in Task Manager")


def _words(e: BaseException, phase: str) -> str:
    """An error from PICam's side with what helps: who has the camera or may be holding PICam (advice), or how to
    start PICam afresh once its helper has ended. phase: "start" (PICam starting), "connect", "use" or "scan"."""
    held = getattr(e, "held", "")
    if held:
        return str(e) + advice(held)
    if not isinstance(e, Gone):
        return str(e)
    if e.hung and phase in ("start", "scan"):   # PICam itself didn't start in time: held elsewhere, most likely
        return str(e) + advice("picam")
    if phase == "scan":
        return str(e)
    return str(e) + (". Switch the camera off and on in the Lab panel" if phase == "use" else
                     ". Connect the camera again") + " to start PICam afresh" + (
        "; if it hangs again, quit other Kelvin or LightField instances first" if e.hung else "")


# ------------------------------------------------------------------ PICam's side, in the helper process

_DLL: List[Any] = []                       # Picam.dll, once loaded in this process (PICam's helper)
_FAILED: Dict[int, Tuple[Any, str]] = {}   # PICam modules that couldn't start in this process, and why


def _dll() -> Any:
    if not _DLL:
        _DLL.append(load_picam())
    return _DLL[0]


class Session:
    """PICam's side of one camera (or of one listing of the cameras): Kelvin's requests, made through Picam.dll in
    PICam's helper process. lib: a fake PICam module (tests), else Picam.dll."""

    def __init__(self, lib: Any = None):
        self.lib = lib
        self.started = self.opened = False
        self.serial = ""
        self.handle = ctypes.c_void_p()

    def answer(self, req: Dict[str, Any]) -> Tuple[Any, bytes]:
        """One request's answer: its value, and a frame's raw bytes (b"" for none)."""
        op = req.get("op")
        if op == "ping":
            return {"pid": os.getpid(), "python": sys.version.split()[0]}, b""
        if op == "start":
            self.start()
        elif op == "open":
            self.open(str(req.get("serial") or ""))
        elif op == "get":
            return self.get(str(req.get("path"))), b""
        elif op == "set":
            self.set(str(req.get("path")), req.get("value"))
        elif op == "snap":
            return self.snap()
        elif op == "identity":
            return self.identity(), b""
        elif op == "list":
            return self.cameras(), b""
        elif op in ("close", "quit"):
            self.close()
        else:
            raise PicamError("PICam's helper has no request %r" % op)
        return None, b""

    def start(self) -> None:
        """PICam started, once. A start that failed is let go at once (PICam uninitialized: a process left holding a
        failed start kept PICam blocked for every program) and isn't tried again in this process."""
        if self.started:
            return
        if self.lib is None:
            self.lib = _dll()
        failed = _FAILED.get(id(self.lib))
        if failed is not None:
            raise PicamError(failed[1] + " (not tried again in the same process)", "picam")
        code = self.lib.Picam_InitializeLibrary()
        if code != 0:
            e = failure(self.lib, "Picam_InitializeLibrary", code)
            try:
                self.lib.Picam_UninitializeLibrary()
            except Exception:
                pass
            _FAILED[id(self.lib)] = (self.lib, str(e))
            raise e
        self.started = True

    def _call(self, fn: str, *args: Any) -> None:
        code = getattr(self.lib, fn)(*args)
        if code != 0:
            raise failure(self.lib, fn, code)

    def _cameras(self, fn: str) -> List[Tuple[str, str, CameraID]]:
        """(serial, model, its ID) of each camera PICam lists: free to open (Picam_GetAvailableCameraIDs), or open in
        another program (Picam_GetUnavailableCameraIDs; an older PICam lists only the free ones)."""
        ids, n = ctypes.POINTER(CameraID)(), ctypes.c_int()
        try:
            listing = getattr(self.lib, fn)
        except AttributeError:
            return []
        code = listing(ctypes.byref(ids), ctypes.byref(n))
        if code != 0:
            if fn == "Picam_GetAvailableCameraIDs":
                raise failure(self.lib, fn, code)
            return []
        try:
            return [(c.serial_number.decode(errors="replace"), _model(self.lib, c),
                     CameraID(c.model, c.computer_interface, c.sensor_name, c.serial_number))
                    for c in (ids[k] for k in range(n.value))]
        finally:
            if ids:   # PICam's list, even an empty one, is PICam's to free
                self.lib.Picam_DestroyCameraIDs(ids)

    def open(self, serial: str) -> None:
        """The camera with this serial number opened (the first one free, for none). One PICam lists as open in
        another program says so, not "PICam sees: none"."""
        self.start()
        self.serial = serial
        if serial:
            free = self._cameras("Picam_GetAvailableCameraIDs")
            pick = next((c for c in free if c[0] == serial), None)
            if pick is None:
                held = next((c for c in self._cameras("Picam_GetUnavailableCameraIDs") if c[0] == serial), None)
                if held is not None:
                    raise PicamError("Princeton Instruments camera %s (%s) is open in another program"
                                     % (serial, held[1]), "camera")
                raise PicamError("no Princeton Instruments camera %s (PICam sees: %s)"
                                 % (serial, ", ".join(c[0] for c in free) or "none"))
            self._call("Picam_OpenCamera", ctypes.byref(pick[2]), ctypes.byref(self.handle))
        else:
            try:
                self._call("Picam_OpenFirstCamera", ctypes.byref(self.handle))
            except PicamError as e:
                held = [] if e.held else self._cameras("Picam_GetUnavailableCameraIDs")
                if held:
                    raise PicamError("PICam sees no free camera: %s %s open in another program" % (
                        ", ".join("%s (%s)" % (model, s) for s, model, _ in held),
                        "is" if len(held) == 1 else "are"), "camera") from e
                raise
        self.opened = True

    def _need_open(self) -> None:
        if not self.opened:
            raise PicamError("the camera isn't open")

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
            raise PicamError("the camera did not take %d setting(s)" % n.value)

    def _roi(self) -> Roi:
        ptr = ctypes.POINTER(Rois)()
        self._call("Picam_GetParameterRoisValue", self.handle, ROIS_PARAM, ctypes.byref(ptr))
        try:
            rois = ptr.contents
            if rois.roi_count != 1:
                raise PicamError("%d regions of interest are set; Kelvin reads one" % rois.roi_count)
            r = rois.roi_array[0]
            return Roi(r.x, r.width, r.x_binning, r.y, r.height, r.y_binning)
        finally:
            self.lib.Picam_DestroyRois(ptr)

    def get(self, path: str) -> Any:
        self._need_open()
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
        raise PicamError("no %r" % path)

    def set(self, path: str, value: Any) -> None:
        self._need_open()
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
                raise PicamError("%s is one of %s" % (path, ", ".join(table.values())))
            self._call("Picam_SetParameterIntegerValue", self.handle, ADC_GAIN if path == "adc_gain" else SHUTTER, code)
        elif path == "roi":
            try:
                x, w, xb, y, h, yb = [int(v) for v in str(value).split(",")]
            except ValueError:
                raise PicamError("roi is x,width,x_binning,y,height,y_binning (pixels)")
            rois = Rois(ctypes.pointer(Roi(x, w, xb, y, h, yb)), 1)
            self._call("Picam_SetParameterRoisValue", self.handle, ROIS_PARAM, ctypes.byref(rois))
        else:
            raise PicamError("%r can't be set" % path)
        self._commit()

    def snap(self) -> Tuple[Dict[str, Any], bytes]:
        """One frame: its pixels' raw bytes (little-endian), with its shape and dtype; Kelvin makes the array."""
        self._need_open()
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
            why = [w for bit, w in ACQUISITION_ERRORS.items() if errors.value & bit] or (
                ["PICam acquisition errors 0x%x" % errors.value] if errors.value else ["no frame came back"])
            raise PicamError("the acquisition failed: %s" % ", ".join(why))
        size = 2 if depth <= 16 else 4
        n = width * height * size
        raw = ctypes.string_at(data.initial_readout, stride)[:n]
        if len(raw) < n:
            raise PicamError("the frame came back short: %d of its %d bytes" % (len(raw), n))
        return {"shape": [height, width], "dtype": "<u%d" % size}, raw

    def identity(self) -> str:
        self._need_open()
        cid = CameraID()
        try:
            self._call("Picam_GetCameraID", self.handle, ctypes.byref(cid))
            return "Princeton Instruments,%s,%s" % (cid.sensor_name.decode(errors="replace") or "camera",
                                                   cid.serial_number.decode(errors="replace"))
        except PicamError:
            return "Princeton Instruments,camera,%s" % (self.serial or "?")

    def cameras(self) -> Dict[str, List[List[str]]]:
        """What PICam sees, as [serial, model]: the cameras free to open, and those open in another program."""
        self.start()
        return {"free": [[s, m] for s, m, _ in self._cameras("Picam_GetAvailableCameraIDs")],
                "held": [[s, m] for s, m, _ in self._cameras("Picam_GetUnavailableCameraIDs")]}

    def close(self) -> None:
        """The camera closed, then PICam let go (Picam_CloseCamera, then Picam_UninitializeLibrary)."""
        try:
            if self.opened:
                self.opened = False
                self.lib.Picam_CloseCamera(self.handle)
        finally:
            if self.started:
                self.started = False
                self.lib.Picam_UninitializeLibrary()


def _answer(session: Session, req: Dict[str, Any]) -> Tuple[Dict[str, Any], bytes]:
    """A request's reply, and the bytes that follow it (a frame's): PICam's errors go back as words."""
    rid = req.get("id")
    try:
        value, data = session.answer(req)
    except AdapterError as e:
        return {"id": rid, "ok": False, "error": str(e), "held": getattr(e, "held", "")}, b""
    except Exception as e:   # ctypes' or a bug's: as text, the helper goes on
        return {"id": rid, "ok": False, "error": "%s: %s" % (type(e).__name__, e)}, b""
    reply: Dict[str, Any] = {"id": rid, "ok": True, "value": value}
    if data:
        reply["bytes"] = len(data)
    return reply, data


def _line(reply: Dict[str, Any]) -> bytes:
    try:
        text = json.dumps(reply)
    except (TypeError, ValueError):
        text = json.dumps(dict(reply, value=str(reply.get("value"))))
    return (text + "\n").encode("utf-8")


def _listen(inp: Any, requests: "queue.Queue[Optional[Dict[str, Any]]]") -> None:
    """Kelvin's requests as they come. At the pipe's end (Kelvin quit or crashed) None, and the helper's last
    ORPHAN_S begin: a PICam call that won't end can't keep the helper, and PICam, past them."""
    try:
        for line in inp:
            try:
                req = json.loads(line.decode("utf-8") if isinstance(line, bytes) else line)
            except ValueError:
                continue
            if isinstance(req, dict):
                requests.put(req)
    except (OSError, ValueError):
        pass
    requests.put(None)
    timer = threading.Timer(ORPHAN_S, os._exit, args=(3,))
    timer.daemon = True
    timer.start()


def serve(inp: Any, out: Any, lib: Any = None) -> None:
    """PICam's helper: Kelvin's requests, one at a time, each answered on a line of its own (a frame's bytes after
    its line). It ends at close or quit; after a start that failed (that PICam is let go, and Kelvin starts a fresh
    helper to try again); or when Kelvin's end of the pipe closes, the camera closed and PICam let go first.
    lib: a fake PICam module (tests), else Picam.dll."""
    session = Session(lib)
    requests: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
    threading.Thread(target=_listen, args=(inp, requests), daemon=True).start()
    try:
        while True:
            req = requests.get()
            if req is None:
                return
            reply, data = _answer(session, req)
            try:
                out.write(_line(reply))
                if data:
                    out.write(data)
                out.flush()
            except (OSError, ValueError):   # Kelvin is gone
                return
            if req.get("op") in ("close", "quit") or (not reply["ok"] and req.get("op") in ("start", "list")):
                return
    finally:
        session.close()


# ------------------------------------------------------------------ Kelvin's end

def helper_command() -> List[str]:
    """How to start PICam's helper: with Kelvin's own Python (the app runs the script with --bridge, as its other
    helpers), the script getting this Kelvin's import path on its first line (as viworker.py's reader does)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--bridge", str(HELPER)]
    # a venv's python.exe only starts the real one as a second process: the real one, so ending it ends PICam
    python = getattr(sys, "_base_executable", "") or sys.executable
    return [python if Path(python).is_file() else sys.executable, "-E", "-s", "-u", str(HELPER)]


def _doing(op: str, fields: Dict[str, Any]) -> str:
    return {"ping": "starting", "start": "starting PICam", "open": "opening the camera",
            "get": "reading %s" % fields.get("path"), "set": "setting %s" % fields.get("path"),
            "snap": "taking a frame", "identity": "reading the camera's identity", "list": "listing the cameras",
            "close": "closing the camera"}.get(op, op)


def _value(reply: Dict[str, Any], data: bytes) -> Any:
    if not reply.get("ok"):
        raise PicamError(str(reply.get("error") or "PICam's helper gave no reason"), str(reply.get("held") or ""))
    value = reply.get("value")
    return dict(value, data=data) if data and isinstance(value, dict) else value


class Helper:
    """Kelvin's end of a PICam helper process: one request at a time, each answered within its time limit. A helper
    that misses one, or has stopped, is ended at once (PICam goes with it), so nothing in Kelvin holds PICam then."""

    def __init__(self, command: Optional[List[str]] = None):
        self.gone = ""   # why it has ended, once it has
        self.lock = threading.Lock()
        self.replies: "queue.Queue[Optional[Tuple[Dict[str, Any], bytes]]]" = queue.Queue()
        self.errors: "collections.deque[str]" = collections.deque(maxlen=40)
        self.next_id = 0
        env = clean_environ()   # PICam never sees Kelvin's API keys or sign-ins
        for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT"):
            env.pop(k, None)
        try:
            self.proc = subprocess.Popen(command or helper_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, env=env,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            raise AdapterError("Kelvin couldn't start PICam's helper (%s)" % e)
        threading.Thread(target=self._read, name="picam-helper", daemon=True).start()
        threading.Thread(target=self._drain, name="picam-helper-errors", daemon=True).start()
        try:
            self._send({"path": [str(p) for p in sys.path]})   # where it imports Kelvin's code from
        except (OSError, ValueError):
            pass   # it has stopped already: the first request says so

    def _send(self, message: Dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _read(self) -> None:
        """Its replies, each with a frame's bytes when it has one; None once it has stopped."""
        out = self.proc.stdout
        try:
            while out is not None:
                line = out.readline(MAX_LINE)
                if not line:
                    break
                try:
                    reply = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                if not isinstance(reply, dict):
                    continue
                n, data = reply.get("bytes"), b""
                if isinstance(n, int) and 0 < n <= MAX_FRAME:
                    data = out.read(n)
                    if len(data) < n:
                        break   # it stopped partway through a frame
                self.replies.put((reply, data))
        except (OSError, ValueError):
            pass
        finally:
            self.replies.put(None)

    def _drain(self) -> None:
        try:
            for line in self.proc.stderr or ():
                self.errors.append(line.decode("utf-8", "replace").rstrip())
        except (OSError, ValueError):
            pass

    def _said(self) -> str:
        last = [e for e in self.errors if e.strip()][-3:]
        return " (it said: %s)" % " | ".join(last)[:300] if last else ""

    def request(self, op: str, limit: float, **fields: Any) -> Any:
        """Its answer to one request (a frame's bytes as its value's "data"). PicamError for what PICam said; Gone
        when it doesn't answer within limit seconds, or has stopped: then it is ended at once."""
        doing = _doing(op, fields)
        with self.lock:
            if self.gone:
                raise Gone("not connected any more: Kelvin ended PICam's helper (%s)" % self.gone)
            self.next_id += 1
            rid = self.next_id
            try:
                self._send(dict(fields, id=rid, op=op))
            except (OSError, ValueError):
                self.end("it had stopped")
                raise Gone("PICam's helper had stopped%s: nothing in Kelvin holds PICam now" % self._said())
            deadline = time.monotonic() + limit
            while True:
                try:
                    item = self.replies.get(timeout=max(0.01, deadline - time.monotonic()))
                except queue.Empty:
                    self.end("it didn't answer within %g s, %s" % (limit, doing))
                    raise Gone("PICam didn't answer within %g s (%s): Kelvin ended its helper, so nothing in Kelvin "
                               "holds PICam now" % (limit, doing), hung=True)
                if item is None:
                    self.end("it stopped while %s" % doing)
                    raise Gone("PICam's helper stopped while %s%s: nothing in Kelvin holds PICam now"
                               % (doing, self._said()))
                if item[0].get("id") == rid:
                    return _value(*item)

    def end(self, why: str) -> None:
        """Ends it at once (Windows' TerminateProcess), and PICam with it."""
        if not self.gone:
            self.gone = why
            log.warning("PICam's helper (process %s) ended: %s", self.proc.pid, why)
        try:
            self.proc.kill()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=KILL_S)
        except subprocess.TimeoutExpired:
            pass
        self._close_input()

    def _close_input(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except (OSError, ValueError):
            pass

    def stop(self, limit: float = CLOSE_S) -> None:
        """The camera closed and PICam let go in the helper, which then exits; one that hasn't within limit seconds
        (it doesn't answer, or a call is still running, a long acquisition say) is ended. Returns within limit +
        KILL_S, whatever the helper does."""
        deadline = time.monotonic() + limit
        if not self.gone and self.lock.acquire(timeout=min(1.0, limit / 2)):
            try:
                self.next_id += 1
                rid = self.next_id
                self._send({"id": rid, "op": "close"})
                while True:
                    item = self.replies.get(timeout=max(0.01, deadline - time.monotonic()))
                    if item is None or item[0].get("id") == rid:
                        break
            except (OSError, ValueError, queue.Empty):
                pass
            finally:
                self.lock.release()
        try:
            self.proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        if self.proc.poll() is None:
            self.end("it didn't close within %g s" % limit)
        else:
            self.gone = self.gone or "closed"
            self._close_input()


class LocalHelper:
    """PICam's side in Kelvin's own process, for a fake PICam module (tests): the requests and replies a helper
    process gets and gives, each through JSON as across the pipe; no time limits, and nothing to end."""

    def __init__(self, lib: Any):
        self.session = Session(lib)
        self.gone = ""

    def request(self, op: str, limit: float, **fields: Any) -> Any:
        if self.gone:
            raise Gone("not connected any more: Kelvin ended PICam's helper (%s)" % self.gone)
        reply, data = _answer(self.session, json.loads(json.dumps(dict(fields, op=op))))
        return _value(json.loads(_line(reply).decode("utf-8")), data)

    def end(self, why: str) -> None:
        self.gone = self.gone or why
        self.session.close()

    def stop(self, limit: float = CLOSE_S) -> None:
        if not self.gone:
            self.end("closed")


def _array(got: Any) -> Any:
    """A frame from its raw bytes, shape and dtype: a NumPy array (in the app), else rows of numbers."""
    data, dtype = got.get("data") or b"", got.get("dtype")
    height, width = [int(v) for v in got.get("shape") or (0, 0)]
    if dtype not in ("<u2", "<u4") or len(data) != height * width * int(dtype[-1]):
        raise AdapterError("PICam's helper sent a frame Kelvin can't read (%s, %s, %d bytes)" % (
            got.get("shape"), dtype, len(data)))
    try:
        import numpy as np   # in the app; without it, rows of numbers
    except ImportError:
        values = memoryview(data).cast("H" if dtype == "<u2" else "I")
        return [list(values[r * width:(r + 1) * width]) for r in range(height)]
    return np.frombuffer(data, dtype=dtype).reshape(height, width).copy()


class PicamCamera(Camera):
    """A camera through PICam, in a helper process of its own (Helper): each call is forwarded there with a time
    limit. module: a fake PICam module (tests), run in Kelvin's own process instead (LocalHelper)."""
    close_at_quit = True   # Kelvin quitting closes the camera and lets PICam go: left open, PICam stayed unusable

    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        url = urllib.parse.urlparse(address)
        self.serial = (url.netloc or url.path).strip("/")
        self._counted = False
        self.helper: Any = None
        phase = "start"
        try:
            self.helper = LocalHelper(module) if module is not None else Helper()
            self.helper.request("start", START_S)
            phase = "connect"
            self.helper.request("open", OPEN_S, serial=self.serial)
        except AdapterError as e:
            _stop(self.helper)   # a PICam that couldn't start, or a camera that couldn't open: nothing kept
            raise AdapterError("%s: %s" % (name, _words(e, phase))) from e
        _users[0] += 1
        self._counted = True

    @property
    def quiet_close(self) -> bool:
        """Closing sends the camera nothing once PICam's helper has ended: switching the camera off then closes it,
        and on connects it with a fresh helper. While PICam has it open, a switched-off camera stays open, as other
        makers' drivers do (closing is PICam's, and could send it something)."""
        return bool(self.helper.gone)

    def _release(self) -> None:
        if self._counted:
            self._counted = False
            _users[0] = max(0, _users[0] - 1)

    def _ask(self, op: str, limit: float, **fields: Any) -> Any:
        try:
            return self.helper.request(op, limit, **fields)
        except AdapterError as e:
            if self.helper.gone:
                self._release()
            raise AdapterError("%s: %s" % (self.name, _words(e, "use"))) from e

    def get_setting(self, path: str) -> Any:
        if path in SETTINGS:
            return self._ask("get", CALL_S, path=path)
        if path == "spectrum":
            frame = self._frame(fresh=True)
            return [float(sum(col)) for col in zip(*frame)] if not hasattr(frame, "sum") else \
                [float(x) for x in frame.sum(axis=0)]
        return super().get_setting(path)

    def set(self, path: str, value: Any) -> None:
        self._ask("set", SET_S, path=path, value=value)
        self._last = None

    def snap(self) -> Any:
        exposure = float(self._ask("get", CALL_S, path="exposure"))
        got = self._ask("snap", exposure + SNAP_S)
        try:
            return _array(got if isinstance(got, dict) else {})
        except AdapterError as e:
            raise AdapterError("%s: %s" % (self.name, e)) from e

    def identity(self) -> str:
        return str(self._ask("identity", CALL_S))

    def close(self) -> None:
        """The camera closed and PICam let go in the helper, which then exits (ended if it hasn't within CLOSE_S).
        Returns within about 5 s, whatever the helper does: Kelvin closes every instrument when it quits."""
        _stop(self.helper)
        self._release()


def _stop(helper: Any) -> None:
    """A helper closed (Helper.stop), whatever it does: nothing for Kelvin to raise while connecting or closing."""
    try:
        if helper is not None:
            helper.stop(CLOSE_S)
    except Exception:
        pass


def discover_picam(module: Any = None) -> List[Tuple[str, str]]:
    """(address, identity) of each camera PICam sees, free to open, listed by a short-lived helper: Kelvin's own
    process never loads Picam.dll. Listing opens none; while Kelvin has a camera open, PICam is left alone. What it
    saw, in words, goes to LAST["picam"] for the scan's report: not installed, a camera another program has open..."""
    title = "PICam (Princeton Instruments cameras): "
    if _users[0]:
        LAST["picam"] = title + "in use by Kelvin (%d camera%s open)" % (_users[0], "" if _users[0] == 1 else "s")
        return []
    if module is None and not installed():   # nothing to list (or not Windows): no helper started
        LAST["picam"] = title + "not installed here" if sys.platform == "win32" else ""
        return []
    helper: Any = None
    try:
        helper = LocalHelper(module) if module is not None else Helper()
        seen = helper.request("list", START_S)
    except AdapterError as e:
        LAST["picam"] = title + _words(e, "scan")
        return []
    finally:
        _stop(helper)
    seen = seen if isinstance(seen, dict) else {}
    free, held = seen.get("free") or [], seen.get("held") or []
    parts = ["%s (%s) found" % (model, serial) for serial, model in free]
    if held:
        tail = advice("camera")
        parts += ["%s (%s) is open in another program%s" % (model, serial, tail) for serial, model in held]
    LAST["picam"] = title + ("; ".join(parts) if parts else "no camera connected")
    return [("picam://%s" % serial, "Princeton Instruments,%s,%s" % (model, serial)) for serial, model in free]


def self_test() -> bool:
    """For the release build's self-test: PICam's helper starts the way the app starts it (Kelvin.exe --bridge) and
    answers, without loading Picam.dll."""
    helper: Any = None
    try:
        helper = Helper()
        return bool((helper.request("ping", 30) or {}).get("pid"))
    except Exception:
        return False
    finally:
        _stop(helper)
