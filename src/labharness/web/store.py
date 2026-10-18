"""Conversations, one JSON file each, and the projects they are filed under.

Chat history is append-only: earlier turns are never edited. Project membership lives in a separate index
(_projects.json), so filing a chat never rewrites the chat file that a running turn may be saving.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_ID = re.compile(r"^[0-9a-f]{12}$")
_PROJECT_ID = re.compile(r"^p[0-9a-f]{8}$")
MAX_NAME = 60


def _name(name: Any) -> str:
    name = " ".join(str(name or "").split())[:MAX_NAME]
    if not name:
        raise ValueError("A project needs a name.")
    return name


class ChatStore:
    def __init__(self, root: Any):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "_projects.json"
        self._lock = threading.Lock()

    def _path(self, chat_id: str) -> Path:
        if not _ID.match(chat_id or ""):
            raise KeyError("no chat %r" % chat_id)
        return self.root / ("%s.json" % chat_id)

    # ------------------------------------------------------------------ chats

    def create(self, model: str, project: Optional[str] = None) -> Dict[str, Any]:
        if project:
            self._find(self._read_index(), project)  # an unknown project creates nothing
        now = time.time()
        chat = {"id": secrets.token_hex(6), "title": "New chat", "model": model,
                "created": now, "updated": now, "messages": []}
        self.save(chat)
        if project:
            self.assign(chat["id"], project)
        chat["project"] = project or None
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
        tmp.write_text(json.dumps({k: v for k, v in chat.items() if k != "project"}, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, path)

    def delete(self, chat_id: str) -> None:
        path = self._path(chat_id)
        if path.exists():
            path.unlink()
        with self._lock:
            index = self._read_index()
            if index["assign"].pop(chat_id, None) is not None:
                self._write_index(index)

    def list(self) -> List[Dict[str, Any]]:
        assign = self._read_index()["assign"]
        out = []
        for path in self.root.glob("*.json"):
            if not _ID.match(path.stem):  # the projects index, or a stray file
                continue
            try:
                chat = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"id": chat["id"], "title": chat.get("title", "Chat"), "updated": chat.get("updated", 0),
                        "model": chat.get("model"), "project": assign.get(chat["id"])})
        return sorted(out, key=lambda c: c["updated"], reverse=True)

    def project_of(self, chat_id: str) -> Optional[str]:
        return self._read_index()["assign"].get(chat_id)

    # ------------------------------------------------------------------ projects

    def _read_index(self) -> Dict[str, Any]:
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        projects = [p for p in data.get("projects") or [] if _PROJECT_ID.match(str(p.get("id", "")))]
        known = {p["id"] for p in projects}
        assign = {c: p for c, p in (data.get("assign") or {}).items() if p in known}
        return {"projects": projects, "assign": assign}

    def _write_index(self, index: Dict[str, Any]) -> None:
        tmp = self._index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._index_path)

    def projects(self) -> List[Dict[str, Any]]:
        """In the order they were made."""
        return self._read_index()["projects"]

    def create_project(self, name: Any) -> Dict[str, Any]:
        project = {"id": "p" + secrets.token_hex(4), "name": _name(name), "created": time.time()}
        with self._lock:
            index = self._read_index()
            index["projects"].append(project)
            self._write_index(index)
        return project

    def rename_project(self, project_id: str, name: Any) -> Dict[str, Any]:
        with self._lock:
            index = self._read_index()
            project = self._find(index, project_id)
            project["name"] = _name(name)
            self._write_index(index)
        return project

    def delete_project(self, project_id: str) -> None:
        """The project goes; its conversations stay, unfiled."""
        with self._lock:
            index = self._read_index()
            self._find(index, project_id)
            index["projects"] = [p for p in index["projects"] if p["id"] != project_id]
            index["assign"] = {c: p for c, p in index["assign"].items() if p != project_id}
            self._write_index(index)

    def assign(self, chat_id: str, project_id: Optional[str]) -> None:
        """File a chat under a project, or take it out of its project (None)."""
        if not self._path(chat_id).exists():
            raise KeyError("no chat %r" % chat_id)
        with self._lock:
            index = self._read_index()
            if project_id:
                self._find(index, project_id)
                index["assign"][chat_id] = project_id
            else:
                index["assign"].pop(chat_id, None)
            self._write_index(index)

    @staticmethod
    def _find(index: Dict[str, Any], project_id: str) -> Dict[str, Any]:
        for p in index["projects"]:
            if p["id"] == project_id:
                return p
        raise KeyError("no project %r" % project_id)
