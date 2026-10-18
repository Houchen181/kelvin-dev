"""A scripted stand-in for a model, so the portal can be tried without an API key.

It is not AI: it matches a few keywords and calls the real lab tools.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List

from .base import Emit, ModelInfo, Provider, TurnResult

GATE_SWEEP = """plan: gate_sweep
setup: { lockin.amplitude: 0.05, lockin.tc: 0.1, smu.output: on }
sweep: { smu.V: {from: 0, to: 5, step: 0.1} }
settle: 0.5
measure: [lockin.X, smu.I]
after: { smu.V: 0 }"""

FIELD_MAP = """plan: field_gate_map
setup: { lockin.amplitude: 0.05, lockin.tc: 0.1, smu.output: on, magnet.rate: 0.5 }
sweep:
  magnet.B: {values: [0, 0.5, 1]}
  smu.V: {from: 0, to: 4, step: 0.2}
settle: 0.5
measure: [lockin.X]
after: { smu.V: 0, magnet.B: 0 }"""


class DemoProvider(Provider):
    name = "demo"
    label = "Demo"

    def available(self) -> bool:
        return True

    async def models(self) -> List[ModelInfo]:
        return [ModelInfo("demo", "Demo (scripted, no AI)", self.name)]

    async def _say(self, text: str, emit: Emit) -> List[Dict[str, Any]]:
        for word in text.split(" "):
            await emit({"type": "text", "delta": word + " "})
            await asyncio.sleep(0.01)
        return [{"type": "text", "text": text}]

    async def stream(self, model: str, system: str, history: List[Dict[str, Any]],
                     tools: List[Dict[str, Any]], emit: Emit) -> TurnResult:
        last = history[-1]
        if last["role"] == "tool":  # summarize what the tool returned
            result = last["results"][0]
            lines = [l for l in (result["content"] or "").splitlines() if l.strip()]
            run = next((l for l in lines if l.startswith("run ")), None)
            if result["is_error"]:
                text = "That failed: %s" % (lines[0] if lines else "no details")
            elif run:  # the conversation already shows the assessment, and the Runs pane the data
                text = "**%s.** The assessment is above; the plots and data are in the Runs pane." % (run[:1].upper() + run[1:])
            elif result["name"] == "remember":
                text = ("Noted. Every conversation will know it; see Settings → Memory to review or delete it."
                        if "every conversation" in result["content"] else
                        "Noted. Every conversation in this project will know it; open Project settings to review or "
                        "delete it.")
            elif result["name"] == "lab_status":
                text = "Here is the lab right now (expand the step for every value). Drives marked * are away from safe."
            else:
                text = lines[0] if lines else "Done."
            return TurnResult(await self._say(text, emit), stop_reason="end", model=model)
        ask = last["text"].lower()
        if ask.startswith(("remember", "note that")):
            tool = next((t for t in tools if t["name"] == "remember"), None)
            if tool is None:
                text = ("This is an incognito chat, so nothing is remembered." if "Incognito chat" in system else
                        "Learning is switched off in Settings → Memory, so I won't keep it.")
                return TurnResult(await self._say(text, emit), stop_reason="end", model=model)
            fact = re.sub(r"^\s*(remember|note)( that)?[:,]?\s*", "", last["text"], flags=re.I).strip()
            personal = any(w in ask for w in ("i prefer", "i like", "always ", "never ", " my ", "call me"))
            if personal:
                args = {"scope": "global", "kind": "preference"}
            elif "project" in tool["schema"]["properties"]["scope"]["enum"]:
                args = {"scope": "project",
                        "kind": "quirk" if any(w in ask for w in ("leak", "drift", "noisy", "quirk", "unstable")) else "decision"}
            else:
                text = "Experiment facts are kept per project. File this conversation under a project, then ask again."
                return TurnResult(await self._say(text, emit), stop_reason="end", model=model)
            args["text"] = fact[:1].upper() + fact[1:]
            name, intro = "remember", "Keeping that for later."
        elif "map" in ask:
            name, args = "run_plan", {"plan": FIELD_MAP}
            intro = "I'll map the lock-in signal against gate voltage at 0, 0.5 and 1 T (nested loops: field outside, gate inside)."
        elif "sweep" in ask:
            name, args = "run_plan", {"plan": GATE_SWEEP}
            intro = "I'll sweep the gate from 0 to 5 V and record the lock-in signal."
        elif any(w in ask for w in ("status", "check", "what is on", "what's on", "state")):
            name, args, intro = "lab_status", {}, "Checking the lab."
        else:
            text = ("I'm the scripted demo, not an AI. Try *check the lab status*, *sweep the gate*, "
                    "*map gate against field*, or *remember that …*. Add an API key in Settings to talk to Claude, GPT or Gemini.")
            return TurnResult(await self._say(text, emit), stop_reason="end", model=model)
        blocks = await self._say(intro, emit)
        blocks.append({"type": "tool_call", "id": "lh_call_%d" % len(history), "name": name, "input": args})
        return TurnResult(blocks, stop_reason="tool_calls", model=model)
