"""A queue of scans, run one after another without anyone at the computer, like Special Measure's queue.

An item is a plan (a scan, as the run engine runs it) or a step between scans, which sets values, waits for a
reading to settle and/or waits a fixed time: the safe counterpart of Special Measure's text items.

    step: cool to 2 K
    set: { tc.T_set: 2 }
    stabilize: { read: tc.T, within: 0.02, for: 120, timeout: 7200 }
    wait: 600

Items can be added at any position, moved, edited or removed while the queue runs; only the item that is running
is fixed. A person approves the whole queue once, before it starts: every drive range any item uses. Each scan is
still dry-run just before it runs, and a scan or step that needs more than was approved asks again. The queue
pauses when a scan fails, is declined or stopped, or Kelvin closes; it never starts by itself.
The queue is kept per lab in queue.json, next to its runs."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .labconfig import read_yaml
from .plans import PlanError, Stabilize, _split, _stabilize, load_plan

STATES = ("waiting", "running", "done", "failed", "stopped", "skipped")
STEP_KEYS = {"step", "set", "stabilize", "wait", "note"}
MAX_WAIT = 7 * 24 * 3600.0


@dataclass
class Step:
    name: str
    sets: List[Tuple[str, str, Any]] = field(default_factory=list)   # (instr, param, value)
    stabilize: Optional[Stabilize] = None
    wait: float = 0.0

    @property
    def keys(self) -> List[str]:
        return ["%s.%s" % (i, p) for i, p, _ in self.sets]


def load_step(source: Any) -> Step:
    data = read_yaml(source)[0] if isinstance(source, str) else source
    if not isinstance(data, dict) or "step" not in data:
        raise PlanError(["a step starts with step: <what it does>"])
    errors: List[str] = []
    unknown = set(data) - STEP_KEYS
    if unknown:
        errors.append("unknown keys %s (a step has: %s)" % (", ".join(sorted(unknown)), ", ".join(sorted(STEP_KEYS))))
    sets = []
    raw = data.get("set") or {}
    if not isinstance(raw, dict):
        errors.append("set: a mapping of instrument.parameter: value")
        raw = {}
    for key, value in raw.items():
        ip = _split(key, "set", errors)
        if ip:
            sets.append((ip[0], ip[1], value))
    stab = _stabilize(data.get("stabilize"), "stabilize", errors)
    try:
        wait = float(data.get("wait") or 0)
        if not 0 <= wait <= MAX_WAIT:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("wait: seconds, from 0 to a week")
        wait = 0.0
    if not sets and stab is None and not wait:
        errors.append("a step sets values, waits for a reading to settle, or waits (set, stabilize, wait)")
    if errors:
        raise PlanError(errors)
    return Step(str(data.get("step") or "step"), sets, stab, wait)


@dataclass
class Item:
    id: str
    kind: str                     # plan | step
    name: str
    text: str                     # the plan or step as written (YAML)
    note: str = ""
    status: str = "waiting"
    by: str = ""
    added: float = 0.0
    started: Optional[float] = None
    finished: Optional[float] = None
    run_id: Optional[str] = None
    message: str = ""
    project: Optional[str] = None   # the project of the chat that queued it, for runs with no device mounted
    incognito: bool = False         # queued from an incognito chat: its runs are never shared on the Kelvin hub
    unshared: bool = False          # queued from a chat that left unshared work: nor are these
    legacy: bool = False            # queued before Kelvin noted either: its runs are held like older runs
    folder: Optional[str] = None    # the folder of the chat that queued it, where its runs go

    def view(self) -> Dict[str, Any]:
        return asdict(self)


def parse_item(text: str) -> Tuple[str, str]:
    """(kind, name) of a queue item's text, checked for form (instruments are checked when it is added)."""
    try:
        data, _ = read_yaml(text)
    except Exception as e:
        raise PlanError(["not YAML: %s" % e])
    if isinstance(data, dict) and "plan" in data:
        return "plan", load_plan(text).name
    if isinstance(data, dict) and "step" in data:
        return "step", load_step(data).name
    raise PlanError(["a queue item is a plan (plan: <name>, sweep, measure...) or a step (step: <what>, set, "
                     "stabilize, wait)"])


class ScanQueue:
    """The items, in order, and where they are kept. Thread-safe; every change is saved."""

    def __init__(self, path: Optional[Path] = None):
        self.lock = threading.RLock()
        self.items: List[Item] = []
        self.path = path
        self.state = "idle"          # idle | running | pausing | paused
        self.reason = ""             # why it paused or stopped
        self.on_error = "pause"      # pause | continue
        self.changed = time.time()
        if path is not None:
            self._load()

    # ------------------------------------------------------------------ kept on disk

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, AttributeError):
            return
        for raw in data.get("items") or []:
            try:
                item = Item(**{k: raw[k] for k in Item.__dataclass_fields__ if k in raw})
            except TypeError:
                continue
            if "incognito" not in raw and not item.project:   # queued before Kelvin noted incognito chats: it may
                item.legacy = True                              # have come from one, so shared only with older runs
            if item.status == "running":   # Kelvin closed in the middle of it
                item.status, item.message = "stopped", "Kelvin closed while it ran"
            self.items.append(item)
        self.on_error = data.get("on_error") if data.get("on_error") in ("pause", "continue") else "pause"
        if any(i.status == "waiting" for i in self.items):
            self.state, self.reason = "paused", "Kelvin restarted; start the queue again when you are ready"

    def save(self) -> None:
        self.changed = time.time()
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"items": [i.view() for i in self.items], "on_error": self.on_error},
                                      indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def move_to(self, path: Path) -> None:
        with self.lock:
            self.path = path
            self.save()

    # ------------------------------------------------------------------ editing

    def find(self, ref: Any) -> Item:
        """An item by its id, or by its position (1 = first)."""
        with self.lock:
            for i in self.items:
                if i.id == str(ref):
                    return i
            try:
                n = int(str(ref).strip().lstrip("#"))
                return self.items[n - 1] if n >= 1 else self.items[n]
            except (ValueError, IndexError):
                raise KeyError("no queue item %r (the queue has %d)" % (ref, len(self.items)))

    def add(self, text: str, position: Optional[int] = None, note: str = "", by: str = "",
            project: Optional[str] = None, incognito: bool = False, unshared: bool = False,
            folder: Optional[str] = None) -> Item:
        kind, name = parse_item(text)
        item = Item(uuid.uuid4().hex[:8], kind, name, text.strip() + "\n", note=note.strip(), by=by, added=time.time(),
                    project=project, incognito=incognito, unshared=unshared, folder=folder)
        with self.lock:
            if position is None or position > len(self.items):
                self.items.append(item)
            else:
                self.items.insert(max(0, int(position) - 1), item)
            self.save()
        return item

    def edit(self, ref: Any, text: str) -> Item:
        kind, name = parse_item(text)
        with self.lock:
            item = self.find(ref)
            if item.status == "running":
                raise ValueError("%s is running; it can be edited after it finishes" % item.name)
            item.kind, item.name, item.text = kind, name, text.strip() + "\n"
            item.status, item.run_id, item.message, item.started, item.finished = "waiting", None, "", None, None
            self.save()
            return item

    def remove(self, ref: Any) -> Item:
        with self.lock:
            item = self.find(ref)
            if item.status == "running":
                raise ValueError("%s is running; stop it first" % item.name)
            self.items.remove(item)
            self.save()
            return item

    def move(self, ref: Any, position: int) -> Item:
        with self.lock:
            item = self.find(ref)
            self.items.remove(item)
            self.items.insert(max(0, min(len(self.items), int(position) - 1)), item)
            self.save()
            return item

    def again(self, ref: Any) -> Item:
        """Put a finished, failed or skipped item back in line, where it is."""
        with self.lock:
            item = self.find(ref)
            if item.status in ("waiting", "running"):
                return item
            item.status, item.run_id, item.message, item.started, item.finished = "waiting", None, "", None, None
            self.save()
            return item

    def clear_finished(self) -> int:
        with self.lock:
            before = len(self.items)
            self.items = [i for i in self.items if i.status in ("waiting", "running")]
            self.save()
            return before - len(self.items)

    # ------------------------------------------------------------------ running

    def waiting(self) -> List[Item]:
        with self.lock:
            return [i for i in self.items if i.status == "waiting"]

    def next_waiting(self) -> Optional[Item]:
        with self.lock:
            return next((i for i in self.items if i.status == "waiting"), None)

    def mark(self, item: Item, status: str, message: str = "", run_id: Optional[str] = None) -> None:
        with self.lock:
            item.status, item.message = status, message
            if run_id:
                item.run_id = run_id
            if status == "running":
                item.started, item.finished = time.time(), None
            elif status != "waiting":
                item.finished = time.time()
            self.save()

    def view(self) -> Dict[str, Any]:
        with self.lock:
            running = next((i for i in self.items if i.status == "running"), None)
            return {"state": self.state, "reason": self.reason, "on_error": self.on_error,
                    "items": [i.view() for i in self.items], "running": running.id if running else None,
                    "waiting": sum(1 for i in self.items if i.status == "waiting"), "changed": self.changed}

    def text(self) -> str:
        v = self.view()
        lines = ["queue: %s%s" % (v["state"], " (%s)" % v["reason"] if v["reason"] else "")]
        for n, i in enumerate(self.items, 1):
            extra = (" → run %s" % i.run_id if i.run_id else "") + (": %s" % i.message if i.message else "")
            lines.append("%d. [%s] %s %s%s%s" % (n, i.status, i.kind, i.name, " (%s)" % i.note if i.note else "", extra))
        if not self.items:
            lines.append("(empty: add plans or steps with queue_add)")
        return "\n".join(lines)


def saved_queues(folder: Path) -> List[str]:
    return sorted(p.stem for p in folder.glob("*.yaml")) if folder.is_dir() else []


def save_named(folder: Path, name: str, items: List[Item]) -> Path:
    """A queue kept for later, as YAML: the items' texts, separated by ---."""
    import re
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,60}$", name or ""):
        raise ValueError("name it with letters, digits, spaces, . _ -")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name.strip() + ".yaml")
    body = "\n---\n".join(("# note: %s\n" % i.note if i.note else "") + i.text.strip() for i in items)
    path.write_text(body + "\n", encoding="utf-8")
    return path


def load_named(folder: Path, name: str) -> List[Tuple[str, str]]:
    """[(text, note)] of a saved queue."""
    path = folder / (str(name).strip() + ".yaml")
    if path.parent.resolve() != folder.resolve() or not path.exists():
        raise KeyError("no saved queue %r (have: %s)" % (name, ", ".join(saved_queues(folder)) or "none"))
    out = []
    for part in path.read_text(encoding="utf-8").split("\n---\n"):
        part = part.strip()
        if not part:
            continue
        note = ""
        if part.startswith("# note: "):
            first, _, part = part.partition("\n")
            note = first[len("# note: "):]
        out.append((part, note))
    return out
