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
from typing import Any, Callable, Dict

MAX_LENGTH = 500      # characters in an expression
MAX_NODES = 300       # its parts

CONSTANTS = {"pi": 3.141592653589793, "kB": 1.380649e-23, "qe": 1.602176634e-19, "h": 6.62607015e-34,
             "hbar": 1.054571817e-34, "muB": 9.2740100783e-24, "mu0": 1.25663706212e-06, "eps0": 8.8541878128e-12,
             "c": 299792458.0, "me": 9.1093837015e-31, "G0": 7.748091729e-05, "RK": 25812.80745,
             "Phi0": 2.067833848e-15}


class ExprError(ValueError):
    pass


def functions(np: Any) -> Dict[str, Callable[..., Any]]:
    def deriv(y: Any, x: Any) -> Any:
        return np.gradient(y, x)

    def smooth(y: Any, n: Any) -> Any:
        """A moving average over n points."""
        n = int(n)
        if not 1 <= n <= min(1001, max(1, np.size(y))):
            raise ExprError("smooth(y, n) needs 1 <= n <= %d" % min(1001, max(1, np.size(y))))
        return np.convolve(y, np.ones(n) / n, mode="same")

    def interp(x0: Any, x: Any, y: Any) -> Any:
        order = np.argsort(x)
        return np.interp(x0, x[order], y[order])

    return {"abs": np.abs, "sqrt": np.sqrt, "log": np.log, "log10": np.log10, "exp": np.exp, "sin": np.sin,
            "cos": np.cos, "tan": np.tan, "arcsin": np.arcsin, "arccos": np.arccos, "arctan": np.arctan,
            "arctan2": np.arctan2, "hypot": np.hypot, "sinh": np.sinh, "cosh": np.cosh, "tanh": np.tanh,
            "deg2rad": np.deg2rad, "rad2deg": np.rad2deg, "sign": np.sign, "floor": np.floor, "ceil": np.ceil,
            "round": np.round, "clip": np.clip, "where": np.where, "deriv": deriv, "diff": np.diff,
            "cumsum": np.nancumsum, "smooth": smooth, "interp": interp, "mean": np.nanmean, "std": np.nanstd,
            "min": np.nanmin, "max": np.nanmax, "sum": np.nansum, "median": np.nanmedian,
            "count": lambda a: np.float64(np.count_nonzero(np.isfinite(a)))}


HELP = ("functions: abs sqrt log log10 exp sin cos tan arcsin arccos arctan arctan2 hypot sinh cosh tanh deg2rad "
        "rad2deg sign floor ceil round clip(a, lo, hi) where(cond, a, b) deriv(y, x) diff cumsum smooth(y, n) "
        "interp(x0, x, y) mean std min max sum median count; constants: " + " ".join(CONSTANTS))


def _dotted(node: Any) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        raise ExprError("only column names can have dots (instrument.parameter)")
    parts.append(node.id)
    return ".".join(reversed(parts))


def evaluate(text: str, column: Callable[[str], Any], np: Any) -> Any:
    """text evaluated with column(key) giving a column's values as a numpy array (KeyError if there is none)."""
    text = str(text or "").strip()
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
    funcs = functions(np)
    binary = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply, ast.Div: np.true_divide,
              ast.Pow: np.power, ast.Mod: np.mod, ast.FloorDiv: np.floor_divide}
    compare = {ast.Lt: np.less, ast.LtE: np.less_equal, ast.Gt: np.greater, ast.GtE: np.greater_equal,
               ast.Eq: np.equal, ast.NotEq: np.not_equal}

    def named(key: str) -> Any:
        try:
            return column(key)
        except KeyError as e:
            if key in CONSTANTS:
                return np.float64(CONSTANTS[key])
            raise ExprError(e.args[0] if e.args else "no column %r" % key)

    def ev(node: Any) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (bool, int, float)):
                return np.float64(node.value)
            raise ExprError("only numbers can be written as values (column names go without quotes)")
        if isinstance(node, (ast.Name, ast.Attribute)):
            return named(_dotted(node))
        if isinstance(node, ast.BinOp) and type(node.op) in binary:
            return binary[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp):
            v = ev(node.operand)
            if isinstance(node.op, ast.USub):
                return np.negative(v)
            if isinstance(node.op, ast.UAdd):
                return v
            if isinstance(node.op, ast.Not):
                return np.logical_not(v)
        if isinstance(node, ast.BoolOp):
            values = [ev(v) for v in node.values]
            out = values[0]
            for v in values[1:]:
                out = np.logical_and(out, v) if isinstance(node.op, ast.And) else np.logical_or(out, v)
            return out
        if isinstance(node, ast.Compare):
            left, out = ev(node.left), None
            for op, right_node in zip(node.ops, node.comparators):
                if type(op) not in compare:
                    raise ExprError("compare with < <= > >= == !=")
                right = ev(right_node)
                step = compare[type(op)](left, right)
                out = step if out is None else np.logical_and(out, step)
                left = right
            return out
        if isinstance(node, ast.IfExp):
            return np.where(ev(node.test), ev(node.body), ev(node.orelse))
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
                return named(node.args[0].value)
            if name not in funcs:
                raise ExprError("unknown function %r (%s)" % (name, HELP))
            args = [ev(a) for a in node.args]
            try:
                return funcs[name](*args)
            except ExprError:
                raise
            except (TypeError, ValueError, IndexError) as e:
                raise ExprError("%s: %s" % (name, e))
        raise ExprError("%s is not allowed in an expression (%s)" % (type(node).__name__, HELP))

    with np.errstate(all="ignore"):
        try:
            return ev(tree)
        except RecursionError:
            raise ExprError("the expression is nested too deeply")
