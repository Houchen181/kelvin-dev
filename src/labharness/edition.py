"""Which Kelvin this is.

- **The user version** (the app other people install): models come through the Kelvin hub with the person's Kelvin
  account (web/hubmodels.py), from a model server on their own computer, or from their ChatGPT plan. It takes no API
  keys of its own.
- **The private build** (private-v releases) and Kelvin from source: the same, plus the person's own API keys, other
  model services and the Claude Code bridge."""
from __future__ import annotations

import sys


def private() -> bool:
    """The private build, or Kelvin from source; False for the user version."""
    if not getattr(sys, "frozen", False):
        return True
    try:
        from . import _private_bridge  # noqa: F401  (written only for the private build)
        return True
    except ImportError:
        return False
