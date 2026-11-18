"""Real instrument backends: VISA (GPIB, USB-TMC, LAN, RS-232) through PyVISA, set up from the card.

A backend has the same three methods as a twin: write(command), query(command) -> str, close(). Each holds its
own lock, so the agent, a plan and the lab panel never interleave on one instrument. Like everything below the
safety gate, a backend has no safety checks of its own; only the gate and the run engine call it.

Addresses in a lab config:
    sim://name                                  a twin (simulated), see sim/twin.py
    GPIB0::12::INSTR                            any VISA resource string, as NI MAX or `kelvin-scan` shows it
    USB0::0x05E6::0x2450::04412345::INSTR       USB-TMC
    TCPIP0::192.168.1.20::inst0::INSTR          LAN (VXI-11); TCPIP0::host::5025::SOCKET for raw sockets
    ASRL3::INSTR  or  serial://COM3             RS-232 / USB-serial, with the card's baud rate and framing
    prologix://192.168.1.50/12                  GPIB address 12 behind a Prologix GPIB-ETHERNET; prologix://COM5/12
                                                behind a GPIB-USB (prologix.py: Kelvin speaks to the adapter itself)

PyVISA uses the installed VISA (NI-VISA, Keysight IO Libraries) when there is one, else its pure-Python backend
(pyvisa-py, with pyserial for serial ports). Set KELVIN_VISA_BACKEND (e.g. "@py") to choose.
"""
from __future__ import annotations

import errno
import logging
import os
import re
import sys
import threading
import time
import warnings
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .cards import Card, parity_name, serial_settings

log = logging.getLogger("kelvin")
_rm: Any = None
_rm_own = False   # whether Kelvin made _rm itself
VISA_NOTE = ""    # why the VISA installed on this computer isn't the one Kelvin uses, when it is there and didn't load
_rm_lock = threading.Lock()
# PyVISA warns of a reply that ended at the bus's end-of-message mark (GPIB's EOI) without the line end: whole all the
# same, and a field test's log had six such warnings in an afternoon
warnings.filterwarnings("ignore", message="read string doesn't end with termination characters")
WAIT_SLICE_MS = 1000   # a long wait on an instrument is read a second at a time, so a stop ends it within one
SYNC_MS = 2000         # after a timeout: how long its identity, asked to check it is back in step, may take
REOPEN_MS = 5000       # how long making a session that died anew may wait for the instrument (pyvisa's open_timeout)
AFTER_MS = 1000        # and its error queue, asked right after a query it didn't answer
RESYNC_S = 10.0        # after a bus's clear: how long an instrument's check waits for its connection (resync)
# every call into a VISA driver is made on its session's own thread (Courier) and waited for at most this long beyond
# twice the session's timeout: a call that never returns (a wedged GPIB-USB adapter's) can't hold the instrument, the
# run waiting on it, or the lab, past that. Its session is set aside and the instrument connected anew
CALL_GRACE_S = 10.0
CALL_FLOOR_S = 20.0    # the least any call is waited for (a 3 s timeout's call: 20 s)
LOOK_GRACE_S = 2.0     # a look waits for its own call the session's timeout and this long (Session.look_give_up), and
LOOK_FLOOR_S = 3.0     # this long at least: past it, the driver should have answered or said it timed out
OPEN_CALL_S = 30.0     # opening a session (on top of its own open_timeout), and asking or setting one of its attributes
CLOSE_CALL_S = 10.0    # closing one
STOP_SLICE_S = 0.2     # how often a caller waiting on its call looks for a person's Stop
POST_WAIT_S = 0.2      # how long setting a session's timeout waits for it to be in place (Session.__setattr__)
# a transfer whose length its timeout doesn't bound (a scope's deep-memory waveform, a trace read to its end: the driver
# reads it a chunk at a time, each within the timeout) is waited for this long: a 20 MB waveform over GPIB takes 20 s
BULK_CALL_S = 600.0


class AdapterError(RuntimeError):
    pass


class ErrorAnswer(AdapterError):
    """The instrument answered this request with an error of its own for it (a controller's error number, a JSON-RPC
    error): it is there and answering, so it is never taken for a lost instrument, whatever its words say ("sensor not
    connected", "no response from sensor" are a sensor's state, not the link's: lost_kind, session_dead and
    timed_out never read its text). A reading answered so is the one reading a run that doesn't need it may set aside
    (Lab.health_check)."""


class LinkLost(AdapterError, ConnectionError):
    """A maker's link to an instrument broke: its socket closed, its port went away, its program or helper ended.
    lost_kind calls it gone. A driver raises it with the error it came from as its cause, and its link is made anew
    rather than handed back dead (review of 0.3.45, 4.2 and 4.4)."""


class NoAnswer(AdapterError, TimeoutError):
    """An instrument reached through a maker's link didn't answer in time. lost_kind calls it silent. A driver raises
    it, and a reply that may still come late is never taken for the next command's (review of 0.3.45, 4.4 and 4.5)."""


class Hung(LinkLost):
    """A call into an instrument's driver that didn't return in its time (Courier): its session is set aside (broken),
    the call is left where it is (Python can't end it; it holds nothing of Kelvin's), and the instrument is connected
    anew to go on. lost_kind calls it gone."""


class OutOfStep(AdapterError):
    """An instrument's replies came out of step with its questions: its own identity was read where another question's
    reply should be (a late reply to an earlier *IDN?), and that question's own reply didn't follow it
    (VisaBackend._past_a_late_one). The instrument is put back in step. asked_again: the question, a plain reading,
    was asked once more and its identity came again; otherwise it wasn't asked again, as it may change something (an
    action, the error queue, a buffer). It answers, so it is never taken for a lost instrument (lost_kind: none): the
    instrument says it as a comms fault in those words (Instrument._io), not a loose cable or noise."""

    def __init__(self, text: str, command: str = "", reply: str = "", asked_again: bool = False):
        super().__init__(text)
        self.command, self.reply, self.asked_again = command, reply, asked_again


def only_numbers(text: str) -> bool:
    """Whether a reply is numbers only (a reading, a list of them), which no instrument's identity is."""
    fields = [f for f in re.split(r"[\s,;]+", str(text).strip()) if f]
    if not fields:
        return False
    try:
        for f in fields:
            float(f)
    except ValueError:
        return False
    return True


def an_identity(text: Any, pattern: Optional[str], said: str = "") -> bool:
    """Whether a reply is an instrument's own identity: exactly what it said as itself when last asked, where that is
    known (said); else its card's identity pattern at the reply's start, the reply in an identity's form (letters and a
    comma) and not numbers. A lab card's pattern found anywhere in a reply (idn: "2700" in '+1.27001E+00VDC,...') made
    a genuine reading a late identity, thrown away and asked again (review of 2026-11-15)."""
    text = str(text).strip()
    if not text or only_numbers(text):
        return False
    if said:
        return text == str(said).strip()
    return bool(pattern) and re.match(str(pattern), text, re.I) is not None and "," in text \
        and re.search(r"[A-Za-z]", text) is not None


_waits = threading.local()   # this thread's own stop and patience, while it waits on a call (stoppable)
_running = threading.local()   # on a courier's own thread: the call it makes now (time_left)
_reading = threading.local()   # this thread's query reads a card's plain value: how a reply of it reads (reading)


def reading(reads: Callable[[str], bool]) -> Any:
    """While this thread asks for a card's plain reading (Instrument.read: the parameter's own get, no acquisition, no
    trace; a with block): a reply that reads as that value (reads(reply)) is never taken for a late identity, and
    after a late reply in its place it may be asked once more, which only reads again (VisaBackend._past_a_late_one).
    Any other question (an action's, the error queue's, a raw one a person approved, a buffer's read) is never asked
    twice."""
    import contextlib

    @contextlib.contextmanager
    def plain() -> Any:
        before = getattr(_reading, "now", None)
        _reading.now = reads
        try:
            yield
        finally:
            _reading.now = before
    return plain()


def time_left() -> Optional[float]:
    """On a courier's own thread, how long the call it makes now has left before its caller takes it for one that
    won't return (its limit, less what it has taken); None anywhere else. Code inside such a call that waits for
    something of its own (a Prologix adapter's turn behind another instrument's long read) waits no longer than
    that, and says it was busy, rather than be taken for a hung driver."""
    job = getattr(_running, "job", None)
    if job is None or job.began is None:
        return None
    return job.limit - (time.monotonic() - job.began)


def looking() -> bool:
    """Whether this thread's wait on a call is a look's (stoppable with patience: the monitor's)."""
    now = getattr(_waits, "now", None)
    return now is not None and now[1] is not None


def a_look() -> bool:
    """Whether this thread's call is a look's own (instrument.patience: the monitor's, a run's record of the
    instruments it doesn't use), not a way to safe that only waits behind earlier calls a while (HELD_S)."""
    now = getattr(_waits, "now", None)
    return bool(now is not None and len(now) > 2 and now[2])


def this_wait() -> Tuple[Optional[Callable[[], bool]], Optional[float], bool]:
    """This thread's stop, patience and whether its call is a look's own (stoppable), while it waits on a call;
    (None, None, False) outside one. For a connection that waits for its driver's answer itself, not through a Courier:
    a maker's library in its helper process (library.Bridge)."""
    now = getattr(_waits, "now", None)
    if now is None:
        return None, None, False
    return now[0], now[1], bool(len(now) > 2 and now[2])


def stoppable(stopped: Callable[[], bool], patience: Optional[float] = None, look: bool = False) -> Any:
    """While this thread waits on a call into a driver (a with block): stopped() ends its wait, a person's Stop
    coming back at once (instrument.Stopped), and patience (a look's: the monitor's, a run's record of the
    instruments it doesn't use) is the most it waits behind the session's earlier calls (instrument.Busy). A call it
    stops waiting for that hasn't started is taken back; one under way goes on on its session's thread, its late reply
    dropped before the next command (owed). Instrument._io gives it the lab's Stops and this thread's own. look: a
    look's own call (a_look), which a VISA session waits for only as long as its timeout says (Session.look_give_up)."""
    import contextlib

    @contextlib.contextmanager
    def waiting() -> Any:
        before = getattr(_waits, "now", None)
        _waits.now = (stopped, patience, look)
        try:
            yield
        finally:
            _waits.now = before
    return waiting()


class _Job:
    __slots__ = ("fn", "args", "kwargs", "limit", "what", "box", "done", "taken_back", "began", "lock")

    def __init__(self, fn: Callable[..., Any], args: Tuple[Any, ...], kwargs: Dict[str, Any], limit: float,
                 what: str):
        self.fn, self.args, self.kwargs, self.limit, self.what = fn, args, kwargs, limit, what
        self.box: Dict[str, Any] = {}
        self.done = threading.Event()
        self.taken_back = False          # its caller stopped waiting before it began: it never runs
        self.began: Optional[float] = None
        self.lock = threading.Lock()     # taken back, or begun: one or the other, never both


class Courier:
    """One session's calls into its driver, made one after another on a thread of its own, each waited for by the
    thread that asked. A call that runs past its own limit leaves this thread to it (dead): the session it served is
    set aside, and every caller waiting on it, that one's and those behind it, gets Hung at once and lets go of what
    it holds. Nothing else is ever held by a driver that doesn't return. A field test's queue, and a person's Stop,
    Skip and Clear GPIB0, all waited behind one such call until Kelvin was restarted, with nobody in the lab."""

    def __init__(self, name: str):
        import queue
        self.name, self.dead, self.ended = name, "", False
        self._jobs: Any = queue.Queue()
        self._now: Optional[_Job] = None   # the call under way
        self.gave_up_at: Optional[float] = None   # when a look last stopped waiting for its own call (wall time)
        self._thread = threading.Thread(target=self._run, name="kelvin-%s-driver" % name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            with job.lock:
                skip = job.taken_back
                if not skip:
                    job.began = time.monotonic()
            if skip:
                job.done.set()
                continue
            self._now = job
            _running.job = job
            try:
                job.box["value"] = job.fn(*job.args, **job.kwargs)
            except BaseException as e:   # the caller's to raise, if it still waits
                job.box["error"] = e
            finally:
                _running.job = None
                self._now = None
                job.done.set()

    def on_its_thread(self) -> bool:
        """Whether the thread asking is this courier's own (a driver's call that calls into the driver again): such a
        call is made there and then, never queued behind itself."""
        return threading.current_thread() is self._thread

    def _overdue(self) -> Optional[str]:
        """The call under way, when it has run past its own limit: in words; None otherwise."""
        now = self._now
        if now is not None and now.began is not None and time.monotonic() - now.began > now.limit:
            return "its driver didn't return from %s within %d s" % (now.what, round(now.limit))
        return None

    def call(self, limit: float, what: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """fn(*args, **kwargs) on this courier's thread: its result, its error, or Hung once the call under way (this
        one, or an earlier one this one waits behind) runs past its own limit; this thread's own stop ends the wait at
        once (instrument.Stopped), and a look's patience after that long (instrument.Busy) (stoppable)."""
        return self._call(limit, what, fn, args, kwargs, False)

    def kept(self, limit: float, what: str, fn: Callable[..., Any], *args: Any) -> Any:
        """As call, but never taken back once asked: it is made in its turn even when its caller stops waiting for it
        (a Stop, a look's patience). A session's timeout, put back after a short wait (VisaBackend.brief, a check
        that it is back in step), so is never left short by a Stop that came meanwhile, and every call after finds
        it put back."""
        return self._call(limit, what, fn, args, {}, True)

    def look(self, limit: float, give_up: float, what: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """As call, for a look (the monitor's): past give_up seconds of its own call, the look stops waiting (GaveUp, a
        Busy) and the call goes on, its own limit unchanged: a slow reading is never taken for a driver that won't
        return, and the connection the work uses isn't ended for it; a later caller finds it hung once past its
        limit."""
        return self._call(limit, what, fn, args, kwargs, False, give_up)

    def post(self, what: str, fn: Callable[..., Any], *args: Any) -> Optional[threading.Event]:
        """fn(*args) in its turn, after the calls asked before it and before any asked after, not waited for: a
        session's timeout, set around a call (Session.__setattr__). A thread a Stop let go of is never held again
        putting a timeout back behind the call it left in the driver. Its done event, to wait on a moment if wanted;
        nothing, once the courier is dead or ended."""
        if self.dead or self.ended:
            return None
        job = _Job(fn, args, {}, OPEN_CALL_S, what)
        self._jobs.put(job)
        return job.done

    def _call(self, limit: float, what: str, fn: Callable[..., Any], args: Tuple[Any, ...], kwargs: Dict[str, Any],
              keep: bool, give_up: Optional[float] = None) -> Any:
        if self.dead:
            raise Hung("%s: %s" % (self.name, self.dead))
        if self.ended:   # its session was closed: nothing runs its calls any more
            raise LinkLost("%s: its connection was closed" % self.name)
        job = _Job(fn, args, kwargs, limit, what)
        self._jobs.put(job)
        stopped, patience = (getattr(_waits, "now", None) or (None, None))[:2]
        waited = time.monotonic()
        while not job.done.wait(STOP_SLICE_S):
            if not self._thread.is_alive() and not job.done.is_set():   # closed meanwhile: never made
                job.taken_back = True
                raise LinkLost("%s: its connection was closed" % self.name)
            late = self._overdue()
            if late or self.dead:
                if not self.dead:
                    self.dead = late
                    log.warning("%s: %s; its session is set aside", self.name, late)
                job.taken_back = True
                raise Hung("%s: %s" % (self.name, self.dead))
            if stopped is not None and stopped():
                with job.lock:
                    if job.began is None and not keep:
                        job.taken_back = True
                from .instrument import Stopped
                raise Stopped("%s: stopped by a person while it waited for %s" % (self.name, what))
            # a look's patience is for waiting behind the session's earlier calls, never for its own call once that
            # has begun: that one is waited for as any other (its own limit), so a silent instrument is found silent
            if patience is not None and job.began is None and time.monotonic() - waited > patience:
                with job.lock:
                    begun = job.began is not None
                    if not begun and not keep:
                        job.taken_back = True
                if not begun:   # never sent ("nothing was sent" holds); one that began meanwhile is waited for
                    from .instrument import Busy
                    under_way = self._now
                    raise Busy("%s is busy: its driver is still on an earlier call (%s)"
                               % (self.name, under_way.what if under_way is not None else "one before this"))
            began = job.began
            if give_up is not None and began is not None and time.monotonic() - began > give_up:
                from .instrument import GaveUp
                self.gave_up_at = time.time()
                raise GaveUp("%s is busy: its driver hasn't returned from %s in %d s; a look waits no longer (the "
                             "call goes on)" % (self.name, what, round(give_up)))
        if "error" in job.box:
            raise job.box["error"]
        return job.box.get("value")

    def end(self) -> None:
        """No more calls: its thread ends once those under way have (a dead one is left to its call). A call asked of
        it after is refused at once (LinkLost), never left waiting for a thread that is gone."""
        if self.ended:
            return
        self.ended = True
        self._jobs.put(None)   # a dead one's thread ends too, should its call ever return


# the timeouts NI-488.2 keeps on a GPIB board (T10us ... T1000s), in seconds
_NI_TIMEOUTS = (1e-5, 3e-5, 1e-4, 3e-4, 1e-3, 3e-3, 0.01, 0.03, 0.1, 0.3, 1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1000.0)


class Session:
    """A VISA session whose every call into the driver, and every attribute it asks or sets, goes through its Courier,
    waited for at most its limit: twice its timeout (as it is set now) and CALL_GRACE_S, CALL_FLOOR_S at least. To
    the backend it is the session itself."""
    __slots__ = ("_raw", "_courier", "_timeout", "_methods", "_bulk", "_gpib")

    def __init__(self, raw: Any, courier: Courier, gpib: bool = False):
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "_courier", courier)
        object.__setattr__(self, "_timeout", None)
        object.__setattr__(self, "_methods", {})
        object.__setattr__(self, "_bulk", 0)
        object.__setattr__(self, "_gpib", gpib)   # a GPIB board's session: NI-488.2's own timeouts

    def bulk(self) -> Any:
        """Its calls meanwhile (a with block) may take BULK_CALL_S: a transfer its timeout doesn't bound, read a chunk
        at a time (a deep-memory waveform, a trace to its end), is never taken for a driver that won't return."""
        import contextlib

        @contextlib.contextmanager
        def long() -> Any:
            object.__setattr__(self, "_bulk", self._bulk + 1)
            try:
                yield
            finally:
                object.__setattr__(self, "_bulk", self._bulk - 1)
        return long()

    def _limit(self) -> float:
        t = self._timeout
        if t is None:
            try:
                t = self._courier.call(OPEN_CALL_S, "its timeout", getattr, self._raw, "timeout", None)
            except Hung:
                raise
            except Exception:
                t = None
            object.__setattr__(self, "_timeout", t)
        try:
            seconds = float(t) / 1000.0
        except (TypeError, ValueError):
            seconds = 0.0
        if seconds != seconds or seconds == float("inf"):   # none, or for ever: a day's sweep at most
            seconds = 43200.0
        if self._gpib:   # NI-488.2 keeps only these timeouts, rounding a VISA one up to the next (31 s waits 100 s)
            seconds = next((v for v in _NI_TIMEOUTS if v >= seconds - 1e-9), seconds)
        return max(CALL_FLOOR_S, 2 * seconds + CALL_GRACE_S, BULK_CALL_S if self._bulk else 0.0)

    def __getattr__(self, name: str) -> Any:
        method = self._methods.get(name)
        if method is not None:
            return method
        if name == "timeout" and self._timeout is not None:
            return self._timeout   # as last set: in place before any call after it (one after another), not waited for
        value = self._courier.call(OPEN_CALL_S, "its %s" % name, getattr, self._raw, name)
        if name == "timeout":
            object.__setattr__(self, "_timeout", value)
        if not callable(value):
            return value

        def call(*args: Any, **kwargs: Any) -> Any:
            limit = self._limit()
            give_up = self.look_give_up() if a_look() else None
            if give_up is not None:   # a query's own wait between its write and its read (a card's query_delay)
                try:
                    give_up += max(0.0, float(kwargs.get("delay") or 0.0))
                except (TypeError, ValueError):
                    pass
            if give_up is not None and give_up < limit:   # a look: past its timeout, the driver won't say more
                return self._courier.look(limit, give_up, name, value, *args, **kwargs)
            return self._courier.call(limit, name, value, *args, **kwargs)
        self._methods[name] = call
        return call

    def look_give_up(self) -> Optional[float]:
        """How long a look waits for its own call: the session's timeout (as last set; NI-488.2's on a GPIB board) and
        LOOK_GRACE_S, LOOK_FLOOR_S at least. Past it the driver would have answered, or said it timed out, had the bus
        been well: a wedged GPIB adapter's call goes on to its limit (twice that and more) without the look. None: a
        long transfer (bulk), or a timeout not known yet."""
        seconds = self._seconds(self._timeout)
        if seconds is None or self._bulk:
            return None
        return max(LOOK_FLOOR_S, seconds + LOOK_GRACE_S)

    def under_way(self) -> Optional[Tuple[str, float, bool]]:
        """The call in its driver now: (what, seconds so far, overdue: past look_give_up, or past half its limit where
        that isn't known); None: none."""
        job = self._courier._now
        began = job.began if job is not None else None
        if began is None:
            return None
        so_far = time.monotonic() - began
        due = self.look_give_up()
        return job.what, so_far, so_far > (due if due is not None else job.limit / 2.0)

    def _seconds(self, t: Any) -> Optional[float]:
        """A VISA timeout (ms) in seconds, as the driver keeps it (NI-488.2's on a GPIB board); None: none known."""
        try:
            seconds = float(t) / 1000.0
        except (TypeError, ValueError):
            return None
        if seconds != seconds or seconds == float("inf"):
            return None
        if self._gpib:
            seconds = next((v for v in _NI_TIMEOUTS if v >= seconds - 1e-9), seconds)
        return seconds

    def __setattr__(self, name: str, value: Any) -> None:
        """Set in its turn. Its timeout, set around calls (a short wait and back, a long acquisition and back), is
        never waited for (Courier.post): it is in place for every call asked after it, which is waited for by it
        (_limit), and a Stop that let a thread go never holds it again behind the call it left in the driver. Anything
        else is waited for, and never taken back once asked (Courier.kept)."""
        if name == "timeout":
            object.__setattr__(self, "_timeout", value)
            done = self._courier.post("setting its timeout", setattr, self._raw, name, value)
            if done is not None:   # in place at once when its thread is free; never long behind a call left there
                done.wait(POST_WAIT_S)
            return
        self._courier.kept(OPEN_CALL_S, "setting its %s" % name, setattr, self._raw, name, value)

    def discard(self) -> None:
        """Closed and let go of: through its courier while that lives; else on a thread of its own, not waited for
        (closing may free the call that didn't return)."""
        courier, raw = self._courier, self._raw
        if courier.ended:   # closed already (a connection made anew that failed keeps the one it closed)
            return
        if courier.dead or courier._now is not None:   # a call still in the driver (one a Stop let go): never waited
            courier.end()                               # behind; closing may end it
            threading.Thread(target=_close_quietly, args=(raw,), name="kelvin-%s-close" % courier.name,
                             daemon=True).start()
            return
        try:
            with stoppable(lambda: False):   # a Stop pending on this thread never cancels the close (nor leaks it)
                courier.call(CLOSE_CALL_S, "closing it", raw.close)
        except Exception:
            pass
        finally:
            courier.end()


def under_way(backend: Any) -> Optional[Tuple[str, float, bool]]:
    """The call in this connection's driver now (Session.under_way: what, seconds so far, overdue); None: none, or a
    connection that isn't a VISA session's."""
    res = getattr(backend, "res", None)
    return res.under_way() if isinstance(res, Session) else None


def gave_up_at(backend: Any) -> float:
    """When a look last stopped waiting for this connection's call (Courier.look's give-up; wall time); 0: never."""
    res = getattr(backend, "res", None)
    courier = res._courier if isinstance(res, Session) else getattr(backend, "_courier", None)
    return getattr(courier, "gave_up_at", None) or 0.0


def long_calls(res: Any) -> Any:
    """res's calls meanwhile may take BULK_CALL_S (Session.bulk); nothing changes for any other session."""
    import contextlib
    return res.bulk() if isinstance(res, Session) else contextlib.nullcontext()


def _close_quietly(raw: Any) -> None:
    try:
        raw.close()
    except Exception:
        pass


def timed_out(e: BaseException) -> bool:
    """A read that ran out of time (PyVISA's VI_ERROR_TMO), not a broken connection; also when Kelvin said it in
    words (instrument.InstrumentLost), from the error it came from. Never the instrument's own error answer."""
    if isinstance(e, ErrorAnswer):
        return False
    if isinstance(e, TimeoutError) or getattr(e, "error_code", None) == -1073807339 or "VI_ERROR_TMO" in str(e):
        return True
    return e.__cause__ is not None and e.__cause__ is not e and timed_out(e.__cause__)


# VISA's codes for an instrument that can't be reached at all, as opposed to one that is slow to answer
GONE = {-1073807265: "VI_ERROR_NLISTENERS",    # GPIB: nothing listens at the address (off, unplugged, address changed)
        -1073807343: "VI_ERROR_RSRC_NFOUND",   # no longer listed (a pulled USB cable, switched off)
        -1073807194: "VI_ERROR_CONN_LOST",     # a LAN connection dropped
        -1073807298: "VI_ERROR_IO",            # an I/O error (a pulled USB cable)
        -1073807346: "VI_ERROR_INV_OBJECT"}    # the session went away
_SLOW = re.compile(r"timed? ?out|time-out|did not answer|didn't answer|no response", re.I)
_GONE = re.compile(r"not connected|isn't connected|connection (was )?(lost|refused|reset|closed|aborted)|unreachable"
                   r"|no listeners|device (was )?(removed|not found|disconnected)|broken pipe|no such device"
                   r"|actively refused|forcibly closed|closed the connection|input/output error", re.I)
# a VISA session these end is dead for good (a power cycle, a replug: the instrument comes back as a new device), and
# a new one is opened; never NLISTENERS, after which a GPIB session still works once the instrument is on again
DEAD = {-1073807194: "VI_ERROR_CONN_LOST", -1073807346: "VI_ERROR_INV_OBJECT",
        -1073807343: "VI_ERROR_RSRC_NFOUND", -1073807298: "VI_ERROR_IO"}
# what the OS says of a device that went away under a connection: no such device, an I/O error, a pipe broken
LOST_ERRNO = {errno.ENODEV, errno.EIO, errno.EPIPE}
# pyserial's and pyusb's errors (an adapter or a USB device pulled), and pyvisa's for a session that went away; their
# timeouts (SerialTimeoutException, USBTimeoutError) are an instrument slow to answer, not gone
LOST_TYPES = ("SerialException", "USBError", "InvalidSession")
_DEAD = re.compile(r"no such device|device (was )?(removed|disconnected|not configured)|invalid session"
                   r"|connection (was )?(lost|reset|aborted)|forcibly closed|broken pipe|input/output error", re.I)


def _errors(e: BaseException) -> Iterator[BaseException]:
    """An error and the ones it came from (raise ... from, or raised while handling), each once: a driver's words
    over the bus's error, a lost USB device under pyvisa-py's ValueError."""
    seen = set()
    while e is not None and id(e) not in seen and len(seen) < 8:
        seen.add(id(e))
        yield e
        e = e.__cause__ if e.__cause__ is not None else None if e.__suppress_context__ else e.__context__


def _lost_type(e: BaseException) -> Optional[str]:
    """What an error's own kind says: "gone", "silent" (a timeout of pyserial's or pyusb's), or None."""
    names = [k.__name__ for k in type(e).__mro__]
    if any("Timeout" in n for n in names[:-3]):
        return "silent"
    if getattr(e, "error_code", None) in GONE or isinstance(e, ConnectionError) or \
            getattr(e, "errno", None) in LOST_ERRNO or any(n in LOST_TYPES for n in names):
        return "gone"
    return None


def lost_kind(e: BaseException) -> Optional[str]:
    """Whether an error says the instrument isn't answering: "gone" when it can't be reached at all (nothing listens
    on GPIB, a pulled USB cable or USB-serial adapter, a dropped connection, a server that refuses it), "silent" when
    it didn't answer in time; None for any other error (a command it refused, a card's mistake), which is not a lost
    instrument. The errors it came from count too: a maker's driver and pyvisa-py say a lost device in their own
    words over the system's (review of 0.3.45, 4.4: eleven such went by as no loss at all). The instrument's own error
    answer (ErrorAnswer) is never a loss: its words name a sensor's state, not the link's."""
    if isinstance(e, ErrorAnswer):
        return None
    chain = list(_errors(e))
    for err in chain:
        kind = _lost_type(err)
        if kind == "gone":
            return kind
    if timed_out(e) or any(_lost_type(err) == "silent" for err in chain):
        return "silent"
    for err in chain:
        text = str(err)
        if _GONE.search(text):
            return "gone"
        if _SLOW.search(text):
            return "silent"
    return None


def session_dead(e: BaseException) -> str:
    """Whether an exchange's error says its VISA session is dead (the instrument was power-cycled or replugged, its
    LAN connection dropped, its USB-serial adapter pulled): what said so, in a few words; "" for any other error,
    a timeout, or GPIB's NLISTENERS (its session still works once the instrument is back), or the instrument's own
    error answer (ErrorAnswer), whatever its words."""
    if isinstance(e, ErrorAnswer):
        return ""
    for err in _errors(e):
        code = getattr(err, "error_code", None)
        if code in DEAD:
            return DEAD[code]
        if code is not None or _lost_type(err) == "silent":
            continue
        if _lost_type(err) == "gone" or _DEAD.search(str(err)):
            return (str(err).strip().splitlines() or [type(err).__name__])[0][:120] or type(err).__name__
    return ""


def names_device(resource: str) -> bool:
    """Whether a VISA address names the device itself, not only a place another could be: USB-TMC with its serial
    number (USB0::0x05E6::0x2400::04412345::INSTR). A LAN address or a serial port is wherever a device is put."""
    return bool(re.match(r"^USB\d*::(0x)?[0-9a-f]+::(0x)?[0-9a-f]+::[^:]+::", resource, re.I))


# a maker's software an instrument is reached through, by its address's scheme: (where, what to check)
_SOFTWARE = {"qd": ("MultiVu at %s", "is MultiVu running, with its server started?"),
             "zhinst": ("LabOne at %s", "is the LabOne data server running, and the instrument on?"),
             "qick": ("the board's QICK server (%s)", "is the board on, with its Pyro4 name server and QICK server "
                                                     "running (a reboot ends both unless they start at boot)?"),
             "picam": ("PICam (camera %s)", "is it on and cabled, and not open in another program (LightField, "
                                            "another Kelvin)?")}


def where_and_cause(address: str) -> Tuple[str, str]:
    """Where an instrument at this address is reached, and what to check when it stops answering, in words:
    ("on GPIB (address 7)", "cable or power?"), ("through MultiVu at 127.0.0.1:5000", "is MultiVu running, ...?");
    ("", "") for a simulated one."""
    address = (address or "").strip()
    scheme, sep, rest = address.partition("://")
    if sep and scheme.lower() == "sim":
        return "", ""
    if sep and scheme.lower() == "prologix":
        from .prologix import place, where
        try:
            at = where(address)
        except ValueError:
            return "through a Prologix adapter (%s)" % rest, "cable or power?"
        return ("on GPIB (address %d) through the Prologix %s" % (at.pad, place(at.adapter)),
                "cable or power? (or the adapter: unplugged, or held by another program)")
    if sep and scheme.lower() in ("jsonrpc", "zmq"):   # its own interface on the network, no maker's program between
        host = rest.split("?", 1)[0].split("/", 1)[0].rsplit(":", 1)[0] or rest
        return "on the network (%s)" % host, "network cable, power or its IP address?"
    if sep and scheme.lower() not in ("visa", "serial"):
        where, cause = _SOFTWARE.get(scheme.lower(), ("its maker's software (%s)" % address,
                                                      "is it on, and its maker's software running?"))
        return "through " + (where % rest if "%s" in where else where), cause
    name = resource_name(address).upper()
    m = re.match(r"^GPIB\d*::(\d+)", name)
    if m:
        return "on GPIB (address %s)" % m.group(1), "cable or power?"
    if name.startswith("USB"):
        return "on USB", "USB cable or power?"
    m = re.match(r"^TCPIP\d*::([^:]+)", name)
    if m:
        return "on the network (%s)" % m.group(1), "network cable, power or its IP address?"
    m = re.match(r"^ASRL(\w+?)::", name)
    if m:
        port = "COM%s" % m.group(1) if m.group(1).isdigit() else m.group(1)
        return "on " + port, "cable or power? (or the port's baud rate and framing)"
    return "at " + address, "cable or power?"


def short_model(model: str) -> str:
    """A card's model name, short enough for an alarm: "Keithley 2182 / 2182A nanovoltmeter" as it is, "Quantum Design
    PPMS / DynaCool / VersaLab / MPMS3 (MultiVu)" -> "Quantum Design PPMS / DynaCool"."""
    text = re.split(r" \(|, ", model or "", maxsplit=1)[0].strip()
    while len(text) > 40 and " / " in text:
        text = text.rsplit(" / ", 1)[0]
    return text if len(text) <= 40 else text[:39].rstrip() + "…"


def short_code(e: BaseException) -> str:
    """The error as the bus or library gave it, short: VISA's code name, else its first line."""
    code = getattr(e, "error_code", None)
    if code in GONE or code == -1073807339:
        return GONE.get(code, "VI_ERROR_TMO")
    text = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return text[:120]


def resource_manager() -> Any:
    """The process-wide PyVISA resource manager (created on first use)."""
    global _rm, _rm_own
    if not _rm_lock.acquire(timeout=OPEN_CALL_S):   # another thread is still starting it (the NI services wedged?)
        raise AdapterError("VISA is still starting on another thread after %d s: check NI-VISA's services (NI MAX), or "
                           "restart the computer" % round(OPEN_CALL_S))
    try:
        if _rm is None:
            _rm_own = True   # Kelvin's own, which it closes as it quits (close_resource_manager)
            try:
                import pyvisa
            except ImportError as e:
                raise AdapterError("talking to real instruments needs PyVISA: pip install \"kelvin-lab[hardware]\"") from e
            from .survey import use_bundled_libusb
            use_bundled_libusb()   # Kelvin's own libusb, for USB instruments without a vendor VISA
            choice = os.environ.get("KELVIN_VISA_BACKEND")
            _rm = pyvisa.ResourceManager(choice) if choice else _installed_or_builtin(pyvisa)
            try:
                from .survey import describe_visa
                log.info("VISA: %s", describe_visa(_rm))
            except Exception:
                pass
        return _rm
    finally:
        _rm_lock.release()


def _said(e: BaseException) -> str:
    return " ".join(str(e).split())[:240] or type(e).__name__


def _installed_or_builtin(pyvisa: Any) -> Any:
    """The installed VISA (NI-VISA, Keysight IO Libraries, R&S VISA) where there is one, else Kelvin's built-in VISA
    (pyvisa-py: no GPIB, USB through libusb). PyVISA looks for the installed one along PATH alone, and gives the
    built-in one, without a word, when it finds none there or can't load what it finds: so Kelvin also looks in the
    Windows system folder, where the VISA standard puts it, loads it the way Windows' other programs (NI MAX) do when
    Python's own way can't, and keeps why in VISA_NOTE when one is there and still doesn't load."""
    global VISA_NOTE
    from .survey import _is_builtin, installed_visa_libraries
    failed: List[str] = []
    rm = None
    try:
        rm = pyvisa.ResourceManager()   # the installed VISA PyVISA finds along PATH, or the built-in one
        if not _is_builtin(rm):
            VISA_NOTE = ""
            return rm
    except Exception as e:   # what it found along PATH didn't load
        failed.append(_said(e))
    for path in installed_visa_libraries():
        try:
            found = _load_installed(pyvisa, path)
        except Exception as e:
            failed.append("%s: %s" % (path, _said(e)))
            continue
        log.info("VISA: %s, found in the Windows system folder", path)
        if rm is not None:
            try:
                rm.close()   # the built-in one, not used
            except Exception:
                pass
        VISA_NOTE = ""
        return found
    VISA_NOTE = ("This computer's VISA didn't load in Kelvin (%s), so Kelvin uses its built-in VISA, which can't reach "
                 "GPIB: repair NI-VISA (or your maker's VISA) in Windows' Apps, then restart Kelvin"
                 % "; ".join(failed)[:400]) if failed else ""
    if VISA_NOTE:
        log.warning("%s", VISA_NOTE)
    return rm if rm is not None else pyvisa.ResourceManager("@py")


def _load_installed(pyvisa: Any, path: str) -> Any:
    """PyVISA's resource manager over the VISA library at path. Python looks for the libraries that one needs in the
    system folders and the library's own, never along PATH as Windows' other programs do: when PyVISA can't load it,
    it is loaded first as those programs load it, and PyVISA's own load then finds it loaded."""
    try:
        return pyvisa.ResourceManager(path)
    except Exception as first:
        if not sys.platform.startswith("win"):
            raise
        import ctypes
        try:
            ctypes.WinDLL(path, winmode=0x8)   # LOAD_WITH_ALTERED_SEARCH_PATH: its folder, Windows', then PATH
        except OSError:
            raise first
        return pyvisa.ResourceManager(path)


def close_resource_manager() -> None:
    """PyVISA's resource manager closed, after every session it opened is (Kelvin quitting, Lab.close first): left to
    Python's exit, it complained of sessions gone before it (VI_ERROR_INV_OBJECT, at every quit of a field test)."""
    global _rm, _rm_own
    if not _rm_lock.acquire(timeout=CLOSE_CALL_S):   # still being started on a thread stuck in the driver: left
        return
    try:
        if not _rm_own:   # one set from outside (a test's pretend bus) is theirs
            return
        rm, _rm, _rm_own = _rm, None, False
    finally:
        _rm_lock.release()
    close = getattr(rm, "close", None)
    if close is not None:   # on a thread of its own, CLOSE_CALL_S at most: quitting never waits on a wedged VISA
        closing = threading.Thread(target=_close_quietly, args=(rm,), name="kelvin-visa-close", daemon=True)
        closing.start()
        closing.join(CLOSE_CALL_S)


def set_resource_manager(rm: Any) -> None:
    """Use another resource manager (tests use a fake bus)."""
    global _rm, _rm_own
    with _rm_lock:
        _rm, _rm_own = rm, False


def bus_of(address: str) -> Optional[str]:
    """The GPIB bus an instrument shares with others: "GPIB0" (a board), or "prologix://<adapter>"; None for one on a
    connection of its own (USB, LAN, a serial port), which can't hang the others."""
    address = (address or "").strip()
    if address.lower().startswith("prologix://"):
        from .prologix import where
        try:
            return "prologix://" + where(address).adapter
        except ValueError:
            return None
    m = re.match(r"^GPIB(\d*)::", resource_name(address).upper())
    return "GPIB%s" % (m.group(1) or "0") if m else None


def bus_words(bus: str) -> str:
    """A bus as people know it: "GPIB0", "the Prologix adapter on COM5"."""
    if bus.startswith("prologix://"):
        from .prologix import place
        return "the Prologix adapter %s" % place(bus[len("prologix://"):])
    return bus


class Buses:
    """GPIB buses that stopped answering (one instrument can hang a whole bus): Kelvin sends nothing on one until it is
    cleared or answers again, so its own calls never pile up in front of the clear. In a field test a hung bus took
    every instrument on it for 45 minutes, Kelvin went on asking each of them, and an interface clear from another
    program waited more than a minute behind its calls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.hung: Dict[str, Dict[str, Any]] = {}   # bus -> {since, names, text}
        self._through = threading.local()   # the bus this thread may send on anyway (its probe, its clear)

    def flag(self, bus: str, names: List[str], text: str) -> None:
        import time
        with self._lock:
            self.hung.setdefault(bus, {"since": time.time(), "names": list(names), "text": text})

    def clear(self, bus: str) -> bool:
        with self._lock:
            return self.hung.pop(bus, None) is not None

    def text(self, bus: Optional[str]) -> Optional[str]:
        """Why nothing is sent on this bus now, in words; None when it may be."""
        if not bus or getattr(self._through, "bus", None) == bus:
            return None
        with self._lock:
            h = self.hung.get(bus)
        if h is None:
            return None
        return ("%s isn't answering: Kelvin sends nothing on it until it is cleared (Lab panel: Clear the bus, or the "
                "agent's clear_bus) or it answers again" % bus_words(bus))

    def through(self, bus: str) -> Any:
        """This thread may send on the bus meanwhile (a probe, the clear itself)."""
        import contextlib

        @contextlib.contextmanager
        def passing() -> Any:
            before = getattr(self._through, "bus", None)
            self._through.bus = bus
            try:
                yield
            finally:
                self._through.bus = before
        return passing()


BUSES = Buses()


def interface_clear(bus: str, rm: Any = None) -> None:
    """GPIB's interface clear on this bus: every instrument on it stops talking and listening; their settings and
    outputs stay as they are. What brought a field test's hung bus back at once."""
    if bus.startswith("prologix://"):   # on a thread of its own too: an adapter whose link hangs can't hold the person
        from .prologix import held

        def clear() -> None:
            with held(bus[len("prologix://"):]) as adapter:
                adapter.interface_clear()
        courier = Courier(bus)
        try:
            courier.call(OPEN_CALL_S, "its interface clear", clear)
        except Hung as e:
            raise AdapterError("%s's interface clear didn't return within %d s: the adapter's link hangs. Unplug and "
                               "replug the adapter; if that doesn't free it, restart Kelvin" % (bus, round(OPEN_CALL_S))) \
                from e
        finally:
            if not courier.dead:
                courier.end()
        return
    courier = Courier(bus)   # the clear's own: a driver wedged on the bus can't hold the person who asked for it
    try:
        rm = rm or courier.call(OPEN_CALL_S, "starting VISA", resource_manager)
        res = courier.call(OPEN_CALL_S, "opening %s's interface" % bus, rm.open_resource, "%s::INTFC" % bus)
        try:
            courier.call(OPEN_CALL_S, "its interface clear", res.send_ifc)
        finally:
            try:
                Session(res, courier).discard()
            except Exception:
                pass
    except Hung as e:
        raise AdapterError("%s's interface clear didn't return within %d s: its driver hangs. Reset the GPIB adapter "
                           "(NI MAX: Reset), or unplug and replug a USB one; if that doesn't free it, restart Kelvin"
                           % (bus, round(OPEN_CALL_S))) from e
    finally:
        if not courier.dead:
            courier.end()


def resource_name(address: str) -> str:
    """A lab-config address as a VISA resource string."""
    address = address.strip()
    if address.startswith("visa://"):
        return address[len("visa://"):]
    if address.lower().startswith("prologix://"):   # not VISA's: one way to write it (prologix.py)
        from .prologix import canonical
        return canonical(address)
    if address.lower().startswith("zmq://"):   # not VISA's either: one way to write it, its port spelt out (zeromq.py)
        from .zeromq import canonical as zmq_canonical
        return zmq_canonical(address)
    m = re.match(r"^serial://(.+)$", address)
    if m:
        port = m.group(1)
        num = re.match(r"^COM(\d+)$", port, re.I)
        return "ASRL%s::INSTR" % (num.group(1) if num else port)
    return address


def is_serial(name: str) -> bool:
    return name.upper().startswith("ASRL")


def marks_message_end(name: str) -> bool:
    """A bus that marks where each reply ends (EOI on GPIB, also through a Prologix adapter; the end of a USB-TMC or
    VXI-11/HiSLIP message), unlike a plain socket or a serial line, where only a line end does."""
    name = name.upper()
    return name.endswith("::INSTR") and not is_serial(name) or name.startswith("PROLOGIX://")


def apply_serial_settings(res: Any, settings: Dict[str, Any]) -> None:
    """Baud rate and framing on a serial resource, from a card's connection block (or a lab config's): parity as a
    word or the manual's letter (N, O, E), or framing: 8N1."""
    settings = serial_settings(settings)
    try:
        from pyvisa import constants
        parity = {"none": constants.Parity.none, "odd": constants.Parity.odd, "even": constants.Parity.even,
                  "mark": constants.Parity.mark, "space": constants.Parity.space}
        stop = {1.0: constants.StopBits.one, 1.5: constants.StopBits.one_and_a_half, 2.0: constants.StopBits.two}
    except ImportError:  # a fake bus in tests
        parity, stop = {}, {}
    if "parity" in settings and parity_name(settings["parity"]) is None:
        raise AdapterError("parity %r: it is none, odd, even, mark or space (N, O, E, M, S)" % (settings["parity"],))
    if "baud_rate" in settings:
        res.baud_rate = int(settings["baud_rate"])
    if "data_bits" in settings:
        res.data_bits = int(settings["data_bits"])
    if "parity" in settings:
        res.parity = parity.get(settings["parity"], settings["parity"])
    if "stop_bits" in settings:
        res.stop_bits = stop.get(float(settings["stop_bits"]), settings["stop_bits"])


class VisaBackend:
    simulated = False
    quiet_close = True   # closing the session sends the instrument nothing

    def __init__(self, name: str, card: Card, address: str, rm: Any = None,
                 overrides: Optional[Dict[str, Any]] = None):
        self.name = name
        self.address = address
        self.resource = resource_name(address)
        conn = dict(card.connection, **serial_settings(overrides or {}))   # a lab's framing: 8N1 wins over the card's
        self.ack = conn.get("ack")
        self.enq = bool(conn.get("enq"))
        self.echo = bool(conn.get("echo"))
        self.set_reply = re.compile(conn["set_reply"]) if conn.get("set_reply") else None
        self.write_only = bool(conn.get("write_only"))
        self.query_delay = float(conn.get("query_delay", 0.0))
        self.lock = threading.RLock()
        self._rm, self._conn, self._on_connect = rm, conn, list(card.on_connect)
        # a reply the instrument may still send: an exchange timed out. It is dropped, and the instrument asked who it
        # is until it answers as itself, before the next command (_settle): a field test's *IDN? got the reading of a
        # query from before a bus hang, and every answer after it would have been one behind
        self.owed = False
        # its session died (a power cycle, a replug): what said so. Nothing is sent on it until it is made anew and the
        # instrument has answered as itself (reopen; the monitor's reconnector, or a person's reconnect)
        self.broken = ""
        self.sessions = 1   # sessions opened for it: one more each time it is made anew
        self.hung_at: Optional[float] = None   # when a call into its driver last didn't return (wall time)
        self._stuck_open: Optional[Courier] = None   # an open that didn't return: none is tried again beside it
        self._sync = (card.idn_query, card.idn_pattern) if card.idn_pattern else None
        # the identity it last answered as itself, when asked who it is: read where another reply should be, it is a
        # late reply to an earlier *IDN?, also for a card with no identity pattern (_in_its_place)
        self.said_as = ""
        self.bus = bus_of(address)
        self._open()

    def _open(self) -> None:
        """Its session opened and set up, then its start-up commands sent (on_connect). One that fails half way lets go
        of what it opened: a Prologix adapter's connection stayed held when a start-up command failed (review of
        0.3.45, 4.6)."""
        self._connect()
        try:
            self._start()
        except BaseException:
            self._close_res()
            raise

    def _start(self) -> None:
        for command in self._on_connect:   # configuration only; parse_card refuses drive commands here
            self.write(command)

    def _close_res(self) -> None:
        res = getattr(self, "res", None)
        try:
            if isinstance(res, Session):   # through its courier, or, if that is lost to a call, beside it
                res.discard()
            elif res is not None:
                res.close()
        except Exception:
            pass   # closing: nothing more to do with it

    def _connect(self, open_timeout: Optional[int] = None) -> None:
        """Its session opened and set up (timeout, line ends, serial framing; the lines it greets a connection with,
        read and dropped). Nothing is sent to the instrument. open_timeout (ms): how long a real resource manager may
        wait for it (pyvisa-py's connection to a LAN instrument)."""
        rm = self._rm
        stuck = getattr(self, "_stuck_open", None)   # (VISA itself started on the courier below: bounded too)
        if stuck is not None and stuck._now is not None:   # never one more beside it: each would only hang as well
            raise AdapterError("%s: Kelvin's last try to open %s is still stuck in the VISA driver (%s); clear the bus, "
                               "or check the adapter (NI MAX's Reset of the board), or restart Kelvin"
                               % (self.name, self.resource, stuck._now.what))
        self._stuck_open = None
        courier = Courier(self.name)   # a session's own: one lost to a call that never returned is never used again
        opened: Dict[str, Any] = {}

        def open_it(*args: Any, **kwargs: Any) -> Any:
            opened["raw"] = (rm or resource_manager()).open_resource(*args, **kwargs)
            return opened["raw"]
        try:
            rm = rm or courier.call(OPEN_CALL_S, "starting VISA", resource_manager)
            if open_timeout and hasattr(rm, "visalib"):
                raw = courier.call(OPEN_CALL_S + open_timeout / 1000.0, "opening %s" % self.resource,
                                   open_it, self.resource, open_timeout=open_timeout)
            else:
                raw = courier.call(OPEN_CALL_S, "opening %s" % self.resource, open_it, self.resource)
            self.res = Session(raw, courier, gpib=self.resource.upper().startswith("GPIB"))
        except AdapterError:
            if courier.dead:   # the open itself never returned
                self._stuck_open = courier
            courier.end()
            raise
        except Exception as e:
            from .instrument import Stopped
            if isinstance(e, Stopped):
                # a person's Stop ended the wait for the open, which goes on in the driver: none is opened beside it
                # while it does (_stuck_open), and what it opens, should it, is closed, unused, as soon as it has
                if courier._now is not None:
                    self._stuck_open = courier
                courier.post("closing what an open a Stop let go of opened",
                             lambda: opened.get("raw") is not None and _close_quietly(opened["raw"]))
                courier.end()
                raise
            courier.end()
            from .survey import _is_builtin, ni4882
            gpib = self.resource.upper().startswith("GPIB")
            if gpib and rm is not None and _is_builtin(rm):   # Kelvin's built-in VISA has no GPIB
                if VISA_NOTE:
                    raise AdapterError("%s: can't reach %s. %s." % (self.name, self.resource, VISA_NOTE)) from e
                raise AdapterError("%s: can't reach %s: GPIB needs a VISA installed on this computer (NI-VISA with "
                                   "NI-488.2 for an NI GPIB adapter), and Kelvin found none. Install it, then restart "
                                   "Kelvin." % (self.name, self.resource)) from e
            if "LIBRARY_NFOUND" in str(e):
                part = ni4882() if gpib else ""
                if part == "here":   # there, and VISA couldn't load it all the same
                    raise AdapterError(
                        "%s: this computer's VISA couldn't load the GPIB library it needs to reach %s "
                        "(VI_ERROR_LIBRARY_NFOUND), though NI-488.2 is installed: repair NI-488.2 and NI-VISA in "
                        "Windows' Apps, then restart Kelvin." % (self.name, self.resource)) from e
                if part == "32-bit":
                    raise AdapterError(
                        "%s: this computer's VISA can't reach %s: NI-488.2 is installed for 32-bit programs only, and "
                        "Kelvin is 64-bit: reinstall NI-488.2 with its 64-bit support, then restart Kelvin."
                        % (self.name, self.resource)) from e
                raise AdapterError("%s: this computer's VISA cannot reach %s: its interface driver is missing (for GPIB, "
                                   "install NI-488.2 or your GPIB adapter's driver)." % (self.name, self.resource)) from e
            from .others import holders_text   # who may hold it: another Kelvin still closing, MATLAB, NI MAX...
            from .portholders import who_has   # who has it open: a serial port's program (field report 2026-11-15)
            raise AdapterError("%s: cannot open %s (%s). Check the cable, the address and that no other program "
                               "holds the instrument%s%s." % (self.name, self.resource, e, holders_text(),
                                                             who_has(self.resource, e))) from e
        try:
            self.res.timeout = int(self._conn.get("timeout_ms", 3000))
            self.res.read_termination = self._conn.get("read_termination", "\n")
            self.res.write_termination = self._conn.get("write_termination", "\n")
            if is_serial(self.resource):
                try:
                    apply_serial_settings(self.res, self._conn)
                except AdapterError as e:
                    raise AdapterError("%s: %s" % (self.name, e)) from None
            if self._conn.get("greeting_lines"):
                self._drop_greeting(int(self._conn["greeting_lines"]))
        except BaseException:
            self._close_res()
            raise

    def _drop_greeting(self, lines: int) -> None:
        """The lines an instrument sends by itself as a connection opens (the AMI 430 greets each LAN connection with
        two), read and dropped: left there, every reply would be taken for the command before. One that sends none
        (the same instrument on its serial port) costs a short wait."""
        before = self.res.timeout
        try:
            self.res.timeout = min(int(before or 3000), 2000)
            for _ in range(lines):
                try:
                    self.res.read()
                except Exception as e:
                    if timed_out(e):
                        break
                    raise
        finally:
            self.res.timeout = before

    def _check_ack(self, command: str, answer: Optional[str] = None) -> None:
        """The ack line after a command (answer: a query's reply, read before it). An instrument that acks only its
        set commands leaves a query's ack missing: its card wants set_reply, not ack, and the error says so."""
        try:
            reply = str(self.res.read()).strip()
        except Exception as e:
            if answer is not None and timed_out(e):
                raise AdapterError("%s answered %r to %r but sent no %r after it: if only its set commands are "
                                   "answered %r, its card wants set_reply: \"^%s$\" instead of ack"
                                   % (self.name, answer.strip(), command, self.ack, self.ack, re.escape(self.ack))) from e
            raise
        if reply != self.ack:   # a reply read in the ack's place may be a late one, the ack behind it: back in step
            self.owed = True    # before the next command, never one behind from here
            raise AdapterError("%s: %r answered %r instead of %r" % (self.name, command, reply, self.ack))

    def _enq_ack(self, command: str) -> None:
        """Pfeiffer's protocol: the command, then <ACK> (or <NAK>) on a line of its own. Measured values the
        instrument was sending by itself until then (it does from power-up) are passed over."""
        self.res.write(command)
        for _ in range(20):
            reply = str(self.res.read()).strip()
            if reply == "\x06":
                return
            if reply == "\x15":
                raise AdapterError("%s refused %r (it answered NAK)" % (self.name, command))
        self.owed = True   # its <ACK> may still come, behind the lines read: back in step before the next command
        raise AdapterError("%s did not acknowledge %r" % (self.name, command))

    def _bus_ok(self) -> None:
        """Raises while its bus is hung (BUSES): nothing is sent, at once, instead of each command waiting it out."""
        why = BUSES.text(self.bus)
        if why:
            raise AdapterError("%s: %s" % (self.name, why))

    def brief(self, ms: int) -> Any:
        """Its exchanges wait ms at most meanwhile (a with block): a retry of one that stopped answering then holds
        the bus that long, not its card's whole timeout, in front of every other instrument on it."""
        import contextlib

        @contextlib.contextmanager
        def short() -> Any:
            with self.lock:
                res = self.res
                before = res.timeout
                try:
                    res.timeout = min(int(before or ms), ms)
                    yield
                finally:
                    if self.res is res:
                        res.timeout = before
        return short()

    def resync(self) -> None:
        """Back in step after its bus was cleared: a device clear, then asked who it is until it answers as itself.
        Its connection is waited for RESYNC_S at most: an exchange still hung in the driver after the interface clear
        keeps it, and the clear says so rather than wait with it (a field test's clear came while a run's exchange
        hung on the bus)."""
        if not self.lock.acquire(timeout=RESYNC_S):
            raise AdapterError("%s is still held by an exchange that hasn't ended since the clear: reconnect it, or "
                               "restart Kelvin if it never ends" % self.name)
        try:
            self.owed = True
            self._settle()
        finally:
            self.lock.release()

    def write(self, command: str) -> None:
        self._bus_ok()
        with self.lock:
            self._settle()
            try:
                if self.enq:
                    return self._enq_ack(command)
                self.res.write(command)
                if self.echo:
                    self.res.read()   # the command, echoed
                if self.ack:
                    self._check_ack(command)
                elif self.set_reply is not None:   # instruments that answer every set (e.g. Oxford Mercury: ...:VALID)
                    reply = str(self.res.read()).strip()
                    if not self.set_reply.search(reply):
                        raise AdapterError("%s refused %r: %s" % (self.name, command, reply))
            except Exception as e:
                self._failed(e)
                raise

    def query(self, command: str) -> str:
        if self.write_only:
            raise AdapterError("%s does not answer queries (write-only); Kelvin uses the last value it set" % self.name)
        self._bus_ok()
        with self.lock:
            self._settle()
            try:
                reply = self._query(command)
                if self._in_its_place(command, reply):
                    return self._past_a_late_one(command, reply)
                if self._asks_identity(command) and self._identity_form(reply):
                    self.said_as = str(reply).strip()
                return reply
            except UnicodeDecodeError as e:   # bytes, not text: a binary block where the card expects text
                self.drain()
                from .cards import BINARY_REPLY
                raise AdapterError("%s (%s): %s" % (self.name, command, BINARY_REPLY)) from e
            except Exception as e:
                self._failed(e)
                raise

    def _asks_identity(self, command: str) -> bool:
        """Whether a question asks who it is (*IDN?, its card's identity query), alone or among others (*IDN?;*OPC?)."""
        own = {"*IDN?", (self._sync[0] if self._sync else "*IDN?").strip().upper()}
        return any(part.strip().upper() in own for part in str(command).split(";"))

    def _identity_form(self, reply: Any) -> bool:
        """Whether a reply to a question of who it is is its identity: its card's pattern, as the lab's identity check
        reads it; with none, IEEE 488.2's four fields (maker, model, serial, firmware). Never numbers."""
        text = str(reply).strip()
        if not text or only_numbers(text):
            return False
        if self._sync is not None:
            return bool(re.search(self._sync[1], text, re.I))
        return len(text.split(",")) >= 4

    def _in_its_place(self, command: str, reply: Any) -> bool:
        """Whether a reply is the instrument's own identity where another question was asked: a late reply to an
        earlier *IDN? (one that ran out of time: the monitor's, or a check's own on a slow bus), read in this question's
        place, this one's own reply still to come (field report 2026-11-15: lockin's SLVL? got
        'Stanford_Research_Systems,SR830,s/n81118,ver1.07', and the monitor called it garbled). Exactly what it last
        said as itself (said_as), which no reading is; where that isn't known, its card's identity pattern at the
        reply's start in an identity's form (an_identity), unless the reply reads as the plain reading asked for
        (reading). Never for a question of who it is."""
        text = str(reply).strip()
        if not text or self._asks_identity(command):
            return False
        if self.said_as:
            return text == self.said_as
        reads = getattr(_reading, "now", None)
        if reads is not None and reads(text):   # it reads as the value asked for: a genuine reading, kept
            return False
        return an_identity(text, self._sync[1] if self._sync else None)

    def _read_past(self, command: str) -> Optional[str]:
        """This question's own reply, read past a late identity in its place: the question was sent and done, so its
        reply comes next (two more replies at most). None when it doesn't come in time, or only identities do: the
        instrument is then owed (put back in step before what is asked next)."""
        if self.enq or self.echo or self.ack:   # replies framed by the protocol: put back in step instead
            self.owed = True
            return None
        for _ in range(2):
            try:
                reply = str(self.res.read())
            except Exception as e:
                if not timed_out(e):
                    raise
                break
            if not self._in_its_place(command, reply):
                return reply
        self.owed = True
        return None

    def _past_a_late_one(self, command: str, stale: str) -> str:
        """A late identity read in this question's place (_in_its_place): out of step, not garbled. The question was
        sent, and done, so its own reply comes next: it is read past the late one, nothing asked twice. A review of
        2026-11-15 found that asking again moved a stage twice (MOVE:REL 1;*OPC?) and lost the error the first
        SYST:ERR? had taken from the queue; a buffer's read (R?, DATA:REM?) would lose its readings. Its own reply not
        in time: put back in step (a device clear, then asked who it is), and asked once more only when it is a plain
        reading of a card's (reading), which only reads again; anything else raises OutOfStep, not asked again."""
        log.warning("%s: its identity (%r) came where %s's reply should be, a late reply to an earlier question: read "
                    "past it", self.name, stale.strip()[:60], command.strip())
        own = self._read_past(command)
        if own is not None:
            return own
        self._settle()   # owed: a device clear, then asked who it is, what is behind that dropped
        if getattr(_reading, "now", None) is None:
            raise OutOfStep("%s answers out of step: its own identity (%r), a late reply to an earlier question, came "
                            "where %s's reply should be, and that reply didn't follow. Kelvin put it back in step and "
                            "didn't ask %s again, as that may change what the instrument does"
                            % (self.name, stale.strip()[:60], command.strip(), command.strip()), command.strip(), stale)
        reply = self._query(command)
        if self._in_its_place(command, reply):
            self.owed = True
            raise OutOfStep("%s answers out of step: its reply to %s was its own identity (%r) again after Kelvin put "
                            "it back in step and asked once more, a late reply to an earlier question read in this "
                            "one's place" % (self.name, command.strip(), str(reply).strip()[:60]), command.strip(),
                            str(reply), asked_again=True)
        return reply

    def _failed(self, e: BaseException) -> None:
        """An exchange failed. One that ran out of time may still owe its reply (_timed_out). One whose session died
        (session_dead: a power cycle, a replug, a dropped LAN connection) marks it broken: it gave
        VI_ERROR_CONN_LOST to every exchange after, and nothing but switching it off and on brought it back (review
        of 0.3.45, 4.1). It is made anew between exchanges, checked first (reopen)."""
        from .instrument import GaveUp, Stopped
        if isinstance(e, Hung):   # its driver didn't return: on GPIB, most likely the adapter or the bus (hung_buses)
            self.hung_at = time.time()
        if isinstance(e, (Stopped, GaveUp)):   # a Stop, or a look, ended the wait, not the call: its late reply is
            self.owed = True                   # dropped before the next command
            return
        if timed_out(e):
            self._timed_out()
            return
        dead = session_dead(e)
        if dead:
            self.broken = dead

    def reopens_by_itself(self) -> bool:
        """Whether Kelvin may make its session anew without a person (the monitor's reconnector): when it can check that
        the instrument is the same one, by the identity its card asks, or its address names the device itself (USB
        with its serial number). Otherwise a person reconnects it (Lab panel, or reconnect_instrument)."""
        return (self._sync is not None and not self.write_only) or names_device(self.resource)

    def reopen(self, check: Optional[Callable[[str], Optional[str]]] = None, person: bool = False) -> str:
        """Its session made anew: when it died (broken) or is out of step (owed), between exchanges (under its lock:
        none goes meanwhile), never in the middle of one's sequence. The old one is closed, a new one opened, which
        sends the instrument nothing; what it may still owe is dropped (a device clear, or a serial line read until
        quiet); then it is asked who it is, and used only if it answers as itself, by its card's identity and
        check(identity) (the lab's identity_problem). Only then do its start-up commands go again (on_connect).
        person: a person asked for it, so a card with no identity reconnects wherever it is (reopens_by_itself).
        Returns the identity it answered ("" for a card without one). Raises while it can't, and stays broken: not
        there yet, or "answers as X now" when another instrument answers there."""
        with self.lock:
            if not person and not self.reopens_by_itself():
                raise AdapterError("%s: its card has no identity to check it by, and %s could be another instrument: "
                                   "once it is back, switch it off and on in the Lab panel, or reconnect it"
                                   % (self.name, self.resource))
            why = self.broken or ("out of step" if self.owed else "connected again")
            self.broken = why   # meanwhile nothing goes on it (an exchange is refused at once, not kept waiting)
            self._close_res()
            self._connect(open_timeout=REOPEN_MS)
            idn = ""
            try:
                if marks_message_end(self.resource):
                    clear = getattr(self.res, "clear", None)
                    if clear is not None:
                        clear()   # a device clear: what it may still owe is dropped; nothing is sent to it
                elif is_serial(self.resource):
                    self._quiet()
                if self._sync is not None and not self.write_only:
                    idn = self._ask_identity()[-1].strip()
                    problem = (None if re.search(self._sync[1], idn, re.I) else
                               "it answers as %r, not as its card's model" % idn[:60])
                    if problem is None and check is not None:
                        problem = check(idn)
                    if problem:
                        self.broken = "answers as %r now" % idn[:60]
                        wrong = AdapterError("%s answers again, but not as itself: %s. Kelvin sends it nothing until "
                                             "the instrument there is the one in the lab config" % (self.name, problem))
                        wrong.idn, wrong.problem = idn, problem   # what answered, for the lab to set it aside and ask
                        raise wrong
                self.broken, self.owed = "", False
                self._start()
            except BaseException:
                self.broken = self.broken or why
                res = getattr(self, "res", None)
                if isinstance(res, Session) and res._courier.dead:   # its first call never came back: none beside it
                    self._stuck_open = res._courier
                self._close_res()
                raise
            self.sessions += 1
            return idn

    def _timed_out(self) -> None:
        """An exchange ran out of time, so the instrument may still send its reply. A device clear drops it now, where
        the bus marks message ends (a Keithley 2400's manual: a query's reply must be read, or the device cleared);
        the rest happens before the next command (_settle)."""
        self.owed = True
        if marks_message_end(self.resource):
            try:
                self.res.clear()
            except Exception:
                pass   # the bus may not answer either: _settle clears it again before the next command

    def _settle(self) -> None:
        """Before a command that follows one that timed out: what the instrument may still owe is dropped (a device
        clear; a serial line read until it is quiet; a plain socket connected again), then it is asked who it is
        until it answers as itself, twice at most, so one stale reply is read off. Raises while it isn't back in step,
        and the command isn't sent. Nothing goes on a session that died (broken) until it is made anew (reopen)."""
        if self.broken:
            raise LinkLost("%s: its connection ended (%s); Kelvin connects to it again once it is back"
                           % (self.name, self.broken))
        if not self.owed:
            return
        self.owed = False   # owed again below while it isn't back in step; meanwhile its own commands go (_open's)
        try:
            self._back_in_step()
        except BaseException as e:
            self.owed = True
            dead = session_dead(e) if isinstance(e, Exception) else ""
            if dead:
                self.broken = dead
            raise

    def _back_in_step(self) -> None:
        if marks_message_end(self.resource):
            clear = getattr(self.res, "clear", None)
            if clear is not None:
                clear()   # a bus that still doesn't answer raises here
        elif is_serial(self.resource):
            self._quiet()
        else:   # a plain socket, connected again; its start-up commands go as on any connection (never a loop back)
            self._close_res()
            self._open()
        if self._sync is not None and not self.write_only:
            query, pattern = self._sync
            replies = self._ask_identity()
            if not re.search(pattern, replies[-1], re.I):
                raise AdapterError("%s is out of step: after a reply it didn't send in time, it answered %s to %s, "
                                   "not who it is. Kelvin cleared it; check it, then reconnect it"
                                   % (self.name, ", ".join(repr(r.strip()[:40]) for r in replies), query))

    def _ask_identity(self) -> List[str]:
        """Who it is, asked once within SYNC_MS, its replies read until its own: one stale reply in front of it is read
        off. Then what may still be queued behind its answer is dropped (_drop_behind): the stale reply in front may be
        an identity too, a late answer to an earlier *IDN? (the monitor's, or this check's own on a slow bus), taken for
        this one's, whose own then answered the next command (field report 2026-11-15: lockin's SLVL? got
        'Stanford_Research_Systems,SR830,s/n81118,ver1.07'). A stale reading read off, then nothing in time: its
        identity is late, the bus slow (a time out), not another instrument there. The replies, its answer last."""
        query, pattern = self._sync
        res, before = self.res, self.res.timeout
        replies: List[str] = []
        try:
            res.timeout = min(int(before or SYNC_MS), SYNC_MS)
            if self.enq or self.echo or self.ack or self.query_delay:
                replies.append(str(self._query(query)))
            else:   # asked once, its replies read until its own: one stale reply in front of it is read off
                replies.append(str(res.query(query)))
                while not re.search(pattern, replies[-1], re.I) and len(replies) < 3:
                    try:
                        replies.append(str(res.read()))
                    except Exception as e:
                        if timed_out(e) and all(only_numbers(r) for r in replies):
                            raise   # readings read off, its own identity not yet in: slow, as a time out
                        if timed_out(e):
                            break   # it answered, and nothing more comes: not as itself (said by the caller)
                        raise
            if re.search(pattern, replies[-1], re.I):
                self.said_as = replies[-1].strip()
                self._drop_behind()
        except Exception as e:
            if timed_out(e):
                self._timed_out()
            raise
        finally:
            if self.res is res:
                res.timeout = before
        return replies

    def _drop_behind(self) -> None:
        """What may still be queued behind an answer just read, dropped: a device clear where the bus marks message
        ends; on a serial line, what comes until it is quiet. A plain socket checked was connected anew, so nothing of
        before is behind it."""
        if marks_message_end(self.resource):
            clear = getattr(self.res, "clear", None)
            if clear is not None:
                clear()
        elif is_serial(self.resource):
            self._quiet()

    def _quiet(self) -> None:
        """A serial line read until nothing more comes (0.3 s), at most 2 s: a late reply's bytes, dropped."""
        import time
        res, before, end = self.res, self.res.timeout, time.monotonic() + 2.0
        try:
            res.timeout = 300
            while time.monotonic() < end:
                res.read_raw()
        except Exception:
            pass   # quiet
        finally:
            res.timeout = before

    def ask_after_timeout(self, command: str) -> Optional[str]:
        """One quick question (its error queue) right after an exchange that timed out, once the instrument is back
        in step: its reply, or None when it isn't, or doesn't answer within a second."""
        if self.write_only or self.enq or self.echo or self.ack or BUSES.text(self.bus):
            return None
        with self.lock:
            res = self.res
            before = res.timeout
            try:
                res.timeout = min(int(before or AFTER_MS), AFTER_MS)
                self._settle()
                reply = str(self._query(command))
                if self._in_its_place(command, reply):   # a late identity in front of its error queue's reply: read
                    return self._read_past(command)       # past it, the error it took never lost (None: owed)
                return reply
            except Exception as e:
                if timed_out(e):
                    self.owed = True   # cleared before the next command
                elif not self.broken and session_dead(e):
                    self.broken = session_dead(e)
                return None
            finally:
                if self.res is res:
                    res.timeout = before

    def query_trace(self, command: str) -> str:
        """A trace's reply, read to the end of its message where the bus marks one, not to the first line end: some
        instruments send a line per point (HP's network analyzers). On a plain socket or a serial line, where only a
        line end marks it, the reply ends there as usual."""
        if not marks_message_end(self.resource) or self.write_only or self.enq or self.echo or self.ack:
            with self.lock, long_calls(self.res):
                return self.query(command)
        with self.lock:
            res = self.res
            before = res.read_termination
            try:   # the setting inside: one a Stop ends the wait for is still made, and is put back here
                res.read_termination = ""   # read to EOI (GPIB) or the message's end (USB, LAN)
                with long_calls(res):
                    return self.query(command)
            finally:
                if self.res is res:
                    res.read_termination = before

    def query_waiting(self, command: str, seconds: float, stopped: Callable[[], bool]) -> str:
        """A query the instrument answers only once it has finished something long (*OPC? after a sweep): asked once,
        then its answer awaited a moment at a time, so a person's stop ends the wait at once (Stopped) instead of
        when the sweep would have."""
        if self.write_only or self.enq or self.echo or self.ack or self.query_delay:
            return self.query(command)
        import time
        from .instrument import Stopped
        self._bus_ok()
        with self.lock:
            self._settle()
            res = self.res
            before, end = res.timeout, time.monotonic() + seconds
            try:
                try:   # its write too: one that times out or finds the session dead is handled as any exchange's
                    res.write(command)
                except Exception as e:
                    self._failed(e)
                    raise
                res.timeout = WAIT_SLICE_MS
                while True:
                    try:
                        reply = str(res.read())
                    except Stopped:   # a person's Stop ended the wait for this slice: the instrument is cleared now
                        with stoppable(lambda: False):   # behind the slice's read (a second at most), never cut short
                            self._abandon()
                        raise
                    except Exception as e:
                        if not timed_out(e):
                            self._failed(e)
                            raise
                    else:   # a late identity in front of this one's answer (its own still to come): read on past it
                        if not self._in_its_place(command, reply):
                            return reply
                        log.warning("%s: a late identity came before %s's answer: read on past it", self.name, command)
                    if stopped():
                        self._abandon()
                        raise Stopped("%s: stopped by a person while it was still working on %s" % (self.name, command))
                    if time.monotonic() >= end:
                        self._timed_out()   # the answer it still owes is dropped, never taken for a later command's
                        raise AdapterError("%s did not answer %s within %.0f s" % (self.name, command, seconds))
            finally:
                if self.res is res:   # a connection made again after a stop keeps its own timeout
                    res.timeout = before

    def _abandon(self) -> None:
        """After a stop cut a wait short, the answer the instrument still owes must never be taken for a later
        command's: a bus that marks message ends gets a device clear (which also ends the wait on the instrument);
        a plain socket is connected again, which drops it; a serial line can only be emptied of what came so far,
        and the instrument answers when its sweep ends: either way it is put back in step before the next command,
        its late reply read off (owed, _settle). A serial *OPC? cut by a Stop answered "1" to the next query
        (review of 0.3.45, 4.6)."""
        if marks_message_end(self.resource) or is_serial(self.resource):
            self.drain()
            self.owed = True
            return
        self._close_res()
        try:
            self._open()
        except Exception as e:
            self.owed = True   # the next command connects it again, or says the instrument can't be reached
            dead = session_dead(e)
            if dead:
                self.broken = dead

    def drain(self) -> None:
        """Drop the rest of a reply Kelvin could not read (a binary block cut at a line-end byte), so the next reply
        is the next command's: a device clear, or on a serial line, reading until nothing more comes. A plain socket
        is connected again instead, which drops it: pyvisa-py's clear() of a socket whose instrument had closed it read
        for ever (review of 0.3.45, 4.6)."""
        import time
        with self.lock:
            if not marks_message_end(self.resource) and not is_serial(self.resource):
                self._close_res()
                try:
                    self._open()
                except Exception as e:
                    self.owed = True   # the next command connects it again first
                    dead = session_dead(e)
                    if dead:
                        self.broken = dead
                return
            try:
                self.res.clear()
                return
            except Exception:
                pass
            before, end = self.res.timeout, time.monotonic() + 5.0
            try:
                self.res.timeout = 300
                while time.monotonic() < end:
                    self.res.read_raw()
            except Exception:
                pass   # nothing more within 0.3 s: drained
            finally:
                self.res.timeout = before

    def _query(self, command: str) -> str:
        if self.enq:   # acknowledged, then the reply on <ENQ>
            self._enq_ack(command)
            self.res.write_raw(b"\x05")
            return str(self.res.read())
        if self.echo:
            self.res.write(command)
            self.res.read()   # the command, echoed
            if self.query_delay:
                import time
                time.sleep(self.query_delay)
            reply = str(self.res.read())
        elif self.query_delay:
            reply = str(self.res.query(command, delay=self.query_delay))
        else:
            reply = str(self.res.query(command))
        if self.ack:
            self._check_ack(command, reply)
        return reply

    def query_binary(self, command: str, binary: str) -> List[float]:
        """A trace the instrument sends as an IEEE 488.2 block of binary numbers (an oscilloscope's waveform in BYTE
        or WORD format): binary is the card's "int16", or "int16 little" when the lowest byte comes first."""
        from .cards import BINARY
        if self.write_only or self.enq or self.echo:
            raise AdapterError("%s: binary traces need an instrument that answers queries plainly" % self.name)
        number, _, order = binary.partition(" ")
        self._bus_ok()
        with self.lock:
            self._settle()
            try:
                with long_calls(self.res):
                    values = self.res.query_binary_values(command, datatype=BINARY[number],
                                                          is_big_endian=order.strip() != "little", container=list)
            except (ValueError, TypeError) as e:   # its header or length didn't read: the rest of it is dropped,
                self.drain()                        # never read as the next command's reply
                raise AdapterError("%s: %r did not answer with a block of %s numbers (%s)" % (self.name, command,
                                                                                            number, e)) from e
            except Exception as e:
                self._failed(e)
                raise
        return [float(v) for v in values]

    def close(self) -> None:
        """Its session closed: under its lock when that comes within CLOSE_CALL_S; else beside the exchange that has it
        (switching off or clearing a bus never waits on a call that doesn't return), which then fails, its session gone."""
        held = self.lock.acquire(timeout=CLOSE_CALL_S)
        try:
            if not held:
                self.broken = self.broken or "closed while an exchange still had it"
            self._close_res()
        finally:
            if held:
                self.lock.release()


def open_backend(name: str, card: Card, address: str, rm: Optional[Any] = None,
                 overrides: Optional[Dict[str, Any]] = None) -> Any:
    """VISA for addresses like GPIB0::24::INSTR or serial://COM3; Kelvin's own Prologix support for prologix://; a
    vendor driver for zhinst:// or qd://."""
    from .drivers import is_driver_address, open_driver
    from .prologix import MANAGER, is_prologix
    if is_driver_address(address):
        return open_driver(name, card, address)
    if is_prologix(address):   # GPIB through the adapter: VISA isn't needed, and doesn't know these addresses
        return VisaBackend(name, card, address, rm if rm is not None else MANAGER, overrides)
    return VisaBackend(name, card, address, rm, overrides)
