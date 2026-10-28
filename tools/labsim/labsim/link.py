"""A program's connection to its bench's server (used by the VISA library, the serial shim and the SDK shims)."""
from __future__ import annotations

import json
import os
import threading
from multiprocessing.connection import Client
from typing import Any, Dict, Optional, Tuple


class LabsimError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def home() -> str:
    h = os.environ.get("LABSIM_HOME")
    if not h:
        raise LabsimError("no_server", "LABSIM_HOME is not set: start a bench with `python -m labsim up <bench.yaml> "
                                       "--home <folder>` and point LABSIM_HOME at that folder")
    return h


def server_info(folder: Optional[str] = None) -> Dict[str, Any]:
    path = os.path.join(folder or home(), "server.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        raise LabsimError("no_server", "no labsim server has run in %s (no server.json)" % (folder or home()))


def address(folder: Optional[str] = None) -> Tuple[Tuple[str, int], bytes]:
    info = server_info(folder)
    return ("127.0.0.1", int(info["port"])), bytes.fromhex(info["key"])


class Link:
    """One connection; calls on it are serialized."""

    def __init__(self, folder: Optional[str] = None):
        addr, key = address(folder)
        try:
            self.conn = Client(addr, authkey=key)
        except OSError as e:
            raise LabsimError("no_server", "the labsim server of %s isn't running (%s)" % (folder or home(), e))
        self.lock = threading.Lock()

    def call(self, op: str, **kw: Any) -> Dict[str, Any]:
        kw["op"] = op
        with self.lock:
            try:
                self.conn.send(kw)
                return self.conn.recv()
            except (EOFError, OSError) as e:
                raise LabsimError("conn_lost", "lost the labsim server (%s)" % e)

    def must(self, op: str, **kw: Any) -> Dict[str, Any]:
        r = self.call(op, **kw)
        if "err" in r:
            raise LabsimError(r["err"], r.get("msg") or r["err"])
        return r

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


_local = threading.local()


def shared() -> Link:
    """A link for this thread (SDK shims: a camera waiting on an exposure must not block another thread's call)."""
    link = getattr(_local, "link", None)
    if link is None:
        link = _local.link = Link()
    return link
