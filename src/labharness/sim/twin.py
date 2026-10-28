"""Twin backends: fake instruments driven by the card's own command set.

A twin answers the same SCPI strings a real instrument would, so dry runs
exercise the exact command path used on hardware. A card can name a twin
class with physics; otherwise the generic Twin stores values, carries out the
ramps the card leaves to the instrument, and answers the card's health checks
as a healthy instrument would.
"""
from __future__ import annotations

import importlib
import math
import random
import re
import threading
from typing import Any, Callable, Dict, List, Optional

from ..cards import Card, HealthCheck, ParamSpec, _is_number
from ..scpi import Template, split_compound

try:
    from re import _parser as sre_parse   # Python 3.11+
except ImportError:
    import sre_parse

_CLASS_CHAR = {"CATEGORY_DIGIT": "0", "CATEGORY_SPACE": " ", "CATEGORY_NOT_WORD": "-"}   # \d, \s, \W; others "a"


def _bare(command: str) -> str:
    """A command as an instrument compares it: no surrounding space, no leading colon, any case."""
    return command.strip().lstrip(":").lower()


def _written(command: str) -> Optional[str]:
    """The command a card's manual notation stands for (optional [nodes] left out), or None if it can't be read."""
    try:
        return Template(command).render()
    except ValueError:
        return None


def _reply_matching(pattern: str) -> Optional[str]:
    """A short text that `pattern` finds (as a health check matches a reply), or None if none can be made. Follows
    what cards write there: literals, classes, repeats, groups, alternatives and anchors."""
    def text(items: Any, fill: str, groups: Dict[Any, str]) -> str:
        out = []
        for op, av in items:
            name = op.name
            if name == "LITERAL":
                out.append(chr(av))
            elif name == "ANY":
                out.append(fill)
            elif name == "NOT_LITERAL":   # [^x]
                out.append(fill if ord(fill) != av else "b")
            elif name == "IN":   # a class: its first member, or the fill for [^...]
                kind, first = av[0][0].name, av[0][1]
                if kind == "LITERAL":
                    out.append(chr(first))
                elif kind == "RANGE":
                    out.append(chr(first[0]))
                elif kind == "CATEGORY":
                    out.append(_CLASS_CHAR.get(first.name, "a"))
                else:
                    out.append(fill)
            elif name == "BRANCH":
                out.append(text(av[1][0], fill, groups))
            elif name == "SUBPATTERN":
                groups[av[0]] = text(av[-1], fill, groups)
                out.append(groups[av[0]])
            elif name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
                out.append(text(av[2], fill, groups) * av[0])
            elif name == "ATOMIC_GROUP":
                out.append(text(av, fill, groups))
            elif name == "GROUPREF":
                out.append(groups.get(av, ""))
        return "".join(out)   # anchors and lookarounds add nothing

    try:
        parsed = sre_parse.parse(pattern)
        for fill in "a0 .":   # a stand-in for "any character" that the rest of the pattern accepts
            reply = text(parsed, fill, {}).strip()
            if re.search(pattern, reply):
                return reply
    except Exception:   # a pattern this can't follow gets no made-up reply
        pass
    return None


class World:
    """Shared clock, noise and a toy sample wired to all twins."""

    STEP = 0.5  # s, integration step for slow dynamics
    MAX_STEPS = 2000  # after a long idle gap, take coarser steps rather than stall (the dynamics are exponential/clamped)

    def __init__(self, clock: Any, seed: int = 0):
        self.clock = clock
        self.rng = random.Random(seed)
        self.twins: Dict[str, "Twin"] = {}
        self._t = clock.now()
        self.lock = threading.RLock()  # the monitor reads from its own thread

    def add(self, name: str, twin: "Twin") -> None:
        self.twins[name] = twin

    def of_kind(self, kind: str) -> Optional["Twin"]:
        for twin in self.twins.values():
            if twin.card.kind == kind:
                return twin
        return None

    def update(self) -> None:
        now = self.clock.now()
        dt = now - self._t
        if dt <= 0:
            return
        n = min(self.MAX_STEPS, int(math.ceil(dt / self.STEP)))
        for _ in range(n):
            for twin in self.twins.values():
                twin.evolve(dt / n)
        self._t = now

    def noise(self, sigma: float) -> float:
        return self.rng.gauss(0.0, sigma)

    # --- toy sample: a gated device in a cryostat with a magnet, a laser and an RF resonator ---

    def gate_voltage(self) -> float:
        smu = self.of_kind("source-meter")
        return smu.state["V"] if smu and smu.state.get("output") else 0.0

    def field(self) -> float:
        magnet = self.of_kind("magnet-supply")
        return magnet.state["B_actual"] if magnet else 0.0

    def temperature(self) -> float:
        tc = self.of_kind("temperature-controller")
        return tc.state["T_sample"] if tc else 1.6

    def laser_on_sample(self) -> float:
        laser = self.of_kind("laser")
        if laser and laser.state.get("emission") and laser.state.get("shutter"):
            return laser.state["power"]
        return 0.0

    def resistance(self) -> float:
        """Ohms: a charge-neutrality peak at 1.5 V, magnetoresistance, weak T dependence."""
        vg, b, t = self.gate_voltage(), self.field(), self.temperature()
        peak = 9e3 / (1 + ((vg - 1.5) / 0.8) ** 2)
        return (1e3 + peak * (1 + 0.3 * b * b)) / (1 + t / 50.0)

    def gate_leakage(self, v: float) -> float:
        """Amps: 1 TΩ leakage, dielectric breakdown above 12 V."""
        i = v / 1e12
        if abs(v) > 12.0:
            i += math.copysign(2e-7 * (abs(v) - 12.0), v)
        return i


class Twin:
    simulated = True

    def __init__(self, card: Card, world: World, name: str):
        self.card = card
        self.world = world
        self.name = name
        self.errors: List[str] = []
        self.fault: Optional[str] = None  # e.g. "offline": stop answering
        self.state: Dict[str, Any] = {}
        # setpoints the instrument ramps to by itself, each with the reading that follows it (see evolve)
        self.ramps = [spec for spec in card.params.values() if spec.ramp == "instrument" and spec.readback]
        self.reset()
        world.add(name, self)

    def reset(self) -> None:
        for spec in self.card.params.values():
            if spec.default is not None:
                v = spec.default
            elif spec.safe is not None:
                v = spec.safe
            elif spec.type == "bool":
                v = False
            elif spec.type == "enum":
                v = next(iter(spec.values or {"": ""}))
            elif spec.type == "trace":
                v = []
            elif spec.type in ("str", "image"):
                v = ""
            else:
                v = spec.limits[0] if spec.limits and spec.limits[0] > 0 else 0.0
            self.state[spec.name] = v
        for spec in self.ramps:   # at rest: a reading with no default of its own is where its setpoint is
            if self.card.params[spec.readback].default is None and _is_number(self.state[spec.name]):
                self.state[spec.readback] = self.state[spec.name]
        for check in self.card.health:   # set up the way the card's health checks expect (field units in tesla, ...)
            if check.query and check.expect:
                self._meet(check)

    def _meet(self, check: HealthCheck) -> None:
        """Give the setting a health query reads a value whose reply the check accepts, if the card has one."""
        try:
            expect = re.compile(check.expect)
        except re.error:
            return
        command = _written(check.query)
        hits = [s for s in self.card.getters if command is not None and s.get_cmd.match(command) is not None]
        if len(hits) != 1 or hits[0].field is not None or hasattr(self, "measure_" + hits[0].name):
            return
        spec = hits[0]
        choices = [self.state[spec.name]]
        if spec.type == "enum":
            choices += list(spec.values or {})
        elif spec.type == "bool":
            choices += [True, False]
        elif spec.type in ("str", "float", "int"):
            made = _reply_matching(check.expect)
            try:
                choices += [] if made is None else [spec.from_wire(made)]
            except ValueError:   # e.g. a number the check wants written in a way this card doesn't read
                pass
        for value in choices:
            if expect.search(spec.to_wire(value)):
                self.state[spec.name] = value
                return

    def idn(self) -> str:
        """The card's example reply, else one its identify pattern accepts, so the identity check passes."""
        pattern = self.card.idn_pattern
        return self.card.idn_example or (pattern and _reply_matching(pattern)) or "VIRTUAL,%s,0,0" % self.card.model

    # --- backend interface (same as a VISA resource) ---

    def write(self, command: str) -> None:
        with self.world.lock:
            self._write(command)

    def _write(self, command: str) -> None:
        self._check_fault()
        self.world.update()
        for spec in self.card.params.values():  # a card command that is itself compound, e.g. "TARG {value};RAMP"
            if spec.set_cmd is not None and ";" in spec.set_cmd.text and spec.set_cmd.match(command) is not None:
                self._write_one(command)
                return
        for part in split_compound(command):
            self._write_one(part)

    def query(self, command: str) -> str:
        with self.world.lock:
            return self._query(command)

    def _query(self, command: str) -> str:
        self._check_fault()
        self.world.update()
        parts = split_compound(command)
        for part in parts[:-1]:
            self._write_one(part)
        return self._query_one(parts[-1] if parts else "")

    def load_state(self, values: Dict[str, Any]) -> None:
        for k, v in values.items():
            if k in self.state:
                self.state[k] = v

    def close(self) -> None:
        pass

    # --- behaviour ---

    def apply(self, param: str, value: Any) -> None:
        self.state[param] = value

    def read(self, param: str) -> Any:
        fn = getattr(self, "measure_" + param, None)
        return fn() if fn else self.state[param]

    def evolve(self, dt: float) -> None:
        """Advance dt seconds (0: only what happens at once). A reading follows the setpoint the instrument ramps to
        by itself, at the card's ramp rate (or at once if it gives none), while that setpoint reaches the sample."""
        for spec in self.ramps:
            target, actual = self.state[spec.name], self.state[spec.readback]
            if not (_is_number(target) and _is_number(actual)) or target == actual or not self._applied(spec):
                continue
            rate = self._ramp_rate(spec)
            step = math.inf if rate is None else rate * dt
            self.state[spec.readback] = (target if abs(target - actual) <= step
                                         else actual + math.copysign(step, target - actual))

    def _ramp_rate(self, spec: ParamSpec) -> Optional[float]:
        """Units per second: the value of the card's rate parameter, else its max_rate; None (at once) for neither."""
        rate = self.state.get(spec.rate_param) if spec.rate_param else None
        if _is_number(rate) and rate > 0:
            return rate / (60.0 if spec.rate_per == "min" else 1.0)
        return spec.max_rate if spec.max_rate and spec.max_rate > 0 else None

    def _applied(self, spec: ParamSpec) -> bool:
        """Whether a setpoint reaches the sample: no switch, or its switch (applies_when) away from safe."""
        switch = self.card.params.get(spec.applies_when) if spec.applies_when else None
        return switch is None or switch.distance(self.state[switch.name]) > 0

    def take_time(self, seconds: float) -> None:
        """Model acquisition time (instant on a SimClock)."""
        self.world.clock.sleep(seconds)
        self.world.update()

    def _check_fault(self) -> None:
        if self.fault:
            raise TimeoutError("%s: %s" % (self.name, self.fault))

    def _write_one(self, cmd: str) -> None:
        if re.match(r"^\s*\*RST\s*$", cmd, re.I):
            self.reset()
            return
        if re.match(r"^\s*\*CLS\s*$", cmd, re.I):
            self.errors.clear()
            return
        for spec in self.card.setters:
            text = spec.set_cmd.match(cmd)
            if text is None:
                continue
            try:
                value = spec.from_wire(text)
            except ValueError:
                self.errors.append('-224,"Illegal parameter value"')
                return
            if spec.numeric and spec.limits and not spec.limits[0] <= value <= spec.limits[1]:
                self.errors.append('-222,"Data out of range"')
                return
            self.apply(spec.name, value)
            self.evolve(0.0)   # what follows at once, e.g. a reading whose ramp has no rate
            return
        self.errors.append('-113,"Undefined header"')

    def _query_one(self, cmd: str) -> str:
        reply = self._answer(cmd)
        if reply is None:
            reply = self._health_reply(cmd)
        if reply is None:
            self.errors.append('-113,"Undefined header"')
            raise TimeoutError("%s: no response to %r" % (self.name, cmd))
        return reply

    def _health_reply(self, cmd: str) -> Optional[str]:
        """A health query, sent as the card writes it, that the commands above don't answer: in manual notation
        (:SYSTem:ERRor[:NEXT]?) it is answered as the command it stands for (a queued error still shows); otherwise
        with a reply its expect accepts, as a healthy instrument's would be."""
        for check in self.card.health:
            if not check.query or _bare(check.query) != _bare(cmd):
                continue
            command = _written(check.query)
            reply = self._answer(command) if command is not None and _bare(command) != _bare(cmd) else None
            if reply is None and check.expect is not None:
                reply = _reply_matching(check.expect)
            return reply
        return None

    def _answer(self, cmd: str) -> Optional[str]:
        """The reply to one query, or None if the instrument has no such query."""
        if re.match(r"^\s*\*IDN\?\s*$", cmd, re.I):
            return self.idn()
        if re.match(r"^\s*\*OPC\?\s*$", cmd, re.I):
            return "1"
        if self.card.error_query is not None and self.card.error_query.match(cmd) is not None:
            return self.errors.pop(0) if self.errors else '0,"No error"'
        if cmd.strip().lower() == self.card.idn_query.strip().lower():
            return self.idn()
        hits = [s for s in self.card.getters if s.get_cmd.match(cmd) is not None]
        if any(s.field is not None for s in hits):  # one query, several values: answer like the instrument
            reply = ["0"] * (max(s.field or 0 for s in hits) + 1)
            for s in hits:
                reply[s.field or 0] = s.to_wire(self.read(s.name))
            return ",".join(reply)
        if hits:
            return hits[0].to_wire(self.read(hits[0].name))
        return None


class GenericTwin(Twin):
    """The twin of a card without one of its own: it keeps what is set, and a set point's reading (its readback)
    goes straight to the new value, so a dry run that waits for it finishes; there is no physics to wait for. A set
    point the instrument ramps to by itself follows at the card's rate instead (Twin.evolve)."""

    def apply(self, param: str, value: Any) -> None:
        super().apply(param, value)
        spec = self.card.params.get(param)
        if spec is not None and spec.readback and spec.readback in self.state and spec not in self.ramps:
            self.state[spec.readback] = value


def make_twin_backend(card: Card, world: World, name: str) -> Twin:
    cls: Any = GenericTwin
    if card.twin:
        module, _, attr = card.twin.partition(":")
        cls = getattr(importlib.import_module(module), attr)
    return cls(card, world, name)


def sim_backend_factory(world: World) -> Callable[[str, Card, str], Twin]:
    def factory(name: str, card: Card, address: str) -> Twin:
        return make_twin_backend(card, world, name)
    return factory
