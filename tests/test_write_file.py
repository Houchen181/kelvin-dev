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


def chat_day(tmp_path, provider, folder, answers=(), library_here=False):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover(answers), tmp_path / "data")
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
