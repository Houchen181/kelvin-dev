"""Find the instruments connected to this computer and match them to cards.

Discovery only asks instruments who they are (*IDN? or the card's own identity query). It never sets anything,
so it is safe to run with a sample mounted. Serial ports are tried with each card's own framing (baud rate,
data bits, parity, terminations) rather than a blind sweep. Instruments the lab already uses are left alone.
An identity reply no card matches is looked up in the instrument collection (`catalog`, collection.py), which
names the instrument and says where its maker publishes the manual.

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
    collection: str = ""   # no card matches, but the instrument collection knows it: its id there
    manuals: List[str] = field(default_factory=list)   # and the addresses of its public manuals

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
        if self.idn and self.collection:
            return ("%s: %s (%s in the instrument collection) answered, but there is no card for it yet; a card can be "
                    "drafted from its manual%s%s" % (self.address, self.model, self.collection,
                                                     " (%s)" % self.manuals[0] if self.manuals else "", where))
        if self.idn:
            return "%s: answered %r but no card matches; a card can be drafted for it%s" % (self.address, self.idn[:80], where)
        return "%s: %s%s" % (self.address, self.how or "no answer", where)


def match_card(idn: str, registry: CardRegistry) -> Optional[Card]:
    """The card whose identity pattern fits this reply; the most specific pattern wins."""
    hits = [c for c in registry if c.idn_pattern and re.search(c.idn_pattern, idn, re.I)]
    return max(hits, key=lambda c: len(c.idn_pattern or "")) if hits else None


GENERIC_SCHEMES = ("http", "https", "lib", "serial", "visa", "sim", "modbus")   # any instrument's: no card follows
_OPTION = re.compile(r"[?&]([a-z0-9_]+=[^&\s\"',)]+)", re.I)


def by_scheme(address: str, registry: CardRegistry) -> List[Card]:
    """The cards for a maker's-driver address (qd://... is a Quantum Design system: qd/multivu), from the addresses
    the cards themselves give; where several share the scheme, those whose example has the address's options
    (andor://0?sdk=2 is the SDK2 camera), then those that name it most."""
    scheme = address.split("://", 1)[0].lower() if "://" in address else ""
    if not scheme or scheme in GENERIC_SCHEMES:
        return []
    hits = []
    for card in registry:
        text = json.dumps(card.data, ensure_ascii=False) if card.data else " ".join(card.notes)
        examples = re.findall(r"(?<![a-z0-9])%s://[^\s\"',)]*" % re.escape(scheme), text, re.I)
        if examples:
            hits.append((card, examples))
    options = set(o.lower() for o in _OPTION.findall(address))
    if len(hits) > 1 and options:
        same = [h for h in hits if options & {o.lower() for e in h[1] for o in _OPTION.findall(e)}]
        hits = same or hits
    if len(hits) > 1:
        most = max(len(examples) for _, examples in hits)
        hits = [h for h in hits if len(h[1]) == most]
    return [card for card, _ in hits]


def _words(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in ("model", "the", "and", "card")]


def cards_named(name: str, registry: CardRegistry) -> List[Card]:
    """The cards a name means (american-magnetics/430, AMI 430, smc100, Mercury iTC): the card with that id, else those
    whose id or model has every word of the name that has a digit in it and one of its other words (the maker's), or,
    for a name without a number, all of its words."""
    flat = re.sub(r"[^a-z0-9]+", "", name.lower())
    exact = [c for c in registry if re.sub(r"[^a-z0-9]+", "", c.id.lower()) == flat]
    if exact or not flat:
        return exact
    words = _words(name)
    numbered = [w for w in words if any(ch.isdigit() for ch in w)]
    plain = [w for w in words if w not in numbered and len(w) > 2]

    def near(w: str, mine: set) -> bool:
        return w in mine or any(m.startswith(w) or w.startswith(m) for m in mine if len(m) > 2)
    out = []
    for card in registry:
        have = " ".join([card.id, card.model, card.vendor]).lower()
        mine = set(_words(have))
        if numbered:   # a model number as a word, or inside one (smc100 in newport/smc100), never a bare digit or two
            fits = all(w in mine or len(w) > 2 and w in re.sub(r"[^a-z0-9]+", "", have) for w in numbered) and \
                (not plain or any(near(w, mine) for w in plain))
        else:
            fits = bool(plain) and all(near(w, mine) for w in plain)
        if fits:
            out.append(card)
    return out


def card_for(address: str, registry: CardRegistry, idn: str = "", name: str = "") -> Tuple[Optional[Card], str]:
    """The card for an instrument Kelvin is about to add, before anyone drafts one: the one named (by id, or by maker
    and model: name), the one its identity reply matches, or the one its driver address calls for (qd://). (card,
    how it was found), or (None, why not: the candidates when several fit)."""
    if name:
        named = cards_named(name, registry)
        if len(named) == 1:
            return named[0], "" if named[0].id == name.strip() else "its name (%s)" % name
        if named:
            return None, "several cards fit %r: %s; give card=<one of them>" % (name, ", ".join(c.id for c in named))
        return None, "there is no card %r" % name
    if idn:
        card = match_card(idn, registry)
        if card is not None:
            return card, "its identity reply"
    schemed = by_scheme(address, registry)
    if len(schemed) == 1:
        return schemed[0], "its address (%s://)" % address.split("://", 1)[0]
    if schemed:
        return None, "several cards fit %s addresses: %s; give card=<one of them>" % (
            address.split("://", 1)[0], ", ".join(c.id for c in schemed))
    return None, ""


def find_cards(query: str, registry: CardRegistry, catalog: Any = None, limit: int = 10) -> Dict[str, Any]:
    """Kelvin's cards (its own, the instrument collection's and the lab's, as the registry has them) and the
    collection's instruments that have no card yet, for a query: words (maker, model, kind), an identity reply, or a
    driver address. {cards: [Card], entries: [collection entries without a card]}, the best first."""
    query = (query or "").strip()
    if "://" in query:
        return {"cards": by_scheme(query, registry)[:limit], "entries": []}
    cards: List[Card] = []
    entries: List[Dict[str, Any]] = []
    if "," in query and len(query) > 12:   # an identity reply: what it matches
        card = match_card(query, registry)
        cards = [card] if card else []
        entry = catalog.instrument_for(query) if catalog is not None else None
        if entry and not isinstance(entry.get("card"), dict):
            entries.append(entry)
        if cards or entries:
            return {"cards": cards, "entries": entries}
    words = [w for w in _words(query) if len(w) > 1]
    if not words:
        return {"cards": sorted(registry, key=lambda c: c.id)[:limit] if not query else [], "entries": []}

    def score(text: str, name: str) -> int:
        hay, title = set(_words(text)), set(_words(name))
        hits = [w for w in words if w in hay or (len(w) > 3 and any(h.startswith(w) for h in hay))]
        if len(hits) < len(words) and not any(any(ch.isdigit() for ch in w) and w in hay for w in words):
            return 0   # every word, or the model number
        return sum(3 if w in title else 1 for w in hits)
    ranked = []
    for card in registry:
        s = score(" ".join([card.id, card.model, card.vendor, card.kind] + card.notes[:2]), card.id + " " + card.model)
        if s:
            ranked.append((-s, card.id, card))
    cards = [c for _, _, c in sorted(ranked, key=lambda r: r[:2])][:limit]
    if catalog is not None:
        have = {c.id for c in registry}
        found = []
        for entry in catalog.index().get("instruments") or []:
            if not isinstance(entry, dict) or isinstance(entry.get("card"), dict) or entry.get("id") in have:
                continue
            s = score(" ".join(str(entry.get(k) or "") for k in ("id", "maker", "model", "name", "kind")),
                      "%s %s" % (entry.get("id") or "", entry.get("name") or ""))
            if s:
                found.append((-s, str(entry.get("id")), entry))
        entries = [e for _, _, e in sorted(found, key=lambda r: r[:2])][:limit]
    return {"cards": cards, "entries": entries}


def look_up(found: Found, catalog: Any) -> Found:
    """An instrument that answered but matches no card: its name, kind and public manuals from the instrument
    collection (collection.Collection), if the collection knows its identity reply."""
    entry = catalog.instrument_for(found.idn) if catalog is not None and found.idn else None
    if entry:
        from .collection import manuals
        found.collection, found.manuals = str(entry["id"]), manuals(entry)
        found.model = str(entry.get("name") or "%s %s" % (entry.get("maker", ""), entry.get("model", ""))).strip()
        found.kind = str(entry.get("kind") or "")
        found.how = "no card yet; the instrument collection knows it as %s" % entry["id"]
    return found


def _settings_of(card: Card) -> Dict[str, Any]:
    conn = card.connection
    s = {"baud_rate": conn["baud_rate"], "data_bits": conn.get("data_bits", 8), "parity": conn.get("parity", "none"),
         "stop_bits": conn.get("stop_bits", 1.0), "read_termination": conn.get("read_termination", "\n"),
         "write_termination": conn.get("write_termination", "\n"), "query": card.idn_query}
    if conn.get("enq"):
        s["enq"] = True
    return s


# boards that reset when their port opens (the DTR line): Arduinos and CH340 USB-serial adapters. Each framing tried
# was one more reset of a student's hot plate, heater off (l07: seven times on COM4), so such a port is asked once.
RESETS_ON_OPEN = ("2341:", "2a03:", "1a86:7523", "1a86:5523")


def resets_on_open(vid_pid: Optional[str]) -> bool:
    return bool(vid_pid) and str(vid_pid).lower().startswith(RESETS_ON_OPEN)


BOOT_S = 3.0   # how long such a board may take to start (an Arduino's bootloader waits about two seconds)
# a reply that refuses the query rather than answering it: an error token, a lone ?, an unknown-command line
REFUSAL = re.compile(r"^(?:err(?:or)?\b.*|\?+|nak|unknown(?: command)?\b.*|invalid\b.*|syntax error\b.*|bad command\b.*|"
                     r"command not (?:found|recognized)\b.*|not recognized\b.*)$", re.I)


def _serial_settings(registry: CardRegistry) -> List[Dict[str, Any]]:
    """Each distinct way a known card talks over serial, then common defaults."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for card in registry:
        conn = card.connection
        if "baud_rate" not in conn or conn.get("write_only"):
            continue
        s = _settings_of(card)
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


def _ask(rm: Any, name: str, query: str, timeout_ms: int, settings: Optional[Dict[str, Any]] = None,
         booting: float = 0.0, said: Optional[List[str]] = None) -> str:
    """Its reply to the identity query, or '' for none: no reply, control characters (a NAK), numbers only (a gauge
    controller sending its readings by itself, as Pfeiffer's do from power-up), or a refusal (ERR). booting: the
    seconds a board that resets as its port opens may take to start; what it prints meanwhile (an Arduino sketch's
    READY) goes into `said`, and the query is sent after it, as sent earlier it is lost in the bootloader."""
    res = rm.open_resource(name)
    try:
        res.timeout = timeout_ms
        res.read_termination = (settings or {}).get("read_termination", "\n")
        res.write_termination = (settings or {}).get("write_termination", "\n")
        if settings:
            apply_serial_settings(res, settings)
        if booting:
            res.timeout = int(booting * 1000)
            try:
                said = said if said is not None else []
                said.append(str(res.read()).strip())   # the first line once it has started, or nothing
                res.timeout = 300
                for _ in range(3):
                    said.append(str(res.read()).strip())
            except Exception:
                pass
            res.timeout = timeout_ms
        if name.upper().endswith("::SOCKET"):   # what it says by itself as a connection opens (an AMI 430 greets)
            res.timeout = min(timeout_ms, 300)
            for _ in range(3):
                try:
                    res.read()
                except Exception:
                    break
            res.timeout = timeout_ms
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
            if reply == query.strip():   # it echoes each command (an ANC300's command line): its answer comes next
                reply = str(res.read()).strip()
        if re.fullmatch(r"[\d\s.,;:+\-Ee]*", reply) or REFUSAL.match(reply) or reply in (said or []):
            return ""   # not who it is: its readings, a refusal of the query, or the banner it printed again
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
             usb: Optional[Dict[str, str]] = None, queries: Optional[List[str]] = None, catalog: Any = None) -> Found:
    """Ask one instrument who it is, and match the answer to a card (or look it up in `catalog`, the instrument
    collection). queries: only these (not on a serial port), for a quick ask where the likely card is known: each one
    opens the address again, which for a network address nobody answers at takes the connect timeout each time."""
    rm = rm or resource_manager()
    found = Found(address)
    tries: List[Tuple[str, Optional[Dict[str, Any]]]]
    vid_pid = (usb if usb is not None else _usb_ids()).get(address) if is_serial(address) else None
    if resets_on_open(vid_pid):
        return _ask_board(found, rm, registry, vid_pid, timeout_ms, catalog)
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
        return look_up(found, catalog)
    vid_pid = vid_pid or (usb if usb is not None else _usb_ids()).get(address)
    if vid_pid:
        cards = [c for c in registry if vid_pid in c.usb_ids]
        if cards:
            card = cards[0]
            found.card_id, found.model, found.kind, found.how = card.id, card.model, card.kind, "USB ID %s" % vid_pid
            return found
    found.how = "no answer (it may be write-only, use other settings, or be switched off)"
    return found


def _ask_board(found: Found, rm: Any, registry: CardRegistry, vid_pid: str, timeout_ms: int, catalog: Any) -> Found:
    """A board that resets as its port opens (an Arduino, a CH340 board): opened once, with the framing of a card for
    its USB ID or a sketch's usual 9600 8N1. What it prints as it starts (READY) is read first and is not who it is,
    nor a refusal of the query (ERR): such a board is reported as there, without an identity, so first-run setup
    doesn't take it for an instrument named READY (l07, the orchestrator's labsim check)."""
    named = [c for c in registry if vid_pid in c.usb_ids and "baud_rate" in c.connection]
    baud, term = DEFAULT_SERIAL[0]
    settings = _settings_of(named[0]) if named else {
        "baud_rate": baud, "data_bits": 8, "parity": "none", "stop_bits": 1.0, "read_termination": term,
        "write_termination": term, "query": "*IDN?"}
    said: List[str] = []
    try:
        reply = _ask(rm, found.address, settings["query"], max(timeout_ms, 1000), settings, BOOT_S, said)
    except Exception:
        reply = ""
    said = [s for s in said if s]
    if said or reply:   # it talks at this framing: kept, for the card a person writes for it
        found.settings = _framing(settings)
        found.serial = {k: v for k, v in settings.items() if k != "query"}
    if reply:
        found.idn = reply
        card = match_card(reply, registry)
        if card:
            found.card_id, found.model, found.kind, found.how = card.id, card.model, card.kind, "identity reply"
            return found
        found.how = "no card matches the reply"
        return look_up(found, catalog)
    cards = [c for c in registry if vid_pid in c.usb_ids]
    if cards:
        card = cards[0]
        found.card_id, found.model, found.kind, found.how = card.id, card.model, card.kind, "USB ID %s" % vid_pid
        return found
    found.how = ("an Arduino-type board (USB %s): %s and didn't answer %s; give Kelvin its commands to make a card"
                 % (vid_pid, "it printed %s as it started" % " / ".join(said[:2]) if said else
                    "it printed nothing as it started", settings["query"]))
    return found


def scan(registry: CardRegistry, rm: Any = None, in_lab: Optional[Dict[str, str]] = None,
         timeout_ms: int = 800, network: bool = True, catalog: Any = None) -> List[Found]:
    """Every instrument VISA can see, identified. `in_lab` maps addresses already used by the lab to their names;
    those are not queried (they may be busy), only listed. `catalog`: the instrument collection, if it is on."""
    return scan_survey(registry, rm, in_lab, timeout_ms, network, catalog)[0]


def scan_survey(registry: CardRegistry, rm: Any = None, in_lab: Optional[Dict[str, str]] = None,
                timeout_ms: int = 800, network: bool = True, catalog: Any = None,
                **survey_options: Any) -> Tuple[List[Found], Any]:
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
        out.append(identify(name, registry, rm, timeout_ms, usb, catalog=catalog))
    from .drivers import MODULES, discover_libraries, discover_swabian, discover_zhinst, library_notes
    # instruments behind vendor software: Zurich Instruments (LabOne), Swabian Pulse Streamers and Time Taggers,
    # NI DAQ cards, Ocean spectrometers, Andor and Basler cameras, Andor spectrographs, attocube ANC350s
    for address, idn in discover_zhinst(MODULES.get("zhinst")) + discover_swabian() + discover_libraries():
        if address in in_lab:
            out.append(Found(address, how="already in this lab", in_lab=in_lab[address]))
            continue
        card = match_card(idn, registry)
        found = Found(address, idn, card.id if card else None, card.model if card else "", card.kind if card else "",
                      "found by its maker's library" if card else "no card for this model yet")
        out.append(found if card else look_up(found, catalog))
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


_RATED = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(mW|W)(?![A-Za-z])")


def rated_limits(card: Card, idn: str) -> Dict[str, Tuple[float, float]]:
    """A laser's own power rating, where its identity reply names it (Coherent's OBIS: '... 520nm LX 20mW ...'), as its
    power drives' limit in place of the card's, which covers the whole series (100 mW): at a first start Kelvin
    proposed 0.1 W for a 20 mW laser (l05, l06)."""
    m = _RATED.search(idn or "") if card.kind in ("laser", "laser-driver") else None
    if not m:
        return {}
    watts = float(m.group(1)) * (1e-3 if m.group(2) == "mW" else 1.0)
    return {p.name: (p.limits[0], watts) for p in card.drives
            if p.numeric and p.limits and p.unit == "W" and p.limits[0] < watts < p.limits[1]}


def instrument_block(item: Dict[str, Any], registry: CardRegistry) -> List[str]:
    """One instrument entry of a lab config, with its drives' vendor limits to tighten. Optional: connection and
    vars (what the person or the manual says), added_by: kelvin (Kelvin added it; no person has reviewed it)."""
    card = registry.get(item["card"])
    note = item.get("note") or card.model.split(" (")[0]
    lines = ["  %s:" % item["name"], "    card: %s        # %s" % (card.id, note), "    address: %s" % json.dumps(item["address"])]
    used = {p: v for p, v in (item.get("limits") or {}).items() if p in card.params}   # what the lab's scripts use
    rated = {} if used else rated_limits(card, str(item.get("idn") or item.get("note") or ""))
    lim = ["%s: [%g, %g]" % (p.name, p.limits[0], p.limits[1]) for p in card.drives
           if p.numeric and p.limits and p.name not in used and p.name not in rated]
    if used:
        lines.append("    limits: { %s }   # from your scripts (the values they use): check them, and tighten the "
                     "rest" % ", ".join(["%s: [%g, %g]" % (p, v[0], v[1]) for p, v in used.items()] + lim))
    elif rated:
        lines.append("    limits: { %s }   # its own rating, from its identity reply: tighten to what your sample "
                     "can take" % ", ".join(["%s: [%g, %g]" % (p, v[0], v[1]) for p, v in rated.items()] + lim))
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


def _instrument_lines(lines: List[str], name: str) -> Tuple[int, int]:
    """Where one instrument's entry is in the lab config's lines: its name's line, and the line after its block."""
    start = next((i for i, l in enumerate(lines) if re.match(r"^instruments\s*:", l)), len(lines))
    at = next((i for i in range(start + 1, len(lines))
               if re.match(r"^(\s+)%s\s*:" % re.escape(name), lines[i])), None)
    if at is None:
        raise KeyError("no instrument %r in the lab config" % name)
    indent = len(lines[at]) - len(lines[at].lstrip())
    end = at + 1
    while end < len(lines) and (not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > indent):
        end += 1
    while end > at + 1 and not lines[end - 1].strip():
        end -= 1
    return at, end


def instrument_text(text: str, name: str) -> str:
    """One instrument's entry in the lab config text, as written (its comments too). KeyError: none by that name."""
    lines = text.split("\n")
    at, end = _instrument_lines(lines, name)
    return "\n".join(lines[at:end])


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
    at, end = _instrument_lines(lines, name)
    indent = len(lines[at]) - len(lines[at].lstrip())
    head, _, rest = lines[at].partition(":")
    if rest.strip() and not rest.strip().startswith("#"):   # written on one line: name: { card: ..., ... }
        entry = dict(entry, limits=merged) if merged else {k: v for k, v in entry.items() if k != "limits"}
        if reviewed:
            entry.pop("added_by", None)
        lines[at] = "%s: %s" % (head, _flow(entry))
        return "\n".join(lines)
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
    from .homelab import builtin_registry
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
