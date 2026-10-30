"""Safety gaps the overnight test of 0.3.32 found (UX report 2026-10-30, section 3.2; testers t01, t02, t05, t07, t10),
each reproduced on the test lab: auto mode past a device's protection, changes that break an interlock written on the
reading, a stop that froze a magnet and left a laser on without a word, hold defeating an interlock, a queue sweeping a
broken gate again, a stage moving under someone's hands, cards that missed what mattered, one undo list for the whole
lab, and a PIN asked for tightening a limit."""
import json

import pytest

from labharness import ScriptedApprover, protection
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
