"""A person's Stop means stop for the lab's own scripts and experiments (the 0.3.45 review, its section 1).

- A Stop during an experiment's rehearsal is never a clean rehearsal: auto mode and the queue then run nothing (1.1).
- A Stop while a run is approved or starting is never lost: its Python never starts (1.2).
- A script waiting on *OPC? with a long timeout of its own stops within a second, the instrument cleared; a direct read
  that runs out of time leaves the instrument owing its reply, dropped before the next command (1.3).
- A thread of the script's own that goes on reading never keeps its run going after a Stop (1.4).
- A process that is still there after Kelvin ended it never skips the way back to safe (1.5).
"""
import threading
import time

import pytest

from labharness import adapters, scriptrun
from labharness.adapters import set_resource_manager
from labharness.eventlog import EventLog
from labharness.gate import SafetyGate, ScriptedApprover
from labharness.homelab import build_lab
from test_hardware import Bus, Device, Resource
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


# ------------------------------------------------------------------ 1.3 a script's own long wait

class Sweeping(Resource):
    """A VISA session to an instrument in a long sweep: its *OPC? is answered only once the sweep ends (long after the
    script's 40 s here), each read waiting out its timeout as VISA's does; a device clear ends the wait."""

    def __init__(self, dev):
        super().__init__(dev)
        self.owes = False
        self.cleared = 0

    def write(self, cmd):
        if cmd.strip() == "*OPC?":
            self.owes = True
            return
        super().write(cmd)

    def query(self, cmd, delay=None):
        if cmd.strip() == "*OPC?":
            self.write(cmd)
            return self.read()
        return super().query(cmd, delay)

    def read(self):
        if self.owes:
            time.sleep(float(self.timeout or 3000) / 1000.0)
            raise TimeoutError("VI_ERROR_TMO")
        return super().read()

    def clear(self):
        self.cleared += 1
        self.owes = False


class SweepingBus(Bus):
    def open_resource(self, name):
        self.res = Sweeping(self.devices[name])
        return self.res


@pytest.fixture
def sweeping():
    bus = SweepingBus({"GPIB0::24::INSTR": Device("keithley/2400")})
    set_resource_manager(bus)
    yield bus
    set_resource_manager(None)


VISA_LAB = """
lab: Sweep bench
instruments:
  smu: { card: keithley/2400, address: "GPIB0::24::INSTR", limits: { V: [-5, 5] }, rates: { V: 5 } }
"""


def test_a_stop_ends_a_scripts_long_wait_for_opc_within_a_second(sweeping, folder, tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, "WAIT_SLICE_MS", 200)
    lab = build_lab(VISA_LAB)
    write(folder, "long_wait.py", """
        import pyvisa
        smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
        smu.timeout = 40000          # a long sweep: the script waits up to 40 s for it
        try:
            print(smu.query("*OPC?"))
        finally:
            print("finally ran")
    """)
    t0 = time.monotonic()
    gate, request, report = run(lab, folder, tmp_path, "long_wait.py", stop_after=1.5)
    assert time.monotonic() - t0 < 15, report.message                      # not the script's 40 s
    assert report.status == "aborted" and "stop requested" in report.message, report.message
    assert sweeping.res.cleared >= 1                     # its late answer is never taken for another command's
    assert "finally ran" in output(report)
    assert "KEITHLEY" in lab.instrument("smu").query_raw("*IDN?")          # in step after it


def test_a_direct_read_that_times_out_leaves_the_instrument_owing_its_reply(sweeping, folder, tmp_path):
    lab = build_lab(VISA_LAB)
    write(folder, "read.py", """
        import pyvisa
        smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
        try:
            smu.read()                   # nothing was asked: it times out
        except Exception as e:
            print("read failed:", type(e).__name__)
        print(smu.query("*IDN?"))
    """)
    gate, request, report = run(lab, folder, tmp_path, "read.py")
    assert report.status == "done", report.message
    out = output(report)
    assert "read failed" in out and "KEITHLEY" in out, out
    assert sweeping.res.cleared >= 1   # what it may still send was dropped, never read as the *IDN? reply


# ------------------------------------------------------------------ 1.4 a thread of the script's own

def test_a_scripts_own_polling_thread_never_keeps_its_run_going_after_a_stop(lab, folder, tmp_path, monkeypatch):
    monkeypatch.setattr(scriptrun, "GRACE_S", 1.0)
    monkeypatch.setattr(scriptrun, "MAX_GRACE_S", 60.0)
    write(folder, "poller.py", """
        import threading, time, pyvisa
        rm = pyvisa.ResourceManager()
        smu = rm.open_resource("GPIB0::24::INSTR")
        lockin = rm.open_resource("GPIB0::8::INSTR")

        def watch():   # a live plot's readings, in a thread of its own (not a daemon)
            while True:
                lockin.query("SOURce:FREQuency?")
                time.sleep(0.2)
        threading.Thread(target=watch).start()
        smu.write("SOUR:VOLT 0.5")
        for k in range(10000):
            time.sleep(0.1)
    """)
    events = []
    t0 = time.monotonic()
    gate, request, report = run(lab, folder, tmp_path, "poller.py", stop_after=1.5, progress=events.append)
    assert time.monotonic() - t0 < 20, report.message   # not MAX_GRACE_S
    assert report.status == "aborted" and "its main script ended" in report.message, report.message
    kinds = [(e.get("event"), e.get("phase")) for e in events]
    assert ("phase", "stopping") in kinds and kinds.index(("phase", "stopping")) < kinds.index(("end", None)), kinds
    assert lab.instrument("smu").read("V") == 0   # back to safe


# ------------------------------------------------------------------ 1.5 a process that outlives its kill

def test_a_script_process_that_outlives_its_kill_still_goes_back_to_safe(lab, folder, tmp_path, monkeypatch):
    monkeypatch.setattr(scriptrun, "GRACE_S", 1.0)
    monkeypatch.setattr(scriptrun, "END_WAIT_S", 1.0, raising=False)
    monkeypatch.setattr(scriptrun._Job, "end", lambda self: None)   # ended, and still there (stuck in a driver)
    write(folder, "stuck.py", """
        import time, pyvisa
        smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
        smu.write("OUTP ON")
        smu.write("SOUR:VOLT 2")
        while True:
            try:
                time.sleep(0.2)
            except KeyboardInterrupt:
                pass
    """)
    gate, request, report = run(lab, folder, tmp_path, "stuck.py", stop_after=1.0)
    assert report.status == "aborted" and "so Kelvin ended it" in report.message, report.message
    assert any("was still there" in w for w in report.warnings), report.warnings
    assert lab.instrument("smu").read("output") is False   # back to safe all the same
