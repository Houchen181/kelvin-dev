"""Other programs on this computer that may be holding an instrument: another Kelvin, or a program labs drive
instruments with (MATLAB, LabVIEW, NI MAX, LightField). Said when an instrument is busy, a camera is open elsewhere or
a GPIB bus stops answering, so a person knows where to look: a field test's "no other program holds the instrument"
was about Kelvin itself, an earlier copy of it still closing."""
from __future__ import annotations

import os
import threading
import time
from typing import List, Tuple

# what labs drive instruments with, by the program's file name (lower case)
PROGRAMS = {"matlab.exe": "MATLAB", "labview.exe": "LabVIEW", "nimax.exe": "NI MAX", "lightfield.exe": "LightField",
            "igor64.exe": "Igor Pro", "igor.exe": "Igor Pro"}
HELPERS = ("--bridge", "--screen-mcp", "--self-test", "--remove-data")   # Kelvin.exe started as a helper, not a Kelvin
KEEP_S = 10.0   # how long one look holds: a busy instrument is often asked about several times in a row

_lock = threading.Lock()
_last: Tuple[float, List[str]] = (0.0, [])


def other_kelvins() -> List[int]:
    """The other Kelvins running on this computer, by process id: not this one, its helpers, nor its children."""
    try:
        import psutil
    except ImportError:
        return []
    try:
        me = psutil.Process()
        mine = {me.pid} | {c.pid for c in me.children(recursive=True)}
        if me.ppid():
            mine.add(me.ppid())   # this Kelvin's own parent (a helper asking)
    except Exception:
        return []
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() != "kelvin.exe" or p.info["pid"] in mine:
                continue
            if any(h in (p.info["cmdline"] or []) for h in HELPERS):
                continue
            out.append(int(p.info["pid"]))
        except Exception:
            continue
    return sorted(out)


def holders() -> List[str]:
    """Other programs running now that may be holding an instrument, in words: ["another Kelvin (process 15668)",
    "MATLAB"]. [] when none (or this computer can't say)."""
    global _last
    with _lock:
        if time.monotonic() - _last[0] < KEEP_S:
            return list(_last[1])
    out = ["another Kelvin (process %d)" % pid for pid in other_kelvins()]
    try:
        import psutil
        seen = set()
        for p in psutil.process_iter(["name"]):
            label = PROGRAMS.get((p.info.get("name") or "").lower())
            if label and label not in seen and p.pid != os.getpid():
                seen.add(label)
        out += sorted(seen)
    except Exception:
        pass
    with _lock:
        _last = (time.monotonic(), out)
    return list(out)


def holders_text() -> str:
    """"; running now: another Kelvin (process 15668), MATLAB", or "" when none of them is."""
    found = holders()
    return "; running now: %s" % ", ".join(found) if found else ""
