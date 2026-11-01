"""
panel_engine.py - the sequence engine behind the labMaster panel.

A *sequence* is a flat list of blocks that run top to bottom. A block is one of

    kind='script'     run a measurement script (under paths.script_search_dirs) in a
                      subprocess, once per rep, through runners/run_block.py
    kind='wait'       sleep for N minutes (default unit), with a live countdown
    kind='safe_stop'  run the configured safe-stop script (paths.safe_stop_script)

Design rules:
  * A measurement that is killed may leave its outputs as they were -> every kill is
    followed by the configured safe-stop script, always (an empty paths.safe_stop_script
    means there is none, and the step is skipped).
  * A long measurement is ~1 rep per tens of minutes, so 'pause' happens BETWEEN reps: the
    runner checks a sentinel file after each rep and exits cleanly, and the block keeps its
    remaining reps for the next start.
"""
import ast
import contextlib
import itertools
import copy
import json
import math
import os
import re
import sys
import signal
import subprocess
import threading
import time
from collections import deque

from . import paths as P
from . import scan as panel_scan

RUN_IDLE, RUN_RUNNING, RUN_PAUSED, RUN_ERROR, RUN_STOPPING = 'idle', 'running', 'paused', 'error', 'stopping'
_ids = itertools.count(1)


def _uid(prefix):
    return '%s_%d%03d' % (prefix, int(time.time()) % 100000, next(_ids) % 1000)


AUTO_NAME = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$')

# What this engine can do, in the snapshot: the page shows a control only when the engine lists
# it. 'open_folder': POST /api/open_folder, GET /api/client and the snapshot's 'paths';
# 'next_after_rep': POST /api/control next_rep, the snapshot's next_after_rep and an item's
# ended_early.
FEATURES = ('open_folder', 'next_after_rep')


def live_overrides(overrides):
    """The overrides that are really set (None is "not set")."""
    return {k: v for k, v in (overrides or {}).items() if v is not None}


def _no_script(target):
    """The one wording for a block whose script is not on disk."""
    return ('the script for this block is not there: %s. It has been renamed, moved, or not '
            'copied across - open the block with the gear and pick it again.'
            % (target or '(none set)'))


def _script_roots(cfg):
    """The folders a script block's script may come from: paths.script_search_dirs, resolved
    against the repo root - or the repo root itself when that list is empty or unreadable."""
    dirs = ((cfg.get('paths') or {}).get('script_search_dirs')
            if isinstance(cfg, dict) and isinstance(cfg.get('paths'), dict) else None)
    out = []
    for d in dirs if isinstance(dirs, (list, tuple)) else []:
        if isinstance(d, str) and d.strip():
            try:
                out.append(os.path.normcase(os.path.abspath(P.resolve(d.strip()))))
            except (TypeError, ValueError):
                continue
    return out or [os.path.normcase(os.path.abspath(P.ROOT))]


def _under(path, roots):
    for r in roots:
        try:
            if os.path.commonpath([path, r]) == r:
                return True
        except ValueError:                   # another drive
            continue
    return False


def _target_outside(cfg, target):
    """'' when a script block's target is a .py file inside one of this panel's script folders
    (_script_roots), else why it is refused. The page only ever offers those; a block posted
    over the network with any other path - another drive, a share - used to be run as it was,
    so a LAN client could run a program of its choosing on this PC (blind review #044, round 2
    I02 / N10). A share is refused without being touched. '' for no target at all: that is
    _no_script's to say."""
    t = str(target or '').strip() if not isinstance(target, (list, dict)) else '?'
    if not t:
        return ''
    roots = _script_roots(cfg)
    dirs = ((cfg.get('paths') or {}).get('script_search_dirs')
            if isinstance(cfg, dict) and isinstance(cfg.get('paths'), dict) else None)
    shown = ', '.join(d.strip() for d in dirs if isinstance(d, str) and d.strip()) \
        if isinstance(dirs, (list, tuple)) else ''
    shown = shown or 'the repo folder'
    why = ('the script "%s" is not one of this lab\'s: a block runs only a .py file inside the '
           'panel\'s script folders (%s - paths.script_search_dirs in config.json). A path '
           'anywhere else - another drive, a network share - is refused, so that a request '
           'over the network cannot run a program of its choosing on this PC. Pick the script '
           'from the function list.' % (t, shown))
    if t.startswith('\\\\') or t.startswith('//'):
        return why                           # UNC or a device path: never opened
    try:
        path = os.path.normcase(os.path.abspath(P.resolve(t)))
    except (TypeError, ValueError, OSError):
        return why
    if not path.endswith('.py') or not _under(path, roots):
        return why
    try:
        # inside by its name; and inside once links and junctions are followed too
        real = os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return why
    if not _under(real, [os.path.normcase(os.path.realpath(r)) for r in roots]):
        return why
    return ''


def _is_share(target):
    t = str(target or '').strip() if isinstance(target, str) else ''
    return t.startswith('\\\\') or t.startswith('//')


def _target_refusal(cfg, target):
    """_target_outside in real mode. In simulation the runner never loads the script, so only a
    network path is refused there (every look at it - the page's poll included - would reach
    out to that share); the test fixtures of a sandbox may live anywhere else."""
    if not isinstance(cfg, dict) or cfg.get('sim'):
        return _target_outside(cfg, target) if _is_share(target) else ''
    return _target_outside(cfg, target)


def default_seq_name():
    return time.strftime('%Y-%m-%d %H:%M')


# blocks.json has more than one writer - the page's whole-list save, and the engine itself (a Duplicate). One writer
# at a time, and the revision a page's save is checked against is read under the same lock (Engine.palette_rev /
# save_palette_from). Never held while the engine lock is taken or a line is logged.
_PALETTE_LOCK = threading.RLock()


def _finite(v):
    """The number, or None for anything that is not a finite number - a gate that compares
    against None or NaN would let it through."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _num(v, default=0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _whole_reps(v, name='reps'):
    """A rep count as the runner will run it: a whole number of 1 or more. 2.9 used to run 2
    reps and -3 one, while the block and History said 2.9 / -3 (blind review #009)."""
    f = _finite(v) if not isinstance(v, bool) else None
    if f is None or f != int(f) or f < 1:
        raise ValueError('%s must be a whole number of 1 or more (got %r)' % (name, v))
    return int(f)


def _clean_params(params, kind=None, target=None, cfg=None, name=None):
    """A params patch checked before anything is changed (contract C4). None stays None: it
    means "remove this key" (contract C3).

    Given the block's kind, the fields of the built-in kinds are checked too, and so is a
    main() argument whose default in the script is a number or a yes/no: '90 min', '1,5', a
    cleared box or 'false' used to be stored as typed and replaced by a default when the block
    ran - a 90-minute wait ran for one minute. The error names the block (`name`) and the
    field."""
    out = {}
    rules = _KIND_RULES.get(kind or '') or {}
    sig = None
    if kind == 'script' and target:
        sig = _main_sig(P.resolve(target))
    try:
        for k, v in (params or {}).items():
            if v is None:
                out[k] = None
            elif k in ('reps', 'numreps'):
                out[k] = _whole_reps(v, k)
            elif k in rules:
                out[k] = _rule_value(kind, k, v, cfg)
            elif sig and sig.get('has_main') and not sig.get('opaque') and k in sig['defaults']:
                out[k] = _main_arg_value(sig, k, v, target)
            else:
                out[k] = v
    except ValueError as e:
        if name:
            raise ValueError('"%s": %s' % (name, e))
        raise
    return out


# ------------------------------------------------------------------ what a block's values must be
# One table for the built-in kinds, used when a value is saved (add, update, commit), in the
# pre-flight, and again when the block starts: a value can reach the queue without being
# typed (a palette entry, a saved sequence, a queue from before these checks).
#   (check, lo, hi, what it has to be)
#   'num' any finite number   'range' lo..hi   'above' more than lo, at most hi
#   'whole' a whole number lo..hi   'bool' true/false   'choice' one of lo
_MAX_MINUTES = 1e6        # about two years; past this, now + minutes*60 stops being a date
_MINUTES = ('range', 0.0, _MAX_MINUTES, 'a number of minutes, 0 or more')
_KIND_RULES = {
    'wait': {'minutes': _MINUTES},
}
# the block kinds this engine runs
KINDS = ('script', 'wait', 'safe_stop')
# what the runner treats as the script's own rep count and sets to one per call
_REP_ARGS = ('numreps', 'reps', 'num_reps', 'nreps', 'n_reps')
# ...of which these two are the BLOCK's own rep count, taken out before main() is called
_BLOCK_REPS = ('reps', 'numreps')


def _shown(v):
    return repr(v) if not isinstance(v, str) else "'%s'" % v


def _as_number(v):
    """A finite float from a number or from text that is one; None for anything else - a
    bool, '', 'inf', 'nan', '90 min', '1,5' (a decimal comma is not guessed at)."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
    elif not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _as_bool(v):
    """True/False, or None for what is not clearly either: bool('false') is True."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        return {'true': True, 'yes': True, 'on': True, '1': True,
                'false': False, 'no': False, 'off': False, '0': False}.get(v.strip().lower())
    return None


def _rule_value(kind, key, v, cfg=None):
    """One field of a built-in kind, checked: the value to store, or ValueError naming it."""
    check, lo, hi, what = _KIND_RULES[kind][key]

    def bad(extra=''):
        return ValueError('%s must be %s%s (got %s)' % (key, what, extra, _shown(v)))

    if check == 'bool':
        b = _as_bool(v)
        if b is None:
            raise bad()
        return b
    if check == 'choice':
        s = v.strip().lower() if isinstance(v, str) else None
        if s not in lo:
            raise bad()
        return s
    f = _as_number(v)
    if f is None:
        raise bad()
    if check == 'whole':
        if f != int(f) or f < lo or (hi is not None and f > hi):
            raise bad(', %g to %g' % (lo, hi) if hi is not None else ', at least %g' % lo)
        return int(f)
    elif check == 'above':
        if not f > lo or (hi is not None and f > hi):
            raise bad('' if hi is None else ', at most %g' % hi)
    elif check == 'range':
        if lo is not None and f < lo:
            raise bad()
        if hi is not None and f > hi:
            raise bad('' if ('%g' % hi) in what else ', at most %g' % hi)
    # a number keeps its type; text that holds one is stored as the number
    if isinstance(v, (int, float)):
        return v
    return int(f) if f.is_integer() else f


def _param_problems(it, cfg=None, at_start=False):
    """What is wrong with a block's STORED params, as sentences, [] when nothing is - for the
    pre-flight and for the block's own start. Never raises.

    at_start: a stored rep count that is a number is run the way the runner runs it
    (max(1, int)); only one that is not a number at all stops the block there."""
    p = it.get('params') if isinstance(it, dict) else None
    if not isinstance(p, dict):
        return []
    kind = it.get('kind') or 'script'
    rules = _KIND_RULES.get(kind) or {}
    out = []
    for k, v in p.items():
        if v is None:
            continue
        try:
            if k in ('reps', 'numreps'):
                if not at_start:
                    _whole_reps(v, k)
                elif _as_number(v) is None:
                    raise ValueError('%s must be a whole number of 1 or more (got %s)'
                                     % (k, _shown(v)))
            elif k in rules:
                _rule_value(kind, k, v, cfg)
        except ValueError as e:
            out.append(str(e))
        except Exception as e:                                          # noqa: BLE001
            out.append('%s could not be checked (%s: %s)' % (k, type(e).__name__, e))
    return out


# The fields of a built-in kind that a null (C3: "remove this key") must not take away: the
# block would then run on a built-in default the page never showed - {'minutes': null} made a
# 90-minute wait one minute.
_KEEP_KEYS = {
    'wait': ('minutes',),
}


def _check_kind(block):
    """At entry: a block of a kind this engine does not run is refused (ValueError, 400)."""
    kind = (block.get('kind') if isinstance(block, dict) else None) or 'script'
    if kind not in KINDS:
        name = block.get('name') if isinstance(block, dict) else None
        raise ValueError('%sunknown block kind %r - a block is one of %s'
                         % ('"%s": ' % name if name else '', kind, ', '.join(KINDS)))


def _check_removals(kind, params, patch, name=None):
    """At entry (add, update, commit): a null for a field the block has and its kind needs is
    refused, naming it, before anything is changed (contract C4). A null for a field the block
    does not have changes nothing and is let through."""
    keep = _KEEP_KEYS.get(kind or '')
    if not keep or not isinstance(patch, dict):
        return
    have = params if isinstance(params, dict) else {}
    gone = [k for k in keep if k in patch and patch[k] is None and have.get(k) is not None]
    if gone:
        raise ValueError('%s%s cannot be removed (null) from a %s block: it would then run on a '
                         'built-in default instead of the %s it shows. Give %s a value instead.'
                         % ('"%s": ' % name if name else '', ', '.join(gone),
                            {'wait': 'Wait'}.get(kind, kind),
                            ', '.join('%s=%s' % (k, _shown(have[k])) for k in gone),
                            'it' if len(gone) == 1 else 'them'))


def _check_target(cfg, block):
    """At entry: a script block whose script is not inside the panel's script folders is
    refused in real mode - see _target_outside."""
    if not isinstance(block, dict) or (block.get('kind') or 'script') != 'script':
        return
    why = _target_refusal(cfg, block.get('target'))
    if why:
        raise ValueError('%s%s' % ('"%s": ' % block['name'] if block.get('name') else '', why))


# ------------------------------------------------------------------ what the script says, read from its source
_SRC_CACHE = {}                  # (what, abs path) -> ((mtime, size), value)


def _from_source(what, path, fn):
    """fn(the parsed file), cached until the file changes; None when it cannot be read. Parsed
    from the bytes as Python does, and never imported: a script with no main() RUNS when it is
    imported."""
    try:
        st = os.stat(path)
        stamp = (st.st_mtime, st.st_size)
    except (OSError, TypeError, ValueError):
        return None
    key = (what, path)
    hit = _SRC_CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        with open(path, 'rb') as f:
            tree = ast.parse(f.read(), path)
        val = fn(tree)
    except Exception:                    # noqa: BLE001 - unreadable is "cannot tell", never a crash
        val = None
    _SRC_CACHE[key] = (stamp, val)
    return val


_NOLIT = object()


def _literal_or_none(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return _NOLIT


def _sig_of(tree):
    fn, opaque = None, False
    for node in tree.body:               # what run_block.has_main() looks at, the last one wins
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'main':
            fn, opaque = node, False
        elif isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                (a.asname or a.name) == 'main' for a in node.names):
            fn, opaque = None, True
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'main' for t in node.targets):
            fn, opaque = None, True
    if opaque:
        return {'has_main': True, 'opaque': True}
    if fn is None:
        return {'has_main': False}
    a = fn.args
    pos = list(getattr(a, 'posonlyargs', []) or []) + list(a.args)
    defaults = {}
    for arg, d in zip(pos[len(pos) - len(a.defaults):], a.defaults):
        defaults[arg.arg] = _literal_or_none(d)
    for arg, d in zip(a.kwonlyargs, a.kw_defaults):
        if d is not None:
            defaults[arg.arg] = _literal_or_none(d)
    return {'has_main': True, 'opaque': False,
            'names': [x.arg for x in pos + list(a.kwonlyargs)],
            'varkw': a.kwarg is not None,
            'defaults': {k: v for k, v in defaults.items() if v is not _NOLIT}}


def _main_sig(path):
    """main()'s signature as the runner will call it: None when the file cannot be read,
    {'has_main': False} for a script-style file, {'opaque': True} when main comes from an
    import or an assignment (its arguments are not known here), else names / varkw /
    defaults (the literal ones)."""
    return _from_source('sig', path, _sig_of) if path else None


def _main_arg_value(sig, k, v, target=''):
    """A value for a main() argument whose default is a number or a yes/no, as that type.
    Text such as '4e5,' reached main() as a str, and 'false' was a true value (#030)."""
    d = sig['defaults'][k]
    base = os.path.basename(str(target or '')) or 'the script'
    if isinstance(d, bool):
        b = _as_bool(v)
        if b is None:
            raise ValueError('%s must be true or false - main() of %s has %s=%r (got %s)'
                             % (k, base, k, d, _shown(v)))
        return b
    if isinstance(d, (int, float)):
        f = _as_number(v)
        if f is None:
            raise ValueError('%s must be a number - main() of %s has %s=%r (got %s)'
                             % (k, base, k, d, _shown(v)))
        if isinstance(v, (int, float)):
            return int(v) if isinstance(d, int) and float(v).is_integer() else v
        return int(f) if isinstance(d, int) and f.is_integer() else f
    return v


def _main_arg_problems(target, params):
    """Block parameters the script cannot receive, or of the wrong type for it, as sentences
    (blind review #030). The runner drops a name main() does not take, and a script with no
    main() never sees any: the block ran on the script's own value while the page and
    History showed this one. Never raises."""
    try:
        p = params if isinstance(params, dict) else {}
        # reps / numreps are the BLOCK's rep count, which the engine takes out; the runner's
        # other rep names (nreps, num_reps, n_reps) reach main() like any parameter and used
        # to be skipped here too (round 2 N46)
        names = [k for k, v in p.items() if v is not None and k not in _BLOCK_REPS]
        if not names:
            return []
        path = P.resolve(target) if target else ''
        if not path or not os.path.isfile(path):
            return []
        sig = _main_sig(path)
        if not sig or sig.get('opaque'):
            return []
        base = os.path.basename(path)
        alias = [k for k in names if k in _REP_ARGS]
        if alias and sig.get('has_main'):
            taken = [k for k in alias if k in (sig.get('names') or [])]
            if taken:
                # the runner owns the rep loop: it sets whatever main() calls its own rep count
                # to 1 for each call, so the value typed here is never used
                names = [k for k in names if k not in taken]
                out_alias = ['%s is what main() of %s calls its own rep count: the runner sets it '
                             'to 1 for every rep, so %s here is not used. How many times the '
                             'block runs is its reps (%s now). Remove %s from the block, and set '
                             'reps instead.'
                             % (k, base, _shown(p[k]), _shown(p.get('reps', p.get('numreps', 1))),
                                k) for k in taken]
            else:
                out_alias = []
        else:
            out_alias = []
        if not names:
            return out_alias
        if not sig.get('has_main'):
            return ['%s has no main(), so the block parameter%s %s cannot reach it - the file '
                    'runs with the values written in it. Remove %s from the block and change '
                    'the value with "Script parameters..." instead.'
                    % (base, 's' if len(names) > 1 else '', ', '.join(names),
                       'them' if len(names) > 1 else 'it')]
        out = []
        if not sig.get('varkw'):
            lost = [k for k in names if k not in sig['names']]
            if lost:
                out.append('main() of %s does not take %s: the block would run on the script\'s '
                           'own value while the page and History show this one. Remove %s from '
                           'the block, or correct the name (main() takes: %s).'
                           % (base, ', '.join(lost), 'them' if len(lost) > 1 else 'it',
                              ', '.join(sig['names']) or 'no arguments'))
        for k in names:
            if k in sig['defaults']:
                try:
                    _main_arg_value(sig, k, p[k], path)
                except ValueError as e:
                    out.append(str(e))
        return out_alias + out
    except Exception:                                                   # noqa: BLE001
        return []


def _opaque_main_note(target, params):
    """A warning, or '': main() comes from an import or an assignment, so which block
    parameters it takes cannot be read before the run - one it does not take is dropped, and
    only the runner's note says so (round 2 N47). Never raises."""
    try:
        names = [k for k, v in (params or {}).items() if v is not None and k not in _BLOCK_REPS]
        path = P.resolve(target) if target else ''
        if not names or not path or not os.path.isfile(path):
            return ''
        sig = _main_sig(path)
        if not (sig and sig.get('opaque')):
            return ''
        return ('main() of %s is imported or assigned, not written in the file, so the panel '
                'cannot tell before the run whether it takes %s. One it does not take is dropped '
                'and the block runs on the script\'s own value - the runner then says "NOT used" '
                'on the card and in History.' % (os.path.basename(path), ', '.join(sorted(names))))
    except Exception:                                                   # noqa: BLE001
        return ''


_SYS_PATH_CALLS = ('insert', 'append', 'extend')


def _path_changes(path):
    """[line] of the places a script changes where Python looks for modules - sys.path.insert /
    append / extend, an assignment to sys.path, site.addsitedir: the pre-flight's import check
    looks modules up on the runner's own sys.path and cannot follow such a change (round 2
    G02). [] when there is none or the file cannot be read."""
    def is_sys_path(node):
        return (isinstance(node, ast.Attribute) and node.attr == 'path'
                and isinstance(node.value, ast.Name) and node.value.id == 'sys')

    def get(tree):
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Attribute) and f.attr in _SYS_PATH_CALLS \
                        and is_sys_path(f.value):
                    out.append(node.lineno)
                elif isinstance(f, ast.Attribute) and f.attr == 'addsitedir':
                    out.append(node.lineno)
            elif isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if is_sys_path(t) or (isinstance(t, ast.Subscript) and is_sys_path(t.value)):
                        out.append(node.lineno)
        return sorted(set(out))
    return _from_source('syspath', path, get) or []


def _top_imports(path):
    """The modules a script imports at its top level, dotted, in order. Imports inside a
    function, an `if` or a `try` are left out: those may be optional, or never run."""
    def get(tree):
        out = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                out.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module \
                    and node.module != '__future__':
                out.append(node.module)
        return list(dict.fromkeys(out))
    return _from_source('imports', path, get) or []


def _int_of(node, ints):
    if isinstance(node, ast.Constant) and isinstance(node.value, int) \
            and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        v = _int_of(node.operand, ints)
        return -v if v is not None else None
    if isinstance(node, ast.Name):
        return ints.get(node.id)
    return None


def _keyboard_reads(path):
    """[(line, what)] of the calls in a script that wait for the keyboard: input(), and
    msvcrt.getch()/getwch(). A panel block has no keyboard (blind review #110, #026)."""
    def get(tree):
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Name) and f.id in ('input', 'raw_input'):
                out.append((node.lineno, '%s()' % f.id))
            elif isinstance(f, ast.Attribute) and f.attr in ('getch', 'getwch', 'getche') \
                    and isinstance(f.value, ast.Name) and f.value.id == 'msvcrt':
                out.append((node.lineno, 'msvcrt.%s()' % f.attr))
        return sorted(out)
    return _from_source('keyboard', path, get) or []


def _top_loops(path):
    """The loops a script with no main() runs at its top level, outermost only: `for .. in
    range(N)` with N known from the source, and `while True`. [{'line', 'count', 'names',
    'text'}]; count None for `while True`."""
    def get(tree):
        ints, out = {}, []

        def walk(body):
            for st in body:
                if isinstance(st, ast.Assign) and len(st.targets) == 1 \
                        and isinstance(st.targets[0], ast.Name):
                    val = _literal_or_none(st.value)
                    if isinstance(val, int) and not isinstance(val, bool):
                        ints[st.targets[0].id] = val
                    else:
                        ints.pop(st.targets[0].id, None)
                elif isinstance(st, ast.For):
                    it = st.iter
                    if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) \
                            and it.func.id == 'range' and not it.keywords and 1 <= len(it.args) <= 3:
                        vals = [_int_of(a, ints) for a in it.args]
                        count = None
                        if all(v is not None for v in vals) and (len(vals) < 3 or vals[2]):
                            count = len(range(*vals))
                        if count is not None:
                            out.append({'line': st.lineno, 'count': count,
                                        'names': [a.id for a in it.args if isinstance(a, ast.Name)],
                                        'text': 'for ... in range(%s)' % ', '.join(
                                            ast.unparse(a) if hasattr(ast, 'unparse') else '?'
                                            for a in it.args)})
                elif isinstance(st, ast.While):
                    if isinstance(st.test, ast.Constant) and st.test.value in (True, 1):
                        out.append({'line': st.lineno, 'count': None, 'names': [],
                                    'text': 'while True'})
                elif isinstance(st, ast.If):
                    walk(st.body)
                    walk(st.orelse)
                elif isinstance(st, ast.With):
                    walk(st.body)
                elif isinstance(st, ast.Try):
                    walk(st.body)
                    walk(st.orelse)
                    walk(st.finalbody)

        walk(tree.body)
        return out
    return _from_source('loops', path, get) or []


# the pre-flight's import check runs this in a child started like the runner: the runner's
# python_exe, its working folder and its sys.path (run_block.setup_import_path). It only
# LOOKS modules up (importlib.util.find_spec for the top name, PathFinder for the rest inside
# the lab's own packages); nothing is imported and no script is run.
_IMPORT_CHECK = r'''
import importlib.machinery, importlib.util, json, os, sys
job = json.loads(sys.stdin.read())
def norm(p):
    return os.path.normcase(os.path.abspath(p))
root = job['root']
base = list(sys.path[1:])
home = [norm(h) for h in (os.path.dirname(os.path.abspath(sys.executable)), sys.prefix,
                          sys.exec_prefix) if h]
if not any(p and norm(p) == norm(root) for p in base):
    at = len(base)
    for i, p in enumerate(base):
        if p and (norm(p) in home or 'site-packages' in p.lower()):
            at = i
            break
    base.insert(at, root)
out = {}
for t in job['targets']:
    here = os.path.dirname(os.path.abspath(t['path']))
    sys.path[:] = [here, job['panel'], job['runners']] + base
    importlib.invalidate_caches()
    miss = []
    for name in t['mods']:
        parts = name.split('.')
        try:
            spec = importlib.util.find_spec(parts[0])
        except Exception:
            spec = None
        if spec is None:
            miss.append([name, parts[0], False])
            continue
        locs = list(spec.submodule_search_locations or [])
        mine = any(norm(os.path.dirname(l)) in (norm(root), norm(here)) for l in locs)
        for k in range(1, len(parts)):
            if not (locs and mine):
                break
            sub = '.'.join(parts[:k + 1])
            try:
                s2 = importlib.machinery.PathFinder.find_spec(sub, locs)
            except Exception:
                s2 = None
            if s2 is None:
                miss.append([name, sub, True])
                break
            locs = list(s2.submodule_search_locations or [])
    out[t['path']] = miss
sys.stdout.write('##IMPORTS ' + json.dumps(out) + '\n')
'''


def _missing_imports(cfg, targets):
    """({target: [[module, the part that is not found, is it the lab's own], ...]}, '') for
    the top-level imports of each script, or (None, why) when the check could not run.
    `targets` is [(abs path, [module, ...]), ...] (blind review #106)."""
    job = [{'path': t, 'mods': list(m)} for t, m in targets if m]
    if not job:
        return {}, ''
    payload = json.dumps({'root': P.ROOT, 'panel': P.PANEL_DIR, 'runners': P.RUNNERS_DIR,
                          'targets': job})
    try:
        r = subprocess.run([P.python_exe(cfg), '-u', '-c', _IMPORT_CHECK], input=payload,
                           capture_output=True, text=True, encoding='utf-8', errors='replace',
                           timeout=30, cwd=P.work_dir(cfg))
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return None, '%s: %s' % (type(e).__name__, e)
    for line in (r.stdout or '').splitlines():
        if line.startswith('##IMPORTS '):
            try:
                got = json.loads(line[10:])
            except ValueError:
                break
            return (got if isinstance(got, dict) else {}), ''
    return None, ((r.stderr or r.stdout or '').strip()[-300:] or 'exit code %s' % r.returncode)


# the pip package a module comes from, where the name differs or is worth saying (blind review #052)
_PACKAGE_OF = {'serial': 'pyserial', 'mpl_toolkits': 'matplotlib', 'matplotlib': 'matplotlib',
               'numpy': 'numpy', 'PyQt5': 'PyQt5', 'pyvisa': 'pyvisa'}


def _import_note(target, name, part, local, cfg, sim):
    pkg = _PACKAGE_OF.get(str(part).split('.')[0])
    what = ('%s.py is not there' % part.replace('.', '/') if local
            else 'no module %s%s can be found for %s'
            % (part, ' (package %s)' % pkg if pkg else '', P.python_exe(cfg).replace('\\', '/')))
    msg = ('%s imports %s, but %s: the block stops with "import failed" as soon as it starts. %s'
           % (os.path.basename(target), name, what,
              'Copy it across, or correct the import.' if local
              else 'Install it for that Python, or correct the import.'))
    if sim:
        msg += (' (A simulated run does not import the script, so it passes in simulation; in '
                'real mode this block would stop the run.)')
    return msg


def _data_root_note(cfg):
    """paths.data_root and where the lab scripts really save. qcodes writes relative to the
    process's working folder - the runner runs in paths.work_dir - so a data_root anywhere but
    <work_dir>/data is a folder the scripts never write to (blind review #107). None when
    they agree, in simulation (nothing saves there), or without a config."""
    if not isinstance(cfg, dict) or cfg.get('sim'):
        return None
    try:
        want = os.path.join(P.work_dir(cfg), 'data')
        have = P.data_root(cfg)
        if os.path.normcase(os.path.abspath(want)) == os.path.normcase(os.path.abspath(have)):
            return None
    except Exception:                                                   # noqa: BLE001
        return None
    return ('paths.data_root is %s, but a script that saves its datasets under the folder it '
            'runs in writes to %s (paths.work_dir). The panel looks for new datasets and serves '
            'the files under data_root, so it may find none of them (a script that uses '
            'labmaster_dataset follows LABMASTER_DATA_ROOT and is not affected). Set data_root '
            'to %s, or leave both empty.'
            % (have.replace('\\', '/'), want.replace('\\', '/'), want.replace('\\', '/')))


def _reps_hint(it):
    """The rep count as the runner will run it, for a message: 1 when it is not a number."""
    p = it.get('params') if isinstance(it.get('params'), dict) else {}
    f = _as_number(p.get('reps', p.get('numreps')))
    return max(1, int(f)) if f is not None else 1


def _check_overrides(target, overrides, name=None):
    """An override value the copy cannot be written with - 'Ture' for a flag, nan, a null in a
    linspace - refused when it is saved, not at the start of the run (contract C4; blind
    review #031). A script the scanner cannot read is left to the pre-flight and the run."""
    check = getattr(panel_scan, 'check_values', None)
    if check is None or not isinstance(overrides, dict) or not overrides:
        return
    path = P.resolve(target) if target else ''
    try:
        bad = check(path, {k: v for k, v in overrides.items() if v is not None})
    except Exception:                                                   # noqa: BLE001
        return
    if bad:
        raise ValueError('%s%s%s' % ('"%s": ' % name if name else '', bad[0],
                                     '' if len(bad) == 1 else ' (and %d more)' % (len(bad) - 1)))


def _merge_overrides(target, current, patch):
    """current + patch, where a key that names the same line as one already there REPLACES
    it: 'N' saved beside 'N@12' made two keys for one line, which the writer then refuses at
    the start of the run (blind review #023). None values are kept (the caller drops them).
    Falls back to a plain update when the scanner cannot read the script."""
    merge = getattr(panel_scan, 'merge_overrides', None)
    if merge is not None:
        try:
            return merge(P.resolve(target) if target else '', dict(current or {}), dict(patch))
        except Exception:                                               # noqa: BLE001
            pass
    out = dict(current or {})
    out.update(patch)
    return out


def _merge_base(before, after, base, patch, patch_base):
    """The override_base that goes with a merged (not replaced) override set (R05): the
    patch's source lines laid over the recorded ones, and the base of every key the merge took
    out dropped - one set to None, or replaced under its other spelling ('N' vs 'N@12').
    `before` / `after` are the override sets before the merge and after its Nones were dropped."""
    ob = dict(base or {}) if isinstance(base, dict) else {}
    pb = patch_base if isinstance(patch_base, dict) else {}
    for k in list(ob):
        if k in (before or {}) and k not in (after or {}):
            ob.pop(k)
    for k, v in (patch or {}).items():
        if v is None or k not in (after or {}):
            ob.pop(k, None)
        elif isinstance(pb.get(k), str):
            # source text only: a number here is not what the line said, and verify() would
            # refuse the run as "the file now says -6, it was -6" (the page sends the text)
            ob[k] = pb[k]
    return ob


# ------------------------------------------------------------------ pre-flight entries
_KIND_LABEL = {'wait': '(wait)', 'safe_stop': '(safe stop)'}


def _preview_entry(i, it, problems=None, warnings=None):
    """One row of the pre-start dialog, built from whatever the block holds; never raises."""
    it = it if isinstance(it, dict) else {}
    kind = it.get('kind') or 'script'
    params = it.get('params') if isinstance(it.get('params'), dict) else {}
    if kind == 'script':
        try:
            t = P.resolve(it.get('target')) if it.get('target') else ''
            script = P.rel_to_root(t) if t else '(no script set)'
        except Exception:                                               # noqa: BLE001
            script = str(it.get('target') or '(no script set)')
    else:
        script = _KIND_LABEL.get(kind, '(%s)' % kind)
    return {'n': i + 1, 'uid': it.get('uid'), 'name': it.get('name') or '(unnamed)',
            'script': script, 'reps': params.get('reps') if kind == 'script' else None,
            'changes': [], 'problems': list(problems or []), 'warnings': list(warnings or []),
            'by': it.get('by', 'you')}


def _unchecked(e):
    return ('the pre-start check could not read this block (%s: %s). It would most likely fail '
            'when it starts - open it and check its values.' % (type(e).__name__, e))


def _merge_order(items, by_uid, order, made):
    """The sequence a commit leaves: the draft's `order`, and every live block it did not list
    kept in its place - right before the listed block that followed it in the live sequence,
    or where it was at the end when no listed block follows it (a partial order, as the agent
    sends for an insert, lists only the blocks above). Such blocks all went to the end, so a
    wait the agent inserted at #1 while the page was saving silently ran after the
    measurements it was meant to precede (blind review #090, round 2 I07 / N44). New
    blocks the order does not list still go at the end.

    Returns (the new list, [(block kept before a listed one, that one's uid)])."""
    listed = [u for u in dict.fromkeys(order) if u in by_uid]
    is_listed = set(listed)
    live = [i for i in items if i['uid'] in by_uid]            # the deleted ones are gone
    before, waiting = {}, []
    for it in live:
        if it['uid'] in is_listed:
            for w in waiting:
                before[w['uid']] = it['uid']
            waiting = []
        else:
            waiting.append(it)
    ahead = {}
    for it in live:
        if it['uid'] in before:
            ahead.setdefault(before[it['uid']], []).append(it)
    out = []
    for u in listed:
        out.extend(ahead.get(u, []))
        out.append(by_uid[u])
    out.extend(waiting)                                          # no listed block after them
    out.extend(m for m in made if m['uid'] not in is_listed)
    kept = [(it, before[it['uid']]) for it in live if it['uid'] in before]
    return out, kept


def _apply_params(dst, patch):
    """Merge a cleaned params patch; None removes the key (contract C3)."""
    for k, v in patch.items():
        if v is None:
            dst.pop(k, None)
        else:
            dst[k] = copy.deepcopy(v)


def _run_params(item):
    """The params a non-script block RUNS with: the copy frozen in item['exec'] when it was
    marked running (#020), not the editable ones. An edit that landed between "running" and
    the block reading its values used to be run - a 0.03-minute wait ran for 240 minutes -
    while the log and History said it applied only to the next run (cross-cut pass)."""
    ex = item.get('exec') if isinstance(item.get('exec'), dict) else None
    if ex is not None and isinstance(ex.get('params'), dict):
        return ex['params']
    return item.get('params') if isinstance(item.get('params'), dict) else {}


def _params_of(it):
    """The block's params dict - made one if a stored queue holds none, or null."""
    if not isinstance(it.get('params'), dict):
        it['params'] = {}
    return it['params']


def _short(v, n=60):
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s if len(s) <= n else s[:n - 3] + '...'


def _running(eng, it):
    """Under the lock. Running, or still being wound down after a stop.
    Module level, like the three below: tests borrow update_item and
    preview_overrides onto a stand-in object that has no such methods."""
    return getattr(eng, 'current_uid', None) == it['uid'] or it.get('status') == 'running'


def _ee_complete(it, done, reps):
    """A block ended early by "Next block after this rep" whose record still fits what it holds - ended_early.of is
    the plan, 0 < reps_done <= done, and nothing changed since its last segment (_plan_changes). Start then completes
    it without measuring (_run_script's ee_done), so it counts like a block with every rep measured - not "N-k reps
    left"."""
    ee = it.get('ended_early') if isinstance(it, dict) else None
    if not isinstance(ee, dict) or not (0 < int(_num(ee.get('reps_done'), 0) or 0) <= done) \
            or int(_num(ee.get('of'), 0) or 0) != reps:
        return False
    return not _plan_changes(_last_exec(it), it)


def _edit_note(eng, it, before):
    """Under the lock, after a params/overrides edit: what it means for the run. '' if
    nothing worth saying. `before` is the plan before the edit."""
    if _running(eng, it):
        # the running copy keeps what it was started with (item['exec']); History is
        # written from that, not from these. The edit is the plan for the NEXT run (F19).
        ex = it.get('exec') or {}
        return ('"%s" is running with %s - the new values apply to the next time it runs, '
                'not to the run in progress'
                % (it['name'], json.dumps(ex.get('params') if 'params' in ex else before['params'],
                                          ensure_ascii=False)))
    done = int(_num((it.get('progress') or {}).get('done'), 0))
    if it.get('kind') == 'script' and it.get('status') == 'pending' and done > 0 \
            and _plan_of(it) != before and _plan_changes(_last_exec(it), it):
        # half run: its reps so far were measured with the old values (blind review #087)
        return ('"%s" has %d rep(s) measured with the values it had before this edit. They '
                'stay in History, but are not pooled with new ones: when it runs again it '
                'starts from rep 1' % (it['name'], done))
    return ''


_DIGESTS = {}                    # abs path -> ((mtime, size), sha1 hex)


def _script_digest(target):
    """sha1 of the script file's bytes, '' when it cannot be read. Cached until the file's
    stamp changes: it is asked for under the engine lock, on every edit of a script block."""
    if _is_share(target):
        return ''
    try:
        path = P.resolve(target) if target else ''
        st = os.stat(path)
    except (OSError, TypeError, ValueError):
        return ''
    stamp = (st.st_mtime, st.st_size)
    hit = _DIGESTS.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        import hashlib
        with open(path, 'rb') as f:
            val = hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return ''
    _DIGESTS[path] = (stamp, val)
    return val


def _plan_of(item):
    """What a block measures with, as planned now: every parameter but the rep count, the
    overrides as set, the script - and the script file's own
    contents (a constant edited on disk between two segments is another measurement too,
    round 2 N58). Two segments with different plans are two measurements, never one
    group (blind review #087)."""
    return {'script': (item.get('target') or '').replace('\\', '/'),
            'script_sha1': _script_digest(item.get('target')),
            'params': copy.deepcopy({k: v for k, v in (item.get('params') or {}).items()
                                     if k not in ('reps', 'numreps') and v is not None}),
            'overrides': copy.deepcopy(live_overrides(item.get('overrides')))}


def _last_exec(item):
    """What the last segment of a block ran with: its exec, or - when the panel went down
    between a segment's start and its runner's (round 2 N57) - the copy kept on disk then."""
    ex = item.get('exec') if isinstance(item, dict) else None
    if isinstance(ex, dict):
        return ex
    ex = item.get('exec_last') if isinstance(item, dict) else None
    return ex if isinstance(ex, dict) else None


def _plan_changes(prev, item, now=None):
    """What differs between the plan the last segment ran with (its exec) and the block
    now, as short 'name: old -> new' strings; [] when nothing does or it cannot be told.

    The rep count is never a difference: lowering or raising it changes how many reps are
    measured, not what one rep measures."""
    if not prev:
        return []
    now = now or _plan_of(item)
    old = prev.get('plan')
    skip = set()
    if old is None:
        # an exec recorded before plans were: compare what it has
        p = dict(prev.get('params') or {})
        for k in ('reps', 'numreps'):
            p.pop(k, None)
        old = {'script': now['script'], 'params': p, 'overrides': dict(prev.get('overrides') or {})}
    out = []
    if old.get('script') != now['script']:
        out.append('script: %s -> %s' % (os.path.basename(old.get('script') or '?'),
                                         os.path.basename(now['script'] or '?')))
    elif old.get('script_sha1') and now.get('script_sha1') \
            and old['script_sha1'] != now['script_sha1']:
        # the same file, edited on disk since the last segment: a constant that is not one of
        # the block's overrides may have changed (round 2 N58). A plan from before the digest
        # was recorded has none, and is not compared on it.
        out.append('%s was edited on disk since then' % os.path.basename(now['script'] or '?'))
    for part in ('params', 'overrides'):
        a, b = old.get(part) or {}, now[part]
        for k in sorted(set(a) | set(b)):
            if k in skip:
                continue
            if (k in a) != (k in b) or a.get(k) != b.get(k):
                out.append('%s: %s -> %s' % (k, _short(a[k]) if k in a else '(not set)',
                                             _short(b[k]) if k in b else '(not set)'))
    return out


# ------------------------------------------------------------------ Start after a failure (U03)
# A Start used to run the first PENDING block, so a block that had failed was passed over
# without a word; with only failed blocks left it said
# "sequence finished" and closed the batch. The operator chooses now: retry (measured again
# from rep 1, in the same batch) or skip (marked skipped by them, and History says so).

def _failed_choice(v):
    """None, 'retry' or 'skip' ('' is None, case does not matter). Anything else is a bad
    request (ValueError -> 400), a failed=true too - not an AttributeError (a 500)."""
    if v is None:
        return None
    s = v.strip().lower() if isinstance(v, str) else None
    if s == '':
        return None
    if s not in ('retry', 'skip'):
        raise ValueError('start: failed must be "retry" or "skip" (got %s)' % _shown(v))
    return s


def _failed_uid_list(v):
    """The failed blocks the operator's dialog showed (their uids), None when not given."""
    if v is None:
        return None
    if not isinstance(v, (list, tuple)) or not all(isinstance(u, str) for u in v):
        raise ValueError('start: failed_uids must be a list of block uids (got %s)' % _shown(v))
    return list(v)


def _failed_items(items):
    """[(n, block)] of the blocks that failed, n counted from 1."""
    return [(n, it) for n, it in enumerate(items or [], 1)
            if isinstance(it, dict) and it.get('status') == 'error']


def _failed_names(errs, limit=4):
    text = ', '.join('#%d "%s"' % (n, it.get('name')) for n, it in errs[:limit])
    return text + (' and %d more' % (len(errs) - limit) if len(errs) > limit else '')


def _failed_refusal(items):
    """Why a Start with no failed choice is refused, '' when no block has failed."""
    errs = _failed_items(items)
    if not errs:
        return ''
    one = len(errs) == 1
    if one:
        n, it = errs[0]
        what = 'block #%d "%s" failed (%s)' % (n, it.get('name'),
                                              _short(it.get('message') or 'no message', 160))
    else:
        what = 'blocks %s failed' % _failed_names(errs)
    are = 'it is' if one else 'they are'
    return ('not starting: %s, and Start would pass over %s. Choose: retry (failed="retry": %s '
            'set back to pending, measured again from rep 1, and the run goes on from there in '
            'the same batch) or skip (failed="skip": %s marked skipped by you, History says so, '
            'and %s not run again) - on the page, Retry or Skip on the run bar.'
            % (what, 'it' if one else 'them', are, are, are))


def _as_started(items, failed):
    """The blocks as a Start with this failed choice leaves them, for the pre-flight and the
    plan: a retried block pending from rep 1, a skipped one skipped. The failed blocks are
    copies; the live ones are not touched."""
    if failed is None:
        return items
    out = []
    for it in items:
        if isinstance(it, dict) and it.get('status') == 'error':
            if failed == 'retry':
                it = dict(it)
                Engine._reset_item(it)
            else:
                it = dict(it, status='skipped')
        out.append(it)
    return out


# ------------------------------------------------------------------ Load over progress (U05)

def _at_stake(items, run_state):
    """What a Load that REPLACES the sequence would discard: blocks with a record of a run, and
    a paused batch. Never raises."""
    out = {'done': 0, 'partial': 0, 'failed': 0, 'skipped': 0,
           'paused': run_state == RUN_PAUSED}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        st = it.get('status')
        pr = it.get('progress') if isinstance(it.get('progress'), dict) else {}
        if st == 'done':
            out['done'] += 1
        elif st == 'pending' and (_as_number(pr.get('done')) or 0) > 0:
            out['partial'] += 1
        elif st == 'error':
            out['failed'] += 1
        elif st == 'skipped':
            out['skipped'] += 1
    out['any'] = bool(out['paused'] or any(out[k] for k in ('done', 'partial', 'failed',
                                                            'skipped')))
    return out


def _stake_text(stake):
    s = stake or {}
    parts = ['%d %s' % (s[k], what) for k, what in (
        ('done', 'done'), ('partial', 'partly run'), ('failed', 'failed'), ('skipped', 'skipped'))
        if s.get(k)]
    if s.get('paused'):
        parts.append('a paused batch')
    return ', '.join(parts)


# ------------------------------------------------------------------ Save as over a name (U37)
# what a saved sequence keeps of a block (save_current writes the uid as well)
_SAVED_KEYS = ('name', 'kind', 'target', 'params', 'overrides', 'override_base', 'color',
               'block_id')


def _seq_sig(items):
    """A digest of what a saved sequence keeps of these blocks: the same after a Load and after
    a Save, another one once a block is added, removed, moved or edited. Statuses, progress
    and results never count."""
    import hashlib
    items = [it for it in (items or []) if isinstance(it, dict)]
    rows = [{k: it.get(k) for k in _SAVED_KEYS} for it in items]
    return hashlib.sha1(json.dumps(rows, sort_keys=True, default=str)
                        .encode('utf-8')).hexdigest()


# ------------------------------------------------------------------ the plan before Start (U30)

_PLAN_ROWS = 5                  # the newest History rows an estimate is made from


def _plan_rows(history, sim):
    """History rows an estimate may use, newest first: the same mode (a simulated rep says
    nothing about a real one), not a decision, a real duration, and a segment that ran."""
    out = []
    for r in reversed(history or []):
        if not isinstance(r, dict) or bool(r.get('sim')) != bool(sim) or r.get('decision'):
            continue
        s, f = _as_number(r.get('started')), _as_number(r.get('finished'))
        if s is None or f is None or not f > s > 0:
            continue
        if r.get('status') not in (('done', 'paused') if (r.get('kind') or 'script') == 'script'
                                   else ('done',)):
            continue
        out.append(r)
    return out


def _row_reps_run(r):
    """The reps a History row's segment measured itself, None when not one: reps_run where it
    was recorded (0 for a segment that only analysed), else reps_done from its start rep."""
    if 'reps_run' in r:
        n = _as_number(r.get('reps_run'))
    else:
        done, first = _as_number(r.get('reps_done')), _as_number(r.get('start_rep'))
        n = None if done is None else done - (first - 1 if first else 0)
    return int(n) if n is not None and n >= 1 else None


def _plan_values(params, overrides):
    """What 'the same values' compares, normalised the way _plan_of is: no rep count and
    nothing unset."""
    p = {k: v for k, v in (params or {}).items()
         if v is not None and k not in ('reps', 'numreps')}
    o = {k: v for k, v in (overrides or {}).items() if v is not None}
    return json.dumps([p, o], sort_keys=True, default=str)


def _median(xs):
    xs = sorted(xs)
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2.0


def _same_script(a, b):
    try:
        return bool(a) and bool(b) and _norm_path(P.resolve(a)) == _norm_path(P.resolve(b))
    except Exception:                                                   # noqa: BLE001
        return False


def _plan_block(n, it, rows, ss_timeout):
    """One entry of the plan: what the block will do and how long it took before."""
    kind = it.get('kind') or 'script'
    p = it.get('params') if isinstance(it.get('params'), dict) else {}
    e = {'n': n, 'uid': it.get('uid'), 'name': it.get('name') or '(unnamed)', 'kind': kind,
         'reps': None, 'reps_left': None, 'minutes': None, 'timeout_min': None,
         'est_s': None, 'max_s': None, 'basis': 'unknown', 'rows': 0}
    if kind == 'wait':
        # the params it will run with: the worker freezes them when it starts the block
        m = _as_number(p.get('minutes', 1))
        if m is not None:
            e.update(minutes=m, est_s=max(0.0, m) * 60.0, basis='exact')
        return e
    if kind == 'script':
        reps = _reps_hint(it)
        pr = it.get('progress') if isinstance(it.get('progress'), dict) else {}
        done = int(_as_number(pr.get('done')) or 0)
        changed = done > 0 and bool(_plan_changes(_last_exec(it), it))
        left = reps if (changed or done <= 0) else max(0, reps - done)
        if left and not changed and _ee_complete(it, done, reps):
            # ended early by "Next block after this rep" (a panel restart left it pending): Start
            # only completes it - no measuring time for the reps it was cut short of
            left = 0
        e.update(reps=reps, reps_left=left)
        if left == 0:
            e['basis'] = 'already measured'
            return e
        mine = [r for r in rows if (r.get('kind') or 'script') == 'script'
                and _row_reps_run(r) and _same_script(r.get('target'), it.get('target'))]
        ov = it.get('overrides') if isinstance(it.get('overrides'), dict) else {}
        want = _plan_values(p, live_overrides(ov))
        same = [r for r in mine if _plan_values(r.get('params'), r.get('overrides')) == want]
        for basis, use in (('same values', same), ('same script', mine)):
            use = use[:_PLAN_ROWS]
            if use:
                per = [(_as_number(r['finished']) - _as_number(r['started'])) / _row_reps_run(r)
                       for r in use]
                e.update(est_s=round(_median(per) * left, 1), basis=basis, rows=len(use))
                break
        return e
    # safe_stop: how long the same kind of block took
    if kind == 'safe_stop' and ss_timeout:
        e['max_s'] = float(ss_timeout)           # run.safe_stop_timeout_seconds
    use = [r for r in rows if r.get('kind') == kind][:_PLAN_ROWS]
    if use and kind == 'safe_stop':
        e.update(est_s=round(_median([_as_number(r['finished']) - _as_number(r['started'])
                                      for r in use]), 1), basis='same kind', rows=len(use))
    return e


def _plan_estimate(items, history, sim, now, failed=None, ss_timeout=None):
    """The plan the Start dialog shows: every block still to run, in the order it runs, with an
    estimate made only from what the panel has recorded - History durations of the same script
    (with the same values where there are some) per rep, times the reps left; wait minutes
    exactly; the same kind of block for the rest - and nothing invented: a block with nothing
    to go on is 'unknown', with its timeout as a bound where it has one. Pure."""
    rows = _plan_rows(history, sim)
    blocks = []
    for n, it in enumerate(_as_started(list(items or []), failed), 1):
        if not isinstance(it, dict) or it.get('status') != 'pending':
            continue
        try:
            blocks.append(_plan_block(n, it, rows, ss_timeout))
        except Exception:                                               # noqa: BLE001
            blocks.append({'n': n, 'uid': it.get('uid'), 'name': it.get('name') or '(unnamed)',
                           'kind': it.get('kind') or 'script', 'reps': None, 'reps_left': None,
                           'minutes': None, 'timeout_min': None, 'est_s': None, 'max_s': None,
                           'basis': 'unknown', 'rows': 0})
    known = sum(e['est_s'] for e in blocks if e['est_s'] is not None)
    unknown = [e for e in blocks if e['est_s'] is None]
    end_at = now + known if not unknown else None
    bounded = all(e['max_s'] is not None for e in unknown)
    return {'now': now, 'sim': bool(sim), 'blocks': blocks, 'known_s': known,
            'n_unknown': len(unknown), 'end_at': end_at, 'end_min': now + known,
            'end_max': end_at if not unknown else (
                now + known + sum(e['max_s'] for e in unknown) if bounded else None)}


# PowerShell writes the console's OEM code page (437 or GBK, by the PC's language) and Python
# used to decode it as the ANSI one (1252 here), strictly: one character outside the overlap in
# any python command line killed the decode, and every interlock that needs the list refused
# Start. PowerShell is told to write UTF-8 and it is decoded as UTF-8, replacing what it cannot
# (audit F4).
_PS_UTF8 = '[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); '


def _ps_python_rows(ps):
    """[(pid, command line)] of every python.exe / pythonw.exe, or None if the scan failed."""
    try:
        out = subprocess.run(['powershell', '-NoProfile', '-Command', _PS_UTF8 + ps],
                             capture_output=True, text=True, encoding='utf-8', errors='replace',
                             timeout=20, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    rows = []
    for line in (out or '').splitlines():
        pid, _, cmd = line.partition('\t')
        try:
            rows.append((int(pid.strip()), cmd.strip()))
        except ValueError:
            continue
    return rows


def _norm_path(s):
    """One spelling of a path inside a command line, for a substring test."""
    return str(s or '').replace('/', '\\').lower()


def _pid_alive(pid):
    """Is that process there? A syscall, no shell: cheap enough to ask under the lock.
    (os.kill(pid, 0) TERMINATES the process on Windows - never use it there.)

    A process this one may not open - an elevated runner watched by a panel that is not,
    another user's - IS there: ERROR_ACCESS_DENIED used to read as "it has ended", and every
    hold that follows a pid let go at once (#098 re-check)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True                                     # there, and not ours
        except OSError:
            return False
    try:
        import _winapi
        try:
            h = _winapi.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        except OSError as e:
            return getattr(e, 'winerror', None) == 5        # ERROR_ACCESS_DENIED: it is there
        try:
            return _winapi.WaitForSingleObject(h, 0) == 0x102   # WAIT_TIMEOUT: still running
        finally:
            _winapi.CloseHandle(h)
    except Exception:                                       # noqa: BLE001
        return False


_K32 = []


def _proc_created(pid):
    """When process `pid` was created (wall clock, s), or None when that cannot be asked. A pid
    named in a record, or seen in a process scan, is only taken as the same process while it
    was created before that: Windows gives the pid of a process that has ended to the next one,
    and a hold that followed it would wait on an unrelated program (#098 re-check)."""
    if os.name != 'nt':
        return None
    try:
        import ctypes
        from ctypes import wintypes
        if not _K32:
            k32 = ctypes.WinDLL('kernel32', use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            k32.GetProcessTimes.argtypes = ((wintypes.HANDLE,)
                                            + (ctypes.POINTER(wintypes.FILETIME),) * 4)
            k32.CloseHandle.argtypes = (wintypes.HANDLE,)
            _K32.append(k32)
        k32 = _K32[0]
        h = k32.OpenProcess(0x1000, False, int(pid))          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            ft = [wintypes.FILETIME() for _ in range(4)]
            if not k32.GetProcessTimes(h, *[ctypes.byref(f) for f in ft]):
                return None
            return ((ft[0].dwHighDateTime << 32) | ft[0].dwLowDateTime) / 1e7 - 11644473600.0
        finally:
            k32.CloseHandle(h)
    except Exception:                                       # noqa: BLE001
        return None


def _same_process(pid, existed_at):
    """`pid` is alive and still the process that existed at `existed_at` (wall clock): not one
    created since, which was given the pid of one that ended. A creation time that cannot be
    asked (another user's process) is taken as the same process."""
    if not _pid_alive(pid):
        return False
    born = _proc_created(pid) if existed_at is not None else None
    return born is None or born <= existed_at + 2.0


def _still_born(pid, born, seen):
    """`pid` is alive and still the process a scan listed: the same kernel creation time as
    when it was first seen (`born`) - never compared with a wall-clock time, which a step of
    the clock moves (#098 re-check, clock steps: a helper created just before a step back read
    as "created after it was seen" and was dropped at once). With no creation time known then,
    the wall-clock rule of _same_process, as before."""
    if not _pid_alive(pid):
        return False
    if born is None:
        return _same_process(pid, seen)
    now = _proc_created(pid)
    return now is None or abs(now - born) < 0.5


def _rec_age(rec, now=None):
    """How long ago a runner's record was last written, in s (never negative): from its wall-
    clock 'updated' (else 'noticed') - and, when it carries 'updated_mono' (run_block.py writes
    time.monotonic(), which on Windows counts from the boot for every process), the smaller of
    the two. A step of the wall clock since the write cannot age it then, and the smaller age
    only ever holds a safe stop longer (#098 re-check, clock steps)."""
    rec = rec if isinstance(rec, dict) else {}
    now = time.time() if now is None else now
    wall = now - (_finite(rec.get('updated')) or _finite(rec.get('noticed')) or 0)
    m = _finite(rec.get('updated_mono'))
    if m is not None and not isinstance(rec.get('updated_mono'), bool):
        mono = time.monotonic() - m
        if mono >= 0:
            wall = min(wall, mono)
    return max(0.0, wall)


# the last process scan of this thread (_leftover_runners): True / False, None when there was
# none - a SAFE STOP whose last look at the process list failed cannot show that it ran alone
_LEFT_SCAN = threading.local()

_SHORT = {}


def _ss_spellings(path):
    """The ways a run of the safe-stop script `path` can name it on its command line, in
    _norm_path's spelling: its full path, its name alone (run from its own folder:
    `python my_stop.py`) and the 8.3 short spelling of both (#098 re-check). {} for none."""
    p = str(path or '').strip()
    if not p:
        return set()
    out = {_norm_path(p), _norm_path(os.path.basename(p))}
    if os.name == 'nt':
        short = _SHORT.get(p)
        if short is None:
            short = ''
            try:
                import ctypes
                buf = ctypes.create_unicode_buffer(1024)
                n = ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, 1024)
                if 0 < n < 1024:
                    short = buf.value
            except Exception:                               # noqa: BLE001
                short = ''
            _SHORT[p] = short
        if short:
            out |= {_norm_path(short), _norm_path(os.path.basename(short))}
    out.discard('')
    return out


def _names_script(c, s):
    """Does command line `c` (in _norm_path's spelling) name the script spelled `s`? A path
    anywhere in it, as before; a name alone only as a whole word of it (after a quote, a space
    or a folder, before a quote or a space): 'slow_ss.py' is not 'my_slow_ss.py'."""
    if not s:
        return False
    if '\\' in s:
        return s in c
    i = c.find(s)
    while i >= 0:
        before = c[i - 1] if i > 0 else ' '
        j = i + len(s)
        after = c[j] if j < len(c) else ' '
        if before in ' "\'\\=' and after in ' "\'':
            return True
        i = c.find(s, i + 1)
    return False


def _panel_event(line):
    """(text before it, the JSON after it) for a line holding '##PANEL {' anywhere, else
    (line, None). Output written by C code (a DLL's printf) bypasses the runner's line
    tracking and can still glue an unfinished line to the next event (blind review #028)."""
    if line.startswith('##PANEL '):
        return '', line[8:]
    i = line.find('##PANEL {')
    if i < 0:
        return line, None
    return line[:i], line[i + 8:]


# the block kinds that drive an output: a panel restart in the middle of one of these may
# have left an output on, or a runner alive (blind review #003)
_HW_KINDS = ('script', 'safe_stop')
# the words a refusal of "Next block after this rep" names a block's kind with
_NEXT_KIND_WORDS = {'wait': 'wait', 'safe_stop': 'safe stop'}


def _push(eng, kind, item=None, name=None, reason=None):
    pass

    n = getattr(eng, 'notify', None)
    if n is None:
        return
    try:
        it = item if isinstance(item, dict) else {}
        nm = name if name is not None else (it.get('name') or it.get('kind') or '')

        n.event(kind, list_name=(eng.seq or {}).get('name') or '', name=nm,
                reason=reason if reason is not None else (it.get('message') or getattr(eng, 'fault', None) or ''),
                at=time.time(),
                key=('%s|%s|%s|%s|%s|%s' % (kind, eng.run_id, it.get('uid'), it.get('segment'), it.get('started'), nm))
                if item else None)
    except Exception:
        pass

def _push_between(eng):
    pass

    try:
        with eng.lock:
            only = (getattr(eng, '_trial', None) or {}).get('uid')
            nxt = next((i for i in ((eng.seq or {}).get('items') or []) if i.get('status') == 'pending'
                        and (only is None or i.get('uid') == only)), None)
            _push(eng, 'failed', None, name=(nxt or {}).get('name') or '(between blocks)',
                  reason=getattr(eng, 'fault', None) or getattr(eng, 'message', None) or '')
    except Exception:
        pass

class Engine(object):
    def __init__(self, config):
        P.ensure_dirs()
        self.cfg = config
        self.lock = threading.RLock()
        # the session log file: one writer at a time, rotated past _SESSION_LOG_MAX (#053)
        self._log_io = threading.Lock()
        self._log_writes = 0
        self._log_check_at = 0

        self.seq = {'name': default_seq_name(), 'items': []}
        self.cursor = 0
        self._trial = None        # {'uid','name','run_state','run_id','message'} while ONE block is tried on its own
        self.run_state = RUN_IDLE
        self.message = ''
        self.current_uid = None
        self.wait_end = None                 # epoch seconds while a wait block runs - for SHOWING
        # the deadline itself, on the monotonic clock: a step of the wall clock (a time sync,
        # a hand-set clock) used to end a 60-minute wait at once, or stretch it (#049)
        self._wait_deadline = None
        self._halt_why = None                # a block asked the queue to stop (#067)

        self._proc = None
        self._worker_thread = None
        self._pause_after_block = False
        self._pause_after_rep = False
        # "Next block after this rep", armed for ONE running script block - {uid, name, by, at,
        # rep, of} or None. It uses the pause file as "After rep" does; the block then ends
        # complete with the reps done, and the queue goes on without pausing (next_after_rep,
        # _end_early)
        self._next_after_rep = None
        self._abort = threading.Event()      # set by stop()/skip(): kill the child, leave the loop
        self._skip = threading.Event()
        self._end_wait = threading.Event()   # wakes a sleeping wait so it re-reads its flags
        self._wait_skip = threading.Event()  # "end this wait now", and ONLY that (F01)
        self._in_wait = None                 # uid of the wait block that is running, if any
        # stop() holds this from the kill to the end of the safe stop; start() will not run
        # while it is held, so two workers can never overlap (F05)
        self._stop_lock = threading.Lock()
        # two safe stops never run at once: a Stop pressed while a Skip's safe stop is still
        # running waits for it and then runs its own, instead of two processes driving the
        # outputs together
        self._safe_stop_lock = threading.Lock()
        # a safe stop that FAILED after a kill: the outputs may still be on. Nothing starts
        # until a safe stop has succeeded, and this says why (F12)
        self.fault = None
        # The ONE "safe stop in flight" state (blind review #000, #005, #054). A count, not a
        # flag: stop(), the header SAFE STOP and run_safe_stop itself each hold it while they
        # run, and they can overlap. While it is above zero nothing new may start.
        # snapshot() shows it (contract C1).
        self._ss_holds = 0
        self._ss_reason = ''
        self._stop_gen = 0            # bumped by every Stop / SAFE STOP: a Start prepared before it is void
        self._stopping_now = False    # stop() is between its first and its last bookkeeping (under self.lock)
        self._stop_reason = 'user'
        self._after_stop = None       # (run_state, message) for a trial that ended while stop() ran
        self._pb_busy = False         # the safe-stop script owns the outputs right now
        self._pb_owner = None         # ... and the thread running it
        self._fault_prev = None       # (run_state, message) from before the fault, for its clearing (N18)
        # the safe-stop child this session runs, and those a session before it left running
        # (state/safe_stop_child.json, N31) - the leftover scan knows them by their pid
        self._ss_child_pid = None
        self._ss_left_pids = set()
        self._error_goes_on = None    # a failed block that asked the queue to go on
        self._ps_cache = None         # (time, rows) of the last process scan
        # Look for what this panel may have left running (a runner, its own safe stop): on
        # after a restart that cut something off, and whenever a scan has found one, until a
        # scan finds none. Kept in queue.json with the pids it needs, so a second restart does
        # not lose it (round 3, R07-R09).
        self._restart_check = False
        self._restarted = False       # ... and it was a restart that turned it on (the wording)
        # Saving never stops a run (blind review #007): a save that fails is logged once, shown
        # on the page (snapshot 'storage_problems'), and tried again at every save point.
        self._q_dirty = False         # queue.json is behind memory (a save failed, or was deferred)
        self._q_saved_at = 0.0
        self._q_try_at = 0.0
        self._h_dirty = False         # the same for history.json
        self._h_try_at = 0.0
        self._save_fail = {}          # 'queue.json' / 'history.json' -> {'error', 'since', 'n'}
        self._drafts = []             # [(draft id, result)] of the last commits: a repeated Save (#022)
        # runners that ran their own safe stop after this panel died under them (#102)
        self._orphans = []            # [(record path, pid, until)]: still running theirs
        self._orphan_open = []        # records that wait for a SAFE STOP that succeeds
        self._orphan_moves = []       # [(record path, old pid, new pid)] for the log
        self._orphan_info = {}        # record path -> {'anchor', 'safe_stop', ...} (#098 re-check)
        self._orphan_seen = set()     # the record names this panel has read (listed again later)
        self._orphan_io = threading.Lock()   # one listing of the folder at a time
        self._orphan_scripts = set()  # the safe-stop script of every record read (#098 re-check)
        # runner pids a SAFE STOP of this panel ended and then ran alone and succeeded after: a
        # record such a runner wrote just before, read later, is answered (#098 re-check)
        self._ss_answered = set()
        # safe stops this panel did not start, as the last process scan found them: a runner's
        # helper and any run of the safe-stop script not its own - {pid: {'cmd', 'seen', ...}}
        self._ss_foreign = {}
        self._ss_foreign_ended = []   # [(pid, entry)] that have ended, for the log
        # python.exe rows of the last scan whose command line could not be read (a process of
        # another user, or one run as administrator while this panel is not), and those said
        self._ps_hidden = []
        self._ps_hidden_said = set()
        self._t_start = time.monotonic()
        self._relist_until = 0.0      # see _relist_due (set once the state has been read)

        self.log_seq = 0
        self.log_lines = deque(maxlen=4000)  # [{'n','ts','lvl','uid','text'}]
        # a damaged file is kept as <name>.corrupt-<time> and said so, not silently [] (#047)
        self.chat = P.read_json(P.CHAT_PATH, [], expect=list) or []
        self.history = P.read_json(P.HISTORY_PATH, [], expect=list) or []   # one row per finished block
        self.run_id = None
        self.saved = P.read_json(P.SAVED_PATH, [], expect=list) or []
        self._load_queue()
        self._read_orphan_records()
        self._read_left_markers()
        if self._restart_check:
            # a restart that cut something off: the orphan folder is listed again for as long
            # as a runner chain cut off by it may still write its record, whatever the leftover
            # scans find meanwhile (#098 re-check)
            self._relist_until = self._t_start + self._orphan_span_s('pending')
        self._report_unreadable()
        self._touch_pause_file(False)        # never inherit a pause from a killed session
        v = _finite(self._run_cfg().get('safe_stop_timeout_seconds', 120))
        if v and v > self._SS_TIMEOUT_MAX:
            self.log('config.json sets run.safe_stop_timeout_seconds = %g; the safe stop is given '
                     'at most %d s (a longer wait cannot be handed to the process, N21)'
                     % (v, self._SS_TIMEOUT_MAX), 'warn')
        # History's data folder: the index of the data folder, built in the background, and
        # the manual groups (panel_datafolder). No file I/O here - df_start() (panel_server.main)
        # loads the stores and starts the index, in-process test engines only when they call it.
        # One that cannot be loaded is said once and changes nothing else.
        self.df, self.df_error = None, ''
        try:
            from . import datafolder as panel_datafolder
            self.df = panel_datafolder.DataFolder(self)
        except Exception as e:                                  # noqa: BLE001
            self.df = None
            self.df_error = '%s: %s' % (type(e).__name__, e)
            self.log('History data folder is not available: %s' % self.df_error, 'error')

    # ------------------------------------------------------------------ logging

    # state/logs/session.log is rotated past this size; the old part is kept beside it as
    # session-<date>-<time>.log (every runner line lands here, blind review #053)
    _SESSION_LOG_MAX = 50 * 1024 * 1024
    _SESSION_LOG_EVERY = 200             # appends between two size checks

    _LOG_ROW_MAX = 20000                 # characters of one Log row (0.1.7: one print of a long list)

    def log(self, text, lvl='info', uid=None):
        text = str(text).rstrip()
        if len(text) > self._LOG_ROW_MAX:
            # one row is at most this long, whatever wrote it - a runner's or an analysis's single print of a long
            # trace was one multi-MB row in memory, in session.log and in a page's first /api/log
            text = text[:self._LOG_ROW_MAX] + ' ... (%d characters cut)' % (len(text) - self._LOG_ROW_MAX)
        with self.lock:
            self.log_seq += 1
            row = {'n': self.log_seq, 'ts': time.time(), 'lvl': lvl,
                   'uid': uid or self.current_uid, 'text': text}
            self.log_lines.append(row)
        rotated = None
        try:
            with self._log_io:
                self._log_writes += 1
                if self._log_writes >= self._log_check_at:
                    self._log_check_at = self._log_writes + self._SESSION_LOG_EVERY
                    rotated = self._rotate_session_log()
                with open(P.SESSION_LOG, 'a', encoding='utf-8') as f:
                    f.write('%s [%s] %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), lvl, row['text']))
        except OSError:
            pass
        if rotated:
            self.log('session.log passed %d MB: the earlier part is kept as %s (nothing is deleted)'
                     % (self._SESSION_LOG_MAX // (1024 * 1024), os.path.basename(rotated)), 'info')
        return row

    def _rotate_session_log(self):
        """Under _log_io. The new name when session.log was moved aside, else None. A file
        another program holds open (Windows) is simply tried again at the next check."""
        try:
            if os.path.getsize(P.SESSION_LOG) < self._SESSION_LOG_MAX:
                return None
            d = os.path.dirname(P.SESSION_LOG)
            stem = 'session-%s' % time.strftime('%Y%m%d-%H%M%S')
            new, k = os.path.join(d, stem + '.log'), 2
            while os.path.exists(new):
                new, k = os.path.join(d, '%s-%d.log' % (stem, k)), k + 1
            os.replace(P.SESSION_LOG, new)
            return new
        except OSError:
            return None

    def get_log(self, since=0, limit=400):
        """The log rows after `since`. since=0 (a page that has shown nothing yet) is the newest
        `limit`. A cursor above 0 gets the OLDEST `limit` after it, `more` true while rows are
        left, and `next` the cursor to ask with next time: the newest ones used to come back
        and the cursor jumped over the rest - a burst of more than 400 lines, or a pane that
        was hidden for a while, lost its middle without a mark (blind review #095). `dropped`:
        rows between the cursor and the oldest kept that the 4000-row buffer no longer holds
        (they are in state/logs/session.log). `seq` is the newest row's number, as before."""
        with self.lock:
            rows = [r for r in self.log_lines if r['n'] > since]
            seq = self.log_seq
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 400
        if since > 0:
            out, more = rows[:limit], len(rows) > limit
        else:
            out, more = rows[-limit:], False
        dropped = max(0, out[0]['n'] - since - 1) if (since > 0 and out) else 0
        return {'lines': out, 'seq': seq, 'next': out[-1]['n'] if out else since,
                'more': more, 'dropped': dropped}

    def block_log(self, uid, limit=200):
        """Round 4 (P6, U3 C4): the newest `limit` log rows of one block (by uid) that the in-memory buffer still
        holds - History's dialog for a failed block. `kept_from`: the oldest row the buffer holds (older lines are
        only in state/logs/session.log)."""
        uid = str(uid or '')
        try:
            limit = max(1, min(1000, int(limit)))
        except (TypeError, ValueError):
            limit = 200
        with self.lock:
            rows = [r for r in self.log_lines if uid and r.get('uid') == uid]
            first = self.log_lines[0]['ts'] if self.log_lines else None
        return {'uid': uid, 'lines': rows[-limit:], 'n': len(rows), 'kept_from': first}

    # ------------------------------------------------------------------ persistence

    def _write_state(self, name, path, obj):
        """P.write_json that never raises: True once it is on disk. A save that fails (the file
        held open by another process past write_json's own retries, a full disk) is logged
        once, kept in storage_problems for the page, and cleared by the next save that works.
        queue.json and history.json are checkpoints; they are never a reason to stop
        measuring, and a failed save used to end the night as 'engine error' (#007)."""
        try:
            P.write_json(path, obj)
        except (OSError, TypeError, ValueError) as e:
            with self.lock:
                f = self._save_fail.get(name)
                first = f is None
                if first:
                    f = self._save_fail[name] = {'since': time.time(), 'n': 0}
                f['n'] += 1
                f['error'] = '%s: %s' % (type(e).__name__, e)
            if first:
                self.log('%s could not be saved (%s). The run goes on; every save point tries '
                         'again, and the page says so until one works' % (name, f['error']), 'error')
            return False
        with self.lock:
            f = self._save_fail.pop(name, None)
        if f:
            self.log('%s saved again (it had failed %d time(s) since %s)'
                     % (name, f['n'], time.strftime('%H:%M:%S', time.localtime(f['since']))), 'ok')
        return True

    def _save_queue(self, soon=False):
        """Write queue.json. Never raises (#007). `soon`: a progress checkpoint during a rep
        (blind review #004) - skipped when the last save was a moment ago, and made up by the
        next save point or the next poll."""
        with self.lock:
            now = time.time()
            if soon and now - self._q_saved_at < 0.5:
                self._q_dirty = True
                return False
            self._q_try_at = now
            ok = self._write_state('queue.json', P.QUEUE_PATH, {
                'seq': self.seq, 'cursor': self.cursor,
                'run_state': self.run_state if self.run_state != RUN_RUNNING else RUN_PAUSED,
                'message': self.message,
                # a paused batch resumed after a restart is the SAME batch
                'run_id': self.run_id,
                'fault': self.fault,
                # what the leftover scan still looks for: a restart after the one that turned
                # it on used to forget it, and SAFE STOP then ran beside the leftover (R07)
                'leftover_check': self._leftover_state()})
            self._q_dirty = not ok
            if ok:
                self._q_saved_at = now
            return ok

    def _leftover_state(self):
        """Under the lock. The leftover scan's state as queue.json keeps it (R07), or None."""
        if not (self._restart_check or self._ss_left_pids):
            return None
        return {'on': bool(self._restart_check), 'ss_pids': sorted(self._ss_left_pids)}

    def _load_leftover_state(self, rec):
        """At start, from queue.json: what the last session's scan was still looking for."""
        if not isinstance(rec, dict):
            return
        ss = self._pids_in({'older': rec.get('ss_pids')})
        if rec.get('on') or ss:
            # found before and not yet seen gone: this session looks for it as well
            self._restart_check = True
            self._restarted = True
        self._ss_left_pids |= ss

    # history.json keeps the newest rows; the older ones are moved, not dropped, into
    # history_archive.jsonl beside it, one JSON row per line (blind review #090)
    _HISTORY_MAX = 3000

    def _history_add(self, row):
        """Under the lock. One row into History; what falls off the front waits in
        _h_archive_due until _write_history has appended it to the archive. It used to be cut
        off and gone: params, overrides and the dataset links of every block past the 3000th
        (#090). Bumps the version the page compares (#088)."""
        if isinstance(row, dict):
            # the panel version this row was measured / recorded under (the file VERSION when the panel started;
            # "unknown" without one). Every new row comes through here
            row.setdefault('panel_version', getattr(P, 'PANEL_VERSION', None) or 'unknown')
        self.history.append(row)
        over = len(self.history) - self._HISTORY_MAX
        if over > 0:
            self.__dict__.setdefault('_h_archive_due', []).extend(self.history[:over])
            self.history = self.history[over:]
        self._h_ver = self.__dict__.get('_h_ver', 0) + 1

    def _history_archive_path(self):
        return os.path.join(os.path.dirname(P.HISTORY_PATH), 'history_archive.jsonl')

    def _archive_history(self, rows):
        """Append rows to history_archive.jsonl, on disk before history.json lets go of them.
        True when they are there. After a crash between the two writes a row can be in the
        archive twice; its id tells the copies apart."""
        path = self._history_archive_path()
        try:
            with open(path, 'a', encoding='utf-8') as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False, default=str) + '\n')
                f.flush()
                os.fsync(f.fileno())
        except (OSError, TypeError, ValueError) as e:
            self.log('History: %d old row(s) could not be moved to %s (%s: %s) - history.json '
                     'keeps them until that works' % (len(rows), os.path.basename(path),
                                                      type(e).__name__, e), 'warn')
            return False
        if not self.__dict__.get('_h_archive_said'):
            self._h_archive_said = True
            self.log('History keeps its newest %d rows; older ones are moved to %s as they fall '
                     'off (nothing is deleted)' % (self._HISTORY_MAX, P.rel_to_root(path)), 'info')
        return True

    def _write_history(self):
        with self.lock:
            due = list(self.__dict__.get('_h_archive_due') or [])
            rows = list(self.history)
            self._h_try_at = time.time()
        # one writer archives at a time: two would append the same rows twice. The one that
        # does not get to (a poll retrying a failed save) keeps them in history.json meanwhile
        arch = self.__dict__.setdefault('_h_archive_lock', threading.Lock())
        if due and arch.acquire(False):
            try:
                if self._archive_history(due):
                    with self.lock:
                        left = self.__dict__.get('_h_archive_due') or []
                        self._h_archive_due = left[len(due):]
                        due = list(self._h_archive_due)
                        rows = list(self.history)
            finally:
                arch.release()
        # not archived yet (it failed, or another writer has them): they stay on disk here
        ok = self._write_state('history.json', P.HISTORY_PATH, due + rows)
        with self.lock:
            self._h_dirty = not ok
        return ok

    def _retry_saves(self):
        """Under the lock, from snapshot(): what a failed or deferred save left behind."""
        # while one is failing, not more often than every 15 s: write_json waits up to ~2 s
        # for a held file, under the lock every poll needs
        now = time.time()
        if self._q_dirty and now - self._q_try_at >= (15.0 if 'queue.json' in self._save_fail else 0.5):
            self._save_queue()
        if self._h_dirty and now - self._h_try_at >= 15.0:
            self._write_history()

    # what a state file that cannot be saved puts at risk, for the page (G03: every failing
    # file used to be called a risk to "an older History")
    _SAVE_STAKES = {
        'queue.json': 'a panel restart would come back to an older queue',
        'history.json': 'a panel restart would come back to an older History',
        'safe_stop_child.json': 'a panel restart would not know that a safe stop was running',
    }

    # the state files a damaged copy of which is worth a word on the page, and what the panel
    # did instead of reading them
    _STATE_FILES = (('QUEUE_PATH', 'the queue started empty'),
                    ('HISTORY_PATH', 'History started empty; the earlier rows are in the copy'),
                    ('SAVED_PATH', 'the saved sequences list started empty'),
                    ('CHAT_PATH', 'the chat started empty'),
                    ('BLOCKS_PATH', 'the function list is empty until it is repaired'))

    def _unreadable_notes(self):
        out = []
        for attr, then in self._STATE_FILES:
            pr = P.read_problem(getattr(P, attr))
            if pr:
                out.append('%s %s - %s%s' % (
                    os.path.basename(pr['path']), pr['problem'], then,
                    '; the damaged file was kept as %s' % os.path.basename(pr['copy'])
                    if pr.get('copy') else ''))
        return out

    def _report_unreadable(self):
        for note in self._unreadable_notes():
            self.log('state file not usable: ' + note, 'error')

    def _storage_problems(self):
        """Under the lock. Sentences for the page: saves that are failing, files that were
        damaged. Empty when all is well."""
        out = []
        for name, f in sorted(self._save_fail.items()):
            out.append('%s could not be saved since %s (%d attempt(s): %s). The run goes on and '
                       'every save point tries again; until one works, %s.'
                       % (name, time.strftime('%H:%M:%S', time.localtime(f['since'])), f['n'],
                          f.get('error') or '?',
                          self._SAVE_STAKES.get(name, 'a panel restart would come back to an '
                                                      'older copy of it')))
        return out + self._unreadable_notes()

    def _load_queue(self):
        st = P.read_json(P.QUEUE_PATH, None, expect=dict)
        broken = P.read_problem(P.QUEUE_PATH)
        if broken:
            # Whether a block was running when it was written cannot be known: treated like a
            # restart in the middle of one (#003) - SAFE STOP before Start (blind review #047)
            self._restart_check = True
            self._restarted = True
            self._set_fault('queue.json could not be read at start (%s)%s, so whether a block '
                            'was running when the panel went down is unknown'
                            % (broken['problem'], '; it was kept as %s' % os.path.basename(
                                broken['copy']) if broken.get('copy') else ''))
            self._save_queue()
            return
        if not st:
            return
        self.seq = st.get('seq') or self.seq
        self.cursor = int(st.get('cursor') or 0)
        self.message = st.get('message') or ''
        self.run_id = st.get('run_id') or None
        self.fault = st.get('fault') or None
        self._load_leftover_state(st.get('leftover_check'))
        if self.fault:
            # A fault from before this restart: whatever it was about may still be running,
            # and the pids a first restart had found are only here now (R07, R09)
            self._restart_check = True
            self._restarted = True
        try:
            died = os.path.getmtime(P.QUEUE_PATH)        # its last save: the last sign of life
        except OSError:
            died = None
        # a block left 'running' by a server restart never finished
        interrupted, done_note = [], {}
        for it in self.seq.get('items', []):
            aside = it.get('trial_saved')
            if it.get('status') == 'running':
                pr = it.get('progress') or {}
                if isinstance(pr, dict):
                    pr.pop('running', None)               # that rep did not finish
                it['status'] = 'pending'
                note = (' (%s of %s rep(s) done)' % (pr.get('done') or 0, pr.get('total'))
                        if (it.get('kind') or 'script') == 'script' and pr.get('total') else '')
                done_note[it.get('uid')] = note
                it['message'] = 'interrupted by a panel restart' + note
                if note and int(_num(pr.get('done'), 0)) >= int(_num(pr.get('total'), 0)) > 0:
                    it['message'] += ' - every rep was measured; Start completes it without measuring again'
                elif note and isinstance(it.get('ended_early'), dict):
                    # ended early by "Next block after this rep" before the restart
                    it['message'] += (' - it was ended early after rep %s ("Next block after this rep"); Start '
                                      'completes it without measuring again' % it['ended_early'].get('reps_done'))
                # The segment the restart cut off gets its History row - its reps and the
                # datasets it had recorded (saved after every rep now). It used to get none,
                # and the next row read 'segment 2' with no segment 1 (blind review #004).
                it['finished'] = died
                self._record_history(it, self.seq['items'].index(it) + 1,
                                     ds_from=len(it.get('datasets') or []), outcome='interrupted',
                                     trial=aside is not None)
                interrupted.append(it)
            if aside is not None:
                # it died during a trial: the block goes back to what the night had (#011)
                was = it.get('message') or ''
                kept = self._trial_restore(it)
                self.run_id = aside.get('_run_id')
                if was.startswith('interrupted'):
                    it['message'] = 'its trial run was %s - see History%s' % (
                        was, '; the %d rep(s) measured before the trial are kept' % kept if kept else '')
        items = self.seq.get('items', [])
        started = any(i.get('status') in ('done', 'skipped', 'error') for i in items)
        pending = any(i.get('status') == 'pending' for i in items)
        failed = [i for i in items if i.get('status') == 'error']
        self.run_state = RUN_PAUSED if (started and pending) else RUN_IDLE
        if not pending and not failed:
            # nothing left to run: that batch is over, and blocks added now start a new one
            # instead of joining it in History (blind review #117). A failed block can still
            # be retried in it (U03), so its batch stays open.
            self.run_id = None
        # A block that drives an output, or a stop, cut off by the restart: nobody ran the
        # safe stop after it, and its runner may still be alive and shutting down on its own
        # (it runs its own safe stop, at a moment this panel cannot know). Start is refused
        # until a SAFE STOP has succeeded, and until no such runner is left (blind review #003).
        hw = [it for it in interrupted if (it.get('kind') or 'script') in _HW_KINDS]
        if hw or st.get('run_state') == RUN_STOPPING:
            self._restart_check = True
            self._restarted = True
            what = ('block "%s" was running%s' % (hw[0].get('name'), done_note.get(hw[0].get('uid'), ''))
                    if hw else 'a stop was in progress')
            try:
                no_script = not self._safe_stop_path()
            except Exception:                                           # noqa: BLE001
                no_script = False                # a value that cannot be used: the fault says it
            if not self.fault and no_script:
                # nothing a safe stop could run: no fault - the leftover check still refuses
                # Start while a runner from before is alive
                self.log('the panel was restarted while %s. No stop script is configured '
                         '(paths.safe_stop_script is empty), so there is no safe stop to run - '
                         'check the instruments\' outputs yourself' % what, 'warn')
            elif not self.fault:
                # what to look at, not only that something happened: the page shows this
                # text, and the usual causes leave the outputs as they were (round 2, N09)
                self._set_fault('the panel was restarted while %s, and no safe stop has run '
                                'since; a runner left from before may still be shutting down. '
                                'Likely causes: the panel window was closed, Windows logged off '
                                'or restarted (Windows Update too), or the panel crashed. Check '
                                'the instruments\' outputs, then press SAFE STOP before Start' % what)
        if self.fault:
            # the outputs may still be on from before the restart; say so, refuse to start
            self.run_state = RUN_ERROR
            self.message = self.fault
        elif not pending and failed:
            # finished with failures: still what the queue says after a restart (U03)
            self.run_state = RUN_ERROR
            self.message = 'sequence finished: %d failed / %d skipped' % (
                len(failed), sum(1 for i in items if i.get('status') == 'skipped'))
        if interrupted or self.fault:
            # written now: another restart before the next save must not record the same cut-off
            # segment a second time, nor lose the fault
            self._save_queue()

    def _save_chat(self):
        P.write_json(P.CHAT_PATH, self.chat[-500:])

    def _save_saved(self):
        P.write_json(P.SAVED_PATH, self.saved)

    # ---- what a runner left behind after this panel died under it (blind review #102) ----
    # Two channels, so that no single one failing lets a safe stop of ours run beside a runner's
    # own (#098 re-check): the record the runner writes (read at start, and again while the
    # restart check is on), and the process list (_leftover_runners), which knows a runner's
    # helper and any run of the safe-stop script whatever the record says or fails to say.

    def _orphan_dir(self):
        return os.path.join(P.STATE_DIR, 'orphan_safe_stop')

    # a record older than this is not taken as a safe stop still running, whatever pid it names -
    # and none is held for longer than this
    _ORPHAN_LIVE_S = 15 * 60

    def _orphan_span_s(self, status):
        """How long a runner's chain may still run after it wrote `status`, by the runner's own
        limits (runners/run_block.py): its grace once the panel has gone (CLEANUP_GRACE_S at
        most), its own safe stop, the helper's wait for it to exit (RETRY_WAIT_S) and the
        helper's safe stop - and a minute more. A record is held that long from its last write:
        a fixed 210 s gave up on a chain still inside its limits (#098 re-check)."""
        try:
            grace = float(os.environ.get('LABMASTER_RUNNER_CLEANUP_GRACE_S') or 180.0)
        except ValueError:
            grace = 180.0
        ss = max(120.0, self._ss_timeout())
        helper = 60.0 + ss
        return {'pending': grace + ss + helper, 'running': ss + helper}.get(status, helper) + 60.0

    def _orphan_read(self, path):
        """(record, '') - or (None, why) when it cannot be read now, 'gone' when it is not there.
        One a SAFE STOP has put away is read where it was put (.seen.json). Read once, nothing
        set aside and no retry sleeps (it is read under the lock): one held for a moment by a
        scanner or a backup is read again later, it is not damaged (#098 re-check)."""
        for p in (path, path[:-len('.json')] + '.seen.json'):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    got = json.load(f)
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as e:
                return None, 'cannot be read now (%s)' % e
            return (got, '') if isinstance(got, dict) else (None, 'holds no record')
        return None, 'gone'

    @staticmethod
    def _orphan_anchor(path, rec=None):
        """When the record was last written (its file's time, or its 'updated'): every pid in it
        belongs to a process that existed then, so one created later is another program that
        was given the pid (#098 re-check)."""
        t = []
        for p in (path, path[:-len('.json')] + '.seen.json'):
            try:
                t.append(os.path.getmtime(p))
                break
            except OSError:
                continue
        u = _finite((rec or {}).get('updated'))
        if u is not None:
            t.append(u)
        return max(t) if t else None

    def _read_orphan_records(self):
        """At start. A runner whose panel died ran the safe stop itself and wrote down how it
        went; only it knows whether the outputs were switched off, and nobody read
        it. A good one is said in the log and put away. Anything else is a FAULT until a SAFE
        STOP succeeds, and one whose safe stop (or its retry) is still running holds Start off
        as a safe stop in flight (contract C1)."""
        self._orphan_relist()

    def _orphan_relist(self):
        """Outside the lock: the records this panel has not seen yet - at start, and again while
        the restart check is on. A panel restarted in the first seconds of a runner's grace
        listed the folder before the runner had written anything, and never looked again: the
        helper that ran the safe stop was invisible to it (#098 re-check)."""
        try:
            names = sorted(n for n in os.listdir(self._orphan_dir())
                           if n.startswith('runner_') and n.endswith('.json')
                           and not n.endswith('.seen.json'))
        except OSError:
            return
        with self._orphan_io:
            with self.lock:
                new = [n for n in names if n not in self._orphan_seen]
                self._orphan_seen.update(new)
            if new:
                self._orphan_take(new)

    def _orphan_take(self, names):
        """Records seen for the first time: said, and held or answered (see _read_orphan_records)."""
        bad = []
        for n in names:
            path = os.path.join(self._orphan_dir(), n)
            rec = P.read_json(path, None, expect=dict)
            target = os.path.basename(str((rec or {}).get('target') or '?'))
            if not isinstance(rec, dict):
                # held by a scanner, or damaged: read again, and never taken as nothing running -
                # it may name a runner or a helper still running its safe stop (#098 re-check).
                # The pid in its name is the runner's that wrote it.
                m = re.match(r'runner_(\d+)_', n)
                pid = int(m.group(1)) if m else 0
                anchor = self._orphan_anchor(path)
                age = time.time() - anchor if anchor is not None else 0.0
                with self.lock:
                    self._orphans.append((path, pid, time.monotonic() + max(0.0, min(
                        self._ORPHAN_LIVE_S, self._orphan_span_s('pending') - max(0.0, age)))))
                    self._orphan_info[path] = {'anchor': anchor, 'taken': time.monotonic()}
                self.log('orphan safe-stop record %s cannot be read - whether that runner switched '
                         'its outputs off is unknown. It is read again: Start waits while its runner '
                         '(pid %s) or the safe stop it hands on may still be running'
                         % (n, pid or '?'), 'error')
                bad.append((path, 'left a record that cannot be read'))
                continue
            status, ok = rec.get('status'), rec.get('ok')
            log = rec.get('log') or ''
            if rec.get('safe_stop'):
                # the process list knows a run of it as a safe stop in flight, tracked or not
                with self.lock:
                    self._orphan_scripts.add(str(rec['safe_stop']))
            if status == 'done' and ok is True:
                self.log('the panel died during %s; its runner ran the safe stop itself: OK%s%s'
                         % (target, ' (%s)' % rec['measurement'] if rec.get('measurement') else '',
                            ', log %s' % log if log else ''), 'warn')
                self._orphan_archive(path)
                continue
            # its age by the monotonic clock where the record has it (a step of the wall clock
            # used to shorten the hold, or make a fresh one look stale - #098 re-check)
            age = _rec_age(rec)
            fresh = age < self._ORPHAN_LIVE_S
            anchor = self._orphan_anchor(path, rec)
            # the runner itself, or the helper it hands the safe stop to (#098: the forced path
            # exits first and the helper runs the safe stop once it is gone), or 0 in the
            # moment between 'retry after exit' and the helper's pid being written down
            pid = self._orphan_holder(rec, anchor) if fresh else None
            rpid = rec.get('runner_pid')
            if (pid is None and not rec.get('retry_pid') and status in ('pending', 'running')
                    and isinstance(rpid, int) and rpid in self._ss_answered):
                # a runner this panel's SAFE STOP ended (it wrote this just before), and that
                # SAFE STOP then ran alone and succeeded: nothing of it is left to answer - it
                # used to raise a fresh "did not finish" fault after that SAFE STOP (#098 re-check)
                self.log('orphan safe-stop record %s: %s - its runner (pid %d) was ended by a SAFE '
                         'STOP of this panel, which then ran alone and succeeded: nothing more to '
                         'answer' % (n, target, rpid), 'warn')
                self._orphan_archive(path)
                continue
            if pid is not None:
                # held as long as the runner's own limits give its chain, on a clock that a step
                # of the wall clock cannot shorten (#098 re-check)
                until = time.monotonic() + max(0.0, min(self._ORPHAN_LIVE_S,
                                                        self._orphan_span_s(status) - max(0.0, age)))
                with self.lock:
                    self._orphans.append((path, int(pid), until))
                    self._orphan_info[path] = {'anchor': anchor, 'taken': time.monotonic(),
                                               'safe_stop': rec.get('safe_stop') or ''}
                if not pid:
                    pid = 'its helper is being started'
                self.log('the panel died during %s, and the runner\'s own safe stop is still running '
                         '(pid %s, %s): Start waits for it'
                         % (target, pid, status), 'warn')
                why = 'is still running (pid %s)' % pid
            elif status == 'done':
                why = 'failed: %s' % (rec.get('error') or 'no reason given')
            else:
                why = 'did not finish (%s)' % (status or 'no status')
            self.log('orphan safe-stop record %s: %s - %s%s' % (n, target, why,
                     ', log %s' % log if log else ''), 'error')
            bad.append((path, why))
        if bad:
            why = ('after the panel died mid-block (%s), the runner\'s own safe stop %s - the '
                   'state of the outputs is unknown' % (
                       os.path.basename(bad[0][0]), bad[0][1]))
            with self.lock:
                self._orphan_open.extend(p for p, _w in bad if p not in self._orphan_open)
                # A runner record that does not say "done, OK" is a restart that cut a block
                # off, whatever queue.json says (its save at the block's start may not have
                # landed, #007): the leftover check goes on and is kept in queue.json, so SAFE
                # STOP looks at the process list and ends what is left (#098 re-check)
                self._restart_check = True
                self._restarted = True
                if self.fault:
                    self.log('(the fault stands: %s)' % why, 'error')
                else:
                    self._set_fault(why)
                self._save_queue()

    def _orphan_archive(self, path):
        try:
            os.replace(path, path[:-len('.json')] + '.seen.json')
        except OSError:
            pass

    # 'retry after exit' is on disk a moment before the helper's pid is (the runner writes the
    # status, starts the helper, then writes retry_pid): that long, no pid still holds - and so
    # does a record that cannot be read, that long after the pid it names has ended
    _ORPHAN_SPAWN_S = 15.0
    # after a restart that cut a block off, how long a python.exe whose command line cannot be
    # read is taken as a runner that may not have written its record yet (it writes 'pending'
    # within about 2 s of noticing that its panel has gone - #098 re-check)
    _HIDDEN_WINDOW_S = 30.0

    def _orphan_holder(self, rec, anchor=None):
        """Who runs the safe stop for this record now: a live pid (the helper the runner
        handed the safe stop to, or the runner itself), 0 while the helper is being started,
        None when nobody does any more (done, or every pid it names has ended - or belongs now
        to a process created after the record was last written, `anchor`)."""
        if not isinstance(rec, dict) or rec.get('status') == 'done':
            return None
        for key in ('retry_pid', 'runner_pid'):
            pid = rec.get(key)
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 \
                    and self._orphan_alive(pid, anchor):
                return pid
        if rec.get('status') == 'retry after exit' and not rec.get('retry_pid') \
                and _finite(rec.get('updated')) is not None:
            if _rec_age(rec) < self._ORPHAN_SPAWN_S:
                return 0
        return None

    def _orphan_alive(self, pid, anchor):
        """A pid a record names is still that runner or its helper: alive, and not created after
        the record was last written (`anchor`, _same_process). A process whose age cannot be
        asked (another user's, an elevated one) is taken as it - unless the last process scan,
        made after that write, did not list it among the Python processes: then it is another
        program that was given the pid, and a hold on it would never end (#098 re-check).

        One the process list holds as a runner's helper or a run of the safe-stop script, still
        the process it listed, is taken as it too: a step of the wall clock back after it was
        created made it read as "created after the record" (#098 re-check, clock steps)."""
        v = self._ss_foreign.get(pid)
        if v is not None and _still_born(pid, v.get('born'), v.get('seen')):
            return True
        if not _same_process(pid, anchor):
            return False
        cache = self._ps_cache
        # a scan that ENDED this long after that write began after it (one takes up to 20 s)
        if (cache and anchor is not None and cache[0] > anchor + 25.0
                and _proc_created(pid) is None):
            return any(p == pid for p, _c in cache[1])
        return True

    def _orphan_state(self, k):
        """Under the lock: orphan record k is 'held' (its safe stop is still running, and waited
        for), 'stuck' (a process it names is still there past its hold: no longer waited for,
        and never taken as ended) or 'gone'. Its pid first (syscalls); once that has ended, the
        record is read again - the runner on the forced path hands the safe stop to a helper
        whose pid only the record knows (#098) - and one that cannot be read now is held a
        moment and read again, not taken as nothing running (#098 re-check)."""
        path, pid, until = self._orphans[k]
        info = self._orphan_info.setdefault(path, {})
        late = time.monotonic() >= until
        if pid and self._orphan_alive(pid, info.get('anchor')):
            return 'stuck' if late else 'held'
        rec, problem = self._orphan_read(path)
        if rec is None and problem != 'gone':
            # a scanner holds it, or it is being replaced: a moment's hold after the pid it named
            # has ended, then the process list decides (a helper it finds is held there)
            first = info.setdefault('unreadable_since', time.monotonic())
            return 'held' if not late and time.monotonic() - first < self._ORPHAN_SPAWN_S else 'gone'
        info.pop('unreadable_since', None)
        anchor = self._orphan_anchor(path, rec)
        info['anchor'] = anchor
        if rec is not None and rec.get('safe_stop'):
            info['safe_stop'] = rec['safe_stop']
            self._orphan_scripts.add(str(rec['safe_stop']))
        nxt = self._orphan_holder(rec, anchor)
        if nxt is None:
            return 'gone'
        if nxt != pid:
            self._orphans[k] = (path, nxt, until)
            if nxt:
                self._orphan_moves.append((path, pid, nxt))
                if nxt in self._ss_foreign:
                    # the process list found it too: the record's lines say how it ends
                    self._ss_foreign[nxt]['quiet'] = True
        if not late:
            return 'held'
        return 'stuck' if nxt else 'gone'

    def _orphan_holds(self, k):
        """Under the lock: does orphan record k still own the outputs - and is it waited for?"""
        return self._orphan_state(k) == 'held'

    def _orphan_busy(self):
        """Under the lock. A runner's own safe stop (or its retry) still running: two
        safe stops at once is the one thing never allowed. Syscalls only, and a read of the
        record once the pid it names has ended."""
        if not self._orphans:
            return False
        held = False
        for k in range(len(self._orphans)):
            held = self._orphan_holds(k) or held
        return held

    def _orphan_stuck_why(self):
        """Under the lock: the refusal while a runner's own safe stop is still running past its
        hold - no longer waited for, so SAFE STOP may run, but never taken as ended: Start and
        a trial refuse beside it, and the fault stays (#098 re-check). '' otherwise."""
        stuck = [self._orphans[k][1] for k in range(len(self._orphans))
                 if self._orphan_state(k) == 'stuck']
        # ... and one the process list found, past its own limits in the same way
        waited = {p for p, _v in self._ss_foreign_waited()}
        found = [p for p, _v in self._ss_foreign_live() if p not in waited and p not in stuck]
        if not stuck and not found:
            return ''
        return ('%s (pid %s) is still running, longer than its own limits allow - it may be stuck '
                'in a driver call, and it may still drive the outputs. End that python.exe in '
                'Task Manager, then press SAFE STOP. SAFE STOP is still allowed.'
                % ('the runner\'s own safe stop from before the restart' if stuck and not found
                   else 'a safe stop that this panel did not start',
                   ', '.join(str(p) for p in stuck + found)))

    def _orphan_running_own(self):
        """Outside the lock: {pid: record name} of the runners that are running their own safe
        stop now by the record that follows them ('running', and it names the runner as the one
        that runs the safe stop). A SAFE STOP of this panel never ends one (#098 re-check)."""
        with self.lock:
            tracked = [(path, pid) for path, pid, _u in self._orphans if pid]
        out = {}
        for path, pid in tracked:
            rec, _problem = self._orphan_read(path)
            if (isinstance(rec, dict) and rec.get('status') == 'running'
                    and rec.get('runner_pid') == pid):
                out[pid] = 'record %s' % os.path.basename(path)
        return out

    def _orphan_beside(self):
        """Under the lock: in words, the safe stops this panel did not start that are running
        now - a runner's own, waited for or not, and those the process list found - or ''.
        A safe stop of ours that runs beside one of them does not clear the fault."""
        parts = []
        pids = [self._orphans[k][1] for k in range(len(self._orphans))
                if self._orphan_state(k) != 'gone']
        if pids:
            named = ', '.join(str(p) for p in pids if p)
            parts.append('the runner\'s own safe stop from before the restart (%s)'
                         % ('pid ' + named if named else 'its helper being started'))
        fs = [(p, v) for p, v in self._ss_foreign_live() if p not in pids]
        if fs:
            parts.append(self._ss_foreign_words(fs))
        return '; '.join(parts)

    def _orphan_refresh(self):
        """Outside the lock: an orphan safe stop that has finished since - say how it went; one
        still running past its hold - say so, once, and keep it (#098 re-check: it used to be
        dropped, and 'has ended: no record' said of a live helper). While the restart check is
        on (see _relist_due), the folder is listed again for a record written since."""
        if self._relist_due():
            self._orphan_relist()
        with self.lock:
            states = [self._orphan_state(k) for k in range(len(self._orphans))]
            now = list(zip(self._orphans, states))
            self._orphans = [o for o, st in now if st != 'gone']
            done = [(o[0], o[1], bool(self._orphan_info.get(o[0], {}).get('stuck_said')))
                    for o, st in now if st == 'gone']
            stuck = []
            for o, st in now:
                info = self._orphan_info.setdefault(o[0], {})
                if st == 'stuck' and not info.get('stuck_said'):
                    info['stuck_said'] = True
                    stuck.append(o)
            moves, self._orphan_moves = self._orphan_moves, []
            self._ss_foreign_live()
            ended, self._ss_foreign_ended = self._ss_foreign_ended, []
        for path, old, new in moves:
            self.log('the runner (pid %s) of %s has exited and handed its safe stop to a helper '
                     '(pid %d), which runs it now: Start and SAFE STOP go on waiting for it'
                     % (old or '?', os.path.basename(path), new), 'warn')
        for path, pid, _until in stuck:
            taken = (self._orphan_info.get(path) or {}).get('taken')
            self.log('the runner\'s own safe stop (pid %s) is still running %s, longer than its '
                     'own limits allow - no longer waited for: SAFE STOP may run beside it, but '
                     'the fault stays, and Start and a trial are refused until it has ended. It may '
                     'be stuck in a driver call: end that python.exe in Task Manager'
                     % (pid, 'after %s of waiting for it' % _hms(time.monotonic() - taken)
                        if taken is not None else 'past its hold'), 'error')
        for path, pid, was_stuck in done:
            rec, _problem = self._orphan_read(path)
            rec = rec or {}
            late = ' (it had run past its hold)' if was_stuck else ''
            if rec.get('status') == 'done' and rec.get('ok') is True:
                self.log('the runner\'s own safe stop (pid %s) has finished%s: OK. The fault stays '
                         'until SAFE STOP is pressed and succeeds' % (pid or '?', late), 'warn')
            else:
                self.log('the runner\'s own safe stop (pid %s) has ended%s: %s%s'
                         % (pid or '?', late, rec.get('status') or 'no record',
                            ', %s' % rec['error'] if rec.get('error') else ''), 'error')
        for pid, v in ended:
            if not v.get('quiet'):
                self.log('%s (pid %d) has ended' % ('the runner\'s own safe stop, run by its helper'
                                                    if v.get('helper') else 'the run of the '
                                                    'safe-stop script that this panel did not '
                                                    'start', pid), 'warn')

    def _relist_due(self):
        """Is the orphan folder listed again now? While the leftover check is on, while a fault
        stands, and for the longest span of a runner chain after a restart that cut a block off
        (#098 re-check: a scan that could not see the old runner turned the check off, and the
        record that runner wrote a moment later was never read)."""
        return bool(self._restart_check or self.fault
                    or time.monotonic() < self.__dict__.get('_relist_until', 0.0))

    def _orphan_look(self):
        """Outside the lock: the orphan folder listed again (see _relist_due) and the process
        list read - the channel that does not depend on any record (#098 re-check)."""
        if self._relist_due():
            self._orphan_relist()
        try:
            self._leftover_runners()
        except Exception as exc:                            # noqa: BLE001 - never in the way
            _LEFT_SCAN.last_ok = False
            self.log('safe stop: the process list could not be read for a safe stop in flight '
                     '(%s: %s)' % (type(exc).__name__, exc), 'warn')

    def _orphan_what(self):
        """Under the lock: what a SAFE STOP waits for, for its log line."""
        if self._orphan_busy():
            return 'the runner\'s own safe stop'
        fs = self._ss_foreign_waited()
        return self._ss_foreign_words(fs) if fs else 'the safe stop that is running'

    def _orphan_wait(self):
        """Before our own safe stop: one this panel did not start owns the outputs - a
        runner's own, known by its record, or found in the process list (#098 re-check: the
        record was the only channel, and every way it failed ran two safe stops at once). Each
        is waited for as long as its own limits allow, and never ended. Returns what ours would
        run beside now, in words - '' when nothing (the caller looks again right before its
        script, and says so). One that ran beside another clears no fault."""
        # a runner's record not answered yet is a restart that cut a block off as well, whatever
        # queue.json said (#098 re-check)
        look = bool(self._restart_check or self._orphans or self._ss_foreign or self._orphan_open)
        if look:
            self._orphan_look()
        t0 = looked = time.monotonic()
        said = False
        while True:
            with self.lock:
                busy = self._orphan_busy() or bool(self._ss_foreign_waited())
            if not busy and said and look and time.monotonic() - looked >= 1.0:
                # what it waited for has ended: one more look, for a helper its record did not
                # name (a write that did not land)
                self._orphan_look()
                looked = time.monotonic()
                with self.lock:
                    busy = self._orphan_busy() or bool(self._ss_foreign_waited())
            if not busy:
                # the records once more before the wait is over: a runner's 'pending' that
                # landed while the first look ran was only named as running beside ours, and
                # that runner was then ended as a leftover in its own safe stop (#098 re-check)
                self._orphan_refresh()
                with self.lock:
                    busy = self._orphan_busy() or bool(self._ss_foreign_waited())
                if not busy:
                    break
            if not said:
                with self.lock:
                    what = self._orphan_what()
                self.log('waiting for %s to finish first' % what, 'warn')
                said = True
            if time.monotonic() - t0 > self._ORPHAN_LIVE_S + 60.0:
                self._orphan_refresh()
                break
            time.sleep(0.2)
        with self.lock:
            return self._orphan_beside()

    # ---- a safe stop this panel did not start, found in the process list (#098 re-check) ----

    def _ss_foreign_found(self, found):
        """From a process scan: [(pid, command line)] of a runner's helper of this state folder
        and of any run of the safe-stop script that is not this panel's own. Kept with the time
        they were seen and their creation time - a pid is the same process only while that has
        not changed - and each is said once."""
        now, mono = time.time(), time.monotonic()
        born = {pid: _proc_created(pid) for pid, _c in found}
        new = []
        with self.lock:
            # a helper its record already names is said by the record's lines, not twice
            named = {pid for _p, pid, _u in self._orphans if pid}
            keep = {}
            for pid, cmd in found:
                v = self._ss_foreign.get(pid)
                if v is None or v['cmd'] != cmd or (born.get(pid) is not None
                                                    and v.get('born') is not None
                                                    and abs(born[pid] - v['born']) >= 0.5):
                    # its limits run from when it started, as a record's from its last write;
                    # and it is the same process while its creation time is the one seen now
                    # (_still_born: never a wall-clock comparison, which a clock step moves)
                    age = max(0.0, now - born[pid]) if born.get(pid) else 0.0
                    v = {'cmd': cmd, 'seen': now, 'since': mono - age, 'quiet': pid in named,
                         'helper': '--orphan-retry' in _norm_path(cmd), 'born': born.get(pid)}
                    if not v['quiet']:
                        new.append((pid, v))
                keep[pid] = v
            self._ss_foreign_ended.extend((p, v) for p, v in self._ss_foreign.items()
                                          if p not in keep)
            self._ss_foreign = keep
        for pid, v in new:
            self.log('a safe stop that this panel did not start is running (pid %d, %s: %s) - '
                     'Start waits for it, SAFE STOP waits for it before its own, and it is never '
                     'ended here'
                     % (pid, 'a runner\'s own safe stop, handed to its helper' if v['helper']
                        else 'a run of the safe-stop script', v['cmd'][-160:]), 'warn')

    def _ss_foreign_live(self):
        """Under the lock: [(pid, entry)] of those still running - syscalls only. A process the
        scan listed is still it while its creation time has not changed (_still_born): a step of
        the wall clock back made a fresh helper read as created after it was seen, and it was
        dropped at once (#098 re-check)."""
        live = []
        for pid, v in list(self._ss_foreign.items()):
            if _still_born(pid, v.get('born'), v.get('seen')):
                live.append((pid, v))
            else:
                del self._ss_foreign[pid]
                self._ss_foreign_ended.append((pid, v))
        return live

    def _ss_foreign_waited(self):
        """Under the lock: those still running that a SAFE STOP still waits for, and beside
        which nothing starts - as long as their own limits allow from when they started (a
        helper's wait for its runner, a safe stop's timeout) and a minute more, and never longer
        than a record is held. Past that a SAFE STOP may run beside them, the fault stays, and
        the rest is refused as beside a stuck one (_orphan_stuck_why)."""
        ss = max(120.0, self._ss_timeout())
        mono = time.monotonic()
        return [(pid, v) for pid, v in self._ss_foreign_live()
                if mono - v['since'] < min(self._ORPHAN_LIVE_S,
                                           (60.0 if v['helper'] else 0.0) + ss + 60.0)]

    @staticmethod
    def _ss_foreign_words(fs):
        helpers = [str(p) for p, v in fs if v.get('helper')]
        scripts = [str(p) for p, v in fs if not v.get('helper')]
        parts = (['the runner\'s own safe stop, run by its helper (pid %s)' % ', '.join(helpers)]
                 if helpers else [])
        if scripts:
            parts.append('a run of the safe-stop script that this panel did not start (pid %s)'
                         % ', '.join(scripts))
        return ', and '.join(parts)

    # ---- what this panel had running when it ended (N31) ----
    # A small record, written while the panel's own safe-stop child runs (it is started so that
    # it outlives the panel, #103) and removed when it has ended the way it should. One found at
    # start means the last session ended under it.

    def _ss_child_path(self):
        return os.path.join(P.STATE_DIR, 'safe_stop_child.json')

    @staticmethod
    def _pids_in(rec):
        out = set()
        if isinstance(rec, dict):
            for p in [rec.get('pid')] + list(rec.get('older') or []):
                if isinstance(p, int) and not isinstance(p, bool) and p > 0:
                    out.add(p)
        return out

    def _read_left_markers(self):
        """At start. A safe stop of this panel running when the last session ended: it may still
        be running beside the next one (N31). That is a FAULT until a SAFE STOP succeeds, and the
        leftover scan looks for what may still be running."""
        notes = []
        ss_path = self._ss_child_path()
        if os.path.exists(ss_path):
            rec = P.read_json(ss_path, None, expect=dict)
            self._ss_left_pids |= self._pids_in(rec)
            if self._ss_left_pids:
                self._restart_check = True       # the scan looks for them
                self._restarted = True
            if (rec or {}).get('pid') or rec is None:
                notes.append('its own safe stop was running (%s), so whether that finished is '
                             'unknown' % ('pid %s, started %s' % (
                                 rec.get('pid'), time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(
                                     _finite(rec.get('started')) or 0))) if rec else 'record unreadable'))
        if not notes:
            return
        self._restart_check = True               # look for what may still be running
        self._restarted = True
        why = 'the panel ended while ' + '; and while '.join(notes)
        with self.lock:
            if self.fault:
                self.log('(the fault stands: %s)' % why, 'error')
            else:
                self._set_fault(why)
            # the safe-stop record stays until the scan has seen that child gone (it names the pid)
            self._save_queue()

    def _remove_quietly(self, path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            self.log('could not remove %s (%s): a restart would take it as still open'
                     % (os.path.basename(path), e), 'warn')

    def _ss_child_mark(self, pid):
        with self.lock:
            self._ss_child_pid = pid
            older = sorted(self._ss_left_pids)
        self._write_state('safe_stop_child.json', self._ss_child_path(),
                          {'pid': pid, 'started': time.time(), 'older': older})

    def _ss_child_unmark(self):
        with self.lock:
            self._ss_child_pid = None
            older = sorted(self._ss_left_pids)
        if older:
            self._write_state('safe_stop_child.json', self._ss_child_path(),
                              {'pid': None, 'older': older})
        else:
            self._remove_quietly(self._ss_child_path())
            with self.lock:
                self._save_fail.pop('safe_stop_child.json', None)

    def _leftovers_gone(self, check_off=False):
        """A scan found nothing of this panel's left running: forget the old pids - and, with
        `check_off`, stop looking. queue.json follows, so a restart does not bring them back."""
        with self.lock:
            had = bool(self._ss_left_pids)
            changed = had or (check_off and self._restart_check)
            self._ss_left_pids = set()
            if check_off:
                self._restart_check = False
                self._restarted = False
            running = self._ss_child_pid is not None
        if had and not running:
            self._remove_quietly(self._ss_child_path())
        if changed:
            self._save_queue()

    # ------------------------------------------------------------------ palette (the left column)

    def palette(self):
        """The function list (blocks.json), as stored. A missing file is an empty list."""
        data = P.read_json(P.BLOCKS_PATH, {'palette': []}, expect=dict) or {'palette': []}
        pal = data.get('palette', [])
        return [dict(b) for b in (pal if isinstance(pal, list) else []) if isinstance(b, dict)]

    def save_palette(self, palette):
        clean = []
        for b in palette:
            _check_kind(b)
            kind = b.get('kind') or 'script'
            clean.append({'id': b.get('id') or _uid('blk'),
                          'name': (b.get('name') or 'unnamed').strip(),
                          'kind': kind,
                          'target': (b.get('target') or '').replace('\\', '/'),
                          'params': b.get('params') or {},
                          'overrides': b.get('overrides') or {},
                          'override_base': b.get('override_base') or {},
                          'note': b.get('note') or '',
                          'color': b.get('color') or ''})
        P.write_json(P.BLOCKS_PATH, {'palette': clean})
        return clean

    # ------------------------------------------------------------------ the function list's own edits
    # The engine writes blocks.json by itself too (Duplicate), so a page that still holds the list it loaded earlier
    # must not put that list back over a new entry: the state carries the list's revision (palette_rev) and the page
    # loads the list again when it changes; the page's own save names the revision it was based on (base_rev), and an
    # older one is refused (409).

    PALETTE_CHANGED = ('the function list was not saved: it changed after this page loaded it (a function was added '
                       'elsewhere - a Duplicate, another tab). The list has been loaded again: make the change once '
                       'more')

    def _palette_rev_unlocked(self):
        try:
            with open(P.BLOCKS_PATH, 'rb') as f:
                raw = f.read()
        except OSError:
            return ''
        import hashlib
        return hashlib.sha1(raw).hexdigest()[:16]

    def palette_rev(self):
        """The function list's revision - a hash of blocks.json, '' when there is none. In the state (palette_rev),
        and what a page's save names as its base_rev."""
        with _PALETTE_LOCK:
            return self._palette_rev_unlocked()

    def palette_with_rev(self):
        """GET /api/palette: the list and its revision, read together (a write between the two would otherwise give
        a page a revision newer than the list it holds)."""
        with _PALETTE_LOCK:
            return self.palette(), self._palette_rev_unlocked()

    def save_palette_from(self, palette, base_rev=None):
        """POST /api/palette: the whole list a page sends, saved as save_palette() saves it - refused (RuntimeError,
        409) when the page names the revision it was based on and blocks.json has changed since: the entry added
        meanwhile would be lost under the older list ('' is a revision too: the list as it was before blocks.json
        existed). A request without base_rev (a script, an older page) is saved, but every entry the engine added
        since the panel started (Duplicates - _palette_track) that the list leaves out is put back, where it was, and
        the log says so: such a save used to delete every entry the engine had added since, without a word. A save
        that names its revision and leaves one out deletes it on purpose (the page had it): it is not tracked any
        more."""
        put_back = []
        with _PALETTE_LOCK:
            if base_rev is not None and str(base_rev) != self._palette_rev_unlocked():
                raise RuntimeError(self.PALETTE_CHANGED)
            auto = getattr(self, '_palette_auto', None)
            if auto is None:
                auto = self._palette_auto = {}
            sent = {b.get('id') for b in palette if isinstance(b, dict) and b.get('id')}
            missing = [i for i in auto if i not in sent]
            if base_rev is None and missing:
                palette = list(palette)
                for i in missing:
                    rec = auto[i]
                    ent = copy.deepcopy(rec['entry'])
                    ids = [b.get('id') if isinstance(b, dict) else None for b in palette]
                    if rec.get('after') and rec['after'] in ids:
                        at = ids.index(rec['after']) + 1
                    else:                     # its neighbour is gone too: after the last of its kind, else at the end
                        same = [k for k, b in enumerate(palette) if isinstance(b, dict) and b.get('kind') == ent.get('kind')]
                        at = same[-1] + 1 if same else len(palette)
                    palette.insert(at, ent)
                    put_back.append(ent.get('name') or i)
            else:
                for i in missing:
                    auto.pop(i, None)         # removed on a page that had it: on purpose
            saved = self.save_palette(palette)
            for b in saved:
                if b.get('id') in auto:
                    auto[b['id']]['entry'] = copy.deepcopy(b)        # as it is now (renamed, edited)
        if put_back:
            self.log('function list: a page that does not name the list\'s revision (one loaded before the panel was '
                     'updated - reload it with Ctrl+F5) saved a list without %s, added by the panel since it started - '
                     'put back, so nothing the panel added is lost; delete it from a reloaded page if it is not wanted'
                     % ', '.join('"%s"' % n for n in put_back), 'warn')
        return saved

    def _palette_track(self, entry, after_id):
        """Under _PALETTE_LOCK: an entry the engine itself just wrote into blocks.json (a Duplicate) and the id of the
        entry it came right after - kept for this panel's life, so a save from a page that never saw it
        (save_palette_from without base_rev) cannot delete it."""
        auto = getattr(self, '_palette_auto', None)
        if auto is None:
            auto = self._palette_auto = {}
        if isinstance(entry, dict) and entry.get('id'):
            auto[entry['id']] = {'entry': copy.deepcopy(entry), 'after': after_id}

    def _palette_raw(self):
        """Under _PALETTE_LOCK: blocks.json as it is - (the whole object, its list of entries as stored)."""
        data = P.read_json(P.BLOCKS_PATH, None, expect=dict)
        if data is None:
            if os.path.isfile(P.BLOCKS_PATH):
                raise RuntimeError('blocks.json cannot be read - the function list was not changed')
            data = {'palette': []}
        pal = data.get('palette', [])
        if not isinstance(pal, list):
            raise RuntimeError('blocks.json holds no list of functions - the function list was not changed')
        return data, list(pal)

    def _palette_write_raw(self, data, pal):
        """Under _PALETTE_LOCK: the list written back with every entry it was read with, unchanged (only what the
        caller added); the file as it was is kept as state/blocks.json.bak first, as a page's save keeps it."""
        try:
            if os.path.isfile(P.BLOCKS_PATH):
                with open(P.BLOCKS_PATH, 'rb') as f:
                    old = f.read()
                with open(os.path.join(P.STATE_DIR, 'blocks.json.bak'), 'wb') as f:
                    f.write(old)
        except OSError:
            pass                                      # a backup must never stop the save
        out = dict(data)
        out['palette'] = pal
        P.write_json(P.BLOCKS_PATH, out)

    @staticmethod
    def _palette_new_id(pal):
        ids = {b.get('id') for b in pal if isinstance(b, dict)}
        while True:
            new = _uid('blk')
            if new not in ids:
                return new

    def duplicate_palette_entry(self, entry_id):
        """Item 2: a copy of one function, right after it - a new id, the name "<name> (copy)" ("(copy 2)", ... when
        that name is taken), everything else as the original has it. The original and every other entry are written
        back exactly as they were. ValueError (400) without an id; RuntimeError (409) when that function is not in
        the list any more (removed meanwhile, another tab)."""
        eid = str(entry_id or '').strip()
        if not eid:
            raise ValueError('duplicate: say which function (its id)')
        with _PALETTE_LOCK:
            data, pal = self._palette_raw()
            k = next((i for i, b in enumerate(pal) if isinstance(b, dict) and b.get('id') == eid), None)
            if k is None:
                raise RuntimeError('not duplicated: that function is not in the function list any more (removed or '
                                   'changed elsewhere) - the list has been loaded again')
            base = (str(pal[k].get('name') or '').strip()) or 'unnamed'
            names = {str(b.get('name') or '').strip() for b in pal if isinstance(b, dict)}
            name, n = '%s (copy)' % base, 2
            while name in names:
                name, n = '%s (copy %d)' % (base, n), n + 1
            new = copy.deepcopy(pal[k])
            new['id'] = self._palette_new_id(pal)
            new['name'] = name
            self._palette_write_raw(data, pal[:k + 1] + [new] + pal[k + 1:])
            self._palette_track(new, eid)             # round 6 review #20 / #22: an older page's save keeps it
        self.log('function list: "%s" duplicated as "%s" (right after it)' % (base, name), 'ok')
        return new

    def _fresh_seq_name(self):
        """The name a new sequence gets after a Clear (default_seq_name, the minute) - one no saved sequence has, so
        a Save after a Clear never offers to replace the sequence that was cleared. Under the lock."""
        base = default_seq_name()
        taken = {str(s.get('name') or '').strip() for s in (getattr(self, 'saved', None) or [])
                 if isinstance(s, dict)}
        name, n = base, 2
        while name in taken:
            name, n = '%s (%d)' % (base, n), n + 1
        return name

    def list_scripts(self):
        """Every .py under the configured search dirs - the picker for a new palette entry."""
        out = []
        for d in (self.cfg['paths'].get('script_search_dirs') or []):
            abs_d = P.resolve(d)
            if not os.path.isdir(abs_d):
                continue
            for name in sorted(os.listdir(abs_d)):
                if not name.endswith('.py') or name.startswith('__'):
                    continue
                path = os.path.join(abs_d, name)
                try:
                    with open(path, 'r', encoding='utf-8', errors='replace') as f:
                        src = f.read()          # whole file: main() is often far down
                except OSError:
                    continue
                m = re.search(r'^def main\s*\(([^)]*)\)', src, re.M)
                out.append({'path': P.rel_to_root(path), 'name': name,
                            'has_main': m is not None,
                            'main': m.group(1).strip() if m else None})
        return out

    def preview_overrides(self, failed=None):
        """For the confirm-before-start dialog: every pending block that rewrites its script,
        and every pending block the panel already knows something about - a value it cannot
        use, a script that is not there or cannot be imported, a parameter main() does not
        take.

        A block that cannot be read is THAT block's problem, never a failed request: one bad
        value used to make the whole check fail, and the page then started without it
        (blind review #031).

        `failed` ('retry' / 'skip'): the check of the sequence as that Start leaves it (U03) -
        a retried block is checked as pending, a skipped one as skipped."""
        failed = _failed_choice(failed)
        out = []
        with self.lock:
            items = _as_started(list(self.seq['items']), failed)
        # tests borrow this method onto a stand-in with no config: the checks that need one
        # (the import check, data_root) are simply not made there
        cfg = getattr(self, 'cfg', None)
        sim = bool((cfg or {}).get('sim')) if isinstance(cfg, dict) else True
        for i, it in enumerate(items):
            try:
                ov = live_overrides(it.get('overrides'))
                if it['kind'] != 'script' or not ov or it['status'] not in ('pending',):
                    continue
                target = P.resolve(it.get('target'))
                # EVERY key is checked, with the line's source where it was recorded (#033)
                base = it.get('override_base') or {}
                if not (target and os.path.isfile(target)):
                    # both the scanners below would fail on this, each reporting the same errno,
                    # and neither would say what to do about it
                    problems, changes, more = [_no_script(target)], [], []
                else:
                    problems = panel_scan.verify(target, {k: base.get(k) for k in ov})
                    changes, more = panel_scan.preview(target, ov)
                warnings = []
                if target and os.path.isfile(target):
                    warnings = self._script_cautions(it, target, ov, _reps_hint(it))
                if not (changes or problems or more or warnings):
                    continue
                out.append({'n': i + 1, 'uid': it['uid'], 'name': it['name'],
                            'script': P.rel_to_root(target), 'reps': (it['params'] or {}).get('reps'),
                            'changes': list(changes),
                            'problems': problems + more,
                            'warnings': warnings,
                            'by': it.get('by', 'you')})
            except Exception as e:                                      # noqa: BLE001
                out.append(_preview_entry(i, it, problems=[_unchecked(e)]))

        # A script that is not there. The loop above only examines blocks that rewrite a
        # parameter, so a block with no overrides was never looked at - and a script that
        # has been renamed, moved, or lost to a half-finished sync used to get all the way
        # to its turn in the queue before anyone found out.
        seen = {e['uid'] for e in out}
        for i, it in enumerate(items):
            try:
                if it['kind'] != 'script' or it['status'] != 'pending' or it['uid'] in seen:
                    continue
                target = P.resolve(it.get('target'))
                if target and os.path.isfile(target):
                    # no overrides, but the script may still deserve a word (a no-main script
                    # that loops itself, F06)
                    warnings = self._script_cautions(it, target, {}, _reps_hint(it))
                    if warnings:
                        out.append({'n': i + 1, 'uid': it['uid'], 'name': it['name'],
                                    'script': P.rel_to_root(target),
                                    'reps': (it['params'] or {}).get('reps'), 'changes': [],
                                    'problems': [], 'warnings': warnings, 'by': it.get('by', 'you')})
                    continue
                out.append({
                    'n': i + 1, 'uid': it['uid'], 'name': it['name'],
                    'script': P.rel_to_root(target) if target else '(no script set)',
                    'reps': (it.get('params') or {}).get('reps'), 'changes': [],
                    'by': it.get('by', 'you'),
                    'problems': [_no_script(target)]})
            except Exception as e:                                      # noqa: BLE001
                out.append(_preview_entry(i, it, problems=[_unchecked(e)]))

        by_entry = {x['uid']: x for x in out}

        def entry_for(i, it):
            ent = by_entry.get(it.get('uid'))
            if ent is None:
                ent = by_entry[it.get('uid')] = _preview_entry(i, it)
                out.append(ent)
            ent.setdefault('problems', [])
            ent.setdefault('warnings', [])
            return ent

        # A half-run block: what Start will do with the reps it already has - go on, start
        # again from rep 1 because its values changed (blind review #087), or nothing more
        # because every rep is measured (#010, #084). Said before Start, not only in the log.
        for i, it in enumerate(items):
            try:
                done = int(_num((it.get('progress') or {}).get('done'), 0))
                if it['kind'] != 'script' or it['status'] != 'pending' or done <= 0:
                    continue
                with self.lock:
                    changed = _plan_changes(_last_exec(it), it)
                reps = _reps_hint(it)
                if changed:
                    note = ('%d rep(s) were measured with other values (%s): it starts again from '
                            'rep 1, and those reps stay in History'
                            % (done, '; '.join(changed[:4])))
                elif done < reps and _ee_complete(it, done, reps):
                    note = ('it was ended early after rep %d of %d ("Next block after this rep"): Start '
                            'completes it without measuring again. Press ↺ on it to measure it again '
                            'from rep 1' % (done, reps))
                elif done >= reps:
                    note = ('all %d rep(s) are already measured: Start completes it without measuring '
                            'again. Press ↺ on it to measure it again' % reps)
                else:
                    continue
                entry_for(i, it)['warnings'].append(note)
            except Exception as e:                                      # noqa: BLE001
                entry_for(i, it)['problems'].append(_unchecked(e))

        # Every pending block's own values, whatever its kind: '90 min', reps 'inf', a
        # parameter main() cannot take. Each used to be replaced by a default, or dropped, when
        # the block ran (blind review #030, #032, #035).
        imports = []
        for i, it in enumerate(items):
            try:
                if it.get('status') != 'pending':
                    continue
                kind = it.get('kind') or 'script'
                if kind not in KINDS:
                    # a queue kept from another panel: a kind this one cannot run
                    entry_for(i, it)['problems'].append(
                        'this panel cannot run a block of kind %r - remove it (a block is one of %s)'
                        % (kind, ', '.join(KINDS)))
                    continue
                bad = _param_problems(it, cfg)
                if kind == 'script':
                    # in real mode, only a script of the panel's script folders runs (#044, N10)
                    where = _target_refusal(cfg, it.get('target'))
                    if where:
                        bad.append(where)
                    bad += _main_arg_problems(it.get('target'), it.get('params'))
                    opaque = _opaque_main_note(it.get('target'), it.get('params'))
                    if opaque:
                        entry_for(i, it)['warnings'].append(opaque)
                    target = P.resolve(it.get('target'))
                    if target and not _is_share(it.get('target')) and os.path.isfile(target):
                        mods = _top_imports(target)
                        if mods:
                            imports.append((i, it, target, mods))
                if bad:
                    entry_for(i, it)['problems'].extend(bad)
            except Exception as e:                                      # noqa: BLE001
                entry_for(i, it)['problems'].append(_unchecked(e))

        # What the scripts import, looked up the way the runner will (blind review #106): a
        # simulated run never imports them, so nothing else finds a module missing from this
        # machine before the block's turn comes.
        if imports and isinstance(cfg, dict):
            uniq = list(dict.fromkeys(t for _i, _it, t, _m in imports))
            mods_of = {t: m for _i, _it, t, m in imports}
            found, why = _missing_imports(cfg, [(t, mods_of[t]) for t in uniq])
            if found is None:
                self.log('the import check before Start could not run (%s) - the scripts\' '
                         'imports were not checked' % why, 'warn')
            else:
                for i, it, target, _m in imports:
                    # a script that changes sys.path itself may find the module where it adds
                    # it, which a lookup on the runner's own path cannot follow: a warning, not
                    # a reason to refuse Start (round 2 G02)
                    moved = _path_changes(target)
                    for miss in found.get(target) or []:
                        try:
                            name, part, local = miss[0], miss[1], bool(miss[2])
                        except (TypeError, IndexError):
                            continue
                        ent = entry_for(i, it)
                        if moved:
                            note = ('%s imports %s, which is not found on the runner\'s own '
                                    'sys.path - but %s changes sys.path itself (line %s), so it may '
                                    'be found where the script adds it. The check cannot follow '
                                    'that, so this is only a warning; if %s is really missing, '
                                    'the block stops with "import failed" when it starts.'
                                    % (os.path.basename(target), name, os.path.basename(target),
                                       ', '.join(str(n) for n in moved[:3]), part))
                        else:
                            note = _import_note(target, name, part, local, cfg, sim)
                        (ent['warnings'] if (sim or moved) else ent['problems']).append(note)

        # where the datasets really go, once for the sequence (blind review #107)
        note = _data_root_note(cfg)
        if note:
            first = next(((i, it) for i, it in enumerate(items)
                          if it.get('status') == 'pending' and it.get('kind') == 'script'), None)
            if first:
                entry_for(*first)['warnings'].append(note)
        out.sort(key=lambda e: e['n'])
        return out

    def plan_estimate(self, failed=None):
        """The Start dialog's plan (U30): every block still to run, in order, with estimates
        from History only - see _plan_estimate. `failed` as for Start. Copied under the lock,
        computed outside it (it reads each script's digest)."""
        failed = _failed_choice(failed)
        with self.lock:
            items = copy.deepcopy(self.seq['items'])
            history = list(self.history)
            sim = bool(self.cfg.get('sim'))
        return _plan_estimate(items, history, sim, time.time(), failed=failed,
                              ss_timeout=self._ss_timeout())

    def scan_script(self, path):
        """The parameters the UI offers when you import a script."""
        abs_path = P.resolve(path)
        if not os.path.isfile(abs_path):
            return {'error': 'no such script: %s' % abs_path, 'params': []}
        info = panel_scan.scan(abs_path)
        info['path'] = P.rel_to_root(abs_path)
        info['name'] = os.path.basename(abs_path)
        return info

    # ------------------------------------------------------------------ sequence editing

    def _item_from_block(self, block, params=None):
        # `block` is either a palette entry (key 'id') or an already-built item from a saved
        # sequence (key 'block_id'). Reading only 'id' meant a save -> load -> save round trip
        # dropped the link back to the palette, and that link is what lets a damaged item be
        # repaired later.
        merged = dict(block.get('params') or {}, **(params or {}))
        return {'uid': _uid('it'),
                'block_id': block.get('id') or block.get('block_id'),
                'name': block.get('name') or 'block',
                'kind': block.get('kind') or 'script',
                'target': block.get('target') or '',
                # a None is "not set" (contract C3), never a value the script is handed
                'params': copy.deepcopy({k: v for k, v in merged.items() if v is not None}),
                'overrides': copy.deepcopy(dict(block.get('overrides') or {})),
                'override_base': copy.deepcopy(dict(block.get('override_base') or {})),
                'by': block.get('by') or 'you',
                'color': block.get('color') or '',
                'status': 'pending', 'message': '',
                'progress': {'done': 0, 'total': 0},
                'started': None, 'finished': None, 'datasets': []}

    def add_item(self, block_id=None, block=None, params=None, index=None, by='you', for_of=None):
        with self.lock:
            src = block
            if src is None:
                src = next((b for b in self.palette() if b.get('id') == block_id), None)
            if src is None:
                raise KeyError('unknown block %r' % block_id)
            _check_kind(src)
            _check_target(self.cfg, src)
            clean = _clean_params(params, src.get('kind') or 'script', src.get('target'), self.cfg, src.get('name') or 'block')
            _check_removals(src.get('kind'), src.get('params'), clean, src.get('name') or 'block')
            item = self._item_from_block(src, clean)
            item['by'] = by
            if isinstance(for_of, dict) and for_of.get('name'):
                item['for'] = {'name': str(for_of['name'])[:24], 'msg_id': for_of.get('msg_id')}
            items = self.seq['items']
            index = len(items) if index is None else max(0, min(int(index), len(items)))
            items.insert(index, item)
            self._place_cursor()
            self._save_queue()
            self.log('added "%s" at #%d%s' % (item['name'], index + 1, '   [by the agent]' if by == 'agent' else ''), 'info', item['uid'])
            return item

    def update_item(self, uid, patch, by='you'):
        with self.lock:
            it = self._find(uid)
            patch = patch or {}
            # Everything is checked before anything is changed: a refused patch changes nothing.
            want = patch.get('expect_status')
            if want and it.get('status') != want:
                # the agent read the block, then asked for a change; it moved on meanwhile
                raise RuntimeError('"%s" is %s now, not %s - nothing was changed'
                                   % (it['name'], it.get('status'), want))
            if patch.get('reset') and _running(self, it):
                # Reset under a running block emptied the worker's item: started=None crashed
                # the worker when the block ended, and the queue stopped (blind review #008)
                raise RuntimeError('"%s" is running (or still being wound down after a stop) - it '
                                   'cannot be reset now. Stop it or let it finish, then reset it.'
                                   % it['name'])
            # the values are checked against what they are FOR: the block's kind, and the
            # script it will run once this patch is in (C4; blind review #030, #032)
            new_target = patch.get('target') if isinstance(patch.get('target'), str) \
                and patch['target'].strip() else it.get('target')
            if new_target != it.get('target'):
                _check_target(getattr(self, 'cfg', None),
                              {'kind': it.get('kind'), 'target': new_target, 'name': it.get('name')})
            params = _clean_params(patch['params'], it.get('kind') or 'script', new_target,
                                   getattr(self, 'cfg', None), it.get('name')) \
                if isinstance(patch.get('params'), dict) else None
            _check_removals(it.get('kind'), it.get('params'), params, it.get('name'))
            if isinstance(patch.get('overrides'), dict):
                _check_overrides(new_target, patch['overrides'], it.get('name'))
            before = _plan_of(it)
            # A patch that merely OMITS a field must not erase it. A JSON caller that
            # builds its body with dict.get() sends null for everything it did not set, and
            # `if k in patch` happily wrote that null - which is how the block names went
            # missing after an agent reset. None/'' is never a legitimate value here.
            for k in ('name', 'target', 'color'):
                v = patch.get(k)
                if isinstance(v, str) and v.strip():
                    it[k] = v.strip()
            if params is not None:
                _apply_params(_params_of(it), params)       # None removes (C3)
            if 'overrides' in patch and isinstance(patch['overrides'], dict):
                if patch.get('replace_overrides'):      # the picker sends the whole set
                    it['overrides'] = dict(patch['overrides'])
                    if isinstance(patch.get('override_base'), dict):
                        it['override_base'] = dict(patch['override_base'])
                else:
                    was_ov = dict(it.get('overrides') or {})
                    it['overrides'] = _merge_overrides(it.get('target'), it.get('overrides'),
                                                       patch['overrides'])
                for k, v in list(it['overrides'].items()):
                    if v is None:                       # null = go back to the value in the file
                        it['overrides'].pop(k)
                if not patch.get('replace_overrides'):
                    # the source lines follow the merge, so the linspace checks still hold (R05)
                    it['override_base'] = _merge_base(was_ov, it['overrides'],
                                                      it.get('override_base'),
                                                      patch['overrides'],
                                                      patch.get('override_base'))
            if patch.get('reset'):
                was = it.get('status')
                self._reset_item(it)
                self._place_cursor()            # Start resumes from here: say so
                # '↺ this one and the N below' is one reset per block, and none of them was
                # in the log: a re-run looked unexplained the next morning (blind review #017)
                self.log('"%s" set back to pending (↺, it was %s) - it runs again at its turn%s'
                         % (it['name'], was, '   [by the agent]' if by == 'agent' else ''),
                         'warn', uid)
                if was == 'error':
                    # the last failed block reset: no Retry / Skip left to make (U03, #117)
                    self._refinish_if_no_failures('"%s" was set back to pending' % it['name'])
            elif params is not None or isinstance(patch.get('overrides'), dict):
                note = _edit_note(self, it, before)
                if note:
                    self.log(note, 'warn', uid)
            self._save_queue()
            return it

    @staticmethod
    def _reset_item(it):
        """Back to "has not run": status, progress, what it produced - and its datasets.

        The datasets go too. They are the record of the LAST run; kept, the history counted
        the old folders a second time. History has its own copy of the list, so nothing is
        lost.
        """
        it.update(status='pending', message='', progress={'done': 0, 'total': 0},
                  started=None, finished=None, datasets=[], datasets_partial=[])
        it.pop('stall', None)                # the card's notes of the last run (#026, C6)
        it.pop('runner_notes', None)
        it.pop('exec', None)
        it.pop('exec_last', None)
        it.pop('segment', None)
        it.pop('seg_ds_from', None)
        it.pop('seg_dsp_from', None)
        it.pop('seg_rep_from', None)
        it.pop('ended_early', None)          # "Next block after this rep" of the last run

    def commit_draft(self, draft, by='you'):
        """Apply a whole edit of the sequence in one go, under one lock.

        The page used to send it as a series of requests - add, delete, reorder, then the
        parameter edits - and between two of them the worker was free to pick up a block
        that had been added but not yet given its overrides: it ran with the file's values
        while the page showed the edited ones (F09). Here nothing is visible to the worker
        until all of it is.

            draft = {'adds':    [{'tid': '<temp id>', 'block_id': .. | 'block': {..},
                                  'params': {..}, 'overrides': {..}, 'override_base': {..}}],
                     'deletes': ['<uid>', ..],
                     'order':   ['<uid>' | '__new<tid>', ..],
                     'patches': [{'uid': .., 'params': {..}, 'overrides': {..},
                                  'override_base': {..}, 'replace_overrides': bool}]}

        Returns {'uids': {'__new<tid>': uid}} so the page can map its temporary ids, and
        'warnings': what the edit means for a running or half-run block.

        Atomic: every uid and every value is checked before anything is changed, so a refused
        commit changes nothing (blind review #022). With draft['id'], a commit that arrives a
        second time (the page retrying after a lost answer) is applied once.
        """
        draft = draft or {}
        did = str(draft.get('id') or '').strip()
        with self.lock:
            if did:
                for d, res in self._drafts:
                    if d == did:
                        self.log('the same sequence save arrived twice - it was applied once, the '
                                 'first time', 'warn')
                        return dict(res, items=self.seq['items'], repeat=True)
            palette = self.palette()
            new_by_tid = {}
            made = []
            for a in draft.get('adds') or []:
                src = a.get('block')
                if src is None:
                    src = next((b for b in palette if b.get('id') == a.get('block_id')), None)
                if src is None:
                    raise KeyError('unknown block %r' % a.get('block_id'))
                _check_kind(src)
                _check_target(self.cfg, src)
                clean = _clean_params(a.get('params'), src.get('kind') or 'script',
                                      src.get('target'), self.cfg, src.get('name') or 'block')
                _check_removals(src.get('kind'), src.get('params'), clean,
                                src.get('name') or 'block')
                item = self._item_from_block(src, clean)
                item['by'] = by
                _check_overrides(src.get('target'), a.get('overrides'), src.get('name'))
                if isinstance(a.get('overrides'), dict):
                    item['overrides'] = copy.deepcopy(a['overrides'])
                if isinstance(a.get('override_base'), dict):
                    item['override_base'] = copy.deepcopy(a['override_base'])
                if did:
                    # which Save made it: the page's commitLanded tells its own block from a
                    # same-name one with the same values that another tab or a Load added (R06)
                    item['draft_id'] = did
                new_by_tid['__new' + str(a.get('tid') or len(made))] = item
                made.append(item)
            uid_of = {tid: it['uid'] for tid, it in new_by_tid.items()}
            items = self.seq['items']
            by_uid = {i['uid']: i for i in items}
            for uid in draft.get('deletes') or []:
                if uid in by_uid and self.current_uid == uid:
                    raise RuntimeError('this block is running (or still being wound down after '
                                       'a stop) - wait for it to finish first')
            for it in made:
                by_uid[it['uid']] = it
            for uid in draft.get('deletes') or []:
                by_uid.pop(uid, None)
            order = [uid_of.get(u, u) for u in (draft.get('order') or [])]
            unknown = [u for u in order if u not in by_uid]
            if unknown:
                raise KeyError('no such block(s): %s' % ', '.join(unknown))
            new_order, kept_here = _merge_order(items, by_uid, order, made)
            # every patch checked before the first is applied: a patch naming a block that is
            # gone used to raise after the earlier ones had already changed live blocks
            patches = []
            for pt in draft.get('patches') or []:
                uid = uid_of.get(pt.get('uid'), pt.get('uid'))
                it = by_uid.get(uid)
                if it is None:
                    raise KeyError('no such block: %s' % pt.get('uid'))
                params = _clean_params(pt['params'], it.get('kind') or 'script', it.get('target'),
                                       self.cfg, it.get('name')) \
                    if isinstance(pt.get('params'), dict) else None
                _check_removals(it.get('kind'), it.get('params'), params, it.get('name'))
                if isinstance(pt.get('overrides'), dict):
                    _check_overrides(it.get('target'), pt['overrides'], it.get('name'))
                patches.append((it, pt, params))
            warnings = []
            for it, pt, params in patches:
                before = _plan_of(it)
                if params is not None:
                    _apply_params(_params_of(it), params)   # None removes (C3)
                if isinstance(pt.get('overrides'), dict):
                    ov = dict(pt['overrides'])
                    if pt.get('replace_overrides'):
                        it['overrides'] = ov
                        if isinstance(pt.get('override_base'), dict):
                            it['override_base'] = copy.deepcopy(pt['override_base'])
                    else:
                        was_ov = dict(it.get('overrides') or {})
                        it['overrides'] = _merge_overrides(it.get('target'), it.get('overrides'), ov)
                    for k, v in list(it['overrides'].items()):
                        if v is None:
                            it['overrides'].pop(k)
                    if not pt.get('replace_overrides'):
                        it['override_base'] = _merge_base(was_ov, it['overrides'],
                                                          it.get('override_base'), ov,
                                                          pt.get('override_base'))
                if params is not None or isinstance(pt.get('overrides'), dict):
                    # the page's Save said "the run follows this" even for the running block,
                    # whose run keeps the values it started with (blind review #020)
                    note = _edit_note(self, it, before)
                    if note:
                        warnings.append(note)
                        self.log(note, 'warn', it['uid'])
            # only now does the worker get to see any of it
            self.seq['items'] = new_order
            for it, before_uid in kept_here:
                # a block added elsewhere (the agent, another tab) that this edit did not know
                # of: kept where it was, not moved behind the measurements (#090, round 2 I07)
                note = ('"%s" was added elsewhere while this edit was being made; it keeps its '
                        'place%s' % (it.get('name'), ', before "%s"' % by_uid[before_uid]['name']
                                     if before_uid in by_uid else ', at the end'))
                warnings.append(note)
                self.log(note, 'warn', it['uid'])
            if draft.get('deletes'):
                # the last failed block removed: no Retry / Skip left to make (U03, #117)
                self._refinish_if_no_failures('it was removed in an edit of the sequence')
            self._place_cursor()
            # never raises (#007): a save that fails is on the page, and memory is right - the
            # page must not be told to save again, which added its new blocks twice (#022)
            self._save_queue()
            for it in made:
                # the agent adds through here now: the log line keeps saying who (#018)
                self.log('added "%s" at #%d%s' % (it['name'], self.seq['items'].index(it) + 1,
                                                  '   [by the agent]' if by == 'agent' else ''),
                         'info', it['uid'])
            if draft.get('deletes'):
                self.log('removed %d block(s)' % len(draft['deletes']))
            self.log('sequence saved in one step: %d block(s)' % len(self.seq['items']))
            res = {'uids': uid_of, 'warnings': warnings}
            if did:
                self._drafts = (self._drafts + [(did, res)])[-20:]
            return dict(res, items=self.seq['items'])

    def delete_item(self, uid, by='you'):
        with self.lock:
            idx = self._index(uid)
            if self.current_uid == uid:
                # running, OR still being wound down after a stop: the worker has yet to
                # write its History row, and deleting it under the worker crashed the run
                # (agent A, finding A2)
                raise RuntimeError('this block is running (or still being wound down after a '
                                   'stop) - wait for it to finish first')
            it = self.seq['items'].pop(idx)
            # the last failed block gone: no Retry / Skip left to make (U03, #117)
            self._refinish_if_no_failures('"%s" was removed' % it.get('name'))
            self._place_cursor()
            self._save_queue()
            # a delete was in no log at all; one of a failed block is a decision about it (U03)
            self.log('removed "%s" (#%d, it was %s)%s'
                     % (it.get('name'), idx + 1, it.get('status'),
                        '   [by the agent]' if by == 'agent' else ''),
                     'warn' if it.get('status') == 'error' else 'info', uid)

    def move_item(self, uid, new_index):
        with self.lock:
            idx = self._index(uid)
            items = self.seq['items']
            new_index = max(0, min(int(new_index), len(items) - 1))
            items.insert(new_index, items.pop(idx))
            self._place_cursor()
            self._save_queue()

    def duplicate_item(self, uid, by='you'):
        """A copy of one block of the sequence, inserted right after it - a new uid, status
        pending, by `by`, the name "<name> (copy)" ("(copy 2)", ... when the sequence has that
        name already: the function list's rule), and the same kind / target / params / overrides
        / override_base / colour / palette link. Built by _item_from_block, the constructor of
        every new block (add_item, commit_draft): nothing of a run comes with it - status,
        progress, messages, datasets, exec records, a trial.

        Refused where add_item refuses, with its checks on the values the copy would have: a
        script that is not one of the panel's (ValueError, 400); an unknown uid is a KeyError
        (400). Like add_item it is not refused while the queue runs: the copy is pending and
        runs at its turn. Returns the new item."""
        with self.lock:
            idx = self._index(uid)
            it = self.seq['items'][idx]
            base = str(it.get('name') or '').strip() or 'block'
            names = {str(x.get('name') or '').strip() for x in self.seq['items']}
            name, n = '%s (copy)' % base, 2
            while name in names:
                name, n = '%s (copy %d)' % (base, n), n + 1
            kind = it.get('kind') or 'script'
            src = {'block_id': it.get('block_id'), 'name': name, 'kind': kind, 'target': it.get('target') or '',
                   'params': copy.deepcopy(dict(it.get('params') or {})),
                   'overrides': copy.deepcopy(dict(it.get('overrides') or {})),
                   'override_base': copy.deepcopy(dict(it.get('override_base') or {})),
                   'color': it.get('color') or ''}
            # add_item's checks, as for a palette entry added as it is (no params patch); a refusal names the block
            # the person asked to copy
            _check_kind(dict(src, name=base))
            _check_target(self.cfg, dict(src, name=base))
            _check_removals(kind, src['params'], {}, base)
            item = self._item_from_block(src)
            item['by'] = by
            self.seq['items'].insert(idx + 1, item)
            self._place_cursor()
            self._save_queue()
            self.log('duplicated "%s" (#%d) as "%s" at #%d, right after it%s'
                     % (base, idx + 1, name, idx + 2, '   [by the agent]' if by == 'agent' else ''),
                     'info', item['uid'])
            return item

    def reorder(self, uids):
        """Apply a whole new order in one go. Unknown uids are refused; any block the caller did
        not list (one the agent added while you were dragging) keeps its place at the end."""
        with self.lock:
            items = self.seq['items']
            by_uid = {i['uid']: i for i in items}
            unknown = [u for u in uids if u not in by_uid]
            if unknown:
                raise KeyError('no such block(s): %s' % ', '.join(unknown))
            listed = [by_uid[u] for u in dict.fromkeys(uids)]
            rest = [i for i in items if i['uid'] not in set(uids)]
            self.seq['items'] = listed + rest
            self._place_cursor()
            self._save_queue()
            self.log('sequence reordered (%d block%s%s)'
                     % (len(listed), '' if len(listed) == 1 else 's',
                        ', %d left in place' % len(rest) if rest else ''))
            return self.seq['items']

    def _refuse_if_busy(self, what):
        """Running OR stopping: a stop in progress still owns the sequence (F05)."""
        if self.run_state == RUN_RUNNING:
            raise RuntimeError('sequence is running - stop it first')
        if self.run_state == RUN_STOPPING or self._stop_lock.locked():
            raise RuntimeError('a stop is still in progress - wait for it to finish before you %s'
                               % what)
        if self.current_uid is not None:
            # the worker has not written the last block's History row yet (agent A, A2)
            raise RuntimeError('the last block is still being wound down - try again in a moment')

    def clear_sequence(self, keep_done=False):
        with self.lock:
            self._refuse_if_busy('clear the sequence')
            self.run_id = None                  # a new sequence is a new batch
            self.seq.pop('loaded_from', None)   # it is no saved sequence any more (U37)
            if keep_done:
                self.seq['items'] = [i for i in self.seq['items'] if i['status'] == 'done']
            else:
                self.seq['items'] = []
            self.cursor = len(self.seq['items'])
            self.run_state = RUN_IDLE
            self.message = ''
            if getattr(self, 'fault', None):
                # a fault that still stands is still what the queue says (N33): Start is
                # refused until a SAFE STOP succeeds, and an idle badge said the opposite
                self._fault_prev = (RUN_IDLE, '')
                self.run_state = RUN_ERROR
                self.message = self.fault
            cur = (self.seq.get('name') or '').strip()
            # round 6 (C, item 3): a Clear is a new sequence, so it gets a new name (one no saved sequence has) -
            # it kept a name it had been given ("night" after a Load of "night"), and a Save then offered to replace
            # the saved one. Clear keeping the done blocks keeps the old rule: those blocks are still that run's
            if not keep_done or not cur or AUTO_NAME.match(cur):
                self.seq['name'] = self._fresh_seq_name()
            self._save_queue()

    def rename_sequence(self, name):
        with self.lock:
            self.seq['name'] = (name or '').strip() or default_seq_name()
            self._save_queue()

    def reset_all(self):
        with self.lock:
            self._refuse_if_busy('reset it')
            for it in self.seq['items']:
                # and forget what it produced: until it runs again, this sequence has no
                # results
                self._reset_item(it)
            self.run_id = None                      # running it all again is a new batch
            self.cursor = 0
            self.run_state = RUN_IDLE
            self.message = ''
            if getattr(self, 'fault', None):
                # a fault that still stands is still what the queue says (N33): Start is
                # refused until a SAFE STOP succeeds, and an idle badge said the opposite
                self._fault_prev = (RUN_IDLE, '')
                self.run_state = RUN_ERROR
                self.message = self.fault
            self._save_queue()
            self.log('sequence reset to the first block')

    def _find(self, uid):
        for it in self.seq['items']:
            if it['uid'] == uid:
                return it
        raise KeyError('unknown block %r' % uid)

    def _end_trial(self, item, what):
        """A trial of one block is over: the block goes back to pending (History keeps the
        row it just wrote), the queue back to the state and batch it had. Under the lock."""
        tr = self._trial or {}
        self._trial = None
        if item is None:
            # it never ran (deleted, or no longer pending): what was set aside goes back
            item = next((i for i in self.seq['items'] if i['uid'] == tr.get('uid')
                         and 'trial_saved' in i), None)
            if item is not None:
                self._trial_restore(item)
                item = None
        if item is not None:
            kept = self._trial_restore(item)
            item['message'] = 'trial run %s at %s - see History' % (what, time.strftime('%H:%M'))
            if kept:
                item['message'] += ('; the %d rep(s) measured before the trial are kept, and Start '
                                    'resumes it at rep %d' % (kept, kept + 1))
            item['trial'] = {'at': time.time(), 'outcome': what}
        self.run_id = tr.get('run_id')
        back = tr.get('run_state') or RUN_IDLE
        if back == RUN_RUNNING:
            back = RUN_PAUSED
        if what == 'stopped':
            what = 'stopped by %s' % self._stop_reason
        msg = 'trial of "%s": %s' % (tr.get('name'), what)
        if self.fault:
            self.run_state = RUN_ERROR
        elif self._stopping_now:
            # stop() is still running its safe stop. Restored now, idle/paused let a Start
            # in on top of it, and stop() then found no 'stopping' to
            # finish and never said the trial was stopped (blind review #005). It restores
            # this itself once the safe stop is over.
            self.run_state = RUN_STOPPING
            self._after_stop = (back, msg)
        else:
            self.run_state = back
        self.message = self.fault or msg
        self._place_cursor()
        self.log('trial of "%s" over (%s): the block is pending again, the sequence is as it was'
                 % (tr.get('name'), what), 'ok' if what == 'done' else 'warn')
        self._save_queue()

    # what a trial sets aside and puts back: the night's record of the block
    _TRIAL_KEEP = ('progress', 'datasets', 'datasets_partial', 'segment', 'exec', 'exec_last',
                   'message', 'started', 'finished', 'seg_ds_from', 'seg_dsp_from',
                   'ended_early')   # a trial ended early is the trial's, not the night's

    def _trial_aside(self, it):
        """Under the lock, as a trial starts. A trial of a half-run block used to resume at rep
        done+1, analyse the night's reps and its own as one group, then wipe the night's
        progress (blind review #011). It runs from rep 1 on a clean slate now; the night's
        progress and datasets wait in the item (so they survive a restart too) and come back
        afterwards."""
        keep = {k: copy.deepcopy(it[k]) for k in self._TRIAL_KEEP if k in it}
        keep['_run_id'] = self.run_id
        it['trial_saved'] = keep
        for k in self._TRIAL_KEEP:
            it.pop(k, None)
        it.update(progress={'done': 0, 'total': 0}, datasets=[], datasets_partial=[],
                  started=None, finished=None, message='')
        return int(_num((keep.get('progress') or {}).get('done'), 0))

    def _trial_restore(self, it):
        """Under the lock. The block back to what the night had before the trial: the trial's
        own progress and datasets go (History has them). Returns the reps kept."""
        keep = it.pop('trial_saved', None)
        self._reset_item(it)
        if not keep:
            return 0
        for k in self._TRIAL_KEEP:
            if k in keep:
                it[k] = keep[k]
        it['status'] = 'pending'
        return int(_num((it.get('progress') or {}).get('done'), 0))

    def _place_cursor(self):
        """Point the cursor at the block the run is at. Under the lock.

        The worker takes the first pending block, top to bottom, so the cursor is a
        report, not a choice: the running block if there is one, else the first pending
        one, else the end. It used to be nudged by one on add and delete and merely
        clamped on reorder and commit, so deleting the finished blocks above a paused
        run left it pointing past the block that would run next (fourth audit,
        follow-up 1).
        """
        items = self.seq['items']
        if self.current_uid:
            for i, it in enumerate(items):
                if it['uid'] == self.current_uid:
                    self.cursor = i
                    return
        self.cursor = next((i for i, it in enumerate(items) if it['status'] == 'pending'),
                           len(items))

    def _index(self, uid):
        for i, it in enumerate(self.seq['items']):
            if it['uid'] == uid:
                return i
        raise KeyError('unknown block %r' % uid)

    # ------------------------------------------------------------------ saved sequences

    def _loaded_from(self, entry):
        """Under the lock: the saved sequence the live one now is (after a Save, or a Load that
        replaced it), and a digest to tell later whether it still is (U37)."""
        self.seq['loaded_from'] = {'id': entry['id'], 'name': entry['name'],
                                   'saved': entry['saved'], 'sig': _seq_sig(self.seq['items']),
                                   'seq_name': self.seq.get('name')}

    def save_current(self, name=None, overwrite=False):
        with self.lock:
            name = (name or '').strip() or (self.seq.get('name') or '').strip() or default_seq_name()
            old = [s for s in self.saved if s.get('name') == name]
            if old and not overwrite:
                # a Save as... onto a name that is there used to replace that sequence without
                # a word (U37); the person says so now, or chooses another name
                was = old[-1]
                at = _finite(was.get('saved'))
                raise RuntimeError('a saved sequence called "%s" already exists (%s blocks, saved '
                                   '%s), so nothing was saved. Choose another name, or save '
                                   'again with overwrite=true to replace it.'
                                   % (name, was.get('n_blocks', '?'),
                                      time.strftime('%Y-%m-%d %H:%M', time.localtime(at))
                                      if at is not None else 'at an unknown time'))
            # a COPY, down to the nested dicts: the live queue goes on being edited after
            # the save, and a template that changed with it was not a template
            items = [copy.deepcopy({k: it.get(k) for k in ('uid',) + _SAVED_KEYS})
                     for it in self.seq['items']]
            entry = {'id': _uid('seq'), 'name': name, 'saved': time.time(),
                     'n_blocks': len(items), 'items': items}
            self.saved = [s for s in self.saved if s['name'] != name] + [entry]
            self._save_saved()
            if old:
                at = _finite(old[-1].get('saved'))
                self.log('saved sequence "%s" (%d blocks), replacing the one saved at %s'
                         % (name, len(items), time.strftime('%Y-%m-%d %H:%M', time.localtime(at))
                            if at is not None else 'an unknown time'), 'warn')
            else:
                self.log('saved sequence "%s" (%d blocks)' % (name, len(items)))
            # the live sequence is now that saved one - its own name is not changed: a Save as...
            # during a run must not rename the batch History is writing (U37)
            self._loaded_from(entry)
            self._save_queue()
            return entry

    def load_saved(self, sid, append=False, replace=False):
        with self.lock:
            self._refuse_if_busy('load a saved sequence')
            entry = next((s for s in self.saved if s['id'] == sid), None)
            if entry is None:
                raise KeyError('unknown saved sequence %r' % sid)
            stake = _at_stake(self.seq['items'], self.run_state)
            if not append and not replace and stake['any']:
                # a Load used to wipe done blocks, their results and a paused batch without
                # a word (U05): the person chooses - append, or replace=true
                raise RuntimeError('loading "%s" would replace a sequence that has progress (%s), '
                                   'so nothing was loaded. Append it instead (append=true), or '
                                   'load it again with replace=true to discard that - History '
                                   'keeps what ran.' % (entry['name'], _stake_text(stake)))
            if not append:
                self.run_id = None              # REPLACING the sequence starts a new batch
            new = [self._item_from_block(b, b.get('params')) for b in entry['items']]
            if append:
                # appending to a paused batch keeps it that batch, paused where it was
                # (agent A, finding A4)
                self.seq['items'].extend(new)
            else:
                self.seq['items'] = new
                self.seq['name'] = entry['name']
                self.cursor = 0
                self.run_state = RUN_IDLE
                self.message = ''
                if getattr(self, 'fault', None):
                    # a fault that still stands is still what the queue says, as clear_sequence
                    # keeps it (N33): an idle badge after a Load said the opposite
                    self._fault_prev = (RUN_IDLE, '')
                    self.run_state = RUN_ERROR
                    self.message = self.fault
                self._loaded_from(entry)
            self._save_queue()
            if not append and stake['any']:
                self.log('loaded "%s" (%d blocks), replacing a sequence with progress (%s)'
                         % (entry['name'], len(new), _stake_text(stake)), 'warn')
            else:
                self.log('loaded "%s" (%d blocks)' % (entry['name'], len(new)))
            return entry

    def delete_saved(self, sid):
        with self.lock:
            self.saved = [s for s in self.saved if s['id'] != sid]
            self._save_saved()

    # ------------------------------------------------------------------ chat with the agent

    def post_chat(self, sender, text):
        with self.lock:
            msg = {'id': _uid('msg'), 'ts': time.time(), 'sender': sender,
                   'text': str(text), 'read': sender != 'user'}
            self.chat.append(msg)
            self._save_chat()
            return msg

    def clear_chat(self):
        """Wipes what the panel shows. The Claude conversation and the measurement history are
        separate files and are not touched."""
        with self.lock:
            n = len(self.chat)
            self.chat = []
            self._save_chat()
        self.log('chat display cleared (%d message(s)); the agent still remembers the '
                 'conversation - use "new" to start it over' % n, 'info')
        return n

    def read_chat(self, unread_only=False, limit=50, mark=True):
        with self.lock:
            rows = [m for m in self.chat if (not unread_only or (m['sender'] == 'user' and not m.get('read')))]
            if mark and unread_only:
                for m in rows:
                    m['read'] = True
                self._save_chat()
            return rows[-limit:]

    def unread_count(self):
        return sum(1 for m in self.chat if m['sender'] == 'user' and not m.get('read'))

    # ------------------------------------------------------------------ what a killed panel left running

    # PowerShell rather than tasklist: tasklist cannot show a command line, and a runner is
    # just another python.exe. Same call the test harness uses to find its own processes.
    _PS_SCAN = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' or "
                "Name='pythonw.exe'\" | ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")

    def _leftover_runners(self, reuse=False):
        """(scanned, [(pid, command line)]): measurement runners of THIS panel folder that the
        engine does not own - left by a panel that was killed or crashed. They are recognised by
        the pause file (under this state folder) in their command line, so another copy of the
        panel is never mistaken for one (blind review #003). `reuse`: a scan made a moment ago."""
        cache = self._ps_cache
        rows = cache[1] if reuse and cache and time.time() - cache[0] < 3.0 else None
        if rows is None:
            rows = _ps_python_rows(self._PS_SCAN)
            if rows == [] and os.path.basename(sys.executable).lower() in ('python.exe',
                                                                           'pythonw.exe'):
                rows = None                 # a listing that works has this panel in it (#124)
            if rows is None:
                _LEFT_SCAN.last_ok = False  # a SAFE STOP cannot show it ran alone (#098 re-check)
                return False, []
            self._ps_cache = (time.time(), rows)
        _LEFT_SCAN.last_ok = True
        pause = _norm_path(self._pause_file())
        mine = {os.getpid()}
        p = self._proc
        if p is not None and getattr(p, 'pid', None):
            mine.add(p.pid)
        if self._ss_child_pid:
            mine.add(self._ss_child_pid)
        # the panel's own safe-stop child from a session before this one: known by the pid its
        # record names AND the script in its command line, so a reused pid is never taken (N31)
        ss_pids = set(self._ss_left_pids)
        try:
            ss_name = os.path.basename(self._safe_stop_path()).lower()
        except Exception:                                   # noqa: BLE001
            ss_name = ''
        out = []
        # A safe stop this panel did not start (#098 re-check): a runner's helper of THIS state
        # folder ('run_block.py --orphan-retry <state>/orphan_safe_stop/...'), and a run of the
        # safe-stop script - the configured one, or the one an orphan record names - that is not
        # this panel's own. Neither a leftover nor ever ended: Start and a trial wait for it,
        # SAFE STOP waits for it first (panel_server._guard_rows knows them the same way). The
        # record alone could not be relied on: written late, not at all, or unreadable, it held
        # nothing.
        odir = _norm_path(self._orphan_dir()) + '\\'
        # every spelling of the script: its full path, its name alone (run by hand from its own
        # folder) and the 8.3 short forms - the full path alone missed both (#098 re-check) -
        # for the configured script and for the one every record read names, tracked or not
        with self.lock:
            paths = [i.get('safe_stop') for i in list(self._orphan_info.values())]
            paths += list(self._orphan_scripts)
        try:
            paths.append(self._safe_stop_path())
        except Exception:                                   # noqa: BLE001
            pass
        scripts = set()
        for sp in paths:
            scripts |= _ss_spellings(sp)
        flying, hidden = [], []
        for pid, cmd in rows:
            c = _norm_path(cmd)
            if pid in mine:
                continue
            if not c.strip():
                # its command line cannot be read (another user's, or one run as administrator
                # while this panel is not): see _check_leftovers (#098 re-check)
                hidden.append(pid)
                continue
            if (('run_block.py' in c and pause in c)
                    or (pid in ss_pids and ss_name and ss_name in c)):
                out.append((pid, cmd))
            elif 'run_block.py' in c:
                # a runner names the safe-stop script as its --safe-stop, and runs none now
                if '--orphan-retry' in c and odir in c:
                    flying.append((pid, cmd))
            elif any(_names_script(c, s) for s in scripts):
                flying.append((pid, cmd))
        self._ps_hidden = hidden
        self._ss_foreign_found(flying)
        return True, out

    def _is_old_safe_stop(self, pid, cmd):
        try:
            name = os.path.basename(self._safe_stop_path()).lower()
        except Exception:                                   # noqa: BLE001
            return False
        return pid in self._ss_left_pids and bool(name) and name in _norm_path(cmd)

    def _provably_mine(self, pid, cmd):
        """A leftover this panel can prove is its own, and so may end: a runner by the pause
        file of THIS state folder, or a safe stop by a pid this panel wrote down (queue.json,
        safe_stop_child.json)."""
        c = _norm_path(cmd)
        pause = _norm_path(self._pause_file())
        return (('run_block.py' in c and pause in c)
                or self._is_old_safe_stop(pid, cmd))

    def _left_origin(self):
        """Where a leftover comes from, in the operator's words (R10: not every one found is
        from before a restart)."""
        return ('from before the panel restart' if self._restarted
                else 'that this panel no longer controls')

    def _check_leftovers(self):
        """Outside the lock (it may shell out). Returns the refusal text, or ''.
        In simulation only while the leftover check is on; in real mode every time. The check is
        on after a restart that cut something off, and from any scan that finds something until
        one finds nothing (R08) - queue.json keeps it across a restart (R07).

        While it is on, a scan that fails refuses (N32): a runner from before cannot be ruled
        out. What a scan finds is a FAULT (N30): ended by hand or on its own, it was killed or
        ended without a safe stop after it, so Start waits for a SAFE STOP that succeeds -
        which ends first whatever of it is provably this panel's (R10)."""
        if self.cfg.get('sim') and not self._restart_check:
            return ''
        if self._relist_due():
            # a runner's record written after this panel started is read now (#098 re-check)
            self._orphan_relist()
        # A safe stop it finds that this panel did not start is kept apart: the caller's
        # _ss_busy() refuses beside it (#098 re-check)
        scanned, left = self._leftover_runners()
        origin = self._left_origin()
        if not scanned:
            if self._restart_check:
                self.log('could not list the running processes, so a runner %s cannot be ruled '
                         'out - refused until the list can be read' % origin, 'warn')
                return ('the list of running processes could not be read (PowerShell '
                        'Get-CimInstance failed), so a runner or safe stop %s cannot be ruled '
                        'out. Try again in a moment. SAFE STOP is still allowed.' % origin)
            return ''
        chk_note = ''
        hidden = list(self.__dict__.get('_ps_hidden') or [])
        left_s = self._HIDDEN_WINDOW_S - (time.monotonic() - self._t_start)
        if not left and hidden and self._restart_check and left_s > 0:
            # A python.exe whose command line cannot be read, in the first moments after a
            # restart: the old panel's runner (run as administrator while this panel is not)
            # shows so, and its record may not be written yet. It cannot be ruled out, so
            # nothing starts beside it until the record has had its time; the check stays on
            # (#098 re-check: it read as "nothing left", and the check went off with the
            # records' re-reads)
            with self.lock:
                new = [p for p in hidden if p not in self._ps_hidden_said]
                self._ps_hidden_said.update(new)
            if new:
                self.log('python.exe pid %s is running, and its command line cannot be read (a '
                         'process of another user, or one run as administrator while this panel '
                         'is not): for %d s after the restart it is taken as a runner or safe '
                         'stop %s that may not have written its record yet'
                         % (', '.join(str(p) for p in new), self._HIDDEN_WINDOW_S, origin), 'warn')
            return ('python.exe pid %s is running, and its command line cannot be read (a process '
                    'of another user, or one run as administrator while this panel is not), so it '
                    'cannot be ruled out that it is a runner or safe stop %s '
                    'whose record has not been written yet. Try again in %d s. SAFE STOP is still '
                    'allowed.' % (', '.join(str(p) for p in hidden[:5]), origin, int(left_s) + 1)
                    + (' ' + chk_note if chk_note else ''))
        if not left:
            with self.lock:
                # a safe stop in flight that this panel did not start keeps it looking, and so
                # does a fault that stands or a runner's record not answered yet: a scan that
                # could not see the old runner turned the check off, and with it the re-reads of
                # the orphan folder (#098 re-check)
                keep = bool(self._ss_foreign_live()) or bool(self.fault) or bool(self._orphan_open)
            self._leftovers_gone(check_off=not keep)
            return chk_note
        what = '; '.join('pid %d: %s' % (p, c[-160:]) for p, c in left[:3])
        mine = [(p, c) for p, c in left if self._provably_mine(p, c)]
        other = [(p, c) for p, c in left if not self._provably_mine(p, c)]
        with self.lock:
            # found: SAFE STOP now ends what of it is this panel's, and the scan keeps looking
            # until it sees it gone - a failed scan refuses from here on too (R08, N32)
            changed = not self._restart_check
            self._restart_check = True
            if not self.fault:
                changed = True
                # the fault is what a later Start shows: it says what SAFE STOP cannot do (R10)
                self._set_fault('%d process(es) %s are still running and hold the instruments '
                                '(%s)%s' % (len(left), origin, what, '' if not other else
                                            '; SAFE STOP does not end pid %s (no record of this '
                                            'panel names it) - end it in Task Manager first'
                                            % ', '.join(str(p) for p, _c in other)))
        if changed:
            self._save_queue()
        text = ('%d process(es) %s are still running and hold the instruments: %s.'
                % (len(left), origin, what))
        if any('run_block.py' in _norm_path(c) for p, c in mine):
            text += ' A runner runs its own safe stop as it shuts down.'
        if mine:
            text += (' Press SAFE STOP: it ends %s (pid %s) first - a safe stop of the panel\'s '
                     'own is given time to finish - then runs the safe stop; Start is allowed '
                     'again once that has succeeded.'
                     % ('it' if len(mine) == 1 else 'them', ', '.join(str(p) for p, _c in mine)))
        if other:
            text += (' pid %s: %s a script of this panel folder, but no record of '
                     'this panel names %s, so SAFE STOP does not end %s. End %s in Task Manager '
                     '(or wait for %s to end), then press SAFE STOP; Start is allowed again once '
                     'that has succeeded.'
                     % (', '.join(str(p) for p, _c in other),
                        'runs' if len(other) == 1 else 'run',
                        'it' if len(other) == 1 else 'them', 'it' if len(other) == 1 else 'them',
                        'it' if len(other) == 1 else 'them', 'it' if len(other) == 1 else 'them'))
        return text + (' ' + chk_note if chk_note else '')

    def _end_leftovers(self):
        """In run_safe_stop, before the script, while the leftover check is on (N30, N31, R10):
        what this panel left running - a runner, its own safe stop - is ended first, so the
        script never runs beside it. A safe stop of its own is given its
        timeout to finish on its own. Returns '' or why the processes could not be ended (the
        safe stop still runs; the caller keeps the fault)."""
        if not self._restart_check:
            return ''
        scanned, left = self._leftover_runners()
        origin = self._left_origin()
        if not scanned:
            self.log('safe stop: could not list the running processes, so a runner %s cannot be '
                     'ruled out; the safe stop runs anyway, but it cannot show '
                     'that it ran alone, so it clears no fault (Start stays refused until the list '
                     'can be read)' % origin, 'warn')
            return ''
        if not left:
            self._leftovers_gone()
            return ''
        # a runner whose record says it is running its own safe stop is never ended here: it is
        # a safe stop in flight, waited for - ending its tree cut that safe stop off half way
        # (#098 re-check)
        own = self._orphan_running_own()
        old_ss = [(p, c) for p, c in left if self._is_old_safe_stop(p, c)]
        if old_ss:
            # (a pid of safe_stop_child.json: always from before a restart)
            self.log('safe stop: the panel\'s own safe stop from before the restart is still running '
                     '(pid %s) - waiting for it to finish first' % ', '.join(str(p) for p, _c in old_ss),
                     'warn')
            t0 = time.monotonic()
            while (any(_pid_alive(p) for p, _c in old_ss)
                   and time.monotonic() - t0 < self._ss_timeout()):
                time.sleep(0.2)
        # Only what is provably this panel's is ended: a runner by the pause file of THIS state
        # folder, or a safe stop by the pid this panel wrote down. A process of this folder that
        # no record names is left alone, and said (the fault stays).
        ended = []
        for pid, cmd in left:
            provable = self._provably_mine(pid, cmd)
            if not _pid_alive(pid):
                continue
            if pid in own:
                self.log('safe stop: pid %d is running its own safe stop (%s) - not ended: a safe '
                         'stop in flight is waited for, never cut off' % (pid, own[pid]), 'warn')
                continue
            if not provable:
                self.log('safe stop: pid %d looks like this panel\'s but no record names it - not '
                         'ended here; end it in Task Manager if it is: %s' % (pid, cmd[-160:]),
                         'error')
                continue
            ended.append(pid)
            # a record it wrote just before, read later, is answered by this SAFE STOP if it
            # then runs alone and succeeds (run_safe_stop)
            getattr(_LEFT_SCAN, 'ended', []).append(pid)
            self.log('safe stop: ending pid %d (%s): %s'
                     % (pid, origin, cmd[-160:]), 'warn')
            try:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/T', '/F', '/PID', str(pid)], capture_output=True,
                                   timeout=15, stdin=subprocess.DEVNULL)
                else:
                    os.kill(pid, signal.SIGKILL)
            except (OSError, subprocess.SubprocessError) as e:
                self.log('safe stop: could not end pid %d: %s' % (pid, e), 'error')
        # what it ended is given a moment to be gone - not what it left alone, which used to
        # hold the script back 5 s for nothing
        t0 = time.monotonic()
        while any(_pid_alive(p) for p in ended) and time.monotonic() - t0 < 5.0:
            time.sleep(0.1)
        scanned, still = self._leftover_runners()
        # (one running its own safe stop is waited for by the caller, not "not ended")
        still = [(p, c) for p, c in still if p not in own]
        if scanned and not still:
            self._leftovers_gone()
            return ''
        if not scanned:
            return ''
        unproven = [p for p, c in still if not self._provably_mine(p, c)]
        return ('%d process(es) %s could not be ended (%s)%s'
                % (len(still), origin, ', '.join('pid %d' % p for p, _c in still[:5]),
                   '' if not unproven else
                   '; pid %s: no record of this panel names %s, so the safe stop does not end %s '
                   '- end %s in Task Manager, then press SAFE STOP again'
                   % (', '.join(str(p) for p in unproven[:5]),
                      'it' if len(unproven) == 1 else 'them', 'it' if len(unproven) == 1 else 'them',
                      'it' if len(unproven) == 1 else 'them')))

    # ------------------------------------------------------------------ run control

    def start(self, only_uid=None, failed=None, failed_uids=None):
        """Start (or resume) the sequence - or, with only_uid, try ONE pending block on its own.

        A trial runs the block exactly as the queue would: same runner, a
        History row of its own (run id 'trial_…', run name 'trial: <block>'). When it is over
        the block is pending again and the queue is as it was - idle or paused, the same
        batch - so the night still runs it. Everything Start refuses, a trial refuses too.

        A refused Start goes to the log with its reason, not only back to the caller: the
        morning review reads the log, and a toast is gone by then (usability review).

        `failed` ('retry' / 'skip') is the operator's choice for the failed blocks the run
        would pass over (U03): without one, a Start with a failed block is refused, naming
        it. `failed_uids`: the failed blocks the operator's dialog showed; a choice made for
        another set of failures than there is now is refused, not applied to it.
        """
        try:
            return self._start(only_uid, failed, failed_uids)
        except RuntimeError as e:
            self.log('%s refused: %s' % ('trial run' if only_uid is not None else 'Start', e),
                     'warn')
            raise

    def _start_refusals(self):
        """What refuses a Start, checked before AND after the unlocked gap. Under the lock."""
        if self.fault:
            raise RuntimeError('not starting: %s. Run SAFE STOP; once it succeeds, '
                               'Start is allowed again.' % self.fault)
        if self.run_state == RUN_STOPPING or self._stop_lock.locked():
            raise RuntimeError('a stop is still in progress (the safe stop is running). '
                               'Wait for it to finish, then press Start.')
        self._refuse_during_safe_stop('press Start')
        stuck = self._orphan_stuck_why()   # (#098 re-check)
        if stuck:
            raise RuntimeError('not starting: ' + stuck)

    def _preflight_refusal(self, only_uid=None, failed=None):
        """Why Start is refused by the pre-flight (preview_overrides), or '': every pending
        block with a problem - for a trial, only the block tried. Outside the lock. A check
        that could not be made refuses too, as the page does ("nothing was started").
        `failed`: the sequence is checked as that choice leaves it (U03)."""
        try:
            pv = self.preview_overrides() if failed is None else self.preview_overrides(failed=failed)
        except Exception as exc:                                        # noqa: BLE001
            return ('not starting: the check before Start could not be made (%s: %s), so '
                    'nothing was started' % (type(exc).__name__, exc))
        def probs(e):
            return list(e.get('problems') or [])
        bad = [e for e in pv if probs(e)
               and (only_uid is None or e.get('uid') == only_uid)]
        if not bad:
            return ''
        parts = []
        for e in bad[:4]:
            pr = probs(e)
            parts.append('#%s "%s": %s%s' % (e.get('n'), e.get('name'), str(pr[0]).rstrip('. '),
                                             '' if len(pr) == 1 else ' (and %d more)' % (len(pr) - 1)))
        return ('not starting: the check before Start found %d block(s) that cannot run as they '
                'are (the same list the Start dialog shows) - %s%s. Fix %s, or remove %s, and '
                'press Start again.'
                % (len(bad), ' | '.join(parts), ' | and %d more block(s)' % (len(bad) - 4)
                   if len(bad) > 4 else '', 'it' if len(bad) == 1 else 'them',
                   'that block' if len(bad) == 1 else 'those blocks'))

    def _start(self, only_uid=None, failed=None, failed_uids=None):
        # the snapshot after the lock is let go: nothing slow is ever called under the
        # engine lock (blind review #012)
        try:
            self._start_run(only_uid, failed, failed_uids)
        finally:
            self._flush_decisions()         # a Skip's History rows, written outside the lock
        return self.snapshot()

    def _start_run(self, only_uid=None, failed=None, failed_uids=None):
        # a bad choice is a bad request (400), checked before anything else
        failed = _failed_choice(failed)
        failed_uids = _failed_uid_list(failed_uids)
        if only_uid is not None and (failed or failed_uids is not None):
            raise ValueError('a trial run of one block takes no failed choice')
        with self.lock:
            if self.run_state == RUN_RUNNING:
                if only_uid is not None:
                    raise RuntimeError('a sequence is running - a block cannot be tried on its '
                                       'own until it has stopped or paused')
                if failed is not None or failed_uids is not None:
                    # a Retry / Skip is refused, not dropped: pressed in one tab while another
                    # (or a trial) had started the run, it came back "ok" and nothing was done
                    # (U03-3)
                    raise RuntimeError(self._running_choice_refusal(failed))
                return
            self._start_refusals()
            if only_uid is None and failed is None and _failed_items(self.seq['items']):
                # after the FAULT check: a standing fault still says SAFE STOP first
                raise RuntimeError(_failed_refusal(self.seq['items']))
            old = self._worker_thread
            gen = self._stop_gen
        if old is not None and old.is_alive():
            # the previous run is still unwinding (writing its history row); a second
            # worker beside it would be two measurements at once. Wait a moment, then refuse.
            old.join(5.0)
            if old.is_alive():
                raise RuntimeError('the previous run has not finished stopping yet - try again '
                                   'in a moment')
        # The pre-flight the page's Start dialog shows, made here too: a Start posted straight
        # to the API (a script, the agent with may_start, a second tab) used to skip it, and a
        # block the dialog would have held back ran (round 2 N29, N51). The same checks and the
        # same words; outside the lock, like the scan below (it reads every script, and starts
        # the import check's child).
        why = (self._preflight_refusal(only_uid) if failed is None
               else self._preflight_refusal(only_uid, failed=failed))
        if why:
            raise RuntimeError(why)
        # the process scan shells out to PowerShell (up to 20 s); under the lock it stalled
        # every status poll and every stop meanwhile (agent C, C4)
        left = self._check_leftovers()
        with self.lock:
            if self.run_state == RUN_RUNNING:
                if failed is not None or failed_uids is not None:
                    raise RuntimeError(self._running_choice_refusal(failed))
                return
            # Everything the first check looked at, looked at again. The lock was down for the
            # join and the scans, and a Stop, SAFE STOP or fault that landed there used to be
            # undone here: the abort was cleared and the queue ran after the operator's STOP
            # (blind review #000).
            self._start_refusals()
            if gen != self._stop_gen:
                raise RuntimeError('a Stop or SAFE STOP arrived while Start was being prepared, '
                                   'so the sequence was not started. Press Start again if you '
                                   'still want it to run.')
            if not self.seq['items']:
                raise RuntimeError('the sequence is empty')
            if left:
                # a runner that outlived a killed panel is still measuring, or about to run
                # its own safe stop in the middle of whatever starts now (blind review #003)
                raise RuntimeError('not starting: ' + left)
            errs = _failed_items(self.seq['items'])
            if only_uid is None and failed is None and errs:
                raise RuntimeError(_failed_refusal(self.seq['items']))
            if only_uid is None and failed is not None:
                # the choice was made for the failures the operator saw: one made for another
                # set of them is refused, not applied - a Skip that became a plain Resume
                # measured the block the person had chosen to pass over (U03)
                self._failed_choice_stale(errs, failed_uids)
            if only_uid is None and not errs \
                    and all(i['status'] in ('done', 'skipped') for i in self.seq['items']):
                raise RuntimeError('every block already ran - press Reset to run the sequence again')
            if only_uid is None and errs and failed:
                if not self.run_id:
                    self.run_id = _uid('run')       # the decision rows belong to this batch
                for n, it in errs:
                    self._apply_failed_choice(n, it, failed)
                self._place_cursor()
                if not any(i['status'] == 'pending' for i in self.seq['items']):
                    # only failed blocks were left, and they are skipped now: nothing to run
                    self._finish_sequence()
                    self._save_queue()
                    return
            self._trial = None                  # a marker left by a crashed trial must not leak
            if only_uid is not None:
                one = self._find(only_uid)
                if one['status'] != 'pending':
                    raise RuntimeError('only a pending block can be tried on its own - press '
                                       '\u21ba on it first')
                self._trial = {'uid': one['uid'], 'name': one['name'], 'run_state': self.run_state,
                               'run_id': self.run_id, 'message': self.message}
                self._trial['kept'] = self._trial_aside(one)
            self._abort.clear()
            self._skip.clear()
            self._end_wait.clear()
            self._wait_skip.clear()
            self._pause_after_block = False
            self._pause_after_rep = False
            self._next_after_rep = None         # round 6b L: never inherited from the last run
            self._after_stop = None
            # The runner reads this file after every rep and stops when it is there. The
            # worker deletes it on the way out, but a panel that was killed while paused
            # (the console window closed, a power cut) leaves it behind - and then every
            # block of the next run quietly stopped after one rep, looking like it had
            # finished. Nothing may be pending here except what was just asked for.
            self._touch_pause_file(False)
            self.run_state = RUN_RUNNING
            self.message = ''
            if self._trial:
                self.cursor = self._index(only_uid)
                self.run_id = _uid('trial')     # its own batch in History; the night's is kept
                self.message = 'trial of "%s"' % self._trial['name']
            else:
                self.cursor = next((i for i, it in enumerate(self.seq['items'])
                                    if it['status'] == 'pending'), len(self.seq['items']))
                if not self.run_id:             # a resume keeps the same batch
                    self.run_id = _uid('run')
            self._worker_thread = threading.Thread(target=self._worker, name='panel-worker', daemon=True)
            self._worker_thread.start()
            if self._trial:
                self.log('trial run of block #%d "%s" on its own%s - the rest of the sequence '
                         'waits, and the block is pending again afterwards%s'
                         % (self.cursor + 1, self._trial['name'],
                            '  [SIM]' if self.cfg.get('sim') else '',
                            '. It runs from rep 1; the %d rep(s) the night has measured are set '
                            'aside, not pooled with it, and come back after the trial'
                            % self._trial['kept'] if self._trial.get('kept') else ''), 'head')
            else:
                self.log('sequence "%s" started at block #%d%s'
                         % (self.seq['name'], self.cursor + 1, '  [SIM]' if self.cfg.get('sim') else ''))
            return

    def _running_choice_refusal(self, failed):
        """Under the lock: the refusal of a Retry / Skip that arrives while the queue (or a
        trial) runs - it was made for a queue that was not running, and is not applied (U03-3)."""
        what = {'retry': 'Retry', 'skip': 'Skip'}.get(failed, 'Retry / Skip choice')
        if self._trial:
            now = ('a trial of "%s" is running - look at the failed block(s) again once it has '
                   'finished' % self._trial.get('name'))
        else:
            now = ('the sequence is already running ("%s") - someone started it meanwhile'
                   % (self.seq.get('name') or ''))
        return 'nothing was changed: %s; the %s was not applied' % (now, what)

    def _failed_choice_stale(self, errs, failed_uids):
        """Under the lock: a failed choice made for another set of failures than there is now
        (another tab, the agent's reset, a Retry elsewhere) is refused (U03)."""
        if not errs:
            if failed_uids:
                what = '; '.join('"%s" is %s now' % (i.get('name'), i.get('status'))
                                 for i in self.seq['items'] if i.get('uid') in failed_uids) \
                    or 'the blocks it was for are not in the sequence any more'
            else:
                what = 'no block is marked failed now'
            raise RuntimeError('nothing was started: no block has failed any more (%s) - press '
                               'Start again' % what)
        if failed_uids is not None and set(failed_uids) != {it['uid'] for _n, it in errs}:
            raise RuntimeError('the failed blocks are not the ones the choice was made for: %s %s '
                               'failed now. Nothing was started - look at %s and choose again.'
                               % (_failed_names(errs), 'has' if len(errs) == 1 else 'have',
                                  'it' if len(errs) == 1 else 'them'))

    def _apply_failed_choice(self, n, it, failed):
        """Under the lock: one failed block retried or skipped, as the operator chose (U03). Its
        failed run keeps its History row, error and all."""
        was = it.get('message') or 'no message'
        if failed == 'retry':
            # the segment count goes on: the retry is the next segment of the same batch, so
            # the failed run's kept patched copy (<run_id>-seg<n>) and its History row are not
            # overwritten by the retry's (blind review #111)
            seg = it.get('segment')
            self._reset_item(it)
            if seg:
                it['segment'] = seg
            self.log('"%s" set back to pending for a retry (it had failed: %s) - it is measured '
                     'again from rep 1, in the same batch' % (it['name'], _short(was, 160)),
                     'warn', it['uid'])
            return
        it['status'] = 'skipped'
        it['message'] = 'skipped by you after it failed (%s)' % _short(was, 160)
        # the row only: history.json is written by _start once the lock is let go (#012)
        self._record_decision(it, n, 'skipped by the operator', it['message'], write=False)
        # 'the next block' only when there is one: a failure at the end left the log saying the
        # run went on when it ended there
        after = any(isinstance(i, dict) and i.get('status') == 'pending'
                    for i in self.seq['items'][n:])
        self.log('"%s" skipped by you after it failed - it is not run again; %s'
                 % (it['name'], 'the run goes on with the next block'
                    if after else 'nothing is left to run after it'),
                 'warn', it['uid'])

    def _record_decision(self, item, pos, outcome, message, write=True):
        """A History row for a decision, not a run (U03): the operator skipped a failed block.
        The _record_history schema, with decision: true, no datasets (the failed run's row
        lists those - a second listing would count them twice) and no duration. Never raises.
        `write` False: the row only, under the caller's lock; history.json is written later,
        outside it (_flush_decisions), as no file is ever written under the engine lock."""
        try:
            now = time.time()
            with self.lock:
                row = {'id': _uid('h'), 'run_id': self.run_id or 'run_?',
                       'run_name': self.seq.get('name') or '', 'trial': False, 'n': pos,
                       'uid': item.get('uid'), 'segment': int(_as_number(item.get('segment')) or 1),
                       'name': item.get('name'), 'kind': item.get('kind'),
                       'target': item.get('target') or '', 'patched_copy': '', 'params': {},
                       'overrides': {}, 'status': 'skipped', 'outcome': outcome,
                       'message': message, 'started': now, 'finished': now, 'datasets': [],
                       'datasets_before': len(item.get('datasets') or []),
                       'datasets_partial': [], 'by': 'you',
                       'sim': bool(self.cfg.get('sim')), 'decision': True}
                self._history_add(row)             # the rows it pushes out are archived (#090)
                if not write:
                    self._decisions_unwritten = True
        except (TypeError, ValueError) as e:              # history must never break a run
            self.log('could not record history: %s' % e, 'warn')
            return
        if write:
            self._write_history()                          # never raises (#007)

    def _flush_decisions(self):
        """history.json after decision rows recorded under the lock (write=False): written once,
        here, outside the lock - a Skip of k blocks used to write it k times with every status
        poll, Stop and SAFE STOP waiting behind it (#012, #007)."""
        with self.lock:
            due = bool(self.__dict__.get('_decisions_unwritten'))
            self._decisions_unwritten = False
        if due:
            self._write_history()                          # never raises (#007)

    def _refinish_if_no_failures(self, why):
        """Under the lock. A queue that finished with failed blocks keeps its batch, so a Retry
        or Skip belongs to it (U03). Once the last failed block has gone - deleted, or set back
        to pending with ↺ - there is no choice left to make: the batch is closed as a clean
        finish closes it, so blocks added later start a batch of their own (#117), and Home no
        longer says 'k failed'. With failures still left, the count follows."""
        if self.run_state != RUN_ERROR or getattr(self, 'fault', None) \
                or not str(self.message or '').startswith('sequence finished'):
            return
        items = self.seq['items']
        errs = _failed_items(items)
        ns = sum(1 for i in items if isinstance(i, dict) and i.get('status') == 'skipped')
        if errs:
            self.message = 'sequence finished: %d failed / %d skipped' % (len(errs), ns)
            return
        self.run_state = RUN_IDLE
        self.message = ('sequence finished: 0 failed / %d skipped' % ns if ns
                        else 'sequence finished')
        self.run_id = None
        self.log('no failed block is left (%s): the batch is closed - Start begins a new one' % why)

    def _finish_sequence(self, note=''):
        """Under the lock: nothing is left to run. With failed blocks the queue says so and
        keeps its batch - a Retry or Skip still belongs to it (U03); 'sequence finished' used
        to replace the failure on Home and close the batch. Without, idle and closed (#117)."""
        items = self.seq['items']
        errs = _failed_items(items)
        ns = sum(1 for i in items if i.get('status') == 'skipped')
        tail = ' (%s)' % note if note else ''
        self.cursor = len(items)
        if errs:
            self.run_state = RUN_ERROR
            self.message = 'sequence finished: %d failed / %d skipped%s' % (len(errs), ns, tail)
            self.log('sequence "%s" finished: %d failed / %d skipped - %s failed; Start asks to '
                     'retry or skip %s%s' % (self.seq['name'], len(errs), ns, _failed_names(errs),
                                             'it' if len(errs) == 1 else 'them',
                                             ' (%s)' % note if note else ''), 'warn')
            return
        self.run_state = RUN_IDLE
        self.message = ('sequence finished: 0 failed / %d skipped' % ns if ns
                        else 'sequence finished') + tail
        self.run_id = None                  # the batch is closed
        self.log('sequence "%s" finished%s' % (self.seq['name'], ' - ' + note if note else ''), 'ok')

    def pause(self, after='block', want=None):
        """Arm a pause after the current rep or block - or, pressed again, take it back.

        A pause that is only a flag is invisible: for a four-hour block the person cannot
        tell whether the press registered, and there used to be no way to change their
        mind. The armed flags travel in the snapshot now, and a second press disarms.

        `want` (True = arm, False = disarm) is what the button showed when it was pressed. A
        press made from a display one poll old must not take back the pause it meant to arm
        (blind review #095): when the flag already is what was wanted, nothing changes.
        None keeps the toggle.
        """
        self._pause(after, want)
        return self.snapshot()             # outside the lock (#012)

    def _pause(self, after, want):
        with self.lock:
            if self.run_state != RUN_RUNNING:
                raise RuntimeError('nothing is running')
            if want is not None:
                armed = self._pause_after_rep if after == 'rep' else self._pause_after_block
                if bool(want) == bool(armed):
                    return
            if after == 'rep':
                if self._pause_after_rep:                    # pressed again: carry on
                    self._pause_after_rep = False
                    self._touch_pause_file(False)
                    self.log('pause after the rep taken back: the run carries on', 'warn')
                    return
                self._pause_after_rep = True
                self._touch_pause_file(True)
                self.log('pause requested: the runner stops after the current rep', 'warn')
                self._next_yields('Pause after the rep', 'the run pauses after this rep instead (Resume measures '
                                  'the reps left)')
            else:
                if self._pause_after_block:
                    self._pause_after_block = False
                    self.log('pause after the block taken back: the run carries on', 'warn')
                    return
                self._pause_after_block = True
                self.log('pause requested: stops after the current block', 'warn')
                self._next_yields('Pause after the block', 'the block runs all its reps, then the run pauses')
            self._end_wait.set()
            return

    def _next_yields(self, what, then):
        """Under the lock: a pause armed while "Next block after this rep" is armed - the last press wins (round 6b L,
        2026-10-05, batch 7.1), and the log says which. The pause file stays only for a pause after the rep."""
        nx, self._next_after_rep = self._next_after_rep, None
        if not nx:
            return
        if not self._pause_after_rep:
            self._touch_pause_file(False)
        self.log('"Next block after this rep" on "%s" is taken back: %s was pressed after it, and the last press '
                 'wins - %s' % (nx.get('name'), what, then), 'warn', nx.get('uid'))

    def next_after_rep(self, uid=None, want=None, by='you'):
        """"Next block after this rep" for the running multi-rep script block - or, pressed again,
        take it back (as pause). The runner stops cleanly after the current rep (the pause file, as "After rep"); the
        block is then COMPLETE with the reps done - status done - and the queue goes on to the next block WITHOUT
        pausing. `reps` stays the PLAN: the early end is recorded as item['ended_early'] = {reps_done, of, by, at}
        (the card, History's row).

        `uid`: the block the operator saw (contract C2, as skip) - one that ended meanwhile is refused, nothing
        changed. `want` (True = arm, False = take back): what the button showed (#095), None toggles. `by`: 'you' or
        'agent'. With a pause armed, the last press wins (_next_yields; arming this takes back either pause), and
        the log says which. Refused when nothing runs, between blocks and for a block that is not a script (a wait
        has no reps: Skip ends it now). On the block's LAST rep there is nothing to cut: nothing is armed, the block
        finishes as planned, and the answer's note says so.

        Returns {'armed': bool, 'note': str}; RuntimeError (409) for a refusal."""
        by = 'agent' if by == 'agent' else 'you'
        with self.lock:
            if self.run_state != RUN_RUNNING:
                raise RuntimeError('nothing is running')
            cur = self.current_uid
            now = next((i for i in self.seq['items'] if i['uid'] == cur), None)
            if cur is None or now is None:
                raise RuntimeError('no block is running at this moment (the last one has just finished) - nothing '
                                   'was changed')
            if uid is not None and uid != cur:
                raise RuntimeError('that block is not running any more - it finished before this arrived, and "%s" '
                                   'is running now. Nothing was changed.' % now['name'])
            nx = self._next_after_rep
            armed = bool(nx) and nx.get('uid') == cur
            if want is not None and bool(want) == armed:
                return {'armed': armed, 'note': ''}
            if armed:
                self._next_after_rep = None
                if not self._pause_after_rep:
                    self._touch_pause_file(False)
                self.log('"Next block after this rep" taken back: "%s" runs all its %d rep(s)%s'
                         % (now['name'], int(_num(nx.get('of'), 0) or 0) or _reps_hint(now),
                            '   [by the agent]' if by == 'agent' else ''), 'warn', cur)
                return {'armed': False, 'note': ''}
            kind = now.get('kind') or 'script'
            if kind != 'script':
                raise RuntimeError('"Next block after this rep" is for a script block with reps - "%s" is a %s block, '
                                   'which has no reps. Skip ends it now; Pause waits for its end.'
                                   % (now['name'], _NEXT_KIND_WORDS.get(kind, kind)))
            pr = now.get('progress') if isinstance(now.get('progress'), dict) else {}
            total = int(_num(pr.get('total'), 0) or 0) or _reps_hint(now)
            done = int(_num(pr.get('done'), 0) or 0)
            on = int(_num(pr.get('running'), 0) or 0) or min(done + 1, total)
            if isinstance(now.get('ended_early'), dict):
                # its runner has stopped already (ended early)
                note = ('"%s" has ended already (after rep %s of %s): there is nothing to cut - the queue goes on'
                        % (now['name'], now['ended_early'].get('reps_done'), now['ended_early'].get('of')))
                self.log('"Next block after this rep": ' + note, 'info', cur)
                return {'armed': False, 'note': note}
            if done >= total and not int(_num(pr.get('running'), 0) or 0):
                # every rep is measured and only the block's end is left - "on its last rep" would be
                # wrong here: say what is true
                note = ('"%s" has measured all its reps (%d of %d): there is nothing to cut - the queue goes on to '
                        'the next block as planned' % (now['name'], done, total))
                self.log('"Next block after this rep": ' + note, 'info', cur)
                return {'armed': False, 'note': note}
            if on >= total:
                note = ('"%s" is on its last rep (%d of %d): there is nothing to cut - it finishes, and the queue goes '
                        'on to the next block as planned' % (now['name'], on, total))
                self.log('"Next block after this rep": ' + note, 'info', cur)
                return {'armed': False, 'note': note}
            yielded = [w for w, f in (('Pause after the rep', self._pause_after_rep),
                                      ('Pause after the block', self._pause_after_block)) if f]
            self._pause_after_rep = False
            self._pause_after_block = False
            self._next_after_rep = {'uid': cur, 'name': now['name'], 'by': by, 'at': time.time(), 'rep': on,
                                    'of': total}
            self._touch_pause_file(True)
            if self._trial:
                then = 'the trial ends there'
            else:
                nxt = next((i for i in self.seq['items'] if i.get('status') == 'pending' and i['uid'] != cur), None)
                then = ('the queue goes on to "%s" without pausing' % nxt['name'] if nxt
                        else 'it is the last block: the sequence finishes')
            self.log('"Next block after this rep"%s: "%s" stops after rep %d of %d and is then complete with the reps '
                     'done; %s%s'
                     % ('   [by the agent]' if by == 'agent' else '', now['name'], on, total, then,
                        ('. It replaces %s (the last press wins)' % ' and '.join(yielded)) if yielded else ''),
                     'warn', cur)
            return {'armed': True, 'note': ''}

    def _wait_gone(self, uid, what):
        """Under the lock: the refusal when the wait the operator saw (uid) is not the one
        running (contract C2, as skip_block): it ended and another wait began meanwhile."""
        if uid is not None and uid != self._in_wait:
            now = next((i for i in self.seq['items'] if i['uid'] == self._in_wait), None)
            raise RuntimeError('that wait is not running any more - "%s" is running now. Nothing '
                               'was %s.' % (now['name'] if now else 'nothing', what))

    def resume_wait_now(self, uid=None):
        """End the running wait block now - the person's explicit decision, and the ONLY
        thing that may end a wait before its condition is met. Pause and the rest merely
        wake the wait so it re-reads its flags (F01). `uid`: the wait the operator saw."""
        with self.lock:
            if self._in_wait is None:
                raise RuntimeError('no wait block is running')
            self._wait_gone(uid, 'ended')
            self._wait_skip.set()
            self._end_wait.set()
            self.log('wait ended early by the user', 'warn')

    def set_wait_minutes(self, minutes, uid=None):
        """Change the remaining time of the wait block that is running: the minutes left from
        now, above 0 (end_wait ends it). `uid`: the wait the operator saw."""
        with self.lock:
            if self._in_wait is None:
                raise RuntimeError('no wait block is running')
            self._wait_gone(uid, 'changed')
            if self.wait_end is None:
                raise RuntimeError('the running wait has no fixed time left to set. End it now '
                                   '(end_wait).')
            # a missing value must not read as "0 minutes", which would silently end the wait;
            # nor may inf (every status poll then failed on int(inf)) or nan (max(0, nan) is
            # 0: the wait ended at once, recorded as done) - blind review #015. 0 is refused
            # too (U01): ending a wait is its own decision, end_wait
            mins = _as_number(minutes)
            if mins is None or not 0 < mins <= _MAX_MINUTES:
                raise ValueError('set_wait needs minutes: a number above 0 and at most %g (got %s) '
                                 '- to end the wait now, use end_wait'
                                 % (_MAX_MINUTES, _shown(minutes)))
            self._wait_deadline = time.monotonic() + mins * 60.0
            self.wait_end = time.time() + mins * 60.0
            self.log('wait: %g min remaining' % mins, 'warn')

    def skip_block(self, uid=None):
        """Skip the running block - and only the one the operator saw (contract C2).

        The page sends the uid its confirm dialog showed. A block that finished while the
        dialog was open has been replaced by the next one, and skipping "whatever runs now"
        killed that one instead (blind review #089). Between two blocks nothing is running,
        and a skip there used to land on the next block the moment it started.
        """
        with self.lock:
            if self.run_state != RUN_RUNNING:
                raise RuntimeError('nothing is running')
            cur = self.current_uid
            now = next((i for i in self.seq['items'] if i['uid'] == cur), None)
            if cur is None or now is None:
                raise RuntimeError('no block is running at this moment (the last one has just '
                                   'finished) - nothing was skipped')
            if uid is not None and uid != cur:
                raise RuntimeError('that block is not running any more - it finished before the '
                                   'Skip arrived, and "%s" is running now. Nothing was skipped.'
                                   % now['name'])
            self._skip.set()
            self._end_wait.set()
            proc = self._proc
            self.log('skip requested for the running block "%s"' % now['name'], 'warn')
        # Outside the lock, as stop() does: the child's grace to clean up is up to ten
        # seconds, and under the lock every poll, every Stop and the child's own output
        # waited for it (blind review #006). Only the child running NOW is killed - one
        # started after this line is the next block's. None: the block is still being
        # prepared, and _spawn will not start it with the skip set.
        if proc is not None:
            self._kill_child(proc)

    def stop(self, reason='user'):
        """Kill the child, run the safe stop, leave the queue where it is.

        Serialised: from the kill to the end of the safe stop nothing may start (F05), and
        a safe stop that fails leaves a fault that refuses Start until one succeeds (F12).
        The safe stop runs only once nothing the worker started can still reach an
        instrument: a runner that was about to start is refused or killed (see _spawn)
        (blind review #001).
        """
        with self._stop_lock, self._ss_hold('stop'):
            with self.lock:
                self._stop_gen += 1
                self._stopping_now = True
                self._stop_reason = reason
                if self.run_state == RUN_RUNNING:
                    self.run_state = RUN_STOPPING
                self._abort.set()
                self._end_wait.set()
            self.log('STOP (%s): killing the measurement' % reason, 'warn')
            res = None
            try:
                # each step on its own: a kill that raised used to skip the safe stop
                # altogether (N20). What it may have left alive is said, and is a fault.
                alive_after = []
                try:
                    self._kill_child()
                except Exception as exc:                    # noqa: BLE001
                    self.log('STOP: the kill failed (%s: %s) - the safe stop runs anyway'
                             % (type(exc).__name__, exc), 'error')
                    with self.lock:
                        p = self._proc
                    if p is not None and p.poll() is None:
                        alive_after.append('the runner (pid %s) may still be running: the kill '
                                           'failed (%s: %s)' % (p.pid, type(exc).__name__, exc))
                # unconditional: a killed measurement may have left its outputs on
                res = self.run_safe_stop(reason='stop')
                if alive_after:
                    res = dict(res or {}, ok=False, error='; '.join(alive_after))
            except Exception as exc:                        # noqa: BLE001
                # whatever it was, a stop must not stay "stopping" for good with no fault,
                # refusing Start until the panel is restarted (blind review #101)
                self.log('STOP: %s: %s' % (type(exc).__name__, exc), 'error')
                res = {'ok': False, 'error': '%s: %s' % (type(exc).__name__, exc)}
            finally:
                with self.lock:
                    self._stopping_now = False
                    after, self._after_stop = self._after_stop, None
                    if self.run_state == RUN_STOPPING:
                        if after:
                            # a trial that ended while this ran: the queue as it was (#005)
                            self.run_state, self.message = after
                        else:
                            self.run_state = RUN_PAUSED
                            self.message = 'stopped by %s' % reason
                            self._close_batch_if_done('stopped by %s after the last block had '
                                                      'measured' % reason)
                    if not (res or {}).get('ok'):
                        self._refine_fault('the safe stop after the stop failed (%s)'
                                           % ((res or {}).get('error') or 'non-zero exit'))
                    self._touch_pause_file(False)
                    self._save_queue()
        # the worker is on its way out; let it go before anyone can press Start
        w = self._worker_thread
        if w is not None and w.is_alive() and w is not threading.current_thread():
            w.join(3.0)
        return self.snapshot()

    def _set_fault(self, why):
        """A kill was followed by a safe stop that did not succeed: the outputs may still
        be on. The queue stops advancing and Start is refused until a safe stop succeeds.
        Under the lock."""
        if not self.fault:
            # what the queue said before, for when a safe stop clears the fault (N18)
            self._fault_prev = (self.run_state, self.message)
        self.fault = '%s - the outputs may still be on. Run SAFE STOP until it succeeds.' % why
        self.run_state = RUN_ERROR
        self.message = self.fault
        self.log('FAULT: ' + self.fault, 'error')

    def _refine_fault(self, why):
        """Under the lock. run_safe_stop sets a fault for every failed safe stop; the caller
        knows better what it followed, so its wording replaces the generic one without a
        second FAULT line. A safe stop that was not run_safe_stop's sets it here."""
        if self.fault:
            self.fault = '%s - the outputs may still be on. Run SAFE STOP until it succeeds.' % why
            self.run_state = RUN_ERROR
            self.message = self.fault
        else:
            self._set_fault(why)

    def _close_batch_if_done(self, note):
        """Under the lock. A stop or a pause that leaves nothing pending has finished the
        sequence: it is closed the way the worker's own "sequence finished" does it. Left
        'paused', Resume answered "every block already ran" and blocks added the next day
        joined last night's batch in History (blind review #117)."""
        items = self.seq['items']
        if (self._trial or self.fault or self.run_state != RUN_PAUSED or not items
                or any(i['status'] in ('pending', 'running') for i in items)):
            return False
        # with a failed block it stays that batch, 'error', and says so (U03)
        self._finish_sequence(note)
        return True

    # ---- the one "safe stop in flight" state (see __init__) ----

    def _ss_take(self, reason=''):
        with self.lock:
            self._ss_holds += 1
            if reason:
                self._ss_reason = reason

    def _ss_release(self):
        with self.lock:
            self._ss_holds = max(0, self._ss_holds - 1)
            if not self._ss_holds:
                self._ss_reason = ''

    @contextlib.contextmanager
    def _ss_hold(self, reason=''):
        self._ss_take(reason)
        try:
            yield
        finally:
            self._ss_release()

    def _pb_claimed(self):
        """Under the lock. Someone runs a safe stop, or is about to: a hold, the script itself,
        a runner's own after this panel died under it (#102) - known by its record, or by the
        process list (a runner's helper, a run of the safe-stop script this panel did not
        start: #098 re-check), while its own limits allow."""
        return (self._ss_holds > 0 or self._safe_stop_lock.locked() or self._orphan_busy()
                or bool(self._ss_foreign_waited()))

    def _ss_busy(self):
        """A safe stop is running or about to, by whatever path - a runner's own, after this
        panel died under it, included (#102). Under the lock."""
        return self._pb_claimed() or self._stop_lock.locked()

    def _ss_why(self):
        """Under the lock: whose safe stop it is, for a refusal."""
        if self._ss_reason:
            return self._ss_reason
        if not (self._ss_holds or self._safe_stop_lock.locked() or self._stop_lock.locked()):
            if self._orphan_busy():
                return 'the runner\'s own, after the panel was restarted'
            fs = self._ss_foreign_waited()
            if fs:
                return self._ss_foreign_words(fs)
        return 'stop'

    def _refuse_during_safe_stop(self, what):
        """Under the lock."""
        if self._ss_busy():
            raise RuntimeError('a safe stop is running (%s) - it has the instruments until it '
                               'has finished. Wait for it, then %s.' % (self._ss_why(), what))

    def _flag_outcome(self):
        """'stopped' / 'skipped' if a Stop or Skip is pending for the running block."""
        if self._abort.is_set():
            return 'stopped'
        if self._skip.is_set():
            return 'skipped'
        return None

    def _run_cfg(self):
        """config 'run', read defensively: a config value must never be able to disarm a kill
        or a safe stop by making it raise (blind review #046)."""
        run = self.cfg.get('run') if isinstance(getattr(self, 'cfg', None), dict) else None
        return run if isinstance(run, dict) else {}

    # the longest a safe stop is given: a hand-edited 1e7 used to overflow the wait inside
    # subprocess, which killed the script at once and called it "could not start" (N21)
    _SS_TIMEOUT_MAX = 3600

    def _ss_timeout(self):
        # a non-number here used to raise out of the safe stop itself (blind review #101)
        v = _finite(self._run_cfg().get('safe_stop_timeout_seconds', 120))
        if not v or v <= 0:
            return 120.0
        return float(min(v, self._SS_TIMEOUT_MAX))

    def _safe_stop_path(self):
        """paths.safe_stop_script as an absolute path; '' when none is configured (an empty value
        means "no stop script": the safe stop step is skipped, and that is never a fault). The
        default one that is not under the root is the panel's own example (P.safe_stop_script).
        Raises what P.resolve raises for a value that is not a path."""
        return P.safe_stop_script(self.cfg)

    def safe_stop_everything(self, reason='user'):
        """The header button. Not "run the script": stop what is running, THEN the script.

        A safe stop that left the queue running was a switch-off the next block could undo
        a minute later, and a measurement child kept sending to instruments the script had
        just silenced (F03). So: a running sequence is stopped (which kills its child and runs
        the safe stop), and only when nothing is running is the script run on its own.
        """
        with self._ss_hold(reason):
            with self.lock:
                self._stop_gen += 1                 # a Start being prepared is void (#000)
                running = self.run_state in (RUN_RUNNING, RUN_STOPPING)
                # a runner the worker has not let go of is a sequence still running,
                # whatever run_state says (blind review #001)
                child = self._proc is not None and self._proc.poll() is None
            if running or child:
                self.stop(reason=reason)
                return {'ok': not self.fault, 'stopped_sequence': True,
                        'error': self.fault or None}
            res = self.run_safe_stop(reason=reason)
            res['stopped_sequence'] = False
            return res

    def run_safe_stop(self, reason='user'):
        """Run the configured safe-stop script (paths.safe_stop_script). Always allowed.

        It always returns a verdict and never raises (blind review #101), and a failure is
        a fault whoever asked for it: the header button with nothing running and a Safe stop
        block used to shrug it off (#055). An empty paths.safe_stop_script means there is no
        stop script: the step is skipped and the verdict is ok (never a fault).
        """
        res = {'ok': False, 'error': 'the safe stop did not run'}
        with self._ss_hold(reason):
            with self.lock:
                self._stop_gen += 1
            try:
                script = self._safe_stop_path()
                if not script:
                    res = {'ok': True, 'skipped': 'no stop script configured'}
            except Exception as exc:                        # noqa: BLE001
                script = None
                res = {'ok': False, 'error': 'paths.safe_stop_script cannot be used: %s' % exc}
            self.log('SAFE STOP (%s): %s' % (reason, os.path.basename(script) if script else
                                             'no stop script configured (paths.safe_stop_script '
                                             'is empty) - nothing to run' if script == '' else '?'),
                     'warn')
            # a runner's own safe stop, from before this panel started, may still be running:
            # never two at once (blind review #102) - known by its record or by the process list,
            # and waited for as long as its own limits allow (#098 re-check)
            _LEFT_SCAN.last_ok, _LEFT_SCAN.ended = None, []
            self._orphan_wait()
            left_over, beside, unproven, ended_now = '', '', False, []
            with self._safe_stop_lock:
                try:
                    # after a restart: what this panel left running is ended first (N30, N31)
                    left_over = self._end_leftovers()
                except Exception as exc:                    # noqa: BLE001 - never raise from here
                    _LEFT_SCAN.last_ok = False
                    self.log('safe stop: the check for processes left from before the restart '
                             'failed: %s: %s' % (type(exc).__name__, exc), 'warn')
                with self.lock:
                    again = self._orphan_busy() or bool(self._ss_foreign_waited())
                if again:
                    # a safe stop in flight that the leftovers' own look found - a runner that
                    # handed its safe stop to its helper between the list and the kill - or a
                    # runner left alone in its own safe stop: waited for as well, never run beside
                    # (#098 re-check: the script started at once beside the helper)
                    self._orphan_wait()
                    try:
                        left_over = self._end_leftovers()
                    except Exception as exc:                # noqa: BLE001 - never raise from here
                        _LEFT_SCAN.last_ok = False
                        self.log('safe stop: the check for processes left from before the restart '
                                 'failed: %s: %s' % (type(exc).__name__, exc), 'warn')
                # what ours starts beside, looked at now - not what the wait saw before the
                # leftovers were ended (#098 re-check)
                with self.lock:
                    beside = self._orphan_beside()
                # the leftover check is on and the last look at the process list failed: nothing
                # shows ours runs alone
                unproven = bool(self._restart_check) and getattr(_LEFT_SCAN, 'last_ok', None) is False
                ended_now = list(getattr(_LEFT_SCAN, 'ended', None) or [])
                if beside:
                    self.log('a safe stop that this panel did not start is still running (%s) - '
                             'running ours anyway, beside it: this safe stop does not clear the '
                             'fault' % beside, 'error')
                with self.lock:
                    self._pb_busy = True
                    self._pb_owner = threading.current_thread()
                try:
                    if script:
                        try:
                            res = self._safe_stop_script(script)
                        except Exception as exc:            # noqa: BLE001 - never raise from here
                            self.log('safe stop failed: %s: %s' % (type(exc).__name__, exc), 'error')
                            res = {'ok': False, 'error': '%s: %s' % (type(exc).__name__, exc)}
                finally:
                    with self.lock:
                        self._pb_busy = False
                        self._pb_owner = None
            if left_over and res.get('ok'):
                # the script ran, but something from before the restart is still running
                res = dict(res, ok=False, error='the safe stop ran, but ' + left_over)
            with self.lock:
                still = self._orphan_beside() if res.get('ok') else ''
                if res.get('ok') and (beside or still):
                    # ours ran beside a safe stop this panel did not start - the wait for it gave
                    # up, or it outlived its own limits: that one may have driven the outputs
                    # during ours, or still does after it, so ours shows nothing and the records
                    # are not answered - even when it ended during ours (#098 re-check)
                    res = dict(res, ok=False, error='the safe stop ran, but a safe stop that this '
                               'panel did not start was still running when it %s (%s) - the two '
                               'overlapped, so this one does not show the outputs are off: press '
                               'SAFE STOP again once it has ended'
                               % ('started' if beside else 'ended', beside or still))
                    self.log(res['error'], 'error')
                    if not self.fault:
                        self._set_fault(res['error'])
                        self._save_queue()
                elif res.get('ok') and unproven:
                    # the leftover check is on and its last look at the process list failed:
                    # a safe stop this panel did not start may have run beside ours, and a fault
                    # is cleared only by one that really ran alone - so a fault that stands
                    # stays, and the records stay unanswered (#098 re-check). None is raised
                    # for it: Start is refused anyway while the list cannot be read
                    res = dict(res, ok=False, error='the safe stop ran, but the list of running '
                               'processes could not be read, so it cannot be shown that no other '
                               'safe stop ran beside this one - press SAFE STOP again once it can '
                               'be read')
                    self.log(res['error'], 'error')
                elif res.get('ok'):
                    # the records of runners that died with the panel have been answered now
                    for p in self._orphan_open:
                        self._orphan_archive(p)
                    self._orphan_open = []
                    # ... and so is any a runner this SAFE STOP ended wrote just before (#098)
                    self._ss_answered.update(ended_now)
                    if self.fault:
                        self._clear_fault()
                        self._save_queue()
                else:
                    self._set_fault('the safe stop (%s) failed (%s)'
                                    % (reason, res.get('error') or 'non-zero exit'))
                    self._save_queue()
        return res

    def _clear_fault(self):
        """Under the lock. A safe stop has succeeded: the fault goes, and the queue says what
        it is now rather than 'stopped' about a queue nothing had stopped (N18)."""
        self.log('the safe stop succeeded: the fault is cleared, Start is allowed again', 'ok')
        self.fault = None
        prev, self._fault_prev = self._fault_prev, None
        if self.run_state != RUN_ERROR:
            return
        was = prev[0] if prev else None
        if was == RUN_ERROR:
            # a block had failed before the fault came: that is still what the queue says
            self.message = '%s (the safe stop has now succeeded)' % (prev[1] or 'error')
            return
        items = self.seq['items']
        started = any(i.get('status') in ('done', 'skipped', 'error') for i in items)
        pending = any(i.get('status') == 'pending' for i in items)
        failed = any(i.get('status') == 'error' for i in items)
        self.run_state = RUN_PAUSED if (started and pending) else RUN_IDLE
        if not pending and not failed:
            self.run_id = None                   # nothing left of that batch (#117)
        self.message = ('stopped; the safe stop has now succeeded'
                        if was in (RUN_RUNNING, RUN_STOPPING) else 'the safe stop has now succeeded')
        if not pending and failed:
            # only failed blocks left: that batch can still retry them (U03), and says so
            self._finish_sequence('the safe stop has now succeeded')

    def _safe_stop_script(self, script):
        """Run the safe-stop script once. Exit code 0 is OK; anything else - and a line of its
        output that says "stop FAILED" - is a failure. In simulation it is never run."""
        if self.cfg.get('sim'):
            self.log('[SIM] safe stop: %s is not run in simulation' % os.path.basename(script), 'warn')
            return {'ok': True, 'sim': True}
        if not os.path.isfile(script):
            self.log('safe stop script missing: %s (paths.safe_stop_script; leave it empty for no stop '
                     'script)' % script, 'error')
            return {'ok': False, 'error': 'script missing: %s' % script}
        # Its own process group and its own hidden console: a Ctrl-C at the panel's console
        # cannot cut it short, and closing that window cannot kill it half way - it finishes
        # even when Windows ends the panel (blind review #103, #024). backslashreplace: an old
        # copy's non-ASCII output must not break the decoding of what it said.
        flags = ((subprocess.CREATE_NEW_PROCESS_GROUP
                  | getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)) if os.name == 'nt' else 0)
        try:
            proc = self._run_safe_stop_child([P.python_exe(self.cfg), '-u', script], flags)
            failed = []
            for line in (proc.stdout or '').splitlines()[-40:]:
                # a stage that says it failed is an error in the log, not a warning (#099)
                bad = 'stop FAILED' in line or line.lstrip().startswith('[!]')
                if 'stop FAILED' in line:
                    failed.append(line.strip())
                self.log('  safe_stop| %s' % line, 'error' if bad else 'warn')
            if proc.returncode != 0:
                self.log('safe stop returned %d: %s' % (proc.returncode, (proc.stderr or '')[-400:]), 'error')
                return {'ok': False, 'error': 'exit code %d' % proc.returncode, 'rc': proc.returncode}
            if failed:
                # a script that caught its failure and still exited 0
                self.log('safe stop exited 0 but a stage says it FAILED - treated as a failure',
                         'error')
                return {'ok': False, 'error': 'a stage FAILED (%s)' % failed[0][:160], 'rc': 0}
            self.log('safe stop finished: OK', 'ok')
            return {'ok': True, 'rc': 0}
        except subprocess.SubprocessError as e:
            self.log('safe stop failed: %s' % e, 'error')
            return {'ok': False, 'error': str(e)}
        except Exception as e:                              # noqa: BLE001
            # CreateProcess failing (python.exe or the work folder missing, access denied,
            # out of memory): the safe stop did NOT run, and that must read as a failure,
            # not escape and leave STOP "stopping" with no fault (blind review #101)
            self.log('the safe stop could not be started: %s: %s' % (type(e).__name__, e), 'error')
            return {'ok': False, 'error': 'could not start the safe stop: %s: %s'
                                          % (type(e).__name__, e)}

    def _run_safe_stop_child(self, argv, flags):
        """subprocess.run(argv, capture_output, timeout), with the child's pid written down
        while it runs (state/safe_stop_child.json): it is started so that it outlives the panel
        (#103), and a panel restarted under it must not run a second safe stop beside it (N31).
        Raises what subprocess.run raises: OSError when it cannot start, TimeoutExpired."""
        timeout = self._ss_timeout()
        proc = subprocess.Popen(argv, cwd=P.work_dir(self.cfg), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, errors='backslashreplace',
                                stdin=subprocess.DEVNULL, creationflags=flags)
        try:
            self._ss_child_mark(proc.pid)
        except Exception as exc:                            # noqa: BLE001 - never in the way
            self.log('safe stop: could not write down its pid (%s)' % exc, 'warn')
        try:
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # what subprocess.run does, with the tree: a script that hangs in a driver
                self._kill_tree(proc)
                try:
                    proc.communicate(timeout=10)
                except (subprocess.SubprocessError, OSError, ValueError):
                    pass
                raise subprocess.TimeoutExpired(argv, timeout)
            # anything else (the panel going down under it) leaves it running to its end, on
            # purpose (#103); its record stays, so the next start knows it may still be running
        finally:
            if proc.poll() is not None:
                try:
                    self._ss_child_unmark()
                except Exception:                           # noqa: BLE001
                    pass
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    # ------------------------------------------------------------------ the worker

    def _pause_file(self):
        return os.path.join(P.STATE_DIR, 'pause_request')

    def _stop_file(self, uid):
        """Where a runner in a console of its own is asked to stop (blind review #024)."""
        return os.path.join(P.STATE_DIR, 'runs', re.sub(r'[^\w.-]', '_', str(uid)), 'stop_request')

    def _touch_pause_file(self, on):
        try:
            if on:
                with open(self._pause_file(), 'w', encoding='utf-8') as f:
                    f.write(str(time.time()))
            elif os.path.exists(self._pause_file()):
                os.unlink(self._pause_file())
        except OSError:
            pass

    def _wait_safe_stops(self):
        """Before a block: a safe stop someone else started (the header SAFE STOP, a runner's own
        after a restart) has the instruments until it has finished, and the next measurement used
        to start inside it (blind review #000). False if it did not finish within its own timeout
        and a minute."""
        t0, said = time.monotonic(), False            # a duration: not the wall clock (#049)
        limit = self._ss_timeout() + 60.0
        while True:
            with self.lock:
                busy = self._pb_claimed()
                why = self._ss_why()
            if not busy or self._abort.is_set():
                return True
            if not said:
                self.log('waiting for the safe stop that is running (%s) to finish before the '
                         'next block' % (why if why != 'stop' else 'safe stop'), 'warn')
                said = True
            if time.monotonic() - t0 > limit:
                return False
            time.sleep(0.2)

    def _worker(self):
        try:
            while True:
                self._df_flush_nudge()
                if not self._wait_safe_stops():
                    with self.lock:
                        self._set_fault('a safe stop did not finish within %g s, so the next block was not started' % (self._ss_timeout() + 60.0))
                        _push_between(self)
                    break
                with self.lock:
                    if self._abort.is_set() or self.run_state != RUN_RUNNING:
                        break
                    items = self.seq['items']
                    only = (self._trial or {}).get('uid')
                    item = next((i for i in items if i['status'] == 'pending' and (only is None or i['uid'] == only)), None)
                    if item is None and self._trial:
                        self._end_trial(None, 'the block was not pending any more')
                        break
                    if item is None:
                        self._finish_sequence()
                        _push(self, 'finished')
                        self._save_queue()
                        break
                    self.cursor = items.index(item)
                    item.update(status='running', started=time.time(), finished=None, message='')
                    item.pop('stall', None)
                    item.pop('runner_notes', None)
                    self._halt_why = None
                    self._error_goes_on = None
                    item['segment'] = int(item.get('segment') or 0) + 1
                    ds_before = len(item.get('datasets') or [])
                    item['seg_ds_from'] = ds_before
                    item['seg_dsp_from'] = len(item.get('datasets_partial') or [])
                    pr0 = item.get('progress') if isinstance(item.get('progress'), dict) else {}
                    item['seg_rep_from'] = max(0, int(_as_number(pr0.get('done')) or 0))
                    if item['kind'] != 'script':
                        item['exec'] = {'params': copy.deepcopy(item.get('params') or {}), 'overrides': copy.deepcopy(item.get('overrides') or {}), 'when': time.time()}
                    self.current_uid = item['uid']
                    self._save_queue()
                self.log('--- block #%d  %s (%s) ---' % (self.cursor + 1, item['name'], item['kind']), 'head', item['uid'])
                crashed = False
                try:
                    outcome = self._run_item(item)
                except Exception as exc:
                    crashed = True
                    outcome = 'error'
                    item['message'] = 'engine error in "%s": %s: %s' % (item['name'], type(exc).__name__, exc)
                    self.log(item['message'], 'error', item['uid'])
                    try:
                        self._kill_child()
                    except Exception as exc2:
                        self.log('could not kill the child: %s' % exc2, 'error', item['uid'])
                    res = self.run_safe_stop(reason='engine error')
                    if not res.get('ok'):
                        with self.lock:
                            self._refine_fault('the safe stop after an engine error failed (%s)' % (res.get('error') or 'non-zero exit'))
                with self.lock:
                    item['finished'] = time.time()
                    self.current_uid = None
                    pos = self.seq['items'].index(item) + 1 if item in self.seq['items'] else 0
                    pr = item.get('progress')
                    cut = pr.get('running') if isinstance(pr, dict) else None
                    if cut:
                        pr['running'] = 0
                    if outcome == 'ok':
                        item['status'] = 'done'
                        self.cursor = pos
                        self.log('block #%d done (%s)' % (pos, _hms(item['finished'] - (item.get('started') or item['finished']))), 'ok', item['uid'])
                    elif outcome == 'skipped':
                        item['status'] = 'skipped'
                        self.cursor = pos
                    elif outcome == 'paused':
                        item['status'] = 'pending'
                        self.run_state = RUN_PAUSED
                        self.message = 'paused inside "%s"' % item['name']
                    elif outcome == 'stopped':
                        item['status'] = 'pending'
                        note = 'stopped by %s' % self._stop_reason
                        if item['kind'] == 'script' and isinstance(pr, dict) and pr.get('total'):
                            note += ' - %d of %d rep(s) done%s' % (int(_num(pr.get('done'), 0)), int(_num(pr.get('total'), 0)), ', rep %d was cut off' % cut if cut else '')
                        item['message'] = note
                    else:
                        item['status'] = 'error'
                        _push(self, 'failed', item)
                        self.cursor = pos
                        if self._halt_why or (self._run_cfg().get('stop_queue_on_error', True) and (not self._error_goes_on)):
                            self.run_state = RUN_ERROR
                            self.message = 'block "%s" failed: %s' % (item['name'], item.get('message') or '')
                        elif not self._run_cfg().get('stop_queue_on_error', True):
                            self.log('"%s" failed - run.stop_queue_on_error is false: the next block starts' % item['name'], 'warn', item['uid'])
                    nx = self._next_after_rep
                    if nx and nx.get('uid') == item['uid']:
                        self._next_after_rep = None
                        if not self._pause_after_rep:
                            self._touch_pause_file(False)
                        self.log('"Next block after this rep" had nothing to cut: %s' % ('the block finished all its %d rep(s) first' % int(_num((pr if isinstance(pr, dict) else {}).get('total'), 0) or 0) if outcome == 'ok' else 'the %s came first - it is dropped%s' % ({'stopped': 'Stop', 'skipped': 'Skip', 'paused': 'pause'}.get(outcome, 'failure'), ' (Resume measures the reps left)' if outcome in ('stopped', 'paused') else '')), 'warn', item['uid'])
                    self._halt_why = None
                    self._error_goes_on = None
                    self._record_history(item, pos, ds_from=ds_before, outcome=outcome)
                    self._skip.clear()
                    if crashed:
                        self.run_state = RUN_ERROR
                        self.message = item['message']
                    if self.fault:
                        self.run_state = RUN_ERROR
                        self.message = self.fault
                    self._save_queue()
                    if self._trial:
                        self._end_trial(item, outcome if outcome in ('paused', 'stopped') else item['status'])
                        break
                    if self.run_state != RUN_RUNNING:
                        self._close_batch_if_done('%s after the last block had measured' % (self.message or 'stopped'))
                        break
                    if self._pause_after_block:
                        self._pause_after_block = False
                        self.run_state = RUN_PAUSED
                        self.message = 'paused after "%s"' % item['name']
                        self.log('paused after block #%d' % pos, 'warn')
                        self._close_batch_if_done('the pause came after the last block')
                        break
                    if self._pause_after_rep:
                        self._pause_after_rep = False
                        self._touch_pause_file(False)
                        self.run_state = RUN_PAUSED
                        self.message = 'paused after "%s"' % item['name']
                        self.log('paused after block #%d (%s)' % (pos, 'the pause after the rep came during its last rep' if item['kind'] == 'script' else 'pause after rep was armed during a %s block, which has no reps' % item['kind']), 'warn')
                        self._close_batch_if_done('the pause came after the last block')
                        break
        except Exception as e:
            self.log('engine crashed: %s: %s' % (type(e).__name__, e), 'error')
            with self.lock:
                self.run_state = RUN_ERROR
                self.message = 'engine error: %s' % e
                stuck = [i for i in self.seq['items'] if i.get('status') == 'running']
                for i in stuck:
                    i['status'] = 'error'
                    i['message'] = 'engine error: %s: %s' % (type(e).__name__, e)
                    i['finished'] = time.time()
                    _push(self, 'failed', i)
                self.current_uid = None
            if stuck:
                try:
                    self._kill_child()
                except Exception:
                    pass
                res = self.run_safe_stop(reason='engine crashed')
                if not res.get('ok'):
                    with self.lock:
                        self._refine_fault('the safe stop after the engine crashed failed (%s)' % (res.get('error') or 'non-zero exit'))
                for i in stuck:
                    try:
                        with self.lock:
                            pos = self.seq['items'].index(i) + 1 if i in self.seq['items'] else 0
                        self._record_history(i, pos, ds_from=int(_num(i.get('seg_ds_from'), 0)), outcome='error')
                    except Exception as exc2:
                        self.log('could not record history for "%s": %s' % (i.get('name'), exc2), 'warn')
            else:
                _push_between(self)
        finally:
            with self.lock:
                if self.run_state == RUN_RUNNING:
                    self.run_state = RUN_PAUSED
                self.current_uid = None
                self.wait_end = None
                self._wait_deadline = None
                self._touch_pause_file(False)
                self._pause_after_rep = False
                self._pause_after_block = False
                self._next_after_rep = None
                if self._trial:
                    tr, self._trial = (self._trial, None)
                    it = next((i for i in self.seq['items'] if i['uid'] == tr.get('uid')), None)
                    if it is not None and 'trial_saved' in it:
                        msg = it.get('message') or ''
                        self._trial_restore(it)
                        it['message'] = 'its trial run ended in an engine error: %s' % msg
                    self.run_id = tr.get('run_id')
                self._save_queue()
            self._df_flush_nudge()

    def _run_item(self, item):
        kind = item['kind']
        flag = self._flag_outcome()
        if flag:
            # a Stop or Skip between "marked running" and here: nothing has started, so
            # nothing is left for the safe stop to catch up with (blind review #001)
            self.log('%s before it started' % flag, 'warn', item['uid'])
            return flag
        if kind == 'wait':
            # checked again here: a value can reach the queue without being typed (a palette
            # entry, a saved sequence, a queue from before the checks), and a bad one used to
            # be replaced by a default - a 90-minute wait of one minute (blind review #032)
            ran = dict(item, params=_run_params(item))
            bad = _param_problems(ran, self.cfg, at_start=True)
            if bad:
                return self._refuse_values(item, bad)
        return self._run_item_kind(item, kind)

    def _run_item_kind(self, item, kind):
        if kind == 'wait':
            return self._run_wait(item)
        if kind == 'safe_stop':
            res = self.run_safe_stop(reason='block')
            if res.get('skipped'):
                item['message'] = 'no stop script configured - nothing to run'
            return 'ok' if res.get('ok') else 'error'
        if kind == 'script':
            return self._run_script(item)
        item['message'] = 'unknown block kind %r - this panel runs %s' % (kind, ', '.join(KINDS))
        self.log(item['message'], 'error', item['uid'])
        return 'error'

    def _refuse_values(self, item, bad):
        """The block does not start: its values are not ones it can run with. Nothing has
        been started, so there is nothing for a safe stop to catch up with."""
        item['message'] = 'not started: %s%s' % (bad[0], '' if len(bad) == 1
                                                 else ' (and %d more - see the log)' % (len(bad) - 1))
        self.log('"%s" not started - its values cannot be used as they are:' % item.get('name'),
                 'error', item['uid'])
        for b in bad:
            self.log('  ' + b, 'error', item['uid'])
        return 'error'

    def _run_wait(self, item):
        # checked in _run_item: a number of minutes, 0 or more (blind review #015, #032)
        minutes = _num(_run_params(item).get('minutes', 1), 1)
        total = max(0.0, minutes) * 60.0
        with self.lock:
            # cleared before the wait is published as running, in the same step: an "end the
            # wait now" accepted just after _in_wait was set used to be wiped by these clears,
            # and the wait ran its full time after the page had said "wait ended" (U01)
            self._end_wait.clear()
            self._wait_skip.clear()
            t0 = time.monotonic()
            self._wait_deadline = t0 + total
            self.wait_end = time.time() + total
            self._in_wait = item['uid']
            item['progress'] = {'done': 0, 'total': int(total), 'unit': 's'}
        self.log('waiting %g min' % minutes, 'info', item['uid'])
        try:
            while True:
                with self.lock:
                    # the monotonic deadline decides; wait_end follows it for the display (#049)
                    now = time.monotonic()
                    dl = self._wait_deadline if self._wait_deadline is not None else now
                    left = dl - now
                    shown = time.time() + max(0.0, left)
                    if self.wait_end is None or abs(shown - self.wait_end) > 1.0:
                        self.wait_end = shown       # only after a clock step: it is shown
                    # from the deadline as it is NOW: after "+10 min" or "Set remaining…" the
                    # row and the agent's progress kept the original length, and showed a
                    # countdown the engine was not running (U01)
                    elapsed = max(0.0, now - t0)
                    item['progress'] = {'done': int(elapsed),
                                        'total': int(elapsed + max(0.0, left)), 'unit': 's'}
                if self._abort.is_set():
                    return 'stopped'
                if self._skip.is_set():
                    self.log('wait skipped', 'warn', item['uid'])
                    return 'skipped'
                if left <= 0:
                    return 'ok'
                if self._end_wait.wait(timeout=min(1.0, max(0.05, left))):
                    self._end_wait.clear()
                    # Woken: by pause, skip, stop, or "end the wait now". Only the last one
                    # ends the wait. A pause used to end it too - a 100-minute wait cut to
                    # a tenth of a second. A pause now takes effect when the wait is over,
                    # which is what "pause after this block" says (F01).
                    if self._wait_skip.is_set():
                        self._wait_skip.clear()
                        item['message'] = 'ended early by the user'
                        self.log('wait ended early', 'warn', item['uid'])
                        return 'ok'
        finally:
            with self.lock:
                self.wait_end = None
                self._wait_deadline = None
                self._in_wait = None

    def _start_afresh(self, item, reps, why):
        """The block runs again from rep 1 with an empty dataset list: what it measured before
        was taken with other values, and one block must never pool two parameter sets (blind
        review #087, #010, #011). The earlier segments keep their History rows, which list
        their datasets; nothing on disk is touched."""
        with self.lock:
            old = [d for d in (item.get('datasets') or []) if d]
            done = int(_num((item.get('progress') or {}).get('done'), 0))
            item['datasets'] = []
            item['datasets_partial'] = []
            item['seg_ds_from'] = 0
            item['seg_dsp_from'] = 0
            item['seg_rep_from'] = 0             # this segment measures from rep 1 (U30)
            item.pop('ended_early', None)        # from rep 1, its early end is gone
            item['progress'] = {'done': 0, 'total': reps}
        self.log('"%s" %s - the %d rep(s) measured before (%d dataset(s)) are kept in History but '
                 'NOT pooled with new ones: it runs again from rep 1'
                 % (item['name'], why, done, len(old)), 'warn', item['uid'])

    def _keep_patched_copy(self, item, path):
        """A copy of the patched script for History, per run and segment. The one the runner
        runs is rewritten by the next run of the block, so a History row that pointed at it
        later showed another run's values (blind review #111)."""
        try:
            d = os.path.join(os.path.dirname(path), '%s-seg%d' % (
                re.sub(r'[^\w.-]', '_', str(self.run_id or 'run')), int(item.get('segment') or 1)))
            os.makedirs(d, exist_ok=True)
            out = os.path.join(d, os.path.basename(path))
            with open(path, 'rb') as src, open(out, 'wb') as dst:
                dst.write(src.read())
            return P.rel_to_root(out)
        except OSError as e:
            self.log('could not keep a copy of the patched script for History: %s' % e, 'warn',
                     item['uid'])
            return ''

    @staticmethod
    def _keep_prev(item, prev, outcome):
        """A block stopped before its runner started keeps the last segment's exec: the next
        resume is compared against it, and without it an edit made meanwhile went unseen."""
        if prev and not item.get('exec'):
            item['exec'] = prev
        item.pop('exec_last', None)
        return outcome

    def _run_script(self, item):
        # a stale snapshot must not be recorded for THIS attempt; it is what the last segment
        # ran with, which is what a resume is compared against (blind review #087). It waits
        # in exec_last meanwhile, which goes to disk with every save: a panel that went down
        # before the runner started must not leave a half-run block with no record of what its
        # reps were measured with (round 2 N57)
        with self.lock:
            prev = item.pop('exec', None) or (item.get('exec_last')
                                              if isinstance(item.get('exec_last'), dict) else None)
            if prev:
                item['exec_last'] = prev
            else:
                item.pop('exec_last', None)
        target = P.resolve(item.get('target'))
        # only a script of the panel's script folders runs in real mode: a block sent over the
        # network with any .py path (a share, another drive) must not run it (#044, N10)
        where = _target_refusal(self.cfg, item.get('target'))
        if where:
            return self._keep_prev(item, prev, self._refuse_values(item, [where]))
        if not os.path.isfile(target):
            item['message'] = 'script not found: %s' % target
            self.log(item['message'], 'error', item['uid'])
            return 'error'
        with self.lock:
            # read in one piece with the plan it is recorded as: an edit that lands meanwhile
            # belongs to the next run, whole
            # None is "not set" (contract C3), never a value main() is handed - a queue saved
            # before the entry checks may still hold one
            params = {k: v for k, v in (item['params'] or {}).items() if v is not None}
            raw_overrides = dict(item.get('overrides') or {})
            plan = _plan_of(item)
        # the runner runs max(1, int(reps)); the plan and History now say the same (#009).
        # One that is not a number at all ('inf', 'nan', '3 reps') is refused here: int(inf)
        # used to escape as an engine error, and a word ran 1 rep (blind review #035)
        raw_reps = params.pop('reps', params.pop('numreps', 1))
        f_reps = 1.0 if raw_reps is None else _as_number(raw_reps)
        if f_reps is None:
            return self._keep_prev(item, prev, self._refuse_values(
                item, ['reps must be a whole number of 1 or more (got %s)' % _shown(raw_reps)]))
        reps = max(1, int(f_reps))
        # `reps` is the PLAN and is never rewritten; how many have run is progress.done. A
        # pause used to write the remainder back over the plan, so a Reset re-ran a 3-rep
        # block as 2 reps, and a saved template took the remainder as its setting (F16).
        done_before = max(0, int(_num((item.get('progress') or {}).get('done'), 0) or 0))
        changed = _plan_changes(prev, item, plan) if done_before > 0 else []
        # a block ended early by "Next block after this rep" is complete with the reps it had
        # then - a panel restart right after left it pending with fewer reps than the plan;
        # Start then completes it, as for a block with every rep measured (#084). A record that
        # does not fit what the block holds now (other values, other reps) is stale: it goes
        ee = item.get('ended_early') if isinstance(item.get('ended_early'), dict) else None
        ee_done = bool(ee) and not changed and 0 < int(_num(ee.get('reps_done'), 0) or 0) <= done_before \
            and int(_num(ee.get('of'), 0) or 0) == reps
        if ee and not ee_done:
            with self.lock:
                item.pop('ended_early', None)
        if (done_before < reps and not ee_done) or changed:
            # it is going to measure: a parameter main() cannot take, or of the wrong type for
            # it, would be dropped and the block run on the script's own value while the page
            # and History showed this one (blind review #030). Refused like a stale override.
            bad = _main_arg_problems(item.get('target'), params)
            if bad:
                return self._keep_prev(item, prev, self._refuse_values(item, bad))
        if done_before > 0:
            if changed:
                # A resume with new values ran the remaining reps with them, and History
                # pooled both sets as one block (blind review #087)
                self._start_afresh(item, reps, 'was changed since its last segment (%s)'
                                   % '; '.join(changed[:6]))
                done_before, prev = 0, None
        elif any(item.get('datasets') or []):
            # from rep 1 on top of datasets it kept: they are an earlier attempt's
            self._start_afresh(item, reps, 'starts from rep 1 but still listed datasets')
        if done_before >= reps > 0 or ee_done:
            # Every rep of the plan is already measured - the panel restarted as the block
            # ended, or reps was lowered after a partial run. It used to start again from rep 1
            # on top of the datasets it kept: hours re-measured (blind review #115).
            if done_before >= reps:
                self.log('all %d rep(s) of this block were already measured (%d done) - not measuring '
                         'again; the block is complete. Press ↺ on the block to measure it again '
                         'from rep 1.' % (reps, done_before), 'warn', item['uid'])
            else:
                self.log('this block was ended early after rep %d of %d ("Next block after this rep") - not '
                         'measuring again; the block is complete. Press ↺ on the block to measure it '
                         'again from rep 1.' % (done_before, reps), 'warn', item['uid'])
            with self.lock:
                item['progress'] = {'done': done_before, 'total': reps, 'running': 0}
                if prev:
                    item['exec'] = copy.deepcopy(prev)       # what those reps were measured with (#116)
                item.pop('exec_last', None)
            return 'ok'

        # values the user edited in the UI are written into a COPY of the script under
        # state/runs/<uid>/ - the script itself is never touched
        overrides = {k: v for k, v in raw_overrides.items() if v is not None}
        flag = self._flag_outcome()
        if flag:
            self.log('%s before the runner started' % flag, 'warn', item['uid'])
            return self._keep_prev(item, prev, flag)
        patched = ''
        if overrides:
            # every key that is patched, with its recorded source where there is one: a partial
            # base used to leave the rest unchecked (#033)
            base = item.get('override_base') or {}
            problems = panel_scan.verify(target, {k: base.get(k) for k in overrides})
            if problems:
                item['message'] = problems[0]
                # not only "the script changed": a line that has no effect, or two keys for one
                # line, land here too (blind review #041)
                self.log('these parameter overrides cannot be used:', 'error', item['uid'])
                for pr in problems:
                    self.log('  ' + pr, 'error', item['uid'])
                return 'error'
            try:
                target, changes = panel_scan.write_patched_copy(
                    target, overrides, os.path.join(P.STATE_DIR, 'runs', item['uid']))
            except (ValueError, KeyError, SyntaxError, TypeError, OSError) as e:
                # TypeError: a null inside an array override (float(None)) used to escape as an
                # engine error and stop the queue (blind review #031)
                item['message'] = 'parameter override failed: %s' % e
                self.log(item['message'], 'error', item['uid'])
                return 'error'
            for c in changes:
                self.log('  override  %s' % c, 'warn', item['uid'])
            self.log('running the patched copy: %s' % P.rel_to_root(target), 'info', item['uid'])
            if changes:
                patched = self._keep_patched_copy(item, target)
        for w in self._script_cautions(item, target if not overrides else P.resolve(item.get('target')),
                                       overrides, reps):
            self.log('CAUTION: ' + w, 'warn', item['uid'])

        # what this run is actually made of, frozen now: History is written from it, and an
        # edit to the block while it runs is the plan for the next run, not this one (F19).
        # `plan` is what a resume is compared against (#087); `source_target` the script
        # itself, not the copy (#111).
        with self.lock:
            item['exec'] = {'target': P.rel_to_root(target), 'reps': reps,
                            'source_target': plan['script'],
                            'patched_copy': patched,
                            'start_rep': done_before + 1,
                            'params': copy.deepcopy(dict(params, reps=reps)),
                            'overrides': copy.deepcopy(overrides),
                            'plan': plan, 'when': time.time()}
            item.pop('exec_last', None)          # this segment's own record now (N57)

        cmd = [P.python_exe(self.cfg), '-u', os.path.join(P.RUNNERS_DIR, 'run_block.py'),
               '--target', target, '--reps', str(reps), '--start-rep', str(done_before + 1),
               '--params', json.dumps(params), '--pause-file', self._pause_file(),
               '--data-root', P.data_root(self.cfg),
               # if this panel disappears (the console window is closed, a power cut) the
               # runner stops itself and runs the safe stop, because nobody else will
               '--parent-pid', str(os.getpid()),
               # '' when no stop script is configured: the runner then skips that step
               '--safe-stop', self._safe_stop_path(),
               # the ORIGINAL script's folder: with overrides the target is a copy under
               # state/runs/<uid>/, and a sibling import failed only for blocks with overrides
               # (blind review #109)
               '--script-dir', os.path.dirname(P.resolve(item.get('target'))),
               # where it records its own safe stop if this panel dies - read at the next
               # start (#102); this engine's state folder, not the runner's guess at it
               '--orphan-dir', self._orphan_dir(),
               # a stop reaches it through this file: it has a console of its own (#024)
               '--stop-file', self._stop_file(item['uid'])]
        if self.cfg.get('sim'):
            cmd += ['--sim', '--sim-seconds', str(_num(self.cfg.get('sim_seconds_per_rep'), 6))]
        with self.lock:
            item['progress'] = {'done': done_before, 'total': reps}
        self.log('run %s  reps=%d%s  params=%s'
                 % (os.path.basename(target), reps,
                    '' if not done_before else ' (resuming at rep %d)' % (done_before + 1),
                    json.dumps(params, ensure_ascii=False)), 'info', item['uid'])

        state = {'outcome': None, 'reps_done': done_before, 'done': False,
                 'in_rep': 0, 'interrupted': False, 'rep_t0': None, 'rep_m0': None,
                 'folders': {}, 'saved': {}}

        def on_event(ev):
            kind = ev.get('event')
            if kind == 'done':
                state['done'] = True
            if kind == 'rep_start':
                # a rep can run for half an hour; "0/4 done" and "on the first of 4" have to
                # look different while you are watching it
                state['in_rep'] = int(ev.get('i') or 0)
                # when it began, for the folders it may leave if the runner dies in it (#100)
                state['rep_t0'], state['rep_m0'] = time.time(), time.monotonic()
                with self.lock:
                    item['progress'] = {'done': state['reps_done'], 'total': reps,
                                        'running': int(ev.get('i') or 0)}
            elif kind == 'rep_done':
                state['reps_done'] = int(ev.get('i') or 0)
                state['in_rep'] = 0
                with self.lock:
                    item['progress'] = {'done': state['reps_done'], 'total': reps,
                                        'running': 0}
                    # on disk now, not only at the block's end: a panel that dies at rep 8
                    # of 10 resumes at rep 9 with its datasets, instead of rep 1 (#004)
                    self._save_queue()
                self.log('rep %s/%s done' % (ev.get('i'), ev.get('n')), 'ok', item['uid'])
            elif kind == 'interrupted':
                state['interrupted'] = True
            elif kind == 'folder':
                # where the rep is writing, said by the runner as soon as it is on disk: not a
                # dataset - the record of that rep if the runner dies before it can say more
                # (#100, _rep_folders_left)
                if isinstance(ev.get('path'), str) and ev['path']:
                    rep_n = int(_num(ev.get('rep'), 0) or 0)
                    k = os.path.normcase(os.path.normpath(P.resolve(ev['path'])))
                    told = state['folders'].setdefault(rep_n, [])
                    if k not in [os.path.normcase(os.path.normpath(P.resolve(x))) for x in told]:
                        told.append(ev['path'])
                    if ev.get('saved'):
                        # the script has said it saved that scan ('Data saved to'): a finished
                        # scan of the rep, a dataset if the runner then dies (#099 x #100)
                        state['saved'].setdefault(rep_n, set()).add(k)
                        self.log('rep %d: the script says it saved %s' % (rep_n, ev['path']),
                                 'info', item['uid'])
                    else:
                        self.log('rep %d is writing %s' % (rep_n, ev['path']), 'info', item['uid'])
            elif kind == 'params':
                # what the runner really handed main() (contract C6): History records that,
                # not a name main() does not take (blind review #030)
                passed, ignored = ev.get('passed'), ev.get('ignored')
                with self.lock:
                    ex = item.get('exec')
                    if isinstance(ex, dict) and isinstance(passed, dict):
                        ex['params'] = dict({k: v for k, v in passed.items()
                                             if k not in _REP_ARGS}, reps=reps)
                        if isinstance(ignored, list) and ignored:
                            ex['params_ignored'] = [str(x) for x in ignored]
            elif kind == 'dataset':
                if ev.get('partial'):
                    # a folder the rep had created when it was interrupted or failed: it is
                    # on disk, so it is recorded - but it is kept apart from the reps that
                    # finished (agent B, finding B4)
                    with self.lock:
                        item.setdefault('datasets_partial', []).append(ev.get('path'))
                        self._save_queue(soon=True)
                    self.log('dataset (interrupted): %s' % ev.get('path'), 'warn', item['uid'])
                else:
                    with self.lock:
                        item.setdefault('datasets', []).append(ev.get('path'))
                        self._save_queue(soon=True)
                    self.log('dataset: %s' % ev.get('path'), 'info', item['uid'])
            elif kind == 'note':
                self.log('runner: %s' % (ev.get('message') or ''), 'warn', item['uid'])
                # on the card too, not only as a log line: 'NOT used', 'asked for input()',
                # 'wrote no new dataset' are things to see before the morning (C6, #030)
                if ev.get('message'):
                    self._item_note(item['uid'], 'runner_notes', str(ev['message'])[:300])
            elif kind == 'paused':
                state['outcome'] = 'paused'
                if ev.get('done') is not None:
                    state['reps_done'] = int(ev.get('done') or 0)
                # what the runner stopped FOR is decided HERE, as the engine handles the
                # runner's 'paused' line - "Next block after this rep" armed for this block then,
                # or a pause. A take-back after this line cannot un-stop the runner: the block
                # ends early and _end_early says the take-back came too late
                with self.lock:
                    nx = self._next_after_rep
                    state['next'] = dict(nx) if (nx and nx.get('uid') == item['uid']) else None
            elif kind == 'error':
                item['message'] = ev.get('message') or 'runner error'
                state['outcome'] = 'error'

        # 0.1.7: an analysis run from History asks which of its datasets this run's rep is still writing
        # (_an_live) - read only, while the runner runs, and gone when it ends
        self._an_live_run = (item, state, reps)
        try:
            rc = self._spawn(cmd, item['uid'], on_event)    # it finds --stop-file in cmd
        finally:
            self._an_live_run = None

        # the runner ended inside a rep without a word on how: killed after the grace, killed
        # from outside, or the process ended in the middle of the script
        unreported = (bool(state['in_rep']) and not state['interrupted']
                      and state['outcome'] != 'error' and not state['done'])
        if rc == 0 and unreported and not (self._abort.is_set() or self._skip.is_set()):
            # exit code 0 from inside a rep (os._exit(0), an exit the runner never saw) is not
            # a finished rep: it used to make the block 'ok' - the partial rep credited, no
            # safe stop, the outputs left as the script had them (blind review #009)
            item['message'] = ('the runner exited (code 0) in the middle of rep %d: the script '
                               'ended the process before the rep finished - not counted as done'
                               % state['in_rep'])
            state['outcome'] = 'error'
        if unreported and not self.cfg.get('sim'):
            # the runner lists the folder of an interrupted rep itself, from its Ctrl-Break
            # handler; one killed after the grace never got there (blind review #009), nor
            # one killed from outside - the folders are looked for here instead (#100)
            how = ('killed after the %gs grace?' % _num(self._run_cfg().get('grace_seconds'), 10)
                   if (self._abort.is_set() or self._skip.is_set()) else 'exit code %s' % rc)
            found = self._rep_folders_left(item, state)
            # the scans the script said it had saved are finished ones, as report_partial()
            # files them (#099): only the rest is the scan the kill cut off (#099 x #100)
            saved_k = (state.get('saved') or {}).get(state['in_rep']) or set()
            done_scans = [p for p in found
                          if os.path.normcase(os.path.normpath(P.resolve(p))) in saved_k]
            found = [p for p in found if p not in done_scans]
            if done_scans:
                with self.lock:
                    item.setdefault('datasets', []).extend(done_scans)
                    self._save_queue(soon=True)
                self.log('the runner ended without reporting rep %d (%s): %d scan(s) of that rep '
                         'the script had said it saved are kept as datasets (their rep did not '
                         'finish): %s' % (state['in_rep'], how, len(done_scans),
                                          ', '.join(done_scans)), 'warn', item['uid'])
            if found:
                with self.lock:
                    item.setdefault('datasets_partial', []).extend(found)
                    self._save_queue(soon=True)
                self.log('the runner ended without reporting rep %d (%s): %d folder(s) made during '
                         'that rep are kept as interrupted - on disk and in History: %s'
                         % (state['in_rep'], how, len(found), ', '.join(found)), 'warn', item['uid'])
            elif not done_scans:
                self.log('the runner ended without reporting rep %d (%s): no #NNN_ folder appeared '
                         'under %s during that rep (a script that saves somewhere else is not seen '
                         'here)' % (state['in_rep'], how, P.data_root(self.cfg).replace('\\', '/')),
                         'warn', item['uid'])

        # the runner stopped after a rep for "Next block after this rep" - the block is complete
        # with the reps done (or a runner that ended cleanly short of its reps with it armed)
        with self.lock:
            nx_now = self._next_after_rep if (self._next_after_rep or {}).get('uid') == item['uid'] else None
        nx = (state.get('next') if state['outcome'] == 'paused'
              else nx_now if (rc == 0 and state['outcome'] != 'error' and not state['done']
                              and state['reps_done'] < reps
                              and not (self._abort.is_set() or self._skip.is_set())) else None)
        if nx and 0 < state['reps_done'] < reps:
            return self._end_early(item, nx, state['reps_done'], reps, rc)
        if state['outcome'] == 'paused' or (self._pause_after_rep and rc == 0 and state['reps_done'] < reps
                                            and state['outcome'] != 'error'):
            left = max(0, reps - state['reps_done'])
            with self.lock:
                item['progress'] = {'done': state['reps_done'], 'total': reps, 'running': 0}
                item['message'] = 'paused with %d of %d rep(s) left' % (left, reps)
                self._pause_after_rep = False
                self._touch_pause_file(False)
            self.log('paused after rep %d of %d; %d left for the next start'
                     % (state['reps_done'], reps, left), 'warn', item['uid'])
            return 'paused'
        complete = (state['outcome'] != 'error' and state['reps_done'] >= reps
                    and (rc == 0 or state['done']))
        if complete and (self._abort.is_set() or self._skip.is_set()):
            # The Stop or Skip came after the last rep had finished, while the runner was only
            # exiting: the block is complete. Thrown away, a Stop re-measured it from rep 1
            # on Resume and a Skip dropped a good result (blind review #115).
            self.log('the %s came after the last rep had finished - the block is complete and '
                     'kept' % ('Stop' if self._abort.is_set() else 'Skip'), 'warn', item['uid'])
            if self._skip.is_set() and not self._abort.is_set() and rc != 0:
                # the kill landed on its way out: the outputs get their safe stop regardless
                res = self.run_safe_stop(reason='skip')
                if not res.get('ok'):
                    with self.lock:
                        item['message'] = 'the safe stop after the skip failed (%s)' % (
                            res.get('error') or 'non-zero exit')
                        self._refine_fault(item['message'])
                    return 'error'
            return 'ok'
        if self._abort.is_set():
            return 'stopped'
        if self._skip.is_set():
            # skipping is a kill, see stop(); a safe stop that fails after it is a fault,
            # not a footnote - the next block must not start on outputs still on (F12)
            res = self.run_safe_stop(reason='skip')
            if not res.get('ok'):
                with self.lock:
                    item['message'] = 'skipped, but the safe stop after it failed (%s)' % (
                        res.get('error') or 'non-zero exit')
                    self._refine_fault(item['message'])
                return 'error'
            return 'skipped'
        if rc == 0 and state['outcome'] != 'error':
            return 'ok'
        item['message'] = item.get('message') or 'exit code %s' % rc
        self.log('block failed: %s' % item['message'], 'error', item['uid'])
        res = self.run_safe_stop(reason='failure')                      # the outputs may be left on
        if not res.get('ok'):
            with self.lock:
                self._refine_fault('the safe stop after the failed block "%s" failed (%s)'
                                   % (item['name'], res.get('error') or 'non-zero exit'))
        return 'error'

    def _end_early(self, item, nx, done, reps, rc=0):
        """The runner stopped cleanly after rep `done` of `reps` for "Next block after this rep".
        The block is COMPLETE with the reps done: 'ok' - the worker marks it done and goes on to
        the next block without pausing. `reps` (the plan, params.reps) is never rewritten; the
        early end is recorded as item['ended_early'] = {reps_done, of, by, at, asked_at[, late]} -
        on the card, in History's row, and what a restart reads (_run_script: it is then
        complete)."""
        uid = item['uid']
        with self.lock:
            # a Stop or Skip that landed after the runner had stopped cleanly for Next - the block
            # stays complete (#115), and a Stop halts the queue: "goes on without pausing" would
            # have been false
            late = 'Stop' if self._abort.is_set() else 'Skip' if self._skip.is_set() else None
            item['progress'] = {'done': done, 'total': reps, 'running': 0}
            item['ended_early'] = dict({'reps_done': int(done), 'of': int(reps), 'by': nx.get('by') or 'you',
                                        'at': time.time(), 'asked_at': nx.get('at')},
                                       **({'late': late} if late else {}))
            # the flag no longer naming this block means it was taken back (a second press, or a
            # pause pressed after it) AFTER the runner had read the pause file - the take-back's
            # own log line said "runs all its reps", which this line has to correct
            late_back = (self._next_after_rep or {}).get('uid') != uid
            if not late_back:
                self._next_after_rep = None
            if not self._pause_after_rep:
                self._touch_pause_file(False)
            pausing = self._pause_after_rep or self._pause_after_block
            trial = bool(self._trial)
            item['message'] = ('ended early after rep %d of %d ("Next block after this rep") - complete with %d '
                               'rep(s)' % (done, reps, done))
            self._save_queue()
        if late:
            then = ('the Stop that came as it ended stops the queue' if late == 'Stop'
                    else 'the trial ends here' if trial
                    else 'the Skip that came as it ended changes nothing more: the queue goes on to the next block')
        else:
            then = ('the trial ends here' if trial
                    else 'a pause pressed since then comes next' if pausing
                    else 'the queue goes on to the next block without pausing')
        self.log('ended early after rep %d of %d ("Next block after this rep", %s): "%s" is complete with the %d rep(s) '
                 'done; %s%s'
                 % (done, reps, 'asked by the agent' if nx.get('by') == 'agent' else 'pressed by you', item['name'], done,
                    then,
                    (' (the take-back came after the runner had already stopped after rep %d)' % done)
                    if late_back else ''), 'warn', uid)
        if late == 'Skip' and rc not in (0, None):
            # as #115: a Skip's kill that landed on the runner's way out - the outputs get their safe stop regardless
            res = self.run_safe_stop(reason='skip')
            if not res.get('ok'):
                with self.lock:
                    item['message'] = 'the safe stop after the skip failed (%s)' % (res.get('error') or 'non-zero exit')
                    self._refine_fault(item['message'])
                return 'error'
        return 'ok'

    def _rep_folders_left(self, item, state):
        """The #NNN_* folders under paths.data_root made during the rep a runner ended in
        without reporting it (killed after the grace, killed from outside, ended in the middle):
        what the runner's own Ctrl-Break handler would have listed. Only a folder both created
        and last changed after the rep began, as the runner counts them (#118), and none the
        block already lists. The block's record used to say only "may be on disk" (#100).
        The folders the runner announced for that rep (its `folder` events) come first: it
        knows what was on disk before the block; the look here covers one it had no time to
        announce."""
        known = set()
        for p in list(item.get('datasets') or []) + list(item.get('datasets_partial') or []):
            if isinstance(p, str) and p:
                known.add(os.path.normcase(os.path.normpath(P.resolve(p))))
        said = []
        for p in (state.get('folders') or {}).get(state.get('in_rep'), []):
            k = os.path.normcase(os.path.normpath(P.resolve(p)))
            if k not in known:
                known.add(k)
                said.append(p)
        t0, m0 = state.get('rep_t0'), state.get('rep_m0')
        if t0 is None or m0 is None:
            return said
        # the rep's start as file times see it, also across a wall-clock step (as the runner)
        since = min(t0, time.time() - (time.monotonic() - m0)) - 3.0
        found = []
        try:
            days = [e for e in os.scandir(P.data_root(self.cfg))
                    if re.match(r'^\d{4}-\d{2}-\d{2}$', e.name) and e.is_dir()]
        except OSError:
            return said                     # what the runner told is still known
        for day in days:
            try:
                with os.scandir(day.path) as entries:
                    for e in entries:
                        if not (e.name.startswith('#') and e.is_dir()):
                            continue
                        st = e.stat()
                        created = getattr(st, 'st_birthtime', None) or st.st_ctime
                        if created < since or st.st_mtime < since:
                            continue
                        p = os.path.normpath(os.path.abspath(e.path))
                        if os.path.normcase(p) not in known:
                            found.append((created, p.replace('\\', '/')))
            except OSError:
                continue
        return said + [p for _c, p in sorted(found)]

    def _script_cautions(self, item, target, overrides, reps):
        """Things worth saying before a script runs that are not reasons to refuse.

          * a script with no main() runs at import, and some loop their own `reps` at
            module level (`reps = 100`). The block's reps MULTIPLY that, and
            the panel's pause between reps cannot see inside the script's loop (F06) - and so
            does a main() that does not take its REPS as an argument (#027);
          * an edit to a line inside an `if` decided at run time takes effect only when
            that branch runs (F20);
          * what a measurement class does with a value: keys it reads only under a flag, or
            recomputes (panel_scan.cautions and its _CLASS_RULES, #014 #015 #103).
        """
        out = []
        try:
            info = panel_scan.scan(target)
        except Exception:                                              # noqa: BLE001
            return out
        if info.get('error'):
            return out
        ov = overrides or {}
        # any case: a script's own rep constant is REPS (blind review #027)
        reps_names = ('reps', 'numreps', 'num_reps', 'nreps', 'n_reps')

        def reps_like(p):
            return (p.get('effective') is True and str(p.get('name') or '').lower() in reps_names
                    and isinstance(p.get('value'), (int, float))
                    and not isinstance(p.get('value'), bool) and p['value'] > 1
                    and p['key'] not in ov and p['name'] not in ov)
        if not info.get('has_main'):
            for p in info.get('params') or []:
                if p.get('scope') == 'module' and reps_like(p):
                    out.append('%s has no main(): it runs at import and loops `%s = %g` itself. '
                               'This block runs it %d time(s), so that is %d scan(s) in all, and '
                               'a pause between reps cannot stop it inside its own loop. Set the '
                               'block to 1 rep, or override `%s` in the script.'
                               % (os.path.basename(target), p['name'], p['value'], reps,
                                  int(p['value']) * int(reps), p['name']))
            # A loop with no reps-like name: `for i in range(100):` around a whole scan made 4
            # reps 400 scans with no word (blind review #036). A
            # name the check above looks at, or one being overridden, is left to that.
            for lp in _top_loops(target):
                names = lp.get('names') or []
                if any(str(n).lower() in reps_names
                       or any(k == n or str(k).startswith(n + '@') for k in ov) for n in names):
                    continue
                base = os.path.basename(target)
                if lp.get('count') is None:
                    out.append('%s has no main() and runs `%s:` at line %d: each rep runs until '
                               'the file ends that loop itself, and a pause between reps cannot '
                               'reach inside it - only Stop can.' % (base, lp['text'], lp['line']))
                elif lp['count'] > 1:
                    out.append('%s has no main(): it runs at import and repeats `%s` (line %d) '
                               '%d times itself. This block runs it %d time(s), so that is %d '
                               'passes in all, and a pause between reps cannot stop it inside its '
                               'own loop. Set the block to 1 rep, or change that loop in the script.'
                               % (base, lp['text'], lp['line'], lp['count'], reps,
                                  lp['count'] * int(reps)))
        else:
            # With a main() the runner sets a reps ARGUMENT of it to 1 - not a constant main()
            # does not take. A main() that takes nothing and a run_scan() that does
            # `for rep in range(REPS)`, REPS = 10: one block rep was ten sweeps (~2.2 h) with no
            # word, Pause could not act inside them, and a Stop mid-sweep left that sweep's
            # points out of its .dat (blind review #027)
            sig = _main_sig(target) or {}
            takes = set(sig.get('names') or [])
            for p in info.get('params') or []:
                if p.get('scope') in ('module', 'main') and reps_like(p) and p['name'] not in takes:
                    out.append('%s loops `%s = %g` itself (line %s), and main() does not take it '
                               'as an argument, so the panel cannot set it to 1: each rep of this '
                               'block is %g passes of the script\'s own loop - %d in all for %d '
                               'rep(s). A pause between reps cannot act inside them (only Stop '
                               'can), and whatever the script saves only after a whole pass is '
                               'not saved for the pass a Stop cuts short. Change `%s` under '
                               'Script parameters to change it.'
                               % (os.path.basename(target), p['name'], p['value'], p.get('line'),
                                  p['value'], int(p['value']) * int(reps), reps, p['name']))
        # what a measurement class does with a value (#014, #015) is panel_scan.cautions()'s,
        # below: it reads the class's source, applies the block's own flags, and the picker
        # marks the same rows. Said once, not twice.
        # a script that waits for the keyboard: a panel block has none. input() now gets
        # end-of-file at once (the runner), so it ends there - maybe with no data - and a
        # console getch() cannot be interrupted at all (blind review #110, #026)
        keys = _keyboard_reads(target)
        if keys:
            base = os.path.basename(target)
            getch = [w for _l, w in keys if w.startswith('msvcrt')]
            out.append('%s waits for the keyboard (%s): a panel block has no keyboard. %s'
                       % (base, ', '.join('%s at line %d' % (w, l) for l, w in keys[:3]),
                          'A console key read never returns - only a kill ends it.' if getch else
                          'input() gets end-of-file at once, so the script may end right there '
                          'with no data.'))
        for note in panel_scan.cautions(target, ov):
            out.append(note)
        return out

    # ------------------------------------------------------------------ the runner process

    def _spawn(self, cmd, uid, on_event=None, env=None, stop_file=None):
        """Run one child to its end, its output into the log and its ##PANEL events to
        `on_event`. `stop_file`: the child is a runner that takes --stop-file - it gets a
        hidden console of its own and is asked to stop through that file (see _kill_child).
        Taken from the command line when not given: a runner's cmd carries --stop-file."""
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
        if stop_file is None and '--stop-file' in cmd[:-1]:
            stop_file = cmd[cmd.index('--stop-file') + 1]
        if stop_file:
            if os.name == 'nt':
                # Its own hidden console: in the panel's, closing that window killed the runner
                # at the same instant as the panel, before its finally: (outputs off) or its
                # watchdog could run (blind review #024). A Ctrl-Break cannot reach another
                # console, so a stop is asked for through the stop file.
                flags |= getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
            try:
                os.makedirs(os.path.dirname(stop_file), exist_ok=True)
                if os.path.exists(stop_file):
                    os.remove(stop_file)             # one left by a killed run is not a stop now
            except OSError as e:
                self.log('could not prepare the stop request file %s: %s' % (stop_file, e),
                         'warn', uid)
        while True:
            # a runner drives the instruments: a safe stop someone else started since the
            # worker looked (N35, I04) is waited for, and looked at again in the same locked
            # step as the Popen
            if stop_file and not self._wait_safe_stops():
                self.log('not started: a safe stop did not finish within %g s'
                         % (self._ss_timeout() + 60.0), 'error', uid)
                return -1
            with self.lock:
                # Checked, started and published in one piece, under the lock stop() and
                # skip_block() set their flag under: either they see this child and kill it, or
                # it is never started. A Stop that landed while the block was being prepared
                # used to kill nothing, run the safe stop, and let the runner start after it and
                # run every rep while the panel said "paused" (blind review #001).
                flag = self._flag_outcome()
                if flag:
                    self.log('not started: %s' % ('Stop was pressed' if flag == 'stopped'
                                                  else 'Skip was pressed'), 'warn', uid)
                    return -1
                if stop_file and self._pb_claimed():
                    continue                    # one began after the wait: wait for it too
                t_spawn = time.time()
                try:
                    # stdin from nowhere: a script that asks for input() gets EOF at once instead
                    # of waiting all night on the panel's console (blind review #110)
                    proc = subprocess.Popen(cmd, cwd=P.work_dir(self.cfg), stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            bufsize=1, text=True, encoding='utf-8', errors='replace',
                                            creationflags=flags,
                                            env=dict(os.environ, **dict(env or {})))
                except (OSError, ValueError) as e:
                    self.log('cannot start the runner: %s' % e, 'error', uid)
                    return -1
                proc._panel_stop_file = stop_file         # how _kill_child asks it to stop
                self._proc = proc
                break
        drained = threading.Event()
        detached = []                    # set once the worker has stopped waiting for the pipe
        heard = [time.monotonic()]       # the last line or event from the child

        def pump():
            try:
                for line in proc.stdout:
                    heard[0] = time.monotonic()
                    line = line.rstrip('\r\n')
                    # an event anywhere in the line: a DLL's printf can still glue an unfinished
                    # line in front of one (blind review #028)
                    text, js = _panel_event(line)
                    if js is not None:
                        if text.strip():
                            self.log(text, 'out', uid)
                        if detached:
                            continue
                        try:
                            ev = json.loads(js)
                        except ValueError:
                            self.log('runner: a ##PANEL line could not be read, so what it '
                                     'reported is lost: %s' % line[:160], 'warn', uid)
                            continue
                        if not isinstance(ev, dict):
                            self.log('runner: ignored a ##PANEL line that is not an object: %s'
                                     % line[:120], 'warn', uid)
                            continue
                        if on_event:
                            try:
                                on_event(ev)
                            except Exception as exc:               # noqa: BLE001
                                # a bad event must not stop this loop: a child whose stdout is
                                # no longer read blocks on a full pipe, and this call never
                                # returned (agent A, finding A1)
                                self.log('runner event %s could not be handled: %s: %s'
                                         % (js[:120], type(exc).__name__, exc), 'warn', uid)
                        continue
                    if line.strip():
                        self.log(line, 'out', uid)
            except (OSError, ValueError):
                pass
            finally:
                drained.set()

        reader = threading.Thread(target=pump, name='panel-runner-output', daemon=True)
        reader.start()
        stuck = False
        try:
            rc, stalled = self._wait_child(proc, uid, heard)
            # The runner has exited; its output is normally drained within moments. Something
            # it started that still holds the pipe - a grandchild that ignored Ctrl-Break or
            # is wedged in a driver - kept the worker here for good, with the queue wedged and
            # the grandchild still measuring (blind review #048).
            if not drained.wait(5.0):
                killed = self._kill_orphans_of(proc.pid, t_spawn, uid)
                if not drained.wait(10.0):
                    stuck = True
                    detached.append(True)
                    self.log('something the runner started is still holding its output after '
                             'the runner ended%s - no longer waiting for it'
                             % (' (killed: pid %s)' % ', '.join(map(str, killed)) if killed else ''),
                             'error', uid)
                elif killed:
                    stuck = True             # it was still running after the block: not a clean end
            if stalled:
                stuck = True                 # stopped by the stall guard: a failure, safe stop after
        finally:
            with self.lock:
                if self._proc is proc:
                    self._proc = None
        if stuck:
            # not a clean end, whatever the exit code said: the caller's failure path runs
            # the safe stop after it
            return rc if rc else -1
        return rc

    def _wait_child(self, proc, uid, heard):
        """(exit code, why it was stopped or None). The stall guard (blind review #026): a
        block that prints nothing and reports nothing for run.stall_warn_minutes (120 by
        default) gets a warning on its card and in the log - a blocking read the runner
        cannot intercept (a DLL wait, a console getch) otherwise sat there all night with
        nobody told. With run.stall_stop_minutes above 0 it is also stopped then, as a failed
        block (the safe stop runs after it). Off by default: some reps are long and quiet."""
        run = self._run_cfg()
        warn_s = max(0.0, _num(run.get('stall_warn_minutes'), 120)) * 60.0
        stop_s = max(0.0, _num(run.get('stall_stop_minutes'), 0)) * 60.0
        warned, stalled, warned_at = False, None, 0.0
        step = 5.0 if min([s for s in (warn_s, stop_s) if s > 0] or [600.0]) >= 60.0 else 0.2
        while True:
            try:
                rc = proc.wait(timeout=step)
                if warned and stalled is None and heard[0] > warned_at:
                    # it spoke again, and then ended, before the next look
                    self.log('the block is producing output again', 'info', uid)
                    self._item_note(uid, 'stall', None)
                return rc, stalled
            except subprocess.TimeoutExpired:
                pass
            quiet = time.monotonic() - heard[0]
            if warn_s and quiet >= warn_s and not warned:
                warned, warned_at = True, time.monotonic()
                note = ('no output and no event from this block for %s - it may be stuck (a '
                        'blocking read the runner cannot interrupt?). Look at it, or Skip it'
                        % _hms(quiet))
                self.log(note, 'warn', uid)
                self._item_note(uid, 'stall', note)
            elif warned and quiet < warn_s:
                warned = False
                self.log('the block is producing output again', 'info', uid)
                self._item_note(uid, 'stall', None)
            if stop_s and quiet >= stop_s and stalled is None:
                stalled = ('stopped: no output and no event for %s (run.stall_stop_minutes = %g)'
                           % (_hms(quiet), stop_s / 60.0))
                self.log(stalled, 'error', uid)
                self._item_note(uid, 'stall', stalled)
                with self.lock:
                    it = next((i for i in self.seq['items'] if i['uid'] == uid), None)
                    if it is not None:
                        it['message'] = stalled
                threading.Thread(target=self._kill_child, args=(proc,), daemon=True).start()

    def _item_note(self, uid, key, text):
        """A line for the block's card: `stall` (the guard above) or a runner note (C6)."""
        with self.lock:
            it = next((i for i in self.seq['items'] if i['uid'] == uid), None)
            if it is None:
                return
            if key == 'runner_notes':
                notes = [n for n in (it.get('runner_notes') or []) if n != text]
                it['runner_notes'] = (notes + [text])[-6:]
            elif text is None:
                it.pop(key, None)
            else:
                it[key] = text

    def _kill_orphans_of(self, ppid, since, uid=None, who="the block's runner"):
        """Kill what a runner that has already exited left behind: the processes whose parent
        was that runner and that started after it did (the PID alone may be reused). `who`
        names it in the log line (an analysis run from History uses this too, 0.1.7)."""
        if os.name != 'nt':
            return []
        ps = ("Get-CimInstance Win32_Process -Filter \"ParentProcessId=%d\" | ForEach-Object "
              "{ \"$($_.ProcessId)`t$(((Get-Date) - $_.CreationDate).TotalSeconds)\" }" % int(ppid))
        try:
            out = subprocess.run(['powershell', '-NoProfile', '-Command', _PS_UTF8 + ps],
                                 capture_output=True, text=True, encoding='utf-8',
                                 errors='replace', timeout=20, stdin=subprocess.DEVNULL).stdout
        except (OSError, subprocess.SubprocessError, ValueError):
            return []
        age_max = time.time() - since + 2.0
        killed = []
        for line in (out or '').splitlines():
            pid, _, age = line.partition('\t')
            try:
                pid, age = int(pid.strip()), float(age.strip().replace(',', '.'))
            except ValueError:
                continue
            if age > age_max:
                continue                     # older than the runner: a reused PID, not its child
            self.log('killing pid %d, left running by %s' % (pid, who), 'warn', uid)
            try:
                subprocess.run(['taskkill', '/T', '/F', '/PID', str(pid)], capture_output=True,
                               timeout=15, stdin=subprocess.DEVNULL)
                killed.append(pid)
            except (OSError, subprocess.SubprocessError):
                pass
        return killed

    def _kill_child(self, proc=None):
        """Ctrl-Break, the grace to clean up, then the whole process tree. `proc` pins the
        child to kill; by default whatever runs now."""
        if proc is None:
            with self.lock:
                proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        grace = _num(self._run_cfg().get('grace_seconds'), 10)
        sf = getattr(proc, '_panel_stop_file', None)
        asked = False
        if sf:
            # a runner in a console of its own: a Ctrl-Break cannot reach it, the stop file
            # does, and it raises the same KeyboardInterrupt there (blind review #024)
            try:
                with open(sf, 'w', encoding='utf-8') as f:
                    f.write(str(time.time()))
                asked = True
            except OSError as e:
                self.log('could not write the stop request %s (%s) - it is killed after %g s'
                         % (sf, e, grace), 'warn')
        if not asked or os.name != 'nt':
            try:
                if os.name == 'nt':
                    proc.send_signal(signal.CTRL_BREAK_EVENT)           # KeyboardInterrupt in the child
                else:
                    proc.send_signal(signal.SIGINT)
            except (OSError, ValueError) as e:
                # no console to deliver it through, most likely: the child gets no chance to
                # clean up, so it says so rather than swallowing it (blind review #048)
                self.log('could not send Ctrl-Break to the child (%s) - it is killed after %g s'
                         % (e, grace), 'warn')
        # the grace on the monotonic clock: a wall-clock step cut it short, or held the hard
        # kill back for as long as the step (#049)
        t0 = time.monotonic()
        while time.monotonic() - t0 < grace and proc.poll() is None:
            time.sleep(0.2)
        if proc.poll() is None:
            self.log('child did not stop in %gs - killing it and everything it started' % grace,
                     'warn')
            self._kill_tree(proc)

    def _kill_tree(self, proc):
        """The runner AND what it started. proc.kill() ended run_block.py alone, and a
        measurement it had launched in a subprocess went on after STOP (blind review #048)."""
        if os.name == 'nt':
            try:
                subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], capture_output=True,
                               timeout=15, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            if proc.poll() is None:
                proc.kill()
        except OSError:
            pass

    # ------------------------------------------------------------------ status for the UI

    # what a segment's end is called in History when the block itself goes back to 'pending':
    # a stopped or paused segment used to read as a bare 'pending' (blind review #009)
    _ROW_STATUS = {'paused': 'paused', 'stopped': 'stopped', 'interrupted': 'interrupted'}

    def _record_history(self, item, pos, ds_from=0, outcome=None, trial=None):
        """One row per finished block - per SEGMENT of it, when a block was paused and
        resumed. This is the raw material for History: which measurement ran, when, with what
        values, and which datasets it wrote.

        The parameters come from what was actually handed to the runner (item['exec']),
        not from the editable block: an edit made while the block ran used to be recorded
        as if the script had used it (F19). And each row lists only the datasets its own
        segment added, so a dataset is counted once however often the block paused (F22) -
        the interrupted folders too (#016). The segment's start is kept in the item
        (seg_ds_from), so a block that started again from rep 1 part way through still
        lists only this segment's datasets.
        """
        try:
            ex = item.get('exec') or {}
            all_ds = list(item.get('datasets') or [])
            all_p = list(item.get('datasets_partial') or [])
            ds_from = int(item.get('seg_ds_from', ds_from) or 0)
            dsp_from = int(item.get('seg_dsp_from', 0) or 0)
            trial = bool(self._trial) if trial is None else bool(trial)
            pr = item.get('progress') if isinstance(item.get('progress'), dict) else {}
            row = {
                'id': _uid('h'),
                'run_id': self.run_id or 'run_?',
                'run_name': ('trial: %s' % item.get('name')) if trial
                            else (self.seq.get('name') or ''),
                'trial': trial,
                'n': pos,
                'uid': item.get('uid'),
                'segment': int(item.get('segment') or 1),
                'name': item.get('name'),
                'kind': item.get('kind'),
                # the script itself; the patched copy it ran is its own field, kept per
                # segment - the copy under runs/<uid>/ is rewritten by the next run (#111)
                'target': ex.get('source_target') or item.get('target') or ex.get('target') or '',
                'patched_copy': ex.get('patched_copy') or '',
                'params': copy.deepcopy(ex.get('params') if 'params' in ex else (item.get('params') or {})),
                'overrides': copy.deepcopy(ex.get('overrides') if 'overrides' in ex
                                           else (item.get('overrides') or {})),
                'status': self._ROW_STATUS.get(outcome) or item.get('status'),
                'outcome': outcome or item.get('status'),
                'message': item.get('message') or '',
                'started': item.get('started'),
                'finished': item.get('finished'),
                'datasets': all_ds[ds_from:],
                'datasets_before': ds_from,
                # folders a killed or failed rep left behind: on disk, not finished
                'datasets_partial': all_p[dsp_from:],
                'by': item.get('by', 'you'),
                'sim': bool(self.cfg.get('sim')),
            }
            if ex.get('params_ignored'):
                row['params_ignored'] = list(ex['params_ignored'])   # never reached main() (#030)
            if (item.get('kind') or 'script') == 'script' and pr.get('total'):
                row['reps_done'] = int(_num(pr.get('done'), 0))
                row['reps_total'] = int(_num(pr.get('total'), 0))
                if ex.get('start_rep'):
                    row['start_rep'] = ex['start_rep']
                if isinstance(item.get('ended_early'), dict) and outcome in (None, 'ok'):
                    # ended early by "Next block after this rep" - complete with reps_done of the
                    # plan's reps (status done; reps_done / reps_total say the same numbers)
                    row['ended_early'] = copy.deepcopy(item['ended_early'])
                if _as_number(item.get('seg_rep_from')) is not None:
                    # what this segment measured itself: 0 for one that measured nothing (every
                    # rep was measured before) or was refused - its duration is no rep's (U30)
                    row['reps_run'] = max(0, row['reps_done'] - int(_as_number(item['seg_rep_from'])))
            with self.lock:
                self._history_add(row)             # the rows it pushes out are archived (#090)
        except (OSError, TypeError, ValueError) as e:      # history must never break a run
            self.log('could not record history: %s' % e, 'warn')
            self._df_nudge()
            return
        self._write_history()                              # never raises (#007)
        self._df_nudge()

    def _df_nudge(self):
        """A block ended and may have written datasets: History's data-folder index scans again
        soon. Never under the engine lock - the worker records History while it holds it, so
        the nudge then waits for its next step outside the lock (_df_flush_nudge: the top of
        its loop, its end, or the next poll). Never waits, never raises (a stand-in without df
        too)."""
        if getattr(self, 'df', None) is None:
            return
        self._df_nudge_due = True
        self._df_flush_nudge()

    def _df_flush_nudge(self):
        """The data-folder index asked to scan again, if a block end asked for it and the engine
        lock is not held here. Never raises."""
        if not self.__dict__.get('_df_nudge_due'):
            return
        owned = getattr(self.lock, '_is_owned', None)
        if owned is not None and owned():
            return
        self._df_nudge_due = False
        df = getattr(self, 'df', None)
        if df is not None:
            try:
                df.index_request()
            except Exception:                                           # noqa: BLE001
                pass

    def get_history(self, limit=60, measured=False):
        """Finished blocks grouped into runs (batches), newest run first. `measured` is kept
        for callers of the older signature; every run is a measured one."""
        with self.lock:
            rows = list(self.history)
        runs, order = {}, []
        for r in rows:
            rid = r.get('run_id') or 'run_?'
            if rid not in runs:
                runs[rid] = {'run_id': rid, 'name': r.get('run_name') or '', 'blocks': [],
                             'started': r.get('started'), 'finished': r.get('finished'),
                             'sim': r.get('sim')}
                order.append(rid)
            g = runs[rid]
            g['blocks'].append(r)
            if r.get('started') and (not g['started'] or r['started'] < g['started']):
                g['started'] = r['started']
            if r.get('finished') and (not g['finished'] or r['finished'] > g['finished']):
                g['finished'] = r['finished']
        out = []
        for rid in reversed(order):
            g = runs[rid]
            # a block paused and resumed is several rows and ONE block, and a dataset
            # listed by two rows is one dataset (old rows repeated the whole list)
            g['n_blocks'] = len({b.get('uid') or b['id'] for b in g['blocks']})
            g['n_error'] = sum(1 for b in g['blocks'] if b['status'] == 'error')
            g['n_datasets'] = len({d for b in g['blocks'] for d in (b.get('datasets') or []) if d})
            out.append(g)
        return out[:limit]

    def paths_view(self):
        """The panel's own folders, whole, as Windows writes them (round 4, P3: Settings shows them, a click opens
        them): {data_root, state_dir, logs_dir, history_export_dir}. Read at call time (a test or the sandbox moves
        P.STATE_DIR); history_export_dir is where "Export manual groups" writes - the configured folder, else the
        default under the state folder ('' when the configured value cannot be used: Settings says why)."""
        try:
            from . import history_export as _hx
            exp = _hx.export_dir(self.cfg)[0] or ''
        except Exception:                                               # noqa: BLE001
            exp = ''
        return {'data_root': os.path.normpath(P.data_root(self.cfg)), 'state_dir': os.path.normpath(P.STATE_DIR),
                'logs_dir': os.path.normpath(P.LOG_DIR), 'history_export_dir': os.path.normpath(exp) if exp else ''}

    def history_version(self):
        """A token that changes whenever History does - a row added, rows archived, a Clear -
        and differs between two sessions of the panel. The snapshot carries it, so the page
        fetches History only when it has changed (#088)."""
        # a per-instance random tail, not the millisecond alone: two sessions (or two engines in a
        # test) born in the same millisecond would otherwise share a token, and the page could take
        # one session's History for the other's (#088). os.urandom keeps them apart across processes.
        born = self.__dict__.setdefault('_h_born',
                                        '%x-%s' % (int(time.time() * 1000), os.urandom(4).hex()))
        return '%s.%d' % (born, self.__dict__.get('_h_ver', 0))

    def history_view(self, limit=60, version=None, measured=False):
        """get_history for the page: {'version', 'runs'}, or {'version', 'unchanged': True}
        when `version` is the one the page already has. The History tab used to download
        every row of the newest 60 runs every 8 s - up to 3000 rows, megabytes - for a list
        that changes when a block ends (blind review #088)."""
        with self.lock:
            v = self.history_version()
        if version is not None and str(version) == v:
            return {'version': v, 'unchanged': True}
        return {'version': v, 'runs': self.get_history(limit, measured=bool(measured))}

    def clear_history(self):
        with self.lock:
            self._h_ver = self.__dict__.get('_h_ver', 0) + 1
            self.history = []
        self._write_history()

    # ------------------------------------------------------------------ History's data folder

    # Lock order (panel_datafolder, panel_dataindex): a store lock or the index's scan lock is
    # never taken while the engine lock is held - snapshot() reads df.versions(), which takes
    # none - and the data folder's code never holds one of its own locks while it calls
    # self.log.

    def df_start(self):
        """History's data folder: its stores loaded and the index's background thread started
        (panel_server.main, once the panel is up). An in-process test engine never starts it
        unless it calls this. True when started; never raises."""
        # 0.1.7: the folder of your own analysis scripts (my_analysis.dirs[0]) made, with its README, if it is not
        # there yet - started here, with the data folder, so an in-process test engine never writes it unless it
        # starts the data folder. Never raises; nothing there is ever replaced.
        try:
            _myscripts().ensure_default_dir(self.cfg, self.log)
        except Exception as e:                                          # noqa: BLE001
            self.log('my analysis: %s' % e, 'warn')
        df = getattr(self, 'df', None)
        if df is None:
            return False
        try:
            df.start()
            return True
        except Exception as e:                                          # noqa: BLE001
            self.log('History data folder could not be started: %s: %s' % (type(e).__name__, e),
                     'error')
            return False

    def df_stop(self):
        """The panel is shutting down: the index's thread is told to stop, never waited for."""
        idx = getattr(getattr(self, 'df', None), 'index', None)
        try:
            stop = getattr(idx, 'stop', None)
            if stop is not None:
                stop()
        except Exception:                                               # noqa: BLE001
            pass

    def _df_record_job(self, job):
        """After an analysis job has ended: every item run on History groups (dfgroup / dfgroups) that got as far as
        running, and has no record yet, is recorded whatever its end - one analysis record per group
        (df.record_analysis; a combined run over several groups is recorded once per group, each card shows it,
        its `asked` that group's own datasets) and ONE History row per item in the day's analysis run (run_id
        'an_YYYYMMDD'), so the group's card shows it and By run lists it. Items without a group are recorded
        nowhere. Outside the engine lock but for the row itself; the number recorded; never raises."""
        try:
            df = getattr(self, 'df', None)
            items = list((job or {}).get('items') or []) if isinstance(job, dict) else []
        except Exception:                                               # noqa: BLE001
            return 0
        if df is None:
            return 0
        n = 0
        for k, it in enumerate(items, 1):
            try:
                if not isinstance(it, dict) or it.get('record') or not it.get('started'):
                    continue                 # cancelled before it started: nothing ran
                toks = [t for t in (it.get('dfgroups') or []) if isinstance(t, str) and t]
                if not toks and it.get('dfgroup'):
                    toks = [it['dfgroup']]
                if not toks:
                    continue
                hid = _uid('h')
                with self.lock:
                    it['history_row'] = hid  # the record names its row (a key made by analysis_run)
                recs = []
                for tok in toks:
                    try:
                        if len(toks) == 1 and tok == it.get('dfgroup'):
                            rec = df.record_analysis(job, k, it)
                        else:
                            rec = df.record_analysis(job, k, it, token=tok)
                        if not isinstance(rec, dict) or not rec.get('id'):
                            raise ValueError('the analysis record came back without an id')
                        recs.append(rec)
                    except Exception as e:                              # noqa: BLE001
                        if len(toks) == 1:
                            raise
                        self.log('analysis: item %d could not be recorded for one of its History groups (%s: %s)'
                                 % (k, type(e).__name__, e), 'warn', uid=_an_log_uid(job))
                if not recs:
                    raise ValueError('none of its %d History groups could be recorded' % len(toks))
                rec = recs[0]
                fin = _finite(it.get('finished')) or time.time()
                rid = 'an_' + time.strftime('%Y%m%d', time.localtime(fin))
                with self.lock:
                    pos = 1 + sum(1 for r in self.history
                                  if isinstance(r, dict) and r.get('run_id') == rid)
                    row = _df_history_row(self, job, k, it, rec, hid, rid, pos,
                                          **({'recs': recs} if len(recs) > 1 else {}))
                    self._history_add(row)
                    it['record'] = rec['id']
                    if 'records' in it:
                        it['records'] = [r['id'] for r in recs]   # replaced, never appended in place
                n += 1
                self.log('analysis: recorded in History - %s (%s)'
                         % (it.get('label') or row['name'], row['status']), 'info', uid=_an_log_uid(job))
            except Exception as e:                                      # noqa: BLE001
                self.log('analysis: item %d could not be recorded in History (%s: %s)'
                         % (k, type(e).__name__, e), 'warn', uid=_an_log_uid(job))
        if n:
            self._write_history()                                       # never raises (#007)
        return n

    def snapshot(self):
        # Nothing slow is called while the engine lock is held (blind review #012): every page
        # poll, every Stop and the runner's output logging wait behind it. A caller that already
        # holds the lock is given the values of the last poll instead.
        owned = getattr(self.lock, '_is_owned', None)
        if not (owned is not None and owned()):
            self._orphan_refresh()
        # History's data folder: the versions the page compares (attribute reads only - no
        # lock, no I/O), before the engine lock is taken; a caller that holds it gets the
        # last poll's (the data folder is never called under the lock)
        dfv = None
        df = getattr(self, 'df', None)
        if df is not None:
            if owned is not None and owned():
                dfv = copy.deepcopy(getattr(self, '_df_last', None))
            else:
                self._df_flush_nudge()       # a block end's nudge still waiting (a backstop)
                try:
                    dfv = df.versions()
                except Exception as exc:                                # noqa: BLE001
                    dfv = {'index': None, 'scanning': False, 'manual': None,
                           'error': '%s: %s' % (type(exc).__name__, exc)}
                self._df_last = dfv
        # the function list's revision (the page loads the list again when it changes), read
        # before the engine lock is taken; a caller that holds it gets the last poll's
        if owned is not None and owned():
            palrev = getattr(self, '_palrev_last', '')
        else:
            palrev = self._palrev_last = self.palette_rev()
        # settings that name something that is not there (a safe-stop script, the script folders): a
        # look at the disk, so before the lock and at most every 2 s; a caller that holds the lock
        # gets the last one
        setup = getattr(self, '_setup_last', None)
        if not (owned is not None and owned()) and (setup is None or time.monotonic() - setup[0] > 2.0):
            try:
                setup = (time.monotonic(), P.setup_problems(self.cfg))
            except Exception as exc:                                    # noqa: BLE001
                setup = (time.monotonic(), [{'key': 'paths', 'path': '',
                                             'text': 'the paths in config.json cannot be checked: %s' % exc}])
            self._setup_last = setup
        with self.lock:
            self._retry_saves()              # a failed or deferred save, tried again (#007)
            items = self.seq['items']
            done = sum(1 for i in items if i['status'] in ('done', 'skipped'))
            cur = next((i for i in items if i['status'] == 'running'), None)
            first_pending = next((k for k, i in enumerate(items) if i['status'] == 'pending'), None)
            cur_idx = (items.index(cur) + 1) if cur else (first_pending + 1 if first_pending is not None
                                                          else len(items))
            nxt = items[first_pending] if first_pending is not None else None
            # the time left from the monotonic deadline (#049); wait_end is what is shown
            if self.wait_end is None or _finite(self.wait_end) is None:
                left = None                  # never a status poll that fails on int(inf) (#015)
            elif self._wait_deadline is not None:
                left = self._wait_deadline - time.monotonic()
            else:
                left = self.wait_end - time.time()
            if left is not None and not math.isfinite(left):
                left = None
            # the saved sequence this one is (U37): its sig stays here, the page gets 'same'
            lf = self.seq.get('loaded_from')
            lf = ({'id': lf.get('id'), 'name': lf.get('name'), 'saved': lf.get('saved'),
                   'seq_name': lf.get('seq_name'), 'same': _seq_sig(items) == lf.get('sig')}
                  if isinstance(lf, dict) else None)
            return {
                'ts': time.time(),
                # 0.1.7: the panel PC's calendar day, the clock that names the data folder's day folders - History's
                # Today / Yesterday / Last 7 days count from it, not from the browser's own clock (another time zone,
                # a wrong clock). Additive; an older page ignores it.
                'day': time.strftime('%Y-%m-%d'),
                'sim': bool(self.cfg.get('sim')),
                'run_state': self.run_state,
                'message': self.message,
                'sequence': {'name': self.seq['name'], 'items': items, 'loaded_from': lf,
                             # what a Load that replaces it would discard (U05)
                             'at_stake': _at_stake(items, self.run_state)},
                'progress': {'index': cur_idx if items else 0, 'done': done, 'total': len(items)},
                'current': {'uid': cur['uid'], 'name': cur['name'],
                            'progress': cur.get('progress'), 'started': cur.get('started')} if cur else None,
                'next': {'name': nxt['name'], 'uid': nxt['uid']} if nxt else None,
                'wait_seconds_left': None if left is None else max(0, int(left)),
                # 0.1.7: the same time left, exact at 'ts' - the page counts it down on its own 1 s tick (the integer
                # stays for the agent and older pages)
                'wait_left_s': None if left is None else round(max(0.0, left), 3),
                # an armed pause is a promise the page has to be able to show, and undo
                'pause_after_block': bool(self._pause_after_block),
                'pause_after_rep': bool(self._pause_after_rep),
                # "Next block after this rep", armed for the block it names
                'next_after_rep': (dict(self._next_after_rep) if self._next_after_rep else None),
                # a failed safe stop after a kill; Start is refused until one succeeds
                'fault': self.fault,
                # a state file that cannot be saved (the run goes on) or was found damaged at
                # start (a copy kept): sentences for a banner, [] when all is well (#007, #047)
                'storage_problems': self._storage_problems(),
                # settings that name something that is not there: [{key, path, text}], [] when all
                # is well (P.setup_problems) - a banner on the page, a mark in Settings
                'setup_problems': copy.deepcopy(setup[1]) if setup else [],
                # a safe stop is running or about to, whoever started it (contract C1): Start
                # is refused meanwhile
                'safe_stop_running': self._ss_busy(),
                # whether a stop script is configured at all (paths.safe_stop_script): without
                # one, SAFE STOP only stops what runs
                'safe_stop_script': bool(str((self.cfg.get('paths') or {}).get('safe_stop_script') or '').strip()),
                'in_wait': self._in_wait is not None,
                # one block being tried on its own; the rest of the sequence waits
                'trial': ({'uid': self._trial['uid'], 'name': self._trial['name']}
                          if self._trial else None),
                'saved': [{k: s[k] for k in ('id', 'name', 'saved', 'n_blocks')} for s in self.saved],
                # the function list's revision - it changes when blocks.json does (a page's
                # save, a Duplicate); the page loads the list again then
                'palette_rev': palrev,
                'history_runs': len({r.get('run_id') for r in self.history}),
                'history_last': (self.history[-1].get('finished') if self.history else None),
                # History is fetched again only when this changes (#088)
                'history_version': self.history_version(),
                # History's data folder: {index, scanning, error, manual}, each fetched again
                # only when it changes; None when the data folder is not available
                'datafolder': dfv,
                'chat_unread': self.unread_count(),
                'log_seq': self.log_seq,
                # what this engine can do (the page shows a control only when it is listed)
                'features': list(FEATURES),
                # the block kinds this engine runs
                'kinds': list(KINDS),
                # the panel's folders, whole (Settings shows them; a click opens them)
                'paths': self.paths_view(),
            }

def _hms(seconds):
    seconds = int(max(0, seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return ('%dh%02dm' % (h, m)) if h else ('%dm%02ds' % (m, s)) if m else ('%ds' % s)


# ══════════════════════════════════════════════════════════════════════════════════════
#  Analysing data from History with your own scripts (0.1.7).
#
#  History picks the script first - one of your own, in my_analysis.dirs (panel_myscripts) - with a PARAMS form,
#  several groups at once (combined or each separately), run by runners/run_myscript.py under the instrument guard.
#  Separate from the sequence engine on purpose: an analysis only reads data folders and writes figures, so it can
#  run while a measurement is going on. It keeps its own child-process handle, because self._proc belongs to the
#  runner and Stop must never reach in here. The page asks for it only when the engine lists 'my_analysis'.
# ══════════════════════════════════════════════════════════════════════════════════════

FEATURES += ('my_analysis',)


def _myscripts():
    """panel_myscripts, imported when first used (a copy without it, or one that does not load, answers in words
    instead of taking the engine down). RuntimeError with the reason."""
    try:
        from . import myscripts as panel_myscripts
        return panel_myscripts
    except Exception as e:                                              # noqa: BLE001
        raise RuntimeError('the analysis scripts cannot be listed: panel_myscripts.py does not load (%s: %s)'
                           % (type(e).__name__, e))


_AN_TAIL_N = 60                 # the Scripts tab's live tail: the last 60 lines of output ...
_AN_TAIL_W = 300                # ... each cut to 300 characters
_DAY_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
# _spawn_analysis's exit code when the analysis left something running after it ended, and that had to be killed
# (or could not be, and is no longer waited for) - not a clean end, whatever the analysis's own exit code was
_AN_RC_LEFTOVER = -3
_AN_LEFTOVER_WORDS = ('something the analysis started was still running after the analysis ended - it was killed '
                      '(see the log); its result is not trusted')
# What an analysis prints goes to the panel Log under the uid of its OWN job ('analysis:<job id>', _an_log_uid) -
# with no uid, Engine.log would file it under current_uid, and a History run beside a measurement would write its
# lines into the measuring block's log. And at most _AN_LOG_LINES lines of a run reach the Log, each cut to _AN_LOG_W
# characters: one print(y.tolist()) of a long trace is one multi-MB line. Every line still goes, whole, to the run's
# own panel_output.log (_AnOutLog; not output.log, a name a script may well write itself); the live tail (on_line)
# is unchanged.
_AN_LOG_LINES = 100
_AN_LOG_W = 4000
_AN_STATE_KEEP_S = 3 * 86400    # state/analysis_jobs/ files older than this are removed when a job starts
_AN_STATE_DIRNAME = 'analysis_jobs'


def _an_state_dir():
    return os.path.join(P.STATE_DIR, _AN_STATE_DIRNAME)


def _an_remove(path):
    """Remove one file of state/analysis_jobs/ that a run no longer needs. Never raises."""
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _an_prune_state():
    """At the start of an analysis job: the files in state/analysis_jobs/ older than _AN_STATE_KEEP_S - what a run
    left when the panel was stopped in the middle of it (each run removes its own). Never raises."""
    try:
        now = time.time()
        with os.scandir(_an_state_dir()) as it:
            for e in it:
                try:
                    if e.is_file() and now - e.stat().st_mtime > _AN_STATE_KEEP_S:
                        os.remove(e.path)
                except OSError:
                    continue
    except OSError:
        pass


_AN_SPAWN_OPTS = threading.local()


class _an_spawn_opts(object):
    """`with _an_spawn_opts(cancelled=..., uid=..., job_mb=..., out_folder=..., log_lines=...):` - the options
    _spawn_analysis reads for the call made inside it, on this thread (the analysis worker runs one at a time)."""

    def __init__(self, **kw):
        self.kw, self.was = kw, None

    def __enter__(self):
        self.was = getattr(_AN_SPAWN_OPTS, 'v', None)
        _AN_SPAWN_OPTS.v = self.kw
        return self

    def __exit__(self, *exc):
        _AN_SPAWN_OPTS.v = self.was
        return False


def _an_log_uid(job):
    """The Log uid of an analysis job's lines: 'analysis:<its id>'. Never empty (Engine.log falls back to the block
    that measures for an empty one)."""
    jid = job.get('id') if isinstance(job, dict) else None
    return 'analysis:%s' % (jid or '?')


def _an_cut(text, width=_AN_LOG_W):
    """`text` cut to `width` characters, with how many were cut said."""
    text = str(text)
    if len(text) <= width:
        return text
    return text[:width] + ' ... (%d characters cut)' % (len(text) - width)


class _AnOutLog(object):
    """Every output line of one analysis run, whole, appended to <out>/panel_output.log - opened once per run, not
    once a line. Never raises: a file that cannot be written is simply not written (the Log still has the first
    lines)."""

    def __init__(self, folder):
        self.path, self.f = None, None
        if folder:
            try:
                self.path = os.path.join(folder, 'panel_output.log')
                self.f = open(self.path, 'a', encoding='utf-8', errors='replace')
            except OSError:
                self.path, self.f = None, None

    def write(self, line):
        if self.f is not None:
            try:
                self.f.write(str(line) + '\n')
            except (OSError, ValueError):
                pass

    def close(self):
        f, self.f = self.f, None
        if f is not None:
            try:
                f.close()
            except OSError:
                pass


# The keys of runners/run_myscript.py's OWN ##PANEL event (its final line, and _bail's). The script prints into the
# same stdout, so a '##PANEL {"event": "analysis", ...}' line of its own reaches the engine too: only the runner's
# last event is kept, and only these keys.
_MY_EVENT_KEYS = ('event', 'analysis', 'format', 'script', 'id', 'group', 'folder', 'verdict', 'problems',
                  'n_datasets', 'n_used', 'n_figures', 'manifest', 'results', 'exit_code')


def _my_event_into(store, ev):
    """on_event of a run of your own script: `store` becomes the runner's event `ev` - the LAST one wins (the runner
    always prints its own last), and only its own keys (_MY_EVENT_KEYS). Other events are ignored."""
    if isinstance(ev, dict) and ev.get('event') == 'analysis':
        store.clear()
        store.update({k: v for k, v in ev.items() if k in _MY_EVENT_KEYS})


def _my_out_folder(cfg, paths, name):
    """The out folder of one run of your script `name`, made: <data root>/<day>/analysis/<name>_<HH-MM-SS>, '_2',
    '_3' ... when it is there already. <day> is the nearest YYYY-MM-DD day folder above the first dataset that sits
    directly under the data root, looked for at most 3 levels up, else today. So /api/analysis/figures and History
    (analyses_using reads <day>/analysis/*/analysis.json) find it. OSError when it cannot be made."""
    root = os.path.normpath(P.data_root(cfg))
    day = None
    if paths:
        p = os.path.normpath(str(paths[0]))
        for _ in range(3):
            parent = os.path.dirname(p)
            if parent == p or os.path.normcase(p) == os.path.normcase(root):
                break                                       # a drive root, or the data root itself: nothing above
            if _DAY_RE.fullmatch(os.path.basename(parent)) and \
                    os.path.normcase(os.path.dirname(parent)) == os.path.normcase(root):
                day = os.path.basename(parent)
                break
            p = parent
    day = day or time.strftime('%Y-%m-%d')
    adir = os.path.join(root, day, 'analysis')
    os.makedirs(adir, exist_ok=True)
    stem = '%s_%s' % (name, time.strftime('%H-%M-%S'))
    for k in range(1, 1000):
        out = os.path.join(adir, stem if k == 1 else '%s_%d' % (stem, k))
        try:
            os.mkdir(out)                                   # exclusive: two runs in one second never share one
            return out
        except FileExistsError:
            continue
    raise OSError('999 folders named %s_* already' % stem)


def _df_row_name(grp):
    """'<type> <MM-DD HH:MM> (<n> reps)' of a History group. Its start is the data-folder index's wall clock
    (seconds of the PC's own clock since 1970, no time zone), so it is formatted with gmtime: localtime would shift
    it by this PC's UTC offset."""
    g = grp if isinstance(grp, dict) else {}
    tn = ' '.join(str(g.get('type_name') or g.get('type') or 'History group').split())[:40]
    st = _finite(g.get('start'))
    try:
        when = time.strftime('%m-%d %H:%M', time.gmtime(st)) if st is not None else '?'
    except (OverflowError, OSError, ValueError):
        when = '?'
    try:
        n = max(0, int(g.get('n') or 0))
    except (TypeError, ValueError):
        n = 0
    unit = 'scan' if g.get('unit') == 'scan' else 'rep'
    return '%s %s (%d %s%s)' % (tn, when, n, unit, '' if n == 1 else 's')


def _df_history_row(eng, job, k, it, rec, hid, rid, pos, recs=None):
    """The History row of one analysis item run on History groups (Engine._df_record_job): kind 'analysis' in the
    day's analysis run, no datasets and no uid, so the plan's estimates, the dataset counts and the plots of datasets
    all leave it alone. A run that failed (done, verdict 'failed') is an error here; outcome keeps the item's own
    state. ONE row per item, also when a combined run over several History groups wrote one record per group
    (`recs`, `rec` the first of them) - the row is named after the first group, '+ N more', and lists every
    record; it names the script's file (my_analysis/fit_sweep.py)."""
    res = it.get('result') if isinstance(it.get('result'), dict) else {}
    grp = rec.get('group') if isinstance(rec.get('group'), dict) else {}
    outs = rec.get('outputs') if isinstance(rec.get('outputs'), dict) else {}
    state = str(it.get('state') or rec.get('state') or '')
    verdict = rec['verdict'] if 'verdict' in rec else res.get('verdict')
    probs = rec.get('problems') if isinstance(rec.get('problems'), list) \
        else (res.get('problems') if isinstance(res.get('problems'), list) else [])
    probs = [str(p) for p in probs][:10]
    fit_failed = state == 'done' and verdict == 'failed'
    msg = (it.get('error') or rec.get('error') or (probs[0] if probs else '')
           or ('the script said its result failed' if fit_failed else ''))
    module = str(rec.get('analysis') or it.get('analysis') or '')
    asked = rec['asked'] if isinstance(rec.get('asked'), list) else list(it.get('paths') or [])
    cfg = eng.cfg if isinstance(getattr(eng, 'cfg', None), dict) else {}
    sf = str(it.get('script_file') or '')
    recs = [r for r in (recs or []) if isinstance(r, dict)]
    row = {
        'id': hid, 'run_id': rid, 'run_name': 'Analysis', 'trial': False, 'n': pos, 'uid': None,
        'segment': 1, 'name': _df_row_name(grp), 'kind': 'analysis',
        'target': '/'.join(sf.replace('\\', '/').split('/')[-2:]) or module,
        'patched_copy': '', 'params': {}, 'overrides': {},
        'status': 'error' if fit_failed else state, 'outcome': state,
        'message': str(msg)[:500],
        'started': it.get('started'), 'finished': it.get('finished'),
        'datasets': [], 'datasets_before': 0, 'datasets_partial': [],
        'by': 'analysis', 'sim': bool(cfg.get('sim')),
        'analysis': {'record': rec.get('id'), 'job': (job or {}).get('id'), 'module': module,
                     'kind': 'my', 'script': sf or None, 'script_id': it.get('script'),
                     'params': copy.deepcopy(it.get('params')) if isinstance(it.get('params'), dict) else {},
                     'verdict': verdict, 'problems': probs,
                     'n_figures': outs['n_figures'] if 'n_figures' in outs else res.get('n_figures'),
                     'folder': outs.get('folder'), 'n_datasets': len(asked),
                     # round 16: the (first) record's numbers, for By run
                     **({'results': copy.deepcopy(rec['results'])} if isinstance(rec.get('results'), dict) else {})},
        # start: the index's wall clock (read with getUTC* in the page, gmtime here)
        'dfgroup': {'token': grp.get('token') or it.get('dfgroup'), 'gid': grp.get('gid'),
                    'type': grp.get('type'), 'type_name': grp.get('type_name'),
                    'start': grp.get('start'), 'n': grp.get('n'), 'unit': grp.get('unit'),
                    'manual': grp.get('manual'), 'wall_clock': True},
    }
    an = row['analysis']
    if len(recs) > 1:
        row['name'] = '%s + %d more group%s' % (row['name'], len(recs) - 1, '' if len(recs) == 2 else 's')
        an['records'] = [r.get('id') for r in recs]
        an['n_datasets'] = len(it.get('paths') or [])
        an['n_groups'] = len(recs)
        row['dfgroup']['tokens'] = [((r.get('group') or {}).get('token')) for r in recs]
        # round 17: every group of a combined run, so By run's "previous run of the same script on the same data"
        # does not take a run over one group alone for one over two (the row is named after its first group)
        row['dfgroup']['gids'] = [((r.get('group') or {}).get('gid')) for r in recs]
    return row


class _ChildJob(object):
    """A Windows job object around one child process (_job_for_child): the child and anything it starts get a memory
    limit and a lower priority, and all of it ends when close() runs - or when the panel ends, since the handle
    closes with it. `note` says what was not set."""

    def __init__(self, handle=None, note=''):
        self.handle, self.note = handle, note

    def close(self):
        h, self.handle = self.handle, None
        if h:
            try:
                import ctypes
                ctypes.WinDLL('kernel32').CloseHandle(ctypes.c_void_p(h))
            except Exception:                               # noqa: BLE001
                pass

    def __bool__(self):
        return bool(self.handle)


def _job_for_child(proc, max_mb, low_priority=True, max_processes=None):
    """Put a started child in a new job object: at most max_mb of memory per process (an allocation past it fails - a
    MemoryError in Python), kill-on-close, die on an unhandled exception instead of an error dialog, and -
    low_priority - BELOW_NORMAL priority, so an analysis never slows down a measurement that runs beside it.
    max_processes: at most that many processes in the job. Returns a _ChildJob; its note says what could not be done
    (never raises). Not Windows: priority only (nice), no memory limit."""
    if os.name != 'nt':
        try:
            if low_priority:
                os.setpriority(os.PRIO_PROCESS, proc.pid, 10)
        except (AttributeError, OSError):
            pass
        return _ChildJob(None, 'no job object: not Windows')
    try:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class BASIC(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                        ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', wintypes.DWORD),
                        ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t),
                        ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t),
                        ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BASIC), ('IoInfo', IO_COUNTERS),
                        ('ProcessMemoryLimit', ctypes.c_size_t),
                        ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t),
                        ('PeakJobMemoryUsed', ctypes.c_size_t)]

        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                wintypes.DWORD)
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        k32.SetPriorityClass.restype = wintypes.BOOL
        k32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        BELOW_NORMAL = 0x4000
        ph = int(getattr(proc, '_handle', 0) or 0)
        if not ph:
            return _ChildJob(None, 'no job object: the child has no process handle')
        if low_priority:
            k32.SetPriorityClass(ph, BELOW_NORMAL)       # also when the job cannot be made
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return _ChildJob(None, 'no job object (CreateJobObject failed, error %d)'
                             % ctypes.get_last_error())
        info = EXTENDED()
        flags = 0x2000 | 0x400                              # KILL_ON_JOB_CLOSE | DIE_ON_UNHANDLED_EXCEPTION
        if max_mb:
            flags |= 0x100                                  # PROCESS_MEMORY
            info.ProcessMemoryLimit = int(max_mb) * 1024 * 1024
        if low_priority:
            flags |= 0x20                                   # PRIORITY_CLASS, for its children too
            info.BasicLimitInformation.PriorityClass = BELOW_NORMAL
        if max_processes:
            flags |= 0x8                                    # ACTIVE_PROCESS
            info.BasicLimitInformation.ActiveProcessLimit = int(max_processes)
        info.BasicLimitInformation.LimitFlags = flags
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            err = ctypes.get_last_error()
            k32.CloseHandle(job)
            return _ChildJob(None, 'no job object (SetInformationJobObject failed, error %d)' % err)
        if not k32.AssignProcessToJobObject(job, ph):
            err = ctypes.get_last_error()
            k32.CloseHandle(job)
            return _ChildJob(None, 'no job object (AssignProcessToJobObject failed, error %d)'
                             % err)
        return _ChildJob(int(job), '')
    except Exception as e:                                  # noqa: BLE001 - never stops an analysis
        return _ChildJob(None, 'no job object (%s: %s)' % (type(e).__name__, e))


_TOKEN_RE = re.compile(r'^[0-9a-f]{16,40}$')


def _analysis_methods(cls):

    def _an_live(self):
        """The script block running now, as an analysis needs it, or None: {'uid', 'name', 'rep' (the rep being
        measured, 0 between reps), 'reps', 'listed' {normcase path} the datasets it has listed, 'told' {normcase
        path} the folders the runner said this rep is writing (its `folder` events), 'saved' {normcase path} the
        scans of this rep the script said it saved, 'since' the rep's start as file times see it (None between
        reps)}. Never raises."""
        live = getattr(self, '_an_live_run', None)
        if not live:
            return None
        try:
            item, state, reps = live

            def nk(p):
                return os.path.normcase(os.path.normpath(P.resolve(p)))
            with self.lock:
                rep = int(state.get('in_rep') or 0)
                listed = [p for p in list(item.get('datasets') or []) + list(item.get('datasets_partial') or [])
                          if isinstance(p, str) and p]
                told = list((state.get('folders') or {}).get(rep) or []) if rep else []
                saved = set((state.get('saved') or {}).get(rep) or ()) if rep else set()
                t0, m0 = state.get('rep_t0'), state.get('rep_m0')
                uid, name = item.get('uid'), item.get('name')
            since = None
            if rep and t0 is not None and m0 is not None:
                # as _rep_folders_left counts a rep's folders: across a wall-clock step too
                since = min(t0, time.time() - (time.monotonic() - m0)) - 3.0
            return {'uid': uid, 'name': name, 'rep': rep, 'reps': int(reps),
                    'listed': {nk(p) for p in listed},
                    'told': {nk(p) for p in told if isinstance(p, str) and p},
                    'saved': set(saved), 'since': since}
        except Exception:                                               # noqa: BLE001
            return None

    def _an_writing(self, path, live):
        """True when `path` is a dataset folder the running block's rep is still writing (live: _an_live()) - never
        analysed while it is written. The runner says a rep's folder soon after it appears; until it has said one, a
        folder made and changed since the rep began (as _rep_folders_left finds them) is taken for it. A dataset the
        block has listed, or a scan the script said it saved, is finished. Never raises."""
        try:
            if not live or not live.get('rep'):
                return False
            k = os.path.normcase(os.path.normpath(P.resolve(path)))
            if k in live['listed'] or k in live['saved']:
                return False
            if k in live['told']:
                return True
            if live['told'] or live['since'] is None:
                return False
            st = os.stat(path)
            created = getattr(st, 'st_birthtime', None) or st.st_ctime
            return created >= live['since'] and st.st_mtime >= live['since']
        except Exception:                                               # noqa: BLE001
            return False

    def analysis_status(self):
        """The analysis job as the page and the Scripts tab read it: {'state': 'idle'} before the first, else the last
        job (id, state, started, finished, done, total, items[...], left_out_jobs?)."""
        with self.lock:
            j = self._an_job
            return dict(j) if j else {'state': 'idle'}

    def analysis_scripts(self, classes=()):
        """The Scripts tab's list for data of these measurement classes (panel_myscripts.scan). Read fresh every call:
        a script dropped into my_analysis, edited or deleted is seen at the next one."""
        return _myscripts().scan(self, classes=classes)

    def analysis_new_script(self, name, template, classes=()):
        """The Scripts tab's "New script": my_analysis.dirs[0]/<name>.py written from templates/my_analysis/
        <template>.py (panel_myscripts.new_script) - its record, with the VS Code link. ValueError for a bad name /
        template / class, FileExistsError when the file is there (nothing is replaced)."""
        rec = _myscripts().new_script(self, name, template, classes)
        self.log('my analysis: new script %s (from the template "%s")' % (rec.get('file'), template), 'info')
        return rec

    def analysis_run(self, jobs):
        """Start one analysis job: `jobs` [{script: 'my:<k>:<file.py>', params, paths, dfgroup and / or dfgroups,
        label}] - each one item, run one after the other on the analysis worker. ValueError in words for anything
        that cannot run; RuntimeError('an analysis is already running') with .data {'running': {...}} while one
        runs. The job's id."""
        # a job run on History groups carries their handoffs' tokens: the panel must still know each group, and the
        # job may analyse only their datasets - the run is recorded against those groups. Looked up before the lock
        # (the store takes its own).
        if not isinstance(jobs, list) or not jobs:
            raise ValueError('jobs must be a list of at least one {script, params, paths, ...}')
        if len(jobs) > 200:
            raise ValueError('at most 200 jobs at a time')
        dfgroups = {}
        for i, j in enumerate(jobs):
            if not isinstance(j, dict):
                continue
            toks = []
            one = j.get('dfgroup')
            if one is not None and one != '':
                toks.append(one)
            many = j.get('dfgroups')
            if many is not None:
                if not isinstance(many, list) or len(many) > 200:
                    raise ValueError('job %d: dfgroups must be a list of History group tokens (at most 200)'
                                     % (i + 1))
                for t in many:
                    if t not in toks:
                        toks.append(t)
            if not toks:
                continue
            gone = ('job %d: the History group it was opened from is not known to the panel any '
                    'more - tick it again in History' % (i + 1))
            allowed = set()
            for tok in toks:
                if not isinstance(tok, str) or not _TOKEN_RE.match(tok):
                    raise ValueError(gone)
                df = getattr(self, 'df', None)
                try:
                    ho = df.handoff_get(tok) if df is not None else None
                except KeyError:
                    ho = None
                if not isinstance(ho, dict):
                    raise ValueError(gone)
                allowed.update(os.path.normcase(os.path.normpath(p)) for p in (ho.get('paths') or [])
                               if isinstance(p, str))
            raw = j.get('paths')
            if isinstance(raw, list):
                for p in raw:
                    if isinstance(p, str) and p and '\x00' not in p and \
                            os.path.normcase(os.path.normpath(P.resolve(p))) not in allowed:
                        raise ValueError('job %d: %s is not one of the datasets of the History '
                                         'group%s it was opened from' % (i + 1, p, 's' if len(toks) > 1 else ''))
            dfgroups[i] = toks
        # each job names its script - 'my:<k>:<file.py>' - and its PARAMS. Resolved here, before the lock (it reads
        # the folder and the file's declarations): an id that is not exact, a file not there or one that does not
        # parse, a parameter not declared or of the wrong type is a 400 in words.
        mine = {}
        for i, j in enumerate(jobs):
            if not isinstance(j, dict):
                raise ValueError('job %d must be an object {script, params, paths, ...}' % (i + 1))
            sid = j.get('script')
            if not isinstance(sid, str) or not sid:
                raise ValueError('job %d: script must be a script id (my:<folder number>:<file.py>)' % (i + 1))
            try:
                mine[i] = self._my_ready(sid, j.get('params'))
            except ValueError as e:
                raise ValueError('job %d: %s' % (i + 1, e))
        # a dataset the running block is still writing is never analysed - the page leaves it out itself; any other
        # client has it left out here, and the job item says so. Looked at before the lock (_an_live takes it).
        live = self._an_live()
        held_log, dropped = [], []
        with self.lock:
            if self._an_job and self._an_job.get('state') == 'running':
                # which one, so the Scripts tab can name it and offer "wait and retry" / "cancel that analysis" (the
                # server's 409 carries `running`)
                busy = self._an_job
                cur = next((x for x in busy.get('items') or [] if x.get('state') == 'running'), None)
                err = RuntimeError('an analysis is already running')
                err.data = {'running': {'id': busy.get('id'), 'started': busy.get('started'),
                                        'done': busy.get('done'), 'total': busy.get('total'),
                                        'label': (cur or {}).get('label'),
                                        'labels': [x.get('label') for x in (busy.get('items') or [])][:20]}}
                raise err
            clean = []
            for i, j in enumerate(jobs):
                rec, params = mine[i]
                name = 'my:' + rec['name']
                raw = j.get('paths') or []
                # a path the OS cannot even be given (a NUL in it) must never escape the worker thread
                if not isinstance(raw, list) or any(not isinstance(p, str) or not p or '\x00' in p for p in raw):
                    raise ValueError('job %d: the folders must be a list of paths' % (i + 1))
                if len(raw) > 20000:
                    raise ValueError('job %d: at most 20000 folders' % (i + 1))
                paths = [P.resolve(p) for p in raw]
                if not paths:
                    raise ValueError('job %d needs at least one dataset folder' % (i + 1))
                held = [p for p in paths if self._an_writing(p, live)] if live else []
                if held:
                    paths = [p for p in paths if p not in held]
                    held_log.append((j.get('label') or name, held, not paths))
                    if not paths:
                        # a job of only the rep being written is left out with a log line, not the whole request
                        # refused. Refused below only when no job is left.
                        dropped.append('job %d: its datasets are still being measured (rep %d of %d of "%s") - '
                                       'analyse them once that rep is written'
                                       % (i + 1, live['rep'], live['reps'], live['name']))
                        continue
                label = j.get('label')
                label = str(label)[:200] if isinstance(label, str) and label.strip() else name
                clean.append({'analysis': name, 'paths': paths, 'label': label,
                              'state': 'queued', 'result': None, 'error': None,
                              # made here, never added later: the job is served (dict(job)) and serialised
                              # outside the lock while the worker fills them in
                              'started': None, 'finished': None,
                              'dfgroup': (dfgroups.get(i) or [None])[0], 'record': None, 'history_row': None,
                              'kind': 'my', 'script': rec['id'], 'script_file': rec.get('file'),
                              'params': dict(params),
                              'out': None, 'tail': [], 'dfgroups': list(dfgroups.get(i) or []),
                              'n_figures': None, 'records': []})
                if held:
                    clean[-1]['left_out_writing'] = [os.path.basename(os.path.normpath(p)) for p in held]
            if not clean:
                raise ValueError('; '.join(dropped) if dropped else 'nothing to analyse')
            self._an_job = {'id': _uid('an'), 'state': 'running', 'started': time.time(),
                            'done': 0, 'total': len(clean), 'items': clean, 'finished': None}
            if dropped:
                self._an_job['left_out_jobs'] = list(dropped)       # said, not hidden
            job = self._an_job
        for lab, held, whole in held_log:
            self.log('analysis: %s - %s still being measured (rep %d of %d of "%s"): %s'
                     % (lab, 'not analysed: every dataset of it is' if whole else 'left out,',
                        live['rep'], live['reps'], live['name'],
                        ', '.join(os.path.basename(os.path.normpath(p)) for p in held)), 'warn',
                     uid=_an_log_uid(job))
        t = threading.Thread(target=self._analysis_worker, args=(job,), name='panel-analysis', daemon=True)
        t.start()
        return job['id']

    def analysis_cancel(self, job_id=None):
        """Stop the analysis job: the running script is killed, the rest not started. The measurement is never
        touched - Stop does not reach in here, nor this out there. `job_id`: the job the page means - the one its
        question was about. When it is given and is not the job that runs now, nothing is cancelled (False): a dialog
        left open while that job ended and another started must not cancel the new one. None: the job that runs
        now."""
        with self.lock:
            job = self._an_job
            if not job or job.get('state') != 'running':
                return False
            if job_id is not None and str(job_id) != str(job.get('id')):
                return False
            job['cancel'] = True
            proc = self._an_proc
        if proc is None:
            # asked before its process is started (or between two items): the worker sees the flag before it starts
            # the next one, and _spawn_analysis right after its Popen - nothing to kill yet
            self.log('analysis: cancel requested - the analysis stops before it starts', 'warn',
                     uid=_an_log_uid(job))
            return True
        if proc.poll() is None:
            self._kill_tree(proc)
        elif getattr(proc, '_panel_t_spawn', None) is not None:
            # the analysis itself has ended, and something it started still holds its output (_spawn_analysis is
            # waiting for it to drain): that is what is killed
            self._kill_orphans_of(proc.pid, proc._panel_t_spawn, who='the analysis')
        self.log('analysis: cancelled', 'warn', uid=_an_log_uid(job))
        return True

    def _analysis_worker(self, job):
        # every way out ends the job: an exception here would otherwise leave it 'running' for good, and every later
        # analysis refused until the panel restarted
        crashed = None
        _an_prune_state()
        try:
            self._analysis_items(job)
        except Exception as exc:                                        # noqa: BLE001
            crashed = '%s: %s' % (type(exc).__name__, exc)
            self.log('analysis crashed: %s' % crashed, 'error', uid=_an_log_uid(job))
        finally:
            with self.lock:
                for it in job['items']:
                    if it['state'] in ('queued', 'running'):
                        it['state'] = 'cancelled' if job.get('cancel') else 'error'
                        it['error'] = it.get('error') or (
                            'cancelled' if job.get('cancel') else 'the analysis worker stopped: %s' % crashed)
                        it['finished'] = time.time()
                job['state'] = 'error' if crashed else ('cancelled' if job.get('cancel') else 'done')
                if crashed:
                    job['error'] = crashed
                job['finished'] = time.time()
        ok = sum(1 for i in job['items'] if i['state'] == 'done')
        self.log('analysis finished: %d of %d run(s) done' % (ok, job['total']),
                 'ok' if ok == job['total'] else 'warn', uid=_an_log_uid(job))
        # the items run on History groups: records and one History row each, outside the lock, whatever their end;
        # never raises
        record = getattr(self, '_df_record_job', None)
        if record is not None:
            try:
                record(job)
            except Exception:                                           # noqa: BLE001
                pass

    def _an_tail(self, it):
        """on_line of _spawn_analysis for item `it`: its output's last _AN_TAIL_N text lines, each cut to _AN_TAIL_W
        characters, for the Scripts tab's live view. The list is REPLACED under the lock, never appended in place:
        the job is served (dict(job)) and serialised outside the lock while this runs."""
        def on_line(line):
            ln = str(line)[:_AN_TAIL_W]
            with self.lock:
                it['tail'] = (list(it.get('tail') or []) + [ln])[-_AN_TAIL_N:]
        return on_line

    def _analysis_items(self, job):
        luid = _an_log_uid(job)
        self.log('--- analysis: %d run(s) ---' % job['total'], 'head', uid=luid)
        for it in job['items']:
            if job.get('cancel'):
                break
            self._analysis_item(job, it)

    def _my_ready(self, sid, given):
        """(record, clean PARAMS) of your script `sid` as it is NOW on disk - resolved and its parameters checked
        again at the moment it is run, so a file edited since it was chosen is what runs, and one deleted, broken or
        no longer taking a parameter is refused in words (ValueError)."""
        try:
            MS = _myscripts()
        except RuntimeError as e:
            raise ValueError(str(e))
        rec = MS.resolve(self, sid)
        if not rec.get('runnable'):
            raise ValueError('%s cannot be run: %s' % (rec.get('file'), '; '.join(rec.get('problems') or [])
                                                       or 'it is not runnable'))
        return rec, MS.check_params(rec, given or {})

    def _my_cmd(self, rec, params, paths, base, label, sid):
        """The command line of one run of your script, and what it needs made first: its out folder (<data root>/
        <day>/analysis/<name>_<HH-MM-SS>, _my_out_folder), the dataset list `base`.txt and the PARAMS
        `base`-params.json. (cmd, out folder, guard on?). ValueError in words when the runner is missing or a folder
        / file cannot be written."""
        MS = _myscripts()
        runner = os.path.join(P.RUNNERS_DIR, 'run_myscript.py')
        if not os.path.isfile(runner):
            raise ValueError('the runner of your own scripts is missing: %s' % P.rel_to_root(runner))
        try:
            out = _my_out_folder(self.cfg, paths, rec['name'])
        except OSError as e:
            raise ValueError('its output folder could not be made under %s (%s)' % (P.data_root(self.cfg), e))
        lst, pfile = base + '.txt', base + '-params.json'
        try:
            os.makedirs(os.path.dirname(base), exist_ok=True)
            with open(lst, 'w', encoding='utf-8') as f:
                f.write(''.join(str(p) + '\n' for p in paths))
            with open(pfile, 'w', encoding='utf-8') as f:
                json.dump(params, f, ensure_ascii=False)
        except (OSError, TypeError, ValueError) as e:
            raise ValueError('its dataset list could not be written to %s (%s)' % (P.STATE_DIR, e))
        cmd = [P.python_exe(self.cfg), '-u', runner, '--script', rec['path'], '--out', out,
               '--datasets-file', lst, '--params-file', pfile]
        guard = MS.cfg_of(self.cfg)['guard']
        if guard:
            cmd.append('--guard')
            # the port this panel really listens on (a --port start writes it into the config before the engine is
            # made): refused on loopback too. run_myscript.py puts it in LABMASTER_GUARD_PORTS, so a guarded child
            # Python refuses it too.
            ports = self._my_guard_ports()
            if ports:
                cmd += ['--guard-ports', ','.join(str(p) for p in ports)]
        cmd += ['--label', str(label or rec.get('name') or sid)[:200], '--id', sid]
        return cmd, out, guard

    def _my_guard_ports(self):
        """The loopback port(s) of this panel, sorted: server.port (default 8770). A value that is not a port number
        is left out. Never raises."""
        cfg = self.cfg if isinstance(self.cfg, dict) else {}
        out = set()
        s = cfg.get('server')
        v = s.get('port', 8770) if isinstance(s, dict) else 8770
        try:
            p = int(str(v).strip()) if not isinstance(v, bool) else 0
        except (TypeError, ValueError):
            p = 0
        if 1 <= p <= 65535:
            out.add(p)
        return sorted(out)

    def _analysis_item(self, job, it):
        """One item: its out folder made, its datasets and PARAMS written to state/analysis_jobs/, and
        runners/run_myscript.py started through _spawn_analysis - the timeout, Cancel, the ##PANEL event. The script
        is resolved again here, so a file edited since Run is what runs, and one deleted or broken since is an error
        in words. It runs at BELOW_NORMAL priority with at most my_analysis.max_memory_mb per process (a job object,
        _job_for_child - it may run beside a measurement, and it is anybody's code); its two files in
        state/analysis_jobs/ are removed once it has ended (the runner reads them at its start; the item keeps the
        params); a Cancel pressed while it was being prepared is honoured before anything starts. Never raises (the
        worker ends the job whatever happens)."""
        n = job['items'].index(it) + 1
        luid = _an_log_uid(job)
        base = os.path.join(_an_state_dir(), '%s-%d' % (job['id'], n))

        def fail(why):
            with self.lock:
                it['state'], it['error'] = 'error', why
                it['finished'] = time.time()
                job['done'] += 1
            self.log('  -> not started (%s)' % why, 'error', uid=luid)
        try:
            try:
                rec, params = self._my_ready(it['script'], it.get('params') or {})
                cmd, out, guard = self._my_cmd(rec, params, it['paths'], base,
                                               it.get('label') or it['analysis'], it['script'])
            except Exception as e:                                      # noqa: BLE001
                return fail(str(e) or type(e).__name__)
            mcfg = _myscripts().cfg_of(self.cfg)
            mem_mb = mcfg['max_memory_mb']
            with self.lock:
                stop = bool(job.get('cancel'))
                if not stop:
                    it['state'] = 'running'
                    it['started'] = time.time()
                    it['out'] = out
                    it['params'] = dict(params)
            if stop:
                # left queued: the worker's finally marks it cancelled, nothing started - and the out folder just
                # made for it goes again (os.rmdir: only while it is still empty)
                try:
                    os.rmdir(out)
                except OSError:
                    pass
                return
            self.log('analysing %s with your script %s  (%d dataset(s)%s) -> %s'
                     % (it['label'], rec.get('file'), len(it['paths']),
                        '' if guard else ', instrument guard OFF', P.rel_to_root(out)), 'info', uid=luid)
            got = {}
            # the runner's own event only (_my_event_into): a ##PANEL line the script printed itself never reaches
            # the item's result
            extra_env = {'LABMASTER_GUARD_MODULES': ','.join(mcfg['guard_modules']),
                         'LABMASTER_GUARD_DLLS': ','.join(mcfg['guard_dlls'])} if guard else {}
            with _an_spawn_opts(cancelled=lambda: job.get('cancel'), uid=luid, job_mb=mem_mb, out_folder=out,
                                log_lines=_AN_LOG_LINES, env=extra_env):
                rc = self._spawn_analysis(cmd, lambda ev: _my_event_into(got, ev), on_line=self._an_tail(it))
            with self.lock:
                if got:
                    it['n_figures'] = got.get('n_figures')
                if job.get('cancel'):
                    it['state'], it['error'] = 'cancelled', 'cancelled'
                    if got:
                        it['result'] = got
                elif rc == 0 and got:
                    it['state'], it['result'] = 'done', got
                    self.log('  -> %s  (%d figures)%s'
                             % (got.get('group'), got.get('n_figures') or 0,
                                '  verdict: %s' % got['verdict'] if got.get('verdict') not in (None, 'ok') else ''),
                             'ok' if got.get('verdict') in (None, 'ok') else 'warn', uid=luid)
                else:
                    why = '; '.join(str(x) for x in (got.get('problems') or []))
                    if 'MemoryError' in why or (not why and any('MemoryError' in str(x)
                                                                for x in (it.get('tail') or [])[-5:])):
                        why = ('%s%syour script went past my_analysis.max_memory_mb (%d MB per process) - a '
                               'smaller piece at a time, or a larger limit in config.json'
                               % (why, ' - ' if why else '', mem_mb))
                    it['state'] = 'error'
                    it['error'] = ('timed out after %s' % _hms(self._an_timeout()) if rc == -2 else
                                   _AN_LEFTOVER_WORDS if rc == _AN_RC_LEFTOVER else
                                   'exit code %s%s' % (rc, (': ' + why) if why else
                                                       (' (the runner reported nothing - see its output)'
                                                        if not got else '')))
                    if got:
                        it['result'] = got
                    self.log('  -> FAILED (%s)' % it['error'], 'error', uid=luid)
                it['finished'] = time.time()
                job['done'] += 1
        finally:
            # the dataset list and the PARAMS file: the runner read them at its start, the item keeps the params
            _an_remove(base + '.txt')
            _an_remove(base + '-params.json')

    def _an_timeout(self):
        v = _finite(self._run_cfg().get('analysis_timeout_minutes', 60))
        return (v if v and v > 0 else 60.0) * 60.0

    def _spawn_analysis(self, cmd, on_event, on_line=None, **opts):
        """Like _spawn, but with its own handle: Stop belongs to the measurement. Never raises. An analysis that runs
        past run.analysis_timeout_minutes (60) is killed. `on_line(text)` is given every output line that is not a
        ##PANEL event (the text before one glued to it too) - the Scripts tab's live tail; an on_line that raises is
        ignored.

        What the analysis leaves running when it ends: its output is read to EOF, and EOF comes only when EVERY
        process holding the pipe has closed it - a child a script started (a Python subprocess passes the guard, and
        inherits the pipe) and left behind would hold the job 'running' for as long as it lived. The output is read by
        a pump thread; once the analysis has exited it is given 5 s to drain, then what it left behind is killed
        (_kill_orphans_of: its children started after it), 10 s more, and then it is no longer waited for. Either way
        the end is not clean: _AN_RC_LEFTOVER, whatever its own exit code. The timeout and Cancel reach the leftovers
        too: the proc keeps its start time (_panel_t_spawn), and self._an_proc stays set until the drain is over.

        The options of the run, as keywords or set around the call by the worker with _an_spawn_opts:
        - `cancelled`: a callable, true once the job is cancelled. Read right after the Popen, in the same lock that
          sets self._an_proc - analysis_cancel sets the flag and reads _an_proc under that lock too, so a Cancel
          either finds the process or is seen here.
        - `uid`: the Log uid of every line written here (the job's 'analysis:<id>', _an_log_uid).
        - `job_mb`: put the process in a job object (_job_for_child): BELOW_NORMAL priority for it and its children,
          at most job_mb MB per process, all of it killed when the job is closed (after the drain). While a block
          measures, its numpy / MKL / OpenMP threads are held to 2 (unless the environment says otherwise).
        - `out_folder`: every output line, whole, also goes to <out_folder>/panel_output.log; `log_lines`: at most
          that many reach the panel Log (None: all), each cut to _AN_LOG_W characters; one closing line says how many
          more there were.
        - `env`: extra environment variables for the process."""
        o = dict(getattr(_AN_SPAWN_OPTS, 'v', None) or {})
        o.update(opts)
        cancelled, uid, job_mb = o.get('cancelled'), o.get('uid'), o.get('job_mb')
        out_folder, log_lines = o.get('out_folder'), o.get('log_lines')

        def said(text):
            if on_line is not None:
                try:
                    on_line(text)
                except Exception:                                       # noqa: BLE001
                    pass
        outlog = _AnOutLog(out_folder)
        counts = {'logged': 0, 'more': 0}

        def tolog(text, lvl='out'):
            # whole into the run's own file; the first `log_lines` into the Log, cut
            outlog.write(text)
            if log_lines is None or counts['logged'] < log_lines:
                counts['logged'] += 1
                self.log(_an_cut(text), lvl, uid=uid)
            else:
                counts['more'] += 1
        t_spawn = time.time()
        env = dict(os.environ, LABMASTER_DATA_ROOT=P.data_root(self.cfg))
        for k, v in (o.get('env') or {}).items():
            env[str(k)] = str(v)
        if job_mb and getattr(self, 'run_state', None) in (RUN_RUNNING, RUN_STOPPING):
            for k in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
                env.setdefault(k, '2')
        try:
            # the same data folder the panel reads: the scripts' helpers find it in LABMASTER_DATA_ROOT
            proc = subprocess.Popen(cmd, cwd=P.work_dir(self.cfg), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1,
                                    text=True, encoding='utf-8', errors='replace', env=env)
        except Exception as e:                                          # noqa: BLE001
            self.log('cannot start the analysis: %s: %s' % (type(e).__name__, e), 'error', uid=uid)
            outlog.close()
            return -1
        proc._panel_t_spawn = t_spawn            # what the timeout and analysis_cancel kill leftovers by
        cj = None
        if job_mb:
            # a grandchild started before this assignment escapes the job: the interpreter's own start-up makes that
            # unlikely, and Popen cannot start a process suspended - accepted
            cj = _job_for_child(proc, job_mb, low_priority=True)
            if cj.note:
                self.log('analysis: it runs without its memory limit / lower priority - %s' % cj.note, 'warn',
                         uid=uid)
        with self.lock:
            self._an_proc = proc
            stop = bool(cancelled is not None and cancelled())
        if stop:
            self._kill_tree(proc)        # cancelled while it was being started: the wait below ends at once
        timed_out = []
        drained = threading.Event()
        detached = []                    # set once the worker has stopped waiting for the pipe

        def too_long():
            if proc.poll() is not None and drained.is_set():
                return                   # it ended, and its output is all read, just as the timer went off
            timed_out.append(True)
            self.log('analysis ran past %s - killed' % _hms(self._an_timeout()), 'error', uid=uid)
            if proc.poll() is None:
                self._kill_tree(proc)
            else:
                # the analysis has ended; what it started still holds its output
                self._kill_orphans_of(proc.pid, t_spawn, uid=uid, who='the analysis')

        def pump():
            try:
                for line in proc.stdout:
                    line = line.rstrip('\r\n')
                    if detached:
                        # no longer waited for: what still comes goes to the log only - the item is finished
                        if line.strip():
                            tolog(line)
                        continue
                    text, js = _panel_event(line)
                    if js is not None:
                        if text.strip():
                            tolog(text)
                            said(text)
                        try:
                            ev = json.loads(js)
                        except ValueError:
                            self.log('analysis: a ##PANEL line could not be read: %s' % line[:160],
                                     'warn', uid=uid)
                            continue
                        if not isinstance(ev, dict):
                            self.log('analysis: ignored a ##PANEL line that is not an object: %s'
                                     % line[:120], 'warn', uid=uid)
                            continue
                        try:
                            on_event(ev)
                        except Exception as exc:                        # noqa: BLE001
                            self.log('analysis event could not be handled: %s: %s'
                                     % (type(exc).__name__, exc), 'warn', uid=uid)
                        continue
                    if line.strip():
                        tolog(line)
                    said(line)
            except (OSError, ValueError):
                pass
            finally:
                drained.set()
                if detached:
                    outlog.close()

        timer = threading.Timer(self._an_timeout(), too_long)
        timer.daemon = True
        timer.start()
        reader = threading.Thread(target=pump, name='panel-analysis-output', daemon=True)
        reader.start()
        stuck = False
        rc = -1
        try:
            rc = proc.wait()             # the timer and Cancel end it; nothing else is waited for here
            if not drained.wait(5.0):
                killed = self._kill_orphans_of(proc.pid, t_spawn, uid=uid, who='the analysis')
                if not drained.wait(10.0):
                    stuck = True
                    detached.append(True)
                    self.log('something the analysis started is still holding its output after it ended%s - no '
                             'longer waiting for it'
                             % (' (killed: pid %s)' % ', '.join(map(str, killed)) if killed else ''), 'error',
                             uid=uid)
                else:
                    stuck = True         # it had to be killed: not a clean end, whatever the exit code said
                    self.log('the analysis left %s running after it ended - killed'
                             % (('pid %s' % ', '.join(map(str, killed))) if killed else 'something'), 'error',
                             uid=uid)
        except Exception as e:                                          # noqa: BLE001
            stuck = True
            detached.append(True)
            self.log('analysis: waiting for it failed: %s: %s' % (type(e).__name__, e), 'error', uid=uid)
        finally:
            timer.cancel()
            if cj is not None:
                cj.close()               # kill-on-close: anything of it still in the job ends here, after the drain
            with self.lock:
                if self._an_proc is proc:
                    self._an_proc = None
            if counts['more']:
                self.log('... %d more output line(s) not shown in the Log%s'
                         % (counts['more'], (' - every line is in %s' % P.rel_to_root(outlog.path))
                            if outlog.path else ''), 'info', uid=uid)
            if not detached or drained.is_set():
                outlog.close()           # detached: the pump may still write - it closes the file when it ends
        if timed_out:
            return -2
        return _AN_RC_LEFTOVER if stuck else rc

    for fn in (_an_live, _an_writing, analysis_status, analysis_scripts, analysis_new_script, analysis_run,
               analysis_cancel, _analysis_worker, _an_tail, _analysis_items, _my_ready, _my_cmd, _my_guard_ports,
               _analysis_item, _an_timeout, _spawn_analysis):
        setattr(cls, fn.__name__, fn)
    cls._an_job = None
    cls._an_proc = None
    cls._an_live_run = None
    return cls


_analysis_methods(Engine)
