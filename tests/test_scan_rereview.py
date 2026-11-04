"""The security re-review of the Runs panel (0.3.35), each finding with its fix: the code beside a script a person
imported is theirs too, an approval covers the items as they were when it was asked, only driving the sample waits
for a person, the data folder's runs are Kelvin's own, a run's version is filed under its marks too, an item waits
for its restore, and `..` in a path is fine."""
import ctypes
import json
import os

import pytest

pytest.importorskip("mcp")

from labharness import ScriptedApprover, folderfiles, rundata  # noqa: E402
from labharness.gate import Approver  # noqa: E402

from test_scan_library import (SCRIPT, SWEEP, finish, run_id_of, script_runtime, scripts,  # noqa: E402,F401
                               virtual, write)
from test_scan_review import During, ai_saves  # noqa: E402


def kept_by_person(rt):
    """What web/agent.py's ChatAgent._kept gives write_file: the lab's scripts folders, the scripts imported."""
    return rt.scripts_folders() + rt.library.chosen()


# ------------------------------------------------------------------ 1: the code beside an imported script

def test_the_code_beside_an_imported_script_is_the_persons(tmp_path, scripts):
    """Re-review finding 1 (imported_sibling.py): write_file saved proj/helpers.py beside an imported proj/measure.py,
    which imports it, and the person's next run of the unchanged script ran the AI's code."""
    chat = tmp_path / "project"
    (chat / "proj" / "data").mkdir(parents=True)
    measure = write(chat / "proj", "measure.py", "import helpers\nprint('measured', helpers.VALUE)\n")
    write(chat / "proj", "helpers.py", "VALUE = 1\n")
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([True, True, True]))
    rt.library_import(str(measure))
    assert "done" in rt.run_script(str(measure))
    kw = dict(ai_saves(rt), kept=kept_by_person(rt))
    for path in ("proj/measure.py", "proj/helpers.py", "proj/numpy.py", "proj/helpers.PYW", "proj/fast.pyd",
                 "proj/__pycache__/helpers.cpython-313.pyc", "proj/data/__init__.py", "proj/newpkg/notes.txt",
                 "proj/data/../helpers.py",
                 "proj/data/results.csv"):   # since re-review 2, its whole folder: what it reads there too
        with pytest.raises(folderfiles.FolderError, match="only a person changes|Only a person changes"):
            folderfiles.save(chat, path, "open('ai_was_here', 'w')\n", **kw)
    assert (chat / "proj" / "helpers.py").read_text() == "VALUE = 1\n" and not (chat / "proj" / "newpkg").exists()
    folderfiles.save(chat, "notes/helpers.py", "VALUE = 2\n", **kw)              # code elsewhere in the chat: fine

    # a person (or another program) changes what it imports: its next card says so, by name
    write(chat / "proj", "helpers.py", "VALUE = 2\n")
    write(chat / "proj", "units.py", "MV = 1e-3\n")
    first = rt.library_view()["scripts"][0]["last_run"]
    assert "done" in rt.run_script(str(measure))
    card = rt.gate.approver.requests[-1]
    assert card.items[1] == ("Beside it, changed since its last run (%s) (it may import them): helpers.py, "
                             "units.py (new)" % first), card.items
    assert "done" in rt.run_script(str(measure))                     # nothing changed since: nothing to say
    assert not any(line.startswith("Beside it") for line in rt.gate.approver.requests[-1].items)
    assert rundata.meta(rt._run_dir(rt.library_view()["scripts"][0]["last_run"]))["beside"]["helpers.py"]


# ------------------------------------------------------------------ 2, 3: what is approved, and what waits

EDITED = SWEEP.replace("to: 2, step: 0.5", "to: 1.5, step: 0.25").replace("measure: [lockin.X, smu.I]",
                                                                       "measure: [lockin.X, lockin.Y, smu.I]")
WIDER = SWEEP.replace("to: 2, step: 0.5", "to: 3, step: 0.5")
LOOK = "plan: look\ntime: {every: 0.2, points: 3}\nmeasure: [lockin.X]"   # reads only: drives nothing


class Person(Approver):
    """Answers from a list; while card k waits, during[k] happens (something else, meanwhile)."""

    def __init__(self, answers, during=()):
        self.answers, self.during, self.requests = list(answers), list(during), []

    def approve(self, request):
        self.requests.append(request)
        k = len(self.requests) - 1
        if k < len(self.during) and self.during[k] is not None:
            self.during[k]()
        if not self.answers:
            raise AssertionError("unexpected approval request:\n%s" % request)
        return self.answers.pop(0)


def test_an_edit_while_its_card_waits_is_another_text(tmp_path):
    """Re-review finding 2 (edit_during_card.py): an item edited while the queue's card waited ran as edited, recorded
    as approved."""
    rt = virtual(tmp_path, [])
    item = rt.queue.add(SWEEP)
    rt.gate.approver = Person([True, False], [lambda: rt.queue_edit(item.id, WIDER, by="agent")])
    assert rt.queue_start().startswith("queue started")
    finish(rt)
    start, again = rt.gate.approver.requests
    assert "3 V" not in "\n".join(start.items) and "3 V" in "\n".join(again.items)   # the card it was asked on
    assert item.status == "waiting" and rt.queue.state == "paused"   # the wider text asked again: declined

    # within what was approved, an edit that only measures more runs as edited, asking nobody: only driving waits
    rt = virtual(tmp_path / "second", [])
    item = rt.queue.add(SWEEP)
    rt.gate.approver = Person([True], [lambda: rt.queue_edit(item.id, EDITED, by="agent")])
    rt.queue_start()
    finish(rt)
    assert item.status == "done" and len(rt.gate.approver.requests) == 1
    assert "lockin.Y" in rundata.load(rt._run_dir(item.run_id), with_rows=False).plan_text

    # and an edit while the ask-again card waits: what that card showed is what it approved, not the edit
    rt = virtual(tmp_path / "third", [])
    rt.queue.add(SWEEP)
    added = {}

    def sneak():   # while the start's card waits: a step beyond what it shows
        rt.queue_add("step: sneak in\nset: {smu.V: 3}", position=1, by="agent")
        added["step"] = rt.queue.items[0]

    def edit():    # while the card about that step (at 3 V) waits: it becomes 4 V
        rt.queue_edit(added["step"].id, "step: sneak in\nset: {smu.V: 4}", by="agent")
    rt.gate.approver = Person([True, True, False], [sneak, edit])
    rt.queue_start()
    finish(rt)
    cards = rt.gate.approver.requests
    assert len(cards) == 3 and "3 V" in "\n".join(cards[1].items) and "4 V" in "\n".join(cards[2].items)
    assert added["step"].status == "waiting" and rt.queue.state == "paused"


def test_a_measurement_added_anywhere_runs_without_a_card(tmp_path):
    """Re-review finding 3 (measure_only_friction.py): a measurement-only scan added ahead of approved items brought
    back a card for drives already approved; only driving the sample waits for a person."""
    rt = virtual(tmp_path, [])
    rt.queue.add(SWEEP)
    rt.queue.add(SWEEP.replace("gate_sweep", "second_sweep"))
    rt.gate.approver = During([True], lambda: rt.queue_add(LOOK, position=2, by="agent"))   # while its card waits
    assert rt.queue_start().startswith("queue started")
    finish(rt)
    assert len(rt.gate.approver.requests) == 1                        # the start's card only
    assert [(i.name, i.status) for i in rt.queue.items] == [("gate_sweep", "done"), ("look", "done"),
                                                            ("second_sweep", "done")]

    # a scan that drives more than was approved: asked about, alone; what was approved is not asked again
    rt = virtual(tmp_path / "second", [])
    rt.queue.add(SWEEP)
    rt.queue.add(SWEEP.replace("gate_sweep", "second_sweep"))
    rt.gate.approver = During([True, True], lambda: rt.queue_add(WIDER.replace("gate_sweep", "wider"), position=2,
                                                                by="agent"))
    rt.queue_start()
    finish(rt)
    start, again = rt.gate.approver.requests
    assert again.title.startswith("Run the queue: 1 scan") and "smu.V" in again.items[1]
    assert [i.status for i in rt.queue.items] == ["done", "done", "done"]


# ------------------------------------------------------------------ 4: the data folder's runs

def test_the_data_folders_runs_are_kelvins_own(tmp_path, scripts):
    """Re-review finding 4 (fake_run_in_data_home.py): with the data folder inside a chat's folder, write_file made
    data/runs/forged/{plan.yaml, meta.json, data.jsonl}, listed as a run, and Run again queued its plan under the
    project and chat its meta.json claimed."""
    write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([]))
    kw = dict(ai_saves(rt), kept=[str(scripts)])
    for name in ("plan.yaml", "meta.json", "data.jsonl", "notes.txt"):
        with pytest.raises(folderfiles.FolderError, match="the data folder's runs"):
            folderfiles.save(tmp_path, "data/runs/forged/" + name, "{}", **kw)
    forged = rt.data_dir / "runs" / "forged"                         # made some other way: still no run
    forged.mkdir(parents=True)
    (forged / "meta.json").write_text(json.dumps({"status": "done", "project": "p2"}), encoding="utf-8")
    (forged / "plan.yaml").write_text("plan: forged\nmeasure: [lockin.X]\n", encoding="utf-8")
    assert rt.runs.find("forged") is None and "forged" not in [d.name for d in rt.runs.iterdir()]
    with pytest.raises(KeyError):
        rt.run_again_view("forged")
    assert rt.queue_add("script: sweep.py\nversion: forged").startswith("not added")


# ------------------------------------------------------------------ 5: a version, under its run's marks too

def test_a_version_is_filed_under_its_runs_marks_too(tmp_path, scripts):
    """Re-review finding 5: queue_add with version: was filed under the chat's marks alone, so a project's chat could
    run another chat's confidential run again as shared work. Chosen: the union of both marks (it keeps the work
    confidential and lets the chat go on), never the chat's alone."""
    write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([True]))
    with rt.attributed({"project": "p1", "chat": "c1", "confidential": True, "rules": {"models": ["local"]}}):
        source = run_id_of(rt.run_script("sweep.py"))
    with rt.attributed({"project": "p1", "chat": "c2"}):           # another chat of the project, which may see it
        assert rt.queue_add("script: sweep.py\nversion: %s" % source).startswith("added")
        rt.queue_add("script: sweep.py")
        assert rt.queue_edit(rt.queue.items[1].id, "script: sweep.py\nversion: %s" % source).startswith("changed")
    for item in rt.queue.items:
        whose = item.whose()
        assert whose["chat"] == "c2" and whose["project"] == "p1" and whose["confidential"]
        assert whose["rules"] == {"models": ["local"]} and rt.unshared_work(whose)
    plain = rt.queue_add("script: sweep.py")                        # no version: the chat's own marks
    assert plain.startswith("added") and not rt.unshared_work(rt.queue.items[-1].whose())
    run = {"project": "p1", "confidential": True, "rules": {"models": ["local"]}}
    assert rt._with_marks({"project": "p2", "chat": "c9"}, run)["unshared"]                 # another project's
    assert rt._with_marks({"rules": {"models": ["any"]}}, run)["unshared"]                  # other rules
    assert rt._with_marks({"chat": "c9"}, {}) == {"chat": "c9"}


# ------------------------------------------------------------------ 6: an item waits for its restore

def test_an_item_never_starts_before_its_restore_step(tmp_path, scripts):
    write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([True]))   # the first run only: the script never runs
    run_id = run_id_of(rt.run_script("sweep.py"))
    rt.set("lockin", "tc", 0.3)
    rt.run_again(run_id, restore=True, at_end=True)
    step, item = rt.queue.items
    assert step.restores == item.id and item.restored_by == step.id
    rt.queue_change("move", step.id, 2)                                # the step after its script now
    rt.queue_start()
    finish(rt)
    assert item.status == "skipped" and "comes after it in the queue now" in item.message, item.message
    assert step.status == "done"

    rt.queue.items.clear()
    rt.set("lockin", "tc", 0.3)
    rt.run_again(run_id, restore=True, at_end=True)
    step, item = rt.queue.items
    rt.queue_change("remove", step.id)                                 # the step taken out
    rt.queue_start()
    finish(rt)
    assert item.status == "skipped" and "was taken out of the queue" in item.message, item.message


# ------------------------------------------------------------------ 7: `..` in a path

def _short(path):
    if os.name != "nt":
        return None
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 1024)
    return buf.value if n and buf.value.lower() != str(path).lower() else None


def test_dots_in_a_path_and_other_spellings_of_what_is_kept(tmp_path):
    """Re-review finding 7 (short_names.py): notes/../x.py was refused as a name ending in a dot; and what it checked
    of short names, case and the like still holds."""
    chat = tmp_path / "chat"
    scripts_dir = chat / "MeasurementScripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "sweep.py").write_text("print(1)\n")
    run = chat / "20261031-101500-longsweepname.py"
    run.mkdir()
    (run / "record.bin").write_bytes(b"x")
    imported = chat / "proj" / "long_imported_script.py"
    imported.parent.mkdir()
    imported.write_text("print(2)\n")
    kw = dict(own=[], data=tmp_path / "data", kept=[str(scripts_dir), str(imported)])
    assert folderfiles.save(chat, "notes/../x.py", "print(3)\n", **kw)[0] == (chat / "x.py").resolve()
    assert folderfiles.save(chat, "./y.txt", "y\n", **kw)[0] == (chat / "y.txt").resolve()
    refused = ["proj/sibling.py", "proj/../MeasurementScripts/x.py", "notes/x.py."]
    if os.name == "nt":   # other spellings of the same files on Windows (case, the \\?\ prefix); elsewhere other files
        refused += ["measurementscripts/NEW.PY", "PROJ/LONG_IMPORTED_SCRIPT.PY",
                    "20261031-101500-LONGSWEEPNAME.PY/SCRIPT.PY", "\\\\?\\" + str(scripts_dir / "x.py")]
    for p in (scripts_dir, run, imported):   # Windows' 8.3 names, where the volume keeps them
        short = _short(p)
        if short:
            refused.append(str(short) if p == imported else
                           os.path.join(short, "script.py" if p == run else "new.py"))
    for rel in refused:
        with pytest.raises(folderfiles.FolderError):
            folderfiles.save(chat, rel, "print('AI')\n", **kw)
    assert sorted(p.name for p in scripts_dir.iterdir()) == ["sweep.py"] and sorted(p.name for p in run.iterdir()) == \
        ["record.bin"]
