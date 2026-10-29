"""Honest automatic assessments (field-test plan item 22): no fits of a set point's read-back against it, a sweep over
two axes looked at as a map, peaks that must stand out from noise reaching somewhere along the sweep, loud failure on
empty data, and an analysis that is fast and stops in time."""
import math
import random
import time

import pytest

np = pytest.importorskip("numpy")

from labharness import ScriptedApprover, build_virtual_lab  # noqa: E402
from labharness.analysis import analyze_curve, analyze_run  # noqa: E402

B = {"key": "magnet.B", "unit": "T", "kind": "instrument"}
X = {"key": "stage.x", "unit": "mm", "kind": "instrument"}
Y = {"key": "stage.y", "unit": "mm", "kind": "instrument"}


def test_a_read_back_of_the_swept_set_point_is_checked_not_fitted():
    rows = [{"magnet.B": b, "magnet.B_actual": b + 1e-4 * math.sin(7 * b), "lockin.X": 1e-6 * (1 + 0.3 * b * b)}
            for b in np.linspace(0, 2, 41)]
    a = analyze_run(rows, [B], [{"key": "magnet.B_actual", "unit": "T", "kind": "scalar"},
                                {"key": "lockin.X", "unit": "V", "kind": "scalar"}], {"status": "done", "points": 41})
    rb = next(s for s in a["series"] if s["y"] == "magnet.B_actual")
    assert rb["kind"] == "tracking" and rb["fit"] is None and "is the read-back of magnet.B" in rb["summary"]
    assert "magnet.B_actual is linear" not in a["assessment"] and a["assessment"].startswith("lockin.X")
    # the swept axis read back as itself ("x linear in x")
    same = analyze_run([{"stage.x": v, "stage.x2": 0} for v in range(20)], [X],
                       [{"key": "stage.x", "unit": "mm", "kind": "scalar"}], {"status": "done", "points": 20})
    assert same["series"][0]["kind"] == "tracking" and "linear" not in same["assessment"]


def test_a_set_point_the_readback_never_followed_is_an_issue():
    rows = [{"magnet.B": b, "magnet.B_actual": 0.0012 + 1e-5 * (i % 3)} for i, b in enumerate(np.linspace(0, 2, 21))]
    a = analyze_run(rows, [B], [{"key": "magnet.B_actual", "unit": "T", "kind": "scalar"}],
                    {"status": "done", "points": 21}, readbacks={"magnet.B": "magnet.B_actual"})
    assert any("did not reach it" in i for i in a["issues"]) and "no problems found" not in a["assessment"]
    assert "set point was not reached" in a["next"]


def test_the_virtual_magnets_read_back_in_a_real_run(tmp_path):
    from labharness.agent.runtime import LabRuntime
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 50), tmp_path / "data")
    out = rt.run("plan: field\nsweep: {magnet.B: {from: 0, to: 0.5, points: 11}}\nmeasure: [magnet.B_actual]")
    assert "done" in out and "linear in magnet.B" not in out and "read-back of magnet.B" in out


def spot_map(rng, amp, n=31, noise=1.0):
    rows = []
    for i, y in enumerate(np.linspace(0, 3, n)):
        for j, x in enumerate(np.linspace(0, 3, n)):
            z = 100 + amp * math.exp(-4 * math.log(2) * ((x - 1.7) ** 2 + (y - 1.1) ** 2) / 0.4 ** 2)
            rows.append({"stage.y": y, "stage.x": x, "counter.rate": z + rng.gauss(0, noise)})
    return rows


def test_a_two_axis_sweep_is_a_map_with_its_spot_fitted():
    rows = spot_map(random.Random(1), 40)
    a = analyze_run(rows, [Y, X], [{"key": "counter.rate", "unit": "counts/s", "kind": "scalar"}],
                    {"status": "done", "points": len(rows)})
    (s,) = a["series"]
    assert s["kind"] == "spot" and not s.get("group")      # one map, not 31 rows judged one by one
    c = {r["name"]: r for r in s["results"]}
    assert c["center stage.x"]["num"] == pytest.approx(1.7, abs=0.03)
    assert c["center stage.y"]["num"] == pytest.approx(1.1, abs=0.03)
    assert c["FWHM along stage.x"]["num"] == pytest.approx(0.4, abs=0.06)
    assert a["assessment"].startswith("counter.rate has a spot at stage.x = 1.69")


def test_noise_maps_show_no_spot_whatever_the_seed():
    """Look-elsewhere: 961 points of pure noise always hold an excursion of ~3σ somewhere; it is not a feature."""
    for seed in range(8):
        rows = spot_map(random.Random(seed), 0.0)
        a = analyze_run(rows, [Y, X], [{"key": "counter.rate", "unit": "counts/s", "kind": "scalar"}],
                        {"status": "done", "points": len(rows)})
        (s,) = a["series"]
        assert s["kind"] == "flat", (seed, s["summary"])
        assert "no measured column shows a signal" in a["assessment"].lower()


def test_a_weak_spot_in_noise_is_still_found():
    rows = spot_map(random.Random(3), 8.0)                  # 8σ high, about 3 points wide
    a = analyze_run(rows, [Y, X], [{"key": "counter.rate", "unit": "counts/s", "kind": "scalar"}],
                    {"status": "done", "points": len(rows)})
    assert a["series"][0]["kind"] == "spot"


def test_one_glitch_in_noise_is_not_a_peak():
    rng = random.Random(5)
    for k in (17, 30):
        xs = [0.1 * i for i in range(51)]
        ys = [rng.gauss(0, 1e-3) for _ in xs]
        ys[k] += 0.03                                          # one point 30σ off: a glitch, not a dip or a peak
        s = analyze_curve(xs, ys, "stage.x", "lockin.X", "mm", "V")
        assert s["kind"] not in ("peak", "dip"), s["summary"]


def test_noise_alone_is_never_called_a_feature():
    hits = 0
    for seed in range(40):
        rng = random.Random(seed)
        xs = [0.05 * i for i in range(101)]
        s = analyze_curve(xs, [rng.gauss(0, 1.0) for _ in xs], "stage.x", "lockin.X", "mm", "V")
        hits += s["kind"] in ("peak", "dip", "oscillation", "relaxation", "linear")
    assert hits == 0


def test_empty_and_signal_free_runs_fail_loudly():
    a = analyze_run([], [B], [{"key": "lockin.X", "unit": "V", "kind": "scalar"}], {"status": "done", "points": 0})
    assert a["assessment"].startswith("No data") and "no problems" not in a["assessment"]
    rows = [{"magnet.B": 0.1 * i, "lockin.X": None} for i in range(10)]
    a = analyze_run(rows, [B], [{"key": "lockin.X", "unit": "V", "kind": "scalar"}], {"status": "done", "points": 10})
    assert "lockin.X: no numbers at any of its 10 points" in a["issues"] and "no problems" not in a["assessment"]
    rng = random.Random(2)
    rows = [{"magnet.B": 0.1 * i, "lockin.X": rng.gauss(0, 1e-9)} for i in range(30)]
    a = analyze_run(rows, [B], [{"key": "lockin.X", "unit": "V", "kind": "scalar"}], {"status": "done", "points": 30})
    assert "no problems" not in a["assessment"] and "no measured column shows a signal" in a["assessment"].lower()


def test_a_long_trace_is_analysed_fast_and_whole():
    """16 001 points (a VNA sweep) took 11 minutes of pure-Python fitting while holding the lab (field test l04)."""
    rng = np.random.default_rng(0)
    f = np.linspace(4.99e9, 5.01e9, 16001)
    power = 1 - 0.95 / (1 + ((f - 5.0013e9) / 0.4e6) ** 2)          # 0.8 MHz wide: 640 of the 16 001 points
    trace = (10 * np.log10(power) + rng.normal(0, 0.05, f.size)).tolist()
    rows = [{"i": 0, "vna.S21": trace, "vna.S21.axis": [4.99e9, 5.01e9]}]
    t0 = time.monotonic()
    a = analyze_run(rows, [], [{"key": "vna.S21", "unit": "dB", "kind": "trace", "x_unit": "Hz"}],
                    {"status": "done", "points": 1})
    assert time.monotonic() - t0 < 10
    s = a["series"][0]
    assert s["kind"] == "dip" and s["params"]["center"][0] == pytest.approx(5.0013e9, abs=2e4)
    q = next(r for r in s["results"] if r["name"].startswith("Q"))
    assert q["num"] == pytest.approx(5.0013e9 / 0.8e6, rel=0.05)


def test_the_analysis_stops_at_its_time_limit():
    rng = random.Random(4)
    rows = [{"smu.V": 0.1 * i, **{"m%d.X" % k: rng.gauss(0, 1) + 0.1 * i for k in range(30)}} for i in range(200)]
    measures = [{"key": "m%d.X" % k, "unit": "V", "kind": "scalar"} for k in range(30)]
    a = analyze_run(rows, [{"key": "smu.V", "unit": "V", "kind": "instrument"}], measures,
                    {"status": "done", "points": 200}, time_limit=0.0)
    assert any(i.startswith("not analysed in the 0 s") for i in a["issues"])
    assert "no problems" not in a["assessment"]
