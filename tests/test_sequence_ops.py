"""The queue as the portal's Sequence: its name, kept across a restart; skip (a waiting item, and the running one,
which ends while the queue goes on); duplicate; a script's or a step's values changed in place (params); a ladder of
copies, one per value; what each block shows (summary, settings, progress while it runs), worked out without reading
a file at every poll; and the library's functions (meta, the scripts folder's files, a script read but never run)."""
import re
import time

import pytest
import yaml

pytest.importorskip("mcp")

from simlab import build_virtual_lab  # noqa: E402
from labharness import ScriptedApprover, scriptrun  # noqa: E402
from labharness.clock import RealClock  # noqa: E402
from labharness.scanqueue import ScanQueue, item_with, saved_name  # noqa: E402

from test_scan_library import READS, SCRIPT, finish, portal, script_runtime, scripts, virtual, write  # noqa: E402,F401

MINUTE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

SWEEP = """plan: gate_sweep
setup: { lockin.amplitude: 0.05, smu.output: on }
sweep: { smu.V: {from: 0, to: 2, step: 0.5} }
settle: 0.1
measure: [lockin.X, smu.I]
after: { smu.V: 0 }"""

SLOW = """plan: slow_sweep
setup: { smu.output: on }
sweep: { smu.V: {from: 0, to: 1.9, step: 0.1} }
settle: 0.3
measure: [smu.I]
after: { smu.V: 0 }"""   # 20 points, 0.3 s each on the real clock

WAIT = "step: wait 10 min\nwait: 600"


def real(tmp_path, answers):
    """A runtime whose lab keeps the computer's time: a wait or a settle takes as long as it says."""
    from labharness.agent.runtime import LabRuntime
    rt = LabRuntime(build_virtual_lab(clock=RealClock(), seed=1), ScriptedApprover(list(answers)), tmp_path / "data")
    rt.auto_report = "off"
    return rt


def until(check, seconds=20.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        got = check()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError("not within %s s" % seconds)


def item_view(rt, item_id):
    return next(i for i in rt.queue_view()["items"] if i["id"] == item_id)


# ------------------------------------------------------------------ the sequence's name

def test_the_sequence_is_named_and_keeps_its_name(tmp_path):
    path = tmp_path / "queue.json"
    q = ScanQueue(path)
    assert q.name == "" and q.view()["name"] == ""
    a = q.add(WAIT)
    assert MINUTE.match(q.name)                               # its first item went in: the minute, as labMaster's
    first = q.name
    q.add(WAIT)
    assert q.name == first                                    # more items: the same sequence
    assert q.rename("  Night   run ") == "Night run" and q.view()["name"] == "Night run"
    assert ScanQueue(path).name == "Night run"                # kept across a restart
    assert "\nname: Night run\n" in q.text() and "name:" not in q.text(lambda i: True)   # only when all is seen
    with pytest.raises(ValueError, match="at most 80"):
        q.rename("x" * 81)
    assert MINUTE.match(q.rename(""))                         # no name: the minute, as a new one's

    for i in list(q.items):
        q.remove(i.id)
    q.rename("cool-down")                                     # named while empty: its first item keeps the name
    q.add(WAIT)
    assert q.name == "cool-down" and ScanQueue(path).name == "cool-down"
    q.remove(q.items[0].id)
    q.add(WAIT)
    assert MINUTE.match(q.name)                               # emptied, then a new first item: a new sequence

    old = tmp_path / "old.json"                               # kept before a sequence had a name
    old.write_text('{"items": [{"id": "a1", "kind": "step", "name": "w", "text": "step: w\\nwait: 1\\n", '
                   '"added": 1767225600.0}]}', encoding="utf-8")
    assert ScanQueue(old).name == time.strftime("%Y-%m-%d %H:%M", time.localtime(1767225600.0))
    assert a.id != q.items[0].id


def test_save_and_load_use_the_sequences_name(tmp_path):
    rt = virtual(tmp_path, [])
    rt.queue_add(WAIT, note="first")
    assert rt.queue_rename("Cool down: 2 K", by="human") == "renamed Cool down: 2 K"
    assert saved_name("Cool down: 2 K") == "Cool down. 2 K" and saved_name("夜间") .startswith("20")
    assert rt.queue_save() == "saved the queue as Cool down. 2 K.yaml"   # no name: the sequence's, made to fit
    assert "Cool down. 2 K" in rt.queue_view()["saved"]
    rt.queue_change("remove", "1")
    assert rt.queue.items == []
    assert "added 1 items from Cool down. 2 K" in rt.queue_load("Cool down. 2 K")
    assert rt.queue.name == "Cool down. 2 K"                  # loaded into an empty queue: its name too
    rt.queue_rename("mine")
    rt.queue_load("Cool down. 2 K")
    assert rt.queue.name == "mine" and len(rt.queue.items) == 2   # appended to one with items: the name stays
    assert any(e.get("type") == "queue_rename" for e in rt.gate.log.events())


# ------------------------------------------------------------------ skip

def test_skipping_the_running_item_goes_on_to_the_next(tmp_path):
    rt = real(tmp_path, [True])                               # the queue's one approval
    rt.queue_add(SLOW)
    rt.queue_add("step: after it\nwait: 0.2")
    rt.queue_add("step: never\nwait: 0.2")
    scan, after, never = rt.queue.items
    assert rt.queue_skip(never.id, "human") == "skipped never"   # a waiting item: it won't run
    assert never.status == "skipped" and never.message == "skipped by human"
    assert rt.queue.on_error == "pause"
    assert rt.queue_start("human").startswith("queue started")
    progress = until(lambda: (item_view(rt, scan.id).get("progress") or {}).get("done", 0) >= 1 and
                     item_view(rt, scan.id)["progress"])
    assert progress["total"] == 20 and progress["phase"] == "measuring" and progress["run_id"]
    assert progress["eta_s"] is None or progress["eta_s"] > 0
    assert rt.queue_skip(scan.id, "human") == "skipping slow_sweep: it ends now, and the queue goes on"
    finish(rt, 30)
    assert scan.status == "skipped" and scan.message == "skipped by human", (scan.status, scan.message)
    assert scan.run_id == progress["run_id"]                  # its run keeps the points it took
    assert after.status == "done" and never.status == "skipped"
    assert rt.queue.state == "idle" and rt.queue.reason == "finished"   # no pause, though on a failure: pause
    assert "progress" not in item_view(rt, scan.id)
    assert rt.queue_skip(after.id) == "not changed: after it has finished (done)"
    assert rt.queue_skip("nope").startswith("not changed: no queue item")
    assert rt.queue.again(never.id).status == "waiting"       # a skipped item goes back in line
    logged = [e for e in rt.gate.log.events() if e.get("type") == "queue_skip"]
    assert [bool(e.get("running")) for e in logged] == [False, True]


def test_a_skip_meant_for_an_item_that_finished_never_ends_the_next(tmp_path):
    rt = real(tmp_path, [])                                   # waits only: nothing asks
    rt.queue_add("step: first\nwait: 30")
    rt.queue_add("step: second\nwait: 30")
    first, second = rt.queue.items
    assert rt.queue_start("human").startswith("queue started: 2 steps")
    progress = until(lambda: item_view(rt, first.id).get("progress"))
    assert progress["phase"] == "wait" and 25 < progress["until"] - time.time() <= 30.5   # its block counts down
    rt.queue_skip(first.id, "human")
    until(lambda: second.status == "running")
    stops = rt.gate.stop_generation
    assert rt.queue_skip(first.id, "human") == "not changed: first has finished (skipped)"   # a click from before
    time.sleep(0.4)
    assert second.status == "running" and rt.gate.stop_generation == stops   # the next one goes on
    assert item_view(rt, second.id)["progress"]["phase"] == "wait"
    rt.queue_skip(second.id, "human")
    finish(rt, 10)
    assert [i.status for i in rt.queue.items] == ["skipped", "skipped"] and rt.queue.state == "idle"


def test_skipping_a_script_declines_its_own_card(tmp_path, scripts):
    from labharness.agent.approval import WebApprover
    write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, WebApprover(timeout=20))
    rt.queue_add("script: sweep.py\nparams: {VMAX: 0.6}")
    rt.queue_add("step: then\nwait: 0.1")
    script, step = rt.queue.items
    assert rt.queue_start("human").startswith("queue started")   # a script asks for itself as it starts
    card = rt.approver.wait_for_pending(timeout=20)
    assert card and card["title"] == "Run script sweep.py?"
    assert script.status == "running"
    t0 = time.time()
    rt.queue_skip(script.id, "human")
    finish(rt, 30)
    assert time.time() - t0 < 10                              # declined at the skip, not when the card ran out
    assert script.status == "skipped" and step.status == "done" and rt.queue.state == "idle"
    assert rt.approver.pending_list() == []
    (card,) = rt.approver.history_list()
    assert card["answer"] == "declined" and card["by"] == "a skip by human"


# ------------------------------------------------------------------ duplicate

def test_duplicate_puts_a_copy_right_after_it_filed_as_it_is(tmp_path):
    rt = virtual(tmp_path, [])
    with rt.attributed({"project": "p1", "chat": "c1", "unshared": True, "folder": str(tmp_path)}):
        rt.queue_add(SWEEP, note="the usual", then_pause=True)
    rt.queue_add(WAIT)
    first, wait = rt.queue.items
    assert rt.queue_duplicate(first.id, "human") == "duplicated gate_sweep as #2"
    copy = rt.queue.items[1]
    assert copy.id != first.id and copy.text == first.text and copy.note == "the usual" and copy.status == "waiting"
    assert (copy.project, copy.chat, copy.unshared, copy.folder) == ("p1", "c1", True, str(tmp_path))   # its work
    assert copy.then_pause is False and copy.by == "human"
    assert rt.queue_change("duplicate", wait.id, by="agent").startswith("duplicated wait 10 min as #4")
    assert [i.name for i in rt.queue.items] == ["gate_sweep", "gate_sweep", "wait 10 min", "wait 10 min"]
    rt.queue.mark(first, "running")
    assert rt.queue_duplicate(first.id).startswith("duplicated")   # the running one too: the copy waits
    assert rt.queue.items[1].status == "waiting"
    assert rt.queue_duplicate("nope").startswith("not changed: no queue item")


# ------------------------------------------------------------------ params

def test_a_scripts_settings_change_in_place(tmp_path, scripts):
    write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([]))
    rt.queue_add("script: sweep.py\nparams: {VMAX: 0.6}")
    item = rt.queue.items[0]
    shown = item_view(rt, item.id)
    assert shown["settable"] == {"VMAX": 0.4} and shown["notes"] == {"VMAX": "the top of the sweep, in volts"}
    assert shown["params"] == {"VMAX": 0.6} and shown["summary"] == "VMAX = 0.6" and "problem" not in shown

    assert rt.queue_params(item.id, {"VMAX": "0.5"}, by="human") == "changed sweep.py"
    assert yaml.safe_load(item.text) == {"script": "sweep.py", "params": {"VMAX": 0.5}}   # its own type: a number
    assert item_view(rt, item.id)["params"] == {"VMAX": 0.5}
    for bad, why in (({"VMAX": "ten"}, "VMAX is a number"), ({"STEPS": 3}, "no setting STEPS"),
                     ([1, 2], "a mapping")):
        out = rt.queue_params(item.id, bad)
        assert out.startswith("not changed") and why in out, out
    assert yaml.safe_load(item.text)["params"] == {"VMAX": 0.5}   # a refused change changes nothing
    assert rt.queue_params(item.id, {"VMAX": None}) == "changed sweep.py"   # None: the file's own value
    assert yaml.safe_load(item.text) == {"script": "sweep.py"} and item_view(rt, item.id)["params"] == {}

    rt.queue.mark(item, "running")
    assert "is running" in rt.queue_params(item.id, {"VMAX": 0.3})
    rt.queue.mark(item, "done")
    assert rt.queue_params(item.id, {"VMAX": 0.3}) == "changed sweep.py"
    assert item.status == "waiting"                           # as an edit: a finished item is back in line
    rt.queue_add(SWEEP)
    assert "is a scan" in rt.queue_params("2", {"x": 1})


def test_a_steps_wait_and_set_values_change_in_place(tmp_path):
    rt = virtual(tmp_path, [])
    rt.queue_add(WAIT)
    step = rt.queue.items[0]
    shown = item_view(rt, step.id)
    assert shown["wait"] == 600 and shown["set"] == {} and shown["summary"] == ""   # its name says it all
    assert rt.queue_params(step.id, wait=300) == "changed wait 5 min"   # the Wait block's name says the new time
    assert yaml.safe_load(step.text) == {"step": "wait 5 min", "wait": 300.0}
    assert rt.queue_params(step.id, sets={"smu.V": 0.5}) == "changed wait 5 min"
    shown = item_view(rt, step.id)
    assert shown["set"] == {"smu.V": 0.5} and shown["summary"] == "smu.V → 500 mV · wait 5 min"
    assert "outside the limits" in rt.queue_params(step.id, sets={"smu.V": 1000})
    assert rt.queue_params(step.id, wait="soon").startswith("not changed")
    assert rt.queue_params(step.id).startswith("not changed: give a step's wait or set")
    assert rt.queue_params(step.id, sets={"smu.V": None}) == "changed wait 5 min"
    assert item_view(rt, step.id)["set"] == {}
    assert rt.queue_params(step.id, wait=0).startswith("not changed")   # a step that does nothing is no step


# ------------------------------------------------------------------ ladder

def test_a_ladder_adds_one_copy_of_the_items_per_value(tmp_path, scripts):
    write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([]))
    with rt.attributed({"project": "p1", "unshared": True}):
        rt.queue_add("script: sweep.py\nparams: {VMAX: 0.6}")
    rt.queue_add("step: settle\nwait: 60")
    script, step = rt.queue.items
    out = rt.queue_ladder([step.id, script.id], "VMAX", [0.1, 0.2, 0.3], by="human")
    assert out == "added 6 items: VMAX = 0.1, 0.2, 0.3", out
    added = rt.queue.items[2:]
    assert [i.kind for i in added] == ["script", "step"] * 3    # in the queue's order, value after value
    assert [yaml.safe_load(i.text)["params"]["VMAX"] for i in added[::2]] == [0.1, 0.2, 0.3]
    assert all(i.text == step.text for i in added[1::2])         # a wait between them goes along as it is
    assert all(i.project == "p1" and i.unshared for i in added[::2])   # each copy filed as its item is
    assert [item_view(rt, i.id)["summary"] for i in added[::2]] == ["VMAX = 0.1", "VMAX = 0.2", "VMAX = 0.3"]

    n = len(rt.queue.items)
    assert rt.queue_ladder([step.id], "wait", [60, 120]) == "added 2 items: wait = 60, 120"
    assert [yaml.safe_load(i.text) for i in rt.queue.items[n:]] == [{"step": "settle", "wait": 60.0},
                                                                    {"step": "settle", "wait": 120.0}]
    n = len(rt.queue.items)
    assert rt.queue_ladder([step.id], "smu.V", [0.1, 0.2]).startswith("added 2 items")
    assert [yaml.safe_load(i.text)["set"] for i in rt.queue.items[n:]] == [{"smu.V": 0.1}, {"smu.V": 0.2}]

    rt.queue_add(SWEEP)
    scan = rt.queue.items[-1]
    n = len(rt.queue.items)
    assert rt.queue_ladder([scan.id], "lockin.amplitude", [0.01, 0.02]).startswith("added 2 items")
    assert [yaml.safe_load(i.text)["setup"]["lockin.amplitude"] for i in rt.queue.items[n:]] == [0.01, 0.02]
    n = len(rt.queue.items)
    for refs, key, values, why in (([scan.id], "smu.nplc", [1, 2], "no item takes smu.nplc (a scan takes only one "
                                                                   "of its setup values: gate_sweep)"),
                                   ([script.id], "VMAX", list(range(401)), "at most 400 items at once"),
                                   ([script.id], "VMAX", [], "give the values"),
                                   ([script.id], "", [1], "name the setting"),
                                   ([], "VMAX", [1], "tick the items"),
                                   (["nope"], "VMAX", [1], "no queue item"),
                                   ([script.id], "VMAX", [0.2, "ten"], "VMAX is a number"),
                                   ([step.id], "smu.V", [0.1, 1000], "outside the limits")):
        out = rt.queue_ladder(refs, key, values)
        assert out.startswith("not added") and why in out, (key, out)
    assert len(rt.queue.items) == n                            # refused: none of it added
    assert rt.queue_ladder([scan.id, step.id], "smu.V", [0.3]).startswith("added 2 items")   # the scan goes along


def test_a_ladder_takes_the_labs_own_names_as_the_queue_does(tmp_path):
    """A lab's own name for a setting (names:, a Special Measure channel's) stands for its instrument.parameter in a
    step's set values and a scan's setup, as queue_add takes one: the copies keep the instruments' own names."""
    rt = virtual(tmp_path, [])
    rt.lab.config.names.update(Vsd="smu.V", amp="lockin.amplitude")
    rt.queue_add("step: settle\nwait: 60")
    rt.queue_add(SWEEP)
    step, scan = rt.queue.items
    n = len(rt.queue.items)
    assert rt.queue_ladder([step.id], "Vsd", [0.1, 0.2]) == "added 2 items: Vsd = 0.1, 0.2"
    assert [yaml.safe_load(i.text)["set"] for i in rt.queue.items[n:]] == [{"smu.V": 0.1}, {"smu.V": 0.2}]
    n = len(rt.queue.items)
    assert rt.queue_ladder([scan.id], "amp", [0.01, 0.02]).startswith("added 2 items")
    setups = [yaml.safe_load(i.text)["setup"] for i in rt.queue.items[n:]]
    assert [s["lockin.amplitude"] for s in setups] == [0.01, 0.02] and not any("amp" in s for s in setups)
    assert "outside the limits" in rt.queue_ladder([step.id], "Vsd", [1000])   # checked as the setting it names


# ------------------------------------------------------------------ what the blocks show, and what it costs

def test_the_view_reads_a_scripts_file_only_when_it_changes(tmp_path, scripts, monkeypatch):
    script = write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([]))
    for _ in range(3):
        rt.queue_add("script: sweep.py")
    reads, finds = [], []
    real_source, real_locate = scriptrun.source_of, scriptrun.locate
    monkeypatch.setattr(scriptrun, "source_of", lambda p: reads.append(p) or real_source(p))
    monkeypatch.setattr(scriptrun, "locate", lambda *a, **k: finds.append(a) or real_locate(*a, **k))
    for _ in range(5):                                         # /api/state polls
        view = rt.queue_view()
    assert [i["settable"] for i in view["items"]] == [{"VMAX": 0.4}] * 3
    assert len(reads) == 1 and len(finds) == 1                 # one file: read once, found once
    script.write_text(SCRIPT.replace("VMAX = 0.4", "VMAX = 0.75"), encoding="utf-8")
    assert rt.queue_view()["items"][0]["settable"] == {"VMAX": 0.75} and len(reads) == 2
    rt.queue_view()
    assert len(reads) == 2
    script.unlink()
    assert rt.queue_view()["items"][0]["problem"] == "sweep.py is not there"
    plan = rt.queue_add(SWEEP)
    assert plan.startswith("added")
    assert rt.queue_view()["items"][-1]["summary"] == "smu.V 0 → 2 V, 5 points · lockin.X, smu.I"
    assert rt.queue_view()["name"] and isinstance(rt.queue_view()["saved"], list)


# ------------------------------------------------------------------ the library's functions

NOTEBOOK = ('{"cells": [{"cell_type": "code", "source": ["POWER = 3  # mW\\n", "print(POWER)\\n"], "metadata": {}, '
            '"outputs": [], "execution_count": null}], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}')


def test_the_library_lists_reads_and_describes_functions(tmp_path, scripts):
    write(scripts, "sweep.py", SCRIPT)
    (scripts / "sub").mkdir()
    write(scripts / "sub", "probe.py", READS)
    write(scripts, "notes.ipynb", NOTEBOOK)
    write(scripts, "readme.txt", "x")
    marker = tmp_path / "ran.txt"
    write(scripts, "trap.py", "open(%r, 'w').write('ran')\nLEVEL = 2\n" % str(marker))
    for hidden in (".venv/lib", "__pycache__", "node_modules/x", ".hidden", "env", "site-packages"):
        (scripts / hidden).mkdir(parents=True, exist_ok=True)
        write(scripts / hidden, "nope.py", "x = 1\n")
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([]))
    files = rt.library_files()
    assert files["folder"] == str(scripts) and files["more"] is False
    assert [f["rel"] for f in files["files"]] == ["notes.ipynb", "sub/probe.py", "sweep.py", "trap.py"]
    assert [f["kind"] for f in files["files"]] == ["ipynb", "py", "py", "py"]
    assert files["files"][1]["path"] == str(scripts / "sub" / "probe.py") and files["files"][1]["name"] == "probe.py"

    got = rt.library_inspect(str(scripts / "sweep.py"))
    assert got == {"name": "sweep.py", "path": str(scripts / "sweep.py"), "settable": {"VMAX": 0.4},
                   "notes": {"VMAX": "the top of the sweep, in volts"}, "problem": None}
    assert rt.library_inspect("notes.ipynb")["settable"] == {"POWER": 3}   # in the scripts folder; a notebook too
    assert rt.library_inspect(str(scripts / "trap.py"))["settable"] == {"LEVEL": 2} and not marker.exists()  # read only
    assert "does not exist" in rt.library_inspect(str(scripts / "gone.py"))["problem"]
    assert "not a Python script" in rt.library_inspect(str(scripts / "readme.txt"))["problem"]
    assert "is a folder" in rt.library_inspect(str(scripts / "sub"))["problem"]
    with pytest.raises(ValueError, match="give the script's path"):
        rt.library_inspect("  ")

    entry = rt.library_import(str(scripts / "sweep.py"))["entry"]
    assert entry["color"] == "" and entry["note"] == ""
    shown = rt.library_meta(entry["id"], name="Gate sweep", color="#2F6FB0", note="the usual  check")
    assert (shown["name"], shown["color"], shown["note"]) == ("Gate sweep", "#2f6fb0", "the usual check")
    (listed,) = rt.library_view()["scripts"]
    assert listed["color"] == "#2f6fb0" and listed["named"] is True and listed["settable"] == {"VMAX": 0.4}
    with pytest.raises(ValueError, match="#rrggbb"):
        rt.library_meta(entry["id"], color="blue")
    with pytest.raises(KeyError):
        rt.library_meta("script-nope", color="")
    assert rt.library_view()["scripts"][0]["color"] == "#2f6fb0"   # a refused change changes nothing

    rt.library_run(entry["id"], at_end=True)                   # its item knows its entry
    rt.queue_add("script: sweep.py")                           # and one queued by its file alone
    a, b = rt.queue_view()["items"]
    assert a["library"] == entry["id"] and a["color"] == b["color"] == "#2f6fb0"
    assert a["name"] == b["name"] == "Gate sweep"              # the function's name, on its blocks
    assert rt.queue.items[1].name == "sweep.py"                # the queue's own words stay the file's
    rt.library_meta(entry["id"], color="", note="")
    assert "color" not in rt.queue_view()["items"][0] and rt.library_view()["scripts"][0]["note"] == ""
    scan, _ = rt.library.add_plan(SWEEP + "\n", "Gate", "")     # a scan of the library, dragged in as its text
    rt.library_meta(scan["id"], color="#123456")
    assert rt.queue_add(SWEEP).startswith("added")
    assert rt.queue_view()["items"][-1]["color"] == "#123456"

    rt.scripts_folder = lambda: None
    assert rt.library_files() == {"folder": None, "files": [], "more": False}


# ------------------------------------------------------------------ over HTTP, and for the AI

def test_the_new_ops_over_http(tmp_path, scripts):
    from labharness.agent.approval import WebApprover
    write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, WebApprover(timeout=5))
    client, _ = portal(rt, tmp_path)

    def op(**body):
        return client.post("/api/queue", json=body)
    added = op(op="add", text="script: sweep.py").json()
    assert MINUTE.match(added["name"])
    sid = added["items"][0]["id"]
    got = op(op="rename", name="Night").json()
    assert got["name"] == "Night" and got["message"] == "renamed Night"
    got = op(op="params", item=sid, params={"VMAX": 0.7}).json()
    assert got["items"][0]["params"] == {"VMAX": 0.7} and got["items"][0]["settable"] == {"VMAX": 0.4}
    bad = op(op="params", item=sid, params={"VMAX": "ten"})
    assert bad.status_code == 400 and "VMAX is a number" in bad.json()["error"]
    wid = op(op="add", text=WAIT).json()["items"][-1]["id"]
    got = op(op="params", item=wid, wait=120).json()
    assert got["items"][-1]["wait"] == 120 and got["items"][-1]["name"] == "wait 2 min"
    assert op(op="params", item=wid, set={"smu.V": 0.2}).json()["items"][-1]["set"] == {"smu.V": 0.2}
    got = op(op="duplicate", item=sid).json()
    assert len(got["items"]) == 3 and got["items"][1]["text"] == got["items"][0]["text"]
    assert got["message"].startswith("duplicated sweep.py as #2")
    got = op(op="ladder", items=[sid], key="VMAX", values=[0.1, 0.2]).json()
    assert len(got["items"]) == 5 and got["message"] == "added 2 items: VMAX = 0.1, 0.2"
    bad = op(op="ladder", items=[sid], key="VMAX", values=list(range(500)))
    assert bad.status_code == 400 and "at most 400" in bad.json()["error"]
    got = op(op="skip", item=wid).json()
    assert [i["status"] for i in got["items"]].count("skipped") == 1 and got["message"] == "skipped wait 2 min"
    assert op(op="skip", item=wid).status_code == 400          # skipped already: refused
    assert op(op="duplicate", item="nope").status_code == 400
    assert op(op="fly").status_code == 400
    assert client.post("/api/queue", content=b"[1]", headers={"content-type": "application/json"}).status_code == 400
    state = client.get("/api/state").json()["queue"]
    assert state["name"] == "Night" and state["items"][0]["summary"] == "VMAX = 0.7"
    assert client.get("/api/queue").json()["items"][0]["settable"] == {"VMAX": 0.4}

    def lib(**body):
        return client.post("/api/library", json=body)
    files = lib(op="files").json()
    assert files["folder"] == str(scripts) and [f["rel"] for f in files["files"]] == ["sweep.py"]
    seen = lib(op="inspect", path=files["files"][0]["path"]).json()
    assert seen["settable"] == {"VMAX": 0.4} and seen["problem"] is None
    assert lib(op="inspect", path="").status_code == 400
    entry = lib(op="import", path=files["files"][0]["path"]).json()["entry"]
    got = lib(op="meta", id=entry["id"], name="Gate sweep", color="#aabbcc").json()
    assert got["entry"]["color"] == "#aabbcc" and got["library"]["scripts"][0]["name"] == "Gate sweep"
    assert lib(op="meta", id=entry["id"], color="red").status_code == 400
    assert lib(op="meta", id="script-nope", note="x").status_code == 404
    assert client.get("/api/queue").json()["items"][0]["color"] == "#aabbcc"   # its blocks, by its file


def test_a_poll_never_fails_on_a_value_json_has_no_word_for(tmp_path, scripts):
    from labharness.agent.approval import WebApprover
    write(scripts, "odd.py", "LIMIT = 1e999   # no limit\nDAY = '2026-11-02'\nprint(LIMIT, DAY)\n")
    rt = script_runtime(tmp_path, scripts, WebApprover(timeout=5))
    client, _ = portal(rt, tmp_path)
    assert rt.queue_add("script: odd.py\nparams: {DAY: 2026-11-03}").startswith("not added: DAY is text")   # a date
    assert rt.queue_add("script: odd.py\nparams: {DAY: '2026-11-03'}").startswith("added")
    state = client.get("/api/state")
    assert state.status_code == 200, state.text
    (item,) = state.json()["queue"]["items"]
    assert item["settable"] == {"LIMIT": None, "DAY": "2026-11-02"} and item["params"] == {"DAY": "2026-11-03"}
    assert client.post("/api/library", json={"op": "inspect", "path": str(scripts / "odd.py")}).status_code == 200
    client.post("/api/library", json={"op": "import", "path": str(scripts / "odd.py")})
    assert client.get("/api/library").status_code == 200


def test_through_the_hub_a_block_of_unshared_work_shows_none_of_it(tmp_path, scripts, monkeypatch):
    import json
    from test_team_and_analysis import _relay
    write(scripts, "sweep.py", SCRIPT)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([]))
    with rt.attributed({"incognito": True}):                  # from an incognito chat: never shared
        rt.queue_add("script: sweep.py\nparams: {VMAX: 0.6}\nnote: Acme wafer")
    rt.queue_add(WAIT)
    rt.queue_rename("Acme run")
    ask = _relay(rt, tmp_path, monkeypatch)
    rt.scripts_folder = lambda: str(scripts)                   # as Settings would have it
    assert rt.queue_view()["items"][0]["settable"] == {"VMAX": 0.4}   # on the lab's own network: all of it
    status, raw = ask("GET", "/api/state")
    assert status == 200 and b"Acme" not in raw and b"VMAX" not in raw, raw[:3000]
    queue = json.loads(raw)["queue"]
    hidden, wait = queue["items"]
    assert hidden["name"] == "an item of unshared work" and hidden["summary"] == "" and hidden["params"] == {}
    assert hidden["settable"] == {} and hidden["notes"] == {} and queue["name"] == ""
    assert wait["name"] == "wait 10 min" and wait["wait"] == 600     # shared work shows as it is


def test_the_ai_skips_duplicates_and_renames_through_queue_change(tmp_path):
    from test_agent import call
    rt = virtual(tmp_path, [])
    rt.queue_add(WAIT)
    out = call(rt, ("queue_change", {"action": "rename", "name": "Overnight"}),
               ("queue_change", {"action": "duplicate", "item": "1"}),
               ("queue_change", {"action": "skip", "item": "2"}),
               ("queue_show", {}))
    assert out[0] == "renamed Overnight" and out[1] == "duplicated wait 10 min as #2"
    assert out[2] == "skipped wait 10 min" and "name: Overnight" in out[3] and "2. [skipped] step" in out[3]
    assert [i.status for i in rt.queue.items] == ["waiting", "skipped"] and rt.queue.items[1].by == "agent"

    async def schema():
        from mcp import Client
        from labharness.agent.server import build_server
        async with Client(build_server(rt)) as client:
            return {t.name: t for t in (await client.list_tools()).tools}["queue_change"]
    import anyio
    tool = anyio.run(schema)
    assert "name" in tool.input_schema["properties"]
    assert all(w in tool.description for w in ("skip", "duplicate", "rename"))


def test_the_progress_of_a_step_and_item_with(tmp_path):
    assert item_with("step: cool\nset: {tc.setpoint: 2}\nwait: 60", sets={"tc.setpoint": None, "magnet.B": 0.5}) == \
        "step: cool\nset: {magnet.B: 0.5}\nwait: 60\n"
    assert item_with("script: a.py\nparams: {X: 1}", params={"X": None}) == "script: a.py\n"
    assert item_with("plan: p\nsetup: {a.b: 1}\nmeasure: [a.c]", setup={"a.b": 2, "x.y": 3}) == \
        "plan: p\nsetup: {a.b: 2}\nmeasure: [a.c]\n"        # a scan's setup: only the values it has
    rt = real(tmp_path, [])
    rt.queue_add("step: settle\nwait: 2")
    (step,) = rt.queue.items
    rt.queue_start("human")
    until(lambda: step.status == "running")
    progress = until(lambda: item_view(rt, step.id).get("progress"))
    assert progress["phase"] == "wait" and progress["until"] > time.time()
    finish(rt, 10)
    assert step.status == "done" and "progress" not in item_view(rt, step.id) and rt._queue_step_now is None
