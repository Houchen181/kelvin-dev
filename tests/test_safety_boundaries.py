"""Faults and wire-format escapes must obey the same rules as ordinary commands."""
import pytest

from labharness.conditions import parse_condition
from labharness.labconfig import Interlock


@pytest.mark.parametrize("separator", ["\n", "\r", "\r\n", "\x00", "\x0b", "\x1b", "\x7f"])
def test_raw_message_injection_never_reaches_the_backend(lab, make_gate, monkeypatch, separator):
    gate, approver = make_gate()
    sent = []
    monkeypatch.setattr(lab.instruments["smu"].backend, "query", lambda cmd: sent.append(cmd) or "identity")
    r = gate.raw("smu", "*IDN?" + separator + "SOUR:VOLT 100")
    assert r.action == "refused" and sent == [] and approver.requests == []
    assert gate.log.events()[-1]["action"] == "refused"


@pytest.mark.parametrize("command", ["UNMODELED 100", "SOUR:VOLT:RANG 200", "TRIG?", "GET /unknown"])
def test_unclassified_commands_are_blocked_on_real_backends(lab, make_gate, command):
    gate, approver = make_gate()
    lab.instruments["smu"].backend.simulated = False  # still a twin; never touches hardware
    r = gate.raw("smu", command)
    assert r.action == "refused" and approver.requests == []


def test_raw_query_respects_output_dependent_read(lab, make_gate, monkeypatch):
    gate, _ = make_gate()
    inst = lab.instruments["smu"]
    inst.card.params["I"].read_when = "output"
    query = inst.backend.query
    sent = []

    def record(command):
        sent.append(command)
        return query(command)

    monkeypatch.setattr(inst.backend, "query", record)
    r = gate.raw("smu", "MEAS:CURR?")
    assert r.ok and r.value is None
    assert not any(c.startswith("MEAS") for c in sent)


def test_interlock_is_rechecked_after_approval(lab, make_gate, monkeypatch):
    gate, approver = make_gate()
    read = lab.read
    state = {"hot": False}
    monkeypatch.setattr(lab, "read", lambda i, p: 20.0 if state["hot"] and (i, p) == ("tc", "T_sample")
                        else read(i, p))

    def approve(request):
        state["hot"] = True
        return True

    monkeypatch.setattr(approver, "approve", approve)
    r = gate.set("magnet", "B", 0.2)
    assert r.action == "refused" and "after approval" in r.message
    assert lab.read("magnet", "B") == 0


@pytest.mark.parametrize("instrument,parameter,target", [("smu", "V", 1), ("smu", "V", 0.05),
                                                        ("magnet", "B", 0.2)])
def test_interlock_is_watched_during_motion(lab, make_gate, monkeypatch, instrument, parameter, target):
    gate, _ = make_gate([True])
    lab.config.interlocks.append(Interlock("smu.V", parse_condition("tc.T_sample < 10")))
    read, sleep = lab.read, lab.clock.sleep
    state = {"hot": False}
    monkeypatch.setattr(lab, "read", lambda i, p: 20.0 if state["hot"] and (i, p) == ("tc", "T_sample")
                        else read(i, p))

    def heat(seconds):
        sleep(seconds)
        state["hot"] = True

    monkeypatch.setattr(lab.clock, "sleep", heat)
    r = gate.set(instrument, parameter, target)
    assert r.action == "tripped" and "interlock" in r.message
    assert lab.read(instrument, parameter) != target
    if instrument == "smu":
        assert lab.read("smu", "V") == 0
    else:
        assert "holding" in r.message  # the lab's magnet policy is hold, not zero


@pytest.mark.parametrize("hold", [False, True])
def test_failed_guard_uses_the_lab_recovery_policy(lab, make_gate, monkeypatch, hold):
    gate, _ = make_gate([True])
    lab.instruments["smu"].backend.state["output"] = True
    if hold:
        lab.config.safe_actions["smu.V"] = "hold"
    read = lab.read

    def fail(i, p):
        if (i, p) == ("smu", "I"):
            raise TimeoutError("leakage sensor disconnected")
        return read(i, p)

    monkeypatch.setattr(lab, "read", fail)
    r = gate.set("smu", "V", 1)
    assert r.action == "error" and "leakage sensor disconnected" in r.message
    assert read("smu", "V") == (0.05 if hold else 0)
    assert any(e["type"] == "trip" for e in gate.log.events())


def test_recovery_failure_does_not_hide_the_original_fault(lab, make_gate, monkeypatch):
    gate, _ = make_gate([True])
    read, inst = lab.read, lab.instruments["smu"]
    write = inst.write

    def fail_read(i, p):
        if (i, p) == ("smu", "I"):
            raise TimeoutError("sensor failed")
        return read(i, p)

    def fail_safe(p, v):
        if p == "V" and v == 0:
            raise OSError("source stopped accepting writes")
        write(p, v)

    monkeypatch.setattr(lab, "read", fail_read)
    monkeypatch.setattr(inst, "write", fail_safe)
    r = gate.set("smu", "V", 1)
    assert r.action == "error" and "sensor failed" in r.message
    assert "recovery failed" in r.message and "source stopped accepting writes" in r.message


def test_native_ramp_timeout_holds_at_the_measured_position(lab, make_gate, monkeypatch):
    gate, _ = make_gate([True])
    inst = lab.instruments["magnet"]
    inst.backend.state.update(B=0.3, B_actual=0.3)
    read = inst.read
    monkeypatch.setattr(inst, "read", lambda p: 0.3 if p == "B_actual" else read(p))
    r = gate.set("magnet", "B", 0.5)
    assert r.action == "timeout" and "holding" in r.message
    assert read("B") == 0.3


def test_smaller_opposite_polarity_still_needs_approval(make_gate):
    gate, approver = make_gate([True, False])
    assert gate.set("smu", "V", 5).ok
    assert gate.set("smu", "V", -4).action == "declined"
    assert len(approver.requests) == 2


@pytest.mark.parametrize("value", ["cw;SOUR:VOLT 100", "cw\nSOUR:VOLT 100", "cw\x00"])
def test_string_parameters_cannot_inject_extra_commands(lab, value):
    spec = lab.instruments["smu"].spec("V")
    spec.type = "str"
    with pytest.raises(ValueError, match="separators|control characters"):
        spec.coerce(value)
