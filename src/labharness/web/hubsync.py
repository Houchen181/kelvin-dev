"""The Kelvin hub in the portal: what a person signed in with their Kelvin account shares there, in the background.

Sharing goes over the person's Kelvin account sign-in (central.py): the hub they sign in to, and the token it gave this
computer for them (account_token in their settings, encrypted with their password). It has no connecting of its own,
and it is off unless the person switches it on (Settings → Account: hub_share). Nothing goes before they agree to the
hub's terms: the hub refuses a sync until then, and the portal says so (brief()).

While it is on, Kelvin sends what changed (docs/hub.md), and what was removed since:
- their normal projects and unfiled chats, with whole conversations (collect() in hub/client.py), and
- the records of the finished runs in their data folder that they made, in those projects or in none: each run's meta
  with its card (its plan, what it swept and measured, its results and assessment: with_card()). Runs from before
  Kelvin noted who made them and whether an incognito chat did go only if the person says so ("Also share older
  runs").
Never: a run's data files (run.h5, plots, reports stay on this computer), unpublished, confidential or model-limited
projects with their chats and runs, incognito chats and the runs they started, anyone else's runs, API keys, or ChatGPT
and Claude Code sign-ins.

Switched off, Kelvin takes back from the hub what it shared there, then sends nothing. Switched on again, it shares
everything once more. Before Kelvin 0.3.51 it was on unless switched off, which kept nothing in the settings: for a
person who never chose, Kelvin now takes back what it shared then, the same way.

What was sent is remembered per person in hub-sync.json (ids and fingerprints, never content), with the hub account
and computer it went to: only what changed goes again, also after the next sign-in, which brings a new token."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime
from http.client import HTTPException
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

from .._version import __version__
from .. import rundata
from ..hub.client import HubClient, HubClientError, Http, _http, collect
from ..hub.db import LOCAL_ID, MAX_BATCH, MAX_MESSAGES, MAX_META
from ..eventlog import RunFolders
from .store import GENERAL, ChatStore

log = logging.getLogger("labharness.hubsync")

SYNC_EVERY = 120                  # seconds between looks for what changed
BATCH_BYTES = 16 * 1024 * 1024    # of JSON in one sync request (the hub takes at most 64 MB)
STATE = "hub-sync.json"
KINDS = ("projects", "chats", "runs")
NETWORK = (OSError, HTTPException, ValueError)   # the hub can't be reached, or answered nonsense
OLD = object()                    # a run's meta without "user": made before Kelvin noted who made runs
INTERRUPTED = "Interrupted; Kelvin goes on at the next sync."
SIGNED_OUT = ("The hub signed this computer out (a password reset, or the account was disabled there). Sign out of "
              "Kelvin and in again, and Kelvin goes on sharing.")
TERMS = ("The hub's terms changed: agree to them in Kelvin (the notice at its top, or Settings → Account), and Kelvin "
         "goes on sharing.")
NOT_TAKEN = "The hub doesn't take shared work yet; Kelvin tries again later."


class Stopped(Exception):
    """The person signed out, their sign-in or choice changed, or Kelvin is closing, in the middle of a sync: the rest
    waits for the next one."""


def connection_of(place: str) -> str:
    """Which place on the hub hub-sync.json was made for: a digest of the account and the computer, never a token."""
    return hashlib.sha256(place.encode("utf-8")).hexdigest()[:16] if place else ""


def said(e: HubClientError) -> str:
    """What the hub said, without the status code."""
    return str(e.body["error"]) if isinstance(e.body, dict) and e.body.get("error") else str(e)


def terms_of(url: str, answer: Any) -> Dict[str, str]:
    """What Kelvin shows its person before they agree to the hub's terms in force, here in Kelvin: their version (from
    the hub's answer), and the hub's pages to read them on, at the address Kelvin reaches the hub by; the terms say
    what changed. An older hub says no consent_version: there they agree on its own page (consent_url)."""
    a = answer if isinstance(answer, dict) else {}
    return {"consent_url": url + "/consent", "consent_version": str(a.get("consent_version") or ""),
            "terms_url": url + "/terms", "privacy_url": url + "/privacy"}


def wants_terms(e: HubClientError) -> bool:
    """The hub refused because the person hasn't agreed to its new terms yet."""
    body = e.body if isinstance(e.body, dict) else {}
    return e.status == 403 and bool(body.get("consent_url") or body.get("needs_consent")
                                    or "terms" in str(body.get("error") or ""))


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
              old_runs: bool = False) -> Tuple[List[Dict[str, Any]], int]:
    """The records of the finished runs (those with a meta.json) that may be shared, oldest first, and how many older
    ones stay here. A run is shared if this person made it (meta "user"; None without accounts), in a shared project
    or in none, and no incognito chat started it, nor work under model rules or marked confidential or unpublished
    (the run keeps them, whatever its project is marked later). A run of a project that is gone is not shared: Kelvin
    can't tell whether that project was. Runs from before Kelvin noted who made them (no "user"; or made without
    accounts, now that there are) are shared only with old_runs: one may have come from an incognito chat, or from
    someone else using the same data folder. Only the record goes: its files stay here."""
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
        if not isinstance(meta, dict) or any(meta.get(k) for k in ("incognito", "unshared", "confidential",
                                                                    "unpublished", "rules")):
            continue   # from an incognito chat, one that keeps unshared work's marks, or work under its rules or marks
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
        out.append(with_card({"id": d.name, "project": project, "device": str(meta.get("device_name") or ""),
                              "meta": meta, "created": started(meta, d)}, d))
    return out, held


def with_card(item: Dict[str, Any], folder: Path) -> Dict[str, Any]:
    """A run's record with its card in its meta (rundata.card: its plan, what it swept, measured and held, its results,
    assessment and issues), while that stays within what the hub keeps of a run (MAX_META). A card that doesn't fit,
    or can't be read, is left out, never the run. A card that changes (a fit or a re-analysis after the run ended)
    changes the record, so it goes again."""
    try:
        card = rundata.card(folder)
    except Exception:   # an unreadable card never keeps the run's record from going
        log.debug("no card for %s", folder.name, exc_info=True)
        return item
    if not isinstance(card, dict) or not card:
        return item
    fuller = dict(item, meta=dict(item["meta"], card=card))
    try:
        size = len(json.dumps(fuller, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return item
    return fuller if size <= MAX_META else item


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
    data.pop("files", None)   # Kelvin 0.3.46 and before also sent runs' files, and kept which here: no more
    data.pop("retry", None)
    return data


def save_state(folder: Path, state: Dict[str, Any]) -> None:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (STATE + ".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, folder / STATE)
    except OSError as e:
        log.warning("could not save %s: %s", STATE, e)


def on_the_hub(state: Dict[str, Any]) -> Dict[str, int]:
    """How many chats and run records this Kelvin has on the hub, by what it remembers sending there."""
    return {k: len(state.get("sent", {}).get(k) or {}) for k in ("chats", "runs")}


class HubLink:
    """What the person signed in to Kelvin shares on their Kelvin hub, and the background sync that sends it.

    settings, store and data_dir() are the signed-in person's (the portal switches them on sign-in, then calls use()).
    account() gives (the hub, the person's token there, the place on the hub: their account and this computer) while
    they are signed in with a Kelvin account, else None. It is read again at every sync, so a new sign-in's token goes
    at once. A sync works from a snapshot taken while nobody can switch, so one person's work never goes out under
    another's token; it stops early when the person, their sign-in or their choice changes (use(), refresh())."""

    def __init__(self, settings: Any, store: ChatStore, data_dir: Callable[[], Path],
                 account: Optional[Callable[[], Optional[Tuple[str, str, str]]]] = None, http: Http = _http):
        self.settings = settings
        self.store = store
        self.data_dir = data_dir
        self.account = account or (lambda: None)
        self.http = http                  # tests answer with the hub's own app
        self._switch = threading.Lock()   # whose sharing this is changes only under it
        self._sync = threading.Lock()     # one sync at a time
        self.gen = 0                      # counts changes of person, sign-in and choice: a sync of another stops
        self.folder: Optional[Path] = None
        self.person: Optional[str] = None
        self.terms: Optional[Dict[str, str]] = None   # the hub's new terms while it waits for them (terms_of, brief())
        self.syncing = False
        self.stopping = False
        self.wake: Any = None             # an asyncio.Event the portal's sync loop waits on

    def use(self, folder: Optional[Any], person: Optional[str] = None) -> None:
        """Serve the person whose settings are in use now (hub-sync.json goes in their folder; person: their account
        name, None without accounts); folder None: nobody."""
        with self._switch:
            self.gen += 1
            self.folder = Path(folder) if folder is not None else None
            self.person = person if folder is not None else None
            self.terms = None
        forget = getattr(self.settings, "forget_hub_connection", None)
        if folder is not None and forget is not None:
            forget()   # a connection of its own, from Kelvin 0.3.9 to 0.3.12: gone

    def refresh(self) -> None:
        """The person's sign-in or their choice changed: a sync still going with what was before stops at its next
        request, and the next one goes with what is now (the portal wakes its loop after this)."""
        with self._switch:
            self.gen += 1

    def wait_for_terms(self, terms: Optional[Dict[str, str]]) -> None:
        """The hub waits for the person to agree to its new terms (terms_of; None: it doesn't), as their sign-in said,
        or they agreed to them here in Kelvin."""
        with self._switch:
            self.terms = terms or None

    def _terms_now(self, gen: int, terms: Optional[Dict[str, str]]) -> None:
        with self._switch:
            if gen == self.gen:
                self.terms = terms or None

    def _link(self) -> Optional[Tuple[str, str, str]]:
        try:
            got = self.account()
        except Exception:   # never let a question about the sign-in end the syncing
            log.exception("the hub sign-in")
            return None
        if not got or not all(got):
            return None
        url, token, place = got
        return str(url).rstrip("/"), str(token), str(place)

    @property
    def linked(self) -> bool:
        """Someone is signed in with a Kelvin account: Kelvin may share on their hub, or take back what it shared."""
        return self.folder is not None and self._link() is not None

    @property
    def sharing(self) -> bool:
        """The person's choice (Settings → Account): off unless switched on."""
        return self.settings.get("hub_share") == "on"

    def poke(self) -> None:
        """Sync soon (from the event loop)."""
        if self.wake is not None:
            self.wake.set()

    # ------------------------------------------------------------------ status

    def brief(self) -> Optional[Dict[str, Any]]:
        """For the portal's poll, without asking the hub: the hub's new terms (terms_of), while Kelvin waits for the
        person to agree to them before it shares; None otherwise."""
        with self._switch:
            terms = self.terms if self.folder is not None else None
        return dict(terms) if terms and self.sharing else None

    def status(self, check: bool = True) -> Dict[str, Any]:
        """For the browser: whether Kelvin shares and where, who the person is on the hub, what of theirs is there and
        the last sync. Never the token. check: ask the hub (GET /api/me)."""
        with self._switch:
            gen, folder = self.gen, self.folder
        link = self._link() if folder is not None else None
        url, token, place = link or ("", "", "")
        state = load_state(folder, url) if folder is not None and url else {}
        if state.get("connection") != connection_of(place):
            state = {}   # made for another account or computer: nothing of this one is there yet
        sharing, there = self.sharing, on_the_hub(state)
        out: Dict[str, Any] = {"signed_in": bool(link), "sharing": sharing, "url": url, "account": None,
                               "problem": None, "waiting": None, "sync": state.get("last"),
                               "synced_at": state.get("synced_at"), "syncing": self.syncing, "on_hub": there,
                               "taking_back": bool(link and not sharing and any(state.get("sent", {}).values()))}
        if link and check:
            try:
                me = HubClient(url, token, http=self.http).me()
            except HubClientError as e:
                if e.status == 401:
                    out["problem"] = SIGNED_OUT
                elif e.status == 404:
                    out["waiting"] = NOT_TAKEN
                else:
                    out["problem"] = said(e)
            except NETWORK:
                out["problem"] = "Kelvin can't reach the hub at %s now." % url
            else:
                consent, terms = bool(me.get("needs_consent")), terms_of(url, me)
                self._terms_now(gen, terms if consent else None)
                out["account"] = dict(terms, name=me.get("name"), email=me.get("email"),
                                      email_confirmed=bool(me.get("email_confirmed")), needs_consent=consent,
                                      account_url=url + "/account")
        return out

    # ------------------------------------------------------------------ syncing

    def sync_once(self) -> Optional[Dict[str, Any]]:
        """Send what changed since the last sync, and what was removed; switched off, take back what was shared.
        None when nobody is signed in with a Kelvin account here, or it is switched off with nothing of theirs on
        the hub."""
        with self._sync:
            with self._switch:
                link = self._link() if self.folder is not None else None
                if link is None:
                    return None
                snap = {"gen": self.gen, "folder": self.folder, "url": link[0], "token": link[1], "place": link[2],
                        "chats": self.store.root, "runs": RunFolders(self.data_dir()), "person": self.person,
                        "old_runs": self.settings.get("hub_old_runs") == "on", "share": self.sharing}
            if not snap["share"]:
                state = load_state(snap["folder"], snap["url"])
                if state.get("connection") != connection_of(snap["place"]) or not any(state["sent"].values()):
                    return None   # nothing of theirs there to take back
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
        if state.get("connection") != connection_of(snap["place"]):
            # another account or computer on this hub (or what Kelvin 0.3.12 connected on its own): everything once
            # more, keeping the ids for what was removed since
            state["sent"] = {k: {i: "" for i in state["sent"][k]} for k in KINDS}
            state["connection"] = connection_of(snap["place"])
            state.pop("last", None)
            state.pop("synced_at", None)
        last: Dict[str, Any] = {"at": time.time(), "ok": False, "sharing": snap["share"], "sent": {k: 0 for k in KINDS},
                                "withheld": 0, "removed": 0, "refused": [], "older_runs": 0, "error": None}
        try:
            self._send(client, snap, state, last)
            last["ok"] = True
            state["synced_at"] = last["at"]
            self._terms_now(snap["gen"], None)
        except Stopped:
            last["error"] = INTERRUPTED
        except HubClientError as e:
            if snap["gen"] != self.gen:
                last["error"] = INTERRUPTED   # a new sign-in or person: never this one's answer
            elif e.status == 401:   # the sign-in's token is the sign-in's to keep: Kelvin never forgets it here
                last["error"], last["signed_out"] = SIGNED_OUT, True
            elif wants_terms(e):
                last["error"], last["consent"] = TERMS, True
                self._terms_now(snap["gen"], terms_of(snap["url"], e.body))
            elif e.status == 404:   # a hub that takes no shared work (yet): said quietly, never an error
                last["waiting"] = NOT_TAKEN
            else:
                last["error"] = said(e)
        except NETWORK:
            last["error"] = "Kelvin can't reach the hub at %s now; it tries again in a few minutes." % snap["url"]
        last["refused"] = last["refused"][:20]
        before = (state.get("last") or {}).get("error")
        state["last"] = last
        save_state(snap["folder"], state)
        if last["error"] and last["error"] != before:   # said once, not every two minutes while it lasts
            log.info("hub sync: %s", last["error"])
        return last

    def _send(self, client: HubClient, snap: Dict[str, Any], state: Dict[str, Any], last: Dict[str, Any]) -> None:
        if snap["share"]:
            got = collect(ChatStore(snap["chats"], create=False))   # its own view of the folder: a switch can't move it
            shared = {p["id"] for p in got["projects"] if p.get("name")}
            runs, last["older_runs"] = run_items(snap["runs"], shared, snap["person"], snap["old_runs"])
            current = {"projects": got["projects"], "chats": got["chats"], "runs": runs}
        else:   # switched off: everything it shared there goes from the hub
            current = {k: [] for k in KINDS}
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
            last["removed"] += int(done.get("removed") or 0)
            gone = {k: ids[MAX_BATCH:] for k, ids in gone.items()}
            save_state(snap["folder"], state)
