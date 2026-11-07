"""Watching sensitive instruments: readings that leave their range, instruments that stop answering or report
errors. A watch only reads. When it trips it raises an alarm once (and clears it when the reading is back), tells
people (see notify.py), and, for watches written in the lab config, can stop the running plan and send named drives to
their safe values - the only kind of change it may make.

An instrument that stops answering raises one alarm in plain words, whoever met it (a watch, a run, the monitor's own
check that each real instrument still answers): which instrument, on which bus, what to check ("nvm (Keithley 2182A)
doesn't answer on GPIB (address 7): cable or power?"). It clears when the instrument answers again. Replies that
arrive garbled are one comms fault, latched: raised once, cleared only after its replies have been clean for a while.

The gate tells the monitor what it met while changing something (gate.alarm): a guard's trip, and a wait for a set
point that gave up. Those alarms are latched: they stay on until a person says Got it.

Every alarm is kept with its history: when it went up, how often (a repeat that nobody has looked at yet folds into it,
with a count, instead of a new alarm and a new notice), when and why it cleared, and who said Got it, where and when.
A watch's alarm clears only once its reading is back past its threshold by more than its noise (hysteresis): a
reading sitting at its threshold raised ten alarms in 90 s, each a notice and a push.

Durations are in lab time (the lab clock), so tests on a simulated clock behave like the real lab.
"""
from __future__ import annotations

import itertools
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .instrument import Busy, Garbled, InstrumentLost, SensorError, patience

FAILS_BEFORE_ALARM = 3       # failed exchanges in a row before "doesn't answer" (one that can't reach it at all: one)
WAIT_S = 1.0                 # the longest the monitor waits for an instrument whose connection other work has (an
                             # acquisition, a wait for *OPC?): then it is busy, not read this round. A network
                             # analyzer's 20-minute sweep held the monitor, and every watch, interlock and alarm with it
FRESH_S = 15.0               # seconds: another's latest reading of a busy instrument (the running work's own) stands in
                             # for the monitor's own when at most this old (three interlock checks)
UNREAD_S = 300.0             # lab seconds a watched reading may go unread, its instrument busy, before people hear of it
INTERLOCK_UNREAD_S = 60.0    # lab seconds an interlock may go unchecked, its reading's instrument busy, before it fails
                             # as one that can't be read does: twelve checks, and the minute the heartbeat allows
LATE_S = 30.0                # seconds one look at the lab may take before "Kelvin's watching is held up" goes up
LATE_EVERY = 2.0             # how often (seconds) the watchdog thread looks whether a look is taking too long
SAFE_WAIT_S = 2.0            # seconds the monitor waits for a to-safe it started before it says the alarm: one that
                             # takes longer (a ramp) goes on on its own thread, and says how it ended in the alarm
SAFE_STOP_S = 10.0           # seconds stopping the monitor (quitting, a lab switch) waits for to-safes under way
ERROR_CHECK_EVERY = 60.0     # lab seconds between error-queue checks on real instruments
BUS_PROBE_S = 30.0           # how often one instrument on a hung GPIB bus is asked whether it answers again
ALIVE_EVERY = 20.0           # lab seconds between checks that each real instrument answers, while a run or queue goes
RESET_EVERY = 5.0            # lab seconds between checks that the outputs the running plan switched on are still on
GARBLE_CLEAR_S = 300.0       # lab seconds without a garbled reply before a comms fault clears
INTERLOCK_EVERY = 5.0        # lab seconds between checks of the interlocks whose drive is away from safe
INTERLOCK_CLEAR_S = 60.0     # lab seconds a broken interlock's condition must hold again before its alarm clears
STALL_S = 300.0              # lab seconds a set point's reading may go without getting closer before the alarm
SETPOINT_EVERY = 15.0        # lab seconds between the set-point watchdog's looks
FOLD_S = 600.0               # lab seconds: the same alarm again within this of its clearing, unseen, folds into it
CLEAR_BAND = 0.01            # a watch's alarm clears once back past its threshold by this share of it (or 3x its noise)
KEEP_SEEN = 50               # alarms people said Got it to that are over: the newest this many are kept (no other goes)
_ids = itertools.count(1)


class Approach:
    """A reading on its way to a set point, and how long since it last got closer by more than its noise explains:
    half the set point's tolerance, or a thousandth of the way still to go, whichever is more (but at most a quarter of
    what the rate covers in `window`, so a slow ramp still counts as moving). A cryostat or a magnet that stops on
    the way (out of helium, a heater or compressor that quit) stops getting closer; one ramping slowly doesn't.
    Shared by the set-point watchdog here, a plan's ramp (engine.py) and the gate's wait for a set point."""

    def __init__(self, target: float, tol: float, now: float, per_s: Optional[float] = None,
                 window: Optional[float] = None):
        self.target, self.tol, self.since = float(target), float(tol or 0.0), now
        self.cap = 0.25 * per_s * window if per_s and window and per_s > 0 else math.inf
        self.best: Optional[float] = None   # the closest it has been
        self.last: Any = None
        self.arrived = False

    def there(self, value: Any) -> bool:
        return _number(value) and abs(value - self.target) <= self.tol

    def update(self, value: Any, now: float) -> float:
        """Take in a reading; lab seconds since it last got closer. A reading that isn't a number is no progress."""
        self.last = value
        if _number(value):
            d = abs(value - self.target)
            margin = min(max(0.5 * self.tol, 1e-3 * (self.best or d)), self.cap)
            if self.best is None or d < self.best - max(margin, 1e-12):
                self.best, self.since = d, now
        return now - self.since


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _minutes(seconds: float) -> str:
    return "%.0f s" % seconds if seconds < 90 else "%.0f min" % (seconds / 60)


def rate_per_s(inst: Any, spec: Any, rate: Any = None) -> Optional[float]:
    """How fast a set point ramps, in its unit per second: `rate` (in its rate parameter's unit), else what its rate
    parameter reads; None when its card names none or it can't be read."""
    try:
        if rate is None and spec.rate_param:
            rate = inst.read(spec.rate_param)
    except Exception:
        return None
    if not _number(rate) or rate <= 0:
        return None
    return float(rate) / (60.0 if spec.rate_per == "min" else 1.0)


def _dur(s: float) -> str:
    """A duration in words: "40 s", "3 min", "2.5 h"."""
    s = max(0.0, s)
    return "%.0f s" % s if s < 90 else "%.0f min" % (s / 60) if s < 5400 else "%.1f h" % (s / 3600)


def _since(t: float) -> str:
    """How long ago a wall time was, in words."""
    return _dur(time.time() - t)


def _at(t: Optional[float]) -> str:
    return time.strftime("%H:%M", time.localtime(t)) if t else "?"


def switch_words(inst: Any, spec: Any) -> str:
    """The switch a set point reaches the sample through, when it may be why the reading doesn't get there: "tc.
    heater_range is off, so nothing drives T_sample there", or "tc.heater_range is only low: a higher range may be
    needed" (a range below its highest). "" otherwise, or when it can't be read. A heater left off was reported as
    "the instrument may be stuck"."""
    switch = inst.card.params.get(spec.applies_when) if spec.applies_when else None
    if switch is None:
        return ""
    try:
        now = inst.read(switch.name)
        key = "%s.%s" % (inst.name, switch.name)
        if switch.distance(now) == 0:
            return "%s is %s, so nothing drives %s there" % (key, switch.show(now), spec.readback or "it")
        if switch.type == "enum" and switch.distance(now) < max(switch.distance(v) for v in switch.values or {}):
            return "%s is only %s: a higher range may be needed" % (key, switch.show(now))
    except Exception:
        pass
    return ""


@dataclass
class Watch:
    key: str                                   # instr.param to read
    above: Optional[float] = None
    below: Optional[float] = None
    for_s: float = 0.0                         # the condition must hold this long before it alarms
    every: float = 10.0                        # lab seconds between reads
    then: List[Tuple[str, str, Any]] = field(default_factory=list)   # drives to send to safe on alarm
    stop_run: bool = False                     # also stop the running plan (lab-config watches with actions)
    source: str = "person"                     # lab | person | agent
    note: str = ""
    id: str = field(default_factory=lambda: "w%d" % next(_ids))
    device: str = ""                           # the sample it was made for: it ends when that sample is taken out
    # state
    value: Any = None
    since: Optional[float] = None              # lab time the condition became true
    last_read: Optional[float] = None
    alarm: Optional[str] = None                # the active alarm's id
    recent: List[float] = field(default_factory=list)   # its latest readings: their noise sets the band
    unread_since: Optional[float] = None       # lab time since when it goes unread, its instrument busy
    unread_alarm: Optional[str] = None         # the alarm that says it has gone unread too long

    def rule(self, unit: str = "") -> str:
        u = " " + unit if unit else ""
        parts = []
        if self.above is not None:
            parts.append("above %g%s" % (self.above, u))
        if self.below is not None:
            parts.append("below %g%s" % (self.below, u))
        return "%s %s%s" % (self.key, " or ".join(parts), " for %gs" % self.for_s if self.for_s else "")

    def band(self) -> float:
        """How far back past its threshold the reading must come before the alarm clears: 3x the reading's noise (from
        the lower median of the changes between its latest values, so the jump that set off the alarm doesn't count as
        noise), between CLEAR_BAND and ten times that of the threshold (of the range, for a threshold at 0); never
        more than a quarter of the range between above and below."""
        r = self.recent
        steps = sorted(abs(b - a) for a, b in zip(r, r[1:]))
        # the median |change| of Gaussian noise is 0.954 σ; too few readings tell nothing
        noise = steps[(len(steps) - 1) // 2] / 0.954 if len(steps) >= 3 else 0.0
        both = self.above is not None and self.below is not None
        scale = max(abs(x) for x in (self.above, self.below) if x is not None)
        if not scale and both:
            scale = self.above - self.below
        band = max(3 * noise, CLEAR_BAND * scale)
        if scale:
            band = min(band, 10 * CLEAR_BAND * scale)
        return min(band, (self.above - self.below) / 4) if both else band

    def view(self, unit: str = "") -> Dict[str, Any]:
        return {"id": self.id, "key": self.key, "above": self.above, "below": self.below, "for_s": self.for_s,
                "every": self.every, "source": self.source, "note": self.note, "rule": self.rule(unit),
                "then": ["%s.%s %s" % t for t in self.then], "value": self.value, "alarm": bool(self.alarm),
                "device": self.device}


def _and(names: List[str]) -> str:
    """"a", "a and b", "a, b and c"."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


@dataclass
class Alarm:
    id: str
    kind: str          # reading | silent | error | interlock | stall | comms | reset | trip
    key: str
    text: str
    raised: float      # wall time
    actions: List[str] = field(default_factory=list)
    cleared: Optional[float] = None
    acknowledged: bool = False
    # its history: how often it went up (a repeat nobody had seen folds into it), the latest time, why it cleared, and
    # who said Got it (their name and where: "Ada (phone, Pixel 7)") and when. A latched alarm (a guard's trip, a wait
    # that gave up) stays on until a person says Got it.
    count: int = 1
    last: Optional[float] = None
    cleared_text: str = ""
    acknowledged_by: str = ""
    acknowledged_at: Optional[float] = None
    latched: bool = False
    source: str = field(default="", repr=False)   # what raised it (a watch, an interlock, a drive): repeats fold by it

    @property
    def latest(self) -> float:
        """When it last went up (wall time)."""
        return self.last or self.raised

    def view(self) -> Dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "key": self.key, "text": self.text, "actions": self.actions,
                "raised": self.raised, "cleared": self.cleared, "acknowledged": self.acknowledged,
                "count": self.count, "last": self.latest, "cleared_text": self.cleared_text,
                "acknowledged_by": self.acknowledged_by, "acknowledged_at": self.acknowledged_at,
                "latched": self.latched, **({"bus": self.key} if self.source == "bus" else {})}

    @classmethod
    def restore(cls, v: Dict[str, Any]) -> "Alarm":
        """One kept in alarms.json (view()), back as an Alarm. KeyError, TypeError or ValueError for one that isn't."""
        def when(k: str) -> Optional[float]:
            return float(v[k]) if v.get(k) is not None else None
        return cls(str(v["id"]), str(v["kind"]), str(v["key"]), str(v["text"]), float(v["raised"]),
                   [str(x) for x in v.get("actions") or []], when("cleared"), bool(v.get("acknowledged")),
                   max(1, int(v.get("count") or 1)), when("last"), str(v.get("cleared_text") or ""),
                   str(v.get("acknowledged_by") or ""), when("acknowledged_at"), bool(v.get("latched")))

    def line(self) -> str:
        """Its history in one line: "02:13 tc.T_sample = 5.001 K is above 5 K (3 times, last 02:20); cleared 02:31
        (back in range); Got it: Ada (phone, Pixel 7) 07:30"."""
        bits = ["%s %s%s" % (_at(self.raised), self.text, " (%d times, last %s)" % (self.count, _at(self.last))
                              if self.count > 1 else "")]
        if self.cleared:
            bits.append("cleared %s%s" % (_at(self.cleared), " (%s)" % self.cleared_text if self.cleared_text else ""))
        else:
            bits.append("still on" + (" until a person says Got it" if self.latched else ""))
        if self.acknowledged:
            bits.append("Got it: %s%s" % (self.acknowledged_by or "a person",
                                          " %s" % _at(self.acknowledged_at) if self.acknowledged_at else ""))
        return "; ".join(bits)


class _Safing:
    """A drive the monitor sends to safe on a thread of its own (a watch's action, an interlock's): what the gate said,
    as (ok, words), once it is done."""

    def __init__(self, key: str, alarm: Alarm):
        self.key, self.alarm = key, alarm
        self.going = "%s going to safe" % key   # its alarm's words until it is done
        self.said: List[Tuple[bool, str]] = []
        self.finished = False   # the gate is done with it (said is in)
        self.late = False       # its alarm went out before that: it says how it ended there itself
        self.done = threading.Event()   # finished, and said so where it had to


class Monitor:
    """Polls watches on a background thread (start/stop), or one pass at a time (tick) in tests.

    Nothing an instrument does holds it up. Its exchanges wait a moment at most for a connection other work has (an
    acquisition, a wait for *OPC?): such an instrument is busy, not read this round, and its latest reading by that
    work stands in while fresh (WAIT_S, FRESH_S). A watch unread for long says so (UNREAD_S); an interlock unchecked
    for long fails as one that can't be read (INTERLOCK_UNREAD_S). A drive it sends to safe goes there on a thread of
    its own (SAFE_WAIT_S). A watchdog thread says when one look at the lab takes too long (LATE_S), naming what holds
    it up, while it still does. in_use(name): whether the work under way uses that instrument (the runtime's lab
    lock), for a maker's driver whose connection has no lock to wait on."""

    def __init__(self, lab: Any, gate: Any, notify: Callable[[str, str, str], None], poll_s: float = 1.0,
                 in_use: Optional[Callable[[str], bool]] = None):
        self.lab = lab
        self.gate = gate
        self.notify = notify
        self.poll_s = poll_s
        self.in_use = in_use
        self.watches: Dict[str, Watch] = {}
        # wall time of the latest sign of life: a pass began, or a check in one (the heartbeat pings only while there
        # is one within the last minute: watching)
        self.last_tick: Optional[float] = None
        self._pass_began: Optional[float] = None   # wall time the look at the lab under way began
        self._doing: Optional[str] = None          # what it is doing now, in words, and since when (wall time)
        self._doing_since: Optional[float] = None
        self._slowest: Tuple[str, float] = ("", 0.0)   # the slowest check of this look, and how long it took
        self._late: Optional[Alarm] = None         # "Kelvin's watching is held up", while it is
        self._record: Optional[Alarm] = None       # "the lab's record can't be written", while it can't
        self._unchecked: Dict[int, float] = {}     # interlocks not checked lately, their reading busy: since (lab time)
        self._safing: Dict[str, _Safing] = {}      # the latest to-safe of each drive (instr.param)
        self._safe_last: Dict[str, _Safing] = {}   # and of each instrument: its next waits for it
        self._watchdog: Optional[threading.Thread] = None
        self.alarms: List[Alarm] = []        # newest first, active and cleared
        self._fails: Dict[str, int] = {}
        self._unreadable: Dict[str, Alarm] = {}   # a watch's reading that fails though its instrument answers
        self._silent: Dict[str, Alarm] = {}       # instruments that don't answer (from their health)
        self._reset: Dict[str, Alarm] = {}        # outputs found off under a running plan (a power cut resets them)
        self._errors: Dict[str, Alarm] = {}
        self._last_error_check: Dict[str, float] = {}
        self._last_alive_check: Dict[str, float] = {}
        self._last_reset_check: Dict[str, float] = {}
        self._suspect: Dict[str, float] = {}      # output switches read off under a running plan once: since when
        self._comms: Dict[str, Alarm] = {}        # instruments whose replies arrive garbled (latched)
        self._buses: Dict[str, Alarm] = {}        # GPIB buses that stopped answering, by bus
        self._alive_asked: Dict[str, float] = {}  # when each was last found answering (wall time) by _check_alive
        self._bus_probe_at: Dict[str, float] = {}
        self._garbles_seen: Dict[str, int] = {}   # garbled replies already counted, by instrument
        self._garbled_lab: Dict[str, float] = {}  # lab time the latest garbled reply was seen
        self._last_interlock_check = -1e18
        self._broken: Dict[int, Alarm] = {}   # interlocks found broken (by their place in the lab config)
        self._holding: Dict[int, float] = {}  # of those, the ones whose condition holds again: since when (lab time)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # the set-point watchdog: set points on their way (by drive, instr.param), and how long without getting closer
        self.stall_s = STALL_S
        self._moves: Dict[str, Approach] = {}
        self._stalled: Dict[str, Alarm] = {}
        self._last_setpoint_check = -1e18
        self._cleared_lab: Dict[str, float] = {}   # lab time each alarm cleared: a repeat soon after folds into it
        self._back: set = set()   # repeats that came back after people were told their alarm cleared: told again
        # an alarm changed without a notice to people (a repeat folded into it, a Got it): the runtime keeps the history
        self.on_change: Callable[[], None] = lambda: None
        try:   # the gate's trips and the waits it gave up on are alarms too (from_gate)
            gate.alarm = self.from_gate
        except AttributeError:
            pass

    # ------------------------------------------------------------------ watches

    def add(self, watch: Watch) -> Watch:
        self.lab.resolve(watch.key)  # KeyError for an unknown reading
        if watch.above is None and watch.below is None:
            raise ValueError("a watch needs above, below or both")
        if watch.above is not None and watch.below is not None and watch.below >= watch.above:
            raise ValueError("below must be smaller than above (it alarms outside the range)")
        with self._lock:
            self.watches[watch.id] = watch
        return watch

    def remove(self, watch_id: str, source: Optional[str] = None, why: str = "") -> Watch:
        """Stop a watch; its alarm, if on, clears with it (it once stayed on, and came back after a restart), and so
        does its instrument's "can't be read" once no watch reads that instrument (nothing else could end it). Watches
        from the lab config change only there (PermissionError when a source is given)."""
        unread = None
        with self._lock:
            w = self.watches.get(watch_id)
            if w is None:
                raise KeyError("no watch %r" % watch_id)
            if source is not None and w.source == "lab":
                raise PermissionError("%s comes from the lab config; change it there" % watch_id)
            self.watches.pop(watch_id)
            instr = w.key.split(".")[0]
            if not any(x.key.split(".")[0] == instr for x in self.watches.values()):   # no watch reads it now
                self._fails.pop(instr, None)
                unread = self._unreadable.pop(instr, None)
        if w.alarm:
            self._clear(w.alarm, "%s is no longer watched (%s)" % (w.key, why or "the watch was removed"))
            w.alarm = None
        self._read_again(w, "%s is no longer watched (%s)" % (w.key, why or "the watch was removed"))
        if unread is not None:
            self._clear(unread.id, "%s is no longer read: no watch is on it (%s)" % (instr, why or
                                                                                   "its watch was removed"))
        return w

    def end_for(self, device: str, why: str) -> List[Watch]:
        """The watches made for a sample, ended when it is taken out (a sample's watch outlived it, and alarmed on the
        next one): removed, their alarms cleared. Returns them."""
        ended = [w for w in list(self.watches.values()) if device and w.device == device and w.source != "lab"]
        for w in ended:
            self.remove(w.id, why=why)
        return ended

    def list(self) -> List[Dict[str, Any]]:
        out = []
        for w in self.watches.values():
            try:
                unit = self.lab.resolve(w.key)[1].unit
            except KeyError:
                unit = ""
            view = w.view(unit)
            view["then"] = ["%s.%s %s" % (i, p, self._show("%s.%s" % (i, p), v)) for i, p, v in w.then]
            inst = self.lab.instruments.get(w.key.split(".")[0])
            view["paused"] = inst is not None and not inst.enabled   # switched off: nothing is read, so not watched
            out.append(view)
        return out

    def active(self) -> List[Alarm]:
        return [a for a in self.alarms if a.cleared is None]

    def unseen(self) -> List[Alarm]:
        """The alarms that cleared before anyone said Got it (overnight, say): shown until someone does."""
        return [a for a in self.alarms if a.cleared is not None and not a.acknowledged]

    def lines(self, most: int = 5) -> List[str]:
        """The alarms a person should hear of, for lab_status and the watches tool: those on, then the latest that went
        up and cleared while nobody looked (at most `most` of them, and how many more)."""
        out = ["ALARM: %s%s%s" % (a.text, " (%s)" % "; ".join(a.actions) if a.actions else "",
                                  " (%d times)" % a.count if a.count > 1 else "") for a in self.active()]
        unseen = self.unseen()
        out += ["ALARM (cleared, nobody has said Got it yet): %s" % a.line() for a in unseen[:most]]
        if len(unseen) > most:
            out.append("and %d more alarms that cleared before anyone said Got it (the Lab panel's Alarm history)"
                       % (len(unseen) - most))
        return out

    def acknowledge(self, alarm_id: str, by: str = "") -> Optional[Alarm]:
        """A person's Got it, kept with the alarm: who (their name and where), and when. A latched alarm (a guard's
        trip, a wait that gave up) ends here, also one that latched after a Got it (else nothing could end it); one
        whose condition still holds stays on, seen. None: no such alarm."""
        with self._lock:
            a = next((x for x in self.alarms if x.id == alarm_id), None)
            if a is None:
                return None
            now = time.time()
            a.just_acknowledged = not a.acknowledged   # a second Got it (three in 0.7 s) is the same one: kept once
            if not a.acknowledged:
                a.acknowledged, a.acknowledged_by, a.acknowledged_at = True, by, now
            if a.latched and a.cleared is None:
                a.cleared, a.cleared_text = now, "Got it from %s" % (by or "a person")
                self._cleared_lab[a.id] = self.lab.clock.now()
        self.on_change()
        return a

    # ------------------------------------------------------------------ the loop

    def start(self) -> "Monitor":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="kelvin-monitor", daemon=True)
            self._thread.start()
        if self._watchdog is None:
            self._watchdog = threading.Thread(target=self._watch_the_watcher, name="kelvin-monitor-watchdog",
                                              daemon=True)
            self._watchdog.start()
        return self

    def stop(self) -> None:
        """No more looks at the lab. The drives on their way to safe get there first (quitting, or a lab switch,
        closes their connections next), SAFE_STOP_S at most; one still going then is said in the record."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._watchdog is not None:
            self._watchdog.join(timeout=5)
            self._watchdog = None
        end = time.monotonic() + SAFE_STOP_S
        with self._lock:
            going = [h for h in self._safing.values() if not h.done.is_set()]
        for h in going:
            if not h.done.wait(max(0.0, end - time.monotonic())):
                self.gate.log.write("alarm_actions", by="monitor", alarm=h.alarm.id, target=h.key, ok=False,
                                    actions=["%s was still on its way to safe as Kelvin stopped watching" % h.key])

    def _run(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                self.tick()
            except Exception:  # the monitor must outlive any one bad read
                pass

    def watching(self) -> bool:
        """Is the monitor going: a look at the lab, or a check in one, began lately (the heartbeat pings only then). A
        check held up for over a minute stops it: then nothing is watched."""
        return self.last_tick is not None and time.time() - self.last_tick < max(60.0, 20 * self.poll_s)

    def tick(self) -> None:
        """One look at the lab: the watches, the set points, the interlocks, the error queues, whether each instrument
        answers, the buses, the instruments' health and the lab's record, each on its own: one that fails never keeps
        the others from their turn (its error is raised once they have had it)."""
        began = self.last_tick = self._pass_began = time.time()
        self._doing, self._doing_since, self._slowest = None, None, ("", 0.0)
        now = self.lab.clock.now()
        failed: List[BaseException] = []
        try:
            with patience(WAIT_S, self.in_use):   # an instrument that other work has is busy, not waited for
                for doing, check, args in (("reading the watches", self._check_watches, (now,)),
                                           ("following the set points", self._check_setpoints, (now,)),
                                           ("checking the interlocks", self._check_interlocks, (now,)),
                                           ("reading the error queues", self._check_errors, (now,)),
                                           ("checking that the instruments answer", self._check_alive, (now,)),
                                           # first: a hung bus is one alarm, and its instruments' own don't go up
                                           ("checking the GPIB buses", self._check_buses, ()),
                                           ("checking the instruments' health", self._check_health, (now,)),
                                           ("checking the lab's record", self._check_record, ())):
                    self._at(doing)
                    try:
                        check(*args)
                    except Exception as e:   # the next check has its turn all the same
                        failed.append(e)
            self._at(None)
        finally:
            self._pass_began = None
        self._in_time(time.time() - began)
        if failed:
            raise failed[0]

    def _at(self, doing: Optional[str]) -> None:
        """What this look at the lab is doing now (None: done): a sign of life for the heartbeat, and what the watchdog
        names if it takes too long. Keeps the slowest so far."""
        now = time.time()
        if self._doing is not None and self._doing_since is not None and now - self._doing_since > self._slowest[1]:
            self._slowest = (self._doing, now - self._doing_since)
        self._doing, self._doing_since = doing, now
        self.last_tick = now

    def _watch_the_watcher(self) -> None:
        while not self._stop.wait(LATE_EVERY):
            try:
                self._check_late()
            except Exception:   # the watchdog outlives anything too
                pass

    def _check_late(self, now: Optional[float] = None) -> None:
        """The watchdog's look, on a thread of its own: a look at the lab under way for LATE_S or more raises "Kelvin's
        watching is held up", naming what holds it up, while it still does (the look can't say it itself: it is what
        is held up)."""
        began, doing, since = self._pass_began, self._doing, self._doing_since
        now = time.time() if now is None else now
        if began is None or now - began < LATE_S or self._late is not None:
            return
        self._late_up("Kelvin's watching is held up: %s has taken %s so far" % (
            doing or "one look at the lab", _dur(now - (since or began))),
            "Until it is done, no watch is read, no interlock is checked and no other alarm goes up.")

    def _in_time(self, took: float) -> None:
        """A look at the lab that took LATE_S or more is said (if the watchdog hasn't already); the next one that
        doesn't clears it."""
        if took >= LATE_S:
            if self._late is None:
                what, slow = self._slowest
                self._late_up("Kelvin's watching fell behind: one look at the lab took %s%s" % (
                    _dur(took), " (%s took %s)" % (what, _dur(slow)) if what else ""),
                    "Watches and interlocks were checked late meanwhile.")
            return
        with self._lock:
            late, self._late = self._late, None
        if late is not None:
            self._clear(late.id, "Kelvin's watching is on time again (one look at the lab took %s)" % _dur(took))

    def _late_up(self, text: str, detail: str) -> None:
        with self._lock:
            if self._late is not None:
                return
            a = self._late = self._new("delay", "watching", text, source="monitor")
        self._tell(a, detail)

    def _fresh(self, inst: Any, param: str, ask: Callable[[], Any]) -> Any:
        """ask() now; while its instrument is busy with other work (Busy), that work's own latest reading of it, if at
        most FRESH_S old. Busy when there is none."""
        try:
            return ask()
        except Busy:
            got = inst.seen(param, FRESH_S)
            if got is None:
                raise
            return got[0]

    def _read(self, instr: str, param: str) -> Any:
        """A reading for the monitor (_fresh): asked now, or the running work's own while fresh. Busy otherwise."""
        inst = self.lab.instruments.get(instr)
        if inst is None:
            return self.lab.read(instr, param)   # says which instruments there are
        return self._fresh(inst, param, lambda: self.lab.read(instr, param))

    def _check_watches(self, now: float) -> None:
        with self._lock:
            watches = list(self.watches.values())
        for w in watches:
            if w.last_read is not None and now - w.last_read < w.every:
                continue
            w.last_read = now
            instr = w.key.split(".")[0]
            if instr in self.lab.instruments and not self.lab.instruments[instr].enabled:
                w.value = None   # switched off: nothing is sent to it, so nothing to watch (and no alarm)
                self._read_again(w, "%s is not watched while %s is disconnected" % (w.key, instr))
                continue
            if instr in self.lab.instruments and self._on_hung_bus(self.lab.instruments[instr]):
                w.value = None   # its bus is hung: the bus's alarm says it, not one of the watch's own
                continue
            self._at("reading %s" % w.key)
            try:
                value = self._read(*w.key.split(".", 1))
            except Busy as e:   # other work has its instrument, and nothing it read of it is fresh: not read now
                self._unread(w, now, str(e))
                continue
            except SensorError as e:   # it answers, but says its sensor is faulty: the reading is unknown, so alarm
                self._read_again(w)
                self._answered(instr)
                w.value = None
                if w.alarm is None:
                    self._raise(w, "%s can't be read: %s" % (w.key, str(e).splitlines()[0][:200]))
                continue
            except Exception as e:
                self._failed(instr, e)
                continue
            self._read_again(w)
            self._answered(instr)
            w.value = value
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue  # not readable just now (e.g. its switch is off) or not a number
            if math.isfinite(value):
                w.recent = (w.recent + [float(value)])[-20:]
            if w.alarm:   # it clears only once back past its threshold by more than its noise (hysteresis)
                band = w.band()
                if (w.above is None or value <= w.above - band) and (w.below is None or value >= w.below + band):
                    self._clear(w.alarm, "%s is back in range (%s)" % (w.key, self._show(w.key, value)))
                    w.alarm, w.since = None, None
                continue
            out = (w.above is not None and value > w.above) or (w.below is not None and value < w.below)
            if not out:
                w.since = None
                continue
            if w.since is None:
                w.since = now
            if now - w.since >= w.for_s:
                self._raise_reading(w, value)

    def _unread(self, w: Watch, now: float, why: str) -> None:
        """A watch not read this round: other work has its instrument, and nothing it read of it is fresh. One that
        goes unread for UNREAD_S is said, once (a watch that can't read can't alarm either); read again, it clears."""
        if w.unread_since is None:
            w.unread_since = now
        if w.unread_alarm is not None or now - w.unread_since < UNREAD_S:
            return
        a = self._new("silent", w.key, "%s has not been read for %s: %s" % (w.key, _dur(now - w.unread_since), why),
                      source="unread " + w.id)
        w.unread_alarm = a.id
        self._tell(a, "Its watch can't alarm while it isn't read. Kelvin reads it again as soon as the work that has "
                      "the instrument lets it.", watch=w.id)

    def _read_again(self, w: Watch, text: str = "") -> None:
        """The watch is read again (or no longer needs to be): its alarm for going unread clears."""
        since, alarm = w.unread_since, w.unread_alarm
        w.unread_since = w.unread_alarm = None
        if alarm is not None:
            self._clear(alarm, text or "%s is read again (it wasn't for %s)" % (
                w.key, _dur(self.lab.clock.now() - since) if since is not None else "a while"))

    # ------------------------------------------------------------------ alarms

    def _show(self, key: str, value: Any) -> str:
        try:
            return self.lab.resolve(key)[1].show(value)
        except KeyError:
            return str(value)

    def _new(self, kind: str, key: str, text: str, source: str = "", latched: bool = False) -> Alarm:
        """A new alarm, or the same one again: one that went up for the same reason (the same watch, interlock or
        drive), that nobody has said Got it to, and that is still on or cleared less than FOLD_S ago, takes the repeat
        instead (its count goes up). Its count says which: 1 for a new one. A repeat that comes back after people
        were told it cleared is news (_tell tells it); one while it flaps unannounced is only counted."""
        now = self.lab.clock.now()
        with self._lock:
            for a in self.alarms:
                if (a.kind, a.key, a.source) == (kind, key, source) and not a.acknowledged and (
                        a.cleared is None or now - self._cleared_lab.get(a.id, -1e18) < FOLD_S):
                    if a.cleared is not None and a.count == 1:   # its clear was told (_clear tells a first one)
                        self._back.add(a.id)
                    a.count += 1
                    a.last, a.text, a.cleared, a.cleared_text = time.time(), text, None, ""
                    a.latched = a.latched or latched
                    return a
            a = Alarm("a%d" % next(_ids), kind, key, text, time.time(), latched=latched, source=source)
            self.alarms.insert(0, a)
            self.trim()
        return a

    def trim(self) -> None:
        """The history kept: every alarm nobody has said Got it to, and every one still on, whatever their number (a
        night's trip once vanished behind 50 newer alarms, from the portal, the phones and lab_status); of the others,
        the newest KEEP_SEEN."""
        with self._lock:
            room = KEEP_SEEN
            kept = []
            for a in self.alarms:   # newest first
                if not a.acknowledged or a.cleared is None:
                    kept.append(a)
                elif room > 0:
                    kept.append(a)
                    room -= 1
            self.alarms[:] = kept

    def _tell(self, a: Alarm, detail: str, **record: Any) -> None:
        """An alarm that went up, into the lab's record, and to people unless it is a repeat folded into one nobody
        has said Got it to yet: they were told of it, and it shows with its count. A repeat that came back after
        people were told it cleared is told again, with its count: a phone's last word was "Cleared" while it was
        on again (a second interlock break within minutes reached nobody away from the lab)."""
        if a.count > 1:
            self.gate.log.write("alarm_again", by="monitor", alarm=a.id, count=a.count, text=a.text, **record)
            if a.id in self._back:
                self._back.discard(a.id)
                self.notify("alarm", "Alarm again (%d times): %s" % (a.count, a.text), detail)
            else:
                self.on_change()
            return
        self.gate.log.write("alarm", by="monitor", alarm=a.id, text=a.text, actions=a.actions or None, **record)
        self.notify("alarm", "Alarm: " + a.text, detail)

    def _raise_reading(self, w: Watch, value: Any) -> None:
        which = "above %g" % w.above if w.above is not None and value > w.above else "below %g" % w.below
        unit = self.lab.resolve(w.key)[1].unit
        text = "%s = %s is %s%s%s" % (w.key, self._show(w.key, value), which, " " + unit if unit else "",
                                     " for %gs" % w.for_s if w.for_s else "")
        self._raise(w, text)

    def _raise(self, w: Watch, text: str) -> None:
        """The watch's alarm and its actions: for a reading out of range, or one that can't be had (a sensor fault),
        which acts the same, since the value could be anything. Its actions run every time it goes up."""
        a = self._new("reading", w.key, text, source=w.id)
        w.alarm = a.id
        actions = []
        if w.stop_run:   # the run says what stopped it, not "stop requested", as if a person had
            self.gate.request_abort("a watch (%s)" % text)
            actions.append("stopped the running plan")
        # only ever toward safe (lab-config watches name safe values), each on a thread of its own (_to_safe)
        started = [self._to_safe(a, "%s.%s" % (instr, param), lambda i=instr, p=param: self._cut(i, p))
                   for instr, param, safe in w.then]
        self._actions(a, actions, started, keep=a.count > 1)
        self._tell(a, "; ".join(a.actions) or (w.note or "Kelvin is watching it."), watch=w.id)

    def _cut(self, instr: str, param: str) -> List[Tuple[bool, str]]:
        """A watch's action: the drive to safe, and its switch off too (gate.cut): V = 0 does nothing on a source in
        current mode."""
        try:
            return [(r.ok, r.message if r.ok else "could not set %s.%s: %s" % (instr, param, r.message))
                    for r in self.gate.cut(instr, param, "monitor")]
        except Exception as e:
            return [(False, "could not set %s.%s: %s" % (instr, param, e))]

    def _abort_to_safe(self, drive: str) -> List[Tuple[bool, str]]:
        """An interlock's action: its drive to safe, even where the lab config says hold."""
        try:
            return [(r.ok, r.message) for r in self.gate.abort_to_safe([drive], "monitor", hold=False)]
        except Exception as e:
            return [(False, "could not bring %s to safe: %s" % (drive, e))]

    def _to_safe(self, alarm: Alarm, key: str, job: Callable[[], List[Tuple[bool, str]]]) \
            -> Tuple[str, Optional[_Safing]]:
        """Send a drive to safe on a thread of its own (job: the gate's to-safe, what it said), so a ramp of minutes
        never holds the monitor up: an 8 V gate at 0.5 V/s took every other check for 17 s, and an output that waits for
        a magnet to ramp down took them for tens of minutes. One instrument's go one after another (two ramps of one
        drive would interleave their steps); one already on its way isn't sent again (None). (key, its handle)."""
        instr = key.split(".")[0]
        with self._lock:
            going = self._safing.get(key)
            if going is not None and not going.done.is_set():
                return key, None
            h = _Safing(key, alarm)
            before = self._safe_last.get(instr)
            self._safing[key] = self._safe_last[instr] = h
        threading.Thread(target=self._safe_run, args=(h, job, before), name="kelvin-to-safe", daemon=True).start()
        return key, h

    def _safe_run(self, h: _Safing, job: Callable[[], List[Tuple[bool, str]]], before: Optional[_Safing]) -> None:
        if before is not None:
            before.done.wait()   # its instrument's to-safe before this one first
        try:
            said = list(job())
        except Exception as e:   # (job says what failed itself; this is for what it didn't foresee)
            said = [(False, "could not bring %s to safe: %s" % (h.key, e))]
        with self._lock:
            h.said, h.finished = said, True
            late = h.late
        try:
            if late:
                self._safe_ended(h)
        finally:
            h.done.set()

    def _actions(self, a: Alarm, first: List[str], started: List[Tuple[str, Optional[_Safing]]],
                 keep: bool = False, add: bool = False) -> None:
        """The alarm's actions: `first`, then what each to-safe just started said, all of them waited for SAFE_WAIT_S
        at most; one still going says so, and says how it ended in the alarm itself (_safe_ended). keep: a repeat keeps
        the actions it had when there are none now. add: they go after its earlier ones, each once."""
        end = time.monotonic() + SAFE_WAIT_S
        for key, h in started:
            if h is not None:
                self._at("sending %s to safe" % key)
                h.done.wait(max(0.0, end - time.monotonic()))
        with self._lock:
            words = list(first)
            for key, h in started:
                if h is None:
                    words.append("%s already on its way to safe" % key)
                elif h.finished:
                    words += [w for _, w in h.said]
                else:
                    h.late = True
                    words.append(h.going)
            if add:
                a.actions += [w for w in words if w not in a.actions]
            elif words or not keep:
                a.actions = words

    def _safe_ended(self, h: _Safing) -> None:
        """A to-safe that outlasted SAFE_WAIT_S is done: its alarm says how in place of "going to safe", and so does
        the record; people hear of it when it didn't get there (they were told it was on its way)."""
        a, words = h.alarm, [w for _, w in h.said]
        with self._lock:
            if h.going in a.actions:
                k = a.actions.index(h.going)
                a.actions[k:k + 1] = words
            else:
                a.actions += [w for w in words if w not in a.actions]
        ok = all(flag for flag, _ in h.said)
        self.gate.log.write("alarm_actions", by="monitor", alarm=a.id, target=h.key, actions=words, ok=ok)
        if ok:
            self.on_change()
        else:
            self.notify("alarm", "Alarm: %s did not go to safe" % h.key, "; ".join(words) + " (after: %s)" % a.text)

    def safe_done(self, timeout: Optional[float] = None) -> bool:
        """Wait until every to-safe the monitor started is done (timeout seconds at most); whether they are."""
        end = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            going = list(self._safing.values())
        for h in going:
            if not h.done.wait(None if end is None else max(0.0, end - time.monotonic())):
                return False
        return True

    def _check_interlocks(self, now: float) -> None:
        """The interlocks are enforced, not only checked when their drive moves: every INTERLOCK_EVERY lab seconds,
        each one whose drive is away from safe has its condition read. When it fails, or can't be read (an open
        sensor, its instrument gone), an alarm goes up, the running plan stops and the drive goes to safe, even where
        the lab config says hold: hold is for a stop, and a held field once stayed at 1 T above its interlock. A drive
        counts as at safe once its reading is (a field ramping down is still up: its alarm once cleared on the set
        point while the field was 1.5 T). The alarm clears once the drive is at safe, or the condition has held again
        for INTERLOCK_CLEAR_S (a sample at its interlock's 10 K raised and cleared it six times in 15 s). While it is
        up, a drive set away from safe again goes back to safe.

        An instrument other work has (an acquisition, a wait for *OPC?) isn't waited for: the work's own reading stands
        in while fresh. A condition with none is not checked this round, and after INTERLOCK_UNREAD_S unchecked it
        fails as one that can't be read. A drive whose place is unknown so is taken as away from safe: its condition
        is checked."""
        if now - self._last_interlock_check < INTERLOCK_EVERY:
            return
        self._last_interlock_check = now
        from .gate import _unreachable, interlock_problem, reading_away
        for i, lock in enumerate(self.lab.config.interlocks):
            self._at("checking the interlock on %s" % lock.drive)
            try:
                inst, spec = self.lab.resolve(lock.drive)
                if not inst.enabled:
                    self._unchecked.pop(i, None)
                    continue   # switched off: Kelvin sends it nothing, and nothing reaches the sample from it
                at = self._fresh(inst, spec.name, lambda: self.gate._current(inst, spec))
                unknown = False
            except Busy:   # where it is set is unknown now: it may be away from safe
                at, unknown = None, True
            except Exception:
                continue   # its drive can't be read: silence is the read failures' job
            try:   # where it is, not only where it is set to
                there = self._read(inst.name, spec.readback) if spec.readback and spec.ramp == "instrument" else at
            except Busy:
                there, unknown = None, True   # not known to be at safe: a field ramping down is up
            except Exception:
                there = at   # its reading can't be read: by its set point, as before
            set_safe = at is not None and spec.distance(at) == 0
            # set to safe, and there: a field ramping down is up
            safe = set_safe and not reading_away(spec, there) and not unknown
            try:
                broken = None if safe else interlock_problem(self.lab, lock, read=self._read)
            except Busy as e:   # its reading's instrument is busy, and nothing fresh of it: not checked this round
                since = self._unchecked.setdefault(i, now)
                if now - since < INTERLOCK_UNREAD_S:
                    continue
                broken = ", but %s could not be read for %s (%s)" % (lock.require.key, _dur(now - since), e)
            else:
                self._unchecked.pop(i, None)
            if broken is None:
                if i in self._broken and (safe or now - self._holding.setdefault(i, now) >= INTERLOCK_CLEAR_S):
                    self._holding.pop(i, None)
                    again = "the drive is at safe" if safe else "%s holds again" % lock.require.text
                    self._clear(self._broken.pop(i).id, "interlock %s: %s" % (lock.drive, again))
                continue
            self._holding.pop(i, None)
            if i in self._broken:
                # set away again while it is broken: back to safe (not one merely unknown now, already sent there)
                if not set_safe and not (unknown and at is None) and not _unreachable(spec):
                    started = self._to_safe(self._broken[i], lock.drive, lambda d=lock.drive: self._abort_to_safe(d))
                    if started[1] is not None:
                        self._actions(self._broken[i], [], [started], add=True)
                continue
            moving = spec.readback and _number(at) and _number(there) and abs(at - there) > spec.tol
            shown = ("a value not known now (%s is busy)" % inst.name if unknown and at is None and not _number(there)
                     else spec.show(there if _number(there) else at) + (" (set to %s)" % spec.show(at) if moving else ""))
            text = "Interlock: %s is at %s, which needs %s%s" % (lock.drive, shown, lock.require.text, broken)
            a = self._broken[i] = self._new("interlock", lock.drive, text, source="interlock %d" % i)
            self.gate.request_abort("the interlock on %s (it needs %s%s)" % (lock.drive, lock.require.text, broken))
            started = self._to_safe(a, lock.drive, lambda d=lock.drive: self._abort_to_safe(d))
            self._actions(a, ["stopped the running plan"], [started])
            self._tell(a, "; ".join(a.actions), interlock=lock.drive)

    def _check_setpoints(self, now: float) -> None:
        """The set-point watchdog. A set point an instrument ramps to by itself (a cryostat's temperature, a magnet's
        field) that Kelvin set, whose reading has not got closer to it for stall_s (lab time), raises an alarm: a wait
        that never ended once held a run for 3 h with a current source on, and nobody heard. The alarm clears when
        the reading moves on or gets there, or the set point changes. Only set points Kelvin set since it connected
        are followed, until their reading gets there: a lab at rest never alarms, nor a set point no instrument
        reaches (0 K, which a stop holds short of). Never raises: the interlock and error checks after it run anyway."""
        if now - self._last_setpoint_check < SETPOINT_EVERY:
            return
        self._last_setpoint_check = now
        seen: set = set()
        for name, inst in list(self.lab.instruments.items()):
            if not inst.enabled:
                continue
            for spec in inst.card.params.values():
                if spec.is_drive and spec.ramp == "instrument" and spec.readback and spec.numeric:
                    try:
                        self._follow(name, inst, spec, now, seen)
                    except Busy:   # other work has it: followed on, looked at again next time
                        seen.add("%s.%s" % (name, spec.name))
                    except Exception:   # one odd instrument must not stop the monitor's other checks
                        continue
        for key in [k for k in self._moves if k not in seen]:   # switched off, or no longer set by Kelvin
            del self._moves[key]
            if key in self._stalled:
                self._clear(self._stalled.pop(key).id, "%s is no longer followed" % key)

    def _follow(self, name: str, inst: Any, spec: Any, now: float, seen: set) -> None:
        """One set point: where its reading is now against where it was going, alarming or clearing as it goes."""
        from .gate import WAIT_STALL_S, _unreachable
        target = inst._last.get(spec.name)
        if not _number(target) or (_unreachable(spec) and target <= 0) or not self._applied(inst, spec):
            return
        key = "%s.%s" % (name, spec.name)
        seen.add(key)
        move = self._moves.get(key)
        if move is None or move.target != float(target):
            stalled = self._stalled.pop(key, None)
            if stalled is not None:
                self._clear(stalled.id, "%s has a new set point (%s)" % (key, spec.show(target)))
            move = self._moves[key] = Approach(target, spec.tol, now, rate_per_s(inst, spec), self.stall_s)
        if move.arrived:
            return   # it got there; a reading that drifts off later is for a watch
        try:
            value = self._fresh(inst, spec.readback, lambda: inst.read(spec.readback))
        except Exception:
            return   # silence is the read failures' job (and a busy instrument is looked at next time)
        stuck = move.update(value, now)
        reading = "%s.%s" % (name, spec.readback)
        if move.there(value):
            move.arrived = True
            if key in self._stalled:
                self._clear(self._stalled.pop(key).id, "%s reached its set point (%s)" % (reading, spec.show(target)))
        elif key in self._stalled and stuck < self.stall_s:
            self._clear(self._stalled.pop(key).id, "%s is getting closer to %s again (%s)" % (
                reading, spec.show(target), self._show(reading, value)))
        elif key not in self._stalled and stuck >= self.stall_s:
            switch = switch_words(inst, spec)   # a range too low for the set point, not a stuck instrument
            text = "%s has not got closer to %s for %s: it reads %s (set point %s%s)" % (
                reading, spec.show(target), _minutes(stuck),
                self._show(reading, value) if _number(value) else repr(value), key, "; " + switch if switch else "")
            a = self._stalled[key] = self._new("stall", key, text, source="setpoint")
            self._tell(a, "%sA wait for it ends after %s without progress, and so does a run that ramps it." % (
                "" if switch else "The instrument may be stuck: check its rate, and its helium, heater or compressor. ",
                _minutes(WAIT_STALL_S)), setpoint=key)

    def from_gate(self, kind: str, key: str, text: str, actions: Optional[List[str]] = None,
                  target: Any = None) -> Optional[Alarm]:
        """What the gate met while it changed something, as an alarm that stays on until a person says Got it: a
        guard's trip (a gate's leakage past its limit, the night's most important event, went into the record and
        nowhere else), and a wait for a set point that gave up ("stall": its reading stopped getting closer, or its
        time was up; the alarm the system prompt promised never went up). The watchdog's alarm for that set point and
        target, if it is on already and nobody has said Got it to it, becomes this one. One a person has seen ends
        here instead, and the wait's end is an alarm of its own, told: it is news, and a Got it said before the alarm
        latched once left nothing that could end it. Never raises: the gate goes on whatever happens here."""
        try:
            with self._lock:
                told = self._stalled.get(key) if kind == "stall" else None
                move = self._moves.get(key)
                same = told is not None and told.cleared is None and move is not None and _number(target) \
                    and move.target == float(target)
                if same and not told.acknowledged:   # it now says how the wait ended, and stays on
                    del self._stalled[key]
                    told.text, told.latched, told.last = text, True, time.time()
                    told.actions = list(actions or told.actions)
                    a = told
                else:
                    if same:   # seen: it ends with the wait, quietly (the person knows it), and the new one says why
                        del self._stalled[key]
                        told.cleared, told.cleared_text = time.time(), "the wait for it gave up (the next alarm)"
                        self._cleared_lab[told.id] = self.lab.clock.now()
                        self.gate.log.write("alarm_cleared", by="monitor", alarm=told.id, text=told.cleared_text)
                    told = None
                    a = self._new(kind, key, text, source="gate", latched=True)
                    a.actions = list(actions or [])
            if told is not None:   # people heard of it when it went up; the record has the rest
                self.gate.log.write("alarm_again", by="monitor", alarm=a.id, count=a.count, text=a.text, target=key)
                self.on_change()
            else:
                self._tell(a, "; ".join(a.actions) or "It stays on until a person says Got it.", target=key)
            return a
        except Exception:
            return None

    @staticmethod
    def _applied(inst: Any, spec: Any) -> bool:
        """Whether a set point reaches the sample: no switch it applies through, or that switch away from safe (a
        controller's setpoint with its heater range off goes nowhere, and isn't a stall). Followed afresh once on."""
        switch = inst.card.params.get(spec.applies_when) if spec.applies_when else None
        if switch is None:
            return True
        try:
            return switch.distance(inst.read(switch.name)) > 0
        except Busy:
            raise   # not known now: the set point is followed on (_check_setpoints)
        except Exception:
            return False

    def _clear(self, alarm_id: str, text: str) -> None:
        """An alarm whose condition ended. People hear of it the first time; a repeat folded into it clears quietly
        (the record has it). A latched alarm ends only at a person's Got it."""
        for a in list(self.alarms):
            if a.id == alarm_id and a.cleared is None and not a.latched:
                a.cleared, a.cleared_text = time.time(), text
                self._cleared_lab[a.id] = self.lab.clock.now()
                self.gate.log.write("alarm_cleared", by="monitor", alarm=a.id, text=text)
                if a.count > 1:
                    self.on_change()
                else:
                    self.notify("alarm_cleared", "Cleared: " + text, "")

    def _failed(self, instr: str, e: Exception) -> None:
        """A watch's reading failed. One its instrument didn't answer is that instrument's health, which
        _check_health says once for everyone who met it; any other failure (a reply its card can't read) alarms after
        a few tries as a reading that can't be had."""
        if isinstance(e, (InstrumentLost, Garbled)):
            return   # the instrument's: said once, for everyone who met it
        self._fails[instr] = self._fails.get(instr, 0) + 1
        if self._fails[instr] >= FAILS_BEFORE_ALARM and instr not in self._unreadable:
            text = "%s can't be read (%s)" % (instr, str(e).splitlines()[0][:160] if str(e) else type(e).__name__)
            a = self._new("silent", instr, text, source="unreadable")
            self._unreadable[instr] = a
            self._tell(a, "It answers, but not with a reading its card can read: check the card (Lab → Cards…) and "
                          "the instrument's settings.")

    def _answered(self, instr: str) -> None:
        self._fails[instr] = 0
        a = self._unreadable.pop(instr, None)
        if a is not None:
            self._clear(a.id, "%s can be read again" % instr)

    # ------------------------------------------------------------------ instruments that stop answering

    def mark(self) -> Tuple[float, Dict[str, int]]:
        """Now, and how many times each instrument has failed to answer so far: give it to lost_since after some
        work."""
        return time.time(), {name: inst.health.losses for name, inst in self.lab.instruments.items()}

    def lost_since(self, mark: Tuple[float, Dict[str, int]]) -> List[Alarm]:
        """The instruments that failed to answer since `mark`, when that stopped work nobody was watching (a queue
        item): their alarm goes up now, without waiting for more failed reads, so the pause has its reason in the
        alarms. One that answers again already (a power blip) is cleared at once; the record keeps that it was out.
        One whose alarm went up since `mark` already isn't raised again: one alarm per fault."""
        t0, losses = mark
        out = []
        for name, inst in list(self.lab.instruments.items()):
            h = inst.health
            if h.losses <= losses.get(name, 0):
                continue
            with self._lock:
                a = self._silent.get(name) or next((x for x in self.alarms if x.key == name and x.latest >= t0
                                                    and x.kind in ("silent", "reset")), None) \
                    or self._raise_lost(name, h.lost_text)
            out.append(a)
        self._check_health()   # one that answers again is cleared; garbled replies latch their fault
        return out

    @staticmethod
    def _on_hung_bus(inst: Any) -> bool:
        from .adapters import BUSES, bus_of
        return not inst.simulated and bus_of(inst.config.address) in BUSES.hung

    def _raise_lost(self, name: str, text: str) -> Alarm:
        a = self._new("silent", name, text)
        self._silent[name] = a
        self._tell(a, "Runs that use it stop, and plans refuse to start, until it answers again.", instrument=name)
        return a

    def _check_health(self, now: Optional[float] = None) -> None:
        """The instruments' health, from every exchange with them whoever made it (a watch, a run, a set, this
        monitor): one alarm per instrument that doesn't answer, after FAILS_BEFORE_ALARM in a row, or at once when the
        bus says it can't be reached at all (nothing listens on GPIB, a pulled USB cable); cleared when it answers.
        Garbled replies: one comms-fault alarm at the first, kept until none has come for GARBLE_CLEAR_S."""
        now = self.lab.clock.now() if now is None else now
        for name, inst in list(self.lab.instruments.items()):
            h = inst.health
            self._check_garbles(name, inst, now)
            with self._lock:
                on = self._silent.get(name)
                if on is None and inst.enabled and h.fails and (h.gone or h.fails >= FAILS_BEFORE_ALARM) \
                        and not self._on_hung_bus(inst):   # a hung bus has one alarm, for all of them
                    self._raise_lost(name, h.lost_text)
                elif on is not None and h.fails == 0:
                    del self._silent[name]
                    self._clear(on.id, "%s answers again (it didn't for %s)%s" % (
                        name, _since(on.raised), "; a power cut puts an instrument back to its power-on settings "
                        "(outputs off), so check them before going on" if any(s.enables for s in inst.card.drives)
                        else ""))

    def _check_buses(self) -> None:
        """GPIB buses that stopped answering (Lab.hung_buses): one alarm for the bus, not one per instrument, and
        Kelvin stops sending on it (adapters.BUSES) until it is cleared (clear_bus) or answers again: every
        BUS_PROBE_S one instrument on it is asked who it is."""
        from .adapters import BUSES, bus_words
        hung = dict(self.lab.hung_buses())
        for bus, names in hung.items():
            if bus in BUSES.hung:
                continue
            first = self.lab.first_silent(names)
            inst = self.lab.instruments.get(first) if first else None
            when = (" (after %s, at %s)" % (inst.health.first_failed or "a command", time.strftime(
                "%H:%M:%S", time.localtime(inst.health.first_failed_at)))
                if inst is not None and inst.health.first_failed_at else "")
            text = ("%s isn't answering: %s stopped answering, and nothing on it has answered since. Kelvin stopped "
                    "sending on it.%s Clear the bus (an interface clear, then each instrument cleared and asked who it "
                    "is: settings and output levels stay; what they are in the middle of ends)" % (
                        bus_words(bus), _and(names), " The first to stop was %s%s, most likely what hung it." % (
                            first, when) if first else ""))
            BUSES.flag(bus, names, text)
            self._bus_probe_at[bus] = time.time()
            with self._lock:
                a = self._buses[bus] = self._new("comms", bus, text, source="bus")
            self._tell(a, "Runs that use instruments on it stop; Kelvin asks one of them again every %.0f s." %
                       BUS_PROBE_S, bus=bus)
        for bus in list(BUSES.hung):
            if time.time() - self._bus_probe_at.get(bus, 0.0) < BUS_PROBE_S:
                continue
            self._bus_probe_at[bus] = time.time()
            if self._probe_bus(bus):
                self.bus_cleared(bus, "%s answers again" % bus_words(bus))

    def _probe_bus(self, bus: str) -> bool:
        """One instrument on a hung bus (not the one that stopped first), asked who it is: whether the bus answers."""
        from .adapters import BUSES, bus_of
        on = [i for i in list(self.lab.instruments.values()) if i.enabled and not i.simulated
              and bus_of(i.config.address) == bus and i.card.idn_pattern]
        first = self.lab.first_silent([i.name for i in on])
        # not the one that stopped first (most likely what hung it), nor one nothing listens at: among the others, the
        # one that answered last
        others = [i for i in on if i.name != first and not i.health.gone] or on
        if not others:
            return False
        pick = max(others, key=lambda i: i.health.answered_at or 0.0)
        try:
            with BUSES.through(bus):
                pick.query_raw(pick.card.idn_query)
            return True
        except Exception:
            return False

    def bus_cleared(self, bus: str, text: str) -> None:
        """A hung bus answers again (a probe, or clear_bus): Kelvin sends on it again, and its alarm clears."""
        from .adapters import BUSES
        BUSES.clear(bus)
        with self._lock:
            a = self._buses.pop(bus, None)
        if a is not None:
            self._clear(a.id, text)

    def _check_garbles(self, name: str, inst: Any, now: float) -> None:
        h = inst.health
        with self._lock:
            if h.garbles > self._garbles_seen.get(name, 0):
                self._garbles_seen[name] = h.garbles
                self._garbled_lab[name] = now
                if name not in self._comms:
                    a = self._comms[name] = self._new("comms", name, h.garbled_text)
                    self._tell(a, "Raised once: it stays until %s's replies have been clean for %s. Runs that read it "
                                  "stop at a reply Kelvin can't read." % (name, _dur(GARBLE_CLEAR_S)), instrument=name)
            elif name in self._comms and now - self._garbled_lab.get(name, now) >= GARBLE_CLEAR_S \
                    and (h.answered_at or 0) > (h.garbled_at or 0):
                a = self._comms.pop(name)
                self._clear(a.id, "%s's replies are clean again (none garbled for %s; %d garbled in all)"
                            % (name, _dur(GARBLE_CLEAR_S), h.garbles))

    def _check_alive(self, now: float) -> None:
        """While a plan or a queue item runs, each real instrument is asked to answer every ALIVE_EVERY (lab time): an
        instrument nothing read (a voltmeter during a cool-down) was unplugged for minutes with no alarm. The question
        disturbs nothing: its identity query on the bus, or for a maker's software (whose identity it answers by
        itself) its headline reading. Its health takes the answer (_check_health). Also, an output this run switched
        on that reads off now: the instrument restarted (a power cut puts it back to its power-on settings) or someone
        switched it off, and the run would go on measuring with it off."""
        asked = getattr(self.gate, "_asked", None)   # the drive levels the running plan or queue item set
        if asked is None:   # no plan runs: the next one is watched afresh (an alarm for a restart stays till Got it)
            self._suspect.clear()
            self._reset.clear()
            return
        asked = dict(asked)
        for name, inst in list(self.lab.instruments.items()):
            if inst.simulated or not inst.enabled or self._on_hung_bus(inst):
                continue
            answered = inst.health.answered_at
            if answered is not None and answered > self._alive_asked.get(name, 0.0):
                self._last_alive_check[name] = now   # it answered something since (the run talks to it): nothing sent
                self._alive_asked[name] = time.time()
            if now - self._last_alive_check.get(name, -1e18) >= ALIVE_EVERY:
                self._last_alive_check[name] = now
                probe = self._probe(inst)
                self._at("asking %s whether it answers" % name)
                try:
                    if probe is not None:
                        probe()
                except Exception:
                    # its health says it, if it didn't answer; a busy one (Busy) answers the work that has it, which
                    # shows soon enough whether it is there
                    continue
                finally:   # after the question: its own answer is not "answered since"
                    self._alive_asked[name] = time.time()
            if now - self._last_reset_check.get(name, -1e18) >= RESET_EVERY:
                self._last_reset_check[name] = now
                self._check_reset(name, inst, asked, now)

    def _probe(self, inst: Any) -> Optional[Callable[[], Any]]:
        if inst.card.idn_pattern and not hasattr(inst.backend, "identity"):
            def ask() -> None:   # its identity, which it gave at the start: anything else arrived garbled
                reply = inst.query_raw(inst.card.idn_query)
                if not re.search(inst.card.idn_pattern, reply.strip(), re.I):
                    inst.health.garbled(inst.garble_text(inst.card.idn_query, reply))
            return ask
        def plain(s: Any) -> bool:   # a reading that only reads: no acquisition, no trace or frame, no switch to wait on
            return s.get_cmd is not None and s.type not in ("trace", "image") and not s.acquires \
                and not s.get_before and s.read_when is None
        head = inst.card.params.get(inst.card.headline) if inst.card.headline else None
        param = head.name if head is not None and plain(head) else next(
            (s.name for s in inst.card.params.values() if s.kind == "measure" and plain(s)), None)
        return (lambda: inst.read(param)) if param else None

    def _check_reset(self, name: str, inst: Any, asked: Dict[str, Any], t: float) -> None:
        """An output switch the running plan turned on that reads off twice, RESET_EVERY apart (a laser takes a moment
        to follow): one alarm, and the plan stops."""
        mine = [(k.split(".", 1)[1], v) for k, v in asked.items() if k.split(".", 1)[0] == name]
        switches = [(p, v) for p, v in mine if inst.card.params.get(p) is not None and inst.card.params[p].enables
                    and inst.card.params[p].distance(v) > 0]
        for param, value in switches:
            spec = inst.card.params[param]
            try:
                now = inst.read(param)
            except Exception:
                return   # its health says it, if it didn't answer
            if now is None or spec.distance(now) > 0:
                self._suspect.pop("%s.%s" % (name, param), None)
                continue
            first = self._suspect.setdefault("%s.%s" % (name, param), t)
            if t - first < RESET_EVERY:
                continue   # off once: it may still be following
            with self._lock:
                if name in self._reset:
                    return
                recent = self._silent.get(name) or next((a for a in self.alarms if a.kind == "silent" and a.key == name
                                                         and time.time() - a.latest < 600), None)
                text = "%s.%s is %s, though the running plan switched it %s: %s restarted (a power cut puts it back to " \
                       "its power-on settings) or someone switched it off" % (name, param, spec.show(now),
                                                                           spec.show(value), name)
                if recent is not None:   # the same fault as its alarm for not answering: said there, not again
                    recent.text += "; it came back with %s.%s %s (it restarted)" % (name, param, spec.show(now))
                    a = recent
                else:
                    a = self._new("reset", name, text)
                self._reset[name] = a
            self.gate.request_abort("%s.%s, found %s under the running plan (%s restarted, or someone switched it "
                                    "off)" % (name, param, spec.show(now), name))
            a.actions.append("stopped the running plan, which would have gone on measuring with %s.%s %s"
                             % (name, param, spec.show(now)))
            if recent is None:
                self._tell(a, "; ".join(a.actions), instrument=name)
            else:   # the same fault as its alarm for not answering: said there, not again
                self.gate.log.write("alarm", by="monitor", alarm=a.id, instrument=name, text=a.text, actions=a.actions)
                self.on_change()
            return

    def _check_errors(self, now: float) -> None:
        """Real instruments' own error queues, once a minute (lab time). That it answered or didn't goes into its
        health."""
        if getattr(self.gate, "_asked", None) is not None:
            return   # while a plan or a queue item runs, the bus is its own: the error queues wait for it to end
        for name, inst in list(self.lab.instruments.items()):
            if inst.simulated or inst.card.error_query is None or not inst.enabled or self._on_hung_bus(inst):
                continue
            if now - self._last_error_check.get(name, -1e18) < ERROR_CHECK_EVERY:
                continue
            self._last_error_check[name] = now
            try:   # the whole queue, so it never overflows (-350) while one error a minute is read
                errs, _ = inst.drain_errors()
            except Exception:
                continue  # one it didn't answer is its health's (_check_health)
            err = errs[0] if errs else None
            if name in self._comms:
                continue   # while its replies arrive garbled, an "error" in one may be noise: the comms fault says it
            if err and name not in self._errors:
                text = "%s reports an error: %s%s" % (name, err, " (and %d more)" % (len(set(errs)) - 1)
                                                      if len(set(errs)) > 1 else "")
                a = self._errors[name] = self._new("error", name, text)
                self._tell(a, "", instrument=name)
            elif not err and name in self._errors:
                self._clear(self._errors.pop(name).id, "%s reports no more errors" % name)

    # ------------------------------------------------------------------ the lab's record

    def _check_record(self) -> None:
        """The lab's record (events.jsonl), while it can't be written (its folder's drive or network share went away,
        a full disk): one alarm says so. Kelvin keeps what happens meanwhile, and writes it there as soon as it can
        (EventLog: tried again every so often, also from here); then the alarm clears."""
        log = getattr(self.gate, "log", None)
        flush = getattr(log, "flush", None)
        if getattr(log, "trouble", None) and flush is not None:
            flush()   # it may be back (no more often than the log's own retries)
        trouble = getattr(log, "trouble", None)
        with self._lock:
            on = self._record
            if trouble and on is None:
                a = self._record = self._new("record", "the lab's record", "The lab's record can't be written: %s"
                                             % trouble, source="record")
            elif not trouble and on is not None:
                self._record = None
        if trouble and on is None:
            self._tell(a, "Kelvin keeps what happens meanwhile and writes it there as soon as it can. Runs can't keep "
                          "their data there either: check that the data folder's drive or network share is there, "
                          "with room.")
        elif not trouble and on is not None:
            self._clear(on.id, "the lab's record is written again")
