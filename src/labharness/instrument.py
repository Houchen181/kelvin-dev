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


OFF_TEXT = "%s is switched off in the Lab panel: Kelvin sends it nothing until a person switches it on"
NOT_CONNECTED = "%s is not connected (%s): Kelvin sends it nothing until it connects (reconnect_instrument)"


class InstrumentOff(RuntimeError):
    """A command for an instrument a person switched off (Lab panel)."""


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
    garbles: int = 0                     # replies that arrived garbled since Kelvin connected it
    garbled_text: str = ""               # the latest, in plain words
    garbled_at: Optional[float] = None

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def failed(self, gone: bool, text: str) -> None:
        with self._lock:
            self.fails += 1
            self.losses += 1
            self.gone, self.lost_text, self.lost_at = gone, text, time.time()

    def answered(self) -> None:
        with self._lock:
            self.fails, self.gone, self.answered_at = 0, False, time.time()

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
            self.health.failed(kind == "gone", self.loss_text(e))
            raise InstrumentLost(self.health.lost_text) from e
        if alive:
            self.health.answered()
        return out

    def loss_text(self, e: BaseException) -> str:
        """It doesn't answer, in words: "nvm (Keithley 2182A) doesn't answer on GPIB (address 7): cable or power?
        [VI_ERROR_NLISTENERS]"."""
        where, cause = where_and_cause(self.config.address)
        return "%s (%s) doesn't answer%s%s [%s]" % (self.name, short_model(self.card.model), " " + where if where else "",
                                                    ": " + cause if cause else "", short_code(e))

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
        if spec.get_before:
            return self._acquire(spec)
        if spec.type == "trace":
            return self._trace(spec)
        command = spec.get_cmd.render()
        value = self._parse(spec, command, self._io(self.backend.query, command))
        if spec.status_cmd is not None or spec.plausible is not None:
            self._vouch(spec, value)
        return value

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
        self._io(self.backend.write, spec.set_cmd.render(spec.to_wire(value)))
        self._last[param] = value

    def query_raw(self, command: str) -> str:
        # a maker's driver says its identity itself, without asking the instrument: that shows nothing about it
        local = hasattr(self.backend, "identity") and command.strip().upper() in ("*IDN?", self.card.idn_query.upper())
        return self._io(self.backend.query, command, alive=not local)

    def write_raw(self, command: str) -> None:
        self._io(self.backend.write, command)

    def instrument_error(self) -> Optional[str]:
        """The first queued instrument error, or None."""
        if self.card.error_query is None:
            return None
        command = self.card.error_query.render()
        raw = self._io(self.backend.query, command)
        resp = raw.strip()
        if self._noise(raw):
            raise self._garbled(command, raw)
        if self.card.no_error and re.search(self.card.no_error, resp):   # the card's own no-error reply: 1TE@ (SMC100)
            return None
        if re.match(r"^[+-]?\d+\s*,", resp):
            self._error_form = True
        elif self._error_form:   # it has answered as SCPI's error queue does, and this isn't that: a garbled reply
            raise self._garbled(command, raw)
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
