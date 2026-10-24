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
import socket
from http.client import HTTPException
from typing import Any, Callable, Dict, Optional, Tuple

from ._version import __version__
from .hub import client

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

    def _post(self, path: str, body: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer %s" % token
        try:
            status, answer = self.http("POST", self.base_url + path, json.dumps(body).encode("utf-8"), headers)
        except NETWORK as e:
            raise Unreachable("Kelvin can't reach the Kelvin hub (%s)." % type(e).__name__)
        if status == 200 and isinstance(answer, dict):
            return answer
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
