"""The lab's memory: what people and Kelvin learned about this lab, kept for every chat, everyone who works here and
every agent (plan item 28).

F1 t06, F2 l01 l04 l06 l10, F3 #13: each chat started from nothing. A new chat forgot which Lake Shore channel reads the
mixing chamber, assumed the lock-in's internal reference when it takes the chopper's, did not know the excitation
current, and wrote a night's queue in the order that had left both lasers at 0 W that morning, a lesson learned two
chats before.

A note is one durable fact about the lab, of one kind:
- lesson: what to do or to avoid, learned at the bench ("switch each OBIS's emission on before its power");
- wiring: what is connected where (a thermometer's channel, a cable, a filter, a breakout box);
- reference: where a reference comes from (a lock-in's reference, a clock, a calibration standard);
- excitation: what drives the sample and how it is stated (10 nA rms through 10 MΩ);
- fact: anything else that lasts (the fridge's base temperature, when the helium is filled).
A note may be tied to instruments: it is then shown when one of them is used, and its lessons on their approval cards.

Each note keeps who wrote it and when (a person at the portal, Kelvin in someone's chat, an agent over MCP), and what
it said before each change. People add, change and delete notes in the portal's Lab panel; Kelvin keeps them with
remember (scope lab) and corrects one by its id. Every change goes into the lab's record.

The memory is the lab's, not a person's: it is kept in Kelvin's own folder (lab-memory.json for the lab in lab.yaml,
virtual-lab-memory.json for the virtual lab, so practising never writes into the real lab's), where everyone who signs
in on this computer, every chat and every agent reads the same notes.

Every chat sees a brief of it (BRIEF_CHARS at most): lessons first, then wiring, references, excitations and other
facts; notes on the lab's own instruments before notes on instruments it no longer has, the newest first; and how many
it left out, which shared_memory looks up. An incognito chat uses no memory at all (the owner's choice, 2026-10-29): no
brief, no shared_memory, no notes with an instrument's use (without_notes); approval cards still show the person who
answers them the lab's lessons."""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

KINDS = ("lesson", "wiring", "reference", "excitation", "fact")   # in the brief's order: lessons first
WHAT = {"lesson": "what to do or avoid", "wiring": "what is connected where", "reference": "where a reference comes "
        "from", "excitation": "what drives the sample, and how it is stated", "fact": "anything else that lasts"}
MAX_TEXT = 300      # characters: one fact, in a sentence or two
MAX_NOTES = 200
MAX_TIES = 8        # instruments one note is tied to
MAX_BEFORE = 5      # earlier versions kept with a note
BRIEF_CHARS = 1200  # the notes in every chat's prompt (about 300 tokens); the rest is one lookup away
AT_USE = 5          # notes shown with an instrument's use; shared_memory has the rest
AT_USE_HEAD = "From the lab's memory, on "   # how those notes start, at the end of a tool's answer
ON_CARD = 3         # lessons shown on an approval card
FILE = "lab-memory.json"
VIRTUAL_FILE = "virtual-lab-memory.json"
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_lock = threading.Lock()   # one writer at a time (the runtime opens the file anew for each call)
# the brief's head, in every chat's prompt and every agent's instructions (head())
SECTION = """# The lab's memory
What people and Kelvin keep about this lab, for every chat, everyone here and every agent: how it is wired, where
references come from, what excites the sample, lessons learned. Each note says who wrote it and when. Follow them
unless the person says otherwise; a person's note outranks Kelvin's. If one looks out of date or disagrees with what
you see, {fix}. shared_memory looks up every note, and describe_instrument shows an instrument's."""


def head(keep: str = "") -> str:
    """The brief's head. keep: how this client keeps a note ("" when it keeps none: unshared work, learning
    switched off)."""
    return SECTION.format(fix="say so, and correct it (%s, replaces: its id) once the person confirms" % keep if keep
                          else "say so: a person corrects it in the portal's Lab panel")


def without_notes(text: str) -> str:
    """A tool's answer without the lab's notes it ends with (LabRuntime.lab_notes_at_use), for a chat that uses no
    memory: an incognito one."""
    at = text.rfind("\n" + AT_USE_HEAD)
    return text[:at] if at >= 0 else text


class LabMemoryError(ValueError):
    """What is wrong with a note, or with the memory's file, in words."""


def path_for(folder: Any, virtual: bool) -> Path:
    """Where a lab keeps its memory: the virtual lab's apart from the lab in lab.yaml."""
    return Path(folder) / (VIRTUAL_FILE if virtual else FILE)


def kind_of(kind: Any) -> str:
    """A note's kind, checked (lessons is lesson)."""
    k = str(kind or "").strip().lower()
    k = k[:-1] if k.endswith("s") and k[:-1] in KINDS else k   # lessons, facts
    if k not in KINDS:
        raise LabMemoryError("a lab note's kind is one of %s" % ", ".join(
            "%s (%s)" % (k, WHAT[k]) for k in KINDS))
    return k


def text_of(text: Any) -> str:
    """A note's text on one line, checked: one fact, not empty, not long."""
    t = " ".join(str(text or "").split())
    if not t:
        raise LabMemoryError("a note needs its text: the fact, in a sentence")
    if len(t) > MAX_TEXT:
        raise LabMemoryError("a note is at most %d characters (this one has %d); keep the fact itself"
                             % (MAX_TEXT, len(t)))
    return t


def ties(instruments: Any) -> List[str]:
    """The instruments a note is tied to, by name: a list, or names in a text (pump, probe); a parameter (pump.power)
    stands for its instrument."""
    if instruments in (None, "", []):
        return []
    items = re.split(r"[,;\s]+", instruments) if isinstance(instruments, str) else list(instruments)
    out: List[str] = []
    for item in items:
        name = str(item or "").strip().split(".")[0].strip()
        if not name:
            continue
        if not _NAME.match(name):
            raise LabMemoryError("%r is not an instrument's name (the names lab_status shows, such as pump or li)"
                                 % str(item)[:60])
        if name.lower() not in [o.lower() for o in out]:
            out.append(name)
    if len(out) > MAX_TIES:
        raise LabMemoryError("a note is tied to at most %d instruments" % MAX_TIES)
    return out


def who(by: Any, account: Any = None) -> str:
    """Who wrote or changed a note, as people read it: a person (by their account's name, at the portal or on a
    phone), Kelvin in someone's chat, or an agent over MCP."""
    by, account = str(by or ""), str(account or "")
    if by.startswith("Kelvin"):
        return "Kelvin, in %s's chat" % account if account else "Kelvin"
    if by.startswith("agent"):
        return "an AI agent over MCP"
    phone = re.match(r"human \(phone (.*)\)$", by)
    if account:
        return account + (" (phone %s)" % phone.group(1) if phone else "")
    return "a person" + (" on the phone %s" % phone.group(1) if phone else " at the portal")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sound(n: Any) -> bool:
    """A note as Kelvin keeps it (a hand-edited file may hold others: they are left out, never lost)."""
    return (isinstance(n, dict) and isinstance(n.get("id"), str) and n.get("kind") in KINDS
            and isinstance(n.get("text"), str) and n["text"].strip() != "")


def line(note: Dict[str, Any]) -> str:
    """A note in one line, for a model: its kind and instruments, the fact, who wrote it and when (and who changed
    it last), and its id (to correct it)."""
    tied = " [%s]" % ", ".join(note["instruments"]) if note.get("instruments") else ""
    said = "%s, %s" % (who(note.get("by"), note.get("account")), str(note.get("at") or "")[:10])
    edited = note.get("edited")
    if isinstance(edited, dict):
        said += "; changed by %s, %s" % (who(edited.get("by"), edited.get("account")), str(edited.get("at") or "")[:10])
    return "- %s%s: %s (%s; id %s)" % (note["kind"], tied, note["text"], said, note["id"])


def view(note: Dict[str, Any]) -> Dict[str, Any]:
    """A note for the portal: as kept, with who wrote it (and changed it last) in words."""
    out = {k: note.get(k) for k in ("id", "kind", "text", "instruments", "at", "edited", "before")}
    out["instruments"] = list(note.get("instruments") or [])
    out["before"] = list(note.get("before") or [])
    out["who"] = who(note.get("by"), note.get("account"))
    if isinstance(note.get("edited"), dict):
        out["edited_who"] = who(note["edited"].get("by"), note["edited"].get("account"))
    return out


def _tied_to(note: Dict[str, Any], names: Iterable[str]) -> bool:
    wanted = {str(n).lower() for n in names}
    return any(str(i).lower() in wanted for i in note.get("instruments") or [])


def newest(note: Dict[str, Any]) -> str:
    """When a note was last written: changed, else kept."""
    edited = note.get("edited") if isinstance(note.get("edited"), dict) else {}
    return str(edited.get("at") or note.get("at") or "")


def _newest_first(notes: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Newest first; notes kept in the same second, the later in the file first."""
    return [n for _, n in sorted(enumerate(notes), key=lambda p: (newest(p[1]), p[0]), reverse=True)]


def ordered(notes: Iterable[Dict[str, Any]], instruments: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """The notes in the order chats, shared_memory and the Lab panel show them: lessons first, then wiring, references,
    excitations and other facts; within a kind, notes on the lab's own instruments (or on none) before notes on
    instruments it doesn't have, the newest first."""
    here = {str(i).lower() for i in instruments}
    out = _newest_first(notes)
    out.sort(key=lambda n: (KINDS.index(n["kind"]), bool(n.get("instruments")) and not _tied_to(n, here)))
    return out


def brief(notes: Sequence[Dict[str, Any]], instruments: Iterable[str] = (),
          budget: int = BRIEF_CHARS) -> Tuple[List[str], int]:
    """The notes every chat sees in its prompt, in order (ordered), as lines within `budget` characters, and how many
    were left out. A note that doesn't fit is left for shared_memory, and a shorter one after it may still fit."""
    picked: List[str] = []
    used = 0
    for n in ordered(notes, instruments):
        text = line(n)
        if used + len(text) + 1 <= budget:
            picked.append(text)
            used += len(text) + 1
    return picked, len(notes) - len(picked)


def for_instruments(notes: Sequence[Dict[str, Any]], names: Iterable[str],
                    kinds: Sequence[str] = KINDS) -> List[Dict[str, Any]]:
    """The notes tied to any of these instruments, lessons first, the newest first within a kind."""
    names = list(names)
    found = _newest_first(n for n in notes if n["kind"] in kinds and _tied_to(n, names))
    found.sort(key=lambda n: KINDS.index(n["kind"]))
    return found


def find(notes: Sequence[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    """The notes that bear on the words (their text, kind and instruments), best first; none share a word: none."""
    from .web.memory import rank   # pure Python: the same idf-weighted overlap that recalls a project's notes
    docs = [dict(n, text="%s %s %s" % (n["text"], n["kind"], " ".join(n.get("instruments") or [])),
                 created=newest(n), _note=n) for n in notes]
    return [d["_note"] for d in rank(docs, query) if d["score"] > 0]


class LabMemory:
    """The notes of one lab, in one JSON file: {"notes": [{id, kind, text, instruments, by, account, at, edited,
    before}]}. Read anew on every call (several Kelvins, the app and the MCP server, may share it); written whole,
    through a temporary file, so a crash never leaves half a file."""

    def __init__(self, path: Any):
        self.path = Path(path)

    def _read(self, strict: bool = False) -> Dict[str, Any]:
        """The file's notes. strict (before a write): a file that exists but can't be read stops the write, so a
        hand-edited file with a mistake in it is never replaced by an empty one."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"notes": []}
        except (OSError, ValueError) as e:
            if strict:
                raise LabMemoryError("the lab's memory (%s) can't be read: %s. Fix or move that file first"
                                     % (self.path, e))
            return {"notes": []}
        notes = data.get("notes") if isinstance(data, dict) else None
        if not isinstance(notes, list):
            if strict:
                raise LabMemoryError("the lab's memory (%s) is not a list of notes. Fix or move that file first"
                                     % self.path)
            return {"notes": []}
        return dict(data, notes=notes)

    def _write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def notes(self) -> List[Dict[str, Any]]:
        """Every note, in the order they were kept."""
        return [n for n in self._read()["notes"] if _sound(n)]

    def stamp(self) -> str:
        """Changes whenever the file does: the portal reloads the notes then."""
        try:
            st = self.path.stat()
        except OSError:
            return "%s:none" % self.path.name
        return "%s:%d:%d" % (self.path.name, st.st_mtime_ns, st.st_size)

    def get(self, note_id: str) -> Dict[str, Any]:
        for n in self.notes():
            if n["id"] == str(note_id):
                return n
        raise KeyError("no note %r in the lab's memory" % note_id)

    def add(self, kind: Any, text: Any, instruments: Any = None, by: str = "", account: Optional[str] = None,
            replaces: Optional[str] = None) -> Tuple[Dict[str, Any], str]:
        """Keep a note. Returns (note, what happened): "saved", "known" (the same fact, in the same or other words, is
        there already: it is left as it was) or "updated" (it corrects the note `replaces`, whose earlier text is
        kept with it)."""
        from .web.memory import SAME, similarity
        kind, text, tied = kind_of(kind), text_of(text), ties(instruments)
        if replaces:
            return self.change(str(replaces), by, account, kind=kind, text=text, instruments=tied), "updated"
        with _lock:
            data = self._read(strict=True)
            notes = data["notes"]
            for n in notes:
                if _sound(n) and (n["text"].lower() == text.lower() or similarity(n["text"], text) >= SAME):
                    return n, "known"
            if sum(1 for n in notes if _sound(n)) >= MAX_NOTES:
                raise LabMemoryError("the lab's memory is full (%d notes); a person can delete old ones in the Lab "
                                     "panel, under Lab memory" % MAX_NOTES)
            note = {"id": "m" + secrets.token_hex(4), "kind": kind, "text": text, "instruments": tied,
                    "by": by or "human", "account": account or None, "at": _now()}
            notes.append(note)
            self._write(data)
        return note, "saved"

    def change(self, note_id: str, by: str, account: Optional[str] = None, kind: Any = None, text: Any = None,
               instruments: Any = None) -> Dict[str, Any]:
        """Change a note's kind, text or instruments; what it said before is kept with it (the last few versions),
        and who changed it and when."""
        new: Dict[str, Any] = {}
        if kind is not None:
            new["kind"] = kind_of(kind)
        if text is not None:
            new["text"] = text_of(text)
        if instruments is not None:
            new["instruments"] = ties(instruments)
        with _lock:
            data = self._read(strict=True)
            note = next((n for n in data["notes"] if _sound(n) and n["id"] == str(note_id)), None)
            if note is None:
                raise KeyError("no note %r in the lab's memory" % note_id)
            if all(note.get(k) == v for k, v in new.items()):
                return note
            last = note.get("edited") if isinstance(note.get("edited"), dict) else {}
            was = {"kind": note["kind"], "text": note["text"], "instruments": list(note.get("instruments") or []),
                   "by": last.get("by", note.get("by")), "account": last.get("account", note.get("account")),
                   "at": last.get("at", note.get("at"))}
            note["before"] = ([was] + list(note.get("before") or []))[:MAX_BEFORE]
            note.update(new)
            note["edited"] = {"by": by or "human", "account": account or None, "at": _now()}
            self._write(data)
        return note

    def remove(self, note_id: str) -> Dict[str, Any]:
        with _lock:
            data = self._read(strict=True)
            note = next((n for n in data["notes"] if _sound(n) and n["id"] == str(note_id)), None)
            if note is None:
                raise KeyError("no note %r in the lab's memory" % note_id)
            data["notes"] = [n for n in data["notes"] if n is not note]
            self._write(data)
        return note
