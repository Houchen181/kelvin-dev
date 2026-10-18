"""Kelvin's memory: what it keeps between conversations, and what it recalls for one.

Three levels that never mix:
- global: the user's preferences and conventions, used in every conversation;
- project: a project's instructions and notes (decisions, procedures, quirks, results), used only in the
  conversations filed under that project;
- conversation: the chat itself. Nothing from one chat's history is copied into another.

Learning happens through the `remember` tool: the model saves a short, typed fact when something durable is
settled, never questions or chit-chat. Every note is visible to the user, can be deleted, and is shown when it
is recalled. Recalled notes travel with the user's message (not the system prompt), and a large project recalls
only the notes that bear on the question, so old, unrelated facts do not steer a new answer.

Incognito chats use no memory at all and save none.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

GLOBAL_KINDS = ("preference", "convention", "profile")
PROJECT_KINDS = ("decision", "procedure", "quirk", "result")
KINDS = {"global": GLOBAL_KINDS, "project": PROJECT_KINDS}
RECALL_ALL_BELOW = 12   # a small project recalls all its notes; a bigger one only the most relevant
RECALL_TOP = 10
SAME = 0.8              # notes this similar are the same fact

_WORD = re.compile(r"[a-z0-9_.µΩ°+-]+")
_STOP = set("""a an and are as at be been but by can could did do does for from had has have how i if in into is it its
just me my no not of on or our so than that the their them then there these they this to too us was we were what when
where which who why will with would you your about after again all also any because before being both each few
further here more most other over same should some such only own very once out up down off under until while
please thanks thank ok okay yes hi hello""".split())


def words(text: str) -> List[str]:
    """Lower-case content words; instrument keys such as smu.V stay whole (and also split)."""
    out: List[str] = []
    for w in _WORD.findall(str(text or "").lower()):
        w = w.strip(".-+")
        if (len(w) < 2 and not w.isdigit()) or w in _STOP:  # numbers always count: 6 V is not 8 V
            continue
        out.append(w)
        if "." in w and not w.replace(".", "").isdigit():
            out += [p for p in w.split(".") if len(p) >= 2 and p not in _STOP]
    return out


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of content words: 1 = the same fact in other words."""
    wa, wb = set(words(a)), set(words(b))
    if not wa or not wb:
        return 1.0 if str(a).strip().lower() == str(b).strip().lower() else 0.0
    return len(wa & wb) / len(wa | wb)


def rank(notes: Sequence[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    """Notes ordered by how much they bear on `query` (idf-weighted word overlap), most relevant first.
    Ties go to the newer note. Notes sharing no word with the query score zero."""
    q = set(words(query))
    docs = [set(words(n.get("text", ""))) for n in notes]
    df: Dict[str, int] = {}
    for d in docs:
        for w in d:
            df[w] = df.get(w, 0) + 1
    n = max(1, len(docs))

    def score(i: int) -> float:
        return sum(math.log(1 + n / df[w]) for w in q & docs[i])

    order = sorted(range(len(notes)), key=lambda i: (score(i), notes[i].get("created", 0)), reverse=True)
    return [dict(notes[i], score=round(score(i), 3)) for i in order]


def recall(global_notes: Sequence[Dict[str, Any]], project: Optional[Dict[str, Any]], query: str,
           already: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """The notes to give the model with this message: every global note (they are the user's standing
    preferences), and the project's notes - all of them while the project is small, otherwise only those that
    share words with the question. Notes already given earlier in this chat are not repeated."""
    seen = set(already)
    out = [dict(n, scope="global") for n in global_notes if n.get("id") not in seen]
    if project:
        notes = [n for n in project.get("notes", []) if n.get("id") not in seen]
        if len(project.get("notes", [])) > RECALL_ALL_BELOW:
            notes = [n for n in rank(notes, query) if n["score"] > 0][:RECALL_TOP]
            notes.sort(key=lambda n: n.get("created", 0))
        out += [dict(n, scope="project") for n in notes]
    return [{"id": n["id"], "scope": n["scope"], "kind": n.get("kind", ""), "text": n.get("text", ""),
             "created": n.get("created")} for n in out]


def block(notes: Sequence[Dict[str, Any]], project_name: Optional[str] = None) -> str:
    """How recalled notes are put in front of the user's message."""
    if not notes:
        return ""
    lines = ["<memory>",
             "Background from earlier work (saved notes, not part of this conversation). Use a note only if it bears "
             "on the question; it may be out of date."]
    for n in notes:
        where = "you, every project" if n["scope"] == "global" else "project %s" % (project_name or "")
        lines.append("- [%s; %s; id %s] %s" % (n["kind"], where.strip(), n["id"], n["text"]))
    lines.append("</memory>")
    return "\n".join(lines)
