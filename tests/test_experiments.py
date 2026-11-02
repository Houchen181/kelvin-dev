"""Experiments: a lab's Python that reaches the instruments by name through Kelvin (the shim's kelvin module), split
into actions, with live inputs, rows of its own and a rehearsal on the simulated lab (experiment.py, scriptrun.py).

The runs start a real Python with Kelvin's stand-in, as tests/test_script_runs.py does, on its simulated lab."""
import json
import math
import threading
import time
from pathlib import Path

import pytest
from test_script_runs import (LAB, SWEEP, _python, folder, lab, output, run,  # noqa: F401 (fixtures)
                              script_runtime, write)

from labharness import experiment, scriptrun
from labharness.cards import parse_card
from labharness.clock import SimClock
from labharness.eventlog import EventLog, RunStore
from labharness.gate import SafetyGate, ScriptedApprover
from labharness.homelab import builtin_registry
from labharness.lab import Lab
from labharness.labconfig import load_lab_config
from labharness.plans import PlanError
from labharness.scanqueue import load_script_item
from labharness.sim.twin import World, sim_backend_factory
from labharness.web.agent import DEFERRED
from labharness.yamlio import load_yaml

SAMPLE = '''
import numpy
from kelvin import lab, action, live, record

V_MAX = 1.0        # V: the top of the sweep
POINTS = 5

def settle():
    lab.set("smu.V", 0)

@action
def initialize():
    lab.set("laser.power", 2.5)
    settle()

@action("scan", main=True)
def scan():
    lab.set("smu.output", True)
    for v in numpy.linspace(0, V_MAX, POINTS):
        if live("Break", False):
            break
        lab.set("smu.V", v)
        record(V=v, X=lab.get("lockin.X"))
    while not live("Stop", False):
        pass
'''


# ------------------------------------------------------------------ read ahead (Task 1)

def test_an_experiment_is_a_script_that_imports_kelvin():
    assert experiment.is_experiment("from kelvin import lab\n")
    assert experiment.is_experiment("import kelvin\n")
    assert not experiment.is_experiment("import pyvisa\n")
    assert not experiment.is_experiment("import kelvinator\n")


def test_an_experiments_actions_main_action_and_live_inputs_are_read_ahead():
    exp = experiment.read(SAMPLE)
    assert exp.actions == ["initialize", "scan"] and exp.main == "scan"
    assert exp.live == {"Break": False, "Stop": False} and exp.problems == []


def test_the_values_an_action_sets_are_read_ahead_through_its_helpers():
    exp = experiment.read(SAMPLE)
    init, scan = experiment.uses(exp, "initialize"), experiment.uses(exp, "scan")
    assert init.sets == {"laser.power": [2.5], "smu.V": [0.0]}
    assert scan.sets == {"smu.output": [True], "smu.V": [0.0, 0.25, 0.5, 0.75, 1.0]}
    assert scan.gets == ["lockin.X"] and init.gets == []


def test_arithmetic_on_a_loop_variable_is_applied_to_each_value():
    src = "from kelvin import lab\nfor mv in [100, 200]:\n    lab.set('smu.V', mv * 1e-3)\n"
    assert experiment.uses(experiment.read(src)).sets == {"smu.V": [0.1, 0.2]}


def test_a_value_worked_out_as_it_runs_is_open():
    src = ("from kelvin import lab\ndef ramp_to(v):\n    lab.set('smu.V', v)\n"
           "ramp_to(float(open('top.txt').read()))\n")
    assert experiment.uses(experiment.read(src)).sets == {"smu.V": None}


def test_a_key_worked_out_as_it_runs_is_said():
    src = "from kelvin import lab\nname = input()\nlab.set(name + '.V', 1)\n"
    assert experiment.uses(experiment.read(src)).unread == [
        "line 3: lab.set(name + '.V', 1) names its instrument or parameter as it runs"]


def test_problems_in_the_actions_are_said():
    src = ("from kelvin import action\n@action('scan', main=True)\ndef a():\n    pass\n"
           "@action('scan')\ndef b():\n    pass\n@action('sweep', main=True)\ndef c():\n    pass\n")
    assert experiment.read(src).problems == ["two actions are named scan (lines 2 and 5)",
                                             "two actions are marked main: scan and sweep"]


def test_read_ahead_gives_an_experiments_instruments_drives_and_values_worked_out(lab, tmp_path):
    src = SAMPLE + "\ndef extra():\n    lab.set('smu.V', float(open('x').read()))\n"
    path = write(tmp_path, "exp.py", src)
    found = scriptrun.read_ahead(lab, path, path.read_text(), "scan")
    assert found.experiment
    assert list(found.instruments) == ["smu", "lockin"]   # first use; extra() and initialize() aren't scan's
    assert found.drives["smu.V"] == [0.0, 0.25, 0.5, 0.75, 1.0] and found.drives["smu.output"] == [True]
    assert "smu.V" not in found.open and "laser.power" not in found.drives
    assert scriptrun.read_ahead(lab, path, path.read_text(), "").open["smu.V"] == \
        "set from values the experiment works out as it runs"


# ------------------------------------------------------------------ kelvin.lab, through the gate (Task 2)

EXP_SWEEP = """
from kelvin import lab
lab.set("smu.output", True)
for v in [0, 0.5, 1.0]:
    lab.set("smu.V", v)
    print("X", lab.get("lockin.X"))
lab.set("smu.V", 0)
lab.set("smu.output", False)
"""

STAGE = """card: 1
id: acme/stage-1
model: ACME Stage-1
vendor: ACME
kind: motion-stage
status: lab-verified
identify: { idn: "^ACME,STAGE-1" }
actions:
  home: { send: "HOME", label: Home search, drives: true }
parameters:
  speed: { label: Speed, kind: measure, unit: mm/s, set: "SPD {value}", get: "SPD?", limits: [0, 10], default: 1 }
"""


def entries(report):
    return [json.loads(ln) for ln in (Path(report.path) / "commands.jsonl").read_text().splitlines()]


@pytest.fixture
def staged():
    registry = builtin_registry()
    registry.add(parse_card(load_yaml(STAGE)))
    cfg = load_lab_config(LAB + '  stage:\n    card: acme/stage-1\n    address: "GPIB0::3::INSTR"\n', registry)
    clock = SimClock()
    world = World(clock, 0)
    return Lab(cfg, registry, sim_backend_factory(world), clock, world)


def test_an_experiment_sets_and_reads_by_name_through_the_gate_with_one_approval(lab, folder, tmp_path):
    write(folder, "exp.py", EXP_SWEEP)
    gate, request, report = run(lab, folder, tmp_path, "exp.py")
    assert report.status == "done", report.message + "\n" + "\n".join(report.output)
    assert "It uses smu, lockin through kelvin.lab" in str(request)
    assert "smu.V: now 0 V, the script uses 0 … 1 V" in str(request)
    assert len(gate.approver.requests) == 1                        # nothing asked as it ran
    assert sum(1 for ln in output(report).splitlines() if ln.startswith("X ")) == 3
    assert [e["value"] for e in entries(report) if e.get("key") == "smu.V" and e["action"] in ("set", "no-change")] \
        == [0, 0.5, 1.0, 0]                                          # 0 at first is the gate's "no-change"
    assert lab.instrument("smu").read("output") is False


def test_a_set_beyond_the_limits_stops_the_experiment_and_brings_its_drives_back_to_safe(lab, folder, tmp_path):
    write(folder, "far.py", """
        from kelvin import lab, KelvinRefused
        lab.set("smu.output", True)
        try:
            lab.set("smu.V", 12)
        except KelvinRefused as e:
            print("got: KelvinRefused", e)
            raise
    """)
    gate, request, report = run(lab, folder, tmp_path, "far.py")
    assert report.status == "aborted"
    assert "refused: smu.V 12 V is outside the lab config's limits [-10, 10] V" in report.message
    assert "got: KelvinRefused" in output(report)
    assert lab.instrument("smu").read("output") is False


def test_raw_by_name_goes_through_the_gate(lab, folder, tmp_path):
    write(folder, "raw.py", """
        from kelvin import lab
        lab.raw("smu", "SOUR:VOLT 0.5")
        print("X", float(lab.raw("lockin", "MEAS:X?")))
        lab.raw("smu", "SOUR:VOLT 0")
    """)
    gate, request, report = run(lab, folder, tmp_path, "raw.py")
    assert report.status == "done", report.output
    assert "smu.V: now 0 V, the script uses 0 … 500 mV" in str(request)


def test_a_kelvin_py_beside_the_experiment_does_not_replace_kelvins_api(lab, folder, tmp_path):
    write(folder, "kelvin.py", 'raise ImportError("the lab\'s own kelvin.py")\n')
    write(folder, "exp.py", "from kelvin import lab\nprint('X', lab.get('lockin.X'))\n")
    gate, request, report = run(lab, folder, tmp_path, "exp.py")
    assert report.status == "done", report.output


def test_the_release_build_brings_every_file_of_the_shim_kelvins_api_too():
    """Kelvin.exe runs a lab's script with the shim's files beside it (scriptrun.SHIM_DIR): the build (kelvin.spec)
    brings each one, or `from kelvin import lab` fails only in the app."""
    spec = (Path(__file__).resolve().parents[1] / "packaging" / "kelvin.spec").read_text(encoding="utf-8")
    for name in sorted(p.name for p in scriptrun.SHIM_DIR.glob("*.py")):
        assert '"%s"' % name in spec, name


def test_a_driving_action_read_ahead_is_approved_with_the_run_and_not_asked_again(staged, folder, tmp_path):
    write(folder, "home.py", "from kelvin import lab\nlab.do('stage.home')\n")
    gate, request, report = run(staged, folder, tmp_path, "home.py")
    assert report.status == "done", report.message + "\n" + output(report)
    assert "It does stage.home (Home search), which drives the sample: this approval covers it" in str(request)
    assert len(gate.approver.requests) == 1


def test_a_driving_action_not_read_ahead_asks_as_it_runs(staged, folder, tmp_path):
    (folder / "which.txt").write_text("stage.home")
    write(folder, "home2.py", "from kelvin import lab\nlab.do(open('which.txt').read())\n")
    gate, request, report = run(staged, folder, tmp_path, "home2.py", answers=(True, False))
    assert report.status == "aborted" and "declined: stage.home not done" in report.message


# ------------------------------------------------------------------ actions (Task 3)

ACTIONS = """
from kelvin import lab, action
print("module ran")

@action
def initialize():
    lab.set("laser.power", 2.5)
    print("initialize ran")

@action("scan", main=True)
def scan():
    for v in [0, 0.5]:
        lab.set("smu.V", v)
    print("scan ran")
"""


def test_the_chosen_action_runs_and_the_others_do_not(lab, folder, tmp_path):
    write(folder, "exp.py", ACTIONS)
    gate, request, report = run(lab, folder, tmp_path, "exp.py", action="initialize")
    out = output(report)
    assert report.status == "done", out
    assert "module ran" in out and "initialize ran" in out and "scan ran" not in out
    assert request.title == "Run script exp.py (initialize)?"
    assert "laser.power" in str(request) and "smu.V" not in str(request)   # only what initialize does


def test_without_an_action_the_main_one_runs(lab, folder, tmp_path):
    write(folder, "exp.py", ACTIONS)
    gate, request, report = run(lab, folder, tmp_path, "exp.py")
    assert "scan ran" in output(report) and request.title == "Run script exp.py (scan)?"


def test_an_action_the_experiment_does_not_have_is_said_before_anything_runs(lab, folder):
    write(folder, "exp.py", ACTIONS)
    with pytest.raises(scriptrun.ScriptError, match=r"exp.py: it has no action 'sweep' \(it has: initialize, scan\)"):
        scriptrun.prepare(lab, "exp.py", [folder], action="sweep", choose_python=_python)


def test_a_plain_script_has_no_actions(lab, folder):
    write(folder, "plain.py", "print('hi')\n")
    with pytest.raises(scriptrun.ScriptError, match="plain.py doesn't use kelvin, so it has no actions"):
        scriptrun.prepare(lab, "plain.py", [folder], action="scan", choose_python=_python)


def test_several_actions_and_no_main_ask_which():
    exp = experiment.read("from kelvin import action\n@action\ndef a():\n    pass\n@action\ndef b():\n    pass\n")
    with pytest.raises(ValueError, match=r"it has several actions and none is marked main: say which \(a, b\)"):
        experiment.choose(exp)
    assert experiment.choose(exp, "b") == "b"
    assert experiment.choose(experiment.read("from kelvin import lab\n")) == ""
    with pytest.raises(ValueError, match="it has no actions: it runs as a whole, so leave action out"):
        experiment.choose(experiment.read("from kelvin import lab\n"), "scan")


def test_a_changed_input_changes_the_range_read_ahead(lab, folder):
    write(folder, "exp.py", SAMPLE)
    prep = scriptrun.prepare(lab, "exp.py", [folder], params={"V_MAX": 2.0}, action="scan", choose_python=_python)
    assert prep.found.drives["smu.V"][-1] == 2.0


# ------------------------------------------------------------------ record: the experiment's own rows (Task 4)

def test_recorded_rows_are_the_runs_rows_and_the_first_column_is_its_axis(lab, folder, tmp_path):
    write(folder, "rec.py", """
        from kelvin import lab, record
        for v in [0, 0.5, 1.0]:
            lab.set("smu.V", v)
            record(V=v, X=lab.get("lockin.X"))
        lab.set("smu.V", 0)
    """)
    gate, request, report = run(lab, folder, tmp_path, "rec.py")
    assert report.status == "done", report.output
    assert [r["V"] for r in report.rows] == [0, 0.5, 1.0]
    assert all(set(r) == {"i", "t", "V", "X"} for r in report.rows)        # no rows of Kelvin's own
    assert report.columns == {"axes": [{"key": "V", "unit": "", "kind": "instrument"}],
                              "measures": [{"key": "X", "unit": "", "kind": "scalar"}]}


def test_recorded_rows_reach_the_live_view_as_they_are_recorded(lab, folder, tmp_path):
    write(folder, "live_rows.py", """
        from kelvin import record
        for v in [0, 0.5, 1.0]:
            record(V=v, X=2 * v)
    """)
    events = []
    gate, request, report = run(lab, folder, tmp_path, "live_rows.py", progress=events.append)
    assert report.status == "done", report.output
    points = [e for e in events if e["event"] == "point"]
    assert [e["row"]["V"] for e in points] == [0, 0.5, 1.0]   # the Runs pane follows its rows, as a plan's
    assert [e["done"] for e in points] == [1, 2, 3]
    assert points[-1]["columns"] == report.columns
    assert events[0]["event"] == "start" and events[-1]["event"] == "end"


def test_columns_names_the_axes_and_units(lab, folder, tmp_path):
    write(folder, "two.py", """
        from kelvin import record, columns
        columns(axes=("g1", "g2"), units={"g1": "V", "g2": "V", "R": "ohm"})
        record(g1=0.1, g2=0.2, R=50.0)
    """)
    gate, request, report = run(lab, folder, tmp_path, "two.py")
    assert [(a["key"], a["unit"]) for a in report.columns["axes"]] == [("g1", "V"), ("g2", "V")]
    assert report.columns["measures"] == [{"key": "R", "unit": "ohm", "kind": "scalar"}]


def test_numpy_values_are_recorded_as_plain_numbers(lab, folder, tmp_path):
    pytest.importorskip("numpy")
    write(folder, "np.py", """
        import numpy
        from kelvin import record, trace
        record(a=numpy.float64(1.5), b=numpy.array([1, 2]), c=float("nan"),
               s=trace(numpy.array([0.5, 0.25]), x=[1.0, 2.0]))
    """)
    gate, request, report = run(lab, folder, tmp_path, "np.py")
    assert report.status == "done", output(report)
    row = report.rows[0]
    assert row["a"] == 1.5 and type(row["a"]) is float and row["b"] == [1, 2] and math.isnan(row["c"])
    assert row["s"] == [0.5, 0.25] and row["s.x"] == [1.0, 2.0]
    assert {"key": "s", "unit": "", "kind": "trace"} in report.columns["measures"]


def test_a_list_of_lists_is_recorded_as_an_image(lab, folder, tmp_path):
    write(folder, "img.py", "from kelvin import record\nrecord(n=1, frame=[[0, 1], [2, 3]])\n")
    gate, request, report = run(lab, folder, tmp_path, "img.py")
    assert report.status == "done", output(report)
    assert {"key": "frame", "unit": "", "kind": "image"} in report.columns["measures"]


def test_i_and_t_are_kelvins_own_columns(lab, folder, tmp_path):
    write(folder, "bad.py", "from kelvin import record\nrecord(t=1)\n")
    gate, request, report = run(lab, folder, tmp_path, "bad.py")
    assert report.status == "aborted" and "ValueError: i and t are Kelvin's own columns" in output(report)


# ------------------------------------------------------------------ live inputs, set_live, stopped() (Task 5)

BREAKS = """
import time
from kelvin import live
n = 0
while not live("Break", False):
    n += 1
    time.sleep(0.05)
    if n > 2000:
        raise SystemExit("never saw Break")
print("broke after", n)
"""


def test_a_live_input_changed_mid_run_reaches_the_running_experiment(lab, folder, tmp_path):
    write(folder, "brk.py", BREAKS)
    gate, request, report = run(lab, folder, tmp_path, "brk.py",
                                during=(1.5, lambda job: job.set_live("Break", "on", by="tester")))
    assert report.status == "done", output(report)
    assert "broke after" in output(report)
    assert "While it runs, these may change (set_live): Break (now False)" in str(request)
    live_entry = next(e for e in entries(report) if e.get("action") == "live")
    assert {"action": "live", "live": "Break", "value": True, "before": False, "by": "tester"}.items() <= \
        live_entry.items()


def test_a_live_value_is_coerced_to_its_inputs_type_or_refused(lab, folder, tmp_path):
    write(folder, "inputs.py", "from kelvin import live\nlive('Break', False)\nlive('Setpoint', 0.5)\n"
                               "live('Mode', 'fast')\n")
    prep = scriptrun.prepare(lab, "inputs.py", [folder], choose_python=_python)
    job = scriptrun.ScriptRun(SafetyGate(lab, ScriptedApprover([]), EventLog(None, lab.clock)), prep,
                              RunStore(tmp_path / "runs"), {}, set())
    assert job.set_live("Break", "true") == (False, True)
    assert job.set_live("Setpoint", "0.75") == (0.5, 0.75)
    assert job.set_live("Mode", 3) == ("fast", "3")
    with pytest.raises(ValueError, match="Break takes true or false, not 'maybe'"):
        job.set_live("Break", "maybe")
    with pytest.raises(KeyError, match=r"inputs.py has no live input Nope \(it has: Break, Setpoint, Mode\)"):
        job.set_live("Nope", 1)


def test_a_stop_ends_the_experiments_own_threads(lab, folder, tmp_path):
    write(folder, "threads.py", """
        import threading, time
        from kelvin import lab, stopped
        seen = []

        def watch():
            while not stopped():
                lab.get("lockin.X")
                time.sleep(0.05)
            seen.append("thread saw the stop")

        t = threading.Thread(target=watch)
        t.start()
        lab.set("smu.output", True)
        lab.set("smu.V", 1)
        try:
            while True:
                time.sleep(0.1)
        finally:
            t.join(5)
            print(seen[0] if seen else "thread still running")
    """)
    gate, request, report = run(lab, folder, tmp_path, "threads.py", stop_after=1.5)
    assert report.status == "aborted" and "stop requested" in report.message
    assert "thread saw the stop" in output(report)
    assert lab.instrument("smu").read("output") is False and lab.instrument("smu").read("V") == 0


# ------------------------------------------------------------------ rehearsal on the simulated lab (Task 6)

SLOW = """
import time
from kelvin import lab, record
lab.set("smu.output", True)
for v in [0.2, 0.4, 0.6]:
    lab.set("smu.V", v)
    time.sleep(30)
    record(V=v, X=lab.get("lockin.X"))
open("written.txt", "w").write("x")
lab.set("smu.V", 0)
lab.set("smu.output", False)
"""


def prepared(lab, folder, name, **kw):
    return scriptrun.prepare(lab, name, [folder], choose_python=_python, **kw)


def test_an_experiment_rehearses_on_the_simulated_lab_and_the_real_lab_is_untouched(lab, folder):
    write(folder, "slow.py", SLOW)
    t0 = time.monotonic()
    r = scriptrun.rehearse(lab, prepared(lab, folder, "slow.py"))
    assert r.ok and r.ended == "by itself", r.problems
    assert r.lab_seconds >= 90 and time.monotonic() - t0 < 30
    assert [row["V"] for row in r.rows] == [0.2, 0.4, 0.6]
    assert any(c.get("key") == "smu.V" for c in r.commands)
    assert lab.instrument("smu").read("V") == 0 and lab.instrument("smu").read("output") is False
    assert not (folder / "written.txt").exists()


def test_a_rehearsal_that_does_not_end_is_stopped_and_counts_if_it_stops(lab, folder):
    write(folder, "loop.py", "import time\nfrom kelvin import live\nwhile not live('Stop', False):\n"
                             "    time.sleep(1)\n")
    r = scriptrun.rehearse(lab, prepared(lab, folder, "loop.py"), seconds=2)
    assert r.ok and r.ended == "on Stop"


def test_a_rehearsal_that_breaks_a_limit_fails(lab, folder):
    write(folder, "far.py", "from kelvin import lab\nlab.set('smu.V', 12)\n")
    r = scriptrun.rehearse(lab, prepared(lab, folder, "far.py"))
    assert not r.ok and r.ended == "failed"
    assert any("refused: smu.V 12 V is outside the lab config's limits [-10, 10] V" in p for p in r.problems)


def test_a_rehearsal_never_reaches_outside_the_simulated_lab(lab, folder):
    write(folder, "out.py", "import pyvisa\nfrom kelvin import lab\n"
                            "pyvisa.ResourceManager().open_resource('GPIB0::5::INSTR')\n")
    r = scriptrun.rehearse(lab, prepared(lab, folder, "out.py"))
    assert not r.ok and r.asked == []
    assert any("a rehearsal reaches only the simulated lab, not GPIB0::5::INSTR" in p for p in r.problems)


def test_a_change_beyond_the_approved_range_is_listed_as_asked(lab, folder):
    top = folder / "top.txt"            # by its full path: a rehearsal works in a folder of its own
    top.write_text("3")
    write(folder, "top.py", "from kelvin import lab\nlab.set('smu.V', float(open(%r).read()))\n" % str(top))
    r = scriptrun.rehearse(lab, prepared(lab, folder, "top.py", drives={"smu.V": [0, 1]}))
    assert r.ok and any("smu.V" in a and "3 V" in a for a in r.asked)


def test_a_plain_script_is_not_rehearsed(lab, folder):
    write(folder, "plain.py", "print('hi')\n")
    with pytest.raises(scriptrun.ScriptError, match="plain.py doesn't use kelvin.lab, so Kelvin can't rehearse it"):
        scriptrun.rehearse(lab, prepared(lab, folder, "plain.py"))


# ------------------------------------------------------------------ the runtime, the agent's tools, the queue (Task 7)

def approve_everything(rt):
    """A person who says yes to every card, until the test ends (the returned event)."""
    done = threading.Event()

    def person():
        while not done.is_set():
            entry = rt.approver.wait_for_pending(timeout=0.5)
            if entry:
                rt.approver.answer(entry["id"], True)
    threading.Thread(target=person, daemon=True).start()
    return done


def wait_until(predicate, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("still waiting after %s s" % seconds)


def test_run_script_runs_an_action_in_the_background_and_set_live_reaches_it(lab, folder, tmp_path):
    write(folder, "brk.py", BREAKS)
    rt = script_runtime(lab, folder, tmp_path, timeout=30)
    person = approve_everything(rt)
    try:
        said = rt.run_script("brk.py", background=True)
        assert "While it runs, set_live changes its live inputs: Break" in said, said
        wait_until(lambda: rt._live_run is not None, 30)
        assert rt.set_live("Break", True).startswith("Break is now True in run ")
        wait_until(lambda: rt.queue.state != "running", 60)
        run_id = rt.queue.items[-1].run_id
        assert "broke after" in (rt._run_dir(run_id) / "output.txt").read_text()
        assert rt.set_live("Break", False) == \
            "no experiment is running now: a live input changes only while its experiment runs"
    finally:
        person.set()


def test_set_live_never_waits_for_the_lab(lab, folder, tmp_path):
    rt = script_runtime(lab, folder, tmp_path)
    hold = threading.Event()
    threading.Thread(target=lambda: rt._do("a run", lambda: hold.wait(10) and ""), daemon=True).start()
    time.sleep(0.2)
    t0 = time.monotonic()
    assert rt.set_live("Break", True).startswith("no experiment is running now")
    assert time.monotonic() - t0 < 1
    hold.set()


def test_dry_run_rehearses_an_experiment_and_says_a_plain_script_cant_be(lab, folder, tmp_path):
    write(folder, "exp.py", EXP_SWEEP)
    write(folder, "sweep.py", SWEEP)
    rt = script_runtime(lab, folder, tmp_path)
    said = rt.run_script("exp.py", dry_run=True)
    assert said.startswith("REHEARSAL ONLY") and "it ended by itself" in said, said
    assert rt.approver.history_list() == []               # nobody was asked
    assert "not rehearsed: sweep.py doesn't use kelvin.lab" in rt.run_script("sweep.py", dry_run=True)


def test_a_queued_action_runs_and_run_again_keeps_it(lab, folder, tmp_path):
    write(folder, "exp.py", ACTIONS)
    rt = script_runtime(lab, folder, tmp_path, timeout=30)
    person = approve_everything(rt)
    try:
        rt.queue_run_now(["script: exp.py\naction: initialize\n"], by="agent")
        wait_until(lambda: rt.queue.state != "running" and rt.queue.items[-1].run_id, 60)
        assert "initialize ran" in (rt._run_dir(rt.queue.items[-1].run_id) / "output.txt").read_text()
        again = rt.run_again(rt.queue.items[-1].run_id, at_end=True)
        assert "action: initialize" in again["queue"]["items"][-1]["text"]
    finally:
        person.set()


def test_a_script_items_action_is_text():
    with pytest.raises(PlanError, match="action: the name of one of the experiment's actions"):
        load_script_item({"script": "x.py", "action": 3})
    assert load_script_item({"script": "x.py", "action": "scan"}).name == "x.py (scan)"


def test_check_script_names_an_experiments_actions(lab, folder, tmp_path):
    write(folder, "exp.py", ACTIONS)
    rt = script_runtime(lab, folder, tmp_path)
    assert "Actions: initialize, scan (main)" in rt.check_script("exp.py")


def test_set_live_is_with_the_scripts_tools():
    assert "set_live" in DEFERRED["running the lab's own scripts"]


# ------------------------------------------------------------------ the final review's findings

def test_a_rehearsal_starts_no_program_however_the_experiment_reaches_for_one(lab, folder, tmp_path):
    marker = tmp_path / "ran.txt"
    write(folder, "prog.py", """
        import os
        try:
            import numpy                  # what an experiment uses still loads (where there is numpy)
        except ImportError:
            pass
        from kelvin import lab
        lab.set("smu.V", 0.1)
        getattr(os, "sys" + "tem")(%r)
    """ % ("echo x > %s" % marker))
    r = scriptrun.rehearse(lab, prepared(lab, folder, "prog.py"))
    assert not r.ok and r.ended == "failed", r
    assert any("a rehearsal reaches only the simulated lab" in p for p in r.problems), r.problems
    assert not marker.exists()


def test_a_rehearsal_loads_no_native_library(lab, folder):
    write(folder, "native.py", """
        import os
        from kelvin import lab
        ct = __import__("ct" + "ypes")
        ct.WinDLL("kernel32") if os.name == "nt" else ct.CDLL("libc.so.6")
        print("loaded")
    """)
    r = scriptrun.rehearse(lab, prepared(lab, folder, "native.py"))
    assert not r.ok and any("a rehearsal reaches only the simulated lab" in p for p in r.problems), r.problems


def test_a_rehearsal_writes_only_in_its_own_folder(lab, folder, tmp_path):
    outside = tmp_path / "outside.txt"
    write(folder, "wr.py", "from kelvin import lab\nopen(%r, 'w').write('x')\n" % str(outside))
    r = scriptrun.rehearse(lab, prepared(lab, folder, "wr.py"))
    assert not r.ok and not outside.exists()
    assert any("a rehearsal reaches only the simulated lab" in p for p in r.problems), r.problems


@pytest.mark.parametrize("line, said", [
    ("import subprocess", "it imports subprocess"),
    ("import win32com.client", "it imports win32com"),
    ("from ctypes import WinDLL", "it imports ctypes"),
    ("import os\nos.system('dir')", "it calls os.system"),
    ("import zhinst", "it uses zhinst"),
])
def test_an_experiment_that_reaches_past_the_simulated_lab_is_not_rehearsed(lab, folder, line, said):
    write(folder, "past.py", "from kelvin import lab\n%s\n" % line)
    with pytest.raises(scriptrun.ScriptError, match="past.py can't be rehearsed: %s" % said):
        scriptrun.rehearse(lab, prepared(lab, folder, "past.py"))


def test_a_dry_run_builds_its_simulated_lab_under_the_lab_lock(lab, folder, tmp_path):
    write(folder, "exp.py", EXP_SWEEP)
    rt = script_runtime(lab, folder, tmp_path)
    held = []
    make_twin = lab.make_twin

    def twin(*a, **k):   # it reads every instrument's state: never beside work that holds them
        held.append(rt.lock.owner()[0] == threading.get_ident())
        return make_twin(*a, **k)
    lab.make_twin = twin
    assert "it ended by itself" in rt.run_script("exp.py", dry_run=True)
    assert held == [True]


def test_a_live_default_written_as_a_constant_is_read_ahead_and_reaches_the_script(lab, folder, tmp_path):
    src = """
        from kelvin import live
        SETPOINT = 0.5
        MODE = "fast"
        print("Setpoint", live("Setpoint", SETPOINT))
        print("Mode", live("Mode", MODE))
        print("Who", live("Who", str(1 + 1)))
    """
    write(folder, "consts.py", src)
    assert experiment.read((folder / "consts.py").read_text()).live == {"Setpoint": 0.5, "Mode": "fast", "Who": None}
    gate, request, report = run(lab, folder, tmp_path, "consts.py")
    out = output(report)
    assert "Setpoint 0.5" in out and "Mode fast" in out and "Who 2" in out, out
    assert "Setpoint (now 0.5), Mode (now fast), Who;" in str(request)


def test_an_experiments_card_in_auto_mode_does_not_say_it_cant_be_rehearsed(lab, folder, tmp_path):
    write(folder, "exp.py", EXP_SWEEP)
    rt = script_runtime(lab, folder, tmp_path, timeout=20)
    rt.gate.auto = True
    out = {}
    t = threading.Thread(target=lambda: out.update(said=rt.run_script("exp.py")), daemon=True)
    t.start()
    entry = rt.approver.wait_for_pending(timeout=30)
    rt.approver.answer(entry["id"], False)
    t.join(30)
    text = "\n".join(entry["items"])
    assert "can't be dry-run" not in text
    assert "Auto mode does not cover this run: a person approves each run of a script, experiments too" in text
