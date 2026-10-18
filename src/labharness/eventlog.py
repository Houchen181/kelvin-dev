"""The lab notebook's raw record: an append-only event log and per-run folders."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ._version import __version__


def _jsonable(v: Any) -> Any:
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (set, frozenset)):
        return sorted(_jsonable(x) for x in v)
    if isinstance(v, tuple):
        return [_jsonable(x) for x in v]
    try:
        json.dumps(v)
        return v
    except TypeError:
        return str(v)


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


class EventLog:
    """One JSON object per line; never rewritten. path=None keeps it in memory."""

    def __init__(self, path: Any = None, clock: Any = None):
        self.path = Path(path) if path else None
        self.clock = clock
        self._memory: List[Dict[str, Any]] = []
        self._seq = 0
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                with open(self.path, "rb") as f:
                    self._seq = sum(1 for _ in f)

    def write(self, type_: str, **fields: Any) -> Dict[str, Any]:
        self._seq += 1
        event: Dict[str, Any] = {"seq": self._seq, "wall": _now(), "type": type_}
        if self.clock is not None:
            event["t"] = round(self.clock.now(), 6)
        for k, v in fields.items():
            if v is not None:
                event[k] = _jsonable(v)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        else:
            self._memory.append(event)
        return event

    def events(self) -> List[Dict[str, Any]]:
        if not self.path:
            return list(self._memory)
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


class RunRecord:
    """One run: plan.yaml, snapshot_start/end.json, data.jsonl, meta.json."""

    def __init__(self, run_id: str, path: Optional[Path], plan: Dict[str, Any], operator: str):
        self.run_id = run_id
        self.path = path
        self.rows: List[Dict[str, Any]] = []
        self.meta: Dict[str, Any] = {"run_id": run_id, "plan": plan.get("plan"), "operator": operator,
                                     "started": _now(), "labharness": __version__}
        if path:
            path.mkdir(parents=True)
            with open(path / "plan.yaml", "w", encoding="utf-8") as f:
                yaml.safe_dump(plan, f, sort_keys=False, allow_unicode=True)

    def snapshot(self, kind: str, data: Dict[str, Any]) -> None:
        self.meta["snapshot_" + kind] = True
        if self.path:
            with open(self.path / ("snapshot_%s.json" % kind), "w", encoding="utf-8") as f:
                json.dump(_jsonable(data), f, indent=1, default=str)

    def add(self, row: Dict[str, Any]) -> None:
        self.rows.append(row)
        if self.path:
            with open(self.path / "data.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({k: _jsonable(v) for k, v in row.items()}) + "\n")

    def finish(self, **meta: Any) -> None:
        self.meta.update({k: _jsonable(v) for k, v in meta.items()})
        self.meta["finished"] = _now()
        if self.path:
            with open(self.path / "meta.json", "w", encoding="utf-8") as f:
                json.dump(self.meta, f, indent=1)


class RunStore:
    def __init__(self, root: Any = None):
        self.root = Path(root) if root else None

    def create(self, plan_name: str, plan: Dict[str, Any], operator: str) -> RunRecord:
        base = "%s-%s" % (datetime.now().strftime("%Y%m%d-%H%M%S"), re.sub(r"[^\w.-]+", "_", plan_name))
        run_id, n = base, 1
        while self.root and (self.root / run_id).exists():
            n += 1
            run_id = "%s-%d" % (base, n)
        return RunRecord(run_id, self.root / run_id if self.root else None, plan, operator)
