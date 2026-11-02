"""The cards the lab's LabVIEW VIs need (Plan 3): the Keithley 2400 sources current and switches its source function
through its card, every switch approved as a drive; a draft card for Thorlabs' K-Cube DC servo, the controller an APT
MG17Motor control drives."""
import pytest

from labharness.clock import SimClock
from labharness.eventlog import EventLog
from labharness.gate import SafetyGate, ScriptedApprover
from labharness.homelab import builtin_registry
from labharness.lab import Lab
from labharness.labconfig import load_lab_config
from labharness.sim.twin import World, sim_backend_factory

LAB = """lab: card fixes
instruments:
  smu:
    card: keithley/2400
    address: "GPIB0::24::INSTR"
"""


@pytest.fixture
def gate_2400():
    registry = builtin_registry()
    clock = SimClock()
    world = World(clock, 0)
    lab = Lab(load_lab_config(LAB, registry), registry, sim_backend_factory(world), clock, world)
    approver = ScriptedApprover([True] * 20)
    return SafetyGate(lab, approver, EventLog(None, clock)), approver, lab


def test_the_2400_sources_current_through_its_card(gate_2400):
    gate, approver, lab = gate_2400
    assert gate.set("smu", "source_function", "current").ok
    assert gate.set("smu", "I_source", 1e-3).ok and lab.read("smu", "I_source") == pytest.approx(1e-3)
    assert gate.set("smu", "compliance_V", 10).ok


def test_switching_the_2400s_source_function_is_a_drive_change(gate_2400):
    gate, approver, lab = gate_2400
    gate.set("smu", "source_function", "current")
    assert any("source_function" in str(r) for r in approver.requests), [str(r) for r in approver.requests]


def test_a_raw_source_function_command_on_the_2400_is_now_its_card_setting(gate_2400):
    gate, approver, lab = gate_2400
    r = gate.raw("smu", ":SOUR:FUNC CURR")
    assert r.action != "refused", r.message
    assert lab.read("smu", "source_function") == "current"


def test_an_output_on_card_still_says_which_source_function_it_applies(gate_2400):
    gate, approver, lab = gate_2400   # l08: an output once went on into current mode with nothing to say so
    gate.set("smu", "source_function", "current")
    gate.set("smu", "output", True)
    assert "smu: source function current" in str(approver.requests[-1]), str(approver.requests[-1])


def test_the_kdc101_is_a_draft_whose_every_move_asks():
    card = builtin_registry().get("thorlabs/kdc101")
    assert card.status == "draft" and {"home", "stop"} <= set(card.actions)
    assert {"position", "position_actual"} <= set(card.params)


def test_a_library_cards_actions_may_call_the_functions_they_name():
    from labharness import library
    sets, gets = library.card_calls(builtin_registry().get("thorlabs/kdc101"))
    assert {"MoveTo", "Home", "StopImmediate"} <= sets and "Position" in gets


def test_the_2400s_card_says_what_an_output_on_sets_first():
    text = builtin_registry().get("keithley/2400").params["output"].description
    assert "I_source" in text and "compliance_V" in text and "0.2 V" in text
