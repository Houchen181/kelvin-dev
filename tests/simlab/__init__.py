"""The tests' simulated lab: common instruments with toy physics, so procedures (limits, order, timing, guards, data
shape) can be exercised with no hardware. Test-only: Kelvin itself has no virtual lab (2026-10-29); its rehearsals use
generic twins of a lab's own instruments (labharness.sim.twin).

    build_virtual_lab()   the lab (virtual_lab.yaml): smu, lockin, tc, magnet, vna, laser, stage, pg, mw, counter
    demo_envelope(lab)    its project envelope (demo_project.yaml)
    lab_cards(registry)   a registry with the test lab's cards added (cards/, ids virtual/...)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from labharness.cards import CardRegistry, load_card
from labharness.clock import SimClock
from labharness.homelab import builtin_registry
from labharness.lab import Lab, SwitchedOff
from labharness.labconfig import Envelope, load_envelope, load_lab_config
from labharness.sim.twin import World, sim_backend_factory

DATA = Path(__file__).parent


def lab_cards(registry: Optional[CardRegistry] = None) -> CardRegistry:
    """`registry` (Kelvin's own cards if none) with the test lab's cards put in."""
    out = registry if registry is not None else builtin_registry()
    for path in sorted((DATA / "cards").glob("*.yaml")):
        out.put(load_card(path))
    return out


def build_virtual_lab(clock: Any = None, seed: int = 0, config: Optional[Any] = None,
                      registry: Optional[CardRegistry] = None, switched_off: Optional[SwitchedOff] = None) -> Lab:
    """The test lab, on a simulated clock unless given one. config: another lab config of simulated instruments;
    registry: the cards to use, with the test lab's put in."""
    clock = clock or SimClock()
    cards = lab_cards(registry)
    cfg = load_lab_config(config or DATA / "virtual_lab.yaml", cards)
    world = World(clock, seed)
    return Lab(cfg, cards, sim_backend_factory(world), clock, world, switched_off)


def demo_envelope(lab: Lab) -> Envelope:
    return load_envelope(DATA / "demo_project.yaml", lab.config, lab.registry)
