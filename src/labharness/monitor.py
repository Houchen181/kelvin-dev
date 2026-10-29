"""Watching sensitive instruments: readings that leave their range, instruments that stop answering or report
errors. A watch only reads. When it trips it raises an alarm once (and clears it when the reading is back), tells
people (see notify.py), and, for watches written in the lab config, can stop the running plan and send named drives to
their safe values - the only kind of change it may make.

Durations are in lab time (the lab clock), so the virtual lab at 10x real time and tests on a simulated clock behave
like the real lab.
"""
from __future__ import annotations

import itertools
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .instrument import SensorError

FAILS_BEFORE_ALARM = 3       # consecutive failed reads before "not answering"
ERROR_CHECK_EVERY = 60.0     # lab seconds between error-queue checks on real instruments
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
        self.alarms: List[Alarm] = []        # newest first, active and cleared
        self._fails: Dict[str, int] = {}
        self._silent: Dict[str, Alarm] = {}
        self._errors: Dict[str, Alarm] = {}
        self._last_error_check: Dict[str, float] = {}
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

    def tick(self) -> None:
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
        reaches (0 K, which a stop holds short of)."""
        if now - self._last_setpoint_check < SETPOINT_EVERY:
            return
        self._last_setpoint_check = now
        from .gate import WAIT_STALL_S, _unreachable
        seen = set()
        for name, inst in list(self.lab.instruments.items()):
            if not inst.enabled:
                continue
            for spec in inst.card.params.values():
                if not (spec.is_drive and spec.ramp == "instrument" and spec.readback and spec.numeric):
                    continue
                target = inst._last.get(spec.name)
                if not _number(target) or (_unreachable(spec) and target <= 0):
                    continue
                key = "%s.%s" % (name, spec.name)
                seen.add(key)
                move = self._moves.get(key)
                if move is None or move.target != float(target):
                    stalled = self._stalled.pop(key, None)
                    if stalled is not None:
                        self._clear(stalled.id, "%s has a new set point (%s)" % (key, spec.show(target)))
                    move = self._moves[key] = Approach(target, spec.tol, now, rate_per_s(inst, spec), self.stall_s)
                if move.arrived:
                    continue   # it got there; a reading that drifts off later is for a watch
                try:
                    value = inst.read(spec.readback)
                except Exception:
                    continue   # silence is the read failures' job
                stuck = move.update(value, now)
                reading = "%s.%s" % (name, spec.readback)
                if move.there(value):
                    move.arrived = True
                    if key in self._stalled:
                        self._clear(self._stalled.pop(key).id, "%s reached its set point (%s)" % (
                            reading, spec.show(target)))
                elif key in self._stalled and stuck < self.stall_s:
                    self._clear(self._stalled.pop(key).id, "%s is getting closer to %s again (%s)" % (
                        reading, spec.show(target), self._show(reading, value)))
                elif key not in self._stalled and stuck >= self.stall_s:
                    text = "%s has not got closer to %s for %s: it reads %s (set point %s)" % (
                        reading, spec.show(target), _minutes(stuck),
                        self._show(reading, value) if _number(value) else repr(value), key)
                    a = self._stalled[key] = self._new("stall", key, text)
                    self.gate.log.write("alarm", by="monitor", setpoint=key, text=text)
                    self.notify("alarm", "Alarm: " + text, "The instrument may be stuck: check its rate, and its "
                                "helium, heater or compressor. A wait for it ends after %s without progress, and so "
                                "does a run that ramps it." % _minutes(WAIT_STALL_S))
        for key in [k for k in self._moves if k not in seen]:   # switched off, or no longer set by Kelvin
            del self._moves[key]
            if key in self._stalled:
                self._clear(self._stalled.pop(key).id, "%s is no longer followed" % key)

    def _clear(self, alarm_id: str, text: str) -> None:
        for a in self.alarms:
            if a.id == alarm_id and a.cleared is None:
                a.cleared = time.time()
                self.gate.log.write("alarm_cleared", by="monitor", text=text)
                self.notify("alarm_cleared", "Cleared: " + text, "")

    def _failed(self, instr: str, e: Exception) -> None:
        self._fails[instr] = self._fails.get(instr, 0) + 1
        if self._fails[instr] >= FAILS_BEFORE_ALARM and instr not in self._silent:
            text = "%s is not answering (%s)" % (instr, str(e).splitlines()[0][:100] if str(e) else type(e).__name__)
            a = self._new("silent", instr, text)
            self._silent[instr] = a
            self.gate.log.write("alarm", by="monitor", text=text)
            self.notify("alarm", "Alarm: " + text, "Check its cable and power; plans that use it will refuse to start.")

    def _answered(self, instr: str) -> None:
        self._fails[instr] = 0
        a = self._silent.pop(instr, None)
        if a is not None:
            self._clear(a.id, "%s answers again" % instr)

    def _check_errors(self, now: float) -> None:
        """Real instruments' own error queues, once a minute (lab time)."""
        for name, inst in self.lab.instruments.items():
            if inst.simulated or inst.card.error_query is None or not inst.enabled:
                continue
            if now - self._last_error_check.get(name, -1e18) < ERROR_CHECK_EVERY:
                continue
            self._last_error_check[name] = now
            try:   # the whole queue, so it never overflows (-350) while one error a minute is read
                errs, _ = inst.drain_errors()
            except Exception:
                continue  # silence is the read failures' job
            err = errs[0] if errs else None
            if err and name not in self._errors:
                text = "%s reports an error: %s%s" % (name, err, " (and %d more)" % (len(set(errs)) - 1)
                                                      if len(set(errs)) > 1 else "")
                self._errors[name] = self._new("error", name, text)
                self.gate.log.write("alarm", by="monitor", text=text)
                self.notify("alarm", "Alarm: " + text, "")
            elif not err and name in self._errors:
                self._clear(self._errors.pop(name).id, "%s reports no more errors" % name)
