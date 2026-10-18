"""TOPTICA DLC pro (and other DeCoP controllers): the command line on TCP port 1998.

Address: toptica://<ip>[:1998]

Card commands name the controller's parameters (from TOPTICA's Python SDK parameter tree, e.g. dlcpro v3_3_3):
    GET laser1:dl:cc:current-act            sent as (param-ref 'laser1:dl:cc:current-act)
    SET laser1:dl:cc:current-set {value}    sent as (param-set! 'laser1:dl:cc:current-set 85.5)
    SET laser1:dl:cc:enabled {value}        with bool_text ["#t", "#f"]
A reply starting with Error, or a negative status from param-set!, is an error. Kelvin sends nothing else: no
exec commands, so no user-level changes, network settings or firmware updates."""
from __future__ import annotations

import re
import socket
import urllib.parse
from typing import Any

from .adapters import AdapterError
from .cards import Card
from .drivers import DriverBackend, _fmt

PROMPT = b"\n> "
_PATH = re.compile(r"^[a-z][a-z0-9:_-]*$")


def decop_value(value: Any) -> str:
    if isinstance(value, str):
        if value in ("#t", "#f"):
            return value
        return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')
    return _fmt(value)


class DeCoP(DriverBackend):
    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        url = urllib.parse.urlparse(address)
        if not url.hostname:
            raise AdapterError("%s: a TOPTICA controller's address is toptica://<ip>[:1998]" % name)
        self.host, port = url.hostname, url.port or 1998
        timeout = float(card.connection.get("timeout_ms", 5000)) / 1000.0
        connect = getattr(module, "create_connection", None) or socket.create_connection   # tests pass a fake
        try:
            self.sock = connect((self.host, port), timeout)
        except OSError as e:
            raise AdapterError("%s: could not reach %s:%d (%s)" % (name, self.host, port, e)) from e
        self.buf = b""
        self._until_prompt()   # the welcome text

    def _until_prompt(self) -> str:
        while PROMPT not in self.buf:
            try:
                chunk = self.sock.recv(4096)
            except OSError as e:
                raise AdapterError("%s: no reply from the controller (%s)" % (self.name, e)) from e
            if not chunk:
                raise AdapterError("%s: the controller closed the connection" % self.name)
            self.buf += chunk
        text, _, self.buf = self.buf.partition(PROMPT)
        return text.decode("utf-8", "replace").replace("\r", "").strip()

    def _line(self, line: str) -> str:
        try:
            self.sock.sendall((line + "\n").encode("ascii"))
        except OSError as e:
            raise AdapterError("%s: lost the connection (%s)" % (self.name, e)) from e
        lines = [x for x in self._until_prompt().split("\n") if x.strip() and x.strip() != line]   # drop an echo
        reply = lines[-1].strip() if lines else ""
        if reply.lower().startswith("error"):
            raise AdapterError("%s: %s → %s" % (self.name, line, reply))
        return reply

    def _check(self, path: str) -> None:
        if not _PATH.match(path):
            raise AdapterError("%s: %r is not a parameter name" % (self.name, path))

    def set(self, path: str, value: Any) -> None:
        self._check(path)
        reply = self._line("(param-set! '%s %s)" % (path, decop_value(value)))
        try:
            status = int(reply)
        except ValueError:
            raise AdapterError("%s: setting %s answered %r" % (self.name, path, reply))
        if status < 0:
            raise AdapterError("%s: setting %s failed (error %d)" % (self.name, path, status))

    def get(self, path: str) -> Any:
        self._check(path)
        reply = self._line("(param-ref '%s)" % path)
        if len(reply) >= 2 and reply[0] == reply[-1] == '"':
            reply = reply[1:-1]
        return reply

    def identity(self) -> str:
        try:
            serial = self.get("serial-number")
        except AdapterError:
            serial = ""
        return "TOPTICA,%s,%s,%s" % (self.card.model, serial, self.host)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
