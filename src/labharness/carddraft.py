"""Cards for instruments Kelvin does not know yet: drafted from the manual or the maker's example scripts, checked,
tried on the instrument, kept in ~/Kelvin/cards.

A card Kelvin writes is always a draft. On real hardware a draft card's readings work, so it can be tried against
the instrument, but every setting and output change on it asks a person, saying the card is a draft, until a person
reviews the card and marks it lab-verified. Only a person can do that, and Kelvin never overwrites a card a person
has reviewed."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

from .cards import STATUSES, Card, CardError, parse_card

DATA = Path(__file__).parent / "data" / "cards"
EXAMPLE = DATA / "instruments" / "keithley_2400.yaml"
MAX_MANUAL = 60000

GUIDE = """# Instrument cards (YAML, schema version 1)
A card tells Kelvin how to talk to one instrument model and which parameters are safe to change.

Top level:
- card: 1; id: vendor/model in lowercase (e.g. acme/sm-2000); model: full name; vendor; kind (source-meter,
  lock-in, temperature-controller, magnet-supply, laser, preamplifier, multimeter, generator, ...);
  status: draft (always, for a card you write); manual: which manual and chapter it came from.
- identify: {idn: a regex matching the *IDN? reply, example: a real reply}. Add query: if the instrument does not
  answer *IDN?.
- error_query: the command that reads the error queue, if any (e.g. "SYSTem:ERRor?"). Replies starting with 0 ("0,No
  error", 0000) mean no error; for any other way of saying it, no_error: a regex for that reply (Newport's "1TE@").
- connection (only if the manual requires it): read_termination, write_termination, baud_rate, data_bits, parity
  (none, odd, even; or the manual's N, O, E), stop_bits, or framing: "8N1" for all three; timeout_ms, write_only (a
  listen-only instrument), set_reply (a regex for the reply every set command gets, while queries get only their
  answer: an Arduino's "^OK$", Oxford Mercury's ":VALID$"), ack (the line sent after every command, queries too, as
  Coherent OBIS sends OK after each answer), echo: true (each command is echoed as a line first), greeting_lines (lines
  the instrument sends by itself when a connection opens, e.g. 2 for the AMI 430 on its LAN port), enq: true (each
  command is answered ACK or NAK and the reply comes only on ENQ, as on Pfeiffer's gauge controllers; a get is then
  the bare mnemonic, e.g. "PR1", and a set the mnemonic with its parameters, e.g. "SEN,{value},0").
- parameters: one entry per quantity, keyed by a short name.
- display: {headline: <parameter>} for the reading shown large; raw: {blocked: [...], reset: [...]}: command headers
  that must never be sent (calibration, firmware, sensor curves, protection limits) and reset commands.

Each parameter:
- label; kind: measure (reading or a setting that does not drive the sample) or drive (anything that drives the
  sample harder: voltage, current, power, field, temperature, outputs); unit (SI: V, A, W, T, K, Hz, s ...).
- type: float (default), int, bool, enum (with values: {label: code}) or trace.
- A trace (an oscilloscope's waveform, a spectrum analyzer's sweep): its x-axis as axis: {start: <param>, stop:
  <param>} (a sweep's start and stop frequency), {origin: <param>, step: <param>} (a scope's first time and the time
  between points: XORigin, XINCrement) or {values: <trace param>}. Comma-separated replies, with or without the
  #<n><length> header, need nothing more; for a waveform sent as binary numbers (BYTE or WORD format), binary: int8,
  uint8, int16, uint16, int32, float32 or float64 (add "little" when the manual says the lowest byte comes first).
  When the numbers are digitizer levels, y: their scaling from raw and the card's parameters that read it, as the
  manual gives it, e.g. y: "(raw - yref) * yinc + yorig". The number of points is a setting of its own, never sent
  by a get; the reply's format is best sent first in the reading's get list (WAVeform:FORMat WORD, FORMat:DATA
  ASCii,0), so another program changing it cannot break the reading.
- A network analyzer's S-parameter (S21, S11): a trace in dB of its formatted data (log magnitude: one number per
  point), taken by one sweep in the reading: get: [the text format (FORMat:DATA ASCii,0), the commands that make the
  measurement that S-parameter in log magnitude, a single sweep, *OPC?, the data query], acquires: true, wait: the
  longest sweep in seconds, axis: {start: <start frequency>, stop: <stop frequency>}. A reply of two numbers per
  point (complex data: real and imaginary; some analyzers' formatted data: the value and 0): pairs: true, and y from
  re and im, e.g. y: "20*log10(hypot(re, im))" for dB (without y, the first of each pair).
- set: command template with {value}; get: the query. SCPI short/long forms as in the manual, optional nodes in
  [brackets], e.g. "SOURce:VOLTage[:LEVel] {value}".
- limits: [min, max] the instrument accepts (the vendor's, not the lab's).
- Drives must have safe: the value that takes the drive away from the sample (0, off), and for continuous drives
  ramp: gate with max_rate (units per second) so Kelvin ramps instead of jumping.
- applies_when: <bool drive> when the value only reaches the sample while that switch is on; enables: [...] on the
  switch itself.
- Replies: field: n (0-based) for comma-separated replies; parse: a regex with one group for odd formats;
  read_when: <bool drive> for readings that would switch something on if read while it is off.
- description: one line; default: the power-on value if the manual gives it.

Instruments that speak Modbus (the manual gives a register map: compressors, temperature controllers, chillers,
flow and vacuum controllers): the address is modbus://<ip>?unit=<n> (TCP) or modbus://COM5?unit=<n>&baud=9600&parity=N
(RTU). get: "READ 30008 float32" (input register 30008 in the usual numbering: 0xxxx coils, 1xxxx discrete
inputs, 3xxxx input and 4xxxx holding registers; or "READ HR 6" counted from 0); types uint16, int16, uint32, int32,
float32, float64, then swapped when the low word comes first and *0.1 for a register in tenths. set: "WRITE 40001
int16 *0.1 {value}" or "WRITE COIL 3 {value}". Modbus has no identity reply: leave identify out.

Instruments with a web API (HTTP and JSON; the lab config's address is http(s)://host:port): commands are the
method, the path and, for writes, a JSON body with {value} in it: get: "GET /v1/Basic/ActualOutputPower" (the JSON
reply is the value; "GET /v1/Status#laser.state" takes one field), set: "PUT /v1/Basic/TargetAttenuatorPercentage
{value}" or "PUT /v1/Shutter {\"open\": {value}}". For an action per state, a bool with bool_text: [EnableOutput,
CloseOutput] and set: "POST /v1/Basic/{value}". identify: {query: "GET /v1/...", idn: a regex for its reply}.

Instruments the maker controls through a library (its example scripts in Python, MATLAB or C call a module or a
DLL instead of sending text): the card gets a library section, and each command calls one function.
- library: module: <the Python module the examples import> (object: <the class or object the functions hang on>;
  create: true if the examples make an instance first, create_args: [<its arguments>], {address} for what the lab's
  address names). Functions on its sub-objects are called by their path: attocube's network instruments
  (device = Device(ip); device.connect(); device.sample.getTemperature()) are module: <their module>, object: Device,
  create: true, create_args: ["{address}"], open: [connect], close: [close], get: "GET sample.getTemperature". Or
  dll: <its file name>, prefix: <the part of the exported
  names before the function name>, convention: cdecl or stdcall, returns: error when every function returns an error
  code (0 = fine), and functions: for each function the card calls, its C arguments (args: [float]) and the one value
  it writes back through a pointer (out: float; out: str for a char buffer followed by its length). Types: int8 uint8
  int16 uint16 int32 int uint32 int64 uint64 float double bool str. Take them from the header file; never guess.
  Or a .NET library (Thorlabs Kinesis and many stages and cameras): assembly: [<its .dll files the examples
  reference>], type: <the full name of the class>, setup: [<static calls the examples make first, by full name:
  Thorlabs.MotionControl.DeviceManagerCLI.DeviceManagerCLI.BuildDeviceList>], create: <the factory method the
  examples call (CreateKCubeDCServo), or true for a constructor>, create_args: ["{address}"], properties: [<the
  properties the card reads or sets, such as Position>]. Kelvin passes values as each method's parameters want them
  (a Decimal position, an enum by name). Or a COM object of an older maker's program: com: <its ProgID>, properties.
  A property is read with get: "GET Position" and set with set: "SET Exposure= {value}".
- library.open: the calls the examples make to connect, in order ({address} is what the lab's address names, such as
  a COM port or an IP address; add "wait 10" where the examples sleep before the instrument answers). library.close:
  the calls they end with.
- set: "SET setUserTemperature {value}", get: "GET getSampleTemperature"; fixed arguments may follow the name
  ("GET getChannelTemperature 2"). A get must call a function that only reads (get..., is..., query...).
- A switch the library can only toggle (toggleFullTemperatureControl), with a function that reads its state
  (isControllingTemperature): library.toggles: {setTemperatureControl: {toggle: toggleFullTemperatureControl, state:
  isControllingTemperature}}, then set: "SET setTemperatureControl {value}" and get: "GET isControllingTemperature".
  Kelvin reads the state before it toggles.
- Leave out what the maker's own program or touch screen should do (sample exchange, pumping, valves, calibration
  curves, firmware): Kelvin can call only the functions the card names.
- requires: [<the maker's Python packages the library imports, as pip names them>], when they come from PyPI.
- Say in notes how to address it: lib://<port or IP>?env=<the environment install_for_instrument made for the
  library: its Python, 32-bit for a 32-bit DLL, with the maker's package or files in it>; or, where a person set the
  library up themselves, lib://<port or IP>?python=<that Python>&path=<the folder with the maker's files>. A .NET
  library in the folder its maker's installer put it in: lib://<serial>?path=<that folder in Program Files>. A COM
  object: lib://<a name>?env=<an environment with pywin32>.
Example scripts that send text (inst.write("SOUR:VOLT 1"), inst.query("MEAS:VOLT?")) give ordinary command templates.

Acquisitions and triggers (a multimeter's buffer, a scope's single shot, an analyzer's sweep): a reading whose
commands arm, trigger and wait before the fetch is get: ["INIT", "*TRG", "*OPC?", "TRAC:DATA?"] with acquires: true
and wait: <the seconds it may take>; *OPC? answers when the acquisition is done. Those commands only arm, trigger
and wait: a setting (trigger source, sample count) is a parameter of its own. Named commands with no value go under
actions: {trigger: {send: "*TRG", label: Trigger}, fire: {send: ":BURS:FIRE", label: Fire a burst, drives: true}}
(drives: true when it drives the sample, which then asks a person); a plan does them at every point with trigger:.
A measurement the instrument times itself into its buffer, with parameters (cyclic voltammetry, an impedance sweep,
a scope's segmented capture) is a technique: techniques: {cv: {label, parameters: {start: {unit: V, drive: V},
rate: {unit: V/s, rate_of: V, limits: [lo, hi], default: x}, ...}, let: {delay: "step / rate"}, duration: "<its
seconds, from its parameters>", arm: [commands with {its names}], trigger: [...], wait: "*OPC?" (or done: {get,
match}), count: "<query: how many points>", fetch: {I: {get: "<query with {n}>", unit: A}, ...}, x: <the quantity
the others are against>, abort: [...]}}. A level it applies names the drive it is a level of (drive:), so that
drive's limits hold; every other number has limits; its commands never set a parameter of the card.

Instruments whose commands start with the value ("ON,1" / "OFF,1"): set: "{value},{ch}" with bool_text: ["ON", "OFF"]
(quote the words: YAML reads a bare ON as true). Settings with no read-back: leave out get; Kelvin remembers what
it set, and treats the value as unknown until then. Connecting leaves the instrument as it is: on_connect may only
set up the connection (listen mode, headers), never a parameter.

Rules: use only commands that are in the manual text you were given; never invent a command, a limit or a reply
format. If the manual does not say, leave the parameter out and mention it. Block calibration, firmware, sensor
curve and protection-limit commands under raw.blocked."""


def example() -> str:
    return EXAMPLE.read_text(encoding="utf-8")


def guide() -> str:
    return GUIDE + "\n\n# A complete example card\n" + example()


def extract_yaml(text: str) -> str:
    """The card from a model's answer: the first ```yaml block, or the text from `card: 1` on."""
    m = re.search(r"```(?:ya?ml)?\s*\n(.*?)```", text or "", re.S)
    if m:
        return m.group(1).strip() + "\n"
    i = (text or "").find("card: 1")
    return (text[i:] if i >= 0 else text or "").strip() + "\n"


def notes_in(text: str) -> List[str]:
    """What the model wrote beside its card: what it left out or is unsure of (at most five lines)."""
    m = re.search(r"```(?:ya?ml)?\s*\n.*?```", text or "", re.S)
    if not m:
        return []
    rest = text[:m.start()] + "\n" + text[m.end():]
    lines = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", line).strip() for line in rest.splitlines()]
    return [line[:300] for line in lines if line][:5]


def check(text: str, source: str = "draft") -> Tuple[Optional[Card], List[str], List[str]]:
    """(card or None, problems that stop it from being used, warnings worth fixing)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = " (line %d)" % (mark.line + 1) if mark is not None else ""
        return None, ["the YAML does not parse%s: %s" % (where, getattr(e, "problem", None) or e)], []
    try:
        card = parse_card(data, source)
    except CardError as e:
        return None, list(e.errors), []
    warnings = []
    if not card.idn_pattern:
        warnings.append("no identify.idn: a scan cannot recognise this instrument")
    if card.error_query is None:
        warnings.append("no error_query: Kelvin cannot read the instrument's own errors")
    if not data.get("manual"):
        warnings.append("say which manual (and chapter) the card follows, under manual:")
    if not any(p.is_drive for p in card.params.values()):
        warnings.append("no drives: Kelvin can read this instrument but not set anything that reaches the sample")
    return card, [], warnings


def file_for(card_id: str) -> str:
    return re.sub(r"[^\w.-]+", "_", card_id).strip("_") + ".yaml"


def user_cards(cards_dir: Optional[Path], collection_ids: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """The user's own cards, with any problems (a broken file is listed, not skipped silently). "kelvins": the lab's
    version of one of Kelvin's cards (under its id), which replaces Kelvin's in this lab. collection_ids: the ids the
    instrument collection has cards for, which Kelvin uses instead of a draft of the same id."""
    from .homelab import builtin_ids
    out = []
    if not cards_dir or not Path(cards_dir).is_dir():
        return out
    collection_ids = set(collection_ids)
    for path in sorted(Path(cards_dir).glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        card, problems, warnings = check(text, path.name)
        if card is not None and card.id in collection_ids and card.status == "draft":
            warnings = ["The instrument collection has a card %s and Kelvin uses that instead of this draft, until a "
                        "person marks this one reviewed" % card.id] + warnings
        out.append({"file": path.name, "id": card.id if card else None, "model": card.model if card else path.stem,
                    "kind": card.kind if card else "", "vendor": card.vendor if card else "",
                    "status": card.status if card else "broken", "problems": problems, "warnings": warnings,
                    "yaml": text, "kelvins": bool(card and card.id in builtin_ids())})
    return out


def save(cards_dir: Path, text: str, kelvin_ids: Iterable[str], by: str = "human",
         replace: Optional[str] = None, version: bool = False) -> Tuple[Card, Path, List[str]]:
    """Check and keep a card. kelvin_ids: the ids Kelvin has cards for itself (its own, and the instrument
    collection's); a new card may not take one, though a card of the lab's that has one already can be changed.
    version: the card may take one of them as this lab's version of it (another firmware, a wider range, a different
    command): it replaces Kelvin's card in this lab, and Kelvin's stays for every other lab. Kelvin's cards must be
    drafts and may not replace a card a person reviewed."""
    card, problems, warnings = check(text)
    if card is None:
        raise ValueError("the card has problems: " + "; ".join(problems[:8]))
    cards_dir = Path(cards_dir)
    path = cards_dir / file_for(card.id)
    if card.id in set(kelvin_ids) and not version and not path.exists():
        if by != "human":   # Kelvin: use the card it has, unless this lab's instrument really differs
            raise ValueError("Kelvin already has a card %s: use it (add_instrument card=%s, try_card %s). Only when "
                             "it doesn't fit this lab's instrument (another firmware, a wider range, a different "
                             "command), save_card with version: true makes yours this lab's version of it, or give "
                             "yours another id" % (card.id, card.id, card.id))
        raise ValueError("Kelvin already has a card %s; give yours another id (e.g. %s), or make it this lab's "
                         "version of Kelvin's card (in the portal: Lab, Cards…, Make your version)" % (
                             card.id, "mylab/" + card.id.split("/")[-1]))
    if by != "human" and card.status != "draft":
        raise ValueError("a card Kelvin writes must have status: draft; a person reviews it and marks it lab-verified")
    cards_dir.mkdir(parents=True, exist_ok=True)
    if path.exists() and by != "human":
        old, _, _ = check(path.read_text(encoding="utf-8"))
        if old is not None and old.status != "draft":
            raise ValueError("a person has reviewed %s (%s); Kelvin may not change it" % (card.id, old.status))
    if replace and replace != card.id:   # renamed: drop the old file
        old_path = cards_dir / file_for(replace)
        if old_path.exists() and old_path != path:
            old_path.unlink()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    tmp.replace(path)
    return card, path, warnings


def set_status(cards_dir: Path, card_id: str, status: str) -> Card:
    """A person marks a card reviewed (lab-verified) or back to draft."""
    if status not in STATUSES:
        raise ValueError("status must be one of %s" % ", ".join(STATUSES))
    path = Path(cards_dir) / file_for(card_id)
    if not path.exists():
        raise KeyError("no card %r among yours" % card_id)
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(r"(?m)^status:\s*\S+", "status: " + status, text, count=1)
    if not n:
        new = re.sub(r"(?m)^(kind:.*)$", r"\1\nstatus: " + status, text, count=1)
    card, problems, _ = check(new)
    if card is None:
        raise ValueError("the card has problems: " + "; ".join(problems[:8]))
    path.write_text(new, encoding="utf-8")
    return card


REVIEWS = "_reviewed.json"   # in the person's cards folder: Kelvin's own draft cards they reviewed for their lab


def card_sha(card: Card) -> str:
    """The card's file as it is, so a review holds only for the card that was reviewed."""
    try:
        return hashlib.sha256(Path(card.source).read_bytes()).hexdigest()
    except OSError:
        return ""


def builtin_reviews(cards_dir: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads((Path(cards_dir) / REVIEWS).read_text(encoding="utf-8")) if cards_dir else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def review_builtin(cards_dir: Path, card: Card, status: str, by: str = "") -> None:
    """A person marks one of Kelvin's own draft cards reviewed for their lab (lab-verified), or takes that back
    (draft). Kept with the card's content: a later Kelvin whose card changed asks again until it is reviewed."""
    if status not in ("lab-verified", "draft"):
        raise ValueError("a card that comes with Kelvin is marked reviewed (lab-verified) or back to draft")
    reviews = builtin_reviews(cards_dir)
    if status == "draft":
        reviews.pop(card.id, None)
    else:
        reviews[card.id] = {"status": status, "sha": card_sha(card), "by": by, "at": time.strftime("%Y-%m-%d")}
    folder = Path(cards_dir)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / (REVIEWS + ".tmp")
    tmp.write_text(json.dumps(reviews, indent=1), encoding="utf-8")
    os.replace(tmp, folder / REVIEWS)


def apply_reviews(registry: Any, cards_dir: Optional[Path]) -> None:
    """Kelvin's own draft cards a person reviewed for this lab count as reviewed, while the card is unchanged."""
    for card_id, review in builtin_reviews(cards_dir).items():
        if card_id in registry:
            card = registry.get(card_id)
            if isinstance(review, dict) and review.get("sha") and review["sha"] == card_sha(card):
                card.status = str(review.get("status") or card.status)


def delete(cards_dir: Path, card_id: str) -> None:
    path = Path(cards_dir) / file_for(card_id)
    if not path.exists():
        raise KeyError("no card %r among yours" % card_id)
    path.unlink()


DRAFT_SYSTEM = GUIDE + """

You write one card for the instrument the user names, from the manual text or example scripts they give you. Answer with the card in
one ```yaml block, then at most five short lines on what you left out or are unsure of. If the manual leaves an output's
ranges to another document (its specifications or a datasheet), keep that output as a reading only and add the line
"Ranges in: <that document's title>; for: <those outputs, e.g. amplitude, offset>". Status: draft."""


def draft_prompt(manual: str, name: str, specs: str = "") -> str:
    """specs: pages of the document the manual leaves some ranges to (its specifications), if Kelvin has it."""
    manual = manual.strip()
    clipped = manual[:MAX_MANUAL]
    note = "" if len(manual) <= MAX_MANUAL else "\n(The manual text was cut at %d characters.)" % MAX_MANUAL
    ranges = ("\n\nIts specifications (the document the manual leaves ranges to), for the limits:\n%s"
              % specs.strip()[:MAX_MANUAL // 2]) if specs.strip() else ""
    return ("Instrument: %s\n\nAn example of a finished card, for the format:\n%s%s\n\nThe manual, its command "
            "reference, or the maker's example scripts:\n%s%s" % (name or "(see the manual)", example(), ranges,
                                                                    clipped, note))


SOURCE_TYPES = {".py", ".m", ".h", ".hpp", ".c", ".cpp", ".cs", ".vb", ".bas", ".jl", ".tcl", ".ipynb", ".txt", ".md",
                ".rst", ".ini", ".json", ".yaml", ".yml", ".htm", ".html", ".pdf"}
MAX_PDF_BYTES = 40_000_000   # a manual: read up to MAX_MANUAL characters of its text
SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv", "build", "dist"}


def _source_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":   # a manual: its pages' text
        from .profiles import pdf_pages
        try:
            pages = pdf_pages(path)
        except RuntimeError as e:   # no pypdf here
            return "(%s: %s)" % (path.name, e)
        except Exception as e:
            return "(%s: Kelvin could not read this PDF: %s)" % (path.name, str(e).splitlines()[0] if str(e) else e)
        text = "\n\n".join("[page %d]\n%s" % (i + 1, p.strip()) for i, p in enumerate(pages) if p.strip())
        return text or "(%s: no text in this PDF, a scan? attach pictures of its pages)" % path.name
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".ipynb":   # a notebook: its code and notes, not its outputs
        try:
            cells = json.loads(text).get("cells") or []
            text = "\n\n".join("".join(c.get("source") or []) for c in cells if c.get("cell_type") in ("code", "markdown"))
        except (ValueError, AttributeError):
            pass
    elif path.suffix.lower() in (".htm", ".html"):
        text = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text))
    return text


def read_sources(path: str, limit: int = MAX_MANUAL, own: Iterable[Any] = ()) -> str:
    """A maker's example scripts, headers and READMEs from a file or folder, as one text with each file's name: what
    a card is drafted from when the maker gives code rather than a command reference. Never Kelvin's own folders
    (own: its folder, with the lab config, the settings, the accounts, the cards and everyone's chats), nor a file
    that may hold sign-ins, keys or tokens (folderfiles.secret): a folder that holds them is read without them."""
    from .folderfiles import mine, run_of, secret
    root = Path(path).expanduser()
    if not root.exists():
        raise ValueError("no file or folder %s" % path)
    kelvins = mine(root.resolve(), own)
    if kelvins is not None:
        raise ValueError("%s is in Kelvin's own folder (%s), which holds the lab config, the settings, the accounts, "
                         "the instrument cards, and everyone's chats and runs: Kelvin's AI never reads there. Name the "
                         "maker's files where the maker put them" % (root, kelvins))
    if run_of(root.resolve()) is not None:
        raise ValueError("%s is in a Kelvin run, not a maker's files: describe_run and get_run_data read runs" % root)
    if secret(root.resolve()):
        raise ValueError("%s may hold sign-ins, keys or tokens (a password file, an SSH or cloud key, a .env file, "
                         "another program's sign-in): Kelvin's AI never reads such files" % root)
    if root.is_file():
        files = [root]
    else:
        files = []
        for p in sorted(root.rglob("*")):
            if len(files) >= 400:
                break
            if any(part in SKIP_DIRS or part.startswith(".") for part in p.relative_to(root).parts[:-1]):
                continue
            if p.is_file() and p.suffix.lower() in SOURCE_TYPES and p.stat().st_size < (
                    MAX_PDF_BYTES if p.suffix.lower() == ".pdf" else 2_000_000) and not secret(p) \
                    and mine(p.resolve(), own) is None and run_of(p.resolve()) is None:   # nor Kelvin's runs
                files.append(p)
    if not files:
        raise ValueError("no scripts, headers, text files or PDFs in %s" % path)

    def rank(p: Path) -> Tuple[int, int]:
        name = p.name.lower()
        first = 0 if name.startswith("readme") else 1 if p.suffix.lower() in (".h", ".hpp") else \
            2 if "example" in str(p).lower() or "demo" in name else 3
        return first, len(p.parts)

    out, used, left = [], 0, []
    for p in sorted(files, key=rank):
        text = _source_text(p).strip()
        if not text:
            continue
        label = p.name if root.is_file() else str(p.relative_to(root))
        piece = "=== %s ===\n%s\n" % (label, text)
        if used + len(piece) > limit:
            left.append(label)
            continue
        out.append(piece)
        used += len(piece)
    if left:
        out.append("(Left out, over the length limit: %s)" % ", ".join(left[:40]))
    return "\n".join(out)


def repair_prompt(text: str, problems: List[str]) -> str:
    return ("Your card has these problems:\n- %s\n\nAnswer with the corrected card in one ```yaml block.\n\n%s"
            % ("\n- ".join(problems[:12]), text))
