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
    lines, grants, _ = rt.gate.plan_approval(rt._queue_drives(rt.queue.waiting()), True)
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
