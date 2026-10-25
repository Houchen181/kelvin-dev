"""Data Kelvin didn't measure, made into a run: another program's tables (CSV, TSV, text) and LightField's .spe
spectra, so Kelvin's own tools work on them (describe_run, get_run_data, compute, look_at, reports, the automatic
fits). Several files with the same x values go into one run side by side, to compare, normalise or subtract.

Only numbers are read: a file's words never reach the model except as column names."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

MAX_BYTES = 200 * 2 ** 20
MAX_ROWS = 200000
UNIT = re.compile(r"^(.*?)\s*[\(\[]\s*([^\)\]]{1,20})\s*[\)\]]\s*$")   # "Wavelength (nm)", "Intensity [counts]"


class DataError(ValueError):
    """Not imported: the message says why (in words for the model and the person)."""


@dataclass
class Table:
    name: str                                     # the file's, as a column prefix
    x_key: str
    x_unit: str
    x: List[float]
    columns: Dict[str, Tuple[str, List[float]]] = field(default_factory=dict)   # key: (unit, values)
    notes: List[str] = field(default_factory=list)


def key_of(text: Any, fallback: str) -> str:
    """A column's name as compute takes it: letters, digits and _, starting with a letter, at most 40 long."""
    key = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]+", "_", str(text or "").strip())).strip("_").lower()[:40]
    key = key.strip("_") or fallback
    return key if key[0].isalpha() else "f_" + key


def _number(text: str) -> float:
    return float(text.strip().replace("−", "-"))


def read(path: Any) -> Table:
    p = Path(str(path)).expanduser()
    if not p.is_file():
        raise DataError("there is no file %s" % path)
    if p.stat().st_size > MAX_BYTES:
        raise DataError("%s is bigger than Kelvin imports (%d MB)" % (p.name, MAX_BYTES // 2 ** 20))
    if p.suffix.lower() == ".spe":
        return read_spe(p)
    if p.suffix.lower() in (".h5", ".hdf5", ".npy", ".mat", ".xlsx", ".xls", ".pdf", ".png", ".jpg", ".tif", ".tiff"):
        raise DataError("%s: Kelvin imports tables (CSV, TSV, text) and LightField's .spe files" % p.name)
    return read_table(p)


# ------------------------------------------------------------------ tables

def read_table(p: Path) -> Table:
    raw = p.read_bytes()
    if b"\x00" in raw[:4096]:
        raise DataError("%s is not a text table" % p.name)
    text = raw.decode("utf-8-sig", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith(("#", "%", "//"))]
    if not lines:
        raise DataError("%s holds no numbers" % p.name)
    sample = "\n".join(lines[:30])
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",\t;").delimiter
        rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
    except csv.Error:
        rows = [ln.split() for ln in lines]
    rows = [[c.strip() for c in r] for r in rows if any(c.strip() for c in r)]

    def numeric(r: List[str]) -> bool:
        try:
            [_number(c) for c in r if c]
            return bool([c for c in r if c])
        except ValueError:
            return False
    header = rows[0] if rows and not numeric(rows[0]) else []
    data, skipped = [], 0
    for r in rows[1 if header else 0:]:
        if numeric(r):
            data.append([_number(c) if c else float("nan") for c in r])
        else:
            skipped += 1
        if len(data) > MAX_ROWS:
            raise DataError("%s has more than %d rows" % (p.name, MAX_ROWS))
    if not data:
        raise DataError("%s holds no rows of numbers" % p.name)
    width = max(len(r) for r in data)
    if width < 2:
        raise DataError("%s has one column: Kelvin needs x and at least one value" % p.name)
    data = [r + [float("nan")] * (width - len(r)) for r in data]
    names, units = [], []
    for k in range(width):
        title = header[k] if k < len(header) else ""
        m = UNIT.match(title)
        names.append(m.group(1) if m else title)
        units.append(m.group(2).strip() if m else "")
    lowered = [n.lower() for n in names]
    if "intensity" in lowered and "wavelength" in lowered and any(n in lowered for n in ("frame", "row")):
        return _lightfield_csv(p, data, lowered, units, skipped)
    stem = key_of(p.stem, "data")
    table = Table(stem, key_of(names[0], "x"), units[0], [r[0] for r in data])
    for k in range(1, width):
        table.columns["%s.%s" % (stem, key_of(names[k], "y%d" % k))] = (units[k], [r[k] for r in data])
    if skipped:
        table.notes.append("%s: %d rows that weren't numbers left out" % (p.name, skipped))
    return table


def _lightfield_csv(p: Path, data: List[List[float]], names: List[str], units: List[str], skipped: int) -> Table:
    """LightField's long CSV export (a row per pixel, with Frame and Row): each frame's rows summed per wavelength,
    the frames averaged, as LightField shows a spectrum."""
    wl, it = names.index("wavelength"), names.index("intensity")
    fr = names.index("frame") if "frame" in names else None
    sums: Dict[float, Dict[float, float]] = {}
    for r in data:
        frame = r[fr] if fr is not None else 0.0
        sums.setdefault(r[wl], {}).setdefault(frame, 0.0)
        sums[r[wl]][frame] += r[it]
    xs = sorted(sums)
    stem = key_of(p.stem, "spectrum")
    table = Table(stem, "wavelength", units[wl] or "nm", xs)
    table.columns["%s.counts" % stem] = ("counts", [sum(sums[x].values()) / len(sums[x]) for x in xs])
    frames = len({r[fr] for r in data}) if fr is not None else 1
    table.notes.append("%s: LightField's export, rows summed and %d frame%s averaged" % (
        p.name, frames, "" if frames == 1 else "s"))
    return table


# ------------------------------------------------------------------ LightField's .spe

SPE_TYPES = {0: "<f4", 1: "<i4", 2: "<i2", 3: "<u2", 5: "<f8", 6: "<u1", 8: "<u4"}
PIXELS = {"MonochromeUnsigned16": "<u2", "MonochromeUnsigned32": "<u4", "MonochromeFloating32": "<f4"}


def read_spe(p: Path) -> Table:
    """A LightField (SPE 3.0) or WinSpec (2.x) file: each frame's rows summed per column, the frames averaged; the
    wavelengths from its calibration (SPE 3.0), else pixel numbers."""
    import numpy as np
    raw = p.read_bytes()
    if len(raw) < 4100:
        raise DataError("%s is too short for an .spe file" % p.name)
    head = np.frombuffer(raw[:4100], dtype=np.uint8)

    def at(offset: int, kind: str) -> Any:
        return np.frombuffer(head[offset:offset + np.dtype(kind).itemsize].tobytes(), dtype=kind)[0]
    width, height = int(at(42, "<u2")), int(at(656, "<u2"))
    frames, version = int(at(1446, "<i4")), float(at(1992, "<f4"))
    kind = SPE_TYPES.get(int(at(108, "<i2")))
    stride = width * height * (np.dtype(kind).itemsize if kind else 0)
    wavelengths: List[float] = []
    notes = []
    if version >= 3.0:
        footer = raw[int(at(678, "<u8")):]
        frames, width, height, kind, stride, wavelengths = _spe3_layout(footer, frames, width, height, kind, stride)
    if not kind or width <= 0 or height <= 0 or frames <= 0:
        raise DataError("%s: Kelvin can't read this .spe file's layout" % p.name)
    size = width * height * np.dtype(kind).itemsize
    spectra = []
    for k in range(frames):
        start = 4100 + k * stride
        block = raw[start:start + size]
        if len(block) < size:
            break
        image = np.frombuffer(block, dtype=kind).astype(float).reshape(height, width)
        spectra.append(image.sum(axis=0))
    if not spectra:
        raise DataError("%s holds no complete frame" % p.name)
    spectrum = np.mean(spectra, axis=0)
    stem = key_of(p.stem, "spectrum")
    if len(wavelengths) == width:
        table = Table(stem, "wavelength", "nm", [float(w) for w in wavelengths])
    else:
        table = Table(stem, "pixel", "", [float(i) for i in range(width)])
        notes.append("%s: no wavelength calibration in the file; x is the pixel number" % p.name)
    table.columns["%s.counts" % stem] = ("counts", [float(v) for v in spectrum])
    notes.append("%s: %d frame%s of %dx%d, rows summed%s" % (p.name, len(spectra), "" if len(spectra) == 1 else "s",
                                                          width, height, ", frames averaged" if len(spectra) > 1 else ""))
    table.notes += notes
    return table


def _spe3_layout(footer: bytes, frames: int, width: int, height: int, kind: Any, stride: int) -> Tuple[Any, ...]:
    """SPE 3.0's XML footer: the frame's layout (the first region) and the wavelength of each of its columns."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(footer.decode("utf-8", errors="replace").strip("\x00 \r\n"))
    except ET.ParseError:
        return frames, width, height, kind, stride, []

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]
    frame = next((e for e in root.iter() if local(e.tag) == "DataBlock" and e.get("type") == "Frame"), None)
    region = next((e for e in frame.iter() if local(e.tag) == "DataBlock" and e.get("type") == "Region"), None) \
        if frame is not None else None
    if frame is not None:
        frames = int(frame.get("count") or frames)
        kind = PIXELS.get(frame.get("pixelFormat") or "", kind)
        stride = int(frame.get("stride") or frame.get("size") or stride)
    if region is not None:
        width, height = int(region.get("width") or width), int(region.get("height") or height)
    wavelengths: List[float] = []
    for e in root.iter():
        if local(e.tag) == "Wavelength" and e.text and "," in e.text:
            wavelengths = [float(v) for v in e.text.split(",") if v.strip()]
            break
    mapping = next((e for e in root.iter() if local(e.tag) == "SensorMapping"), None)
    if wavelengths and len(wavelengths) != width and mapping is not None:   # a region of the sensor, maybe binned
        x0, step = int(mapping.get("x") or 0), max(1, int(mapping.get("xBinning") or 1))
        picked = [sum(wavelengths[x0 + k * step:x0 + (k + 1) * step]) / step for k in range(width)
                  if x0 + (k + 1) * step <= len(wavelengths)]
        wavelengths = picked if len(picked) == width else wavelengths
    return frames, width, height, kind, stride, wavelengths


# ------------------------------------------------------------------ several files, one run

def merge(tables: List[Table]) -> Table:
    """The files' columns side by side on their shared x values (to compare or subtract them)."""
    first = tables[0]
    out = Table(first.name, first.x_key, first.x_unit, list(first.x), dict(first.columns), list(first.notes))
    for t in tables[1:]:
        if len(t.x) != len(out.x) or any(abs(a - b) > 1e-6 * max(1.0, abs(a)) for a, b in zip(t.x, out.x)):
            raise DataError("%s's x values differ from %s's: import them one at a time (or export them on the "
                               "same axis)" % (t.name, first.name))
        for k, v in t.columns.items():
            name = k if k not in out.columns else "%s_%d" % (k, len(out.columns))
            out.columns[name] = v
        out.notes += t.notes
    return out
