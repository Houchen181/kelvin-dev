"""Phones through the Kelvin hub (hub/relay.py): while someone is signed in here with their Kelvin account, this Kelvin
keeps one connection open to the hub, and a phone signed in to the same account reaches it through the hub. No
pairing, and no phone access on this computer's network needed: the connection goes out from the lab.

The hub's requests are answered by the portal itself, as for a paired phone of that person with role "control"
(remote.py): the same routes, the same refusals. One more rule: the hub sees what passes through it (there is no
end-to-end encryption yet), so the person's unshared work (unpublished, confidential or model-limited projects,
incognito chats, and their runs) doesn't go this way; app.py's `private` and `trim` keep it out. On the lab network or
through Tailscale the phone reaches this computer directly, with a token it gets through the hub
(POST /api/phone/direct), and sees everything as before."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .remote import allowed

log = logging.getLogger("kelvin")

RETRY, MAX_RETRY = 5.0, 300.0     # seconds between tries to reach the hub, doubling while it can't be reached
CHUNK = 256 * 1024                # bytes in one frame of an answer
TRIMMED_MAX = 8 * 1024 * 1024     # bytes of an answer read whole to leave unshared work out (lists, the lab's state)
AT_ONCE = 16                      # requests answered at once
RELAY_ONLY = {("POST", "/api/phone/direct"), ("DELETE", "/api/phone/direct")}
# answers that can list unshared work: read whole and passed through `trim` (a run's files stream as they are)
TRIMMED = {"/api/chats", "/api/projects", "/api/runs", "/api/state", "/api/live", "/api/bootstrap", "/api/watches"}

Target = Tuple[str, str, str]     # the hub's address, the person's token there, the person's name here


class SignedOut(Exception):
    """The hub doesn't take the token any more (signed out there, a password reset, a disabled account)."""


def socket_url(hub: str) -> str:
    parts = urlsplit(hub)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return "%s://%s%s/api/relay/kelvin" % (scheme, parts.netloc, parts.path.rstrip("/"))


def _connect(url: str, token: str) -> Any:
    from websockets.asyncio.client import connect
    from .._version import __version__
    from ..hub.client import ssl_context
    secure = {"ssl": ssl_context()} if url.startswith("wss://") else {}   # Kelvin's own certificates too
    return connect(url, additional_headers={"Authorization": "Bearer " + token}, open_timeout=20,
                   ping_interval=25, ping_timeout=25, max_size=2 ** 22, user_agent_header="Kelvin/" + __version__,
                   **secure)


class RelayLink:
    """Keeps this Kelvin on the hub while someone is signed in with a Kelvin account (target), and answers the
    phone requests that come through it with the portal's own app."""

    def __init__(self, app: Any, target: Callable[[], Optional[Target]], hello: Callable[[], Dict[str, Any]],
                 private: Optional[Callable[[str, str, bytes, str], Optional[str]]] = None,
                 trim: Optional[Callable[[str, str, Any], Tuple[Any, int]]] = None,
                 connect: Callable[[str, str], Any] = _connect):
        self.app = app            # the portal inside its guard: what answers a paired phone's requests
        self.target = target
        self.hello = hello        # {"name", "version", "direct"} for the hub
        self.private = private    # (method, path, body, query) -> why this request reaches unshared work, or None
        self.trim = trim          # (method, path, answer) -> (the answer without unshared work, how much was left out)
        self.connect = connect
        self.state = "off"        # off | connecting | online | offline | signed out
        self.problem: Optional[str] = None
        self.wake = asyncio.Event()

    def view(self) -> Dict[str, Any]:
        return {"state": self.state, "problem": self.problem}

    async def _pause(self, seconds: float) -> None:
        self.wake.clear()
        try:
            await asyncio.wait_for(self.wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def run(self) -> None:
        delay = RETRY
        refused: Optional[Target] = None   # the sign-in the hub turned down: wait for another
        while True:
            want = self.target()
            if want is None or want == refused:
                if want is None:
                    self.state, self.problem, refused = "off", None, None
                await self._pause(30)
                continue
            self.state = "connecting"
            try:
                await self._session(want)
                delay = RETRY
            except asyncio.CancelledError:
                raise
            except SignedOut:
                self.state, refused = "signed out", want
                self.problem = "The Kelvin hub signed this computer out of your account. Sign in again here."
                log.info("phones through the hub: the hub signed this computer out")
                continue
            except Exception as e:
                self.state, self.problem = "offline", "Kelvin can't reach the hub now (%s)." % (str(e)[:120] or
                                                                                              type(e).__name__)
            if self.target() == want:
                await self._pause(delay)
                delay = min(delay * 2, MAX_RETRY)

    async def _session(self, want: Target) -> None:
        from websockets.exceptions import InvalidStatus
        hub, token, person = want
        try:
            ws = await self.connect(socket_url(hub), token)
        except InvalidStatus as e:
            status = getattr(getattr(e, "response", None), "status_code", 0)
            if status in (401, 403):
                raise SignedOut()
            raise
        async with ws:
            sending = asyncio.Lock()

            async def send(message: Any) -> None:
                async with sending:
                    await ws.send(message)

            said = dict(self.hello())
            await send(json.dumps(dict(said, t="hello")))
            self.state, self.problem = "online", None
            log.info("phones through the hub: on (%s, for %s)", urlsplit(hub).netloc, person)
            tasks: Dict[int, asyncio.Task] = {}
            room = asyncio.Semaphore(AT_ONCE)

            async def watch() -> None:   # a sign-out or another person here ends this connection; say when direct changes
                nonlocal said
                while True:
                    await asyncio.sleep(2)
                    if self.target() != want:
                        await ws.close()
                        return
                    now = dict(self.hello())
                    if now != said:
                        said = now
                        await send(json.dumps(dict(now, t="hello")))

            watcher = asyncio.ensure_future(watch())
            try:
                async for message in ws:
                    if not isinstance(message, str):
                        continue
                    frame = json.loads(message)
                    if not isinstance(frame, dict):
                        continue
                    if frame.get("t") == "req" and isinstance(frame.get("id"), int):
                        tasks[frame["id"]] = asyncio.ensure_future(self._answer(send, frame, room))
                    elif frame.get("t") == "cancel":
                        task = tasks.pop(frame.get("id"), None)
                        if task is not None:
                            task.cancel()
                    tasks = {k: t for k, t in tasks.items() if not t.done()}
            finally:
                watcher.cancel()
                for task in tasks.values():
                    task.cancel()
            if self.target() == want:
                raise ConnectionError("the hub closed the connection")

    async def _answer(self, send: Callable[[Any], Any], frame: Dict[str, Any], room: asyncio.Semaphore) -> None:
        rid = frame["id"]
        method = str(frame.get("method") or "GET").upper()
        target = str(frame.get("path") or "/")
        query = str(frame.get("query") or "")
        try:
            body = base64.b64decode(frame.get("body") or "")
        except ValueError:
            body = b""
        phone = frame.get("phone") if isinstance(frame.get("phone"), dict) else {}

        async def reply(status: int, payload: Any) -> None:
            data = json.dumps(payload).encode("utf-8")
            await send(json.dumps({"t": "head", "id": rid, "status": status,
                                   "headers": [["content-type", "application/json"]]}))
            await send(rid.to_bytes(4, "big") + data)
            await send(json.dumps({"t": "end", "id": rid}))

        if not target.startswith("/api/") or ".." in target:
            await reply(404, {"error": "not found"})
            return
        if (method, target) not in RELAY_ONLY and not allowed("control", method, target):
            await reply(403, {"error": "A phone can't do that. Use the Kelvin computer."})
            return
        why = self.private(method, target, body, query) if self.private else None
        if why:
            await reply(403, {"error": why, "unshared": True})
            return
        async with room:
            await self._serve(send, rid, method, target, query, body, phone)

    async def _serve(self, send: Callable[[Any], Any], rid: int, method: str, path: str, query: str, body: bytes,
                     phone: Dict[str, Any]) -> None:
        trimmed = method == "GET" and path in TRIMMED and self.trim is not None
        head: Dict[str, Any] = {}
        held: List[bytes] = []
        sent = {"head": False, "end": False, "size": 0}

        async def out_head(status: int, headers: List[List[str]]) -> None:
            await send(json.dumps({"t": "head", "id": rid, "status": status, "headers": headers}))
            sent["head"] = True

        async def out_bytes(data: bytes) -> None:
            for i in range(0, len(data), CHUNK):
                await send(rid.to_bytes(4, "big") + data[i:i + CHUNK])

        async def receive() -> Dict[str, Any]:
            if not sent.get("asked"):
                sent["asked"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()   # until the request ends (the phone went away: this task is cancelled)
            return {"type": "http.disconnect"}

        async def asgi_send(message: Dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                head["status"] = message["status"]
                head["headers"] = [[k.decode("latin-1"), v.decode("latin-1")] for k, v in message.get("headers", [])
                                   if k.lower() not in (b"content-length",)]
                if not trimmed:
                    await out_head(head["status"], head["headers"])
            elif message["type"] == "http.response.body":
                data = message.get("body", b"")
                if trimmed:
                    sent["size"] += len(data)
                    if sent["size"] > TRIMMED_MAX:
                        raise ValueError("too large an answer to check for unshared work")
                    held.append(data)
                elif data:
                    await out_bytes(data)
                if not message.get("more_body"):
                    if trimmed:
                        await self._out_trimmed(out_head, out_bytes, method, path, head, b"".join(held))
                    await send(json.dumps({"t": "end", "id": rid}))
                    sent["end"] = True

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                 "method": method, "scheme": "https", "path": path, "raw_path": path.encode("utf-8"),
                 "query_string": query.encode("latin-1", "replace"), "root_path": "",
                 "headers": [(b"host", b"relay"), (b"content-type", b"application/json"),
                             (b"content-length", str(len(body)).encode("ascii"))],
                 "client": ("hub", 0), "server": ("relay", 0),
                 "kelvin": {"role": "control", "device": {"id": str(phone.get("id") or "")[:80],
                                                          "name": str(phone.get("name") or "Phone")[:80],
                                                          "via": "hub"}}}
        try:
            await self.app(scope, receive, asgi_send)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("phones through the hub: %s %s failed: %s", method, path, e)
            if not sent["head"]:
                await out_head(500, [["content-type", "application/json"]])
                await out_bytes(json.dumps({"error": "Kelvin had a problem answering that."}).encode("utf-8"))
                await send(json.dumps({"t": "end", "id": rid}))
            elif not sent["end"]:
                await send(json.dumps({"t": "fail", "id": rid, "message": "Kelvin had a problem answering that."}))

    async def _out_trimmed(self, out_head: Any, out_bytes: Any, method: str, path: str, head: Dict[str, Any],
                           data: bytes) -> None:
        """An answer that may list unshared work, without it."""
        headers = head.get("headers") or []
        is_json = any(k.lower() == "content-type" and "json" in v for k, v in headers)
        if is_json and head.get("status") == 200 and data:
            try:
                payload = json.loads(data)
                payload, hidden = self.trim(method, path, payload)
                data = json.dumps(payload).encode("utf-8")
                if hidden:
                    headers = headers + [["x-kelvin-hidden", str(hidden)]]
            except ValueError:
                pass
        await out_head(head.get("status") or 500, headers)
        if data:
            await out_bytes(data)
