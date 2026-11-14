"""The records findings of the 0.3.52 audit (KELVIN-0.3.52-AUDIT.md: "Records and data integrity", D1-D12, and the
runtime's R11-R15), each kept from coming back by the repro that showed it (audit-repros/records, runtime-a)."""
import errno
import json
import os
import shutil
import sqlite3
import stat
import threading
from pathlib import Path

import pytest

from labharness import ScriptedApprover, integrity, notebook, numbercheck, rundata
from labharness.eventlog import EventLog, RunFolders, RunStore, verify_chain

pytest.importorskip("mcp")
h5py = pytest.importorskip("h5py")

from labharness.agent.runtime import LabRuntime, Viewer  # noqa: E402
from simlab import build_virtual_lab  # noqa: E402

PLAN = "plan: iv\nsweep: {smu.V: {from: 0, to: 0.2, step: 0.1}}\nmeasure: [smu.I]\n"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8
CONFIDENTIAL = {"project": "P1", "chat": "chat-A", "confidential": True}


def runtime(folder):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 50), folder)
    rt.auto_report = "off"   # a run's PDF and slides are not what these look at
    return rt


def measured(rt, name="iv"):
    said = rt.run(PLAN.replace("plan: iv", "plan: " + name))
    run = rt.pick_runs(last=1)[-1]
    assert run.split("-", 2)[2].startswith(name.replace(" ", "_")), said
    return run, rt._run_dir(run)


def writable(path):
    os.chmod(str(path), stat.S_IREAD | stat.S_IWRITE)


def secret_project(pid):
    secret = pid == "P1"
    return {"id": pid, "name": "Secret flake" if secret else (pid or "General"), "devices": [], "notes": [],
            "references": [], "summary": "", "private": secret, "unshared": secret, "confidential": secret}


def inside():   # the confidential project's own chat
    return Viewer(chat="chat-A", project="P1", allows=lambda marks: True, whose=dict(CONFIDENTIAL),
                  model="anthropic:claude-x")


def outsider():   # another project's chat, with a model the confidential project never allows
    return Viewer(chat="chat-B", project=None, allows=lambda marks: False, whose={"chat": "chat-B"},
                  model="kelvin-hub:other-model")


def backup_root(nas):
    return nas / "Kelvin backup" / "Virtual Lab"


# ---------------------------------------------------------------- D1, D7, R12: the backup


def test_d1_each_persons_record_is_filed_under_their_name(tmp_path):
    nas = tmp_path / "nas"
    nas.mkdir()
    ann = runtime(tmp_path / "users" / "ann" / "data")
    ann.user = "ann"
    ann.record_step("ann loaded chip A1", sample="A1")
    ann.update_sample("A1", kind="chip")
    bob = runtime(tmp_path / "users" / "bob" / "data")
    bob.user = "bob"
    bob.record_step("bob loaded chip B7", sample="B7")
    bob.update_sample("B7", kind="chip")
    people = backup_root(nas) / "people"

    def holds(who, text, name="events.jsonl"):
        return text in (people / who / name).read_text(encoding="utf-8")
    for _ in range(2):   # each backs up twice, in turn: neither writes over the other
        assert ann.backup_records(str(nas), by="ann (portal)").startswith("backed up into")
        assert bob.backup_records(str(nas), by="bob (portal)").startswith("backed up into")
        bob.record_step("bob cooled B7 down", sample="B7")
    assert holds("ann", "ann loaded chip A1") and not holds("ann", "bob loaded")
    assert holds("bob", "bob loaded chip B7") and holds("bob", "bob cooled B7 down") and not holds("bob", "ann ")
    assert holds("ann", '"A1"', "samples.json") and holds("bob", '"B7"', "samples.json")
    assert not holds("ann", '"B7"', "samples.json")


def test_d1_a_record_that_lost_lines_never_replaces_its_full_copy(tmp_path):
    rt = runtime(tmp_path / "data")
    rt.user = "ann"
    for _ in range(8):
        rt.record_step("checked the wiring")
    nas = tmp_path / "nas"
    nas.mkdir()
    rt.backup_records(str(nas), by="ann (portal)")
    copy = backup_root(nas) / "people" / "ann" / "events.jsonl"
    full = copy.read_bytes()
    rt.gate.log.settle()
    lines = rt.events_path.read_bytes().splitlines(keepends=True)
    rt.events_path.write_bytes(b"".join(lines[:5]))   # the record here lost its end
    said = rt.backup_records(str(nas), by="ann (portal)")
    assert "events.jsonl here holds less than its copy in the backup" in said, said
    assert copy.read_bytes() == full   # 68 lines once became 5 there
    rt.gate.log.settle()
    other = EventLog(tmp_path / "elsewhere" / "events.jsonl")   # another data folder's record, under her name
    for _ in range(12):
        other.write("step", text="somewhere else")
    other.settle()
    shutil.copyfile(str(other.path), str(rt.events_path))
    said = rt.backup_records(str(nas), by="ann (portal)")
    assert "kept beside it as events.2.jsonl" in said, said
    assert copy.read_bytes() == full and (copy.parent / "events.2.jsonl").read_bytes().startswith(
        other.path.read_bytes())


def test_d7_a_backup_holds_the_samples_pictures(tmp_path):
    from labharness.samples import SampleBook
    rt = runtime(tmp_path / "data")
    rt.user = "ann"
    pic = SampleBook(rt.data_dir).add_picture(None, "D3", PNG, "png", "the chip after bonding", "chat", "human",
                                              account="ann")
    nas = tmp_path / "nas"
    nas.mkdir()
    rt.backup_records(str(nas), by="ann (portal)")
    copies = list((nas / "Kelvin backup").rglob(pic["file"]))
    assert len(copies) == 1 and copies[0].read_bytes() == PNG
    assert pic["file"] in (copies[0].parent.parent.parent / "samples.json").read_text(encoding="utf-8")


def test_r12_no_chat_starts_a_backup_or_names_its_folder(tmp_path):
    import anyio
    from mcp import Client
    from labharness.agent.server import build_server
    rt = runtime(tmp_path / "data")
    rt.project_info = secret_project
    rt.home = tmp_path / "home"
    rt.user = "ada"
    rt.attribution = dict(CONFIDENTIAL)
    with rt.viewing(inside()):
        measured(rt, "secret_iv")
        rt.write_entry(title="Tc of the secret flake", body="Recipe: anneal at 300 C for 2 h.")
    folder = tmp_path / "chatB"   # another chat's own folder, which its file tools read
    folder.mkdir()
    rt.attribution = {"chat": "chat-B", "folder": str(folder)}
    for viewer in (outsider(), Viewer()):   # another project's chat; an agent over MCP
        with rt.viewing(viewer):
            said = rt.backup_records(str(folder))
        assert said == ("not backed up: only a person backs up the records and chooses where they go. Ask them to "
                        "do it in Kelvin's portal: Settings → Data & reports → Back up."), said

    async def by_the_tool():
        async with Client(build_server(rt)) as client:
            r = await client.call_tool("backup_records", {"folder": str(folder)})
            return "\n".join(c.text for c in r.content if getattr(c, "type", "") == "text")
    assert "only a person backs up the records" in anyio.run(by_the_tool)
    assert list(folder.rglob("*")) == []   # nothing of anyone's was copied there
    nas = tmp_path / "nas"
    nas.mkdir()
    assert rt.backup_records(str(nas), by="ada (portal)").startswith("backed up into")   # the person, from Settings
    assert [e["by"] for e in rt.gate.log.events() if e["type"] == "backup"] == ["ada (portal)"]


def test_r12_the_person_backs_up_from_the_portal(tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from labharness.web.app import STATIC, create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    rt = runtime(tmp_path / "data")
    rt.record_step("loaded chip 2", sample="D3")
    settings = Settings(tmp_path / "settings.json")
    client = TestClient(create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"]))
    h = {"X-Kelvin-Token": settings.access_key}
    nas = tmp_path.parent / (tmp_path.name + "-nas")   # the app's own folder is tmp_path: never backed up into
    nas.mkdir()
    r = client.post("/api/backup", json={"folder": str(nas)}, headers=h)
    assert r.status_code == 200 and r.json()["said"].startswith("backed up into"), r.text
    assert (backup_root(nas) / "people" / "no account" / "events.jsonl").exists()
    assert [e["by"] for e in rt.gate.log.events() if e["type"] == "backup"] == ["human (portal)"]
    r = client.post("/api/backup", json={"folder": "relative/path"}, headers=h)
    assert r.status_code == 400 and "full path" in r.json()["error"]
    page, js = (STATIC / "index.html").read_text(encoding="utf-8"), (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'id="backup-run"' in page and '"/api/backup"' in js


# ---------------------------------------------------------------- R11, R13: unshared work stays apart


def test_r11_another_chat_cannot_read_a_confidential_entry_by_amending_it(tmp_path):
    rt = runtime(tmp_path / "data")
    rt.project_info = secret_project
    rt.home = tmp_path / "home"
    rt.user = "ada"
    with rt.viewing(inside()):
        rt.write_entry(title="Tc of the secret flake", body="The transition is at 4.2 K. Recipe: anneal at 300 C "
                       "for 2 h.", day="2026-11-13")
    eid = next(iter(notebook.read(rt.data_dir)))
    rt.sign_entry(eid)   # the person, in the Analysis pane's Notebook
    for viewer in (Viewer(), outsider()):
        with rt.viewing(viewer):
            assert [e["type"] for e in rt.seen_events(rt.gate.log.events()) if e.get("entry") == eid] == []
            assert rt.write_entry(title="note", entry=eid) == "not kept: no entry %s (notebook lists them)" % eid
            assert rt.notebook(entry=eid) == "no entry %s" % eid
            assert rt.search_records("anneal").startswith("nothing found") and "Recipe" not in rt.notebook()
    assert len(notebook.read(rt.data_dir)[eid]["versions"]) == 1   # nothing was kept
    with rt.viewing(inside()):   # even its own project's chat: an AI never amends a person's signed entry
        said = rt.write_entry(title="note", entry=eid)
    assert said.startswith("not kept: %s is signed, and only a person amends a signed entry" % eid), said
    assert rt.write_entry(body="The transition is at 4.2 K.", entry=eid).startswith("kept an amendment of")
    e = notebook.read(rt.data_dir)[eid]
    assert notebook.latest(e)["whose"] == CONFIDENTIAL and notebook.marks(e) == CONFIDENTIAL   # its marks kept
    with rt.viewing(outsider()):
        assert rt.notebook(entry=eid) == "no entry %s" % eid
    with rt.viewing(inside()):
        assert "The transition is at 4.2 K." in rt.notebook(entry=eid)


def test_r13_search_records_shows_no_figure_of_unshared_work(tmp_path):
    rt = runtime(tmp_path / "data")
    rt.project_info = secret_project
    rt.home = tmp_path / "home"
    rt.attribution = dict(CONFIDENTIAL)
    with rt.viewing(inside()):
        run, _ = measured(rt, "secret_iv")
    rt.attribution = {}
    figures = rt.data_dir / "figures"
    figures.mkdir()
    (figures / "20261114-120151-secret_flake_iv.json").write_text(json.dumps({
        "id": "f1", "figure": "20261114-120151-secret_flake_iv", "made": "2026-11-14T12:01:51",
        "runs": [{"run": run}], "spec": {"title": "Secret flake IV at 4 K", "x": "smu.V", "y": "smu.I"}}),
        encoding="utf-8")
    with rt.viewing(outsider()):
        assert run in rt._hidden_runs()
        for words in ("secret", "smu", "flake"):
            said = rt.search_records(words)
            assert "[figures]" not in said and run not in said, said
    with rt.viewing(inside()):
        assert "[figures] 20261114-120151-secret_flake_iv (from %s)" % run in rt.search_records("flake")


# ---------------------------------------------------------------- R14, R15: moving the data folder


def other_drive(monkeypatch, old):
    """os.rename as Windows answers a rename from the old folder onto another drive (EXDEV); the rest as it is."""
    real = os.rename
    here = str(Path(old).resolve())

    def rename(src, dst, *args, **kwargs):
        if str(Path(src).resolve()).startswith(here):
            raise OSError(errno.EXDEV, "The system cannot move the file to a different disk drive", str(src))
        return real(src, dst, *args, **kwargs)
    monkeypatch.setattr(os, "rename", rename)


def test_r14_a_move_to_another_drive_copies_checks_and_deletes(tmp_path, monkeypatch):
    rt = runtime(tmp_path / "data")
    runs = [measured(rt)[1] for _ in range(2)]
    seals = {d.name: rundata.sealed(d) for d in runs}
    new = tmp_path / "other-drive"
    other_drive(monkeypatch, tmp_path / "data")
    out = rt.set_data_dir(new, move=True)
    monkeypatch.undo()
    assert out["failed"] == [] and out["kept"] == [], out
    for d in runs:
        assert not d.exists()   # no stump of it left behind
        there = new / "runs" / d.name
        assert rundata.check_seal(there) == ("intact", seals[d.name])
        assert not os.stat(str(there / "run.h5")).st_mode & stat.S_IWRITE   # still marked read-only
    assert not (new / ".kelvin-moving").exists() and rt.events_path == new / "events.jsonl"
    assert rt.events_path.exists() and not (tmp_path / "data" / "events.jsonl").exists()


def test_r14_a_run_that_cant_be_deleted_stays_whole_and_is_said(tmp_path, monkeypatch):
    rt = runtime(tmp_path / "data")
    run, d = measured(rt)
    files = sorted(p.name for p in d.iterdir())
    new = tmp_path / "other-drive"
    real = shutil.rmtree

    def rmtree(path, *args, **kwargs):   # one file goes, then one another program has open stops it
        if Path(path).resolve() == d.resolve():
            os.unlink(str(Path(path) / rundata.SEAL))
            raise PermissionError(errno.EACCES, "The process cannot access the file because it is being used by "
                                                "another process", str(Path(path) / rundata.RUN_FILE))
        return real(path, *args, **kwargs)
    other_drive(monkeypatch, tmp_path / "data")
    monkeypatch.setattr(shutil, "rmtree", rmtree)
    out = rt.set_data_dir(new, move=True)
    monkeypatch.undo()
    said = [f for f in out["failed"] if f.startswith("runs/%s: " % run)]
    assert said and "so it stays here whole" in said[0], out
    assert sorted(p.name for p in d.iterdir()) == files and rundata.check_seal(d)[0] == "intact"   # whole again
    assert not (new / "runs" / run).exists() and not (new / ".kelvin-moving").exists()
    assert rt.runs.find(run) == d   # still found where it stayed


def test_r15_the_move_carries_what_kelvin_reads_in_the_data_folder(tmp_path):
    from labharness import protection
    from labharness.samples import SampleBook
    rt = runtime(tmp_path / "data")
    rt.user = "ada"
    rt.mount(None, {"id": "d4", "name": "D4", "protection": protection.normalize(
        {"gates": [{"drive": "smu.V", "dielectric": "hBN", "thickness_nm": 20, "leak": "smu.I"}]}, rt.lab)})
    assert rt.lab.instruments["smu"].limits("V") == (-6.0, 6.0)
    rt.write_entry(title="Day 1", body="Mounted D4 and bonded it.")
    rt.update_sample("D4", kind="device", location="fridge 2")
    pic = SampleBook(rt.data_dir).add_picture("d4", "D4", PNG, "png", "bonded", "chat", "human")
    rt.queue_add("step: wait a bit\nwait: 5")
    rt.queue_save("night")
    new = tmp_path / "new"
    out = rt.set_data_dir(new, move=True)
    assert out["kept"] == [] and out["failed"] == [], out
    assert "Day 1" in rt.notebook() and "fridge 2" in rt.sample_history("D4")
    assert SampleBook(new).picture("d4", "D4", pic["file"])[0].read_bytes() == PNG
    assert "no saved queue" not in rt.queue_load("night")
    left = {p.name for p in (tmp_path / "data").iterdir()}
    assert not left & {"notebook", "samples.json", "samples", "queues", "mounted.json"}, left
    again = runtime(new)   # Kelvin's next start, on the folder the person chose: the device is still protected
    assert again.mounted.get("device_name") == "D4" and again.lab.instruments["smu"].limits("V") == (-6.0, 6.0)


def test_r15_what_the_new_folder_has_stays_and_the_answer_says_what_stayed_behind(tmp_path):
    rt = runtime(tmp_path / "data")
    rt.write_entry(title="Mine", body="Here.")
    rt.update_sample("A1", kind="chip")
    new = tmp_path / "new"
    (new / "notebook").mkdir(parents=True)
    theirs = '{"entry": "e00000000", "version": 1, "title": "Theirs", "body": "There."}\n'
    (new / "notebook" / "entries.jsonl").write_text(theirs, encoding="utf-8")
    (new / "samples.json").write_text('{"samples": {}}', encoding="utf-8")
    out = rt.set_data_dir(new, move=True)
    assert "notebook/entries.jsonl" in out["kept"] and "samples.json" in out["kept"], out
    assert (new / "notebook" / "entries.jsonl").read_text(encoding="utf-8") == theirs   # never written over
    assert [notebook.latest(e)["title"] for e in notebook.read(tmp_path / "data").values()] == ["Mine"]   # stayed


# ---------------------------------------------------------------- D2, D4, D6: what Verify sees


def test_d2_a_run_whose_seal_file_is_gone_is_never_sealed_again(tmp_path):
    rt = runtime(tmp_path / "data")
    run, d = measured(rt)
    kept = rundata.sealed(d)
    writable(d / "run.h5")
    with h5py.File(str(d / "run.h5"), "r+") as f:
        ds = f["points"]["smu.I"]
        ds[...] = ds[()] * 10.0   # the data, ten times better
    writable(d / rundata.SEAL)
    (d / rundata.SEAL).unlink()   # and its seal file deleted
    problem = "run %s: its seal file is gone, and run.h5 changed since it was kept" % run
    assert problem in rt.verify_records()
    assert rt.tidy_runs()["sealed"] == 0 and rundata.sealed(d) is None   # Kelvin never seals it again
    assert [e for e in rt.gate.log.events() if e["type"] == "run_sealed" and e.get("run") == run] == []
    said = rt.verify_records()
    assert problem in said and "No problems" not in said, said
    rt.gate.log.write("run_sealed", by="kelvin", run=run, sha256=rundata.sha256_file(d / "run.h5"), late=True)
    assert "run %s: the record kept its seal %s…, and later said another" % (run, kept[:12]) in rt.verify_records()


def test_d2_a_seal_file_deleted_from_an_unchanged_run_is_said(tmp_path):
    rt = runtime(tmp_path / "data")
    run, d = measured(rt)
    writable(d / rundata.SEAL)
    (d / rundata.SEAL).unlink()
    assert "run %s: its seal file is gone; run.h5 is as the record kept it" % run in rt.verify_records()


def test_d4_a_signed_entry_changed_in_the_file_is_no_longer_shown_as_signed(tmp_path):
    rt = runtime(tmp_path / "data")
    rt.user = "ann"
    eid = rt.write_entry("Cool-down 3", "Cooled D3 to base. The contacts were fine.").split()[2]
    rt.sign_entry(eid)
    p = notebook.path_of(rt.data_dir)
    p.write_bytes(p.read_bytes().replace(b"The contacts were fine.", b"Contact 3 was open."))
    shown = rt.notebook(entry=eid)
    assert "(altered; " in shown and "signed by ann" not in shown, shown
    said = rt.verify_records()
    assert "the notebook, line 2: it doesn't follow the line before it" in said, said
    assert "the notebook, entry %s: v1's text was changed after it was kept" % eid in said
    assert "ann's signature on v1 doesn't match its text now" in said
    rt.user = "bo"
    with pytest.raises(ValueError, match="not signed: "):
        rt.sign_entry(eid, "witness")


def test_d4_verify_walks_each_runs_notes(tmp_path):
    rt = runtime(tmp_path / "data")
    run, d = measured(rt)
    rt.annotate(run, "the sample warmed at point 2", flag="bad", by="human")
    rt.annotate(run, "looked again: it is fine", flag="none", by="human")
    notes = d / "notes.jsonl"
    notes.write_bytes(notes.read_bytes().replace(b"warmed at point 2", b"was fine all along"))
    said = rt.verify_records()
    assert "run %s's notes (notes.jsonl), line 2: it doesn't follow the line before it" % run in said, said


def test_d6_verify_misses_no_run_deleted_whole(tmp_path):
    rt = runtime(tmp_path / "data")
    run, d = measured(rt)
    for q in d.iterdir():
        writable(q)
    shutil.rmtree(str(d))
    said = rt.verify_records()
    assert "run %s: the record kept its seal, but its folder is gone" % run in said, said


def test_d6_a_record_cut_before_a_runs_end_is_found(tmp_path):
    rt = runtime(tmp_path / "data")
    rt.home = tmp_path / "home"   # Kelvin's own folder, where the record's end is kept
    run, d = measured(rt)
    rt.gate.log.settle()
    lines = rt.events_path.read_bytes().splitlines(keepends=True)
    cut = next(i for i, x in enumerate(lines) if b'"type": "run_end"' in x and run.encode() in x)
    rt.events_path.write_bytes(b"".join(lines[:cut]))   # its end cut, with the run's seal
    writable(d / "run.h5")
    with h5py.File(str(d / "run.h5"), "r+") as f:
        f["points"]["smu.I"][...] = f["points"]["smu.I"][()] * 10.0
    writable(d / rundata.SEAL)
    (d / rundata.SEAL).write_bytes(("%s *run.h5\n" % rundata.sha256_file(d / "run.h5")).encode("ascii"))
    said = rt.verify_records()
    assert "your record: it was cut short: it had" in said and "No problems" not in said, said


def test_d6_the_records_end_goes_with_it_when_the_data_folder_moves(tmp_path):
    from labharness.eventlog import ENDS_FILE, read_ends
    rt = runtime(tmp_path / "data")
    rt.home = tmp_path / "home"
    measured(rt)
    rt.gate.log.settle()
    old = os.path.abspath(str(rt.events_path))
    assert old in read_ends(rt.home / ENDS_FILE)
    rt.set_data_dir(tmp_path / "new", move=True)
    ends = read_ends(rt.home / ENDS_FILE)
    assert old not in ends and os.path.abspath(str(rt.events_path)) in ends
    said = rt.verify_records()
    assert "No problems" in said, said


# ---------------------------------------------------------------- D3: a torn line


class HalfThenFull:
    """A file on a disk that fills: takes the first 20 bytes of a write, then says ENOSPC."""

    def __init__(self, real):
        self.real, self.taken = real, False

    def fileno(self):
        return self.real.fileno()

    def write(self, data):
        if not self.taken:
            self.taken = True
            return self.real.write(bytes(data[:20]))
        raise OSError(errno.ENOSPC, "No space left on device")

    def flush(self):
        self.real.flush()

    def close(self):
        self.real.close()


def test_d3_a_line_the_disk_took_in_part_is_ended_and_the_record_reads_on(tmp_path):
    path = tmp_path / "a" / "events.jsonl"
    log = EventLog(path)
    for i in range(3):
        log.write("command", i=i)
    with log.holding():
        log.settle()
        log._held = HalfThenFull(log._held)   # the disk fills part-way through the next line
        log.write("set", name="smu.V", value=1.0)
        log.settle()
        log._retry_at = 0.0                   # room again
        log.write("set", name="smu.V", value=2.0)
        log.settle()
    v = verify_chain(path)
    assert v["breaks"] == [{"line": 4, "why": "not a record line"}], v   # the part, named: no break of Kelvin's own
    assert [e.get("value") for e in log.events() if e["type"] == "set"] == [1.0, 2.0]


def test_d3_the_nul_bytes_a_power_cut_leaves_never_break_the_record(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    for i in range(3):
        log.write("command", i=i)
    log.settle()
    with open(path, "ab") as f:
        f.write(b"\x00" * 48)   # the last line's bytes never reached the disk
    log = EventLog(path)        # the next start
    log.write("lab_ready")
    log.settle()
    v = verify_chain(path)
    assert v["lines"] == 5 and v["breaks"] == [{"line": 4, "why": "not a record line"}], v
    assert [e["type"] for e in log.events()] == ["command"] * 3 + ["lab_ready"]


def test_d3_the_runtimes_tools_read_a_record_with_a_torn_line(tmp_path):
    rt = runtime(tmp_path / "data")
    rt.home = tmp_path / "home"
    rt.record_step("loaded chip 2", sample="D3")
    rt.gate.log.settle()
    with open(rt.events_path, "ab") as f:
        f.write(b'{"seq": 99, "wall": "2026-1')   # a torn line
    rt.record_step("cooled down", sample="D3")
    assert rt.write_entry("Loading", "Loaded chip 2.").startswith("kept entry")
    assert rt.digest().startswith("since") and "chip" in rt.search_records("chip")
    assert "cooled down" in rt.sample_history("D3")
    assert [b["why"] for b in verify_chain(rt.events_path)["breaks"]] == ["not a record line"]


# ---------------------------------------------------------------- D5: the number check


def test_d5_the_number_check_compares_units_and_both_ends():
    def missing(text, records):
        return numbercheck.check(text, records)["missing"]
    assert missing("The gate was held at 50 mV.", [{"smu.V": 50.0, "unit": "V"}]) == ["50 mV"]
    assert missing("The gate was held at 50 mV.", [{"value": 50, "unit": "mV"}]) == []
    assert missing("The gate was held at 50 mV.", [{"smu.V": 0.05}]) == []
    assert missing("R rose from 1.5-9.9 kOhm.", [{"R": 1500.0}]) == ["9.9 kOhm"]
    assert missing("R rose from 1.5-9.9 kOhm.", [{"R": 1500.0}, {"R": 9900.0}]) == []
    assert missing("Tc = 4.2 ± 3.9 K", [{"Tc": 4.2}]) == ["± 3.9 K"]
    assert missing("Tc = 4.2 ± 0.1 K", [{"name": "Tc", "value": 4.2, "error": 0.1, "unit": "K"}]) == []
    assert missing("a resistance of 61 Ohm", [{"key": "smu.I", "unit": "A", "n": 61}]) == ["61 Ohm"]


def test_d5_an_events_line_number_is_no_measurement(tmp_path):
    rt = runtime(tmp_path / "data")
    for _ in range(45):
        rt.record_step("checked the wiring")   # the day's record: lines 1 to 45 and more
    run, _ = measured(rt)
    said = rt.write_entry("Gate sweep", "The two-terminal resistance was 41 Ohm at 42 K, and the peak sat at "
                                        "43 mV [run:%s]." % run)
    assert "0 of 3 numbers are in the records it cites; not found: 41 Ohm, 42 K, 43 mV" in said, said


# ---------------------------------------------------------------- D8, D9, D10, D11, D12


def test_d8_samples_changed_at_the_same_moment_are_all_kept(tmp_path):
    from labharness.samples import SampleBook
    errors = []

    def many(prefix):
        for i in range(150):
            try:
                SampleBook(tmp_path).update(None, "%s%d" % (prefix, i), {"kind": "chip"}, "human")
            except Exception as e:   # noqa: BLE001 - every failure counts
                errors.append("%s: %s" % (type(e).__name__, e))
    threads = [threading.Thread(target=many, args=(p,)) for p in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(SampleBook(tmp_path).all()) == 300
    assert not list(tmp_path.glob("*.tmp"))


def test_d9_a_catalog_damaged_past_its_first_page_is_built_again(tmp_path):
    rt = runtime(tmp_path / "data")
    run, _ = measured(rt)
    rt.catalog.sync(rt.runs)
    assert run in rt.find_runs()
    path = Path(rt.catalog.path)
    size = path.stat().st_size
    page = sqlite3.connect(str(path)).execute("PRAGMA page_size").fetchone()[0]
    with open(path, "r+b") as f:
        f.seek(page)   # its first page (the header and the schema) as it was
        f.write(os.urandom(size - page))
    assert run in rt.find_runs()
    assert rt.tidy_runs()["failed"] == 0 and rt.catalog.count() == 1
    assert integrity.run_folders(rt.runs.iterdir())   # (the runs themselves were never touched)


def test_d10_a_plan_name_ending_in_a_dot_gives_a_run_one_id(tmp_path):
    store = RunStore(tmp_path / "runs", RunFolders(tmp_path))
    for name in ("IV at 4 K.", "warm up..."):
        rec = store.create(name, {"plan": name}, "me")
        assert not rec.run_id.endswith(".") and rec.path.name == rec.run_id and rec.path.is_dir()
        rec.abandon()
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == sorted(
        p.name for p in (tmp_path / "runs").iterdir() if (p / rundata.RECORD).exists())   # no empty folder left
    seal = "a" * 64   # an older Kelvin's run of that name: the record says its id with the dot, its folder without
    d = tmp_path / "old" / "20261114-115754-IV_at_4_K"
    d.mkdir(parents=True)
    (d / rundata.RUN_FILE).write_bytes(b"changed")
    (d / rundata.SEAL).write_bytes(("%s *run.h5\n" % rundata.sha256_file(d / rundata.RUN_FILE)).encode("ascii"))
    recorded = integrity.recorded_seals([{"type": "run_end", "run": "20261114-115754-IV_at_4_K.", "sha256": seal}])
    assert [r[0] for r in integrity.check_runs([d], recorded)["reseal"]] == [d.name]


def test_d10_two_runs_of_one_name_in_the_same_second_both_start(tmp_path):
    store = RunStore(tmp_path / "runs", RunFolders(tmp_path))
    made, failed = [], []

    def one(name, gate):
        gate.wait()
        try:
            r = store.create(name, {"plan": name}, "me")
            made.append(r.run_id)
            r.abandon()
        except OSError as e:
            failed.append(str(e))
    for n in range(40):
        gate = threading.Barrier(2)
        threads = [threading.Thread(target=one, args=("import%d" % n, gate)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert failed == [] and len(made) == len(set(made)) == 80


def test_d11_a_signature_lands_on_the_version_seen_and_the_writer_never_witnesses(tmp_path):
    rt = runtime(tmp_path / "data")
    rt.user = "ann"
    eid = rt.write_entry("Cool-down 3", "Cooled D3 to base; the contacts were fine.").split()[2]
    seen = notebook.latest(notebook.read(rt.data_dir)[eid])["version"]   # what ann opened
    rt.write_entry(body="Cooled D3 to base; contact 3 was open and the data are not to be trusted.", entry=eid)
    with pytest.raises(ValueError, match="v2 of this entry came in after you opened it"):
        rt.sign_entry(eid, "author", seen)
    assert notebook.read(rt.data_dir)[eid]["signs"] == []
    eid2 = rt.write_entry("Leak check", "Gate leakage checked by ann.").split()[2]   # ann wrote it
    rt.user = "bob"
    rt.sign_entry(eid2, "author", 1)
    rt.user = "ann"
    with pytest.raises(ValueError, match="a witness is someone other than who wrote it"):
        rt.sign_entry(eid2, "witness", 1)


def test_d11_the_page_signs_the_version_it_shows(tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from labharness.web.app import STATIC, create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    rt = runtime(tmp_path / "data")
    rt.user = "ann"
    eid = rt.write_entry("Cool-down 3", "Cooled D3 to base.").split()[2]
    rt.write_entry(body="Cooled D3 to base; the still heater was off.", entry=eid)
    settings = Settings(tmp_path / "settings.json")
    client = TestClient(create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"]))
    h = {"X-Kelvin-Token": settings.access_key}
    r = client.post("/api/notebook/sign", json={"entry": eid, "role": "author", "version": 1}, headers=h)
    assert r.status_code == 400 and "came in after you opened it" in r.json()["error"], r.text
    r = client.post("/api/notebook/sign", json={"entry": eid, "role": "author", "version": 2}, headers=h)
    assert r.status_code == 200 and r.json()["signed"]["version"] == 2
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'body: { entry: id, role, version }' in js and 'nbSign(e.id, "author", e.version)' in js


def test_d12_a_damaged_derived_h5_leaves_the_sealed_run_readable(tmp_path):
    rt = runtime(tmp_path / "data")
    run, d = measured(rt)
    rundata.set_analysis(d, dict(rundata.read_h5(d).analysis or {}, note="kept"))
    size = (d / rundata.DERIVED).stat().st_size
    with open(d / rundata.DERIVED, "r+b") as f:
        f.truncate(size // 2)   # cut short
    assert rundata.check_seal(d)[0] == "intact"
    assert rundata.load(d).meta["run_id"] == run and rundata.card(d)
    assert not rt.describe_run(run).startswith("cannot read")
    rt.catalog.add(d)
    rundata.set_analysis(d, {"assessment": "looked at again", "series": []})   # a new one begun, the damaged aside
    assert rundata.read_h5(d, with_rows=False).analysis["assessment"] == "looked at again"
    assert (d / (rundata.DERIVED + ".damaged")).exists() and not (d / (rundata.DERIVED + ".part")).exists()


def test_d12_a_change_that_fails_part_way_leaves_derived_h5_as_it_was(tmp_path):
    rt = runtime(tmp_path / "data")
    _, d = measured(rt)
    rundata.set_analysis(d, {"assessment": "first", "series": []})
    before = (d / rundata.DERIVED).read_bytes()
    with pytest.raises(RuntimeError):
        with rundata.DERIVED_LOCK, rundata.changing_derived(d) as f:
            del f["analysis"]
            raise RuntimeError("Kelvin ended here")
    assert (d / rundata.DERIVED).read_bytes() == before and not (d / (rundata.DERIVED + ".part")).exists()
    assert rundata.read_h5(d, with_rows=False).analysis["assessment"] == "first"
