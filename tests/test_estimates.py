"""Time estimates that count what the lab takes (UX test 2026-10-30, report section 3.5: 2-40x too short everywhere).
"81 points, ~8 s" took 3.6 min (a counter's 1 s integration at each point); a queue's card said ~29 s where its own
dry run said ~6.8 min; a gate ramp took 10x its card. Cards, dry runs and queue cards count each point's readings, a
ramp's steps and settling with one estimator (timing.py), calibrated by what this lab's runs took."""
import re

import pytest

from labharness import ScriptedApprover
from labharness.forecast import Forecast
from labharness.plans import load_plan
from labharness.timing import Timing
from simlab import build_virtual_lab

ODMR = """plan: cw_odmr
setup: {{ counter.integration: {t} }}
sweep: {{ mw.frequency: {{from: 2.80e9, to: 2.94e9, points: 81}} }}
settle: 0.5
measure: [counter.rate]
"""
GATE = """plan: gate
setup: { smu.output: on }
sweep: { smu.V: {from: 0, to: 3, step: 0.1, back: true} }
settle: 0.3
measure: [lockin.X, smu.I]
after: { smu.V: 0, smu.output: off }
"""


def runtime(tmp_path, answers=(), lab=None):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    return LabRuntime(lab or build_virtual_lab(seed=1), ScriptedApprover(list(answers)), tmp_path / "data", None)


def slow_bus(lab, seconds):
    """Every exchange with an instrument takes this long, as on a GPIB bus or through a maker's software (the twins a
    dry run uses answer at once)."""
    for inst in lab.instruments.values():
        for name in ("query", "write"):
            real = getattr(inst.backend, name)
            setattr(inst.backend, name, lambda *a, _real=real: (lab.clock.sleep(seconds), _real(*a))[1])


def minutes(text):
    m = re.search(r"~(\d+(?:\.\d+)?) (s|min|h)\b", text)
    return float(m.group(1)) * {"s": 1 / 60, "min": 1, "h": 60}[m.group(2)]


def test_a_card_counts_each_points_integration_and_agrees_with_its_dry_run(lab, make_engine):
    """t03: "cw_odmr_fine: 81 points, ~8 s" took 3.6 min: the counter's 1 s integration at each point wasn't counted.
    Its twin times it, so the card and the dry run count it the same."""
    engine, gate, _ = make_engine([])
    f = Forecast(engine).plan(load_plan(ODMR.format(t=1)))
    assert f.seconds >= 81 * (1 + 0.5) and f.duration() == ", ~2.0 min"
    sim = engine.simulate(ODMR.format(t=1))
    assert sim.ok and abs(sim.duration_s - f.seconds) < 0.05 * f.seconds
    short = Forecast(engine).plan(load_plan(ODMR.format(t=0.1)))   # the plan's own setting, not the instrument's now
    assert abs(short.seconds - 81 * 0.6) < 0.05 * short.seconds


def test_the_queue_card_and_its_dry_run_say_the_same_time(tmp_path):
    """t03: a queue's card said ~29 s; its dry run, ~6.8 min."""
    rt = runtime(tmp_path, [True])
    rt.queue_add(ODMR.format(t=1))
    rt.queue_add(GATE)
    rehearsed = rt.queue_start(dry_run=True)
    assert rehearsed.startswith("REHEARSAL ONLY")
    said = next(line for line in rehearsed.splitlines() if "would take" in line)
    assert rt.queue_start().startswith("queue started")
    rt._queue_thread.join(300)
    [card] = rt.gate.approver.requests
    assert card.title.startswith("Run the queue: 2 scans")
    assert minutes(card.title) == pytest.approx(minutes(said), rel=0.1) and minutes(card.title) >= 2.5


def test_estimates_count_what_this_labs_runs_took(tmp_path):
    """t01, t07, t10: a gate sweep took 2x its card, a ramp 9x: each step's exchanges with the instrument and its guard
    reading, each point's readings, take time a card didn't count. After one run, every estimate counts what the run
    measured: the card, the dry run (its twins answer at once, and are paced), a person's own change, the time left."""
    rt = runtime(tmp_path, [True] * 6)
    slow_bus(rt.lab, 0.05)
    before = Forecast(rt.engine).plan(rt._plan(GATE)).seconds
    report = rt.engine.run(GATE)
    assert report.ok and report.duration_s > 1.5 * before          # the first card didn't know
    after = Forecast(rt.engine).plan(rt._plan(GATE)).seconds
    assert after == pytest.approx(report.duration_s, rel=0.1)
    sim = rt.engine.simulate(GATE)
    assert sim.duration_s == pytest.approx(report.duration_s, rel=0.1)
    kept = rt.timing.mine()
    assert kept["reads"]["lockin.X"]["s"] >= 0.05
    assert kept["steps"]["smu.V"]["away"] > kept["steps"]["smu.V"]["toward"] > 0.05   # away: its guard read too
    assert kept["reads"]["smu.I"]["settings"]["nplc"] == 1.0
    inst, spec = rt.lab.resolve("smu.V")
    card = rt.gate._describe_move(inst, spec, 0.0, 5.0)              # 50 steps at 0.5 V/s: more than its 10 s
    assert re.search(r"ramp 0\.5 V/s \(~(\d+) s\)", card) and int(re.search(r"\(~(\d+) s\)", card).group(1)) > 14
    again = Timing(lambda: rt.data_dir / "timing.json", lambda: rt.lab.config.name)   # kept for the next start
    assert again.step_s("smu.V", True) == kept["steps"]["smu.V"]["away"]


def test_a_reading_timed_at_other_settings_is_not_taken_for_one_now(tmp_path):
    """A coarse scan at 0.1 s integration doesn't make a 1 s one look short: a reading's time is taken from a run only
    at the settings it ran with; otherwise its twin times it (or the card says it isn't counted)."""
    rt = runtime(tmp_path, [True] * 4)
    slow_bus(rt.lab, 0.3)
    assert rt.engine.run(ODMR.format(t=0.1)).ok
    assert rt.timing.mine()["reads"]["counter.rate"]["settings"]["integration"] == 0.1
    same = Forecast(rt.engine).read_s("counter.rate")                 # the instrument is at 0.1 s now: as measured
    assert same == pytest.approx(0.1 + 0.3, abs=0.05)
    f = Forecast(rt.engine)
    f.plan(rt._plan(ODMR.format(t=1)))                                # this plan sets 1 s: its twin's 1 s
    assert f.read_s("counter.rate") == pytest.approx(1.0, abs=0.01)


def test_a_reading_with_no_time_known_is_said_on_the_card(lab, make_engine, monkeypatch):
    """A reading that takes its own time (a trace, a frame) that neither a run nor a twin times stays out of the
    estimate, and the card says so ("at least")."""
    engine, gate, _ = make_engine([])
    monkeypatch.setattr(lab.instruments["vna"].card, "twin", None)   # a VNA whose card has no twin with physics
    f = Forecast(engine).plan(load_plan("plan: s21\ntime: {every: 1, points: 3}\nmeasure: [vna.S21]\n"))
    assert "the time reading vna.S21 takes" in "; ".join(f.not_counted()) and f.duration().startswith(", at least")


SWEEPS = ["sweep: {smu.V: {from: 0, to: 2, step: 0.5}}\nsettle: 0.1",
          "sweep: {smu.V: {from: -1, to: 1, points: 201, back: true}}",    # through safe: steps away and toward it
          "sweep: {smu.V: {from: 1, to: -1, step: 0.03}}",                  # its last step shorter
          "sweep: {smu.V: {values: [0, 1, 1, 2, -1, 0.5]}}\nsettle: 0.2",  # a value the same as the one before
          "repeat: 3\nsweep: {smu.V: {from: 0, to: 5, step: 0.25, settle: {5: 3}}}",
          "sweep: {magnet.B: {values: [0, 0.5, 1]}, smu.V: {from: 0, to: 1, points: 51}}",   # a ramp at its own rate
          "sweep: {lockin.frequency: {from: 10, to: 100000, points: 41, log: true}}",       # one set a step
          "sweep: {vna.power: {from: -30, to: -10, step: 1}}",
          "sweep: {smu.V: {from: 2, to: 2, points: 5}}",
          "setup: {smu.output: on}\nsweep: {smu.V: {from: 0, to: 3, step: 0.1}}\nafter: {smu.V: 0}\n"
          "time: {every: 2, points: 3}"]


def test_a_sweeps_steps_are_added_up_at_once_to_what_each_takes(tmp_path, monkeypatch):
    """A card's (and the scan editor's) time walked a sweep step by step: a million points took 27 s. Its steps are now
    added up at once, from their sizes and which way they go; the time is what each step's own takes add up to."""
    import time
    from labharness import forecast
    from labharness.timing import Took
    rt = runtime(tmp_path)
    took = Took()   # what steps take in this lab, away from safe and toward it
    for key, away, s in (("smu.V", True, 0.013), ("smu.V", False, 0.007), ("magnet.B", True, 0.4),
                         ("lockin.frequency", False, 0.05)):
        took.step(key, s, away)
    took.point(0.003)
    rt.timing.learn("r0", took, {})

    def walked(text):
        f = Forecast(rt.engine).plan(rt._plan("plan: p\nmeasure: [smu.I]\n" + text))
        return f.seconds, f.duration(), {k: (c.start, c.values, c.untimed) for k, c in f.changes.items()}, f._rates

    at_once = [walked(text) for text in SWEEPS]
    with monkeypatch.context() as m:   # each step's own, one after the other, as the card walked them
        m.setattr(Forecast, "_travels", lambda f, inst, spec, values: (
            sum(f.travel(inst, spec, a, b)[0] for a, b in zip(values, values[1:])),
            all(f.travel(inst, spec, a, b)[1] for a, b in zip(values, values[1:]))))
        m.setattr(Forecast, "_record_all", lambda f, inst, spec, values, t, timed: [
            f._record(inst, spec, v, t if k == 0 else 0.0, timed) for k, v in enumerate(values)])
        m.setattr(forecast, "floats", lambda spec, values: None)
        by_step = [walked(text) for text in SWEEPS]
    for text, (s, said, changes, rates), (s0, said0, changes0, rates0) in zip(SWEEPS, at_once, by_step):
        assert s == pytest.approx(s0, rel=1e-12) and said == said0 and changes == changes0 and rates == rates0, text
    assert [said for _, said, _, _ in at_once] == [", ~5 s", ", ~22 s", ", ~8 s", ", ~15 s", ", ~66 s", ", ~5.3 min",
                                                   ", ~3 s", ", ~2 s", ", ~5 s", ", ~2.3 min"]
    t0 = time.perf_counter()
    big = Forecast(rt.engine).plan(rt._plan("plan: big\nsweep: {smu.V: {from: 0, to: 1, points: 1000000}}\n"
                                            "measure: [smu.I]"))
    assert time.perf_counter() - t0 < 5 and big.duration() == ", ~9.1 h"
