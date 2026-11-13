"""Kelvin's own documentation, for the models: where things are in the portal (data/portal.md, which the portal's
models also get in their instructions) and how Kelvin works (the user documentation, docs/*.md, which the app
carries in labharness/data/docs). With it a model sends a person to the right place in the portal instead of
guessing at a menu, and explains Kelvin's rules as they are written. The portal's Help shows people the same pages
(lookup, topics, section)."""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Dict, List, Tuple

HERE = Path(__file__).resolve().parent
MAP = HERE / "data" / "portal.md"
DOCS = ("portal.md", "safety.md", "data.md", "instruments.md", "agents.md", "hub.md")   # what people read
MAX_PASSAGE = 1500
_WORD = re.compile(r"[a-z0-9µ]+")
_STOP = set("""a an and are as at be by can could do does for from has have how i if in into is it its me my no not of
on or so that the then there this to was what when where which who why will with would you your kelvin""".split())


def portal_map() -> str:
    """The map of the portal, whole ("" if the app lacks it): kelvin_help searches it, point by point."""
    try:
        return MAP.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


INDEX_LEAD = """# Where things are in Kelvin's portal

You can't see the portal. When the person must do something there, tell them exactly where, with the names in bold \
as the portal shows them; never guess a menu or a button. The portal's map has a point on each of the places below: \
kelvin_help gives a point whole (ask it with the place's or the task's words) before you tell a person where to \
click. If it isn't there either, say you don't know where it is."""
MAX_INDEX_TOPIC = 90


def portal_index() -> str:
    """The map's table of contents, for a model's instructions: each point's place in a line, its steps through
    kelvin_help. The whole map (about 37,000 characters, a quarter of a chat's prompt) went with every step a model
    took, for the few answers that send a person somewhere; a place's point, looked up when it's needed, says the
    same."""
    whole = portal_map()
    if not whole:
        return ""
    places: List[str] = []
    for line in whole.splitlines():
        if not line.startswith("- "):
            continue
        topic = re.split(r":(?:\s|$)", line[2:], maxsplit=1)[0].strip()
        if len(topic) > MAX_INDEX_TOPIC:   # a point that opens with a sentence: its first words
            topic = topic[:MAX_INDEX_TOPIC].rsplit(" ", 1)[0] + " …"
        if topic and topic not in places:
            places.append(topic)
    return INDEX_LEAD + "\n" + "\n".join("- " + p for p in places)


def _folders() -> List[Path]:
    return [HERE / "data" / "docs", HERE.parents[1] / "docs"]   # in the app; in a checkout of the source


def passages() -> List[Tuple[str, str]]:
    """(where, text): the map's points and each document's sections, cut at headings and top-level bullets."""
    out: List[Tuple[str, str]] = []
    sources = [("the portal's map", MAP)] + [
        (name, next((f / name for f in _folders() if (f / name).is_file()), None)) for name in DOCS]
    for name, path in sources:
        if path is None or not path.is_file():
            continue
        heading: List[str] = []
        buf: List[str] = []
        code = False

        def flush() -> None:
            text = "\n".join(buf).strip()
            # a point's topic: "A draft card:", "The **Runs** pane:", "**Runs pane:**", in its place's words, so a
            # question in them finds that point (the prompt lists the map's places by them: guide.portal_index)
            lead = re.match(r"^- ([^\n]{2,120}?):(?:\*\*)?(?:\s|$)", text)
            topic = lead.group(1).replace("**", "").strip() if lead else ""
            if text:
                out.append((" › ".join([name] + heading + ([topic] if topic else [])), text))
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("```"):
                code = not code
            m = None if code else re.match(r"^(#{1,4})\s+(.*)", line)
            if m:
                flush()
                buf = []
                heading = heading[:len(m.group(1)) - 1] + [m.group(2).strip()]
                continue
            if line.startswith("- ") and not code and buf:
                flush()
                buf = []
            buf.append(line)
        flush()
    return out


def _best(question: str, limit: int, found: List[Tuple[str, str]]) -> List[int]:
    """The passages (by index) that bear on the question, best first."""
    words = sorted({w for w in _WORD.findall(str(question or "").lower()) if len(w) > 1 and w not in _STOP})
    n = len(found)
    texts = [(where + "\n" + text).lower() for where, text in found]
    df = {w: sum(1 for t in texts if w in t) for w in words}
    scored = []
    for i, (t, (where, _)) in enumerate(zip(texts, found)):
        score = sum(math.log(1 + n / (1 + df[w])) * (2.0 if w in where.lower() else 1.0) for w in words if w in t)
        score *= 1.3 if where.startswith("the portal's map") else 1.0   # the map is where things are
        if score > 0:
            scored.append((score, i))
    return [i for _, i in sorted(scored, key=lambda s: (-s[0], s[1]))[:max(1, min(8, int(limit)))]]


def search(question: str, limit: int = 4) -> str:
    """The passages of Kelvin's documentation that bear on the question, best first."""
    if not any(w not in _STOP and len(w) > 1 for w in _WORD.findall(str(question or "").lower())):
        return "say what to look up, e.g. 'review a draft card' or 'change an instrument's limits'"
    found = passages()
    if not found:
        return "Kelvin's documentation is not on this computer"
    best = _best(question, limit, found)
    if not best:
        return "nothing in Kelvin's documentation about %r" % question
    return "\n\n".join("[%s]\n%s" % (found[i][0], found[i][1] if len(found[i][1]) <= MAX_PASSAGE else
                                     found[i][1][:MAX_PASSAGE] + " …") for i in best)


# ------------------------------------------------------------------ Help in the portal: the same pages, for people

def lookup(question: str, limit: int = 6) -> List[Dict[str, str]]:
    """For the portal's Help: the passages that bear on the question, best first, each whole ({where, text}), from
    the documentation people read (the models' map of the portal is left out: it is written for them)."""
    found = [(w, t) for w, t in passages() if not w.startswith("the portal's map")]
    return [{"where": found[i][0], "text": found[i][1]} for i in _best(question, limit, found)] if found else []


def topics() -> List[Dict[str, str]]:
    """For the portal's Help: each document's sections, to open one by one ({doc, title, where})."""
    out = []
    for name in DOCS:
        path = next((f / name for f in _folders() if (f / name).is_file()), None)
        if path is None:
            continue
        title = ""
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^(#{1,2})\s+(.*)", line)
            if m and m.group(1) == "#":
                title = m.group(2).strip()
            elif m:
                out.append({"doc": title or name, "title": m.group(2).strip(), "where": "%s › %s" % (
                    name, m.group(2).strip())})
    return out


def section(where: str) -> str:
    """A section of the documentation, whole (its subsections too), by "doc.md › Heading" as topics() gives it."""
    name, _, heading = str(where or "").partition(" › ")
    if name not in DOCS:
        return ""
    path = next((f / name for f in _folders() if (f / name).is_file()), None)
    if path is None:
        return ""
    out: List[str] = []
    inside = False
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(#{1,2})\s+(.*)", line)
        if m:
            if inside:
                break
            inside = len(m.group(1)) == 2 and m.group(2).strip() == heading.strip()
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()
