"""Sensing fails closed (field tests: an open thermometer read 0 K, which satisfied "T < 10 K", raised no alarm and
gave an auto-fit a "peak"; a NaN leakage passed its guard; a leakage trip set V = 0 on a source in current mode;
interlocks were checked only when their own drive moved; "toward safe" skipped approval on unreviewed draft cards)."""
import math

from labharness.conditions import parse_condition
from labharness.labconfig import Interlock
from labharness.monitor import Monitor, Watch
from labharness.homelab import builtin_registry


def open_sensor(lab):
    """The virtual thermometer's leads come off: it reads 0 K, as an open Lake Shore input does."""
    lab.read("tc", "T_sample")
    lab.instruments["tc"].backend.state["T_sample"] = 0.0
    lab.instruments["tc"].backend.evolve = lambda dt: None   # and stays there


def test_a_guard_reading_of_nan_or_none_at_all_trips_the_ramp(lab, make_gate):
    gate, approver = make_gate([True] * 6)
    smu = lab.instruments["smu"].backend
    assert gate.set("smu", "output", True).ok
    smu.measure_I = lambda: math.nan
    r = gate.set("smu", "V", 5)
    assert r.action == "tripped" and "smu.I can't be read (I needs a finite number)" in r.message
    assert lab.read("smu", "V") == 0 and lab.read("smu", "output") is False
    real = lab.read                                                 # a driver that hands back a float NaN itself
    lab.read = lambda instr, param: math.nan if (instr, param) == ("smu", "I") else real(instr, param)
    assert gate.set("smu", "output", True).ok
    r = gate.set("smu", "V", 5)
    assert r.action == "tripped" and "smu.I reads nan, not a number" in r.message and lab.read("smu", "V") == 0
    lab.read = real

    def dead():
        raise TimeoutError("smu: no response to 'MEAS:CURR?'")
    smu.measure_I = dead
    assert gate.set("smu", "output", True).ok
    r = gate.set("smu", "V", 5)
    assert r.action == "tripped" and "smu.I can't be read" in r.message and lab.read("smu", "V") == 0


def test_a_leakage_trip_switches_the_output_off_as_well(lab, make_gate):
    gate, _ = make_gate([True, True])
    gate.set("smu", "output", True)
    r = gate.set("smu", "V", 14)                                  # the toy gate breaks down above 12 V
    assert r.action == "tripped" and "back to 0 V; output off" in r.message
    assert lab.read("smu", "V") == 0 and lab.read("smu", "output") is False


def test_an_open_thermometer_is_an_error_and_fails_the_interlock(lab, make_gate):
    gate, approver = make_gate([True])
    open_sensor(lab)
    r = gate.get("tc", "T_sample")
    assert r.action == "error" and "can't really be" in r.message
    r = gate.set("magnet", "B", 1)
    assert r.action == "refused" and "interlock" in r.message and "tc.T_sample can't be read" in r.message
    assert approver.requests == []


def test_the_lake_shore_cards_read_their_sensor_status():
    for card_id in ("lakeshore/336", "lakeshore/350"):
        spec = builtin_registry().get(card_id).params["T_A"]
        assert spec.status_cmd.render() == "RDGST? A" and spec.plausible[0] > 0


def test_the_monitor_enforces_an_interlock_whose_reading_drifts_out_or_dies(lab, make_gate):
    gate, approver = make_gate([True, True])
    lab.config.safe_actions.pop("magnet.B")                       # the virtual lab holds its field: send it to 0 here
    notices = []
    monitor = Monitor(lab, gate, lambda kind, title, text="": notices.append((kind, title, text)))
    monitor.tick()
    assert gate.set("magnet", "B", 0.5).ok
    gen = gate.stop_generation
    lab.clock.sleep(10)
    lab.read("tc", "T_sample")
    lab.instruments["tc"].backend.state["T_sample"] = 25.0         # the fridge warms up while the field is on
    monitor.tick()
    alarm = monitor.active()[0]
    assert alarm.kind == "interlock" and "magnet.B is at 500 mT, which needs tc.T_sample < 10 (now 25" in alarm.text
    assert gate.stop_generation != gen and "stopped the running plan" in alarm.actions
    assert lab.read("magnet", "B") == 0 and any("magnet.B 500 mT → 0 T" in a for a in alarm.actions)
    lab.clock.sleep(10)
    monitor.tick()
    assert alarm.cleared                                          # the field is at safe: nothing left to protect

    lab.instruments["tc"].backend.state["T_sample"] = 2.0
    assert gate.set("magnet", "B", 0.5).ok
    open_sensor(lab)
    lab.clock.sleep(10)
    monitor.tick()
    assert "tc.T_sample can't be read" in monitor.active()[0].text


def test_setting_an_interlocks_condition_is_checked_against_it(lab, make_gate):
    gate, approver = make_gate([True, True, True])
    lab.config.interlocks.append(Interlock("laser.emission", parse_condition("tc.setpoint < 10")))
    assert gate.set("tc", "setpoint", 50, wait=False).ok           # the laser is off: nothing to protect
    assert gate.set("tc", "setpoint", 2, wait=False).ok
    assert gate.set("laser", "emission", True).ok
    r = gate.set("tc", "setpoint", 50, wait=False)
    assert r.action == "refused" and "laser.emission (now on) needs tc.setpoint < 10" in r.message
    assert lab.read("tc", "setpoint") == 2 and len(approver.requests) == 2


def test_a_watch_on_a_dead_sensor_alarms_and_acts_at_once(lab, make_gate):
    gate, _ = make_gate([True])
    monitor = Monitor(lab, gate, lambda *a: None)
    monitor.add(Watch("tc.T_sample", above=20, every=1, then=[("laser", "emission", False)], stop_run=True,
                      source="lab"))
    assert gate.set("laser", "emission", True).ok
    open_sensor(lab)
    monitor.tick()
    alarm = monitor.active()[0]
    assert "tc.T_sample can't be read" in alarm.text and lab.read("laser", "emission") is False


def test_a_watch_that_zeroes_a_level_switches_its_output_off_too(lab, make_gate):
    gate, _ = make_gate([True, True])
    monitor = Monitor(lab, gate, lambda *a: None)
    monitor.add(Watch("smu.I", above=1e-6, every=1, then=[("smu", "V", 0.0)], source="lab"))
    gate.set("smu", "output", True)
    gate.set("smu", "V", 2)
    lab.instruments["smu"].backend.measure_I = lambda: 1e-5       # a source someone switched to current mode
    monitor.tick()
    assert lab.read("smu", "V") == 0 and lab.read("smu", "output") is False


def test_on_an_unreviewed_draft_card_even_toward_safe_asks(lab, make_gate):
    gate, approver = make_gate([True, False, True])
    lab.instruments["smu"].backend.simulated = False
    lab.instruments["smu"].card.status = "draft"
    assert gate.set("smu", "V", 1).ok
    r = gate.set("smu", "V", 0.5)
    assert r.action == "declined" and "even a change toward its safe value (0 V) asks" in str(approver.requests[1])
    assert gate.set("smu", "V", 0.5).ok and len(approver.requests) == 3
    items, grants, problems = gate.plan_approval({"smu.V": [0.2, 0.0]})   # a plan's one approval covers it
    assert "smu.V" in grants and not problems
