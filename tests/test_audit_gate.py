"""The 0.3.52 audit's findings on the safety gate and the run engine (G1-G12), kept fixed. Each test is the audit's
repro (audit-repros/gate), made a test."""
import pytest

from labharness import EventLog, SafetyGate
from labharness.gate import Approver
from simlab import build_virtual_lab


class Person(Approver):
    """Answers every card with `answer`; before answering, does `meanwhile` (what happens while a card waits)."""

    def __init__(self, answer=True, meanwhile=None):
        self.answer, self.meanwhile, self.requests, self.informed = answer, meanwhile, [], []

    def approve(self, request):
        self.requests.append(request)
        if self.meanwhile is not None:
            self.meanwhile()
        return self.answer

    def inform(self, request, reasons):
        self.informed.append((request, reasons))


def _gate(answer=True, meanwhile=None):
    lab = build_virtual_lab(seed=1)
    person = Person(answer, meanwhile)
    return lab, SafetyGate(lab, person, EventLog(None, lab.clock)), person


def _twin(lab, name):
    return lab.instruments[name].backend.state


KEITHLEY = {"lab": "Bench", "instruments": {"smu": {"card": "keithley/2400", "limits": {"V": [-10, 10]}}}}


def _keithley(answer=True):
    """Kelvin's own Keithley 2400 card on a virtual twin: its :READ? switches the output on (read_when: output)."""
    lab = build_virtual_lab(seed=1, config=KEITHLEY)
    person = Person(answer)
    return lab, SafetyGate(lab, person, EventLog(None, lab.clock)), person


# ------------------------------------------------------------------ G1: line breaks in raw commands and text values

@pytest.mark.parametrize("command", ["*IDN?\n:OUTP ON\n:SOUR:VOLT 14", "*CLS\n:OUTP ON\n:SOUR:VOLT 14",
                                     "*RST\n:OUTP ON\n:SOUR:VOLT 14", "SOUR:VOLT?\r\n:OUTP ON\n:SOUR:VOLT 100"])
def test_g1_each_line_of_a_raw_command_is_checked_as_its_own(command):
    """A raw command was judged by how its first line started and sent whole: the output went on at 14 V, unasked."""
    lab, gate, person = _gate(answer=False)
    r = gate.raw("smu", command)
    assert not r.ok and person.requests, r.message
    state = _twin(lab, "smu")
    assert state["output"] is False and state["V"] == 0.0


def test_g1_a_control_character_or_a_text_value_with_a_line_break_is_refused():
    lab, gate, person = _gate()
    assert gate.raw("smu", "*IDN?\x1b:OUTP ON").action == "refused"
    r = gate.set("pg", "sequence", "none\nOUTPut ON")
    assert r.action == "refused" and not person.requests, r.message


# ------------------------------------------------------------------ G2: raw queries as the card's readings

def test_g2_a_raw_read_that_switches_an_output_on_is_asked_only_as_its_reading_would_be():
    """:READ? switches a 2400's output on: get_value asks it only while the output is on; a raw one went out, and a
    level approved as dark reached the sample."""
    lab, gate, person = _keithley()
    assert gate.set("smu", "V", 5).ok and _twin(lab, "smu")["output"] is False
    r = gate.raw("smu", ":READ?")
    assert r.action == "refused" and "read only while" in r.message, r.message
    assert _twin(lab, "smu")["output"] is False
    lab.hands_on = {"by": "Ada", "since": "14:02"}
    try:
        assert gate.raw("smu", ":READ?").action == "refused"
    finally:
        lab.hands_on = None


def test_g2_a_query_its_card_doesnt_list_asks_on_an_instrument_with_drives():
    lab, gate, person = _keithley(answer=False)
    asked = len(person.requests)
    r = gate.raw("smu", ":MEAS:CURR?")   # not on its card, and it measures: a 2400 switches its output on for it
    assert r.action == "declined" and len(person.requests) == asked + 1, r.message
    assert _twin(lab, "smu")["output"] is False
    assert gate.raw("smu", "*IDN?").ok and gate.raw("smu", ":TRIGger:COUNt?").ok   # reports and states: never asked
    assert len(person.requests) == asked + 1


# ------------------------------------------------------------------ G3: a move through safe is a move away

def test_g3_a_move_through_safe_to_the_other_polarity_asks():
    lab, gate, person = _gate()
    assert gate.set("smu", "V", 5).ok
    person.answer = False
    asked = len(person.requests)
    r = gate.set("smu", "V", -4.9)
    assert r.action == "declined" and len(person.requests) == asked + 1, r.message
    assert _twin(lab, "smu")["V"] == 5.0
    person.answer = True
    assert gate.set("smu", "V", 2).message.endswith("(toward safe)") or "toward safe" in gate.set("smu", "V", 2).message


# ------------------------------------------------------------------ G4, G5: what changed while a card waited

def test_g4_a_yes_after_the_drive_was_brought_to_safe_meanwhile_is_declined():
    """A watch brought the drive to safe while the card waited: a later yes made it jump from there, undoing it."""
    lab, gate, person = _gate()
    assert gate.set("lockin", "amplitude", 0.5).ok

    def watch_to_safe():
        lab.instruments["lockin"].write("amplitude", 0.0)
    person.meanwhile = watch_to_safe
    r = gate.set("lockin", "amplitude", 0.6)
    assert r.action == "declined" and "changed while its card waited" in r.message, r.message
    assert lab.instruments["lockin"].read("amplitude") == 0.0


def test_g5_a_limit_set_while_the_card_waits_holds_after_the_yes():
    lab, gate, person = _gate()
    smu = lab.instruments["smu"]

    def mounted():
        smu.device_limits["V"] = (-6.0, 6.0)
    person.meanwhile = mounted
    r = gate.set("smu", "V", 10)
    assert r.action == "refused" and "since its card was answered" in r.message, r.message
    assert _twin(lab, "smu")["V"] == 0.0


# ------------------------------------------------------------------ G6: a switch judged by what it applies

def test_g6_auto_mode_judges_a_switch_by_the_set_point_it_applies():
    lab, gate, person = _gate()
    assert gate.set("tc", "setpoint", 250, wait=False).ok
    gate.auto = True
    asked, told = len(person.requests), len(person.informed)
    r = gate.set("tc", "heater_range", "low")
    assert len(person.informed) == told and len(person.requests) == asked + 1, r.message
    assert "applies tc.setpoint 250 K" in " ".join(person.requests[-1].items)


# ------------------------------------------------------------------ G7, G8: a plan's path, its preconditions

SWEEP = "plan: gate_sweep\nsweep: { smu.V: {from: -3, to: 3, step: 0.5} }\nmeasure: [smu.I]\n"


def test_g7_a_plan_begun_away_from_safe_asks_once_for_its_whole_path():
    from labharness import RunEngine
    lab, gate, person = _gate()
    assert gate.set("smu", "V", 5).ok
    asked = len(person.requests)
    report = RunEngine(gate).run(SWEEP)
    assert report.status == "done", report.message
    assert len(person.requests) == asked + 1, [r.title for r in person.requests[asked:]]


def test_g8_a_plans_preconditions_are_read_again_after_its_card():
    from labharness import RunEngine
    lab, gate, person = _gate()
    plan = ('plan: cold\npreconditions: ["tc.T_sample < 2.5"]\nsweep: { smu.V: {from: 0, to: 1, step: 0.5} }\n'
            "measure: [smu.I]\n")
    tc = lab.instruments["tc"].backend

    def warmed():
        tc.state["T_sample"] = 35.0
    person.meanwhile = warmed
    report = RunEngine(gate).run(plan)
    assert report.status == "refused" and "since its card was answered" in report.message, report.message


# ------------------------------------------------------------------ G9-G12

@pytest.mark.parametrize("command", ["SOUR:VOLT:LIST 1,2,300", "SOUR:VOLT:STOP 0.5kV"])
def test_g9_every_number_of_a_drive_familys_command_is_checked(command):
    lab, gate, person = _gate()
    r = gate.raw("smu", command)
    assert r.action == "refused" and "beyond smu.V's limits" in r.message, r.message


def test_g10_a_sweep_with_no_values_is_refused():
    from labharness.plans import load_plan, PlanError
    lab = build_virtual_lab(seed=1)
    with pytest.raises(PlanError, match="no values"):
        load_plan("plan: p\nsweep: { smu.V: {values: []} }\nmeasure: [smu.I]\n")


def test_g11_a_condition_at_one_point_works_out_only_its_branch_and_powers_stay_real():
    from labharness import expr
    assert expr.scalar("100/n if n != 0 else 50", {"n": 0.0}) == 50.0
    with pytest.raises(expr.ExprError, match="no real value"):
        expr.scalar("abs(n ** 0.5)", {"n": -8.0})


def test_g12_queue_positions_start_at_one():
    from labharness.scanqueue import ScanQueue
    q = ScanQueue(None)
    for name in ("first", "second", "third"):
        q.add("plan: %s\ntime: { every: 1, points: 1 }\nmeasure: [smu.I]\n" % name)
    assert q.find(1).name == "first" and q.find("#3").name == "third"
    for ref in (0, -1):
        with pytest.raises(KeyError, match="numbered from 1"):
            q.find(ref)
