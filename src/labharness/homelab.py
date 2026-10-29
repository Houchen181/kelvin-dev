"""The lab an installed Kelvin controls, and the cards it can use.

The person's own lab config (lab.yaml in Kelvin's folder) names their instruments, each with its card and address.
Until there is one, Kelvin has a lab with no instruments: Lab, Set up… finds them (Kelvin has no virtual lab of
simulated instruments; a plan's rehearsal runs on generic twins of the lab's own instruments, Lab.make_twin).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .cards import Card, CardRegistry, load_card
from .clock import RealClock, SimClock
from .lab import Lab, SwitchedOff
from .labconfig import ConfigError, Envelope, LabConfig, load_lab_config, read_yaml
from .sim.twin import World, sim_backend_factory

DATA = Path(__file__).parent / "data"
NO_LAB = "My lab"   # the name of the lab Kelvin has before a person sets theirs up
SET_ASIDE = ("set aside: its entry in lab.yaml has a problem (%s). Kelvin sends it nothing until the entry is fixed "
             "(Lab, Set up…, then Save and connect); the rest of the lab works")
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


def collection_ids(collection: Any = None) -> frozenset:
    """The ids the instrument collection has cards for, when it is on (collection.py)."""
    return frozenset(c.id for c in (collection.cards() if collection is not None else []))


def kelvin_ids(collection: Any = None) -> frozenset:
    """The ids Kelvin has cards for: its own, and the instrument collection's when it is on (collection.py)."""
    return builtin_ids() | collection_ids(collection)


def own_cards(cards_dir: Any) -> List[Tuple[Card, bool, bool]]:
    """The user's cards in `cards_dir` (the first file for each id; broken ones left out), each with whether a person
    reviewed it on this computer (not a draft, and not a card `kic install` copied there and nobody changed since),
    and whether it is such a copy."""
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
        out.append((card, card.status != "draft" and not as_copied, as_copied))
    return out


def lab_registry(home: Any = None, collection: Any = None) -> CardRegistry:
    """The cards a lab can use, in layers: Kelvin's own; the instrument collection's when it is on (`collection`, see
    collection.py), which replace Kelvin's own with the same id; and the user's own from `home`/cards.

    A user card a person reviewed on this computer wins over both. A draft of the lab's own under the id of one of
    Kelvin's cards is this lab's version of it (another firmware, a wider range; Kelvin may write one): it replaces
    Kelvin's card here, every output change asking until a person reviews it, but gives way to the collection's card,
    which is newer. A card `kic install` copied and nobody changed gives way to both. A broken user card is left out;
    the Cards dialog lists it with its problems. Kelvin's own draft cards a person reviewed for this lab count as
    reviewed here (carddraft.apply_reviews)."""
    registry = builtin_registry()
    folder = Path(home) / "cards" if home else None
    mine = own_cards(folder)
    reviewed = {card.id for card, here, _ in mine if here}
    shared = set()
    for card in collection.cards() if collection is not None else []:
        if card.id not in reviewed:
            registry.put(card)
            shared.add(card.id)
    for card, here, copied in mine:
        if here or card.id not in registry or not (copied or card.id in shared):
            registry.put(card)
    from .carddraft import apply_reviews
    apply_reviews(registry, folder)   # Kelvin's own draft cards a person reviewed for this lab
    return registry


def card_now(lab: Lab, name: str, home: Any = None, collection: Any = None) -> Optional[Card]:
    """The card an instrument of `lab` has on disk now, by the layers a lab opens with (lab_registry), when it differs
    from the one it connected with (a card fixed since); None when it is the same. The lab's registry takes it.
    ValueError when the card is gone, or the lab config would not be valid with it (a limit on a parameter it no
    longer has): then the instrument keeps the card it has."""
    from .labconfig import ConfigError
    from .lab import same_card
    inst = lab.instruments[name]
    fresh = lab_registry(home, collection)
    if inst.card.id not in fresh:
        raise ValueError("Kelvin has no card %s any more" % inst.card.id)
    card = fresh.get(inst.card.id)
    if same_card(card.bind(inst.config.vars), inst.card):
        return None
    ic = inst.config
    named = set(ic.limits) | set(ic.rates) | set(getattr(ic, "auto", {}) or {}) | set(getattr(ic, "guards", {}) or {})
    missing = sorted(p for p in named if p not in card.params)
    if missing:
        raise ValueError("the lab config names %s, which %s has no longer" % (", ".join(missing), card.id))
    source = Path(lab.config.source) if lab.config.source and not lab.config.source.startswith("<") else None
    if source is not None and source.is_file():   # the whole config, as the next start would read it
        registry = CardRegistry()
        for c in lab.registry:
            registry.put(c)
        registry.put(card)
        try:
            load_lab_config(source, registry)
        except ConfigError as e:
            ours = [x for x in e.errors if "instruments.%s" % name in x] or e.errors
            raise ValueError("the lab config would have problems with %s as it is now: %s" % (card.id,
                                                                                           "; ".join(ours[:3])))
    lab.registry.put(card)
    return card


def is_simulated(config: LabConfig) -> bool:
    """Every instrument simulated (sim://, as in tests). A lab with none is not: it runs on real time."""
    return bool(config.instruments) and all(ic.address.startswith("sim://") for ic in config.instruments.values())


def empty_lab(registry: Optional[CardRegistry] = None, switched_off: Optional[SwitchedOff] = None,
              name: str = NO_LAB) -> Lab:
    """A lab with no instruments yet: what Kelvin controls before a person sets theirs up (Lab, Set up…)."""
    return build_lab({"lab": name, "instruments": {}}, registry=registry, switched_off=switched_off)


def build_lab(config: Any, clock: Any = None, seed: int = 0, registry: Optional[CardRegistry] = None,
              switched_off: Optional[SwitchedOff] = None, partial: bool = False, later: bool = False,
              set_aside: Optional[Dict[str, str]] = None) -> Lab:
    """A lab from a lab config whose instruments may be real (VISA/serial addresses) or simulated (sim://).
    switched_off: the instruments a person switched off in the Lab panel, never connected. set_aside: instruments
    whose entry has a problem, with what (set_aside_problems): in the lab, never connected, so nothing reaches them.

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
        if set_aside and name in set_aside:   # also when reconnected: only a fixed entry and a new lab connect it
            raise RuntimeError(SET_ASIDE % set_aside[name])
        if address.startswith("sim://"):
            return sim(name, card, address)
        from .adapters import open_backend
        backend = open_backend(name, card, address, overrides=cfg.instruments[name].connection)
        opened.append(backend)
        return backend

    try:
        lab = Lab(cfg, registry, factory, clock, world, switched_off, partial or bool(set_aside), later)
    except Exception:
        for backend in opened:  # one instrument failed: let go of the ones already opened
            backend.close()
        raise
    from .instrument import Unavailable
    for name, why in (set_aside or {}).items():   # said at once, not "connecting"
        if name in lab.instruments and lab.instruments[name].enabled is False and lab.instruments[name].unavailable:
            lab.instruments[name].backend = Unavailable(name, SET_ASIDE % why)
    return lab


def set_aside_problems(source: Any, registry: CardRegistry, error: ConfigError) -> Optional[Tuple[Dict[str, Any],
                                                                                                   Dict[str, str]]]:
    """A lab config whose only problems are in instruments' own entries (a limit beyond what a changed card now
    allows, a rate, a guard, an auto rule, a connection setting): the config with each such instrument reduced to its
    card and address, and what is wrong with each. They join the lab never connected (build_lab set_aside), and the
    rest of the lab works: one stale limit once kept the whole lab from opening, saying so only in the log. None
    when a problem is anywhere else (an unknown card, the interlocks, the watches), as then no lab can be trusted."""
    try:
        data, _ = read_yaml(source)
    except Exception:
        return None
    instruments = data.get("instruments") if isinstance(data, dict) else None
    if not isinstance(instruments, dict):
        return None
    why: Dict[str, List[str]] = {}
    for err in error.errors:
        m = re.match(r"instruments\.(\w+)\.(limits|rates|auto|guards|connection|vars|trust|added_by)\b", err)
        if not m or not isinstance(instruments.get(m.group(1)), dict):
            return None
        why.setdefault(m.group(1), []).append(err.split(".", 2)[-1])
    data = dict(data, instruments={name: ({k: v for k, v in d.items() if k in ("card", "address")} if name in why else d)
                                   for name, d in instruments.items()})
    try:
        load_lab_config(data, registry)
    except ConfigError:
        return None
    return data, {name: "; ".join(w) for name, w in why.items()}


def open_home_lab(home: Any, switched_off: Optional[SwitchedOff] = None, collection: Any = None,
                  connect: bool = True) -> Tuple[Lab, Optional[Envelope], Dict[str, Any]]:
    """The lab an installed Kelvin controls: the person's own lab.yaml in `home`. An instrument that can't be
    connected (also every one of them) stays in the lab, unavailable with why; one whose entry has a problem is set
    aside. With no lab.yaml, or one Kelvin can't read at all, a lab with no instruments, and info["error"] says why;
    Lab, Set up… sets theirs up. `collection`: the instrument collection, when it is on. connect=False: the lab opens
    at once, its instruments connected afterwards in the background (LabRuntime.connect_pending).
    info["kind"]: "file" (the lab.yaml) or "none"."""
    path = Path(home) / "lab.yaml"
    info: Dict[str, Any] = {"kind": "none", "path": str(path), "error": None, "has_file": path.exists()}
    registry = lab_registry(home, collection)
    if path.exists():
        try:   # an instrument whose entry has a problem is set aside, with why, and the rest of the lab used
            config: Any = path
            aside: Dict[str, str] = {}
            try:
                load_lab_config(path, registry)
            except ConfigError as e:   # a problem in one instrument's entry sets that one aside, not the whole lab
                found = set_aside_problems(path, registry, e)
                if found is None:
                    raise
                config, aside = found
                info["set_aside"] = aside
            lab = build_lab(config, registry=registry, switched_off=switched_off, partial=connect, later=not connect,
                            set_aside=aside)
            return lab, None, dict(info, kind="file")
        except Exception as e:
            from . import envs
            info["install"] = envs.needed(str(e))   # a maker's package missing: the Lab panel offers to install it
            info["error"] = "Your lab (%s) could not be opened: %s" % (path.name,
                                                                      envs.for_people(str(e).splitlines()[0]))
    return empty_lab(registry, switched_off), None, info
