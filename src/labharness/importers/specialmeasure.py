"""Special Measure's run files (NNN_name.mat), as its smrun saves them, made into runs.

Special Measure (sm: a MATLAB measurement program; Kelvin reads the files it writes and uses none of its code) saves
each run as a .mat file, the run's number and name in the file's name (NNN_name.mat), holding:
- scan: the scan as it ran. Its loops, a struct array with the innermost (fastest) loop first, each with npoints, rng,
  setchan (the channels it sets), setchanranges (optional: each set channel's own [from to]), getchan (the channels
  it reads), ramptime, waittime, startwait and trafofn; and consts (channels set before it runs: setchan, val, set),
  comments (a char matrix), name, disp and saveloop;
- data: a cell, an array for each channel read, loop 1's first, then loop 2's... Each is outer loop first, down to the
  loop that read it ([npoints(end) ... npoints(i)]), then the reading's own dimensions for a channel that gives an
  array; a point never measured (a run stopped early) is NaN;
- configch and configvals: channels and their values as it started (a snapshot), and configdata.
smrun saves the file as the run starts, without data, and again with them when it ends or is stopped: a file without
data is a run that never ended. A file of a scan alone (smscan) is a saved scan, not a run.

How a run becomes a Table, by smrun's own rules for the values:
- a loop's values: linspace(rng(1), rng(end), npoints); rng itself without npoints; 1 to npoints without rng; with
  setchanranges, each set channel from its own from to its own to over npoints. A set channel whose values a function
  in the scan makes (trafofn) can't be known here: its loop's own value stands for it, and the notes say so;
- x: the innermost loop's first set channel (the loop's own value when it sets none: a count, a time series); the next
  loop out is the outer axis of a 2-D scan; loops further out are columns on each row. The loops' other set channels
  are columns too, after the channels read;
- each channel read is a column. One read once per line (in an outer loop) repeats on each row of its line, as the
  notes say. One that gives an array at each point: a picture (2-D) becomes the run's camera frames, anything else is
  left out, and said; an array per line with a value for each point of the loop inside it is that loop's points (sm's
  line-wise readings);
- points never measured (NaN in every channel read at them) are left out: a run stopped early keeps what it measured;
- the file's words reach the model only as column names (letters, digits and _): the run's number, when MATLAB saved
  the file, the loops' ranges and waits, the scan's constants and the snapshot are said in Kelvin's words, and the
  scan's comments stay in the file.
scan_of gives the scan whole, as plain Python, for what turns it into a Kelvin plan; loops() its loops as smrun runs
them."""
from __future__ import annotations

import glob
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import matfile
from ..imports import MAX_FRAMES, MAX_ROWS, DataError, Table, key_of

RUN = re.compile(r"^(\d+)_")         # NNN_name: the run's number
VARIABLES = ("scan", "data", "configch", "configvals")   # what a run is read from (configdata is never needed)
MAX_LOOP = 10 ** 7                   # points in one loop: a scan that says more is no run Kelvin reads
SHOWN = 40                           # the snapshot's channels said in the notes, at most


@dataclass
class Loop:
    """One of a scan's loops, as smrun runs it. number: 1 for the innermost (fastest)."""
    number: int
    setchans: List[Any]                       # names (str), or channel numbers (int)
    getchans: List[Any]
    npoints: int
    own: List[float]                          # the loop's own value at each of its points
    values: List[Optional[List[float]]]       # each set channel's value at each point; None: a function makes it
    waittime: float = 0.0
    startwait: float = 0.0
    ramptime: List[float] = field(default_factory=list)

    def axis(self) -> Tuple[str, List[float]]:
        """Its axis in a run: its first set channel's key and values, or loopN and its own value when it sets none
        or a function makes that channel's values."""
        if self.setchans and self.values[0] is not None:
            return channel_key(self.setchans[0]), self.values[0]
        return "loop%d" % self.number, self.own


def _np() -> Any:
    import numpy
    return numpy


def items(value: Any) -> List[Any]:
    """A struct array's or a cell's elements in MATLAB's order (a lone struct: itself); nothing: []."""
    np = _np()
    if value is None:
        return []
    if isinstance(value, np.ndarray):
        if not value.size:
            return []
        if value.dtype == object:
            return list(value.reshape(-1, order="F"))
    return [value]


def channels(value: Any) -> List[Any]:
    """A loop's setchan or getchan as smchanlookup takes it: one name (char), names (a cell, or a char matrix's rows),
    or channel numbers; as a list of names (str) and numbers (int)."""
    np = _np()
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [s.strip() for s in value if isinstance(s, str) and s.strip()]
    if isinstance(value, np.ndarray):
        if value.dtype == object:
            return [c for item in value.reshape(-1, order="F") for c in channels(item)]
        if value.dtype.kind in "biuf":
            return [int(v) for v in value.reshape(-1, order="F") if math.isfinite(v)]
    return []


def channel_key(name: Any) -> str:
    """A channel's column key: its name as letters, digits and _ (a channel given by its number: chN)."""
    if isinstance(name, int):
        return "ch%d" % name
    return key_of(name, "channel")


def _numbers(value: Any) -> List[float]:
    np = _np()
    if isinstance(value, np.ndarray) and value.dtype.kind in "biuf":
        return [float(v) for v in value.reshape(-1, order="F")]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [float(value)]
    return []


def _number(value: Any, default: Optional[float] = None) -> Optional[float]:
    found = _numbers(value)
    return found[0] if found else default


def linspace(a: float, b: float, n: int) -> List[float]:
    """MATLAB's linspace: n values from a to b, both ends exactly; a single value is b."""
    if n <= 0:
        return []
    if n == 1:
        return [float(b)]
    if a == b:
        return [float(a)] * n
    out = [a + k * (b - a) / (n - 1) for k in range(n)]
    out[0], out[-1] = float(a), float(b)
    return out


def _function(entry: Any) -> bool:
    """Whether a trafofn entry is a function (a handle, a function's name, a struct whose fn is one); empty is the
    loop's own value."""
    np = _np()
    if isinstance(entry, matfile.Function):
        return True
    if isinstance(entry, str):
        return bool(entry.strip())
    if isinstance(entry, dict):
        return _function(entry.get("fn"))
    if isinstance(entry, np.ndarray) and entry.dtype == object:
        return any(_function(e) for e in entry.reshape(-1))
    return False


def is_scan(value: Any) -> bool:
    """Whether a value is a Special Measure scan: a struct with loops, each a struct."""
    if not isinstance(value, dict):
        return False
    found = items(value.get("loops"))
    return bool(found) and all(isinstance(lp, dict) for lp in found)


def loops(scan: Any) -> List[Loop]:
    """A scan's loops, innermost first, each with its values as smrun makes them (the module's docstring).
    DataError: a loop of more than MAX_LOOP points, or of a number of points that isn't one."""
    shared = any(_function(t) for t in items(scan.get("trafofn")))   # every loop's values pass through these
    out = []
    for i, lp in enumerate(items(scan.get("loops"))):
        sets = channels(lp.get("setchan"))
        ranges = [r for r in (_numbers(x) for x in items(lp.get("setchanranges"))) if r]
        rng = ranges[0] if ranges else _numbers(lp.get("rng"))
        npoints = _number(lp.get("npoints"))
        if npoints is None:
            own = list(rng)
        elif not math.isfinite(npoints) or npoints < 0 or npoints > MAX_LOOP:
            raise DataError("loop %d of the scan has %g points" % (i + 1, npoints))
        elif not rng:
            own = [float(k) for k in range(1, int(npoints) + 1)]
        else:
            own = linspace(rng[0], rng[-1], int(npoints))
        if len(own) > MAX_LOOP:
            raise DataError("loop %d of the scan has more than %d points" % (i + 1, MAX_LOOP))
        n = len(own)
        made = items(lp.get("trafofn"))
        values: List[Optional[List[float]]] = []
        for j in range(len(sets)):
            if shared:
                values.append(None)
            elif j < len(ranges):   # smrun maps the loop's range onto each channel's own
                values.append(linspace(ranges[j][0], ranges[j][-1], n))
            elif j < len(made) and _function(made[j]):
                values.append(None)
            else:
                values.append(list(own))
        out.append(Loop(i + 1, sets, channels(lp.get("getchan")), n, own, values,
                        _number(lp.get("waittime"), 0.0) or 0.0, _number(lp.get("startwait"), 0.0) or 0.0,
                        _numbers(lp.get("ramptime"))))
    return out


# ------------------------------------------------------------------ what the file is

def is_run(path: Any) -> bool:
    """Whether a file is a Special Measure run file: a .mat holding a scan with loops, as smrun saves it (with its
    data, unless the run never ended). Only the scan is read."""
    try:
        if not any(v["name"] == "scan" and v["class"] == "struct" for v in matfile.whos(path)):
            return False
        return is_scan(matfile.load(path, ["scan"]).get("scan"))
    except DataError:
        return False


def scan_of(path: Any) -> Dict[str, Any]:
    """The scan a Special Measure file holds (a run's scan, or a saved smscan), whole, as plain Python (matfile.plain):
    dicts, lists, numbers, str, function handles as their text (matfile.Function, a str). Its loops, consts and disp
    are always lists, of one loop too; the rest is as MATLAB saved it (setchan: a name, or a list of them; a 1x1 value
    a number). The comments are in it: words from the file, for a person to read or for the model as outside text.
    DataError: no scan in it."""
    p = Path(str(path)).expanduser()
    variables = matfile.load(p, ["scan", "smscan"])
    scan = next((variables[k] for k in ("scan", "smscan") if is_scan(variables.get(k))), None)
    if scan is None:
        raise DataError("%s holds no Special Measure scan (a scan or smscan with loops)" % p.name)
    out = matfile.plain(scan)
    for k in ("loops", "consts", "disp"):
        if k in scan:
            out[k] = [matfile.plain(v) for v in items(scan[k])]
    return out


def read_run(path: Any) -> Table:
    """A Special Measure run file as a Table (the module's docstring says how). DataError: not one, or one Kelvin
    can't lay out, saying why."""
    p = Path(str(path)).expanduser()
    return run_table(p, matfile.load(p, VARIABLES))


# ------------------------------------------------------------------ a run's Table

def _temporary(p: Path) -> str:
    """Where a run that never ended left its points: smrun's temporary copies beside it (NAME-tempN.mat~, written as
    it measured and deleted when it ends), the latest by its time holding the most."""
    try:
        found = sorted(p.parent.glob(glob.escape(p.stem) + "-temp*.mat~"), key=lambda f: f.stat().st_mtime)
    except OSError:
        found = []
    if not found:
        return "smrun's temporary copies of it (%s-temp<N>.mat~) would hold what it measured, and none is beside it" % (
            p.stem)
    return "its temporary copy %s (the latest of %d beside it) holds what it had measured by then: import that" % (
        found[-1].name, len(found))


def _strip(shape: Sequence[int]) -> Tuple[int, ...]:
    """A shape without its trailing ones (MATLAB keeps none past the second dimension)."""
    s = list(shape)
    while s and s[-1] == 1:
        s.pop()
    return tuple(s)


def _span(values: Sequence[float]) -> str:
    if not values:
        return "no values"
    return "at %g" % values[0] if len(values) == 1 or values[0] == values[-1] else \
        "from %g to %g" % (values[0], values[-1])


def _names(keys: Sequence[str]) -> str:
    return keys[0] if len(keys) == 1 else ", ".join(keys[:-1]) + " and " + keys[-1]


def run_table(p: Path, variables: Dict[str, Any]) -> Table:
    """A run file's variables (matfile.load) as a Table. DataError, saying why, for one that isn't as smrun saves
    them (never a bare error: the file is someone else's)."""
    try:
        return _run_table(p, variables)
    except DataError:
        raise
    except matfile.UNFORESEEN as e:
        raise DataError("%s: its scan or data aren't laid out as Special Measure saves them (%s)" % (
            p.name, str(e).splitlines()[0][:120] if str(e) else type(e).__name__))


def _run_table(p: Path, variables: Dict[str, Any]) -> Table:
    np = _np()
    scan = variables.get("scan")
    if not is_scan(scan):
        raise DataError("%s is not a Special Measure run: it holds no scan with loops" % p.name)
    if "data" not in variables:
        raise DataError("%s holds Special Measure's scan but no data: smrun saves a run's data when it ends, and this "
                        "one never did; %s" % (p.name, _temporary(p)))
    data = variables["data"]
    if not (isinstance(data, np.ndarray) and data.dtype == object):
        raise DataError("%s: its data aren't a cell of arrays, as Special Measure saves them" % p.name)
    try:
        lps = loops(scan)
    except DataError as e:
        raise DataError("%s: %s" % (p.name, e))
    stem = key_of(p.stem, "sm_run")
    notes: List[str] = []
    arrays = items(data)
    named = sum(len(lp.getchans) for lp in lps)
    if named == len(arrays):
        listed = [(channel_key(c), lp.number - 1) for lp in lps for c in lp.getchans]
    else:   # a procfn's outputs, or data changed after the run: placed by their shapes alone
        listed = [("data%d" % (k + 1), None) for k in range(len(arrays))]
        notes.append("%s: its scan names %d channel%s read but its data hold %d arrays: columns are named by their "
                     "place in data (data1, data2...)" % (p.name, named, "" if named == 1 else "s", len(arrays)))
    ns = [lp.npoints for lp in lps]
    depth = len(ns)
    if not all(ns):
        raise DataError("%s: loop %d of its scan has no points" % (p.name, ns.index(0) + 1))
    prefix = [tuple(reversed(ns[b:])) for b in range(depth)]   # a reading at loop b's points: outer loop first
    scalars: List[Tuple[str, int, Any]] = []
    pictures: List[Tuple[str, int, Any]] = []
    left: List[str] = []
    for (key, j), a in zip(listed, arrays):
        if not isinstance(a, np.ndarray) or a.dtype.kind not in "biufc":
            left.append("%s (not numbers)" % key)
            continue
        if a.dtype.kind == "b":
            a = a.astype(np.uint8)
        top = depth - 1 if j is None else j
        b = next((b for b in range(top + 1) if _strip(a.shape) == _strip(prefix[b])), None)
        if b is not None:   # a number at each point of loop b (read in an outer loop: a value per point inside)
            flat = a.reshape(-1)
            if flat.dtype.kind == "c":
                scalars += [(key + "_re", b, flat.real), (key + "_im", b, flat.imag)]
            else:
                scalars.append((key, b, flat))
            continue
        for jj in (range(depth) if j is None else [j]):
            pre = prefix[jj]
            if tuple(a.shape[:len(pre)]) != pre:
                continue
            extra = _strip(a.shape[len(pre):])
            if len(extra) == 2 and a.dtype.kind != "c":
                pictures.append((key, jj, a.reshape((-1,) + extra)))
            else:
                left.append("%s (%s at each point)" % (key, "a picture of complex numbers" if len(extra) == 2 else
                                                        "an array of %s values" % "x".join(str(d) for d in extra)))
            break
        else:
            left.append("%s (an array of %s, which fits none of the scan's loops)" % (
                key, "x".join(str(d) for d in a.shape)))
    found = [b for _, b, _ in scalars] + [j for _, j, _ in pictures]
    if not found:
        raise DataError("%s: none of its channels gives a number or a picture at each point%s" % (
            p.name, ": " + ", ".join(left) if left else " (its scan reads none)"))
    b0 = min(found)
    rows = math.prod(ns[b0:])
    if rows > 50 * MAX_ROWS:
        raise DataError("%s has more than %d points" % (p.name, MAX_ROWS))
    framed = [(k, j, a) for k, j, a in pictures if j == b0]
    for k, j, _ in pictures:
        if j != b0:
            left.append("%s (a picture once per point of loop %d)" % (k, j + 1))
    for k, _, _ in framed[1:]:
        left.append("%s (a second picture at each point: a run keeps one)" % k)
    framed = framed[:1]
    measured = np.zeros(rows, dtype=bool)
    for _, b, v in scalars:
        if b == b0:
            measured |= ~np.isnan(v.astype(float))
    for _, _, a in framed:
        measured |= ~np.all(np.isnan(a.reshape(rows, -1).astype(float)), axis=1)
    kept = np.flatnonzero(measured)
    if not kept.size:
        raise DataError("%s holds no measured point: every reading is NaN (the run stopped before its first)" % p.name)
    if kept.size > MAX_ROWS:
        raise DataError("%s has more than %d points" % (p.name, MAX_ROWS))
    if framed and kept.size > MAX_FRAMES:
        raise DataError("%s has more than %d frames" % (p.name, MAX_FRAMES))

    def per_row(values: Sequence[float], i: int) -> List[float]:
        """Loop i's value at each kept row."""
        spread = np.tile(np.repeat(np.asarray(values, dtype=float), math.prod(ns[b0:i])), math.prod(ns[i + 1:]))
        return [float(v) for v in spread[kept]]

    x_key, x_values = lps[b0].axis()
    table = Table(stem, x_key, "", per_row(x_values, b0))
    if depth > b0 + 1:   # the next loop out: a 2-D scan's slow axis
        key, values = lps[b0 + 1].axis()
        table.outer = (key if key != x_key else key + "_2", "", per_row(values, b0 + 1))

    def column(key: str) -> str:
        name, k = "%s.%s" % (stem, key), 2
        while name in table.columns:
            name, k = "%s.%s_%d" % (stem, key, k), k + 1
        return name
    for key, b, v in scalars:   # the channels read, in data's order
        spread = np.repeat(v.astype(float), math.prod(ns[b0:b]))
        table.columns[column(key)] = ("", [float(x) for x in spread[kept]])
    made_by: List[Tuple[str, Optional[str]]] = []   # (a channel a function sets, the loop value standing for it)
    for lp in lps[b0:]:   # the loops' other set channels (and the further loops' own), after what was read
        axis = lp.number - 1 <= b0 + 1
        for k in range(1 if axis else 0, len(lp.setchans)):
            if lp.values[k] is not None:
                table.columns[column(channel_key(lp.setchans[k]))] = ("", per_row(lp.values[k], lp.number - 1))
        own = not any(v is not None for v in lp.values)
        if not axis and own:
            table.columns[column("loop%d" % lp.number)] = ("", per_row(lp.own, lp.number - 1))
        made_by += [(channel_key(c), "loop%d" % lp.number if (k == 0 and axis) or own else None)
                    for k, (c, v) in enumerate(zip(lp.setchans, lp.values)) if v is None]
    if framed:
        key, _, a = framed[0]
        table.frames = ("%s.%s" % (stem, key), "", [a[i] for i in kept])
        table.fits = False

    said = _notes(p, scan, variables, lps, b0, rows, kept, scalars, left, made_by)
    table.notes = said[:1] + notes + said[1:]   # which run it is first, then how its data were named
    return table


def _sweep(lp: Loop, noun: str) -> str:
    """How a loop swept, in words: its points, its first set channel's span, and the channels set with it."""
    keys = [(channel_key(c), v) for c, v in zip(lp.setchans, lp.values) if v is not None]
    if lp.npoints == 1:
        noun = noun.replace("points", "point").replace("values", "value")
    if not keys:
        return "%d %s loop %d's own value (%s)" % (lp.npoints, noun, lp.number, _span(lp.own))
    words = "%d %s %s (%s)" % (lp.npoints, noun, keys[0][0], _span(keys[0][1]))
    if len(keys) > 1:
        words += " with %s together" % _names(["%s (%s)" % (k, _span(v)) for k, v in keys[1:]])
    return words


def _notes(p: Path, scan: Dict[str, Any], variables: Dict[str, Any], lps: List[Loop], b0: int, rows: int,
           kept: Any, scalars: List[Tuple[str, int, Any]], left: List[str],
           made_by: List[Tuple[str, Optional[str]]]) -> List[str]:
    """What a run's notes say, in Kelvin's words (the file's own only as keys and numbers): which run it is and how
    it swept, what was left out and why, what repeats, its waits, its constants, its snapshot, and that its comments
    stay in the file."""
    ns = [lp.npoints for lp in lps]
    depth = len(lps)
    number = RUN.match(p.stem)
    try:
        saved = matfile.header(p)["created"]
    except DataError:
        saved = None
    about = "Special Measure run %d" % int(number.group(1)) if number else "a Special Measure run"
    if saved is not None:
        about += ", saved %s" % saved.isoformat(sep=" ", timespec="seconds")
    if depth == b0 + 1:
        about += "; %s" % _sweep(lps[b0], "points of")
    else:
        about += "; a 2-D scan, %s, each over %s" % (_sweep(lps[b0 + 1], "values of"), _sweep(lps[b0], "of"))
        if depth > b0 + 2:
            about += "; repeated over %s (columns)" % "; ".join("loop %d: %s" % (lp.number, _sweep(lp, "values of"))
                                                              for lp in lps[b0 + 2:])
    out = ["%s: %s" % (p.name, about)]
    dropped = rows - kept.size
    if dropped and kept[-1] + 1 == kept.size:   # every point after the last one measured: a run stopped early
        out.append("%s: stopped early, %s: the %d points it never measured are left out" % (
            p.name, "after %d of its %d points" % (kept.size, rows) if depth == b0 + 1 else
            "in line %d of %d" % (kept[-1] // ns[b0] + 1, rows // ns[b0]), dropped))
    elif dropped:
        out.append("%s: %d point%s without a reading in any channel left out" % (
            p.name, dropped, "" if dropped == 1 else "s"))
    for b in sorted({b for _, b, _ in scalars if b > b0}):
        out.append("%s: %s read once per %s (in loop %d): each value repeats on the %d rows it covers" % (
            p.name, _names([k for k, bb, _ in scalars if bb == b]), "line" if b == b0 + 1 else
            "value of loop %d" % (b + 1), b + 1, math.prod(ns[b0:b])))
    if left:
        out.append("%s: left out: %s" % (p.name, ", ".join(left)))
    if made_by:
        out.append("%s: functions in the scan (trafofn) set %s, and Kelvin doesn't run them: %s" % (
            p.name, _names([k for k, _ in made_by]), "; ".join(
                "%s stands for %s" % (own, k) if own else "%s left out" % k for k, own in made_by)))
    if b0:
        inside = _names(["loop %d (%s)" % (lp.number, lp.axis()[0]) for lp in lps[:b0]])
        out.append("%s: %s read%s nothing: each row is a point of loop %d, read after %s swept" % (
            p.name, inside, "s" if b0 == 1 else "", b0 + 1, "it" if b0 == 1 else "they"))
    for lp in lps[b0:]:   # how it waited and ramped: smrun's waittime, startwait and ramptime
        waits = []
        if lp.waittime > 0:
            waits.append("%g s at each point" % lp.waittime)
        if lp.startwait > 0:
            waits.append("%g s more at its first" % lp.startwait if lp.waittime > 0 else
                         "%g s at its first point" % lp.startwait)
        said = ["waits " + ", ".join(waits)] if waits else []
        ramps = [r for r in lp.ramptime if r == r and r]
        if ramps and min(ramps) < 0:   # a negative ramptime: the instrument ramps through the loop by itself
            said.append("is ramped by the instrument, %g s a point" % abs(min(ramps)))
        elif ramps:
            said.append("ramps %g s to each point" % max(ramps))
        if said:
            out.append("%s: loop %d (%s) %s" % (p.name, lp.number, lp.axis()[0], "; ".join(said)))
    held = []
    for c in items(scan.get("consts")):
        if not isinstance(c, dict):
            continue
        names, value, applied = channels(c.get("setchan")), _number(c.get("val")), _number(c.get("set"), 1.0)
        if names and value is not None and applied:
            held.append("%s %g" % (channel_key(names[0]), value))
    if held:
        out.append("%s: set before it ran (the scan's constants): %s" % (p.name, ", ".join(held)))
    names, values = channels(variables.get("configch")), _numbers(variables.get("configvals"))
    if names and len(names) == len(values):
        pairs = ["%s %g" % (channel_key(n), v) for n, v in zip(names, values)]
        out.append("%s: as it started (configch): %s%s" % (p.name, ", ".join(pairs[:SHOWN]), (
            ", and %d more in the file" % (len(pairs) - SHOWN)) if len(pairs) > SHOWN else ""))
    elif names or values:
        out.append("%s: its snapshot (configch, configvals) pairs %d channels with %d values: left out" % (
            p.name, len(names), len(values)))
    comments = scan.get("comments")
    lines = [ln for ln in (comments if isinstance(comments, list) else [comments] if isinstance(comments, str)
                           else []) if isinstance(ln, str) and ln.strip()]
    if lines:
        out.append("%s: the scan's comments (%d line%s) stay in the file" % (
            p.name, len(lines), "" if len(lines) == 1 else "s"))
    return out
