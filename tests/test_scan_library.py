"""The person's library of scans and scripts, and runs run again (the Runs panel): every run notes its plan or script
(scanlibrary.py), a person imports scripts that may then run from outside the scripts folder, the queue runs script
items and pauses after an item when asked, and a run runs again as it ran, with the settings it started with."""
import asyncio
import json
import sys

import pytest
import yaml

pytest.importorskip("mcp")

from simlab import build_virtual_lab, demo_envelope  # noqa: E402
from labharness import ScriptedApprover, rundata, scriptrun  # noqa: E402
from labharness.clock import SimClock  # noqa: E402
from labharness.homelab import builtin_registry  # noqa: E402
from labharness.lab import Lab  # noqa: E402
from labharness.labconfig import load_lab_config  # noqa: E402
from labharness.scanlibrary import MAX_RUNS, Library, params_from_changes, plan_id, script_id, summary  # noqa: E402
from labharness.sim.twin import World, sim_backend_factory  # noqa: E402

from test_script_runs import FAKE_PYVISA, FAKE_SERIAL, LAB, write  # noqa: E402

SWEEP = """plan: gate_sweep
setup: { lockin.amplitude: 0.05, smu.output: on }
sweep: { smu.V: {from: 0, to: 2, step: 0.5} }
settle: 0.1
measure: [lockin.X, smu.I]
after: { smu.V: 0 }"""

SCRIPT = """
import pyvisa

VMAX = 0.4    # the top of the sweep, in volts
rm = pyvisa.ResourceManager()
smu = rm.open_resource("GPIB0::24::INSTR")
lockin = rm.open_resource("GPIB0::8::INSTR")
print("version one")
smu.write("OUTP ON")
try:
    for v in [0, VMAX]:
        smu.write("SOUR:VOLT %g" % v)
        lockin.query("MEAS:X?")
finally:
    smu.write("SOUR:VOLT 0")
    smu.write("OUTP OFF")
"""

READS = """
import pyvisa
lockin = pyvisa.ResourceManager().open_resource("GPIB0::8::INSTR")
print("X", lockin.query("MEAS:X?"))
"""


def virtual(tmp_path, answers):
    from labharness.agent.runtime import LabRuntime
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover(list(answers)), tmp_path / "data", None)
    rt.auto_report = "off"
    return rt


@pytest.fixture
def scripts(tmp_path, monkeypatch):
    """The lab's scripts folder, with fakes of pyvisa and pyserial that refuse every real instrument."""
    fakes = tmp_path / "fakes"
    (fakes / "pyvisa").mkdir(parents=True)
    (fakes / "pyvisa" / "__init__.py").write_text(FAKE_PYVISA)
    (fakes / "serial").mkdir()
    (fakes / "serial" / "__init__.py").write_text(FAKE_SERIAL)
    monkeypatch.setenv("PYTHONPATH", str(fakes))
    folder = tmp_path / "scripts"
    folder.mkdir()
    return folder


def script_lab():
    registry = builtin_registry()
    clock = SimClock()
    world = World(clock, 0)
    return Lab(load_lab_config(LAB, registry), registry, sim_backend_factory(world), clock, world)


def script_runtime(tmp_path, folder, approver):
    from labharness.agent.runtime import LabRuntime
    rt = LabRuntime(script_lab(), approver, tmp_path / "data")
    rt.scripts_folder = lambda: str(folder)
    rt.scripts_python = lambda: sys.executable
    rt.auto_report = "off"
    return rt


def run_id_of(answer):
    import re
    m = re.search(r"saved in .*[\\/](\d{8}-\d{6}-[\w.-]*): script\.py", answer)
    assert m, answer
    return m.group(1)


def finish(rt, seconds=120):
    rt._queue_thread.join(seconds)
    assert not rt._queue_thread.is_alive(), "the queue did not finish"


# ------------------------------------------------------------------ the library itself

def test_the_library_keeps_each_scan_and_script_once(tmp_path, monkeypatch):
    lib = Library(tmp_path / "library.json")
    text = "plan: iv\nsweep:\n  smu.V: {from: 0, to: 1, step: 0.5}\nmeasure: [smu.I]\n"
    lib.note_plan(text, "iv", "smu.V 0 → 1 V", "20261031-100000-iv", when=100)
    entry = lib.note_plan(text, "iv", "", "20261031-110000-iv", when=200)
    lib.note_plan(text + "settle: 1\n", "iv", "", "20261031-120000-iv", when=300)   # another scan, same name
    assert entry["id"] == plan_id(text) and entry["count"] == 2 and entry["source"] == "kelvin"
    assert entry["runs"] == ["20261031-110000-iv", "20261031-100000-iv"] and entry["used"] == 200
    assert entry["summary"] == "smu.V 0 → 1 V" and entry["added"] == 100
    assert [e["used"] for e in lib.entries()] == [300, 200]          # the latest used first

    script = write(tmp_path, "sweep.py", "print('hi')\n")
    for k in range(MAX_RUNS + 2):   # however the path is spelled: one entry
        spelled = str(script) if k % 2 else str(tmp_path / "." / "sweep.py")
        last = lib.note_script(spelled, "run-%02d" % k, {"VMAX": k}, ["--fast"], "py", "abc", when=400 + k)
    assert last["id"] == script_id(script) and last["count"] == MAX_RUNS + 2
    assert last["runs"][0] == "run-%02d" % (MAX_RUNS + 1) and len(last["runs"]) == MAX_RUNS
    assert last["params"] == {"VMAX": MAX_RUNS + 1} and last["args"] == ["--fast"] and last["sha256"] == "abc"
    assert lib.chosen() == []                                         # Kelvin ran it: no permission from that

    (tmp_path / "elsewhere").mkdir()
    other = write(tmp_path / "elsewhere", "probe.py", "print(1)\n")
    mine = lib.add_script(other)
    assert mine["source"] == "you" and mine["count"] == 0 and lib.chosen() == [str(other.resolve())]
    assert lib.add_script(script)["count"] == MAX_RUNS + 2            # imported later: keeps its runs, now yours
    assert sorted(lib.chosen()) == sorted([str(other.resolve()), str(script.resolve())])

    assert lib.rename(mine["id"], "  Probe   the lock-in ")["name"] == "Probe the lock-in"
    with pytest.raises(ValueError, match="give it a name"):
        lib.rename(mine["id"], "   ")
    with pytest.raises(KeyError):
        lib.rename("plan-nope", "x")
    assert lib.remove(mine["id"])["path"] == str(other.resolve())
    assert str(other.resolve()) not in lib.chosen()
    with pytest.raises(KeyError):
        lib.remove(mine["id"])

    import labharness.scanlibrary as scanlibrary   # beyond its size, Kelvin's own least used go; a person's stay
    monkeypatch.setattr(scanlibrary, "MAX_ENTRIES", 3)
    lib.add_script(other)
    for k in range(4):
        lib.note_plan("plan: p%d\n" % k, "p%d" % k, "", "r%d" % k, when=1000 + k)
    kept = {e["name"] for e in lib.entries()}
    assert "probe.py" in kept and len(kept) == 3 and "p3" in kept and "iv" not in kept

    assert params_from_changes(["VMAX = 2.0 (the script has 0.4)", "NAME = 'a b' (the script has 'x')",
                                "junk"]) == {"VMAX": 2.0, "NAME": "a b"}
    assert summary(yaml.safe_load(SWEEP), lambda key: {"smu.V": "V"}.get(key, "")) == \
        "smu.V 0 → 2 V, 5 points · lockin.X, smu.I"
    assert summary({"plan": "warm", "sweep": {"tc.setpoint": {"values": [2, 5]}, "smu.V": {"from": 0, "to": 1,
                                                                                         "points": 3, "back": True}},
                    "time": {"every": 2, "for": 4}, "measure": ["tc.T_sample"]}) == \
        "tc.setpoint 2 → 5 (2) × smu.V 0 → 1 → 0 (5) × every 2 s, 30 points · tc.T_sample"


def test_a_broken_library_is_kept_aside_and_a_run_still_runs(tmp_path):
    rt = virtual(tmp_path, [True, True])
    path = rt.data_dir / "library.json"
    path.write_text("{not json", encoding="utf-8")
    assert rt.library_view() == {"scripts": [], "scans": []}
    aside = list(rt.data_dir.glob("library.json.broken-*"))
    assert len(aside) == 1 and aside[0].read_text(encoding="utf-8") == "{not json"
    assert "done" in rt.run(SWEEP)
    assert [e["name"] for e in rt.library.entries()] == ["gate_sweep"]

    def broken(*args, **kwargs):
        raise OSError("the disk is full")
    rt.library.note_plan = broken   # noting fails: the run goes on all the same
    assert "done" in rt.run(SWEEP)
    assert rt.library.entries()[0]["count"] == 1


# ------------------------------------------------------------------ every run notes its entry

def test_every_run_of_a_plan_notes_it_once_in_the_library(tmp_path):
    rt = virtual(tmp_path, [True, True])
    out = rt.run(SWEEP)
    assert "done" in out, out
    (entry,) = rt.library.entries()
    run_id = entry["runs"][0]
    rec = rundata.load(rt._run_dir(run_id), with_rows=False)
    assert entry["kind"] == "plan" and entry["name"] == "gate_sweep" and entry["source"] == "kelvin"
    assert entry["text"] == rec.plan_text and entry["id"] == plan_id(rec.plan_text)
    assert entry["summary"] == "smu.V 0 → 2 V, 5 points · lockin.X, smu.I"
    assert rt.simulate(SWEEP).startswith("dry run")             # a rehearsal is no run
    rt.queue_add(SWEEP)
    assert rt.queue_start().startswith("queue started")         # a queue's scan is a run like any
    finish(rt)
    assert rt.queue.items[0].status == "done"
    (entry,) = rt.library.entries()
    assert entry["count"] == 2 and entry["runs"] == [rt.queue.items[0].run_id, run_id]
    view = rt.library_view()
    assert view["scripts"] == [] and view["scans"][0]["last_run"] == rt.queue.items[0].run_id
    imported = rt.import_data(str(write(tmp_path, "table.csv", "x,y\n1,2\n2,4\n")))
    assert "run" in imported and len(rt.library.entries()) == 1   # data from another program is no scan of Kelvin's


# ------------------------------------------------------------------ scripts a person imported may run

def test_an_imported_script_runs_from_outside_the_scripts_folder(tmp_path, scripts):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    probe = write(elsewhere, "probe.py", READS)
    stranger = write(elsewhere, "stranger.py", READS)
    with pytest.raises(scriptrun.ScriptError, match="outside the lab's scripts folder"):
        scriptrun.locate(str(probe), [scripts])
    assert scriptrun.locate(str(probe), [scripts], [probe]) == probe.resolve()
    assert scriptrun.locate("probe.py", [scripts], [probe]) == probe.resolve()   # by its name too
    assert scriptrun.locate(str(probe), [], [probe]) == probe.resolve()         # no scripts folder at all
    with pytest.raises(scriptrun.ScriptError, match="outside the lab's scripts folder"):
        scriptrun.locate(str(stranger), [scripts], [probe])

    rt = script_runtime(tmp_path, scripts, ScriptedApprover([True]))
    assert "outside the lab's scripts folder" in rt.run_script(str(probe))
    for bad, why in (("probe.py", "full path"), (str(elsewhere / "nope.py"), "does not exist"),
                     (str(elsewhere), "is a folder"),
                     (str(write(elsewhere, "notes.txt", "x")), "not a Python script"),
                     (str(write(elsewhere, "broken.py", "def (:\n")), "does not parse")):
        with pytest.raises(ValueError, match=why):
            rt.library_import(bad)
    got = rt.library_import(str(probe), by="human (portal)")
    entry = got["entry"]
    assert entry["source"] == "you" and entry["kind"] == "script" and entry["exists"] and entry["count"] == 0
    assert got["check"]["problems"] == [] and "probe.py in" in got["check"]["text"]
    assert "run_script asks a person once" in got["check"]["text"]
    ran = rt.run_script(str(probe))                              # the check and the approval as for any script
    assert "script probe.py done" in ran, ran
    assert rt.gate.approver.requests[-1].title == "Run script probe.py?"
    assert "outside the lab's scripts folder" in rt.run_script(str(stranger))   # never imported: still refused
    (mine,) = rt.library_view()["scripts"]
    assert mine["count"] == 1 and mine["last_run"] == run_id_of(ran) and mine["source"] == "you"
    rt.library_change("remove", mine["id"], by="human (portal)")
    assert "outside the lab's scripts folder" in rt.run_script(str(probe))     # taken out: refused again
    assert any(e.get("type") == "library" and e.get("change") == "imported" for e in rt.gate.log.events())


# ------------------------------------------------------------------ the queue: script items, then_pause, versions

def test_the_queue_runs_a_script_item_which_asks_for_itself(tmp_path, scripts):
    write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([True]))   # one approval: the script's own
    assert rt.queue_add("script: nope.py").startswith("not added: no script nope.py")
    assert rt.queue_add("script: sweep.py\nparams: {VMAX: ten}").startswith("not added: VMAX is a number")
    assert rt.queue_add("script: sweep.py\nwhat: 1").startswith("not added: unknown keys what")
    out = rt.queue_add("script: sweep.py\nparams: {VMAX: 0.6}\nnote: the usual check")
    assert out.startswith("added script sweep.py as #1"), out
    assert rt.queue.items[0].note == "the usual check"
    rehearsed = rt.queue_start(dry_run=True)
    assert "without its script (a script can't be rehearsed)" in rehearsed and rt.queue.state == "idle", rehearsed
    started = rt.queue_start()
    assert started.startswith("queue started: 1 script"), started   # nothing for the queue's own card
    finish(rt)
    (item,) = rt.queue.items
    assert item.status == "done", item.message
    (request,) = rt.gate.approver.requests
    assert request.title == "Run script sweep.py?" and "VMAX = 0.6 (the script has 0.4)" in request.items[0]
    meta = rundata.meta(rt._run_dir(item.run_id))
    assert meta["queue"]["position"] == 1 and meta["queue"]["note"] == "the usual check"
    (entry,) = rt.library_view()["scripts"]
    assert entry["params"] == {"VMAX": 0.6} and entry["last_run"] == item.run_id and not entry["changed"]
    assert entry["settable"] == {"VMAX": 0.4} and entry["notes"] == {"VMAX": "the top of the sweep, in volts"}


def test_the_queues_card_says_its_script_asks_for_itself_and_where_it_pauses(tmp_path, scripts):
    write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([False]))   # the person declines: nothing runs
    rt.queue_add("plan: gate\nsweep: {smu.V: {values: [0, 0.2]}}\nmeasure: [lockin.X]\nafter: {smu.V: 0}")
    rt.queue_add("script: sweep.py", then_pause=True)
    rt.queue_add("plan: later\nsweep: {smu.V: {values: [0, 5]}}\nmeasure: [lockin.X]")   # behind the pause
    assert rt.queue_start().startswith("declined: the queue did not start")
    (card,) = rt.gate.approver.requests
    assert card.title.startswith("Run the queue: 1 scan and 1 script"), card.title
    text = "\n".join(card.items)
    assert "#2 sweep.py: a script: it asks you itself when it starts" in text
    assert "The queue pauses after #2 sweep.py" in text and "up to there" in text
    assert "plan uses 0 … 200 mV" in text and "0 … 5 V" not in text   # the scan behind the pause isn't on it
    assert [i.status for i in rt.queue.items] == ["waiting"] * 3


def test_the_queue_pauses_after_an_item_marked_then_pause(tmp_path):
    rt = virtual(tmp_path, [])                                   # waits only: nothing asks
    rt.queue_add("step: first\nwait: 1", then_pause=True)
    rt.queue_add("step: second\nwait: 1")
    assert "the queue pauses after it" in rt.queue_show()
    assert rt.queue_start().startswith("queue started: 1 step")  # its approval covers up to the pause
    finish(rt)
    first, second = rt.queue.items
    assert first.status == "done" and second.status == "waiting"
    assert rt.queue.state == "paused" and rt.queue.reason == "paused after first, as asked"
    assert rt.queue_start().startswith("queue started: 1 step")
    finish(rt)
    assert second.status == "done" and rt.queue.state == "idle" and rt.queue.reason == "finished"
    rt.queue.set_then_pause(second.id, True)                    # the last one: nothing waits, the queue just ends
    rt.queue.again(second.id)
    rt.queue_start()
    finish(rt)
    assert rt.queue.state == "idle" and rt.queue.reason == "finished"


def test_a_version_runs_the_script_as_it_ran_then(tmp_path, scripts):
    script = write(scripts, "sweep.py", SCRIPT)
    write(scripts, "other.py", READS)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([True, True, True, True]))
    first = rt.run_script("sweep.py", {"VMAX": 0.6})
    assert "script sweep.py done" in first, first
    then = run_id_of(first)
    then_text = (rt._run_dir(then) / "script.py").read_text(encoding="utf-8")
    script.write_text(script.read_text(encoding="utf-8").replace("version one", "version two"), encoding="utf-8")
    assert rt.run_again_view(then)["script"]["changed"] is True
    assert "ran %s" % script.resolve() in rt.queue_add("script: other.py\nversion: %s" % then)   # not its run
    assert rt.queue_add("script: sweep.py\nversion: 20990101-000000-nope").startswith("not added: no run")
    rt.queue_add("script: sweep.py\nparams: {VMAX: 0.6}\nversion: %s" % then)
    rt.queue_add("script: sweep.py\nparams: {VMAX: 0.6}")
    assert rt.queue.items[0].name == "sweep.py as it ran in %s" % then
    assert rt.queue_start().startswith("queue started: 2 scripts")
    finish(rt)
    old, new = rt.queue.items
    assert old.status == "done" and new.status == "done", (old.message, new.message)
    old_dir, new_dir = rt._run_dir(old.run_id), rt._run_dir(new.run_id)
    assert "version one" in (old_dir / "output.txt").read_text(encoding="utf-8")
    assert "version two" in (new_dir / "output.txt").read_text(encoding="utf-8")
    assert (old_dir / "script.py").read_text(encoding="utf-8") == then_text   # exactly the text that ran then
    kept = yaml.safe_load(rundata.load(old_dir, with_rows=False).plan_text)
    was = yaml.safe_load(rundata.load(rt._run_dir(then), with_rows=False).plan_text)
    assert kept["version"] == then and kept["sha256"] == was["sha256"] and kept["params"] == {"VMAX": 0.6}
    assert "as it ran in run %s" % then in rt.gate.approver.requests[1].items[0]
    assert "as it ran in run" not in rt.gate.approver.requests[2].items[0]
    (entry,) = [e for e in rt.library_view()["scripts"] if e["name"] == "sweep.py"]
    assert entry["count"] == 3 and entry["changed"] is False     # its last run read the file as it is now

    script.unlink()   # the file is gone: its run's copy still runs, in its folder, under its name
    assert rt.queue_add("script: sweep.py").startswith("not added: no script sweep.py")
    with pytest.raises(scriptrun.ScriptError, match="does not exist"):   # only a version may name a file that is gone
        scriptrun.locate(str(script), [scripts])
    rt.queue_add("script: %s\nparams: {VMAX: 0.6}\nversion: %s" % (script, then))
    rt.queue_start()
    finish(rt)
    gone = rt.queue.items[-1]
    assert gone.status == "done", gone.message
    assert "version one" in (rt._run_dir(gone.run_id) / "output.txt").read_text(encoding="utf-8")


# ------------------------------------------------------------------ a run again, over HTTP

def portal(rt, tmp_path):
    from starlette.testclient import TestClient
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from test_web import Scripted
    folder, python = rt.scripts_folder, rt.scripts_python
    settings = Settings(tmp_path / "settings.json")
    guard = create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"],
                       providers={"scripted": Scripted([])})
    rt.scripts_folder, rt.scripts_python = folder, python    # the test's own, as Settings would have them
    client = TestClient(guard)
    client.headers["X-Kelvin-Token"] = settings.access_key
    return client, guard


def test_a_plan_runs_again_with_the_settings_it_started_with(tmp_path):
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from test_web import human
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    rt.auto_report = "off"
    client, _ = portal(rt, tmp_path)
    human(rt.approver, [True])
    assert "done" in rt.run(SWEEP)
    run_id = rt.runs_index(1)[0]["run_id"]
    rt.set("lockin", "frequency", 77.7)                        # settings changed since
    rt.set("smu", "nplc", 10)

    view = client.get("/api/runs/%s/again" % run_id).json()
    assert view["kind"] == "plan" and view["name"] == "gate_sweep" and view["read_now"] is True
    assert view["text"].startswith("plan: gate_sweep") and "script" not in view
    by_key = {c["key"]: c for c in view["conditions"]}
    assert by_key["lockin.frequency"] == {"key": "lockin.frequency", "then": 17.777, "now": 77.7, "unit": "Hz",
                                          "differs": True, "settable": True, "drive": False, "own": False}
    assert by_key["smu.nplc"]["differs"] and by_key["smu.output"]["drive"]
    assert by_key["smu.output"]["own"] and not by_key["smu.output"]["differs"]      # the plan sets it itself
    assert by_key["smu.V"]["own"] and not by_key["smu.V"]["differs"]                # and sweeps this
    assert not by_key["lockin.X"]["settable"] and not by_key["tc.setpoint"]["differs"]
    assert view["differ"] == sum(1 for c in view["conditions"] if c["settable"] and c["differs"]) >= 2

    queued = client.post("/api/runs/%s/again" % run_id, json={"restore": True, "queue": True})
    assert queued.status_code == 200, queued.text
    body = queued.json()
    item = body["queue"]["items"][-1]
    assert item["kind"] == "plan" and item["status"] == "waiting" and not item["then_pause"]
    assert body["queue"]["state"] == "idle"                    # queue: true adds it, nothing starts
    setup = yaml.safe_load(item["text"])["setup"]
    assert list(setup.items()) == [("smu.nplc", 1.0), ("lockin.frequency", 17.777), ("lockin.amplitude", 0.05),
                                   ("smu.output", True)]     # restored first; the plan's own setup wins
    rt.queue_change("remove", item["id"])

    human(rt.approver, [True])                                 # run now: the queue's approval, as ever
    now = client.post("/api/runs/%s/again" % run_id, json={"restore": True})
    assert now.status_code == 200, now.text
    assert "queue started: 1 scan" in now.json()["message"]
    finish(rt)
    (item,) = rt.queue.items
    assert item.status == "done", item.message
    assert item.then_pause and rt.queue.state == "idle"         # nothing else waited: it just ends
    assert lab.read("lockin", "frequency") == 17.777 and lab.read("smu", "nplc") == 1.0
    again = client.get("/api/runs/%s/again" % item.run_id).json()            # the run made so: nothing to put back
    assert again["differ"] == 0, [c for c in again["conditions"] if c["differs"]]
    assert {c["key"] for c in again["conditions"] if c["own"]} >= {"lockin.frequency", "smu.nplc", "smu.output"}
    assert client.get("/api/runs/nope/again").status_code == 404


def test_a_script_runs_again_as_it_ran(tmp_path, scripts):
    from labharness.agent.approval import WebApprover
    from test_web import human
    script = write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, WebApprover(timeout=5))
    client, _ = portal(rt, tmp_path)
    human(rt.approver, [True])
    ran = rt.run_script("sweep.py", {"VMAX": 0.6})
    run_id = run_id_of(ran)
    rt.set("lockin", "tc", 0.3)
    view = client.get("/api/runs/%s/again" % run_id).json()
    assert view["kind"] == "script" and view["name"] == "sweep.py" and "text" not in view
    s = view["script"]
    assert s["path"] == str(script.resolve()) and s["exists"] and not s["changed"]
    assert s["params"] == {"VMAX": 0.6} and s["args"] == [] and s["python"].endswith("Python %d.%d" % sys.version_info[:2])
    assert len(s["sha256"]) == 64 and {c["key"]: c for c in view["conditions"]}["lockin.tc"]["differs"]

    body = client.post("/api/runs/%s/again" % run_id, json={"restore": True, "queue": True}).json()
    step, item = body["queue"]["items"][-2:]
    assert step["kind"] == "step" and yaml.safe_load(step["text"])["set"] == {"lockin.tc": 0.1}
    assert item["kind"] == "script" and yaml.safe_load(item["text"]) == {
        "script": str(script.resolve()), "params": {"VMAX": 0.6}, "python": s["python"]}   # no version: unchanged

    script.write_text(script.read_text(encoding="utf-8").replace("version one", "version two"), encoding="utf-8")
    assert client.get("/api/runs/%s/again" % run_id).json()["script"]["changed"] is True
    then = client.post("/api/runs/%s/again" % run_id, json={"queue": True}).json()["queue"]["items"][-1]
    assert yaml.safe_load(then["text"])["version"] == run_id
    now = client.post("/api/runs/%s/again" % run_id, json={"version": "now", "queue": True}).json()
    assert "version" not in yaml.safe_load(now["queue"]["items"][-1]["text"])
    bad = client.post("/api/runs/%s/again" % run_id, json={"version": "later", "queue": True})
    assert bad.status_code == 400 and "version is then" in bad.json()["error"]
    original = script.read_text(encoding="utf-8")
    script.unlink()                                             # gone: only as it ran then
    assert client.get("/api/runs/%s/again" % run_id).json()["script"]["exists"] is False
    gone = client.post("/api/runs/%s/again" % run_id, json={"queue": True}).json()["queue"]["items"][-1]
    assert yaml.safe_load(gone["text"])["version"] == run_id
    refused = client.post("/api/runs/%s/again" % run_id, json={"version": "now", "queue": True})
    assert refused.status_code == 400 and "only as it ran then" in refused.json()["error"]
    script.write_text(original, encoding="utf-8")

    # the script's own record of what ran, read as text, never opened with a program
    files = client.get("/api/runs/" + run_id).json()["files"]
    assert {f["name"] for f in files if f.get("view")} == {"script.py", "output.txt", "commands.jsonl"}
    got = client.get("/api/runs/%s/files/script.py" % run_id)
    assert got.status_code == 200 and got.headers["content-type"].startswith("text/plain")
    assert got.headers["x-content-type-options"] == "nosniff" and "sandbox" in got.headers["content-security-policy"]
    assert got.text.replace("\r\n", "\n") == (rt._run_dir(run_id) / "script.py").read_text(encoding="utf-8")
    assert "version one" in client.get("/api/runs/%s/files/output.txt" % run_id).text
    assert client.post("/api/runs/%s/files/script.py/open" % run_id, json={}).status_code == 404
    with pytest.raises(KeyError, match="not opened"):
        rt.run_file(run_id, "commands.jsonl", opening=True)


def test_the_library_routes(tmp_path, scripts):
    from labharness.agent.approval import WebApprover
    write(scripts, "sweep.py", SCRIPT)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    probe = write(elsewhere, "probe.py", READS)
    rt = script_runtime(tmp_path, scripts, WebApprover(timeout=5))
    client, _ = portal(rt, tmp_path)
    assert client.get("/api/library").json() == {"scripts": [], "scans": []}
    bad = client.post("/api/library", json={"op": "import", "path": str(elsewhere / "nope.py")})
    assert bad.status_code == 400 and "does not exist" in bad.json()["error"]
    got = client.post("/api/library", json={"op": "import", "path": str(probe)}).json()
    assert got["entry"]["source"] == "you" and got["check"]["problems"] == []
    assert [e["name"] for e in got["library"]["scripts"]] == ["probe.py"]
    entry_id = got["entry"]["id"]
    renamed = client.post("/api/library", json={"op": "rename", "id": entry_id, "name": "X probe"}).json()
    assert set(renamed) == {"library"} and renamed["library"]["scripts"][0]["name"] == "X probe"
    queued = client.post("/api/library", json={"op": "run", "id": entry_id, "params": {}, "queue": True})
    assert queued.status_code == 200 and "at the end of the queue" in queued.json()["message"]
    assert yaml.safe_load(queued.json()["queue"]["items"][-1]["text"]) == {"script": str(probe.resolve())}
    item_id = queued.json()["queue"]["items"][-1]["id"]
    marked = client.post("/api/queue", json={"op": "then_pause", "item": item_id, "value": True}).json()
    assert marked["items"][-1]["then_pause"] is True and "pauses after probe.py" in marked["message"]
    assert client.post("/api/queue", json={"op": "then_pause", "item": item_id, "value": 0}).json()["items"][-1][
        "then_pause"] is False
    added = client.post("/api/queue", json={"op": "add", "text": "step: settle\nwait: 5", "then_pause": True}).json()
    assert added["items"][-1]["then_pause"] is True
    assert client.post("/api/library", json={"op": "run", "id": "plan-nope"}).status_code == 404
    assert client.post("/api/library", json={"op": "remove", "id": entry_id}).json()["library"]["scripts"] == []
    assert client.post("/api/library", json={"op": "fly"}).status_code == 400


# ------------------------------------------------------------------ never on phones, never for the AI

def test_phones_get_none_of_it(tmp_path):
    from starlette.testclient import TestClient
    from labharness.web.relay import RelayLink
    from labharness.web.remote import allowed
    for role in ("watch", "control"):
        for method, path in (("GET", "/api/library"), ("POST", "/api/library"), ("GET", "/api/runs/r1/again"),
                             ("POST", "/api/runs/r1/again")):
            assert not allowed(role, method, path), (role, method, path)
    rt = virtual(tmp_path, [True])
    assert "done" in rt.run(SWEEP)
    run_id = rt.runs_index(1)[0]["run_id"]
    (rt._run_dir(run_id) / "output.txt").write_text("hello", encoding="utf-8")   # as a script's run keeps it
    client, guard = portal(rt, tmp_path)
    assert any(f.get("view") for f in client.get("/api/runs/" + run_id).json()["files"])
    with TestClient(guard):
        guard.remote.port = 8771                                # as if the network listener were running
        phone = TestClient(guard, base_url="http://192.168.1.50:8771")
        _, token = guard.remote.issue("Ana's phone", "control", "ios-ana")
        ctl = {"X-Kelvin-Token": token}
        for method, path in (("GET", "/api/library"), ("POST", "/api/library"),
                             ("GET", "/api/runs/%s/again" % run_id), ("POST", "/api/runs/%s/again" % run_id)):
            r = phone.request(method, path, headers=ctl, json={} if method == "POST" else None)
            assert r.status_code == 403, (method, path, r.status_code)
        assert phone.get("/api/runs/%s/files/output.txt" % run_id, headers=ctl).status_code == 403
        assert not any(f.get("view") for f in phone.get("/api/runs/" + run_id, headers=ctl).json()["files"])

    sent = []

    async def send(message):
        sent.append(message)

    async def through_the_hub():
        link = RelayLink(guard.app, lambda: None, lambda: {})
        await link._answer(send, {"id": 1, "method": "GET", "path": "/api/library"}, asyncio.Semaphore(1))
        await link._serve(send, 2, "POST", "/api/runs/%s/again" % run_id, "", b"{}", {"id": "p1", "name": "P"})
    asyncio.run(through_the_hub())
    heads = [json.loads(m) for m in sent if isinstance(m, str) and json.loads(m).get("t") == "head"]
    assert [h["status"] for h in heads] == [403, 403]           # the hub's rule, and the route's own


def test_the_ai_never_rewrites_what_a_run_ran(tmp_path):
    from labharness import folderfiles
    run = tmp_path / "chat" / "20261031-101500-sweep.py"
    run.mkdir(parents=True)
    (run / "record.bin").write_bytes(b"")
    for name in ("script.py", "output.txt", "commands.jsonl"):
        assert "only Kelvin itself writes" in folderfiles.off_limits(run / name, saving=True)
        assert folderfiles.off_limits(run / name) is None      # reading is fine
    assert folderfiles.off_limits(tmp_path / "chat" / "script.py", saving=True) is None   # not in a run
    assert folderfiles.off_limits(run / "notes.txt", saving=True) is None


def test_queue_items_keep_then_pause_and_say_script_items(tmp_path):
    from labharness.scanqueue import ScanQueue, load_script_item, parse_item
    from labharness.plans import PlanError
    assert parse_item("script: C:/lab/sweep.py\nparams: {VMAX: 2}") == ("script", "sweep.py")
    assert parse_item("script: sweep.py\nversion: 20261031-101500-sweep.py") == \
        ("script", "sweep.py as it ran in 20261031-101500-sweep.py")
    item = load_script_item("script: sweep.py\nargs: [--fast, 3]\npython: C:/py/python.exe")
    assert item.args == ["--fast", "3"] and item.python == "C:/py/python.exe" and item.params == {}
    for text, why in (("script: ''", "script: its file name"), ("script: a.py\nversion: last", "version:"),
                      ("script: a.py\nargs: --fast", "args:"), ("script: a.py\nparams: [1]", "params:")):
        with pytest.raises(PlanError, match=why):
            load_script_item(text)
    q = ScanQueue(tmp_path / "queue.json")
    a = q.add("script: sweep.py", then_pause=True)
    assert ScanQueue(tmp_path / "queue.json").find(a.id).then_pause is True   # kept across a restart
    assert q.set_then_pause(a.id, False).then_pause is False
    assert "1. [waiting] script sweep.py" in q.text()
    assert q.text(lambda i: True).startswith("queue: idle\n1. [waiting] a script of unshared work")


def test_what_a_script_set_itself_is_never_put_back(tmp_path, scripts):
    write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([True, True, True]))
    rt.set("smu", "output", True)                              # on as it starts; the script turns it off at its end
    ran = rt.run_script("sweep.py")
    assert "script sweep.py done" in ran, ran
    run_id = run_id_of(ran)
    assert {"smu.output", "smu.V"} <= set(rundata.meta(rt._run_dir(run_id))["sets"])
    view = rt.run_again_view(run_id)
    by_key = {c["key"]: c for c in view["conditions"]}
    assert by_key["smu.output"]["own"] and not by_key["smu.output"]["differs"]   # the script switches it itself
    assert view["differ"] == 0
