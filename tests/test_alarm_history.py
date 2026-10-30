"""Alarms you can rely on overnight (UX test 2026-10-30, report section 3.3): a gate's leakage trip is an alarm; alarms
that cleared in the night stay until someone says Got it, who did is kept, and a removed watch's alarm clears; a
reading at its threshold raises one alarm, not ten; a sample's watches end with it; a wait that gives up raises the
alarm the prompt promised, and a heater left off is said at once; Kelvin says where alarms reach people; and the
phone's Stop ends a queue between its scans."""
import json
import threading
import time

import pytest

from labharness import EventLog, SafetyGate, ScriptedApprover
from labharness.clock import RealClock
from labharness.monitor import FOLD_S, Monitor, Watch
from labharness.scanqueue import ScanQueue
from simlab import build_virtual_lab

GATE_SWEEP = """plan: {name}
setup: {{ smu.output: on }}
sweep: {{ smu.V: {{from: 0, to: {to}, step: 0.5}} }}
measure: [smu.I]
after: {{ smu.V: 0, smu.output: off }}"""


def runtime(tmp_path, answers=(), lab=None):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    return LabRuntime(lab or build_virtual_lab(seed=1), ScriptedApprover(list(answers)), tmp_path / "data", None)


def watching(lab, tmp_path, answers=()):
    gate = SafetyGate(lab, ScriptedApprover(list(answers)), EventLog(tmp_path / "events.jsonl", lab.clock))
    notices = []
    monitor = Monitor(lab, gate, lambda kind, title, text="": notices.append((kind, title, text)))
    return gate, monitor, notices


def reads(lab, temperature, dt=1.0):
    """Let lab time pass, then pin the sample temperature (the virtual cryostat would relax it back)."""
    lab.clock.sleep(dt)
    lab.read("tc", "T_sample")
    lab.instruments["tc"].backend.state["T_sample"] = temperature


# ---------------------------------------------------------------------------------------- a guard's trip is an alarm

def test_a_leakage_trip_in_the_night_is_an_alarm_that_stays_until_someone_says_got_it(tmp_path):
    """t05: the gate's leakage tripped at 12.05 V in the night; /api/state had no alarm, and the morning account said
    "No alarm was raised while it ran"."""
    rt = runtime(tmp_path, [True])
    rt.queue_add(GATE_SWEEP.format(name="wide_gate", to=13))
    assert rt.queue_start().startswith("queue started")
    rt._queue_thread.join(120)
    [trip] = [a for a in rt.monitor.alarms if a.kind == "trip"]
    assert trip.text.startswith("smu.V tripped at ") and "|smu.I| = " in trip.text and "back to 0 V" in trip.text
    assert trip.latched and trip.cleared is None and not trip.acknowledged
    alarm_notices = [n for n in rt.notifier.notices if n["kind"] == "alarm"]
    assert [n["title"] for n in alarm_notices] == ["Alarm: " + trip.text]           # told at once, wherever it goes
    summary = rt.queue.last["lines"]
    assert "No alarm was raised" not in "\n".join(summary)
    assert any(line.startswith("Alarms while it ran: ") and "smu.V tripped at" in line for line in summary)
    for _ in range(3):                                                              # latched: no reading clears it
        rt.lab.clock.sleep(30)
        rt.monitor.tick()
    assert trip.cleared is None and "ALARM: smu.V tripped at" in rt.lab_status()
    assert [a["id"] for a in rt.ui_state()["alarms"] if not a["cleared"] and not a["acknowledged"]] == [trip.id]
    assert rt.acknowledge_alarm(trip.id, "human (phone Ana's phone)", "phone", "Ana's phone")
    assert trip.cleared and trip.acknowledged_by == "a person (phone, Ana's phone)"
    assert "Got it from a person (phone, Ana's phone)" in trip.cleared_text


def test_a_dry_run_that_trips_raises_no_alarm(lab, make_engine):
    """A rehearsal on the simulated lab is not the lab: its trips are said in its result, not raised as alarms."""
    engine, gate, _ = make_engine([])
    monitor = Monitor(lab, gate, lambda *a: None)
    report = engine.simulate(GATE_SWEEP.format(name="rehearsed", to=13))
    assert "tripped" in report.message and not monitor.alarms


# --------------------------------------------------------- the history: kept until Got it, who said it, removed watches

def test_an_alarm_that_cleared_overnight_stays_until_got_it_and_who_said_it_is_kept(tmp_path):
    pytest.importorskip("starlette")
    from test_stop import portal
    rt = runtime(tmp_path)
    rt.add_watch("tc.T_sample", above=20, note="leak")
    reads(rt.lab, 25.0)
    rt.monitor.tick()
    reads(rt.lab, 2.0, 15)
    rt.monitor.tick()
    [alarm] = [a for a in rt.monitor.alarms if a.kind == "reading"]
    assert alarm.cleared and "back in range" in alarm.cleared_text and not alarm.acknowledged
    assert "ALARM (cleared, nobody has said Got it yet): " in rt.lab_status()
    client, headers, _ = portal(tmp_path, rt)
    with client:
        [shown] = [a for a in client.get("/api/state", headers=headers).json()["alarms"] if a["id"] == alarm.id]
        # what the iPhone app 0.1.7 reads is all there, as it was; the history is added beside it
        assert {"id", "kind", "key", "text", "actions", "raised", "cleared", "acknowledged"} <= set(shown)
        assert shown["cleared"] and not shown["acknowledged"] and shown["count"] == 1
        assert shown["cleared_text"].startswith("tc.T_sample is back in range")
        assert client.post("/api/alarms/%s/ack" % alarm.id, json={}, headers=headers).json() == {"ok": True}
        [shown] = [a for a in client.get("/api/state", headers=headers).json()["alarms"] if a["id"] == alarm.id]
        assert shown["acknowledged"] and shown["acknowledged_by"] == "a person (portal)" and shown["acknowledged_at"]
    [ack] = [e for e in rt.gate.log.events() if e["type"] == "alarm_ack"]
    assert ack["by"] == "human (portal)" and ack["channel"] == "portal" and ack["text"] == alarm.text
    assert "a person (portal) said Got it to the alarm: tc.T_sample = 25 K" in rt.events(5)
    kept = json.loads((rt.data_dir / "alarms.json").read_text(encoding="utf-8"))["alarms"]
    assert kept[0]["acknowledged_by"] == "a person (portal)"                       # and across a restart
    again = runtime(tmp_path, lab=rt.lab)
    assert again.monitor.alarms[0].acknowledged_by == "a person (portal)"


def test_removing_a_watch_clears_its_alarm(tmp_path):
    """t05: a watch acknowledged and removed kept its alarm on (cleared: null), and it came back after a restart."""
    rt = runtime(tmp_path)
    w = rt.add_watch("tc.T_sample", below=1.9)
    rt.monitor.tick()
    [alarm] = rt.monitor.active()
    rt.acknowledge_alarm(alarm.id)
    rt.remove_watch(w["id"])
    assert alarm.cleared and "the watch was removed" in alarm.cleared_text and rt.monitor.active() == []
    assert "ALARM" not in rt.lab_status()
    assert runtime(tmp_path, lab=rt.lab).alarms_before == []                       # nothing "on when Kelvin stopped"


def test_the_portal_keeps_every_unseen_alarm_beyond_the_latest_ones(tmp_path):
    rt = runtime(tmp_path)
    w = rt.add_watch("tc.T_sample", above=20)
    reads(rt.lab, 25.0)
    rt.monitor.tick()
    first = rt.monitor.alarms[0]
    reads(rt.lab, 2.0)
    rt.monitor.tick()
    for n in range(25):   # 25 others since, each seen
        a = rt.monitor._new("error", "x%d" % n, "error %d" % n)
        rt.monitor.acknowledge(a.id)
    shown = [a["id"] for a in rt.alarms_view()]
    assert len(shown) == 21 and shown[-1] == first.id                               # the unseen one is still there
    assert w["id"] in [v["id"] for v in rt.monitor.list()]


# ------------------------------------------------------------------- hysteresis and repeats folded into one alarm

def test_a_noisy_reading_at_its_threshold_raises_one_alarm_not_ten(lab, tmp_path):
    """t09 round 2: a set point of 5 K against a watch "above 5 K" raised 10 alarms, each a notice and a push, in 90 s on
    +-4 mK of noise. It clears only once back past its threshold by more than its noise."""
    gate, monitor, notices = watching(lab, tmp_path)
    w = monitor.add(Watch("tc.T_sample", above=5.0, every=1))
    for k in range(90):
        reads(lab, 5.004 if k % 2 else 4.996)
        monitor.tick()
    assert len(monitor.alarms) == 1 and [n[0] for n in notices] == ["alarm"]
    assert monitor.alarms[0].cleared is None and w.band() >= 0.05                 # 1% of the threshold at least
    reads(lab, 4.97)
    monitor.tick()
    assert monitor.alarms[0].cleared is None                                       # inside the band: still on
    reads(lab, 4.9)
    monitor.tick()
    assert monitor.alarms[0].cleared and notices[-1][0] == "alarm_cleared"


def test_an_alarm_that_comes_back_before_anyone_looked_folds_into_one_with_a_count(lab, tmp_path):
    gate, monitor, notices = watching(lab, tmp_path)
    monitor.add(Watch("tc.T_sample", above=20, every=1))
    for _ in range(3):   # out, back well inside for a while, out again, within minutes
        reads(lab, 25.0, 30)
        monitor.tick()
        for _ in range(4):
            reads(lab, 2.0, 30)
            monitor.tick()
    [alarm] = monitor.alarms
    assert alarm.count == 3 and alarm.last >= alarm.raised and alarm.cleared
    assert [n[0] for n in notices] == ["alarm", "alarm_cleared"]                    # told once, not three times
    assert [e["type"] for e in gate.log.events()].count("alarm_again") == 2        # the record has every one
    monitor.acknowledge(alarm.id, "Ada (portal)")
    reads(lab, 25.0, 30)
    monitor.tick()                                                                 # seen: a new one goes up, and is told
    assert len(monitor.alarms) == 2 and monitor.alarms[0].count == 1 and notices[-1][0] == "alarm"
    reads(lab, 2.0, 30)
    monitor.tick()
    reads(lab, 25.0, FOLD_S + 60)                                                  # much later: a new alarm again
    monitor.tick()
    assert len(monitor.alarms) == 3 and notices[-1][0] == "alarm"


# ------------------------------------------------------------------------------------ a sample's watches end with it

def test_a_samples_watches_end_when_it_is_taken_out_and_kelvin_says_so(tmp_path):
    """t09 round 2: "Acme-HfO2-D1 must stay below 5 K" went on watching (and alarming) after D1 was unmounted."""
    rt = runtime(tmp_path)
    rt.mount(None, {"id": "d1", "name": "D1"}, by="human")
    mine = rt.add_watch("tc.T_sample", above=5, note="D1 must stay below 5 K")
    helium = rt.add_watch("tc.T_sample", above=250, lab_wide=True)
    assert mine["device"] == "D1" and helium["device"] == ""
    reads(rt.lab, 6.0)
    rt.monitor.tick()
    [alarm] = rt.monitor.active()
    m = rt.mount(None, {"id": "d2", "name": "D2"}, by="human")                    # the next sample goes in
    assert m["watches"] == ["1 watch made for D1 ended with it: tc.T_sample above 5 K"]
    assert [v["id"] for v in rt.monitor.list() if v["source"] != "lab"] == [helium["id"]]
    assert alarm.cleared and "D1 was taken out" in alarm.cleared_text
    assert rt.notifier.notices[-1]["title"] == "Watches ended with D1"
    assert any(e["type"] == "watch" and e.get("change") == "ended" for e in rt.gate.log.events())
    d2 = rt.add_watch("smu.I", above=1e-9)
    assert rt.unmount("human") == ["1 watch made for D2 ended with it: smu.I above 1e-09 A"]
    assert d2["id"] not in [v["id"] for v in rt.monitor.list()]


def test_a_watch_kept_for_a_sample_that_is_no_longer_mounted_does_not_come_back(tmp_path):
    lab, data = build_virtual_lab(seed=1), tmp_path / "data"
    data.mkdir(parents=True)
    (data / "watches.json").write_text(json.dumps({"labs": {lab.config.name: [
        {"read": "tc.T_sample", "above": 5, "device": "D1"}, {"read": "smu.I", "above": 1e-6}]}}), encoding="utf-8")
    rt = runtime(tmp_path, lab=lab)
    assert [v["rule"] for v in rt.monitor.list() if v["source"] != "lab"] == ["smu.I above 1e-06 A"]
    assert rt.restored["dropped"] == ["tc.T_sample (made for D1, which is no longer mounted)"]


# -------------------------------------------------------------- a wait that gives up is an alarm; a heater left off

def test_a_heater_left_off_is_said_at_once_and_the_set_point_put_back_in_words(lab, tmp_path):
    """t01, t09 round 2: with the heater off, setting 20 K waited 10 min, then said "the instrument may be stuck" and
    silently put the approved set point back to 1.6 K."""
    gate, monitor, notices = watching(lab, tmp_path, answers=[True])
    t0 = lab.clock.now()
    r = gate.set("tc", "setpoint", 20)
    assert not r.ok and lab.clock.now() - t0 < 60
    assert r.message.startswith("tc.setpoint: tc.heater_range is off, so nothing brings T_sample (")
    assert "set back to" in r.message and "not 20 K" in r.message and "switch tc.heater_range on first" in r.message
    assert "stuck" not in r.message and lab.read("tc", "setpoint") < 2


def test_a_wait_that_gives_up_raises_the_alarm_the_prompt_promised(lab, tmp_path):
    """t01: "A wait whose reading stops getting closer ends after 10 min, and an alarm says so": /api/state had none.
    It goes up even when the watchdog didn't look meanwhile, says the heater range, and stays until Got it."""
    gate, monitor, notices = watching(lab, tmp_path, answers=[True, True])
    assert gate.set("tc", "heater_range", "low").ok                               # tops out at 10 K
    r = gate.set("tc", "setpoint", 25)
    assert not r.ok and "has not got closer to 25 K for 10.0 min" in r.message
    assert "tc.heater_range is only low: a higher range may be needed" in r.message
    assert "tc.setpoint set back to 10 K, where T_sample is, not 25 K" in r.message
    [alarm] = monitor.alarms
    assert alarm.kind == "stall" and alarm.latched and alarm.text == r.message
    assert notices == [("alarm", "Alarm: " + r.message, "It stays on until a person says Got it.")]
    for _ in range(3):
        lab.clock.sleep(30)
        monitor.tick()
    assert alarm.cleared is None


def test_the_watchdogs_alarm_becomes_the_wait_that_gave_up(lab, tmp_path):
    """When the watchdog already raised it (after 5 min), the wait's end at 10 min is the same alarm, not a second."""
    gate, monitor, notices = watching(lab, tmp_path, answers=[True, True])
    assert gate.set("tc", "heater_range", "low").ok
    done = {}
    real_sleep = lab.clock.sleep

    def sleep(seconds):   # the monitor looks while the wait goes on, as its thread does
        real_sleep(seconds)
        if lab.clock.now() - done.setdefault("look", lab.clock.now()) >= 15:
            done["look"] = lab.clock.now()
            monitor.tick()
    lab.clock.sleep = sleep
    r = gate.set("tc", "setpoint", 25)
    lab.clock.sleep = real_sleep
    [alarm] = monitor.alarms
    assert alarm.kind == "stall" and alarm.latched and alarm.text == r.message and alarm.cleared is None
    assert [n[0] for n in notices] == ["alarm"]                                    # told once, at 5 min
    monitor.tick()
    lab.clock.sleep(60)
    monitor.tick()
    assert alarm.cleared is None and len(monitor.alarms) == 1


# ------------------------------------------------------------------------------- where alarms reach people, honestly

def test_kelvin_says_where_alarms_reach_people_and_promises_no_more(tmp_path):
    """t05: the prompt and the watch tool said alarms reach the person away from the lab, with no phone, email or
    webhook set up."""
    pytest.importorskip("mcp")
    from labharness.agent.server import INSTRUCTIONS
    from test_agent import call
    assert "even when" not in INSTRUCTIONS and "only through what is set up" in INSTRUCTIONS
    rt = runtime(tmp_path, [True])
    [said] = call(rt, ("watch", {"reading": "smu.I", "above": 1e-9}))
    assert "alarms show only in the portal on this computer: nothing is set up to reach anyone away from it" in said
    rt.queue_add(GATE_SWEEP.format(name="short", to=1))
    started = rt.queue_start()
    assert started.startswith("queue started") and "alarms show only in the portal" in started
    rt._queue_thread.join(60)
    rt.notifier.configure("https://ntfy.sh/kelvin-secret-topic", ["alarm"])
    rt.notifier.phones = lambda: 2
    reach = rt.alarms_reach()
    assert reach == ("alarms show in the portal on this computer and reach people away from it through the Kelvin app "
                     "on 2 paired phones (while it is open), the webhook (ntfy.sh)")
    assert "secret" not in reach
    rt.notifier.configure("https://ntfy.sh/kelvin-secret-topic", ["run"])          # alarms not ticked for it
    rt.notifier.phones = lambda: 0
    assert rt.alarms_reach().startswith("alarms show only in the portal")
    assert not rt.notifier.reaches_away("alarm") and rt.notifier.reaches_away("run")


# ------------------------------------------------------------------------- the phone's Stop between a queue's scans

def real_runtime(tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    return LabRuntime(build_virtual_lab(clock=RealClock(), seed=1), WebApprover(timeout=10), tmp_path / "data")


WAIT_STEP = "step: wait for the fridge\nwait: 600"
WATCH = "plan: next\ntime: { every: 30, points: 3 }\nmeasure: [smu.I]\n"


def queue_in_its_wait(rt):
    rt.queue_add(WAIT_STEP)
    rt.queue_add(WATCH)
    assert rt.queue_start().startswith("queue started")
    t0 = time.time()
    while rt.queue.items[0].status != "running" and time.time() - t0 < 5:
        time.sleep(0.05)
    time.sleep(0.3)
    assert rt.queue.items[0].status == "running" and (rt.live_view() or {}).get("status") != "running"


def paused(rt, seconds=5):
    t0 = time.time()
    while rt.queue.state != "paused" and time.time() - t0 < seconds:
        time.sleep(0.05)
    return rt.queue.state == "paused" and [i.status for i in rt.queue.items] == ["stopped", "waiting"]


@pytest.mark.parametrize("path, body", [("/api/stop", {}), ("/api/runs/stop", {})])
def test_the_phones_stop_ends_a_queue_waiting_between_its_scans(tmp_path, path, body):
    """t05: during a queue's wait step the phone's Stop (POST /api/runs/stop {}) answered ok: false and the queue went
    on to the next scan. The Stop for everything (POST /api/stop with no chat) ends it there too."""
    pytest.importorskip("starlette")
    from test_stop import portal
    rt = real_runtime(tmp_path)
    client, headers, _ = portal(tmp_path, rt)
    with client:
        queue_in_its_wait(rt)
        r = client.post(path, json=body, headers=headers)
        assert r.status_code == 200 and r.json()["ok"] is True
        assert paused(rt)
        assert rt.queue.items[0].message == "stopped while waiting" and rt.live is None


def test_a_stop_between_two_items_never_lets_the_next_one_start(tmp_path):
    """The queue picks its next item, then a Stop comes before the item begins: it never starts."""
    rt = runtime(tmp_path, [True])
    rt.queue_add(WAIT_STEP)
    item = rt.queue.items[0]
    since = rt.gate.stop_generation, rt.lab.stops.count   # as the queue had it when it picked the item
    rt.stop("human (phone Ana's phone)")
    ok, message = rt._queue_item(item, 1, {}, "human", since)
    assert not ok and message == "stopped before it started" and item.status == "stopped"


def test_the_phone_page_shows_the_queue_with_a_stop_and_every_alarm_until_got_it():
    pytest.importorskip("starlette")
    from labharness.web.app import STATIC
    js = (STATIC / "mobile.js").read_text(encoding="utf-8")
    assert 'filter((a) => !a.acknowledged)' in js and "!a.cleared && !a.acknowledged" not in js
    queue = js[js.index("const queued"):js.index('"No run in progress"')]
    assert 'api("/api/stop", {})' in queue
    portal_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "!a.cleared && !a.acknowledged" not in portal_js and "acknowledged_by" in portal_js
