"""Kelvin's web_search tool: searching the web through the search a model service already offers, so the person needs
no other account. Claude's web search (the Claude API), OpenAI's (the Responses API), Gemini's Google Search, or
Claude Code's WebSearch on the person's own plan (the temporary bridge). The chat's own service is asked first, then
the others that are set up, in that order.

Each answers a short request ("search the web for ..., list what is worth opening") with titles and web addresses,
which Kelvin passes to the chat model; read_web_page and save_manual (agent/runtime.py) take it from there. The query
leaves the computer, so project rules that keep work off the internet switch this off with literature search."""
from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional, Tuple

ORDER = ("anthropic", "openai", "gemini", "claudecode")   # services with a web search, the chat's own first
PROMPT = ("Search the web for: {query}\n\nThen answer with the results worth opening, best first, at most {limit}, "
          "one per line exactly as\n- <title> | <web address> | <what is there, in a few words>\nUse only addresses "
          "the search found. No other text.")
_URL = re.compile(r"https?://[^\s|<>\"'`\])]+")


def results_in(text: str) -> List[Dict[str, str]]:
    """Results from a model's answer: one per line with a web address, the title before it, a note after."""
    out: List[Dict[str, str]] = []
    for line in (text or "").splitlines():
        m = _URL.search(line)
        if not m:
            continue
        url = m.group(0).rstrip(".,;:")
        if any(r["url"] == url for r in out):
            continue
        title = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line[:m.start()])
        title = re.sub(r"\[([^\]]*)\]\($", r"\1", title.strip()).strip(" |-—:*[]()")
        note = line[m.end():].strip(" |-—:)*")
        out.append({"title": title, "url": url, "snippet": note})
    return out


def merge(first: List[Dict[str, str]], more: List[Dict[str, str]], limit: int) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for r in list(first) + list(more):
        if r.get("url") and not any(o["url"] == r["url"] for o in out):
            out.append(r)
    return out[:limit]


async def _model_for(provider: Any, chat_provider: str, chat_model: str) -> str:
    if provider.name == chat_provider and chat_model:
        return chat_model
    if provider.name == "claudecode":
        return "sonnet"
    models = await provider.models()
    return models[0].id if models else ""


async def search(providers: Dict[str, Any], active_model: Optional[str], query: str,
                 limit: int = 8) -> Tuple[List[Dict[str, str]], str, List[str]]:
    """(results, the service that found them, what went wrong with the others)."""
    chat_provider, _, chat_model = (active_model or "").partition(":")
    problems: List[str] = []
    for name in [chat_provider] + [n for n in ORDER if n != chat_provider]:
        provider = providers.get(name)
        if provider is None or not provider.available() or not getattr(provider, "can_search", False):
            continue
        try:
            model = await _model_for(provider, chat_provider, chat_model)
            found = await asyncio.wait_for(provider.search(model, query, limit), 300)
        except Exception as e:   # not enabled for this account, no such model, the network...
            text = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            problems.append("%s: %s" % (provider.label, text[:200]))
            continue
        if found:
            return found[:limit], provider.label, problems
        problems.append("%s found nothing" % provider.label)
    return [], "", problems


def describe(found: List[Dict[str, str]], by: str, problems: List[str]) -> str:
    """The tool's answer, for the chat model."""
    if not found:
        why = "; ".join(problems) if problems else "no model service that can search the web is set up"
        return ("no web results (%s). Web search goes through a Claude, OpenAI or Gemini API key, or Claude Code, in "
                "Settings → Models." % why)
    lines = ["%d. %s — %s%s" % (i + 1, r.get("title") or "(untitled)", r["url"],
                                "\n   " + r["snippet"] if r.get("snippet") else "") for i, r in enumerate(found)]
    lines.append("(searched with %s) read_web_page reads one; save_manual keeps an instrument's manual." % by)
    return "\n".join(lines)
