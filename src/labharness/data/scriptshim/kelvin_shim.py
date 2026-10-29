"""Kelvin's stand-in for the lab's instruments while Kelvin runs one of the lab's own scripts (labharness/scriptrun.py).

Kelvin starts the lab's Python with this file first:

    python -u kelvin_shim.py [--source FILE] SCRIPT [ARGS...]

and the script runs as it always does (__name__ == "__main__", its folder first on sys.path, its arguments in
sys.argv; --source: the text to run instead of the file's own, with the values Kelvin was asked to change), with
stand-ins for the instruments in Kelvin's lab:

    pyvisa      ResourceManager.open_resource of a lab instrument's address gives a stand-in resource that sends each
                command to Kelvin, which passes it through its safety gate on its own connection and answers with the
                instrument's reply; any other address opens as usual once Kelvin allows it (it asks a person)
    pyserial    Serial on a lab instrument's port: the same, a line at a time
    socket      a connection to a lab instrument's address (TCPIP::host::port::SOCKET): the same
    a maker's library (zhinst, nidaqmx, ...)   reaches instruments without Kelvin seeing the commands: Kelvin asks a
                person before the script first imports it
    time.sleep  in the main thread, ends at once when the run stops

A command the gate refuses ends the script with KelvinRefused; a person's stop with KeyboardInterrupt (once), so the
script's own `finally:` runs. Its commands go through Kelvin too, and Kelvin brings what the script drove back to safe
after it. Without Kelvin (the connection lost) the script stops as well: it never runs on unwatched.

KELVIN_IPC (host:port) and KELVIN_IPC_TOKEN say where Kelvin listens. A Python the script starts loads this file too,
through sitecustomize.py next to it on PYTHONPATH. Standard library only, Python 3.6 or later.
"""
import base64
import builtins
import importlib.abc
import json
import os
import socket
import sys
import threading
import time
import traceback
import types
import weakref

import _thread

_REAL_SOCKET = socket.socket
_REAL = {name: getattr(socket.socket, name) for name in
         ("connect", "connect_ex", "send", "sendall", "recv", "recv_into", "close", "getpeername")}
_REAL_CREATE_CONNECTION = socket.create_connection
_REAL_SLEEP = time.sleep
_LINK = None


class KelvinRefused(RuntimeError):
    """Kelvin did not send a command of the script: its safety gate refused it, a person declined it, or the run is
    stopping."""


class KelvinInstrumentError(IOError):
    """The instrument did not answer as asked (a timeout, an error it reported)."""


class _Link(object):
    """The connection to Kelvin: requests from any thread, and one reader for Kelvin's answers and its stop."""

    def __init__(self, address, token):
        host, _, port = address.rpartition(":")
        self.sock = _REAL_SOCKET(socket.AF_INET, socket.SOCK_STREAM)
        _REAL["connect"](self.sock, (host, int(port)))
        self.lock = threading.Lock()        # one request written at a time
        self.state = threading.Lock()       # the stop's bookkeeping, between the reader and the main thread
        self.waiting = {}                   # request id -> [answered, answer]
        self.next_id = 0
        self.stopping = threading.Event()   # Kelvin is stopping the run
        self.why = ""
        self.lost = False
        self.interrupted = False            # the main thread has had its one KeyboardInterrupt or KelvinRefused
        self.main_waiting = False           # the main thread is in a request or in time.sleep: it raises itself
        self.asked = {}                     # a maker's library -> Kelvin's answer
        self.opaque = ()
        reader = threading.Thread(target=self._read, name="kelvin-link")
        reader.daemon = True
        reader.start()
        hello = self.request("hello", token=token, pid=os.getpid(), python=sys.version.split()[0])
        self.opaque = tuple(hello.get("opaque") or ())

    # ------------------------------------------------------------------ the reader

    def _read(self):
        buf = b""
        try:
            while True:
                data = _REAL["recv"](self.sock, 65536)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    msg = json.loads(line.decode("utf-8"))
                    if msg.get("op") == "stop":
                        self._stop(msg.get("why") or "Kelvin stopped the run")
                        continue
                    slot = self.waiting.get(msg.get("id"))
                    if slot is not None:
                        slot[1] = msg
                        slot[0].set()
        except (OSError, ValueError):
            pass
        self.lost = True
        self._stop("the connection to Kelvin was lost")
        for slot in list(self.waiting.values()):
            slot[0].set()
        ender = threading.Thread(target=self._end_soon, name="kelvin-gone")
        ender.daemon = True
        ender.start()

    def _end_soon(self):
        """Nothing watches the instruments without Kelvin: the script has a few seconds to end, then it is ended."""
        _REAL_SLEEP(10)
        sys.stderr.write("Kelvin: ending the script, since Kelvin is gone\n")
        sys.stderr.flush()
        os._exit(3)

    def _stop(self, why):
        with self.state:
            if self.stopping.is_set():
                return
            self.why = why
            self.stopping.set()
            interrupt = not self.main_waiting and not self.interrupted   # else the wait it is in raises
            if interrupt:
                self.interrupted = True
        if interrupt:
            _thread.interrupt_main()

    # ------------------------------------------------------------------ requests

    def _enter(self):
        if threading.current_thread() is threading.main_thread():
            with self.state:
                self.main_waiting = True

    def _leave(self, refused=False):
        """Back from a wait: True when the main thread should now raise KeyboardInterrupt for a stop."""
        if threading.current_thread() is not threading.main_thread():
            return False
        with self.state:
            self.main_waiting = False
            if refused:
                self.interrupted = True
                return False
            fire = self.stopping.is_set() and not self.interrupted
            if fire:
                self.interrupted = True
            return fire

    def request(self, op, **fields):
        if self.lost:
            self._leave(refused=True)
            raise KelvinRefused("Kelvin is gone, so the script stops: nothing watches the instruments")
        self._enter()
        slot = [threading.Event(), None]
        with self.lock:
            self.next_id += 1
            rid = self.next_id
            self.waiting[rid] = slot
            fields.update(id=rid, op=op)
            try:
                _REAL["sendall"](self.sock, (json.dumps(fields) + "\n").encode("utf-8"))
            except OSError:
                slot[0].set()
        try:
            slot[0].wait()
        finally:
            self.waiting.pop(rid, None)
        answer = slot[1]
        if answer is None:
            self._leave(refused=True)
            raise KelvinRefused("Kelvin is gone, so the script stops: nothing watches the instruments")
        if not answer.get("ok"):
            stop = bool(answer.get("stop"))
            fire = self._leave(refused=stop)
            if stop:
                raise KelvinRefused(answer.get("error") or "refused by Kelvin")
            if fire:
                raise KeyboardInterrupt
            raise KelvinInstrumentError(answer.get("error") or "the instrument did not answer")
        if self._leave():
            raise KeyboardInterrupt
        return answer

    def command(self, name, text, query=False, timeout=None):
        """One command to a lab instrument, through Kelvin's gate: the instrument's reply, or None."""
        return self.request("command", instrument=name, command=str(text), query=bool(query),
                            timeout=timeout).get("reply")

    def open(self, address, kind):
        """A lab instrument's name for this address, or None to open it as usual (Kelvin allowed it)."""
        return self.request("open", address=address, kind=kind).get("instrument")

    def lookup(self, address):
        """A lab instrument's name for this address, or None; nothing is opened and nobody is asked."""
        return self.request("lookup", address=address).get("instrument")

    def library(self, module):
        """May the script import this maker's library? Kelvin asks a person once."""
        if module not in self.asked:
            try:
                self.request("library", module=module)
                self.asked[module] = None
            except KelvinRefused as e:
                self.asked[module] = str(e)
        if self.asked[module]:
            raise ImportError("Kelvin: %s" % self.asked[module], name=module)

    def sleep(self, seconds):
        if seconds < 0:
            raise ValueError("sleep length must be non-negative")
        if threading.current_thread() is not threading.main_thread() or self.interrupted:
            return _REAL_SLEEP(seconds)
        self._enter()
        try:
            self.stopping.wait(seconds)
        finally:
            fire = self._leave()
        if fire:
            raise KeyboardInterrupt


# ------------------------------------------------------------------ pyvisa

_FORMATS = {"f": float, "e": float, "g": float, "d": int, "i": int, "u": int, "s": str,
            "b": lambda x: int(x, 2), "x": lambda x: int(x, 16), "X": lambda x: int(x, 16), "o": lambda x: int(x, 8)}
_MESSAGE_BASED = []   # pyvisa's MessageBasedResource, once pyvisa is in: isinstance checks (QCoDeS) pass


class _Resource(object):
    """A lab instrument as the script's pyvisa sees it: each command goes to Kelvin, through its safety gate, and
    comes back with the instrument's reply. Settings such as timeouts and line endings are kept here: Kelvin's own
    connection has the lab's."""

    def __init__(self, link, name, address, settings):
        d = self.__dict__
        d.update(_link=link, _name=name, _reply=None, resource_name=address, timeout=2000.0, read_termination=None,
                 write_termination="\r\n", encoding="ascii", chunk_size=20 * 1024, query_delay=0.0, send_end=True,
                 session=0, visalib=None, last_status=0)
        for key, value in settings.items():
            if key not in ("resource_pyclass", "access_mode", "open_timeout"):
                d[key] = value

    @property
    def __class__(self):
        return _MESSAGE_BASED[0] if _MESSAGE_BASED else _Resource

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        raise AttributeError("Kelvin's stand-in for %s has no %r: Kelvin passes the script's commands to the "
                             "instrument, not every pyvisa attribute" % (self.resource_name, name))

    def __repr__(self):
        return "<Kelvin's stand-in for %s (%s)>" % (self._name, self.resource_name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _timeout(self):
        t = self.timeout
        return None if t is None or t == float("inf") else float(t)

    # writing and reading
    def write(self, message, termination=None, encoding=None):
        self.__dict__["_reply"] = self._link.command(self._name, message, timeout=self._timeout())
        end = self.write_termination if termination is None else termination
        return len(message) + len(end or "")

    def write_raw(self, message):
        text = message.decode("latin-1") if isinstance(message, (bytes, bytearray)) else str(message)
        self.__dict__["_reply"] = self._link.command(self._name, text.rstrip("\r\n"), timeout=self._timeout())
        return len(message)

    def query(self, message, delay=None):
        self.__dict__["_reply"] = None
        reply = self._link.command(self._name, message, query=True, timeout=self._timeout())
        return "" if reply is None else reply

    def _take(self):
        reply = self._reply
        self.__dict__["_reply"] = None
        return reply

    def read(self, termination=None, encoding=None):
        reply = self._take()
        if reply is not None:
            return reply
        return self._link.request("read", instrument=self._name, timeout=self._timeout()).get("reply") or ""

    def read_raw(self, size=None):
        reply = self._take()
        if reply is not None:
            return (reply + (self.read_termination or "\n")).encode("latin-1")
        raw = self._link.request("read_raw", instrument=self._name, timeout=self._timeout()).get("raw") or ""
        return base64.b64decode(raw)

    def read_bytes(self, count, chunk_size=None, break_on_termchar=False):
        reply = self._take()
        if reply is not None:
            data = (reply + (self.read_termination or "\n")).encode("latin-1")
            if len(data) > count:
                self.__dict__["_reply"] = data[count:].decode("latin-1")
            return data[:count]
        raw = self._link.request("read_bytes", instrument=self._name, count=int(count),
                                 break_on_termchar=bool(break_on_termchar), timeout=self._timeout()).get("raw") or ""
        return base64.b64decode(raw)

    def query_ascii_values(self, message, converter="f", separator=",", container=list, delay=None):
        text = self.query(message)
        convert = converter if callable(converter) else _FORMATS.get(str(converter).lstrip("%")[-1:], float)
        pieces = text.split(separator) if not callable(separator) else separator(text)
        values = [convert(p.strip()) for p in pieces if p.strip()]
        return values if container is list else container(values)

    def query_binary_values(self, message, datatype="f", is_big_endian=False, container=list, delay=None,
                            header_fmt="ieee", expect_termination=True, data_points=0, chunk_size=None):
        values = self._link.request("query_binary", instrument=self._name, command=str(message),
                                    datatype=str(datatype), is_big_endian=bool(is_big_endian),
                                    header_fmt=str(header_fmt), expect_termination=bool(expect_termination),
                                    data_points=int(data_points or 0), timeout=self._timeout()).get("values") or []
        return values if container is list else container(values)

    def write_ascii_values(self, message, values, converter="f", separator=",", termination=None, encoding=None):
        if callable(converter):
            block = separator.join(converter(v) for v in values)
        else:
            fmt = converter if str(converter).startswith("%") else "%" + str(converter)
            block = separator.join(fmt % (v,) for v in values)
        return self.write(message + block, termination, encoding)

    def write_binary_values(self, message, values, datatype="f", is_big_endian=False, termination=None,
                            encoding=None, header_fmt="ieee"):
        values = [float(v) if isinstance(v, float) else int(v) for v in values]
        self._link.request("write_binary", instrument=self._name, command=str(message), values=values,
                           datatype=str(datatype), is_big_endian=bool(is_big_endian), header_fmt=str(header_fmt),
                           timeout=self._timeout())
        return len(values)

    # the rest of a resource's day
    def clear(self):
        self.__dict__["_reply"] = None
        self._link.request("clear", instrument=self._name)

    def assert_trigger(self):
        self.write("*TRG")

    def read_stb(self):
        return int(self._link.request("stb", instrument=self._name).get("value") or 0)

    @property
    def stb(self):
        return self.read_stb()

    def flush(self, mask=None):
        pass

    def lock(self, timeout="default", requested_key=None):
        return ""

    def lock_excl(self, timeout="default"):
        pass

    def unlock(self):
        pass

    def open(self, access_mode=None, open_timeout=None):
        pass

    def close(self):
        self.__dict__["_reply"] = None   # Kelvin keeps its own connection to the instrument

    def wait_for_srq(self, timeout=25000):
        raise KelvinInstrumentError("Kelvin's stand-in for %s can't wait for a service request; poll with *OPC? "
                                    "instead" % self.resource_name)


def _resource_info(address):
    """What pyvisa's resource_info says of an address, worked out from the address itself: a lab instrument's
    address is Kelvin's to answer for, and the VISA library may not know it (sim://) or be missing."""
    import re
    from pyvisa import constants, highlevel
    a = address.strip().upper()
    kinds = (("GPIB", "gpib"), ("ASRL", "asrl"), ("COM", "asrl"), ("TCPIP", "tcpip"), ("USB", "usb"),
             ("PXI", "pxi"), ("VXI", "vxi"))
    kind = next((getattr(constants.InterfaceType, v, None) for k, v in kinds if a.startswith(k)), None)
    board = re.match(r"^[A-Z]+(\d*)", a)
    return highlevel.ResourceInfo(kind if kind is not None else constants.InterfaceType.unknown,
                                  int(board.group(1)) if board and board.group(1) else 0,
                                  "SOCKET" if a.endswith("::SOCKET") else "INSTR", address, None)


class _Manager(object):
    """pyvisa's ResourceManager where no VISA library can be loaded (no NI-VISA on this computer, say): the lab's
    instruments are Kelvin's anyway, so they open; any other address fails as it would have."""

    def __init__(self, why):
        self.why, self.visalib, self.session = why, None, 0

    def open_resource(self, resource_name, *args, **kwargs):
        name = _LINK.open(str(resource_name), "visa")
        if name is None:
            raise KelvinInstrumentError("%s can't be opened here: %s" % (resource_name, self.why))
        return _Resource(_LINK, name, str(resource_name), kwargs)

    def resource_info(self, resource_name, extended=True):
        if _LINK.lookup(str(resource_name)) is None:
            raise KelvinInstrumentError("%s can't be looked up here: %s" % (resource_name, self.why))
        return _resource_info(str(resource_name))

    def list_resources(self, query="?*::INSTR"):
        return ()

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def _patch_pyvisa(module):
    rm_class = getattr(module, "ResourceManager", None)
    if rm_class is None or getattr(rm_class.open_resource, "kelvin", False):
        return
    resources = getattr(module, "resources", None)
    if resources is not None and getattr(resources, "MessageBasedResource", None) is not None:
        _MESSAGE_BASED[:] = [resources.MessageBasedResource]
    real_open = rm_class.open_resource
    real_new = rm_class.__new__
    real_info = getattr(rm_class, "resource_info", None)

    def open_resource(self, resource_name, *args, **kwargs):
        name = _LINK.open(str(resource_name), "visa")
        if name is None:
            return real_open(self, resource_name, *args, **kwargs)
        return _Resource(_LINK, name, str(resource_name), kwargs)

    def resource_info(self, resource_name, extended=True):   # PyMeasure asks it before it opens
        if real_info is not None and _LINK.lookup(str(resource_name)) is None:
            return real_info(self, resource_name, extended)
        return _resource_info(str(resource_name))

    def __new__(cls, *args, **kwargs):
        try:
            return real_new(cls) if real_new is object.__new__ else real_new(cls, *args, **kwargs)
        except Exception as e:
            return _Manager("%s: %s" % (type(e).__name__, e))

    open_resource.kelvin = True
    rm_class.open_resource = open_resource
    rm_class.resource_info = resource_info
    rm_class.__new__ = staticmethod(__new__)


# ------------------------------------------------------------------ a line at a time (pyserial, sockets)

class _Lines(object):
    """Commands written as bytes, a line at a time, each through Kelvin; the replies are read back as bytes."""

    def __init__(self, link, name, peer=None):
        self.link, self.name, self.peer = link, name, peer
        self.out, self.into, self.eol = b"", b"", b"\n"

    def write(self, data):
        self.out += bytes(data)
        while True:
            cuts = [i for i in (self.out.find(b"\n"), self.out.find(b"\r")) if i >= 0]
            if not cuts:
                return
            cut = min(cuts)
            end = cut
            while end < len(self.out) and self.out[end:end + 1] in (b"\r", b"\n"):
                end += 1
            line, self.eol, self.out = self.out[:cut], self.out[cut:end], self.out[end:]
            text = line.decode("latin-1").strip()
            if text:
                reply = self.link.command(self.name, text)
                if reply is not None:
                    self.into += reply.encode("latin-1") + self.eol

    def read(self, size):
        size = len(self.into) if size is None or size < 0 else size
        data, self.into = self.into[:size], self.into[size:]
        return data

    def read_until(self, expected=b"\n", size=None):
        at = self.into.find(expected)
        n = len(self.into) if at < 0 else at + len(expected)
        if size is not None and size >= 0:
            n = min(n, size)
        return self.read(n)


def _patch_serial(module):
    real = getattr(module, "Serial", None)
    if real is None or getattr(real, "kelvin", False):
        return

    class Serial(real):
        """pyserial's Serial; on a lab instrument's port, each line goes to Kelvin instead."""
        kelvin = True
        _kelvin_lines = None

        def open(self):
            name = _LINK.open(str(self.port), "serial") if self.port is not None else None
            if name is None:
                return real.open(self)
            self._kelvin_lines = _Lines(_LINK, name)
            self.is_open = True

        def close(self):
            if self._kelvin_lines is not None:
                self.is_open = False
                return
            return real.close(self)

        def write(self, data):
            if self._kelvin_lines is None:
                return real.write(self, data)
            self._kelvin_lines.write(data)
            return len(data)

        def read(self, size=1):
            if self._kelvin_lines is None:
                return real.read(self, size)
            return self._kelvin_lines.read(size)

        def read_until(self, expected=b"\n", size=None):
            if self._kelvin_lines is None:
                return real.read_until(self, expected, size)
            return self._kelvin_lines.read_until(expected, size)

        def readline(self, size=-1):
            if self._kelvin_lines is None:
                return real.readline(self, size)
            return self._kelvin_lines.read_until(b"\n", None if size is None or size < 0 else size)

        def readlines(self, hint=-1):
            if self._kelvin_lines is None:
                return real.readlines(self, hint)
            lines = []
            while self._kelvin_lines.into:
                lines.append(self.readline())
            return lines

        @property
        def in_waiting(self):
            if self._kelvin_lines is None:
                return real.in_waiting.__get__(self)
            return len(self._kelvin_lines.into)

        def reset_input_buffer(self):
            if self._kelvin_lines is None:
                return real.reset_input_buffer(self)
            self._kelvin_lines.into = b""

        def reset_output_buffer(self):
            if self._kelvin_lines is None:
                return real.reset_output_buffer(self)

        def flush(self):
            if self._kelvin_lines is None:
                return real.flush(self)

    Serial.__name__ = Serial.__qualname__ = "Serial"
    module.Serial = Serial


_SOCKETS = weakref.WeakKeyDictionary()   # a socket to a lab instrument's address -> its _Lines


def _socket_address(address):
    if isinstance(address, tuple) and len(address) >= 2 and isinstance(address[0], str):
        return "TCPIP0::%s::%s::SOCKET" % (address[0], address[1])
    return None


def _patch_socket():
    if getattr(socket.socket.connect, "kelvin", False):
        return

    def lines(sock):
        return _SOCKETS.get(sock)

    def connect(self, address):
        where = _socket_address(address)
        name = _LINK.open(where, "socket") if where and _LINK is not None else None
        if name is None:
            return _REAL["connect"](self, address)
        _SOCKETS[self] = _Lines(_LINK, name, address)

    def connect_ex(self, address):
        try:
            connect(self, address)
        except OSError as e:
            return e.errno or 1
        return 0

    def send(self, data, flags=0):
        found = lines(self)
        if found is None:
            return _REAL["send"](self, data, flags)
        found.write(data)
        return len(data)

    def sendall(self, data, flags=0):
        found = lines(self)
        if found is None:
            return _REAL["sendall"](self, data, flags)
        found.write(data)

    def recv(self, bufsize, flags=0):
        found = lines(self)
        if found is None:
            return _REAL["recv"](self, bufsize, flags)
        if not found.into:
            raise socket.timeout("timed out: the instrument sent nothing (Kelvin's stand-in answers queries only)")
        return found.read(bufsize)

    def recv_into(self, buffer, nbytes=0, flags=0):
        found = lines(self)
        if found is None:
            return _REAL["recv_into"](self, buffer, nbytes, flags)
        data = recv(self, nbytes or len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def close(self):
        _SOCKETS.pop(self, None)
        return _REAL["close"](self)

    def getpeername(self):
        found = lines(self)
        if found is not None:
            return found.peer
        return _REAL["getpeername"](self)

    def create_connection(address, *args, **kwargs):
        where = _socket_address(address)
        name = _LINK.open(where, "socket") if where and _LINK is not None else None
        if name is None:
            return _REAL_CREATE_CONNECTION(address, *args, **kwargs)
        sock = _REAL_SOCKET(socket.AF_INET, socket.SOCK_STREAM)
        _SOCKETS[sock] = _Lines(_LINK, name, address)
        return sock

    for fn in (connect, connect_ex, send, sendall, recv, recv_into, close, getpeername, create_connection):
        fn.kelvin = True
    for name, fn in (("connect", connect), ("connect_ex", connect_ex), ("send", send), ("sendall", sendall),
                     ("recv", recv), ("recv_into", recv_into), ("close", close), ("getpeername", getpeername)):
        setattr(socket.socket, name, fn)
    socket.create_connection = create_connection


# ------------------------------------------------------------------ makers' libraries, and installing it all

class _Libraries(importlib.abc.MetaPathFinder):
    """Asks Kelvin before the script first imports a maker's library that reaches instruments past the gate (one that
    is installed: a missing one fails as it always would)."""

    def find_spec(self, fullname, path=None, target=None):
        if _LINK is None or fullname not in _LINK.opaque:
            return None
        for finder in sys.meta_path:
            find = getattr(finder, "find_spec", None) if finder is not self else None
            if find is not None and find(fullname, path, target) is not None:
                _LINK.library(fullname)
                break
        return None


def install():
    """Connect to Kelvin and put the stand-ins in place (once per process)."""
    global _LINK
    if _LINK is not None:
        return _LINK
    address = os.environ.get("KELVIN_IPC")
    if not address:
        raise RuntimeError("KELVIN_IPC is not set: Kelvin starts this, to run one of the lab's scripts")
    _LINK = _Link(address, os.environ.get("KELVIN_IPC_TOKEN", ""))
    for name, patch in (("pyvisa", _patch_pyvisa), ("serial", _patch_serial)):
        try:
            module = __import__(name)
        except ImportError:
            continue   # the script can't import it either
        try:
            patch(module)
        except Exception as e:   # never let a script reach the instruments unwatched
            raise RuntimeError("Kelvin could not put its stand-in into %s (%s: %s)" % (name, type(e).__name__, e))
    _patch_socket()
    sys.meta_path.insert(0, _Libraries())
    time.sleep = _LINK.sleep
    return _LINK


def _save_figures(run_dir):
    plt = sys.modules.get("matplotlib.pyplot")
    saved = []
    if plt is None or not run_dir:
        return saved
    for n in plt.get_fignums():
        path = os.path.join(run_dir, "figure-%d.png" % n)
        try:
            plt.figure(n).savefig(path, dpi=110)
            saved.append(os.path.basename(path))
        except Exception:
            pass
    return saved


def _probe(names):
    """Kelvin choosing a Python for a script: this one's version, and which of the script's modules it lacks (looked
    for, not imported), with the script's own folder (the working folder) on the path."""
    import importlib.util
    sys.path.insert(0, os.getcwd())
    missing = []
    for name in names:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    sys.stdout.write(json.dumps({"version": list(sys.version_info[:2]), "missing": missing,
                                 "executable": sys.executable}) + "\n")
    return 0


def main(argv):
    if argv[:1] == ["--probe"]:
        return _probe(argv[1:])
    source = None
    if argv[:1] == ["--source"]:
        source, argv = argv[1], argv[2:]
    if not argv:
        sys.stderr.write("usage: kelvin_shim.py [--source FILE] SCRIPT [ARGS...]\n")
        return 2
    script = os.path.abspath(argv[0])
    link = install()
    if source:   # Kelvin's copy, with the values it was asked to change: UTF-8
        with open(source, encoding="utf-8-sig") as f:
            text = f.read()
    else:        # the script itself, read as Python reads it (its coding line, else UTF-8)
        import tokenize
        with tokenize.open(script) as f:
            text = f.read()
    sys.argv = [script] + list(argv[1:])
    sys.path[0] = os.path.dirname(script)
    module = types.ModuleType("__main__")
    module.__dict__.update(__file__=script, __builtins__=builtins, __cached__=None)
    sys.modules["__main__"] = module
    status, error = 0, ""
    try:
        exec(compile(text, script, "exec"), module.__dict__)
    except SystemExit as e:
        status = e.code
    except BaseException as e:
        traceback.print_exc()
        status, error = 1, "".join(traceback.format_exception_only(type(e), e)).strip()
    finally:
        sys.stdout.flush()
        figures = _save_figures(os.environ.get("KELVIN_RUN_DIR"))
        try:
            link.request("bye", status=status if isinstance(status, int) or status is None else 1, error=error,
                         figures=figures)
        except BaseException:
            pass
    return status


if __name__ == "__main__":
    # one stand-in per process: the one sitecustomize.py loaded as kelvin_shim, if it did (this file run as __main__
    # is a second copy of the module); else this one is it (Kelvin's own Python, which has no sitecustomize)
    _shim = sys.modules.get("kelvin_shim")
    if _shim is None:
        _shim = sys.modules["kelvin_shim"] = sys.modules[__name__]
    sys.exit(_shim.main(sys.argv[1:]))
