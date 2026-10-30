"""The sequence engine as one process-wide service, inside Kelvin (v1-A).

`sequence.service()` is the single `SequenceService` Kelvin's threads share: it owns one vendored
labMaster `Engine`, the person's "AI may start runs" switch, and Kelvin's own record of the AI's
measurements. No HTTP, no port 8770 - the engine is a library in this process now (V1_SPEC.md A).

    service()            the one instance (home = ~/Kelvin unless a caller names another)
    reset_service()      tests: close it, so the next service() starts fresh

The engine's data root is `~/Kelvin/sequence` (KELVIN_SEQUENCE_DIR overrides). The code, the
runners and VERSION live in this package (`paths.use_root` moves only the data folders).

Every public method is shaped for the portal and the MCP tools, and NEVER raises: a failed read
answers {"ok": False, "error": "..."} (with an empty list where the shape has one), a failed write
the same plus "state": None and "message": "" - the shape `web/labmaster.py` has returned since v0.

Threading: the Engine is thread-safe itself (its own RLock); this service adds one lock only around
the arm switch and `state/kelvin_runs.json` (read-modify-write, atomic replace). A call that stops
a running sequence may take a few seconds (the runner's grace), exactly as the panel's did.

Kelvin's runs record (`~/Kelvin/sequence/state/kelvin_runs.json`), at most 200, newest last:

    {"runs": [{"run_id", "name", "kind", "status", "detail", "started", "finished",
               "source": "kelvin"}, ...]}

`merged_runs()` is what the Sequence page's History shows: the engine's own finished runs
(source "panel") and Kelvin's records, newest first.
"""
from __future__ import annotations

import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths as P

ARM_KEY = "lab_sequence_ai_start"            # Kelvin settings: a person's switch, never an MCP tool
WAIT_KEY = "sequence_tools_wait_for_lab"     # Kelvin settings (v1-A stores it only; see web/agent.py)
KELVIN_RUNS_MAX = 200                        # one record per AI measurement; the oldest fall off
KELVIN_RUNS_FILE = "kelvin_runs.json"
SOURCE_PANEL = "panel"
SOURCE_KELVIN = "kelvin"
URL = "in-process"

# what /api/control accepts: the panel's action set, as the v0 bridge has it
CONTROL_ACTIONS = ("start", "pause", "pause_rep", "next_rep", "skip", "stop", "reset",
                   "end_wait", "set_wait")
DEFAULT_COLOR = "#5D9B86"                    # the colour "+ New function" starts a function with
AUTO_NAME = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")   # the engine's default sequence name

_SCRIPTS_README = """# Your measurement scripts

Put the `.py` files a sequence block may run in this folder, then add them to the function
library on Kelvin's Sequence page ("Import Python", or ask Kelvin to import one).

A script is normally a module with

    def main(...):        # one call = one measurement (one rep)
        ...

written as the labMaster panel expects: `labmaster_dataset` for its data, prints for the log.
"""


def _now() -> float:
    return time.time()


def _why(exc: BaseException) -> str:
    """One readable sentence for an exception: a KeyError's own text, never just repr noise."""
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    text = str(exc).strip()
    return text or type(exc).__name__


def _short(text: Any, limit: int = 2000) -> str:
    return str(text if text is not None else "")[:limit]


def _limit(value: Any, default: int, low: int = 1, high: int = 200) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def _settings_flag(settings: Any, name: str, default: bool) -> bool:
    """A yes/no Kelvin setting, read through whatever the Settings object offers (flag() in
    web/settings.py, else get()). A settings object that cannot be read is the default."""
    if settings is None:
        return default
    try:
        flag = getattr(settings, "flag", None)
        if callable(flag):
            return bool(flag(name, default))
    except Exception:                                             # noqa: BLE001 - never raise here
        return default
    try:
        raw = settings.get(name)
    except Exception:                                             # noqa: BLE001
        return default
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _settings_set_flag(settings: Any, name: str, on: bool) -> bool:
    """Store a yes/no setting; False when this Settings object has no way to write one."""
    if settings is None:
        return False
    try:
        setter = getattr(settings, "set_flag", None)
        if callable(setter):
            setter(name, bool(on))
            return True
    except Exception:                                             # noqa: BLE001
        return False
    try:
        update = getattr(settings, "update", None)
        if callable(update):
            update({name: "on" if on else ""})
            return True
    except Exception:                                             # noqa: BLE001
        return False
    return False


def _empty(keys: str, why: str, **extra: Any) -> Dict[str, Any]:
    """A failed read/write in the portal's shape: the named list keys are empty, not missing."""
    out: Dict[str, Any] = {"ok": False, "error": why}
    for key in keys.split():
        out[key] = []
    out.update(extra)
    return out


class SequenceService:
    """One vendored engine, one home folder, one arm switch, one record of Kelvin's runs."""

    def __init__(self, home: Optional[Path] = None, settings: Any = None):
        self.home = Path(home).expanduser().resolve() if home is not None else Path.home() / "Kelvin"
        self.settings = settings
        self.root = ""                    # the engine's data root, filled when the engine starts
        self._engine = None
        self._panel_engine = None
        self._cfg: Dict[str, Any] = {}
        self._error = ""
        self._closed = False
        self._lock = threading.RLock()    # the arm switch + state/kelvin_runs.json
        self._build_lock = threading.RLock()
        self._armed_memory = False        # used only when no settings object was given

    # ------------------------------------------------------------------ starting the engine

    def _root(self) -> str:
        env = (os.environ.get("KELVIN_SEQUENCE_DIR") or "").strip()
        return os.path.abspath(os.path.expanduser(env or str(self.home / "sequence")))

    def _prepare_root(self) -> str:
        """The data folders V1_SPEC.md lists, made on first use; blocks.json seeded from the
        package's blocks.default.json. Never touches a file that is there."""
        root = P.use_root(self._root())
        os.makedirs(root, exist_ok=True)
        P.ensure_dirs()
        for name in ("data", "scripts", "notes"):
            os.makedirs(os.path.join(root, name), exist_ok=True)
        readme = os.path.join(root, "scripts", "README.md")
        if not os.path.exists(readme):
            with open(readme, "w", encoding="utf-8", newline="\n") as f:
                f.write(_SCRIPTS_README)
        # A first start gets a config.json with no safe-stop script: Kelvin ships none, and the engine's
        # own default (scripts/example_safe_stop.py) would be a "missing script" banner on every page
        # (and, in real mode, a FAULT after every Stop). '' is the engine's documented "no stop script".
        # A real lab puts its own script here (paths.safe_stop_script) once Kelvin runs real instruments.
        if not os.path.exists(os.path.join(root, "config.json")):
            try:
                P.save_config({"paths": {"safe_stop_script": ""}})
            except OSError:
                pass
        P.seed_blocks()
        return root

    def _start_engine(self) -> None:
        """Build the engine once, for this service's root. Failures are kept in self._error (a
        portal that cannot start the engine still opens and says so, as it did with a panel down)."""
        with self._build_lock:
            if self._engine is not None or self._closed:
                return
            try:
                self.root = self._prepare_root()
                from . import engine as panel_engine   # 439 KB of stdlib: imported on first use
                self._panel_engine = panel_engine
                cfg = P.load_config()
                self._cfg = cfg if isinstance(cfg, dict) else {}
                self._engine = panel_engine.Engine(self._cfg)
                self._error = ""
            except Exception as exc:                              # noqa: BLE001 - never raise
                self._engine = None
                self._error = "the sequence engine could not start: %s: %s" % (
                    type(exc).__name__, _why(exc))

    def _ready(self):
        """(engine, error): the engine, or None and why not."""
        if self._closed:
            return None, "the sequence engine is closed (Kelvin is shutting down)"
        self._start_engine()
        if self._engine is None:
            return None, self._error or "the sequence engine is not available"
        return self._engine, ""

    def _snapshot(self, engine: Any = None) -> Optional[Dict[str, Any]]:
        """The engine's snapshot with Kelvin's additions, or None when it cannot be read."""
        engine = engine if engine is not None else self._engine
        if engine is None:
            return None
        try:
            state = engine.snapshot()
        except Exception:                                         # noqa: BLE001
            return None
        if not isinstance(state, dict):
            return None
        state.setdefault("panel_version", P.PANEL_VERSION)
        state.setdefault("engine_version", P.PANEL_VERSION)
        return state

    def _state_answer(self, message: str = "", ok: bool = True,
                      error: Optional[str] = None) -> Dict[str, Any]:
        return {"ok": ok, "error": error, "state": self._snapshot(), "message": message}

    def _refused(self, why: str, **extra: Any) -> Dict[str, Any]:
        """A write that was not carried out, in the shape web/labmaster.py's writes have."""
        out: Dict[str, Any] = {"ok": False, "error": why, "state": None, "message": ""}
        out.update(extra)
        return out

    def _write(self, fn: Any, message: str, **extra: Any) -> Dict[str, Any]:
        """Run one engine change; {"ok", "error", "state", "message"} either way. Never raises."""
        engine, why = self._ready()
        if engine is None:
            return self._refused(why, **extra)
        try:
            result = fn(engine)
        except Exception as exc:                                  # noqa: BLE001 - a refusal is an answer
            return self._refused(_why(exc), **extra)
        del result                                                # the change is the answer; the value is not
        out: Dict[str, Any] = {"ok": True, "error": None, "state": self._snapshot(engine),
                               "message": message}
        out.update(extra)
        return out

    # ------------------------------------------------------------------ reading (never raises)

    def ping(self) -> Dict[str, Any]:
        """{"ok", "sim", "url": "in-process", "version", "error"} - always available in-process."""
        out: Dict[str, Any] = {"ok": False, "sim": None, "url": URL, "version": None, "error": None}
        engine, why = self._ready()
        if engine is None:
            out["error"] = why
            return out
        out["ok"] = True
        out["version"] = str(getattr(P, "PANEL_VERSION", "") or "") or None
        try:
            out["sim"] = bool(self._cfg.get("sim"))
        except Exception:                                         # noqa: BLE001
            out["sim"] = None
        return out

    def state(self) -> Dict[str, Any]:
        """{"ok", "url", "error", "state"}: the Engine's snapshot (None when it cannot be read)."""
        engine, why = self._ready()
        if engine is None:
            return {"ok": False, "url": URL, "error": why, "state": None}
        state = self._snapshot(engine)
        if state is None:
            return {"ok": False, "url": URL,
                    "error": "the sequence engine's state could not be read", "state": None}
        return {"ok": True, "url": URL, "error": None, "state": state}

    def palette(self) -> Dict[str, Any]:
        """{"ok", "error", "palette": [...], "rev"}: the function library and its revision."""
        engine, why = self._ready()
        if engine is None:
            return _empty("palette", why, rev="")
        try:
            if hasattr(engine, "palette_with_rev"):
                entries, rev = engine.palette_with_rev()
            else:
                entries, rev = engine.palette(), engine.palette_rev()
            return {"ok": True, "error": None,
                    "palette": [dict(e) for e in entries if isinstance(e, dict)],
                    "rev": str(rev or "")}
        except Exception as exc:                                  # noqa: BLE001
            return _empty("palette", _why(exc), rev="")

    def scripts(self) -> Dict[str, Any]:
        """{"ok", "error", "scripts": [{path, name, has_main, main}]} - the .py files a
        function may run, under the engine's configured search dirs (data root scripts/)."""
        engine, why = self._ready()
        if engine is None:
            return _empty("scripts", why)
        try:
            found = engine.list_scripts()
            return {"ok": True, "error": None,
                    "scripts": [dict(s) for s in found if isinstance(s, dict)] if isinstance(found, list) else []}
        except Exception as exc:                                  # noqa: BLE001
            return _empty("scripts", _why(exc))

    def saved(self) -> Dict[str, Any]:
        """{"ok", "error", "saved": [{id, name, saved, n_blocks}]} - the saved sequences."""
        engine, why = self._ready()
        if engine is None:
            return _empty("saved", why)
        try:
            items = engine.saved
        except Exception as exc:                                  # noqa: BLE001
            return _empty("saved", _why(exc))
        out: List[Dict[str, Any]] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            out.append({k: item.get(k) for k in ("id", "name", "saved", "n_blocks") if k in item})
        return {"ok": True, "error": None, "saved": out}

    def history(self, limit: int = 20) -> Dict[str, Any]:
        """{"ok", "error", "runs": [...]}: the engine's own finished runs (batches), newest first."""
        wanted = _limit(limit, 20, high=50)
        engine, why = self._ready()
        if engine is None:
            return _empty("runs", why)
        try:
            view = engine.history_view(wanted)
            runs = view.get("runs") if isinstance(view, dict) else None
            return {"ok": True, "error": None,
                    "runs": [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []}
        except Exception as exc:                                  # noqa: BLE001
            return _empty("runs", _why(exc))

    # ------------------------------------------------------------------ the sequence (writes)

    def seq_add(self, block: Any = "", block_id: str = "",
                params: Optional[Dict[str, Any]] = None, index: Optional[int] = None) -> Dict[str, Any]:
        """Add one function to the sequence: by palette id, by palette name, or a palette entry."""
        engine, why = self._ready()
        if engine is None:
            return self._refused(why)
        wanted_id = str(block_id or "").strip()
        at: Optional[int] = None
        if index is not None:
            try:
                at = max(0, int(index))
            except (TypeError, ValueError):
                return self._refused("seq add: index must be a whole number")
        try:
            if wanted_id:
                item = engine.add_item(block_id=wanted_id, params=params, index=at)
            elif isinstance(block, dict) and block:
                item = engine.add_item(block=dict(block), params=params, index=at)
            else:
                name = str(block or "").strip()
                if not name:
                    return self._refused("seq add: say which function (its palette id or name)")
                entries = engine.palette()
                found = [e for e in entries if str(e.get("id") or "") == name]
                if not found:       # a name, case and spaces aside
                    found = [e for e in entries
                             if str(e.get("name") or "").strip().lower() == name.lower()]
                if not found:
                    return self._refused("seq add: no function %r in the library (see the function "
                                         "library / labmaster_functions)" % name)
                if len(found) > 1:
                    return self._refused("seq add: %d functions are called %r - give its palette id"
                                         % (len(found), name))
                item = engine.add_item(block_id=str(found[0].get("id") or ""), params=params, index=at)
        except Exception as exc:                                  # noqa: BLE001
            return self._refused(_why(exc))
        name = str((item or {}).get("name") or "") if isinstance(item, dict) else ""
        return {"ok": True, "error": None, "state": self._snapshot(engine),
                "message": ("Added %s." % name) if name else "Added to the sequence."}

    def seq_update(self, uid: Any, params: Optional[Dict[str, Any]] = None, name: str = "",
                   reset: bool = False) -> Dict[str, Any]:
        """Change one block's parameters or name, or reset it to pending (reset=True)."""
        uid = str(uid or "").strip()
        if not uid:
            return self._refused("seq update: say which block (its uid)")
        patch: Dict[str, Any] = {}
        if params is not None:
            patch["params"] = params
        if str(name or "").strip():
            patch["name"] = str(name).strip()
        if reset:
            patch["reset"] = True
        if not patch:
            return self._refused("seq update: nothing to change (give params, a name or reset)")
        return self._write(lambda engine: engine.update_item(uid, patch), "Updated the block.")

    def seq_delete(self, uid: Any) -> Dict[str, Any]:
        """Remove one block from the sequence."""
        uid = str(uid or "").strip()
        if not uid:
            return self._refused("seq delete: say which block (its uid)")
        return self._write(lambda engine: engine.delete_item(uid), "Removed the block.")

    def seq_move(self, uid: Any, index: Any) -> Dict[str, Any]:
        """Move one block to a new 0-based position."""
        uid = str(uid or "").strip()
        if not uid:
            return self._refused("seq move: say which block (its uid)")
        try:
            at = max(0, int(index))
        except (TypeError, ValueError):
            return self._refused("seq move: give the new position (a whole number)")
        return self._write(lambda engine: engine.move_item(uid, at), "Moved the block.")

    def seq_duplicate(self, uid: Any) -> Dict[str, Any]:
        """Copy one block right after itself."""
        uid = str(uid or "").strip()
        if not uid:
            return self._refused("seq duplicate: say which block (its uid)")
        engine, why = self._ready()
        if engine is None:
            return self._refused(why)
        try:
            item = engine.duplicate_item(uid)
        except Exception as exc:                                  # noqa: BLE001
            return self._refused(_why(exc))
        name = str((item or {}).get("name") or "") if isinstance(item, dict) else ""
        return {"ok": True, "error": None, "state": self._snapshot(engine),
                "message": ("Copied it as %s." % name) if name else "Copied the block."}

    def seq_clear(self, keep_done: bool = False) -> Dict[str, Any]:
        """Empty the sequence (keep_done=True keeps the blocks that finished)."""
        return self._write(lambda engine: engine.clear_sequence(keep_done=bool(keep_done)),
                           "Cleared the sequence.")

    def seq_rename(self, name: Any) -> Dict[str, Any]:
        """Name the live sequence (saved sequences keep their own names)."""
        label = str(name or "").strip()
        if not label:
            return self._refused("seq rename: give the sequence a name")
        return self._write(lambda engine: engine.rename_sequence(label), "Renamed the sequence.")

    # ------------------------------------------------------------------ saved sequences

    def _saved_id(self, item_id: str = "", name: str = "") -> Dict[str, Any]:
        """The saved sequence an id or a name means: {"ok", "id", "error", "name"}."""
        wanted = str(item_id or "").strip()
        if wanted:
            return {"ok": True, "id": wanted, "name": ""}
        label = str(name or "").strip()
        if not label:
            return {"ok": False, "id": "", "name": "",
                    "error": "say which saved sequence (its id, or its name)"}
        listed = self.saved()
        if not listed["ok"]:
            return {"ok": False, "id": "", "name": "", "error": str(listed.get("error") or "")}
        hits = [s for s in listed["saved"] if str(s.get("name") or "") == label]
        if len(hits) == 1:
            return {"ok": True, "id": str(hits[0].get("id") or ""), "name": label}
        if not hits:
            return {"ok": False, "id": "", "name": "",
                    "error": 'no saved sequence is called "%s"' % label}
        return {"ok": False, "id": "", "name": "",
                "error": '%d saved sequences are called "%s": say which by id' % (len(hits), label)}

    def saved_save(self, name: str = "", overwrite: bool = False) -> Dict[str, Any]:
        """Save the live sequence under a name (overwrite=True replaces one saved with that name)."""
        name = str(name or "").strip()
        if not name:
            return self._refused("saved save: give the sequence a name")
        engine, why = self._ready()
        if engine is None:
            return self._refused(why)
        try:
            entry = engine.save_current(name, overwrite=bool(overwrite))
        except Exception as exc:                                  # noqa: BLE001
            return self._refused(_why(exc))
        return {"ok": True, "error": None, "state": self._snapshot(engine),
                "message": 'Saved the sequence as "%s".' % name,
                "entry": entry if isinstance(entry, dict) else None}

    def saved_load(self, item_id: str = "", name: str = "", append: bool = False,
                   replace: bool = False) -> Dict[str, Any]:
        """Load a saved sequence into the live one: append=True adds it after what is there,
        replace=True discards the live blocks (the engine refuses a load over progress until one
        is given)."""
        if append and replace:
            return self._refused("saved load: append and replace are different - choose one")
        found = self._saved_id(item_id, name)
        if not found["ok"]:
            return self._refused("saved load: " + str(found.get("error") or ""))
        engine, why = self._ready()
        if engine is None:
            return self._refused(why)
        try:
            engine.load_saved(str(found["id"]), append=bool(append), replace=bool(replace))
        except Exception as exc:                                  # noqa: BLE001
            return self._refused(_why(exc))
        return {"ok": True, "error": None, "state": self._snapshot(engine),
                "message": ('Appended saved sequence "%s".' % found["name"]) if append
                else ('Loaded saved sequence "%s".' % found["name"])}

    def saved_delete(self, item_id: str = "", name: str = "") -> Dict[str, Any]:
        """Delete a saved sequence (by id, or by a name no other saved sequence has)."""
        found = self._saved_id(item_id, name)
        if not found["ok"]:
            return self._refused("saved delete: " + str(found.get("error") or ""))
        engine, why = self._ready()
        if engine is None:
            return self._refused(why)
        try:
            engine.delete_saved(str(found["id"]))
        except Exception as exc:                                  # noqa: BLE001
            return self._refused(_why(exc))
        return {"ok": True, "error": None, "state": self._snapshot(engine),
                "message": 'Deleted saved sequence "%s".' % (found["name"] or found["id"])}

    # ------------------------------------------------------------------ control

    def control(self, action: Any, **kw: Any) -> Dict[str, Any]:
        """start / pause / pause_rep / next_rep / skip / stop / reset / end_wait / set_wait,
        with whatever the action takes (want, uid, minutes, failed, failed_uids, reason).
        safe_stop goes to safe_stop(), as the v0 bridge did."""
        action = str(action or "").strip().lower()
        if action == "safe_stop":        # the page's SAFE STOP button
            return self.safe_stop(str(kw.get("reason") or ""))
        if not action:
            return self._refused("control: say which action")
        if action not in CONTROL_ACTIONS:
            return self._refused("control: unknown action %r (one of %s)"
                                 % (action, ", ".join(CONTROL_ACTIONS)))
        engine, why = self._ready()
        if engine is None:
            return self._refused(why)
        want = kw.get("want")
        if want is not None and not isinstance(want, bool):
            return self._refused("%s: want must be true or false (got %r)" % (action, want))
        uid = kw.get("uid")
        try:
            if action == "start":
                start: Dict[str, Any] = {}
                if kw.get("failed") is not None:
                    start["failed"] = kw.get("failed")
                if kw.get("failed_uids") is not None:
                    start["failed_uids"] = kw.get("failed_uids")
                engine.start(**start)
            elif action in ("pause", "pause_rep"):
                engine.pause("rep" if action == "pause_rep" else "block", want=want)
            elif action == "next_rep":
                engine.next_after_rep(uid=str(uid) if uid else None, want=want)
            elif action == "skip":
                engine.skip_block(uid=str(uid) if uid else None)
            elif action == "stop":
                engine.stop(reason=str(kw.get("reason") or "user"))
            elif action == "reset":
                engine.reset_all()
            elif action == "end_wait":
                engine.resume_wait_now(uid=str(uid) if uid else None)
            elif action == "set_wait":
                engine.set_wait_minutes(kw.get("minutes"),
                                        **({"uid": str(uid)} if uid else {}))
        except Exception as exc:                                  # noqa: BLE001 - a refusal is an answer
            return self._refused(_why(exc))
        return {"ok": True, "error": None, "state": self._snapshot(engine),
                "message": "The sequence engine carried out %s." % action}

    def safe_stop(self, reason: str = "") -> Dict[str, Any]:
        """Stop whatever runs, then run the configured safe-stop script."""
        engine, why = self._ready()
        if engine is None:
            return self._refused(why)
        try:
            result = engine.safe_stop_everything(reason=str(reason or "") or "user")
        except Exception as exc:                                  # noqa: BLE001
            return self._refused(_why(exc))
        result = result if isinstance(result, dict) else {}
        ok = bool(result.get("ok", True))
        message = str(result.get("message") or "").strip()
        if not message:
            if result.get("sim"):
                message = "Safe stop: simulated, no script was run."
            elif result.get("skipped"):
                message = "Safe stop: %s." % result.get("skipped")
            else:
                message = "Safe stop started."
        return {"ok": ok, "error": None if ok else str(result.get("error") or "the safe stop failed"),
                "state": self._snapshot(engine), "message": message}

    # ------------------------------------------------------------------ importing a script

    def import_script(self, path: Any, name: str = "", kind: str = "script") -> Dict[str, Any]:
        """Put a script from the engine's search dirs into the function library, as "+ New
        function" does: the whole library is written back with base_rev (the engine refuses a
        list older than the one it has). Returns {"ok", "error", "entry", "palette", "message"}."""
        wanted = str(path or "").strip()
        if not wanted:
            return {"ok": False, "error": "import: give the script's path, as /api/scripts lists it",
                    "entry": None, "palette": None, "message": ""}
        engine, why = self._ready()
        if engine is None:
            return {"ok": False, "error": why, "entry": None, "palette": None, "message": ""}
        try:
            found = engine.list_scripts()
            record = _find_script([s for s in found if isinstance(s, dict)] if isinstance(found, list) else [],
                                  wanted)
            if record is None:
                return {"ok": False, "entry": None, "palette": None, "message": "",
                        "error": "import: %r is not a .py file under the engine's script search dirs "
                                 "(see /api/labmaster/scripts)" % wanted}
            if hasattr(engine, "palette_with_rev"):
                entries_now, rev = engine.palette_with_rev()
            else:
                entries_now, rev = engine.palette(), engine.palette_rev()
            target = _script_path(record.get("path") or wanted)
            entries = [dict(e) for e in entries_now if isinstance(e, dict)]
            made = _new_palette_entry(target, name, kind)
            same_target = sum(1 for e in entries if _script_path(e.get("target")) == target)
            saved = engine.save_palette_from([made] + entries, rev)
        except Exception as exc:                                  # noqa: BLE001 - refused, not raised
            return {"ok": False, "error": _why(exc), "entry": None, "palette": None, "message": ""}
        saved_palette = [e for e in saved if isinstance(e, dict)] if isinstance(saved, list) else None
        entry: Optional[Dict[str, Any]] = None
        if saved_palette:
            old_ids = {str(e.get("id") or "") for e in entries}
            fresh = [e for e in saved_palette
                     if str(e.get("id") or "") not in old_ids and _script_path(e.get("target")) == target]
            if not fresh:
                fresh = [e for e in saved_palette if str(e.get("id") or "") not in old_ids]
            entry = fresh[0] if fresh else None
        if entry is None:      # the engine answered without the saved list: the entry we sent is what it saved
            entry = dict(made)
        said = 'Added "%s" to the function library.' % (entry.get("name") or made["name"])
        if same_target:
            said += " The library already had %d function%s for that script." % (
                same_target, "" if same_target == 1 else "s")
        return {"ok": True, "error": None, "entry": entry,
                "palette": saved_palette if saved_palette is not None else [made] + entries,
                "message": said}

    # ------------------------------------------------------------------ the arm switch (a person)

    def armed(self) -> bool:
        """May Kelvin's AI start a run on its own? A person's switch (Sequence page), default off."""
        if self.settings is not None:
            return _settings_flag(self.settings, ARM_KEY, False)
        with self._lock:
            return bool(self._armed_memory)

    def set_armed(self, on: Any) -> Dict[str, Any]:
        """Store the person's switch. Only the portal's POST /api/labmaster/armed calls this: it
        is deliberately not an MCP tool (the AI can never arm itself)."""
        on = bool(on)
        stored = _settings_set_flag(self.settings, ARM_KEY, on) if self.settings is not None else True
        with self._lock:
            self._armed_memory = on
        if self.settings is not None and not stored:
            return {"ok": False, "armed": self.armed(),
                    "error": "the arm switch could not be saved into Kelvin's settings"}
        return {"ok": True, "error": None, "armed": on,
                "message": ("AI may start runs." if on
                            else "The AI needs a person's approval before each start.")}

    # ------------------------------------------------------------------ Kelvin's runs record

    def _runs_path(self) -> str:
        """Kelvin's record file, in the engine's state folder. Read from this service's own root
        (not P.STATE_DIR alone): a record can be written before the engine has started, and a test
        may have pointed the vendored paths module at another temporary root since."""
        root = self.root or self._root()
        return os.path.join(root, "state", KELVIN_RUNS_FILE)

    def _read_runs(self) -> List[Dict[str, Any]]:
        """The record as stored ([] when it is not there or cannot be read)."""
        try:
            data = P.read_json(self._runs_path(), {"runs": []}, expect=dict)
        except Exception:                                         # noqa: BLE001
            return []
        runs = data.get("runs") if isinstance(data, dict) else None
        return [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []

    def _write_runs(self, runs: List[Dict[str, Any]]) -> None:
        """At most KELVIN_RUNS_MAX, newest last, whole file atomically (a crash never leaves it
        half written: paths.write_json is a temporary file then os.replace)."""
        P.write_json(self._runs_path(), {"runs": list(runs)[-KELVIN_RUNS_MAX:]})

    def kelvin_runs(self) -> List[Dict[str, Any]]:
        """Kelvin's own record of the AI's measurements, oldest first."""
        return self._read_runs()

    def record_kelvin_run(self, name: str, kind: str = "measurement", status: str = "done",
                          detail: str = "", run_id: str = "",
                          started: Optional[float] = None) -> Dict[str, Any]:
        """One AI measurement (a plan, a script, a recipe, a queue item) on the Sequence page's
        History. Never raises: a record that cannot be written answers ok=False, and the AI's
        work goes on (the callers treat it as fail-soft)."""
        label = _short(name, 200).strip()
        if not label:
            label = "measurement"
        status = (_short(status, 40).strip().lower() or "done")
        run: Dict[str, Any] = {
            "run_id": _short(run_id, 80).strip() or ("kr_" + uuid.uuid4().hex[:12]),
            "name": label,
            "kind": _short(kind, 60).strip() or "measurement",
            "status": status,
            "detail": _short(detail, 2000),
            "started": _finite_or_now(started),
            "finished": _now() if status in ("done", "failed", "error", "cancelled", "skipped")
            else None,
            "source": SOURCE_KELVIN,
        }
        try:
            with self._lock:
                runs = self._read_runs()
                runs.append(run)
                self._write_runs(runs)
                count = len(runs[-KELVIN_RUNS_MAX:])
        except Exception as exc:                                  # noqa: BLE001 - fail-soft
            return {"ok": False, "error": "the run could not be recorded: %s: %s"
                    % (type(exc).__name__, _why(exc)), "run": run}
        return {"ok": True, "error": None, "run": run, "runs": count}

    def merged_runs(self, limit: int = 20) -> Dict[str, Any]:
        """{"ok", "runs": [...]}: the engine's own runs (source "panel") and Kelvin's records
        (source "kelvin"), newest first. The Sequence page's History."""
        wanted = _limit(limit, 20, high=100)
        error: Optional[str] = None
        panel: List[Dict[str, Any]] = []
        engine, why = self._ready()
        if engine is None:
            error = why
        else:
            try:
                view = engine.history_view(wanted)
                runs = view.get("runs") if isinstance(view, dict) else None
                panel = [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []
            except Exception as exc:                              # noqa: BLE001 - Kelvin's runs still show
                error = _why(exc)
        out: List[Dict[str, Any]] = [_panel_run(r) for r in panel]
        out += [_kelvin_run(r) for r in self.kelvin_runs()]
        out.sort(key=lambda r: _run_when(r), reverse=True)
        return {"ok": True, "error": error, "runs": out[:wanted]}

    # ------------------------------------------------------------------ the complete History page

    def panel_runs(self) -> List[Dict[str, Any]]:
        """EVERY run in the engine's own History (not the newest 60), as /api/history/all rows:
        source "panel", name, status, started (epoch), at (local ISO), finished, detail, run_id,
        blocks, dataset (the first dataset path it wrote, if any), folder None, report_files [],
        summary "". Newest first. Never raises: an engine that cannot be read is []."""
        engine, _why_not = self._ready()
        if engine is None:
            return []
        try:
            runs = engine.get_history(10 ** 6) if hasattr(engine, "get_history") else []
        except Exception:                                         # noqa: BLE001 - an empty History, not a 500
            return []
        out: List[Dict[str, Any]] = []
        for run in runs if isinstance(runs, list) else []:
            if not isinstance(run, dict):
                continue
            blocks = run.get("blocks") if isinstance(run.get("blocks"), list) else []
            datasets: List[str] = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                for path in block.get("datasets") or []:
                    text = _slash(str(path or "").strip())
                    if text and text not in datasets:
                        datasets.append(text)
            n_blocks = _int_or_zero(run.get("n_blocks")) or len(
                {str(b.get("uid") or "") for b in blocks if isinstance(b, dict)})
            n_error = _int_or_zero(run.get("n_error"))
            started = _epoch_or_none(run.get("started"))
            finished = _epoch_or_none(run.get("finished"))
            bits = ["%d block%s" % (n_blocks, "" if n_blocks == 1 else "s")]
            if datasets:
                bits.append("%d dataset%s" % (len(datasets), "" if len(datasets) == 1 else "s"))
            if n_error:
                bits.append("%d error%s" % (n_error, "" if n_error == 1 else "s"))
            out.append({"source": SOURCE_PANEL,
                        "name": str(run.get("name") or run.get("run_id") or "").strip()
                        or str(run.get("run_id") or ""),
                        "status": "failed" if n_error else "done",
                        "started": started, "at": _iso_at(started), "finished": finished,
                        "detail": ", ".join(bits),
                        "run_id": str(run.get("run_id") or "") or None,
                        "blocks": n_blocks, "dataset": datasets[0] if datasets else None,
                        "folder": None, "report_files": [], "summary": ""})
        out.sort(key=lambda entry: entry.get("started") or 0.0, reverse=True)
        return out

    def panel_run(self, run_id: str) -> Dict[str, Any]:
        """One panel run's detail (its blocks with params and datasets, and the run's datasets) for
        /api/history/panel-run: {"ok", "error", "run"}. Not found is ok False with a readable error
        (the route answers 404). Never raises."""
        wanted = str(run_id or "").strip()
        if not wanted:
            return {"ok": False, "error": "give the panel run's id", "run": None}
        engine, why = self._ready()
        if engine is None:
            return {"ok": False, "error": why, "run": None}
        try:
            rows = [row for row in (getattr(engine, "history", None) or [])
                    if isinstance(row, dict) and str(row.get("run_id") or "") == wanted]
        except Exception:                                         # noqa: BLE001
            rows = []
        if not rows:
            return {"ok": False, "error": 'no panel run "%s"' % wanted, "run": None}
        rows.sort(key=lambda row: (_epoch_or_none(row.get("started")) or 0.0,
                                   _int_or_zero(row.get("n"))))
        blocks: List[Dict[str, Any]] = []
        datasets: List[str] = []
        for row in rows:
            row_datasets = []
            for path in row.get("datasets") or []:
                text = _slash(str(path or "").strip())
                if text:
                    row_datasets.append(text)
                    if text not in datasets:
                        datasets.append(text)
            blocks.append({"n": row.get("n"), "uid": str(row.get("uid") or ""),
                           "name": str(row.get("name") or ""), "kind": str(row.get("kind") or ""),
                           "status": str(row.get("status") or ""),
                           "message": str(row.get("message") or ""),
                           "started": _epoch_or_none(row.get("started")),
                           "finished": _epoch_or_none(row.get("finished")),
                           "params": row.get("params") if isinstance(row.get("params"), dict) else {},
                           "overrides": (row.get("overrides")
                                         if isinstance(row.get("overrides"), dict) else {}),
                           "datasets": row_datasets})
        started = min((block["started"] for block in blocks if block["started"] is not None),
                      default=None)
        finished = max((block["finished"] for block in blocks if block["finished"] is not None),
                       default=None)
        run = {"run_id": wanted, "name": str(rows[0].get("run_name") or ""),
               "status": "failed" if any(block["status"] == "error" for block in blocks) else "done",
               "started": started, "at": _iso_at(started), "finished": finished,
               "blocks": blocks, "datasets": datasets,
               "n_blocks": len({block["uid"] for block in blocks if block["uid"]}),
               "n_datasets": len(datasets)}
        return {"ok": True, "error": None, "run": run}

    def ensure_sequence_name(self, name: str) -> Dict[str, Any]:
        """Give the live sequence the AI's name when it has none (or only the engine's default
        date-time name). A name a person set is never overwritten: the answer says which stands."""
        label = _short(name, 200).strip()
        engine, why = self._ready()
        if engine is None:
            return {"ok": False, "error": why, "state": None, "message": "", "name": "", "renamed": False}
        state = self._snapshot(engine) or {}
        current = str(((state.get("sequence") or {}).get("name")) or "").strip()
        if not label:
            return {"ok": False, "error": "give the sequence a name", "state": state, "message": "",
                    "name": current, "renamed": False}
        try:
            if current and not AUTO_NAME.match(current):
                return {"ok": True, "error": None, "state": state,
                        "message": 'The sequence is already called "%s".' % current,
                        "name": current, "renamed": False}
            engine.rename_sequence(label)
        except Exception as exc:                                  # noqa: BLE001
            return {"ok": False, "error": _why(exc), "state": state, "message": "",
                    "name": current, "renamed": False}
        return {"ok": True, "error": None, "state": self._snapshot(engine),
                "message": 'Named the sequence "%s".' % label, "name": label, "renamed": True}

    # ------------------------------------------------------------------ shutting down

    def close(self) -> None:
        """Kelvin is closing: stop what runs (the runner is killed, the safe stop runs as STOP does)
        and let the data-folder index go. Never raises; one call does the work."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            engine, self._engine = self._engine, None
        if engine is None:
            return
        try:
            child = getattr(engine, "_proc", None)
            running = str(getattr(engine, "run_state", "idle")) in ("running", "stopping") \
                or (child is not None and child.poll() is None)
            if running:
                engine.stop(reason="Kelvin is closing")
        except Exception:                                         # noqa: BLE001 - a close never raises
            pass
        try:
            stop_df = getattr(engine, "df_stop", None)
            if callable(stop_df):
                stop_df()
        except Exception:                                         # noqa: BLE001
            pass


# ---------------------------------------------------------------- one service per process


_SERVICE: Optional[SequenceService] = None
_SERVICE_LOCK = threading.RLock()


def service(home: Optional[Path] = None, settings: Any = None) -> SequenceService:
    """The process-wide service. `home` (~/Kelvin) and `settings` are used when it is made; a
    later call with a different home (a test, a second portal in one process) closes the old one
    and makes a new service for that home. A service that has been closed is replaced too."""
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is not None and _SERVICE._closed:
            _SERVICE = None
        if _SERVICE is None:
            _SERVICE = SequenceService(home, settings)
            return _SERVICE
        if home is not None:
            wanted = Path(home).expanduser().resolve()
            if wanted != _SERVICE.home:
                _SERVICE.close()
                _SERVICE = SequenceService(wanted, settings)
            elif settings is not None:
                _SERVICE.settings = settings
        elif settings is not None:
            _SERVICE.settings = settings
        return _SERVICE


def reset_service() -> None:
    """Tests: close the one service (the next service() starts a new engine)."""
    global _SERVICE
    with _SERVICE_LOCK:
        old, _SERVICE = _SERVICE, None
    if old is not None:
        old.close()


# ---------------------------------------------------------------- helpers for the two run lists


def _finite_or_now(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _now()
    if number != number or number in (float("inf"), float("-inf")):   # NaN / ±inf
        return _now()
    return number


def _epoch_or_none(value: Any) -> Optional[float]:
    """An epoch second (a number, or an ISO string as a run's meta keeps it), or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            try:
                return datetime.fromisoformat(text.rstrip("Z")).astimezone().timestamp()
            except ValueError:
                return None
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
    if number != number or number in (float("inf"), float("-inf")):   # NaN / ±inf
        return None
    return number


def _iso_at(epoch: Optional[float]) -> str:
    """An epoch second as local ISO 8601 with this PC's offset, or '' (never raises)."""
    if epoch is None:
        return ""
    try:
        return datetime.fromtimestamp(float(epoch)).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return ""


def _slash(path: Any) -> str:
    """A path as the pages show it: forward slashes (the run and dataset paths in History)."""
    return str(path or "").strip().replace("\\", "/")


def _run_when(run: Dict[str, Any]) -> float:
    """When a merged row sorts from: finished, else started, else 0 (a bad value sorts last)."""
    for key in ("finished", "started"):
        try:
            value = float(run.get(key))
        except (TypeError, ValueError):
            continue
        if value == value and value not in (float("inf"), float("-inf")):
            return value
    return 0.0


def _panel_run(run: Dict[str, Any]) -> Dict[str, Any]:
    """One of the engine's History runs, as the merged list shows it (source "panel")."""
    n_blocks = _int_or_zero(run.get("n_blocks"))
    n_error = _int_or_zero(run.get("n_error"))
    n_datasets = _int_or_zero(run.get("n_datasets"))
    bits = ["%d block%s" % (n_blocks, "" if n_blocks == 1 else "s")]
    if n_datasets:
        bits.append("%d dataset%s" % (n_datasets, "" if n_datasets == 1 else "s"))
    if n_error:
        bits.append("%d error%s" % (n_error, "" if n_error == 1 else "s"))
    return {"source": SOURCE_PANEL, "run_id": str(run.get("run_id") or ""),
            "name": str(run.get("name") or ""),
            "status": "error" if n_error else "done",
            "detail": ", ".join(bits), "started": run.get("started"),
            "finished": run.get("finished"), "sim": run.get("sim"),
            "n_blocks": n_blocks, "n_error": n_error, "n_datasets": n_datasets}


def _kelvin_run(run: Dict[str, Any]) -> Dict[str, Any]:
    """One of Kelvin's run records, as the merged list shows it (source "kelvin")."""
    out = dict(run)
    out["source"] = SOURCE_KELVIN
    out.setdefault("run_id", "")
    out.setdefault("name", "")
    out.setdefault("status", "done")
    out.setdefault("detail", "")
    out.setdefault("started", None)
    out.setdefault("finished", None)
    return out


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- importing a script, as app.js does


def _script_path(path: Any) -> str:
    """A script's path with forward slashes and no "./" prefix, as list_scripts() gives it."""
    text = str(path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def _find_script(entries: List[Dict[str, Any]], wanted: str) -> Optional[Dict[str, Any]]:
    """The engine's script record for a path (or a file name no other record has)."""
    want = _script_path(wanted)
    if not want:
        return None
    for entry in entries:
        if _script_path(entry.get("path")) == want:
            return entry
    base = want.rsplit("/", 1)[-1]
    by_name = [e for e in entries if str(e.get("name") or "") == base]
    return by_name[0] if len(by_name) == 1 else None


def _new_palette_entry(target: str, name: str, kind: str) -> Dict[str, Any]:
    """A new function as "+ New function" makes one (labMaster/static/app.js): a script block
    starts with {"reps": 1}; the engine gives it its id when the whole library is saved."""
    kind = str(kind or "script").strip() or "script"
    label = str(name or "").strip()
    if not label:
        label = target.rsplit("/", 1)[-1]
        if label.lower().endswith(".py"):
            label = label[:-3]
    return {"id": "", "name": label, "kind": kind,
            "target": target if kind == "script" else "",
            "params": {"reps": 1} if kind == "script" else {},
            "overrides": {}, "override_base": {}, "note": "", "color": DEFAULT_COLOR}
