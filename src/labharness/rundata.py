"""A run's data: one HDF5 file per run (run.h5), written from the record kept while the run is measured.

While a plan runs, every point is appended to the run's record (record.bin) the moment it is measured: written and
flushed at once, and synced to disk about once a second. A crash loses at most the point being measured, a power cut
at most the last second or so. When the run ends, Kelvin writes run.h5 from the record, checks it, and removes the
record (and the camera frames it packed in). A run cut short by a crash is finished the same way at Kelvin's next
start, with the status "interrupted".

run.h5 is the run's one data file. h5py, MATLAB (h5read), Igor Pro, Origin and HDFView open it:

    /                attributes: the run (format, run_id, plan, status, started, finished, points, project,
                     device...); meta_json holds all of it as JSON
    /plan            the plan as run (YAML text)
    /points/<key>    one 1-D array per scalar column (i, t, each swept axis and each reading), with the
                     attributes unit, role (index, time, axis or measured), kind and quantity
    /traces/<key>    one 2-D array (points x samples) per trace reading, NaN-padded; <key>.length when the
                     traces' lengths differ, and <key>.x (each point's x values) or <key>.axis (start and stop)
    /frames/<key>    one 3-D array (points x rows x columns) per camera reading, compressed
    /conditions      start and end: every instrument setting at the start and the end (JSON text)
    /analysis        the automatic fits and assessment (JSON text)
    /card            what an AI reads first about the run (JSON text)

Without h5py (Kelvin's core alone) a run keeps its record, and everything here reads that instead."""
from __future__ import annotations

import json
import math
import os
import shutil
import struct
import sys
import time
import zlib
from array import array
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

RUN_FILE = "run.h5"
RECORD = "record.bin"
LOCK = "record.lock"     # held while a Kelvin records the run
FORMAT = "kelvin-run/1"
MAGIC = b"KELVIN-RECORD 1\n"
_HEAD = struct.Struct("<II")     # a frame: payload length, CRC-32 of the payload
SYNC_EVERY = 1.0                 # seconds between syncs of the record to disk
LEGACY = ("data.jsonl", "meta.json", "plan.yaml", "analysis.json", "snapshot_start.json", "snapshot_end.json",
          "data.csv", "conditions.csv")
INTERRUPTED = "interrupted"      # status of a run that ended with Kelvin (a crash, a power cut)
CRASH_REASON = "Kelvin stopped while this run was measured (a crash or a power cut); every point up to then is kept"


_H5: Optional[bool] = None


def have_h5() -> bool:
    """Whether h5py (and numpy) are there to keep runs as run.h5."""
    global _H5
    if _H5 is None:
        try:
            import h5py  # noqa: F401
            import numpy  # noqa: F401
            _H5 = True
        except ImportError:
            _H5 = False
    return _H5


def _jsonable(v: Any) -> Any:
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (set, frozenset)):
        return sorted(_jsonable(x) for x in v)
    if isinstance(v, tuple):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    try:
        json.dumps(v)
        return v
    except TypeError:
        return str(v)


def _numbers(v: Any) -> Optional[array]:
    """A list of numbers (a trace) as 64-bit floats (None as NaN), or None when it isn't one."""
    if not isinstance(v, (list, tuple)) or not v:
        return None
    out = array("d")
    for x in v:
        if x is None:
            out.append(float("nan"))
        elif isinstance(x, (int, float)) and not isinstance(x, bool):
            out.append(float(x))
        else:
            return None
    return out


# ------------------------------------------------------------------ the record (while measuring)

def _frame(kind: str, fields: Optional[Dict[str, Any]] = None, values: Optional[Dict[str, Any]] = None) -> bytes:
    """One frame of a record: its length and CRC-32, then a JSON line, then the trace values as little-endian
    64-bit floats (head["blobs"] names them and their counts)."""
    head: Dict[str, Any] = {"t": kind}
    head.update({k: _jsonable(v) for k, v in (fields or {}).items()})
    blobs: List[Tuple[str, array]] = []
    if values is not None:
        plain: Dict[str, Any] = {}
        for k, v in values.items():
            nums = _numbers(v)
            if nums is not None:
                blobs.append((k, nums))
            else:
                plain[k] = _jsonable(v)
        head["v"] = plain
        if blobs:
            head["blobs"] = [[k, len(a)] for k, a in blobs]
    payload = json.dumps(head, ensure_ascii=False).encode("utf-8") + b"\n"
    for _, a in blobs:
        if sys.byteorder == "big":
            a.byteswap()
        payload += a.tobytes()
    return _HEAD.pack(len(payload), zlib.crc32(payload) & 0xFFFFFFFF) + payload


class Recorder:
    """Appends a run's frames to its record: each one flushed at once, synced to disk about once a second. While it
    records, it holds a lock on the run (record.lock), so no Kelvin takes the run for one a crash ended."""

    def __init__(self, folder: Path):
        self.path = Path(folder) / RECORD
        self._lock = _hold(Path(folder) / LOCK)
        new = not self.path.exists()
        self._f = open(self.path, "ab")
        if new:
            self._f.write(MAGIC)
        self._synced = time.monotonic()

    def write(self, kind: str, fields: Optional[Dict[str, Any]] = None, values: Optional[Dict[str, Any]] = None,
              sync: bool = False) -> None:
        self._f.write(_frame(kind, fields, values))
        self._f.flush()
        now = time.monotonic()
        if sync or now - self._synced >= SYNC_EVERY:
            os.fsync(self._f.fileno())
            self._synced = now

    def close(self) -> None:
        if not self._f.closed:
            self._f.flush()
            os.fsync(self._f.fileno())
            self._f.close()

    def release(self) -> None:
        """Let go of the run (after close, once it is kept as it should be)."""
        self.close()
        if self._lock is not None:
            _let_go(self._lock)
            self._lock = None
            try:
                (self.path.parent / LOCK).unlink()
            except OSError:
                pass


def _hold(path: Path) -> Optional[Any]:
    """Lock a file for as long as it stays open (the system lets go when the process ends, even in a crash).
    None when another Kelvin, or this one elsewhere, holds it."""
    try:
        f = open(path, "a+b")
    except OSError:
        return None
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def _let_go(f: Any) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    f.close()


def being_recorded(folder: Path) -> bool:
    """Whether a Kelvin (this one or another) is recording this run now."""
    path = Path(folder) / LOCK
    if not path.exists() or not (Path(folder) / RECORD).exists():
        return False
    held = _hold(path)
    if held is None:
        return True
    _let_go(held)
    return False


def closed(folder: Path) -> bool:
    """Whether a run's record ends with its finish frame (a look at the end of the file only)."""
    path = Path(folder) / RECORD
    with open(path, "rb") as f:
        f.seek(max(0, path.stat().st_size - (1 << 18)))
        return b'{"t": "finish"' in f.read()


@dataclass
class Recorded:
    """What a record holds (or a run.h5 read back): the run as Kelvin keeps it."""
    plan_text: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)
    columns: Dict[str, Any] = field(default_factory=lambda: {"axes": [], "measures": []})
    snapshots: Dict[str, Any] = field(default_factory=dict)
    rows: List[Dict[str, Any]] = field(default_factory=list)
    analysis: Optional[Dict[str, Any]] = None
    finished: bool = False
    torn: bool = False          # the record ended mid-frame (a crash while writing the last point)
    whole: int = 0              # bytes of the record up to its last whole frame


def _frames(data: bytes) -> Iterator[Tuple[Dict[str, Any], bytes, int]]:
    if not data.startswith(MAGIC):
        raise ValueError("not a Kelvin run record")
    pos = len(MAGIC)
    while pos < len(data):
        if pos + _HEAD.size > len(data):
            raise EOFError
        n, crc = _HEAD.unpack_from(data, pos)
        payload = data[pos + _HEAD.size:pos + _HEAD.size + n]
        if len(payload) < n or zlib.crc32(payload) & 0xFFFFFFFF != crc:
            raise EOFError
        nl = payload.index(b"\n")
        pos += _HEAD.size + n
        yield json.loads(payload[:nl].decode("utf-8")), payload[nl + 1:], pos


def read_record(folder: Path) -> Recorded:
    """A run's record, up to its last whole frame."""
    rec = Recorded(whole=len(MAGIC))
    try:
        for head, blob, end in _frames((Path(folder) / RECORD).read_bytes()):
            kind = head.get("t")
            if kind == "start":
                rec.plan_text = str(head.get("plan") or "")
                rec.meta.update(head.get("meta") or {})
            elif kind == "columns":
                rec.columns = head.get("columns") or rec.columns
            elif kind == "snapshot":
                rec.snapshots[str(head.get("kind"))] = head.get("data") or {}
            elif kind == "row":
                row = dict(head.get("v") or {})
                off = 0
                for name, count in head.get("blobs") or []:
                    vals = array("d")
                    vals.frombytes(blob[off:off + 8 * count])
                    if sys.byteorder == "big":
                        vals.byteswap()
                    off += 8 * count
                    row[name] = [None if math.isnan(x) else x for x in vals]
                rec.rows.append(row)
            elif kind == "meta":
                rec.meta.update(head.get("meta") or {})
            elif kind == "analysis":
                rec.analysis = head.get("analysis")
            elif kind == "finish":
                rec.meta.update(head.get("meta") or {})
                rec.finished = True
            rec.whole = end
    except EOFError:
        rec.torn = True
    return rec


# ------------------------------------------------------------------ what each column is

QUANTITY_BY_UNIT = {"V": "voltage", "A": "current", "K": "temperature", "T": "magnetic_field", "Hz": "frequency",
                    "s": "time", "W": "power", "mW": "power", "dBm": "power", "dB": "ratio", "Ω": "resistance",
                    "Ohm": "resistance", "deg": "phase", "°": "phase", "rad": "phase", "m": "position",
                    "mm": "position", "µm": "position", "um": "position", "nm": "wavelength",
                    "counts": "counts", "counts/s": "count_rate", "Pa": "pressure", "mbar": "pressure",
                    "Torr": "pressure", "T/min": "field_rate", "K/min": "temperature_rate", "PLC": "integration"}


def quantity_of(key: str, unit: str) -> str:
    """A plain physical quantity for a column (voltage, temperature...), so runs from different instruments and
    labs compare: from its unit, else its name."""
    name = key.rsplit(".", 1)[-1].lower()
    if unit == "nm" and not any(w in name for w in ("wave", "lambda", "wl")):
        return "position"
    q = QUANTITY_BY_UNIT.get(unit or "")
    if q:
        return q
    for word, q in (("temp", "temperature"), ("field", "magnetic_field"), ("freq", "frequency"),
                    ("phase", "phase"), ("power", "power"), ("count", "counts"), ("wave", "wavelength")):
        if word in name:
            return q
    return ""


def column_info(columns: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    info: Dict[str, Dict[str, str]] = {"i": {"role": "index", "unit": "", "kind": "scalar"},
                                       "t": {"role": "time", "unit": "s", "kind": "scalar"}}
    for a in columns.get("axes") or []:
        info[a["key"]] = {"role": "axis", "unit": a.get("unit") or "", "kind": a.get("kind") or "instrument"}
    for m in columns.get("measures") or []:
        info[m["key"]] = {"role": "measured", "unit": m.get("unit") or "", "kind": m.get("kind") or "scalar",
                          "x_unit": m.get("x_unit") or ""}
    for k, v in info.items():
        v["quantity"] = quantity_of(k, v["unit"])
    return info


# ------------------------------------------------------------------ the card: what an AI reads first

def _round(v: Any) -> Any:
    """Four significant digits: enough to see what happened, cheap to read."""
    if isinstance(v, float) and math.isfinite(v):
        return float("%.4g" % v)
    return v


def column_stats(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Each scalar column's count, range and mean; each trace column's count and length."""
    out: Dict[str, Dict[str, Any]] = {}
    for k in dict.fromkeys(k for r in rows for k in r):
        vals = [r.get(k) for r in rows]
        nums = [float(v) for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(float(v))]
        if nums:
            out[k] = {"n": len(nums), "min": min(nums), "max": max(nums), "mean": sum(nums) / len(nums),
                      "first": nums[0], "last": nums[-1]}
        elif any(isinstance(v, list) for v in vals):
            lengths = [len(v) for v in vals if isinstance(v, list)]
            out[k] = {"n": len(lengths), "samples": max(lengths) if lengths else 0, "trace": True}
    return out


def make_card(rec: Recorded, stats: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The run in a few lines, for an AI to read before it asks for any data: what was swept and measured (with
    units and ranges), the conditions held, the fitted results with uncertainties, the assessment and the issues."""
    meta, info = rec.meta, column_info(rec.columns)
    stats = stats if stats is not None else column_stats(rec.rows)
    swept = []
    for a in rec.columns.get("axes") or []:
        s = stats.get(a["key"]) or {}
        swept.append({k: _round(v) for k, v in (("key", a["key"]), ("unit", a.get("unit") or ""),
                                                ("from", s.get("first")), ("to", s.get("last")),
                                                ("min", s.get("min")), ("max", s.get("max"))) if v not in (None, "")})
    measured = []
    for m in rec.columns.get("measures") or []:
        s = stats.get(m["key"]) or {}
        item = {"key": m["key"], "unit": m.get("unit") or "", "kind": m.get("kind") or "scalar",
                "quantity": info.get(m["key"], {}).get("quantity", "")}
        if "min" in s:
            item.update(min=_round(s["min"]), max=_round(s["max"]), mean=_round(s["mean"]))
        if s.get("trace"):
            item["samples"] = s.get("samples")
        measured.append({k: v for k, v in item.items() if v not in (None, "")})
    results = []
    a = rec.analysis or {}
    for series in a.get("series") or []:
        for r in series.get("results") or []:
            label = series.get("y") or ""
            if series.get("group"):
                label += " (%s)" % series["group"]
            results.append("%s: %s %s" % (label, r.get("name"), r.get("value")))
    done, planned = meta.get("points", len(rec.rows)), meta.get("planned_points")
    card = {"run": meta.get("run_id"), "plan": meta.get("plan"), "status": meta.get("status"),
            "points": "%s of %s" % (done, planned) if planned else done,
            "started": meta.get("started"), "duration_s": meta.get("duration_s"),
            "device": meta.get("device_name"), "project": meta.get("project_name"),
            "lab": meta.get("lab"), "simulated": meta.get("simulated"),
            "swept": swept, "measured": measured, "held": meta.get("held") or {},
            "results": results[:12], "assessment": a.get("assessment") or "",
            "issues": (a.get("issues") or [])[:8] + list(meta.get("warnings") or [])[:4],
            "reason": meta.get("reason"), "file": RUN_FILE}
    return {k: v for k, v in card.items() if v not in (None, "", [], {})}


# ------------------------------------------------------------------ run.h5

def _attr(v: Any) -> Any:
    """A value HDF5 can keep as an attribute (lists and dicts as JSON)."""
    if isinstance(v, (str, bool, int, float)):
        return v
    return json.dumps(_jsonable(v), ensure_ascii=False)


def write_h5(folder: Path, rec: Recorded) -> Path:
    """Write run.h5 from a run's record (or rows read from older files); returns its path. Written to a temporary
    name and renamed, so a run.h5 is never half there."""
    import h5py
    import numpy as np
    folder = Path(folder)
    info = column_info(rec.columns)
    rows = rec.rows
    n = len(rows)
    meta = dict(rec.meta)
    meta.setdefault("points", n)
    stats = column_stats(rows)
    card = make_card(rec, stats)
    order = ["i", "t"] + [a["key"] for a in rec.columns.get("axes") or []] + \
        [m["key"] for m in rec.columns.get("measures") or []]
    keys = list(dict.fromkeys(order + [k for r in rows for k in r]))
    keys = [k for k in keys if any(k in r for r in rows)]
    frame_keys = [k for k in keys if any(isinstance(r.get(k), str) and r.get(k, "").startswith("frames/")
                                         for r in rows)]
    aux = [k for k in keys if (k.endswith(".x") or k.endswith(".axis")) and k.rsplit(".", 1)[0] in keys]
    tmp = folder / (RUN_FILE + ".tmp")
    with h5py.File(tmp, "w") as f:
        f.attrs["format"] = FORMAT
        for k in ("run_id", "plan", "status", "started", "finished", "points", "planned_points", "duration_s",
                  "reason", "operator", "user", "lab", "simulated", "project", "project_name", "device",
                  "device_name", "labharness"):
            if meta.get(k) is not None:
                f.attrs[k] = _attr(meta[k])
        f.attrs["meta_json"] = json.dumps(_jsonable(meta), ensure_ascii=False)
        f.attrs["columns_json"] = json.dumps(_jsonable(rec.columns), ensure_ascii=False)
        text = h5py.string_dtype()
        f.create_dataset("plan", data=rec.plan_text or "", dtype=text)
        cond = f.create_group("conditions")
        for kind in ("start", "end"):
            cond.create_dataset(kind, data=json.dumps(_jsonable(rec.snapshots.get(kind) or {}), ensure_ascii=False),
                                dtype=text)
        f.create_dataset("analysis", data=json.dumps(_jsonable(rec.analysis) if rec.analysis else None), dtype=text)
        f.create_dataset("card", data=json.dumps(_jsonable(card), ensure_ascii=False), dtype=text)
        points, traces, framed = f.create_group("points"), f.create_group("traces"), f.create_group("frames")
        for k in keys:
            if k in frame_keys or k in aux:
                continue
            vals = [r.get(k) for r in rows]
            col = info.get(k, {"role": "measured", "unit": "", "kind": "scalar", "quantity": quantity_of(k, "")})
            if any(isinstance(v, list) for v in vals):
                ds = _traces(traces, k, vals, np)
                for a in (".x", ".axis"):
                    if k + a in keys:
                        _traces(traces, k + a, [r.get(k + a) for r in rows], np)
                if col.get("x_unit"):
                    ds.attrs["x_unit"] = col["x_unit"]
            elif all(v is None or isinstance(v, (int, float)) for v in vals):
                is_bool = any(isinstance(v, bool) for v in vals)
                ds = points.create_dataset(k, data=np.array([float("nan") if v is None else float(v) for v in vals],
                                                            dtype="f8"), chunks=n > 4096 or None)
                if is_bool:
                    ds.attrs["type"] = "bool"
            else:
                ds = points.create_dataset(k, data=["" if v is None else str(v) for v in vals], dtype=text)
            for a in ("unit", "role", "kind", "quantity"):
                ds.attrs[a] = col.get(a) or ""
        for k in frame_keys:
            _frames_into(framed, folder, k, [r.get(k) for r in rows], np)
            framed[k].attrs.update({"role": "measured", "kind": "image", "unit": info.get(k, {}).get("unit", "")})
    os.replace(tmp, folder / RUN_FILE)
    return folder / RUN_FILE


def _traces(group: Any, key: str, vals: List[Any], np: Any) -> Any:
    lengths = [len(v) if isinstance(v, list) else 0 for v in vals]
    width = max(lengths) if lengths else 0
    data = np.full((len(vals), width), np.nan)
    for i, v in enumerate(vals):
        if isinstance(v, list) and v:
            data[i, :len(v)] = [float("nan") if x is None else float(x) for x in v]
    ds = group.create_dataset(key, data=data, chunks=(1, max(1, width)) if width and len(vals) > 1 else None,
                              compression="gzip" if data.size > 100000 else None)
    if len(set(lengths)) > 1:
        group.create_dataset(key + ".length", data=np.array(lengths, dtype="i4"))
    return ds


def _frames_into(group: Any, folder: Path, key: str, refs: List[Any], np: Any) -> None:
    arrays = []
    for ref in refs:
        try:
            arrays.append(np.load(str(folder / str(ref)), allow_pickle=False) if ref else None)
        except (OSError, ValueError):
            arrays.append(None)
    shapes = {(a.shape, a.dtype.str) for a in arrays if a is not None}
    if len(shapes) == 1 and all(a is not None for a in arrays):
        (shape, dtype), = shapes
        ds = group.create_dataset(key, shape=(len(arrays),) + tuple(shape), dtype=dtype,
                                  chunks=(1,) + tuple(shape), compression="gzip", compression_opts=4)
        for i, a in enumerate(arrays):
            ds[i] = a
    else:   # frames of different sizes, or some missing: one dataset per point
        sub = group.create_group(key)
        for i, a in enumerate(arrays):
            if a is not None:
                sub.create_dataset("%05d" % i, data=a, compression="gzip", compression_opts=4)


def read_h5(folder: Path, with_rows: bool = True) -> Recorded:
    """run.h5 read back as the record it came from (rows as Kelvin keeps them, frames as their place in run.h5)."""
    import h5py
    rec = Recorded(finished=True)
    with h5py.File(Path(folder) / RUN_FILE, "r") as f:
        rec.meta = json.loads(f.attrs.get("meta_json", "{}"))
        rec.columns = json.loads(f.attrs.get("columns_json", '{"axes": [], "measures": []}'))
        rec.plan_text = _text(f.get("plan"))
        rec.snapshots = {k: json.loads(_text(f["conditions"][k]) or "{}") for k in ("start", "end")
                         if "conditions" in f and k in f["conditions"]}
        rec.analysis = json.loads(_text(f.get("analysis")) or "null")
        if not with_rows:
            return rec
        n = int(f["points"]["i"].shape[0]) if "i" in f.get("points", {}) else int(rec.meta.get("points") or 0)
        rows: List[Dict[str, Any]] = [{} for _ in range(n)]
        for k, ds in f.get("points", {}).items():
            vals = ds[()]
            if ds.dtype.kind in ("O", "S", "U"):
                for i in range(n):
                    v = vals[i]
                    rows[i][k] = v.decode("utf-8") if isinstance(v, bytes) else str(v)
            else:
                as_bool = ds.attrs.get("type") == "bool"
                for i in range(n):
                    v = float(vals[i])
                    rows[i][k] = None if math.isnan(v) else (bool(v) if as_bool else
                                                             (int(v) if k == "i" else v))
        traces = f.get("traces", {})
        for k, ds in traces.items():
            if k.endswith(".length"):
                continue
            data = ds[()]
            lengths = traces[k + ".length"][()] if k + ".length" in traces else [data.shape[1]] * n
            for i in range(min(n, data.shape[0])):
                rows[i][k] = [None if math.isnan(x) else float(x) for x in data[i, :int(lengths[i])]]
        for k in f.get("frames", {}):
            for i in range(n):
                rows[i][k] = "%s:/frames/%s[%d]" % (RUN_FILE, k, i)
        rec.rows = rows
    return rec


def _text(ds: Any) -> str:
    if ds is None:
        return ""
    v = ds[()]
    return v.decode("utf-8") if isinstance(v, bytes) else str(v)


# ------------------------------------------------------------------ finishing a run

def finish(folder: Path) -> Optional[Path]:
    """Keep a run that has ended as run.h5: write it from the record, check it, then remove the record and the
    frames packed into it. A run a crash or a power cut ended is kept the same way, with the status interrupted
    (Kelvin does this at its next start). Returns run.h5's path, or None without h5py: the run then keeps its record
    (closed as interrupted, if it was), which Kelvin reads instead."""
    folder = Path(folder)
    if not (folder / RECORD).exists():
        return None
    rec = read_record(folder)
    if not rec.finished:   # Kelvin ended while this run was measured
        last = datetime.fromtimestamp((folder / RECORD).stat().st_mtime)
        rec.meta.update(status=INTERRUPTED, points=len(rec.rows), reason=CRASH_REASON,
                        finished=last.isoformat(timespec="milliseconds"))
        if rec.rows and isinstance(rec.rows[-1].get("t"), (int, float)):
            rec.meta["duration_s"] = round(float(rec.rows[-1]["t"]), 3)
        if not have_h5():   # closed as interrupted, after its last whole frame
            with open(folder / RECORD, "r+b") as f:
                f.truncate(rec.whole)
                f.seek(0, 2)
                f.write(_frame("finish", {"meta": rec.meta}))
                f.flush()
                os.fsync(f.fileno())
            return None
        rec.finished = True
    if not have_h5():
        return None
    path = write_h5(folder, rec)
    back = read_h5(folder, with_rows=False)
    if back.meta.get("run_id") != rec.meta.get("run_id"):
        raise ValueError("run.h5 did not read back as written")
    (folder / RECORD).unlink()
    shutil.rmtree(folder / "frames", ignore_errors=True)
    try:
        (folder / LOCK).unlink()   # left by a crash (a Kelvin recording it deletes its own)
    except OSError:
        pass
    return path


def upgrade(folder: Path, columns: Optional[Dict[str, Any]] = None) -> Optional[Path]:
    """A run folder from before run.h5 (data.jsonl, meta.json, plan.yaml...) as run.h5. The older files are moved
    into legacy/ inside the run folder, not deleted. columns: its axes and readings with units, when the lab still
    knows them (else from the plan, without units). None when there is nothing to upgrade."""
    folder = Path(folder)
    if (folder / RUN_FILE).exists() or not (folder / "data.jsonl").exists() or not have_h5():
        return None
    rec = Recorded(finished=True)
    rec.plan_text = (folder / "plan.yaml").read_text(encoding="utf-8") if (folder / "plan.yaml").exists() else ""
    rec.meta = _read_json(folder / "meta.json") or {}
    if not rec.meta:   # Kelvin stopped while measuring it (its meta was only written when a run ended)
        rec.meta = {"status": INTERRUPTED, "reason": CRASH_REASON, "started": _started(folder.name),
                    "plan": _plan_name(rec.plan_text) or folder.name.split("-", 2)[-1]}
    rec.meta.setdefault("run_id", folder.name)
    rec.analysis = _read_json(folder / "analysis.json")
    rec.snapshots = {k: _read_json(folder / ("snapshot_%s.json" % k)) or {} for k in ("start", "end")}
    rec.rows = _jsonl_rows(folder / "data.jsonl")
    rec.columns = columns or _guess_columns(rec)
    write_h5(folder, rec)
    if len(read_h5(folder).rows) != len(rec.rows):
        (folder / RUN_FILE).unlink()
        raise ValueError("run.h5 did not read back with every point")
    legacy = folder / "legacy"
    legacy.mkdir(exist_ok=True)
    for name in LEGACY + ("frames",):
        p = folder / name
        if p.exists():
            os.replace(str(p), str(legacy / name))
    for p in folder.glob("*.csv"):   # each trace's CSV
        os.replace(str(p), str(legacy / p.name))
    return folder / RUN_FILE


def _started(run_id: str) -> Optional[str]:
    """When a run started, from its name (YYYYMMDD-HHMMSS-plan)."""
    try:
        return datetime.strptime(run_id[:15], "%Y%m%d-%H%M%S").isoformat(timespec="milliseconds")
    except ValueError:
        return None


def _plan_name(plan_text: str) -> Optional[str]:
    try:
        import yaml
        return (yaml.safe_load(plan_text) or {}).get("plan")
    except Exception:
        return None


def _guess_columns(rec: Recorded) -> Dict[str, Any]:
    """Axes and readings of an older run, from its plan (no lab needed; units as far as the plan knows none)."""
    axes, measures = [], []
    try:
        import yaml
        plan = yaml.safe_load(rec.plan_text) or {}
        sweep = plan.get("sweep") or {}
        for key in (sweep if isinstance(sweep, dict) else {}):
            axes.append({"key": str(key), "unit": "", "kind": "instrument"})
        for key in plan.get("measure") or []:
            measures.append({"key": str(key), "unit": "", "kind": "scalar"})
    except Exception:
        pass
    return {"axes": axes, "measures": measures}


def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _jsonl_rows(path: Path) -> List[Dict[str, Any]]:
    """An older run's points (data.jsonl), up to a last line cut short by a crash."""
    rows: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    break   # the last line of a crashed run
    return rows


# ------------------------------------------------------------------ reading a run, whatever it is kept as

def is_run(folder: Path) -> bool:
    folder = Path(folder)
    return (folder / RUN_FILE).exists() or (folder / RECORD).exists() or (folder / "meta.json").exists() or \
        (folder / "data.jsonl").exists()


def load(folder: Path, with_rows: bool = True) -> Recorded:
    """A run as Kelvin keeps it: from run.h5, else its record (still measuring, or no h5py), else older files."""
    folder = Path(folder)
    if (folder / RUN_FILE).exists() and have_h5():
        return read_h5(folder, with_rows)
    if (folder / RECORD).exists():
        rec = read_record(folder)
        if not rec.finished and not rec.meta.get("status"):   # measuring now, or ended by a crash
            rec.meta["status"] = "running" if being_recorded(folder) else INTERRUPTED
        return rec
    if (folder / "meta.json").exists() or (folder / "data.jsonl").exists():   # before run.h5
        rec = Recorded(finished=(folder / "meta.json").exists())
        rec.meta = _read_json(folder / "meta.json") or {}
        rec.plan_text = (folder / "plan.yaml").read_text(encoding="utf-8") if (folder / "plan.yaml").exists() else ""
        rec.analysis = _read_json(folder / "analysis.json")
        rec.snapshots = {k: _read_json(folder / ("snapshot_%s.json" % k)) or {} for k in ("start", "end")}
        if with_rows and (folder / "data.jsonl").exists():
            rec.rows = _jsonl_rows(folder / "data.jsonl")
        rec.columns = _guess_columns(rec)
        return rec
    raise KeyError("no run in %s" % folder.name)


class Columns:
    """A run's columns, for reading numbers out without its traces: each scalar column as a numpy array (text
    columns as text), and each trace's length (or a camera frame) at each point."""

    def __init__(self, n: int, numbers: Dict[str, Any], texts: Dict[str, List[str]], sizes: Dict[str, List[int]],
                 kinds: Dict[str, str], order: List[str]):
        self.n, self.numbers, self.texts, self.sizes, self.kinds, self.order = n, numbers, texts, sizes, kinds, order

    def number(self, key: str) -> Any:
        """A column of numbers (numpy, NaN where a point has none); KeyError, saying why, for anything else."""
        if key in self.numbers:
            return self.numbers[key]
        if key in self.sizes:
            raise KeyError("%s is a %s, not a column of numbers (get_trace reads one trace)" % (key, self.kinds[key]))
        if key in self.texts:
            raise KeyError("%s holds text, not numbers" % key)
        raise KeyError("no column %r (have: %s)" % (key, ", ".join(self.order)))

    def cell(self, key: str, i: int) -> str:
        if key in self.numbers:
            v = float(self.numbers[key][i])
            return "" if v != v else str(int(v)) if key == "i" else "%.6g" % v
        if key in self.sizes:
            return "trace[%d]" % self.sizes[key][i] if self.kinds[key] == "trace" else "frame"
        return self.texts[key][i] if key in self.texts else ""


def columns(folder: Path) -> Columns:
    """A run's columns (Columns): from run.h5 without reading its traces, else from its rows."""
    import numpy as np
    folder = Path(folder)
    numbers: Dict[str, Any] = {}
    texts: Dict[str, List[str]] = {}
    sizes: Dict[str, List[int]] = {}
    kinds: Dict[str, str] = {}
    if (folder / RUN_FILE).exists() and have_h5():
        import h5py
        with h5py.File(folder / RUN_FILE, "r") as f:
            cols = json.loads(f.attrs.get("columns_json", "{}"))
            for k, ds in f.get("points", {}).items():
                if ds.dtype.kind in "fiub":
                    numbers[k] = ds[()].astype(float)
                else:
                    texts[k] = [x.decode("utf-8") if isinstance(x, bytes) else str(x) for x in ds[()]]
            traces = f.get("traces", {})
            for k, ds in traces.items():
                if k.endswith((".length", ".x", ".axis")) and k.rsplit(".", 1)[0] in traces:
                    continue
                width = int(ds.shape[1]) if ds.ndim > 1 else 0
                lengths = traces[k + ".length"][()] if k + ".length" in traces else [width] * int(ds.shape[0])
                sizes[k] = [int(x) for x in lengths]
                kinds[k] = "trace"
            for k, item in f.get("frames", {}).items():
                sizes[k], kinds[k] = [1] * (int(item.shape[0]) if hasattr(item, "shape") else len(item)), "frame"
        n = len(numbers["i"]) if "i" in numbers else max([len(v) for v in list(numbers.values()) +
                                                           list(texts.values()) + list(sizes.values())] or [0])
    else:
        rec = load(folder)
        rows, cols, n = rec.rows, rec.columns, len(rec.rows)
        keys = list(dict.fromkeys(k for r in rows for k in r))
        for k in keys:
            vals = [r.get(k) for r in rows]
            if k.endswith((".x", ".axis")) and k.rsplit(".", 1)[0] in keys:
                continue
            if any(isinstance(v, list) for v in vals):
                sizes[k], kinds[k] = [len(v) if isinstance(v, list) else 0 for v in vals], "trace"
            elif any(isinstance(v, str) and v.startswith("frames/") for v in vals):
                sizes[k], kinds[k] = [1] * n, "frame"
            elif all(v is None or isinstance(v, (int, float)) for v in vals):
                numbers[k] = np.array([np.nan if v is None else float(v) for v in vals])
            else:
                texts[k] = ["" if v is None else str(v) for v in vals]
    known = ["i", "t"] + [a["key"] for a in cols.get("axes") or []] + [m["key"] for m in cols.get("measures") or []]
    present = list(numbers) + list(texts) + list(sizes)
    order = [k for k in dict.fromkeys(known + present) if k in present]
    return Columns(n, numbers, texts, sizes, kinds, order)


def trace_row(folder: Path, key: str, i: int) -> Optional[Dict[str, Any]]:
    """One point's trace with its x values or axis, as a row ({key: values, key.x or key.axis: ...}), read from
    run.h5 alone; None when the run is not kept as run.h5. KeyError: no such trace; IndexError(points): no such
    point."""
    folder = Path(folder)
    if not (folder / RUN_FILE).exists() or not have_h5():
        return None
    import h5py
    with h5py.File(folder / RUN_FILE, "r") as f:
        traces = f.get("traces", {})
        if key not in traces or key.endswith(".length"):
            raise KeyError(key)
        ds = traces[key]
        if not 0 <= i < ds.shape[0]:
            raise IndexError(int(ds.shape[0]))
        row: Dict[str, Any] = {}
        for k in (key, key + ".x", key + ".axis"):
            if k in traces:
                width = int(traces[k + ".length"][i]) if k + ".length" in traces else traces[k].shape[1]
                row[k] = [None if math.isnan(x) else float(x) for x in traces[k][i, :width]]
        return row


def frame(folder: Path, ref: str) -> Any:
    """One camera frame of a run, as a NumPy array, by the reference its row keeps: frames/<file>.npy while the run
    is measured, run.h5:/frames/<key>[i] once it is packed. ValueError: not a frame of this run."""
    import numpy as np
    folder = Path(folder)
    head, sep, rest = str(ref).partition(":/frames/")
    if sep and head == RUN_FILE and rest.endswith("]") and "[" in rest:
        key, _, i = rest[:-1].rpartition("[")
        import h5py
        with h5py.File(folder / RUN_FILE, "r") as f:
            item = f["frames"][key]
            if isinstance(item, h5py.Group):   # frames of different sizes: one dataset per point
                return item["%05d" % int(i)][()]
            return item[int(i)]
    p = (folder / str(ref)).resolve()
    if p.suffix.lower() != ".npy" or folder.resolve() not in p.parents:
        raise ValueError("not a frame of this run: %s" % ref)
    return np.load(str(p), allow_pickle=False)


def meta(folder: Path) -> Dict[str, Any]:
    """A run's meta (status, points, times, project, device...), without reading its points."""
    return load(folder, with_rows=False).meta


def card(folder: Path) -> Dict[str, Any]:
    folder = Path(folder)
    if (folder / RUN_FILE).exists() and have_h5():
        import h5py
        with h5py.File(folder / RUN_FILE, "r") as f:
            if "card" in f:
                return json.loads(_text(f["card"]) or "{}")
    return make_card(load(folder))


def set_analysis(folder: Path, analysis: Dict[str, Any]) -> None:
    """Keep a run's analysis (and its card) up to date after a re-analysis."""
    folder = Path(folder)
    if (folder / RUN_FILE).exists() and have_h5():
        import h5py
        rec = read_h5(folder)
        rec.analysis = analysis
        text = json.dumps(_jsonable(make_card(rec)), ensure_ascii=False)
        with h5py.File(folder / RUN_FILE, "r+") as f:
            for name, value in (("analysis", json.dumps(_jsonable(analysis), ensure_ascii=False)), ("card", text)):
                if name in f:
                    del f[name]
                f.create_dataset(name, data=value, dtype=h5py.string_dtype())
    elif (folder / RECORD).exists():
        r = Recorder(folder)
        try:
            r.write("analysis", {"analysis": analysis})
        finally:
            r.close()
