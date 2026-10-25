"""The lab notebook's raw record: an append-only event log and per-run folders (each run's data: rundata.py)."""
from __future__ import annotations

import json
import math
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import yaml

from ._version import __version__
from . import rundata


def _jsonable(v: Any) -> Any:
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (set, frozenset)):
        return sorted(_jsonable(x) for x in v)
    if isinstance(v, tuple):
        return [_jsonable(x) for x in v]
    try:
        json.dumps(v)
        return v
    except TypeError:
        return str(v)


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


_SHARED = threading.Lock()


class EventLog:
    """One JSON object per line; never rewritten. path=None keeps it in memory.

    shared: the lab's instrument log (~/Kelvin/instrument-log.jsonl), where every event of everyone who uses these
    instruments is also written, with user: the signed-in person (None: nobody, such as setting up on start)."""

    def __init__(self, path: Any = None, clock: Any = None, shared: Any = None, user: Optional[str] = None):
        self.path = Path(path) if path else None
        self.clock = clock
        self.shared = Path(shared) if shared else None
        self.user = user
        self._memory: List[Dict[str, Any]] = []
        self._seq = 0
        self._lock = threading.Lock()  # agent, plans and people write from different threads; keep every line
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                with open(self.path, "rb") as f:
                    self._seq = sum(1 for _ in f)

    def write(self, type_: str, **fields: Any) -> Dict[str, Any]:
        with self._lock:
            self._seq += 1
            event: Dict[str, Any] = {"seq": self._seq, "wall": _now(), "type": type_}
            if self.clock is not None:
                event["t"] = round(self.clock.now(), 6)
            for k, v in fields.items():
                if v is not None:
                    event[k] = _jsonable(v)
            if self.path:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
            else:
                self._memory.append(event)
        if self.shared:
            line = json.dumps(dict(event, user=self.user), ensure_ascii=False) + "\n"
            try:
                with _SHARED:
                    self.shared.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.shared, "a", encoding="utf-8") as f:
                        f.write(line)
            except OSError:   # the person's own record has it; the lab's log is a copy
                pass
        return event

    def events(self) -> List[Dict[str, Any]]:
        if not self.path:
            return list(self._memory)
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


class RunRecord:
    """One run, recorded point by point in its folder's record.bin while it is measured (rundata.py: flushed at
    once, synced to disk about once a second), and kept as run.h5 when it ends."""

    def __init__(self, run_id: str, path: Optional[Path], plan: Dict[str, Any], operator: str):
        self.run_id = run_id
        self.path = path
        self.rows: List[Dict[str, Any]] = []
        self.snapshots: Dict[str, Dict[str, Any]] = {}
        self.meta: Dict[str, Any] = {"run_id": run_id, "plan": plan.get("plan"), "operator": operator,
                                     "started": _now(), "labharness": __version__}
        self._plan = plan
        self._record: Optional[rundata.Recorder] = None
        if path:
            path.mkdir(parents=True)
            self._record = rundata.Recorder(path)

    def _write(self, kind: str, fields: Optional[Dict[str, Any]] = None, values: Optional[Dict[str, Any]] = None,
               sync: bool = False) -> None:
        if self._record is None:
            return
        if not getattr(self, "_started", False):   # the plan and who/where first, with the meta as it is by then
            self._started = True
            self._record.write("start", {"plan": yaml.safe_dump(self._plan, sort_keys=False, allow_unicode=True),
                                         "meta": dict(self.meta)}, sync=True)
        self._record.write(kind, fields, values, sync=sync)

    def set_columns(self, columns: Dict[str, Any]) -> None:
        """The run's axes and readings with their units (engine.columns)."""
        self._write("columns", {"columns": columns})

    def note(self, **fields: Any) -> None:
        """More about the run as it goes (such as the conditions held)."""
        self.meta.update({k: _jsonable(v) for k, v in fields.items()})
        self._write("meta", {"meta": fields})

    def snapshot(self, kind: str, data: Dict[str, Any]) -> None:
        self.meta["snapshot_" + kind] = True
        self.snapshots[kind] = data
        self._write("snapshot", {"kind": kind, "data": data})

    def add(self, row: Dict[str, Any]) -> None:
        self.rows.append(row)
        self._write("row", values=row)

    def set_analysis(self, analysis: Dict[str, Any]) -> None:
        self._write("analysis", {"analysis": analysis})

    def finish(self, **meta: Any) -> None:
        """The run ended (done, stopped or failed): its record is closed and kept as run.h5. If that can't be
        written now (no h5py, a full disk), the record stays, and Kelvin finishes it at its next start."""
        self.meta.update({k: _jsonable(v) for k, v in meta.items()})
        self.meta["finished"] = _now()
        if self._record is None:
            return
        self._write("finish", {"meta": dict(self.meta)}, sync=True)
        self._record.close()
        try:
            rundata.finish(self.path)
        except Exception as e:   # the record keeps every point; it becomes run.h5 at the next start
            self.meta.setdefault("warnings", []).append("run.h5 not written yet: %s" % e)
        finally:
            self._record.release()


RUN_NAME = re.compile(r"^\d{8}-\d{6}-[\w.-]*$")   # a run's folder is its id, as RunStore names it


class RunFolders:
    """Where a person's runs are: the data folder's runs/, and each folder a chat saved runs in (the data folder's
    folders.json lists them). It reads like runs/ alone did: folders / run_id is that run's folder wherever it is,
    and iterdir() gives every run's folder (in a chat's folder only Kelvin's runs: the person keeps other files
    there too). A folder that can't be read now (a drive that is away) is skipped until it is back."""

    INDEX = "folders.json"

    def __init__(self, data_dir: Any):
        self.home = Path(data_dir) / "runs"
        self.index = Path(data_dir) / self.INDEX

    def chat_folders(self) -> List[Path]:
        try:
            listed = json.loads(self.index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [Path(p) for p in listed if isinstance(p, str)] if isinstance(listed, list) else []

    def roots(self) -> List[Path]:
        out = [self.home]
        for p in self.chat_folders():
            if p not in out:
                out.append(p)
        return out

    def remember(self, folder: Any) -> None:
        """Note a chat's folder once it holds a run, so Kelvin finds its runs from then on."""
        folder, listed = Path(folder), self.chat_folders()
        if folder == self.home or folder in listed:
            return
        self.index.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.index.with_suffix(".tmp")
        tmp.write_text(json.dumps([str(p) for p in listed + [folder]], indent=1), encoding="utf-8")
        tmp.replace(self.index)

    def find(self, run_id: str) -> Optional[Path]:
        """The run's folder, or None (an id is a name, never a path)."""
        if not run_id or run_id in (".", "..") or "/" in run_id or "\\" in run_id or ":" in run_id:
            return None
        for root in self.roots():
            d = root / run_id
            try:
                if d.is_dir() and (root == self.home or rundata.is_run(d)):
                    return d
            except OSError:
                continue
        return None

    def __truediv__(self, run_id: str) -> Path:
        return self.find(run_id) or self.home / run_id

    def iterdir(self) -> Iterator[Path]:
        seen = set()
        for root in self.roots():
            try:
                found = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name)
            except OSError:
                continue
            for d in found:
                if d.name in seen or (root != self.home and not (RUN_NAME.match(d.name) and rundata.is_run(d))):
                    continue
                seen.add(d.name)
                yield d

    def exists(self) -> bool:
        return any(root.is_dir() for root in self.roots())


class RunStore:
    """Makes each run's folder, named by its id: in root (the data folder's runs/), or in the folder the run is
    given (its chat's). folders: where runs are kept, so an id is never used twice."""

    def __init__(self, root: Any = None, folders: Optional[RunFolders] = None):
        self.root = Path(root) if root else None
        self.folders = folders

    def create(self, plan_name: str, plan: Dict[str, Any], operator: str, folder: Any = None) -> RunRecord:
        root = Path(folder) if folder and self.root else self.root
        base = "%s-%s" % (datetime.now().strftime("%Y%m%d-%H%M%S"), re.sub(r"[^\w.-]+", "_", plan_name))
        run_id, n = base, 1
        while root and ((root / run_id).exists() or (self.folders is not None and self.folders.find(run_id))):
            n += 1
            run_id = "%s-%d" % (base, n)
        if root is None or root == self.root:
            return RunRecord(run_id, root / run_id if root else None, plan, operator)
        try:
            rec = RunRecord(run_id, root / run_id, plan, operator)
        except OSError as e:   # the chat's folder is away (a drive, a server): the run still keeps its data
            rec = RunRecord(run_id, self.root / run_id, plan, operator)
            rec.moved = "the chat's folder %s could not be written (%s), so this run is in %s" % (
                root, e.strerror or e, rec.path)
            return rec
        if self.folders is not None:
            self.folders.remember(root)
        return rec
