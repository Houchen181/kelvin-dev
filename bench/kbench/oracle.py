"""A scripted model for dry runs: it plays a task's oracle (a known solution) through Kelvin's real chat loop, so a
task, its lab and its checks can be tested with no model and no cost (python bench/run.py check).

An oracle is a list of steps, each one model response:
    {"tool": "run_plan", "input": {...}}        one tool call
    [{"tool": ...}, {"tool": ...}]              several in one response
    "text"                                      an answer with no tool call: ends the person's turn
    fn(ctx) -> any of the above                 worked out from what the tools answered so far (ctx: Ctx)
When the steps run out, the answer is task.oracle_answer (text, or fn(ctx) -> text).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

RUN_ID = re.compile(r"\brun (\d{8}-\d{6}-[\w.-]*\w)|run_id='([^']+)'")


class Ctx:
    """What an oracle step may look at: the tool results so far, and the bench's truth (to write a correct answer)."""

    def __init__(self, history: List[Dict[str, Any]], env: Any):
        self.history = history
        self.env = env

    @property
    def p(self) -> Dict[str, Any]:
        return self.env.world.p if self.env is not None else {}

    def results(self) -> List[Dict[str, Any]]:
        return [r for m in self.history if m.get("role") == "tool" for r in m.get("results") or []]

    def last(self) -> str:
        res = self.results()
        return str(res[-1].get("content") or "") if res else ""

    def run_ids(self) -> List[str]:
        out: List[str] = []
        for r in self.results():
            for a, b in RUN_ID.findall(str(r.get("content") or "")):
                rid = a or b
                if rid and rid not in out:
                    out.append(rid)
        return out


def make_provider(base: Any, steps: Optional[List[Any]], answer: Any, env: Any) -> Any:
    """A Provider (subclass of the arm's own base class) that plays the steps."""
    from labharness.web.providers.base import ModelInfo, TurnResult

    class Oracle(base):   # type: ignore[misc, valid-type]
        name, label = "oracle", "Oracle (scripted)"

        def __init__(self) -> None:
            self.steps = list(steps or [])
            self.calls = 0

        def available(self) -> bool:
            return True

        async def models(self) -> List[Any]:
            return [ModelInfo("oracle", "Oracle (scripted)", self.name)]

        async def stream(self, model: str, system: str, history: List[Dict[str, Any]], tools: List[Dict[str, Any]],
                         emit: Any) -> Any:
            ctx = Ctx(history, env)
            step: Any
            if self.steps:
                step = self.steps.pop(0)
            else:
                step = answer(ctx) if callable(answer) else (answer or "Done.")
            while callable(step):
                step = step(ctx)
            if isinstance(step, str):
                await emit({"type": "text", "delta": step})
                return TurnResult([{"type": "text", "text": step}], stop_reason="end", model=model)
            calls = step if isinstance(step, list) else [step]
            blocks: List[Dict[str, Any]] = []
            for c in calls:
                self.calls += 1
                blocks.append({"type": "tool_call", "id": "oracle_%d" % self.calls, "name": c["tool"],
                               "input": c.get("input") or {}})
            return TurnResult(blocks, stop_reason="tool_calls", model=model)

    return Oracle()
