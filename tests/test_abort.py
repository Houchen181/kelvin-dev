"""The abort invariant: after a stop or a failure every drive the plan touched goes to safe first (or holds where the
lab says hold), and a plan's own on_abort only adds to that. Nothing in the clean-up waits on a set point the
instrument ramps itself, a safe value no instrument reaches (0 K) is held instead, and a stop ends every wait, toward
safe too (field test: Stop didn't end a PPMS cool-down for ten minutes)."""
import threading
import time

from labharness import RunEngine, SafetyGate, ScriptedApprover
from simlab import build_virtual_lab
from labharness.clock import RealClock

TRIPS = """
plan: trips
setup: { smu.output: on }
sweep: { smu.V: {from: 0, to: 14, step: 1} }
measure: [smu.I]
"""


def test_a_plans_on_abort_adds_to_going_safe_and_never_replaces_it(lab, make_engine):
    engine, gate, approver = make_engine([True])
    report = engine.run(TRIPS + "on_abort: { smu.V: 0 }\n")             # it only zeroes the voltage
    assert report.status == "aborted" and "tripped" in report.message
    assert lab.read("smu", "V") == 0 and lab.read("smu", "output") is False    # the output is off all the same


def test_on_abort_cannot_move_a_drive_the_lab_holds(lab, make_engine):
    engine, gate, approver = make_engine([True])
    report = engine.run("""
plan: field_then_gate
setup: { magnet.B: 0.5, smu.output: on }
sweep: { smu.V: {from: 0, to: 14, step: 1} }
measure: [smu.I]
on_abort: { magnet.B: 0 }
""")
    assert report.status == "aborted"
    assert lab.read("magnet", "B") == 0.5 and abs(lab.read("magnet", "B_actual") - 0.5) < 1e-3   # safe_actions: hold
    assert "magnet.B stays held" in report.message and lab.read("smu", "output") is False


def test_a_stop_never_waits_on_absolute_zero(lab, make_engine):
    engine, gate, approver = make_engine([True])
    t = {}
    engine.progress = lambda e: t.setdefault("stopping", lab.clock.now()) if e.get("phase") == "stopping" else None
    report = engine.run("""
plan: warm_then_gate
setup: { tc.heater_range: low, tc.setpoint: 5, smu.output: on }
sweep: { smu.V: {from: 0, to: 14, step: 1} }
measure: [smu.I]
""")
    assert report.status == "aborted" and "tripped" in report.message
    assert lab.clock.now() - t["stopping"] < 60                     # no 30-minute wait for 0 K
    assert lab.read("tc", "heater_range") == "off"                  # the plan's heater, back to safe first
    assert lab.read("tc", "setpoint") == 5                          # held: the reading was there already
    lab.instruments["tc"].backend.state["T_sample"] = 3.0           # a stop while it is still on its way
    assert gate.abort_to_safe(["tc.setpoint"], "test")[0].message == "tc.setpoint held at 3 K (its safe 0 K can't be reached)"


def test_a_stop_ends_a_wait_toward_safe_at_once():
    lab = build_virtual_lab(clock=RealClock(), seed=1)
    gate = SafetyGate(lab, ScriptedApprover([]))
    out = {}
    # the heater is off, so the sample stays at 1.6 K: cooling to 1 K would wait the card's 30 minutes
    worker = threading.Thread(target=lambda: out.update(report=RunEngine(gate).run(
        "plan: cool\nsetup: { tc.setpoint: 1.0 }\nmeasure: [tc.T_sample]\n")), daemon=True)
    worker.start()
    state = lab.instruments["tc"].backend.state
    deadline = time.time() + 10
    while state["setpoint"] != 1.0 and time.time() < deadline:   # until the run is waiting for the sample
        time.sleep(0.05)
    time.sleep(0.3)
    t0 = time.time()
    gate.request_abort()   # the stop button
    worker.join(10)
    assert not worker.is_alive() and time.time() - t0 < 2
    report = out["report"]
    assert report.status == "aborted" and "stopped waiting" in report.message and "stop requested" in report.message
    assert abs(lab.read("tc", "setpoint") - lab.read("tc", "T_sample")) < 0.1   # held, not left chasing 0 K
