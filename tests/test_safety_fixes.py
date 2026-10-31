"""Safety gaps the overnight test of 0.3.32 found (UX report 2026-10-30, section 3.2; testers t01, t02, t05, t07, t10),
each reproduced on the test lab: auto mode past a device's protection, changes that break an interlock written on the
reading, a stop that froze a magnet and left a laser on without a word, hold defeating an interlock, a queue sweeping a
broken gate again, a stage moving under someone's hands, cards that missed what mattered, one undo list for the whole
lab, and a PIN asked for tightening a limit."""
import json
import time

import pytest

from labharness import ScriptedApprover, cardlang, protection
from labharness.monitor import Monitor
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


# ------------------------------------------------------------------- 3. a stop never freezes a magnet

def test_a_stop_keeps_the_magnets_rate_so_the_field_can_come_down(lab, make_engine):
    """t02: a stop set magnet.rate to its "safe" 0 T/min: the field stayed at -2.5 T, and bringing it down needed an
    approval and then waited 10 minutes for a field that couldn't move."""
    engine, gate, _ = make_engine([True])
    stop_at_first_point(engine, gate.request_abort)
    report = engine.run(FIELD_AT.format(b=0.5))
    assert report.status == "aborted"
    assert lab.read("magnet", "rate") == 0.5 and "magnet.rate" not in report.message   # its rate stays
    assert gate.set("magnet", "B", 0).ok                                         # toward safe: no approval, it moves
    lab.instruments["magnet"].backend.state.update(rate=0.0, B=1.0, B_actual=1.0)
    t0 = lab.clock.now()
    r = gate.set("magnet", "B", 0)                                               # a zero rate: said at once
    assert r.action == "refused" and lab.clock.now() - t0 < 1
    assert r.message == "refused: magnet.rate is 0 T/min, so magnet.B can't move: set magnet.rate first"


def test_a_stopped_run_and_stop_run_say_what_is_still_on(tmp_path):
    """t02, t07: after a stop the chat never said where the field was, nor that the laser was still on."""
    rt = runtime(tmp_path, [True, True, True])
    for param, value in (("emission", True), ("power", 6), ("shutter", True)):
        assert rt.gate.set("laser", param, value).ok
    stop_at_first_point(rt.engine, rt.gate.request_abort)
    said = rt.run("plan: stage_scan\nsweep: { stage.position: {from: 0, to: 5, step: 1} }\n"
                  "measure: [laser.P_detected]\n")
    assert "aborted after" in said
    assert "still on: laser.emission on, laser.power 6 mW, laser.shutter open" in said
    said = rt.stop_work("stop")
    assert said.startswith("nothing to stop") and "still on: laser.emission on" in said


# ------------------------------------------------------------------- 4. hold doesn't defeat an interlock

def test_an_interlock_that_fails_sends_a_held_drive_to_safe_and_says_so(lab, make_engine):
    """t10: with safe_actions: {magnet.B: hold}, the interlock tripped and the field stayed at 1 T; the model only
    saw "stop requested"."""
    engine, gate, _ = make_engine([True])
    assert lab.config.safe_actions["magnet.B"] == "hold"
    notes = []
    monitor = Monitor(lab, gate, lambda *a: notes.append(a))

    def warm_up():
        hold_temperature(lab, 10.5)
        monitor.tick()
    stop_at_first_point(engine, warm_up)
    report = engine.run("plan: field\nsetup: { magnet.B: 0.5 }\ntime: { every: 30, points: 3 }\nmeasure: [lockin.X]\n")
    assert report.status == "aborted" and "stopped by the interlock on magnet.B (it needs tc.T_sample < 10" in \
        report.message
    assert lab.read("magnet", "B") == 0                                          # to safe, not held at 0.5 T
    assert [a.kind for a in monitor.alarms] == ["interlock"]


def test_an_interlock_alarm_does_not_flap_while_the_sample_sits_at_its_edge(lab, make_gate, monkeypatch):
    """t10: at the interlock's own 10 K the alarm went up and cleared six times in 15 s."""
    gate, _ = make_gate([])
    monkeypatch.setattr(gate, "abort_to_safe", lambda keys, by, hold=True: [])   # a drive that won't go to safe
    lab.instruments["magnet"].backend.state.update(B=1.0, B_actual=1.0)
    monitor = Monitor(lab, gate, lambda *a: None)
    for k in range(8):                                                           # 10.01 K, 9.99 K, ...
        hold_temperature(lab, 10.01 if k % 2 == 0 else 9.99)
        monitor.tick()
        lab.clock.sleep(5)
    assert len(monitor.alarms) == 1 and monitor.alarms[0].cleared is None
    hold_temperature(lab, 5.0)
    for _ in range(14):                                                          # holds again for a minute: clears
        monitor.tick()
        lab.clock.sleep(5)
    assert len(monitor.alarms) == 1 and monitor.alarms[0].cleared is not None


# ------------------------------------------------------------------- 5. a guard trip stops the queue

def test_a_guard_trip_pauses_the_queue_whatever_on_error_says(tmp_path):
    """t05: with on a failure: continue, a gate that broke down at 12.05 V was swept again by the next item."""
    rt = runtime(tmp_path, [True])
    rt.queue_set_on_error("continue")
    rt.queue_add(GATE_SWEEP.format(name="wide", top=13))
    rt.queue_add(GATE_SWEEP.format(name="again", top=12.5))
    assert rt.queue_start().startswith("queue started")
    rt._queue_thread.join(120)
    (first, second) = rt.queue.items
    assert first.status == "failed" and "tripped" in first.message and second.status == "waiting"
    assert rt.queue.state == "paused" and "a guard tripped, so the queue pauses" in rt.queue.reason
    [card] = rt.gate.approver.requests
    assert "If an item fails, the queue goes on to the next (on a failure: continue)" in str(card)


# ------------------------------------------------------------------- 6. Bring to safe

def test_bring_to_safe_sets_every_declared_safe_value_from_the_card_after_a_stop(tmp_path):
    """t07: Stop left the laser on and said nothing, and there was no "bring everything to safe"."""
    rt = runtime(tmp_path, [True, True, True, True])
    lab = rt.lab
    for param, value in (("emission", True), ("power", 6), ("shutter", True)):
        assert rt.gate.set("laser", param, value).ok
    assert rt.gate.set("tc", "heater_range", "low").ok
    lab.instruments["magnet"].backend.state.update(B=1.5, B_actual=1.5, rate=0.2)
    lab.instruments["stage"].backend.state.update(position=12.0, position_actual=12.0)
    rt.stop("human (portal)")
    deadline = time.time() + 10
    while (rt.incident or {}).get("kind") != "stop" and time.time() < deadline:
        time.sleep(0.05)
    lines = rt.incident_lines()
    assert lines[0].startswith("still on: laser.emission on, laser.power 6 mW, laser.shutter open")
    assert "magnet.B 1.5 T (B_actual 1.5 T)" in lines[0] and "tc.heater_range low" in lines[0]
    assert "magnet.rate" not in lines[0]
    said = rt.incident_safe("human (portal)")                                    # the card's Bring to safe
    assert not lab.read("laser", "emission") and lab.read("laser", "power") == 0 and not lab.read("laser", "shutter")
    assert lab.read("tc", "heater_range") == "off" and lab.read("magnet", "B") == 0   # the lab's hold is for stops
    assert lab.read("magnet", "rate") == 0.2 and lab.read("stage", "position") == 12.0   # rate and positions stay
    assert rt.incident is None and any("laser.emission on → off" in line for line in said)
    assert "bring_to_safe" in [e["type"] for e in rt.gate.log.events()]


def test_bring_to_safe_is_a_persons_in_the_portal(tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from labharness.web.app import STATIC, create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    rt = runtime(tmp_path, [True])
    assert rt.gate.set("smu", "output", True).ok
    home = tmp_path / "Kelvin"
    settings = Settings(home / "settings.json")
    client = TestClient(create_app(rt, settings, ChatStore(home / "chats"), hosts=["testserver"], providers={}))
    with client:
        r = client.post("/api/safe", json={}, headers={"X-Kelvin-Token": settings.access_key})
    assert r.status_code == 200 and not rt.lab.read("smu", "output")
    assert any("smu.output on → off" in line for line in r.json()["lines"])
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert '$("lab-safe").addEventListener("click", bringToSafe)' in js and '"/api/safe"' in js
    assert 'id="lab-safe"' in (STATIC / "index.html").read_text(encoding="utf-8")


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


# ------------------------------------------------------------------- 9. undo is each chat's own

def test_undo_takes_back_only_this_chats_own_direct_changes(tmp_path):
    """t07, t10: undo_last walked back a run's sweep steps, then switched another chat's heater back on, and auto mode
    approved it."""
    rt = runtime(tmp_path, [True, True, False])
    rt.attribution = {"chat": "c1"}
    assert "(approved)" in rt.set("tc", "heater_range", "low")
    assert "toward safe" in rt.set("tc", "heater_range", "off")
    rt.attribution = {"chat": "c2"}
    assert "(approved)" in rt.set("smu", "V", 1)
    assert "done" in rt.run("plan: scan\nsweep: { stage.position: {values: [1, 2]} }\nmeasure: [laser.P_detected]\n")
    assert rt.undo() == "undo: smu.V 1 V → 0 V, 2 s (toward safe)"               # not the run's stage steps
    assert rt.undo().startswith("nothing to undo")                               # nor chat c1's heater
    assert rt.lab.read("stage", "position") == 2 and rt.lab.read("tc", "heater_range") == "off"
    rt.attribution = {"chat": "c1"}
    rt.gate.auto = True
    said = rt.undo()                                                             # off → low: a person decides
    assert said.startswith("undo: declined") and rt.lab.read("tc", "heater_range") == "off"
    card = rt.gate.approver.requests[-1]
    assert card.title == "Undo: Approve drive change on tc?"
    assert any(i.startswith("Undo of this chat's change at ") and "tc.heater_range low → off" in i for i in card.items)
    assert "it undoes an earlier change, which a person approves" in card.items[-1]
    assert cardlang.card(card.title, card.items, None, "zh-Hans")["english"] == 0
    rt.gate.auto = False
    rt.gate.approver.answers += [True]
    assert "(approved)" in rt.set("smu", "V", 2)                                 # c1 sets 2 V; another chat 1.5 V
    rt.attribution = {"chat": "c3"}
    assert "toward safe" in rt.set("smu", "V", 1.5)
    rt.attribution = {"chat": "c1"}
    assert rt.undo().startswith("nothing undone: smu.V is 1.5 V now, not the 2 V this chat set at ")
    assert rt.lab.read("smu", "V") == 1.5


# ------------------------------------------------------------------- 10. tightening a limit takes no PIN

def test_tightening_a_limit_takes_no_pin_and_widening_does(tmp_path):
    """t10: narrowing ±10 V to ±5 V asked for the owner's PIN, though the docs say narrowing never does."""
    pytest.importorskip("starlette")
    from test_governance import LAB
    from starlette.testclient import TestClient
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from labharness.adapters import set_resource_manager
    from test_hardware import Bus, Device
    set_resource_manager(Bus({"GPIB0::24::INSTR": Device("keithley/2400", name="k"),
                              "GPIB0::8::INSTR": Device("srs/sr830", name="l")}))
    try:
        home = tmp_path / "Kelvin"
        rt = LabRuntime(build_virtual_lab(seed=1), WebApprover(timeout=5), home / "lab")
        settings = Settings(home / "settings.json")
        client = TestClient(create_app(rt, settings, ChatStore(home / "lab" / "chats", create=False),
                                       hosts=["testserver"], providers={}))
        h = {"X-Kelvin-Token": settings.access_key}
        with client:
            assert client.post("/api/lab", headers=h, json={"yaml": LAB}).json()["ok"]
            assert client.post("/api/owner-pin", headers=h, json={"pin": "2468"}).json() == {"set": True}
            r = client.post("/api/instruments/smu/limits", headers=h, json={"limits": {"V": [-3, 3]}})
            assert r.status_code == 200 and rt.lab.instruments["smu"].limits("V") == (-3.0, 3.0)   # tighter: no PIN
            r = client.post("/api/instruments/smu/limits", headers=h, json={"limits": {"V": [-8, 8]}})
            assert r.status_code == 403 and r.json()["pin"] and rt.lab.instruments["smu"].limits("V") == (-3.0, 3.0)
            r = client.post("/api/instruments/smu/limits", headers=h, json={"limits": {"V": None}})
            assert r.status_code == 403 and r.json()["pin"]                       # taking it out widens too
    finally:
        set_resource_manager(None)


# ------------------------------------------------------------------- review of 0.3.34: an interlock's to-safe stays

def warm_on_the_third_poll(lab, gate, monitor):
    """The sample warms past magnet.B's interlock while a wait polls, and the monitor acts before the wait wakes: the
    monitor's thread winning the race, made deterministic."""
    pause, polls = gate._pause, []

    def pausing(seconds, gen):
        polls.append(seconds)
        if len(polls) == 3:
            hold_temperature(lab, 10.5)
            monitor.tick()
        pause(seconds, gen)
    gate._pause = pausing


def test_a_stopped_wait_never_undoes_an_interlocks_to_safe(lab, make_engine):
    """Review of 0.3.34: the interlock sent magnet.B to 0, then the run's wait it stopped set it back to where the
    field was, and the monitor, its alarm up, never acted again: the field stayed up above its interlock."""
    engine, gate, _ = make_engine([True])
    monitor = Monitor(lab, gate, lambda *a: None)
    warm_on_the_third_poll(lab, gate, monitor)
    report = engine.run("plan: field\nsetup: { magnet.rate: 0.5, magnet.B: 1.0 }\ntime: { every: 30, points: 3 }\n"
                        "measure: [lockin.X]\n")
    assert report.status == "aborted" and "it goes on to safe, where it was sent meanwhile" in report.message
    assert lab.read("magnet", "B") == 0                                          # on its way to safe, not held
    lab.clock.sleep(30)
    monitor.tick()
    assert lab.read("magnet", "B_actual") == 0 and monitor.alarms[0].cleared is not None


def test_while_an_interlock_is_broken_its_drive_goes_on_to_safe(lab, make_gate):
    """Review of 0.3.34: the same race on a chat's direct change, which no run's clean-up follows; and a set point
    put away from safe again while the alarm is up goes back to safe at the next check."""
    gate, _ = make_gate([True])
    lab.config.safe_actions.pop("magnet.B")
    lab.instruments["magnet"].backend.state.update(rate=0.5)
    monitor = Monitor(lab, gate, lambda *a: None)
    warm_on_the_third_poll(lab, gate, monitor)
    r = gate.set("magnet", "B", 2.0)
    assert r.action == "stopped" and r.message.endswith("it goes on to safe, where it was sent meanwhile")
    assert lab.read("magnet", "B") == 0
    lab.instruments["magnet"].write("B", 0.5)                                    # a hold that came late
    lab.clock.sleep(10)
    monitor.tick()
    alarm = monitor.alarms[0]
    assert lab.read("magnet", "B") == 0 and alarm.cleared is None
    assert any(a.startswith("magnet.B 500 mT → 0 T (safe state)") for a in alarm.actions)


def test_the_interlock_alarm_clears_on_the_field_not_its_set_point(lab, make_gate):
    """Review of 0.3.34: the alarm said "the drive is at safe" once magnet.B was set to 0, the field still 1.5 T."""
    gate, _ = make_gate([])
    lab.config.safe_actions.pop("magnet.B")
    lab.instruments["magnet"].backend.state.update(B=1.5, B_actual=1.5, rate=0.2)
    monitor = Monitor(lab, gate, lambda *a: None)
    hold_temperature(lab, 12)
    monitor.tick()
    alarm = monitor.alarms[0]
    assert "magnet.B is at 1.5 T, which needs tc.T_sample < 10" in alarm.text and lab.read("magnet", "B") == 0
    lab.clock.sleep(60)
    monitor.tick()
    assert alarm.cleared is None and lab.read("magnet", "B_actual") > 1           # set to 0, but the field is up
    lab.clock.sleep(420)
    monitor.tick()
    assert alarm.cleared is not None and lab.read("magnet", "B_actual") == 0


# ------------------------------------------------------------------- review of 0.3.34: a rate that freezes a drive

def test_a_rate_that_freezes_the_field_asks_a_person_and_says_so(lab, make_gate, monkeypatch):
    """Review of 0.3.34: magnet.rate 0.2 → 0 T/min with the field at 1.5 T counted as "toward safe", so nobody was
    asked, and a failed interlock's to-safe then couldn't bring the field down. A supply whose card calls its slowest
    rate safe (a Lake Shore 625's 0.0001 A/s) had the same trap."""
    gate, approver = make_gate([False, True, True])
    gate.auto = True
    magnet = lab.instruments["magnet"].backend.state
    magnet.update(B=1.5, B_actual=1.5, rate=0.2)
    r = gate.set("magnet", "rate", 0)
    assert r.action == "declined" and lab.read("magnet", "rate") == 0.2
    items = approver.requests[0].items
    assert "at 0 T/min magnet.B can't move, so a stop, an interlock or Bring to safe couldn't bring it back" in items[0]
    assert items[-1].startswith("Auto mode does not cover this")                # never Kelvin's own yes
    r = gate.set("magnet", "rate", 0.001)                                      # 25 h back from 1.5 T: asks too
    assert r.ok and "magnet.B would take 25.0 h to get back to safe (its card allows 2.0 h for a move)" in r.message
    magnet.update(B=0.0, B_actual=0.0)                                         # at safe: nothing to freeze
    assert gate.set("magnet", "rate", 0).ok and len(approver.requests) == 2
    monkeypatch.setattr(lab.instruments["magnet"].card.params["rate"], "safe", 0.0001)
    magnet.update(B=1.5, B_actual=1.5, rate=0.2)
    r = gate.set("magnet", "rate", 0.0001)                                     # its "safe" value: 250 h back
    assert r.ok and "would take" in r.message and len(approver.requests) == 3
    magnet.update(rate=0.2)
    assert gate.set("magnet", "rate", 0.1).ok and len(approver.requests) == 3    # slower, still back in 15 min


def test_a_plan_that_zeroes_the_rate_with_the_field_up_asks_even_in_auto_mode(lab, make_engine):
    engine, gate, approver = make_engine([True])
    gate.auto = True
    lab.instruments["magnet"].backend.state.update(B=1.5, B_actual=1.5, rate=0.2)
    report = engine.run("plan: hold_field\nsetup: { magnet.rate: 0 }\ntime: { every: 10, points: 1 }\n"
                        "measure: [lockin.X]\n")
    assert report.status == "done" and lab.read("magnet", "rate") == 0
    items = approver.requests[0].items
    assert any(i.startswith("magnet.rate: now ") and "at 0 T/min magnet.B can't move" in i for i in items)
    assert any(i.startswith("Auto mode does not cover this run: magnet.rate: at 0 T/min") for i in items)


def test_what_a_stop_leaves_on_says_a_field_still_up(tmp_path):
    """Review of 0.3.34: with magnet.B set to 0 and the field still 2.5 T (ramping down, or frozen by a rate of 0),
    stop_run said nothing was left on, and Bring to safe said nothing either."""
    rt = runtime(tmp_path)
    magnet = rt.lab.instruments["magnet"].backend.state
    magnet.update(B=0.0, B_actual=2.5, rate=0.2)
    with rt.lock:
        assert rt.left_on() == ["magnet.B 0 T (B_actual 2.5 T)"]
    assert "still on: magnet.B 0 T (B_actual 2.5 T)" in rt.stop_work("stop")
    magnet.update(rate=0.0)
    said = rt.bring_to_safe("human (portal)")
    assert said[-1] == ("still on: magnet.B 0 T (B_actual 2.5 T; magnet.rate is 0 T/min, so magnet.B can't move: "
                        "set magnet.rate first)")


# ------------------------------------------------------------------- review of 0.3.34: held stages, clean-up too

def test_hands_on_the_sample_holds_a_positioner_through_a_stops_clean_up(lab, make_engine, monkeypatch):
    """Review of 0.3.34: with hands on the sample, a stopped run's clean-up still drove a positioner's drive to safe
    (a piezo scanner's x, stood in for by the smu); the laser, which is no stage, still goes to safe."""
    engine, gate, _ = make_engine([True, True])
    monkeypatch.setattr(lab.instruments["smu"].card, "kind", "positioner")
    assert gate.set("laser", "emission", True).ok

    def hands_on():
        lab.hands_on = {"by": "human (portal)", "since": "now"}
        gate.request_abort("hands on the sample")
    stop_at_first_point(engine, hands_on)
    report = engine.run(GATE_SWEEP.format(name="scan", top=2))
    assert report.status == "aborted" and lab.read("smu", "V") == 0 and lab.read("smu", "output") is True
    assert "smu.output stays where it is: human (portal) has hands on the sample" in report.message
    lab.instruments["smu"].backend.state.update(V=0.5)
    assert [r.message.split(":")[0] for r in gate.to_safe("smu")] == [
        "smu.V stays where it is", "smu.output stays where it is", "smu.compliance_I stays where it is"]
    assert gate.cut("smu", "V", "monitor")[0].action == "held" and lab.read("smu", "V") == 0.5
    assert gate.to_safe("laser") and lab.read("laser", "emission") is False
