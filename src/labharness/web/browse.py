"""Kelvin's folder browser: the folders on the Kelvin computer, for a person choosing one in a browser there (a browser
can't give a folder's path itself; the Kelvin app opens the computer's own folder picker instead). Names only, never a
file's contents: a folder's folders with their full paths, its parent and the path to it, the computer's drives and the
home folder, and how many scripts it holds, so the person sees they are in the right one. Hidden and system folders are
left out. A folder that isn't there, or can't be read, answers with the nearest folder above it that can."""
from __future__ import annotations

import os
import re
import stat
import string
from pathlib import Path
from typing import Any, Dict, List, Tuple

MOST = 500                    # folders listed at once; how many more there are is said
SCRIPTS = (".py", ".ipynb")   # a script, as the lab's scripts folder holds them (directly in it)
SYSTEM = {"$recycle.bin", "system volume information"}   # Windows' own, on every drive (a USB stick carries them)
HIDDEN = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 2) | getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 4)


def start() -> Path:
    """Where browsing starts when no folder is given: the person's home folder."""
    try:
        return Path.home()
    except (RuntimeError, KeyError):   # no home to find: the first drive
        return Path(roots()[0])


def roots() -> List[str]:
    """The tops of the computer's folders: on Windows the drives it has now, by their letters alone (none is read, so
    an empty card reader or a network drive that is away costs nothing); elsewhere /."""
    if os.name != "nt":
        return ["/"]
    import ctypes
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return ["%s:\\" % c for i, c in enumerate(string.ascii_uppercase) if mask >> i & 1]


def whole(text: str) -> Path:
    """A path as typed or pasted, made whole: the quotes around it dropped, ~ the home folder, a drive alone (D:) its
    top folder, . and .. taken out; on Windows as Windows reads it (a mapped drive keeps its letter), elsewhere with
    its links followed. ValueError for a path that isn't a full one."""
    text = str(text or "").strip()
    if len(text) > 1 and text[0] == text[-1] == '"':   # Explorer's Copy as path
        text = text[1:-1].strip()
    if "\0" in text:
        raise ValueError("That isn't a folder's path.")
    text = os.path.expanduser(text)
    if os.name == "nt" and re.fullmatch(r"[A-Za-z]:", text):
        text += "\\"
    if not os.path.isabs(text):
        raise ValueError("Give the folder's full path, such as D:\\Data or /Users/me/Data.")
    return Path(os.path.abspath(text) if os.name == "nt" else os.path.realpath(text))


def _hidden(entry: "os.DirEntry[str]") -> bool:
    """What Explorer or Finder doesn't show: a dot name, Windows' own folders on each drive, and on Windows anything
    marked hidden or system (the listing says so itself: nothing more is read)."""
    if entry.name.startswith(".") or entry.name.casefold() in SYSTEM:
        return True
    return os.name == "nt" and bool(entry.stat(follow_symlinks=False).st_file_attributes & HIDDEN)


def _read(folder: Path) -> Tuple[List[str], int]:
    """The names of a folder's folders, as listed, and how many scripts it holds. OSError when it can't be listed."""
    names, scripts = [], 0
    with os.scandir(folder) as found:
        for entry in found:
            try:
                if _hidden(entry):
                    continue
                if entry.is_dir():
                    entry.name.encode("utf-8")   # a name no page can show (bytes that aren't text) is left out
                    names.append(entry.name)
                elif entry.name.lower().endswith(SCRIPTS) and entry.is_file():
                    scripts += 1
            except (OSError, UnicodeError):   # gone meanwhile, or not to be looked at: left out
                continue
    return names, scripts


def _why(e: OSError) -> str:
    return (e.strerror or str(e) or type(e).__name__).rstrip(".")


def listing(asked: str = "") -> Dict[str, Any]:
    """The folders in a folder (asked: its path, "" the home folder): each with its full path, sorted by name whatever
    the case, at most MOST of them (more: how many were left out); the folder's parent (None at a drive's top) and the
    folders on the way to it (parts, for the page's path); how many scripts it holds (.py and .ipynb directly in it);
    the computer's drives (roots) and the home folder. For a folder that isn't there, a file, or a folder that can't be
    read: the nearest folder above it that can, with a note saying so (missing: the path asked, when nothing is
    there). ValueError for a path that isn't a full one, or when no folder on its way can be read."""
    home = start()
    target = whole(asked) if str(asked or "").strip() else home
    where, note, missing = target, None, None
    while True:
        try:
            names, scripts = _read(where)
            break
        except OSError as e:
            if where == target:
                if isinstance(e, FileNotFoundError):
                    note, missing = "No folder at %s." % target, str(target)
                elif isinstance(e, NotADirectoryError):
                    note = "%s is a file: this is its folder." % target
                else:
                    note = "%s can't be read: %s." % (target, _why(e))
            if where.parent == where:
                raise ValueError("%s can't be read: %s." % (where, _why(e)))
            where = where.parent
    names.sort(key=lambda n: (n.casefold(), n))
    return {"path": str(where), "parent": str(where.parent) if where.parent != where else None,
            "parts": [{"name": p.name or p.anchor.rstrip("\\/") or p.anchor, "path": str(p)}
                      for p in [*reversed(where.parents), where]],
            "folders": [{"name": n, "path": str(where / n)} for n in names[:MOST]], "more": max(0, len(names) - MOST),
            "scripts": scripts, "roots": roots(), "home": str(home), "note": note, "missing": missing}
