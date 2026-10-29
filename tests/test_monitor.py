"""Watching instruments: thresholds that must hold for a while, instruments that stop answering, the lab config's
watches that act (only toward safe), and telling people in the app and through a webhook."""
import json
import threading
from pathlib import Path

import pytest

from labharness import build_virtual_lab, builtin_registry, demo_envelope
from labharness.agent.approval import WebApprover
from labharness.agent.runtime import LabRuntime
from labharness.labconfig import ConfigError, load_lab_config
from labharness.monitor import FAILS_BEFORE_ALARM, Monitor, Watch
from labharness.notify import Notifier, webhook_request

VIRTUAL_LAB = Path(__file__).resolve().parents[1] / "src" / "labharness" / "data" / "virtual_lab.yaml"


def hold(lab, temperature, dt=0.0):
    """Let lab time pass, then pin the sample temperature (the virtual cryostat would relax it back)."""
    lab.clock.sleep(dt)
    lab.read("tc", "T_sample")
    lab.instruments["tc"].backend.state["T_sample"] = temperature


@pytest.fixture
def runtime(tmp_path):
    lab = build_virtual_lab(seed=1)
    return LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))


def test_a_reading_must_stay_out_of_range_before_it_alarms_once_and_then_clears(lab, make_gate):
    gate, _ = make_gate([])
    notices = []
    monitor = Monitor(lab, gate, lambda kind, title, text="": notices.append((kind, title, text)))
    watch = monitor.add(Watch("tc.T_sample", above=20, for_s=10, every=1))

    monitor.tick()
    assert not monitor.alarms and watch.value < 20
    hold(lab, 25.0, 1)
    monitor.tick()
    hold(lab, 25.0, 5)
    monitor.tick()
    assert not monitor.alarms                                      # out of range, but not for 10 s yet
    hold(lab, 25.0, 6)
    monitor.tick()
    assert len(monitor.alarms) == 1 and watch.alarm
    alarm = monitor.alarms[0]
    assert alarm.kind == "reading" and "above 20 K for 10s" in alarm.text and not alarm.actions
    assert notices[-1][0] == "alarm" and "tc.T_sample" in notices[-1][1]
    hold(lab, 25.0, 2)
    monitor.tick()
    assert len(monitor.alarms) == 1 and len(notices) == 1         # raised once, not on every read

    hold(lab, 2.0, 2)
    monitor.tick()
    assert alarm.cleared and watch.alarm is None and not monitor.active()
    assert notices[-1][0] == "alarm_cleared" and "back in range" in notices[-1][1]
    assert monitor.acknowledge(alarm.id) and alarm.acknowledged and not monitor.acknowledge("a-none")


def test_a_short_blip_does_not_alarm(lab, make_gate):
    gate, _ = make_gate([])
    monitor = Monitor(lab, gate, lambda *a: None)
    monitor.add(Watch("tc.T_sample", above=20, for_s=10, every=1))
    hold(lab, 25.0)
    monitor.tick()
    hold(lab, 2.0, 5)
    monitor.tick()
    hold(lab, 25.0, 6)
    monitor.tick()                                                 # out again, but the clock restarted
    assert not monitor.alarms


def test_watch_rules(lab, make_gate):
    gate, _ = make_gate([])
    monitor = Monitor(lab, gate, lambda *a: None)
    with pytest.raises(KeyError):
        monitor.add(Watch("tc.nothing", above=1))
    with pytest.raises(ValueError, match="above, below"):
        monitor.add(Watch("tc.T_sample"))
    with pytest.raises(ValueError, match="smaller"):
        monitor.add(Watch("tc.T_sample", above=1, below=2))
    w = monitor.add(Watch("smu.I", above=1e-6, below=-1e-6))
    assert monitor.list()[0]["rule"] == "smu.I above 1e-06 A or below -1e-06 A"
    monitor.remove(w.id, "agent")
    lab_watch = monitor.add(Watch("tc.T_sample", above=20, source="lab"))
    for who in ("agent", "person"):
        with pytest.raises(PermissionError, match="lab config"):
            monitor.remove(lab_watch.id, who)
    with pytest.raises(KeyError):
        monitor.remove("w-none", "person")


def test_an_instrument_that_stops_answering_alarms_after_a_few_tries(lab, make_gate):
    gate, _ = make_gate([])
    notices = []
    monitor = Monitor(lab, gate, lambda kind, title, text="": notices.append((kind, title)))
    monitor.add(Watch("lockin.X", above=1, every=0))
    lab.instruments["lockin"].backend.fault = "offline"
    for _ in range(FAILS_BEFORE_ALARM - 1):
        monitor.tick()
    assert not monitor.alarms                                      # one bad read is not an outage
    monitor.tick()
    assert monitor.alarms[0].kind == "silent"
    assert monitor.alarms[0].text == "lockin (Virtual Lock-in Amplifier) doesn't answer [lockin: offline]"
    monitor.tick()
    assert len(monitor.alarms) == 1
    lab.instruments["lockin"].backend.fault = None
    monitor.tick()
    assert monitor.alarms[0].cleared and notices[-1][1].startswith("Cleared: lockin answers again (it didn't for ")


def test_the_lab_owners_watch_stops_the_run_and_turns_the_laser_off(runtime):
    lab, gate = runtime.lab, runtime.gate
    [view] = runtime.monitor.list()
    assert view["source"] == "lab" and view["then"] == ["laser.emission off"]
    lab.instruments["laser"].backend.state.update(emission=True, shutter=True, power=10.0)
    generation = gate.stop_generation

    hold(lab, 25.0)
    runtime.monitor.tick()
    hold(lab, 25.0, 10)
    runtime.monitor.tick()
    [alarm] = runtime.monitor.active()
    assert gate.stop_generation == generation + 1                 # the running plan (if any) stops
    assert alarm.actions[0] == "stopped the running plan" and "emission" in alarm.actions[1]
    assert lab.read("laser", "emission") is False
    assert "power ramped to 0 mW first" in alarm.actions[1]       # switching off brings the power down gently
    assert lab.instruments["laser"].backend.state["shutter"] is True  # and nothing it was not told to touch
    state = runtime.ui_state()
    assert state["alarms"][0]["id"] == alarm.id and state["watches"][0]["alarm"]
    assert state["notices"][-1]["kind"] == "alarm"
    assert "ALARM: tc.T_sample" in runtime.events(5)
    assert "ALARM: tc.T_sample = " in runtime.lab_status()
    with pytest.raises(PermissionError):
        runtime.remove_watch(view["id"], source="person")


def test_a_lab_config_watch_may_only_send_drives_to_safe():
    text = VIRTUAL_LAB.read_text(encoding="utf-8")
    assert load_lab_config(text, builtin_registry()).watches[0]["then"] == [("laser", "emission", False)]
    with pytest.raises(ConfigError, match="safe value"):
        load_lab_config(text.replace("laser.emission: off", "laser.emission: on"), builtin_registry())
    with pytest.raises(ConfigError, match="not a drive"):
        load_lab_config(text.replace("laser.emission: off", "tc.T_sample: 1"), builtin_registry())
    with pytest.raises(ConfigError, match="numeric reading"):
        load_lab_config(text.replace("read: tc.T_sample", "read: laser.emission"), builtin_registry())
    with pytest.raises(ConfigError, match="above, below"):
        load_lab_config(text.replace("above: 20", "every: 5"), builtin_registry())


def test_people_and_kelvin_add_notify_only_watches(runtime):
    view = runtime.add_watch("smu.I", above=1e-7, for_s=-3, note="leak", source="person")
    assert view["then"] == [] and view["for_s"] == 0 and view["source"] == "person"
    with pytest.raises(KeyError):
        runtime.add_watch("smu.nothing", above=1)
    with pytest.raises(ValueError, match="numeric reading"):
        runtime.add_watch("laser.emission", above=0)
    assert "smu.I above 1e-07 A" in runtime.watches_text() and "[lab, ok]" in runtime.watches_text()
    runtime.remove_watch(view["id"], source="agent")
    assert len(runtime.monitor.list()) == 1
    assert "person: added watch" in runtime.events(5) and "agent: removed watch" in runtime.events(5)


def test_kelvin_watch_tools(runtime):
    pytest.importorskip("mcp")
    from test_agent import call
    lab_id = runtime.monitor.list()[0]["id"]
    added, listed, refused, unknown = call(
        runtime, ("watch", {"reading": "tc.T_sample", "below": 1.0, "for_seconds": 30}), ("watches", {}),
        ("unwatch", {"watch_id": lab_id}), ("watch", {"reading": "tc.nothing", "above": 1}))
    assert added.startswith("watching tc.T_sample below 1 K for 30s (id w")
    assert "[agent, ok]" in listed and "[lab, ok]" in listed
    assert refused.startswith("not removed") and "lab config" in refused
    assert unknown.startswith("not watched")
    mine = added.rsplit("id ", 1)[1].rstrip(")")
    assert call(runtime, ("unwatch", {"watch_id": mine}))[0] == "no longer watching (%s)" % mine


def test_webhook_formats():
    body, headers = webhook_request("https://ntfy.sh/my-topic", "Alarm: tc.T_sample — 25 K", "laser off")
    assert body == b"laser off" and headers["Title"] == "Alarm: tc.T_sample ? 25 K"   # headers must be ASCII
    body, headers = webhook_request("https://hooks.slack.com/services/x", "Run finished", "peak at 1.5 V")
    assert json.loads(body) == {"text": "**Run finished**\npeak at 1.5 V"} and headers["Content-Type"] == "application/json"
    assert json.loads(webhook_request("https://discord.com/api/webhooks/x", "t", "")[0]) == {"content": "t"}


def test_notifier_sends_only_what_the_user_chose():
    sent, arrived = [], threading.Event()

    def post(url, data, headers):
        sent.append((url, data))
        arrived.set()

    n = Notifier(post=post)
    n("alarm", "nothing set")
    assert not sent and n.notices[-1]["title"] == "nothing set"   # no webhook: the app shows it, nothing leaves
    with pytest.raises(ValueError):
        n.configure("ftp://example.org/x")
    n.configure("https://ntfy.sh/topic", ["alarm"])
    n("run", "Run finished: sweep")
    n("alarm", "Alarm: hot", "laser off")
    assert arrived.wait(5)
    assert sent == [("https://ntfy.sh/topic", b"laser off")]
    first = n.notices[0]["id"]
    assert [x["title"] for x in n.since(first)] == ["Run finished: sweep", "Alarm: hot"]
    assert n.test() == "sent"

    def broken(url, data, headers):
        raise OSError("no route to host")

    n = Notifier(post=broken)
    assert n.test() == "no webhook, email or heartbeat set"
    n.configure("https://ntfy.sh/topic")
    assert n.test() == "could not reach the webhook: no route to host"


def test_people_hear_about_approvals_and_finished_runs(runtime):
    def human():
        entry = runtime.approver.wait_for_pending(timeout=5)
        runtime.approver.answer(entry["id"], False)
    t = threading.Thread(target=human, daemon=True)
    t.start()
    runtime.set("smu", "V", 3)
    t.join(5)
    kinds = [(n["kind"], n["title"]) for n in runtime.notifier.notices]
    assert kinds and kinds[0][0] == "approval" and kinds[0][1].startswith("Approval needed")

    out = runtime.run("plan: quick\nsweep: {lockin.tc: {values: [0.1, 0.3]}}\nmeasure: [lockin.X]\n")
    assert "done" in out
    run = runtime.notifier.notices[-1]
    assert run["kind"] == "run" and run["title"] == "Run finished: quick"


def test_portal_watches_alarms_and_notification_settings(runtime, tmp_path):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from test_web import Scripted, client_for
    settings = Settings(tmp_path / "settings.json")
    client, headers = client_for((runtime, settings, ChatStore(tmp_path / "chats")), {"scripted": Scripted([])})
    with client:
        lab_id = client.get("/api/watches", headers=headers).json()["watches"][0]["id"]
        r = client.post("/api/watches", json={"key": "tc.T_sample", "above": "30", "below": "", "for_s": "5"},
                        headers=headers)
        assert r.status_code == 200 and r.json()["rule"] == "tc.T_sample above 30 K for 5s"
        assert client.post("/api/watches", json={"key": "tc.nope", "above": 1}, headers=headers).status_code == 404
        assert client.post("/api/watches", json={"key": "tc.T_sample"}, headers=headers).status_code == 400
        assert client.post("/api/watches", json={"key": "tc.T_sample", "above": "hot"}, headers=headers).status_code == 400
        assert client.delete("/api/watches/" + lab_id, headers=headers).status_code == 403
        assert client.delete("/api/watches/" + r.json()["id"], headers=headers).status_code == 200
        assert client.delete("/api/watches/" + r.json()["id"], headers=headers).status_code == 404
        assert client.post("/api/alarms/a-none/ack", json={}, headers=headers).status_code == 404

        state = client.get("/api/state", headers=headers).json()
        assert {"watches", "alarms", "notices"} <= set(state)
        assert client.get("/api/settings", headers=headers).json()["settings"]["notify_kinds"] == ["alarm", "run"]
        assert client.post("/api/notify/test", json={}, headers=headers).json()["result"] == "no webhook, email or heartbeat set"
        bad = client.post("/api/settings", json={"webhook": "javascript:alert(1)"}, headers=headers)
        assert bad.status_code == 400 and "http" in bad.json()["error"]
        secret = "https://ntfy.sh/kelvin-private-topic-123"
        r = client.post("/api/settings", json={"webhook": secret, "notify_kinds": "alarm,approval,bogus"}, headers=headers)
        assert r.status_code == 200 and secret not in r.text                       # a webhook URL is a secret
        assert r.json()["settings"]["notify_kinds"] == ["alarm", "approval"]
        assert runtime.notifier.webhook == secret and runtime.notifier.kinds == {"alarm", "approval"}
        assert secret not in client.get("/api/settings", headers=headers).text
        r = client.post("/api/settings", json={"notify_kinds": ""}, headers=headers)
        assert r.json()["settings"]["notify_kinds"] == [] and runtime.notifier.kinds == set()
