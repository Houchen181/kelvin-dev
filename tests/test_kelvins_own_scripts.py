"""Kelvin's own scripts, run as the lab's are, and auto mode for them (asked for 2026-11-15).

The person asked why Kelvin never saved code among the lab's scripts, then decided: "for the script it should be pretty
safe once the user has asked to run, so let Kelvin be able to do so". Kelvin's kelvin_... folders among the lab's
scripts now take its own scripts and their settings; save_script saves one as kelvin_scripts/<name>.py in the lab's
first scripts folder; run_script runs it as it runs the lab's own, every command to a lab instrument through the gate,
its card saying plainly that Kelvin wrote it, with all the code it approves (the script's and that of Kelvin's own
modules it imports), or what changed since a run a person approved.

Then: "no need for the approval, this should be covered by auto mode". After a review, auto mode covers only Kelvin's
own scripts, as Kelvin saved them, and runs each fenced (tests/test_kelvins_own_scripts_fence.py): the lab's own
scripts ask a person each run, as before. A run of Kelvin's goes without a card as a plan does, when every drive it sets
stays in auto's ranges on instruments Kelvin is sure of, it does no card action that drives the sample, reading it finds
nothing that reaches past the gate, and the model hasn't read text from outside the lab; else its card asks, saying
why. With auto mode off, every run asks."""
import base64
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from labharness import folderfiles, rundata, scriptrun
from labharness.clock import SimClock
from labharness.gate import ScriptedApprover
from labharness.lab import Lab
from labharness.labconfig import load_lab_config
from labharness.sim.twin import World, sim_backend_factory

from test_script_runs import LAB, folder, lab, write  # noqa: F401 (fixtures)

SWEEP = """import pyvisa
smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
smu.write("OUTP ON")
for v in (0.5, 1.0):
    smu.write("SOUR:VOLT %g" % v)
    print("I", smu.query("MEAS:CURR?"))
smu.write("SOUR:VOLT 0")
smu.write("OUTP OFF")
"""


def kelvin(lab, folder, tmp_path, answers=()):
    """A runtime whose scripts folder is the test's, and a person who answers from a list: a card nobody expected
    fails the test (ScriptedApprover)."""
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    rt = LabRuntime(lab, ScriptedApprover(list(answers)), tmp_path / "data")
    rt.scripts_folders = lambda: [str(folder)]
    rt.scripts_python = lambda: sys.executable
    rt.auto_report = "off"
    return rt


def cards(rt):
    return rt.gate.approver.requests


def auto_approved(rt):
    return [e for e in rt.gate.log.events() if e.get("type") == "ask" and e.get("answer") == "auto-approved"]


def last_run(rt, path):
    from labharness.scanlibrary import script_id
    run_id = rt.library.get(script_id(Path(path)))["runs"][0]
    return run_id, rt._run_dir(run_id)


def commands(run_dir):
    import json
    return [json.loads(ln) for ln in (run_dir / "commands.jsonl").read_text().splitlines()]


def own(folder, name):
    return (folder / "kelvin_scripts" / name).resolve()


# ------------------------------------------------------------------ saving them

def test_save_script_saves_where_run_script_runs_it(lab, folder, tmp_path):
    rt = kelvin(lab, folder, tmp_path)
    said = rt.save_script("odmr_check", SWEEP)
    path = own(folder, "odmr_check.py")
    assert said.startswith("saved %s (8 lines). Run it with run_script (script: %s)" % (path, path)), said
    assert "fenced" in said
    assert path.read_text() == SWEEP and folderfiles.made(path.parent) == [path.name]
    info = folderfiles.written(path)
    assert info["sha256"] == hashlib.sha256(SWEEP.encode()).hexdigest(), info
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d", info["at"]), info
    said = rt.save_script("odmr_check.py", SWEEP.replace("(0.5, 1.0)", "(0.5, 1.5)"))   # again: replaced, kept first
    assert "the version it replaced is kept as" in said and "(0.5, 1.5)" in path.read_text()
    assert [p.read_text() for p in (path.parent / folderfiles.KEPT).iterdir()] == [SWEEP]
    events = [e for e in rt.gate.log.events() if e.get("type") == "file_saved"]
    assert [e["tool"] for e in events] == ["save_script", "save_script"] and events[0]["path"] == str(path)


def test_save_script_refuses_what_kelvin_never_saves(lab, folder, tmp_path):
    rt = kelvin(lab, folder, tmp_path)
    for name in ("../evil", "a/b", "", "-x", "x" * 70):
        assert rt.save_script(name, "x = 1\n").startswith("not saved: name it with letters"), name
    for name in ("pyvisa", "serial", "kelvin", "kelvin_api", "sitecustomize", "usercustomize") + (
            ("json", "csv") if sys.version_info >= (3, 10) else ()):
        said = rt.save_script(name, "x = 1\n")
        assert said.startswith("not saved") and "is named as a module a script means" in said, (name, said)
    for name in ("NUL", "con", "Aux", "COM1", "lpt9"):   # names Windows keeps for its devices
        said = rt.save_script(name, "x = 1\n")
        assert said.startswith("not saved: %s is a name Windows keeps for a device" % name), said
    for name in ("test_laser", "laser_test", "conftest"):   # what a person's pytest would collect and run
        said = rt.save_script(name, "x = 1\n")
        assert said.startswith("not saved") and "named as a test a test runner collects" in said, (name, said)
    assert "doesn't parse as Python 3" in rt.save_script("broken", "def (:\n")
    assert rt.save_script("empty", "  \n").startswith("not saved: give the script's whole code")
    rt.save_script("odmr_check", SWEEP)
    (folder / "kelvin_scripts" / "theirs.py").write_text("print('mine')\n")   # the person put it there
    said = rt.save_script("theirs", "print('Kelvin')\n")
    assert said.startswith("not saved") and "a file it didn't save there" in said, said
    assert (folder / "kelvin_scripts" / "theirs.py").read_text() == "print('mine')\n"
    assert sorted(folderfiles.made(folder / "kelvin_scripts")) == ["odmr_check.py"]
    rt.scripts_folders = lambda: []
    said = rt.save_script("odmr_check", SWEEP)
    assert said.startswith("not saved: this lab has no scripts folder") and "Settings" in said and "Set up" in said


def test_write_file_saves_code_and_settings_in_kelvins_own_folders(tmp_path):
    """A chat whose folder is a scripts folder saves its own code there too, in a kelvin_... folder only."""
    lab_dir = tmp_path / "lab"
    (lab_dir / "data").mkdir(parents=True)
    kw = dict(own=[], data=tmp_path / "kelvin-data", kept=[str(lab_dir)])
    for rel in ("kelvin_scripts/odmr.py", "kelvin_scripts/odmr.ipynb", "kelvin_scripts/settings.json",
                "kelvin_scripts/run.yaml", "kelvin_scripts/run.yml", "kelvin_scripts/pyproject.toml",
                "kelvin_scripts/rig.ini", "kelvin_scripts/setup.cfg", "kelvin_scripts/__init__.py",
                "kelvin_scripts/testing.py", "kelvin_scripts/latest.py",
                "kelvin-tools/helpers/fit.py", "kelvin_reports/notes.md"):
        target, _ = folderfiles.save(lab_dir, rel, "x = 1\n", **kw)
        assert folderfiles.written(target) is not None, rel


def test_the_refusals_that_stay(tmp_path):
    """Programs and shortcuts, a file named as a module a script means (Kelvin's stand-in's, pyvisa and pyserial the
    gate goes through, Python's start-up hooks and its own modules) or as a test a runner collects, the person's own
    folders, a new folder of another name, a file Kelvin didn't save, and Python environments: none of them changes."""
    lab_dir = tmp_path / "lab"
    (lab_dir / "data").mkdir(parents=True)
    (lab_dir / ".venv").mkdir()
    (lab_dir / ".venv" / "pyvenv.cfg").write_text("home = x\n")
    kw = dict(own=[], data=tmp_path / "kelvin-data", kept=[str(lab_dir)])
    folderfiles.save(lab_dir, "kelvin_scripts/odmr.py", "x = 1\n", **kw)
    (lab_dir / "kelvin_scripts" / "theirs.py").write_text("y = 2\n")
    refused = {
        "odmr.py": "person's own folders", "data/odmr.py": "person's own folders",
        "cal/offsets.py": "no name of Kelvin's", "nidaqmx/__init__.py": "no name of Kelvin's",
        "kelvin_scripts/run.bat": "no .bat file", "kelvin_scripts/run.cmd": "no .cmd file",
        "kelvin_scripts/setup.ps1": "no .ps1 file", "kelvin_scripts/tool.exe": "no .exe file",
        "kelvin_scripts/odmr.lnk": "no .lnk file", "kelvin_scripts/odmr.pyw": "no .pyw file",
        "kelvin_scripts/x.pth": "no .pth file", "kelvin_scripts/Makefile": "no file of that kind",
        "kelvin_scripts/kelvin.py": "named as a module", "kelvin_scripts/kelvin_shim.py": "named as a module",
        "kelvin_scripts/kelvin_api.py": "named as a module", "kelvin_scripts/pyvisa.py": "named as a module",
        "kelvin_scripts/serial.py": "named as a module", "kelvin_scripts/sitecustomize.py": "named as a module",
        "kelvin_scripts/usercustomize.py": "named as a module",
        "kelvin_scripts/pyvisa/__init__.py": "named as a module",
        "kelvin_scripts/kelvin/helpers.py": "named as a module",
        "kelvin_scripts/test_laser.py": "named as a test", "kelvin_scripts/laser_test.py": "named as a test",
        "kelvin_scripts/conftest.py": "named as a test", "kelvin_scripts/sub/Test_Power.py": "named as a test",
        "kelvin_scripts/theirs.py": "a file it didn't save there",
        ".venv/kelvin_scripts/odmr.py": "person's own folders",
    }
    if sys.version_info >= (3, 10):
        refused.update({"kelvin_scripts/json.py": "named as a module",
                        "kelvin_scripts/csv/__init__.py": "named as a module"})
    for rel, why in refused.items():
        with pytest.raises(folderfiles.FolderError, match="only a person changes it") as e:
            folderfiles.save(lab_dir, rel, "x = 1\n", **kw)
        assert why in str(e.value), (rel, str(e.value))
    assert (lab_dir / "kelvin_scripts" / "theirs.py").read_text() == "y = 2\n"
    assert not (lab_dir / "cal").exists() and not (lab_dir / "kelvin_scripts" / "pyvisa").exists()
    with pytest.raises(folderfiles.FolderError, match="Python environment"):   # a venv anywhere: as strict as ever
        folderfiles.save(tmp_path, "lab/.venv/Lib/site-packages/kelvin_x/x.py", "x = 1\n", own=[],
                         data=tmp_path / "kelvin-data")


def test_a_persons_pytest_in_the_scripts_folder_runs_nothing_of_kelvins(lab, folder, tmp_path):
    """A review of 2026-11-15: the person's pytest at the scripts folder collected kelvin_scripts/test_laser.py and ran
    it, with no stand-in and no gate. Kelvin saves no file a test runner collects."""
    rt = kelvin(lab, folder, tmp_path)
    marker = tmp_path / "collected.txt"
    code = "open(%r, 'w').write('ran')\ndef test_power():\n    pass\n" % str(marker)
    assert "named as a test" in rt.save_script("test_laser", code)
    rt.save_script("laser_power", code)   # any other name: saved, and pytest leaves it alone
    write(folder, "test_their_analysis.py", "def test_ok():\n    pass\n")   # the person's own tests
    env = dict(os.environ, PYTHONPATH="")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=str(folder),
                       capture_output=True, text=True, env=env, timeout=120)
    assert "1 passed" in r.stdout, r.stdout[-600:]
    assert not marker.exists()


# ------------------------------------------------------------------ running them: the card shows all the code

def test_run_script_runs_kelvins_script_through_the_gate_and_its_card_names_kelvin(lab, folder, tmp_path):
    rt = kelvin(lab, folder, tmp_path, answers=[True])
    rt.save_script("odmr_check", SWEEP)
    said = rt.run_script("kelvin_scripts/odmr_check.py")
    assert "script odmr_check.py done" in said, said
    [card] = cards(rt)
    items = card.items
    at = next(i for i, x in enumerate(items) if x.startswith("Kelvin wrote this script (kelvin_scripts/odmr_check.py, "
                                                               "saved "))
    assert items[at].endswith("):"), items[at]
    # the code, one item of many lines, which the portal shows as written (a <pre>: app.js approvalItem), indented
    assert items[at + 1] == SWEEP and "\n    smu.write(\"SOUR:VOLT %g\" % v)\n" in items[at + 1]
    assert items[-1] == scriptrun.OWN_ASKS
    path = own(folder, "odmr_check.py")
    run_id, run_dir = last_run(rt, path)
    sent = [(c["instrument"], c["command"]) for c in commands(run_dir) if c.get("instrument") == "smu"]
    assert ("smu", "SOUR:VOLT 1") in sent and ("smu", "OUTP OFF") in sent, sent   # each through the gate
    assert rundata.load(run_dir, with_rows=False).meta["approved"] == "person"
    assert (run_dir / "script.py").read_text(encoding="utf-8") == SWEEP          # the record keeps its code
    assert lab.instrument("smu").read("V") == 0 and lab.instrument("smu").read("output") is False


def test_the_card_shows_all_the_code_it_approves(lab, folder, tmp_path):
    rt = kelvin(lab, folder, tmp_path, answers=[False])
    code = "".join("print(%d)\n" % k for k in range(300))
    rt.save_script("long_one", code)
    assert rt.run_script("kelvin_scripts/long_one.py").startswith("declined")
    assert code in cards(rt)[0].items


HELPER = ("from helper2 import gain\n\n"
          "def ramp(smu, top):\n"
          "    for v in (0.5 * gain, top):\n"
          "        smu.write('SOUR:VOLT %g' % v)\n")
HELPER2 = "gain = 1.0\n"
USES = """import pyvisa
import helper
smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
smu.write("OUTP ON")
helper.ramp(smu, 1.0)
smu.write("SOUR:VOLT 0")
smu.write("OUTP OFF")
"""


def test_the_card_shows_kelvins_own_modules_it_imports_all_the_way(lab, folder, tmp_path):
    """Not a line count: each module of Kelvin's the script imports beside it, followed all the way, with its code;
    and a module beside it that Kelvin didn't write, by name (auto mode never runs a script that imports one)."""
    rt = kelvin(lab, folder, tmp_path, answers=[False, False])
    rt.save_script("helper", HELPER)
    rt.save_script("helper2", HELPER2)
    rt.save_script("uses", USES)
    assert rt.run_script("kelvin_scripts/uses.py").startswith("declined")
    items = cards(rt)[0].items
    for name, text in (("helper.py", HELPER), ("helper2.py", HELPER2)):
        at = next(i for i, x in enumerate(items) if x.startswith("It imports %s beside it, which Kelvin wrote too "
                                                                   "(saved " % name))
        assert items[at].endswith("):") and items[at + 1] == text, items[at:at + 2]
    (folder / "kelvin_scripts" / "theirs.py").write_text("print('mine')\n")   # the person's own, beside it
    rt.save_script("uses_theirs", "import theirs\n" + SWEEP)
    rt.gate.auto = True
    assert rt.run_script("kelvin_scripts/uses_theirs.py").startswith("declined")
    items = cards(rt)[1].items
    assert "It imports theirs.py beside it, which Kelvin didn't write: the whole file at %s" % own(
        folder, "theirs.py") in items, items
    assert items[-1] == ("Auto mode does not cover this run: it imports theirs.py beside it, which Kelvin didn't "
                         "write"), items[-1]


def test_a_change_shows_what_changed_since_a_run_a_person_approved(lab, folder, tmp_path):
    rt = kelvin(lab, folder, tmp_path, answers=[True] * 4)
    path = own(folder, "uses.py")
    rt.save_script("helper", HELPER)
    rt.save_script("helper2", HELPER2)
    rt.save_script("uses", USES)
    assert "done" in rt.run_script("kelvin_scripts/uses.py")
    first, first_dir = last_run(rt, path)
    assert (first_dir / "modules" / "helper.py").read_text(encoding="utf-8") == HELPER   # kept with the run
    assert rundata.load(first_dir, with_rows=False).meta["modules"] == {
        "helper.py": hashlib.sha256(HELPER.encode()).hexdigest(),
        "helper2.py": hashlib.sha256(HELPER2.encode()).hexdigest()}
    rt.save_script("uses", USES.replace("helper.ramp(smu, 1.0)", "helper.ramp(smu, 1.5)"))
    rt.save_script("helper2", "gain = 0.5\n")
    assert "done" in rt.run_script("kelvin_scripts/uses.py")
    items = cards(rt)[1].items
    at = next(i for i, x in enumerate(items) if x.startswith("Kelvin wrote this script"))
    assert items[at].endswith("; what changed since run %s, which a person approved:" % first), items[at]
    assert "-helper.ramp(smu, 1.0)\n+helper.ramp(smu, 1.5)" in items[at + 1], items[at + 1]
    assert "import helper" not in items[at + 1]                          # the change, not the whole script again
    assert any(x.startswith("It imports helper.py beside it, which Kelvin wrote too") and
               x.endswith("; the same as in run %s" % first) for x in items), items
    at = next(i for i, x in enumerate(items) if x.startswith("It imports helper2.py"))
    assert items[at].endswith("; what changed since run %s:" % first) and "-gain = 1.0\n+gain = 0.5" in items[at + 1]
    second, _ = last_run(rt, path)
    assert "done" in rt.run_script("kelvin_scripts/uses.py")              # nothing changed since
    assert any(x.endswith("; the same code as run %s, which a person approved" % second) for x in cards(rt)[2].items)
    third, _ = last_run(rt, path)
    # a run auto mode approved is no person's yes: the next card still shows what changed since the person's last
    # (the drives a module of its sets are anywhere within their limits, so auto mode runs this one written out)
    rt.gate.auto = True
    rt.save_script("uses", SWEEP)
    assert "done" in rt.run_script("kelvin_scripts/uses.py") and len(cards(rt)) == 3
    rt.gate.auto = False
    assert "done" in rt.run_script("kelvin_scripts/uses.py")
    items = cards(rt)[3].items
    at = next(i for i, x in enumerate(items) if x.startswith("Kelvin wrote this script"))
    assert items[at].endswith("what changed since run %s, which a person approved:" % third), items[at]
    assert "-helper.ramp(smu, 1.5)\n" in items[at + 1] and "+for v in (0.5, 1.0):\n" in items[at + 1], items[at + 1]
    assert "uses.py has changed since its last run a person approved (%s): what runs is the file as it is now" \
        % third in items, items


def test_a_persons_file_in_a_kelvin_folder_is_theirs(lab, folder, tmp_path):
    rt = kelvin(lab, folder, tmp_path, answers=[True])
    rt.save_script("odmr_check", SWEEP)
    (folder / "kelvin_scripts" / "theirs.py").write_text(SWEEP)   # the person put it there
    assert "done" in rt.run_script("kelvin_scripts/theirs.py")
    assert not any(x.startswith("Kelvin wrote") for x in cards(rt)[0].items)
    assert cards(rt)[0].items[-1] == scriptrun.SCRIPT_ASKS             # a person's, as any of the lab's
    assert "Kelvin wrote" not in rt.check_script("kelvin_scripts/theirs.py")
    # nor is a script of the lab's own, anywhere
    write(folder, "sweep.py", SWEEP)
    assert "Kelvin wrote" not in rt.check_script("sweep.py")
    # one of Kelvin's that someone changed since: said so, with the code as it is now
    (folder / "kelvin_scripts" / "odmr_check.py").write_text(SWEEP.replace("(0.5, 1.0)", "(0.5, 0.7)"))
    said = rt.check_script("kelvin_scripts/odmr_check.py")
    assert re.search(r"Kelvin wrote this script \(kelvin_scripts/odmr_check\.py, saved [\d: -]+\), changed since "
                     r"outside Kelvin:", said), said
    assert "  for v in (0.5, 0.7):" in said.splitlines()
    assert said.splitlines()[-1].endswith("(odmr_check.py changed after Kelvin saved it)"), said.splitlines()[-1]
    # one Kelvin saved with Windows' line ends is as it saved it (Python reads it with \n)
    rt.save_script("crlf_check", SWEEP.replace("\n", "\r\n"))
    said = rt.check_script("kelvin_scripts/crlf_check.py")
    assert "Kelvin wrote this script (kelvin_scripts/crlf_check.py, saved " in said and "changed since" not in said


def test_check_script_says_what_the_card_says(lab, folder, tmp_path):
    rt = kelvin(lab, folder, tmp_path, answers=[False])
    rt.save_script("odmr_check", SWEEP)
    said = rt.check_script("kelvin_scripts/odmr_check.py")
    assert rt.run_script("kelvin_scripts/odmr_check.py").startswith("declined")
    lines = said.splitlines()
    for item in cards(rt)[0].items:   # each of the card's lines, as the card has it (code: each of its lines)
        for line in item.rstrip("\n").split("\n"):
            assert "  " + line in lines, line
    assert lines[-1] == (
        "Auto mode is off, so the card asks a person; with it on, it would run without the card, fenced (smu.output is "
        "one step from safe; smu.V stays within 10% of the way from safe to its limit; it is Kelvin's own, as it saved "
        "it, and runs fenced: nothing in it reaches past the gate)"), lines[-1]
    rt.gate.auto = True
    assert rt.check_script("kelvin_scripts/odmr_check.py").splitlines()[-1].startswith(
        "Auto mode is on: run_script runs it without the card, fenced (smu.output is one step from safe")
    rt.save_script("probe", 'import serial\nlaser = serial.Serial("COM11", 115200)\n')
    assert rt.check_script("kelvin_scripts/probe.py").splitlines()[-1] == (
        "Auto mode is on, but doesn't cover it: the card asks a person (it opens COM11, outside the lab: auto mode "
        "covers only what the gate checks)")
    write(folder, "sweep.py", SWEEP)   # the lab's own: its card asks each run, so check_script says no more
    said = rt.check_script("sweep.py")
    assert "run_script asks a person once:" in said and "Auto mode is" not in said


# ------------------------------------------------------------------ auto mode: Kelvin's own, as it saved them

def test_in_auto_mode_kelvins_script_runs_fenced_without_a_card_and_the_labs_asks(lab, folder, tmp_path):
    """Kelvin's own: no card, the gate on every command, and the record says auto mode approved it, why, and keeps
    the code it ran. The lab's own script asks a person each run, in auto mode too (as before 2026-11-15)."""
    rt = kelvin(lab, folder, tmp_path, answers=[False])   # the lab's script's card, declined; any other fails
    rt.gate.auto = True
    rt.save_script("odmr_check", SWEEP)
    said = rt.run_script("kelvin_scripts/odmr_check.py")
    assert "script odmr_check.py done" in said, said
    assert cards(rt) == []
    [approved] = auto_approved(rt)
    assert approved["note"].startswith("auto mode: smu.output is one step from safe; smu.V stays within 10%"), approved
    assert approved["note"].endswith("it is Kelvin's own, as it saved it, and runs fenced: nothing in it reaches "
                                     "past the gate")
    run_id, run_dir = last_run(rt, own(folder, "odmr_check.py"))
    assert rundata.load(run_dir, with_rows=False).meta["approved"] == "auto mode"
    assert (run_dir / "script.py").read_text(encoding="utf-8") == SWEEP
    assert any(c.get("instrument") == "smu" and c["command"] == "SOUR:VOLT 1" for c in commands(run_dir))
    write(folder, "sweep.py", SWEEP)
    assert rt.run_script("sweep.py").startswith("declined")
    [card] = cards(rt)
    assert card.items[-2:] == [scriptrun.SCRIPT_ASKS, "Auto mode does not cover this run: " + scriptrun.LAB_SCRIPT]


def test_a_lab_script_asks_each_run_however_deep_its_imports(lab, folder, tmp_path):
    """A review of 2026-11-15: the lab's sweep.py imported rig.py, which imported stage.py, which started cmd.exe;
    reading looked one level deep, and auto mode ran it without a card. The lab's own scripts ask each run again."""
    rt = kelvin(lab, folder, tmp_path, answers=[False])
    rt.gate.auto = True
    marker = tmp_path / "program-ran.txt"
    write(folder, "stage.py", "import subprocess, sys\nsubprocess.run([sys.executable, '-c', \"open(%r, 'w')\"])\n"
          % str(marker))
    write(folder, "rig.py", "import stage\n")
    write(folder, "sweep.py", "import rig\n" + SWEEP)
    assert rt.run_script("sweep.py").startswith("declined")
    assert len(cards(rt)) == 1 and auto_approved(rt) == [] and not marker.exists()


BEYOND = SWEEP.replace("(0.5, 1.0)", "(0.5, 5.0)")
CASES = {
    "outside port": ('import serial\nlaser = serial.Serial("COM11", 115200)\n' + SWEEP,
                     "it opens COM11, outside the lab: auto mode covers only what the gate checks"),
    "beyond auto's ranges": (BEYOND, "smu.V goes 50% of the way from safe to its limit (auto mode covers 25%)"),
    "ctypes": ("import ctypes\n" + SWEEP,
               "it imports ctypes, which reaches past the gate: auto mode covers only what the gate checks"),
    "a maker's library": ("import zhinst\n" + SWEEP, "it uses zhinst, which reaches instruments past the gate"),
    "an unsure instrument": (SWEEP, "smu.V: Kelvin added it to the lab itself"),
    "outside text": (SWEEP, "Kelvin read a web page at "),
    # what reading a script for the lab's addresses misses: a USB instrument's device file, opened as a file; code
    # put together as it runs; an instrument's web interface
    "a device file": (SWEEP + 'with open("/dev/usbtmc0", "w") as f:\n    f.write("OUTP ON")\n',
                      "it names the device /dev/usbtmc0, which a script can open past the gate: auto mode covers "
                      "only what the gate checks"),
    "code it builds": (SWEEP + 'exec("import ct" + "ypes")\n', "it runs code it puts together as it goes "
                                                              "(exec), which Kelvin can't read ahead"),
    "a network client": ("import urllib.request\n" + SWEEP + 'urllib.request.urlopen("http://10.0.0.7/on")\n',
                         "it uses urllib.request, which reaches the network past the gate"),
    # the modules it imports, followed all the way (a review of 2026-11-15), each Kelvin's own as it saved it
    "a module two levels down": ("import helper\n" + SWEEP, "it imports subprocess, which reaches past the gate"),
    "a module changed since": ("import changed\n" + SWEEP, "changed.py beside it changed after Kelvin saved it"),
    "a module with a coding line": ("import coded\n" + SWEEP, "coded.py names an encoding of its own (utf-7), which "
                                                              "Kelvin doesn't read ahead"),
    "a module from sys.path": ("import sys\nsys.path.insert(0, '..')\n" + SWEEP,
                               "probe_run.py uses sys.path, where Python finds modules, so Kelvin can't tell what it "
                               "imports"),
    "a file it changed since": (SWEEP, "probe_run.py changed after Kelvin saved it"),
}


@pytest.mark.parametrize("case", list(CASES))
def test_in_auto_mode_these_still_ask_saying_why(lab, folder, tmp_path, monkeypatch, case):
    rt = kelvin(lab, folder, tmp_path, answers=[False])
    rt.gate.auto = True
    code, why = CASES[case]
    if case == "a maker's library":   # (the test's own zhinst, on the scripts' PYTHONPATH)
        fake = tmp_path / "fakes" / "zhinst"
        fake.mkdir(parents=True)
        (fake / "__init__.py").write_text("")
    if case == "an unsure instrument":   # (the test's instruments are simulated, which Kelvin is always sure of)
        monkeypatch.setattr(rt.gate, "unsure", lambda inst: "Kelvin added it to the lab itself, and its limits are "
                                                            "the maker's" if inst.name == "smu" else None)
    if case == "outside text":
        rt.gate.note_outside("a web page")
    if case == "a module two levels down":
        rt.save_script("helper", "import helper2\n")
        rt.save_script("helper2", "import subprocess\n")
    if case == "a module changed since":
        rt.save_script("changed", "x = 1\n")
        (folder / "kelvin_scripts" / "changed.py").write_text("x = 2\n")
    if case == "a module with a coding line":   # read as UTF-7, its second line is: import ctypes (a review's)
        hidden = "+" + base64.b64encode("import ctypes".encode("utf-16-be")).decode().rstrip("=") + "-"
        assert (hidden + "\n").encode().decode("utf-7") == "import ctypes\n"
        rt.save_script("coded", "# -*- coding: utf-7 -*-\n%s0\n" % hidden)   # (as UTF-8: +AGkA... - 0)
    rt.save_script("probe_run", code)
    if case == "a file it changed since":
        (folder / "kelvin_scripts" / "probe_run.py").write_text(code + "# edited by hand\n")
    said = rt.run_script("kelvin_scripts/probe_run.py")
    assert said.startswith("declined: script probe_run.py not run"), said
    [card] = cards(rt)
    assert card.items[-1].startswith("Auto mode does not cover this run: ") and why in card.items[-1], card.items[-1]
    assert auto_approved(rt) == []


@pytest.fixture
def staged():
    """The test lab with a stage whose home search drives the sample (an action of its card)."""
    from labharness.cards import parse_card
    from labharness.homelab import builtin_registry
    from labharness.yamlio import load_yaml
    from test_experiments import STAGE
    registry = builtin_registry()
    registry.add(parse_card(load_yaml(STAGE)))
    cfg = load_lab_config(LAB + '  stage:\n    card: acme/stage-1\n    address: "GPIB0::3::INSTR"\n', registry)
    clock = SimClock()
    world = World(clock, 0)
    return Lab(cfg, registry, sim_backend_factory(world), clock, world)


def test_in_auto_mode_an_action_that_drives_the_sample_asks(staged, folder, tmp_path):
    rt = kelvin(staged, folder, tmp_path, answers=[False])
    rt.gate.auto = True
    rt.save_script("home_it", 'from kelvin import lab\nlab.do("stage.home")\n')
    assert rt.run_script("kelvin_scripts/home_it.py").startswith("declined")
    assert cards(rt)[0].items[-1] == ("Auto mode does not cover this run: it does stage.home, which drives the sample: "
                                      "a person starts that")


def test_with_auto_mode_off_every_run_asks(lab, folder, tmp_path):
    rt = kelvin(lab, folder, tmp_path, answers=[True, True])
    rt.save_script("odmr_check", SWEEP)
    write(folder, "sweep.py", SWEEP)
    for script in ("kelvin_scripts/odmr_check.py", "sweep.py"):
        assert "done" in rt.run_script(script)
    assert len(cards(rt)) == 2 and auto_approved(rt) == []
    assert not any(x.startswith("Auto mode does not cover") for c in cards(rt) for x in c.items)
