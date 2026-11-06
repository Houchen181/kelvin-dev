"""PICam in a helper process of its own (princeton.py, data/bridge/kelvin_picam.py), as the field report of 2026-11-06
asked: a PICam that couldn't start is let go, said in words and not started again in that process; a camera another
program has open says so, and which program; a call that hangs ends the helper, and connecting again starts a fresh
one; closing returns in time whatever the helper does; a scan lists the cameras through a helper too. PICam is a fake
(test_more_instruments.FakePicamDLL): in Kelvin's own process, or in a stand-in for the helper started as a process."""
import functools
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from labharness import builtin_registry, frames, princeton
from labharness.adapters import AdapterError
from labharness.drivers import MODULES
from labharness.homelab import build_lab

from test_more_instruments import FakePicamDLL, _out

ROOT = Path(__file__).resolve().parent.parent
PYTHON = getattr(sys, "_base_executable", "") or sys.executable   # as princeton starts one (a venv's starts another)
LAB = "lab: P\ninstruments:\n  ccd: {card: princeton/picam, address: 'picam://1234567'}\n"
CLOSE_BUDGET = princeton.CLOSE_S + princeton.KILL_S   # what close() may take at most, as Kelvin has it
HOLDER = princeton.holder   # the tests' own fixture keeps it from looking at this computer's programs

# a stand-in for PICam's helper: the real one's start (kelvin_picam.py) and requests (princeton.serve), with a fake
# Picam.dll whose calls named hang=<PICam function> never return (orphan=<s>: its ORPHAN_S). At its end it says
# PICam's last two calls.
FAKE = r'''
import json, os, sys, time
out = os.fdopen(os.dup(1), "wb")
os.dup2(2, 1)
sys.stdout = sys.stderr
inp = os.fdopen(0, "rb")
first = json.loads(inp.readline().decode("utf-8") or "{}")
sys.path[:] = list(dict.fromkeys([p for p in first.get("path") or [] if p] + sys.path))
from labharness import princeton
from test_more_instruments import FakePicamDLL
ARGS = dict(a.split("=", 1) for a in sys.argv[1:])
HANG = {a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("hang=")}
princeton.ORPHAN_S = float(ARGS.get("orphan", princeton.ORPHAN_S))


class Picam(FakePicamDLL):
    def __getattr__(self, name):
        if name in HANG:
            def call(*args):
                time.sleep(3600)
                return 0
            return call
        return super().__getattr__(name)


dll = Picam()
try:
    princeton.serve(inp, out, dll)
finally:
    print("calls: " + ",".join(dll.calls[-2:]))
'''


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(frames, "FOLDER", tmp_path / "frames")
    monkeypatch.setattr(princeton, "holder", lambda: ("", []))   # not this computer's own programs
    monkeypatch.setattr(princeton, "_users", [0])   # cameras open: none, whatever a test before left
    yield
    MODULES.clear()


def asking(helper, requests):
    """Waits until the helper has had this many requests, the last still unanswered (a call in PICam)."""
    deadline = time.monotonic() + 10
    while not (helper.next_id >= requests and helper.lock.locked()) and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)


def fake_helper(tmp_path, monkeypatch, *args):
    """Kelvin starts the stand-in instead of PICam's helper, as it starts the real one (its import path first)."""
    script = tmp_path / "fake_picam_helper.py"
    script.write_text(FAKE, encoding="utf-8")
    monkeypatch.setattr(princeton, "helper_command", lambda: [PYTHON, "-E", "-s", "-u", str(script)] + list(args))


@functools.lru_cache(maxsize=None)
def card():
    return builtin_registry().get("princeton/picam")


class Unstartable(FakePicamDLL):
    """PICam as on the lab PC after the crash: its start fails with error 4, and it can't name errors before it has
    started."""

    def __getattr__(self, name):
        if name == "Picam_InitializeLibrary":
            return lambda: self.calls.append(name) or 4
        if name == "Picam_GetEnumerationString":
            return lambda kind, value, out: 1   # PicamError_LibraryNotInitialized
        return super().__getattr__(name)


def test_a_start_that_failed_is_said_in_words_let_go_and_not_tried_again():
    dll = Unstartable()
    with pytest.raises(AdapterError) as failed:
        princeton.PicamCamera("camera", card(), "picam://X045418024", module=dll)
    assert str(failed.value) == (
        "camera: PICam couldn't start: an unexpected error inside PICam (PicamError_UnexpectedError, code 4). "
        "Another program may be holding PICam: quit other Kelvin or LightField instances, then try again. A Kelvin "
        "whose window is closed may still be running: end it in Task Manager")
    assert dll.calls == ["Picam_InitializeLibrary", "Picam_UninitializeLibrary"]   # let go at once
    assert princeton._users[0] == 0
    with pytest.raises(AdapterError) as again:   # in the same process, PICam isn't started again
        princeton.PicamCamera("camera", card(), "picam://X045418024", module=dll)
    assert "PicamError_UnexpectedError, code 4) (not tried again in the same process)" in str(again.value)
    assert princeton.discover_picam(dll) == []   # nor by a scan
    assert dll.calls.count("Picam_InitializeLibrary") == 1
    assert "PicamError_UnexpectedError" in princeton.LAST["picam"]
    # PICam names what it can itself; the header's names are for what it can't
    assert princeton.explain(FakePicamDLL(), "Picam_Acquire", 32) == (
        "PICam couldn't acquire: the acquisition timed out (PicamError_TimeOutOccurred, code 32). The camera didn't "
        "deliver in time: check its cable and that it is cooled and connected")
    assert princeton.explain(FakePicamDLL(), "Picam_Acquire", 999) == "PICam couldn't acquire: error 999"


class Held(FakePicamDLL):
    """PICam with no camera free: the field report's PyLoN-IR is open in another program."""

    def __init__(self):
        super().__init__()
        self.held = (self.P.CameraID * 1)(self.P.CameraID(1, 6, b"PyLoN-IR 1024-1.7", b"X045418024"))

    def __getattr__(self, name):
        if name == "Picam_GetAvailableCameraIDs":
            return lambda ids, n: 0   # none
        if name == "Picam_GetUnavailableCameraIDs":
            def call(ids, n):
                _out(ids).contents, _out(n).value = self.held[0], 1
                return 0
            return call
        return super().__getattr__(name)


def test_a_camera_open_in_another_program_says_so_and_which(monkeypatch):
    def connect():
        with pytest.raises(AdapterError) as held:
            princeton.PicamCamera("camera", card(), "picam://X045418024", module=Held())
        return str(held.value)

    monkeypatch.setattr(princeton, "holder", lambda: ("kelvin", [15668]))
    assert connect() == (
        "camera: Princeton Instruments camera X045418024 (PyLoN-IR 1024-1.7) is open in another program (another "
        "Kelvin, process 15668): quit it, then connect the camera again. A Kelvin whose window is closed may still be "
        "running: end it in Task Manager")
    monkeypatch.setattr(princeton, "holder", lambda: ("lightfield", []))
    assert "is open in another program (LightField): close LightField for Kelvin to use it through PICam" in connect()
    monkeypatch.setattr(princeton, "holder", lambda: ("", []))
    text = connect()
    assert "is open in another program (LightField?): close it" in text and "PICam sees" not in text
    assert princeton._users[0] == 0
    assert princeton.discover_picam(Held()) == []
    assert princeton.LAST["picam"].endswith("PyLoN-IR 1024-1.7 (X045418024) is open in another program (LightField?): "
                                            "close it for Kelvin to use it through PICam, or use it through LightField "
                                            "(Teledyne LightField, lightfield://<experiment>)")
    with pytest.raises(AdapterError) as missing:   # one PICam doesn't list at all
        princeton.PicamCamera("camera", card(), "picam://1111111", module=Held())
    assert str(missing.value) == "camera: no Princeton Instruments camera 1111111 (PICam sees: none)"


def fake_psutil(running):
    """psutil, for a computer running these processes: {pid: (name, parent's pid)}."""
    class Process:
        def __init__(self, pid):
            self.pid, self.info = pid, {"pid": pid, "name": running[pid][0], "ppid": running[pid][1]}

        def children(self, recursive=False):
            kids = [Process(p) for p, (_, ppid) in running.items() if ppid == self.pid]
            return kids + [g for k in kids for g in k.children(True)] if recursive else kids
    return SimpleNamespace(Process=Process, process_iter=lambda attrs=None: [Process(p) for p in running],
                           Error=OSError)


def test_who_holds_picam(monkeypatch, tmp_path):
    holder = HOLDER
    me = os.getpid()
    running = {me: ("python.exe", 1), 4300: ("Kelvin.exe", me),          # this Kelvin and its own helper
               4242: ("Kelvin.exe", 1), 4243: ("Kelvin.exe", 4242),      # another Kelvin, and its helper
               5000: ("kelvin-hub.exe", 1), 5001: ("Kelvin-0.3.44-setup.exe", 1),   # the hub, an installer
               6000: ("explorer.exe", 1)}
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil(running))
    monkeypatch.setattr(princeton, "KELVIN_HOME", tmp_path)
    assert holder() == ("kelvin", [4242])
    (tmp_path / "app.json").write_text(json.dumps({"port": 50107, "pid": 7000}), encoding="utf-8")
    running[7000] = ("python.exe", 1)   # a Kelvin run from source, named by the Kelvin folder's app.json
    assert holder() == ("kelvin", [7000, 4242])
    (tmp_path / "app.json").write_text(json.dumps({"port": 50109, "pid": me}), encoding="utf-8")   # this one's own
    del running[4242], running[4243]
    assert holder() == ("", [])
    running[8000] = ("LightField.exe", 1)
    assert holder() == ("lightfield", [])
    monkeypatch.setattr(princeton, "holder", holder)
    assert princeton.advice("picam") == (". Another program may be holding PICam: LightField is running; close it, "
                                         "then try again")


def test_a_hung_call_ends_the_helper_and_connecting_again_starts_a_fresh_one(tmp_path, monkeypatch):
    fake_helper(tmp_path, monkeypatch, "hang=Picam_ReadParameterFloatingPointValue")   # the sensor's temperature
    monkeypatch.setattr(princeton, "CALL_S", 1.0)
    lab = build_lab(LAB)
    cam = lab.instrument("ccd").backend
    first = cam.helper.proc
    assert lab.read("ccd", "exposure") == pytest.approx(0.1) and princeton._users[0] == 1
    assert lab.read("ccd", "spectrum") == [15.0, 18.0, 21.0, 24.0]   # a frame across the pipe: its bytes, shape, dtype
    assert lab.read("ccd", "max") == 12.0 and lab.read("ccd", "roi") == "0,4,1,0,3,1"
    started = time.monotonic()
    with pytest.raises(AdapterError) as hung:
        cam.get_setting("temperature")
    assert time.monotonic() - started < 4
    assert str(hung.value).startswith(
        "ccd: PICam didn't answer within 1 s (reading temperature): Kelvin ended its helper, so nothing in Kelvin "
        "holds PICam now. Switch the camera off and on in the Lab panel to start PICam afresh")
    assert first.poll() is not None and princeton._users[0] == 0   # the helper is gone, and PICam with it
    with pytest.raises(AdapterError) as after:
        cam.get_setting("exposure")
    assert "not connected any more: Kelvin ended PICam's helper (it didn't answer within 1 s" in str(after.value)
    assert cam.quiet_close   # nothing is left to send it: off closes it, on connects it with a fresh helper
    lab.switch("ccd", False)
    lab.switch("ccd", True)
    fresh = lab.instrument("ccd").backend
    assert fresh is not cam and fresh.helper.proc.pid != first.pid and not fresh.quiet_close
    assert lab.read("ccd", "exposure") == pytest.approx(0.1) and princeton._users[0] == 1
    lab.close()
    assert fresh.helper.proc.wait(5) == 0 and princeton._users[0] == 0   # closed: camera, then PICam, then the helper


def test_a_start_that_hangs_ends_the_helper_and_says_who_may_hold_picam(tmp_path, monkeypatch):
    """The lab PC's case: behind a PICam left broken, starting it waited 446 s, then failed. Kelvin waits START_S."""
    fake_helper(tmp_path, monkeypatch, "hang=Picam_InitializeLibrary")
    monkeypatch.setattr(princeton, "START_S", 1.0)
    monkeypatch.setattr(princeton, "holder", lambda: ("kelvin", [14696]))
    helpers = []
    real = princeton.Helper
    monkeypatch.setattr(princeton, "Helper", lambda: helpers.append(real()) or helpers[-1])
    with pytest.raises(AdapterError) as hung:
        princeton.PicamCamera("camera", card(), "picam://X045418024")
    assert str(hung.value) == (
        "camera: PICam didn't answer within 1 s (starting PICam): Kelvin ended its helper, so nothing in Kelvin holds "
        "PICam now. Another program may be holding PICam: another Kelvin is running (process 14696); quit it, then try "
        "again. A Kelvin whose window is closed may still be running: end it in Task Manager")
    assert helpers[0].proc.poll() is not None and princeton._users[0] == 0
    monkeypatch.setattr(princeton, "installed", lambda: True)
    assert princeton.discover_picam() == [] and helpers[1].proc.poll() is not None   # a scan waits no longer
    assert princeton.LAST["picam"].startswith("PICam (Princeton Instruments cameras): PICam didn't answer within 1 s "
                                              "(listing the cameras)")


def test_closing_returns_in_time_whatever_the_helper_does(tmp_path, monkeypatch):
    assert CLOSE_BUDGET <= 5.0   # Kelvin closes every instrument when it quits, each within its time
    fake_helper(tmp_path, monkeypatch, "hang=Picam_CloseCamera", "hang=Picam_Acquire")
    monkeypatch.setattr(princeton, "CLOSE_S", 1.5)
    cam = princeton.PicamCamera("ccd", card(), "picam://1234567")
    started = time.monotonic()
    cam.close()   # the helper never answers: it is ended
    took = time.monotonic() - started
    assert 1.4 < took < 1.5 + princeton.KILL_S + 1.0 and cam.helper.proc.poll() is not None
    assert "didn't close within 1.5 s" in cam.helper.gone and princeton._users[0] == 0
    cam = princeton.PicamCamera("ccd", card(), "picam://1234567")   # a frame being taken isn't waited for either
    errors = []

    def snap():
        try:
            cam.snap()
        except AdapterError as e:
            errors.append(str(e))
    taking = threading.Thread(target=snap)
    taking.start()
    asking(cam.helper, 4)   # start, open, its exposure, the frame
    started = time.monotonic()
    cam.close()
    assert time.monotonic() - started < 1.5 + princeton.KILL_S + 1.0 and cam.helper.proc.poll() is not None
    taking.join(5)
    assert errors and errors[0].startswith("ccd: PICam's helper stopped while taking a frame")


def said(helper, text, wait=5.0):
    deadline = time.monotonic() + wait
    while text not in helper.errors and time.monotonic() < deadline:
        time.sleep(0.05)
    return text in helper.errors


def test_a_helper_whose_kelvin_has_gone_lets_picam_go_by_itself(tmp_path, monkeypatch):
    """Kelvin quit or crashed, its end of the pipe closed: the helper closes the camera and lets PICam go, as Kelvin's
    close would; one stuck in a PICam call (an acquisition) exits anyway, ORPHAN_S later."""
    fake_helper(tmp_path, monkeypatch, "hang=Picam_Acquire", "orphan=2")
    helper = princeton.Helper()
    helper.request("start", 30)
    helper.request("open", 30, serial="1234567")
    helper.proc.stdin.close()   # as when Kelvin's process ends
    assert helper.proc.wait(10) == 0 and said(helper, "calls: Picam_CloseCamera,Picam_UninitializeLibrary")
    helper = princeton.Helper()
    helper.request("start", 30)
    helper.request("open", 30, serial="1234567")
    errors = []

    def snap():
        try:
            helper.request("snap", 30)
        except AdapterError as e:
            errors.append(str(e))
    taking = threading.Thread(target=snap)
    taking.start()
    asking(helper, 3)   # start, open, the frame
    helper.proc.stdin.close()
    started = time.monotonic()
    assert helper.proc.wait(10) == 3 and time.monotonic() - started < 6   # ORPHAN_S (2 s here), not the hour
    taking.join(5)
    assert errors and errors[0].startswith("PICam's helper stopped while taking a frame")


def test_a_scan_lists_the_cameras_through_a_helper_of_its_own(tmp_path, monkeypatch):
    fake_helper(tmp_path, monkeypatch)
    helpers = []
    real = princeton.Helper
    monkeypatch.setattr(princeton, "Helper", lambda: helpers.append(real()) or helpers[-1])
    monkeypatch.setattr(princeton, "installed", lambda: True)
    assert princeton.discover_picam() == [("picam://1234567", "Princeton Instruments,PyLoN 400BR eXcelon,1234567")]
    assert princeton.LAST["picam"].endswith("PyLoN 400BR eXcelon (1234567) found")
    assert len(helpers) == 1 and helpers[0].proc.wait(5) == 0   # it listed, let PICam go and went
    monkeypatch.setattr(princeton, "installed", lambda: False)   # no PICam on this computer: no helper at all
    assert princeton.discover_picam() == [] and len(helpers) == 1


def test_the_app_starts_picams_helper_with_its_bridge_and_brings_it(monkeypatch):
    """Kelvin.exe runs the helper's script with --bridge (packaging/kelvin_app.py), as its other helpers; the build
    (kelvin.spec) and the package (pyproject.toml) bring the script. A ping loads no Picam.dll."""
    with monkeypatch.context() as m:
        m.setattr(sys, "frozen", True, raising=False)
        assert princeton.helper_command() == [sys.executable, "--bridge", str(princeton.HELPER)]
    assert princeton.HELPER.is_file() and princeton.self_test()   # what the release build's self-test can ask
    lines = [{"path": [str(ROOT / "src")] + sys.path}, {"id": 1, "op": "ping"}, {"id": 2, "op": "close"}]
    out = subprocess.run([PYTHON, "-I", str(ROOT / "packaging" / "kelvin_app.py"), "--bridge", str(princeton.HELPER)],
                         input="".join(json.dumps(x) + "\n" for x in lines).encode(), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, timeout=60)
    replies = [json.loads(line) for line in out.stdout.splitlines()]
    assert replies[0]["ok"] and replies[0]["value"]["pid"] and replies[1] == {"id": 2, "ok": True, "value": None}
    assert out.returncode == 0, out.stderr
    spec = (ROOT / "packaging" / "kelvin.spec").read_text(encoding="utf-8")
    assert '"data" / "bridge" / "kelvin_picam.py"), "labharness/data/bridge")' in spec
    assert '"data/bridge/*.py"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
