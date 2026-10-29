"""Overnight durability (field tests, 2026-10: a queue re-asked in the night and declined after 5 minutes; a lost
instrument paused the queue with a raw VISA code and no alarm; garbled replies flapped as errors 42 times; off-desk
alerts needed a third-party webhook). Each check reproduces what testers met, on the virtual lab, a fake bus or labsim."""
import pytest

from labharness import ScriptedApprover, build_virtual_lab

SWEEP = """plan: {name}
setup: {{ smu.output: on }}
sweep: {{ smu.V: {{from: 0, to: 1, step: 0.5}} }}
settle: 0.1
measure: [smu.I]
after: {{ smu.V: 0, smu.output: off }}"""


def runtime(tmp_path, answers, lab=None):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    return LabRuntime(lab or build_virtual_lab(seed=1), ScriptedApprover(list(answers)), tmp_path / "data", None)


def run_queue(rt):
    out = rt.queue_start()
    assert out.startswith("queue started"), out
    rt._queue_thread.join(120)
    return [(i.name, i.status, i.message) for i in rt.queue.items]


# ---------------------------------------------------------------------------------------------- the queue's approval


def test_a_queue_approval_covers_its_whole_path_so_nothing_asks_in_the_night(tmp_path):
    """The output was left on and the gate at 2 V when the queue started: the first item's end switched the output off,
    so the second item's switching it on was a new question at 3 a.m. (and the sweep up from 0 another), which nobody
    answered. The one approval covers every value the queue sets away from safe, wherever the drive is when it starts."""
    rt = runtime(tmp_path, [True])                                  # one answer: a second ask fails the item
    smu = rt.lab.instruments["smu"].backend
    smu.state.update(output=True, V=2.0)                            # left from the day's work
    rt.queue_add(SWEEP.format(name="first"))
    rt.queue_add(SWEEP.format(name="second"))
    assert run_queue(rt) == [("first", "done", ""), ("second", "done", "")]
    [card] = rt.gate.approver.requests
    assert "smu.output" in str(card) and "smu.V" in str(card)       # both on the one card, though both were on now
    assert rt.queue.state == "idle"


def test_the_queue_approval_covers_what_a_plan_sets_after_a_stop(tmp_path):
    """A plan's on_abort runs after a stop or a failure, in the night too: the one approval covers what it sets."""
    rt = runtime(tmp_path, [True])
    rt.queue_add(SWEEP.format(name="first") + "\non_abort: { smu.V: 0.5 }")
    out = rt.queue_start(dry_run=True)
    assert out.startswith("REHEARSAL ONLY")
    lines, grants, _ = rt.gate.plan_approval(rt._queue_drives(rt.queue.waiting()), whole_path=True)
    assert grants["smu.V"].covers(0.5) and grants["smu.output"].covers(True)


INTERLOCKED = """plan: field_points
sweep: { magnet.B: {values: [0.1, 0.2]} }
measure: [lockin.X]
after: { magnet.B: 0 }"""

COOL = """step: cool to 2 K
set: { tc.setpoint: 2 }
stabilize: { read: tc.T_sample, within: 0.05, for: 20, timeout: 900 }"""


def warm(rt, kelvin):
    tc = rt.lab.instruments["tc"].backend
    tc.state.update(heater_range="medium", setpoint=kelvin, T_sample=kelvin)


def test_a_queue_that_cools_first_starts_while_the_sample_is_still_warm(tmp_path):
    """The magnet's interlock needs the sample below 10 K; the queue cools to 2 K first. Checked against the lab now,
    the field sweep was refused and the queue couldn't start (a tester fell back to four approvals in the night). Its
    interlock is now checked when the field moves, after the cool-down, and the card says so."""
    rt = runtime(tmp_path, [True])
    warm(rt, 12.0)
    rt.queue_add(COOL)
    rt.queue_add(INTERLOCKED)
    assert run_queue(rt) == [("cool to 2 K", "done", ""), ("field_points", "done", "")]
    [card] = rt.gate.approver.requests
    assert "its interlock needs tc.T_sample < 10, which doesn't hold now (now 12" in str(card)


def test_an_interlock_that_still_fails_when_its_item_comes_pauses_the_queue_there(tmp_path):
    """Left for later is still checked: a cool-down that doesn't get below 10 K stops the queue at the field sweep."""
    rt = runtime(tmp_path, [True])
    warm(rt, 12.0)
    rt.queue_add(COOL.replace("setpoint: 2", "setpoint: 11").replace("cool to 2 K", "cool to 11 K"))
    rt.queue_add(INTERLOCKED)
    (_, first, _), (_, second, why) = run_queue(rt)
    assert first == "done" and second == "failed" and "interlock: magnet.B needs tc.T_sample < 10" in why
    assert rt.queue.state == "paused" and rt.lab.read("magnet", "B") == 0


def test_a_field_sweep_first_in_the_queue_is_still_refused_at_the_start(tmp_path):
    """Only an item before the first one that moves the drive counts: one that sweeps the field first can't start."""
    rt = runtime(tmp_path, [])
    warm(rt, 12.0)
    rt.queue_add(INTERLOCKED)
    rt.queue_add(COOL)
    rt.queue_add(INTERLOCKED.replace("field_points", "field_again"))
    out = rt.queue_start()
    assert out.startswith("the queue did not start") and "interlock: magnet.B needs tc.T_sample < 10" in out


# ---------------------------------------------------------------------------------------------- instruments that are lost


class VisaError(Exception):
    """What PyVISA raises: VisaIOError, with the status code."""

    def __init__(self, code, name, text):
        super().__init__("%s (%d): %s" % (name, code, text))
        self.error_code = code


NLISTENERS = (-1073807265, "VI_ERROR_NLISTENERS", "No listeners condition is detected (both NRFD and NDAC are deasserted).")
TMO = (-1073807339, "VI_ERROR_TMO", "Timeout expired before operation completed.")


def test_a_lost_instrument_is_said_in_plain_words_with_its_bus_and_what_to_check():
    """l09 got "queue paused" with a raw VI_ERROR_NLISTENERS and no instrument named. The plan's words: "the 2182A
    doesn't answer on GPIB: cable or power?"."""
    from labharness.adapters import lost_kind, short_model, where_and_cause
    assert where_and_cause("GPIB0::7::INSTR") == ("on GPIB (address 7)", "cable or power?")
    assert where_and_cause("USB0::0xB506::0x2000::003456::INSTR") == ("on USB", "USB cable or power?")
    assert where_and_cause("TCPIP0::192.168.1.20::inst0::INSTR")[0] == "on the network (192.168.1.20)"
    assert where_and_cause("serial://COM3")[0] == "on COM3" and "baud rate" in where_and_cause("ASRL3::INSTR")[1]
    assert where_and_cause("qd://127.0.0.1:5000") == ("through MultiVu at 127.0.0.1:5000",
                                                      "is MultiVu running, with its server started?")
    assert where_and_cause("sim://lockin") == ("", "")
    assert short_model("Keithley 2182 / 2182A nanovoltmeter") == "Keithley 2182 / 2182A nanovoltmeter"
    assert short_model("Quantum Design PPMS / DynaCool / VersaLab / MPMS3 (MultiVu)") == "Quantum Design PPMS / DynaCool"
    assert lost_kind(VisaError(*NLISTENERS)) == "gone" and lost_kind(VisaError(*TMO)) == "silent"
    assert lost_kind(Exception("timed out waiting for the device to answer")) == "silent"   # MultiPyVu
    assert lost_kind(ConnectionResetError()) == "gone"
    assert lost_kind(ValueError("V needs a number, got 'k5.14'")) is None   # it answered: not a lost instrument


def real_twin(lab, name):
    twin = lab.instruments[name].backend
    twin.simulated = False   # as a real instrument: the monitor checks it
    return twin


def test_an_instrument_nothing_reads_is_checked_while_work_runs_and_alarms_once(lab, make_gate):
    """l09 unplugged the 2182A for 2 min 40 s during a cool-down: nothing read it, so nothing noticed. While a plan
    or a queue item runs, every real instrument is asked to answer; one that can't be reached at all alarms at once,
    in words, and once; it clears when it answers again."""
    from labharness.monitor import Monitor
    gate, _ = make_gate([])
    notices = []
    monitor = Monitor(lab, gate, lambda kind, title, text="": notices.append((kind, title)))
    twin = real_twin(lab, "lockin")
    lab.instruments["lockin"].config.address = "GPIB0::7::INSTR"
    real = twin.query

    def unplugged(command):
        raise VisaError(*NLISTENERS)
    with gate.preapproved({}):                                      # a run is going
        monitor.tick()
        assert not monitor.alarms
        twin.query = unplugged
        lab.clock.sleep(21)
        monitor.tick()
        [alarm] = monitor.alarms
        assert alarm.kind == "silent" and alarm.text == (
            "lockin (Virtual Lock-in Amplifier) doesn't answer on GPIB (address 7): cable or power? "
            "[VI_ERROR_NLISTENERS]")
        for _ in range(3):
            lab.clock.sleep(21)
            monitor.tick()
        assert len(monitor.alarms) == 1 and [k for k, _ in notices] == ["alarm"]   # once, not at every check
        twin.query = real
        lab.clock.sleep(21)
        monitor.tick()
    assert alarm.cleared and notices[-1][1].startswith("Cleared: lockin answers again (it didn't for ")


def test_nothing_is_asked_of_an_instrument_while_no_work_runs(lab, make_gate):
    from labharness.monitor import Monitor
    gate, _ = make_gate([])
    monitor = Monitor(lab, gate, lambda *a: None)
    twin = real_twin(lab, "lockin")
    sent = []
    real = twin.query
    twin.query = lambda c: sent.append(c) or real(c)
    for _ in range(3):
        lab.clock.sleep(21)
        monitor.tick()
    assert not any("IDN" in c for c in sent)                        # idle: only the minute's error-queue check


def test_an_output_that_a_run_switched_on_and_that_restarted_off_stops_the_run(lab, make_gate):
    """A 1 s power cut on the 6221 went unnoticed: it came back with its output off and the queue measured on, at no
    current. An output the running plan switched on that reads off twice, a few seconds apart, alarms once and stops
    the run."""
    from labharness.monitor import RESET_EVERY, Monitor
    gate, _ = make_gate([True])
    notices = []
    monitor = Monitor(lab, gate, lambda kind, title, text="": notices.append((kind, title, text)))
    smu = real_twin(lab, "smu")
    with gate.preapproved({}):
        assert gate.set("smu", "output", True).ok                    # the run switched it on
        monitor.tick()
        assert not monitor.alarms
        smu.state["output"] = False                                  # a power cut: back with its power-on settings
        generation = gate.stop_generation
        lab.clock.sleep(RESET_EVERY)
        monitor.tick()
        assert not monitor.alarms                                    # once may be a switch still following
        lab.clock.sleep(RESET_EVERY)
        monitor.tick()
        [alarm] = monitor.alarms
        assert alarm.kind == "reset" and "smu.output is off, though the running plan switched it on" in alarm.text
        assert gate.stop_generation == generation + 1 and "stopped the running plan" in alarm.actions[0]
        lab.clock.sleep(RESET_EVERY)
        monitor.tick()
        assert len(monitor.alarms) == 1 and len(notices) == 1


def test_a_lost_instrument_that_ends_a_queue_item_alarms_at_once_and_names_itself_in_the_pause(tmp_path):
    rt = runtime(tmp_path, [])
    rt.queue.add("plan: look\ntime: { every: 1, points: 3 }\nmeasure: [lockin.X]")
    rt.lab.instruments["lockin"].backend.fault = "offline"           # it stopped answering
    rt.queue.state = "running"
    rt._queue_loop({}, "test")
    [item] = rt.queue.items
    assert item.status == "failed" and "lockin (Virtual Lock-in Amplifier) doesn't answer" in item.message
    assert rt.queue.state == "paused" and "lockin (Virtual Lock-in Amplifier) doesn't answer" in rt.queue.reason
    [alarm] = rt.monitor.active()                                    # at once, not after three more failed reads
    assert alarm.kind == "silent" and alarm.key == "lockin"
    rt.monitor.tick()
    assert len(rt.monitor.alarms) == 1
