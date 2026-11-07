"""An instrument whose driver never returns from opening it (a camera SDK's open, a GPIB driver) never holds the lab
for good (review of 0.3.45, 3.2: the setup showed "Connecting cam, 1 of 5" for good, and switching it off, Save and
connect, reconnecting and every change waited behind it; a restart hung on it again). Once its time is up it is set
aside with why and the rest of the lab connects and works; a person can switch it off at once; a connection that opens
after all is closed again, never used; the old lab's closing never holds a switch of labs."""
import threading
import time

import pytest

from labharness import lab as labmod
from labharness.clock import SimClock
from labharness.gate import ScriptedApprover
from labharness.homelab import builtin_registry
from labharness.instrument import InstrumentOff, Unavailable
from labharness.lab import Lab, OpenTimedOut
from labharness.labconfig import load_lab_config
from labharness.sim.twin import World, sim_backend_factory

# the tests' lab's cards (simlab): no connection timeout of their own, so OPEN_S alone says how long an open may take
CFG = {"lab": "bench", "instruments": {"cam": {"card": "virtual/source-meter", "address": "fake://cam"},
                                        "tc": {"card": "virtual/temperature-controller", "address": "fake://tc"}}}


def wait_for(check, within=5.0):
    end = time.monotonic() + within
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


class Kept:
    """A connection the fake driver made: a simulated instrument behind it, and whether it was closed. Closing sends
    nothing, as a VISA session's."""
    quiet_close = True

    def __init__(self, inner):
        self.inner, self.closed = inner, False

    def write(self, command):
        self.inner.write(command)

    def query(self, command):
        return self.inner.query(command)

    def close(self):
        self.closed = True

    def __getattr__(self, name):
        return getattr(self.inner, name)


class Hub:
    """A driver whose open of the instruments in `hang` doesn't return until released (as an SDK's that never does)."""

    def __init__(self, hang=("cam",)):
        self.release = threading.Event()
        self.hang = set(hang)
        self.made = []
        self.sim = sim_backend_factory(World(SimClock(), 1))

    def factory(self, name, card, address):
        if name in self.hang:
            self.release.wait(30)
        backend = Kept(self.sim(name, card, address))
        self.made.append((name, backend))
        return backend

    def of(self, name):
        return [b for n, b in self.made if n == name]


def lab_of(hub, later=False, partial=True):
    registry = builtin_registry()
    return Lab(load_lab_config(CFG, registry), registry, hub.factory, SimClock(), partial=partial, later=later)


@pytest.fixture
def short(monkeypatch):
    from labharness import others
    monkeypatch.setattr(labmod, "OPEN_S", 0.6)
    monkeypatch.setattr(others, "holders", lambda: ["LabVIEW"])   # what else runs here, as psutil would say


def test_an_open_that_never_returns_is_set_aside_in_its_time_and_closed_if_it_opens_later(short):
    hub = Hub()
    lab = lab_of(hub, later=True)
    began = time.monotonic()
    with pytest.raises(OpenTimedOut):
        lab.reconnect("cam")
    assert time.monotonic() - began < 3
    why = lab.unavailable["cam"]
    assert why == ("cam: its driver didn't finish opening it within 1 s, so Kelvin went on without it: is the "
                   "instrument on, and not open in another program (running now: LabVIEW)? If opening hangs again, "
                   "turn the instrument itself off and on, then connect it again")
    assert "its maker's software or another program" in labmod.open_hung_text("cam", 60, "andor://0")
    assert lab.reconnect("tc") == "" and lab.instruments["tc"].enabled          # the rest of the lab connects
    hub.release.set()                                                          # the driver returns after all
    assert wait_for(lambda: hub.of("cam") and hub.of("cam")[0].closed)         # closed again at once
    assert isinstance(lab.instruments["cam"].backend, Unavailable)             # never handed to the lab
    with pytest.raises(KeyError):
        lab.instrument("cam")


def test_how_long_an_open_may_take():
    card = builtin_registry().get
    assert labmod.open_deadline(card("keithley/2400"), "GPIB0::24::INSTR") == 60.0       # a VISA session: a minute
    assert labmod.open_deadline(card("princeton/spectrapro"), "serial://COM4") == 75.0   # its own 30 s, twice, and 15 s
    assert labmod.open_deadline(card("princeton/lightfield"), "lightfield://PL") == 300.0  # LightField starting
    assert labmod.open_deadline(card("princeton/picam"), "picam://1234567") == 120.0     # PICam's helper's own times
    # the lib:// helper's, then the Kinesis card's own four open steps at its 70 s each
    assert labmod.open_deadline(card("thorlabs/kdc101"), "lib://27000001") == 155.0 + 4 * 70.0


def test_a_lab_opened_at_once_sets_the_hung_one_aside_and_opens_the_rest(short):
    hub = Hub()
    began = time.monotonic()
    lab = lab_of(hub)
    assert time.monotonic() - began < 3
    assert lab.instruments["tc"].enabled and "didn't finish opening" in lab.unavailable["cam"]
    hub.release.set()
    with pytest.raises(OpenTimedOut):                                          # not partial: raised, as any error
        lab_of(Hub(), partial=False)


def test_the_setup_goes_on_and_a_person_switches_the_hung_one_off_without_waiting(tmp_path, short):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    from labharness.autosetup import AutoSetup
    hub = Hub()
    lab = lab_of(hub, later=True)
    rt = LabRuntime(lab, ScriptedApprover([]), tmp_path / "data")
    rt.lab_info = {"kind": "file"}
    setup = AutoSetup(rt, {"auto_setup": "off"}, tmp_path / "home")
    setup.start("start")
    assert wait_for(lambda: rt.busy == "connecting cam") and rt.setting_up
    began = time.monotonic()
    rt.switch_instrument("cam", False, "human (portal)")                       # waits for nothing, not the lab
    assert time.monotonic() - began < 0.5
    assert "cam" in lab.switched_off
    setup.join(10)
    assert not rt.setting_up and setup.status["state"] == "done" and lab.instruments["tc"].enabled
    assert "cam" in lab.switched_off and "cam" not in lab.unavailable           # still off: the person's word stands
    hub.release.set()
    assert wait_for(lambda: hub.of("cam") and hub.of("cam")[0].closed)          # its late connection: closed, unused
    assert "cam" in lab.switched_off
    [switched] = [e for e in rt.gate.log.events() if e["type"] == "instrument_switched"]
    assert switched["on"] is False and switched["by"] == "human (portal)"


def test_save_and_connect_waits_only_for_the_opens_time(tmp_path, short):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    hub = Hub()
    lab = lab_of(hub, later=True)
    rt = LabRuntime(lab, ScriptedApprover([]), tmp_path / "data")
    worker = threading.Thread(target=rt.connect_pending, daemon=True)
    worker.start()
    assert wait_for(lambda: rt.busy == "connecting cam")
    new = lab_of(Hub(hang=()), later=True)
    began = time.monotonic()
    rt.switch_lab(new, None, {"kind": "file"}, "human (portal)")
    assert time.monotonic() - began < 5 and rt.lab is new
    assert new.instruments["cam"].enabled and new.instruments["tc"].enabled
    worker.join(5)
    assert not worker.is_alive()                                               # the old lab's connecting: given up
    hub.release.set()
    assert wait_for(lambda: hub.of("cam") and hub.of("cam")[0].closed)          # its late connection: closed, unused


def test_the_old_labs_closing_never_holds_a_switch(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from labharness.agent import runtime as rtmod
    from labharness.agent.runtime import LabRuntime
    monkeypatch.setattr(rtmod, "SWITCH_CLOSE_S", 0.4)
    hub = Hub(hang=())
    lab = lab_of(hub)
    rt = LabRuntime(lab, ScriptedApprover([]), tmp_path / "data")
    stuck = threading.Event()
    lab.instruments["cam"].backend.close = lambda: stuck.wait(30)               # a camera library that never returns
    began = time.monotonic()
    rt.switch_lab(lab_of(Hub(hang=()), later=True), None, {"kind": "file"}, "human (portal)")
    assert time.monotonic() - began < 5
    assert rt.lab.instruments["cam"].enabled and rt.lab.instruments["tc"].enabled
    [changed] = [e for e in rt.gate.log.events() if e["type"] == "lab_changed"]
    assert changed["still_closing"] == ["cam"]
    stuck.set()


def test_a_person_switches_off_again_an_instrument_still_connecting(tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    hub = Hub(hang=())
    lab = lab_of(hub)
    rt = LabRuntime(lab, ScriptedApprover([]), tmp_path / "data")
    rt.switch_instrument("cam", False, "Ada")
    assert hub.of("cam")[0].closed                                              # a VISA session: closed
    hub.hang = {"cam"}
    got = []

    def on():
        try:
            rt.switch_instrument("cam", True, "Ada")
            got.append("on")
        except Exception as e:
            got.append(e)
    worker = threading.Thread(target=on, daemon=True)
    worker.start()
    assert wait_for(lambda: lab.instruments["cam"].unavailable == labmod.CONNECTING_AGAIN)
    began = time.monotonic()
    rt.switch_instrument("cam", False, "Ada")                                   # not behind the open
    assert time.monotonic() - began < 0.5 and "cam" in lab.switched_off
    hub.release.set()
    worker.join(5)
    assert isinstance(got[0], InstrumentOff)                                    # the switch on says it stayed off
    assert "cam" in lab.switched_off and len(hub.of("cam")) == 2 and hub.of("cam")[1].closed
