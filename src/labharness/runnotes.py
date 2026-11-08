"""Notes on a kept run: the flags, tags and comments people and Kelvin add afterwards (the annotate tool, the Runs
panel), kept in notes.jsonl in the run's folder beside run.h5, which never changes (asked for 2026-11-07: an ELN's
practice, a run judged after it was measured: "bad: the sample warmed at point 120", "key result", "Fig. 3").

    {"id": "n1a2b3c4", "at": "2026-11-07T10:32:01", "by": "agent" | "human", "account": "ann",
     "ai": {"model": ..., "chat": ...}, "flag": "bad" | "check" | "key" | "none", "tags": ["cooldown 3"],
     "text": "the sample warmed at point 120", "corrects": "n0f1e2d3"}

A note is a line, chained as the lab's record is (eventlog.append_chained), and is never changed or taken back: a
correction is a new note naming the one it corrects (corrects), and the run is shown with the notes that stand. A
flag stays until a later note sets another (none clears it); tags add up, and a correction's replace the
corrected note's."""
from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .eventlog import append_chained

FILE = "notes.jsonl"
FLAGS = ("bad", "check", "key")          # set aside / look again / a key result; "none" clears the flag
MAX_TEXT = 2000
MAX_TAGS = 10
MAX_TAG = 40


def _tags(tags: Any) -> List[str]:
    if isinstance(tags, str):
        tags = [t for t in tags.split(",")]
    out: List[str] = []
    for t in tags or []:
        t = " ".join(str(t).split())[:MAX_TAG]
        if t and t.lower() not in [x.lower() for x in out]:
            out.append(t)
    if len(out) > MAX_TAGS:
        raise ValueError("at most %d tags on a note" % MAX_TAGS)
    return out


def read(folder: Path) -> List[Dict[str, Any]]:
    """Every note on a run, oldest first."""
    path = Path(folder) / FILE
    out: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        note = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(note, dict):
                        note.pop("prev", None)
                        out.append(note)
    except OSError:
        pass
    return out


def standing(notes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The notes no later note corrects."""
    corrected = {n.get("corrects") for n in notes if n.get("corrects")}
    return [n for n in notes if n.get("id") not in corrected]


def flag_of(notes: List[Dict[str, Any]]) -> str:
    """The run's flag now: the latest standing note's that sets one ("" when none, or cleared)."""
    for n in reversed(standing(notes)):
        if n.get("flag"):
            return "" if n["flag"] == "none" else n["flag"]
    return ""


def tags_of(notes: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for n in standing(notes):
        for t in n.get("tags") or []:
            if t.lower() not in [x.lower() for x in out]:
                out.append(t)
    return out


def add(folder: Path, by: str, text: str = "", flag: str = "", tags: Any = None, corrects: str = "",
        account: Optional[str] = None, ai: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Keep a note on a run; ValueError when it says nothing, or names a flag or a note that isn't one."""
    text = str(text or "").strip()[:MAX_TEXT]
    flag = str(flag or "").strip().lower()
    if flag and flag not in FLAGS + ("none",):
        raise ValueError("a flag is bad, check or key (none clears it)")
    tags = _tags(tags)
    corrects = str(corrects or "").strip()
    if not (text or flag or tags):
        raise ValueError("a note needs a comment, a flag or tags")
    if corrects and corrects not in [n.get("id") for n in read(folder)]:
        raise ValueError("no note %s on this run to correct" % corrects)
    note: Dict[str, Any] = {"id": "n" + secrets.token_hex(4), "at": datetime.now().isoformat(timespec="seconds"),
                            "by": by}
    for k, v in (("account", account), ("ai", ai), ("flag", flag), ("tags", tags), ("text", text),
                 ("corrects", corrects)):
        if v:
            note[k] = v
    append_chained(Path(folder) / FILE, note)
    return note


def line(note: Dict[str, Any]) -> str:
    """A note in a few words: [flag] tags: comment (who, when)."""
    head = "[%s] " % note["flag"] if note.get("flag") and note["flag"] != "none" else \
        "[flag cleared] " if note.get("flag") == "none" else ""
    tags = ", ".join("#" + t for t in note.get("tags") or [])
    who = note.get("account") or ("Kelvin" if note.get("by") == "agent" else "a person")
    said = "%s%s%s" % (head, tags + (": " if tags and note.get("text") else ""), note.get("text") or "")
    return "%s (%s, %s%s)" % (said.strip(), who, str(note.get("at", ""))[:16].replace("T", " "),
                              "; corrects %s" % note["corrects"] if note.get("corrects") else "")
