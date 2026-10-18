"""Watching sensitive instruments: readings that leave their range, instruments that stop answering or report
errors. A watch only reads. When it trips it raises an alarm once (and clears it when the reading is back), tells
people (see notify.py), and, for watches the lab owner wrote, can stop the running plan and send named drives to
their safe values - the only kind of change it may make.

Durations are in lab time (the lab clock), so the virtual lab at 10x real time and tests on a simulated clock behave
like the real lab.
"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

FAILS_BEFORE_ALARM = 3       # consecutive failed reads before "not answering"
ERROR_CHECK_EVERY = 60.0     # lab seconds between error-queue checks on real instruments
_ids = itertools.count(1)


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
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

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
                raise PermissionError("%s comes from the lab config; only the lab owner can change it" % watch_id)
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
            try:
                value = self.lab.read(*w.key.split(".", 1))
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
        a = self._new("reading", w.key, text)
        w.alarm = a.id
        if w.stop_run:
            self.gate.request_abort()
            a.actions.append("stopped the running plan")
        for instr, param, safe in w.then:   # only ever toward safe: lab-config watches name safe values
            try:
                r = self.gate._set(instr, param, safe, False, "monitor")
                a.actions.append(r.message if r.ok else "could not set %s.%s: %s" % (instr, param, r.message))
            except Exception as e:
                a.actions.append("could not set %s.%s: %s" % (instr, param, e))
        self.gate.log.write("alarm", by="monitor", watch=w.id, text=text, actions=a.actions or None)
        self.notify("alarm", "Alarm: " + text, "; ".join(a.actions) or (w.note or "Kelvin is watching it."))

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
            if inst.simulated or inst.card.error_query is None:
                continue
            if now - self._last_error_check.get(name, -1e18) < ERROR_CHECK_EVERY:
                continue
            self._last_error_check[name] = now
            try:
                err = inst.instrument_error()
            except Exception:
                continue  # silence is the read failures' job
            if err and name not in self._errors:
                text = "%s reports an error: %s" % (name, err)
                self._errors[name] = self._new("error", name, text)
                self.gate.log.write("alarm", by="monitor", text=text)
                self.notify("alarm", "Alarm: " + text, "")
            elif not err and name in self._errors:
                self._clear(self._errors.pop(name).id, "%s reports no more errors" % name)
