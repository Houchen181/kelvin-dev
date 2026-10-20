"""A Kelvin's side of the hub: connect through the browser, then send what its person shares.

    client = HubClient("https://hub.example.org")
    url, pending = client.begin("http://127.0.0.1:8770/hub/callback", host_id, "Lab PC 2", "win32", "0.3.8", "Ada")
    # open url in the person's browser; the hub sends it back to the callback with ?code=...&state=...
    token = client.finish(query, pending)    # keep it in the person's settings (encrypted with their password)
    client.push(**collect(store))            # their shared projects and chats (ChatStore), never the others

The hub applies the same rules again on its side (see db.py), so a Kelvin that sends too much is refused there."""
from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from .db import challenge_of, project_shared, restricted

PENDING_TTL = 600

Http = Callable[..., Tuple[int, Any]]


class HubClientError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status = status   # the hub's HTTP status, when it answered (401: this Kelvin isn't connected any more)
        self.body = body


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is answered as the 3xx it is, never followed: urllib would send the token on to wherever it points,
    plain http included."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


def _http(method: str, url: str, body: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any]:
    """(status, parsed JSON or text). Never raises for an HTTP error status, and never follows a redirect."""
    from .._version import __version__
    req = urllib.request.Request(url, data=body, method=method, headers=dict(
        {"User-Agent": "Kelvin/%s" % __version__, "Accept": "application/json"}, **(headers or {})))
    try:
        with _OPENER.open(req, timeout=60) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, json.loads(raw) if raw else {}
    except ValueError:
        return status, raw.decode("utf-8", "replace")


def check_url(base_url: str) -> str:
    """A hub on the internet needs https; plain http only on this computer."""
    url = str(base_url or "").strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return url
    if parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1"):
        return url
    raise HubClientError("The hub's address must start with https:// (http:// only for this computer).")


def shareable_chat(chat: Dict[str, Any]) -> bool:
    """Not incognito, and never in a project that isn't shared: a chat moved out of one keeps its mark."""
    return not (chat.get("incognito") or chat.get("unpublished") or chat.get("confidential")
                or restricted(chat.get("rules")))


def collect(store: Any) -> Dict[str, List[Dict[str, Any]]]:
    """What a ChatStore shares: its shared projects, and the chats in them and unfiled chats, with their whole
    conversations. An unshared project goes only as its id and "not shared" (never its name), so the hub drops
    whatever it had of it. Projects and chats carry their marks (all clear, or they wouldn't be sent), so the hub
    can check them again."""
    projects = store.projects()
    shared = {p["id"] for p in projects if project_shared(p)}
    out_projects = [{"id": p["id"], "name": p["name"], "unpublished": False, "confidential": False,
                     "rules": p.get("rules")} if p["id"] in shared else {"id": p["id"], "shared": False}
                    for p in projects]
    chats = []
    for summary in store.list():
        if summary.get("project") and summary["project"] not in shared:
            continue
        if not shareable_chat(summary):
            continue
        try:
            chat = store.load(summary["id"])
        except (KeyError, OSError, ValueError):
            continue
        if not shareable_chat(chat):
            continue
        chats.append({"id": chat["id"], "project": summary.get("project"), "title": chat.get("title", "Chat"),
                      "updated": chat.get("updated"), "messages": chat.get("messages", []),
                      "rules": chat.get("rules"), "unpublished": False, "confidential": False, "incognito": False})
    return {"projects": out_projects, "chats": chats}


class HubClient:
    def __init__(self, base_url: str, token: Optional[str] = None, http: Http = _http):
        self.base_url = check_url(base_url)
        self.token = token
        self.http = http

    # ------------------------------------------------------------------ connecting

    def begin(self, redirect_uri: str, installation: str, name: str = "", platform: str = "",
              version: str = "", person: str = "", previous: Any = ()) -> Tuple[str, Dict[str, Any]]:
        """The hub's /connect URL for the browser, and what finish() needs back. person: who is signed in to this
        Kelvin (its local account), for the hub to show. previous: up to 5 earlier installation ids of this person's
        Kelvin, whose shared work the hub moves to this one."""
        if not redirect_uri.startswith("http://127.0.0.1:"):
            raise HubClientError("the callback must be http://127.0.0.1:<port>/...")
        verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(24)
        params = {"redirect_uri": redirect_uri, "state": state, "code_challenge": challenge_of(verifier),
                  "code_challenge_method": "S256", "installation": installation, "name": name,
                  "platform": platform, "version": version}
        if person:
            params["person"] = person
        previous = [str(i) for i in previous if i and str(i) != installation][:5]
        if previous:
            params["previous"] = ",".join(previous)
        pending = {"state": state, "verifier": verifier, "redirect_uri": redirect_uri, "at": time.time()}
        return self.base_url + "/connect?" + urllib.parse.urlencode(params), pending

    def finish(self, query: Dict[str, str], pending: Dict[str, Any]) -> str:
        """Handle the hub's redirect back to Kelvin: trade the code for a token, and keep it."""
        if str(query.get("state") or "") != pending["state"] or time.time() - pending["at"] > PENDING_TTL:
            raise HubClientError("This connection link has expired or was already used. Start again.")
        if query.get("error"):
            raise HubClientError("Connecting was cancelled." if query["error"] == "access_denied"
                                 else "The hub said: %s" % query["error"])
        status, body = self._json("POST", "/api/token", {"grant_type": "authorization_code", "code": query.get("code"),
                                                         "code_verifier": pending["verifier"],
                                                         "redirect_uri": pending["redirect_uri"]}, auth=False)
        if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
            detail = (body.get("error_description") or body.get("error")) if isinstance(body, dict) else str(body)[:200]
            raise HubClientError("The hub did not complete the connection: %s" % detail, status, body)
        self.token = body["access_token"]
        return self.token

    def disconnect(self) -> bool:
        """End this connection on the hub. True only if the hub confirmed it (204)."""
        if not self.token:
            return False
        try:
            status, _ = self._json("DELETE", "/api/token")
        finally:
            self.token = None
        return status == 204

    # ------------------------------------------------------------------ using it

    def me(self) -> Dict[str, Any]:
        return self._ok(*self._json("GET", "/api/me"))

    def push(self, projects: Optional[List[Dict[str, Any]]] = None, chats: Optional[List[Dict[str, Any]]] = None,
             runs: Optional[List[Dict[str, Any]]] = None, removed: Optional[Dict[str, List[str]]] = None,
             version: str = "") -> Dict[str, Any]:
        payload = {"projects": projects or [], "chats": chats or [], "runs": runs or [], "removed": removed or {}}
        if version:
            payload["installation"] = {"version": version}
        return self._ok(*self._json("POST", "/api/sync", payload))

    def upload(self, run_id: str, name: str, data: bytes) -> Dict[str, Any]:
        """One of a run's files (send the run with push() first)."""
        path = "/api/sync/runs/%s/files/%s" % (urllib.parse.quote(run_id, safe=""), urllib.parse.quote(name))
        return self._ok(*self.http("PUT", self.base_url + path, data, dict(
            self._auth(), **{"Content-Type": "application/octet-stream"})))

    # ------------------------------------------------------------------ plumbing

    def _auth(self) -> Dict[str, str]:
        if not self.token:
            raise HubClientError("Kelvin is not connected to the hub.")
        return {"Authorization": "Bearer %s" % self.token}

    def _json(self, method: str, path: str, body: Any = None, auth: bool = True) -> Tuple[int, Any]:
        headers = dict(self._auth()) if auth else {}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        return self.http(method, self.base_url + path, data, headers)

    @staticmethod
    def _ok(status: int, body: Any) -> Dict[str, Any]:
        if status == 200 and isinstance(body, dict):
            return body
        detail = body.get("error") if isinstance(body, dict) else str(body)[:200]
        raise HubClientError("The hub said %s: %s" % (status, detail), status, body)
