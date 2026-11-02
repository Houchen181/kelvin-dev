"""What each LabVIEW call becomes in a Kelvin experiment, for the translator and its checker.

A VI calls a maker's driver (Keithley 24XX.lvlib:Enable Output.vi), a vi.lib VI (Write Delimited Spreadsheet.vi), the
lab's own sub-VIs, ActiveX or .NET objects (MG17Motor.MoveAbsoluteRot) and DLLs. A translation replaces each, never
converts it: a driver's call becomes the parameters of the instrument's card it sets, reads or runs (through
kelvin.lab, so the gate checks each one), a vi.lib VI becomes plain Python or the experiment API, and some become
nothing (Kelvin opens and closes instruments itself), said why.

Where a call's replacement comes from, first found first:
- the lab's own map (~/Kelvin/labview-map.yaml), what people in the lab accepted;
- Kelvin's map (data/labview/map.yaml);
- the commands a driver's VI sends, matched against the lab's cards as the gate matches a raw command to a card's
  setting (scriptrun._setter), its readings and its actions;
- else unmapped: the translation lists it as not translated, and may propose an entry a person accepts.
"""
from __future__ import annotations

import datetime
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

KELVINS = Path(__file__).parent / "data" / "labview" / "map.yaml"
BECOMES = ("card", "python", "nothing", "unmapped")
_PLACE = re.compile(r"\{[^{}]*\}")


@dataclass
class Mapping:
    call: str                  # the call as the description names it, or "<object>.<method>" for an ActiveX node
    becomes: str               # "card" | "python" | "nothing" | "unmapped"
    card: str = ""             # the card's id, when it becomes a card's parameters
    sets: List[str] = field(default_factory=list)   # the card's parameters the call sets
    gets: List[str] = field(default_factory=list)   # ... reads
    does: List[str] = field(default_factory=list)   # ... the card's actions it runs
    python: str = ""           # what it becomes otherwise ("time.sleep(seconds)")
    why: str = ""              # why nothing replaces it
    source: str = ""           # "kelvin" (Kelvin's map) | "lab" (the lab's map) | "commands" (matched) | ""

    def words(self) -> str:
        if self.becomes == "card":
            parts = [("sets " + ", ".join(self.sets)) if self.sets else "", ("reads " + ", ".join(self.gets))
                     if self.gets else "", ("does " + ", ".join(self.does)) if self.does else ""]
            return "card %s: %s" % (self.card, "; ".join(p for p in parts if p) or "nothing on it")
        if self.becomes == "python":
            return self.python
        if self.becomes == "nothing":
            return "nothing: " + self.why
        return "unmapped"


def _problem(entry: Any) -> str:
    """Why an entry can't be used ("": it can)."""
    if not isinstance(entry, dict) or not str(entry.get("call") or "").strip():
        return "an entry needs call: the call's name"
    if entry.get("becomes") not in BECOMES[:3]:
        return "%s: becomes is card, python or nothing" % entry["call"]
    if entry["becomes"] == "card" and not str(entry.get("card") or "").strip():
        return "%s: becomes card, but names no card" % entry["call"]
    if entry["becomes"] == "card" and not any(entry.get(k) for k in ("sets", "gets", "does")):
        return "%s: becomes card, but names no parameter or action" % entry["call"]
    for k in ("sets", "gets", "does"):
        if entry.get(k) is not None and (not isinstance(entry[k], list)
                                         or not all(isinstance(x, str) and x for x in entry[k])):
            return "%s: %s is a list of the card's names" % (entry["call"], k)
    return ""


def _mapping(entry: Dict[str, Any], source: str) -> Mapping:
    return Mapping(call=str(entry["call"]), becomes=entry["becomes"], card=str(entry.get("card") or ""),
                   sets=list(entry.get("sets") or []), gets=list(entry.get("gets") or []),
                   does=list(entry.get("does") or []), python=str(entry.get("python") or ""),
                   why=str(entry.get("why") or ""), source=source)


class Table:
    """Kelvin's map and the lab's, by call name: the lab's first."""

    def __init__(self, entries: Iterable[Dict[str, Any]], lab_entries: Iterable[Dict[str, Any]] = ()):
        self.notes: List[str] = []   # the lab's entries that couldn't be used, and why
        self._by: Dict[str, Mapping] = {}
        for e in entries:
            why = _problem(e)
            if why:
                raise ValueError("Kelvin's LabVIEW map: " + why)
            self._by.setdefault(str(e["call"]).casefold(), _mapping(e, "kelvin"))
        for e in lab_entries:
            why = _problem(e)
            if why:
                self.notes.append("the lab's LabVIEW map: " + why)
                continue
            self._by[str(e["call"]).casefold()] = _mapping(e, "lab")

    def find(self, call: str, entry: Dict[str, Any]) -> Optional[Mapping]:
        """The replacement a map gives this call (entry: its description's calls entry, unused yet), or None."""
        return self._by.get(call.casefold())


def _entries(path: Path) -> List[Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    return data if isinstance(data, list) else []


def load(lab_file: Optional[Path] = None) -> Table:
    """Kelvin's map, with the lab's own (labview-map.yaml in Kelvin's folder) over it."""
    own: List[Any] = []
    notes: List[str] = []
    if lab_file is not None:
        try:
            own = _entries(Path(lab_file))
        except (OSError, yaml.YAMLError) as e:
            notes.append("the lab's LabVIEW map can't be read: %s" % e)
    table = Table(_entries(KELVINS), own)
    table.notes[:0] = notes
    return table


def by_commands(sends: List[Dict[str, Any]], cards: Iterable[Any]) -> Optional[Mapping]:
    """The card whose settings, readings and actions match every command a driver's VI sends (as the gate matches a
    raw command to a card's setting), with what each sets, reads or does; None when no card matches them all."""
    from .scriptrun import _setter   # the gate's reading of a command as a card's setting
    texts = [(_PLACE.sub("1", str(s.get("text") or "")).strip(), bool(s.get("query"))) for s in sends]
    texts = [(t, q or t.split(" ", 1)[0].endswith("?")) for t, q in texts if t]
    if not texts:
        return None
    for card in cards:
        sets: List[str] = []
        gets: List[str] = []
        does: List[str] = []
        for text, query in texts:
            hit = ""
            if query:
                for spec in card.getters:
                    try:
                        if spec.get_cmd.match(text) is not None:
                            hit = spec.name
                            gets.append(hit)
                            break
                    except Exception:
                        continue
            else:
                spec, _ = _setter(card, text)
                if spec is not None:
                    hit = spec.name
                    sets.append(hit)
                else:
                    for name, action in card.actions.items():
                        if len(action.send) == 1 and action.send[0].strip().lower() == text.lower():
                            hit = name
                            does.append(name)
                            break
            if not hit:
                break
        else:
            return Mapping(call="", becomes="card", card=card.id, sets=_unique(sets), gets=_unique(gets),
                           does=_unique(does), source="commands")
    return None


def _unique(xs: List[str]) -> List[str]:
    return list(dict.fromkeys(xs))


def _object_nodes(diagram: Optional[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for n in (diagram or {}).get("nodes", []):
        if n.get("kind") in ("invoke", "property"):
            yield n
        for f in n.get("frames", []):
            yield from _object_nodes(f)


def map_description(desc: Dict[str, Any], table: Table, cards: Iterable[Any]) -> Dict[str, Mapping]:
    """What each call of a VI and of the lab's own sub-VIs it calls becomes, by the call's name: every call entry
    and every ActiveX/.NET property or invoke node ("<object>.<method>"), each mapped or said unmapped."""
    cards = list(cards)
    out: Dict[str, Mapping] = {}
    descs = [desc] + list((desc.get("subvis") or {}).values())
    for d in descs:
        for name, entry in (d.get("calls") or {}).items():
            if name in out:
                continue
            m = table.find(name, entry)
            if m is None and entry.get("kind") == "own":
                m = Mapping(name, "python", python="a function of the experiment's own, translated from the sub-VI's "
                                                   "description", source="kelvin")
            if m is None and entry.get("kind") == "driver" and entry.get("sends"):
                m = by_commands(entry["sends"], cards)
            out[name] = _named(m, name) if m is not None else Mapping(name, "unmapped")
        for n in _object_nodes(d.get("diagram")):
            for what in ([n.get("method")] if n["kind"] == "invoke" else list(n.get("items") or [])):
                key = "%s.%s" % (n.get("of") or "?", what)
                if key not in out:
                    m = table.find(key, n)
                    out[key] = _named(m, key) if m is not None else Mapping(key, "unmapped")
    return out


def _named(m: Mapping, call: str) -> Mapping:
    return Mapping(**dict(m.__dict__, call=call))


def text(mappings: Dict[str, Mapping]) -> str:
    """The mappings as lines, for the model's prompt and a person's card."""
    return "\n".join('"%s" -> %s%s' % (call, m.words(), " (by the commands it sends)" if m.source == "commands" else "")
                     for call, m in sorted(mappings.items()))


def accept(lab_file: Path, proposed: List[Dict[str, Any]], by: str) -> List[str]:
    """A person's yes to mappings a translation proposed: each usable one goes into the lab's map (replacing an
    entry for the same call), with who and when. Says what changed; an entry that can't be used is left out."""
    lab_file = Path(lab_file)
    try:
        entries = [e for e in _entries(lab_file) if isinstance(e, dict)]
    except (OSError, yaml.YAMLError):
        entries = []
    said: List[str] = []
    when = datetime.datetime.now().isoformat(timespec="seconds")
    for p in proposed:
        if _problem(p):
            continue
        entry = {k: p[k] for k in ("call", "becomes", "card", "sets", "gets", "does", "python", "why") if p.get(k)}
        entry.update(by=by, at=when)
        entries = [e for e in entries if str(e.get("call", "")).casefold() != entry["call"].casefold()]
        entries.append(entry)
        said.append("the lab's map now says %s -> %s" % (entry["call"], _mapping(entry, "lab").words()))
    if said:
        lab_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = lab_file.with_name(lab_file.name + ".tmp")
        tmp.write_text("# What LabVIEW calls become in this lab's experiments: people accepted these "
                       "(labharness/vimap.py)\n" + yaml.safe_dump(entries, sort_keys=False, allow_unicode=True),
                       encoding="utf-8")
        os.replace(tmp, lab_file)
    return said
