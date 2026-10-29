"""The scan queue: scans and steps lined up, approved once, run one after another in the background."""
import threading
import time

import pytest

from labharness import ScriptedApprover
from simlab import build_virtual_lab
from labharness.plans import PlanError
from labharness.scanqueue import ScanQueue, load_step, parse_item

SWEEP = """plan: gate_sweep
setup: { lockin.amplitude: 0.05, smu.output: on }
sweep: { smu.V: {from: 0, to: 2, step: 0.5} }
settle: 0.1
measure: [lockin.X, smu.I]
after: { smu.V: 0 }"""

FIELD = """plan: field_points
sweep: { magnet.B: {values: [0.5, 1]} }
measure: [lockin.X]
after: { magnet.B: 0 }"""

STEP = """step: field to 0.5 T
set: { magnet.B: 0.5 }
wait: 30"""


def runtime(tmp_path, answers):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    return LabRuntime(build_virtual_lab(seed=1), ScriptedApprover(list(answers)), tmp_path / "data", None)


def test_items_are_plans_or_steps_and_the_queue_is_kept(tmp_path):
    assert parse_item(SWEEP) == ("plan", "gate_sweep") and parse_item(STEP) == ("step", "field to 0.5 T")
    assert load_step(STEP).sets == [("magnet", "B", 0.5)] and load_step(STEP).wait == 30
    with pytest.raises(PlanError, match="a queue item is a plan"):
        parse_item("hello: 1")
    with pytest.raises(PlanError, match="sets values"):
        load_step("step: nothing")
    q = ScanQueue(tmp_path / "queue.json")
    a, b = q.add(SWEEP), q.add(FIELD)
    c = q.add(STEP, position=2, note="let the magnet settle")
    assert [i.name for i in q.items] == ["gate_sweep", "field to 0.5 T", "field_points"]
    q.move(b.id, 1)
    assert q.find(1) is b and q.find("#2") is a and q.find("#3") is c
    q.mark(a, "running")
    with pytest.raises(ValueError, match="running"):
        q.remove(a.id)
    again = ScanQueue(tmp_path / "queue.json")                  # Kelvin restarted in the middle of a scan
    assert again.find(a.id).status == "stopped" and again.state == "paused"
    assert again.find(c.id).note == "let the magnet settle"
    assert "restarted" in again.reason


def test_a_queue_is_approved_once_and_runs_in_order(tmp_path):
    rt = runtime(tmp_path, [True])                              # exactly one approval: more would be an error
    assert "added plan gate_sweep as #1" in rt.queue_add(SWEEP)
    rt.queue_add(FIELD)
    rt.queue_add(STEP, position=2)
    assert rt.queue_add("plan: bad\nsweep: {nope.V: {values: [1]}}\nmeasure: [smu.I]").startswith("not added")
    assert rt.queue_add("step: too far\nset: {smu.V: 1000}").startswith("not added")   # outside the limits
    out = rt.queue_start()
    assert out.startswith("queue started"), out
    rt._queue_thread.join(120)
    items = rt.queue.items
    assert [i.status for i in items] == ["done", "done", "done"], [(i.name, i.status, i.message) for i in items]
    assert rt.queue.state == "idle" and rt.queue.reason == "finished"
    assert items[0].run_id and items[2].run_id and items[1].run_id is None   # a step makes no run
    from labharness import rundata
    meta = rundata.meta(rt.data_dir / "runs" / items[2].run_id)
    assert meta["queue"]["position"] == 3                       # each run says which queue item it was
    assert rt.queue_start() == "nothing is waiting in the queue"


def test_a_failure_pauses_the_queue_and_leaves_the_rest_waiting(tmp_path):
    rt = runtime(tmp_path, [])
    failing = SWEEP.replace("plan: gate_sweep", "plan: needs_field\npreconditions: [\"magnet.B > 5\"]")
    first, second = rt.queue.add(failing), rt.queue.add(FIELD)
    rt.queue.state = "running"
    rt._queue_loop({}, "test")                                  # the background loop, run here
    assert first.status == "failed" and "precondition" in first.message
    assert second.status == "waiting" and rt.queue.state == "paused" and "needs_field" in rt.queue.reason
    rt.queue_set_on_error("continue")
    rt.queue.again(first.id)
    rt.queue.state = "running"
    rt._queue_loop({}, "test")                                  # carry on past failures when asked to
    assert first.status == "failed" and second.status == "failed"   # FIELD needs an approval nobody gave


def test_a_queue_rehearsal_says_what_would_fail_and_never_starts_the_queue(tmp_path):
    rt = runtime(tmp_path, [])                                  # nobody answers: asking would be an error
    rt.queue.add(SWEEP.replace("plan: gate_sweep", "plan: cold_only\npreconditions: [\"magnet.B > 5\"]"))
    out = rt.queue_start(dry_run=True)                          # a rehearsal, because asked for
    assert out.startswith("REHEARSAL ONLY") and "would fail" in out and "precondition" in out
    assert rt.queue.state == "idle" and [i.status for i in rt.queue.items] == ["waiting"]
    rt.queue.items.clear()
    rt.queue.add(SWEEP)
    out = rt.queue_start(dry_run=True)                          # one that would pass: still only a rehearsal
    assert out.startswith("REHEARSAL ONLY") and "would take ~" in out and rt.queue.state == "idle"
    assert rt.gate.approver.requests == [] and rt._queue_thread is None


def test_stop_pauses_and_chat_tools_answer_while_a_scan_runs(tmp_path):
    rt = runtime(tmp_path, [])
    rt.queue.add(SWEEP)
    rt.queue.state = "running"
    rt.stop(by="human")
    assert rt.queue.state == "pausing" and "stopped by human" in rt.queue.reason
    held = threading.Event()

    def hold():   # the queue's thread, in the middle of a scan
        with rt.lock:
            held.set()
            time.sleep(3)
    threading.Thread(target=hold, daemon=True).start()
    held.wait(2)
    t0 = time.time()
    status = rt.lab_status()   # at once: what the scan holds isn't asked, its latest values are shown
    assert time.time() - t0 < 2.9 and "busy now: the queue" in status and "smu (not read now" in status


def test_queues_are_saved_and_loaded(tmp_path):
    rt = runtime(tmp_path, [])
    rt.queue_add(SWEEP, note="first")
    rt.queue_add(STEP)
    assert rt.queue_save("overnight") == "saved the queue as overnight.yaml"
    rt.queue_change("clear")
    rt.queue_change("remove", "1")
    rt.queue_change("remove", "1")
    assert not rt.queue.items
    assert "added 2 items from overnight" in rt.queue_load("overnight")
    assert [i.note for i in rt.queue.items] == ["first", ""]
    assert "overnight" in rt.queue_view()["saved"]


def test_a_queue_is_not_dry_run_unless_asked(tmp_path, monkeypatch):
    """One approval for the whole queue, from its plans' drive ranges; no dry run of the queue or of its scans."""
    rt = runtime(tmp_path, [True])
    rt.queue_add(SWEEP)
    rt.queue_add(FIELD)
    rehearsed, real_sim, real_queue = [], rt.engine.simulate, rt._rehearse
    monkeypatch.setattr(rt.engine, "simulate", lambda plan: rehearsed.append("scan") or real_sim(plan))
    monkeypatch.setattr(rt, "_rehearse", lambda items: rehearsed.append("queue") or real_queue(items))
    assert rt.queue_start().startswith("queue started")
    rt._queue_thread.join(120)
    assert [i.status for i in rt.queue.items] == ["done", "done"] and rehearsed == []


def test_auto_mode_starts_a_low_risk_queue_without_asking(tmp_path):
    rt = runtime(tmp_path, [])                                  # nobody answers: asking would be an error
    rt.gate.auto = True
    rt.queue_add(SWEEP)
    rt.queue_add(FIELD.replace("[0.5, 1]", "[0.25, 0.5]"))     # a sixth of the way to the magnet's limit
    out = rt.queue_start()                                      # rehearsed on the simulated lab, every range small
    assert out.startswith("queue started"), out
    rt._queue_thread.join(120)
    assert [i.status for i in rt.queue.items] == ["done", "done"]
    asks = [e for e in rt.gate.log.events() if e.get("type") == "ask"]
    assert asks[0]["answer"] == "auto-approved" and "the dry run finished cleanly" in asks[0]["note"]
    rt.gate.approver = ScriptedApprover([False])
    rt.queue_add("step: gate high\nset: { smu.V: 10 }")         # beyond the lab's auto range for smu.V
    assert rt.queue_start().startswith("declined")
    assert "Auto mode does not cover this: smu.V leaves its auto range" in str(rt.gate.approver.requests[0])
