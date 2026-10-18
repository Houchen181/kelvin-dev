"""Conversations, one JSON file each, and the projects they are filed under.

Chat history is append-only: earlier turns are never edited. Project membership lives in a separate index
(_projects.json), so filing a chat never rewrites the chat file that a running turn may be saving.

Each project also keeps its memory in that index: the user's instructions and a short list of notes (durable
facts). A chat sees only the memory of the project it is filed under, and deleting a project deletes its memory.
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
MAX_INSTRUCTIONS = 4000  # characters, for a project's instructions and for the global ones
MAX_NOTE = 300
MAX_NOTES = 50
NOTE_KINDS = ("decision", "procedure", "quirk", "result")


def _name(name: Any) -> str:
    name = " ".join(str(name or "").split())[:MAX_NAME]
    if not name:
        raise ValueError("A project needs a name.")
    return name


def instructions_text(text: Any) -> str:
    return str(text or "").strip()[:MAX_INSTRUCTIONS]


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

    def memory_of(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """The memory of the project this chat is filed under; None for an unfiled chat."""
        index = self._read_index()
        project_id = index["assign"].get(chat_id)
        return self._find(index, project_id) if project_id else None

    # ------------------------------------------------------------------ projects

    def _read_index(self) -> Dict[str, Any]:
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        projects = [p for p in data.get("projects") or [] if _PROJECT_ID.match(str(p.get("id", "")))]
        for p in projects:
            p.setdefault("instructions", "")
            p.setdefault("notes", [])
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

    def project(self, project_id: str) -> Dict[str, Any]:
        return self._find(self._read_index(), project_id)

    def create_project(self, name: Any) -> Dict[str, Any]:
        project = {"id": "p" + secrets.token_hex(4), "name": _name(name), "created": time.time(),
                   "instructions": "", "notes": []}
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

    def set_instructions(self, project_id: str, text: Any) -> Dict[str, Any]:
        with self._lock:
            index = self._read_index()
            project = self._find(index, project_id)
            project["instructions"] = instructions_text(text)
            self._write_index(index)
        return project

    def add_note(self, project_id: str, text: Any, kind: str, chat_id: Optional[str] = None) -> Dict[str, Any]:
        """Save a short durable fact to a project. Saving the same text again returns the note already there."""
        text = " ".join(str(text or "").split())
        if not text:
            raise ValueError("A note needs some text.")
        if len(text) > MAX_NOTE:
            raise ValueError("A note is at most %d characters; shorten it to the fact itself." % MAX_NOTE)
        if kind not in NOTE_KINDS:
            raise ValueError("A note's kind is one of: %s." % ", ".join(NOTE_KINDS))
        with self._lock:
            index = self._read_index()
            project = self._find(index, project_id)
            for note in project["notes"]:
                if note["text"].lower() == text.lower():
                    return note
            if len(project["notes"]) >= MAX_NOTES:
                raise ValueError("This project already has %d notes; the user can delete old ones in Project settings."
                                 % MAX_NOTES)
            note = {"id": "n" + secrets.token_hex(4), "kind": kind, "text": text, "created": time.time(), "chat": chat_id}
            project["notes"].append(note)
            self._write_index(index)
        return note

    def delete_note(self, project_id: str, note_id: str) -> None:
        with self._lock:
            index = self._read_index()
            project = self._find(index, project_id)
            kept = [n for n in project["notes"] if n.get("id") != note_id]
            if len(kept) == len(project["notes"]):
                raise KeyError("no note %r" % note_id)
            project["notes"] = kept
            self._write_index(index)

    def delete_project(self, project_id: str) -> None:
        """The project and its memory go; its conversations stay, unfiled."""
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
