"""Instruments driven through their maker's library: a Python module or a DLL, as in the maker's example scripts.

Address: lib://<what the library connects to>?python=<python.exe>&path=<folder with the maker's files>, e.g.
    lib://COM4?python=C:/Python38-32/python.exe&path=C:/attocube/attoDRYLib
The part after lib:// is {address} in the card's open calls (a COM port, an IP address, a serial number). python is
the interpreter that can load the library (a 32-bit one for a 32-bit DLL; by default Kelvin's own), and path is the
folder holding the maker's module or DLL. Or env=<name>: one of the environments Kelvin installs a maker's package
or files into, once a person approves (envs.py), with the Python the library needs:
    lib://192.168.1.20?env=attodry

The card says what to load and how to call it, under library:
    library:
      dll: attoDRYLib.dll                  # or module: PyAttoDRY (with object: AttoDRY, create: false)
      prefix: AttoDRY_Interface_
      returns: error                       # every function returns an error code, 0 when fine
      functions:                           # C signatures: arguments in, one value out through a pointer
        getSampleTemperature: { out: float }
        setUserTemperature: { args: [float] }
      open: ["begin {device}", "Connect {address}", "wait 10"]
      close: ["Disconnect", "end"]
      toggles:                             # a switch the library only toggles: Kelvin reads its state first
        setTemperatureControl: { toggle: toggleFullTemperatureControl, state: isControllingTemperature }
Card commands call one function each: "GET getSampleTemperature", "SET setUserTemperature {value}" (more fixed
arguments may follow the name). A library whose functions sit on sub-objects of the object it creates, as attocube's
network instruments do (device = Device(ip); device.connect(); device.sample.getTemperature()), is written
    library: { module: atto_device.CRYO2100, object: Device, create: true, create_args: ["{address}"],
               open: [connect], close: [close] }
with commands such as "GET sample.getTemperature". The library runs in a helper process (data/bridge/
kelvin_bridge.py), so a crash or a 32-bit DLL never takes Kelvin down, only functions the card names can be called,
and only on objects the maker's own package made."""
from __future__ import annotations

import collections
import json
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .adapters import AdapterError
from .cards import Card
from .drivers import DriverBackend

BRIDGE = Path(__file__).parent / "data" / "bridge" / "kelvin_bridge.py"
_CALL = re.compile(r"^\s*(SET|GET)\s+([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*)(.*)$", re.I | re.S)


def bridge_command(python: Optional[str] = None) -> List[str]:
    """How to start the helper: with the given Python, or with Kelvin's own (the app runs it with --bridge)."""
    if python:
        return [python, "-u", str(BRIDGE)]
    if getattr(sys, "frozen", False):
        return [sys.executable, "--bridge", str(BRIDGE)]
    return [sys.executable, "-u", str(BRIDGE)]


def split_call(text: str) -> Tuple[str, List[str]]:
    """'Connect COM4' -> ('Connect', ['COM4']); arguments are separated by spaces or commas."""
    parts = [p for p in re.split(r"[\s,]+", text.strip()) if p]
    if not parts:
        raise AdapterError("empty call")
    return parts[0], parts[1:]


def card_calls(card: Card) -> Tuple[Set[str], Set[str]]:
    """The functions the card's commands call: (those that set, those that read)."""
    sets, gets = set(), set()
    for spec in card.params.values():
        for tmpl, names in ((spec.set_cmd, sets), (spec.get_cmd, gets)):
            m = _CALL.match(tmpl.text) if tmpl is not None else None
            if m:
                names.add(m.group(2))
    m = _CALL.match(card.idn_query or "")
    if m and m.group(1).upper() == "GET":
        gets.add(m.group(2))
    return sets, gets


class Bridge:
    """The helper process: one request at a time, each answered within a timeout."""

    def __init__(self, name: str, command: List[str]):
        self.name = name
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, creationflags=flags)
        except OSError as e:
            raise AdapterError("%s: could not start %s (%s). Check python= in the address." % (name, command[0], e))
        self.lines: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.errors: "collections.deque[str]" = collections.deque(maxlen=40)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain, daemon=True).start()
        self.next_id = 0

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _drain(self) -> None:
        for line in self.proc.stderr:
            self.errors.append(line.decode("utf-8", "replace").rstrip())

    def request(self, op: str, timeout: float, **fields: Any) -> Any:
        self.next_id += 1
        rid = self.next_id
        try:
            self.proc.stdin.write((json.dumps(dict(fields, id=rid, op=op)) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except OSError:
            raise AdapterError("%s: the library's helper has stopped%s" % (self.name, self._said()))
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise AdapterError("%s: the library did not answer %s within %g s%s" % (self.name, fields.get("name", op),
                                                                                        timeout, self._said()))
            if line is None:
                raise AdapterError("%s: the library's helper stopped%s" % (self.name, self._said()))
            try:
                reply = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            if reply.get("id") != rid:
                continue   # the answer to a request that timed out
            if not reply.get("ok"):
                raise AdapterError("%s: %s" % (self.name, reply.get("error")))
            return reply.get("value")

    def _said(self) -> str:
        last = [e for e in self.errors if e.strip()][-3:]
        return " (it said: %s)" % " | ".join(last) if last else ""

    def stop(self) -> None:
        try:
            self.request("quit", 2)
        except AdapterError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class LibraryInstrument(DriverBackend):
    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        lib = card.library
        if not lib:
            raise AdapterError("%s: card %s has no library section, so it cannot use a lib:// address" % (name, card.id))
        url = urllib.parse.urlparse(address)
        q = dict(urllib.parse.parse_qsl(url.query))
        self.target = (url.netloc + url.path).strip("/")
        self.timeout = float(card.connection.get("timeout_ms", 10000)) / 1000.0
        self.sets, self.gets = card_calls(card)
        self.toggles: Dict[str, Dict[str, str]] = dict(lib.get("toggles") or {})
        python = q.get("python")
        if q.get("env"):   # one of Kelvin's instrument environments (envs.py): its Python, and the maker's files
            from . import envs
            try:
                python = str(envs.python_of(q["env"]))
            except ValueError as e:
                raise AdapterError("%s: %s" % (name, e)) from e
            if not Path(python).is_file():
                raise AdapterError("%s: there is no instrument environment %r on this computer yet: Kelvin makes it "
                                   "with install_for_instrument" % (name, q["env"]))
            if not q.get("path") and (envs.env_dir(q["env"]) / "files").is_dir():
                q["path"] = str(envs.env_dir(q["env"]) / "files")
        start = module or Bridge   # tests may pass their own
        self.bridge = start(name, bridge_command(python))
        self.python: Any = None
        try:
            self.python = self.bridge.request("ping", 30)
            load = {k: lib[k] for k in ("module", "object", "create", "create_args", "dll", "prefix", "convention",
                                         "returns", "functions") if k in lib}
            if load.get("create_args"):   # Device(ip): the address as the examples pass it
                load["create_args"] = [a.replace("{address}", self.target) if isinstance(a, str) else a
                                       for a in load["create_args"]]
            self.loaded = self.bridge.request("load", 60, path=q.get("path", ""), **load)
            for step in lib.get("open") or []:
                self._step(step)
        except AdapterError as e:
            self.bridge.stop()
            bits = self.python.get("bits") if isinstance(self.python, dict) else None
            hint = (" A 32-bit DLL needs a 32-bit Python: set python= in the address, or use an environment made with "
                    "bits 32." if bits == 64 and lib.get("dll") and "WinError 193" in str(e) else "")
            if "No module named" in str(e) and q.get("env"):
                hint = (" Kelvin installs the maker's package into environment %s once a person approves "
                        "(install_for_instrument with env %s and packages %s, or files for the maker's own folder)"
                        % (q["env"], q["env"], json.dumps(card.requires) if card.requires else "<its pip name>"))
            raise AdapterError(str(e) + hint) from e

    def _step(self, text: str) -> Any:
        fn, args = split_call(text.replace("{address}", self.target))
        if fn == "wait":
            time.sleep(float(args[0]) if args else 1.0)
            return None
        return self.bridge.request("call", self.timeout, name=fn, args=args)

    def write(self, command: str) -> None:
        m = _CALL.match(command or "")
        if not m or m.group(1).upper() != "SET":
            raise AdapterError("%s: %r is not SET <function> [arguments]" % (self.name, command))
        fn, args = m.group(2), split_call("x " + m.group(3))[1]
        if fn not in self.sets:
            raise AdapterError("%s: %s is not on the card, so Kelvin does not call it" % (self.name, fn))
        with self.lock:
            if fn in self.toggles:
                want = bool(float(args[-1])) if args and re.match(r"^[+-]?[0-9.]+$", args[-1]) else \
                    str(args[-1]).lower() in ("on", "true", "yes")
                state = self.bridge.request("call", self.timeout, name=self.toggles[fn]["state"], args=[])
                if bool(state) != want:
                    self.bridge.request("call", self.timeout, name=self.toggles[fn]["toggle"], args=[])
                return
            self.bridge.request("call", self.timeout, name=fn, args=args)

    def query(self, command: str) -> str:
        if command.strip().upper() == "*IDN?" or command.strip() == (self.card.idn_query or "").strip():
            return self.identity()
        m = _CALL.match(command or "")
        if not m or m.group(1).upper() != "GET":
            raise AdapterError("%s: %r is not GET <function> [arguments]" % (self.name, command))
        fn, args = m.group(2), split_call("x " + m.group(3))[1]
        if fn not in self.gets:
            raise AdapterError("%s: %s is not a reading on the card, so Kelvin does not call it" % (self.name, fn))
        with self.lock:
            return _text(self.bridge.request("call", self.timeout, name=fn, args=args))

    def identity(self) -> str:
        m = _CALL.match(self.card.idn_query or "")
        if m and m.group(1).upper() == "GET":
            with self.lock:
                return _text(self.bridge.request("call", self.timeout, name=m.group(2),
                                                 args=split_call("x " + m.group(3))[1]))
        return "%s,%s,%s" % (self.card.vendor or "library", self.card.model, self.target)

    def close(self) -> None:
        for step in (self.card.library or {}).get("close") or []:
            try:
                self._step(step)
            except Exception:
                pass
        self.bridge.stop()


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return ""
    if isinstance(value, float):
        return "%.12g" % value
    if isinstance(value, (int, str)):
        return str(value)
    return json.dumps(value)


def self_test() -> bool:
    """For the release build's self-test: the helper starts with Kelvin's own Python and answers."""
    try:
        b = Bridge("self-test", bridge_command())
        try:
            return bool(b.request("ping", 30).get("bits"))
        finally:
            b.stop()
    except Exception:
        return False
