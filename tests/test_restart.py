"""After Kelvin stops (a crash, a power cut, an update) and starts again: a card says what the stop left behind, the
watches and the alarm history come back, and outputs left on with nothing happening go to safe (field tests: a 6221
on for 2.7 h and a laser on an emitter for 2 h 47 min after a turn died; watches gone without a word)."""
import json
import time

import pytest

from labharness import ScriptedApprover, rundata
from simlab import build_virtual_lab

pytest.importorskip("mcp")
from labharness.agent.runtime import LabRuntime  # noqa: E402


def kelvin(lab, data):
    """Kelvin started on these instruments (a restart keeps them as they were) and this data folder."""
    rt = LabRuntime(lab, ScriptedApprover([True] * 10), data)
    rt.auto_report = "off"
    return rt


def crashed_run(data, queued):
    """A run as a crash leaves it: its record without a finish frame (test_rundata.crashed_run)."""
    folder = data / "runs" / "20261028-021500-rt_sweep"
    folder.mkdir(parents=True)
    r = rundata.Recorder(folder)
    meta = {"run_id": folder.name, "plan": "rt_sweep", "started": "2026-10-28T02:15:00.000", "planned_points": 10}
    if queued:
        meta["queue"] = {"item": "q1", "position": 1, "note": ""}
    r.write("start", {"plan": "plan: rt_sweep\n", "meta": meta})
    r.write("columns", {"columns": {"axes": [{"key": "smu.V", "unit": "V", "kind": "instrument"}],
                                    "measures": [{"key": "smu.I", "unit": "A", "kind": "scalar"}]}})
    for i in range(3):
        r.write("row", values={"i": i, "t": float(i), "smu.V": 0.1 * i, "smu.I": 1e-9 * i})
    r.close()
    rundata._let_go(r._lock)
    return folder.name


def left_on(rt):
    """The smu sourcing 1 V into the sample, and the magnet at 0.5 T, as a run left them."""
    assert "1 V" in rt.set("smu", "output", "on") + rt.set("smu", "V", 1)
    rt.set("magnet", "B", 0.5)
    assert rt.lab.read("smu", "output") is True


def test_after_an_unattended_crash_the_card_lists_what_was_left_and_the_outputs_go_to_safe(tmp_path):
    lab, data = build_virtual_lab(seed=1), tmp_path / "data"
    before = kelvin(lab, data)
    left_on(before)
    before.add_watch("smu.I", above=1e-6, note="leakage")
    run_id = crashed_run(data, queued=True)
    (data / "queue.json").write_text(json.dumps({"items": [   # the queue was running it when Kelvin stopped
        {"id": "q1", "kind": "plan", "name": "rt_sweep", "text": "plan: rt_sweep\nmeasure: [smu.I]\n",
         "status": "running"}]}), encoding="utf-8")

    rt = kelvin(lab, data)                                        # Kelvin starts again on the same instruments
    assert [w["rule"] for w in rt.monitor.list() if w["source"] != "lab"] == ["smu.I above 1e-06 A"]   # the watch is back
    rt._incident_check()
    i = rt.incident
    assert i["outputs"] == ["smu"] and i["runs"] == [run_id] and i["queue"] == ["rt_sweep"] and i["unattended"]
    assert "magnet.B 500 mT" in i["away"] and "smu.V 1 V" in i["on"]
    assert i["safe"] and rt.lab.read("smu", "output") is False and rt.lab.read("smu", "V") == 0
    assert rt.lab.read("magnet", "B") == 0.5                      # field, temperature and positions stay
    assert i["watches"]["back"] == ["smu.I above 1e-06 A"]
    status = rt.lab_status()
    assert "AFTER KELVIN RESTARTED" in status and "runs it cut short" in status and run_id in status
    assert rt.ui_state()["incident"]["lines"]
    rt._incident_check()                                          # once per start
    rt.dismiss_incident("human (portal)")
    assert rt.incident is None and "AFTER KELVIN RESTARTED" not in rt.lab_status()
    kinds = [e["type"] for e in rt.gate.log.events()]
    assert "incident" in kinds and "incident_dismissed" in kinds


def test_after_a_crash_with_a_person_there_the_card_offers_safe_and_waits(tmp_path):
    lab, data = build_virtual_lab(seed=1), tmp_path / "data"
    left_on(kelvin(lab, data))
    crashed_run(data, queued=False)
    rt = kelvin(lab, data)
    rt._incident_check()
    assert rt.incident["outputs"] == ["smu"] and not rt.incident["unattended"] and rt.incident["safe"] == []
    assert rt.lab.read("smu", "output") is True                  # offered, not done
    lines = rt.incident_safe("human (portal)")
    assert lines and rt.lab.read("smu", "output") is False and rt.incident["safe"] == lines


def test_a_clean_start_shows_no_card(tmp_path):
    rt = kelvin(build_virtual_lab(seed=1), tmp_path / "data")
    rt._incident_check()
    assert rt.incident is None


def test_watches_and_the_alarm_history_outlast_a_restart(tmp_path):
    lab, data = build_virtual_lab(seed=1), tmp_path / "data"
    before = kelvin(lab, data)
    left_on(before)
    w = before.add_watch("smu.V", above=0.5, source="agent")
    before.add_watch("lockin.X", below=-1.0)
    before.monitor.tick()
    assert [a.text for a in before.monitor.active()] == ["smu.V = 1 V is above 0.5 V"]
    before.remove_watch(w["id"])                                  # removed: not back after the restart
    assert before.monitor.active() == []                          # nor its alarm, which once came back as "on"
    assert "no longer watched" in before.monitor.alarms[0].cleared_text
    before.add_watch("smu.V", above=0.5, source="agent")
    before.monitor.tick()                                         # the new watch alarms of its own
    saved = json.loads((data / "watches.json").read_text(encoding="utf-8"))
    assert len(saved["labs"][lab.config.name]) == 2

    rt = kelvin(lab, data)
    assert sorted((v["rule"], v["source"]) for v in rt.monitor.list() if v["source"] != "lab") == [
        ("lockin.X below -1 V", "person"), ("smu.V above 0.5 V", "agent")]
    old = rt.monitor.alarms[0]
    assert old.cleared and "on when Kelvin stopped" in old.text and rt.monitor.active() == []
    assert [a["text"] for a in rt.alarms_before] == ["smu.V = 1 V is above 0.5 V"]
    rt._incident_check()
    assert rt.incident["alarms"] == ["smu.V = 1 V is above 0.5 V"]
    rt.monitor.tick()                                             # still above: its watch raises it again
    assert [a.text for a in rt.monitor.active()] == ["smu.V = 1 V is above 0.5 V"]


def test_a_watch_whose_reading_the_lab_no_longer_has_is_dropped_and_said(tmp_path):
    lab, data = build_virtual_lab(seed=1), tmp_path / "data"
    (data).mkdir(parents=True)
    (data / "watches.json").write_text(json.dumps({"labs": {lab.config.name: [
        {"read": "oldtc.T_A", "below": 2.0, "source": "person"}, {"read": "smu.I", "above": 1e-6}]}}),
        encoding="utf-8")
    rt = kelvin(lab, data)
    assert [v["rule"] for v in rt.monitor.list() if v["source"] != "lab"] == ["smu.I above 1e-06 A"]
    assert rt.restored["dropped"] and rt.restored["dropped"][0].startswith("oldtc.T_A")
    rt._incident_check()
    assert "watches not back" in " ".join(rt.incident_lines())


def test_light_left_on_with_nothing_happening_goes_to_safe_and_other_outputs_are_told(tmp_path):
    rt = kelvin(build_virtual_lab(seed=1), tmp_path / "data")
    left_on(rt)
    assert "on" in rt.set("laser", "emission", True)
    rt.idle_off_s = 60
    rt._idle_check()
    assert rt.lab.read("laser", "emission") is True              # it was just used
    rt.last_active = time.monotonic() - 61
    rt.queue.state = "running"
    rt._idle_check()
    assert rt.lab.read("laser", "emission") is True              # a queue is running: not idle
    rt.queue.state = "idle"
    rt._idle_check()
    assert rt.lab.read("laser", "emission") is False             # light on the sample: off
    assert rt.lab.read("smu", "output") is True                  # a source is only told about (a gate held on purpose)
    assert rt.lab.read("magnet", "B") == 0.5                      # never field, temperature or positions
    [off] = [e for e in rt.gate.log.events() if e["type"] == "idle_off"]
    assert off["by"] == "kelvin" and "laser.emission on" in off["was"]
    [told] = [e for e in rt.gate.log.events() if e["type"] == "idle_on"]
    assert "smu.output on" in told["still"]
    titles = [n["title"] for n in rt.notifier.notices]
    assert any(t.startswith("Left on for") for t in titles) and any(t.startswith("Still on after") for t in titles)
    rt.idle_off_all = True                                        # Settings: other outputs too
    rt.last_active = time.monotonic() - 61
    rt._idle_check()
    assert rt.lab.read("smu", "output") is False and rt.lab.read("smu", "V") == 0
    rt.idle_off_s = None
    left_on(rt)
    rt.last_active = time.monotonic() - 10 ** 6
    rt._idle_check()
    assert rt.lab.read("smu", "output") is True                  # switched off in Settings: never


def test_the_portal_shows_the_card_and_a_person_answers_it(tmp_path, monkeypatch):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from labharness.agent import runtime as runtime_module
    from labharness.web.app import STATIC, create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    monkeypatch.setattr(runtime_module, "BACKGROUND_S", 0.1)
    lab, data = build_virtual_lab(seed=1), tmp_path / "data"
    left_on(kelvin(lab, data))
    crashed_run(data, queued=False)
    rt = kelvin(lab, data)
    settings = Settings(tmp_path / "Kelvin" / "settings.json")
    client = TestClient(create_app(rt, settings, ChatStore(tmp_path / "Kelvin" / "chats"), hosts=["testserver"],
                                   providers={}))
    h = {"X-Kelvin-Token": settings.access_key}
    with client:                                                  # Kelvin starts: the check runs in the background
        t0 = time.time()
        while rt.incident is None and time.time() - t0 < 5:
            time.sleep(0.05)
        card = client.get("/api/state", headers=h).json()["incident"]
        assert card["outputs"] == ["smu"] and any(line.startswith("left on:") and "smu.output on" in line for line in card["lines"])
        assert rt.idle_off_s == 60 * 60                           # the Settings default
        r = client.post("/api/incident", json={"action": "safe"}, headers=h).json()
        assert r["ok"] and rt.lab.read("smu", "output") is False
        assert client.post("/api/incident", json={"action": "dismiss"}, headers=h).json()["ok"]
        assert client.get("/api/state", headers=h).json()["incident"] is None
    [safe] = [e for e in rt.gate.log.events() if e["type"] == "incident_safe"]
    assert safe["by"] == "human (portal)"
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function incidentCard(i)" in js and "renderAlerts(s.alarms, s.incident);" in js
