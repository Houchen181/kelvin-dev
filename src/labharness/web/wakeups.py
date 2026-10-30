"""Waiting without freezing every chat: a chat asks to be continued later (wake_me), its turn ends, and Kelvin
continues it when the time comes, a reading holds a condition, or the lab's run or queue finishes.

A wait inside a turn holds the lab (it has one set of instruments: one chat's call works it at a time), so a long
wait would keep every other chat off the lab. With wake_me, the model ends its answer instead; meanwhile other chats
use the lab. When the moment comes and the chat isn't answering, Kelvin starts a turn in that chat with a <wake>
message saying why, and tells the person. A chat may be woken at most WAKES_PER_DAY times a day, and only while
its owner's chats are the ones open (on a shared computer, another person may be signed in meanwhile). Wake-ups are
kept in the person's chat folder (_wakes.json), so a restart (an update, a crash) during a long wait loses none."""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..conditions import Condition, parse_condition

TICK_S = 2.0
WAKES_PER_DAY = 12
MAX_HOURS = 48.0

WAKE_ME = {
    "name": "wake_me",
    "description": "Continue this chat later instead of waiting in it: after calling this, end your answer saying "
                   "what you are waiting for. Kelvin continues the chat when the time comes (after_minutes), when a "
                   "reading holds a condition (until, e.g. 'tc.T_sample < 4.3', for hold_minutes), or when the lab's "
                   "run or queue finishes (run_ends); meanwhile the lab is free for other chats. Use it for any wait "
                   "longer than "
                   "a minute: a cool-down, a temperature to settle, a long queue. note: what to do then, for "
                   "yourself. One wake-up per chat; a new one replaces it.",
    "schema": {"type": "object", "required": ["note"], "properties": {
        "after_minutes": {"type": "number", "minimum": 0.5, "description": "Continue after this many minutes"},
        "until": {"type": "string", "description": "A condition on a reading: instrument.parameter < or > a value"},
        "hold_minutes": {"type": "number", "minimum": 0, "description": "How long the condition must hold (0)"},
        "run_ends": {"type": "boolean", "description": "Continue when the run or queue now running finishes"},
        "within_hours": {"type": "number", "minimum": 0.1, "description": "Give up and continue anyway after "
                                                                           "this long (12)"},
        "note": {"type": "string", "description": "What to do when continuing"}}},
}


@dataclass
class Wake:
    chat_id: str
    kind: str                       # time | until | run
    note: str
    text: str                       # what it waits for, in words
    due: float                      # when to continue (time), or to give up (until, run)
    condition: Optional[Condition] = None
    hold_s: float = 0.0
    since: Optional[float] = None   # when the condition began to hold
    created: float = 0.0
    root: str = ""                  # whose chats (the chat folder): woken only while that person's are open
    restored: bool = False          # kept over a restart of Kelvin

    def kept(self) -> Dict[str, Any]:
        """As _wakes.json keeps it: wall-clock times, the condition as text."""
        now, wall = time.monotonic(), time.time()
        return {"kind": self.kind, "note": self.note, "text": self.text, "due_at": wall + (self.due - now),
                "until": self.condition.text if self.condition else "", "hold_s": self.hold_s,
                "created_at": wall - (now - self.created)}


class Waker:
    def __init__(self, agent: Any):
        self.agent = agent
        self.pending: Dict[str, Wake] = {}
        self.history: Dict[str, List[float]] = {}   # when each chat was woken, for the daily limit
        self.last: Optional[Dict[str, Any]] = None   # the latest wake-up, for the portal
        self.loaded: Optional[str] = None            # the chat folder whose kept wake-ups are read

    # -------------------------------------------------------------- asked for by a chat

    def add(self, chat_id: str, args: Any) -> Tuple[str, bool]:
        args = args if isinstance(args, dict) else {}
        note = str(args.get("note") or "").strip()[:500]
        if not note:
            return "say in note what to do when continuing", True
        recent = [t for t in self.history.get(chat_id, []) if time.time() - t < 86400]
        if len(recent) >= WAKES_PER_DAY:
            return ("this chat has been continued %d times today: ask the person before going on" % len(recent)), True
        now = time.monotonic()
        try:
            within = min(MAX_HOURS, float(args.get("within_hours") or 12.0)) * 3600
            if args.get("until"):
                cond = parse_condition(str(args["until"]))
                self.agent.runtime.lab.resolve(cond.key)   # KeyError: no such reading
                hold = max(0.0, float(args.get("hold_minutes") or 0)) * 60
                wake = Wake(chat_id, "until", note, "%s%s" % (cond.text, " for %g min" % (hold / 60) if hold else ""),
                            now + within, cond, hold)
            elif args.get("run_ends"):
                if not self._running():
                    return "nothing is running now: start the run or queue first", True
                wake = Wake(chat_id, "run", note, "the run or queue finishes", now + within)
            elif args.get("after_minutes"):
                minutes = min(MAX_HOURS * 60, max(0.5, float(args["after_minutes"])))
                wake = Wake(chat_id, "time", note, "%s have passed" % _dur(minutes * 60), now + minutes * 60)
            else:
                return "say when: after_minutes, until (a condition), or run_ends", True
        except (KeyError, ValueError, TypeError) as e:
            return "not set: %s" % (e.args[0] if e.args else e), True
        wake.created, wake.root = now, self._root()
        replaced = chat_id in self.pending
        self.pending[chat_id] = wake
        self._keep()
        return ("Kelvin continues this chat when %s (at most %s from now)%s. End your answer now, saying what you are "
                "waiting for." % (wake.text, _dur(wake.due - now), "; the earlier wake-up is replaced"
                                  if replaced else "")), False

    def cancel(self, chat_id: str) -> bool:
        gone = self.pending.pop(chat_id, None) is not None
        if gone:
            self._keep()
        return gone

    def view(self) -> List[Dict[str, Any]]:
        """The signed-in person's wake-ups."""
        now, root = time.monotonic(), self._root()
        return [{"chat_id": w.chat_id, "text": w.text, "note": w.note, "left_s": max(0, int(w.due - now))}
                for w in self.pending.values() if w.root == root]

    def latest(self) -> Optional[Dict[str, Any]]:
        """The latest wake-up, if it was in the signed-in person's chats."""
        last = self.last
        return {k: v for k, v in last.items() if k != "root"} if last and last.get("root") == self._root() else None

    def _root(self) -> str:
        return str(getattr(self.agent.store, "root", ""))

    # -------------------------------------------------------------- kept over restarts

    def _file(self) -> Path:
        return Path(self._root()) / "_wakes.json"

    def _keep(self) -> None:
        """Write the signed-in person's wake-ups to their chat folder."""
        root = self._root()
        mine = {w.chat_id: w.kept() for w in self.pending.values() if w.root == root}
        path = self._file()
        try:
            if not mine:
                if path.exists():
                    path.unlink()
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(mine), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass   # kept in memory all the same

    def _restore(self) -> None:
        """The wake-ups kept in the signed-in person's chat folder, once per folder (at start, at each sign-in)."""
        root = self._root()
        if root == self.loaded:
            return
        self.loaded = root
        try:
            kept = json.loads(self._file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        now, wall = time.monotonic(), time.time()
        for chat_id, k in (kept if isinstance(kept, dict) else {}).items():
            try:
                cond = parse_condition(k["until"]) if k.get("until") else None
                w = Wake(chat_id, str(k["kind"]), str(k["note"]), str(k["text"]), now + float(k["due_at"]) - wall,
                         cond, float(k.get("hold_s") or 0), created=now - (wall - float(k["created_at"])), root=root,
                         restored=True)
            except (KeyError, TypeError, ValueError):
                continue
            self.pending.setdefault(chat_id, w)

    # -------------------------------------------------------------- watching

    def _running(self) -> bool:
        rt = self.agent.runtime
        live = rt.live or {}
        return live.get("status") == "running" or rt.queue.state in ("running", "pausing")

    def _check(self, w: Wake) -> Optional[str]:
        """Why the chat continues now, or None to keep waiting."""
        now = time.monotonic()
        if w.kind == "time":
            return "%s" % w.text if now >= w.due else None
        if w.kind == "run":
            if w.restored and not self._running():
                return "Kelvin restarted while it waited for the run or queue, which no longer runs: see how it ended"
            if not self._running():
                return "the run or queue has finished"
            return "it has waited %s; the run or queue is still going" % _dur(now - w.created) if now >= w.due else None
        lab = self.agent.runtime.lab
        try:   # never a command between a run's own: the run's latest measurement of it, or wait
            value, why = self.agent.runtime.reading_now(w.condition.key)
            if why is not None:
                return None if now < w.due else "it has waited %s, and %s isn't read now (%s)" % (
                    _dur(now - w.created), w.condition.key, why)
            ok, value = w.condition.check(lambda instr, param: value)
        except Exception as e:   # a reading failed: say so rather than wait forever
            return "%s could not be read (%s)" % (w.condition.key, str(e).splitlines()[0] if str(e) else e)
        shown = lab.resolve(w.condition.key)[1].show(value)
        if ok:
            w.since = w.since if w.since is not None else now
            if now - w.since >= w.hold_s:
                return "%s is reached (now %s)" % (w.text, shown)
        else:
            w.since = None
        if now >= w.due:
            return "it has waited %s, and %s is not reached (now %s)" % (_dur(now - w.created), w.text, shown)
        return None

    async def run(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    async def tick(self) -> None:
        self._restore()
        for chat_id, w in list(self.pending.items()):
            if self.agent.answering(chat_id):   # the chat is answering now: wake it when that ends
                continue
            if w.root != self._root():   # another person is signed in: wait for the owner, a while
                if time.monotonic() - w.created > MAX_HOURS * 3600:
                    self.pending.pop(chat_id, None)
                continue
            why = await asyncio.to_thread(self._check, w)
            if why is None:
                continue
            self.pending.pop(chat_id, None)
            self._keep()
            await self._wake(w, why)

    async def _wake(self, w: Wake, why: str) -> None:
        from .agent import Busy
        agent = self.agent
        try:
            chat = agent.store.load(w.chat_id)
        except KeyError:
            return   # the chat is gone
        model = str(chat.get("model") or "")
        try:
            provider = agent.claim(w.chat_id, model)
        except Busy:
            self.pending.setdefault(w.chat_id, w)   # try again on the next tick
            self._keep()
            return
        except (KeyError, ValueError):
            return   # its model is no longer there
        self.history.setdefault(w.chat_id, []).append(time.time())
        self.last = {"chat_id": w.chat_id, "at": time.time(), "why": why, "title": chat.get("title"), "root": w.root}
        agent.runtime.tell("run", "Kelvin continued “%s”" % (chat.get("title") or "a chat"), why,
                           agent._stays_here(chat))   # an unshared chat's title never goes through the hub
        why = why[0].upper() + why[1:]
        text = "<wake>%s. You asked to continue then: %s</wake>" % (why, w.note)

        async def keep(event: Dict[str, Any]) -> None:   # nobody is watching: the chat keeps what happens
            pass
        await agent.run_turn(w.chat_id, text, model, provider, keep, wake="%s. Next: %s" % (why, w.note))
        self.last = dict(self.last, done=time.time())


def _dur(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return "%d s" % seconds
    if seconds < 5400:
        return "%d min" % round(seconds / 60)
    return "%.1f h" % (seconds / 3600)
