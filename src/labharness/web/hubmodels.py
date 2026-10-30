"""Models through the Kelvin hub (hub/llm.py): for a person signed in to Kelvin with their Kelvin account, Claude, GPT,
Gemini and OpenCode with the keys of the hub's owner, within the person's monthly budget. In the user version
(edition.py) these, a model server on the person's own computer and their ChatGPT plan are the only models.

Each is an ordinary model connection (Anthropic's, OpenAI's, Gemini's) whose address is the hub and whose key is the
person's hub token, under a name of its own (hub-anthropic, ...): a project's rules can allow or exclude the hub, and
confidential work never uses it (agent.py). Which services the hub offers, and the person's month so far, come from
GET /api/llm, asked again every 10 minutes while someone looks at the models."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("kelvin")
SERVICES = {   # the hub's service: (Kelvin's name for it, its label in the model picker)
    "anthropic": ("hub-anthropic", "Claude · Kelvin hub"),
    "openai": ("hub-openai", "GPT · Kelvin hub"),
    "gemini": ("hub-gemini", "Gemini · Kelvin hub"),
    "opencode": ("hub-opencode", "OpenCode Zen · Kelvin hub"),
    "opencode-go": ("hub-opencode-go", "OpenCode Go · Kelvin hub"),
}
NAMES = {name: service for service, (name, _) in SERVICES.items()}
EVERY = 600         # seconds between askings
AGAIN = 60          # ...or after the hub couldn't be reached
WAIT_S = 8          # the most the model list waits for the hub
_SEEN: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}   # by hub and sign-in: (when, what the hub said)


def through_hub(name: str) -> bool:
    """Is this provider one of the hub's?"""
    return name in NAMES


def _from_hub(url: str, token: str) -> Dict[str, Any]:
    from .. import central
    return central.Central(url).llm(token)


class HubModels:
    """What one hub tells one signed-in person: the services it offers (and which of their models), and their month."""

    def __init__(self, url: str, token: str, fetch: Callable[[str, str], Dict[str, Any]] = _from_hub):
        self.url, self.token, self.fetch = url.rstrip("/"), token, fetch
        self.key = hashlib.sha256(("%s %s" % (self.url, token)).encode()).hexdigest()[:24]
        self._lock = threading.Lock()

    def status(self) -> Optional[Dict[str, Any]]:
        return (_SEEN.get(self.key) or (0.0, None))[1]

    def models_of(self, service: str) -> Optional[List[str]]:
        """The models the hub offers on this service (None: every model it lists, or not offered at all)."""
        for s in (self.status() or {}).get("services") or []:
            if s.get("id") == service:
                return s.get("models")
        return None

    def offers(self, service: str) -> bool:
        return any(s.get("id") == service for s in (self.status() or {}).get("services") or [])

    def refresh(self) -> None:
        """Ask the hub again when the last answer is old (once at a time)."""
        if time.time() < (_SEEN.get(self.key) or (0.0, None))[0] or not self._lock.acquire(blocking=False):
            return
        try:
            before = self.status()
            try:
                answer = self.fetch(self.url, self.token)
                if not isinstance(answer, dict):
                    raise ValueError("not a list of services")
                # while it offers nothing (no keys set yet), asked again in a minute, so keys show soon after
                _SEEN[self.key] = (time.time() + (EVERY if answer.get("services") else AGAIN), answer)
            except Exception as e:   # offline, signed out, an older hub: what it said before stays a while
                log.info("the Kelvin hub's models: %s", e)
                _SEEN[self.key] = (time.time() + AGAIN, before)
        finally:
            self._lock.release()

    async def prepare(self) -> None:
        try:
            await asyncio.wait_for(asyncio.to_thread(self.refresh), WAIT_S)
        except asyncio.TimeoutError:
            pass


def hub_providers(where: Optional[Tuple[str, str]], vision: Any = None) -> Dict[str, Any]:
    """The hub's model connections for a person signed in there ((the hub's address, their token); None: nobody)."""
    if not where or not where[0] or not where[1]:
        return {}
    from .providers.hub import KINDS   # Anthropic's, OpenAI's and Gemini's SDKs: only once someone is signed in
    hub = HubModels(*where)
    return {p.name: p for p in (kind(hub, vision) for kind in KINDS)}
