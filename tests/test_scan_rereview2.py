"""The second security re-review of the Runs panel (0.3.35), each finding with its fix: the whole folder of a script a
person imported is theirs, and so is every Python environment; a step restoring a run's settings isn't edited; the
queue never runs an item nobody started; and an edit beyond what was approved is still asked about."""
import pickle
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("mcp")

from labharness import ScriptedApprover, folderfiles, rundata  # noqa: E402

from test_scan_library import (SCRIPT, SWEEP, finish, portal, run_id_of, script_runtime, scripts,  # noqa: E402,F401
                               virtual, write)
from test_scan_rereview import LOOK, Person, kept_by_person  # noqa: E402
from test_scan_review import ai_saves  # noqa: E402

LATER = "plan: for_tonight\ntime: {every: 0.2, points: 3}\nmeasure: [smu.I]"   # waiting for later: nobody started it
LATER_DRIVE = SWEEP.replace("gate_sweep", "tonight_sweep").replace("to: 2,", "to: 1.5,")


# ------------------------------------------------------------------ 1: an imported script's folder, Python's own

def test_the_whole_folder_of_an_imported_script_is_the_persons(tmp_path, scripts):
    """Re-review 2, finding 1 (imported_bypass.py): write_file could still change what an imported script runs with: a
    package in the .venv Kelvin picks for it (in a parent folder), and the settings.json and cal.pkl it reads."""
    lab = tmp_path / "lab"                                            # the chat's folder: the person's lab folder
    proj = lab / "proj"
    (proj / "sub").mkdir(parents=True)
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(lab / ".venv")], check=True,
                   capture_output=True)
    site = next(p for p in list((lab / ".venv").glob("Lib/site-packages")) +
                list((lab / ".venv").glob("lib/python*/site-packages")))
    (site / "labutil").mkdir()
    write(site / "labutil", "__init__.py", "VALUE = 1\n")              # the person's own package
    measure = write(proj, "measure.py", "import json, pickle\nimport labutil\n"
                                        "cfg = json.load(open('settings.json'))\n"
                                        "cal = pickle.load(open('cal.pkl', 'rb'))\n"
                                        "print('measured', labutil.VALUE, cfg['vmax'], cal)\n")
    write(proj, "settings.json", '{"vmax": 1}')
    (proj / "cal.pkl").write_bytes(pickle.dumps({"offset": 0.1}, protocol=0))
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([True, True]))
    rt.scripts_python = lambda: ""                                    # none set: Kelvin picks the .venv above it
    rt.library_import(str(measure))
    assert "done" in rt.run_script(str(measure))
    assert ".venv" in rt.library_view()["scripts"][0]["python"]

    kw = dict(ai_saves(rt), kept=kept_by_person(rt))
    ran = tmp_path / "ai_code_ran.txt"
    code = "open(%r, 'w').write('x')\nVALUE = 1\n" % str(ran)
    planted = "cbuiltins\nexec\n(V%s\ntR." % ("open(%r, 'w').write('x')" % str(ran)).replace("\\", "\\u005c")
    package = str(site.relative_to(lab) / "labutil" / "__init__.py")   # in the .venv Kelvin picked for it
    for rel, text in [("proj/helpers.py", code), ("proj/sub/x.py", code), ("proj/newpkg/notes.txt", "hi\n"),
                      ("proj/settings.json", '{"vmax": 10}'), ("proj/cal.pkl", planted),
                      ("proj/data/results.csv", "x,y\n"), (package, code), (".venv/pyvenv.cfg", "home = elsewhere\n"),
                      (".venv/notes.txt", "hi\n")]:
        with pytest.raises(folderfiles.FolderError, match="only a person changes it"):
            folderfiles.save(lab, rel, text, **kw)
    assert (proj / "settings.json").read_text() == '{"vmax": 1}' and not (proj / "newpkg").exists()
    folderfiles.save(lab, "common.py", "x = 1\n", **kw)                 # beside the project, not in it: the chat's

    assert "done" in rt.run_script(str(measure))                      # as it ran: none of the AI's code
    out = rt._run_dir(rt.library_view()["scripts"][0]["last_run"]) / "output.txt"
    assert "measured 1 1 {'offset': 0.1}" in out.read_text(encoding="utf-8") and not ran.exists()


def test_nothing_is_saved_in_a_python_environment_wherever_it_is(tmp_path):
    """Re-review 2, finding 1: what Python loads runs with the lab's scripts, so write_file saves nothing in a venv
    (the one beside a script, or one set in Settings), a conda environment, a Python installation or installed
    packages, in whatever folder; the chat's own files beside them are fine, and reading them is too."""
    chat = tmp_path / "chat"
    for marker in ("venv/pyvenv.cfg", "conda/conda-meta/history", "py313/python.exe", "py/Lib/os.py",
                   "usr/lib/python3.12/os.py"):
        (chat / marker).parent.mkdir(parents=True, exist_ok=True)
        (chat / marker).write_text("")
    kw = dict(own=[], data=tmp_path / "data")
    for rel in ("venv/Lib/site-packages/labutil/__init__.py", "venv/Scripts/notes.txt", "venv/sub/new/x.py",
                "conda/Lib/json/__init__.py", "py313/python313._pth", "py313/Lib/json/__init__.py",
                "py/Lib/json/__init__.py", "usr/lib/python3.12/json/x.py", "usr/local/x.txt",
                "anything/site-packages/x.py", "deb/dist-packages/x.txt", "other/SITE-PACKAGES/x.txt"):
        with pytest.raises(folderfiles.FolderError, match="only a person changes it"):
            folderfiles.save(chat, rel, "x = 1\n", **kw)
    for rel in ("notes/venv.txt", "py313-notes.txt", "packages/x.py", "lib/x.py"):
        folderfiles.save(chat, rel, "fine\n", **kw)
    assert folderfiles.inside(chat, "venv/pyvenv.cfg", **kw) == (chat / "venv" / "pyvenv.cfg").resolve()
    assert folderfiles.environment(Path(tmp_path / "chat" / "notes" / "x.py")) is None


# ------------------------------------------------------------------ 2: a restore step isn't edited

def test_a_step_restoring_a_runs_settings_isnt_edited(tmp_path, scripts):
    """Re-review 2, finding 6 (restore_edit.py): queue_edit rewrote the step Run again queued to set back what the run
    started with; it kept its link, finished, and the script ran as if restored."""
    write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([True, True]))   # the run, then the script as queued
    run_id = run_id_of(rt.run_script("sweep.py"))
    rt.set("lockin", "tc", 0.3)
    rt.run_again(run_id, restore=True, at_end=True)
    step, item = rt.queue.items
    text = step.text
    said = rt.queue_edit(step.id, "step: the lab as run %s started\nset: {lockin.tc: 0.3}" % run_id, by="agent")
    assert said == ("not changed: %s restores the settings another item ran with, so it isn't edited: remove it and "
                    "its item, then use Run again" % step.name), said
    with pytest.raises(ValueError, match="so it isn't edited"):
        rt.queue.edit(step.id, "step: wait\nwait: 1")
    client, _ = portal(rt, tmp_path)                                  # the Queue pane alike
    r = client.post("/api/queue", json={"op": "edit", "item": step.id, "text": "step: wait\nwait: 1"})
    assert r.status_code == 400 and "isn't edited: remove it and its item" in r.json()["error"]
    assert step.text == text and step.restores == item.id and item.restored_by == step.id
    rt.queue_start()
    finish(rt)
    assert (step.status, item.status) == ("done", "done"), (step.message, item.message)
    assert rt.lab.snapshot(["lockin"])["lockin"]["tc"] == pytest.approx(0.1)   # set back as the run started


# ------------------------------------------------------------------ 3: the queue never runs what nobody started

def restore_pair(tmp_path, scripts):
    """sweep.py run once and lockin.tc changed since, a measurement waiting for later (nobody started it), then Run
    again with the restore first: [later, step, script], the script marked to pause after, as Run now leaves it."""
    write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, Person([True]))           # the first run's card: nothing else may ask
    run_id = run_id_of(rt.run_script("sweep.py"))
    rt.set("lockin", "tc", 0.3)
    rt.queue_add(LATER, by="human")
    rt.run_again(run_id, restore=True, at_end=True)
    later, step, item = rt.queue.items
    rt.queue.set_then_pause(item.id, True)
    return rt, later, step, item


def test_a_start_with_a_pause_point_runs_only_what_it_was_started_for(tmp_path, scripts):
    """Re-review 2, the new finding (skip_runs_past_pause.py): when the loop didn't stop at the item marked then_pause
    (skipped for its restore, taken out, moved), it went on into items nobody started, asking only for drives beyond
    the approval."""
    # A. the restore step fails, the queue going on past failures: its script is skipped, and the queue pauses
    rt, later, step, item = restore_pair(tmp_path / "a", scripts)
    rt.queue.move(step.id, 1)
    rt.queue.move(item.id, 2)                                         # [step, script (then pause), later]
    rt.queue_set_on_error("continue")
    rt.lab.switch("lockin", False)                                    # the restore can't set it now
    assert rt.queue_start().startswith("queue started")
    finish(rt)
    assert (step.status, item.status, later.status) == ("failed", "skipped", "waiting"), (step.message, item.message)
    assert rt.queue.state == "paused" and rt.queue.reason == "paused after the items it was started for, as asked"
    assert len(rt.gate.approver.requests) == 1
    assert any(line.startswith("Not run: #3 for_tonight") for line in rt.queue.last["lines"]), rt.queue.last

    # B. the restore step put behind its script: the script is skipped, and neither the step nor the later one runs
    rt, later, step, item = restore_pair(tmp_path / "b", scripts)
    rt.queue.move(item.id, 1)
    rt.queue.move(step.id, 2)                                         # [script (then pause), step, later]
    rt.queue_start()
    finish(rt)
    assert (item.status, step.status, later.status) == ("skipped", "waiting", "waiting")
    assert "comes after it in the queue now" in item.message and rt.queue.state == "paused"
    assert len(rt.gate.approver.requests) == 1

    # C. the item it was started for taken out while the start's card waited: nothing else runs, nor asks
    rt = virtual(tmp_path / "c", [])
    rt.queue_add(SWEEP, then_pause=True, by="human")
    rt.queue_add(LATER_DRIVE, by="human")                              # for tonight, not started: smu.V 0 to 1.5 V
    rt.queue_add(LATER, by="human")
    first = rt.queue.items[0]
    rt.gate.approver = Person([True], [lambda: rt.queue_change("remove", first.id, by="agent")])
    assert rt.queue_start().startswith("queue started: 1 scan")
    finish(rt)
    assert [(i.name, i.status) for i in rt.queue.items] == [("tonight_sweep", "waiting"), ("for_tonight", "waiting")]
    assert rt.queue.state == "paused" and len(rt.gate.approver.requests) == 1

    # D. moved behind the later one, and a measurement added at the front, while its card waited: it alone runs
    rt = virtual(tmp_path / "d", [])
    rt.queue_add(SWEEP, then_pause=True, by="human")
    rt.queue_add(LATER, by="human")
    mine = rt.queue.items[0]

    def meanwhile():
        rt.queue_change("move", mine.id, 2, by="agent")
        rt.queue_add(LOOK, position=1, by="agent")
    rt.gate.approver = Person([True], [meanwhile])
    rt.queue_start()
    finish(rt)
    assert [(i.name, i.status) for i in rt.queue.items] == [("look", "waiting"), ("for_tonight", "waiting"),
                                                            ("gate_sweep", "done")]
    assert rt.queue.state == "paused" and rt.queue.reason == "paused after gate_sweep, as asked"
    assert len(rt.gate.approver.requests) == 1


def test_a_run_while_another_runs_waits_for_the_next_start(tmp_path, monkeypatch):
    """A Run while the queue runs another Run's start (which runs only what it was started for) waits next in line,
    marked to pause after, and says so; an item the AI adds meanwhile waits too. The next start runs them."""
    rt = virtual(tmp_path, [])
    rt.queue_add(LATER, by="human")                                   # waiting, nobody started it
    run_item, said = rt._queue_item, {}

    def meanwhile(item, *args, **kwargs):   # while the Run's item runs: another Run, and an item the AI adds
        if item.name == "mine":
            said["now"] = rt.queue_run_now(["step: now\nwait: 1"])["message"]
            rt.queue_add("step: added\nwait: 1", position=1, by="agent")
        return run_item(item, *args, **kwargs)
    monkeypatch.setattr(rt, "_queue_item", meanwhile)
    assert rt.queue_run_now(["step: mine\nwait: 1"])["message"].startswith("queue started")
    finish(rt)
    assert said["now"] == ("added now next in line: the queue pauses after mine, as asked; start it again to run it, "
                           "and it pauses after it")
    assert [(i.name, i.status, i.then_pause) for i in rt.queue.items] == [
        ("added", "waiting", False), ("mine", "done", True), ("now", "waiting", True),
        ("for_tonight", "waiting", False)]
    assert rt.queue.state == "paused" and rt.queue.reason == "paused after mine, as asked"
    assert rt.queue_start().startswith("queue started: 2 steps")       # the next start: up to the one marked
    finish(rt)
    assert [i.status for i in rt.queue.items] == ["done", "done", "done", "waiting"] and rt.queue.state == "paused"


# ------------------------------------------------------------------ 4: an edit beyond what was approved

def test_an_edit_beyond_what_was_approved_is_asked_about_each_time(tmp_path):
    """Re-review 2 (edit_beyond.py), as it stays: an edit while the start's card waits, beyond what it showed, is asked
    about before it runs; another while that card waits is asked about again; the scan runs as last approved."""
    rt = virtual(tmp_path, [])
    rt.queue_add(SWEEP)
    item = rt.queue.items[0]

    def to(volts):
        return lambda: rt.queue_edit(item.id, SWEEP.replace("to: 2,", "to: %s," % volts), by="agent")
    rt.gate.approver = Person([True, True, True], [to(3), to(3.5)])
    assert rt.queue_start().startswith("queue started")
    finish(rt)
    start, again, third = rt.gate.approver.requests
    assert "0 … 2 V" in "\n".join(start.items) and "These were" not in start.items[0]
    assert again.items[0].startswith("These were added to the queue, or changed, after it was approved")
    assert "0 … 3 V" in "\n".join(again.items) and "0 … 3.5 V" in "\n".join(third.items)
    assert third.items[0].startswith("These were added") and item.status == "done"
    plan = yaml.safe_load(rundata.load(rt._run_dir(item.run_id), with_rows=False).plan_text)
    assert plan["sweep"]["smu.V"]["to"] == 3.5
