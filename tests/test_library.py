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
from labharness.virtual_lab import build_lab, lab_registry
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
