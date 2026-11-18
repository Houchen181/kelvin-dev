"""A card a person reviewed, sent back to Kelvin's public instrument collection, so that the next lab with the same
instrument doesn't draft it again.

Kelvin needs no GitHub token for it. It opens the collection's "Send a card from Kelvin" issue form in the person's
browser, filled in, and they submit it from their own GitHub account, or don't: nothing leaves the computer until they
do. A workflow in the collection turns the issue into a pull request its maintainer reviews (its tools/kic.py
from-issue), and comments on the issue with what to fix when the card can't go in.

- Which cards: one a person reviewed (community or lab-verified) with something new in it: the lab's own card, or its
  version of one of Kelvin's cards or of the collection's. A draft waits for a person's review, and Kelvin's and the
  collection's cards as they are (a review aside) have nothing new to send.
- What goes: the card as the lab keeps it, and what the collection files it by (maker, model, kind, interfaces; its id
  in the collection when the collection knows the model; an identity reply and USB IDs Kelvin saw; the firmware), with
  a summary of the check on hardware from the lab's own record for the person to edit. Nothing private: the lab's
  addresses, serial numbers, IP and MAC addresses, host names, file paths, e-mail addresses and the names of people and
  of the lab are taken out, of the card too, and the preview says what was. A credit is the person's to type in the
  form; Kelvin never fills it in.
- Asked every time: the agent's contribute_card shows a person exactly what goes and where on an approval card, and
  only their yes opens the form, never Kelvin's own yes nor auto mode's. The portal's Send to the collection shows the
  same, and is the lab owner's."""
from __future__ import annotations

import getpass
import hashlib
import html
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from . import carddraft
from .cards import Card
from .collection import REPO, entry_for
from .profiles import parse_idn, real_serial
from .usbids import is_generic
from .yamlio import load_yaml

NEW_ISSUE = "https://github.com/%s/issues/new"
FORM = "new-card.yml"              # the collection's issue form (.github/ISSUE_TEMPLATE/new-card.yml)
VERIFICATION = "verification.yml"  # its form for a check of a card it has, on hardware
# the form's fields, by the ids Kelvin fills it in with, in its order: the collection's form has the same ids (its
# tests say so), and its from-issue reads the issue back by their labels
FIELDS = ("instrument", "maker", "model", "kind", "interfaces", "idn", "usb", "firmware", "checked", "credit", "card")
LABELS = {"instrument": "Its id in the collection", "maker": "Maker", "model": "Model", "kind": "Kind",
          "interfaces": "Interfaces", "idn": "Its identity reply", "usb": "USB IDs", "firmware": "Firmware",
          "checked": "What was checked on hardware", "credit": "Credit", "card": "The card"}
# GitHub's own limits on the new-issue page, probed on 2026-11-17 without signing in: it answers a URL of up to about
# 6.4 KB, then 500 (and 414 past about 8.2 KB). A person who isn't signed in is sent on to its sign-in page with the
# whole URL again in return_to, percent-encoded once more, and that page failed past about 6.4 KB too (6423 characters
# loaded, 6655 did not). So the card goes into the link only while that sign-in URL stays within MOST; else it is
# copied, for the person to paste.
SIGNIN = "https://github.com/login?return_to="
MOST = 6000
# Windows hands a link to the browser through ShellExecute, which may cut one at 2083 characters
# (INTERNET_MAX_URL_LENGTH): a longer one is opened through a small page on this computer that goes on to it
SHELL_MOST = 2000
SUMMARY_MOST = 1500   # characters of the record's summary in the link, at most

NOT_REVIEWED = ("%s is a draft: a person reviews a card before it goes to the public collection (Lab, Cards…, Mark "
                "reviewed, once its commands, limits and safe values are checked against the manual and the "
                "instrument), and then it can be sent")
NOTHING_NEW = ("%s is %s as it is, so there is nothing new in it to send. Your lab's changes to it go as your version "
               "of it (Lab, Cards…, Make your version, then review it); to report that you checked it on hardware, "
               "use the collection's Hardware verification form: %s")
PRIVATE_IN_CARD = ("%s holds what stays in this lab (%s), and with that taken out the card doesn't work (%s). Take it "
                   "out yourself (Lab, Cards…, Edit), then send the card again")

# what stays in the lab, as a placeholder in what goes, and in words for the preview
ADDRESS, SERIAL, HOST, NAME, LAB, PATH = "address", "serial number", "host name", "name", "lab", "file path"
IP, MAC, EMAIL = "IP address", "MAC address", "e-mail address"
SAID = {ADDRESS: "an address of the lab's", SERIAL: "a serial number", HOST: "this computer's name",
        NAME: "a person's name", LAB: "the lab's name", PATH: "a file path", IP: "an IP address",
        MAC: "a MAC address", EMAIL: "an e-mail address"}
# words too common to take out wherever they stand, should a person or a lab be called so
COMMON = {"lab", "my lab", "user", "users", "admin", "administrator", "guest", "test", "kelvin", "owner", "default",
          "public", "root", "home", "localhost", "instruments"}
PATH_END = r"[^\s\"'<>|,;\]\}]*"
# what looks private wherever it stands: (pattern, what it is, whether a match may stay). Taken out in two turns around
# the lab's own values: e-mail addresses and paths before them (a name taken out first left an e-mail's domain), IP and
# MAC addresses after (an IP address taken out first left the rest of the lab's address it is in)
LOOKS_PRIVATE_FIRST: List[Tuple["re.Pattern[str]", str, Optional[Callable[[str], bool]]]] = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"), EMAIL, None),
    # a folder on a Windows drive, but for the makers' own (Program Files, ProgramData) and Windows itself, which the
    # makers' notes name for where their libraries go
    (re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]+" + PATH_END), PATH,
     lambda m: re.match(r"^[A-Za-z]:[\\/]+(program|windows\b)", m, re.I) is not None),
    # a server's share (\\server\share, its backslashes doubled in YAML's quotes), never a drive's folder (C:\\Program)
    (re.compile(r"(?<![\w\\:])\\{2,4}[A-Za-z0-9][\w.$-]*" + PATH_END), PATH, None),
    (re.compile(r"(?<![\w.~])/(?:home|Users|mnt|media|Volumes|net|srv|root)/" + PATH_END), PATH, None),
]
LOOKS_PRIVATE_LAST: List[Tuple["re.Pattern[str]", str, Optional[Callable[[str], bool]]]] = [
    (re.compile(r"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])"), MAC, None),
    (re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?!\.?\d)"),
     IP, None),
]


@dataclass
class Contribution:
    """One card, ready for the collection: what goes (the card and the form's other fields) and the link that opens the
    form with them."""
    card_id: str
    model: str
    origin: str                  # whose card it is, in words
    yaml: str                    # the card as it goes: as the lab keeps it, with what is private taken out
    fields: Dict[str, str]       # the form's other fields by their ids; credit stays empty, the person's to fill in
    title: str
    url: str                     # the form, filled in
    copy: bool                   # too long for the link: the card goes by the clipboard, for the person to paste
    removed: List[str] = field(default_factory=list)   # what was taken out as private, in words
    repository: str = REPO


# ------------------------------------------------------------------ which card

def reviewed_card(ref: str, cards_dir: Optional[Path], lab: Any = None,
                  collection: Any = None) -> Tuple[Card, str, str]:
    """(the card, its text as the lab keeps it, whose it is in words) for a card a person reviewed that has something
    new for the collection: by its id, or the name of the lab's instrument that uses it. ValueError says why it can't
    go (a draft, a broken card, one of Kelvin's or the collection's cards as they are); KeyError: there is no such
    card."""
    from .homelab import builtin_ids, builtin_registry
    ref = str(ref or "").strip()
    inst = (getattr(lab, "instruments", None) or {}).get(ref) if lab is not None else None
    card_id = inst.card.id if inst is not None else ref
    path = Path(cards_dir) / carddraft.file_for(card_id) if cards_dir and card_id else None
    if path is None or not path.is_file():
        if card_id in builtin_ids() or collection_text(collection, card_id) is not None:
            whose = "Kelvin's own card" if card_id in builtin_ids() else "the collection's card"
            raise ValueError(NOTHING_NEW % (card_id, whose, verification_url()))
        raise KeyError("no card %r among this lab's cards (Lab, Cards… lists them)" % ref)
    text = path.read_text(encoding="utf-8")
    card, problems, _ = carddraft.check(text, path.name)
    if card is None:
        raise ValueError("%s has problems to fix first (Lab, Cards…, Edit): %s" % (card_id, "; ".join(problems[:3])))
    if card.status == "draft":
        raise ValueError(NOT_REVIEWED % card.id)
    if copied_by_kic(path):
        raise ValueError(NOTHING_NEW % (card.id, "the collection's card", verification_url()))
    origin = "this lab's own card"
    if card.id in builtin_ids():
        theirs = Path(builtin_registry().get(card.id).source).read_text(encoding="utf-8")
        if same_card(text, theirs):
            raise ValueError(NOTHING_NEW % (card.id, "Kelvin's own card", verification_url()))
        origin = "this lab's version of Kelvin's card"
    theirs = collection_text(collection, card.id)
    if theirs is not None:
        if same_card(text, theirs):
            raise ValueError(NOTHING_NEW % (card.id, "the collection's card", verification_url()))
        origin = "this lab's version of the collection's card"
    return card, text, origin


def same_card(a: str, b: str) -> bool:
    """Whether two cards say the same, a person's review of them (their status) aside."""
    try:
        x, y = load_yaml(a), load_yaml(b)
    except Exception:
        return False
    return isinstance(x, dict) and isinstance(y, dict) and \
        {k: v for k, v in x.items() if k != "status"} == {k: v for k, v in y.items() if k != "status"}


def collection_text(collection: Any, card_id: str) -> Optional[str]:
    """The collection's card of this id as its copy on this computer has it; None without one."""
    if collection is None:
        return None
    try:
        entry = collection.instrument(card_id)
        card = entry.get("card") if isinstance(entry, dict) else None
        data = collection.read(str(card.get("path")), str(card.get("sha256"))) if isinstance(card, dict) else None
    except Exception:   # a copy being replaced, or none: Kelvin's own check is the one that counts then
        return None
    return data.decode("utf-8", errors="replace") if data is not None else None


def copied_by_kic(path: Path) -> bool:
    """A card the collection's `kic install` copied into the cards folder, and nobody has changed since."""
    from .homelab import INSTALLED
    try:
        copied = json.loads((path.parent / INSTALLED).read_text(encoding="utf-8")).get("installed") or {}
        return copied.get(path.name) == hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError, AttributeError):
        return False


def verification_url(repository: str = REPO) -> str:
    return (NEW_ISSUE % repository) + "?template=" + VERIFICATION


# ------------------------------------------------------------------ what the lab knows of the instrument

def users(lab: Any, card_id: str) -> List[Dict[str, Any]]:
    """The lab's instruments on this card: {name, address, real}, real when it is on hardware now, not simulated."""
    out = []
    for name, ic in (getattr(getattr(lab, "config", None), "instruments", None) or {}).items():
        if getattr(ic, "card", None) != card_id:
            continue
        address = str(getattr(ic, "address", "") or "")
        inst = (getattr(lab, "instruments", None) or {}).get(name)
        real = bool(address) and not address.lower().startswith("sim://") and not getattr(inst, "simulated", False)
        out.append({"name": name, "address": address, "real": real})
    return out


def serial_port(address: str) -> Optional[str]:
    """The serial port (as VISA names it: ASRL3::INSTR) an address is the instrument's own, or None: a GPIB adapter's
    port (prologix://) or a bus several instruments share (modbus://) is not the instrument's."""
    from .scriptrun import canonical
    a = str(address or "").strip()
    low = a.lower()
    if low.startswith(("prologix://", "modbus://")):
        return None
    if low.startswith("lib://"):   # a maker's library talking to the instrument's own port
        a = a[len("lib://"):].split("?", 1)[0]
    c = canonical(a)
    return c if c.startswith("ASRL") else None


def usb_id(address: str, ports: Dict[str, str]) -> Optional[str]:
    """The USB ID (vid:pid in hex) of the device at a lab address: from a USB VISA address itself
    (USB0::0x1313::0x8078::...), or, for a USB serial port, from what this computer says of the port (ports: serial
    resource -> vid:pid)."""
    from .scriptrun import canonical
    m = re.match(r"^USB\d*::0X([0-9A-F]{1,4})::0X([0-9A-F]{1,4})::", canonical(address))
    if m:
        return "%04x:%04x" % (int(m.group(1), 16), int(m.group(2), 16))
    port = serial_port(address)
    for resource, vid_pid in ports.items() if port else ():
        if str(resource).upper() == port.upper():
            return str(vid_pid).lower()
    return None


def interface_of(address: str, vid_pid: Optional[str]) -> Optional[str]:
    """How an instrument at this address connects, as the collection names it (collection.yaml's interfaces); None
    when the address doesn't say."""
    from .scriptrun import canonical
    low, c = str(address or "").strip().lower(), canonical(address)
    if c.startswith("GPIB") or low.startswith("prologix://"):
        return "gpib"
    if c.startswith("USB"):
        return "usb-tmc"
    if serial_port(address):   # a USB device's own port (a maker's ID), else RS-232 (or a generic USB-serial cable)
        return "usb" if vid_pid and not is_generic(vid_pid) else "rs-232"
    if low.startswith(("http://", "https://")):
        return "web-api"
    if low.startswith("modbus://"):
        return "rs-485" if re.match(r"^modbus://(com\d|/dev/)", low) else "lan"
    if c.startswith("TCPIP") or low.startswith(("zmq://", "jsonrpc://")):
        return "lan"
    if low.startswith("uvc://"):
        return "usb"
    host = urllib.parse.urlsplit(low).hostname or "" if "://" in low else ""
    return "lan" if re.match(r"^(\d{1,3}\.){3}\d{1,3}$|^[a-z0-9-]+(\.[a-z0-9-]+)+$", host) else None


def masked(reply: str) -> str:
    """An identity reply with its serial number taken out (the third of maker, model, serial number, firmware)."""
    serial = real_serial(parse_idn(reply).get("serial", ""))
    if not serial or not serial.strip("0"):
        return reply
    return re.sub(r"(?<![\w/])%s(?![\w])" % re.escape(serial), "<%s>" % SERIAL, reply)


def model_of(card: Card, idn: Dict[str, str]) -> str:
    """A model name as the collection writes one (SR844, PEM-200): from the identity reply, else the card's model
    without its maker's words, else its id."""
    if idn.get("model"):
        return re.sub(r"^model\s+", "", idn["model"], flags=re.I).strip()
    maker = {w.lower() for w in re.findall(r"[\w-]+", card.vendor or "")}
    words = [w for w in card.model.split()]
    while words and words[0].lower().strip(",.") in maker:
        words.pop(0)
    return words[0] if words else card.id.split("/")[-1].upper()


def collection_entry(card: Card, replies: Sequence[str], index: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The collection's entry for the card's instrument, when the collection knows the model: under the card's id, by
    an identity reply Kelvin saw (or the card's own example), or by the card's model as the collection names it."""
    entries = [e for e in index.get("instruments") or [] if isinstance(e, dict)]
    by_id = next((e for e in entries if e.get("id") == card.id), None)
    if by_id is not None:
        return by_id
    for reply in list(replies) + ([str(card.idn_example)] if card.idn_example else []):
        entry = entry_for(index, reply)
        if entry is not None:
            return entry
    plain = re.sub(r"[^a-z0-9]", "", card.model.lower())
    return next((e for e in entries if plain and re.sub(r"[^a-z0-9]", "", str(e.get("name") or "").lower()) == plain),
                None)


# ------------------------------------------------------------------ the check on hardware, from the lab's record

RECORD_TYPES = ("command", "card_try", "instrument_added", "instrument_changed")


def hardware_summary(events: Iterable[Dict[str, Any]], card: Card, names: Sequence[str]) -> str:
    """What the lab's own record says of this card on hardware, for the person to edit: its tries on a real address
    (Try it, try_card), and the parameters read and set on the lab's real instruments on it (names), with the values
    set. An instrument counts from the record's last word on where it is (added or changed: on this card, at a real
    address) and, with no such word, throughout; never while the record had it simulated or on another card. "" when
    the record has none of it."""
    names = set(names)
    state: Dict[str, bool] = {}     # an instrument's latest place in the record: on this card, on hardware
    held: Dict[str, List[Tuple[str, str, Any, str]]] = {}   # its commands before the record said where it was
    done: List[Tuple[str, str, Any, str]] = []               # (read or set, parameter, value, day) that count
    tries: List[Dict[str, Any]] = []
    for e in events:
        kind, day = e.get("type"), str(e.get("wall") or "")[:10]
        if kind in ("instrument_added", "instrument_changed") and e.get("instrument") in names:
            held.pop(e["instrument"], None)   # what came before was another card's, or another address's
            state[e["instrument"]] = e.get("card") == card.id and \
                not str(e.get("address") or "").lower().startswith("sim://")
        elif kind == "card_try":
            if e.get("card") == card.id and not str(e.get("address") or "").lower().startswith("sim://"):
                tries.append(e)
                done.append(("try", "", None, day))
        elif kind == "command" and e.get("ok"):
            instr, _, param = str(e.get("target") or "").partition(".")
            if instr not in names or param not in card.params:
                continue
            if e.get("method") == "get" and e.get("action") == "read":
                item = ("read", param, None, day)
            elif e.get("method") in ("set", "safe") and e.get("action") == "set":
                item = ("set", param, e.get("value"), day)
            else:
                continue
            if instr not in state:
                held.setdefault(instr, []).append(item)
            elif state[instr]:
                done.append(item)
    done += [item for items in held.values() for item in items]   # never placed by the record: as the lab has it now
    reads: Dict[str, int] = {}
    sets: Dict[str, List[Any]] = {}
    for what, param, value, _ in done:
        if what == "read":
            reads[param] = reads.get(param, 0) + 1
        elif what == "set":
            sets.setdefault(param, []).append(value)
    days = [day for _, _, _, day in done]
    lines = []
    if tries:
        lines.append("- Tried with Kelvin's Try it %s on the instrument; the last time %s." % (
            "once" if len(tries) == 1 else "%d times" % len(tries), tried(tries[-1])))
    if reads:
        lines.append("- Read on the instrument: %s (%d reads)." % (", ".join(sorted(reads)), sum(reads.values())))
    if sets:
        lines.append("- Set on the instrument: %s." % ", ".join(
            "%s (%s)" % (name, values_set(card.params[name], values)) for name, values in sorted(sets.items())))
    days = sorted(d for d in days if d)
    if not lines:
        return ""
    span = days[0] if days[0] == days[-1] else "%s to %s" % (days[0], days[-1])
    return "From this lab's Kelvin record, %s:\n%s" % (span, "\n".join(lines))


def tried(e: Dict[str, Any]) -> str:
    """One try of the card, in words, from its line in the record."""
    bits = ["%s reading%s answered" % (e.get("answered", 0), "" if e.get("answered") == 1 else "s")]
    if e.get("matches") is True:
        bits.insert(0, "it answered as the card expects")
    elif e.get("matches") is False:
        bits.insert(0, "its identity did NOT match the card's pattern")
    if e.get("failed"):
        bits.append("%d failed" % e["failed"])
    if e.get("not_tried"):
        bits.append("%d not tried" % e["not_tried"])
    if e.get("set_check") is not None:
        bits.append("its set command %s" % ("checked" if e["set_check"] else "FAILED"))
    return ", ".join(bits)


def values_set(spec: Any, values: Sequence[Any]) -> str:
    """The values a parameter was set to, in words: a range for a number, each value else (a few)."""
    numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if spec.numeric and numbers:
        lo, hi = min(numbers), max(numbers)
        return spec.show(lo) if lo == hi else "%s to %s" % (spec.show(lo), spec.show(hi))
    shown = list(dict.fromkeys(spec.show(v) for v in values))
    return ", ".join(shown[:4]) + (", …" if len(shown) > 4 else "")


def lab_record(rt: Any) -> Iterator[Dict[str, Any]]:
    """The lab's record as the summary reads it: its instrument log (everyone's) where there is one, else this person's
    record. Never what unshared work wrote (a confidential or unpublished project's, an incognito chat's): what goes to
    the collection is public."""
    shared = Path(rt.shared_log) if getattr(rt, "shared_log", None) else None
    path = shared if shared is not None and shared.exists() else Path(rt.events_path)
    marks = tuple('"type": "%s"' % t for t in RECORD_TYPES)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not any(m in line for m in marks):   # most lines: settled without reading them as JSON
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("type") in RECORD_TYPES and not rt.unshared_work(e.get("whose")):
                    yield e
    except OSError:
        return


# ------------------------------------------------------------------ what stays in the lab

def address_parts(address: str) -> List[Tuple[str, str]]:
    """A lab address and what in it says where the lab is: the whole, as written and as VISA writes it, its host (an
    IP address or a name), its serial port, a USB instrument's serial number, and the folders a lib:// address names."""
    from .scriptrun import canonical
    a = str(address or "").strip()
    if not a or a.lower().startswith("sim://"):
        return []
    out = [(a, ADDRESS)]
    c = canonical(a)
    if c and c.upper() != a.upper():
        out.append((c, ADDRESS))
    m = re.match(r"^TCPIP\d*::(\[[^\]]+\]|[^:]+)::", c)
    if m:
        out.append((m.group(1).strip("[]"), ADDRESS))
    m = re.match(r"^USB\d*::[^:]+::[^:]+::([^:]+)", c)
    if m:
        out.append((m.group(1), SERIAL))
    m = re.match(r"^ASRL(\d+)::INSTR$", c)
    if m:
        out.append(("COM" + m.group(1), ADDRESS))
    if "://" in a:
        parts = urllib.parse.urlsplit(a)
        if parts.hostname:   # its host, IP address or port: lib://COM4, http://10.0.0.5:8080
            out.append((parts.hostname, ADDRESS))
        for key, value in urllib.parse.parse_qsl(parts.query):
            if key in ("python", "path"):
                out.append((value, PATH))
    return out


def instrument_profile(home: Any, name: str) -> Dict[str, Any]:
    """What Kelvin keeps of a lab instrument in its folder (profiles.py: its identity reply, the serial number it was
    set up with); {} without one."""
    from .profiles import folder
    try:
        data = json.loads((folder(home, name) / "instrument.json").read_text(encoding="utf-8")) if home else {}
    except (KeyError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def private_values(rt: Any, serials: Sequence[str] = ()) -> List[Tuple[str, str]]:
    """What stays in this lab, as (value, what it is): its instruments' addresses (and the hosts, ports and USB serial
    numbers in them) and those the last scan found, the serial numbers its instruments answered with, this computer's
    name, the names of the person signed in, of the computer's user and of the lab, and Kelvin's folders."""
    out: List[Tuple[str, str]] = [(s, SERIAL) for s in serials]
    found = getattr(rt, "found_info", None) or {}
    lab = getattr(rt, "lab", None)
    instruments = getattr(getattr(lab, "config", None), "instruments", None) or {}
    for a in [str(getattr(ic, "address", "") or "") for ic in instruments.values()] + \
            [str((v or {}).get("address") or k) for k, v in found.items()]:
        out += address_parts(a)
    replies = [str((v or {}).get("idn") or "") for v in found.values()]
    for name in instruments:
        profile = instrument_profile(getattr(rt, "home", None), name)
        replies.append(str(profile.get("identity") or ""))
        kept = profile.get("serial_number")
        if isinstance(kept, dict):
            out.append((str(kept.get("serial") or ""), SERIAL))
    out += [(real_serial(parse_idn(r).get("serial", "")), SERIAL) for r in replies if r]
    out += [(h, HOST) for h in (socket.gethostname(), platform.node())]
    try:
        user = getpass.getuser()
    except Exception:   # no user name in this process's environment
        user = ""
    out += [(str(n or ""), NAME) for n in (getattr(rt, "user", None), user, Path.home().name)]
    lab_name = str(getattr(getattr(lab, "config", None), "name", "") or "")
    out.append((lab_name, LAB))
    for folder in (Path.home(), getattr(rt, "home", None), getattr(rt, "data_dir", None)):
        if folder:
            out += [(str(folder), PATH), (str(folder).replace("\\", "/"), PATH)]
    return [(v.strip(), w) for v, w in out
            if len(v.strip()) >= (5 if w == SERIAL else 3) and v.strip().lower() not in COMMON]


def scrub(text: str, private: Sequence[Tuple[str, str]], counts: Dict[str, int]) -> str:
    """The text with what stays in the lab taken out: the lab's own values (private: (value, what it is)), and what
    looks private wherever it stands (e-mail, MAC and IP addresses, file paths but the makers' own). Each is replaced by
    <what it is>, and counted. The lab's own folders go first, whole (a home folder's name may have a space in it)."""
    def put(what: str) -> Callable[[Any], str]:
        def out(m: Any) -> str:
            counts[what] = counts.get(what, 0) + 1
            return "<%s>" % what
        return out

    def known(values: Iterable[Tuple[str, str]]) -> None:
        nonlocal text
        for value, what in sorted(set(values), key=lambda p: -len(p[0])):   # the longest first: an address, its host
            rest = PATH_END if what == PATH else ""
            text = re.sub(r"(?<![A-Za-z0-9])%s%s(?![A-Za-z0-9])" % (re.escape(value), rest), put(what), text,
                          flags=re.I)

    def looks(patterns: Sequence[Tuple[Any, str, Optional[Callable[[str], bool]]]]) -> None:
        nonlocal text
        for pattern, what, stays in patterns:
            text = pattern.sub(lambda m: m.group(0) if stays and stays(m.group(0)) else put(what)(m), text)

    known(p for p in private if p[1] == PATH)
    looks(LOOKS_PRIVATE_FIRST)
    known(p for p in private if p[1] != PATH)
    looks(LOOKS_PRIVATE_LAST)
    return text


# ------------------------------------------------------------------ the contribution and its link

def prepare(card: Card, text: str, origin: str, instruments: Sequence[Dict[str, Any]], replies: Sequence[str],
            ports: Dict[str, str], events: Iterable[Dict[str, Any]], index: Dict[str, Any],
            private: Sequence[Tuple[str, str]], repository: str = REPO) -> Contribution:
    """The contribution of a card a person reviewed (reviewed_card), from what Kelvin knows of the lab: its instruments
    on hardware on this card ({name, address}), the identity replies it saw from them, the USB IDs of this computer's
    serial ports, the lab's record, the collection's index, and what stays in the lab (private: (value, what it is)).
    ValueError when taking out what is private leaves a card that doesn't work."""
    entry = collection_entry(card, replies, index) or {}
    idn = parse_idn(replies[0]) if replies else {}
    found = {u["address"]: usb_id(u["address"], ports) for u in instruments}
    interfaces = [i for i in dict.fromkeys(interface_of(a, v) for a, v in found.items()) if i] or \
        [str(i) for i in entry.get("interfaces") or []]
    usb = [v for v in dict.fromkeys(found.values()) if v and not is_generic(v) and v not in card.usb_ids]
    fields = {"instrument": str(entry.get("id") or ""),
              "maker": str(entry.get("maker") or card.vendor or idn.get("maker") or card.id.split("/")[0]),
              "model": str(entry.get("model") or model_of(card, idn)),
              "kind": str(entry.get("kind") or card.kind),
              "interfaces": ", ".join(interfaces),
              "idn": masked(replies[0]) if replies else "",
              "usb": ", ".join(usb),
              "firmware": str(idn.get("firmware") or ""),
              "checked": hardware_summary(events, card, [u["name"] for u in instruments])}
    counts: Dict[str, int] = {}
    fields = {k: scrub(v, private, counts) for k, v in fields.items()}
    kept = scrub(text, private, counts)
    removed = ["%s%s" % (SAID[w], " (%d)" % n if n > 1 else "") for w, n in sorted(counts.items())]
    still, problems, _ = carddraft.check(kept)
    if still is None or still.id != card.id:
        raise ValueError(PRIVATE_IN_CARD % (card.id, ", ".join(removed), "; ".join(problems[:2]) or
                                            "its id would change"))
    title = "Card: %s %s" % (fields["maker"], fields["model"])
    url, copy, fields = form_url(fields, kept, title, repository)
    return Contribution(card.id, card.model, origin, kept, fields, title, url, copy, removed, repository)


def form_url(fields: Dict[str, str], card: str, title: str,
             repository: str = REPO) -> Tuple[str, bool, Dict[str, str]]:
    """The issue form's address with its fields filled in, whether the card is left out of it (copied instead), and the
    fields as the address has them. The card goes in only while GitHub takes the whole address, its sign-in page
    included (fits); a summary too long even without the card is cut at a line."""
    shown = dict(fields, checked=cut(fields.get("checked") or "", SUMMARY_MOST))
    url = link(shown, title, repository, card)
    if fits(url):
        return url, False, shown
    url = link(shown, title, repository)
    while not fits(url) and shown["checked"]:
        shown["checked"] = cut(shown["checked"], len(shown["checked"]) - 1)
        url = link(shown, title, repository)
    return url, True, shown


def link(fields: Dict[str, str], title: str, repository: str, card: str = "") -> str:
    query = [("template", FORM), ("title", title)] + [(k, fields[k]) for k in FIELDS if fields.get(k)]
    if card:
        query.append(("card", card))
    return (NEW_ISSUE % repository) + "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)


def fits(url: str) -> bool:
    """Whether GitHub takes this address, and its sign-in page the address with it (SIGNIN, MOST)."""
    return len(SIGNIN) + len(urllib.parse.quote(url, safe="")) <= MOST


def cut(text: str, most: int) -> str:
    """The text cut at its last line end within `most` characters (a line cut at a word when there is none)."""
    if len(text) <= most:
        return text
    head = text[:max(0, most - 2)]
    at = head.rfind("\n")
    if at > 0:
        return head[:at] + "\n…"
    at = head.rfind(" ")
    return (head[:at] if at > 0 else head) + " …" if most > 2 else ""


def question(c: Contribution) -> str:
    return "Send your card %s (%s) to Kelvin's public instrument collection?" % (c.card_id, c.model)


def preview(c: Contribution) -> List[str]:
    """What the person sees before anything goes, on the agent's approval card and in the portal: where it goes, each
    field of the form, and the card itself."""
    items = ["Where it goes: a public issue in github.com/%s, the repository of Kelvin's instrument collection, under "
             "your own GitHub account. Kelvin opens its form in your browser, filled in; nothing is sent until you "
             "submit it there, and you can change any field first. The collection's maintainer reviews it before it "
             "becomes part of the collection." % c.repository,
             "The card: %s, %s." % (c.card_id, c.origin)]
    for fid in FIELDS:
        if fid not in ("card", "credit"):
            items.append("%s: %s" % (LABELS[fid], c.fields.get(fid) or "(empty)"))
    items.append("Credit: left empty, for you to fill in on GitHub if you want your lab or name shown.")
    if c.removed:
        items.append("Taken out, as it stays in this lab: %s." % ", ".join(c.removed))
    items.append("The card is too long for the link: Kelvin copies it, and you paste it into the form's card field "
                 "(Ctrl+V)." if c.copy else "The card goes in the link, in the form's card field.")
    items.append("The card, as it goes:\n" + c.yaml.rstrip("\n"))
    return items


# ------------------------------------------------------------------ handing it over: the browser and the clipboard

def hand_over(c: Contribution, opener: Optional[Callable[[str], Any]] = None,
              copier: Optional[Callable[[str], bool]] = None) -> Tuple[bool, bool]:
    """After a person's yes, on this computer: the card copied when it isn't in the link, then the form opened in the
    browser. (opened, copied)."""
    copied = bool((copier or copy_text)(c.yaml)) if c.copy else False
    return open_form(c.url, opener), copied


def open_form(url: str, opener: Optional[Callable[[str], Any]] = None, windows: Optional[bool] = None) -> bool:
    """Open the form in this computer's browser. On Windows a long link goes through a page on this computer that goes
    on to it (launcher): Windows hands links to the browser through ShellExecute, which may cut one at 2083
    characters."""
    target = url
    if (os.name == "nt" if windows is None else windows) and len(url) > SHELL_MOST:
        target = launcher(url).as_uri()
    try:
        return bool((opener or webbrowser.open)(target))
    except Exception:   # no browser here (a server without a desktop): said by the caller
        return False


def launcher(url: str) -> Path:
    """A page on this computer that goes on to `url` at once, with a link to it should it not: one file in the
    temporary folder, written again each time."""
    path = Path(tempfile.gettempdir()) / "kelvin-send-card.html"
    href = html.escape(url, quote=True)
    path.write_text('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Kelvin: send a card</title>'
                    '<meta http-equiv="refresh" content="0; url=%s"></head><body><p>Opening the collection\'s form on '
                    'GitHub… <a href="%s">Open it</a></p></body></html>\n' % (href, href), encoding="utf-8")
    return path


def copy_text(text: str) -> bool:
    """Put text on this computer's clipboard: Windows' own clipboard calls, pbcopy on a Mac, wl-copy, xclip or xsel
    elsewhere. False when none could."""
    try:
        if os.name == "nt":
            return _windows_copy(text)
        commands = [["pbcopy"]] if sys.platform == "darwin" else \
            [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
        for command in commands:
            if shutil.which(command[0]) and subprocess.run(
                    command, input=text.encode("utf-8"), capture_output=True, timeout=10,
                    env=dict(os.environ, LANG=os.environ.get("LANG") or "en_US.UTF-8")).returncode == 0:
                return True
    except Exception:   # no clipboard here: the person copies the card from Lab, Cards… instead
        return False
    return False


def _windows_copy(text: str) -> bool:
    """The text on the Windows clipboard as Unicode text, through a hidden window of its own: a clipboard opened with
    no window has no owner after EmptyClipboard, and SetClipboardData then fails (as pyperclip found)."""
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")   # their own function settings
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    data = (text.replace("\r\n", "\n").replace("\n", "\r\n") + "\0").encode("utf-16-le")
    window = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, None, None, None, None)
    if not window:
        return False
    try:
        for _ in range(20):   # another program may hold the clipboard for a moment
            if user32.OpenClipboard(window):
                break
            time.sleep(0.05)
        else:
            return False
        try:
            user32.EmptyClipboard()
            memory = kernel32.GlobalAlloc(0x0002, len(data))   # GMEM_MOVEABLE
            pointer = kernel32.GlobalLock(memory) if memory else None
            if not pointer:
                if memory:
                    kernel32.GlobalFree(memory)
                return False
            ctypes.memmove(pointer, data, len(data))
            kernel32.GlobalUnlock(memory)
            if not user32.SetClipboardData(13, memory):   # CF_UNICODETEXT; from here on the clipboard owns the memory
                kernel32.GlobalFree(memory)
                return False
            return True
        finally:
            user32.CloseClipboard()
    finally:
        user32.DestroyWindow(window)


def said(c: Contribution, opened: bool, copied: bool) -> str:
    """What the agent's tool answers once the person said yes."""
    if opened:
        head = "Opened the collection's form in the person's browser, filled in with the card %s" % c.card_id
    else:
        head = "Kelvin could not open a browser here; the form, filled in with the card %s, is at %s" % (
            c.card_id, c.url)
    lines = [head + (" (what was taken out as private: %s)." % ", ".join(c.removed) if c.removed else ".")]
    if c.copy:
        lines.append("The card is too long for the link: %s" % (
            "Kelvin copied it, and the person pastes it into the form's card field." if copied else
            "Kelvin could not copy it, so the person copies it from Lab, Cards…, Edit (or the portal's Send to the "
            "collection, which copies it) and pastes it into the form's card field."))
    lines.append("Nothing is sent until the person submits the form on GitHub, from their own account; the "
                 "collection's maintainer reviews it there.")
    return " ".join(lines)


# ------------------------------------------------------------------ from the lab Kelvin runs

def serial_ports() -> Dict[str, str]:
    """This computer's USB serial ports, by VISA resource (ASRL3::INSTR), with their USB IDs, where it says them."""
    try:
        from .discovery import _usb_ids
        return _usb_ids()
    except Exception:   # no pyserial, or the system won't say
        return {}


def identities(rt: Any, card_id: str, instruments: Sequence[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """(the identity replies Kelvin saw from the lab's instruments on this card, the serial numbers it knows of them):
    from each instrument's folder (profiles.py, as it was set up) and the last scan."""
    from .adapters import resource_name
    replies, serials = [], []
    found = getattr(rt, "found_info", None) or {}
    for u in instruments:
        profile = instrument_profile(getattr(rt, "home", None), u["name"])
        if profile.get("card") == card_id and profile.get("identity"):
            replies.append(str(profile["identity"]))
        kept = profile.get("serial_number")
        if isinstance(kept, dict) and real_serial(kept.get("serial")):
            serials.append(str(kept["serial"]))
        info = found.get(resource_name(u["address"])) or {}
        if info.get("idn"):
            replies.append(str(info["idn"]))
    serials += [s for s in (real_serial(parse_idn(r).get("serial", "")) for r in replies) if s]
    return list(dict.fromkeys(replies)), list(dict.fromkeys(serials))


def gather(rt: Any, ref: str) -> Contribution:
    """The contribution of one of this lab's cards (by its id, or the instrument that uses it), from what the runtime
    knows: its cards folder, its lab and its instruments' folders, the last scan, the collection's copy here and the
    lab's record. ValueError or KeyError says why that card can't go."""
    card, text, origin = reviewed_card(ref, rt.cards_dir, rt.lab, rt.collection)
    real = [u for u in users(rt.lab, card.id) if u["real"]]
    replies, serials = identities(rt, card.id, real)
    ports = serial_ports() if any(serial_port(u["address"]) for u in real) else {}
    try:
        rt.gate.log.settle(1.0)   # the record's latest lines in its file
    except Exception:
        pass
    index = rt.collection.index() if rt.collection is not None else {}
    return prepare(card, text, origin, real, replies, ports, lab_record(rt), index, private_values(rt, serials))
