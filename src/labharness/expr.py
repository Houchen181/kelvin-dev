"""Arithmetic on a run's columns, for the AI (compute, and get_run_data's where): a small, safe expression language.

    lockin.X / smu.I              a new column from others (here a resistance)
    deriv(lockin.X, smu.V)        dX/dV
    mean(smu.I)                   one number
    smu.V > 1 and tc.T < 5        a condition (where)

Only numbers, column names (instrument.parameter, i and t; col("name") for any other name), arithmetic
(+ - * / ** %), comparisons, and/or/not, a if condition else b, the constants in CONSTANTS and the functions in
functions(): nothing else in Python is reachable. Numbers are 64-bit floats, so no computation can grow without
bound."""
from __future__ import annotations

import ast
import contextlib
import functools
import math
import operator
from typing import Any, Callable, Dict, List

MAX_LENGTH = 500      # characters in an expression
MAX_NODES = 300       # its parts

CONSTANTS = {"pi": 3.141592653589793, "kB": 1.380649e-23, "qe": 1.602176634e-19, "h": 6.62607015e-34,
             "hbar": 1.054571817e-34, "muB": 9.2740100783e-24, "mu0": 1.25663706212e-06, "eps0": 8.8541878128e-12,
             "c": 299792458.0, "me": 9.1093837015e-31, "G0": 7.748091729e-05, "RK": 25812.80745,
             "Phi0": 2.067833848e-15}


class ExprError(ValueError):
    pass


def moving_average(np: Any, y: Any, n: int) -> Any:
    """The mean of the n points around each (a point without a number keeps none). Near the ends the window holds
    fewer points and the mean is over those, never over zeros past the sweep: zero padding pulled the ends down, so
    at_min(smooth(...)) gave the sweep's first frequency, and a recipe tuned the next run off its resonance."""
    y = np.asarray(y, dtype=float)
    ok = np.isfinite(y)
    k = np.ones(int(n))
    lead = (int(n) - 1) // 2   # the window's points before its center, as numpy's mode="same" takes them
    total = np.convolve(np.where(ok, y, 0.0), k, mode="full")[lead:lead + y.size]
    count = np.convolve(ok.astype(float), k, mode="full")[lead:lead + y.size]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(ok & (count > 0), total / np.maximum(count, 1.0), np.nan)


def _whole(n: Any, what: str) -> int:
    """A number numpy takes only as a whole one (decimals, the order of a difference): every number written in an
    expression is a float, which numpy refused there, so round(x, 3) and diff(x, 2) always failed."""
    try:
        f = float(n)
    except (TypeError, ValueError):
        raise ExprError("%s needs one number for n" % what)
    if not f.is_integer():
        raise ExprError("%s needs a whole number for n, not %s" % (what, n))
    return int(f)


def functions(np: Any) -> Dict[str, Callable[..., Any]]:
    def deriv(y: Any, x: Any) -> Any:
        return np.gradient(y, x)

    def diff(y: Any, n: Any = 1) -> Any:
        """The n-th difference of neighbouring values (n up to 1000: numpy steps through each)."""
        n = _whole(n, "diff(y, n)")
        if not 0 <= n <= 1000:
            raise ExprError("diff(y, n) needs 0 <= n <= 1000")
        return np.diff(y, n)

    def round_(y: Any, n: Any = 0) -> Any:
        """Rounded to n decimals (a negative n: to tens, hundreds...)."""
        n = _whole(n, "round(y, n)")
        if not -400 <= n <= 400:   # past a 64-bit float's own digits
            raise ExprError("round(y, n) needs -400 <= n <= 400")
        return np.round(y, n)

    def smooth(y: Any, n: Any) -> Any:
        """A moving average over n points; near the ends, over the points there are."""
        n = int(n)
        if not 1 <= n <= 1001:
            raise ExprError("smooth(y, n) needs 1 <= n <= 1001")
        return moving_average(np, y, n)

    def interp(x0: Any, x: Any, y: Any) -> Any:
        order = np.argsort(x)
        return np.interp(x0, x[order], y[order])

    def at_min(y: Any, x: Any) -> Any:
        """x where y is smallest (e.g. the frequency of a dip)."""
        return np.asarray(x, dtype=float)[int(np.nanargmin(y))]

    def at_max(y: Any, x: Any) -> Any:
        """x where y is largest (e.g. the gate voltage of a peak)."""
        return np.asarray(x, dtype=float)[int(np.nanargmax(y))]

    return {"abs": np.abs, "sqrt": np.sqrt, "log": np.log, "log10": np.log10, "exp": np.exp, "sin": np.sin,
            "cos": np.cos, "tan": np.tan, "arcsin": np.arcsin, "arccos": np.arccos, "arctan": np.arctan,
            "arctan2": np.arctan2, "hypot": np.hypot, "sinh": np.sinh, "cosh": np.cosh, "tanh": np.tanh,
            "deg2rad": np.deg2rad, "rad2deg": np.rad2deg, "sign": np.sign, "floor": np.floor, "ceil": np.ceil,
            "round": round_, "clip": np.clip, "where": np.where, "deriv": deriv, "diff": diff,
            "cumsum": np.nancumsum, "smooth": smooth, "interp": interp, "at_min": at_min, "at_max": at_max,
            "mean": np.nanmean, "std": np.nanstd,
            "min": np.nanmin, "max": np.nanmax, "sum": np.nansum, "median": np.nanmedian,
            "count": lambda a: np.float64(np.count_nonzero(np.isfinite(a)))}


HELP = ("functions: abs sqrt log log10 exp sin cos tan arcsin arccos arctan arctan2 hypot sinh cosh tanh deg2rad "
        "rad2deg sign floor ceil round clip(a, lo, hi) where(cond, a, b) deriv(y, x) diff cumsum smooth(y, n) "
        "interp(x0, x, y) at_min(y, x) at_max(y, x) (x where y is smallest or largest) mean std min max sum median "
        "count; constants: " + " ".join(CONSTANTS))


class _Point:
    """numpy's functions as evaluate uses them, on one point's plain numbers: a plan's sets: is worked out at each
    point without numpy (Kelvin's core runs without it). A function of a whole column (mean, smooth, deriv...) has
    no meaning at one point and says so."""
    float64 = float
    add, subtract, multiply, power = operator.add, operator.sub, operator.mul, operator.pow
    mod, floor_divide = operator.mod, operator.floordiv
    less, less_equal, greater, greater_equal = operator.lt, operator.le, operator.gt, operator.ge
    equal, not_equal, negative = operator.eq, operator.ne, operator.neg
    abs, floor, ceil, round, hypot = abs, math.floor, math.ceil, round, math.hypot
    sqrt, log, log10, exp, sin, cos, tan = math.sqrt, math.log, math.log10, math.exp, math.sin, math.cos, math.tan
    arcsin, arccos, arctan, arctan2 = math.asin, math.acos, math.atan, math.atan2
    sinh, cosh, tanh, deg2rad, rad2deg = math.sinh, math.cosh, math.tanh, math.radians, math.degrees

    @staticmethod
    def true_divide(a: float, b: float) -> float:
        if b == 0:
            raise ExprError("a division by zero")
        return a / b

    @staticmethod
    def logical_not(a: Any) -> bool:
        return not a

    @staticmethod
    def logical_and(a: Any, b: Any) -> bool:
        return bool(a) and bool(b)

    @staticmethod
    def logical_or(a: Any, b: Any) -> bool:
        return bool(a) or bool(b)

    @staticmethod
    def where(c: Any, a: Any, b: Any) -> Any:
        return a if c else b

    @staticmethod
    def sign(x: float) -> float:
        return float((x > 0) - (x < 0))

    @staticmethod
    def clip(a: float, lo: float, hi: float) -> float:
        return min(max(a, lo), hi)

    @staticmethod
    def errstate(**_: Any) -> Any:
        return contextlib.nullcontext()

    def __getattr__(self, name: str) -> Any:
        def whole(*_: Any) -> Any:
            raise ExprError("this function works on a whole column, not on one point's values")
        return whole


@functools.lru_cache(maxsize=512)
def _parsed(text: str) -> Any:
    """text parsed and checked, once for each text (a plan's sets: works one out at every combination of its loops'
    values). The tree is only read."""
    if not text:
        raise ExprError("the expression is empty")
    if len(text) > MAX_LENGTH:
        raise ExprError("the expression is longer than %d characters" % MAX_LENGTH)
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        raise ExprError("not a valid expression: %s" % e.msg)
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise ExprError("the expression has too many parts")
    return tree


_POINT = _Point()
_POINT_FUNCS = functions(_POINT)


def names_in(text: str) -> List[str]:
    """The names an expression uses (instrument.parameter, a plan's variables, col("...")), in the order it first
    uses them, without its functions and constants. ExprError: not an expression evaluate takes."""
    tree = _parsed(str(text or "").strip())
    out: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "col" and node.args and \
                    isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                out.append(node.args[0].value)
                return
            for a in node.args:
                walk(a)
            return
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = _dotted(node)
            if name not in CONSTANTS:
                out.append(name)
            return
        for child in ast.iter_child_nodes(node):
            walk(child)
    walk(tree)
    return list(dict.fromkeys(out))


def scalar(text: str, values: Dict[str, Any]) -> float:
    """text worked out at one point: values gives each name its number. A finite number, else ExprError."""
    def column(key: str) -> Any:
        if key not in values:
            raise KeyError("no value for %s here" % key)
        v = values[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ExprError("%s is %r here, not a number" % (key, v))
        return float(v)
    try:
        out = evaluate(text, column, _POINT)
    except ExprError:
        raise
    except (ArithmeticError, ValueError, TypeError) as e:   # sqrt(-1), log(0), 1e308 ** 2
        raise ExprError(str(e) or type(e).__name__)
    if isinstance(out, bool) or not isinstance(out, (int, float)) or not math.isfinite(out):
        raise ExprError("it gives %r, not a finite number" % (out,))
    return float(out)


def _dotted(node: Any) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        raise ExprError("only column names can have dots (instrument.parameter)")
    parts.append(node.id)
    return ".".join(reversed(parts))


def _build(tree: Any, np: Any, funcs: Dict[str, Callable[..., Any]]) -> Callable[[Callable[[str], Any]], Any]:
    """The tree as a function of named (a name's values): each part made once into a closure, so working it out at
    many points walks no tree. What isn't allowed is refused here, before anything is worked out."""
    binary = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply, ast.Div: np.true_divide,
              ast.Pow: np.power, ast.Mod: np.mod, ast.FloorDiv: np.floor_divide}
    compare = {ast.Lt: np.less, ast.LtE: np.less_equal, ast.Gt: np.greater, ast.GtE: np.greater_equal,
               ast.Eq: np.equal, ast.NotEq: np.not_equal}

    def build(node: Any) -> Callable[[Callable[[str], Any]], Any]:
        if isinstance(node, ast.Expression):
            return build(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (bool, int, float)):
                try:
                    number = np.float64(node.value)
                except OverflowError:   # a whole number of hundreds of digits
                    raise ExprError("%.40s… is too large for a 64-bit float" % node.value)
                return lambda named: number
            raise ExprError("only numbers can be written as values (column names go without quotes)")
        if isinstance(node, (ast.Name, ast.Attribute)):
            key = _dotted(node)
            return lambda named: named(key)
        if isinstance(node, ast.BinOp) and type(node.op) in binary:
            op, left, right = binary[type(node.op)], build(node.left), build(node.right)
            return lambda named: op(left(named), right(named))
        if isinstance(node, ast.UnaryOp):
            operand = build(node.operand)
            if isinstance(node.op, ast.USub):
                return lambda named: np.negative(operand(named))
            if isinstance(node.op, ast.UAdd):
                return operand
            if isinstance(node.op, ast.Not):
                return lambda named: np.logical_not(operand(named))
        if isinstance(node, ast.BoolOp):
            parts = [build(v) for v in node.values]
            both = np.logical_and if isinstance(node.op, ast.And) else np.logical_or

            def boolean(named: Callable[[str], Any]) -> Any:
                values = [part(named) for part in parts]
                out = values[0]
                for v in values[1:]:
                    out = both(out, v)
                return out
            return boolean
        if isinstance(node, ast.Compare):
            first, steps = build(node.left), []
            for op, right_node in zip(node.ops, node.comparators):
                if type(op) not in compare:
                    raise ExprError("compare with < <= > >= == !=")
                steps.append((compare[type(op)], build(right_node)))

            def compared(named: Callable[[str], Any]) -> Any:
                left, out = first(named), None
                for op, right_part in steps:
                    right = right_part(named)
                    step = op(left, right)
                    out = step if out is None else np.logical_and(out, step)
                    left = right
                return out
            return compared
        if isinstance(node, ast.IfExp):
            test, body, orelse = build(node.test), build(node.body), build(node.orelse)
            return lambda named: np.where(test(named), body(named), orelse(named))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ExprError("call a function by its name, e.g. deriv(lockin.X, smu.V)")
            name = node.func.id
            if node.keywords:
                raise ExprError("give %s its arguments in order, without names" % name)
            if name == "col":
                if len(node.args) != 1 or not isinstance(node.args[0], ast.Constant) or \
                        not isinstance(node.args[0].value, str):
                    raise ExprError('col takes one column name in quotes, e.g. col("smu.V")')
                key = node.args[0].value
                return lambda named: named(key)
            if name not in funcs:
                raise ExprError("unknown function %r (%s)" % (name, HELP))
            fn, args = funcs[name], [build(a) for a in node.args]

            def called(named: Callable[[str], Any]) -> Any:
                values = [a(named) for a in args]
                try:
                    return fn(*values)
                except ExprError:
                    raise
                except (TypeError, ValueError, IndexError, ArithmeticError) as e:   # smooth(y, 1e400): infinite n
                    raise ExprError("%s: %s" % (name, e))
            return called
        raise ExprError("%s is not allowed in an expression (%s)" % (type(node).__name__, HELP))

    try:
        return build(tree)
    except RecursionError:
        raise ExprError("the expression is nested too deeply")


@functools.lru_cache(maxsize=512)
def _at_a_point(text: str) -> Callable[[Callable[[str], Any]], Any]:
    """text made once into the function scalar works out at each point."""
    return _build(_parsed(text), _POINT, _POINT_FUNCS)


def evaluate(text: str, column: Callable[[str], Any], np: Any) -> Any:
    """text evaluated with column(key) giving a column's values as a numpy array (KeyError if there is none)."""
    text = str(text or "").strip()
    fn = _at_a_point(text) if np is _POINT else _build(_parsed(text), np, functions(np))

    def named(key: str) -> Any:
        try:
            return column(key)
        except KeyError as e:
            if key in CONSTANTS:
                return np.float64(CONSTANTS[key])
            raise ExprError(e.args[0] if e.args else "no column %r" % key)

    with np.errstate(all="ignore"):
        try:
            return fn(named)
        except RecursionError:
            raise ExprError("the expression is nested too deeply")
