"""Instruments that speak Modbus (compressors, temperature controllers, chillers, flow and vacuum controllers): Kelvin
reads and writes their registers itself, over TCP or a serial line, as their register maps describe them."""
import socket
import struct
import threading

import pytest

from labharness import EventLog, SafetyGate, ScriptedApprover, drivers
from labharness.adapters import AdapterError
from labharness.modbus import crc16, parse
from labharness.homelab import build_lab, lab_registry

COMPRESSOR = """card: 1
id: acme/cp-1
model: ACME CP-1 helium compressor
vendor: ACME
kind: compressor
status: lab-verified
parameters:
  running: { label: Compressor running, kind: drive, type: bool, set: "WRITE COIL 0 {value}", get: "READ 00001",
             safe: off }
  water_in: { label: Cooling water in, kind: measure, unit: C, get: "READ 30001 float32" }
  helium: { label: Helium return pressure, kind: measure, unit: psi, get: "READ IR 2 int16 *0.1" }
  hours: { label: Hours run, kind: measure, unit: h, get: "READ 30004 uint32 swapped" }
  setpoint: { label: Water set point, kind: drive, unit: C, set: "WRITE 40001 int16 *0.1 {value}",
              get: "READ 40001 int16 *0.1", limits: [5, 30], safe: 20, ramp: none }
"""


class Device:
    """A Modbus device's registers, answering requests as the Modbus specification says."""

    def __init__(self):
        water = struct.unpack(">2H", struct.pack(">f", 18.5))
        hours = struct.unpack(">2H", struct.pack(">I", 123456))
        self.input = {0: water[0], 1: water[1], 2: 2873, 3: hours[1], 4: hours[0]}   # hours: low word first
        self.holding = {0: 200}
        self.coils = {0: False}

    def answer(self, pdu):
        f = pdu[0]
        if f in (3, 4):
            start, count = struct.unpack(">HH", pdu[1:5])
            table = self.holding if f == 3 else self.input
            if any(r not in table for r in range(start, start + count)):
                return bytes([f | 0x80, 2])
            return bytes([f, 2 * count]) + struct.pack(">%dH" % count, *[table[r] for r in range(start, start + count)])
        if f in (1, 2):
            start, count = struct.unpack(">HH", pdu[1:5])
            return bytes([f, 1, int(self.coils.get(start, False))])
        if f == 5:
            reg, value = struct.unpack(">HH", pdu[1:5])
            self.coils[reg] = value == 0xFF00
            return pdu[:5]
        if f == 6:
            reg, value = struct.unpack(">HH", pdu[1:5])
            self.holding[reg] = value
            return pdu[:5]
        if f == 16:
            reg, count, _ = struct.unpack(">HHB", pdu[1:6])
            for i, v in enumerate(struct.unpack(">%dH" % count, pdu[6:6 + 2 * count])):
                self.holding[reg + i] = v
            return pdu[:5]
        return bytes([f | 0x80, 1])


class TcpServer:
    def __init__(self, device):
        self.device, self.sock = device, socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(2)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            while True:
                head = conn.recv(7)
                if len(head) < 7:
                    break
                tid, _, length, unit = struct.unpack(">HHHB", head)
                pdu = conn.recv(length - 1)
                reply = self.device.answer(pdu)
                conn.sendall(struct.pack(">HHHB", tid, 0, len(reply) + 1, unit) + reply)
            conn.close()


@pytest.fixture
def compressor(tmp_path):
    device = Device()
    server = TcpServer(device)
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "cp1.yaml").write_text(COMPRESSOR, encoding="utf-8")
    lab = build_lab('lab: Cryostat\ninstruments:\n  cp: { card: acme/cp-1, address: "modbus://127.0.0.1:%d?unit=1" }\n'
                    % server.port, registry=lab_registry(tmp_path))
    yield lab, device
    lab.close()
    server.sock.close()


def test_a_compressor_read_and_set_over_modbus_tcp(compressor, tmp_path):
    lab, device = compressor
    assert lab.read("cp", "water_in") == pytest.approx(18.5)            # a float in two registers
    assert lab.read("cp", "helium") == pytest.approx(287.3)             # tenths of a psi
    assert lab.read("cp", "hours") == 123456                            # low word first
    assert lab.read("cp", "running") is False and lab.read("cp", "setpoint") == pytest.approx(20.0)
    gate = SafetyGate(lab, ScriptedApprover([True, True]), EventLog(tmp_path / "events.jsonl", lab.clock))
    assert gate.set("cp", "setpoint", 22.5).ok and device.holding[0] == 225
    assert gate.set("cp", "running", True).ok and device.coils[0] is True
    assert gate.set("cp", "setpoint", 40).action == "refused"           # the card's 30 C
    with pytest.raises(AdapterError, match=r"Modbus exception 2 \(illegal data address\)"):
        lab.instruments["cp"].query_raw("READ HR 50")


def test_modbus_rtu_frames_and_the_usual_register_numbers(monkeypatch, tmp_path):
    device = Device()
    sent = []

    def line(frame):   # a serial line to the device: unit, PDU, CRC
        sent.append(frame)
        assert crc16(frame[:-2]) == frame[-2:] and frame[0] == 7
        reply = bytes([7]) + device.answer(frame[1:-2])
        return reply + crc16(reply)
    monkeypatch.setitem(drivers.MODULES, "modbus", {"transport": line, "rtu": True})
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "cp1.yaml").write_text(COMPRESSOR, encoding="utf-8")
    lab = build_lab('lab: Cryostat\ninstruments:\n  cp: { card: acme/cp-1, address: "modbus://COM9?unit=7&baud=9600" }\n',
                    registry=lab_registry(tmp_path))
    try:
        assert lab.read("cp", "water_in") == pytest.approx(18.5)
        assert sent[-1][:6] == bytes([7, 4, 0, 0, 0, 2])               # unit 7, read input registers 0-1
    finally:
        lab.close()
    assert crc16(bytes([1, 3, 0, 0, 0, 10])) == bytes([0xC5, 0xCD])     # the specification's example
    assert parse("READ 30008 float32")["register"] == 7 and parse("READ 400010")["table"] == "HR"
    assert parse("READ 10003")["type"] == "bool" and parse("WRITE HR 6 int16 *0.1 22.5")["scale"] == 0.1
    for bad in ("READ 20001", "WRITE IR 3 5", "WRITE 40001 uint16", "READ HR x", "SET 40001 1"):
        with pytest.raises(AdapterError):
            parse(bad)
