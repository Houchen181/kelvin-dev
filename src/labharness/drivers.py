"""Instruments driven through their vendor's software instead of text commands over VISA.

A card for such an instrument still has command templates, in a small neutral language its driver translates:
    "SET <path> {value}"   and   "GET <path>"
so the safety gate (limits, ramps, approvals, raw-command checks), the twins and the run engine treat it exactly
like a SCPI instrument. The address names the driver:
    zhinst://dev3456                      Zurich Instruments (MFLI, MFIA, HF2LI, UHFLI...) through LabOne (zhinst-core)
    zhinst://dev3456?host=10.0.0.5&port=8004&interface=1GbE
    qd://192.168.1.20:5000                Quantum Design PPMS, DynaCool, VersaLab, MPMS3 through MultiPyVu
    m2://10.0.0.8:9360                    M Squared lasers (SolsTiS...): JSON over TCP (m2.py)
    toptica://10.0.0.9                    TOPTICA DLC pro: its command line (decop.py)
    qick://10.0.0.50:8888/myqick          RFSoC boards running QICK, through its Pyro4 server (rfsoc.py)
    http://10.0.0.7:20022/v1              instruments with a web API, e.g. a PHAROS laser (webapi.py)
    lib://COM4?python=...&path=...        the maker's Python module or DLL, as its example scripts use it (library.py)
    nidaq://Dev1                          National Instruments DAQ cards through NI-DAQmx (daq.py)
    seabreeze://<serial>                  Ocean Insight spectrometers through python-seabreeze (spectra.py)
    shamrock://0                          Andor Shamrock / Kymera spectrographs through Andor's DLL (spectra.py)
    picam://<serial>                      Princeton Instruments cameras (PyLoN...) through PICam's DLL (princeton.py)
    lightfield://<experiment>             Princeton Instruments cameras and spectrographs through LightField's
                                          automation (lightfield.py)
    pylon://<serial>                      Basler cameras through pypylon (cameras.py)
    andor://0?sdk=3                       Andor cameras through Andor's pyAndorSDK3 / pyAndorSDK2 (cameras.py)
    anc300://COM5, anc350://0             attocube positioners: the ANC300's command line, the ANC350 through pylablib
    modbus://192.168.1.50?unit=1          Modbus TCP or RTU (modbus://COM5?unit=1&baud=9600): registers (modbus.py)
    uvc://0, genicam://<serial>           USB video cameras (OpenCV), GenICam cameras (Harvesters) (cameras.py)

The makers' libraries are not in the app: Kelvin installs the one an instrument needs into its drivers environment
(envs.py) once a person approves, and says so when one is missing. From source, pip install them."""
from __future__ import annotations

import math
import re
import threading
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from . import envs
from .adapters import AdapterError
from .cards import Card

SCHEMES = ("zhinst", "qd", "pulsestreamer", "pulseblaster", "timetagger", "http", "https", "m2", "toptica", "qick", "lib",
           "nidaq", "seabreeze", "shamrock", "picam", "lightfield", "pylon", "andor", "anc300", "anc350", "modbus",
           "uvc", "genicam")
_COMMAND = re.compile(r"^\s*(SET|GET)\s+(\S+)(?:\s+(\S+))?\s*$", re.I)


def is_driver_address(address: str) -> bool:
    return address.split("://", 1)[0].lower() in SCHEMES and "://" in address


def parse_command(command: str) -> Tuple[str, str, Optional[str]]:
    m = _COMMAND.match(command or "")
    if not m:
        raise AdapterError("not a driver command: %r (expected SET <path> <value> or GET <path>)" % command)
    verb, path, value = m.group(1).upper(), m.group(2), m.group(3)
    if verb == "SET" and value is None:
        raise AdapterError("SET %s needs a value" % path)
    return verb, path, value


def _number(text: str) -> Any:
    low = text.strip().lower()
    if low in ("on", "true"):
        return 1
    if low in ("off", "false"):
        return 0
    try:
        v = float(text)
    except ValueError:   # a word (a protocol) or name=value pairs (wiring): the driver reads it
        return text.strip()
    return int(v) if v.is_integer() and "." not in text and "e" not in low else v


def _fmt(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):   # a trace: a spectrum, its wavelengths
        return ",".join("%.8g" % float(x) for x in v)
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    return "%.12g" % float(v)


class DriverBackend:
    """write(command) / query(command) / close(), like a VISA backend or a twin."""
    simulated = False

    def __init__(self, name: str, card: Card, address: str):
        self.name = name
        self.card = card
        self.address = address
        self.lock = threading.Lock()

    def write(self, command: str) -> None:
        verb, path, value = parse_command(command)
        if verb != "SET":
            raise AdapterError("%s: %r reads, it does not set" % (self.name, command))
        with self.lock:
            self.set(path.lower(), _number(value))   # cards write paths in capitals; vendors use lower case

    def query(self, command: str) -> str:
        if command.strip().upper() in ("*IDN?", self.card.idn_query.strip().upper()):
            with self.lock:
                return self.identity()
        verb, path, _ = parse_command(command)
        if verb != "GET":
            raise AdapterError("%s: %r sets, it does not read" % (self.name, command))
        with self.lock:
            return _fmt(self.get(path.lower()))

    def set(self, path: str, value: Any) -> None:
        raise NotImplementedError

    def get(self, path: str) -> Any:
        raise NotImplementedError

    def identity(self) -> str:
        raise NotImplementedError

    def close(self) -> None:
        pass


# ------------------------------------------------------------------ Zurich Instruments (LabOne)

class Zhinst(DriverBackend):
    """Paths are LabOne nodes below the device, e.g. /oscs/0/freq. A demodulator sample field is read as
    /demods/0/sample.x (x, y, r, theta in degrees, frequency)."""
    SAMPLE = re.compile(r"^(/demods/\d+/sample)\.(x|y|r|theta|frequency)$")

    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        url = urllib.parse.urlparse(address)
        self.device = (url.netloc or url.path).lower()
        if not re.match(r"^dev\d+$", self.device):
            raise AdapterError("%s: a Zurich Instruments address is zhinst://dev1234 (the device id), not %r"
                               % (name, address))
        q = dict(urllib.parse.parse_qsl(url.query))
        if module is None:
            try:
                import zhinst.core as module   # type: ignore
            except ImportError as e:
                raise AdapterError("%s needs the LabOne Python API: %s" % (name, envs.missing("zhinst-core"))) from e
        try:
            self.daq = module.ziDAQServer(q.get("host", "localhost"), int(q.get("port", 8004)), 6)
            if q.get("interface", "1GbE"):
                self.daq.connectDevice(self.device, q.get("interface", "1GbE"))
        except Exception as e:
            raise AdapterError("%s: could not reach %s through the LabOne data server: %s" % (name, self.device, e)) from e

    def _node(self, path: str) -> str:
        if not path.startswith("/") or ".." in path:
            raise AdapterError("%s: %r is not a node path" % (self.name, path))
        return "/%s%s" % (self.device, path.lower())

    def set(self, path: str, value: Any) -> None:
        self.daq.set(self._node(path), value)

    def get(self, path: str) -> Any:
        m = self.SAMPLE.match(path)
        if m:
            node = self._node(m.group(1))
            sample = self.daq.getSample(node)
            first = lambda key: float(list(sample[key])[0])   # noqa: E731 - arrays of one value
            x, y = first("x"), first("y")
            return {"x": x, "y": y, "r": math.hypot(x, y), "theta": math.degrees(math.atan2(y, x)),
                    "frequency": first("frequency")}[m.group(2)]
        node = self._node(path)
        try:
            return self.daq.getDouble(node)
        except Exception:
            return self.daq.getInt(node)

    def identity(self) -> str:
        try:
            kind = self.daq.getString("/%s/features/devtype" % self.device)
        except Exception:
            kind = "?"
        return "Zurich Instruments,%s,%s" % (kind, self.device)


def discover_zhinst(module: Any = None) -> List[Tuple[str, str]]:
    """(address, identity) of every Zurich Instruments device LabOne can see. Only asks; sets nothing."""
    if module is None:
        try:
            import zhinst.core as module   # type: ignore
        except ImportError:
            return []
    found = []
    try:
        disc = module.ziDiscovery()
        for dev in disc.findAll():
            props = disc.get(dev)
            found.append(("zhinst://%s" % str(dev).lower(),
                          "Zurich Instruments,%s,%s" % (props.get("devicetype", "?"), str(dev).lower())))
    except Exception:
        return []
    return found


# ------------------------------------------------------------------ Quantum Design (MultiPyVu)

class MultiVu(DriverBackend):
    """Paths: temperature, temperature/setpoint, temperature/rate (K/min); field, field/setpoint, field/rate (T/s;
    MultiVu works in oersted, 1 T = 10 000 Oe); chamber. Setting a setpoint starts the approach at the rate
    set last (MultiVu itself ramps)."""
    OE_PER_T = 1e4

    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        url = urllib.parse.urlparse(address)
        self.host = url.hostname or "localhost"
        self.port = url.port or 5000
        if module is None:
            try:
                import MultiPyVu as module   # type: ignore
            except ImportError as e:
                raise AdapterError("%s needs Quantum Design's MultiPyVu (and its server running next to MultiVu): %s"
                                   % (name, envs.missing("MultiPyVu"))) from e
        try:
            self.client = module.Client(host=self.host, port=self.port)
            self.client.open()
        except Exception as e:
            raise AdapterError("%s: could not reach the MultiPyVu server at %s:%d: %s" % (name, self.host, self.port, e)) from e
        self.t_rate = 1.0          # K/min
        self.b_rate = 0.001        # T/s
        self.t_set: Optional[float] = None
        self.b_set: Optional[float] = None

    def set(self, path: str, value: Any) -> None:
        c = self.client
        if path == "temperature/rate":
            self.t_rate = float(value)
        elif path == "field/rate":
            self.b_rate = float(value)
        elif path == "temperature/setpoint":
            c.set_temperature(float(value), self.t_rate, c.temperature.approach_mode.fast_settle)
            self.t_set = float(value)
        elif path == "field/setpoint":
            c.set_field(float(value) * self.OE_PER_T, self.b_rate * self.OE_PER_T, c.field.approach_mode.linear)
            self.b_set = float(value)
        else:
            raise AdapterError("%s: MultiVu has no setting %r" % (self.name, path))

    def get(self, path: str) -> Any:
        c = self.client
        if path == "temperature":
            return float(c.get_temperature()[0])
        if path == "field":
            return float(c.get_field()[0]) / self.OE_PER_T
        if path == "temperature/setpoint":
            return self.t_set if self.t_set is not None else float(c.get_temperature()[0])
        if path == "field/setpoint":
            return self.b_set if self.b_set is not None else float(c.get_field()[0]) / self.OE_PER_T
        if path == "temperature/rate":
            return self.t_rate
        if path == "field/rate":
            return self.b_rate
        if path == "chamber":
            return str(c.get_chamber())
        raise AdapterError("%s: MultiVu has no reading %r" % (self.name, path))

    def identity(self) -> str:
        return "Quantum Design,MultiVu,%s:%d" % (self.host, self.port)

    def close(self) -> None:
        try:
            self.client.close_client()
        except Exception:
            pass


# ------------------------------------------------------------------ pulse generators and photon counters (spin experiments)

class PulseGeneratorDriver(DriverBackend):
    """A pulse generator that plays one of pulses.PROTOCOLS, or a written sequence (pulses.LIBRARY, by its name in
    `sequence`), in a loop. Paths: protocol, sequence, tau, t_pi, t_laser, t_gate, t_wait, running (play / stop),
    period and mw_duty (read). The card's on_connect says the wiring:
        SET wiring laser=0,mw=1,gate=2,mw_y=3   which output drives the AOM, the MW switch, the counter gate (and
                                                any other channel a written sequence names; none: not wired)
        SET delays laser=4.5e-7                 how late each channel's device responds (moved earlier)
        SET limits max_mw_duty=0.2              stricter than the default 50 % (resolution comes from the model)
    A sequence that fails pulses.check, or that names a channel no output is wired to, is refused before it reaches
    the hardware. Subclasses only play and stop."""
    RESOLUTION = 1e-9
    MAX_SEGMENTS: Optional[int] = None   # how many segments (instructions) a period may have, where the model says
    TIMES = ("tau", "t_pi", "t_laser", "t_gate", "t_wait")

    def __init__(self, name: str, card: Card, address: str):
        super().__init__(name, card, address)
        from . import pulses
        self.state: Dict[str, Any] = dict(pulses.DEFAULTS, protocol="pulsed_odmr", sequence=pulses.NONE, running=0)
        self.wiring: Dict[str, int] = {"laser": 0, "mw": 1, "gate": 2}
        self.delays: Dict[str, float] = {}
        self.limits: Dict[str, float] = {"resolution": self.RESOLUTION}
        if self.MAX_SEGMENTS:
            self.limits["max_segments"] = self.MAX_SEGMENTS

    @staticmethod
    def _pairs(text: str) -> Dict[str, str]:
        out = {}
        for part in str(text).split(","):
            k, sep, v = part.partition("=")
            if not sep or not k.strip():
                raise AdapterError("expected name=value pairs, got %r" % text)
            out[k.strip().lower()] = v.strip()
        return out

    def sequence(self) -> Any:
        from . import pulses
        return pulses.make(str(self.state["protocol"]), self.state.get("sequence"),
                           {k: float(self.state[k]) for k in self.TIMES}, self.delays)

    def set(self, path: str, value: Any) -> None:
        from . import pulses
        if path in ("wiring", "delays", "limits"):
            pairs = self._pairs(value)
            if path == "wiring":
                bad = sorted(k for k in pairs if not pulses.CHANNEL.match(k) or k in pulses.RESERVED)
                if bad:
                    raise AdapterError("%s: %s is not a channel name (lower-case letters, digits and _)"
                                       % (self.name, ", ".join(bad)))
                for k, v in pairs.items():
                    if v.lower() in (pulses.NONE, "-", ""):   # not wired (a card's spare channel)
                        self.wiring.pop(k, None)
                    else:
                        self.wiring[k] = int(v)
            elif path == "delays":
                self.delays.update({k: float(v) for k, v in pairs.items()})
            else:
                self.limits.update({k: float(v) for k, v in pairs.items()})
            return
        if path == "protocol":
            value = str(value).lower()
            if value not in pulses.PROTOCOLS:
                raise AdapterError("%s: no protocol %r (have: %s)" % (self.name, value, ", ".join(pulses.PROTOCOLS)))
        elif path == "sequence":
            value = str(value).strip().lower() or pulses.NONE
            if value != pulses.NONE and value not in pulses.LIBRARY:
                raise AdapterError("%s: no sequence %r (saved: %s; none plays the protocol)"
                                   % (self.name, value, ", ".join(sorted(pulses.LIBRARY)) or "none yet"))
        elif path in self.TIMES:
            value = float(value)
        elif path != "running":
            raise AdapterError("%s: no setting %r" % (self.name, path))
        old = dict(self.state)
        self.state[path] = value
        playing = bool(int(self.state["running"]))
        if not playing:
            if old.get("running"):
                self.stop()
            return
        try:
            seq = self.sequence()
            problems = pulses.check(seq, self.limits)
        except (KeyError, ValueError) as e:   # a written sequence these times can't build (a negative wait)
            seq, problems = None, [str(e.args[0] if e.args else e)]
        unwired = sorted(ch for ch, spans in (seq.channels.items() if seq is not None else []) if spans
                         and ch not in self.wiring)
        if unwired:   # it would play without them: fail closed
            problems.append("the sequence uses %s, which no output is wired to (wiring: %s)" % (
                ", ".join(unwired), ", ".join("%s=%d" % kv for kv in sorted(self.wiring.items()))))
        if problems:
            self.state = old
            raise AdapterError("%s: refused: %s" % (self.name, problems[0]))
        self.play(seq)

    def get(self, path: str) -> Any:
        if path == "period":
            return self.sequence().period
        if path == "mw_duty":
            return self.sequence().mw_duty()
        if path in self.state:
            return self.state[path]
        raise AdapterError("%s: no reading %r" % (self.name, path))

    def channels(self, seq: Any) -> Dict[int, List[Tuple[int, int]]]:
        """Output number -> (duration ns, level) pattern for each wired channel."""
        return {self.wiring[ch]: seq.pattern(ch) for ch in seq.channels if ch in self.wiring}

    def play(self, seq: Any) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        try:
            self.stop()
        except Exception:
            pass


class CounterDriver(DriverBackend):
    """A photon counter that reads the gate's alternating signal and reference windows. Paths: integration (s);
    signal, reference, contrast (one acquisition serves one reading of each); rate (counts/s, light on, no gating).
    Subclasses acquire(seconds) -> (signal, reference) and count_rate(seconds)."""

    def __init__(self, name: str, card: Card, address: str):
        super().__init__(name, card, address)
        self.integration = 0.5
        self._last: Optional[Dict[str, float]] = None
        self._read: set = set()

    def set(self, path: str, value: Any) -> None:
        if path != "integration":
            raise AdapterError("%s: no setting %r" % (self.name, path))
        if not 1e-3 <= float(value) <= 3600:
            raise AdapterError("%s: integration must be 1 ms to 1 h" % self.name)
        self.integration = float(value)

    def get(self, path: str) -> Any:
        if path == "integration":
            return self.integration
        if path == "rate":
            return self.count_rate(self.integration)
        if path not in ("signal", "reference", "contrast"):
            raise AdapterError("%s: no reading %r" % (self.name, path))
        if self._last is None or path in self._read:
            sig, ref = self.acquire(self.integration)
            self._last, self._read = {"signal": sig, "reference": ref, "contrast": sig / ref if ref else 0.0}, set()
        self._read.add(path)
        return self._last[path]

    def acquire(self, seconds: float) -> Tuple[float, float]:
        raise NotImplementedError

    def count_rate(self, seconds: float) -> float:
        raise NotImplementedError


PLAYING: Dict[str, Any] = {}   # the sequence each pulse generator plays: the counter needs its windows' order


class PulseStreamer(PulseGeneratorDriver):
    """Swabian Instruments Pulse Streamer 8/2 through its `pulsestreamer` client. Address pulsestreamer://<ip or
    hostname>. Outputs 0-7; 1 ns steps, 2 ns shortest pulse. The client checks inputs with Python asserts (gone under
    python -O), so this driver checks everything itself first."""
    RESOLUTION = 2e-9
    OUTPUTS = range(8)

    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        host = address.split("://", 1)[1].split("?")[0].strip("/")
        if not host:
            raise AdapterError("%s: a Pulse Streamer address is pulsestreamer://<ip or hostname>" % name)
        if module is None:
            try:
                import pulsestreamer as module   # type: ignore
            except ImportError as e:
                raise AdapterError("%s: %s" % (name, envs.missing("pulsestreamer"))) from e
        self.mod = module
        try:
            self.ps = module.PulseStreamer(host)
        except (AssertionError, OSError, Exception) as e:   # a failed connection is an AssertionError in the client
            raise AdapterError("%s: no Pulse Streamer at %s (%s)" % (name, host, e)) from e

    def play(self, seq: Any) -> None:
        chans = self.channels(seq)
        bad = [ch for ch in chans if ch not in self.OUTPUTS]
        if bad:
            raise AdapterError("%s: outputs are 0-7, not %s" % (self.name, bad))
        s = self.ps.createSequence()
        for ch, pattern in chans.items():
            if any(n < 0 or level not in (0, 1) for n, level in pattern):
                raise AdapterError("%s: bad pattern on output %d" % (self.name, ch))
            s.setDigital(ch, [(int(n), int(level)) for n, level in pattern])
        self.ps.stream(s, self.mod.PulseStreamer.REPEAT_INFINITELY, self.mod.OutputState.ZERO())
        PLAYING[self.name] = seq

    def stop(self) -> None:
        PLAYING.pop(self.name, None)
        self.ps.constant(self.mod.OutputState.ZERO())   # the vendor's way to stop: every output low

    def identity(self) -> str:
        try:
            return "Swabian Instruments,Pulse Streamer 8/2,%s,%s" % (self.ps.getSerial(), self.ps.getFirmwareVersion())
        except Exception:
            return "Swabian Instruments,Pulse Streamer 8/2,?,?"


class PulseBlaster(PulseGeneratorDriver):
    """SpinCore PulseBlaster through `spinapi` (a ctypes wrapper of SpinCore's SpinAPI; SpinAPI itself must be
    installed). Address pulseblaster://<board, 0-based>?clock=<core clock MHz>[&pro=1]. pro=1 is for the
    PulseBlasterESR-PRO, whose bits 21-23 must be 111 for ordinary pulses (on other models they are outputs). The
    shortest instruction is 5 clock cycles. Stopping loads an all-off program, since pb_stop may leave outputs high.
    A period has at most 4096 instructions (the ESR-PRO's memory; SET limits max_segments=... for a larger board)."""
    ON = 0xE00000
    MIN_CYCLES = 5
    MAX_SEGMENTS = 4096

    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        url = urllib.parse.urlparse(address)
        q = dict(urllib.parse.parse_qsl(url.query))
        try:
            self.board = int((url.netloc or url.path or "0").strip("/") or 0)
            self.clock = float(q.get("clock", ""))
        except ValueError:
            raise AdapterError("%s: a PulseBlaster address is pulseblaster://0?clock=500 (board, core clock in MHz)"
                               % name)
        self.pro = q.get("pro") in ("1", "true", "yes")
        self.limits["resolution"] = self.MIN_CYCLES / (self.clock * 1e6)
        if module is None:
            try:
                import spinapi as module   # type: ignore
            except ImportError as e:
                raise AdapterError("%s: %s; it also needs SpinCore's SpinAPI (SpinCore's installer)"
                                   % (name, envs.missing("spinapi"))) from e
        self.sp = module
        try:
            if self.sp.pb_count_boards() <= self.board:
                raise AdapterError("%s: no PulseBlaster board %d" % (name, self.board))
            self.sp.pb_select_board(self.board)
            self.sp.pb_init()
            self.sp.pb_core_clock(self.clock)
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError("%s: could not open PulseBlaster board %d: %s" % (name, self.board, e)) from e

    def _program(self, steps: List[Tuple[int, float]], loop: bool) -> None:
        sp = self.sp
        sp.pb_start_programming(sp.PULSE_PROGRAM)
        start = None
        for i, (flags, ns) in enumerate(steps):
            last = i == len(steps) - 1
            op, data = (sp.BRANCH, start if start is not None else 0) if (last and loop) else (sp.CONTINUE, 0)
            addr = sp.pb_inst_pbonly(flags | (self.ON if self.pro else 0), op, data, ns)
            if start is None:
                start = addr
        if not loop:
            sp.pb_inst_pbonly(self.ON if self.pro else 0, sp.STOP, 0, 1000.0)   # STOP keeps the (all-off) flags
        sp.pb_stop_programming()
        sp.pb_reset()
        sp.pb_start()

    def play(self, seq: Any) -> None:
        shortest = self.MIN_CYCLES * 1e3 / self.clock   # ns
        steps = []
        for duration, on in seq.segments():
            ns = duration * 1e9
            if ns < shortest - 1e-6:
                raise AdapterError("%s: two edges are %.1f ns apart, closer than the PulseBlaster's %.0f ns shortest "
                                   "instruction" % (self.name, ns, shortest))
            steps.append((sum(1 << self.wiring[ch] for ch in on if ch in self.wiring), ns))
        self._program(steps, loop=True)
        PLAYING[self.name] = seq

    def stop(self) -> None:
        PLAYING.pop(self.name, None)
        self._program([(0, 1000.0)], loop=False)

    def identity(self) -> str:
        try:
            version = self.sp.pb_get_version()
            version = version.decode() if isinstance(version, bytes) else str(version)
        except Exception:
            version = "?"
        return "SpinCore,PulseBlaster,board %d,%s" % (self.board, version)

    def close(self) -> None:
        super().close()
        try:
            self.sp.pb_close()
        except Exception:
            pass


class TimeTagger(CounterDriver):
    """Swabian Instruments Time Tagger (20, Ultra, X) through `Swabian-TimeTagger`. Address
    timetagger://<serial, or empty for the first>?click=1&gate=2[&level=0.5]: the detector input and the gate input
    (1-based; the gate's falling edge closes each window). Per period the gate opens a signal window, then a
    reference window. Which window comes first in the record is found from the generator's own timing.

    g2 (Hanbury Brown–Twiss): &click2=<the second detector's input>. g2 correlates click and click2 for the
    integration time (Swabian's Correlation, normalised to uncorrelated light), g2_delay is its delays (s), and
    g2_zero, g2_zero_err and g2_tau are the fit of that correlation (correlation.py): g2(0) with its error, and the
    antibunching time. Settings g2_binwidth (s) and g2_bins."""
    BINS = 2_000_000

    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        url = urllib.parse.urlparse(address)
        q = dict(urllib.parse.parse_qsl(url.query))
        self.serial = (url.netloc or url.path).strip("/")
        try:
            self.click, self.gate = int(q.get("click", 1)), int(q.get("gate", 2))
            self.click2 = int(q["click2"]) if q.get("click2") else None
        except ValueError:
            raise AdapterError("%s: click, click2 and gate are input numbers (1-based)" % name)
        self.binwidth, self.bins = 1e-10, 1000   # 100 ps bins, ±50 ns: an emitter's antibunching is ns wide
        self._g2: Optional[Dict[str, Any]] = None
        if module is None:
            try:
                from Swabian import TimeTagger as module   # type: ignore
            except ImportError:
                try:
                    import TimeTagger as module   # type: ignore  (the Windows installer's module)
                except ImportError as e:
                    raise AdapterError("%s: %s" % (name, envs.missing("Swabian-TimeTagger"))) from e
        self.tt = module
        try:
            self.tagger = module.createTimeTagger(self.serial)
            if q.get("level"):
                for ch in (self.click, self.gate):
                    self.tagger.setTriggerLevel(ch, float(q["level"]))
        except Exception as e:
            raise AdapterError("%s: could not open the Time Tagger %s: %s" % (name, self.serial or "", e)) from e

    def _gated(self) -> Any:
        if hasattr(self.tt, "GatedCounter"):
            return self.tt.GatedCounter(self.tagger, [self.click], self.gate, -self.gate, self.BINS)
        return self.tt.CountBetweenMarkers(self.tagger, self.click, self.gate, -self.gate, self.BINS)

    def acquire(self, seconds: float) -> Tuple[float, float]:
        m = self._gated()
        self.tagger.sync()
        m.startFor(int(seconds * 1e12), True)                 # picoseconds
        if not m.waitUntilFinished(int(seconds * 1000) + 10_000):   # milliseconds
            raise AdapterError("%s: the acquisition did not finish" % self.name)
        data = m.getData()   # GatedCounter: one row per click channel; CountBetweenMarkers: one row
        rows = data.tolist() if hasattr(data, "tolist") else list(data)
        counts = list(rows[0]) if rows and isinstance(rows[0], (list, tuple)) else rows
        widths = list(m.getBinWidths())
        index = list(m.getIndex()) if hasattr(m, "getIndex") else []
        filled = [i for i, w in enumerate(widths) if w > 0]
        if len(filled) < 2:
            raise AdapterError("%s: no gate windows arrived on input %d: is the pulse generator playing, and its gate "
                               "output wired there?" % (self.name, self.gate))
        start = self._first_signal(index, filled)
        sel = filled[start:]
        sel = sel[: len(sel) // 2 * 2]
        return float(sum(counts[i] for i in sel[0::2])), float(sum(counts[i] for i in sel[1::2]))

    @staticmethod
    def _first_signal(index: List[int], filled: List[int]) -> int:
        """0 if the record starts with a signal window, 1 if with a reference window: within one laser pulse the
        signal window opens (t_laser - t_gate) before the reference window, which the generator's timing gives."""
        seq = next(iter(PLAYING.values()), None)
        if seq is None or len(index) < 3:
            return 0
        (_, s0, _), (_, r0, _) = seq.windows
        expected = (r0 - s0) * 1e12                           # ps
        gap01 = index[filled[1]] - index[filled[0]]
        gap12 = index[filled[2]] - index[filled[1]]
        return 0 if abs(gap01 - expected) <= abs(gap12 - expected) else 1

    def count_rate(self, seconds: float) -> float:
        m = self.tt.Countrate(self.tagger, [self.click])
        m.startFor(int(seconds * 1e12), True)
        m.waitUntilFinished(int(seconds * 1000) + 10_000)
        return float(list(m.getData())[0])

    # ------------------------------------------------------------------ g2: correlations between two detectors

    G2 = ("g2", "g2_delay", "g2_zero", "g2_zero_err", "g2_tau")

    def set(self, path: str, value: Any) -> None:
        if path == "g2_binwidth":
            if not 1e-12 <= float(value) <= 1e-6:
                raise AdapterError("%s: g2_binwidth is 1 ps to 1 us" % self.name)
            self.binwidth, self._g2 = float(value), None
        elif path == "g2_bins":
            if not 10 <= int(value) <= 100_000:
                raise AdapterError("%s: g2_bins is 10 to 100000" % self.name)
            self.bins, self._g2 = int(value), None
        else:
            super().set(path, value)

    def get(self, path: str) -> Any:
        if path == "g2_binwidth":
            return self.binwidth
        if path == "g2_bins":
            return self.bins
        if path not in self.G2:
            return super().get(path)
        last = self._g2
        if path == "g2_delay":   # the delays depend on the bins alone: the latest correlation's, or the ones it will have
            return last["delay"] if last is not None else self._delays(None)
        # one correlation serves one reading of each (as the gated counts do): the trace and the fit of it read in one
        # plan step are of the same correlation, and the next step's (after a move) of a new one, however soon
        if path == "g2" or last is None or path in last["read"]:
            last = self._correlate(self.integration)
        last["read"].add(path)
        if path == "g2":
            return last["g2"]
        fit = last.get("fit")
        if fit is None:
            from .correlation import fit_g2
            try:
                fit = last["fit"] = fit_g2(last["delay"], last["g2"], last["counts"])
            except ValueError as e:
                raise AdapterError("%s: g2 not fitted: %s" % (self.name, e)) from e
        return {"g2_zero": fit["g2_zero"], "g2_zero_err": fit["g2_zero_err"], "g2_tau": fit["tau"]}[path]

    def _correlate(self, seconds: float) -> Dict[str, Any]:
        """One correlation of click and click2 for `seconds`: its normalised g2, delays (s) and raw coincidences."""
        if self.click2 is None:
            raise AdapterError("%s: g2 needs the second detector's input: add it to the address "
                               "(timetagger://%s?click=%d&click2=2)" % (self.name, self.serial, self.click))
        bw = max(1, int(round(self.binwidth * 1e12)))   # picoseconds
        m = self.tt.Correlation(self.tagger, self.click, self.click2, bw, self.bins)
        self.tagger.sync()
        m.startFor(int(seconds * 1e12), True)
        if not m.waitUntilFinished(int(seconds * 1000) + 10_000):
            raise AdapterError("%s: the correlation did not finish" % self.name)
        data = m.getData()
        counts = [float(c) for c in (data.tolist() if hasattr(data, "tolist") else data)]
        if hasattr(m, "getDataNormalized"):   # normalised to uncorrelated light: N1·N2·binwidth/T
            g2 = [float(v) for v in m.getDataNormalized()]
        else:
            edge = counts[: self.bins // 10] + counts[-(self.bins // 10):]
            mean = sum(edge) / len(edge) if edge else 0.0
            g2 = [c / mean if mean else 0.0 for c in counts]
        delay = self._delays(m)
        if not any(counts):
            raise AdapterError("%s: no coincidences between inputs %d and %d in %s s: are both detectors seeing light?"
                               % (self.name, self.click, self.click2, "%g" % seconds))
        self._g2 = {"g2": g2, "delay": delay, "counts": counts, "read": set()}
        return self._g2

    def _delays(self, m: Any) -> List[float]:
        """Each bin's delay (s): the measurement's own index, else centred on zero as Swabian's Correlation bins are."""
        if m is not None and hasattr(m, "getIndex"):
            return [float(t) * 1e-12 for t in m.getIndex()]
        bw = max(1, int(round(self.binwidth * 1e12)))
        return [(i - self.bins // 2) * bw * 1e-12 for i in range(self.bins)]

    def identity(self) -> str:
        try:
            return "Swabian Instruments,Time Tagger %s,%s" % (self.tagger.getModel(), self.tagger.getSerial())
        except Exception:
            return "Swabian Instruments,Time Tagger,%s" % self.serial

    def close(self) -> None:
        try:
            self.tt.freeTimeTagger(self.tagger)
        except Exception:
            pass


def discover_swabian(modules: Optional[Dict[str, Any]] = None) -> List[Tuple[str, str]]:
    """(address, identity) of the Pulse Streamers and Time Taggers this computer can see, through their libraries."""
    modules = modules if modules is not None else MODULES
    found: List[Tuple[str, str]] = []
    ps = modules.get("pulsestreamer")
    if ps is None:
        try:
            import pulsestreamer as ps   # type: ignore
        except ImportError:
            ps = None
    if ps is not None:
        try:
            for dev in ps.findPulseStreamers():
                found.append(("pulsestreamer://%s" % dev.ip, "Swabian Instruments,Pulse Streamer 8/2,%s" % dev.serial))
        except Exception:
            pass
    tt = modules.get("timetagger")
    if tt is None:
        try:
            from Swabian import TimeTagger as tt   # type: ignore
        except ImportError:
            try:   # the module Swabian's Windows installer puts on the path, as the driver opens it (l05)
                import TimeTagger as tt   # type: ignore
            except ImportError:
                tt = None
    if tt is not None:
        try:
            for serial in tt.scanTimeTagger():
                found.append(("timetagger://%s" % serial, "Swabian Instruments,Time Tagger,%s" % serial))
        except Exception:
            pass
    return found


from .webapi import HttpInstrument   # noqa: E402  (instruments with a web API: http:// and https:// addresses)

DRIVERS = {"zhinst": Zhinst, "qd": MultiVu, "pulsestreamer": PulseStreamer, "pulseblaster": PulseBlaster,
           "timetagger": TimeTagger, "http": HttpInstrument, "https": HttpInstrument}
MODULES: Dict[str, Any] = {}   # tests put fake vendor libraries here
LATER = {"m2": ("m2", "M2Laser"), "toptica": ("decop", "DeCoP"), "qick": ("rfsoc", "Qick"),
         "lib": ("library", "LibraryInstrument"), "nidaq": ("daq", "NIDAQ"),
         "seabreeze": ("spectra", "OceanSpectrometer"), "shamrock": ("spectra", "AndorSpectrograph"),
         "picam": ("princeton", "PicamCamera"), "lightfield": ("lightfield", "LightFieldCamera"),
         "pylon": ("cameras", "BaslerCamera"), "andor": ("cameras", "AndorCamera"),
         "anc300": ("positioners", "ANC300"), "anc350": ("positioners", "ANC350"),
         "modbus": ("modbus", "ModbusInstrument"), "uvc": ("cameras", "UsbCamera"),
         "genicam": ("cameras", "GenicamCamera")}   # they import this module


def library_notes() -> List[str]:
    """What the last scan saw through the makers' programs that say so, in words for its report: PICam (cameras
    free, or open in another program) and LightField. Nothing on a computer without Teledyne Princeton software."""
    from .lightfield import note as lightfield_note
    from .princeton import LAST
    lightfield = lightfield_note()
    picam = LAST.get("picam") or ""
    if picam.endswith("not installed here") and not lightfield:
        picam = ""
    return [n for n in (picam, lightfield) if n] + package_notes()


# a maker's program or driver (words in its name and maker), the package Kelvin reaches its instruments through, and
# what they are: with the program here and the package not, the scan says how Kelvin installs it
PACKAGE_HINTS = ((("ni-daqmx",), "nidaqmx", "NI DAQ cards", "NI-DAQmx"),
                 (("labone",), "zhinst-core", "Zurich Instruments' instruments", "LabOne"),
                 (("oceanview",), "seabreeze", "Ocean spectrometers", "OceanView"),
                 (("basler", "pylon"), "pypylon", "Basler cameras", "Basler's pylon"),
                 (("time tagger",), "Swabian-TimeTagger", "Swabian Time Taggers", "Swabian's Time Tagger software"),
                 (("multivu",), "MultiPyVu", "Quantum Design systems", "MultiVu"),
                 (("spinapi",), "spinapi", "SpinCore PulseBlasters", "SpinAPI"),
                 (("spinnaker",), "harvesters", "GenICam cameras (Teledyne FLIR)", "Spinnaker"),
                 (("vimba",), "harvesters", "GenICam cameras (Allied Vision)", "Vimba"),
                 (("ids", "peak"), "harvesters", "GenICam cameras (IDS)", "IDS peak"),
                 (("kinesis",), "pythonnet", "Thorlabs motion controllers, through Kinesis' .NET library",
                  "Thorlabs Kinesis"))


def package_notes(programs: Optional[List[Dict[str, str]]] = None) -> List[str]:
    """Makers' programs installed here whose instruments Kelvin reaches through a package it doesn't have yet."""
    from . import probe
    try:
        found = probe.programs() if programs is None else programs
    except Exception:
        return []
    text = [("%s %s" % (p.get("name", ""), p.get("publisher", ""))).lower() for p in found]
    out = []
    for words, package, what, program in PACKAGE_HINTS:
        if any(all(w in t for w in words) for t in text) and envs.have(package) is None:
            out.append("%s is installed on this computer; for %s, %s" % (program, what, envs.missing(package)))
    return out


def discover_libraries() -> List[Tuple[str, str]]:
    """(address, identity) of the instruments behind the vendor libraries installed here: NI DAQ cards, Princeton
    Instruments cameras, Andor spectrographs and sCMOS cameras, Ocean spectrometers, Basler cameras, attocube ANC350s.
    Each library only lists what it sees; nothing is opened or set."""
    from .cameras import discover_andor, discover_pylon
    from .daq import discover_nidaq
    from .positioners import discover_attocube
    from .princeton import discover_picam
    from .spectra import discover_seabreeze, discover_shamrock
    return (discover_nidaq(MODULES.get("nidaq")) + discover_picam(MODULES.get("picam"))
            + discover_shamrock(MODULES.get("shamrock")) + discover_andor(MODULES)
            + discover_seabreeze(MODULES.get("seabreeze")) + discover_pylon(MODULES.get("pylon"))
            + discover_attocube(MODULES.get("anc350")))


def open_driver(name: str, card: Card, address: str) -> DriverBackend:
    scheme = address.split("://", 1)[0].lower()
    if scheme in LATER and scheme not in DRIVERS:
        import importlib
        module, cls = LATER[scheme]
        DRIVERS[scheme] = getattr(importlib.import_module("." + module, __package__), cls)
    backend = DRIVERS[scheme](name, card, address, MODULES.get(scheme))
    for command in card.on_connect:   # configuration only (wiring, delays, limits); parse_card refuses drives here
        backend.write(command)
    return backend
