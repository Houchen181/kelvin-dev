"""Gamry Framework's .dta files: a tab-separated text file that starts "EXPLAIN", names its experiment ("TAG CV",
"TAG EISPOT"...), lists its settings (KEY, type, value, label: "SCANRATE QUANT 100 Scan Rate (mV/s)") and holds its
data as tables: a line "CURVE TABLE" (or CURVE1, CURVE2 for a voltammogram's cycles, ZCURVE for an impedance
spectrum, OCVCURVE for the open-circuit stretch before), then the columns' names and units, then a row per point,
each line starting with a tab.

The experiment's tables become one run: a voltammogram's cycles one after another (with a curve column saying which),
on its potential; an impedance spectrum on its frequency; anything else on its time. The open-circuit stretch before a
measurement is left out, and said so."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple

from ..imports import DataError, Table, key_of, number_of, parse_time, table_from_rows, unit_of

# experiments by their TAG: (in Kelvin's words, the keys of its x, in order of preference)
TAGS = {
    "CV": ("cyclic voltammetry", ["vf", "t"]),
    "LSV": ("linear sweep voltammetry", ["vf", "t"]),
    "POTENTIODYNAMIC": ("potentiodynamic scan", ["vf", "t"]),
    "POTDYN": ("potentiodynamic scan", ["vf", "t"]),
    "CYCPOL": ("cyclic polarization", ["vf", "t"]),
    "LPR": ("polarization resistance", ["vf", "t"]),
    "TAFEL": ("Tafel scan", ["vf", "t"]),
    "EISPOT": ("impedance spectroscopy (potentiostatic)", ["freq", "time"]),
    "EISGALV": ("impedance spectroscopy (galvanostatic)", ["freq", "time"]),
    "CORPOT": ("open-circuit potential", ["t", "time"]),
    "CHRONOA": ("chronoamperometry", ["t", "time"]),
    "CHRONOP": ("chronopotentiometry", ["t", "time"]),
    "CHRONOC": ("chronocoulometry", ["t", "time"]),
}
MAIN = re.compile(r"^(?:Z?CURVE\d*)$")      # the measurement's own tables
LEFT_OUT = ["pt"]                            # the point's number in its curve: the run counts its own


def read_dta(p: Path, text: str) -> Table:
    lines = text.splitlines()
    tag, settings, tables = "", {}, []
    i = 0
    while i < len(lines):
        parts = lines[i].split("\t")
        if len(parts) >= 2 and parts[1].strip() == "TABLE" and i + 2 < len(lines):
            header, units = lines[i + 1].split("\t"), lines[i + 2].split("\t")
            j = i + 3
            rows = []
            while j < len(lines) and lines[j].startswith("\t"):
                rows.append(lines[j].split("\t")[1:])
                j += 1
            tables.append((parts[0].strip(), header[1:] if not header[0].strip() else header,
                           units[1:] if not units[0].strip() else units, rows))
            i = j
            continue
        if parts[0] == "TAG" and len(parts) > 1:
            tag = parts[1].strip().upper()
        elif len(parts) >= 3 and parts[1].strip() in ("LABEL", "QUANT", "IQUANT", "POTEN"):
            settings[parts[0].strip().upper()] = (parts[2].strip(), parts[3].strip() if len(parts) > 3 else "")
        i += 1
    if not tables:
        raise DataError("%s: a Gamry file without a table of points" % p.name)
    main = [t for t in tables if MAIN.match(t[0].upper()) and t[3]]
    if not main:
        main = [max(tables, key=lambda t: len(t[3]))]
    header, units = main[0][1], main[0][2]
    lowered = [h.strip().lower() for h in header]
    several = len(main) > 1
    rows: List[List[str]] = []
    for k, (name, h, u, body) in enumerate(main):
        if [c.strip().lower() for c in h] != lowered:
            raise DataError("%s: its curves have different columns (%s, %s)" % (p.name, main[0][0], name))
        number = re.sub(r"\D", "", name) or str(k + 1)
        rows += [r + [number] for r in body] if several and "cycle" not in lowered else body
    if several and "cycle" not in lowered:
        header, units = header + ["curve"], units + [""]
    what, prefer = TAGS.get(tag, ("", ["freq", "t", "time"]))
    table = table_from_rows(key_of(p.stem, "data"), p.name, rows, comma=True, header=header,
                            units=[_unit(u) for u in units], prefer=prefer, drop=LEFT_OUT, grid=False)
    about = [what] if what else []
    if several:
        about.append("%d curves one after another" % len(main))
    about += _settings(settings)
    table.notes.insert(0, "%s: Gamry Framework data%s" % (p.name, "".join(", " + a for a in about)))
    table.fits = False   # Kelvin's automatic fits are made for its sweeps
    others = [t for t in tables if t not in main and t[3]]
    for name, _, _, body in others:
        table.notes.append("%s: its %s table (%d points%s) left out" % (
            p.name, key_of(name, "other"), len(body), ", the open-circuit stretch before the measurement"
            if name.upper().startswith("OCV") else ""))
    return table


def _unit(text: str) -> str:
    """Gamry's units: "V vs. Ref." is V, "#" a count, "ohm" and "°" as Kelvin writes them."""
    text = text.strip()
    if text == "#":
        return ""
    if text.lower().startswith("v vs"):
        return "V"
    return unit_of(text)


def _settings(settings: Dict[str, Tuple[str, str]]) -> List[str]:
    """When it started, and the settings that are numbers people ask about (area, scan rate), in Kelvin's words."""
    out = []
    date, time = settings.get("DATE", ("", ""))[0], settings.get("TIME", ("", ""))[0]
    when = parse_time(("%s %s" % (date, time)).strip()) if date else None
    if when is not None:
        out.append("started %s" % when.isoformat(sep=" ", timespec="seconds"))
    for key, word in (("AREA", "area"), ("SCANRATE", "scan rate")):
        value, label = settings.get(key, ("", ""))
        number = number_of(value, comma=True)
        if number is not None:
            m = re.search(r"\(([^()]{1,12})\)\s*$", label)
            out.append("%s %g %s" % (word, number, unit_of(m.group(1)) if m else ""))
    return [s.strip() for s in out]
