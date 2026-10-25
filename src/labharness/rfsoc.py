"""RFSoC boards running QICK (the Quantum Instrumentation Control Kit), reached through its Pyro4 server.

Address: qick://<name server host>[:8888]/<proxy name>, as started on the board:
    pyro4-ns -n 0.0.0.0 -p 8888          then   python -m qick.pyro_cli myqick -n <host> -p 8888
Needs qick and Pyro4 on the computer that runs Kelvin (Kelvin installs them into its drivers environment once a
person approves: envs.py). The board's firmware may have a tProc v1 or v2: Kelvin reads which from the board and runs
programs written for it (the instrument's tproc var, 1 or 2, overrides that for a firmware it doesn't recognise).

Kelvin plays one generator channel and reads one readout channel (set in the card's vars):
    output     drive: off sends soc.reset_gens(), which drives every generator with zeros
    gain       drive: amplitude, 0 to 1 of the generator's full scale (the lab sets its own cap)
    frequency  MHz;  nqz (Nyquist zone 1 or 2);  mixer (MHz, for generators with a digital mixer)
    mode       tone: a continuous tone while the output is on;  pulsed: one pulse per readout, during reads only
    length, readout_length, trigger_delay (us);  reps (averages)
    i, q, magnitude, phase   one averaged acquisition (QICK's acquire) per read, shared by the four values
A continuous tone keeps playing after a program ends, so Kelvin stops it whenever the output goes off, the run
ends or aborts (through the safe sequence), and when it disconnects."""
from __future__ import annotations

import math
import urllib.parse
from types import SimpleNamespace
from typing import Any, Dict, Optional

from . import envs
from .adapters import AdapterError
from .cards import Card
from .drivers import DriverBackend

DEFAULTS = {"output": 0, "gain": 0.0, "frequency": 100.0, "nqz": 1, "mixer": 0.0, "mode": "tone", "length": 1.0,
            "readout_length": 1.0, "trigger_delay": 0.2, "reps": 100, "phase0": 0.0}
READINGS = ("i", "q", "magnitude", "phase")


def load_qick() -> Any:
    try:
        import qick   # type: ignore
        from qick.pyro import make_proxy   # type: ignore
    except ImportError as e:
        raise AdapterError("QICK boards need qick, at the board's version where it is known, and Pyro4: %s"
                           % envs.missing("qick", "Pyro4")) from e
    try:
        from qick.asm_v2 import AveragerProgramV2   # type: ignore
    except ImportError:   # a qick from before tProc v2, as a board with v1 firmware may run
        AveragerProgramV2 = None
    try:
        from qick import AveragerProgram   # type: ignore   # tProc v1
    except ImportError:
        AveragerProgram = None
    return SimpleNamespace(make_proxy=make_proxy, AveragerProgramV2=AveragerProgramV2, AveragerProgram=AveragerProgram,
                           version=str(getattr(qick, "__version__", "") or ""))


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
    def __init__(self, name: str, card: Card, address: str, module: Any = None):
        super().__init__(name, card, address)
        url = urllib.parse.urlparse(address)
        proxy = url.path.strip("/")
        if not url.hostname or not proxy:
            raise AdapterError("%s: a QICK board's address is qick://<name server host>[:8888]/<proxy name>" % name)
        self.qick = module or load_qick()
        try:
            self.soc, self.soccfg = self.qick.make_proxy(ns_host=url.hostname, ns_port=url.port or 8888,
                                                         proxy_name=proxy)
        except Exception as e:   # Pyro's NamingError, CommunicationError...
            raise AdapterError("%s: could not reach the QICK server %r through the name server at %s:%d (%s). On the "
                               "board, start the Pyro4 name server, then the QICK server under that name: on a PYNQ "
                               "board, in its Jupyter (http://%s:9090), run qick/pyro4/00_nameserver.ipynb, then "
                               "01_server.ipynb (Kelvin runs them with run_on_board once a person approves)."
                               % (name, proxy, url.hostname, url.port or 8888, e, url.hostname)) from e
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
