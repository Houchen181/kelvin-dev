"""A maker's-driver link that broke comes back by itself, a lost one says so, and a reply never stays one behind
(review of 0.3.45, 4.2, 4.4 and 4.5).

- 4.2: a TOPTICA, M Squared, ANC300, Modbus RTU, MultiVu or lib:// link that broke was never remade: every read failed
  until Kelvin restarted, and the Lab panel's switch, off then on, handed back the same dead connection.
- 4.4: what those links raised when they broke ("closed the connection", "no reply", "actively refused") wasn't
  counted as a lost instrument: no lost alarm, and a watch said "check the card".
- 4.5: after one reply that came late, every TOPTICA and ANC300 reply was the one before's (temp-act read as the
  current, axis 2's step voltage as axis 1's offset): values the gate ramps from.
"""
import json
import socket

import pytest

from labharness import builtin_registry
from labharness.adapters import LinkLost, NoAnswer, lost_kind
from labharness.drivers import MODULES
from labharness.homelab import build_lab
from labharness.instrument import InstrumentLost


@pytest.fixture(autouse=True)
def no_fakes_left():
    yield
    MODULES.clear()


def raised(fn):
    try:
        fn()
    except Exception as e:   # noqa: BLE001 - what it raised is what is checked
        return e
    raise AssertionError("it didn't raise")


# ------------------------------------------------------------------ TOPTICA DLC pro: its command line (decop.py)

class Dlc:
    """A DLC pro's command line: every connection gets the welcome; restart() ends every connection made before it;
    a parameter in `slow` answers only when the next command on that connection is sent (a reply that came late)."""

    def __init__(self):
        self.params = {"laser1:dl:cc:current-act": "85.1", "laser1:dl:tc:temp-act": "20.01", "emission": "#t",
                       "serial-number": '"DLC-001"'}
        self.slow, self.made, self.off = set(), [], False

    def create_connection(self, address, timeout):
        if self.off:
            raise ConnectionRefusedError(10061, "No connection could be made because the target machine actively "
                                                "refused it")
        conn = DlcConnection(self)
        self.made.append(conn)
        return conn

    def restart(self):
        for conn in self.made:
            conn.closed = True


class DlcConnection:
    def __init__(self, dlc):
        self.dlc, self.closed = dlc, False
        self.out, self.late = b"DeCoP command line\r\n> ", b""

    def sendall(self, data):
        if self.closed:
            raise ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")
        self.out += self.late   # what came too late arrives now, before this command's reply
        self.late = b""
        name = data.decode().strip()[len("(param-ref '"):-1]
        reply = (self.dlc.params.get(name, "Error: -1 unknown parameter") + "\r\n> ").encode()
        if name in self.dlc.slow:
            self.dlc.slow.discard(name)
            self.late = reply
        else:
            self.out += reply

    def recv(self, n):
        if self.closed:
            return b""
        chunk, self.out = self.out, b""
        if not chunk:
            raise socket.timeout("timed out")
        return chunk

    def close(self):
        self.closed = True


DLC_LAB = """
lab: Cooling lasers
instruments:
  dl: { card: toptica/dlc-pro, address: "toptica://10.0.0.9" }
"""


def test_a_toptica_that_restarted_is_lost_then_reached_again_by_itself():
    dlc = MODULES["toptica"] = Dlc()
    lab = build_lab(DLC_LAB)
    assert lab.read("dl", "emission") is True and len(dlc.made) == 1
    dlc.restart()
    e = raised(lambda: lab.read("dl", "emission"))
    assert isinstance(e, InstrumentLost) and isinstance(e.__cause__, LinkLost) and lost_kind(e.__cause__) == "gone"
    assert lab.instruments["dl"].health.gone                                  # its lost alarm goes up
    assert lab.read("dl", "emission") is True and len(dlc.made) == 2          # the next exchange connects again
    lab.switch("dl", False)                                                   # off: its connection closed (it sends
    assert dlc.made[-1].closed and "dl" not in lab._parked                    # the laser nothing), not set aside
    lab.switch("dl", True)
    assert len(dlc.made) == 3 and lab.read("dl", "emission") is True          # on: a fresh one


def test_a_toptica_reply_that_came_late_is_never_taken_for_the_next_ones():
    dlc = Dlc()
    from labharness.decop import DeCoP
    dl = DeCoP("dl", builtin_registry().get("toptica/dlc-pro"), "toptica://10.0.0.9", module=dlc)
    dlc.slow.add("laser1:dl:cc:current-act")
    e = raised(lambda: dl.query("GET laser1:dl:cc:current-act"))
    assert isinstance(e, NoAnswer) and lost_kind(e) == "silent"
    assert dl.query("GET laser1:dl:tc:temp-act") == "20.01"                  # not 85.1, the current's late reply
    assert dl.query("GET emission") == "#t"
    assert dl.query("GET laser1:dl:tc:temp-act") == "20.01"
    assert len(dlc.made) == 2                                                 # the late one's connection was let go


def test_a_toptica_that_cant_be_reached_has_no_identity_made_up():
    dlc = Dlc()
    from labharness.decop import DeCoP
    dl = DeCoP("dl", builtin_registry().get("toptica/dlc-pro"), "toptica://10.0.0.9", module=dlc)
    assert "DLC-001" in dl.query("*IDN?")
    dlc.restart()
    dlc.off = True
    assert isinstance(raised(lambda: dl.query("*IDN?")), LinkLost)
    assert isinstance(raised(lambda: dl.query("*IDN?")), LinkLost)            # still not there: said again
    dlc.off = False
    assert "DLC-001" in dl.query("*IDN?")                                     # back: reached again by itself


# ------------------------------------------------------------------ M Squared: JSON over TCP (m2.py)

class Laser:
    """An M Squared laser's remote interface: a fresh link per connection, start_link first; restart() ends them."""

    def __init__(self):
        self.made, self.silent = [], False

    def create_connection(self, address, timeout):
        conn = LaserLink(self)
        self.made.append(conn)
        return conn

    def restart(self):
        for conn in self.made:
            conn.closed = True


class LaserLink:
    def __init__(self, laser):
        self.laser, self.closed, self.out, self.linked = laser, False, b"", False

    def getsockname(self):
        return ("10.0.0.2", 51234)

    def sendall(self, data):
        if self.closed:
            raise ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")
        msg = json.loads(data.decode("ascii"))["message"]
        if self.laser.silent and msg["op"] != "start_link":
            return
        reply = {"status": "ok"} if msg["op"] == "start_link" else {"status": [0], "wavelength": [780.0]}
        self.linked = self.linked or msg["op"] == "start_link"
        self.out += json.dumps({"message": {"transmission_id": msg["transmission_id"], "op": msg["op"] + "_reply",
                                            "parameters": reply}}).encode()

    def recv(self, n):
        if self.closed:
            return b""
        chunk, self.out = self.out, b""
        if not chunk:
            raise socket.timeout("timed out")
        return chunk

    def close(self):
        self.closed = True


def test_an_m2_laser_that_restarted_is_lost_then_linked_again_by_itself():
    laser = Laser()
    from labharness.m2 import M2Laser
    ti = M2Laser("ti", builtin_registry().get("m2/solstis"), "m2://10.0.0.8:9360", module=laser)
    assert ti.query("GET get_status#wavelength") == "780"
    laser.restart()
    e = raised(lambda: ti.query("GET get_status#wavelength"))
    assert isinstance(e, LinkLost) and lost_kind(e) == "gone"
    assert ti.query("GET get_status#wavelength") == "780"
    assert len(laser.made) == 2 and laser.made[-1].linked                     # linked again, start_link and all
    laser.silent = True
    e = raised(lambda: ti.query("GET get_status#wavelength"))
    assert isinstance(e, NoAnswer) and lost_kind(e) == "silent"
    assert len(laser.made) == 2 and not laser.made[-1].closed                 # a late reply has its id: the link stays
    assert M2Laser.quiet_close


# ------------------------------------------------------------------ attocube ANC300: its command line (positioners.py)

class Anc:
    """An ANC300 on the network: password, then its command line, each reply in its own segment, ending OK; one in
    `slow` arrives only as the next command is sent (it came late). restart() ends every connection made before."""

    def __init__(self):
        self.values = {"getv 1": "voltage = 30.000000 V", "geta 1": "voltage = 12.500000 V",
                       "geta 2": "voltage = 0.000000 V", "getv 2": "voltage = 45.000000 V",
                       "ver": "attocube ANC300 controller version 1.1.0-1304 2013-10-17 08:16"}
        self.slow, self.made = set(), []

    def create_connection(self, address, timeout=None):
        conn = AncConnection(self)
        self.made.append(conn)
        return conn

    def restart(self):
        for conn in self.made:
            conn.closed = True


class AncConnection:
    def __init__(self, anc):
        self.anc, self.closed, self.segments, self.late, self.authorized = anc, False, [], b"", False

    def settimeout(self, t):
        pass

    def sendall(self, data):
        if self.closed:
            raise ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")
        if self.late:   # the late reply arrives now, before this command's
            self.segments.append(self.late)
            self.late = b""
        line = data.decode().strip()
        if not self.authorized:
            self.authorized = True
            self.segments.append(b"Authorization success\r\nOK\r\n")
            return
        reply = (self.anc.values.get(line, "") + "\r\nOK\r\n").encode()
        if line in self.anc.slow:
            self.anc.slow.discard(line)
            self.late = reply
        else:
            self.segments.append(reply)

    def recv(self, n):
        if self.closed:
            return b""
        if self.segments:
            return self.segments.pop(0)
        raise socket.timeout("timed out")

    def close(self):
        self.closed = True


@pytest.fixture
def anc(monkeypatch):
    from labharness import positioners
    fake = Anc()
    monkeypatch.setattr(positioners.socket, "create_connection", fake.create_connection)
    return fake


def test_an_anc300_reply_that_came_late_is_never_taken_for_the_next_ones(anc):
    from labharness.positioners import Link
    link = Link("10.0.0.10", timeout=0.3)
    anc.slow.add("geta 1")
    e = raised(lambda: link.command("geta 1"))
    assert isinstance(e, NoAnswer) and lost_kind(e) == "silent"
    assert link.command("getv 2") == ["voltage = 45.000000 V"]               # axis 2's step voltage, not axis 1's
    assert link.command("geta 2") == ["voltage = 0.000000 V"]                # offset
    assert link.command("getv 1") == ["voltage = 30.000000 V"]
    assert len(anc.made) == 1                                                # put back in step on the same link


def test_an_anc300_that_restarted_is_lost_then_reached_again_by_itself(anc):
    from labharness.positioners import Link
    link = Link("10.0.0.11", timeout=0.3)
    assert link.command("getv 1") == ["voltage = 30.000000 V"]
    anc.restart()
    e = raised(lambda: link.command("getv 1"))
    assert isinstance(e, LinkLost) and lost_kind(e) == "gone"
    assert link.command("getv 1") == ["voltage = 30.000000 V"] and len(anc.made) == 2   # password and all again


def test_an_anc300_axis_switched_off_and_on_gets_a_link_that_works(anc):
    lab = build_lab("""
lab: Positioners
instruments:
  x: { card: attocube/anc300, address: "anc300://10.0.0.12", vars: { axis: 1 } }
""")
    try:
        assert lab.read("x", "voltage") == pytest.approx(30.0)
        anc.restart()
        assert isinstance(raised(lambda: lab.read("x", "voltage")), InstrumentLost)
        lab.switch("x", False)                                                # its share of the link let go of
        assert "x" not in lab._parked
        lab.switch("x", True)
        assert lab.read("x", "voltage") == pytest.approx(30.0) and len(anc.made) == 2
    finally:
        lab.close()


# ------------------------------------------------------------------ Modbus, JSON-RPC and web APIs

def compressor_card():
    return type("C", (), {"connection": {"timeout_ms": 300}, "idn_query": "*IDN?", "model": "CP2800",
                          "vendor": "Cryomech", "on_connect": []})()


class Adapter:
    """A USB-serial adapter: pulled, its port fails (as pyserial says on Windows) and can't be opened; plugged back,
    a new open works. A Modbus RTU device answers every read with 1.5 as a float32."""

    def __init__(self):
        import serial
        self.serial, self.pulled, self.opened = serial, False, 0
        adapter = self

        class Port:
            def __init__(self, port, baud=9600, **kw):
                if adapter.pulled:
                    raise serial.SerialException("could not open port %r: FileNotFoundError(2, 'The system cannot "
                                                 "find the file specified.', None, 2)" % port)
                adapter.opened += 1
                self.reply = b""

            def reset_input_buffer(self):
                pass

            def write(self, frame):
                if adapter.pulled:
                    raise serial.SerialException("WriteFile failed (PermissionError(13, 'The device does not "
                                                 "recognize the command.', None, 22))")
                import struct
                from labharness.modbus import crc16
                head = bytes([frame[0], frame[1], 4]) + struct.pack(">f", 1.5)
                self.reply = head + crc16(head)

            def read(self, n):
                chunk, self.reply = self.reply[:n], self.reply[n:]
                return chunk

            def close(self):
                pass

        self.Port = Port


def test_a_modbus_rtu_device_whose_adapter_was_replugged_is_reached_again(monkeypatch):
    from labharness.modbus import ModbusInstrument
    adapter = Adapter()
    monkeypatch.setattr(adapter.serial, "Serial", adapter.Port)
    comp = ModbusInstrument("comp", compressor_card(), "modbus://COM7?unit=1")
    assert comp.query("READ 30008 float32") == "1.5"
    adapter.pulled = True
    e = raised(lambda: comp.query("READ 30008 float32"))
    assert isinstance(e, LinkLost) and lost_kind(e) == "gone"
    assert isinstance(raised(lambda: comp.query("READ 30008 float32")), LinkLost)   # still out: said again
    adapter.pulled = False
    assert comp.query("READ 30008 float32") == "1.5" and adapter.opened == 2          # plugged back: opened again
    assert ModbusInstrument.quiet_close


def test_a_modbus_device_that_doesnt_answer_is_lost_and_a_garbled_one_is_not():
    from labharness.modbus import ModbusInstrument
    replies = []
    comp = ModbusInstrument("comp", compressor_card(), "modbus://10.0.0.5?unit=1",
                            module={"transport": lambda frame: replies.pop(0) if replies else b""})
    assert lost_kind(raised(lambda: comp.query("READ 30008 float32"))) == "silent"   # no reply at all
    rtu = ModbusInstrument("comp", compressor_card(), "modbus://COM5?unit=1",
                           module={"transport": lambda frame: b"", "rtu": True})
    assert lost_kind(raised(lambda: rtu.query("READ 30008 float32"))) == "silent"
    replies.append(b"\x00\x01\x00")                                                  # three bytes: it answered, garbled
    assert lost_kind(raised(lambda: comp.query("READ 30008 float32"))) is None


class Rpc:
    def __init__(self, mode):
        self.mode = mode

    def create_connection(self, address, timeout):
        return self

    def settimeout(self, t):
        pass

    def sendall(self, data):
        if self.mode == "reset":
            raise ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")

    def recv(self, n):
        if self.mode == "silent":
            raise socket.timeout("timed out")
        return b""

    def close(self):
        pass


@pytest.mark.parametrize("mode, kind", [("silent", "silent"), ("reset", "gone"), ("closed", "gone")])
def test_a_json_rpc_controller_that_went_silent_or_away_is_lost(mode, kind):
    from labharness.jsonrpc import JsonRpcInstrument
    card = builtin_registry().get("attocube/attodry-enspire")
    dry = JsonRpcInstrument("dry", card, "jsonrpc://10.0.0.40:9090", module=Rpc(mode))
    query = next(p.get_cmd.render() for p in card.params.values() if p.get_cmd is not None)
    assert lost_kind(raised(lambda: dry.query(query))) == kind


def test_a_web_api_that_refuses_or_doesnt_answer_is_lost():
    import urllib.error
    from labharness.webapi import HttpInstrument
    card = builtin_registry().get("lightcon/pharos")

    def refused(req, timeout=None, **kw):
        raise urllib.error.URLError(ConnectionRefusedError(10061, "No connection could be made because the target "
                                                                  "machine actively refused it"))

    def silent(req, timeout=None, **kw):
        raise urllib.error.URLError(socket.timeout("timed out"))
    e = raised(lambda: HttpInstrument("fs", card, "http://10.0.0.7:20022/v1", module=refused).query(
        "GET /Basic/ActualOutputPower"))
    assert isinstance(e, LinkLost) and "could not reach http://10.0.0.7:20022/v1" in str(e)
    e = raised(lambda: HttpInstrument("fs", card, "http://10.0.0.7:20022/v1", module=silent).query(
        "GET /Basic/ActualOutputPower"))
    assert isinstance(e, NoAnswer)


# ------------------------------------------------------------------ Quantum Design MultiVu (drivers.py)

class MultiVuServer:
    """MultiPyVu's server next to MultiVu: its clients fail while it is down; a new client reaches it once it is up."""

    def __init__(self):
        self.up, self.clients = True, []
        server = self

        class Mode:
            fast_settle, linear = "fast", "linear"

        class Client:
            def __init__(self, host="localhost", port=5000):
                server.clients.append(self)
                self.T, self.temperature = 300.0, type("T", (), {"approach_mode": Mode})
                self.field, self.closed = type("B", (), {"approach_mode": Mode}), False

            def open(self):
                if not server.up:
                    raise ConnectionRefusedError(10061, "No connection could be made because the target machine "
                                                        "actively refused it")

            def get_temperature(self):
                if not server.up or self.closed:
                    raise ConnectionAbortedError(10053, "An established connection was aborted")
                return self.T, "Stable"

            def get_field(self):
                return 0.0, "Holding"

            def close_client(self):
                self.closed = True

        self.Client = Client


def test_a_multivu_whose_server_restarted_is_lost_then_reached_again():
    server = MODULES["qd"] = MultiVuServer()
    lab = build_lab("""
lab: PPMS
instruments:
  ppms: { card: qd/multivu, address: "qd://192.168.1.20" }
""")
    assert lab.read("ppms", "T") == pytest.approx(300.0) and len(server.clients) == 1
    server.up = False
    e = raised(lambda: lab.read("ppms", "T"))
    assert isinstance(e, InstrumentLost) and lost_kind(e.__cause__) == "gone"
    assert isinstance(raised(lambda: lab.read("ppms", "T")), InstrumentLost)  # still down: tried, and said again
    server.up = True
    assert lab.read("ppms", "T") == pytest.approx(300.0)                      # back: a new client, by itself
    assert server.clients[0].closed and not server.clients[-1].closed
