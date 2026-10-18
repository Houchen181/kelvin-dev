"""The virtual lab: common instruments, all simulated, ready to try with no hardware."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .cards import CardRegistry
from .clock import SimClock
from .lab import Lab
from .labconfig import Envelope, load_envelope, load_lab_config
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


def demo_envelope(lab: Lab) -> Envelope:
    return load_envelope(DATA / "demo_project.yaml", lab.config, lab.registry)
