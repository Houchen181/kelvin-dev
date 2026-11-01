"""The chat stream a phone reads: one JSON event per line, ended by "\\n" alone. U+2028, U+2029 and U+0085 in an
event's text (from a paper, an instrument's reply read as Latin-1) go escaped, since some readers end a line at them
too: the iPhone app's did, and lost both halves of the event."""
import json

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")
pytest.importorskip("httpx")

from test_web import Scripted, client_for, parts  # noqa: E402,F401

SAID = "The abstract says: superconductivity at 4 K. The SMU replied 1.2\x85V"


def test_line_and_paragraph_separators_and_nel_never_end_a_line_of_the_chat_stream(parts):
    client, headers = client_for(parts, {"scripted": Scripted([[{"type": "text", "text": SAID}]])})
    with client:
        chat = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        with client.stream("POST", "/api/chats/%s/turn" % chat["id"], json={"text": "What does it say?",
                                                                             "model": "scripted:m1"},
                           headers=headers) as r:
            assert r.status_code == 200
            body = r.read().decode("utf-8")
    assert not any(c in body for c in "  \x85")
    events = [json.loads(line) for line in body.splitlines()]   # splitlines ends a line at those three too
    assert "".join(e["delta"] for e in events if e["type"] == "text") == SAID
    assert events[0]["type"] == "turn_start" and events[-1]["type"] == "turn_end"
