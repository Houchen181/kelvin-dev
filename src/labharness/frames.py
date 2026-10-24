"""Camera frames: an image reading is kept as a file, never inline in a run's data.

A driver that snaps a frame saves it here (NumPy's .npy: any analysis program reads it) and answers with its path.
A run moves each frame into its own folder (runs/<id>/frames/) and records the file's name in data.csv; a frame read
outside a run stays in ~/Kelvin/frames (or the system's temporary folder until a person signs in)."""
from __future__ import annotations

import os
import struct
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

FOLDER: Optional[Path] = None   # where frames read outside a run go; the runtime points it at the data folder
_lock = threading.Lock()
_n = [0]


def folder() -> Path:
    f = FOLDER or Path(tempfile.gettempdir()) / "kelvin-frames"
    f.mkdir(parents=True, exist_ok=True)
    return f


def save(frame: Any, name: str = "frame") -> str:
    """Write one frame (a 2-D NumPy array, or rows of numbers) as .npy and return its path."""
    with _lock:
        _n[0] += 1
        n = _n[0]
    path = folder() / ("%s-%s-%04d.npy" % (time.strftime("%Y%m%d-%H%M%S"), "".join(
        c if c.isalnum() or c in "._-" else "_" for c in name)[:40], n % 10000))
    if hasattr(frame, "dtype") and hasattr(frame, "shape"):
        import numpy as np   # a camera's library brings NumPy with it
        np.save(str(path), frame, allow_pickle=False)
    else:
        _save_rows(path, [list(r) for r in frame])
    return str(path)


def _save_rows(path: Path, rows: Sequence[Sequence[float]]) -> None:
    """.npy without NumPy (simulated cameras): float64, C order."""
    h, w = len(rows), len(rows[0]) if rows else 0
    header = "{'descr': '<f8', 'fortran_order': False, 'shape': (%d, %d), }" % (h, w)
    header += " " * ((64 - (10 + len(header) + 1) % 64) % 64) + "\n"
    with open(path, "wb") as f:
        f.write(b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header)) + header.encode("latin1"))
        for r in rows:
            f.write(struct.pack("<%dd" % w, *[float(x) for x in r]))


def load_rows(path: Any) -> List[List[float]]:
    """A float64 .npy written by _save_rows, as rows (tests; real frames are read with NumPy)."""
    data = Path(path).read_bytes()
    n = struct.unpack("<H", data[8:10])[0]
    header = data[10:10 + n].decode("latin1")
    shape = header.split("'shape': (")[1].split(")")[0]
    h, w = [int(x) for x in shape.split(",") if x.strip()]
    vals = struct.unpack("<%dd" % (h * w), data[10 + n:10 + n + 8 * h * w])
    return [list(vals[i * w:(i + 1) * w]) for i in range(h)]


def stats(frame: Any) -> Dict[str, float]:
    """Mean, max, min and sum of a frame (NumPy array or rows)."""
    if hasattr(frame, "dtype"):
        return {"mean": float(frame.mean()), "max": float(frame.max()), "min": float(frame.min()),
                "sum": float(frame.sum(dtype="float64"))}
    flat = [float(x) for r in frame for x in r]
    return {"mean": sum(flat) / len(flat) if flat else 0.0, "max": max(flat, default=0.0),
            "min": min(flat, default=0.0), "sum": sum(flat)}


def keep(src: Any, run_dir: Path, key: str, point: int) -> str:
    """Move a frame into a run's frames/ folder; returns its path relative to the run folder."""
    src = Path(str(src))
    dest_dir = run_dir / "frames"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / ("%05d_%s.npy" % (point, "".join(c if c.isalnum() or c in "._-" else "_" for c in key)))
    try:
        os.replace(src, dest)
    except OSError:   # another drive: copy, then remove
        dest.write_bytes(src.read_bytes())
        try:
            src.unlink()
        except OSError:
            pass
    return "frames/" + dest.name
