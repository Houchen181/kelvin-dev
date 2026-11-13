"""The benchmark's tasks, by module (one area of lab work each). Adding a task: write a Task in one of these modules
(or a new module listed in MODULES) and add it to that module's TASKS; `python bench/run.py check --tasks <id>` then
plays its oracle through Kelvin to show the task and its checks work, before any model is paid for."""
from __future__ import annotations

import importlib
from typing import Dict, List

MODULES = ["measure", "nv", "analysis", "recovery", "safety", "overnight", "records", "setup"]


def all_tasks() -> List["object"]:
    out = []
    for name in MODULES:
        module = importlib.import_module("%s.%s" % (__name__, name))
        out += list(getattr(module, "TASKS", []))
    ids = [t.id for t in out]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError("task ids used twice: %s" % sorted(dupes))
    return out


def by_id() -> Dict[str, "object"]:
    return {t.id: t for t in all_tasks()}


def get(task_id: str) -> "object":
    tasks = by_id()
    if task_id not in tasks:
        raise KeyError("no task %r (python bench/run.py list)" % task_id)
    return tasks[task_id]


def select(spec: str = "all") -> List["object"]:
    """Tasks by id, category or tag, comma-separated ("all": every task; "pilot": the pilot set)."""
    tasks = all_tasks()
    if not spec or spec == "all":
        return tasks
    want = [s.strip() for s in spec.split(",") if s.strip()]
    out = [t for t in tasks if t.id in want or t.category in want or any(w in t.tags for w in want)]
    missing = [w for w in want if not any(t.id == w or t.category == w or w in t.tags for t in tasks)]
    if missing:
        raise KeyError("no task, category or tag %s" % missing)
    return out
