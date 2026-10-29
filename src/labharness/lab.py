"""A lab runtime: the instruments of one setup, its config and its clock."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .cards import Card, CardRegistry, ParamSpec, num
from .adapters import timed_out
from .clock import SimClock, Stops
from .instrument import NOT_CONNECTED, OFF_TEXT, Garbled, Instrument, InstrumentLost, OffBackend, Unavailable
from .labconfig import InstrumentConfig, LabConfig

BackendFactory = Callable[[str, Card, str], Any]
SwitchedOff = Callable[[str], Iterable[str]]   # a lab's name -> the instruments a person switched off in it


@dataclass
class HealthResult:
    instr: str
    ok: bool
    problems: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        return "%s ok" % self.instr if self.ok else "%s FAIL (%s)" % (self.instr, "; ".join(self.problems))


def real_backend_factory(name: str, card: Card, address: str) -> Any:
    """A real instrument over VISA (GPIB, USB, LAN, serial); see adapters.py."""
    from .adapters import open_backend
    return open_backend(name, card, address)



def first_line(e: BaseException) -> str:
    return str(e).splitlines()[0][:300] if str(e).strip() else type(e).__name__


def same_card(a: Card, b: Card) -> bool:
    """The same card, as written and bound to the same variables (a card file edited since gives False)."""
    return a.id == b.id and a.data == b.data and a.bound == b.bound and a.status == b.status


def connection_of(card: Card) -> Tuple[Any, ...]:
    """What a connection is made with: a change here needs the instrument connected again."""
    return (card.connection, card.on_connect, card.library, card.requires)


CONNECTING = "Kelvin is connecting it as it starts"   # an instrument of a lab opened before connecting (later=True)


class Lab:
    def __init__(self, config: LabConfig, registry: CardRegistry, backend_factory: BackendFactory,
                 clock: Any, world: Any = None, switched_off: Optional[SwitchedOff] = None, partial: bool = False,
                 later: bool = False):
        """switched_off: the instruments a person switched off (Lab panel). They are never connected, so nothing,
        not even the connecting, is sent to them. partial: an instrument that can't be connected is set aside, with
        the reason, and the others are used (otherwise its error is raised). later: none is connected yet, so Kelvin
        opens at once (connecting can take minutes: timeouts, makers' drivers); each waits, as connecting, until
        reconnect connects it."""
        self.config = config
        self.registry = registry
        self.clock = clock
        self.stops = Stops()   # a person's stop, which every long wait (and instrument wait) watches
        self.world = world
        self._factory = backend_factory
        self._parked: Dict[str, Any] = {}   # connections set aside unclosed when switched off (closing would send)
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
                    backend = backend_factory(name, card, ic.address)
                except Exception as e:
                    if not partial:
                        raise
                    backend = Unavailable(name, first_line(e))
            self.instruments[name] = Instrument(name, card, backend, ic, self.stops)

    def instrument(self, name: str) -> Instrument:
        """The instrument to talk to; a switched-off one is refused (KeyError, so the gate refuses the command)."""
        try:
            inst = self.instruments[name]
        except KeyError:
            raise KeyError("no instrument %r (have: %s)" % (name, ", ".join(self.instruments)))
        if not inst.enabled:
            raise KeyError(NOT_CONNECTED % (name, inst.unavailable) if inst.unavailable else OFF_TEXT % name)
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
        return "not connected (%s)" % inst.unavailable if inst.unavailable else "switched off in the Lab panel"

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
                    inst.backend.close()
                    inst.backend = Unavailable(name, "connecting again with its card as it is now")
                    news += ", connected again with its new connection settings"
        if inst.unavailable is None:
            return news
        try:
            inst.backend = self._factory(name, inst.card, inst.config.address)
        except Exception as e:
            inst.backend = Unavailable(name, first_line(e))
            raise
        return news

    def switch(self, name: str, on: bool) -> None:
        """Switch an instrument on or off for this lab. Off sends nothing: a plain VISA session is closed (that
        sends the instrument nothing and frees it for other programs); a driver connection, whose closing could
        send something (a pulse generator stops its outputs), is set aside as it is. On connects it again, or
        takes back the connection set aside."""
        inst = self.instruments[name]   # KeyError: not in this lab
        if inst.unavailable is not None:   # not connected: on tries again, off keeps it off
            if on:
                self.reconnect(name)
            else:
                inst.backend = OffBackend(name)
            return
        if on == inst.enabled:
            return
        if not on:
            backend, inst.backend = inst.backend, OffBackend(name)
            if getattr(backend, "quiet_close", False):
                backend.close()
            else:
                self._parked[name] = backend
            return
        backend = self._parked.pop(name, None)
        inst.backend = backend if backend is not None else self._factory(name, inst.card, inst.config.address)

    def add(self, ic: InstrumentConfig, card: Card) -> Instrument:
        """Connect one more instrument, as the lab config now lists it (Kelvin added it). The others stay connected
        as they are: connecting them again would open a camera or a maker's program a second time. Raises KeyError
        for a name in use, and AdapterError (or OSError) when it cannot connect."""
        if ic.name in self.instruments:
            raise KeyError("%s is already in %s" % (ic.name, self.config.name))
        self.registry.put(card)
        bound = card.bind(ic.vars)
        self.config.instruments[ic.name] = ic   # the backend factory reads its connection settings from here
        try:
            backend = self._factory(ic.name, bound, ic.address)
        except Exception:
            del self.config.instruments[ic.name]
            raise
        inst = self.instruments[ic.name] = Instrument(ic.name, bound, backend, ic, self.stops)
        return inst

    def remove(self, name: str) -> None:
        """Take out an instrument just added (its entry could not be kept in the lab config): closed, then forgotten."""
        inst = self.instruments.pop(name)
        self.config.instruments.pop(name, None)
        self._parked.pop(name, None)
        inst.close()

    def resolve(self, key: str) -> Tuple[Instrument, ParamSpec]:
        instr, dot, param = key.partition(".")
        if not dot:
            raise KeyError("expected instrument.parameter, got %r" % key)
        if instr not in self.instruments:
            self.instrument(instr)   # says which instruments there are
        inst = self.instruments[instr]   # what it is, even switched off: talking to it is refused, not knowing it
        return inst, inst.spec(param)

    def read(self, instr: str, param: str) -> Any:
        return self.instrument(instr).read(param)

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Every readable scalar parameter of every instrument."""
        snap: Dict[str, Dict[str, Any]] = {}
        for name, inst in self.instruments.items():
            if not inst.enabled:
                continue
            values: Dict[str, Any] = {}
            silent = False
            for spec in inst.card.params.values():
                if spec.type in ("trace", "image") or spec.acquires or (spec.get_cmd is None and
                                                                        spec.name not in inst._last):
                    continue   # traces, frames and acquisitions are read when asked or measured, not to look
                if silent:   # it did not answer: each other reading would only wait as long again
                    values[spec.name] = "ERROR: not read: %s did not answer" % name
                    continue
                try:
                    values[spec.name] = inst.read(spec.name)
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
                out.append(HealthResult(name, False, [self.away(name) or "switched off in the Lab panel"]))
                continue
            problems: List[str] = []
            try:
                card = inst.card
                if card.idn_pattern:
                    idn = inst.query_raw(card.idn_query).strip()
                    if not re.search(card.idn_pattern, idn, re.I) and Instrument._noise(idn):
                        problems.append(str(inst._garbled(card.idn_query, idn)))   # noise, not another instrument
                        out.append(HealthResult(name, False, problems))
                        continue
                    if not re.search(card.idn_pattern, idn, re.I):
                        from .virtual_lab import builtin_ids
                        problems.append("identity %r does not match card %s (wrong address or instrument? If it is "
                                        "a variant the card doesn't know, %s)" % (idn[:80], card.id, (
                                            "make this lab's version of the card with its identity: Lab, Cards…, "
                                            "Make your version") if card.id in builtin_ids() else
                                        "add its identity to the card: Lab, Cards…, Edit"))
                        out.append(HealthResult(name, False, problems))
                        continue  # nothing else is meaningful on the wrong instrument
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
                    except Exception as e:
                        dead.append("%s (%s)" % (spec.name, str(e).splitlines()[0][:60] if str(e) else type(e).__name__))
                if dead:
                    problems.append("cannot read " + ", ".join(dead))
                for chk in card.health:
                    if chk.query:
                        resp = inst.query_raw(chk.query).strip()
                        if chk.expect and not re.search(chk.expect, resp):
                            problems.append("%s returned %r" % (chk.query, resp))
                    else:
                        v = inst.read(chk.read)
                        lo, hi = chk.range
                        if v is not None and not lo <= v <= hi:
                            problems.append("%s = %s, expected %s to %s" % (chk.read, inst.spec(chk.read).show(v), num(lo), num(hi)))
                for spec in card.drives:
                    lim = inst.limits(spec.name)
                    if spec.numeric and lim and spec.get_cmd is not None:
                        v = inst.read(spec.name)
                        if v is not None and not lim[0] <= v <= lim[1]:
                            problems.append("%s = %s is outside %s limits" % (spec.name, spec.show(v), inst.limits_where(spec.name)))
            except (InstrumentLost, Garbled) as e:   # already in words: which instrument, which bus, what to check
                problems.append(str(e))
            except Exception as e:
                problems.append("no response (%s)" % e)
            out.append(HealthResult(name, not problems, problems))
        return out

    def _state_of(self, inst: Instrument) -> Dict[str, Any]:
        state = getattr(inst.backend, "state", None)
        if isinstance(state, dict):
            return dict(state)
        return {k: v for k, v in self.snapshot()[inst.name].items()
                if v is not None and not (isinstance(v, str) and v.startswith("ERROR"))}

    def make_twin(self, seed: int = 0) -> "Lab":
        """A simulated copy of this lab in its current state, for dry runs."""
        from .sim.twin import World, sim_backend_factory
        clock = SimClock()
        world = World(clock, seed)
        off = set(self.switched_off) | set(self.unavailable)
        twin = Lab(self.config, self.registry, sim_backend_factory(world), clock, world, lambda _: off)
        for name, inst in self.instruments.items():
            if name in off:
                continue
            twin.instruments[name].backend.load_state(self._state_of(inst))
            twin.instruments[name]._last = dict(inst._last)
        return twin

    def close(self) -> None:
        """Connections set aside when their instrument was switched off stay as they are: closing could send."""
        for inst in self.instruments.values():
            inst.close()
