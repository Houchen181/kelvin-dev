"""A field report (2026-11-17), item 6: fit_run's double_lorentzian on an imported ODMR run, y3 against x, a doublet of
two dips of about 8 % each 6 MHz apart, gave R² 0.846 with its second line fitted as a peak; with A1 and A2 bounded to
≤ 0 it gave R² 0.907, both dips and flat residuals.

The second line started where one Lorentzian of the largest feature's width left most behind, whatever its sign. For
a doublet whose half maximum spans both dips, that width took in both: the one line overshot beside them and the
"second line" came out a peak, from where the fit found a broad dip with a peak in it. Both lines now start with the
largest feature's sign, at two features of that sign where the curve shows two, else at the one feature split in two,
neither line wider than twice the gap between them; a short fit from each such start keeps the one that leaves least
behind (fitting._doublet_guess). A single line fitted with two no longer gets a large second line of the other sign
(one on a noisy point once stood several times the line's height against it)."""
import math

import pytest

np = pytest.importorskip("numpy")

from labharness import fitting  # noqa: E402

MODEL = fitting.MODELS["double_lorentzian"]()
DIPS = {"A1": [None, 0], "A2": [None, 0]}
PEAKS = {"A1": [0, None], "A2": [0, None]}


def lorentz(x, c, w):
    return 1.0 / (1.0 + ((x - c) / (w / 2)) ** 2)


def curve(lines, seed=2, n=121, span=(2.84e9, 2.90e9), r2=0.9, level=1.0):
    """Lorentzian lines (center, FWHM, amplitude as a fraction of the level) on a level, with Gaussian noise that
    leaves the true curve an R² of about r2."""
    x = np.linspace(span[0], span[1], n)
    clean = level + sum(a * level * lorentz(x, c, w) for c, w, a in lines)
    noise = float(np.std(clean)) * math.sqrt((1 - r2) / r2)
    return x, clean + np.random.default_rng(seed).normal(0, noise, n)


REPORT = ((2.867e9, 5e6, -0.08), (2.873e9, 5e6, -0.08))   # two 8 % dips 6 MHz apart, each 5 MHz wide


def lines_of(result):
    """A fit's two lines as (center, FWHM, amplitude), by center."""
    v = {p["name"]: p["value"] for p in result["params"]}
    return sorted([(v["x1"], v["w1"], v["A1"]), (v["x2"], v["w2"], v["A2"])])


def from_truth(x, y, lines, level=1.0):
    """The fit started at the lines the curve was made of: the minimum a start should find."""
    (c1, w1, a1), (c2, w2, a2) = lines
    return fitting.fit(MODEL, x, y, start={"y0": level, "A1": a1 * level, "x1": c1, "w1": w1, "A2": a2 * level,
                                           "x2": c2, "w2": w2})


def assert_found(result, lines, x, y, at=0.5e6):
    """Both lines where they are, each with its own sign, and the fit as good as the one started at the truth."""
    found = lines_of(result)
    for (c, w, a), (fc, fw, fa) in zip(sorted(lines), found):
        assert fc == pytest.approx(c, abs=at) and fa * a > 0, (found, lines)
    assert result["r2"] >= from_truth(x, y, lines)["r2"] - 1e-9
    assert not result["failed"], result["failed"]


def test_the_reports_doublet_of_dips_is_two_dips_without_bounds():
    x, y = curve(REPORT)
    # the start it had: the largest dip's line, as wide as both dips together (10 MHz, wider than the gap between
    # them; each is 5 MHz), and the second where that line leaves most behind: a peak beside its overshoot. From there
    # the fit ends at a broad dip with a peak in it
    b, a, c, w, _ = fitting._peak(np, x, y)
    _, a2, c2, w2, _ = fitting._peak(np, x, y - a * fitting._lorentz(x, c, w))
    assert a < 0 < a2 and w > abs(REPORT[1][0] - REPORT[0][0])
    old = fitting.fit(MODEL, x, y, start={"y0": b, "A1": a, "x1": c, "w1": w, "A2": a2, "x2": c2, "w2": w2})
    bounded = fitting.fit(MODEL, x, y, bounds=DIPS, x_unit="Hz")
    assert old["r2"] == pytest.approx(0.851, abs=0.005) and max(line[2] for line in lines_of(old)) > 0
    assert bounded["r2"] == pytest.approx(0.907, abs=0.005) and max(line[2] for line in lines_of(bounded)) < 0

    g = MODEL.guess(x, y)   # now: two dips, neither taking in the other
    assert g["A1"] < 0 and g["A2"] < 0
    assert max(g["w1"], g["w2"]) < 2 * abs(g["x1"] - g["x2"])
    free = fitting.fit(MODEL, x, y, x_unit="Hz")
    assert_found(free, REPORT, x, y)
    assert free["r2"] >= bounded["r2"] - 1e-9 and free["r2"] == pytest.approx(0.907, abs=0.005)
    assert all(-0.1 < a < -0.06 for _, _, a in lines_of(free))
    assert lines_of(fitting.fit(MODEL, x, y, x_unit="Hz")) == lines_of(free)   # the same every time


def test_the_reports_doublet_is_two_dips_whatever_the_noise():
    """The start from one line's width went wrong for about one noise draw in eight of the report's doublet."""
    for seed in range(30):
        x, y = curve(REPORT, seed=seed)
        free = fitting.fit(MODEL, x, y)
        assert_found(free, REPORT, x, y, at=1e6)
        assert free["r2"] >= fitting.fit(MODEL, x, y, bounds=DIPS)["r2"] - 1e-9, seed


def test_a_doublet_of_peaks():
    """The report's doublet upside down, noise and all: two peaks, found as the two dips are."""
    lines = tuple((c, w, -a) for c, w, a in REPORT)
    for seed in range(10):
        x, y = curve(REPORT, seed=seed)
        y = 2.0 - y
        free = fitting.fit(MODEL, x, y)
        assert_found(free, lines, x, y, at=1e6)
        assert free["r2"] >= fitting.fit(MODEL, x, y, bounds=PEAKS)["r2"] - 1e-9, seed


@pytest.mark.parametrize("gap", [30e6, 12e6, 8e6, 4e6])
def test_well_separated_and_overlapping_doublets(gap):
    """Lines 5 MHz wide, 30 MHz apart (each on its own), 12 and 8 MHz (a valley between them) and 4 MHz (one broad
    dip with a flat bottom)."""
    lines = ((2.87e9 - gap / 2, 5e6, -0.08), (2.87e9 + gap / 2, 5e6, -0.08))
    for seed in range(3):
        x, y = curve(lines, seed=seed, n=201, span=(2.82e9, 2.92e9), r2=0.95)
        assert_found(fitting.fit(MODEL, x, y), lines, x, y, at=0.25 * gap)


@pytest.mark.parametrize("lines", [
    ((2.866e9, 5e6, -0.10), (2.873e9, 5e6, -0.04)),     # the weaker a shoulder of the stronger
    ((2.86e9, 4e6, -0.04), (2.88e9, 7e6, -0.10)),       # apart, the weaker and narrower first
    ((2.868e9, 8e6, -0.09), (2.872e9, 3e6, -0.05)),     # a narrow dip in a broad one
], ids=["shoulder", "apart", "narrow-in-broad"])
def test_doublets_of_unequal_depths(lines):
    for seed in range(3):
        x, y = curve(lines, seed=seed, n=201, span=(2.83e9, 2.91e9), r2=0.97)
        assert_found(fitting.fit(MODEL, x, y), lines, x, y, at=1.5e6)


def test_the_scale_of_x_does_not_matter():
    """The same doublet with x in Hz, MHz and GHz, and on a level of counts: the same lines."""
    x, y = curve(REPORT, seed=4)
    hz = lines_of(fitting.fit(MODEL, x, y))
    for unit in (1e6, 1e9):
        found = lines_of(fitting.fit(MODEL, x / unit, y))
        assert [c * unit for c, _, _ in found] == pytest.approx([c for c, _, _ in hz], abs=1e3)
    counts = lines_of(fitting.fit(MODEL, x, 2.4e5 * y))
    assert [a / 2.4e5 for _, _, a in counts] == pytest.approx([a for _, _, a in hz], rel=1e-3)


@pytest.mark.parametrize("sign", [-1, 1], ids=["dip", "peak"])
def test_a_single_line_gets_no_large_second_line_of_the_other_sign(sign):
    """One line fitted with two: the second may share it or sit on the noise, never stand against it."""
    for seed in range(8):
        for width, r2 in ((5e6, 0.95), (12e6, 0.7), (20e6, 0.5)):
            x, y = curve(((2.87e9, width, sign * 0.08),), seed=seed, n=121, r2=r2)
            if seed == 0:   # a noisy point on the other side, larger than the line
                y[30] += -sign * 0.15
            result = fitting.fit(MODEL, x, y)
            one = fitting.fit(fitting.MODELS["lorentzian"](), x, y)
            assert result["r2"] >= one["r2"] - 1e-6
            for c, w, a in lines_of(result):
                seen = abs(a) * float(lorentz(x, c, w).max())   # its largest at the points
                assert a * sign > 0 or seen < 0.02, (seed, width, r2, lines_of(result))


def test_fit_run_on_an_imported_odmr_run(tmp_path):
    """The report's own path: an ODMR file imported as a run, fit_run's double_lorentzian on y3 against x."""
    pytest.importorskip("h5py")
    from labharness import ScriptedApprover, datatools
    from labharness.agent.runtime import LabRuntime
    from simlab import build_virtual_lab
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    x, y = curve(REPORT)
    rows = ["%.6f %.6f %.6f %.6f" % (f, 1.0, 1.0, v) for f, v in zip(x, y)]
    (tmp_path / "odmr.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert rt.import_data(str(tmp_path / "odmr.txt")).startswith("imported odmr.txt as run ")
    run_id = rt.runs_index(1)[0]["run_id"]
    _, text = datatools.fit_run(rt, run_id, "double_lorentzian", x="x", y="odmr.y3", name="ODMR doublet")
    assert "double_lorentzian fit" in text and "R² 0.907" in text, text
    (kept,) = fitting.stored(rt._run_dir(run_id))
    v = dict(zip(kept["params"], kept["values"]))
    assert v["A1"] < 0 and v["A2"] < 0 and not kept.get("failed")
    assert sorted([v["x1"], v["x2"]]) == pytest.approx([2.867e9, 2.873e9], abs=0.5e6)


def test_the_automatic_analysis_starts_a_doublets_second_line_with_the_firsts_sign():
    """The analysis (analysis.py) had its own two-line starts with the same flaw, its second line where one line left
    most behind of either sign; it keeps two lines only of one kind, so a start of the other sign was a start wasted.
    Asked for two lines on the report's doublet, both come out dips."""
    from labharness import analysis
    x, y = curve(REPORT)
    out = analysis.analyze_curve(x, y, "f", "y3", "Hz", "", force="double_lorentzian")
    assert (out["kind"], out["lines"]) == ("dip", 2) and out["fit"]["r2"] > 0.9, out["summary"]   # was 0.851
    fitted = out["fit"]["y"]   # two dips: the curve never above its ends (HEAD's had a peak, and called it a dip)
    assert max(fitted) <= max(fitted[0], fitted[-1]) + 1e-9
    said = {r["name"]: r["value"] for r in out["results"]}   # "2.86694 ± 0.00021 GHz"
    centers = [float(said["center %d" % k].split()[0]) for k in (1, 2)]
    assert centers == [pytest.approx(2.867, abs=6e-4), pytest.approx(2.873, abs=6e-4)], said
