"""The data folder (Settings -> Data & reports): moving it with what is saved, and Kelvin starting on it."""
import json

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")

from test_web import Scripted, client_for, parts  # noqa: E402,F401  (fixture)


def events(runtime):
    return [json.loads(line) for line in runtime.events_path.read_text(encoding="utf-8").splitlines()]


def test_the_data_folder_can_move_with_what_is_saved(parts, tmp_path):
    runtime, settings, store = parts
    runtime.engine.store.root.mkdir(parents=True, exist_ok=True)
    for name in ("20261001-120000-sweep", "20261002-120000-map"):
        (runtime.data_dir / "runs" / name).mkdir()
        (runtime.data_dir / "runs" / name / "meta.json").write_text("{}", encoding="utf-8")
    (runtime.data_dir / "reports").mkdir()
    (runtime.data_dir / "reports" / "week.pdf").write_bytes(b"%PDF-")
    runtime.gate.log.write("note", by="test")
    old = runtime.data_dir
    new = tmp_path / "lab server" / "Kelvin data"
    clash = new / "runs" / "20261002-120000-map"
    clash.mkdir(parents=True)                                     # a run of that name is there already

    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        now = client.get("/api/data-folder", headers=headers).json()
        assert now["path"] == str(old) and now["runs"] == 2 and now["reports"] == 1
        bad = client.post("/api/data-folder", json={"path": "relative/folder"}, headers=headers)
        assert bad.status_code == 400 and "full path" in bad.json()["error"]
        afile = tmp_path / "a-file"
        afile.write_text("x", encoding="utf-8")
        bad = client.post("/api/data-folder", json={"path": str(afile)}, headers=headers)
        assert bad.status_code == 400 and "cannot write" in bad.json()["error"]

        r = client.post("/api/data-folder", json={"path": str(new), "move": True}, headers=headers).json()
        assert r["path"] == str(new) and r["moved"] == 3 and r["kept"] == ["runs/20261002-120000-map"]
        assert (new / "runs" / "20261001-120000-sweep" / "meta.json").exists() and (new / "reports" / "week.pdf").exists()
        assert (old / "runs" / "20261002-120000-map").exists()                     # nothing overwritten
        assert settings.get("data_dir") == str(new)
        assert runtime.events_path == new / "events.jsonl"
        assert any(e["type"] == "data_folder" for e in events(runtime))
        assert any(e.get("type") == "note" for e in events(runtime))                # the record came along

        runtime.gate.raw("lockin", "*IDN?")
        assert any(e.get("target") == "lockin: *IDN?" for e in events(runtime))     # new commands land there
        assert [x["run_id"] for x in runtime.runs_index()] == ["20261002-120000-map", "20261001-120000-sweep"]

        back = tmp_path / "plain"
        r = client.post("/api/data-folder", json={"path": str(back)}, headers=headers).json()
        assert r["moved"] == 0 and (new / "runs" / "20261001-120000-sweep").exists()   # without move, data stay


def test_kelvin_starts_on_the_chosen_folder_or_says_why_not(tmp_path):
    from labharness.web.app import data_folder_for
    from labharness.web.settings import Settings
    settings = Settings(tmp_path / "settings.json")
    assert data_folder_for(settings, tmp_path) == (tmp_path / "lab", None)
    settings.set_data_dir(str(tmp_path / "chosen"))
    assert data_folder_for(settings, tmp_path) == (tmp_path / "chosen", None)
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a folder", encoding="utf-8")
    settings.set_data_dir(str(blocked))
    folder, problem = data_folder_for(Settings(tmp_path / "settings.json"), tmp_path)
    assert folder == tmp_path / "lab" and "can't be written" in problem
    settings.update({"data_dir": str(tmp_path / "sneaky")})                        # only through the runtime
    assert settings.get("data_dir") == str(blocked)
