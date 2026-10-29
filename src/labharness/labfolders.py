"""Which folder each lab keeps its data in (its runs, its record and its reports), when nobody chose one.

Each lab gets a folder of its own named after it, ~/Kelvin/labs/<its name>. ~/Kelvin/virtual-lab is where an older
Kelvin kept its virtual lab's data (Kelvin has no virtual lab since 2026-10-29) and, before that, some real labs'
(F2 l05, l08, l09: people took them for simulated); its runs and reports are still found.

Nothing is ever moved or renamed. A lab whose data are already somewhere keeps them there: a lab that has measured
into virtual-lab before (an older Kelvin did that) goes on using it. The folder each lab uses is recorded
(~/Kelvin/lab-folders.json, by the lab's config file), so a lab that is renamed keeps its folder. A data folder a
person chose in Settings, or an account's own, always comes first (the caller decides that).

Moving a lab's data by hand: close Kelvin, move the folder, and change its line in lab-folders.json to the new place
(or choose the new place in Settings, Data folder, with "Move what is saved now")."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

VIRTUAL = "virtual-lab"
LABS = "labs"
RECORD = "lab-folders.json"
_RESERVED = {"con", "prn", "aux", "nul"} | {"com%d" % i for i in range(1, 10)} | {"lpt%d" % i for i in range(1, 10)}
_lock = threading.Lock()


def safe_name(name: str) -> str:
    """A folder name from a lab's name: letters, digits, spaces made _, . and -; at most 60 characters; never empty,
    never a name Windows keeps for itself."""
    text = re.sub(r"[^\w.-]+", "_", str(name or "").strip()).strip("._-")[:60].rstrip("._-")
    if not text or text.lower() in _RESERVED:
        text = "lab_" + text if text else "lab"
    return text


def _read(home: Path) -> Dict[str, Any]:
    try:
        data = json.loads((home / RECORD).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(home: Path, data: Dict[str, Any]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    tmp = home / (RECORD + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, home / RECORD)


def _place(home: Path, folder: Path) -> str:
    """How the record names a folder: relative to ~/Kelvin when it is inside it, so the record moves with it."""
    try:
        return folder.relative_to(home).as_posix()
    except ValueError:
        return str(folder)


def _measured_there(folder: Path, lab_name: str) -> bool:
    """Has this lab (by name, real instruments) kept runs in that folder already? The catalog says, else the runs'
    own records (the newest few hundred)."""
    db = folder / "catalog.sqlite"
    if db.exists():
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
            try:
                row = con.execute("SELECT 1 FROM runs WHERE lab = ? AND (simulated = 0 OR simulated IS NULL) LIMIT 1",
                                  (lab_name,)).fetchone()
            finally:
                con.close()
            return row is not None
        except sqlite3.Error:
            pass
    runs = folder / "runs"
    if not runs.is_dir():
        return False
    from . import rundata
    try:
        found = sorted((d for d in runs.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True)[:400]
    except OSError:
        return False
    for d in found:
        try:
            meta = rundata.meta(d)
        except Exception:
            continue
        if meta.get("lab") == lab_name and not meta.get("simulated", True):
            return True
    return False


def folder_for(home: Any, lab_name: str, key: Optional[str] = None, record: bool = True) -> Path:
    """The data folder of a lab, by the rules in the module's docstring. key: what the lab is known by across a
    rename (its config file); its name when there is none. record: note the choice (False only looks)."""
    home = Path(home)
    key = str(key or "name:" + str(lab_name))
    with _lock:
        data = _read(home)
        labs = data.setdefault("labs", {})
        known = labs.get(key)
        if isinstance(known, dict) and known.get("folder"):
            folder = Path(known["folder"])
            return folder if folder.is_absolute() else home / folder
        if _measured_there(home / VIRTUAL, lab_name):   # its data are there already: it keeps them there
            folder = home / VIRTUAL
        else:
            base = home / LABS / safe_name(lab_name)
            taken = {str(v.get("folder")) for k, v in labs.items() if k != key and isinstance(v, dict)}
            folder, n = base, 1
            while _place(home, folder) in taken:   # two labs with one name: each its own folder
                n += 1
                folder = base.with_name("%s_%d" % (base.name, n))
        if record:
            labs[key] = {"folder": _place(home, folder), "name": lab_name,
                         "since": datetime.now().isoformat(timespec="seconds")}
            _write(home, data)
        return folder


def all_folders(home: Any) -> list:
    """Every lab's data folder this Kelvin knows of (the old virtual-lab first): where earlier reports and runs are
    found."""
    home = Path(home)
    out = [home / VIRTUAL]
    for v in (_read(home).get("labs") or {}).values():
        if isinstance(v, dict) and v.get("folder"):
            f = Path(v["folder"])
            f = f if f.is_absolute() else home / f
            if f not in out:
                out.append(f)
    return out
