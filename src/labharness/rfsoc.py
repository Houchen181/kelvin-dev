"""RFSoC boards running QICK (the Quantum Instrumentation Control Kit), reached through its Pyro4 server.

Address: qick://<name server host>[:8888]/<proxy name>, as started on the board:
    pyro4-ns -n 0.0.0.0 -p 8888          then   python -m qick.pyro_cli myqick -n <host> -p 8888
Needs qick and Pyro4 on the computer that runs Kelvin (Kelvin installs them into its drivers environment once a
person approves: envs.py). The board's firmware may have a tProc v1 or v2: Kelvin reads which from the board and runs
programs written for it (the instrument's tproc var, 1 or 2, overrides that for a firmware it doesn't recognise).

The name server gives the address the board's server registered, which isn't always one this computer reaches: qick's
start_server, its name server on the board (ns_host "localhost"), registers the address of one network interface,
iface, eth0 unless told. A field report's RFSoC 4x2 was reached at 192.168.3.1 over its USB network (usb0) and
registered 192.168.2.99, its eth0's; every call then waited out its connection (2026-11-15). Kelvin looks up where the
server is before connecting (look_up, a few seconds at most). Where this computer surely can't reach it (cant_reach),
the connect fails at once with the fix; where it may not, Pyro tries, and its error says so. A call to the board that
gets no answer says why when Kelvin can tell (Qick._lost). The card's notes have a service that starts both at boot.

Kelvin plays one generator channel and reads one readout channel (set in the card's vars):
    output     drive: off sends soc.reset_gens(), which drives every generator with zeros
    gain       drive: amplitude, 0 to 1 of the generator's full scale (the lab sets its own cap)
    frequency  MHz;  nqz (Nyquist zone 1 or 2);  mixer (MHz, for generators with a digital mixer)
    mode       tone: a continuous tone while the output is on;  pulsed: one pulse per readout, during reads only
    length, readout_length, trigger_delay (us);  reps (averages)
    i, q, magnitude, phase   one averaged acquisition (QICK's acquire) per read, shared by the four values
A continuous tone keeps playing after a program ends, so Kelvin stops it whenever the output goes off, the run
ends or aborts (through the safe sequence), and when it disconnects.

Pyro4 pickles what goes between them (QICK sets it so), and the board's numpy can be older than Kelvin's (numpy 1.21 on
a PYNQ image): Kelvin reads what numpy 1.x pickles (read_old_numpy), and sends plain Python numbers and lists (Board)."""
from __future__ import annotations

import importlib
import importlib.util
import ipaddress
import logging
import math
import socket
import sys
import urllib.parse
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import envs
from .adapters import AdapterError, NoAnswer
from .cards import Card
from .drivers import DriverBackend

log = logging.getLogger("kelvin")

DEFAULTS = {"output": 0, "gain": 0.0, "frequency": 100.0, "nqz": 1, "mixer": 0.0, "mode": "tone", "length": 1.0,
            "readout_length": 1.0, "trigger_delay": 0.2, "reps": 100, "phase0": 0.0}
READINGS = ("i", "q", "magnitude", "phase")
# each call to the board waits at most this long (or the card's connection timeout_ms, if longer): Pyro's own default
# is forever, so a board that stopped answering held the lab for good. QICK's acquisitions are many short calls
CALL_S = 60.0
PYNQ_USB = "192.168.3.1"   # a PYNQ board on its USB network (usb0), as its image sets it up
REACH_S = 2.0              # how long a connection to where the board's server is may take before Kelvin says it can't
LOOKUP_S = 5.0             # how long the board's name server is waited for, to say where its QICK server is
# Pyro4's errors of the connection itself (no answer in time, refused, closed, no such name), never the board's own
PYRO_COMMS = ("CommunicationError", "TimeoutError", "ConnectionClosedError", "NamingError")
# The modules a numpy 1.x pickle names, which numpy 2 moved from numpy.core to numpy._core: an array is
# numpy.core.multiarray._reconstruct (numpy.core.numeric._frombuffer in pickle protocol 5), a numpy number
# numpy.core.multiarray.scalar, a ufunc one of numpy.core._multiarray_umath's, an error one of numpy.core._exceptions'.
# numpy 2 keeps numpy.core's modules only for old pickles and old imports; the app Kelvin installed had none of them
# (PyInstaller didn't see numpy's lazy imports, until packaging/kelvin.spec named them), and a later numpy drops them.
OLD_NUMPY = ("multiarray", "numeric", "_multiarray_umath", "umath", "_exceptions", "numerictypes", "fromnumeric",
             "records", "defchararray", "arrayprint", "function_base", "getlimits", "shape_base", "einsumfunc",
             "overrides", "_internal", "_dtype", "_dtype_ctypes")


def read_old_numpy() -> None:
    """Let this process read what numpy 1.x pickles, as a board's QICK sends its readings: each numpy.core module such
    a pickle names imports, as numpy._core's own module where numpy.core's is missing. Nothing changes where it is
    there, nor under numpy 1.x."""
    try:
        import numpy  # noqa: F401
    except ImportError:
        return
    for name in OLD_NUMPY:
        old = "numpy.core." + name
        try:
            if old in sys.modules or importlib.util.find_spec(old) is not None:
                continue
        except ImportError:   # a numpy with no numpy.core at all
            pass
        try:
            sys.modules[old] = importlib.import_module("numpy._core." + name)
        except ImportError:
            pass


def plain(value: Any) -> Any:
    """A value as plain Python, which the board's numpy reads whatever its version: numpy 2 pickles its arrays and
    numbers under numpy._core, which numpy 1.x doesn't have. An array becomes lists, a numpy number a Python one."""
    if isinstance(value, dict):
        return {plain(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [plain(v) for v in value]
        return items if isinstance(value, list) else tuple(items)
    if type(value).__module__.partition(".")[0] == "numpy" and callable(getattr(value, "tolist", None)):
        return value.tolist()
    return value


def pyro_comms(e: BaseException) -> bool:
    """Whether an error is Pyro4's own of the connection to the board (no answer in time, a connection refused or
    closed, a name the name server doesn't have), not one the board's QICK raised."""
    kind = type(e)
    return kind.__module__.startswith("Pyro4") and any(k.__name__ in PYRO_COMMS for k in kind.__mro__)


class Board:
    """The board's Pyro4 proxy, as Kelvin and QICK's programs call it: each call's arguments go as plain Python
    (plain()). What it answers comes as the board pickled it. Its attributes are the proxy's own (QICK checks
    _pyroUri, and then sends a pulse's samples as lists itself). A call that gets no answer (Pyro4's errors of the
    connection) is said by lost(error), when it can tell why: the error to raise instead, or None."""

    def __init__(self, proxy: Any, lost: Optional[Callable[[BaseException], Optional[BaseException]]] = None):
        object.__setattr__(self, "_kelvin_proxy", proxy)
        object.__setattr__(self, "_kelvin_lost", lost)

    def __getattr__(self, name: str) -> Any:
        if name in ("_kelvin_proxy", "_kelvin_lost"):   # not set yet: a copy being made
            raise AttributeError(name)
        try:   # a Pyro4 proxy may ask the board what it has, the first time
            attr = getattr(self._kelvin_proxy, name)
        except Exception as e:
            self._kelvin_said(e)
            raise
        if name.startswith("_") or not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return attr(*plain(args), **plain(kwargs))
            except Exception as e:
                self._kelvin_said(e)
                raise
        return call

    def _kelvin_said(self, e: BaseException) -> None:
        """Raises why the board didn't answer, in words with what to do, when lost() can tell; else nothing."""
        lost = self._kelvin_lost
        if lost is not None and pyro_comms(e):
            instead = lost(e)
            if instead is not None:
                raise instead from e

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._kelvin_proxy, name, value)


def look_up(ns_host: str, ns_port: int, proxy_name: str) -> Tuple[str, int]:
    """Where the board's Pyro4 name server says its QICK server is: the host and port the board's start_server
    registered (the proxy's URI). Pyro4's own lookup, as make_proxy's, with its serializer: QICK's name server takes
    pickle only (PYRO_SERIALIZERS_ACCEPTED), and this lookup comes before make_proxy sets it."""
    import Pyro4   # type: ignore
    Pyro4.config.SERIALIZER = "pickle"
    Pyro4.config.PICKLE_PROTOCOL_VERSION = 4
    with Pyro4.locateNS(host=ns_host, port=ns_port) as ns:
        uri = ns.lookup(proxy_name)
    return str(uri.host), int(uri.port)


def _ip(host: str) -> Optional[Any]:
    """A host as an IP address (its own, or what it resolves to here); None when it isn't one and doesn't resolve."""
    host = str(host or "").strip("[]")
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0])
    except (OSError, IndexError, UnicodeError, ValueError):
        return None


def takes_connection(host: str, port: int, seconds: float = REACH_S) -> bool:
    """Whether host:port takes a connection from this computer within `seconds` (closed at once; nothing sent)."""
    from . import probe
    try:
        sock = probe._connect(host, port, seconds)
    except OSError:
        return False
    try:
        sock.close()
    except OSError:
        pass
    return True


def networks_here() -> List[Dict[str, Any]]:
    """This computer's networks (probe.network_adapters): [] when they can't be listed."""
    from . import probe
    try:
        return list(probe.network_adapters())
    except Exception:
        return []


def _on(ip: Any, nets: List[Dict[str, Any]]) -> bool:
    """Whether an address is on one of these networks of this computer's."""
    for a in nets:
        try:
            if ip is not None and ip in ipaddress.ip_network(a["network"], strict=False):
                return True
        except (KeyError, TypeError, ValueError):
            continue
    return False


def cant_reach(host: str, port: int, ns_host: str) -> Tuple[str, bool]:
    """Whether this computer reaches the QICK server at host:port, where the board's name server at ns_host (which
    answered) says it is: ("", False) when it does (the name server's own address, an address on one of this
    computer's networks, or one that takes a connection); else (why, sure). Sure: the board's own loopback, or a private
    address on none of this computer's networks while the name server's is on one (the board's own other network,
    as eth0's 192.168.2.99 beside its usb0), which takes no connection twice (REACH_S, then 2.5 times as long): the
    connect then fails at once, with the fix. Otherwise not sure (a router between, a packet lost, a network this
    computer can't list): Pyro tries, the words kept for its error. One 2 s try once decided it (review of 2026-11-15),
    and a lost packet would have stopped a board that works."""
    ip, ns_ip = _ip(host), _ip(ns_host)
    if host == ns_host or (ip is not None and ip == ns_ip):
        return "", False
    if ip is not None and ip.is_loopback:   # the board's own, unless the name server is on this computer too
        return ("", False) if ns_ip is not None and ns_ip.is_loopback else \
            ("%s is the board's own loopback address" % host, True)
    nets = networks_here()
    if _on(ip, nets) or takes_connection(host, port):
        return "", False
    on = ", ".join("%s on %s" % (a.get("network"), a.get("name")) for a in nets if a.get("network"))
    why = "%s takes no connection from this computer%s" % (host, ", and is on none of its networks (%s)" % on
                                                           if on else "")
    sure = ip is not None and ip.is_private and _on(ns_ip, nets) and not takes_connection(host, port, REACH_S * 2.5)
    return why, sure


def _within(seconds: float, fn: Callable[..., Any], *args: Any) -> Tuple[str, Any]:
    """fn(*args) on a thread of its own, waited for `seconds` at most: ("ok", what it returned), ("error", what it
    raised), or ("late", None), the call left to end by itself. A name server's lookup waited Pyro's COMMTIMEOUT (a
    minute) on a board that didn't answer, after a call that had already failed (review of 2026-11-15)."""
    import threading
    box: Dict[str, Any] = {}
    done = threading.Event()

    def run() -> None:
        try:
            box["value"] = fn(*args)
        except BaseException as e:   # the caller's to say
            box["error"] = e
        finally:
            done.set()
    threading.Thread(target=run, name="kelvin-qick-lookup", daemon=True).start()
    if not done.wait(seconds):
        return "late", None
    return ("error", box["error"]) if "error" in box else ("ok", box.get("value"))


def load_qick() -> Any:
    try:
        import qick   # type: ignore
        from qick.pyro import make_proxy   # type: ignore
    except ImportError as e:
        raise AdapterError("QICK boards need qick, at the board's version where it is known, and Pyro4: %s"
                           % envs.missing("qick", "Pyro4")) from e
    try:   # every Pyro4 proxy, the name server's that make_proxy uses too, waits at most CALL_S for an answer, not forever
        import Pyro4   # type: ignore
        if not getattr(Pyro4.config, "COMMTIMEOUT", 0):
            Pyro4.config.COMMTIMEOUT = CALL_S
    except Exception:
        pass
    try:
        from qick.asm_v2 import AveragerProgramV2   # type: ignore
    except ImportError:   # a qick from before tProc v2, as a board with v1 firmware may run
        AveragerProgramV2 = None
    try:
        from qick import AveragerProgram   # type: ignore   # tProc v1
    except ImportError:
        AveragerProgram = None
    return SimpleNamespace(make_proxy=make_proxy, AveragerProgramV2=AveragerProgramV2, AveragerProgram=AveragerProgram,
                           version=str(getattr(qick, "__version__", "") or ""), lookup=look_up)


def generation(tprocs: Any) -> Optional[int]:
    """Which tProc the board's firmware has (1 or 2), from the board's own description; None if Kelvin can't tell."""
    kind = str(((tprocs or [{}])[0] or {}).get("type") or "")
    if not kind or kind == "qick_processor":
        return 2
    if kind.startswith("axis_tproc64x32"):
        return 1
    return None


def programs_v1(base: Any) -> Dict[str, Any]:
    """The same two programs for tProc v1 firmware, on the library's AveragerProgram: the gain in DAC units, the
    pulse length in the generator's clock cycles, the readout window in samples, the readout delay in the tProc's."""

    def declare(prog: Any, periodic: bool) -> None:
        cfg = prog.cfg
        gen, ro = cfg["gen"], cfg["ro"]
        kw = {"mixer_freq": cfg["mixer"]} if cfg["has_mixer"] else {}
        prog.declare_gen(ch=gen, nqz=cfg["nqz"], **kw)
        prog.declare_readout(ch=ro, length=prog.us2cycles(cfg["readout_length"], ro_ch=ro), freq=cfg["frequency"],
                             gen_ch=gen)
        prog.set_pulse_registers(ch=gen, style="const", freq=prog.freq2reg(cfg["frequency"], gen_ch=gen, ro_ch=ro),
                                 phase=prog.deg2reg(cfg["phase0"], gen_ch=gen), gain=cfg["gain_dac"],
                                 length=prog.us2cycles(cfg["length"], gen_ch=gen),
                                 mode="periodic" if periodic else "oneshot")
        prog.synci(200)   # time for the processor to set up before the first pulse

    class Tone(base):   # the tone starts once and repeats; each rep only reads
        def initialize(self):
            declare(self, True)
            self.pulse(ch=self.cfg["gen"], t=0)

        def body(self):
            self.trigger(adcs=[self.cfg["ro"]], pins=[0], adc_trig_offset=self.us2cycles(self.cfg["trigger_delay"]))
            self.wait_all()
            self.sync_all(self.us2cycles(1.0))

    class Probe(base):   # one pulse and one read per rep
        def initialize(self):
            declare(self, False)

        def body(self):
            cfg = self.cfg
            self.measure(pulse_ch=cfg["gen"], adcs=[cfg["ro"]], pins=[0],
                         adc_trig_offset=self.us2cycles(cfg["trigger_delay"]), wait=True,
                         syncdelay=self.us2cycles(1.0))

    return {"tone": Tone, "pulsed": Probe}


def programs(base: Any) -> Dict[str, Any]:
    """The two QICK programs Kelvin runs, on the library's own program class (tProc v2)."""

    def declare(prog: Any, cfg: Dict[str, Any], periodic: bool) -> None:
        gen, ro = cfg["gen"], cfg["ro"]
        kw = {"mixer_freq": cfg["mixer"]} if cfg["has_mixer"] else {}
        prog.declare_gen(ch=gen, nqz=cfg["nqz"], **kw)
        if cfg["tproc_readout"]:
            prog.declare_readout(ch=ro, length=cfg["readout_length"])
            prog.add_readoutconfig(ch=ro, name="ro", freq=cfg["frequency"], gen_ch=gen)
        else:
            prog.declare_readout(ch=ro, length=cfg["readout_length"], freq=cfg["frequency"], gen_ch=gen)
        pulse = dict(ch=gen, name="p", ro_ch=ro, style="const", freq=cfg["frequency"], length=cfg["length"],
                     phase=cfg["phase0"], gain=cfg["gain"])
        if periodic:
            pulse["mode"] = "periodic"
        prog.add_pulse(**pulse)
        if cfg["tproc_readout"]:
            prog.send_readoutconfig(ch=ro, name="ro", t=0)

    class Tone(base):   # the tone starts once and repeats; each rep only reads
        def _initialize(self, cfg):
            declare(self, cfg, True)
            self.pulse(ch=cfg["gen"], name="p", t=0)

        def _body(self, cfg):
            self.trigger(ros=[cfg["ro"]], pins=[0], t=cfg["trigger_delay"])

    class Probe(base):   # one pulse and one read per rep
        def _initialize(self, cfg):
            declare(self, cfg, False)

        def _body(self, cfg):
            self.pulse(ch=cfg["gen"], name="p", t=0)
            self.trigger(ros=[cfg["ro"]], pins=[0], t=cfg["trigger_delay"])

    return {"tone": Tone, "pulsed": Probe}


def _channels(text: Any) -> Dict[str, int]:
    pairs = dict(p.split("=", 1) for p in str(text).split(",") if "=" in p)
    try:
        return {k.strip(): int(v) for k, v in pairs.items()}
    except ValueError:
        raise AdapterError("channels take numbers: gen=0,ro=0")


class Qick(DriverBackend):
    call_s = None   # every call to the board waits at most its own time (Pyro's COMMTIMEOUT)
    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        url = urllib.parse.urlparse(address)
        proxy = url.path.strip("/")
        if not url.hostname or not proxy:
            raise AdapterError("%s: a QICK board's address is qick://<name server host>[:8888]/<proxy name>" % name)
        read_old_numpy()   # before the board's first answer: its numpy may be 1.x
        self.qick = module or load_qick()
        self.ns: Tuple[str, int, str] = (url.hostname, url.port or 8888, proxy)
        # where the board's name server says its server is, looked up first: one this computer can't reach is said at
        # once, with the fix, where make_proxy's first call waited out its connection (a minute) and said only that
        self.registered: Optional[Tuple[str, int]] = None
        self.doubt = ""   # an address it registered that this computer may not reach: said if Pyro can't either
        lookup = getattr(self.qick, "lookup", None)
        if lookup is not None:   # waited LOOKUP_S at most; no name server there, or no such server: make_proxy says
            state, got = _within(LOOKUP_S, lookup, url.hostname, url.port or 8888, proxy)
            self.registered = tuple(got) if state == "ok" else None   # type: ignore[assignment]
        if self.registered is not None:
            why, sure = cant_reach(self.registered[0], self.registered[1], url.hostname)
            if why and sure:
                raise AdapterError("%s: %s" % (name, self._cant_reach_words(self.registered, why)))
            if why:
                self.doubt = self._cant_reach_words(self.registered, why, "may not reach")
                log.warning("%s: %s; Pyro tries all the same", name, self.doubt)
        try:
            self.soc, self.soccfg = self.qick.make_proxy(ns_host=url.hostname, ns_port=url.port or 8888,
                                                         proxy_name=proxy)
        except Exception as e:   # Pyro's NamingError, CommunicationError...
            if self.doubt:
                raise AdapterError("%s: could not reach the QICK server %r (%s): %s" % (name, proxy, e, self.doubt)) \
                    from e
            raise AdapterError("%s: could not reach the QICK server %r through the name server at %s:%d (%s). On the "
                               "board, start the Pyro4 name server, then the QICK server under that name: on a PYNQ "
                               "board, in its Jupyter (http://%s:9090), run qick/pyro4/00_nameserver.ipynb, then "
                               "01_server.ipynb (Kelvin runs them with run_on_board once a person approves). Over the "
                               "board's USB network, start the server with iface='usb0' (the notebook's start_server "
                               "registers eth0's address, which this computer can't reach). A reboot ends both: the "
                               "card's notes (qick/rfsoc) have a service that starts them at every boot."
                               % (name, proxy, url.hostname, url.port or 8888, e, url.hostname)) from e
        try:   # Pyro4's proxy: each call times out (Pyro4.errors.TimeoutError) instead of waiting forever
            self.soc._pyroTimeout = max(CALL_S, float(card.connection.get("timeout_ms") or 0) / 1000.0)
        except Exception:
            pass
        # what Kelvin and QICK's programs send it: plain Python, which any numpy reads; a call that gets no answer says
        # why, when Kelvin can tell (_lost)
        self.soc = Board(self.soc, self._lost)
        # the board's QICK and this computer's should be the same version: Pyro hands objects over as they are
        self.versions = (str(self._cfg("sw_version") or ""), str(getattr(self.qick, "version", "") or ""))
        self.firmware = str(((self._cfg("tprocs") or [{}])[0] or {}).get("type") or "")
        self.found = generation(self._cfg("tprocs"))   # from the board; the tproc var overrides it (SET tproc)
        self.tproc: Optional[int] = None
        self.programs: Dict[str, Any] = {}
        self._use(self.found)
        self.state: Dict[str, Any] = dict(DEFAULTS, gen=0, ro=0)
        self.data: Dict[str, float] = {}
        self.taken: set = set()
        self.playing = False

    def _fix(self) -> str:
        """How to start the board's server so it registers an address this computer reaches, in words (qick's
        start_server: with its name server on the board, ns_host 'localhost', it registers the address of iface)."""
        ns_host, ns_port, proxy = self.ns
        usb = str(_ip(ns_host) or ns_host) == PYNQ_USB
        return ("qick's start_server, its name server on the board (ns_host='localhost'), registers the address of one "
                "network interface, iface, eth0 unless told; this computer reaches the board at %s%s. Start the server "
                "with %s, or with ns_host='%s' (qick.pyro_cli: -n %s, as it takes no iface). Kelvin runs it on the "
                "board with run_on_board once a person approves; the card's notes (qick/rfsoc) have a service that "
                "starts the name server and the server at every boot"
                % (ns_host, ", its USB network (usb0)" if usb else "",
                   "start_server(ns_host='localhost', ns_port=%d, proxy_name='%s', iface='usb0')" % (ns_port, proxy)
                   if usb else "iface set to the board's interface on that network (usb0 for its USB network, eth0 for "
                               "its Ethernet)", ns_host, ns_host))

    def _cant_reach_words(self, at: Tuple[str, int], why: str, reach: str = "can't reach") -> str:
        ns_host, ns_port, proxy = self.ns
        return ("the board's name server (%s:%d) has its QICK server %r at %s:%d, which this computer %s: %s. %s"
                % (ns_host, ns_port, proxy, at[0], at[1], reach, why, self._fix()))

    def _lost(self, e: BaseException) -> Optional[BaseException]:
        """A call to the board that got no answer (Pyro4's errors of the connection): why, in words with what to do,
        when Kelvin can tell (NoAnswer, its fix also kept for the alarm: Instrument.loss_text); None otherwise, and
        Pyro's own words go on. The field report's person started the name server and server again by hand three
        times, after reboots that ended the Jupyter kernels they ran in."""
        try:
            words = self._why_silent()
        except Exception:   # the look itself failed: Pyro's words, as they were
            return None
        if not words:
            return None
        out = NoAnswer("%s: %s" % (self.name, words))
        out.fix = words   # type: ignore[attr-defined]
        return out

    def _why_silent(self) -> str:
        """Why the board's server doesn't answer, when Kelvin can tell, in words; "" otherwise (a slow board): its
        name server doesn't answer either (the board off or restarting, or both stopped), it has no server of that
        name now, or another address (started again), or the server's address is one this computer can't reach."""
        ns_host, ns_port, proxy = self.ns
        uri = getattr(self.soc, "_pyroUri", None)   # where the proxy calls: as the name server said at the connect
        try:
            host, port = str(uri.host), int(uri.port)
        except (AttributeError, TypeError, ValueError):
            if self.registered is None:
                return ""
            host, port = self.registered
        if not takes_connection(ns_host, ns_port):
            return ("the board's name server at %s:%d doesn't answer either: the board is off or restarting, or its "
                    "name server and QICK server were stopped (run in Jupyter kernels, a reboot ends both). Once it is "
                    "back, start them again (run_on_board), or have them start at every boot (the card's notes: "
                    "qick/rfsoc); then connect %s again" % (ns_host, ns_port, self.name))
        lookup = getattr(self.qick, "lookup", None)
        if lookup is not None:   # waited LOOKUP_S at most, never Pyro's minute on top of a call that already failed
            state, got = _within(LOOKUP_S, lookup, ns_host, ns_port, proxy)
            if state == "late" or (state == "error" and type(got).__name__ != "NamingError"):
                return ("the board's name server at %s:%d takes a connection but didn't answer%s: the board may be "
                        "busy or restarting. Once it answers, connect %s again; a reboot ends the name server and "
                        "QICK server unless they start at every boot (the card's notes: qick/rfsoc)" % (
                            ns_host, ns_port, " within %g s" % LOOKUP_S if state == "late" else " (%s)" % got,
                            self.name))
            if state == "error":   # Pyro's NamingError: it answered, and has no server of that name
                return ("the board's name server at %s:%d answers, but has no QICK server %r now (it stopped, or was "
                        "started under another name): start it again (run_on_board), or at every boot (the card's "
                        "notes: qick/rfsoc); then connect %s again" % (ns_host, ns_port, proxy, self.name))
            now = tuple(got)
            if now != (host, port):
                why, _ = cant_reach(now[0], now[1], ns_host)
                if why:
                    return ("its QICK server was started again and registered %s:%d, which this computer can't reach: "
                            "%s. %s" % (now[0], now[1], why, self._fix()))
                return ("its QICK server was started again: the board's name server has it at %s:%d now, not %s:%d; "
                        "connect %s again" % (now[0], now[1], host, port, self.name))
        why, sure = cant_reach(host, port, ns_host)
        return "this computer %s its QICK server at %s:%d: %s. %s" % (
            "can't reach" if sure else "may not reach", host, port, why, self._fix()) if why else ""

    def _use(self, tproc: Optional[int]) -> None:
        """Programs for this tProc generation (None: not known yet; the first program says how to set it)."""
        self.tproc, self.programs = tproc, {}
        if tproc is None:
            return
        base = getattr(self.qick, "AveragerProgram" if tproc == 1 else "AveragerProgramV2", None)
        if base is None:
            raise AdapterError("%s: the board's firmware has a tProc v%d, and this computer's qick (%s) has no "
                               "programs for it: install the board's qick version (install_for_instrument)"
                               % (self.name, tproc, getattr(self.qick, "version", "") or "?"))
        self.programs = programs_v1(base) if tproc == 1 else programs(base)

    def _cfg(self, key: str) -> Any:
        try:
            return self.soccfg[key]
        except (KeyError, TypeError, IndexError):
            return None

    def _config(self, gain: Optional[float] = None) -> Dict[str, Any]:
        s = self.state
        gens, ros = self._cfg("gens") or [], self._cfg("readouts") or []
        if s["gen"] >= len(gens) or s["ro"] >= len(ros):
            raise AdapterError("%s: the board has generators 0-%d and readouts 0-%d; the card asks for gen %d, ro %d"
                               % (self.name, len(gens) - 1, len(ros) - 1, s["gen"], s["ro"]))
        has_mixer = bool(gens[s["gen"]].get("has_mixer"))
        if has_mixer and not s["mixer"]:
            raise AdapterError("%s: generator %d has a digital mixer: set mixer (MHz) first" % (self.name, s["gen"]))
        g = s["gain"] if gain is None else gain
        maxv = gens[s["gen"]].get("maxv") or 32766   # tProc v1 takes the gain in DAC units
        return dict(s, gain=g, gain_dac=int(round(g * maxv)), has_mixer=has_mixer,
                    tproc_readout="tproc_ctrl" in ros[s["ro"]])

    def _run(self, kind: str, cfg: Dict[str, Any]) -> Any:
        if not self.programs:
            raise AdapterError("%s: Kelvin doesn't know this board's firmware (its tProc is %r): set the instrument's "
                               "tproc to 1 or 2 in the lab config (vars: {tproc: \"1\"})" % (self.name, self.firmware))
        if self.tproc == 1:   # AveragerProgram: the config in one dict; I and Q as lists, per readout channel
            prog = self.programs[kind](self.soccfg, dict(cfg, reps=int(cfg["reps"])))
            di, dq = prog.acquire(self.soc, load_pulses=True, progress=False)
            return [[(di[0][0], dq[0][0])]]   # as tProc v2's: the first readout's first point, (I, Q)
        prog = self.programs[kind](self.soccfg, reps=int(cfg["reps"]), final_delay=1.0, cfg=cfg)
        return prog.acquire(self.soc, progress=False)

    def _start_tone(self) -> None:
        self._run("tone", self._config())
        self.playing = True

    def _stop(self) -> None:
        self.soc.reset_gens()
        self.playing = False

    def set(self, path: str, value: Any) -> None:
        s = self.state
        if path == "channels":
            ch = _channels(value)
            if self.playing:
                self._stop()
            s["gen"], s["ro"] = ch.get("gen", s["gen"]), ch.get("ro", s["ro"])
            return
        if path == "tproc":   # the card's tproc var: auto (what the board says), 1 or 2
            choice = str(value).strip().lower().lstrip("v")
            if choice not in ("auto", "1", "2"):
                raise AdapterError("%s: tproc is auto, 1 or 2" % self.name)
            if self.playing:
                self._stop()
            self._use(self.found if choice == "auto" else int(choice))
            return
        if path not in DEFAULTS or path == "phase0":
            raise AdapterError("%s: no setting %r (it has: %s)" % (self.name, path, ", ".join(DEFAULTS)))
        if path == "mode":
            if value not in ("tone", "pulsed"):
                raise AdapterError("%s: mode is tone or pulsed" % self.name)
        elif path in ("nqz", "reps", "output"):
            value = int(value)
            if path == "nqz" and value not in (1, 2):
                raise AdapterError("%s: the Nyquist zone is 1 or 2" % self.name)
        else:
            value = float(value)
            if path == "gain" and not 0 <= value <= 1:
                raise AdapterError("%s: gain is 0 to 1 of full scale" % self.name)
        s[path] = value
        self.data.clear()
        if path == "output":
            if value and s["mode"] == "tone":
                self._start_tone()
            elif not value:
                self._stop()
        elif s["output"] and s["mode"] == "tone":
            self._start_tone()   # play the new frequency or gain
        elif path == "mode" and self.playing:
            self._stop()   # pulsed: nothing plays between reads

    def get(self, path: str) -> Any:
        if path in READINGS:
            if not self.data or path in self.taken:
                self._acquire()
            self.taken.add(path)
            return self.data[path]
        if path not in DEFAULTS:
            raise AdapterError("%s: no reading %r" % (self.name, path))
        return self.state[path]

    def _acquire(self) -> None:
        s = self.state
        if s["output"] and s["mode"] == "tone":
            iq = self._run("tone", self._config())
            self.playing = True
        else:   # pulsed, or the output is off: a pulse at zero gain reads the background
            iq = self._run("pulsed", self._config(gain=s["gain"] if s["output"] else 0.0))
        try:
            point = iq[0][0]
            i, q = float(point[0]), float(point[1])
        except (TypeError, IndexError, ValueError) as e:
            raise AdapterError("%s: unexpected acquisition data (%s)" % (self.name, e)) from e
        self.data = {"i": i, "q": q, "magnitude": math.hypot(i, q), "phase": math.degrees(math.atan2(q, i))}
        self.taken = set()

    def identity(self) -> str:
        board = self._cfg("board") or "RFSoC"
        text = "QICK,%s,%s" % (board, self.address.split("://", 1)[1])
        if self.tproc:
            text += ",tProc v%d" % self.tproc
        theirs, ours = getattr(self, "versions", ("", ""))
        if theirs or ours:
            text += ",board's qick %s, Kelvin's qick %s%s" % (theirs or "?", ours or "?", "" if theirs == ours else
                                                               " (they differ: make them the same version)")
        return text

    def close(self) -> None:
        try:
            if self.playing:
                self._stop()
        except Exception:
            pass
