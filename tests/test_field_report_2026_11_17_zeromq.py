"""Field report 2026-11-17, request R2: a ZeroMQ connection. IDQ's ID900 and ID1000 Time Controllers speak SCPI over
a ZeroMQ REQ/REP socket on TCP port 5555: one reply to every command, setters included; time values end in TB; an
error comes back as text holding SCPI_ERR. Kelvin's look found one (2026-11-15) but had no way to reach it, so the lab
wrote a Python wrapper and a card of its own. zmq://<host>[:<port>] now reaches it (zeromq.py), speaking ZMTP itself
as the look does, without pyzmq: here against a Time Controller written out by hand on a port of this computer."""
import socket
import struct
import threading
import time
import types

import pytest

from labharness import EventLog, SafetyGate, ScriptedApprover, cardtry, probe, scriptrun, zeromq
from labharness.adapters import (AdapterError, ErrorAnswer, LinkLost, NoAnswer, bus_of, lost_kind, resource_name,
                                 where_and_cause)
from labharness.cards import CardError, parse_card
from labharness.clock import SimClock
from labharness.discovery import card_for
from labharness.drivers import is_driver_address, sends_text
from labharness.homelab import build_lab, builtin_registry
from labharness.instrument import Stopped
from labharness.lab import Lab
from labharness.labconfig import load_lab_config
from labharness.sim.twin import World, sim_backend_factory
from labharness.yamlio import load_yaml

IDN = "ID Quantique,ID900,SN01234,v7.0.4"
CARD = """card: 1
id: test/time-controller
model: Test Time Controller
vendor: ID Quantique
kind: time-controller
status: draft
identify: { idn: "^ID Quantique,ID900", example: "ID Quantique,ID900,SN01234,v7.0.4" }
parameters:
  delay: { label: Input 1 delay, kind: measure, type: int, set: "INPUt1:DELAy {value}TB", get: "INPUt1:DELAy?",
           parse: '([-+0-9.eE]+)\\s*TB', limits: [0, 10000000] }
  counts: { label: Input 1 counts, kind: measure, type: int, get: "INPUt1:COUNter?" }
  histogram: { label: Histogram, kind: measure, type: trace, get: "HIST1:DATA?" }
"""


def frame(flags, body):
    if len(body) > 255:
        return bytes([flags | 0x02]) + struct.pack(">Q", len(body)) + body
    return bytes([flags, len(body)]) + body


def command(name, data=b""):
    return frame(0x04, bytes([len(name)]) + name + data)


class TimeController:
    """An IDQ Time Controller as Kelvin's zmq:// connection meets it: a ZeroMQ REP socket's side of ZMTP 3.1, written
    out by hand on a port of this computer in libzmq's order (its signature as a connection opens, its major version
    once the peer's signature is in, the rest of its greeting once the peer's version is, READY), then a reply to each
    request: SCPI, with times in TB and SCPI_ERR for what it refuses. Every connection is served on a thread of its
    own. Its knobs make it answer as a peer may: slowly, in frames, after a PING, hanging up, refusing."""
    ERR = b'SCPI_ERR -113,"Undefined header"'

    def __init__(self, socket_type=b"REP", mechanism=b"NULL"):
        self.socket_type, self.mechanism = socket_type, mechanism
        self.delay, self.counts = 1000, 0
        self.histogram = b",".join(b"%d" % (i % 1000) for i in range(400000))   # 1.5 MB as text
        self.heard, self.readies, self.pongs = [], [], []   # (connection, command) in order; READYs; PONGs
        self.connections = 0
        self.slow = {}            # a command -> how long its reply waits (s)
        self.frames = {}          # a command -> the frames its reply comes in
        self.ping = False         # a PING before each reply, as ZMTP 3.1's heartbeat sends it
        self.beat = 0.0           # and every so often while a slow reply waits
        self.close_after = set()  # the commands after whose reply it closes the connection, as if it restarted
        self.hang_up = set()      # the commands it closes the connection on, no reply sent
        self.refuse = set()       # the commands it answers with ZMTP's ERROR
        self.set_reply = b""      # what a setting gets back
        self.gone = []            # the connections that ended, by their number
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        self.address = "zmq://127.0.0.1:%d" % self.port
        self.free = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.free.set()
        self.listener.close()

    def _accept(self):
        while not self.free.is_set():
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn, self.connections), daemon=True).start()

    @staticmethod
    def _need(conn, n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise OSError("closed")
            data += chunk
        return data

    def _frame(self, conn):
        flags = self._need(conn, 1)[0]
        size = struct.unpack(">Q", self._need(conn, 8))[0] if flags & 0x02 else self._need(conn, 1)[0]
        return flags, self._need(conn, size)

    def _serve(self, conn, number):
        conn.settimeout(10)
        try:
            conn.sendall(b"\xff" + bytes(7) + b"\x01\x7f")
            self._need(conn, 10)
            conn.sendall(b"\x03")
            self._need(conn, 1)
            conn.sendall(b"\x01" + self.mechanism.ljust(20, b"\x00") + b"\x00" + bytes(31))
            self._need(conn, 53)
            conn.sendall(command(b"READY", bytes([11]) + b"Socket-Type" + struct.pack(">I", len(self.socket_type))
                                 + self.socket_type))
            self.readies.append(self._frame(conn)[1])
            while True:
                parts = []
                while True:
                    flags, body = self._frame(conn)
                    if flags & 0x04:   # a command between messages: Kelvin's PONG
                        self.pongs.append(body)
                        continue
                    parts.append(body)
                    if not flags & 0x01:
                        break
                said = parts[-1].decode()
                self.heard.append((number, said))
                if said in self.hang_up:
                    return
                if said in self.refuse:
                    conn.sendall(command(b"ERROR", b"\x0fNot allowed now"))
                    return
                wait = self.slow.get(said, 0)
                while wait > 0:
                    time.sleep(min(wait, self.beat or wait))
                    wait -= min(wait, self.beat or wait)
                    if self.beat and wait > 0:
                        conn.sendall(command(b"PING", b"\x00\x64beat"))
                reply = [b""] + (self.frames.get(said) or [self.answer(said)])   # the delimiter, then the reply
                out = command(b"PING", b"\x00\x64ctx1") if self.ping else b""
                out += b"".join(frame(0x01 if i < len(reply) - 1 else 0x00, p) for i, p in enumerate(reply))
                conn.sendall(out)
                if said in self.close_after:
                    return
        except OSError:
            pass
        finally:
            conn.close()
            self.gone.append(number)

    def ended(self, number, seconds=5.0):
        """Whether connection `number` has ended, waiting `seconds` at most for it to."""
        end = time.monotonic() + seconds
        while number not in self.gone and time.monotonic() < end:
            time.sleep(0.01)
        return number in self.gone

    def answer(self, said):
        if said == "*IDN?":
            return IDN.encode()
        if said == "*OPC?":
            return b"1"
        if said == "INPUt1:DELAy?":
            return b"%dTB" % self.delay
        if said == "INPUt1:COUNter?":
            return b"%d" % self.counts
        if said == "HIST1:DATA?":
            return self.histogram
        if said.startswith("INPUt1:DELAy ") and said.endswith("TB"):
            value = int(said[len("INPUt1:DELAy "):-2])
            if not 0 <= value <= 1000000:
                return b'SCPI_ERR -222,"Data out of range"'
            self.delay = value
            return self.set_reply
        return self.ERR


@pytest.fixture
def tc():
    made = []

    def make(*a, **k):
        made.append(TimeController(*a, **k))
        return made[-1]
    yield make
    for t in made:
        t.close()


def card(**connection):
    data = load_yaml(CARD)
    if connection:
        data["connection"] = connection
    return parse_card(data, "test card")


def connected(t, **connection):
    return zeromq.ZmqInstrument("tc", card(**connection), t.address)


def lab_with(t, **connection):
    registry = builtin_registry()
    registry.add(card(**connection))
    return build_lab("lab: Time tagging\ninstruments:\n  tc: { card: test/time-controller, address: \"%s\" }\n"
                     % t.address, registry=registry)


# ---------------------------------------------------------------------- the connection

def test_kelvin_greets_the_time_controller_as_a_req_socket_and_asks_it(tc):
    t = tc()
    backend = connected(t)
    assert backend.peer == {"version": "3.1", "socket": "REP"}
    assert backend.query("*IDN?") == IDN
    assert t.heard == [(1, "*IDN?")] and t.connections == 1
    assert b"\x05READY\x0bSocket-Type\x00\x00\x00\x03REQ" in t.readies[0]   # as a REQ socket
    backend.close()                      # sends the instrument nothing: it only sees the connection go
    assert t.ended(1) and t.heard == [(1, "*IDN?")] and backend.quiet_close

    lab = lab_with(t)                    # through a lab, its card's commands: the time read with its parse
    try:
        assert lab.read("tc", "delay") == 1000 and lab.read("tc", "counts") == 0
        health = lab.health_check(["tc"])[0]
        assert health.ok, health.problems
    finally:
        lab.close()


def test_a_setting_reads_its_reply_before_anything_else_is_sent(tc, tmp_path):
    """A REQ socket can't send twice: every command's reply is read, a setting's too (here the empty one IDQ's setters
    answer), so the next request goes on the same connection, in step."""
    t = tc()
    backend = connected(t)
    backend.write("INPUt1:DELAy 2000TB")
    assert backend.query("INPUt1:DELAy?") == "2000TB"
    assert t.heard == [(1, "INPUt1:DELAy 2000TB"), (1, "INPUt1:DELAy?")] and t.connections == 1

    t.set_reply = b"OK"                  # a card whose instrument says it took a setting: checked as on VISA
    checked = zeromq.ZmqInstrument("tc2", card(set_reply="^OK$"), t.address)
    checked.write("INPUt1:DELAy 3000TB")
    t.set_reply = b"NOPE"
    with pytest.raises(AdapterError, match=r"tc2 refused 'INPUt1:DELAy 4000TB': NOPE"):
        checked.write("INPUt1:DELAy 4000TB")

    lab = lab_with(t)                    # through the gate: the card's set template writes the unit
    try:
        gate = SafetyGate(lab, ScriptedApprover([True]), EventLog(tmp_path / "events.jsonl", lab.clock))
        assert gate.set("tc", "delay", 2500, wait=False).ok   # a draft card: the person said yes
        assert "INPUt1:DELAy 2500TB" in [h[1] for h in t.heard] and lab.read("tc", "delay") == 2500
    finally:
        lab.close()


def test_scpi_err_is_the_instruments_own_error_in_its_own_words(tc, tmp_path):
    t = tc()
    backend = connected(t)
    with pytest.raises(ErrorAnswer, match=r"tc refused 'BOGUS\?': SCPI_ERR -113,\"Undefined header\"") as e:
        backend.query("BOGUS?")
    assert lost_kind(e.value) is None    # it is there and answering: never a lost instrument
    with pytest.raises(ErrorAnswer, match="Data out of range"):
        backend.write("INPUt1:DELAy 5000000TB")
    assert backend.query("*IDN?") == IDN and t.connections == 1   # an error is a reply: the connection goes on

    t.ERR = b"ERROR: unknown command"    # an instrument that words its errors another way: its card says how
    assert backend.query("BOGUS?") == "ERROR: unknown command"
    with pytest.raises(ErrorAnswer, match="ERROR: unknown command"):
        connected(t, error_reply="^ERROR").query("BOGUS?")

    lab = lab_with(t)                    # through the gate: the set fails in the instrument's words, nothing lost
    try:
        gate = SafetyGate(lab, ScriptedApprover([True]), EventLog(tmp_path / "events.jsonl", lab.clock))
        result = gate.set("tc", "delay", 5000000, wait=False)
        assert not result.ok and "Data out of range" in result.message, result
        assert lab.instruments["tc"].health.fails == 0
    finally:
        lab.close()

    for bad, said in (("(", "not a valid regex"), (" ", "leave it out for SCPI_ERR")):
        with pytest.raises(CardError, match=r"connection.error_reply: .*%s" % said):
            card(error_reply=bad)


def test_a_reply_that_doesnt_come_in_time_is_no_answer_and_the_next_request_connects_anew(tc):
    t = tc()
    t.slow["INPUt1:COUNter?"] = 1.5
    backend = connected(t, timeout_ms=300)
    t0 = time.monotonic()
    with pytest.raises(NoAnswer, match=r"tc: no reply to INPUt1:COUNter\? within 0.3 s") as e:
        backend.query("INPUt1:COUNter?")
    assert time.monotonic() - t0 < 1.2 and lost_kind(e.value) == "silent"
    assert backend.link is None          # dropped: a REQ socket can't ask again before its reply
    assert backend.query("*IDN?") == IDN   # on a new connection: the late "0" is never read as its reply
    assert t.heard == [(1, "INPUt1:COUNter?"), (2, "*IDN?")] and t.connections == 2

    t.hang_up.add("HIST1:DATA?")         # a connection that ends under a request: lost, and never sent again
    with pytest.raises(LinkLost, match=r"tc: lost the connection to 127.0.0.1:%d asking HIST1:DATA\? \(it closed "
                                       r"the connection\)" % t.port) as e:
        backend.query("HIST1:DATA?")
    assert lost_kind(e.value) == "gone"
    assert backend.query("INPUt1:DELAy?") == "1000TB" and [h for h in t.heard if h[1] == "HIST1:DATA?"] == [
        (2, "HIST1:DATA?")] and t.connections == 3

    t.close_after.add("*IDN?")           # it closed the connection while nothing was asked (a restart of its server):
    assert backend.query("*IDN?") == IDN   # made anew before the next request, which goes once and is answered
    assert t.ended(3)
    time.sleep(0.05)                     # its close on its way here
    assert backend.query("INPUt1:DELAy?") == "1000TB" and t.heard[-1] == (4, "INPUt1:DELAy?")

    closed = socket.socket()             # nothing there: the link failed, said as a lost instrument
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    with pytest.raises(LinkLost, match="could not reach 127.0.0.1:%d" % port) as e:
        zeromq.ZmqInstrument("tc", card(), "zmq://127.0.0.1:%d" % port)
    assert lost_kind(e.value) == "gone"


def test_a_reply_of_several_frames_and_a_long_one(tc, monkeypatch):
    t = tc()
    t.frames["INPUt1:COUNter?"] = [b"12", b"34", b"56"]                   # each frame one of the reply's values
    t.frames["*IDN?"] = [b"ID Quantique,ID900", b",SN01234,v7.0.4"]      # split at its commas: none added
    backend = connected(t)
    assert backend.query("INPUt1:COUNter?") == "12,34,56"
    assert backend.query("*IDN?") == IDN
    histogram = backend.query("HIST1:DATA?")                              # 1.5 MB in one frame
    assert histogram.encode() == t.histogram and len(histogram) > 1000000

    lab = lab_with(t)                    # the card's trace reads it whole
    try:
        values = lab.read("tc", "histogram")
        assert len(values) == 400000 and values[1001] == 1.0
    finally:
        lab.close()

    monkeypatch.setattr(zeromq, "MOST_REPLY", 100000)   # a reply past the most Kelvin reads: refused unread
    with pytest.raises(AdapterError, match=r"the reply to HIST1:DATA\? from 127.0.0.1:%d can't be read: it sent a "
                                           r"frame of %d bytes, which makes a reply longer than Kelvin reads"
                                           % (t.port, len(t.histogram))) as e:
        backend.query("HIST1:DATA?")
    assert lost_kind(e.value) is None and backend.query("*IDN?") == IDN   # anew: the rest of it never read as a reply


def test_a_ping_meanwhile_is_answered_and_a_refusal_says_why(tc):
    t = tc()
    t.ping = True                        # ZMTP 3.1's heartbeat in front of each reply: answered, the reply read
    backend = connected(t)
    assert backend.query("*IDN?") == IDN and backend.query("INPUt1:DELAy?") == "1000TB"
    assert t.pongs[0] == b"\x04PONGctx1"   # its context back, as ZMTP asks

    t.refuse.add("INPUt1:COUNter?")      # ZMTP's ERROR: its reason said, and the next request connects anew
    with pytest.raises(AdapterError, match=r"the reply to INPUt1:COUNter\? from 127.0.0.1:%d can't be read: it "
                                           r"refused the connection: Not allowed now" % t.port):
        backend.query("INPUt1:COUNter?")
    assert backend.query("*IDN?") == IDN

    for kw, said in (({"socket_type": b"PUB"}, "its socket is PUB, which takes no questions: Kelvin asked it nothing"),
                     ({"mechanism": b"CURVE"}, "it wants ZeroMQ's CURVE security, which Kelvin doesn't speak")):
        other = tc(**kw)
        with pytest.raises(AdapterError, match="tc: can't connect to ZeroMQ at 127.0.0.1:%d: %s" % (other.port, said)):
            connected(other)
        assert other.heard == []         # nothing asked


def test_one_request_at_a_time(tc):
    t = tc()
    t.slow["INPUt1:COUNter?"] = 0.01
    backend = connected(t)
    wrong = []

    def ask(question, answer):
        for _ in range(15):
            got = backend.query(question)
            if got != answer:
                wrong.append((question, got))
    threads = [threading.Thread(target=ask, args=a) for a in (("*IDN?", IDN), ("INPUt1:DELAy?", "1000TB"),
                                                               ("INPUt1:COUNter?", "0"))]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    assert wrong == [] and len(t.heard) == 45 and t.connections == 1


def test_a_long_wait_for_a_reply_ends_at_a_persons_stop(tc):
    """*OPC? after an acquisition answers only once it is done: waited for as long as the card's reading says (past
    the connection's own timeout), a person's Stop ending the wait at once."""
    t = tc()
    t.slow["*OPC?"] = 3.0
    backend = connected(t, timeout_ms=300)
    pressed = threading.Event()
    threading.Timer(0.3, pressed.set).start()
    t0 = time.monotonic()
    with pytest.raises(Stopped, match=r"stopped by a person while it was still working on \*OPC\?"):
        backend.query_waiting("*OPC?", 10.0, pressed.is_set)
    assert time.monotonic() - t0 < 1.5
    assert backend.query("*IDN?") == IDN   # on a new connection: the late "1" is never read as its reply
    with pytest.raises(NoAnswer, match=r"no reply to \*OPC\? within 0.5 s"):
        backend.query_waiting("*OPC?", 0.5, lambda: False)
    t.slow["*OPC?"] = 0.6
    assert backend.query_waiting("*OPC?", 5.0, lambda: False) == "1"   # twice the connection's timeout: answered
    t.slow["*OPC?"], t.beat = 1.0, 0.4   # its heartbeat while the reply waits, slower than the connection's timeout:
    assert backend.query_waiting("*OPC?", 5.0, lambda: False) == "1"   # each PING answered, and the wait goes on
    assert backend.query("*IDN?") == IDN and b"\x04PONGbeat" in t.pongs


def test_a_card_is_tried_on_it_as_on_any_text_instrument(tc):
    """Its commands are the instrument's own: a try asks only what only reads (never MEASure?, which may start
    something), and checks a set command by writing a setting with the value it has, the TB with it."""
    t = tc()
    data = load_yaml(CARD)
    data["parameters"]["rate"] = {"label": "Count rate", "kind": "measure", "get": "MEASure:RATE?"}
    result = cardtry.try_card(parse_card(data, "test card"), t.address)
    assert result["identity"] == IDN and result["matches"]
    rows = {r["name"]: r for r in result["readings"]}
    assert rows["delay"]["value"] == 1000 and rows["histogram"]["value"] == "trace (400000 points)"
    assert "may start a measurement" in rows["rate"]["skipped"]
    assert result["write_check"] == {"name": "delay", "value": 1000, "unit": "", "ok": True,
                                     "sent": "INPUt1:DELAy 1000TB"}
    assert "MEASure:RATE?" not in [h[1] for h in t.heard] and ("INPUt1:DELAy 1000TB" in [h[1] for h in t.heard])


# ---------------------------------------------------------------------- the address, and the fence

def test_the_address_and_its_helpers():
    assert resource_name("zmq://10.0.0.7") == resource_name("ZMQ://10.0.0.7:5555/") == "zmq://10.0.0.7:5555"
    assert bus_of("zmq://10.0.0.7:5555") is None   # a connection of its own: no GPIB bus to hang
    assert where_and_cause("zmq://10.0.0.7:5555") == ("on the network (10.0.0.7)",
                                                      "network cable, power or its IP address?")
    assert is_driver_address("zmq://10.0.0.7") and sends_text("zmq://10.0.0.7")
    assert not sends_text("jsonrpc://10.0.0.40:9090") and not sends_text("GPIB0::5::INSTR")
    assert zeromq.canonical("zmq://[FE80::1]") == "zmq://[fe80::1]:5555"
    assert zeromq.as_socket("zmq://[::ffff:10.0.0.7]:5556") == "TCPIP0::10.0.0.7::5556::SOCKET"
    assert zeromq.as_socket("zmq://[fe80:0::1]") == "TCPIP0::[fe80::1]::5555::SOCKET"
    for bad in ("zmq://", "zmq://10.0.0.7:99999", "zmq://10.0.0.7:x", "zmq://10.0.0.7/data", "zmq://10.0.0.7?a=1",
                "tcp://10.0.0.7:5555"):
        with pytest.raises(ValueError, match="is not a ZeroMQ instrument's address: zmq://<its IP address"):
            zeromq.where(bad)
    with pytest.raises(AdapterError, match="tc: 'zmq://10.0.0.7/data' is not a ZeroMQ instrument's address"):
        zeromq.ZmqInstrument("tc", card(), "zmq://10.0.0.7/data")
    assert card_for("zmq://10.0.0.7:5555", builtin_registry()) == (None, "")   # any instrument's: no card from it


FENCE_LAB = """
lab: Fence
instruments:
  tc: { card: virtual/lock-in, address: "zmq://10.0.0.7:5555" }
  here: { card: virtual/source-meter, address: "zmq://127.0.0.1" }
"""


@pytest.fixture
def fenced():
    registry = builtin_registry()
    clock = SimClock()
    world = World(clock, 0)
    return Lab(load_lab_config(FENCE_LAB, registry), registry, sim_backend_factory(world), clock, world)


@pytest.mark.parametrize("address, name", [
    ("TCPIP0::10.0.0.7::5555::SOCKET", "tc"),     # a script's raw socket to its port, as Kelvin's stand-in names it
    ("TCPIP::10.0.0.7::5555::SOCKET", "tc"),
    ("zmq://10.0.0.7", "tc"),                      # its port left out: 5555
    ("TCPIP0::localhost::5555::SOCKET", "here"),   # another name of its host
    ("TCPIP0::10.0.0.7::5556::SOCKET", None),      # another port: another instrument
    ("TCPIP0::10.0.0.8::5555::SOCKET", None),
])
def test_the_fence_knows_a_zeromq_instrument_by_its_port(fenced, address, name):
    assert scriptrun.lab_name(fenced, address) == name


def test_a_scripts_raw_socket_to_it_goes_through_the_gate_or_not_at_all(fenced):
    """Opened through Kelvin's stand-in (a plain socket's connect), the socket's commands go through the safety gate,
    as for any lab instrument; a socket of the script's own past the stand-in is refused, and the run ends there."""
    def nobody(request):
        raise AssertionError("nobody is asked: %s" % request.title)
    me = types.SimpleNamespace(lab=fenced, rehearsal=False, allowed=set(), prep=types.SimpleNamespace(name="tags.py"),
                               opened={}, passed=[], _ask=nobody, auto=False, allowed_ports=set())
    assert scriptrun.ScriptRun._open(me, "TCPIP0::10.0.0.7::5555::SOCKET", "socket") == {"instrument": "tc"}
    with pytest.raises(scriptrun._Refuse, match=r"refused: tags.py connects a socket of its own to "
                                                r"TCPIP0::10.0.0.7::5555::SOCKET, which is the lab's tc, past Kelvin's "
                                                r"stand-in") as e:
        scriptrun.ScriptRun._past(me, "TCPIP0::10.0.0.7::5555::SOCKET", "socket.connect")
    assert e.value.stop and me.passed == []
    assert scriptrun.same_device(fenced, "TCPIP0::10.0.0.7::inst0::INSTR") == "tc"   # its host, another way in


# ---------------------------------------------------------------------- the scan and the look

def runtime_on(tmp_path, lab, kind):
    from labharness.agent.runtime import LabRuntime
    rt = LabRuntime(lab, ScriptedApprover([]), tmp_path / "data")
    rt.home, rt.cards_dir, rt.scan_network = tmp_path, tmp_path / "cards", False
    rt.lab_info = {"kind": kind, "path": str(tmp_path / "lab.yaml"), "error": None,
                   "has_file": (tmp_path / "lab.yaml").exists()}
    return rt


@pytest.fixture
def quiet_network(monkeypatch):
    """This computer's network as the look reads it: one adapter, an empty neighbour table, no adverts."""
    from labharness import survey
    monkeypatch.setattr(probe, "WIN", True)
    monkeypatch.setattr(probe, "network_adapters", lambda: [
        {"name": "Ethernet", "address": "192.168.1.10", "network": "192.168.1.0/24", "link_local": False}])
    monkeypatch.setattr(probe, "_run_text", lambda command: "")
    monkeypatch.setattr(probe, "_udp", lambda: pytest.fail("a GigE Vision broadcast with no link-local network"))
    monkeypatch.setattr(survey, "network_adverts", lambda *a, **k: ([], []))


def test_inspect_address_offers_its_zmq_address_and_kelvin_adds_it_through_it(tc, tmp_path, monkeypatch,
                                                                               quiet_network):
    pytest.importorskip("mcp")
    from labharness.homelab import empty_lab, lab_registry
    t = tc()
    monkeypatch.setattr(probe, "PORTS", [])   # only the port named is looked at
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "test_time-controller.yaml").write_text(CARD, encoding="utf-8")
    rt = runtime_on(tmp_path, empty_lab(lab_registry(tmp_path)), "none")
    said = rt.inspect_address("127.0.0.1:%d" % t.port)
    assert ("- port %d: a ZeroMQ (REQ) instrument; it says: %s; Kelvin's address for it: %s → card "
            "test/time-controller (Test Time Controller): add_instrument %s" % (t.port, IDN, t.address, t.address)
            ) in said.splitlines(), said
    assert t.address in rt.last_found and rt.found_info[t.address]["card_id"] == "test/time-controller"

    tried = rt.try_card("test/time-controller", t.address)   # found by the look: nobody is asked
    assert "matches the card" in tried and "delay = 1000" in tried, tried
    assert "set command: delay takes INPUt1:DELAy 1000TB" in tried, tried
    added = rt.add_instrument(t.address, name="tc")
    assert added.startswith("added tc (Test Time Controller, card test/time-controller, a draft)"), added
    try:
        assert rt.lab.read("tc", "delay") == 1000 and "zmq://127.0.0.1:%d" % t.port in (
            tmp_path / "lab.yaml").read_text(encoding="utf-8")
    finally:
        rt.lab.close()


class Collection:
    """The instrument collection, as far as a scan reads it: it knows IDQ's ID900."""

    @staticmethod
    def instrument_for(idn):
        return {"id": "idquantique/id900", "name": "ID Quantique ID900 Time Controller", "kind": "time-controller",
                "manuals": []} if idn.startswith("ID Quantique,ID900") else None


def test_find_on_network_lists_the_time_controller_at_its_zmq_address(tc, tmp_path, monkeypatch, quiet_network):
    """The field report's network: the Time Controller at 192.168.1.78 (its port 5555 here on this computer's own)."""
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    from simlab import build_virtual_lab
    t = tc()

    def connect(ip, port, seconds):
        if (ip, port) == ("192.168.1.78", 5555):
            return socket.create_connection(("127.0.0.1", t.port), timeout=seconds)
        raise ConnectionRefusedError()
    monkeypatch.setattr(probe, "_connect", connect)
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    rt.collection = Collection()
    lines = rt.find_on_network().splitlines()
    assert ("- 192.168.1.78: zmq://192.168.1.78:5555 answers %r → no card yet (the instrument collection knows it as "
            "idquantique/id900): draft_card zmq://192.168.1.78:5555" % IDN) in lines, lines
    assert "zmq://192.168.1.78:5555" in rt.last_found
    assert rt.found_info["zmq://192.168.1.78:5555"]["idn"] == IDN

    t.slow["*IDN?"] = 3.0                # one that takes requests but says nothing to *IDN?: its address still kept
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data2")
    lines = rt.find_on_network().splitlines()
    assert ("- 192.168.1.78: port 5555: a ZeroMQ instrument (asked who it is as a REQ socket asks: it sent nothing "
            "more within 1.5 s); Kelvin's address for it: zmq://192.168.1.78:5555") in lines, lines
    assert "zmq://192.168.1.78:5555" in rt.last_found
