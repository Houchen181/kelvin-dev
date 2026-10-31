"""The review of the Runs panel's library and Run again (0.3.35), each finding with its fix: a run's script.py runs
again only as Kelvin recorded it, the AI never makes a script runnable or rewrites one a person chose, a run again is
filed as the run was, the queue runs exactly what a person approved, and the rest."""
import asyncio
import json
import sys

import pytest
import yaml

pytest.importorskip("mcp")

from labharness import ScriptedApprover, folderfiles, rundata, scriptrun  # noqa: E402
from labharness.eventlog import RunFolders  # noqa: E402
from labharness.gate import Approver  # noqa: E402
from labharness.scanlibrary import plan_id  # noqa: E402

from test_scan_library import (READS, SCRIPT, SWEEP, finish, portal, run_id_of, script_runtime,  # noqa: E402,F401
                               scripts, virtual, write)


class During(Approver):
    """A person who answers from a list, and while the first card waits, something else happens (during)."""

    def __init__(self, answers, during=None):
        self.answers, self.during, self.requests = list(answers), during, []

    def approve(self, request):
        self.requests.append(request)
        if self.during is not None:
            happen, self.during = self.during, None
            happen()
        if not self.answers:
            raise AssertionError("unexpected approval request:\n%s" % request)
        return self.answers.pop(0)


def ai_saves(rt):
    """What a chat's write_file passes to folderfiles (web/agent.py): Kelvin's own folders, the data folder, runs
    the chat may not see."""
    return dict(own=[], data=rt.data_dir, unseen=lambda d: not rt.may_see(rt._run_marks(d)))


# ------------------------------------------------------------------ 1: a version runs only what Kelvin recorded

def test_a_planted_run_never_runs_as_a_version(tmp_path, scripts):
    """Review finding 1 (fake_run_version.py): the AI wrote a run's folder (script.py, meta.json, plan.yaml) into a
    chat's folder, and `version:` ran its code."""
    write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([True]))
    chat = tmp_path / "chatfolder"
    chat.mkdir()
    RunFolders(rt.data_dir).remember(chat)
    fake = "20261031-101500-sweep.py"
    for name in ("script.py", "meta.json", "plan.yaml", "data.jsonl", "snapshot_start.json", "record.bin", "run.h5",
                 "output.txt", "commands.jsonl", "SCRIPT.PY"):
        with pytest.raises(folderfiles.FolderError, match="only Kelvin itself writes"):
            folderfiles.save(chat, "%s/%s" % (fake, name), "print('AI-written code')\n", **ai_saves(rt))
    for odd in ("script.py.", "script.py ", "script.py::$DATA", fake + "./script.py", fake + " /script.py"):
        with pytest.raises(folderfiles.FolderError):   # names Windows saves as the run's own script.py
            folderfiles.save(chat, odd if odd.startswith(fake) else "%s/%s" % (fake, odd), "print('x')\n",
                             **ai_saves(rt))
    with pytest.raises(folderfiles.FolderError, match="isn't one Kelvin saves"):
        folderfiles.save(chat, "notes/helper.py.", "x\n", **ai_saves(rt))
    assert not (chat / fake).exists()
    folderfiles.save(chat, "notes/meta.json", "{}", **ai_saves(rt))             # a folder not named as a run: fine
    assert RunFolders(rt.data_dir).find("notes") is None                         # and never a run in a chat's folder

    # a run's folder made some other way, without the record only Kelvin writes: nothing of it runs
    (chat / fake).mkdir()
    (chat / fake / "script.py").write_text("print('planted')\n", encoding="utf-8")
    (chat / fake / "meta.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")
    (chat / fake / "plan.yaml").write_text("plan: sweep.py\nscript: %s\n" % (scripts / "sweep.py"), encoding="utf-8")
    said = rt.queue_add("script: sweep.py\nversion: %s" % fake)
    assert said.startswith("not added: %s is not a run Kelvin recorded" % fake), said
    with pytest.raises(scriptrun.ScriptError, match="not a run Kelvin recorded"):
        rt._prepare_script("sweep.py", version=fake)
    ghost = "20261031-101600-ghost.py"                                           # a script that isn't there at all
    (chat / ghost).mkdir()
    (chat / ghost / "data.jsonl").write_text("", encoding="utf-8")
    (chat / ghost / "plan.yaml").write_text("plan: ghost.py\nscript: %s\n" % (scripts / "ghost.py"), encoding="utf-8")
    assert rt.queue_add("script: %s\nversion: %s" % (scripts / "ghost.py", ghost)).startswith("not added")

    # a real run whose script.py changed since it ran: refused, the text it ran is fingerprinted in its record
    ran = rt.run_script("sweep.py")
    assert "script sweep.py done" in ran, ran
    real = run_id_of(ran)
    meta = rundata.meta(rt._run_dir(real))
    text = (rt._run_dir(real) / "script.py").read_text(encoding="utf-8")
    assert meta["script_sha256"] == scriptrun.fingerprint(text)
    assert rt.queue_add("script: sweep.py\nversion: %s" % real).startswith("added script")
    rt.queue.items.clear()
    (rt._run_dir(real) / "script.py").write_text(text + "print('changed')\n", encoding="utf-8")
    said = rt.queue_add("script: sweep.py\nversion: %s" % real)
    assert "is not the text it ran" in said and said.startswith("not added"), said
    view = rt.run_again_view(real)["script"]
    assert view["as_ran"] is False and "not the text it ran" in view["as_ran_why"]


def test_a_run_from_before_the_fingerprint_doesnt_run_as_it_ran(tmp_path, scripts, monkeypatch):
    write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([True]))
    with monkeypatch.context() as m:   # as a run recorded before Kelvin kept the fingerprint
        m.setattr(scriptrun, "fingerprint", lambda text: None)
        old = run_id_of(rt.run_script("sweep.py"))
    said = rt.queue_add("script: sweep.py\nversion: %s" % old)
    assert said.startswith("not added") and "kept no fingerprint" in said and "as it is now" in said, said
    assert rt.run_again_view(old)["script"]["as_ran"] is False


# ------------------------------------------------------------------ 2: what may run stays a person's choice

def test_the_ai_never_forges_the_library_nor_rewrites_a_script_a_person_chose(tmp_path, scripts):
    """Review finding 2 (forge_library.py): with the data folder inside a chat's folder, write_file forged a
    library entry ("source": "you") and an AI-written script outside the scripts folder ran."""
    write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([]))
    chat = tmp_path                                                   # a chat folder that holds the data folder
    for name in ("queue.json", "folders.json", "library.json"):
        with pytest.raises(folderfiles.FolderError, match="only Kelvin itself writes"):
            folderfiles.save(chat, "data/" + name, "{}", **ai_saves(rt))
    evil = folderfiles.save(chat, "notes/evil.py", "print('AI code')\n", **ai_saves(rt))[0]
    assert rt.library.chosen() == []
    assert rt.queue_add("script: %s" % evil).startswith("not added: %s is outside the lab's scripts folder" % evil)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    mine = write(elsewhere, "probe.py", READS)
    rt.library_import(str(mine))
    kept = [str(scripts)] + rt.library.chosen()                       # what web/agent.py's _kept gives
    for target in (mine, scripts / "sweep.py", scripts / "new_helper.py"):
        with pytest.raises(folderfiles.FolderError, match="only a person changes it"):
            folderfiles.save(target.parent, target.name, "print('AI code')\n", kept=kept, **ai_saves(rt))
    assert "AI code" not in mine.read_text() and "AI code" not in (scripts / "sweep.py").read_text()
    folderfiles.save(elsewhere, "other.py", "print(1)\n", kept=kept, **ai_saves(rt))   # beside it: the chat's own


def test_in_a_chat_write_file_refuses_the_scripts_folder(tmp_path, scripts):
    from labharness.agent.server import build_server
    from labharness.web.agent import ChatAgent
    from labharness.web.store import ChatStore
    from test_web import Scripted
    write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([]))
    provider = Scripted([[{"type": "tool_call", "id": "w1", "name": "write_file",
                           "input": {"path": "sweep.py", "content": "print('rewritten')\n"}}],
                         [{"type": "text", "text": "Done."}]])
    store = ChatStore(tmp_path / "chats")
    agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
    assert agent._kept() == [str(scripts)]
    chat = store.create("scripted:m1")
    chat["folder"] = str(scripts)                                     # the person chose the scripts folder for it
    store.save(chat)

    async def emit(e):
        pass

    async def day():
        await agent.start()
        try:
            agent.claim(chat["id"], "scripted:m1")
            await agent.run_turn(chat["id"], "fix my sweep", "scripted:m1", provider, emit)
        finally:
            await agent.close()
    asyncio.run(day())
    result = next(r for m in store.load(chat["id"])["messages"] if m["role"] == "tool" for r in m["results"])
    assert result["is_error"] and "lab's scripts folder" in result["content"], result
    assert "rewritten" not in (scripts / "sweep.py").read_text()


def test_the_scripts_card_says_when_its_file_changed_since_its_last_run(tmp_path, scripts):
    script = write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([True, True, True]))
    first = run_id_of(rt.run_script("sweep.py"))
    assert not any("changed since" in line for line in rt.gate.approver.requests[0].items)
    script.write_text(script.read_text(encoding="utf-8").replace("version one", "version two"), encoding="utf-8")
    assert "done" in rt.run_script("sweep.py")
    assert rt.gate.approver.requests[1].items[1] == \
        "sweep.py has changed since its last run (%s): what runs is the file as it is now" % first
    rt.queue_add("script: sweep.py")                                  # unchanged since that run: nothing to say
    rt.queue_start()
    finish(rt)
    assert not any("changed since" in line for line in rt.gate.approver.requests[2].items)


# ------------------------------------------------------------------ 3: a run again is filed as the run was

def test_a_run_again_keeps_whose_work_it_was(tmp_path):
    rt = virtual(tmp_path, [True, True])
    chat_folder = tmp_path / "confidential chat"
    chat_folder.mkdir()
    whose = {"project": "p1", "chat": "c1", "confidential": True, "folder": str(chat_folder)}
    with rt.attributed(whose):                                        # a confidential project's chat ran it
        assert "done" in rt.run(SWEEP)
    (run_id,) = [d.name for d in chat_folder.iterdir()]
    marks = {"project": "p1", "chat": "c1", "confidential": True, "folder": str(chat_folder)}
    (entry,) = rt.library.entries()
    assert entry["marks"] == marks                                    # the library keeps its latest run's marks
    with rt.attributed({}):                                           # a person, from the Runs panel
        rt.run_again(run_id, at_end=True)
        rt.library_run(entry["id"], at_end=True)
    for item in rt.queue.items:
        assert item.whose() == dict(marks, incognito=False, unshared=False, rules=None, unpublished=False)
        assert rt.unshared_work(item.whose())                         # trimmed off the hub and phones like the run
    rt.queue.items.pop()
    assert rt.queue_start().startswith("queue started")
    finish(rt)
    again = rt.queue.items[0]
    assert again.status == "done", again.message
    meta = rundata.meta(rt._run_dir(again.run_id))
    assert meta["confidential"] and meta["chat"] == "c1" and meta["project"] == "p1"
    assert rt._run_dir(again.run_id).parent == chat_folder.resolve()  # in its chat's folder, as the first


# ------------------------------------------------------------------ 4, 5: the queue runs what was approved

def test_the_queue_runs_only_what_a_person_approved(tmp_path):
    rt = virtual(tmp_path, [])
    sneak = "step: sneak in\nset: {smu.V: 1}"
    rt.gate.approver = During([True, False], lambda: rt.queue_add(sneak, position=1, by="agent"))
    rt.queue_add(SWEEP)
    assert rt.queue_start().startswith("queue started")               # its card waited while the step came first
    finish(rt)
    first, again = rt.gate.approver.requests
    assert "sneak" not in "\n".join(first.items)                       # approved: the scan alone
    assert again.items[0].startswith("These were added to the queue, or changed, after it was approved")
    assert [i.status for i in rt.queue.items] == ["waiting", "waiting"]   # asked again, declined: nothing ran
    assert rt.queue.state == "paused" and "after the queue was approved" in rt.queue.reason

    rt.gate.approver = During([True])                                  # approved this time: both run
    assert rt.queue_start().startswith("queue started")
    finish(rt)
    assert [i.status for i in rt.queue.items] == ["done", "done"]


def test_a_start_that_lost_the_race_never_starts_a_second_loop(tmp_path, monkeypatch):
    rt = virtual(tmp_path, [])
    rt.queue_add("step: wait a moment\nwait: 1")
    approve = rt._queue_approve

    def meanwhile(*args, **kwargs):   # another start (a second click, another chat) won while this one asked
        got = approve(*args, **kwargs)
        rt.queue.state = "running"
        return got
    monkeypatch.setattr(rt, "_queue_approve", meanwhile)
    assert rt.queue_start().startswith("the queue is already running")
    assert rt._queue_thread is None


def test_run_again_while_the_queue_pauses_keeps_the_pause(tmp_path):
    rt = virtual(tmp_path, [])
    rt.queue_add("step: later\nwait: 1")
    rt.queue.state = "pausing"                                        # finishing its item, then pausing
    out = rt.queue_run_now(["step: now\nwait: 1"])
    now, later = rt.queue.items
    assert now.name == "now" and now.then_pause and not later.then_pause
    assert "start it again to run it, and it pauses after it" in out["message"]
    rt.queue.state = "paused"
    rt.queue_start()
    finish(rt)
    assert now.status == "done" and later.status == "waiting" and rt.queue.state == "paused"


# ------------------------------------------------------------------ 6: a failed restore holds its run back

def test_a_restore_that_doesnt_finish_skips_the_script(tmp_path, scripts):
    write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([True]))  # the first run only: the second never asks
    run_id = run_id_of(rt.run_script("sweep.py"))
    rt.set("lockin", "tc", 0.3)
    rt.run_again(run_id, restore=True, at_end=True)
    step, item = rt.queue.items
    assert step.kind == "step" and step.restores == item.id and item.kind == "script"
    rt.lab.switch("lockin", False)                                    # the restore can't set it now
    rt.queue_set_on_error("continue")                                 # even going on past failures
    rt.queue_start()
    finish(rt)
    assert step.status == "failed" and item.status == "skipped", (step.message, item.message)
    assert item.message.startswith("not run: the lab as run %s started didn't finish" % run_id)
    assert any(line.startswith("Skipped: #2 sweep.py: not run") for line in rt.queue.last["lines"])


# ------------------------------------------------------------------ 7: the library's Run, as last used

def test_the_library_runs_with_its_python_and_only_the_values_a_person_changed(tmp_path, scripts):
    script = write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([True, True]))
    rt.run_script("sweep.py", {"VMAX": 0.4})                          # the file's own value: no change
    (entry,) = rt.library_view()["scripts"]
    assert entry["params"] == {} and "VMAX" not in rt.gate.approver.requests[0].items[0]
    rt.run_script("sweep.py", {"VMAX": 0.6})
    (entry,) = rt.library_view()["scripts"]
    assert entry["params"] == {"VMAX": 0.6} and entry["python"].endswith("Python %d.%d" % sys.version_info[:2])
    script.write_text(script.read_text(encoding="utf-8").replace("VMAX = 0.4", "VMAX = 0.5"), encoding="utf-8")
    rt.library_run(entry["id"], params={}, at_end=True)              # the file's values now
    item = yaml.safe_load(rt.queue.items[-1].text)
    assert item == {"script": str(script.resolve()), "python": entry["python"]}   # its Python, as it ran
    prep = rt._prepare_script("sweep.py", {"VMAX": 0.5})
    assert prep.params == {} and prep.changes == []                  # what the file has is no change of a person's


# ------------------------------------------------------------------ 9, 10: the library goes with the data

def test_moving_the_data_folder_takes_the_library_along(tmp_path):
    rt = virtual(tmp_path, [True])
    assert "done" in rt.run(SWEEP)
    old = rt.data_dir / "library.json"
    assert old.exists()
    new = tmp_path / "new data"
    rt.set_data_dir(str(new), move=True)
    assert not old.exists() and (new / "library.json").exists()
    assert [e["name"] for e in rt.library.entries()] == ["gate_sweep"]


def test_a_restored_scan_is_noted_under_its_own_entry(tmp_path):
    rt = virtual(tmp_path, [True, True, True])
    assert "done" in rt.run(SWEEP)
    (entry,) = rt.library.entries()
    first = entry["runs"][0]
    rt.set("lockin", "frequency", 77.7)
    rt.run_again(first, restore=True)                                  # its setup has more in it now
    finish(rt)
    restored = rt.queue.items[0]
    assert restored.status == "done" and restored.library == plan_id(entry["text"]), restored.message
    assert "lockin.frequency" in yaml.safe_load(restored.text)["setup"]
    rt.set("lockin", "frequency", 55.5)
    rt.run_again(restored.run_id, restore=True)                        # a run of the restored text, restored again
    finish(rt)
    (entry,) = rt.library.entries()                                    # one scan, every run of it
    assert entry["count"] == 3 and entry["runs"][:2] == [rt.queue.items[-1].run_id, restored.run_id]


# ------------------------------------------------------------------ 12, 13, 14: what the panel is told

def test_what_couldnt_be_read_now_is_said(tmp_path, monkeypatch):
    rt = virtual(tmp_path, [True])
    assert "done" in rt.run(SWEEP)
    run_id = rt.runs_index(1)[0]["run_id"]
    view = rt.run_again_view(run_id)
    assert view["read_now"] is True and not any("why" in c for c in view["conditions"] if c["settable"])
    rt.lab.switch("vna", False)
    view = rt.run_again_view(run_id)
    vna = [c for c in view["conditions"] if c["key"].startswith("vna.")]
    assert view["read_now"] is False and vna and all(c["now"] is None and "switched off" in c["why"] for c in vna)
    rt.lab.switch("vna", True)
    tc = rt.lab.instruments["tc"]
    real = tc.read

    def broken(param):
        if param == "setpoint":
            raise OSError("no answer")
        return real(param)
    monkeypatch.setattr(tc, "read", broken)
    view = rt.run_again_view(run_id)
    (setpoint,) = [c for c in view["conditions"] if c["key"] == "tc.setpoint"]
    assert view["read_now"] is False and setpoint["now"] is None and "no answer" in setpoint["why"]


def test_a_run_that_cant_be_read_answers_in_json(tmp_path, monkeypatch):
    rt = virtual(tmp_path, [])
    client, _ = portal(rt, tmp_path)

    def locked(*args, **kwargs):
        raise OSError(13, "Permission denied")
    monkeypatch.setattr(rt, "run_again_view", locked)
    monkeypatch.setattr(rt, "run_again", locked)
    for method in ("GET", "POST"):
        r = client.request(method, "/api/runs/20261031-101500-x/again", json={} if method == "POST" else None)
        assert r.status_code == 500 and "Permission denied" in r.json()["error"], (method, r.text)


def test_a_long_output_is_served_from_its_end(tmp_path):
    rt = virtual(tmp_path, [True])
    assert "done" in rt.run(SWEEP)
    run_id = rt.runs_index(1)[0]["run_id"]
    lines = ["line %06d: ünïcode" % k for k in range(20000)]
    body = ("\n".join(lines) + "\n").encode("utf-8")
    (rt._run_dir(run_id) / "output.txt").write_bytes(body)
    (rt._run_dir(run_id) / "commands.jsonl").write_bytes(b'{"command": "OUTP ON"}\n')
    client, _ = portal(rt, tmp_path)
    got = client.get("/api/runs/%s/files/output.txt" % run_id)
    assert got.status_code == 200 and int(got.headers["x-kelvin-cut"]) == len(body) > 256 * 1024
    assert len(got.content) <= 256 * 1024 and got.text.startswith("line ")    # from a whole line, to its end
    assert got.text.endswith("line 019999: ünïcode\n")
    small = client.get("/api/runs/%s/files/commands.jsonl" % run_id)
    assert small.status_code == 200 and "x-kelvin-cut" not in small.headers and small.text == '{"command": "OUTP ON"}\n'
