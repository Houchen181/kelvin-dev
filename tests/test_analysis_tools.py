"""Kelvin's own analysis tools (field-test plan items 20 and 21): fit_run with Kelvin's and the person's models, kept
in run.h5; export_run (CSV, Excel, NumPy), the Runs pane's download and data.csv at every run's end; plot_runs; and
run_python's sandbox."""
import csv
import io
import json
import math
import re
import time
import zipfile
import xml.etree.ElementTree as ET

import pytest

np = pytest.importorskip("numpy")
h5py = pytest.importorskip("h5py")

from labharness import ScriptedApprover, build_virtual_lab, datatools, export, fitting, rundata  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402

GATE = ("plan: gate\nsetup: {lockin.amplitude: 0.05, smu.output: on}\nsweep: {smu.V: {from: 0, to: 3, points: 61}}\n"
        "settle: 0.5\nmeasure: [lockin.X, smu.I]\nafter: {smu.V: 0}")
VNA = ("plan: vna\nsetup: {vna.rf: on, vna.power: -20, vna.f_start: 3.99e9, vna.f_stop: 4.01e9, vna.points: 401}\n"
       "measure: [vna.S21]")


@pytest.fixture
def rt(tmp_path):
    return LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 200), tmp_path / "data")


def last(rt):
    return rt.pick_runs(last=1)[-1]

# ------------------------------------------------------------------ fits


def test_fit_run_fits_a_runs_peak_and_keeps_it_in_run_h5(rt):
    assert "done" in rt.run(GATE)
    run_id = last(rt)
    png, text = datatools.fit_run(rt, "last", "lorentzian", y="lockin.X")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert "lorentzian fit" in text and "x0 = 1.5" in text and "reduced" in text
    assert "kept in" in text and "/fits/lockin.X lorentzian" in text
    (kept,) = fitting.stored(rt._run_dir(run_id), with_data=True)
    assert kept["name"] == "lockin.X lorentzian" and kept["model"] == "lorentzian"
    assert kept["chi2"] > 0 and kept["dof"] == 61 - 4 and len(kept["residuals"]) == 61
    cov = np.asarray(kept["covariance"])
    assert cov.shape == (4, 4) and np.allclose(cov, cov.T)
    errors = dict(zip(kept["params"], kept["errors"]))
    assert errors["x0"] == pytest.approx(math.sqrt(cov[2, 2]))
    assert kept["spec"]["y"] == "lockin.X"
    # fitting again under the same name replaces it; another name keeps both; the run's card is untouched
    datatools.fit_run(rt, run_id, "gaussian", y="lockin.X", name="lockin.X lorentzian")
    datatools.fit_run(rt, run_id, "linear", y="smu.I", name="I-V")
    assert sorted(f["name"] for f in fitting.stored(rt._run_dir(run_id))) == ["I-V", "lockin.X lorentzian"]
    assert rundata.load(rt._run_dir(run_id), with_rows=False).analysis["series"]


def test_the_persons_model_with_a_window_weights_and_a_held_parameter(rt):
    assert "done" in rt.run(GATE)
    _, text = datatools.fit_run(rt, "last", "y0 + A / (1 + ((x - c) / (w/2))**2)", y="lockin.X",
                                start={"y0": 0, "A": 4e-5, "c": 1.4, "w": 1}, fix={"y0": 0}, x_range=[0.5, 2.5],
                                sigma=2e-7, keep=False)
    assert "expression fit" in text and "window: 41 of 61 points with x in [0.5, 2.5]" in text
    assert "y0 = 0 (held)" in text and "weighted by the sigma given" in text and "kept" not in text
    _, text = datatools.fit_run(rt, "last", "a + b*x", y="smu.I", where="smu.V > 1", keep=False)
    assert "window: 40 of 61 points where smu.V > 1" in text
    with pytest.raises(datatools.ToolError, match="no parameter"):
        datatools.fit_run(rt, "last", "2*x", y="smu.I")
    with pytest.raises(datatools.ToolError, match="has no parameter q"):
        datatools.fit_run(rt, "last", "lorentzian", y="lockin.X", fix={"q": 1})


def test_a_resonance_recorded_in_db_is_fitted_on_linear_power(rt):
    assert "done" in rt.run(VNA)
    _, text = datatools.fit_run(rt, "last", "lorentzian", y="vna.S21", keep=False)
    assert "the trace at row 0" in text and "fitted as linear power 10^(dB/10)" in text
    assert "Q (center/FWHM)" in text


def rng():
    return np.random.default_rng(7)


def test_resonator_circle_fit_with_cable_delay():
    f = np.linspace(5e9 - 3e6, 5e9 + 3e6, 801)
    true = dict(fr=5.0002e9, Ql=1.0e4, Qc=2.0e4, phi=0.2, a=0.8, alpha=0.3, tau=50e-9)
    m = fitting.MODELS["resonator"]()
    noise = rng().normal(0, 0.003, (2, f.size))
    r = fitting.fit(m, f, m.f(f, true) + noise[0] + 1j * noise[1], x_unit="Hz")
    v = {p["name"]: (p["value"], p["error"]) for p in r["params"]}
    for k in ("fr", "Ql", "Qc", "phi", "tau"):
        assert abs(v[k][0] - true[k]) < 5 * v[k][1], (k, v[k], true[k])
    qi = next(d for d in r["derived"] if d["name"].startswith("Qi"))
    expected = 1 / (1 / true["Ql"] - math.cos(true["phi"]) / true["Qc"])
    assert qi["value"] == pytest.approx(expected, rel=0.02)
    mag = fitting._resonator_magnitude()   # without the phase, from |S21|
    r = fitting.fit(mag, f, np.abs(m.f(f, true)) + noise[0], x_unit="Hz")
    assert {p["name"]: p["value"] for p in r["params"]}["Ql"] == pytest.approx(1e4, rel=0.02)


def test_a_labsim_resonators_q_from_what_its_pna_reports(monkeypatch):
    """labsim's cQED chip (field test l04): its notch at 7.613 GHz has Qc 1.2e5 and Qi 9e5 behind a 58 ns cable
    delay. The PNA reports dB and degrees. A Lorentzian on the dB values gave Q 2.4 times too high; Kelvin's
    automatic analysis (linear power) and fit_run's circle fit give the loaded Q, |Qc|, Qi and the delay."""
    from labsim_bench import LABSIM
    monkeypatch.syspath_prepend(str(LABSIM))
    from labsim.world import Chip
    from labharness.analysis import analyze_curve, fit as normalised_fit
    chip = Chip({})
    f = np.linspace(7.613e9 - 0.5e6, 7.613e9 + 0.5e6, 801)
    z = chip.s21(f, 0.0, 0.0, 0.012)
    g = rng()
    z = z + chip.noise_rel(0.0, 1e3) * np.abs(z).mean() * (g.normal(size=f.size) + 1j * g.normal(size=f.size))
    db, deg = 20 * np.log10(np.abs(z)), np.degrees(np.angle(z))
    ql = 1 / (1 / 9.0e5 + 1 / 1.2e5)                       # the chip's own, at this power (its TLS loss saturated)
    on_db = normalised_fit("lorentzian", f, db)
    assert on_db["values"][2] / on_db["values"][3] > 2 * ql   # the old way
    s = analyze_curve(f.tolist(), db.tolist(), "frequency", "vna.S21", "Hz", "dB")
    q = next(r for r in s["results"] if r["name"].startswith("Q"))
    assert q["num"] == pytest.approx(ql, rel=0.03)
    r = fitting.fit(fitting.MODELS["resonator"](), f, 10 ** (db / 20) * np.exp(1j * np.radians(deg)), x_unit="Hz")
    v = {p["name"]: p["value"] for p in r["params"]}
    assert v["Ql"] == pytest.approx(ql, rel=0.01) and v["Qc"] == pytest.approx(1.2e5, rel=0.01)
    assert v["tau"] == pytest.approx(58e-9, rel=0.01) and v["phi"] == pytest.approx(0.05, abs=0.01)
    qi = next(d["value"] for d in r["derived"] if d["name"].startswith("Qi"))
    assert qi == pytest.approx(9.0e5, rel=0.03)


def test_biexponential_convolved_with_the_instrument_response():
    t = np.linspace(-20e-12, 400e-12, 600)
    m = fitting.MODELS["biexp_irf"]()
    true = dict(y0=0.01, A1=1.0, tau1=80e-12, A2=0.5, tau2=12e-12, t0=10e-12, irf=6e-12)
    r = fitting.fit(m, t, m.f(t, true) + rng().normal(0, 0.005, t.size), x_unit="s")
    v = {p["name"]: (p["value"], p["error"]) for p in r["params"]}
    for k in ("tau1", "tau2", "t0", "irf"):
        assert abs(v[k][0] - true[k]) < 5 * v[k][1], (k, v[k], true[k])


def test_graphene_hall_and_hc2_models():
    v = np.linspace(-40, 40, 161)
    g = fitting.MODELS["graphene"]()
    true = dict(R_c=300.0, mu=1.0, n0=3e15, V_D=2.0, C_g=1.21e-4, aspect=2.0)
    r = fitting.fit(g, v, g.f(v, true) * (1 + rng().normal(0, 0.003, v.size)), fix={"C_g": 1.21e-4, "aspect": 2.0},
                    x_unit="V", y_unit="Ω")
    d = {x["name"]: x["value"] for x in r["derived"]}
    assert d["mobility"] == pytest.approx(1e4, rel=0.02) and d["residual density n0"] == pytest.approx(3e11, rel=0.03)
    with pytest.raises(fitting.FitError, match="needs C_g and aspect"):
        fitting.fit(g, v, v)
    b = np.linspace(-1, 1, 41)
    hall = fitting.MODELS["hall"]()
    r = fitting.fit(hall, b, 5 - b / (fitting.QE * 2e16) + rng().normal(0, 0.5, b.size), fix={"rho_xx": 1000.0},
                    x_unit="T", y_unit="Ω")
    d = {x["name"]: x for x in r["derived"]}
    assert d["carrier density n = 1/(e·|R_H|)"]["value"] == pytest.approx(2e12, rel=0.01)
    assert d["carriers"]["unit"] == "electrons" and "Hall mobility |R_H|/rho_xx" in d
    temps = np.linspace(1, 9, 17)
    whh = fitting.MODELS["whh_hc2"]()
    r = fitting.fit(whh, temps, whh.f(temps, {"Tc": 9.2, "S": 2.0}) + rng().normal(0, 0.05, temps.size))
    d = {x["name"]: x["value"] for x in r["derived"]}
    assert d["Bc2(0), WHH (0.693·Tc·S)"] == pytest.approx(0.693 * 9.2 * 2.0, rel=0.02)
    gl = fitting.MODELS["gl_hc2"]()
    r = fitting.fit(gl, temps, gl.f(temps, {"Tc": 9.2, "Bc2_0": 12.0}))
    assert {p["name"]: p["value"] for p in r["params"]}["Bc2_0"] == pytest.approx(12.0, rel=1e-4)


def test_errors_match_numpys_own_least_squares():
    x = np.linspace(0, 10, 30)
    y = 1.5 + 0.7 * x + rng().normal(0, 0.3, x.size)
    r = fitting.fit(fitting.MODELS["linear"](), x, y)
    _, cov = np.polyfit(x, y, 1, cov=True)
    errs = {p["name"]: p["error"] for p in r["params"]}
    assert errs["b"] == pytest.approx(math.sqrt(cov[0, 0]), rel=1e-4)
    assert errs["a"] == pytest.approx(math.sqrt(cov[1, 1]), rel=1e-4)


def test_numbers_given_instead_of_a_run(rt):
    temps = [1, 2, 3, 4, 5, 6, 7, 8]
    b = [11.8, 11.3, 10.6, 9.6, 8.3, 6.8, 5.0, 2.9]
    png, text = datatools.fit_run(rt, "", "gl_hc2", x_values=temps, y_values=b, x_unit="K", y_unit="T")
    assert png and "gl_hc2 fit" in text and "GL coherence length" in text and "kept" not in text

# ------------------------------------------------------------------ exports


def test_export_run_writes_csv_excel_and_numpy_in_full(rt):
    assert "done" in rt.run(GATE)
    run_id = last(rt)
    folder = rt._run_dir(run_id)
    assert (folder / "data.csv").exists()          # every run gets it at its end
    table = rundata.columns(folder)
    text = datatools.export_run(rt, run_id, "csv")
    assert "data.csv" in text and "Runs pane" in text
    rows = list(csv.reader(io.StringIO((folder / "data.csv").read_text(encoding="utf-8-sig"))))
    head = rows[0]
    assert head[:3] == ["i", "t (s)", "smu.V (V)"] and "lockin.X (V)" in head and len(rows) == 62
    k = head.index("lockin.X (V)")
    assert [float(r[k]) for r in rows[1:]] == list(table.numbers["lockin.X"])   # every digit
    datatools.export_run(rt, run_id, "xlsx")
    with zipfile.ZipFile(folder / "data.xlsx") as z:
        names = z.namelist()
        assert "xl/workbook.xml" in names and "xl/worksheets/sheet1.xml" in names
        book = ET.fromstring(z.read("xl/workbook.xml"))
        sheets = [s.get("name") for s in book.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet")]
        assert sheets == ["data", "run"]
        data = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        assert len(list(data.iter(ns + "row"))) == 62
    datatools.export_run(rt, run_id, "npz")
    with np.load(folder / "data.npz", allow_pickle=False) as z:
        assert np.array_equal(z["lockin.X"], table.numbers["lockin.X"])
        assert json.loads(str(z["meta.json"]))["run_id"] == run_id
    with pytest.raises(datatools.ToolError, match="csv, xlsx or npz"):
        datatools.export_run(rt, run_id, "mat")


def test_a_traces_export_one_line_per_sample(rt):
    assert "done" in rt.run(VNA)
    folder = rt._run_dir(last(rt))
    rows = list(csv.reader(io.StringIO((folder / "vna.S21.csv").read_text(encoding="utf-8-sig"))))
    assert rows[0] == ["i", "x (Hz)", "vna.S21 (dB)"] and len(rows) == 402
    assert float(rows[1][1]) == pytest.approx(3.99e9) and float(rows[-1][1]) == pytest.approx(4.01e9)
    data, name, media = export.download(folder, "csv")
    assert name.endswith("-csv.zip") and set(zipfile.ZipFile(io.BytesIO(data)).namelist()) == {"data.csv",
                                                                                                "vna.S21.csv"}
    datatools.export_run(rt, "last", "xlsx")
    with zipfile.ZipFile(folder / "data.xlsx") as z:
        book = z.read("xl/workbook.xml").decode()
        assert 'name="vna.S21"' in book


def test_the_runs_pane_downloads_an_export(rt, tmp_path):
    pytest.importorskip("starlette")
    pytest.importorskip("httpx")
    from starlette.testclient import TestClient
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    assert "done" in rt.run(GATE)
    run_id = last(rt)
    settings = Settings(tmp_path / "settings.json")
    app = create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"], providers={})
    headers = {"X-Kelvin-Token": settings.access_key}
    with TestClient(app) as client:
        r = client.get("/api/runs/%s/export?format=xlsx" % run_id, headers=headers)
        assert r.status_code == 200 and r.content[:2] == b"PK"
        assert 'filename="%s-data.xlsx"' % run_id in r.headers["content-disposition"]
        r = client.get("/api/runs/%s/export" % run_id, headers=headers)
        assert r.status_code == 200 and r.content.decode("utf-8-sig").startswith("i,t (s),smu.V (V)")
        assert client.get("/api/runs/%s/export?format=doc" % run_id, headers=headers).status_code == 400
        assert client.get("/api/runs/nope/export", headers=headers).status_code == 404
        assert client.get("/api/runs/%s/export" % run_id).status_code == 401   # the access key, as for every /api

# ------------------------------------------------------------------ figures


def test_plot_runs_overlays_derived_quantities_in_their_units(rt):
    assert "done" in rt.run(GATE)
    assert "done" in rt.run(GATE.replace("from: 0, to: 3", "from: 0, to: 2"))
    first, second = rt.pick_runs(last=2)
    png, text = datatools.plot_runs(rt, [{"run": first, "y": "lockin.X", "unit": "µV"},
                                         {"run": second, "y": "lockin.X", "unit": "µV", "style": "points"}],
                                    title="Two gate sweeps", formats=["png", "svg", "pdf"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert "Plot: lockin.X (µV) vs smu.V (V)" in text and "2 curves" in text
    saved = re.search(r"saved: (.*)", text).group(1).split("; ")
    assert [s.rsplit(".", 1)[1] for s in saved] == ["png", "svg", "pdf"]
    ET.parse(saved[1])
    assert open(saved[2], "rb").read(5) == b"%PDF-"
    # a derived quantity in its own unit, on a log axis
    _, text = datatools.plot_runs(rt, [{"run": first, "y": "smu.V / smu.I", "unit": "Ω", "where": "smu.V > 0"}],
                                  y_log=True)
    assert "(Ω, log)" in text
    with pytest.raises(datatools.ToolError, match="can't be shown in"):
        datatools.plot_runs(rt, [{"run": first, "y": "lockin.X", "unit": "A"}])


def test_a_heat_map_and_a_kept_fit(rt):
    assert "done" in rt.run("plan: map\nsweep: {smu.V: {from: 0, to: 1, points: 9}, laser.power: {from: 0, to: 0.001, "
                            "points: 9}}\nmeasure: [counter.rate]")
    png, text = datatools.plot_runs(rt, heat_map={"run": "last", "z": "counter.rate"})
    assert png and "Heat map: counter.rate" in text and "9 x 9 points" in text
    assert "done" in rt.run(GATE)
    datatools.fit_run(rt, "last", "lorentzian", y="lockin.X", name="peak")
    _, text = datatools.plot_runs(rt, [{"run": "last", "y": "lockin.X", "fit": "peak"}])
    assert "dashed: lorentzian fit" in text

# ------------------------------------------------------------------ run_python


def test_run_python_computes_on_a_runs_data_and_shows_its_pictures(rt):
    assert "done" in rt.run(GATE)
    run_id = last(rt)
    code = ("x, y = run['smu.V'], run['lockin.X']\n"
            "print('peak at %.2f V' % x[np.argmax(y)])\n"
            "print(run.units['lockin.X'], len(run.keys()))\n"
            "plot((x, y, 'X'), title='X')\n"
            "open('result.txt', 'w').write('ok')\n")
    images, text = datatools.run_python(rt, code, [run_id])
    assert "finished" in text and "peak at 1.5" in text and "V 5" in text
    assert len(images) == 1 and images[0][:8] == b"\x89PNG\r\n\x1a\n"
    assert "result.txt" in text and "plot-1.png" in text
    folder = re.search(r"its folder: (.*)", text).group(1)
    assert open(folder + "/result.txt").read() == "ok"
    assert "in this Python: numpy" in text


@pytest.mark.parametrize("code, refused", [
    ("open(RUN, 'ab').write(b'x')", "writes only in its own folder"),
    ("import h5py\nh5py.File(RUN, 'a')", "read-only"),
    ("import os\nos.remove(RUN)", "writes only in its own folder"),
    ("import os\nos.rename(RUN, 'gone.h5')", "writes only in its own folder"),
    ("open(OUTSIDE, 'w').write('x')", "writes only in its own folder"),
    ("open(SETTINGS).read()", "reads only its own folder"),
    ("import socket\nsocket.create_connection(('127.0.0.1', 9))", "can't do this here (socket"),
    ("import subprocess\nsubprocess.run(['python', '-c', '1'])", "can't do this here (subprocess"),
    ("import ctypes\nctypes.CDLL('kernel32' if __import__('os').name == 'nt' else 'libc.so.6')", "ctypes"),
    ("import pyvisa", "can't import pyvisa"),
])
def test_run_python_cant_touch_runs_files_instruments_or_programs(rt, tmp_path, code, refused):
    assert "done" in rt.run(GATE)
    run_id = last(rt)
    h5 = rt._run_dir(run_id) / rundata.RUN_FILE
    before = h5.read_bytes()
    (tmp_path / "settings.json").write_text("{}", encoding="utf-8")
    code = code.replace("RUN", repr(str(h5))).replace("OUTSIDE", repr(str(tmp_path / "evil.txt"))) \
        .replace("SETTINGS", repr(str(tmp_path / "settings.json")))
    _, text = datatools.run_python(rt, code, [run_id])
    assert "stopped with an error" in text and refused in text, text
    assert h5.read_bytes() == before and not (tmp_path / "evil.txt").exists()


def test_run_python_stops_at_its_time_limit_and_at_stop(rt):
    assert "done" in rt.run(GATE)
    t0 = time.monotonic()
    _, text = datatools.run_python(rt, "while True:\n    pass", ["last"], timeout_s=2)
    assert "stopped at its time limit" in text and time.monotonic() - t0 < 15
    import threading
    threading.Timer(1.5, rt.stop).start()   # a person's Stop
    t0 = time.monotonic()
    _, text = datatools.run_python(rt, "while True:\n    pass", ["last"], timeout_s=60)
    assert "stopped (Stop" in text and time.monotonic() - t0 < 15


def test_the_tools_are_served_and_never_wait_for_the_lab(tmp_path):
    pytest.importorskip("mcp")
    import anyio
    from mcp import Client
    from labharness.agent.server import build_server
    from labharness.web.agent import LOOKUPS, OFF_LAB
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")

    async def main():
        async with Client(build_server(rt)) as client:
            return {t.name for t in (await client.list_tools()).tools}
    names = anyio.run(main)
    assert {"fit_run", "plot_runs", "export_run", "run_python"} <= names
    assert {"fit_run", "plot_runs", "export_run", "run_python"} <= LOOKUPS & OFF_LAB
