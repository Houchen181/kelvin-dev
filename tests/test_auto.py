"""Auto mode: a person can let low-risk drive changes and runs go ahead without asking."""
import json

import pytest

from labharness import builtin_registry
from labharness.labconfig import ConfigError, load_lab_config

PLAN = """
plan: gate_sweep
setup: {{ lockin.amplitude: 0.05, smu.output: on }}
sweep: {{ smu.V: {{from: 0, to: {top}, step: 0.5}} }}
settle: 0.5
measure: [lockin.X]
after: {{ smu.V: 0 }}
"""


def logged(tmp_path, kind="ask"):
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["type"] == kind]


def test_auto_mode_is_off_until_a_person_turns_it_on(make_gate):
    gate, approver = make_gate([True])
    assert gate.auto is False
    assert "(approved)" in gate.set("smu", "V", 2).message
    assert len(approver.requests) == 1


def test_low_risk_drive_changes_run_without_asking(lab, make_gate, tmp_path):
    gate, approver = make_gate([])           # any request would fail the test
    gate.auto = True
    assert "(auto-approved)" in gate.set("smu", "V", 3).message           # inside the lab's own auto range ±5 V
    assert "(auto-approved)" in gate.set("tc", "setpoint", 50, wait=False).message  # 17% of the way to 300 K
    assert "(auto-approved)" in gate.set("tc", "heater_range", "low").message  # one step from off
    assert lab.read("smu", "V") == 3
    asks = logged(tmp_path)
    assert [a["answer"] for a in asks] == ["auto-approved"] * 3
    assert "smu.V stays in its auto range -5 … 5 V" in asks[0]["note"]
    assert "17% of the way" in asks[1]["note"]


def test_riskier_changes_still_ask_and_say_why(make_gate):
    gate, approver = make_gate([True, True, False, True])
    gate.auto = True
    assert "(approved)" in gate.set("smu", "V", 8).message
    assert "(approved)" in gate.set("tc", "setpoint", 120, wait=False).message
    assert gate.set("laser", "power", 1).action == "declined"
    assert "(approved)" in gate.set("tc", "heater_range", "medium").message
    why = [r.items[-1] for r in approver.requests]
    assert why[0] == "Auto mode does not cover this: smu.V leaves its auto range -5 … 5 V"
    assert why[1] == ("Auto mode does not cover this: tc.setpoint goes 40% of the way from safe to its limit "
                      "(auto mode covers 25%)")
    assert why[2] == "Auto mode does not cover this: laser.power is never auto-approved in this lab"
    assert why[3] == "Auto mode does not cover this: tc.heater_range goes more than one step from safe"


def test_position_not_step_size_decides(make_gate):
    """Creeping up in small steps does not get past auto mode: each target is judged from safe."""
    gate, approver = make_gate([True])
    gate.auto = True
    for v in (20, 40, 60):
        assert "(auto-approved)" in gate.set("tc", "setpoint", v, wait=False).message
    assert len(approver.requests) == 0
    gate.set("tc", "setpoint", 80, wait=False)  # 27% of the way: asks, although the step is only 20 K
    assert len(approver.requests) == 1


def test_limits_interlocks_and_raw_families_are_unchanged(lab, make_gate):
    gate, approver = make_gate([False])
    gate.auto = True
    assert gate.set("smu", "V", 20).action == "refused"                   # beyond the lab limit
    lab.instrument("tc").backend.load_state({"T_sample": 50.0})
    assert gate.set("magnet", "B", 0.2).action == "refused"               # interlock: sample too warm
    assert gate.raw("smu", "SOUR:VOLT:PROT 20").action == "declined"      # unparseable drive family: always asks
    assert "touches drive V" in str(approver.requests[0])
    assert "(auto-approved)" in gate.raw("smu", "SOUR:VOLT 2").message      # a plain setting is judged like set


def test_low_risk_plan_runs_without_asking(make_engine, tmp_path):
    engine, gate, approver = make_engine([])
    gate.auto = True
    report = engine.run(PLAN.format(top=4))
    assert report.ok, report.message
    ask = logged(tmp_path)[-1]
    assert ask["answer"] == "auto-approved" and ask["request"].startswith("Run plan gate_sweep")
    assert "smu.output is one step from safe" in ask["note"] and "the dry run finished cleanly" in ask["note"]


def test_plan_beyond_auto_still_asks_once(make_engine):
    engine, gate, approver = make_engine([True])
    gate.auto = True
    report = engine.run(PLAN.format(top=8))
    assert report.ok, report.message
    (request,) = approver.requests
    assert request.items[-1] == "Auto mode does not cover this run: smu.V leaves its auto range -5 … 5 V"


def test_auto_plan_needs_a_clean_dry_run(make_gate):
    gate, _ = make_gate([])
    gate.auto = True

    class DryRun:
        ok = False

    ok, why = gate.auto_plan({}, DryRun())
    assert not ok and why == ["the dry run did not finish cleanly"]
    assert gate.auto_plan({}, None) == (False, ["the dry run did not finish cleanly"])
    gate.auto = False
    assert gate.auto_plan({}, None) == (False, [])


LAB = """
lab: test
auto_fraction: {fraction}
instruments:
  smu:
    card: virtual/source-meter
    limits: {{ V: [-10, 10] }}
    auto: {auto}
"""


def test_lab_config_auto_rules_are_checked():
    reg = builtin_registry()
    cfg = load_lab_config(LAB.format(fraction=0.1, auto="{ V: [-1, 2], output: never }"), reg)
    assert cfg.auto_fraction == 0.1
    assert cfg.instruments["smu"].auto["V"].hi == 2 and cfg.instruments["smu"].auto["output"] == "never"
    assert load_lab_config(LAB.format(fraction=0.25, auto="never"), reg).instruments["smu"].auto == {"*": "never"}
    for fraction, auto, msg in [
        (2, "never", "auto_fraction: must be a number from 0 to 1"),
        (0.2, "{ V: [-20, 2] }", "exceeds limits"),
        (0.2, "{ I: never }", "auto rules belong to drives"),
        (0.2, "sometimes", "must be never or a mapping"),
    ]:
        with pytest.raises(ConfigError) as e:
            load_lab_config(LAB.format(fraction=fraction, auto=auto), reg)
        assert msg in str(e.value)
