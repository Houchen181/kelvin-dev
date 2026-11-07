"""A maker's call that never returns (an SDK's capture, a board that stops answering), or a maker's library whose
listing never returns, never holds the lab for good (review of 0.3.45, 3.2): the call ends its connection, and
connecting again makes a fresh one; an Andor acquisition is waited for its exposure and readout, then aborted; QICK's
calls time out; a stuck listing is left out of the scan, and said."""
import threading
import time
from types import SimpleNamespace

import pytest

from labharness.adapters import AdapterError
from labharness.clock import SimClock
from labharness.drivers import MODULES, DriverBackend
from labharness.homelab import build_lab, builtin_registry
from labharness.lab import Lab
from labharness.labconfig import load_lab_config


def wait_for(check, within=5.0):
    end = time.monotonic() + within
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


class Stuck(DriverBackend):
    """A maker's driver one of whose calls never returns until released."""

    def __init__(self, name, card, address, release):
        super().__init__(name, card, address)
        self.release, self.closed, self.calls = release, False, 0

    def get(self, path):
        self.calls += 1
        return self.bounded(0.3, "its maker's read", lambda: self.release.wait(30) and 1.0)

    def identity(self):
        return "Fake,Stuck,1"

    def close(self):
        self.closed = True


def test_a_makers_call_that_never_returns_ends_its_connection_and_connecting_again_makes_a_fresh_one():
    release = threading.Event()
    made = []

    def factory(name, card, address):
        made.append(Stuck(name, card, address, release))
        return made[-1]
    registry = builtin_registry()
    lab = Lab(load_lab_config({"lab": "bench", "instruments": {"cam": {"card": "keithley/2400",
                                                                         "address": "fake://cam"}}}, registry),
              registry, factory, SimClock())
    first = lab.instruments["cam"].backend
    began = time.monotonic()
    with pytest.raises(AdapterError) as e:
        first.query("GET reading")
    assert 0.25 < time.monotonic() - began < 3
    assert "not connected any more: its maker's read didn't return within 0 s" in str(e.value)
    began = time.monotonic()
    with pytest.raises(AdapterError):
        first.query("GET reading")                                             # at once, not behind the stuck call
    assert time.monotonic() - began < 0.2 and first.quiet_close
    assert "connected again (its connection had ended" in lab.reconnect("cam")
    assert lab.instruments["cam"].backend is made[1] and first.closed           # a fresh one; the old one let go
    release.set()
    assert lab.instruments["cam"].backend.query("GET reading") == "1"
    made[1].gone = "its maker's read didn't return"
    lab.switch("cam", False)                                                   # ended: let go, not set aside
    assert made[1].closed and "cam" not in lab._parked
    lab.switch("cam", True)
    assert lab.instruments["cam"].backend is made[2]


def test_a_call_that_waited_behind_the_stuck_one_is_refused_too():
    release = threading.Event()
    stuck = Stuck("cam", builtin_registry().get("keithley/2400"), "fake://cam", release)
    got = []

    def ask():
        try:
            got.append(stuck.query("GET reading"))
        except AdapterError as e:
            got.append(e)
    first = threading.Thread(target=ask, daemon=True)
    first.start()
    assert wait_for(lambda: stuck.calls == 1)
    second = threading.Thread(target=ask, daemon=True)   # waits for the driver behind the first call
    second.start()
    first.join(3)
    second.join(3)
    assert len(got) == 2 and all(isinstance(g, AdapterError) for g in got)
    assert stuck.calls == 1                                                    # never sent into the stuck driver
    release.set()


def andor_sdk2(calls, wait=None, readout=2.0):
    class Atmcd:   # as test_more_instruments' fake: every call answers DRV_SUCCESS but those named
        def __getattr__(self, name):
            if name == "WaitForAcquisitionTimeOut" and wait is None:
                raise AttributeError(name)   # an SDK without it

            def f(*args):
                calls.append((name,) + args)
                if name == "WaitForAcquisitionTimeOut":
                    return wait
                if name == "GetAcquiredData":
                    return 20002, list(range(args[0]))
                return {"GetDetector": (20002, 3, 2), "GetAcquisitionTimings": (20002, 0.1, 0.1, 0.1),
                        "GetReadOutTime": (20002, readout), "IsCoolerOn": (20002, 1)}.get(name, 20002)
            return f
    return SimpleNamespace(atmcd=Atmcd)


ANDOR = "lab: B\ninstruments:\n  ixon: {card: andor/ccd, address: 'andor://0?sdk=2'}\n"


def test_an_andor_acquisition_that_never_delivers_is_aborted_in_its_time(monkeypatch):
    calls = []
    monkeypatch.setitem(MODULES, "andor", andor_sdk2(calls, wait=20024))       # DRV_NO_NEW_DATA: no frame came
    lab = build_lab(ANDOR)
    with pytest.raises(AdapterError) as e:
        lab.read("ixon", "spectrum")
    assert "no frame came within 62 s, so Kelvin aborted the acquisition" in str(e.value)
    assert ("WaitForAcquisitionTimeOut", 62100) in calls and ("AbortAcquisition",) in calls
    assert not lab.instruments["ixon"].backend.gone                            # the camera answers: still connected
    monkeypatch.setitem(MODULES, "andor", andor_sdk2(calls, wait=20002))
    lab2 = build_lab(ANDOR)
    assert lab2.read("ixon", "spectrum") == [0.0, 1.0, 2.0]


def test_an_andor_sdk_without_a_timed_wait_is_waited_for_on_a_thread_of_its_own(monkeypatch):
    from labharness import cameras
    calls, release = [], threading.Event()
    sdk = andor_sdk2(calls, readout=0.0)
    blocking = sdk.atmcd

    class Atmcd(blocking):
        def WaitForAcquisition(self):
            release.wait(30)
            return 20002
    monkeypatch.setitem(MODULES, "andor", SimpleNamespace(atmcd=Atmcd))
    monkeypatch.setattr(cameras, "ACQUIRE_MARGIN_S", 0.2)
    lab = build_lab(ANDOR)
    began = time.monotonic()
    with pytest.raises(Exception) as e:
        lab.read("ixon", "spectrum")
    assert time.monotonic() - began < 3 and "not connected any more" in str(e.value)
    old = lab.instruments["ixon"].backend
    assert old.gone and "connected again" in lab.reconnect("ixon")
    assert lab.instruments["ixon"].backend is not old
    release.set()


def test_a_lightfield_capture_that_never_returns_ends_its_connection(monkeypatch):
    pytest.importorskip("numpy")
    from labharness import lightfield
    from test_lightfield import LAB, FakeLightField
    release = threading.Event()
    lf = FakeLightField()
    lf.experiment.Capture = lambda count: release.wait(30)
    monkeypatch.setitem(MODULES, "lightfield", lf)
    monkeypatch.setattr(lightfield, "CAPTURE_S", 0.6)
    monkeypatch.setattr(lightfield, "FRAME_S", 0.0)
    lab = build_lab(LAB)
    began = time.monotonic()
    with pytest.raises(Exception) as e:
        lab.read("spec", "spectrum")
    assert time.monotonic() - began < 3
    assert "not connected any more: LightField's capture didn't return within 1 s" in str(e.value)
    assert "connected again" in lab.reconnect("spec")
    assert lf.disposed and lf.started == [True, True]                           # the old LightField let go, a new one
    release.set()


def test_a_qick_boards_calls_wait_at_most_their_time():
    from labharness.rfsoc import CALL_S, Qick
    from test_web_instruments import SOCCFG, FakeSoc, fake_qick
    soc = FakeSoc()
    card = builtin_registry().get("qick/rfsoc")
    Qick("rf", card, "qick://10.0.0.50:8888/myqick", fake_qick(soc, SOCCFG))
    assert soc._pyroTimeout == CALL_S == 60.0                                   # Pyro's own default: forever


def test_a_makers_listing_that_never_returns_is_left_out_of_the_scan_and_said(monkeypatch):
    from labharness import drivers
    release = threading.Event()

    def sdk3():
        release.wait(30)
        return SimpleNamespace(DeviceCount=1)
    monkeypatch.setitem(MODULES, "andor", SimpleNamespace(AndorSDK3=sdk3))
    monkeypatch.setattr(drivers, "LIST_S", 0.3)
    try:
        began = time.monotonic()
        assert drivers.discover_libraries() == []
        assert time.monotonic() - began < 3
        assert any("Andor's camera SDK didn't finish listing its instruments within 0 s" in n
                   for n in drivers.library_notes())
        began = time.monotonic()
        assert drivers.discover_makers() == []                                 # not started again behind it
        assert time.monotonic() - began < 0.5
        assert any("its listing from an earlier scan still hasn't returned" in n for n in drivers.library_notes())
    finally:
        release.set()
    assert wait_for(lambda: not drivers._LISTING["andor"].is_alive())
    assert drivers.discover_libraries() == [("andor://0?sdk=3", "Andor,sCMOS camera,0")]
    assert not any("Andor" in n for n in drivers.library_notes())
