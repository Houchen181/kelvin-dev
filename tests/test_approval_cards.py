"""Approval cards that tell the truth (field tests: cards without a ramp rate, a duration or the end state; a cryostat
cooling left off because it was "toward safe"; an output switched on into current mode with nothing on its card; an
output a queue never switched on; 41 approvals for one plan on a draft card; an excitation that took four cards
before the run's own).

A run's one card shows every drive it changes (toward safe too), the rate each moves at as read from the instrument,
how long the run takes and what it leaves on at the end. An output-on card says what the output applies and the
instrument's mode. An approved plan covers each of its in-limit steps, on a draft card too, and what a run needs set
up first (an excitation started, a laser on) goes in its setup, on that same card."""
import re

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
    # smu.V ramps 0 → 2 V and back at 0.5 V/s (8 s), 5 settles of 1 s; the output and compliance switch at once
    assert abs(f.seconds - 13.0) < 0.01, f.seconds
    f = Forecast(engine).plan(load_plan("plan: t\ntime: {every: 2, points: 31}\nmeasure: [smu.I]\nrepeat: 2\n"))
    assert abs(f.seconds - 2 * 30 * 2) < 0.01        # a time series waits between its points, in every repeat


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
    """Newer plan steps (a ramp sweep, until:) must never break a card: the drive is shown, its time unknown."""
    from labharness.plans import Axis
    engine, gate, approver = make_engine([False])
    plan = load_plan(COOL_AND_SWEEP.replace("tc.setpoint: 4, ", ""))
    plan.axes.insert(0, Axis("tc", "setpoint", [2.0], kind="ramp"))     # a kind this forecast doesn't know
    plan.source["until"] = "tc.T_sample < 2.5"
    assert engine.run(plan).status == "declined"
    text, title = card(approver), approver.requests[0].title
    assert title.endswith(", at least ~13 s"), title
    assert "tc.setpoint: now 1.6 K, plan uses 2 K" in text, text
    assert "Not in the ~13 s: tc.setpoint's ramp sweep; cool_and_sweep's until" in text, text


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
    from labharness.virtual_lab import build_lab
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
