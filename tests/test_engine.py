import json

from labharness import load_plan

GATE_SWEEP = """
plan: gate_sweep
preconditions: ["tc.T_sample < 2.5"]
setup:
  lockin.amplitude: 0.05
  lockin.tc: 0.1
  smu.output: on
sweep:
  smu.V: {from: 0, to: 5, step: 0.25}
settle: 0.5
measure: [lockin.X, smu.I]
after:
  smu.V: 0
"""


def test_plan_runs_with_a_single_approval(lab, make_engine, tmp_path):
    engine, gate, approver = make_engine([True])
    report = engine.run(GATE_SWEEP)
    assert report.ok, report.message
    assert len(approver.requests) == 1
    request = str(approver.requests[0])
    assert "21 points" in request and "smu.V: now 0 V, plan uses 0 … 5 V" in request
    assert "smu.output" in request and "lockin.amplitude" not in request  # inside the envelope
    assert report.points == 21 and len(report.rows) == 21
    peak = max(report.rows, key=lambda r: r["lockin.X"])
    assert 1.0 <= peak["smu.V"] <= 2.0          # the toy sample's peak sits at 1.5 V
    assert lab.read("smu", "V") == 0            # `after` ran
    run_dir = tmp_path / "runs" / report.run_id
    for name in ("plan.yaml", "data.jsonl", "meta.json", "snapshot_start.json", "snapshot_end.json"):
        assert (run_dir / name).exists(), name
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["status"] == "done" and meta["points"] == 21


def test_dry_run_leaves_the_lab_untouched(lab, make_engine):
    engine, gate, approver = make_engine([])
    t0 = lab.clock.now()
    sim = engine.simulate(GATE_SWEEP)
    assert sim.ok and sim.points == 21 and sim.duration_s > 10
    assert sim.would_ask and "smu.V" in sim.would_ask[0]
    assert lab.clock.now() == t0
    assert lab.read("smu", "output") is False and lab.read("lockin", "amplitude") == 0


def test_dry_run_predicts_a_trip_and_the_real_run_aborts_safely(lab, make_engine):
    engine, gate, approver = make_engine([True])
    plan = GATE_SWEEP.replace("to: 5", "to: 14")
    report = engine.run(plan)
    assert "tripped" in str(approver.requests[0])           # the human saw the predicted trip
    assert report.status == "aborted" and "tripped" in report.message
    assert lab.read("smu", "V") == 0 and lab.read("smu", "output") is False
    assert lab.read("lockin", "amplitude") == 0              # default on_abort: all plan drives to safe


def test_declined_plan_does_nothing(lab, make_engine):
    engine, gate, approver = make_engine([False])
    report = engine.run(GATE_SWEEP)
    assert report.status == "declined" and lab.read("smu", "output") is False


def test_invalid_plans_are_refused_before_anything_runs(make_engine):
    engine, gate, approver = make_engine([])
    assert "lab limits" in engine.run(GATE_SWEEP.replace("to: 5", "to: 30")).message
    assert "no parameter 'Vg'" in engine.run(GATE_SWEEP.replace("smu.V: {", "smu.Vg: {")).message
    assert "unknown key" in engine.run(GATE_SWEEP + "\nrepeat: 3\n").message
    assert approver.requests == []


def test_preflight_blocks_an_offline_instrument(lab, make_engine):
    engine, gate, approver = make_engine([])
    lab.instruments["lockin"].backend.fault = "offline"
    report = engine.run(GATE_SWEEP)
    assert report.status == "refused" and "preflight failed: lockin FAIL" in report.message


def test_precondition_must_hold(lab, make_engine):
    engine, gate, approver = make_engine([])
    lab.instruments["tc"].backend.state["T_sample"] = 4.2
    report = engine.run(GATE_SWEEP)
    assert report.status == "refused" and "tc.T_sample < 2.5" in report.message


def test_measurement_plan_inside_the_envelope_needs_no_approval(make_engine):
    engine, gate, approver = make_engine([])
    report = engine.run("""
plan: vna_power_series
setup: { vna.rf: on, vna.points: 201 }
sweep: { vna.power: {values: [-40, -30, -20]} }
measure: [vna.S21]
""")
    assert report.ok, report.message
    assert approver.requests == []
    assert len(report.rows) == 3 and len(report.rows[0]["vna.S21"]) == 201


def test_nested_sweep_asks_once_for_both_drives(lab, make_engine):
    engine, gate, approver = make_engine([True])
    report = engine.run("""
plan: field_and_gate
setup: { smu.output: on, lockin.amplitude: 0.05 }
sweep:
  magnet.B: {values: [0, 0.5]}
  smu.V: {from: 0, to: 2, step: 1}
measure: [lockin.X]
after: { smu.V: 0 }
""")
    assert report.ok, report.message
    assert len(approver.requests) == 1
    request = str(approver.requests[0])
    assert "magnet.B" in request and "smu.V" in request
    assert [(r["magnet.B"], r["smu.V"]) for r in report.rows] == [
        (0.0, 0.0), (0.0, 1.0), (0.0, 2.0), (0.5, 0.0), (0.5, 1.0), (0.5, 2.0)]


def test_plan_parsing():
    p = load_plan(GATE_SWEEP)
    assert p.points == 21 and p.requires == ["lockin", "smu", "tc"]
    assert p.axes[0].values[-1] == 5
