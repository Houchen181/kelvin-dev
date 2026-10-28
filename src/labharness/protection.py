"""Protecting the sample (VISION item 3): a device's own limits, its gates' leakage guards and the source's own caps.

A device (a sample in a project) carries protection that a person sets in the Lab panel; the agent can read it but
never change it:

    limits: {"laser.power": [0, 0.05]}                    drives this device must never pass
    gates:  [{"drive": "smu.V", "dielectric": "hBN", "thickness_nm": 20, "limit": 6,
              "leak": "smu.I", "leak_max": 1e-9, "rate": 0.1}]

While the device is mounted (apply):
- its limits narrow the lab's (Instrument.limits: the tighter side holds, and no approval, envelope or auto mode
  passes them), and a gate ramps no faster than its rate;
- each gate with a leakage reading gets a guard: a ramp stops and goes back to safe once the leakage passes leak_max;
- the instrument itself is capped where its card allows: the source range just above the limit (the smallest that
  covers it, so a smaller range left from before can't clip a sweep the limits allow), autorange off, and
  the source's own current limit at CAP_FACTOR times leak_max. These caps are device limits too, so nothing raises
  them, and they are written into the instrument (write_caps), so even a crash of Kelvin or of the computer can't
  take the sample past them.

A gate's limit comes from its dielectric unless the person gives one: FIELD (V/nm) times its thickness. hBN and SiO2
break down near 1 V/nm and thin films of Al2O3 and HfO2 lower; defects make real devices fail sooner.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from .cards import ParamSpec, num, si
from .labconfig import Guard

FIELD = {"hBN": 0.3, "SiO2": 0.3, "Al2O3": 0.2, "HfO2": 0.2}   # V/nm a gate stays below
LEAK_MAX = 1e-9      # A: the leakage that stops a gate's ramp, unless the person sets another
CAP_FACTOR = 10      # the source's own current limit, in units of leak_max
MAX_ITEMS = 24
# Instruments that set the sample's surroundings or position rather than applying anything to it. With hands on the
# sample they stay as they are: no temperature or field change, and nothing moves under the person's hands.
SURROUNDINGS = {"temperature-controller", "cryostat", "magnet-supply", "positioner", "motion-stage", "monochromator",
                "spectrometer", "camera", "power-meter", "multimeter", "nanovoltmeter", "photon-counter"}


def applies_to_sample(card: Any) -> bool:
    """Does this instrument apply something to the sample (a source, an excitation, light, RF)?"""
    return card.kind not in SURROUNDINGS


def gate_limit(dielectric: str, thickness_nm: Optional[float]) -> Optional[float]:
    """The largest |V| for a gate through this dielectric, or None when Kelvin has no rule for it."""
    field = FIELD.get(dielectric)
    if field is None or not thickness_nm or thickness_nm <= 0:
        return None
    return round(field * thickness_nm, 6)


def _number(x: Any, what: str) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise ValueError("%s needs a number" % what)
    if not math.isfinite(v):
        raise ValueError("%s needs a finite number" % what)
    return v


def _drive(lab: Any, key: Any, what: str) -> Tuple[Any, ParamSpec]:
    try:
        inst, spec = lab.resolve(str(key or ""))
    except KeyError as e:
        raise ValueError("%s: %s" % (what, e.args[0]))
    if not spec.is_drive or not spec.numeric or spec.set_cmd is None:
        raise ValueError("%s: %s.%s is not a numeric drive" % (what, inst.name, spec.name))
    return inst, spec


def normalize(raw: Any, lab: Any) -> Dict[str, Any]:
    """A person's protection for a device, checked against this lab's instruments. ValueError says what is wrong."""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("protection must be an object with limits and gates")
    limits: Dict[str, List[float]] = {}
    for key, pair in list((raw.get("limits") or {}).items())[:MAX_ITEMS]:
        inst, spec = _drive(lab, key, "a limit")
        name = "%s.%s" % (inst.name, spec.name)
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("%s: give its limits as [lowest, highest]" % name)
        lo, hi = _number(pair[0], name), _number(pair[1], name)
        safe = spec.safe if spec.safe is not None else 0
        if not lo <= safe <= hi:
            raise ValueError("%s: the limits must include its safe value (%s)" % (name, spec.show(safe)))
        limits[name] = [lo, hi]
    gates: List[Dict[str, Any]] = []
    for i, g in enumerate((raw.get("gates") or [])[:MAX_ITEMS]):
        if not isinstance(g, dict):
            raise ValueError("gate %d must be an object" % (i + 1))
        inst, spec = _drive(lab, g.get("drive"), "gate %d" % (i + 1))
        name = "%s.%s" % (inst.name, spec.name)
        dielectric = str(g.get("dielectric") or "other")[:40]
        thickness = _number(g["thickness_nm"], name + " thickness") if g.get("thickness_nm") not in (None, "") else None
        if thickness is not None and thickness <= 0:
            raise ValueError("%s: the dielectric's thickness must be above 0 nm" % name)
        limit = (_number(g["limit"], name + " limit") if g.get("limit") not in (None, "")
                 else gate_limit(dielectric, thickness))
        if not limit or limit <= 0:
            raise ValueError("%s: give its limit, or a dielectric (%s) and its thickness to work it out from"
                             % (name, ", ".join(FIELD)))
        leak = None
        if g.get("leak"):
            try:
                li, lspec = lab.resolve(str(g["leak"]))
            except KeyError as e:
                raise ValueError("%s leakage: %s" % (name, e.args[0]))
            if not lspec.numeric or lspec.get_cmd is None:
                raise ValueError("%s leakage: %s.%s is not a reading" % (name, li.name, lspec.name))
            leak = "%s.%s" % (li.name, lspec.name)
        leak_max = _number(g.get("leak_max") or LEAK_MAX, name + " leakage")
        if leak_max <= 0:
            raise ValueError("%s: the leakage that stops the ramp must be above 0" % name)
        rate = _number(g["rate"], name + " ramp rate") if g.get("rate") not in (None, "") else None
        if rate is not None and rate <= 0:
            raise ValueError("%s: the ramp rate must be above 0" % name)
        gates.append({"drive": name, "dielectric": dielectric, "thickness_nm": thickness, "limit": limit,
                      "leak": leak, "leak_max": leak_max, "rate": rate})
    return {"limits": limits, "gates": gates} if limits or gates else {}


def device_limits(protection: Dict[str, Any]) -> Dict[str, Tuple[float, float]]:
    """The drive limits a device sets: its own, narrowed by each gate's ±limit."""
    out = {k: (float(v[0]), float(v[1])) for k, v in (protection.get("limits") or {}).items()}
    for g in protection.get("gates") or []:
        lo, hi = out.get(g["drive"], (-math.inf, math.inf))
        out[g["drive"]] = (max(lo, -g["limit"]), min(hi, g["limit"]))
    return out


def range_for(spec: ParamSpec, need: float) -> Optional[float]:
    """The smallest of a source's ranges that still covers `need`; None when none does."""
    for step in spec.range_steps:
        if step >= need * (1 - 1e-9):
            return step
    return None


def caps(lab: Any, protection: Dict[str, Any]) -> List[Tuple[Any, str, Any, str]]:
    """What the instruments themselves are held to: (instrument, parameter, value, why). What this lab doesn't
    have is left out (apply names it)."""
    out: List[Tuple[Any, str, Any, str]] = []
    for key, (lo, hi) in device_limits(protection).items():
        try:
            inst, spec = lab.resolve(key)
        except KeyError:
            continue
        if spec.range_param and spec.range_steps:
            step = range_for(spec, max(abs(lo), abs(hi)))
            if step is not None:
                if spec.autorange_param:
                    out.append((inst, spec.autorange_param, False, "range kept fixed"))
                out.append((inst, spec.range_param, step, "range just above ±%s %s" % (num(max(abs(lo), abs(hi))),
                                                                                       spec.unit)))
    for g in protection.get("gates") or []:
        try:
            inst, spec = lab.resolve(g["drive"])
        except KeyError:
            continue
        if spec.current_limit:
            cl = inst.spec(spec.current_limit)
            floor = cl.safe if isinstance(cl.safe, (int, float)) else 0.0
            out.append((inst, spec.current_limit, max(floor, CAP_FACTOR * g["leak_max"]),
                        "%d× the leakage that stops the ramp" % CAP_FACTOR))
    return out


def clear(lab: Any) -> None:
    for inst in lab.instruments.values():
        inst.device_name = None
        inst.device_limits = {}
        inst.device_rates = {}
        inst.device_guards = {}


def apply(lab: Any, protection: Dict[str, Any], device: str) -> List[str]:
    """Hold this lab's instruments to a mounted device's protection (software side). Returns what could not be
    applied here: an instrument or parameter this lab doesn't have (the device was set up for another lab)."""
    clear(lab)
    missing: List[str] = []
    if not protection:
        return missing

    def where(key: str) -> Optional[Tuple[Any, ParamSpec]]:
        try:
            return lab.resolve(key)
        except KeyError:
            missing.append(key)
            return None

    for key, (lo, hi) in device_limits(protection).items():
        found = where(key)
        if found:
            found[0].device_name = device
            found[0].device_limits[found[1].name] = (lo, hi)
    for g in protection.get("gates") or []:
        found = where(g["drive"])
        if not found:
            continue
        inst, spec = found
        if g.get("rate"):
            inst.device_rates[spec.name] = float(g["rate"])
        if g.get("leak") and where(g["leak"]):
            li, lspec = lab.resolve(g["leak"])
            inst.device_guards.setdefault(spec.name, []).append(Guard(li.name, lspec.name, float(g["leak_max"])))
    for inst, param, value, _ in caps(lab, protection):   # the caps are limits too: nothing raises the range or the current limit
        pspec = inst.spec(param)
        if pspec.numeric:
            low = (inst.config.limits.get(param) or pspec.limits or (0.0, value))[0]
            inst.device_limits[param] = (min(low, value), value)
            inst.device_name = device
    return missing


def write_caps(gate: Any, lab: Any, protection: Dict[str, Any]) -> Tuple[List[str], bool]:
    """Write the caps into the instruments, through the gate. A range is changed only while its level is at safe,
    and a switched-off instrument gets nothing; those wait. Returns what was done, and whether every cap is in
    place (False: some wait)."""
    lines: List[str] = []
    done = True
    for inst, param, value, why in caps(lab, protection):
        if not inst.enabled:
            lines.append("%s is %s: its caps are written when it is on again" % (
                inst.name, "not connected" if inst.unavailable else "switched off"))
            done = False
            continue
        try:
            now = inst.read(param)
        except Exception:
            now = None
        owner = inst.card.range_owner(param)
        if isinstance(value, bool):
            if now is not None and bool(now) == value:
                continue
        elif isinstance(now, (int, float)) and not isinstance(now, bool) and (
                math.isclose(now, value, rel_tol=1e-9) if owner is not None else now <= value):
            continue   # a current limit at or below its cap stays; a range is the one that covers the limit
        if owner is not None:
            try:
                level = inst.read(owner.name)
            except Exception:
                level = None
            if level is None or owner.distance(level) > 0:
                lines.append("%s.%s: set when %s.%s is back at %s" % (inst.name, param, inst.name, owner.name,
                                                                     owner.show(owner.safe)))
                done = False
                continue
        r = gate.set(inst.name, param, value)
        lines.append(r.message if r.ok else "%s.%s not set: %s" % (inst.name, param, r.message))
    return lines, done


def outside(lab: Any, protection: Dict[str, Any]) -> List[str]:
    """Drives that sit outside a device's limits now (it was mounted, or its protection set, under bias). The gate
    lets them move only back within the limits, or to safe."""
    lines: List[str] = []
    for key, (lo, hi) in device_limits(protection).items():
        try:
            inst, spec = lab.resolve(key)
            now = inst.read(spec.name) if inst.enabled else None
        except Exception:
            continue
        if isinstance(now, (int, float)) and not isinstance(now, bool) and not lo <= now <= hi:
            lines.append("%s is at %s, outside the device's limits (%s to %s): bring it back within them or to %s"
                         % (key, spec.show(now), num(lo), num(hi), spec.show(spec.safe if spec.safe is not None else 0)))
    return lines


def combine(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Two devices' protection at once (a plan on another device of the mounted chip): the tighter of each holds."""
    if not a or not b:
        return a or b or {}
    limits = {k: [float(v[0]), float(v[1])] for k, v in (a.get("limits") or {}).items()}
    for k, (lo, hi) in (b.get("limits") or {}).items():
        mine = limits.get(k)
        limits[k] = [max(mine[0], lo), min(mine[1], hi)] if mine else [lo, hi]
    gates = {g["drive"]: dict(g) for g in a.get("gates") or []}
    for g in b.get("gates") or []:
        mine = gates.get(g["drive"])
        if mine is None:
            gates[g["drive"]] = dict(g)
            continue
        if g["limit"] < mine["limit"]:   # its dielectric is the one that sets the limit
            mine.update(limit=g["limit"], dielectric=g.get("dielectric"), thickness_nm=g.get("thickness_nm"))
        mine["leak_max"] = min(mine["leak_max"], g["leak_max"])
        mine["leak"] = mine.get("leak") or g.get("leak")
        rates = [r for r in (mine.get("rate"), g.get("rate")) if r]
        mine["rate"] = min(rates) if rates else None
    return {"limits": limits, "gates": list(gates.values())}


def options(lab: Any) -> Dict[str, Any]:
    """What can be protected in this lab, for the Lab panel: each numeric drive with the lab's own limits (and, for
    a source, the current reading that shows a gate's leakage), and the dielectrics Kelvin has a rule for."""
    drives: List[Dict[str, Any]] = []
    for name, inst in lab.instruments.items():
        params = list(inst.card.params.values())
        currents = [p for p in params if p.numeric and p.get_cmd is not None and not p.is_drive and p.unit == "A"]
        for spec in params:
            if not (spec.is_drive and spec.numeric and spec.set_cmd is not None):
                continue
            lim = inst.config.limits.get(spec.name) or spec.limits
            drives.append({"key": "%s.%s" % (name, spec.name), "label": "%s %s" % (name, spec.title),
                           "unit": spec.unit, "safe": spec.safe if spec.safe is not None else 0,
                           "limits": [lim[0], lim[1]] if lim else None,
                           "gate": spec.unit == "V" and applies_to_sample(inst.card),
                           "leak": "%s.%s" % (name, currents[0].name) if currents else None})
    readings = [{"key": "%s.%s" % (name, p.name), "unit": p.unit} for name, inst in lab.instruments.items()
                for p in inst.card.params.values()
                if p.numeric and p.get_cmd is not None and not p.is_drive and p.unit == "A"]
    return {"dielectrics": [{"name": k, "field": v} for k, v in FIELD.items()], "leak_max": LEAK_MAX,
            "cap_factor": CAP_FACTOR, "drives": drives, "readings": readings}


def summary(protection: Dict[str, Any], lab: Any = None) -> List[str]:
    """The protection in words, one line per gate or limit (for the Lab panel, the phone and the agent)."""
    lines: List[str] = []

    def unit(key: str) -> str:
        try:
            return lab.resolve(key)[1].unit if lab is not None else ""
        except KeyError:
            return ""

    for g in protection.get("gates") or []:
        u = unit(g["drive"]) or "V"
        bits = ["Gate %s within ±%s %s" % (g["drive"], num(g["limit"]), u)]
        if g.get("thickness_nm") and g.get("dielectric") in FIELD:
            bits[0] += " (%s nm %s)" % (num(g["thickness_nm"]), g["dielectric"])
        if g.get("rate"):
            bits.append("ramps at most %s %s/s" % (num(g["rate"]), u))
        if g.get("leak"):
            bits.append("stops above %s on %s" % (si(g["leak_max"], "A"), g["leak"]))
        lines.append(", ".join(bits))
    for key, (lo, hi) in (protection.get("limits") or {}).items():
        u = unit(key)
        lines.append("%s from %s to %s%s" % (key, num(lo), num(hi), " " + u if u else ""))
    if lab is not None:
        held = caps(lab, protection)
        if held:
            words = []
            for inst, param, value, _ in held:
                pspec = inst.spec(param)
                words.append("%s.%s %s" % (inst.name, param, "off" if value is False else "≤ " + pspec.show(value)))
            lines.append("Capped in the instruments: " + ", ".join(words))
    return lines
