"""The sequence engine's door in Kelvin (labharness/web/labmaster.py) and its portal routes.

Since v1-A the labMaster engine runs in Kelvin's own process (V1_SPEC.md): there is no 8770 panel
and no HTTP client. These tests drive the real vendored engine against a temporary home, so the
facade's names and answer shapes, the Home page's tiles and the /api/labmaster/* routes are the
ones Kelvin serves - with no labMaster process, no network, and no write into this PC's ~/Kelvin.
"""
import inspect
import json
import time

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")
pytest.importorskip("httpx")

from starlette.testclient import TestClient  # noqa: E402

from simlab import build_virtual_lab, demo_envelope  # noqa: E402
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.sequence import service as sequence_service  # noqa: E402
from labharness.web import labmaster  # noqa: E402
from labharness.web.app import create_app  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402


@pytest.fixture
def parts(tmp_path, monkeypatch):
    """(runtime, settings, home, client, headers): a real portal whose home - and engine - is tmp_path."""
    monkeypatch.delenv("KELVIN_SEQUENCE_DIR", raising=False)
    sequence_service.reset_service()
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    settings = Settings(tmp_path / "settings.json")
    store = ChatStore(tmp_path / "chats")
    app = create_app(runtime, settings, store, hosts=["testserver"])
    client = TestClient(app)
    headers = {"X-Kelvin-Token": settings.access_key}
    try:
        yield runtime, settings, tmp_path, client, headers
    finally:
        client.close()
        sequence_service.reset_service()


def api(client, headers, path, body=None):
    if body is None:
        return client.get(path, headers=headers).json()
    return client.post(path, headers=headers, json=body).json()


def api_delete(client, headers, path):
    return client.request("DELETE", path, headers=headers).json()


def wait_frontend_still(svc_call, done, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if done(svc_call()):
            return True
        time.sleep(0.02)
    return False


# --- the facade: the names and shapes the MCP tools and the page call ----------------------------


def test_panel_url_is_in_process():
    assert labmaster.panel_url() == "in-process"
    source = inspect.getsource(labmaster)
    for word in ("urllib", "urlopen", "http://127.0.0.1:8770", "KELVIN_LABMASTER_URL"):
        assert word not in source, "the facade still speaks HTTP: %r" % word


def test_reads_keep_their_v0_shapes(parts):
    _, _, _, client, headers = parts
    ping = labmaster.ping()
    assert set(ping) == {"ok", "sim", "url", "version", "error"}
    assert ping["ok"] is True and ping["url"] == "in-process" and ping["sim"] is True
    assert ping["version"] == "0.1.7"

    state = labmaster.state()
    assert set(state) == {"ok", "url", "error", "state"}
    assert state["ok"] is True and isinstance(state["state"], dict)

    palette = labmaster.palette()
    assert set(palette) == {"ok", "error", "palette", "rev"}
    assert [entry["name"] for entry in palette["palette"]] == ["Wait", "Safe stop"]

    scripts = labmaster.scripts()
    assert set(scripts) == {"ok", "error", "scripts"} and scripts["scripts"] == []

    saved = labmaster.saved()
    assert set(saved) == {"ok", "error", "saved"} and saved["saved"] == []

    history = labmaster.history(5)
    assert set(history) == {"ok", "error", "runs"} and history["runs"] == []


def test_writes_keep_their_v0_shapes(parts):
    _, _, _, client, headers = parts
    added = labmaster.seq_add(block="Wait")
    uid = added["state"]["sequence"]["items"][0]["uid"]
    answers = [added, labmaster.seq_update(uid, name="first"), labmaster.seq_duplicate(uid),
               labmaster.seq_move(uid, 1), labmaster.seq_delete(uid), labmaster.seq_clear(),
               labmaster.saved_save("one"), labmaster.saved_load(name="one"),
               labmaster.saved_delete(name="one"), labmaster.safe_stop("test")]
    for answer in answers:
        # v0's writes had "entry" in saved_save's answer as well: the four keys are always there
        assert {"ok", "error", "state", "message"} <= set(answer), answer
        assert answer["ok"] is True, answer
    assert "entry" in answers[6]

    refused = labmaster.seq_add(block="nothing")
    assert refused["ok"] is False and refused["state"] is None and refused["message"] == ""
    assert refused["error"].startswith("seq add: no function")

    assert labmaster.control("fly")["ok"] is False
    assert labmaster.control("")["ok"] is False


def test_import_script_end_to_end(parts):
    _, _, home, _, _ = parts
    assert labmaster.ping()["ok"] is True            # the engine makes its data folders on first use
    script = home / "sequence" / "scripts" / "example_measurement.py"
    script.write_text("def main(n_points=21):\n    return n_points\n", encoding="utf-8")
    answer = labmaster.import_script("scripts/example_measurement.py")
    assert answer["ok"] is True and answer["entry"]["name"] == "example_measurement"
    assert answer["palette"] and answer["message"].startswith('Added "example_measurement"')
    assert [entry["name"] for entry in labmaster.palette()["palette"]] == [
        "example_measurement", "Wait", "Safe stop"]


# --- v1-A: the arm switch, Kelvin's runs, and the Home page --------------------------------------


def test_armed_is_read_only_here_and_persisted_by_the_route(parts):
    _, settings, home, client, headers = parts
    assert labmaster.armed() is False
    assert api(client, headers, "/api/labmaster/armed") == {"ok": True, "armed": False}
    assert api(client, headers, "/api/labmaster/armed", {"armed": True}) == {
        "ok": True, "error": None, "armed": True, "message": "AI may start runs."}
    assert api(client, headers, "/api/labmaster/armed") == {"ok": True, "armed": True}
    assert labmaster.armed() is True
    stored = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert stored["lab_sequence_ai_start"] == "on"

    assert api(client, headers, "/api/labmaster/armed", {"armed": False})["armed"] is False
    assert api(client, headers, "/api/labmaster/armed", {"nope": 1})["ok"] is False


def test_record_and_merged_runs(parts):
    _, _, _, client, headers = parts
    assert labmaster.record_kelvin_run("laser check", kind="plan", detail="SIM")["ok"] is True
    runs = labmaster.kelvin_runs()
    assert [run["name"] for run in runs] == ["laser check"]
    assert api(client, headers, "/api/labmaster/kelvin-runs")["runs"][0]["source"] == "kelvin"

    merged = api(client, headers, "/api/labmaster/history?limit=5")
    assert merged["ok"] is True
    assert merged["runs"][0]["name"] == "laser check"
    assert merged["runs"][0]["source"] == "kelvin"
    assert labmaster.merged_runs(5)["runs"] == merged["runs"]

    assert labmaster.ensure_sequence_name("real measurement--2026-10-30")["renamed"] is True
    assert labmaster.state()["state"]["sequence"]["name"] == "real measurement--2026-10-30"


def test_a_sim_sequence_shows_in_the_merged_history(parts):
    _, _, _, client, headers = parts
    assert labmaster.seq_add(block="Safe stop")["ok"] is True
    assert labmaster.seq_rename("history check")["ok"] is True
    assert labmaster.control("start")["ok"] is True
    assert wait_frontend_still(
        lambda: labmaster.state()["state"],
        lambda state: state["run_state"] not in ("running", "stopping"))
    runs = api(client, headers, "/api/labmaster/history")["runs"]
    assert any(run["source"] == "panel" and run["status"] == "done" for run in runs)
    assert labmaster.control("stop")["ok"] is True


def test_home_page_tiles_come_from_the_in_process_engine(parts):
    _, _, home, client, headers = parts
    assert labmaster.seq_add(block="Safe stop")["ok"] is True
    page = api(client, headers, "/api/home")
    assert page["ok"] is True
    assert page["panel"]["ok"] is True and page["panel"]["url"] == "in-process"
    assert page["panel"]["version"] == "0.1.7"
    tiles = {tile["id"]: tile for tile in page["tiles"]}
    assert set(tiles) == {"measurement", "history", "saved", "functions", "panel"}
    assert tiles["panel"]["title"] == "Sequence engine"
    assert tiles["panel"]["value"] == "in-process"
    assert tiles["panel"]["detail"] == "labMaster engine v0.1.7"
    assert tiles["measurement"]["value"] == "0/1"
    assert "sequence engine" in page["greeting"]


def test_home_cards_are_kelvins_own_file(parts):
    _, _, home, client, headers = parts
    added = api(client, headers, "/api/home/cards", {"kind": "note", "title": "Laser check",
                                                     "text": "Align the beam first."})
    assert added["ok"] is True
    assert json.loads((home / "home.json").read_text(encoding="utf-8"))["cards"][0]["title"] == "Laser check"
    page = api(client, headers, "/api/home")
    assert [card["title"] for card in page["cards"]] == ["Laser check"]
    card_id = page["cards"][0]["id"]
    assert api_delete(client, headers, "/api/home/cards/" + card_id)["ok"] is True
    assert api(client, headers, "/api/home")["cards"] == []


# --- the routes ----------------------------------------------------------------------------------


def test_routes_answer_the_pages(parts):
    _, _, _, client, headers = parts
    state = api(client, headers, "/api/labmaster/state")
    assert state["ok"] is True and state["url"] == "in-process"
    assert state["state"]["panel_version"] == "0.1.7"

    assert api(client, headers, "/api/labmaster/palette")["rev"]
    assert api(client, headers, "/api/labmaster/scripts")["scripts"] == []
    assert api(client, headers, "/api/labmaster/saved")["saved"] == []

    added = api(client, headers, "/api/labmaster/seq", {"action": "add", "block": "Wait"})
    assert added["ok"] is True and len(added["state"]["sequence"]["items"]) == 1
    uid = added["state"]["sequence"]["items"][0]["uid"]
    renamed = api(client, headers, "/api/labmaster/seq", {"action": "rename", "name": "laser check"})
    assert renamed["ok"] is True and renamed["state"]["sequence"]["name"] == "laser check"
    assert api(client, headers, "/api/labmaster/seq", {"action": "update", "uid": uid,
                                                       "params": {"minutes": 3}})["ok"] is True
    assert api(client, headers, "/api/labmaster/seq", {"action": "duplicate", "uid": uid})["ok"] is True
    assert api(client, headers, "/api/labmaster/seq", {"action": "move", "uid": uid, "index": 1})["ok"] is True
    assert api(client, headers, "/api/labmaster/seq", {"action": "delete", "uid": uid})["ok"] is True
    assert api(client, headers, "/api/labmaster/seq", {"action": "clear", "keep_done": False})["ok"] is True
    assert api(client, headers, "/api/labmaster/seq", {"action": "fly"})["ok"] is False

    assert api(client, headers, "/api/labmaster/seq", {"action": "add", "block": "Safe stop"})["ok"] is True
    assert api(client, headers, "/api/labmaster/control", {"action": "start"})["ok"] is True
    assert wait_frontend_still(
        lambda: api(client, headers, "/api/labmaster/state")["state"] or {},
        lambda state: state.get("run_state") not in ("running", "stopping"))
    assert api(client, headers, "/api/labmaster/control", {"action": "stop"})["ok"] is True
    assert api(client, headers, "/api/labmaster/control", {"action": "safe_stop"})["ok"] is True
    assert api(client, headers, "/api/labmaster/control", {"action": "nope"})["ok"] is False

    saved = api(client, headers, "/api/labmaster/saved", {"action": "save", "name": "one"})
    assert saved["ok"] is True
    loaded = api(client, headers, "/api/labmaster/saved", {"action": "load", "name": "one", "replace": True})
    assert loaded["ok"] is True
    assert [s["name"] for s in api(client, headers, "/api/labmaster/saved")["saved"]] == ["one"]
    assert api(client, headers, "/api/labmaster/saved", {"action": "delete", "name": "one"})["ok"] is True
    assert api(client, headers, "/api/labmaster/saved", {"action": "fly"})["ok"] is False

    imported = api(client, headers, "/api/labmaster/import", {"path": "scripts/nope.py"})
    assert imported["ok"] is False and imported["entry"] is None


def test_the_api_is_still_behind_the_token(parts):
    _, _, _, client, _ = parts
    assert client.get("/api/labmaster/state").status_code in (401, 403)
    assert client.get("/api/labmaster/armed").status_code in (401, 403)
    assert client.post("/api/labmaster/armed", json={"armed": True}).status_code in (401, 403)
