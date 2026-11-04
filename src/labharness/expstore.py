"""The lab's experiments: kept translations of the lab's VIs, and every version of each, in Kelvin's own folder
(~/Kelvin/experiments), so everyone who signs in on this Kelvin sees and runs them.

    experiments/<name>/v<N>/experiment.py     the experiment, byte for byte as it was written
                            translation.json  where it came from: the VI, the checks, what wasn't translated
                            version.json      who made it and when, what it was edited from, who approved it
    experiments/.drafts/<id>/                 a translation nobody kept yet

A version, once written, is never changed: an edit (by the AI or a person) is a new version, edited from the latest,
and is not approved until a person approves it (its first run asks). Approval is of the file as it was written: a
version whose file changed on disk afterwards is no longer approved, and can't be again. Nothing here is deleted.
"""
from __future__ import annotations

import datetime
import difflib
import hashlib
import json
import os
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_DRAFT = re.compile(r"^d\d{8}-\d{6}-[0-9a-f]{6}$")
_VERSION = re.compile(r"^v([1-9][0-9]*)$")


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes((json.dumps(data, indent=1, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
    os.replace(tmp, path)


@dataclass
class Version:
    name: str
    number: int
    folder: Path
    made_by: str
    made_at: str
    edited_from: Optional[int]
    note: str
    sha256: str
    approved_by: str = ""
    approved_at: str = ""

    @property
    def path(self) -> Path:
        return self.folder / "experiment.py"

    @property
    def intact(self) -> bool:
        """Is the file still what was written (its fingerprint)?"""
        try:
            return _sha(self.path.read_bytes()) == self.sha256
        except OSError:
            return False

    @property
    def approved(self) -> bool:
        return bool(self.approved_by) and self.intact

    def translation(self) -> Dict[str, Any]:
        try:
            data = json.loads((self.folder / "translation.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def record(self) -> Dict[str, Any]:
        return {"name": self.name, "number": self.number, "made_by": self.made_by, "made_at": self.made_at,
                "edited_from": self.edited_from, "note": self.note, "sha256": self.sha256,
                "approved_by": self.approved_by, "approved_at": self.approved_at}


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)

    # ---------------------------------------------------------------- reading

    def _check_name(self, name: str) -> None:
        if not isinstance(name, str) or not NAME.match(name):
            raise ValueError("%r isn't a name Kelvin can keep an experiment under: lowercase letters, digits, - and _, "
                             "up to 64, starting with a letter or a digit" % (name,))

    def names(self) -> List[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and NAME.match(p.name) and self.has(p.name))

    def has(self, name: str) -> bool:
        return isinstance(name, str) and bool(NAME.match(name)) and bool(self.versions(name))

    def versions(self, name: str) -> List[Version]:
        folder = self.root / name
        if not isinstance(name, str) or not NAME.match(name) or not folder.is_dir():
            return []
        out = []
        for p in folder.iterdir():
            m = _VERSION.match(p.name)
            if not m:
                continue
            try:
                data = json.loads((p / "version.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append(Version(name, int(m.group(1)), p, str(data.get("made_by") or ""), str(data.get("made_at") or ""),
                               data.get("edited_from"), str(data.get("note") or ""), str(data.get("sha256") or ""),
                               str(data.get("approved_by") or ""), str(data.get("approved_at") or "")))
        return sorted(out, key=lambda v: v.number)

    def get(self, name: str, number: Optional[int] = None) -> Version:
        """A version of a kept experiment: its latest when number is None. KeyError, in words, when there isn't one."""
        versions = self.versions(name)
        if not versions:
            raise KeyError("the lab has no experiment %r (it has: %s)" % (name, ", ".join(self.names()) or "none"))
        if number is None:
            return versions[-1]
        for v in versions:
            if v.number == number:
                return v
        raise KeyError("%s has no version %s (it has v1 to v%d)" % (name, number, versions[-1].number))

    def find(self, path: Any) -> Optional[Version]:
        """The version whose file a path is (a run of it, run again: the same version), or None."""
        try:
            p = Path(str(path)).resolve()
            root = self.root.resolve()
        except (OSError, ValueError, RuntimeError):
            return None
        m = _VERSION.match(p.parent.name)
        if p.name != "experiment.py" or m is None or p.parent.parent.parent != root:
            return None
        try:
            return self.get(p.parent.parent.name, int(m.group(1)))
        except KeyError:
            return None

    def diff(self, name: str, a: int, b: int) -> str:
        """What changed from version a to version b, as a unified diff."""
        old = self.get(name, a).path.read_text(encoding="utf-8").splitlines(keepends=True)
        new = self.get(name, b).path.read_text(encoding="utf-8").splitlines(keepends=True)
        return "".join(difflib.unified_diff(old, new, fromfile="%s v%d" % (name, a), tofile="%s v%d" % (name, b)))

    # ---------------------------------------------------------------- writing

    def add(self, name: str, source: str, translation: Dict[str, Any], by: str, note: str = "",
            edited_from: Optional[int] = None) -> Version:
        """A new version: v1 of a new experiment (edited_from None), or the next one of a kept experiment, edited from
        its latest. Written whole into a folder of its own, or not at all."""
        self._check_name(name)
        versions = self.versions(name)
        if versions:
            latest = versions[-1].number
            if edited_from is None:
                raise ValueError("%s is kept already: a change is a new version, edited from v%d" % (name, latest))
            if edited_from != latest:
                raise ValueError("%s is at version %d now, made meanwhile: edit v%d" % (name, latest, latest))
            number = latest + 1
        else:
            if edited_from is not None:
                raise ValueError("%s has no version %d to edit: it isn't kept yet" % (name, edited_from))
            number = 1
        data = source.encode("utf-8")
        record = {"name": name, "number": number, "made_by": by, "made_at": _now(), "edited_from": edited_from,
                  "note": note, "sha256": _sha(data), "approved_by": "", "approved_at": ""}
        folder = self.root / name
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (".v%d-%s" % (number, secrets.token_hex(4)))
        tmp.mkdir()
        try:
            (tmp / "experiment.py").write_bytes(data)
            (tmp / "translation.json").write_bytes((json.dumps(translation, indent=1, ensure_ascii=False,
                                                               default=str) + "\n").encode("utf-8"))
            _write_json(tmp / "version.json", record)
            os.rename(tmp, folder / ("v%d" % number))   # all or nothing; fails if another write made it meanwhile
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return self.get(name, number)

    def approve(self, name: str, number: int, by: str) -> Version:
        """A person's approval of a version, as it was written."""
        v = self.get(name, number)
        if not v.intact:
            raise ValueError("%s v%d's file changed after it was written, so it can't be approved: save it as a new "
                             "version" % (name, number))
        record = dict(v.record(), approved_by=by, approved_at=_now())
        _write_json(v.folder / "version.json", record)
        return self.get(name, number)

    # ---------------------------------------------------------------- drafts: translations nobody kept yet

    def draft(self, source: str, translation: Dict[str, Any], by: str) -> str:
        """Keep a translation as a draft until a person keeps it: its id."""
        draft_id = "d%s-%s" % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), secrets.token_hex(3))
        folder = self.root / ".drafts" / draft_id
        folder.mkdir(parents=True)
        (folder / "experiment.py").write_bytes(source.encode("utf-8"))
        _write_json(folder / "translation.json", translation)
        _write_json(folder / "draft.json", {"by": by, "at": _now()})
        return draft_id

    def _draft_folder(self, draft_id: str) -> Path:
        if not isinstance(draft_id, str) or not _DRAFT.match(draft_id):
            raise ValueError("%r isn't a draft's id (translate_vi gives one, such as d20261102-101500-a1b2c3)"
                             % (draft_id,))
        folder = self.root / ".drafts" / draft_id
        if not (folder / "experiment.py").is_file():
            raise KeyError("there is no draft %s (kept already, or never made)" % draft_id)
        return folder

    def drafts(self) -> List[Dict[str, Any]]:
        """The drafts nobody kept yet, newest first: {id, name, vi, by, at, ok_to_keep}."""
        folder = self.root / ".drafts"
        out = []
        for p in sorted(folder.iterdir(), reverse=True) if folder.is_dir() else []:
            if not _DRAFT.match(p.name) or not (p / "experiment.py").is_file():
                continue
            try:
                about = json.loads((p / "draft.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                about = {}
            t = self.draft_translation(p.name)
            out.append({"id": p.name, "name": t.get("name") or "", "vi": (t.get("vi") or {}).get("name") or "",
                        "by": about.get("by") or "", "at": about.get("at") or "",
                        "ok_to_keep": bool((t.get("report") or {}).get("ok_to_keep"))})
        return out

    def draft_path(self, draft_id: str) -> Path:
        return self._draft_folder(draft_id) / "experiment.py"

    def draft_translation(self, draft_id: str) -> Dict[str, Any]:
        try:
            data = json.loads((self._draft_folder(draft_id) / "translation.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def discard(self, draft_id: str) -> None:
        """A draft a person lets go, deleted. KeyError: there is no such draft; ValueError: not a draft's id; OSError:
        it could not be deleted (a file in it held open, say)."""
        shutil.rmtree(self._draft_folder(draft_id))

    def keep(self, draft_id: str, name: str, by: str) -> Version:
        """A person keeps a draft: v1 of a new experiment, or the next version of a kept one, approved by them."""
        folder = self._draft_folder(draft_id)
        source = (folder / "experiment.py").read_bytes().decode("utf-8")
        try:
            made_by = str(json.loads((folder / "draft.json").read_text(encoding="utf-8")).get("by") or by)
        except (OSError, ValueError):
            made_by = by
        versions = self.versions(name)
        v = self.add(name, source, self.draft_translation(draft_id), made_by,
                     edited_from=versions[-1].number if versions else None)
        v = self.approve(name, v.number, by)
        shutil.rmtree(folder, ignore_errors=True)
        return v
