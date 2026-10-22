"""Nanopositioners and scanners behind a vendor library (text-command controllers such as PI's GCS or Thorlabs'
MDT69x piezo drivers have plain cards instead).

    anc300://COM5            attocube ANC300 (USB or RS-232 serial port)          through pylablib (pip install pylablib)
    anc300://192.168.1.10    attocube ANC300 over the network (?pwd=<its password>; 123456 by default)
    anc350://0               attocube ANC350 closed-loop controller (USB, index)  through pylablib

The axis is the card's `axis` variable (ANC300: 1-7; ANC350: 0-2), so one lab-config entry per axis, as a stage.
Connecting changes no axis setting: pylablib only switches the ANC300's command echo off, which is its link, not an
axis. An open-loop ANC300 can't tell where an axis is, so `steps` counts the steps Kelvin made since it connected."""
from __future__ import annotations

import re
import threading
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from .adapters import AdapterError
from .cards import Card
from .drivers import DriverBackend

_PATH = re.compile(r"^(\w+?)(\d+)$")   # "offset1": the parameter, then the axis
MOVE_TIMEOUT_S = 60.0
_shared: Dict[str, Tuple[Any, int]] = {}   # one connection per controller, shared by its axes
_shared_lock = threading.Lock()


def _attocube(module: Any, name: str) -> Any:
    if module is not None:
        return module
    try:
        from pylablib.devices import Attocube   # type: ignore
        return Attocube
    except ImportError as e:
        raise AdapterError("%s needs pylablib: pip install pylablib (the ANC350 also needs attocube's USB driver)"
                           % name) from e


class _Controller(DriverBackend):
    """Paths are <parameter><axis>, e.g. offset1, so one controller serves the axes of several lab entries."""
    KIND = ""

    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        url = urllib.parse.urlparse(address)
        self.target = (url.netloc or url.path).strip("/")
        self.query_args = dict(urllib.parse.parse_qsl(url.query))
        self.lib = _attocube(module, name)
        key = "%s:%s" % (self.KIND, self.target.lower())
        with _shared_lock:
            dev, users = _shared.get(key, (None, 0))
            if dev is None:
                try:
                    dev = self.open()
                except AdapterError:
                    raise
                except Exception as e:
                    raise AdapterError("%s: could not open the %s at %s: %s" % (name, self.KIND, self.target or "0",
                                                                               e)) from e
            _shared[key] = (dev, users + 1)
        self.key, self.dev = key, dev
        self.steps: Dict[int, int] = {}

    def open(self) -> Any:
        raise NotImplementedError

    @staticmethod
    def split(path: str) -> Tuple[str, int]:
        m = _PATH.match(path)
        if not m:
            raise AdapterError("not an axis path: %r (e.g. offset1)" % path)
        return m.group(1), int(m.group(2))

    def call(self, what: str, fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError("%s: %s: %s" % (self.name, what, str(e).splitlines()[0] if str(e) else type(e).__name__)) from e

    def close(self) -> None:
        with _shared_lock:
            dev, users = _shared.get(self.key, (None, 0))
            if dev is None:
                return
            if users > 1:
                _shared[self.key] = (dev, users - 1)
                return
            _shared.pop(self.key, None)
        try:
            dev.close()
        except Exception:
            pass


class ANC300(_Controller):
    """Per axis: mode (gnd, stp, cap, offs, stp+, stp-, inp), voltage (step amplitude, V), frequency (Hz), offset (DC
    output for scanning, V), output (the voltage on the axis now, V), steps (steps made since Kelvin connected;
    setting it steps by the difference and waits for the axis to stop)."""
    KIND = "ANC300"

    def open(self) -> Any:
        if not self.target:
            raise AdapterError("%s: an ANC300 address is anc300://COM5 or anc300://<ip>" % self.name)
        pwd = self.query_args.get("pwd")
        return self.lib.ANC300(self.target, pwd=pwd) if pwd else self.lib.ANC300(self.target)

    def get(self, path: str) -> Any:
        what, axis = self.split(path)
        d = self.dev
        if what == "mode":
            return str(self.call(path, d.get_mode, axis))
        if what == "voltage":
            return float(self.call(path, d.get_voltage, axis))
        if what == "frequency":
            return float(self.call(path, d.get_frequency, axis) or 0.0)
        if what == "offset":
            return float(self.call(path, d.get_offset, axis))
        if what == "output":
            return float(self.call(path, d.get_output, axis))
        if what == "steps":
            return self.steps.get(axis, 0)
        raise AdapterError("%s: no %r" % (self.name, path))

    def set(self, path: str, value: Any) -> None:
        what, axis = self.split(path)
        d = self.dev
        if what == "mode":
            self.call(path, d.set_mode, axis, str(value))
        elif what == "voltage":
            self.call(path, d.set_voltage, axis, float(value))
        elif what == "frequency":
            self.call(path, d.set_frequency, axis, float(value))
        elif what == "offset":
            self.call(path, d.set_offset, axis, float(value))
        elif what == "steps":
            delta = int(value) - self.steps.get(axis, 0)
            if delta:
                self.call(path, d.move_by, axis, delta)
                self.call(path, d.wait_move, axis, MOVE_TIMEOUT_S)
                self.steps[axis] = int(value)
        else:
            raise AdapterError("%s: %r can't be set" % (self.name, path))

    def identity(self) -> str:
        try:
            info = self.dev.get_device_info()
            return "attocube,ANC300,%s" % info.serial
        except Exception:
            return "attocube,ANC300,%s" % self.target


class ANC350(_Controller):
    """Per axis (0-2): position (um; setting it moves there closed-loop and waits), voltage (step amplitude, V),
    frequency (Hz), offset (DC level, V), enabled (the axis output on/off), target_reached."""
    KIND = "ANC350"
    PRECISION_M = 1e-7

    def open(self) -> Any:
        try:
            idx = int(self.target or 0)
        except ValueError:
            raise AdapterError("%s: an ANC350 address is anc350://<USB index>, e.g. anc350://0" % self.name)
        return self.lib.ANC350(idx)

    def get(self, path: str) -> Any:
        what, axis = self.split(path)
        d = self.dev
        if what == "position":
            return float(self.call(path, d.get_position, axis)) * 1e6
        if what == "voltage":
            return float(self.call(path, d.get_voltage, axis))
        if what == "frequency":
            return float(self.call(path, d.get_frequency, axis))
        if what == "offset":
            return float(self.call(path, d.get_offset, axis))
        if what == "enabled":
            return 1 if self.call(path, d.is_enabled, axis) else 0
        if what == "target_reached":
            return 1 if self.call(path, d.is_target_reached, axis, self.PRECISION_M) else 0
        raise AdapterError("%s: no %r" % (self.name, path))

    def set(self, path: str, value: Any) -> None:
        what, axis = self.split(path)
        d = self.dev
        if what == "position":
            self.call(path, d.move_to, axis, float(value) * 1e-6)
            self.call(path, d.wait_move, axis, self.PRECISION_M, MOVE_TIMEOUT_S)
        elif what == "voltage":
            self.call(path, d.set_voltage, axis, float(value))
        elif what == "frequency":
            self.call(path, d.set_frequency, axis, float(value))
        elif what == "offset":
            self.call(path, d.set_offset, axis, float(value))
        elif what == "enabled":
            self.call(path, d.enable_axis, axis, bool(value))
        else:
            raise AdapterError("%s: %r can't be set" % (self.name, path))

    def identity(self) -> str:
        try:
            return "attocube,ANC350,%s" % self.dev.get_hardware_id()
        except Exception:
            return "attocube,ANC350,%s" % (self.target or "0")


def discover_attocube(module: Any = None) -> List[Tuple[str, str]]:
    """ANC350s on USB (counted by pylablib; none is opened). ANC300s are serial or network: add them by address."""
    try:
        lib = _attocube(module, "scan")
        return [("anc350://%d" % i, "attocube,ANC350,%d" % i) for i in range(int(lib.get_usb_devices_number_ANC350()))]
    except Exception:
        return []
