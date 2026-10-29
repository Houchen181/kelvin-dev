"""Watching sensitive instruments: readings that leave their range, instruments that stop answering or report
errors. A watch only reads. When it trips it raises an alarm once (and clears it when the reading is back), tells
people (see notify.py), and, for watches written in the lab config, can stop the running plan and send named drives to
their safe values - the only kind of change it may make.

An instrument that stops answering raises one alarm in plain words, whoever met it (a watch, a run, the monitor's own
check that each real instrument still answers): which instrument, on which bus, what to check ("nvm (Keithley 2182A)
doesn't answer on GPIB (address 7): cable or power?"). It clears when the instrument answers again. Replies that
arrive garbled are one comms fault, latched: raised once, cleared only after its replies have been clean for a while.

Durations are in lab time (the lab clock), so the virtual lab at 10x real time and tests on a simulated clock behave
like the real lab.
"""
from __future__ import annotations

import itertools
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .instrument import Garbled, InstrumentLost, SensorError

FAILS_BEFORE_ALARM = 3       # failed exchanges in a row before "doesn't answer" (one that can't reach it at all: one)
ERROR_CHECK_EVERY = 60.0     # lab seconds between error-queue checks on real instruments
ALIVE_EVERY = 20.0           # lab seconds between checks that each real instrument answers, while a run or queue goes
RESET_EVERY = 5.0            # lab seconds between checks that the outputs the running plan switched on are still on
GARBLE_CLEAR_S = 300.0       # lab seconds without a garbled reply before a comms fault clears
INTERLOCK_EVERY = 5.0        # lab seconds between checks of the interlocks whose drive is away from safe
STALL_S = 300.0              # lab seconds a set point's reading may go without getting closer before the alarm
SETPOINT_EVERY = 15.0        # lab seconds between the set-point watchdog's looks
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
    # state
    value: Any = None
    since: Optional[float] = None              # lab time the condition became true
    last_read: Optional[float] = None
    alarm: Optional[str] = None                # the active alarm's id

    def rule(self, unit: str = "") -> str:
        u = " " + unit if unit else ""
        parts = []
        if self.above is not None:
            parts.append("above %g%s" % (self.above, u))
        if self.below is not None:
            parts.append("below %g%s" % (self.below, u))
        return "%s %s%s" % (self.key, " or ".join(parts), " for %gs" % self.for_s if self.for_s else "")

    def view(self, unit: str = "") -> Dict[str, Any]:
        return {"id": self.id, "key": self.key, "above": self.above, "below": self.below, "for_s": self.for_s,
                "every": self.every, "source": self.source, "note": self.note, "rule": self.rule(unit),
                "then": ["%s.%s %s" % t for t in self.then], "value": self.value, "alarm": bool(self.alarm)}


@dataclass
class Alarm:
    id: str
    kind: str          # reading | silent | error
    key: str
    text: str
    raised: float      # wall time
    actions: List[str] = field(default_factory=list)
    cleared: Optional[float] = None
    acknowledged: bool = False

    def view(self) -> Dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "key": self.key, "text": self.text, "actions": self.actions,
                "raised": self.raised, "cleared": self.cleared, "acknowledged": self.acknowledged}


class Monitor:
    """Polls watches on a background thread (start/stop), or one pass at a time (tick) in tests."""

    def __init__(self, lab: Any, gate: Any, notify: Callable[[str, str, str], None], poll_s: float = 1.0):
        self.lab = lab
        self.gate = gate
        self.notify = notify
        self.poll_s = poll_s
        self.watches: Dict[str, Watch] = {}
        self.last_tick: Optional[float] = None   # wall time of the latest pass: the heartbeat's sign of life
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
        self._garbles_seen: Dict[str, int] = {}   # garbled replies already counted, by instrument
        self._garbled_lab: Dict[str, float] = {}  # lab time the latest garbled reply was seen
        self._last_interlock_check = -1e18
        self._broken: Dict[int, Alarm] = {}   # interlocks found broken (by their place in the lab config)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # the set-point watchdog: set points on their way (by drive, instr.param), and how long without getting closer
        self.stall_s = STALL_S
        self._moves: Dict[str, Approach] = {}
        self._stalled: Dict[str, Alarm] = {}
        self._last_setpoint_check = -1e18

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

    def remove(self, watch_id: str, source: Optional[str] = None) -> Watch:
        with self._lock:
            w = self.watches.get(watch_id)
            if w is None:
                raise KeyError("no watch %r" % watch_id)
            if source is not None and w.source == "lab":
                raise PermissionError("%s comes from the lab config; change it there" % watch_id)
            return self.watches.pop(watch_id)

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

    def acknowledge(self, alarm_id: str) -> bool:
        for a in self.alarms:
            if a.id == alarm_id:
                a.acknowledged = True
                return True
        return False

    # ------------------------------------------------------------------ the loop

    def start(self) -> "Monitor":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="kelvin-monitor", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                self.tick()
            except Exception:  # the monitor must outlive any one bad read
                pass

    def watching(self) -> bool:
        """Is the monitor going: it ticked lately (the heartbeat pings only then)."""
        return self.last_tick is not None and time.time() - self.last_tick < max(60.0, 20 * self.poll_s)

    def tick(self) -> None:
        self.last_tick = time.time()
        now = self.lab.clock.now()
        with self._lock:
            watches = list(self.watches.values())
        for w in watches:
            if w.last_read is not None and now - w.last_read < w.every:
                continue
            w.last_read = now
            instr = w.key.split(".")[0]
            if instr in self.lab.instruments and not self.lab.instruments[instr].enabled:
                w.value = None   # switched off: nothing is sent to it, so nothing to watch (and no alarm)
                continue
            try:
                value = self.lab.read(*w.key.split(".", 1))
            except SensorError as e:   # it answers, but says its sensor is faulty: the reading is unknown, so alarm
                self._answered(instr)
                w.value = None
                if w.alarm is None:
                    self._raise(w, "%s can't be read: %s" % (w.key, str(e).splitlines()[0][:200]))
                continue
            except Exception as e:
                self._failed(instr, e)
                continue
            self._answered(instr)
            w.value = value
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue  # not readable just now (e.g. its switch is off) or not a number
            out = (w.above is not None and value > w.above) or (w.below is not None and value < w.below)
            if not out:
                w.since = None
                if w.alarm:
                    self._clear(w.alarm, "%s is back in range (%s)" % (w.key, self._show(w.key, value)))
                    w.alarm = None
                continue
            if w.since is None:
                w.since = now
            if w.alarm is None and now - w.since >= w.for_s:
                self._raise_reading(w, value)
        self._check_setpoints(now)
        self._check_interlocks(now)
        self._check_errors(now)
        self._check_alive(now)
        self._check_health(now)

    # ------------------------------------------------------------------ alarms

    def _show(self, key: str, value: Any) -> str:
        try:
            return self.lab.resolve(key)[1].show(value)
        except KeyError:
            return str(value)

    def _new(self, kind: str, key: str, text: str) -> Alarm:
        a = Alarm("a%d" % next(_ids), kind, key, text, time.time())
        with self._lock:
            self.alarms.insert(0, a)
            del self.alarms[50:]
        return a

    def _raise_reading(self, w: Watch, value: Any) -> None:
        which = "above %g" % w.above if w.above is not None and value > w.above else "below %g" % w.below
        unit = self.lab.resolve(w.key)[1].unit
        text = "%s = %s is %s%s%s" % (w.key, self._show(w.key, value), which, " " + unit if unit else "",
                                     " for %gs" % w.for_s if w.for_s else "")
        self._raise(w, text)

    def _raise(self, w: Watch, text: str) -> None:
        """The watch's alarm and its actions: for a reading out of range, or one that can't be had (a sensor fault),
        which acts the same, since the value could be anything."""
        a = self._new("reading", w.key, text)
        w.alarm = a.id
        if w.stop_run:
            self.gate.request_abort()
            a.actions.append("stopped the running plan")
        for instr, param, safe in w.then:   # only ever toward safe: lab-config watches name safe values
            try:   # its switch goes off too (gate.cut): V = 0 does nothing on a source in current mode
                a.actions += [r.message if r.ok else "could not set %s.%s: %s" % (instr, param, r.message)
                              for r in self.gate.cut(instr, param, "monitor")]
            except Exception as e:
                a.actions.append("could not set %s.%s: %s" % (instr, param, e))
        self.gate.log.write("alarm", by="monitor", watch=w.id, text=text, actions=a.actions or None)
        self.notify("alarm", "Alarm: " + text, "; ".join(a.actions) or (w.note or "Kelvin is watching it."))

    def _check_interlocks(self, now: float) -> None:
        """The interlocks are enforced, not only checked when their drive moves: every INTERLOCK_EVERY lab seconds,
        each one whose drive is away from safe has its condition read. When it fails, or can't be read (an open
        sensor, its instrument gone), an alarm goes up, the running plan stops and the drive goes to safe (or holds
        where the lab config says hold). The alarm clears once the condition holds again or the drive is at safe."""
        if now - self._last_interlock_check < INTERLOCK_EVERY:
            return
        self._last_interlock_check = now
        from .gate import interlock_problem
        for i, lock in enumerate(self.lab.config.interlocks):
            try:
                inst, spec = self.lab.resolve(lock.drive)
                if not inst.enabled:
                    continue   # switched off: Kelvin sends it nothing, and nothing reaches the sample from it
                at = self.gate._current(inst, spec)
            except Exception:
                continue   # its drive can't be read: silence is the read failures' job
            broken = None if at is not None and spec.distance(at) == 0 else interlock_problem(self.lab, lock)
            if broken is None:
                if i in self._broken:
                    self._clear(self._broken.pop(i).id, "interlock %s: %s holds again, or the drive is at safe"
                                % (lock.drive, lock.require.text))
                continue
            if i in self._broken:
                continue
            text = "Interlock: %s is at %s, which needs %s%s" % (lock.drive, spec.show(at), lock.require.text, broken)
            a = self._broken[i] = self._new("interlock", lock.drive, text)
            self.gate.request_abort()
            a.actions.append("stopped the running plan")
            a.actions += [r.message for r in self.gate.abort_to_safe([lock.drive], "monitor")]
            self.gate.log.write("alarm", by="monitor", interlock=lock.drive, text=text, actions=a.actions or None)
            self.notify("alarm", "Alarm: " + text, "; ".join(a.actions))

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
            value = inst.read(spec.readback)
        except Exception:
            return   # silence is the read failures' job
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
            text = "%s has not got closer to %s for %s: it reads %s (set point %s)" % (
                reading, spec.show(target), _minutes(stuck),
                self._show(reading, value) if _number(value) else repr(value), key)
            self._stalled[key] = self._new("stall", key, text)
            self.gate.log.write("alarm", by="monitor", setpoint=key, text=text)
            self.notify("alarm", "Alarm: " + text, "The instrument may be stuck: check its rate, and its helium, heater "
                        "or compressor. A wait for it ends after %s without progress, and so does a run that ramps "
                        "it." % _minutes(WAIT_STALL_S))

    @staticmethod
    def _applied(inst: Any, spec: Any) -> bool:
        """Whether a set point reaches the sample: no switch it applies through, or that switch away from safe (a
        controller's setpoint with its heater range off goes nowhere, and isn't a stall). Followed afresh once on."""
        switch = inst.card.params.get(spec.applies_when) if spec.applies_when else None
        if switch is None:
            return True
        try:
            return switch.distance(inst.read(switch.name)) > 0
        except Exception:
            return False

    def _clear(self, alarm_id: str, text: str) -> None:
        for a in self.alarms:
            if a.id == alarm_id and a.cleared is None:
                a.cleared = time.time()
                self.gate.log.write("alarm_cleared", by="monitor", text=text)
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
            a = self._new("silent", instr, text)
            self._unreadable[instr] = a
            self.gate.log.write("alarm", by="monitor", text=text)
            self.notify("alarm", "Alarm: " + text, "It answers, but not with a reading its card can read: check the "
                                                   "card (Lab → Cards…) and the instrument's settings.")

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
                a = self._silent.get(name) or next((x for x in self.alarms if x.key == name and x.raised >= t0
                                                    and x.kind in ("silent", "reset")), None) \
                    or self._raise_lost(name, h.lost_text)
            out.append(a)
        self._check_health()   # one that answers again is cleared; garbled replies latch their fault
        return out

    def _raise_lost(self, name: str, text: str) -> Alarm:
        a = self._new("silent", name, text)
        self._silent[name] = a
        self.gate.log.write("alarm", by="monitor", instrument=name, text=text)
        self.notify("alarm", "Alarm: " + text, "Runs that use it stop, and plans refuse to start, until it answers "
                                               "again.")
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
                if on is None and inst.enabled and h.fails and (h.gone or h.fails >= FAILS_BEFORE_ALARM):
                    self._raise_lost(name, h.lost_text)
                elif on is not None and h.fails == 0:
                    del self._silent[name]
                    self._clear(on.id, "%s answers again (it didn't for %s)%s" % (
                        name, _since(on.raised), "; a power cut puts an instrument back to its power-on settings "
                        "(outputs off), so check them before going on" if any(s.enables for s in inst.card.drives)
                        else ""))

    def _check_garbles(self, name: str, inst: Any, now: float) -> None:
        h = inst.health
        with self._lock:
            if h.garbles > self._garbles_seen.get(name, 0):
                self._garbles_seen[name] = h.garbles
                self._garbled_lab[name] = now
                if name not in self._comms:
                    a = self._comms[name] = self._new("comms", name, h.garbled_text)
                    self.gate.log.write("alarm", by="monitor", instrument=name, text=a.text)
                    self.notify("alarm", "Alarm: " + a.text, "Raised once: it stays until %s's replies have been clean "
                                                             "for %s. Runs that read it stop at a reply Kelvin can't read."
                                % (name, _dur(GARBLE_CLEAR_S)))
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
            if inst.simulated or not inst.enabled:
                continue
            if now - self._last_alive_check.get(name, -1e18) >= ALIVE_EVERY:
                self._last_alive_check[name] = now
                probe = self._probe(inst)
                try:
                    if probe is not None:
                        probe()
                except Exception:
                    continue   # its health says it, if it didn't answer
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
                                                         and time.time() - a.raised < 600), None)
                text = "%s.%s is %s, though the running plan switched it %s: %s restarted (a power cut puts it back to " \
                       "its power-on settings) or someone switched it off" % (name, param, spec.show(now),
                                                                           spec.show(value), name)
                if recent is not None:   # the same fault as its alarm for not answering: said there, not again
                    recent.text += "; it came back with %s.%s %s (it restarted)" % (name, param, spec.show(now))
                    a = recent
                else:
                    a = self._new("reset", name, text)
                self._reset[name] = a
            self.gate.request_abort()
            a.actions.append("stopped the running plan, which would have gone on measuring with %s.%s %s"
                             % (name, param, spec.show(now)))
            self.gate.log.write("alarm", by="monitor", instrument=name, text=a.text, actions=a.actions)
            if recent is None:
                self.notify("alarm", "Alarm: " + text, "; ".join(a.actions))
            return

    def _check_errors(self, now: float) -> None:
        """Real instruments' own error queues, once a minute (lab time). That it answered or didn't goes into its
        health."""
        for name, inst in self.lab.instruments.items():
            if inst.simulated or inst.card.error_query is None or not inst.enabled:
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
                self._errors[name] = self._new("error", name, text)
                self.gate.log.write("alarm", by="monitor", text=text)
                self.notify("alarm", "Alarm: " + text, "")
            elif not err and name in self._errors:
                self._clear(self._errors.pop(name).id, "%s reports no more errors" % name)
