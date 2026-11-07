"""A person's Stop means stop for the lab's own scripts and experiments (the 0.3.45 review, its section 1).

- A Stop during an experiment's rehearsal is never a clean rehearsal: auto mode and the queue then run nothing (1.1).
- A Stop while a run is approved or starting is never lost: its Python never starts (1.2).
"""
import threading
import time

from labharness import scriptrun
from labharness.eventlog import EventLog
from labharness.gate import SafetyGate, ScriptedApprover
from test_lab_experiments import SLOW, auto_approved, keep_as, rt_keysight  # noqa: F401 (a fixture)
from test_script_runs import SWEEP, _python, folder, lab, output, run, script_runtime, write  # noqa: F401 (fixtures)


def rehearsing(monkeypatch):
    """An event set once a rehearsal has sent the simulated lab a command."""
    seen = threading.Event()
    log = scriptrun.ScriptRun._log

    def logged(self, **entry):
        if self.rehearsal:
            seen.set()
        return log(self, **entry)
    monkeypatch.setattr(scriptrun.ScriptRun, "_log", logged)
    return seen


# ------------------------------------------------------------------ 1.1 a Stop during a rehearsal

def test_in_auto_mode_a_stop_during_the_rehearsal_means_the_real_run_never_starts(rt_keysight, monkeypatch):
    rt = rt_keysight
    keep_as(rt, SLOW, "slow")
    rt.gate.auto = True
    seen = rehearsing(monkeypatch)
    out = {}
    th = threading.Thread(target=lambda: out.update(said=rt.run_script("slow")), daemon=True)
    th.start()
    try:
        assert seen.wait(60), "the rehearsal never ran"
        rt.stop("human (portal)")
        th.join(30)
        assert not th.is_alive(), "the real run started after the Stop"
    finally:
        if th.is_alive():   # it ran on the lab: a second Stop ends it
            rt.stop("human (portal)")
            th.join(60)
    assert out["said"].startswith("script not run: a person pressed Stop while it was rehearsed"), out
    assert not auto_approved(rt, "slow") and rt.approver.pending_list() == []   # neither approved nor asked
    assert rt.lab.instrument("smu").read("V") == 0                               # nothing reached the lab


def test_in_auto_mode_a_stop_during_the_queues_rehearsal_means_it_never_starts(rt_keysight, monkeypatch):
    rt = rt_keysight
    keep_as(rt, SLOW, "slow")
    rt.gate.auto = True
    assert rt.queue_add("script: slow\n").startswith("added")
    seen = rehearsing(monkeypatch)
    out = {}
    th = threading.Thread(target=lambda: out.update(said=rt.queue_start()), daemon=True)
    th.start()
    try:
        assert seen.wait(60), "the rehearsal never ran"
        rt.stop("human (portal)")
        th.join(30)
        assert not th.is_alive()
    finally:
        if rt._queue_thread is not None:   # it started: a second Stop ends it
            rt.stop("human (portal)")
            rt._queue_thread.join(60)
    assert out["said"].startswith("the queue did not start: a person pressed Stop while it was rehearsed"), out
    assert rt.queue.state not in ("running", "pausing") and [i.status for i in rt.queue.items] == ["waiting"]
    assert [e for e in rt.approver.history_list() if e.get("by") == "auto mode"] == []
    assert rt.lab.instrument("smu").read("V") == 0


def test_a_rehearsal_a_person_stops_is_never_clean_but_one_whose_time_ran_out_is(lab, folder):
    write(folder, "slow.py", SLOW)
    prep = scriptrun.prepare(lab, "slow.py", [folder], choose_python=_python)
    real = SafetyGate(lab, ScriptedApprover([]), EventLog(None, lab.clock))   # the lab's own gate
    threading.Timer(1.5, real.request_abort).start()
    r = scriptrun.rehearse(lab, prep, real=real)
    assert not r.ok and r.ended == "stopped by a person" and "a person stopped it" in r.message, r
    timed = scriptrun.rehearse(lab, prep, seconds=1.5, real=real)   # Kelvin's own Stop, as its time ran out
    assert timed.ok and timed.ended == "on Stop", timed
    late = scriptrun.rehearse(lab, prep, real=real, since=real.stop_generation - 1)   # a Stop before it began
    assert not late.ok and late.ended == "stopped by a person" and "before it started" in late.message
    assert late.commands == []


TWO = '''
import time
from kelvin import lab, action, stopped   # VI: n1
@action("scan", main=True)
def scan():
    while not stopped():                  # VI: n2
        lab.set("smu.V", 0.1)             # VI: n3
        time.sleep(1)


@action("after")
def after():
    lab.set("smu.V", 0.0)                 # VI: n4
'''


def test_a_translations_check_a_person_stops_is_not_passed(lab, folder):
    from labharness import vicheck
    path = write(folder, "two.py", TWO)
    real = SafetyGate(lab, ScriptedApprover([]), EventLog(None, lab.clock))
    threading.Timer(1.5, real.request_abort).start()
    out, found = vicheck.rehearse_actions(
        lab, path, TWO, lab.make_twin, real=real,
        prepare=lambda p, action: scriptrun.prepare(lab, str(p), [p.parent], action=action, choose_python=_python))
    assert out["scan"]["ended"] == "stopped by a person" and "after" not in out, out   # the rest isn't rehearsed
    assert any(f.blocks and "a person pressed Stop" in f.text for f in found), found   # keeping it waits


SWEEPS_UNTIL_STOPPED = '''
from kelvin import lab, action, record, stopped   # VI: n1
V_STOP = 1.0                             # VI: c10
@action("scan", main=True)
def scan():
    while not stopped():
        for v in [0.0, 0.5, V_STOP]:         # VI: n2
            lab.set("smu.V", v)              # VI: n3
            record(V=v, X=lab.get("lockin.X"))   # VI: n4, c11
'''


def test_a_translation_a_person_stops_is_neither_repaired_nor_passed(lab, folder):
    from labharness import vicheck, vitranslate
    from test_translation import DESC, NOTES, TABLE, fenced, prepare_for, scripted
    real = SafetyGate(lab, ScriptedApprover([]), EventLog(None, lab.clock))
    ask = scripted(fenced(SWEEPS_UNTIL_STOPPED, NOTES))
    threading.Timer(1.5, real.request_abort).start()
    t = vitranslate.translate(DESC, lab, ask, TABLE, folder, lab.make_twin, prepare=prepare_for(lab), real=real)
    assert t.rounds == 1 and len(ask.said) == 1, t.report.text()   # the model isn't asked to "repair" a Stop
    assert not t.report.ok_to_keep and vicheck.stopped(t.report), t.report.text()


# ------------------------------------------------------------------ 1.2 a Stop while a run is approved or starting

def test_a_stop_while_a_script_run_is_starting_means_its_python_never_starts(lab, folder, tmp_path, monkeypatch):
    write(folder, "slow_start.py", """
        import pyvisa
        smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
        smu.write("OUTP ON")
        smu.write("SOUR:VOLT 1")
        print("ran to the end")
    """)
    files_now = scriptrun.ScriptRun._files_now

    def slow(self):   # the first walk of its folder (a network drive), before its Python starts
        if not getattr(self, "_walked", False):
            self._walked = True
            time.sleep(2.0)
        return files_now(self)
    monkeypatch.setattr(scriptrun.ScriptRun, "_files_now", slow)
    gate, request, report = run(lab, folder, tmp_path, "slow_start.py", stop_after=0.5)
    assert report.status == "aborted" and "stop requested before it started" in report.message, report.message
    assert "ran to the end" not in output(report) and report.commands == 0
    assert lab.instrument("smu").read("output") is False


def test_a_stop_before_a_script_starts_is_never_forgotten_by_its_approval(lab, folder, tmp_path):
    write(folder, "sweep.py", SWEEP)
    rt = script_runtime(lab, folder, tmp_path)
    preflight = rt.engine.preflight

    def stopped_meanwhile(*a, **k):   # a person presses Stop while its instruments are checked
        rt.stop("human (portal)")
        return preflight(*a, **k)
    rt.engine.preflight = stopped_meanwhile

    def person():   # who then says yes to its card anyway (on a phone that never showed the Stop)
        asked = rt.approver.wait_for_pending(timeout=30)
        if asked:
            rt.approver.answer(asked["id"], True)
    threading.Thread(target=person, daemon=True).start()
    said = rt.run_script("sweep.py")
    assert said.startswith("script not run: a person pressed Stop before it started"), said
    assert lab.instrument("smu").read("output") is False
