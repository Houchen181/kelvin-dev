"""The person's Home: the blocks Kelvin puts there as it works (a result, the next step it suggests, a reading to
watch, a note, a checklist), beside the lab's own tiles (the sequence, the runs, the lab). Kelvin adds and changes
them with its home tools (agent/server.py); a person ticks, pins or removes them on the Home page.

    home.json, in the data folder (each person's own, beside queue.json and library.json):
    {"blocks": [{"id", "kind", "title", "text", "value", "error", "unit", "items": [{"text", "done"}], "run_id",
                 "watch", "actions": [{"label", "do", "text" | "run_id" | "pane"}], "tone", "key", "pinned", "by",
                 "chat", "created", "updated"}], "changed"}

kind: result (a number a run gave, with its error and unit), next (a step Kelvin suggests, with what a click on it
does), watch (an instrument's reading, shown live), note, check (a checklist a person ticks). An action is a button:
do "queue" adds its text (a scan's plan or a step) to the end of the sequence, as the person's own; "ask" puts its
text in the chat; "run" opens a run; "pane" opens a panel (sequence, runs, lab, analysis). A click is the person's,
so nothing a block offers runs by itself: a sequence still asks its approval when it starts.

A block with a key is one Kelvin keeps up to date: adding a block whose key is on Home already changes that block, so
the reading Kelvin follows stays one block. Pinned blocks come first, then the newest; beyond MAX_BLOCKS the oldest
unpinned go. The file is written whole, then put in place; one that can't be read as Home is kept aside
(home.json.broken-<time>) and Home starts empty, so a broken file never stops Kelvin."""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

FILE = "home.json"
KINDS = ("result", "next", "watch", "note", "check")
DOES = ("queue", "ask", "run", "pane")
PANES = ("sequence", "runs", "lab", "analysis")
TONES = ("", "ok", "warn", "bad")
MAX_BLOCKS = 40
MAX_TITLE = 80
MAX_TEXT = 1200
MAX_ITEMS = 20
MAX_ITEM = 160
MAX_ACTIONS = 2
MAX_LABEL = 32
MAX_ACTION_TEXT = 4000   # a scan's plan, or the words put in the chat
MAX_KEY = 60
WATCH = re.compile(r"^[A-Za-z_][\w-]*\.[A-Za-z_][\w.-]*$")   # instrument.parameter
_LOCK = threading.RLock()   # every Home file, one change at a time (a person and Kelvin change it at once)


class Unreadable(OSError):
    """home.json is there but can't be read now (the computer refused): nothing is written over it."""


def _line(value: Any, most: int, what: str) -> str:
    text = " ".join(str(value if value is not None else "").split())
    if len(text) > most:
        raise ValueError("%s is at most %d characters" % (what, most))
    return text


def _text(value: Any, most: int, what: str) -> str:
    text = str(value if value is not None else "").strip()
    if len(text) > most:
        raise ValueError("%s is at most %d characters" % (what, most))
    return text


def _number_or_text(value: Any, what: str) -> Union[float, str, None]:
    """A value as given: a finite number stays a number; anything else is a few words ("on", "2.41 ± 0.02")."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("%s must be a finite number" % what)
        return value
    return _line(value, 40, what) or None


def _items(raw: Any, old: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """A checklist: each item a few words (a string, or {text, done}). An item still there keeps its tick."""
    if raw is None:
        return list(old or [])
    if not isinstance(raw, list):
        raise ValueError("items: a list of short lines")
    if len(raw) > MAX_ITEMS:
        raise ValueError("a checklist has at most %d items" % MAX_ITEMS)
    ticked = {i.get("text"): bool(i.get("done")) for i in old or [] if isinstance(i, dict)}
    out = []
    for item in raw:
        if isinstance(item, dict):
            text, done = _line(item.get("text"), MAX_ITEM, "an item"), item.get("done")
        else:
            text, done = _line(item, MAX_ITEM, "an item"), None
        if text:
            out.append({"text": text, "done": bool(ticked.get(text, False) if done is None else done)})
    return out


def _actions(raw: Any) -> List[Dict[str, Any]]:
    """A block's buttons: [{label, do, text | run_id | pane}], at most MAX_ACTIONS."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("actions: a list of {label, do, ...}")
    if len(raw) > MAX_ACTIONS:
        raise ValueError("a block has at most %d buttons" % MAX_ACTIONS)
    out = []
    for a in raw:
        if not isinstance(a, dict):
            raise ValueError("an action is {label, do, ...}")
        do = str(a.get("do") or "")
        if do not in DOES:
            raise ValueError("an action's do is one of: %s" % ", ".join(DOES))
        label = _line(a.get("label"), MAX_LABEL, "a button's label") or {
            "queue": "Add to sequence", "ask": "Ask Kelvin", "run": "Open run", "pane": "Open"}[do]
        act: Dict[str, Any] = {"label": label, "do": do}
        if do in ("queue", "ask"):
            act["text"] = _text(a.get("text"), MAX_ACTION_TEXT, "an action's text")
            if not act["text"]:
                raise ValueError("a %s button needs its text (%s)" % (
                    do, "the scan's plan or a step" if do == "queue" else "what to ask"))
        elif do == "run":
            act["run_id"] = _line(a.get("run_id"), 80, "run_id")
            if not act["run_id"]:
                raise ValueError("a run button needs its run_id")
        else:
            act["pane"] = str(a.get("pane") or "")
            if act["pane"] not in PANES:
                raise ValueError("a pane button opens one of: %s" % ", ".join(PANES))
        out.append(act)
    return out


def clean(fields: Dict[str, Any], old: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A block's fields, checked (ValueError says what is wrong), merged over `old` when it changes one. Only the
    fields given change; None leaves a field as it was, "" (or []) empties it."""
    out = dict(old or {})
    if "kind" in fields and fields["kind"] is not None:
        kind = str(fields["kind"])
        if kind not in KINDS:
            raise ValueError("kind is one of: %s" % ", ".join(KINDS))
        out["kind"] = kind
    out.setdefault("kind", "note")
    if fields.get("title") is not None:
        out["title"] = _line(fields["title"], MAX_TITLE, "the title")
    if not out.get("title"):
        raise ValueError("a block needs a title")
    if fields.get("text") is not None:
        out["text"] = _text(fields["text"], MAX_TEXT, "the text")
    for name in ("value", "error"):
        if name in fields and fields[name] is not None:
            out[name] = _number_or_text(fields[name], name)
    if fields.get("unit") is not None:
        out["unit"] = _line(fields["unit"], 16, "the unit")
    if "items" in fields and fields["items"] is not None:
        out["items"] = _items(fields["items"], (old or {}).get("items"))
    if fields.get("run_id") is not None:
        out["run_id"] = _line(fields["run_id"], 80, "run_id")
    if fields.get("watch") is not None:
        watch = _line(fields["watch"], 80, "watch")
        if watch and not WATCH.match(watch):
            raise ValueError("watch is instrument.parameter, as lab_status names them (e.g. tc.T_sample)")
        out["watch"] = watch
    if "actions" in fields and fields["actions"] is not None:
        out["actions"] = _actions(fields["actions"])
    if fields.get("tone") is not None:
        tone = str(fields["tone"])
        if tone not in TONES:
            raise ValueError("tone is one of: ok, warn, bad (or empty)")
        out["tone"] = tone
    if out["kind"] == "watch" and not out.get("watch"):
        raise ValueError("a watch block names the reading it shows: watch=instrument.parameter")
    if out["kind"] == "check" and not out.get("items"):
        raise ValueError("a check block lists its items")
    for name in ("text", "unit", "run_id", "watch", "tone"):
        out.setdefault(name, "")
    out.setdefault("items", [])
    out.setdefault("actions", [])
    out.setdefault("value", None)
    out.setdefault("error", None)
    return out


class HomeBoard:
    """The person's Home in one file (home.json). `path`: the file, or a function giving it (it follows the person's
    data folder, which changes when someone else signs in). Thread-safe: each change reads, changes and writes the
    whole file under one lock."""

    def __init__(self, path: Union[Any, Callable[[], Any]]):
        self._path = path

    @property
    def path(self) -> Path:
        return Path(self._path() if callable(self._path) else self._path)

    # ------------------------------------------------------------------ the file

    def _read(self) -> Tuple[List[Dict[str, Any]], float]:
        path = self.path
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return [], 0.0
        except OSError as e:
            raise Unreadable("Kelvin could not read %s (%s)" % (path, e.strerror or e)) from None
        try:
            data = json.loads(raw.decode("utf-8"))
            blocks = data["blocks"]
            if not isinstance(blocks, list):
                raise ValueError("blocks is not a list")
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            aside = path.with_name("%s.broken-%s" % (path.name, time.strftime("%Y%m%d-%H%M%S")))
            try:
                os.replace(path, aside)
            except OSError:
                pass
            return [], 0.0
        good = [b for b in blocks if isinstance(b, dict) and isinstance(b.get("id"), str) and b.get("kind") in KINDS]
        return good, float(data.get("changed") or 0)

    def _write(self, blocks: List[Dict[str, Any]]) -> float:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        changed = time.time()
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"blocks": blocks, "changed": changed}, indent=1, ensure_ascii=False, default=str),
                       encoding="utf-8")
        os.replace(tmp, path)
        return changed

    @staticmethod
    def _order(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Pinned first, then the newest; of two changed in the same instant, the one written later."""
        ranked = sorted(enumerate(blocks), key=lambda ib: (not ib[1].get("pinned"),
                                                           -(ib[1].get("updated") or ib[1].get("created") or 0),
                                                           -ib[0]))
        return [b for _, b in ranked]

    @staticmethod
    def _find(blocks: List[Dict[str, Any]], ref: str) -> int:
        """The block a reference names: its id, or the key Kelvin gave it."""
        ref = str(ref or "").strip()
        for i, b in enumerate(blocks):
            if b["id"] == ref:
                return i
        for i, b in enumerate(blocks):
            if ref and b.get("key") == ref:
                return i
        raise KeyError("no block %r on Home" % ref)

    # ------------------------------------------------------------------ reading

    def view(self) -> Dict[str, Any]:
        """{blocks (pinned first, then the newest), changed}: what the Home page shows."""
        with _LOCK:
            blocks, changed = self._read()
        return {"blocks": self._order(blocks), "changed": changed}

    def blocks(self) -> List[Dict[str, Any]]:
        return self.view()["blocks"]

    def stamp(self) -> str:
        """What changes whenever Home does (the file's time and size), for the page's state poll: "" with no file."""
        try:
            st = self.path.stat()
        except OSError:
            return ""
        return "%d:%d" % (st.st_mtime_ns, st.st_size)

    # ------------------------------------------------------------------ changes

    def add(self, fields: Dict[str, Any], by: str = "kelvin", chat: Optional[str] = None
            ) -> Tuple[Dict[str, Any], bool]:
        """A new block, or (its key on Home already) that block changed: (block, added). by: "kelvin" or the
        person's name; chat: the chat it came from."""
        key = _line(fields.get("key"), MAX_KEY, "the key")
        with _LOCK:
            blocks, _ = self._read()
            if key:
                try:
                    i = self._find(blocks, key)
                except KeyError:
                    i = -1
                if i >= 0 and blocks[i].get("key") == key:
                    block = clean(fields, blocks[i])
                    block.update(updated=time.time(), by=by, chat=chat or blocks[i].get("chat"))
                    blocks[i] = block
                    self._write(blocks)
                    return block, False
            now = time.time()
            block = clean(fields)
            block.update(id="b-" + uuid.uuid4().hex[:8], key=key, pinned=False, by=by, chat=chat, created=now,
                         updated=now)
            blocks.append(block)
            unpinned = [b for b in self._order(blocks) if not b.get("pinned")]
            for old in unpinned[MAX_BLOCKS:]:
                blocks.remove(old)
            self._write(blocks)
            return block, True

    def update(self, ref: str, fields: Dict[str, Any], by: str = "kelvin") -> Dict[str, Any]:
        """The block `ref` names (id or key), its given fields changed."""
        with _LOCK:
            blocks, _ = self._read()
            i = self._find(blocks, ref)
            block = clean({k: v for k, v in fields.items() if k != "key"}, blocks[i])
            block.update(updated=time.time(), by=by)
            blocks[i] = block
            self._write(blocks)
            return block

    def remove(self, ref: str) -> Dict[str, Any]:
        with _LOCK:
            blocks, _ = self._read()
            block = blocks.pop(self._find(blocks, ref))
            self._write(blocks)
            return block

    def tick(self, ref: str, index: int, done: bool) -> Dict[str, Any]:
        """A checklist's item ticked (or not) by a person."""
        with _LOCK:
            blocks, _ = self._read()
            block = blocks[self._find(blocks, ref)]
            items = block.get("items") or []
            if not 0 <= int(index) < len(items):
                raise KeyError("no item %s in %r" % (index, block["title"]))
            items[int(index)]["done"] = bool(done)
            self._write(blocks)
            return block

    def pin(self, ref: str, pinned: bool) -> Dict[str, Any]:
        with _LOCK:
            blocks, _ = self._read()
            block = blocks[self._find(blocks, ref)]
            block["pinned"] = bool(pinned)
            self._write(blocks)
            return block

    def clear(self) -> int:
        """Every unpinned block gone; how many."""
        with _LOCK:
            blocks, _ = self._read()
            kept = [b for b in blocks if b.get("pinned")]
            if len(kept) != len(blocks):
                self._write(kept)
            return len(blocks) - len(kept)


def describe(block: Dict[str, Any]) -> str:
    """A block in one line, for Kelvin's tools: "b-1a2b3c4d result 'Peak position' 2.41 ± 0.02 V (key peak)"."""
    parts = [block["id"], block["kind"], repr(block.get("title") or "")]
    if block.get("value") is not None:
        value = "%s" % block["value"]
        if block.get("error") not in (None, ""):
            value += " ± %s" % block["error"]
        parts.append(value + (" " + block["unit"] if block.get("unit") else ""))
    if block.get("watch"):
        parts.append("watch " + block["watch"])
    if block.get("items"):
        parts.append("%d/%d ticked" % (sum(1 for i in block["items"] if i.get("done")), len(block["items"])))
    if block.get("pinned"):
        parts.append("pinned")
    if block.get("key"):
        parts.append("(key %s)" % block["key"])
    if block.get("by") and block["by"] != "kelvin":
        parts.append("by " + block["by"])
    return " ".join(parts)
