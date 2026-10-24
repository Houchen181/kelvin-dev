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

PyVISA uses the installed VISA (NI-VISA, Keysight IO Libraries) when there is one, else its pure-Python backend
(pyvisa-py, with pyserial for serial ports). Set KELVIN_VISA_BACKEND (e.g. "@py") to choose.
"""
from __future__ import annotations

import os
import re
import threading
from typing import Any, Dict, Optional

from .cards import Card

_rm: Any = None
_rm_lock = threading.Lock()


class AdapterError(RuntimeError):
    pass


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
    m = re.match(r"^serial://(.+)$", address)
    if m:
        port = m.group(1)
        num = re.match(r"^COM(\d+)$", port, re.I)
        return "ASRL%s::INSTR" % (num.group(1) if num else port)
    return address


def is_serial(name: str) -> bool:
    return name.upper().startswith("ASRL")


def apply_serial_settings(res: Any, settings: Dict[str, Any]) -> None:
    """Baud rate and framing on a serial resource, from a card's connection block."""
    try:
        from pyvisa import constants
        parity = {"none": constants.Parity.none, "odd": constants.Parity.odd, "even": constants.Parity.even,
                  "mark": constants.Parity.mark, "space": constants.Parity.space}
        stop = {1.0: constants.StopBits.one, 1.5: constants.StopBits.one_and_a_half, 2.0: constants.StopBits.two}
    except ImportError:  # a fake bus in tests
        parity, stop = {}, {}
    if "baud_rate" in settings:
        res.baud_rate = int(settings["baud_rate"])
    if "data_bits" in settings:
        res.data_bits = int(settings["data_bits"])
    if "parity" in settings:
        res.parity = parity.get(str(settings["parity"]).lower(), settings["parity"])
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
        conn = dict(card.connection, **(overrides or {}))
        self.ack = conn.get("ack")
        self.echo = bool(conn.get("echo"))
        self.set_reply = re.compile(conn["set_reply"]) if conn.get("set_reply") else None
        self.write_only = bool(conn.get("write_only"))
        self.query_delay = float(conn.get("query_delay", 0.0))
        self.lock = threading.RLock()
        try:
            self.res = (rm or resource_manager()).open_resource(self.resource)
        except AdapterError:
            raise
        except Exception as e:
            if "LIBRARY_NFOUND" in str(e):
                raise AdapterError("%s: this computer's VISA cannot reach %s: its interface driver is missing (for GPIB, "
                                   "install NI-488.2 or your GPIB adapter's driver)." % (name, self.resource)) from e
            raise AdapterError("%s: cannot open %s (%s). Check the cable, the address and that no other program "
                               "holds the instrument." % (name, self.resource, e)) from e
        self.res.timeout = int(conn.get("timeout_ms", 3000))
        self.res.read_termination = conn.get("read_termination", "\n")
        self.res.write_termination = conn.get("write_termination", "\n")
        if is_serial(self.resource):
            apply_serial_settings(self.res, conn)
        for command in card.on_connect:   # configuration only; parse_card refuses drive commands here
            self.write(command)

    def _check_ack(self, command: str) -> None:
        reply = str(self.res.read()).strip()
        if reply != self.ack:
            raise AdapterError("%s: %r answered %r instead of %r" % (self.name, command, reply, self.ack))

    def write(self, command: str) -> None:
        with self.lock:
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
                self._check_ack(command)
            return reply

    def close(self) -> None:
        with self.lock:
            try:
                self.res.close()
            except Exception:
                pass


def open_backend(name: str, card: Card, address: str, rm: Optional[Any] = None,
                 overrides: Optional[Dict[str, Any]] = None) -> Any:
    """VISA for addresses like GPIB0::24::INSTR or serial://COM3; a vendor driver for zhinst:// or qd://."""
    from .drivers import is_driver_address, open_driver
    if is_driver_address(address):
        return open_driver(name, card, address)
    return VisaBackend(name, card, address, rm, overrides)
