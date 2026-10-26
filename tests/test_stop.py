"""A person's stop ends every long wait at once: a run's settle or interval between points, Kelvin's own wait, a ramp's
step. (A tool call an instrument holds up: test_web; a sweep an analyzer is still taking: test_vna.)"""
import threading
import time

import pytest

from labharness import RunEngine, SafetyGate, ScriptedApprover, build_virtual_lab
from labharness.clock import RealClock, SimClock, Stops


def test_a_stop_wakes_a_sleep_and_simulated_time_passes_at_once():
    stops = Stops()
    count = stops.count
    threading.Timer(0.2, stops.pulse).start()
    t0 = time.time()
    assert stops.sleep(RealClock(), 30, count) is False and time.time() - t0 < 1
    assert stops.sleep(RealClock(), 0.05, stops.count) is True
    assert stops.sleep(RealClock(), 5, count) is False   # a stop that came before the sleep began
    clock = SimClock()
    assert stops.sleep(clock, 3600, stops.count) and clock.now() == 3600


def test_a_stop_ends_a_run_waiting_between_its_points():
    lab = build_virtual_lab(clock=RealClock(), seed=1)
    gate = SafetyGate(lab, ScriptedApprover([]))
    out = {}
    worker = threading.Thread(target=lambda: out.update(report=RunEngine(gate).run(
        "plan: watch\ntime: { every: 30, points: 3 }\nmeasure: [smu.I]\n")))
    t0 = time.time()
    worker.start()
    time.sleep(0.5)
    gate.request_abort()   # the stop button
    worker.join(5)
    assert not worker.is_alive() and time.time() - t0 < 3
    assert out["report"].status == "aborted" and "stop requested" in out["report"].message


def test_kelvins_wait_ends_at_a_stop(tmp_path):
    pytest.importorskip("mcp")
    from labharness import demo_envelope
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    lab = build_virtual_lab(clock=RealClock(), seed=1)
    rt = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    threading.Timer(0.3, rt.stop).start()
    t0 = time.time()
    said = rt.wait(60)
    assert time.time() - t0 < 3 and said.startswith("stopped after") and "a person pressed stop" in said
