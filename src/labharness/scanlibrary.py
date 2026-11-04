"""The person's library of scans and scripts, for the Runs panel: every plan and every script Kelvin ran, kept by what
it is, so the exact same measurement is a click away; and the scripts a person imported, which may run though they
are outside the lab's scripts folders (a person chose each one; scriptrun.locate).

    library.json, in the data folder (each person's own, beside queue.json):
    {"entries": [{"id", "kind": "plan" | "script", "name", "source": "kelvin" | "you" | "labview" | "specialmeasure",
                  "added", "used", "count", "runs": [run ids, newest first, at most 10], ...}]}

A scan a person kept from LabVIEW (Set up → From your LabVIEW…: a VI translated by viscan.py) is a plan entry with
source "labview", the VI's name (vi) and path; one kept from Special Measure (From Special Measure…: a scan translated
by specialmeasure.py) has source "specialmeasure" and the file it came from; each is theirs, as a script they
imported is.

A plan is kept by the sha256 of its text as it ran (the run's /plan), with a line that says what it does; a script by
its path (resolved), with the values (params) and arguments its last run had, the Python it ran in, and the sha256 of
the file as that run read it, so the panel can say when the file has changed since. Every run notes its entry as it
starts (LabRuntime._noted, through RunStore's hook), and noting never stops a run. The file is written whole, then put
in place; one that can't be read as a library is kept aside (library.json.broken-<time>) and a new one starts, so a
broken file never stops Kelvin.

The portal calls the entries functions (the Sequence's list, as labMaster's): a person gives one a colour and a
note as well as a name (meta), and picks a new one from the scripts in the lab's scripts folders (runnable_files)."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

FILE = "library.json"
MAX_RUNS = 10       # the runs an entry lists, newest first
MAX_ENTRIES = 400   # beyond this, the entries Kelvin noted itself go, least recently used first (a person's stay)
MAX_NAME = 80
MAX_NOTE = 300
MAX_FILES = 1000    # the scripts runnable_files lists at most
MAX_LOOKED = 50000  # and the files and folders it looks through at most (a scripts folder set to a whole drive)
# folders whose scripts are no lab's own: environments, caches, packages (and every hidden folder: .git, .venv...)
SKIP_DIRS = {".git", ".venv", "venv", "env", "__pycache__", "node_modules", "site-packages"}
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_LOCK = threading.RLock()   # every library file, one change at a time (runs start on several threads)


class Unreadable(OSError):
    """library.json is there but can't be read now (the computer refused): nothing is written over it."""


def plan_id(text: str) -> str:
    return "plan-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def script_key(path: Any) -> str:
    """A script's path as the library keeps it: resolved, and in one case where the file system has none."""
    return os.path.normcase(str(Path(str(path)).expanduser().resolve()))


def script_id(path: Any) -> str:
    return "script-" + hashlib.sha256(script_key(path).encode("utf-8")).hexdigest()[:16]


def _num(x: float) -> str:
    return "%.4g" % x


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _span(values: List[Any], unit: str) -> str:
    """An axis's values in a few words: "0 → 3 V", there and back "0 → 3 → 0 V", or the values themselves."""
    tail = " " + unit if unit else ""
    if values and all(_number(v) for v in values):
        if len(values) == 1:
            return _num(values[0]) + tail
        first, last = values[0], values[-1]
        turn = max(values, key=lambda v: abs(v - first))
        mid = " → " + _num(turn) if turn not in (first, last) else ""
        return "%s%s → %s%s" % (_num(first), mid, _num(last), tail)
    shown = [str(v) for v in values[:4]]
    return ", ".join(shown) + (" …" if len(values) > 4 else "")


def summary(plan: Dict[str, Any], unit_of: Callable[[str], str] = lambda key: "") -> str:
    """A scan in one line: what it sweeps, how many points, what it measures ("smu.V 0 → 3 V, 61 points · lockin.X,
    smu.I"). unit_of(key): a parameter's unit in this lab ("" when unknown)."""
    from .plans import load_plan
    try:
        p = load_plan(plan)
    except Exception:
        return ""
    swept = [a for a in p.axes if a.kind in ("instrument", "ramp", "technique", "variable")]
    parts: List[str] = []
    for a in p.axes:
        if a.kind == "repeat":
            parts.append("%d repeats" % len(a.values))
        elif a.kind == "time":
            parts.append("every %s s" % _num(a.every))
        elif a.kind == "ramp":
            parts.append("%s ramp → %s" % (a.key, " → ".join(_span([v], unit_of(a.key)) for v in a.values)))
        else:   # what moves with it, after it: "stage.y 70 → 72 µm with stage.x 93 → 81 µm"
            parts.append("%s %s%s%s" % (a.key, _span(a.values, unit_of(a.key)),
                                        "".join(" with %s %s" % (w.key, _span(w.values, unit_of(w.key)))
                                                for w in a.with_),
                                        " (%d)" % len(a.values) if len(swept) > 1 else ""))
    text = " × ".join(parts)
    if p.sets:   # what the variables set: "n 0 → 2 × E -0.3 → 0.3, setting dac.Vbg, dac.Vtg"
        text += ", setting %s" % ", ".join(c.key for c in p.sets)
    if p.ramp is None:
        text += "%s%d point%s" % (", " if text else "", p.points, "" if p.points == 1 else "s")
    keys = ["%s.%s" % m for m in p.measure] + [t.key for t in p.techniques] + \
        ["%s.%s" % m for a in p.axes for m in a.measure]
    if keys:
        text += " · " + ", ".join(keys[:4]) + (" +%d" % (len(keys) - 4) if len(keys) > 4 else "")
    return text


def plain_name(name: Any) -> str:
    """A name a person gives an entry: one line, not empty, at most MAX_NAME characters (ValueError otherwise)."""
    text = " ".join(str(name or "").split())
    if not text:
        raise ValueError("give it a name")
    if len(text) > MAX_NAME:
        raise ValueError("a name is at most %d characters" % MAX_NAME)
    return text


def plain_color(color: Any) -> str:
    """A colour a person gives an entry: #rrggbb (in lower case), or "" for none (ValueError otherwise)."""
    text = str(color or "").strip()
    if text and not COLOR.match(text):
        raise ValueError("a colour is #rrggbb, such as #2f6fb0")
    return text.lower()


def plain_note(note: Any) -> str:
    """A note a person gives an entry: one line, at most MAX_NOTE characters, "" for none (ValueError otherwise)."""
    text = " ".join(str(note or "").split())
    if len(text) > MAX_NOTE:
        raise ValueError("a note is at most %d characters" % MAX_NOTE)
    return text


def _link(path: Any) -> bool:
    """A link, or a junction (Windows' link to a folder): either may lead out of the folder it is in."""
    try:
        st = os.lstat(path)
    except OSError:
        return True
    return stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0) == 0xA0000003   # IO_REPARSE_TAG_MOUNT_POINT


def runnable_files(root: Path, most: int = MAX_FILES) -> Tuple[List[Dict[str, str]], bool]:
    """The scripts Kelvin can run in a folder and its sub-folders (.py, .ipynb), SKIP_DIRS, hidden folders, links and
    junctions left out (what one leads to is listed where it is, if that is a folder Kelvin runs from: scriptrun.locate
    runs nothing reached out of them): ([{path, rel (with /), name, kind: py | ipynb}], at most `most`, by their path
    in it; whether there were more). Read only: no file is opened."""
    found: List[Dict[str, str]] = []
    more, looked = False, 0
    try:
        root = Path(root)
        if not root.is_dir():
            return [], False
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")
                                 and not _link(os.path.join(dirpath, d)))
            looked += len(dirnames) + len(filenames)
            for name in sorted(filenames):
                kind = os.path.splitext(name)[1].lower().lstrip(".")
                if kind not in ("py", "ipynb") or name.startswith("."):
                    continue
                path = Path(dirpath) / name
                if _link(path):
                    continue
                found.append({"path": str(path), "rel": path.relative_to(root).as_posix(), "name": name,
                              "kind": kind})
            if len(found) > most or looked > MAX_LOOKED:
                more = True
                break
    except OSError:
        pass
    found.sort(key=lambda f: (f["rel"].lower(), f["rel"]))
    if len(found) > most:
        found, more = found[:most], True
    return found, more


def shares(sizes: List[int], most: int) -> List[int]:
    """How many of each of several lists to keep, at most `most` in all: one of each in turn while there is room, so a
    long list leaves the others room, and a short one gives them what it doesn't use (the first ones first)."""
    room = [0] * len(sizes)
    while most > 0 and any(r < s for r, s in zip(room, sizes)):
        for i, s in enumerate(sizes):
            if most > 0 and room[i] < s:
                room[i] += 1
                most -= 1
    return room


class Library:
    """The person's library in one file (library.json). Thread-safe: each change reads, changes and writes the whole
    file under one lock."""

    def __init__(self, path: Any):
        self.path = Path(path)

    # ------------------------------------------------------------------ the file

    def _read(self) -> Dict[str, Dict[str, Any]]:
        """The entries by id. A file that is not a library is kept aside and an empty one starts; Unreadable when the
        computer refuses to read it (nothing is then written over it)."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as e:
            raise Unreadable("Kelvin could not read %s (%s)" % (self.path, e.strerror or e)) from None
        try:
            data = json.loads(raw.decode("utf-8"))
            entries = data["entries"]
            if not isinstance(entries, list):
                raise ValueError("entries is not a list")
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            self._set_aside()
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for e in entries:
            if isinstance(e, dict) and isinstance(e.get("id"), str) and e.get("kind") in ("plan", "script"):
                out[e["id"]] = e
        return out

    def _set_aside(self) -> None:
        """A broken library.json, kept beside as library.json.broken-<time> for a person to look at."""
        aside = self.path.with_name("%s.broken-%s" % (self.path.name, time.strftime("%Y%m%d-%H%M%S")))
        try:
            os.replace(self.path, aside)
        except OSError:
            pass

    def _write(self, entries: Dict[str, Dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"entries": list(entries.values())}, indent=1, ensure_ascii=False, default=str),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ reading

    def entries(self) -> List[Dict[str, Any]]:
        """Every entry, the latest used (or added) first."""
        with _LOCK:
            found = self._read()
        return sorted(found.values(), key=lambda e: -(e.get("used") or e.get("added") or 0))

    def get(self, entry_id: str) -> Dict[str, Any]:
        with _LOCK:
            found = self._read()
        if entry_id not in found:
            raise KeyError("nothing called %r in the library" % entry_id)
        return found[entry_id]

    def chosen(self) -> List[str]:
        """The scripts a person imported: they may run though they are outside the lab's scripts folders."""
        try:
            return [str(e["path"]) for e in self.entries()
                    if e.get("kind") == "script" and e.get("source") == "you" and e.get("path")]
        except Unreadable:
            return []

    # ------------------------------------------------------------------ what runs note

    def note_plan(self, text: str, name: str, about: str, run_id: str, when: Optional[float] = None,
                  marks: Optional[Dict[str, Any]] = None, under: Optional[str] = None) -> Dict[str, Any]:
        """A plan that starts a run: its entry, made or brought up to date. marks: whose work the run is (its project,
        chat and folder, and the marks of unshared work), which a run from the library keeps. under: the entry it
        runs again (a scan run again with its settings restored first, whose text is the scan's own and more): its
        runs are that entry's while it is there."""
        entry_id = under if under and under.startswith("plan-") else plan_id(text)
        with _LOCK:
            found = self._read()
            if entry_id not in found:
                entry_id = plan_id(text)
            entry = found.get(entry_id) or {
                "id": entry_id, "kind": "plan", "name": name or "plan", "source": "kelvin",
                "added": when or time.time(), "used": None, "count": 0, "runs": [],
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text, "summary": about}
            if about and not entry.get("summary"):
                entry["summary"] = about
            entry["marks"] = dict(marks or {})
            self._ran(entry, run_id, when)
            found[entry_id] = entry
            self._keep(found)
            self._write(found)
            return entry

    def note_script(self, path: Any, run_id: str, params: Optional[Dict[str, Any]] = None,
                    args: Optional[List[Any]] = None, python: Optional[str] = None, sha256: Optional[str] = None,
                    when: Optional[float] = None, marks: Optional[Dict[str, Any]] = None,
                    beside: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """A script that starts a run: its entry, with what that run had (the values a person changed, its
        arguments, Python, the sha256 of the file as it read it, the Python files beside it, hashed, and whose work it
        was: marks, as note_plan)."""
        entry_id = script_id(path)
        with _LOCK:
            found = self._read()
            entry = found.get(entry_id) or self._new_script(path, "kelvin", when)
            entry.update(params=dict(params or {}), args=[str(a) for a in args or []], python=python or None,
                         sha256=sha256 or None, marks=dict(marks or {}),
                         beside=dict(beside) if isinstance(beside, dict) else None)
            self._ran(entry, run_id, when)
            found[entry_id] = entry
            self._keep(found)
            self._write(found)
            return entry

    @staticmethod
    def _new_script(path: Any, source: str, when: Optional[float] = None) -> Dict[str, Any]:
        resolved = Path(str(path)).expanduser().resolve()
        return {"id": script_id(resolved), "kind": "script", "name": resolved.name, "source": source,
                "added": when or time.time(), "used": None, "count": 0, "runs": [], "path": str(resolved),
                "params": {}, "args": [], "python": None, "sha256": None}

    @staticmethod
    def _ran(entry: Dict[str, Any], run_id: str, when: Optional[float]) -> None:
        entry["used"] = when or time.time()
        entry["count"] = int(entry.get("count") or 0) + 1
        entry["runs"] = ([run_id] + [r for r in entry.get("runs") or [] if r != run_id])[:MAX_RUNS]

    @staticmethod
    def _keep(found: Dict[str, Dict[str, Any]]) -> None:
        """At most MAX_ENTRIES: the entries Kelvin noted itself go first, least recently used first; a script a person
        imported, a scan they kept from LabVIEW or Special Measure, or an entry they named, stays."""
        extra = len(found) - MAX_ENTRIES
        if extra <= 0:
            return
        spare = sorted((e for e in found.values() if e.get("source") not in ("you", "labview", "specialmeasure")
                        and not e.get("named")
                        and not e.get("color") and not e.get("note")),   # one a person coloured or noted stays too
                       key=lambda e: e.get("used") or e.get("added") or 0)
        for e in spare[:extra]:
            found.pop(e["id"], None)

    # ------------------------------------------------------------------ a person's changes

    def keep_scan(self, text: str, name: Any, about: str, vi: str, path: str, source: str = "labview") -> Dict[str, Any]:
        """A scan a person keeps before it ever ran: a LabVIEW VI translated into a plan (viscan.py), or a Special
        Measure scan (specialmeasure.py; source "specialmeasure"), kept by its plan's text as every scan is (its runs
        are noted under it), with its source and the file it came from (vi: its name; path). One already in the
        library keeps its runs and its name, and becomes theirs."""
        entry_id = plan_id(text)
        with _LOCK:
            found = self._read()
            entry = found.get(entry_id) or {
                "id": entry_id, "kind": "plan", "name": plain_name(name), "added": time.time(), "used": None,
                "count": 0, "runs": [], "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text,
                "summary": about}
            entry.update(source=source, vi=str(vi), path=str(path))
            found[entry_id] = entry
            self._keep(found)
            self._write(found)
            return entry

    def add_script(self, path: Any) -> Dict[str, Any]:
        """A script a person imported (its path, checked by the caller): it may run from now on. One already in the
        library (Kelvin ran it from the scripts folders) keeps its runs and becomes theirs."""
        entry_id = script_id(path)
        with _LOCK:
            found = self._read()
            entry = found.get(entry_id) or self._new_script(path, "you")
            entry["source"] = "you"
            found[entry_id] = entry
            self._write(found)
            return entry

    def add_plan(self, text: str, name: Any, about: str, edited_from: Optional[str] = None,
                 marks: Optional[Dict[str, Any]] = None, when: Optional[float] = None) -> Tuple[Dict[str, Any], bool]:
        """A scan a person made or edited (the scan editor's Save): an entry of its own, theirs (it stays when Kelvin
        makes room), named as they named it; edited_from: the name of the scan it was edited from; marks: whose work
        that one's runs were, which its runs keep. The very same scan already there stays as it was, theirs from now
        on. (entry, whether it is new); ValueError: not a name."""
        entry_id = plan_id(text)
        with _LOCK:
            found = self._read()
            entry = found.get(entry_id)
            new = entry is None
            if new:
                entry = {"id": entry_id, "kind": "plan", "name": plain_name(name), "source": "you",
                         "added": when or time.time(), "used": None, "count": 0, "runs": [],
                         "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text, "summary": about,
                         "marks": dict(marks or {})}
                if edited_from:
                    entry["edited_from"] = str(edited_from)
            entry["source"] = "you"
            found[entry_id] = entry
            self._keep(found)
            self._write(found)
            return entry, new

    def rename(self, entry_id: str, name: Any) -> Dict[str, Any]:
        text = plain_name(name)
        with _LOCK:
            found = self._read()
            if entry_id not in found:
                raise KeyError("nothing called %r in the library" % entry_id)
            found[entry_id].update(name=text, named=True)
            self._write(found)
            return found[entry_id]

    def meta(self, entry_id: str, name: Any = None, color: Any = None, note: Any = None) -> Dict[str, Any]:
        """A person's name, colour and note for an entry, each only when given (not None): a colour is #rrggbb, and ""
        takes the colour or the note off. KeyError: no such entry; ValueError: not a name, a colour or a note (then
        nothing changes)."""
        text = plain_name(name) if name is not None else None
        shade = plain_color(color) if color is not None else None
        words = plain_note(note) if note is not None else None
        with _LOCK:
            found = self._read()
            if entry_id not in found:
                raise KeyError("nothing called %r in the library" % entry_id)
            entry = found[entry_id]
            if text is not None:
                entry.update(name=text, named=True)
            for key, value in (("color", shade), ("note", words)):
                if value:
                    entry[key] = value
                elif value is not None:
                    entry.pop(key, None)
            self._write(found)
            return entry

    def remove(self, entry_id: str) -> Dict[str, Any]:
        """Out of the library (its runs stay). A script a person imported may no longer run from outside the scripts
        folder."""
        with _LOCK:
            found = self._read()
            if entry_id not in found:
                raise KeyError("nothing called %r in the library" % entry_id)
            entry = found.pop(entry_id)
            self._write(found)
            return entry


def params_from_changes(changes: Any) -> Dict[str, Any]:
    """The values a script run had, from the lines its record keeps ("VMAX = 2.0 (the script has 1.0)"), for runs made
    before Kelvin kept them as values too."""
    import ast
    out: Dict[str, Any] = {}
    for line in changes or []:
        m = re.match(r"^([A-Za-z_]\w*) = (.*) \(the script has .*\)$", str(line), re.S)
        if not m:
            continue
        try:
            out[m.group(1)] = ast.literal_eval(m.group(2))
        except (ValueError, SyntaxError):
            continue
    return out
