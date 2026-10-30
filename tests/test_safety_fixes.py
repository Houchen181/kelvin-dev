"""Safety gaps the overnight test of 0.3.32 found (UX report 2026-10-30, section 3.2; testers t01, t02, t05, t07, t10),
each reproduced on the test lab: auto mode past a device's protection, changes that break an interlock written on the
reading, a stop that froze a magnet and left a laser on without a word, hold defeating an interlock, a queue sweeping a
broken gate again, a stage moving under someone's hands, cards that missed what mattered, one undo list for the whole
lab, and a PIN asked for tightening a limit."""
import json

import pytest

from labharness import ScriptedApprover, cardlang, protection
from simlab import build_virtual_lab

GATE_D1 = {"gates": [{"drive": "smu.V", "dielectric": "hBN", "thickness_nm": 20, "leak": "smu.I"}]}   # ±6 V

FIELD_AT = """plan: field_points
setup: {{ magnet.rate: 0.5 }}
sweep: {{ magnet.B: {{values: [{b}, 1]}} }}
measure: [lockin.X]
"""

GATE_SWEEP = """plan: {name}
setup: {{ smu.output: on }}
sweep: {{ smu.V: {{from: 0, to: {top}, step: 0.5}} }}
measure: [smu.I]
after: {{ smu.V: 0 }}"""


def runtime(tmp_path, answers=()):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    return LabRuntime(build_virtual_lab(seed=1), ScriptedApprover(list(answers)), tmp_path / "data", None)


def asks(tmp_path):
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["type"] == "ask"]


def hold_temperature(lab, kelvin):
    """The sample sits at this temperature (the heater holds it there)."""
    lab.instruments["tc"].backend.state.update(heater_range="medium", setpoint=kelvin, T_sample=kelvin)


def stop_at_first_point(engine, stop):
    before = engine.progress

    def progress(event):
        if before is not None:
            before(event)
        if event["event"] == "point":
            stop()
    engine.progress = progress


# ------------------------------------------------------------------- 1. auto mode stays inside a device's protection

def test_auto_mode_stays_inside_the_mounted_devices_protection(tmp_path):
    """t01: D1 protected to ±6 V (20 nm hBN); the lab's own auto range ±5 V let 5 V through unasked."""
    rt = runtime(tmp_path, [False, False])
    rt.mount(None, {"id": "d1", "name": "D1", "protection": protection.normalize(GATE_D1, rt.lab)})
    rt.gate.auto = True
    assert rt.gate.set("smu", "V", 5).action == "declined"                       # asked: 83% of D1's limit
    assert rt.gate.approver.requests[-1].items[-1] == (
        "Auto mode does not cover this: smu.V goes 83% of the way from safe to device D1's limit, [-6, 6] V "
        "(auto mode covers 25%)")
    assert "(auto-approved)" in rt.gate.set("smu", "V", 1).message                 # 17%: low risk, inside both
    note = asks(rt.data_dir)[-1]["note"]
    assert "stays in its auto range -5 … 5 V" in note and "device D1's limit, [-6, 6] V" in note
    said = rt.run(GATE_SWEEP.format(name="to_four", top=4))                       # a run is judged the same way
    assert "declined: plan to_four not run" in said
    assert "smu.V goes 67% of the way from safe to device D1's limit" in str(rt.gate.approver.requests[-1])


# ------------------------------------------------------------------- 2. changes that would break an interlock

def test_a_set_point_that_drives_an_interlocks_reading_past_it_is_refused_never_auto_approved(lab, make_gate,
                                                                                            tmp_path):
    """t10: with the magnet at 0.75 T (interlock: tc.T_sample < 10), tc.setpoint 70 K was auto-approved."""
    gate, approver = make_gate([])
    gate.auto = True
    lab.instruments["magnet"].backend.state.update(B=0.75, B_actual=0.75)
    r = gate.set("tc", "setpoint", 70, wait=False)
    assert r.action == "refused" and lab.read("tc", "setpoint") != 70
    assert r.message == ("refused: interlock: magnet.B (now 750 mT) needs tc.T_sample < 10; setting tc.setpoint to "
                         "70 K (it takes tc.T_sample there) would break it, so bring magnet.B to safe first")
    assert not approver.requests and not asks(tmp_path)
    assert gate.set("tc", "setpoint", 5, wait=False).ok                           # inside it: fine


def test_cooling_while_an_interlock_fails_already_is_not_refused(lab, make_gate):
    """The monitor brings the field down; a set point that takes the sample no further from 10 K still goes."""
    gate, _ = make_gate([])
    hold_temperature(lab, 12.0)
    lab.instruments["magnet"].backend.state.update(B=0.5, B_actual=0.5)
    assert gate.set("tc", "setpoint", 11, wait=False).ok
    assert gate.set("tc", "setpoint", 15, wait=False).action == "refused"


def test_validate_plan_and_run_plan_foresee_a_plan_step_that_breaks_an_interlock(tmp_path):
    """t10: validate_plan said ok to setup {magnet.B: 0.5, tc.setpoint: 15}; t02: a temperature series with a 12 K
    step at -2.5 T ran 1.6 h of lab time before the interlock stopped it."""
    rt = runtime(tmp_path, [])
    said = rt.validate("plan: warm\nsetup: {magnet.B: 0.5, tc.heater_range: low, tc.setpoint: 15}\n"
                       "measure: [lockin.X]\n")
    assert ("will be refused: interlock: magnet.B needs tc.T_sample < 10, but it would be at 500 mT while "
            "tc.T_sample is at 15 K (the plan's setup sets tc.setpoint to 15 K)") in said
    series = ("plan: t_series\nsetup: {tc.heater_range: medium}\n"
              "sweep: {tc.setpoint: {values: [2, 4, 6, 8, 12]}, magnet.B: {values: [-2.5, 2.5]}}\n"
              "measure: [lockin.X]\n")
    t0 = rt.lab.clock.now()
    said = rt.run(series)
    assert said.startswith("refused: interlock: magnet.B needs tc.T_sample < 10") and "12 K" in said
    assert rt.lab.clock.now() - t0 < 60 and rt.lab.read("magnet", "B") == 0          # nothing ran
    ok = rt.validate("plan: warm_up\nsetup: {magnet.B: 0}\nsweep: {tc.setpoint: {values: [2, 300]}}\n"
                     "measure: [lockin.X]\n")
    assert "will be refused" not in ok                                               # the field is down first


def test_a_queue_whose_own_step_breaks_an_interlock_does_not_start(tmp_path):
    """t05: a warm-up step before a field sweep; the rehearsal said it would fail, the real card said nothing."""
    rt = runtime(tmp_path, [])
    rt.queue_add("step: warm to 15 K\nset: { tc.heater_range: medium, tc.setpoint: 15 }")
    rt.queue_add(FIELD_AT.format(b=0.5))
    rehearsed = rt.queue_start(dry_run=True)
    assert "would be refused: interlock: magnet.B needs tc.T_sample < 10" in rehearsed
    said = rt.queue_start()
    assert said.startswith("the queue did not start") and "#1 warm to 15 K sets tc.setpoint to 15 K" in said
    assert rt.queue.state != "running" and not rt.gate.approver.requests


def test_a_card_says_what_heating_the_sample_sets_off_and_auto_mode_leaves_it_to_a_person(lab, make_gate):
    """t07: heating to 25 K with the laser on said nothing of the laser's 20 K guard or the lab's 20 K watch."""
    gate, approver = make_gate([True, True, False])
    assert gate.set("laser", "emission", True).ok and gate.set("laser", "power", 5).ok
    gate.auto = True
    assert gate.set("tc", "setpoint", 25, wait=False).action == "declined"
    card = approver.requests[-1].items
    assert "at 25 K, laser.power's guard (|tc.T_sample| ≤ 20 K) fails while laser.power is 5 mW" in card
    assert "at 25 K, the lab watch on tc.T_sample (above 20 K) sets laser.emission off" in card
    assert card[-1] == "Auto mode does not cover this: it sets off what is listed above"
    assert cardlang.card(approver.requests[-1].title, card, None, "zh-Hans")["english"] == 0   # read in Chinese too


# ------------------------------------------------------------------- 7. hands on the sample holds a stage

def test_hands_on_the_sample_holds_the_motion_stage(tmp_path):
    """t07: with the lock on, stage.position 24 → 30 mm went ahead; in optics the stage carries the sample."""
    rt = runtime(tmp_path, [])
    rt.hands_on("human (portal)")
    r = rt.gate.set("stage", "position", 30)
    assert r.action == "refused" and "nothing on stage moves until a person unlocks it" in r.message
    assert rt.gate.raw("stage", "MOVE:ABSolute 30").action == "refused"
    assert rt.gate.raw("stage", "*RST").action == "refused"
    assert rt.gate.get("stage", "position_actual").ok                            # reading still works
    rt.hands_off("human (portal)")
    assert rt.gate.set("stage", "position", 30).ok


# ------------------------------------------------------------------- 8. what a card must say

def test_a_card_shows_a_guards_reading_now_and_a_drive_whose_output_is_off(lab, make_gate, make_engine):
    """t07: a laser ramp approved with the sample already at 25 K, past its 20 K guard; t01, t10: a gate at 5 V with
    its output off, which the card and the result never said."""
    gate, approver = make_gate([True])
    r = gate.set("smu", "V", 2)
    assert approver.requests[-1].items[0].endswith("; smu.output is off, so smu.V won't reach the sample")
    assert r.message.endswith("; smu.output is off, so smu.V won't reach the sample")
    hold_temperature(lab, 25.0)
    r = gate.set("laser", "power", 5)                                            # it would trip at its first step
    assert r.action == "refused" and r.message == ("refused: laser.power: its guard needs |tc.T_sample| ≤ 20 K, and "
                                                   "it reads 25 K now, so it would trip at once")
    engine, _, approver = make_engine([False])
    engine.run("plan: light\nsetup: { laser.emission: on, laser.power: 2 }\ntime: { every: 1, points: 2 }\n"
               "measure: [laser.P_detected]\n")
    assert "stops if |tc.T_sample| > 20 K (now 25 K, past it)" in str(approver.requests[-1])


def test_the_queue_card_says_its_dry_run_just_failed(tmp_path):
    """t05: the rehearsal said two items would fail; minutes later the real card said nothing of it."""
    rt = runtime(tmp_path, [False])
    rt.queue_add(GATE_SWEEP.format(name="wide", top=13))                         # breaks down above 12 V
    assert "would fail: #1 wide" in rt.queue_start(dry_run=True)
    assert rt.queue_start().startswith("declined")
    assert "Its dry run at" in str(rt.gate.approver.requests[-1]) and "#1 wide" in str(rt.gate.approver.requests[-1])
