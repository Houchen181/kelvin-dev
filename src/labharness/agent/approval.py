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


def _minutes(seconds: float) -> str:
    return "%.0f s" % seconds if seconds < 90 else "%.0f min" % (seconds / 60)


def who_text(who: Dict[str, Any]) -> str:
    """A person who answered, in words: their account (when people sign in) and where they answered from, e.g.
    "Ada (phone Pixel 7)" or "a person (portal)"."""
    where = ", ".join(str(x) for x in (who.get("channel"), who.get("device")) if x)
    return "%s%s" % (who.get("account") or "a person", " (%s)" % where if where else "")


class WebApprover(Approver):
    """Blocks the gate until a human answers on the page, or declines after `timeout` seconds. A request nobody
    answered by then goes to people away from the computer instead (escalate: the phone, email, a webhook) and waits
    escalate_s more before it is declined: in the night a card declined after 5 minutes ended a whole queue."""

    def __init__(self, timeout: float = 120.0, escalate_s: float = 0.0):
        self.timeout = timeout
        # escalate(request): tells people away from the computer about a request nobody answered in `timeout`; True
        # when anyone could be reached (then it waits escalate_s more), False when nobody can (declined as before)
        self.escalate: Optional[Callable[[Dict[str, Any]], bool]] = None
        self.escalate_s = escalate_s
        self._cv = threading.Condition()
        self._pending: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._history: Deque[Dict[str, Any]] = deque(maxlen=20)
        self._seq = 0
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        # who declines a request before anyone sees it (asked from the requesting thread): the client that cancelled
        # the call asking it (LabRuntime.cancel_call). None: it is shown as usual
        self.vetoes: List[Callable[[], Optional[str]]] = []
        # where a request acts, shown on its card: SIMULATOR or REAL HARDWARE (LabRuntime.where); None: no label
        self.where: Optional[Callable[[ApprovalRequest], Optional[str]]] = None
        # the lab's lessons on the instruments a request names, shown on its card (LabRuntime.lab_lessons_for)
        self.lessons: Optional[Callable[[ApprovalRequest], List[str]]] = None

    def subscribe(self, listener: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
        """Call `listener(request)` for every new request (from the requesting thread). Returns unsubscribe."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def approve(self, request: ApprovalRequest) -> bool:
        why = next((w for w in (veto() for veto in list(self.vetoes)) if w), None)
        try:
            where = self.where(request) if self.where else None
        except Exception:
            where = "REAL HARDWARE"   # unsure: say the instruments may be real
        try:
            lessons = list(self.lessons(request)) if self.lessons else []
        except Exception:   # the lab's memory never holds up an approval
            lessons = []
        with self._cv:
            self._seq += 1
            now = time.time()
            entry = {"id": "r%d" % self._seq, "title": request.title, "items": list(request.items),
                     "created": now, "expires": now + self.timeout, "answer": None, "by": None,
                     "thread": threading.get_ident(), "where": where, "lessons": lessons}
            if why:   # never shown: nobody is left to act on a yes
                entry.update(answer=False, by=why, decided=now)
                self._history.appendleft(entry)
                request.note = "declined by %s" % why
                return False
            self._pending[entry["id"]] = entry
            self._cv.notify_all()
        public = {"id": entry["id"], "title": entry["title"], "items": entry["items"], "seconds_left": int(self.timeout),
                  "where": where, "lessons": lessons}
        for listener in list(self._listeners):
            try:
                listener(public)
            except Exception:
                pass
        with self._cv:
            while entry["answer"] is None:
                left = entry["expires"] - time.time()
                if left > 0:
                    self._cv.wait(left)
                elif not entry.get("away") and self._escalate(entry, public):
                    continue   # it waits for someone away from the computer now
                elif entry["answer"] is None:
                    entry["answer"], entry["by"] = False, "timeout"
            del self._pending[entry["id"]]
            entry["decided"] = time.time()
            self._history.appendleft(entry)
        if entry["by"] == "timeout" and entry.get("escalated"):
            request.note = "no answer within %.0f s at the computer, nor in %s after it went to the phone" % (
                self.timeout, _minutes(entry["decided"] - entry["escalated"]))
        elif entry["by"] == "timeout":
            request.note = "no answer within %.0f s" % self.timeout
        elif entry.get("who"):   # the record says who answered, from where, and when (reports print it)
            request.note = "%s by %s at %s" % ("approved" if entry["answer"] else "declined", who_text(entry["who"]),
                                               time.strftime("%H:%M:%S", time.localtime(entry["decided"])))
        elif entry["by"] != "human":
            request.note = "declined by %s" % entry["by"]
        return bool(entry["answer"])

    def _escalate(self, entry: Dict[str, Any], public: Dict[str, Any]) -> bool:
        """Called with the lock held, once, when nobody answered in `timeout`: tell people away from the computer
        (outside the lock: sending may take a moment, and an answer may come meanwhile). True: it waits escalate_s more."""
        entry["away"] = True   # asked once, reached or not
        if self.escalate is None or self.escalate_s <= 0:
            return False
        now = time.time()
        until = now + self.escalate_s
        self._cv.release()
        try:
            reached = bool(self.escalate(dict(public, seconds_left=int(self.escalate_s), escalated=True,
                                              until=until)))
        except Exception:
            reached = False
        finally:
            self._cv.acquire()
        if reached and entry["answer"] is None:
            entry["escalated"], entry["expires"] = now, until
            self._cv.notify_all()
        return reached

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

    def answer(self, request_id: str, approve: bool, by: str = "human", who: Optional[Dict[str, Any]] = None) -> bool:
        """A person's answer. who: their account, the channel (portal, phone, approval page) and device."""
        with self._cv:
            entry = self._pending.get(request_id)
            if entry is None or entry["answer"] is not None:
                return False
            entry["answer"], entry["by"], entry["who"] = bool(approve), by, who
            self._cv.notify_all()
            return True

    def title_of(self, request_id: str) -> Optional[str]:
        with self._cv:
            entry = self._pending.get(request_id)
            return entry["title"] if entry else None

    def decline_all(self, by: str = "stop button") -> int:
        return self.decline_from(None, by)

    def decline_from(self, thread: Optional[int], by: str) -> int:
        """Decline the open requests asked from that thread (None: every open request). How many were declined."""
        with self._cv:
            n = 0
            for entry in self._pending.values():
                if entry["answer"] is None and thread in (None, entry.get("thread")):
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
                     "seconds_left": max(0, int(e["expires"] - now)), "where": e.get("where"),
                     "escalated": bool(e.get("escalated")), "lessons": e.get("lessons") or []}
                    for e in self._pending.values() if e["answer"] is None]

    def history_list(self) -> List[Dict[str, Any]]:
        with self._cv:
            return [{"title": e["title"], "items": e["items"], "answer": "approved" if e["answer"] else "declined",
                     "by": who_text(e["who"]) if e.get("who") else e["by"], "where": e.get("where"),
                     "at": time.strftime("%H:%M:%S", time.localtime(e["decided"]))}
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
                    answer = getattr(server.runtime, "answer", None)
                    ok = answer(str(body.get("id")), bool(body.get("approve")), "human (approval page)",
                                "approval page") if answer else \
                        server.runtime.approver.answer(str(body.get("id")), bool(body.get("approve")))
                    return self._json(200 if ok else 409, {"ok": ok})
                if self.path == "/api/mode":
                    try:
                        return self._json(200, server.runtime.set_approval_mode(str(body.get("mode")),
                                                                               "human (approval page)",
                                                                               pin=body.get("pin")))
                    except PermissionError as e:   # the lab owner's PIN (accounts.OwnerPin)
                        return self._json(403, {"error": str(e), "pin": True})
                    except ValueError as e:
                        return self._json(400, {"error": str(e)})
                if self.path == "/api/stop":
                    return self._json(200, {"ok": True, "declined": server.runtime.stop(by="human (approval page)")})
                self._json(404, {"error": "not found"})

        return Handler
