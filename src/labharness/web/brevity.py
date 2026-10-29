"""Short answers, so a person can judge them at a glance (VISION item 9).

Kelvin's answers keep under a word budget: 200 words unless the user changes it (Settings → Models). The model is
asked for it, in the system prompt (RULE) and again with each of the person's messages (REMINDER, added when the
request is built and never stored), so it writes a short answer itself; Kelvin doesn't rewrite what it wrote. Longer
only when the person asks for it (a derivation, every step, a whole document). Code blocks and plans never count.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

WORDS = 200          # the budget, unless the user changes it

RULE = """# Short answers
Keep each answer under {words} words (code and plans don't count): the user must be able to judge it at a glance.
Go longer only when the user asks for detail or the content needs it, such as a derivation or document they asked
for. No preamble, no restating the question, no summary of what you are about to say, no closing offers."""

REMINDER = ("(Answer in at most {words} words; code and plans don't count. Lead with the result or the decision "
            "needed. Longer only if this message asks for it.)")

_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_LINK_TARGET = re.compile(r"\]\([^)]*\)")
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
_WORD = re.compile(r"[^\W_]+(?:['’.,\-/][^\W_]+)*")   # 1.52, don't, I-V and mK/s count once


def words(text: str) -> int:
    """The words a person reads: not code blocks or link targets. Two CJK characters count as one word."""
    prose = _LINK_TARGET.sub("]", _FENCE.sub(" ", text or ""))
    cjk = len(_CJK.findall(prose))
    return len(_WORD.findall(_CJK.sub(" ", prose))) + (cjk + 1) // 2


def with_reminder(history: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """The history as the model gets it: each of the person's messages comes with the word budget ("brief", which
    user_text adds), so the model keeps its answers short itself. On every message, not only the latest, so earlier
    turns replay unchanged and the prompt cache holds. The stored chat never has it. No budget, no change."""
    if not limit:
        return history
    brief = REMINDER.format(words=limit)
    return [dict(m, brief=brief) if m["role"] == "user" else m for m in history]
