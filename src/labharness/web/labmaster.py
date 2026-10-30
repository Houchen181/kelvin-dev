"""The labMaster bridge: the portal's side of the labMaster panel's HTTP API, and the Home page's cards.

    panel at KELVIN_LABMASTER_URL (default http://127.0.0.1:8770), its /api/* endpoints as panel_server.py
    serves them. Nothing here raises: a panel that is down, slow or refusing an edit answers
    {"ok": False, "error": "<what went wrong>"} instead, so the portal shows it in words and never a 500.

    The write functions (seq_*, control, safe_stop, saved_*, import_script) return
    {"ok", "error", "state", "message"} - "state" is the panel's snapshot after the change, as
    GET /api/labmaster/state gives it; import_script adds the palette entry it made.

    HomeStore is Kelvin's own: the cards the AI puts on the Home page, kept in ~/Kelvin/home.json
    (atomic write, UTF-8). snapshot() turns the panel's /api/state into the Home page's five tiles -
    and, with the panel down, still returns a usable page (every tile tone="down").

    Standard library only: the portal imports this module on a thread, beside the event loop.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_URL_ENV = "KELVIN_LABMASTER_URL"     # where the panel is; the desktop app and the panel share a PC
DEFAULT_URL = "http://127.0.0.1:8770"
TIMEOUT = 4.0                             # one panel call: a hung panel must not hold the page past one poll
CARD_KINDS = ("note", "metric", "checklist", "link", "sequence")
DEFAULT_COLOR = "#5D9B86"                 # the colour "+ New function" starts a function with
_HOME_LOCK = threading.RLock()            # one Home page, however many panels/tools reach it


# ---------------------------------------------------------------- the panel's HTTP API (never raises)

def panel_url() -> str:
    """Where the labMaster panel is: KELVIN_LABMASTER_URL, else http://127.0.0.1:8770."""
    url = (os.environ.get(BASE_URL_ENV) or "").strip()
    return (url or DEFAULT_URL).rstrip("/")


def _json_or_none(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None


def _call(path: str, method: str = "GET", body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One request to the panel: {"ok", "error", "status", "data"}. Never raises.

    A 2xx answer with a JSON object is data; a 4xx/5xx carries the panel's own {"error"} as the error."""
    url = panel_url() + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
            raw, status = answer.read(), answer.status
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read()
        except OSError:
            pass
        payload = _json_or_none(raw)
        said = str((payload or {}).get("error") or "").strip() if isinstance(payload, dict) else ""
        return {"ok": False, "status": e.code, "data": payload if isinstance(payload, dict) else None,
                "error": said or "labMaster answered HTTP %d for %s" % (e.code, path)}
    except (urllib.error.URLError, OSError) as e:
        why = getattr(e, "reason", None) or e
        return {"ok": False, "status": None, "data": None,
                "error": "labMaster panel not reachable at %s (%s)" % (panel_url(), why)}
    except Exception as e:                                        # noqa: BLE001 - this module never raises
        return {"ok": False, "status": None, "data": None,
                "error": "labMaster request %s failed (%s: %s)" % (path, type(e).__name__, e)}
    payload = _json_or_none(raw)
    if not isinstance(payload, dict):
        return {"ok": False, "status": status, "data": None,
                "error": "labMaster answered %s with something other than JSON" % path}
    if payload.get("ok") is False:     # a 200 that says it refused (some endpoints answer so)
        return {"ok": False, "status": status, "data": payload,
                "error": str(payload.get("error") or "labMaster refused the request")}
    return {"ok": True, "status": status, "data": payload, "error": None}


def ping() -> Dict[str, Any]:
    """Is the panel there, is it simulated, which version? {"ok", "sim", "url", "version", "error"}."""
    url = panel_url()
    out: Dict[str, Any] = {"ok": False, "sim": None, "url": url, "version": None, "error": None}
    answer = _call("/api/ping")
    if not answer["ok"]:
        out["error"] = answer["error"]
        return out
    payload = answer["data"] or {}
    out["ok"] = True
    sim = payload.get("sim")
    out["sim"] = bool(sim) if sim is not None else None
    version = _call("/api/version")
    if version["ok"]:
        out["version"] = str((version["data"] or {}).get("version") or "") or None
    return out


def state() -> Dict[str, Any]:
    """GET /api/state: {"ok", "url", "error", "state"} (state is None when the panel is not there)."""
    answer = _call("/api/state")
    return {"ok": answer["ok"], "url": panel_url(), "error": answer["error"],
            "state": answer["data"] if answer["ok"] else None}


def palette() -> Dict[str, Any]:
    """GET /api/palette: {"ok", "error", "palette": [...], "rev": str} (the function library and its revision)."""
    answer = _call("/api/palette")
    if not answer["ok"]:
        return {"ok": False, "error": answer["error"], "palette": [], "rev": ""}
    payload = answer["data"] or {}
    entries = payload.get("palette")
    return {"ok": True, "error": None,
            "palette": [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else [],
            "rev": str(payload.get("rev") or "")}


def scripts() -> Dict[str, Any]:
    """GET /api/scripts: {"ok", "error", "scripts": [{path, name, has_main, main}]} - the .py files a
    function may run, under the panel's configured search dirs."""
    answer = _call("/api/scripts")
    if not answer["ok"]:
        return {"ok": False, "error": answer["error"], "scripts": []}
    payload = answer["data"] or {}
    found = payload.get("scripts")
    return {"ok": True, "error": None, "scripts": [s for s in found if isinstance(s, dict)]
            if isinstance(found, list) else []}


def saved() -> Dict[str, Any]:
    """GET /api/saved: {"ok", "error", "saved": [{id, name, saved, n_blocks}]} - the saved sequences."""
    answer = _call("/api/saved")
    if not answer["ok"]:
        return {"ok": False, "error": answer["error"], "saved": []}
    payload = answer["data"] or {}
    items = payload.get("saved")
    out: List[Dict[str, Any]] = []
    for s in items if isinstance(items, list) else []:
        if not isinstance(s, dict):
            continue
        out.append({k: s.get(k) for k in ("id", "name", "saved", "n_blocks") if k in s})
    return {"ok": True, "error": None, "saved": out}


def history(limit: int = 5) -> Dict[str, Any]:
    """GET /api/history: {"ok", "error", "runs": [...]} - the newest finished runs (batches), newest first."""
    try:
        wanted = max(1, min(int(limit or 5), 50))
    except (TypeError, ValueError):
        wanted = 5
    answer = _call("/api/history?limit=%d" % wanted)
    if not answer["ok"]:
        return {"ok": False, "error": answer["error"], "runs": []}
    payload = answer["data"] or {}
    runs = payload.get("runs")
    out = []
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, dict):
            continue
        out.append({"run_id": str(run.get("run_id") or ""), "name": str(run.get("name") or ""),
                    "started": run.get("started"), "finished": run.get("finished"), "sim": run.get("sim"),
                    "n_blocks": run.get("n_blocks"), "n_error": run.get("n_error"),
                    "n_datasets": run.get("n_datasets")})
    return {"ok": True, "error": None, "runs": out[:wanted]}


# ---------------------------------------------------------------- the sequence (writes)

def _refused(why: str, **extra: Any) -> Dict[str, Any]:
    """A write that never reached the panel: the same shape as one it refused."""
    out: Dict[str, Any] = {"ok": False, "error": why, "state": None, "message": ""}
    out.update(extra)
    return out


def _changed(answer: Dict[str, Any], message: str) -> Dict[str, Any]:
    """The panel's answer to a change, as the write functions return it."""
    if not answer["ok"]:
        return {"ok": False, "error": answer["error"], "state": None, "message": ""}
    data = answer["data"] if isinstance(answer["data"], dict) else {}
    state = data.get("state")
    return {"ok": True, "error": None, "state": state if isinstance(state, dict) else None,
            "message": message}


def _item_name(data: Any) -> str:
    item = data.get("item") if isinstance(data, dict) else None
    return str(item.get("name") or "") if isinstance(item, dict) else ""


def seq_add(block: Any = "", block_id: str = "", params: Optional[Dict[str, Any]] = None,
            index: Optional[int] = None) -> Dict[str, Any]:
    """Add one function to the sequence. The panel adds by palette id: block may be that id or the
    function's name (looked up in the library here), or a palette entry, whole (the panel takes one too)."""
    block_id = str(block_id or "").strip()
    body: Dict[str, Any] = {}
    if block_id:
        body["block_id"] = block_id
    elif isinstance(block, dict) and block:
        body["block"] = block          # a palette entry as the library holds it: the panel takes it whole
    else:
        name = str(block or "").strip()
        if not name:
            return _refused("seq add: say which function (its palette id or name)")
        library = palette()
        if not library["ok"]:
            return _refused("seq add: the function library could not be read: %s" % library["error"])
        entries = library["palette"]
        found = [e for e in entries if str(e.get("id") or "") == name]
        if not found:   # a name, case and spaces aside
            found = [e for e in entries if str(e.get("name") or "").strip().lower() == name.lower()]
        if not found:
            return _refused("seq add: no function %r in the library (see the function library / "
                            "labmaster_functions)" % name)
        if len(found) > 1:
            return _refused("seq add: %d functions are called %r - give its palette id" % (len(found), name))
        body["block_id"] = str(found[0].get("id") or "")
    if params is not None:
        body["params"] = params
    if index is not None:
        try:
            body["index"] = max(0, int(index))
        except (TypeError, ValueError):
            return _refused("seq add: index must be a whole number")
    answer = _call("/api/seq/add", "POST", body)
    return _changed(answer, ("Added %s." % _item_name(answer["data"])) if _item_name(answer["data"])
                    else "Added to the sequence.")


def seq_update(uid: str, params: Optional[Dict[str, Any]] = None, name: str = "",
               reset: bool = False) -> Dict[str, Any]:
    """Change one block's parameters or name, or reset it to pending (reset=True)."""
    uid = str(uid or "").strip()
    if not uid:
        return _refused("seq update: say which block (its uid)")
    patch: Dict[str, Any] = {}
    if params is not None:
        patch["params"] = params
    if str(name or "").strip():
        patch["name"] = str(name).strip()
    if reset:
        patch["reset"] = True
    if not patch:
        return _refused("seq update: nothing to change (give params, a name or reset)")
    answer = _call("/api/seq/update", "POST", {"uid": uid, "patch": patch})
    return _changed(answer, "Updated the block.")


def seq_delete(uid: str) -> Dict[str, Any]:
    """Remove one block from the sequence."""
    uid = str(uid or "").strip()
    if not uid:
        return _refused("seq delete: say which block (its uid)")
    return _changed(_call("/api/seq/delete", "POST", {"uid": uid}), "Removed the block.")


def seq_move(uid: str, index: int) -> Dict[str, Any]:
    """Move one block to a new 0-based position."""
    uid = str(uid or "").strip()
    if not uid:
        return _refused("seq move: say which block (its uid)")
    try:
        at = max(0, int(index))
    except (TypeError, ValueError):
        return _refused("seq move: give the new position (a whole number)")
    return _changed(_call("/api/seq/move", "POST", {"uid": uid, "index": at}), "Moved the block.")


def seq_duplicate(uid: str) -> Dict[str, Any]:
    """Copy one block right after itself."""
    uid = str(uid or "").strip()
    if not uid:
        return _refused("seq duplicate: say which block (its uid)")
    answer = _call("/api/seq/duplicate", "POST", {"uid": uid})
    name = _item_name(answer["data"])
    return _changed(answer, ("Copied it as %s." % name) if name else "Copied the block.")


def seq_clear(keep_done: bool = False) -> Dict[str, Any]:
    """Empty the sequence (keep_done=True keeps the blocks that finished)."""
    return _changed(_call("/api/seq/clear", "POST", {"keep_done": bool(keep_done)}), "Cleared the sequence.")


def seq_rename(name: str) -> Dict[str, Any]:
    """Name the live sequence (saved sequences keep their own names)."""
    name = str(name or "").strip()
    if not name:
        return _refused("seq rename: give the sequence a name")
    return _changed(_call("/api/seq/rename", "POST", {"name": name}), "Renamed the sequence.")


CONTROL_ACTIONS = ("start", "pause", "pause_rep", "next_rep", "skip", "stop", "reset", "end_wait", "set_wait")


def control(action: str, **kw: Any) -> Dict[str, Any]:
    """POST /api/control: action plus whatever it takes (want, uid, minutes, failed, failed_uids, reason).

    stop/skip/reset and the like are the caller's to confirm: this only sends what it is given."""
    action = str(action or "").strip().lower()
    if action == "safe_stop":     # the page's SAFE STOP button, as /api/control's action set has it
        return safe_stop(str(kw.get("reason") or ""))
    if not action:
        return _refused("control: say which action")
    if action not in CONTROL_ACTIONS:
        return _refused("control: unknown action %r (one of %s)" % (action, ", ".join(CONTROL_ACTIONS)))
    body: Dict[str, Any] = {"action": action}
    for key in ("want", "uid", "minutes", "failed", "failed_uids", "reason"):
        if kw.get(key) is not None:
            body[key] = kw[key]
    answer = _call("/api/control", "POST", body)
    if not answer["ok"]:
        return {"ok": False, "error": answer["error"], "state": None, "message": ""}
    data = answer["data"] if isinstance(answer["data"], dict) else {}
    state = data.get("state") if isinstance(data.get("state"), dict) else None
    return {"ok": True, "error": None, "state": state, "message": "The panel carried out %s." % action}


def safe_stop(reason: str = "") -> Dict[str, Any]:
    """Stop whatever runs, then run the configured safe-stop script (POST /api/safe_stop)."""
    answer = _call("/api/safe_stop", "POST", {"reason": str(reason or "") or "user"})
    if not answer["ok"]:
        return {"ok": False, "error": answer["error"], "state": None, "message": ""}
    data = answer["data"] if isinstance(answer["data"], dict) else {}
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    state = data.get("state") if isinstance(data.get("state"), dict) else None
    said = str(result.get("message") or "").strip()
    return {"ok": bool(data.get("ok", True)), "error": None, "state": state,
            "message": said or "Safe stop started."}


# ---------------------------------------------------------------- saved sequences (writes)

def _saved_id(item_id: str = "", name: str = "") -> Dict[str, Any]:
    """The saved sequence an id or a name means: {"ok", "id", "error", "name"}.

    A name is only used when it maps to exactly one saved sequence (the panel loads by id)."""
    wanted = str(item_id or "").strip()
    if wanted:
        return {"ok": True, "id": wanted, "name": ""}
    label = str(name or "").strip()
    if not label:
        return {"ok": False, "id": "", "name": "",
                "error": "say which saved sequence (its id, or its name)"}
    listed = saved()
    if not listed["ok"]:
        return {"ok": False, "id": "", "name": "", "error": listed["error"]}
    hits = [s for s in listed["saved"] if str(s.get("name") or "") == label]
    if len(hits) == 1:
        return {"ok": True, "id": str(hits[0].get("id") or ""), "name": label}
    if not hits:
        return {"ok": False, "id": "", "name": "",
                "error": 'no saved sequence is called "%s"' % label}
    return {"ok": False, "id": "", "name": "",
            "error": '%d saved sequences are called "%s": say which by id' % (len(hits), label)}


def saved_save(name: str = "", overwrite: bool = False) -> Dict[str, Any]:
    """Save the live sequence under a name (overwrite=True replaces one saved with that name)."""
    name = str(name or "").strip()
    if not name:
        return _refused("saved save: give the sequence a name")
    answer = _call("/api/saved/save", "POST", {"name": name, "overwrite": bool(overwrite)})
    if not answer["ok"]:
        return {"ok": False, "error": answer["error"], "state": None, "message": ""}
    data = answer["data"] if isinstance(answer["data"], dict) else {}
    state = data.get("state") if isinstance(data.get("state"), dict) else None
    return {"ok": True, "error": None, "state": state, "message": 'Saved the sequence as "%s".' % name,
            "entry": data.get("entry") if isinstance(data.get("entry"), dict) else None}


def saved_load(item_id: str = "", name: str = "", append: bool = False,
               replace: bool = False) -> Dict[str, Any]:
    """Load a saved sequence into the live one: append=True adds it after what is there, replace=True
    discards the live blocks (the panel refuses a load over a sequence with progress until one is given)."""
    if append and replace:
        return _refused("saved load: append and replace are different - choose one")
    found = _saved_id(item_id, name)
    if not found["ok"]:
        return _refused("saved load: " + found["error"])
    body: Dict[str, Any] = {"id": found["id"]}
    if append:
        body["append"] = True
    if replace:
        body["replace"] = True
    answer = _call("/api/saved/load", "POST", body)
    if not answer["ok"]:
        return {"ok": False, "error": answer["error"], "state": None, "message": ""}
    data = answer["data"] if isinstance(answer["data"], dict) else {}
    state = data.get("state") if isinstance(data.get("state"), dict) else None
    return {"ok": True, "error": None, "state": state,
            "message": ('Appended saved sequence "%s".' % found["name"]) if append
            else ('Loaded saved sequence "%s".' % found["name"])}


def saved_delete(item_id: str = "", name: str = "") -> Dict[str, Any]:
    """Delete a saved sequence (by id, or by a name no other saved sequence has)."""
    found = _saved_id(item_id, name)
    if not found["ok"]:
        return _refused("saved delete: " + found["error"])
    answer = _call("/api/saved/delete", "POST", {"id": found["id"]})
    if not answer["ok"]:
        return {"ok": False, "error": answer["error"], "state": None, "message": ""}
    data = answer["data"] if isinstance(answer["data"], dict) else {}
    state = data.get("state") if isinstance(data.get("state"), dict) else None
    return {"ok": True, "error": None, "state": state,
            "message": 'Deleted saved sequence "%s".' % (found["name"] or found["id"])}


# ---------------------------------------------------------------- importing a .py into the function library

def _script_path(path: Any) -> str:
    """A script's path with forward slashes and no "./" prefix, as /api/scripts lists it."""
    text = str(path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def _find_script(entries: List[Dict[str, Any]], wanted: str) -> Optional[Dict[str, Any]]:
    """The panel's script record for a path (or a file name no other record has), as /api/scripts lists it."""
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
    """A new function as "+ New function" makes one (labMaster/static/app.js): a script block starts with
    {"reps": 1}; the panel gives it its id when the whole library is saved."""
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


def import_script(path: str, name: str = "", kind: str = "script") -> Dict[str, Any]:
    """Put a script from the panel's search dirs into the function library, as "+ New function" does.

    The library is replaced whole (POST /api/palette): the list as /api/palette gives it, the new entry
    first (the page unshifts a new function, app.js), and base_rev = the revision that list was read at -
    which the panel refuses (409) when something else changed the library meanwhile. Returns
    {"ok", "error", "entry", "palette", "message"}; entry is the new function as the panel saved it."""
    wanted = str(path or "").strip()
    if not wanted:
        return {"ok": False, "error": "import: give the script's path, as /api/scripts lists it",
                "entry": None, "palette": None, "message": ""}
    listing = scripts()
    if not listing["ok"]:
        return {"ok": False, "error": listing["error"], "entry": None, "palette": None, "message": ""}
    record = _find_script(listing["scripts"], wanted)
    if record is None:
        return {"ok": False, "entry": None, "palette": None, "message": "",
                "error": "import: %r is not a .py file under the panel's script search dirs "
                         "(see /api/labmaster/scripts)" % wanted}
    library = palette()
    if not library["ok"]:
        return {"ok": False, "error": library["error"], "entry": None, "palette": None, "message": ""}
    target = _script_path(record.get("path") or wanted)
    entries = [dict(e) for e in library["palette"]]
    made = _new_palette_entry(target, name, kind)
    same_target = sum(1 for e in entries if _script_path(e.get("target")) == target)
    answer = _call("/api/palette", "POST", {"palette": [made] + entries, "base_rev": library["rev"]})
    if not answer["ok"]:
        return {"ok": False, "error": answer["error"], "entry": None, "palette": None, "message": ""}
    data = answer["data"] if isinstance(answer["data"], dict) else {}
    saved_palette = data.get("palette")
    saved_palette = [e for e in saved_palette if isinstance(e, dict)] if isinstance(saved_palette, list) else None
    entry: Optional[Dict[str, Any]] = None
    if saved_palette:
        old_ids = {str(e.get("id") or "") for e in entries}
        fresh = [e for e in saved_palette
                 if str(e.get("id") or "") not in old_ids and _script_path(e.get("target")) == target]
        if not fresh:
            fresh = [e for e in saved_palette if str(e.get("id") or "") not in old_ids]
        entry = fresh[0] if fresh else None
    if entry is None:     # the panel answered without the saved list: the entry we sent is what it saved
        entry = dict(made)
    said = 'Added "%s" to the function library.' % (entry.get("name") or made["name"])
    if same_target:
        said += " The library already had %d function%s for that script." % (
            same_target, "" if same_target == 1 else "s")
    return {"ok": True, "error": None, "entry": entry,
            "palette": saved_palette if saved_palette is not None else [made] + entries, "message": said}


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
        return part + " - labMaster is connected."
    return part + " - labMaster isn't running at %s." % (panel.get("url") or panel_url())


def _tile(tile_id: str, title: str, value: str, detail: str, tone: str) -> Dict[str, Any]:
    return {"id": tile_id, "title": title, "value": value, "detail": detail, "tone": tone}


def _down_tiles() -> List[Dict[str, Any]]:
    """The five tiles with the panel down: the page still works, and says why nothing is live."""
    why = "labMaster is not running at %s" % panel_url()
    return [_tile("measurement", "Measurement", "-", why, "down"),
            _tile("history", "History", "-", why, "down"),
            _tile("saved", "Saved sequences", "-", why, "down"),
            _tile("functions", "Functions", "-", why, "down"),
            _tile("panel", "labMaster", "-", why, "down")]


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
    version = panel.get("version") or "an unknown version"
    mode = "SIM" if panel.get("sim") else ("real" if panel.get("sim") is False else "unknown mode")
    return _tile("panel", "labMaster", mode, "labMaster %s at %s" % (version, panel.get("url") or panel_url()),
                 "ok")


def snapshot(home: Optional[HomeStore] = None) -> Dict[str, Any]:
    """The Home page in one answer: {"ok", "panel", "greeting", "tiles", "cards", "updated_at"}.

    With the panel down (or gone mid-read) the page is still usable: ok stays True, panel.ok is False
    and every tile says tone="down". The five tiles come from the panel's /api/state and /api/palette."""
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
        panel = dict(panel, ok=False, error=live.get("error") or "the panel's state could not be read")
        return {"ok": True, "panel": panel, "greeting": _greeting(panel), "tiles": _down_tiles(),
                "cards": cards, "updated_at": updated_at}
    panel_state = live["state"]
    library = palette()
    tiles = [_measurement_tile(panel_state), _history_tile(panel_state), _saved_tile(panel_state),
             _functions_tile(library), _panel_tile(panel)]
    return {"ok": True, "panel": panel, "greeting": _greeting(panel), "tiles": tiles, "cards": cards,
            "updated_at": updated_at}
