import pytest

import labharness
import simlab
from labharness import EventLog, RunEngine, RunStore, SafetyGate, ScriptedApprover, homelab
from simlab import build_virtual_lab, demo_envelope

# The tests' simulated lab (tests/simlab) has cards of its own, ids virtual/...: for the tests, Kelvin's own cards include
# them, as the app's did before it lost its virtual lab (2026-10-29). The app ships none of them (test_no_virtual_lab.py).
_KELVINS_OWN = homelab.builtin_registry


def _with_the_test_labs_cards():
    return simlab.lab_cards(_KELVINS_OWN())


homelab.builtin_registry = labharness.builtin_registry = _with_the_test_labs_cards
homelab._BUILTIN_IDS = None


@pytest.fixture(autouse=True)
def no_real_hub(monkeypatch):
    """Tests never reach the real Kelvin hub: one that needs a hub names its own."""
    from labharness import central
    monkeypatch.setattr(central, "DEFAULT_HUB", "")


@pytest.fixture
def lab():
    return build_virtual_lab(seed=1)


@pytest.fixture
def make_gate(lab, tmp_path):
    """make_gate(answers) -> (gate, approver). An unexpected approval request fails the test."""
    def _make(answers=(), envelope=True):
        approver = ScriptedApprover(answers)
        log = EventLog(tmp_path / "events.jsonl", lab.clock)
        gate = SafetyGate(lab, approver, log, envelope=demo_envelope(lab) if envelope else None)
        return gate, approver
    return _make


@pytest.fixture
def make_engine(make_gate, tmp_path):
    def _make(answers=()):
        gate, approver = make_gate(answers)
        return RunEngine(gate, RunStore(tmp_path / "runs")), gate, approver
    return _make
