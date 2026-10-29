"""Kelvin saves files in a chat's folder (issue #1: it wrote a helper script but couldn't save it): write_file saves at
once, only inside the folder the person chose, never a file Windows or Python would run by itself; a file it replaces
is kept first; and code an instrument's maker's library loads asks the person, since it would run unchecked."""
import asyncio
import json

import pytest

from labharness import folderfiles
from labharness.folderfiles import FolderError
from labharness.library import code_folders

TEXT = "import nidaqmx\n\n\nclass KelvinCounter:\n    pass\n"


def test_a_file_is_saved_in_the_folder_and_one_it_replaces_is_kept(tmp_path):
    folder = tmp_path / "M2 scanning"
    folder.mkdir()
    saved, kept = folderfiles.save(folder, "helpers/kelvin_counter.py", TEXT)
    assert saved == (folder / "helpers" / "kelvin_counter.py").resolve() and kept is None
    assert saved.read_bytes() == TEXT.encode("utf-8")                       # as written: LF, UTF-8
    assert folderfiles.save(folder, "helpers/kelvin_counter.py", TEXT) == (saved, None)   # already so: nothing kept
    again, kept = folderfiles.save(folder, "helpers/kelvin_counter.py", TEXT + "# v2\n")
    assert again == saved and kept.parent == saved.parent / ".kelvin-old" and kept.read_text() == TEXT
    assert kept.name.startswith("kelvin_counter.") and kept.suffix == ".py"
    assert not list(saved.parent.glob(".*.kelvin-new"))                     # no half-written file left


@pytest.mark.parametrize("path", ["../outside.txt", "run.bat", "go.ps1", "setup.cmd", "shortcut.lnk", "Sitecustomize.py",
                                  "extra.pth", ".kelvin-old/kelvin_counter.py", ""])
def test_what_it_never_saves(tmp_path, path):
    folder = tmp_path / "chat"
    folder.mkdir()
    with pytest.raises(FolderError):
        folderfiles.save(folder, path, "x")
    with pytest.raises(FolderError, match="outside this chat's folder .*: Kelvin saves only"):
        folderfiles.save(folder, tmp_path / "elsewhere.txt", "x")
    with pytest.raises(FolderError, match="is a folder"):
        folderfiles.save(folder, ".", "x")


def test_a_file_too_long_is_not_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(folderfiles, "MAX_SAVED_CHARS", 10)
    with pytest.raises(FolderError, match="too long"):
        folderfiles.save(tmp_path, "big.csv", "x" * 11)


def test_the_folders_instruments_load_code_from(tmp_path):
    found = code_folders({"counter": "lib://Dev1?env=nidaq&path=D:/Python Scripts/M2",
                          "cryo": "lib://COM4?python=C:/Python38-32/python.exe",
                          "smu": "GPIB0::24::INSTR"}, tmp_path)
    assert [(n, p.as_posix()) for n, p in found] == [
        ("counter", "D:/Python Scripts/M2"), ("counter", (tmp_path / "env" / "nidaq").as_posix()),
        ("cryo", "C:/Python38-32")]


# --- in a chat ------------------------------------------------------------------------------------------------------

pytest.importorskip("mcp")
from labharness import build_virtual_lab  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.gate import ScriptedApprover  # noqa: E402
from labharness.web.agent import ChatAgent  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from test_web import Scripted  # noqa: E402


def save_turns(path="kelvin_counter.py", content=TEXT):
    return [[{"type": "tool_call", "id": "w1", "name": "write_file", "input": {"path": path, "content": content}}],
            [{"type": "text", "text": "Saved it."}]]


def chat_day(tmp_path, provider, folder, answers=(), library_here=False, home=None):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover(answers), tmp_path / "data")
    rt.home = home   # Kelvin's own folder, as the app sets it
    if library_here:   # the counter's maker's code is loaded from this chat's folder
        rt.lab.config.instruments["smu"].address = "lib://Dev1/ctr0?env=nidaq&path=%s" % folder
    store = ChatStore(tmp_path / "chats")
    agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
    chat = store.create("scripted:m1")
    if folder:
        chat["folder"] = str(folder)
        store.save(chat)
    shown = []

    async def emit(e):
        if e.get("type") == "approval":
            shown.append(e)

    async def day():
        await agent.start()
        try:
            agent.claim(chat["id"], "scripted:m1")
            await agent.run_turn(chat["id"], "save the counter helper in my folder", "scripted:m1", provider, emit)
        finally:
            await agent.close()
    asyncio.run(day())
    result = next(r for m in store.load(chat["id"])["messages"] if m["role"] == "tool" for r in m["results"]
                  if r["name"] == "write_file")
    record = tmp_path / "data" / "events.jsonl"
    events = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()] if record.exists() else []
    return rt, result, shown, events


def test_kelvin_saves_in_the_chats_folder_without_asking(tmp_path):
    folder = tmp_path / "M2 scanning"
    folder.mkdir()
    provider = Scripted(save_turns())
    rt, result, shown, events = chat_day(tmp_path, provider, folder)          # no answers: any question would fail
    assert "write_file" in provider.tool_names[0] and "write_file saves a text file there" in provider.systems[0]
    assert not result["is_error"] and result["content"].startswith("saved %s" % (folder / "kelvin_counter.py"))
    assert (folder / "kelvin_counter.py").read_text() == TEXT and not shown and rt.approver.requests == []
    [saved] = [e for e in events if e["type"] == "file_saved"]
    assert saved["path"] == str((folder / "kelvin_counter.py").resolve()) and saved["by"] == "agent"


def test_a_chat_without_a_folder_saves_nothing(tmp_path):
    provider = Scripted(save_turns())
    rt, result, shown, events = chat_day(tmp_path, provider, None)
    assert "write_file" not in provider.tool_names[0]
    assert result["is_error"] and "no folder of its own" in result["content"]


def test_code_an_instruments_library_loads_is_shown_whole_in_the_chat(tmp_path):
    folder = tmp_path / "M2 scanning"
    folder.mkdir()
    rt, result, shown, events = chat_day(tmp_path, Scripted(save_turns()), folder, library_here=True)
    assert not result["is_error"] and (folder / "kelvin_counter.py").read_text() == TEXT
    assert rt.approver.requests == []                                         # saving drives nothing: Kelvin's own yes
    [card] = shown
    assert card["auto"] and card["title"] == "Save code that smu loads?" and card["items"][-1] == "The file:\n" + TEXT
    assert "runs on the instrument when it connects" in " ".join(card["items"])
    [asked] = [e for e in events if e["type"] == "ask"]
    assert asked["answer"] == "auto-approved" and asked["note"].startswith("drives nothing: saving a file")
    assert [e["loaded_by"] for e in events if e["type"] == "file_saved"] == [["smu"]]

    rt, result, shown, events = chat_day(tmp_path / "notes", Scripted(save_turns("scan-notes.md", "gate 100 ms\n")),
                                         folder, library_here=True)            # not code: nothing to show either
    assert not result["is_error"] and not shown and (folder / "scan-notes.md").is_file()


def test_after_reading_a_file_a_person_answers_for_code_an_instrument_loads(tmp_path):
    folder = tmp_path / "M2 scanning"
    folder.mkdir()
    (folder / "readme.txt").write_text("Counter on ctr0.\n")
    turns = [[{"type": "tool_call", "id": "r1", "name": "read_file", "input": {"path": "readme.txt"}}]] + save_turns()
    rt, result, shown, events = chat_day(tmp_path, Scripted(turns), folder, answers=[False], library_here=True)
    [asked] = rt.approver.requests                                            # the file could be steering the model
    assert "Not approved by Kelvin itself: this chat has read text from outside the lab" in str(asked)
    assert asked.items[2] == "The file:\n" + TEXT
    assert result["is_error"] and result["content"].startswith("declined: nothing was saved")
    assert not (folder / "kelvin_counter.py").exists()
    assert [e["answer"] for e in events if e["type"] == "ask"] == ["declined"]


# --- Kelvin's own folders (field tests: a chat folder at the Kelvin home let write_file replace lab.yaml) ------------

def kelvin_home(tmp_path):
    """A person's folder with Kelvin's own folder in it, as on a lab computer: C:/Users/me and C:/Users/me/Kelvin."""
    me = tmp_path / "me"
    home = me / "Kelvin"
    for f, text in (("lab.yaml", "lab: mine\n"), ("settings.json", "{}"), ("accounts.json", "[]"),
                    ("cards/acme.yaml", "id: acme\n"), ("users/bob/chats/c1.json", '{"title": "Bob chat"}')):
        (home / f).parent.mkdir(parents=True, exist_ok=True)
        (home / f).write_text(text, encoding="utf-8")
    return me, home


@pytest.mark.parametrize("path", ["Kelvin/lab.yaml", "Kelvin/settings.json", "Kelvin/accounts.json",
                                  "Kelvin/cards/acme.yaml", "Kelvin/users/bob/chats/c1.json", "Kelvin/new.txt"])
def test_kelvin_never_saves_or_reads_in_its_own_folder_through_a_chats_folder(tmp_path, path):
    me, home = kelvin_home(tmp_path)
    before = (me / path).read_bytes() if (me / path).exists() else None
    with pytest.raises(FolderError, match="Kelvin's own folder"):
        folderfiles.save(me, path, "lab: rewritten\n", own=[home])
    assert ((me / path).read_bytes() if (me / path).exists() else None) == before
    with pytest.raises(FolderError, match="Kelvin's own folder"):
        folderfiles.read(me, path, own=[home])
    with pytest.raises(FolderError, match="Kelvin's own folder"):
        folderfiles.listing(me, "Kelvin/users", own=[home])
    assert folderfiles.save(me, "notes.md", "fine\n", own=[home])[0] == (me / "notes.md").resolve()   # the rest works
    assert "Kelvin/" in folderfiles.listing(me, "", own=[home])


@pytest.mark.parametrize("path", ["events.jsonl", "mounted.json", "queue.json", "catalog.sqlite",
                                  "runs/20261028-101010-sweep/run.h5", "elsewhere/run.h5"])
def test_kelvin_never_rewrites_the_labs_records(tmp_path, path):
    data = tmp_path / "Data"   # a chat's folder may be the data folder itself
    with pytest.raises(FolderError, match="only Kelvin itself writes"):
        folderfiles.save(data, path, "{}", data=data)
    assert folderfiles.save(data, "analysis/fit.py", "x = 1\n", data=data)[1] is None


def test_in_a_chat_write_file_refuses_the_lab_config_and_read_file_another_persons_chats(tmp_path):
    me, home = kelvin_home(tmp_path)
    rt, result, shown, events = chat_day(tmp_path, Scripted(save_turns("Kelvin/lab.yaml", "lab: wide open\n")), me,
                                         home=home)
    assert result["is_error"] and "Kelvin's own folder" in result["content"]
    assert (home / "lab.yaml").read_text(encoding="utf-8") == "lab: mine\n"
    assert not [e for e in events if e["type"] == "file_saved"]
    turns = [[{"type": "tool_call", "id": "r1", "name": "read_file", "input": {"path": "Kelvin/users/bob/chats/c1.json"}}],
             [{"type": "text", "text": "Read it."}]]
    provider = Scripted(turns)
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data2")
    rt.home = home
    store = ChatStore(tmp_path / "chats2")
    agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
    chat = store.create("scripted:m1")
    chat["folder"] = str(me)
    store.save(chat)

    async def day():
        await agent.start()
        try:
            agent.claim(chat["id"], "scripted:m1")

            async def emit(e):
                pass
            await agent.run_turn(chat["id"], "read bob's chat", "scripted:m1", provider, emit)
        finally:
            await agent.close()
    asyncio.run(day())
    [read] = [r for m in store.load(chat["id"])["messages"] if m["role"] == "tool" for r in m["results"]]
    assert read["is_error"] and "Kelvin's own folder" in read["content"] and "Bob chat" not in read["content"]


def test_a_chat_folder_in_kelvins_own_folder_is_refused_when_chosen(tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    home = tmp_path / "Kelvin"   # the app's folder: next to its settings
    settings = Settings(home / "settings.json")
    store = ChatStore(home / "users" / "me" / "chats")
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    client = TestClient(create_app(rt, settings, store, hosts=["testserver"], providers={"scripted": Scripted([])}))
    h = {"X-Kelvin-Token": settings.access_key}
    with client:
        for folder in (home, home / "users" / "bob" / "chats", home / "cards"):
            r = client.post("/api/chats", json={"model": "scripted:m1", "folder": str(folder)}, headers=h)
            assert r.status_code == 400 and "Kelvin's own folder" in r.json()["error"]
            assert client.post("/api/folder", json={"path": str(folder)}, headers=h).status_code == 400
        chat = client.post("/api/chats", json={"model": "scripted:m1", "folder": str(tmp_path / "work")}, headers=h)
        assert chat.status_code == 200 and chat.json()["own_folder"]
        r = client.patch("/api/chats/%s" % chat.json()["id"], json={"folder": str(home / "users" / "bob")}, headers=h)
        assert r.status_code == 400 and "Kelvin's own folder" in r.json()["error"]
