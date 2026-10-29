"""attocube nanopositioners and scanners (text-command controllers such as PI's GCS or Thorlabs' MDT69x piezo drivers
have plain cards instead).

    anc300://COM5            attocube ANC300 on its USB or RS-232 serial port: its command line, spoken directly
    anc300://192.168.1.10    attocube ANC300 over the network (TCP 7230; ?pwd=<its password>, 123456 by default)
    anc350://0               attocube ANC350 closed-loop controller (USB, index), through pylablib (not in the
                             app: Kelvin installs it once a person approves)

The axis is the card's `axis` variable (ANC300: 1-7; ANC350: 0-2), so one lab-config entry per axis, as a stage.
Connecting changes no axis setting: Kelvin only switches the ANC300's command echo off, which is its link, not an
axis. An open-loop ANC300 can't tell where an axis is, so `steps` counts the steps Kelvin made since it connected."""
from __future__ import annotations

import re
import socket
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from . import envs
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
        raise AdapterError("%s needs pylablib (the ANC350 also needs attocube's USB driver): %s"
                           % (name, envs.missing("pylablib-lightweight"))) from e


class _Controller(DriverBackend):
    """Paths are <parameter><axis>, e.g. offset1, so one controller serves the axes of several lab entries."""
    KIND = ""

    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        url = urllib.parse.urlparse(address)
        self.target = (url.netloc or url.path).strip("/")
        self.query_args = dict(urllib.parse.parse_qsl(url.query))
        self.module = module
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


class Link:
    """The ANC300's command line: a serial port (38400 baud, 8N1) or the network (TCP 7230, its password first).
    Each command is answered by lines that end with OK, or with ERROR."""

    def __init__(self, target: str, pwd: Optional[str] = None, timeout: float = 3.0):
        self.buf = b""
        self.timeout = timeout
        if re.match(r"^(COM\d+|/dev/)", target, re.I):
            import serial   # pyserial, part of Kelvin's hardware support
            self.port: Any = serial.Serial(target, 38400, bytesize=8, parity="N", stopbits=1, timeout=0.2)
            self.sock: Any = None
        else:
            host, _, port = target.partition(":")
            self.sock, self.port = socket.create_connection((host, int(port or 7230)), timeout=timeout), None
            self.sock.settimeout(0.2)
            self._send(pwd or "123456")
            self._until_ok()
        self._send("echo off")   # the link only: no axis changes
        self._until_ok()

    def _send(self, line: str) -> None:
        data = (line + "\r\n").encode("ascii")
        if self.port is not None:
            self.port.write(data)
        else:
            self.sock.sendall(data)

    def _chunk(self) -> bytes:
        try:
            return self.port.read(4096) if self.port is not None else self.sock.recv(4096)
        except socket.timeout:
            return b""

    def _until_ok(self, timeout: Optional[float] = None) -> List[str]:
        end, lines = time.monotonic() + (timeout or self.timeout), []
        while time.monotonic() < end:
            while b"\n" in self.buf:
                raw, self.buf = self.buf.split(b"\n", 1)
                line = re.sub(rb"\xff[\xfb-\xfe].", b"", raw).decode("ascii", "replace").strip()   # telnet options
                if line in ("OK", "ERROR"):
                    if line == "ERROR":
                        raise AdapterError(" ".join(lines) or "the ANC300 said ERROR")
                    return lines
                if line:
                    lines.append(line)
            self.buf += self._chunk()
        raise AdapterError("the ANC300 did not answer in %g s" % (timeout or self.timeout))

    def command(self, line: str, timeout: Optional[float] = None) -> List[str]:
        self.buf = b""
        self._send(line)
        return [x for x in self._until_ok(timeout) if x != line]   # an echo, should it still be on

    def close(self) -> None:
        if self.port is not None:
            self.port.close()
        else:
            self.sock.close()


def _value(lines: List[str]) -> str:
    """'voltage = 30.000000 V' -> '30.000000'; 'mode = stp' -> 'stp'."""
    for line in lines:
        if "=" in line:
            return line.split("=", 1)[1].strip().split()[0] if line.split("=", 1)[1].strip() else ""
    raise AdapterError("the ANC300 answered %r" % " / ".join(lines))


class ANC300(_Controller):
    """Per axis: mode (gnd, stp, cap, offs, stp+, stp-, inp), voltage (step amplitude, V), frequency (Hz), offset (DC
    output for scanning, V), output (the voltage on the axis now, V), steps (steps made since Kelvin connected;
    setting it steps by the difference and waits for the axis to stop)."""
    KIND = "ANC300"
    GET = {"mode": "getm", "voltage": "getv", "frequency": "getf", "offset": "geta", "output": "geto"}
    SET = {"mode": "setm", "voltage": "setv", "frequency": "setf", "offset": "seta"}

    def open(self) -> Any:
        if not self.target:
            raise AdapterError("%s: an ANC300 address is anc300://COM5 or anc300://<ip>" % self.name)
        pwd = self.query_args.get("pwd")
        return (self.module or Link)(self.target, pwd)

    def get(self, path: str) -> Any:
        what, axis = self.split(path)
        if what == "steps":
            return self.steps.get(axis, 0)
        if what not in self.GET:
            raise AdapterError("%s: no %r" % (self.name, path))
        text = _value(self.call(path, self.dev.command, "%s %d" % (self.GET[what], axis)))
        if what == "mode":
            return text
        try:
            return float(text)
        except ValueError:   # a frequency the controller reports as ?
            return 0.0

    def set(self, path: str, value: Any) -> None:
        what, axis = self.split(path)
        if what == "steps":
            delta = int(value) - self.steps.get(axis, 0)
            if delta:
                self.call(path, self.dev.command, "%s %d %d" % ("stepu" if delta > 0 else "stepd", axis, abs(delta)))
                self.call(path, self.dev.command, "stepw %d" % axis, MOVE_TIMEOUT_S)   # until the axis stops
                self.steps[axis] = int(value)
            return
        if what not in self.SET:
            raise AdapterError("%s: %r can't be set" % (self.name, path))
        self.call(path, self.dev.command, "%s %d %s" % (self.SET[what], axis, value if what == "mode" else
                                                        "%.6g" % float(value)))

    def identity(self) -> str:
        """Its answer to ver ("attocube ANC300 controller version 1.1.0-1304 ..."): the ANC300 has no *IDN?. What
        doesn't answer it isn't taken for an ANC300 (no identity is made up), so a wrong port fails the health check."""
        said = " ".join(self.dev.command("ver")).strip()
        if not said:
            raise AdapterError("%s did not say who it is (ver)" % self.name)
        try:
            serial = " ".join(self.dev.command("getcser")).strip()
        except AdapterError:   # not on every firmware
            serial = ""
        return "%s%s" % (said, ", serial %s" % serial if serial else "")


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
        return _attocube(self.module, self.name).ANC350(idx)

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
    """ANC350s on USB (counted by pylablib, where it is installed; none is opened). ANC300s are serial or network:
    add them by address."""
    try:
        lib = _attocube(module, "scan")
        return [("anc350://%d" % i, "attocube,ANC350,%d" % i) for i in range(int(lib.get_usb_devices_number_ANC350()))]
    except Exception:
        return []
