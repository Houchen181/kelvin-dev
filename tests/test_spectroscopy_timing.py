"""Spectroscopy and timing on the confocal labsim bench (plan item 26): a CCD spectrum's x axis is the Shamrock's
wavelength calibration, the Time Tagger measures g2 with a fit, and the CCD's cooling set point reaches the camera
(F2 l05: "819.6 nm" was a pixel index, g2 was impossible, and the set point failed inside Kelvin)."""
import json
import math

import pytest

from labsim_bench import labsim_bench, labsim_offline  # noqa: F401

LAB = """lab: labsim confocal
instruments:
  idus:
    card: andor/ccd
    address: "andor://0?sdk=2"
    added_by: kelvin
  spec:
    card: andor/shamrock
    address: "shamrock://0"
  counter:
    card: swabian/time-tagger
    address: "timetagger://1910000123?click=1&click2=2"
"""


@pytest.fixture
def confocal(labsim_bench, tmp_path, monkeypatch):
    """The confocal bench with its makers' libraries, Andor's spectrograph DLL among them, and a Kelvin lab on it."""
    import ctypes
    from labharness import spectra
    from labharness.virtual_lab import build_lab
    bench = labsim_bench("confocal", sdk=True)
    monkeypatch.setattr(ctypes, "WinDLL", ctypes.WinDLL)   # put back after the test
    monkeypatch.setattr(ctypes, "windll", ctypes.windll)
    from labsim import dlls
    dlls.install()                                         # as shims/sitecustomize.py does in a program at start
    monkeypatch.setattr(spectra, "_shamrock_lib", [])
    (tmp_path / "lab.yaml").write_text(LAB, encoding="utf-8")
    lab = build_lab(tmp_path / "lab.yaml", partial=True)
    assert lab.unavailable == {}
    yield bench, lab
    lab.close()


def gate_for(lab, tmp_path):
    from labharness import ScriptedApprover
    from labharness.eventlog import EventLog
    from labharness.gate import SafetyGate
    return SafetyGate(lab, ScriptedApprover([True] * 40), EventLog(tmp_path / "events.jsonl", lab.clock))


def light(bench, at=None, watts=0.0075):
    """The 532 nm laser on at `watts` (40% reaches the sample), the focus at `at` (x, y, z um)."""
    bench.ctl("set", "optics.lasers", json.dumps({"laser": {"nm": 532.0, "W": watts, "on": True,
                                                            "path": "excitation", "shutter": ""}}))
    if at is not None:
        bench.ctl("set", "optics.fine", json.dumps(list(at)))


def test_a_ccd_spectrum_has_the_spectrographs_wavelengths(confocal, tmp_path):
    bench, lab = confocal
    gate = gate_for(lab, tmp_path)
    wl = lab.read("idus", "wavelengths")
    # the bench's own calibration: 600 l/mm on a ~300 mm spectrograph, 1024 columns of 26 um, centred on 700 nm
    disp = 26e-3 * 1e6 / (600 * 300.0)
    assert len(wl) == 1024 and wl[0] == pytest.approx(700 + (0.5 - 512) * disp, abs=1e-3)
    assert wl[-1] == pytest.approx(700 + (1023.5 - 512) * disp, abs=1e-3)
    assert gate.set("spec", "grating", 3).ok and gate.set("spec", "wavelength", 575).ok
    wl = lab.read("idus", "wavelengths")   # read again with every spectrum: it follows the grating and centre
    assert wl[512] == pytest.approx(575 + 0.5 * disp / 2, abs=1e-3) and wl[1] - wl[0] == pytest.approx(disp / 2, abs=1e-4)
    light(bench)
    raman = 1.0 / (1.0 / 532.0 - 520.7e-7)   # silicon's 520.7 /cm line under 532 nm light: 547.2 nm
    peak = lab.read("idus", "peak_wavelength")
    assert peak == pytest.approx(raman, abs=0.3)   # in nm: l05's "819.6" was the pixel


def test_a_run_records_the_spectrum_with_its_wavelengths(confocal, tmp_path):
    from labharness import RunEngine, RunStore, rundata
    from labharness.engine import RunEngine as _  # noqa: F401
    bench, lab = confocal
    gate = gate_for(lab, tmp_path)
    light(bench)
    plan = {"plan": "raman_check", "setup": {"spec.grating": 3},
            "sweep": {"spec.wavelength": {"values": [560, 575]}},
            "measure": ["idus.spectrum", "idus.peak_wavelength"]}
    report = RunEngine(gate, RunStore(tmp_path / "runs")).run(plan)
    assert report.status == "done", report.message
    rec = rundata.load(tmp_path / "runs" / report.run_id)
    m = next(m for m in rec.columns["measures"] if m["key"] == "idus.spectrum")
    assert m["x_unit"] == "nm"
    for row, centre in zip(rec.rows, (560, 575)):
        xs = row["idus.spectrum.x"]
        assert len(xs) == len(row["idus.spectrum"]) == 1024 and xs[512] == pytest.approx(centre, abs=0.2)
        assert row["idus.peak_wavelength"] == pytest.approx(1.0 / (1.0 / 532.0 - 520.7e-7), abs=0.3)


def test_a_ccd_on_no_spectrograph_says_so(confocal, tmp_path, monkeypatch):
    from labharness.adapters import AdapterError
    bench, lab = confocal
    cam = lab.instrument("idus").backend
    monkeypatch.setattr(cam, "spectrograph", "none")
    monkeypatch.setattr(cam, "_shamrock", None)
    with pytest.raises(Exception, match="no spectrograph"):
        lab.read("idus", "wavelengths")
    assert len(lab.read("idus", "spectrum_pixels")) == 1024   # by column, honestly so
    monkeypatch.setattr(cam, "spectrograph", "3")
    with pytest.raises(Exception, match="no spectrograph 3"):
        lab.read("idus", "wavelengths")
    assert isinstance(AdapterError("x"), Exception)


def test_the_ccd_set_point_reaches_the_camera_and_a_closed_camera_reads_no_temperature(confocal, tmp_path):
    """F2 l05: setting the iDus to -60 C failed inside Kelvin ("must be real number, not NoneType": the set point
    has no read-back), never reaching the SDK; after a lab switch shut Andor's session, the camera read "0 C"."""
    bench, lab = confocal
    gate = gate_for(lab, tmp_path)
    r = gate.set("idus", "setpoint", -60)
    assert r.ok, r.message
    assert gate.set("idus", "cooler", True).ok
    status = next(i for i in bench.ctl("status")["instruments"] if i["name"] == "ccd")["status"]
    assert status["setpoint_C"] == -60 and status["cooler"] is True
    assert lab.read("idus", "temperature_status") in ("not reached", "not stabilized")
    assert lab.read("idus", "setpoint") == -60   # shown once set: the SDK can't report it
    lab.instrument("idus").backend.sdk.ShutDown()   # another program (or a Kelvin closing) ends Andor's session
    with pytest.raises(Exception, match="20075"):
        lab.read("idus", "temperature")             # an error, never 0 C
    with pytest.raises(Exception, match="isn't initialized"):
        lab.instrument("idus").backend.identity()   # never "Andor,,0", taken for another camera
    r = gate.set("idus", "setpoint", -70)
    assert not r.ok and "20075" in r.message and "NoneType" not in r.message


def single_emitter(labsim_offline):
    """Where a bright emitter sits alone (no other within 2 um), from the bench's own (seeded) sample."""
    optics = labsim_offline("confocal").world.optics
    alone = [e for e in optics.emitters if e["cps"] > 1.5e5 and 5 < e["x"] < 95 and 5 < e["y"] < 95 and
             all(o is e or math.hypot(o["x"] - e["x"], o["y"] - e["y"]) > 2.0 for o in optics.emitters)]
    e = max(alone, key=lambda e: e["cps"])
    return e["x"], e["y"], optics.focus_z + e["dz"]


def test_g2_of_a_single_emitter_is_antibunched(confocal, labsim_offline, tmp_path):
    """F2 l05: the Time Tagger card was only a gated counter, so g2, the test of a single-photon source, was
    impossible. Now it correlates two detectors and fits the dip."""
    bench, lab = confocal
    gate = gate_for(lab, tmp_path)
    x, y, z = single_emitter(labsim_offline)
    light(bench, (x, y, z))
    for param, value in (("integration", 4.0), ("g2_binwidth", 1e-9), ("g2_bins", 200)):
        assert gate.set("counter", param, value).ok
    g2 = lab.read("counter", "g2")
    delays = lab.read("counter", "g2_delay")
    assert len(g2) == len(delays) == 200 and delays[100] == pytest.approx(0.0) and delays[101] == pytest.approx(1e-9)
    g0, err, tau = (lab.read("counter", k) for k in ("g2_zero", "g2_zero_err", "g2_tau"))   # of that correlation
    assert g0 < 0.5 and err < 0.25 and abs(g0 - 0.15) < 3 * err + 0.05   # the bench's emitter: g2(0) = 0.15
    assert 1.5e-9 < tau < 9e-9                                          # and 4 ns antibunching
    light(bench, (x + 20.0, y + 20.0, z))   # away from it, on bare silicon: a few stray coincidences
    with pytest.raises(Exception, match="too few coincidences"):
        lab.read("counter", "g2_zero")    # a new correlation, too thin to fit: said, never a number made up


def test_g2_needs_the_second_detector(confocal, tmp_path, monkeypatch):
    bench, lab = confocal
    monkeypatch.setattr(lab.instrument("counter").backend, "click2", None)
    with pytest.raises(Exception, match="click2"):
        lab.read("counter", "g2")


def test_the_g2_fit_on_its_own():
    np = pytest.importorskip("numpy")
    from labharness.correlation import fit_g2, verdict
    rng = np.random.default_rng(5)
    t = (np.arange(200) - 100) * 1e-9
    for rho, below in ((0.85, True), (0.0, False), (-0.6, False)):
        n = rng.poisson(40 * (1 - rho * np.exp(-np.abs(t - 1e-9) / 3e-9)))
        f = fit_g2(t, n / 40.0, n)
        assert abs(f["g2_zero"] - (1 - rho)) < 3 * f["g2_zero_err"] + 0.05
        assert ("single emitter" in verdict(f)) == below
    assert "bunched" in verdict(f)
    with pytest.raises(ValueError):
        fit_g2([0, 1], [1, 1])
