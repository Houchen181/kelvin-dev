"""Compatibility layer: the lab's team chat is ``peoplechat.py`` since v1-B (V1_SPEC.md F1).

Kept - rather than deleted - so that what imported the old module keeps working unchanged: ``web/app.py``'s
``TeamChat``, the ``team_chat`` tool (``web/agent.py``), and the tests. The chat itself - display names and
colors, reply quoting, Markdown text, ``@agent``, unread/last, retention and archives, and the migration of the
old ``team-chat.jsonl`` - is in :mod:`labharness.peoplechat`; this module only re-exports its names under the
old module's.

The old class kept the chat in ``~/Kelvin/team-chat.jsonl`` (one JSON message a line); the new one keeps it in
``~/Kelvin/team/chat.json`` (``KELVIN_TEAM_DIR`` overrides the folder) and copies the old file there on the
first start, renaming it ``.migrated`` - the old messages are still read, and ``read()`` says what it said.
"""
from .peoplechat import (  # noqa: F401 - the old module's public names, re-exported
    AGENT_COLOR,
    AGENT_NAME,
    MAX_AGENT_TEXT,
    MAX_NAME,
    MAX_TEXT,
    PAGE,
    PALETTE,
    READ_CHARS,
    READ_LAST,
    READ_MOST,
    TeamChat,
    TeamChatError,
    TooFast,
    clean_color,
    clean_cid,
    clean_name,
    clean_text,
    colors,
    mention_agent,
    mentions_of,
    palette,
    person_color,
    reserved_color,
    reserved_name,
)

__all__ = ["AGENT_COLOR", "AGENT_NAME", "MAX_AGENT_TEXT", "MAX_NAME", "MAX_TEXT", "PAGE", "PALETTE", "READ_CHARS",
           "READ_LAST", "READ_MOST", "TeamChat", "TeamChatError", "TooFast", "clean_color", "clean_cid",
           "clean_name", "clean_text", "colors", "mention_agent", "mentions_of", "palette", "person_color",
           "reserved_color", "reserved_name"]
