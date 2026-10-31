"""Automatic assessments that said wrong things (UX test 2026-10-30, report section 3.4): a VNA run fitted with a
different model on each row (a Q "drop" of 14 % that wasn't there), a centred resonance called an oscillation with
τ in GHz, a field shift missed; an ODMR doublet called "does not depend on frequency", pulsed runs read through a
first point of 0 counts, Ramsey told to widen its sweep; a parabola called a dip 117 T wide and a gate map's
magnetoresistance a 2-D Gaussian spot; fits of noise given as numbers and failed fits kept unmarked; smooth() pulling
a sweep's ends down; a Hall density from a zero R_H, a lock-in's volts where the measurement chain gives ohms, and a
leakage current leading a gate sweep's headline."""
import math
import random

import pytest

np = pytest.importorskip("numpy")

from labharness import RunEngine, SafetyGate, ScriptedApprover, datatools, expr, fitting, workbench  # noqa: E402
from labharness.analysis import analyze_curve, analyze_run  # noqa: E402
from labharness.clock import SimClock  # noqa: E402
from simlab import build_virtual_lab  # noqa: E402

S21 = {"key": "vna.S21", "unit": "dB", "kind": "trace", "x_unit": "Hz"}


@pytest.fixture
def rt(tmp_path):
    from labharness.agent.runtime import LabRuntime
    return LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 200), tmp_path / "data")


def resonance(f, f0, noise, rng):
    """The test lab's VNA trace: a 25 dB line on -40 dB, 2 MHz wide in dB."""
    return (-40 + 25 / (1 + (2 * (f - f0) / 2e6) ** 2) + rng.normal(0, noise, f.size)).tolist()


def trace_rows(outer, values, f0s, noise=0.05, span=(3.997e9, 4.003e9), points=1201, seed=0):
    rng = np.random.default_rng(seed)
    f = np.linspace(span[0], span[1], points)
    return [{"i": i, outer: v, "vna.S21": resonance(f, f0, noise, rng), "vna.S21.axis": list(span)}
            for i, (v, f0) in enumerate(zip(values, f0s))]

# ------------------------------------------------------------------ VNA traces (t04)


def test_one_model_for_every_trace_of_a_run_so_q_moves_only_when_the_line_does():
    rows = trace_rows("tc.setpoint", [2, 5, 10, 20], [4e9] * 4, noise=0.08)
    a = analyze_run(rows, [{"key": "tc.setpoint", "unit": "K", "kind": "instrument"}], [S21],
                    {"status": "done", "points": 4})
    assert {s["fit"]["model"] for s in a["series"]} == {a["series"][0]["fit"]["model"]}   # one model, every row
    assert all(s["kind"] == "peak" for s in a["series"])
    assert "does not change within the errors" in a["trends"][0] and a["next"].startswith("Nothing changes")


def test_a_centred_resonance_is_a_peak_not_an_oscillation_with_tau_in_ghz():
    rng = np.random.default_rng(4)
    f = np.linspace(3.9985e9, 4.0015e9, 601)                       # a fine scan: the line fills the window
    for noise in (0.05, 0.8, 2.5):                                  # -10 to -50 dBm on the test lab's VNA
        s = analyze_curve(f.tolist(), resonance(f, 4e9, noise, rng), "frequency", "vna.S21", "Hz", "dB")
        assert s["kind"] == "peak", (noise, s["summary"])
        assert "oscillat" not in s["summary"] and not any("τ" in r["name"] for r in s["results"])


def test_a_resonance_that_moves_with_field_is_followed_out_of_the_window():
    fields = [0, 0.5, 1.0, 1.5, 2.0]
    rows = trace_rows("magnet.B", fields, [4e9 * (1 - 3e-4 * b * b) for b in fields])
    a = analyze_run(rows, [{"key": "magnet.B", "unit": "T", "kind": "instrument"}], [S21],
                    {"status": "done", "points": 5})
    trend = a["trends"][0]
    assert "center shifts by -2.70" in trend and "MHz" in trend, trend
    assert "no peak inside the sweep at 2 T" in trend and "Nothing changes" not in a["next"]
    assert a["next"].startswith("Re-centre the sweep on the peak at each magnet.B value")


def test_two_resolved_lines_are_two_and_a_flat_topped_one_is_one():
    rng = np.random.default_rng(5)
    f = np.linspace(2.85e9, 2.89e9, 81)
    lines = sum(-5700 / (1 + ((f - c) / 1.1e6) ** 2) for c in (2.868e9, 2.872e9))   # a 2 % doublet, 4 MHz apart
    s = analyze_curve(f.tolist(), (2.857e5 + lines + rng.normal(0, 530, f.size)).tolist(), "mw.frequency",
                      "counter.rate", "Hz", "counts/s")
    assert s["kind"] == "dip" and s.get("lines") == 2 and "does not depend" not in s["summary"], s["summary"]
    centers = sorted(r["num"] for r in s["results"] if r["name"].startswith("center"))
    assert centers == pytest.approx([2.868e9, 2.872e9], abs=0.3e6) and "MHz apart, centred at" in s["summary"]
    assert next(r["num"] for r in s["results"] if r["name"] == "splitting") == pytest.approx(4e6, abs=0.4e6)
    flat_top = -40 + 25 / (1 + (2 * (f - 2.87e9) / 6e6) ** 2) ** 0.5   # one line, not Lorentzian: not two
    s = analyze_curve(f.tolist(), (flat_top + rng.normal(0, 0.02, f.size)).tolist(), "frequency", "x", "Hz", "dB")
    assert s.get("lines") != 2

# ------------------------------------------------------------------ NV centres (t03)


def pulsed_rows(protocol, x_key, xs, contrast_of, zero_first=True, seed=1):
    rng = random.Random(seed)
    rows = []
    for i, x in enumerate(xs):
        ref = 7250 * (1 - 0.3 * i / len(xs)) + rng.gauss(0, 85)   # fewer repetitions as the sequence lengthens
        sig = ref * contrast_of(x) + rng.gauss(0, 85)
        rows.append({"i": i, x_key: x, "counter.signal": round(sig), "counter.reference": round(ref),
                     "counter.contrast": round(sig) / round(ref)})
    if zero_first:   # the first acquisition after the generator starts: nothing in the windows yet
        rows[0].update({"counter.signal": 0, "counter.reference": 0, "counter.contrast": 1.0075})
    return rows


COUNTER = [{"key": "counter.signal", "unit": "counts", "kind": "scalar"},
           {"key": "counter.reference", "unit": "counts", "kind": "scalar"},
           {"key": "counter.contrast", "unit": "", "kind": "scalar"}]


def test_pulsed_odmr_leads_with_its_contrast_and_leaves_out_a_first_point_of_zero_counts():
    rows = pulsed_rows("pulsed_odmr", "mw.frequency", np.linspace(2.855e9, 2.885e9, 61),
                       lambda f: 1 - 0.1 / (1 + ((f - 2.87e9) / 5e6) ** 2))
    a = analyze_run(rows, [{"key": "mw.frequency", "unit": "Hz", "kind": "instrument"}], COUNTER,
                    {"status": "done", "points": 61, "protocol": "pulsed_odmr"})
    assert a["assessment"].startswith("counter.contrast has a dip at mw.frequency = 2.870")
    assert "The first point reads 0 counts on counter.signal" in a["assessment"]
    assert "counter may not have settled" in a["issues"][0]
    reference = next(s for s in a["series"] if s["y"] == "counter.reference")
    assert reference["kind"] not in ("relaxation", "oscillation") and "τ" not in reference["summary"]
    assert a["next"].startswith("Set the microwave to a line's center (2.870")


@pytest.mark.parametrize("protocol, detuning", [("rabi", 5e6), ("ramsey", 2e6)])
def test_rabi_and_ramsey_are_read_as_their_protocol_says(protocol, detuning):
    taus = np.linspace(0, 1.5e-6 if protocol == "ramsey" else 4e-7, 61)
    decay = 1e-6 if protocol == "ramsey" else 1.5e-6
    rows = pulsed_rows(protocol, "pg.tau", taus,
                       lambda t: 1 - 0.05 * (1 - math.cos(2 * math.pi * detuning * t) * math.exp(-t / decay)))
    a = analyze_run(rows, [{"key": "pg.tau", "unit": "s", "kind": "instrument"}], COUNTER,
                    {"status": "done", "points": 61, "protocol": protocol})
    contrast = next(s for s in a["series"] if s["y"] == "counter.contrast")
    assert contrast["kind"] == "oscillation" and a["assessment"].startswith("counter.contrast oscillates")
    assert contrast["params"]["frequency"][0] == pytest.approx(detuning, rel=0.05)
    assert "Widen" not in a["next"] and "edge of the sweep" not in a["assessment"]
    pi_time = any("π" in r["name"] for r in contrast["results"])
    assert pi_time == (protocol == "rabi") and ("π pulse" in a["next"]) == (protocol == "rabi")


def test_a_decay_along_frequency_is_never_told_in_time_words():
    rng = np.random.default_rng(6)
    f = np.linspace(2.80e9, 2.90e9, 101)
    s = analyze_curve(f.tolist(), (100 + 50 * np.exp(-(f - f[0]) / 20e6) + rng.normal(0, 0.5, f.size)).tolist(),
                      "mw.frequency", "pd.V", "Hz", "V")
    assert s["kind"] == "relaxation" and "falls exponentially with mw.frequency" in s["summary"]
    assert "relaxes" not in s["summary"] and not any("time" in r["name"] for r in s["results"])


RAMSEY = """
plan: ramsey
setup: { laser.emission: on, laser.shutter: open, laser.power: 2, mw.output: on, mw.power: 0, mw.frequency: 2.866e9,
         pg.protocol: ramsey, pg.running: on }
sweep: { pg.tau: {from: 0, to: 1.5e-6, points: 61} }
measure: [counter.signal, counter.reference, counter.contrast]
after: { pg.running: off }
"""


def test_the_engine_says_what_a_pulse_generator_played_and_what_guarded_the_run():
    from labharness.plans import load_plan
    lab = build_virtual_lab(clock=SimClock(), seed=3)
    engine = RunEngine(SafetyGate(lab, ScriptedApprover([True] * 5)))
    r = engine.run(RAMSEY)
    assert r.status == "done", r.message
    assert r.analysis["next"].startswith("Its frequency is the detuning from resonance")
    plan = load_plan(RAMSEY)
    played = engine.measured_with(plan, {"pg": {"running": True, "protocol": "ramsey", "sequence": "none"}})
    assert played == {"protocol": "ramsey", "guards": ["tc.T_sample"]}   # the laser's guard: the sample's temperature
    written = engine.measured_with(plan, {"pg": {"running": True, "protocol": "ramsey", "sequence": "xy8"}})
    assert "protocol" not in written                                      # a written sequence: Kelvin can't tell

# ------------------------------------------------------------------ magnetoresistance (t02)


def test_a_parabola_rises_as_b_squared_and_is_never_a_dip_wider_than_the_sweep():
    rng = np.random.default_rng(7)
    b = np.linspace(-2.5, 2.5, 101)
    s = analyze_curve(b.tolist(), (14.5e-6 * (1 + 0.2 * b * b) + rng.normal(0, 2e-9, b.size)).tolist(),
                      "magnet.B", "lockin.X", "T", "V")
    assert s["kind"] == "quadratic" and s["summary"].startswith("lockin.X rises as magnet.B² from 14.5")
    assert "+125%" in s["summary"] and "FWHM" not in s["summary"] and "dip" not in s["summary"]


def test_a_gate_maps_magnetoresistance_is_told_along_each_axis_not_as_a_spot():
    rows = []
    for b in np.linspace(-1, 1, 11):
        for v in np.linspace(0, 3, 31):
            r = 1e3 + 9e3 / (1 + ((v - 1.5) / 0.8) ** 2) * (1 + 0.3 * b * b)
            rows.append({"magnet.B": b, "smu.V": v, "lockin.X": 5e-9 * r})
    a = analyze_run(rows, [{"key": "magnet.B", "unit": "T", "kind": "instrument"},
                           {"key": "smu.V", "unit": "V", "kind": "instrument"}],
                    [{"key": "lockin.X", "unit": "V", "kind": "scalar"}], {"status": "done", "points": len(rows)})
    (s,) = a["series"]
    assert s["kind"] == "structured map" and "2-D Gaussian" not in s["summary"]
    assert "along smu.V" in s["summary"] and "has a peak at smu.V = 1.500" in s["summary"]
    assert "along magnet.B" in s["summary"] and "rises as magnet.B²" in s["summary"]

# ------------------------------------------------------------------ fits that found nothing, and failed fits (t03, t04)


def test_a_fit_of_noise_says_it_found_nothing():
    t = np.linspace(0, 4e-7, 41)
    r = fitting.fit(fitting.MODELS["damped_cosine"](), t, 1 + np.random.default_rng(3).normal(0, 0.01, t.size),
                    x_unit="s")
    text = "\n".join(fitting.fit_text(r))
    assert r["p_flat"] > fitting.SIGNIFICANT and "not significant: a flat line fits these points about as well" in text
    x = np.linspace(-1, 1, 81)
    real = fitting.fit(fitting.MODELS["lorentzian"](), x, 1 / (1 + (x / 0.1) ** 2) +
                       np.random.default_rng(3).normal(0, 0.02, x.size))
    assert real["p_flat"] < 1e-10 and not any(w.startswith("not significant") for w in real["warnings"])
    assert real["failed"] is None


def test_a_failed_fit_says_so_and_is_kept_only_when_asked_marked_failed(rt):
    assert "done" in rt.run("plan: vna\nsetup: {vna.rf: on, vna.power: -20, vna.f_start: 3.9985e9, "
                            "vna.f_stop: 4.0015e9, vna.points: 601}\nmeasure: [vna.S21]")
    run_id = rt.pick_runs(last=1)[-1]
    _, text = datatools.fit_run(rt, run_id, "resonator", y="vna.S21")   # a notch model on a transmission peak
    assert text.splitlines()[1].startswith("FAILED: ") and "not kept: the fit failed" in text
    assert "this trace rises at its resonance, a transmission peak: fit lorentzian" in text
    assert fitting.stored(rt._run_dir(run_id)) == []
    _, text = datatools.fit_run(rt, run_id, "resonator", y="vna.S21", name="notch", keep=True)
    (kept,) = fitting.stored(rt._run_dir(run_id), with_data=True)
    assert kept["failed"] and "kept in" in text
    from labharness.reports.collect import Collector
    chart, table = Collector._kept_fit(kept, fitting)
    assert chart.title.endswith("(failed)") and chart.caption.startswith("Failed (") and table.rows[0][0] == "failed"
    _, text = datatools.plot_runs(rt, [{"run": run_id, "y": "vna.S21", "fit": "notch"}])
    assert "failed" in text


def test_a_decay_time_the_data_cant_fix_fails_the_fit():
    x = np.linspace(0, 1, 30)
    r = fitting.fit(fitting.MODELS["exponential"](), x, 2 + np.random.default_rng(1).normal(0, 1, x.size))
    assert r["failed"] and "FAILED: " + r["failed"] in fitting.fit_text(r)[0]


def test_a_hall_density_only_from_an_r_h_that_stands_out_in_ohms():
    hall = fitting.MODELS["hall"]()
    b = np.linspace(-1, 1, 41)
    noise = np.random.default_rng(2).normal(0, 0.5, b.size)
    r = fitting.fit(hall, b, 5 + noise, x_unit="T", y_unit="Ω")
    assert not any(d["name"].startswith("carrier") for d in r["derived"])
    assert any("within 3σ of 0" in w for w in r["warnings"])
    r = fitting.fit(hall, b, 1e-5 * b + noise * 1e-8, x_unit="T", y_unit="V")
    assert not r["derived"] and any("not ohms" in w for w in r["warnings"])
    ohms = fitting.fit(hall, b, 5 - b / (fitting.QE * 2e16) + noise, x_unit="T", y_unit="Ω")
    kilo = fitting.fit(hall, b * 1e3, (5 - b / (fitting.QE * 2e16) + noise) / 1e3, x_unit="mT", y_unit="kΩ")
    density = lambda r: next(d["value"] for d in r["derived"] if d["name"].startswith("carrier density"))  # noqa
    assert density(ohms) == pytest.approx(2e12, rel=0.01) and density(kilo) == pytest.approx(density(ohms), rel=1e-6)

# ------------------------------------------------------------------ smoothing at a sweep's ends (t03)


def test_smooth_averages_over_the_points_there_are_so_at_min_finds_the_dip():
    f = np.linspace(2.85e9, 2.89e9, 41)
    rate = 2.85e5 - 5e3 / (1 + ((f - 2.872e9) / 1e6) ** 2)
    cols = {"counter.rate": rate, "mw.frequency": f}
    found = expr.evaluate("at_min(smooth(counter.rate, 3), mw.frequency)", cols.__getitem__, np)
    assert found == pytest.approx(2.872e9, abs=1e6)        # zero padding made it the sweep's first frequency
    level = expr.evaluate("smooth(counter.rate, 5)", cols.__getitem__, np)
    assert level[0] == pytest.approx(rate[:3].mean()) and level[-1] == pytest.approx(rate[-3:].mean())
    assert expr.evaluate("smooth(counter.rate, 99)", cols.__getitem__, np).shape == rate.shape   # wider than the data
    line = workbench._smooth(np, 3.0 * np.arange(12.0), 7)                                       # the pane's too
    assert line == pytest.approx(3.0 * np.arange(12.0))


def test_a_recipes_kept_frequency_is_the_lines_not_the_sweeps_edge(rt):
    out = rt.run("plan: odmr\nsetup: {laser.emission: on, laser.shutter: open, laser.power: 5, mw.output: on, "
                 "mw.power: -20, counter.integration: 1}\nsweep: {mw.frequency: {from: 2.86e9, to: 2.88e9, "
                 "points: 41}}\nmeasure: [counter.rate]")
    assert "done" in out
    f0 = rt.value_of(rt.pick_runs(last=1)[-1], "at_min(smooth(counter.rate, 3), mw.frequency)")
    assert 2.865e9 < f0 < 2.875e9

# ------------------------------------------------------------------ the measurement chain and the headline (t01, t06)


def test_the_assessment_gives_a_chained_reading_in_ohms(rt):
    rt.measurement_chain("setup", {"excitation": {"source": "lockin.amplitude", "form": "rms", "series_resistor": 1e7},
                                   "readings": {"lockin.X": {"gain": 1, "name": "R_xx"}}})
    out = rt.run("plan: gate\nsetup: {lockin.amplitude: 0.01, smu.output: on}\nsweep: {smu.V: {from: 0, to: 3, "
                 "points: 61}}\nsettle: 0.5\nmeasure: [lockin.X, smu.I]\nafter: {smu.V: 0}")
    assert "assessment: R_xx has a peak at smu.V = 1.50" in out, out
    run_id = rt.pick_runs(last=1)[-1]
    from labharness import rundata
    s = next(s for s in rundata.load(rt._run_dir(run_id), with_rows=False).analysis["series"] if s["y"] == "lockin.X")
    height = next(r for r in s["results"] if r["name"] == "height")
    assert height["unit"] == "Ω" and height["num"] == pytest.approx(9e3 * (1 - 0.0) / (1 + 1.6 / 50), rel=0.05)
    assert max(y for y in s["fit"]["y"] if y is not None) < 1e-3   # drawn over the reading itself, in volts
    assert rt.analyze(run_id).splitlines()[1].startswith("assessment: R_xx has a peak")   # and when re-analysed


def test_a_gate_sweeps_headline_is_the_sample_not_its_leakage():
    lab = build_virtual_lab(clock=SimClock(), seed=3)
    r = RunEngine(SafetyGate(lab, ScriptedApprover([True] * 5))).run(
        "plan: gate\nsetup: {lockin.amplitude: 0.05, smu.output: on}\nsweep: {smu.V: {from: 0, to: 2, points: 41}}\n"
        "settle: 0.5\nmeasure: [lockin.X, smu.I]\nafter: {smu.V: 0}")
    assert r.status == "done", r.message
    head = r.analysis["assessment"]
    assert head.startswith("lockin.X has a peak at smu.V = "), head   # the sample's reading leads: inside the sweep
    assert float(head.split(" = ", 1)[1].split()[0]) == pytest.approx(1.50, abs=0.01)   # whatever the noise's draw
    assert "edge of the sweep; the feature may extend" not in r.analysis["assessment"]

# ------------------------------------------------------------------ the review of 0.3.34 (2026-10-31)


def test_a_line_as_wide_as_the_sweep_gets_no_width_or_q_and_a_narrower_one_its_own():
    rng = np.random.default_rng(12)
    f = np.linspace(4.999e9, 5.001e9, 201)                                       # a 2 MHz window
    for fwhm in (1.4e6, 2e6, 3e6):
        y = 1 - 0.5 / (1 + ((f - 5.0001e9) / (fwhm / 2)) ** 2) + rng.normal(0, 0.002, f.size)
        s = analyze_curve(f.tolist(), y.tolist(), "vna.frequency", "vna.S21", "Hz", "")
        assert s["kind"] == "dip" and s["params"]["center"][0] == pytest.approx(5.0001e9, abs=5e3), s["summary"]
        if fwhm < 2e6:   # its own Q, not a Gaussian's 1.4 times too high
            assert s["params"]["Q"][0] == pytest.approx(5.0001e9 / fwhm, rel=0.02), s["summary"]
        else:
            assert "wider than the sweep: its FWHM and Q need a wider one" in s["summary"], s["summary"]
            assert "Q ≈" not in s["summary"] and set(s["params"]) == {"center"}
            assert any("wider than the 2 MHz sweep: widen the range" in flag for flag in s["flags"])


@pytest.mark.parametrize("tc, width", [(4, 0.05), (5.5, 0.2), (7, 0.5), (10, 1.0)])
def test_a_superconducting_transition_is_a_step_not_a_peak_or_two_dips(tc, width):
    rng = np.random.default_rng(11)
    t = np.linspace(2, 12, 101)
    s = analyze_curve(t.tolist(), (50 * (1 + np.tanh((t - tc) / width)) + rng.normal(0, 0.1, t.size)).tolist(),
                      "temp.T", "R", "K", "Ω")
    assert s["kind"] == "step" and s["summary"].startswith("R steps up from "), s["summary"]
    center, error, _ = s["params"]["center"]
    assert abs(center - tc) < 4 * error and s["params"]["change"][0] == pytest.approx(100, rel=0.01)
    assert s["params"]["rise"][0] == pytest.approx(2 * math.atanh(0.8) * width, rel=0.05)   # 10 % to 90 %


def test_a_switch_sharper_than_the_points_is_placed_between_two_of_them_and_steps_are_never_a_peak():
    rng = np.random.default_rng(3)
    b = np.linspace(-2, 2, 81)
    s = analyze_curve(b.tolist(), (np.where(b > 0.3, 1.0, 0.0) + rng.normal(0, 0.01, b.size)).tolist(),
                      "magnet.B", "V", "T", "V")
    assert s["kind"] == "step" and "sharper than the 50 mT step between points" in s["summary"], s["summary"]
    center, error, _ = s["params"]["center"]
    assert error == pytest.approx(0.025) and abs(center - 0.275) <= error   # between 250 and 300 mT
    assert "rise" not in s["params"] and "use a step ≤ 12.5 mT" in s["flags"][0]
    two = np.where(b > 0.5, 1.0, 0.0) + np.where(b > -0.5, 1.0, 0.0) + rng.normal(0, 0.01, b.size)
    stairs = 25812.8 / np.clip(np.floor(np.abs(b) * 3 + 1), 1, None) + rng.normal(0, 5, b.size)   # quantum Hall
    for y in (two, stairs):
        s = analyze_curve(b.tolist(), y.tolist(), "magnet.B", "R_xy", "T", "Ω")
        assert s["kind"] not in ("peak", "dip"), s["summary"]


def test_a_dip_with_a_shoulder_is_not_given_one_lines_q():
    rng = np.random.default_rng(9)
    f = np.linspace(4.99e9, 5.01e9, 401)
    q = 2 * (f - 5e9) / 1e6
    y = 1 - 0.6 / (1 + q ** 2) - 0.25 / (1 + ((q - 3) / 1.5) ** 2) + rng.normal(0, 0.005, f.size)
    s = analyze_curve(f.tolist(), y.tolist(), "vna.frequency", "vna.S21", "Hz", "")
    assert s.get("lines") == 2 or "Q ≈" not in s["summary"], s["summary"]   # one Lorentzian: half the main dip's Q
