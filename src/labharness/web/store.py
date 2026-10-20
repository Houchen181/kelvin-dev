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
from .rules import merge, normalize, restrictive

_ID = re.compile(r"^[0-9a-f]{12}$")
_PROJECT_ID = re.compile(r"^p[0-9a-f]{8}$")
MAX_NAME = 60
MAX_INSTRUCTIONS = 4000  # characters, for a project's instructions and for the global ones
MAX_NOTE = 300
MAX_NOTES = 50          # per project
MAX_GLOBAL_NOTES = 40
MAX_PAPERS = 200        # on a project's reading list
MAX_DEVICES = 200       # samples per project
MAX_SUMMARY = 6000      # the summary at the top of a project's master report
SHARING = ("", "unpublished", "confidential")   # on the Kelvin hub: shared (the default), or never
GENERAL = "general"     # the workspace of runs and devices outside every project
NOTE_KINDS = PROJECT_KINDS


def _label(text: Any) -> str:
    """A short one-line label that may be empty (a device's name or material)."""
    return " ".join(str(text or "").split())[:MAX_NAME]


def _name(name: Any) -> str:
    name = " ".join(str(name or "").split())[:MAX_NAME]
    if not name:
        raise ValueError("A project needs a name.")
    return name


def instructions_text(text: Any) -> str:
    return str(text or "").strip()[:MAX_INSTRUCTIONS]


class ChatStore:
    def __init__(self, root: Any, create: bool = True):
        self._lock = threading.Lock()
        self.use(root, create)

    def use(self, root: Any, create: bool = True) -> None:
        """Keep chats in another folder: the signed-in person's. Incognito chats of whoever was here are gone.
        create=False: make the folder only when there is something to save in it."""
        with self._lock:
            self.root = Path(root)
            if create:
                self.root.mkdir(parents=True, exist_ok=True)
            self._index_path = self.root / "_projects.json"
            self._temp: Dict[str, Dict[str, Any]] = {}  # incognito chats: this process only
            self._kept_cache: Optional[Tuple[Any, Dict[str, Any]]] = None

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
        return self._with_kept(json.loads(path.read_text(encoding="utf-8")), self._kept())

    def _kept(self) -> Dict[str, Any]:
        """The marks and rules chats keep from projects they were in (the index's "marks" and "stamped"), read again
        only when the index changes."""
        try:
            st = self._index_path.stat()
            key: Any = (st.st_mtime_ns, st.st_size)
        except OSError:
            return {"marks": {}, "stamped": {}}
        if self._kept_cache is None or self._kept_cache[0] != key:
            index = self._read_index()
            self._kept_cache = (key, {"marks": index["marks"], "stamped": index["stamped"]})
        return self._kept_cache[1]

    @staticmethod
    def _with_kept(chat: Dict[str, Any], kept: Dict[str, Any]) -> Dict[str, Any]:
        """A chat with what it keeps from projects it left: never shared on the hub, and still under their rules."""
        cid = chat.get("id")
        for m in kept["marks"].get(cid, []):
            chat[m] = True
        if cid in kept["stamped"]:
            chat["rules"] = merge(chat.get("rules"), kept["stamped"][cid])
        return chat

    def save(self, chat: Dict[str, Any]) -> None:
        chat["updated"] = time.time()
        if chat.get("incognito"):
            self._path(chat["id"])  # validates the id
            self._temp[chat["id"]] = chat
            return
        path = self._path(chat["id"])
        self.root.mkdir(parents=True, exist_ok=True)
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
            kept = [index[k].pop(chat_id, None) for k in ("assign", "marks", "stamped")]
            if any(k is not None for k in kept):
                self._write_index(index)

    def list(self) -> List[Dict[str, Any]]:
        index = self._read_index()
        assign = index["assign"]
        out = []
        for path in self.root.glob("*.json"):
            if not _ID.match(path.stem):  # the projects index, or a stray file
                continue
            try:
                chat = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            self._with_kept(chat, index)
            out.append({"id": chat["id"], "title": chat.get("title", "Chat"), "updated": chat.get("updated", 0),
                        "model": chat.get("model"), "project": assign.get(chat["id"]),
                        "rules": chat.get("rules"), **{m: True for m in index["marks"].get(chat["id"], [])}})
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
            p.setdefault("papers", [])
            p.setdefault("rules", {"providers": None, "literature": True})
            p.setdefault("devices", [])
            p.setdefault("summary", "")
        known = {p["id"] for p in projects}
        assign = {c: p for c, p in (data.get("assign") or {}).items() if p in known}
        notes = [n for n in data.get("global_notes") or [] if isinstance(n, dict) and n.get("id") and n.get("text")]
        general = data.get("general") if isinstance(data.get("general"), dict) else {}
        general.setdefault("devices", [])
        general.setdefault("summary", "")
        marks = {c: [m for m in ms if m in SHARING[1:]] for c, ms in (data.get("marks") or {}).items()
                 if isinstance(ms, list)}   # chats that left an unshared project (never shared on the hub)
        stamped = {}   # the rules of model-limited projects, kept by their chats wherever they go
        for c, r in (data.get("stamped") or {}).items():
            try:
                stamped[c] = normalize(r) if isinstance(r, dict) else None
            except ValueError:
                stamped[c] = {"providers": [], "literature": False}   # unreadable: the strictest
        return {"projects": projects, "assign": assign, "global_notes": notes, "general": general, "marks": marks,
                "stamped": {c: r for c, r in stamped.items() if r}}

    def _write_index(self, index: Dict[str, Any]) -> None:
        self._kept_cache = None
        self.root.mkdir(parents=True, exist_ok=True)
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
                   "instructions": "", "notes": [], "papers": [], "rules": {"providers": None, "literature": True}}
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

    def set_rules(self, project_id: str, rules: Any) -> Dict[str, Any]:
        """Which model providers may see this project, and whether literature search may be used. Its chats keep
        limits set now, wherever they are filed later (and so are never shared on the hub)."""
        rules = normalize(rules)   # ValueError for unknown providers
        with self._lock:
            index = self._read_index()
            project = self._find(index, project_id)
            project["rules"] = rules
            for chat_id, pid in index["assign"].items():
                if pid == project_id:
                    self._keep_rules(index, chat_id, project)
            self._write_index(index)
        return project

    def add_paper(self, project_id: str, paper: Dict[str, Any], why: str = "",
                  chat_id: Optional[str] = None) -> Tuple[Dict[str, Any], str]:
        """Put a paper on the project's reading list. Returns (entry, "saved" or "known")."""
        why = " ".join(str(why or "").split())[:300]
        with self._lock:
            index = self._read_index()
            project = self._find(index, project_id)
            for entry in project["papers"]:
                if entry["paper"].lower() == str(paper["id"]).lower():
                    if why:
                        entry["why"] = why
                        self._write_index(index)
                    return entry, "known"
            if len(project["papers"]) >= MAX_PAPERS:
                raise ValueError("the reading list is full (%d papers); remove some first" % MAX_PAPERS)
            entry = {"id": "r" + secrets.token_hex(4), "paper": str(paper["id"]), "title": str(paper.get("title", ""))[:300],
                     "authors": [str(a) for a in (paper.get("authors") or [])[:8]], "year": paper.get("year"),
                     "venue": str(paper.get("venue") or "")[:120], "url": str(paper.get("url") or ""),
                     "why": why, "chat": chat_id, "added": time.time()}
            project["papers"].append(entry)
            self._write_index(index)
        return entry, "saved"

    def delete_paper(self, project_id: str, entry_id: str) -> None:
        with self._lock:
            index = self._read_index()
            project = self._find(index, project_id)
            kept = [e for e in project["papers"] if e.get("id") != entry_id]
            if len(kept) == len(project["papers"]):
                raise KeyError("no paper %r on the reading list" % entry_id)
            project["papers"] = kept
            self._write_index(index)

    def set_sharing(self, project_id: str, sharing: Any) -> Dict[str, Any]:
        """Whether a connected Kelvin shares this project on the Kelvin hub (hubsync.py): "" (yes, the default),
        "unpublished" or "confidential" (never)."""
        sharing = str(sharing or "")
        if sharing not in SHARING:
            raise ValueError("sharing must be one of: %s" % ", ".join(repr(s) for s in SHARING))
        with self._lock:
            index = self._read_index()
            project = self._find(index, project_id)
            for mark in SHARING[1:]:
                project.pop(mark, None)
            if sharing:
                project[sharing] = True
            self._write_index(index)
        return project

    @staticmethod
    def _keep_marks(index: Dict[str, Any], chat_id: str, project: Optional[Dict[str, Any]]) -> None:
        """A chat leaving an unpublished or confidential project keeps that mark, and one leaving a model-limited
        project keeps its rules, so it is never shared later (nor read by a model the rules exclude)."""
        marks = [m for m in SHARING[1:] if project and project.get(m)]
        if marks:
            index["marks"][chat_id] = sorted(set(index["marks"].get(chat_id, [])) | set(marks))
        ChatStore._keep_rules(index, chat_id, project)

    @staticmethod
    def _keep_rules(index: Dict[str, Any], chat_id: str, project: Optional[Dict[str, Any]]) -> None:
        if project and restrictive(project.get("rules")):
            index["stamped"][chat_id] = merge(index["stamped"].get(chat_id), project["rules"])

    def delete_project(self, project_id: str) -> None:
        """The project and its memory go; its conversations stay, unfiled (and unshared, if it was)."""
        with self._lock:
            index = self._read_index()
            project = self._find(index, project_id)
            for chat_id, pid in index["assign"].items():
                if pid == project_id:
                    self._keep_marks(index, chat_id, project)
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
            old = index["assign"].get(chat_id)
            if old and old != project_id:
                self._keep_marks(index, chat_id, self._find(index, old))
            if project_id:
                index["assign"][chat_id] = project_id
            else:
                index["assign"].pop(chat_id, None)
            self._write_index(index)

    # ------------------------------------------------------------------ devices (samples) and the master report

    def _workspace(self, index: Dict[str, Any], project_id: Optional[str]) -> Dict[str, Any]:
        """A project, or the General workspace (project_id None or "general") for work outside every project."""
        return index["general"] if project_id in (None, "", GENERAL) else self._find(index, project_id)

    def devices(self, project_id: Optional[str]) -> List[Dict[str, Any]]:
        return list(self._workspace(self._read_index(), project_id)["devices"])

    def find_device(self, project_id: Optional[str], ref: Any) -> Dict[str, Any]:
        """A device by its id or its name (any case)."""
        ref = str(ref or "").strip()
        for d in self.devices(project_id):
            if d["id"] == ref or d["name"].lower() == ref.lower():
                return d
        raise KeyError("no device %r here (have: %s)" % (ref, ", ".join(d["name"] for d in self.devices(project_id))
                                                         or "none yet"))

    def add_device(self, project_id: Optional[str], name: Any, material: Any = "", description: Any = "") -> Dict[str, Any]:
        """A sample measured in this project, e.g. "D3" on a graphene chip."""
        name = _label(name)
        if not name:
            raise ValueError("Give the device a name, such as D3 or Chip 2 / Hall bar A.")
        with self._lock:
            index = self._read_index()
            devices = self._workspace(index, project_id)["devices"]
            if any(d["name"].lower() == name.lower() for d in devices):
                raise ValueError("There is already a device called %s here." % name)
            if len(devices) >= MAX_DEVICES:
                raise ValueError("This project already has %d devices; retire old ones first." % MAX_DEVICES)
            device = {"id": "d" + secrets.token_hex(4), "name": name, "material": _label(material),
                      "description": str(description or "").strip()[:MAX_INSTRUCTIONS], "created": time.time(),
                      "retired": False}
            devices.append(device)
            self._write_index(index)
        return device

    def update_device(self, project_id: Optional[str], device_id: str, **fields: Any) -> Dict[str, Any]:
        with self._lock:
            index = self._read_index()
            devices = self._workspace(index, project_id)["devices"]
            device = next((d for d in devices if d["id"] == device_id), None)
            if device is None:
                raise KeyError("no device %r" % device_id)
            if fields.get("name") is not None:
                name = _label(fields["name"])
                if not name:
                    raise ValueError("A device needs a name.")
                if any(d["name"].lower() == name.lower() and d["id"] != device_id for d in devices):
                    raise ValueError("There is already a device called %s here." % name)
                device["name"] = name
            if fields.get("material") is not None:
                device["material"] = _label(fields["material"])
            if fields.get("description") is not None:
                device["description"] = str(fields["description"]).strip()[:MAX_INSTRUCTIONS]
            if fields.get("retired") is not None:
                device["retired"] = bool(fields["retired"])
            self._write_index(index)
            return device

    def summary_of(self, project_id: Optional[str]) -> str:
        return self._workspace(self._read_index(), project_id).get("summary") or ""

    def set_summary(self, project_id: Optional[str], text: Any) -> None:
        """The words at the top of the project's master report; empty means Kelvin's summary from the data."""
        with self._lock:
            index = self._read_index()
            self._workspace(index, project_id)["summary"] = str(text or "").strip()[:MAX_SUMMARY]
            self._write_index(index)

    @staticmethod
    def _find(index: Dict[str, Any], project_id: str) -> Dict[str, Any]:
        for p in index["projects"]:
            if p["id"] == project_id:
                return p
        raise KeyError("no project %r" % project_id)
