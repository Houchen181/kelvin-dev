"""LabVIEW Measurement files (.lvm), as NI documents them: text that starts "LabVIEW Measurement", a file header (its
fields one to a line: the separator, Tab or Comma; the decimal separator; X_Columns, No, One or Multi; Time_Pref,
Absolute or Relative; the date and time) ending at ***End_of_Header***, then one or more segments, each with a header
of its own (Channels, Samples, Y_Unit_Label, X_Dimension, X0, Delta_X: a value per channel) ending the same way, a
line naming the columns (X_Value for an X column, each channel, Comment), and its rows of numbers.

A file's segments with the same channels become one run, one after another (with a segment column saying which).
Its x is the X column when the file has one (several, with Multi: they must agree), else X0 + i × Delta_X from the
segment's header. An absolute time (seconds since 1904, LabVIEW's clock) becomes seconds from the first, and the note
says when that was. A file LabVIEW wrote without headers is an ordinary table (imports.read_table)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..imports import DataError, Table, key_of, number_of, parse_time, table_from_rows, unit_of

MARK = "LabVIEW Measurement"
END = "***End_of_Header***"
SEGMENT = {"Channels", "Samples", "Date", "Time", "Y_Unit_Label", "X_Dimension", "X0", "Delta_X"}
# units as LabVIEW writes them in words
WORDS = {"volts": "V", "volt": "V", "amps": "A", "amperes": "A", "amp": "A", "ohms": "Ohm", "seconds": "s",
         "hertz": "Hz", "watts": "W", "kelvin": "K", "tesla": "T", "deg c": "°C", "degrees c": "°C"}
X_UNITS = {"time": "s", "frequency": "Hz"}
LABVIEW_EPOCH = datetime(1904, 1, 1)   # LabVIEW's clock counts seconds from here (UTC)


def _cells(line: str, sep: str) -> List[str]:
    cells = [c.strip().strip('"').strip() for c in line.split(sep)]
    while cells and not cells[-1]:
        cells.pop()
    return cells


def _header(lines: List[str], i: int, sep: str) -> Tuple[Optional[Dict[str, List[str]]], int]:
    """A header's fields from line i to its ***End_of_Header***: ({field: [values]}, the line after it); None when
    it never ends. A quoted value (a description) may run over several lines."""
    out: Dict[str, List[str]] = {}
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith(END):
            return out, i + 1
        if line.count('"') % 2:   # a quoted value that goes on below
            while i + 1 < len(lines) and lines[i + 1].count('"') % 2 == 0:
                i += 1
            i += 1
        cells = _cells(line, sep)
        if cells:
            out[cells[0]] = cells[1:]
        i += 1
    return None, i


def _row(line: str, sep: str, comma: bool) -> bool:
    cells = line.split(sep)
    return bool(cells) and number_of(cells[0].strip(), comma) is not None


def _unit(text: str) -> str:
    text = text.strip()
    return WORDS.get(text.lower(), unit_of(text))


def read_lvm(p: Path, text: str) -> Table:
    lines = text.splitlines()
    sep = "\t"
    for line in lines[1:60]:
        m = re.match(r"^Separator\s*[\t,;]\s*(\w+)", line)
        if m:
            sep = {"tab": "\t", "comma": ","}.get(m.group(1).lower(), "\t")
            break
    head, i = _header(lines, 1, sep)
    if head is None:
        raise DataError("%s: a LabVIEW measurement file whose header never ends (%s)" % (p.name, END))
    comma = (head.get("Decimal_Separator") or ["."])[0] == ","
    x_columns = ((head.get("X_Columns") or ["One"])[0] or "One").lower()
    segments: List[Tuple[Dict[str, List[str]], List[str], List[List[str]]]] = []
    seg: Dict[str, List[str]] = {}
    while i < len(lines):
        if not lines[i].strip(sep + " "):
            i += 1
            continue
        if _cells(lines[i], sep)[:1] and _cells(lines[i], sep)[0] in SEGMENT:   # a segment's own header
            got, i = _header(lines, i, sep)
            if got is None:
                raise DataError("%s: a segment's header never ends (%s)" % (p.name, END))
            seg = got
            continue
        names: List[str] = []
        if not _row(lines[i], sep, comma):
            names = _cells(lines[i], sep)
            i += 1
        rows: List[List[str]] = []
        while i < len(lines) and _row(lines[i], sep, comma):
            rows.append(lines[i].split(sep))
            i += 1
        if rows:
            segments.append((seg, names, rows))
        elif not names:   # a line that is neither: the file isn't as NI writes it
            raise DataError("%s: line %d is neither a header, the columns' names nor a row of numbers"
                            % (p.name, i + 1))
    if not segments:
        raise DataError("%s holds no rows of numbers" % p.name)
    return _table(p, head, segments, x_columns, comma)


def _table(p: Path, head: Dict[str, List[str]], segments: List[Tuple[Dict[str, List[str]], List[str],
                                                                       List[List[str]]]],
           x_columns: str, comma: bool) -> Table:
    first_seg, first_names, _ = segments[0]
    dimension = ((first_seg.get("X_Dimension") or [""])[0] or "").strip()
    x_key = dimension.lower() if dimension.lower() in X_UNITS else "x"
    x_unit = X_UNITS.get(dimension.lower(), "")
    data: List[List[Optional[float]]] = []
    channels: Optional[List[str]] = None
    units: List[str] = []
    counted = False   # a segment with neither an X column nor X0 and Delta_X: x is the point's number
    for k, (seg, names, rows) in enumerate(segments):
        width = max(len(r) for r in rows)
        names = (names + [""] * width)[:width] if names else (
            ["X_Value"] * (x_columns != "no") + ["y%d" % j for j in range(1, width)])
        xs = [j for j, n in enumerate(names) if n == "X_Value"] if x_columns != "no" else []
        if x_columns == "one" and not xs and names:
            xs = [0]
        ys = [j for j, n in enumerate(names) if j not in xs and n != "Comment" and
              any(number_of(r[j].strip(), comma) is not None for r in rows[:50] if j < len(r))]
        named = [names[j] or "y%d" % j for j in ys]
        if channels is None:
            channels = named
            labels = seg.get("Y_Unit_Label") or []
            units = [_unit(labels[n]) if n < len(labels) else "" for n in range(len(ys))]
        elif named != channels:
            raise DataError("%s: its segments hold different channels (%s; %s): Kelvin imports a file of one kind "
                            "of segment" % (p.name, ", ".join(channels), ", ".join(named)))
        x0 = number_of((seg.get("X0") or [""])[0], comma)
        dx = number_of((seg.get("Delta_X") or [""])[0], comma)
        for n, r in enumerate(rows):
            cells = [number_of(c.strip(), comma) if c.strip() else None for c in r] + [None] * width
            if xs:
                x = cells[xs[0]]
                if any(cells[j] is not None and x is not None and abs(cells[j] - x) > 1e-9 * max(1.0, abs(x))
                       for j in xs[1:]):
                    raise DataError("%s: its channels have different x values: export them one at a time" % p.name)
            elif x0 is not None and dx is not None:
                x = x0 + n * dx
            else:
                x, counted = float(len(data)), True
            data.append([x] + [cells[j] for j in ys] + ([float(k + 1)] if len(segments) > 1 else []))
    if counted:
        x_key, x_unit = "point", ""
    notes = []
    absolute = (head.get("Time_Pref") or [""])[0].lower() == "absolute"
    start = next((row[0] for row in data if row[0] is not None), None)
    if absolute and x_unit == "s" and start is not None and start > 1e8:   # seconds since 1904: from the first
        notes.append("time as seconds from %s UTC" % (LABVIEW_EPOCH + timedelta(seconds=start)).isoformat(
            sep=" ", timespec="seconds"))
        data = [[None if row[0] is None else row[0] - start] + row[1:] for row in data]
    header = [x_key] + list(channels or []) + (["segment"] if len(segments) > 1 else [])
    table = table_from_rows(key_of(p.stem, "data"), p.name, data, comma=False, header=header,
                            units=[x_unit] + units + [""], prefer=[x_key], grid=False)
    about = []
    when = parse_time(" ".join(x for x in ((head.get("Date") or [""])[0], (head.get("Time") or [""])[0]) if x))
    if when is not None:
        about.append("started %s" % when.isoformat(sep=" ", timespec="seconds"))
    if len(segments) > 1:
        about.append("%d segments one after another (column segment)" % len(segments))
    table.notes[:0] = ["%s: a LabVIEW measurement file%s" % (p.name, "".join(", " + a for a in about))] + \
        ["%s: %s" % (p.name, n) for n in notes]
    return table
