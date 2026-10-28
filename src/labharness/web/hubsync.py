"""The Kelvin hub in the portal: connect a person's Kelvin to their hub account, then share their work there.

Connecting works like Sign in with ChatGPT (chatgpt.py), with the hub as the sign-in page: Kelvin opens the hub's
/connect page in the browser with a PKCE challenge, this Kelvin's installation id and its loopback callback
(http://127.0.0.1:<port>/hub/callback). The person signs in there and chooses a lab; the hub sends the browser back
with a one-use code, which Kelvin trades for a token. The token is kept in the person's settings, encrypted with
their password like the API keys, and never goes to the browser or the logs.

While connected, Kelvin sends in the background what its person shares (docs/hub.md), and what was removed since:
- their normal projects and unfiled chats, with whole conversations (collect() in hub/client.py), and
- the finished runs in their data folder that they made, in those projects or in none: the run (its meta.json),
  then its files (data.csv, plots, reports...) one by one. Runs from before Kelvin noted who made them and whether an
  incognito chat did only if the person says so ("Also share older runs").
Never: unpublished, confidential or model-limited projects with their chats and runs, incognito chats and the runs
they started, anyone else's runs, API keys, or ChatGPT and Claude Code sign-ins.

Each person connects as their own installation (the computer's id and their folder's), so two people on one computer
keep their own connections. Connecting again tells the hub the ids this person connected with before, so it moves
what they shared under those to the new one instead of keeping two copies.

What was sent is remembered per person in hub-sync.json (ids and fingerprints, never content), so only what changed
goes again."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import sys
import threading
import time
from datetime import datetime
from http.client import HTTPException
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

from .._version import __version__
from .. import rundata
from ..eventlog import RunFolders
from ..hub.client import PENDING_TTL, HubClient, HubClientError, Http, _http, check_url, collect
from ..hub.db import INSTALLATION_ID, LOCAL_ID, MAX_BATCH, MAX_MESSAGES, MAX_META, HubError, file_parts
from .store import GENERAL, ChatStore

log = logging.getLogger("labharness.hubsync")

SYNC_EVERY = 120                  # seconds between looks for what changed
BATCH_BYTES = 16 * 1024 * 1024    # of JSON in one sync request (the hub takes at most 64 MB)
MAX_FILE = 200 * 1024 * 1024      # a larger run file stays on this computer (the hub's own default limit)
MAX_RUN_FILES = 200               # files sent per run
STATE = "hub-sync.json"
KINDS = ("projects", "chats", "runs")
NETWORK = (OSError, HTTPException, ValueError)   # the hub can't be reached, or answered nonsense
REFUSED = (400, 413)              # the hub won't take this file as it is: again only when it changes
RETRY_FIRST = 300                 # a file the hub failed on (5xx, 429) waits this long, doubling each time...
RETRY_MAX = 4 * 3600              # ...up to this
FAILURES = 3                      # file failures in a row that end a sync: the hub itself is in trouble
OLD = object()                    # a run's meta without "user": made before Kelvin noted who made runs


class Stopped(Exception):
    """The person signed out (or Kelvin is closing) in the middle of a sync: the rest waits for the next one."""


def computer_name() -> str:
    try:
        return socket.gethostname()[:80] or "Kelvin"
    except OSError:
        return "Kelvin"


def _digest(where: str) -> str:
    return hashlib.sha256(where.encode("utf-8")).hexdigest()[:12]


def installation_for(computer: str, folder: Any) -> str:
    """This person's installation on the hub: the computer's id and a digest of their own folder, so two people on
    one computer (or one person with two accounts on it) each keep their own connection. The folder is resolved
    first, so another spelling of it (a short 8.3 name, other case) gives the same id."""
    try:
        where = str(Path(folder).resolve())
    except OSError:
        where = os.path.abspath(str(folder))
    where = os.path.normcase(where)
    return "%s:%s" % (computer, _digest(where.lower() if sys.platform == "win32" else where))


def previous_installations(computer: str, folder: Any, remembered: Any = ()) -> List[str]:
    """The ids this person's Kelvin may have shared under before (latest first, at most 5): the ones it connected
    with, the one Kelvin 0.3.8's hub branch made from the folder as spelled, and the bare computer id (the first
    connections)."""
    current = installation_for(computer, folder)
    as_spelled = "%s:%s" % (computer, _digest(os.path.normcase(os.path.abspath(str(folder)))))
    out: List[str] = []
    for ident in list(remembered) + [as_spelled, computer]:
        if ident and ident != current and ident not in out and INSTALLATION_ID.match(str(ident)):
            out.append(str(ident))
    return out[:5]


def connection_of(token: str) -> str:
    """Which connection hub-sync.json was made for (a digest of the token, never the token)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16] if token else ""


def said(e: HubClientError) -> str:
    """What the hub said, without the status code."""
    return str(e.body["error"]) if isinstance(e.body, dict) and e.body.get("error") else str(e)


# ------------------------------------------------------------------ what a person's data folder shares

def started(meta: Dict[str, Any], folder: Path) -> float:
    try:
        return datetime.fromisoformat(str(meta.get("started"))).timestamp()
    except (TypeError, ValueError):
        try:
            return folder.stat().st_mtime
        except OSError:
            return time.time()


def run_items(runs: Any, shared: Set[str], person: Optional[str] = None,
              old_runs: bool = False) -> Tuple[List[Tuple[Dict[str, Any], Path]], int]:
    """The finished runs (those with a meta.json) that may be shared, oldest first, and how many older ones stay
    here. A run is shared if this person made it (meta "user"; None without accounts), in a shared project or in
    none, and no incognito chat started it. A run of a project that is gone is not shared: Kelvin can't tell whether
    that project was. Runs from before Kelvin noted who made them (no "user"; or made without accounts, now that
    there are) are shared only with old_runs: one may have come from an incognito chat, or from someone else using
    the same data folder."""
    try:
        folders = sorted((d for d in runs.iterdir() if d.is_dir()), key=lambda d: d.name)   # runs: RunFolders
    except OSError:
        return [], 0
    out, held = [], 0
    for d in folders:
        if not LOCAL_ID.match(d.name):
            continue
        try:
            meta = rundata.meta(d)
        except (KeyError, OSError, ValueError):
            continue
        if meta.get("status") in (None, "running"):
            continue   # still running, or never finished
        if not isinstance(meta, dict) or meta.get("incognito") or meta.get("unshared"):
            continue   # from an incognito chat, or one that keeps unshared work's marks
        project = meta.get("project") or None
        project = None if project == GENERAL else project
        if project is not None and project not in shared:
            continue
        owner = OLD if meta.get("legacy") else meta.get("user", OLD)   # legacy: from a queue item of then
        if owner != person:
            if owner is not OLD and owner is not None:
                continue   # someone else's, in a data folder they share with this person
            if not old_runs:
                held += 1
                continue
        out.append(({"id": d.name, "project": project, "device": str(meta.get("device_name") or ""), "meta": meta,
                     "created": started(meta, d)}, d))
    return out, held


def run_files(folder: Path) -> List[Tuple[str, Path, List[int]]]:
    """(name, path, [size, mtime]) of a run's files, as the hub names them; hidden and temporary files stay here."""
    out: List[Tuple[str, Path, List[int]]] = []
    try:
        paths = sorted(p for p in folder.rglob("*") if p.is_file())
    except OSError:
        return out
    for path in paths:
        name = path.relative_to(folder).as_posix()
        if name.startswith(".") or "/." in name or name.endswith(".tmp"):
            continue
        try:
            file_parts(name)
            st = path.stat()
        except (HubError, OSError):
            continue
        out.append((name, path, [st.st_size, st.st_mtime_ns]))
        if len(out) >= MAX_RUN_FILES:
            break
    return out


def batches(todo: List[Tuple[Dict[str, Any], str, int]]) -> Iterator[List[Tuple[Dict[str, Any], str, int]]]:
    """(item, fingerprint, size) in groups the hub takes in one request."""
    batch: List[Tuple[Dict[str, Any], str, int]] = []
    used = 0
    for entry in todo:
        if batch and (len(batch) >= MAX_BATCH or used + entry[2] > BATCH_BYTES):
            yield batch
            batch, used = [], 0
        batch.append(entry)
        used += entry[2]
    if batch:
        yield batch


# ------------------------------------------------------------------ what was sent (hub-sync.json)

def load_state(folder: Optional[Path], url: str) -> Dict[str, Any]:
    try:
        data = json.loads((Path(folder) / STATE).read_text(encoding="utf-8")) if folder else {}
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or data.get("hub") != url:   # another hub: nothing of this person is there yet
        data = {"hub": url}
    sent = data.get("sent") if isinstance(data.get("sent"), dict) else {}
    data["sent"] = {k: dict(sent.get(k) or {}) for k in KINDS}
    data["files"] = data.get("files") if isinstance(data.get("files"), dict) else {}
    data["retry"] = data.get("retry") if isinstance(data.get("retry"), dict) else {}   # run → file → [when, wait]
    return data


def save_state(folder: Path, state: Dict[str, Any]) -> None:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (STATE + ".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, folder / STATE)
    except OSError as e:
        log.warning("could not save %s: %s", STATE, e)


class HubLink:
    """The hub connection of whoever is signed in to Kelvin, and the background sync of what they share.

    settings, store and data_dir() are the signed-in person's (the portal switches them on sign-in, then calls
    use()). A sync works from a snapshot taken while nobody can switch, so one person's work never goes out under
    another's token; it stops early when the person changes."""

    def __init__(self, settings: Any, store: ChatStore, data_dir: Callable[[], Path], http: Http = _http):
        self.settings = settings
        self.store = store
        self.data_dir = data_dir
        self.http = http                  # tests answer with the hub's own app
        self._switch = threading.Lock()   # whose hub this is changes only under it
        self._sync = threading.Lock()     # one sync at a time
        self.gen = 0                      # counts changes of person and of connection: a sync of another stops
        self.folder: Optional[Path] = None
        self.person: Optional[str] = None
        self.url = ""
        self.token = ""
        self.pending: Dict[str, Dict[str, Any]] = {}
        self.syncing = False
        self.stopping = False
        self.wake: Any = None             # an asyncio.Event the portal's sync loop waits on

    def use(self, folder: Optional[Any], person: Optional[str] = None) -> None:
        """Serve the person whose settings are in use now (hub-sync.json goes in their folder; person: their account
        name, None without accounts); folder None: nobody. A connection someone started and didn't finish is
        dropped."""
        with self._switch:
            self.gen += 1
            self.pending = {}
            self.folder = Path(folder) if folder is not None else None
            self.person = person if folder is not None else None
            self.url = (self.settings.get("hub_url") or "") if folder is not None else ""
            self.token = (self.settings.get("hub_token") or "") if folder is not None else ""

    @property
    def connected(self) -> bool:
        return bool(self.folder is not None and self.url and self.token)

    def poke(self) -> None:
        """Sync soon (from the event loop)."""
        if self.wake is not None:
            self.wake.set()

    # ------------------------------------------------------------------ connecting

    def begin(self, url: str, redirect_uri: str) -> str:
        """The hub's /connect page for this attempt, to open in the person's browser."""
        url = check_url(url)
        with self._switch:
            if self.folder is None:
                raise HubClientError("Sign in to Kelvin first.")
            now = time.time()
            self.pending = {s: p for s, p in self.pending.items() if now - p["at"] < PENDING_TTL}
            computer = self.settings.installation_id()
            installation = installation_for(computer, self.folder)
            link, pending = HubClient(url, http=self.http).begin(
                redirect_uri, installation, computer_name(), sys.platform, __version__, self.person or "",
                previous_installations(computer, self.folder, self.settings.hub_installations()))
            self.pending[pending["state"]] = dict(pending, url=url, gen=self.gen, installation=installation)
        return link

    def finish(self, query: Dict[str, str]) -> Dict[str, Any]:
        """Handle the hub's redirect back to Kelvin: trade the code for a token and keep it. Returns who the person
        is on the hub (empty if that can't be asked now). Raises HubClientError, or a NETWORK error."""
        with self._switch:
            p = self.pending.pop(str(query.get("state") or ""), None)
        if p is None:
            raise HubClientError("This connection link has expired or was already used. Start again from Kelvin's "
                                 "Settings.")
        client = HubClient(p["url"], http=self.http)
        token = client.finish(query, p)
        with self._switch:
            mine = p["gen"] == self.gen
            if mine:
                self.gen += 1   # a sync still going with the old token stops at its next request
                self.settings.set_hub(p["url"], token)
                self.settings.add_hub_installation(p["installation"])
                self.url, self.token = p["url"], token
        if not mine:   # someone else signed in to Kelvin meanwhile, or this one disconnected: not theirs to keep
            try:
                client.disconnect()
            except (HubClientError,) + NETWORK:
                pass
            raise HubClientError("Someone else signed in to Kelvin meanwhile, or it was disconnected. Connect again "
                                 "from your own Settings.")
        log.info("connected to the hub at %s", p["url"])   # the next sync sends everything once more
        try:
            return client.me()
        except (HubClientError,) + NETWORK:
            return {}

    def disconnect(self) -> bool:
        """Forget the token here, then end the connection on the hub. True if the hub confirmed it. A sync in
        progress stops at its next request. What was shared stays on the hub (the account page deletes it)."""
        with self._switch:
            self.gen += 1
            url, token = self.url, self.token
            if token:
                self.settings.set_hub(url, None)   # the address stays, for connecting again
                self.token = ""
                log.info("disconnected from the hub at %s", url)
        if not token:
            return False
        try:
            return HubClient(url, token, http=self.http).disconnect()
        except (HubClientError,) + NETWORK:
            return False   # unreachable: forgotten here anyway; the account page there ends it too

    def _forget(self, gen: int) -> None:
        with self._switch:
            if gen == self.gen and self.token:
                self.settings.set_hub(self.url, None)   # the address stays, for connecting again
                self.token = ""
                log.info("disconnected from the hub at %s", self.url)

    # ------------------------------------------------------------------ status

    def status(self, check: bool = True) -> Dict[str, Any]:
        """For the browser: the hub's address, who the person is there and the last sync. Never the token.
        check: ask the hub (GET /api/me)."""
        with self._switch:
            gen, folder, url, token = self.gen, self.folder, self.url, self.token
        state = load_state(folder, url) if folder is not None and url else {}
        if state.get("connection") != connection_of(token):
            state = {}   # made for another connection: this one hasn't synced yet
        out: Dict[str, Any] = {"connected": bool(folder is not None and url and token), "url": url, "account": None,
                               "problem": None, "sync": state.get("last"), "synced_at": state.get("synced_at"),
                               "syncing": self.syncing}
        if out["connected"] and check:
            try:
                me = HubClient(url, token, http=self.http).me()
            except HubClientError as e:
                if e.status == 401:
                    self._forget(gen)
                    out.update(connected=False, problem="The hub ended this connection (it was disconnected there). "
                                                        "Connect again.")
                else:
                    out["problem"] = said(e)
            except NETWORK:
                out["problem"] = "Kelvin can't reach the hub at %s now." % url
            else:
                out["account"] = {"name": me.get("name"), "email": me.get("email"),
                                  "shares_into": me.get("shares_into"), "labs": me.get("labs") or [],
                                  "email_confirmed": bool(me.get("email_confirmed")),
                                  "needs_consent": bool(me.get("needs_consent")),
                                  "account_url": url + "/account", "consent_url": url + "/consent"}
        return out

    # ------------------------------------------------------------------ syncing

    def sync_once(self) -> Optional[Dict[str, Any]]:
        """Send what changed since the last sync, and what was removed. None if not connected."""
        with self._sync:
            with self._switch:
                if not (self.folder is not None and self.url and self.token):
                    return None
                snap = {"gen": self.gen, "folder": self.folder, "url": self.url, "token": self.token,
                        "chats": self.store.root, "runs": RunFolders(self.data_dir()), "person": self.person,
                        "old_runs": self.settings.get("hub_old_runs") == "on"}
            self.syncing = True
            try:
                return self._sync_person(snap)
            finally:
                self.syncing = False

    def _check(self, snap: Dict[str, Any]) -> None:
        if self.stopping or snap["gen"] != self.gen:
            raise Stopped()

    def _sync_person(self, snap: Dict[str, Any]) -> Dict[str, Any]:
        client = HubClient(snap["url"], snap["token"], http=self.http)
        state = load_state(snap["folder"], snap["url"])
        if state.get("connection") != connection_of(snap["token"]):
            # a new connection: send everything once more, keeping the ids for what was removed since
            state["sent"] = {k: {i: "" for i in state["sent"][k]} for k in KINDS}
            state.update(files={}, retry={}, connection=connection_of(snap["token"]))
            state.pop("last", None)
            state.pop("synced_at", None)
        last: Dict[str, Any] = {"at": time.time(), "ok": False, "sent": {k: 0 for k in KINDS}, "withheld": 0,
                                "removed": 0, "files": 0, "refused": [], "older_runs": 0, "error": None}
        try:
            self._send(client, snap, state, last)
            last["ok"] = True
            state["synced_at"] = last["at"]
        except Stopped:
            last["error"] = "Interrupted; Kelvin goes on at the next sync."
        except HubClientError as e:
            if e.status == 401 and snap["gen"] != self.gen:
                last["error"] = "Interrupted; Kelvin goes on at the next sync."
            elif e.status == 401:
                self._forget(snap["gen"])
                last["error"] = "The hub ended this connection. Connect again in Settings → Account."
            elif e.status == 403:
                last["error"] = "The hub's terms changed: agree to them on the hub, and Kelvin goes on sharing."
                last["consent"] = True
            else:
                last["error"] = said(e)
        except NETWORK:
            last["error"] = "Kelvin can't reach the hub at %s now; it tries again in a few minutes." % snap["url"]
        last["refused"] = last["refused"][:20]
        state["last"] = last
        save_state(snap["folder"], state)
        if last["error"]:
            log.info("hub sync: %s", last["error"])
        return last

    def _send(self, client: HubClient, snap: Dict[str, Any], state: Dict[str, Any], last: Dict[str, Any]) -> None:
        got = collect(ChatStore(snap["chats"], create=False))   # its own view of the folder: a switch can't move it
        shared = {p["id"] for p in got["projects"] if p.get("name")}
        runs, last["older_runs"] = run_items(snap["runs"], shared, snap["person"], snap["old_runs"])
        current = {"projects": got["projects"], "chats": got["chats"], "runs": [item for item, _ in runs]}
        refused_now: Set[str] = set()
        for kind in KINDS:   # projects first: the hub files chats and runs under projects it already has
            todo = []
            for item in current[kind]:
                text = json.dumps(item, sort_keys=True, ensure_ascii=False)
                fp = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
                if state["sent"][kind].get(item["id"]) == fp:
                    continue
                size = len(text.encode("utf-8"))
                if size > (MAX_MESSAGES if kind == "chats" else MAX_META if kind == "runs" else BATCH_BYTES):
                    last["refused"].append({kind[:-1]: item["id"], "why": "larger than the hub keeps"})
                    refused_now.add(item["id"])
                    state["sent"][kind][item["id"]] = fp
                    continue
                todo.append((item, fp, size))
            for batch in batches(todo):
                self._check(snap)
                done = client.push(**{kind: [item for item, _, _ in batch]}, version=__version__)
                why = {str(r.get(kind[:-1])): r.get("why") for r in done.get("refused") or [] if isinstance(r, dict)}
                for item, fp, _ in batch:
                    state["sent"][kind][item["id"]] = fp
                    if item["id"] in why:
                        refused_now.add(item["id"])
                        last["refused"].append({kind[:-1]: item["id"], "why": why[item["id"]]})
                        if item.get("project") and "project" in str(why[item["id"]]):
                            # the hub doesn't have its project (as after a new hub account): both go again next time
                            state["sent"][kind][item["id"]] = ""
                            state["sent"]["projects"][item["project"]] = ""
                last["sent"][kind] += int(done.get(kind) or 0)
                last["withheld"] += int(done.get("withheld") or 0)
                save_state(snap["folder"], state)

        gone = {k: sorted(set(state["sent"][k]) - {item["id"] for item in current[k]}) for k in KINDS}
        while any(gone.values()):
            self._check(snap)
            chunk = {k: ids[:MAX_BATCH] for k, ids in gone.items() if ids}
            done = client.push(removed=chunk, version=__version__)
            for k, ids in chunk.items():
                for i in ids:
                    state["sent"][k].pop(i, None)
                    if k == "runs":
                        state["files"].pop(i, None)
                        state["retry"].pop(i, None)
            last["removed"] += int(done.get("removed") or 0)
            gone = {k: ids[MAX_BATCH:] for k, ids in gone.items()}
            save_state(snap["folder"], state)

        tried = failures = 0
        for item, folder in reversed(runs):   # newest first
            if item["id"] in refused_now or item["id"] not in state["sent"]["runs"]:
                continue
            have = state["files"].setdefault(item["id"], {})
            later = state["retry"].get(item["id"]) or {}
            for name, path, fp in run_files(folder):
                if have.get(name) == fp or (later.get(name) or [0])[0] > time.time():
                    continue   # sent, or the hub failed on it lately: later
                self._check(snap)
                if fp[0] > MAX_FILE:
                    last["refused"].append({"run": item["id"], "file": name, "why": "larger than 200 MB"})
                else:
                    try:
                        data = path.read_bytes()
                    except OSError:
                        continue   # being written, or gone: next time
                    self._check(snap)
                    try:
                        client.upload(item["id"], name, data)
                        last["files"] += 1
                        failures = 0
                    except (HubClientError,) + NETWORK as e:
                        status = e.status if isinstance(e, HubClientError) else None
                        if isinstance(e, HubClientError) and not (status == 429 or (status or 0) >= 500):
                            if status not in REFUSED:
                                raise   # signed out there, or new terms
                            last["refused"].append({"run": item["id"], "file": name, "why": said(e)})
                        else:   # the hub failed on this one, or the connection broke while it went (a timeout on a
                            # big file, say, or the hub closing early after refusing it): the others first
                            wait = min(RETRY_MAX, 2 * later[name][1]) if later.get(name) else RETRY_FIRST
                            later = state["retry"].setdefault(item["id"], {})
                            later[name] = [time.time() + wait, wait]
                            why = said(e) if isinstance(e, HubClientError) else \
                                "the connection broke while it went (%s)" % type(e).__name__
                            last["refused"].append({"run": item["id"], "file": name, "again": wait,
                                                    "why": why + "; Kelvin tries it again later"})
                            failures += 1
                            save_state(snap["folder"], state)
                            if failures >= FAILURES:
                                raise
                            continue
                have[name] = fp   # sent, or refused: again only when it changes
                later.pop(name, None)
                tried += 1
                if tried % 20 == 0:
                    save_state(snap["folder"], state)
            if not state["retry"].get(item["id"]):
                state["retry"].pop(item["id"], None)
