"""A lab runtime: the instruments of one setup, its config and its clock."""
from __future__ import annotations

import contextvars
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .cards import Card, CardRegistry, ParamSpec, num
from .adapters import AdapterError, timed_out
from .clock import SimClock, Stops
from .instrument import (NOT_CONNECTED, OFF_TEXT, Garbled, Instrument, InstrumentFault, InstrumentLost, InstrumentOff,
                         NotHere, NotNow, OffBackend, Unavailable)
from .labconfig import InstrumentConfig, LabConfig

log = logging.getLogger("kelvin")
BackendFactory = Callable[[str, Card, str], Any]
SwitchedOff = Callable[[str], Iterable[str]]   # a lab's name -> the instruments a person switched off in it
# how long an instrument's driver may take to open it (a maker's driver that takes longer by itself is given its own
# time: open_deadline): past it the instrument is set aside with why and the lab goes on. A camera SDK's open that
# never returned held the lab, the first start and every restart for good (review of 0.3.45)
OPEN_S = 60.0
ENDED_CLOSE_S = 10.0   # a connection that ended by itself (its driver didn't return) is closed waiting this long at most
CONNECTING_AGAIN = "Kelvin is connecting it"   # while a person's switch on (Lab panel) opens it


class OpenTimedOut(AdapterError):
    """An instrument's driver didn't finish opening it in time (open_deadline): it is set aside, said in words."""


@dataclass
class HealthResult:
    instr: str
    ok: bool
    problems: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)   # what is worth knowing and isn't a problem (what answers there)
    # readings that can't be asked in the state the instrument is in now, and why (its card's checks: health
    # before): a plan that measures one is refused; every other one may go (a 2400 another program left taking ten
    # readings a trigger still sweeps its source)
    not_now: Dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:
        said = " (%s)" % "; ".join(self.notes) if self.notes else ""
        return "%s ok%s" % (self.instr, said) if self.ok else "%s FAIL (%s)%s" % (self.instr, "; ".join(self.problems),
                                                                                said)


# the lab config entry of an instrument being connected and not yet in the lab (Lab.add, Lab.replace): a backend
# factory reads its connection settings here first, so nothing else sees the instrument half added
_CONNECTING: "contextvars.ContextVar[Optional[InstrumentConfig]]" = contextvars.ContextVar("kelvin_connecting",
                                                                                          default=None)


def connecting_entry(name: str) -> Optional[InstrumentConfig]:
    """The lab config entry of this instrument while Lab.add or Lab.replace connects it, else None."""
    ic = _CONNECTING.get()
    return ic if ic is not None and ic.name == name else None


def real_backend_factory(name: str, card: Card, address: str) -> Any:
    """A real instrument over VISA (GPIB, USB, LAN, serial); see adapters.py."""
    from .adapters import open_backend
    return open_backend(name, card, address)



def _ended(backend: Any) -> str:
    """Why a connection that still counts as made has ended by itself, or "": a camera library's helper Kelvin ended
    after a call that hung (princeton.py), or a maker's driver whose call didn't return in time (drivers.py
    DriverBackend.bounded), is gone, and reconnecting it makes a fresh one."""
    helper = getattr(backend, "helper", None)
    return str(getattr(backend, "gone", "") or getattr(helper, "gone", "") or "")


def _close_quietly(inst: Instrument) -> None:
    try:
        inst.close()
    except Exception:
        pass   # closing: nothing more to do with it


def _close_backend(backend: Any) -> None:
    try:
        backend.close()
    except Exception:
        pass   # closing: nothing more to do with it


def _let_go(backend: Any, within: float = ENDED_CLOSE_S) -> bool:
    """A connection closed on a thread of its own, waiting at most `within` seconds: one that ended because its driver
    didn't return may not return from closing either, and must not hold the lab. True if it closed in time."""
    t = threading.Thread(target=_close_backend, args=(backend,), name="kelvin-close", daemon=True)
    t.start()
    t.join(within)
    return not t.is_alive()


def _close_now(backend: Any) -> None:
    """Close a connection: at once, or on a thread of its own (bounded) when it ended by itself (_let_go)."""
    if _ended(backend):
        _let_go(backend)
    else:
        backend.close()


def open_deadline(card: Card, address: str) -> float:
    """How long an instrument's driver may take to open it: OPEN_S (a VISA session opens in seconds, its card's
    configuration commands with it); longer for a maker's driver that takes longer by itself (drivers.OPEN_S:
    LightField starting, PICam's helper, the lib:// helper), and a lib:// card's own open steps (each a call into the
    maker's library, at the card's timeout) on top. A card whose own timeout is long (a Thorlabs KDC101's 70 s) has
    two exchanges' worth of it at least, so that an open that fails says why in its own words (a timeout on a hung
    GPIB bus), not only that it took too long."""
    from .drivers import OPEN_S as OWN
    scheme = address.split("://", 1)[0].lower() if "://" in address else ""
    try:
        each = max(0.0, float((card.connection or {}).get("timeout_ms") or 0) / 1000.0)
    except (TypeError, ValueError):
        each = 0.0
    limit = max(OPEN_S, OWN.get(scheme, 0.0), 2 * each + 15.0 if each else 0.0)
    steps = (card.library or {}).get("open") or []
    if scheme == "lib" and steps:
        limit += len(steps) * (each or 10.0)
    return limit


def open_hung_text(name: str, limit: float, address: str) -> str:
    """An instrument whose driver didn't finish opening it in time, in words: what may hold it, and what helps."""
    from .drivers import is_driver_address
    try:
        from .others import holders
        running = holders()
    except Exception:
        running = []
    return ("%s: its driver didn't finish opening it within %d s, so Kelvin went on without it: is the instrument on, "
            "and not open in %s%s? If opening hangs again, turn the instrument itself off and on, then connect it "
            "again" % (name, round(limit), "its maker's software or another program" if is_driver_address(address)
                       else "another program", " (running now: %s)" % ", ".join(running) if running else ""))


def first_line(e: BaseException) -> str:
    return str(e).splitlines()[0][:300] if str(e).strip() else type(e).__name__


def same_card(a: Card, b: Card) -> bool:
    """The same card, as written and bound to the same variables (a card file edited since gives False)."""
    return a.id == b.id and a.data == b.data and a.bound == b.bound and a.status == b.status


def connection_of(card: Card) -> Tuple[Any, ...]:
    """What a connection is made with: a change here needs the instrument connected again."""
    return (card.connection, card.on_connect, card.library, card.jsonrpc, card.requires)


CONNECTING = "Kelvin is connecting it as it starts"
BUS_WINDOW_S = 120.0   # instruments on one GPIB bus that stop answering this close together: the bus hung   # an instrument of a lab opened before connecting (later=True)


class Lab:
    def __init__(self, config: LabConfig, registry: CardRegistry, backend_factory: BackendFactory,
                 clock: Any, world: Any = None, switched_off: Optional[SwitchedOff] = None, partial: bool = False,
                 later: bool = False, deadline: bool = True):
        """switched_off: the instruments a person switched off (Lab panel). They are never connected, so nothing,
        not even the connecting, is sent to them. partial: an instrument that can't be connected is set aside, with
        the reason, and the others are used (otherwise its error is raised). later: none is connected yet, so Kelvin
        opens at once (connecting can take minutes: timeouts, makers' drivers); each waits, as connecting, until
        reconnect connects it. deadline: each connection is opened on a thread of its own and waited for at most
        open_deadline (_open); False for a twin, whose simulated instruments open at once."""
        self.config = config
        self.registry = registry
        self.clock = clock
        self.stops = Stops()   # a person's stop, which every long wait (and instrument wait) watches
        self.world = world
        # how long readings and changes take in this lab, as its runs measured them (timing.Timing; the runtime sets
        # it). A twin has none of its own: it is paced at the real lab's (pace), so dry runs and cards agree
        self.timing: Any = None
        self.pace: Any = None
        self._factory = backend_factory
        self._deadline = deadline
        # an instrument's connection changes under this only, and never across I/O: a person may switch off one that
        # is still connecting without waiting for the lab (an open can take a minute), and the connection made
        # meanwhile is then not the lab's (_take)
        self._swap = threading.Lock()
        self._parked: Dict[str, Any] = {}   # connections set aside unclosed when switched off (closing would send)
        self._set_aside: List[Any] = []     # those of instruments taken out of the lab, likewise (take_out)
        off = set(switched_off(config.name)) if switched_off else set()
        self.instruments: Dict[str, Instrument] = {}
        for name, ic in config.instruments.items():
            card = registry.get(ic.card).bind(ic.vars)   # with this installation's sensor or channel names
            if name in off:
                backend = OffBackend(name)
            elif later:
                backend = Unavailable(name, CONNECTING)
            else:
                try:
                    backend = self._open(name, card, ic.address)
                except Exception as e:
                    if not partial:
                        raise
                    backend = Unavailable(name, first_line(e))
            self.instruments[name] = Instrument(name, card, backend, ic, self.stops)

    def _open(self, name: str, card: Card, address: str) -> Any:
        """A connection made by the lab's backend factory on a thread of its own, waited for at most open_deadline:
        a driver that never returns from opening (a camera's SDK, a GPIB driver) can't hold the lab, a first start or
        a restart for good. Past it OpenTimedOut is raised (the caller sets the instrument aside, with why), and the
        connection, should it open after all, is closed again: it is never handed to the lab, so nothing Kelvin
        doesn't know of talks to the instrument. A simulated one (sim://, a twin's) is made at once."""
        if not self._deadline or address.startswith("sim://"):
            return self._factory(name, card, address)
        limit = open_deadline(card, address)
        context = contextvars.copy_context()   # connecting_entry(name): the entry being connected, there too
        lock = threading.Lock()
        done = threading.Event()
        out: Dict[str, Any] = {}

        def work() -> None:
            try:
                backend = context.run(self._factory, name, card, address)
            except BaseException as e:   # the caller's to raise; nothing to close
                with lock:
                    out["error"] = e
                    done.set()
                return
            with lock:
                if not out.get("late"):
                    out["backend"] = backend
                    done.set()
                    return
            log.warning("%s: its driver finished opening %s after Kelvin had stopped waiting; closed again, unused",
                        name, address)
            _close_backend(backend)

        threading.Thread(target=work, name="kelvin-open-" + name, daemon=True).start()
        done.wait(limit)
        with lock:
            if not done.is_set():
                out["late"] = True
        if out.get("late"):
            log.warning("%s: its driver didn't finish opening %s within %g s; set aside", name, address, limit)
            raise OpenTimedOut(open_hung_text(name, limit, address))
        if "error" in out:
            raise out["error"]
        return out["backend"]

    def _take(self, inst: Instrument, was: Any, backend: Any) -> bool:
        """The instrument's connection is now `backend`, if it is still `was` (nobody switched it off meanwhile, as a
        person may while it connects: set_off): True. False leaves it as it is; a new connection is then not the
        lab's (_not_ours)."""
        with self._swap:
            if inst.backend is not was:
                return False
            inst.backend = backend
            return True

    def _not_ours(self, name: str, backend: Any) -> None:
        """A connection made for an instrument a person switched off while it connected: closed where closing sends
        nothing (a VISA session), else set aside unclosed, as switching it off would (switching on takes it back)."""
        if getattr(backend, "quiet_close", False) or _ended(backend):
            _let_go(backend)
        else:
            self._parked[name] = backend

    def set_off(self, name: str) -> bool:
        """Switch off an instrument that isn't connected (still connecting, set aside with why, or off already),
        waiting for nothing: no connection of it is in use, so nothing is sent or closed. False when it is connected:
        switching it off then needs the lab (switch). Raises KeyError for one not in this lab."""
        inst = self.instruments[name]
        with self._swap:
            if inst.enabled:
                return False
            if inst.unavailable is not None:
                inst.backend = OffBackend(name)
            return True

    def instrument(self, name: str) -> Instrument:
        """The instrument to talk to; a switched-off one is refused (KeyError, so the gate refuses the command)."""
        try:
            inst = self.instruments[name]
        except KeyError:
            raise NotHere("no instrument %r (have: %s)" % (name, ", ".join(self.instruments)))
        if not inst.enabled:
            raise NotHere(NOT_CONNECTED % (name, inst.unavailable) if inst.unavailable else OFF_TEXT % name)
        return inst

    @property
    def switched_off(self) -> List[str]:
        """The instruments a person switched off (not those that could not be connected)."""
        return [name for name, inst in self.instruments.items() if not inst.enabled and inst.unavailable is None]

    @property
    def unavailable(self) -> Dict[str, str]:
        """The instruments that could not be connected, with why."""
        return {name: inst.unavailable for name, inst in self.instruments.items() if inst.unavailable is not None}

    @property
    def connecting(self) -> List[str]:
        """The instruments still to be connected (a lab opened with later=True)."""
        return [name for name, why in self.unavailable.items() if why == CONNECTING]

    def away(self, name: str) -> Optional[str]:
        """Why an instrument of this lab is not in use (switched off, or not connected), or None."""
        inst = self.instruments.get(name)
        if inst is None or inst.enabled:
            return None
        return "not connected (%s)" % inst.unavailable if inst.unavailable else "disconnected from Kelvin in the Lab panel"

    def reconnect(self, name: str, card: Optional[Card] = None) -> str:
        """Connect an instrument that could not be connected; it stays set aside, with the new reason, if it still
        can't (the error is raised). card: its card as it is now, when it may have changed since the instrument
        connected (a card fixed meanwhile): the instrument takes it, and a connection whose settings it changed
        (line ends, an ack, serial framing) is made again, as closing a VISA session sends the instrument nothing.
        A maker's driver, whose closing could send something, keeps its connection until Kelvin next opens the lab.
        Returns what changed, in words ('' for nothing)."""
        inst = self.instruments[name]
        news = ""
        if card is not None:
            bound = card.bind(inst.config.vars)
            if not same_card(bound, inst.card):
                news = "its card %s as it is now" % card.id
                old, inst.card = inst.card, bound
                if hasattr(inst.backend, "card"):
                    inst.backend.card = bound   # a driver translates the card's commands
                if inst.enabled and connection_of(old) != connection_of(bound):
                    if not getattr(inst.backend, "quiet_close", False):
                        return news + "; its new connection settings apply when Kelvin next opens the lab"
                    backend, inst.backend = inst.backend, Unavailable(name, "connecting again with its card as it is now")
                    _close_now(backend)
                    news += ", connected again with its new connection settings"
        ended = _ended(inst.backend)
        if inst.unavailable is None and not ended:
            return news
        if ended:   # a connection that ended by itself (a camera's PICam helper Kelvin had to end): made anew
            backend, inst.backend = inst.backend, Unavailable(name, ended)
            _let_go(backend)   # on a thread of its own: a driver that didn't return may not return from closing
        was = inst.backend
        try:
            backend = self._open(name, inst.card, inst.config.address)
        except Exception as e:
            self._take(inst, was, Unavailable(name, first_line(e)))
            raise
        if not self._take(inst, was, backend):   # a person switched it off while it connected: it stays off
            self._not_ours(name, backend)
            raise InstrumentOff(OFF_TEXT % name)
        return news + ("; connected again (its connection had ended: %s)" % ended if ended else "")

    def switch(self, name: str, on: bool) -> None:
        """Switch an instrument on or off for this lab. Off sends nothing: a plain VISA session is closed (that
        sends the instrument nothing and frees it for other programs); a driver connection, whose closing could
        send something (a pulse generator stops its outputs), is set aside as it is. On connects it again, or
        takes back the connection set aside. While it connects it counts as not connected (CONNECTING_AGAIN), so a
        person can switch it off again at once (set_off)."""
        inst = self.instruments[name]   # KeyError: not in this lab
        if inst.unavailable is not None:   # not connected: on tries again, off keeps it off
            if on:
                self.reconnect(name)
            else:
                self.set_off(name)
            return
        if on == inst.enabled:
            return
        if not on:
            backend, inst.backend = inst.backend, OffBackend(name)
            if _ended(backend):   # its driver didn't return: closed on a thread of its own, made anew when on
                _let_go(backend)
            elif getattr(backend, "quiet_close", False):
                backend.close()
            else:
                self._parked[name] = backend
            return
        backend = self._parked.pop(name, None)
        if backend is not None and _ended(backend):   # ended while set aside: a fresh connection instead
            _let_go(backend)
            backend = None
        if backend is None:
            was = Unavailable(name, CONNECTING_AGAIN)
            with self._swap:
                inst.backend = was
            try:
                backend = self._open(name, inst.card, inst.config.address)
            except Exception as e:   # a person wanted it on: it is on, not connected, and says why (not "switched off")
                self._take(inst, was, Unavailable(name, first_line(e)))
                raise
            if not self._take(inst, was, backend):   # switched off again while it connected
                self._not_ours(name, backend)
                raise InstrumentOff(OFF_TEXT % name)
            return
        inst.backend = backend

    def _connect(self, ic: InstrumentConfig, card: Card) -> Any:
        """A connection to an instrument as this entry says, made before the lab lists it (connecting_entry)."""
        token = _CONNECTING.set(ic)
        try:
            return self._open(ic.name, card, ic.address)
        finally:
            _CONNECTING.reset(token)

    def _publish(self, ic: InstrumentConfig, inst: Instrument) -> None:
        """An instrument and its entry, listed as new dictionaries (never changed in place), the instrument first: a
        reader going through either never meets one half there (the Lab panel's /state failed for minutes while a
        GPIB connection was being opened for an instrument the config listed already), nor a dictionary changing
        size under it (setup failed so while the agent added an instrument)."""
        self.instruments = dict(self.instruments, **{ic.name: inst})
        self.config.instruments = dict(self.config.instruments, **{ic.name: ic})

    def add(self, ic: InstrumentConfig, card: Card) -> Instrument:
        """Connect one more instrument, as the lab config now lists it (Kelvin added it). The others stay connected
        as they are: connecting them again would open a camera or a maker's program a second time. It joins the lab
        once connected, never before. Raises KeyError for a name in use, and AdapterError (or OSError) when it cannot
        connect."""
        if ic.name in self.instruments:
            raise KeyError("%s is already in %s" % (ic.name, self.config.name))
        self.registry.put(card)
        bound = card.bind(ic.vars)
        backend = self._connect(ic, bound)
        inst = Instrument(ic.name, bound, backend, ic, self.stops)
        self._publish(ic, inst)
        return inst

    def replace(self, ic: InstrumentConfig, card: Card) -> Callable[[], None]:
        """An instrument of this lab connected as its lab config entry now says: another card (this lab's version of
        Kelvin's), another address, other connection settings or card variables. Its connection is closed first (a
        serial port opens once), then made anew; one that can't be made is raised, and the instrument is as it was
        (connected again with its old card and address, or set aside with why). Returns what puts it back so (its
        new connection answered as another instrument, or its entry could not be kept). One a person switched off
        is refused (ValueError): nothing may be sent to it, its closing neither."""
        inst = self.instruments[ic.name]   # KeyError: not in this lab
        if not inst.enabled and inst.unavailable is None:
            raise ValueError(OFF_TEXT % ic.name)
        bound = card.bind(ic.vars)
        was = inst.unavailable
        if was is None:
            old, inst.backend = inst.backend, Unavailable(ic.name, "connecting again with its new entry")
            _close_now(old)
        connecting = inst.backend

        def back() -> None:   # its old card and address, connected again where it was connected
            if was is not None:
                self._take(inst, connecting, Unavailable(ic.name, was))
                return
            try:
                backend = self._open(ic.name, inst.card, inst.config.address)
            except Exception as e:
                self._take(inst, connecting, Unavailable(ic.name, first_line(e)))
                return
            if not self._take(inst, connecting, backend):   # switched off meanwhile
                self._not_ours(ic.name, backend)
        try:
            backend = self._connect(ic, bound)   # with its new connection settings, not its old entry's
        except Exception:
            back()
            raise
        if inst.backend is not connecting:   # a person switched it off while it connected: it stays off
            self._not_ours(ic.name, backend)
            raise InstrumentOff(OFF_TEXT % ic.name)
        self.registry.put(card)
        new = Instrument(ic.name, bound, backend, ic, self.stops)
        self._publish(ic, new)

        def undo() -> None:
            new.close()
            self._publish(inst.config, inst)
            back()
        return undo

    def remove(self, name: str) -> None:
        """Take out an instrument just added (its entry could not be kept in the lab config): closed, then forgotten
        (its entry first, then the instrument, each a new dictionary: see _publish)."""
        inst = self.instruments[name]
        self.config.instruments = {k: v for k, v in self.config.instruments.items() if k != name}
        self.instruments = {k: v for k, v in self.instruments.items() if k != name}
        self._parked.pop(name, None)
        inst.close()

    def take_out(self, name: str) -> str:
        """Take an instrument out of this lab for good (its entry left the lab config: remove_instrument): forgotten,
        its entry first, then the instrument (each a new dictionary: see _publish). Its connection is closed where
        that sends the instrument nothing (a VISA session) or Kelvin closes it when it quits anyway (a camera); a
        maker's driver, whose closing could send something (a pulse generator stops its outputs), is set aside as it
        is, as a switched-off instrument's is, and left so when Kelvin quits. Returns "closed", "set aside", or "" (it
        had no connection of its own: switched off, never connected, simulated)."""
        inst = self.instruments[name]   # KeyError: not in this lab
        self.config.instruments = {k: v for k, v in self.config.instruments.items() if k != name}
        self.instruments = {k: v for k, v in self.instruments.items() if k != name}
        simulated = inst.simulated
        backend = self._parked.pop(name, None) or inst.backend
        inst.backend = OffBackend(name)   # whatever still holds the instrument sends it nothing from now on
        if isinstance(backend, OffBackend):
            return ""
        if simulated or getattr(backend, "quiet_close", False) or getattr(backend, "close_at_quit", False) \
                or _ended(backend):
            try:
                _close_now(backend)
            except Exception:
                pass
            return "" if simulated else "closed"
        self._set_aside.append(backend)
        return "set aside"

    def resolve(self, key: str) -> Tuple[Instrument, ParamSpec]:
        key = self.config.names.get(key, key)   # the lab's own name for it (names:): Vbg for dac.CH0
        instr, dot, param = key.partition(".")
        if not dot:
            raise KeyError("expected instrument.parameter, got %r" % key)
        if instr not in self.instruments:
            self.instrument(instr)   # says which instruments there are
        inst = self.instruments[instr]   # what it is, even switched off: talking to it is refused, not knowing it
        return inst, inst.spec(param)

    def read(self, instr: str, param: str) -> Any:
        return self.instrument(instr).read(param)

    def snapshot(self, names: Optional[Iterable[str]] = None, settings: bool = True) -> Dict[str, Dict[str, Any]]:
        """Every readable scalar parameter of every instrument (or of these). settings=False: its readings and outputs
        only, not its settings (a lock-in's time constant, a source's range), which change only when someone changes
        them (the Lab panel reads those less often)."""
        snap: Dict[str, Dict[str, Any]] = {}
        for name, inst in self.instruments.items():
            if not inst.enabled or (names is not None and name not in names):
                continue
            values: Dict[str, Any] = {}
            silent = False
            replies: Dict[str, str] = {}   # this look's own replies: two readings of one :READ? take it once
            for spec in inst.card.params.values():
                if spec.type in ("trace", "image") or spec.acquires or (spec.get_cmd is None and
                                                                        spec.name not in inst._last):
                    continue   # traces, frames and acquisitions are read when asked or measured, not to look
                if not settings and spec.set_cmd is not None and not spec.is_drive:
                    continue   # a setting: read at its own, slower pace
                if silent:   # it did not answer: each other reading would only wait as long again
                    values[spec.name] = "ERROR: not read: %s did not answer" % name
                    continue
                try:
                    values[spec.name] = inst.read(spec.name, replies)
                except Exception as e:  # a snapshot must never stop a run
                    values[spec.name] = "ERROR: %s" % e
                    silent = timed_out(e) or isinstance(e, InstrumentLost)
            snap[name] = values
        return snap

    def health_check(self, names: Optional[List[str]] = None) -> List[HealthResult]:
        """Before an instrument is used: is it the model its card says (a swapped address would send commands to
        the wrong instrument), does its error queue look clean, does every reading work, are the card's sanity
        checks fine, and are its drives inside the lab's limits? Only reads; never sets anything."""
        out = []
        for name in names or [n for n, inst in self.instruments.items() if inst.enabled]:
            inst = self.instruments.get(name)
            if inst is None:
                out.append(HealthResult(name, False, ["not in this lab"]))
                continue
            if not inst.enabled:
                out.append(HealthResult(name, False, [self.away(name) or "disconnected from Kelvin in the Lab panel"]))
                continue
            problems: List[str] = []
            notes: List[str] = []
            not_now: Dict[str, str] = {}
            try:
                card = inst.card
                if card.idn_pattern:
                    idn = inst.query_raw(card.idn_query).strip()
                    if not re.search(card.idn_pattern, idn, re.I) and Instrument._noise(idn):
                        problems.append(str(inst._garbled(card.idn_query, idn)))   # noise, not another instrument
                        out.append(HealthResult(name, False, problems))
                        continue
                    if not re.search(card.idn_pattern, idn, re.I):
                        from .homelab import builtin_ids
                        problems.append("identity %r does not match card %s (wrong address or instrument? If it is "
                                        "a variant the card doesn't know, %s)" % (idn[:80], card.id, (
                                            "make this lab's version of the card with its identity: Lab, Cards…, "
                                            "Make your version") if card.id in builtin_ids() else
                                        "add its identity to the card: Lab, Cards…, Edit"))
                        out.append(HealthResult(name, False, problems))
                        continue  # nothing else is meaningful on the wrong instrument
                    variant = card.variant(idn)
                    if variant is not None:   # another identity the card fits, each written out: said, and confirmed
                        notes.append("answers as %s" % variant["is"])
                        if variant["query"]:
                            said = inst.query_raw(variant["query"]).strip()
                            if not re.search(variant["expect"], said, re.I):
                                problems.append("its identity %r looks like %s, but %s answered %r" % (
                                    idn[:80], variant["is"], variant["query"], said[:60]))
                err = inst.instrument_error()
                if err:
                    problems.append("instrument reports %s" % err)
                dead = []
                for spec in card.params.values():  # every reading once (traces are slow, frames are files: skipped)
                    if spec.kind != "measure" or spec.get_cmd is None or spec.type in ("trace", "image") \
                            or spec.acquires:   # an acquisition runs only when asked or measured, as for a snapshot
                        continue
                    try:
                        inst.read(spec.name)
                    except NotNow as e:   # that reading only, not the instrument: said, and kept for plans
                        not_now[spec.name] = str(e)
                    except Exception as e:
                        dead.append("%s (%s)" % (spec.name, str(e).splitlines()[0][:60] if str(e) else type(e).__name__))
                if dead:
                    problems.append("cannot read " + ", ".join(dead))
                for chk in card.health:
                    if chk.query:
                        resp = inst.query_raw(chk.query).strip()
                        if chk.expect and not re.search(chk.expect, resp, re.I):
                            said = ("%s (%s answered %r)" % (chk.says, chk.query, resp[:60]) if chk.says
                                    else "%s returned %r" % (chk.query, resp))
                            if chk.before:   # it keeps those readings from being asked, nothing more
                                for b in chk.before:
                                    not_now.setdefault(b, "%s doesn't read %s now: %s" % (name, b, said))
                            else:
                                problems.append(said)
                    else:
                        v = inst.read(chk.read)
                        lo, hi = chk.range
                        if v is not None and not lo <= v <= hi:
                            problems.append("%s = %s, expected %s to %s" % (chk.read, inst.spec(chk.read).show(v), num(lo), num(hi)))
                for spec in card.drives:
                    lim = inst.limits(spec.name)
                    if spec.numeric and lim and spec.get_cmd is not None:
                        v = inst.read(spec.name)   # beyond them but at safe is not: an SR860's sine out off reads 0 V
                        if v is not None and not lim[0] <= v <= lim[1] and spec.distance(v) > 0:
                            problems.append("%s = %s is outside %s limits" % (spec.name, spec.show(v), inst.limits_where(spec.name)))
            except (InstrumentLost, Garbled, InstrumentFault) as e:   # in words already: which, where, what to do
                problems.append(str(e))
            except Exception as e:
                problems.append("no response (%s)" % e)
            if not_now:
                notes.append("not read now: %s" % "; ".join(sorted(set(not_now.values()))))
            out.append(HealthResult(name, not problems, problems, notes, not_now))
        return out

    def _state_of(self, inst: Instrument) -> Dict[str, Any]:
        state = getattr(inst.backend, "state", None)
        if isinstance(state, dict):
            return dict(state)
        # this instrument's readings only: a twin read the whole lab once per instrument (530 reads, not 53)
        return {k: v for k, v in self.snapshot([inst.name])[inst.name].items()
                if v is not None and not (isinstance(v, str) and v.startswith("ERROR"))}

    def buses(self) -> Dict[str, List[Instrument]]:
        """The lab's real instruments on each GPIB bus (a board, a Prologix adapter), by bus."""
        from .adapters import bus_of
        out: Dict[str, List[Instrument]] = {}
        for inst in list(self.instruments.values()):
            bus = None if inst.simulated else bus_of(inst.config.address)
            if bus:
                out.setdefault(bus, []).append(inst)
        return out

    def hung_buses(self, now: Optional[float] = None) -> List[Tuple[str, List[str]]]:
        """GPIB buses that look hung now: two or more of their instruments stopped answering within BUS_WINDOW_S
        (or couldn't be opened for a timeout), and none answered since the first of them did. A dead instrument
        alone is its own alarm; a dead bus takes all of them. (bus, the instruments that don't answer)."""
        now = time.time() if now is None else now
        out = []
        for bus, insts in self.buses().items():
            # timeouts only: "nothing listens" (an instrument switched off at its mains) means the bus itself worked
            failing = [i for i in insts if i.enabled and i.health.fails and not i.health.gone and i.health.lost_at
                       and now - i.health.lost_at < BUS_WINDOW_S]
            opened = [i for i in insts if i.unavailable and "VI_ERROR_TMO" in i.unavailable
                      and "cannot open" in i.unavailable]
            if len(failing) + len(opened) < 2:
                continue
            first = min([i.health.first_failed_at or i.health.lost_at for i in failing] or [now])
            if any(i.health.answered_at and i.health.answered_at > first for i in insts if i.enabled):
                continue   # one answered since: the bus works
            out.append((bus, [i.name for i in failing + opened]))
        return out

    def first_silent(self, names: Iterable[str]) -> Optional[str]:
        """Of these instruments, the one that stopped answering first (on a hung bus, most likely what hung it)."""
        silent = [(i.health.first_failed_at, i.name) for i in (self.instruments.get(n) for n in names)
                  if i is not None and i.health.fails and i.health.first_failed_at]
        return min(silent)[1] if silent else None

    def make_twin(self, seed: int = 0) -> "Lab":
        """A simulated copy of this lab in its current state, for dry runs."""
        from .sim.twin import World, sim_backend_factory
        clock = SimClock()
        world = World(clock, seed)
        off = set(self.switched_off) | set(self.unavailable)
        twin = Lab(self.config, self.registry, sim_backend_factory(world), clock, world, lambda _: off, deadline=False)
        twin.pace = self.timing or self.pace   # readings and steps take what they take in this lab (timing.py)
        for name, inst in self.instruments.items():
            if name in off:
                continue
            twin.instruments[name].backend.load_state(self._state_of(inst))
            twin.instruments[name]._last = dict(inst._last)
        return twin

    def close(self, within: Optional[float] = None, quitting: bool = False) -> List[str]:
        """Every connection closed (a camera's, a maker's program's: they hold the instrument for other programs until
        then). Connections set aside when their instrument was switched off stay as they are: closing could send.
        within: seconds for all of them together, each closed on a thread of its own, so one that hangs (a camera
        library that never returns, a GPIB bus that doesn't answer) never keeps Kelvin from quitting. Returns the
        instruments still closing when the time was up."""
        if within is None:
            for inst in self.instruments.values():
                inst.close()
            return []
        threads = []
        for name, inst in self.instruments.items():
            if quitting and not (getattr(inst.backend, "quiet_close", False)
                                 or getattr(inst.backend, "close_at_quit", False)):
                continue   # a driver whose closing sends something (a pulse generator stops its outputs): as it is
            t = threading.Thread(target=_close_quietly, args=(inst,), name="kelvin-close-" + name, daemon=True)
            t.start()
            threads.append((name, t))
        end = time.monotonic() + within
        for _, t in threads:
            t.join(max(0.0, end - time.monotonic()))
        return [name for name, t in threads if t.is_alive()]
