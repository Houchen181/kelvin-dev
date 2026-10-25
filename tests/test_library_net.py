"""Instruments whose maker gives a .NET library (Thorlabs Kinesis, many stages and cameras) or a COM object (older
makers' programs): Kelvin's helper loads them like a Python module or a DLL, calls only the maker's own members, reads
properties without calling, and passes values as the .NET methods want them (a position as a Decimal)."""
import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from labharness import EventLog, SafetyGate, ScriptedApprover
from labharness.cards import CardError, parse_card
from labharness.library import BRIDGE, describe
from labharness.virtual_lab import build_lab, lab_registry
from labharness.yamlio import load_yaml

STAGE_CS = r"""
namespace Acme.Stage
{
    public static class DeviceManager
    {
        public static int Built = 0;
        public static int BuildDeviceList() { Built += 1; return 1; }
    }

    public enum Mode { Open, Closed }

    public class Stage
    {
        private decimal position = 0m;
        public string Serial;
        public bool Connected { get; private set; }
        public decimal Velocity { get; set; }
        public Mode Loop { get; set; }
        public static Stage Create(string serial) { var s = new Stage(); s.Serial = serial; return s; }
        public void Connect(string serial)
        {
            if (DeviceManager.Built == 0) throw new System.InvalidOperationException("build the device list first");
            if (serial != Serial) throw new System.ArgumentException("wrong serial " + serial);
            Connected = true;
        }
        public void Disconnect() { Connected = false; }
        public decimal Position { get { return position; } }
        public void MoveTo(decimal target, int timeout)
        {
            if (!Connected) throw new System.InvalidOperationException("not connected");
            position = target;
        }
        public double GetTemperature() { return 21.5; }
    }
}
"""

CARD = """card: 1
id: acme/stage-net
model: ACME linear stage (.NET)
vendor: ACME
kind: positioner
status: lab-verified
library:
  assembly: Acme.Stage.dll
  type: Acme.Stage.Stage
  setup: [Acme.Stage.DeviceManager.BuildDeviceList]
  create: Create
  create_args: ["{address}"]
  properties: [Position, Connected, Velocity, Loop]
  open: ["Connect {address}"]
  close: [Disconnect]
parameters:
  x: { label: Position, kind: measure, unit: mm, set: "SET MoveTo {value} 60000", get: "GET Position", limits: [0, 25] }
  connected: { label: Connected, kind: measure, type: bool, get: "GET Connected" }
  velocity: { label: Velocity, kind: measure, unit: mm/s, set: "SET Velocity= {value}", get: "GET Velocity",
              limits: [0, 5] }
  loop: { label: Loop, kind: measure, type: enum, values: { open: Open, closed: Closed }, set: "SET Loop= {value}",
          get: "GET Loop" }
  temperature: { label: Motor temperature, kind: measure, unit: C, get: "GET GetTemperature" }
"""


def csc():
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    return exe if exe.is_file() else None


@pytest.fixture
def stage_dll(tmp_path):
    try:
        clr = importlib.util.find_spec("clr")
    except ImportError:
        clr = None
    if not sys.platform.startswith("win") or csc() is None or clr is None:
        pytest.skip("needs Windows' C# compiler and pythonnet")
    folder = tmp_path / "Acme"
    folder.mkdir()
    (folder / "AcmeStage.cs").write_text(STAGE_CS, encoding="utf-8")
    subprocess.run([str(csc()), "-nologo", "-target:library", "-out:" + str(folder / "Acme.Stage.dll"),
                    str(folder / "AcmeStage.cs")], check=True, capture_output=True)
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "stage.yaml").write_text(CARD, encoding="utf-8")
    return folder


def test_a_stage_from_its_makers_dotnet_library(stage_dll, tmp_path):
    lab = build_lab('lab: Optics\ninstruments:\n  stage: { card: acme/stage-net, address: "lib://27000001?path=%s" }\n'
                    % stage_dll.as_posix(), registry=lab_registry(tmp_path))
    try:
        assert lab.read("stage", "connected") is True                     # setup, the factory, Connect {address}
        assert lab.read("stage", "x") == pytest.approx(0)                   # a Decimal property, read
        assert lab.read("stage", "temperature") == pytest.approx(21.5)      # a method that reads
        gate = SafetyGate(lab, ScriptedApprover([]), EventLog(tmp_path / "events.jsonl", lab.clock))
        assert gate.set("stage", "x", 12.5).ok and lab.read("stage", "x") == pytest.approx(12.5)   # MoveTo(Decimal)
        assert gate.set("stage", "velocity", 2.5).ok and lab.read("stage", "velocity") == pytest.approx(2.5)
        assert gate.set("stage", "loop", "closed").ok and lab.read("stage", "loop") == "closed"   # an enum by name
    finally:
        lab.close()
    assert describe(str(stage_dll / "Acme.Stage.dll")).splitlines()[:2] == ["class Acme.Stage.DeviceManager",
                                                                          "    static BuildDeviceList() -> Int32"]
    said = describe(str(stage_dll / "Acme.Stage.dll"))
    assert "    MoveTo(Decimal target, Int32 timeout) -> Void" in said and "enum Acme.Stage.Mode: Open, Closed" in said
    assert "    Position  (property, Decimal)" in said and "    Velocity  (property, Decimal, settable)" in said


def bridge_module():
    spec = importlib.util.spec_from_file_location("kelvin_bridge_under_test", str(BRIDGE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_helper_keeps_dotnet_to_the_makers_own(stage_dll):
    from labharness.adapters import AdapterError
    from labharness.library import Bridge, bridge_command
    load = {"path": str(stage_dll), "assembly": "Acme.Stage.dll", "type": "Acme.Stage.Stage", "create": "Create",
            "create_args": ["27000001"]}
    helper = Bridge("stage", bridge_command())   # .NET loads in the helper, never in this process
    try:
        helper.request("load", 60, setup=["Acme.Stage.DeviceManager.BuildDeviceList"], **load)
        for bad in ("GetType", "MemberwiseClone"):
            with pytest.raises(AdapterError, match="reaches the"):
                helper.request("call", 10, name=bad, args=[])
        with pytest.raises(AdapterError, match="is a function, not a property"):
            helper.request("read", 10, name="GetTemperature")
    finally:
        helper.stop()
    for setup, why in ((["System.IO.File.Delete C:/x"], "not in the maker's assemblies"),
                       ([], "platform's own")):
        helper = Bridge("stage", bridge_command())
        try:
            with pytest.raises(AdapterError, match=why):
                helper.request("load", 60, setup=setup, **(load if setup else dict(load, assembly="System.Management.dll")))
        finally:
            helper.stop()


def test_com_objects_are_the_makers_programs_not_windows_own(monkeypatch):
    bridge = bridge_module()

    class Camera:   # what win32com's Dispatch gives for a maker's automation object
        __module__ = "win32com.client.dynamic"

        def __init__(self):
            self.Exposure = 0.1

        def Acquire(self, frames):
            return "acquired %s" % frames
    client = types.ModuleType("win32com.client")
    client.Dispatch = lambda progid: Camera()
    package = types.ModuleType("win32com")
    package.client = client
    monkeypatch.setitem(sys.modules, "win32com", package)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    servers = {"WinX32.ExpSetup": r"C:\Program Files\Princeton Instruments\WinSpec\WinX32.exe",
               "Fake.Shell": r"C:\Windows\System32\shell32.dll", "Fake.Office": r"C:\Program Files\Microsoft Office\x.dll"}
    monkeypatch.setattr(bridge, "_com_server", lambda progid: servers.get(progid, ""))
    lib = bridge.Library()
    assert lib.load({"com": "WinX32.ExpSetup"})["server"].endswith("WinX32.exe")
    assert lib.read("Exposure") == 0.1 and lib.assign("Exposure", "0.25") is None and lib.read("Exposure") == 0.25
    assert lib.call("Acquire", ["3"]) == "acquired 3"
    for progid, why in (("WScript.Shell", "not a maker's COM object"), ("Fake.Shell", "Windows' or Microsoft's own"),
                        ("Fake.Office", "Windows' or Microsoft's own"), ("Nobody.Here", "no COM object")):
        with pytest.raises(bridge.Refused, match=why):
            bridge.Library().load({"com": progid})


def test_dotnet_and_com_cards_are_checked():
    base = load_yaml(CARD)
    card = parse_card(base)
    assert card.library["type"] == "Acme.Stage.Stage" and "Position" in card.library["properties"]
    bad = dict(base, library=dict(base["library"], type="System.IO.File"))
    with pytest.raises(CardError, match="the maker's .NET class"):
        parse_card(bad)
    with pytest.raises(CardError, match="Velocity is a property: SET Velocity= "):
        parse_card(dict(base, parameters=dict(base["parameters"], velocity=dict(
            base["parameters"]["velocity"], set="SET Velocity {value}"))))
    with pytest.raises(CardError, match="only a property under library.properties is set with ="):
        parse_card(dict(base, parameters=dict(base["parameters"], x=dict(base["parameters"]["x"],
                                                                         set="SET MoveTo= {value}"))))
    com = {"card": 1, "id": "pi/winspec", "model": "WinSpec", "vendor": "Princeton", "kind": "spectrometer",
           "library": {"com": "WinX32.ExpSetup", "properties": ["Exposure"]},
           "parameters": {"exposure": {"kind": "measure", "unit": "s", "get": "GET Exposure",
                                       "set": "SET Exposure= {value}"}}}
    assert parse_card(com).library["com"] == "WinX32.ExpSetup"
    with pytest.raises(CardError, match="name one of module"):
        parse_card(dict(com, library={"com": "WinX32.ExpSetup", "module": "x"}))
