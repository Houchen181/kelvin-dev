"""A VI translated into a Kelvin experiment by the AI, and checked by Kelvin until nothing blocks it.

The model gets the VI's description (videsc), what each of its calls becomes (vimap), the lab's instruments, and
Kelvin's rules for an experiment (data/labview/translate.md: the experiment API, the layout, every line tagged with
the VI node it comes from). It answers with the experiment and its notes (the inputs it made from controls, what it
left out and why, the VI's file columns and what records them, mappings it proposes). Kelvin's fixed checks
(vicheck) send what fails back, for at most ROUNDS answers; the second opinion reads only the last. Nothing here
touches the lab: the checks rehearse on simulated copies of it.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import vicheck, videsc, vimap

Ask = Callable[[str, str], Tuple[str, str]]   # (system, prompt) -> (answer, model key), as autocard.Ask
MAX_PROMPT = 400_000   # characters of description and context; above it the model is never called
ROUNDS = 3             # the first answer and at most two repairs
SYSTEM_FILE = Path(__file__).parent / "data" / "labview" / "translate.md"
_PYTHON = re.compile(r"```(?:python|py)[ \t]*\n(.*?)```", re.S)
_OPEN = re.compile(r"```(?:python|py)[ \t]*\n", re.S)
_JSON = re.compile(r"```json[ \t]*\n(.*?)```", re.S)


@dataclass
class Translation:
    source: str
    notes: Dict[str, Any]
    report: vicheck.Report
    rounds: int
    model: str
    vi: Dict[str, Any] = field(default_factory=dict)        # {"path", "name", "sha256", "labview"}
    mappings: Dict[str, Any] = field(default_factory=dict)  # call -> what it becomes (vimap.Mapping, as a dict)


def system() -> str:
    """Kelvin's rules for the model (data/labview/translate.md)."""
    return SYSTEM_FILE.read_text(encoding="utf-8")


def lab_text(lab: Any) -> str:
    """The lab's instruments for the model: each by its name, with its card, address, parameters and actions."""
    lines = []
    for name, inst in lab.instruments.items():
        card = inst.card
        params = []
        for p in card.params.values():
            what = "drive" if p.is_drive else "setting" if p.set_cmd is not None else "reading"
            limits = inst.limits(p.name) if p.is_drive else None
            params.append("%s (%s%s%s)" % (p.name, what, ", " + p.unit if p.unit else "",
                                           ", limits [%s, %s]" % limits if limits else ""))
        actions = ["%s%s" % (a, " (drives the sample)" if getattr(spec, "drives", False) else "")
                   for a, spec in card.actions.items()]
        lines.append("%s: %s (%s) at %s\n  parameters: %s%s" % (
            name, card.id, card.model, getattr(inst.config, "address", "?"), ", ".join(params) or "none",
            "\n  actions: " + ", ".join(actions) if actions else ""))
    return "\n".join(lines) or "(the lab has no instruments)"


def prompt(desc_text: str, mapping_text: str, lab_text: str) -> str:
    return ("# The VI\n\n%s\n\n# What each of its calls becomes in Kelvin\n\n%s\n\n# The lab's instruments\n\n%s\n\n"
            "Translate the VI into one Kelvin experiment, as your rules say." % (
                desc_text, mapping_text or "(it calls nothing)", lab_text))


def parse(answer: str) -> Tuple[str, Dict[str, Any]]:
    """The experiment (the first ```python block; a block cut off at the end counts, to its end) and its notes (the
    first ```json block; {} when there is none or it isn't JSON). ValueError, in words, when there is no code."""
    answer = answer or ""
    blocks = _PYTHON.findall(answer)
    if blocks:
        source = blocks[0]
    else:
        opened = _OPEN.search(answer)
        if opened is None:
            raise ValueError("the answer has no ```python block: answer with the whole experiment in one ```python "
                             "block, then the notes in one ```json block")
        source = answer[opened.end():]   # cut off before its block closed: the checks say where it breaks
    notes: Dict[str, Any] = {}
    m = _JSON.search(answer)
    if m:
        try:
            found = json.loads(m.group(1))
            notes = found if isinstance(found, dict) else {}
        except ValueError:
            notes = {}
    return source.rstrip() + "\n", notes


def _desc_text(desc: Dict[str, Any]) -> str:
    try:
        return videsc.render_text(desc)
    except Exception:   # a description not as videsc makes them: the model gets it as it is
        return json.dumps(desc, default=str, indent=1)


def _needs_repair(report: vicheck.Report) -> bool:
    return bool(report.blocking) or any(f.kind == "untranslated" for f in report.findings) or \
        any(not r.get("ok") for r in report.rehearsals.values())


def _repair(first: str, source: str, problems: List[str]) -> str:
    previous = "```python\n%s```" % source if source else "(no experiment)"
    return "%s\n\n# Your previous answer\n\n%s\n\n# What Kelvin's checks found\n\n%s\n\nAnswer again with the whole " \
           "corrected experiment in one ```python block and its notes in one ```json block." % (
               first, previous, "\n".join("- " + p for p in problems))


def translate(desc: Dict[str, Any], lab: Any, ask: Ask, table: vimap.Table, folder: Path,
              twin_of: Callable[[], Any], plan_text: str = "", review_ask: Optional[Ask] = None, real: Any = None,
              prepare: Optional[Callable[[Path, str], Any]] = None) -> Translation:
    """The VI translated and checked: the last attempt, with its report (ok_to_keep: nothing blocks). folder: where
    each attempt is written (experiment.py) to be rehearsed. ValueError, before the model hears of it, for a
    description larger than MAX_PROMPT."""
    desc_text = _desc_text(desc)
    cards = list({inst.card.id: inst.card for inst in lab.instruments.values()}.values())
    mappings = vimap.map_description(desc, table, cards)
    first = prompt(desc_text, vimap.text(mappings), lab_text(lab))
    if len(first) > MAX_PROMPT:
        raise ValueError("the description is %d characters, more than Kelvin sends in one go (%d): translate its "
                         "sub-VIs first" % (len(first), MAX_PROMPT))
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "experiment.py"
    rules = system()
    text, source, notes, model = first, "", {}, ""
    report = vicheck.Report()
    rounds = 0
    for rounds in range(1, ROUNDS + 1):
        answer, model = ask(rules, text)
        try:
            source, notes = parse(answer)
        except ValueError as e:
            source, notes = "", {}
            report = vicheck.Report([vicheck.Finding("syntax", str(e), True)])
            text = _repair(first, "", [str(e)])
            continue
        path.write_text(source, encoding="utf-8")
        report = vicheck.check(source, desc, lab, mappings, notes, path, twin_of, plan_text, real=real,
                               prepare=prepare, desc_text=desc_text)
        if not _needs_repair(report):
            break
        problems = [f.text for f in report.findings if f.blocks or f.kind in ("untranslated", "rehearsal")]
        text = _repair(first, source, problems)
    if review_ask is not None and source and not any(f.kind in ("syntax", "api") for f in report.blocking):
        report.findings += vicheck.review(review_ask, desc_text, source)
    vi = {k: desc.get(k) for k in ("path", "name", "sha256", "labview")}
    return Translation(source, notes, report, rounds, model, vi, {k: asdict(m) for k, m in mappings.items()})
