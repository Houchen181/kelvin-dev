"""labsim benches for tests: simulated instruments Kelvin talks to as if they were plugged in (tools/labsim).

    from labsim_bench import labsim_bench  # noqa: F401  (the fixture)

    def test_a_cool_down(labsim_bench, tmp_path):
        bench = labsim_bench("ppms", sdk=True)       # tools/labsim/benches/ppms.yaml, or a bench file's path
        lab = build_lab(lab_yaml, partial=True)      # this process talks to the bench now: VISA, COM ports, SDKs
        bench.ctl("fault", "ppms", "dead")           # as `python -m labsim fault ppms dead`: power, unplug, set ...
        subprocess.run(cmd, env=dict(os.environ, **bench.env))   # another program, pointed at the same bench

The bench's server runs in its own process, with its folder in the test's temporary folder, and stops when the test
ends; Kelvin's VISA resource manager, pyserial and the import path are put back as they were. It needs PyVISA
(Kelvin's [hardware] extras); without it the test is skipped.

labsim_offline(bench) gives a bench's world and instruments in this process instead, with no server: time passes
only when advance() says so, which suits an instrument's own physics.

labsim_lab(bench, lab_yaml) puts a Kelvin lab on such an offline bench: Kelvin's VISA and the makers' libraries reach
it in this process, and the lab's clock is the bench's own (BenchClock), so bench time passes only while Kelvin waits.
A 30-minute cool-down then takes seconds, with Kelvin's adapters, cards and gate as on the real thing."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

LABSIM = Path(__file__).resolve().parents[1] / "tools" / "labsim"
SHIMS = LABSIM / "shims"


def bench_file(bench: str) -> Path:
    """A bench by name (tools/labsim/benches/NAME.yaml) or by its file's path."""
    path = Path(bench)
    return path if path.suffix in (".yaml", ".yml") else LABSIM / "benches" / (bench + ".yaml")


class Bench:
    """A bench running in its own server process."""

    def __init__(self, path: Path, home: Path, proc: "subprocess.Popen[bytes]", log: Any) -> None:
        self.path, self.home, self.proc, self._log = path, home, proc, log

    @property
    def env(self) -> Dict[str, str]:
        """What points another program at this bench, as `python -m labsim env` prints it."""
        path = [str(SHIMS), str(LABSIM)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])
        return {"LABSIM_HOME": str(self.home), "KELVIN_VISA_BACKEND": "@labsim", "PYTHONPATH": os.pathsep.join(path)}

    def ctl(self, cmd: str, *args: Any) -> Dict[str, Any]:
        """A control command as `python -m labsim CMD ARGS` sends it: status, log, power, unplug, plug, fault, hold,
        set."""
        from labsim.link import Link
        link = Link(str(self.home))
        try:
            return link.must("ctl", cmd=cmd, args=[str(a) for a in args])
        finally:
            link.close()

    def stop(self) -> None:
        """Stop the server: the one in server.json (a venv's python.exe runs it as a child of the one started)."""
        try:
            pid = json.loads((self.home / "server.json").read_text(encoding="utf-8"))["pid"]
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, ValueError, KeyError):
            pass
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            pass
        self._log.close()


def start(bench: str, home: Path, timeout: float = 60.0) -> Bench:
    """Start a bench's server (`python -m labsim serve`) in a process of its own and wait until it answers."""
    from labsim.link import LabsimError, Link
    path = bench_file(bench)
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(LABSIM)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    log = open(home / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-u", "-m", "labsim", "serve", str(path), "--home", str(home)],
                            cwd=str(LABSIM), env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    running = Bench(path, home, proc, log)
    deadline = time.monotonic() + timeout
    while True:
        if proc.poll() is not None or time.monotonic() > deadline:
            running.stop()
            raise RuntimeError("the labsim server for %s didn't come up:\n%s"
                               % (path.name, (home / "server.log").read_text(encoding="utf-8", errors="replace")))
        if (home / "server.json").exists():
            try:
                link = Link(str(home))
                link.must("hello")
                link.close()
                return running
            except (LabsimError, OSError, ValueError):   # still starting
                pass
        time.sleep(0.1)


def _hook_serial(monkeypatch: Any) -> None:
    """The bench's COM ports in pyserial, as shims/sitecustomize.py puts them at start-up; put back after the test."""
    try:
        import serial
        import serial.tools.list_ports as list_ports
    except ImportError:
        return
    monkeypatch.setattr(serial, "Serial", serial.Serial)   # noted as they are, so the test's end restores them
    monkeypatch.setattr(serial, "serial_for_url", serial.serial_for_url)
    monkeypatch.setattr(list_ports, "comports", list_ports.comports)
    if os.name == "nt":
        import serial.tools.list_ports_windows as ports_windows
        monkeypatch.setattr(ports_windows, "comports", ports_windows.comports)
    from labsim import hooks
    hooks.install_serial()


@pytest.fixture
def labsim_bench(tmp_path: Path, monkeypatch: Any):
    """labsim_bench(bench, sdk=False) starts the bench for this test and points this process at it: Kelvin's VISA
    (KELVIN_VISA_BACKEND=@labsim), the bench's COM ports in pyserial, and with sdk=True the makers' Python libraries
    (MultiPyVu, TimeTagger, pypylon ...) as labsim's stand-ins. Returns the Bench. One bench per test."""
    pytest.importorskip("pyvisa")
    pytest.importorskip("yaml")
    pytest.importorskip("numpy")
    from labharness import adapters
    monkeypatch.syspath_prepend(str(LABSIM))   # labsim and its VISA library, pyvisa_labsim
    before = getattr(adapters, "_rm", None)
    adapters.set_resource_manager(None)        # Kelvin makes its resource manager afresh, on the bench
    started: List[Bench] = []

    def start_bench(bench: str, sdk: bool = False) -> Bench:
        if started:
            raise RuntimeError("one bench per test: LABSIM_HOME names one bench for the whole process")
        running = start(bench, tmp_path / ("labsim-" + bench_file(bench).stem))
        started.append(running)
        monkeypatch.setenv("LABSIM_HOME", str(running.home))
        monkeypatch.setenv("KELVIN_VISA_BACKEND", "@labsim")
        _hook_serial(monkeypatch)
        if sdk:
            monkeypatch.syspath_prepend(str(SHIMS))
        return running

    yield start_bench
    rm = getattr(adapters, "_rm", None)
    if rm is not None and rm is not before:
        try:
            rm.close()   # its sessions were on the bench; the next resource manager starts clean
        except Exception:
            pass
    adapters.set_resource_manager(before)
    for running in started:
        running.stop()
    for name, module in list(sys.modules.items()):   # the stand-ins go, so later tests see this computer's own
        if str(getattr(module, "__file__", None) or "").startswith(str(SHIMS)):
            del sys.modules[name]


@pytest.fixture
def labsim_offline(tmp_path: Path, monkeypatch: Any):
    """labsim_offline(bench): the bench's world and instruments in this process, without a server or VISA. Nothing
    moves until advance(bench, seconds); an instrument answers its commands through handle() or sdk_call()."""
    pytest.importorskip("yaml")
    pytest.importorskip("numpy")
    monkeypatch.syspath_prepend(str(LABSIM))

    def build(bench: str) -> Any:
        from labsim.server import Server
        return Server(str(bench_file(bench)), str(tmp_path / ("offline-" + bench_file(bench).stem)))
    return build


def advance(bench: Any, seconds: float, dt: float = 0.05) -> None:
    """Let this much bench time pass at once: the world and every instrument that is on tick as the server's
    ticker does, without waiting for it."""
    now = time.monotonic()
    for _ in range(int(round(seconds / dt))):
        bench.world.step(dt)
        now += dt
        for inst in bench.instruments.values():
            if inst.powered:
                inst.tick(dt, now)


def ctl(bench: Any, cmd: str, *args: Any) -> Dict[str, Any]:
    """A control command (fault, power, set ...) on a running bench (Bench.ctl) or an offline one."""
    if isinstance(bench, Bench):
        return bench.ctl(cmd, *args)
    r = bench.dispatch({"op": "ctl", "cmd": cmd, "args": [str(a) for a in args]}, set())
    if "err" in r:
        raise RuntimeError(r.get("msg") or r["err"])
    return r


class BenchClock:
    """A Kelvin lab's clock that is an offline bench's: now() is bench time, and sleep() lets the bench's world and
    instruments move on by that much (in steps of dt, the last one shorter), as the server's ticker would. at(s, fn)
    does fn once s more seconds of bench time have passed: a fault in the middle of something Kelvin waits on."""
    simulated = False   # Kelvin treats the instruments as real ones; only the time is the bench's

    def __init__(self, bench: Any, dt: float = 0.25) -> None:
        self.bench, self.dt, self.t = bench, dt, 0.0
        self._due: List[Any] = []

    def now(self) -> float:
        return self.t

    def at(self, seconds: float, fn: Any) -> None:
        self._due.append((self.t + seconds, fn))

    def sleep(self, seconds: float) -> None:
        while seconds > 1e-9:
            d = min(self.dt, seconds)
            with self.bench.cond:
                self.bench.world.step(d)
                now = time.monotonic()
                for inst in self.bench.instruments.values():
                    if inst.powered:
                        inst.tick(d, now)
            seconds -= d
            self.t += d
            for due in [x for x in self._due if x[0] <= self.t + 1e-9]:
                self._due.remove(due)
                due[1]()


class _InProcess:
    """labsim's link to its server, for a server in this process (an offline bench): each request is handled at once."""
    bench: Any = None

    def __init__(self, folder: Any = None) -> None:
        self.mine: set = set()

    def call(self, op: str, **kw: Any) -> Dict[str, Any]:
        kw["op"] = op
        return type(self).bench.dispatch(kw, self.mine)

    def must(self, op: str, **kw: Any) -> Dict[str, Any]:
        from labsim.link import LabsimError
        r = self.call(op, **kw)
        if "err" in r:
            raise LabsimError(r["err"], r.get("msg") or r["err"])
        return r

    def close(self) -> None:
        pass


@pytest.fixture
def labsim_lab(labsim_offline, tmp_path: Path, monkeypatch: Any):
    """labsim_lab(bench, lab_yaml) -> (lab, offline bench): a Kelvin lab built from lab_yaml (its text) on the bench,
    in this process. Kelvin's VISA (@labsim) and the makers' libraries (labsim's stand-ins) reach the bench through an
    in-process link, and the lab's clock is the bench's (BenchClock): bench time passes only while Kelvin waits. The
    bench's instruments are named as in its bench file; ctl(bench, ...) makes faults. Put back after the test."""
    pytest.importorskip("pyvisa")
    from labharness import adapters
    from labharness.virtual_lab import build_lab
    monkeypatch.syspath_prepend(str(SHIMS))
    before = getattr(adapters, "_rm", None)
    built: List[Any] = []

    def make(bench_name: str, lab_yaml: str) -> Any:
        bench = labsim_offline(bench_name)
        import labsim.link
        import labsim.sdkclient
        import pyvisa_labsim.highlevel
        link = type("Link", (_InProcess,), {"bench": bench})
        monkeypatch.setattr(pyvisa_labsim.highlevel, "Link", link)
        monkeypatch.setattr(labsim.link, "Link", link)
        monkeypatch.setattr(labsim.sdkclient, "shared", lambda: link())
        monkeypatch.setenv("KELVIN_VISA_BACKEND", "@labsim")
        monkeypatch.setenv("LABSIM_HOME", str(tmp_path / "labsim-offline"))
        adapters.set_resource_manager(None)   # one made afresh, on this bench
        path = tmp_path / "lab.yaml"
        path.write_text(lab_yaml, encoding="utf-8")
        lab = build_lab(path, partial=True)
        lab.clock = BenchClock(bench)
        built.append(lab)
        return lab, bench

    yield make
    for lab in built:
        lab.close()
    rm = getattr(adapters, "_rm", None)
    if rm is not None and rm is not before:
        try:
            rm.close()
        except Exception:
            pass
    adapters.set_resource_manager(before)
    for name, module in list(sys.modules.items()):   # the stand-ins go, so later tests see this computer's own
        if str(getattr(module, "__file__", None) or "").startswith(str(SHIMS)):
            del sys.modules[name]
