"""What a person does to the sequence or to Home in the portal, told to Kelvin's chats (2026-11-02): one line per
change ("You skipped #3 gate_sweep"), so the AI that queued a scan or suggested a block knows what became of it.

Who hears it, as the queue and Home are each person's own (their data folder) and so are their chats:
- a change to one block of the sequence: the chat that queued it (the queue files each item under its chat);
- a change to the whole sequence (start, pause, stop, load, clear): the chat that last started it, else the chats
  whose blocks wait in it;
- a change to a block on Home: the chat that put it there;
- a change nobody's chat made (the person's own blocks): no chat at once.
Every chat of the person's also reads, with its next message, the changes since its last one that it has not seen.

How it reaches a chat: one answering now reads it at its next step (beside what the person writes meanwhile); one
waiting with wake_me for the run or queue is woken by the queue itself when a stop or a pause ends it; any other gets
the line in its thread (role "event"), which goes to the model with the chat's next message as a <lab_events> block.
Nothing calls a model for a change unless the person switched on Settings → "Kelvin answers sequence changes": then a
stop or a failure continues each chat it concerns at once (wakeups.py, within that chat's daily wake-ups).

    _events.jsonl, in the person's chat folder: {"id", "at", "text", "about": "sequence" | "home", "chats": [ids]}
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

FILE = "_events.jsonl"
KEEP = 500          # lines kept in _events.jsonl, the newest
DIGEST = 12         # changes at most in one <lab_events> block
DIGEST_S = 24 * 3600   # and none older than a day
PICKED = ("running", "starting")   # the block the sequence is on: starting while its checks run (ScanQueue.pick)
_LOCK = threading.Lock()


def block(events: List[str]) -> str:
    """The changes as the model reads them, in front of the chat's next message."""
    return "<lab_events>\n%s\n</lab_events>" % "\n".join("- " + e for e in events)


# ---------------------------------------------------------------------- a person's change to the sequence, in words

def _find(view: Dict[str, Any], ref: Any) -> Any:
    """(position, item) of an item named by its id, its number or #number, in a queue view; (None, None)."""
    items = view.get("items") or []
    key = str(ref if ref is not None else "").strip().lstrip("#")
    for n, it in enumerate(items, 1):
        if it.get("id") == key:
            return n, it
    if key.isdigit() and 1 <= int(key) <= len(items):
        return int(key), items[int(key) - 1]
    return None, None


def _label(n: Any, it: Dict[str, Any]) -> str:
    return "#%s %s" % (n, it.get("name") or it.get("kind") or "block")


def _changed(old: Dict[str, Any], new: Dict[str, Any]) -> str:
    """What changed between two values of a block's settings: "VMAX 0.4 → 0.6, POINTS 21 → 41"."""
    keys = [k for k in list(old) + [k for k in new if k not in old] if old.get(k) != new.get(k)]
    return ", ".join("%s %s → %s" % (k, old.get(k, "—"), new.get(k, "—")) for k in keys[:4]) + \
        (" and %d more" % (len(keys) - 4) if len(keys) > 4 else "")


def queue_said(op: str, body: Dict[str, Any], before: Dict[str, Any], after: Dict[str, Any],
               started_by: Optional[str] = None) -> Optional[tuple]:
    """A person's change to the sequence (POST /api/queue {op, ...}), in one line, with the chats it concerns and
    whether it is urgent: (text, chats, urgent), or None when it changes nothing worth telling (a saved copy, the
    failure rule)."""
    n, it = _find(before, body.get("item"))
    owner = [it.get("chat")] if it else []
    working = [i.get("chat") for i in before.get("items") or [] if i.get("status") in ("waiting", "running")]
    conductor = [started_by] + working
    new = [x for x in after.get("items") or [] if x.get("id") not in {i.get("id") for i in before.get("items") or []}]
    at = {x.get("id"): k for k, x in enumerate(after.get("items") or [], 1)}
    if op == "add" and new:
        return "You added %s." % _label(at[new[0]["id"]], new[0]), [], False
    if op in ("edit", "params") and it:
        n2, now = _find(after, it.get("id"))
        what = ""
        if now and it.get("kind") == "script":
            what = _changed(it.get("params") or {}, now.get("params") or {})
        elif now and it.get("kind") == "step":
            what = _changed(dict(it.get("set") or {}, wait=it.get("wait")), dict(now.get("set") or {}, wait=now.get("wait")))
        return "You changed %s%s." % (_label(n, it), ": " + what if what else ""), owner, False
    if op == "move" and it:
        n2, _ = _find(after, it.get("id"))
        return "You moved %s to #%s." % (_label(n, it), n2), owner, False
    if op == "remove" and it:
        return "You removed %s." % _label(n, it), owner, False
    if op == "again" and it:
        return "You put %s back in line." % _label(n, it), owner, False
    if op == "skip" and it:
        running = it.get("status") in PICKED
        how = "ran" if it.get("status") == "running" else "was starting"
        return ("You skipped %s while it %s; the sequence went on." % (_label(n, it), how) if running else
                "You skipped %s." % _label(n, it)), owner + ([started_by] if running else []), False
    if op == "duplicate" and it and new:
        return "You copied %s as #%s." % (_label(n, it), at[new[0]["id"]]), owner, False
    if op == "then_pause" and it:
        return ("The sequence now pauses after %s." if body.get("value", True) else
                "The sequence no longer pauses after %s.") % _label(n, it), owner, False
    if op == "clear":
        gone = [i for i in before.get("items") or [] if i.get("id") not in at]
        if gone:
            return "You cleared %d finished block%s." % (len(gone), "" if len(gone) == 1 else "s"), \
                [i.get("chat") for i in gone], False
    if op == "start":
        waiting = [i for i in before.get("items") or [] if i.get("status") == "waiting"]
        return "You started the sequence%s." % (" (%d blocks waiting)" % len(waiting) if waiting else ""), working, False
    if op == "pause":   # want "pausing": a second press of Pause, which took the pause back
        running = next((i for i in before.get("items") or [] if i.get("status") in PICKED), None)
        after_it = " after %s" % (running.get("name") or "the running block") if running else ""
        if body.get("want") == "pausing":
            return "You took back the pause%s; the sequence goes on." % after_it, conductor, False
        return "You paused the sequence%s." % after_it, conductor, False
    if op == "load" and new:
        return "You loaded the saved sequence '%s' (%d block%s)." % (body.get("name"), len(new),
                                                                     "" if len(new) == 1 else "s"), [], False
    if op == "rename" and after.get("name") != before.get("name"):
        return "You renamed the sequence '%s'." % after.get("name"), conductor, False
    if op == "ladder" and new:
        src = [_find(before, ref)[1] for ref in body.get("items") or []]
        return "You added a sweep of %s over %d values (%d blocks)." % (
            body.get("key"), len(body.get("values") or []), len(new)), [s.get("chat") for s in src if s], False
    return None


def stop_said(before: Dict[str, Any], started_by: Optional[str] = None) -> Optional[tuple]:
    """A person's Stop while the sequence ran: (text, chats, urgent), or None when it wasn't running."""
    if before.get("state") not in ("running", "pausing"):
        return None
    n, it = next(((k, i) for k, i in enumerate(before.get("items") or [], 1) if i.get("status") in PICKED),
                 (None, None))
    working = [i.get("chat") for i in before.get("items") or [] if i.get("status") in ("waiting",) + PICKED]
    return ("You stopped the sequence%s; it paused." % (" at %s" % _label(n, it) if it else ""),
            [started_by] + working, True)


class Notices:
    def __init__(self, agent: Any):
        self.agent = agent
        self.pending: Dict[str, List[Dict[str, Any]]] = {}   # for chats answering now: read at their next step

    # ------------------------------------------------------------------ the person's log

    def _file(self) -> Path:
        return Path(str(getattr(self.agent.store, "root", "."))) / FILE

    def _append(self, event: Dict[str, Any]) -> None:
        path = self._file()
        with _LOCK:
            try:
                lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            except OSError:
                lines = []
            lines = (lines + [json.dumps(event, ensure_ascii=False)])[-KEEP:]
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
                tmp.replace(path)
            except OSError:
                pass   # the chats it concerns still get it; only the others' digest misses it

    def recent(self, since: float) -> List[Dict[str, Any]]:
        """The person's changes after `since`, oldest first."""
        try:
            lines = self._file().read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and float(e.get("at") or 0) > since:
                out.append(e)
        return out

    # ------------------------------------------------------------------ a change

    def tell(self, text: str, chats: Iterable[Optional[str]] = (), about: str = "sequence",
             urgent: bool = False) -> Dict[str, Any]:
        """A person's change, in one line, for the chats it concerns (see the module's note). urgent: a stop or a
        failure, which continues those chats at once when the person asked Kelvin to answer such changes."""
        event = {"id": "e-" + uuid.uuid4().hex[:8], "at": time.time(), "text": str(text).strip(), "about": about,
                 "chats": sorted({str(c) for c in chats if c})}
        self._append(event)
        for chat_id in event["chats"]:
            if self.agent.answering(chat_id):
                self.pending.setdefault(chat_id, []).append(event)
                continue
            self._into_thread(chat_id, event)
            if urgent and self._react():
                self.agent.waker.soon(chat_id, event["text"])
        return event

    def _react(self) -> bool:
        settings = getattr(self.agent, "settings", None)
        return bool(settings is not None and settings.get("sequence_react") == "on")

    def _into_thread(self, chat_id: str, event: Dict[str, Any]) -> None:
        """The line in a chat that isn't answering: shown in its thread, read with its next message."""
        try:
            chat = self.agent.store.load(chat_id)
        except (KeyError, OSError, ValueError):
            return   # gone: its digest is gone with it
        if chat.get("incognito"):
            return
        chat["messages"].append(self.line(event))
        try:
            self.agent.store.save(chat)
        except OSError:
            pass

    @staticmethod
    def line(event: Dict[str, Any], read: bool = False) -> Dict[str, Any]:
        out = {"role": "event", "id": event["id"], "text": event["text"], "at": event["at"],
               "about": event.get("about", "sequence")}
        if read:
            out["read"] = True
        return out

    # ------------------------------------------------------------------ into the model's view

    def at_turn(self, chat: Dict[str, Any], message: Dict[str, Any]) -> None:
        """A turn starts: the lines waiting in this chat, and the person's changes since its last message that it
        hasn't seen, go with the new message (as `events`, kept with it so the turn replays the same)."""
        if chat.get("incognito"):
            return
        events: List[str] = []
        seen = set()
        for m in chat["messages"]:
            if m.get("role") == "event":
                seen.add(m.get("id"))
                if not m.get("read"):
                    events.append(m["text"])
                    m["read"] = True
            elif m.get("role") == "user":
                seen.update(m.get("event_ids") or [])
        last = max([float(m.get("at") or 0) for m in chat["messages"] if m.get("role") == "user"] +
                   [float(chat.get("created") or 0)])
        since = max(last, time.time() - DIGEST_S)
        others = [e for e in self.recent(since) if e.get("id") not in seen]
        ids = [e["id"] for e in others]
        events += [e["text"] for e in others]
        message["at"] = time.time()
        if events:
            message["events"] = events[-DIGEST:]
            message["event_ids"] = ids

    def after_turn(self, chat_id: str) -> None:
        """A chat's answer ended: what was told to it after its last step goes into its thread, read with its next
        message (its turn saved the chat already, so nothing else writes it now)."""
        for event in self.pending.pop(chat_id, []):
            self._into_thread(chat_id, event)

    async def at_step(self, chat: Dict[str, Any], emit: Any) -> None:
        """A chat answering now, before its next step: the changes meanwhile, shown and read now."""
        waiting = self.pending.pop(chat["id"], [])
        if not waiting or chat.get("incognito"):
            return
        for e in waiting:
            chat["messages"].append(self.line(e, read=True))
            await emit({"type": "note", "text": e["text"]})
        chat["messages"].append({"role": "user", "text": "", "events": [e["text"] for e in waiting],
                                 "event_ids": [e["id"] for e in waiting], "notice": True, "at": time.time()})
