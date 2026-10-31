"""The lab notebook's raw record: an append-only event log and per-run folders (each run's data: rundata.py)."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

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
    instruments is also written, with user: the signed-in person (None: nobody, such as setting up on start).
    whose: the marks of unshared work the writing thread does (LabRuntime._whose_now), kept with each event it writes,
    so that other chats and phones through the Kelvin hub never see it."""

    def __init__(self, path: Any = None, clock: Any = None, shared: Any = None, user: Optional[str] = None,
                 whose: Optional[Callable[[], Optional[Dict[str, Any]]]] = None):
        self.path = Path(path) if path else None
        self.clock = clock
        self.shared = Path(shared) if shared else None
        self.user = user
        self.whose = whose
        self._memory: List[Dict[str, Any]] = []
        self._seq = 0
        self._lock = threading.Lock()  # agent, plans and people write from different threads; keep every line
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                with open(self.path, "rb") as f:
                    self._seq = sum(1 for _ in f)

    def write(self, type_: str, **fields: Any) -> Dict[str, Any]:
        try:
            whose = self.whose() if self.whose is not None else None
        except Exception:   # the record never loses an event to its marks
            whose = None
        with self._lock:
            self._seq += 1
            event: Dict[str, Any] = {"seq": self._seq, "wall": _now(), "type": type_}
            if self.clock is not None:
                event["t"] = round(self.clock.now(), 6)
            for k, v in fields.items():
                if v is not None:
                    event[k] = _jsonable(v)
            if whose and "whose" not in event:
                event["whose"] = _jsonable(whose)
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
NAME_MAX = 40      # characters of the plan's name in a run's folder name; a longer name is cut and hashed
PATH_MAX = 259     # Windows' longest path (MAX_PATH less its terminating NUL), unless long paths are switched on
INSIDE_MAX = 48    # the longest name Kelvin writes inside a run's folder: frames/00001_<reading>.npy, report.pptx


class RunFolderError(OSError):
    """A run's folder couldn't be made, so the run did not start (nothing was set); the message says why in words."""


def run_name(plan_name: str, most: int = NAME_MAX) -> str:
    """The plan's part of a run's folder name: letters, digits and . _ - only, at most `most` characters. A longer
    one keeps its start and a short hash of the whole name, so two long names stay apart (a 70-character plan title
    once made a folder Windows could not create: l09, l10)."""
    safe = re.sub(r"[^\w.-]+", "_", plan_name)
    if len(safe) <= most:
        return safe
    digest = hashlib.sha1(plan_name.encode("utf-8")).hexdigest()[:6]
    return (safe[:max(0, most - 7)].rstrip("._-") + "-" + digest).lstrip("-")


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

    def _run_id(self, root: Optional[Path], plan_name: str) -> str:
        """date-time-plan, the plan's part short enough that the run's folder and every file Kelvin keeps in it fit
        in a Windows path (a deep data folder leaves less room: the plan's part shrinks to a hash, then goes)."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        most = NAME_MAX
        if root is not None and os.name == "nt":
            room = PATH_MAX - len(str(root)) - len(stamp) - 4 - INSIDE_MAX   # "\", "-", and "-2" for a taken name
            most = min(most, max(0, room))
        name = run_name(plan_name, most) if most >= 7 else ""
        base = "%s-%s" % (stamp, name)
        run_id, n = base, 1
        while root and ((root / run_id).exists() or (self.folders is not None and self.folders.find(run_id))):
            n += 1
            run_id = "%s-%d" % (base, n)
        return run_id

    def create(self, plan_name: str, plan: Dict[str, Any], operator: str, folder: Any = None) -> RunRecord:
        root = Path(folder) if folder and self.root else self.root
        run_id = self._run_id(root, plan_name)
        if root is None or root == self.root:
            return self._record(run_id, root / run_id if root else None, plan, operator)
        try:
            rec = RunRecord(run_id, root / run_id, plan, operator)
        except OSError as e:   # the chat's folder is away (a drive, a server): the run still keeps its data
            run_id = self._run_id(self.root, plan_name)   # the data folder may be deeper than the chat's
            rec = self._record(run_id, self.root / run_id, plan, operator)
            rec.moved = "the chat's folder %s could not be written (%s), so this run is in %s" % (
                root, e.strerror or e, rec.path)
            return rec
        if self.folders is not None:
            self.folders.remember(root)
        return rec

    @staticmethod
    def _record(run_id: str, path: Optional[Path], plan: Dict[str, Any], operator: str) -> RunRecord:
        try:
            return RunRecord(run_id, path, plan, operator)
        except OSError as e:   # nowhere to keep the run: it doesn't start, and says why (never a bare "tool error")
            too_long = os.name == "nt" and path is not None and len(str(path)) + INSIDE_MAX > PATH_MAX
            raise RunFolderError("Kelvin could not make the run's folder %s (%s)%s, so the run did not start and "
                                 "nothing was set" % (path, e.strerror or e, (
                                     ": the data folder's path is too long for Windows; choose a shorter one in "
                                     "Settings, Data folder") if too_long else "")) from e
