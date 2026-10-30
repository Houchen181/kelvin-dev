"""Central Kelvin accounts: one account per person, on the Kelvin hub, that signs them in on every computer.

Kelvin asks the hub (the computer's hub address, Settings) to check the email and password, and keeps:
- a token for the person on this computer (in their own settings, encrypted with their password);
- a verifier of the password (accounts.py), so the same password signs them in here offline, and
- their API keys encrypted with that password, as before.

A password changed elsewhere (another computer, or a reset by mail) takes effect here the next time Kelvin reaches
the hub. Keys sealed with the old password stay unreadable unless the person gives the old one once.

Accounts, and two things for chats: the hub's list of models that see pictures (web/vision.py), and PDF pages it draws
as pictures for a model that sees pictures but can't read PDFs, which it keeps nothing of (web/agent.py: never pages
from incognito chats or unshared work). Kelvin shares no data with the hub from here (that is web/hubsync.py, switched
off for now)."""
from __future__ import annotations

import base64
import json
import logging
import socket
import ssl
from http.client import HTTPException
from typing import Any, Callable, Dict, List, Optional, Tuple

from ._version import __version__
from .hub import client

log = logging.getLogger("kelvin")
NETWORK = (OSError, HTTPException, ValueError)   # the hub can't be reached, or answered nonsense
# The one Kelvin hub, for every Kelvin user in every lab. For now it runs on a lab workstation through Tailscale Funnel;
# when it moves to its permanent server, an update changes this (and Account.defaultHub in the iPhone app). Settings
# (account_hub) can name another, for development.
DEFAULT_HUB = "https://kelvin.tail269ee5.ts.net"
PICTURE_KINDS = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}   # what the hub's drawn pages are

Http = Callable[..., Tuple[int, Any]]


class CentralError(Exception):
    """The hub answered no: wrong password, too many tries, an account that exists, terms to agree to..."""

    def __init__(self, message: str, status: int = 400, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class Unreachable(Exception):
    """Kelvin can't reach the hub now (offline, or the hub is down)."""


def why(e: BaseException) -> str:
    """Why the hub can't be reached, in plain words: a browser on the same computer may still open it, since it has
    its own certificates, its own DNS and the system's proxy script."""
    reason = getattr(e, "reason", e)   # urllib's URLError wraps the cause
    text = str(reason)
    if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in text:
        return "this computer doesn't trust the hub's certificate (%s)." % (
            getattr(reason, "verify_message", "") or text)
    if isinstance(reason, socket.gaierror):
        return "this computer can't look up the hub's address (DNS)."
    if isinstance(reason, TimeoutError) or "timed out" in text:
        return "the hub didn't answer in time (a firewall or a proxy may hold the connection)."
    if isinstance(reason, (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError)):
        return "the connection was refused or cut off (a firewall, a proxy or security software?)."
    if isinstance(reason, ssl.SSLError):
        return "the secure connection to the hub failed (%s)." % text
    if isinstance(e, ValueError):
        return "the hub's answer made no sense."
    return "%s (%s)." % (type(reason).__name__, text) if text else "%s." % type(reason).__name__


def kelvin(installation: str) -> Dict[str, str]:
    """How this Kelvin introduces itself: its computer's id, its name, platform and version."""
    import sys
    try:
        name = socket.gethostname()[:80] or "Kelvin"
    except OSError:
        name = "Kelvin"
    return {"id": installation, "name": name, "platform": sys.platform, "version": __version__}


class Central:
    def __init__(self, base_url: str, http: Optional[Http] = None):
        try:
            self.base_url = client.check_url(base_url)
        except client.HubClientError as e:
            raise Unreachable(str(e))
        self.http = http or client._http

    def _post(self, path: str, body: Dict[str, Any], token: Optional[str] = None,
              missing: Optional[str] = None) -> Dict[str, Any]:
        """missing: what to say when the hub doesn't have this yet (an older hub answers 404)."""
        return self._send("POST", path, json.dumps(body).encode("utf-8"), token, "application/json", missing)

    def _send(self, method: str, path: str, data: Optional[bytes], token: Optional[str] = None,
              kind: str = "application/json", missing: Optional[str] = None) -> Dict[str, Any]:
        headers = {"Content-Type": kind} if data is not None else {}
        if token:
            headers["Authorization"] = "Bearer %s" % token
        try:
            status, answer = self.http(method, self.base_url + path, data, headers)
        except NETWORK as e:
            log.warning("the Kelvin hub %s: %r", self.base_url, e)   # all of it, in kelvin.log
            raise Unreachable("Kelvin can't reach the Kelvin hub: %s" % why(e))
        if status == 200 and isinstance(answer, dict):
            return answer
        if status == 404 and missing:
            raise CentralError(missing, 404)
        if status == 501 and isinstance(answer, dict) and answer.get("error"):   # a hub that can't do this one thing
            raise CentralError(str(answer["error"]), 501, answer)
        if status == 404 or status >= 500 or not isinstance(answer, dict):   # not a hub, or not up (a tunnel's 530...)
            raise Unreachable("The Kelvin hub isn't answering (%s)." % status)
        raise CentralError(str(answer.get("error") or "The hub said no (%s)." % status), status, answer)

    def llm(self, token: str) -> Dict[str, Any]:
        """The model services the hub offers through its gateway, and this person's month so far (web/hubmodels.py):
        {"services": [{"id", "name", "models"}], "month", "spent", "budget", "renews", "lab"}."""
        return self._send("GET", "/api/llm", None, token, missing="This Kelvin hub offers no models yet.")

    def pictures(self, token: str) -> Dict[str, Any]:
        """Which models see pictures, service by service: {"services": {"opencode": {"yes": [...], "no": [...]}}}
        (web/vision.py)."""
        return self._send("GET", "/api/models/pictures", None, token,
                          missing="This Kelvin hub has no list of models that see pictures yet.")

    def draw_pages(self, token: str, pdf: bytes) -> List[Tuple[str, bytes]]:
        """A PDF's pages drawn as pictures by the hub, which keeps nothing: [(mime, bytes)], one per page."""
        answer = self._send("POST", "/api/pdf/pages", pdf, token, "application/pdf",
                            missing="This Kelvin hub can't draw PDF pages yet.")
        out = []
        for page in answer.get("pages") or []:
            mime = page.get("mime") if isinstance(page, dict) else None
            try:
                data = base64.b64decode(page.get("data") or "", validate=True) if mime in PICTURE_KINDS else b""
            except ValueError:
                data = b""
            if not data.startswith(PICTURE_KINDS.get(mime, b"\0")):
                raise CentralError("The hub's pictures of the pages weren't pictures.", 502)
            out.append((mime, data))
        return out

    def sign_in(self, email: str, password: str, installation: str) -> Dict[str, Any]:
        return self._post("/api/signin", {"email": email, "password": password, "installation": kelvin(installation)})

    def register(self, name: str, email: str, password: str, consent: bool, installation: str) -> Dict[str, Any]:
        return self._post("/api/register", {"name": name, "email": email, "password": password,
                                            "consent": bool(consent), "installation": kelvin(installation)})

    def reset(self, email: str) -> None:
        self._post("/api/reset", {"email": email})

    def change_password(self, token: str, old: str, new: str) -> None:
        self._post("/api/password", {"old": old, "new": new}, token)

    def delete_account(self, email: str, password: str) -> None:
        """Delete the Kelvin account on the hub (its password, as on the account page)."""
        self._post("/api/account/delete", {"email": email, "password": password},
                   missing="This Kelvin hub can't delete accounts from Kelvin yet. Delete it on your account page "
                           "there (%s/account), then remove it from this computer." % self.base_url)
