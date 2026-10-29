"""Each chat has a folder the person chose, where its runs are saved; Kelvin keeps finding every run, wherever it is."""
import json

import pytest

from labharness import ScriptedApprover, rundata
from simlab import build_virtual_lab

LOOK = """plan: look
repeat: 3
measure: [lockin.X]"""


def runtime(tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    return LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data", None)


def test_a_chats_runs_go_to_its_folder_and_kelvin_still_finds_them(tmp_path):
    rt = runtime(tmp_path)
    mine = tmp_path / "Sample 7" / "cooldown 3"                        # made when its first run starts
    rt.attribution = {"folder": str(mine)}
    out = rt.run(LOOK)
    rt.attribution = {}
    run_id = rt.runs_index(1)[0]["run_id"]
    assert rundata.is_run(mine / run_id) and not (tmp_path / "data" / "runs" / run_id).exists()
    assert str(mine / run_id) in out                                   # the answer says where, in full
    assert len(rundata.load(rt._run_dir(run_id)).rows) == 3
    assert run_id in rt.find_runs()                                    # the catalog has it
    assert rt.pick_runs(last=1) == [run_id]                            # and reports
    assert json.loads((tmp_path / "data" / "folders.json").read_text(encoding="utf-8")) == [str(mine)]
    rt.run(LOOK)                                                       # a chat without a folder: the data folder
    other = rt.runs_index(1)[0]["run_id"]
    assert (tmp_path / "data" / "runs" / other).is_dir()
    (mine / "notes").mkdir()                                           # the person's own files there are theirs
    (mine / "notes" / "meta.json").write_text("{}", encoding="utf-8")
    assert sorted(d.name for d in rt.runs.iterdir()) == sorted([run_id, other])
    with pytest.raises(KeyError):
        rt._run_dir("../notes")


def test_a_queued_run_keeps_its_chats_folder_and_a_missing_drive_keeps_the_data(tmp_path):
    rt = runtime(tmp_path)
    mine = tmp_path / "queued"
    rt.attribution = {"folder": str(mine)}
    rt.queue_add(LOOK)
    rt.attribution = {}
    rt.queue.state = "running"
    rt._queue_loop({}, "test")                                         # the background loop, run here, later
    run_id = rt.runs_index(1)[0]["run_id"]
    assert (mine / run_id).is_dir()
    blocked = tmp_path / "a file"                                      # a folder Kelvin can't make: a drive away
    blocked.write_text("x", encoding="utf-8")
    rt.attribution = {"folder": str(blocked / "runs")}
    out = rt.run(LOOK)
    rt.attribution = {}
    newest = rt.runs_index(1)[0]["run_id"]
    assert (tmp_path / "data" / "runs" / newest).is_dir()              # measured all the same, in the data folder
    assert "could not be written" in out and str(tmp_path / "data" / "runs" / newest) in out


def test_runs_in_chat_folders_are_shared_like_the_others(tmp_path):
    pytest.importorskip("starlette")
    from labharness.eventlog import RunFolders
    from labharness.web.hubsync import run_items
    rt = runtime(tmp_path)
    rt.user = None
    rt.attribution = {"folder": str(tmp_path / "elsewhere")}
    rt.run(LOOK)
    rt.attribution = {}
    rt.run(LOOK)
    items, _ = run_items(RunFolders(tmp_path / "data"), set(), None)
    assert len(items) == 2


# ---------------------------------------------------------------- the portal: picking, showing, opening

pytest.importorskip("starlette")
from test_web import Scripted, client_for  # noqa: E402,F401


@pytest.fixture
def parts(tmp_path):
    """As on a lab computer: Kelvin's own folder (settings, chats) apart from the folders people choose for chats,
    which may never be in it (folderfiles.off_limits)."""
    from simlab import demo_envelope
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    lab = build_virtual_lab(seed=1)
    home = tmp_path / "Kelvin"
    return (LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab)),
            Settings(home / "settings.json"), ChatStore(home / "chats"))


def test_the_portal_sets_shows_and_remembers_a_chats_folder(parts, tmp_path):
    runtime, settings, store = parts
    client, headers = client_for(parts, {"scripted": Scripted([])})
    data_runs = str(runtime.data_dir / "runs")
    with client:
        assert client.get("/api/folder", headers=headers).json() == {"folder": data_runs, "data": data_runs,
                                                                     "recent": []}
        made = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        assert made["folder"] == data_runs and not made["own_folder"]   # nothing chosen yet: the data folder
        bad = client.patch("/api/chats/%s" % made["id"], json={"folder": "relative/path"}, headers=headers)
        assert bad.status_code == 400 and "full path" in bad.json()["error"]
        mine = tmp_path / "Data" / "Sample A"
        shown = client.patch("/api/chats/%s" % made["id"], json={"folder": str(mine)}, headers=headers).json()
        assert shown["folder"] == str(mine) and shown["own_folder"] and mine.is_dir()
        assert client.get("/api/folder", headers=headers).json()["folder"] == str(mine)   # the next chat starts here
        nxt = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        assert nxt["folder"] == str(mine)
        project = client.post("/api/projects", json={"name": "Graphene"}, headers=headers).json()["id"]
        other = tmp_path / "Data" / "Graphene"
        picked = client.post("/api/folder", json={"path": str(other), "project": project}, headers=headers).json()
        assert picked == {"folder": str(other)}                         # picked for a new chat in the project
        inside = client.post("/api/chats", json={"model": "scripted:m1", "project": project}, headers=headers).json()
        assert inside["folder"] == str(other)
        assert client.get("/api/folder?project=%s" % project, headers=headers).json()["folder"] == str(other)
        back = client.patch("/api/chats/%s" % made["id"], json={"folder": None}, headers=headers).json()
        assert back["folder"] == data_runs and not back["own_folder"]   # back to the data folder
        runtime.operating = "a task"
        locked = client.patch("/api/chats/%s" % made["id"], json={"folder": str(mine)}, headers=headers)
        assert locked.status_code == 423                                # only a person picks it
        runtime.operating = None


def test_kelvin_knows_the_chats_folder_and_saves_its_runs_there(parts, tmp_path):
    runtime, settings, store = parts
    mine = tmp_path / "measurements"
    bot = Scripted([[{"type": "tool_call", "id": "c1", "name": "run_plan", "input": {"plan": LOOK}}],
                    [{"type": "text", "text": "Done."}]])
    client, headers = client_for(parts, {"scripted": bot})
    with client:
        chat = client.post("/api/chats", json={"model": "scripted:m1", "folder": str(mine)}, headers=headers).json()
        with client.stream("POST", "/api/chats/%s/turn" % chat["id"], json={"text": "measure", "model": "scripted:m1"},
                           headers=headers) as r:
            events = [json.loads(line) for line in r.iter_lines() if line.strip()]
    assert any(e.get("type") == "turn_end" for e in events)
    assert "saved in %s, the folder the person chose" % mine in bot.systems[0]
    runs = [d for d in mine.iterdir() if d.is_dir()]
    assert len(runs) == 1 and rundata.is_run(runs[0])


def test_new_chats_can_skip_the_picker_and_the_data_folder_counts_runs_elsewhere(parts, tmp_path):
    runtime, settings, store = parts
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        assert client.get("/api/settings", headers=headers).json()["settings"]["ask_folder"] is True
        client.post("/api/settings", json={"ask_folder": "off"}, headers=headers)
        assert client.get("/api/settings", headers=headers).json()["settings"]["ask_folder"] is False
    runtime.attribution = {"folder": str(tmp_path / "elsewhere")}
    runtime.run(LOOK)
    runtime.attribution = {}
    assert runtime.data_summary()["runs"] == 0 and runtime.data_summary()["elsewhere"] == 1


def test_kelvin_help_says_where_a_chats_runs_go():
    from labharness import guide
    found = guide.search("where are this chat's runs saved, how do I change the folder")
    assert "**Choose folder…**" in found and "under the message box" in found


def test_the_folder_menu_offers_recent_folders_and_the_data_folder(parts, tmp_path):
    runtime, settings, store = parts
    client, headers = client_for(parts, {"scripted": Scripted([])})
    data_runs = str(runtime.data_dir / "runs")
    first, second = tmp_path / "Sample A", tmp_path / "Sample B"
    with client:
        for folder in (first, second, first):
            client.post("/api/folder", json={"path": str(folder)}, headers=headers)
        seen = client.get("/api/folder", headers=headers).json()
        assert seen["folder"] == str(first) and seen["recent"] == [str(first), str(second)]   # newest first, once
        back = client.post("/api/folder", json={"path": None}, headers=headers).json()
        assert back == {"folder": data_runs}                                # new chats start in the data folder
        seen = client.get("/api/folder", headers=headers).json()
        assert seen["folder"] == data_runs and seen["recent"] == [str(first), str(second)]
        made = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        assert made["folder"] == data_runs and not made["own_folder"]
