"""The virtual lab: common instruments, all simulated, ready to try with no hardware."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .cards import CardRegistry
from .clock import RealClock, SimClock
from .lab import Lab
from .labconfig import Envelope, LabConfig, load_envelope, load_lab_config
from .sim.twin import World, sim_backend_factory

DATA = Path(__file__).parent / "data"


def builtin_registry() -> CardRegistry:
    registry = CardRegistry()
    registry.load_dir(DATA / "cards")
    return registry


def build_virtual_lab(clock: Any = None, seed: int = 0, config: Optional[Any] = None) -> Lab:
    clock = clock or SimClock()
    registry = builtin_registry()
    cfg = load_lab_config(config or DATA / "virtual_lab.yaml", registry)
    world = World(clock, seed)
    return Lab(cfg, registry, sim_backend_factory(world), clock, world)


def is_simulated(config: LabConfig) -> bool:
    return all(ic.address.startswith("sim://") for ic in config.instruments.values())


def build_lab(config: Any, clock: Any = None, seed: int = 0, registry: Optional[CardRegistry] = None) -> Lab:
    """A lab from a lab config whose instruments may be real (VISA/serial addresses) or simulated (sim://).

    With any real instrument the lab runs on real time: ramps and settling are timed by the clock, so a sped-up
    simulation clock would ramp real hardware too fast. Asking for one is refused."""
    registry = registry or builtin_registry()
    cfg = load_lab_config(config, registry)
    if not is_simulated(cfg):
        if clock is not None and getattr(clock, "simulated", False):
            raise ValueError("%s has real instruments, so it must run on real time, not a simulated clock" % cfg.name)
        clock = RealClock()
    clock = clock or SimClock()
    world = World(clock, seed)
    sim = sim_backend_factory(world)

    def factory(name: str, card: Any, address: str) -> Any:
        if address.startswith("sim://"):
            return sim(name, card, address)
        from .adapters import open_backend
        return open_backend(name, card, address, overrides=cfg.instruments[name].connection)

    return Lab(cfg, registry, factory, clock, world)


def demo_envelope(lab: Lab) -> Envelope:
    return load_envelope(DATA / "demo_project.yaml", lab.config, lab.registry)
