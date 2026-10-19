"""Ownership of persisted experiment data exposed to portal models.

The local owner retains access. Models get a bound scope: their project, their
one unfiled chat, or the standalone MCP entry point's separate workspace.
Scope is attached before work starts and survives queues, threads and restarts.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, Optional

Scope = Dict[str, Optional[str]]
_current: ContextVar[Optional[Scope]] = ContextVar("kelvin_data_scope", default=None)


def current() -> Optional[Scope]:
    scope = _current.get()
    return dict(scope) if scope is not None else None


@contextmanager
def use(scope: Optional[Scope]) -> Iterator[None]:
    token = _current.set(dict(scope) if scope is not None else None)
    try:
        yield
    finally:
        _current.reset(token)


def visible(owner: Optional[Scope]) -> bool:
    caller = _current.get()
    if caller is None:  # trusted local owner / low-level embedding code
        return True
    if not isinstance(owner, dict) or not owner:
        return False  # legacy/unowned data is never silently shared with a model
    if caller.get("project"):
        return owner.get("project") == caller["project"]
    return not owner.get("project") and owner.get("chat") == caller.get("chat")


class ScopedRuntime:
    """Bind at the MCP server, not the client task: worker threads must receive the scope too."""

    def __init__(self, runtime: Any, scope: Scope):
        self._runtime = runtime
        self._scope = dict(scope)

    def __getattr__(self, name: str) -> Any:
        member = getattr(self._runtime, name)
        if not callable(member):
            return member

        def call(*args: Any, **kwargs: Any) -> Any:
            with use(self._scope):
                return member(*args, **kwargs)
        return call
