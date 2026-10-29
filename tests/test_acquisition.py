"""Triggered and buffered acquisition: a reading that arms, triggers and waits (*OPC?) before it fetches a multimeter's
buffer, and named actions (trigger, arm, fire a burst) that a person approves when they drive the sample, also at every
point of a plan."""
import pytest

from labharness import RunEngine, SafetyGate, ScriptedApprover, EventLog
from labharness.adapters import set_resource_manager
from labharness.cards import CardError, parse_card
from labharness.homelab import build_lab, lab_registry
from labharness.yamlio import load_yaml

DMM = """card: 1
id: acme/dmm-1
model: ACME DMM-1 multimeter
vendor: ACME
kind: dmm
status: lab-verified
identify: { idn: "^ACME,DMM-1" }
actions:
  arm: { send: "INIT", label: Arm the buffer }
  trigger: { send: "*TRG", label: Trigger a reading }
parameters:
  count: { label: Samples, kind: measure, type: int, set: "SAMP:COUN {value}", get: "SAMP:COUN?" }
  buffer:
    label: Buffer
    kind: measure
    type: trace
    unit: V
    get: ["INIT", "*TRG", "*OPC?", "TRAC:DATA?"]
    acquires: true
    wait: 20
"""
GEN = """card: 1
id: acme/gen-1
model: ACME GEN-1 burst generator
vendor: ACME
kind: function-generator
status: lab-verified
identify: { idn: "^ACME,GEN-1" }
actions:
  fire: { send: ":BURS:FIRE", label: Fire a burst, drives: true }
parameters:
  cycles: { label: Cycles, kind: measure, type: int, set: ":BURS:NCYC {value}", get: ":BURS:NCYC?" }
"""


class Resource:
    """A bus as VISA gives it: a query's answer waits to be read, and each read has the timeout set then."""

    def __init__(self, idn):
        self.idn, self.sent, self.timeouts, self.pending = idn, [], [], []
        self.timeout = 2000
        self.read_termination = self.write_termination = None
        self.armed, self.buffer, self.fired, self.count = False, [], 0, 3

    def write(self, cmd):
        self.sent.append(cmd)
        if cmd.endswith("?"):
            answers = {"*IDN?": self.idn, "*OPC?": "1", "SAMP:COUN?": str(self.count), ":BURS:NCYC?": "10",
                       "TRAC:DATA?": "#0" + ",".join("%.3f" % v for v in self.buffer)}
            self.pending.append(answers.get(cmd, "0"))
        elif cmd == "INIT":
            self.armed, self.buffer = True, []
        elif cmd == "*TRG" and self.armed:
            self.buffer, self.armed = [0.5 * (i + 1) for i in range(self.count)], False
        elif cmd == ":BURS:FIRE":
            self.fired += 1
        elif cmd.startswith("SAMP:COUN "):
            self.count = int(cmd.split()[1])

    def query(self, cmd, delay=None):
        self.write(cmd)
        return self.read()

    def read(self):
        self.timeouts.append((self.sent[-1], self.timeout))
        if not self.pending:
            raise TimeoutError("VI_ERROR_TMO")
        return self.pending.pop(0)

    def close(self):
        pass


class Bus:
    def __init__(self):
        self.devices = {"GPIB0::22::INSTR": Resource("ACME,DMM-1,1,1.0"), "GPIB0::10::INSTR": Resource("ACME,GEN-1,1,1.0")}

    def open_resource(self, name):
        return self.devices[name]

    def list_resources(self, query="?*::INSTR"):
        return tuple(self.devices)


@pytest.fixture
def bench(tmp_path):
    bus = Bus()
    set_resource_manager(bus)
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "dmm.yaml").write_text(DMM, encoding="utf-8")
    (tmp_path / "cards" / "gen.yaml").write_text(GEN, encoding="utf-8")
    lab = build_lab('lab: Bench\ninstruments:\n  dmm: { card: acme/dmm-1, address: "GPIB0::22::INSTR" }\n'
                    '  gen: { card: acme/gen-1, address: "GPIB0::10::INSTR" }\n', registry=lab_registry(tmp_path))
    yield lab, bus.devices["GPIB0::22::INSTR"], bus.devices["GPIB0::10::INSTR"]
    lab.close()
    set_resource_manager(None)


def test_a_buffered_reading_arms_triggers_waits_and_fetches(bench):
    lab, dmm, _ = bench
    before = dmm.timeout
    assert lab.read("dmm", "buffer") == pytest.approx([0.5, 1.0, 1.5])
    assert dmm.sent[-4:] == ["INIT", "*TRG", "*OPC?", "TRAC:DATA?"]
    waited = dict(dmm.timeouts)
    assert waited["TRAC:DATA?"] == 20000 and dmm.timeout == before    # the card's wait, then back
    assert waited["*OPC?"] == 1000   # awaited a second at a time (up to the card's 20 s), so a stop ends it


def test_actions_and_the_ones_that_drive_the_sample(bench, tmp_path):
    lab, dmm, gen = bench
    approver = ScriptedApprover([False, True])
    gate = SafetyGate(lab, approver, EventLog(tmp_path / "events.jsonl", lab.clock))
    assert gate.act("dmm", "arm").ok and dmm.armed                    # arming measures: no question
    assert gate.act("gen", "fire").action == "declined" and gen.fired == 0
    assert gate.act("gen", "fire").ok and gen.fired == 1
    assert "Fire a burst" in approver.requests[0].title
    assert "has no action blast" in gate.act("gen", "blast").message
    assert "actions (do_action, or a plan's trigger:): fire (drives the sample: asks first)" in gate.describe("gen")


def test_a_plan_fires_at_every_point_once_approved(bench, tmp_path):
    lab, dmm, gen = bench
    approver = ScriptedApprover([True])
    run = RunEngine(SafetyGate(lab, approver, EventLog(tmp_path / "events.jsonl", lab.clock))).run("""
plan: bursts
time: { every: 0.05, points: 3 }
trigger: [gen.fire]
measure: [dmm.buffer]
""")
    assert run.status == "done", run.message
    assert gen.fired == 3 and len(approver.requests) == 1
    assert "At every point, after settling: gen.fire (Fire a burst), which drive the sample" in approver.requests[0].items
    refused = RunEngine(SafetyGate(lab, ScriptedApprover([]))).run("plan: x\ntrigger: [gen.blast]\nmeasure: [dmm.count]\n")
    assert refused.status == "refused" and "gen has no action blast" in refused.message


def test_cards_keep_acquisitions_to_arming_and_actions_off_parameters():
    base = load_yaml(DMM)
    params = base["parameters"]
    with pytest.raises(CardError, match="add acquires: true"):
        parse_card(dict(base, parameters=dict(params, buffer=dict(params["buffer"], acquires=False))))
    with pytest.raises(CardError, match="would set count"):
        parse_card(dict(base, parameters=dict(params, buffer=dict(params["buffer"],
                                                                  get=["SAMP:COUN 5", "INIT", "TRAC:DATA?"]))))
    with pytest.raises(CardError, match="would set count: set it as the parameter"):
        parse_card(dict(base, actions={"more": {"send": "SAMP:COUN 100"}}))
    card = parse_card(base)
    assert card.params["buffer"].get_before == ["INIT", "*TRG", "*OPC?"] and card.params["buffer"].wait_s == 20
    assert card.actions["trigger"].send == ["*TRG"] and not card.actions["trigger"].drives
