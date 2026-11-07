"""The checks a VI's translation into a Kelvin experiment must pass: fixed code, no model (the second opinion is a
model, and only ever a warning).

1. Static rules on its text: valid Python; imports only from ALLOWED_IMPORTS; no call that reaches past the
   experiment API (open, exec, getattr ...); it uses kelvin; per lab instrument, it drives only what the VI's calls
   map to on that instrument's card (vimap); its inputs keep the VI's saved values; every VI element a person would
   miss (a control, an indicator, an instrument call, a wait, a loop's stop condition) is named by a "# VI: <id>" tag
   or listed as not translated, and Kelvin lists the ones it isn't.
2. A rehearsal of each action on a simulated copy of the lab (scriptrun.rehearse): it ends, or stops on Stop, keeps
   to the limits and reaches nothing past the simulated lab; its recorded columns are the VI's.
3. Where the VI was also translated into a plan (viscan), the commands each sends on the simulated lab, compared in
   order.
4. A second model's review.

What blocks keeping a translation is a safety failure (Finding.blocks): a path around the gate, a forbidden
import or call, a drive the VI never touched, an input default changed, a rehearsal that breaks a limit, reaches
past the simulated lab or never ends; and a check a person stopped (their Stop during a rehearsal), which isn't
passed until it runs again. The rest are warnings a person accepts by keeping it.
"""
from __future__ import annotations

import ast
import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from . import experiment, scriptrun, videsc
from .vimap import Mapping

Ask = Callable[[str, str], Tuple[str, str]]   # (system, prompt) -> (answer, model key), as autocard.Ask
REHEARSE_S = scriptrun.REHEARSE_S
ALLOWED_IMPORTS = ("kelvin", "math", "cmath", "time", "datetime", "statistics", "random", "itertools", "functools",
                   "collections", "dataclasses", "enum", "typing", "json", "re", "string", "threading", "queue",
                   "numpy", "scipy")
FORBIDDEN_CALLS = ("open", "exec", "eval", "compile", "__import__", "input", "breakpoint", "globals", "vars",
                   "getattr", "setattr", "delattr")
# names that reach Python's machinery past the experiment API (__builtins__.__dict__["open"])
DUNDERS = ("__builtins__", "__dict__", "__class__", "__subclasses__", "__globals__", "__code__", "__getattribute__",
           "__import__", "__loader__", "__spec__", "__bases__", "__mro__", "__closure__")
# what reaches native code or files from inside an allowed package (numpy.ctypeslib.load_library loads a DLL)
FORBIDDEN_ATTRS = ("ctypeslib", "ctypes", "_ctypes", "cffi", "load_library", "f2py", "distutils", "DataSource",
                   "LowLevelCallable")
WAITS = ("Wait (ms)", "Wait Until Next ms Multiple", "Time Delay")
# a rehearsal's refusal that is not a safety failure: an instrument the lab doesn't have, or one switched off or not
# connected now (lab.away); every other refusal (a limit, a guard, an interlock, hands on the sample, a draft card)
# blocks keeping the translation
_NOT_THERE = re.compile(r"^refused: (the lab has no instrument |[\w-]+ is (not connected|switched off|"
                        r"disconnected from Kelvin))")
_TAG = re.compile(r"#\s*VI:\s*([A-Za-z0-9_.]+(?:\s*,\s*[A-Za-z0-9_.]+)*)")
_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


@dataclass
class Finding:
    kind: str        # "syntax" | "import" | "call" | "api" | "drive" | "default" | "instrument" | "untranslated"
                     # | "rehearsal" | "columns" | "trace" | "review"
    text: str        # Kelvin's words, for the person and for the model's next round
    blocks: bool     # a safety failure: keeping the translation is refused while it stands
    node: str = ""   # the VI node it concerns, when there is one


@dataclass
class Report:
    findings: List[Finding] = field(default_factory=list)
    rehearsals: Dict[str, Dict[str, Any]] = field(default_factory=dict)   # action ("" for none) -> what it did
    trace: str = ""   # the comparison with the VI's own scan, in words ("": none)

    @property
    def blocking(self) -> List[Finding]:
        return [f for f in self.findings if f.blocks]

    @property
    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if not f.blocks]

    @property
    def ok_to_keep(self) -> bool:
        return not self.blocking

    def text(self) -> str:
        lines: List[str] = []
        if self.blocking:
            lines.append("What keeps it from being kept:")
            lines += ["- " + f.text for f in self.blocking]
        for action, r in self.rehearsals.items():
            lines.append("%s: %s" % (action or "the experiment", r.get("message", "")))
        if self.trace:
            lines.append(self.trace)
        if self.warnings:
            lines.append("Warnings (a person accepts them by keeping it):")
            lines += ["- " + f.text for f in self.warnings]
        return "\n".join(lines) or "Every check passed."

    def to_json(self) -> Dict[str, Any]:
        return {"ok_to_keep": self.ok_to_keep, "findings": [asdict(f) for f in self.findings],
                "rehearsals": self.rehearsals, "trace": self.trace}


def tags(source: str) -> Dict[str, List[int]]:
    """VI node id -> the lines whose "# VI: <id>, <id>" tag names it."""
    out: Dict[str, List[int]] = {}
    for n, line in enumerate(source.splitlines(), 1):
        m = _TAG.search(line)
        if m:
            for node in m.group(1).split(","):
                out.setdefault(node.strip(), []).append(n)
    return out


def _nodes(diagram: Optional[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for n in (diagram or {}).get("nodes", []):
        yield n
        for f in n.get("frames", []):
            yield from _nodes(f)


def _accountable(desc: Dict[str, Any]) -> List[Tuple[str, str]]:
    """The VI elements a translation must name or list as not translated: (id, what it is). The lab's own sub-VIs'
    nodes count, under the ids the description's text gives them (s1.n12); their panels become a function's
    arguments, so they don't."""
    out = [(c["id"], '%s "%s"' % ("indicator" if c.get("indicator") else "control", c.get("name", "")))
           for c in desc.get("panel") or [] if not videsc._is_error(c)]
    descs = [("", desc)] + [("s%d." % i, sub) for i, sub in enumerate((desc.get("subvis") or {}).values(), 1)]
    for prefix, d in descs:
        for n in _nodes(d.get("diagram")):
            k = n.get("kind")
            if k == "call":
                what = 'call "%s"' % n.get("call", "")
            elif k in ("property", "invoke"):
                what = "%s %s" % (k, n.get("of", ""))
            elif k == "call_library":
                what = 'DLL call "%s"' % n.get("function", "")
            elif k == "prim" and n.get("name") in WAITS:
                what = n["name"]
            elif k == "loop" and n.get("loop") == "while":
                what = "while loop (its stop condition)"
            else:
                continue
            out.append((prefix + n["id"], what))
    return out


def _call_name(node: ast.Call) -> str:
    f = node.func
    return f.id if isinstance(f, ast.Name) else ""


_UNKNOWN = object()


def _module_values(tree: ast.Module) -> Dict[str, List[Any]]:
    """Each module-level name's assigned values (_UNKNOWN where it isn't a plain value)."""
    out: Dict[str, List[Any]] = {}
    for node in tree.body:
        targets, value = [], None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        for t in targets:
            if isinstance(t, ast.Name):
                try:
                    v = ast.literal_eval(value)
                except (ValueError, SyntaxError, TypeError):
                    v = _UNKNOWN
                out.setdefault(t.id, []).append(v)
    return out


def _tagged_inputs(tree: ast.Module, source: str, panel: Dict[str, Any]) -> Dict[str, str]:
    """Module-level names whose line's "# VI:" tag names exactly one of the VI's controls: inputs made from it."""
    by_line: Dict[int, List[str]] = {}
    for node_id, lines in tags(source).items():
        if node_id in panel and not panel[node_id].get("indicator"):
            for n in lines:
                by_line.setdefault(n, []).append(node_id)
    out: Dict[str, str] = {}
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] \
            if isinstance(node, ast.AnnAssign) else []
        ids = by_line.get(getattr(node, "lineno", 0), [])
        if len(ids) == 1:
            for t in targets:
                if isinstance(t, ast.Name):
                    out[t.id] = ids[0]
    return out


def _same_value(v: Any, control: Dict[str, Any]) -> bool:
    d = control.get("default")
    choices = control.get("choices") or []
    if choices and isinstance(d, int) and 0 <= d < len(choices) and v == choices[d]:
        return True   # a ring's saved value is its index; the input may name the choice
    if isinstance(v, (int, float)) and isinstance(d, (int, float)) and not isinstance(v, str):
        return math.isclose(float(v), float(d), rel_tol=1e-12, abs_tol=1e-15)
    return v == d


def static(source: str, desc: Dict[str, Any], mappings: Dict[str, Mapping], lab: Any,
           notes: Dict[str, Any]) -> List[Finding]:
    """The rules on a translation's text (see the module's docstring), without running it."""
    out: List[Finding] = []
    said: Set[str] = set()

    def add(kind: str, text: str, blocks: bool, node: str = "") -> None:
        if text not in said:
            said.add(text)
            out.append(Finding(kind, text, blocks, node))

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [Finding("syntax", "it isn't valid Python: line %s: %s" % (e.lineno, e.msg), True)]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if top not in ALLOWED_IMPORTS or any(p in FORBIDDEN_ATTRS for p in a.name.split(".")):
                    add("import", "it imports %s, which a translated experiment may not (only %s, and nothing in "
                                  "them that reaches native code)" % (a.name, ", ".join(ALLOWED_IMPORTS)), True)
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            names = (node.module or "").split(".") + [a.name for a in node.names]
            if node.level:
                add("import", "it imports from the folder it is in (from . import), which a translated experiment "
                              "may not", True)
            elif top not in ALLOWED_IMPORTS or any(p in FORBIDDEN_ATTRS for p in names):
                add("import", "it imports %s from %s, which a translated experiment may not (only %s, and nothing in "
                              "them that reaches native code)" % (", ".join(a.name for a in node.names), node.module,
                                                                  ", ".join(ALLOWED_IMPORTS)), True)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in FORBIDDEN_CALLS:
            add("call", "it uses %s, which a translated experiment may not: it reaches instruments only through "
                        "kelvin.lab, and what it keeps goes to record()" % node.id, True)
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            add("call", "it uses %s, which reaches native code past the gate" % node.attr, True)
        elif isinstance(node, ast.Name) and node.id in DUNDERS or isinstance(node, ast.Attribute) and \
                node.attr in DUNDERS:
            add("call", "it uses %s, which reaches past the experiment API" %
                (node.id if isinstance(node, ast.Name) else node.attr), True)
    if not experiment.is_experiment(source):
        add("api", "it doesn't use kelvin: a translated experiment reaches the instruments only through kelvin.lab",
            True)
        return out
    exp = experiment.read(source)
    for p in exp.problems:
        add("api", p, True)

    # what it drives, per lab instrument, against what the VI's calls map to on that instrument's card
    allowed_sets: Dict[str, Set[str]] = {}
    allowed_does: Dict[str, Set[str]] = {}
    for m in mappings.values():
        if m.becomes == "card":
            allowed_sets.setdefault(m.card, set()).update(m.sets)
            allowed_does.setdefault(m.card, set()).update(m.does)
    uses = experiment.uses(exp)

    def instrument(key: str, kind: str) -> Optional[Tuple[Any, str]]:
        name, _, part = key.partition(".")
        inst = lab.instruments.get(name)
        if inst is None:
            add("instrument", "it uses %s, but the lab has no instrument called %s" % (key, name), False)
            return None
        card = inst.card
        where = card.actions if kind == "action" else card.params
        if part not in where:
            add("instrument", "it uses %s, but %s's card (%s) has no %s %s" % (key, name, card.id, kind, part), False)
            return None
        return card, part

    for key in uses.sets:
        found = instrument(key, "parameter")
        if found is None:
            continue
        card, part = found
        if part in allowed_sets.get(card.id, set()):
            continue
        if card.params[part].is_drive:
            add("drive", "it sets %s, a drive the VI never sets (none of its calls maps to %s's %s)"
                % (key, card.id, part), True)
        else:
            add("drive", "it sets %s, a setting none of the VI's calls set" % key, False)
    for key in uses.gets:
        instrument(key, "parameter")
    for key in uses.acts:
        found = instrument(key, "action")
        if found is None:
            continue
        card, part = found
        if getattr(card.actions[part], "drives", False) and part not in allowed_does.get(card.id, set()):
            add("drive", "it does %s, which drives the sample, and the VI never does" % key, True)
    for name, commands in uses.raw.items():
        inst = lab.instruments.get(name)
        if inst is None:
            add("instrument", "it sends commands to %s, but the lab has no instrument called %s" % (name, name), False)
            continue
        for cmd in commands:
            if cmd is None:   # a raw command it works out as it runs: Kelvin can't say what it would change
                add("unread", "it sends %s a raw command it works out as it runs: a translation writes its raw commands "
                              "out, or uses the card's parameters" % name, True)
                continue
            spec, _ = scriptrun._setter(inst.card, cmd)
            if spec is not None and spec.is_drive and spec.name not in allowed_sets.get(inst.card.id, set()):
                add("drive", "it sends %r to %s, which sets %s, a drive the VI never sets" % (cmd, name, spec.name),
                    True)
    if uses.unread:   # the translator's rules write every key out: one worked out as it runs could be any drive
        add("unread", "Kelvin can't read ahead which instrument or parameter these use, so it can't check them against "
                      "the VI: %s; write the key out, as \"smu.V\"" % "; ".join(uses.unread[:5]), True)

    # its inputs keep the VI's saved values: those its notes name, and each module-level value tagged with one control
    panel = {c.get("id"): c for c in desc.get("panel") or []}
    values = _module_values(tree)
    inputs = dict(_tagged_inputs(tree, source, panel))
    inputs.update(notes.get("inputs") or {})
    for name, cid in inputs.items():
        control = panel.get(cid)
        if control is None:
            add("default", "its input %s names %s, which isn't on the VI's panel" % (name, cid), False)
            continue
        if control.get("default") is None:
            continue
        for v in values.get(name, []):
            if v is not _UNKNOWN and not _same_value(v, control):
                add("default", "its input %s is %r, but the VI's control \"%s\" (%s) is saved as %r: a translation "
                               "keeps the VI's values" % (name, v, control.get("name", ""), cid, control["default"]),
                    True, cid)

    # every element a person would miss is named or listed
    tagged = set(tags(source))
    listed = {str(x.get("id")) for x in notes.get("not_translated") or [] if isinstance(x, dict)}
    for node, what in _accountable(desc):
        if node not in tagged and node not in listed:
            add("untranslated", "not mentioned by the translation: %s %s" % (node, what), False, node)
    return out


def _is_safety(problem: str) -> bool:
    """Does a rehearsal's problem block keeping the translation? Every refusal does (its process's, or the gate's),
    except one of an instrument that isn't there (the static check says so), never by how the refusal is worded."""
    if problem.startswith("its process refused"):
        return True
    return problem.startswith("refused") and not _NOT_THERE.match(problem)


def _default_prepare(lab: Any) -> Callable[[Path, str], Any]:
    return lambda path, action: scriptrun.prepare(lab, str(path), [path.parent], action=action)


STOPPED = "stopped by a person"   # scriptrun.Rehearsal.ended for a rehearsal a person's Stop ended


def stopped(report: Report) -> bool:
    """A person's Stop ended the check's rehearsals: it isn't passed, and nothing in the code is to be repaired for it
    (vitranslate stops there)."""
    return any(r.get("ended") == STOPPED for r in report.rehearsals.values())


def rehearse_actions(lab: Any, path: Path, source: str, twin_of: Callable[[], Any],
                     prepare: Optional[Callable[[Path, str], Any]] = None,
                     real: Any = None, since: Optional[int] = None) -> Tuple[Dict[str, Any], List[Finding]]:
    """Each action (the whole file when it has none) rehearsed in file order on one simulated copy of the lab, made by
    twin_of, as a person runs them one after another (initialize, then scan): what each did, and what blocks or
    warns. A person's Stop on the lab (real, the lab's gate; since: its stops when the check began) ends the
    rehearsals: the translation isn't checked then, which blocks keeping it until it is checked again."""
    prepare = prepare or _default_prepare(lab)
    out: Dict[str, Any] = {}
    found: List[Finding] = []
    if real is not None and since is None:
        since = real.stop_generation
    try:
        twin = twin_of()
    except Exception as e:   # the lab couldn't be copied now: nothing is rehearsed, and it says so
        return {}, [Finding("rehearsal", "not rehearsed: %s" % e, False)]
    for action in experiment.read(source).actions or [""]:
        name = action or "the experiment"
        try:
            prep = prepare(Path(path), action)
        except Exception as e:   # not a safety failure: it isn't prepared to run (the run would say the same)
            out[action] = {"ok": False, "ended": "not rehearsed", "message": "not rehearsed: %s" % e,
                           "problems": [str(e)], "columns": {}}
            found.append(Finding("rehearsal", "%s isn't rehearsed: %s" % (name, e), False))
            continue
        try:
            r = scriptrun.rehearse(lab, prep, seconds=REHEARSE_S, twin=twin, real=real, since=since)
        except scriptrun.ScriptError as e:
            out[action] = {"ok": False, "ended": "not rehearsed", "message": str(e), "problems": [str(e)],
                           "columns": {}}
            found.append(Finding("rehearsal", "it reaches past the simulated lab: %s" % e, True))
            continue
        out[action] = {"ok": r.ok, "ended": r.ended, "message": r.message, "problems": list(r.problems),
                       "columns": r.columns, "asked": list(r.asked)}
        if r.ended == STOPPED:   # never a passed check: the rest isn't rehearsed, and keeping it waits for a check
            found.append(Finding("rehearsal", "a person pressed Stop during %s's rehearsal, so it isn't checked: "
                                              "check it again" % name, True))
            found += [Finding("rehearsal", "%s, in its rehearsal: %s" % (name, p), True)
                      for p in r.problems if _is_safety(p)]
            break
        if r.ended == "not on Stop":
            found.append(Finding("rehearsal", "%s never ends, not even on Stop" % name, True))
        for p in r.problems:
            if _is_safety(p):
                found.append(Finding("rehearsal", "%s, in its rehearsal: %s" % (name, p), True))
            elif r.ended != "not on Stop":
                found.append(Finding("rehearsal", "%s failed in its rehearsal: %s" % (name, p), False))
        for q in r.asked:
            found.append(Finding("rehearsal", "%s: a person would be asked: %s" % (name, " ".join(q.split())), False))
    return out, found


def _norm(command: str) -> str:
    text = " ".join(str(command).split()).upper()
    return _NUMBER.sub(lambda m: format(float(m.group(0)), ".9g"), text)


def _wire(twin: Any) -> List[Tuple[str, str]]:
    """Every write to the simulated instruments, in order, from now on."""
    sent: List[Tuple[str, str]] = []
    for name, inst in twin.instruments.items():
        backend = inst.backend
        write = backend.write

        def recorded(command: str, _write: Any = write, _name: str = name) -> Any:
            sent.append((_name, command))
            return _write(command)
        backend.write = recorded
    return sent


def compare_trace(lab: Any, plan_text: str, path: Path, action: str, twin_of: Callable[[], Any],
                  prepare: Optional[Callable[[Path, str], Any]] = None) -> Tuple[str, List[Finding]]:
    """The commands the VI's scan (its viscan plan) and the experiment's action send on the simulated lab, compared in
    order (writes only: each side reads back and checks errors its own way). Words, and a warning when they differ."""
    from .engine import RunEngine
    from .eventlog import EventLog, RunStore
    from .gate import SafetyGate, SimulationApprover
    from .plans import load_plan
    twin = twin_of()
    planned = _wire(twin)
    gate = SafetyGate(twin, SimulationApprover(), EventLog(None, twin.clock), operator="dry-run")
    engine = RunEngine(gate, RunStore(None))
    engine.analyze = False
    try:
        report = engine.run(load_plan(plan_text), dry_run=False)
    except Exception as e:
        return "", [Finding("trace", "the VI's scan didn't run on the simulated lab: %s" % e, False)]
    if report.status != "done":
        return "", [Finding("trace", "the VI's scan didn't run on the simulated lab: %s" % report.message, False)]
    sent: List[Tuple[str, str]] = []
    try:
        scriptrun.rehearse(lab, (prepare or _default_prepare(lab))(Path(path), action), seconds=REHEARSE_S,
                           twin=twin_of(), wire=sent)
    except Exception as e:
        return "", [Finding("trace", "the experiment's commands couldn't be compared: %s" % e, False)]
    a = [(n, _norm(c)) for n, c in planned]
    b = [(n, _norm(c)) for n, c in sent]
    if a == b:
        return "matches the VI's scan: %d commands, in the same order" % len(a), []
    i = next((k for k, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    said = lambda xs: "'%s: %s'" % xs[i] if i < len(xs) else "nothing more"   # noqa: E731
    text = "differs from the VI's scan at command %d of %d: the scan sends %s, the experiment %s" % (
        i + 1, max(len(a), len(b)), said(a), said(b))
    return text, [Finding("trace", text, False)]


REVIEW_SYSTEM = """You review a translation of a LabVIEW VI into a Kelvin experiment (Python that reaches the lab's \
instruments only through kelvin.lab). Compare the VI's description with the Python: wrong values, a stop condition \
or a comparison inverted, a loop that runs a different number of times, a lost instrument call, a wait or a recorded \
column, a mode of the VI left out. Say only what you can point to. Answer one JSON object: {"findings": [{"problem": \
"...", "severity": "high|medium|low", "node": "<the VI node id, if one>"}]} (an empty list when you find nothing)."""


def _json_in(text: str) -> Optional[Dict[str, Any]]:
    """The first balanced {...} in a text that is a JSON object."""
    for start in [i for i, ch in enumerate(text) if ch == "{"]:
        depth, quoted, escaped = 0, False, False
        for j in range(start, len(text)):
            ch = text[j]
            if quoted:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    quoted = False
            elif ch == '"':
                quoted = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        found = json.loads(text[start:j + 1])
                    except ValueError:
                        break
                    if isinstance(found, dict):
                        return found
                    break
    return None


def review(ask: Ask, desc_text: str, source: str) -> List[Finding]:
    """A second model's reading of the translation against the VI's description: warnings, never a block."""
    prompt = "The VI's description:\n%s\n\nThe Kelvin experiment translated from it:\n```python\n%s\n```" % (
        desc_text, source)
    try:
        answer, model = ask(REVIEW_SYSTEM, prompt)
    except Exception as e:
        return [Finding("review", "the second opinion failed: %s" % e, False)]
    data = _json_in(answer or "")
    if data is None:
        return [Finding("review", "second opinion: " + " ".join(answer.split())[:400], False)] if answer.strip() else []
    out = []
    for f in data.get("findings") or []:
        if isinstance(f, dict) and f.get("problem"):
            out.append(Finding("review", "second opinion (%s): %s" % (f.get("severity") or "unsure", f["problem"]),
                               False, str(f.get("node") or "")))
    return out


def check(source: str, desc: Dict[str, Any], lab: Any, mappings: Dict[str, Mapping], notes: Dict[str, Any],
          path: Path, twin_of: Callable[[], Any], plan_text: str = "", review_ask: Optional[Ask] = None,
          real: Any = None, prepare: Optional[Callable[[Path, str], Any]] = None, desc_text: str = "") -> Report:
    """Every check on a translation saved at path. The rehearsals, columns, trace and review run only when the static
    rules found nothing that blocks (a file that reaches past the gate never runs, even on the simulated lab). A
    person's Stop on the lab (real) during the rehearsals ends the check there: it is not passed."""
    since = real.stop_generation if real is not None else None
    report = Report(static(source, desc, mappings, lab, notes))
    if report.blocking:
        return report
    report.rehearsals, found = rehearse_actions(lab, Path(path), source, twin_of, prepare, real, since)
    report.findings += found
    if stopped(report):
        return report   # no trace, columns or second opinion from a check a person cut short
    have: Set[str] = set()
    for r in report.rehearsals.values():
        cols = r.get("columns") or {}
        have |= {str(c.get("key")) for c in list(cols.get("axes") or []) + list(cols.get("measures") or [])}
    for old, new in (notes.get("columns") or {}).items():
        if str(new) not in have:
            report.findings.append(Finding("columns", "the VI's column %s should be recorded as %s, but no action's "
                                                      "rehearsal recorded it" % (old, new), False))
    if plan_text:
        exp = experiment.read(source)
        main = exp.main or (exp.actions[0] if exp.actions else "")
        report.trace, found = compare_trace(lab, plan_text, Path(path), main, twin_of, prepare)
        report.findings += found
    if review_ask is not None:
        if not desc_text:
            try:
                desc_text = videsc.render_text(desc)
            except Exception:
                desc_text = json.dumps(desc, default=str)[:200000]
        report.findings += review(review_ask, desc_text, source)
    return report
