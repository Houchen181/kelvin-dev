"""The lab's record and a script run's files never hold Kelvin (a review of the hung-bus fixes, 2026-11-13).

A data folder on a network share that stalls (a NAS rebooting, a VPN dropping) blocks a file call for tens of seconds,
and every gate command writes a line of the record: each one, a run's steps, a ramp to safe and the monitor with them,
waited out that call. A full disk or a gone share during a script run froze the script in print() (nobody read its
pipe any more), or skipped its way back to safe. A script whose main code ended but left a thread running never
ended, and its run with it."""
import importlib.util
import io
import socket
import threading
import time
import types
from pathlib import Path

import pytest

from labharness import eventlog, rundata, scriptrun
from test_script_runs import _python, folder, lab, run, write  # noqa: F401 (fixtures)


# ---------------------------------------------------------------------- the lab's record

def test_a_write_never_waits_out_a_record_file_that_stalls(tmp_path, monkeypatch):
    """Each write waits a moment at most for its line; once a file call has stalled, the next writes don't wait for it
    at all. Their lines are read back meanwhile, the record's trouble says the file doesn't answer (the monitor's
    alarm), and once it answers every line is in the file, in order, its chain whole."""
    monkeypatch.setattr(eventlog, "STALL_S", 0.5)
    real, free, inside = eventlog._chained, threading.Event(), threading.Event()

    def stalls(path, held, tail, *records):
        if records and records[0].get("type") == "two" and not free.is_set():
            inside.set()
            free.wait(20)                    # a network share that doesn't answer
        return real(path, held, tail, *records)
    monkeypatch.setattr(eventlog, "_chained", stalls)
    path = tmp_path / "events.jsonl"
    log = eventlog.EventLog(path)
    try:
        log.write("one")
        t0 = time.monotonic()
        log.write("two")                     # its file call stalls
        assert time.monotonic() - t0 < 1.5
        assert inside.wait(5)
        t0 = time.monotonic()
        for k in range(20):                  # a run's steps meanwhile: none waits on it
            log.write("more", k=k)
        assert time.monotonic() - t0 < 1.0
        time.sleep(0.6)
        assert "doesn't answer" in (log.trouble or "")
        assert [e["type"] for e in log.events()] == ["one", "two"] + ["more"] * 20   # read back meanwhile
    finally:
        free.set()
    assert log.settle(5) and log.trouble is None
    assert [e["type"] for e in eventlog.EventLog(path).events()] == ["one", "two"] + ["more"] * 20
    v = eventlog.verify_chain(path)
    assert v["lines"] == v["chained"] == 22 and v["breaks"] == []


def test_a_flush_and_a_runs_open_record_never_wait_out_a_stalled_file(tmp_path, monkeypatch):
    """The monitor's look asks the record to write what it kept back (flush), and a run keeps the record's files open
    while it measures (holding): neither waits on a file call that doesn't come back."""
    real, free, inside = eventlog._chained, threading.Event(), threading.Event()

    def stalls(path, held, tail, *records):
        if not free.is_set():
            inside.set()
            free.wait(20)
        return real(path, held, tail, *records)
    log = eventlog.EventLog(tmp_path / "events.jsonl")
    monkeypatch.setattr(eventlog, "_chained", stalls)
    try:
        threading.Thread(target=log.write, args=("stuck",), daemon=True).start()
        assert inside.wait(5)
        time.sleep(0.6)
        t0 = time.monotonic()
        log.flush()
        with log.holding():
            log.write("in a run")
        assert time.monotonic() - t0 < 1.0
    finally:
        free.set()
    assert log.settle(5)
    assert [e["type"] for e in eventlog.EventLog(tmp_path / "events.jsonl").events()] == ["stuck", "in a run"]


# ---------------------------------------------------------------------- a script run's own files

def test_a_scripts_output_is_read_to_its_end_whatever_its_file_says(tmp_path):
    """output.txt that can't be written any more (a full disk, its drive gone) left the script's pipe unread: the
    script froze in its next print(), and its run with it. The pipe is read to its end, and what couldn't be kept said."""
    out = scriptrun._Output(tmp_path / "output.txt")
    real = out.file

    class Full:
        closed = False

        def __init__(self):
            self.n = 0

        def write(self, data):
            self.n += 1
            if self.n > 2:
                raise OSError(28, "No space left on device")
            return len(data)

        def flush(self):
            pass

        def close(self):
            self.closed = True
    out.file = Full()
    data = b"x" * (65536 * 5)
    stream = io.BufferedReader(io.BytesIO(data))
    out._copy(stream)                        # in a run, on the pipe's own thread
    assert stream.read() == b""              # all of it read: the script's pipe never fills
    assert out.kept + out.left == len(data) and out.left > 0
    out.close(0)
    assert "No space left on device" in out.lost()
    real.close()


def test_a_record_that_fills_up_stops_the_script_and_its_drives_still_go_back_to_safe(lab, folder, tmp_path,
                                                                                         monkeypatch):
    """A row its record couldn't take raised: the script got errors for commands that had reached the instrument, and
    at the run's end the error escaped before the way back to safe: its source stayed at 2 V, its output on. Now the
    run stops, as a plan's does, says why, and every drive it touched goes back to safe."""
    monkeypatch.setattr(scriptrun, "GRACE_S", 1.0)
    write(folder, "drive.py", """
        import time, pyvisa
        rm = pyvisa.ResourceManager()
        smu = rm.open_resource("GPIB0::24::INSTR")
        lockin = rm.open_resource("GPIB::8")
        smu.write("OUTP ON")
        smu.write("SOUR:VOLT 2")
        while True:
            lockin.query("MEAS:X?")   # a reading: kept until the next one makes it a row
            time.sleep(0.1)
    """)
    real = rundata.Recorder.write_frame   # each frame reaches the file here (on the record's own thread)
    full = threading.Event()

    def write_(self, data, sync=False):
        if full.is_set() and b'{"t": "row"' in data[:64]:
            raise OSError(28, "No space left on device")
        return real(self, data, sync)
    monkeypatch.setattr(rundata.Recorder, "write_frame", write_)
    threading.Timer(1.5, full.set).start()   # the disk fills while it runs
    gate, _, report = run(lab, folder, tmp_path, "drive.py", stop_after=15.0)   # (the Stop: only if all else fails)
    smu = lab.instrument("smu")
    assert report.status == "aborted" and "could not be written" in report.message, report.message
    assert smu.read("V") == 0 and smu.read("output") is False


def test_a_script_whose_main_code_ended_but_left_a_thread_running_ends(lab, folder, tmp_path, monkeypatch):
    """A thread the script started that isn't a daemon kept its process going after its main code ended: the run
    never ended, nor a queue behind it. GRACE_S after its main code ends, it is stopped as one that stops is, and the
    run is done (its own work ended well), saying so."""
    monkeypatch.setattr(scriptrun, "GRACE_S", 1.0)
    write(folder, "leaves_a_thread.py", """
        import threading, time
        def forever():
            while True:
                time.sleep(0.2)
        threading.Thread(target=forever).start()   # not a daemon
        print("main done")
    """)
    t0 = time.monotonic()
    _, _, report = run(lab, folder, tmp_path, "leaves_a_thread.py", stop_after=30.0)
    assert time.monotonic() - t0 < 20.0
    assert report.status == "done", report.message
    assert any("its main script ended" in w for w in report.warnings), report.warnings


def test_a_scripts_long_timeout_goes_to_the_session_the_connection_has_once_it_is_the_runs():
    """The script's own longer timeout was read for the session before its connection was the run's: a reconnect in
    between (the session made anew) sent it to the one set aside, and the script's query then timed out at the card's
    timeout."""
    class Res:
        def __init__(self):
            self.timeout = 1000
    old, new = Res(), Res()
    lock = threading.RLock()
    backend = types.SimpleNamespace(lock=lock, res=old)

    def wait_for(held):                      # a reconnect made the session anew while the run waited
        backend.res = new
        held.acquire()
    inst = types.SimpleNamespace(backend=backend, _wait_for=wait_for)
    with scriptrun.ScriptRun._patience(object(), inst, 5000.0):
        assert new.timeout == 5000.0 and old.timeout == 1000
    assert new.timeout == 1000


# ---------------------------------------------------------------------- the script's stand-in

def _shim():
    path = Path(scriptrun.__file__).parent / "data" / "scriptshim" / "kelvin_shim.py"
    spec = importlib.util.spec_from_file_location("kelvin_shim_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_scripts_stand_in_never_waits_for_ever_to_reach_kelvin(monkeypatch):
    """Endpoint security that drops this computer's own connections left the script hanging before it ran anything,
    with nothing to say why: the stand-in's connection to Kelvin is waited for CONNECT_S, then said."""
    shim = _shim()
    monkeypatch.setattr(shim, "CONNECT_S", 0.3)
    seen = {}

    def dropped(sock, address):              # nothing ever answers: as long as the socket lets it wait
        seen["timeout"] = sock.gettimeout()
        time.sleep(sock.gettimeout() or 0)
        raise socket.timeout("timed out")
    monkeypatch.setitem(shim._REAL, "connect", dropped)
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="could not reach Kelvin"):
        shim._Link("127.0.0.1:9", "token")
    assert seen["timeout"] == 0.3 and time.monotonic() - t0 < 3.0
