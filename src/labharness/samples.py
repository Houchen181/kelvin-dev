"""The lab's samples as a LIMS keeps them (phase 2 of the ELN plan; asked for 2026-11-07: "Kelvin's data saving
protocol and experimental procedure recording should follow the practice of modern ELN/LIMS system").

What each sample is (kind), what it was cut or made from (parent: lineage, wafer to chip to device), where it came
from (source: grower, batch, supplier), its state and where it is kept (location), each change with who made it and
when. Its timeline is read from what Kelvin keeps (LabRuntime.sample_history): mounts, steps done by hand and
observations, its runs with their notes, and the cool-downs and warm-ups its runs' temperatures show.

    <data folder>/samples.json
    {"samples": {"<device id, or name:<name>>": {"name", "kind", "parent", "source", "state", "location",
                                                 "material", "description", "project", "created",
                                                 "changes": [{"at", "by", "account", "ai", "fields"}]}}}

A project's devices (Project settings) are samples whose name and protection are kept there; this file adds what a
LIMS keeps about them. A sample may be no project's device (the wafer the chips came from). Nothing here is deleted:
each change keeps what it changed from."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

FILE = "samples.json"
KINDS = ("wafer", "chip", "device", "crystal", "flake", "film", "powder", "solution", "other")
FIELDS = ("kind", "parent", "source", "state", "location", "material", "description")
MAX_FIELD = 200
COLD_K = 30.0     # a run started below this is cold (a cool-down between a warm run and it)
WARM_K = 200.0    # above this, warm


def _key(device_id: Optional[str], name: str) -> str:
    return device_id or "name:" + " ".join(str(name).split()).lower()


class SampleBook:
    def __init__(self, data_dir: Any):
        self.path = Path(data_dir) / FILE
        self._lock = threading.Lock()

    def _read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"samples": {}}
        return data if isinstance(data, dict) and isinstance(data.get("samples"), dict) else {"samples": {}}

    def _write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(str(tmp), str(self.path))

    def all(self) -> Dict[str, Dict[str, Any]]:
        return self._read()["samples"]

    def find(self, device_id: Optional[str], name: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        """(its key, the sample) by its device id or its name (any case); the sample None when it has no entry."""
        samples = self.all()
        if device_id and device_id in samples:
            return device_id, samples[device_id]
        wanted = " ".join(str(name or "").split()).lower()
        for key, s in samples.items():
            if str(s.get("name", "")).lower() == wanted:
                return key, s
        return _key(device_id, name), None

    def lineage(self, name: str) -> List[str]:
        """The samples it came from, nearest first (its parent, the parent's parent...)."""
        out: List[str] = []
        samples = self.all()
        by_name = {str(s.get("name", "")).lower(): s for s in samples.values()}
        s = by_name.get(str(name).lower())
        while s and s.get("parent") and str(s["parent"]).lower() not in [x.lower() for x in out + [name]]:
            out.append(str(s["parent"]))
            s = by_name.get(str(s["parent"]).lower())
        return out

    def children(self, name: str) -> List[str]:
        return [str(s.get("name")) for s in self.all().values()
                if str(s.get("parent", "")).lower() == str(name).lower()]

    def update(self, device_id: Optional[str], name: str, fields: Dict[str, Any], by: str,
               account: Optional[str] = None, ai: Optional[Dict[str, Any]] = None,
               project: Optional[str] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Create or change a sample: (the sample now, what changed {field: [before, after]}). A field given as
        "none" is cleared. A parent not kept yet is kept too, bare. ValueError: a kind that isn't one, a parent that
        would make a loop, nothing to change."""
        name = " ".join(str(name or "").split())[:80]
        if not name:
            raise ValueError("name the sample")
        clean: Dict[str, Optional[str]] = {}
        for k, v in fields.items():
            if k not in FIELDS or v is None or str(v).strip() == "":
                continue
            v = " ".join(str(v).split())[:MAX_FIELD]
            clean[k] = None if v.lower() == "none" else v
        if clean.get("kind"):
            clean["kind"] = clean["kind"].lower()
            if clean["kind"] not in KINDS:
                raise ValueError("a sample's kind is one of %s" % ", ".join(KINDS))
        with self._lock:
            data = self._read()
            samples = data["samples"]
            key, s = self.find(device_id, name)
            parent = clean.get("parent")
            if parent:
                chain = [parent.lower()]
                p = next((x for x in samples.values() if str(x.get("name", "")).lower() == parent.lower()), None)
                while p and p.get("parent"):
                    if str(p["parent"]).lower() in chain:
                        break
                    chain.append(str(p["parent"]).lower())
                    p = next((x for x in samples.values()
                              if str(x.get("name", "")).lower() == str(p["parent"]).lower()), None)
                if name.lower() in chain:
                    raise ValueError("%s can't come from %s: %s came from it" % (name, parent, parent))
            now = datetime.now().isoformat(timespec="seconds")
            new = s is None
            if new:
                s = {"name": name, "created": now, "changes": []}
                if project:
                    s["project"] = project
            changed = {k: [s.get(k), v] for k, v in clean.items() if s.get(k) != v}
            if not changed and not new:
                raise ValueError("%s already says that" % name)
            for k, (_, v) in changed.items():
                if v is None:
                    s.pop(k, None)
                else:
                    s[k] = v
            change = {"at": now, "by": by, "fields": changed}
            if account:
                change["account"] = account
            if ai:
                change["ai"] = ai
            s.setdefault("changes", []).append(change)
            samples[key] = s
            if parent and not any(str(x.get("name", "")).lower() == parent.lower() for x in samples.values()):
                samples[_key(None, parent)] = {"name": parent, "created": now, "changes": [
                    {"at": now, "by": by, "fields": {}, "why": "kept as %s's parent" % name}],
                    **({"project": project} if project else {})}
            self._write(data)
            return dict(s), changed


def describe(s: Dict[str, Any], lineage: List[str], children: List[str]) -> str:
    """A sample in one line: name (kind; material; from parent ← its parent; source; state; location)."""
    bits = [s.get("kind"), s.get("material")]
    if lineage:
        bits.append("from " + " ← ".join(lineage))
    bits += [("source " + s["source"]) if s.get("source") else None, s.get("state"),
             ("kept in " + s["location"]) if s.get("location") else None]
    head = "%s (%s)" % (s.get("name"), "; ".join(b for b in bits if b)) if any(bits) else str(s.get("name"))
    if children:
        head += "; made into " + ", ".join(children)
    if s.get("description"):
        head += ". " + s["description"]
    return head


def thermal_cycles(runs: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """Cool-downs and warm-ups the runs' temperatures at their start show (each run's coldest reading in K), as
    (when, text) between the runs that show them: "cooled down (run A at 295 K, run B at 1.6 K)"."""
    out: List[Tuple[str, str]] = []
    last: Optional[Dict[str, Any]] = None
    n = 0
    for r in runs:
        k = r.get("kelvin")
        if not isinstance(k, (int, float)):
            continue
        if last is not None:
            was = last["kelvin"]
            if was > WARM_K and k < COLD_K:
                n += 1
                out.append((r.get("started") or "", "cool-down %d (inferred: run %s at %g K, then run %s at %g K)"
                            % (n, last["run_id"], was, r["run_id"], k)))
            elif was < COLD_K and k > WARM_K:
                out.append((r.get("started") or "", "warmed up (inferred: run %s at %g K, then run %s at %g K)"
                            % (last["run_id"], was, r["run_id"], k)))
        if k > WARM_K or k < COLD_K:
            last = r
    return out
