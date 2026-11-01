# -*- coding: utf-8 -*-
"""
panel_myscripts.py - the analysis scripts History offers: your own scripts, hot-pluggable.

0.1.7. A script is any top-level *.py in one of config.json my_analysis.dirs (default my_analysis/ under the root,
gitignored, never touched by an update): drop a file there and the next scan lists it - nothing is registered,
nothing restarts. What a file declares (TITLE, CLASSES, PARAMS) is read with ast: nothing here imports, compiles or
runs a script, so a file that does not parse is listed with its SyntaxError instead of taking anything down. The
runner (runners/run_myscript.py) is what runs one, in its own process, under the instrument guard
(runners/myscript_guard.py).

Script ids (the contract with the page and the engine):
    my:<k>:<file.py>     the file <file.py> directly inside my_analysis.dirs[k]

Every function here is pure and never raises, except resolve / check_params / new_script, which raise ValueError
(and FileExistsError) with a sentence a person understands - the engine and the server turn those into a 400 / 409.
"""
import ast
import math
import os
import re
import shutil
import threading
import time
from urllib.parse import quote

from . import paths as P

DEFAULTS = {'dirs': ['my_analysis'], 'guard': True, 'vscode_remote': '', 'max_memory_mb': 4096,
            'guard_modules': [], 'guard_dlls': []}
MAX_MEMORY_RANGE = (256, 65536)      # my_analysis.max_memory_mb outside it reads as the default
HELPER_NAME = 'labmaster_analysis.py'

# used with fullmatch, never match: '$' also matches before a trailing newline. ASCII only (\w and \d take any
# script's letters and digits); k without a leading zero, so one file has one id
ID_MY_RE = re.compile(r'my:(0|[1-9][0-9]{0,2}):(.+)', re.S)
PARAM_NAME_RE = re.compile(r'^[A-Za-z_]\w{0,40}\Z', re.A)
NEW_NAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,60}\Z')
TEMPLATE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,40}\Z')
# a measurement class a script may declare (a dataset folder's class: #NNN_<Class>_HH-MM-SS)
CLASS_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,119}\Z')
# what the page may ask the list for: anything else cannot match a declaration, so it is dropped, not refused
ASK_RE = CLASS_RE
# a guard_modules entry (a module or a dotted package path) and a guard_dlls entry (a DLL base name, * wildcards)
GUARD_MODULE_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*\Z')
GUARD_DLL_RE = re.compile(r'^[A-Za-z0-9_*.-]{1,80}\Z')

MAX_BYTES = 2 * 1024 * 1024          # a larger file's declarations are not read
MAX_PARAMS = 100
MAX_LIST = 10000
MAX_STR = 2000
MAX_SCRIPTS_PER_DIR = 500            # a folder someone pointed at a whole code tree is cut, and said so

# built-in titles of the two templates the panel ships, for a template whose docstring is still a placeholder
TEMPLATE_TITLES = {'plot': 'Plot each dataset (the simplest)', 'fit': 'Fit with parameters (PARAMS, scipy)'}

_MEMO = {}                           # abs path -> ((mtime_ns, size), declarations)
_MEMO_LOCK = threading.Lock()


# ---------------------------------------------------------------- config

def _names(v, rx):
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        x = x.strip() if isinstance(x, str) else ''
        if x and rx.match(x) and x not in out:
            out.append(x)
    return out[:200]


def cfg_of(cfg):
    """config.json my_analysis with the defaults filled in: {dirs [str], guard bool, vscode_remote str,
    max_memory_mb int, guard_modules [str], guard_dlls [str]}. A value of the wrong type is the default; a
    guard_modules / guard_dlls entry that is not a name is left out. `max_memory_mb`: the memory one process of a
    script started from History may take (a Windows job object), a whole number in MAX_MEMORY_RANGE."""
    sec = (cfg or {}).get('my_analysis') if isinstance(cfg, dict) else None
    sec = sec if isinstance(sec, dict) else {}
    ds = sec.get('dirs')
    ds = [d.strip() for d in ds if isinstance(d, str) and d.strip()] if isinstance(ds, list) else None
    guard = sec.get('guard')
    remote = sec.get('vscode_remote')
    mem = sec.get('max_memory_mb')
    if (isinstance(mem, bool) or not _finite(mem) or mem != int(mem)
            or not MAX_MEMORY_RANGE[0] <= mem <= MAX_MEMORY_RANGE[1]):
        mem = DEFAULTS['max_memory_mb']
    return {'dirs': ds or list(DEFAULTS['dirs']), 'max_memory_mb': int(mem),
            'guard': guard if isinstance(guard, bool) else DEFAULTS['guard'],
            'vscode_remote': (remote.strip() if isinstance(remote, str) else DEFAULTS['vscode_remote']),
            'guard_modules': _names(sec.get('guard_modules'), GUARD_MODULE_RE),
            'guard_dlls': _names(sec.get('guard_dlls'), GUARD_DLL_RE)}


def dirs(cfg):
    """[absolute path] of my_analysis.dirs, in order (index k of a 'my:<k>:...' id). Relative ones are relative to the
    root (P.resolve)."""
    return [os.path.normpath(P.resolve(d)) for d in cfg_of(cfg)['dirs']]


def helper_dir():
    """The folder holding labmaster_analysis.py (the helper a script imports): the root's scripts/ when it has the
    helper, else the panel's own scripts/ - the same fallback the panel uses for scripts/."""
    for base in (P.ROOT, P.PANEL_DIR):
        d = os.path.join(base, P.SCRIPTS_DIRNAME)
        if os.path.isfile(os.path.join(d, HELPER_NAME)):
            return os.path.normpath(d)
    return os.path.normpath(os.path.join(P.PANEL_DIR, P.SCRIPTS_DIRNAME))


def templates_dir():
    return os.path.join(P.PANEL_DIR, 'templates', 'my_analysis')


def ensure_default_dir(cfg, log=None):
    """At the data folder's start: dirs[0] made if it is missing, and the templates' README.md copied into it ONLY when
    that file is absent - a README you edited is never replaced, and nothing else is ever written there (except by
    "New script"). Never raises; True when dirs[0] is there afterwards."""
    def say(text, lvl):
        try:
            if log is not None:
                log(text, lvl)
        except Exception:                                               # noqa: BLE001
            pass
    try:
        d = dirs(cfg)[0]
    except Exception:                                                   # noqa: BLE001
        return False
    try:
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
            say('my analysis: made the folder for your own scripts, %s' % d, 'info')
        src = os.path.join(templates_dir(), 'README.md')
        dst = os.path.join(d, 'README.md')
        if os.path.isfile(src) and not os.path.lexists(dst):
            shutil.copyfile(src, dst)
            say('my analysis: put README.md (how to write a script) into %s' % d, 'info')
        return True
    except Exception as e:                                              # noqa: BLE001
        say('my analysis: the folder for your own scripts (%s) could not be prepared: %s: %s'
            % (d, type(e).__name__, e), 'warn')
        return os.path.isdir(d)


def vscode_url(cfg, abs_path):
    """The "open in VS Code" link of a file or folder: vscode://vscode-remote/<remote>/<D:/path> when
    my_analysis.vscode_remote is set (a remote window, e.g. over SSH), else vscode://file/<D:/path>. The path with
    forward slashes, percent-escaped where a URL needs it ('#', ' '). A network path keeps its UNC marker: the link's
    path is /D:/... for a drive and //server/... for \\\\server\\share."""
    try:
        remote = cfg_of(cfg)['vscode_remote'].strip().strip('/')
        p = quote(os.path.abspath(str(abs_path)).replace('\\', '/'), safe="/:-._~()+,;=@!$&'")
        path = p if p.startswith('//') else '/' + p.lstrip('/')
        if remote:
            return 'vscode://vscode-remote/%s%s' % (remote, path)
        return 'vscode://file%s' % path
    except Exception:                                                   # noqa: BLE001
        return ''


# ---------------------------------------------------------------- reading a script's declarations

def _finite(v):
    """A real number a float can hold. An int past ~1e308 (a 400-digit literal in PARAMS, or in a request's params
    from json.loads) is NOT one: math.isfinite raises OverflowError on it - it is "must be a number": a problem line on
    that one entry, or a 400."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:
        return False


def _num_list(v):
    return isinstance(v, (list, tuple)) and len(v) <= MAX_LIST and all(_finite(x) for x in v)


def _same_choice(c, v):
    """v is the choice c: equal, and of the same kind - True is not 1, '1' is not 1 (2 and 2.0 are the same number)."""
    if isinstance(c, bool) or isinstance(v, bool):
        return isinstance(c, bool) and isinstance(v, bool) and c == v
    if isinstance(c, str) or isinstance(v, str):
        return isinstance(c, str) and isinstance(v, str) and c == v
    return _finite(c) and _finite(v) and c == v


def _type_of(default):
    """The PARAMS type a default gives, or None when it gives none."""
    if isinstance(default, bool):
        return 'bool'
    if isinstance(default, int):
        return 'int' if _finite(default) else None      # one a float cannot hold is refused (see _finite)
    if isinstance(default, float):
        return 'float' if math.isfinite(default) else None
    if isinstance(default, str):
        return 'str' if len(default) <= MAX_STR else None
    if _num_list(default):
        return 'list'
    return None


def _param(name, raw):
    """(the param entry, '') or (None, why) of one PARAMS entry: a plain literal default, or a dict with key
    'default' and optional label / help / min / max / choices."""
    rich = isinstance(raw, dict)
    if rich and 'default' not in raw:
        return None, 'a dict entry needs a "default" key'
    if rich:
        extra = set(raw) - {'default', 'label', 'help', 'min', 'max', 'choices'}
        if extra:
            return None, 'unknown key%s %s (default, label, help, min, max, choices)' % (
                's' if len(extra) > 1 else '', ', '.join(sorted(repr(x) for x in extra)))
    default = raw.get('default') if rich else raw
    label = raw.get('label', name) if rich else name
    hlp = raw.get('help', '') if rich else ''
    lo = raw.get('min') if rich else None
    hi = raw.get('max') if rich else None
    choices = raw.get('choices') if rich else None
    if not isinstance(label, str) or len(label) > 120:
        return None, 'label must be text of at most 120 characters'
    if not isinstance(hlp, str) or len(hlp) > 1000:
        return None, 'help must be text of at most 1000 characters'
    for k, v in (('min', lo), ('max', hi)):
        if v is not None and not _finite(v):
            return None, '%s must be a number' % k
    if lo is not None and hi is not None and lo > hi:
        return None, 'min %g is above max %g' % (lo, hi)
    if choices is not None:
        if not isinstance(choices, (list, tuple)) or not 1 <= len(choices) <= 200:
            return None, 'choices must be a list of 1 to 200 values'
        kinds = {_type_of(c) for c in choices}
        if len(kinds) != 1 or kinds & {None, 'list'}:
            return None, 'choices must all be numbers, all text or all true/false'
        if not any(_same_choice(c, default) for c in choices):
            return None, 'its default %r is not one of its choices' % (default,)
        typ = 'choice'
        choices = list(choices)
    else:
        typ = _type_of(default)
        if typ is None and isinstance(default, int) and not isinstance(default, bool):
            return None, 'its default is a whole number too large to use (above about 1e308)'
        if typ is None:
            return None, ('its default %r is none of: true/false, a whole number, a number, text (at most %d '
                          'characters), a list of numbers (at most %d)' % (default, MAX_STR, MAX_LIST))[:300]
    if typ in ('int', 'float') and ((lo is not None and default < lo) or (hi is not None and default > hi)):
        return None, 'its default %r is outside min/max' % (default,)
    if typ == 'list' and any((lo is not None and x < lo) or (hi is not None and x > hi) for x in default):
        return None, 'a number of its default is outside min/max'
    return {'name': name, 'type': typ, 'default': list(default) if isinstance(default, tuple) else default,
            'label': label or name, 'help': hlp, 'min': lo, 'max': hi, 'choices': choices}, ''


def _read_params(node, problems):
    """PARAMS = {...}: each entry read on its own (a bad one skipped with a problem naming it, the rest kept)."""
    out, seen = [], set()
    if not isinstance(node, ast.Dict):
        problems.append('PARAMS (line %d) is not a dict literal {name: default, ...}: ignored' % node.lineno)
        return out
    for kn, vn in zip(node.keys, node.values):
        if kn is None:                                                  # {**other}
            problems.append('PARAMS (line %d): "**" is not read - write each entry out' % node.lineno)
            continue
        try:
            name = ast.literal_eval(kn)
        except Exception:                                               # noqa: BLE001
            problems.append('PARAMS line %d: a name that is not a literal text: skipped' % kn.lineno)
            continue
        if not isinstance(name, str) or not PARAM_NAME_RE.match(name):
            problems.append('PARAMS line %d: %r is not a parameter name ([A-Za-z_]\\w{0,40}): skipped'
                            % (kn.lineno, name))
            continue
        if name in seen:
            problems.append('PARAMS: %s is given twice - the first is used' % name)
            continue
        try:
            raw = ast.literal_eval(vn)
        except Exception:                                               # noqa: BLE001
            problems.append('PARAMS %s (line %d) is not a literal (a number, text, true/false, a list of numbers, '
                            'or a dict of those): skipped' % (name, vn.lineno))
            continue
        try:
            ent, why = _param(name, raw)
        except Exception as e:                                          # noqa: BLE001 - one entry, never the list
            ent, why = None, 'cannot be read (%s)' % (str(e)[:120] or type(e).__name__)
        if ent is None:
            problems.append('PARAMS %s (line %d): %s - skipped' % (name, vn.lineno, why))
            continue
        if len(out) >= MAX_PARAMS:
            problems.append('PARAMS: more than %d entries - the rest are skipped' % MAX_PARAMS)
            break
        seen.add(name)
        out.append(ent)
    return out


def _first_line(doc):
    for ln in (doc or '').splitlines():
        if ln.strip():
            return ln.strip()[:200]
    return ''


def _empty_decl():
    return {'title': '', 'about': '', 'classes': [], 'params': [], 'problems': [], 'parses': True}


def _read_decl(path):
    """What a script declares, read with ast (never imported, never compiled to run):
    {title, about, classes, params, problems, parses}."""
    out = _empty_decl()
    try:
        if os.stat(path).st_size > MAX_BYTES:
            out['problems'].append('larger than 2 MB: its declarations are not read')
            return out
        with open(path, 'rb') as f:
            raw = f.read()
        tree = ast.parse(raw, filename=path)
    except SyntaxError as e:
        out['parses'] = False
        out['problems'].append('does not parse (SyntaxError line %s: %s)' % (e.lineno, e.msg))
        return out
    except (OSError, ValueError, MemoryError, RecursionError) as e:
        out['parses'] = False
        out['problems'].append('cannot be read (%s)' % (str(e)[:120] or type(e).__name__))
        return out
    try:
        out['about'] = _first_line(ast.get_docstring(tree))
    except Exception:                                                   # noqa: BLE001
        pass
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign) else
                   [node.target] if isinstance(node, ast.AnnAssign) and node.value is not None else [])
        for t in targets:
            name = getattr(t, 'id', None)
            if name not in ('TITLE', 'CLASSES', 'PARAMS'):
                continue
            if name == 'PARAMS':
                out['params'] = _read_params(node.value, out['problems'])
                continue
            try:
                v = ast.literal_eval(node.value)
            except Exception:                                           # noqa: BLE001
                out['problems'].append('%s is not a literal (line %d): ignored' % (name, node.lineno))
                continue
            if name == 'TITLE':
                if isinstance(v, str):
                    out['title'] = ' '.join(v.split())[:120]
                else:
                    out['problems'].append('TITLE is not text (line %d): ignored' % node.lineno)
                continue
            if not isinstance(v, (tuple, list)) or not all(isinstance(x, str) for x in v):
                out['problems'].append('CLASSES must be a tuple of texts (line %d): ignored' % node.lineno)
                continue
            good = []
            for x in v:
                if CLASS_RE.match(x):
                    if x not in good:
                        good.append(x)
                else:
                    out['problems'].append('CLASSES: %r is not a class name (a letter, then letters, digits, "_" '
                                           'or "-"): left out' % (x,))
            out['classes'] = good[:50]
    return out


def _decl(path):
    """_read_decl, cached per file by (mtime_ns, size): a saved edit is read at the next scan. None when the file is
    gone."""
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    with _MEMO_LOCK:
        hit = _MEMO.get(path)
        if hit and hit[0] == stamp:
            return hit[1]
    try:
        d = _read_decl(path)
    except Exception as e:                                              # noqa: BLE001 - one file, never the list
        d = dict(_empty_decl(), parses=False,
                 problems=['its declarations cannot be read (%s)' % (str(e)[:120] or type(e).__name__)])
    with _MEMO_LOCK:
        if len(_MEMO) > 4000:
            _MEMO.clear()
        _MEMO[path] = (stamp, d)
    return d


# ---------------------------------------------------------------- records

def _inside(real_file, real_dir):
    return os.path.normcase(os.path.dirname(real_file)) == os.path.normcase(real_dir)


def _rel(path):
    return P.rel_to_root(path)


# ---------------------------------------------------------------- names a script must not have
#
# runners/run_myscript.py puts the script's own folder FIRST on sys.path, and every script of a folder shares it - so
# my_analysis/scipy.py (or csv.py, inspect.py ...) takes the place of that module for EVERY script in the folder:
# "from scipy.optimize import curve_fit" fails with "'scipy' is not a package", pandas with "cannot import name
# QUOTE_NONNUMERIC from csv", matplotlib (and with it the runner's plt.show capture) with "cannot import name
# 'Parameter' from inspect". New script refuses such a name, and the listing marks a file dropped there by hand with
# one. Also refused: the DOS device names, which on Windows name the device whatever the extension.

_DEVICE_NAMES = frozenset(('CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'))
_DEVICE_RE = re.compile(r'(COM|LPT)[0-9\u00b9\u00b2\u00b3]\Z')
# what the runner's scripts import most, refused whatever this server's own Python has installed: the panel may run
# under another interpreter than paths.python_exe
_KEY_PACKAGES = frozenset(('numpy', 'scipy', 'matplotlib', 'pandas', 'lmfit', 'pylab', 'mpl_toolkits',
                           'pil', 'h5py', 'sympy', 'numba', 'cython', 'ipython', 'skimage', 'sklearn', 'tqdm',
                           'yaml', 'six', 'dateutil', 'pytz', 'packaging', 'setuptools', 'pip', 'site',
                           'sitecustomize', 'usercustomize', 'labmaster_analysis', 'labmaster_dataset'))
_SHADOW_LOCK = threading.Lock()
_SHADOW_MEMO = {}                    # folder -> (mtime_ns, {lower name: name}) of the runner's own path folders
_SPEC_MEMO = {}                      # (stem, my folders) -> (monotonic time, what find_spec found or '')


def _device_name(stem):
    """True for a reserved Windows device name (CON, NUL, COM1 ... - the case does not matter)."""
    u = (stem or '').upper()
    return u in _DEVICE_NAMES or bool(_DEVICE_RE.fullmatch(u))


def _module_names_in(folder):
    """{lower-case name: name} of the modules one folder of the runner's sys.path offers: *.py / *.pyd stems and
    package folders (with __init__.py). Cached by the folder's mtime; {} for one that is not there. Never raises."""
    try:
        stamp = os.stat(folder).st_mtime_ns
    except OSError:
        return {}
    with _SHADOW_LOCK:
        hit = _SHADOW_MEMO.get(folder)
        if hit and hit[0] == stamp:
            return hit[1]
    names = {}
    try:
        with os.scandir(folder) as it:
            for e in it:
                n = e.name
                try:
                    if e.is_dir():
                        if os.path.isfile(os.path.join(e.path, '__init__.py')):
                            names.setdefault(n.lower(), n)
                    elif n.lower().endswith(('.py', '.pyd', '.pyw')):
                        stem = n.rsplit('.', 1)[0].split('.', 1)[0]  # foo.cp310-win_amd64.pyd -> foo
                        if stem:
                            names.setdefault(stem.lower(), stem)
                except OSError:
                    continue
    except OSError:
        return {}
    with _SHADOW_LOCK:
        if len(_SHADOW_MEMO) > 50:
            _SHADOW_MEMO.clear()
        _SHADOW_MEMO[folder] = (stamp, names)
    return names


def _under(path, folders):
    p = os.path.normcase(os.path.normpath(os.path.realpath(path)))
    for f in folders:
        f = os.path.normcase(os.path.normpath(os.path.realpath(f)))
        if p == f or p.startswith(f.rstrip('\\/') + os.sep):
            return True
    return False


def _shadowed_module(stem, cfg=None):
    """Where a module named like `stem` lives - '' when none: a script of that name in a script folder would hide it
    from every other script there (see above). Compared without regard to case. Looked for in the runner's own
    sys.path folder (the one holding labmaster_analysis.py), the standard library and the built-in modules, the
    packages the scripts lean on (_KEY_PACKAGES), and anything this Python can import (importlib.util.find_spec of
    the top-level name, which runs no code of a package; one that errors counts as taken) - except a hit inside the
    script folders themselves: your own scripts never count against each other. Never raises.
    Only a stem that is a Python identifier can hide anything: `import x` looks for x.py or a package x, never
    x.v2.py - so such a file hides nothing, and is never looked up (find_spec of a dotted name would IMPORT its
    parent package)."""
    import sys
    stem = (stem or '').strip()
    low = stem.lower()
    if not low or not stem.isidentifier():
        return ''
    folder = helper_dir()
    if low in _module_names_in(folder):
        return '%s in %s' % (_module_names_in(folder)[low], _rel(folder))
    std = set(getattr(sys, 'stdlib_module_names', ()) or ()) | set(sys.builtin_module_names)
    for n in std:
        if n.lower() == low:
            return '%s, of the Python standard library' % n
    if low in _KEY_PACKAGES:
        return '%s, an installed package the scripts use' % low
    try:
        mine = dirs(cfg or {})
    except Exception:                                                   # noqa: BLE001
        mine = []
    # find_spec walks sys.path (about half a millisecond a name): remembered for a minute per name, so a scan of a
    # folder of many scripts stays quick; a package installed meanwhile is seen within that minute
    key = (stem, tuple(mine))
    now = time.monotonic()
    with _SHADOW_LOCK:
        hit = _SPEC_MEMO.get(key)
    if hit and now - hit[0] < 60.0:
        return hit[1]
    found = _found_spec(stem, low, mine)
    with _SHADOW_LOCK:
        if len(_SPEC_MEMO) > 2000:
            _SPEC_MEMO.clear()
        _SPEC_MEMO[key] = (now, found)
    return found


def _found_spec(stem, low, mine):
    """_shadowed_module's last look: what this Python can import as `stem` (or its lower case), '' for nothing, or
    a hit inside the script folders `mine`. Never raises."""
    import importlib.util
    for n in dict.fromkeys((stem, low)):
        if not n.isidentifier():
            continue
        try:
            spec = importlib.util.find_spec(n)
        except Exception:                                               # noqa: BLE001
            return '%s (it cannot be looked up, so it is taken to be one)' % n
        if spec is None:
            continue
        origin = spec.origin if spec.origin not in (None, 'built-in', 'frozen', 'namespace') else None
        places = list(spec.submodule_search_locations or []) + ([origin] if origin else [])
        if places and all(_under(x, mine) for x in places):
            continue                     # one of your own scripts, seen through a path that holds it
        return '%s, installed in this Python' % n
    return ''


def name_problem(name, cfg=None):
    """Why `name` (a script's file name without .py) must not be the name of a script in a script folder - '' when
    it may be. A reserved Windows device name, or the name of an importable module (_shadowed_module). Never
    raises."""
    try:
        if _device_name(name):
            return '%s is a reserved Windows device name - choose another name' % name
        where = _shadowed_module(name, cfg)
        if where:
            return ('%s is the name of an importable module (%s); a script with that name would hide it from every '
                    'other script in this folder - choose another name, e.g. %s_fit' % (name, where, name))
    except Exception:                                                   # noqa: BLE001
        pass
    return ''


def _my_record(cfg, k, folder, fname):
    """The list's entry of the file `fname` in dirs[k] (`folder`, absolute). Never raises.
    A file whose name hides an importable module (dropped there by hand - New script refuses such a name) gets a
    problem line: it may run itself, but it breaks the other scripts of the folder that import that module.
    The keys kinds / needs / is_default / alternate / default_for / alternate_for are always empty or false here
    (the page's script list draws them for other kinds of script; kept so it needs no special case)."""
    full = os.path.join(folder, fname)
    rec = {'id': 'my:%d:%s' % (k, fname), 'src': 'my', 'name': fname[:-3], 'file': _rel(full),
           'path': full, 'title': '', 'about': '', 'mtime': None, 'size': None,
           'classes': [], 'kinds': [], 'needs': [], 'params': [], 'problems': [], 'runnable': True,
           'fits': False, 'is_default': False, 'alternate': False, 'vscode': vscode_url(cfg, full),
           'default_for': [], 'alternate_for': []}
    # a name resolve refuses (fit..v2.py, ' a.py' - a file dropped there by hand) is listed with resolve's own
    # words and not runnable
    bad = my_id_problem(rec['id'])
    if bad:
        rec['problems'].append(bad)
        rec['runnable'] = False
        rec['title'] = rec['name']
        return rec
    try:
        hidden = _shadowed_module(fname[:-3], cfg)
    except Exception:                                                   # noqa: BLE001
        hidden = ''
    if hidden:
        # listed first: the page shows a row's first problem
        rec['problems'].append('its name hides an importable module (%s) from every script in this folder - rename it'
                               % hidden)
    try:
        real = os.path.realpath(full)
        if not _inside(real, os.path.realpath(folder)):
            # a link (symlink, junction) that leads out of the folder: resolve refuses it, so it is said, not run
            rec['problems'].append('it links outside %s - it is not run from here' % _rel(folder))
            rec['runnable'] = False
        st = os.stat(real)
        rec['mtime'], rec['size'] = st.st_mtime, st.st_size
    except OSError as e:
        rec['problems'].append('cannot be read (%s)' % (e.strerror or type(e).__name__))
        rec['runnable'] = False
        return rec
    d = _decl(real) if rec['runnable'] else None
    if d is None:
        rec['title'] = rec['name']
        return rec
    rec.update(title=d['title'] or rec['name'], about=d['about'], classes=list(d['classes']),
               params=[dict(p, choices=list(p['choices']) if p['choices'] is not None else None)
                       for p in d['params']])
    rec['problems'] += list(d['problems'])
    if not d['parses']:
        rec['runnable'] = False
    return rec


def _asked(values):
    if isinstance(values, str):
        values = values.split(',')
    out = []
    for v in values or ():
        v = v.strip() if isinstance(v, str) else ''
        if v and ASK_RE.match(v) and v not in out:
            out.append(v)
    return out[:50]


def _templates():
    out = []
    folder = templates_dir()
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith('.py') and TEMPLATE_RE.match(n[:-3]))
    except OSError:
        return out
    for n in names:
        tid = n[:-3]
        title = ''
        try:
            with open(os.path.join(folder, n), 'r', encoding='utf-8', errors='replace') as f:
                head = f.read(4000)
            m = re.search(r'^\s*(?:[rRuU]?)("""|\'\'\')\s*(.*?)\s*$', head, re.M)
            if m:
                title = m.group(2).strip().strip('"\'').strip()
            if not title or '{{' in title:
                t = re.search(r'^TITLE\s*=\s*[\'"]([^\'"\n]+)[\'"]', head, re.M)
                title = t.group(1).strip() if t and '{{' not in t.group(1) else ''
        except OSError:
            pass
        out.append({'id': tid, 'title': (title or TEMPLATE_TITLES.get(tid) or tid)[:120]})
    # the two the panel ships first, in that order
    order = {'plot': 0, 'fit': 1}
    out.sort(key=lambda t: (order.get(t['id'], 9), t['id']))
    return out


def scan(engine, classes=()):
    """The script list: {dirs, guard, templates, scripts}. Read fresh every call (each file's declarations cached by
    (mtime_ns, size)), so a script dropped in, edited or deleted is seen at the next call. Newest first.
    `classes`: the History group's measurement classes (a list, or a comma-separated text) - a script that declares
    one of them `fits`. Never raises."""
    cfg = getattr(engine, 'cfg', None) or {}
    ca = _asked(classes)
    out = {'dirs': [], 'guard': cfg_of(cfg)['guard'], 'templates': _templates(), 'scripts': []}
    mine, seen_dirs = [], set()
    for k, d in enumerate(dirs(cfg)):
        ent = {'k': k, 'path': d, 'rel': _rel(d), 'exists': os.path.isdir(d), 'n': 0, 'default': k == 0}
        # the folder's own "open in VS Code" link - also for an empty folder, where no script's link could be made
        # into one
        ent['vscode'] = vscode_url(cfg, d) if ent['exists'] else ''
        out['dirs'].append(ent)
        key = os.path.normcase(os.path.realpath(d))
        if not ent['exists'] or key in seen_dirs:
            if key in seen_dirs:
                ent['note'] = 'the same folder as an earlier entry - listed there'
            continue
        seen_dirs.add(key)
        try:
            names = sorted(n for n in os.listdir(d)
                           if n.endswith('.py') and not n.startswith(('_', '.'))
                           and os.path.isfile(os.path.join(d, n)))
        except OSError as e:
            ent['note'] = 'cannot be listed (%s)' % (e.strerror or type(e).__name__)
            continue
        if len(names) > MAX_SCRIPTS_PER_DIR:
            ent['note'] = '%d scripts - only the first %d by name are listed' % (len(names), MAX_SCRIPTS_PER_DIR)
            names = names[:MAX_SCRIPTS_PER_DIR]
        for n in names:
            rec = _my_record(cfg, k, d, n)
            rec['fits'] = bool(set(rec['classes']) & set(ca))
            mine.append(rec)
            ent['n'] += 1
    mine.sort(key=lambda r: -(r['mtime'] or 0))
    # each entry keeps its absolute native path - the Scripts tab's details show it. The dirs carry absolute paths
    # already, so nothing new is disclosed
    out['scripts'] = mine
    return out


# ---------------------------------------------------------------- resolve, params, new script

def my_id_problem(sid):
    """'' when `sid` is a well-formed script id - my:<k>:<file.py>, the file a bare name ending in .py that does not
    start with '_' or '.' - else why, in words. The FORM only: nothing on disk is looked at, and k is not checked
    against my_analysis.dirs (resolve does both). Never raises."""
    if not isinstance(sid, str) or not sid or len(sid) > 400:
        return 'not a script id of your own: %r - it is my:<folder number>:<file.py>' % (
            str(sid)[:80] if sid is not None else sid,)
    if '\x00' in sid:
        return 'not a script id: it holds a NUL character'
    m = ID_MY_RE.fullmatch(sid)
    if not m:
        return 'not a script id of your own: %r - it is my:<folder number>:<file.py>' % sid[:80]
    fname = m.group(2)
    if ('/' in fname or '\\' in fname or ':' in fname or '..' in fname or fname != fname.strip()
            or os.path.basename(fname) != fname or os.path.isabs(fname)):
        return ('%s: the file must be a plain file name inside the folder (no folder, "..", drive or slash in it)'
                % sid[:80])
    if not fname.endswith('.py') or len(fname) < 4:
        return '%s: only a .py file can be run' % sid[:80]
    if fname.startswith(('_', '.')):
        return '%s: a file whose name starts with "_" or "." is a helper, not a script - it is not run' % sid[:80]
    return ''


def resolve(engine, sid):
    """The record of script id `sid`, with 'path' (absolute) - ValueError, in words, for any id that is not exactly
    my:<k>:<file.py> or names a file that is not there. The file is a bare name directly inside dirs[k] whose real
    path (links followed) is still directly inside that folder: '..', a drive, a slash, a NUL, a hidden or private
    file (_x.py, .x.py) and a link leading out are all refused. The record may still be not runnable (does not
    parse) - the caller says so."""
    cfg = getattr(engine, 'cfg', None) or {}
    if not isinstance(sid, str) or not sid or len(sid) > 400:
        raise ValueError('not a script id: %r - it is my:<folder number>:<file.py>'
                         % (str(sid)[:80] if sid is not None else sid,))
    if '\x00' in sid:
        raise ValueError('not a script id: it holds a NUL character')
    m = ID_MY_RE.fullmatch(sid)
    if not m:
        raise ValueError('not a script id: %r - it is my:<folder number>:<file.py>' % sid[:80])
    k, fname = int(m.group(1)), m.group(2)
    ds = dirs(cfg)
    if not 0 <= k < len(ds):
        raise ValueError('%s: there is no folder %d of your own scripts (my_analysis.dirs has %d)'
                         % (sid[:80], k, len(ds)))
    why = my_id_problem(sid)
    if why:
        raise ValueError(why)
    folder = ds[k]
    full = os.path.join(folder, fname)
    real = os.path.realpath(full)
    if not _inside(real, os.path.realpath(folder)):
        raise ValueError('%s leads outside %s (a link?) - only a file in that folder is run' % (sid[:80], _rel(folder)))
    if not os.path.isfile(real):
        raise ValueError('%s is not there any more (%s) - press Refresh' % (fname, _rel(full)))
    rec = _my_record(cfg, k, folder, fname)
    rec['path'] = full
    return rec


def _bad(name, what):
    raise ValueError('parameter %s: %s' % (name, what))


def _check_one(p, v):
    name, typ = p['name'], p['type']
    lo, hi = p.get('min'), p.get('max')

    def rng(x):
        if lo is not None and x < lo:
            _bad(name, '%g is below its min %g' % (x, lo))
        if hi is not None and x > hi:
            _bad(name, '%g is above its max %g' % (x, hi))
    if typ == 'bool':
        if not isinstance(v, bool):
            _bad(name, 'must be true or false (got %r)' % (v,))
        return v
    if typ == 'int':
        if isinstance(v, bool) or not _finite(v) or (isinstance(v, float) and not v.is_integer()):
            _bad(name, 'must be a whole number (got %r)' % (v,))
        v = int(v)
        rng(v)
        return v
    if typ == 'float':
        if not _finite(v):
            _bad(name, 'must be a number (got %r)' % (v,))
        v = float(v)
        rng(v)
        return v
    if typ == 'str':
        if not isinstance(v, str):
            _bad(name, 'must be text (got %r)' % (v,))
        if len(v) > MAX_STR:
            _bad(name, 'is longer than %d characters' % MAX_STR)
        return v
    if typ == 'list':
        if not isinstance(v, list):
            _bad(name, 'must be a list of numbers (got %s)' % type(v).__name__)
        if len(v) > MAX_LIST:
            _bad(name, 'has %d numbers - at most %d' % (len(v), MAX_LIST))
        if not all(_finite(x) for x in v):
            _bad(name, 'must hold numbers only')
        for x in v:
            rng(x)
        return list(v)
    if typ == 'choice':
        for c in p.get('choices') or []:
            if _same_choice(c, v):
                return c
        _bad(name, '%r is not one of %s' % (v, ', '.join(repr(c) for c in (p.get('choices') or [])[:20])))
    _bad(name, 'has an unknown type %r' % typ)


def check_params(rec, given):
    """A clean {name: value} for script record `rec` - every declared parameter, the missing ones at their default.
    ValueError for an unknown name, a wrong type, a value outside min/max or not one of the choices."""
    if given is None:
        given = {}
    if not isinstance(given, dict):
        raise ValueError('params must be an object {name: value} (got %s)' % type(given).__name__)
    decl = {p['name']: p for p in (rec or {}).get('params') or [] if isinstance(p, dict) and p.get('name')}
    unknown = [k for k in given if k not in decl]
    if unknown:
        raise ValueError('unknown parameter%s %s - %s declares %s' % (
            's' if len(unknown) > 1 else '', ', '.join(repr(str(k)[:40]) for k in unknown[:10]),
            (rec or {}).get('name') or 'the script', ', '.join(decl) if decl else 'none'))
    out = {}
    for name, p in decl.items():
        out[name] = _check_one(p, given[name]) if name in given else \
            (list(p['default']) if isinstance(p['default'], (list, tuple)) else p['default'])
    return out


def _classes_arg(classes):
    if classes is None:
        return []
    if isinstance(classes, str):
        classes = [c for c in classes.split(',')]
    if not isinstance(classes, (list, tuple)):
        raise ValueError('classes must be a list of measurement class names')
    out = []
    for c in classes:
        c = c.strip() if isinstance(c, str) else c
        if not isinstance(c, str) or not CLASS_RE.match(c):
            raise ValueError('%r is not a measurement class name (a letter, then letters, digits, "_" or "-")' % (c,))
        if c not in out:
            out.append(c)
    if len(out) > 20:
        raise ValueError('at most 20 classes')
    return out


def new_script(engine, name, template, classes=()):
    """Write dirs[0]/<name>.py from templates/my_analysis/<template>.py, its placeholders filled in ({{TITLE}},
    {{CLASSES}} a tuple literal, {{HELPER_DIR}} the folder holding labmaster_analysis.py for a run outside the panel,
    {{NAME}}); the new file's record (with 'vscode'). ValueError for a bad name / template / class, FileExistsError
    when the file is there already - nothing is ever replaced (the file is opened with 'x')."""
    cfg = getattr(engine, 'cfg', None) or {}
    if not isinstance(name, str) or not NEW_NAME_RE.match(name):
        raise ValueError('a script name is a letter, then letters, digits or "_" (at most 61), e.g. fit_sweep '
                         '(got %r)' % (str(name)[:80] if name is not None else name,))
    # a device name, or the name of a module it would hide from every script of the folder - refused before
    # anything is written
    why = name_problem(name, cfg)
    if why:
        raise ValueError(why)
    if not isinstance(template, str) or not TEMPLATE_RE.match(template):
        raise ValueError('not a template: %r' % (str(template)[:60] if template is not None else template,))
    src = os.path.join(templates_dir(), template + '.py')
    if not os.path.isfile(src):
        have = ', '.join(t['id'] for t in _templates()) or 'none'
        raise ValueError('no template %r (there are: %s)' % (template, have))
    cl = _classes_arg(classes)
    with open(src, 'r', encoding='utf-8', newline='') as f:
        text = f.read()
    title = name.replace('_', ' ').strip()
    text = (text.replace('{{TITLE}}', title)
            .replace('{{CLASSES}}', repr(tuple(cl)))
            .replace('{{HELPER_DIR}}', helper_dir().replace('\\', '/'))
            .replace('{{NAME}}', name))
    folder = dirs(cfg)[0]
    os.makedirs(folder, exist_ok=True)
    fname = name + '.py'
    full = os.path.join(folder, fname)
    if os.path.lexists(full):
        raise FileExistsError('%s is already in %s - choose another name' % (fname, _rel(folder)))
    try:
        with open(full, 'x', encoding='utf-8', newline='') as f:
            f.write(text)
    except FileExistsError:
        raise FileExistsError('%s is already in %s - choose another name' % (fname, _rel(folder)))
    rec = _my_record(cfg, 0, folder, fname)
    rec['fits'] = bool(set(rec['classes']) & set(cl))
    rec['created'] = time.time()
    return rec
