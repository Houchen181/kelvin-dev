"""Instruments with a JSON-RPC interface (jsonrpc.py), against a fake attocube eNSPIRE controller that answers as the
real one does: one JSON object a line, {"api": 2} in each request, and the controller's own error number first in
every result list."""
import io
import json

import pytest

from labharness import EventLog, SafetyGate, ScriptedApprover
from labharness.adapters import AdapterError, where_and_cause
from labharness.cards import CardError, parse_card
from labharness.drivers import MODULES
from labharness.homelab import build_lab, builtin_registry
from labharness.jsonrpc import JsonRpcInstrument, arguments, text_of

CRYO = "com.attocube.cryostat.interface"


class FakeEnspire:
    """The controller's side of the link: answers each request line, in small pieces, sometimes after a stale answer."""

    def __init__(self):
        self.sent, self.out, self.connected_to, self.closed = [], b"", None, 0
        self.state = {"T": 4.2, "T_set": 4.2, "temp_control": False, "vti": 3.5, "magnet_T": 4.0, "B": [0.0, 0.0],
                      "B_set": [0.0, 0.0], "h_state": ["IDLE", "IDLE"], "quench": False}
        self.error_next = 0        # the controller's error number for the next request
        self.refuse_next = False   # a JSON-RPC error object for the next request
        self.stale = False         # an answer to an earlier request comes first

    def create_connection(self, address, timeout):
        self.connected_to = address
        return self

    def settimeout(self, seconds):
        pass

    def answer(self, rid, values):
        if self.refuse_next:
            self.refuse_next = False
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}
        code, self.error_next = self.error_next, 0
        return {"jsonrpc": "2.0", "id": rid, "result": [code] + values}

    def sendall(self, data):
        assert data.endswith(b"\n")
        req = json.loads(data.decode("utf-8"))
        assert req["jsonrpc"] == "2.0" and req["api"] == 2
        self.sent.append((req["method"], req["params"]))
        m, p, s = req["method"], req["params"], self.state
        values = {
            CRYO + ".system.getDeviceType": lambda: ["attoDRY2100XL"],
            "com.attocube.system.getSerialNumber": lambda: ["XL-0042"],
            "com.attocube.system.getFirmwareVersion": lambda: ["1.4.2"],
            CRYO + ".sample.getTemperature": lambda: [s["T"]],
            CRYO + ".sample.getSetPoint": lambda: [s["T_set"]],
            CRYO + ".sample.getTempControlStatus": lambda: [s["temp_control"]],
            CRYO + ".vti.getTemperature": lambda: [s["vti"]],
            CRYO + ".magnet.getTemperature": lambda: [s["magnet_T"]],
            CRYO + ".magnet.getH": lambda: [s["B"][p[0]]],
            CRYO + ".magnet.getHSetPoint": lambda: [s["B_set"][p[0]]],
            CRYO + ".magnet.getHState": lambda: [s["h_state"][p[0]]],
            CRYO + ".magnet.getIsInQuenchState": lambda: [s["quench"]],
            CRYO + ".pressures.getCryoInPressure": lambda: [950.0],
            CRYO + ".pressures.getCryoOutPressure": lambda: [30.0],
            CRYO + ".pressures.getDumpPressure": lambda: [800.0],
            CRYO + ".action.getCurrentCommand": lambda: ["idle"],
            CRYO + ".system.getLastError": lambda: [0, "", "", "", 0],
        }
        if m == CRYO + ".system.isCryostatSane":   # its error number comes last: [sane, component, error]
            reply = {"jsonrpc": "2.0", "id": req["id"], "result": [True, "", 0]}
        elif m in values:
            reply = self.answer(req["id"], values[m]())
        else:
            acts = {CRYO + ".sample.setSetPoint": lambda: s.update(T_set=float(p[0])),
                    CRYO + ".sample.startTempControl": lambda: s.update(temp_control=True, T=s["T_set"]),
                    CRYO + ".sample.stopTempControl": lambda: s.update(temp_control=False),
                    CRYO + ".magnet.setHSetPoint": lambda: s["B_set"].__setitem__(p[0], float(p[1])),
                    CRYO + ".magnet.startFieldControl": lambda: (s["h_state"].__setitem__(p[0], "DRIVEN"),
                                                                 s["B"].__setitem__(p[0], s["B_set"][p[0]])),
                    CRYO + ".magnet.stopFieldControl": lambda: s["h_state"].__setitem__(p[0], "IDLE")}
            if m in acts:
                if not self.error_next and not self.refuse_next:
                    acts[m]()
                reply = self.answer(req["id"], [])
            else:
                reply = {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": "Method not found"}}
        if self.stale:
            self.stale = False
            self.out += (json.dumps({"jsonrpc": "2.0", "id": req["id"] - 1, "result": [0, 123.0]}) + "\n").encode()
        self.out += (json.dumps(reply) + "\n").encode()

    def recv(self, n):
        chunk, self.out = self.out[:9], self.out[9:]   # small pieces: the driver puts each answer together
        if not chunk:
            raise TimeoutError("timed out")
        return chunk

    def close(self):
        self.closed += 1


LAB = """
lab: Cryostat
instruments:
  cryo: { card: attocube/attodry-enspire, address: "jsonrpc://10.0.0.40:9090", limits: { B_set: [-1, 1] } }
"""


@pytest.fixture
def fake():
    fake = MODULES["jsonrpc"] = FakeEnspire()
    yield fake
    MODULES.clear()


def gate_for(lab, tmp_path, answers=()):
    return SafetyGate(lab, ScriptedApprover(list(answers)), EventLog(tmp_path / "events.jsonl", lab.clock))


def test_an_enspire_cryostat_reads_through_its_json_rpc_interface(fake):
    lab = build_lab(LAB)
    assert fake.connected_to == ("10.0.0.40", 9090)
    assert lab.read("cryo", "T") == pytest.approx(4.2)
    assert lab.read("cryo", "B") == 0.0 and fake.sent[-1] == (CRYO + ".magnet.getH", [0])   # its channel, a number
    assert lab.read("cryo", "magnet_state") == "IDLE"
    assert lab.read("cryo", "quench") is False and lab.read("cryo", "sane") is True   # its error number last: taken out
    assert lab.read("cryo", "last_error") == 0 and lab.read("cryo", "p_dump") == pytest.approx(800.0)
    health = lab.health_check(["cryo"])[0]
    assert health.ok, health.problems   # who it is: attocube,attoDRY2100XL,XL-0042,1.4.2 fits the card
    assert lab.instruments["cryo"].backend.identity() == "attocube,attoDRY2100XL,XL-0042,1.4.2"


def test_its_temperature_and_field_are_set_through_the_gate(fake, tmp_path):
    lab = build_lab(LAB)
    gate = gate_for(lab, tmp_path, answers=[True] * 8)   # a draft card: each change asks a person
    assert gate.set("cryo", "T_set", 10, wait=False).ok
    assert fake.sent[-1] == (CRYO + ".sample.setSetPoint", [10])
    assert gate.set("cryo", "temperature_control", True, wait=False).ok
    assert (CRYO + ".sample.startTempControl", []) in fake.sent
    assert lab.read("cryo", "temperature_control") is True
    assert gate.set("cryo", "field_control", True, wait=False).ok
    assert (CRYO + ".magnet.startFieldControl", [0]) in fake.sent   # the switch's on method, with its channel
    assert gate.set("cryo", "B_set", 0.5, wait=False).ok
    assert (CRYO + ".magnet.setHSetPoint", [0, 0.5]) in fake.sent
    assert gate.set("cryo", "B_set", 2, wait=False).action == "refused"   # the lab's limits
    assert gate.set("cryo", "field_control", False, wait=False).ok
    assert fake.sent[-1] == (CRYO + ".magnet.stopFieldControl", [0])


def test_the_controller_s_errors_and_refusals_are_said(fake):
    lab = build_lab(LAB)
    backend = lab.instruments["cryo"].backend
    fake.error_next = 7
    with pytest.raises(AdapterError, match="getTemperature: the instrument answered with its error 7"):
        backend.query("GET %s.sample.getTemperature" % CRYO)
    fake.refuse_next = True
    with pytest.raises(AdapterError, match="was refused: Method not found"):
        backend.query("GET %s.sample.getTemperature" % CRYO)
    fake.stale = True   # an answer to a request that timed out comes first: skipped by its id
    assert backend.query("GET %s.vti.getTemperature" % CRYO) == "3.5"
    with pytest.raises(AdapterError, match="not on the card"):   # only methods the card names are ever called
        backend.write("SET %s.system.shutdown 1" % CRYO)
    with pytest.raises(AdapterError, match="not a reading on the card"):
        backend.query("GET %s.system.getNetworkConfiguration" % CRYO)
    assert where_and_cause("jsonrpc://10.0.0.40:9090") == ("on the network (10.0.0.40)",
                                                           "network cable, power or its IP address?")


def test_a_card_s_json_rpc_readings_only_read():
    card = {"card": 1, "id": "x/rpc", "model": "X", "kind": "cryostat", "jsonrpc": {"port": 9090},
            "parameters": {"T": {"kind": "measure", "unit": "K", "get": "GET dev.startCooldown"}}}
    with pytest.raises(CardError, match="only reads"):
        parse_card(card)
    card["parameters"]["T"]["get"] = "GET dev.getTemperature"
    card["jsonrpc"]["switches"] = {"heater": {"on": "dev.heaterOn"}}
    with pytest.raises(CardError, match="needs on and off"):
        parse_card(card)
    card["jsonrpc"]["switches"] = {"heater": {True: "dev.heaterOn", False: "dev.heaterOff"}}   # YAML's bare on/off
    assert parse_card(card).jsonrpc["switches"]["heater"] == {"on": "dev.heaterOn", "off": "dev.heaterOff"}
    card["library"] = {"module": "dev"}
    with pytest.raises(CardError, match="not both"):
        parse_card(card)


def test_arguments_and_answers_as_text():
    assert arguments('0 1.5 true "two words" word -2e-3') == [0, 1.5, True, "two words", "word", -0.002]
    with pytest.raises(AdapterError):
        arguments('"open')
    assert text_of([True, "IDLE", 1.25, None]) == "1,IDLE,1.25,"


def test_over_http_and_with_the_port_from_the_card():
    seen = []

    def urlopen(req, timeout=None):
        body = json.loads(req.data)
        seen.append((req.full_url, body["method"], body["params"]))
        return io.BytesIO(json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": 21.5}).encode())

    card = parse_card({"card": 1, "id": "x/web-rpc", "model": "X", "kind": "temperature", "vendor": "Acme",
                       "jsonrpc": {"port": 8080, "transport": "http", "path": "/rpc", "errors": "none"},
                       "parameters": {"T": {"kind": "measure", "unit": "C", "get": "GET getTemp 2"}}})
    inst = JsonRpcInstrument("t", card, "jsonrpc://10.0.0.9", type("M", (), {"urlopen": staticmethod(urlopen)}))
    assert inst.query("GET getTemp 2") == "21.5"
    assert seen == [("http://10.0.0.9:8080/rpc", "getTemp", [2])]
    assert inst.identity() == "Acme,X,10.0.0.9"


def test_the_older_attodry_card_stays_for_the_dll():
    reg = builtin_registry()
    from labharness.discovery import match_card
    assert match_card("attocube,attoDRY2100XL,XL-0042,1.4.2", reg).id == "attocube/attodry-enspire"
    old = reg.get("attocube/attodry")
    assert old.library and not old.jsonrpc
    assert match_card("attocube,%s,COM4" % old.model, reg).id == "attocube/attodry"
