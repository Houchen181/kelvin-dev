"""Auto mode from the people's side: the portal and approval page switch it; no agent tool can."""
import json

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")
anyio = pytest.importorskip("anyio")

from mcp import Client  # noqa: E402

from simlab import build_virtual_lab, demo_envelope  # noqa: E402
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from simlab.demo import DemoProvider  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from test_agent import call, http, page, runtime  # noqa: E402,F401  (fixtures and helpers)
from test_web import client_for, stream_turn  # noqa: E402


def test_no_agent_tool_can_switch_the_mode(runtime):  # noqa: F811
    async def main():
        async with Client(build_server(runtime)) as client:
            return [t.name for t in (await client.list_tools()).tools]
    names = anyio.run(main)
    assert not [n for n in names if "mode" in n or "auto" in n or "approv" in n]
    (status,) = call(runtime, ("lab_status", {}))
    assert "auto mode" not in status
    runtime.set_approval_mode("auto")
    (status,) = call(runtime, ("lab_status", {}))
    assert "approvals: auto mode (a person turned it on)" in status


def test_mode_starts_as_ask_and_is_logged(tmp_path):
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, WebApprover(timeout=5), tmp_path, demo_envelope(lab))
    assert rt.approval_mode()["mode"] == "ask" and rt.ui_state()["approval_mode"] == "ask"
    info = rt.set_approval_mode("auto", "human (test)")
    assert info["mode"] == "auto" and "25% of the way" in info["rule"]
    assert "smu.V -5 … 5 V" in info["rule"] and "Never automatic: laser" in info["rule"]
    with pytest.raises(ValueError):
        rt.set_approval_mode("yolo")
    assert any("approvals: auto mode on" in e for e in rt.ui_state()["events"])
    again = LabRuntime(build_virtual_lab(seed=1), WebApprover(timeout=5), tmp_path, demo_envelope(lab))
    assert again.approval_mode()["mode"] == "ask"   # a restart always comes back asking


def test_approval_page_switches_the_mode(runtime, page):  # noqa: F811
    assert http(page, "/api/mode", {"mode": "auto"}, token=False)[0] == 401
    assert http(page, "/api/mode", {"mode": "maybe"})[0] == 400
    code, body = http(page, "/api/mode", {"mode": "auto"})
    assert code == 200 and json.loads(body)["mode"] == "auto"
    state = json.loads(http(page, "/api/state")[1])
    assert state["approval_mode"] == "auto" and "lab limit" in state["approval_rule"]
    assert "(auto-approved)" in runtime.set("smu", "V", 2)
    history = json.loads(http(page, "/api/state")[1])["history"]
    assert history[0]["answer"] == "approved" and history[0]["by"] == "auto mode"


def test_portal_auto_mode_runs_the_demo_sweep_without_a_human(tmp_path):
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    parts = (rt, Settings(tmp_path / "settings.json"), ChatStore(tmp_path / "chats"))
    client, headers = client_for(parts, {"demo": DemoProvider()})
    with client:
        assert client.get("/api/mode", headers=headers).json()["mode"] == "ask"
        assert client.post("/api/mode", json={"mode": "auto"}).status_code == 401   # people only, with the key
        assert client.post("/api/mode", json={"mode": "auto"}, headers=headers).json()["mode"] == "auto"
        chat = client.post("/api/chats", json={"model": "demo:demo"}, headers=headers).json()
        events = stream_turn(client, headers, chat["id"], "sweep the gate", "demo:demo")   # nobody answers
        auto = [e for e in events if e["type"] == "approval"]
        assert len(auto) == 1 and auto[0]["auto"] is True and auto[0]["title"].startswith("Run plan gate_sweep")
        assert "the dry run finished cleanly" in auto[0]["reasons"]
        end = next(e for e in events if e["type"] == "tool_end")
        assert end["run_id"] and "done: " in end["content"]
        client.post("/api/mode", json={"mode": "ask"}, headers=headers)
        assert client.get("/api/state", headers=headers).json()["approval_mode"] == "ask"
