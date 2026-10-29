"""Real instrument backends: VISA (GPIB, USB-TMC, LAN, RS-232) through PyVISA, set up from the card.

A backend has the same three methods as a twin: write(command), query(command) -> str, close(). Each holds its
own lock, so the agent, a plan and the lab panel never interleave on one instrument. Like everything below the
safety gate, a backend has no safety checks of its own; only the gate and the run engine call it.

Addresses in a lab config:
    sim://name                                  a twin (simulated), see sim/twin.py
    GPIB0::12::INSTR                            any VISA resource string, as NI MAX or `kelvin-scan` shows it
    USB0::0x05E6::0x2450::04412345::INSTR       USB-TMC
    TCPIP0::192.168.1.20::inst0::INSTR          LAN (VXI-11); TCPIP0::host::5025::SOCKET for raw sockets
    ASRL3::INSTR  or  serial://COM3             RS-232 / USB-serial, with the card's baud rate and framing
    prologix://192.168.1.50/12                  GPIB address 12 behind a Prologix GPIB-ETHERNET; prologix://COM5/12
                                                behind a GPIB-USB (prologix.py: Kelvin speaks to the adapter itself)

PyVISA uses the installed VISA (NI-VISA, Keysight IO Libraries) when there is one, else its pure-Python backend
(pyvisa-py, with pyserial for serial ports). Set KELVIN_VISA_BACKEND (e.g. "@py") to choose.
"""
from __future__ import annotations

import os
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from .cards import Card, parity_name, serial_settings

_rm: Any = None
_rm_lock = threading.Lock()
WAIT_SLICE_MS = 1000   # a long wait on an instrument is read a second at a time, so a stop ends it within one


class AdapterError(RuntimeError):
    pass


def timed_out(e: BaseException) -> bool:
    """A read that ran out of time (PyVISA's VI_ERROR_TMO), not a broken connection; also when Kelvin said it in
    words (instrument.InstrumentLost), from the error it came from."""
    if isinstance(e, TimeoutError) or getattr(e, "error_code", None) == -1073807339 or "VI_ERROR_TMO" in str(e):
        return True
    return e.__cause__ is not None and e.__cause__ is not e and timed_out(e.__cause__)


# VISA's codes for an instrument that can't be reached at all, as opposed to one that is slow to answer
GONE = {-1073807265: "VI_ERROR_NLISTENERS",    # GPIB: nothing listens at the address (off, unplugged, address changed)
        -1073807343: "VI_ERROR_RSRC_NFOUND",   # no longer listed (a pulled USB cable, switched off)
        -1073807194: "VI_ERROR_CONN_LOST",     # a LAN connection dropped
        -1073807298: "VI_ERROR_IO",            # an I/O error (a pulled USB cable)
        -1073807346: "VI_ERROR_INV_OBJECT"}    # the session went away
_SLOW = re.compile(r"timed? ?out|time-out|did not answer|didn't answer|no response", re.I)
_GONE = re.compile(r"not connected|isn't connected|connection (was )?(lost|refused|reset|closed|aborted)|unreachable"
                   r"|no listeners|device (was )?(removed|not found|disconnected)|broken pipe", re.I)


def lost_kind(e: BaseException) -> Optional[str]:
    """Whether an error says the instrument isn't answering: "gone" when it can't be reached at all (nothing listens
    on GPIB, a pulled USB cable, a dropped connection), "silent" when it didn't answer in time; None for any other
    error (a command it refused, a card's mistake), which is not a lost instrument."""
    if getattr(e, "error_code", None) in GONE or isinstance(e, (ConnectionError, BrokenPipeError)):
        return "gone"
    if timed_out(e):
        return "silent"
    text = str(e)
    if _GONE.search(text):
        return "gone"
    if _SLOW.search(text):
        return "silent"
    return None


# a maker's software an instrument is reached through, by its address's scheme: (where, what to check)
_SOFTWARE = {"qd": ("MultiVu at %s", "is MultiVu running, with its server started?"),
             "zhinst": ("LabOne at %s", "is the LabOne data server running, and the instrument on?")}


def where_and_cause(address: str) -> Tuple[str, str]:
    """Where an instrument at this address is reached, and what to check when it stops answering, in words:
    ("on GPIB (address 7)", "cable or power?"), ("through MultiVu at 127.0.0.1:5000", "is MultiVu running, ...?");
    ("", "") for a simulated one."""
    address = (address or "").strip()
    scheme, sep, rest = address.partition("://")
    if sep and scheme.lower() == "sim":
        return "", ""
    if sep and scheme.lower() == "prologix":
        from .prologix import place, where
        try:
            at = where(address)
        except ValueError:
            return "through a Prologix adapter (%s)" % rest, "cable or power?"
        return ("on GPIB (address %d) through the Prologix %s" % (at.pad, place(at.adapter)),
                "cable or power? (or the adapter: unplugged, or held by another program)")
    if sep and scheme.lower() not in ("visa", "serial"):
        where, cause = _SOFTWARE.get(scheme.lower(), ("its maker's software (%s)" % address,
                                                      "is it on, and its maker's software running?"))
        return "through " + (where % rest if "%s" in where else where), cause
    name = resource_name(address).upper()
    m = re.match(r"^GPIB\d*::(\d+)", name)
    if m:
        return "on GPIB (address %s)" % m.group(1), "cable or power?"
    if name.startswith("USB"):
        return "on USB", "USB cable or power?"
    m = re.match(r"^TCPIP\d*::([^:]+)", name)
    if m:
        return "on the network (%s)" % m.group(1), "network cable, power or its IP address?"
    m = re.match(r"^ASRL(\w+?)::", name)
    if m:
        port = "COM%s" % m.group(1) if m.group(1).isdigit() else m.group(1)
        return "on " + port, "cable or power? (or the port's baud rate and framing)"
    return "at " + address, "cable or power?"


def short_model(model: str) -> str:
    """A card's model name, short enough for an alarm: "Keithley 2182 / 2182A nanovoltmeter" as it is, "Quantum Design
    PPMS / DynaCool / VersaLab / MPMS3 (MultiVu)" -> "Quantum Design PPMS / DynaCool"."""
    text = re.split(r" \(|, ", model or "", maxsplit=1)[0].strip()
    while len(text) > 40 and " / " in text:
        text = text.rsplit(" / ", 1)[0]
    return text if len(text) <= 40 else text[:39].rstrip() + "…"


def short_code(e: BaseException) -> str:
    """The error as the bus or library gave it, short: VISA's code name, else its first line."""
    code = getattr(e, "error_code", None)
    if code in GONE or code == -1073807339:
        return GONE.get(code, "VI_ERROR_TMO")
    text = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return text[:120]


def resource_manager() -> Any:
    """The process-wide PyVISA resource manager (created on first use)."""
    global _rm
    with _rm_lock:
        if _rm is None:
            try:
                import pyvisa
            except ImportError as e:
                raise AdapterError("talking to real instruments needs PyVISA: pip install \"kelvin-lab[hardware]\"") from e
            from .survey import use_bundled_libusb
            use_bundled_libusb()   # Kelvin's own libusb, for USB instruments without a vendor VISA
            choice = os.environ.get("KELVIN_VISA_BACKEND")
            if choice:
                _rm = pyvisa.ResourceManager(choice)
            else:
                try:
                    _rm = pyvisa.ResourceManager()          # NI-VISA / Keysight / R&S VISA if installed
                except Exception:
                    _rm = pyvisa.ResourceManager("@py")     # pure Python: GPIB needs a driver, USB needs libusb
        return _rm


def set_resource_manager(rm: Any) -> None:
    """Use another resource manager (tests use a fake bus)."""
    global _rm
    with _rm_lock:
        _rm = rm


def resource_name(address: str) -> str:
    """A lab-config address as a VISA resource string."""
    address = address.strip()
    if address.startswith("visa://"):
        return address[len("visa://"):]
    if address.lower().startswith("prologix://"):   # not VISA's: one way to write it (prologix.py)
        from .prologix import canonical
        return canonical(address)
    m = re.match(r"^serial://(.+)$", address)
    if m:
        port = m.group(1)
        num = re.match(r"^COM(\d+)$", port, re.I)
        return "ASRL%s::INSTR" % (num.group(1) if num else port)
    return address


def is_serial(name: str) -> bool:
    return name.upper().startswith("ASRL")


def marks_message_end(name: str) -> bool:
    """A bus that marks where each reply ends (EOI on GPIB, also through a Prologix adapter; the end of a USB-TMC or
    VXI-11/HiSLIP message), unlike a plain socket or a serial line, where only a line end does."""
    name = name.upper()
    return name.endswith("::INSTR") and not is_serial(name) or name.startswith("PROLOGIX://")


def apply_serial_settings(res: Any, settings: Dict[str, Any]) -> None:
    """Baud rate and framing on a serial resource, from a card's connection block (or a lab config's): parity as a
    word or the manual's letter (N, O, E), or framing: 8N1."""
    settings = serial_settings(settings)
    try:
        from pyvisa import constants
        parity = {"none": constants.Parity.none, "odd": constants.Parity.odd, "even": constants.Parity.even,
                  "mark": constants.Parity.mark, "space": constants.Parity.space}
        stop = {1.0: constants.StopBits.one, 1.5: constants.StopBits.one_and_a_half, 2.0: constants.StopBits.two}
    except ImportError:  # a fake bus in tests
        parity, stop = {}, {}
    if "parity" in settings and parity_name(settings["parity"]) is None:
        raise AdapterError("parity %r: it is none, odd, even, mark or space (N, O, E, M, S)" % (settings["parity"],))
    if "baud_rate" in settings:
        res.baud_rate = int(settings["baud_rate"])
    if "data_bits" in settings:
        res.data_bits = int(settings["data_bits"])
    if "parity" in settings:
        res.parity = parity.get(settings["parity"], settings["parity"])
    if "stop_bits" in settings:
        res.stop_bits = stop.get(float(settings["stop_bits"]), settings["stop_bits"])


class VisaBackend:
    simulated = False
    quiet_close = True   # closing the session sends the instrument nothing

    def __init__(self, name: str, card: Card, address: str, rm: Any = None,
                 overrides: Optional[Dict[str, Any]] = None):
        self.name = name
        self.address = address
        self.resource = resource_name(address)
        conn = dict(card.connection, **serial_settings(overrides or {}))   # a lab's framing: 8N1 wins over the card's
        self.ack = conn.get("ack")
        self.enq = bool(conn.get("enq"))
        self.echo = bool(conn.get("echo"))
        self.set_reply = re.compile(conn["set_reply"]) if conn.get("set_reply") else None
        self.write_only = bool(conn.get("write_only"))
        self.query_delay = float(conn.get("query_delay", 0.0))
        self.lock = threading.RLock()
        self._rm, self._conn, self._on_connect = rm, conn, list(card.on_connect)
        self._open()

    def _open(self) -> None:
        try:
            self.res = (self._rm or resource_manager()).open_resource(self.resource)
        except AdapterError:
            raise
        except Exception as e:
            if "LIBRARY_NFOUND" in str(e):
                raise AdapterError("%s: this computer's VISA cannot reach %s: its interface driver is missing (for GPIB, "
                                   "install NI-488.2 or your GPIB adapter's driver)." % (self.name, self.resource)) from e
            raise AdapterError("%s: cannot open %s (%s). Check the cable, the address and that no other program "
                               "holds the instrument." % (self.name, self.resource, e)) from e
        self.res.timeout = int(self._conn.get("timeout_ms", 3000))
        self.res.read_termination = self._conn.get("read_termination", "\n")
        self.res.write_termination = self._conn.get("write_termination", "\n")
        if is_serial(self.resource):
            try:
                apply_serial_settings(self.res, self._conn)
            except AdapterError as e:
                self.close()
                raise AdapterError("%s: %s" % (self.name, e)) from None
        if self._conn.get("greeting_lines"):
            self._drop_greeting(int(self._conn["greeting_lines"]))
        for command in self._on_connect:   # configuration only; parse_card refuses drive commands here
            self.write(command)

    def _drop_greeting(self, lines: int) -> None:
        """The lines an instrument sends by itself as a connection opens (the AMI 430 greets each LAN connection with
        two), read and dropped: left there, every reply would be taken for the command before. One that sends none
        (the same instrument on its serial port) costs a short wait."""
        before = self.res.timeout
        try:
            self.res.timeout = min(int(before or 3000), 2000)
            for _ in range(lines):
                try:
                    self.res.read()
                except Exception as e:
                    if timed_out(e):
                        break
                    raise
        finally:
            self.res.timeout = before

    def _check_ack(self, command: str, answer: Optional[str] = None) -> None:
        """The ack line after a command (answer: a query's reply, read before it). An instrument that acks only its
        set commands leaves a query's ack missing: its card wants set_reply, not ack, and the error says so."""
        try:
            reply = str(self.res.read()).strip()
        except Exception as e:
            if answer is not None and timed_out(e):
                raise AdapterError("%s answered %r to %r but sent no %r after it: if only its set commands are "
                                   "answered %r, its card wants set_reply: \"^%s$\" instead of ack"
                                   % (self.name, answer.strip(), command, self.ack, self.ack, re.escape(self.ack))) from e
            raise
        if reply != self.ack:
            raise AdapterError("%s: %r answered %r instead of %r" % (self.name, command, reply, self.ack))

    def _enq_ack(self, command: str) -> None:
        """Pfeiffer's protocol: the command, then <ACK> (or <NAK>) on a line of its own. Measured values the
        instrument was sending by itself until then (it does from power-up) are passed over."""
        self.res.write(command)
        for _ in range(20):
            reply = str(self.res.read()).strip()
            if reply == "\x06":
                return
            if reply == "\x15":
                raise AdapterError("%s refused %r (it answered NAK)" % (self.name, command))
        raise AdapterError("%s did not acknowledge %r" % (self.name, command))

    def write(self, command: str) -> None:
        with self.lock:
            if self.enq:
                return self._enq_ack(command)
            self.res.write(command)
            if self.echo:
                self.res.read()   # the command, echoed
            if self.ack:
                self._check_ack(command)
            elif self.set_reply is not None:   # instruments that answer every set (e.g. Oxford Mercury: ...:VALID)
                reply = str(self.res.read()).strip()
                if not self.set_reply.search(reply):
                    raise AdapterError("%s refused %r: %s" % (self.name, command, reply))

    def query(self, command: str) -> str:
        if self.write_only:
            raise AdapterError("%s does not answer queries (write-only); Kelvin uses the last value it set" % self.name)
        with self.lock:
            try:
                return self._query(command)
            except UnicodeDecodeError as e:   # bytes, not text: a binary block where the card expects text
                self.drain()
                from .cards import BINARY_REPLY
                raise AdapterError("%s (%s): %s" % (self.name, command, BINARY_REPLY)) from e

    def query_trace(self, command: str) -> str:
        """A trace's reply, read to the end of its message where the bus marks one, not to the first line end: some
        instruments send a line per point (HP's network analyzers). On a plain socket or a serial line, where only a
        line end marks it, the reply ends there as usual."""
        if not marks_message_end(self.resource) or self.write_only or self.enq or self.echo or self.ack:
            return self.query(command)
        with self.lock:
            before = self.res.read_termination
            self.res.read_termination = ""   # read to EOI (GPIB) or the message's end (USB, LAN)
            try:
                return self.query(command)
            finally:
                self.res.read_termination = before

    def query_waiting(self, command: str, seconds: float, stopped: Callable[[], bool]) -> str:
        """A query the instrument answers only once it has finished something long (*OPC? after a sweep): asked once,
        then its answer awaited a moment at a time, so a person's stop ends the wait at once (Stopped) instead of
        when the sweep would have."""
        if self.write_only or self.enq or self.echo or self.ack or self.query_delay:
            return self.query(command)
        import time
        from .instrument import Stopped
        with self.lock:
            res = self.res
            before, end = res.timeout, time.monotonic() + seconds
            res.write(command)
            try:
                res.timeout = WAIT_SLICE_MS
                while True:
                    try:
                        return str(res.read())
                    except Exception as e:
                        if not timed_out(e):
                            raise
                    if stopped():
                        self._abandon()
                        raise Stopped("%s: stopped by a person while it was still working on %s" % (self.name, command))
                    if time.monotonic() >= end:
                        raise AdapterError("%s did not answer %s within %.0f s" % (self.name, command, seconds))
            finally:
                if self.res is res:   # a connection made again after a stop keeps its own timeout
                    res.timeout = before

    def _abandon(self) -> None:
        """After a stop cut a wait short, the answer the instrument still owes must never be taken for a later
        command's: a bus that marks message ends gets a device clear (which also ends the wait on the instrument);
        a plain socket is connected again, which drops it; a serial line can only be emptied."""
        if marks_message_end(self.resource) or is_serial(self.resource):
            self.drain()
            return
        try:
            self.res.close()
        except Exception:
            pass
        try:
            self._open()
        except Exception:
            pass   # the next command says the instrument can't be reached

    def drain(self) -> None:
        """Drop the rest of a reply Kelvin could not read (a binary block cut at a line-end byte), so the next reply
        is the next command's: a device clear, or on a plain socket, reading until nothing more comes."""
        import time
        with self.lock:
            try:
                self.res.clear()
                return
            except Exception:
                pass
            before, end = self.res.timeout, time.monotonic() + 5.0
            try:
                self.res.timeout = 300
                while time.monotonic() < end:
                    self.res.read_raw()
            except Exception:
                pass   # nothing more within 0.3 s: drained
            finally:
                self.res.timeout = before

    def _query(self, command: str) -> str:
        if self.enq:   # acknowledged, then the reply on <ENQ>
            self._enq_ack(command)
            self.res.write_raw(b"\x05")
            return str(self.res.read())
        if self.echo:
            self.res.write(command)
            self.res.read()   # the command, echoed
            if self.query_delay:
                import time
                time.sleep(self.query_delay)
            reply = str(self.res.read())
        elif self.query_delay:
            reply = str(self.res.query(command, delay=self.query_delay))
        else:
            reply = str(self.res.query(command))
        if self.ack:
            self._check_ack(command, reply)
        return reply

    def query_binary(self, command: str, binary: str) -> List[float]:
        """A trace the instrument sends as an IEEE 488.2 block of binary numbers (an oscilloscope's waveform in BYTE
        or WORD format): binary is the card's "int16", or "int16 little" when the lowest byte comes first."""
        from .cards import BINARY
        if self.write_only or self.enq or self.echo:
            raise AdapterError("%s: binary traces need an instrument that answers queries plainly" % self.name)
        number, _, order = binary.partition(" ")
        with self.lock:
            try:
                values = self.res.query_binary_values(command, datatype=BINARY[number],
                                                      is_big_endian=order.strip() != "little", container=list)
            except (ValueError, TypeError) as e:
                raise AdapterError("%s: %r did not answer with a block of %s numbers (%s)" % (self.name, command,
                                                                                            number, e)) from e
        return [float(v) for v in values]

    def close(self) -> None:
        with self.lock:
            try:
                self.res.close()
            except Exception:
                pass


def open_backend(name: str, card: Card, address: str, rm: Optional[Any] = None,
                 overrides: Optional[Dict[str, Any]] = None) -> Any:
    """VISA for addresses like GPIB0::24::INSTR or serial://COM3; Kelvin's own Prologix support for prologix://; a
    vendor driver for zhinst:// or qd://."""
    from .drivers import is_driver_address, open_driver
    from .prologix import MANAGER, is_prologix
    if is_driver_address(address):
        return open_driver(name, card, address)
    if is_prologix(address):   # GPIB through the adapter: VISA isn't needed, and doesn't know these addresses
        return VisaBackend(name, card, address, rm if rm is not None else MANAGER, overrides)
    return VisaBackend(name, card, address, rm, overrides)
