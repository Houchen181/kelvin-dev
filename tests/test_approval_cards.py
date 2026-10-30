"""Approval cards that tell the truth (field tests: cards without a ramp rate, a duration or the end state; a cryostat
cooling left off because it was "toward safe"; an output switched on into current mode with nothing on its card; an
output a queue never switched on; 41 approvals for one plan on a draft card; an excitation that took four cards
before the run's own).

A run's one card shows every drive it changes (toward safe too), the rate each moves at as read from the instrument,
how long the run takes and what it leaves on at the end. An output-on card says what the output applies and the
instrument's mode. An approved plan covers each of its in-limit steps, on a draft card too, and what a run needs set
up first (an excitation started, a laser on) goes in its setup, on that same card."""
import re

import pytest
from labsim_bench import labsim_bench  # noqa: F401  (the fixture)

from labharness import load_plan
from labharness.cards import Action

COOL_AND_SWEEP = """
plan: cool_and_sweep
setup: { tc.setpoint: 4, smu.output: on, smu.compliance_I: 1.0e-6 }
sweep: { smu.V: {from: 0, to: 2, step: 0.5} }
settle: 1
measure: [smu.I, tc.T_sample]
after: { smu.V: 0 }
"""


def card(approver, n=0):
    return str(approver.requests[n])


def test_a_plan_card_shows_every_drive_it_changes_the_duration_and_what_it_leaves_on(lab, make_engine):
    engine, gate, approver = make_engine([False])
    lab.instruments["tc"].write("heater_range", "low")
    lab.instruments["tc"].write("setpoint", 20.0)      # warm: the plan cools it, toward its safe value
    report = engine.run(COOL_AND_SWEEP)
    assert report.status == "declined"
    text = card(approver)
    title = approver.requests[0].title
    # how long, without a dry run: at least, since tc.setpoint's ramp has no rate to read
    assert title == "Run plan cool_and_sweep: 5 points, at least ~13 s", title
    assert "tc.setpoint: now 20 K, plan uses 4 K, toward its safe value" in text     # cooling is on the card too
    assert "smu.compliance_I: now 100 µA, plan uses 1 µA, toward its safe value" in text
    assert "smu.output: now off, plan uses on; it applies smu.V and smu.compliance_I" in text
    assert "smu.V: now 0 V, plan uses 0 … 2 V; ramp 0.5 V/s" in text
    assert "At the end it leaves tc.setpoint 4 K, smu.output on and smu.compliance_I 1 µA; back at safe: smu.V 0 V" \
        in text
    # its card gives no rate: its time isn't in the estimate, and the card says so
    assert "Not in the ~13 s: tc.setpoint's ramp (its card gives no rate Kelvin can read)" in text


def test_the_duration_counts_ramps_settles_and_waits(lab, make_engine):
    from labharness.forecast import Forecast
    engine, gate, approver = make_engine([])
    plan = load_plan(COOL_AND_SWEEP.replace("tc.setpoint: 4, ", ""))
    engine.validate(plan)
    f = Forecast(engine).plan(plan)
    # smu.V ramps 0 → 2 V and back at 0.5 V/s (8 s), 5 settles of 1 s; the output and compliance switch at once; and
    # each point's readings: smu.I integrates for 1 NPLC (1/60 s on its twin)
    assert abs(f.seconds - (13.0 + 5 / 60)) < 0.01, f.seconds
    f = Forecast(engine).plan(load_plan("plan: t\ntime: {every: 2, points: 31}\nmeasure: [smu.I]\nrepeat: 2\n"))
    assert abs(f.seconds - (2 * 30 * 2 + 62 / 60)) < 0.01   # a time series waits between its points, in every repeat


def test_a_card_says_when_an_output_the_plan_drives_stays_off(lab, make_engine):
    """A queue swept a gate whose output was off throughout, and its card didn't say so (l08)."""
    engine, gate, approver = make_engine([False])
    engine.run("plan: dark_gate\nsweep: { smu.V: {from: 0, to: 2, step: 1} }\nmeasure: [smu.I]\n")
    assert "smu.output is off, and the plan doesn't switch it on: smu.V won't reach the sample" in card(approver)


def test_an_instrument_ramp_shows_the_rate_read_from_the_instrument(lab, make_engine):
    engine, gate, approver = make_engine([False, False, False])
    lab.instruments["magnet"].write("rate", 0.25)
    engine.run("plan: field\nsweep: { magnet.B: {values: [0.5, 1]} }\nmeasure: [lockin.X]\nafter: { magnet.B: 0 }\n")
    text = card(approver)
    # 0 → 0.5 → 1 T and back to 0 at 0.25 T/min: 8 minutes; the interlock it needs, with its condition now
    assert re.search(r"magnet\.B: now 0 T, plan uses 0 … 1 T; at 0\.25 T/min \(magnet\.rate, read now\), ~8\.0 min; "
                     r"interlock: needs tc\.T_sample < 10 \(now 1\.[56]\d* K\)", text), text
    assert approver.requests[0].title == "Run plan field: 2 points, ~8.0 min"
    assert "At the end everything it changed is back at safe: magnet.B 0 T" in text
    engine.run("plan: field\nsetup: { magnet.rate: 0.1 }\nsweep: { magnet.B: {values: [0.5, 1]} }\n"
               "measure: [lockin.X]\nafter: { magnet.B: 0 }\n")
    text = card(approver, 1)
    assert "magnet.B: now 0 T, plan uses 0 … 1 T; at 0.1 T/min (the plan sets magnet.rate), ~20.0 min" in text, text
    assert "magnet.rate: now 0.25 T/min, plan uses 0.1 T/min, toward its safe value" in text
    assert approver.requests[1].title.endswith("~20.0 min")
    r = gate.set("magnet", "B", 1)                          # a person's own change: the same rate, read now
    assert r.action == "declined"
    assert "magnet.B 0 T → 1 T; at 0.25 T/min (magnet.rate, read now), ~4.0 min" in card(approver, 2), card(approver, 2)


def test_an_output_on_card_says_what_it_applies_and_how_the_instrument_is_set(lab, make_gate):
    gate, approver = make_gate([False])
    smu = lab.instruments["smu"]
    smu.write("V", 3.0)                                       # a level left from before, with the output off
    gate.set("smu", "output", True)
    text = card(approver)
    assert "smu.output off → on; it applies smu.V at 0 V (its safe value, set first) and smu.compliance_I at " \
           "100 µA (as it is now)" in text, text


def test_a_draft_cards_settings_in_an_approved_plan_ask_once_on_its_card(lab, make_engine):
    """On a draft card each setting asks (nobody has checked which are drives), but inside a plan a person approved
    every step asked again: 41 cards for one delay scan (l06)."""
    engine, gate, approver = make_engine([True])
    smu = lab.instruments["smu"]
    smu.backend.simulated = False
    smu.card.status = "draft"
    report = engine.run("plan: nplc_scan\nsetup: { smu.output: on }\nsweep: { smu.nplc: {values: [1, 2, 5, 10]} }\n"
                        "measure: [smu.I]\nafter: { smu.output: off }\n")
    assert report.ok, report.message
    assert len(approver.requests) == 1, [str(r) for r in approver.requests]
    text = card(approver)
    assert "smu.nplc: now 1 PLC, plan uses 1 … 10 PLC" in text and "isn't sure about smu" in text
    r = gate.set("smu", "nplc", 3)                          # outside the plan: it asks, as before
    assert r.action == "error" or len(approver.requests) == 2


def draft_smu_queue(tmp_path):
    """A runtime whose source-meter is a real instrument on a draft card: each of its settings asks, outside an
    approved run or queue."""
    from test_queue import runtime
    rt = runtime(tmp_path, [True])                          # one approval: another fails the item that asks
    smu = rt.lab.instruments["smu"]
    smu.backend.simulated = False
    smu.card.status = "draft"
    return rt, smu


def test_a_draft_cards_settings_in_a_queue_ask_once_on_the_queues_card(tmp_path):
    """Overnight flagged it: a draft card's settings in a queue's scans and steps must be covered by the queue's one
    card, or the night asks again at each item, with nobody there."""
    rt, smu = draft_smu_queue(tmp_path)
    rt.queue_add("plan: nplc_scan\nsetup: { smu.output: on }\nsweep: { smu.nplc: {values: [1, 2]} }\n"
                 "measure: [smu.I]\nafter: { smu.output: off }\n")
    rt.queue_add("step: slower\nset: { smu.nplc: 5 }\nwait: 1\n")
    rt.queue_add("plan: iv\nsetup: { smu.output: on }\nsweep: { smu.V: {from: 0, to: 1, step: 0.5} }\n"
                 "measure: [smu.I]\nafter: { smu.V: 0, smu.output: off, smu.nplc: 1 }\n")
    out = rt.queue_start()
    assert out.startswith("queue started"), out
    rt._queue_thread.join(60)
    assert [i.status for i in rt.queue.items] == ["done"] * 3, [(i.name, i.message) for i in rt.queue.items]
    assert len(rt.approver.requests) == 1, [str(r) for r in rt.approver.requests]
    assert "smu.nplc: now 1 PLC, plan uses 1 … 5 PLC; Kelvin isn't sure about smu" in str(rt.approver.requests[0])


def test_what_a_queue_sets_a_draft_card_to_as_it_is_now_is_covered_too(tmp_path):
    """A queue covers its whole path: a draft card's setting it only ever sets to what it reads now, and an output
    it only ever switches off, are covered as well, so a change at the front panel between two scans of the night
    doesn't make the next one ask (and fail, with nobody there)."""
    rt, smu = draft_smu_queue(tmp_path)
    plan = "plan: dark_%d\nsetup: { smu.output: off, smu.nplc: 1 }\ntime: { every: 1, points: 3 }\nmeasure: [smu.I]\n"
    rt.queue_add(plan % 1)
    rt.queue_add(plan % 2)
    shown, changed = rt.engine.progress, []

    def progress(event):
        shown(event)
        if event["event"] == "end" and not changed:        # someone at the front panel, after the first scan
            smu.write("nplc", 2)
            smu.write("output", True)
            changed.append(event["run_id"])
    rt.engine.progress = progress
    assert rt.queue_start().startswith("queue started")
    rt._queue_thread.join(60)
    assert changed and [i.status for i in rt.queue.items] == ["done", "done"], \
        [(i.name, i.message) for i in rt.queue.items]
    assert len(rt.approver.requests) == 1 and smu.read("nplc") == 1 and smu.read("output") is False
    assert rt.approver.requests[0].title.startswith("Run the queue: 2 scans")   # asked at the start, not at night
    text = str(rt.approver.requests[0])
    assert "smu.output: now off, plan uses off; Kelvin isn't sure about smu" in text, text
    assert "smu.nplc: now 1 PLC, plan uses 1 PLC; Kelvin isn't sure about smu" in text, text


def test_a_draft_setting_a_queue_item_sets_after_a_stop_is_covered_too(tmp_path):
    """An item's on_abort setting on a draft card asked in the middle of the night's clean-up, after a trip."""
    rt, smu = draft_smu_queue(tmp_path)
    rt.queue_add("plan: leaky\nsetup: { smu.output: on }\nsweep: { smu.V: {from: 0, to: 14, step: 1} }\n"
                 "measure: [smu.I]\non_abort: { smu.nplc: 2 }\n")   # the gate's leakage guard trips on the way up
    assert rt.queue_start().startswith("queue started")
    rt._queue_thread.join(60)
    item = rt.queue.items[0]
    assert item.status == "failed" and "tripped" in item.message, item.message
    assert "then smu.nplc 1 PLC → 2 PLC (queue approval)" in item.message and smu.read("nplc") == 2, item.message
    assert len(rt.approver.requests) == 1 and smu.read("output") is False and smu.read("V") == 0
    assert "smu.nplc: now 1 PLC, plan uses 2 PLC; Kelvin isn't sure about smu" in str(rt.approver.requests[0])


def test_a_setup_action_runs_once_on_the_runs_own_card(lab, make_engine):
    """An excitation took three cards before the run's own (amplitude, output on, start the waveform: l10)."""
    engine, gate, approver = make_engine([True])
    smu = lab.instruments["smu"]
    sent = []
    smu.card.actions["arm"] = Action("arm", ["ARM"], "Arm the source's trigger", drives=True)
    smu.backend.simulated = False
    real_write = smu.backend.write
    smu.backend.write = lambda cmd: sent.append(cmd) if cmd == "ARM" else real_write(cmd)
    report = engine.run("plan: armed\nsetup: { smu.output: on, smu.V: 1, smu.arm: run }\nmeasure: [smu.I]\n"
                        "after: { smu.V: 0 }\n")
    assert report.ok, report.message
    assert sent == ["ARM"] and len(approver.requests) == 1
    assert "At the start: smu.arm (Arm the source's trigger), which drives the sample" in card(approver)
    bad = engine.run("plan: x\nsetup: { smu.arm: off }\nmeasure: [smu.I]\n")
    assert bad.status == "refused" and "smu.arm is one of smu's actions" in bad.message


def test_an_on_abort_action_that_fails_never_stops_the_clean_up(lab, make_engine):
    engine, gate, approver = make_engine([True])
    smu = lab.instruments["smu"]
    smu.card.actions["halt"] = Action("halt", ["HALT"], "Halt the source's trigger")
    smu.backend.simulated = False
    real_write = smu.backend.write

    def write(cmd):
        if cmd == "HALT":
            raise TimeoutError("no answer")
        real_write(cmd)
    smu.backend.write = write
    report = engine.run("plan: halted\nsetup: { smu.output: on }\nsweep: { smu.V: {from: 0, to: 14, step: 1} }\n"
                        "measure: [smu.I]\non_abort: { smu.halt: run }\n")   # the gate's leakage guard trips
    assert report.status == "aborted" and "tripped" in report.message, report.message
    assert "then smu.halt: smu (Virtual Source-Meter) doesn't answer [no answer]" in report.message, report.message
    assert lab.read("smu", "V") == 0 and lab.read("smu", "output") is False   # and the drives went to safe first
    bad = engine.run("plan: x\nsetup: { smu.output: on }\nmeasure: [smu.I]\non_abort: { smu.arm: run }\n")
    assert bad.status == "refused"                     # no such action: a parameter it doesn't have
    smu.card.actions["arm"] = Action("arm", ["ARM"], "Arm the source's trigger", drives=True)
    bad = engine.run("plan: x\nsetup: { smu.output: on }\nmeasure: [smu.I]\non_abort: { smu.arm: run }\n")
    assert bad.status == "refused" and "after a stop only actions that don't can run" in bad.message


def test_a_queue_whose_scans_start_an_action_asks_once_on_its_card(tmp_path):
    from test_queue import runtime
    rt = runtime(tmp_path, [True])                          # one approval: another would fail the test
    smu = rt.lab.instruments["smu"]
    sent = []
    smu.card.actions["arm"] = Action("arm", ["ARM"], "Arm the source's trigger", drives=True)
    smu.backend.simulated = False
    real_write = smu.backend.write
    smu.backend.write = lambda cmd: sent.append(cmd) if cmd == "ARM" else real_write(cmd)
    for n in (1, 2):
        rt.queue_add("plan: armed_%d\nsetup: { smu.arm: run }\nmeasure: [smu.I]\n" % n)
    out = rt.queue_start()
    assert out.startswith("queue started"), out
    rt._queue_thread.join(60)
    assert [i.status for i in rt.queue.items] == ["done", "done"], [(i.name, i.message) for i in rt.queue.items]
    assert sent == ["ARM", "ARM"]
    assert "At the start: smu.arm (Arm the source's trigger), which drives the sample" in str(rt.approver.requests[0])


def test_a_queues_actions_count_only_on_the_queues_own_thread(lab, make_gate):
    """Another chat's run, working while the queue runs, never borrows the queue's approval of an action."""
    import threading
    gate, _ = make_gate()
    gate.queue_actions = frozenset({"smu.arm"})
    seen = []
    with gate.standing({}):
        assert gate.action_covered("smu.arm") and not gate.action_covered("smu.fire")
        other = threading.Thread(target=lambda: seen.append(gate.action_covered("smu.arm")))
        other.start()
        other.join()
    assert seen == [False] and not gate.action_covered("smu.arm")


def test_a_queue_card_shows_its_duration_and_where_the_queue_leaves_the_lab(tmp_path):
    from test_queue import FIELD, STEP, SWEEP, runtime
    rt = runtime(tmp_path, [False])
    rt.queue_add(SWEEP)
    rt.queue_add(STEP)
    rt.queue_add(FIELD)
    out = rt.queue_start()
    assert out.startswith("declined"), out
    request = rt.approver.requests[0]
    assert request.title.startswith("Run the queue: 2 scans and 1 step, ~"), request.title
    text = str(request)
    assert "magnet.B: now 0 T, plan uses 0 … 1 T; at" in text, text
    assert "At the end it leaves lockin.amplitude 50 mV and smu.output on; back at safe: smu.V 0 V, magnet.B 0 T" \
        in text, text



def test_a_part_of_a_plan_the_forecast_does_not_know_is_shown_with_its_time_unknown(lab, make_engine):
    """Newer plan steps (a kind of sweep or a plan key added after forecast.py) must never break a card: the drive is
    shown, its time unknown."""
    from labharness.plans import Axis
    engine, gate, approver = make_engine([False])
    plan = load_plan(COOL_AND_SWEEP.replace("tc.setpoint: 4, ", ""))
    plan.axes.append(Axis("tc", "setpoint", [2.0], kind="spiral"))   # a kind this forecast doesn't know
    plan.source["pulses"] = "a newer key"
    assert engine.run(plan).status == "declined"
    text, title = card(approver), approver.requests[0].title
    assert title.endswith(", at least ~13 s"), title
    assert "tc.setpoint: now 1.6 K, plan uses 2 K" in text, text
    assert "Not in the ~13 s: tc.setpoint's spiral sweep; cool_and_sweep's pulses" in text, text


def test_a_forecast_that_fails_never_loses_the_card(lab, make_engine, monkeypatch):
    from labharness.forecast import Forecast
    engine, gate, approver = make_engine([False])

    def broken(self, plan, axis, outer):
        raise RuntimeError("boom")
    monkeypatch.setattr(Forecast, "_axis", broken)
    assert engine.run(COOL_AND_SWEEP).status == "declined"
    text = card(approver)
    assert "smu.V: now 0 V, plan uses 0 … 2 V; ramp 0.5 V/s" in text      # the gate's own lines are all there
    assert "the rest of cool_and_sweep (boom)" in text, text
    assert "at least ~" in approver.requests[0].title or "time not known" in approver.requests[0].title


def test_a_queue_step_part_the_forecast_does_not_know_is_shown(lab, make_engine):
    import dataclasses
    from labharness.forecast import Forecast
    from labharness.scanqueue import Step
    engine, gate, approver = make_engine([])

    @dataclasses.dataclass
    class Later(Step):
        until: str = ""
    f = Forecast(engine, "the queue").queue_step(Later("cool", [("tc", "setpoint", 0.0)], None, 30, "tc.T_sample < 2"))
    assert f.seconds == 30 and "cool's until" in f.not_counted() and f.duration() == ", at least ~30 s"


def test_validate_plan_says_what_the_runs_card_will_ask(tmp_path):
    from test_queue import runtime
    rt = runtime(tmp_path, [])
    smu = rt.lab.instruments["smu"]
    smu.card.actions["arm"] = Action("arm", ["ARM"], "Arm the source's trigger", drives=True)
    out = rt.validate("plan: armed\nsetup: { smu.arm: run }\nmeasure: [smu.I]\n")
    assert "needs approval: At the start: smu.arm (Arm the source's trigger), which drives the sample" in out, out
    smu.backend.simulated = False
    smu.card.status = "draft"
    out = rt.validate("plan: nplc\nsweep: { smu.nplc: {values: [1, 2]} }\nmeasure: [smu.I]\n")
    assert "needs approval: smu.nplc: now 1 PLC, plan uses 1 … 2 PLC; Kelvin isn't sure about smu" in out, out


def test_the_portal_the_phone_and_the_approval_page_show_the_same_card(tmp_path):
    """The chat's card (the approver's event), the Lab panel, the phone and the approval page (all from ui_state's
    pending) show one title and the same lines; the phone's notice gives the title and the first line asked."""
    import threading
    import pytest
    pytest.importorskip("mcp")
    from simlab import build_virtual_lab
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    rt = LabRuntime(build_virtual_lab(seed=1), WebApprover(timeout=10), tmp_path / "data")
    rt.auto_report = "off"
    events = []
    rt.approver.subscribe(events.append)
    rt.lab.instruments["tc"].write("heater_range", "low")
    rt.lab.instruments["tc"].write("setpoint", 20.0)
    done = []
    worker = threading.Thread(target=lambda: done.append(rt.run(COOL_AND_SWEEP)), daemon=True)
    worker.start()
    asked = rt.approver.wait_for_pending(timeout=10)
    assert asked is not None
    [pending] = rt.ui_state()["pending"]
    [event] = [e for e in events if e["id"] == asked["id"]]
    assert pending["title"] == event["title"] == asked["title"] and pending["items"] == event["items"] == asked["items"]
    assert pending["where"] == event["where"] == "SIMULATOR"
    assert any(i.startswith("At the end it leaves") for i in pending["items"])
    assert pending["items"][0].startswith("smu.output: now off, plan uses on")       # asked lines first
    notices = [(n["kind"], n["title"], n["text"]) for n in rt.notifier.notices]   # what reaches the phone's alert
    assert ("approval", "Approval needed: " + pending["title"], pending["items"][0]) in notices
    assert rt.answer(asked["id"], False)
    worker.join(30)
    assert done and done[0].startswith("declined")


def test_the_agent_is_told_to_put_a_runs_prep_in_its_setup():
    import pytest
    pytest.importorskip("mcp")
    from labharness.agent.server import INSTRUCTIONS
    text = " ".join(INSTRUCTIONS.split())
    assert "One run, one approval: what a run needs set up first goes in its plan's setup, not in set_value or " \
           "do_action calls before it" in text and "isrc.wave_start: run" in text

# ------------------------------------------------------------------ what plans do since wave 2, counted on the card:
# ramp: sweeps, until: and while:, a hold per step, stabilize near:, retry (their cards said "time not counted")

FIELD_RAMP = """
plan: field_ramp
sweep:
  magnet.B: {ramp: [{to: 1, rate: 0.5}, {to: 0.2, rate: 0.25, hold: 30}], every: 5}
measure: [tc.T_sample]
after: {magnet.B: 0}
"""


def test_a_ramp_leg_takes_its_distance_over_its_rate_and_its_hold(lab, make_engine):
    engine, gate, approver = make_engine([False, True])
    assert engine.run(FIELD_RAMP).status == "declined"
    title, text = approver.requests[0].title, card(approver)
    # up to 1 T at 0.5 T/min (2 min), down to 0.2 T at 0.25 T/min (3.2 min), its 30 s hold, then back to 0 at the
    # rate the last leg left (48 s)
    assert title == "Run plan field_ramp: measures every 5 s while magnet.B ramps to 1 T at 0.5 T/min, then 200 mT " \
                    "at 0.25 T/min (~70 points), ~6.5 min", title
    assert "magnet.B: now 0 T, plan uses 0 … 1 T; at 0.5 T/min, then 0.25 T/min (the plan sets magnet.rate), ~6.0 min;" \
           " interlock: needs tc.T_sample < 10" in text, text
    assert "Not in" not in text and "sweep" not in text, text
    report = engine.run(FIELD_RAMP)
    assert report.ok and report.duration_s == pytest.approx(390, abs=10)    # what the card said


def test_a_ramp_leg_without_a_rate_goes_at_the_rate_the_instrument_is_set_to(lab, make_engine):
    engine, gate, approver = make_engine([False, False])
    plan = FIELD_RAMP.replace("{to: 1, rate: 0.5}, {to: 0.2, rate: 0.25, hold: 30}", "{to: 0.5}, {to: 0.1, hold: 20}")
    engine.run(plan)
    title, text = approver.requests[0].title, card(approver)
    # 0.9 T in all at the 0.2 T/min the supply reads now (4.5 min), a 20 s hold, and back to 0 (30 s)
    assert title.endswith("ramps to 500 mT, then 100 mT (~60 points), ~5.3 min"), title
    assert "magnet.B: now 0 T, plan uses 0 … 500 mT; at 0.2 T/min (magnet.rate, read now), ~5.0 min" in text, text
    engine.run(plan.replace("sweep:", "setup: {magnet.rate: 0.4}\nsweep:"))   # its setup sets the rate first
    title, text = approver.requests[1].title, card(approver, 1)
    assert title.endswith("ramps to 500 mT, then 100 mT (~33 points), ~2.8 min"), title
    assert "magnet.B: now 0 T, plan uses 0 … 500 mT; at 0.4 T/min (the plan sets magnet.rate), ~2.5 min" in text, text


def test_a_ramp_whose_rate_nobody_can_read_says_so(lab, make_engine):
    engine, gate, approver = make_engine([False])
    engine.run("plan: warm\nsetup: {tc.heater_range: medium}\nsweep: {tc.setpoint: {ramp: 5, every: 10}}\n"
               "measure: [tc.T_sample]\n")
    title, text = approver.requests[0].title, card(approver)
    assert title == "Run plan warm: measures every 10 s while tc.setpoint ramps to 5 K (as many points as it takes), " \
                    "time not known", title
    assert "Not in its time: tc.setpoint's ramp (its card gives no rate Kelvin can read)" in text, text


def test_a_ramps_rate_on_a_magnet_supply_is_a_drive_asked_with_the_run(lab, make_engine):
    """A supply's ramp rate is a drive (a fast ramp risks a quench): its legs' rates are asked with the field, and
    the card says where the run leaves it (it said everything was back at safe, with the rate left at 0.25 T/min)."""
    engine, gate, approver = make_engine([False])
    engine.run(FIELD_RAMP)
    request = approver.requests[0]
    assert "magnet.rate: now 0.2 T/min, plan uses 0.25 … 0.5 T/min" in request.items[:2], request.items
    assert "At the end it leaves magnet.rate 0.25 T/min; back at safe: magnet.B 0 T" in request.items, request.items


def test_a_ramp_records_about_its_duration_over_every_points(lab, make_engine):
    """The card's count of a ramp's points is the forecast's: its legs at the rates the run sets, in every pass of
    the loops outside it, each pass from where the one before left it."""
    engine, gate, approver = make_engine([False, True, False, True])
    plan = "plan: up\nsetup: {magnet.rate: 0.4}\nsweep: {magnet.B: {ramp: 1, every: 5}}\nmeasure: [tc.T_sample]\n" \
           "after: {magnet.B: 0}\n"
    engine.run(plan)   # 1 T at the 0.4 T/min its setup sets, not the 0.2 T/min the supply reads now: 150 s
    assert approver.requests[0].title.endswith("ramps to 1 T (~31 points), ~5.0 min"), approver.requests[0].title
    assert engine.run(plan).points == 31
    nested = "plan: field_by_gate\nsetup: {smu.output: on}\nsweep:\n  smu.V: {values: [0, 1, 2]}\n" \
             "  magnet.B: {ramp: [{to: 0.5, rate: 0.5}, {to: 0.1}], every: 5}\nmeasure: [tc.T_sample]\n" \
             "after: {magnet.B: 0, smu.V: 0}\n"
    engine.run(nested)   # 0 → 0.5 → 0.1 T, then twice 0.1 → 0.5 → 0.1 T: 300 s at 0.5 T/min
    assert approver.requests[2].title.endswith("(~66 points), ~5.3 min"), approver.requests[2].title
    assert engine.run(nested).points == pytest.approx(66, abs=3)


def test_until_and_while_make_the_time_the_most_it_takes(lab, make_engine):
    engine, gate, approver = make_engine([False, False])
    engine.run(FIELD_RAMP.replace("every: 5}", "every: 5, until: \"magnet.B_actual > 0.5\"}"))
    title, text = approver.requests[0].title, card(approver)
    assert title.endswith("(~70 points), at most ~6.5 min"), title
    assert text.endswith("It may end sooner: magnet.B's ramp ends once magnet.B_actual > 0.5"), text
    engine.run("plan: iv\nsetup: {smu.output: on}\nsweep: {smu.V: {from: 0, to: 2, step: 0.5, "
               "until: \"abs(smu.I) > 1e-7\"}}\nwhile: \"tc.T_sample < 5\"\nmeasure: [smu.I]\nafter: {smu.V: 0}\n")
    title, text = approver.requests[1].title, card(approver, 1)
    assert title == "Run plan iv: 5 points, at most ~8 s", title
    assert "It may end sooner: smu.V skips its other values once abs(smu.I) > 1e-7; the sweep ends once " \
           "tc.T_sample < 5 no longer holds" in text, text


def test_a_hold_per_step_is_counted_step_by_step(lab, make_engine):
    """settle: per value (a staircase's last steps need longer): the card counted the axis's one settle for each."""
    engine, gate, approver = make_engine([False, False])
    plan = "plan: staircase\nsetup: {smu.output: on}\nsweep: {smu.V: {values: [0, 1, 2], settle: [0, 5, 60]}}\n" \
           "settle: 1\nmeasure: [smu.I]\nafter: {smu.V: 0}\n"
    engine.run(plan)   # holds of 0, 5 and 60 s, and 2 V up and back down at 0.5 V/s (8 s)
    assert approver.requests[0].title == "Run plan staircase: 3 points, ~73 s", approver.requests[0].title
    engine.run(plan.replace("[0, 5, 60]", "{2: 60}"))   # the others hold for the plan's settle: (1 s)
    assert approver.requests[1].title == "Run plan staircase: 3 points, ~70 s", approver.requests[1].title


def test_stabilize_near_may_wait_out_its_timeout(lab, make_engine):
    engine, gate, approver = make_engine([False])
    engine.run("plan: steps\nsetup: {tc.heater_range: medium}\nsweep: {tc.setpoint: {values: [3, 5], stabilize: "
               "{read: tc.T_sample, within: 0.02, for: 30, near: 0.05, timeout: 600}}}\nmeasure: [tc.T_sample]\n")
    title, text = approver.requests[0].title, card(approver)
    assert title == "Run plan steps: 2 points, at least ~60 s", title
    assert "Not in the ~60 s: tc.setpoint's ramp (its card gives no rate Kelvin can read); tc.T_sample's stabilize " \
           "(counted at its 30 s at each point; up to its 10.0 min timeout wherever tc.T_sample doesn't come within " \
           "50 mK of its set point)" in text, text


def test_retry_adds_time_nobody_can_count(lab, make_engine):
    engine, gate, approver = make_engine([False])
    engine.run("plan: gate_retry\nsetup: {smu.output: on}\nsweep: {smu.V: {from: 0, to: 2, step: 0.5}}\nsettle: 1\n"
               "measure: [lockin.X, smu.I]\nretry: {times: 3, wait: 5}\n")
    title, text = approver.requests[0].title, card(approver)
    assert title == "Run plan gate_retry: 5 points, at least ~9 s", title
    assert "Not in the ~9 s: readings taken again after a fault (retry: up to 3 times, 5 s apart)" in text, text


def test_a_queues_card_counts_its_ramps_and_says_what_may_end_them(tmp_path):
    from test_queue import runtime
    rt = runtime(tmp_path, [False])
    rt.queue_add(FIELD_RAMP.replace("every: 5}", "every: 5, until: \"magnet.B_actual > 0.5\"}"))
    rt.queue_add("plan: gate_retry\nsetup: {smu.output: on}\nsweep: {smu.V: {from: 0, to: 2, step: 0.5}}\n"
                 "measure: [smu.I]\nafter: {smu.V: 0}\nretry: 2\n")
    assert rt.queue_start().startswith("declined")
    request = rt.approver.requests[0]
    assert request.title == "Run the queue: 2 scans, ~6.6 min (it may end sooner or wait longer)", request.title
    text = str(request)
    assert "magnet.B: now 0 T, plan uses 0 … 1 T; at 0.5 T/min, then 0.25 T/min (the queue sets magnet.rate)" in text
    assert "Not in the ~6.6 min: readings taken again after a fault (gate_retry's retry: up to 2 times, 10 s apart)" \
        in text, text
    assert "It may end sooner: magnet.B's ramp (in field_ramp) ends once magnet.B_actual > 0.5" in text, text

# ------------------------------------------------------------------ on labsim: instruments Kelvin takes for real

TRANSPORT = """lab: labsim transport
instruments:
  gate:
    card: keithley/2400
    address: "GPIB0::24::INSTR"
    limits: { V: [-5, 5], compliance_I: [1e-09, 1e-06] }
  isrc:
    card: keithley/6221
    address: "GPIB0::12::INSTR"
    limits: { I: [-1e-06, 1e-06], wave_amplitude: [2e-12, 1e-06], wave_offset: [-1e-07, 1e-07], compliance_V: [0.1, 10] }
  li_xx:
    card: srs/sr830
    address: "GPIB0::8::INSTR"
    limits: { amplitude: [0.004, 1] }
"""


def labsim_lab(tmp_path, text):
    from labharness.homelab import build_lab
    (tmp_path / "lab.yaml").write_text(text, encoding="utf-8")
    lab = build_lab(tmp_path / "lab.yaml", partial=True)
    assert lab.unavailable == {}, lab.unavailable
    return lab


def labsim_engine(lab, answers, tmp_path):
    from labharness import EventLog, RunEngine, RunStore, SafetyGate, ScriptedApprover
    approver = ScriptedApprover(answers)
    gate = SafetyGate(lab, approver, EventLog(tmp_path / "events.jsonl", lab.clock))
    return RunEngine(gate, RunStore(tmp_path / "runs")), gate, approver


def test_on_labsim_an_output_on_card_says_the_2400_now_sources_current(labsim_bench, tmp_path):
    """l08: someone had put the gate's 2400 into current mode; the card said only "output off → on", and 10 µA went
    into the back gate."""
    labsim_bench("transport")
    lab = labsim_lab(tmp_path, TRANSPORT)
    try:
        engine, gate, approver = labsim_engine(lab, [False, False], tmp_path)
        assert gate.set("gate", "output", True).action == "declined"
        assert "gate: source function voltage" in card(approver)
        lab.instruments["gate"].write_raw(":SOUR:FUNC CURR")    # at the front panel, past Kelvin
        assert gate.set("gate", "output", True).action == "declined"
        text = card(approver, 1)
        assert "gate.output off → on; it applies gate.V at 0 V (its safe value, set first) and gate.compliance_I at " \
               "1 nA (its safe value, set first); gate: source function current" in text, text
        assert lab.read("gate", "output") is False
    finally:
        lab.close()


EXCITED = """
plan: rxx_ac
setup:
  li_xx.reference: external
  isrc.wave_function: sine
  isrc.wave_frequency: 13.333
  isrc.wave_amplitude: 1.0e-7
  isrc.phase_marker: on
  isrc.wave_start: run
time: { every: 1, points: 4 }
measure: [li_xx.X, li_xx.R]
after: { isrc.wave_stop: run }
"""


def test_on_labsim_an_excitation_started_in_setup_is_on_the_runs_one_card(labsim_bench, tmp_path):
    """l10: the excitation took three cards (amplitude, output on, start the waveform) before the run's own, which
    didn't mention it. In the plan's setup it is one card, and the lock-in sees the sample."""
    bench = labsim_bench("transport")
    lab = labsim_lab(tmp_path, TRANSPORT)
    try:
        engine, gate, approver = labsim_engine(lab, [True], tmp_path)
        report = engine.run(EXCITED)
        assert report.ok, report.message
        assert len(approver.requests) == 1
        text = card(approver)
        assert "isrc.wave_amplitude: now 2 pA, plan uses 100 nA; ramp 0.001 A/s" in text, text
        assert "At the start: isrc.wave_start (Arm and start the waveform (output on)), which drives the sample" in text
        assert "won't reach the sample" not in text         # the action switches the output on itself
        assert "At the end it leaves isrc.wave_amplitude 100 nA; then it runs isrc.wave_stop" in text
        assert report.rows[-1]["li_xx.R"] > 1e-5, report.rows      # locked to the 6221's phase marker
        status = next(i["status"] for i in bench.ctl("status")["instruments"] if i["name"] == "isrc")
        assert status["output"] is False and not status["wave"]["running"]     # stopped in its after step
    finally:
        lab.close()


PPMS = """lab: labsim PPMS
instruments:
  ppms:
    card: qd/multivu
    address: "qd://127.0.0.1:5000"
    limits: { T_set: [1.8, 400], B_set: [-9, 9], T_rate: [0.01, 20], B_rate: [1e-05, 0.01] }
  isrc:
    card: keithley/6221
    address: "GPIB0::12::INSTR"
    limits: { I: [-1e-05, 1e-05], compliance_V: [0.1, 10] }
"""

COOLDOWN = """
plan: rt_cooldown
setup: { ppms.T_rate: 20, ppms.T_set: 14, isrc.output: on, isrc.I: 1.0e-6 }
time: { every: 5, points: 3 }
measure: [ppms.T]
after: { isrc.I: 0 }
"""


def test_on_labsim_a_ppms_cooldown_is_on_the_card_with_its_rate_and_time(labsim_bench, tmp_path):
    """l03: the card of an R(T) run listed only the current source; the PPMS cooling to 14 K at 20 K/min, the run's
    main action, wasn't on it because it was toward safe."""
    labsim_bench("ppms", sdk=True)
    lab = labsim_lab(tmp_path, PPMS)
    try:
        engine, gate, approver = labsim_engine(lab, [False], tmp_path)
        assert engine.run(COOLDOWN).status == "declined"
        text, title = card(approver), approver.requests[0].title
        assert re.search(r"ppms\.T_set: now 30\d(\.\d+)? K, plan uses 14 K, toward its safe value; at 20 K/min \(the plan "
                         r"sets ppms\.T_rate\), ~14\.\d min", text), text
        assert "isrc.I: now 0 A, plan uses 0 … 1 µA; ramp 0.001 A/s" in text, text
        assert re.match(r"Run plan rt_cooldown: 3 points, ~1[45]\.\d min$", title), title   # 286 K at 20 K/min
        assert "At the end it leaves ppms.T_set 14 K and isrc.output on; back at safe: isrc.I 0 A" in text
        assert lab.read("ppms", "T") > 250                 # declined: nothing moved
    finally:
        lab.close()


PUMPPROBE = """lab: labsim pump-probe
instruments:
  delay:
    card: newport/smc100
    address: "ASRL6::INSTR"
    limits: { position: [0, 25] }
"""


def test_on_labsim_a_delay_scan_on_a_draft_card_asks_once(labsim_bench, tmp_path):
    """l06: on a draft stage card each step of an approved scan asked again (41 approvals)."""
    import time
    labsim_bench("pumpprobe")
    lab = labsim_lab(tmp_path, PUMPPROBE)
    try:
        stage = lab.instruments["delay"]
        stage.card.status = "draft"                        # the lab's own draft of the card, not yet reviewed
        stage.act("home")                                  # homed earlier, at the start of the day
        deadline = time.monotonic() + 20
        while not str(stage.read("state")).startswith("ready") and time.monotonic() < deadline:
            time.sleep(0.2)
        engine, gate, approver = labsim_engine(lab, [True], tmp_path)
        report = engine.run("plan: t0_coarse\nsweep: { delay.position: {from: 12.0, to: 12.4, step: 0.2} }\n"
                            "measure: [delay.position_actual]\n")
        assert report.ok, report.message
        assert len(approver.requests) == 1, [str(r) for r in approver.requests]
        text = card(approver)
        assert "delay.position: now 0 mm, plan uses 12 … 12.4 mm; at 2.5 mm/s (delay.velocity, read now), ~5 s; " \
               "Kelvin isn't sure about delay: its card is a draft" in text, text
        assert abs(report.rows[-1]["delay.position_actual"] - 12.4) < 0.01
        r = gate.set("delay", "position", 12.0)            # outside the plan, a draft card's setting asks again
        assert r.action == "error" and "unexpected approval request" in r.message
    finally:
        lab.close()
