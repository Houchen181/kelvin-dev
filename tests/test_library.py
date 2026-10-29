"""Instruments driven through their maker's library (a Python module or a DLL), as in the maker's example scripts.
The library runs in Kelvin's helper process (data/bridge/kelvin_bridge.py), started here with this Python."""
import os
import sys
from pathlib import Path

import pytest

from labharness import EventLog, SafetyGate, ScriptedApprover, builtin_registry
from labharness.adapters import AdapterError
from labharness.cards import CardError, parse_card
from labharness.library import Bridge, bridge_command
from labharness.homelab import build_lab, lab_registry
from labharness.yamlio import load_yaml

VENDOR = '''
"""A maker's wrapper, written like the example scripts that come with a cryostat."""
state = {"T": 4.2, "Tset": 4.2, "control": 0, "port": None, "device": None}

def begin(device):
    state["device"] = device

def Connect(port):
    state["port"] = port

def Disconnect():
    pass

def end():
    pass

def getSampleTemperature():
    return state["T"] if not state["control"] else state["Tset"]

def getUserTemperature():
    return state["Tset"]

def setUserTemperature(t):
    if not 1.5 <= t <= 300:
        raise ValueError("set point out of range")
    state["Tset"] = t

def isControllingTemperature():
    return state["control"]

def toggleFullTemperatureControl():
    state["control"] = 1 - state["control"]

def getPort():
    return state["port"]

def startSampleExchange():
    raise RuntimeError("Kelvin must never call this")
'''

CARD = """
card: 1
id: acme/cryo
model: ACME cryostat (Python library)
vendor: ACME
kind: cryostat
status: lab-verified
vars:
  device: { default: "1", label: "model number" }
library:
  module: fakecryo
  open: ["begin {device}", "Connect {address}"]
  close: ["Disconnect", "end"]
  toggles:
    setControl: { toggle: toggleFullTemperatureControl, state: isControllingTemperature }
parameters:
  T:
    label: Temperature
    kind: measure
    unit: K
    get: "GET getSampleTemperature"
  control:
    label: Temperature control
    kind: drive
    type: bool
    set: "SET setControl {value}"
    get: "GET isControllingTemperature"
    safe: off
  T_set:
    label: Set point
    applies_when: control
    kind: drive
    unit: K
    set: "SET setUserTemperature {value}"
    get: "GET getUserTemperature"
    limits: [1.5, 300]
    safe: 1.5
  port:
    label: Port
    kind: measure
    type: str
    get: "GET getPort"
safe_sequence: [T_set, control]
"""


@pytest.fixture
def vendor(tmp_path):
    folder = tmp_path / "vendor"
    folder.mkdir()
    (folder / "fakecryo.py").write_text(VENDOR, encoding="utf-8")
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "cryo.yaml").write_text(CARD, encoding="utf-8")
    return folder


def test_a_cryostat_from_its_makers_python_library(vendor, tmp_path):
    lab = build_lab("""
lab: Cryostat
instruments:
  cryo: { card: acme/cryo, address: "lib://COM9?path=%s", vars: { device: "2" }, limits: { T_set: [1.5, 50] } }
""" % vendor.as_posix(), registry=lab_registry(tmp_path))
    inst = lab.instruments["cryo"]
    try:
        assert inst.backend.python["bits"] in (32, 64)
        assert lab.read("cryo", "port") == "COM9"                       # {address} in the open calls
        assert lab.read("cryo", "T") == pytest.approx(4.2) and lab.read("cryo", "control") is False
        gate = SafetyGate(lab, ScriptedApprover([True] * 3), EventLog(tmp_path / "events.jsonl", lab.clock))
        assert gate.set("cryo", "control", True).ok                     # asked; the state is read before toggling
        assert lab.read("cryo", "control") is True
        assert gate.set("cryo", "control", True).action == "no-change"  # already on: not toggled back off
        assert gate.set("cryo", "T_set", 100).action == "refused"       # the lab's 50 K
        assert gate.set("cryo", "T_set", 10).ok and lab.read("cryo", "T") == pytest.approx(10)
        with pytest.raises(AdapterError, match="not a reading on the card"):
            inst.backend.query("GET startSampleExchange")               # only the card's functions are called
        with pytest.raises(AdapterError, match="not on the card"):
            inst.backend.write("SET toggleFullTemperatureControl 1")    # the toggle itself only through the switch
        assert gate.raw("cryo", "SET setUserTemperature 20").ok         # raw, as the card's setter: approved
    finally:
        lab.close()


def test_the_helper_refuses_python_itself_and_system_dlls(tmp_path):
    b = Bridge("t", bridge_command())
    try:
        with pytest.raises(AdapterError, match="not a maker's module"):
            b.request("load", 30, path="", module="os")
        with pytest.raises(AdapterError, match="part of Python itself"):
            b.request("load", 30, path="", module="json")
        (tmp_path / "kernel32.dll").write_bytes(b"MZ")
        with pytest.raises(AdapterError, match="system library"):
            b.request("load", 30, path=str(tmp_path), dll="kernel32.dll", functions={})
        with pytest.raises(AdapterError, match="no ../x.dll"):
            b.request("load", 30, path=str(tmp_path), dll="../x.dll", functions={})
        with pytest.raises(AdapterError, match="nothing is loaded"):
            b.request("call", 30, name="system", args=["calc"])
    finally:
        b.stop()


def _c_math_library():
    if sys.platform == "win32":
        path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "msvcrt.dll"
    else:
        path = next((p for p in (Path("/lib/x86_64-linux-gnu/libm.so.6"), Path("/usr/lib/x86_64-linux-gnu/libm.so.6"),
                                 Path("/lib64/libm.so.6"), Path("/usr/lib64/libm.so.6")) if p.exists()), None)
    return path if path and path.exists() else None


def test_a_dll_is_called_with_the_cards_c_signatures(tmp_path):
    """A C library standing in for a maker's DLL: modf(double x, double *whole) returns the fraction."""
    lib = _c_math_library()
    if lib is None:
        pytest.skip("no C math library to stand in for a DLL")
    folder = tmp_path / "dll"
    folder.mkdir()
    name = "vendormath" + (".dll" if sys.platform == "win32" else ".so")
    (folder / name).write_bytes(lib.read_bytes())
    b = Bridge("thing", bridge_command())
    try:
        b.request("load", 30, path=str(folder), dll=name, returns="error",
                  functions={"modf": {"args": ["double"], "out": "double", "ret": "double"},
                             "fabs": {"args": ["double"], "ret": "double"}})
        assert b.request("call", 10, name="modf", args=["3.75"]) == 3.0      # through the pointer it declared
        assert b.request("call", 10, name="fabs", args=["-2.5"]) == 2.5      # a value it returns
        with pytest.raises(AdapterError, match="takes 1 arguments"):
            b.request("call", 10, name="fabs", args=[])
        with pytest.raises(AdapterError, match="not among the card's functions"):
            b.request("call", 10, name="system", args=["calc"])
    finally:
        b.stop()


ACS_DEVICE = '''
"""Written like attocube's network instruments' Python API: Device(address) speaks JSON-RPC over TCP; each subsystem
is a sub-object whose functions call "com.attocube.cryostat.interface.<subsystem>.<function>"."""
import json
import socket
from getpass import getuser            # a function of Python's own, imported as libraries often do

from .sample import Sample
from .vti import Vti
from .magnet import Magnet


class Device:
    def __init__(self, address):
        host, _, port = address.partition(":")
        self.address, self.port = host, int(port or 9090)
        self.sock = None
        self.sample, self.vti, self.magnet = Sample(self), Vti(self), Magnet(self)

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), timeout=5)
        self.reader = self.sock.makefile("r")

    def close(self):
        self.sock.close()

    def request(self, method, params=()):
        self.sock.sendall((json.dumps({"jsonrpc": "2.0", "method": method, "params": list(params), "id": 1}) + "\\n")
                          .encode())
        reply = json.loads(self.reader.readline())
        if reply["result"][0] != 0:
            raise RuntimeError("attoDRY error %d" % reply["result"][0])
        return reply["result"][1] if len(reply["result"]) > 1 else None
'''
ACS_PART = '''
class {cls}:
    def __init__(self, device):
        self.device = device
        self.interface_name = "com.attocube.cryostat.interface.{part}"

    def getTemperature(self):
        return self.device.request(self.interface_name + ".getTemperature")

    def getSetPoint(self):
        return self.device.request(self.interface_name + ".getSetPoint")

    def setSetPoint(self, value):
        return self.device.request(self.interface_name + ".setSetPoint", [value])

    def getField(self):
        return self.device.request(self.interface_name + ".getField")
'''
NETWORK_CARD = """
card: 1
id: acme/cryo-net
model: ACME network cryostat (Python library, sub-objects)
vendor: ACME
kind: cryostat
status: lab-verified
identify: { query: "GET system.getDeviceType", idn: "CRYO" }
library:
  module: atto_device.CRYO2100
  object: Device
  create: true
  create_args: ["{address}"]
  open: [connect]
  close: [close]
parameters:
  T: { label: Sample temperature, kind: measure, unit: K, get: "GET sample.getTemperature" }
  T_vti: { label: VTI temperature, kind: measure, unit: K, get: "GET vti.getTemperature" }
  B: { label: Field, kind: measure, unit: T, get: "GET magnet.getField" }
  T_set:
    label: Sample set point
    kind: drive
    unit: K
    set: "SET sample.setSetPoint {value}"
    get: "GET sample.getSetPoint"
    limits: [1.6, 300]
    safe: 1.6
"""


@pytest.fixture
def cryostat(tmp_path):
    """The cryostat's JSON-RPC server on this computer, and attocube-style library files for it."""
    import json
    import socketserver
    import threading
    state = {"sample.getTemperature": 4.21, "vti.getTemperature": 3.9, "magnet.getField": 0.5,
             "sample.getSetPoint": 4.2, "system.getDeviceType": "CRYO2100"}

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            for line in self.rfile:
                req = json.loads(line)
                method = req["method"].replace("com.attocube.cryostat.interface.", "")
                if method == "sample.setSetPoint":
                    state["sample.getSetPoint"] = state["sample.getTemperature"] = req["params"][0]
                    result = [0]
                else:
                    result = [0, state[method]] if method in state else [3]
                self.wfile.write((json.dumps({"jsonrpc": "2.0", "result": result, "id": req["id"]}) + "\n").encode())

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    package = tmp_path / "vendor" / "atto_device" / "CRYO2100"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text(ACS_DEVICE, encoding="utf-8")
    for part, cls in (("sample", "Sample"), ("vti", "Vti"), ("magnet", "Magnet")):
        (package / ("%s.py" % part)).write_text(ACS_PART.format(cls=cls, part=part), encoding="utf-8")
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "cryo-net.yaml").write_text(NETWORK_CARD, encoding="utf-8")
    yield tmp_path / "vendor", server.server_address[1], state
    server.shutdown()


def test_a_cryostat_whose_library_puts_each_part_on_a_sub_object(cryostat, tmp_path):
    """As attocube's network instruments: device.sample.getTemperature(), Device created with the address."""
    vendor, port, state = cryostat
    lab = build_lab("""
lab: Cryostat
instruments:
  cryo: { card: acme/cryo-net, address: "lib://127.0.0.1:%d?path=%s" }
""" % (port, vendor.as_posix()), registry=lab_registry(tmp_path))
    inst = lab.instruments["cryo"]
    try:
        assert lab.read("cryo", "T") == pytest.approx(4.21) and lab.read("cryo", "T_vti") == pytest.approx(3.9)
        assert lab.read("cryo", "B") == pytest.approx(0.5)
        gate = SafetyGate(lab, ScriptedApprover([True] * 3), EventLog(tmp_path / "events.jsonl", lab.clock))
        assert gate.set("cryo", "T_set", 10).ok and state["sample.getSetPoint"] == 10   # over JSON-RPC, read back
        bridge = inst.backend.bridge
        with pytest.raises(AdapterError, match="sock is not part of the maker's library"):
            bridge.request("call", 10, name="sock.close", args=[])          # Python's own socket object
        assert bridge.request("call", 10, name="sample.device.vti.getTemperature", args=[]) == pytest.approx(3.9)
    finally:
        lab.close()
    b = Bridge("t", bridge_command())
    try:   # the module itself, without the object: its imports are not the maker's
        b.request("load", 30, path=str(vendor), module="atto_device.CRYO2100")
        with pytest.raises(AdapterError, match="json is not part of the maker's library"):
            b.request("call", 10, name="json.dumps", args=["x"])
        with pytest.raises(AdapterError, match="getuser is not one of the maker's functions"):
            b.request("call", 10, name="getuser", args=[])                  # imported from Python itself
    finally:
        b.stop()


def test_library_cards_are_checked():
    base = load_yaml(CARD)
    bad = dict(base, library=dict(base["library"], module="subprocess"))
    with pytest.raises(CardError, match="not a maker's module"):
        parse_card(bad, "t")
    params = dict(base["parameters"], exchange={"label": "x", "kind": "measure", "get": "GET startSampleExchange"})
    with pytest.raises(CardError, match="only reads"):
        parse_card(dict(base, parameters=params), "t")
    with pytest.raises(CardError, match="library.open"):
        parse_card(dict(base, on_connect=["begin 1"]), "t")
    dll = dict(base, library={"dll": "x.dll", "functions": {"getA": {"out": "float"}}, "open": ["begin 1"]})
    with pytest.raises(CardError, match="begin is not under library.functions"):
        parse_card(dll, "t")
    card = builtin_registry().get("attocube/attodry")
    assert card.status == "draft" and card.library["open"][0] == "begin 1"
    assert card.library["toggles"]["setFieldControl"]["state"] == "isControllingField"
