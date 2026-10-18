"""Conversations, one JSON file each, and the projects they are filed under.

Chat history is append-only: earlier turns are never edited. Project membership lives in a separate index
(_projects.json), so filing a chat never rewrites the chat file that a running turn may be saving.

Each project also keeps its memory in that index: the user's instructions and a short list of notes (durable
facts). A chat sees only the memory of the project it is filed under, and deleting a project deletes its memory.
Global notes (the user's preferences and conventions) live there too. See memory.py for how notes are recalled.

Incognito chats live only in this process: never written to disk, never listed, never filed under a project.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .memory import KINDS, PROJECT_KINDS, SAME, similarity

_ID = re.compile(r"^[0-9a-f]{12}$")
_PROJECT_ID = re.compile(r"^p[0-9a-f]{8}$")
MAX_NAME = 60
MAX_INSTRUCTIONS = 4000  # characters, for a project's instructions and for the global ones
MAX_NOTE = 300
MAX_NOTES = 50          # per project
MAX_GLOBAL_NOTES = 40
NOTE_KINDS = PROJECT_KINDS


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
        self._temp: Dict[str, Dict[str, Any]] = {}  # incognito chats: this process only

    def _path(self, chat_id: str) -> Path:
        if not _ID.match(chat_id or ""):
            raise KeyError("no chat %r" % chat_id)
        return self.root / ("%s.json" % chat_id)

    # ------------------------------------------------------------------ chats

    def create(self, model: str, project: Optional[str] = None, incognito: bool = False) -> Dict[str, Any]:
        if incognito and project:
            raise ValueError("An incognito chat cannot belong to a project.")
        if project:
            self._find(self._read_index(), project)  # an unknown project creates nothing
        now = time.time()
        chat = {"id": secrets.token_hex(6), "title": "Incognito chat" if incognito else "New chat", "model": model,
                "created": now, "updated": now, "messages": []}
        if incognito:
            chat["incognito"] = True
        self.save(chat)
        if project:
            self.assign(chat["id"], project)
        chat["project"] = project or None
        return chat

    def load(self, chat_id: str) -> Dict[str, Any]:
        if chat_id in self._temp:
            return self._temp[chat_id]
        path = self._path(chat_id)
        if not path.exists():
            raise KeyError("no chat %r" % chat_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, chat: Dict[str, Any]) -> None:
        chat["updated"] = time.time()
        if chat.get("incognito"):
            self._path(chat["id"])  # validates the id
            self._temp[chat["id"]] = chat
            return
        path = self._path(chat["id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: v for k, v in chat.items() if k != "project"}, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, path)

    def delete(self, chat_id: str) -> None:
        if self._temp.pop(chat_id, None) is not None:
            return
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

    def is_incognito(self, chat_id: str) -> bool:
        return chat_id in self._temp

    def memory_of(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """The memory of the project this chat is filed under; None for an unfiled or incognito chat."""
        if chat_id in self._temp:
            return None
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
        notes = [n for n in data.get("global_notes") or [] if isinstance(n, dict) and n.get("id") and n.get("text")]
        return {"projects": projects, "assign": assign, "global_notes": notes}

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
        """Save a short durable fact to a project. Saving the same fact again returns the note already there."""
        return self.remember("project", text, kind, project_id=project_id, chat_id=chat_id)[0]

    def remember(self, scope: str, text: Any, kind: str, project_id: Optional[str] = None,
                 chat_id: Optional[str] = None, replaces: Optional[str] = None) -> Tuple[Dict[str, Any], str]:
        """Save a short, typed, durable fact. Returns (note, what happened): "saved", "known" (the same fact, in
        the same or other words, is already there) or "updated" (it corrects the note `replaces`)."""
        if scope not in KINDS:
            raise ValueError("A note is kept for the user (global) or for a project.")
        text = " ".join(str(text or "").split())
        if not text:
            raise ValueError("A note needs some text.")
        if len(text) > MAX_NOTE:
            raise ValueError("A note is at most %d characters; shorten it to the fact itself." % MAX_NOTE)
        if kind not in KINDS[scope]:
            raise ValueError("A %s note's kind is one of: %s." % (scope, ", ".join(KINDS[scope])))
        if scope == "project" and not project_id:
            raise ValueError("This conversation is not in a project, so there is no project to keep it for.")
        with self._lock:
            index = self._read_index()
            if scope == "project":
                notes, cap = self._find(index, project_id)["notes"], MAX_NOTES
                where = "This project already has %d notes; the user can delete old ones in Project settings."
            else:
                notes, cap = index["global_notes"], MAX_GLOBAL_NOTES
                where = "There are already %d global notes; the user can delete old ones in Settings."
            if replaces:
                old = next((n for n in notes if n.get("id") == replaces), None)
                if old is None:
                    raise KeyError("no note %r" % replaces)
                old.update(text=text, kind=kind, updated=time.time(), chat=chat_id or old.get("chat"))
                self._write_index(index)
                return old, "updated"
            for note in notes:
                if note["text"].lower() == text.lower() or similarity(note["text"], text) >= SAME:
                    return note, "known"
            if len(notes) >= cap:
                raise ValueError(where % cap)
            note = {"id": "n" + secrets.token_hex(4), "kind": kind, "text": text, "created": time.time(), "chat": chat_id}
            notes.append(note)
            self._write_index(index)
        return note, "saved"

    def global_notes(self) -> List[Dict[str, Any]]:
        return self._read_index()["global_notes"]

    def delete_global_note(self, note_id: str) -> None:
        with self._lock:
            index = self._read_index()
            kept = [n for n in index["global_notes"] if n.get("id") != note_id]
            if len(kept) == len(index["global_notes"]):
                raise KeyError("no note %r" % note_id)
            index["global_notes"] = kept
            self._write_index(index)

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
        if chat_id in self._temp:
            if project_id:
                raise ValueError("An incognito chat cannot be filed under a project.")
            return
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
