"""@agent in the team chat: Kelvin's main AI answers there (V1_SPEC.md F2, v1-B).

A person writes "@agent ..." (or "@kelvin ...") in the team chat; the portal wakes this, and Kelvin answers in
the same chat under the reserved name "Kelvin" (via "agent"), replying to the message that asked. The answer is
one ordinary chat of Kelvin's own - kept by web/store.py with the title "Team chat", so the AI has what it was
asked before and the person can open it under Chat - and the turn is run exactly as the portal's own turn route
runs one (web/app.py): the same model claim, the same tools, the same safety gates and approvals. The AI's
writes to the lab still take a person's approval as before; this module changes no gate.

Rules:
- one answer at a time (a single flight): while Kelvin is answering, another @agent gets a readable notice in
  the chat instead of a second answer;
- no model configured, no key, a model the chat's rules forbid, or an error: one "Kelvin could not answer: ..."
  notice in the chat - never an exception to the portal;
- nothing here is a safety gate or an approval: they stay where they were (agent/approval.py, gate.py).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger("kelvin")

TITLE = "Team chat"          # the title of the conversation the AI answers @agent in
AGENT_NAME = "Kelvin"        # the reserved name its answers are written under (peoplechat.py)
WAKE = "Team chat: @agent"   # the comment on the conversation: what woke it (for whoever opens it under Chat)

NO_MODEL = ("Kelvin could not answer: no model is configured for it. Add an API key under Settings → Models, "
            "then write @agent again.")
BUSY = ("Kelvin is still answering an earlier @agent message, so it did not take this one. Wait for that answer, "
        "then write @agent again.")
FAILED = "Kelvin could not answer: %s"


def _why(e: BaseException) -> str:
    """An error in a few words: its first line (as web/agent.py's _why)."""
    text = str(e).strip()
    return text.splitlines()[0] if text else type(e).__name__


class TeamAgent:
    """@agent → an answer of Kelvin's in the team chat (V1_SPEC.md F2). Never raises at the portal."""

    def __init__(self, agent: Any, store: Any, chat: Any, settings: Any):
        self.agent = agent       # web/agent.py's ChatAgent: models(), claim(), run_turn()
        self.store = store       # web/store.py's ChatStore: the dedicated conversation
        self.chat = chat         # peoplechat.py's TeamChat: where @agent is written and the answer goes
        self.settings = settings
        self.task: Optional[asyncio.Task] = None   # the answer in flight (one at a time)
        self.chat_id = ""        # the dedicated conversation's id, once it exists

    # ------------------------------------------------------------------ waking (the portal's POST /api/team)

    def maybe_wake(self, message: Any) -> Optional[asyncio.Task]:
        """Wake Kelvin for an @agent message; None when it was not for Kelvin or Kelvin is already answering.

        Called from the portal's POST /api/team, on the server's event loop: the answer runs in a task of its
        own (so the person's message is answered at once) and never raises to the request."""
        if not isinstance(message, dict) or not message.get("mentions_agent"):
            return None
        if self.task is not None and not self.task.done():
            self._say(BUSY, reply_to=message.get("id"))
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:   # no portal loop (a script, a test): nothing to wake on
            return None
        self.task = loop.create_task(self.answer(message))
        return self.task

    async def answer(self, message: Dict[str, Any]) -> None:
        """Claim the dedicated conversation, run the turn, and write the answer to the team chat."""
        asked = message.get("id") if isinstance(message, dict) else None
        notes: List[str] = []
        try:
            model = await self._model()
            if not model:
                self._say(NO_MODEL, reply_to=asked)
                return
            chat_id = self._conversation(model)
            provider = self.agent.claim(chat_id, model)
            parts: List[str] = []

            async def emit(event: Dict[str, Any]) -> None:
                if event.get("type") == "text":
                    parts.append(str(event.get("delta") or ""))
                elif event.get("type") == "error":
                    notes.append(str(event.get("message") or ""))

            await self.agent.run_turn(chat_id, str(message.get("text") or ""), model, provider, emit)
            answer = "".join(parts).strip() or self._last_answer(chat_id)
            if not answer:
                self._say(FAILED % (notes[-1] if notes else "it wrote no answer"), reply_to=asked)
                return
            self.chat.post(AGENT_NAME, answer, via="agent", reply_to=asked)
        except asyncio.CancelledError:   # Kelvin closing: the turn is stopped, not "answered"
            raise
        except Exception as e:           # never an exception to the portal: said in the chat, in words
            log.warning("team chat: @agent could not answer: %s", _why(e))
            self._say(FAILED % _why(e), reply_to=asked)

    # ------------------------------------------------------------------ the conversation and the model

    async def _model(self) -> str:
        """The model the answer runs on: the person's default one that a provider offers now ("" none)."""
        try:
            models = await self.agent.models()
        except Exception as e:   # a provider that fails to list is no model, not an exception at the portal
            log.info("team chat: models could not be listed: %s", _why(e))
            return ""
        keys = [str(m["key"]) for group in models.get("groups") or [] for m in group.get("models") or []
                if isinstance(m, dict) and m.get("key")]
        if not keys:
            return ""
        try:
            from .settings import default_model
            wanted = str(self.settings.get("default_model") or "") if self.settings is not None else ""
            return default_model(keys, wanted) or keys[0]
        except Exception:
            return keys[0]

    def _conversation(self, model: str) -> str:
        """The dedicated conversation titled "Team chat": found, or made once (it is Kelvin's own, kept as an
        ordinary chat, so the person can open it under Chat and the AI reads what it was asked before)."""
        store = self.store
        if self.chat_id:
            try:
                store.load(self.chat_id)
                return self.chat_id
            except KeyError:
                self.chat_id = ""
        try:
            for chat in store.list():
                if chat.get("title") == TITLE:
                    self.chat_id = str(chat["id"])
                    return self.chat_id
        except Exception as e:
            log.info("team chat: could not look for the %r conversation: %s", TITLE, _why(e))
        chat = store.create(model)
        chat["title"] = TITLE
        chat["wake"] = WAKE
        store.save(chat)
        self.chat_id = str(chat["id"])
        return self.chat_id

    def _last_answer(self, chat_id: str) -> str:
        """The turn's answer when its text events said nothing: the chat's last assistant message."""
        try:
            chat = self.store.load(chat_id)
        except (KeyError, OSError, ValueError):
            return ""
        for m in reversed(chat.get("messages") or []):
            if m.get("role") == "assistant":
                text = "".join(str(b.get("text") or "") for b in m.get("blocks") or []
                               if isinstance(b, dict) and b.get("type") == "text").strip()
                if text:
                    return text
        return ""

    # ------------------------------------------------------------------ notices in the chat

    def _say(self, text: str, reply_to: Any = None) -> None:
        """A system notice from Kelvin in the team chat ("could not answer", "still answering"); never raises."""
        try:
            self.chat.post(AGENT_NAME, text, via="system", reply_to=reply_to)
        except Exception as e:   # a chat that cannot be written is logged, never raised at the portal
            log.warning("team chat: could not write %r: %s", str(text)[:120], _why(e))
