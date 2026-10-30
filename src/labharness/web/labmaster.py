"""The sequence engine's door for the rest of Kelvin, and the Home page's cards.

Since v1-A (V1_SPEC.md) labMaster's engine runs INSIDE Kelvin's process: there is no 8770 port and
nothing here talks HTTP. Every function keeps the name and the answer shape it had in v0 (the
portal's routes and the MCP tools call them):

    ping/state/palette/scripts/saved/history           reads
    seq_add/seq_update/seq_delete/seq_move/            the live sequence (writes)
    seq_duplicate/seq_clear/seq_rename
    saved_save/saved_load/saved_delete                 saved sequences
    control/safe_stop                                  the engine's run control
    import_script                                      a .py into the function library

Nothing raises: a call that cannot be carried out answers {"ok": False, "error": "<what went
wrong>"} - the reads with their empty list, the writes with "state": None and "message": "". Each
one is one call into `labharness.sequence.service`, the process-wide SequenceService.

What is new here (v1-A), for the MCP tools and the Sequence page:
    merged_runs(limit=20)     the engine's runs + Kelvin's record of the AI's measurements
    kelvin_runs()             Kelvin's own record
    record_kelvin_run(...)    an AI measurement onto that record (fail-soft: never breaks the work)
    ensure_sequence_name(...) name the sequence when it has none (an AI-chosen name)
    armed()                   may the AI start a run? (read only: the person sets it in the portal)

HomeStore is Kelvin's own: the cards the AI puts on the Home page, kept in ~/Kelvin/home.json
(atomic write, UTF-8). snapshot() turns the engine's state into the Home page's five tiles.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..sequence import service as sequence_service

CARD_KINDS = ("note", "metric", "checklist", "link", "sequence")
DEFAULT_COLOR = "#5D9B86"                 # the colour "+ New function" starts a function with
IN_PROCESS = "in-process"
_HOME_LOCK = threading.RLock()            # one Home page, however many stores/tools reach it


def _service() -> Any:
    """The process-wide sequence service (created by the portal, or default ~/Kelvin)."""
    return sequence_service.service()


def panel_url() -> str:
    """Where the sequence engine is: in Kelvin's own process (the 8770 port is gone)."""
    return IN_PROCESS


# ---------------------------------------------------------------- the engine's API (never raises)

def ping() -> Dict[str, Any]:
    """Is the engine up, is it simulated, which version? {"ok", "sim", "url", "version", "error"}."""
    return _service().ping()


def state() -> Dict[str, Any]:
    """{"ok", "url", "error", "state"}: the engine's snapshot (None when it cannot be read)."""
    return _service().state()


def palette() -> Dict[str, Any]:
    """{"ok", "error", "palette": [...], "rev": str} - the function library and its revision."""
    return _service().palette()


def scripts() -> Dict[str, Any]:
    """{"ok", "error", "scripts": [{path, name, has_main, main}]} - the .py files a function may run."""
    return _service().scripts()


def saved() -> Dict[str, Any]:
    """{"ok", "error", "saved": [{id, name, saved, n_blocks}]} - the saved sequences."""
    return _service().saved()


def history(limit: int = 5) -> Dict[str, Any]:
    """{"ok", "error", "runs": [...]} - the engine's own finished runs, newest first."""
    return _service().history(limit)


# ---------------------------------------------------------------- the sequence (writes)

def seq_add(block: Any = "", block_id: str = "", params: Optional[Dict[str, Any]] = None,
            index: Optional[int] = None) -> Dict[str, Any]:
    """Add one function to the sequence. The engine adds by palette id: block may be that id or the
    function's name (looked up in the library), or a palette entry, whole."""
    return _service().seq_add(block=block, block_id=block_id, params=params, index=index)


def seq_update(uid: str, params: Optional[Dict[str, Any]] = None, name: str = "",
               reset: bool = False) -> Dict[str, Any]:
    """Change one block's parameters or name, or reset it to pending (reset=True)."""
    return _service().seq_update(uid, params=params, name=name, reset=reset)


def seq_delete(uid: str) -> Dict[str, Any]:
    """Remove one block from the sequence."""
    return _service().seq_delete(uid)


def seq_move(uid: str, index: int) -> Dict[str, Any]:
    """Move one block to a new 0-based position."""
    return _service().seq_move(uid, index)


def seq_duplicate(uid: str) -> Dict[str, Any]:
    """Copy one block right after itself."""
    return _service().seq_duplicate(uid)


def seq_clear(keep_done: bool = False) -> Dict[str, Any]:
    """Empty the sequence (keep_done=True keeps the blocks that finished)."""
    return _service().seq_clear(keep_done=keep_done)


def seq_rename(name: str) -> Dict[str, Any]:
    """Name the live sequence (saved sequences keep their own names)."""
    return _service().seq_rename(name)


CONTROL_ACTIONS = ("start", "pause", "pause_rep", "next_rep", "skip", "stop", "reset", "end_wait",
                   "set_wait")


def control(action: str, **kw: Any) -> Dict[str, Any]:
    """The engine's run control: action plus whatever it takes (want, uid, minutes, failed,
    failed_uids, reason). stop/skip/reset and the like are the caller's to confirm: this only
    sends what it is given. safe_stop goes to safe_stop(), as the panel's page does."""
    return _service().control(action, **kw)


def safe_stop(reason: str = "") -> Dict[str, Any]:
    """Stop whatever runs, then run the configured safe-stop script."""
    return _service().safe_stop(reason)


# ---------------------------------------------------------------- saved sequences (writes)

def saved_save(name: str = "", overwrite: bool = False) -> Dict[str, Any]:
    """Save the live sequence under a name (overwrite=True replaces one saved with that name)."""
    return _service().saved_save(name=name, overwrite=overwrite)


def saved_load(item_id: str = "", name: str = "", append: bool = False,
               replace: bool = False) -> Dict[str, Any]:
    """Load a saved sequence into the live one: append=True adds it after what is there,
    replace=True discards the live blocks."""
    return _service().saved_load(item_id=item_id, name=name, append=append, replace=replace)


def saved_delete(item_id: str = "", name: str = "") -> Dict[str, Any]:
    """Delete a saved sequence (by id, or by a name no other saved sequence has)."""
    return _service().saved_delete(item_id=item_id, name=name)


# ---------------------------------------------------------------- importing a .py into the library

def import_script(path: str, name: str = "", kind: str = "script") -> Dict[str, Any]:
    """Put a script from the engine's search dirs into the function library, as "+ New function"
    does. Returns {"ok", "error", "entry", "palette", "message"}; entry is the new function."""
    return _service().import_script(path, name, kind)


# ---------------------------------------------------------------- v1-A: Kelvin's own runs and names

def merged_runs(limit: int = 20) -> Dict[str, Any]:
    """{"ok", "runs": [...]}: the engine's runs (source "panel") and Kelvin's own records of the
    AI's measurements (source "kelvin"), newest first. The Sequence page's History."""
    return _service().merged_runs(limit)


def kelvin_runs() -> List[Dict[str, Any]]:
    """Kelvin's own record of the AI's measurements, oldest first."""
    return _service().kelvin_runs()


def record_kelvin_run(name: str, kind: str = "measurement", status: str = "done", detail: str = "",
                      run_id: str = "", started: Optional[float] = None) -> Dict[str, Any]:
    """Record one AI measurement for the Sequence page's History. Fail-soft: if it cannot be
    recorded, the answer says so and the measurement itself is not affected."""
    return _service().record_kelvin_run(name, kind=kind, status=status, detail=detail,
                                        run_id=run_id, started=started)


def ensure_sequence_name(name: str) -> Dict[str, Any]:
    """Give the live sequence the AI's name when it has none (or only the default date-time name).
    A name a person set is never overwritten."""
    return _service().ensure_sequence_name(name)


def armed() -> bool:
    """May Kelvin's AI start a run without asking? Read-only here: only a person's POST
    /api/labmaster/armed changes it."""
    return _service().armed()


# ---------------------------------------------------------------- the Home page's cards (Kelvin's own)

def _now_iso() -> str:
    """The time as ISO 8601 with this PC's offset (a page shows it in the reader's own day)."""
    try:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    except (OSError, ValueError):
        return datetime.now().isoformat(timespec="seconds")


def _new_card_id() -> str:
    return "card_" + uuid.uuid4().hex[:12]


class HomeStore:
    """The cards on Kelvin's Home page, kept in one JSON file (~/Kelvin/home.json by default).

    The AI adds to them through Kelvin's MCP tools; a person deletes one in the page. Every write is
    read-modify-write under one lock and lands whole or not at all (a temporary file then os.replace),
    so a crash mid-write never leaves the page a half card."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else Path.home() / "Kelvin" / "home.json"

    # -- reading and cleaning

    def cards(self) -> List[Dict[str, Any]]:
        """The cards as stored (oldest first); [] when the file is not there or cannot be read."""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        if isinstance(data, dict):
            data = data.get("cards")
        if not isinstance(data, list):
            return []
        return [self._clean(card) for card in data if isinstance(card, dict)]

    @staticmethod
    def _clean(card: Dict[str, Any]) -> Dict[str, Any]:
        """One card with every field the page draws, and an id, a time and a source when it has none.

        The fields are cut to a size a page can draw without reading next week's file: the card may come
        from a model, and one runaway answer must not put a megabyte into home.json."""
        c = card if isinstance(card, dict) else {}
        kind = str(c.get("kind") or "note").strip().lower()
        if kind not in CARD_KINDS:
            kind = "note"
        source = str(c.get("source") or "ai").strip().lower()
        if source not in ("ai", "person"):
            source = "ai"
        items = c.get("items")
        if isinstance(items, list):
            items = [i for i in items if isinstance(i, (str, int, float, bool, dict))][:100]
        else:
            items = []
        return {"id": str(c.get("id") or _new_card_id()), "kind": kind,
                "title": str(c.get("title") or "").strip()[:200], "text": str(c.get("text") or "")[:20000],
                "items": items, "url": str(c.get("url") or "").strip()[:2000],
                "detail": str(c.get("detail") or "")[:2000], "created": str(c.get("created") or _now_iso()),
                "source": source}

    # -- writing

    def _write(self, cards: List[Dict[str, Any]]) -> None:
        """The whole file, atomically: a temporary file in the same folder, then os.replace."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(".%s.%s.tmp" % (self.path.name, uuid.uuid4().hex[:8]))
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"cards": cards, "updated_at": _now_iso()}, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def add(self, card: Dict[str, Any]) -> Dict[str, Any]:
        """Add one card (id, created and source filled in), as stored."""
        with _HOME_LOCK:
            cards = self.cards()
            new = self._clean(card)
            cards.append(new)
            self._write(cards)
            return new

    def update(self, card_id: str, **fields: Any) -> Dict[str, Any]:
        """Change the fields given on one card (id and created keep their own), as stored."""
        card_id = str(card_id or "")
        with _HOME_LOCK:
            cards = self.cards()
            for i, card in enumerate(cards):
                if str(card.get("id")) != card_id:
                    continue
                merged = dict(card)
                for key, value in fields.items():
                    if key in ("kind", "title", "text", "items", "url", "detail", "source"):
                        merged[key] = value
                new = self._clean(merged)
                new["id"], new["created"] = card_id, str(card.get("created") or new["created"])
                cards[i] = new
                self._write(cards)
                return new
        raise KeyError(card_id)

    def remove(self, card_id: str) -> Dict[str, Any]:
        """Remove one card: {"ok": True, "id"}; KeyError when there is no such card."""
        card_id = str(card_id or "")
        with _HOME_LOCK:
            cards = self.cards()
            keep = [c for c in cards if str(c.get("id")) != card_id]
            if len(keep) == len(cards):
                raise KeyError(card_id)
            self._write(keep)
        return {"ok": True, "id": card_id}


# ---------------------------------------------------------------- the Home page

def _greeting(panel: Dict[str, Any]) -> str:
    hour = datetime.now().hour
    part = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    if panel.get("ok"):
        return part + " - the sequence engine is running in Kelvin."
    return part + " - the sequence engine is not available%s." % (
        (" (" + str(panel.get("error")) + ")") if panel.get("error") else "")


def _tile(tile_id: str, title: str, value: str, detail: str, tone: str) -> Dict[str, Any]:
    return {"id": tile_id, "title": title, "value": value, "detail": detail, "tone": tone}


def _down_tiles() -> List[Dict[str, Any]]:
    """The five tiles with the engine unavailable: the page still works, and says why nothing is live."""
    why = "the sequence engine is not available"
    return [_tile("measurement", "Measurement", "-", why, "down"),
            _tile("history", "History", "-", why, "down"),
            _tile("saved", "Saved sequences", "-", why, "down"),
            _tile("functions", "Functions", "-", why, "down"),
            _tile("panel", "Sequence engine", "-", why, "down")]


def _measurement_tile(snapshot_state: Dict[str, Any]) -> Dict[str, Any]:
    """Done/total, the running (or next) block, and the progress as a percentage."""
    sequence = snapshot_state.get("sequence") if isinstance(snapshot_state.get("sequence"), dict) else {}
    items = sequence.get("items") if isinstance(sequence.get("items"), list) else []
    progress = snapshot_state.get("progress") if isinstance(snapshot_state.get("progress"), dict) else {}
    total = progress.get("total")
    total = int(total) if isinstance(total, (int, float)) else len(items)
    done = progress.get("done")
    done = int(done) if isinstance(done, (int, float)) else 0
    run_state = str(snapshot_state.get("run_state") or "idle")
    current = snapshot_state.get("current") if isinstance(snapshot_state.get("current"), dict) else {}
    following = snapshot_state.get("next") if isinstance(snapshot_state.get("next"), dict) else {}
    tone = {"running": "busy", "stopping": "busy", "paused": "warn", "error": "warn"}.get(run_state, "idle")
    if total == 0:
        if snapshot_state.get("fault"):
            tone = "warn"
            return _tile("measurement", "Measurement", "0/0", "no blocks in the sequence - fault", tone)
        return _tile("measurement", "Measurement", "0/0", "no blocks in the sequence yet", "idle")
    percent = int(round(100.0 * done / total))
    parts = ["%d%% done" % percent]
    if current.get("name"):
        parts.append("now: %s" % current["name"])
    elif following.get("name"):
        parts.append("next: %s" % following["name"])
    if snapshot_state.get("fault"):
        parts.append("fault")
    if run_state != "idle":
        parts.append(run_state)
    return _tile("measurement", "Measurement", "%d/%d" % (done, total), " - ".join(parts), tone)


def _history_tile(snapshot_state: Dict[str, Any]) -> Dict[str, Any]:
    runs = snapshot_state.get("history_runs")
    runs = int(runs) if isinstance(runs, (int, float)) else 0
    if runs == 0:
        return _tile("history", "History", "0", "no finished runs yet", "idle")
    last = snapshot_state.get("history_last")
    if isinstance(last, (int, float)) and last > 0:
        try:
            said = "last finished %s" % datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M")
        except (OSError, OverflowError, ValueError):
            said = "last finished at an unknown time"
    else:
        said = "the last one's time is not known"
    return _tile("history", "History", str(runs), "%d run%s - %s" % (runs, "" if runs == 1 else "s", said), "idle")


def _saved_tile(snapshot_state: Dict[str, Any]) -> Dict[str, Any]:
    entries = snapshot_state.get("saved") if isinstance(snapshot_state.get("saved"), list) else []
    names = [str(s.get("name") or "") for s in entries if isinstance(s, dict) and s.get("name")]
    if names:
        shown = ", ".join(names[:3]) + (" and %d more" % (len(names) - 3) if len(names) > 3 else "")
        said = shown
    else:
        said = "no saved sequences yet"
    return _tile("saved", "Saved sequences", str(len(entries)), said, "idle")


def _functions_tile(library: Dict[str, Any]) -> Dict[str, Any]:
    """The function library (the palette: what a sequence is built from) - not the .py files on disk."""
    if not library.get("ok"):
        return _tile("functions", "Functions", "-", library.get("error") or "the function library was not read",
                     "down")
    entries = library.get("palette") if isinstance(library.get("palette"), list) else []
    names = [str(e.get("name") or "") for e in entries if isinstance(e, dict) and e.get("name")]
    said = (", ".join(names[:4]) + (" and %d more" % (len(names) - 4) if len(names) > 4 else "")) if names \
        else "the function library is empty: import a .py script"
    return _tile("functions", "Functions", str(len(entries)), said, "idle")


def _panel_tile(panel: Dict[str, Any]) -> Dict[str, Any]:
    """The engine itself: it is in this process, and its version is the one Kelvin vendored."""
    version = str(panel.get("version") or "") or "unknown"
    return _tile("panel", "Sequence engine", IN_PROCESS, "labMaster engine v%s" % version, "ok")


def snapshot(home: Optional[HomeStore] = None) -> Dict[str, Any]:
    """The Home page in one answer: {"ok", "panel", "greeting", "tiles", "cards", "updated_at"}.

    With the engine unavailable the page is still usable: ok stays True, panel.ok is False and
    every tile says tone="down". The five tiles come from the engine's state and palette."""
    store = home if home is not None else HomeStore()
    try:
        cards = store.cards()
    except Exception:                                              # noqa: BLE001 - a Home page never 500s
        cards = []
    updated_at = _now_iso()
    panel = ping()
    if not panel.get("ok"):
        return {"ok": True, "panel": panel, "greeting": _greeting(panel), "tiles": _down_tiles(),
                "cards": cards, "updated_at": updated_at}
    live = state()
    if not live.get("ok") or not isinstance(live.get("state"), dict):
        panel = dict(panel, ok=False, error=live.get("error") or "the engine's state could not be read")
        return {"ok": True, "panel": panel, "greeting": _greeting(panel), "tiles": _down_tiles(),
                "cards": cards, "updated_at": updated_at}
    panel_state = live["state"]
    library = palette()
    tiles = [_measurement_tile(panel_state), _history_tile(panel_state), _saved_tile(panel_state),
             _functions_tile(library), _panel_tile(panel)]
    return {"ok": True, "panel": panel, "greeting": _greeting(panel), "tiles": tiles, "cards": cards,
            "updated_at": updated_at}
