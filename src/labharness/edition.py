"""Which Kelvin this is.

- **The Kelvin app**, both the user version other people install and the private build (private-v releases): models
  come through the Kelvin hub with the person's Kelvin account (web/hubmodels.py), from a model server on their own
  computer, or from their ChatGPT plan. It takes no API keys of its own.
- **The private build** adds the Claude Code bridge (private()).
- **Kelvin from source**, for its developers: also the person's own API keys and other model services (own_keys())."""
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


def own_keys() -> bool:
    """May people use API keys of their own (and other model services)? Only Kelvin from source: the app, private
    build included, takes its models through the Kelvin hub."""
    return not getattr(sys, "frozen", False)
