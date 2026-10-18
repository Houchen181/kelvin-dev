import pytest

from labharness import EventLog, RunEngine, RunStore, SafetyGate, ScriptedApprover, build_virtual_lab, demo_envelope


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
