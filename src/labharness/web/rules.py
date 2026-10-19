"""Who may see a project's work: which model providers, and whether search words may leave the computer.

A project's rules apply to its chats. A chat that ran under rules keeps them (they are stamped on the chat), so
moving it out of the project or deleting the project never lets another provider read what it said. Rules only
ever combine to the stricter of the two."""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional

PROVIDERS = {"anthropic": "Claude", "openai": "GPT", "gemini": "Gemini", "local": "local models"}
ALWAYS = {"demo"}   # the scripted demo model: nothing leaves the computer
OPEN = {"providers": None, "literature": True}


def normalize(rules: Any) -> Dict[str, Any]:
    if not isinstance(rules, dict):
        return dict(OPEN)
    providers = rules.get("providers")
    if providers is not None:
        if not isinstance(providers, (list, tuple)):
            raise ValueError("providers must be a list, or null for any")
        unknown = [p for p in providers if not isinstance(p, str) or not re.match(r"^[a-z][a-z0-9-]{0,30}$", p)]
        if unknown:
            raise ValueError("unknown model provider %s" % ", ".join(map(str, unknown)))
        # a list is never widened to "any": with other services set up, the four built-in kinds are not all of them
        providers = [p for p in PROVIDERS if p in providers] + sorted(set(providers) - set(PROVIDERS))
    return {"providers": providers, "literature": rules.get("literature", True) is not False}


def merge(*sets: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The stricter of several rule sets."""
    providers: Optional[list] = None
    literature = True
    for r in sets:
        if not r:
            continue
        r = normalize(r)
        if r["providers"] is not None:
            providers = r["providers"] if providers is None else [p for p in providers if p in r["providers"]]
        literature = literature and r["literature"]
    return {"providers": providers, "literature": literature}


def restrictive(rules: Optional[Dict[str, Any]]) -> bool:
    r = normalize(rules)
    return r["providers"] is not None or not r["literature"]


def allows(rules: Optional[Dict[str, Any]], model_key: str) -> bool:
    provider = str(model_key).partition(":")[0]
    r = normalize(rules)
    return provider in ALWAYS or r["providers"] is None or provider in r["providers"]


def describe(rules: Optional[Dict[str, Any]]) -> str:
    r = normalize(rules)
    parts = []
    if r["providers"] is not None:
        names = [PROVIDERS.get(p, p) for p in r["providers"]]
        parts.append("only %s may see it" % (" and ".join(names) if names else "no model"))
    if not r["literature"]:
        parts.append("no literature search")
    return "; ".join(parts) or "any model"


def names(providers: Iterable[str]) -> str:
    return " and ".join(PROVIDERS.get(p, p) for p in providers)
