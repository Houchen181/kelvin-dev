"""A queue of scans, run one after another without anyone at the computer, like Special Measure's queue.

An item is a plan (a scan, as the run engine runs it), a step between scans, which sets values, waits for a
reading to settle and/or waits a fixed time: the safe counterpart of Special Measure's text items,

    step: cool to 2 K
    set: { tc.T_set: 2 }
    stabilize: { read: tc.T, within: 0.02, for: 120, timeout: 7200 }
    wait: 600

or one of the lab's own scripts, run as run_script runs it (scriptrun.py), with its values and arguments:

    script: sweep.py           # in the lab's scripts folders, or one a person imported (the Runs panel)
    params: { VMAX: 2 }
    args: [--fast]
    version: 20261031-101500-sweep.py   # optional: that run's script.py, as it ran then

Items can be added at any position, moved, edited or removed while the queue runs; only the item that is running
is fixed. A person approves the whole queue once, before it starts: every drive range its scans and steps use. A
script can't be rehearsed, so it asks its own approval when it starts. A scan or step that needs more than was
approved asks again. The queue pauses when an item fails, is declined or stopped, after an item marked then_pause,
or when Kelvin closes; it never starts by itself.
The queue is kept per lab in queue.json, next to its runs, with an account of its last run for the morning: what was
done, what stopped part-way and what data it kept, what didn't run, and every alarm raised meanwhile.

The portal calls the queue the Sequence. It has a name: a person's, or the minute its first item went into it, as
labMaster names a sequence ("2026-11-02 14:05"). An item can be skipped (it won't run; the running one ends and the
queue goes on), copied (duplicate, and a ladder: a copy of some items per value of one setting), and a script's or a
step's values changed in place (item_with)."""
from __future__ import annotations

import dataclasses
import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import yaml

from .eventlog import RUN_NAME
from .labconfig import read_yaml
from .plans import PlanError, Stabilize, _split, _stabilize, load_plan
from .yamlio import _Dumper as _YamlDumper

STATES = ("waiting", "running", "done", "failed", "stopped", "skipped")
STEP_KEYS = {"step", "set", "stabilize", "wait", "note"}
SCRIPT_KEYS = {"script", "params", "args", "python", "version", "note", "action"}
KINDS = {"plan": "scan", "step": "step", "script": "script"}   # each kind of item, in words
MAX_WAIT = 7 * 24 * 3600.0
MAX_NAME = 80        # a sequence's name, as a person gives it
MAX_COPIES = 400     # the items one ladder adds at most
SAVED_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,60}$")   # a saved queue's name (its file's)
# a step named for its wait ("wait 10 min", the Sequence's Wait block): its name follows a new wait (item_with)
WAIT_NAME = re.compile(r"^\s*wait\s+[\d.]+\s*(s|sec|secs|seconds?|min|mins|minutes?|h|hours?)\s*$", re.I)
_KEEP = object()     # item_with: a value not given (None takes it out)


def new_name(when: Optional[float] = None) -> str:
    """The name a new sequence gets: the minute its first item went in (when; now when None)."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(when or None))


class _ItemDumper(_YamlDumper):
    """An item's YAML as a person writes one: a key per line, a mapping or list of plain values on one line
    ({VMAX: 2}), numbers as load_yaml reads them back (yamlio)."""

    def represent(self, data: Any) -> None:
        node = self.represent_data(data)
        if isinstance(node, yaml.MappingNode):
            node.flow_style = False   # the item's own keys, one per line
        self.serialize(node)
        self.represented_objects, self.object_keeper, self.alias_key = {}, [], None


def dump_item(data: Dict[str, Any]) -> str:
    """An item's text from its mapping (_ItemDumper), in the order its keys were written."""
    return yaml.dump(data, Dumper=_ItemDumper, sort_keys=False, allow_unicode=True, default_flow_style=None,
                     width=1000)


def plain_name(name: Any) -> str:
    """A sequence's name as a person gives it: one line, at most MAX_NAME characters ("" for none). ValueError: too
    long."""
    text = " ".join(str(name or "").split())
    if len(text) > MAX_NAME:
        raise ValueError("a name is at most %d characters" % MAX_NAME)
    return text


def saved_name(name: str) -> str:
    """A sequence's name made to fit a saved queue's (SAVED_NAME: letters, digits, spaces, . _ -): "2026-11-02 14:05"
    is saved as "2026-11-02 14.05"; a name with nothing of that kind, under the minute it is saved."""
    text = re.sub(r"[^A-Za-z0-9 _.-]+", "-", str(name or "").replace(":", "."))
    text = re.sub(r"^[^A-Za-z0-9]+", "", " ".join(text.split()))[:61].rstrip()
    return text if SAVED_NAME.match(text) else new_name().replace(":", ".")


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
class ScriptItem:
    path: str                     # the script's file name or path, as run_script takes it
    params: Dict[str, Any] = field(default_factory=dict)
    args: List[str] = field(default_factory=list)
    python: str = ""              # the Python to run it in (scriptrun.python_named); "" for the one Kelvin picks
    version: Optional[str] = None   # a run whose script.py runs, as it ran then; None: the file as it is
    note: str = ""
    action: str = ""              # an experiment's action to run (experiment.py); "": the one marked main

    @property
    def name(self) -> str:
        base = re.split(r"[\\/]", self.path.strip().strip('"'))[-1] or self.path
        return base + (" (%s)" % self.action if self.action else "") + (
            " as it ran in %s" % self.version if self.version else "")


def load_script_item(source: Any) -> ScriptItem:
    data = read_yaml(source)[0] if isinstance(source, str) else source
    if not isinstance(data, dict) or "script" not in data:
        raise PlanError(["a script item starts with script: <its file name or path>"])
    errors: List[str] = []
    unknown = set(data) - SCRIPT_KEYS
    if unknown:
        errors.append("unknown keys %s (a script item has: %s)" % (", ".join(sorted(map(str, unknown))),
                                                                   ", ".join(sorted(SCRIPT_KEYS))))
    path = data.get("script")
    if not isinstance(path, str) or not path.strip():
        errors.append("script: its file name, or its path")
        path = ""
    params = data.get("params") or {}
    if not isinstance(params, dict) or not all(isinstance(k, str) for k in params):
        errors.append("params: a mapping of setting: value (check_script lists a script's settings)")
        params = {}
    args = data.get("args") or []
    if not isinstance(args, list) or any(isinstance(a, (dict, list)) for a in args):
        errors.append("args: a list of its command-line arguments")
        args = []
    python = data.get("python") or ""
    if not isinstance(python, str):
        errors.append("python: the path of the Python to run it in")
        python = ""
    version = data.get("version")
    if version is not None and not (isinstance(version, str) and RUN_NAME.match(version)):
        errors.append("version: the id of a run of this script, whose script.py runs as it ran then")
        version = None
    action = data.get("action") or ""
    if not isinstance(action, str):
        errors.append("action: the name of one of the experiment's actions")
        action = ""
    if errors:
        raise PlanError(errors)
    return ScriptItem(path.strip(), dict(params), [str(a) for a in args], python.strip(), version,
                      str(data.get("note") or ""), action.strip())


@dataclass
class Item:
    id: str
    kind: str                     # plan | step | script
    name: str
    text: str                     # the plan, step or script item as written (YAML)
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
    chat: Optional[str] = None      # the chat that queued it, and the model rules and marks it was under: its
    rules: Optional[Dict[str, Any]] = None   # runs keep them, as a chat's own runs do
    confidential: bool = False
    unpublished: bool = False
    then_pause: bool = False        # the queue pauses after this item (a person's "run now" from the Runs panel)
    restores: Optional[str] = None  # a step that sets back the settings item <id> ran with: if it doesn't finish,
    #                                 that item is skipped, never run in conditions it didn't run in
    restored_by: Optional[str] = None   # and that item: it starts only once step <id> has finished (else skipped)
    library: Optional[str] = None   # the library's entry its runs are noted under (a scan run again, restored)

    def view(self) -> Dict[str, Any]:
        return asdict(self)

    def whose(self) -> Dict[str, Any]:
        """Whose work it is, as its runs are filed (LabRuntime.attribution)."""
        return {"project": self.project, "chat": self.chat, "incognito": self.incognito, "unshared": self.unshared,
                "rules": self.rules, "confidential": self.confidential, "unpublished": self.unpublished,
                "folder": self.folder}


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
    if isinstance(data, dict) and "script" in data:
        return "script", load_script_item(data).name
    raise PlanError(["a queue item is a plan (plan: <name>, sweep, measure...), a step (step: <what>, set, "
                     "stabilize, wait) or one of the lab's scripts (script: <its file>, params, args)"])


def item_with(text: str, params: Optional[Dict[str, Any]] = None, sets: Optional[Dict[str, Any]] = None,
              wait: Any = _KEEP, setup: Optional[Dict[str, Any]] = None) -> str:
    """An item's text with some of its values changed, each given (None takes one out): a script's params, a step's
    set values and its wait (seconds), a scan's setup values (only those it has). Written again as YAML, so its
    comments go; checked when the item is changed (LabRuntime.queue_edit). PlanError: not an item's text."""
    try:
        data, _ = read_yaml(text if "\n" in text else text + "\n")
    except Exception as e:
        raise PlanError(["not YAML: %s" % e])
    if not isinstance(data, dict):
        raise PlanError(["not a queue item"])
    data = dict(data)

    def merged(key: str, given: Dict[str, Any], only_known: bool = False) -> None:
        now = data.get(key) or {}
        if not isinstance(now, dict):
            raise PlanError(["%s: a mapping" % key])
        now = dict(now)
        for k, v in given.items():
            if v is None:
                now.pop(k, None)
            elif not only_known or k in now:
                now[k] = v
        if now:
            data[key] = now
        else:
            data.pop(key, None)
    if params is not None:
        merged("params", params)
    if sets is not None:
        merged("set", sets)
    if setup is not None:
        merged("setup", setup, only_known=True)
    if wait is not _KEEP:
        if wait is None:
            data.pop("wait", None)
        else:
            data["wait"] = wait
            name = str(data.get("step") or "")
            if WAIT_NAME.match(name):   # "wait 10 min", the Wait block's own name: it says the new time
                data["step"] = "wait " + span_words(float(wait))
    return dump_item(data)


def span_words(seconds: float) -> str:
    """A time in a few words: "30 s", "10 min", "1.5 h"."""
    if seconds < 90:
        return "%g s" % round(seconds, 1)
    if seconds < 5400:
        return "%g min" % round(seconds / 60, 1)
    return "%g h" % round(seconds / 3600, 1)


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
        self.cut_short: List[Item] = []   # the items that were running when Kelvin last closed
        self.last: Optional[Dict[str, Any]] = None   # the account of its last run (summarize), until it runs again
        self.name = ""               # the sequence's name: a person's, or the minute its first item went in
        self.name_kept = False       # a person named it while it was empty: its first item keeps that name
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
                self.cut_short.append(item)
            self.items.append(item)
        self.on_error = data.get("on_error") if data.get("on_error") in ("pause", "continue") else "pause"
        self.last = data.get("last") if isinstance(data.get("last"), dict) else None
        self.name = data.get("name") if isinstance(data.get("name"), str) else ""
        self.name_kept = bool(data.get("name_kept")) and not self.items
        if not self.name and self.items:   # kept before a sequence had a name: the minute its first item went in
            self.name = new_name(min(i.added for i in self.items) or None)
        if any(i.status == "waiting" for i in self.items):
            self.state, self.reason = "paused", "Kelvin restarted; start the queue again when you are ready"

    def save(self) -> None:
        self.changed = time.time()
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"items": [i.view() for i in self.items], "on_error": self.on_error,
                                       "last": self.last, "name": self.name, "name_kept": self.name_kept or None},
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
            folder: Optional[str] = None, chat: Optional[str] = None, rules: Optional[Dict[str, Any]] = None,
            confidential: bool = False, unpublished: bool = False, then_pause: bool = False, legacy: bool = False,
            library: Optional[str] = None) -> Item:
        kind, name = parse_item(text)
        if kind == "script" and not note.strip():   # a script item's own note: line, kept with its run
            note = load_script_item(text).note
        item = Item(uuid.uuid4().hex[:8], kind, name, text.strip() + "\n", note=note.strip(), by=by, added=time.time(),
                    project=project, incognito=incognito, unshared=unshared, folder=folder, chat=chat, rules=rules,
                    confidential=confidential, unpublished=unpublished, then_pause=bool(then_pause),
                    legacy=bool(legacy), library=library)
        with self.lock:
            self._first_in()
            if position is None or position > len(self.items):
                self.items.append(item)
            else:
                self.items.insert(max(0, int(position) - 1), item)
            self.save()
        return item

    def _first_in(self) -> None:
        """An item goes in: into an empty queue, it starts a new sequence, named by the minute (unless a person named
        it while it was empty). Under the lock."""
        if not self.items and not self.name_kept:
            self.name = new_name()
        self.name_kept = False

    def rename(self, name: Any) -> str:
        """The sequence's name, as a person gives it ("": the minute, as a new one's). ValueError: too long."""
        text = plain_name(name)
        with self.lock:
            self.name = text or new_name()
            self.name_kept = not self.items
            self.save()
            return self.name

    def copies(self, made: Sequence[Tuple[Any, Optional[str]]], position: Optional[int] = None,
               by: str = "") -> List["Item"]:
        """Copies of items, each (item or its ref, its own text or None for the item's), waiting: filed as the item is
        (whose work it is, its library entry, its note), never marked to pause after, nor restoring another's
        settings. position: where the first goes (1 = first), the rest after it; None: each right after its item.
        PlanError: a text that isn't an item's; KeyError: no such item."""
        parsed = [(ref, None if text is None else (parse_item(text), text.strip() + "\n")) for ref, text in made]
        out: List[Item] = []
        with self.lock:
            found = [(self.find(ref) if not isinstance(ref, Item) else ref, own) for ref, own in parsed]
            at = None if position is None else max(0, min(len(self.items), int(position) - 1))
            for item, own in found:
                copy = dataclasses.replace(item, id=uuid.uuid4().hex[:8], status="waiting", by=by or item.by,
                                           added=time.time(), started=None, finished=None, run_id=None, message="",
                                           then_pause=False, restores=None,
                                           rules=dict(item.rules) if isinstance(item.rules, dict) else item.rules)
                if own is not None:
                    (copy.kind, copy.name), copy.text = own
                if at is None:
                    self.items.insert(self.items.index(item) + 1, copy)
                else:
                    self.items.insert(at, copy)
                    at += 1
                out.append(copy)
            if out:
                self.save()
        return out

    def skip(self, ref: Any, by: str = "") -> Item:
        """A waiting item, skipped: it won't run, and a person can put it back in line (again). The running item is
        ended by LabRuntime.queue_skip. ValueError: it is running, or has finished."""
        with self.lock:
            item = self.find(ref)
            if item.status == "running":
                raise ValueError("%s is running: the queue's skip ends it" % item.name)
            if item.status != "waiting":
                raise ValueError("%s has finished (%s)" % (item.name, item.status))
            self.mark(item, "skipped", "skipped by %s" % by if by else "skipped")
            return item

    def edit(self, ref: Any, text: str) -> Item:
        """An item's new text. A step restoring the settings another item ran with (restores) isn't edited: what it
        sets back is that run's, so the item would no longer run as it ran."""
        kind, name = parse_item(text)
        with self.lock:
            item = self.find(ref)
            if item.status == "running":
                raise ValueError("%s is running; it can be edited after it finishes" % item.name)
            if item.restores:
                raise ValueError("%s restores the settings another item ran with, so it isn't edited: remove it and "
                                 "its item, then use Run again" % item.name)
            item.kind, item.name, item.text = kind, name, text.strip() + "\n"
            item.status, item.run_id, item.message, item.started, item.finished = "waiting", None, "", None, None
            self.save()
            return item

    def retext(self, change: Callable[[str], str]) -> List[str]:
        """Each waiting item's text through change, as an instrument renamed changes its keys: the names of the items
        it changed. The running item, and those that ran, stay as they were."""
        with self.lock:
            changed = []
            for item in self.items:
                if item.status == "waiting":
                    text = change(item.text)
                    if text != item.text:
                        item.text = text
                        changed.append(item.name)
            if changed:
                self.save()
            return changed

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

    def set_then_pause(self, ref: Any, on: bool) -> Item:
        """Whether the queue pauses after this item."""
        with self.lock:
            item = self.find(ref)
            item.then_pause = bool(on)
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
            return {"state": self.state, "reason": self.reason, "on_error": self.on_error, "name": self.name,
                    "items": [i.view() for i in self.items], "running": running.id if running else None,
                    "waiting": sum(1 for i in self.items if i.status == "waiting"), "changed": self.changed,
                    "last": self.last}

    def summarize(self, started: float, alarms: List[Any], points: Callable[[str], Optional[Tuple[int, int]]],
                  unshared: Callable[[Item], bool] = lambda item: False) -> List[str]:
        """What happened since the queue started, in lines, for the person who comes in the morning, from the record
        alone: when it ran and how it ended, what was done, what stopped part-way and the data it kept, what stopped
        with none, what didn't run, and every alarm raised meanwhile (cleared or still on). Kept with the queue and
        shown with it until it runs again. alarms: the monitor's (raised, cleared, text); points(run_id): (points
        taken, points planned) of a run; unshared(item): an item of unshared work, which the account then names (it
        is kept marked so)."""
        end = time.time()
        with self.lock:
            ran = [(n, i) for n, i in enumerate(self.items, 1) if i.started and i.started >= started]
            waiting = [(n, i) for n, i in enumerate(self.items, 1) if i.status == "waiting"]
            skipped = [(n, i) for n, i in enumerate(self.items, 1) if i.status == "skipped" and not i.started
                       and (i.finished or 0) >= started]   # held back meanwhile (a restore before it didn't finish)
            state, reason = self.state, self.reason
        lines = ["The queue ran %s to %s (%s) and %s" % (_at(started), _at(end), _span(end - started),
                                                        "finished" if state == "idle" else "paused: %s" % reason)]
        done, partial, empty = [], [], []
        for n, i in ran:
            got = points(i.run_id) if i.run_id else None
            label = "#%d %s" % (n, i.name)
            if i.status == "done":
                done.append(label + (" (%d points, run %s)" % (got[0], i.run_id) if got else ""))
            elif got and got[0]:
                partial.append("%s, %d of %d points kept (run %s): %s" % (label, got[0], got[1], i.run_id,
                                                                         _why(i.message)))
            else:
                empty.append("%s: %s" % (label, _why(i.message) or i.status))
        if done:
            lines.append("Done: " + "; ".join(done))
        if partial:
            lines.append("Stopped part-way, its data kept: " + "; ".join(partial))
        if empty:
            lines.append("Stopped with no data: " + "; ".join(empty))
        if skipped:
            lines.append("Skipped: " + "; ".join("#%d %s: %s" % (n, i.name, i.message) for n, i in skipped))
        if waiting:
            lines.append("Not run: " + ", ".join("#%d %s" % (n, i.name) for n, i in waiting))
        # a guard's trip is one of them (monitor.Monitor.from_gate); so is an alarm that went up before and again now
        raised = sorted((a for a in alarms if (getattr(a, "last", None) or getattr(a, "raised", 0)) >= started),
                        key=lambda a: a.raised)
        lines.append("Alarms while it ran: " + "; ".join(
            "%s %s%s (%s)" % (_at(a.raised), a.text, " (%d times)" % a.count if getattr(a, "count", 1) > 1 else "",
                              "cleared %s" % _at(a.cleared) if a.cleared else "still on")
            for a in raised) if raised else "No alarm was raised while it ran.")
        named = [i for _, i in ran + skipped + waiting]
        with self.lock:
            self.last = {"started": started, "ended": end, "lines": lines, "items": [i.id for i in named]}
            if any(unshared(i) for i in named):
                self.last["unshared"] = True
            self.save()
        return lines

    def text(self, hidden: Optional[Callable[[Item], bool]] = None) -> str:
        """The queue in lines. hidden(item): an item of unshared work the reader may not see, which shows only its
        place and state, without its name, note and run; nor is the reason it paused the queue, or the account of the
        last run that names it, shown."""
        v = self.view()
        out = {i.id for i in self.items if hidden is not None and hidden(i)}
        reason = v["reason"]
        if reason and any(i.name in reason for i in self.items if i.id in out):
            reason = "at an item of unshared work"
        lines = ["queue: %s%s" % (v["state"], " (%s)" % reason if reason else "")]
        if self.name and not out:   # a person's own words, which may name unshared work: only when all is seen
            lines.append("name: %s" % self.name)
        for n, i in enumerate(self.items, 1):
            if i.id in out:
                lines.append("%d. [%s] a %s of unshared work (another project's or chat's)" % (
                    n, i.status, KINDS.get(i.kind, i.kind)))
                continue
            extra = (" → run %s" % i.run_id if i.run_id else "") + (": %s" % i.message if i.message else "") + (
                "; the queue pauses after it" if i.then_pause and i.status in ("waiting", "running") else "")
            lines.append("%d. [%s] %s %s%s%s" % (n, i.status, i.kind, i.name, " (%s)" % i.note if i.note else "", extra))
        if not self.items:
            lines.append("(empty: add plans, steps or scripts with queue_add)")
        if self.last and v["state"] not in ("running", "pausing"):
            if out & set(self.last.get("items") or []):
                lines.append("last run: its account names unshared work, which isn't shown here")
            else:
                lines += ["last run:"] + ["  " + line for line in self.last.get("lines") or []]
        return "\n".join(lines)


def _at(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def _span(seconds: float) -> str:
    return "%.0f min" % (seconds / 60) if seconds < 5400 else "%.1f h" % (seconds / 3600)


def _why(message: str) -> str:
    """Why a run stopped, from its item's message: "run X aborted after 9/80 points: <why>; then <cleanup>"."""
    m = re.search(r"points: (.*?)(?:; then |$)", message or "", re.S)
    return (m.group(1) if m else message or "").strip()[:400]


def saved_queues(folder: Path) -> List[str]:
    return sorted(p.stem for p in folder.glob("*.yaml")) if folder.is_dir() else []


def save_named(folder: Path, name: str, items: List[Item]) -> Path:
    """A queue kept for later, as YAML: the items' texts, separated by ---."""
    if not SAVED_NAME.match(name or ""):
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
