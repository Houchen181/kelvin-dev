"""The MCP server and the human approval channel. Needs Python >= 3.10 and `mcp`."""
import json
import re
import threading
import time
import urllib.error
import urllib.request

import pytest

mcp = pytest.importorskip("mcp")
anyio = pytest.importorskip("anyio")

from mcp import Client  # noqa: E402

from labharness import build_virtual_lab, demo_envelope  # noqa: E402
from labharness.agent.approval import ApprovalServer, WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402

GATE_SWEEP = """
plan: gate_sweep
setup: { lockin.amplitude: 0.05, smu.output: on }
sweep: { smu.V: {from: 0, to: 5, step: 0.25} }
settle: 0.5
measure: [lockin.X, smu.I]
after: { smu.V: 0 }
"""


@pytest.fixture
def runtime(tmp_path):
    lab = build_virtual_lab(seed=1)
    return LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))


def human(approver, answers):
    """A person at the approval page, answering requests in order."""
    def run():
        for answer in answers:
            entry = approver.wait_for_pending(timeout=5)
            if entry is None:
                return
            approver.answer(entry["id"], answer)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def call(runtime, *calls):
    """Run tool calls over a real MCP client session; return their text results."""
    async def main():
        out = []
        async with Client(build_server(runtime)) as client:
            for name, args in calls:
                result = await client.call_tool(name, args)
                out.append("\n".join(c.text for c in result.content if getattr(c, "type", "") == "text"))
        return out
    return anyio.run(main)


def test_tools_are_listed_and_none_can_approve(runtime):
    async def main():
        async with Client(build_server(runtime)) as client:
            return (await client.list_tools()).tools
    tools = {t.name: t for t in anyio.run(main)}
    assert {"lab_status", "describe_instrument", "get_value", "set_value", "send_raw",
            "validate_plan", "simulate_plan", "run_plan", "analyze_run", "get_run_data", "get_trace",
            "recent_events"} <= set(tools)
    assert not [n for n in tools if "approve" in n or "answer" in n]   # approvals only come from the page
    assert tools["get_value"].annotations.read_only_hint is True
    assert tools["set_value"].annotations.read_only_hint is False


def test_measurement_needs_no_human(runtime):
    status, tc = call(runtime, ("lab_status", {}), ("set_value", {"instrument": "lockin", "parameter": "tc", "value": 0.3}))
    assert status.startswith("Virtual Lab (simulated)") and "smu: V 0 V" in status
    assert tc == "lockin.tc 100 ms → 300 ms"
    assert runtime.approver.history_list() == []


def test_drive_waits_for_the_human(runtime):
    human(runtime.approver, [True])
    (r,) = call(runtime, ("set_value", {"instrument": "smu", "parameter": "V", "value": 3}))
    assert r.startswith("smu.V 0 V → 3 V") and "(approved)" in r
    assert runtime.approver.history_list()[0]["by"] == "human"


def test_unanswered_request_is_declined_with_a_reason(runtime):
    runtime.approver.timeout = 0.2
    (r,) = call(runtime, ("set_value", {"instrument": "smu", "parameter": "V", "value": 3}))
    assert r == "declined: smu.V stays 0 V (no answer within 0 s)"


def test_plan_over_mcp(runtime):
    human(runtime.approver, [True])
    validate, run = call(runtime, ("validate_plan", {"plan": GATE_SWEEP}),
                         ("run_plan", {"plan": GATE_SWEEP, "dry_run": True}))   # a dry run, because asked for
    assert "ok: gate_sweep, 21 points" in validate and "needs approval: smu.V" in validate
    assert "dry run: run" in run and "done: 21 points" in run
    center = re.search(r"assessment: lockin\.X has a peak at smu\.V = ([\d.]+) ± ", run)
    assert center and abs(float(center.group(1)) - 1.5) < 0.01 and "\nnext: " in run
    assert "smu.I [linear]" in run and "resistance (1/slope)" in run
    run_id = run.split("run_id=")[1].strip("')")
    (data,) = call(runtime, ("get_run_data", {"run_id": run_id, "columns": ["smu.V", "lockin.X"], "max_rows": 5}))
    lines = data.splitlines()
    assert lines[0].startswith(run_id + ": 21 rows") and lines[1] == "smu.V,lockin.X"
    assert lines[-1].startswith("5,")                      # the last row is always included


def test_analyze_run_refits_on_request(runtime):
    human(runtime.approver, [True])
    run, = call(runtime, ("run_plan", {"plan": GATE_SWEEP}))
    run_id = run.split("run_id=")[1].strip("')")
    gauss, wrong_model, wrong_column = call(
        runtime, ("analyze_run", {"run_id": run_id, "column": "lockin.X", "model": "gaussian"}),
        ("analyze_run", {"run_id": run_id, "model": "spline"}),
        ("analyze_run", {"run_id": run_id, "column": "smu.Q"}))
    assert gauss.startswith(run_id + ": 21 rows") and "gaussian fit" in gauss and "smu.I" not in gauss
    assert wrong_model.startswith("unknown model 'spline'")
    assert wrong_column.startswith("unknown column 'smu.Q'")


def test_trace_with_frequency_axis(runtime):
    plan = "plan: s21\nsetup: {vna.rf: on, vna.points: 101}\nmeasure: [vna.S21]\n"
    run, = call(runtime, ("run_plan", {"plan": plan}))
    run_id = run.split("run_id=")[1].strip("')")
    (trace,) = call(runtime, ("get_trace", {"run_id": run_id, "row": 0, "column": "vna.S21", "max_points": 11}))
    lines = trace.splitlines()
    assert "101 points" in lines[0] and lines[1] == "x,y"
    assert lines[2].startswith("3990000000,") and lines[-1].startswith("4010000000,")


def test_plan_path_is_not_read_from_disk(runtime, tmp_path):
    secret = tmp_path / "secret.yaml"
    secret.write_text("plan: leak\n", encoding="utf-8")
    (r,) = call(runtime, ("validate_plan", {"plan": str(secret)}))
    assert r.startswith("invalid plan: a plan must be a YAML mapping")


def test_events_tool(runtime):
    call(runtime, ("set_value", {"instrument": "lockin", "parameter": "tc", "value": 0.3}))
    (events,) = call(runtime, ("recent_events", {"n": 5}))
    assert "lockin.tc 100 ms → 300 ms" in events


# --- the approval page ---------------------------------------------------------------------

@pytest.fixture
def page(runtime):
    server = ApprovalServer(runtime, port=0).start()
    yield server
    server.stop()


def http(page, path, body=None, token=True, host=None, origin=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (page.port, path),
                                 data=json.dumps(body).encode() if body is not None else None)
    if token:
        req.add_header("X-Kelvin-Token", page.token)
    if host:
        req.add_header("Host", host)
    if origin:
        req.add_header("Origin", origin)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_page_needs_the_access_key(page):
    assert http(page, "/", token=False)[0] == 200                 # the page itself holds no secrets
    assert http(page, "/api/state", token=False)[0] == 401
    assert http(page, "/api/answer", {"id": "r1", "approve": True}, token=False)[0] == 401
    assert http(page, "/api/state", host="evil.example:80")[0] == 403
    assert http(page, "/api/stop", {}, origin="http://evil.example")[0] == 403


def test_human_approves_through_the_page(runtime, page):
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("r", runtime.set("smu", "V", 2)))
    t.start()
    entry = runtime.approver.wait_for_pending(timeout=5)
    state = json.loads(http(page, "/api/state")[1])
    assert state["pending"][0]["items"][0].startswith("smu.V 0 V → 2 V")
    assert http(page, "/api/answer", {"id": entry["id"], "approve": True})[0] == 200
    t.join(5)
    assert "(approved)" in result["r"]
    state = json.loads(http(page, "/api/state")[1])
    v = [p for p in state["status"]["smu"] if p["name"] == "V"][0]
    assert v == {"name": "V", "value": "2 V", "away": True}


def test_stop_declines_waiting_requests_and_leaves_drives_alone(runtime, page):
    human(runtime.approver, [True])
    runtime.set("smu", "output", True)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("r", runtime.set("smu", "V", 2)))
    t.start()
    runtime.approver.wait_for_pending(timeout=5)
    status, body = http(page, "/api/stop", {})
    t.join(5)
    assert status == 200 and json.loads(body) == {"ok": True, "declined": 1}
    assert result["r"] == "declined: smu.V stays 0 V (declined by stop button)"
    assert runtime.lab.read("smu", "output") is True          # nothing is sent to safe
    assert http(page, "/api/safe_state", {})[0] == 404
    assert any("stopped the current ramp or plan; declined 1 open request(s)" in e
               for e in json.loads(http(page, "/api/state")[1])["events"])


def test_no_tool_sends_every_drive_to_safe(runtime):
    async def main():
        async with Client(build_server(runtime)) as client:
            return [t.name for t in (await client.list_tools()).tools]
    assert "safe_state" not in anyio.run(main)
