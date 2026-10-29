"""DAQ cards, spectrometers, monochromators, cameras and nanopositioners, through fakes of their vendors' libraries
and of their serial or network ports."""
import csv
from types import SimpleNamespace

import pytest

from labharness import EventLog, SafetyGate, ScriptedApprover, builtin_registry, frames
from labharness.adapters import set_resource_manager
from labharness.discovery import match_card
from labharness.drivers import MODULES, discover_libraries
from labharness.virtual_lab import build_lab


def gate_for(lab, tmp_path, answers=()):
    return SafetyGate(lab, ScriptedApprover(list(answers)), EventLog(tmp_path / "events.jsonl", lab.clock))


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(frames, "FOLDER", tmp_path / "frames")
    yield
    MODULES.clear()
    set_resource_manager(None)


# ------------------------------------------------------------------ fake vendor libraries

class FakeNIDAQ:
    """nidaqmx: one USB-6001 called Dev1."""

    def __init__(self):
        self.writes, self.reads = [], []
        self.ai = {"Dev1/ai0": [1.0, 2.0, 3.0, 2.0]}
        dev = SimpleNamespace(name="Dev1", product_type="USB-6001", dev_serial_num=0x1F2A3B4)
        daq = self
        self.system = SimpleNamespace(System=SimpleNamespace(local=lambda: SimpleNamespace(devices=[dev])),
                                      Device=lambda name: dev)
        self.constants = SimpleNamespace(TerminalConfiguration=SimpleNamespace(RSE="rse", DIFF="diff"))

        class Channels:
            def __init__(self, task):
                self.task = task

            def __getattr__(self, method):
                def add(name, **kw):
                    self.task.chans.append((method, name, kw))
                return add

        class Task:
            def __init__(self):
                self.chans, self.n = [], 1
                self.ai_channels = self.ao_channels = self.di_channels = self.do_channels = self.ci_channels = \
                    Channels(self)
                self.timing = SimpleNamespace(cfg_samp_clk_timing=lambda rate, samps_per_chan: setattr(self, "n", samps_per_chan))

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, number_of_samples_per_channel=None):
                method, name, kw = self.chans[0]
                daq.reads.append((name, kw))
                if method == "add_ai_voltage_chan":
                    data = daq.ai.get(name, [0.5] * 8)
                    return data[:number_of_samples_per_channel] if number_of_samples_per_channel else data[0]
                if method == "add_di_chan":
                    return True
                return 50.0   # counter edges

            def write(self, value, auto_start=True):
                daq.writes.append((self.chans[0][1], value))

            def start(self):
                pass

            def stop(self):
                pass

        self.Task = Task


class FakeSeabreeze:
    def __init__(self):
        class Spectrometer:
            model, serial_number = "USB4000", "USB4F0001"
            integration_time_micros_limits = (10, 65_000_000)
            us = None

            @classmethod
            def from_serial_number(cls, serial):
                if serial != cls.serial_number:
                    raise RuntimeError("not found")
                return cls()

            @classmethod
            def from_first_available(cls):
                return cls()

            def wavelengths(self):
                return [400 + 0.5 * i + 0.002 * i * i for i in range(40)]   # not evenly spaced

            def intensities(self, correct_dark_counts=False, correct_nonlinearity=False):
                return [100.0 + (900.0 if i == 25 else 0.0) for i in range(40)]

            def integration_time_micros(self, us):
                type(self).us = us

            def close(self):
                pass

        self.Spectrometer = Spectrometer
        self.list_devices = lambda: [SimpleNamespace(serial_number="USB4F0001", model="USB4000")]


class FakePylon:
    """pypylon: one Basler ace whose frames count up, so each grab differs."""

    def __init__(self):
        self.grabs = 0
        pylon = self

        class Node:
            def __init__(self, v):
                self.v = v

            def GetValue(self):
                return self.v

            def SetValue(self, v):
                self.v = v

            def IsReadable(self):
                return True

        info = SimpleNamespace(GetSerialNumber=lambda: "40012345", GetModelName=lambda: "acA1920-155um")

        class Camera:
            def __init__(self, dev):
                self.ExposureTime, self.Gain = Node(10000.0), Node(3.0)
                self.Width, self.Height = Node(4), Node(3)
                self.opened = False

            def Open(self):
                self.opened = True

            def Close(self):
                self.opened = False

            def GetDeviceInfo(self):
                return info

            def GrabOne(self, timeout_ms):
                pylon.grabs += 1
                g = pylon.grabs
                return SimpleNamespace(GrabSucceeded=lambda: True, Release=lambda: None,
                                       Array=[[g * 10 + r * 4 + c for c in range(4)] for r in range(3)])

        self.InstantCamera = Camera
        tl = SimpleNamespace(EnumerateDevices=lambda: [info], CreateDevice=lambda d: d)
        self.TlFactory = SimpleNamespace(GetInstance=lambda: tl)


class FakeANC300:
    """An ANC300's command line (axes 1-2): answers each command with lines ending in OK, as the controller does."""

    def __init__(self):
        self.opened, self.sent, self.closed = [], [], 0
        self.mode, self.offset, self.voltage = {1: "gnd", 2: "stp"}, {1: 0.0, 2: 0.0}, {1: 30.0, 2: 30.0}
        fake = self

        class Link:
            def __init__(self, target, pwd=None):
                fake.opened.append(target)

            def command(self, line, timeout=None):
                fake.sent.append(line)
                words = line.split()
                cmd, axis = words[0], int(words[1]) if len(words) > 1 else 0
                if cmd == "getm":
                    return ["mode = %s" % fake.mode[axis]]
                if cmd == "setm":
                    fake.mode[axis] = words[2]
                elif cmd == "getv":
                    return ["voltage = %.6f V" % fake.voltage[axis]]
                elif cmd == "setv":
                    fake.voltage[axis] = float(words[2])
                elif cmd == "getf":
                    return ["frequency = 1000 Hz"]
                elif cmd in ("geta", "geto"):
                    return ["voltage = %.6f V" % fake.offset[axis]]
                elif cmd == "seta":
                    fake.offset[axis] = float(words[2])
                elif cmd == "getcser":
                    return ["ANC300B-C-1234"]
                elif cmd == "ver":
                    return ["attocube ANC300 controller version 1.1.0-1304 2013-10-17 08:16"]
                return []

            def close(self):
                fake.closed += 1

        self.Link = Link


class FakeANC350:
    """pylablib's Attocube module, for the ANC350 on USB."""

    def __init__(self):
        lib = self
        self.calls = []

        class ANC350:
            def __init__(self, conn=0):
                self.pos = {0: 1e-6, 1: 0.0, 2: 0.0}

            def get_position(self, axis):
                return self.pos[axis]

            def move_to(self, axis, position, precision=None):
                lib.calls.append(("move_to", axis, position))
                self.pos[axis] = position

            def wait_move(self, axis, precision=1e-6, timeout=10.0, period=0.01):
                lib.calls.append(("wait", axis))

            def is_target_reached(self, axis, precision=None):
                return True

            def get_hardware_id(self):
                return 1

            def close(self):
                pass

        self.ANC350 = ANC350
        self.get_usb_devices_number_ANC350 = lambda: 1


def _out(arg):
    """What a ctypes.byref(...) argument points to (the fakes of a DLL write their answers there)."""
    return arg._obj


class FakeShamrockDLL:
    """Andor's spectrograph DLL: one Shamrock, its calls returning 20202 (success)."""

    def __init__(self):
        self.wl, self.grating, self.slit, self.shutter, self.flipper = 500.0, 1, 100.0, 0, 0
        self.calls = []

    def __getattr__(self, name):
        def call(*args):
            self.calls.append(name)
            if name == "ShamrockGetNumberDevices":
                _out(args[0]).value = 1
            elif name == "ShamrockGetWavelength":
                _out(args[1]).value = self.wl
            elif name == "ShamrockSetWavelength":
                self.wl = args[1].value
            elif name == "ShamrockGetGrating":
                _out(args[1]).value = self.grating
            elif name == "ShamrockSetGrating":
                self.grating = args[1]
            elif name == "ShamrockGetAutoSlitWidth":
                assert args[1] == 1          # the input side slit
                _out(args[2]).value = self.slit
            elif name == "ShamrockGetShutter":
                _out(args[1]).value = self.shutter
            elif name == "ShamrockSetShutter":
                self.shutter = args[1]
            elif name == "ShamrockGetFlipperMirror":
                _out(args[2]).value = self.flipper
            elif name == "ShamrockGetSerialNumber":
                args[1].value = b"SR-0123"
            return 20202
        return call


class FakePicamDLL:
    """Princeton Instruments' Picam.dll: one PyLoN whose 4x3 chip reads 1..12, its calls returning 0 (success)."""

    def __init__(self):
        import ctypes
        from labharness import princeton as P
        self.P, self.ctypes = P, ctypes
        self.floats = {P.EXPOSURE: 100.0, P.SETPOINT: -120.0, P.TEMPERATURE: -119.5, P.ADC_SPEED: 0.1}
        self.ints = {P.TEMPERATURE_STATUS: 2, P.ADC_GAIN: 2, P.SHUTTER: 1, P.READOUT_STRIDE: 24, P.PIXEL_BIT_DEPTH: 16}
        self.roi = (0, 4, 1, 0, 3, 1)
        self.committed, self.calls, self.keep = False, [], []
        self.ids = (P.CameraID * 1)(P.CameraID(1, 1, b"PyLoN 400BR eXcelon", b"1234567"))

    def __getattr__(self, name):
        P, ct = self.P, self.ctypes

        def call(*args):
            self.calls.append(name)
            if name == "Picam_GetAvailableCameraIDs":
                _out(args[0]).contents = self.ids[0]
                _out(args[1]).value = 1
            elif name in ("Picam_OpenCamera", "Picam_OpenFirstCamera"):
                _out(args[-1]).value = 1234
            elif name == "Picam_GetCameraID":
                _out(args[1]).sensor_name, _out(args[1]).serial_number = b"PyLoN 400BR eXcelon", b"1234567"
            elif name in ("Picam_GetParameterFloatingPointValue", "Picam_ReadParameterFloatingPointValue"):
                _out(args[2]).value = self.floats[args[1]]
            elif name == "Picam_SetParameterFloatingPointValue":
                self.floats[args[1]], self.committed = args[2], False
            elif name in ("Picam_GetParameterIntegerValue", "Picam_ReadParameterIntegerValue"):
                _out(args[2]).value = self.ints[args[1]]
            elif name == "Picam_SetParameterIntegerValue":
                self.ints[args[1]], self.committed = args[2], False
            elif name == "Picam_GetParameterRoisValue":
                roi = P.Roi(*self.roi)
                rois = P.Rois(ct.pointer(roi), 1)
                self.keep += [roi, rois]
                _out(args[2]).contents = rois
            elif name == "Picam_SetParameterRoisValue":
                r = _out(args[2]).roi_array[0]
                self.roi = (r.x, r.width, r.x_binning, r.y, r.height, r.y_binning)
                self.committed = False
            elif name == "Picam_CommitParameters":
                self.committed = True
            elif name == "Picam_AreParametersCommitted":
                _out(args[1]).value = 1 if self.committed else 0
            elif name == "Picam_Acquire":
                assert self.committed                    # PICam acquires only with its settings sent
                buf = (ct.c_uint16 * 12)(*range(1, 13))
                self.keep.append(buf)
                _out(args[3]).initial_readout = ct.addressof(buf)
                _out(args[3]).readout_count = 1
            return 0
        return call


# ------------------------------------------------------------------ DAQ

DAQ_LAB = """
lab: DAQ bench
instruments:
  daq: { card: ni/daqmx, address: "nidaq://Dev1?terminal=rse", limits: { ao0: [-1, 1] } }
"""


def test_a_daq_card(tmp_path):
    daq = MODULES["nidaq"] = FakeNIDAQ()
    lab = build_lab(DAQ_LAB)
    assert daq.writes == []                                              # connecting puts nothing out
    gate = gate_for(lab, tmp_path, [True])
    assert lab.read("daq", "ai0") == 1.0
    assert daq.reads[-1] == ("Dev1/ai0", {"min_val": -10.0, "max_val": 10.0, "terminal_config": "rse"})
    assert gate.set("daq", "samples", 4).ok and lab.read("daq", "ai0") == pytest.approx(2.0)   # averaged
    r = gate.set("daq", "ao0", 0.2)
    assert r.action == "refused" and "safe value" in r.message           # unknown (no read-back) and ramped
    assert gate.set("daq", "ao0", 0).ok and daq.writes == [("Dev1/ao0", 0.0)]   # to safe: no question
    assert gate.set("daq", "ao0", 0.2).ok and daq.writes[-1] == ("Dev1/ao0", 0.2)
    assert gate.set("daq", "ao0", 5).action == "refused"                 # the lab's 1 V limit
    gate.set("daq", "integration", 0.01)
    assert lab.read("daq", "ctr0") == pytest.approx(5000.0)              # 50 edges in 10 ms
    assert "ctr0" not in lab.snapshot()["daq"]                           # counting only when asked
    assert lab.instrument("daq").backend.identity() == "National Instruments,USB-6001,1F2A3B4"
    assert discover_libraries()[0] == ("nidaq://Dev1", "National Instruments,USB-6001,1F2A3B4")


# ------------------------------------------------------------------ spectrometer, recorded with its wavelengths

SPEC_LAB = """
lab: Spectroscopy
instruments:
  spec: { card: ocean/spectrometer, address: "seabreeze://USB4F0001" }
"""


def test_a_spectrometer_run_keeps_its_wavelengths(tmp_path):
    MODULES["seabreeze"] = FakeSeabreeze()
    from labharness.agent.runtime import LabRuntime
    lab = build_lab(SPEC_LAB)
    rt = LabRuntime(lab, ScriptedApprover([True] * 4), tmp_path / "data", None)
    rt.report_kinds = {"scan"}
    assert "integration" not in lab.snapshot()["spec"]                   # unknown until Kelvin sets it
    assert "? → 50 ms" in rt.set("spec", "integration", 0.05) and MODULES["seabreeze"].Spectrometer.us == 50000
    assert "peak_wavelength" in rt.get("spec", "peak_wavelength") and "413.8 nm" in rt.get("spec", "peak_wavelength")
    out = rt.run("plan: spectra\nsweep: {spec.averages: {values: [1, 2]}}\nmeasure: [spec.spectrum]")
    assert "done" in out, out
    run = rt._run_dir(rt.runs_index(1)[0]["run_id"])
    from labharness import rundata
    wl = MODULES["seabreeze"].Spectrometer().wavelengths()
    xs = rundata.load(run).rows[0]["spec.spectrum.x"]               # each spectrum keeps its wavelengths
    assert xs[39] == pytest.approx(wl[39]) and xs[1] - xs[0] != pytest.approx(xs[-1] - xs[-2])   # not linear
    assert (run / "report.pdf").exists()


# ------------------------------------------------------------------ cameras: frames as files

CAM_LAB = """
lab: Imaging
instruments:
  cam: { card: basler/camera, address: "pylon://40012345" }
"""


def test_a_camera_run_keeps_each_frame_as_a_file(tmp_path):
    pylon = MODULES["pylon"] = FakePylon()
    from labharness.agent.runtime import LabRuntime
    lab = build_lab(CAM_LAB)
    rt = LabRuntime(lab, ScriptedApprover([True] * 4), tmp_path / "data", None)
    rt.report_kinds = {"scan"}
    assert pylon.grabs == 0 and "mean" not in lab.snapshot()["cam"]      # nothing grabbed to show the panel
    assert lab.read("cam", "exposure") == pytest.approx(0.01)
    out = rt.run("plan: images\nsweep: {cam.exposure: {values: [0.001, 0.002]}}\nmeasure: [cam.frame, cam.mean]")
    assert "done" in out, out
    run = rt._run_dir(rt.runs_index(1)[0]["run_id"])
    h5py = pytest.importorskip("h5py")
    with h5py.File(run / "run.h5", "r") as f:                           # the frames, packed into run.h5
        stack, means = f["frames/cam.frame"][()], f["points/cam.mean"][()]
    assert stack.shape[0] == 2 and not (run / "frames").exists()
    for k in range(2):
        assert means[k] == pytest.approx(float(stack[k].mean()))         # the mean of that very frame
    loose = tmp_path / "data" / "frames"                                 # frames read outside a run go here
    assert not list(loose.glob("*.npy"))                                 # moved into the run, none left behind
    assert "image" in rt.get("cam", "frame") and len(list(loose.glob("*.npy"))) == 1


def test_andor_cameras_through_andors_sdks(tmp_path):
    sdk3_cam = SimpleNamespace(ExposureTime=0.01, SensorTemperature=-0.5, SensorCooling=False, TemperatureStatus="Cooler Off",
                               CameraModel="ZYLA-5.5", SerialNumber="VSC-01234", closed=False,
                               acquire=lambda timeout: SimpleNamespace(image=[[1, 2], [3, 4]]))
    sdk3 = SimpleNamespace(AndorSDK3=lambda: SimpleNamespace(GetCamera=lambda i: sdk3_cam, DeviceCount=1))
    calls = []

    class Atmcd:
        def __getattr__(self, name):
            def f(*args):
                calls.append((name,) + args)
                if name == "GetAcquiredData":
                    return 20002, list(range(args[0]))
                return {"GetDetector": (20002, 3, 2), "GetAcquisitionTimings": (20002, 0.1, 0.1, 0.1),
                        "GetTemperatureF": (20036, -60.0), "IsCoolerOn": (20002, 1),
                        "GetHeadModel": (20002, "DU897_BV"), "GetCameraSerialNumber": (20002, 12345)}.get(name, 20002)
            return f

    sdk2 = SimpleNamespace(atmcd=Atmcd)
    MODULES["andor"] = sdk3
    lab = build_lab("lab: A\ninstruments:\n  zyla: {card: andor/scmos, address: 'andor://0?sdk=3'}\n")
    assert lab.read("zyla", "exposure") == 0.01 and lab.read("zyla", "temperature") == -0.5
    assert "image" in lab.instrument("zyla").spec("frame").show(lab.read("zyla", "frame"))
    assert lab.instrument("zyla").backend.identity() == "Andor,ZYLA-5.5,VSC-01234"
    MODULES["andor"] = sdk2
    lab2 = build_lab("lab: B\ninstruments:\n  ixon: {card: andor/ccd, address: 'andor://0?sdk=2'}\n")
    assert [c[0] for c in calls] == ["Initialize", "GetDetector"]          # opening sets nothing
    assert lab2.read("ixon", "temperature") == -60.0 and lab2.read("ixon", "temperature_status") == "stabilized"
    assert lab2.read("ixon", "spectrum") == [0.0, 1.0, 2.0]                # full vertical binning: one per column
    assert ("SetReadMode", 0) in calls
    frames.load_rows(lab2.read("ixon", "frame"))
    assert ("SetReadMode", 4) in calls and ("SetImage", 1, 1, 1, 3, 1, 2) in calls
    lab2.close()
    assert ("ShutDown",) not in calls                                      # cooled: not shut down cold


# ------------------------------------------------------------------ nanopositioners

ANC_LAB = """
lab: Cryostat stage
instruments:
  x: { card: attocube/anc300, address: "anc300://COM5", vars: { axis: "1" }, limits: { offset: [0, 40] } }
  y: { card: attocube/anc300, address: "anc300://COM5", vars: { axis: "2" } }
  fine: { card: attocube/anc350, address: "anc350://0", vars: { axis: "0" } }
"""


def test_attocube_positioners(tmp_path):
    anc300, anc350 = FakeANC300(), FakeANC350()
    MODULES.update(anc300=anc300.Link, anc350=anc350)
    lab = build_lab(ANC_LAB)
    assert anc300.opened == ["COM5"] and anc300.sent == []              # two axes, one link; nothing sent yet
    gate = gate_for(lab, tmp_path, [True, True])
    assert lab.read("x", "mode") == "gnd" and lab.read("y", "mode") == "stp"   # as they were
    assert lab.read("x", "voltage") == 30.0 and lab.read("x", "frequency") == 1000.0
    assert gate.set("x", "steps", 25).ok and anc300.sent[-2:] == ["stepu 1 25", "stepw 1"]
    assert gate.set("x", "steps", 15).ok and anc300.sent[-2:] == ["stepd 1 10", "stepw 1"]
    assert lab.read("x", "steps") == 15
    assert gate.set("x", "offset", 60).action == "refused"               # the lab's 40 V
    assert gate.set("x", "offset", 2).ok and anc300.offset[1] == 2.0      # asked, then ramped
    assert lab.instrument("x").backend.identity() == ("attocube ANC300 controller version 1.1.0-1304 2013-10-17 08:16, "
                                                      "serial ANC300B-C-1234")   # its ver: there is no *IDN?
    assert lab.health_check(["x"])[0].ok
    assert lab.read("fine", "position") == pytest.approx(1.0)              # um
    assert gate.set("fine", "position", 5).ok and ("move_to", 0, pytest.approx(5e-6)) in anc350.calls
    lab.close()
    assert anc300.closed == 1                                            # the shared link, once


def test_the_anc300_command_line():
    """The real link's parsing, on canned bytes: telnet options and the echo dropped, OK and ERROR read."""
    from labharness.positioners import Link, _value

    class FakeLink(Link):
        def __init__(self, chunks):
            self.buf, self.timeout, self.chunks, self.sent = b"", 1.0, list(chunks), []

        def _send(self, line):
            self.sent.append(line)

        def _chunk(self):
            return self.chunks.pop(0) if self.chunks else b""

    link = FakeLink([b"\xff\xfb\x01getv 1\r\nvoltage = 30.000000 V\r\n", b"OK\r\n"])
    assert _value(link.command("getv 1")) == "30.000000"
    link = FakeLink([b"Axis not in computer control mode\r\nERROR\r\n"])
    with pytest.raises(Exception, match="computer control"):
        link.command("stepu 1 10")


def test_an_andor_spectrograph_through_andors_dll(tmp_path):
    dll = MODULES["shamrock"] = FakeShamrockDLL()
    lab = build_lab("lab: S\ninstruments:\n  spg: {card: andor/shamrock, address: 'shamrock://0'}\n")
    assert dll.calls == ["ShamrockInitialize", "ShamrockGetNumberDevices"]   # opening moves nothing
    gate = gate_for(lab, tmp_path)
    assert lab.read("spg", "wavelength") == 500.0 and lab.read("spg", "grating") == 1
    assert gate.set("spg", "wavelength", 632.8).ok and dll.wl == pytest.approx(632.8, abs=1e-3)
    assert gate.set("spg", "shutter", True).ok and dll.shutter == 1 and lab.read("spg", "shutter") is True
    assert lab.read("spg", "slit_input") == 100.0
    assert lab.instrument("spg").backend.identity() == "Andor,Shamrock,SR-0123"
    lab.close()
    assert dll.calls[-1] == "ShamrockClose"


def test_a_pylon_camera_through_picam(tmp_path):
    from labharness import princeton
    dll = MODULES["picam"] = FakePicamDLL()
    lab = build_lab("lab: P\ninstruments:\n  ccd: {card: princeton/picam, address: 'picam://1234567'}\n")
    assert [c for c in dll.calls if "Set" in c or "Commit" in c] == []   # opening sets nothing
    gate = gate_for(lab, tmp_path)
    assert lab.read("ccd", "exposure") == pytest.approx(0.1) and lab.read("ccd", "temperature") == -119.5
    assert lab.read("ccd", "temperature_status") == "locked" and lab.read("ccd", "adc_gain") == "medium"
    assert lab.read("ccd", "roi") == "0,4,1,0,3,1"
    assert lab.read("ccd", "spectrum") == [15.0, 18.0, 21.0, 24.0]        # each column's three rows summed
    assert lab.read("ccd", "max") == 12.0
    assert gate.set("ccd", "exposure", 2).ok and dll.floats[princeton.EXPOSURE] == 2000.0 and dll.committed
    assert gate.set("ccd", "roi", "0,4,1,0,3,3").ok and dll.roi == (0, 4, 1, 0, 3, 3)   # the chip binned to a row
    assert lab.instrument("ccd").backend.identity() == "Princeton Instruments,PyLoN 400BR eXcelon,1234567"
    assert "image" in lab.instrument("ccd").spec("frame").show(lab.read("ccd", "frame"))
    lab.close()
    assert dll.calls[-2:] == ["Picam_CloseCamera", "Picam_UninitializeLibrary"]


class Port:
    """A serial or network port to a fake text instrument: answer(cmd) returns its reply lines."""

    def __init__(self, answer, echo=False):
        self.answer, self.echo, self.sent, self.out = answer, echo, [], []
        self.timeout = self.read_termination = self.write_termination = None

    def write(self, cmd):
        self.sent.append(cmd)
        self.out += ([cmd] if self.echo else []) + self.answer(cmd)

    def query(self, cmd, delay=None):
        self.write(cmd)
        return self.read()

    def read(self):
        if not self.out:
            raise TimeoutError("VI_ERROR_TMO")
        return self.out.pop(0)

    def close(self):
        pass


class Ports:
    def __init__(self, ports):
        self.ports = ports

    def list_resources(self, query="?*::INSTR"):
        return tuple(self.ports)

    def open_resource(self, name):
        return self.ports[name]


def test_text_monochromator_and_piezo_controllers(tmp_path):
    acton = {"nm": 500.0, "grating": 1}

    def acton_answer(cmd):   # the SpectraPro echoes on RS-232: the echo and the answer share one line, ending ok
        if cmd.endswith(" GOTO"):
            acton["nm"] = float(cmd.split()[0])
            return [cmd + "  ok"]
        if cmd == "?NM":
            return ["?NM  %.2f nm  ok" % acton["nm"]]
        if cmd == "?GRATING":
            return ["?GRATING  %d  ok" % acton["grating"]]
        if cmd.endswith(" GRATING"):
            acton["grating"] = int(cmd.split()[0])
            return [cmd + "  ok"]
        return [cmd + "  ok"]

    mdt = {"X": 0.0}

    def mdt_answer(cmd):   # echoed (Port does that), then the bracketed answer
        if cmd.endswith("R?"):
            return ["[ %.2f]" % mdt[cmd[0]]]
        if cmd[1] == "V":
            mdt[cmd[0]] = float(cmd[2:])
        return []

    pi = {"tgt": 10.0, "svo": 1}

    def pi_answer(cmd):
        if cmd == "*IDN?":
            return ["(c)2015 Physik Instrumente (PI) GmbH & Co. KG, E-727, 0115032209, 01.004"]
        if cmd.startswith("MOV "):
            pi["tgt"] = float(cmd.split()[2])
            return []
        if cmd in ("MOV? 1", "POS? 1"):
            return ["1=%.4f" % pi["tgt"]]
        if cmd == "VEL? 1":
            return ["1=100"]
        if cmd in ("ONT? 1", "SVO? 1"):
            return ["1=1"]
        if cmd == "ERR?":
            return ["0"]
        return []

    ports = Ports({"ASRL4::INSTR": Port(acton_answer), "ASRL6::INSTR": Port(mdt_answer, echo=True),
                   "TCPIP0::10.0.0.5::50000::SOCKET": Port(pi_answer)})
    set_resource_manager(ports)
    lab = build_lab("""
lab: Optics
instruments:
  mono: { card: princeton/spectrapro, address: "ASRL4::INSTR" }
  piezo: { card: thorlabs/mdt69xa, address: "ASRL6::INSTR", limits: { x: [0, 75] } }
  scan: { card: pi/gcs-piezo, address: "TCPIP0::10.0.0.5::50000::SOCKET", limits: { position: [0, 100] } }
""")
    assert all(p.sent == [] for p in ports.ports.values())               # connecting sends nothing
    gate = gate_for(lab, tmp_path, [True])
    assert lab.read("mono", "wavelength") == 500.0 and lab.read("mono", "grating") == 1
    assert gate.set("mono", "wavelength", 632.8).ok and acton["nm"] == 632.8
    assert gate.set("mono", "grating", 2).ok and lab.read("mono", "grating") == 2
    assert gate.raw("mono", "8 INIT-GRATING").action == "refused"        # rewrites its start-up memory
    assert lab.read("piezo", "x") == 0.0
    assert gate.set("piezo", "x", 100).action == "refused"               # the lab's 75 V
    assert gate.set("piezo", "x", 5).ok and mdt["X"] == 5.0              # asked, then ramped up
    assert gate.set("scan", "position", 42.5).ok and lab.read("scan", "position_actual") == 42.5
    assert all(h.ok for h in lab.health_check(["scan"]))


def test_scans_find_them_through_their_libraries():
    MODULES.update(nidaq=FakeNIDAQ(), picam=FakePicamDLL(), shamrock=FakeShamrockDLL(), seabreeze=FakeSeabreeze(),
                   pylon=FakePylon(), anc350=FakeANC350())
    reg = builtin_registry()
    found = dict(discover_libraries())
    assert {a: match_card(i, reg).id for a, i in found.items()} == {
        "nidaq://Dev1": "ni/daqmx", "picam://1234567": "princeton/picam", "shamrock://0": "andor/shamrock",
        "seabreeze://USB4F0001": "ocean/spectrometer", "pylon://40012345": "basler/camera",
        "anc350://0": "attocube/anc350"}
