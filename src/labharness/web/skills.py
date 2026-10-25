"""Lab know-how kept for later: skills (procedures, in words) and recipes (staged measurements, see recipes.py).

A skill is a procedure in Markdown with a name and a one-line description of when it applies, packed as Claude's skills
are: a folder <name>/ with SKILL.md (its head: name and description; then the instructions) and any files that go
with it (a calibration table, a reference figure, a maker's sequence file, a script to read). How to cool the fridge
down, how to align the laser through the objective, how this lab finds the Dirac point. The model sees the names and
descriptions that apply to the chat, reads a skill with use_skill when a task matches it (or when the person starts it
with /name), and its files with read_skill_file. It keeps one with save_skill, once a person approves, when a procedure
worked and will be needed again. People can add a skill packed for Claude (.zip or .skill), take one of Kelvin's to
Claude, and switch any off. Recipes are listed and kept the same way (save_recipe) and run with run_recipe.

Three scopes that never mix, as memory's: Kelvin's own (built in, data/skills, read-only), the person's (every
project: "global") and a project's (only the chats filed under it). The person's and the projects' live in the
person's chat folder, skills/<scope>/<name>/SKILL.md (or, from before, skills/<scope>/<name>.md) and
recipes/<scope>/<name>.yaml; the ones switched off are listed in skills/off.json. Incognito chats see only Kelvin's
own and keep none."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import recipes
from .store import ChatStore

BUILTIN = Path(__file__).resolve().parent.parent / "data" / "skills"
KINDS = {"skill": ("skills", ".md"), "recipe": ("recipes", ".yaml")}
SKILL_FILE = "SKILL.md"
MAX_STEPS = 8000      # characters of a procedure Kelvin keeps
MAX_SHOWN = 40000     # characters of a skill use_skill gives (one added from elsewhere may be longer)
MAX_LISTED = 40       # skills and recipes named in the prompt
MAX_IMPORT_BYTES = 20 * 2 ** 20
MAX_IMPORT_FILES = 200
_SCOPE = re.compile(r"^(global|p[0-9a-f]{8})$")

USE_SKILL = {
    "name": "use_skill",
    "description": "Read one of the skills listed under 'Skills' (a procedure that worked before) before doing the "
                   "task it describes, and follow it unless the person says otherwise. It lists the files that come "
                   "with the skill.",
    "schema": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}},
}
READ_SKILL_FILE = {
    "name": "read_skill_file",
    "description": "Read a file that comes with a skill (use_skill lists them): a table, a figure, a reference PDF, or "
                   "a script to read (Kelvin doesn't run a skill's scripts). PDFs by pages, as read_file.",
    "schema": {"type": "object", "required": ["skill", "path"], "properties": {
        "skill": {"type": "string", "description": "The skill's name"},
        "path": {"type": "string", "description": "The file, as use_skill lists it, e.g. 'references/table.csv'"},
        "pages": {"type": "string", "description": "A PDF's pages: '3', '3-5', '7-' or '1,4,9' (empty: from the start)"},
        "start": {"type": "integer", "minimum": 0, "description": "Read on from this character (the answer says where)"},
        "figures": {"type": "boolean", "description": "A PDF's pages as drawn (figures, plots, equations), up to 20"}}},
}
SAVE_SKILL = {
    "name": "save_skill",
    "description": "Keep a procedure that worked, in words, so it can be followed again: the steps, values and "
                   "checks that mattered, and what to watch out for. A person approves it first. scope: project "
                   "(this project's chats; the default in a project) or global (every project: general know-how). "
                   "Saving under an existing name replaces that skill.",
    "schema": {"type": "object", "required": ["name", "description", "steps"], "properties": {
        "name": {"type": "string", "description": "Short, e.g. 'cool-down' or 'find-dirac-point'"},
        "description": {"type": "string", "description": "One line: when to use it"},
        "steps": {"type": "string", "description": "The procedure in Markdown: numbered steps with values"},
        "scope": {"type": "string", "enum": ["project", "global"]}}},
}
SAVE_RECIPE = {
    "name": "save_recipe",
    "description": "Keep a staged measurement that worked as a recipe, to run again with run_recipe: each stage a "
                   "plan (as run_plan takes it, with {{value}} or {{arithmetic}} for numbers that change), a pass "
                   "check on its data, values to keep for later stages, retries. Build it from the plans that "
                   "worked. A person approves it first. scope: project (default in a project) or global.",
    "schema": {"type": "object", "required": ["recipe"], "properties": {
        "recipe": {"type": "string", "description": "The recipe's YAML (recipe, description, values, stages)"},
        "scope": {"type": "string", "enum": ["project", "global"]}}},
}
READ_GUIDE = ("Skills are procedures that worked before; when a task matches one, read it with use_skill first (and "
              "the files it comes with, with read_skill_file, when the steps call for them).")
GUIDE = READ_GUIDE + """ Recipes are staged measurements that check their own results; run one with run_recipe \
(its name). When a procedure or a staged measurement has worked and will be needed again, offer to keep it \
(save_skill, save_recipe): a person approves each."""


@dataclass
class Item:
    kind: str           # skill | recipe
    name: str
    description: str
    scope: str          # kelvin | global | a project's id
    body: str           # a skill's steps; a recipe's YAML
    saved: float = 0.0
    folder: Optional[Path] = None              # a skill packed as a folder (SKILL.md and its files)
    files: List[str] = field(default_factory=list)   # the files with it, by their paths in the folder
    enabled: bool = True

    def view(self) -> Dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "description": self.description, "scope": self.scope,
                "body": self.body, "saved": self.saved, "files": self.files, "enabled": self.enabled}


def front(text: str) -> Dict[str, Any]:
    """A skill file's head (name, description, and anything else its YAML says) and its steps."""
    meta: Dict[str, Any] = {}
    body = text
    if text.startswith("---"):
        head, sep, rest = text[3:].partition("\n---")
        if sep:
            body = rest.split("\n", 1)[1] if "\n" in rest else ""
            try:
                import yaml
                loaded = yaml.safe_load(head)
            except Exception:
                loaded = None
            if isinstance(loaded, dict):
                meta.update({str(k): v for k, v in loaded.items()})
            else:   # a head YAML can't read: its lines, key: value
                for line in head.splitlines():
                    key, colon, value = line.partition(":")
                    if colon and key.strip():
                        meta[key.strip()] = value.strip()
    meta["steps"] = body.strip()
    return meta


def _one_line(text: Any, n: int = 300) -> str:
    return " ".join(str(text or "").split())[:n]


def skill_text(name: str, description: str, steps: str, metadata: Optional[Dict[str, str]] = None) -> str:
    """A SKILL.md as Claude's skills are written: its head (name, description, metadata), then the steps."""
    import yaml
    head: Dict[str, Any] = {"name": name, "description": description}
    if metadata:
        head["metadata"] = metadata
    return "---\n%s---\n%s\n" % (yaml.safe_dump(head, sort_keys=False, allow_unicode=True, width=1000), steps.strip())


class Know:
    """The skills and recipes a person has (and Kelvin's own), in the person's chat folder."""

    def __init__(self, store: ChatStore):
        self.store = store

    def _dir(self, kind: str, scope: str) -> Path:
        if not _SCOPE.match(scope):
            raise KeyError("no scope %r" % scope)
        return self.store.root / KINDS[kind][0] / scope

    # ------------------------------------------------------------------ switched off

    def _off_file(self) -> Path:
        return self.store.root / "skills" / "off.json"

    def _off(self) -> set:
        try:
            listed = json.loads(self._off_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        return {str(k) for k in listed} if isinstance(listed, list) else set()

    @staticmethod
    def _key(kind: str, scope: str, name: str) -> str:
        return "%s:%s:%s" % (kind, scope, name)

    def switch(self, kind: str, scope: str, name: str, on: bool) -> None:
        """Offer a skill or a recipe to Kelvin again, or no longer (it stays kept)."""
        if kind not in KINDS:
            raise KeyError("no kind %r" % kind)
        off = self._off()
        key = self._key(kind, scope, recipes.slug(name))
        (off.discard if on else off.add)(key)
        self._write(self._off_file(), json.dumps(sorted(off), indent=1))

    # ------------------------------------------------------------------ reading

    def _read(self, kind: str, path: Path, scope: str) -> Optional[Item]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        saved = path.stat().st_mtime if scope != "kelvin" else 0.0
        if kind == "skill":
            meta = front(text)
            return Item("skill", path.stem, _one_line(meta.get("description")), scope, meta["steps"], saved)
        try:
            recipe = recipes.parse(text)
        except recipes.RecipeError:
            return None   # a hand-edited file that no longer reads is not offered
        return Item("recipe", recipe.name, recipe.description, scope, text, saved)

    def _read_folder(self, folder: Path, scope: str) -> Optional[Item]:
        """A skill packed as a folder: SKILL.md and the files that go with it."""
        try:
            text = (folder / SKILL_FILE).read_text(encoding="utf-8-sig")
            files = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")
                           if p.is_file() and p != folder / SKILL_FILE)
            saved = (folder / SKILL_FILE).stat().st_mtime if scope != "kelvin" else 0.0
        except OSError:
            return None
        meta = front(text)
        return Item("skill", folder.name, _one_line(meta.get("description")), scope, meta["steps"], saved,
                    folder=folder, files=[f for f in files if not f.split("/")[-1].startswith(".")])

    def _skills_in(self, d: Path, scope: str) -> List[Item]:
        out = [i for i in (self._read("skill", p, scope) for p in sorted(d.glob("*.md"))) if i]
        named = {i.name for i in out}
        for sub in sorted(p for p in d.iterdir() if p.is_dir()) if d.is_dir() else []:
            if (sub / SKILL_FILE).is_file() and sub.name not in named and "." not in sub.name:
                item = self._read_folder(sub, scope)
                if item:
                    out.append(item)
        return out

    def _scopes(self, project: Optional[str], incognito: bool) -> List[str]:
        return [] if incognito else ["global"] + ([project] if project else [])

    def items(self, project: Optional[str] = None, incognito: bool = False, own: bool = True) -> List[Item]:
        """What a chat in this project can use: Kelvin's own skills (own), the person's and the project's; the ones
        switched off too (enabled False)."""
        out: List[Item] = []
        if own and BUILTIN.is_dir():
            out += self._skills_in(BUILTIN, "kelvin")
        for scope in self._scopes(project, incognito):
            for kind, (folder, ext) in KINDS.items():
                d = self.store.root / folder / scope
                if not d.is_dir():
                    continue
                if kind == "skill":
                    out += self._skills_in(d, scope)
                else:
                    out += [i for i in (self._read(kind, p, scope) for p in sorted(d.glob("*" + ext))) if i]
        off = self._off()
        for i in out:
            i.enabled = self._key(i.kind, i.scope, i.name) not in off
        return out

    def find(self, kind: str, name: str, project: Optional[str] = None, incognito: bool = False) -> Optional[Item]:
        """The project's first, then the person's, then Kelvin's own; never one switched off."""
        name = recipes.slug(name)
        found = [i for i in self.items(project, incognito) if i.kind == kind and i.name == name and i.enabled]
        order = {project: 0, "global": 1, "kelvin": 2}
        return min(found, key=lambda i: order.get(i.scope, 3)) if found else None

    def shown(self, item: Item) -> str:
        """A skill as use_skill gives it: its steps, and the files that come with it."""
        body = item.body if len(item.body) <= MAX_SHOWN else item.body[:MAX_SHOWN] + "\n… (the rest is not shown)"
        if item.files:
            body += "\n\nFiles with this skill (read_skill_file reads them): " + ", ".join(item.files[:60])
        return body

    # ------------------------------------------------------------------ keeping

    def save_skill(self, name: str, description: str, steps: str, scope: str, by: str = "") -> Item:
        name = recipes.slug(name)
        if not recipes.NAME.match(name):
            raise ValueError("a skill needs a short name (letters, digits, dashes)")
        description, steps = _one_line(description), str(steps or "").strip()
        if not description or not steps:
            raise ValueError("a skill needs a description (when to use it) and its steps")
        if len(steps) > MAX_STEPS:
            raise ValueError("the steps are longer than %d characters: keep what matters" % MAX_STEPS)
        meta = {"saved": time.strftime("%Y-%m-%d")}
        if by:
            meta["chat"] = _one_line(by, 80)
        folder = self._dir("skill", scope) / name
        self._write(folder / SKILL_FILE, skill_text(name, description, steps, meta))
        (self._dir("skill", scope) / (name + ".md")).unlink(missing_ok=True)   # kept as a file, from before
        return Item("skill", name, description, scope, steps, time.time(), folder=folder,
                    files=self._read_folder(folder, scope).files if folder.is_dir() else [])

    def save_recipe(self, recipe: recipes.Recipe, scope: str) -> Item:
        text = recipes.dump(recipe)
        self._write(self._dir("recipe", scope) / (recipe.name + ".yaml"), text)
        return Item("recipe", recipe.name, recipe.description, scope, text, time.time())

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def delete(self, kind: str, scope: str, name: str) -> bool:
        """Kelvin's own cannot be deleted (KeyError)."""
        if kind not in KINDS:
            raise KeyError("no kind %r" % kind)
        name = recipes.slug(name)
        path = self._dir(kind, scope) / (name + KINDS[kind][1])
        folder = self._dir(kind, scope) / name
        if kind == "skill" and (folder / SKILL_FILE).is_file():
            shutil.rmtree(folder)
            return True
        if not path.is_file():
            return False
        path.unlink()
        return True

    # ------------------------------------------------------------------ to and from Claude

    def import_skill(self, scope: str, data: bytes, filename: str = "", replace: bool = False) -> Item:
        """A skill as Claude's are packed: a .zip (or .skill) with SKILL.md at its top or in one folder, and the files
        that go with it; or a lone SKILL.md. ValueError: not a skill Kelvin can take; FileExistsError: there is one by
        its name here (replace overwrites it)."""
        target_dir = self._dir("skill", scope)
        files: Dict[str, bytes] = {}
        prefix = ""
        if data[:4] == b"PK\x03\x04":
            try:
                pack = zipfile.ZipFile(io.BytesIO(data))
                entries = [(i, i.filename.replace("\\", "/")) for i in pack.infolist() if not i.is_dir()]
            except zipfile.BadZipFile as e:
                raise ValueError("Kelvin could not open this .zip (%s)" % e)
            heads = [n for _, n in entries if n.split("/")[-1] == SKILL_FILE and n.count("/") <= 1
                     and not n.startswith("__MACOSX/")]
            if len(heads) != 1:
                raise ValueError("a skill's .zip holds one SKILL.md, at its top or in one folder")
            prefix, total = heads[0][:-len(SKILL_FILE)], 0
            for info, name in entries:
                rel = name[len(prefix):] if name.startswith(prefix) else ""
                parts = rel.split("/")
                if not rel or name.startswith("__MACOSX/") or any(p in ("", ".", "..") or p.startswith(".")
                                                                   for p in parts) or ":" in rel:
                    continue   # outside the skill's folder, a folder's own entry, or a system's hidden file
                total += info.file_size
                if len(files) >= MAX_IMPORT_FILES or total > MAX_IMPORT_BYTES:
                    raise ValueError("this skill is bigger than Kelvin takes (%d files, %d MB at most)"
                                     % (MAX_IMPORT_FILES, MAX_IMPORT_BYTES // 2 ** 20))
                files[rel] = pack.read(info)
        else:
            files[SKILL_FILE] = data
        try:
            text = files[SKILL_FILE].decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("SKILL.md is not text (UTF-8)")
        meta = front(text)   # its name: as SKILL.md says, else its folder's in the .zip, else the file's
        stem = Path(filename).stem if Path(filename).stem.lower() != "skill" else ""
        name = recipes.slug(meta.get("name") or prefix.strip("/") or stem)
        if not recipes.NAME.match(name):
            raise ValueError("SKILL.md needs a name: lower-case letters, digits and dashes")
        if not _one_line(meta.get("description")):
            raise ValueError("SKILL.md needs a description: when to use the skill")
        if not meta["steps"]:
            raise ValueError("SKILL.md has no instructions after its head")
        folder, flat = target_dir / name, target_dir / (name + ".md")
        if (folder.exists() or flat.exists()) and not replace:
            raise FileExistsError("there is a skill %s here already" % name)
        staging = target_dir / (name + ".adding")
        shutil.rmtree(staging, ignore_errors=True)
        for rel, content in files.items():
            path = staging / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        shutil.rmtree(folder, ignore_errors=True)
        flat.unlink(missing_ok=True)
        staging.rename(folder)
        item = self._read_folder(folder, scope)
        if item is None:
            raise ValueError("Kelvin could not read the skill it added")
        return item

    def export(self, scope: str, name: str) -> Tuple[str, bytes]:
        """A skill as a .zip Claude takes: <name>/SKILL.md and its files. KeyError: no such skill."""
        name = recipes.slug(name)
        where = BUILTIN if scope == "kelvin" else self._dir("skill", scope)
        item = next((i for i in self._skills_in(where, scope) if i.name == name), None)
        if item is None:
            raise KeyError("no skill %r" % name)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as pack:
            if item.folder is not None:
                for p in sorted(item.folder.rglob("*")):
                    if p.is_file() and not p.name.startswith("."):
                        pack.write(p, "%s/%s" % (name, p.relative_to(item.folder).as_posix()))
            else:   # kept as one file, from before: written as Claude's skills are
                meta = front((where / (name + ".md")).read_text(encoding="utf-8"))
                extra = {k: str(meta[k]) for k in ("saved",) if meta.get(k)}
                pack.writestr("%s/%s" % (name, SKILL_FILE), skill_text(name, item.description, item.body, extra))
        return name + ".zip", buf.getvalue()

    # ------------------------------------------------------------------ the prompt

    def index(self, project: Optional[str] = None, incognito: bool = False) -> str:
        """The prompt's list: each skill and recipe by name and when to use it (the newest first, at most
        MAX_LISTED), or "" when there are none."""
        items = sorted((i for i in self.items(project, incognito) if i.enabled),
                       key=lambda i: (i.scope == "kelvin", -i.saved))[:MAX_LISTED]
        whose = {"kelvin": "Kelvin's", "global": "yours"}
        lines = []
        for kind, title in (("skill", "Skills"), ("recipe", "Recipes")):
            mine = [i for i in items if i.kind == kind]
            if mine:
                lines.append("%s:" % title)
                lines += ["- %s: %s (%s)" % (i.name, i.description, whose.get(i.scope, "this project's"))
                          for i in mine]
        return "\n".join(lines)
