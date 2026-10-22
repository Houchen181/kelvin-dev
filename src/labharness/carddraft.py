"""Cards for instruments Kelvin does not know yet: drafted from the manual or the maker's example scripts, checked,
tried on the instrument, kept in ~/Kelvin/cards.

A card Kelvin writes is always a draft. On real hardware a draft card's readings work, so it can be tried against
the instrument, but its drives stay simulation-only until a person reviews the card and marks it lab-verified.
Only a person can do that, and Kelvin never overwrites a card a person has reviewed."""
from __future__ import annotations

import json
import re
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
- error_query: the command that reads the error queue, if any (e.g. "SYSTem:ERRor?").
- connection (only if the manual requires it): read_termination, write_termination, baud_rate, data_bits, parity,
  stop_bits, timeout_ms, write_only (a listen-only instrument), ack (a reply every set returns).
- parameters: one entry per quantity, keyed by a short name.
- display: {headline: <parameter>} for the reading shown large; raw: {blocked: [...], reset: [...]}: command headers
  that must never be sent (calibration, firmware, sensor curves, protection limits) and reset commands.

Each parameter:
- label; kind: measure (reading or a setting that does not drive the sample) or drive (anything that drives the
  sample harder: voltage, current, power, field, temperature, outputs); unit (SI: V, A, W, T, K, Hz, s ...).
- type: float (default), int, bool, enum (with values: {label: code}) or trace.
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

Instruments with a web API (HTTP and JSON; the lab config's address is http(s)://host:port): commands are the
method, the path and, for writes, a JSON body with {value} in it: get: "GET /v1/Basic/ActualOutputPower" (the JSON
reply is the value; "GET /v1/Status#laser.state" takes one field), set: "PUT /v1/Basic/TargetAttenuatorPercentage
{value}" or "PUT /v1/Shutter {\"open\": {value}}". For an action per state, a bool with bool_text: [EnableOutput,
CloseOutput] and set: "POST /v1/Basic/{value}". identify: {query: "GET /v1/...", idn: a regex for its reply}.

Instruments the maker controls through a library (its example scripts in Python, MATLAB or C call a module or a
DLL instead of sending text): the card gets a library section, and each command calls one function.
- library: module: <the Python module the examples import> (object: <the class or object the functions hang on>;
  create: true if the examples make an instance first). Or dll: <its file name>, prefix: <the part of the exported
  names before the function name>, convention: cdecl or stdcall, returns: error when every function returns an error
  code (0 = fine), and functions: for each function the card calls, its C arguments (args: [float]) and the one value
  it writes back through a pointer (out: float; out: str for a char buffer followed by its length). Types: int8 uint8
  int16 uint16 int32 int uint32 int64 uint64 float double bool str. Take them from the header file; never guess.
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
- Say in notes how to address it: lib://<port or IP>?python=<a Python that can load the library, 32-bit for a 32-bit
  DLL>&path=<the folder with the maker's files>.
Example scripts that send text (inst.write("SOUR:VOLT 1"), inst.query("MEAS:VOLT?")) give ordinary command templates.

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


def user_cards(cards_dir: Optional[Path]) -> List[Dict[str, Any]]:
    """The user's own cards, with any problems (a broken file is listed, not skipped silently)."""
    out = []
    if not cards_dir or not Path(cards_dir).is_dir():
        return out
    for path in sorted(Path(cards_dir).glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        card, problems, warnings = check(text, path.name)
        out.append({"file": path.name, "id": card.id if card else None, "model": card.model if card else path.stem,
                    "kind": card.kind if card else "", "vendor": card.vendor if card else "",
                    "status": card.status if card else "broken", "problems": problems, "warnings": warnings,
                    "yaml": text})
    return out


def save(cards_dir: Path, text: str, builtin_ids: Iterable[str], by: str = "human",
         replace: Optional[str] = None) -> Tuple[Card, Path, List[str]]:
    """Check and keep a card. Kelvin's cards must be drafts and may not replace a card a person reviewed."""
    card, problems, warnings = check(text)
    if card is None:
        raise ValueError("the card has problems: " + "; ".join(problems[:8]))
    if card.id in set(builtin_ids):
        raise ValueError("Kelvin already has a card %s; give yours another id, e.g. %s" % (
            card.id, "mylab/" + card.id.split("/")[-1]))
    if by != "human" and card.status != "draft":
        raise ValueError("a card Kelvin writes must have status: draft; a person reviews it and marks it lab-verified")
    cards_dir = Path(cards_dir)
    cards_dir.mkdir(parents=True, exist_ok=True)
    path = cards_dir / file_for(card.id)
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


def delete(cards_dir: Path, card_id: str) -> None:
    path = Path(cards_dir) / file_for(card_id)
    if not path.exists():
        raise KeyError("no card %r among yours" % card_id)
    path.unlink()


DRAFT_SYSTEM = GUIDE + """

You write one card for the instrument the user names, from the manual text or example scripts they give you. Answer with the card in
one ```yaml block, then at most five short lines on what you left out or are unsure of. Status: draft."""


def draft_prompt(manual: str, name: str) -> str:
    manual = manual.strip()
    clipped = manual[:MAX_MANUAL]
    note = "" if len(manual) <= MAX_MANUAL else "\n(The manual text was cut at %d characters.)" % MAX_MANUAL
    return ("Instrument: %s\n\nAn example of a finished card, for the format:\n%s\n\nThe manual, its command "
            "reference, or the maker's example scripts:\n%s%s" % (name or "(see the manual)", example(), clipped, note))


SOURCE_TYPES = {".py", ".m", ".h", ".hpp", ".c", ".cpp", ".cs", ".vb", ".bas", ".jl", ".tcl", ".ipynb", ".txt", ".md",
                ".rst", ".ini", ".json", ".yaml", ".yml", ".htm", ".html"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv", "build", "dist"}


def _source_text(path: Path) -> str:
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


def read_sources(path: str, limit: int = MAX_MANUAL) -> str:
    """A maker's example scripts, headers and READMEs from a file or folder, as one text with each file's name: what
    a card is drafted from when the maker gives code rather than a command reference."""
    root = Path(path).expanduser()
    if not root.exists():
        raise ValueError("no file or folder %s" % path)
    if root.is_file():
        files = [root]
    else:
        files = []
        for p in sorted(root.rglob("*")):
            if len(files) >= 400:
                break
            if any(part in SKIP_DIRS for part in p.relative_to(root).parts[:-1]):
                continue
            if p.is_file() and p.suffix.lower() in SOURCE_TYPES and p.stat().st_size < 2_000_000:
                files.append(p)
    if not files:
        raise ValueError("no scripts, headers or text files in %s" % path)

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
