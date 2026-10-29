"""Automatic analysis: curve classification, fits with uncertainties, flags, and the run-level assessment."""
import math
import random

import pytest

from labharness.analysis import _fmt_pm, analyze_curve, analyze_run, hysteresis, noise_estimate

pytest.importorskip("numpy")   # the analysis is NumPy's; without it a run says "not analysed"


def lorentz(x, c, w, a, b):
    return b + a / (1 + ((x - c) / (w / 2)) ** 2)


def test_numbers_carry_their_uncertainty():
    assert _fmt_pm(1.50234, 0.0041, "V") == "1.5023 ± 0.0041 V"
    assert _fmt_pm(43.6e-6, 0.0027e-6, "V") == "43.6000 ± 0.0027 µV"
    assert _fmt_pm(1.01e-12, 0.018e-12, "") == "(1.010 ± 0.018)e-12"   # no SI prefix for a bare number
    assert _fmt_pm(2.5, None, "K") == "2.5 K"


def test_noise_estimate_ignores_smooth_trends():
    rng = random.Random(1)
    ys = [0.3 * i + rng.gauss(0, 0.01) for i in range(400)]
    assert 0.008 < noise_estimate(ys) < 0.012


def test_peak_center_width_and_height():
    rng = random.Random(2)
    xs = [i * 0.1 for i in range(51)]
    ys = [lorentz(x, 1.5, 1.6, 40e-6, 5e-6) + rng.gauss(0, 0.2e-6) for x in xs]
    s = analyze_curve(xs, ys, "smu.V", "lockin.X", "V", "V")
    assert s["kind"] == "peak" and s["fit"]["model"] == "lorentzian" and not s["flags"]
    c, ec, _ = s["params"]["center"]
    assert abs(c - 1.5) < max(4 * ec, 1e-3)
    assert abs(s["params"]["FWHM"][0] - 1.6) < 0.05
    assert abs(s["params"]["height"][0] - 40e-6) < 1e-6
    assert s["summary"].startswith("lockin.X has a peak at smu.V = 1.50")
    assert len(s["fit"]["x"]) == len(s["fit"]["y"]) > 50   # a smooth curve for the plot


def test_dip_and_resonator_quality_factor():
    """A resonance is a Lorentzian in linear power, which is how it is fitted when it was recorded in dB: the same
    dip fitted as a Lorentzian on its dB values gave a Q 2.4 times too high (field test l04)."""
    rng = random.Random(3)
    xs = [3.99e9 + 5e4 * i for i in range(401)]
    power = [1e-3 * lorentz(f, 4.0e9, 2e6, -0.99, 1.0) for f in xs]          # a 20 dB deep notch, 2 MHz wide
    ys = [10 * math.log10(p) + rng.gauss(0, 0.05) for p in power]
    s = analyze_curve(xs, ys, "frequency", "vna.S21", "Hz", "dB")
    assert s["kind"] == "dip"
    q = next(r for r in s["results"] if r["name"].startswith("Q"))
    assert 1900 < q["num"] < 2100 and "linear power" in q["name"]
    assert "Q ≈" in s["summary"] and "FWHM of the linear power" in s["summary"]
    depth = next(r for r in s["results"] if r["name"] == "depth")
    assert depth["unit"] == "dB" and 19 < depth["num"] < 21
    assert min(y for y in s["fit"]["y"] if y is not None) < -45                 # the fit drawn in dB, as recorded


def test_under_resolved_and_edge_features_are_flagged():
    xs = [i * 0.5 for i in range(11)]
    ys = [lorentz(x, 2.6, 0.9, 1.0, 0.0) for x in xs]
    s = analyze_curve(xs, ys, "smu.V", "lockin.X", "V", "V")
    assert any("under-resolved" in f for f in s["flags"]), s
    xs = [i * 0.05 for i in range(41)]
    ys = [lorentz(x, 1.9, 0.8, 1.0, 0.0) for x in xs]
    s = analyze_curve(xs, ys, "smu.V", "lockin.X", "V", "V")
    assert any("edge of the sweep" in f for f in s["flags"]), s


def test_noisy_iv_is_linear_and_gives_resistance():
    rng = random.Random(4)
    xs = [-2 + 0.2 * i for i in range(21)] * 3
    ys = [v / 1e12 + rng.gauss(0, 2e-13) for v in xs]
    s = analyze_curve(xs, ys, "smu.V", "smu.I", "V", "A")
    assert s["kind"] == "linear"
    r = next(r["value"] for r in s["results"] if r["name"].startswith("resistance"))
    assert r.endswith("GΩ") and 900 < float(r.split(" ± ")[0]) < 1100
    assert "R =" in s["summary"]


def test_relaxation_time_constant():
    rng = random.Random(5)
    xs = [5.0 * i for i in range(61)]
    ys = [1.6 + 2.0 * math.exp(-t / 30.0) + rng.gauss(0, 0.002) for t in xs]
    s = analyze_curve(xs, ys, "time", "tc.T_sample", "s", "K")
    assert s["kind"] == "relaxation"
    tau, etau, _ = s["params"]["τ"]
    assert abs(tau - 30) < max(4 * etau, 0.5)


def test_noise_is_flat_and_a_wandering_reading_is_steady():
    rng = random.Random(6)
    s = analyze_curve(list(range(50)), [rng.gauss(0, 1e-9) for _ in range(50)], "smu.V", "lockin.X", "V", "V")
    assert s["kind"] == "flat" and "no signal" in s["summary"]
    t, walk = 1.6, []
    for _ in range(31):  # slow, correlated fluctuations around a constant (a temperature log)
        t = 1.6 + 0.7 * (t - 1.6) + rng.gauss(0, 0.002)
        walk.append(t)
    s = analyze_curve([10.0 * i for i in range(31)], walk, "time", "tc.T_sample", "s", "K")
    assert s["kind"] == "steady" and s["summary"].startswith("tc.T_sample is stable over 300 s: 1.60")


def test_clipped_signal_is_flagged():
    xs = [i * 0.1 for i in range(51)]
    ys = [min(lorentz(x, 2.5, 1.0, 1.0, 0.0), 0.6) for x in xs]
    s = analyze_curve(xs, ys, "smu.V", "lockin.X", "V", "V")
    assert any("clipped" in f for f in s["flags"]), s


def test_hysteresis_between_up_and_down_sweeps():
    up = [i * 0.1 for i in range(11)]
    xs = up + up[-2::-1]
    ys = [x for x in up] + [x + 0.3 for x in up[-2::-1]]
    assert "hysteresis" in hysteresis(xs, ys, "T", "V")
    assert hysteresis(xs, [x for x in xs], "T", "V") is None


AXES = [{"key": "magnet.B", "unit": "T", "kind": "instrument"}, {"key": "smu.V", "unit": "V", "kind": "instrument"}]
MEASURES = [{"key": "lockin.X", "unit": "V", "kind": "scalar"}]


def nested_rows(height_per_tesla):
    rng = random.Random(7)
    rows = []
    for b in (0.0, 0.5, 1.0):
        for i in range(41):
            v = 0.1 * i
            rows.append({"magnet.B": b, "smu.V": v,
                         "lockin.X": lorentz(v, 1.5, 1.6, 40e-6 * (1 + height_per_tesla * b * b), 5e-6) + rng.gauss(0, 0.1e-6)})
    return rows


def test_run_assessment_reports_how_the_feature_changes_with_the_outer_loop():
    a = analyze_run(nested_rows(0.3), AXES, MEASURES, {"status": "done", "points": 123})
    assert [s["group"] for s in a["series"]] == ["magnet.B = 0 T", "magnet.B = 500 mT", "magnet.B = 1 T"]
    assert a["trends"] and "height rises" in a["trends"][0] and "center stays put" in a["trends"][0]
    assert "peak" in a["assessment"] and "height rises" in a["assessment"]
    assert a["next"].startswith("Add more magnet.B values")
    a = analyze_run(nested_rows(0.0), AXES, MEASURES, {"status": "done", "points": 123})
    assert "does not change" in a["trends"][0] and a["next"].startswith("Nothing changes with magnet.B")


def test_aborted_run_leads_the_issues_and_the_next_step():
    rows = nested_rows(0.0)[:30]
    a = analyze_run(rows, AXES, MEASURES, {"status": "aborted", "points": 30, "planned_points": 123,
                                           "reason": "guard tripped: smu.I"})
    assert a["issues"][0] == "run stopped after 30 of 123 points: guard tripped: smu.I"
    assert "Run stopped after 30 of 123 points" in a["assessment"]
