"""Twin backends: fake instruments driven by the card's own command set.

A twin answers the same SCPI strings a real instrument would, so dry runs
exercise the exact command path used on hardware. A card can name a twin
class with physics; otherwise the generic Twin just stores values.
"""
from __future__ import annotations

import importlib
import math
import random
import re
import threading
from typing import Any, Callable, Dict, List, Optional

from ..cards import Card
from ..scpi import split_compound


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

    def idn(self) -> str:
        return self.card.idn_example or "VIRTUAL,%s,0,0" % self.card.model

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
        pass

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
            return
        self.errors.append('-113,"Undefined header"')

    def _query_one(self, cmd: str) -> str:
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
        self.errors.append('-113,"Undefined header"')
        raise TimeoutError("%s: no response to %r" % (self.name, cmd))


class GenericTwin(Twin):
    """The twin of a card without one of its own: it keeps what is set, and a set point's reading (its readback)
    goes straight to the new value, so a dry run that waits for it finishes; there is no physics to wait for."""

    def apply(self, param: str, value: Any) -> None:
        super().apply(param, value)
        spec = self.card.params.get(param)
        if spec is not None and spec.readback and spec.readback in self.state:
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
