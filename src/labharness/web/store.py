"""Conversations, one JSON file each. History is append-only: earlier turns are never edited."""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List

_ID = re.compile(r"^[0-9a-f]{12}$")


class ChatStore:
    def __init__(self, root: Any):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, chat_id: str) -> Path:
        if not _ID.match(chat_id or ""):
            raise KeyError("no chat %r" % chat_id)
        return self.root / ("%s.json" % chat_id)

    def create(self, model: str) -> Dict[str, Any]:
        now = time.time()
        chat = {"id": secrets.token_hex(6), "title": "New chat", "model": model,
                "created": now, "updated": now, "messages": []}
        self.save(chat)
        return chat

    def load(self, chat_id: str) -> Dict[str, Any]:
        path = self._path(chat_id)
        if not path.exists():
            raise KeyError("no chat %r" % chat_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, chat: Dict[str, Any]) -> None:
        chat["updated"] = time.time()
        path = self._path(chat["id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(chat, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def delete(self, chat_id: str) -> None:
        path = self._path(chat_id)
        if path.exists():
            path.unlink()

    def list(self) -> List[Dict[str, Any]]:
        out = []
        for path in self.root.glob("*.json"):
            try:
                chat = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"id": chat["id"], "title": chat.get("title", "Chat"), "updated": chat.get("updated", 0),
                        "model": chat.get("model")})
        return sorted(out, key=lambda c: c["updated"], reverse=True)
