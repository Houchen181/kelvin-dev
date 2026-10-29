import json

import pytest

from labharness import load_plan
from labharness.plans import PlanError
from labharness import rundata

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
    assert "smu.output" in request
    assert "lockin.amplitude: now 0 V, plan uses 50 mV, inside the project envelope" in request   # shown, not asked
    assert report.points == 21 and len(report.rows) == 21
    peak = max(report.rows, key=lambda r: r["lockin.X"])
    assert 1.0 <= peak["smu.V"] <= 2.0          # the toy sample's peak sits at 1.5 V
    assert lab.read("smu", "V") == 0            # `after` ran
    run_dir = tmp_path / "runs" / report.run_id
    rec = rundata.load(run_dir)
    assert "gate_sweep" in rec.plan_text and rec.snapshots["start"] and rec.snapshots["end"]
    assert rec.meta["status"] == "done" and rec.meta["points"] == 21 and len(rec.rows) == 21
    # one data file per run (run.h5); Kelvin's core alone keeps the record it would be written from
    assert sorted(p.name for p in run_dir.iterdir()) == (["run.h5"] if rundata.have_h5() else ["record.bin"])


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
    report = engine.run(plan, dry_run=True)                  # asked for a dry run
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
    assert "the lab config's limits" in engine.run(GATE_SWEEP.replace("to: 5", "to: 30")).message
    assert "no parameter 'Vg'" in engine.run(GATE_SWEEP.replace("smu.V: {", "smu.Vg: {")).message
    assert "unknown key" in engine.run(GATE_SWEEP + "\nrepeats: 3\n").message
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


def test_loop_options_expand_as_written():
    p = load_plan("""
plan: loops
repeat: 2
sweep:
  magnet.B: {from: 1, to: 100, points: 3, log: true}
  smu.V: {from: 0, to: 1, points: 3, back: true, settle: 0.2}
time: {every: 5, for: 10}
measure: [lockin.X]
""")
    assert [(a.key, a.kind) for a in p.axes] == [("repeat", "repeat"), ("magnet.B", "instrument"),
                                                  ("smu.V", "instrument"), ("time", "time")]
    assert p.axes[0].values == [1, 2]
    assert [round(v, 9) for v in p.axes[1].values] == [1, 10, 100]
    assert p.axes[2].values == [0, 0.5, 1, 0.5, 0] and p.axes[2].settle == 0.2
    assert p.axes[3].values == [0, 5, 10]
    assert p.points == 2 * 3 * 5 * 3
    assert p.instrument_axes == p.axes[1:3]


def test_bad_loop_options_are_refused():
    for bad in ("repeat: 0", "time: {every: 0, for: 10}", "sweep: {smu.V: {from: -1, to: 1, points: 5, log: true}}",
                "sweep: {smu.V: {values: [1], stabilize: {read: tc.T_sample, within: -1}}}"):
        with pytest.raises(PlanError):
            load_plan("plan: x\nmeasure: [lockin.X]\n" + bad)


def test_run_reports_progress_and_analyses_itself(make_engine, tmp_path):
    pytest.importorskip("numpy")
    engine, gate, approver = make_engine([True])
    events = []
    engine.progress = events.append
    report = engine.run(GATE_SWEEP)
    assert report.ok, report.message
    kinds = [e["event"] for e in events]
    assert kinds[0] == "start" and kinds[-1] == "end" and kinds.count("point") == 21
    start = events[0]
    assert start["total"] == 21 and [a["key"] for a in start["axes"]] == ["smu.V"]
    assert [(m["key"], m["kind"]) for m in start["measures"]] == [("lockin.X", "scalar"), ("smu.I", "scalar")]
    points = [e for e in events if e["event"] == "point"]
    assert [p["done"] for p in points] == list(range(1, 22)) and 0 <= points[-1]["eta_s"] < 1
    assert events[-1]["status"] == "done"
    phase = next(e for e in events if e["event"] == "phase")       # GATE_SWEEP has `after` steps
    assert phase["phase"] == "after" and kinds.index("phase") == len(kinds) - 2
    a = report.analysis
    peak = a["series"][0]
    assert peak["y"] == "lockin.X" and peak["kind"] == "peak"
    assert abs(peak["params"]["center"][0] - 1.5) < 0.05
    assert a["series"][1]["kind"] == "linear"
    assert "peak" in a["assessment"] and a["next"]
    saved = rundata.load(tmp_path / "runs" / report.run_id, with_rows=False).analysis
    assert saved["assessment"] == a["assessment"]


def test_dry_run_is_not_analysed(make_engine):
    engine, gate, approver = make_engine([])
    assert engine.simulate(GATE_SWEEP).analysis is None


def test_repeat_and_time_series(make_engine):
    engine, gate, approver = make_engine([])
    report = engine.run("""
plan: noise_log
setup: { lockin.amplitude: 0.05 }
repeat: 2
time: {every: 5, for: 10}
measure: [lockin.X]
""")
    assert report.ok, report.message
    assert [(r["repeat"], r["time"]) for r in report.rows] == [(1, 0), (1, 5), (1, 10), (2, 0), (2, 5), (2, 10)]
    ts = [r["t"] for r in report.rows]
    assert ts[1] - ts[0] >= 5 and ts[2] - ts[1] >= 5


def test_stabilize_waits_for_the_reading(lab, make_engine):
    engine, gate, approver = make_engine([True])
    report = engine.run("""
plan: warm_steps
setup: { tc.heater_range: low }
sweep:
  tc.setpoint: {values: [1.8, 2.0], stabilize: {read: tc.T_sample, within: 0.01, for: 30, timeout: 1800}}
measure: [tc.T_sample]
after: { tc.setpoint: 1.6 }
""")
    assert report.ok, report.message
    assert not report.warnings
    for row in report.rows:
        assert abs(row["tc.T_sample"] - row["tc.setpoint"]) < 0.02
    assert report.duration_s > 60


def test_stabilize_gives_up_with_a_warning(lab, make_engine):
    pytest.importorskip("numpy")
    engine, gate, approver = make_engine([True])
    report = engine.run("""
plan: impatient
setup: { tc.heater_range: low }
sweep:
  tc.setpoint: {values: [1.8], stabilize: {read: tc.T_sample, within: 0.000001, for: 10, timeout: 60}}
measure: [tc.T_sample]
after: { tc.setpoint: 1.6 }
""")
    assert report.ok and len(report.rows) == 1
    assert report.warnings and "did not settle" in report.warnings[0]
    assert any("did not settle" in i for i in report.analysis["issues"])


def test_event_log_keeps_every_line_from_many_threads(tmp_path):
    import threading
    from labharness import EventLog
    log = EventLog(tmp_path / "events.jsonl")
    threads = [threading.Thread(target=lambda k=k: [log.write("t", k=k, i=i) for i in range(200)]) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1600 and sorted(json.loads(l)["seq"] for l in lines) == list(range(1, 1601))


MEASURE_ONLY = """
plan: noise_check
setup: { lockin.tc: 0.1 }
repeat: 3
measure: [lockin.X, tc.T_sample]
"""


def test_no_plan_is_dry_run_unless_asked(lab, make_engine, monkeypatch):
    """Not even one that drives the sample: its caution is the limits, one approval, ramps and guard readings. A dry
    run happens when asked, and in auto mode, where nobody approves and the dry run stands in for the person's look."""
    engine, gate, approver = make_engine([True, True, True])
    assert engine.drive_values(load_plan(MEASURE_ONLY)) == {}
    rehearsed, real = [], engine.simulate
    monkeypatch.setattr(engine, "simulate", lambda plan: rehearsed.append(1) or real(plan))
    report = engine.run(MEASURE_ONLY)
    assert report.ok and report.dry_run is None and approver.requests == []
    report = engine.run(GATE_SWEEP)
    assert report.ok, report.message
    assert report.dry_run is None and rehearsed == [] and len(approver.requests) == 1   # one approval, no rehearsal
    assert "21 points" in str(approver.requests[0])
    report = engine.run(GATE_SWEEP, dry_run=True)
    assert report.ok and report.dry_run is not None and rehearsed == [1]
    gate.auto = True
    engine.run(GATE_SWEEP)
    assert rehearsed == [1, 1]


def test_without_a_dry_run_a_trip_still_ends_safely(lab, make_engine):
    """What a dry run would have predicted, the guard readings catch on the real run."""
    engine, gate, approver = make_engine([True])
    report = engine.run(GATE_SWEEP.replace("to: 5", "to: 14"))
    assert report.status == "aborted" and "tripped" in report.message and report.dry_run is None
    assert lab.read("smu", "V") == 0 and lab.read("smu", "output") is False


def test_instruments_are_checked_before_use_not_before_every_scan(lab, make_engine, monkeypatch):
    from labharness import engine as engine_module
    engine, gate, approver = make_engine([True])
    checked, real = [], lab.health_check
    monkeypatch.setattr(lab, "health_check", lambda names=None: checked.append(sorted(names or [])) or real(names))
    assert engine.run(MEASURE_ONLY).ok and engine.run(MEASURE_ONLY).ok
    assert checked == [["lockin", "tc"]]                                  # once, not again for the second scan
    engine.preflight(["lockin"], fresh=True)                              # asked for: always checks
    assert checked[-1] == ["lockin"]
    monkeypatch.setattr(engine_module, "HEALTHY_FOR", -1.0)               # a while later: checked again
    assert engine.run(MEASURE_ONLY).ok and checked[-1] == ["lockin", "tc"]
    monkeypatch.setattr(engine_module, "HEALTHY_FOR", 600.0)
    n = len(checked)
    assert engine.run(GATE_SWEEP).ok                                     # a plan that drives always checks now
    assert len(checked) == n + 1 and "smu" in checked[-1]


def test_an_instrument_that_failed_is_checked_again(lab, make_engine):
    engine, gate, approver = make_engine([])
    assert engine.run(MEASURE_ONLY).ok
    lab.instruments["lockin"].backend.fault = "offline"                  # it dies after its check
    assert engine.run(MEASURE_ONLY).status != "done"                     # the scan just stops
    report = engine.run(MEASURE_ONLY)
    assert report.status == "refused" and "preflight failed: lockin FAIL" in report.message


def test_the_agent_is_told_to_work_like_an_experimentalist():
    pytest.importorskip("mcp")
    from labharness.agent.server import INSTRUCTIONS
    assert "not like a programmer" in INSTRUCTIONS and "call run_plan directly" in INSTRUCTIONS
    assert "simulate_plan, then run_plan" not in INSTRUCTIONS and "not a routine" in INSTRUCTIONS
