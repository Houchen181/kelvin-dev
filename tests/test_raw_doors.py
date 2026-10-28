"""The gate's side doors for raw commands (field tests: ':SOUR:FUNC CURR' put a back gate into current mode, 10 µA went
into the oxide; 'CONF:FIELD:UNITS 0' switched a magnet to kG; 'conf:field:targ 5;ramp' got a generic card with no
value, rate or interlock, one click from bypassing the interlock; *CAL? was not blocked)."""
import pytest

from labharness import EventLog, SafetyGate, ScriptedApprover, build_virtual_lab

MAGNET_LAB = """
lab: side doors
instruments:
  magnet: { card: ami/430, address: "sim://magnet" }
  tc: { card: virtual/temperature-controller, address: "sim://tc" }
interlocks:
  - drive: magnet.B_target
    require: "tc.T_sample < 10"
"""


@pytest.fixture
def magnet(tmp_path):
    lab = build_virtual_lab(config=MAGNET_LAB)
    approver = ScriptedApprover([True, True])
    return lab, SafetyGate(lab, approver, EventLog(tmp_path / "events.jsonl", lab.clock)), approver


def test_a_chain_the_card_writes_as_one_setting_is_checked_against_the_interlock(magnet):
    lab, gate, approver = magnet
    lab.instruments["tc"].backend.state["T_sample"] = 295.0                  # warm: the interlock fails
    r = gate.raw("magnet", "conf:field:targ 5;ramp")
    assert r.action == "refused" and "interlock" in r.message and "tc.T_sample < 10" in r.message
    assert approver.requests == [] and lab.read("magnet", "B_target") == 0
    r = gate.raw("magnet", "CONF:FIELD:TARG 5")                              # half of it: the drive's family
    assert r.action == "refused" and "an interlock guards" in r.message and approver.requests == []
    lab.instruments["tc"].backend.state["T_sample"] = 4.2                    # cold: asked as the setting it is
    r = gate.raw("magnet", "conf:field:targ 5;ramp")
    assert r.ok and "0 T → 5 T" in str(approver.requests[0]) and lab.read("magnet", "B_target") == 5


def test_function_protection_units_and_calibration_are_refused_unless_the_card_lists_them(lab, make_gate):
    gate, approver = make_gate([])
    for cmd in (":SOUR:FUNC CURR", "SOUR:VOLT:PROT 20", ":SENS:VOLT:PROT 210", "UNIT:VOLT DBM", "*CAL?",
                "CAL:ZERO"):
        r = gate.raw("smu", cmd)
        assert r.action == "refused", cmd
    assert "source function" in gate.raw("smu", ":SOUR:FUNC CURR").message
    assert gate.raw("smu", "SENS:CURR:PROT 1e-6").ok                         # compliance_I is on the card: a setting
    assert gate.raw("smu", "SOUR:FUNC?").action != "refused"                 # reading it is free
    assert approver.requests == [] and lab.read("smu", "output") is False


def test_units_are_refused_on_the_magnet(magnet):
    lab, gate, approver = magnet
    assert gate.raw("magnet", "CONF:FIELD:UNITS 0").action == "refused"
    assert lab.read("magnet", "field_units") == "T" and approver.requests == []


def test_an_unlisted_write_on_an_instrument_with_drives_asks(lab, make_gate):
    gate, approver = make_gate([False])
    r = gate.raw("smu", "TRIG:SOUR BUS")
    assert r.action == "declined" and "not on its card" in str(approver.requests[0])
    assert "has drives that reach the sample (V, compliance_I, output)" in str(approver.requests[0])
    assert gate.raw("smu", "*CLS").ok and len(approver.requests) == 1        # status housekeeping: never an output


def test_a_drive_family_command_shows_its_value_against_the_limits(lab, make_gate):
    gate, approver = make_gate([False])
    r = gate.raw("smu", "SOUR:VOLT:STOP 20")
    assert r.action == "refused" and "beyond smu.V's limits [-15, 15] V" in r.message
    r = gate.raw("smu", "SOUR:VOLT:STAR 3")
    assert r.action == "declined"
    assert "it carries 3 V, within smu.V's limits [-15, 15] V (now 0 V)" in str(approver.requests[0])
