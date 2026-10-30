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
