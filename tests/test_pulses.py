"""Pulse sequences for spin experiments, the virtual NV set-up, and the fits that read Rabi and relaxation curves."""
import math
import random

import pytest

from labharness import analysis, build_virtual_lab, pulses
from labharness.clock import SimClock
from labharness.engine import RunEngine
from labharness.gate import SafetyGate, ScriptedApprover
from labharness.sim import nv


def test_each_protocol_has_the_right_timing():
    rabi = pulses.build("rabi", {"tau": 400e-9, "t_laser": 3e-6, "t_gate": 300e-9, "t_wait": 1e-6, "gap": 20e-9})
    ns = lambda spans: [tuple(round(t * 1e9) for t in span) for span in spans]   # noqa: E731
    assert ns(rabi.channels["mw"]) == [(0, 400)]
    assert ns(rabi.channels["laser"]) == [(420, 3420)]
    assert [(w, round(a * 1e9), round(b * 1e9)) for w, a, b in rabi.windows] == [("signal", 420, 720), ("reference", 3120, 3420)]
    assert rabi.period == pytest.approx(4420e-9)
    echo = pulses.build("hahn_echo", {"tau": 2e-6, "t_pi": 100e-9})
    mw = echo.channels["mw"]
    assert [round((b - a) * 1e9) for a, b in mw] == [50, 100, 50]                    # pi/2, pi, pi/2
    assert round((mw[1][0] - mw[0][1]) * 1e9) == round((mw[2][0] - mw[1][1]) * 1e9) == 1000   # tau/2 each side
    ramsey = pulses.build("ramsey", {"tau": 1e-6, "t_pi": 100e-9})
    assert round((ramsey.channels["mw"][1][0] - ramsey.channels["mw"][0][1]) * 1e9) == 1000
    assert pulses.build("t1", {"tau": 1e-3}).channels["mw"] == []
    cw = pulses.build("cw", {"t_laser": 2e-6})
    assert cw.duty("mw") == pytest.approx(0.5) and cw.duty("laser") == pytest.approx(1.0)
    with pytest.raises(ValueError):
        pulses.build("spin_lock")


def test_segments_and_patterns_add_up_to_the_period():
    seq = pulses.build("hahn_echo", {"tau": 1e-6}, delays={"laser": 500e-9})
    segs = seq.segments()
    assert sum(d for d, _ in segs) == pytest.approx(seq.period)
    for ch in pulses.CHANNELS:
        pat = seq.pattern(ch)
        assert sum(n for n, _ in pat) == round(seq.period * 1e9)
        assert all(a[1] != b[1] for a, b in zip(pat, pat[1:]))                   # levels alternate
    # the AOM's 500 ns lag: the laser starts 500 ns earlier, wrapping into the previous period's end
    plain = pulses.build("hahn_echo", {"tau": 1e-6})
    assert seq.channels["laser"][0][0] == pytest.approx(plain.channels["laser"][0][0] - 500e-9)
    early = pulses.build("rabi", {"tau": 0}, delays={"laser": 100e-9})
    assert early.channels["laser"][-1][1] == pytest.approx(early.period)       # wrapped: ends at the period's end
    assert "laser" in seq.diagram() and "#" in seq.diagram()


def test_a_sequence_is_checked_before_it_plays():
    assert pulses.check(pulses.build("pulsed_odmr")) == []
    hot = pulses.build("rabi", {"tau": 20e-6, "t_wait": 0})
    assert any("microwave is on" in p for p in pulses.check(hot))
    assert pulses.check(hot, {"max_mw_duty": 0.9}) == []
    overlap = pulses.build("pulsed_odmr", {"t_laser": 400e-9, "t_gate": 300e-9})
    assert any("overlap" in p for p in pulses.check(overlap))
    short = pulses.build("pulsed_odmr", {"t_pi": 5e-9})
    assert any("resolution" in p for p in pulses.check(short, {"resolution": 10e-9}))
    assert pulses.counts_split([10, 2, 11, 3]) == (21.0, 5.0)


def test_nv_physics_has_the_right_shape():
    lo, hi = nv.resonances(0.0)
    assert lo < 2.870e9 < hi and hi - lo == pytest.approx(4e6)
    assert nv.resonances(0.005)[1] - nv.resonances(0.005)[0] == pytest.approx(2 * math.hypot(2e6, 140e6))
    pi = 0.5 / nv.rabi_frequency(0.0)
    on = lambda proto, **p: nv.flip_probability(proto, p, lo, 0.0, True, 0.0)   # noqa: E731
    assert on("rabi", tau=pi) > 0.8 > on("rabi", tau=2 * pi)
    assert on("pulsed_odmr", t_pi=pi) > 0.8 and nv.flip_probability("pulsed_odmr", {"t_pi": pi}, 2.95e9, 0.0, True, 0.0) < 0.05
    assert on("hahn_echo", tau=1e-6) < 0.05 < on("hahn_echo", tau=100e-6)
    assert on("t1", tau=1e-6) < 0.01 < on("t1", tau=10e-3)
    assert nv.flip_probability("rabi", {"tau": pi}, lo, 0.0, False, 0.0) == 0.0     # no microwave, no flips


@pytest.fixture
def engine():
    lab = build_virtual_lab(clock=SimClock(), seed=3)
    return RunEngine(SafetyGate(lab, ScriptedApprover([True] * 20))), lab


def test_cw_odmr_finds_the_resonance(engine):
    eng, _ = engine
    r = eng.run("""
plan: cw_odmr
setup: { laser.emission: on, laser.shutter: open, laser.power: 2, mw.output: on, mw.power: -10 }
sweep: { mw.frequency: {from: 2.84e9, to: 2.90e9, points: 61} }
measure: [counter.rate]
""")
    assert r.status == "done", r.message
    lead = r.analysis["series"][0]
    assert lead["kind"] == "dip" and "2.870" in lead["summary"]


def test_rabi_gives_the_pi_time(engine):
    eng, lab = engine
    r = eng.run("""
plan: rabi
setup: { laser.emission: on, laser.shutter: open, laser.power: 2, mw.output: on, mw.power: 0, mw.frequency: 2.868e9,
         pg.protocol: rabi, pg.running: on }
sweep: { pg.tau: {from: 0, to: 4.0e-7, points: 41} }
measure: [counter.signal, counter.reference, counter.contrast]
after: { pg.running: off }
""")
    assert r.status == "done", r.message
    contrast = next(s for s in r.analysis["series"] if s["y"] == "counter.contrast")
    assert contrast["kind"] == "oscillation"
    f = contrast["params"]["frequency"][0]
    assert 4e6 < f < 7e6 and 70e-9 < 0.5 / f < 125e-9                       # 5 MHz drive at 0 dBm: pi ≈ 100 ns
    assert r.analysis["assessment"].lower().startswith("counter.contrast oscillates")   # the telling curve leads
    assert "π pulse" in r.analysis["next"]
    assert lab.instruments["pg"].read("running") is False


def test_the_generator_refuses_a_sequence_that_would_heat(engine):
    eng, lab = engine
    gate = eng.gate
    assert gate.set("pg", "protocol", "rabi").ok
    assert gate.set("pg", "running", True).ok
    r = gate.set("pg", "tau", 9e-3)                                           # MW on 99.9 % of the time
    assert not r.ok and "microwave is on" in r.message
    assert lab.instruments["pg"].read("tau") != 9e-3


def test_the_oscillation_fit_reads_a_noisy_rabi_curve():
    rng = random.Random(1)
    xs = [i * 10e-9 for i in range(60)]
    ys = [1 - 0.05 * (1 - math.cos(2 * math.pi * 4e6 * x) * math.exp(-x / 1e-6)) + rng.gauss(0, 0.008) for x in xs]
    s = analysis.analyze_curve(xs, ys, "pg.tau", "contrast", "s", "")
    assert s["kind"] == "oscillation"
    assert s["params"]["frequency"][0] == pytest.approx(4e6, rel=0.05)
    assert any(r["name"].startswith("half period") for r in s["results"])
    flat = analysis.analyze_curve(xs, [1 + rng.gauss(0, 0.008) for _ in xs], "pg.tau", "contrast", "s", "")
    assert flat["kind"] in ("flat", "steady")                                  # noise is not an oscillation


def test_kelvin_shows_a_sequence_before_playing_it(tmp_path):
    pytest.importorskip("mcp")
    from labharness import demo_envelope
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.agent.server import INSTRUCTIONS
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, WebApprover(timeout=1), tmp_path, demo_envelope(lab))
    text = rt.pulse_sequence("ramsey", {"tau": 1e-6}, "pg")
    assert text.startswith("ramsey:") and "period" in text and "checks: ok" in text and "mw" in text
    assert "problems:" in rt.pulse_sequence("rabi", {"tau": 50e-6, "t_wait": 0}, "")
    assert "unknown protocol" in rt.pulse_sequence("spin_lock", {}, "")
    assert "show_sequence" in INSTRUCTIONS and "Rabi" in INSTRUCTIONS
