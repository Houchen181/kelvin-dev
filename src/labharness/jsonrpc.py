"""Instruments with a JSON-RPC interface: JSON-RPC 2.0 requests over the network, through the same safety gate.

Address: jsonrpc://<host>[:<port>], e.g.
    jsonrpc://192.168.1.40:9090        attocube's eNSPIRE cryostat controller (attoDRY2100e, 2100XL), at the address
                                       of its WebApp; attocube's AMC and IDS answer the same way
The port, when the address names none, is the card's jsonrpc.port.

The card says how its instrument speaks JSON-RPC, under jsonrpc:
    jsonrpc:
      port: 9090                       # when the address names none
      transport: tcp                   # tcp: one JSON request a line over a TCP connection (default); http: POSTed
      path: /                          #   to this path, with transport: http
      fields: { api: 2 }               # what every request carries besides jsonrpc, method, params and id
      errors: first                    # where the instrument's own error number is in an answer's list: first
                                       # (default), none, or its position; 0 means fine, and it is not a value
      errors_at: { <method>: 2 }       # a method whose error number sits elsewhere
      identity: [<method>, ...]        # read for who it is: "<vendor>,<answer>,<answer>..."
      switches:                        # an on/off the instrument has two methods for
        <name>: { on: <method>, off: <method> }
      open: ["<method> [arguments]"]   # called once connected, before anything else
      close: ["<method> [arguments]"]  # called before the connection closes
Card commands call one method each, with its arguments after its name (numbers, true and false, words, or "quoted
text"), separated by spaces:
    GET <method> [arguments]           its answer: its one value, or its values separated by commas (field: picks one)
    SET <method> [arguments] {value}   the value goes last
    SET <switch> [arguments] {value}   the switch's on or off method, with the arguments
Only methods the card names are ever called (a request for another is refused, as the gate refuses an unknown
command), readings call only methods whose names read (get..., is..., has...: checked when the card loads), and a
request is never sent twice: one whose answer was lost may have acted already."""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Set, Tuple

from .adapters import AdapterError
from .cards import RPC_CALL, Card
from .drivers import DriverBackend

TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|\S+')
MAX_ANSWER = 1 << 20   # bytes one answer may take; more is not an instrument's answer


def arguments(text: str) -> List[Any]:
    """'0 1.5 true "two words" word' -> [0, 1.5, True, 'two words', 'word']: each as JSON where it is JSON (a number,
    true, false, null, a list), else the word itself."""
    out: List[Any] = []
    for token in TOKEN.findall(text or ""):
        try:
            out.append(json.loads(token))
        except ValueError:
            if token.startswith('"'):
                raise AdapterError("%s is not a quoted text" % token)
            out.append(token)
    return out


def text_of(values: List[Any]) -> str:
    """An answer's values as Kelvin reads a reply: one value as itself, several separated by commas."""
    def one(v: Any) -> str:
        if isinstance(v, bool):
            return "1" if v else "0"
        if v is None:
            return ""
        if isinstance(v, float):
            return "%.12g" % v
        if isinstance(v, (int, str)):
            return str(v)
        return json.dumps(v)
    return ",".join(one(v) for v in values)


def _on(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("on", "true", "yes", "1")


def card_methods(card: Card) -> Tuple[Set[str], Set[str]]:
    """The methods (and switches) the card's commands call: (those that set, those that read)."""
    sets, gets = set(), set()
    for spec in card.params.values():
        for tmpl, names in ((spec.set_cmd, sets), (spec.get_cmd, gets)):
            m = RPC_CALL.match(tmpl.text) if tmpl is not None else None
            if m:
                names.add(m.group(2))
    for action in card.actions.values():
        for text in action.send:
            m = RPC_CALL.match(text)
            if m and m.group(1).upper() == "SET":
                sets.add(m.group(2))
    m = RPC_CALL.match(card.idn_query or "")
    if m and m.group(1).upper() == "GET":
        gets.add(m.group(2))
    return sets, gets


class JsonRpcInstrument(DriverBackend):
    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        spec = card.jsonrpc or {}
        if not spec:
            raise AdapterError("%s: card %s has no jsonrpc section, so it cannot use a jsonrpc:// address"
                               % (name, card.id))
        url = urllib.parse.urlparse(address)
        try:
            port = url.port
        except ValueError:
            port = None
        self.host = url.hostname or ""
        self.port = port or spec.get("port")
        if not self.host or not self.port:
            raise AdapterError("%s: the address of a JSON-RPC instrument is jsonrpc://<its IP address>:<port>%s"
                               % (name, " (%s's port is %s)" % (card.model, spec["port"]) if spec.get("port") else ""))
        self.timeout = float(card.connection.get("timeout_ms", 5000)) / 1000.0
        self.fields = dict(spec.get("fields") or {})
        self.errors = spec.get("errors", "first")
        self.errors_at = dict(spec.get("errors_at") or {})
        self.switches: Dict[str, Dict[str, str]] = dict(spec.get("switches") or {})
        self.http = spec.get("transport") == "http"
        self.path = str(spec.get("path") or "/")
        self.sets, self.gets = card_methods(card)
        self.connect = getattr(module, "create_connection", None) or socket.create_connection   # tests: a fake
        self.urlopen = getattr(module, "urlopen", None) or urllib.request.urlopen
        self.sock: Any = None
        self.buf = ""
        self.next_id = 0
        if not self.http:
            self._open()
        try:
            for step in spec.get("open") or []:
                self._step(step)
        except AdapterError:
            self._drop()
            raise

    @property
    def quiet_close(self) -> bool:   # closing sends the instrument nothing, unless the card has calls for it
        return not (self.card.jsonrpc or {}).get("close")

    # ------------------------------------------------------------------ the link

    def _open(self) -> None:
        try:
            self.sock = self.connect((self.host, int(self.port)), self.timeout)
        except OSError as e:
            raise AdapterError("%s: could not reach %s:%s (%s). Is it on, at this address, and on this computer's "
                               "network?" % (self.name, self.host, self.port, e)) from e
        try:
            self.sock.settimeout(self.timeout)
        except (OSError, AttributeError):
            pass
        self.buf = ""

    def _drop(self) -> None:
        sock, self.sock = self.sock, None
        self.buf = ""
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _answer(self) -> Dict[str, Any]:
        """The next JSON object on the link (one a line, or one after another)."""
        dec = json.JSONDecoder()
        while True:
            text = self.buf.lstrip()
            if text.startswith("{"):
                try:
                    obj, end = dec.raw_decode(text)
                except ValueError:
                    obj, end = None, 0   # not all here yet
                if end:
                    self.buf = text[end:]
                    if isinstance(obj, dict):
                        return obj
                    continue
            elif text:
                self._drop()
                raise AdapterError("%s: the instrument's answer is not JSON-RPC: %r" % (self.name, text[:80]))
            if len(self.buf) > MAX_ANSWER:
                self._drop()
                raise AdapterError("%s: an answer longer than %d bytes" % (self.name, MAX_ANSWER))
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:   # the link stays: a late answer is told apart by its id
                raise AdapterError("%s: no answer within %g s" % (self.name, self.timeout))
            except OSError as e:
                self._drop()
                raise AdapterError("%s: lost the connection to %s:%s (%s)" % (self.name, self.host, self.port, e))
            if not chunk:
                self._drop()
                raise AdapterError("%s: the instrument closed the connection" % self.name)
            self.buf += chunk.decode("utf-8", "replace")

    def _over_tcp(self, request: Dict[str, Any]) -> Dict[str, Any]:
        if self.sock is None:   # dropped after a failure: connected again (the failed request is never sent again)
            self._open()
        try:
            self.sock.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
        except OSError as e:
            self._drop()
            raise AdapterError("%s: lost the connection to %s:%s (%s)" % (self.name, self.host, self.port, e))
        while True:
            answer = self._answer()
            if answer.get("id") == request["id"]:
                return answer
            # the answer to a request that timed out, or a notification: not this one's

    def _over_http(self, request: Dict[str, Any]) -> Dict[str, Any]:
        url = "http://%s:%s%s" % (self.host, self.port, self.path)
        req = urllib.request.Request(url, data=json.dumps(request).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self.urlopen(req, timeout=self.timeout) as r:
                body = r.read(MAX_ANSWER + 1)
        except urllib.error.HTTPError as e:
            body = e.read(MAX_ANSWER + 1) if hasattr(e, "read") else b""
            if not body.strip().startswith(b"{"):
                raise AdapterError("%s: %s answered HTTP %s" % (self.name, url, e.code))
        except (OSError, ValueError) as e:
            raise AdapterError("%s: could not reach %s (%s)" % (self.name, url, e))
        try:
            answer = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            raise AdapterError("%s: the instrument's answer is not JSON-RPC: %r" % (self.name, body[:80]))
        if not isinstance(answer, dict):
            raise AdapterError("%s: the instrument's answer is not JSON-RPC: %r" % (self.name, body[:80]))
        return answer

    def call(self, method: str, params: List[Any]) -> List[Any]:
        """One request; its answer's values, without the instrument's error number (raised when it isn't 0)."""
        self.next_id += 1
        request = dict(self.fields, jsonrpc="2.0", method=method, params=list(params), id=self.next_id)
        answer = self._over_http(request) if self.http else self._over_tcp(request)
        error = answer.get("error")
        if error is not None:
            message = error.get("message") if isinstance(error, dict) else error
            code = error.get("code") if isinstance(error, dict) else None
            raise AdapterError("%s: %s was refused: %s%s" % (self.name, method, message,
                                                             " (%s)" % code if code is not None else ""))
        result = answer.get("result")
        if not isinstance(result, list):
            return [result] if result is not None else []
        at = self.errors_at.get(method, self.errors)
        if at == "none" or not result:
            return list(result)
        i = 0 if at == "first" else int(at)
        i = i if i >= 0 else len(result) + i
        if not 0 <= i < len(result) or isinstance(result[i], bool) or not isinstance(result[i], int):
            return list(result)   # no error number where the card says: the values as they came
        if result[i] != 0:
            raise AdapterError("%s: %s: the instrument answered with its error %d" % (self.name, method, result[i]))
        return result[:i] + result[i + 1:]

    def _step(self, text: str) -> None:
        method, _, rest = text.strip().partition(" ")
        self.call(method, arguments(rest))

    # ------------------------------------------------------------------ what the gate and the lab call

    def write(self, command: str) -> None:
        m = RPC_CALL.match(command or "")
        if not m or m.group(1).upper() != "SET":
            raise AdapterError("%s: %r is not SET <method> [arguments] <value>" % (self.name, command))
        method, args = m.group(2), arguments(command[m.end():])
        if method not in self.sets:
            raise AdapterError("%s: %s is not on the card, so Kelvin does not call it" % (self.name, method))
        with self.lock:
            if method in self.switches:
                if not args:
                    raise AdapterError("%s: %s needs on or off" % (self.name, method))
                sw = self.switches[method]
                self.call(sw["on"] if _on(args[-1]) else sw["off"], args[:-1])
                return
            self.call(method, args)

    def query(self, command: str) -> str:
        if command.strip().upper() == "*IDN?" or command.strip() == (self.card.idn_query or "").strip():
            return self.identity()
        m = RPC_CALL.match(command or "")
        if not m or m.group(1).upper() != "GET":
            raise AdapterError("%s: %r is not GET <method> [arguments]" % (self.name, command))
        method = m.group(2)
        if method not in self.gets:
            raise AdapterError("%s: %s is not a reading on the card, so Kelvin does not call it" % (self.name, method))
        with self.lock:
            return text_of(self.call(method, arguments(command[m.end():])))

    def identity(self) -> str:
        methods = (self.card.jsonrpc or {}).get("identity") or []
        if not methods:
            return "%s,%s,%s" % (self.card.vendor or "JSON-RPC", self.card.model, self.host)
        with self.lock:
            return ",".join([self.card.vendor or "JSON-RPC"] + [text_of(self.call(m, [])) for m in methods])

    def close(self) -> None:
        for step in (self.card.jsonrpc or {}).get("close") or []:
            try:
                with self.lock:
                    self._step(step)
            except Exception:
                pass
        self._drop()
