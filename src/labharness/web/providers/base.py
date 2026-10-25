"""What every model provider implements.

History is provider-neutral and append-only:
    {"role": "user", "text": "...", "memory": [notes recalled for this message, optional]}
    {"role": "assistant", "provider": "anthropic", "model": "...", "blocks": [...], "raw": ...}
    {"role": "tool", "results": [{"id", "name", "content", "is_error"}]}
Assistant blocks: {"type": "text", "text"}, {"type": "thinking", "text"},
{"type": "tool_call", "id", "name", "input"} (input None = arguments were not valid JSON).
`raw` is the provider's own form of the turn, replayed unchanged to the same
provider so reasoning state (thinking blocks, thought signatures) survives.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

Emit = Callable[[Dict[str, Any]], Awaitable[None]]


@dataclass
class ModelInfo:
    id: str
    label: str
    provider: str


@dataclass
class TurnResult:
    blocks: List[Dict[str, Any]]
    raw: Any = None
    stop_reason: str = "end"        # end | tool_calls | pause | max_tokens | refusal
    model: Optional[str] = None     # the model that actually answered (fallbacks can change it)
    note: str = ""

    @property
    def tool_calls(self) -> List[Dict[str, Any]]:
        return [b for b in self.blocks if b["type"] == "tool_call"]


class Provider:
    name = "base"
    label = "Base"
    can_search = False   # has a web search of its own (web/websearch.py)

    def available(self) -> bool:
        raise NotImplementedError

    async def models(self) -> List[ModelInfo]:
        raise NotImplementedError

    async def stream(self, model: str, system: str, history: List[Dict[str, Any]],
                     tools: List[Dict[str, Any]], emit: Emit) -> TurnResult:
        raise NotImplementedError

    async def search(self, model: str, query: str, limit: int = 8) -> List[Dict[str, str]]:
        """Web results ({title, url, snippet}) through this service's own web search."""
        raise NotImplementedError


def safe_id(value: Any, n: int) -> str:
    """A tool-call id every provider accepts."""
    text = re.sub(r"[^A-Za-z0-9_-]", "_", str(value or ""))
    return text or "lh_call_%d" % n


def user_text(message: Dict[str, Any]) -> str:
    """A user message as the model sees it: notes recalled for it (if any) in front of what the user wrote.
    Stored with the message, so earlier turns replay unchanged and the prompt cache holds."""
    from ..memory import block
    notes = message.get("memory")
    text = block(notes, message.get("memory_project")) + "\n\n" + message["text"] if notes else message["text"]
    review = message.get("review")
    if review:   # a second opinion on the previous answer, asked for by the user
        text = '<second_opinion model="%s" verdict="%s">\n%s\n</second_opinion>\n\n%s' % (
            review.get("label") or review.get("model"), review.get("verdict"), review.get("text", ""), text)
    return text


def text_of(blocks: List[Dict[str, Any]]) -> str:
    return "".join(b["text"] for b in blocks if b["type"] == "text")
