"""An experiment: a lab's Python that reaches the instruments by name through Kelvin (`from kelvin import lab`, the
module the script shim serves: data/scriptshim/kelvin_api.py), read without running it.

    lab.set("smu.V", v), lab.get("lockin.X"), lab.do("stage.home"), lab.raw("smu", "SYST:BEEP")
    @action / @action("scan", main=True)      each runnable piece of it, run one at a time
    live("Break", False)                      an input a person or the chat changes while it runs

Kelvin reads ahead what each action does (its own code, the module's, and every function of the file they use), so
the one approval a person answers shows the drives it moves and the values it moves them to (scriptrun.read_ahead),
as for any of the lab's scripts. What it can't read ahead (a value or an instrument the experiment works out as it
runs) is said, and the gate checks every command as it is sent anyway."""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .scripts import _Values, _unparse

API = "kelvin"
CALLS = ("set", "get", "do", "raw")
SHOWN = 60   # characters of a call, as an unread line shows it


@dataclass
class Uses:
    sets: Dict[str, Optional[List[Any]]] = field(default_factory=dict)   # "smu.V" -> its values (None: as it runs)
    gets: List[str] = field(default_factory=list)                        # "lockin.X", in first-use order
    acts: List[str] = field(default_factory=list)                        # "stage.home"
    raw: Dict[str, List[Optional[str]]] = field(default_factory=dict)    # instrument -> commands (None: as it runs)
    unread: List[str] = field(default_factory=list)   # calls whose instrument or parameter isn't written out

    def add(self, other: "Uses") -> None:
        for key, values in other.sets.items():
            if key in self.sets and (self.sets[key] is None or values is None):
                self.sets[key] = None
            elif key in self.sets:
                self.sets[key] = _union(self.sets[key] or [], values)
            else:
                self.sets[key] = None if values is None else list(values)
        self.gets += [k for k in other.gets if k not in self.gets]
        self.acts += [k for k in other.acts if k not in self.acts]
        for name, commands in other.raw.items():
            have = self.raw.setdefault(name, [])
            have += [c for c in commands if c not in have]
        self.unread += [u for u in other.unread if u not in self.unread]


@dataclass
class Experiment:
    actions: List[str] = field(default_factory=list)   # in file order
    main: Optional[str] = None                          # the action marked main=True; None when none is
    live: Dict[str, Any] = field(default_factory=dict)  # live input -> its default, in first-use order
    problems: List[str] = field(default_factory=list)
    scopes: Dict[str, Uses] = field(default_factory=dict, repr=False)      # "" (the module's own code), functions
    refs: Dict[str, List[str]] = field(default_factory=dict, repr=False)   # scope -> the file's functions it uses
    functions: Dict[str, str] = field(default_factory=dict, repr=False)    # action -> its function


def _union(a: List[Any], b: List[Any]) -> List[Any]:
    return list(a) + [v for v in b if v not in a]


def is_experiment(source: str) -> bool:
    """Does the script use Kelvin's experiment API (it imports kelvin)?"""
    from .scriptrun import imports
    return API in imports(source)


class _Names:
    """How the file names the API: `from kelvin import lab as L` (L is lab), `import kelvin as k` (k.lab)."""

    def __init__(self, tree: ast.Module):
        self.direct: Dict[str, str] = {}   # local name -> the API's name (lab, action, live)
        self.modules: Set[str] = set()     # names the kelvin module itself goes by
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == API and not node.level:
                for a in node.names:
                    self.direct[a.asname or a.name] = a.name
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == API:
                        self.modules.add(a.asname or a.name)

    def of(self, node: ast.AST) -> Optional[str]:
        """The API's name this expression is (lab, action, live), or None."""
        if isinstance(node, ast.Name):
            return self.direct.get(node.id)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in self.modules:
            return node.attr
        return None


class _Reader(ast.NodeVisitor):
    """One scope's calls to lab.set/get/do/raw (the module's own code, or one of its functions), with the values each
    set reads ahead: loop variables are known inside their loop only, and a function's own names (its parameters, what
    it assigns) are worked out as it runs."""

    def __init__(self, names: _Names, values: _Values, functions: Set[str], local: Set[str]):
        self.names, self.values, self.functions, self.local = names, values, functions, local
        self.uses = Uses()
        self.refs: List[str] = []
        self.loops: List[Dict[str, List[float]]] = []

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and node.id in self.functions and node.id not in self.refs:
            self.refs.append(node.id)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        _, vals = self.values.series(node.iter)
        bound = {node.target.id: vals} if vals and isinstance(node.target, ast.Name) else {}
        self.loops.append(bound)
        for stmt in node.body:
            self.visit(stmt)
        self.loops.pop()
        for stmt in node.orelse:
            self.visit(stmt)

    def _bound(self, name: str) -> Optional[List[float]]:
        for frame in reversed(self.loops):
            if name in frame:
                return frame[name]
        return None

    def visit_Call(self, node: ast.Call) -> None:
        self.generic_visit(node)
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr in CALLS and self.names.of(f.value) == "lab"):
            return
        args = list(node.args)
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        if f.attr == "raw":
            inst = args[0] if args else kw.get("instrument")
            cmd = args[1] if len(args) > 1 else kw.get("command")
            name, ok = self.values.text(inst)
            if not ok or not name:
                return self._unread(node)
            text, known = self.values.text(cmd)
            have = self.uses.raw.setdefault(name, [])
            entry = text if known else None
            if entry not in have:
                have.append(entry)
            return
        key, ok = self.values.text(args[0] if args else kw.get("key"))
        if not ok or not key:
            return self._unread(node)
        if f.attr == "get":
            if key not in self.uses.gets:
                self.uses.gets.append(key)
        elif f.attr == "do":
            if key not in self.uses.acts:
                self.uses.acts.append(key)
        else:
            value = args[1] if len(args) > 1 else kw.get("value")
            self.uses.add(Uses(sets={key: self._values(value)}))

    def _unread(self, node: ast.Call) -> None:
        text = _unparse(node)
        line = "line %d: %s names its instrument or parameter as it runs" % (
            node.lineno, text if len(text) <= SHOWN else text[:SHOWN - 1] + "…")
        if line not in self.uses.unread:
            self.uses.unread.append(line)

    def _values(self, node: Optional[ast.AST]) -> Optional[List[Any]]:
        """The values a set's value takes, read ahead; None when they are worked out as it runs."""
        if node is None:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, (bool, str)):
            return [node.value]
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        loops = {n: self._bound(n) for n in names if self._bound(n) is not None}
        if any(n in self.local and n not in loops for n in names):
            return None   # a parameter, or a name the function assigns as it runs
        if not loops:
            v = self.values.number(node)
            return None if v is None else [v]
        if len(loops) > 1:
            return None
        (var, xs), = loops.items()
        missing = object()
        before = self.values.known.get(var, missing)
        out: List[Any] = []
        try:
            for x in xs:
                self.values.known[var] = x
                v = self.values.number(node)
                if v is None:
                    return None
                if v not in out:
                    out.append(v)
        finally:
            if before is missing:
                self.values.known.pop(var, None)
            else:
                self.values.known[var] = before
        return out


def _known(tree: ast.Module) -> _Values:
    """The module's constants and lists of numbers, as a script's reading keeps them (scripts._Values)."""
    values = _Values()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for t in targets:
            if not isinstance(t, ast.Name):
                continue
            num = values.number(value)
            if num is not None:
                values.known[t.id] = num
            elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                values.known[t.id] = value.value
            elif isinstance(value, (ast.List, ast.Tuple)):
                nums = [values.number(e) for e in value.elts]
                if nums and None not in nums:
                    values.known[t.id] = [float(n) for n in nums]
    return values


def _locals(fn: ast.AST) -> Set[str]:
    """A function's own names: its parameters and every name it assigns."""
    a = fn.args
    out = {x.arg for x in list(getattr(a, "posonlyargs", [])) + a.args + a.kwonlyargs}
    out |= {x.arg for x in (a.vararg, a.kwarg) if x is not None}
    out |= {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    return out


def _action_of(names: _Names, dec: ast.AST, fn: str) -> Optional[Dict[str, Any]]:
    """An @action decorator: {name, main, line}; None for any other decorator."""
    call = dec if isinstance(dec, ast.Call) else None
    if names.of(call.func if call else dec) != "action":
        return None
    name, main = fn, False
    if call is not None:
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        given = call.args[0] if call.args else kw.get("name")
        if isinstance(given, ast.Constant) and isinstance(given.value, str) and given.value:
            name = given.value
        m = kw.get("main")
        main = isinstance(m, ast.Constant) and m.value is True
    return {"name": name, "main": main, "line": dec.lineno}


def read(source: str) -> Experiment:
    """What an experiment's file holds, read without running it: its actions, its live inputs, and what each part of
    it does with the instruments. SyntaxError for a file that isn't Python."""
    tree = ast.parse(source)
    names = _Names(tree)
    values = _known(tree)
    functions = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    exp = Experiment()

    module = _Reader(names, values, set(functions), set())
    for stmt in tree.body:
        if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            module.visit(stmt)
        else:
            for d in stmt.decorator_list:
                module.visit(d)
    exp.scopes[""], exp.refs[""] = module.uses, module.refs

    found: List[Dict[str, Any]] = []
    for fn_name, fn in functions.items():
        reader = _Reader(names, values, set(functions), _locals(fn))
        for stmt in fn.body:
            reader.visit(stmt)
        exp.scopes[fn_name], exp.refs[fn_name] = reader.uses, reader.refs
        for dec in fn.decorator_list:
            act = _action_of(names, dec, fn_name)
            if act is not None:
                found.append(dict(act, function=fn_name))
                break

    for act in found:
        twin = next((a for a in found if a["name"] == act["name"] and a is not act), None)
        if act["name"] in exp.functions:
            if twin is not None and found.index(twin) < found.index(act):
                exp.problems.append("two actions are named %s (lines %d and %d)" % (
                    act["name"], twin["line"], act["line"]))
            continue
        exp.actions.append(act["name"])
        exp.functions[act["name"]] = act["function"]
    mains = [a["name"] for a in found if a["main"]]
    if len(mains) > 1:
        exp.problems.append("two actions are marked main: %s and %s" % (mains[0], mains[1]))
    exp.main = mains[0] if mains else None

    constants = {}   # the module's own plain values (SETPOINT = 0.5, MODE = "fast"), as written
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                constants[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                constants.pop(node.targets[0].id, None)
    calls = sorted((n for n in ast.walk(tree) if isinstance(n, ast.Call) and names.of(n.func) == "live"),
                   key=lambda n: (n.lineno, n.col_offset))
    for c in calls:
        kw = {k.arg: k.value for k in c.keywords if k.arg}
        given = c.args[0] if c.args else kw.get("name")
        if not (isinstance(given, ast.Constant) and isinstance(given.value, str)) or given.value in exp.live:
            continue
        default = c.args[1] if len(c.args) > 1 else kw.get("default")
        exp.live[given.value] = _default(default, constants, values)
    return exp


def _default(node: Optional[ast.AST], constants: Dict[str, Any], values: _Values) -> Any:
    """A live input's default, read ahead: a value written out, a module constant, or arithmetic on them; None when
    it is worked out as it runs (the experiment's own value then counts from its first read)."""
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        pass
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    return values.number(node)


def choose(exp: Experiment, action: str = "") -> str:
    """The action a run runs: the one named, else the one marked main, else the only one; "" for an experiment with
    no actions (it runs as a whole). ValueError: why none can run, in words."""
    if exp.problems:
        raise ValueError(exp.problems[0])
    if not exp.actions:
        if action:
            raise ValueError("it has no actions: it runs as a whole, so leave action out")
        return ""
    if action:
        if action not in exp.actions:
            raise ValueError("it has no action %r (it has: %s)" % (action, ", ".join(exp.actions)))
        return action
    if exp.main:
        return exp.main
    if len(exp.actions) == 1:
        return exp.actions[0]
    raise ValueError("it has several actions and none is marked main: say which (%s)" % ", ".join(exp.actions))


def uses(exp: Experiment, action: str = "") -> Uses:
    """What one action does with the instruments: the module's own code, the action's function, and every function of
    the file they use (called, or passed as a thread's target), in turn. action "": everything in the file."""
    out = Uses()
    if not action:
        for scope in exp.scopes.values():
            out.add(scope)
        return out
    todo = [""] + ([exp.functions[action]] if action in exp.functions else [])
    seen: Set[str] = set()
    while todo:
        scope = todo.pop(0)
        if scope in seen or scope not in exp.scopes:
            continue
        seen.add(scope)
        out.add(exp.scopes[scope])
        todo += exp.refs.get(scope, [])
    return out
