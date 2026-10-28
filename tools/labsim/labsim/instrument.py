"""The instrument's side of a connection, and the command parser models declare their commands for.

An instrument keeps what a real one keeps: an input buffer that becomes a command at its terminator (or at EOI on
GPIB, USB-TMC and VXI-11), an output queue a reply waits in until it is read, an error queue and the IEEE 488.2
status bytes. A new command arriving while a reply is still unread drops that reply (-410, Query INTERRUPTED) on
instruments that follow 488.2. A command it doesn't know gives -113 and no reply, so a query for it times out.
"""
from __future__ import annotations

import math
import random
import re
import time
from collections import deque
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

MESSAGE_BUSES = ("gpib", "usb", "vxi11", "hislip")   # buses that mark a message's end (EOI / END)


class CommandError(Exception):
    def __init__(self, code: int, text: str):
        super().__init__("%d,%s" % (code, text))
        self.code, self.text = code, text


UNDEFINED = (-113, "Undefined header")
ILLEGAL = (-224, "Illegal parameter value")
DATATYPE = (-104, "Data type error")
MISSING = (-109, "Missing parameter")
TOO_MANY = (-108, "Parameter not allowed")
SUFFIX = (-131, "Invalid suffix")
CONFLICT = (-221, "Settings conflict")
OUT_OF_RANGE = (-222, "Data out of range")
INTERRUPTED = (-410, "Query INTERRUPTED")
UNTERMINATED = (-420, "Query UNTERMINATED")
OVERFLOW = (-350, "Queue overflow")


def fail(pair: Tuple[int, str], detail: str = "") -> CommandError:
    return CommandError(pair[0], pair[1] + ("; " + detail if detail else ""))


_SUFFIX = {"": 1.0, "V": 1.0, "MV": 1e-3, "UV": 1e-6, "NV": 1e-9, "KV": 1e3, "A": 1.0, "MA": 1e-3, "UA": 1e-6,
           "NA": 1e-9, "PA": 1e-12, "HZ": 1.0, "KHZ": 1e3, "MHZ": 1e6, "GHZ": 1e9, "S": 1.0, "MS": 1e-3, "US": 1e-6,
           "NS": 1e-9, "OHM": 1.0, "KOHM": 1e3, "MOHM": 1e6, "W": 1.0, "MW": 1e-3, "UW": 1e-6, "DBM": 1.0, "DB": 1.0,
           "K": 1.0, "T": 1.0, "G": 1e-4, "KG": 0.1, "DEG": 1.0, "PCT": 1.0}
_NUMBER = re.compile(r"([+-]?(?:\d+\.?\d*|\.\d+)(?:[Ee][+-]?\d+)?)\s*([A-Za-z]*)$")


def parse_number(text: str, minmax: Optional[Sequence[float]] = None) -> float:
    t = text.strip().strip('"').upper()
    if minmax is not None:
        if t in ("MIN", "MINIMUM"):
            return float(minmax[0])
        if t in ("MAX", "MAXIMUM"):
            return float(minmax[1])
        if t in ("DEF", "DEFAULT") and len(minmax) > 2:
            return float(minmax[2])
    if t in ("INF", "+INF", "9.9E37"):
        return 9.9e37
    m = _NUMBER.fullmatch(t)
    if not m:
        raise fail(DATATYPE, text)
    scale = _SUFFIX.get(m.group(2))
    if scale is None:
        raise fail(SUFFIX, text)
    return float(m.group(1)) * scale


class Args(list):
    """A command's parameters, as the instrument parses them."""

    def __init__(self, items: Iterable[str], raw: str = ""):
        super().__init__(items)
        self.raw = raw

    def need(self, least: int, most: Optional[int] = None) -> None:
        if len(self) < least:
            raise fail(MISSING)
        if most is not None and len(self) > most:
            raise fail(TOO_MANY)

    def num(self, i: int = 0, lo: Optional[float] = None, hi: Optional[float] = None,
            minmax: Optional[Sequence[float]] = None, default: Optional[float] = None) -> float:
        if i >= len(self):
            if default is not None:
                return default
            raise fail(MISSING)
        v = parse_number(self[i], minmax)
        if (lo is not None and v < lo - 1e-12 * abs(lo)) or (hi is not None and v > hi + 1e-12 * abs(hi)):
            raise fail(OUT_OF_RANGE, self[i])
        return v

    def int(self, i: int = 0, lo: Optional[int] = None, hi: Optional[int] = None, default: Optional[int] = None) -> int:
        v = self.num(i, lo, hi, default=default)
        return int(round(v))

    def bool(self, i: int = 0) -> bool:
        if i >= len(self):
            raise fail(MISSING)
        t = self[i].strip().upper()
        if t in ("ON", "1", "TRUE"):
            return True
        if t in ("OFF", "0", "FALSE"):
            return False
        try:
            return parse_number(t) != 0
        except CommandError:
            raise fail(ILLEGAL, self[i])

    def word(self, i: int, choices: Sequence[str]) -> str:
        """One of the choices, written in manual notation (VOLTage): long or short form, any case."""
        if i >= len(self):
            raise fail(MISSING)
        t = self[i].strip().strip('"').strip("'").upper()
        for c in choices:
            long, short = c.upper(), (re.match(r"[A-Z0-9]*", c).group(0) or c.upper())
            if t in (long, short):
                return long
        raise fail(ILLEGAL, self[i])

    def text(self, i: int = 0) -> str:
        if i >= len(self):
            raise fail(MISSING)
        return self[i].strip().strip('"').strip("'")


def command(*patterns: str, query: bool = True, setter: bool = True, group: str = "") -> Callable:
    """Declare a handler fn(self, q, args, ch) for headers written in manual notation: SOURce:VOLTage[:LEVel];
    '#' after a mnemonic captures a channel number (SOURce#), <n> is a numeric node (RAMP:RATE:FIELD:<n>)."""
    def deco(fn: Callable) -> Callable:
        fn._labsim = getattr(fn, "_labsim", []) + [(p, query, setter, group) for p in patterns]
        return fn
    return deco


_TOKEN = re.compile(r"(\[)?(:)?(?:(\*?[A-Za-z][A-Za-z0-9_]*)(#)?|(<n>))(:)?(\])?")


def header_regex(pattern: str) -> "re.Pattern[str]":
    out, pos = [], 0
    pattern = pattern.lstrip(":")
    for m in _TOKEN.finditer(pattern):
        if m.start() != pos:
            raise ValueError("bad command pattern %r at %d" % (pattern, pos))
        pos = m.end()
        opt, colon, word, chan, numeric, tail, close = m.groups()   # tail: "[SENSe:]VOLTage" style
        if bool(opt) != bool(close):
            raise ValueError("unbalanced [ ] in %r" % pattern)
        if numeric:
            node = r"(\d+)"
        else:
            long = word.upper()
            caps = re.match(r"\*?[A-Z0-9_]*", word).group(0)
            short = caps if caps and caps != long else long
            node = re.escape(long) if short == long else "(?:%s|%s)" % (re.escape(long), re.escape(short))
            if chan:
                node += r"(\d*)"
        node = (":" if colon else "") + node + (":" if tail else "")
        out.append("(?:%s)?" % node if opt else node)
        if pos == len(pattern):
            break
    if pos != len(pattern):
        raise ValueError("bad command pattern %r" % pattern)
    return re.compile("".join(out))


def split_units(text: str, sep: str = ";") -> List[str]:
    out, cur, quote = [], [], ""
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch == sep:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def split_params(rest: str, sep: Optional[str]) -> List[str]:
    rest = rest.strip()
    if not rest:
        return []
    if sep is None:
        return rest.split()
    return [p.strip() for p in split_units(rest, sep)]


class Chunk:
    __slots__ = ("data", "pos", "ready", "eom")

    def __init__(self, data: bytes, ready: float, eom: bool):
        self.data, self.pos, self.ready, self.eom = data, 0, ready, eom


def fmt_e(v: float, digits: int = 6) -> str:
    """+1.234560E-03, as Keithley and Keysight send numbers."""
    return "%+.*E" % (digits, v)


class Instrument:
    """An instrument on one or more buses. Models set the class attributes and declare commands."""
    model = "generic"
    idn = "LABSIM,GENERIC,{serial},1.0"
    default_serial = "000000"
    has_488 = True           # *IDN?, *RST, *CLS, *ESR?, *STB?, *OPC?
    has_syst_err = True      # SYSTem:ERRor?
    drops_unread = True      # 488.2: a new command drops an unread reply (-410)
    terminators: Tuple[bytes, ...] = (b"\n",)
    reply_end = b"\n"
    param_split: Optional[str] = ","
    framing = (9600, 8, "none", 1.0)   # on a serial port: baud, data bits, parity, stop bits
    greeting = b""                     # sent when a raw socket connects
    echo = False
    error_format = '{code:+d},"{text}"'
    usb_ids = ""                       # "vid:pid" the bench gives a USB-serial port by default

    def __init__(self, name: str, cfg: Dict[str, Any], world: Any):
        self.name, self.cfg, self.world = name, dict(cfg or {}), world
        self.random = random.Random(hash(name) & 0xFFFFFFFF)
        self.serial_no = str(self.cfg.get("serial_number") or self.default_serial)
        self.inbuf = bytearray()
        self.out: deque = deque()
        self.errors: deque = deque()
        self.esr = self.ese = self.sre = 0
        self.powered = True
        self.faults: set = set()
        self.busy_until = 0.0
        self.now = time.monotonic()
        self.bus = "gpib"
        self._delay = 0.0
        self._ctx: Optional[str] = None
        self.traffic: deque = deque(maxlen=400)   # what came in and went out, for `labsim log`
        self.commands = self._table()
        self.setup()
        self.reset()

    # ---- what models override
    def setup(self) -> None:
        """Once, at start: wiring and fixed properties."""

    def reset(self) -> None:
        """*RST and power-on: the settings a fresh instrument has."""

    def tick(self, dt: float, now: float) -> None:
        """Called by the server about 20 times a second: ramps, filters, PID loops."""

    def trigger(self) -> None:
        pass

    def on_open(self, bus: str, now: float) -> None:
        """A controller opened a session (a raw socket connected: some instruments greet it first)."""
        if bus == "socket" and self.greeting:
            self.out.append(Chunk(self.greeting, now + 0.05, False))

    def status(self) -> Dict[str, Any]:
        """What `labsim status` shows."""
        return {}

    # ---- the command table
    def _table(self) -> List[Tuple["re.Pattern[str]", Callable, bool, bool]]:
        table, seen = [], set()
        for klass in type(self).__mro__:
            for attr, fn in vars(klass).items():
                if attr in seen:
                    continue
                seen.add(attr)            # a subclass's attribute of the same name hides the parent's command
                if not hasattr(fn, "_labsim"):
                    continue
                for pattern, query, setter, group in fn._labsim:
                    if group == "488" and not self.has_488 or group == "err" and not self.has_syst_err:
                        continue
                    table.append((header_regex(pattern), getattr(self, attr), query, setter))
        return table

    def match(self, header: str) -> Tuple[Optional[Tuple[Any, ...]], Tuple[Optional[int], ...]]:
        for rx, fn, query, setter in self.commands:
            m = rx.fullmatch(header)
            if m:
                return (fn, query, setter), tuple(int(g) if g else None for g in m.groups())
        return None, ()

    def lookup(self, header: str) -> Tuple[Optional[Tuple[Any, ...]], Tuple[Optional[int], ...]]:
        """SCPI path rules: after ';' a header without a leading ':' continues from the last one's parent node.
        Like many real parsers this one also tries the header from the root when that finds nothing."""
        h = header.upper()
        if h.startswith("*"):
            return self.match(h)
        absolute = h.startswith(":")
        h = h.lstrip(":")
        tries = [h] if absolute or not self._ctx else [self._ctx + ":" + h, h]
        for cand in tries:
            entry, chans = self.match(cand)
            if entry:
                self._ctx = cand.rsplit(":", 1)[0] if ":" in cand else None
                return entry, chans
        return None, ()

    def handle(self, text: str) -> Any:
        """One message (up to its terminator): its commands in order, the replies of its queries joined by ';'."""
        replies: List[str] = []
        self._ctx = None
        for unit in split_units(text):
            unit = unit.strip()
            if not unit:
                continue
            m = re.match(r"^(\S+?)(\?)?(?:\s+(.*))?$", unit, re.S)
            if not m:
                raise fail(UNDEFINED, unit)
            header, q, rest = m.group(1), bool(m.group(2)), m.group(3) or ""
            entry, chans = self.lookup(header)
            if entry is None or (q and not entry[1]) or (not q and not entry[2]):
                raise fail(UNDEFINED, unit)
            reply = entry[0](q, Args(split_params(rest, self.param_split), rest), chans)
            if q:
                replies.append(reply if isinstance(reply, bytes) else "" if reply is None else str(reply))
        if any(isinstance(r, bytes) for r in replies):   # a binary block (IEEE 488.2 #...) among the replies
            return b";".join(r if isinstance(r, bytes) else r.encode("latin-1") for r in replies)
        return ";".join(replies) if replies else None

    # ---- the device's end of the line
    def reachable(self) -> bool:
        return self.powered and "unplugged" not in self.faults

    def feed(self, data: bytes, end: bool, bus: str, now: float) -> None:
        """Bytes arriving from a controller; end: EOI / END asserted with the last one."""
        if not self.reachable() or "dead" in self.faults:
            return
        self.now, self.bus = now, bus
        if self.echo:
            self.out.append(Chunk(bytes(data), now, False))
        self.inbuf += data
        while True:
            hits = [(self.inbuf.find(t), -len(t)) for t in self.terminators if self.inbuf.find(t) >= 0]
            if not hits:
                break
            idx, neg = min(hits)
            message = bytes(self.inbuf[:idx])
            del self.inbuf[:idx - neg]
            self.dispatch(message, now)
        if end and bus in MESSAGE_BUSES and self.inbuf:
            message = bytes(self.inbuf)
            self.inbuf.clear()
            self.dispatch(message, now)

    def dispatch(self, raw: bytes, now: float) -> None:
        text = raw.decode("latin-1").strip()
        if not text:
            return
        self.traffic.append((time.strftime("%H:%M:%S"), "<", text))
        # 488.2 message exchange (GPIB, USB-TMC, VXI-11, HiSLIP): the instrument holds its reply until it is read and
        # drops it when a new command comes first. On a raw socket or a serial line what it sent is already on the
        # wire, in the computer's buffer, and stays there to be read.
        if self.drops_unread and self.bus in MESSAGE_BUSES and not self.echo and any(c.eom for c in self.out):
            self.out.clear()
            self.push_error(*INTERRUPTED)
        self._delay = 0.0
        try:
            reply = self.handle(text)
        except CommandError as e:
            self.push_error(e.code, e.text)
            self.traffic.append((time.strftime("%H:%M:%S"), "!", "%d,%s" % (e.code, e.text)))
            reply = None
        self.send(reply, now)

    def send(self, reply: Any, now: Optional[float] = None, delay: Optional[float] = None) -> None:
        if reply is None:
            return
        now = self.now if now is None else now
        data = reply if isinstance(reply, bytes) else str(reply).encode("latin-1", "replace")
        if "garble" in self.faults:
            data = bytes(b ^ (1 << self.random.randrange(7)) if self.random.random() < 0.3 else b for b in data)
        ready = now + (self._delay if delay is None else delay) + (2.0 if "slow" in self.faults else 0.0)
        self.traffic.append((time.strftime("%H:%M:%S"), ">", data.decode("latin-1")[:300]))
        self.out.append(Chunk(data + self.reply_end, ready, True))

    def take(self, count: int, term: Optional[int], end_ok: bool, now: float) -> Tuple[bytes, Optional[str]]:
        data = bytearray()
        while self.out and len(data) < count:
            c = self.out[0]
            if c.ready > now:
                break
            seg = c.data[c.pos:c.pos + count - len(data)]
            if term is not None:
                k = seg.find(bytes((term,)))
                if k >= 0:
                    data += seg[:k + 1]
                    c.pos += k + 1
                    eom = c.pos >= len(c.data) and c.eom
                    if c.pos >= len(c.data):
                        self.out.popleft()
                    return bytes(data), ("end" if eom and end_ok else "term")
            data += seg
            c.pos += len(seg)
            if c.pos >= len(c.data):
                self.out.popleft()
                if c.eom and end_ok:
                    return bytes(data), "end"
        if len(data) >= count:
            return bytes(data), "max"
        return bytes(data), None

    def next_ready(self, now: float) -> Optional[float]:
        return max(0.0, self.out[0].ready - now) if self.out else None

    def device_clear(self) -> None:
        self.inbuf.clear()
        self.out.clear()

    def power(self, on: bool) -> None:
        self.inbuf.clear()
        self.out.clear()
        self.errors.clear()
        self.esr = 0
        self.powered = on
        if on:
            self.reset()

    # ---- status
    def push_error(self, code: int, text: str) -> None:
        if code < 0:
            self.esr |= {1: 32, 2: 16, 3: 8, 4: 4}.get(-code // 100, 8)
        else:
            self.esr |= 8
        if not self.has_syst_err:
            return
        if len(self.errors) >= 10:
            self.errors[-1] = OVERFLOW
            return
        self.errors.append((code, text))

    def stb(self) -> int:
        v = (4 if self.errors else 0) | (16 if self.out else 0) | (32 if self.esr & self.ese else 0)
        return v | (64 if v & self.sre else 0)

    def delay(self, seconds: float) -> None:
        """The reply now being made is ready only after this long (a measurement's integration time)."""
        self._delay += max(0.0, seconds)

    def identity(self) -> str:
        return str(self.cfg.get("idn") or self.idn.format(serial=self.serial_no))

    # ---- IEEE 488.2 common commands and the SCPI error queue
    @command("*IDN", setter=False, group="488")
    def _idn(self, q, a, ch):
        return self.identity()

    @command("*RST", query=False, group="488")
    def _rst(self, q, a, ch):
        self.reset()

    @command("*CLS", query=False, group="488")
    def _cls(self, q, a, ch):
        self.errors.clear()
        self.esr = 0

    @command("*ESR", setter=False, group="488")
    def _esr(self, q, a, ch):
        v, self.esr = self.esr, 0
        return str(v)

    @command("*ESE", group="488")
    def _ese(self, q, a, ch):
        if q:
            return str(self.ese)
        self.ese = a.int(0, 0, 255)

    @command("*SRE", group="488")
    def _sre(self, q, a, ch):
        if q:
            return str(self.sre)
        self.sre = a.int(0, 0, 255)

    @command("*STB", setter=False, group="488")
    def _stb(self, q, a, ch):
        return str(self.stb())

    @command("*OPC", group="488")
    def _opc(self, q, a, ch):
        if q:
            self.delay(max(0.0, self.busy_until - self.now))
            return "1"
        self.esr |= 1

    @command("*WAI", query=False, group="488")
    def _wai(self, q, a, ch):
        self.delay(max(0.0, self.busy_until - self.now))

    @command("*TST", setter=False, group="488")
    def _tst(self, q, a, ch):
        self.delay(1.5)
        return "0"

    @command("*TRG", query=False, group="488")
    def _trg(self, q, a, ch):
        self.trigger()

    @command("SYSTem:ERRor[:NEXT]", setter=False, group="err")
    def _syst_err(self, q, a, ch):
        code, text = self.errors.popleft() if self.errors else (0, "No error")
        return self.error_format.format(code=code, text=text)

    @command("SYSTem:ERRor:COUNt", setter=False, group="err")
    def _syst_err_count(self, q, a, ch):
        return str(len(self.errors))

    @command("SYSTem:VERSion", setter=False, group="err")
    def _syst_ver(self, q, a, ch):
        return "1999.0"

    # ---- helpers for models
    def noise(self, sigma: float) -> float:
        return self.random.gauss(0.0, sigma * (10.0 if "noisy" in self.faults else 1.0))

    def opt(self, key: str, default: Any) -> Any:
        return self.cfg.get(key, default)


def onoff(v: bool) -> str:
    return "1" if v else "0"


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def db(x: float) -> float:
    return 20.0 * math.log10(max(abs(x), 1e-30))
