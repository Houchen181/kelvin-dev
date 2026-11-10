"""Which Kelvin this is.

- **The Kelvin app**, both the published app and the private build (private-v pre-releases, which differ only in where
  they update from: updater.channel): models come through the Kelvin hub with the person's Kelvin account
  (web/hubmodels.py), from a model server on their own computer, from their ChatGPT plan, or from the Claude Code on
  their computer (claudecode.py). It takes no API keys of its own.
- **Kelvin from source**, for its developers: also the person's own API keys and other model services (own_keys())."""
from __future__ import annotations

import sys


def own_keys() -> bool:
    """May people use API keys of their own (and other model services)? Only Kelvin from source: the app, private
    build included, takes its models through the Kelvin hub."""
    return not getattr(sys, "frozen", False)
