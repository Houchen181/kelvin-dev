"""An episode's lab, built inside the worker with the arm's own Kelvin (labharness from the arm's snapshot).

    env = build(task, seed, folder)   # the lab, its world (the truth), the envelope, Kelvin's runtime
    env.runtime                       # Kelvin's LabRuntime, with the simulated person answering its approvals

Every episode runs on the bench lab (lab/lab.yaml, twins from kbench.physics). Nothing in it reaches this computer's
hardware or the internet: VISA and serial ports are fenced off (fence_hardware), Kelvin's look-arounds of the computer
(scan, connected devices, installed software...) answer from the bench, and the literature and web switch is off.
"""
from __future__ import annotations

import copy
import random
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .physics import BenchWorld, InstantClock

LAB = Path(__file__).parent / "lab"
NO_HARDWARE = "not available in this lab: Kelvin's look at this computer's hardware is off here"

# Kelvin's tools that look at the computer itself (ports, buses, the network, installed programs): on the bench they
# answer that nothing new is connected, so no episode ever talks to a real instrument or leaks what is on this PC.
LOOKS = {
    "scan": "scanned GPIB, USB, LAN and serial ports: nothing new is connected; every instrument found is already in "
            "the lab (lab_status lists them)",
    "inspect_address": NO_HARDWARE, "find_on_network": "no instruments announced themselves on the local network",
    "inspect_serial": NO_HARDWARE, "connected_devices": "nothing is plugged in besides the lab's own instruments",
    "installed_software": "no makers' software matches", "find_maker_files": "no makers' files match",
    "install_for_instrument": "not run: installing software is not available in this lab",
    "run_on_board": NO_HARDWARE, "read_examples": "no makers' examples are on this computer",
    "library_api": "not available in this lab",
}


def deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class _NoInstrument(Exception):
    pass


class _EmptyResourceManager:
    """VISA with nothing on it: the bench never reaches this computer's instruments."""

    def list_resources(self, query: str = "?*::INSTR"):
        return ()

    def list_resources_info(self, query: str = "?*::INSTR"):
        return {}

    def open_resource(self, name: str, *a: Any, **k: Any):
        raise OSError("VI_ERROR_RSRC_NFOUND: %s is not connected" % name)

    def close(self) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)


def fence_hardware() -> List[str]:
    """Keep this process off the computer's real instruments: an empty VISA, no serial ports. What was fenced."""
    done = []
    try:
        import pyvisa
        pyvisa.ResourceManager = lambda *a, **k: _EmptyResourceManager()   # type: ignore[assignment]
        done.append("pyvisa")
    except ImportError:
        pass
    try:
        import serial
        import serial.tools.list_ports as list_ports

        class _NoPort:
            def __init__(self, *a: Any, **k: Any):
                raise serial.SerialException("could not open port: not connected")
        serial.Serial = _NoPort   # type: ignore[assignment,misc]
        serial.serial_for_url = lambda *a, **k: _NoPort()   # type: ignore[assignment]
        list_ports.comports = lambda *a, **k: []   # type: ignore[assignment]
        try:
            import serial.tools.list_ports_windows as ports_windows
            ports_windows.comports = lambda *a, **k: []   # type: ignore[assignment]
        except ImportError:
            pass
        done.append("serial")
    except ImportError:
        pass
    try:
        from labharness import adapters
        adapters.set_resource_manager(_EmptyResourceManager())
        done.append("adapters")
    except Exception:
        pass
    return done


@dataclass
class Env:
    task: Any
    seed: int
    folder: Path
    lab: Any
    world: BenchWorld
    clock: InstantClock
    envelope: Any
    lab_file: Path
    runtime: Any = None
    approver: Any = None
    work: Optional[Path] = None          # the chat's folder (files the person gives Kelvin)
    notes: List[str] = field(default_factory=list)
    device_at: Any = None                # address -> the task's own device there (or None)

    def twin(self, name: str) -> Any:
        return self.world.twins[name]

    def wait_queue(self, timeout: float = 600.0) -> str:
        """Wait (wall time) until Kelvin's queue is no longer running: what it ended as."""
        import time
        q = self.runtime.queue
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if q.state not in ("running", "pausing"):
                return q.state
            time.sleep(0.2)
        return q.state

    def advance(self, seconds: float) -> None:
        """Let time pass on the bench (the night, a coffee break): the world evolves, as the clock would."""
        self.clock.sleep(seconds)
        with self.world.lock:
            self.world.update()


def sample_params(task: Any, seed: int) -> Dict[str, Any]:
    """The sample's numbers for this task and seed: the task's params, drawn from its own random stream."""
    params = task.params
    if callable(params):
        return dict(params(random.Random("%s/%d" % (task.id, seed))))
    return dict(params or {})


def build(task: Any, seed: int, folder: Path) -> Env:
    from labharness.cards import load_card
    from labharness.homelab import builtin_registry
    from labharness.lab import Lab
    from labharness.labconfig import load_envelope, load_lab_config
    from labharness.sim.twin import sim_backend_factory

    folder.mkdir(parents=True, exist_ok=True)
    registry = builtin_registry()
    for path in sorted((LAB / "cards").glob("*.yaml")):
        registry.put(load_card(path))
    raw = yaml.safe_load((LAB / "lab.yaml").read_text(encoding="utf-8"))
    raw = deep_merge(raw, getattr(task, "lab_patch", None) or {})
    home = folder / "home"
    (home / "cards").mkdir(parents=True, exist_ok=True)
    for path in sorted((LAB / "cards").glob("*.yaml")):   # the lab's own cards, where Kelvin keeps a lab's cards
        (home / "cards" / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    lab_file = home / "lab.yaml"
    lab_file.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    cfg = load_lab_config(lab_file, registry)
    clock = InstantClock()
    world = BenchWorld(clock, seed, sample_params(task, seed), getattr(task, "limits", None))
    twins = sim_backend_factory(world)
    devices = getattr(task, "devices", None) or {}
    opened: Dict[str, Any] = {}

    def device_at(address: str, name: str = "") -> Any:
        """A task's own device at this address, however it is spelled: one physical device, whatever Kelvin calls
        it (its name in the bench's world is the class's WORLD_NAME), kept across reconnects; None elsewhere."""
        import re
        for pattern, cls in devices.items():
            if re.search(pattern, str(address).strip()):
                if pattern not in opened:
                    opened[pattern] = cls(world, getattr(cls, "WORLD_NAME", None) or name)
                return opened[pattern]
        return None

    def factory(name: str, card: Any, address: str) -> Any:
        """The lab's twins; at a task's own address, its device."""
        device = device_at(address, name)
        return device if device is not None else twins(name, card, address)

    if devices:   # trying a card opens the address itself (cardtry.open_backend), not through the lab: same device
        from labharness import cardtry
        plain_open = cardtry.open_backend

        def open_backend(name: str, card: Any, address: str, *a: Any, **k: Any) -> Any:
            device = device_at(address, name)
            return device if device is not None else plain_open(name, card, address, *a, **k)
        cardtry.open_backend = open_backend

    lab = Lab(cfg, registry, factory, clock, world)
    initial = getattr(task, "initial", None) or {}
    if callable(initial):   # it may depend on the sample (a bias field, say)
        initial = initial(world.p)
    for name, values in initial.items():   # the lab as the person left it
        twin = world.twins[name]
        twin.state.update(values)
    world._last_B = None
    envelope = load_envelope(LAB / "project.yaml", lab.config, lab.registry)
    return Env(task=task, seed=seed, folder=folder, lab=lab, world=world, clock=clock, envelope=envelope,
               lab_file=lab_file, device_at=device_at)


# What the lab keeps in its shared memory (Kelvin's lab memory, which every chat and agent reads): how it is wired.
LAB_NOTES = [
    ("wiring", "The lock-in's sine output drives the sample through a 10 MOhm series resistor, so the excitation "
               "current is amplitude / 10 MOhm (rms) and the sample's resistance is R = X * 10 MOhm / amplitude.",
     ["lockin"]),
    ("wiring", "The source-meter smu is wired to the device's back gate. The gate dielectric breaks down somewhere "
               "above 10 V; the leakage then rises steeply and can destroy the device.", ["smu"]),
    ("wiring", "NV set-up: the pulse generator pg gates the laser (its AOM), the microwave switch and the photon "
               "counter's signal and reference windows; the microwave source mw feeds the antenna over the diamond.",
     ["pg", "mw", "counter"]),
]


def fit_runtime(env: Env, runtime: Any) -> None:
    """Kelvin's runtime on the bench: its look-arounds of the computer answer from the bench, and the lab's memory
    holds how it is wired."""
    env.runtime = runtime
    runtime.lab_info = {"kind": "file", "path": str(env.lab_file)}   # as open_home_lab says: a lab.yaml
    runtime.cards_dir = env.lab_file.parent / "cards"   # as the app sets it (create_app), for the MCP arm too
    runtime.home = env.lab_file.parent
    runtime.lab_memory_dir = env.lab_file.parent         # as create_app and kelvin-mcp set it: the lab's home
    for kind, text, instruments in LAB_NOTES:
        try:
            runtime.keep_lab_note(kind, text, instruments, "", "person", None)
        except Exception as e:   # an arm without lab memory: said in meta.json
            env.notes.append("lab note not kept: %s" % e)
    for name, text in dict(LOOKS, **(getattr(env.task, "looks", None) or {})).items():
        if hasattr(runtime, name):   # a text, or a function of (env, the tool's arguments) for a task's own device
            setattr(runtime, name, (lambda t: (lambda *a, **k: t(env, *a, **k) if callable(t) else t))(text))
            env.notes.append("stubbed %s" % name)
    try:
        runtime.literature.enabled = False
    except AttributeError:
        pass


class ApprovalDesk:
    """The person at the portal: answers approval cards at once, as the task says, and keeps every card."""

    def __init__(self, policy: Any, approver: Any):
        import re
        self.policy = policy
        self.approver = approver
        self.cards: List[Dict[str, Any]] = []
        self._re = re
        self._lock = threading.Lock()

    def decide(self, text: str) -> bool:
        for pattern, answer in getattr(self.policy, "approve", None) or []:
            if self._re.search(pattern, text, self._re.I | self._re.S):
                return bool(answer)
        return bool(getattr(self.policy, "default", True))

    def __call__(self, card: Dict[str, Any]) -> None:
        import time
        text = "\n".join([str(card.get("title") or "")] + [str(i) for i in card.get("items") or []])
        entry = {"id": card.get("id"), "title": card.get("title"), "items": list(card.get("items") or []),
                 "at": time.time()}
        if card.get("auto"):   # auto mode approved it without asking: only shown
            entry["answer"], entry["by"] = True, "auto mode"
        else:
            yes = self.decide(text)
            entry["answer"], entry["by"] = yes, "person"
            self.approver.answer(card["id"], yes, by="human",
                                 who={"account": "bench person", "channel": "portal"})
        with self._lock:
            self.cards.append(entry)
