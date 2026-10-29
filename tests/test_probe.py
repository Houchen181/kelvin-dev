"""Finding out what an instrument is before Kelvin has a card for it: what an address on the lab's network answers,
what is plugged into this computer (also without a driver), and which makers' programs and files are here. Only
looking: on the network, a port's greeting, *IDN? where instruments speak SCPI, a web page's title."""
import socket
import threading

import pytest

from labharness import probe
from simlab import build_virtual_lab


class Server:
    """A port on this computer that answers like an instrument, a board or a web page."""

    def __init__(self, answer):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.heard = []
        self.answer = answer
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                greeting = self.answer(None)
                if greeting:
                    conn.sendall(greeting)
                conn.settimeout(2)
                data = conn.recv(4096)
                self.heard.append(data)
                reply = self.answer(data)
                if reply:
                    conn.sendall(reply)
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self.sock.close()


def scpi(data):
    return b"Stanford_Research_Systems,SR830,s/n86813,ver1.07\n" if data and data.startswith(b"*IDN?") else None


def ssh(data):
    return b"SSH-2.0-OpenSSH_8.2p1 Ubuntu-4ubuntu0.5\r\n" if data is None else None


def jupyter(data):
    if data and data.startswith(b"GET / "):
        return (b"HTTP/1.1 302 Found\r\nServer: TornadoServer/6.1\r\nLocation: /login?next=%2Ftree%3F\r\n"
                b"Content-Length: 0\r\n\r\n")
    return None


def pyro(data):
    return b"PYRO\x00\x30\x00\x05handshake failed, reason: invalid data or unsupported protocol version" if data else None


def page(data):
    if data and data.startswith(b"GET / "):
        return (b"HTTP/1.0 200 OK\r\nServer: lighttpd\r\nContent-Type: text/html\r\n\r\n"
                b"<html><head><title>Red Pitaya &amp; friends</title></head></html>")
    return None


@pytest.fixture
def board():
    servers = {name: Server(fn) for name, fn in (("scpi", scpi), ("ssh", ssh), ("jupyter", jupyter), ("pyro", pyro),
                                                  ("page", page))}
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    gone = closed.getsockname()[1]
    closed.close()
    table = [(servers["scpi"].port, "idn", "SCPI over a socket: TCPIP0::{host}::%d::SOCKET" % servers["scpi"].port),
             (servers["ssh"].port, "banner", "SSH"), (servers["jupyter"].port, "http", "Jupyter"),
             (servers["pyro"].port, "http", "a Pyro4 name server"), (servers["page"].port, "http", "web page"),
             (gone, "http", "nothing")]
    yield servers, table, gone
    for s in servers.values():
        s.close()


def test_what_an_address_answers(board):
    servers, table, gone = board
    found = probe.inspect("127.0.0.1", table)
    said = {f["port"]: probe.what_answers(f) for f in found["found"]}
    assert gone not in said and found["looked"] == [p for p, _, _ in table]
    assert "answers *IDN? with 'Stanford_Research_Systems,SR830,s/n86813,ver1.07'" in said[servers["scpi"].port]
    assert "TCPIP0::127.0.0.1::%d::SOCKET" % servers["scpi"].port in said[servers["scpi"].port]
    assert "says 'SSH-2.0-OpenSSH_8.2p1 Ubuntu-4ubuntu0.5'" in said[servers["ssh"].port]
    assert "Jupyter (a PYNQ board's password is xilinx" in said[servers["jupyter"].port]
    assert "a Pyro4 server" in said[servers["pyro"].port]
    assert "web page 'Red Pitaya & friends'" in said[servers["page"].port]
    assert servers["ssh"].heard == [b""] or servers["ssh"].heard == []   # SSH was only listened to


def test_only_local_networks_are_looked_at():
    with pytest.raises(ValueError, match="on the internet"):
        probe.inspect("8.8.8.8")
    assert probe.host_of("TCPIP0::192.168.1.20::5025::SOCKET") == ("192.168.1.20", 5025)
    assert probe.host_of("qick://192.168.3.1:8888/myqick") == ("192.168.3.1", 8888)
    assert probe.host_of("192.168.1.20:7777") == ("192.168.1.20", 7777)
    assert probe.host_of("lakeshore.local") == ("lakeshore.local", None)
    assert probe.host_of("http://10.0.0.5/v1") == ("10.0.0.5", None)


def test_kelvin_inspects_an_address_and_remembers_what_answered(board, tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    from labharness.gate import ScriptedApprover
    servers, table, gone = board
    monkeypatch.setattr(probe, "PORTS", table)
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    said = rt.inspect_address("127.0.0.1")
    resource = "TCPIP0::127.0.0.1::%d::SOCKET" % servers["scpi"].port
    assert "5 of the ports instruments use answer" in said
    assert "→ card srs/sr830" in said and "add_instrument %s" % resource in said
    assert "Closed or silent: %d" % gone in said
    assert resource in rt.last_found                     # try_card and add_instrument may use it without asking
    assert "on the internet" in rt.inspect_address("8.8.8.8")


def test_what_is_plugged_in_even_without_a_driver(monkeypatch):
    raw = [
        {"id": "USB\\ROOT_HUB30\\4&1", "vid": None, "pid": None, "description": "USB Root Hub (USB 3.0)",
         "service": "USBHUB3", "class": "USB", "maker": "(Standard USB HUBs)", "name": "", "reported": "", "problem": 0},
        {"id": "USB\\VID_0403&PID_6001\\A600XYZ", "vid": 0x0403, "pid": 0x6001, "description": "USB Serial Converter",
         "service": "FTDIBUS", "class": "USB", "maker": "FTDI", "name": "", "reported": "ANC350", "problem": 0},
        {"id": "USB\\VID_1313&PID_8078\\P0001", "vid": 0x1313, "pid": 0x8078, "description": "PM100D",
         "service": "", "class": "", "maker": "", "name": "", "reported": "PM100D", "problem": 28},
        {"id": "USB\\VID_0525&PID_A4A2&MI_01\\6&2b", "vid": 0x0525, "pid": 0xA4A2,
         "description": "CDC Data", "service": "", "class": "", "maker": "", "name": "", "reported": "", "problem": 0},
        {"id": "USB\\VID_0525&PID_A4A2\\5&3a", "vid": 0x0525, "pid": 0xA4A2, "description": "USB Composite Device",
         "service": "usbccgp", "class": "USB", "maker": "(Standard USB Host Controller)", "name": "",
         "reported": "RNDIS/Ethernet Gadget", "problem": 0},
        {"id": "USB\\VID_0525&PID_A4A2&MI_00\\6&2a", "vid": 0x0525, "pid": 0xA4A2,
         "description": "Remote NDIS Compatible Device", "service": "usb_rndis", "class": "Net", "maker": "Microsoft",
         "name": "", "reported": "", "problem": 0},
        {"id": "USB\\VID_046D&PID_C52B\\5&1", "vid": 0x046D, "pid": 0xC52B, "description": "USB Composite Device",
         "service": "usbccgp", "class": "USB", "maker": "", "name": "", "reported": "USB Receiver", "problem": 0},
        {"id": "USB\\VID_046D&PID_C52B&MI_00\\6&1", "vid": 0x046D, "pid": 0xC52B, "description": "USB Input Device",
         "service": "HidUsb", "class": "HIDClass", "maker": "", "name": "", "reported": "", "problem": 0},
    ]
    monkeypatch.setattr(probe, "WIN", True)
    monkeypatch.setattr(probe, "_windows_usb", lambda: raw)
    devices = probe.usb_devices()
    assert len(devices) == 4                                         # the hub left out, interfaces folded in
    ports = [{"port": "COM5", "description": "USB Serial Port", "vid": 0x0403, "pid": 0x6001, "serial": "A600XYZ",
              "maker": "FTDI", "product": ""}]
    lines = [probe.usb_line(d, ports) for d in devices if not probe.ordinary(d)]
    assert lines[0].startswith("ANC350 / USB Serial Converter (USB 0403:6001, FTDI, serial A600XYZ); serial port COM5")
    assert "PM100D (USB 1313:8078, Thorlabs, serial P0001); no driver installed" in lines[1]
    assert "RNDIS/Ethernet Gadget" in lines[2] and "a board running Linux, as a USB network" in lines[2]
    assert "usb_rndis" in lines[2]
    assert [d["names"][0] for d in devices if probe.ordinary(d)] == ["USB Receiver"]


def test_kelvin_says_what_is_plugged_in(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    from labharness.gate import ScriptedApprover
    monkeypatch.setattr(probe, "usb_devices", lambda: [
        {"vid": 0x0525, "pid": 0xA4A2, "serial": "", "names": ["RNDIS/Ethernet Gadget"], "maker": "",
         "drivers": ["usbccgp", "usb_rndis"], "classes": ["Net"], "problem": 0}])
    monkeypatch.setattr(probe, "serial_ports", lambda: [{"port": "COM1", "description": "Communications Port",
                                                        "vid": None, "pid": None, "serial": "", "maker": "",
                                                        "product": ""}])
    monkeypatch.setattr(probe, "network_adapters", lambda: [
        {"name": "Ethernet 3", "address": "192.168.3.100", "network": "192.168.3.0/24", "link_local": False}])
    said = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data").connected_devices()
    assert "RNDIS/Ethernet Gadget (USB 0525:a4a2, Linux USB gadget)" in said
    assert "Serial ports (not USB): COM1 (Communications Port)" in said
    assert "Ethernet 3 192.168.3.0/24 (this computer 192.168.3.100)" in said


def test_makers_programs_and_files(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "WIN", True)
    monkeypatch.setattr(probe, "windows_programs", lambda: [
        {"name": "NI-DAQmx Runtime 2024 Q1", "publisher": "National Instruments", "version": "24.0", "location": ""},
        {"name": "LightField", "publisher": "Teledyne Princeton Instruments", "version": "6.17",
         "location": "C:\\Program Files\\Princeton Instruments\\LightField"},
        {"name": "Mozilla Firefox", "publisher": "Mozilla", "version": "131", "location": ""}])
    assert [p["name"] for p in probe.programs()] == ["LightField", "NI-DAQmx Runtime 2024 Q1"]
    assert [p["name"] for p in probe.programs("firefox")] == ["Mozilla Firefox"]
    sdk = tmp_path / "attocube" / "attoDRY" / "Python"
    sdk.mkdir(parents=True)
    (sdk / "attoDRYLib.dll").write_bytes(b"MZ" * 10)
    (sdk / "attodry_example.py").write_bytes(b"import attoDRYLib\n")
    (tmp_path / "other").mkdir()
    hits = probe.find_files("attodry", [tmp_path])
    assert hits[0] == (str(tmp_path / "attocube" / "attoDRY"), "folder")
    assert (str(sdk / "attodry_example.py"), ".py, 18 B") in hits and len(hits) == 3
    assert probe.find_files("nothing-like-this", [tmp_path]) == []


class SerialDevice:
    """An instrument on a serial line: it answers only at its own speed and framing; at any other it hears noise."""

    def __init__(self, baud, parity, bits, reply=b"", streams=b"", echo=False):
        self.baud, self.parity, self.bits, self.reply, self.streams, self.echo = baud, parity, bits, reply, streams, echo
        self.heard = []

    def opener(self, port, baud, parity, bits, timeout):
        device = self

        class Port:
            pending = []

            def read(self, n):
                if device.streams and (baud, parity, bits) == (device.baud, "none", 8):
                    return device.streams
                return b"\xfe\x81\x00" if device.streams else b""

            def reset_input_buffer(self):
                pass

            def write(self, data):
                device.heard.append((baud, parity, bits, data))
                if (baud, parity, bits) == (device.baud, device.parity, device.bits):
                    self.pending = ([data] if device.echo else []) + [device.reply]
                else:
                    self.pending = [b"\x8f\xf0\x13"]

            def read_until(self, end, n):
                return self.pending.pop(0) if self.pending else b""

            def close(self):
                pass
        return Port()


def test_a_serial_instrument_found_at_its_own_speed_and_framing(monkeypatch, tmp_path):
    device = SerialDevice(57600, "odd", 7, b"LSCI,MODEL336,1234567,1.0\r\n", echo=True)
    said = probe.inspect_serial("COM7", opener=device.opener, listen=0.01)
    assert said["streams"] == [] and said["answer"]["baud"] == 57600 and said["answer"]["parity"] == "odd"
    assert said["answer"]["echo"] is True and said["answer"]["reply"] == "LSCI,MODEL336,1234567,1.0"
    assert all(data == b"*IDN?\r\n" for *_, data in device.heard)          # only the identity query, ever
    gauge = SerialDevice(9600, "none", 8, streams=b"0, 1.2300E-03\r\n")
    assert probe.inspect_serial("COM8", opener=gauge.opener, listen=0.01)["streams"] == [(9600, "0, 1.2300E-03")]

    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    from labharness.gate import ScriptedApprover
    monkeypatch.setattr(probe, "_open_serial", device.opener)
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    text = rt.inspect_serial("COM7")
    assert "COM7 answers *IDN? at 57600 baud, 7O1 (it echoes each command)" in text
    assert "card lakeshore/336" in text and "add_instrument serial://COM7" in text
    assert "serial://COM7" in rt.last_found
    from labharness.adapters import resource_name
    assert rt.found_info[resource_name("serial://COM7")]["serial"]["baud_rate"] == 57600
    assert "only an identity query" in rt.inspect_serial("COM7", query="RST")
    assert "is not a serial port" in rt.inspect_serial("GPIB0::5::INSTR")
