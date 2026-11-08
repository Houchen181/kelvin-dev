"""The lab notebook's raw record: an append-only event log and per-run folders (each run's data: rundata.py).

The record is chained: each line keeps the SHA-256 of the line before it (prev; the first line of a file follows
GENESIS), so a line changed or taken out afterwards breaks the chain where it was, and Verify (integrity.py) finds it.
Lines an older Kelvin wrote keep no prev; the first chained line follows the last of them. While a run measures, the
record's files stay open (EventLog.holding), each line still handed to the operating system as it is written, as
closing the file did: a set in a plan costs some 15 µs of record instead of half a millisecond of opening and closing
two files."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, Iterator, List, Optional, Tuple

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


def plan_text(plan: Dict[str, Any]) -> str:
    """A run's plan as its record keeps it (run.h5 /plan): the same plan always gives the same text."""
    return yaml.safe_dump(plan, sort_keys=False, allow_unicode=True)


_SHARED = threading.Lock()
_SHARED_SEQ: Dict[str, int] = {}   # the lab's instrument log numbers its own lines: by file, counted once
_SHARED_TAIL: Dict[str, List[Any]] = {}   # and chains them: [its last line's hash, its size] as this Kelvin left it
GENESIS = "0" * 64                 # what the first line of a chained record follows
_TAILS: Dict[str, List[Any]] = {}  # other chained records (append_chained): [last line's hash, size], by file
_TAILS_LOCK = threading.Lock()
RETRY_S = 30.0          # while the record can't be written, how often (seconds) Kelvin tries again
KEEP_BACK = 50000       # lines kept back meanwhile, at most (the oldest go first)


def line_hash(line: bytes) -> str:
    """A record line's SHA-256 (without its line end): what the next line keeps as prev."""
    return hashlib.sha256(line.rstrip(b"\r\n")).hexdigest()


def _tail(path: Path) -> Tuple[int, Optional[str], int]:
    """(lines, its last line's hash or None, size in bytes) of a record file; (0, None, 0) without one."""
    n, last, size = 0, None, 0
    try:
        with open(path, "rb") as f:
            for line in f:
                n += 1
                if line.strip():
                    last = line
            size = f.tell()
    except OSError:
        return 0, None, 0
    return n, (line_hash(last) if last is not None else None), size


def _last_hash(path: Path) -> Optional[str]:
    """The hash of a record file's last line, read from its end (someone else wrote to it since)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            back = min(size, 1 << 16)
            while True:
                f.seek(size - back)
                lines = [x for x in f.read(back).splitlines() if x.strip()]
                if len(lines) >= 2 or back == size:   # the first may be cut short
                    break
                back = min(size, back * 4)
    except OSError:
        return None
    return line_hash(lines[-1]) if lines else None


def _chained(path: Path, held: Optional[BinaryIO], tail: List[Any], *records: Dict[str, Any]) -> None:
    """Append lines to a chained record file in one write, each keeping the hash of the line before it (prev). tail:
    [the last line's hash, the file's size] as this Kelvin left them, brought up to date when someone else wrote to the
    file since (another Kelvin, a person's editor), and moved on only once the lines are written. held: the file kept
    open (EventLog.holding: unbuffered, so a write that failed leaves nothing behind to go out later), else it is
    opened for the lines."""
    f = held if held is not None else open(path, "ab")
    try:
        size = os.fstat(f.fileno()).st_size if held is not None else f.tell()
        last = tail[0] if size == tail[1] else _last_hash(path)
        lines = []
        for record in records:
            line = (json.dumps(dict(record, prev=last or GENESIS), ensure_ascii=False) + "\n").encode("utf-8")
            lines.append(line)
            last = line_hash(line)
        data = memoryview(b"".join(lines))
        while data:   # an unbuffered file may take only part of it
            n = f.write(data)
            if not n:
                raise OSError("%s took nothing of the record" % path)
            data = data[n:]
        f.flush()
        tail[0], tail[1] = last, size + sum(len(x) for x in lines)
    finally:
        if held is None:
            f.close()


def _let_go(f: Optional[BinaryIO]) -> None:
    """Close a record file kept open, whatever its drive says now (a run's end never fails for it)."""
    if f is not None:
        try:
            f.close()
        except OSError:
            pass


def append_chained(path: Any, record: Dict[str, Any]) -> None:
    """Append one line to a chained record file other than the event logs (the config history's audit.jsonl)."""
    path = Path(path)
    with _TAILS_LOCK:
        key = str(path)
        if key not in _TAILS:
            _, last, size = _tail(path)
            _TAILS[key] = [last, size]
        path.parent.mkdir(parents=True, exist_ok=True)
        _chained(path, None, _TAILS[key], record)


def verify_chain(path: Any) -> Dict[str, Any]:
    """Walk a chained record file. {"lines", "chained" (lines that keep their prev), "since" (the time of the first
    chained line), "breaks" ([{"line", "seq", "wall", "why"}]: a line whose prev isn't the line before it, so that
    line or the one before was changed, or lines between were taken out; or a line without prev after the chain
    began, which something that doesn't chain wrote), "last" (the last line's hash)}."""
    out: Dict[str, Any] = {"lines": 0, "chained": 0, "since": None, "breaks": [], "last": None}
    before: Optional[str] = None
    try:
        f = open(path, "rb")
    except OSError:
        return out
    with f:
        for no, raw in enumerate(f, 1):
            if not raw.strip():
                continue
            out["lines"] += 1
            try:
                e = json.loads(raw)
            except ValueError:
                e = None
            if not isinstance(e, dict):
                out["breaks"].append({"line": no, "why": "not a record line"})
            elif e.get("prev") is not None:
                out["chained"] += 1
                if out["since"] is None:
                    out["since"] = e.get("wall")
                if e["prev"] != (before or GENESIS):
                    out["breaks"].append({"line": no, "seq": e.get("seq"), "wall": e.get("wall"),
                                          "why": "it doesn't follow the line before it"})
            elif out["chained"]:
                out["breaks"].append({"line": no, "seq": e.get("seq"), "wall": e.get("wall"),
                                      "why": "a line without its link, written by something that doesn't chain"})
            before = line_hash(raw)
    out["last"] = before
    return out


def _unchained(event: Dict[str, Any]) -> Dict[str, Any]:
    event.pop("prev", None)
    return event


class EventLog:
    """One JSON object per line; never rewritten. path=None keeps it in memory.

    shared: the lab's instrument log (~/Kelvin/instrument-log.jsonl), where every event of everyone who uses these
    instruments is also written, with user: the signed-in person (None: nobody, such as setting up on start).
    whose: the marks of unshared work the writing thread does (LabRuntime._whose_now), kept with each event it writes,
    so that other chats and phones through the Kelvin hub never see it. actor: the AI the writing thread works for
    (LabRuntime._actor_now: the chat's model and the chat), kept with each event as ai.

    A write never fails for the file: while it can't be written (its folder's drive or network share went away, a full
    disk), the lines are kept back and written first once it can be (tried again every RETRY_S), chained as they are
    written, so the chain holds across the gap; trouble says why meanwhile (the monitor's alarm). A record that raised
    made every change fail with it, a person's Stop answered with an error, and a stopped run's clean-up ended at its
    first drive."""

    def __init__(self, path: Any = None, clock: Any = None, shared: Any = None, user: Optional[str] = None,
                 whose: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
                 actor: Optional[Callable[[], Optional[Dict[str, Any]]]] = None):
        self.path = Path(path) if path else None
        self.clock = clock
        self.shared = Path(shared) if shared else None
        self.user = user
        self.whose = whose
        self.actor = actor
        self._memory: List[Dict[str, Any]] = []
        self._seq = 0
        self._tail: List[Any] = [None, 0]   # [the last line's hash, the file's size] (_chained)
        self._held: Optional[BinaryIO] = None          # the record's files while a run measures (holding)
        self._held_shared: Optional[BinaryIO] = None
        self._holds = 0
        self._lock = threading.Lock()  # agent, plans and people write from different threads; keep every line
        self.trouble: Optional[str] = None   # why the file can't be written, while it can't
        self._back: List[str] = []           # the lines kept back meanwhile
        self._dropped = 0                    # and those that didn't fit (KEEP_BACK)
        self._retry_at = 0.0                 # when (monotonic) to try the file again
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                self._seq, last, size = _tail(self.path)
                self._tail = [last, size]

    def ai_now(self) -> Optional[Dict[str, Any]]:
        """The AI the calling thread works for (actor), or None: a person, Kelvin itself, a queue."""
        try:
            return self.actor() if self.actor is not None else None
        except Exception:   # the record never loses an event to its marks
            return None

    @contextmanager
    def holding(self) -> Iterator[None]:
        """Keep the record's files open while a run measures (each line is still handed to the operating system as
        it is written, as closing the file did; a crash loses nothing more)."""
        with self._lock:
            self._holds += 1
            if self._holds == 1 and self.path:
                try:
                    self._held = open(self.path, "ab", buffering=0)
                except OSError:
                    self._held = None
        if self.shared:
            with _SHARED:
                if self._holds == 1 and self._held_shared is None:
                    try:
                        self.shared.parent.mkdir(parents=True, exist_ok=True)
                        self._held_shared = open(self.shared, "ab", buffering=0)
                    except OSError:
                        self._held_shared = None
        try:
            yield
        finally:
            with self._lock:
                self._holds -= 1
                last = self._holds == 0
                if last:
                    _let_go(self._held)
                    self._held = None
            if last:
                with _SHARED:
                    _let_go(self._held_shared)
                    self._held_shared = None

    def write(self, type_: str, **fields: Any) -> Dict[str, Any]:
        try:
            whose = self.whose() if self.whose is not None else None
        except Exception:   # the record never loses an event to its marks
            whose = None
        ai = self.ai_now() if "ai" not in fields else None
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
            if ai:
                event["ai"] = _jsonable(ai)
            if self.path:
                self._append(event)
            else:
                self._memory.append(event)
        if self.shared:
            try:
                with _SHARED:
                    self.shared.parent.mkdir(parents=True, exist_ok=True)
                    key = str(self.shared)
                    if key not in _SHARED_SEQ:
                        _SHARED_SEQ[key], last, size = _tail(self.shared)
                        _SHARED_TAIL[key] = [last, size]
                    _SHARED_SEQ[key] += 1
                    # its own numbering: each person's record numbers its events (own_seq), and those mixed in here
                    # read 34, 1, 35 (a field test's log, as Kelvin set up on start with nobody signed in)
                    try:
                        _chained(self.shared, self._held_shared, _SHARED_TAIL[key],
                                 dict(event, seq=_SHARED_SEQ[key], own_seq=event["seq"], user=self.user))
                    except OSError:   # a file kept open that failed is let go of: the next line opens it anew
                        _let_go(self._held_shared)
                        self._held_shared = None
                        raise
            except OSError:   # the person's own record has it; the lab's log is a copy
                pass
        return event

    def _append(self, event: Optional[Dict[str, Any]]) -> None:
        """An event onto the file, after those kept back (the caller holds the lock). While the file can't be written,
        it is kept back with them, and chained only as it is written; the file is tried again once RETRY_S has passed,
        never at every line (a network share that is away can take seconds to say so)."""
        if event is not None and not self._back:   # the usual way: straight onto the file
            try:
                _chained(self.path, self._held, self._tail, event)
                return
            except OSError as e:
                self._unwritten(e)
        if event is not None:
            self._back.append(json.dumps(event, ensure_ascii=False))
            if len(self._back) > KEEP_BACK:
                self._dropped += len(self._back) - KEEP_BACK
                del self._back[:-KEEP_BACK]
        if not self._back or time.monotonic() < self._retry_at:
            return
        try:
            _chained(self.path, self._held, self._tail, *[json.loads(line) for line in self._back])
        except OSError as e:
            self._unwritten(e)
            return
        self._back.clear()
        self.trouble, self._dropped = None, 0

    def _unwritten(self, e: OSError) -> None:
        """The file couldn't be written: why, and when to try again. A file kept open is let go of (its drive may
        have gone and come back): it is opened for each line from then on."""
        self._retry_at = time.monotonic() + RETRY_S
        self.trouble = "%s could not be written (%s)%s" % (
            self.path, e.strerror or e, "; %d older events could not be kept" % self._dropped if self._dropped else "")
        _let_go(self._held)
        self._held = None

    def flush(self) -> None:
        """The lines kept back, written now if the file can be (no more often than RETRY_S apart)."""
        if self.path:
            with self._lock:
                self._append(None)

    def events(self) -> List[Dict[str, Any]]:
        if not self.path:
            return list(self._memory)
        with self._lock:
            back = [json.loads(line) for line in self._back]
        try:
            if not self.path.exists():
                return back
            with open(self.path, encoding="utf-8") as f:
                return [_unchained(json.loads(line)) for line in f if line.strip()] + back
        except OSError:   # the file can't be read now: what is kept back, at least
            return back


class RunRecord:
    """One run, recorded point by point in its folder's record.bin while it is measured (rundata.py: flushed at
    once, synced to disk about once a second), and kept as run.h5 when it ends."""

    def __init__(self, run_id: str, path: Optional[Path], plan: Dict[str, Any], operator: str):
        self.run_id = run_id
        self.path = path
        self.rows: List[Dict[str, Any]] = []
        self.snapshots: Dict[str, Dict[str, Any]] = {}
        self.meta: Dict[str, Any] = {"run_id": run_id, "uuid": str(uuid.uuid4()), "plan": plan.get("plan"),
                                     "operator": operator, "started": _now(), "labharness": __version__}
        self.sha256: Optional[str] = None   # run.h5's seal, once the run is kept (finish)
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
            self._record.write("start", {"plan": plan_text(self._plan), "meta": dict(self.meta)}, sync=True)
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

    def abandon(self) -> None:
        """Let go of a run that never started (its record couldn't be written): its folder's lock goes."""
        if self._record is not None:
            try:
                self._record.release()
            except OSError:
                pass

    def finish(self, **meta: Any) -> None:
        """The run ended (done, stopped or failed): its record is closed and kept as run.h5. If that can't be
        written now (no h5py, a full disk), the record stays, and Kelvin finishes it at its next start."""
        self.meta.update({k: _jsonable(v) for k, v in meta.items()})
        self.meta["finished"] = _now()
        if self._record is None:
            return
        try:   # a record that can't be written (its drive went away) raises, and the run is let go of all the same
            self._write("finish", {"meta": dict(self.meta)}, sync=True)
            self._record.close()
            try:
                rundata.finish(self.path)
                self.sha256 = rundata.sealed(self.path)
            except Exception as e:   # the record keeps every point; it becomes run.h5 at the next start
                self.meta.setdefault("warnings", []).append("run.h5 not written yet: %s" % e)
        finally:
            try:
                self._record.release()
            except OSError:
                pass


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
    and iterdir() gives every run's folder: only folders named as Kelvin names runs (RUN_NAME), and in a chat's
    folder only those with a run's record (the person keeps other files there too). A folder that can't be read now
    (a drive that is away) is skipped until it is back."""

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
        if not run_id or not RUN_NAME.match(run_id) or "/" in run_id or "\\" in run_id or ":" in run_id:
            return None   # only Kelvin's runs, as iterdir lists them: named as a run (in a chat's folder, recorded)
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
                if d.name in seen or not RUN_NAME.match(d.name) or (root != self.home and not rundata.is_run(d)):
                    continue
                seen.add(d.name)
                yield d

    def exists(self) -> bool:
        return any(root.is_dir() for root in self.roots())


class RunStore:
    """Makes each run's folder, named by its id: in root (the data folder's runs/), or in the folder the run is
    given (its chat's). folders: where runs are kept, so an id is never used twice. noted(record, plan): told of each
    run as its record is made (the person's library, scanlibrary.py); whatever it does, the run goes on."""

    def __init__(self, root: Any = None, folders: Optional[RunFolders] = None,
                 noted: Optional[Callable[[RunRecord, Dict[str, Any]], None]] = None):
        self.root = Path(root) if root else None
        self.folders = folders
        self.noted = noted

    def _run_id(self, root: Optional[Path], plan_name: str, when: Optional[float] = None) -> str:
        """date-time-plan, the plan's part short enough that the run's folder and every file Kelvin keeps in it fit
        in a Windows path (a deep data folder leaves less room: the plan's part shrinks to a hash, then goes). when:
        the time it was taken (old data imported), else now."""
        stamp = (datetime.fromtimestamp(when) if when is not None else datetime.now()).strftime("%Y%m%d-%H%M%S")
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

    def create(self, plan_name: str, plan: Dict[str, Any], operator: str, folder: Any = None,
               meta: Optional[Dict[str, Any]] = None, when: Optional[float] = None) -> RunRecord:
        """Every run's record is made here: plans (and a queue's scans and a recipe's stages) and scripts alike.
        meta: what the run's record says from its start (where it ran, whose work it is), as noted sees it. when: the
        time old data imported was taken, for its id (and so its place among the runs)."""
        rec = self._create(plan_name, plan, operator, folder, when)
        rec.meta.update(meta or {})
        if self.noted is not None:
            try:
                self.noted(rec, plan)
            except Exception:   # the library is a help: a run never stops for it
                pass
        return rec

    def _create(self, plan_name: str, plan: Dict[str, Any], operator: str, folder: Any = None,
                when: Optional[float] = None) -> RunRecord:
        root = Path(folder) if folder and self.root else self.root
        run_id = self._run_id(root, plan_name, when)
        if root is None or root == self.root:
            return self._record(run_id, root / run_id if root else None, plan, operator)
        try:
            rec = RunRecord(run_id, root / run_id, plan, operator)
        except OSError as e:   # the chat's folder is away (a drive, a server): the run still keeps its data
            run_id = self._run_id(self.root, plan_name, when)   # the data folder may be deeper than the chat's
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
