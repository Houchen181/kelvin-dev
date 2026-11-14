"""The analysis and fitting findings of the 0.3.52 audit (A1 to A9), each checked against an answer worked out apart
from the code under test: the truth the data were made from, the same fit with the bound held, numpy's own
derivative, or the spread of many noisy fits."""
import math
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from labharness import analysis, chain, correlation, datatools, fitting, workbench  # noqa: E402
from labharness.reports.collect import Collector  # noqa: E402

PHI0, QE = 2.067833848e-15, 1.602176634e-19   # the flux quantum (Wb) and e (C), as CODATA gives them


def derived(result, name):
    return next(d for d in result["derived"] if d["name"].startswith(name))


# ------------------------------------------------------------------ A1: the units of the data

def test_hc2_and_graphene_fits_take_the_datas_units():
    """A1: with Bc2 in mT or Oe the coherence length read 405.8 pm and 128.3 pm for 12.83 nm, WHH's Bc2(0) "2.4948
    kT", and graphene's mobility 1e7 with R in kΩ (10 with the gate in mV) for 1e4 cm²/(V·s)."""
    rng = np.random.default_rng(3)
    xi = math.sqrt(PHI0 / (2 * math.pi * 2.0))   # Bc2(0) = 2 T: 12.83 nm
    t = np.linspace(2, 8.5, 14)
    bc2 = 2.0 * (1 - (t / 9.0) ** 2) + rng.normal(0, 0.005, t.size)
    for unit, factor in (("T", 1.0), ("mT", 1e3), ("Oe", 1e4), ("kOe", 10.0)):
        r = fitting.fit(fitting.MODELS["gl_hc2"](), t, bc2 * factor, x_unit="K", y_unit=unit)
        assert derived(r, "GL coherence length")["value"] == pytest.approx(xi, rel=0.01), unit
    whh = fitting.MODELS["whh_hc2"]()
    b = whh.f(t, {"Tc": 9.0, "S": 0.4}) * 1e3 + rng.normal(0, 2, t.size)   # in mT
    r = fitting.fit(whh, t, b, x_unit="K", y_unit="mT")
    b0 = derived(r, "Bc2(0)")
    assert b0["unit"] == "T" and b0["value"] == pytest.approx(0.693 * 9.0 * 0.4, rel=0.01)
    assert derived(r, "GL coherence length")["value"] == pytest.approx(
        math.sqrt(PHI0 / (2 * math.pi * 0.693 * 9.0 * 0.4)), rel=0.01)
    amps = fitting.fit(fitting.MODELS["gl_hc2"](), t, bc2 * 50, x_unit="K", y_unit="A")   # a magnet's current
    assert not amps["derived"] and any("not a magnetic field" in w for w in amps["warnings"])

    vg = np.linspace(-40, 40, 161)
    cg, aspect = 1.21e-4, 2.0
    n = cg * (vg - 2.0) / QE
    ohms = 100 + aspect / (QE * 1.0 * np.sqrt(1e15 ** 2 + n ** 2)) + rng.normal(0, 2, vg.size)   # mu 1 m²/(V·s)
    g = fitting.MODELS["graphene"]()
    for xu, xf, yu, yf in (("V", 1.0, "Ω", 1.0), ("V", 1.0, "kΩ", 1e-3), ("mV", 1e3, "Ω", 1.0), ("mV", 1e3, "kΩ", 1e-3)):
        r = fitting.fit(g, vg * xf, ohms * yf, x_unit=xu, y_unit=yu, fix={"C_g": cg, "aspect": aspect})
        assert not r["failed"], (xu, yu, r["failed"])
        assert derived(r, "mobility")["value"] == pytest.approx(1e4, rel=0.01), (xu, yu)
        assert derived(r, "residual density")["value"] == pytest.approx(1e11, rel=0.02), (xu, yu)
        v = {p["name"]: p["value"] for p in r["params"]}
        assert v["V_D"] == pytest.approx(2.0 * xf, rel=0.01) and v["R_c"] == pytest.approx(100 * yf, rel=0.05)
        cx, cy = fitting.curve(g, r)   # drawn in the data's own units, over them
        assert max(cy) == pytest.approx(float(np.max(ohms * yf)), rel=0.02), (xu, yu)
    with pytest.raises(fitting.FitError, match="resistance"):
        fitting.fit(g, vg, ohms, x_unit="V", y_unit="V", fix={"C_g": cg, "aspect": aspect})
    with pytest.raises(fitting.FitError, match="gate voltage"):
        fitting.fit(g, vg, ohms, x_unit="s", y_unit="Ω", fix={"C_g": cg, "aspect": aspect})


# ------------------------------------------------------------------ A2: a dc chain's sign

def test_a_dc_measurement_chain_keeps_the_currents_sign():
    """A2: at -1 µA through 1 kΩ the assessment read "R_xx ... -1.000001 kΩ ... no problems found", and an I-V from
    -1 µA to 1 µA a step from -1 kΩ to 1 kΩ. R = V / I = (-1 mV) / (-1 µA) = +1 kΩ."""
    rng = np.random.default_rng(2)
    ch = chain.parse({"excitation": {"source": "isrc.I", "form": "dc"}, "readings": {"nvm.V": {"name": "R_xx"}}})
    meas = [{"key": "nvm.V", "unit": "V", "kind": "scalar"}]
    d = chain.derived_readings(ch, {"isrc.I": -1e-6}, {}, meas)
    assert d["nvm.V"][0] == pytest.approx(1 / -1e-6) and "-1 µA dc" in d["nvm.V"][3]
    rows = [{"magnet.B": float(b), "nvm.V": float(1000.0 * -1e-6 + rng.normal(0, 1e-7))} for b in np.linspace(0, 1, 21)]
    out = analysis.analyze_run(rows, [{"key": "magnet.B", "unit": "T"}], meas, {"status": "done"}, derive=d)
    mean = next(r for r in out["series"][0]["results"] if r["name"] == "mean")
    assert mean["num"] == pytest.approx(1000.0, rel=1e-3) and mean["unit"] == "Ω"

    currents = np.linspace(-1e-6, 1e-6, 41)
    rows = [{"isrc.I": float(i), "nvm.V": float(1000.0 * i + rng.normal(0, 2e-7))} for i in currents]
    d = chain.derived_readings(ch, {}, {"isrc": {"I": 1e-7}}, meas)
    out = analysis.analyze_run(rows, [{"key": "isrc.I", "unit": "A"}], meas, {"status": "done"}, derive=d)
    assert "steps" not in out["assessment"] and "-1 kΩ" not in out["assessment"], out["assessment"]
    mean = next(r for r in out["series"][0]["results"] if r["name"] == "mean")
    assert mean["num"] == pytest.approx(1000.0, rel=2e-3)
    peak = chain.parse({"excitation": {"source": "iac.amplitude", "form": "peak"}, "readings": {"li.X": {"gain": 1}}})
    assert chain.excitation_current(peak, lambda k: -1e-6)[0] == pytest.approx(1e-6 / math.sqrt(2))   # ac: no sign


# ------------------------------------------------------------------ A3: a narrow line on a long trace

def notch(f, f0, fwhm, depth):
    return 10 * np.log10(1 - depth / (1 + ((f - f0) / (fwhm / 2)) ** 2))


def test_a_narrow_line_on_a_long_trace_is_found():
    """A3: 16 001 points with a notch 4 dB deep, 20 points wide and 0.03 dB of noise read "shows no clear dependence on
    frequency"; the same line on 2000 points gave the dip with its Q, f0 / FWHM = 5.0123 GHz / 250 kHz = 20 049."""
    rng = np.random.default_rng(9)
    f = np.linspace(4.9e9, 5.1e9, 16001)
    for y, unit in ((notch(f, 5.0123e9, 250e3, 0.6) + rng.normal(0, 0.03, f.size), "dB"),
                    (1000 - 300 / (1 + ((f - 5.0123e9) / 125e3) ** 2) + rng.normal(0, 5, f.size), "counts/s")):
        s = analysis.analyze_curve(f.tolist(), y.tolist(), "frequency", "vna.S21", "Hz", unit)
        assert s["kind"] == "dip", s["summary"]
        assert s["params"]["center"][0] == pytest.approx(5.0123e9, abs=5e3)
        assert s["params"]["Q"][0] == pytest.approx(5.0123e9 / 250e3, rel=0.03)
    flat = rng.normal(0, 0.03, f.size)
    assert analysis.analyze_curve(f.tolist(), flat.tolist(), "frequency", "vna.S21", "Hz", "dB")["kind"] == "steady"


# ------------------------------------------------------------------ A4: a report's trace chart

def test_a_scan_reports_trace_chart_draws_that_rows_own_fit():
    """A4: a line moving from 4.995 to 5.005 GHz over 5 fields: the chart of the last trace had the first row's fit
    (at 4.995 GHz) dashed over the data peaking at 5.005 GHz."""
    rng = np.random.default_rng(5)
    f = np.linspace(4.99e9, 5.01e9, 401)
    rows = []
    for b in (0.0, 0.5, 1.0, 1.5, 2.0):
        y = 1e-3 * (0.2 + 1.0 / (1 + ((f - (4.995e9 + 5e6 * b)) / 5e5) ** 2)) + rng.normal(0, 1e-5, f.size)
        rows.append({"magnet.B": b, "vna.mag": y.tolist(), "vna.mag.axis": [float(f[0]), float(f[-1])]})
    axes, measures = [{"key": "magnet.B", "unit": "T"}], [{"key": "vna.mag", "unit": "V", "kind": "trace",
                                                           "x_unit": "Hz"}]
    a = analysis.analyze_run(rows, axes, measures, {"status": "done"})
    charts = Collector(Path("."), None, lambda r: None, lambda: [], lambda e: "")._charts(
        rows, {"axes": axes, "measures": measures}, {}, a)
    chart = next(c for c in charts if c.title.startswith("vna.mag"))
    data, fit = chart.series[0], next(s for s in chart.series if s.fit)
    peak = lambda s: s.xs[int(np.nanargmax(np.array(s.ys, dtype=float)))]   # noqa: E731
    assert peak(data) == pytest.approx(5.005e9, abs=1e5) and peak(fit) == pytest.approx(5.005e9, abs=1e5)


# ------------------------------------------------------------------ A5: g2's antibunching time at its limit

def test_the_g2_fit_says_when_its_time_ends_at_the_windows_limit():
    """A5: a true 15 ns in a ±30 ns window read "10.00 ± 0.65 ns" (a sixth of the window, where the fit stops) with
    g2(0) 0.173 for 0.20, and nothing said so. In a ±100 ns window the same emitter gives its 15 ns."""
    rng = np.random.default_rng(2)
    for half, at_limit in ((30e-9, True), (100e-9, False)):
        tau = np.arange(-half, half + 1e-12, 0.5e-9)
        counts = rng.poisson(400 * (1 - 0.8 * np.exp(-np.abs(tau - 1e-9) / 15e-9)))
        fit = correlation.fit_g2(tau.tolist(), (counts / 400.0).tolist(), counts.tolist())
        assert fit["tau_at_limit"] is at_limit and fit["tau_limit"] == pytest.approx(2 * half / 6, rel=0.01)
        said = correlation.verdict(fit)
        assert ("τc is not measured" in said) is at_limit and "single emitter" in said
        if not at_limit:
            assert abs(fit["tau"] - 15e-9) < 3 * fit["tau_err"] and abs(fit["g2_zero"] - 0.2) < 3 * fit["g2_zero_err"]


# ------------------------------------------------------------------ A6: a parameter at its bound

def test_a_fit_with_a_parameter_at_its_bound_converges():
    """A6: a fit whose parameter ended at a bound ran to the step limit and was FAILED ("it did not converge"); the
    same fit with that parameter held at the bound's value converges, to the same chi-squared."""
    rng = np.random.default_rng(9)
    x = np.linspace(-10, 10, 201)
    y = 1.0 + 2.0 / (1 + x ** 2) + rng.normal(0, 0.02, x.size)   # FWHM 2, bounded at 1.5
    bound = fitting.fit(fitting.MODELS["lorentzian"](), x, y, bounds={"fwhm": [0, 1.5]})
    held = fitting.fit(fitting.MODELS["lorentzian"](), x, y, fix={"fwhm": 1.5})
    assert bound["converged"] and not bound["failed"] and bound["iterations"] < 100
    assert bound["chi2"] == pytest.approx(held["chi2"], rel=1e-6)
    for p, q in zip(bound["params"], held["params"]):
        assert p["value"] == pytest.approx(q["value"], rel=1e-4, abs=1e-6)
    assert next(p for p in bound["params"] if p["name"] == "fwhm")["at_bound"]

    vg = np.linspace(-40, 40, 161)
    n = 1.21e-4 * (vg - 2.0) / QE
    clean = 2.0 / (QE * 1.0 * np.sqrt(1e15 ** 2 + n ** 2))   # four-probe: R_c = 0, the model's own bound
    for seed in range(20):
        r = fitting.fit(fitting.MODELS["graphene"](), vg, clean + np.random.default_rng(seed).normal(0, 5, vg.size),
                        x_unit="V", y_unit="Ω", fix={"C_g": 1.21e-4, "aspect": 2.0})
        assert not r["failed"], (seed, r["failed"])
        assert derived(r, "mobility")["value"] == pytest.approx(1e4, rel=0.01)


# ------------------------------------------------------------------ A7: a line in dB fitted on linear power

def test_a_db_lines_sigma_goes_into_power_and_its_kept_fit_is_drawn_in_db(tmp_path, monkeypatch):
    """A7: a sigma of 0.05 dB weighted the linear power (reduced chi-squared 0.045, the width ±28 kHz), and plot_runs
    drew the kept fit in linear power (0 to 0.03) over the trace in dB (-40 to -15)."""
    f = np.linspace(4.995e9, 5.005e9, 401)
    clean = notch(f, 5e9, 1e6, 0.75)
    widths, errors, reduced = [], [], []
    for seed in range(15):
        db = clean + np.random.default_rng(seed).normal(0, 0.05, f.size)
        _, text = datatools.fit_run(None, "", "lorentzian", x_values=f.tolist(), y_values=db.tolist(), x_unit="Hz",
                                    y_unit="dB", sigma=0.05)
        reduced.append(float(text.split("(reduced ")[1].split(")")[0]))
        w = next(ln for ln in text.splitlines() if ln.strip().startswith("fwhm =")).split("=")[1].split()
        scale = {"kHz": 1.0, "MHz": 1e3}[w[3]]   # "998.1 ± 3.6 kHz" or "1.0003 ± 0.0036 MHz", in kHz
        widths.append(float(w[0]) * scale)
        errors.append(float(w[2]) * scale)
    # the sigma given is the noise: chi-squared per degree of freedom near 1, and the errors as the fits spread
    assert np.mean(reduced) == pytest.approx(1.0, abs=0.15)
    assert np.mean(errors) == pytest.approx(np.std(widths, ddof=1), rel=0.4)
    assert np.mean(widths) == pytest.approx(1000.0, abs=3 * np.std(widths, ddof=1) / math.sqrt(len(widths)))

    from labharness import ScriptedApprover, plotting
    from labharness.agent.runtime import LabRuntime
    from simlab import build_virtual_lab
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 200), tmp_path / "data")
    assert "done" in rt.run("plan: vna\nsetup: {vna.rf: on, vna.power: -20, vna.f_start: 3.99e9, vna.f_stop: 4.01e9, "
                            "vna.points: 401}\nmeasure: [vna.S21]")
    _, text = datatools.fit_run(rt, "last", "lorentzian", y="vna.S21")
    assert "kept in" in text
    drawn = []
    real = plotting.Panel
    monkeypatch.setattr(plotting, "Panel", lambda curves, *a, **k: drawn.append(list(curves)) or real(curves, *a, **k))
    datatools.plot_runs(rt, [{"run": "last", "y": "vna.S21", "fit": "vna.S21 lorentzian"}])
    trace = next(c for c in drawn[0] if c.style != "fit")
    fit = next(c for c in drawn[0] if c.style == "fit")
    # over the trace in dB, where the line is (its wings, near zero power, are far from any point in dB): before, at 0
    on = np.interp(np.asarray(trace.xs, dtype=float), np.asarray(fit.xs, dtype=float), np.asarray(fit.ys, dtype=float))
    ys = np.asarray(trace.ys, dtype=float)
    line = ys > np.nanmax(ys) - 10
    assert line.sum() > 10 and np.nanmedian(np.abs(on[line] - ys[line])) < 0.5
    assert np.nanmax(on) == pytest.approx(np.nanmax(ys), abs=1.0)


# ------------------------------------------------------------------ A8: hysteresis at small steps

def test_hysteresis_pairs_up_and_down_at_the_same_x_at_any_step():
    """A8: a pump-probe delay stepped by 20 fs, the same both ways, read "up and down sweeps differ by ... 79% of the
    signal": x was matched to 12 decimals, so each point was paired with another's."""
    t = np.arange(-1e-12, 10e-12 + 1e-18, 20e-15)
    xs = t.tolist() + t[::-1][1:].tolist()
    signal = lambda v: np.where(v > 0, np.exp(-v / 2e-12), 0.0)   # noqa: E731
    same = (1e-3 * signal(np.array(xs))).tolist()
    assert analysis.hysteresis(xs, same, "s", "V") is None
    lagging = (1e-3 * signal(np.array(xs) - np.where(np.arange(len(xs)) > t.size - 1, 0.5e-12, 0.0))).tolist()
    assert "hysteresis" in analysis.hysteresis(xs, lagging, "s", "V")   # the way back 0.5 ps late: a real one
    current = np.linspace(-10e-12, 10e-12, 201)
    xs = current.tolist() + current[::-1][1:].tolist()
    assert analysis.hysteresis(xs, [1e9 * x for x in xs], "A", "V") is None


# ------------------------------------------------------------------ A9: the derivative on uneven x

def test_the_derivative_step_is_right_on_unevenly_spaced_x():
    """A9: y = x² on a log sweep: the Analysis pane's derivative was off by 31% at 3 points a decade; numpy's
    gradient with x (second order on uneven steps) is exact for a parabola."""
    for per_decade in (20, 3):
        x = np.logspace(-2, 0, 2 * per_decade + 1)
        out = workbench.step(np, x, x ** 2, "V", "A", "smu.V", "smu.I", {"op": "deriv"})
        assert np.allclose(out[1][1:-1], 2 * x[1:-1], rtol=1e-9)
    x = np.array([0.0, 0.1, 0.2, 0.2, 0.3, 0.4])   # a repeated x: the slope over its neighbours there, never inf
    out = workbench.step(np, x, 3 * x, "V", "A", "x", "y", {"op": "deriv"})
    assert np.all(np.isfinite(out[1])) and np.allclose(out[1], 3.0)
