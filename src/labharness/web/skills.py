"""Lab know-how kept for later: skills (procedures, in words) and recipes (staged measurements, see recipes.py).

A skill is a procedure in Markdown with a name and a one-line description of when it applies, as SKILL.md files
are: how to cool the fridge down, how to align the laser through the objective, how this lab finds the Dirac point.
The model sees the names and descriptions that apply to the chat and reads a skill with use_skill when a task
matches it; it keeps one with save_skill, once a person approves, when a procedure worked and will be needed again.
Recipes are listed and kept the same way (save_recipe) and run with run_recipe.

Three scopes that never mix, as memory's: Kelvin's own (built in, data/skills, read-only), the person's (every
project: "global") and a project's (only the chats filed under it). The person's and the projects' live in the
person's chat folder, skills/<scope>/<name>.md and recipes/<scope>/<name>.yaml. Incognito chats see only Kelvin's
own and keep none."""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import recipes
from .store import ChatStore

BUILTIN = Path(__file__).resolve().parent.parent / "data" / "skills"
KINDS = {"skill": ("skills", ".md"), "recipe": ("recipes", ".yaml")}
MAX_STEPS = 8000      # characters of a skill's procedure
MAX_LISTED = 40       # skills and recipes named in the prompt
_SCOPE = re.compile(r"^(global|p[0-9a-f]{8})$")

USE_SKILL = {
    "name": "use_skill",
    "description": "Read one of the skills listed under 'Skills' (a procedure that worked before) before doing the "
                   "task it describes, and follow it unless the person says otherwise.",
    "schema": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}},
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
READ_GUIDE = "Skills are procedures that worked before; when a task matches one, read it with use_skill first."
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

    def view(self) -> Dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "description": self.description, "scope": self.scope,
                "body": self.body, "saved": self.saved}


def front(text: str) -> Dict[str, Any]:
    """A skill file's head (name, description) and its steps."""
    meta: Dict[str, Any] = {}
    body = text
    if text.startswith("---"):
        head, sep, rest = text[3:].partition("\n---")
        if sep:
            body = rest.lstrip("\n")
            for line in head.splitlines():
                key, colon, value = line.partition(":")
                if colon and key.strip():
                    meta[key.strip()] = value.strip()
    meta["steps"] = body.strip()
    return meta


def _one_line(text: Any, n: int = 200) -> str:
    return " ".join(str(text or "").split())[:n]


class Know:
    """The skills and recipes a person has (and Kelvin's own), in the person's chat folder."""

    def __init__(self, store: ChatStore):
        self.store = store

    def _dir(self, kind: str, scope: str) -> Path:
        if not _SCOPE.match(scope):
            raise KeyError("no scope %r" % scope)
        return self.store.root / KINDS[kind][0] / scope

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

    def _scopes(self, project: Optional[str], incognito: bool) -> List[str]:
        return [] if incognito else ["global"] + ([project] if project else [])

    def items(self, project: Optional[str] = None, incognito: bool = False, own: bool = True) -> List[Item]:
        """What a chat in this project can use: Kelvin's own skills (own), the person's and the project's."""
        out: List[Item] = []
        if own and BUILTIN.is_dir():
            out += [i for i in (self._read("skill", p, "kelvin") for p in sorted(BUILTIN.glob("*.md"))) if i]
        for scope in self._scopes(project, incognito):
            for kind, (folder, ext) in KINDS.items():
                d = self.store.root / folder / scope
                if d.is_dir():
                    out += [i for i in (self._read(kind, p, scope) for p in sorted(d.glob("*" + ext))) if i]
        return out

    def find(self, kind: str, name: str, project: Optional[str] = None, incognito: bool = False) -> Optional[Item]:
        """The project's first, then the person's, then Kelvin's own."""
        name = recipes.slug(name)
        found = [i for i in self.items(project, incognito) if i.kind == kind and i.name == name]
        order = {project: 0, "global": 1, "kelvin": 2}
        return min(found, key=lambda i: order.get(i.scope, 3)) if found else None

    def save_skill(self, name: str, description: str, steps: str, scope: str, by: str = "") -> Item:
        name = recipes.slug(name)
        if not recipes.NAME.match(name):
            raise ValueError("a skill needs a short name (letters, digits, dashes)")
        description, steps = _one_line(description), str(steps or "").strip()
        if not description or not steps:
            raise ValueError("a skill needs a description (when to use it) and its steps")
        if len(steps) > MAX_STEPS:
            raise ValueError("the steps are longer than %d characters: keep what matters" % MAX_STEPS)
        text = "---\nname: %s\ndescription: %s\nsaved: %s%s\n---\n%s\n" % (
            name, description, time.strftime("%Y-%m-%d"), " (%s)" % _one_line(by, 80) if by else "", steps)
        self._write(self._dir("skill", scope) / (name + ".md"), text)
        return Item("skill", name, description, scope, steps, time.time())

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
        path = self._dir(kind, scope) / (recipes.slug(name) + KINDS[kind][1])
        if not path.is_file():
            return False
        path.unlink()
        return True

    def index(self, project: Optional[str] = None, incognito: bool = False) -> str:
        """The prompt's list: each skill and recipe by name and when to use it (the newest first, at most
        MAX_LISTED), or "" when there are none."""
        items = sorted(self.items(project, incognito), key=lambda i: (i.scope == "kelvin", -i.saved))[:MAX_LISTED]
        whose = {"kelvin": "Kelvin's", "global": "yours"}
        lines = []
        for kind, title in (("skill", "Skills"), ("recipe", "Recipes")):
            mine = [i for i in items if i.kind == kind]
            if mine:
                lines.append("%s:" % title)
                lines += ["- %s: %s (%s)" % (i.name, i.description, whose.get(i.scope, "this project's"))
                          for i in mine]
        return "\n".join(lines)
