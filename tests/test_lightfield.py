"""A PyLoN through LightField's automation (lightfield.py), and PICam saying what it sees and what went wrong
(princeton.py), with LightField and PICam simulated."""
from types import SimpleNamespace

import pytest

from labharness import EventLog, SafetyGate, ScriptedApprover, builtin_registry, frames
from labharness.adapters import AdapterError
from labharness.drivers import MODULES
from labharness.virtual_lab import build_lab

from test_more_instruments import FakePicamDLL, _out


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(frames, "FOLDER", tmp_path / "frames")
    yield
    MODULES.clear()


def gate_for(lab, tmp_path):
    return SafetyGate(lab, ScriptedApprover([]), EventLog(tmp_path / "events.jsonl", lab.clock))


class Region:
    def __init__(self, x, y, width, height, x_binning, y_binning):
        self.X, self.Y, self.Width, self.Height, self.XBinning, self.YBinning = x, y, width, height, x_binning, y_binning


class Image:
    def __init__(self, rows):
        self.rows, self.Height, self.Width = rows, len(rows), len(rows[0])

    def GetData(self):
        return [v for row in self.rows for v in row]


class DataSet:
    def __init__(self, frames, region):
        self.frames, self.Regions = frames, [region]

    def GetFrame(self, region, k):
        return Image(self.frames[k])


class Experiment:
    """A LightField experiment with a PyLoN on a spectrograph: an 8-column chip, 1 nm per column from 500 nm."""

    def __init__(self):
        self.values = {"exposure": 100.0, "frames": 1, "center": 600.0, "grating": "[500nm,300][0][0]",
                       "temperature": -119.5, "setpoint": -120.0, "status": "Locked"}
        self.Name, self.IsReadyToRun, self.IsRunning, self.IsUpdating = "", True, False, False
        self.SystemColumnCalibration = [500.0 + k for k in range(8)]
        self.FullSensorRegion = Region(0, 0, 8, 2, 1, 1)
        self.region = Region(2, 0, 4, 2, 2, 2)          # columns 2-5 binned by 2, both rows binned to one
        self.gratings = ["[500nm,300][0][0]", "[550nm,900][1][0]", "[Mirror,1200][2][0]"]
        self.captured = []

    def Load(self, name):
        if name != "PL spectra":
            return False
        self.Name = name
        return True

    def Exists(self, key):
        return key in self.values

    def GetValue(self, key):
        return self.values[key]

    def SetValue(self, key, value):
        self.values[key] = value

    def IsValid(self, key, value):
        return not (key == "center" and value > 1500)   # the spectrograph's own range

    def GetCurrentCapabilities(self, key):
        return list(self.gratings) if key == "grating" else []

    def Capture(self, count):
        self.captured.append(count)
        return DataSet([[[10 * k, 10 * k + 1]] for k in range(count)], self.region)   # one binned row, two columns


class FakeLightField:
    """LightField's automation: Automation(visible, arguments) starts it; its settings are named by keys."""
    CameraSettings = SimpleNamespace(ShutterTimingExposureTime="exposure", SensorTemperatureReading="temperature",
                                     SensorTemperatureSetPoint="setpoint", SensorTemperatureStatus="status")
    ExperimentSettings = SimpleNamespace(FrameSettingsFramesToStore="frames")
    SpectrometerSettings = SimpleNamespace(GratingCenterWavelength="center", Grating="grating")

    def __init__(self):
        self.experiment, self.started, self.disposed = Experiment(), [], False

    def Automation(self, visible, arguments):
        self.started.append(visible)
        lightfield = self

        class Automation:
            LightFieldApplication = SimpleNamespace(Experiment=lightfield.experiment)

            def Dispose(self):
                lightfield.disposed = True
        return Automation()

    def no_arguments(self):
        return []


LAB = "lab: L\ninstruments:\n  spec: {card: princeton/lightfield, address: 'lightfield://PL spectra'}\n"


def test_a_pylon_on_a_spectrograph_through_lightfield(tmp_path):
    pytest.importorskip("numpy")
    lf = MODULES["lightfield"] = FakeLightField()
    lab = build_lab(LAB)
    assert lf.started == [True] and lf.experiment.Name == "PL spectra"           # LightField's window, the experiment
    before = dict(lf.experiment.values)
    gate = gate_for(lab, tmp_path)
    assert lab.read("spec", "exposure") == pytest.approx(0.1) and lab.read("spec", "grating") == "[500nm,300][0][0]"
    assert lab.read("spec", "temperature") == -119.5 and lab.read("spec", "temperature_status") == "locked"
    assert lab.read("spec", "experiment") == "PL spectra" and lf.experiment.values == before   # reading sets nothing
    assert gate.set("spec", "exposure", 2).ok and lf.experiment.values["exposure"] == 2000.0       # ms in LightField
    assert gate.set("spec", "grating", 900).ok and lf.experiment.values["grating"] == "[550nm,900][1][0]"
    r = gate.set("spec", "grating", 600)
    assert not r.ok and "fits no grating" in r.message and "[Mirror,1200][2][0]" in r.message
    assert gate.set("spec", "center_wavelength", 650).ok and lf.experiment.values["center"] == 650.0
    r = gate.set("spec", "center_wavelength", 2000)
    assert not r.ok and "doesn't take center wavelength" in r.message                        # past the spectrograph
    assert gate.set("spec", "frames", 3).ok
    assert lab.read("spec", "spectrum") == [10.0, 11.0] and lf.experiment.captured == [3]      # three frames averaged
    assert lab.read("spec", "wavelengths") == [502.5, 504.5]                    # its columns, binned by 2, in nm
    assert "image" in lab.instrument("spec").spec("frame").show(lab.read("spec", "frame"))
    lf.experiment.IsRunning = True
    with pytest.raises(AdapterError) as busy:
        lab.read("spec", "spectrum")
    assert "stop it first" in str(busy.value)                                   # someone pressed Run in LightField
    lf.experiment.IsRunning = False
    assert lab.instrument("spec").backend.identity() == "Teledyne Princeton Instruments,LightField,PL spectra"
    lab.close()
    assert lf.disposed                                                         # the LightField Kelvin started closes


def test_lightfield_says_what_it_needs():
    from labharness.lightfield import LightFieldCamera
    card = builtin_registry().get("princeton/lightfield")
    lf = FakeLightField()
    with pytest.raises(AdapterError) as missing:
        LightFieldCamera("spec", card, "lightfield://Raman", module=lf)
    assert "no saved experiment 'Raman'" in str(missing.value) and lf.disposed   # and the LightField it started closed
    lf = FakeLightField()
    lf.experiment.IsReadyToRun = False
    cam = LightFieldCamera("spec", card, "lightfield://PL spectra?visible=0", module=lf)
    assert lf.started == [False]                                                # no window
    with pytest.raises(AdapterError) as ready:
        cam.snap()
    assert "isn't ready to acquire" in str(ready.value)
    lf.experiment.SystemColumnCalibration, cam.region = [], None                 # no spectrograph calibration
    assert cam.wavelengths() == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]           # column numbers


class NamedPicam(FakePicamDLL):
    """PICam naming its errors and models, with one camera free and another open in another program."""

    def __init__(self, busy=True, open_error=0):
        super().__init__()
        P = self.P
        self.busy = (P.CameraID * 1)(P.CameraID(2, 1, b"PyLoN 100BR", b"7654321")) if busy else None
        self.open_error = open_error

    def __getattr__(self, name):
        P = self.P
        if name == "Picam_GetUnavailableCameraIDs":
            def call(ids, n):
                if self.busy is not None:
                    _out(ids).contents = self.busy[0]
                    _out(n).value = 1
                return 0
            return call
        if name == "Picam_GetEnumerationString":
            def call(kind, value, out):
                text = {(P.ERROR_TYPE, 34): b"Device Open Elsewhere", (P.MODEL_TYPE, 1): b"PyLoN 400BR eXcelon"}.get(
                    (kind, value))
                if text is None:
                    return 7
                _out(out).value = text
                return 0
            return call
        if name in ("Picam_OpenCamera", "Picam_OpenFirstCamera") and self.open_error:
            return lambda *args: self.open_error
        return super().__getattr__(name)


def test_picam_says_what_it_sees_and_what_went_wrong():
    from labharness import princeton
    found = princeton.discover_picam(NamedPicam())
    assert found == [("picam://1234567", "Princeton Instruments,PyLoN 400BR eXcelon,1234567")]
    note = princeton.LAST["picam"]
    assert "PyLoN 400BR eXcelon (1234567) found" in note
    assert "PyLoN 100BR (7654321) is open in another program (LightField?)" in note and "lightfield://" in note
    card = builtin_registry().get("princeton/picam")
    with pytest.raises(AdapterError) as taken:
        princeton.PicamCamera("ccd", card, "picam://1234567", module=NamedPicam(busy=False, open_error=34))
    assert "PICam couldn't open the camera: Device Open Elsewhere. Another program has the camera" in str(taken.value)
    assert princeton._users[0] == 0                                            # PICam let go again
    princeton.discover_picam(NamedPicam(busy=False))
    assert princeton.LAST["picam"].endswith("PyLoN 400BR eXcelon (1234567) found")


def test_the_scan_says_what_teledyne_software_saw(monkeypatch, tmp_path):
    from labharness import drivers, lightfield, princeton
    from labharness.survey import Survey
    monkeypatch.setitem(princeton.LAST, "picam", "PICam (Princeton Instruments cameras): not installed here")
    monkeypatch.setattr(lightfield, "root", lambda: None)
    assert drivers.library_notes() == []                     # no Teledyne software at all: nothing to say
    monkeypatch.setattr(lightfield, "root", lambda: str(tmp_path))
    monkeypatch.setattr(lightfield.sys, "platform", "win32")
    notes = drivers.library_notes()
    assert notes[0].endswith("not installed here") and notes[1].startswith("LightField: installed")
    lines = Survey(visa="none", libraries=notes).lines()
    assert lines[-2:] == notes                                # in the scan's report, after the buses
