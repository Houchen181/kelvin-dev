"""RFSoC boards running QICK (the Quantum Instrumentation Control Kit), reached through its Pyro4 server.

Address: qick://<name server host>[:8888]/<proxy name>, as started on the board:
    pyro4-ns -n 0.0.0.0 -p 8888          then   python -m qick.pyro_cli myqick -n <host> -p 8888
Needs qick and Pyro4 on the computer that runs Kelvin (Kelvin installs them into its drivers environment once a
person approves: envs.py), and tProc v2 firmware on the board.

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
        from qick.asm_v2 import AveragerProgramV2   # type: ignore
    except ImportError as e:
        raise AdapterError("QICK boards need qick, at the board's version where it is known, and Pyro4: %s"
                           % envs.missing("qick", "Pyro4")) from e
    return SimpleNamespace(make_proxy=make_proxy, AveragerProgramV2=AveragerProgramV2,
                           version=str(getattr(qick, "__version__", "") or ""))


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
        tprocs = self._cfg("tprocs") or [{}]
        if tprocs[0].get("type") not in (None, "qick_processor"):
            raise AdapterError("%s: Kelvin runs tProc v2 programs; this board's firmware has %s"
                               % (name, tprocs[0].get("type")))
        self.programs = programs(self.qick.AveragerProgramV2)
        self.state: Dict[str, Any] = dict(DEFAULTS, gen=0, ro=0)
        self.data: Dict[str, float] = {}
        self.taken: set = set()
        self.playing = False

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
        return dict(s, gain=s["gain"] if gain is None else gain, has_mixer=has_mixer,
                    tproc_readout="tproc_ctrl" in ros[s["ro"]])

    def _run(self, kind: str, cfg: Dict[str, Any]) -> Any:
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
