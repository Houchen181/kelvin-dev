"""Instruments with a web API (HTTP + JSON), through the same gate: a made-up femtosecond laser's REST interface."""
import io
import json
import urllib.error

import pytest

from labharness import EventLog, SafetyGate, ScriptedApprover
from labharness.adapters import AdapterError
from labharness.drivers import MODULES
from labharness.homelab import build_lab, lab_registry

CARD = """
card: 1
id: acme/femto
model: ACME Femto laser (REST)
kind: laser
status: lab-verified
identify: { query: "GET /v1/About#model", idn: "^FEMTO" }
parameters:
  output:
    label: Output
    kind: drive
    type: bool
    set: "POST /v1/Basic/{value}"
    get: "GET /v1/Basic/IsOutputEnabled"
    bool_text: [EnableOutput, CloseOutput]
    safe: off
    enables: [attenuator]
  attenuator:
    label: Attenuator
    kind: drive
    unit: "%"
    set: "PUT /v1/Basic/TargetAttenuatorPercentage {value}"
    get: "GET /v1/Basic/TargetAttenuatorPercentage"
    limits: [0, 100]
    safe: 0
    applies_when: output
  power:
    label: Output power
    kind: measure
    unit: W
    get: "GET /v1/Basic/ActualOutputPower"
  state:
    label: State
    kind: measure
    type: str
    get: "GET /v1/Status#laser.state"
  shutter:
    label: Shutter
    kind: measure
    type: bool
    set: 'PUT /v1/Shutter {"open": {value}}'
    get: "GET /v1/Shutter#open"
    bool_text: ["true", "false"]
"""


class FakeLaser:
    def __init__(self):
        self.state = {"/v1/Basic/IsOutputEnabled": False, "/v1/Basic/TargetAttenuatorPercentage": 0.0,
                      "/v1/Basic/ActualOutputPower": 0.0, "/v1/Shutter": {"open": False}}
        self.requests = []
        self.fail = None

    def __call__(self, req, timeout=None, context=None):
        path = req.full_url.split(":20022", 1)[1]
        method, body = req.get_method(), req.data
        self.requests.append((method, path, body, dict(req.header_items()), context))
        if self.fail:
            raise urllib.error.HTTPError(req.full_url, self.fail, "nope", {}, io.BytesIO(b'{"error": "interlock"}'))
        if method == "GET":
            if path == "/v1/About":
                reply = {"model": "FEMTO-2", "serial": "123"}
            elif path == "/v1/Status":
                reply = {"laser": {"state": "Operational"}}
            else:
                reply = self.state[path]
        else:
            if path == "/v1/Basic/EnableOutput":
                self.state["/v1/Basic/IsOutputEnabled"] = True
                self.state["/v1/Basic/ActualOutputPower"] = 2.0 * (1 - self.state["/v1/Basic/TargetAttenuatorPercentage"] / 100)
            elif path == "/v1/Basic/CloseOutput":
                self.state["/v1/Basic/IsOutputEnabled"] = False
            else:
                self.state[path] = json.loads(body)
            reply = None
        return io.BytesIO(b"" if reply is None else json.dumps(reply).encode())


@pytest.fixture
def laser(tmp_path, monkeypatch):
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "femto.yaml").write_text(CARD, encoding="utf-8")
    fake = FakeLaser()
    MODULES["http"] = fake
    monkeypatch.setenv("FEMTO_TOKEN", "s3cret")
    lab = build_lab("""
lab: Ultrafast
instruments:
  fs: { card: acme/femto, address: "http://10.0.0.5:20022?auth_env=FEMTO_TOKEN", limits: { attenuator: [0, 30] },
        auto: never }
""", registry=lab_registry(tmp_path))
    yield lab, fake
    MODULES.clear()


def test_a_web_laser_reads_and_writes_through_the_gate(laser, tmp_path):
    lab, fake = laser
    inst = lab.instruments["fs"]
    assert inst.backend.query("*IDN?") == "FEMTO-2"                                   # identity from GET /v1/About
    assert lab.read("fs", "state") == "Operational" and lab.read("fs", "shutter") is False
    assert fake.requests[-1][3]["Authorization"] == "Bearer s3cret"                  # the token, from the environment
    gate = SafetyGate(lab, ScriptedApprover([True, True]), EventLog(tmp_path / "events.jsonl", lab.clock))
    assert gate.set("fs", "shutter", True).ok
    assert fake.requests[-1][:3] == ("PUT", "/v1/Shutter", b'{"open": true}')
    assert gate.set("fs", "output", True).ok                                           # a drive: asked first
    assert fake.requests[-1][:2] == ("POST", "/v1/Basic/EnableOutput")
    r = gate.set("fs", "attenuator", 50)
    assert r.action == "refused" and "30" in r.message                                 # the lab's limit
    assert gate.set("fs", "attenuator", 20).ok
    assert fake.state["/v1/Basic/TargetAttenuatorPercentage"] == 20
    assert gate.get("fs", "power").ok
    raw = gate.raw("fs", "POST /v1/Basic/CloseOutput")                                 # toward safe: no question
    assert raw.ok and fake.state["/v1/Basic/IsOutputEnabled"] is False
    fake.fail = 409
    r = gate.set("fs", "shutter", False)
    assert not r.ok and "HTTP 409" in r.message and "interlock" in r.message


def test_web_addresses_are_checked(monkeypatch, tmp_path):
    from labharness.cards import parse_card
    from labharness.yamlio import load_yaml
    from labharness.webapi import HttpInstrument, body_of, pick
    card = parse_card(load_yaml(CARD), "test")
    with pytest.raises(AdapterError, match="not set"):
        HttpInstrument("fs", card, "http://10.0.0.5:20022?auth_env=NO_SUCH_TOKEN_SET")
    assert body_of("12.5") == b"12.5" and body_of("on") == b'"on"' and body_of(None) is None
    assert pick({"a": [{"b": 3}]}, "a.0.b") == 3
    with pytest.raises(AdapterError, match="no field"):
        pick({"a": 1}, "b")
    pytest.importorskip("ssl")   # some Python builds cannot load it
    secure = HttpInstrument("fs", card, "https://10.0.0.5/api?insecure=1", module=lambda *a, **k: None)
    assert secure.context is not None and secure.base == "https://10.0.0.5/api"
