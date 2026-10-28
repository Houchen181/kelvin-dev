"""A run measures at the levels a person approved (field tests: a VNA at -90 dBm, lasers at 0 W, a 6221 at 0 A, while
the approval card and the summary showed the requested values). Switching an output on still parks its levels at safe
first, so nothing jumps; a plan's switches go on before its levels, a level a switch parked is put back, and every
drive is read back before the first point."""
from labharness import load_plan
from labharness.labconfig import Allowed

DARK = """
plan: pl_at_power
setup: { laser.power: 10, laser.emission: on, laser.shutter: on }
time: { every: 1, points: 3 }
measure: [laser.P_detected]
after: { laser.shutter: off }
"""


def test_a_level_written_before_its_switch_is_measured_at_the_approved_value(lab, make_engine):
    engine, gate, approver = make_engine([True])
    report = engine.run(DARK)
    assert report.ok, report.message
    assert lab.read("laser", "power") == 10 and lab.read("laser", "emission") is True
    assert all(abs(r["laser.P_detected"] - 5) < 0.1 for r in report.rows)   # 0.5 of 10 mW reaches the detector
    assert "laser.power" in str(approver.requests[0]) and "laser.emission" in str(approver.requests[0])


def test_switches_go_on_before_their_levels_and_nothing_else_moves(lab, make_engine):
    engine, _, _ = make_engine()
    plan = load_plan("""
plan: order
setup: { smu.V: 2, lockin.tc: 0.1, smu.compliance_I: 1.0e-6, smu.output: on, laser.power: 3, laser.emission: on }
measure: [smu.I]
""")
    assert [s.key for s in engine.ordered(plan.setup)] == [
        "smu.output", "smu.V", "lockin.tc", "smu.compliance_I", "laser.emission", "laser.power"]
    plan = load_plan("plan: switch_off\nsetup: { smu.V: 0, smu.output: off }\nmeasure: [smu.I]\n")
    assert [s.key for s in engine.ordered(plan.setup)] == ["smu.V", "smu.output"]   # switching off: as written


def test_a_switch_turned_on_later_in_the_plan_puts_its_level_back(lab, make_engine):
    engine, gate, approver = make_engine([True])
    report = engine.run("""
plan: on_late
setup: { smu.V: 2 }
sweep: { smu.output: { values: [on] } }
measure: [smu.I]
""")
    assert report.ok, report.message
    assert lab.read("smu", "V") == 2 and lab.read("smu", "output") is True


def test_inside_a_plan_the_level_is_put_back_after_parking_and_outside_it_stays_parked(lab, make_gate):
    gate, approver = make_gate([True, True])
    grants = {"smu.V": Allowed(lo=2, hi=2), "smu.output": Allowed(values=frozenset([True]))}
    with gate.preapproved(grants):
        assert gate.set("smu", "V", 2).ok
        r = gate.set("smu", "output", True)
    assert r.ok and "V set to 0 V first" in r.message and "V put back to 2 V" in r.message
    assert lab.read("smu", "V") == 2 and approver.requests == []
    gate.set("smu", "output", False)
    gate.set("smu", "V", 2)                     # a person's own request, outside any plan: approved
    r = gate.set("smu", "output", True)
    assert r.ok and "V set to 0 V first" in r.message and "put back" not in r.message
    assert lab.read("smu", "V") == 0


def test_a_level_the_instrument_does_not_take_stops_the_run_before_the_first_point(lab, make_engine):
    engine, gate, approver = make_engine([True])
    twin = lab.instruments["laser"].backend
    real = twin.apply
    twin.apply = lambda param, value: real(param, min(value, 4.0) if param == "power" else value)   # it clips at 4 mW
    report = engine.run(DARK)
    assert report.status == "aborted" and report.points == 0
    assert "before the first point, laser.power reads 4 mW, not the 10 mW the plan set" in report.message
    assert lab.read("laser", "emission") is False and lab.read("laser", "power") == 0    # then back to safe


def test_a_level_lost_during_the_sweep_is_an_issue_not_no_problems_found(lab, make_engine):
    engine, gate, approver = make_engine([True])
    twin = lab.instruments["laser"].backend
    engine.progress = lambda e: twin.state.update(power=0.0) if e.get("event") == "point" else None
    report = engine.run(DARK)
    assert report.ok, report.message
    assert any("laser.power reads 0 mW, not the 10 mW the plan set" in w for w in report.warnings)
    assert "no problems found" not in report.analysis["assessment"]
