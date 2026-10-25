"""Boards that run their own software through Jupyter (PYNQ boards for QICK: RFSoC 4x2, ZCU111, ZCU216; a Red Pitaya;
any lab computer with a notebook server): Kelvin runs code there the way a person runs a notebook cell, once a person
has approved that code. QICK's Pyro4 name server and server are started this way, from the board's own notebooks
(qick/pyro4/00_nameserver.ipynb, then 01_server.ipynb), and the board's QICK version is read.

It speaks Jupyter's own interfaces: the login page (a password, or a token), /api/contents for a notebook's cells,
/api/kernels for a fresh Python kernel, and the kernel's websocket for running code and reading what it prints."""
from __future__ import annotations

import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple


class JupyterError(RuntimeError):
    pass


def board_url(address: str, port: int = 9090) -> str:
    """http://host:port for what a person or a scan gives: 192.168.3.1, 192.168.3.1:9090, http://board:8888/lab."""
    a = address.strip()
    u = urllib.parse.urlparse(a if "://" in a else "http://" + a)
    if not u.hostname:
        raise JupyterError("%r is not a board's address" % address)
    return "%s://%s:%d" % (u.scheme if u.scheme in ("http", "https") else "http", u.hostname, u.port or port)


class Jupyter:
    def __init__(self, url: str, password: str = "", token: str = "", timeout: float = 20.0):
        self.base = url.rstrip("/")
        self.password, self.token, self.timeout = password, token, timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.xsrf = ""

    def _request(self, method: str, path: str, body: Any = None, form: Optional[Dict[str, str]] = None
                 ) -> Tuple[int, bytes]:
        headers = {"User-Agent": "Kelvin (lab assistant)"}
        data = None
        if self.token:
            headers["Authorization"] = "token " + self.token
        if self.xsrf:
            headers["X-XSRFToken"] = self.xsrf
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode("utf-8"), "application/json"
        if form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return r.status, r.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as e:
            return e.code, e.read(65536)
        except (urllib.error.URLError, OSError) as e:
            raise JupyterError("the board's Jupyter at %s does not answer (%s)" % (self.base, getattr(e, "reason", e)))

    def _cookie(self, name: str) -> str:
        return next((c.value for c in self.jar if c.name == name), "")

    def login(self) -> None:
        """Signed in with the token, or with the password through the login page, as a browser does."""
        if self.token:
            return
        status, page = self._request("GET", "/login")
        m = re.search(rb'name="_xsrf"\s+value="([^"]+)"', page)
        self.xsrf = self._cookie("_xsrf") or (m.group(1).decode() if m else "")
        status, _ = self._request("GET", "/api/status")
        if status == 200:
            return   # no password on this server
        if not self.password:
            raise JupyterError("the board's Jupyter asks for a password")
        self._request("POST", "/login?next=%2F", form={"_xsrf": self.xsrf, "password": self.password})
        status, _ = self._request("GET", "/api/status")
        if status != 200:
            raise JupyterError("the board's Jupyter did not accept the password")

    def notebook(self, path: str) -> List[str]:
        """The code cells of a notebook on the board, in order."""
        status, body = self._request("GET", "/api/contents/%s?content=1&type=notebook"
                                     % urllib.parse.quote(path.strip("/")))
        if status != 200:
            raise JupyterError("no notebook %s on the board (%d)" % (path, status))
        cells = (json.loads(body.decode("utf-8")).get("content") or {}).get("cells") or []
        out = []
        for c in cells:
            if c.get("cell_type") == "code":
                source = c.get("source") or ""
                source = "".join(source) if isinstance(source, list) else str(source)
                if source.strip():
                    out.append(source)
        return out

    def start_kernel(self) -> str:
        status, body = self._request("POST", "/api/kernels", body={"name": "python3"})
        if status not in (200, 201):
            raise JupyterError("the board's Jupyter did not start a Python kernel (%d)" % status)
        return json.loads(body.decode("utf-8"))["id"]

    def execute(self, kernel: str, code: str, seconds: float = 30.0) -> Tuple[bool, str]:
        """Run code in a kernel, as a notebook cell: (finished, what it printed). A cell still running after `seconds`
        (a server) is left running in its kernel."""
        from websockets.exceptions import ConnectionClosed
        from websockets.sync.client import connect
        url = re.sub(r"^http", "ws", self.base) + "/api/kernels/%s/channels?session_id=%s" % (kernel, uuid.uuid4().hex)
        headers = {"Origin": self.base, "User-Agent": "Kelvin (lab assistant)"}
        cookies = "; ".join("%s=%s" % (c.name, c.value) for c in self.jar)
        if cookies:
            headers["Cookie"] = cookies
        if self.token:
            headers["Authorization"] = "token " + self.token
        msg_id = uuid.uuid4().hex
        request = {"header": {"msg_id": msg_id, "username": "kelvin", "session": uuid.uuid4().hex,
                              "msg_type": "execute_request", "version": "5.3",
                              "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                   "parent_header": {}, "metadata": {}, "channel": "shell", "buffers": [],
                   "content": {"code": code, "silent": False, "store_history": False, "user_expressions": {},
                               "allow_stdin": False, "stop_on_error": True}}
        out: List[str] = []
        try:
            with connect(url, additional_headers=headers, open_timeout=self.timeout, max_size=2 ** 24) as ws:
                ws.send(json.dumps(request))
                end = time.monotonic() + seconds
                while True:
                    left = end - time.monotonic()
                    if left <= 0:
                        return False, "".join(out)
                    try:
                        m = json.loads(ws.recv(timeout=left))
                    except TimeoutError:
                        return False, "".join(out)
                    except ConnectionClosed:
                        return False, "".join(out) + "(the board closed the connection)\n"
                    if (m.get("parent_header") or {}).get("msg_id") != msg_id:
                        continue
                    kind, content = (m.get("header") or {}).get("msg_type") or m.get("msg_type"), m.get("content") or {}
                    if kind == "stream":
                        out.append(str(content.get("text", "")))
                    elif kind in ("execute_result", "display_data"):
                        out.append(str((content.get("data") or {}).get("text/plain", "")) + "\n")
                    elif kind == "error":
                        out.append("%s: %s\n" % (content.get("ename"), content.get("evalue")))
                    elif kind == "status" and content.get("execution_state") == "idle":
                        return True, "".join(out)
        except OSError as e:
            raise JupyterError("could not reach the kernel on the board (%s)" % e)


def run(address: str, cells: List[str], password: str = "", token: str = "", seconds: float = 30.0,
        jupyter: Optional[Jupyter] = None) -> List[Tuple[bool, str]]:
    """Cells run in order in one new kernel on the board; it stops at a cell still running after `seconds` (a server,
    left running there). Returns each cell's (finished, printed)."""
    j = jupyter or Jupyter(board_url(address), password, token)
    j.login()
    kernel = j.start_kernel()
    results = []
    for cell in cells:
        done, said = j.execute(kernel, cell, seconds)
        results.append((done, said))
        if not done or re.search(r"^\w*Error: ", said, re.M):
            break
    return results
