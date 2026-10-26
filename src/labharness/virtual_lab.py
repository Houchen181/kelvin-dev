"""The virtual lab: common instruments, all simulated, ready to try with no hardware."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .cards import Card, CardRegistry, load_card
from .clock import RealClock, ScaledClock, SimClock
from .lab import Lab, SwitchedOff
from .labconfig import Envelope, LabConfig, load_envelope, load_lab_config
from .sim.twin import World, sim_backend_factory

DATA = Path(__file__).parent / "data"
INSTALLED = ".kelvin-instruments.json"   # the cards `kic install` (the collection's own tool) copied, by SHA-256


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


def kelvin_ids(collection: Any = None) -> frozenset:
    """The ids Kelvin has cards for: its own, and the instrument collection's when it is on (collection.py)."""
    return builtin_ids() | frozenset(c.id for c in (collection.cards() if collection is not None else []))


def own_cards(cards_dir: Any) -> List[Tuple[Card, bool]]:
    """The user's cards in `cards_dir` (the first file for each id; broken ones left out), each with whether a person
    reviewed it on this computer: not a draft, and not a card `kic install` copied there and nobody changed since."""
    folder = Path(cards_dir) if cards_dir else None
    if folder is None or not folder.is_dir():
        return []
    try:
        copied = json.loads((folder / INSTALLED).read_text(encoding="utf-8")).get("installed") or {}
    except (OSError, ValueError, AttributeError):
        copied = {}
    out, seen = [], set()
    for path in sorted(folder.glob("*.yaml")):
        try:
            card = load_card(path)
        except Exception:
            continue
        if card.id in seen:
            continue
        seen.add(card.id)
        as_copied = copied.get(path.name) == hashlib.sha256(path.read_bytes()).hexdigest()
        out.append((card, card.status != "draft" and not as_copied))
    return out


def lab_registry(home: Any = None, collection: Any = None) -> CardRegistry:
    """The cards a lab can use, in layers: Kelvin's own; the instrument collection's when it is on (`collection`, see
    collection.py), which replace Kelvin's own with the same id; and the user's own from `home`/cards.

    A user card whose id Kelvin or the collection has a card for is used only if a person reviewed it on this
    computer, and then nothing replaces it; otherwise Kelvin's (or the collection's) is used. A broken user card is
    left out; the Cards dialog lists it with its problems."""
    registry = builtin_registry()
    mine = own_cards(Path(home) / "cards" if home else None)
    reviewed = {card.id for card, here in mine if here}
    for card in collection.cards() if collection is not None else []:
        if card.id not in reviewed:
            registry.put(card)
    for card, here in mine:
        if here or card.id not in registry:
            registry.put(card)
    return registry


def build_virtual_lab(clock: Any = None, seed: int = 0, config: Optional[Any] = None,
                      registry: Optional[CardRegistry] = None, switched_off: Optional[SwitchedOff] = None) -> Lab:
    clock = clock or SimClock()
    registry = registry or builtin_registry()
    cfg = load_lab_config(config or DATA / "virtual_lab.yaml", registry)
    world = World(clock, seed)
    return Lab(cfg, registry, sim_backend_factory(world), clock, world, switched_off)


def is_simulated(config: LabConfig) -> bool:
    return all(ic.address.startswith("sim://") for ic in config.instruments.values())


def build_lab(config: Any, clock: Any = None, seed: int = 0, registry: Optional[CardRegistry] = None,
              switched_off: Optional[SwitchedOff] = None) -> Lab:
    """A lab from a lab config whose instruments may be real (VISA/serial addresses) or simulated (sim://).
    switched_off: the instruments a person switched off in the Lab panel, never connected.

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
        return Lab(cfg, registry, factory, clock, world, switched_off)
    except Exception:
        for backend in opened:  # one instrument failed: let go of the ones already opened
            backend.close()
        raise


def demo_envelope(lab: Lab) -> Envelope:
    return load_envelope(DATA / "demo_project.yaml", lab.config, lab.registry)


def open_home_lab(home: Any, choice: str = "auto", speed: float = 10, switched_off: Optional[SwitchedOff] = None,
                  collection: Any = None) -> Tuple[Lab, Optional[Envelope], Dict[str, Any]]:
    """The lab an installed Kelvin controls: the user's own lab.yaml in `home` if there is one (unless they chose
    the virtual lab), otherwise the virtual lab at `speed`x real time. If the user's lab cannot be connected, Kelvin
    still starts, on the virtual lab, and says why. `collection`: the instrument collection, when it is on."""
    path = Path(home) / "lab.yaml"
    info: Dict[str, Any] = {"kind": "virtual", "path": str(path), "error": None, "has_file": path.exists()}
    registry = lab_registry(home, collection)
    if choice != "virtual" and path.exists():
        try:
            return build_lab(path, registry=registry, switched_off=switched_off), None, dict(info, kind="file")
        except Exception as e:
            info["error"] = "Your lab (%s) could not be connected: %s" % (path.name, str(e).splitlines()[0])
    lab = build_virtual_lab(clock=ScaledClock(speed) if speed else SimClock(), registry=registry,
                            switched_off=switched_off)
    return lab, demo_envelope(lab), info
