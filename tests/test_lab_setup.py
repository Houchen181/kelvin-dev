"""Setting up real instruments from the portal: scan, draft a lab config, save and connect, switch back."""
import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")

from labharness import build_virtual_lab, builtin_registry, demo_envelope  # noqa: E402
from labharness.adapters import set_resource_manager  # noqa: E402
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.virtual_lab import build_lab, open_home_lab  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from test_hardware import Bus, Device  # noqa: E402
from test_web import Scripted, client_for  # noqa: E402


@pytest.fixture
def bench(tmp_path):
    bus = Bus({"GPIB0::24::INSTR": Device("keithley/2400", name="k"), "GPIB0::8::INSTR": Device("srs/sr830", name="l"),
               "ASRL9::INSTR": Device(silent=True)})
    set_resource_manager(bus)
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    parts = (runtime, Settings(tmp_path / "settings.json"), ChatStore(tmp_path / "chats"))
    yield parts, bus, tmp_path
    set_resource_manager(None)


def test_scan_draft_save_and_connect(bench):
    (runtime, settings, store), bus, home = bench
    client, headers = client_for((runtime, settings, store), {"scripted": Scripted([])})
    with client:
        view = client.get("/api/lab", headers=headers).json()
        assert view["info"]["kind"] == "virtual" and view["yaml"] == ""
        assert {"keithley/2400", "srs/sr830", "lakeshore/336"} <= {c["id"] for c in view["cards"]}
        assert not any(c["id"].startswith("virtual/") for c in view["cards"])

        found = {f["address"]: f for f in client.post("/api/instruments/scan", json={}, headers=headers).json()["found"]}
        assert found["GPIB0::24::INSTR"]["card_id"] == "keithley/2400" and found["GPIB0::24::INSTR"]["suggested"] == "smu"
        assert found["GPIB0::8::INSTR"]["suggested"] == "lockin" and found["ASRL9::INSTR"]["card_id"] is None

        items = [{"name": "smu", "card": "keithley/2400", "address": "GPIB0::24::INSTR"}]
        text = client.post("/api/instruments/draft", json={"items": items, "base": ""}, headers=headers).json()["yaml"]
        assert "smu:" in text and "tighten" in text
        more = [{"name": "smu", "card": "srs/sr830", "address": "GPIB0::8::INSTR"},
                {"name": "pre", "card": "srs/sr560", "address": "serial://COM9"}]
        text = client.post("/api/instruments/draft", json={"items": more, "base": text}, headers=headers).json()["yaml"]
        assert "smu2:" in text and "pre:" in text and text.count("tighten") >= 2      # kept, renamed, added
        assert client.post("/api/instruments/draft", json={"items": [{"name": "bad name", "card": "srs/sr830",
                                                                      "address": "x"}]}, headers=headers).status_code == 400

        bad = client.post("/api/lab", json={"yaml": "lab: x\ninstruments: {smu: {card: nope/nope}}"}, headers=headers)
        assert bad.status_code == 400 and "problems" in bad.json()["error"]
        assert not (home / "lab.yaml").exists()                                     # nothing saved, nothing switched

        bus.devices["ASRL9::INSTR"] = Device("srs/sr560", name="pre")
        text = text.replace('"serial://COM9"', '"ASRL9::INSTR"')
        r = client.post("/api/lab", json={"yaml": text}, headers=headers).json()
        assert r["ok"] and r["info"]["kind"] == "file" and all("ok" in line for line in r["health"]), r
        assert (home / "lab.yaml").read_text(encoding="utf-8") == text and settings.get("lab") == "mine"
        state = client.get("/api/state", headers=headers).json()
        assert state["simulated"] is False and state["lab_info"]["kind"] == "file"
        assert set(state["status"]) == {"smu", "smu2", "pre"}
        assert any("now controlling My lab" in e for e in state["events"])
        assert runtime.gate.auto is False                                            # approvals ask again

        r = client.post("/api/lab", json={"use": "virtual"}, headers=headers).json()
        assert r["info"]["kind"] == "virtual" and client.get("/api/state", headers=headers).json()["simulated"] is True
        r = client.post("/api/lab", json={"use": "mine"}, headers=headers).json()
        assert r["info"]["kind"] == "file" and r["name"] == "My lab"


def test_switching_waits_for_kelvin_to_be_idle(bench):
    (runtime, settings, store), bus, home = bench
    client, headers = client_for((runtime, settings, store), {"scripted": Scripted([])})
    with client:
        from labharness.web.agent import _Turn
        client.app.app.state.agent.turns["x"] = _Turn({"chat_id": "x", "model": "m"})   # Kelvin answering a chat
        assert client.post("/api/lab", json={"use": "virtual"}, headers=headers).status_code == 409
        assert client.post("/api/instruments/scan", json={}, headers=headers).status_code == 409
        del client.app.app.state.agent.turns["x"]


def test_health_check_catches_the_wrong_instrument_at_an_address(bench):
    (runtime, _, _), bus, home = bench
    lab = build_lab("lab: x\ninstruments:\n  smu: {card: keithley/2400, address: 'GPIB0::8::INSTR'}\n")
    (result,) = lab.health_check()
    assert not result.ok and "does not match card keithley/2400" in str(result)
    assert "Make your version" in str(result)                                   # or a variant: the lab's own card
    assert all("?" in c for c in bus.devices["GPIB0::8::INSTR"].sent)          # it only asked


def test_a_broken_home_lab_falls_back_to_the_virtual_lab(tmp_path):
    set_resource_manager(Bus({}))
    try:
        (tmp_path / "lab.yaml").write_text("lab: x\ninstruments:\n  smu: {card: keithley/2400, address: 'GPIB0::3::INSTR'}\n",
                                           encoding="utf-8")
        lab, envelope, info = open_home_lab(tmp_path)
        assert info["kind"] == "virtual" and "could not be connected" in info["error"] and envelope is not None
        lab, envelope, info = open_home_lab(tmp_path, "virtual")
        assert info["kind"] == "virtual" and info["error"] is None and info["has_file"] is True
    finally:
        set_resource_manager(None)


def test_no_agent_tool_changes_the_lab():
    import anyio
    from mcp import Client
    from labharness.agent.server import build_server
    lab = build_virtual_lab()
    rt = LabRuntime(lab, WebApprover(timeout=1), None or __import__("tempfile").mkdtemp(), None)

    async def main():
        async with Client(build_server(rt)) as client:
            return [t.name for t in (await client.list_tools()).tools]
    names = anyio.run(main)
    assert "scan_instruments" in names and not [n for n in names if "lab" in n and n != "lab_status"]
    assert builtin_registry().get("keithley/2400")


def test_kelvin_opens_before_connecting_the_persons_lab(bench):
    import time
    (_, settings, store), bus, home = bench
    (home / "lab.yaml").write_text("lab: Bench\ninstruments:\n"
                                   "  smu: {card: keithley/2400, address: 'GPIB0::24::INSTR'}\n"
                                   "  lockin: {card: srs/sr830, address: 'GPIB0::8::INSTR'}\n"
                                   "  gone: {card: srs/sr830, address: 'GPIB0::30::INSTR'}\n", encoding="utf-8")
    lab, envelope, info = open_home_lab(home, connect=False)
    assert info["kind"] == "file" and lab.connecting == ["smu", "lockin", "gone"]
    assert all(d.sent == [] for d in bus.devices.values())                # nothing asked yet: Kelvin opens at once
    assert "connecting" in lab.away("smu")
    runtime = LabRuntime(lab, WebApprover(timeout=5), home / "data")
    runtime.lab_info = info
    client, headers = client_for((runtime, settings, store), {"scripted": Scripted([])})
    with client:   # the portal is up; the instrument setup connects them, one after another
        for _ in range(100):
            if runtime.setup_status.get("state") == "done":
                break
            time.sleep(0.05)
        status = runtime.setup_status
        assert status["state"] == "done" and status["message"].startswith("Connected smu, lockin"), status
        assert "not connected: gone" in status["message"] and not lab.connecting
        assert lab.instruments["smu"].enabled and "gone" in lab.unavailable and runtime.lab is lab   # no virtual lab
        state = client.get("/api/state", headers=headers).json()
        assert state["simulated"] is False and set(state["status"]) >= {"smu", "lockin"}
