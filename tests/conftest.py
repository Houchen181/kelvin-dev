import pytest
from pathlib import Path

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
def isolated_sequence_engine(tmp_path, monkeypatch):
    """The in-process sequence engine (labharness/sequence) writes under this test's tmp folder, never under
    ~/Kelvin/sequence: v1 fused labMaster into Kelvin, and a tool a test calls (run_plan, queue_add, a start) would
    otherwise leave its runs in the person's own Sequence page."""
    monkeypatch.setenv("KELVIN_SEQUENCE_DIR", str(tmp_path / "sequence"))
    try:
        from labharness.sequence import paths, service
    except Exception:   # noqa: BLE001 - an install without the vendored engine: nothing to isolate
        yield
        return
    try:
        paths.use_root(tmp_path / "sequence")
        service.reset_service()
        yield
    finally:
        service.reset_service()
        paths.use_root(Path.home() / "Kelvin" / "sequence")


@pytest.fixture(autouse=True)
def no_real_hub(monkeypatch):
    """Tests never reach the real Kelvin hub: one that needs a hub names its own. Nor models.dev: a test hub's
    catalog (hub/catalog.py) reads a list of its own, or none."""
    from labharness import central
    monkeypatch.setattr(central, "DEFAULT_HUB", "")
    try:
        from labharness.hub import catalog
    except ImportError:
        return
    monkeypatch.setattr(catalog, "_read_models_dev", lambda: {})
    from labharness.web import hubmodels
    hubmodels._SEEN.clear()   # what a hub said in one test is never another's


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
