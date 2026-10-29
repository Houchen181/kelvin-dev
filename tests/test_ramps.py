"""Plans that measure while something ramps (plan item 9): a ramp: sweep that measures on the way, with a rate and a
hold per leg; until: and while:; a hold per step; logging while a stabilize waits, and near: its set point; and a
plan's retry for a reading that fails for a moment (from item 11), its gap marked in the data. The field tests' check
runs on labsim: a PPMS R(T) from 300 to 2 K at 10 K/min records points all the way down."""
import pytest

from labsim_bench import ctl, labsim_lab, labsim_offline  # noqa: F401  (the fixtures)
from test_watchdog import PPMS_LAB

from labharness import EventLog, RunEngine, RunStore, SafetyGate, ScriptedApprover, rundata
from labharness.conditions import parse_rule
from labharness.plans import PlanError, Stabilize, load_plan

RT_COOLDOWN = """
plan: RT_cooldown
setup: { isrc.I: 1.0e-6, isrc.output: on }
sweep:
  ppms.T_set: { ramp: [{to: 2, rate: 10}], every: 20 }
measure: [nvm.V]
after: { isrc.output: off }
"""


def engine_on(lab, tmp_path, answers=(True,)):
    approver = ScriptedApprover(answers)
    gate = SafetyGate(lab, approver, EventLog(tmp_path / "events.jsonl", lab.clock))
    return RunEngine(gate, RunStore(tmp_path / "runs")), gate, approver


# ------------------------------------------------------------------ the field tests' check, on labsim

def test_a_ppms_r_of_t_records_points_all_the_way_down(labsim_lab, tmp_path):
    """F2 l03 (and F1 t06, F3 P3): an R(T) while the PPMS cooled from 300 K recorded nothing until the set point
    arrived, as a set point waits for its reading. A ramp: sweep sets it without waiting and measures every 20 s on
    the way: points from 300 K down to 2 K, through the film's transition at 8.6 K."""
    lab, bench = labsim_lab("ppms", PPMS_LAB)
    engine, gate, approver = engine_on(lab, tmp_path)
    report = engine.run(RT_COOLDOWN)
    assert report.ok, report.message
    assert len(approver.requests) == 1 and "while ppms.T_set ramps to 2 K at 10 K/min" in str(approver.requests[0])
    temps = [r["ppms.T"] for r in report.rows]
    assert temps[0] > 295 and temps[-1] < 2.06              # from room temperature to the set point
    assert max(a - b for a, b in zip(temps, temps[1:])) < 5   # 10 K/min, a point every 20 s: no gaps on the way
    assert 80 < len(report.rows) < 110                       # 298 K at 10 K/min is ~30 min
    resistance = [(r["ppms.T"], r["nvm.V"] / 1e-6) for r in report.rows]
    assert all(410 < ohm < 530 for t, ohm in resistance if t > 10)   # the normal film, metallic: 514 Ω at 300 K
    assert resistance[0][1] > 500 and any(t < 30 and ohm < 440 for t, ohm in resistance)
    assert all(abs(ohm) < 2 for t, ohm in resistance if t < 7)       # superconducting below its Tc of 8.6 K
    assert report.analysis and report.analysis["series"][0]["x"] == "ppms.T"   # R against the temperature reached
    assert lab.read("isrc", "output") is False                # `after` ran
    assert report.duration_s == pytest.approx(30 * 60, rel=0.1)


def test_a_set_point_in_setup_still_waits_and_the_run_says_what_it_missed(labsim_lab, tmp_path):
    """What the testers' plans did: the set point in setup, then a time series. It waits for 2 K first (a set point
    in setup does), so the series is all at 2 K; the run now says so, and what to write instead."""
    lab, bench = labsim_lab("ppms", PPMS_LAB)
    engine, gate, approver = engine_on(lab, tmp_path, answers=())
    report = engine.run("""
plan: log_while_cooling
setup: { ppms.T_rate: 20, ppms.T_set: 2 }
time: { every: 30, for: 60 }
measure: [ppms.T]
""")
    assert report.ok and len(report.rows) == 3 and all(r["ppms.T"] < 2.1 for r in report.rows)
    assert any("ppms.T_set waited" in w and "nothing was measured on the way (a ramp: sweep measures while it "
               "ramps)" in w for w in report.warnings)


def test_a_ramp_that_stalls_stops_the_run_and_its_outputs_go_to_safe(labsim_lab, tmp_path):
    """The run's own watchdog: a ramp whose reading stops getting closer for its stall time (here 5 min) ends the run
    as a stop does (fail closed: the current source goes to safe), keeping every point and saying why."""
    lab, bench = labsim_lab("ppms", PPMS_LAB)
    engine, gate, approver = engine_on(lab, tmp_path)
    stalled = {}

    def stall_at_200_k(event):
        if event["event"] == "point" and event["row"]["ppms.T"] < 200 and not stalled:
            ctl(bench, "set", "ppms.t_rate", 0)
            stalled["t"] = lab.clock.now()
    engine.progress = stall_at_200_k
    report = engine.run(RT_COOLDOWN.replace("every: 20 }", "every: 20, stall: 300 }"))
    assert report.status == "aborted" and "ppms.T has not got closer to 2 K for 5.0 min" in report.message
    assert "the instrument may be stuck" in report.message
    assert lab.clock.now() - stalled["t"] < 7 * 60
    assert lab.read("isrc", "output") is False and lab.read("isrc", "I") == 0   # the abort's go-to-safe
    assert min(r["ppms.T"] for r in report.rows) > 150 and len(report.rows) > 20   # what it measured is kept


def test_a_meter_that_blips_mid_ramp_is_read_again_and_the_gap_is_marked(labsim_lab, tmp_path):
    """F2 l09: "a 1-second power blip ends the night: there is no retry". With retry:, the 2182A switched off for
    25 s is read again once it is back; the point after the gap says how long was lost (gap_s), and so does the run."""
    lab, bench = labsim_lab("ppms", PPMS_LAB)
    engine, gate, approver = engine_on(lab, tmp_path)
    lab.clock.at(32, lambda: ctl(bench, "power", "nvm", "off"))
    lab.clock.at(57, lambda: ctl(bench, "power", "nvm", "on"))
    plan = RT_COOLDOWN.replace("{to: 2, rate: 10}], every: 20", "{to: 290, rate: 10}], every: 10") \
        + "retry: { times: 5, wait: 10 }\n"
    report = engine.run(plan)
    assert report.ok, report.message
    gapped = [r for r in report.rows if r["gap_s"] > 0]
    assert len(gapped) == 1 and 20 <= gapped[0]["gap_s"] <= 31 and all("gap_s" in r for r in report.rows)
    assert abs(gapped[0]["nvm.V"] / 1e-6 - 513) < 10          # a real reading again, at the level approved
    assert any("1 reading failed for a moment and was taken again" in w and "gap_s" in w for w in report.warnings)
    rec = rundata.load(tmp_path / "runs" / report.run_id)
    assert rec.meta["gaps"][0]["tries"] >= 2


def test_a_source_that_restarted_is_never_measured_dark(labsim_lab, tmp_path):
    """A blip on the 6221 itself: once it answers again its output is off (its power-on state). The retry reads the
    plan's levels back before it counts a point, so the run stops rather than measure the sample dark."""
    lab, bench = labsim_lab("ppms", PPMS_LAB)
    engine, gate, approver = engine_on(lab, tmp_path)
    lab.clock.at(32, lambda: ctl(bench, "power", "isrc", "off"))   # the point at 40 s can't read it
    lab.clock.at(55, lambda: ctl(bench, "power", "isrc", "on"))
    plan = RT_COOLDOWN.replace("{to: 2, rate: 10}], every: 20", "{to: 290, rate: 10}], every: 10") \
        .replace("measure: [nvm.V]", "measure: [nvm.V, isrc.I]") + "retry: { times: 5, wait: 10 }\n"
    report = engine.run(plan)
    assert report.status == "aborted" and "did it restart?" in report.message
    assert "isrc.output reads off, not the on the plan set" in report.message
    assert all(abs(r["nvm.V"]) > 1e-4 for r in report.rows)   # every point kept was measured with the current on


RT_STEPS = """
plan: rt_steps
setup: { isrc.I: 1.0e-6, isrc.output: on }
sweep:
  ppms.T_set: { ramp: [{to: 290, rate: 10}, {to: 280, rate: 5, hold: 60}], every: 10 }
measure: [nvm.V]
after: { isrc.output: off }
"""


def test_a_ramps_card_counts_its_legs_and_a_draft_cards_rates_ask_once(labsim_lab, tmp_path):
    """The run's card counts each leg of a ramp (it said "time not counted"). The PPMS's rate is a setting, not a
    drive: on a draft card each leg's rate asked again in the middle of the ramp, outside the run's one card."""
    import re
    lab, bench = labsim_lab("ppms", PPMS_LAB)
    lab.instruments["ppms"].card.status = "draft"          # the lab's own draft of the card, not yet reviewed
    engine, gate, approver = engine_on(lab, tmp_path)
    report = engine.run(RT_STEPS)
    assert report.ok, report.message
    assert len(approver.requests) == 1, [str(r) for r in approver.requests]
    title, text = approver.requests[0].title, str(approver.requests[0])
    # from 300 K: 10 K at 10 K/min, 10 K at 5 K/min, then its 60 s hold: 4 min, a point every 10 s
    assert re.search(r"ramps to 290 K at 10 K/min, then 280 K at 5 K/min \(~2\d points\), ~4\.\d min$", title), title
    assert re.search(r"ppms\.T_set: now [\d.]+ K, plan uses 280 … 290 K; at 10 K/min, then 5 K/min \(the plan sets "
                     r"ppms\.T_rate\), ~3\.\d min; Kelvin isn't sure about ppms", text), text
    assert re.search(r"ppms\.T_rate: now [\d.]+ K/min, plan uses 5 … 10 K/min; Kelvin isn't sure about ppms", text), text
    # the sample lags the set point and settles into each target (as on a real PPMS): about half a minute a leg more
    assert 240 <= report.duration_s <= 330 and report.rows[-1]["ppms.T"] == pytest.approx(280, abs=0.5)


# ------------------------------------------------------------------ ramps on the virtual lab

FIELD_RAMP = """
plan: field_ramp
sweep:
  magnet.B: {ramp: [{to: 1, rate: 0.5}, {to: 0.2, rate: 0.25, hold: 30}], every: 5}
measure: [tc.T_sample]
after: {magnet.B: 0}
"""


def test_a_ramp_measures_on_the_way_at_each_legs_rate_and_holds(lab, make_engine, tmp_path):
    engine, gate, approver = make_engine([True])
    report = engine.run(FIELD_RAMP)
    assert report.ok, report.message
    request = str(approver.requests[0])
    assert "measures every 5 s while magnet.B ramps to 1 T at 0.5 T/min, then 200 mT at 0.25 T/min (~70 points)" \
        in request
    assert "magnet.rate: now 0.2 T/min, plan uses 0.25 … 0.5 T/min" in request   # a supply's rate is a drive: asked
    fields = [r["magnet.B_actual"] for r in report.rows]
    top = fields.index(1.0)
    assert fields[0] == 0 and fields[:top + 1] == sorted(fields[:top + 1]) and fields[-1] == 0.2
    assert 68 <= len(report.rows) <= 74   # 2 min up, 3.2 min down, a 30 s hold: a point every 5 s
    rec = rundata.load(tmp_path / "runs" / report.run_id)
    legs = rec.meta["legs"]
    assert [leg["to"] for leg in legs] == [1.0, 0.2] and legs[0]["arrived_t"] == pytest.approx(120, abs=5)
    assert legs[1]["ended_t"] - legs[1]["arrived_t"] >= 30
    assert rec.columns["axes"] == [{"key": "magnet.B_actual", "unit": "T", "kind": "ramp"}]
    assert lab.read("magnet", "B") == 0   # `after` ran


def test_a_ramp_is_checked_before_anything_moves(make_engine):
    engine, gate, approver = make_engine([])

    def refused(plan):
        report = engine.run("plan: x\n" + plan + "\nmeasure: [tc.T_sample]")
        assert report.status == "refused", report.message
        return report.message
    assert "Kelvin ramps smu.V itself, a step at a time" in refused("sweep: {smu.V: {ramp: 2, every: 1}}")
    assert "only a number can be ramped" in refused("sweep: {tc.heater_range: {ramp: low, every: 1}}")
    assert "rate: needs tc.setpoint's card to name the parameter its rate is set with" in refused(
        "sweep: {tc.setpoint: {ramp: [{to: 5, rate: 1}], every: 5}}")
    assert "magnet.rate 2 T/min is outside the lab config's limits" in refused(
        "sweep: {magnet.B: {ramp: [{to: 1, rate: 2}], every: 5}}")
    assert "magnet.B 5 T is outside" in refused("sweep: {magnet.B: {ramp: 5, every: 5}}")
    assert "say how close to each target counts as there (within: in T/min)" in refused(
        "sweep: {magnet.rate: {ramp: 0.1, every: 5, read: magnet.B_actual}}")
    assert "no parameter 'T_x'" in refused("sweep: {magnet.B: {ramp: 1, every: 5, read: tc.T_x}}")
    assert approver.requests == []
    with pytest.raises(PlanError, match="innermost loop"):
        load_plan("plan: x\nsweep:\n  magnet.B: {ramp: 1, every: 5}\n  smu.V: {values: [0, 1]}\nmeasure: [smu.I]")
    with pytest.raises(PlanError, match="every: seconds"):
        load_plan("plan: x\nsweep: {magnet.B: {ramp: 1}}\nmeasure: [smu.I]")
    with pytest.raises(PlanError, match="give it hold:"):
        load_plan("plan: x\nsweep: {magnet.B: {ramp: 1, every: 5, settle: 3}}\nmeasure: [smu.I]")


def test_a_stop_ends_a_ramp_at_once_and_its_drives_go_to_safe_or_hold(lab, make_engine):
    """The abort invariant holds in a ramp: a stop ends the wait for the next point at once, every drive the plan
    touched goes to safe (the source's output off), and the field holds where it is (the lab config says hold)."""
    engine, gate, approver = make_engine([True])
    engine.progress = lambda e: gate.request_abort() if e["event"] == "point" and e["done"] == 3 else None
    report = engine.run(FIELD_RAMP.replace("sweep:", "setup: {smu.output: on, smu.V: 1}\nsweep:"))
    assert report.status == "aborted" and report.points == 3 and "stop requested" in report.message
    assert lab.read("smu", "output") is False and lab.read("smu", "V") == 0
    held = lab.read("magnet", "B")
    assert 0.05 < held < 0.15 and "magnet.B held at" in report.message   # held, not ramped on to 1 T or to 0


def test_until_ends_a_loop_early_and_the_plans_own_ends_the_sweep(lab, make_engine):
    engine, gate, approver = make_engine([True])
    report = engine.run("""
plan: warm_until
setup: { tc.heater_range: medium }
sweep:
  tc.setpoint: { values: [2, 4, 6, 8, 10], until: "tc.T_sample > 5" }
measure: [tc.T_sample]
""")
    assert report.ok and [round(r["tc.setpoint"]) for r in report.rows] == [2, 4, 6]   # 8 and 10 skipped
    assert "ended early: tc.setpoint: until tc.T_sample > 5, at point 3" in report.message

    engine, gate, approver = make_engine([True])
    report = engine.run("""
plan: until_whole
setup: { tc.heater_range: medium }
sweep:
  tc.setpoint: { values: [2, 4, 6] }
time: { every: 5, for: 30 }
until: "tc.T_sample > 3.5"
measure: [tc.T_sample]
after: { tc.setpoint: 2 }
""")
    assert report.ok and len(report.rows) == 8 and report.rows[-1]["tc.setpoint"] == 4   # 7 at 2 K, 1 at 4 K
    assert "ended early: until tc.T_sample > 3.5, at point 8" in report.message
    assert lab.read("tc", "setpoint") == 2   # after: runs when it ends early

    engine, gate, approver = make_engine([True])
    report = engine.run(FIELD_RAMP.replace("every: 5}", "every: 5, while: \"magnet.B_actual < 0.5\"}"))
    assert report.ok and report.rows[-1]["magnet.B_actual"] >= 0.5 > report.rows[-2]["magnet.B_actual"]
    assert "magnet.B: while magnet.B_actual < 0.5 no longer held" in report.message


def test_a_hold_per_step(lab, make_engine):
    """F3 #21: the dwell was global, so every step of a staircase got the long final hold. settle: as value: seconds
    (or a list, one per value) gives a step its own."""
    engine, gate, approver = make_engine([True])
    report = engine.run("""
plan: staircase
setup: { tc.heater_range: medium }
sweep:
  tc.setpoint: { values: [2, 3, 4], settle: {4: 120} }
settle: 1
measure: [tc.T_sample]
""")
    t = [r["t"] for r in report.rows]
    assert report.ok and t[1] - t[0] < 100 and t[2] - t[1] >= 120
    plan = load_plan("plan: s\nsweep: {smu.V: {values: [0, 1, 2], settle: [0, 5, 60], back: true}}\nmeasure: [smu.I]")
    assert [plan.axes[0].settle_at(i) for i in range(5)] == [0, 5, 60, 5, 0]   # mirrored on the way back
    with pytest.raises(PlanError, match="3 is not one of the axis's values"):
        load_plan("plan: s\nsweep: {smu.V: {values: [0, 1], settle: {3: 5}}}\nmeasure: [smu.I]")


def test_stabilize_logs_while_it_waits_and_the_analysis_takes_the_settled_points(lab, make_engine, tmp_path):
    """F3 #21: stabilize stopped logging while it waited, and nobody could tell when it settled."""
    pytest.importorskip("numpy")
    engine, gate, approver = make_engine([True])
    report = engine.run("""
plan: settle_logged
setup: { tc.heater_range: medium }
sweep:
  tc.setpoint: { values: [3, 5], stabilize: { read: tc.T_sample, within: 0.02, for: 30, every: 10, timeout: 900 } }
measure: [tc.T_sample]
""")
    assert report.ok
    waiting = [r for r in report.rows if r["stabilizing"]]
    assert len(waiting) >= 6 and [r["stabilizing"] for r in report.rows].count(0) == 2
    assert {r["tc.setpoint"] for r in waiting} == {3, 5}
    rec = rundata.load(tmp_path / "runs" / report.run_id)
    assert [s["settled"] for s in rec.meta["stabilized"]] == [True, True] and rec.meta["stabilized"][1]["after_s"] >= 30
    # the automatic analysis took the two settled points only: too few for a curve, though many more were logged
    assert report.analysis["series"][0]["summary"] == "tc.T_sample: too few points to analyse (2 with numbers)"


def test_near_waits_for_the_set_point_not_only_a_steady_reading(lab, make_engine, monkeypatch):
    """F1 [5]: stabilize passed on a dead thermometer, steady at the wrong value. With near: the reading must also be
    that close to its set point (here the controller's own, which T_sample reads back)."""
    engine, gate, approver = make_engine([True, True])
    assert gate.set("tc", "heater_range", "medium").ok and gate.set("tc", "setpoint", 5, wait=False).ok
    twin = lab.instruments["tc"].backend
    monkeypatch.setattr(type(twin), "measure_T_sample", lambda self: 4.0)   # frozen short of 5 K
    st = Stabilize("tc", "T_sample", within=0.01, duration=30, timeout=120)
    warnings = []
    assert engine._stabilize(st, gate.stop_generation, warnings)["settled"] and not warnings
    st.near = 0.05
    info = engine._stabilize(st, gate.stop_generation, warnings)
    assert not info["settled"] and "it reads 4 K, not within 50 mK of 5 K" in warnings[0]
    from labharness.scanqueue import load_step   # a queue's step between scans takes near: too
    assert load_step("step: s\nset: {tc.setpoint: 5}\nstabilize: {read: tc.T_sample, within: 0.01, near: 0.05}"
                     ).stabilize.near == 0.05


def test_a_plans_retry_takes_a_reading_again_and_marks_the_gap(lab, make_engine, monkeypatch, tmp_path):
    read, failures = lab.read, {"left": 2}

    def flaky(instr, param):
        if (instr, param) == ("lockin", "X") and failures["left"] and lab.clock.now() > 3:
            failures["left"] -= 1
            raise TimeoutError("VI_ERROR_TMO (-1073807339): Timeout expired before operation completed.")
        return read(instr, param)
    monkeypatch.setattr(lab, "read", flaky)
    plan = """
plan: gate_retry
setup: { smu.output: on }
sweep: { smu.V: { from: 0, to: 2, step: 0.5 } }
settle: 1
measure: [lockin.X, smu.I]
retry: { times: 3, wait: 5 }
"""
    engine, gate, approver = make_engine([True])
    report = engine.run(plan)
    assert report.ok and len(report.rows) == 5
    assert [r["gap_s"] for r in report.rows].count(0) == 4
    assert max(r["gap_s"] for r in report.rows) == pytest.approx(10, abs=0.5)   # two tries 5 s apart
    assert "1 reading failed for a moment and was taken again (10 s lost in all, marked on that point as gap_s): " \
           "VI_ERROR_TMO" in " ".join(report.warnings)
    assert rundata.load(tmp_path / "runs" / report.run_id).meta["gaps"][0]["tries"] == 2

    failures["left"] = 2   # without retry: the run stops at the fault, as before
    engine, gate, approver = make_engine([True])
    report = engine.run(plan.replace("retry: { times: 3, wait: 5 }", ""))
    assert report.status == "aborted" and "VI_ERROR_TMO" in report.message

    failures["left"] = 9   # a fault that doesn't pass: tried, then the run stops and says how often
    engine, gate, approver = make_engine([True])
    report = engine.run(plan)
    assert report.status == "aborted" and "(tried 4 times)" in report.message


def test_a_sensor_fault_is_never_retried(lab, make_engine, monkeypatch):
    from labharness.instrument import SensorError
    read = lab.read

    def open_sensor(instr, param):
        if (instr, param) == ("tc", "T_sample") and lab.clock.now() > 3:
            raise SensorError("tc.T_sample reads 0 K, which it can't really be")
        return read(instr, param)
    monkeypatch.setattr(lab, "read", open_sensor)
    engine, gate, approver = make_engine([True])
    report = engine.run("plan: t\nsetup: {smu.output: on}\ntime: {every: 2, for: 10}\nmeasure: [tc.T_sample]\n"
                        "retry: 3")
    assert report.status == "aborted" and "can't really be" in report.message and "tried" not in report.message


def test_rules_read_like_the_conditions_people_write():
    rule = parse_rule("abs(smu.I) > 1e-6 or tc.T < 2 and tc.T > 1")
    assert rule.keys == ["smu.I", "tc.T"]
    assert rule.check(lambda i, p: {"I": -2e-6, "T": 300}[p]) is True
    assert rule.check(lambda i, p: {"I": 0.0, "T": 1.5}[p]) is True
    assert rule.check(lambda i, p: {"I": 0.0, "T": 3.0}[p]) is False
    assert rule.check(lambda i, p: {"I": float("nan"), "T": 300}[p]) is None   # can't tell: the loop ends
    assert rule.check(lambda i, p: {"I": float("nan"), "T": 1.5}[p]) is True
    assert parse_rule("smu.output == on").check(lambda i, p: True) is True
    for bad in ("", "smu.I >", "abs(smu.I > 1", "I > 1"):
        with pytest.raises(ValueError):
            parse_rule(bad)
    with pytest.raises(PlanError, match="while: cannot read"):
        load_plan("plan: x\nmeasure: [smu.I]\nwhile: smu.I ~ 1")
    with pytest.raises(PlanError, match="unknown untill"):
        load_plan("plan: x\nsweep: {smu.V: {values: [1], untill: 'smu.I > 1'}}\nmeasure: [smu.I]")
    plan = load_plan("plan: x\nsweep: {smu.V: {values: [1]}}\nmeasure: [smu.I]\nuntil: 'lockin.X > 1'")
    assert plan.requires == ["smu", "lockin"]   # an instrument only a condition reads is checked before the run too


def test_the_agent_is_told_a_set_point_waits_and_how_to_measure_on_the_way(tmp_path):
    """F2 A2: Kelvin itself wrote "log while cooling" plans that recorded nothing on the way. Its instructions and
    the tools it plans with say that a set point waits, and give the ramp: form; validate_plan and simulate_plan show
    what a ramp does."""
    pytest.importorskip("mcp")
    anyio = pytest.importorskip("anyio")
    from mcp import Client
    from simlab import build_virtual_lab, demo_envelope
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.agent.server import INSTRUCTIONS, build_server
    said = " ".join(INSTRUCTIONS.split())
    assert "blocks until its reading gets there" in said and "sweep it with ramp:" in said
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))

    async def main():
        async with Client(build_server(runtime)) as client:
            return {t.name: " ".join(t.description.split()) for t in (await client.list_tools()).tools}
    tools = anyio.run(main)
    assert "returns only once its reading gets there" in tools["set_value"] and "ramp:" in tools["set_value"]
    assert "ppms.T_set: {ramp: [{to: 2, rate: 10}], every: 10}" in tools["validate_plan"]
    assert "until:" in tools["validate_plan"] and "retry:" in tools["validate_plan"]
    assert "sweep its set point with ramp:" in tools["run_plan"]
    checked = runtime.validate(FIELD_RAMP)
    assert checked.startswith("ok: field_ramp, measures every 5 s while magnet.B ramps to 1 T at 0.5 T/min")
    rehearsed = runtime.simulate(FIELD_RAMP)
    assert "dry run: run" in rehearsed and "tc.T_sample: min" in rehearsed and "at magnet.B_actual=" in rehearsed
