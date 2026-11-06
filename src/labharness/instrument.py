"""An instrument = its card + a backend (a twin now, VISA/serial/... in Phase 1).

These methods talk to hardware with no safety checks. Only the safety gate
and the run engine call them; the agent never gets an Instrument.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .adapters import lost_kind, short_code, short_model, where_and_cause
from .cards import BINARY_REPLY, Card, ParamSpec
from .clock import Stops
from .labconfig import Guard, InstrumentConfig


OFF_TEXT = "%s is disconnected from Kelvin in the Lab panel: Kelvin sends it nothing until a person connects it"
NOT_CONNECTED = "%s is not connected (%s): Kelvin sends it nothing until it connects (reconnect_instrument)"
GUARD_S = 30.0    # how long what a card's check before a reading found holds, unless a command goes to it meanwhile
SHARED_S = 1.0    # how long one reply (:READ?) may serve the other readings that take a field of it


class InstrumentOff(RuntimeError):
    """A command for an instrument a person switched off (Lab panel)."""


class NotHere(KeyError):
    """An instrument Kelvin doesn't have, or can't use now (switched off, not connected), in plain words: a KeyError,
    as every caller that refuses an unknown instrument catches one, whose text isn't in quotes as a KeyError's is."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


class Stopped(RuntimeError):
    """A person's stop ended a wait on the instrument (an acquisition it was still taking)."""


class SensorError(RuntimeError):
    """The instrument answered, but the reading isn't one: its card's status query flags it, or it is outside what it
    can be (an open thermometer's 0 K). Raised instead of returning it, so a guard trips, an interlock fails and a
    run stops rather than taking it for a value."""


class InstrumentLost(RuntimeError):
    """The instrument didn't answer, or can't be reached at all, said in plain words: which one, on which bus, and what
    to check ("nvm (Keithley 2182A) doesn't answer on GPIB (address 7): cable or power? [VI_ERROR_NLISTENERS]").
    A run that met it stopped with these words, not a raw VISA code; the error it came from is its __cause__."""


class InstrumentFault(RuntimeError):
    """The instrument answers, but didn't do what was asked: its error queue says why (830, "Invalid with INFinite
    ARM:COUNT"), or a check its card makes before the reading doesn't hold (health: before). Not a lost instrument, so
    never "cable or power?" (a field test's alarms all said that of a Keithley left measuring continuously)."""


class Garbled(RuntimeError):
    """A reply that arrived garbled: characters no instrument sends, a reading that always read before and now can't
    be, an error-queue reply that isn't one. A loose cable, a bad adapter or electrical noise; nothing in it can be
    trusted, so it is neither a value nor an instrument error (field test: a garbled "0,No error" was alarmed as an
    instrument error and cleared again 42 times in a night)."""


@dataclass
class Health:
    """How an instrument has been answering, kept by every exchange with it (Instrument._io), whoever made it: a run,
    a set, the monitor. The monitor turns it into alarms; a run that stopped on a failure leaves it here for them."""
    fails: int = 0                       # exchanges in a row that got no answer
    gone: bool = False                   # the latest says it can't be reached at all (not only slow to answer)
    losses: int = 0                      # exchanges that got no answer since Kelvin connected it
    lost_text: str = ""                  # the latest, in plain words
    lost_at: Optional[float] = None      # when it last didn't answer (wall time)
    answered_at: Optional[float] = None  # when it last answered
    last_ok: str = ""                    # the last command it answered
    first_failed: str = ""               # the command it first didn't answer, in the run of failures now
    first_failed_at: Optional[float] = None
    garbles: int = 0                     # replies that arrived garbled since Kelvin connected it
    garbled_text: str = ""               # the latest, in plain words
    garbled_at: Optional[float] = None

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def failed(self, gone: bool, text: str, command: str = "") -> None:
        with self._lock:
            if not self.fails:   # the first of a run of failures: which command, and when (a hung bus's first)
                self.first_failed, self.first_failed_at = command, time.time()
            self.fails += 1
            self.losses += 1
            self.gone, self.lost_text, self.lost_at = gone, text, time.time()

    def answered(self) -> None:
        with self._lock:
            self.fails, self.gone, self.answered_at = 0, False, time.time()

    def resting(self, now: Optional[float] = None) -> Optional[float]:
        """Until when Kelvin's own looks (the Lab panel, the idle checks) leave it alone after it stopped answering:
        5 s after the first exchange that got no answer, twice as long after each one more, a minute at most. None
        when they may ask it now. Asking a silent instrument every 2 s kept a hung GPIB bus busy (field test)."""
        with self._lock:
            if not self.fails or self.lost_at is None:
                return None
            until = self.lost_at + min(60.0, 5.0 * 2 ** min(self.fails - 1, 4))
        return until if (time.time() if now is None else now) < until else None

    def garbled(self, text: str) -> None:
        with self._lock:   # it did answer, if not clearly
            self.fails, self.gone = 0, False
            self.garbles += 1
            self.garbled_text, self.garbled_at = text, time.time()


class OffBackend:
    """Stands in for a switched-off instrument's connection: it sends nothing, and refuses every command."""
    simulated = False

    def __init__(self, name: str):
        self.name = name

    def write(self, command: str) -> None:
        raise InstrumentOff(OFF_TEXT % self.name)

    def query(self, command: str) -> str:
        raise InstrumentOff(OFF_TEXT % self.name)

    def close(self) -> None:
        pass


class Unavailable(OffBackend):
    """Stands in for an instrument that could not be connected when its lab was (its maker's package missing, the
    instrument off or unplugged): nothing is sent to it, and Kelvin says why, until it connects (Lab.reconnect)."""

    def __init__(self, name: str, reason: str):
        super().__init__(name)
        self.reason = reason

    def write(self, command: str) -> None:
        raise InstrumentOff(NOT_CONNECTED % (self.name, self.reason))

    def query(self, command: str) -> str:
        raise InstrumentOff(NOT_CONNECTED % (self.name, self.reason))


class Instrument:
    def __init__(self, name: str, card: Card, backend: Any, config: InstrumentConfig, stops: Optional[Stops] = None):
        self.name = name
        self.card = card
        self.backend = backend
        self.config = config
        self.stops = stops or Stops()   # the lab's: a person's stop ends a long wait on this instrument
        self._last: Dict[str, Any] = {}   # what Kelvin set, for parameters with no read-back
        # the mounted device's protection (protection.py): it only ever narrows the lab's limits and rates
        self._device_name: Optional[str] = None
        self.device_shown: Callable[[], bool] = lambda: True   # may whoever asks now see its name (the runtime says)
        self.device_limits: Dict[str, Tuple[float, float]] = {}
        self.device_rates: Dict[str, float] = {}
        self.device_guards: Dict[str, List[Guard]] = {}
        self.health = Health()   # how it has been answering: the monitor's instrument-loss alarms read it
        self._read_ok: set = set()   # readings whose replies parsed: one that later can't is a garbled reply
        self._error_form = False     # its error query has answered as SCPI's do (-113,"Undefined header")
        # what its card's checks before readings (health: before) found lately: check -> (when, holds, reply)
        self._checked: Dict[Tuple[int, int], Tuple[float, bool, str]] = {}
        # one reply that several readings take their fields from (a Keithley 2400's :READ? gives V and I), shared by
        # those read right after it: command -> (when, reply, the readings it has served). Never one reading twice, and
        # nothing after any command is sent: the next reading of the same one asks again
        self._shared: Dict[str, Tuple[float, str, set]] = {}
        self._sharers: Optional[Tuple[Any, Dict[str, set]]] = None
        self._cache_lock = threading.Lock()

    def _forget(self) -> None:
        """A command went to it (a setting, an action, a raw command), or an exchange failed: what its checks found
        and a reply readings shared no longer hold."""
        with self._cache_lock:
            self._checked.clear()
            self._shared.clear()

    @property
    def device_name(self) -> Optional[str]:
        """The device whose protection holds here, by name; None for whoever may not see it (the sample of unshared
        work, asked from another chat: then refusals and descriptions say "device" only)."""
        return self._device_name if self._device_name and self.device_shown() else None

    @device_name.setter
    def device_name(self, name: Optional[str]) -> None:
        self._device_name = name

    def _io(self, fn: Callable[..., Any], *args: Any, alive: bool = True) -> Any:
        """One exchange with the instrument, kept in its health. No answer, or no connection, is raised in plain words
        (InstrumentLost: which instrument, on which bus, what to check), wherever it happens; any other error (a
        command it refused, a card's mistake) passes as it is. alive=False: an answer that doesn't show it is there
        (a maker's driver says its identity by itself)."""
        try:
            out = fn(*args)
        except (InstrumentOff, Stopped, InstrumentLost):
            raise
        except Exception as e:
            kind = lost_kind(e)
            if kind is None:
                raise
            self._forget()
            why = self._said_instead(fn, args) if kind == "silent" else None
            if why:   # it answers its error queue: not lost; it couldn't do this one, and says why
                self.health.answered()
                raise InstrumentFault(why) from e
            command = str(args[0]).strip() if args and isinstance(args[0], str) else ""
            self.health.failed(kind == "gone", self.loss_text(e, command), command)
            raise InstrumentLost(self.health.lost_text) from e
        if alive:
            self.health.answered()
            if args and isinstance(args[0], str):
                self.health.last_ok = args[0].strip()
        return out

    def _said_instead(self, fn: Callable[..., Any], args: Tuple[Any, ...]) -> Optional[str]:
        """After a query it didn't answer in time: what its error queue says, asked at once (once it is back in step,
        in a second at most), in words, or None when it says nothing or doesn't answer that either. A Keithley 2400
        left measuring continuously answers nothing to :READ? and queues 830 "Invalid with INFinite ARM:COUNT"."""
        ask = getattr(self.backend, "ask_after_timeout", None)
        query = getattr(self.backend, "query", None)
        if ask is None or self.card.error_query is None or not args or getattr(fn, "__func__", fn) is not getattr(
                query, "__func__", query):
            return None
        command, errors = str(args[0]).strip(), []
        error_query = self.card.error_query.render()
        if command.upper() == error_query.upper():
            return None
        for _ in range(3):
            raw = ask(error_query)
            if raw is None:
                break
            try:
                err = self._error_from(error_query, raw)
            except Garbled:
                break
            if err is None:
                break
            errors.append(err)
        if not errors:
            return None
        return "%s didn't answer %s: it reports %s%s" % (self.name, command, errors[0], " (and %d more: %s)" % (
            len(errors) - 1, "; ".join(errors[1:])) if len(errors) > 1 else "")

    def loss_text(self, e: BaseException, command: str = "") -> str:
        """It doesn't answer, in words, by what the bus said: "nvm (Keithley 2182A) doesn't answer on GPIB (address 7):
        nothing listens there: switched off, unplugged, or set to another address? [VI_ERROR_NLISTENERS]"; for one
        that answered a moment ago, only this command didn't come back in time. A field test's alarms all said "cable or
        power?" of instruments that were on and cabled, one waiting for a trigger, the bus hung behind it."""
        where, cause = where_and_cause(self.config.address)
        code = short_code(e)
        model = short_model(self.card.model)
        h = self.health
        if code == "VI_ERROR_TMO" and h.answered_at and not h.fails and time.time() - h.answered_at < 300:
            return "%s (%s) didn't answer %s in time%s; it answered %s at %s [%s]" % (
                self.name, model, command or "a command", " " + where if where else "", h.last_ok or "a command",
                time.strftime("%H:%M:%S", time.localtime(h.answered_at)), code)
        if code == "VI_ERROR_NLISTENERS":
            cause = "nothing listens there: switched off, unplugged, or set to another address?"
        elif code == "VI_ERROR_TMO" and where.startswith("on GPIB"):
            cause = ("it doesn't finish answering: busy, waiting for a trigger, or the bus hung (then every instrument "
                     "on it times out)?")
        return "%s (%s) doesn't answer%s%s [%s]" % (self.name, model, " " + where if where else "",
                                                    ": " + cause if cause else "", code)

    def garble_text(self, command: str, reply: str) -> str:
        """Its reply arrived garbled, in words: "nvm's reply to :READ? arrived garbled ('k5.1438:127E,04') on GPIB
        (address 7): a loose cable, a bad adapter or electrical noise; ..."."""
        where, _ = where_and_cause(self.config.address)
        return ("%s's reply to %s arrived garbled (%r)%s: a loose cable, a bad adapter or electrical noise; its "
                "readings can't be trusted until its replies are clean again" % (
                    self.name, command.strip(), reply.strip()[:60], " " + where if where else ""))

    def _garbled(self, command: str, reply: str) -> Garbled:
        self.health.garbled(self.garble_text(command, reply))
        return Garbled(self.health.garbled_text)

    @staticmethod
    def _noise(reply: str) -> bool:
        """Characters no instrument puts in a text reply: control characters other than tabs and line ends."""
        return any((ord(c) < 32 and c not in "\t\r\n") or ord(c) == 127 for c in reply)

    def _parse(self, spec: ParamSpec, command: str, reply: str) -> Any:
        """A reading from its reply. One that can't be read is a garbled reply when this reading has read before;
        the first reply it ever gets that can't be read is the card's (a wrong command or format, or a binary reply
        to a text query), whatever characters it holds."""
        try:
            value = spec.from_wire(reply)
        except ValueError:
            if isinstance(reply, str) and spec.name in self._read_ok:
                raise self._garbled(command, reply) from None
            raise
        self._read_ok.add(spec.name)
        return value

    @property
    def simulated(self) -> bool:
        return bool(getattr(self.backend, "simulated", False))

    @property
    def enabled(self) -> bool:
        """False when a person switched it off in the Lab panel, or it could not be connected: then nothing is sent
        to it."""
        return not isinstance(self.backend, OffBackend)

    @property
    def unavailable(self) -> Optional[str]:
        """Why it could not be connected, while it isn't; None otherwise."""
        return self.backend.reason if isinstance(self.backend, Unavailable) else None

    @property
    def trust(self) -> str:
        return self.config.trust

    def spec(self, param: str) -> ParamSpec:
        return self.card.param(param)

    def read(self, param: str) -> Any:
        """The parameter's value; None if the card says to read it only while a switch is on and it is off."""
        spec = self.spec(param)
        if spec.read_when is not None and not self.read(spec.read_when):
            return None
        if spec.get_cmd is None:
            if param in self._last:
                return self._last[param]
            if spec.default is not None:
                return spec.default
            raise ValueError("%s.%s cannot be read" % (self.name, param))
        self._guard(spec)
        if spec.get_before:
            return self._acquire(spec)
        if spec.type == "trace":
            return self._trace(spec)
        command = spec.get_cmd.render()
        reply = self._shared_reply(spec.name, command)
        if reply is None:
            reply = self._io(self.backend.query, command)
            self._keep_reply(spec.name, command, reply)
        value = self._parse(spec, command, reply)
        if spec.status_cmd is not None or spec.plausible is not None:
            self._vouch(spec, value)
        return value

    def _guard(self, spec: ParamSpec) -> None:
        """The checks its card makes before this reading (health: before), each asked again after GUARD_S or once
        any command went to it: while one doesn't hold, InstrumentFault in the card's words, and the reading isn't
        asked (a Keithley 2400 left measuring continuously never answers :READ?, and asking again and again hung a
        GPIB bus)."""
        card = self.card
        for i, check in enumerate(card.health):
            if spec.name not in check.before:
                continue
            key = (id(card), i)
            with self._cache_lock:
                kept = self._checked.get(key)
            if kept is None or time.monotonic() - kept[0] > GUARD_S:
                reply = self._io(self.backend.query, check.query).strip()
                kept = (time.monotonic(), bool(re.search(check.expect or "", reply, re.I)), reply)
                with self._cache_lock:
                    self._checked[key] = kept
            if not kept[1]:
                raise InstrumentFault("%s doesn't read %s now: %s (%s answered %r)" % (
                    self.name, spec.name, check.says or "a check its card makes first doesn't hold", check.query,
                    kept[2][:60]))

    def _sharing(self) -> Dict[str, set]:
        """The commands whose one reply more than one reading takes its value from (fields of :READ?), by command."""
        card = self.card
        with self._cache_lock:
            if self._sharers is not None and self._sharers[0] is card:
                return self._sharers[1]
        by: Dict[str, set] = {}
        for p in card.params.values():
            if p.get_cmd is None or p.get_before or p.acquires or p.type in ("trace", "image") or p.status_cmd:
                continue
            try:
                by.setdefault(p.get_cmd.render(), set()).add(p.name)
            except Exception:
                continue
        shared = {c: names for c, names in by.items() if len(names) > 1}
        with self._cache_lock:
            self._sharers = (card, shared)
        return shared

    def _shared_reply(self, name: str, command: str) -> Optional[str]:
        """The reply another reading just got to this same command, if this reading hasn't taken it yet."""
        if command not in self._sharing():
            return None
        with self._cache_lock:
            kept = self._shared.get(command)
            if kept is None or name in kept[2] or time.monotonic() - kept[0] > SHARED_S:
                return None
            kept[2].add(name)
            return kept[1]

    def _keep_reply(self, name: str, command: str, reply: str) -> None:
        if command in self._sharing():
            with self._cache_lock:
                self._shared[command] = (time.monotonic(), reply, {name})

    def _vouch(self, spec: ParamSpec, value: Any) -> None:
        """SensorError unless the card's status query says the reading is valid and it is plausible."""
        if spec.status_cmd is not None:
            reply = self._io(self.backend.query, spec.status_cmd.render()).strip()
            if not re.search(spec.status_ok, reply):
                raise SensorError("%s.%s: the instrument flags this reading (%s → %s): the sensor may be open, shorted "
                                  "or out of its range" % (self.name, spec.name, spec.status_cmd.render(), reply))
        lim = spec.plausible
        if lim is not None and not (isinstance(value, (int, float)) and lim[0] <= value <= lim[1]):
            raise SensorError("%s.%s reads %s, which it can't really be (%s to %s): the sensor may be open or shorted"
                              % (self.name, spec.name, spec.show(value), spec.show(lim[0]), spec.show(lim[1])))

    def _acquire(self, spec: ParamSpec) -> Any:
        """A reading that runs an acquisition: its commands first (arm, trigger; *OPC? waits until it is done), then
        the fetch, all in one go on the connection, with the time the card gives it."""
        from .scpi import is_query
        lock = getattr(self.backend, "lock", None)
        held = lock if lock is not None and hasattr(lock, "_is_owned") else None   # re-entrant: hold it throughout
        res = getattr(self.backend, "res", None)
        before = getattr(res, "timeout", None)
        if held is not None:
            held.acquire()
        try:
            if spec.wait_s and before is not None:
                res.timeout = spec.wait_s * 1000.0
            start = self.stops.count
            waiting = getattr(self.backend, "query_waiting", None)
            for c in spec.get_before:
                if self.stops.since(start):
                    raise Stopped("%s.%s: stopped by a person before the instrument was done" % (self.name, spec.name))
                if not is_query(c, bool(self.card.connection.get("enq"))):
                    self._io(self.backend.write, c)
                elif waiting is not None and spec.wait_s:   # *OPC?: answers when the acquisition is done
                    self._io(waiting, c, spec.wait_s, lambda: self.stops.since(start))   # or a stop ends the wait
                else:
                    self._io(self.backend.query, c)
            if spec.type == "trace":
                return self._trace(spec)
            return spec.from_wire(self._io(self.backend.query, spec.get_cmd.render()))
        finally:
            if spec.wait_s and before is not None:
                try:
                    res.timeout = before
                except Exception:
                    pass   # the connection was renewed after a stop
            if held is not None:
                held.release()

    def act(self, name: str) -> List[str]:
        """Send one of the card's named actions; what queries among them answered."""
        from .scpi import is_query
        action = self.card.actions[name]
        said = []
        self._forget()
        for c in action.send:
            if is_query(c, bool(self.card.connection.get("enq"))):
                said.append(self._io(self.backend.query, c).strip())
            else:
                self._io(self.backend.write, c)
        return said

    def _trace(self, spec: ParamSpec) -> List[float]:
        """A trace: as text, or as a block of binary numbers (binary: on the card) where the connection can read one;
        then in its units through the card's y expression of raw (or of re and im, for a trace sent as two numbers
        per point) and the instrument's own scale readings."""
        command = spec.get_cmd.render()
        if spec.binary and hasattr(self.backend, "query_binary"):
            values = self._io(self.backend.query_binary, command, spec.binary)
        else:
            query = getattr(self.backend, "query_trace", self.backend.query)   # a reply of many lines, read whole
            try:
                values = spec.from_wire(self._io(query, command))
            except ValueError as e:
                if str(e) == BINARY_REPLY and hasattr(self.backend, "drain"):
                    self.backend.drain()   # the rest of the block would be taken for the next replies
                raise ValueError("%s.%s: %s" % (self.name, spec.name, e)) from None
        if spec.pairs and len(values) % 2:
            raise ValueError("%s.%s: %d numbers came, not two per point (pairs)" % (self.name, spec.name, len(values)))
        if not spec.y or not values:
            return values[0::2] if spec.pairs else values
        import numpy as np
        from . import expr
        raw, known = np.asarray(values, dtype=float), {}
        own = {"re": raw[0::2], "im": raw[1::2]} if spec.pairs else {"raw": raw}

        def column(name: str) -> Any:
            if name in own:
                return own[name]
            if name not in self.card.params:
                raise KeyError("%s has no parameter %r" % (self.card.id, name))
            if name not in known:
                known[name] = np.float64(float(self.read(name)))
            return known[name]
        shape = next(iter(own.values())).shape
        return [float(v) for v in np.broadcast_to(expr.evaluate(spec.y, column, np), shape)]

    def write(self, param: str, value: Any) -> None:
        spec = self.spec(param)
        self._forget()
        self._io(self.backend.write, spec.set_cmd.render(spec.to_wire(value)))
        self._last[param] = value

    def query_raw(self, command: str) -> str:
        # a maker's driver says its identity itself, without asking the instrument: that shows nothing about it
        local = hasattr(self.backend, "identity") and command.strip().upper() in ("*IDN?", self.card.idn_query.upper())
        self._forget()   # a raw query may change what it does (:INIT;*OPC?)
        return self._io(self.backend.query, command, alive=not local)

    def write_raw(self, command: str) -> None:
        self._forget()
        self._io(self.backend.write, command)

    def instrument_error(self) -> Optional[str]:
        """The first queued instrument error, or None."""
        if self.card.error_query is None:
            return None
        command = self.card.error_query.render()
        return self._error_from(command, self._io(self.backend.query, command))

    def _error_from(self, command: str, raw: str) -> Optional[str]:
        """The error an error-queue reply says, or None for none (Garbled for one no error queue gives)."""
        resp = raw.strip()
        if self._noise(raw):
            raise self._garbled(command, raw)
        if self.card.no_error and re.search(self.card.no_error, resp):   # the card's own no-error reply: 1TE@ (SMC100)
            return None
        if re.match(r"^[+-]?\d+\s*,", resp):
            self._error_form = True
        elif self._error_form:   # it has answered as SCPI's error queue does, and this isn't that: a garbled reply
            raise self._garbled(command, raw)
        fields = [f.strip() for f in resp.split(",")]
        if len(fields) > 1 and all(re.match(r"^[+-]?\d+$", f) for f in fields):   # error registers, each a number:
            return None if all(int(f) == 0 for f in fields) else resp   # a Lake Shore 625's quench is 000,032,000
        return None if re.match(r"^\+?0+\s*(,|$)", resp) else resp   # "0,No error", 0 (SRS LERR?), 0000 (Pfeiffer ERR)

    def drain_errors(self, most: int = 10) -> Tuple[List[str], bool]:
        """Every error queued now, oldest first: read until the queue says no error, at most `most` times, so a
        command's own errors aren't mixed with older ones and the queue doesn't overflow (-350). Also whether the
        reply is a standing status that reading doesn't clear (`most` identical replies, as Pfeiffer's ERR gives)."""
        out: List[str] = []
        while len(out) < most:
            err = self.instrument_error()
            if err is None:
                return out, False
            out.append(err)
        return out, len(set(out)) == 1

    def limits(self, param: str) -> Optional[Tuple[float, float]]:
        """The lab's limits (or the card's), narrowed to the mounted device's: the tighter side always holds."""
        base = self.config.limits.get(param) or self.spec(param).limits
        dev = self.device_limits.get(param)
        if dev is None:
            return base
        if base is None:
            return dev
        return max(base[0], dev[0]), min(base[1], dev[1])

    def limits_source(self, param: str) -> str:
        dev = self.device_limits.get(param)
        base = self.config.limits.get(param) or self.spec(param).limits
        if dev is not None and (base is None or dev[0] > base[0] or dev[1] < base[1]):
            return "device %s" % self.device_name if self.device_name else "device"
        return "lab" if param in self.config.limits else "vendor"

    def limits_where(self, param: str) -> str:
        """Whose limits these are, in words: the card's (the instrument's own range), the lab config's, or a mounted
        device's."""
        src = self.limits_source(param)
        return {"vendor": "its card's", "lab": "the lab config's"}.get(src, src)

    def rate(self, param: str) -> Optional[float]:
        base = self.config.rates.get(param) or self.spec(param).max_rate
        dev = self.device_rates.get(param)
        return min(base, dev) if base and dev else (dev or base)

    def guards(self, param: str) -> List[Guard]:
        return self.config.guards.get(param, []) + self.device_guards.get(param, [])

    def close(self) -> None:
        close = getattr(self.backend, "close", None)
        if close:
            close()
