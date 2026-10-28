"""Teledyne Princeton Instruments cameras and spectrographs (PyLoN, PIXIS, ProEM... on an IsoPlane, SpectraPro or HRS)
through LightField, Princeton's own program. Kelvin starts LightField, a normal window people can watch and use, and
drives it through LightField's automation interface (.NET, reached with pythonnet, which Kelvin's Windows app brings).

    lightfield://<experiment>             the experiment saved in LightField to load (lightfield:// keeps the one
                                          LightField opens); ?visible=0 starts LightField without its window

LightField then has the camera: one program per camera, so a LightField someone opened by hand is closed first, and
Kelvin's PICam route (princeton.py) is not used for the same camera. Kelvin changes only what a person or a plan sets;
the experiment as saved in LightField stays as it was.

Paths: exposure (s), frames (captured per reading, averaged), grating (as LightField names it, [blaze,grooves][index]
[turret], or its grooves per mm), center_wavelength (nm), temperature and setpoint (C), temperature_status,
experiment, and the readings frame (an image), spectrum (the frame's rows summed for each column) with wavelengths
(nm, from LightField's calibration of the spectrograph), and the frame's mean, max, min and sum."""
from __future__ import annotations

import ctypes
import logging
import os
import re
import sys
import time
import urllib.parse
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

from . import envs
from .adapters import AdapterError
from .cameras import Camera
from .cards import Card

log = logging.getLogger("kelvin")
STEP_S = 0.2
MOVE_TIMEOUT_S = 120.0                # a grating turn, or a long move of the spectrograph
GRATING = re.compile(r"^\[([^,\]]*),\s*(\d+)\]")   # LightField's "[550nm,900][1][0]": blaze, grooves per mm
STATUS = {"locked": "locked", "unlocked": "unlocked", "faulted": "faulted"}


def _first(e: BaseException) -> str:
    text = str(e).strip()
    return text.splitlines()[0][:200] if text else type(e).__name__


def root() -> Optional[str]:
    """LightField's folder: LIGHTFIELD_ROOT (its installer sets it), else where it installs by default."""
    default = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Princeton Instruments", "LightField")
    for folder in (os.environ.get("LIGHTFIELD_ROOT", ""), default):
        if folder and os.path.isdir(folder):
            return folder
    return None


def note() -> str:
    """For the instrument scan: whether Kelvin could drive a camera through LightField here."""
    folder = root() if sys.platform == "win32" else None
    if folder is None:
        return ""
    return ("LightField: installed (%s). Kelvin drives the camera and spectrograph through it, not through its "
            "window: add_instrument with address lightfield://<experiment saved in LightField> and card "
            "princeton/lightfield" % folder)


def load_lightfield() -> Any:
    """LightField's automation classes, from LightField's own folder (LightField 5 or later)."""
    if sys.platform != "win32":
        raise AdapterError("LightField runs on Windows")
    folder = root()
    if folder is None:
        raise AdapterError("LightField isn't installed here (no LIGHTFIELD_ROOT, nor Princeton Instruments\\LightField "
                           "in Program Files)")
    try:
        import clr   # pythonnet
    except Exception as e:
        raise AdapterError("Kelvin can't reach .NET here, which LightField's automation needs (%s): %s"
                           % (_first(e), envs.missing("pythonnet")))
    for path in (folder, os.path.join(folder, "AddInViews")):
        if os.path.isdir(path) and path not in sys.path:
            sys.path.append(path)
    for version in ("V5", "V4"):
        try:
            clr.AddReference("PrincetonInstruments.LightFieldView" + version)
            clr.AddReference("PrincetonInstruments.LightField.Automation" + version)
            break
        except Exception:
            continue
    else:
        raise AdapterError("LightField's automation libraries aren't in %s "
                           "(PrincetonInstruments.LightField.AutomationV5 or V4)" % folder)
    try:
        clr.AddReference("PrincetonInstruments.LightFieldAddInSupportServices")
        from PrincetonInstruments.LightField.Automation import Automation   # type: ignore
        from PrincetonInstruments.LightField.AddIns import (CameraSettings, ExperimentSettings,   # type: ignore
                                                            SpectrometerSettings)
        from System import Int32, String   # type: ignore
        from System.Collections.Generic import List as NetList   # type: ignore
    except Exception as e:
        raise AdapterError("LightField's automation didn't load (%s)" % _first(e))
    return SimpleNamespace(Automation=Automation, CameraSettings=CameraSettings, ExperimentSettings=ExperimentSettings,
                           SpectrometerSettings=SpectrometerSettings, no_arguments=lambda: NetList[String](),
                           Int32=Int32)


def _to_numpy(data: Any, np: Any) -> Any:
    """A .NET array as numpy: its memory copied at once (pinned), else element by element."""
    try:
        from System.Runtime.InteropServices import GCHandle, GCHandleType   # type: ignore
        kind = {"UInt16": "<u2", "UInt32": "<u4", "Int32": "<i4", "Single": "<f4",
                "Double": "<f8"}[data.GetType().GetElementType().Name]
        count = int(data.Length)
        handle = GCHandle.Alloc(data, GCHandleType.Pinned)
        try:
            raw = ctypes.string_at(handle.AddrOfPinnedObject().ToInt64(), count * np.dtype(kind).itemsize)
        finally:
            handle.Free()
        return np.frombuffer(raw, dtype=kind).copy()
    except Exception:
        return np.asarray(list(data))


class LightFieldCamera(Camera):
    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        url = urllib.parse.urlparse(address)
        self.wanted = urllib.parse.unquote((url.netloc or url.path).strip("/"))
        query = dict(urllib.parse.parse_qsl(url.query))
        visible = query.get("visible", "1").lower() not in ("0", "false", "no", "off")
        self.api = module if module is not None else load_lightfield()
        self.region: Optional[Tuple[int, int, int]] = None   # the last frame's columns: x, width, binning
        self._warned = False
        log.info("%s: starting LightField%s", name, " (%s)" % self.wanted if self.wanted else "")
        try:
            self.auto = self.api.Automation(visible, self.api.no_arguments())
            self.experiment = self.auto.LightFieldApplication.Experiment
        except Exception as e:
            raise AdapterError("%s: LightField didn't start (%s)" % (name, _first(e)))
        if self.wanted:
            try:
                loaded = self.experiment.Load(self.wanted)
            except Exception as e:
                self.close()
                raise AdapterError("%s: LightField couldn't load the experiment %r (%s)" % (name, self.wanted, _first(e)))
            if not loaded:
                self.close()
                raise AdapterError("%s: LightField has no saved experiment %r: save it in LightField first, or check "
                                   "its name" % (name, self.wanted))

    # ------------------------------------------------------------------ LightField's settings

    def _exists(self, setting: Any) -> bool:
        try:
            return bool(self.experiment.Exists(setting))
        except Exception:
            return False

    def _frames_setting(self) -> Any:
        """The frames to store: AcquisitionFramesToStore in LightField 6 (as its Python samples set it),
        FrameSettingsFramesToStore in older LightFields."""
        es = self.api.ExperimentSettings
        for name in ("AcquisitionFramesToStore", "FrameSettingsFramesToStore"):
            setting = getattr(es, name, None)
            if setting is not None:
                return setting
        raise AdapterError("%s: this LightField has no setting for the frames to store" % self.name)

    def _get(self, setting: Any, what: str) -> Any:
        if not self._exists(setting):
            raise AdapterError("%s: the experiment has no %s (is the camera or spectrograph in it?)" % (self.name, what))
        try:
            return self.experiment.GetValue(setting)
        except Exception as e:
            raise AdapterError("%s: LightField didn't give the %s (%s)" % (self.name, what, _first(e)))

    def _set(self, setting: Any, value: Any, what: str) -> None:
        if not self._exists(setting):
            raise AdapterError("%s: the experiment has no %s (is the camera or spectrograph in it?)" % (self.name, what))
        if self._running():
            raise AdapterError("%s: LightField is acquiring (Run or Acquire in its window): stop it first" % self.name)
        try:
            valid = getattr(self.experiment, "IsValid", None)
            if valid is not None and not valid(setting, value):
                raise AdapterError("%s: LightField doesn't take %s %s for this camera or spectrograph" % (
                    self.name, what, value))
            self.experiment.SetValue(setting, value)
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError("%s: LightField didn't take the %s (%s)" % (self.name, what, _first(e)))

    def _options(self, setting: Any) -> List[str]:
        try:
            return [str(o) for o in self.experiment.GetCurrentCapabilities(setting)]
        except Exception:
            return []

    def _running(self) -> bool:
        try:
            return bool(self.experiment.IsRunning)
        except Exception:
            return False

    def _settle(self) -> None:
        """Wait while LightField moves the spectrograph (a grating or center wavelength just set)."""
        deadline = time.monotonic() + MOVE_TIMEOUT_S
        while True:
            try:
                moving = bool(self.experiment.IsUpdating)
            except Exception:
                return
            if not moving:
                return
            if time.monotonic() > deadline:
                raise AdapterError("%s: the spectrograph is still moving after %d s" % (self.name, MOVE_TIMEOUT_S))
            time.sleep(STEP_S)

    def _grating(self, value: Any) -> str:
        """A grating as LightField names it, from that name or from its grooves per mm."""
        text = str(value).strip()
        options = self._options(self.api.SpectrometerSettings.Grating)
        if not options or text in options:
            return text
        try:
            grooves = int(float(text))
        except ValueError:
            raise AdapterError("%s: grating is one of %s, or its grooves per mm" % (self.name, ", ".join(options)))
        hits = [o for o in options if GRATING.match(o) and int(GRATING.match(o).group(2)) == grooves]
        if len(hits) != 1:
            raise AdapterError("%s: %s grooves/mm %s: the spectrograph has %s" % (
                self.name, grooves, "fits more than one grating" if hits else "fits no grating", ", ".join(options)))
        return hits[0]

    def _frames(self) -> int:
        try:
            return max(1, min(10000, int(self._get(self._frames_setting(), "frames"))))
        except AdapterError:
            return 1

    # ------------------------------------------------------------------ Kelvin's paths

    def get_setting(self, path: str) -> Any:
        cs, ss = self.api.CameraSettings, self.api.SpectrometerSettings
        if path == "exposure":
            return float(self._get(cs.ShutterTimingExposureTime, "exposure time")) / 1000.0
        if path == "frames":
            return self._frames()
        if path == "center_wavelength":
            return float(self._get(ss.GratingCenterWavelength, "center wavelength"))
        if path == "grating":
            return str(self._get(ss.Grating, "grating"))
        if path == "temperature":
            return float(self._get(cs.SensorTemperatureReading, "sensor temperature"))
        if path == "setpoint":
            return float(self._get(cs.SensorTemperatureSetPoint, "temperature setpoint"))
        if path == "temperature_status":
            return STATUS.get(str(self._get(cs.SensorTemperatureStatus, "temperature status")).lower(), "?")
        if path == "experiment":
            return str(getattr(self.experiment, "Name", "") or self.wanted)
        if path == "spectrum":
            frame = self._frame(fresh=True)
            return [float(x) for x in frame.sum(axis=0)]
        if path == "wavelengths":
            return self.wavelengths()
        return super().get_setting(path)

    def set(self, path: str, value: Any) -> None:
        cs, es, ss = self.api.CameraSettings, self.api.ExperimentSettings, self.api.SpectrometerSettings
        if path == "exposure":
            self._set(cs.ShutterTimingExposureTime, float(value) * 1000.0, "exposure time")
        elif path == "frames":
            self._set(self._frames_setting(), getattr(self.api, "Int32", int)(int(value)), "frames")   # .NET's Int32
        elif path == "center_wavelength":
            self._set(ss.GratingCenterWavelength, float(value), "center wavelength")
            self._settle()
        elif path == "grating":
            self._set(ss.Grating, self._grating(value), "grating")
            self._settle()
        elif path == "setpoint":
            self._set(cs.SensorTemperatureSetPoint, float(value), "temperature setpoint")
        else:
            raise AdapterError("%s: %r can't be set" % (self.name, path))
        self._last = None

    def snap(self) -> Any:
        """Capture the experiment's frames (no file saved) and average them; the frame of its first region."""
        if self._running():
            raise AdapterError("%s: LightField is acquiring (Run or Acquire in its window): stop it first" % self.name)
        try:
            ready = bool(self.experiment.IsReadyToRun)
        except Exception:
            ready = True
        if not ready:
            raise AdapterError("%s: LightField isn't ready to acquire: is the camera connected, and in the experiment?"
                               % self.name)
        import numpy as np   # in the app
        count = self._frames()
        try:
            data = self.experiment.Capture(count)
        except Exception as e:
            raise AdapterError("%s: LightField's capture failed (%s)" % (self.name, _first(e)))
        if data is None:
            raise AdapterError("%s: LightField's capture returned no data" % self.name)
        try:
            first = data.Regions[0]
            self.region = (int(first.X), int(first.Width), max(1, int(first.XBinning)))
        except Exception:
            self.region = None
        stack = []
        for k in range(count):
            try:
                image = data.GetFrame(0, k)
                stack.append(_to_numpy(image.GetData(), np).reshape(int(image.Height), int(image.Width)))
            except Exception as e:
                raise AdapterError("%s: LightField's frame %d couldn't be read (%s)" % (self.name, k + 1, _first(e)))
        return np.mean(stack, axis=0) if len(stack) > 1 else stack[0]

    def wavelengths(self) -> List[float]:
        """The wavelength of each column of the latest frame (its region and binning), from LightField's calibration
        of the spectrograph; column numbers when the experiment has no calibrated spectrograph."""
        try:
            calibration = [float(x) for x in (self.experiment.SystemColumnCalibration or [])]
        except Exception:
            calibration = []
        x, width, binning = self.region or self._sensor_columns(len(calibration))
        columns = width // binning
        if not calibration:
            if not self._warned:
                log.warning("%s: LightField has no wavelength calibration: wavelengths are column numbers", self.name)
                self._warned = True
            return [float(k) for k in range(columns)]
        out = []
        for k in range(columns):
            part = calibration[x + k * binning:x + (k + 1) * binning]
            out.append(sum(part) / len(part) if part else float("nan"))
        return out

    def _sensor_columns(self, known: int) -> Tuple[int, int, int]:
        try:
            full = self.experiment.FullSensorRegion
            return int(full.X), int(full.Width), max(1, int(full.XBinning))
        except Exception:
            return 0, known, 1

    def identity(self) -> str:
        return "Teledyne Princeton Instruments,LightField,%s" % (self.get_setting("experiment") or "experiment")

    def close(self) -> None:
        auto = getattr(self, "auto", None)
        if auto is not None:
            try:
                auto.Dispose()   # closes the LightField that Kelvin started
            except Exception:
                pass
            self.auto = None
