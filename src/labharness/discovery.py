"""Find the instruments connected to this computer and match them to cards.

Discovery only asks instruments who they are (*IDN? or the card's own identity query). It never sets anything,
so it is safe to run with a sample mounted. Serial ports are tried with each card's own framing (baud rate,
data bits, parity, terminations) rather than a blind sweep. Instruments the lab already uses are left alone.

    python -m labharness.discovery                 # list what is connected
    python -m labharness.discovery --write lab.yaml   # and draft a lab config to review
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .adapters import AdapterError, apply_serial_settings, is_serial, resource_manager, resource_name
from .cards import Card, CardRegistry

SHORT_NAMES = {"source-meter": "smu", "lock-in": "lockin", "temperature-controller": "tc", "magnet-supply": "magnet",
               "laser": "laser", "laser-driver": "laser", "power-meter": "pm", "preamp": "preamp", "multimeter": "dmm",
               "nanovoltmeter": "nvm", "function-generator": "fg", "vna": "vna", "motion-stage": "stage",
               "current-source": "isrc", "voltage-source": "vsrc", "pulse-generator": "pg",
               "microwave-source": "mw", "rf-source": "rf", "photon-counter": "counter", "awg": "awg",
               "delay-generator": "dg", "aom-driver": "aom", "eom-driver": "eom", "power-supply": "psu"}
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
    serial: Dict[str, Any] = field(default_factory=dict)   # those settings, as a card's or lab's connection

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
        if conn.get("enq"):
            s["enq"] = True
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
    """Its reply to the identity query, or '' for none: no reply, control characters (a NAK), or numbers only (a
    gauge controller sending its readings by itself, as Pfeiffer's do from power-up)."""
    res = rm.open_resource(name)
    try:
        res.timeout = timeout_ms
        res.read_termination = (settings or {}).get("read_termination", "\n")
        res.write_termination = (settings or {}).get("write_termination", "\n")
        if settings:
            apply_serial_settings(res, settings)
        if (settings or {}).get("enq"):   # acknowledged (passing over readings it was sending), then asked with ENQ
            res.write(query)
            for _ in range(20):
                ack = str(res.read()).strip()
                if ack in ("\x06", "\x15"):
                    break
            if ack != "\x06":
                return ""
            res.write_raw(b"\x05")
            reply = str(res.read()).strip()
        else:
            reply = str(res.query(query)).strip()
        if re.fullmatch(r"[\d\s.,;:+\-Ee]*", reply):
            return ""
        return reply if all(ch.isprintable() for ch in reply) else ""
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
             usb: Optional[Dict[str, str]] = None, queries: Optional[List[str]] = None) -> Found:
    """Ask one instrument who it is, and match the answer to a card. queries: only these (not on a serial port),
    for a quick ask where the likely card is known: each one opens the address again, which for a network
    address nobody answers at takes the connect timeout each time."""
    rm = rm or resource_manager()
    found = Found(address)
    tries: List[Tuple[str, Optional[Dict[str, Any]]]]
    if is_serial(address):
        tries = [(s["query"], s) for s in _serial_settings(registry)]
    else:
        queries = queries or ["*IDN?"] + sorted({c.idn_query for c in registry if c.idn_query != "*IDN?"})
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
        found.serial = {k: v for k, v in (settings or {}).items() if k != "query"}
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
    from .drivers import MODULES, discover_libraries, discover_swabian, discover_zhinst, library_notes
    # instruments behind vendor software: Zurich Instruments (LabOne), Swabian Pulse Streamers and Time Taggers,
    # NI DAQ cards, Ocean spectrometers, Andor and Basler cameras, Andor spectrographs, attocube ANC350s
    for address, idn in discover_zhinst(MODULES.get("zhinst")) + discover_swabian() + discover_libraries():
        if address in in_lab:
            out.append(Found(address, how="already in this lab", in_lab=in_lab[address]))
            continue
        card = match_card(idn, registry)
        out.append(Found(address, idn, card.id if card else None, card.model if card else "", card.kind if card else "",
                         "found by its maker's library" if card else "no card for this model yet"))
    looked.libraries = library_notes()
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


def _flow(values: Dict[str, Any]) -> str:
    """A YAML flow mapping, each value written as JSON (which YAML reads as it is: "\\r\\n", 57600, true)."""
    return "{ %s }" % ", ".join("%s: %s" % (k, json.dumps(v)) for k, v in values.items())


def instrument_block(item: Dict[str, Any], registry: CardRegistry) -> List[str]:
    """One instrument entry of a lab config, with its drives' vendor limits to tighten. Optional: connection and
    vars (what the person or the manual says), added_by: kelvin (Kelvin added it; no person has reviewed it)."""
    card = registry.get(item["card"])
    note = item.get("note") or card.model.split(" (")[0]
    lines = ["  %s:" % item["name"], "    card: %s        # %s" % (card.id, note), "    address: %s" % json.dumps(item["address"])]
    used = {p: v for p, v in (item.get("limits") or {}).items() if p in card.params}   # what the lab's scripts use
    lim = ["%s: [%g, %g]" % (p.name, p.limits[0], p.limits[1]) for p in card.drives
           if p.numeric and p.limits and p.name not in used]
    if used:
        lines.append("    limits: { %s }   # from your scripts (the values they use): check them, and tighten the "
                     "rest" % ", ".join(["%s: [%g, %g]" % (p, v[0], v[1]) for p, v in used.items()] + lim))
    elif lim:
        lines.append("    limits: { %s }   # vendor limits: tighten to what your sample can take" % ", ".join(lim))
    if card.kind in ("laser", "laser-driver"):
        lines.append("    auto: never        # a person approves every laser change")
    if item.get("connection"):
        lines.append("    connection: %s" % _flow(item["connection"]))
    if item.get("vars"):
        lines.append("    vars: %s" % _flow(item["vars"]))
    elif card.vars:   # names that differ between installations: say which to check
        lines.append("    vars: { %s }   # %s: check these on your instrument" % (
            ", ".join("%s: %s" % (k, v["default"]) for k, v in card.vars.items()),
            "; ".join(v["label"] for v in card.vars.values())))
    if item.get("added_by") == "kelvin":
        lines.append("    added_by: kelvin   # each output change asks you until you review the limits and delete "
                     "this line")
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


def instrument_key(entry: Dict[str, Any]) -> Tuple[str, str, str]:
    """Which instrument a lab config entry is: its address, card and card variables (one address can hold two
    entries, such as a temperature controller's two loops)."""
    variables = {str(k): str(v) for k, v in (entry.get("vars") or {}).items()}
    return (resource_name(str(entry.get("address") or "")), str(entry.get("card") or ""),
            json.dumps(variables, sort_keys=True))


def listed(text: str) -> Dict[Tuple[str, str, str], str]:
    """The instruments a lab config text lists, by instrument_key, with their names."""
    from .yamlio import load_yaml
    try:
        data = load_yaml(text) if text.strip() else None
    except Exception:
        return {}
    entries = data.get("instruments") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {instrument_key(e): str(n) for n, e in entries.items() if isinstance(e, dict)}


def add_to_config(text: str, items: Sequence[Dict[str, Any]], registry: CardRegistry) -> str:
    """Add instruments to an existing lab config text, keeping what is there (and its comments). One it lists
    already (the same address, card and variables) is not added again."""
    from .yamlio import load_yaml
    there = set(listed(text))
    fresh = []
    for item in items:
        if instrument_key(item) not in there:
            there.add(instrument_key(item))
            fresh.append(item)
    data = load_yaml(text) if text.strip() else None
    if not isinstance(data, dict) or not isinstance(data.get("instruments"), dict) or not data["instruments"]:
        name = data.get("lab", "My lab") if isinstance(data, dict) else "My lab"
        return draft_from(fresh, registry, name)
    taken = set(data["instruments"])
    blocks: List[str] = []
    for item in fresh:
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


def set_limits(text: str, name: str, limits: Dict[str, Optional[Sequence[float]]], reviewed: bool = False) -> str:
    """The lab config text with one instrument's limits changed (None takes a limit out), the rest of the text and
    its comments kept; reviewed: without the instrument's added_by: kelvin mark (a person checked its limits)."""
    from .yamlio import load_yaml
    data = load_yaml(text) if text.strip() else None
    entry = (data.get("instruments") or {}).get(name) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        raise KeyError("no instrument %r in the lab config" % name)
    merged: Dict[str, List[float]] = {str(k): list(v) for k, v in (entry.get("limits") or {}).items()}
    for p, lim in limits.items():
        if lim is None:
            merged.pop(p, None)
        else:
            merged[p] = [float(lim[0]), float(lim[1])]
    lines = text.split("\n")
    start = next(i for i, l in enumerate(lines) if re.match(r"^instruments\s*:", l))
    at = next((i for i in range(start + 1, len(lines))
               if re.match(r"^(\s+)%s\s*:" % re.escape(name), lines[i])), None)
    if at is None:
        raise KeyError("no instrument %r in the lab config" % name)
    indent = len(lines[at]) - len(lines[at].lstrip())
    head, _, rest = lines[at].partition(":")
    if rest.strip() and not rest.strip().startswith("#"):   # written on one line: name: { card: ..., ... }
        entry = dict(entry, limits=merged) if merged else {k: v for k, v in entry.items() if k != "limits"}
        if reviewed:
            entry.pop("added_by", None)
        lines[at] = "%s: %s" % (head, _flow(entry))
        return "\n".join(lines)
    end = at + 1
    while end < len(lines) and (not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > indent):
        end += 1
    while end > at + 1 and not lines[end - 1].strip():
        end -= 1
    block = lines[at + 1:end]
    child = min((len(l) - len(l.lstrip()) for l in block if l.strip() and not l.lstrip().startswith("#")),
                default=indent + 2)
    out, i, placed = [], 0, False
    new = ("%slimits: { %s }   # set in Kelvin (Set up, Limits…)" % (" " * child, ", ".join(
        "%s: [%g, %g]" % (p, v[0], v[1]) for p, v in merged.items()))) if merged else None
    while i < len(block):
        line = block[i]
        depth = len(line) - len(line.lstrip())
        if depth == child and re.match(r"^\s*limits\s*:", line):
            i += 1
            while i < len(block) and (not block[i].strip() or len(block[i]) - len(block[i].lstrip()) > child):
                i += 1   # a limits mapping written as a block: its lines go too
            if new:
                out.append(new)
            placed = True
            continue
        if reviewed and depth == child and re.match(r"^\s*added_by\s*:", line):
            i += 1
            continue
        out.append(line)
        i += 1
    if not placed and new:
        after = next((k for k, l in enumerate(out) if re.match(r"^\s*address\s*:", l)), len(out) - 1)
        out.insert(after + 1, new)
    return "\n".join(lines[:at + 1] + out + lines[end:])


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
