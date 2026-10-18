"""A second opinion: another model reviews a plan before it runs, or an answer after it is given.

The reviewer sees text only (no tools), so it can never touch the lab. Its verdict on a plan is advice: a "stop"
goes to the user, who decides; the safety gate's own checks and approvals apply either way."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

from .providers.base import Provider, text_of

PLAN_SYSTEM = """You review a measurement plan before it runs on real lab instruments, as a careful senior \
experimental physicist would. You see the user's request, the lab's current state, Kelvin's own checks of the plan, \
and the plan.
Look for: risk to the sample or the instruments (levels, ramp rates, compliance, temperature, field, laser power, \
order of steps); whether the plan answers the request; settling and averaging; range and resolution; controls; \
wasted time.
Answer in at most 6 short lines. The first line is exactly one of:
verdict: go
verdict: caution
verdict: stop
Use stop only for a real risk of damage, or a plan that cannot answer the request. Then give the reasons as "- " \
bullets, most important first. No preamble."""

ANSWER_SYSTEM = """You give a second opinion on an answer that Kelvin, an AI lab assistant, gave an experimental \
physicist. You see the user's last request, what Kelvin did (tool calls and shortened results) and what it said.
Check whether the conclusions follow from the data, the numbers and units are right, the uncertainties are honest, \
anything was overlooked (other explanations, systematic errors, missing controls), and whether the suggested next \
step is sound.
Answer in at most 8 short lines. The first line is exactly one of:
verdict: agree
verdict: partly
verdict: disagree
Then "- " bullets, most important first. Be specific; no preamble, no praise."""

VERDICTS = {"plan": ("go", "caution", "stop"), "answer": ("agree", "partly", "disagree")}
_VERDICT = re.compile(r"^\W*verdict\W*:?\W*([a-z]+)", re.I)


def parse(text: str, kind: str) -> Tuple[str, str]:
    """(verdict, reasons). A reply without a recognisable verdict counts as the middle one."""
    verdict, body = "", []
    for line in (text or "").strip().splitlines():
        m = _VERDICT.match(line)
        if m and not verdict:
            verdict = m.group(1).lower()
            continue
        body.append(line.rstrip())
    if verdict not in VERDICTS[kind]:
        verdict = VERDICTS[kind][1]
    return verdict, "\n".join(body).strip()[:2000]


def clip(text: str, n: int) -> str:
    text = str(text)
    return text if len(text) <= n else text[:n] + " …(%d more characters)" % (len(text) - n)


def last_request(messages: List[Dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("text", ""))
    return ""


def answer_context(messages: List[Dict[str, Any]]) -> str:
    """The last exchange as plain text: the request, Kelvin's tool calls with shortened results, what it said."""
    start = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    if start < 0:
        raise ValueError("Nothing to review yet.")
    parts = ["The user asked:\n" + str(messages[start].get("text", ""))]
    said = False
    for m in messages[start + 1:]:
        if m.get("role") == "assistant":
            for b in m.get("blocks") or []:
                if b.get("type") == "tool_call":
                    parts.append("Kelvin called %s(%s)" % (b.get("name"), clip(json.dumps(b.get("input"), ensure_ascii=False), 800)))
                elif b.get("type") == "text" and b.get("text", "").strip():
                    parts.append("Kelvin said:\n" + clip(b["text"], 6000))
                    said = True
        elif m.get("role") == "tool":
            for r in m.get("results") or []:
                parts.append("Result of %s:\n%s" % (r.get("name"), clip(r.get("content", ""), 2500)))
    if not said:
        raise ValueError("Kelvin has not answered that message yet.")
    return "\n\n".join(parts)


def plan_prompt(request: str, status: str, checks: str, plan: str) -> str:
    return ("The user asked:\n%s\n\nThe lab now:\n%s\n\nKelvin's checks of the plan (validation and approvals "
            "it will need):\n%s\n\nThe plan:\n%s" % (clip(request, 3000), clip(status, 4000), clip(checks, 3000),
                                                    clip(plan, 6000)))


async def ask(provider: Provider, model_id: str, system: str, prompt: str) -> str:
    async def quiet(event: Dict[str, Any]) -> None:
        return None
    result = await provider.stream(model_id, system, [{"role": "user", "text": prompt}], [], quiet)
    return text_of(result.blocks)


def as_text(review: Dict[str, Any], label: str) -> str:
    """How the main model sees a review."""
    return "second opinion from %s: verdict %s\n%s" % (label, review["verdict"], review["text"])
