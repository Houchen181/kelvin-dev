"""The set-point watchdog (plan item 9): a set point an instrument ramps to by itself whose reading stops getting
closer raises an alarm, and a wait for it ends, instead of holding a run for hours with nobody told. The field
tests' check runs on labsim: a PPMS that stalls on a cool-down raises an alarm within 10 minutes."""
from labsim_bench import ctl, labsim_lab, labsim_offline  # noqa: F401  (the fixtures)

from labharness import EventLog, SafetyGate, ScriptedApprover
from labharness.monitor import Approach, Monitor

PPMS_LAB = """lab: labsim PPMS
instruments:
  ppms:
    card: qd/multivu
    address: "qd://127.0.0.1:5000"
    limits: { T_set: [1.8, 400], B_set: [-9, 9], T_rate: [0.01, 20], B_rate: [1e-05, 0.01] }
  isrc:
    card: keithley/6221
    address: "GPIB0::12::INSTR"
    limits: { I: [-1e-5, 1e-5] }
  nvm:
    card: keithley/2182a
    address: "GPIB0::7::INSTR"
"""


def watching(lab, tmp_path, answers=()):
    gate = SafetyGate(lab, ScriptedApprover(answers), EventLog(tmp_path / "events.jsonl", lab.clock))
    notices = []
    monitor = Monitor(lab, gate, lambda kind, title, text="": notices.append((kind, title, text)))
    return gate, monitor, notices


def run_for(lab, monitor, seconds, step=5.0):
    end = lab.clock.now() + seconds
    while lab.clock.now() < end - 1e-9:
        lab.clock.sleep(step)
        monitor.tick()


def test_a_stalled_ppms_raises_an_alarm_within_ten_minutes(labsim_lab, tmp_path):
    """F2 l03, l09: a PPMS that stopped short of its set point held a run (its current source on) for 3 h, and would
    have waited out a 4 h timeout, with no alarm. The watchdog is quiet while the PPMS cools at its rate, alarms
    once its reading stops getting closer, and clears when it moves on."""
    lab, bench = labsim_lab("ppms", PPMS_LAB)
    gate, monitor, notices = watching(lab, tmp_path)
    assert gate.set("ppms", "T_rate", 10).ok
    assert gate.set("ppms", "T_set", 2, wait=False).ok   # toward safe (1.8 K): it cools, nobody waits
    run_for(lab, monitor, 15 * 60)   # 10 K/min: 150 K down, never an alarm while it cools at its rate
    assert not monitor.alarms and 140 < lab.read("ppms", "T") < 160
    ctl(bench, "set", "ppms.t_rate", 0)   # it stops on its way (its rate gone: no helium, a compressor that quit)
    stalled_at = lab.clock.now()
    while not monitor.alarms and lab.clock.now() - stalled_at < 20 * 60:
        run_for(lab, monitor, 5)
    assert monitor.alarms, "no alarm 20 min after the PPMS stalled"
    alarm = monitor.alarms[0]
    assert lab.clock.now() - stalled_at <= 10 * 60
    assert alarm.kind == "stall" and "ppms.T has not got closer to 2 K for 5 min" in alarm.text
    assert "set point ppms.T_set" in alarm.text and alarm.cleared is None
    assert notices[-1][0] == "alarm" and "may be stuck" in notices[-1][2]
    run_for(lab, monitor, 120)
    assert len(monitor.alarms) == 1 and len(notices) == 1   # raised once
    ctl(bench, "set", "ppms.t_rate", 10)                    # it moves again
    run_for(lab, monitor, 60)
    assert alarm.cleared and "getting closer to 2 K again" in notices[-1][1]


def test_a_wait_for_a_stalled_set_point_ends_instead_of_holding_the_lab_for_hours(labsim_lab, tmp_path):
    """A plain set point waits for its reading (up to the card's 4 h on a PPMS). One whose reading stops getting
    closer ends the wait after 10 min and says why, so a run or a chat isn't held for hours."""
    lab, bench = labsim_lab("ppms", PPMS_LAB)
    gate, monitor, notices = watching(lab, tmp_path)
    assert gate.set("ppms", "T_rate", 10).ok
    lab.clock.at(60, lambda: ctl(bench, "set", "ppms.t_rate", 0))   # a minute into the cool-down it stops
    t0 = lab.clock.now()
    r = gate.set("ppms", "T_set", 200)    # toward safe: waits for the reading
    assert not r.ok and r.action == "timeout"
    assert "ppms.T_set: T has not got closer to 200 K for 10.0 min (it reads 2" in r.message
    assert "the instrument may be stuck, so Kelvin stopped waiting" in r.message
    assert lab.clock.now() - t0 < 12 * 60   # not the card's 4 h


def test_a_slow_ramp_and_a_lab_at_rest_never_alarm(lab, tmp_path):
    """Only what Kelvin set is followed, until it gets there; a slow ramp still gets closer. The virtual magnet at
    0.02 T/min for 20 min, then nothing: no alarm, and none for the temperature controller sitting at its default."""
    gate, monitor, notices = watching(lab, tmp_path, answers=[True, True])
    assert gate.set("magnet", "rate", 0.02).ok
    assert gate.set("magnet", "B", 0.5, wait=False).ok
    run_for(lab, monitor, 20 * 60, step=15)
    assert not monitor.alarms and 0.3 < lab.read("magnet", "B_actual") < 0.5
    run_for(lab, monitor, 10 * 60, step=15)   # there at 25 min: done with it
    assert lab.read("magnet", "B_actual") == 0.5 and not monitor.alarms and monitor._moves["magnet.B"].arrived


def test_a_heater_range_too_low_for_its_set_point_alarms(lab, tmp_path):
    """F1 [3]: a set point the heater can't reach (its range too low) held the chat for its whole timeout. The reading
    stops short at what the range allows: an alarm after 5 min, and a wait for it ends after 10."""
    gate, monitor, notices = watching(lab, tmp_path, answers=[True, True])
    assert gate.set("tc", "heater_range", "low").ok       # the virtual controller's low range tops out at 10 K
    assert gate.set("tc", "setpoint", 40, wait=False).ok
    run_for(lab, monitor, 9 * 60, step=15)
    alarm = monitor.alarms[0]
    assert alarm.kind == "stall" and "tc.T_sample has not got closer to 40 K" in alarm.text
    t0 = lab.clock.now()
    r = gate.set("tc", "setpoint", 30)                     # a wait for it now ends instead of taking 30 min
    assert not r.ok and "T_sample has not got closer to 30 K for 10.0 min" in r.message
    assert lab.clock.now() - t0 < 11 * 60
    assert len([a for a in monitor.alarms if a.cleared is None]) == 1
    run_for(lab, monitor, 30)
    assert alarm.cleared and "new set point" in notices[-1][1]   # 30 K is another set point: followed afresh


def test_a_set_point_whose_switch_is_off_is_not_a_stall(lab, tmp_path):
    """A controller's setpoint with its heater range off goes nowhere: not followed (no alarm) until the heater is on,
    then followed afresh."""
    gate, monitor, notices = watching(lab, tmp_path, answers=[True, True])
    assert gate.set("tc", "setpoint", 5, wait=False).ok    # its heater range is off
    run_for(lab, monitor, 10 * 60, step=15)
    assert not monitor.alarms and "tc.setpoint" not in monitor._moves
    assert gate.set("tc", "heater_range", "low").ok
    run_for(lab, monitor, 3 * 60, step=15)
    assert not monitor.alarms and monitor._moves["tc.setpoint"].arrived


def test_the_watchdog_never_keeps_the_interlock_check_from_running(lab, tmp_path, monkeypatch):
    """It runs before the interlock check in each pass: whatever goes wrong in it, that check still runs."""
    from labharness import monitor as monitor_module
    gate, monitor, notices = watching(lab, tmp_path, answers=[True])
    assert gate.set("magnet", "B", 0.5, wait=False).ok   # away from safe: its interlock needs tc.T_sample < 10

    def broken(*args, **kwargs):
        raise RuntimeError("an odd card")
    monkeypatch.setattr(monitor_module, "rate_per_s", broken)
    lab.clock.sleep(5)
    lab.instruments["tc"].backend.state["T_sample"] = 15.0   # the sample warms: the interlock fails
    monitor.tick()
    assert [a.kind for a in monitor.alarms] == ["interlock"]


def test_the_approach_counts_progress_beyond_noise_and_slow_ramps():
    way = Approach(2.0, 0.05, 0.0)
    assert way.update(150.0, 0.0) == 0.0
    for t in range(1, 30):   # stuck at 150 K, 2 mK of noise either way
        assert way.update(150.0 + (0.002 if t % 2 else -0.002), t * 20.0) == t * 20.0
    assert way.update(149.0, 700.0) == 0.0 and not way.there(149.0) and way.there(2.03)
    slow = Approach(2.0, 0.05, 0.0, per_s=0.01 / 60, window=600)   # 0.01 K/min: 0.1 K in 10 min
    slow.update(100.0, 0.0)
    assert slow.update(99.97, 150.0) == 0.0                        # 30 mK closer counts at this rate
    assert Approach(2.0, 0.05, 0.0).update(float("nan"), 30.0) == 30.0   # not a number: no progress
