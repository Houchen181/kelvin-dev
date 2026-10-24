"""Phones reach their person's Kelvin through the hub: no pairing, and no way in to the lab's network needed.

A Kelvin whose person signed in with their Kelvin account keeps one WebSocket open to the hub (/api/relay/kelvin,
with that sign-in's token). It goes out from the lab, so firewalls and Wi-Fi that keeps devices apart don't stop it.
A phone signed in to the same account (POST /api/signin with platform "ios") lists the account's Kelvins
(GET /api/relay/kelvins) and calls the phone routes of one through the hub (/api/relay/kelvins/<id>/api/...). The
Kelvin answers as it would a paired phone of that person, by its own rules (web/relay.py). The hub passes requests and
answers through and keeps none of them; it sees them in passing, since there is no end-to-end encryption yet, so
Kelvin doesn't send unshared work this way.

On the Kelvin's socket, text frames are JSON:
- Kelvin -> hub, first: {"t": "hello", "name", "version", "direct"} (direct: Kelvin's phone listener is on, so a
  phone on its network can also reach it without the hub);
- hub -> Kelvin: {"t": "req", "id", "method", "path", "query", "type", "body" (base64), "phone": {"id", "name"}}, and
  {"t": "cancel", "id"} when the phone gives up;
- Kelvin -> hub, for each request: {"t": "head", "id", "status", "headers": [[name, value], ...]}, then
  {"t": "end", "id"}, or {"t": "fail", "id", "message"}.
An answer's bytes come as binary frames: the request's id in 4 bytes, then the bytes.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .db import TooMuch, log

PHONES = ("ios", "android")       # installations that are phones, not Kelvins
MAX_REQUEST = 1024 * 1024         # bytes a phone sends in one request (JSON; nothing bigger goes this way)
HELLO = 15                        # seconds a Kelvin has to say hello once its socket is open
FIRST_BYTE = 60                   # seconds a Kelvin has to start answering
IDLE = 15 * 60                    # seconds an answer may go without a byte (a long chat turn streams as it works)
PER_PERSON = 16                   # one person's requests through the hub at once
PASSED = ("content-type", "content-disposition", "x-kelvin-hidden")   # the answer's headers the phone gets
END = object()


class Link:
    """One Kelvin's socket: the requests in flight on it, each with a queue for what comes back."""

    def __init__(self, ws: WebSocket, name: str, version: str, direct: bool):
        self.ws, self.name, self.version, self.direct = ws, name, version, direct
        self.waiting: Dict[int, "asyncio.Queue[Any]"] = {}
        self.last = 0
        self.since = time.time()
        self._sending = asyncio.Lock()

    def open(self) -> Tuple[int, "asyncio.Queue[Any]"]:
        self.last = self.last % 0xFFFFFFFF + 1
        queue: "asyncio.Queue[Any]" = asyncio.Queue()
        self.waiting[self.last] = queue
        return self.last, queue

    async def send(self, frame: Dict[str, Any]) -> None:
        async with self._sending:
            await self.ws.send_text(json.dumps(frame))

    def fail_all(self, message: str) -> None:
        for queue in self.waiting.values():
            queue.put_nowait(RuntimeError(message))
        self.waiting.clear()


class Relay:
    """Which Kelvins are on the hub now: account -> installation id -> its socket. In memory: a restart of the hub
    drops them, and each Kelvin connects again by itself."""

    def __init__(self) -> None:
        self.links: Dict[int, Dict[str, Link]] = {}
        self.in_flight: Dict[int, int] = {}

    def link(self, user_id: int, kelvin: str) -> Optional[Link]:
        return self.links.get(user_id, {}).get(kelvin)


def bearer(headers: Any) -> str:
    auth = headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def routes(hub: Any, call: Callable[..., Any], read_body: Callable[..., Any],
           api_error: Callable[..., Response]) -> Tuple[Relay, List[Any]]:
    relay = Relay()

    async def signed_in(token: str) -> Optional[Dict[str, Any]]:
        return await call(hub.app, token) if token else None

    # ------------------------------------------------------------------ the Kelvin's side

    async def kelvin_socket(ws: WebSocket) -> None:
        app = await signed_in(bearer(ws.headers))
        if app is None:
            await ws.close(code=4401)   # signed out on the hub, or never signed in
            return
        if app["installation"]["platform"] in PHONES:
            await ws.close(code=4403)   # a phone's sign-in doesn't make it a Kelvin
            return
        await ws.accept()
        try:
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), HELLO))
            if not isinstance(hello, dict) or hello.get("t") != "hello":
                raise ValueError("no hello")
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError, KeyError, RuntimeError):
            await ws.close(code=4400)
            return
        uid, kid = app["user"]["id"], app["installation_id"]
        link = Link(ws, str(hello.get("name") or app["installation"]["name"] or "Kelvin")[:80],
                    str(hello.get("version") or "")[:40], hello.get("direct") is True)
        old = relay.links.setdefault(uid, {}).get(kid)
        relay.links[uid][kid] = link
        if old is not None:   # the same Kelvin again (it reconnected before its old socket timed out)
            old.fail_all("The Kelvin computer reconnected; try again.")
            try:
                await old.ws.close(code=4409)
            except Exception:
                pass
        log.info("relay: Kelvin online user=%s installation=%s", uid, kid)
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    data = message["bytes"]
                    queue = link.waiting.get(int.from_bytes(data[:4], "big"))
                    if queue is not None and len(data) > 4:
                        queue.put_nowait(data[4:])
                    continue
                frame = json.loads(message.get("text") or "{}")
                queue = link.waiting.get(frame.get("id")) if isinstance(frame, dict) else None
                if queue is None:
                    if isinstance(frame, dict) and frame.get("t") == "hello":   # its phone listener went on or off
                        link.direct = frame.get("direct") is True
                    continue
                if frame.get("t") == "head":
                    queue.put_nowait(("head", int(frame.get("status") or 502), frame.get("headers") or []))
                elif frame.get("t") == "end":
                    queue.put_nowait(END)
                elif frame.get("t") == "fail":
                    queue.put_nowait(RuntimeError(str(frame.get("message") or "The Kelvin computer failed.")[:300]))
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            if relay.link(uid, kid) is link:
                del relay.links[uid][kid]
            link.fail_all("The Kelvin computer went offline.")
            log.info("relay: Kelvin offline user=%s installation=%s", uid, kid)

    # ------------------------------------------------------------------ the phone's side

    async def kelvins(request: Request) -> Response:
        """The account's Kelvins: which are online now (reachable through the hub) and when the others were last."""
        app = await signed_in(bearer(request.headers))
        if app is None:
            return api_error("You are signed out of your Kelvin account. Sign in again.", 401)
        uid = app["user"]["id"]
        online = relay.links.get(uid, {})
        out = []
        for k in await call(hub.kelvins_of, uid):
            link = online.get(k["id"])
            out.append({"id": k["id"], "name": link.name if link else k["name"], "platform": k["platform"],
                        "version": link.version if link else k["version"], "online": link is not None,
                        "direct": bool(link and link.direct), "last_seen": k["last_seen"]})
        return JSONResponse({"kelvins": out, "user": {"name": app["user"]["name"], "email": app["user"]["email"]}})

    async def through(request: Request) -> Response:
        """A phone's call to one of its person's Kelvins, passed through and answered as it streams back."""
        app = await signed_in(bearer(request.headers))
        if app is None:
            return api_error("You are signed out of your Kelvin account. Sign in again.", 401)
        uid = app["user"]["id"]
        link = relay.link(uid, request.path_params["kelvin"])
        if link is None:
            return api_error("That Kelvin is offline: it isn't running, or nobody is signed in to it with your "
                             "Kelvin account.", 503, offline=True)
        path = "/" + request.path_params["path"]
        if not path.startswith("/api/"):
            return api_error("not found", 404)
        if relay.in_flight.get(uid, 0) >= PER_PERSON:
            return api_error("Too many requests at once; try again in a moment.", 429)
        try:
            body = await read_body(request, MAX_REQUEST)
        except TooMuch as e:
            return api_error(str(e), 413)
        relay.in_flight[uid] = relay.in_flight.get(uid, 0) + 1
        rid, queue = link.open()
        done = {"released": False}

        def release() -> None:
            if not done["released"]:
                done["released"] = True
                link.waiting.pop(rid, None)
                relay.in_flight[uid] = max(0, relay.in_flight.get(uid, 1) - 1)

        try:
            await link.send({"t": "req", "id": rid, "method": request.method, "path": path,
                             "query": request.url.query, "type": request.headers.get("content-type", ""),
                             "body": base64.b64encode(body).decode("ascii"),
                             "phone": {"id": app["installation_id"], "name": app["installation"]["name"]}})
            first = await asyncio.wait_for(queue.get(), FIRST_BYTE)
        except asyncio.TimeoutError:
            release()
            asyncio.ensure_future(_cancel(link, rid))
            return api_error("The Kelvin computer didn't answer in time.", 504)
        except Exception:
            release()
            return api_error("The Kelvin computer went offline.", 503, offline=True)
        if isinstance(first, Exception):
            release()
            return api_error(str(first), 502)
        if not (isinstance(first, tuple) and first[0] == "head"):
            release()
            return api_error("The Kelvin computer answered out of turn.", 502)
        headers = {str(k): str(v) for k, v in first[2] if str(k).lower() in PASSED}

        async def answer() -> Any:
            finished = False
            try:
                while True:
                    item = await asyncio.wait_for(queue.get(), IDLE)
                    if item is END:
                        finished = True
                        return
                    if isinstance(item, Exception):
                        return
                    yield item
            except asyncio.TimeoutError:
                return
            finally:
                release()
                if not finished:   # the phone went away, or the answer stopped: the Kelvin stops working on it
                    asyncio.ensure_future(_cancel(link, rid))

        return StreamingResponse(answer(), status_code=first[1], headers=headers)

    async def _cancel(link: Link, rid: int) -> None:
        try:
            await link.send({"t": "cancel", "id": rid})
        except Exception:
            pass

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE"]
    return relay, [
        WebSocketRoute("/api/relay/kelvin", kelvin_socket),
        Route("/api/relay/kelvins", kelvins),
        Route("/api/relay/kelvins/{kelvin}/{path:path}", through, methods=methods),
    ]
