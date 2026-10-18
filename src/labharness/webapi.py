"""Instruments with a web API: Kelvin talks to them over HTTP(S) with JSON, through the same safety gate.

Address: the instrument's base URL, e.g.
    http://192.168.1.50:20022                    a laser's REST API
    https://10.0.0.7/api?insecure=1              an instrument with a self-signed certificate (no check)
    http://host:8080?auth_env=MYLASER_TOKEN      a bearer token, taken from that environment variable (never a file)

Card commands (the method, the path below the base URL, and for writes a JSON body with {value} in it):
    GET  /v1/Basic/ActualOutputPower             the reply (a JSON number, string or true/false) is the value
    GET  /v1/Status#laser.state                  ...or one field of a JSON object, by a dotted path after #
    PUT  /v1/Basic/TargetAttenuatorPercentage {value}
    POST /v1/Basic/{value}                       e.g. a bool parameter with bool_text: [EnableOutput, CloseOutput]
    PUT  /v1/Shutter {"open": {value}}           a JSON object body
    SET  is PUT; PATCH and DELETE work as written.
The gate tells a drive's commands from others by the method and the path, so limits, ramps and approvals apply as
for SCPI instruments, and an unknown request to a draft card asks a person first."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from .adapters import AdapterError

METHODS = ("GET", "PUT", "POST", "PATCH", "DELETE", "SET")
_CMD = re.compile(r"^\s*(%s)\s+(\S+)(?:\s+(.*?))?\s*$" % "|".join(METHODS), re.I | re.S)


def parse(command: str) -> Tuple[str, str, Optional[str]]:
    m = _CMD.match(command or "")
    if not m:
        raise AdapterError("not a web API command: %r (expected GET <path>, or PUT/POST <path> <body>)" % command)
    method = m.group(1).upper()
    return ("PUT" if method == "SET" else method), m.group(2), m.group(3)


def body_of(text: Optional[str]) -> Optional[bytes]:
    """A write's body as JSON: the text itself when it is JSON (a number, true, an object), else as a JSON string."""
    if text is None or text == "":
        return None
    try:
        json.loads(text)
        return text.encode("utf-8")
    except ValueError:
        return json.dumps(text).encode("utf-8")


def pick(data: Any, field: str) -> Any:
    for part in [p for p in field.split(".") if p]:
        if isinstance(data, list):
            data = data[int(part)]
        elif isinstance(data, dict):
            if part not in data:
                raise AdapterError("the reply has no field %r (it has: %s)" % (part, ", ".join(map(str, data))))
            data = data[part]
        else:
            raise AdapterError("cannot take field %r of %r" % (part, data))
    return data


def as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return ""
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value)


class HttpInstrument:
    """write(command) / query(command) / close(), like a VISA backend."""
    simulated = False

    def __init__(self, name: str, card: Any, address: str, module: Any = None):
        import threading
        self.name, self.card = name, card
        url = urllib.parse.urlparse(address)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise AdapterError("%s: a web instrument's address is http(s)://host[:port][/base]" % name)
        q = dict(urllib.parse.parse_qsl(url.query))
        self.base = "%s://%s%s" % (url.scheme, url.netloc, url.path.rstrip("/"))
        self.timeout = float(card.connection.get("timeout_ms", 10000)) / 1000.0
        self.headers: Dict[str, str] = {"Accept": "application/json", "User-Agent": "Kelvin"}
        if q.get("auth_env"):
            token = os.environ.get(q["auth_env"], "")
            if not token:
                raise AdapterError("%s: the environment variable %s (its access token) is not set" % (name, q["auth_env"]))
            self.headers["Authorization"] = "Bearer " + token
        self.context = None
        if url.scheme == "https" and q.get("insecure") in ("1", "true", "yes"):
            import ssl   # only here: some Pythons cannot load it, and plain http needs none
            self.context = ssl.create_default_context()
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE
        self.open = module or urllib.request.urlopen   # tests pass a fake
        self.lock = threading.Lock()

    def _request(self, method: str, path: str, body: Optional[bytes]) -> Any:
        path, _, field = path.partition("#")
        url = self.base + (path if path.startswith("/") else "/" + path)
        headers = dict(self.headers)
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.lock:
                kw = {"timeout": self.timeout}
                if self.context is not None:
                    kw["context"] = self.context
                with self.open(req, **kw) as r:
                    raw = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode("utf-8", "replace") if hasattr(e, "read") else ""
            raise AdapterError("%s: %s %s answered HTTP %d %s" % (self.name, method, path, e.code, detail.strip()))
        except (urllib.error.URLError, OSError) as e:
            raise AdapterError("%s: could not reach %s (%s)" % (self.name, self.base, getattr(e, "reason", e)))
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            data = text   # a plain-text reply
        if field:
            data = pick(data, field)
        elif isinstance(data, dict) and len(data) == 1:
            data = next(iter(data.values()))
        return data

    def write(self, command: str) -> None:
        method, path, rest = parse(command)
        if method == "GET":
            raise AdapterError("%s: %r reads, it does not set" % (self.name, command))
        self._request(method, path, body_of(rest))

    def query(self, command: str) -> str:
        cmd = command.strip()
        if cmd.upper() == "*IDN?":
            return self.identity()
        method, path, rest = parse(cmd)
        if method != "GET":
            raise AdapterError("%s: %r sets, it does not read" % (self.name, command))
        return as_text(self._request("GET", path, None))

    def identity(self) -> str:
        q = (self.card.idn_query or "").strip()
        if q.upper().startswith("GET "):
            return as_text(self._request("GET", parse(q)[1], None))
        return "%s,%s" % (self.card.model, self.base)

    def close(self) -> None:
        pass
