"""One instrument that can't be connected (its maker's package missing, switched off at the wall, unplugged) no longer
takes the whole lab to the virtual one: it is set aside with the reason, the rest of the lab works, and it connects
once that is fixed. Nothing is sent to it meanwhile, and what depends on it is refused."""
import json

import pytest

from labharness import EventLog, SafetyGate, ScriptedApprover
from labharness.adapters import set_resource_manager
from labharness.virtual_lab import lab_registry, open_home_lab
from test_hardware import Bus, Device

BENCH = """lab: Bench
instruments:
  smu:
    card: keithley/2400
    address: "GPIB0::24::INSTR"
    limits: { V: [-5, 5] }
    guards:
      V: [{ read: lockin.X, max_abs: 1.0e-3 }]
  lockin:
    card: srs/sr830
    address: "GPIB0::9::INSTR"
"""


@pytest.fixture
def bench(tmp_path):
    bus = Bus({"GPIB0::24::INSTR": Device("keithley/2400", name="k2400")})   # the lock-in isn't there yet
    set_resource_manager(bus)
    (tmp_path / "lab.yaml").write_text(BENCH, encoding="utf-8")
    yield bus
    set_resource_manager(None)


def test_the_rest_of_the_lab_works_when_one_instrument_cant_connect(bench, tmp_path):
    lab, envelope, info = open_home_lab(tmp_path, "auto", 0)
    try:
        assert info["kind"] == "file" and info["error"] is None
        assert list(lab.unavailable) == ["lockin"] and "VI_ERROR_RSRC_NFOUND" in lab.unavailable["lockin"]
        assert lab.switched_off == []                                    # nobody switched it off
        assert lab.read("smu", "V") is not None                           # the source-meter works
        with pytest.raises(KeyError, match="lockin is not connected"):
            lab.read("lockin", "X")
        health = {r.instr: r for r in lab.health_check(["smu", "lockin"])}
        assert health["smu"].ok and not health["lockin"].ok
        assert health["lockin"].problems[0].startswith("not connected (")
        gate = SafetyGate(lab, ScriptedApprover([]), EventLog(tmp_path / "events.jsonl", lab.clock))
        refused = gate.set("smu", "V", 1)                                 # its guard can't be watched
        assert refused.action == "refused" and "its guard lockin.X is on lockin, which is not connected" in \
            refused.message
        assert lab.make_twin().instruments["lockin"].enabled is False     # dry runs leave it out too

        bench.devices["GPIB0::9::INSTR"] = Device("srs/sr830", name="sr830")   # plugged in
        lab.switch("lockin", True)                                        # switching it on connects it
        assert lab.unavailable == {} and lab.read("lockin", "X") is not None
    finally:
        lab.close()


def test_a_lab_none_of_whose_instruments_connect_stays_on_the_virtual_lab(tmp_path):
    set_resource_manager(Bus({}))
    try:
        (tmp_path / "lab.yaml").write_text(BENCH, encoding="utf-8")
        lab, _, info = open_home_lab(tmp_path, "auto", 0)
        assert info["kind"] == "virtual" and "none of its instruments could be connected" in info["error"]
        lab.close()
    finally:
        set_resource_manager(None)


def test_kelvin_reconnects_an_instrument_once_it_is_fixed(bench, tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    lab, _, info = open_home_lab(tmp_path, "auto", 0)
    rt = LabRuntime(lab, ScriptedApprover([]), tmp_path / "data")
    rt.lab_info = info
    try:
        status = rt.lab_status()
        assert "lockin: not connected (" in status and "then reconnect_instrument" in status
        assert rt.ui_state()["unavailable"][0]["name"] == "lockin"
        row = next(i for i in rt.instruments() if i["name"] == "lockin")
        assert row["state"] == "unavailable" and row["why"] and row["install"] is None
        assert rt.ask_to_switch_on("lockin", "for the lock-in signal").startswith("not asked: nobody switched")
        assert rt.reconnect_instrument("lockin").startswith("lockin is still not connected")
        bench.devices["GPIB0::9::INSTR"] = Device("srs/sr830", name="sr830")
        assert rt.reconnect_instrument("lockin").startswith("lockin is connected now")
        assert rt.reconnect_instrument("lockin") == "lockin is connected, with its card as it is now"
        logged = [json.loads(x) for x in (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        assert [e["type"] for e in logged if e["type"] in ("reconnect_failed", "instrument_connected")] == [
            "reconnect_failed", "instrument_connected"]
    finally:
        lab.close()


def test_a_person_may_switch_off_an_instrument_that_isnt_connected(bench, tmp_path):
    lab, _, _ = open_home_lab(tmp_path, "auto", 0)
    try:
        lab.switch("lockin", False)
        assert lab.unavailable == {} and lab.switched_off == ["lockin"]
        with pytest.raises(KeyError, match="switched off in the Lab panel"):
            lab.read("lockin", "X")
    finally:
        lab.close()


def test_an_install_connects_the_instruments_it_was_missing_for(bench, tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from labharness import envs
    from labharness.agent.runtime import LabRuntime
    from test_envs import FakeUv, PYPI
    lab, _, _ = open_home_lab(tmp_path, "auto", 0)
    from labharness.instrument import Unavailable
    lab.instruments["lockin"].backend = Unavailable("lockin", "lockin: " + envs.missing("qick", "Pyro4"))
    rt = LabRuntime(lab, ScriptedApprover([True]), tmp_path / "data")
    rt.home = tmp_path
    monkeypatch.setattr(envs, "_run", FakeUv())
    monkeypatch.setattr("sys.path", list(__import__("sys").path))
    (tmp_path / "tools" / "uv").mkdir(parents=True)
    (tmp_path / "tools" / "uv" / ("uv.exe" if envs.WIN else "uv")).write_text("")
    rt.fetch_url = lambda url: json.dumps(PYPI[url.rstrip("/").split("/")[-2].lower()]).encode()
    bench.devices["GPIB0::9::INSTR"] = Device("srs/sr830", name="sr830")   # what was missing is there now
    try:
        said = rt.install_for_instrument("drivers", ["qick", "Pyro4"])
        assert "installed in environment drivers" in said and "lockin is connected now" in said
        assert lab.unavailable == {}
    finally:
        lab.close()
