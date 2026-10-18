"""Telling people: alarms, approvals waiting, finished runs.

Every notice goes to the in-app list (the portal shows a banner and, if allowed, a system notification). The user can
also give a webhook URL, and then chosen kinds of notice are posted there too: an ntfy topic (push to a phone), or a
Slack, Teams, Discord or other incoming webhook. Nothing is sent anywhere unless the user sets that URL.
"""
from __future__ import annotations

import itertools
import json
import threading
import time
import urllib.request
from collections import deque
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

KINDS = ("alarm", "alarm_cleared", "approval", "run")
DEFAULT_KINDS = ("alarm", "run")
_ids = itertools.count(1)


def webhook_request(url: str, title: str, text: str) -> Tuple[bytes, Dict[str, str]]:
    """The body and headers a webhook expects, by service."""
    host = url.split("/")[2].lower() if "://" in url else ""
    if "ntfy" in host:
        return (text or title).encode("utf-8"), {"Title": title.encode("ascii", "replace").decode(), "Tags": "cat"}
    body = "**%s**\n%s" % (title, text) if text else title
    if "discord" in host:
        return json.dumps({"content": body}).encode("utf-8"), {"Content-Type": "application/json"}
    return json.dumps({"text": body}).encode("utf-8"), {"Content-Type": "application/json"}  # Slack, Teams, Mattermost...


def _post(url: str, data: bytes, headers: Dict[str, str]) -> None:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


class Notifier:
    def __init__(self, post: Callable[[str, bytes, Dict[str, str]], None] = _post):
        self.notices: Deque[Dict[str, Any]] = deque(maxlen=100)
        self.webhook: Optional[str] = None
        self.kinds = set(DEFAULT_KINDS)
        self.last_error: Optional[str] = None
        self._post = post
        self._lock = threading.Lock()

    def configure(self, webhook: Optional[str], kinds: Optional[Iterable[str]] = None) -> None:
        url = (webhook or "").strip()
        if url and not url.startswith(("https://", "http://")):
            raise ValueError("the webhook must be an http(s) URL")
        self.webhook = url or None
        if kinds is not None:
            self.kinds = {k for k in kinds if k in KINDS}

    def __call__(self, kind: str, title: str, text: str = "") -> Dict[str, Any]:
        notice = {"id": next(_ids), "kind": kind, "title": title, "text": text, "at": time.time()}
        with self._lock:
            self.notices.append(notice)
        if self.webhook and kind in self.kinds:
            self._send(title, text)
        return notice

    def since(self, after: int) -> List[Dict[str, Any]]:
        with self._lock:
            return [n for n in self.notices if n["id"] > after]

    def test(self) -> str:
        if not self.webhook:
            return "no webhook set"
        self._send("Kelvin test", "Notifications from Kelvin reach this place.", wait=True)
        return self.last_error or "sent"

    def _send(self, title: str, text: str, wait: bool = False) -> None:
        url = self.webhook

        def go() -> None:
            try:
                data, headers = webhook_request(url, title, text)
                self._post(url, data, headers)
                self.last_error = None
            except Exception as e:
                self.last_error = "could not reach the webhook: %s" % e

        if wait:
            go()
        else:
            threading.Thread(target=go, name="kelvin-notify", daemon=True).start()
