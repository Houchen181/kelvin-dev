"""BioLogic's EC-Lab and BT-Lab files: .mpt, the text export, and .mpr, the binary file EC-Lab writes as it measures.

.mpt: a first line "EC-Lab ASCII FILE" (or BT-Lab), then "Nb header lines : N", the settings (N - 3 lines), the
columns' names (tab-separated: "time/s", "Ewe/V", "<I>/mA", "freq/Hz"...) and the points, numbers perhaps with a
decimal comma.

.mpr: a series of modules, each "MODULE", a short and a long name, its length, version and date, then its data. The
data module ("VMP data") holds the number of points and of columns, each column's type (an id), then the points as
records of a fixed size, laid out by those ids. BioLogic doesn't publish the ids: the ones here, and their sizes, are
those galvani (the long-used open reader, github.com/echemdata/galvani) lists. Kelvin reads only ids it is sure of,
and checks that the records fill the module exactly and that time runs forward, so a file with a column it doesn't
know, or a layout that doesn't add up, is refused with a word to export it from EC-Lab as text (.mpt), never read
with its numbers shifted."""
from __future__ import annotations

import re
import struct
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..imports import (DataError, MAX_ROWS, Table, key_of, number_of, parse_time, table_from_columns,
                       table_from_rows, unit_of)

# technique names as EC-Lab writes them in a file's settings: (in Kelvin's words, its x)
TECHNIQUES = {
    "cyclic voltammetry": ("cyclic voltammetry", "potential"),
    "cyclic voltammetry advanced": ("cyclic voltammetry", "potential"),
    "linear sweep voltammetry": ("linear sweep voltammetry", "potential"),
    "linear polarization": ("linear polarization", "potential"),
    "differential pulse voltammetry": ("differential pulse voltammetry", "potential"),
    "square wave voltammetry": ("square wave voltammetry", "potential"),
    "potentio electrochemical impedance spectroscopy": ("impedance spectroscopy (PEIS)", "frequency"),
    "galvano electrochemical impedance spectroscopy": ("impedance spectroscopy (GEIS)", "frequency"),
    "staircase potentio electrochemical impedance spectroscopy": ("impedance spectroscopy (SPEIS)", "frequency"),
    "staircase galvano electrochemical impedance spectroscopy": ("impedance spectroscopy (SGEIS)", "frequency"),
    "galvanostatic cycling with potential limitation": ("galvanostatic cycling (GCPL)", "time"),
    "potentiodynamic cycling with galvanostatic acceleration": ("potentiodynamic cycling (PCGA)", "time"),
    "chronoamperometry / chronocoulometry": ("chronoamperometry", "time"),
    "chronoamperometry": ("chronoamperometry", "time"),
    "chronopotentiometry": ("chronopotentiometry", "time"),
    "open circuit voltage": ("open circuit voltage", "time"),
    "constant current": ("constant current", "time"),
    "constant voltage": ("constant voltage", "time"),
    "modulo bat": ("Modulo Bat", "time"),
}
X_KEYS = {"potential": ["ewe", "ewe_mean", "control", "time"], "frequency": ["freq", "time"], "time": ["time"]}
FLAGS_LEFT_OUT = ["control_changes", "ns_changes", "counter_inc"]   # markers of where a step changed: not data


def _x_for(techniques: List[Tuple[str, str]]) -> List[str]:
    """One technique: its own x (a voltammogram on its potential, a spectrum on its frequency); linked ones, time."""
    kinds = {x for _, x in techniques}
    return X_KEYS[kinds.pop()] if len(kinds) == 1 else X_KEYS["time"]


def _title(name: str) -> str:
    """EC-Lab's "control/V/mA" is in V or in mA (the technique says which): no one unit."""
    return "control" if name.strip() == "control/V/mA" else name


# ------------------------------------------------------------------ .mpt

def read_mpt(p: Path, text: str) -> Table:
    lines = text.splitlines()
    m = re.match(r"\s*Nb header lines\s*:\s*(\d+)", lines[1] if len(lines) > 1 else "")
    if not m:
        raise DataError("%s: an EC-Lab text file without its header's length (Nb header lines)" % p.name)
    n = int(m.group(1))
    if n < 3 or n > len(lines):
        raise DataError("%s: an EC-Lab text file whose header doesn't fit it (%d lines)" % (p.name, n))
    settings, names = lines[2:n - 1], lines[n - 1].split("\t")
    techniques = [TECHNIQUES[s.strip().lower()] for s in settings if s.strip().lower() in TECHNIQUES]
    rows = [ln.split("\t") for ln in lines[n:] if ln.strip()]
    table = table_from_rows(key_of(p.stem, "data"), p.name, rows, comma=True, header=[_title(c) for c in names],
                            prefer=_x_for(techniques), drop=FLAGS_LEFT_OUT, grid=False)
    table.notes.insert(0, "%s: BioLogic EC-Lab text%s%s" % (
        p.name, ", " + _what(techniques) if techniques else "", "".join("; " + s for s in _settings(settings))))
    return _finish(table, p.name)


def _what(techniques: List[Tuple[str, str]]) -> str:
    words = list(dict.fromkeys(w for w, _ in techniques))
    return words[0] if len(words) == 1 else "linked techniques: " + ", ".join(words)


def _settings(lines: List[str]) -> List[str]:
    """The settings that are numbers (when it started, the electrode's area, the active mass), in Kelvin's words."""
    out = []
    for ln in lines:
        key, sep, value = ln.partition(" : ")
        key, value = key.strip().lower(), value.strip()
        if not sep:
            continue
        if key == "acquisition started on":
            t = parse_time(value)
            if t is not None:
                out.append("started %s" % t.isoformat(sep=" ", timespec="seconds"))
        elif key in ("electrode surface area", "characteristic mass", "mass of active material"):
            m = re.match(r"^(\S+)\s*(\S*)$", value)
            number = number_of(m.group(1), comma=True) if m else None
            if number is not None:
                out.append("%s %g %s" % ("electrode area" if "area" in key else "active mass", number,
                                         unit_of(m.group(2))))
    return [s.strip() for s in out]


FLAGS = ("mode", "ox_red", "error")   # EC-Lab's flags for each point: kept, after the measured columns
LATER = ("time", "step_time", "control")   # the clock and the set point: after what was measured


def _finish(table: Table, name: str) -> Table:
    """The measured columns first (a voltammogram's first plot is its current), then the time and the set point,
    then EC-Lab's flags; the points it flagged as in error said, as they are kept; and no automatic fits (they are
    made for Kelvin's sweeps)."""
    def rank(key: str) -> int:
        short = key.rsplit(".", 1)[-1]
        return 2 if short in FLAGS else 1 if short in LATER else 0
    flags = {k: v for k, v in table.columns.items() if rank(k) == 2}
    table.columns = dict(sorted(table.columns.items(), key=lambda kv: rank(kv[0])))   # stable: the file's order
    for key, (_, values) in flags.items():
        bad = sum(1 for v in values if v == v and v) if key.endswith(".error") else 0
        if bad:
            table.notes.append("%s: EC-Lab flagged %d point%s as in error (column %s)" % (
                name, bad, "" if bad == 1 else "s", key))
    table.fits = False
    return table


# ------------------------------------------------------------------ .mpr

MAGIC = b"BIO-LOGIC MODULAR FILE\x1a".ljust(48) + b"\x00\x00\x00\x00"
# column id: (EC-Lab's name for it, how it is stored). galvani's table, less the ids it marks unsure.
COLUMNS: Dict[int, Tuple[str, str]] = {
    4: ("time/s", "<f8"), 5: ("control/V/mA", "<f4"), 6: ("Ewe/V", "<f4"), 7: ("dQ/mA.h", "<f8"),
    8: ("I/mA", "<f4"), 9: ("Ece/V", "<f4"), 11: ("I/mA", "<f8"), 13: ("(Q-Qo)/mA.h", "<f8"),
    16: ("Analog IN 1/V", "<f4"), 19: ("control/V", "<f4"), 20: ("control/mA", "<f4"), 23: ("dQ/mA.h", "<f8"),
    24: ("cycle number", "<f8"), 26: ("Rapp/Ohm", "<f4"), 32: ("freq/Hz", "<f4"), 33: ("|Ewe|/V", "<f4"),
    34: ("|I|/A", "<f4"), 35: ("Phase(Z)/deg", "<f4"), 36: ("|Z|/Ohm", "<f4"), 37: ("Re(Z)/Ohm", "<f4"),
    38: ("-Im(Z)/Ohm", "<f4"), 39: ("I Range", "<u2"), 69: ("R/Ohm", "<f4"), 70: ("P/W", "<f4"),
    74: ("Energy/W.h", "<f8"), 75: ("Analog OUT/V", "<f4"), 76: ("<I>/mA", "<f4"), 77: ("<Ewe>/V", "<f4"),
    78: ("Cs-2/µF-2", "<f4"), 96: ("|Ece|/V", "<f4"), 98: ("Phase(Zce)/deg", "<f4"), 99: ("|Zce|/Ohm", "<f4"),
    100: ("Re(Zce)/Ohm", "<f4"), 101: ("-Im(Zce)/Ohm", "<f4"), 123: ("Energy charge/W.h", "<f8"),
    124: ("Energy discharge/W.h", "<f8"), 125: ("Capacitance charge/µF", "<f8"),
    126: ("Capacitance discharge/µF", "<f8"), 131: ("Ns", "<u2"), 163: ("|Estack|/V", "<f4"),
    168: ("Rcmp/Ohm", "<f4"), 169: ("Cs/µF", "<f4"), 172: ("Cp/µF", "<f4"), 173: ("Cp-2/µF-2", "<f4"),
    174: ("<Ewe>/V", "<f4"), 434: ("(Q-Qo)/C", "<f4"), 435: ("dQ/C", "<f4"), 441: ("<Ece>/V", "<f4"),
    462: ("Temperature/°C", "<f4"), 467: ("Q charge/discharge/mA.h", "<f8"), 468: ("half cycle", "<u4"),
    469: ("z cycle", "<u4"), 471: ("<Ece>/V", "<f4"), 498: ("Q charge/mA.h", "<f8"),
    499: ("Q discharge/mA.h", "<f8"), 500: ("step time/s", "<f8"), 501: ("Efficiency/%", "<f8"),
    502: ("Capacity/mA.h", "<f8"),
}
# ids of flags, all packed in one byte (where the first of them stands): (name, its bits)
FLAG_BITS: Dict[int, Tuple[str, int]] = {1: ("mode", 0x03), 2: ("ox/red", 0x04), 3: ("error", 0x08),
                                         21: ("control changes", 0x10), 31: ("Ns changes", 0x20),
                                         65: ("counter inc.", 0x80)}
EXPORT = "export it from EC-Lab as text (.mpt) and import that"


def _modules(data: bytes, name: str) -> List[Dict[str, Any]]:
    out = []
    pos = len(MAGIC)
    while pos < len(data):
        if data[pos:pos + 6] != b"MODULE":
            raise DataError("%s: its modules don't follow one another as an .mpr's do; %s" % (name, EXPORT))
        pos += 6
        if data[pos + 35:pos + 39] == b"\xff\xff\xff\xff":   # EC-Lab 11.50 and later: a longer module header
            fields = struct.unpack_from("<10s25sIIII8s", data, pos)
            short, _, _, length, version, _, date = fields
            pos += 59
        else:
            if pos + 51 > len(data):
                raise DataError("%s is cut short" % name)
            short, _, length, version, date = struct.unpack_from("<10s25sII8s", data, pos)
            pos += 51
        if pos + length > len(data):
            raise DataError("%s is cut short (its %s module)" % (name, short.decode("ascii", "replace").strip()))
        out.append({"name": short.decode("ascii", "replace").strip(), "version": version, "date": date,
                    "data": data[pos:pos + length]})
        pos += length
    return out


def read_mpr(p: Path) -> Table:
    import numpy as np
    data = p.read_bytes()
    if data[:len(MAGIC)] != MAGIC:
        raise DataError("%s is not an EC-Lab .mpr file" % p.name)
    try:
        modules = _modules(data, p.name)
    except struct.error:
        raise DataError("%s is cut short" % p.name)
    found = [m for m in modules if m["name"] == "VMP data"]
    if len(found) != 1:
        raise DataError("%s: %s data module; %s" % (p.name, "no" if not found else "more than one", EXPORT))
    module = found[0]
    body, version = module["data"], module["version"]
    if len(body) < 5:
        raise DataError("%s is cut short (its data module)" % p.name)
    points, ncols = struct.unpack_from("<IB", body, 0)
    if version == 0:
        ids, start, idsize = list(body[5:5 + ncols]), 100, 1
    elif version in (2, 3):
        ids, start, idsize = list(struct.unpack_from("<%dH" % ncols, body, 5)), 405 if version == 2 else 406, 2
    else:
        raise DataError("%s: a data module of version %d, which Kelvin doesn't read; %s" % (p.name, version, EXPORT))
    if any(body[5 + idsize * ncols:405 if version else 100]):
        raise DataError("%s: its data module's header isn't laid out as Kelvin knows it; %s" % (p.name, EXPORT))
    unknown = [i for i in ids if i not in COLUMNS and i not in FLAG_BITS]
    if unknown:
        raise DataError("%s has a column Kelvin doesn't know how EC-Lab stores (type %s); %s" % (
            p.name, ", ".join(str(i) for i in unknown[:5]), EXPORT))
    layout: List[Tuple[str, str]] = []
    flags: List[Tuple[str, int]] = []
    titles: List[str] = []
    for i in ids:
        if i in FLAG_BITS:
            if not flags:
                layout.append(("flags", "u1"))
            flags.append(FLAG_BITS[i])
        else:
            layout.append(("c%d" % len(layout), COLUMNS[i][1]))
            titles.append(COLUMNS[i][0])
    record = np.dtype(layout)
    rows = body[start:]
    if record.itemsize * points != len(rows):
        raise DataError("%s: its %d points don't fill its data module as its columns say they should; %s" % (
            p.name, points, EXPORT))
    if points > MAX_ROWS:
        raise DataError("%s has more than %d rows" % (p.name, MAX_ROWS))
    array = np.frombuffer(rows, dtype=record, count=points)
    named: List[Tuple[str, Any]] = []
    k = 0
    for field in record.names:
        if field == "flags":
            for title, bits in flags:
                shift = (bits & -bits).bit_length() - 1
                named.append((title, (array["flags"] & bits) >> shift))
        else:
            named.append((_title(titles[k]), array[field]))
            k += 1
    times = next((v for t, v in named if t == "time/s"), None)
    if times is not None and points > 1:
        steps = np.diff(times.astype(float))
        if not np.all(np.isfinite(times)) or np.sum(steps < 0) > 0.01 * points:
            raise DataError("%s: its times don't run forward as read, so its layout isn't the one Kelvin knows; %s"
                            % (p.name, EXPORT))
    x = ["freq", "time"] if any(t == "freq/Hz" for t, _ in named) else ["time"]
    table = table_from_columns(key_of(p.stem, "data"), p.name, named, prefer=x, drop=FLAGS_LEFT_OUT)
    when = _date(module["date"])
    table.notes.insert(0, "%s: BioLogic EC-Lab binary (.mpr), %d points%s" % (
        p.name, points, ", recorded on %s" % when if when else ""))
    return _finish(table, p.name)


def _date(raw: bytes) -> Optional[str]:
    """A module's date, as EC-Lab writes it (mm/dd/yy)."""
    try:
        return datetime.strptime(raw.decode("ascii").strip(), "%m/%d/%y").date().isoformat()
    except (UnicodeDecodeError, ValueError):
        return None
