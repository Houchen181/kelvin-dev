"""GPIB instruments through a Prologix adapter: the GPIB-ETHERNET (TCP port 1234) and the GPIB-USB (a USB serial port,
FTDI's chip). Many labs reach their GPIB instruments this way, without a GPIB card or its driver.

Kelvin speaks the adapter's own ++ commands (its manual, version 1.6.6.0) with no maker's software: a socket for the
GPIB-ETHERNET, pyserial (which Kelvin brings) for the GPIB-USB. Addresses in a lab config:
    prologix://192.168.1.50/12          GPIB address 12 behind the GPIB-ETHERNET at 192.168.1.50 (port 1234)
    prologix://192.168.1.50:1234/12     the same, with its port
    prologix://COM5/12                  GPIB address 12 behind the GPIB-USB on COM5 (prologix:///dev/ttyUSB0/12)
    prologix://COM5/12/0                and a secondary address: 0 to 30 as VISA writes it (96 to 126 as the manual does)

Every instrument behind one adapter shares its one connection (the GPIB-ETHERNET takes one at a time), and one exchange
with it goes at a time, each addressed to its instrument (++addr), so replies can't cross. An instrument is written and
read as a PyVISA resource is, so VisaBackend drives it with all a card can ask for. The adapter's settings are its own,
not each address's: they are written once as Kelvin connects, after switching off their saving in its EEPROM (++savecfg
0), which addressing instruments in turn would wear out. A reply is read to where the instrument ends it (EOI, which the
adapter marks with ++eot_char); the adapter waits 3 s at most for a byte, so it is asked again for a slower one.

Not pyvisa-py's Prologix support (PRLGX-TCPIP / PRLGX-ASRL, pyvisa-py 0.8.0 and later): it gives the adapter 50 ms to
start each reply (++read_tmo_ms 50), so a slower reply is lost; one instrument's read is lost when two behind an adapter
are asked at once; and it runs only on pyvisa-py, under GPIB0:: names that clash with a GPIB card's."""
from __future__ import annotations

import contextlib
import math
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .adapters import AdapterError, resource_name

PORT = 1234                # the GPIB-ETHERNET's TCP port
END = b"\x04"               # what the adapter adds where an instrument asserted EOI: the end of its reply
MARGIN_S = 0.1              # waited beyond the adapter's own read timeout; by then it has given up
MAX_TMO_MS = 3000           # the longest the adapter waits for a byte (++read_tmo_ms)
CONNECT_S = 3.0
VER_S = 1.0                 # for the adapter's answer to ++ver
SCAN_MS = 300               # a scan's wait for each GPIB address's *IDN? reply (30 addresses, one after another)
GPIB_ADDRESSES = range(1, 31)   # what a scan asks; 0 is the controller's own, by custom
SETUP = (b"++savecfg 0",        # first: nothing after it is written to the EEPROM
         b"++mode 1", b"++auto 0", b"++eoi 1", b"++eos 3", b"++eot_enable 1", b"++eot_char 4")
VERSION = re.compile(r"prologix|gpib\S*\s+controller", re.I)   # what a Prologix (or one that works as it does) says
FTDI, FTDI_PIDS = 0x0403, (0x6001, 0x6015)   # the GPIB-USB's chip: FT232R / FT245R, or the newer FT-X
_ESCAPE = re.compile(rb"([\x1b\r\n+])")       # bytes of a command the adapter would take for its own
_ADDRESS = re.compile(r"^prologix://(?P<adapter>[^?#]+?)/(?P<pad>\d{1,2})(?:/(?P<sad>\d{1,3}))?/?$", re.I)


def is_prologix(address: str) -> bool:
    return (address or "").strip().lower().startswith("prologix://")


def on_serial(adapter: str) -> bool:
    """A GPIB-USB's serial port (COM5, /dev/ttyUSB0), not a GPIB-ETHERNET's host:port."""
    return bool(re.match(r"^(COM\d+|/dev/\S+)$", adapter, re.I))


def adapter_key(text: str) -> str:
    """An adapter as Kelvin keeps it: COM5, /dev/ttyUSB0, or a network one's host:port (192.168.1.50:1234)."""
    a = text.strip().rstrip("/")
    if on_serial(a):
        return a.upper() if a.upper().startswith("COM") else a
    host, sep, port = a.rpartition(":")
    if not sep:
        host, port = a, str(PORT)
    if not host or not port.isdigit() or not re.match(r"^[\w.\-]+$", host):
        raise ValueError("%r is not a Prologix adapter: give its serial port (COM5, /dev/ttyUSB0) or its network "
                         "address (192.168.1.50, or 192.168.1.50:1234)" % text)
    return "%s:%d" % (host.lower(), int(port))


def adapter_of(text: str) -> str:
    """The adapter an address or a name means: prologix://192.168.1.50/12, prologix://192.168.1.50, 192.168.1.50:1234,
    COM5."""
    t = text.strip()
    if is_prologix(t):
        t = re.sub(r"/\d{1,2}(?:/\d{1,3})?/?$", "", t[len("prologix://"):])
    return adapter_key(t)


def shown(adapter: str) -> str:
    """The adapter as an address writes it: the GPIB-ETHERNET's own port left out."""
    return adapter[:-len(":%d" % PORT)] if adapter.endswith(":%d" % PORT) else adapter


def place(adapter: str) -> str:
    """Where the adapter is, in words: "on COM5", "at 192.168.1.50"."""
    return ("on " if on_serial(adapter) else "at ") + shown(adapter)


def visa_name(adapter: str) -> str:
    """The adapter itself as VISA names it, as a program that speaks its ++ commands opens it: ASRL5::INSTR for COM5,
    TCPIP0::192.168.1.50::1234::SOCKET for a GPIB-ETHERNET."""
    if on_serial(adapter):
        return resource_name("serial://" + adapter)
    host, _, port = adapter.rpartition(":")
    return "TCPIP0::%s::%s::SOCKET" % (host, port)


@dataclass(frozen=True)
class Where:
    """An instrument behind an adapter: the adapter (adapter_key) and the instrument's GPIB address."""
    adapter: str
    pad: int
    sad: Optional[int] = None   # 0 to 30, as VISA writes it

    @property
    def address(self) -> str:
        return "prologix://%s/%d%s" % (shown(self.adapter), self.pad, "" if self.sad is None else "/%d" % self.sad)

    def words(self) -> str:
        return "GPIB %d%s behind the Prologix %s" % (self.pad, "" if self.sad is None else " (secondary %d)" % self.sad,
                                                     place(self.adapter))

    def addr_command(self) -> bytes:
        return b"++addr %d" % self.pad + (b"" if self.sad is None else b" %d" % (96 + self.sad))


def where(address: str) -> Where:
    """The adapter and GPIB address an address names; ValueError for one that isn't a Prologix address."""
    m = _ADDRESS.match((address or "").strip())
    if not m:
        raise ValueError("%r is not a Prologix address: prologix://<adapter>/<GPIB address>, such as "
                         "prologix://192.168.1.50/12 or prologix://COM5/12" % address)
    pad = int(m.group("pad"))
    if pad > 30:
        raise ValueError("%s: GPIB addresses are 0 to 30" % address)
    sad = None if m.group("sad") is None else int(m.group("sad"))
    if sad is not None and 96 <= sad <= 126:
        sad -= 96   # as the manual writes it
    if sad is not None and sad > 30:
        raise ValueError("%s: a secondary address is 0 to 30 (or 96 to 126, as the Prologix manual writes it)" % address)
    return Where(adapter_key(m.group("adapter")), pad, sad)


def canonical(address: str) -> str:
    """One way to write an instrument's address (COM in capitals, the GPIB-ETHERNET's own port left out); an address
    that isn't one comes back as it is."""
    try:
        return where(address).address
    except ValueError:
        return address.strip()


def escape(data: bytes) -> bytes:
    """A command's bytes as the adapter passes them on whole: CR, LF, ESC and + would be its own, so each goes after an
    ESC. A command can't address another instrument or reset the adapter this way (++addr 7 reaches the instrument as
    text)."""
    return _ESCAPE.sub(lambda m: b"\x1b" + m.group(1), data)


def first_line(e: BaseException) -> str:
    return str(e).splitlines()[0][:160] if str(e).strip() else type(e).__name__


# ------------------------------------------------------------------ the connection to an adapter

class _Socket:
    """A GPIB-ETHERNET, over TCP."""

    def __init__(self, host: str, port: int, seconds: float):
        self.sock = socket.create_connection((host, port), timeout=seconds)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def send(self, data: bytes) -> None:
        self.sock.settimeout(CONNECT_S)
        self.sock.sendall(data)

    def recv(self, seconds: float) -> bytes:
        """What has come within the time (b'' for nothing); ConnectionError when the adapter closed the connection."""
        self.sock.settimeout(max(seconds, 0.001))
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return b""
        if not data:
            raise ConnectionError("the adapter closed the connection")
        return data

    def drop_input(self) -> None:
        self.sock.settimeout(0)
        try:
            while self.sock.recv(65536):
                pass
        except (BlockingIOError, socket.timeout):
            pass

    def close(self) -> None:
        self.sock.close()


class _Serial:
    """A GPIB-USB, on its serial port (a pyserial Serial)."""

    def __init__(self, port: Any):
        self.port = port

    def send(self, data: bytes) -> None:
        self.port.write(data)
        self.port.flush()

    def recv(self, seconds: float) -> bytes:
        self.port.timeout = max(seconds, 0.001)
        data = self.port.read(1)
        waiting = self.port.in_waiting if data else 0
        return data + self.port.read(waiting) if waiting else data

    def drop_input(self) -> None:
        self.port.reset_input_buffer()

    def close(self) -> None:
        self.port.close()


def _open(adapter: str, seconds: float) -> Any:
    if on_serial(adapter):
        try:
            import serial
        except ImportError as e:
            raise AdapterError("a GPIB-USB adapter needs pyserial: pip install \"kelvin-lab[hardware]\"") from e
        # its chip passes bytes through over USB: the speed is not the instrument's and changes nothing
        return _Serial(serial.Serial(adapter, 115200, timeout=seconds, write_timeout=seconds))
    host, _, port = adapter.rpartition(":")
    return _Socket(host, int(port), seconds)


OPEN: Callable[[str, float], Any] = _open   # tests put a pretend adapter here


def _hello(link: Any, skip: bool = False) -> str:
    """What the adapter says it is (++ver): "Prologix GPIB-USB Controller version 6.107". skip: what came before it
    (anything the commands before said) is passed over, so after it the connection holds nothing old."""
    link.drop_input()
    link.send(b"++ver\n")
    buf, end = b"", time.monotonic() + VER_S
    while True:
        lines = buf.split(b"\n")
        for line in lines[:-1]:
            text = line.decode("latin-1", "replace").strip()
            if text and (not skip or VERSION.search(text)):
                return text
        left = end - time.monotonic()
        if left <= 0:
            return "" if skip else lines[-1].decode("latin-1", "replace").strip()
        buf += link.recv(left)


def _end_at(buf: bytes, block: bool) -> int:
    """Where the reply in buf ends (the adapter's END), or -1 until it has. block: the reply holds an IEEE 488.2 block of
    binary numbers, whose bytes may be END's: only after the block counts."""
    end = buf.find(END)
    if not block:
        return end
    i = buf.find(b"#")
    if i < 0 or 0 <= end < i:   # no block yet, or the reply ended before one
        return end
    if len(buf) < i + 2:
        return -1
    digits = buf[i + 1:i + 2]
    if not digits.isdigit():
        return end
    n = int(digits)
    if n == 0:   # of no stated length: it ends with the message
        return buf.find(END, i + 2)
    if len(buf) < i + 2 + n:
        return -1
    size = buf[i + 2:i + 2 + n]
    return buf.find(END, i + 2 + n + int(size)) if size.isdigit() else end


class Adapter:
    """One Prologix: one connection, shared by every instrument behind it, and one exchange on it at a time."""

    def __init__(self, key: str):
        self.key = key
        self.lock = threading.RLock()
        self.users = 0
        self.link: Any = None
        self.version = ""
        self.state: Dict[str, bytes] = {}   # the adapter's settings as last written (++addr, ++read_tmo_ms)
        self.last: Optional[Where] = None   # the instrument of the last exchange

    def connect(self) -> str:
        """Connected (again, after a lost connection); the adapter's version. AdapterError: it can't be reached, or
        what answers isn't a Prologix."""
        with self.lock:
            if self.link is not None:
                return self.version
            try:
                link = OPEN(self.key, CONNECT_S)
            except AdapterError:
                raise
            except Exception as e:
                raise AdapterError(self._unreachable(e)) from e
            try:
                version = _hello(link)
                if not VERSION.search(version):
                    raise AdapterError("%s did not answer ++ver as a Prologix adapter does (%s): is the adapter %s?"
                                       % (shown(self.key), "it said %r" % version[:60] if version else "no answer",
                                          place(self.key)))
                for command in SETUP:
                    link.send(command + b"\n")
                if not _hello(link, skip=True):   # done with them, and whatever they said is passed over
                    raise AdapterError("the Prologix %s stopped answering as Kelvin set it up" % place(self.key))
            except AdapterError:
                link.close()
                raise
            except Exception as e:
                link.close()
                raise AdapterError(self._unreachable(e)) from e
            self.link, self.version, self.state, self.last = link, version, {}, None
            return version

    def _unreachable(self, e: BaseException) -> str:
        if on_serial(self.key):
            return ("the Prologix %s could not be opened (%s): is it plugged in, and not open in another program?"
                    % (place(self.key), first_line(e)))
        return ("the Prologix %s does not take a connection (%s): is it on, at that address? It takes one connection "
                "at a time, so another program (or another Kelvin) may have it" % (place(self.key), first_line(e)))

    def _link(self) -> Any:
        """The connection for an exchange, made again if it was lost; ConnectionError when that fails: the
        instrument can't be reached."""
        if self.link is None:
            try:
                self.connect()
            except AdapterError as e:
                raise ConnectionError(str(e)) from e
        return self.link

    def _lost(self, e: BaseException) -> ConnectionError:
        """The connection broke (a pulled cable, the adapter switched off): it is let go, so the next exchange
        connects again."""
        self.disconnect()
        return ConnectionError("lost the connection to the Prologix %s (%s)" % (place(self.key), first_line(e)))

    def disconnect(self) -> None:
        with self.lock:
            link, self.link, self.state, self.last = self.link, None, {}, None
            if link is not None:
                try:
                    link.close()
                except Exception:
                    pass

    def _set(self, link: Any, key: str, command: bytes) -> None:
        if self.state.get(key) != command:
            link.send(command + b"\n")
            self.state[key] = command

    def _address(self, link: Any, at: Where) -> None:
        if self.last != at:
            link.drop_input()   # nothing left from another instrument's exchange can pass for this one's
        self._set(link, "addr", at.addr_command())
        self.last = at

    def write(self, at: Where, data: bytes) -> None:
        with self.lock:
            link = self._link()
            try:
                link.drop_input()   # a new command: nothing still to come belongs to it
                self._address(link, at)
                link.send(escape(data) + b"\n")
            except OSError as e:
                raise self._lost(e) from e

    def clear(self, at: Where) -> None:
        """Selected Device Clear (++clr): the instrument drops what it was doing and what it had to say."""
        with self.lock:
            link = self._link()
            try:
                self._address(link, at)
                link.send(b"++clr\n")
            except OSError as e:
                raise self._lost(e) from e

    def read(self, at: Where, tmo_ms: int, term: bytes = b"", block: bool = False) -> Tuple[bytes, str]:
        """One ++read of the instrument at `at`: (bytes, "end") when it ended its reply with EOI; with term (an
        instrument that ends its replies without EOI, read to its line end), (bytes, "term") at that byte; (bytes,
        "silent") once the adapter has stopped waiting for more (tmo_ms without a byte)."""
        with self.lock:
            link = self._link()
            try:
                self._address(link, at)
                self._set(link, "tmo", b"++read_tmo_ms %d" % tmo_ms)
                link.send(b"++read %d\n" % term[-1] if term else b"++read eoi\n")
                buf, quiet = b"", tmo_ms / 1000.0 + MARGIN_S
                until = time.monotonic() + quiet
                while True:
                    end = _end_at(buf, block)
                    if end >= 0:
                        return buf[:end], "end"
                    if term and buf.endswith(term[-1:]):
                        return buf, "term"
                    left = until - time.monotonic()
                    if left <= 0:
                        return buf, "silent"
                    chunk = link.recv(left)
                    if chunk:
                        buf += chunk
                        until = time.monotonic() + quiet
            except OSError as e:
                raise self._lost(e) from e


_adapters: Dict[str, Adapter] = {}
_adapters_lock = threading.Lock()


def _take(key: str) -> Adapter:
    with _adapters_lock:
        a = _adapters.get(key)
        if a is None:
            a = _adapters[key] = Adapter(key)
        a.users += 1
        return a


def _give_back(a: Adapter) -> None:
    with _adapters_lock:
        a.users -= 1
        if a.users <= 0 and _adapters.get(a.key) is a:
            del _adapters[a.key]
            a.disconnect()


@contextlib.contextmanager
def held(adapter: str) -> Iterator[Adapter]:
    """The adapter (as adapter_of reads it) for the time of a with block, shared with the instruments of the lab
    behind it; not connected yet: Adapter.connect."""
    try:
        key = adapter_of(adapter)
    except ValueError as e:
        raise AdapterError(str(e)) from None
    a = _take(key)
    try:
        yield a
    finally:
        _give_back(a)


def version_of(adapter: str) -> str:
    """The adapter's answer to ++ver where a Prologix is ('' where none answers: another device, a busy port)."""
    try:
        with held(adapter) as a:
            return a.connect()
    except (AdapterError, OSError, ValueError):
        return ""


# ------------------------------------------------------------------ one instrument behind it

class Resource:
    """One instrument behind an adapter, written and read as a PyVISA resource is (what VisaBackend uses): timeout in
    ms, read and write terminations, write, read, query, write_raw, read_raw, clear, query_binary_values, close."""

    def __init__(self, adapter: Adapter, at: Where):
        self.adapter, self.at = adapter, at
        self.resource_name = at.address
        self.timeout: Optional[float] = 3000
        self.read_termination: Optional[str] = "\n"
        self.write_termination: Optional[str] = "\n"
        self.encoding = "ascii"
        self._rx, self._end = b"", False   # what came of its reply and isn't read yet; and whether that ends it
        self._eoi = True                   # it ends its replies with EOI (most do; one that doesn't is read to its line end)
        self._closed = False

    def write(self, message: str) -> int:
        return self.write_raw((message + (self.write_termination or "")).encode(self.encoding))

    def write_raw(self, data: bytes) -> int:
        self._open()
        self._rx, self._end = b"", False   # a new command: what it hadn't read of the last reply is no longer wanted
        self.adapter.write(self.at, bytes(data))
        return len(data)

    def read(self) -> str:
        term = self.read_termination or ""
        text = self._read(term.encode(self.encoding)).decode(self.encoding)
        return text[:-len(term)] if term and text.endswith(term) else text

    def read_raw(self, size: Optional[int] = None) -> bytes:
        return self._read(b"")

    def query(self, message: str, delay: Optional[float] = None) -> str:
        self.write(message)
        if delay:
            time.sleep(delay)
        return self.read()

    def query_binary_values(self, message: str, datatype: str = "f", is_big_endian: bool = False,
                            container: Any = list, **_: Any) -> Any:
        """A reply that holds an IEEE 488.2 block of binary numbers (an oscilloscope's waveform)."""
        from pyvisa.util import from_ieee_block
        self.write(message)
        return from_ieee_block(self._read(b"", block=True), datatype, is_big_endian, container)

    def clear(self) -> None:
        self._open()
        self._rx, self._end = b"", False
        self.adapter.clear(self.at)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            _give_back(self.adapter)

    def _open(self) -> None:
        if self._closed:   # as a closed VISA session: it would otherwise connect to the adapter again
            raise ConnectionError("%s: Kelvin closed its connection" % self.at.words())

    def _take(self, term: bytes) -> Optional[bytes]:
        """A whole reply from what came: to its line end (the last byte of the read termination, as VISA reads), or to
        where the instrument ended it."""
        if term:
            i = self._rx.find(term[-1:])
            if i >= 0:
                out, self._rx = self._rx[:i + 1], self._rx[i + 1:]
                self._end = self._end and bool(self._rx)
                return out
        if self._end:
            out, self._rx, self._end = self._rx, b"", False
            return out
        return None

    def _read(self, term: bytes, block: bool = False) -> bytes:
        """One reply's bytes, to its line end (term) or to its end. The timeout runs from the last byte that came, and
        the adapter waits at most 3 s for one, so it is asked again (++read) while a longer one runs."""
        self._open()
        seconds = 1e6 if self.timeout is None else max(float(self.timeout), 1.0) / 1000.0
        until = time.monotonic() + seconds
        while True:
            got = self._take(term)
            if got is not None:
                return got
            left = until - time.monotonic()
            if left <= 0:
                raise TimeoutError("%s did not answer within %g s" % (self.at.words(), seconds))
            tmo = max(1, min(MAX_TMO_MS, math.ceil(left * 1000)))
            data, how = self.adapter.read(self.at, tmo, b"" if self._eoi or not term else term, block)
            if data:
                self._rx += data
                until = time.monotonic() + seconds
            if how == "end" or (how == "silent" and data and not term):
                self._end = True   # read to its end: without EOI, what came before it fell silent
            elif how == "silent" and term and term[-1:] in data:
                self._eoi = False   # its line end came, with no EOI after it: read to its line end from now on


class _Manager:
    """What VisaBackend opens a Prologix address with, in place of PyVISA's resource manager."""
    description = "Kelvin's own Prologix support"

    @staticmethod
    def open_resource(name: str) -> Resource:
        return open_resource(name)


MANAGER = _Manager()


def open_resource(address: str) -> Resource:
    """One instrument behind its adapter, connecting to the adapter unless an instrument behind it already has.
    Nothing is sent to the instrument."""
    try:
        at = where(address)
    except ValueError as e:
        raise AdapterError(str(e)) from None
    a = _take(at.adapter)
    try:
        a.connect()
    except BaseException:
        _give_back(a)
        raise
    return Resource(a, at)


def usb_ports() -> List[str]:
    """Serial ports that may be a Prologix GPIB-USB: on FTDI's chips, or naming themselves Prologix (on Linux and
    macOS the adapter says what it is). Whether one is, its answer to ++ver tells."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    out = []
    for p in list_ports.comports():
        text = " ".join(str(x or "") for x in (p.manufacturer, p.product, p.description)).lower()
        if "prologix" in text or (p.vid == FTDI and p.pid in FTDI_PIDS):
            out.append(p.device)
    return out
