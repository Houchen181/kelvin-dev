"""Data Kelvin didn't measure, made into a run, so Kelvin's own tools work on it (describe_run, get_run_data,
compute, look_at, plot_runs, fit_run, run_python, reports, the automatic fits):
- tables: CSV, TSV and text, with or without a header row or a row of units, after a preamble of the instrument's
  settings, with dates and times (as seconds from the first), a decimal comma, and text columns (left out, and said);
  a long table of a 2-D scan (x, y, value rows) becomes a 2-D run;
- Excel workbooks (.xlsx): the first sheet with numbers, or an Arbin export's Channel sheets;
- makers' files: BioLogic's EC-Lab .mpt and .mpr, Gamry's .dta, Arbin's CSV and Excel exports (importers/), and
  LightField's .spe spectra;
- NumPy's .npy and .npz, and TIFF images: an array of numbers, a table, or camera frames (a frame or a stack of them,
  one point per frame, which look_at shows).

Several files with the same x values go into one run side by side, to compare, normalise or subtract.

Only numbers are read: a file's words never reach the model except as column names (letters, digits and _), and the
units of its columns (a few characters of a unit's own). What a maker's file says about how it was measured reaches
the model only as Kelvin's own words for it (a technique's name, when it started, an electrode's area)."""
from __future__ import annotations

import csv
import functools
import re
import struct
import zlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

MAX_BYTES = 200 * 2 ** 20
MAX_ROWS = 200000
MAX_FRAMES = 5000
UNIT = re.compile(r"^(.*?)\s*[\(\[]\s*([^\)\]]{1,20})\s*[\)\]]\s*$")   # "Wavelength (nm)", "Intensity [counts]"
DATA_SUFFIXES = (".csv", ".tsv", ".txt", ".dat", ".asc", ".xlsx", ".xlsm", ".mpt", ".mpr", ".dta", ".npy", ".npz",
                 ".tif", ".tiff", ".spe")
WHAT = ("tables (CSV, TSV, text, Excel .xlsx), BioLogic's .mpt and .mpr, Gamry's .dta, Arbin's CSV and Excel "
        "exports, LightField's .spe, NumPy's .npy and .npz, and TIFF images")


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
    outer: Optional[Tuple[str, str, List[float]]] = None   # a 2-D table's slow axis: key, unit, its value at each row
    frames: Optional[Tuple[str, str, List[Any]]] = None    # camera frames, one per point: key, unit, the 2-D arrays
    # whether Kelvin's automatic fits suit it: they are made for its sweeps, and mislead on a voltammogram, an
    # impedance spectrum, a cycler's record or a stack of frames (fit_run fits those with a model a person chooses)
    fits: bool = True


def key_of(text: Any, fallback: str) -> str:
    """A column's name as compute takes it: letters, digits and _, starting with a letter, at most 40 long."""
    key = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]+", "_", str(text or "").strip())).strip("_").lower()[:40]
    key = key.strip("_") or fallback
    return key if key[0].isalpha() else "f_" + key


def column_key(name: Any, fallback: str) -> str:
    """key_of, keeping what a column's symbols say: -Im(Z) is minus_im_z, |Z| abs_z, <I> i_mean."""
    text = str(name or "").strip()
    text = re.sub(r"\|([^|]+)\|", r"abs_\1", text)
    text = re.sub(r"<([^<>]+)>", r"\1_mean", text)
    text = re.sub(r"^[-−]\s*(?=[A-Za-z(])", "minus_", text)
    return key_of(text, fallback)


# ------------------------------------------------------------------ units

# units as columns' headers write them ("time/s", "(sec)"), and the few written another way
UNITS = {"s", "ms", "us", "µs", "ns", "min", "h", "sec", "secs", "V", "mV", "uV", "µV", "kV", "A", "mA", "uA", "µA",
         "nA", "pA", "K", "mK", "°C", "C", "mC", "Oe", "T", "mT", "G", "Hz", "kHz", "MHz", "GHz", "Ohm", "ohm", "Ω",
         "kOhm", "MOhm", "mOhm", "Ohm-1", "S", "mS", "uS", "µS", "deg", "°", "rad", "%", "m", "cm", "mm", "um", "µm",
         "nm", "W", "mW", "uW", "µW", "dB", "dBm", "a.u.", "au", "arb.u.", "counts", "cps", "Pa", "kPa", "mbar", "bar",
         "Torr", "Ah", "mAh", "A.h", "mA.h", "Wh", "mWh", "W.h", "mW.h", "F", "mF", "uF", "µF", "nF", "pF", "µF-2",
         "Ohm.cm", "Ohm.cm²", "mS/cm", "V/s", "mV/s", "A/cm²", "mA/cm²", "cm²", "cm^2", "g", "mg", "eV", "meV",
         "L", "mL", "ppm", "1/s"}
_SAME = {"sec": "s", "secs": "s", "seconds": "s", "ohm": "Ohm", "ohms": "Ohm", "Ω": "Ohm", "°": "deg",
         "degree": "deg", "degrees": "deg", "Deg": "deg", "mA.h": "mAh", "A.h": "Ah", "W.h": "Wh", "mW.h": "mWh",
         "cm^2": "cm²", "us": "µs", "uV": "µV", "uA": "µA", "uS": "µS", "um": "µm", "uW": "µW", "uF": "µF",
         "au": "a.u."}
_UNIT_TEXT = re.compile(r"^[A-Za-zµ°%Ω²³·.\-/^*0-9]{1,12}$")
# a unit after the column's name, as BioLogic writes them ("Ewe/V", "Q charge/discharge/mA.h"), longest first
_SLASHED = sorted(UNITS, key=len, reverse=True)


def unit_of(text: Any) -> str:
    """A unit as Kelvin keeps it: a few characters of a unit's own (anything else is no unit: a file's words never
    pass as one), the few written another way in Kelvin's way ("sec" is s, "ohm" Ohm)."""
    text = str(text or "").strip().strip("()[]").strip()
    if not text or not _UNIT_TEXT.match(text) or text.replace(".", "").replace("-", "").isdigit():
        return ""
    return _SAME.get(text, text)


def split_unit(title: Any) -> Tuple[str, str]:
    """A column's header as (name, unit): "Wavelength (nm)", "I [nA]" and "Ewe/V" carry their units."""
    title = str(title or "").strip()
    m = UNIT.match(title)
    if m and unit_of(m.group(2)):
        return m.group(1).strip(), unit_of(m.group(2))
    for u in _SLASHED:
        if title.endswith("/" + u) and len(title) > len(u) + 1:
            return title[:-len(u) - 1].strip(), unit_of(u)
    return (m.group(1).strip() if m else title), ""


# ------------------------------------------------------------------ cells: numbers, dates and times

_MISSING = {"", "-", "--", "na", "n/a", "#n/a", "null", "none", "#div/0!", "#value!", "#ref!", "#num!", "#name?",
            "#null!", "nil"}
_TIME = r"(?:[T ]+(\d{1,2}):(\d{2})(?::(\d{2}(?:[.,]\d*)?))?\s*([AaPp][Mm])?)?"
_YMD = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})" + _TIME + r"\s*(?:Z|[+-]\d{2}:?\d{2})?$")
_DMY = re.compile(r"^(\d{1,2})([/.-])(\d{1,2})\2(\d{4}|\d{2})" + _TIME + "$")
_HMS = re.compile(r"^(\d{1,3}):(\d{2})(?::(\d{2}(?:[.,]\d*)?))?\s*([AaPp][Mm])?$")
_DHMS = re.compile(r"^(\d{1,3}):(\d{1,2}):(\d{2}):(\d{2}(?:[.,]\d*)?)$")   # day:hours:minutes:seconds (IRIG)


def number_of(cell: Any, comma: bool = False) -> Optional[float]:
    """A cell's number, or None. comma: the file may write a decimal comma ("1,5" is 1.5)."""
    if isinstance(cell, bool):
        return float(cell)
    if isinstance(cell, (int, float)):
        return float(cell)
    if not isinstance(cell, str):
        return None
    s = cell.strip().replace("−", "-")
    if not s or "_" in s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    if comma and s.count(",") == 1 and "." not in s:
        try:
            return float(s.replace(",", "."))
        except ValueError:
            return None
    return None


def time_kind(text: str) -> Optional[str]:
    """Whether a cell is a date (ymd: year first; dmy: day and month in either order, then the year), a time of day
    or a time span (hms), or day:h:m:s (dhms)."""
    s = text.strip()
    if len(s) < 4 or len(s) > 40:
        return None
    for kind, pattern in (("ymd", _YMD), ("dmy", _DMY), ("hms", _HMS), ("dhms", _DHMS)):
        if pattern.match(s):
            return kind
    return None


def _clock(h: Any, m: Any, s: Any, ampm: Any) -> float:
    hours = int(h or 0)
    if ampm:
        hours = hours % 12 + (12 if ampm.lower() == "pm" else 0)
    return hours * 3600 + int(m or 0) * 60 + float((s or "0").replace(",", "."))


def _date(kind: str, m: Any, day_first: bool) -> Optional[datetime]:
    if kind == "ymd":
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        clock = m.groups()[3:7]
    else:
        a, b, y = int(m.group(1)), int(m.group(3)), int(m.group(4))
        d, mo = (a, b) if day_first else (b, a)
        y = y + (2000 if y < 70 else 1900) if y < 100 else y
        clock = m.groups()[4:8]
    try:
        return datetime(y, mo, d) + timedelta(seconds=_clock(*clock))
    except (ValueError, OverflowError):
        return None


def parse_time(text: Any) -> Optional[datetime]:
    """One date and time, as a maker's file writes it in its settings (month first unless the day can't be one)."""
    if isinstance(text, datetime):
        return text
    s = str(text or "").strip()
    kind = time_kind(s)
    if kind == "ymd":
        return _date(kind, _YMD.match(s), False)
    if kind == "dmy":
        m = _DMY.match(s)
        return _date(kind, m, m.group(2) == "." or int(m.group(1)) > 12)
    return None


def seconds_of(cells: Sequence[Any]) -> Tuple[List[Optional[float]], Optional[datetime], str]:
    """A column of dates or times as seconds from its first, that first date (None for times of day or spans), and
    the kind read. Day and month: day first where a day comes first ("25/01/2004", "25.01.2004"), else month first."""
    texts = [c.strip() if isinstance(c, str) else c for c in cells]
    sample = texts[::max(1, len(texts) // 2000)]
    kinds = Counter(k for k in (("dt" if isinstance(c, datetime) else time_kind(c) if isinstance(c, str) else None)
                                for c in sample) if k)
    if not kinds:
        return [None] * len(cells), None, ""
    kind = kinds.most_common(1)[0][0]
    out: List[Optional[float]] = []
    if kind in ("hms", "dhms"):
        wrap = 0.0
        last = None
        for c in texts:
            m = (_HMS if kind == "hms" else _DHMS).match(c) if isinstance(c, str) else None
            if not m:
                out.append(None)
                continue
            value = _clock(*m.groups()) if kind == "hms" else int(m.group(1)) * 86400 + _clock(*m.groups()[1:], None)
            if last is not None and value + wrap < last - 43200:   # past midnight
                wrap += 86400
            last = value + wrap
            out.append(last)
        first = next((v for v in out if v is not None), 0.0)
        return [None if v is None else v - first for v in out], None, kind
    pattern = _YMD if kind == "ymd" else _DMY
    matches = [pattern.match(c) if isinstance(c, str) else None for c in texts]
    # the whole column says which comes first: a 13th of the month late in a log settles it
    day_first = kind == "dmy" and any(m.group(2) == "." or int(m.group(1)) > 12 for m in matches if m)
    for c, m in zip(texts, matches):   # seconds since the start of year 1: a day's number, then its clock
        if isinstance(c, datetime):
            out.append(c.toordinal() * 86400.0 + c.hour * 3600 + c.minute * 60 + c.second + c.microsecond / 1e6)
            continue
        if m is None:
            out.append(None)
            continue
        if kind == "ymd":
            y, month, day, clock = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.groups()[3:7]
        else:
            a, b, y = int(m.group(1)), int(m.group(3)), int(m.group(4))
            day, month = (a, b) if day_first else (b, a)
            y = y + (2000 if y < 70 else 1900) if y < 100 else y
            clock = m.groups()[4:8]
        number = _day(y, month, day)
        out.append(None if number is None else number * 86400.0 + _clock(*clock))
    first = next((v for v in out if v is not None), None)
    if first is None:
        return [None] * len(cells), None, kind
    start = datetime.fromordinal(int(first // 86400)) + timedelta(seconds=first % 86400)
    return [None if v is None else v - first for v in out], start, kind


@functools.lru_cache(maxsize=4096)
def _day(year: int, month: int, day: int) -> Optional[int]:
    """A date's number of days since the start of year 1 (None: no such date), worked out once per day."""
    try:
        return datetime(year, month, day).toordinal()
    except ValueError:
        return None


def _scan(cell: Any, comma: bool) -> str:
    """n: a number, t: a date or time, "": empty, w: words."""
    if cell is None:
        return ""
    if isinstance(cell, datetime):
        return "t"
    if isinstance(cell, (bool, int, float)):
        return "n"
    s = str(cell).strip()
    if s.lower() in _MISSING:
        return ""
    if number_of(s, comma) is not None:
        return "n"
    return "t" if time_kind(s) else "w"


# ------------------------------------------------------------------ reading a file

def read(path: Any) -> Table:
    """A file as a Table. DataError: not imported, saying why in plain words, also for a file damaged in a way no
    reader foresaw (never a bare error)."""
    p = Path(str(path)).expanduser()
    if not p.is_file():
        raise DataError("there is no file %s" % path)
    if p.stat().st_size > MAX_BYTES:
        raise DataError("%s is bigger than Kelvin imports (%d MB)" % (p.name, MAX_BYTES // 2 ** 20))
    try:
        return _read(p)
    except DataError:
        raise
    except (ValueError, IndexError, KeyError, struct.error, zlib.error, EOFError, UnicodeError) as e:
        raise DataError("%s could not be read: it looks damaged, or isn't what its name says (%s)" % (
            p.name, str(e).splitlines()[0][:120] if str(e) else type(e).__name__))


def _read(p: Path) -> Table:
    suffix = p.suffix.lower()
    with open(p, "rb") as f:
        head = f.read(64)
    from .importers import arrays, biologic, tiff
    if suffix == ".spe":
        return read_spe(p)
    if head.startswith(b"BIO-LOGIC MODULAR FILE"):
        return biologic.read_mpr(p)
    if suffix == ".mpr":
        raise DataError("%s is not an EC-Lab .mpr file" % p.name)
    if tiff.is_tiff(head):
        return read_tiff(p)
    if suffix in (".tif", ".tiff"):
        raise DataError("%s is not a TIFF file" % p.name)
    if suffix == ".npy" or head.startswith(b"\x93NUMPY"):
        return arrays.read_npy(p)
    if suffix == ".npz":
        return arrays.read_npz(p)
    if suffix in (".xlsx", ".xlsm"):
        return read_xlsx(p)
    if suffix == ".xls":
        raise DataError("%s is an Excel 97-2003 workbook (.xls), which Kelvin doesn't read: save it as .xlsx or CSV "
                        "in Excel, then import that" % p.name)
    if suffix == ".res":
        raise DataError("%s is Arbin's own database (.res), which Kelvin doesn't read: export the test from MITS Pro "
                        "or Data Watcher as Excel (.xlsx) or CSV, then import that" % p.name)
    if suffix in (".h5", ".hdf5", ".mat", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".zip", ".docx",
                  ".pptx", ".opj", ".opju"):
        raise DataError("%s: Kelvin imports %s" % (p.name, WHAT))
    return read_table(p)


def takes(name: Any, data: bytes) -> bool:
    """Whether an attached file is one of the kinds Kelvin imports (by its name, or its first bytes)."""
    from .importers import tiff
    suffix = Path(str(name or "")).suffix.lower()
    return suffix in DATA_SUFFIXES or tiff.is_tiff(data[:4]) or data[:6] == b"\x93NUMPY" or \
        data[:22] == b"BIO-LOGIC MODULAR FILE"


# ------------------------------------------------------------------ tables

def text_of(raw: bytes, name: str) -> str:
    """A text file's text: UTF-8 or UTF-16 (with its mark), else Windows' Western encoding (makers' programs)."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16", errors="replace")
    elif b"\x00" in raw[:4096]:
        raise DataError("%s is not a text table" % name)
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
    return text.replace("\r\r\n", "\n")   # line ends doubled by a copy: no blank line between each (EC-Lab counts them)


def read_table(p: Path) -> Table:
    text = text_of(p.read_bytes(), p.name)
    first = text.lstrip()[:40]
    from .importers import biologic, gamry
    if first.startswith(("EC-Lab ASCII FILE", "BT-Lab ASCII FILE")):
        return biologic.read_mpt(p, text)
    if p.suffix.lower() == ".mpt":
        raise DataError("%s is not an EC-Lab text file (its first line isn't EC-Lab's)" % p.name)
    if first.startswith("EXPLAIN"):
        return gamry.read_dta(p, text)
    if p.suffix.lower() == ".dta":
        raise DataError("%s is not a Gamry data file (it doesn't start with EXPLAIN)" % p.name)
    rows, comma = split_rows(text)
    return table_from_rows(key_of(p.stem, "data"), p.name, rows, comma=comma)


class _Comment(list):
    """A comment line's cells: never data, but it may name the columns (numpy.savetxt writes "# x y")."""


def _cells(lines: List[str], sep: Optional[str]) -> List[List[str]]:
    if sep is None:
        return [ln.split() for ln in lines]
    return [r for r in csv.reader(lines, delimiter=sep)]


def split_rows(text: str) -> Tuple[List[List[str]], bool]:
    """A text table's lines as rows of cells, split where its numbers are: by tab, comma, semicolon, | or spaces
    (whichever gives the most lines of numbers, the fewest words among them). Returns the rows, and whether a decimal
    comma may be written (cells aren't split at commas)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    hint = None
    if lines and re.match(r"^\s*sep=.\s*$", lines[0]):   # Excel's hint of the separator
        hint, lines = lines[0].strip()[4], lines[1:]
    comment = [ln.lstrip().startswith(("#", "%", "//")) for ln in lines]
    sample = [ln for ln, c in zip(lines, comment) if not c][:2000]
    best: Optional[Tuple[Tuple[int, int, int], Optional[str]]] = None
    choices: List[Optional[str]] = [hint] if hint else ["\t", ",", ";", "|", None]
    for rank, sep in enumerate(choices):
        comma = sep != ","
        ok = words = 0
        for row in _cells(sample, sep):
            kinds = [_scan(c, comma) for c in row]
            if sum(1 for k in kinds if k in ("n", "t")) >= 2:
                ok += 1
                words += kinds.count("w")
        score = (ok, -words, -rank)
        if best is None or score > best[0]:
            best = (score, sep)
    sep = best[1] if best else None
    rows: List[List[str]] = []
    plain = _cells([ln for ln, c in zip(lines, comment) if not c], sep)
    k = 0
    for ln, c in zip(lines, comment):
        if c:
            inside = ln.lstrip().lstrip("#%/").strip()
            rows.append(_Comment(_cells([inside], sep)[0] if inside else []))
        else:
            rows.append(plain[k])
            k += 1
    return rows, sep != ","


def _trim(row: Sequence[Any]) -> List[Any]:
    out = [c.strip() if isinstance(c, str) else c for c in row]
    while out and out[-1] in (None, ""):
        out.pop()
    return out


def _start(rows: List[List[Any]], comma: bool) -> int:
    """The first row of the block of numbers: the first of four rows in a row that each hold at least two numbers
    (or dates), so a line of settings with two numbers in it ("Range: 0 10") isn't taken for data. A short file
    without four such rows starts at its longest stretch of them. -1: no row holds two numbers."""
    known: Dict[int, bool] = {}

    def data(i: int) -> bool:
        if i not in known:
            r = rows[i]
            known[i] = not isinstance(r, _Comment) and sum(1 for c in r if _scan(c, comma) in ("n", "t")) >= 2
        return known[i]
    stretches: List[Tuple[int, int]] = []
    i, n = 0, len(rows)
    while i < n:
        if not data(i):
            i += 1
            continue
        j = i
        while j < n and j - i < 4 and data(j):
            j += 1
        if j - i >= 4:
            return i
        stretches.append((i, j - i))
        i = j
    if not stretches:
        return -1
    longest = max(length for _, length in stretches)
    return next(start for start, length in stretches if length == longest)


def _units_like(row: Sequence[Any]) -> bool:
    """A row of units under the names: "(sec)", "[V]", "Ohm"... and nothing else."""
    cells = [c for c in row if c not in (None, "")]
    if not cells:
        return False
    for c in cells:
        s = str(c).strip()
        if s == "#":   # a count
            continue
        inner = s.strip("()[]").strip()
        wrapped = (s[:1], s[-1:]) in (("(", ")"), ("[", "]"))
        if not unit_of(inner) or not (wrapped or inner in UNITS or inner in _SAME):
            return False
    return True


def _names_like(row: Optional[Sequence[Any]], width: int, comma: bool) -> bool:
    """A row that names the columns: about as wide as the data, mostly words, not a setting ("Sample: ...")."""
    if not row:
        return False
    n = len(row)
    if n < max(1, width - 1) or n > width + 1:
        return False
    kinds = [_scan(c, comma) for c in row]
    if kinds.count("w") < max(1, (n + 1) // 2):
        return False
    return not str(row[0]).rstrip().endswith(":")


COUNTERS = ("", "i", "index", "idx", "no", "nr", "n", "point", "points", "pt", "data_point", "datapoint", "num",
            "number", "row", "record")


def counts_rows(values: Sequence[Optional[float]], name: str) -> bool:
    """A column that only counts the rows (0, 1, 2... or 1, 2, 3...), under a name that says so."""
    if re.sub(r"[^a-z_]", "", name.lower()) not in COUNTERS:
        return False
    if not values or values[0] not in (0.0, 1.0):
        return False
    return all(v is not None and v == values[0] + i for i, v in enumerate(values))


def table_from_rows(stem: str, filename: str, rows: List[List[Any]], comma: bool = True,
                    header: Optional[Sequence[Any]] = None, units: Optional[Sequence[Any]] = None,
                    prefer: Sequence[str] = (), drop: Sequence[str] = (), grid: bool = True) -> Table:
    """Rows of cells (a text file's, or a workbook's values) as a Table. Without a header given, the row that names
    the columns is the one just above the numbers (with a row of units between, if there is one). Columns of numbers
    are kept, dates and times as seconds from their first; text columns are left out, and rows whose x isn't a
    number are skipped, both said in the notes. prefer: keys to take as x, in order; drop: keys left out."""
    from .importers import arbin
    rows = [r if isinstance(r, _Comment) else _trim(r) for r in rows]
    rows = [r for r in rows if r or isinstance(r, _Comment)]
    found = header is None   # a table found in a file, not one a maker's reader laid out
    if header is None:
        start = _start(rows, comma)
        if start < 0:
            if any(not isinstance(r, _Comment) and any(_scan(c, comma) in ("n", "t") for c in r) for r in rows):
                raise DataError("%s has one column: Kelvin needs x and at least one value" % filename)
            raise DataError("%s holds no rows of numbers" % filename)
        body = [r for r in rows[start:] if not isinstance(r, _Comment)]
        width = Counter(len(r) for r in body[:200]).most_common(1)[0][0]
        above = rows[start - 1] if start >= 1 else None
        two_above = rows[start - 2] if start >= 2 else None
        if above is not None and _units_like(above) and _names_like(two_above, width, comma):
            header, units = list(two_above or []), list(above)
        elif _names_like(above, width, comma):
            header = list(above or [])
    else:
        body = [r for r in rows if not isinstance(r, _Comment)]
        if not body:
            raise DataError("%s holds no rows of numbers" % filename)
        width = max(len(_trim(header)), Counter(len(r) for r in body[:200]).most_common(1)[0][0])
    header = list(header or [])
    width = max(width, len(_trim(header))) if header else width
    if width < 2:
        raise DataError("%s has one column: Kelvin needs x and at least one value" % filename)
    names: List[str] = []
    unit_list: List[str] = []
    for k in range(width):
        title = header[k] if k < len(header) and header[k] is not None else ""
        name, unit = split_unit(title)
        if not unit and units and k < len(units) and units[k] not in (None, ""):
            unit = unit_of(units[k])
        names.append(name)
        unit_list.append(unit)
    keys: List[str] = []
    for k, name in enumerate(names):
        key = column_key(name, "x" if k == 0 else "y%d" % k)
        base, n = key, 2
        while key in keys:
            key, n = "%s_%d" % (base, n), n + 1
        keys.append(key)
    if len(body) > MAX_ROWS * 2:
        raise DataError("%s has more than %d rows" % (filename, MAX_ROWS))
    cells = [[r[k] if k < len(r) else None for r in body] for k in range(width)]
    kinds: List[str] = []   # each column's: n numbers, t dates or times, w words (most of its cells), "" empty
    step = max(1, len(body) // 4000)   # judged on rows spread through the file: a cycler's 200 000 in a moment
    for k in range(width):
        counts = Counter(_scan(c, comma) for c in cells[k][::step])
        filled = counts["n"] + counts["t"] + counts["w"]
        kinds.append("" if not filled else "n" if counts["n"] > filled / 2 else
                     "t" if counts["t"] > filled / 2 else "w")
    values: Dict[int, List[Optional[float]]] = {}
    starts: Dict[int, Tuple[Optional[datetime], str]] = {}
    for k in range(width):
        if kinds[k] == "n":
            values[k] = [number_of(c, comma) for c in cells[k]]
        elif kinds[k] == "t":
            values[k], first, how = seconds_of(cells[k])
            starts[k] = (first, how)
            unit_list[k] = "s"
    kept = [k for k in range(width) if k in values and keys[k] not in drop]
    lowered = [n.lower() for n in names]
    if "intensity" in lowered and "wavelength" in lowered and any(n in lowered for n in ("frame", "row")):
        return _lightfield_csv(stem, filename, {lowered[k]: values[k] for k in kept}, unit_list[lowered.index(
            "wavelength")])
    if len(kept) < 2:
        if not kept:
            raise DataError("%s holds no rows of numbers" % filename)
        raise DataError("%s has one column of numbers: Kelvin needs x and at least one value" % filename)
    maker = ""
    choice = next((k for want in prefer for k in kept if keys[k] == want), None)
    if choice is None and arbin.is_arbin(keys[k] for k in kept):
        maker = "an Arbin cycler's export"
        kept = [k for k in kept if keys[k] not in arbin.LEFT_OUT]
        unit_list = [arbin.unit(names[k], unit_list[k]) for k in range(width)]
        choice = next((k for want in arbin.X for k in kept if keys[k] == want), None)
    if choice is None:
        choice = kept[0]
        if len(kept) > 2 and kinds[choice] == "n" and counts_rows(values[choice], names[choice]):
            kept = kept[1:]
            # Kelvin's own export: i, t, then the swept axes
            choice = kept[1] if keys[choice] == "i" and keys[kept[0]] == "t" and len(kept) > 2 else kept[0]
    others = [k for k in kept if k != choice]
    use = [i for i, v in enumerate(values[choice]) if v is not None]
    skipped = len(body) - len(use)
    if not use:
        raise DataError("%s holds no rows of numbers" % filename)
    if found and len(use) < 2:   # a line of a note with two numbers in it ("5 V and 10 mA") is no table
        raise DataError("%s holds one row of numbers: Kelvin needs at least two" % filename)
    if len(use) > MAX_ROWS:
        raise DataError("%s has more than %d rows" % (filename, MAX_ROWS))
    nan = float("nan")

    def column(k: int) -> List[float]:
        v = values[k]
        return [nan if v[i] is None else v[i] for i in use]
    table = Table(stem, keys[choice], unit_list[choice], column(choice))
    if grid and not prefer and not maker and len(others) >= 2:
        one, two = column(choice), column(others[0])
        pair = (choice, others[0]) if _outer(one, two) else (others[0], choice) if _outer(two, one) else None
        if pair is not None:
            slow, fast = pair
            table = Table(stem, keys[fast], unit_list[fast], column(fast))
            table.outer = (keys[slow], unit_list[slow], column(slow))
            others = [k for k in others if k not in (slow, fast)]
            blocks = len(set(table.outer[2]))
            table.notes.append("%s: a 2-D scan, %d values of %s, each over %d of %s" % (
                filename, blocks, keys[slow], len(use) // blocks, keys[fast]))
    for k in others:
        table.columns["%s.%s" % (stem, keys[k])] = (unit_list[k], column(k))
    if maker:
        table.notes.insert(0, "%s: %s" % (filename, maker))
        table.fits = False
    if skipped:
        table.notes.append("%s: %d rows that weren't numbers left out" % (filename, skipped))
    words = [keys[k] for k in range(width) if kinds[k] == "w"]
    if words:
        table.notes.append("%s: text column%s left out: %s" % (filename, "" if len(words) == 1 else "s",
                                                               ", ".join(words)))
    for k, (first, how) in starts.items():
        if k in kept:
            table.notes.append("%s: %s, as seconds from %s" % (
                filename, keys[k], first.isoformat(sep=" ", timespec="seconds") if first else "its first time"))
    return table


def _blocks(values: List[float]) -> bool:
    """Whether a column is held in equal blocks of rows (a 2-D scan's slow axis)."""
    n = len(values)
    size = next((i for i, v in enumerate(values) if v != values[0]), n)
    if size < 2 or size >= n or n % size:
        return False
    for b in range(0, n, size):
        block = values[b:b + size]
        if any(v != block[0] for v in block) or (b and block[0] == values[b - 1]):
            return False
    return True


def _outer(slow: List[float], fast: List[float]) -> bool:
    """Whether slow is held in equal blocks while fast runs over the same values in each (either way round, for a
    scan that goes back and forth): a 2-D scan written one row per point."""
    if not _blocks(slow):
        return False
    size = next(i for i, v in enumerate(slow) if v != slow[0])
    first = fast[:size]
    finite = [v for v in first if v == v]
    span = (max(finite) - min(finite)) if finite else 0.0
    if span <= 0:
        return False
    tol = 0.01 * span
    back = first[::-1]
    for b in range(size, len(fast), size):
        block = fast[b:b + size]
        if not (all(abs(u - v) <= tol for u, v in zip(block, first)) or
                all(abs(u - v) <= tol for u, v in zip(block, back))):
            return False
    return True


def table_from_columns(stem: str, filename: str, named: Sequence[Tuple[str, Any]], prefer: Sequence[str] = (),
                       drop: Sequence[str] = ()) -> Table:
    """Columns already read as numbers ([(header, values)], as a maker's binary file holds them) as a Table."""
    keys: List[str] = []
    cols: List[Tuple[str, str, List[float]]] = []
    for title, values in named:
        name, unit = split_unit(title)
        key = column_key(name, "y%d" % len(keys))
        if key in drop:
            continue
        base, n = key, 2
        while key in keys:
            key, n = "%s_%d" % (base, n), n + 1
        keys.append(key)
        cols.append((key, unit, [float(v) for v in values]))
    if len(cols) < 2:
        raise DataError("%s has fewer than two columns of numbers" % filename)
    choice = next((c for want in prefer for c in cols if c[0] == want), cols[0])
    table = Table(stem, choice[0], choice[1], choice[2])
    for c in cols:
        if c is not choice:
            table.columns["%s.%s" % (stem, c[0])] = (c[1], c[2])
    return table


def frames_table(stem: str, filename: str, frames: List[Any], what: str) -> Table:
    """Camera frames as a Table: a point per frame (x: its number, from 0), the frames kept as a run's camera frames
    are; with several, each one's mean and highest value as columns, so how they change reads as numbers."""
    import numpy as np
    if not frames:
        raise DataError("%s holds no frame" % filename)
    if len(frames) > MAX_FRAMES:
        raise DataError("%s has more than %d frames" % (filename, MAX_FRAMES))
    first = np.asarray(frames[0])
    unit = "counts" if first.dtype.kind in "biu" else ""
    key = "%s.frame" % stem
    table = Table(stem, "frame", "", [float(i) for i in range(len(frames))], frames=(key, unit, frames), fits=False)
    if len(frames) > 1:
        table.columns["%s.mean" % stem] = (unit, [float(np.mean(f)) for f in frames])
        table.columns["%s.max" % stem] = (unit, [float(np.max(f)) for f in frames])
    low = min(float(np.nanmin(f)) for f in frames)
    high = max(float(np.nanmax(f)) for f in frames)
    shapes = {np.asarray(f).shape[:2] for f in frames}
    size = "%dx%d" % (first.shape[1], first.shape[0]) if len(shapes) == 1 else "different sizes"
    table.notes.append("%s: %s, %d frame%s of %s pixels (%s%s), values %g to %g" % (
        filename, what, len(frames), "" if len(frames) == 1 else "s", size, first.dtype.name,
        ", colour" if first.ndim == 3 else "", low, high))
    return table


def read_tiff(p: Path) -> Table:
    from .importers import tiff
    frames = tiff.read(p.read_bytes(), p.name)
    return frames_table(key_of(p.stem, "image"), p.name, frames, "a TIFF image" if len(frames) == 1 else
                        "a TIFF stack")


def read_xlsx(p: Path) -> Table:
    """An Arbin export's Channel sheets one after another, else the workbook's first sheet with numbers."""
    from .importers import arbin, xlsx
    try:
        sheets = xlsx.sheets(p)
    except DataError as e:
        raise DataError("%s: %s" % (p.name, e))
    stem = key_of(p.stem, "data")
    channel = arbin.points_sheets(sheets)
    if channel:
        table = table_from_rows(stem, p.name, arbin.joined(channel), comma=False, grid=False)
        if len(channel) > 1:
            table.notes.append("%s: %d sheets of points, one after another" % (p.name, len(channel)))
        return table
    problems = []
    for n, (_, rows) in enumerate(sheets):
        if not any(any(c not in (None, "") for c in r) for r in rows):
            continue
        try:
            table = table_from_rows(stem, p.name, rows, comma=False)
        except DataError as e:
            problems.append(str(e))
            continue
        if len(sheets) > 1:
            table.notes.append("%s: sheet %d of %d read (the first with numbers)" % (p.name, n + 1, len(sheets)))
        return table
    raise DataError(problems[0] if len(problems) == 1 else "%s holds no sheet with rows of numbers" % p.name)


def _lightfield_csv(stem: str, filename: str, cols: Dict[str, List[Optional[float]]], wl_unit: str) -> Table:
    """LightField's long CSV export (a row per pixel, with Frame and Row): each frame's rows summed per wavelength,
    the frames averaged, as LightField shows a spectrum."""
    wl, it, fr = cols["wavelength"], cols["intensity"], cols.get("frame")
    sums: Dict[float, Dict[float, float]] = {}
    for i, w in enumerate(wl):
        if w is None or it[i] is None:
            continue
        frame = fr[i] if fr is not None and fr[i] is not None else 0.0
        sums.setdefault(w, {}).setdefault(frame, 0.0)
        sums[w][frame] += it[i]
    xs = sorted(sums)
    table = Table(stem, "wavelength", wl_unit or "nm", xs)
    table.columns["%s.counts" % stem] = ("counts", [sum(sums[x].values()) / len(sums[x]) for x in xs])
    frames = len({v for v in fr if v is not None}) if fr is not None else 1
    table.notes.append("%s: LightField's export, rows summed and %d frame%s averaged" % (
        filename, frames, "" if frames == 1 else "s"))
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
    framed = next((t for t in tables if t.frames), None)
    if len(tables) > 1 and framed is not None:
        raise DataError("%s holds camera frames: import it on its own" % framed.name)
    out = Table(first.name, first.x_key, first.x_unit, list(first.x), dict(first.columns), list(first.notes),
                first.outer, first.frames, all(t.fits for t in tables))
    for t in tables[1:]:
        if len(t.x) != len(out.x) or any(abs(a - b) > 1e-6 * max(1.0, abs(a)) for a, b in zip(t.x, out.x)):
            raise DataError("%s's x values differ from %s's: import them one at a time (or export them on the "
                               "same axis)" % (t.name, first.name))
        if (t.outer is None) != (out.outer is None) or (t.outer is not None and out.outer is not None and any(
                abs(a - b) > 1e-6 * max(1.0, abs(a)) for a, b in zip(t.outer[2], out.outer[2]))):
            raise DataError("%s's scan differs from %s's: import them one at a time" % (t.name, first.name))
        for k, v in t.columns.items():
            name = k if k not in out.columns else "%s_%d" % (k, len(out.columns))
            out.columns[name] = v
        out.notes += t.notes
    return out
