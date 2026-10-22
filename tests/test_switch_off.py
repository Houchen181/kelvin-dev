"""Switching an instrument off in the Lab panel: Kelvin sends it nothing at all until a person switches it on."""
import pytest

from labharness import ScriptedApprover, demo_envelope
from labharness.agent.runtime import LabRuntime, event_line
from labharness.clock import SimClock
from labharness.instrument import OffBackend
from labharness.lab import Lab
from labharness.labconfig import load_lab_config
from labharness.sim.twin import World, sim_backend_factory
from labharness.virtual_lab import DATA, build_virtual_lab, builtin_registry

SWEEP = """plan: gate_sweep
setup: { lockin.amplitude: 0.05, smu.output: on }
sweep: { smu.V: {from: 0, to: 1, step: 0.5} }
measure: [lockin.X, smu.I]
after: { smu.V: 0 }"""


class Recorder:
    """A simulated instrument's connection that notes every command sent to it."""

    def __init__(self, inner, sent, name):
        self.inner, self.sent, self.name = inner, sent, name
        self.simulated = True
        self.closed = False

    def write(self, command):
        self.sent.append((self.name, command))
        self.inner.write(command)

    def query(self, command):
        self.sent.append((self.name, command))
        return self.inner.query(command)

    def close(self):
        self.closed = True

    def __getattr__(self, name):
        return getattr(self.inner, name)


class QuietRecorder(Recorder):
    quiet_close = True   # like a VISA session: closing it sends nothing


def recorded_lab(off=(), backend=Recorder):
    clock, registry = SimClock(), builtin_registry()
    world = World(clock, 1)
    cfg = load_lab_config(DATA / "virtual_lab.yaml", registry)
    sim, sent, opened = sim_backend_factory(world), [], []

    def factory(name, card, address):
        opened.append(name)
        return backend(sim(name, card, address), sent, name)

    return Lab(cfg, registry, factory, clock, world, lambda lab: set(off) if lab == "Virtual Lab" else set()), sent, opened


def runtime_for(lab, tmp_path):
    return LabRuntime(lab, ScriptedApprover([True] * 20), tmp_path / "data", demo_envelope(lab))


def test_a_switched_off_instrument_gets_nothing(tmp_path):
    lab, sent, opened = recorded_lab(off={"smu"})
    assert "smu" not in opened and isinstance(lab.instruments["smu"].backend, OffBackend)   # never even connected
    rt = runtime_for(lab, tmp_path)
    assert "smu: switched off in the Lab panel" in rt.lab_status()
    assert "switched off" in rt.get("smu", "V")
    assert "switched off" in rt.set("smu", "V", 1)
    assert "switched off" in rt.raw("smu", "*IDN?")
    assert "switched off" in rt.run(SWEEP)                                   # a plan that uses it is refused
    assert "smu switched off (not checked)" in rt.health() and "smu FAIL" not in rt.health()
    assert "smu" not in lab.snapshot() and [h.instr for h in lab.health_check()].count("smu") == 0
    assert "switched off" in str(lab.health_check(["smu"])[0])
    assert "smu" not in [i["name"] for i in rt.instruments() if i["enabled"]]
    assert next(i for i in rt.instruments() if i["name"] == "smu")["state"] == "off"
    rt.monitor.tick()
    assert [c for c in sent if c[0] == "smu"] == []
    assert "lockin.X" in rt.get("lockin", "X")                               # the others work as before


def test_a_drive_whose_guard_or_interlock_is_off_stays_put(tmp_path):
    lab, sent, _ = recorded_lab(off={"tc"})
    rt = runtime_for(lab, tmp_path)
    r = rt.set("laser", "power", 1)
    assert "guard" in r and "tc" in r and "switched off" in r                # nothing would watch the sample's T
    r = rt.set("magnet", "B", 0.1)
    assert "interlock" in r and "switched off" in r
    assert [c for c in sent if c[0] in ("laser", "magnet") and not c[1].endswith("?")] == []
    rt.monitor.tick()                                                         # its watch rests: no "not answering"
    assert not rt.monitor.active() and [c for c in sent if c[0] == "tc"] == []
    assert [w["paused"] for w in rt.monitor.list() if w["key"] == "tc.T_sample"] == [True]


def test_switching_off_and_on_while_kelvin_runs(tmp_path):
    lab, sent, opened = recorded_lab()
    rt = runtime_for(lab, tmp_path)
    rt.set("smu", "V", 1)
    rt.live = {"status": "running"}
    with pytest.raises(RuntimeError):
        rt.switch_instrument("smu", False, "Ada")                            # not in the middle of a run
    rt.live = None
    rt.switch_instrument("smu", False, "Ada")
    before = len(sent)
    rt.lab_status()
    rt.health()
    assert [c for c in sent[before:] if c[0] == "smu"] == []
    assert not lab.instruments["smu"].backend.__class__ is Recorder
    rt.switch_instrument("smu", True, "Ada")
    assert opened.count("smu") == 1                                          # its own connection, set aside, back
    assert "1 V" in rt.get("smu", "V")                                        # as it was left
    with pytest.raises(KeyError):
        rt.switch_instrument("nothing", False, "Ada")
    lines = [event_line(e) for e in rt.gate.log.events() if e.get("type") == "instrument_switched"]
    assert any("Ada: smu switched off: Kelvin sends it nothing" in x for x in lines)
    assert any("smu switched on" in x for x in lines)


def test_a_quiet_connection_is_closed_and_opened_again(tmp_path):
    lab, sent, opened = recorded_lab(backend=QuietRecorder)
    old = lab.instruments["smu"].backend
    lab.switch("smu", False)
    assert old.closed and not lab._parked                                    # closing a VISA session sends nothing
    lab.switch("smu", True)
    assert opened.count("smu") == 2 and lab.instruments["smu"].backend is not old
    driver = lab.instruments["pg"].backend
    driver.quiet_close = False
    lab.switch("pg", False)
    assert not driver.closed and lab._parked["pg"] is driver                 # closing a driver could send: set aside
    lab.close()
    assert not driver.closed


def test_a_dry_run_twin_keeps_it_off():
    lab = build_virtual_lab(seed=1, switched_off=lambda lab: {"smu"})
    twin = lab.make_twin()
    assert not twin.instruments["smu"].enabled and twin.instruments["lockin"].enabled


def test_the_lab_panel_switch(tmp_path):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    pytest.importorskip("httpx")
    from starlette.testclient import TestClient
    from labharness.agent.approval import WebApprover
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore

    settings = Settings(tmp_path / "settings.json")
    lab = build_virtual_lab(seed=1, switched_off=settings.switched_off)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    client = TestClient(create_app(runtime, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"]))
    h = {"X-Kelvin-Token": settings.access_key}
    r = client.post("/api/instruments/smu/switch", headers=h, json={"on": False})
    assert r.status_code == 200 and next(i for i in r.json()["instruments"] if i["name"] == "smu")["state"] == "off"
    assert settings.switched_off("Virtual Lab") == ["smu"] and settings.switched_off("Another lab") == []
    r = client.post("/api/terminal", headers=h, json={"instrument": "smu", "op": "get", "arg": "V"})
    assert "switched off" in r.text
    assert client.post("/api/instruments/nothing/switch", headers=h, json={"on": False}).status_code == 404
    runtime.live = {"status": "running"}
    assert client.post("/api/instruments/smu/switch", headers=h, json={"on": True}).status_code == 409
    runtime.live = None
    again = build_virtual_lab(seed=1, switched_off=Settings(tmp_path / "settings.json").switched_off)
    assert not again.instruments["smu"].enabled                               # still off after a restart
    assert client.post("/api/instruments/smu/switch", headers=h, json={"on": True}).status_code == 200
    assert settings.switched_off("Virtual Lab") == []
