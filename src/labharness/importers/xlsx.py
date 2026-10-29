"""Excel workbooks (.xlsx, Office Open XML) as rows of cells, without openpyxl (the app doesn't bring it): what an
Arbin cycler's export, or any instrument's, holds.

A workbook is a zip of XML parts: the workbook (its sheets' names), each sheet's cells, the strings they share and the
styles (which say a number is a date). Cells come back as numbers, text, dates (datetime) or None. Only what a table
needs is read: no formulas are worked out (the value Excel saved is used), and no formatting.

A file is refused before anything is worked out of it when a part is bigger than Kelvin reads (a zip can expand a
thousandfold) or when its XML declares a DTD (Office never writes one; entity tricks need one)."""
from __future__ import annotations

import posixpath
import re
import zipfile
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from xml.parsers import expat

from ..imports import DataError

MAX_PART = 400 * 2 ** 20      # bytes of one part, unpacked
MAX_CELLS = 20_000_000
MAX_ROWS = 1_048_576          # Excel's own limit
_COLUMN = re.compile(r"^([A-Z]{1,3})(\d+)$")
# the built-in number formats that are dates or times (ECMA-376, 18.8.30)
DATE_FORMATS = set(range(14, 23)) | set(range(27, 37)) | {45, 46, 47} | set(range(50, 59))
_DATE_CODE = re.compile(r"[dmyhs]", re.I)


def _parse(z: zipfile.ZipFile, part: str, start: Any, end: Any = None, text: Any = None) -> None:
    """Run expat over one part of the workbook, refusing a part that's too big or declares a DTD."""
    try:
        info = z.getinfo(part)
    except KeyError:
        raise DataError("the workbook has no %s" % part)
    if info.file_size > MAX_PART:
        raise DataError("the workbook's %s is bigger than Kelvin reads" % posixpath.basename(part))

    def no_dtd(*_: Any) -> None:
        raise DataError("the workbook's XML declares a DTD, which Excel never writes")
    p = expat.ParserCreate(namespace_separator="}")
    p.StartDoctypeDeclHandler = no_dtd
    p.EntityDeclHandler = no_dtd
    p.StartElementHandler = start
    if end is not None:
        p.EndElementHandler = end
    if text is not None:
        p.CharacterDataHandler = text
        p.buffer_text = True
    with z.open(info) as f:
        try:
            p.ParseFile(f)
        except expat.ExpatError as e:
            raise DataError("the workbook's %s is not well-formed XML (%s)" % (posixpath.basename(part), e))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(attrs: Dict[str, str], name: str) -> Optional[str]:
    """An attribute by its local name, whatever namespace prefix the writer used."""
    for k, v in attrs.items():
        if k == name or k.endswith("}" + name):
            return v
    return None


def sheets(path: Any, wanted: Any = None) -> List[Tuple[str, List[List[Any]]]]:
    """Each worksheet's name and rows (a list of cells per row, None where a cell is empty), in the workbook's order.
    wanted(name) -> bool picks the sheets to read (all of them without it)."""
    try:
        z = zipfile.ZipFile(str(path))
    except (zipfile.BadZipFile, OSError) as e:
        raise DataError("not an Excel workbook (.xlsx): %s" % e)
    with z:
        book, date1904 = _workbook(z)
        strings = _strings(z)
        dates = _date_styles(z)
        out = []
        for name, part in book:
            if wanted is not None and not wanted(name):
                continue
            out.append((name, _rows(z, part, strings, dates, date1904)))
        return out


def names(path: Any) -> List[str]:
    """The workbook's sheets' names, in its order."""
    try:
        with zipfile.ZipFile(str(path)) as z:
            return [n for n, _ in _workbook(z)[0]]
    except (zipfile.BadZipFile, OSError) as e:
        raise DataError("not an Excel workbook (.xlsx): %s" % e)


def _workbook(z: zipfile.ZipFile) -> Tuple[List[Tuple[str, str]], bool]:
    """[(sheet name, its part)] and whether dates count from 1904 (old Mac Excel)."""
    found: List[Tuple[str, str]] = []
    flags = {"date1904": False}

    def start(tag: str, attrs: Dict[str, str]) -> None:
        local = _local(tag)
        if local == "sheet":
            found.append((attrs.get("name") or "", _attr(attrs, "id") or ""))
        elif local == "workbookPr" and (attrs.get("date1904") or "").lower() in ("1", "true"):
            flags["date1904"] = True
    _parse(z, "xl/workbook.xml", start)
    targets: Dict[str, str] = {}

    def rel(tag: str, attrs: Dict[str, str]) -> None:
        if _local(tag) == "Relationship" and attrs.get("Id"):
            targets[attrs["Id"]] = attrs.get("Target") or ""
    _parse(z, "xl/_rels/workbook.xml.rels", rel)
    book = []
    for name, rid in found:
        target = targets.get(rid, "")
        if not target:
            continue
        part = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        book.append((name, part))
    return book, flags["date1904"]


def _strings(z: zipfile.ZipFile) -> List[str]:
    """The shared strings, each the text of all its runs."""
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    out: List[str] = []
    state = {"in_t": False, "parts": [], "phonetic": 0}

    def start(tag: str, attrs: Dict[str, str]) -> None:
        local = _local(tag)
        if local == "si":
            state["parts"] = []
        elif local == "rPh":   # a phonetic guide (furigana) is not the cell's text
            state["phonetic"] += 1
        elif local == "t" and not state["phonetic"]:
            state["in_t"] = True

    def end(tag: str) -> None:
        local = _local(tag)
        if local == "t":
            state["in_t"] = False
        elif local == "rPh":
            state["phonetic"] -= 1
        elif local == "si":
            out.append("".join(state["parts"]))

    def text(data: str) -> None:
        if state["in_t"]:
            state["parts"].append(data)
    _parse(z, "xl/sharedStrings.xml", start, end, text)
    return out


def _date_styles(z: zipfile.ZipFile) -> List[bool]:
    """For each cell style (its index in cellXfs), whether its number format shows a date or a time."""
    if "xl/styles.xml" not in z.namelist():
        return []
    custom: Dict[int, bool] = {}
    out: List[bool] = []
    state = {"xfs": False}

    def start(tag: str, attrs: Dict[str, str]) -> None:
        local = _local(tag)
        if local == "numFmt":
            code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", attrs.get("formatCode") or "")   # quoted text, colours
            try:
                custom[int(attrs.get("numFmtId") or -1)] = bool(_DATE_CODE.search(code))
            except ValueError:
                pass
        elif local == "cellXfs":
            state["xfs"] = True
        elif local == "xf" and state["xfs"]:
            try:
                fmt = int(attrs.get("numFmtId") or 0)
            except ValueError:
                fmt = 0
            out.append(custom.get(fmt, fmt in DATE_FORMATS))

    def end(tag: str) -> None:
        if _local(tag) == "cellXfs":
            state["xfs"] = False
    _parse(z, "xl/styles.xml", start, end)
    return out


def _column(ref: str) -> int:
    letters = _COLUMN.match(ref or "")
    if not letters:
        return -1
    n = 0
    for ch in letters.group(1):
        n = n * 26 + ord(ch) - 64
    return n - 1


def excel_date(serial: float, date1904: bool = False) -> Optional[datetime]:
    """An Excel date number as a datetime (days since 1899-12-30, or since 1904-01-01)."""
    try:
        return (datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)) + timedelta(days=float(serial))
    except (OverflowError, ValueError):
        return None


def _rows(z: zipfile.ZipFile, part: str, strings: List[str], dates: List[bool], date1904: bool) -> List[List[Any]]:
    rows: List[List[Any]] = []
    cell: Dict[str, Any] = {}
    state = {"row": -1, "in_v": False, "in_t": False, "cells": 0}

    def start(tag: str, attrs: Dict[str, str]) -> None:
        local = _local(tag)
        if local == "row":
            try:
                state["row"] = int(attrs.get("r") or 0) - 1
            except ValueError:
                state["row"] = -1
            if state["row"] < 0:
                state["row"] = len(rows)
            if state["row"] >= MAX_ROWS:
                raise DataError("the workbook has a row past Excel's last (%d)" % MAX_ROWS)
            while len(rows) <= state["row"]:
                rows.append([])
        elif local == "c":
            cell.clear()
            cell.update(ref=attrs.get("r") or "", t=attrs.get("t") or "n", s=attrs.get("s"), v=[], text=[])
        elif local == "v":
            state["in_v"] = True
        elif local == "t" and cell:   # an inline string's text
            state["in_t"] = True

    def end(tag: str) -> None:
        local = _local(tag)
        if local == "v":
            state["in_v"] = False
        elif local == "t":
            state["in_t"] = False
        elif local == "c" and cell:
            state["cells"] += 1
            if state["cells"] > MAX_CELLS:
                raise DataError("the workbook has more than %d cells" % MAX_CELLS)
            r = rows[state["row"]] if 0 <= state["row"] < len(rows) else None
            if r is None:
                return
            k = _column(cell["ref"])
            k = len(r) if k < 0 else k
            if k > 16384:
                return
            while len(r) <= k:
                r.append(None)
            r[k] = _value(cell, strings, dates, date1904)
            cell.clear()

    def text(data: str) -> None:
        if state["in_v"]:
            cell["v"].append(data)
        elif state["in_t"]:
            cell["text"].append(data)
    _parse(z, part, start, end, text)
    return rows


def _value(cell: Dict[str, Any], strings: List[str], dates: List[bool], date1904: bool) -> Any:
    kind, raw = cell["t"], "".join(cell["v"]).strip()
    if kind == "inlineStr":
        return "".join(cell["text"])
    if kind == "s":
        try:
            return strings[int(raw)]
        except (ValueError, IndexError):
            return None
    if kind in ("str", "e"):   # a formula's text, or an error (#DIV/0!): text either way
        return raw or None
    if kind == "b":
        return 1.0 if raw == "1" else 0.0 if raw == "0" else None
    if kind == "d":   # an ISO 8601 date, as some writers save them
        try:
            return datetime.fromisoformat(raw.replace("Z", ""))
        except ValueError:
            return raw or None
    if not raw:
        return None
    try:
        number = float(raw)
    except ValueError:
        return raw
    try:
        style = int(cell["s"]) if cell.get("s") is not None else -1
    except ValueError:
        style = -1
    if 0 <= style < len(dates) and dates[style]:
        return excel_date(number, date1904) or number
    return number
