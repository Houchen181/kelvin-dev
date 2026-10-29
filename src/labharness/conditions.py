"""Conditions like "tc.T_sample < 10", used by interlocks and plan preconditions; and rules made of them
("abs(smu.I) > 1e-6 or tc.T_sample > 300") for a plan's until: and while:."""
from __future__ import annotations

import math
import operator
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

_COND = re.compile(r"^\s*([A-Za-z_][\w-]*)\.([A-Za-z_]\w*)\s*(<=|>=|==|!=|<|>)\s*(.+?)\s*$")
_TERM = re.compile(r"^\s*(abs\(\s*)?([A-Za-z_][\w-]*)\.([A-Za-z_]\w*)\s*(\))?\s*(<=|>=|==|!=|<|>)\s*(.+?)\s*$", re.I)
_OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
        "==": operator.eq, "!=": operator.ne}


@dataclass
class Condition:
    instr: str
    param: str
    op: str
    value: Any
    text: str

    @property
    def key(self) -> str:
        return "%s.%s" % (self.instr, self.param)

    def check(self, read: Callable[[str, str], Any]) -> Tuple[bool, Any]:
        actual = read(self.instr, self.param)
        a, b = actual, self.value
        if isinstance(a, str) and isinstance(b, str):
            a, b = a.lower(), b.lower()
        try:
            return bool(_OPS[self.op](a, b)), actual
        except TypeError:
            return False, actual


def _literal(raw: str) -> Any:
    s = raw.strip().strip("'\"")
    low = s.lower()
    if low in ("true", "on"):
        return True
    if low in ("false", "off"):
        return False
    try:
        return float(s)
    except ValueError:
        return s


def parse_condition(text: Any) -> Condition:
    m = _COND.match(str(text))
    if not m:
        raise ValueError("cannot parse condition %r (expected e.g. 'tc.T_sample < 10')" % text)
    instr, param, op, raw = m.groups()
    return Condition(instr, param, op, _literal(raw), str(text).strip())


@dataclass
class Term:
    """One comparison of a rule: a reading (or its size, abs(...)) against a value."""
    instr: str
    param: str
    op: str
    value: Any
    absolute: bool = False

    @property
    def key(self) -> str:
        return "%s.%s" % (self.instr, self.param)


@dataclass
class Rule:
    """Comparisons joined by and / or (and binds first), as a plan's until: and while: write them:
    "nvm.V < 1e-6", "abs(smu.I) > 1e-6 or tc.T_sample > 300". check() says whether it holds, or None when a reading
    it needs is not a number (NaN, nothing): the plan then ends that loop, as it can't tell (it fails closed)."""
    any_of: List[List[Term]]
    text: str
    keys: List[str] = field(default_factory=list)

    def check(self, read: Callable[[str, str], Any]) -> Optional[bool]:
        seen: Dict[str, Any] = {}
        unknown = False
        for group in self.any_of:
            verdicts = []
            for t in group:
                if t.key not in seen:
                    seen[t.key] = read(t.instr, t.param)
                verdicts.append(_compare(t, seen[t.key]))
                if verdicts[-1] is False:
                    break   # this alternative fails whatever the rest read
            if all(v is True for v in verdicts):
                return True
            unknown = unknown or False not in verdicts
        return None if unknown else False


def _compare(t: Term, a: Any) -> Optional[bool]:
    """One comparison; None when the reading can't be compared (not a number, NaN, nothing)."""
    b = t.value
    if isinstance(a, str) and isinstance(b, str) and t.op in ("==", "!="):
        return bool(_OPS[t.op](a.lower(), b.lower()))
    if isinstance(a, bool) and isinstance(b, bool) and t.op in ("==", "!="):
        return bool(_OPS[t.op](a, b))
    if isinstance(a, bool) or not isinstance(a, (int, float)) or not math.isfinite(a) or isinstance(b, (bool, str)):
        return None
    return bool(_OPS[t.op](abs(a) if t.absolute else a, b))


def parse_rule(text: Any) -> Rule:
    """A rule from its text; ValueError says what is wrong with it."""
    raw = " ".join(str(text if text is not None else "").split())
    if not raw:
        raise ValueError("a condition is empty (write it like 'tc.T_sample < 10')")
    groups: List[List[Term]] = []
    for alternative in re.split(r"\s+or\s+", raw, flags=re.I):
        terms = []
        for part in re.split(r"\s+and\s+", alternative, flags=re.I):
            m = _TERM.match(part)
            if not m or bool(m.group(1)) != bool(m.group(4)):
                raise ValueError("cannot read %r in %r (write comparisons like 'nvm.V < 1e-6' or 'abs(smu.I) > 1e-6', "
                                 "joined by and / or)" % (part, raw))
            _, instr, param, _, op, value = m.groups()
            terms.append(Term(instr, param, op, _literal(value), bool(m.group(1))))
        groups.append(terms)
    keys = list(dict.fromkeys(t.key for g in groups for t in g))
    return Rule(groups, raw, keys)
