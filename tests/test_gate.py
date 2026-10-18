import json

import pytest

from labharness import SafetyGate, SimulationApprover


def read(lab, key):
    instr, param = key.split(".")
    return lab.read(instr, param)


# --- measure / configure: free -------------------------------------------------------------

def test_measurement_and_configuration_never_ask(lab, make_gate):
    gate, approver = make_gate([])
    assert gate.set("lockin", "tc", 0.3).ok
    assert gate.set("vna", "points", 2001).ok
    assert gate.set("stage", "position", 50).ok                  # a measurement axis, waits for arrival
    assert abs(read(lab, "stage.position_actual") - 50) < 1e-3
    assert gate.get("lockin", "X").ok
    assert gate.raw("smu", "SENS:CURR:NPLC 2").ok
    assert gate.raw("smu", "MEAS:CURR?").action == "read"
    assert approver.requests == []


def test_measurement_still_respects_limits(make_gate):
    gate, approver = make_gate([])
    r = gate.set("lockin", "tc", 1e6)
    assert r.action == "refused" and "vendor limits" in r.message


# --- drives ---------------------------------------------------------------------------------

def test_drive_increase_asks_then_ramps(lab, make_gate):
    gate, approver = make_gate([True])
    t0 = lab.clock.now()
    r = gate.set("smu", "V", 5)
    assert r.ok and "approved" in r.message
    assert len(approver.requests) == 1 and "0 V → 5 V" in str(approver.requests[0])
    assert read(lab, "smu.V") == 5
    assert lab.clock.now() - t0 >= 10  # 5 V at 0.5 V/s


def test_declined_drive_stays_put(lab, make_gate):
    gate, _ = make_gate([False])
    r = gate.set("smu", "V", 5)
    assert r.action == "declined" and read(lab, "smu.V") == 0


def test_drive_toward_safe_runs_without_asking(lab, make_gate):
    gate, approver = make_gate([True])
    gate.set("smu", "V", 5)
    t0 = lab.clock.now()
    r = gate.set("smu", "V", 2)
    assert r.ok and "toward safe" in r.message and len(approver.requests) == 1
    assert lab.clock.now() - t0 >= 6


def test_sign_flip_counts_as_away(make_gate):
    gate, approver = make_gate([True, True])
    gate.set("smu", "V", 5)
    gate.set("smu", "V", -5)
    assert len(approver.requests) == 2


def test_beyond_lab_limits_is_refused_without_asking(make_gate):
    gate, approver = make_gate([])
    r = gate.set("smu", "V", 20)
    assert r.action == "refused" and "lab limits [-15, 15]" in r.message


def test_envelope_runs_inside_and_asks_outside(lab, make_gate):
    gate, approver = make_gate([True])
    assert "envelope" in gate.set("lockin", "amplitude", 0.05).message   # inside [0, 0.1]
    assert approver.requests == []
    assert gate.set("lockin", "amplitude", 0.5).ok                        # outside → asked
    assert len(approver.requests) == 1
    assert gate.set("lockin", "amplitude", 2).action == "refused"         # beyond lab limit 1 V


def test_ask_level_instrument_ignores_envelope(make_gate):
    gate, approver = make_gate([True])
    gate.envelope.ranges["smu.V"] = gate.envelope.ranges["lockin.amplitude"]  # smu has trust: ask
    gate.set("smu", "V", 0.05)
    assert len(approver.requests) == 1


def test_guard_trip_stops_the_ramp_and_returns_to_safe(lab, make_gate):
    gate, _ = make_gate([True, True])
    gate.set("smu", "output", True)
    r = gate.set("smu", "V", 14)  # the toy gate breaks down above 12 V
    assert r.action == "tripped"
    assert 12 < r.value < 12.2 and "back to 0 V" in r.message
    assert read(lab, "smu.V") == 0
    assert any(e["type"] == "trip" for e in gate.log.events())


def test_interlock_blocks_field_when_warm(lab, make_gate):
    gate, approver = make_gate([])
    lab.instruments["tc"].backend.state["T_sample"] = 25.0
    r = gate.set("magnet", "B", 1)
    assert r.action == "refused" and "interlock" in r.message and "tc.T_sample < 10" in r.message
    assert approver.requests == []


def test_magnet_ramps_on_the_instrument_and_waits(lab, make_gate):
    gate, _ = make_gate([True])
    t0 = lab.clock.now()
    r = gate.set("magnet", "B", 1)
    assert r.ok
    assert abs(read(lab, "magnet.B_actual") - 1) <= 1e-3
    assert 290 <= lab.clock.now() - t0 <= 400  # 1 T at 0.2 T/min


def test_switching_output_on_parks_its_drive_first(lab, make_gate):
    gate, _ = make_gate([True, True])
    gate.set("smu", "V", 5)                    # setpoint only; output is off
    r = gate.set("smu", "output", True)
    assert r.ok and "V set to 0 V first" in r.message
    assert read(lab, "smu.V") == 0


def test_switching_output_off_ramps_its_drive_down_first(lab, make_gate):
    gate, approver = make_gate([True, True])
    gate.set("smu", "output", True)
    gate.set("smu", "V", 5)
    t0 = lab.clock.now()
    r = gate.set("smu", "output", False)
    assert r.ok and "V ramped to 0 V first" in r.message
    assert lab.clock.now() - t0 >= 10 and len(approver.requests) == 2


def test_undo_goes_back_through_the_gate(lab, make_gate):
    gate, approver = make_gate([True])
    gate.set("smu", "V", 3)
    r = gate.undo()
    assert r.ok and read(lab, "smu.V") == 0 and len(approver.requests) == 1


# --- raw commands -------------------------------------------------------------------------

def test_raw_drive_is_treated_as_a_drive(lab, make_gate):
    gate, approver = make_gate([False])
    r = gate.raw("smu", ":source:voltage:level 5")
    assert r.action == "declined" and read(lab, "smu.V") == 0
    assert gate.raw("smu", "SOUR:VOLT 20").action == "refused"


def test_raw_command_touching_a_drive_family_asks(make_gate):
    gate, approver = make_gate([False])
    r = gate.raw("smu", "SOUR:VOLT:RANG 20")
    assert r.action == "declined" and "touches drive V" in str(approver.requests[0])


def test_raw_blocked_commands_are_refused(make_gate):
    gate, approver = make_gate([])
    assert gate.raw("vna", "SENS:CORR:COLL:METH SOLT2").action == "refused"
    assert gate.raw("smu", "CAL:ZERO").action == "refused"


def test_raw_reset_brings_drives_to_safe_first(lab, make_gate):
    gate, approver = make_gate([True, True])
    gate.set("smu", "output", True)
    gate.set("smu", "V", 5)
    t0 = lab.clock.now()
    r = gate.raw("smu", "*RST")
    assert r.ok and "drives brought to safe" in r.message
    assert lab.clock.now() - t0 >= 10       # ramped, not jumped
    assert read(lab, "smu.output") is False


def test_compound_raw_checks_each_part(lab, make_gate):
    gate, approver = make_gate([False])
    r = gate.raw("smu", "SENS:CURR:NPLC 2;:SOUR:VOLT 1")
    assert not r.ok and read(lab, "smu.nplc") == 2 and read(lab, "smu.V") == 0


# --- kill switch, logging, misc -------------------------------------------------------------

def test_safe_state_brings_everything_down_without_asking(lab, make_gate):
    gate, approver = make_gate([True] * 7)
    gate.set("smu", "output", True)
    gate.set("smu", "V", 5)
    gate.set("laser", "emission", True)
    gate.set("laser", "power", 10)
    gate.set("laser", "shutter", True)
    gate.set("magnet", "B", 0.5, wait=False)
    lab.clock.sleep(30)                      # magnet still ramping
    n_asks = len(approver.requests)
    results = gate.safe_state()
    assert all(r.ok for r in results) and len(approver.requests) == n_asks
    assert read(lab, "smu.V") == 0 and read(lab, "smu.output") is False
    assert read(lab, "laser.shutter") is False and read(lab, "laser.power") == 0
    held = [r for r in results if r.action == "held"]
    assert held and held[0].target == "magnet.B"   # the lab says hold, not ramp to zero
    assert 0 < read(lab, "magnet.B") < 0.5


def test_every_command_is_logged(make_gate, tmp_path):
    gate, _ = make_gate([True])
    gate.set("lockin", "tc", 0.3)
    gate.set("smu", "V", 1)
    gate.get("smu", "I")
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    kinds = [(e["type"], e.get("method")) for e in events]
    assert ("command", "set") in kinds and ("ask", None) in kinds and ("command", "get") in kinds


def test_draft_card_on_real_hardware_keeps_drives_off(lab, make_gate):
    gate, approver = make_gate([])
    inst = lab.instruments["smu"]
    inst.backend.simulated = False
    inst.card.status = "draft"
    r = gate.set("smu", "V", 1)
    assert r.action == "refused" and "draft card" in r.message
    assert gate.set("smu", "nplc", 2).ok   # measurement still free


def test_simulation_approver_is_refused_on_real_hardware(lab):
    lab.instruments["smu"].backend.simulated = False
    with pytest.raises(ValueError):
        SafetyGate(lab, SimulationApprover())


def test_unresponsive_instrument_reports_an_error(lab, make_gate):
    gate, _ = make_gate([])
    lab.instruments["lockin"].backend.fault = "offline"
    r = gate.get("lockin", "X")
    assert r.action == "error" and "offline" in r.message
