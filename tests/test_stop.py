"""A person's stop ends every long wait at once: a run's settle or interval between points, Kelvin's own wait, a ramp's
step. (A tool call an instrument holds up: test_web; a sweep an analyzer is still taking: test_vna.)"""
import json
import threading
import time

import pytest

from labharness import RunEngine, SafetyGate, ScriptedApprover, build_virtual_lab
from labharness.clock import RealClock, SimClock, Stops


def test_a_stop_wakes_a_sleep_and_simulated_time_passes_at_once():
    stops = Stops()
    count = stops.count
    threading.Timer(0.2, stops.pulse).start()
    t0 = time.time()
    assert stops.sleep(RealClock(), 30, count) is False and time.time() - t0 < 1
    assert stops.sleep(RealClock(), 0.05, stops.count) is True
    assert stops.sleep(RealClock(), 5, count) is False   # a stop that came before the sleep began
    clock = SimClock()
    assert stops.sleep(clock, 3600, stops.count) and clock.now() == 3600


def test_a_stop_ends_a_run_waiting_between_its_points():
    lab = build_virtual_lab(clock=RealClock(), seed=1)
    gate = SafetyGate(lab, ScriptedApprover([]))
    out = {}
    worker = threading.Thread(target=lambda: out.update(report=RunEngine(gate).run(
        "plan: watch\ntime: { every: 30, points: 3 }\nmeasure: [smu.I]\n")))
    t0 = time.time()
    worker.start()
    time.sleep(0.5)
    gate.request_abort()   # the stop button
    worker.join(5)
    assert not worker.is_alive() and time.time() - t0 < 3
    assert out["report"].status == "aborted" and "stop requested" in out["report"].message


def test_kelvins_wait_ends_at_a_stop(tmp_path):
    pytest.importorskip("mcp")
    from labharness import demo_envelope
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    lab = build_virtual_lab(clock=RealClock(), seed=1)
    rt = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    threading.Timer(0.3, rt.stop).start()
    t0 = time.time()
    said = rt.wait(60)
    assert time.time() - t0 < 3 and said.startswith("stopped after") and "a person pressed stop" in said


# --- stop paths outside the gate (field tests: typed STOP, confirm(), MCP cancel, queued calls, the queue) ------------

WATCH = "plan: watch\ntime: { every: 30, points: 3 }\nmeasure: [smu.I]\n"   # a minute of waiting between its points


def real_runtime(tmp_path, timeout=10):
    """A runtime on the virtual lab in real time, with no envelope: raising smu.V asks a person."""
    pytest.importorskip("mcp")
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    return LabRuntime(build_virtual_lab(clock=RealClock(), seed=1), WebApprover(timeout=timeout), tmp_path / "data")


def test_a_stop_also_ends_the_drive_calls_waiting_behind_the_one_it_stopped(tmp_path):
    """Stop declined only the request on show; the call lined up behind it then asked again (P10)."""
    rt = real_runtime(tmp_path)
    out = {}
    first = threading.Thread(target=lambda: out.update(first=rt.set("smu", "V", 3)))
    first.start()
    assert rt.approver.wait_for_pending(timeout=5)                     # the first call asks, holding the lab
    second = threading.Thread(target=lambda: out.update(second=rt.set("smu", "V", 2)))
    second.start()
    time.sleep(0.5)                                                    # the second waits for the lab
    rt.stop("human (portal)")
    first.join(5)
    second.join(5)
    assert out["first"].startswith("declined") and out["second"].startswith("not run: a person pressed Stop")
    assert rt.approver.wait_for_pending(timeout=1) is None             # nothing new to answer
    assert rt.lab.read("smu", "V") == 0
    assert rt.set("smu", "V", 0) and rt.approver.pending_list() == []  # what is asked after the stop runs as usual


def test_a_cancelled_mcp_call_is_declined_and_never_drives(tmp_path):
    """A cancelled call's approval stayed live, and a yes later drove the sample for a client that was gone (P10)."""
    import asyncio
    from mcp import Client
    from labharness.agent.server import build_server
    rt = real_runtime(tmp_path)

    async def day():
        async with Client(build_server(rt), mode="legacy") as client:   # JSON-RPC over streams, as over stdio
            call = asyncio.ensure_future(client.call_tool("set_value", {"instrument": "smu", "parameter": "V",
                                                                        "value": 3}))
            asked = await asyncio.to_thread(rt.approver.wait_for_pending, 5)
            assert asked and "smu" in asked["title"]
            call.cancel()                                                  # the client gives up on it
            await asyncio.sleep(0.5)
            assert rt.approver.pending_list() == []
            assert rt.approver.answer(asked["id"], True) is False          # too late to say yes to it
            [answered] = rt.approver.history_list()
            assert answered["answer"] == "declined" and "cancelled" in answered["by"]

            hold = threading.Event()   # a run has the lab: a call made now waits for it, then is cancelled
            busy = threading.Thread(target=lambda: rt._do("a run", lambda: hold.wait(5) and ""), daemon=True)
            busy.start()
            await asyncio.sleep(0.2)
            call = asyncio.ensure_future(client.call_tool("set_value", {"instrument": "smu", "parameter": "V",
                                                                        "value": 2}))
            await asyncio.sleep(0.5)
            call.cancel()
            await asyncio.sleep(0.3)
            hold.set()
            busy.join(5)
            assert await asyncio.to_thread(rt.approver.wait_for_pending, 1.5) is None   # it never asked

            call = asyncio.ensure_future(client.call_tool("set_value", {"instrument": "smu", "parameter": "V",
                                                                        "value": 1}))
            assert await asyncio.to_thread(rt.approver.wait_for_pending, 5)
        # the client went away (disconnected) while its call waited for a person
        await asyncio.sleep(0.5)
        assert rt.approver.pending_list() == []
    asyncio.run(day())
    time.sleep(0.5)
    assert rt.lab.read("smu", "V") == 0
    record = (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8")
    assert record.count('"call_cancelled"') == 2 and '"answer": "approved"' not in record


def test_a_person_stopping_a_queued_scan_pauses_the_queue_even_when_it_goes_on_after_errors(tmp_path):
    rt = real_runtime(tmp_path)
    rt.queue_set_on_error("continue")
    rt.queue_add(WATCH)
    rt.queue_add(WATCH.replace("plan: watch", "plan: next"))
    assert rt.queue_start().startswith("queue started")
    t0 = time.time()
    while not (rt.live_view() or {}).get("status") == "running" and time.time() - t0 < 5:
        time.sleep(0.05)
    assert rt.stop_run()                                               # the runs pane's Stop run
    while rt.queue.state != "paused" and time.time() - t0 < 10:
        time.sleep(0.05)
    assert rt.queue.state == "paused" and [i.status for i in rt.queue.items] == ["stopped", "waiting"]


def portal(tmp_path, rt, providers=None):
    pytest.importorskip("starlette")
    pytest.importorskip("httpx")
    from starlette.testclient import TestClient
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    settings, store = Settings(tmp_path / "settings.json"), ChatStore(tmp_path / "chats")
    app = create_app(rt, settings, store, hosts=["testserver"], providers=providers or {})
    return TestClient(app), {"X-Kelvin-Token": settings.access_key}, store


def queue_running(rt):
    rt.queue_add(WATCH)
    rt.queue_add(WATCH.replace("plan: watch", "plan: next"))
    assert rt.queue_start().startswith("queue started")
    t0 = time.time()
    while not (rt.live_view() or {}).get("status") == "running" and time.time() - t0 < 5:
        time.sleep(0.05)


def stopped(rt, seconds=5):
    t0 = time.time()
    while rt.queue.state != "paused" and time.time() - t0 < seconds:
        time.sleep(0.05)
    return rt.queue.state == "paused" and rt.queue.items[0].status == "stopped"


def test_the_stop_for_everything_ends_a_queue_that_runs_with_no_chat_answering(tmp_path):
    """POST /api/stop with no chat said ok: false and the queue kept running (l02)."""
    rt = real_runtime(tmp_path)
    client, headers, _ = portal(tmp_path, rt)
    with client:
        queue_running(rt)
        r = client.post("/api/stop", json={}, headers=headers)
        assert r.status_code == 200 and r.json()["ok"] is True
        assert stopped(rt)
    record = (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8")
    assert '"type": "stop"' in record and '"by": "human (portal)"' in record


@pytest.mark.parametrize("typed", ["STOP", "stop!", "Stop now", "abort", "/stop"])
def test_typing_stop_in_a_chat_stops_the_lab_at_once_without_asking_the_model(tmp_path, typed):
    """Typing STOP was read by the model at its next step, if ever, while the run went on (P11, P1)."""
    from labharness.web.agent import typed_stop
    assert typed_stop(typed) and not typed_stop("stop the sweep at 2 V next time") and not typed_stop("unstoppable")
    rt = real_runtime(tmp_path)

    class Never:   # a model that must not be asked
        name, label = "never", "Never"

        def available(self):
            return True

        async def models(self):
            return []

        async def stream(self, *a, **k):
            raise AssertionError("the model was asked")
    client, headers, store = portal(tmp_path, rt, {"never": Never()})
    with client:
        chat = store.create("never:m")
        queue_running(rt)
        r = client.post("/api/chats/%s/turn" % chat["id"], json={"text": typed, "model": "never:m"}, headers=headers)
        assert r.status_code == 200
        said = [json.loads(line) for line in r.text.splitlines() if line.strip()]
        assert said[-1] == {"type": "turn_end", "reason": "stopped"} and said[0]["text"].startswith("Stopped")
        assert stopped(rt)
        rt.queue_start()                                               # again, and stopped while it is answering
        assert client.post("/api/chats/%s/steer" % chat["id"], json={"text": typed}, headers=headers).json()["stopped"]
        assert stopped(rt)
    assert '"by": "human (portal, typed STOP)"' in (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8")


def test_no_dialog_stands_between_a_person_and_stop():
    """The portal's Stop sat behind a native confirm(), which a browser can suppress: then Stop did nothing (P1)."""
    from labharness.web.app import STATIC
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    run_stop = js[js.index('v.stop = h("button"'):js.index("v.count = h(")]
    hands = js[js.index("async function handsOn()"):js.index("function openUnlock()")]
    assert "confirm(" not in run_stop and "confirm(" not in hands
    assert "if (TYPED_STOP.test(text)) return hardStop();" in js
    phone = (STATIC / "mobile.js").read_text(encoding="utf-8")
    assert 'confirm("Stop' not in phone and 'confirm("Hands on' not in phone


def test_a_stop_during_a_queue_step_ends_it_there_and_pauses_the_queue(tmp_path):
    rt = real_runtime(tmp_path)
    rt.queue_set_on_error("continue")
    rt.queue_add("step: warm and field\nset: { smu.V: 2, magnet.B: 0.5 }\nwait: 600")
    rt.queue_add(WATCH)
    out = {}
    worker = threading.Thread(target=lambda: out.update(said=rt.queue_start()), daemon=True)
    worker.start()
    asked = rt.approver.wait_for_pending(timeout=5)                    # the queue's one approval
    rt.answer(asked["id"], True, "human (portal)", "portal")
    t0 = time.time()
    while rt.lab.read("smu", "V") == 0 and time.time() - t0 < 5:      # ramping at 0.5 V/s
        time.sleep(0.05)
    rt.stop("human (portal)")
    while rt.queue.state != "paused" and time.time() - t0 < 10:
        time.sleep(0.05)
    assert rt.queue.state == "paused" and [i.status for i in rt.queue.items] == ["stopped", "waiting"]
    assert rt.lab.read("magnet", "B") == 0 and rt.lab.read("smu", "V") < 2   # nothing after the stop
