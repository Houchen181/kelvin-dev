"""Instruments driven through their vendor's software instead of text commands over VISA.

A card for such an instrument still has command templates, in a small neutral language its driver translates:
    "SET <path> {value}"   and   "GET <path>"
so the safety gate (limits, ramps, approvals, raw-command checks), the twins and the run engine treat it exactly
like a SCPI instrument. The address names the driver:
    zhinst://dev3456                      Zurich Instruments (MFLI, MFIA, HF2LI, UHFLI...) through LabOne (zhinst-core)
    zhinst://dev3456?host=10.0.0.5&port=8004&interface=1GbE
    qd://192.168.1.20:5000                Quantum Design PPMS, DynaCool, VersaLab, MPMS3 through MultiPyVu

The vendor libraries are optional: `pip install zhinst-core` or `pip install MultiPyVu` on the computer that runs
Kelvin. Without them Kelvin says what to install."""
from __future__ import annotations

import math
import re
import threading
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from .adapters import AdapterError
from .cards import Card

SCHEMES = ("zhinst", "qd")
_COMMAND = re.compile(r"^\s*(SET|GET)\s+(\S+)(?:\s+(\S+))?\s*$", re.I)


def is_driver_address(address: str) -> bool:
    return address.split("://", 1)[0].lower() in SCHEMES and "://" in address


def parse_command(command: str) -> Tuple[str, str, Optional[str]]:
    m = _COMMAND.match(command or "")
    if not m:
        raise AdapterError("not a driver command: %r (expected SET <path> <value> or GET <path>)" % command)
    verb, path, value = m.group(1).upper(), m.group(2), m.group(3)
    if verb == "SET" and value is None:
        raise AdapterError("SET %s needs a value" % path)
    return verb, path, value


def _number(text: str) -> Any:
    low = text.strip().lower()
    if low in ("on", "true"):
        return 1
    if low in ("off", "false"):
        return 0
    v = float(text)
    return int(v) if v.is_integer() and "." not in text and "e" not in low else v


def _fmt(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    return "%.12g" % float(v)


class DriverBackend:
    """write(command) / query(command) / close(), like a VISA backend or a twin."""
    simulated = False

    def __init__(self, name: str, card: Card, address: str):
        self.name = name
        self.card = card
        self.address = address
        self.lock = threading.Lock()

    def write(self, command: str) -> None:
        verb, path, value = parse_command(command)
        if verb != "SET":
            raise AdapterError("%s: %r reads, it does not set" % (self.name, command))
        with self.lock:
            self.set(path.lower(), _number(value))   # cards write paths in capitals; vendors use lower case

    def query(self, command: str) -> str:
        if command.strip().upper() in ("*IDN?", self.card.idn_query.strip().upper()):
            with self.lock:
                return self.identity()
        verb, path, _ = parse_command(command)
        if verb != "GET":
            raise AdapterError("%s: %r sets, it does not read" % (self.name, command))
        with self.lock:
            return _fmt(self.get(path.lower()))

    def set(self, path: str, value: Any) -> None:
        raise NotImplementedError

    def get(self, path: str) -> Any:
        raise NotImplementedError

    def identity(self) -> str:
        raise NotImplementedError

    def close(self) -> None:
        pass


# ------------------------------------------------------------------ Zurich Instruments (LabOne)

class Zhinst(DriverBackend):
    """Paths are LabOne nodes below the device, e.g. /oscs/0/freq. A demodulator sample field is read as
    /demods/0/sample.x (x, y, r, theta in degrees, frequency)."""
    SAMPLE = re.compile(r"^(/demods/\d+/sample)\.(x|y|r|theta|frequency)$")

    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        url = urllib.parse.urlparse(address)
        self.device = (url.netloc or url.path).lower()
        if not re.match(r"^dev\d+$", self.device):
            raise AdapterError("%s: a Zurich Instruments address is zhinst://dev1234 (the device id), not %r"
                               % (name, address))
        q = dict(urllib.parse.parse_qsl(url.query))
        if module is None:
            try:
                import zhinst.core as module   # type: ignore
            except ImportError as e:
                raise AdapterError("%s needs the LabOne Python API: pip install zhinst-core" % name) from e
        try:
            self.daq = module.ziDAQServer(q.get("host", "localhost"), int(q.get("port", 8004)), 6)
            if q.get("interface", "1GbE"):
                self.daq.connectDevice(self.device, q.get("interface", "1GbE"))
        except Exception as e:
            raise AdapterError("%s: could not reach %s through the LabOne data server: %s" % (name, self.device, e)) from e

    def _node(self, path: str) -> str:
        if not path.startswith("/") or ".." in path:
            raise AdapterError("%s: %r is not a node path" % (self.name, path))
        return "/%s%s" % (self.device, path.lower())

    def set(self, path: str, value: Any) -> None:
        self.daq.set(self._node(path), value)

    def get(self, path: str) -> Any:
        m = self.SAMPLE.match(path)
        if m:
            node = self._node(m.group(1))
            sample = self.daq.getSample(node)
            first = lambda key: float(list(sample[key])[0])   # noqa: E731 - arrays of one value
            x, y = first("x"), first("y")
            return {"x": x, "y": y, "r": math.hypot(x, y), "theta": math.degrees(math.atan2(y, x)),
                    "frequency": first("frequency")}[m.group(2)]
        node = self._node(path)
        try:
            return self.daq.getDouble(node)
        except Exception:
            return self.daq.getInt(node)

    def identity(self) -> str:
        try:
            kind = self.daq.getString("/%s/features/devtype" % self.device)
        except Exception:
            kind = "?"
        return "Zurich Instruments,%s,%s" % (kind, self.device)


def discover_zhinst(module: Any = None) -> List[Tuple[str, str]]:
    """(address, identity) of every Zurich Instruments device LabOne can see. Only asks; sets nothing."""
    if module is None:
        try:
            import zhinst.core as module   # type: ignore
        except ImportError:
            return []
    found = []
    try:
        disc = module.ziDiscovery()
        for dev in disc.findAll():
            props = disc.get(dev)
            found.append(("zhinst://%s" % str(dev).lower(),
                          "Zurich Instruments,%s,%s" % (props.get("devicetype", "?"), str(dev).lower())))
    except Exception:
        return []
    return found


# ------------------------------------------------------------------ Quantum Design (MultiPyVu)

class MultiVu(DriverBackend):
    """Paths: temperature, temperature/setpoint, temperature/rate (K/min); field, field/setpoint, field/rate (T/s;
    MultiVu works in oersted, 1 T = 10 000 Oe); chamber. Setting a setpoint starts the approach at the rate
    set last (MultiVu itself ramps)."""
    OE_PER_T = 1e4

    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        url = urllib.parse.urlparse(address)
        self.host = url.hostname or "localhost"
        self.port = url.port or 5000
        if module is None:
            try:
                import MultiPyVu as module   # type: ignore
            except ImportError as e:
                raise AdapterError("%s needs Quantum Design's MultiPyVu: pip install MultiPyVu (and its server "
                                   "running next to MultiVu)" % name) from e
        try:
            self.client = module.Client(host=self.host, port=self.port)
            self.client.open()
        except Exception as e:
            raise AdapterError("%s: could not reach the MultiPyVu server at %s:%d: %s" % (name, self.host, self.port, e)) from e
        self.t_rate = 1.0          # K/min
        self.b_rate = 0.001        # T/s
        self.t_set: Optional[float] = None
        self.b_set: Optional[float] = None

    def set(self, path: str, value: Any) -> None:
        c = self.client
        if path == "temperature/rate":
            self.t_rate = float(value)
        elif path == "field/rate":
            self.b_rate = float(value)
        elif path == "temperature/setpoint":
            c.set_temperature(float(value), self.t_rate, c.temperature.approach_mode.fast_settle)
            self.t_set = float(value)
        elif path == "field/setpoint":
            c.set_field(float(value) * self.OE_PER_T, self.b_rate * self.OE_PER_T, c.field.approach_mode.linear)
            self.b_set = float(value)
        else:
            raise AdapterError("%s: MultiVu has no setting %r" % (self.name, path))

    def get(self, path: str) -> Any:
        c = self.client
        if path == "temperature":
            return float(c.get_temperature()[0])
        if path == "field":
            return float(c.get_field()[0]) / self.OE_PER_T
        if path == "temperature/setpoint":
            return self.t_set if self.t_set is not None else float(c.get_temperature()[0])
        if path == "field/setpoint":
            return self.b_set if self.b_set is not None else float(c.get_field()[0]) / self.OE_PER_T
        if path == "temperature/rate":
            return self.t_rate
        if path == "field/rate":
            return self.b_rate
        if path == "chamber":
            return str(c.get_chamber())
        raise AdapterError("%s: MultiVu has no reading %r" % (self.name, path))

    def identity(self) -> str:
        return "Quantum Design,MultiVu,%s:%d" % (self.host, self.port)

    def close(self) -> None:
        try:
            self.client.close_client()
        except Exception:
            pass


DRIVERS = {"zhinst": Zhinst, "qd": MultiVu}
MODULES: Dict[str, Any] = {}   # tests put fake vendor libraries here


def open_driver(name: str, card: Card, address: str) -> DriverBackend:
    scheme = address.split("://", 1)[0].lower()
    return DRIVERS[scheme](name, card, address, MODULES.get(scheme))
