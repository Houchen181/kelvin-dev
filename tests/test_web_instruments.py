"""Networked instruments with their own protocols, each against a fake that answers like the real one: an M Squared
SolsTiS (JSON over TCP), a Light Conversion PHAROS (REST), a TOPTICA DLC pro (DeCoP command line) and an RFSoC
board running QICK (through its Pyro server)."""
import io
import json
import math
import types
import urllib.error

import pytest

from labharness import EventLog, SafetyGate, ScriptedApprover
from labharness.adapters import AdapterError
from labharness.drivers import MODULES
from labharness.virtual_lab import build_lab


def gate_for(lab, tmp_path, answers=()):
    return SafetyGate(lab, ScriptedApprover(list(answers)), EventLog(tmp_path / "events.jsonl", lab.clock))


@pytest.fixture(autouse=True)
def no_fakes_left():
    yield
    MODULES.clear()


# ------------------------------------------------------------------ M Squared SolsTiS: JSON over TCP

class FakeSolstis:
    """The laser's side of the link: answers each message, sometimes in pieces and with a task report first."""

    def __init__(self, allowed_ip="10.0.0.2"):
        self.allowed_ip, self.sent, self.out = allowed_ip, [], b""
        self.wavelength, self.etalon = 780.0, "on"
        self.connected_to = None

    def create_connection(self, address, timeout):
        self.connected_to = address
        return self

    def getsockname(self):
        return ("10.0.0.2", 51234)

    def sendall(self, data):
        msg = json.loads(data.decode("ascii"))["message"]
        self.sent.append(msg)
        op, p, tx = msg["op"], msg.get("parameters", {}), msg["transmission_id"]
        reply = {}
        if op == "start_link":
            reply = {"status": "ok" if p["ip_address"] == self.allowed_ip else "failed", "ip_address": "10.0.0.8"}
        elif op == "get_status":
            reply = {"status": [0], "wavelength": [self.wavelength], "temperature": [21.5], "etalon_lock": self.etalon,
                     "output_monitor": [1.2], "etalon_voltage": [140.0], "resonator_voltage": [55.0]}
        elif op == "move_wave_t":
            target = p["wavelength"][0]
            if not 700 <= target <= 1000:
                reply = {"status": [2]}
            else:
                self.wavelength, reply = target, {"status": [0]}
                # the final report of the previous task arrives before this reply
                self.out += json.dumps({"message": {"transmission_id": [99], "op": "move_wave_t_f_r",
                                                    "parameters": {"report": [0]}}}).encode()
        elif op == "poll_move_wave_t":
            reply = {"status": [0], "current_wavelength": [self.wavelength]}
        elif op == "etalon_lock":
            self.etalon, reply = p["operation"], {"status": [0]}
        elif op == "etalon_lock_status":
            reply = {"status": [0], "condition": self.etalon}
        else:
            self.out += json.dumps({"message": {"transmission_id": tx, "op": "parse_fail",
                                                "parameters": {"protocol_error": [7]}}}).encode()
            return
        self.out += json.dumps({"message": {"transmission_id": tx, "op": op + "_reply", "parameters": reply}}).encode()

    def recv(self, n):
        chunk, self.out = self.out[:7], self.out[7:]   # small pieces: the driver must reassemble the JSON
        if not chunk:
            raise TimeoutError("timed out")
        return chunk

    def close(self):
        pass


M2_LAB = """
lab: Ti:sapphire
instruments:
  ti: { card: m2/solstis, address: "m2://10.0.0.8:9360", limits: { wavelength_target: [760, 820] } }
"""


def test_an_m2_laser_tunes_and_reads_through_the_gate(tmp_path):
    fake = MODULES["m2"] = FakeSolstis()
    lab = build_lab(M2_LAB)
    assert fake.connected_to == ("10.0.0.8", 9360)
    assert fake.sent[0]["op"] == "start_link" and fake.sent[0]["parameters"] == {"ip_address": "10.0.0.2"}
    assert lab.read("ti", "wavelength") == pytest.approx(780.0) and lab.read("ti", "temperature") == 21.5
    gate = gate_for(lab, tmp_path)
    assert gate.set("ti", "wavelength_target", 795.5).ok
    assert fake.sent[-1]["parameters"] == {"wavelength": [795.5]}          # numbers go as one-element lists
    assert lab.read("ti", "tuning_status") == 0 and lab.read("ti", "wavelength") == pytest.approx(795.5)
    backend = lab.instruments["ti"].backend
    assert backend.reports["move_wave_t_f_r"] == {"report": 0}            # the task report, kept, not mistaken for the reply
    assert gate.set("ti", "wavelength_target", 900).action == "refused"    # the lab's range
    assert gate.set("ti", "etalon_lock", False).ok and fake.sent[-1]["parameters"] == {"operation": "off"}
    assert lab.read("ti", "etalon_state") == "off"
    assert gate.raw("ti", "SET gpio_output output=1").action == "refused"  # raw hardware outputs: blocked
    assert gate.raw("ti", "GET poll_move_wave_t#current_wavelength|wavelength").value == "795.5"
    assert gate.get("ti", "tuning_wavelength").value == pytest.approx(795.5)
    with pytest.raises(AdapterError, match="acts on the laser"):
        backend.query("GET tune_etalon")                                  # a GET never sends an op that acts
    with pytest.raises(AdapterError, match="operation not recognised"):
        backend.write("SET no_such_op x=1")


def test_the_m2_link_needs_this_computers_address_on_the_laser():
    MODULES["m2"] = FakeSolstis(allowed_ip="10.0.0.99")
    from labharness import builtin_registry
    from labharness.m2 import M2Laser, parameters
    card = builtin_registry().get("m2/solstis")
    with pytest.raises(AdapterError, match="Remote interface address must be 10.0.0.2"):
        M2Laser("ti", card, "m2://10.0.0.8:9360", MODULES["m2"])
    with pytest.raises(AdapterError, match="port"):
        M2Laser("ti", card, "m2://10.0.0.8", MODULES["m2"])
    assert parameters("wavelength=810,report=finished") == {"wavelength": [810], "report": "finished"}
    with pytest.raises(AdapterError):
        parameters("wavelength=-5 nm")


# ------------------------------------------------------------------ Light Conversion PHAROS: REST

class FakePharos:
    def __init__(self):
        self.state = {"IsOutputEnabled": False, "TargetAttenuatorPercentage": 0.0, "ActualPpDivider": 10,
                      "ActualStateName2": "StateOperational", "Errors": []}
        self.requests = []

    def __call__(self, req, timeout=None, context=None):
        path = req.full_url.split(":20022/v1", 1)[1]
        method = req.get_method()
        self.requests.append((method, path, req.data))
        name = path.rsplit("/", 1)[1]
        if method == "GET":
            if name == "ActualOutputPower":
                reply = 20.0 * self.state["TargetAttenuatorPercentage"] / 100 if self.state["IsOutputEnabled"] else 0.0
            else:
                reply = self.state[name]
            return io.BytesIO(json.dumps(reply).encode())
        if name == "EnableOutput":
            self.state["IsOutputEnabled"] = True
        elif name == "CloseOutput":
            self.state["IsOutputEnabled"] = False
        elif name == "TargetAttenuatorPercentage":
            value = json.loads(req.data)
            if not 0 <= value <= 100:
                raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b""))
            self.state[name] = value
        return io.BytesIO(b"")


def test_a_pharos_opens_only_after_approval_and_its_dangerous_endpoints_are_blocked(tmp_path):
    fake = MODULES["http"] = FakePharos()
    lab = build_lab("""
lab: Ultrafast
instruments:
  fs: { card: lightcon/pharos, address: "http://10.0.0.7:20022/v1", limits: { attenuator: [0, 25] }, auto: never }
""")
    assert lab.read("fs", "state") == "StateOperational" and lab.read("fs", "pp_divider") == 10
    assert lab.read("fs", "errors") == "[]"
    gate = gate_for(lab, tmp_path, [True, True])
    assert gate.set("fs", "output", True).ok and fake.requests[-1][:2] == ("POST", "/Basic/EnableOutput")
    assert gate.set("fs", "attenuator", 50).action == "refused"            # the lab's 25 %
    assert gate.set("fs", "attenuator", 20).ok and fake.requests[-1][2] == b"20"
    assert lab.read("fs", "power") == pytest.approx(4.0)
    r = gate.raw("fs", "POST /Basic/TurnOn")                               # starting the laser stays with people
    assert r.action == "refused" and fake.requests[-1][1] != "/Basic/TurnOn"
    for cmd in ("PUT /Advanced/Presets {}", "POST /Advanced/ResetRemoteInterlock", "PUT /Basic/TargetPpDivider 1"):
        assert gate.raw("fs", cmd).action == "refused", cmd
    assert gate.raw("fs", "GET /Basic/ActualPpDivider").ok                 # reads are reads
    gate.raw("fs", "POST /Basic/CloseOutput")                              # toward safe: no question
    assert fake.state["IsOutputEnabled"] is False


# ------------------------------------------------------------------ TOPTICA DLC pro: DeCoP command line

class FakeDlcPro:
    def __init__(self, echo=True):
        self.echo, self.lines, self.out = echo, [], b"DeCoP command line\r\n> "
        self.params = {"laser1:dl:cc:enabled": "#f", "laser1:dl:cc:current-set": "0", "laser1:dl:cc:current-act": "0.02",
                       "laser1:dl:cc:current-clip": "250", "emission": "#f", "interlock-open": "#f",
                       "frontkey-locked": "#f", "laser1:dl:tc:temp-act": "20.01", "laser1:dl:tc:temp-set": "20",
                       "system-health-txt": '"ok"', "serial-number": '"DLC-001"'}

    def create_connection(self, address, timeout):
        return self

    def sendall(self, data):
        line = data.decode().strip()
        self.lines.append(line)
        reply = ""
        if line.startswith("(param-ref '"):
            name = line[len("(param-ref '"):-1]
            reply = self.params.get(name, "Error: -1 unknown parameter")
        elif line.startswith("(param-set! '"):
            name, value = line[len("(param-set! '"):-1].split(" ", 1)
            if name not in self.params:
                reply = "Error: -1 unknown parameter"
            else:
                self.params[name], reply = value, "0"
        self.out += ((line + "\r\n") if self.echo else "").encode() + reply.encode() + b"\r\n> "

    def recv(self, n):
        chunk, self.out = self.out[:5], self.out[5:]
        if not chunk:
            raise TimeoutError("timed out")
        return chunk

    def close(self):
        pass


def test_a_dlc_pro_reads_its_state_and_its_draft_card_keeps_drives_simulated(tmp_path):
    fake = MODULES["toptica"] = FakeDlcPro()
    lab = build_lab("""
lab: Cooling lasers
instruments:
  dl: { card: toptica/dlc-pro, address: "toptica://10.0.0.9", limits: { current: [0, 150] } }
""")
    inst = lab.instruments["dl"]
    assert inst.backend.query("*IDN?").startswith("TOPTICA,") and "DLC-001" in inst.backend.query("*IDN?")
    assert lab.read("dl", "emission") is False and lab.read("dl", "temperature") == pytest.approx(20.01)
    assert lab.read("dl", "health_text") == "ok"                            # DeCoP strings come quoted
    gate = gate_for(lab, tmp_path, [True, True])
    r = gate.set("dl", "current_enabled", True)
    assert not r.ok and "draft" in r.message                                # unreviewed card: no real drive moves
    assert fake.params["laser1:dl:cc:enabled"] == "#f"
    assert gate.raw("dl", "SET laser1:dl:cc:current-clip 400").action == "refused"
    inst.backend.write("SET laser1:dl:cc:current-set 85.5")                 # what the driver sends, once reviewed
    assert fake.lines[-1] == "(param-set! 'laser1:dl:cc:current-set 85.5)"
    inst.backend.write("SET laser1:dl:cc:enabled #t")
    assert fake.lines[-1] == "(param-set! 'laser1:dl:cc:enabled #t)"
    with pytest.raises(AdapterError, match="unknown parameter"):
        inst.backend.query("GET laser1:dl:nothing")
    with pytest.raises(AdapterError, match="not a parameter name"):
        inst.backend.query("GET emission)(exec'change-ul")


# ------------------------------------------------------------------ RFSoC with QICK, through Pyro

class FakeSoc:
    def __init__(self):
        self.resets, self.programs = 0, []

    def reset_gens(self):
        self.resets += 1


def fake_qick(soc, soccfg):
    class AveragerProgramV2:
        def __init__(self, soccfg_, reps, final_delay, cfg=None):
            self.cfg, self.reps, self.calls = cfg, reps, []
            self._initialize(cfg)
            self._body(cfg)

        def __getattr__(self, name):   # declare_gen, add_pulse, trigger...: recorded
            if name.startswith("_"):
                raise AttributeError(name)
            return lambda **kw: self.calls.append((name, kw))

        def acquire(self, soc_, progress=True):
            soc_.programs.append(self)
            amp = 1000 * self.cfg["gain"]
            return [[[amp * math.cos(0.5), amp * math.sin(0.5)]]]

    return types.SimpleNamespace(make_proxy=lambda ns_host, ns_port, proxy_name: (soc, soccfg),
                                 AveragerProgramV2=AveragerProgramV2)


SOCCFG = {"tprocs": [{"type": "qick_processor"}], "board": "ZCU216",
          "gens": [{"has_mixer": False}, {"has_mixer": True}], "readouts": [{"tproc_ctrl": 0}, {}]}

QICK_LAB = """
lab: Qubits
instruments:
  rf: { card: qick/rfsoc, address: "qick://10.0.0.50:8888/myqick", vars: { gen: "0", ro: "0" }, limits: { gain: [0, 0.2] } }
"""


def test_a_qick_board_plays_a_tone_reads_iq_and_stops_dark(tmp_path):
    soc = FakeSoc()
    MODULES["qick"] = fake_qick(soc, SOCCFG)
    lab = build_lab(QICK_LAB)
    assert lab.instruments["rf"].backend.query("*IDN?") == "QICK,ZCU216,10.0.0.50:8888/myqick"
    gate = gate_for(lab, tmp_path, [True, True])
    assert gate.set("rf", "frequency", 512.5).ok and not soc.programs       # nothing plays while the output is off
    assert gate.set("rf", "output", True).ok
    assert gate.set("rf", "gain", 0.5).action == "refused"                 # the lab's 0.2
    assert gate.set("rf", "gain", 0.1).ok
    tone = soc.programs[-1]
    pulse = [kw for name, kw in tone.calls if name == "add_pulse"][0]
    assert pulse["mode"] == "periodic" and pulse["freq"] == 512.5 and pulse["gain"] == pytest.approx(0.1)
    assert ("send_readoutconfig", {"ch": 0, "name": "ro", "t": 0}) in tone.calls   # a tProc-controlled readout
    n = len(soc.programs)
    m, ph = lab.read("rf", "magnitude"), lab.read("rf", "phase")
    assert m == pytest.approx(100) and ph == pytest.approx(math.degrees(0.5))
    assert len(soc.programs) == n + 1                                      # one acquisition for both values
    assert gate.set("rf", "output", False).ok and soc.resets == 1          # reset_gens: every generator to zero
    assert lab.read("rf", "magnitude") == pytest.approx(0)                 # output off: the background, at zero gain
    assert [kw for name, kw in soc.programs[-1].calls if name == "add_pulse"][0].get("mode") is None


def test_qick_refuses_what_it_cannot_play():
    from labharness import builtin_registry
    from labharness.rfsoc import Qick
    card = builtin_registry().get("qick/rfsoc")
    soc = FakeSoc()
    with pytest.raises(AdapterError, match="tProc v2"):
        Qick("rf", card, "qick://h:8888/q", fake_qick(soc, dict(SOCCFG, tprocs=[{"type": "axis_tproc64x32_x8"}])))
    with pytest.raises(AdapterError, match="proxy name"):
        Qick("rf", card, "qick://h:8888", fake_qick(soc, SOCCFG))
    board = Qick("rf", card, "qick://h:8888/q", fake_qick(soc, SOCCFG))
    board.write("SET channels gen=1,ro=1")
    with pytest.raises(AdapterError, match="digital mixer"):
        board.write("SET output 1")
    board.write("SET mixer 4000")
    board.write("SET output 1")
    declared = [kw for name, kw in soc.programs[-1].calls if name == "declare_gen"][0]
    assert declared["mixer_freq"] == 4000
    assert ("declare_readout", {"ch": 1, "length": 1.0, "freq": 100.0, "gen_ch": 1}) in soc.programs[-1].calls
    board.close()
    assert soc.resets == 1                                                  # disconnecting stops the tone
