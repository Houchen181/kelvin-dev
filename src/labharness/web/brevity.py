"""Short answers, so a person can judge them at a glance (VISION item 9).

Kelvin's answers keep under a word budget: 200 words unless the user changes it (Settings → Chat). The model is asked
for it in the system prompt (RULE). A finished answer that still runs over is condensed by the same model (CONDENSE):
every number, unit, run ID, warning, decision and question stays; background and restatements go. The full answer is
kept beside it, one click away. The condensing leaves an answer whole when the user asked for the length (a full
derivation, every step, a whole document), and says why. Code blocks and plans never count toward the words.
"""
from __future__ import annotations

import re
from typing import Tuple

from . import review
from .providers.base import Provider

WORDS = 200          # the budget, unless the user changes it
TIMEOUT = 90         # seconds the condensing may take; after that the full answer stays

RULE = """# Short answers
Keep each answer under {words} words (code and plans don't count): the user must be able to judge it at a glance.
Go longer only when the user asks for detail or the content needs it, such as a derivation or document they asked
for. No preamble, no restating the question, no summary of what you are about to say, no closing offers."""

CONDENSE = """You shorten answers that Kelvin, an AI lab assistant, wrote for an experimental physicist who must judge \
them quickly. You get the physicist's message and Kelvin's answer.

If the physicist explicitly asked for a long or complete answer (a full derivation, every step, a whole document or \
list), reply with exactly: KEEP: <why, in a few words>

Otherwise rewrite the answer in at most {words} words:
- Lead with the result, or the decision the physicist has to make.
- Keep every number with its unit, run ID, file name, warning, risk, decision, question to the physicist, and link.
- Drop background, restatements, hedging and offers of further help.
- Keep the Markdown. Keep code blocks and plans exactly as they are; they don't count toward the words.
Reply with the rewritten answer only, in the answer's language."""

_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_LINK_TARGET = re.compile(r"\]\([^)]*\)")
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
_WORD = re.compile(r"[^\W_]+(?:['’.,\-/][^\W_]+)*")   # 1.52, don't, I-V and mK/s count once


def words(text: str) -> int:
    """The words a person reads: not code blocks or link targets. Two CJK characters count as one word."""
    prose = _LINK_TARGET.sub("]", _FENCE.sub(" ", text or ""))
    cjk = len(_CJK.findall(prose))
    return len(_WORD.findall(_CJK.sub(" ", prose))) + (cjk + 1) // 2


async def condense(provider: Provider, model_id: str, request: str, answer: str, limit: int) -> Tuple[str, str]:
    """Ask the model that wrote `answer` to shorten it. ("short", the shorter answer) or ("keep", why it stays long);
    raises when the model fails."""
    prompt = "The physicist's message:\n%s\n\nKelvin's answer (%d words):\n%s" % (
        (request or "").strip()[:4000], words(answer), answer)
    said = (await review.ask(provider, model_id, CONDENSE.format(words=limit), prompt)).strip()
    if re.match(r"(?i)\**keep\b", said):
        why = said.split(":", 1)[1].strip() if ":" in said else ""
        return "keep", why.strip("* ")[:200]
    return "short", said
