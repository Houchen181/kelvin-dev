"""Central Kelvin accounts: one account per person, on the Kelvin hub, that signs them in on every computer.

Kelvin asks the hub (the computer's hub address, Settings) to check the email and password, and keeps:
- a token for the person on this computer (in their own settings, encrypted with their password);
- a verifier of the password (accounts.py), so the same password signs them in here offline, and
- their API keys encrypted with that password, as before.

A password changed elsewhere (another computer, or a reset by mail) takes effect here the next time Kelvin reaches
the hub. Keys sealed with the old password stay unreadable unless the person gives the old one once.

Only accounts: Kelvin shares no data with the hub from here (that is web/hubsync.py, switched off for now)."""
from __future__ import annotations

import json
import logging
import socket
import ssl
from http.client import HTTPException
from typing import Any, Callable, Dict, Optional, Tuple

from ._version import __version__
from .hub import client

log = logging.getLogger("kelvin")
NETWORK = (OSError, HTTPException, ValueError)   # the hub can't be reached, or answered nonsense
# The one Kelvin hub, for every Kelvin user in every lab. For now it runs on a lab workstation through Tailscale Funnel;
# when it moves to its permanent server, an update changes this (and Account.defaultHub in the iPhone app). Settings
# (account_hub) can name another, for development.
DEFAULT_HUB = "https://kelvin.tail269ee5.ts.net"

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
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer %s" % token
        try:
            status, answer = self.http("POST", self.base_url + path, json.dumps(body).encode("utf-8"), headers)
        except NETWORK as e:
            log.warning("the Kelvin hub %s: %r", self.base_url, e)   # all of it, in kelvin.log
            raise Unreachable("Kelvin can't reach the Kelvin hub: %s" % why(e))
        if status == 200 and isinstance(answer, dict):
            return answer
        if status == 404 and missing:
            raise CentralError(missing, 404)
        if status == 404 or status >= 500 or not isinstance(answer, dict):   # not a hub, or not up (a tunnel's 530...)
            raise Unreachable("The Kelvin hub isn't answering (%s)." % status)
        raise CentralError(str(answer.get("error") or "The hub said no (%s)." % status), status, answer)

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
