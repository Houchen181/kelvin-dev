"""The lab notebook's raw record: an append-only event log and per-run folders (each run's data: rundata.py).

The record is chained: each line keeps the SHA-256 of the line before it (prev; the first line of a file follows
GENESIS), so a line changed or taken out afterwards breaks the chain where it was, and Verify (integrity.py) finds it.
Lines an older Kelvin wrote keep no prev; the first chained line follows the last of them. While a run measures, the
record's files stay open (EventLog.holding), each line still handed to the operating system as it is written, as
closing the file did: a set in a plan costs some 15 µs of record instead of half a millisecond of opening and closing
two files."""
from __future__ import annotations

import atexit
import collections
import errno
import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
import weakref
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable, Deque, Dict, Iterator, List, Optional, Tuple

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
WRITE_WAIT_S = 0.5      # how long a write waits for its line to reach the file: past it, the line goes on by itself
STALL_S = 10.0          # a file call under way this long: the record's trouble says so (the monitor's alarm)
IDLE_S = 2.0            # the record's own thread ends after this long with nothing to do; a new line starts it again
RECORD_WAIT_S = 0.5     # how long a run's point waits for its frame to reach the file: past it, it goes on by itself
RECORD_KEEP_BYTES = 256 * 1024 * 1024   # a run's frames kept in memory at most while its folder doesn't answer
_LOGS: "weakref.WeakSet[Any]" = weakref.WeakSet()   # the records that write files (settle_all, as Kelvin quits)


def line_hash(line: bytes) -> str:
    """A record line's SHA-256 (without its line end): what the next line keeps as prev."""
    return hashlib.sha256(line.rstrip(b"\r\n")).hexdigest()


def _tail(path: Path) -> Tuple[int, Optional[str], Optional[int]]:
    """(lines, its last line's hash or None, size in bytes) of a record file; (0, None, 0) without one. The size is
    None when the file doesn't end with a line end (a line that reached it only in part): the next write ends that
    line first (_chained)."""
    n, last, size, ended = 0, None, 0, True
    try:
        with open(path, "rb") as f:
            for line in f:
                n += 1
                ended = line.endswith(b"\n")
                if line.strip():
                    last = line
            size = f.tell()
    except OSError:
        return 0, None, 0
    return n, (line_hash(last) if last is not None else None), (size if ended else None)


def _ends_line(path: Path, size: int) -> bool:
    """Whether a record file's first `size` bytes end with a line end (or there are none)."""
    if not size:
        return True
    try:
        with open(path, "rb") as f:
            f.seek(size - 1)
            return f.read(1) in (b"\n", b"")
    except OSError:
        return True   # can't be read now: the write says what is wrong


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
        # a line the file took only in part (a full disk, a share that went away mid-line, the NUL bytes NTFS leaves
        # after a power cut) is ended first: the next line was glued onto it, which made that line unreadable and every
        # reader of the record fail from then on (audit 0.3.52, D3). The part stays, a line of its own, which Verify
        # names; the chain goes on from it.
        lead = b"" if size == tail[1] or _ends_line(path, size) else b"\n"
        lines = []
        for record in records:
            line = (json.dumps(dict(record, prev=last or GENESIS), ensure_ascii=False) + "\n").encode("utf-8")
            lines.append(line)
            last = line_hash(line)
        data = memoryview(lead + b"".join(lines))
        while data:   # an unbuffered file may take only part of it
            n = f.write(data)
            if not n:
                raise OSError("%s took nothing of the record" % path)
            data = data[n:]
        f.flush()
        tail[0], tail[1] = last, size + len(lead) + sum(len(x) for x in lines)
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


ENDS_FILE = "record-ends.json"   # where each person's record ended, kept outside the data folder (keep_end)
# the lines after which a record's end is kept: what Verify relies on (a run's seal, an entry, a signature, a backup)
END_TYPES = ("run_end", "run_sealed", "run_imported", "entry", "entry_signed", "backup")
_ENDS_LOCK = threading.Lock()


def _hash_before(path: Path, n: int) -> Optional[str]:
    """The hash of the last line in a record file's first n bytes (None: none, or it can't be read)."""
    try:
        with open(path, "rb") as f:
            back = min(n, 1 << 16)
            while True:
                f.seek(n - back)
                lines = [x for x in f.read(back).splitlines() if x.strip()]
                if len(lines) >= 2 or back == n:   # the first may be cut short
                    break
                back = min(n, back * 4)
    except OSError:
        return None
    return line_hash(lines[-1]) if lines else None


def end_holds(path: Any, end: Dict[str, Any]) -> Optional[str]:
    """Whether a record still holds the end kept for it ({"bytes", "last", "lines", "at"}: its first `bytes` end with
    the line whose hash is `last`): None when it does, else what happened since, in words."""
    path = Path(path)
    n = int(end.get("bytes") or 0)
    when = str(end.get("at") or "")[:16].replace("T", " ")
    try:
        size = path.stat().st_size
    except OSError:
        return "it is gone, though it had %s lines on %s" % (end.get("lines"), when)
    if size < n:
        return "it was cut short: it had %s lines on %s, and less now" % (end.get("lines"), when)
    if n and _hash_before(path, n) != end.get("last"):
        return "it was changed: its line %s isn't the one it was on %s" % (end.get("lines"), when)
    return None


def read_ends(where: Any) -> Dict[str, Dict[str, Any]]:
    """The records' ends kept in this file, by each record's path."""
    try:
        data = json.loads(Path(where).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _write_ends(where: Path, ends: Dict[str, Dict[str, Any]]) -> None:
    where.parent.mkdir(parents=True, exist_ok=True)
    tmp = where.with_name("%s.%d.tmp" % (where.name, threading.get_ident()))
    tmp.write_text(json.dumps(ends, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(where))


def keep_end(where: Any, path: Any, end: Dict[str, Any]) -> bool:
    """Keep where a record ends now (end: {"bytes", "last", "lines", "at"}) in the file `where`, outside the data
    folder: a record cut short or changed later is then found by Verify, though its own chain still holds (nothing
    else held the chain's end: audit 0.3.52, D6). Moved on only while the record still holds the end kept before;
    once it doesn't, that end stays for Verify to say. Whether it was kept."""
    where, key = Path(where), os.path.abspath(str(path))
    with _ENDS_LOCK:
        ends = read_ends(where)
        before = ends.get(key)
        if before is not None and before != end and end_holds(path, before) is not None:
            return False
        ends[key] = dict(end)
        _write_ends(where, ends)
    return True


def move_end(where: Any, old: Any, new: Any) -> None:
    """A record moved from old to new (the data folder moved): its end kept from now on under its new place."""
    where = Path(where)
    with _ENDS_LOCK:
        ends = read_ends(where)
        end = ends.pop(os.path.abspath(str(old)), None)
        if end is not None:
            ends[os.path.abspath(str(new))] = end
            _write_ends(where, ends)


def _unchained(event: Dict[str, Any]) -> Dict[str, Any]:
    event.pop("prev", None)
    return event


def _record_line(line: str) -> Optional[Dict[str, Any]]:
    """A record line as its event, or None for one that isn't (cut short, NUL bytes): Verify names those."""
    try:
        e = json.loads(line)
    except ValueError:
        return None
    return e if isinstance(e, dict) else None


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
    first drive.

    Nor does a write wait on the file: its line goes to the file on a thread of the record's own (_write_on), in
    order, and the write waits WRITE_WAIT_S at most for it (a local disk writes it at once, so a line is in the file
    as its write returns). A data folder on a network share that stalls (a NAS rebooting, a VPN dropping) blocks a file
    call for tens of seconds: every gate command writes a line, and each one, a run's steps, a ramp to safe and the
    monitor with them, waited out that call; once one write has stalled, the next ones don't wait for it at all."""

    def __init__(self, path: Any = None, clock: Any = None, shared: Any = None, user: Optional[str] = None,
                 whose: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
                 actor: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
                 ends: Optional[Callable[[], Optional[Path]]] = None):
        self.path = Path(path) if path else None
        self.clock = clock
        self.shared = Path(shared) if shared else None
        self.user = user
        self.whose = whose
        self.actor = actor
        self.ends = ends   # where the record's end is kept outside it (keep_end), or None: nowhere
        self._memory: List[Dict[str, Any]] = []
        self._seq = 0
        self._tail: List[Any] = [None, 0]   # [the last line's hash, the file's size] (_chained)
        self._held: Optional[BinaryIO] = None          # the record's files while a run measures (holding)
        self._held_shared: Optional[BinaryIO] = None
        self._holds = 0
        self._lock = threading.Lock()  # agent, plans and people write from different threads; keep every line
        self._turn = threading.Condition(self._lock)   # the writer's work, and the writes waiting for their lines
        self._trouble: Optional[str] = None  # why the file can't be written, while it can't
        self._back: List[str] = []           # the lines kept back meanwhile
        self._dropped = 0                    # and those that didn't fit (KEEP_BACK)
        self._retry_at = 0.0                 # when (monotonic) to try the file again
        # the writer (_write_on): what it hasn't taken yet, what it writes now, the last line it has dealt with
        # (written, or kept back), flush()'s asks and its answers, holding()'s asks (open or let go of the files kept
        # open) and its answers, and a file call under way since when, and on which file
        self._pending: List[Dict[str, Any]] = []
        self._flight: List[Dict[str, Any]] = []
        self._done = 0
        self._flush_asked = self._flushed = 0
        self._hold_want, self._hold_asked, self._hold_done = False, 0, 0
        self._io_since: Optional[float] = None
        self._io_what: Any = None
        self._writer: Optional[threading.Thread] = None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                self._seq, last, size = _tail(self.path)
                self._tail = [last, size]
        self._done = self._seq
        if self.path or self.shared:
            _LOGS.add(self)

    @property
    def trouble(self) -> Optional[str]:
        """Why the record can't be written now, while it can't (the monitor's alarm): its file failed, or a file call
        has been under way STALL_S or more (its drive or network share doesn't answer)."""
        if self._trouble:
            return self._trouble
        since, what = self._io_since, self._io_what
        if since is not None and time.monotonic() - since >= STALL_S:
            return "%s doesn't answer: a write to it has taken %d s so far" % (what, time.monotonic() - since)
        return None

    def ai_now(self) -> Optional[Dict[str, Any]]:
        """The AI the calling thread works for (actor), or None: a person, Kelvin itself, a queue."""
        try:
            return self.actor() if self.actor is not None else None
        except Exception:   # the record never loses an event to its marks
            return None

    # ------------------------------------------------------------------ the callers' side

    @contextmanager
    def holding(self) -> Iterator[None]:
        """Keep the record's files open while a run measures (each line is still handed to the operating system as
        it is written, as closing the file did; a crash loses nothing more). Opened and let go of by the record's own
        thread, waited for WRITE_WAIT_S at most."""
        with self._lock:
            self._holds += 1
            if self._holds == 1 and (self.path or self.shared):
                self._ask_hold(True)
        try:
            yield
        finally:
            with self._lock:
                self._holds -= 1
                if self._holds == 0 and (self.path or self.shared):
                    self._ask_hold(False)

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
            if not self.path:
                self._memory.append(event)
            if self.path or self.shared:   # to the file(s) on the record's own thread; its line waited for a moment
                self._pending.append(event)
                self._wake()
                seq = event["seq"]
                self._await(lambda: self._done >= seq)
        return event

    def flush(self) -> None:
        """The lines kept back, written now if the file can be (no more often than RETRY_S apart): asked of the
        record's own thread, waited for WRITE_WAIT_S at most (the monitor asks at each of its looks)."""
        if not self.path:
            return
        with self._lock:
            if not (self._back or self._pending or self._flight):
                return
            self._flush_asked += 1
            asked = self._flush_asked
            self._wake()
            self._await(lambda: self._flushed >= asked)

    def settle(self, within: float = 5.0) -> bool:
        """Wait, `within` seconds at most, for every line written so far to have been dealt with (written, or kept
        back): before Kelvin quits, or a test reads the file itself. Whether it was."""
        end = time.monotonic() + within
        with self._lock:
            seq = self._seq
            while self._done < seq or self._pending or self._flight:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                if self._pending:
                    self._wake()
                self._turn.wait(min(left, 0.05))
        return True

    def events(self) -> List[Dict[str, Any]]:
        if not self.path:
            return list(self._memory)
        with self._lock:   # what isn't in the file (yet), taken first: a line written meanwhile is read from the file
            unwritten = [json.loads(line) for line in self._back] + [dict(e) for e in self._flight] + \
                [dict(e) for e in self._pending]
        try:
            if not self.path.exists():
                on_file: List[Dict[str, Any]] = []
            else:   # a line that can't be read (one the file took only in part) is left out, never the whole record
                with open(self.path, encoding="utf-8", errors="replace") as f:
                    on_file = [_unchained(e) for e in (_record_line(line) for line in f if line.strip())
                               if e is not None]
        except OSError:   # the file can't be read now: what is kept back, at least
            on_file = []
        seen = {(e.get("seq"), e.get("wall"), e.get("type")) for e in on_file}
        out = list(on_file)
        for e in unwritten:
            key = (e.get("seq"), e.get("wall"), e.get("type"))
            if key not in seen:
                seen.add(key)
                out.append(e)
        return out

    def _ask_hold(self, want: bool) -> None:
        """(holding the lock) The files kept open opened (want) or let go of, by the writer, waited for a moment."""
        self._hold_want = want
        self._hold_asked += 1
        asked = self._hold_asked
        self._wake()
        self._await(lambda: self._hold_done >= asked)

    def _wake(self) -> None:
        """(holding the lock) The writer to its work: started when there is none."""
        if self._writer is None or not self._writer.is_alive():
            self._writer = threading.Thread(target=self._write_on, name="kelvin-record", daemon=True)
            self._writer.start()
        self._turn.notify_all()

    def _await(self, done: Callable[[], bool]) -> None:
        """(holding the lock) Wait for the writer to have done it, WRITE_WAIT_S at most: not at all while the file is
        known to be away and not due to be tried again (the line is kept back, and goes once it can), nor while a file
        call has been under way longer than that (its drive doesn't answer: the line goes on by itself)."""
        if self._trouble and time.monotonic() < self._retry_at:
            return
        end = time.monotonic() + WRITE_WAIT_S
        while not done():
            now = time.monotonic()
            since = self._io_since
            if now >= end or (since is not None and now - since > WRITE_WAIT_S):
                return
            self._turn.wait(min(end - now, 0.05))

    # ------------------------------------------------------------------ the writer's side: the record's own thread

    def _write_on(self) -> None:
        """The record's own thread: takes the lines in order and writes them (_append, _share), opens and lets go of
        the files kept open (holding), tries the lines kept back again when asked (flush); it ends after IDLE_S with
        nothing to do, and the next line starts it again. Never ends on an error: a line it couldn't deal with is
        kept back, or (the lab's log, a copy) left out."""
        me = threading.current_thread()
        while True:
            with self._lock:
                idle = time.monotonic() + IDLE_S
                while not (self._pending or self._flush_asked > self._flushed or self._hold_asked > self._hold_done):
                    left = idle - time.monotonic()
                    if left <= 0:
                        if self._writer is me:
                            self._writer = None
                        return
                    self._turn.wait(left)
                batch, self._pending = self._pending, []
                self._flight = batch
                flush, hold = self._flush_asked, self._hold_asked
                want = self._hold_want
                change = hold > self._hold_done
            try:
                if change and want:
                    self._open_held()
                if self.path and (batch or flush > self._flushed):
                    self._append(batch)
                if self.shared:
                    for event in batch:
                        self._share(event)
                if change and not want:
                    self._let_go_held()
            except Exception:   # never: each step catches what the file says; this keeps the thread for the next
                pass
            finally:
                with self._lock:
                    self._flight = []
                    if batch:
                        self._done = max(self._done, batch[-1]["seq"])
                    self._flushed = max(self._flushed, flush)
                    self._hold_done = max(self._hold_done, hold)
                    self._turn.notify_all()

    def _io(self, what: Any, call: Callable[[], Any]) -> Any:
        """A file call, noted while it is under way (trouble says one that takes STALL_S or more)."""
        self._io_since, self._io_what = time.monotonic(), what
        try:
            return call()
        finally:
            self._io_since = None

    def _open_held(self) -> None:
        if self.path and self._held is None:
            try:
                self._held = self._io(self.path, lambda: open(self.path, "ab", buffering=0))
            except OSError:
                self._held = None
        if self.shared:
            with _SHARED:
                if self._held_shared is None:
                    try:
                        def opening() -> BinaryIO:
                            self.shared.parent.mkdir(parents=True, exist_ok=True)
                            return open(self.shared, "ab", buffering=0)
                        self._held_shared = self._io(self.shared, opening)
                    except OSError:
                        self._held_shared = None

    def _let_go_held(self) -> None:
        _let_go(self._held)
        self._held = None
        with _SHARED:
            _let_go(self._held_shared)
            self._held_shared = None

    def _append(self, events: List[Dict[str, Any]]) -> None:
        """Events onto the file, after those kept back. While the file can't be written, they are kept back with them,
        and chained only as they are written; the file is tried again once RETRY_S has passed, never at every line (a
        network share that is away can take seconds to say so)."""
        if events and not self._back:   # the usual way: straight onto the file
            try:
                self._io(self.path, lambda: _chained(self.path, self._held, self._tail, *events))
                self._end_kept(events)
                return
            except OSError as e:
                self._unwritten(e)
        with self._lock:
            if events:
                self._back.extend(json.dumps(e, ensure_ascii=False) for e in events)
                if len(self._back) > KEEP_BACK:
                    self._dropped += len(self._back) - KEEP_BACK
                    del self._back[:-KEEP_BACK]
            due = bool(self._back) and time.monotonic() >= self._retry_at
            lines = [json.loads(line) for line in self._back] if due else []
        if not due:
            return
        try:
            self._io(self.path, lambda: _chained(self.path, self._held, self._tail, *lines))
        except OSError as e:
            self._unwritten(e)
            return
        with self._lock:   # only this thread adds to them: those written are the first len(lines)
            del self._back[:len(lines)]
            if not self._back:
                self._trouble, self._dropped = None, 0
        self._end_kept(lines)

    def _end_kept(self, written: List[Dict[str, Any]]) -> None:
        """Where the record ends now, kept outside it (keep_end) once a line Verify relies on is written (END_TYPES):
        rarely, so a run's points never wait for it. Whatever goes wrong here, the record's line is written."""
        if self.ends is None or not any(e.get("type") in END_TYPES for e in written):
            return
        try:
            where = self.ends()
            if where is not None and self._tail[1] is not None:
                last = written[-1]
                keep_end(where, self.path, {"bytes": self._tail[1], "last": self._tail[0],
                                            "lines": last.get("seq"), "at": last.get("wall")})
        except Exception:   # the end is a help for Verify; the record never fails for it
            pass

    def _unwritten(self, e: OSError) -> None:
        """The file couldn't be written: why, and when to try again. A file kept open is let go of (its drive may
        have gone and come back): it is opened for each line from then on."""
        self._retry_at = time.monotonic() + RETRY_S
        self._trouble = "%s could not be written (%s)%s" % (
            self.path, e.strerror or e, "; %d older events could not be kept" % self._dropped if self._dropped else "")
        _let_go(self._held)
        self._held = None

    def _share(self, event: Dict[str, Any]) -> None:
        """The event's copy in the lab's instrument log: its own numbering (each person's record numbers its events,
        own_seq, and those mixed in here read 34, 1, 35: a field test's log, as Kelvin set up on start with nobody
        signed in). A line it can't take is left out: the person's own record has it."""
        try:
            with _SHARED:
                key = str(self.shared)

                def write() -> None:
                    self.shared.parent.mkdir(parents=True, exist_ok=True)
                    if key not in _SHARED_SEQ:
                        _SHARED_SEQ[key], last, size = _tail(self.shared)
                        _SHARED_TAIL[key] = [last, size]
                    _SHARED_SEQ[key] += 1
                    _chained(self.shared, self._held_shared, _SHARED_TAIL[key],
                             dict(event, seq=_SHARED_SEQ[key], own_seq=event["seq"], user=self.user))
                try:
                    self._io(self.shared, write)
                except OSError:   # a file kept open that failed is let go of: the next line opens it anew
                    _let_go(self._held_shared)
                    self._held_shared = None
                    raise
        except OSError:   # the person's own record has it; the lab's log is a copy
            pass


def settle_all(within: float = 5.0) -> None:
    """Every record's lines dealt with, `within` seconds at most in all: as Kelvin quits (and as Python ends: its
    records' own threads end with it, and a line still on its way would be lost)."""
    end = time.monotonic() + within
    for log in list(_LOGS):
        try:
            log.settle(max(0.0, end - time.monotonic()))
        except Exception:
            pass


atexit.register(settle_all, 3.0)


class RunRecord:
    """One run, recorded point by point in its folder's record.bin while it is measured (rundata.py: flushed at
    once, synced to disk about once a second), and kept as run.h5 when it ends.

    Its frames go to the file on a thread of the record's own (_write_on), in order. The run's first lines are waited
    for (a folder that can't be written starts no run); once it measures, a point waits RECORD_WAIT_S at most for its
    frame, and none at all while a file call has already taken longer: a data folder on a network share that stalled
    (a NAS rebooting, a VPN dropping) held the run in its point's write for the whole stall, and a person's Stop with
    it: the drives it touched stayed where they were until the share answered. Its frames wait in memory meanwhile
    (RECORD_KEEP_BYTES at most), and its finish waits for every one. A frame that couldn't be written is said at the
    run's next write, as the write said it before."""

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
        self._started = False
        # the record's own writer (_write_on): the frames it hasn't written, how many were asked and written, the
        # bytes waiting, a file call under way since when, the error of a frame no caller has heard of yet
        self._turn = threading.Condition()
        self._frames: Deque[Tuple[bytes, bool]] = collections.deque()
        self._asked = self._written = self._backlog = 0
        self._io_since: Optional[float] = None
        self._failed: Optional[BaseException] = None   # the first frame's error no point has said yet
        self._failed_at: Dict[int, BaseException] = {}   # each frame that couldn't be written, by its number
        self._lost = 0                                   # frames that never reached the file: the finish says so
        self._lost_first: Optional[BaseException] = None
        self._writer: Optional[threading.Thread] = None
        if path:
            path.mkdir(parents=True)
            self._record = rundata.Recorder(path)

    def _write(self, kind: str, fields: Optional[Dict[str, Any]] = None, values: Optional[Dict[str, Any]] = None,
               sync: bool = False) -> None:
        if self._record is None:
            return
        data = rundata.make_frame(kind, fields, values)   # its values as they are now, whenever it reaches the file
        with self._turn:
            if kind == "row":   # an earlier frame that couldn't be written: the run stops at this point, as it did
                self._said_failed()
            frames = []
            if not self._started:   # the plan and who/where first, with the meta as it is by then
                self._started = True
                head = {"plan": plan_text(self._plan), "meta": dict(self.meta)}
                frames.append((rundata.make_frame("start", head), True))
            frames.append((data, sync))
            self._send(frames, RECORD_WAIT_S if self.rows else None)

    def _send(self, frames: List[Tuple[bytes, bool]], wait: Optional[float]) -> None:
        """These frames to the writer, in their turn, waited for `wait` seconds at most (None: until written); the
        caller holds _turn. One of these that couldn't be written within the wait is raised here; an earlier one's
        error waits for the run's next point (add) and its finish, which say it: an end snapshot or an analysis that
        raised it once was let go of, and the run said every point was kept with one missing from run.h5."""
        first = self._asked + 1
        for frame in frames:
            self._frames.append(frame)
            self._backlog += len(frame[0])
        self._asked += len(frames)
        mine = self._asked
        if frames and self._writer is None:
            self._writer = threading.Thread(target=self._write_on, name="kelvin-run-record", daemon=True)
            self._writer.start()
        self._turn.notify_all()
        end = None if wait is None else time.monotonic() + wait
        while self._written < mine:
            now = time.monotonic()
            if end is not None and self._backlog <= RECORD_KEEP_BYTES:
                since = self._io_since
                if now >= end or (since is not None and now - since >= wait):
                    return   # its folder doesn't answer now: the frame goes once it does, in its turn
                self._turn.wait(end - now)
            else:
                self._turn.wait(1.0)
        own = next((self._failed_at[n] for n in range(first, mine + 1) if n in self._failed_at), None)
        if own is not None:
            if self._failed is own:
                self._failed = None   # said now
            raise own

    def _said_failed(self) -> None:
        if self._failed is not None:
            e, self._failed = self._failed, None
            raise e

    def _settled(self, say: bool = True) -> None:
        """Every frame asked so far written (or failed), however long the file takes; say: an error raised."""
        with self._turn:
            if say:
                self._send([], None)
                return
            while self._written < self._asked:
                self._turn.wait(1.0)

    def _write_on(self) -> None:
        """The record's own thread: its frames to the file, in order; ends after IDLE_S with none (a new one starts
        it again)."""
        while True:
            with self._turn:
                if not self._frames:
                    self._turn.wait(IDLE_S)
                    if not self._frames:
                        self._writer = None
                        return
                data, sync = self._frames.popleft()
                self._io_since = time.monotonic()
            failed: Optional[BaseException] = None
            try:
                self._record.write_frame(data, sync)
            except BaseException as e:   # its folder went away, a full disk: said at the run's next write
                failed = e
            with self._turn:
                self._io_since = None
                self._written += 1
                self._backlog -= len(data)
                if failed is not None:
                    self._failed_at[self._written] = failed
                    self._lost += 1
                    if self._lost_first is None:
                        self._lost_first = failed
                    if self._failed is None:
                        self._failed = failed
                self._turn.notify_all()

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
                self._settled(say=False)
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
            self._settled()   # every frame in the file before it is closed
            self._record.close()
            try:
                rundata.finish(self.path)
                self.sha256 = rundata.sealed(self.path)
            except Exception as e:   # the record keeps every point; it becomes run.h5 at the next start
                self.meta.setdefault("warnings", []).append("run.h5 not written yet: %s" % e)
            if self._lost:   # frames that never reached the file: said, as a write that failed is
                first = self._lost_first
                why = (str(first).splitlines()[0][:200] if str(first) else type(first).__name__) if first else "?"
                raise OSError(getattr(first, "errno", None) or errno.EIO, "%d of the run's frames could not be "
                              "written (%s): run.h5 holds the rest" % (self._lost, why))
        finally:
            try:
                self._settled(say=False)
                self._record.release()
            except OSError:
                pass


RUN_NAME = re.compile(r"^\d{8}-\d{6}-[\w.-]*$")   # a run's folder is its id, as RunStore names it
NAME_MAX = 40      # characters of the plan's name in a run's folder name; a longer name is cut and hashed
PATH_MAX = 259     # Windows' longest path (MAX_PATH less its terminating NUL), unless long paths are switched on
INSIDE_MAX = 48    # the longest name Kelvin writes inside a run's folder: frames/00001_<reading>.npy, report.pptx
ID_TRIES = 50      # ids tried for a run whose name others take at the same moment


class RunFolderError(OSError):
    """A run's folder couldn't be made, so the run did not start (nothing was set); the message says why in words."""


def _taken(e: OSError, path: Path) -> bool:
    """Whether a run's folder couldn't be made because another run made it a moment ago (not because a file stands
    where a folder above it should be, which also says "already exists")."""
    try:
        return isinstance(e, FileExistsError) and path.is_dir()
    except OSError:
        return False


def run_name(plan_name: str, most: int = NAME_MAX) -> str:
    """The plan's part of a run's folder name: letters, digits and . _ - only, at most `most` characters. A longer
    one keeps its start and a short hash of the whole name, so two long names stay apart (a 70-character plan title
    once made a folder Windows could not create: l09, l10)."""
    # Windows drops a folder name's trailing dots: "IV at 4 K." was kept in a folder whose name was not its id, so
    # Verify found no seal for it, and "warm up..." got no folder at all (audit 0.3.52, D10)
    safe = re.sub(r"[^\w.-]+", "_", plan_name).rstrip(".")
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
        for _ in range(ID_TRIES):   # two runs of one name made in the same second: the second takes the next id, as
            try:                    # one made a second apart does (it was refused: audit 0.3.52, D10)
                return self._made(root, plan_name, plan, operator, when)
            except FileExistsError:
                continue
        raise RunFolderError("Kelvin could not find a free name for the run's folder in %s, so the run did not start "
                             "and nothing was set" % root)

    def _made(self, root: Optional[Path], plan_name: str, plan: Dict[str, Any], operator: str,
              when: Optional[float]) -> RunRecord:
        """The run's record with a free id; FileExistsError when another run took that id meanwhile."""
        run_id = self._run_id(root, plan_name, when)
        if root is None or root == self.root:
            return self._record(run_id, root / run_id if root else None, plan, operator)
        try:
            rec = RunRecord(run_id, root / run_id, plan, operator)
        except OSError as e:   # the chat's folder is away (a drive, a server): the run still keeps its data
            if _taken(e, root / run_id):
                raise
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
            if path is not None and _taken(e, path):   # another run took this id a moment ago: the next one
                raise
            too_long = os.name == "nt" and path is not None and len(str(path)) + INSIDE_MAX > PATH_MAX
            raise RunFolderError("Kelvin could not make the run's folder %s (%s)%s, so the run did not start and "
                                 "nothing was set" % (path, e.strerror or e, (
                                     ": the data folder's path is too long for Windows; choose a shorter one in "
                                     "Settings, Data folder") if too_long else "")) from e
