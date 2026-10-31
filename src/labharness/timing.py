"""How long readings and changes take in this lab: the one source every estimate uses (a run's or a queue's card,
validate_plan, a dry run, a run's time left), so they agree and count what the lab really takes. Field tests: "81
points, ~8 s" took 3.6 min (a counter's 1 s integration at each point, not counted); a queue's card said ~29 s where
its own dry run said ~6.8 min; a power ramp took 9 times its |change|/rate (each step's guard read).

Each real run measures how long each of its readings took (the mean over its points), with the settings its
instrument had then; how long each step of a drive it stepped took beyond the step's dwell, away from safe (with its
guard readings) and toward it (a set that isn't ramped is one step); and what recording each point took beyond its
readings. The latest run that measured one is kept, for each lab, in timing.json. A reading measured with other
settings than it has now (a counter's integration changed) isn't taken from there: the instrument's twin says how long
it takes, when its card names one that models it (a counter's integration, a VNA's sweep), and otherwise it isn't
counted. A dry run's twins are paced at the same times (RunEngine._execute), so the dry run counts what the card
counts."""
from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _same(a: Any, b: Any) -> bool:
    if _number(a) and _number(b):
        return abs(a - b) <= 1e-9 * max(abs(a), abs(b), 1e-30)
    return a == b


def steps(inst: Any, spec: Any, a: Any, b: Any) -> Tuple[int, float]:
    """A change from a to b as the gate makes it: (its steps, the seconds it dwells on them). A ramp at the lab's rate
    takes its steps (SafetyGate._ramp: ramp_step, else a fifth of the rate per step) and |b - a|/rate; a number that
    isn't ramped is one set. (0, 0) for what isn't stepped: a set point the instrument ramps to by itself, a switch, a
    value unknown or unchanged."""
    if not spec.numeric or spec.ramp == "instrument" or not _number(a) or not _number(b) or _same(a, b):
        return 0, 0.0
    if spec.ramp == "gate":
        rate = inst.rate(spec.name)
        if not rate or rate <= 0:
            return 1, 0.0
        step = spec.ramp_step or rate * 0.2
        return max(1, int(math.ceil(abs(b - a) / step - 1e-9))), abs(b - a) / rate
    return 1, 0.0


def away(spec: Any, a: Any, b: Any) -> bool:
    """A drive's change away from its safe value (its steps read its guards): they take longer than toward it."""
    try:
        return bool(spec.is_drive and spec.distance(b) > spec.distance(a))
    except Exception:
        return False


def settings_of(inst: Any, values: Dict[str, Any]) -> Dict[str, Any]:
    """The instrument's settings among these values (param: value): what a reading's time may depend on (a counter's
    integration, a VNA's points and bandwidth). Not its drives, nor its readings."""
    out = {}
    for name, value in values.items():
        spec = inst.card.params.get(name)
        if spec is not None and not spec.is_drive and spec.set_cmd is not None and spec.type in (
                "float", "int", "bool", "enum") and (_number(value) or isinstance(value, (bool, str))):
            out[name] = value
    return out


def modeled_read_s(inst: Any, param: str, state: Dict[str, Any]) -> Optional[float]:
    """Seconds one reading of param takes on the instrument's twin with these settings, when its card names a twin
    that models it (a counter's integration, a VNA's sweep); None otherwise. The twin is made for this alone, on a
    simulated clock: nothing is sent to the instrument."""
    if not getattr(inst.card, "twin", None):
        return None
    from .clock import SimClock
    from .sim.twin import World, make_twin_backend
    try:
        world = World(SimClock())
        twin = make_twin_backend(inst.card, world, inst.name)
        twin.load_state(state)
        t0 = world.clock.now()
        twin.read(param)
        took = world.clock.now() - t0
    except Exception:
        return None
    return took if took > 0 else None


def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _round(x: float) -> float:
    return float("%.4g" % x)


class Took:
    """One run's times as it measures them (RunEngine._execute): each reading, each step of a drive (away from safe
    or toward it), each point's recording. Means, not medians: what they add up to is the run's time."""

    def __init__(self) -> None:
        self.reads: Dict[str, List[float]] = {}
        self.steps: Dict[Tuple[str, str], List[float]] = {}
        self.points: List[float] = []

    def read(self, key: str, seconds: float) -> None:
        self.reads.setdefault(key, []).append(max(0.0, seconds))

    def step(self, key: str, seconds: float, away_from_safe: bool = False) -> None:
        self.steps.setdefault((key, "away" if away_from_safe else "toward"), []).append(max(0.0, seconds))

    def point(self, seconds: float) -> None:
        self.points.append(max(0.0, seconds))

    def summary(self) -> Dict[str, Any]:
        """For the run's record: {reads: {key: s}, steps: {key: {away: s, toward: s}}, point: s}."""
        out_steps: Dict[str, Dict[str, float]] = {}
        for (key, way), values in self.steps.items():
            out_steps.setdefault(key, {})[way] = _round(_mean(values))
        return {"reads": {k: _round(_mean(v)) for k, v in self.reads.items()}, "steps": out_steps,
                "point": _round(_mean(self.points)) if self.points else None}


class Timing:
    """This lab's times, as its runs measured them, kept in a file (path(): None keeps them in memory) under the lab's
    name (lab()), so another lab on this computer keeps its own."""

    def __init__(self, path: Callable[[], Optional[Path]], lab: Callable[[], str]):
        self._path, self._lab = path, lab
        self._lock = threading.Lock()
        self._memory: Dict[str, Any] = {}
        self._read_at: Tuple[Optional[str], float] = (None, -1.0)

    def _load(self) -> Dict[str, Any]:
        """Every lab's times, read again when the file changed (another Kelvin, an MCP server, wrote it)."""
        path = self._path()
        if path is None:
            return self._memory
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            return self._memory
        if self._read_at != (str(path), stamp):
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                self._memory = data.get("labs") if isinstance(data, dict) and isinstance(data.get("labs"), dict) \
                    else {}
            except (OSError, ValueError):
                self._memory = {}
            self._read_at = (str(path), stamp)
        return self._memory

    def mine(self) -> Dict[str, Any]:
        """This lab's: {reads: {key: {s, settings, run, at}}, steps: {key: {away, toward, run, at}}, point: {s, run,
        at}}."""
        with self._lock:
            lab = self._load().get(self._lab()) or {}
        return lab if isinstance(lab, dict) else {}

    def read_s(self, key: str, setting: Callable[[str], Any]) -> Optional[float]:
        """Seconds one reading of key took in this lab's latest run that took it, if its instrument's settings are as
        they were then (setting(param): the value now, None when unknown); None when no run took it, or it was taken
        at other settings."""
        rec = (self.mine().get("reads") or {}).get(key)
        if not isinstance(rec, dict) or not _number(rec.get("s")):
            return None
        for param, then in (rec.get("settings") or {}).items():
            now = setting(param)
            if now is not None and not _same(now, then):
                return None
        return float(rec["s"])

    def step_s(self, key: str, away_from_safe: bool = False) -> float:
        """Seconds a step of this drive or setting took beyond its dwell (a set that isn't ramped: the whole set): the
        exchanges with the instrument, the checks, and away from safe its guard readings. The other way's when only
        that was measured; 0 when no run stepped it."""
        rec = (self.mine().get("steps") or {}).get(key)
        if not isinstance(rec, dict):
            return 0.0
        ways = ("away", "toward") if away_from_safe else ("toward", "away")
        return next((float(rec[w]) for w in ways if _number(rec.get(w))), 0.0)

    def point_s(self) -> float:
        """Seconds recording a point took beyond its readings. 0 when no run measured it."""
        rec = self.mine().get("point")
        return float(rec["s"]) if isinstance(rec, dict) and _number(rec.get("s")) else 0.0

    def learn(self, run_id: str, took: Took, settings: Dict[str, Dict[str, Any]]) -> None:
        """A real run's times, kept for its lab, in place of what earlier runs measured of the same. settings: each
        instrument's settings while it ran (param: value), kept with its readings' times."""
        summary = took.summary()
        at = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            labs = self._load()
            lab = labs.setdefault(self._lab(), {})
            reads = lab.setdefault("reads", {})
            for key, s in summary["reads"].items():
                reads[key] = {"s": s, "settings": settings.get(key.split(".", 1)[0]) or {}, "run": run_id, "at": at}
            lab_steps = lab.setdefault("steps", {})
            for key, ways in summary["steps"].items():
                kept = lab_steps.get(key) if isinstance(lab_steps.get(key), dict) else {}
                lab_steps[key] = dict({w: kept[w] for w in ("away", "toward") if w in kept}, **ways, run=run_id, at=at)
            if summary["point"] is not None:
                lab["point"] = {"s": summary["point"], "run": run_id, "at": at}
            path = self._path()
            if path is None:
                return
            try:   # written whole, then put in place: a crash never leaves half a file
                path = Path(path)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(json.dumps({"labs": labs}, indent=1), encoding="utf-8")
                os.replace(tmp, path)
                self._read_at = (str(path), os.path.getmtime(path))
            except OSError:
                pass
