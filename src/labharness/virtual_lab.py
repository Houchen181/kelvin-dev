"""The virtual lab: common instruments, all simulated, ready to try with no hardware."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .cards import CardRegistry, load_card
from .clock import RealClock, ScaledClock, SimClock
from .lab import Lab
from .labconfig import Envelope, LabConfig, load_envelope, load_lab_config
from .sim.twin import World, sim_backend_factory

DATA = Path(__file__).parent / "data"


def builtin_registry() -> CardRegistry:
    registry = CardRegistry()
    registry.load_dir(DATA / "cards")
    return registry


_BUILTIN_IDS: Optional[frozenset] = None


def builtin_ids() -> frozenset:
    global _BUILTIN_IDS
    if _BUILTIN_IDS is None:
        _BUILTIN_IDS = frozenset(c.id for c in builtin_registry())
    return _BUILTIN_IDS


def lab_registry(home: Any = None) -> CardRegistry:
    """Kelvin's cards plus the user's own from `home`/cards. A broken user card, or one that reuses a built-in
    id, is left out here; the Cards dialog lists it with its problems."""
    registry = builtin_registry()
    folder = Path(home) / "cards" if home else None
    if folder is not None and folder.is_dir():
        for path in sorted(folder.glob("*.yaml")):
            try:
                card = load_card(path)
            except Exception:
                continue
            if card.id not in registry:
                registry.add(card)
    return registry


def build_virtual_lab(clock: Any = None, seed: int = 0, config: Optional[Any] = None,
                      registry: Optional[CardRegistry] = None) -> Lab:
    clock = clock or SimClock()
    registry = registry or builtin_registry()
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

    opened = []

    def factory(name: str, card: Any, address: str) -> Any:
        if address.startswith("sim://"):
            return sim(name, card, address)
        from .adapters import open_backend
        backend = open_backend(name, card, address, overrides=cfg.instruments[name].connection)
        opened.append(backend)
        return backend

    try:
        return Lab(cfg, registry, factory, clock, world)
    except Exception:
        for backend in opened:  # one instrument failed: let go of the ones already opened
            backend.close()
        raise


def demo_envelope(lab: Lab) -> Envelope:
    return load_envelope(DATA / "demo_project.yaml", lab.config, lab.registry)


def open_home_lab(home: Any, choice: str = "auto", speed: float = 10) -> Tuple[Lab, Optional[Envelope], Dict[str, Any]]:
    """The lab an installed Kelvin controls: the user's own lab.yaml in `home` if there is one (unless they chose
    the virtual lab), otherwise the virtual lab at `speed`x real time. If the user's lab cannot be connected, Kelvin
    still starts, on the virtual lab, and says why."""
    path = Path(home) / "lab.yaml"
    info: Dict[str, Any] = {"kind": "virtual", "path": str(path), "error": None, "has_file": path.exists()}
    registry = lab_registry(home)
    if choice != "virtual" and path.exists():
        try:
            return build_lab(path, registry=registry), None, dict(info, kind="file")
        except Exception as e:
            info["error"] = "Your lab (%s) could not be connected: %s" % (path.name, str(e).splitlines()[0])
    lab = build_virtual_lab(clock=ScaledClock(speed) if speed else SimClock(), registry=registry)
    return lab, demo_envelope(lab), info
