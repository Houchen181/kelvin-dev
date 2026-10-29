"""A pretend Prologix adapter with pretend GPIB instruments behind it, for tests: no network, no hardware.

It does what the adapter's manual (version 1.6.6.0) says: a line ends at an unescaped CR or LF; one that starts with
an unescaped ++ is the adapter's command; in any other, ESC passes the byte after it and an unescaped +, ESC is dropped,
the rest goes to the instrument at ++addr with the ++eos ending. ++read eoi / ++read <byte> read the instrument's reply
(++eot_char marking where it asserted EOI); with nothing to read within ++read_tmo_ms the adapter gives up and sends
nothing, and a command sent before it has given up is counted (overlapped: a real one would take it late). Settings
change in its EEPROM while ++savecfg is on (saved counts them).
"""
import threading
import time

ESC, CR, LF, PLUS = 0x1B, 0x0D, 0x0A, 0x2B


class Instrument:
    """A pretend GPIB instrument. answer(bytes received) gives its reply's bytes (with its line end), or None for a
    command it only takes; delay: the seconds it takes to have a reply ready; eoi: it asserts EOI on its last byte."""

    def __init__(self, answer=None, delay=0.0, eoi=True):
        self.answer = answer or (lambda data: None)
        self.delay, self.eoi = delay, eoi
        self.heard = []   # what it received, each command with its line end
        self.out, self.ready = b"", 0.0


def card_instrument(card_id, name="dev", **kw):
    """An instrument that answers as its card's twin does (as test_hardware's fake VISA bus)."""
    from test_hardware import Device
    dev = Device(card_id, name=name)

    def answer(data):
        command = data.decode("ascii").strip()
        if "?" in command:
            return dev.twin.query(command).encode("ascii") + b"\n"
        dev.twin.write(command)
        return None
    inst = Instrument(answer, **kw)
    inst.device = dev
    return inst


def says(reply, **kw):
    """An instrument that answers *IDN? with reply (bytes, its line end included)."""
    return Instrument(lambda data: reply if data.strip() == b"*IDN?" else None, **kw)


class FakePrologix:
    def __init__(self, instruments=None, version="Prologix GPIB-ETHERNET Controller version 01.06.06.00"):
        self.instruments = dict(instruments or {})   # GPIB address -> Instrument
        self.version = version
        self.commands = []        # its ++ commands, in order, without the ++
        self.state = {"mode": "1", "addr": "10", "auto": "1", "eoi": "1", "eos": "0", "eot_enable": "0",
                      "eot_char": "0", "read_tmo_ms": "1200", "savecfg": "1"}
        self.saved = 0            # settings written to its EEPROM
        self.links = self.open = 0
        self.refuse = False       # takes no connection (switched off)
        self.overlapped = 0       # commands sent while it was still waiting for a reply that didn't come
        self.lock = threading.Lock()
        self._line, self._escaped, self._out, self._busy = [], False, [], 0.0

    # what the host's connection does
    def feed(self, data):
        with self.lock:
            for b in data:
                if self._escaped:
                    self._line.append((b, True))
                    self._escaped = False
                elif b == ESC:
                    self._escaped = True
                elif b in (CR, LF):
                    line, self._line = self._line, []
                    if line:
                        self._do(line)
                else:
                    self._line.append((b, False))

    def take(self, seconds, drop=False):
        """What has reached the host, waiting up to `seconds` for something to."""
        end = time.monotonic() + seconds
        while True:
            with self.lock:
                now = time.monotonic()
                due = [c for c in self._out if c[0] <= now]
                if due or drop:
                    self._out = [c for c in self._out if c[0] > now]
                    return b"".join(d for _, d in due)
            left = end - time.monotonic()
            if left <= 0:
                return b""
            time.sleep(min(left, 0.002))

    # the adapter
    def _send(self, data, when=None):
        self._out.append((time.monotonic() if when is None else when, data))

    def _do(self, line):
        if time.monotonic() < self._busy - 0.02:
            self.overlapped += 1
        if len(line) >= 2 and line[0] == (PLUS, False) and line[1] == (PLUS, False):
            return self._command(bytes(b for b, _ in line[2:]).decode("ascii").strip())
        data = bytes(b for b, escaped in line if escaped or b not in (PLUS, ESC))
        data += {"0": b"\r\n", "1": b"\r", "2": b"\n", "3": b""}[self.state["eos"]]
        inst = self.instruments.get(int(self.state["addr"].split()[0]))
        if inst is None:
            return   # nothing listens at that address
        inst.heard.append(data)
        reply = inst.answer(data)
        if reply is not None:
            inst.out, inst.ready = reply, time.monotonic() + inst.delay
        if self.state["auto"] == "1":
            self._read("eoi")

    def _command(self, text):
        self.commands.append(text)
        name, _, arg = text.partition(" ")
        arg = arg.strip()
        if name == "ver":
            self._send(self.version.encode("ascii") + b"\r\n")
        elif name == "read":
            self._read(arg)
        elif name == "clr":
            inst = self.instruments.get(int(self.state["addr"].split()[0]))
            if inst is not None:
                inst.out = b""
                inst.heard.append(b"<device clear>")
        elif name in self.state:
            if not arg:
                self._send(self.state[name].encode("ascii") + b"\r\n")
            else:
                if name != "savecfg" and self.state["savecfg"] == "1" and self.state[name] != arg:
                    self.saved += 1
                self.state[name] = arg
        elif name not in ("spoll", "ifc", "trg", "loc", "llo", "rst", "srq", "status", "lon", "help"):
            self._send(b"Unrecognized command\r\n")

    def _read(self, arg):
        inst = self.instruments.get(int(self.state["addr"].split()[0]))
        now, wait = time.monotonic(), int(self.state["read_tmo_ms"]) / 1000.0
        if inst is None or not inst.out or inst.ready - now > wait:
            self._busy = now + wait   # it waits that long, then gives up, sending nothing
            return
        when, eot = max(now, inst.ready), bytes([int(self.state["eot_char"])])
        if arg in ("eoi", ""):
            data, inst.out = inst.out, b""
            reads_on = arg == "" or not inst.eoi
        else:
            i = inst.out.find(bytes([int(arg)]))
            data, inst.out = (inst.out, b"") if i < 0 else (inst.out[:i + 1], inst.out[i + 1:])
            reads_on = i < 0
        if inst.eoi and not inst.out and self.state["eot_enable"] == "1" and arg != "":
            data += eot
        self._send(data, when)
        if reads_on:   # no EOI and no end byte: it reads on until its timeout passes
            self._busy = when + wait


class Link:
    """The host's connection to a pretend adapter (as prologix._Socket and _Serial are to a real one)."""

    def __init__(self, fake):
        self.fake = fake
        self.closed = False

    def send(self, data):
        if self.fake.refuse:
            raise ConnectionResetError("[WinError 10054] An existing connection was forcibly closed")
        self.fake.feed(data)

    def recv(self, seconds):
        if self.fake.refuse:
            raise ConnectionResetError("[WinError 10054] An existing connection was forcibly closed")
        return self.fake.take(seconds)

    def drop_input(self):
        self.fake.take(0, drop=True)

    def close(self):
        if not self.closed:
            self.closed = True
            self.fake.open -= 1


class Adapters:
    """Pretend adapters by where they are (COM5, 192.168.1.50:1234), for prologix.OPEN."""

    def __init__(self):
        self.fakes = {}

    def add(self, adapter, instruments=None, **kw):
        fake = self.fakes[adapter] = FakePrologix(instruments, **kw)
        return fake

    def open(self, adapter, seconds):
        fake = self.fakes.get(adapter)
        if fake is None or fake.refuse:
            raise ConnectionRefusedError("[WinError 10061] No connection could be made because the target machine "
                                         "actively refused it")
        if fake.open:
            raise ConnectionRefusedError("it takes one connection at a time")
        fake.links += 1
        fake.open += 1
        return Link(fake)
