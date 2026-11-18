"""Instruments that speak SCPI over ZeroMQ: each command a request answered by one reply, through the same safety gate
(field report 2026-11-17: IDQ's ID900 and ID1000 Time Controllers, which Kelvin found on port 5555 but couldn't
reach, so the lab wrote a wrapper and a card of its own).

Address: zmq://<host>[:<port>], the port 5555 when it names none, e.g.
    zmq://192.168.1.78:5555        an IDQ Time Controller, which its examples reach as tcp://192.168.1.78:5555

The card is a card of text commands, as for SCPI over any other connection. Kelvin's side is a REQ socket and the
instrument's a REP (or ROUTER) socket: each command goes as one message, without a line end, and gets exactly one
reply, a setting too, which is read before anything else is sent (a REQ socket can't send twice):
    - a reply that matches the card's connection.error_reply (SCPI_ERR, as IDQ's refusals say, when it names none) is
      the instrument's own error, raised in its words (ErrorAnswer: it is there and answering);
    - a setting's reply that doesn't match connection.set_reply, where the card has one, is a refusal, as on VISA;
    - a reply of several frames reads as one, its frames' values separated by commas as SCPI's are (joined).
A reply that doesn't come within connection.timeout_ms (5 s when it names none) is NoAnswer, and the connection is
dropped: a REQ socket can't ask again before its reply, and a late one is never read as the next request's. The next
request connects anew, greetings and all; so does one after the instrument closed the connection while nothing was
asked (nothing had gone on it, so nothing is sent twice). A connection that ends under a request is LinkLost, and that
request is never sent again: it may have acted.

Kelvin speaks ZMTP itself (probe.Zmtp, as its look at a port does), since the app doesn't carry pyzmq: version 3.0,
without security (mechanism NULL). An instrument that wants CURVE or PLAIN security is said to, and not connected."""
from __future__ import annotations

import contextlib
import ipaddress
import re
import socket
import threading
import urllib.parse
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .adapters import CLOSE_CALL_S, AdapterError, ErrorAnswer, LinkLost, NoAnswer
from .cards import Card
from .drivers import link_failed
from .probe import Zmtp, ZmtpStop

PORT = 5555                # where IDQ's Time Controllers answer requests
TIMEOUT_MS = 5000          # a reply's wait when the card's connection names none, as for a JSON-RPC instrument's
ERROR_REPLY = "SCPI_ERR"   # in an IDQ Time Controller's refusal (a card's connection.error_reply instead)
MOST_REPLY = 64 << 20      # the longest reply read, in bytes: a Time Controller's histogram, as text, is about 1 MB
MOST_FRAMES = 1 << 16      # the most frames one reply may have
LONGEST_S = 600.0          # the longest a reply may keep coming, however steadily its pieces come
STEP_S = 0.2               # how often a long wait for a reply (query_waiting) looks for a person's Stop
TOO_LONG = "it sent a frame of %%d bytes, which makes a reply longer than Kelvin reads (%d MB)" % (MOST_REPLY >> 20)


def where(address: str) -> Tuple[str, int]:
    """The host and port of a zmq:// address, the port PORT when it names none. ValueError, saying how one is written,
    for anything else."""
    url = urllib.parse.urlparse((address or "").strip())
    try:
        port = url.port if url.port is not None else PORT
    except ValueError:   # a port that isn't a number, or past 65535
        port = 0
    if url.scheme.lower() != "zmq" or not url.hostname or not 0 < port < 65536 or url.path.strip("/") \
            or url.params or url.query or url.fragment or url.username or url.password:
        raise ValueError("%r is not a ZeroMQ instrument's address: zmq://<its IP address or host name>[:<port>], e.g. "
                         "zmq://192.168.1.78:5555 (port %d when it names none)" % (address, PORT))
    return url.hostname, port


def canonical(address: str) -> str:
    """A zmq:// address as Kelvin compares it: zmq://<host>:<port>, the port written out and the host in lower case (an
    IPv6 one in brackets); anything else as it is."""
    try:
        host, port = where(address)
    except ValueError:
        return (address or "").strip()
    return "zmq://%s:%d" % ("[%s]" % host if ":" in host else host, port)


def as_socket(address: str) -> str:
    """The TCP port a ZeroMQ instrument listens on, named as a script's raw socket there is (TCPIP0::<host>::<port>::
    SOCKET, as Kelvin's stand-in names it: kelvin_shim._socket_address), so the script fence knows the instrument by
    it (scriptrun.canonical). ValueError for an address that isn't zmq://."""
    host, port = where(address)
    if ":" in host:   # an IPv6 address, written as the stand-in writes it (an IPv4 one inside it as itself)
        try:
            ip = ipaddress.ip_address(host.split("%")[0])
            mapped = getattr(ip, "ipv4_mapped", None)
            host = str(mapped) if mapped is not None else "[%s]" % ip.compressed
        except ValueError:
            host = "[%s]" % host
    return "TCPIP0::%s::%d::SOCKET" % (host, port)


def joined(parts: List[bytes]) -> bytes:
    """A reply of several frames as one: each frame one of its values, separated by commas as SCPI separates a reply's
    values (none more where a frame already ends, or the next begins, with one), so that field: picks one."""
    out: List[bytes] = []
    for i, part in enumerate(parts):
        if i and not parts[i - 1].endswith(b",") and not part.startswith(b","):
            out.append(b",")
        out.append(part)
    return b"".join(out)


def _close(sock: Any) -> None:
    try:
        sock.close()
    except OSError:
        pass


class ZmqInstrument:
    """write(command) / query(command) / close(), as a VISA backend's, for an instrument that speaks SCPI over ZeroMQ.
    Not a maker's driver (drivers.DriverBackend), though its scheme is registered among theirs: its commands are the
    instrument's own text, so the lab treats it as any text instrument (its identity is asked of it, never said for it
    by a driver; a card is tried on it reading only what only reads: drivers.sends_text)."""
    simulated = False
    quiet_close = True   # closing the connection sends the instrument nothing: its ZeroMQ socket only sees it go

    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        self.name, self.card, self.address = name, card, address
        try:
            self.host, self.port = where(address)
        except ValueError as e:
            raise AdapterError("%s: %s" % (name, e)) from None
        conn = card.connection
        self.timeout = float(conn.get("timeout_ms", TIMEOUT_MS)) / 1000.0
        try:
            self.error = re.compile(str(conn.get("error_reply") or ERROR_REPLY))
            self.set_reply = re.compile(str(conn["set_reply"])) if conn.get("set_reply") else None
        except re.error as e:
            raise AdapterError("%s: card %s's connection has a regex that isn't one: %s" % (name, card.id, e)) from None
        self.connect = getattr(module, "create_connection", None) or socket.create_connection   # tests: a fake
        self.lock = threading.RLock()   # one request at a time; re-entrant, as a VISA backend's (Instrument._turn)
        self.link: Optional[Zmtp] = None
        self.peer: Dict[str, Any] = {}   # what it said of itself as it greeted: its ZMTP version, its socket type
        self._open()

    # ------------------------------------------------------------------ the connection

    def _open(self) -> Zmtp:
        """The connection made and greeted as a REQ socket: the instrument must answer requests (a REP or ROUTER
        socket), in ZMTP 3.0 or later, without security."""
        try:
            sock = self.connect((self.host, self.port), self.timeout)
        except OSError as e:
            raise link_failed(self.name, "could not reach %s:%d. Is it on, at this address, and on this computer's "
                                         "network?" % (self.host, self.port), e) from e
        link = Zmtp(sock, seconds=self.timeout)
        peer: Dict[str, Any] = {}
        try:
            link.greet(peer, "Kelvin")
        except ZmtpStop as e:
            _close(sock)
            raise self._failed(e, "", self.timeout) from e
        self.link, self.peer = link, peer
        return link

    def _drop(self) -> None:
        """The connection let go of, whatever may still come on it: the next request makes a new one."""
        link, self.link = self.link, None
        if link is not None:
            _close(link.sock)

    def _link(self) -> Zmtp:
        """The connection for the next request: made anew where there is none (a failure dropped it), or where the
        instrument closed it while nothing was asked (nothing had gone on it, so nothing is sent twice)."""
        link = self.link
        if link is not None and not link.idle():
            self._drop()
            link = None
        return link if link is not None else self._open()

    def _failed(self, e: ZmtpStop, command: str, seconds: float) -> AdapterError:
        """A request (command; "" while connecting) that couldn't go on, as the error Kelvin raises for it, its
        connection dropped first: a REQ socket can't ask again before its reply, and a late reply is never read as
        the next request's. NoAnswer when nothing came in time, LinkLost when the connection ended; else AdapterError
        with what the instrument sent or refused (it is there, so it is never a lost instrument)."""
        self._drop()
        at, asked = "%s:%d" % (self.host, self.port), command.strip()[:80]
        if e.kind == "time":
            return NoAnswer("%s: no reply to %s within %g s" % (self.name, asked, seconds) if asked else
                            "%s: %s took the connection, but didn't greet as ZeroMQ does within %g s"
                            % (self.name, at, seconds))
        if e.kind == "closed":
            return LinkLost("%s: lost the connection to %s %s (%s)" % (
                self.name, at, "asking %s" % asked if asked else "as it connected", e))
        if asked:
            return AdapterError("%s: the reply to %s from %s can't be read: %s" % (self.name, asked, at, e))
        return AdapterError("%s: can't connect to ZeroMQ at %s: %s" % (self.name, at, e))

    def _exchange(self, command: str, seconds: Optional[float] = None,
                  stopped: Optional[Callable[[], bool]] = None) -> str:
        """One request and its reply, as text: its frames as one (joined), without a C string's NUL or a line end at
        its end. ErrorAnswer for a reply that matches the card's error_reply. seconds, stopped: a long wait's
        (query_waiting): its first bytes awaited `seconds`, a step at a time, a person's Stop ending the wait."""
        wait = self.timeout if seconds is None else seconds
        with self.lock:
            link = self._link()
            try:
                link.until(self.timeout)
                link.request(command.encode("utf-8"))
                if stopped is not None:
                    if not link.wait(wait, stopped, STEP_S):
                        self._drop()   # its reply would come on this connection later: never read as another's
                        from .instrument import Stopped
                        raise Stopped("%s: stopped by a person while it was still working on %s" % (self.name,
                                                                                                   command))
                    wait = self.timeout   # its reply has begun: the rest of it comes as any reply's does
                link.until(wait, LONGEST_S)   # a long reply waited for as long as it keeps coming
                parts = link.reply(MOST_FRAMES, MOST_REPLY, TOO_LONG)
            except ZmtpStop as e:
                raise self._failed(e, command, wait) from e
        text = joined(parts).decode("utf-8", "replace").rstrip("\x00\r\n")
        if self.error.search(text):
            raise ErrorAnswer("%s refused %r: %s" % (self.name, command.strip(), text.strip()[:200]))
        return text

    # ------------------------------------------------------------------ what the gate and the lab call

    def write(self, command: str) -> None:
        """A command that sets: its reply read too, as every request's is, and checked against the card's set_reply
        where it has one."""
        reply = self._exchange(command).strip()
        if self.set_reply is not None and not self.set_reply.search(reply):
            raise AdapterError("%s refused %r: %s" % (self.name, command, reply[:200]))

    def query(self, command: str) -> str:
        """A command that reads: its reply, whole however long (a histogram), as text."""
        return self._exchange(command)

    def query_waiting(self, command: str, seconds: float, stopped: Callable[[], bool]) -> str:
        """A query the instrument answers only once it has finished something long (*OPC? after an acquisition): sent
        once, its reply awaited `seconds` at most a moment at a time, so a person's Stop ends the wait at once
        (Stopped). Either way the connection is then dropped, as after any reply that didn't come."""
        return self._exchange(command, seconds, stopped)

    def brief(self, ms: int) -> Any:
        """Its requests (and a connection made meanwhile) wait ms at most meanwhile, a with block, as a VISA backend's
        do: asking one that stopped answering whether it is back takes that long, not its card's whole timeout."""
        @contextlib.contextmanager
        def short() -> Iterator[None]:
            with self.lock:
                before = self.timeout
                self.timeout = min(before, ms / 1000.0)
                try:
                    yield
                finally:
                    self.timeout = before
        return short()

    def close(self) -> None:
        """The connection closed, under its lock when that comes within CLOSE_CALL_S; else beside the request that has
        it, which then fails as one whose connection ended."""
        held = self.lock.acquire(timeout=CLOSE_CALL_S)
        try:
            self._drop()
        finally:
            if held:
                self.lock.release()
