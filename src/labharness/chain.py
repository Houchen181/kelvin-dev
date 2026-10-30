"""The measurement chain of a device: what sits between the instruments and the sample, so a lock-in's volts become
the sample's resistance, and an excitation is stated as what it is (a 6221's AC amplitude is its peak; a lock-in's
sine output and its readings are rms).

F2 l10: a methods draft said "50 nA rms" for a 6221 set to 50 nA (peak), and Kelvin's reports plotted raw volts
where a person wanted R. Nothing in a run said which.

    excitation:                 # what drives the current through the sample
      source: iac.amplitude     # the setting that gives it (an instrument parameter)
      form: peak                # how that instrument states it: rms, peak, peak-to-peak or dc
      series_resistor: 10e6     # ohms, when a voltage drives the current through a resistor (a lock-in's sine out);
                                # left out for a current source
    readings:                   # what sits between the sample and each instrument reading
      li_xx.X: {gain: 100, name: R_xx}   # a preamplifier's gain (V/V); the derived column's name
      li_xy.X: {gain: 1, name: R_xy}
    notes: free text (the wiring, filters, where the reference comes from)

A device's chain is kept in the data folder (chains.json), set by a person or by Kelvin when a person says how it
is wired, and stamped into every run of that device (its meta, in run.h5), so the run says it whatever changes
later. The excitation's value comes from the run itself: the plan's setup, else the setting at its start."""
from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

FORMS = {"rms": 1.0, "peak": 1 / math.sqrt(2), "peak-to-peak": 1 / (2 * math.sqrt(2)), "dc": 1.0}
SETUP = "setup"   # the chain of the setup itself, for runs on no device


class ChainError(ValueError):
    """What is wrong with a chain, in words."""


def _number(v: Any, what: str) -> float:
    if isinstance(v, bool):
        raise ChainError("%s must be a number" % what)
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ChainError("%s must be a number, not %r" % (what, v))
    if not math.isfinite(x) or x <= 0:
        raise ChainError("%s must be a positive number, not %r" % (what, v))
    return x


def parse(data: Any) -> Dict[str, Any]:
    """A chain, checked: a mapping (or its YAML or JSON text) as in the module's docstring. ChainError says what is
    wrong."""
    if isinstance(data, str):
        import yaml
        try:
            data = yaml.safe_load(data)
        except yaml.YAMLError as e:
            raise ChainError("not valid YAML: %s" % str(e).splitlines()[0])
    if not isinstance(data, dict):
        raise ChainError("a chain is a mapping with excitation, readings and notes")
    unknown = sorted(set(data) - {"excitation", "readings", "notes"})
    if unknown:
        raise ChainError("unknown part %s (a chain has excitation, readings and notes)" % ", ".join(unknown))
    out: Dict[str, Any] = {}
    exc = data.get("excitation")
    if exc is not None:
        if not isinstance(exc, dict) or not isinstance(exc.get("source"), str) or "." not in exc["source"]:
            raise ChainError("excitation needs source: the instrument setting that gives it, e.g. iac.amplitude")
        form = str(exc.get("form") or "").strip().lower().replace(" ", "-")
        if form not in FORMS:
            raise ChainError("say whether %s is rms, peak, peak-to-peak or dc (form): the instrument's manual says; "
                             "a 6221's AC amplitude is its peak, a lock-in's sine output is rms" % exc["source"])
        out["excitation"] = {"source": exc["source"].strip(), "form": form}
        if exc.get("series_resistor") not in (None, ""):
            out["excitation"]["series_resistor"] = _number(exc["series_resistor"], "series_resistor (ohms)")
        extra = sorted(set(exc) - {"source", "form", "series_resistor"})
        if extra:
            raise ChainError("unknown excitation entry %s (it has source, form, series_resistor)" % ", ".join(extra))
    readings = data.get("readings") or {}
    if not isinstance(readings, dict):
        raise ChainError("readings maps each instrument reading (li_xx.X) to its gain and name")
    out_r: Dict[str, Dict[str, Any]] = {}
    for key, r in readings.items():
        if "." not in str(key):
            raise ChainError("a reading is an instrument's parameter, such as li_xx.X, not %r" % key)
        r = r if isinstance(r, dict) else {"gain": r}
        extra = sorted(set(r) - {"gain", "name"})
        if extra:
            raise ChainError("unknown entry %s for %s (a reading has gain and name)" % (", ".join(extra), key))
        out_r[str(key)] = {"gain": _number(r.get("gain", 1), "%s's gain" % key)}
        if r.get("name"):
            out_r[str(key)]["name"] = str(r["name"]).strip()[:40]
    if out_r:
        out["readings"] = out_r
    if data.get("notes"):
        out["notes"] = str(data["notes"]).strip()[:2000]
    if not out:
        raise ChainError("an empty chain: give its excitation, readings or notes")
    return out


def setting_in_run(key: str, plan: Dict[str, Any], start: Dict[str, Any]) -> Any:
    """A setting's value while a run measured: the plan's setup if it set it, else as the run found it."""
    setup = plan.get("setup") if isinstance(plan.get("setup"), dict) else {}
    if key in setup:
        return setup[key]
    inst, _, param = key.partition(".")
    v = (start.get(inst) or {}).get(param) if isinstance(start.get(inst), dict) else None
    return None if isinstance(v, str) else v


def excitation_current(chain: Dict[str, Any], value_of: Callable[[str], Any]) -> Optional[Tuple[float, str]]:
    """The excitation as an rms current through the sample (A), with how it was worked out; None when the chain has
    no excitation or its value isn't known (value_of(key) gives the setting's value, None when unknown)."""
    exc = (chain or {}).get("excitation")
    if not exc:
        return None
    v = value_of(exc["source"])
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v == 0:
        return None
    rms = abs(v) * FORMS[exc["form"]]
    unit = "V" if exc.get("series_resistor") else "A"
    how = "%s = %s %s" % (exc["source"], _si(abs(v), unit), exc["form"])
    if exc["form"] not in ("rms", "dc"):
        how += " = %s rms" % _si(rms, unit)
    if exc.get("series_resistor"):
        rms /= exc["series_resistor"]
        how += " through %s = %s rms" % (_si(exc["series_resistor"], "Ω"), _si(rms, "A"))
    return rms, how


def derived(chain: Dict[str, Any], key: str, unit: str, current: Optional[Tuple[float, str]]
            ) -> Optional[Tuple[float, str, str, str]]:
    """How a voltage reading becomes the sample's resistance: (factor, name, unit, how). R = V / gain / I_rms, so the
    reading times factor. None unless the chain names the reading, it is in volts and the excitation is known."""
    r = ((chain or {}).get("readings") or {}).get(key)
    if r is None or unit != "V" or current is None:
        return None
    i_rms, how = current
    factor = 1.0 / (r["gain"] * i_rms)
    name = r.get("name") or "R from %s" % key
    gain = " / gain %s" % _g(r["gain"]) if r["gain"] != 1 else ""
    return factor, name, "Ω", "%s = %s%s / I, I = %s" % (name, key, gain, how)


def derived_readings(chain: Any, setup: Any, start: Any, measures: Any) -> Dict[str, Tuple[float, str, str, str]]:
    """The readings a run's measurement chain turns into the sample's resistance: {key: (factor, name, unit, how)},
    from the chain stamped into the run, the excitation it measured with (its plan's setup, else the setting at its
    start) and its measured columns. Empty when the run carries no chain, or its excitation isn't known."""
    if not isinstance(chain, dict):
        return {}
    plan = {"setup": setup if isinstance(setup, dict) else {}}
    current = excitation_current(chain, lambda k: setting_in_run(k, plan, start if isinstance(start, dict) else {}))
    out = {}
    for m in measures or []:
        d = derived(chain, m["key"], m.get("unit") or "", current)
        if d:
            out[m["key"]] = d
    return out


def lines(chain: Dict[str, Any], current: Optional[Tuple[float, str]] = None) -> List[str]:
    """The chain in words, one line each."""
    out = []
    exc = (chain or {}).get("excitation")
    if exc:
        out.append("excitation: %s, stated as %s%s%s" % (
            exc["source"], exc["form"], ", through a %s series resistor" % _si(exc["series_resistor"], "Ω")
            if exc.get("series_resistor") else " (a current source)", "; in this run: " + current[1] if current else ""))
    for key, r in ((chain or {}).get("readings") or {}).items():
        out.append("reading %s: gain %s before the instrument%s" % (key, _g(r["gain"]),
                                                                    ", gives %s" % r["name"] if r.get("name") else ""))
    if (chain or {}).get("notes"):
        out.append("notes: %s" % chain["notes"])
    return out


def _g(x: float) -> str:
    return ("%.6g" % x)


def _si(x: float, unit: str) -> str:
    from .cards import si
    return si(x, unit)


class Chains:
    """The measurement chains kept in a data folder: one per device (by its id, else its name) and one for the setup
    itself (runs on no device)."""

    FILE = "chains.json"

    def __init__(self, data_dir: Any):
        self.path = Path(data_dir) / self.FILE
        self._lock = threading.Lock()

    def _read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def key(device: Optional[str], device_name: Optional[str]) -> str:
        if device:
            return "device:" + str(device)
        if device_name:
            return "name:" + str(device_name).strip().lower()
        return SETUP

    def get(self, device: Optional[str] = None, device_name: Optional[str] = None,
            fallback: bool = True) -> Optional[Dict[str, Any]]:
        """The device's chain (by id, else by name) as kept: {chain, device_name, of: device or setup, by, at}. A
        device with none of its own has the setup's (the bias resistor and the preamplifier are usually the
        setup's), unless fallback is False. None when there is none."""
        data = self._read()
        keys = [self.key(device, None) if device else None, self.key(None, device_name) if device_name else None]
        for k in keys + ([SETUP] if fallback or not (device or device_name) else []):
            if k and isinstance(data.get(k), dict) and isinstance(data[k].get("chain"), dict):
                return dict(data[k], of="setup" if k == SETUP else "device")
        return None

    def every(self) -> Dict[str, Dict[str, Any]]:
        """Every chain on record, the setup's first: {key: {chain, device_name, by, at}} (the lab's memory lists
        them)."""
        data = {k: v for k, v in self._read().items() if isinstance(v, dict) and isinstance(v.get("chain"), dict)}
        return dict(sorted(data.items(), key=lambda kv: kv[0] != SETUP))

    def put(self, chain: Dict[str, Any], device: Optional[str], device_name: Optional[str], by: str) -> None:
        with self._lock:
            data = self._read()
            data[self.key(device, device_name)] = {"chain": chain, "device_name": device_name, "by": by,
                                                   "at": datetime.now().isoformat(timespec="seconds")}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)

    def remove(self, device: Optional[str], device_name: Optional[str]) -> bool:
        with self._lock:
            data = self._read()
            gone = data.pop(self.key(device, device_name), None) is not None
            if gone:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self.path)
            return gone
