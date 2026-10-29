"""Telling people: alarms, approvals waiting, finished runs; and that Kelvin is still there.

Every notice goes to the in-app list (the portal shows a banner and, if allowed, a system notification). The user can
also give a webhook URL, and then chosen kinds of notice are posted there too: an ntfy topic (push to a phone), or a
Slack, Teams or Discord incoming webhook. Or an email account (SMTP): the same notices go by email, which reaches a
phone without any third-party service. Nothing is sent anywhere unless the user sets one of these up.

A heartbeat URL (healthchecks.io, Uptime Kuma's push monitor, or anything that alerts when pings stop) is pinged every
few minutes while Kelvin is running and watching the lab: when Kelvin or the computer stops in the night, the pings
stop, and that service tells someone. It is told nothing about the lab.
"""
from __future__ import annotations

import itertools
import json
import re
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

KINDS = ("alarm", "alarm_cleared", "approval", "run")
DEFAULT_KINDS = ("alarm", "run")
SECURITY = ("starttls", "ssl", "none")
HEARTBEAT_S = 300.0   # seconds between heartbeat pings, unless set otherwise
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


def _ping(url: str) -> None:
    with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=10) as r:
        r.read()


@dataclass
class Email:
    """Where email notices go, and the account they are sent from (SMTP). The password is kept like an API key."""
    host: str
    to: List[str]
    port: int = 587
    security: str = "starttls"     # starttls (port 587), ssl (port 465) or none
    user: str = ""
    password: str = field(default="", repr=False)
    sender: str = ""

    def scrub(self, text: str) -> str:
        """An error's words without the account's password or user name: they never reach a log or a screen."""
        for secret in (self.password, self.user):
            if secret:
                text = text.replace(secret, "…")
        return text


def email_from(host: Any, to: Any, port: Any = None, security: Any = None, user: Any = None, password: Any = None,
               sender: Any = None) -> Optional[Email]:
    """An Email from the settings' text, or None when no server or no recipient is set (email off). ValueError for
    settings that can't work."""
    host = str(host or "").strip()
    recipients = [a.strip() for a in re.split(r"[,;\s]+", str(to or "")) if a.strip()]
    if not host or not recipients:
        return None
    if not re.match(r"^[A-Za-z0-9.-]+$", host):
        raise ValueError("the email server is a host name, such as smtp.example.edu")
    bad = [a for a in recipients + ([str(sender).strip()] if sender else []) if not re.match(r"^[^@\s]+@[^@\s]+$", a)]
    if bad:
        raise ValueError("not an email address: %s" % ", ".join(bad))
    security = str(security or "starttls").lower()
    if security not in SECURITY:
        raise ValueError("email security is starttls, ssl or none")
    try:
        port_n = int(port) if port not in (None, "") else (465 if security == "ssl" else 587)
    except (TypeError, ValueError):
        raise ValueError("the email server's port is a number, such as 587") from None
    return Email(host, recipients, port_n, security, str(user or "").strip(), str(password or ""),
                 str(sender or "").strip())


def _mail(cfg: Email, subject: str, text: str) -> None:
    import smtplib
    import ssl
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = subject.replace("\n", " ")[:200]
    msg["From"] = cfg.sender or cfg.user or "kelvin@localhost"
    msg["To"] = ", ".join(cfg.to)
    msg.set_content((text or subject) + "\n\n-- Kelvin")
    context = ssl.create_default_context()
    if cfg.security == "ssl":
        server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=15, context=context)
    else:
        server = smtplib.SMTP(cfg.host, cfg.port, timeout=15)
    try:
        if cfg.security == "starttls":
            server.starttls(context=context)
        if cfg.user:
            server.login(cfg.user, cfg.password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


class Notifier:
    def __init__(self, post: Callable[[str, bytes, Dict[str, str]], None] = _post,
                 mail: Callable[[Email, str, str], None] = _mail, ping: Callable[[str], None] = _ping):
        self.notices: Deque[Dict[str, Any]] = deque(maxlen=100)
        self.webhook: Optional[str] = None
        self.email: Optional[Email] = None
        self.kinds = set(DEFAULT_KINDS)
        self.last_error: Optional[str] = None
        # paired phones (the Kelvin app sees requests and alarms): set by the app; one counts as someone to reach
        self.phones: Callable[[], bool] = lambda: False
        # the heartbeat: pinged every heartbeat_s while alive() (Kelvin is watching the lab), never otherwise
        self.heartbeat: Optional[str] = None
        self.heartbeat_s = HEARTBEAT_S
        self.alive: Callable[[], bool] = lambda: True
        self.last_beat: Optional[float] = None
        self.beat_error: Optional[str] = None
        self._post, self._mail, self._ping = post, mail, ping
        self._lock = threading.Lock()
        self._beat_wake = threading.Event()
        self._beat_thread: Optional[threading.Thread] = None

    def configure(self, webhook: Optional[str], kinds: Optional[Iterable[str]] = None, email: Optional[Email] = None,
                  heartbeat: Optional[str] = None, heartbeat_s: Optional[float] = None) -> None:
        url = (webhook or "").strip()
        if url and not url.startswith(("https://", "http://")):
            raise ValueError("the webhook must be an http(s) URL")
        beat = (heartbeat or "").strip()
        if beat and not beat.startswith(("https://", "http://")):
            raise ValueError("the heartbeat must be an http(s) URL")
        self.webhook = url or None
        self.email = email
        if kinds is not None:
            self.kinds = {k for k in kinds if k in KINDS}
        if heartbeat_s:
            self.heartbeat_s = max(60.0, float(heartbeat_s))
        self.heartbeat = beat or None
        if self.heartbeat and self._beat_thread is None:
            self._beat_thread = threading.Thread(target=self._beating, name="kelvin-heartbeat", daemon=True)
            self._beat_thread.start()
        self._beat_wake.set()   # a new address or interval counts from now

    def reaches_away(self, kind: str = "alarm") -> bool:
        """Can a notice of this kind reach someone away from the computer: a webhook or email that takes it, or a
        paired phone (the Kelvin app shows requests and alarms)?"""
        try:
            phones = bool(self.phones())
        except Exception:
            phones = False
        return phones or (kind in self.kinds and bool(self.webhook or self.email))

    def __call__(self, kind: str, title: str, text: str = "") -> Dict[str, Any]:
        notice = {"id": next(_ids), "kind": kind, "title": title, "text": text, "at": time.time()}
        with self._lock:
            self.notices.append(notice)
        if kind in self.kinds and (self.webhook or self.email):
            self._send(title, text)
        return notice

    def since(self, after: int) -> List[Dict[str, Any]]:
        with self._lock:
            return [n for n in self.notices if n["id"] > after]

    def test(self) -> str:
        if not (self.webhook or self.email or self.heartbeat):
            return "no webhook, email or heartbeat set"
        self.last_error = None
        if self.webhook or self.email:
            self._send("Kelvin test", "Notifications from Kelvin reach this place.", wait=True)
        said = [self.last_error or "sent"] if self.webhook or self.email else []
        if self.heartbeat:
            self._beat()
            said.append("heartbeat: " + (self.beat_error or "pinged"))
        return "; ".join(said)

    def _send(self, title: str, text: str, wait: bool = False) -> None:
        url, email = self.webhook, self.email
        problems: List[str] = []

        def go() -> None:
            if url:
                try:
                    data, headers = webhook_request(url, title, text)
                    self._post(url, data, headers)
                except Exception as e:
                    problems.append("could not reach the webhook: %s" % e)
            if email is not None:
                try:
                    self._mail(email, title, text)
                except Exception as e:   # the account's password and user never go into the words
                    problems.append("could not send the email: %s" % email.scrub(str(e) or type(e).__name__))
            self.last_error = "; ".join(problems) or None

        if wait:
            go()
        else:
            threading.Thread(target=go, name="kelvin-notify", daemon=True).start()

    # ------------------------------------------------------------------ the heartbeat

    def _beating(self) -> None:
        while True:
            self._beat()
            self._beat_wake.clear()
            self._beat_wake.wait(self.heartbeat_s)

    def _beat(self) -> None:
        """One ping, only while Kelvin is watching the lab: a Kelvin that hangs stops pinging, as one that stopped."""
        url = self.heartbeat
        if not url:
            return
        try:
            if not self.alive():
                self.beat_error = "not pinged: Kelvin isn't watching the lab"
                return
            self._ping(url)
            self.last_beat, self.beat_error = time.time(), None
        except Exception as e:
            self.beat_error = "could not reach the heartbeat address: %s" % (str(e) or type(e).__name__)
