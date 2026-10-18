"""Find the instruments connected to this computer and match them to cards.

Discovery only asks instruments who they are (*IDN? or the card's own identity query). It never sets anything,
so it is safe to run with a sample mounted. Serial ports are tried with each card's own framing (baud rate,
data bits, parity, terminations) rather than a blind sweep. Instruments the lab already uses are left alone.

    python -m labharness.discovery                 # list what is connected
    python -m labharness.discovery --write lab.yaml   # and draft a lab config to review
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .adapters import AdapterError, apply_serial_settings, is_serial, resource_manager
from .cards import Card, CardRegistry

SHORT_NAMES = {"source-meter": "smu", "lock-in": "lockin", "temperature-controller": "tc", "magnet-supply": "magnet",
               "laser": "laser", "laser-driver": "laser", "power-meter": "pm", "preamp": "preamp", "multimeter": "dmm",
               "nanovoltmeter": "nvm", "function-generator": "fg", "vna": "vna", "motion-stage": "stage",
               "current-source": "isrc", "voltage-source": "vsrc"}
DEFAULT_SERIAL = [(9600, "\n"), (115200, "\n"), (19200, "\r\n"), (57600, "\r\n")]


@dataclass
class Found:
    address: str
    idn: str = ""
    card_id: Optional[str] = None
    model: str = ""
    kind: str = ""
    how: str = ""          # "identity reply", "USB ID", or why nothing matched
    settings: str = ""     # serial settings that answered, e.g. "57600 7O1"
    in_lab: Optional[str] = None

    def suggest_name(self, taken: Iterable[str] = ()) -> str:
        base = SHORT_NAMES.get(self.kind, re.sub(r"[^a-z0-9]+", "", self.kind.lower()) or "instr")
        taken = set(taken)
        if base not in taken:
            return base
        i = 2
        while "%s%d" % (base, i) in taken:
            i += 1
        return "%s%d" % (base, i)

    def line(self) -> str:
        where = " (in this lab as %s)" % self.in_lab if self.in_lab else ""
        if self.card_id:
            return "%s: %s → card %s (%s)%s" % (self.address, self.model, self.card_id, self.kind, where)
        if self.idn:
            return "%s: answered %r but no card matches; a card can be drafted for it%s" % (self.address, self.idn[:80], where)
        return "%s: %s%s" % (self.address, self.how or "no answer", where)


def match_card(idn: str, registry: CardRegistry) -> Optional[Card]:
    """The card whose identity pattern fits this reply; the most specific pattern wins."""
    hits = [c for c in registry if c.idn_pattern and re.search(c.idn_pattern, idn, re.I)]
    return max(hits, key=lambda c: len(c.idn_pattern or "")) if hits else None


def _serial_settings(registry: CardRegistry) -> List[Dict[str, Any]]:
    """Each distinct way a known card talks over serial, then common defaults."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for card in registry:
        conn = card.connection
        if "baud_rate" not in conn or conn.get("write_only"):
            continue
        s = {"baud_rate": conn["baud_rate"], "data_bits": conn.get("data_bits", 8), "parity": conn.get("parity", "none"),
             "stop_bits": conn.get("stop_bits", 1.0), "read_termination": conn.get("read_termination", "\n"),
             "write_termination": conn.get("write_termination", "\n"), "query": card.idn_query}
        key = tuple(sorted(s.items()))
        if key not in seen:
            seen.add(key)
            out.append(s)
    for baud, term in DEFAULT_SERIAL:
        s = {"baud_rate": baud, "data_bits": 8, "parity": "none", "stop_bits": 1.0, "read_termination": term,
             "write_termination": term, "query": "*IDN?"}
        key = tuple(sorted(s.items()))
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


def _framing(s: Dict[str, Any]) -> str:
    return "%d %d%s%g" % (s["baud_rate"], s["data_bits"], str(s["parity"])[0].upper(), s["stop_bits"])


def _ask(rm: Any, name: str, query: str, timeout_ms: int, settings: Optional[Dict[str, Any]] = None) -> str:
    res = rm.open_resource(name)
    try:
        res.timeout = timeout_ms
        res.read_termination = (settings or {}).get("read_termination", "\n")
        res.write_termination = (settings or {}).get("write_termination", "\n")
        if settings:
            apply_serial_settings(res, settings)
        reply = str(res.query(query)).strip()
        return reply if reply and all(ch.isprintable() for ch in reply) else ""
    finally:
        try:
            res.close()
        except Exception:
            pass


def _usb_ids() -> Dict[str, str]:
    """Serial port resource -> "vid:pid" for USB-serial devices, where the OS tells us."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return {}
    out = {}
    for port in list_ports.comports():
        if port.vid is None:
            continue
        num = re.match(r"^COM(\d+)$", port.device, re.I)
        name = "ASRL%s::INSTR" % (num.group(1) if num else port.device)
        out[name] = "%04x:%04x" % (port.vid, port.pid)
    return out


def identify(address: str, registry: CardRegistry, rm: Any = None, timeout_ms: int = 1000,
             usb: Optional[Dict[str, str]] = None) -> Found:
    """Ask one instrument who it is, and match the answer to a card."""
    rm = rm or resource_manager()
    found = Found(address)
    tries: List[Tuple[str, Optional[Dict[str, Any]]]]
    if is_serial(address):
        tries = [(s["query"], s) for s in _serial_settings(registry)]
    else:
        queries = ["*IDN?"] + sorted({c.idn_query for c in registry if c.idn_query != "*IDN?"})
        tries = [(q, None) for q in queries]
    for query, settings in tries:
        try:
            reply = _ask(rm, address, query, timeout_ms, settings)
        except Exception:
            continue
        if not reply:
            continue
        found.idn = reply
        found.settings = _framing(settings) if settings else ""
        card = match_card(reply, registry)
        if card:
            found.card_id, found.model, found.kind, found.how = card.id, card.model, card.kind, "identity reply"
            return found
        found.how = "no card matches the reply"
        return found
    vid_pid = (usb if usb is not None else _usb_ids()).get(address)
    if vid_pid:
        cards = [c for c in registry if vid_pid in c.usb_ids]
        if cards:
            card = cards[0]
            found.card_id, found.model, found.kind, found.how = card.id, card.model, card.kind, "USB ID %s" % vid_pid
            return found
    found.how = "no answer (it may be write-only, use other settings, or be switched off)"
    return found


def scan(registry: CardRegistry, rm: Any = None, in_lab: Optional[Dict[str, str]] = None,
         timeout_ms: int = 800, network: bool = True) -> List[Found]:
    """Every instrument VISA can see, identified. `in_lab` maps addresses already used by the lab to their names;
    those are not queried (they may be busy), only listed."""
    return scan_survey(registry, rm, in_lab, timeout_ms, network)[0]


def scan_survey(registry: CardRegistry, rm: Any = None, in_lab: Optional[Dict[str, str]] = None,
                timeout_ms: int = 800, network: bool = True, **survey_options: Any) -> Tuple[List[Found], Any]:
    """The scan, and what it looked through (survey.py): VISA, the local network, USB, serial ports and GPIB."""
    from .adapters import resource_name
    from .survey import survey
    rm = rm or resource_manager()
    in_lab = {resource_name(a): n for a, n in (in_lab or {}).items()}
    looked = survey(rm, network=network, **survey_options)
    resources = list(looked.resources)
    usb = _usb_ids()
    out = []
    for name in sorted(set(resources)):
        if name in in_lab:
            out.append(Found(name, how="already in this lab", in_lab=in_lab[name]))
            continue
        out.append(identify(name, registry, rm, timeout_ms, usb))
    from .drivers import MODULES, discover_zhinst
    for address, idn in discover_zhinst(MODULES.get("zhinst")):   # instruments behind vendor software
        if address in in_lab:
            out.append(Found(address, how="already in this lab", in_lab=in_lab[address]))
            continue
        card = match_card(idn, registry)
        out.append(Found(address, idn, card.id if card else None, card.model if card else "", card.kind if card else "",
                         "LabOne discovery" if card else "no card for this model yet"))
    return out, looked


def draft_lab_config(found: Sequence[Found], registry: CardRegistry, lab_name: str = "My lab") -> str:
    """A lab config to review: every identified instrument, with its drives' vendor limits as placeholders that the
    instrument owner should tighten to what the sample can take."""
    items, taken = [], []
    for f in found:
        if not f.card_id or f.in_lab:
            continue
        name = f.suggest_name(taken)
        taken.append(name)
        items.append({"name": name, "card": f.card_id, "address": f.address, "note": f.idn[:60] if f.idn else f.how})
    return draft_from(items, registry, lab_name)


def instrument_block(item: Dict[str, Any], registry: CardRegistry) -> List[str]:
    """One instrument entry of a lab config, with its drives' vendor limits to tighten."""
    card = registry.get(item["card"])
    note = item.get("note") or card.model.split(" (")[0]
    lines = ["  %s:" % item["name"], "    card: %s        # %s" % (card.id, note), "    address: \"%s\"" % item["address"]]
    lim = ["%s: [%g, %g]" % (p.name, p.limits[0], p.limits[1]) for p in card.drives if p.numeric and p.limits]
    if lim:
        lines.append("    limits: { %s }   # vendor limits: tighten to what your sample can take" % ", ".join(lim))
    if card.kind in ("laser", "laser-driver"):
        lines.append("    auto: never        # a person approves every laser change")
    if card.vars:   # names that differ between installations: say which to check
        lines.append("    vars: { %s }   # %s: check these on your instrument" % (
            ", ".join("%s: %s" % (k, v["default"]) for k, v in card.vars.items()),
            "; ".join(v["label"] for v in card.vars.values())))
    return lines


def draft_from(items: Sequence[Dict[str, Any]], registry: CardRegistry, lab_name: str = "My lab") -> str:
    """A lab config from chosen instruments: [{name, card, address, note?}]."""
    lines = ["# Review before use: set limits for what your sample can take, rates for gate-ramped drives,",
             "# and guards (e.g. stop a gate ramp if the leakage current rises).",
             "lab: %s" % lab_name, "", "instruments:"]
    for item in items:
        lines += instrument_block(item, registry)
    if not items:
        lines.append("  {}   # nothing chosen yet")
    return "\n".join(lines) + "\n"


def add_to_config(text: str, items: Sequence[Dict[str, Any]], registry: CardRegistry) -> str:
    """Add instruments to an existing lab config text, keeping what is there (and its comments)."""
    from .yamlio import load_yaml
    data = load_yaml(text) if text.strip() else None
    if not isinstance(data, dict) or not isinstance(data.get("instruments"), dict) or not data["instruments"]:
        name = data.get("lab", "My lab") if isinstance(data, dict) else "My lab"
        return draft_from(items, registry, name)
    taken = set(data["instruments"])
    blocks: List[str] = []
    for item in items:
        item = dict(item)
        if item["name"] in taken:
            base, i = item["name"], 2
            while "%s%d" % (base, i) in taken:
                i += 1
            item["name"] = "%s%d" % (base, i)
        taken.add(item["name"])
        blocks += instrument_block(item, registry)
    lines = text.rstrip("\n").split("\n")
    start = next(i for i, l in enumerate(lines) if re.match(r"^instruments\s*:", l))
    end = next((i for i in range(start + 1, len(lines)) if lines[i] and not lines[i][0].isspace()
                and not lines[i].startswith("#")), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[:end] + blocks + lines[end:]) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    from .virtual_lab import builtin_registry
    ap = argparse.ArgumentParser(prog="kelvin-scan", description="Find connected instruments and match them to cards.")
    ap.add_argument("--write", metavar="LAB_YAML", help="also draft a lab config with what was found")
    ap.add_argument("--timeout", type=int, default=800, help="ms to wait for each identity reply")
    args = ap.parse_args(argv)
    registry = builtin_registry()
    try:
        found = scan(registry, timeout_ms=args.timeout)
    except AdapterError as e:
        print(e, file=sys.stderr)
        return 2
    if not found:
        print("No instruments found. Check that VISA is installed (NI-VISA or Keysight IO Libraries) and the "
              "instruments are on and connected.")
    for f in found:
        print(f.line())
    if args.write:
        with open(args.write, "w", encoding="utf-8") as fh:
            fh.write(draft_lab_config(found, registry))
        print("Drafted %s; review the limits before using it." % args.write)
    return 0


if __name__ == "__main__":
    sys.exit(main())
