"""Procedures (SOPs): how this lab does a thing, step by step, every version kept (phase 3 of the ELN plan; asked for
2026-11-07: "Kelvin's data saving protocol and experimental procedure recording should follow the practice of modern
ELN/LIMS system, and be ai-native").

    procedure: cool-and-check
    description: Cool the sample and check its gate before any sweep.
    steps:
      - name: load
        by: hand                      # a person does it; Kelvin records it (record_step, naming procedure and step)
        do: Load the chip and check pins 1-8 to ground with the multimeter.
        expect: below 10 kOhm         # what the person checks, in words
      - name: leakage
        by: kelvin                    # Kelvin runs it (run_procedure_step), through the gate like any plan
        plan: |
          plan: gate_leak
          sweep: {smu.V: {from: 0, to: 2, step: 0.1}}
          measure: [smu.I]
        expect: max(abs(smu.I)) < 1e-9   # checked on the run's columns, as compute takes it

    ~/Kelvin/procedures/<name>/v<N>.yaml    each version byte for byte as it was written; an edit is a new version
    ~/Kelvin/procedures/<name>/versions.json  who wrote each version and when, and what it was edited from

A run a step makes keeps the procedure, its version and the step in its meta, with what was changed from the step's
plan (deviations); a step done by hand is kept in the lab's record by record_step. procedure_status reads them back:
which steps were done on a sample, when, by whom, how their checks came out, and what is next."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .recipes import slug

BY = ("hand", "kelvin")
_lock = threading.Lock()


class ProcedureError(ValueError):
    pass


def parse(text: str) -> Dict[str, Any]:
    """A procedure's text, checked: its name, description and steps (name, by, do, plan, expect)."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ProcedureError("not YAML: %s" % str(e).splitlines()[0])
    if not isinstance(doc, dict) or not str(doc.get("procedure") or "").strip():
        raise ProcedureError("a procedure starts with procedure: its name")
    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ProcedureError("a procedure has steps: a list, each with name and by (hand or kelvin)")
    seen: List[str] = []
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict) or not str(s.get("name") or "").strip():
            raise ProcedureError("step %d needs a name" % i)
        name = str(s["name"]).strip()
        if name.lower() in seen:
            raise ProcedureError("two steps are called %s" % name)
        seen.append(name.lower())
        by = str(s.get("by") or "").strip().lower()
        if by not in BY:
            raise ProcedureError("step %s: by is hand (a person does it) or kelvin (Kelvin runs its plan)" % name)
        if by == "kelvin" and not isinstance(s.get("plan"), str):
            raise ProcedureError("step %s is Kelvin's: it needs a plan (the plan's text, as run_plan takes it)" % name)
        if by == "hand" and not str(s.get("do") or "").strip():
            raise ProcedureError("step %s is done by hand: do says what the person does" % name)
    return doc


def step_of(doc: Dict[str, Any], name: str) -> Dict[str, Any]:
    wanted = str(name or "").strip().lower()
    for i, s in enumerate(doc["steps"], 1):
        if str(s["name"]).lower() == wanted or wanted == str(i):
            return dict(s, number=i, by=str(s["by"]).lower())
    raise KeyError("procedure %s has no step %r (its steps: %s)" % (doc["procedure"], name,
                                                                   ", ".join(str(s["name"]) for s in doc["steps"])))


def _set_path(doc: Any, path: str, value: Any) -> Any:
    """Change one value of a plan by its path (sweep.smu.V.to: keys may hold dots); returns what it was."""
    parts = str(path).split(".")
    node, i = doc, 0
    while True:
        for j in range(len(parts), i, -1):
            key = ".".join(parts[i:j])
            if isinstance(node, dict) and key in node:
                if j == len(parts):
                    before = node[key]
                    node[key] = value
                    return before
                node, i = node[key], j
                break
        else:
            raise ProcedureError("the step's plan has no %s (a change replaces a value it has; a new one is an edit "
                                 "of the procedure)" % path)


def with_changes(plan_text: str, changes: Dict[str, Any]) -> Tuple[str, List[str]]:
    """The step's plan with the person's changes, and each change as a deviation ("sweep.smu.V.to: 2 → 3")."""
    if not changes:
        return plan_text, []
    doc = yaml.safe_load(plan_text)
    out: List[str] = []
    for path, value in changes.items():
        before = _set_path(doc, path, value)
        if before != value:
            out.append("%s: %s → %s" % (path, before, value))
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), out


class ProcedureBook:
    def __init__(self, root: Any):
        self.root = Path(root)

    def _folder(self, name: str) -> Path:
        return self.root / slug(name)

    def versions(self, name: str) -> List[Dict[str, Any]]:
        try:
            data = json.loads((self._folder(name) / "versions.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def names(self) -> List[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and (p / "versions.json").exists())

    def text(self, name: str, version: int = 0) -> Tuple[str, int]:
        """A version's text (the latest when version is 0) and its number. KeyError: none."""
        vs = self.versions(name)
        if not vs:
            raise KeyError("no procedure %r (procedures lists them)" % name)
        n = version or vs[-1]["version"]
        try:
            return (self._folder(name) / ("v%d.yaml" % n)).read_text(encoding="utf-8"), n
        except OSError:
            raise KeyError("procedure %s has no version %d" % (name, n))

    def save(self, text: str, by: str, account: Optional[str] = None,
             ai: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], int, bool]:
        """Keep a procedure's text as its next version: (the procedure, its version, whether it is new). The same
        text as its latest version is not kept again."""
        doc = parse(text)
        name = str(doc["procedure"]).strip()
        with _lock:
            folder = self._folder(name)
            vs = self.versions(name)
            if vs:
                latest, n = self.text(name)
                if latest == text:
                    return doc, n, False
            n = (vs[-1]["version"] if vs else 0) + 1
            folder.mkdir(parents=True, exist_ok=True)
            (folder / ("v%d.yaml" % n)).write_text(text, encoding="utf-8")
            entry: Dict[str, Any] = {"version": n, "at": datetime.now().isoformat(timespec="seconds"), "by": by,
                                     "from": n - 1 if n > 1 else None, "steps": len(doc["steps"])}
            if account:
                entry["account"] = account
            if ai:
                entry["ai"] = ai
            tmp = folder / "versions.tmp"
            tmp.write_text(json.dumps(vs + [entry], ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(str(tmp), str(folder / "versions.json"))
            return doc, n, True


def outline(doc: Dict[str, Any]) -> List[str]:
    out = []
    for i, s in enumerate(doc["steps"], 1):
        by = str(s["by"]).lower()
        what = str(s.get("do") or "").strip() if by == "hand" else \
            "plan %s" % ((yaml.safe_load(s["plan"]) or {}).get("plan") if isinstance(s.get("plan"), str) else "?")
        out.append("%d %s (%s): %s%s" % (i, s["name"], "by hand" if by == "hand" else "Kelvin", what,
                                         "; expect %s" % s["expect"] if s.get("expect") else ""))
    return out
