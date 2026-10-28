"""Ordinary cameras: a USB webcam or microscope camera (UVC, through OpenCV) and a GenICam industrial camera (through
Harvesters and its maker's GenTL producer). Kelvin installs OpenCV or Harvesters when one is needed; here they are
stood in for, as their documentation describes them."""
import contextlib
import types

import pytest

from labharness import EventLog, SafetyGate, ScriptedApprover, drivers
from labharness.adapters import AdapterError
from labharness.virtual_lab import build_lab

np = pytest.importorskip("numpy")


class Capture:
    """cv2.VideoCapture for camera 0: 640 x 480, colour frames."""

    def __init__(self, index, api):
        self.index, self.api = index, api
        self.props = {3: 640.0, 4: 480.0, 15: -5.0, 14: 0.0, 10: 128.0}

    def isOpened(self):
        return self.index == 0

    def get(self, prop):
        return self.props.get(prop, 0.0)

    def set(self, prop, value):
        self.props[prop] = value
        return True

    def grab(self):
        return True

    def read(self):
        frame = np.zeros((4, 6, 3), dtype=np.uint8)
        frame[:, :, 1] = 90
        return True, frame

    def release(self):
        pass


cv2 = types.SimpleNamespace(VideoCapture=Capture, CAP_DSHOW=700, CAP_ANY=0, CAP_PROP_EXPOSURE=15, CAP_PROP_GAIN=14,
                            CAP_PROP_BRIGHTNESS=10, CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4,
                            CAP_PROP_AUTO_EXPOSURE=21, COLOR_BGR2GRAY=6,
                            cvtColor=lambda image, code: image.mean(axis=2).astype(np.uint8))


class Node:
    def __init__(self, value):
        self.value = value


class Acquirer:
    def __init__(self):
        self.remote_device = types.SimpleNamespace(node_map=types.SimpleNamespace(
            ExposureTime=Node(10000.0), Gain=Node(1.5), Width=Node(6), Height=Node(4)))
        self.running = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    @contextlib.contextmanager
    def fetch(self, timeout):
        assert self.running
        component = types.SimpleNamespace(data=np.arange(24, dtype=np.uint16), width=6, height=4)
        yield types.SimpleNamespace(payload=types.SimpleNamespace(components=[component]))

    def destroy(self):
        pass


class Harvester:
    def __init__(self):
        self.files, self.device_info_list = [], []

    def add_file(self, path):
        self.files.append(path)

    def update(self):
        self.device_info_list = [types.SimpleNamespace(serial_number="20123456", vendor="FLIR",
                                                       model="Blackfly S BFS-U3-16S2M")]

    def create(self, index):
        return Acquirer()

    def reset(self):
        pass


def lab_with(address):
    return build_lab('lab: Microscope\ninstruments:\n  cam: { card: %s, address: "%s" }\n'
                     % ("generic/usb-camera" if address.startswith("uvc") else "generic/genicam-camera", address))


def test_a_usb_microscope_camera(monkeypatch, tmp_path):
    monkeypatch.setitem(drivers.MODULES, "uvc", cv2)
    lab = lab_with("uvc://0")
    try:
        assert lab.read("cam", "mean") == pytest.approx(30)               # grey: the colours averaged
        assert lab.read("cam", "width") == 640 and lab.read("cam", "exposure") == -5
        gate = SafetyGate(lab, ScriptedApprover([]), EventLog(tmp_path / "events.jsonl", lab.clock))
        assert gate.set("cam", "exposure", -7).ok and lab.read("cam", "exposure") == -7
        assert lab.instruments["cam"].backend.cap.props[21] == 0.25          # automatic exposure off, so it holds
    finally:
        lab.close()
    with pytest.raises(AdapterError, match="no USB camera 3"):
        lab_with("uvc://3")


def test_a_genicam_camera_through_its_makers_producer(monkeypatch, tmp_path):
    monkeypatch.setitem(drivers.MODULES, "genicam", Harvester)
    lab = lab_with("genicam://20123456?cti=C:/Program Files/FLIR/Spinnaker/FLIR_GenTL.cti")
    try:
        assert lab.read("cam", "exposure") == pytest.approx(0.01) and lab.read("cam", "gain") == pytest.approx(1.5)
        assert lab.read("cam", "mean") == pytest.approx(11.5)
        gate = SafetyGate(lab, ScriptedApprover([]), EventLog(tmp_path / "events.jsonl", lab.clock))
        assert gate.set("cam", "exposure", 0.02).ok
        assert lab.instruments["cam"].backend.ia.remote_device.node_map.ExposureTime.value == pytest.approx(20000)
        assert lab.instruments["cam"].backend.identity() == "FLIR,Blackfly S BFS-U3-16S2M,20123456"
    finally:
        lab.close()
    with pytest.raises(AdapterError, match="no GenICam camera 999"):
        lab_with("genicam://999?cti=C:/x.cti")
    monkeypatch.delenv("GENICAM_GENTL64_PATH", raising=False)
    monkeypatch.delenv("GENICAM_GENTL32_PATH", raising=False)
    with pytest.raises(AdapterError, match="no GenTL producer"):
        lab_with("genicam://")


def test_without_their_packages_kelvin_says_how_it_installs_them(monkeypatch):
    import builtins
    real = builtins.__import__

    def without(name, *args, **kwargs):
        if name in ("cv2", "harvesters.core"):
            raise ImportError("No module named %r" % name)
        return real(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without)
    with pytest.raises(AdapterError, match=r'packages \["opencv-python-headless"\]'):
        lab_with("uvc://0")
    with pytest.raises(AdapterError, match=r'packages \["harvesters"\]'):
        lab_with("genicam://1?cti=C:/x.cti")
