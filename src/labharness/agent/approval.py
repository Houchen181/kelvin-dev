"""The human side: a local web page where a person approves or declines.

There is no MCP tool for answering approvals; answers come only from this
page. The page is served on 127.0.0.1 and every API call needs a random
access key that is put in the page URL's #fragment (never sent to the
model through any tool).

Limit: this keeps the model from approving its own requests through its
tools. An agent that also has a shell on the lab PC could still reach the
page; in deployment the lab runtime runs where the agent has no shell.
"""
from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from ..gate import ApprovalRequest, Approver

UI_HTML = (Path(__file__).parent / "ui.html").read_bytes()
MAX_BODY = 16 * 1024


class WebApprover(Approver):
    """Blocks the gate until a human answers on the page, or declines after `timeout` seconds."""

    def __init__(self, timeout: float = 120.0):
        self.timeout = timeout
        self._cv = threading.Condition()
        self._pending: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._history: Deque[Dict[str, Any]] = deque(maxlen=20)
        self._seq = 0
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

    def subscribe(self, listener: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
        """Call `listener(request)` for every new request (from the requesting thread). Returns unsubscribe."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def approve(self, request: ApprovalRequest) -> bool:
        with self._cv:
            self._seq += 1
            now = time.time()
            entry = {"id": "r%d" % self._seq, "title": request.title, "items": list(request.items),
                     "created": now, "expires": now + self.timeout, "answer": None, "by": None}
            self._pending[entry["id"]] = entry
            self._cv.notify_all()
        public = {"id": entry["id"], "title": entry["title"], "items": entry["items"], "seconds_left": int(self.timeout)}
        for listener in list(self._listeners):
            try:
                listener(public)
            except Exception:
                pass
        with self._cv:
            while entry["answer"] is None:
                left = entry["expires"] - time.time()
                if left <= 0:
                    entry["answer"], entry["by"] = False, "timeout"
                    break
                self._cv.wait(left)
            del self._pending[entry["id"]]
            entry["decided"] = time.time()
            self._history.appendleft(entry)
        if entry["by"] == "timeout":
            request.note = "no answer within %.0f s" % self.timeout
        elif entry["by"] != "human":
            request.note = "declined by %s" % entry["by"]
        return bool(entry["answer"])

    def inform(self, request: ApprovalRequest, reasons: List[str]) -> None:
        """Auto mode approved this without asking: show it with the reasons, like an answered request."""
        with self._cv:
            self._seq += 1
            now = time.time()
            entry = {"id": "a%d" % self._seq, "title": request.title, "items": list(request.items), "created": now,
                     "decided": now, "answer": True, "by": "auto mode", "reasons": list(reasons)}
            self._history.appendleft(entry)
        public = {"id": entry["id"], "title": entry["title"], "items": entry["items"], "auto": True,
                  "reasons": entry["reasons"]}
        for listener in list(self._listeners):
            try:
                listener(public)
            except Exception:
                pass

    def answer(self, request_id: str, approve: bool, by: str = "human") -> bool:
        with self._cv:
            entry = self._pending.get(request_id)
            if entry is None or entry["answer"] is not None:
                return False
            entry["answer"], entry["by"] = bool(approve), by
            self._cv.notify_all()
            return True

    def decline_all(self, by: str = "kill switch") -> int:
        with self._cv:
            n = 0
            for entry in self._pending.values():
                if entry["answer"] is None:
                    entry["answer"], entry["by"] = False, by
                    n += 1
            self._cv.notify_all()
            return n

    def wait_for_pending(self, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
        """For tests and scripted humans: the oldest open request, waiting up to `timeout`."""
        deadline = time.time() + timeout
        with self._cv:
            while True:
                for entry in self._pending.values():
                    if entry["answer"] is None:
                        return dict(entry)
                left = deadline - time.time()
                if left <= 0:
                    return None
                self._cv.wait(left)

    def pending_list(self) -> List[Dict[str, Any]]:
        with self._cv:
            now = time.time()
            return [{"id": e["id"], "title": e["title"], "items": e["items"],
                     "seconds_left": max(0, int(e["expires"] - now))}
                    for e in self._pending.values() if e["answer"] is None]

    def history_list(self) -> List[Dict[str, Any]]:
        with self._cv:
            return [{"title": e["title"], "items": e["items"], "answer": "approved" if e["answer"] else "declined",
                     "by": e["by"], "at": time.strftime("%H:%M:%S", time.localtime(e["decided"]))}
                    for e in self._history]


class ApprovalServer:
    """Serves the approval page and its small JSON API on 127.0.0.1."""

    def __init__(self, runtime: Any, port: int = 8765):
        self.runtime = runtime
        self.token = secrets.token_urlsafe(24)
        handler = self._handler()
        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError:  # port busy: take any free one
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = "http://127.0.0.1:%d/#%s" % (self.port, self.token)
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="approval-ui", daemon=True)

    def start(self) -> "ApprovalServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _handler(self):
        server = self
        hosts = {"127.0.0.1:%d", "localhost:%d"}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep stderr quiet
                pass

            def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, data: Any) -> None:
                self._send(code, json.dumps(data).encode("utf-8"))

            def _host_ok(self) -> bool:  # blocks DNS-rebinding pages
                return self.headers.get("Host", "") in {h % server.port for h in hosts}

            def _authorized(self) -> bool:
                return hmac.compare_digest(self.headers.get("X-Kelvin-Token", ""), server.token)

            def do_GET(self) -> None:
                if not self._host_ok():
                    return self._json(403, {"error": "bad host"})
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    return self._send(200, UI_HTML, "text/html; charset=utf-8")
                if path == "/api/state":
                    if not self._authorized():
                        return self._json(401, {"error": "missing or wrong access key"})
                    return self._json(200, server.runtime.ui_state())
                self._json(404, {"error": "not found"})

            def do_POST(self) -> None:
                try:  # read the body first: answering before it is read can abort the connection
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if not 0 <= length <= MAX_BODY:
                    self.close_connection = True
                    return self._json(413, {"error": "body too large"})
                raw = self.rfile.read(length)
                origin = self.headers.get("Origin")
                if not self._host_ok() or (origin and origin not in {"http://" + h % server.port for h in hosts}):
                    return self._json(403, {"error": "bad host or origin"})
                if not self._authorized():
                    return self._json(401, {"error": "missing or wrong access key"})
                try:
                    body = json.loads(raw or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError
                except ValueError:
                    return self._json(400, {"error": "bad request"})
                if self.path == "/api/answer":
                    ok = server.runtime.approver.answer(str(body.get("id")), bool(body.get("approve")))
                    return self._json(200 if ok else 409, {"ok": ok})
                if self.path == "/api/mode":
                    try:
                        return self._json(200, server.runtime.set_approval_mode(str(body.get("mode")),
                                                                               "human (approval page)"))
                    except ValueError as e:
                        return self._json(400, {"error": str(e)})
                if self.path == "/api/safe_state":
                    return self._json(200, {"ok": True, "results": server.runtime.kill(by="human (approval page)")})
                self._json(404, {"error": "not found"})

        return Handler
