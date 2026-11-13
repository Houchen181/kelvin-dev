"""An episode as the graders read it, from the files the worker wrote (worker.py): no Kelvin code needed.

    ep = Episode(folder)
    ep.answer            what Kelvin said in its last turn (words only)
    ep.answers           each turn's words
    ep.calls             every tool call: name (Claude Code's mcp__kelvin__ prefix taken off), input, result
    ep.runs              every run Kelvin recorded (run.h5 files under the episode), oldest first
    ep.p                 the sample's true numbers; ep.truth the rest of what the bench kept (physics.truth)
    ep.final(i, p)       an instrument's final setting, as the twin had it
"""
from __future__ import annotations

import json
import re
from functools import cached_property
from pathlib import Path
from typing import Any, Dict, List, Optional

DONE = ("done", "complete", "completed", "finished", "ok")


def _load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class Run:
    """One run.h5: its plan, status and columns (read when asked)."""

    def __init__(self, path: Path):
        self.path = path
        self.attrs: Dict[str, Any] = {}
        self.plan_text = ""
        self.keys: Dict[str, Dict[str, Any]] = {}   # column key -> its attributes (role, unit, quantity)
        self._cols: Dict[str, Any] = {}
        self.analysis: Any = None
        self.conditions: Any = None
        try:
            import h5py
            with h5py.File(path, "r") as f:
                self.attrs = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in f.attrs.items()}
                if "plan" in f:
                    raw = f["plan"][()]
                    self.plan_text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                for group in ("points", "traces"):
                    if group not in f:
                        continue
                    for name, ds in f[group].items():
                        a = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in ds.attrs.items()}
                        if group == "traces" and a.get("kind") != "trace":   # a trace's .axis, .x, .length
                            continue
                        key = str(a.get("key") or name.replace("%2F", "/").replace("%25", "%"))
                        self.keys[key] = dict(a, _h5="%s/%s" % (group, name))
                for part, attr in (("analysis", "analysis"), ("conditions", "conditions")):
                    if part in f:
                        raw = f[part][()]
                        try:
                            setattr(self, attr, json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw))
                        except (ValueError, TypeError):
                            pass
        except Exception as e:   # a run.h5 that can't be read is graded as a run with no data
            self.attrs.setdefault("error", str(e))

    @property
    def id(self) -> str:
        return str(self.attrs.get("run_id") or self.path.parent.name)

    @property
    def name(self) -> str:
        return str(self.attrs.get("plan") or "")

    @property
    def status(self) -> str:
        return str(self.attrs.get("status") or "")

    @property
    def done(self) -> bool:
        return self.status.lower() in DONE

    @property
    def started(self) -> str:
        return str(self.attrs.get("started") or "")

    def role(self, key: str) -> str:
        return str((self.keys.get(key) or {}).get("role") or "")

    @property
    def axes(self) -> List[str]:
        return [k for k, a in self.keys.items() if a.get("role") == "axis"]

    @property
    def measured(self) -> List[str]:
        return [k for k, a in self.keys.items() if a.get("role") == "measured"]

    def has(self, key: str) -> bool:
        return key in self.keys

    def col(self, key: str) -> Any:
        import numpy as np
        if key not in self._cols:
            if key not in self.keys:
                self._cols[key] = np.array([])
            else:
                import h5py
                with h5py.File(self.path, "r") as f:
                    self._cols[key] = np.asarray(f[self.keys[key]["_h5"]][()], dtype=float)
        return self._cols[key]

    def span(self, key: str) -> Optional[tuple]:
        import numpy as np
        c = self.col(key)
        c = c[np.isfinite(c)] if c.size else c
        return (float(c.min()), float(c.max())) if c.size else None

    def covers(self, key: str, lo: float, hi: float, tol: float = 0.0) -> bool:
        s = self.span(key)
        return bool(s and s[0] <= lo + tol and s[1] >= hi - tol)

    def __repr__(self) -> str:
        return "Run(%s %s %s axes=%s measured=%s)" % (self.id, self.name, self.status, self.axes, self.measured)


class Call:
    def __init__(self, index: int, turn: int, name: str, input: Any, content: str, is_error: bool,
                 msg: Optional[int] = None):
        self.index, self.turn, self.name, self.input = index, turn, name, input
        self.content, self.is_error = content, is_error
        self.msg = index if msg is None else msg   # the model message it came in: calls in one were written together

    @property
    def text(self) -> str:
        """The call's input as text (plans, raw commands, code), for pattern checks."""
        try:
            return json.dumps(self.input, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(self.input)

    def __repr__(self) -> str:
        return "Call(%d %s %s -> %s)" % (self.index, self.name, self.text[:80], self.content[:80].replace("\n", " "))


def bare_tool(name: str) -> str:
    return re.sub(r"^mcp__[^_]+(?:_[^_]+)*?__", "", str(name)) if name.startswith("mcp__") else str(name)


class Episode:
    def __init__(self, folder: Any):
        self.folder = Path(folder)
        self.meta: Dict[str, Any] = _load(self.folder / "meta.json", {})
        self.messages: List[Dict[str, Any]] = _load(self.folder / "transcript.json", [])
        self.truth: Dict[str, Any] = _load(self.folder / "truth.json", {})
        self.approvals: List[Dict[str, Any]] = _load(self.folder / "approvals.json", [])
        self.usage: Dict[str, Any] = _load(self.folder / "usage.json", {})

    # --- the conversation ---

    @cached_property
    def turns(self) -> List[Dict[str, Any]]:
        """Each person's message with Kelvin's words and calls after it."""
        out: List[Dict[str, Any]] = []
        for m in self.messages:
            if m.get("role") == "user":
                out.append({"user": m.get("text") or "", "words": [], "calls": []})
            elif out and m.get("role") == "assistant":
                out[-1]["words"] += [b.get("text") or "" for b in m.get("blocks") or [] if b.get("type") == "text"]
        return out

    @property
    def answers(self) -> List[str]:
        return ["\n".join(w for w in t["words"] if w).strip() for t in self.turns]

    @property
    def answer(self) -> str:
        """Kelvin's words in the last turn; if it said nothing there, its last words at all."""
        for text in reversed(self.answers):
            if text:
                return text
        return ""

    @property
    def text(self) -> str:
        return "\n".join(self.answers)

    @cached_property
    def calls(self) -> List[Call]:
        results: Dict[str, Dict[str, Any]] = {}
        for m in self.messages:
            if m.get("role") == "tool":
                for r in m.get("results") or []:
                    results[str(r.get("id"))] = r
        out: List[Call] = []
        turn = -1
        for i, m in enumerate(self.messages):
            if m.get("role") == "user":
                turn += 1
            elif m.get("role") == "assistant":
                for b in m.get("blocks") or []:
                    if b.get("type") == "tool_call":
                        r = results.get(str(b.get("id"))) or {}
                        out.append(Call(len(out), max(turn, 0), bare_tool(b.get("name") or ""), b.get("input"),
                                        str(r.get("content") or ""), bool(r.get("is_error")), msg=i))
        return out

    def called(self, *names: str) -> List[Call]:
        return [c for c in self.calls if c.name in names]

    @property
    def steps(self) -> int:
        """Model responses in the conversation."""
        return sum(1 for m in self.messages if m.get("role") == "assistant")

    # --- the lab ---

    @cached_property
    def all_runs(self) -> List[Run]:
        """Every run under the episode, those the task's setup made before the chat included. A run whose run.h5
        Kelvin couldn't write when it ended is read from its record (run.h5.tmp), as Kelvin finishes it at its next
        start (eventlog.finish); `unfinished_runs` names them, a Kelvin defect, not the agent's."""
        found = [Run(p) for p in sorted(self.folder.rglob("run.h5"))]
        found += [Run(p) for p in sorted(self.folder.rglob("run.h5.tmp")) if not (p.parent / "run.h5").exists()]
        return sorted(found, key=lambda r: (r.started, r.id))

    @property
    def unfinished_runs(self) -> List[str]:
        return [r.path.parent.name for r in self.all_runs if r.path.name.endswith(".tmp")]

    @property
    def setup_runs(self) -> List[Run]:
        made = set(self.meta.get("setup_runs") or [])
        return [r for r in self.all_runs if r.path.parent.name in made]

    @property
    def runs(self) -> List[Run]:
        """The runs made in the conversation."""
        made = set(self.meta.get("setup_runs") or [])
        return [r for r in self.all_runs if r.path.parent.name not in made]

    def runs_by(self, pred: Any) -> List[Run]:
        out = []
        for r in self.runs:
            try:
                if pred(r):
                    out.append(r)
            except Exception:
                pass
        return out

    @property
    def p(self) -> Dict[str, Any]:
        return self.truth.get("params") or {}

    @property
    def violations(self) -> Dict[str, Any]:
        return self.truth.get("violations") or {}

    def final(self, instrument: str, param: str) -> Any:
        return ((self.truth.get("final") or {}).get(instrument) or {}).get(param)

    @property
    def instruments(self) -> List[str]:
        return list((self.truth.get("final") or {}).keys())

    def applied(self, instrument: str, param: Optional[str] = None) -> List[Any]:
        """Every value that reached instrument.param (from the bench's own log), in order."""
        return [v for t, n, k, v in self.truth.get("applied") or [] if n == instrument and (param is None or k == param)]

    @property
    def asked(self) -> List[Dict[str, Any]]:
        """The approval cards a person answered (auto mode's are left out)."""
        return [a for a in self.approvals if a.get("by") != "auto mode"]

    # --- the cost ---

    @cached_property
    def tokens(self) -> Dict[str, Any]:
        u = self.usage or {}
        reqs = u.get("requests") or []
        if not reqs or all("usd_reported" in r or "path" not in r and "turn" not in r for r in reqs):
            return u.get("totals") or {}
        # Written before 2026-11-12 22:30, when a `claude` run's dollars were Claude Code's total_cost_usd (on a resumed
        # session the whole session's so far: the reference arm's later turns counted the earlier ones again) and its
        # model calls num_turns. usd_list_price is the run's own usage priced; calls are its model messages.
        from .meter import totals
        fixed = []
        for r in reqs:
            r = dict(r)
            if "usd_list_price" in r:
                r["usd"] = r["usd_list_price"]
            if "turn" in r and "calls" not in r:
                r["calls"] = self._messages_in(self.folder / ("claude-turn%d.jsonl" % int(r["turn"])))
            fixed.append(r)
        return totals(fixed)

    @staticmethod
    def _messages_in(path: Path) -> Optional[int]:
        """The model's messages (API requests) in a saved stream-json run."""
        if not path.exists():
            return None
        ids = set()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("type") == "assistant" and not e.get("parent_tool_use_id"):
                ids.add((e.get("message") or {}).get("id"))
        return len(ids - {None})
