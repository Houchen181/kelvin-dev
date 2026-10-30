"""The in-process sequence service (labharness/sequence/service.py): the vendored labMaster engine as
a library in Kelvin's process (V1_SPEC.md A).

Every test runs in a temporary home (~/Kelvin replaced by tmp_path/home): the engine's data root is
`<home>/sequence`, with a config/state of its own. Nothing here reaches the labMaster on 8770, a
network or this PC's real ~/Kelvin - and one test proves no socket is opened at all.
"""
import inspect
import json
import time

import pytest

from labharness.sequence import paths as sequence_paths
from labharness.sequence import service as sequence_service
from labharness.web.settings import Settings


@pytest.fixture
def home(tmp_path, monkeypatch):
    """(home, service, settings) with the engine's root under tmp_path; the one service closed after."""
    monkeypatch.delenv("KELVIN_SEQUENCE_DIR", raising=False)
    directory = tmp_path / "Kelvin"
    directory.mkdir()
    settings = Settings(tmp_path / "Kelvin" / "settings.json")
    sequence_service.reset_service()
    svc = sequence_service.service(directory, settings)
    try:
        yield directory, svc, settings
    finally:
        sequence_service.reset_service()


def wait_for(svc, done, timeout=10.0):
    """Poll the engine's state until done(state) is true (a sim block ends in a fraction of a second)."""
    end = time.monotonic() + timeout
    state = svc.state().get("state") or {}
    while time.monotonic() < end:
        state = svc.state().get("state") or {}
        if done(state):
            return state
        time.sleep(0.02)
    raise AssertionError("the engine never reached the expected state: %r" % (state.get("run_state"),))


# --- it is in-process, and it is the engine ------------------------------------------------------


def test_ping_is_in_process_and_simulated(home):
    _, svc, _ = home
    answer = svc.ping()
    assert answer["ok"] is True and answer["error"] is None
    assert answer["url"] == "in-process"
    assert answer["version"] == "0.1.7"
    assert answer["sim"] is True           # the engine's safe default


def test_state_has_no_url_and_is_the_engines_snapshot(home):
    directory, svc, _ = home
    answer = svc.state()
    assert answer["ok"] is True and answer["url"] == "in-process"
    state = answer["state"]
    for key in ("sequence", "progress", "run_state", "saved", "history_runs", "paths", "sim"):
        assert key in state
    assert state["sim"] is True
    assert state["sequence"]["name"]                 # the engine's default date-time name
    assert state["panel_version"] == "0.1.7"
    assert sequence_paths.PANEL_DIR == str(directory / "sequence")


def test_the_data_root_is_made_with_blocks_state_scripts_and_no_network(home, monkeypatch):
    directory, svc, _ = home

    def no_network(*args, **kwargs):
        raise AssertionError("the sequence engine must not open a socket")

    # Ping/state/palette/saved below would fail if anything here reached the network (urllib opens one).
    monkeypatch.setattr("socket.socket", no_network)
    monkeypatch.setattr("socket.create_connection", no_network)
    assert svc.ping()["ok"] is True
    assert svc.state()["ok"] is True
    root = directory / "sequence"
    assert (root / "state").is_dir() and (root / "data").is_dir()
    assert (root / "scripts").is_dir() and (root / "notes").is_dir()
    assert (root / "scripts" / "README.md").is_file()
    palette = json.loads((root / "blocks.json").read_text(encoding="utf-8"))["palette"]
    assert [entry["name"] for entry in palette] == ["Wait", "Safe stop"]
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    assert config["paths"]["safe_stop_script"] == ""


def test_the_modules_are_the_vendored_engine_with_no_http_code(home):
    """The service and the portal's facade contain no HTTP client: the 8770 port is gone."""
    from labharness.web import labmaster as facade
    for module in (sequence_service, facade):
        source = inspect.getsource(module)
        for word in ("urllib", "http.client", "requests", "urlopen", "http://127.0.0.1:8770"):
            assert word not in source, "%s still mentions %r" % (module.__name__, word)


# --- the sequence (writes) -----------------------------------------------------------------------


def test_sequence_writes_and_their_shapes(home):
    _, svc, _ = home
    added = svc.seq_add(block="Wait")                      # by name, as the v0 bridge allowed
    assert added["ok"] is True and added["error"] is None
    assert added["message"] == "Added Wait."
    item = added["state"]["sequence"]["items"][0]
    uid = item["uid"]

    assert svc.seq_add(block="no such function")["ok"] is False
    assert svc.seq_add(block="")["ok"] is False
    assert svc.seq_move("", 0)["ok"] is False and svc.seq_move(uid, "later")["ok"] is False
    assert svc.seq_update("")["ok"] is False
    assert svc.seq_update(uid)["ok"] is False              # nothing to change

    updated = svc.seq_update(uid, params={"minutes": 2}, name="First wait")
    assert updated["ok"] is True
    assert updated["state"]["sequence"]["items"][0]["name"] == "First wait"

    copied = svc.seq_duplicate(uid)
    assert copied["ok"] is True and copied["message"].startswith("Copied it as ")
    assert len(copied["state"]["sequence"]["items"]) == 2

    moved = svc.seq_move(uid, 1)
    assert moved["ok"] is True and moved["state"]["sequence"]["items"][1]["uid"] == uid

    deleted = svc.seq_delete(uid)
    assert deleted["ok"] is True
    assert len(deleted["state"]["sequence"]["items"]) == 1   # the copy is still there

    assert svc.seq_rename("")["ok"] is False
    renamed = svc.seq_rename("laser check")
    assert renamed["ok"] is True and renamed["message"] == "Renamed the sequence."
    assert svc.state()["state"]["sequence"]["name"] == "laser check"

    cleared = svc.seq_clear()
    assert cleared["ok"] is True and cleared["state"]["sequence"]["items"] == []


def test_palette_and_saved_sequences(home):
    _, svc, _ = home
    library = svc.palette()
    assert library["ok"] is True and library["rev"]
    assert [entry["name"] for entry in library["palette"]] == ["Wait", "Safe stop"]
    assert svc.scripts()["scripts"] == []
    assert svc.saved() == {"ok": True, "error": None, "saved": []}

    svc.seq_add(block="Wait")
    assert svc.seq_add(block={"id": "blk_wait", "name": "Wait", "kind": "wait", "params": {"minutes": 1}})["ok"]
    saved = svc.saved_save("night run")
    assert saved["ok"] is True and saved["entry"]["name"] == "night run"
    assert svc.saved_save("night run")["ok"] is False       # no overwrite without being asked
    assert svc.saved_save("night run", overwrite=True)["ok"] is True
    assert [s["name"] for s in svc.saved()["saved"]] == ["night run"]

    svc.seq_clear()
    loaded = svc.saved_load(name="night run")
    assert loaded["ok"] is True and loaded["message"] == 'Loaded saved sequence "night run".'
    assert len(svc.state()["state"]["sequence"]["items"]) == 2

    deleted = svc.saved_delete(name="night run")
    assert deleted["ok"] is True and deleted["message"] == 'Deleted saved sequence "night run".'
    assert svc.saved_delete(name="night run")["ok"] is False


def test_import_script_puts_a_script_into_the_library(home, tmp_path):
    directory, svc, _ = home
    assert svc.ping()["ok"] is True                  # the engine makes its data folders on first use
    script = directory / "sequence" / "scripts" / "example_measurement.py"
    script.write_text("def main(n_points=21):\n    return n_points\n", encoding="utf-8")
    listed = svc.scripts()
    assert listed["ok"] is True
    assert [s["name"] for s in listed["scripts"]] == ["example_measurement.py"]
    assert listed["scripts"][0]["has_main"] is True
    assert listed["scripts"][0]["main"] == "n_points=21"

    imported = svc.import_script("scripts/example_measurement.py")
    assert imported["ok"] is True, imported
    assert imported["entry"]["name"] == "example_measurement"
    assert imported["entry"]["id"]                            # the engine gave it an id
    assert [entry["name"] for entry in svc.palette()["palette"]].count("example_measurement") == 1
    assert "Added" in imported["message"]

    again = svc.import_script("scripts/example_measurement.py")
    assert again["ok"] is True and "already had 1 function" in again["message"]

    added = svc.seq_add(block_id=imported["entry"]["id"])
    assert added["ok"] is True and added["state"]["sequence"]["items"][0]["kind"] == "script"

    refused = svc.import_script("scripts/not_there.py")
    assert refused["ok"] is False and "not a .py file" in refused["error"]
    assert svc.import_script("")["ok"] is False


# --- the arm switch: stored in Kelvin's settings, never in the engine's data ---------------------


def test_arm_switch_is_off_by_default_and_persists_in_settings(home):
    directory, svc, settings = home
    assert svc.armed() is False
    assert settings.get("lab_sequence_ai_start") is None            # off is also "not stored"
    assert settings.lab_sequence_ai_start is False

    answer = svc.set_armed(True)
    assert answer == {"ok": True, "error": None, "armed": True, "message": "AI may start runs."}
    assert svc.armed() is True
    stored = json.loads((directory / "settings.json").read_text(encoding="utf-8"))
    assert stored["lab_sequence_ai_start"] == "on"
    # a second reader of the same file (Kelvin restarted) sees it
    assert Settings(directory / "settings.json").lab_sequence_ai_start is True

    assert svc.set_armed(False)["ok"] is True
    assert svc.armed() is False
    assert "lab_sequence_ai_start" not in json.loads((directory / "settings.json").read_text(encoding="utf-8"))

    # the second v1-A switch is stored off (v1-A: read by no tool yet)
    assert settings.sequence_tools_wait_for_lab is False


def test_arm_switch_without_settings_object_is_only_in_memory(tmp_path):
    sequence_service.reset_service()
    try:
        svc = sequence_service.service(tmp_path / "Kelvin")       # no Settings: a test-only service
        assert svc.armed() is False
        assert svc.set_armed(True)["ok"] is True and svc.armed() is True
    finally:
        sequence_service.reset_service()


# --- Kelvin's runs and the merged History --------------------------------------------------------


def test_record_kelvin_runs_and_merged_history(home):
    _, svc, _ = home
    first = svc.record_kelvin_run("laser check", kind="plan", status="done", detail="SIM, 3 blocks",
                                  started=time.time() - 5)
    assert first["ok"] is True and first["run"]["source"] == "kelvin"
    assert first["run"]["run_id"] and first["run"]["finished"] is not None
    queued = svc.record_kelvin_run("real measurement--2026-10-30", kind="script", status="queued")
    assert queued["ok"] is True and queued["run"]["finished"] is None

    runs = svc.kelvin_runs()
    assert [r["name"] for r in runs] == ["laser check", "real measurement--2026-10-30"]
    assert svc.record_kelvin_run("")["ok"] is True                  # a name is never empty
    assert svc.kelvin_runs()[-1]["name"] == "measurement"

    merged = svc.merged_runs(10)
    assert merged["ok"] is True
    assert {r["source"] for r in merged["runs"]} == {"kelvin"}
    assert merged["runs"][0]["name"] == "measurement"               # newest first
    assert all(key in merged["runs"][0] for key in ("source", "name", "status", "started", "detail"))


def test_merged_history_has_a_panel_run_after_a_sim_sequence(home):
    _, svc, _ = home
    svc.seq_add(block="Safe stop")                                  # in sim this ends at once
    svc.seq_rename("history check")
    started = svc.control("start")
    assert started["ok"] is True, started.get("error")
    wait_for(svc, lambda state: state["run_state"] not in ("running", "stopping"))
    state = svc.state()["state"]
    assert state["history_runs"] >= 1
    svc.record_kelvin_run("kelvin side", kind="measurement")
    merged = svc.merged_runs(10)
    sources = [run["source"] for run in merged["runs"]]
    assert "panel" in sources and "kelvin" in sources
    panel_rows = [run for run in merged["runs"] if run["source"] == "panel"]
    assert panel_rows[0]["status"] == "done"
    assert panel_rows[0]["n_blocks"] == 1

    # stop is a no-op when nothing runs, and safe stop works in sim (its script is skipped)
    stopped = svc.control("stop")
    assert stopped["ok"] is True
    safe = svc.safe_stop("test")
    assert safe["ok"] is True and "Safe stop" in safe["message"]


def test_kelvin_runs_are_capped_at_200(home):
    _, svc, _ = home
    for n in range(205):
        assert svc.record_kelvin_run("run %d" % n)["ok"] is True
    runs = svc.kelvin_runs()
    assert len(runs) == 200
    assert runs[-1]["name"] == "run 204" and runs[0]["name"] == "run 5"
    # the file is JSON, whole and readable (atomic write: no half file to see)
    stored = json.loads((home[0] / "sequence" / "state" / "kelvin_runs.json").read_text(encoding="utf-8"))
    assert len(stored["runs"]) == 200


def test_ensure_sequence_name_only_replaces_the_default_name(home):
    _, svc, _ = home
    default = svc.state()["state"]["sequence"]["name"]
    assert default == time.strftime("%Y-%m-%d %H:%M") or len(default) == 16
    named = svc.ensure_sequence_name("real measurement--2026-10-30")
    assert named["ok"] is True and named["renamed"] is True
    assert named["name"] == "real measurement--2026-10-30"
    assert svc.state()["state"]["sequence"]["name"] == "real measurement--2026-10-30"

    again = svc.ensure_sequence_name("another name")
    assert again["ok"] is True and again["renamed"] is False
    assert again["name"] == "real measurement--2026-10-30"          # a person's name is never replaced
    assert svc.ensure_sequence_name("")["ok"] is False

    svc.seq_rename(default)                                        # a default name again
    assert svc.ensure_sequence_name("laser check")["renamed"] is True


# --- failures are answers, never exceptions ------------------------------------------------------


def test_an_engine_that_cannot_start_answers_errors_instead_of_raising(tmp_path, monkeypatch):
    monkeypatch.delenv("KELVIN_SEQUENCE_DIR", raising=False)
    blocked = tmp_path / "Kelvin"
    blocked.mkdir()
    (blocked / "sequence").write_text("not a folder", encoding="utf-8")   # the root cannot be made
    sequence_service.reset_service()
    try:
        svc = sequence_service.service(blocked)
        ping = svc.ping()
        assert ping["ok"] is False and ping["error"] and ping["url"] == "in-process"
        assert svc.state() == {"ok": False, "url": "in-process", "error": ping["error"], "state": None}
        assert svc.palette() == {"ok": False, "error": ping["error"], "palette": [], "rev": ""}
        assert svc.scripts() == {"ok": False, "error": ping["error"], "scripts": []}
        assert svc.saved() == {"ok": False, "error": ping["error"], "saved": []}
        assert svc.history() == {"ok": False, "error": ping["error"], "runs": []}
        assert svc.merged_runs()["runs"] == []
        write = svc.seq_add(block="Wait")
        assert write["ok"] is False and write["state"] is None and write["message"] == ""
        assert svc.armed() is False and svc.set_armed(True)["armed"] is True   # settings still work
        svc.close()                                                            # never raises
    finally:
        sequence_service.reset_service()


def test_close_stops_the_engine_and_a_new_service_starts_again(tmp_path, monkeypatch):
    monkeypatch.delenv("KELVIN_SEQUENCE_DIR", raising=False)
    sequence_service.reset_service()
    try:
        svc = sequence_service.service(tmp_path / "Kelvin")
        assert svc.ping()["ok"] is True
        svc.close()
        svc.close()                                     # one call does the work; a second is harmless
        assert svc.ping()["ok"] is False and "closed" in svc.ping()["error"]
        assert svc.state()["state"] is None
        svc2 = sequence_service.service(tmp_path / "Kelvin")
        assert svc2 is not svc and svc2.ping()["ok"] is True
    finally:
        sequence_service.reset_service()


def test_service_returns_the_one_instance_and_reset_makes_a_new_one(tmp_path, monkeypatch):
    monkeypatch.delenv("KELVIN_SEQUENCE_DIR", raising=False)
    sequence_service.reset_service()
    try:
        first = sequence_service.service(tmp_path / "Kelvin")
        assert sequence_service.service() is first
        assert sequence_service.service(tmp_path / "Kelvin") is first
        sequence_service.reset_service()
        second = sequence_service.service(tmp_path / "Kelvin")
        assert second is not first and second.ping()["ok"] is True
    finally:
        sequence_service.reset_service()


# --- the complete History page: /api/history/all and /api/history/panel-run (v1-C) ---------------


@pytest.fixture
def history_app(tmp_path, monkeypatch):
    """A portal (tmp home) with helpers to add Kelvin run folders and a real panel run.

    A run folder's meta is faked (rundata.meta): a real run.h5 needs a measuring gate. The app and
    its engine are real, so /api/history/all merges what the routes and the service actually build.
    """
    pytest.importorskip("starlette")
    pytest.importorskip("httpx")
    pytest.importorskip("mcp")
    from types import SimpleNamespace

    from starlette.testclient import TestClient

    from simlab import build_virtual_lab, demo_envelope
    from labharness import rundata
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.web.app import create_app
    from labharness.web.store import ChatStore

    monkeypatch.delenv("KELVIN_SEQUENCE_DIR", raising=False)
    sequence_service.reset_service()
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    runs_dir = runtime.data_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    meta: dict = {}
    monkeypatch.setattr(rundata, "meta", lambda folder: dict(meta.get(str(folder.name), {})))
    settings = Settings(tmp_path / "settings.json")
    app = create_app(runtime, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"])
    client = TestClient(app)
    headers = {"X-Kelvin-Token": settings.access_key}

    def add_run(run_id, status="done", points=3, planned=3, reports=()):
        folder = runs_dir / run_id
        folder.mkdir(parents=True, exist_ok=True)
        if status is not None:
            meta[run_id] = {"status": status, "points": points, "planned_points": planned}
        for name in reports:
            (folder / name).write_bytes(b"%PDF-1.4 a tiny report")
        return folder

    def add_panel_run(name="history page check"):
        svc = sequence_service.service()
        assert svc.seq_add(block="Safe stop")["ok"] is True
        assert svc.seq_rename(name)["ok"] is True
        assert svc.control("start")["ok"] is True
        wait_for(svc, lambda state: state["run_state"] not in ("running", "stopping"))
        return svc

    try:
        yield SimpleNamespace(client=client, headers=headers, runtime=runtime, add_run=add_run,
                              add_panel_run=add_panel_run, runs_dir=runs_dir, tmp_path=tmp_path)
    finally:
        client.close()
        sequence_service.reset_service()


def history_get(history_app, query=""):
    path = "/api/history/all" + ("?" + query if query else "")
    return history_app.client.get(path, headers=history_app.headers).json()


HISTORY_ENTRY_KEYS = {"source", "name", "status", "started", "at", "finished", "detail", "run_id",
                      "blocks", "dataset", "folder", "report_files", "summary"}


def test_history_all_is_empty_and_ok_with_no_data(history_app):
    answer = history_get(history_app)
    assert answer == {"ok": True, "runs": [], "total": 0, "offset": 0, "has_more": False,
                      "counts": {"kelvin": 0, "panel": 0}}


def test_history_all_paginates_kelvin_runs_newest_first(history_app):
    for hour, status in ((10, "done"), (11, "done"), (12, "failed"), (13, "done"), (14, "done")):
        history_app.add_run("20261030-%02d0000-run%d" % (hour, hour), status=status,
                            points=3, planned=3)
    page = history_get(history_app, "limit=2")
    assert page["ok"] is True and page["total"] == 5 and page["offset"] == 0
    assert page["has_more"] is True and page["counts"] == {"kelvin": 5, "panel": 0}
    assert [run["run_id"] for run in page["runs"]] == ["20261030-140000-run14",
                                                       "20261030-130000-run13"]
    entry = page["runs"][0]
    assert set(entry) == HISTORY_ENTRY_KEYS
    assert entry["source"] == "kelvin" and entry["name"] == "run14"
    assert entry["status"] == "done" and entry["started"] > 0 and entry["at"]
    assert entry["at"].startswith("2026-10-30T14:00:00") and ("+" in entry["at"] or "-04:00" in entry["at"])
    assert entry["finished"] is None and entry["run_id"] == "20261030-140000-run14"
    assert entry["blocks"] is None and entry["dataset"] is None
    assert entry["folder"].endswith("20261030-140000-run14")
    assert entry["report_files"] == [] and entry["summary"] == ""

    second = history_get(history_app, "limit=2&offset=2")
    assert [run["run_id"] for run in second["runs"]] == ["20261030-120000-run12",
                                                         "20261030-110000-run11"]
    assert second["runs"][0]["status"] == "failed"
    assert second["has_more"] is True and second["offset"] == 2

    last = history_get(history_app, "limit=2&offset=4")
    assert [run["run_id"] for run in last["runs"]] == ["20261030-100000-run10"]
    assert last["has_more"] is False and last["total"] == 5

    past = history_get(history_app, "limit=10&offset=50")
    assert past == {"ok": True, "runs": [], "total": 5, "offset": 50, "has_more": False,
                    "counts": {"kelvin": 5, "panel": 0}}


def test_history_all_filters_source_status_and_q(history_app):
    history_app.add_run("20261030-100000-laser check", status="done", points=3, planned=3)
    history_app.add_run("20261030-110000-gate sweep", status="failed", points=2, planned=3)
    history_app.add_run("20261030-120000-overnight", status=None, points=None, planned=None)

    kelvin = history_get(history_app, "source=kelvin")
    assert kelvin["counts"] == {"kelvin": 3, "panel": 0} and kelvin["total"] == 3
    assert all(run["source"] == "kelvin" for run in kelvin["runs"])

    panel = history_get(history_app, "source=panel")
    assert panel["counts"] == {"kelvin": 3, "panel": 0}       # counts are the tab badges
    assert panel["total"] == 0 and panel["runs"] == []

    done = history_get(history_app, "status=done")
    assert [run["name"] for run in done["runs"]] == ["laser check"]
    failed = history_get(history_app, "status=failed")
    assert [run["name"] for run in failed["runs"]] == ["gate sweep"]
    unfinished = history_get(history_app, "status=unfinished")
    assert [run["name"] for run in unfinished["runs"]] == ["overnight"]

    assert [run["name"] for run in history_get(history_app, "q=LASER")["runs"]] == ["laser check"]
    assert [run["name"] for run in history_get(history_app, "q=2/3")["runs"]] == ["gate sweep"]
    assert history_get(history_app, "q=nothing like this")["total"] == 0

    bad = history_app.client.get("/api/history/all?source=galaxy", headers=history_app.headers)
    assert bad.status_code == 400 and bad.json()["ok"] is False


def test_history_all_merges_a_panel_run_after_a_kelvin_run(history_app):
    history_app.add_run("20261030-100000-laser check", status="done", points=3, planned=3)
    history_app.add_panel_run("history page check")
    answer = history_get(history_app)
    assert answer["counts"] == {"kelvin": 1, "panel": 1}
    assert answer["total"] == 2
    sources = [run["source"] for run in answer["runs"]]
    assert sources == ["panel", "kelvin"]                     # the panel run is the newer one
    panel = answer["runs"][0]
    assert set(panel) == HISTORY_ENTRY_KEYS
    assert panel["name"] == "history page check"
    assert panel["status"] == "done" and panel["blocks"] == 1
    assert panel["started"] > answer["runs"][1]["started"]
    assert panel["report_files"] == [] and panel["folder"] is None
    assert panel["run_id"] and panel["dataset"] is None

    only_panel = history_get(history_app, "source=panel&limit=1")
    assert only_panel["total"] == 1 and only_panel["runs"][0]["source"] == "panel"


def test_history_report_files_and_their_url_serves_the_file(history_app):
    history_app.add_run("20261030-100000-laser-check", reports=("report.pdf", "notes.pptx",
                                                              "my notes.pdf", "skip me.txt"))
    answer = history_get(history_app, "source=kelvin")
    files = answer["runs"][0]["report_files"]
    assert [item["name"] for item in files] == ["report.pdf", "notes.pptx"]   # report.pdf first
    report = next(item for item in files if item["name"] == "report.pdf")
    assert report["url"] == "/api/runs/20261030-100000-laser-check/files/report.pdf"
    assert report["size"] == len(b"%PDF-1.4 a tiny report")
    downloaded = history_app.client.get(report["url"], headers=history_app.headers)
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("application/pdf")
    assert downloaded.content.startswith(b"%PDF-1.4")


def test_panel_runs_carry_the_dataset_path_and_detail(history_app):
    svc = sequence_service.service()
    assert svc.ping()["ok"] is True                            # the engine the app made
    engine = svc._engine
    engine.history = [{"run_id": "run_abc", "run_name": "overnight", "n": 1, "uid": "u1",
                       "name": "sweep", "kind": "script", "status": "done", "message": "",
                       "started": 1793389567.0, "finished": 1793389600.0,
                       "params": {"reps": 1}, "overrides": {},
                       "datasets": ["C:\\Data\\2026-10-30\\#001_sweep"]}]
    runs = svc.panel_runs()
    assert len(runs) == 1
    assert runs[0]["source"] == "panel" and runs[0]["status"] == "done"
    assert runs[0]["dataset"] == "C:/Data/2026-10-30/#001_sweep"
    assert runs[0]["blocks"] == 1 and runs[0]["detail"] == "1 block, 1 dataset"

    detail = svc.panel_run("run_abc")
    assert detail["ok"] is True and detail["run"]["blocks"][0]["params"] == {"reps": 1}
    assert detail["run"]["datasets"] == ["C:/Data/2026-10-30/#001_sweep"]
    assert svc.panel_run("nope")["ok"] is False
    assert svc.panel_runs()[0]["started"] == 1793389567.0
    assert svc.panel_runs()[0]["at"].startswith("2026-10-30T")


def test_history_panel_run_detail_route(history_app):
    history_app.add_panel_run("detail check")
    listed = history_get(history_app, "source=panel")
    run_id = listed["runs"][0]["run_id"]
    answer = history_app.client.get("/api/history/panel-run?id=" + run_id,
                                    headers=history_app.headers)
    assert answer.status_code == 200
    body = answer.json()
    assert body["ok"] is True
    run = body["run"]
    assert run["run_id"] == run_id and run["name"] == "detail check"
    assert run["n_blocks"] == 1 and len(run["blocks"]) == 1
    assert run["blocks"][0]["name"] == "Safe stop"
    assert isinstance(run["blocks"][0]["params"], dict)

    missing = history_app.client.get("/api/history/panel-run?id=run_missing",
                                     headers=history_app.headers)
    assert missing.status_code == 404
    assert missing.json()["ok"] is False and "no panel run" in missing.json()["error"]


def test_history_all_adds_a_record_with_no_run_folder(history_app):
    """Kelvin's own record of an AI measurement (state/kelvin_runs.json) with no run folder - a queued
    one, or one whose folder was cleaned up - is a "kelvin" row of the complete History too."""
    svc = sequence_service.service()
    recorded = svc.record_kelvin_run("queue only", kind="script", status="queued",
                                     detail="waiting for the sequence", run_id="kr_record_1",
                                     started=1793389800.0)
    assert recorded["ok"] is True

    answer = history_get(history_app, "source=kelvin")
    assert answer["counts"] == {"kelvin": 1, "panel": 0} and answer["total"] == 1
    entry = answer["runs"][0]
    assert set(entry) == HISTORY_ENTRY_KEYS
    assert entry["source"] == "kelvin" and entry["run_id"] == "kr_record_1"
    assert entry["name"] == "queue only" and entry["status"] == "queued"
    assert entry["started"] == 1793389800.0 and entry["at"].startswith("2026-10-30T")
    assert entry["finished"] is None and entry["detail"] == "waiting for the sequence"
    assert entry["folder"] is None and entry["dataset"] is None
    assert entry["report_files"] == [] and entry["summary"] == ""
    assert entry["blocks"] is None

    # the filters see it like any other row
    assert history_get(history_app, "status=queued")["total"] == 1
    assert history_get(history_app, "status=done")["total"] == 0
    assert [run["name"] for run in history_get(history_app, "q=WAITING")["runs"]] == ["queue only"]


def test_history_all_keeps_the_run_folder_row_over_its_record(history_app):
    """One measurement, one row: a record whose run_id is a run folder is that folder's row (it has
    the data and the reports); another record with no folder still gets its own row."""
    history_app.add_run("20261030-100000-laser-check", status="done", points=3, planned=3,
                        reports=("report.pdf",))
    svc = sequence_service.service()
    assert svc.record_kelvin_run("the record's name", status="done",
                                 run_id="20261030-100000-laser-check")["ok"] is True
    assert svc.record_kelvin_run("record only", status="failed", detail="script exited 3",
                                 run_id="kr_record_2")["ok"] is True

    answer = history_get(history_app, "source=kelvin")
    assert answer["counts"]["kelvin"] == 2 and answer["total"] == 2
    by_id = {run["run_id"]: run for run in answer["runs"]}
    assert set(by_id) == {"20261030-100000-laser-check", "kr_record_2"}
    folder_row = by_id["20261030-100000-laser-check"]
    assert folder_row["name"] == "laser-check"                  # the run folder, not the record's name
    assert folder_row["folder"] is not None
    assert [item["name"] for item in folder_row["report_files"]] == ["report.pdf"]
    record_row = by_id["kr_record_2"]
    assert record_row["name"] == "record only" and record_row["status"] == "failed"
    assert record_row["detail"] == "script exited 3"
    assert record_row["folder"] is None and record_row["report_files"] == []

    # a record that names the folder adds nothing, even under its own name
    assert history_get(history_app, "q=the record")["total"] == 0

