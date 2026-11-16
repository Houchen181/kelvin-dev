"""Kelvin's stand-in for the lab's instruments while Kelvin runs one of the lab's own scripts (labharness/scriptrun.py).

Kelvin starts the lab's Python with this file first:

    python -u kelvin_shim.py [--source FILE] [--module NAME] SCRIPT [ARGS...]

and the script runs as it always does (__name__ == "__main__", its folder first on sys.path, its arguments in
sys.argv; --source: the text to run instead of the file's own, with the values Kelvin was asked to change; --module:
SCRIPT is that module of a package, run as python -m runs it: its package imported first, the folder above the top
package first on sys.path, so its relative imports work), then an experiment's action, the one KELVIN_ACTION names
(kelvin_api.action), with stand-ins for the instruments in Kelvin's lab:

    pyvisa      ResourceManager.open_resource of a lab instrument's address gives a stand-in resource that sends each
                command to Kelvin, which passes it through its safety gate on its own connection and answers with the
                instrument's reply; any other address opens as usual once Kelvin allows it (it asks a person)
    pyserial    Serial on a lab instrument's port: the same, a line at a time
    socket      a connection to a lab instrument's address (TCPIP::host::port::SOCKET): the same
    a maker's library (zhinst, nidaqmx, ...)   reaches instruments without Kelvin seeing the commands: Kelvin hears
                of it before the script first imports it, and lets it through with the run
    time.sleep  in the main thread, ends at once when the run stops
    kelvin      Kelvin's experiment API (kelvin_api.py, next to this file): the lab's instruments by name,
                through the safety gate (from kelvin import lab; lab.set("smu.V", 0.5)). It is in place before the
                script's own code runs, so a kelvin.py beside the script never takes its place

A command the gate refuses ends the script with KelvinRefused; a person's stop with KeyboardInterrupt (once), so the
script's own `finally:` runs. Its commands go through Kelvin too, and Kelvin brings what the script drove back to safe
after it. Without Kelvin (the connection lost) the script stops as well: it never runs on unwatched.

What goes around the stand-ins is watched in the script's own process (audit hooks, PEP 578). In a run a person
approved (_guard): a socket's own connection or datagram (_socket, sendto) and a device file opened as a file are put
to Kelvin first, and a lab instrument's is refused, as pyvisa's open_bare_resource and visalib.open of one are. A
rehearsal, a kept experiment's real run and a run auto mode approved are fenced (_fence: no programs, native code, COM,
registry changes, USB, network or device files; writing only in the run's folder, and for auto mode its kelvin_
folder); what the fence refuses in a run auto mode approved ends the run (KELVIN_FENCE=auto).

KELVIN_IPC (host:port) and KELVIN_IPC_TOKEN say where Kelvin listens. A Python the script starts loads this file too,
through sitecustomize.py next to it on PYTHONPATH. Standard library only, Python 3.6 or later.
"""
import base64
import builtins
import hashlib
import importlib.abc
import ipaddress
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
_FENCED = False   # a fenced run (a rehearsal, a kept experiment, a run auto mode approved): no VISA library is loaded
CONNECT_S = 30   # how long the stand-in waits to reach Kelvin as the script starts


class KelvinRefused(RuntimeError):
    """Kelvin did not send a command of the script: its safety gate refused it, a person declined it, or the run is
    stopping."""


class KelvinInstrumentError(IOError):
    """The instrument did not answer as asked (a timeout, an error it reported)."""


class _Link(object):
    """The connection to Kelvin: requests from any thread, and one reader for Kelvin's answers and its stop."""

    def __init__(self, address, token):
        self.address, self.token = address, token
        self.asked = {}                     # a maker's library -> Kelvin's answer
        self.opaque = ()
        self.sock = self._connect()
        self._start()

    def _connect(self):
        host, _, port = self.address.rpartition(":")
        sock = _REAL_SOCKET(socket.AF_INET, socket.SOCK_STREAM)
        # a Kelvin that doesn't answer (endpoint security that drops this computer's own connections) is said, not
        # waited for: the script hung before it ran anything, with nothing to say why
        sock.settimeout(CONNECT_S)
        try:
            _REAL["connect"](sock, (host, int(port)))
        except Exception as e:
            raise RuntimeError("Kelvin's stand-in could not reach Kelvin at %s within %d s (%s): a firewall or "
                               "security program may be blocking this computer's own connections" % (
                                   self.address, CONNECT_S, e))
        sock.settimeout(None)
        return sock

    def _start(self):
        self.lock = threading.Lock()        # one request written at a time
        self.state = threading.Lock()       # the stop's bookkeeping, between the reader and the main thread
        self.waiting = {}                   # request id -> [answered, answer]
        self.next_id = 0
        self.stopping = threading.Event()   # Kelvin is stopping the run
        self.why = ""
        self.lost = False
        self.interrupted = False            # the main thread has had its one KeyboardInterrupt or KelvinRefused
        self.main_waiting = False           # the main thread is in a request or in time.sleep: it raises itself
        reader = threading.Thread(target=self._read, name="kelvin-link")
        reader.daemon = True
        reader.start()
        hello = self.request("hello", token=self.token, pid=os.getpid(), python=sys.version.split()[0])
        self.opaque = tuple(hello.get("opaque") or ())

    def reborn(self):
        """A forked child of the script (multiprocessing's fork, os.fork): its own connection to Kelvin, never its
        parent's, whose answers only the parent's reader takes (a review of 2026-11-15: a forked Pool worker that sent
        a datagram waited for ever); the locks a thread of the parent held at the fork are made anew. When Kelvin
        can't be reached from it, each request fails at once, saying why: it never waits."""
        old = getattr(self, "sock", None)
        try:
            self.sock = self._connect()
            self._start()
        except BaseException as e:
            self.lost = True
            self.gone = ("a forked process of the script couldn't reach Kelvin (%s), so nothing it asks of the lab "
                         "goes ahead" % e)
            self.waiting, self.lock, self.state = {}, threading.Lock(), threading.Lock()
        if old is not None:
            try:
                _REAL["close"](old)   # the child's copy only: the parent's connection goes on
            except Exception:
                pass

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
            raise KelvinRefused(getattr(self, "gone", "") or
                                "Kelvin is gone, so the script stops: nothing watches the instruments")
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

    def past(self, address, how):
        """A way to an address past the stand-ins (how: socket.connect, socket.sendto, socket.sendmsg, device, visa):
        Kelvin says, KelvinRefused when it may not (a lab instrument's, which also ends the run)."""
        return self.request("past", address=address, how=how)

    def library(self, module):
        """May the script import this maker's library? Kelvin says once: yes with the run, unless it stops."""
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

    def __init__(self, why, link):
        self.why, self.visalib, self.session, self._link = why, None, 0, link

    def open_resource(self, resource_name, *args, **kwargs):
        name = self._link.open(str(resource_name), "visa")
        if name is None:
            raise KelvinInstrumentError("%s can't be opened here: %s" % (resource_name, self.why))
        return _Resource(self._link, name, str(resource_name), kwargs)

    def resource_info(self, resource_name, extended=True):
        if self._link.lookup(str(resource_name)) is None:
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
    real_bare = getattr(rm_class, "open_bare_resource", None)
    link, fenced = _LINK, _FENCED   # bound now: a script that rebinds kelvin_shim's names changes nothing here

    def open_resource(self, resource_name, *args, **kwargs):
        name = link.open(str(resource_name), "visa")
        if name is None:
            return real_open(self, resource_name, *args, **kwargs)
        return _Resource(link, name, str(resource_name), kwargs)

    def open_bare_resource(self, resource_name, *args, **kwargs):
        """A bare VISA session (the session number only): a lab instrument's would be one past the stand-in, so Kelvin
        refuses it (and ends the run); any other address as open_resource's (a person allowed it)."""
        link.past(str(resource_name), "visa")
        return real_bare(self, resource_name, *args, **kwargs)

    def resource_info(self, resource_name, extended=True):   # PyMeasure asks it before it opens
        if real_info is not None and link.lookup(str(resource_name)) is None:
            return real_info(self, resource_name, extended)
        return _resource_info(str(resource_name))

    def __new__(cls, *args, **kwargs):
        if fenced:   # its VISA library (a DLL, through ctypes) is past the fence: the lab's instruments open all the same
            return _Manager("a fenced run reaches instruments only through Kelvin's stand-in, never the VISA library",
                            link)
        try:
            rm = real_new(cls) if real_new is object.__new__ else real_new(cls, *args, **kwargs)
        except Exception as e:
            return _Manager("%s: %s" % (type(e).__name__, e), link)
        _guard_visalib(rm, link)
        return rm

    open_resource.kelvin = True
    rm_class.open_resource = open_resource
    if getattr(rm_class, "get_instrument", None) is not None:   # an older pyvisa's alias, bound to the original
        rm_class.get_instrument = open_resource
    if real_bare is not None:
        open_bare_resource.kelvin = True
        rm_class.open_bare_resource = open_bare_resource
    rm_class.resource_info = resource_info
    rm_class.__new__ = staticmethod(__new__)


def _guard_visalib(rm, link):
    """The VISA library's own open (rm.visalib.open), below open_resource: a session it opens at a lab instrument's
    address goes past the stand-in (a review of 2026-11-15: rm.visalib.write on one), so Kelvin is asked first, as for
    open_bare_resource; the library is shared by every manager, so it is put in place once."""
    lib = getattr(rm, "visalib", None)
    real = getattr(lib, "open", None)
    if real is None or getattr(real, "kelvin", False):
        return

    def open(session, resource_name, *args, **kwargs):
        link.past(str(resource_name), "visa")
        return real(session, resource_name, *args, **kwargs)
    open.kelvin = True
    try:
        lib.open = open
    except Exception:   # a library that takes no attributes: its open_bare_resource still asks
        pass


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

    link = _LINK   # bound now: a script that rebinds kelvin_shim's names changes nothing here

    class Serial(real):
        """pyserial's Serial; on a lab instrument's port, each line goes to Kelvin instead."""
        kelvin = True
        _kelvin_lines = None

        def open(self):
            name = link.open(str(self.port), "serial") if self.port is not None else None
            if name is None:
                return real.open(self)
            self._kelvin_lines = _Lines(link, name)
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
    """A socket's address as Kelvin names one (TCPIP0::host::port::SOCKET), None for one that isn't a host and a port:
    an IPv4 address an IPv6 socket writes as ::ffff:127.0.0.1 is that address (a review of 2026-11-15: a dual-stack
    socket reached the lab's lock-in so), any other IPv6 address in brackets, which Kelvin never takes for one that
    reaches no lab instrument."""
    if isinstance(address, tuple) and len(address) >= 2 and isinstance(address[0], str):
        host = address[0]
        if ":" in host:
            try:
                ip = ipaddress.ip_address(host.split("%")[0])
                mapped = getattr(ip, "ipv4_mapped", None)
                host = str(mapped) if mapped is not None else "[%s]" % ip.compressed
            except ValueError:
                host = "[%s]" % host
        return "TCPIP0::%s::%s::SOCKET" % (host, address[1])
    return None


def _patch_socket(link):
    if getattr(socket.socket.connect, "kelvin", False):
        return

    def lines(sock):
        return _SOCKETS.get(sock)

    def connect(self, address):
        where = _socket_address(address)
        name = link.open(where, "socket") if where else None
        if name is None:
            return _REAL["connect"](self, address)
        _SOCKETS[self] = _Lines(link, name, address)

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
        name = link.open(where, "socket") if where else None
        if name is None:
            return _REAL_CREATE_CONNECTION(address, *args, **kwargs)
        sock = _REAL_SOCKET(socket.AF_INET, socket.SOCK_STREAM)
        _SOCKETS[sock] = _Lines(link, name, address)
        return sock

    def refuse_loop_connect(real):
        """asyncio's own connect (on Windows the proactor's ConnectEx, below the socket's connect): refused for a lab
        instrument, whose commands go through Kelvin's gate only by pyvisa or a plain socket; once they reached the
        instrument's own port, past the gate."""
        def loop_connect(self, conn, address, *args, **kwargs):
            where = _socket_address(address)
            name = link.open(where, "socket") if where else None
            if name is not None:
                raise ConnectionRefusedError(
                    "Kelvin: %s is %s, an instrument of the lab: open it with pyvisa or a plain socket, whose commands "
                    "go through the safety gate; asyncio's own connection would go past it" % (where, name))
            return real(self, conn, address, *args, **kwargs)
        loop_connect.kelvin = True
        return loop_connect
    try:
        import asyncio.windows_events as _windows_events
        _windows_events.IocpProactor.connect = refuse_loop_connect(_windows_events.IocpProactor.connect)
    except (ImportError, AttributeError):
        pass
    for fn in (connect, connect_ex, send, sendall, recv, recv_into, close, getpeername, create_connection):
        fn.kelvin = True
    for name, fn in (("connect", connect), ("connect_ex", connect_ex), ("send", send), ("sendall", sendall),
                     ("recv", recv), ("recv_into", recv_into), ("close", close), ("getpeername", getpeername)):
        setattr(socket.socket, name, fn)
    socket.create_connection = create_connection


# ------------------------------------------------------------------ makers' libraries, and installing it all

class _Libraries(importlib.abc.MetaPathFinder):
    """Tells Kelvin before the script first imports a maker's library that reaches instruments past the gate (one that
    is installed: a missing one fails as it always would)."""

    def __init__(self, link):
        self._link = link

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self._link.opaque:
            return None
        for finder in sys.meta_path:
            find = getattr(finder, "find_spec", None) if finder is not self else None
            if find is not None and find(fullname, path, target) is not None:
                self._link.library(fullname)
                break
        return None


_PATCHES = {"pyvisa": _patch_pyvisa, "serial": _patch_serial}


class _Patching(object):
    """The loader of a pyvisa or pyserial imported after the stand-in loaded: it loads as it would, then gets the
    stand-in put into it. Anything else is asked of the real loader."""

    def __init__(self, loader, patch):
        self._loader, self._patch = loader, patch

    def create_module(self, spec):
        create = getattr(self._loader, "create_module", None)
        return create(spec) if create is not None else None

    def exec_module(self, module):
        self._loader.exec_module(module)
        try:
            self._patch(module)
        except Exception as e:   # never let a script reach the instruments unwatched
            raise ImportError("Kelvin could not put its stand-in into %s (%s: %s)" % (module.__name__,
                                                                                    type(e).__name__, e))

    def __getattr__(self, name):
        return getattr(self._loader, name)


class _Late(importlib.abc.MetaPathFinder):
    """pyvisa or pyserial that the script's Python could not import when the stand-in loaded, and that the script
    imports later (a copy kept beside the lab's scripts): patched as it loads, so its instruments reach Kelvin all the
    same. One that can't be patched doesn't load."""

    def find_spec(self, fullname, path=None, target=None):
        patch = _PATCHES.get(fullname)
        if patch is None or fullname in sys.modules:
            return None
        for finder in sys.meta_path:
            find = getattr(finder, "find_spec", None) if not isinstance(finder, (_Late, _Libraries)) else None
            spec = find(fullname, path, target) if find is not None else None
            if spec is None:
                continue
            if spec.loader is None or not hasattr(spec.loader, "exec_module"):
                raise ImportError("Kelvin can't put its stand-in into the %s here, so the script can't use it"
                                  % fullname, name=fullname)
            spec.loader = _Patching(spec.loader, patch)
            return spec
        return None


def install():
    """Connect to Kelvin and put the stand-ins in place (once per process); then what watches the ways around them. A
    run a person approved: the guard (_guard). A run auto mode approved: the guard first, the finder of its imports,
    what it imports from its Python's own packages loaded (_preload), Windows' low integrity, then the fence (_fence).
    A kept experiment's real run and a rehearsal: the fence. A person's Stop at any moment of this ends the run."""
    global _LINK, _FENCED
    if _LINK is not None:
        return _LINK
    address = os.environ.get("KELVIN_IPC")
    if not address:
        raise RuntimeError("KELVIN_IPC is not set: Kelvin starts this, to run one of the lab's scripts")
    fence = os.environ.get("KELVIN_FENCE")
    rehearsal = bool(os.environ.get("KELVIN_REHEARSAL"))
    run_dir = os.environ.get("KELVIN_RUN_DIR") or os.getcwd()
    _FENCED = bool(fence or rehearsal)
    try:   # the run's record as Kelvin made it, before anything the script loads writes there
        record = os.listdir(run_dir)
    except OSError:
        record = []
    sandbox = _sandbox_module(os.environ.get("KELVIN_SANDBOX", "")) if _FENCED else None
    _LINK = link = _Link(address, os.environ.get("KELVIN_IPC_TOKEN", ""))
    if hasattr(os, "register_at_fork"):   # a forked child's own connection, never its parent's (_Link.reborn)
        os.register_at_fork(after_in_child=link.reborn)
    for name, patch in _PATCHES.items():
        try:
            module = __import__(name)
        except ImportError:
            continue   # the script can't import it from here; one it finds later is patched as it loads (_Late)
        try:
            patch(module)
        except Exception as e:   # never let a script reach the instruments unwatched
            raise RuntimeError("Kelvin could not put its stand-in into %s (%s: %s)" % (name, type(e).__name__, e))
    _patch_socket(link)
    _no_input()
    sys.meta_path.insert(0, _Libraries(link))
    sys.meta_path.insert(0, _Late())
    time.sleep = link.sleep
    pairing = _Pairing()
    if rehearsal:
        sys.meta_path.insert(0, _Finder(_refused_imports(sandbox, link), "a rehearsal reaches only the simulated lab"))
        _no_overlapped(pairing)
        _rehearsing(link, sandbox, pairing, record)
    elif fence == "auto":   # a run auto mode approved, which nobody looked at: the lab through Kelvin only
        guard = _guard(link, pairing, address)
        here = os.path.dirname(os.path.abspath(__file__))
        dirs = _python_dirs() + [here]
        for name in ("pyvisa", "serial"):   # the stand-ins' own modules, wherever their Python has them
            module = sys.modules.get(name)
            dirs += list(getattr(module, "__path__", None) or [])
        own = {}
        for path, sha in (json.loads(os.environ.get("KELVIN_OWN_MODULES") or "{}") or {}).items():
            own[os.path.normcase(os.path.realpath(path))] = sha
        sys.meta_path.insert(0, _Finder(_refused_imports(sandbox, link), AUTO_WORDS, own=own, dirs=dirs))
        _no_overlapped(pairing)
        _preload(os.environ.get("KELVIN_PRELOAD", ""), dirs)
        if os.environ.get("KELVIN_LOW_INTEGRITY"):   # Windows itself refuses its writes past its two folders
            why = sandbox._low_integrity()
            if why:
                _RESTRICTED.append(why)
        _fence(run_dir, sandbox, link, pairing, words=AUTO_WORDS, auto=True, record=record, ipc=address,
               writable=[w for w in os.environ.get("KELVIN_FENCE_WRITES", "").split(os.pathsep) if w],
               ours=[guard] if guard is not None else [])
    elif fence:   # one of the lab's kept experiments, in its real run: kelvin.lab only
        sys.meta_path.insert(0, _Finder(_refused_imports(sandbox, link), KEPT_WORDS))
        _no_overlapped(pairing)
        _fence(run_dir, sandbox, link, pairing, record=record, ipc=address)
    else:         # a run a person approved: a lab instrument only through the stand-ins (a raw socket, a device file)
        _guard(link, pairing, address)
    _install_api()
    return link


def _refused_imports(sandbox, link):
    """What a fenced run never imports: run_python's (DENIED_IMPORTS: instruments' packages, COM, the network's
    proactor, Kelvin's own insides) but the stand-ins' pyvisa and pyserial, COM, USB and FTDI by other names, and the
    makers' libraries Kelvin knows (opaque)."""
    return tuple(d for d in sandbox.DENIED_IMPORTS if d not in _FENCE_IMPORTS_OK) + _FENCED_IMPORTS + \
        tuple(link.opaque)


class NoInput(EOFError):
    """input() in a script Kelvin runs: nobody is there to type an answer."""


def _no_input():
    """input() says why it gets no answer, rather than end the script with a bare EOFError ("EOF when reading a
    line"): nobody sits at a keyboard for a script Kelvin runs. A script that catches EOFError still does."""
    import builtins

    def no_input(prompt=""):
        if prompt:
            sys.stdout.write(str(prompt))
            sys.stdout.flush()
        raise NoInput("input(): nobody can type an answer to a script Kelvin runs. Give the value as one of the "
                      "script's settings instead (a constant near its top, such as VMAX = 5, which a run's params "
                      "change), as a command-line argument (args), or in a file the script reads")
    builtins.input = no_input


def _rehearsing(link, sandbox, pairing, record):
    """A rehearsal on the simulated lab (KELVIN_REHEARSAL): a wait takes no time. Kelvin's simulated clock moves on by
    it, and so do the script's clocks (time.time, monotonic, perf_counter), so a loop timed by them ends too."""
    skipped = [0.0]
    real_time, real_monotonic, real_perf = time.time, time.monotonic, time.perf_counter

    def sleep(seconds):
        if seconds < 0:
            raise ValueError("sleep length must be non-negative")
        link.request("sleep", seconds=float(seconds))
        skipped[0] += seconds

    time.sleep = sleep
    time.time = lambda: real_time() + skipped[0]
    time.monotonic = lambda: real_monotonic() + skipped[0]
    time.perf_counter = lambda: real_perf() + skipped[0]
    _fence(os.environ.get("KELVIN_RUN_DIR") or os.getcwd(), sandbox, link, pairing, record=record,
           words="a rehearsal reaches only the simulated lab")


# ------------------------------------------------------------------ what goes around the stand-ins: guard and fence
#
# Both are audit hooks (PEP 578) in the script's own process. Python's own documentation says such hooks are no sandbox:
# code written on purpose to get around them can. They keep honest code, and code an AI was talked into, from reaching
# an instrument past the gate, as run_python's sandbox does (labharness/data/sandbox/kelvin_sandbox.py, whose lists a
# fence shares); on Windows a run auto mode approved runs at low integrity besides, so Windows itself refuses its writes
# anywhere but its two folders. Everything a hook decides by is bound in it as it is made (default arguments, a
# closure): a script that rebinds this module's names changes nothing it decides (a review of 2026-11-15).

# what a fence refuses to import besides run_python's (DENIED_IMPORTS): COM, USB and FTDI by other names
_FENCED_IMPORTS = ("cffi", "_cffi_backend", "win32com", "win32api", "win32process", "win32file", "win32event",
                   "win32service", "pythoncom", "pywintypes", "comtypes", "clr", "pythonnet", "usb", "libusb_package",
                   "libusb1", "hid", "ftd2xx", "pyftdi", "pylibftdi", "_overlapped")
# what run_python refuses that a fenced run imports all the same: pyvisa and pyserial, whose lab instruments go through
# Kelvin's stand-ins (and whose other addresses Kelvin refuses in a fenced run); asyncio's Windows half, loaded
# already, whose proactor is taken out of reach instead (_no_overlapped)
_FENCE_IMPORTS_OK = ("pyvisa", "serial", "asyncio.windows_events", "asyncio.windows_utils")
# what run_python refuses that a fenced run may do, each looked at on its own: a socket of its own (made, and its name
# looked up: only connecting and sending are refused), changing into its own folders, a DLL folder of its Python's,
# reading the registry
_FENCE_EVENTS_OK = ("socket.", "os.chdir", "os.add_dll_directory")
_FENCE_SOCKETS = ("socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg", "socket.sethostname")
# the registry read (fonts, time zones, a file's type: matplotlib and the standard library look there), which changes
# nothing; every other registry event is refused
_REGISTRY_READS = ("winreg.OpenKey", "winreg.OpenKey/result", "winreg.EnumKey", "winreg.EnumValue",
                   "winreg.QueryInfoKey", "winreg.QueryValue", "winreg.QueryReflectionKey",
                   "winreg.ExpandEnvironmentStrings", "winreg.PyHKEY.Detach")
# in the folder of Kelvin's own a run auto mode approved may write in (its kelvin_ folder): never code, nor Kelvin's
# marks and kept versions, nor bytecode, so what runs next is still what Kelvin wrote and was checked
_CODE = (".py", ".pyw", ".pyc", ".pyo", ".pyd", ".pth", ".ipynb", ".so", ".dll", ".dylib", ".exe", ".com", ".bat",
         ".cmd", ".ps1", ".vbs", ".js", ".lnk", ".scr", ".msi")
_REFUSED = []   # what the fence refused, said to Kelvin as the script ends (also when the script carried on)
_RESTRICTED = []   # Windows' low integrity for a run auto mode approved: why it couldn't be, said as the script ends
AUTO_WORDS = "auto mode's fence (a run auto mode approved reaches the lab only through Kelvin's stand-ins)"
KEPT_WORDS = "one of the lab's experiments reaches the lab only through kelvin.lab"


class KelvinFenced(ImportError):
    """An import a fence refused: as a module that isn't there, so code that does without it goes on (Pillow 8.4's
    try: import cffi, which matplotlib loads; a PermissionError there broke every plot under the fence)."""


def _sandbox_module(path):
    """run_python's sandbox (labharness/data/sandbox/kelvin_sandbox.py), loaded from where Kelvin says (KELVIN_SANDBOX):
    what it refuses (DENIED_IMPORTS, DENIED_EVENTS, WRITES, WRITE_FLAGS: O_TEMPORARY too), how it compares paths and
    how it lowers a process to Windows' low integrity, which a fenced run shares rather than keeping a copy of its own.
    RuntimeError when it can't be loaded: the run doesn't start, never fenced less."""
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location("kelvin_sandbox", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.DENIED_IMPORTS, module.DENIED_EVENTS, module.WRITES, module.WRITE_FLAGS   # all there
        return module
    except Exception as e:
        raise RuntimeError("Kelvin couldn't set up the fence of a run nobody looked at (its sandbox at %s: %s: %s), so "
                           "it doesn't start" % (path, type(e).__name__, e))


def _python_dirs():
    """The script's Python's own folders (its installation, its environment, its site-packages): what a run auto mode
    approved imports comes from there (or is Kelvin's own), and a DLL folder a package adds as it loads (delvewheel's
    os.add_dll_directory: SciPy, pyzmq) is one of its own there."""
    import site
    dirs = [sys.prefix, sys.base_prefix, sys.exec_prefix, getattr(sys, "base_exec_prefix", "")]
    try:
        dirs += list(site.getsitepackages())
    except Exception:
        pass
    try:
        dirs.append(site.getusersitepackages())
    except Exception:
        pass
    return [d for d in dirs if d]


def _origins(spec):
    """Where a module found by a finder comes from: its file, a package's folders; () for one built in."""
    out = []
    origin = getattr(spec, "origin", None)
    if origin and origin not in ("built-in", "frozen", "namespace"):
        out.append(origin)
    out += [p for p in (getattr(spec, "submodule_search_locations", None) or []) if p]
    return out


def _inside(path, dirs, _norm=os.path.normcase, _abs=os.path.abspath, _sep=os.sep):
    p = _norm(_abs(path))
    for d in dirs:
        d = _norm(_abs(d)).rstrip("\\/")
        if p == d or p.startswith(d + _sep):
            return True
    return False


def _preload(names, dirs):
    """What the script imports from its Python's own packages, loaded before the fence goes up (KELVIN_PRELOAD, read
    ahead by Kelvin): a package's own start (matplotlib's font list, made with fc-list or read from the registry; a
    DLL found as it loads) is the package's, not the script's, and is never taken for the script reaching past. Only a
    module that comes from its Python's own folders (dirs): one anywhere else (PYTHONPATH, an editable install, a
    .pth's folder, a kelvin_ folder) is no package of its Python's, and loads only as the script imports it, fenced
    (a review of 2026-11-15: a lab library on PYTHONPATH ran a program and a raw socket here, before the fence). A
    person's Stop meanwhile ends the run (KeyboardInterrupt is never caught here)."""
    import importlib.util
    for name in [n.strip() for n in (names or "").split(",") if n.strip()]:
        top = name.split(".")[0]
        try:
            spec = importlib.util.find_spec(top)
            if spec is None or not all(_inside(o, dirs) for o in _origins(spec)):
                continue
            __import__(name)
        except Exception:   # the script meets the same error at its own import, in its own words
            pass


class _Pairing(object):
    """The one connection a fenced run makes of its own: a pair of sockets inside its own process, which asyncio's
    selector loop needs (Windows makes one with a loopback bind and connect: _socketpair). Each bind or connect it
    allows is made once, to its exact loopback address, by Kelvin's own socketpair, and is taken back as it is used."""

    def __init__(self):
        self.lock = threading.Lock()
        self.allowed = []

    def allow(self, event, address):
        with self.lock:
            self.allowed.append((event, address))

    def has(self, event, address):
        with self.lock:
            return (event, address) in self.allowed

    def take(self, event, address):
        with self.lock:
            if (event, address) in self.allowed:
                self.allowed.remove((event, address))
                return True
            return False


def _socketpair(pairing, real_socket=socket.socket, real_connect=_REAL["connect"]):
    """socket.socketpair for a fenced run on Windows, as Python's own makes one there (a loopback listener, a connect
    to it, the connection accepted), each step allowed once, to that address only (_Pairing): asyncio's selector loop
    works, and no other connection is opened (a review of 2026-11-15: asyncio ended every auto run on Windows)."""
    def socketpair(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0):
        if family == socket.AF_INET:
            host = "127.0.0.1"
        elif family == getattr(socket, "AF_INET6", None):
            host = "::1"
        else:
            raise ValueError("Only AF_INET and AF_INET6 socket address families are supported")
        lsock = real_socket(family, type, proto)
        try:
            pairing.allow("socket.bind", (host, 0))
            lsock.bind((host, 0))
            lsock.listen()
            addr, port = lsock.getsockname()[:2]
            csock = real_socket(family, type, proto)
            try:
                csock.setblocking(False)
                pairing.allow("socket.connect", (addr, port))
                try:
                    real_connect(csock, (addr, port))
                except (BlockingIOError, InterruptedError):
                    pass
                csock.setblocking(True)
                ssock, _ = lsock.accept()
                if ssock.getpeername()[:2] != csock.getsockname()[:2]:
                    ssock.close()
                    raise ConnectionError("Unexpected peer connection")
            except BaseException:
                csock.close()
                raise
        finally:
            lsock.close()
        return ssock, csock
    socketpair.kelvin = True
    return socketpair


def _no_overlapped(pairing):
    """Windows, a fenced run: asyncio's proactor out of reach (_overlapped's ConnectEx connects past every audit event:
    a review of 2026-11-15 sent SLVL 4.5 to the lab's lock-in with it), its module emptied where asyncio holds it and
    imported nowhere again; asyncio's default loop the selector one instead, its self-pipe Kelvin's socketpair."""
    if os.name != "nt":
        return
    stub = types.ModuleType("_overlapped")

    def missing(name):
        raise AttributeError("a fenced run has no _overlapped (asyncio's proactor connects past the gate): %s" % name)
    stub.__getattr__ = missing
    events = sys.modules.get("asyncio.windows_events")
    if events is not None:
        events._overlapped = stub
    sys.modules.pop("_overlapped", None)
    socket.socketpair = _socketpair(pairing)
    try:
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass


class _Finder(importlib.abc.MetaPathFinder):
    """A fenced run's imports, however they are asked for (an import statement, importlib.import_module or
    importlib.resources): a refused one fails as a module that isn't there (KelvinFenced). In a run auto mode approved
    (own: Kelvin's own modules it may import, by path, each with the sha256 Kelvin read; dirs: its Python's own folders
    and the stand-in's), any other module it imports must come from there: a module beside it that Kelvin didn't write
    (a review of 2026-11-15: importlib.resources.files('helper') ran a person's kelvin_scripts/helper.py), one Kelvin
    wrote that changed since, or one from anywhere else, is refused."""

    def __init__(self, refused, words, own=None, dirs=()):
        self.refused, self.words, self.own, self.dirs = tuple(refused), words, own, tuple(dirs)
        self.busy = threading.local()

    def _refuse(self, fullname, why):
        if len(_REFUSED) < 20:
            _REFUSED.append("import %s" % fullname)
        raise KelvinFenced("%s: import %s%s" % (self.words, fullname, why), name=fullname)

    def find_spec(self, fullname, path=None, target=None):
        for d in self.refused:
            if fullname == d or fullname.startswith(d + "."):
                self._refuse(fullname, "")
        if self.own is None or getattr(self.busy, "on", False):
            return None
        self.busy.on = True
        try:
            spec = None
            for finder in sys.meta_path:
                find = getattr(finder, "find_spec", None) if finder is not self else None
                if find is None:
                    continue
                spec = find(fullname, path, target)
                if spec is not None:
                    break
        finally:
            self.busy.on = False
        if spec is None:
            return None
        origin = getattr(spec, "origin", None)   # the code that runs (a namespace package's folder runs none)
        for origin in ([origin] if origin and origin not in ("built-in", "frozen", "namespace") else []):
            if _inside(origin, self.dirs):
                continue
            key = os.path.normcase(os.path.realpath(origin))
            sha = self.own.get(key)
            if sha is None:
                self._refuse(fullname, " from %s, which is neither one of Kelvin's own modules it read nor of its "
                                       "Python's packages" % origin)
            try:
                with open(origin, "rb") as f:
                    now = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                now = None
            if now != sha:
                self._refuse(fullname, " from %s, which changed after Kelvin read it" % origin)
        return spec


def _fence(folder, sandbox, link, pairing, words=KEPT_WORDS, writable=(), auto=False, record=None, ipc=None,
           ours=()):
    """A fence inside the script's process (an audit hook, PEP 578), run_python's refusals (sandbox: DENIED_EVENTS,
    WRITES, WRITE_FLAGS with O_TEMPORARY) with what a run needs besides: no programs, native code, COM, registry
    changes, USB, network or device files; writing only in the run's own folder (folder: a rehearsal's, a kept
    experiment's run folder; its record's files there, record: their names as Kelvin made them, stay Kelvin's) and in
    writable (a run auto mode approved: its kelvin_ folder, but no code there), alternate data streams included; a
    folder elsewhere isn't opened (so no dir_fd reaches past it); it may change into its folders, and a package of its
    Python's may add a DLL folder of its own; a refused import fails as a module that isn't there (KelvinFenced). The
    function code of a hook is never replaced, nor are objects listed (gc), as run_python refuses. What the experiment
    uses (NumPy) is loaded first. auto: a run auto mode approved, which nobody looked at: what the fence refuses ends
    the run (Kelvin is told at once: op fenced), its drives back to safe. Like run_python's sandbox it keeps honest code
    and code an AI was talked into away from the instruments; it is not a wall against code written to get around it.
    Python before 3.8 has no audit hooks, so it can't be fenced (Kelvin says so before it starts)."""
    if not hasattr(sys, "addaudithook"):
        raise RuntimeError("Python %s can't keep a rehearsal to the simulated lab: 3.8 or later" % sys.version.split()[0])
    try:
        import numpy  # noqa: F401  loaded before the fence: its own loading may use what the fence refuses
    except Exception:
        pass
    if os.name == "nt":   # each path as the system resolves it now (the folder it is in may change: os.chdir)
        import nt
        full, fold, sep = nt._getfullpathname, str.lower, "\\"
    else:
        full, fold, sep = os.path.abspath, str, "/"
    real = os.path.realpath if os.name != "nt" else None

    def folded(p):
        p = fold(full(p))
        if os.name == "nt":
            if p.startswith("\\\\?\\") and (p[5:6] == ":" or p[4:8] == "unc\\"):   # a long path: as any other
                p = p[4:] if p[5:6] == ":" else "\\\\" + p[8:]
            head, rest = (p[:2], p[2:]) if p[1:2] == ":" else ("", p)
            p = head + "\\".join(part.split(":")[0] for part in rest.split("\\"))   # x.py::$DATA is x.py
        return p.rstrip("\\/") if len(p) > 3 else p
    roots = list(sandbox._roots([folder] + [w for w in writable if w], full, fold))
    own = list(sandbox._roots([w for w in writable if w], full, fold))
    kept = tuple(folded(os.path.join(folder, n)) for n in (os.listdir(folder) if record is None else record))
    python = tuple(folded(d) for d in _python_dirs())
    null = fold(full(os.devnull))
    imports = tuple(d for d in sandbox.DENIED_IMPORTS if d not in _FENCE_IMPORTS_OK) + _FENCED_IMPORTS + \
        tuple(link.opaque)
    events = tuple(e for e in sandbox.DENIED_EVENTS if e not in _FENCE_EVENTS_OK) + _FENCE_SOCKETS
    writes = dict(sandbox.WRITES)
    writes["_winapi.CopyFile2"] = (1,)   # shutil.copy on Windows: its target, as any write
    telling = threading.local()
    host, _, port = (ipc or ":").rpartition(":")
    kelvin = (host, int(port)) if port.isdigit() else None
    mine = list(ours)   # the guard's hook, and this one's (added below): their code is never replaced

    def inside(p, dirs):
        for d in dirs:
            if p == d or p.startswith(d + sep):
                return True
        return False

    def hook(event, args, _sw=str.startswith, _isinstance=isinstance, _str=str, _bytes=bytes, _int=int,
             _fspath=os.fspath, _fold=folded, _null=null, _flags=sandbox.WRITE_FLAGS, _imports=imports,
             _events=events, _writes=writes, _refused=_REFUSED, _PE=PermissionError, _len=len, _words=words,
             _inside=inside, _roots=roots, _own=own, _kept=kept, _python=python, _code=_CODE, _reads=_REGISTRY_READS,
             _auto=auto, _telling=telling, _sep=sep, _link=link, _pairing=pairing, _isdir=os.path.isdir,
             _real=real, _sockets=_FENCE_SOCKETS, _kelvin=kelvin, _Fenced=KelvinFenced, _tuple=tuple, _mine=mine,
             _getattr=getattr):
        def refuse(what, imported=""):
            if _len(_refused) < 20:
                _refused.append(what)
            if imported:   # as a module that isn't there: the script goes on without it, or fails as it would
                raise _Fenced("%s: %s" % (_words, what), name=imported)
            if _auto and not getattr(_telling, "on", False):
                _telling.on = True   # Kelvin ends the run at once, its drives back to safe (a stop follows)
                try:
                    _link.request("fenced", what=what)
                except KelvinRefused:
                    pass
                finally:
                    _telling.on = False
            raise _PE("%s: %s" % (_words, what))

        def path_of(path):
            p = _fspath(path)
            if _isinstance(p, _bytes):
                p = p.decode("utf-8", "surrogateescape")
            return _fold(p)
        if event == "import":
            name = args[0] if args else ""
            if _isinstance(name, _str):
                for d in _imports:
                    if name == d or _sw(name, d + "."):
                        refuse("import %s" % name, imported=name)
            return
        if event == "open" or event in _writes:
            if event == "open":
                mode, flags = (args[1] if _len(args) > 1 else None), (args[2] if _len(args) > 2 else 0)
                writing = (_isinstance(mode, _str) and ("w" in mode or "a" in mode or "x" in mode or "+" in mode)) \
                    or (_isinstance(flags, _int) and flags & _flags)
                spots = ((args[0] if args else None, writing),)
            else:
                mode = None
                spots = _tuple((args[i], True) for i in _writes[event] if i < _len(args))
            for path, writing in spots:
                if path is None or _isinstance(path, _int):
                    continue   # no file, or one opened already (so already checked)
                try:
                    p = path_of(path)
                except (OSError, ValueError, TypeError):
                    refuse("%r, which is not a path a fenced run may use" % (path,))
                if event == "sqlite3.connect" and p.endswith(":memory:"):
                    continue
                if p == _null:
                    continue
                if _sw(p, "\\\\.\\") or _sw(p, "\\\\?\\") or _sw(p, "//./") or _sw(p, "/dev/") or p == "/dev":
                    refuse("the device %s" % path)
                if not writing:
                    if event == "open" and mode is None and not _inside(p, _roots) and _isdir(p):
                        refuse("opening the folder %s, outside its own folders" % path)   # a dir_fd past them
                    continue
                if not _inside(p, _roots) or (_real is not None and not _inside(_real(p), _roots)):
                    refuse("writing %s, outside its own folders" % path)
                if _inside(p, _kept):
                    refuse("writing %s, Kelvin's own record of the run" % path)
                if _inside(p, _own):
                    low = p.rsplit(_sep, 1)[-1]
                    parts = p.split(_sep)
                    if low.startswith(".kelvin-made") or ".kelvin-old" in parts or "__pycache__" in parts or \
                            any(low.endswith(c) for c in _code):
                        refuse("writing %s: code, and Kelvin's own marks, aren't written by a run there" % path)
            return
        if event == "os.chdir":   # into the folders it may write in, as a script that saves its data where it is
            try:
                p = path_of(args[0]) if args else ""
            except (OSError, ValueError, TypeError):
                p = ""
            if not p or not _inside(p, _roots):
                refuse("os.chdir to %s, outside its own folders" % (args[0] if args else "?"))
            return
        if event == "os.add_dll_directory":   # a package of its Python's adds its own DLL folder (delvewheel)
            try:
                p = path_of(args[0]) if args else ""
            except (OSError, ValueError, TypeError):
                p = ""
            if not p or not _inside(p, _python):
                refuse("os.add_dll_directory of %s, outside its Python" % (args[0] if args else "?"))
            return
        if event in _sockets:
            address = args[1] if _len(args) > 1 else None
            if _isinstance(address, _tuple) and _len(address) >= 2:
                if _pairing.take(event, (address[0], address[1])) or (_kelvin is not None and
                                                                      (address[0], address[1]) == _kelvin):
                    return   # the socketpair asyncio's loop needs; Kelvin's own connection
            refuse("%s to %s" % (event, address))
        if event == "object.__setattr__" and _len(args) > 1 and args[1] in ("__code__", "__defaults__",
                                                                            "__kwdefaults__") and \
                (args[0] in _mine or _getattr(args[0], "__module__", None) == "kelvin_shim"):
            refuse("replacing the %s of Kelvin's own %s" % (args[1], _getattr(args[0], "__name__", "function")))
        if event in _reads:
            return
        for d in _events:
            if _sw(event, d):
                refuse(event)

    mine.append(hook)
    sys.addaudithook(hook)


_PSEUDO = ("null", "zero", "random", "urandom", "full", "stdin", "stdout", "stderr", "tty", "ptmx", "console")
_GLOBAL = ("\\\\?\\globalroot\\global??\\", "\\\\?\\globalroot\\??\\", "\\\\.\\globalroot\\global??\\",
           "\\\\.\\globalroot\\??\\", "\\\\?\\")


def _device(path, writing=False):
    """A device file this path opens, as the system names it (COM5 is \\\\.\\COM5, and so are \\\\?\\COM5 and
    \\\\?\\GLOBALROOT\\GLOBAL??\\COM5; /dev/ttyUSB0, through any link to it: /dev/serial/by-id/..., /proc/self/root),
    or None for a file: however its path was spelled or put together (bytes, joined from parts, relative)."""
    try:
        p = os.fspath(path)
        if isinstance(p, bytes):
            p = p.decode("utf-8", "surrogateescape")
    except TypeError:
        return None
    if os.name == "nt":
        import nt
        try:
            f = nt._getfullpathname(p)
        except (OSError, ValueError):
            return None
        low = f.lower()
        for prefix in _GLOBAL:
            if low.startswith(prefix) and not (prefix == "\\\\?\\" and (low[5:6] == ":" or low[4:8] == "unc\\")):
                f, low = "\\\\.\\" + f[len(prefix):], "\\\\.\\" + low[len(prefix):]
                break
        if low.startswith("\\\\.\\") or low.startswith("//./"):
            rest = low[4:].split(":")[0]
            if rest in ("nul", "con", "conin$", "conout$") or rest.startswith("pipe\\") or rest.startswith("pipe/"):
                return None
            return f
        return None
    f = os.path.normpath(p if p.startswith("/") else os.path.join(os.getcwd(), p))
    if f.startswith("/dev/") or f.startswith("/proc/") or os.path.islink(f):
        try:
            f = os.path.realpath(f)   # a link to the port (/dev/serial/by-id), the root seen through /proc
        except (OSError, ValueError):
            pass
    if f.startswith("/dev/"):
        rest = f[5:]
        if rest in _PSEUDO or rest.startswith(("fd/", "pts/", "shm/", "mqueue/")):
            return None
        return f
    if f.startswith("/sys/") and writing:   # a GPIO pin, a USB device's switch
        return f
    return None


def _guard(link, pairing, ipc=None):
    """Every run (a run a person approved; a fenced one too, first): a way to a lab instrument past the stand-ins that
    the script's own process takes is put to Kelvin first (an audit hook, PEP 578; op past): a socket's own connection
    or datagram (the stand-in patches socket.socket's methods; _socket's own, sendto and sendmsg went past them: a
    review of 2026-11-15 had a raw _socket send SLVL 4.5 to the lab's lock-in), and a device file opened as a file
    (COM5, /dev/ttyUSB0). A lab instrument's address, or another way into one, is refused and ends the run; any other
    goes as the stand-ins' own would. Its state is its own (a closure): a script can't switch it off by a module's name
    (the review: kelvin_shim._GUARD.on = True did)."""
    if not hasattr(sys, "addaudithook"):
        return
    said = {}
    busy = threading.local()   # this thread is asking Kelvin: what its own request does is never asked again
    host, _, port = (ipc or ":").rpartition(":")
    kelvin = (host, int(port)) if port.isdigit() else None
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC | getattr(os, "O_TEMPORARY", 0)

    def check(where, how, _link=link, _said=said, _busy=busy):
        key = (where, how)
        if key not in _said:
            _busy.on = True
            try:
                _link.past(where, how)
                _said[key] = ""
            except KelvinRefused as e:
                _said[key] = str(e)
            finally:
                _busy.on = False
        if _said[key]:
            raise PermissionError("Kelvin: %s" % _said[key])

    def hook(event, args, _busy=busy, _check=check, _address=_socket_address, _device=_device, _pairing=pairing,
             _kelvin=kelvin, _flags=write_flags, _isinstance=isinstance, _len=len, _int=int, _str=str,
             _tuple=tuple, _any=any, _getattr=getattr):
        if _getattr(_busy, "on", False):
            return
        if event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
            address = args[1] if _len(args) > 1 else None
            if _isinstance(address, _tuple) and _len(address) >= 2 and (
                    (address[0], address[1]) == _kelvin or _pairing.has(event, (address[0], address[1]))):
                return   # Kelvin's own connection (a forked child's); the socketpair asyncio's loop needs
            where = _address(address)
            if where:
                _check(where, event)
            elif address is not None and not _isinstance(address, (_str, bytes)):   # an address it can't read
                _check("%r" % (address,), event)
        elif event == "open" and args and args[0] is not None and not _isinstance(args[0], _int):
            mode, flags = (args[1] if _len(args) > 1 else None), (args[2] if _len(args) > 2 else 0)
            writing = (_isinstance(mode, _str) and _any(c in mode for c in "wax+")) or \
                (_isinstance(flags, _int) and bool(flags & _flags))
            device = _device(args[0], writing)
            if device:
                _check(device, "device")

    sys.addaudithook(hook)
    return hook


def _install_api():
    """Kelvin's experiment API as the module kelvin, from the file next to this one (never one the script's folder
    or its Python has under that name)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("kelvin", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                         "kelvin_api.py"))
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    api.attach(sys.modules[__name__])
    sys.modules["kelvin"] = api


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
    missing, foreign = [], {}
    dirs = _python_dirs()
    for name in names:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            missing.append(name)
            continue
        away = [o for o in _origins(spec) if not _inside(o, dirs)]
        if away:   # not one of its Python's own packages (PYTHONPATH, an editable install, a .pth's folder)
            foreign[name] = away[0]
    sys.stdout.write(json.dumps({"version": list(sys.version_info[:2]), "missing": missing, "foreign": foreign,
                                 "executable": sys.executable}) + "\n")
    return 0


def _home(script, name):
    """The folder python -m NAME runs SCRIPT from: the one above its top package (nvpulse.__main__ is
    <home>/nvpulse/__main__.py)."""
    home = os.path.dirname(script)
    for _ in name.split(".")[1:]:
        home = os.path.dirname(home)
    return home


def _same_file(a, b):
    return bool(a) and bool(b) and os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _as_module(module, name, script, home):
    """Ready the __main__ module to run SCRIPT as the module NAME of its package, as python -m does (runpy): the
    package (each one on the way down) imported first, so its __init__ runs as it always does, then __package__ and
    __spec__ set, so `from .cli import main` finds the package's own modules. Only the lab's own package, in HOME: one
    of that name that Python has already, or finds first elsewhere, is never imported in its place."""
    import importlib
    import importlib.util
    top = name.split(".")[0]
    found = importlib.util.find_spec(top)   # a top package is looked for, not imported
    where = list(getattr(found, "submodule_search_locations", None) or []) or [getattr(found, "origin", None)]
    inside = os.path.normcase(os.path.realpath(home)).rstrip("\\/") + os.sep
    if found is None or not any(w and os.path.normcase(os.path.realpath(w)).startswith(inside) for w in where):
        raise ImportError("Kelvin: the package %s is %s here, not the lab's in %s: Python found another %s first"
                          % (top, where[0] if found is not None and where[0] else "nowhere", home, top), name=top)
    package = name.rpartition(".")[0]
    if package:
        importlib.import_module(package)
    spec = importlib.util.find_spec(name)
    origin = getattr(spec, "origin", None)
    if not _same_file(origin, script):
        raise ImportError("Kelvin: the module %s is %s here, not %s: Python found another %s first"
                          % (name, origin or "nowhere", script, top), name=name)
    module.__dict__.update(__package__=package, __spec__=spec, __loader__=spec.loader,
                           __cached__=getattr(spec, "cached", None))


def main(argv):
    if argv[:1] == ["--probe"]:
        return _probe(argv[1:])
    source = name = None
    while argv[:1] in (["--source"], ["--module"]) and len(argv) > 1:
        if argv[0] == "--source":
            source = argv[1]
        else:
            name = argv[1]
        argv = argv[2:]
    if not argv:
        sys.stderr.write("usage: kelvin_shim.py [--source FILE] [--module NAME] SCRIPT [ARGS...]\n")
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
    sys.argv = [script] + list(argv[1:])   # as python -m has it too: the module's file, then its arguments
    home = _home(script, name) if name else None
    sys.path[0] = home or os.path.dirname(script)
    module = types.ModuleType("__main__")
    module.__dict__.update(__file__=script, __builtins__=builtins, __cached__=None)
    if os.environ.get("KELVIN_ACTION"):   # an experiment's action: Kelvin runs it, so its __main__ block doesn't too
        module.__dict__["__name__"] = "__kelvin_experiment__"
    sys.modules["__main__"] = module
    status, error = 0, ""
    try:
        if name:   # its package's __init__ fails as the script would: in the run's output, with its traceback
            _as_module(module, name, script, home)
        exec(compile(text, script, "exec"), module.__dict__)
        api = sys.modules.get("kelvin")
        if getattr(api, "_ACTIONS", None):   # an experiment's action: the one Kelvin was asked to run
            api._run(os.environ.get("KELVIN_ACTION", ""))
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
                         figures=figures, refused=list(_REFUSED), restricted=list(_RESTRICTED))
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
