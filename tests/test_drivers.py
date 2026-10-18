"""Instruments behind vendor software (Zurich Instruments LabOne, Quantum Design MultiVu), card variables for
per-installation names, and instruments that answer every set command (Oxford Mercury)."""
import math

import pytest

from labharness import EventLog, SafetyGate, ScriptedApprover
from labharness.adapters import AdapterError, VisaBackend, set_resource_manager
from labharness.cards import CardError
from labharness.discovery import draft_from, scan
from labharness.drivers import MODULES, open_driver, parse_command
from labharness.labconfig import ConfigError, load_lab_config
from labharness.virtual_lab import build_lab, builtin_registry
from labharness.clock import SimClock
from labharness.sim.twin import World, make_twin_backend
from test_hardware import Bus, Device


# ------------------------------------------------------------------ fake vendor libraries

class FakeDAQ:
    def __init__(self, host, port, api):
        self.where = (host, port, api)
        self.nodes = {"/dev5678/oscs/0/freq": 1000.0, "/dev5678/sigouts/0/on": 0, "/dev5678/sigouts/0/amplitudes/1": 0.0,
                      "/dev5678/demods/0/timeconstant": 0.1, "/dev5678/demods/1/timeconstant": 0.3}
        self.connected = None
        self.samples = {"/dev5678/demods/0/sample": (3e-6, 4e-6), "/dev5678/demods/1/sample": (-1e-6, 0.0)}

    def connectDevice(self, dev, interface):
        self.connected = (dev, interface)

    def set(self, path, value):
        self.nodes[path] = value

    def getDouble(self, path):
        if path not in self.nodes or isinstance(self.nodes[path], int):
            raise RuntimeError("ZIAPINotFoundException")
        return float(self.nodes[path])

    def getInt(self, path):
        return int(self.nodes[path])

    def getSample(self, path):
        x, y = self.samples[path]
        return {"x": [x], "y": [y], "frequency": [1000.0], "timestamp": [1]}

    def getString(self, path):
        assert path == "/dev5678/features/devtype"
        return "MFLI"


class Discovery:
    def findAll(self):
        return ["DEV5678"]

    def get(self, dev):
        return {"devicetype": "MFLI"}


class Zhinst:
    daq = None

    @classmethod
    def ziDAQServer(cls, host, port, api):
        cls.daq = FakeDAQ(host, port, api)
        return cls.daq

    ziDiscovery = Discovery


class Mode:
    fast_settle, linear = "fast settle", "linear"


class Client:
    last = None

    def __init__(self, host, port):
        self.where = (host, port)
        self.T, self.oe = 300.0, 0.0
        self.calls = []
        self.temperature = type("T", (), {"approach_mode": Mode})
        self.field = type("B", (), {"approach_mode": Mode})
        Client.last = self

    def open(self):
        self.calls.append("open")

    def get_temperature(self):
        return self.T, "Stable"

    def set_temperature(self, sp, rate, mode):
        self.calls.append(("T", sp, rate, mode))
        self.T = sp            # it gets there at once

    def get_field(self):
        return self.oe, "Holding (driven)"

    def set_field(self, sp, rate, mode):
        self.calls.append(("B", sp, rate, mode))
        self.oe = sp

    def get_chamber(self):
        return "purged and sealed"

    def close_client(self):
        self.calls.append("closed")


class MultiPyVu:
    Client = Client


@pytest.fixture
def vendors():
    MODULES.update(zhinst=Zhinst, qd=MultiPyVu)
    yield
    MODULES.clear()


def gate_for(lab, tmp_path, answers=()):
    return SafetyGate(lab, ScriptedApprover(answers), EventLog(tmp_path / "events.jsonl", lab.clock))


# ------------------------------------------------------------------ the command language

def test_driver_commands():
    assert parse_command("SET /OSCS/0/FREQ 1000") == ("SET", "/OSCS/0/FREQ", "1000")
    assert parse_command(" get TEMPERATURE ") == ("GET", "TEMPERATURE", None)
    for bad in ("FREQ 1000", "SET /OSCS/0/FREQ", "", "SET a b c"):
        with pytest.raises(AdapterError):
            parse_command(bad)


# ------------------------------------------------------------------ Zurich Instruments

LOCKIN = """
lab: Optics
instruments:
  li: { card: zhinst/mfli, address: "zhinst://DEV5678?host=10.0.0.5&interface=USB", limits: { amplitude: [0, 0.5] },
        vars: { demod: "1" } }
"""


def test_zurich_instruments_through_labone(vendors, tmp_path):
    lab = build_lab(LOCKIN)
    daq = Zhinst.daq
    assert daq.where == ("10.0.0.5", 8004, 6) and daq.connected == ("dev5678", "USB")
    li = lab.instruments["li"]
    assert not li.simulated and li.card.bound == {"demod": "1", "osc": "0", "out": "1"}
    assert lab.read("li", "time_constant") == 0.3                    # demodulator 1, as the lab config says
    assert lab.read("li", "X") == pytest.approx(-1e-6) and lab.read("li", "theta") == pytest.approx(180)
    assert li.backend.query("*IDN?") == "Zurich Instruments,MFLI,dev5678"
    gate = gate_for(lab, tmp_path, [True, True])
    assert gate.set("li", "frequency", 2000).ok and daq.nodes["/dev5678/oscs/0/freq"] == 2000
    assert gate.set("li", "output", True).ok and daq.nodes["/dev5678/sigouts/0/on"] == 1
    r = gate.set("li", "amplitude", 0.05)
    assert r.ok and daq.nodes["/dev5678/sigouts/0/amplitudes/1"] == pytest.approx(0.05)
    assert gate.set("li", "amplitude", 0.8).action == "refused"           # the lab's limit, in the card's terms


def test_zurich_samples_and_problems(vendors):
    card = builtin_registry().get("zhinst/mfli")
    be = open_driver("li", card, "zhinst://dev5678")
    assert be.query("GET /DEMODS/0/SAMPLE.R") == "%.12g" % 5e-6
    assert float(be.query("GET /DEMODS/0/SAMPLE.THETA")) == pytest.approx(math.degrees(math.atan2(4, 3)))
    assert be.query("GET /SIGOUTS/0/ON") == "0"                           # an integer node
    with pytest.raises(AdapterError, match="reads, it does not set"):
        be.write("GET /OSCS/0/FREQ")
    with pytest.raises(AdapterError, match="not a node path"):
        be.query("GET ../secret")
    with pytest.raises(AdapterError, match="zhinst://dev1234"):
        open_driver("li", card, "zhinst://localhost")
    MODULES.clear()
    try:
        import zhinst.core  # noqa: F401
    except ImportError:
        with pytest.raises(AdapterError, match="pip install zhinst-core"):
            open_driver("li", card, "zhinst://dev5678")


def test_scan_finds_labone_devices(vendors):
    set_resource_manager(Bus({}))
    try:
        found = scan(builtin_registry())
    finally:
        set_resource_manager(None)
    assert [(f.address, f.card_id, f.how) for f in found] == [("zhinst://dev5678", "zhinst/mfli", "found by its maker's library")]
    text = draft_from([{"name": "li", "card": "zhinst/mfli", "address": "zhinst://dev5678"}], builtin_registry())
    assert 'vars: { demod: 0, osc: 0, out: 1 }' in text and load_lab_config(text, builtin_registry())


# ------------------------------------------------------------------ Quantum Design

PPMS = """
lab: PPMS
instruments:
  ppms: { card: qd/multivu, address: "qd://192.168.1.20", limits: { B_set: [-9, 9], T_set: [1.8, 300] } }
"""


def test_quantum_design_through_multivu(vendors, tmp_path):
    lab = build_lab(PPMS)
    client = Client.last
    assert client.where == ("192.168.1.20", 5000) and client.calls == ["open"]
    assert lab.read("ppms", "T") == 300 and lab.read("ppms", "chamber") == "purged and sealed"
    client.oe = 25000.0
    assert lab.read("ppms", "B") == 2.5                                  # oersted to tesla
    gate = gate_for(lab, tmp_path, [True])
    assert gate.set("ppms", "B_rate", 0.002).ok
    r = gate.set("ppms", "B_set", 1.0)                                   # toward safe from 2.5 T: no approval needed
    assert r.ok and client.calls[-1] == ("B", 10000.0, 20.0, "linear")  # 1 T at 20 Oe/s
    assert gate.set("ppms", "T_rate", 5).ok
    r = gate.set("ppms", "T_set", 10)                                    # warmer than safe (1.8 K): asks
    assert r.ok and client.calls[-1] == ("T", 10.0, 5.0, "fast settle")
    assert gate.set("ppms", "B_set", 12).action == "refused"
    lab.close()
    assert client.calls[-1] == "closed"


# ------------------------------------------------------------------ card variables and Oxford Mercury

MERCURY = """
lab: Fridge
instruments:
  itc: { card: oxford/mercury-itc, address: "TCPIP0::10.0.0.9::7020::SOCKET", vars: { sensor: DB6.T1 } }
"""


def test_card_variables():
    card = builtin_registry().get("oxford/mercury-itc")
    assert card.params["T"].get_cmd.render() == "READ:DEV:MB1.T1:TEMP:SIG:TEMP"     # the default
    bound = card.bind({"sensor": "DB6.T1"})
    assert bound.params["setpoint"].set_cmd.render("4.2") == "SET:DEV:DB6.T1:TEMP:LOOP:TSET:4.2"
    assert card.bind(None) is card and card.params["T"].get_cmd.render().count("MB1.T1") == 1   # unchanged
    for bad in ({"sensor": "MB1.T1;SET:SYS:RST"}, {"sensor": "a b"}, {"sensor": ""}, {"nope": "x"}):
        with pytest.raises(CardError):
            card.bind(bad)                                               # nothing can be smuggled into a command
    with pytest.raises(ConfigError, match="no variable 'channel'"):
        load_lab_config(MERCURY.replace("sensor: DB6.T1", "channel: A"), builtin_registry())
    from labharness.cards import parse_card
    with pytest.raises(CardError, match="does not declare under vars"):
        parse_card({"card": 1, "id": "x/y", "model": "Y", "kind": "k",
                    "parameters": {"T": {"kind": "measure", "get": "READ:{chan}:T"}}})


def test_mercury_answers_every_set(tmp_path):
    reg = builtin_registry()
    itc = Device("oxford/mercury-itc", name="itc", replies=lambda cmd: "STAT:" + cmd + ":VALID")
    itc.twin = make_twin_backend(reg.get("oxford/mercury-itc").bind({"sensor": "DB6.T1"}), World(SimClock()), "itc")
    set_resource_manager(Bus({"TCPIP0::10.0.0.9::7020::SOCKET": itc}))
    try:
        lab = build_lab(MERCURY, registry=reg)
        assert lab.instruments["itc"].card.bound == {"sensor": "DB6.T1"}
        lab.read("itc", "T")
        assert itc.sent[-1] == "READ:DEV:DB6.T1:TEMP:SIG:TEMP"
        gate = gate_for(lab, tmp_path, [True])
        assert gate.set("itc", "ramp_rate", 2).ok and itc.sent[-1] == "SET:DEV:DB6.T1:TEMP:LOOP:RSET:2"
        itc.replies = lambda cmd: "STAT:" + cmd + ":INVALID"
        with pytest.raises(AdapterError, match="refused"):
            lab.instruments["itc"].backend.write("SET:DEV:DB6.T1:TEMP:LOOP:RSET:3")
    finally:
        set_resource_manager(None)
    be = VisaBackend("itc", reg.get("oxford/mercury-itc"), "ASRL1::INSTR", Bus({"ASRL1::INSTR": Device("oxford/mercury-itc")}))
    assert be.set_reply.pattern == ":VALID$"
    assert reg.get("oxford/mercury-itc").params["T"].from_wire("STAT:DEV:DB6.T1:TEMP:SIG:TEMP:4.2310K") == pytest.approx(4.231)
