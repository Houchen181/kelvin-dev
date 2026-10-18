"""Conditions like "tc.T_sample < 10", used by interlocks and plan preconditions."""
from __future__ import annotations

import operator
import re
from dataclasses import dataclass
from typing import Any, Callable, Tuple

_COND = re.compile(r"^\s*([A-Za-z_][\w-]*)\.([A-Za-z_]\w*)\s*(<=|>=|==|!=|<|>)\s*(.+?)\s*$")
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
