"""What came of runs afterwards, kept with where it came from (phase 4 of the ELN plan; asked for 2026-11-07: "Kelvin's
data saving protocol and experimental procedure recording should follow the practice of modern ELN/LIMS system, and
be ai-native").

A figure Kelvin saves (plot_runs) is kept with a record beside it, figures/<name>.json: the runs it was drawn from
(each by its uuid and its run.h5's seal), exactly how it was asked for (the spec), who asked and which AI, and the
versions it was made with. The PNG carries the record's id (a tEXt chunk, kelvin-figure), so a figure found anywhere
leads back to its record. rebuild draws it again from the raw data, after checking that every run is still as it was
sealed. A fit kept (fitting.store) carries the same: Kelvin's and NumPy's versions and the AI that kept it."""
from __future__ import annotations

import json
import struct
import uuid
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import rundata
from ._version import __version__

PNG_KEY = b"kelvin-figure"


def versions() -> Dict[str, str]:
    out = {"kelvin": __version__}
    try:
        import numpy
        out["numpy"] = numpy.__version__
    except ImportError:
        pass
    return out


def source(rt: Any, run_id: str) -> Dict[str, Any]:
    """One run a figure or a fit came from: its id, uuid and seal (None for a run not sealed)."""
    d = rt._run_dir(run_id)
    try:
        uid = rundata.meta(d).get("uuid")
    except Exception:   # an older run: its folder names it
        uid = None
    return {"run": d.name, "uuid": uid, "sha256": rundata.sealed(d)}


def tag_png(png: bytes, text: str) -> bytes:
    """The PNG with a tEXt chunk (kelvin-figure: text) before its end."""
    if not png or png[:8] != b"\x89PNG\r\n\x1a\n" or len(png) < 20:
        return png
    data = PNG_KEY + b"\0" + text.encode("latin-1", "replace")
    chunk = struct.pack(">I", len(data)) + b"tEXt" + data + struct.pack(">I", zlib.crc32(b"tEXt" + data) & 0xffffffff)
    end = png.rfind(b"IEND") - 4
    return png[:end] + chunk + png[end:] if end > 8 else png


def png_tag(png: bytes) -> Optional[str]:
    """The kelvin-figure text a PNG carries, or None."""
    i = 8
    while i + 8 <= len(png):
        n = struct.unpack(">I", png[i:i + 4])[0]
        kind = png[i + 4:i + 8]
        if kind == b"tEXt":
            body = png[i + 8:i + 8 + n]
            key, _, value = body.partition(b"\0")
            if key == PNG_KEY:
                return value.decode("latin-1")
        i += 12 + n
    return None


def keep_figure(folder: Path, base: str, spec: Dict[str, Any], sources: List[Dict[str, Any]], files: List[str],
                by: str, account: Optional[str], ai: Optional[Dict[str, Any]], fid: str = "") -> Dict[str, Any]:
    """The figure's record, figures/<base>.json."""
    record: Dict[str, Any] = {"figure": base, "id": fid or str(uuid.uuid4()),
                              "made": datetime.now().isoformat(timespec="seconds"),
                              "by": by, "runs": sources, "spec": spec, "files": files, "versions": versions()}
    if account:
        record["account"] = account
    if ai:
        record["ai"] = ai
    (Path(folder) / (base + ".json")).write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return record


def find_figure(folder: Path, name: str) -> Tuple[Path, Dict[str, Any]]:
    """A figure's record by its name (with or without .png) or its id. KeyError: none."""
    folder = Path(folder)
    stem = str(name or "").strip()
    for ext in (".png", ".svg", ".pdf", ".json"):
        if stem.endswith(ext):
            stem = stem[:-len(ext)]
    p = folder / (stem + ".json")
    if p.exists():
        return p, json.loads(p.read_text(encoding="utf-8"))
    for p in sorted(folder.glob("*.json")):
        try:
            record = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("id") == stem:
            return p, record
    raise KeyError("no figure %r kept with a record (figures Kelvin saved since records began)" % name)


def check_sources(rt: Any, sources: List[Dict[str, Any]]) -> List[str]:
    """What stands against rebuilding from these runs: a run gone, changed since it was sealed, or not the run.h5 the
    figure was drawn from."""
    problems = []
    for s in sources:
        try:
            d = rt._run_dir(s["run"])
        except KeyError as e:
            problems.append("run %s: %s" % (s["run"], e.args[0] if e.args else "not here"))
            continue
        state, now = rundata.check_seal(d)
        if state == "changed":
            problems.append("run %s changed since it was sealed" % s["run"])
        elif s.get("sha256") and state == "intact" and now != s["sha256"]:
            problems.append("run %s is not the run.h5 the figure was drawn from" % s["run"])
    return problems
