"""A run's data as files other programs open, written by code (never copied through a model, which once took 100 s
for 164 rows, rounded to 6 digits): CSV, Excel and NumPy.

    data.csv        one row per point: i, t, each swept axis and each reading, units in the header ("smu.V (V)");
                    a trace column gives each point's number of samples, a camera column where its frame is
    <trace>.csv     each trace column, one line per sample: i, the swept axes, x and y (vna.S21.csv)
    data.xlsx       the same as sheets (data, one per trace, and run: what was swept and measured, units, the
                    assessment)
    data.npz        every column as an array (a trace 2-D, NaN-padded, with <key>.length when lengths differ and
                    <key>.x or <key>.axis), camera frames as 3-D arrays, and the run's meta and columns as JSON text
                    (numpy.load(path) reads it, no pickles)

Every run gets data.csv (and its traces' CSVs) when it ends; export_run, and Download in the Runs pane, write any of
them. Numbers are written in full (the shortest text that reads back as the same 64-bit float); a point without a
value is an empty cell. Files are written to a temporary name and renamed, so none is ever half there."""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

from . import rundata
from .cards import trace_x

FORMATS = {"csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
           "npz": "application/octet-stream", "zip": "application/zip"}
XLSX_ROWS = 1_048_576          # Excel's limit, the header included
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


class ExportError(ValueError):
    pass


def _number(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return None


def _text(v: Any) -> str:
    """A cell: a number in full, an integer as one, nothing for a missing value."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            return ""
        return str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)
    return str(v)


class Table:
    """A run's columns as an export writes them: its scalar columns in order, its traces and its frames."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.rec = rundata.load(self.folder)
        rows = self.rec.rows
        self.info = rundata.column_info(self.rec.columns)
        self.axes = [a["key"] for a in self.rec.columns.get("axes") or [] if a.get("kind") != "repeat"]
        order = ["i", "t"] + [a["key"] for a in self.rec.columns.get("axes") or []] + \
            [m["key"] for m in self.rec.columns.get("measures") or []]
        keys = [k for k in dict.fromkeys(order + [k for r in rows for k in r]) if any(k in r for r in rows)]
        aux = {k for k in keys if k.endswith((".x", ".axis")) and k.rsplit(".", 1)[0] in keys}
        self.traces = [k for k in keys if k not in aux and any(isinstance(r.get(k), list) for r in rows)]
        self.frames = [k for k in keys if k not in aux and k not in self.traces and
                       any(isinstance(r.get(k), str) and ("frames/" in r[k]) for r in rows)]
        self.scalars = [k for k in keys if k not in aux and k not in self.traces]
        self.rows = rows

    def unit(self, key: str) -> str:
        return (self.info.get(key) or {}).get("unit", "")

    def head(self, key: str) -> str:
        unit = self.unit(key)
        if key in self.traces:
            return "%s (samples)" % key
        return "%s (%s)" % (key, unit) if unit else key

    def cells(self) -> Iterator[List[Any]]:
        """Each point's scalar values (a trace as its length)."""
        for r in self.rows:
            out = []
            for k in self.scalars:
                v = r.get(k)
                out.append(len(v) if isinstance(v, list) else v)
            yield out

    def trace_cells(self, key: str) -> Iterator[List[Any]]:
        """A trace column one sample per line: i, the swept axes, x and y."""
        for n, r in enumerate(self.rows):
            ys = r.get(key)
            if not isinstance(ys, list):
                continue
            xs = trace_x(r, key, len(ys))
            head = [r.get("i", n)] + [r.get(a) for a in self.axes]
            for x, y in zip(xs, ys):
                yield head + [x, y]

    def trace_head(self, key: str) -> List[str]:
        m = self.info.get(key) or {}
        x_unit, unit = m.get("x_unit", ""), m.get("unit", "")
        return ["i"] + [self.head(a) for a in self.axes] + ["x (%s)" % x_unit if x_unit else "x",
                                                             "%s (%s)" % (key, unit) if unit else key]

    def meta_lines(self) -> List[Tuple[str, Any]]:
        """What the run sheet holds: the run, then each column with its unit, role and quantity."""
        meta = self.rec.meta
        out: List[Tuple[str, Any]] = [(k, meta.get(k)) for k in (
            "run_id", "plan", "status", "reason", "started", "finished", "points", "planned_points", "duration_s",
            "lab", "simulated", "project_name", "device_name", "operator") if meta.get(k) not in (None, "")]
        analysis = self.rec.analysis or {}
        if analysis.get("assessment"):
            out.append(("assessment", analysis["assessment"]))
        for k in self.scalars + self.traces + self.frames:
            i = self.info.get(k) or {}
            out.append(("column " + k, ", ".join(x for x in (i.get("role"), i.get("unit"), i.get("quantity"),
                                                              "trace" if k in self.traces else "",
                                                              "camera frames (in data.npz and run.h5)"
                                                              if k in self.frames else "") if x)))
        out.append(("exported", datetime.now().isoformat(timespec="seconds")))
        return out


def _write(path: Path, data: bytes) -> Path:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return path


def _csv(header: Sequence[str], rows: Iterator[Sequence[Any]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow([_text(v) for v in r])
    return buf.getvalue().encode("utf-8-sig")   # with its mark, so Excel reads µ and Ω


def trace_file(key: str) -> str:
    return re.sub(r"[^\w.-]+", "_", key) + ".csv"


def write_csv(folder: Path, table: Optional[Table] = None) -> List[Path]:
    """data.csv and one CSV per trace column, in the run's folder."""
    t = table or Table(folder)
    out = [_write(Path(folder) / "data.csv", _csv([t.head(k) for k in t.scalars], t.cells()))]
    for key in t.traces:
        out.append(_write(Path(folder) / trace_file(key), _csv(t.trace_head(key), t.trace_cells(key))))
    return out

# ------------------------------------------------------------------ Excel, written as its XML (no package needed)


def _col(n: int) -> str:
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _sheet(header: Sequence[str], rows: Iterator[Sequence[Any]], limit: int) -> Tuple[bytes, int, bool]:
    """A worksheet's XML: header in bold, numbers as numbers, text inline. (xml, rows written, cut short)."""
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet xmlns="http://schemas.openxmlformats'
             '.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
             'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><sheetData>']

    def row(n: int, values: Sequence[Any], bold: bool = False) -> str:
        cells = []
        for k, v in enumerate(values):
            ref = "%s%d" % (_col(k), n)
            num = _number(v) if not isinstance(v, str) else None
            if num is not None:
                cells.append('<c r="%s"><v>%s</v></c>' % (ref, repr(num) if not num.is_integer() or abs(num) >= 1e15
                                                           else str(int(num))))
            elif v not in (None, ""):
                cells.append('<c r="%s" t="inlineStr"%s><is><t xml:space="preserve">%s</t></is></c>' % (
                    ref, ' s="1"' if bold else "", escape(_BAD_XML.sub("", str(v)))))
        return '<row r="%d">%s</row>' % (n, "".join(cells))
    parts.append(row(1, header, True))
    n, cut = 1, False
    for values in rows:
        if n >= limit:
            cut = True
            break
        n += 1
        parts.append(row(n, values))
    parts.append("</sheetData></worksheet>")
    return "".join(parts).encode("utf-8"), n - 1, cut


def _sheet_name(key: str, used: set) -> str:
    name = re.sub(r"[\[\]:*?/\\]", "_", key)[:31] or "sheet"
    base, k = name, 2
    while name.lower() in used:
        suffix = " (%d)" % k
        name, k = base[:31 - len(suffix)] + suffix, k + 1
    used.add(name.lower())
    return name


def write_xlsx(folder: Path, table: Optional[Table] = None, limit: int = XLSX_ROWS) -> Tuple[Path, List[str]]:
    """data.xlsx: the points (sheet data), each trace (a sheet of its own) and the run (sheet run). Returns the path
    and what had to be left out (Excel holds 1 048 576 rows a sheet: data.npz holds everything)."""
    t = table or Table(folder)
    used: set = set()
    sheets: List[Tuple[str, bytes]] = []
    notes: List[str] = []
    xml, _, cut = _sheet([t.head(k) for k in t.scalars], t.cells(), limit)
    sheets.append((_sheet_name("data", used), xml))
    if cut:
        notes.append("data: only the first %d points (Excel's limit)" % (limit - 1))
    for key in t.traces:
        xml, n, cut = _sheet(t.trace_head(key), t.trace_cells(key), limit)
        sheets.append((_sheet_name(key, used), xml))
        if cut:
            notes.append("%s: only the first %d samples (Excel's limit; data.npz has them all)" % (key, n))
    meta = t.meta_lines() + [("left out", n) for n in notes]
    xml, _, _ = _sheet(["what", "value"], ([k, v if isinstance(v, (int, float, str)) else json.dumps(v)]
                                           for k, v in meta), limit)
    sheets.append((_sheet_name("run", used), xml))
    names = [s[0] for s in sheets]
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.openxmlformats.org/'
          'package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
          'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/'
          'workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.'
          'spreadsheetml.styles+xml"/>' + "".join(
              '<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-'
              'officedocument.spreadsheetml.worksheet+xml"/>' % (i + 1) for i in range(len(sheets))) + "</Types>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.'
            'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
            'openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')
    book = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<workbook xmlns="http://schemas.openxmlformats.'
            'org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships"><sheets>' + "".join(
                '<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (escape(n, {'"': "&quot;"}), i + 1, i + 1)
                for i, n in enumerate(names)) + "</sheets></workbook>")
    book_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.'
                 'openxmlformats.org/package/2006/relationships">' + "".join(
                     '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                     'relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i + 1, i + 1)
                     for i in range(len(sheets))) +
                 '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                 'styles" Target="styles.xml"/></Relationships>' % (len(sheets) + 1))
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<styleSheet xmlns="http://schemas.'
              'openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/>'
              '</font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill '
              'patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1">'
              '<border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf '
              'numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" '
              'fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" '
              'xfId="0" applyFont="1"/></cellXfs></styleSheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", book)
        z.writestr("xl/_rels/workbook.xml.rels", book_rels)
        z.writestr("xl/styles.xml", styles)
        for i, (_, xml) in enumerate(sheets):
            z.writestr("xl/worksheets/sheet%d.xml" % (i + 1), xml)
    return _write(Path(folder) / "data.xlsx", buf.getvalue()), notes

# ------------------------------------------------------------------ NumPy


def write_npz(folder: Path, table: Optional[Table] = None, path: Optional[Path] = None) -> Path:
    """data.npz (or path): every column as an array, traces NaN-padded with their lengths and x values, camera
    frames, and the run's meta and columns as JSON text (numpy.load reads it without pickles)."""
    import numpy as np
    t = table or Table(folder)
    arrays: Dict[str, Any] = {}
    for k in t.scalars:
        vals = [r.get(k) for r in t.rows]
        if all(v is None or isinstance(v, (int, float)) for v in vals):
            arrays[k] = np.array([np.nan if v is None else float(v) for v in vals], dtype=float)
        elif k not in t.frames:
            arrays[k] = np.array(["" if v is None else str(v) for v in vals])
    for k in t.traces:
        for name in (k, k + ".x", k + ".axis"):
            vals = [r.get(name) for r in t.rows]
            if not any(isinstance(v, list) for v in vals):
                continue
            lengths = [len(v) if isinstance(v, list) else 0 for v in vals]
            data = np.full((len(vals), max(lengths) if lengths else 0), np.nan)
            for i, v in enumerate(vals):
                if isinstance(v, list) and v:
                    data[i, :len(v)] = [np.nan if x is None else float(x) for x in v]
            arrays[name] = data
            if name == k and len(set(lengths)) > 1:
                arrays[k + ".length"] = np.array(lengths, dtype=np.int64)
    for k in t.frames:
        frames = []
        for r in t.rows:
            try:
                frames.append(rundata.frame(t.folder, str(r.get(k))) if r.get(k) else None)
            except (OSError, ValueError, KeyError):
                frames.append(None)
        shapes = {(f.shape, f.dtype.str) for f in frames if f is not None}
        if len(shapes) == 1 and all(f is not None for f in frames):
            arrays[k] = np.stack(frames)
        else:
            for i, f in enumerate(frames):
                if f is not None:
                    arrays["%s[%d]" % (k, i)] = f
    arrays["meta.json"] = np.array(json.dumps(rundata._jsonable(t.rec.meta), ensure_ascii=False))
    arrays["columns.json"] = np.array(json.dumps({k: t.info.get(k) or {} for k in t.scalars + t.traces + t.frames},
                                                 ensure_ascii=False))
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)
    return _write(Path(path) if path else Path(folder) / "data.npz", buf.getvalue())


def export(folder: Path, fmt: str) -> Tuple[List[Path], List[str]]:
    """Write the run's data as fmt (csv, xlsx or npz) into its folder: (the files, what was left out)."""
    fmt = (fmt or "csv").lower().lstrip(".")
    if fmt in ("excel", "xls"):
        fmt = "xlsx"
    if fmt in ("numpy", "npy"):
        fmt = "npz"
    if fmt not in ("csv", "xlsx", "npz"):
        raise ExportError("format must be csv, xlsx or npz, not %r" % fmt)
    t = Table(folder)
    if not t.rows:
        raise ExportError("the run has no points to export")
    if fmt == "csv":
        return write_csv(folder, t), []
    if fmt == "xlsx":
        path, notes = write_xlsx(folder, t)
        return [path], notes
    return [write_npz(folder, t)], []


def download(folder: Path, fmt: str) -> Tuple[bytes, str, str]:
    """The export as one file to hand a browser: (its bytes, its name, its media type). CSV with traces comes as a
    zip of data.csv and the traces' CSVs."""
    paths, _ = export(folder, fmt)
    run_id = Path(folder).name
    if len(paths) == 1:
        p = paths[0]
        return p.read_bytes(), "%s-%s" % (run_id, p.name), FORMATS[p.suffix.lstrip(".")]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            z.write(p, p.name)
    return buf.getvalue(), "%s-csv.zip" % run_id, FORMATS["zip"]
