"""Reviews of the merged fixes (2026-11-14): what a stalled data folder, a slow instrument, a script being read or a
wedged GPIB adapter still held up, kept fixed. A "stalled share" is a sleep in the one file call the code makes."""
import pathlib
import threading
import time

import pytest

from test_field_test_2026_11_06 import runtime
from test_field_test_2026_11_13_hung_queue import wait_until
from test_script_runs import SWEEP, folder, lab, script_runtime, write  # noqa: F401 (fixtures)

STALL_S = 3.0
PLAN = "plan: log\ntime: { every: 0.2, points: 3 }\nmeasure: [smu.I]\n"


def test_a_stop_never_waits_behind_run_now_reading_the_data_folder(tmp_path):
    """Run again read a run's folder (its version's marks) holding the queue's lock, and Stop took that lock before
    it stopped anything: the run went on driving for as long as the share stalled."""
    rt = runtime(tmp_path)
    inside = threading.Event()

    def stalled_version_marks(text):
        inside.set()
        time.sleep(STALL_S)
        return {}
    rt._version_marks = stalled_version_marks
    worker = threading.Thread(target=lambda: rt.queue_run_now([PLAN], at_end=True), daemon=True)
    worker.start()
    assert inside.wait(5)
    gen = rt.gate.stop_generation
    t0 = time.monotonic()
    rt.stop("human (portal)")
    assert rt.gate.stop_generation != gen and time.monotonic() - t0 < 1.0
    worker.join(10)


def test_the_sequences_view_never_waits_long_on_a_stalled_data_folder(tmp_path, monkeypatch):
    """Every Lab panel poll, and Skip and Pause before they act, read the library under one lock: a second view
    waited for the first one's stall, then stalled itself."""
    rt = runtime(tmp_path)
    rt.queue_add(PLAN)
    real_stat = pathlib.Path.stat
    stalls = []

    def stat(self, *a, **k):
        if self.name == "library.json":
            stalls.append(threading.get_ident())
            time.sleep(STALL_S)
        return real_stat(self, *a, **k)
    monkeypatch.setattr(pathlib.Path, "stat", stat)
    poll = threading.Thread(target=rt.ui_state, daemon=True)
    poll.start()
    assert wait_until(lambda: stalls, 5)
    t0 = time.monotonic()
    view = rt.queue_view()
    assert time.monotonic() - t0 < 2.0 and view["items"]   # the queue as it is, with what was found of it last
    poll.join(10)


def test_the_portals_stop_never_waits_behind_a_poll_on_a_stalled_data_folder(tmp_path, monkeypatch):
    """The Lab panel's poll read the data folder on the portal's own loop: while it stalled, nothing else ran there,
    every device's Stop included."""
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    rt = runtime(tmp_path)
    settings = Settings(tmp_path / "Kelvin" / "settings.json")
    client = TestClient(create_app(rt, settings, ChatStore(tmp_path / "Kelvin" / "chats"), hosts=["testserver"],
                                   providers={}))
    h = {"X-Kelvin-Token": settings.access_key}
    real_stat = pathlib.Path.stat
    inside, armed = threading.Event(), threading.Event()

    def stat(self, *a, **k):
        if armed.is_set() and self.name == "home.json" and not inside.is_set():
            inside.set()
            time.sleep(STALL_S)
        return real_stat(self, *a, **k)
    monkeypatch.setattr(pathlib.Path, "stat", stat)
    with client:
        armed.set()
        poll = threading.Thread(target=lambda: client.get("/api/state", headers=h), daemon=True)
        poll.start()
        assert inside.wait(10)
        t0 = time.monotonic()
        r = client.post("/api/stop", json={}, headers=h)
        assert r.status_code == 200 and time.monotonic() - t0 < 1.5
        poll.join(15)


def test_one_drives_hold_never_keeps_another_instruments_output_on(tmp_path):
    """A wait that gave up set its set point back holding the gate's one lock, which every way to safe took first:
    one instrument whose connection was held kept another's output on."""
    from labharness.adapters import set_resource_manager
    from test_hardware import Bus, Device
    set_resource_manager(Bus({"GPIB0::24::INSTR": Device("keithley/2400"), "GPIB0::25::INSTR": Device("keithley/2400")}))
    try:
        rt = runtime(tmp_path, """lab: Bench
instruments:
  smu1:
    card: keithley/2400
    address: GPIB0::24::INSTR
  smu2:
    card: keithley/2400
    address: GPIB0::25::INSTR
""")
        gate = rt.gate
        one, two = rt.lab.instruments["smu1"], rt.lab.instruments["smu2"]
        for inst in (one, two):
            inst.write("output", 1)
            inst.write("V", 0.5)
        held, release = threading.Event(), threading.Event()

        def holder():
            with one.backend.lock:
                held.set()
                release.wait(20)
        threading.Thread(target=holder, daemon=True).start()
        assert held.wait(5)
        giving_up = threading.Thread(target=lambda: gate._gave_up(one, one.spec("V"), 0.5, 1.0, True, "test",
                                                                  alarm=False), daemon=True)
        giving_up.start()
        time.sleep(0.5)
        t = threading.Thread(target=lambda: gate.abort_to_safe(["smu2.V", "smu2.output"], by="test", hold=False),
                             daemon=True)
        t.start()
        assert wait_until(lambda: two.spec("output").distance(two.read("output")) == 0, 3.0), "smu2 waited for smu1"
        release.set()
        t.join(20)
        giving_up.join(20)
    finally:
        set_resource_manager(None)


def test_the_restart_check_never_walks_the_runs_holding_the_lab(tmp_path, monkeypatch):
    """At a start, the check of what the last stop left walked every run folder with the lab held: minutes on a big
    share, and a Stop didn't end it."""
    from labharness.eventlog import RunFolders
    rt = runtime(tmp_path)
    real = RunFolders.iterdir
    walking = threading.Event()

    def slow(self):
        walking.set()
        time.sleep(STALL_S)
        return real(self)
    monkeypatch.setattr(RunFolders, "iterdir", slow)
    rt._incident_due = True
    t = threading.Thread(target=rt._incident_check, daemon=True)
    t.start()
    assert walking.wait(5)
    got = rt.lock.acquire(timeout=1.0)
    if got:
        rt.lock.release()
    t.join(15)
    assert got


def test_a_stop_while_a_script_is_read_never_shows_its_card(lab, folder, tmp_path):
    """A Stop pressed while a script was being read (its folder walked, its Python found) didn't reach the card
    after: it showed, and held the lab for its whole timeout."""
    write(folder, "sweep.py", SWEEP)
    rt = script_runtime(lab, folder, tmp_path, timeout=4)
    prepare = rt._prepare_script
    reading, go = threading.Event(), threading.Event()

    def slow_prepare(*a, **k):
        out = prepare(*a, **k)
        reading.set()
        go.wait(20)
        return out
    rt._prepare_script = slow_prepare
    out = {}
    th = threading.Thread(target=lambda: out.update(said=rt.run_script("sweep.py")), daemon=True)
    th.start()
    assert reading.wait(30)
    rt.stop("human (portal)")
    go.set()
    assert rt.approver.wait_for_pending(timeout=3) is None, "a card was shown after the person's Stop"
    th.join(30)
    assert "not run" in out.get("said", "") and "Stop" in out["said"], out


FAILS = """
import pyvisa
rm = pyvisa.ResourceManager()
smu = rm.open_resource("GPIB0::24::INSTR")
smu.write("OUTP ON")
smu.write("SOUR:VOLT 0.5")
raise OSError("the share holding this script stopped answering")
"""


def test_a_failed_scripts_drives_go_back_to_safe_before_its_folder_is_read(lab, folder, tmp_path, monkeypatch):
    """A failed script's hint ("it is a package's module") read the scripts folder before its drives went back to
    safe: on a share that stalled, they stayed on as long."""
    from labharness import scriptrun
    from test_script_runs import run
    write(folder, "fails.py", FAILS)
    order = []
    real_name, real_safe = scriptrun.package_name, scriptrun.ScriptRun._back_to_safe

    def slow_name(path):
        order.append("package_name")
        time.sleep(1.0)
        return real_name(path)

    def safe(self):
        order.append("_back_to_safe")
        return real_safe(self)
    monkeypatch.setattr(scriptrun, "package_name", slow_name)
    monkeypatch.setattr(scriptrun.ScriptRun, "_back_to_safe", safe)
    run(lab, folder, tmp_path, "fails.py")
    assert order and order[0] == "_back_to_safe", order


def test_a_rescue_goes_beside_work_that_isnt_going_to_safe(tmp_path, monkeypatch):
    """Bring to safe waited two minutes behind any work it couldn't see stuck on an instrument (a bus's clear on a
    wedged adapter, a run's record on a stalled share): it waits that long only for work on its way to safe."""
    from labharness.agent import runtime as rtmod
    monkeypatch.setattr(rtmod, "RESCUE_WAIT_S", 0.5)
    monkeypatch.setattr(rtmod, "RESCUE_MAX_S", 30.0)
    rt = runtime(tmp_path)
    held = threading.Event()

    def busy_elsewhere():   # holds the lab, in no exchange with an instrument and not going to safe
        with rt.lock:
            held.set()
            time.sleep(6.0)
    threading.Thread(target=busy_elsewhere, daemon=True).start()
    assert held.wait(5)
    t0 = time.monotonic()
    lines = rt.bring_to_safe("human (portal)")
    assert time.monotonic() - t0 < 4.0 and "went ahead beside it" in lines[0], lines


def test_a_conda_environment_on_a_stalled_share_never_holds_the_lab(lab, folder, tmp_path, monkeypatch):
    """check_script listed this computer's conda environments holding the lab: one listed on a share that stalled
    held every other change to the lab."""
    from labharness import scriptrun
    write(folder, "sweep.py", SWEEP)
    rt = script_runtime(lab, folder, tmp_path, timeout=4)
    seen = []

    def envs():
        seen.append(rt.lock.owner()[0] == threading.get_ident())
        return []
    monkeypatch.setattr(scriptrun, "conda_envs", envs)
    rt.check_script("sweep.py")
    assert seen and not any(seen)


def test_a_module_lookup_through_the_scripts_folders_is_bounded(tmp_path, monkeypatch):
    """A look for a module by its name walked every folder of the scripts folders, with no end, on every look (a
    queue's start looked nine times): it now gives up past its budget, saying how to name it instead."""
    from labharness import scriptrun
    for i in range(30):
        (tmp_path / ("d%02d" % i) / "sub").mkdir(parents=True)
    monkeypatch.setattr(scriptrun, "WALK_DIRS", 10)
    with pytest.raises(scriptrun.ScriptError, match="give its path"):
        scriptrun.locate_module("nvpulse", [tmp_path])


def test_a_stop_goes_out_while_another_thread_holds_the_queues_lock(tmp_path):
    """Stop took the queue's lock before it stopped anything: whatever held it (a write on a stalled share, a view)
    held the Stop too. The queue is set pausing and the run stopped whether or not the lock comes at once."""
    rt = runtime(tmp_path)
    with rt.queue.lock:
        rt.queue.state = "running"
    held = threading.Event()

    def holder():
        with rt.queue.lock:
            held.set()
            time.sleep(STALL_S)
    threading.Thread(target=holder, daemon=True).start()
    assert held.wait(5)
    gen = rt.gate.stop_generation
    t0 = time.monotonic()
    rt.stop("human (portal)")
    assert rt.gate.stop_generation != gen and time.monotonic() - t0 < 1.0
    assert rt.queue.state == "pausing"


# ------------------------------------------------------------------ the fault scenarios' findings (2026-11-14)

SMU_LAB = """lab: Bench
instruments:
  smu:
    card: keithley/2400
    address: GPIB1::24::INSTR
"""

IV = """plan: iv
setup: { smu.output: on }
sweep: { smu.V: {from: 0, to: 0.5, step: 0.05} }
settle: 0.3
measure: [smu.I]
"""


def test_a_stop_reaches_a_run_whose_record_stalls_and_its_drives_go_to_safe(tmp_path, monkeypatch):
    """A run's point waited for its record's write: on a data folder that stalled (a NAS rebooting), the run sat in
    that write for the whole stall, and a person's Stop with it: the drives it touched stayed where they were. Its
    frames now go to the file on the record's own thread; the run waits a moment at most."""
    from labharness import rundata
    from labharness.adapters import set_resource_manager
    from test_hardware import Bus, Device
    set_resource_manager(Bus({"GPIB1::24::INSTR": Device("keithley/2400")}))
    real = rundata.Recorder.write_frame
    armed, stalled, free = threading.Event(), threading.Event(), threading.Event()

    def write_frame(self, data, sync=False):
        if armed.is_set() and b'{"t": "row"' in data[:64]:
            stalled.set()
            free.wait(60)   # the share doesn't answer
        return real(self, data, sync)
    monkeypatch.setattr(rundata.Recorder, "write_frame", write_frame)
    try:
        rt = runtime(tmp_path, SMU_LAB, answers=(True, True, True))
        smu = rt.lab.instruments["smu"]
        rt.queue_add(IV)
        assert rt.queue_start(by="human").startswith("queue started")
        assert wait_until(lambda: (rt.live_view() or {}).get("done", 0) >= 2, 20)
        armed.set()
        assert stalled.wait(10), "no row was written after the stall began"
        rt.stop("human (portal)")
        assert wait_until(lambda: smu.read("output") is False and smu.read("V") == 0, 8), \
            "the run's drives stayed on while its record's write stalled"
    finally:
        free.set()
        set_resource_manager(None)
    assert wait_until(lambda: rt.queue.state == "paused" and rt.lock.owner()[0] is None, 20)


def test_the_lab_panels_events_never_wait_on_a_stalled_record(tmp_path, monkeypatch):
    """The Lab panel's poll read the record's latest events from its file on the poll's own thread: during a data
    folder's stall, the panel (and one of the portal's threads) waited the whole stall."""
    from labharness.agent import runtime as rtmod
    rt = runtime(tmp_path)
    assert isinstance(rt.ui_state()["events"], list)   # read once, as it is
    real = rtmod.tail_events

    def stalled(path, n):
        time.sleep(STALL_S)
        return real(path, n)
    monkeypatch.setattr(rtmod, "tail_events", stalled)
    for _ in range(3):
        t0 = time.monotonic()
        state = rt.ui_state()
        assert time.monotonic() - t0 < 2.0 and isinstance(state["events"], list)


WEDGE_LAB = """lab: Bench
instruments:
  dmm:
    card: keysight/34465a
    address: GPIB0::27::INSTR
    connection: { timeout_ms: 1000 }
  smu1:
    card: keithley/2400
    address: GPIB0::24::INSTR
    connection: { timeout_ms: 1000 }
  smu2:
    card: keithley/2400
    address: GPIB0::25::INSTR
    connection: { timeout_ms: 1000 }
"""


@pytest.fixture
def wedged_board():
    from labharness.adapters import BUSES, set_resource_manager
    from test_field_test_2026_11_06_bus import HangingBus
    from test_field_test_2026_11_13_hung_queue import Stuck
    stuck = {"dmm": Stuck("keysight/34465a"), "smu1": Stuck("keithley/2400"), "smu2": Stuck("keithley/2400")}
    set_resource_manager(HangingBus({"GPIB0::27::INSTR": stuck["dmm"], "GPIB0::24::INSTR": stuck["smu1"],
                                     "GPIB0::25::INSTR": stuck["smu2"]}))
    yield stuck
    for d in stuck.values():
        d.free.set()
    set_resource_manager(None)
    BUSES.hung.clear()


def test_a_monitor_look_gives_up_on_a_wedged_gpib_adapter_at_its_instruments_timeout(tmp_path, wedged_board):
    """On a GPIB adapter that wedged, the monitor's look waited for its first question the call's whole limit (30 s
    for a 34465a), every watch and interlock waiting: past the instrument's own timeout (and LOOK_GRACE_S) the
    driver would have said it timed out, so the look stops waiting there, and leaves the rest of that bus for the
    look."""
    rt = runtime(tmp_path, WEDGE_LAB)
    m = rt.monitor
    m.tick()   # all answering
    for d in wedged_board.values():
        d.hung.set()
    m._last_error_check.clear()   # the minute's error-queue check is due: it asks each instrument on GPIB0
    t0 = time.monotonic()
    m.tick()
    took = time.monotonic() - t0
    assert took < 8.0, "a look waited %.1f s on the wedged adapter" % took   # its call's limit is 20 s
    assert sum(d.inside.is_set() for d in wedged_board.values()) == 1, "the rest of the bus was asked as well"


def test_a_record_of_the_lab_never_waits_behind_a_call_on_its_gpib_bus(tmp_path, wedged_board):
    """A stopped run's record of the lab looks at every instrument: on a wedged adapter, one whose bus already had a
    call in its driver (the stopped run's own, a Stop left there) was asked anyway, and the queue showed "pausing"
    while the look waited out that call's limit."""
    rt = runtime(tmp_path, WEDGE_LAB)
    for d in wedged_board.values():
        d.hung.set()
    threading.Thread(target=lambda: _quietly(rt.lab.read, "dmm", "V_dc"), daemon=True).start()
    assert wedged_board["dmm"].inside.wait(5)
    time.sleep(1.2)
    t0 = time.monotonic()
    snap = rt.lab.snapshot(needed=())   # every instrument only looked at
    assert time.monotonic() - t0 < 4.0
    assert not wedged_board["smu1"].inside.is_set() and not wedged_board["smu2"].inside.is_set()
    assert "busy" in str(snap["smu1"]), snap["smu1"]


def _quietly(fn, *a):
    try:
        fn(*a)
    except Exception:
        pass


def test_a_reconnect_says_what_is_still_away_from_safe(tmp_path):
    """A to-safe that couldn't reach an instrument while it didn't answer left it as it was (an smu at 0.1 V, its
    output on), and the reconnect that got it back said only that it answers as its card expects."""
    from labharness.adapters import set_resource_manager
    from test_hardware import Bus, Device
    set_resource_manager(Bus({"GPIB1::24::INSTR": Device("keithley/2400")}))
    try:
        rt = runtime(tmp_path, SMU_LAB)
        smu = rt.lab.instruments["smu"]
        smu.write("output", 1)
        smu.write("V", 0.1)
        said = rt.reconnect_instrument("smu", by="human (portal)")
        assert "away from safe" in said and "smu.output" in said, said
        smu.write("V", 0)
        smu.write("output", 0)
        assert "away from safe" not in rt.reconnect_instrument("smu", by="human (portal)")
    finally:
        set_resource_manager(None)


def test_bring_to_safe_never_waits_on_a_run_finishing_its_record_on_a_stalled_share(tmp_path, monkeypatch):
    """A stopped run that finished its record on a data folder that stalled held the lab with its instruments, and
    Bring to safe waited 15 s behind it: finishing a record uses no instrument, so it goes beside at once."""
    from labharness import eventlog
    from labharness.adapters import set_resource_manager
    from test_hardware import Bus, Device
    set_resource_manager(Bus({"GPIB1::24::INSTR": Device("keithley/2400")}))
    real = eventlog.RunRecord.finish
    finishing, free = threading.Event(), threading.Event()

    def stalled_finish(self, **meta):
        finishing.set()
        free.wait(30)   # the share doesn't answer
        return real(self, **meta)
    monkeypatch.setattr(eventlog.RunRecord, "finish", stalled_finish)
    try:
        rt = runtime(tmp_path, SMU_LAB, answers=(True, True, True))
        rt.queue_add(IV)
        assert rt.queue_start(by="human").startswith("queue started")
        assert wait_until(lambda: (rt.live_view() or {}).get("done", 0) >= 2, 20)
        rt.stop("human (portal)")
        assert finishing.wait(15)
        t0 = time.monotonic()
        lines = rt.bring_to_safe("human (portal)")
        assert time.monotonic() - t0 < 5.0, lines
    finally:
        free.set()
        set_resource_manager(None)
    assert wait_until(lambda: rt.queue.state == "paused", 20)


def test_the_lab_panel_never_waits_on_an_instrument_that_hangs(tmp_path, wedged_board):
    """The Lab panel's look read each instrument on the poll's own thread: one whose exchange hung (a wedged GPIB
    adapter) held the panel its call's whole limit, every other instrument's values with it. Each is now looked at on
    a thread of its own; the panel shows the latest values meanwhile."""
    rt = runtime(tmp_path, WEDGE_LAB)
    wedged_board["dmm"].hung.set()
    t0 = time.monotonic()
    rt.ui_state(fresh=["dmm"])   # a person opens the dmm's row: read now
    assert time.monotonic() - t0 < 3.0, "the panel waited on the hung dmm"
    assert wedged_board["dmm"].inside.wait(5)   # its look goes on, on a thread of its own
    t0 = time.monotonic()
    rt.ui_state(fresh=["dmm", "smu1"])   # again: the dmm's look still under way (not one more), smu1 read now
    assert time.monotonic() - t0 < 3.0
    assert wait_until(lambda: rt.ui_state()["status"].get("smu1"), 5), "the others' values waited for the dmm"


def test_adding_a_scan_never_waits_on_an_instrument_that_hangs(tmp_path, wedged_board):
    """Adding a scan to the queue checks it, reading a source's present range: with that source's exchange hung
    (a wedged GPIB adapter), the add waited the call's whole limit. The range is read aside now, a second at most,
    and left unknown past it (the queue's start checks it again)."""
    rt = runtime(tmp_path, WEDGE_LAB)
    wedged_board["smu1"].hung.set()
    t0 = time.monotonic()
    said = rt.queue_add("plan: iv\nsweep: { smu1.V: {from: 0, to: 0.1, step: 0.05} }\nmeasure: [smu1.I]\n",
                        by="human")
    assert time.monotonic() - t0 < 3.0, said
    assert said.startswith("added"), said


def test_what_is_left_on_never_asks_a_wedged_adapter_drive_by_drive(tmp_path, wedged_board):
    """The report of what is left on (a Stop's, stop_run's, Bring to safe's last line) looked at each drive of each
    instrument: on a wedged GPIB adapter each drive waited a look's give-up, or behind the call before it, a minute
    and more in all. An instrument that doesn't answer in the look isn't asked its other drives, nor is the rest of
    its bus."""
    rt = runtime(tmp_path, WEDGE_LAB)
    for d in wedged_board.values():
        d.hung.set()
    t0 = time.monotonic()
    left = rt.left_on(look=True)
    assert time.monotonic() - t0 < 8.0, left
    assert any("couldn't be read" in x for x in left), left


def test_a_frame_that_fails_after_its_point_moved_on_is_said_at_the_finish(tmp_path, monkeypatch):
    """A point's frame that failed after the point had moved on (a share's hiccup) was said at the record's next write,
    which was the end snapshot's, whose error a run lets go of: the run said every point was kept, run.h5 one short.
    Its finish says it now (the run's data unwritten: an alarm, and not called saved)."""
    from labharness import rundata
    from labharness.eventlog import RunRecord
    real = rundata.Recorder.write_frame

    def write_frame(self, data, sync=False):
        if b'{"t": "row"' in data[:64] and b'"i": 1, ' in data:   # the second point's frame
            time.sleep(0.7)   # past RECORD_WAIT_S: the point has moved on
            raise OSError(5, "The specified network name is no longer available")
        return real(self, data, sync)
    monkeypatch.setattr(rundata.Recorder, "write_frame", write_frame)
    rec = RunRecord("20261114-120000-t", tmp_path / "20261114-120000-t", {"plan": "t"}, "test")
    rec.snapshot("start", {})
    rec.add({"i": 0, "t": 0.0, "x": 1.0})
    rec.add({"i": 1, "t": 0.1, "x": 2.0})
    try:
        rec.snapshot("end", {})   # as the engine does: an end snapshot that can't be written is let go of
    except OSError:
        pass
    with pytest.raises(OSError, match="could not be written"):
        rec.finish(status="done")


def test_the_restart_card_goes_up_when_the_runs_catalog_cant_be_opened(tmp_path, monkeypatch):
    """The restart's check tidied the runs, then synced the catalog, then put up its card: a catalog that couldn't be
    opened (locked, its share gone) ended every check before the card, and the runs were walked every 5 s."""
    import sqlite3
    from labharness import catalog as catalog_mod
    from labharness.adapters import set_resource_manager
    from test_hardware import Bus, Device
    dev = Device("keithley/2400")
    set_resource_manager(Bus({"GPIB1::24::INSTR": dev}))
    try:
        rt = runtime(tmp_path, SMU_LAB)
        dev.twin.write("OUTP ON")
        dev.twin.write(":SOUR:VOLT 0.5")

        def cant_open(self):
            raise sqlite3.OperationalError("unable to open database file")
        monkeypatch.setattr(catalog_mod.Catalog, "_connect", cant_open)
        rt._incident_due, rt._tidied = True, False
        rt._incident_check()
        assert rt.incident is not None and not rt._incident_due
    finally:
        set_resource_manager(None)


def test_the_sequences_look_for_a_script_never_piles_up_threads(tmp_path, monkeypatch):
    """While the scripts folder's share stalled, each poll of the Sequence started another look for each script item
    (thousands of threads over a NAS's reboot): one look for each at a time now."""
    from labharness import scriptrun
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for n in ("a.py", "b.py", "c.py"):
        (scripts / n).write_text("print('hello')\n", encoding="utf-8")
    rt = runtime(tmp_path)
    rt.scripts_folders = lambda: [str(scripts)]
    for n in ("a.py", "b.py", "c.py"):
        assert not rt.queue_add("script: %s" % n).startswith("not added")
    rt2 = runtime(tmp_path)   # a restart: nothing found yet
    rt2.scripts_folders = lambda: [str(scripts)]
    free = threading.Event()
    real = scriptrun.locate

    def stalled(*a, **k):
        free.wait(60)
        return real(*a, **k)
    monkeypatch.setattr(scriptrun, "locate", stalled)

    def looks():
        return sum(1 for t in threading.enumerate() if t.name == "kelvin-sequence-look")
    base = looks()
    try:
        most = 0
        for _ in range(8):
            rt2.queue_view()
            most = max(most, looks() - base)
            time.sleep(0.2)
        assert most <= 3, "%d looks for 3 script items" % most
    finally:
        free.set()


def test_a_queue_that_cant_be_saved_is_said_and_clears_once_it_is(tmp_path, monkeypatch):
    """queue.json that couldn't be written (a folder denied, a share gone) was let go of in silence: the queue went on
    in memory, and a restart started without it. An alarm says so now, and clears once it is saved again."""
    import os
    from labharness import scanqueue
    rt = runtime(tmp_path)
    real = os.replace
    denied = {"on": True}

    def replace(src, dst, *a, **k):
        if denied["on"] and str(dst).endswith("queue.json"):
            raise PermissionError(13, "Access is denied", str(dst))
        return real(src, dst, *a, **k)
    monkeypatch.setattr(os, "replace", replace)
    rt.queue_add(PLAN, by="human")
    assert wait_until(lambda: scanqueue.save_trouble([rt.data_dir]) is not None, 5)
    rt.monitor.tick()
    up = [a for a in rt.monitor.alarms if "can't save" in a.text and a.cleared is None]
    assert up, [a.text for a in rt.monitor.alarms]
    denied["on"] = False
    rt.queue_add(PLAN, by="human")
    assert wait_until(lambda: scanqueue.save_trouble([rt.data_dir]) is None, 5)
    rt.monitor.tick()
    assert up[0].cleared is not None


def test_a_reconnect_never_keeps_the_connection_it_replaced(tmp_path):
    """The lab's factory kept every connection it ever opened (for a failed start's clean-up), for the lab's whole
    life: each reconnect left one more session, courier and thread object behind."""
    import gc
    from labharness.adapters import VisaBackend, set_resource_manager
    from labharness.instrument import Unavailable
    from test_hardware import Bus, Device
    set_resource_manager(Bus({"GPIB1::24::INSTR": Device("keithley/2400")}))
    try:
        rt = runtime(tmp_path, SMU_LAB)
        inst = rt.lab.instruments["smu"]

        def backends():
            gc.collect()
            return sum(1 for o in gc.get_objects() if isinstance(o, VisaBackend))
        before = backends()
        for _ in range(10):
            try:
                inst.backend.close()
            except Exception:
                pass
            inst.backend = Unavailable("smu", "left out after its bus hung")
            assert "connected" in rt.reconnect_instrument("smu", by="human (portal)")
        assert backends() - before <= 2
    finally:
        set_resource_manager(None)


def test_bring_to_safe_never_waits_a_whole_call_on_a_bus_another_call_wedged(tmp_path, wedged_board):
    """Bring to safe went to safe on each instrument of a GPIB bus whose adapter had wedged (another instrument's
    call past its time in the driver) as if the bus were well: each to-safe waited its call's whole limit, and the
    button returned 20 s late. Each gives up as a look does now (its timeout and 2 s), and says so."""
    rt = runtime(tmp_path, WEDGE_LAB)
    for n in ("smu1", "smu2"):
        rt.lab.instruments[n].write("output", 1)
        rt.lab.instruments[n].write("V", 0.5)
    for d in wedged_board.values():
        d.hung.set()
    threading.Thread(target=lambda: _quietly(rt.lab.read, "dmm", "V_dc"), daemon=True).start()
    assert wedged_board["dmm"].inside.wait(5)
    time.sleep(3.5)   # the dmm's call is past its timeout and 2 s: the adapter has wedged
    t0 = time.monotonic()
    lines = rt.bring_to_safe("human (portal)")
    assert time.monotonic() - t0 < 10.0, lines   # its calls' limit is 20 s
    assert any("smu1" in line for line in lines), lines


OPENS_ANOTHER = """
import time
import pyvisa
rm = pyvisa.ResourceManager()
time.sleep(0.5)
dev = rm.open_resource("GPIB0::%d::INSTR" % (2 + 3))   # an address worked out as it runs: asked when it opens
print(dev.query("*IDN?"))
"""


def test_the_runs_stop_declines_the_stopped_scripts_own_card(lab, folder, tmp_path):
    """The Runs pane's Stop (stop_run) only counted a stop: a script waiting for its own card ("Let other.py open
    GPIB0::5?") went on waiting for it, the run and the lab held until a person answered or the card timed out. It
    declines the stopped run's cards now, as Skip does."""
    write(folder, "other.py", OPENS_ANOTHER)
    rt = script_runtime(lab, folder, tmp_path, timeout=60)
    out = {}

    def person():   # yes to the run's card; the next one (the open) is left to think about
        first = rt.approver.wait_for_pending(timeout=60)
        rt.approver.answer(first["id"], True)
    threading.Thread(target=person, daemon=True).start()
    th = threading.Thread(target=lambda: out.update(said=rt.run_script("other.py")), daemon=True)
    th.start()
    assert wait_until(lambda: any("open" in c["title"] for c in rt.approver.pending_list()), 60)
    rt.stop_run("human (portal)")
    try:
        assert wait_until(lambda: not rt.approver.pending_list(), 3), rt.approver.pending_list()
        th.join(15)
        assert not th.is_alive(), "the stopped run went on"
    finally:
        rt.stop("test")
        th.join(30)


def _web_runtime(tmp_path, text, approver):
    from labharness.agent.runtime import LabRuntime
    from labharness.homelab import build_lab
    (tmp_path / "lab.yaml").write_text(text, encoding="utf-8")
    rt = LabRuntime(build_lab(tmp_path / "lab.yaml"), approver, tmp_path / "data")
    rt.home, rt.cards_dir, rt.scan_network = tmp_path, tmp_path / "cards", False
    return rt


def test_a_card_prepared_as_a_stop_lands_is_never_shown_after_it(tmp_path):
    """A Stop declined the cards open, then counted as a stop: work that had passed its check before the count and
    added its card after the declines showed it after the Stop, holding the lab its whole timeout. The count goes
    first now, and the approver looks at it as it adds a card, under the lock the declines take."""
    pytest.importorskip("mcp")
    from labharness.adapters import set_resource_manager
    from labharness.agent.approval import WebApprover
    from test_hardware import Bus, Device
    set_resource_manager(Bus({"GPIB1::24::INSTR": Device("keithley/2400")}))
    approver = WebApprover(timeout=60)
    try:
        rt = _web_runtime(tmp_path, SMU_LAB, approver)
        entered, go = threading.Event(), threading.Event()

        def where(request):   # the card being prepared (its notes and lessons), past the gate's own check
            entered.set()
            go.wait(10)
            return None
        approver.where = where
        out = {}
        w = threading.Thread(target=lambda: out.update(said=rt.set("smu", "V", 0.5)), daemon=True)
        w.start()
        assert entered.wait(10), out
        rt.stop("human (portal)")
        go.set()
        time.sleep(1.0)
        assert not approver.pending_list(), "a card shown after the person's Stop: %s" % approver.pending_list()
        w.join(10)
    finally:
        approver.decline_all("test")
        set_resource_manager(None)


def test_the_lab_panels_poll_never_waits_on_a_stamp_of_a_stalled_data_folder(tmp_path, monkeypatch):
    """Each poll of /api/state read Home's stamp (home.json in the data folder) on one of the server's shared
    threads, with no end: on a stalled share every poll held one, thirty viewers held all twelve, and a person's
    queue edits waited behind them. A stamp is read on a thread of its own now, one at a time, waited for half a
    second, then the last one is sent."""
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    rt = runtime(tmp_path)
    settings = Settings(tmp_path / "Kelvin" / "settings.json")
    client = TestClient(create_app(rt, settings, ChatStore(tmp_path / "Kelvin" / "chats"), hosts=["testserver"],
                                   providers={}))
    h = {"X-Kelvin-Token": settings.access_key}
    real_stat = pathlib.Path.stat
    armed = threading.Event()

    def stat(self, *a, **k):
        if armed.is_set() and self.name == "home.json":
            time.sleep(STALL_S)
        return real_stat(self, *a, **k)
    monkeypatch.setattr(pathlib.Path, "stat", stat)
    with client:
        assert client.get("/api/state", headers=h).status_code == 200
        armed.set()
        for _ in range(4):
            t0 = time.monotonic()
            r = client.get("/api/state", headers=h)
            assert r.status_code == 200 and time.monotonic() - t0 < 2.0
        armed.clear()


def test_bring_to_safe_never_waits_on_a_stopped_runs_last_look(tmp_path, monkeypatch):
    """A stopped run's last look at the lab (its record's end) was taken with its instruments held: Bring to safe
    right after the Stop waited behind it (15 s), a slow instrument's exchanges one after another. It holds none of
    them now, and Bring to safe goes beside it at once."""
    from labharness.adapters import set_resource_manager
    from labharness.lab import Lab
    from test_hardware import Bus, Device
    set_resource_manager(Bus({"GPIB1::24::INSTR": Device("keithley/2400")}))
    real = Lab.snapshot
    looking, free = threading.Event(), threading.Event()

    def slow(self, names=None, settings=True, needed=None):
        if needed is not None and not list(needed):   # a stopped run's last look: every instrument only looked at
            looking.set()
            free.wait(20)
        return real(self, names, settings, needed)
    monkeypatch.setattr(Lab, "snapshot", slow)
    try:
        rt = runtime(tmp_path, SMU_LAB, answers=(True, True, True))
        rt.queue_add(IV)
        assert rt.queue_start(by="human").startswith("queue started")
        assert wait_until(lambda: (rt.live_view() or {}).get("done", 0) >= 2, 20)
        rt.stop("human (portal)")
        assert looking.wait(10)
        t0 = time.monotonic()
        lines = rt.bring_to_safe("human (portal)")
        assert time.monotonic() - t0 < 5.0, lines
    finally:
        free.set()
        set_resource_manager(None)


def test_a_record_of_the_lab_stops_reading_an_instrument_at_a_stop(tmp_path):
    """A run's look at the lab (its record as it ends) looked for a Stop only between instruments, and an exchange
    under way ended at one: a Stop between two of an instrument's readings had the rest go out all the same, a slow
    instrument's exchanges one after another beside Bring to safe's own steps. The rest is left at a Stop now."""
    from labharness.adapters import set_resource_manager
    from test_hardware import Bus, Device
    set_resource_manager(Bus({"GPIB1::24::INSTR": Device("keithley/2400")}))
    try:
        rt = runtime(tmp_path, SMU_LAB)
        inst = rt.lab.instruments["smu"]
        real, read = inst.read, []

        def reading(name, *a, **k):
            value = real(name, *a, **k)
            read.append(name)
            if len(read) == 1:
                rt.lab.stops.pulse()   # a Stop (Bring to safe's own) lands between two of its readings
            return value
        inst.read = reading
        snap = rt.lab.snapshot(needed=())
        assert len(read) == 1, "read on after the Stop: %s" % read
        assert any("pressed Stop" in str(v) for v in snap["smu"].values()), snap
    finally:
        set_resource_manager(None)


def test_stop_run_never_waits_long_on_a_look_at_what_is_still_on(tmp_path):
    """stop_run (the agent's Stop) said what is still on after looking at each instrument: an smu whose exchange hung
    held the answer its look's whole give-up (12 s), though the stop itself went out at once. It waits a moment now,
    and the report after the stop says the rest."""
    from labharness.adapters import BUSES, set_resource_manager
    from test_field_test_2026_11_06_bus import HangingBus
    from test_field_test_2026_11_13_hung_queue import Stuck
    smu = Stuck("keithley/2400")
    set_resource_manager(HangingBus({"GPIB1::24::INSTR": smu}))
    try:
        rt = runtime(tmp_path, SMU_LAB)
        rt.lab.instruments["smu"].write("output", 1)
        rt._after_stop = lambda *a, **k: None   # the report after the stop (which would look first) left out here
        smu.hung.set()
        t0 = time.monotonic()
        said = rt.stop_work(by="agent")
        assert time.monotonic() - t0 < 4.0, said
        assert "being looked at" in said or "still on" in said, said
    finally:
        smu.free.set()
        set_resource_manager(None)
        BUSES.hung.clear()


def test_a_look_that_gave_up_on_an_instrument_leaves_it_for_the_rest_of_the_look(tmp_path):
    """A monitor's look gave up on an smu slower than its own timeout (its driver past its time), and then gave up on
    it again for each of its other readings: one look took three give-ups. Once it gave up, the rest of that look
    leaves it."""
    from labharness.adapters import set_resource_manager
    from labharness.instrument import Busy, patience
    from test_field_test_2026_11_13_hung_queue import Slow
    from test_hardware import Bus
    smu = Slow("keithley/2400")
    set_resource_manager(Bus({"GPIB1::24::INSTR": smu}))
    try:
        rt = runtime(tmp_path, SMU_LAB.replace("address: GPIB1::24::INSTR",
                                               "address: GPIB1::24::INSTR\n    connection: { timeout_ms: 1000 }"))
        inst = rt.lab.instruments["smu"]
        smu.slow = 3.3   # past its look's give-up (1 s timeout and 2 s), not its call's limit
        t0 = time.monotonic()
        with patience(1.0):
            for param in ("V", "I_source", "compliance_I"):
                with pytest.raises(Busy):
                    inst.read(param)
        assert time.monotonic() - t0 < 4.5
    finally:
        smu.slow = 0.0
        set_resource_manager(None)
