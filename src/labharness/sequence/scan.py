"""
panel_scan.py - read the tunable parameters out of an existing measurement script, and write
new values back into a COPY of it.

Scripts keep their settings as plain assignments, in two styles:

    scripts/sweep.py      inside main():  n_points = 96
    scripts/scan.py       at module level: OUTPUT_LEVEL = -9.0

Both are found. The `# comment` at the end of the line becomes the parameter's help text, and
the `# ---- section ----` comment above a run of assignments becomes its group, so the UI can
show the script's own documentation instead of a bare list of names.

Which lines may be edited is decided by where the value is READ, not by which assignment comes
last: `n = 61` read at L23 and `n = 65` read at L49 are both live, and each feeds its own sweep
(blind review #112). Every way Python binds a name counts - `x = f()`, `x += 1`, tuple targets,
for/with targets, imports and star imports, def/class, `global x` in a function - so a literal
that is re-bound before anything reads it is refused, not reported as applied (#037, #113).

What a measurement class does with a value once the script hands it over - reads it only under
a flag, or recomputes it - is not in the script: classes a lab wants described go into
_CLASS_RULES (empty by default), and such a line then says so, in the dialog and as a caution
before the run (round 4, #014 #015 #103).

Nothing here ever modifies your file: apply_overrides() returns new source text, and the engine
runs that from state/runs/<uid>/. Everything not overridden keeps the value in the file. The
patched text is scanned again before it is handed out, and refused unless exactly the requested
values changed (#034, #105).
"""
import ast
import copy
import hashlib
import io
import math
import os
import re
import symtable
import sys
import threading
import tokenize

# names that are plumbing, not settings
SKIP_NAMES = {'plt', 'np', 'settings', 'args', 'parser', 'self', 'fig', 'ax', 'data'}
MAX_STR = 300

ALL = '*'           # a read of every name: locals(), globals(), vars(), eval(), exec()
_DYN_ANY = ('locals', 'globals', 'eval', 'exec')
_DYN_BARE = ('vars', 'dir')             # only without arguments do these read the namespace
_MAX_EVENTS = 50000                     # loop bodies are replayed once; this caps deep nesting


def _literal(node, max_str=MAX_STR):
    """A value we can both display in a box and write back as source. A text longer than
    max_str is not offered as a parameter (None: no limit - see _check_patch)."""
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool):
            return {'type': 'bool', 'value': v}
        if isinstance(v, (int, float)):
            return {'type': 'num', 'value': v}
        if isinstance(v, str) and (max_str is None or len(v) <= max_str):
            return {'type': 'str', 'value': v}
        return None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)) \
            and isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, (int, float)) \
            and not isinstance(node.operand.value, bool):
        v = node.operand.value
        return {'type': 'num', 'value': -v if isinstance(node.op, ast.USub) else v}
    if isinstance(node, ast.Call):
        fn = ast.unparse(node.func)
        if fn in ('int', 'float') and len(node.args) == 1:           # num_loops = int(5e5)
            inner = _literal(node.args[0])
            if inner and inner['type'] == 'num':
                val = int(inner['value']) if fn == 'int' else float(inner['value'])
                return {'type': 'num', 'value': val, 'wrap': fn}
        base = fn.rsplit('.', 1)[-1]
        if base in ('linspace', 'arange') and len(node.args) == 3:   # xArray = np.linspace(a, b, n)
            vals = [_literal(a) for a in node.args]
            if all(v and v['type'] == 'num' for v in vals):
                return {'type': base, 'value': [v['value'] for v in vals]}
    return None


def _is_name_main(node):
    """`__name__ == '__main__'`, either way round."""
    if not (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)):
        return False
    sides = [node.left, node.comparators[0]]
    return (any(isinstance(s, ast.Name) and s.id == '__name__' for s in sides)
            and any(isinstance(s, ast.Constant) and s.value == '__main__' for s in sides))


def _static_bool(node, has_main=None):
    """True/False for `if True:` / `if False:` / `if 0:`; None when it is decided at run time.

    With `has_main` given, `if __name__ == '__main__':` is decided too: the runner imports a
    script that has main() and calls main() itself, so that block never runs; a script without
    main() is run as __main__, so it always does."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return node.value
        if isinstance(node.value, int):
            return bool(node.value)
    if has_main is not None and _is_name_main(node):
        return not has_main
    return None


def _comments(src):
    """line -> comment text, for both trailing comments and whole-line section headers."""
    trailing, headers = {}, {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type != tokenize.COMMENT:
                continue
            text = tok.string.lstrip('#').strip(' -=~*').strip()
            line = tok.start[0]
            if tok.line.strip().startswith('#'):
                if len(text) >= 3:              # skip '# ======' separator rules
                    headers[line] = text
            else:
                trailing.setdefault(line, text)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return trailing, headers


def _read_source(path):
    """-> (text, encoding), decoded the way Python decodes a source file: a UTF-8 BOM or a
    PEP 263 coding line is honoured (a BOM used to reach ast.parse as U+FEFF and fail, blind
    review #040).

    A byte that does not decode (a GBK comment in a file with no coding line - the runner's
    importlib/runpy accept that in a comment) is kept as a lone surrogate, so the patched copy
    writes it back byte for byte instead of replacing it; _view() is what the parser sees.

    Line ends are made universal, as a text-mode open() does: the AST counts lines at \\n, \\r\\n
    and \\r only, and the splice below must count them the same way (#105)."""
    with open(path, 'rb') as f:
        raw = f.read()
    enc, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    text = raw.decode(enc, errors='surrogateescape')
    return text.replace('\r\n', '\n').replace('\r', '\n'), enc


_SURROGATE = re.compile('[\udc80-\udcff]')


def _view(src):
    """the text the parser sees: an undecodable byte becomes U+FFFD, one character for one, so
    every column is the same in both"""
    return _SURROGATE.sub('\ufffd', src)


# ------------------------------------------------------------------ what a piece of code uses

def _arg_names(a):
    names = {x.arg for x in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)}
    for x in (a.vararg, a.kwarg):
        if x is not None:
            names.add(x.arg)
    return names


def _store_names(t):
    return {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}


def _uses(node, hidden=frozenset()):
    """(names read, names bound by :=, reads everything?) for code that runs where it stands.
    A lambda's arguments and a comprehension's variables are their own names, not reads.

    Walked with a list, not by recursion: `y = x + x + ... + x` with 1500 terms is a valid
    script (Python compiles it), but one Python frame per level overflowed the recursion
    limit and the RecursionError left scan() and everything that calls it (blind review
    round 2, G07). What is read does not depend on the order the nodes are visited in."""
    reads, binds, dyn = set(), set(), False
    todo = [(node, frozenset(hidden))]
    while todo:
        n, hid = todo.pop()
        if isinstance(n, ast.Name):
            if isinstance(n.ctx, ast.Load) and n.id not in hid:
                reads.add(n.id)
            continue
        if isinstance(n, ast.NamedExpr):
            todo.append((n.value, hid))
            if isinstance(n.target, ast.Name):
                binds.add(n.target.id)
            continue
        if isinstance(n, ast.Lambda):
            a = n.args
            for d in list(a.defaults) + [d for d in a.kw_defaults if d is not None]:
                todo.append((d, hid))
            todo.append((n.body, hid | _arg_names(a)))
            continue
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            inner = hid
            for k, g in enumerate(n.generators):
                todo.append((g.iter, hid if k == 0 else inner))     # the first runs outside
                inner = inner | _store_names(g.target)
                for c in g.ifs:
                    todo.append((c, inner))
            for part in ((n.key, n.value) if isinstance(n, ast.DictComp) else (n.elt,)):
                todo.append((part, inner))
            continue
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id not in hid:
            f = n.func.id
            if f in _DYN_ANY or (f in _DYN_BARE and not n.args and not n.keywords):
                dyn = True
        todo.extend((c, hid) for c in ast.iter_child_nodes(n))
    return reads, binds, dyn


def _deferred(value):
    """For `make = lambda: np.linspace(0, 1, num)` and `gen = (i * n for i in range(3))`:
    (what runs now: reads, := binds, dyn;  what runs when it is called or consumed: reads,
    dyn), else None. A lambda's body and a generator's element run later, not at the line
    that defines them: counted there, `num = 61` before it was offered as the value used and
    `num = 65` after it, the one the call really sees, was refused (round 2, N42). Only a
    lambda or generator stored straight into a name is known to be deferred; one passed to a
    call (`sorted(xs, key=lambda v: v * n)`) may run at once and is left to _uses."""
    if isinstance(value, ast.Lambda):
        a = value.args
        now_r, now_w, now_d = set(), set(), False
        for d in list(a.defaults) + [d for d in a.kw_defaults if d is not None]:
            r, w, dd = _uses(d)
            now_r |= r
            now_w |= w
            now_d = now_d or dd
        r, _w, dd = _uses(value.body, frozenset(_arg_names(a)))
        return now_r, now_w, now_d, r, dd
    if isinstance(value, ast.GeneratorExp):
        gens = value.generators
        now_r, now_w, now_d = _uses(gens[0].iter)       # the first iterable is taken at once
        later, later_d, walrus = set(), False, set()
        inner = frozenset()
        parts = []
        for k, g in enumerate(gens):
            if k:
                parts.append((g.iter, inner))
            inner = inner | _store_names(g.target)
            parts.extend((c, inner) for c in g.ifs)
        parts.append((value.elt, inner))
        for node, hid in parts:
            r, w, dd = _uses(node, hid)
            later |= r
            walrus |= w
            later_d = later_d or dd
        return now_r, now_w | walrus, now_d, later, later_d
    return None


# a generator handed to one of these is used up there and then; to one of _PARTIAL, read
# there but perhaps not to its end
_EAGER = ('list', 'tuple', 'set', 'frozenset', 'sorted', 'sum', 'min', 'max', 'dict')
_PARTIAL = ('any', 'all', 'next')


def _header(st):
    """The parts of a statement that run where it stands - not the bodies it holds."""
    if isinstance(st, ast.Assign):
        later = _deferred(st.value) if (len(st.targets) == 1
                                        and isinstance(st.targets[0], ast.Name)) else None
        if later is not None:
            v = st.value
            if isinstance(v, ast.Lambda):
                return list(v.args.defaults) + [d for d in v.args.kw_defaults if d is not None]
            return [v.generators[0].iter]
        return [st.value] + list(st.targets)
    if isinstance(st, (ast.If, ast.While)):
        return [st.test]
    if isinstance(st, (ast.For, ast.AsyncFor)):
        return [st.iter, st.target]
    if isinstance(st, (ast.With, ast.AsyncWith)):
        return list(st.items)
    if isinstance(st, ast.Try) or type(st).__name__ == 'TryStar':
        return [h.type for h in st.handlers if h.type is not None]
    if type(st).__name__ == 'Match':
        return [st.subject] + [c.pattern for c in st.cases] + [c.guard for c in st.cases
                                                               if c.guard is not None]
    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return list(st.decorator_list) + [st.args] + ([st.returns] if st.returns else [])
    if isinstance(st, ast.ClassDef):
        return list(st.decorator_list) + list(st.bases) + list(st.keywords)
    return [st]


def _breaks(body):
    """Does this loop body `break` out of its own loop somewhere?"""
    todo = list(body)
    while todo:
        n = todo.pop()
        if isinstance(n, ast.Break):
            return True
        if isinstance(n, (ast.For, ast.AsyncFor, ast.While, ast.FunctionDef,
                          ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            todo.extend(getattr(n, 'orelse', []))      # a break in a loop's else is ours
            continue
        todo.extend(ast.iter_child_nodes(n))
    return False


def _lam_uses(nodes, lam):
    """-> (escaped, spent): the stored lambdas / generators these nodes hand on, and the
    generators they use up. A call `make()` of a stored lambda runs its body there and then,
    and so does `list(gen)` for a generator; anything else - `register(cb)`, `h = make`,
    `zip(gen, xs)` - hands it on, to be run at a time the source does not tell (R12)."""
    esc, spent = set(), set()
    for root in nodes:
        if root is None:
            continue
        parent = {}
        todo = [root]
        while todo:
            n = todo.pop()
            for c in ast.iter_child_nodes(n):
                parent[id(c)] = n
                todo.append(c)
            if not (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in lam):
                continue
            par = parent.get(id(n))
            if lam[n.id]['kind'] != 'generator':
                if not (isinstance(par, ast.Call) and par.func is n):
                    esc.add(n.id)
                continue
            if isinstance(par, ast.Call) and isinstance(par.func, ast.Name) and par.args \
                    and par.args[0] is n and par.func.id in _EAGER + _PARTIAL:
                if par.func.id in _EAGER:
                    spent.add(n.id)
            elif isinstance(par, ast.comprehension) and par.iter is n and isinstance(
                    parent.get(id(par)), (ast.ListComp, ast.SetComp, ast.DictComp)):
                spent.add(n.id)
            elif isinstance(par, ast.Starred):
                spent.add(n.id)
            else:
                esc.add(n.id)
    return esc, spent - esc


def _target(t, binds, reads):
    """Names an assignment target binds; the parts of `a[i] = ...` / `a.b = ...` it reads."""
    if isinstance(t, ast.Name):
        binds.append(t.id)
    elif isinstance(t, (ast.Tuple, ast.List)):
        for e in t.elts:
            _target(e, binds, reads)
    elif isinstance(t, ast.Starred):
        _target(t.value, binds, reads)
    else:
        r, _w, _d = _uses(t)
        reads |= r


_SPACES = ('linspace', 'logspace', 'geomspace')


def _count_uses(nodes):
    """name -> [(line, where)] of its uses as a count: an argument of range(), or the number of
    points of np.linspace / logspace / geomspace, straight or through + - * //. A value there
    that is not whole is a TypeError when the script builds its sweep (blind review round 4,
    #105); one passed through int() or round() first is not a count here."""
    out = {}
    for root in nodes:
        for n in ast.walk(root):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            if isinstance(f, ast.Name) and f.id == 'range':
                parts, what = list(n.args), 'an argument of range()'
            elif (f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', None)) in _SPACES:
                parts = n.args[2:3] + [k.value for k in n.keywords if k.arg == 'num']
                what = 'the number of points of %s()' % _unparse(f)
            else:
                continue
            todo = list(parts)
            while todo:
                x = todo.pop()
                if isinstance(x, ast.Name):
                    out.setdefault(x.id, []).append((n.lineno, '%s at L%d' % (what, n.lineno)))
                elif isinstance(x, ast.BinOp):
                    todo += [x.left, x.right]
                elif isinstance(x, ast.UnaryOp):
                    todo.append(x.operand)
    return out


def _defs(body):
    """def/class statements of one scope, wherever they sit in its if/try/for/with blocks."""
    for st in body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield st
        elif isinstance(st, ast.If):
            yield from _defs(st.body)
            yield from _defs(st.orelse)
        elif isinstance(st, ast.Try) or type(st).__name__ == 'TryStar':
            yield from _defs(st.body)
            for h in st.handlers:
                yield from _defs(h.body)
            yield from _defs(st.orelse)
            yield from _defs(st.finalbody)
        elif isinstance(st, (ast.For, ast.AsyncFor, ast.While)):
            yield from _defs(st.body)
            yield from _defs(st.orelse)
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            yield from _defs(st.body)
        elif type(st).__name__ == 'Match':
            for c in st.cases:
                yield from _defs(c.body)


def _subtree(t):
    yield t
    for c in t.get_children():
        yield from _subtree(c)


def _kids(table):
    out = {}
    for c in table.get_children():
        out.setdefault((c.get_name(), c.get_lineno()), []).append(c)
    return out


_COMPS = ('listcomp', 'setcomp', 'dictcomp', 'genexpr')


def _class_early(cls):
    """Names a class body reads BEFORE it binds them itself. A class body looks a name up in
    the module until it has its own, so in `class C: m = n; n = 65` the `m = n` is the
    module's n - which symtable files under the class's own n (round 2, N43). Read after the
    class's own binding (`n = 5; m = n`), it is the class's, and not counted."""
    own, early = set(), set()
    for s in cls.body:
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts = list(s.decorator_list)
            if isinstance(s, ast.ClassDef):
                parts += list(s.bases) + [k.value for k in s.keywords]
            else:
                parts += list(s.args.defaults) + [d for d in s.args.kw_defaults if d is not None]
            for x in parts:
                early |= _uses(x)[0] - own
            own.add(s.name)
            continue
        got = _uses(s)[0]
        if isinstance(s, ast.AugAssign) and isinstance(s.target, ast.Name):
            got.add(s.target.id)
        early |= got - own
        own |= {n.id for n in ast.walk(s) if isinstance(n, ast.Name)
                and isinstance(n.ctx, (ast.Store, ast.Del))}
        if isinstance(s, (ast.Import, ast.ImportFrom)):
            own |= {(a.asname or a.name).split('.')[0] for a in s.names if a.name != '*'}
    return early


def _class_body(t):
    """The symbol tables of a class that run AT the class statement: its own body, the
    comprehensions in it and the bodies of the classes nested in it - not its methods."""
    yield t
    for c in t.get_children():
        if c.get_type() == 'class' or c.get_name() in _COMPS:
            yield from _class_body(c)


def _fn_info(defs, parent, want):
    """name -> {'reads', 'writes', 'how'} for the functions and classes of one scope: what they
    read of that scope's names when called (their bodies run later, not where they stand), and
    what they re-bind in it through `global` / `nonlocal`. `want(symbol)` picks the reads that
    reach this scope. Transitive: a function that calls another reads what that one reads.

    A class body is different: it runs once, at the class statement. What it reads is kept
    apart in 'now' - read there, and not again at every later use of the class or at the end
    of the scope, which made `n = 65` after `class C: m = n` look used (round 2, N43)."""
    kids = _kids(parent) if parent is not None else {}
    in_fn = parent is not None and parent.get_type() == 'function'     # main()'s closures
    fn = {}
    for d in defs:
        ts = kids.get((d.name, d.lineno)) or []
        reads, writes, dyn = set(), set(), not ts       # no symbol table: assume it reads anything
        now, dyn_now = set(), False
        for t in ts:
            body = ({id(x) for x in _class_body(t)} if isinstance(d, ast.ClassDef) else set())
            for sub in _subtree(t):
                here = id(sub) in body
                for s in sub.get_symbols():
                    nm = s.get_name()
                    if s.is_referenced() and want(s):
                        (now if here else reads).add(nm)
                    if s.is_assigned() and (s.is_nonlocal() if in_fn else s.is_declared_global()):
                        writes.add(nm)
                    if nm in ('globals', 'eval', 'exec') and s.is_referenced() and s.is_global():
                        if here:
                            dyn_now = True
                        else:
                            dyn = True
        f = fn.setdefault(d.name, {'reads': set(), 'writes': set(), 'how': []})
        f['reads'] = ALL if (dyn or f['reads'] == ALL) else (f['reads'] | reads)
        if isinstance(d, ast.ClassDef):
            old = f.get('now', set())
            f['now'] = ALL if (dyn_now or old == ALL) else (old | now)
        f['writes'] |= writes
        if writes:
            f['how'].append('`%s %s` in %s() (L%d)' % ('nonlocal' if in_fn else 'global',
                                                      ', '.join(sorted(writes)), d.name, d.lineno))
    changed = True
    while changed:
        changed = False
        for f in fn.values():
            if f['reads'] == ALL:
                continue
            for g in sorted(f['reads'] & set(fn)):
                gi = fn[g]
                if gi['reads'] == ALL:
                    f['reads'] = ALL
                    changed = True
                    break
                if gi['reads'] - f['reads']:
                    f['reads'] |= gi['reads']
                    changed = True
                if gi['writes'] - f['writes']:
                    f['writes'] |= gi['writes']
                    f['how'] = f['how'] + [h for h in gi['how'] if h not in f['how']]
                    changed = True
    return fn


# ------------------------------------------------------------------ star imports, statically

_MOD_CACHE = {}


def _find_module(modname, level, script_path):
    """The source file an import would load, found without importing anything."""
    parts = modname.split('.') if modname else []
    here = os.path.dirname(os.path.abspath(script_path))
    if level:
        base = here
        for _ in range(level - 1):
            base = os.path.dirname(base)
        bases = [base]
    else:
        bases, d = [], here
        for _ in range(4):              # the script's folder and those above it (the repo root)
            bases.append(d)
            up = os.path.dirname(d)
            if up == d:
                break
            d = up
        bases += [p for p in sys.path if p and os.path.isdir(p)]
    for b in bases:
        cand = os.path.join(b, *parts) if parts else b
        for f in (cand + '.py', os.path.join(cand, '__init__.py')):
            if os.path.isfile(f):
                return f
    return None


def _module_names(path, depth=0, deep=True):
    """{'own': names bound at the module's top level, 'exports': what `import *` gives, or None
    when that cannot be worked out}, from its source; nothing is executed. None if unreadable.
    deep=False skips following the module's own star imports ('exports' is then None)."""
    stamp = _stamp(path)
    if stamp is None:
        return None
    _DEPS.append((path, stamp))
    key = (os.path.abspath(path), stamp, deep)
    if key in _MOD_CACHE:
        return _MOD_CACHE[key]
    _MOD_CACHE[key] = None                              # an import cycle ends here
    try:
        tree = ast.parse(_view(_read_source(path)[0]))
    except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    own, star, unknown, all_ = set(), set(), False, None

    def walk(body):
        nonlocal unknown, all_
        for s in body:
            if isinstance(s, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                for t in (s.targets if isinstance(s, ast.Assign) else [s.target]):
                    own.update(_store_names(t))
                    if isinstance(t, ast.Name) and t.id == '__all__' and isinstance(s, ast.Assign) \
                            and isinstance(s.value, (ast.List, ast.Tuple)) \
                            and all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                                    for e in s.value.elts):
                        all_ = [e.value for e in s.value.elts]
            elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                own.add(s.name)
            elif isinstance(s, ast.Import):
                own.update((a.asname or a.name).split('.')[0] for a in s.names)
            elif isinstance(s, ast.ImportFrom):
                for a in s.names:
                    if a.name != '*':
                        own.add(a.asname or a.name)
                        continue
                    sub = _find_module(s.module or '', s.level, path) if deep and depth < 3 else None
                    got = _module_names(sub, depth + 1) if sub else None
                    if got and got['exports'] is not None:
                        star.update(got['exports'])
                    else:
                        unknown = True
            elif isinstance(s, ast.If):
                walk(s.body)
                walk(s.orelse)
            elif isinstance(s, ast.Try):
                walk(s.body)
                for h in s.handlers:
                    walk(h.body)
                walk(s.orelse)
                walk(s.finalbody)
            elif isinstance(s, (ast.For, ast.While, ast.With)):
                walk(s.body)

    walk(tree.body)
    if all_ is not None:
        exports = set(all_)
    elif unknown or not deep:
        exports = None
    else:
        exports = {n for n in own | star if not n.startswith('_')}
    out = {'own': own, 'exports': exports, 'path': path}
    _MOD_CACHE[key] = out
    return out


# ------------------------------------------------------------------ one namespace, in run order

class _Scope:
    """The module, or main(): what runs in it, as events in the order they run.

    read  - names whose current value is used here
    bind  - names that get a new value here (a literal parameter, or anything else)
    path  - the run-time `if`/`try` blocks the event sits in: an event whose path is a prefix
            of another's runs whenever that one did; any other event MAY run
    """

    def __init__(self, kind, fn):
        self.kind = kind
        self.fn = fn
        self.events = []
        self.excl = {}                  # block -> blocks that never run in the same pass
        self.n_blocks = 0
        # `make = lambda: ...` / `gen = (... for ...)` now bound to a name: name -> {'reads',
        # 'kind', 'line', 'path', 'maybe'}. Kept apart from the defs in self.fn: a def may be
        # called at any time (main() after the module), a stored lambda runs where its name
        # is called, and nobody can say when once it has been handed on (round 3, R12/R13).
        self.lam = {}
        self.cur = None                 # the statement being walked: its lambda uses
        # name (or ALL) -> [why, ...]: may be read after the scope's end by a stored lambda /
        # generator that was handed on - at a time nobody can tell (see _effective for the
        # ones a def reaches)
        self.end_unsure = {}
        self.lam_reads = set()          # every name a stored lambda / generator's body reads

    def block(self):
        self.n_blocks += 1
        return self.n_blocks

    def exclusive(self, blocks):
        for b in blocks:
            self.excl.setdefault(b, set()).update(x for x in blocks if x != b)

    def lam_text(self, names):
        return ', '.join('`%s` (a stored %s, L%d)' % (n, self.lam[n]['kind'], self.lam[n]['line'])
                         for n in sorted(names) if n in self.lam)

    def expand(self, names, skip=()):
        """What using `names` reads: through the functions this scope defines and the lambdas
        / generators stored in its names, as deep as they go - `run()` whose body calls a
        stored `make()` reads what make's body reads (round 3, R13; it used to stop at one
        level). -> (names, reached only through a stored lambda, def entries that re-bind).
        ALL for names when anything on the way may read anything."""
        out, via, writers, every = set(names), set(), [], False
        direct = set(names)
        seen = set()
        todo = [(n, False) for n in names if n not in skip]
        while todo:
            n, thru = todo.pop()
            if (n, thru) in seen:
                continue
            seen.add((n, thru))
            if n in self.lam:
                ent, t2 = self.lam[n], True
            elif n in self.fn:
                ent, t2 = self.fn[n], thru
                if ent['writes'] and all(ent is not w for w in writers):
                    writers.append(ent)
            else:
                continue
            if ent['reads'] == ALL:
                every = True
                continue
            for m in ent['reads']:
                (via if t2 else direct).add(m)
                todo.append((m, t2))
        if every:
            return ALL, set(), writers
        return out | direct | via, via - direct, writers

    def read(self, names, dyn, path, line, branch, by=''):
        names = set(names)
        if not names and not dyn:
            return
        writers, unsure, why = [], None, ''
        if dyn:
            names = ALL
        else:
            # using a function this scope defines is using what it reads - and what it
            # re-binds through `global` / `nonlocal`; so is calling a stored lambda. A
            # stored lambda or generator handed on here (`register(cb)`, `h = gen`) is read
            # at a time nobody can tell: what its body reads is an UNSURE read (R12)
            cur = self.cur or {}
            handed = {n for n in names if n in self.lam and n in cur.get('esc', ())}
            which = {n for n in names if n in self.lam and self.lam[n]['maybe']} - handed
            esc = handed | which
            names, _via, writers = self.expand(names, skip=esc)
            if esc:
                u, _v, w2 = self.expand(esc)
                writers += [w for w in w2 if all(w is not x for x in writers)]
                unsure = u if u == ALL else (u - esc)
                parts = []
                if handed:
                    parts.append('%s %s handed on at L%d and may run at any time after it'
                                 % (self.lam_text(handed), 'is' if len(handed) == 1 else 'are',
                                    line))
                if which:
                    parts.append('%s %s used at L%d, but which one %s then depends on a '
                                 'run-time block' % (self.lam_text(which),
                                                     'is' if len(which) == 1 else 'are', line,
                                                     'it is' if len(which) == 1 else 'they are'))
                why = '; '.join(parts)
                if handed:
                    # handed on: whatever holds it may call it after the scope's end too
                    u2, _v2, _w2 = self.expand(handed)
                    t2 = parts[0]
                    for m in ([ALL] if u2 == ALL else sorted(u2 - handed)):
                        self.end_unsure.setdefault(m, []).append(t2)
        self.events.append({'k': 'read', 'names': names, 'path': path, 'line': line,
                            'branch': branch, 'by': by})
        if unsure:
            self.events.append({'k': 'read', 'names': unsure, 'path': path, 'line': line,
                                'branch': branch, 'by': by, 'unsure': True, 'why': why})
        for f in writers:
            self.events.append({'k': 'bind', 'names': set(f['writes']), 'path': path,
                                'line': line, 'branch': branch, 'maybe': True,
                                'how': '; '.join(f['how']), 'param': None})

    def bind(self, names, path, line, branch, how='', param=None):
        if names:
            for n in names:
                ent = self.lam.get(n)
                if ent is None:
                    continue
                if ent['path'][:len(path)] == path:
                    del self.lam[n]         # re-bound: `f = 3` after `f = lambda: n` (R12)
                else:
                    ent['maybe'] = True     # re-bound only in a run-time block: it may still be
            self.events.append({'k': 'bind', 'names': set(names), 'path': path, 'line': line,
                                'branch': branch, 'how': how, 'param': param})

    def defer(self, name, reads, dyn, kind, line, path, was=None):
        """`name = lambda: ...` / `name = (... for ...)`: a call of `name` from here on reads
        what the body reads, as a call of a def'd function does (see read()). Unlike a def it
        is NOT read again at the end of the scope, unless it was handed on or a def reaches
        it - and then at a time the panel cannot know (round 3, R12). `was`: the entry this
        one replaced inside a run-time block, which may still be the one bound."""
        ent = {'reads': ALL if dyn else set(reads), 'kind': kind, 'line': line, 'path': path,
               'maybe': False}
        if not dyn:
            self.lam_reads |= set(reads)    # visibly read, even if the lambda never runs
        if was is not None and was.get('maybe'):
            ent['maybe'] = True
            ent['reads'] = ALL if ALL in (ent['reads'], was['reads']) else ent['reads'] | was['reads']
        self.lam[name] = ent

    def begin(self, st, path):
        """Before a statement runs: which stored lambdas / generators it hands on (escaped)
        and which generators it uses up (spent), so read() and finish() can tell."""
        self.cur = None
        if st is None or not self.lam:
            return
        esc, spent = set(), set()
        nodes = _header(st)
        if isinstance(st, (ast.For, ast.AsyncFor)) and isinstance(st.iter, ast.Name) \
                and st.iter.id in self.lam:
            nodes = [n for n in nodes if n is not st.iter]
            if self.lam[st.iter.id]['kind'] != 'generator':
                esc.add(st.iter.id)
            elif not _breaks(st.body):
                spent.add(st.iter.id)           # a loop with no break uses it all up
        e2, s2 = _lam_uses(nodes, self.lam)
        self.cur = {'esc': esc | e2, 'spent': spent | s2, 'path': path}

    def finish(self):
        """After it: a generator used up where it was made (or less conditionally) reads
        nothing any more; used up only in a run-time block, it MAY still read."""
        cur, self.cur = self.cur, None
        for n in (cur or {}).get('spent', ()):
            ent = self.lam.get(n)
            if ent is None or n in cur['esc']:
                continue
            if ent['path'][:len(cur['path'])] == cur['path']:
                del self.lam[n]
            else:
                ent['maybe'] = True


def _stmt_text(st, n=60):
    try:
        text = ast.unparse(st).split('\n', 1)[0]
    except Exception:                                                # noqa: BLE001
        return ''
    return text if len(text) <= n else text[:n - 3] + '...'


def _unparse(node):
    """ast.unparse, which recurses: a test nested too deeply is named '...' (G07)"""
    try:
        return ast.unparse(node)
    except Exception:                                                # noqa: BLE001
        return '...'


def _scan_source(src, path, analyse=True, long_ok=()):
    """`long_ok`: keys ('name@line') whose text may be longer than MAX_STR - the lines an
    override has just written, when the patched copy is scanned again (round 2, G06)."""
    src = _view(src)
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return {'error': 'cannot parse %s: line %s, %s' % (os.path.basename(path), e.lineno, e.msg),
                'params': []}
    except ValueError as e:                      # a NUL byte: 3.10 raises ValueError for it
        return {'error': 'cannot parse %s: %s' % (os.path.basename(path), e), 'params': []}
    except (RecursionError, MemoryError) as e:   # an expression nested too deeply (G07)
        return {'error': 'cannot parse %s: an expression is nested too deeply for the panel '
                         'to read (%s)' % (os.path.basename(path), type(e).__name__), 'params': []}

    trailing, headers = _comments(src)
    lines = src.split('\n')
    params = []
    order = [0]

    def ccol(lineno, col):
        """ast counts columns in UTF-8 bytes; the splice slices characters, so a `µ` earlier on
        the line used to push the cut right and turn 96 -> 120 into 9120 (blind review #034)"""
        return len(lines[lineno - 1].encode('utf-8')[:col].decode('utf-8'))

    def group_for(line):
        """the nearest '# ---- ... ----' header above this line"""
        best = None
        for hl, text in headers.items():
            if hl < line and text and (best is None or hl > best[0]):
                best = (hl, text)
        return best[1][:60] if best and line - best[0] <= 12 else ''

    # the runner's notion: main() bound at the top level (run_block.has_main)
    has_main = any((isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == 'main')
                   or (isinstance(n, ast.ImportFrom) and any((a.asname or a.name) == 'main'
                                                             for a in n.names))
                   or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'main'
                                                         for t in n.targets))
                   for n in tree.body)
    main = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main'), None)

    try:
        top = symtable.symtable(src, path, 'exec') if analyse else None
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        top = None                       # a statement too deep for symtable (G07): see below
    main_t = None
    if top is not None and main is not None:
        main_t = (_kids(top).get(('main', main.lineno)) or [None])[0]
    if main_t is not None:
        main_locals = {s.get_name() for s in main_t.get_symbols()
                       if (s.is_local() or s.is_parameter()) and not s.is_declared_global()}
    elif main is not None:
        main_locals = _arg_names(main.args) | {n.id for n in ast.walk(main) if isinstance(n, ast.Name)
                                               and isinstance(n.ctx, (ast.Store, ast.Del))}
    else:
        main_locals = set()

    if analyse and top is not None:
        mod_fn = _fn_info(list(_defs(tree.body)), top, lambda s: s.is_global())
        main_fn = (_fn_info(list(_defs(main.body)), main_t,
                            lambda s: s.is_free() and s.get_name() in main_locals)
                   if main is not None else {})
    else:
        mod_fn = {d.name: {'reads': ALL, 'writes': set(), 'how': []} for d in _defs(tree.body)}
        main_fn = ({d.name: {'reads': ALL, 'writes': set(), 'how': []} for d in _defs(main.body)}
                   if main is not None else {})

    def walk(body, S, branch, live, cond, path, params_ok=True):
        for st in body:
            # which stored lambdas / generators the statement hands on or uses up: read()
            # needs it, and a generator used up reads nothing after it (round 3, R12)
            S.begin(st if live else None, path)
            saved = S.cur
            one(st, S, branch, live, cond, path, params_ok, saved)
            S.cur = saved
            S.finish()

    def one(st, S, branch, live, cond, path, params_ok, saved):
        ln = getattr(st, 'lineno', 0)
        if isinstance(st, ast.Assign):
            single = len(st.targets) == 1 and isinstance(st.targets[0], ast.Name)
            name = st.targets[0].id if single else None
            lit = (_literal(st.value, None if '%s@%d' % (name, st.value.lineno) in long_ok
                            else MAX_STR) if single else None)
            is_param = (params_ok and single and lit is not None and name not in SKIP_NAMES
                        and not name.startswith('_') and st.value.lineno == st.value.end_lineno)
            p = None
            if is_param:
                order[0] += 1
                p = {'key': '%s@%d' % (name, st.value.lineno), 'name': name,
                     'scope': S.kind, 'branch': branch, 'order': order[0],
                     'line': st.value.lineno,
                     'col': ccol(st.value.lineno, st.value.col_offset),
                     'end_col': ccol(st.value.end_lineno, st.value.end_col_offset),
                     'source': ast.unparse(st.value)[:120],
                     'help': trailing.get(st.lineno, ''),
                     'group': group_for(st.lineno),
                     'live': live,
                     # inside an `if` that is decided at run time: whether this
                     # line runs cannot be known from the source
                     'conditional': cond > 0,
                     **lit}
                params.append(p)
            if not live:
                return
            later = _deferred(st.value) if single else None
            if later is not None:
                reads, walrus, dyn = later[0], later[1], later[2]
            else:
                reads, walrus, dyn = _uses(st.value)
            was = S.lam.get(name) if later is not None else None
            binds = []
            for t in st.targets:
                _target(t, binds, reads)
            S.read(reads, dyn, path, ln, branch, by=_stmt_text(st.targets[0], 30))
            S.bind(walrus, path, ln, branch, how='`%s`' % _stmt_text(st))
            S.bind(binds, path, ln, branch, how='' if p else '`%s`' % _stmt_text(st), param=p)
            if later is not None:
                # from here on a call `make()` reads what its body reads; the entry it
                # replaces inside a run-time block may still be the one bound (R12)
                S.defer(name, later[3], later[4],
                        'lambda' if isinstance(st.value, ast.Lambda) else 'generator',
                        ln, path, was)
        elif isinstance(st, (ast.AugAssign, ast.AnnAssign)):
            if not live:
                return
            reads, walrus, dyn = (_uses(st.value) if st.value is not None
                                  else (set(), set(), False))
            if isinstance(st, ast.AnnAssign):
                r, _w, _d = _uses(st.annotation)
                reads |= r
            binds = []
            if isinstance(st.target, ast.Name):
                if isinstance(st, ast.AugAssign):
                    reads.add(st.target.id)
                if isinstance(st, ast.AugAssign) or st.value is not None:
                    binds.append(st.target.id)
            else:
                _target(st.target, binds, reads)
            S.read(reads, dyn, path, ln, branch, by=_stmt_text(st.target, 30))
            S.bind(walrus | set(binds), path, ln, branch, how='`%s`' % _stmt_text(st))
        elif isinstance(st, ast.Delete):
            if not live:
                return
            binds, reads = [], set()
            for t in st.targets:
                _target(t, binds, reads)
            S.read(reads, False, path, ln, branch)
            S.bind(binds, path, ln, branch, how='`%s`' % _stmt_text(st))
        elif isinstance(st, (ast.Import, ast.ImportFrom)):
            if not live:
                return
            for a in st.names:
                if a.name == '*':
                    S.events.append({'k': 'bind', 'names': None, 'path': path, 'line': ln,
                                     'branch': branch, 'param': None,
                                     'star': (st.module or '', st.level),
                                     'how': '`%s`' % _stmt_text(st)})
                else:
                    nm = (a.asname or a.name) if isinstance(st, ast.ImportFrom) \
                        else (a.asname or a.name.split('.')[0])
                    S.bind([nm], path, ln, branch, how='`%s`' % _stmt_text(st))
        elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not live:
                return
            # decorators, default values, annotations and a class body run HERE; a
            # function body runs when it is called (see _Scope.read)
            parts = list(st.decorator_list)
            if isinstance(st, ast.ClassDef):
                parts += list(st.bases) + [k.value for k in st.keywords]
            else:
                a = st.args
                parts += list(a.defaults) + [d for d in a.kw_defaults if d is not None]
                parts += [x.annotation for x in list(a.posonlyargs) + list(a.args)
                          + list(a.kwonlyargs) + [a.vararg, a.kwarg]
                          if x is not None and x.annotation is not None]
                if st.returns is not None:
                    parts.append(st.returns)
            reads, dyn = set(), False
            for x in parts:
                r, _w, d = _uses(x)
                reads |= r
                dyn = dyn or d
            if isinstance(st, ast.ClassDef):
                # its body runs here; its methods are counted here too, as a class
                # body may call them - but only they are read again later (N43)
                f = S.fn.get(st.name)
                if f is not None:
                    for part in (f.get('now', set()), f['reads']):
                        if part == ALL:
                            dyn = True
                        else:
                            reads |= part
                if S.kind == 'module':
                    reads |= _class_early(st)
            S.read(reads, dyn, path, ln, branch,
                   by='a default argument of %s()' % st.name
                   if not isinstance(st, ast.ClassDef) else 'class %s' % st.name)
            S.bind([st.name], path, ln, branch, how='`def %s`' % st.name)
        elif isinstance(st, ast.If):
            # a common habit: alternatives kept in `if True:` / `if False:` blocks; an assignment
            # in the branch that never runs must NOT be offered as an editable parameter.
            # An `if` on a run-time value is different: BOTH branches may run, and neither
            # may be called overwritten by the other (F20).
            truth = _static_bool(st.test, has_main)
            test = _unparse(st.test)[:30]
            if live and truth is None:
                r, w, d = _uses(st.test)
                S.read(r, d, path, ln, branch, by='if %s' % test)
                S.bind(w, path, ln, branch, how='`%s`' % test)
            if truth is None:
                b1, b2 = S.block(), S.block()
                S.exclusive([b1, b2])
                walk(st.body, S, ('%s / if %s' % (branch, test)).strip(' /'), live,
                     cond + 1, path + (b1,), params_ok)
                walk(st.orelse, S, ('%s / else of if %s' % (branch, test)).strip(' /'), live,
                     cond + 1, path + (b2,), params_ok)
            else:
                walk(st.body, S, ('%s / if %s' % (branch, test)).strip(' /'),
                     live and truth, cond, path, params_ok)
                walk(st.orelse, S, ('%s / else of if %s' % (branch, test)).strip(' /'),
                     live and not truth, cond, path, params_ok)
        elif isinstance(st, ast.Try) or type(st).__name__ == 'TryStar':
            # `x = 96` up top and `try: x = 40 except: x = -1` further down: the try body
            # runs unless something raises, the handler only if it does. Both used to be
            # invisible here, so the line up top was offered as THE value and an edit to
            # it changed nothing (agent B, finding B2). Now they are conditional
            # candidates like the branches of a run-time if; `finally` runs regardless.
            bt = S.block()
            bh = [S.block() for _h in st.handlers]
            bo = S.block()
            S.exclusive(bh + [bo])
            walk(st.body, S, ('%s / try' % branch).strip(' /'), live, cond + 1,
                 path + (bt,), params_ok)
            for h, b in zip(st.handlers, bh):
                hb = ('%s / except' % branch).strip(' /')
                S.cur = saved                   # the handler's own uses, after the body's
                if live:
                    if h.type is not None:
                        r, _w, d = _uses(h.type)
                        S.read(r, d, path + (b,), h.lineno, hb)
                    if h.name:
                        S.bind([h.name], path + (b,), h.lineno, hb, how='`except ... as %s`' % h.name)
                walk(h.body, S, hb, live, cond + 1, path + (b,), params_ok)
            walk(st.orelse, S, ('%s / else of try' % branch).strip(' /'), live, cond + 1,
                 path + (bo,), params_ok)
            walk(st.finalbody, S, branch, live, cond, path, params_ok)
        elif isinstance(st, (ast.For, ast.AsyncFor, ast.While)):
            is_for = not isinstance(st, ast.While)
            if live:
                r, w, d = _uses(st.iter if is_for else st.test)
                S.read(r, d, path, ln, branch, by='the loop at L%d' % ln)
                S.bind(w, path, ln, branch, how='`%s`' % _stmt_text(st))
            start = len(S.events)
            if live and is_for:
                binds, reads = [], set()
                _target(st.target, binds, reads)
                S.read(reads, False, path, ln, branch)
                S.bind(binds, path, ln, branch, how='`for %s in ...`' % _stmt_text(st.target, 30))
            walk(st.body, S, branch, live, cond, path, params_ok)
            S.cur = saved
            if live and is_for and isinstance(st.iter, ast.Name) and st.iter.id in S.lam:
                # `for v in gen:` works out the next element at every pass, with what the
                # body has just left: read again at the end of the body (R12)
                S.read({st.iter.id}, False, path, ln, branch, by='the loop at L%d' % ln)
            if live and not is_for:
                r, w, d = _uses(st.test)
                S.read(r, d, path, ln, branch, by='the loop at L%d' % ln)
            if live and len(S.events) < _MAX_EVENTS:
                # the body runs again: what its end left is what its top reads next time
                # round, so a value set late in a loop body is not dead (blind review #112)
                S.events.extend(S.events[start:])
            bo = S.block()
            walk(st.orelse, S, ('%s / else of loop' % branch).strip(' /'), live, cond + 1,
                 path + (bo,), False)
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            if live:
                for it in st.items:
                    r, w, d = _uses(it.context_expr)
                    S.read(r, d, path, ln, branch)
                    if it.optional_vars is not None:
                        binds, reads = [], set()
                        _target(it.optional_vars, binds, reads)
                        S.read(reads, False, path, ln, branch)
                        S.bind(set(binds) | w, path, ln, branch,
                               how='`with ... as %s`' % _stmt_text(it.optional_vars, 30))
            walk(st.body, S, branch, live, cond, path, params_ok)
        elif type(st).__name__ == 'Match':
            if live:
                r, w, d = _uses(st.subject)
                S.read(r, d, path, ln, branch)
            blocks = [S.block() for _c in st.cases]
            S.exclusive(blocks)
            for c, b in zip(st.cases, blocks):
                cb = ('%s / case' % branch).strip(' /')
                S.cur = saved
                if live:
                    caps = [x for node in ast.walk(c.pattern)
                            for x in (getattr(node, 'name', None), getattr(node, 'rest', None))
                            if isinstance(x, str)]
                    r, _w, d = _uses(c.pattern)
                    if c.guard is not None:
                        r2, _w2, d2 = _uses(c.guard)
                        r, d = r | r2, d or d2
                    S.bind(caps, path + (b,), c.pattern.lineno, cb, how='`case`')
                    S.read(r, d, path + (b,), c.pattern.lineno, cb)
                walk(c.body, S, cb, live, cond + 1, path + (b,), False)
        elif isinstance(st, (ast.Global, ast.Nonlocal, ast.Pass, ast.Break, ast.Continue)):
            return
        elif live:                                   # Expr, Return, Raise, Assert, ...
            r, w, d = _uses(st)
            S.read(r, d, path, ln, branch, by=_stmt_text(st, 30))
            S.bind(w, path, ln, branch, how='`%s`' % _stmt_text(st))

    mod = _Scope('module', mod_fn)
    walk(tree.body, mod, '', True, 0, (), params_ok=True)
    main_params, sig, msc = [], None, None
    if main:
        msc = _Scope('main', main_fn)
        walk(main.body, msc, '', True, 0, (), params_ok=True)
        sig = [a.arg for a in main.args.args]
        # the arguments main() declares: passed as kwargs, so the source is never touched for these
        defaults = list(main.args.defaults)
        pad = len(main.args.args) - len(defaults)
        for i, a in enumerate(main.args.args):
            d = defaults[i - pad] if i >= pad else None
            lit = _literal(d) if d is not None else None
            main_params.append({'name': a.arg,
                                'value': lit['value'] if lit else None,
                                'type': lit['type'] if lit else 'num',
                                'source': ast.unparse(d) if d is not None else '(no default)'})

    # module-level parameters come first, as they always have: the dialog groups by this order
    params.sort(key=lambda p: (0 if p['scope'] == 'module' else 1, p['order']))

    # A name assigned both at module level and inside main() is two rows that must be told
    # apart by line, the way apply_overrides() and verify() look them up (blind review #041)
    count = {}
    for p in params:
        count[p['name']] = count.get(p['name'], 0) + 1
    for p in params:
        p['ambiguous'] = count[p['name']] > 1

    if analyse:
        # a whole number that a count is built from must stay whole (#105): main()'s own
        # names count where main() uses them, the module's everywhere else
        try:
            in_main = _count_uses([main]) if main is not None else {}
            counts = {'main': {k: v for k, v in in_main.items() if k in main_locals},
                      'module': _count_uses([s for s in tree.body if s is not main])}
            for k, v in in_main.items():
                if k not in main_locals:
                    counts['module'].setdefault(k, []).extend(v)
            for p in params:
                uses = sorted(counts[p['scope']].get(p['name']) or [])
                if p['type'] == 'num' and type(p['value']) is int and not p.get('wrap') and uses:
                    # the use after the line if there is one: the one its value reaches
                    p['count'] = next((w for ln, w in uses if ln > p['line']), uses[0][1])
        except Exception:                                               # noqa: BLE001
            pass            # no count known: the value is checked as any number is
        try:
            for S in [mod] + ([msc] if msc else []):
                # main()'s own names go in for both scopes: the module's for "main() has its
                # own", main()'s for "nothing in main() reads it" (round 4, #104)
                _effective(S, [p for p in params if p['scope'] == S.kind], path, tree,
                           main_locals if main is not None else None)
        except Exception as e:                                          # noqa: BLE001
            # never a crash in the dialog or the run; and never a silent "effective" either
            for p in params:
                if p['live']:
                    p['effective'] = None
                    p['why'] = ('the panel could not work out which line the script uses '
                                '(%s: %s)' % (type(e).__name__, e))
                else:
                    p['effective'] = False
                    p['why'] = 'never runs (%s)' % (p['branch'] or 'dead branch')
        else:
            try:
                _class_notes(tree, path, params, main, main_locals)
            except Exception:                                           # noqa: BLE001
                pass        # an extra: without it the scan says what it said before (round 4)

    return {'error': None, 'params': params, 'main_args': sig, 'main_params': main_params,
            'has_main': main is not None, 'n_lines': src.count('\n') + 1}


def _effective(S, params, path, tree, main_locals):
    """Which assignments does an edit reach? Patching one that nothing reads silently does
    nothing - exactly the failure this has to rule out.

      effective True   what this line sets is read, and nothing that runs at run-time
                       discretion can replace it first
      effective False  never runs, or re-bound before anything reads it
      effective None   CONDITIONAL: inside an `if` decided at run time, read only inside one,
                       or something that may run (an `if`, a `global` in a function, a star
                       import that cannot be checked) may replace it before it is read. The
                       source cannot say; the UI and the run say so.
                       With 'unsure' set as well: nothing is known to read it, but a stored
                       lambda / generator that was handed on, or that a function reaches, may
                       - at a time the source does not tell. Refused like False (round 3,
                       R12/R13): neither "used" nor "no effect" can be said.
    """
    ev = S.events
    real, dyn = set(), False
    for e in ev:
        if e['k'] == 'read':
            if e['names'] == ALL:
                if not e.get('unsure'):
                    dyn = True
            else:
                real |= e['names']
    # bodies of the functions this scope defines run when called - main() after the module
    # has run, anything else at some point: at the end of the scope they read what they read
    end_reads, end_kills = set(), []
    end_unsure = {k: list(v) for k, v in S.end_unsure.items()}
    for fname, f in S.fn.items():
        if f['reads'] == ALL:
            dyn = True
        else:
            end_reads |= f['reads']
            # a stored lambda / generator a function calls is read with the function. main()
            # is called once the module has run (the runner does it), so what it reaches
            # then is read then; when any other function is called after this point the
            # source does not tell (round 3, R13)
            got, via, _w = S.expand(f['reads'])
            if S.kind == 'module' and main_locals is not None and fname == 'main':
                if got == ALL:
                    dyn = True
                else:
                    end_reads |= got
            elif got == ALL:
                end_unsure.setdefault(ALL, []).append(
                    '%s() reaches a stored lambda or generator that may read any name' % fname)
            else:
                for m in via:
                    end_unsure.setdefault(m, []).append(
                        '%s() reads it through a stored lambda or generator, and when %s() is '
                        'called after this line the source does not tell' % (fname, fname))
        if f['writes']:
            end_kills.append({'k': 'bind', 'names': set(f['writes']), 'maybe': True,
                              'line': 0, 'how': '; '.join(f['how']), 'param': None})
    real |= end_reads
    real |= {m for m in end_unsure if m != ALL}
    real |= S.lam_reads

    def implicit(name):
        # a name nothing visibly reads: how it is used cannot be seen, so the value it ends
        # with is taken to be the one used - the rule this analysis always had
        return dyn or name not in real

    star_cache = {}

    def star_names(e):
        if e['star'] not in star_cache:
            f = _find_module(e['star'][0], e['star'][1], path)
            got = _module_names(f) if f else None
            star_cache[e['star']] = (got or {}).get('exports') if got else None
        return star_cache[e['star']]

    def excluded(a, b):
        return any(x in S.excl.get(y, ()) for y in b for x in a)

    def follow(i, name, P=None):
        """What becomes of the value `name` gets at event i: read / killed / unread."""
        P = ev[i]['path'] if P is None else P
        maybe, creads, shadows, ureads = [], [], [], []
        for j in range(i + 1, len(ev)):
            e = ev[j]
            # after `if c: x = 7`, a read further inside that block sees the 7, not this value
            if excluded(e['path'], P) or any(e['path'][:len(q)] == q for q in shadows):
                continue
            sure = e['path'] == P[:len(e['path'])]
            if e['k'] == 'read':
                if e['names'] == ALL or name in e['names']:
                    if e.get('unsure'):
                        ureads.append(e)            # may read it - when, nobody can tell
                        continue
                    if sure:
                        return {'kind': 'read', 'at': e, 'j': j, 'maybe': maybe, 'creads': creads,
                                'ureads': ureads}
                    creads.append(e)
                continue
            soft = bool(e.get('maybe'))
            if 'star' in e:
                names = star_names(e)
                if names is None:
                    hit, soft = True, True
                    e = dict(e, how=e['how'] + ' (its source could not be checked)')
                else:
                    hit = name in names
            else:
                hit = name in e['names']
            if not hit:
                continue
            if e is ev[i]:
                # the next pass of a loop running this same line again: not a competitor
                return {'kind': 'again', 'at': e, 'maybe': maybe, 'creads': creads,
                        'ureads': ureads}
            if sure and not soft:
                return {'kind': 'killed', 'at': e, 'maybe': maybe, 'creads': creads,
                        'ureads': ureads}
            maybe.append(e)
            if not soft:
                shadows.append(e['path'])
        kills = [k for k in end_kills if name in k['names']]
        if implicit(name) or name in end_reads:
            return {'kind': 'read', 'at': None, 'maybe': maybe + kills, 'creads': creads,
                    'implicit': implicit(name), 'ureads': ureads}
        return {'kind': 'unread', 'at': None, 'maybe': maybe, 'creads': creads,
                'ureads': ureads, 'uend': end_unsure.get(name, []) + end_unsure.get(ALL, [])}

    def fate(i, name):
        """The best of what becomes of the value bound at event i, over every pass of the loops
        it sits in (a loop body is replayed, so the same bind appears more than once)."""
        outs = [follow(k, name) for k, e in enumerate(ev) if e is ev[i]]
        for want in (lambda o: o['kind'] == 'read' and not o['maybe'],
                     lambda o: o['kind'] == 'read',
                     lambda o: o['creads'] and o['kind'] == 'killed',
                     lambda o: o['creads'],
                     lambda o: True):
            hit = [o for o in outs if want(o)]
            if hit:
                return hit[0]

    def replaced(o):
        """a different value may be what gets used: a conditional re-bind before the read, or
        read only inside a run-time block and then re-bound. Read only inside a block, with
        nothing competing, is not that: the edit is what that block uses."""
        return (o['kind'] == 'read' and o['maybe']) or o['kind'] == 'killed'

    def through(outs, name, P):
        """`start = 8` read once, by `xs = np.linspace(start, ...)`, reaches the measurement
        only if `xs` does: when the one statement that reads the value stores it in names
        that may themselves be replaced first (`if use_fake: xs = ...`),
        say so. One level deep; None when the value goes through or is used elsewhere too."""
        msg = None
        for o in outs:
            if not (o['kind'] == 'read' and not o['maybe'] and o.get('at') is not None):
                continue
            j = o['j']
            rest = follow(j, name, P)
            if rest['kind'] == 'read' or rest['creads']:
                return None                             # read again later: used there too
            k1 = j + 1
            while k1 < len(ev) and ev[k1].get('unsure'):
                k1 += 1                                 # beside the read, not what stores it
            nxt = ev[k1] if k1 < len(ev) else None
            if not (nxt and nxt['k'] == 'bind' and nxt['line'] == o['at']['line']
                    and nxt.get('names') and 'star' not in nxt and not nxt.get('maybe')):
                return None                             # not stored: a call, an argument...
            txt = None
            for c in sorted(nxt['names']):
                oc = fate(k1, c)
                if not replaced(oc):
                    return None                         # goes through
                if oc['kind'] == 'read':
                    t = maybe_text(oc['maybe'])
                elif oc['creads']:
                    t = 'is read only inside `%s` (L%d), and replaced at line %d otherwise' % (
                        oc['creads'][0]['branch'] or 'a run-time block', oc['creads'][0]['line'],
                        oc['at']['line'])
                else:
                    t = 'is overwritten at line %d before anything reads it' % oc['at']['line']
                txt = txt or 'read at L%d into `%s`, which %s' % (o['at']['line'], c, t)
            msg = msg or txt
        return msg

    occ = {}
    for i, e in enumerate(ev):
        if e['k'] == 'bind' and e.get('param') is not None:
            occ.setdefault(id(e['param']), []).append(i)

    status = {}
    for p in params:
        if not p['live']:
            continue
        outs = [follow(i, p['name']) for i in occ.get(id(p), [])]
        clean = [o for o in outs if o['kind'] == 'read' and not o['maybe']]
        dirty = [o for o in outs if o['kind'] == 'read' and o['maybe']]
        conds = [o for o in outs if o['creads']]
        if clean:
            status[id(p)] = ('sure', clean[0])
        elif dirty:
            status[id(p)] = ('maybe', dirty[0])
        elif any(o['kind'] == 'killed' for o in conds):
            # read inside a run-time block, then re-bound for everything after it
            status[id(p)] = ('cond', [o for o in conds if o['kind'] == 'killed'][0])
        elif conds:
            # read only inside a run-time block, and nothing else competes: this IS the
            # value that block uses
            status[id(p)] = ('sure', conds[0])
        elif any(o.get('ureads') or o.get('uend') for o in outs):
            # nothing is known to read it, but a stored lambda / generator may, at a time
            # the source does not tell: neither "used" nor "no effect" (round 3, R12/R13)
            status[id(p)] = ('unsure', [o for o in outs if o.get('ureads') or o.get('uend')][0])
        else:
            status[id(p)] = ('dead', outs[0] if outs else {'kind': 'unread', 'at': None,
                                                          'maybe': [], 'creads': []})

    groups = {}
    for p in params:
        groups.setdefault(p['name'], []).append(p)

    def lines_of(evs):
        return ', '.join('L%d' % n for n in sorted({x['line'] for x in evs if x.get('line')}))

    def maybe_text(kills):
        plain = [k for k in kills if not k.get('maybe') and 'star' not in k]
        special = [k['how'] for k in kills if k.get('maybe') or 'star' in k]
        parts = []
        if plain:
            parts.append('may be overwritten at %s, depending on a run-time `if`' % lines_of(plain))
        for h in dict.fromkeys(special):
            parts.append('may be re-set by %s' % h)
        return '; '.join(parts)

    for p in params:
        if not p['live']:
            p['effective'] = False
            p['why'] = 'never runs (%s)' % (p['branch'] or 'dead branch')
            continue
        st, o = status[id(p)]
        if st == 'dead':
            p['effective'] = False
            k = o.get('at')
            if o['kind'] == 'killed':
                p['why'] = 'overwritten at line %d%s before anything reads it' % (
                    k['line'], (' by %s' % k['how']) if k.get('how') else '')
            else:
                before = [e['line'] for e in S.events if e['k'] == 'read' and e['line'] < p['line']
                          and (e['names'] == ALL or p['name'] in e['names'])]
                p['why'] = 'nothing reads it after this line%s' % (
                    ' (it is read only before it, at L%d)' % before[-1] if before else '')
            continue
        if st == 'unsure':
            p['effective'] = None
            p['unsure'] = True
            if o.get('ureads'):
                u = o['ureads'][0]
                reason = '%s (it reads `%s`)' % (u['why'], p['name'])
            else:
                reason = o['uend'][0]
            p['why'] = ('the panel cannot tell whether or when this value is used: %s. Nothing '
                        'else reads it after this line' % reason)
            continue
        if st == 'sure' and o.get('implicit') and S.kind == 'main' and not dyn \
                and main_locals is not None and p['name'] in main_locals \
                and p['name'] not in _declared(tree):
            # a module name may be read from outside (an import); a local of main() cannot, so
            # one that nothing in main() reads is certainly not used: such a local was listed
            # as an override applied (round 4, #104)
            p['effective'] = False
            p['why'] = 'nothing in main() reads it'
            continue
        others = [q for q in groups[p['name']] if q is not p and q['live']
                  and status.get(id(q), ('dead',))[0] not in ('dead', 'unsure')]
        if p['conditional'] and (others or st != 'sure'):
            p['effective'] = None
            p['why'] = 'conditional: runs only in `%s`' % p['branch']
            if others:
                p['why'] += '; the script uses this line or %s' % ', '.join(
                    'L%d' % q['line'] for q in others)
            elif st == 'maybe':
                p['why'] += '; and it ' + maybe_text(o['maybe'])
            elif st == 'cond':
                p['why'] += '; and it is read only inside `%s` (L%d)' % (
                    o['creads'][0]['branch'] or 'a run-time block', o['creads'][0]['line'])
            continue
        if st == 'maybe':
            p['effective'] = None
            p['why'] = maybe_text(o['maybe'])
            continue
        if st == 'cond':
            p['effective'] = None
            c = o['creads'][0]
            p['why'] = 'read only inside `%s` (L%d)' % (c['branch'] or 'a run-time block', c['line'])
            p['why'] += ('; L%d sets it again before anything else reads it' % o['at']['line']
                         if o['kind'] == 'killed' else '; nothing else reads it')
            continue
        p['effective'] = True
        p['why'] = ''
        if o.get('implicit') and S.kind == 'module' and not dyn:
            # nothing in this script reads it: the two ways that is certainly not "the
            # value the script uses"
            if main_locals is not None and p['name'] in main_locals:
                p['effective'] = False
                p['why'] = ('main() has its own `%s`, and that is the one it uses; nothing '
                            'reads this line' % p['name'])
                continue
            theirs = _star_copy(tree, path, p['name'])
            if theirs:
                p['effective'] = None
                p['why'] = ('nothing in this script reads it, and %s has its own `%s`, which is '
                            'the one its code uses' % (theirs, p['name']))
                continue
        if o.get('at') is not None:
            occs = occ.get(id(p), [])
            second = (through([follow(i, p['name']) for i in occs], p['name'], ev[occs[0]]['path'])
                      if occs else None)
            if second:
                p['effective'] = None
                p['why'] = second
                continue
        if others and o.get('at') is not None:
            # two live lines for one name (a helper reused for two sweeps): say which one
            # feeds what, so the right one gets edited (blind review #112)
            p['why'] = 'read at L%d%s' % (o['at']['line'],
                                          (' (%s)' % o['at']['by']) if o['at'].get('by') else '')
        elif others and o['creads']:
            c = o['creads'][0]
            p['why'] = 'read only inside `%s` (L%d%s)' % (
                c['branch'] or 'a run-time block', c['line'], (', %s' % c['by']) if c.get('by') else '')


def _star_copy(tree, path, name):
    """'m.py (from m import * at L3)' when a module this script star-imports binds `name` at its
    own top level: its functions read THAT one, never the script's (blind review #113)."""
    for st in ast.walk(tree):
        if isinstance(st, ast.ImportFrom) and any(a.name == '*' for a in st.names):
            f = _find_module(st.module or '', st.level, path)
            got = _module_names(f, deep=False) if f else None
            if got and name in got['own']:
                return '%s (`from %s import *` at L%d)' % (os.path.basename(f), st.module or '.',
                                                          st.lineno)
    return None


def _declared(tree):
    """The names main() declares `global` / `nonlocal` somewhere in it: not its own."""
    main = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main'), None)
    return ({x for n in ast.walk(main) if isinstance(n, (ast.Global, ast.Nonlocal)) for x in n.names}
            if main is not None else set())


# ------------------------------------------------------------------ what the measurement class does with it

# The scan follows a value as far as the script hands it on - `settings = {'width': width,
# ...}` given to a measurement class - and there it stopped: a key the class reads only under
# a flag, or recomputes, was offered as applied as typed (blind review round 4, #014 #015 #103).
# What a class does can be written down here - module name -> {'class': name, 'rules': [...]}
# - and is used only while the class's source still has the lines it was read from (`sign`, a
# list of regular expressions, and `count`: [(regex, how often it occurs)]). A class changed on
# disk makes its notes go quiet, never wrong. A rule:
#   key    the settings key the script's value is handed in as; ('options', '*') is every key
#          of the dict handed in as options, the flag's own key apart
#   flag   the settings key the class decides on; `test` says when the note holds: 'set' - it
#          is not None, 'off' - it is false, 'not1' - it is not 1
#   kind   'ignored': the class does not use the value then; 'recomputed': it uses it, but not
#          as what it says
#   text   the note; {now} is replaced by the flag as it is (e.g. "use_fake = 0 (L24)")
# None ship with labMaster.
_CLASS_RULES = {}


def _class_source(path, cls):
    """The source of the lab module at `path`, if it still defines class `cls`; else None.
    Its stamp is kept with the scan (see _load): the notes follow the class on disk."""
    stamp = _stamp(path)
    if stamp is None:
        return None
    _DEPS.append((path, stamp))
    try:
        src = _read_source(path)[0]
    except (OSError, SyntaxError, UnicodeError):
        return None
    return src if re.search(r'^class %s\b' % re.escape(cls), src, re.M) else None


def _dict_of(node, nodes):
    """The dict literal `node` is, or the one it is bound to in `nodes` and never changed
    afterwards (`settings['width'] = 50`, `settings.update(...)` count as changes); else None."""
    if isinstance(node, ast.Dict):
        return node if all(k is not None for k in node.keys) else None     # no **spread
    if not isinstance(node, ast.Name):
        return None
    found, stores = [], 0
    for n in nodes:
        if isinstance(n, ast.Name) and n.id == node.id and not isinstance(n.ctx, ast.Load):
            stores += 1
        elif isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                and n.targets[0].id == node.id and isinstance(n.value, ast.Dict):
            found.append(n.value)
        elif isinstance(n, ast.Subscript) and not isinstance(n.ctx, ast.Load) \
                and isinstance(n.value, ast.Name) and n.value.id == node.id:
            return None
        elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == node.id \
                and n.attr in ('update', 'setdefault', 'pop', 'popitem', 'clear', '__setitem__'):
            return None
    if len(found) != 1 or stores != 1 or any(k is None for k in found[0].keys):
        return None
    return found[0]


def _settings_keys(arg, nodes):
    """({key path: value node}, [the dict nodes]) of what a class call is handed as settings,
    one nested dict deep (a dict of options handed in as one key); None when it cannot be read
    that way."""
    d = _dict_of(arg, nodes)
    if d is None:
        return None
    keys, dicts = {}, [d]
    for k, v in zip(d.keys, d.values):
        if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
            continue
        keys[(k.value,)] = v
        sub = _dict_of(v, nodes) if isinstance(v, ast.Name) else None
        if sub is not None:
            dicts.append(sub)
            for k2, v2 in zip(sub.keys, sub.values):
                if isinstance(k2, ast.Constant) and isinstance(k2.value, str):
                    keys[(k.value, k2.value)] = v2
    return keys, dicts


def _class_notes(tree, path, params, main, main_locals):
    """What the lab class a script hands its values to does with them (_CLASS_RULES): the
    rules that apply go into the param as 'class_rules', for cautions(), and the one that holds
    with the file's own values into `why`. A value the class then ignores is conditional (None),
    like a line inside a run-time `if` - the flag may be changed in the same block; the scan's
    own verdict is kept in 'src_effective' / 'src_why'."""
    imported = []
    for st in ast.walk(tree):
        if not (isinstance(st, ast.ImportFrom) and st.module):
            continue
        ent = _CLASS_RULES.get(st.module.rsplit('.', 1)[-1])
        if ent is None:
            continue
        local = {ent['class'] if a.name == '*' else (a.asname or a.name)
                 for a in st.names if a.name in ('*', ent['class'])}
        f = _find_module(st.module, st.level, path) if local else None
        src = _class_source(f, ent['class']) if f else None
        if src is not None:
            imported.append((local, ent, src))
    if not imported:
        return
    in_main = {id(n) for n in ast.walk(main)} if main is not None else set()
    in_fn = set()
    for d in ast.walk(tree):
        if d is not main and isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                                            ast.ClassDef)) and id(d) not in in_fn:
            in_fn |= {id(n) for n in ast.walk(d)}
    nodes = {'main': [n for n in ast.walk(main) if id(n) not in in_fn] if main is not None else [],
             'module': [n for n in ast.walk(tree) if id(n) not in in_main and id(n) not in in_fn]}
    loads = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            loads[n.id] = loads.get(n.id, 0) + 1
    found = []
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)) or id(call) in in_fn:
            continue
        hit = next((x for x in imported if call.func.id in x[0]), None)
        if hit is None:
            continue
        kind = 'main' if id(call) in in_main else 'module'
        arg = next((k.value for k in call.keywords if k.arg == 'settings'),
                   call.args[0] if call.args else None)
        got = _settings_keys(arg, nodes[kind])
        if got is None:
            continue
        keys, dicts = got
        inside = {}
        for d in dicts:
            for n in ast.walk(d):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    inside[n.id] = inside.get(n.id, 0) + 1

        def scope_of(name):
            return 'main' if kind == 'main' and name in main_locals else 'module'

        def flag_of(node):
            """{'name', 'keys', 'value', 'line', 'type'} of a flag's value in the file, or None
            when the file does not say it for certain"""
            if isinstance(node, ast.Constant):
                return {'name': None, 'keys': [], 'value': node.value, 'line': node.lineno,
                        'type': None}
            if not isinstance(node, ast.Name):
                return None
            sc = scope_of(node.id)
            rows = [p for p in params if p['name'] == node.id and p['scope'] == sc and p['live']
                    and p.get('effective') is not False]
            if rows:
                q = rows[0]
                if len(rows) != 1 or q.get('effective') is not True or q.get('unsure'):
                    return None
                return {'name': q['name'], 'keys': [q['key']], 'value': q['value'],
                        'line': q['line'], 'type': q['type']}
            # `ref_file = None`: never offered (None is not a value the dialog edits), but known
            ns = nodes[sc]
            stores = [n for n in ns if isinstance(n, ast.Name) and n.id == node.id
                      and not isinstance(n.ctx, ast.Load)]
            nones = [a for a in ns if isinstance(a, ast.Assign) and len(a.targets) == 1
                     and isinstance(a.targets[0], ast.Name) and a.targets[0].id == node.id
                     and isinstance(a.value, ast.Constant) and a.value.value is None]
            if stores and len(stores) == len(nones):
                return {'name': node.id, 'keys': [], 'value': None, 'line': nones[-1].lineno,
                        'type': None}
            return None

        _local, ent, src = hit
        for r in ent['rules']:
            if not all(re.search(s, src, re.M) for s in r['sign']) \
                    or any(len(re.findall(c, src, re.M)) != n for c, n in r.get('count', ())):
                continue                        # not the class these notes were read from
            flag = None
            if r.get('flag'):
                if r['flag'] not in keys:
                    continue
                flag = flag_of(keys[r['flag']])
                if flag is None:
                    continue
                flag['label'] = flag['name'] or r['flag'][-1]
            if r['key'][-1] == '*':
                paths = [k for k in keys if len(k) == len(r['key']) and k[:-1] == r['key'][:-1]
                         and k != r.get('flag')]
            else:
                paths = [r['key']] if r['key'] in keys else []
            for kp in paths:
                v = keys[kp]
                if not isinstance(v, ast.Name):
                    continue
                if loads.get(v.id, 0) != inside.get(v.id, 0):
                    continue                    # the script uses it elsewhere too
                for p in params:
                    if p['name'] == v.id and p['scope'] == scope_of(v.id) and p['live'] \
                            and p.get('effective') is not False and not p.get('unsure'):
                        found.append((p, {'kind': r['kind'], 'text': r['text'],
                                          'test': r.get('test'), 'flag': flag,
                                          'class': ent['class']}))
    for p, rule in found:
        if 'class_rules' not in p:
            p['class_rules'] = []
            p['src_effective'], p['src_why'] = p['effective'], p['why']
        if all(rule != q for q in p['class_rules']):
            p['class_rules'].append(rule)
    for p in params:
        if not p.get('class_rules'):
            continue
        note = _class_note(p)
        if note is None:
            continue
        kind, text = note
        if kind == 'ignored' and p['effective'] is True:
            p['effective'] = None
        # a live line whose value the class does not run as typed: worth a mark in the picker
        p['caution'] = kind == 'recomputed'
        p['why'] = ('%s; %s' % (p['why'], text)) if p['why'] else text


class _Unknown(Exception):
    pass


def _flag_now(flag, overrides, by_key):
    """(value, where) of a class flag: the block's override of it when there is one"""
    for k in flag['keys'] + ([flag['name']] if flag['name'] else []):
        new = (overrides or {}).get(k)
        if new is None:
            continue
        q = (by_key or {}).get(flag['keys'][0]) if flag['keys'] else None
        kind = q['type'] if q else flag.get('type')
        try:
            if kind == 'num':
                v = _finite(new, flag['name'])          # said as it is written: 0, not 0.0
                return (int(v) if v.is_integer() and type(flag['value']) is int else v,
                        'in this block')
            if kind == 'bool':
                b = _as_bool(new)
                if b is None:
                    raise _Unknown()
                return b, 'in this block'
            if kind == 'str':
                return str(new), 'in this block'
        except ValueError:
            pass
        raise _Unknown()                                 # check_values says what is wrong with it
    return flag['value'], 'L%d' % flag['line']


def _holds(test, value):
    if test == 'set':
        return value is not None
    if test == 'off':
        return not value
    if test == 'not1':
        return value != 1
    return False


def _now_text(flag, value, where, test):
    """'use_fake = 0 (L24)', 'ref_file is set (in this block)'"""
    if test == 'set' or value is None:
        return '%s is %s (%s)' % (flag['label'], 'None' if value is None else 'set', where)
    text = repr(value)
    return '%s = %s (%s)' % (flag['label'], text if len(text) <= 40 else text[:37] + '...', where)


_FILE = object()


def _class_note(p, new=_FILE, overrides=None, by_key=None):
    """(kind, text) of the first of p's class rules that holds - with the file's values, or with
    `new` for p itself and the block's `overrides` for the flags. None when no rule holds."""
    for r in p.get('class_rules') or []:
        try:
            value, where = _flag_now(r['flag'], overrides, by_key)
        except _Unknown:
            continue
        if _holds(r['test'], value):
            return r['kind'], r['text'].format(now=_now_text(r['flag'], value, where, r['test']))
    return None


_SCANS = {}                     # (path, sha1 of the bytes) -> (src, encoding, info, deps)
_LOCK = threading.RLock()
_DEPS = []                      # modules read while analysing: [(path, (mtime, size))]


def _stamp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _load(path):
    """-> (src, encoding, info), the analysis cached by the file's CONTENT: the start dialog
    scans each script several times, and an edited file is a different key, never stale. The
    star-imported modules it looked into are part of the key too."""
    with open(path, 'rb') as f:
        raw = f.read()
    key = (os.path.abspath(path), hashlib.sha1(raw).hexdigest())
    with _LOCK:                         # the HTTP threads and the worker scan at the same time
        hit = _SCANS.get(key)
        if hit is not None and all(_stamp(p) == s for p, s in hit[3]):
            return hit[:3]
        src, enc = _read_source(path)
        del _DEPS[:]
        info = _scan_source(src, path)
        deps = list(dict.fromkeys(_DEPS))
        del _DEPS[:]
        if len(_SCANS) > 64:
            _SCANS.clear()
        _SCANS[key] = (src, enc, info, deps)
        return src, enc, info


def scan(path):
    """-> {'params': [...], 'error': str|None}. Each param is addressed by 'name@line'."""
    try:
        _src, _enc, info = _load(path)
    except OSError as e:
        return {'error': str(e), 'params': []}
    except (SyntaxError, UnicodeError) as e:
        return {'error': 'cannot read %s: %s' % (os.path.basename(path), e), 'params': []}
    except RecursionError:
        # the last line of defence: the palette, takes_of() and the run call this, and none
        # of them expects a RecursionError (round 2, G07)
        return {'error': 'cannot read %s: it is nested too deeply for the panel to analyse'
                         % os.path.basename(path), 'params': []}
    return copy.deepcopy(info)                  # callers add keys to it


# ------------------------------------------------------------------ writing values back

def _index(params):
    by_key = {p['key']: p for p in params}
    by_name = {}
    for p in params:
        by_name.setdefault(p['name'], []).append(p)
    return by_key, by_name


def _norm(text):
    """the source text a value reads as after a round trip through the parser"""
    return ast.unparse(ast.parse(text, mode='eval').body)[:120]


def _patch(path, overrides):
    """-> (new_source, changes, encoding). Raises ValueError with a message for the operator."""
    fname = os.path.basename(path)
    try:
        src, enc, info = _load(path)
    except OSError as e:
        raise ValueError(str(e))
    except (SyntaxError, UnicodeError) as e:
        raise ValueError('cannot read %s: %s' % (fname, e))
    except RecursionError:
        raise ValueError('cannot read %s: it is nested too deeply for the panel to analyse'
                         % fname)
    if info['error']:
        raise ValueError(info['error'])
    by_key, by_name = _index(info['params'])

    chosen, edits, changes = {}, [], []
    for key, new in (overrides or {}).items():
        p = by_key.get(key)
        if p is None:
            group = by_name.get(key) or []
            if len(group) == 1:
                p = group[0]
            elif len(group) > 1:
                raise ValueError('%s appears %d times in %s (lines %s) - pick the line in the '
                                 'parameter dialog' % (key, len(group), fname,
                                                        ', '.join(str(q['line']) for q in group)))
            else:
                raise ValueError('%s is not a parameter of %s' % (key, fname))
        text = _as_source(p, new)
        if text is None:
            continue
        seen = chosen.get(p['key'])
        if seen is not None:
            # 'N' and 'N@12' are the same line. Two splices on one line used to leave the
            # tail of the first value behind: 7 became 700 (blind review #023)
            if seen[1] != text:
                raise ValueError('%s and %s both set %s on line %d (to %s and to %s) - keep one '
                                 'of them' % (seen[0], key, p['name'], p['line'], seen[1], text))
            continue
        chosen[p['key']] = (key, text)
        if text == p['source']:
            continue
        if p.get('effective', True) is False:
            raise ValueError('%s (line %d): %s - editing it would have no effect'
                             % (p['name'], p['line'], p['why']))
        if p.get('unsure'):
            # the value shown must be the value the script runs with; here nobody can say
            # (round 3, R12/R13)
            raise ValueError('%s (line %d): %s - the panel will not edit it; change the '
                             'script itself' % (p['name'], p['line'], p['why']))
        edits.append((p, text))
        changes.append('L%-4d %-24s %s  ->  %s' % (p['line'], p['name'], p['source'], text))

    lines = src.split('\n')                  # the AST's lines, not str.splitlines()' (#105)
    for p, text in sorted(edits, key=lambda e: (-e[0]['line'], -e[0]['col'])):   # right to left
        i = p['line'] - 1
        lines[i] = lines[i][:p['col']] + text + lines[i][p['end_col']:]
    new_src = '\n'.join(lines)
    _check_patch(fname, path, info, new_src, edits)
    return new_src, changes, enc


def _check_patch(fname, path, info, new_src, edits):
    """The copy must say exactly what was asked and nothing else: scanned again, the edited
    lines read as the new values and every other parameter as before. Any splice that went
    astray - a wrong column, a wrong line - is refused here instead of being run (#034, #105).

    A text the operator typed may be longer than MAX_STR, the limit for OFFERING a text as a
    parameter: the edited lines are read back without that limit, or such a value was
    refused as "did not come out as intended" when the block started (round 2, G06)."""
    after = _scan_source(new_src, path, analyse=False,
                         long_ok={p['key'] for p, _t in edits if p['type'] == 'str'})
    if after['error']:
        raise ValueError('the patched copy of %s does not parse (%s) - not running it'
                         % (fname, after['error']))
    want = {p['key']: p['source'] for p in info['params']}
    for p, text in edits:
        want[p['key']] = _norm(text)
    got = {p['key']: p['source'] for p in after['params']}
    if got != want:
        bad = sorted(set(got) ^ set(want)) or sorted(k for k in want if got.get(k) != want[k])
        k = bad[0]
        raise ValueError('the patched copy of %s did not come out as intended (%s: expected %s, '
                         'found %s) - not running it' % (fname, k, want.get(k, '(nothing)'),
                                                          got.get(k, '(nothing)')))


def apply_overrides(path, overrides):
    """-> (new_source, [human-readable change lines]). `overrides` maps 'name@line' or a plain
    unambiguous name to the new value. Raises ValueError when an override cannot be applied."""
    new_src, changes, _enc = _patch(path, overrides)
    return _view(new_src), changes


def _finite(v, name, what='a number'):
    """float(v), refusing what would not be a number in the copy: nan/inf are written as bare
    names that fail mid-run with NameError, a bool or None is not a number (blind review #042)"""
    if isinstance(v, bool) or v is None or isinstance(v, (list, tuple, dict)):
        raise ValueError('%s must be %s, got %r' % (name, what, v))
    try:
        val = float(v.strip() if isinstance(v, str) else v)
    except (TypeError, ValueError):
        raise ValueError('%s must be %s, got %r' % (name, what, v))
    if not math.isfinite(val):
        raise ValueError('%s must be a finite number, got %r' % (name, v))
    return val


def _as_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return {1: True, 0: False}.get(v) if v in (0, 1) else None
    if isinstance(v, str):
        return {'true': True, '1': True, 'yes': True,
                'false': False, '0': False, 'no': False}.get(v.strip().lower())
    return None


def _as_source(p, new):
    """The source text for a new value, or ValueError. An unchanged value gives back the
    file's own text, so it is never reported as a change."""
    kind, name = p['type'], p['name']
    if kind == 'num':
        val = _finite(new, name)
        if p.get('count') and not val.is_integer():
            # N_POINTS = 40.5 was written into the copy and np.linspace(..., N_POINTS) stopped
            # the run at its start with a TypeError (blind review round 4, #105)
            raise ValueError('%s is a count (%s), so it must be a whole number, got %r'
                             % (name, p['count'], new))
        if val.is_integer() and isinstance(p['value'], int):
            val = int(val)
        if val == p['value'] and type(val) is type(p['value']):
            return p['source']
        return '%s(%r)' % (p['wrap'], val) if p.get('wrap') else repr(val)
    if kind == 'bool':
        # anything but a clear yes/no used to become False: 'Ture' switched a flag OFF (#042)
        b = _as_bool(new)
        if b is None:
            raise ValueError('%s must be True or False, got %r' % (name, new))
        return 'True' if b else 'False'
    if kind == 'str':
        if new is None or isinstance(new, (list, tuple, dict)):
            raise ValueError('%s must be text, got %r' % (name, new))
        s = str(new)
        return p['source'] if s == p['value'] else repr(s)
    if kind in ('linspace', 'arange'):
        vals = new if isinstance(new, (list, tuple)) else str(new).replace(',', ' ').split()
        third = 'n' if kind == 'linspace' else 'step'
        if len(vals) != 3:
            raise ValueError('%s needs three numbers (start, stop, %s), got %r' % (name, third, new))
        what = 'three numbers (start, stop, %s)' % third
        nums = [_finite(v, name, what) for v in vals]
        orig = list(p['value'])
        # a whole number where the file has an int stays an int: np.arange(20, 500, 20) must
        # not turn into a float64 array because it went through the dialog (#042)
        out = [int(v) if v.is_integer() and isinstance(o, int) and not isinstance(o, bool) else v
               for v, o in zip(nums, orig)]
        if kind == 'linspace':
            if not nums[2].is_integer() or nums[2] < 1:
                raise ValueError('%s: the number of points must be a whole number of at least 1, '
                                 'got %r' % (name, vals[2]))
            out[2] = int(nums[2])
        elif out[2] == 0:
            raise ValueError('%s: the step of an arange cannot be 0' % name)
        if out == orig and all(type(a) is type(b) for a, b in zip(out, orig)):
            return p['source']
        # a sweep the panel could not even list: [0, 1, 1e300] passed every check, and the
        # preview then built a list of 1e300 floats and took the panel down (round 2, N49)
        n = grid_count(kind, *nums)
        if n > MAX_POINTS:
            raise ValueError('%s: %s would have %s points; a sweep may have at most %d%s'
                             % (name, _grid_text(kind, out), _count_text(n), MAX_POINTS,
                                ' - check the step' if kind == 'arange' else ''))
        if n < 1:
            # numpy gives an empty array: the script would sweep nothing (the linspace twin,
            # 0 points, is refused above)
            raise ValueError('%s: %s has no points - the step must go from start towards stop'
                             % (name, _grid_text(kind, out)))
        head = p['source'].split('(', 1)[0]
        return '%s(%s)' % (head, ', '.join(repr(v) for v in out))
    return None


MAX_POINTS = 100000             # a sweep's points; a typical one has a few thousand at most


def grid_count(kind, a, b, third):
    """How many points np.linspace(a, b, third) / np.arange(a, b, third) has, counted the way
    numpy counts, WITHOUT building them: float('inf') when the count does not fit a float.
    For the engine too, before it lists a grid (Engine._grid_of)."""
    a, b, third = float(a), float(b), float(third)
    if kind == 'linspace':
        return max(0.0, math.floor(third)) if math.isfinite(third) else float('inf')
    if third == 0 or (b - a) * third <= 0:
        return 0
    q = (b - a) / third
    if not math.isfinite(q):
        return float('inf')
    return max(0, int(math.ceil(q)))


def _grid_text(kind, vals):
    def num(v):
        t = repr(v)
        return t if len(t) <= 16 else '%.6g' % v
    return '%s(%s)' % (kind, ', '.join(num(v) for v in vals))


def _count_text(n):
    if n == float('inf'):
        return 'too many to count'
    return '%.3g' % n if n >= 1e6 else '%d' % n


def merge_overrides(path, current, patch):
    """`current` updated with `patch`, where a key of `patch` that addresses the same line as a
    key already there ('N' beside 'N@12') REPLACES that key instead of sitting next to it
    (blind review #023). A None value is kept - it means "back to the file's value" and the
    caller drops it. Keys that do not resolve are merged as they are; verify() reports them."""
    out = dict(current or {})
    info = scan(path) if path and os.path.isfile(path) else {'error': 'no script'}
    if info.get('error'):
        out.update(patch or {})
        return out
    by_key, by_name = _index(info['params'])

    def ident(k):
        p = by_key.get(k)
        if p is None:
            g = by_name.get(k) or []
            p = g[0] if len(g) == 1 else None
        return p['key'] if p else None

    for k, v in (patch or {}).items():
        me = ident(k)
        if me is not None:
            for old in [o for o in out if o != k and ident(o) == me]:
                out.pop(old)
        out[k] = v
    return out


def check_values(path, overrides):
    """A message for each value that cannot be written as it stands ('Ture' for a bool, nan, a
    linspace of 36.5 points), so a save can refuse it when it is typed instead of at the start
    of the run (blind review #042). Keys that do not resolve and None (= remove) are left
    alone: verify() and the run deal with those."""
    info = scan(path) if path and os.path.isfile(path) else None
    if not info or info.get('error'):
        return []
    by_key, by_name = _index(info['params'])
    out = []
    for key, new in (overrides or {}).items():
        if new is None:
            continue
        p = by_key.get(key)
        if p is None:
            group = by_name.get(key) or []
            p = group[0] if len(group) == 1 else None
        if p is None:
            continue
        try:
            _as_source(p, new)
        except ValueError as e:
            out.append(str(e))
    return out


def verify(path, base):
    """`base` maps key -> the source text the value had when it was chosen. Returns a list of
    problems, so an edited script can never be patched in the wrong place silently."""
    info = scan(path)
    if info['error']:
        return [info['error']]
    by_key, by_name = _index(info['params'])
    problems, seen = [], {}
    for key, was in (base or {}).items():
        p = by_key.get(key)
        group = []
        if p is None:
            group = by_name.get(key.split('@')[0]) or []
            p = group[0] if len(group) == 1 else None
        if p is None and len(group) > 1:
            # the common `if False:` alternatives: the bare name is ambiguous, not gone
            # (blind review #033)
            problems.append('%s appears %d times in the script (lines %s) - pick the line '
                            '(name@line) in the parameter dialog'
                            % (key, len(group), ', '.join(str(q['line']) for q in group)))
            continue
        if p is None:
            problems.append('%s: no longer in the script - re-pick the parameters' % key)
            continue
        if p['key'] in seen:
            problems.append('%s and %s both set %s on line %d - keep one of them'
                            % (seen[p['key']], key, p['name'], p['line']))
            continue
        seen[p['key']] = key
        if was is not None and p['source'] != was:
            problems.append('%s: the file now says `%s`, it was `%s` when you chose it - re-pick'
                            % (p['name'], p['source'], was))
        elif p.get('effective', True) is False:
            problems.append('%s: %s - editing it would have no effect' % (p['name'], p['why']))
        elif p.get('unsure'):
            problems.append('%s: %s - the panel will not edit it; change the script itself'
                            % (p['name'], p['why']))
    return problems


def cautions(path, overrides):
    """Edits that MAY have no effect: a line inside an `if` decided at run time takes
    effect only when that branch runs (F20). Not a reason to refuse - the person may know
    which branch the script takes - but a reason to say so, before the run and in its log.

    And what the measurement class does with an edited value (_CLASS_RULES), with the block's
    own overrides of the class's flags applied: a value it ignores or recomputes (round 4, #014
    #015 #103)."""
    info = scan(path)
    if info['error']:
        return []
    by_key, by_name = _index(info['params'])
    ov = overrides or {}
    out, seen = [], set()
    for key, new in ov.items():
        p = by_key.get(key)
        if p is None:
            group = by_name.get(key.split('@')[0]) or []
            p = group[0] if len(group) == 1 else None
        if p is None:
            continue
        seen.add(p['key'])
        if p.get('unsure'):
            # refused by _patch / verify; said here too, in the same words (round 3, R12)
            out.append('%s (line %d): %s. The panel will not edit this line.'
                       % (p['name'], p['line'], p['why']))
            continue
        if p.get('src_effective', p.get('effective')) is None:
            out.append('%s (line %d): %s. The edit takes effect only if that is the line the '
                       'script runs.' % (p['name'], p['line'], p.get('src_why', p['why'])))
        note = _class_note(p, p['value'] if new is None else new, ov, by_key)
        if note:
            out.append('%s (line %d): %s.' % (p['name'], p['line'], note[1]))
    return out


def preview(path, overrides):
    """The change list, without writing anything. Used by the confirm-before-start dialog."""
    try:
        _src, changes = apply_overrides(path, overrides)
        return changes, []
    except (ValueError, KeyError, SyntaxError, TypeError, OverflowError) as e:
        # a direct caller must not get a raw TypeError either (blind review #031)
        return [], [str(e)]


def write_patched_copy(path, overrides, out_dir):
    """Write the patched script into out_dir and return (new_path, changes). No overrides -> the
    original path is used unchanged, so a plain run is exactly what it always was."""
    if not overrides:
        return path, []
    new_src, changes, enc = _patch(path, overrides)
    if not changes:
        return path, []
    # the copy keeps the file's own encoding, so a PEP 263 coding line in it stays true; a
    # UTF-8 BOM is not carried over - Python reads plain UTF-8 the same (blind review #040)
    enc = 'utf-8' if enc.lower().replace('_', '-') in ('utf-8-sig', 'utf8-sig') else enc
    try:
        new_src.encode(enc, errors='surrogateescape')
    except UnicodeEncodeError as e:
        raise ValueError('%s is saved as %s, which cannot hold the new value (%s)'
                         % (os.path.basename(path), enc, e.object[e.start:e.end]))
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, os.path.basename(path))
    with open(out, 'w', encoding=enc, errors='surrogateescape') as f:
        f.write(new_src)
    return out, changes
