"""Units through arithmetic, for the Analysis pane (workbench.py): the unit of an expression of a run's columns
(lockin.X / smu.I is in Ω, lockin.X / 1e-6 in µV, deriv(lockin.X, magnet.B) in V/T), of a step's result (d/dx, ∫ dx,
scale) and of the parameters of a model written as an expression (R0*(1 + a*x**2): R0 in y's unit, a in 1/x²).

A unit is a product of named units with exponents, times a power of ten; V/A is written Ω again, A/V S and V·A W. A
number in an expression is a pure number, except that multiplying or dividing by a power of ten moves the prefix
(lockin.X / 1e-6 is in µV) and 100 times a ratio is in %. Where a number may carry a unit of its own (lockin.X / 2e-8,
the volts over a current), the unit is not known: None, never a guess. "" is said only of what has no unit (a ratio, a
normalized curve, a logarithm)."""
from __future__ import annotations

import ast
import math
import re
from fractions import Fraction
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import expr

PREFIX = {"f": -15, "p": -12, "n": -9, "µ": -6, "m": -3, "k": 3, "M": 6, "G": 9, "T": 12}
_PREFIX_OF = {v: k for k, v in PREFIX.items()}
BASES = {"V", "A", "Hz", "W", "s", "Ω", "T", "K", "F", "H", "S", "m", "g", "J", "N", "Pa", "Wb", "eV", "mol"}
_SPELLED = {"ohm": "Ω", "Ohm": "Ω", "ohms": "Ω", "Ohms": "Ω", "Ω": "Ω"}
_SUPER = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+")
_SUPER_OF = str.maketrans("0123456789-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻")
# the physical constants an expression may use (expr.CONSTANTS), with their units
CONSTANTS = {"kB": "J/K", "qe": "C", "h": "J·s", "hbar": "J·s", "muB": "J/T", "mu0": "H/m", "eps0": "F/m",
             "c": "m/s", "me": "kg", "G0": "S", "RK": "Ω", "Phi0": "Wb"}
_ELECTRIC = {"Ω": {"V": 1, "A": -1}, "S": {"A": 1, "V": -1}, "W": {"V": 1, "A": 1}}


class Unit:
    """10^ten times a product of named units (prefixed ones, mV, are names of their own), each with its exponent."""

    __slots__ = ("atoms", "ten")

    def __init__(self, atoms: Optional[Dict[str, Any]] = None, ten: Any = 0):
        self.atoms = {k: Fraction(e) for k, e in (atoms or {}).items() if e}
        self.ten = Fraction(ten)

    def __mul__(self, other: "Unit") -> "Unit":
        atoms = dict(self.atoms)
        for k, e in other.atoms.items():
            atoms[k] = atoms.get(k, Fraction(0)) + e
        return Unit(atoms, self.ten + other.ten)

    def __truediv__(self, other: "Unit") -> "Unit":
        return self * other ** -1

    def __pow__(self, n: Any) -> "Unit":
        return Unit({k: e * Fraction(n) for k, e in self.atoms.items()}, self.ten * Fraction(n))

    def __eq__(self, other: Any) -> bool:   # Ω is V/A
        return isinstance(other, Unit) and _expanded(self.atoms) == _expanded(other.atoms) and self.ten == other.ten

    def __hash__(self) -> int:
        return hash((tuple(sorted(_expanded(self.atoms).items())), self.ten))

    @property
    def plain(self) -> bool:
        """No unit at all: a pure number."""
        return not self.atoms and not self.ten

    def text(self) -> Optional[str]:
        """As a physicist writes it (µV, V/T², Ω, %); None when no prefix says its power of ten."""
        atoms = self.atoms
        shorter = _electric(_expanded(atoms))   # V/A is Ω, Ω·A is V: when that leaves fewer units
        if len(shorter) < len(atoms):
            atoms = shorter
        ten = self.ten
        if ten:
            moved = _absorb(atoms, ten)
            if moved is None:
                if atoms:
                    return None
                return "%" if ten == -2 else "ppm" if ten == -6 else ""   # still a ratio, whatever the number
            atoms = moved
        return _written(atoms)


ONE = Unit()


def _expanded(atoms: Dict[str, Fraction]) -> Dict[str, Fraction]:
    """Ω, S and W written as V and A."""
    out: Dict[str, Fraction] = {}
    for k, e in atoms.items():
        for name, f in (_ELECTRIC[k].items() if k in _ELECTRIC else [(k, 1)]):
            out[name] = out.get(name, Fraction(0)) + e * f
    return {k: v for k, v in out.items() if v}


def _electric(atoms: Dict[str, Fraction]) -> Dict[str, Fraction]:
    """V and A together again, where V stood: as Ω (or S) when one is over the other, as W when they are a product
    and nothing of either is left."""
    v, a = atoms.get("V", Fraction(0)), atoms.get("A", Fraction(0))
    if not v or not a or (v > 0) == (a > 0) and (v != a or v < 0):
        return dict(atoms)
    if (v > 0) == (a > 0):
        name, k, used_v, used_a = "W", v, v, a
    else:
        n = min(abs(v), abs(a))
        name, k, used_v, used_a = ("Ω", n, n, -n) if v > 0 else ("S", n, -n, n)
    out: Dict[str, Fraction] = {}
    for key, e in atoms.items():
        if key == "V":
            out[name] = k
            if e - used_v:
                out["V"] = e - used_v
        elif key == "A":
            if e - used_a:
                out["A"] = e - used_a
        else:
            out[key] = e
    return out


def split(name: str) -> Optional[Tuple[int, str]]:
    """A unit's name as (its prefix's power of ten, the unit it prefixes): mV -> (-3, V); None for one that takes
    no prefix (counts, dBm, %)."""
    if name in BASES:
        return 0, name
    if len(name) > 1 and name[0] in PREFIX and name[1:] in BASES:
        return PREFIX[name[0]], name[1:]
    if name == "cm":
        return -2, "m"
    return None


def _absorb(atoms: Dict[str, Fraction], ten: Fraction) -> Optional[Dict[str, Fraction]]:
    """A power of ten taken into the prefix of the first unit that can take it (µV, 1/ms), or None."""
    for key, e in atoms.items():
        if e not in (1, -1):
            continue
        parts = split(key)
        if parts is None:
            continue
        p = parts[0] + (ten if e == 1 else -ten)
        if p != int(p) or (p and int(p) not in _PREFIX_OF):
            continue
        name = (_PREFIX_OF[int(p)] if p else "") + parts[1]
        if name in atoms and name != key:
            continue
        return {(name if k == key else k): v for k, v in atoms.items()}
    return None


def _power(e: Fraction) -> str:
    if e == 1:
        return ""
    if e.denominator == 1:
        return str(e.numerator).translate(_SUPER_OF)
    return "^%s" % e


def _one(key: str, e: Fraction) -> str:
    name = key if re.fullmatch(r"[^\s()·*/^]+", key) else "(%s)" % key
    if e == Fraction(1, 2):
        return "√" + name
    return name + _power(e)


def _written(atoms: Dict[str, Fraction]) -> str:
    if len(atoms) == 1 and list(atoms.values())[0] == 1:   # one unit, as it was written
        return next(iter(atoms))
    up = [_one(k, e) for k, e in atoms.items() if e > 0]
    down = [_one(k, -e) for k, e in atoms.items() if e < 0]
    if not down:
        return "·".join(up)
    under = down[0] if len(down) == 1 else "(%s)" % "·".join(down)
    return "%s/%s" % ("·".join(up) or "1", under)


# ------------------------------------------------------------------ reading a unit as people write it

_TOKEN = re.compile(r"\s*(?:(?P<pow>\*\*\s*-?\d+(?:\.\d+)?|\^\s*-?\d+(?:\.\d+)?|[⁻⁺]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+)|(?P<open>\()|"
                    r"(?P<close>\))|(?P<op>[·*⋅×/])|(?P<root>√)|(?P<atom>[^\s()·*⋅×/^√⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+))")


def _name(atom: str) -> str:
    if atom in _SPELLED:
        return _SPELLED[atom]
    if len(atom) > 1 and atom[0] in "uμ" and atom[1:] in BASES:   # uV, μV (a Greek mu) as people type them
        return "µ" + atom[1:]
    for spelled, sign in _SPELLED.items():
        if len(atom) > len(spelled) and atom.endswith(spelled) and atom[:-len(spelled)] in PREFIX:
            return atom[:-len(spelled)] + sign
    return atom


def parse(text: Any) -> Unit:
    """A unit as a card or a file writes it: V, mA, V/T², cm²/(V·s), counts/s, 1/Hz, %. Anything else is a unit of its
    own, whole (a.u., /16384 turn)."""
    text = str(text or "").strip()
    if not text:
        return ONE
    tokens = []
    at = 0
    while at < len(text):
        m = _TOKEN.match(text, at)
        if not m or m.end() == at:
            return Unit({text: 1})
        tokens.append((m.lastgroup, m.group(m.lastgroup)))
        at = m.end()
    try:
        u, rest = _product(tokens)
    except ValueError:
        return Unit({text: 1})
    return u if not rest else Unit({text: 1})


def _product(tokens: List[Tuple[str, str]]) -> Tuple[Unit, List[Tuple[str, str]]]:
    u, tokens = _factor(tokens)
    while tokens and tokens[0][0] == "op":
        op = tokens[0][1]
        v, tokens = _factor(tokens[1:])
        u = u / v if op == "/" else u * v
    return u, tokens


def _factor(tokens: List[Tuple[str, str]]) -> Tuple[Unit, List[Tuple[str, str]]]:
    if not tokens:
        raise ValueError("a unit ends too soon")
    root = tokens[0][0] == "root"
    if root:
        tokens = tokens[1:]
        if not tokens:
            raise ValueError("√ of nothing")
    kind, value = tokens[0]
    if kind == "open":
        u, tokens = _product(tokens[1:])
        if not tokens or tokens[0][0] != "close":
            raise ValueError("a bracket left open")
        tokens = tokens[1:]
    elif kind == "atom":
        if value[0].isdigit() or value[0] in "+-.":
            if value != "1":
                raise ValueError("a number in a unit")
            u = ONE
        else:
            u = Unit({_name(value): 1})
        tokens = tokens[1:]
    else:
        raise ValueError("unexpected %r" % value)
    if tokens and tokens[0][0] == "pow":
        raw = tokens[0][1].translate(_SUPER).lstrip("*^ ").strip()
        u = u ** Fraction(raw).limit_denominator(12)
        tokens = tokens[1:]
    return (u ** Fraction(1, 2) if root else u), tokens


# ------------------------------------------------------------------ what the steps do to a unit (None: not known)

def per(y: Optional[str], x: Optional[str]) -> Optional[str]:
    """dy/dx's unit."""
    return None if y is None or x is None else (parse(y) / parse(x)).text()


def times(y: Optional[str], x: Optional[str]) -> Optional[str]:
    """∫ y dx's unit."""
    return None if y is None or x is None else (parse(y) * parse(x)).text()


def inverse(x: Optional[str]) -> Optional[str]:
    """The unit of 1/x: a time's is a frequency (ms: kHz), a frequency's a time."""
    if x is None:
        return None
    parts = split(x)
    if parts and parts[1] in ("s", "Hz") and (not parts[0] or -parts[0] in _PREFIX_OF):
        return (_PREFIX_OF[-parts[0]] if parts[0] else "") + ("Hz" if parts[1] == "s" else "s")
    return (ONE / parse(x)).text()


def scaled(y: Optional[str], factor: float) -> Optional[str]:
    """The unit of y times a number: a power of ten moves the prefix (V × 1e6: µV), 100 times a ratio is in %; any
    other number keeps a ratio a ratio, and leaves a unit unknown (the number may carry a unit of its own)."""
    if y is None:
        return None
    u = _times_number(parse(y), factor)
    return None if u is None else u.text()


def _ten(c: float) -> Optional[int]:
    """c's power of ten, when it is one (1e-6 -> -6)."""
    if not c or not math.isfinite(c):
        return None
    k = round(math.log10(abs(c)))
    return k if abs(abs(c) / 10.0 ** k - 1) < 1e-9 else None


def _times_number(u: Unit, c: float) -> Optional[Unit]:
    k = _ten(c)
    if k is not None:
        return Unit(u.atoms, u.ten - k)
    if u.plain or c == 0:
        return u
    return None


# ------------------------------------------------------------------ an expression's unit

class _Unknown(Exception):
    pass


class _Number:
    """A number written in the expression."""

    __slots__ = ("value",)

    def __init__(self, value: float):
        self.value = float(value)


_SAME = {"abs", "floor", "ceil", "round", "diff", "cumsum", "mean", "std", "min", "max", "sum", "median"}
_PLAIN = {"sign", "exp", "log", "log10", "sin", "cos", "tan", "arcsin", "arccos", "arctan", "arctan2", "sinh",
          "cosh", "tanh", "count"}


def of(text: str, unit_of: Callable[[str], str]) -> Optional[str]:
    """The unit of an expression of a run's columns (expr.py's language); unit_of(name) gives a column's (KeyError for
    a name that is no column). None when it can't be told."""
    try:
        tree = ast.parse(str(text or "").strip(), mode="eval")
    except SyntaxError:
        return None
    try:
        u = _walk(tree.body, unit_of)
    except (_Unknown, RecursionError, ValueError, ZeroDivisionError, OverflowError):
        return None
    return "" if isinstance(u, _Number) else u.text()


def _column(name: str, unit_of: Callable[[str], str]) -> Any:
    try:
        return parse(unit_of(name))
    except KeyError:
        pass
    if name in CONSTANTS:
        return parse(CONSTANTS[name])
    if name in expr.CONSTANTS:
        return _Number(expr.CONSTANTS[name])
    raise _Unknown(name)


def _either(a: Any, b: Any) -> Any:
    """What a sum (or a choice between two) is in: the same unit on both sides, a bare number taking the other's."""
    if isinstance(a, _Number) and isinstance(b, _Number):
        return _Number(a.value)
    if isinstance(a, _Number):
        return b
    if isinstance(b, _Number) or a == b:
        return a
    raise _Unknown("a sum of different units")


def _product_of(a: Any, b: Any, divide: bool) -> Any:
    if isinstance(a, _Number) and isinstance(b, _Number):
        return _Number(a.value / b.value if divide else a.value * b.value)
    if isinstance(b, _Number):   # x * 1e6, x / 1e-6
        if divide and not b.value:
            raise _Unknown("a division by zero")
        u = _times_number(a, 1.0 / b.value if divide else b.value)
    elif isinstance(a, _Number):   # 1e6 * x, 1e-3 / x
        u = _times_number(b ** -1 if divide else b, a.value)
    else:
        return a / b if divide else a * b
    if u is None:
        raise _Unknown("a number that may carry a unit")
    return u


def _exponent(n: Any) -> Fraction:
    if not isinstance(n, _Number):
        raise _Unknown("an exponent with a unit")
    f = Fraction(n.value).limit_denominator(12)
    if abs(float(f) - n.value) > 1e-9:
        raise _Unknown("an odd power")
    return f


def _walk(node: Any, unit_of: Callable[[str], str]) -> Any:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (bool, int, float)):
            return _Number(float(node.value))
        raise _Unknown("a value that isn't a number")
    if isinstance(node, (ast.Name, ast.Attribute)):
        try:
            name = expr._dotted(node)
        except expr.ExprError:
            raise _Unknown("a name")
        return _column(name, unit_of)
    if isinstance(node, ast.BinOp):
        a, b = _walk(node.left, unit_of), _walk(node.right, unit_of)
        if isinstance(node.op, (ast.Add, ast.Sub)):
            if isinstance(a, _Number) and isinstance(b, _Number):
                return _Number(a.value + b.value if isinstance(node.op, ast.Add) else a.value - b.value)
            return _either(a, b)
        if isinstance(node.op, (ast.Mult, ast.Div)):
            return _product_of(a, b, isinstance(node.op, ast.Div))
        if isinstance(node.op, ast.Pow):
            if isinstance(a, _Number):
                if isinstance(b, _Number):
                    return _Number(a.value ** b.value)
                if b.plain:
                    return ONE
                raise _Unknown("a power with a unit")
            if not isinstance(b, _Number) and a.plain and b.plain:
                return ONE
            return a ** _exponent(b)
        if isinstance(node.op, ast.Mod):
            return a if isinstance(b, _Number) else _either(a, b)
        raise _Unknown("an operator")
    if isinstance(node, ast.UnaryOp):
        v = _walk(node.operand, unit_of)
        if isinstance(node.op, ast.Not):
            return ONE
        return _Number(-v.value) if isinstance(v, _Number) and isinstance(node.op, ast.USub) else v
    if isinstance(node, (ast.BoolOp, ast.Compare)):
        return ONE
    if isinstance(node, ast.IfExp):
        return _either(_walk(node.body, unit_of), _walk(node.orelse, unit_of))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        return _call(node.func.id, node.args, unit_of)
    raise _Unknown(type(node).__name__)


def _call(name: str, nodes: Sequence[Any], unit_of: Callable[[str], str]) -> Any:
    if name == "col":
        if len(nodes) != 1 or not isinstance(nodes[0], ast.Constant) or not isinstance(nodes[0].value, str):
            raise _Unknown("col")
        return _column(nodes[0].value, unit_of)
    args = [_walk(n, unit_of) for n in nodes]
    if not args:
        raise _Unknown(name)
    first = args[0]
    if name in _SAME or name in ("smooth", "clip"):
        return first
    if name in _PLAIN:
        return ONE
    if name == "sqrt":
        return _Number(math.sqrt(first.value)) if isinstance(first, _Number) else first ** Fraction(1, 2)
    if name in ("hypot", "where") and len(args) >= 2:
        return _either(args[-2], args[-1]) if name == "where" else _either(args[0], args[1])
    if name == "deriv" and len(args) == 2 and not isinstance(args[1], _Number):
        return _product_of(args[0], args[1], True)
    if name == "interp" and len(args) == 3:
        return args[2]
    if name in ("at_min", "at_max") and len(args) == 2:
        return args[1]
    if name == "deg2rad":
        return Unit({"rad": 1})
    if name == "rad2deg":
        return Unit({"deg": 1})
    raise _Unknown(name)


# ------------------------------------------------------------------ a model's parameters

# A part of a model: its unit as fixed atoms plus multiples of the parameters' units, (atoms, {parameter: multiple}),
# or None for a bare number.
_Form = Optional[Tuple[Dict[str, Fraction], Dict[str, Fraction]]]
_DIMENSIONLESS_ARG = {"exp", "log", "log10", "sin", "cos", "tan", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh"}


def model_units(formula: str, params: Sequence[str], x_unit: Optional[str], y_unit: Optional[str],
                x_name: str = "x") -> Dict[str, str]:
    """The units of a model's parameters where its form fixes them (a*exp(-x/tau) + c: a and c in y's unit, tau in
    x's; R0*(1 + a*x**2): a in 1/x²). The rest are left out, never guessed."""
    try:
        tree = ast.parse(str(formula or "").strip(), mode="eval")
    except SyntaxError:
        return {}
    names = list(params) + ([x_name] if x_unit is None else [])
    hard: List[Tuple[Dict[str, Fraction], Dict[str, Fraction]]] = []   # (multiples, atoms): sum = atoms
    soft: List[Tuple[Dict[str, Fraction], Dict[str, Fraction]]] = []   # 1 + a*x**2: a*x**2 has no unit, if it can be
    try:
        root = _shape(tree.body, set(names), x_name, x_unit, hard, soft)
    except (_Unknown, RecursionError, ValueError, ZeroDivisionError):
        return {}
    if root is not None and y_unit is not None:
        hard.append(_equal(root, (parse(y_unit).atoms, {})))
    if _solve(hard, names) is None:   # the model's own form contradicts itself: say nothing
        return {}
    kept = list(hard)
    for c in soft:
        if _solve(kept + [c], names) is not None:
            kept.append(c)
    found = _solve(kept, names) or {}
    return {p: Unit(found[p]).text() or "" for p in params if p in found}


def _equal(a: Tuple[Dict[str, Fraction], Dict[str, Fraction]],
           b: Tuple[Dict[str, Fraction], Dict[str, Fraction]]) -> Tuple[Dict[str, Fraction], Dict[str, Fraction]]:
    """a = b as (multiples of the parameters' units, the atoms they must make)."""
    multiples = dict(a[1])
    for k, v in b[1].items():
        multiples[k] = multiples.get(k, Fraction(0)) - v
    atoms = dict(b[0])
    for k, v in a[0].items():
        atoms[k] = atoms.get(k, Fraction(0)) - v
    return {k: v for k, v in multiples.items() if v}, {k: v for k, v in atoms.items() if v}


def _scaled(f: _Form, n: Fraction) -> _Form:
    return None if f is None else ({k: v * n for k, v in f[0].items()}, {k: v * n for k, v in f[1].items()})


def _added(a: _Form, b: _Form) -> _Form:
    if a is None or b is None:
        return a if b is None else b
    atoms, multiples = dict(a[0]), dict(a[1])
    for k, v in b[0].items():
        atoms[k] = atoms.get(k, Fraction(0)) + v
    for k, v in b[1].items():
        multiples[k] = multiples.get(k, Fraction(0)) + v
    return {k: v for k, v in atoms.items() if v}, {k: v for k, v in multiples.items() if v}


_NOTHING: Tuple[Dict[str, Fraction], Dict[str, Fraction]] = ({}, {})


def _shape(node: Any, names: set, x_name: str, x_unit: Optional[str], hard: List[Any], soft: List[Any]) -> _Form:
    def walk(n: Any) -> _Form:
        return _shape(n, names, x_name, x_unit, hard, soft)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return None
    if isinstance(node, (ast.Name, ast.Attribute)):
        name = expr._dotted(node)
        if name in names:
            return {}, {name: Fraction(1)}
        if name in (x_name, "x"):
            return dict(parse(x_unit).atoms), {}
        if name in CONSTANTS and len(name) > 1:
            return dict(parse(CONSTANTS[name]).atoms), {}
        if name in expr.CONSTANTS and len(name) > 1:   # pi
            return None
        raise _Unknown(name)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return walk(node.operand)
    if isinstance(node, ast.BinOp):
        a, b = walk(node.left), walk(node.right)
        if isinstance(node.op, (ast.Add, ast.Sub)):
            if a is None or b is None:
                other = a if b is None else b
                if other is not None and other[1]:   # 1 + a*x: the term with parameters is a ratio, unless that
                    soft.append(_equal(other, _NOTHING))   # contradicts the rest
                return other
            hard.append(_equal(a, b))
            return a
        if isinstance(node.op, ast.Mult):
            return _added(a, b) if a is not None or b is not None else None
        if isinstance(node.op, ast.Div):
            return _added(a, _scaled(b, Fraction(-1))) if a is not None or b is not None else None
        if isinstance(node.op, ast.Pow):
            if b is None and isinstance(node.right, (ast.Constant, ast.UnaryOp)):
                return _scaled(a, Fraction(_number(node.right)).limit_denominator(12))
            for part in (a, b):
                if part is not None:
                    hard.append(_equal(part, _NOTHING))
            return _NOTHING
        raise _Unknown("an operator")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and len(node.args) == 1 and not node.keywords:
        arg = walk(node.args[0])
        if node.func.id in _DIMENSIONLESS_ARG:
            if arg is not None:
                hard.append(_equal(arg, _NOTHING))
            return _NOTHING
        if node.func.id == "sqrt":
            return _scaled(arg, Fraction(1, 2))
        if node.func.id in ("abs", "floor", "ceil", "round"):
            return arg
    raise _Unknown(type(node).__name__)


def _number(node: Any) -> float:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_number(node.operand)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    raise _Unknown("a power that isn't a number")


def _solve(rows: List[Tuple[Dict[str, Fraction], Dict[str, Fraction]]], names: Sequence[str]
           ) -> Optional[Dict[str, Dict[str, Fraction]]]:
    """Each row says: the sum of multiples of the names' units makes these atoms. The units the rows fix, by name;
    None when the rows contradict each other. Gaussian elimination, exactly (fractions)."""
    m = [([Fraction(c.get(n, 0)) for n in names], dict(rhs)) for c, rhs in rows]
    pivots: List[int] = []
    r = 0
    for col in range(len(names)):
        at = next((i for i in range(r, len(m)) if m[i][0][col]), None)
        if at is None:
            continue
        m[r], m[at] = m[at], m[r]
        f = m[r][0][col]
        m[r] = ([v / f for v in m[r][0]], {k: v / f for k, v in m[r][1].items()})
        for i in range(len(m)):
            g = m[i][0][col]
            if i == r or not g:
                continue
            rhs = dict(m[i][1])
            for k, v in m[r][1].items():
                rhs[k] = rhs.get(k, Fraction(0)) - g * v
            m[i] = ([a - g * b for a, b in zip(m[i][0], m[r][0])], {k: v for k, v in rhs.items() if v})
        pivots.append(col)
        r += 1
    if any(any(rhs.values()) for _, rhs in m[r:]):
        return None
    return {names[col]: m[i][1] for i, col in enumerate(pivots)
            if all(not m[i][0][j] for j in range(len(names)) if j != col)}
