"""Instruments that speak Modbus: compressors (Cryomech's), temperature controllers (Eurotherm, Omega, Watlow),
chillers, flow controllers, vacuum controllers and PLCs. Kelvin speaks Modbus itself: no package to install.

    modbus://192.168.1.50?unit=1                      Modbus TCP (port 502 unless the address names another)
    modbus://COM5?unit=1&baud=9600&parity=N&stop=1    Modbus RTU on a serial port (RS-485 or RS-232)

A card's commands name the register, its type and any scaling, as the manual's register map gives them:
    get: "READ 30008 float32"         input register 30008 in the usual numbering (0xxxx coils, 1xxxx discrete
                                      inputs, 3xxxx input registers, 4xxxx holding registers), a float in two
    get: "READ HR 6 int16 *0.1"       holding register 6 counted from 0, read in tenths
    set: "WRITE 40001 uint16 {value}"
    set: "WRITE COIL 3 {value}"       a coil, on or off
Types: uint16 and int16 (one register), uint32, int32 and float32 (two), float64 (four); add "swapped" after the
type when the device sends the low word first. Coils and discrete inputs are on/off."""
from __future__ import annotations

import socket
import struct
import threading
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple

from .adapters import AdapterError
from .cards import Card
from .drivers import DriverBackend

TYPES = {"uint16": ("H", 1), "int16": ("h", 1), "uint32": ("I", 2), "int32": ("i", 2), "float32": ("f", 2),
         "float64": ("d", 4)}
TABLES = {"COIL": 1, "DI": 2, "HR": 3, "IR": 4}   # the read function of each
EXCEPTIONS = {1: "illegal function", 2: "illegal data address", 3: "illegal data value", 4: "device failure",
              5: "acknowledged, still working", 6: "device busy", 10: "gateway path unavailable",
              11: "gateway target did not respond"}


def crc16(frame: bytes) -> bytes:
    """Modbus RTU's CRC, low byte first."""
    crc = 0xFFFF
    for b in frame:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)


def parse(command: str) -> Dict[str, Any]:
    """READ|WRITE [COIL|DI|IR|HR] <number> [type] [swapped] [*scale] [value]."""
    words = command.split()
    if not words or words[0].upper() not in ("READ", "WRITE"):
        raise AdapterError("not a Modbus command: %r (READ <register> [type] or WRITE <register> [type] <value>)"
                           % command)
    out: Dict[str, Any] = {"verb": words[0].upper(), "type": "uint16", "swapped": False, "scale": 1.0, "value": None}
    rest = words[1:]
    table = rest[0].upper() if rest and rest[0].upper() in TABLES else None
    rest = rest[1:] if table else rest
    if not rest or not rest[0].isdigit():
        raise AdapterError("%r: which register? (30008, or HR 6 counted from 0)" % command)
    number, rest = int(rest[0]), rest[1:]
    if table is None:   # the usual numbering: the leading digit is the table, the register counted from 1
        lead, offset = divmod(number, 100000 if number >= 100000 else 10000)   # 40001, or 400001
        table = {0: "COIL", 1: "DI", 3: "IR", 4: "HR"}.get(lead)
        if table is None or offset < 1:
            raise AdapterError("%r: %d isn't in the usual numbering; say COIL, DI, IR or HR with the register "
                               "counted from 0" % (command, number))
        number = offset - 1
    out["table"], out["register"] = table, number
    if table in ("COIL", "DI"):
        out["type"] = "bool"
    for w in rest:
        low = w.lower()
        if low in TYPES:
            out["type"] = low
        elif low == "swapped":
            out["swapped"] = True
        elif w.startswith("*"):
            out["scale"] = float(w[1:])
        elif out["verb"] == "WRITE" and out["value"] is None:
            out["value"] = w
        else:
            raise AdapterError("%r: what is %r? (a type such as float32, swapped, *0.1 or the value)" % (command, w))
    if out["verb"] == "WRITE" and (out["value"] is None or table in ("DI", "IR")):
        raise AdapterError("%r: only coils and holding registers are written, with a value" % command)
    return out


def _words(kind: str, value: float, swapped: bool) -> List[int]:
    fmt, count = TYPES[kind]
    raw = struct.pack(">" + fmt, int(round(value)) if fmt in "HhIi" else value)
    words = list(struct.unpack(">%dH" % count, raw))
    return words[::-1] if swapped else words


def _value(kind: str, words: List[int], swapped: bool) -> float:
    fmt, count = TYPES[kind]
    words = words[::-1] if swapped else words
    return struct.unpack(">" + fmt, struct.pack(">%dH" % count, *words))[0]


class ModbusInstrument(DriverBackend):
    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        url = urllib.parse.urlparse(address)
        q = dict(urllib.parse.parse_qsl(url.query))
        self.unit = int(q.get("unit", 1))
        self.timeout = float(card.connection.get("timeout_ms", 2000)) / 1000.0
        self.tid = 0
        self.io_lock = threading.Lock()
        target = (url.netloc + url.path).strip("/")
        self.serial = None
        self.sock = None
        if isinstance(module, dict) and module.get("transport"):
            self.transport = module["transport"]   # tests: a function from a request frame to the reply frame
            self.rtu = bool(module.get("rtu"))
            return
        self.transport = None
        self.rtu = target.upper().startswith("COM") or target.startswith("/dev/")
        try:
            if self.rtu:
                import serial   # pyserial, which Kelvin brings
                parity = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}[
                    q.get("parity", "N").upper()[:1]]
                self.serial = serial.Serial(target, int(q.get("baud", 9600)), parity=parity,
                                            stopbits=int(q.get("stop", 1)), timeout=self.timeout)
            else:
                host, _, port = target.partition(":")
                self.sock = socket.create_connection((host, int(port or 502)), timeout=self.timeout)
        except (OSError, ValueError, KeyError, ImportError) as e:
            raise AdapterError("%s: could not reach %s by Modbus: %s" % (name, target, e)) from e

    # -------------------------------------------------------------- frames

    def _exchange(self, pdu: bytes) -> bytes:
        """One request and its reply's PDU, raising the device's own exception in words."""
        with self.io_lock:
            if self.transport is not None:
                reply = self.transport(self._frame(pdu))
            elif self.rtu:
                self.serial.reset_input_buffer()
                self.serial.write(self._frame(pdu))
                reply = self._read_rtu(pdu[0])
            else:
                self.sock.sendall(self._frame(pdu))
                reply = self._read_tcp()
        body = self._unframe(reply)
        if body[0] & 0x80:
            code = body[1] if len(body) > 1 else 0
            raise AdapterError("%s: the device refused it: Modbus exception %d (%s)" % (
                self.name, code, EXCEPTIONS.get(code, "unknown")))
        if body[0] != pdu[0]:
            raise AdapterError("%s: the device answered another request (function %d)" % (self.name, body[0]))
        return body

    def _frame(self, pdu: bytes) -> bytes:
        if self.rtu:
            frame = bytes([self.unit]) + pdu
            return frame + crc16(frame)
        self.tid = (self.tid + 1) & 0xFFFF
        return struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, self.unit) + pdu

    def _unframe(self, reply: bytes) -> bytes:
        if self.rtu:
            if len(reply) < 5 or crc16(reply[:-2]) != reply[-2:]:
                raise AdapterError("%s: no valid reply (check the unit number, baud rate and wiring)" % self.name)
            if reply[0] != self.unit:
                raise AdapterError("%s: unit %d answered, not %d" % (self.name, reply[0], self.unit))
            return reply[1:-2]
        if len(reply) < 9:
            raise AdapterError("%s: no reply (check the address and the unit number)" % self.name)
        tid, proto, length, unit = struct.unpack(">HHHB", reply[:7])
        if tid != self.tid or proto != 0:
            raise AdapterError("%s: a reply to another request" % self.name)
        return reply[7:6 + length]

    def _recv(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            try:
                chunk = self.sock.recv(n - len(data))
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
        return data

    def _read_tcp(self) -> bytes:
        head = self._recv(7)
        if len(head) < 7:
            return head
        return head + self._recv(struct.unpack(">H", head[4:6])[0] - 1)

    def _read_rtu(self, function: int) -> bytes:
        head = self.serial.read(3)
        if len(head) < 3:
            return head
        if head[1] & 0x80:
            return head + self.serial.read(2)
        if function in (1, 2, 3, 4):
            return head + self.serial.read(head[2] + 2)
        return head + self.serial.read(5)   # writes echo address and value

    # -------------------------------------------------------------- reading and writing

    def _read(self, c: Dict[str, Any]) -> Any:
        function = TABLES[c["table"]]
        count = 1 if c["type"] == "bool" else TYPES[c["type"]][1]
        body = self._exchange(struct.pack(">BHH", function, c["register"], count))
        data = body[2:2 + body[1]] if len(body) > 1 else b""
        if c["type"] == "bool":
            if not data:
                raise AdapterError("%s: an empty reply" % self.name)
            return bool(data[0] & 1)
        if len(data) < 2 * count:
            raise AdapterError("%s: %d bytes where %d were due" % (self.name, len(data), 2 * count))
        value = _value(c["type"], list(struct.unpack(">%dH" % count, data[:2 * count])), c["swapped"])
        return value * c["scale"] if c["scale"] != 1.0 else value

    def _write(self, c: Dict[str, Any]) -> None:
        text = str(c["value"]).strip().lower()
        if c["table"] == "COIL":
            on = text in ("1", "1.0", "on", "true", "yes")
            self._exchange(struct.pack(">BHH", 5, c["register"], 0xFF00 if on else 0))
            return
        value = float(text) / c["scale"]
        words = _words(c["type"], value, c["swapped"])
        if len(words) == 1:
            self._exchange(struct.pack(">BHH", 6, c["register"], words[0]))
        else:
            self._exchange(struct.pack(">BHHB", 16, c["register"], len(words), 2 * len(words))
                           + struct.pack(">%dH" % len(words), *words))

    def write(self, command: str) -> None:
        c = parse(command)
        if c["verb"] != "WRITE":
            raise AdapterError("%s: %r reads, it does not write" % (self.name, command))
        with self.lock:
            self._write(c)

    def query(self, command: str) -> str:
        if command.strip().upper() in ("*IDN?", self.card.idn_query.strip().upper()) and \
                not command.strip().upper().startswith("READ"):
            return self.identity()
        c = parse(command)
        if c["verb"] != "READ":
            raise AdapterError("%s: %r writes, it does not read" % (self.name, command))
        with self.lock:
            value = self._read(c)
        if isinstance(value, bool):
            return "1" if value else "0"
        return "%.10g" % value if isinstance(value, float) else str(value)

    def identity(self) -> str:
        return "%s,%s,%s unit %d" % (self.card.vendor or "Modbus", self.card.model, self.address.split("?")[0],
                                     self.unit)

    def close(self) -> None:
        for handle in (self.sock, self.serial):
            try:
                if handle is not None:
                    handle.close()
            except Exception:
                pass
