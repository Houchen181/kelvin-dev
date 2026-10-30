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
