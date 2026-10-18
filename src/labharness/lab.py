"""A lab runtime: the instruments of one setup, its config and its clock."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .cards import Card, CardRegistry, ParamSpec, num
from .clock import SimClock
from .instrument import Instrument
from .labconfig import LabConfig

BackendFactory = Callable[[str, Card, str], Any]


@dataclass
class HealthResult:
    instr: str
    ok: bool
    problems: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        return "%s ok" % self.instr if self.ok else "%s FAIL (%s)" % (self.instr, "; ".join(self.problems))


def real_backend_factory(name: str, card: Card, address: str) -> Any:
    raise NotImplementedError(
        "%s: real instrument adapters (VISA, serial, vendor SDKs, LabVIEW, MATLAB) arrive in Phase 1; "
        "use a sim:// address for now" % name)


class Lab:
    def __init__(self, config: LabConfig, registry: CardRegistry, backend_factory: BackendFactory,
                 clock: Any, world: Any = None):
        self.config = config
        self.registry = registry
        self.clock = clock
        self.world = world
        self.instruments: Dict[str, Instrument] = {}
        for name, ic in config.instruments.items():
            card = registry.get(ic.card)
            self.instruments[name] = Instrument(name, card, backend_factory(name, card, ic.address), ic)

    def instrument(self, name: str) -> Instrument:
        try:
            return self.instruments[name]
        except KeyError:
            raise KeyError("no instrument %r (have: %s)" % (name, ", ".join(self.instruments)))

    def resolve(self, key: str) -> Tuple[Instrument, ParamSpec]:
        instr, dot, param = key.partition(".")
        if not dot:
            raise KeyError("expected instrument.parameter, got %r" % key)
        inst = self.instrument(instr)
        return inst, inst.spec(param)

    def read(self, instr: str, param: str) -> Any:
        return self.instrument(instr).read(param)

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Every readable scalar parameter of every instrument."""
        snap: Dict[str, Dict[str, Any]] = {}
        for name, inst in self.instruments.items():
            values: Dict[str, Any] = {}
            for spec in inst.card.params.values():
                if spec.type == "trace" or (spec.get_cmd is None and spec.name not in inst._last):
                    continue
                try:
                    values[spec.name] = inst.read(spec.name)
                except Exception as e:  # a snapshot must never stop a run
                    values[spec.name] = "ERROR: %s" % e
            snap[name] = values
        return snap

    def health_check(self, names: Optional[List[str]] = None) -> List[HealthResult]:
        """Identity, error queue, sanity reads, and drives inside lab limits."""
        out = []
        for name in names or list(self.instruments):
            inst = self.instruments.get(name)
            if inst is None:
                out.append(HealthResult(name, False, ["not in this lab"]))
                continue
            problems: List[str] = []
            try:
                card = inst.card
                if card.idn_pattern:
                    idn = inst.query_raw("*IDN?").strip()
                    if not re.search(card.idn_pattern, idn):
                        problems.append("identity %r does not match card %s" % (idn, card.id))
                for chk in card.health:
                    if chk.query:
                        resp = inst.query_raw(chk.query).strip()
                        if chk.expect and not re.search(chk.expect, resp):
                            problems.append("%s returned %r" % (chk.query, resp))
                    else:
                        v = inst.read(chk.read)
                        lo, hi = chk.range
                        if not lo <= v <= hi:
                            problems.append("%s = %s, expected %s to %s" % (chk.read, inst.spec(chk.read).show(v), num(lo), num(hi)))
                for spec in card.drives:
                    lim = inst.limits(spec.name)
                    if spec.numeric and lim and spec.get_cmd is not None:
                        v = inst.read(spec.name)
                        if not lim[0] <= v <= lim[1]:
                            problems.append("%s = %s is outside %s limits" % (spec.name, spec.show(v), inst.limits_source(spec.name)))
            except Exception as e:
                problems.append("no response (%s)" % e)
            out.append(HealthResult(name, not problems, problems))
        return out

    def _state_of(self, inst: Instrument) -> Dict[str, Any]:
        state = getattr(inst.backend, "state", None)
        if isinstance(state, dict):
            return dict(state)
        return {k: v for k, v in self.snapshot()[inst.name].items() if not (isinstance(v, str) and v.startswith("ERROR"))}

    def make_twin(self, seed: int = 0) -> "Lab":
        """A simulated copy of this lab in its current state, for dry runs."""
        from .sim.twin import World, sim_backend_factory
        clock = SimClock()
        world = World(clock, seed)
        twin = Lab(self.config, self.registry, sim_backend_factory(world), clock, world)
        for name, inst in self.instruments.items():
            twin.instruments[name].backend.load_state(self._state_of(inst))
            twin.instruments[name]._last = dict(inst._last)
        return twin

    def close(self) -> None:
        for inst in self.instruments.values():
            inst.close()
