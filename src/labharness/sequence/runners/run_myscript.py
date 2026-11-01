"""
run_myscript.py - runs ONE analysis script of yours for the panel (0.1.7).

A script in my_analysis/ is chosen in History and run by the engine through this file, one process per run (so an
edited script is picked up by the next run, with nothing to reload):

    python -u run_myscript.py --script ABS --out ABS_OUT --datasets-file TXT [--params-file JSON]
                              [--guard] [--guard-ports P,P,...] [--label TEXT] [--id SID]

The contract the script gets, simplest first:
  * sys.argv[1:] = the dataset folders (absolute), sys.argv[0] = the script - enough to write a script;
  * the current folder is ABS_OUT (created here): whatever the script writes by a relative name lands there;
  * environment: LABMASTER_RUNNER=1, LABMASTER_OUT, LABMASTER_DATASETS (JSON list), LABMASTER_PARAMS (JSON object),
    LABMASTER_SCRIPT, LABMASTER_DATA_ROOT (the engine's), MPLBACKEND=Agg, and LABMASTER_DATASETS_FILE /
    LABMASTER_PARAMS_FILE (the lists may be too long for an environment variable - Windows refuses one over 32767
    characters - and then the JSON one is left empty: labmaster_analysis reads the file instead);
    PYTHONIOENCODING=utf-8:replace and PYTHONUTF8=1 for a child Python, which prints into the same pipe;
  * Python's UTF-8 mode (-X utf8) in the script's own process: subprocess.run(..., text=True) decodes what a child
    Python prints (UTF-8, above) as UTF-8, and open() without an encoding reads and writes UTF-8 - not the PC's
    legacy code page (_utf8_mode_or_rerun says why and how);
  * sys.path, first to last: the script's folder, then the folder holding labmaster_analysis.py (the root's
    scripts/, else the panel's) - this runner's own folder is taken OFF it;
  * stdout / stderr in UTF-8 (errors='replace'), line-buffered: a print('tau = 3 µs') never kills a run on a
    PC whose pipes use a legacy code page;
  * matplotlib on Agg; plt.show() saves every open figure not saved yet and closes it, and every figure still open
    at the end is saved (labmaster_analysis._save_figure: the slug of the figure's label, else figure_01, ...; dpi
    120, bbox_inches='tight', never over an existing file). Figure.savefig is wrapped only to MARK a figure the
    script saved itself, so it is not saved a second time. Without matplotlib none of this happens;
  * exit status: a normal end and SystemExit(0 / None) -> 0; SystemExit(n) -> n (an int other than 0),
    SystemExit('text') -> the text printed, 1; an exception -> its traceback (the runner's own frames left out), 3.
    A command line or input this runner cannot use -> 2.

At the end - whatever happened, once ABS_OUT exists - the runner writes ABS_OUT/analysis.json (format "my-analysis",
version 1; read by panel_server._manifest_record and History's analysis folders) and prints ONE last stdout line
    ##PANEL {"event": "analysis", "analysis": "my:<name>", ..., "verdict": "ok"|"poor"|"failed", ...}
which is what the engine records. 'poor' only appears when the script said so with
labmaster_analysis.verdict('poor', why).

How this runner and labmaster_analysis share state: the INPUTS go in the environment (above), so a child Python of
the script sees them too; the OUTPUTS (labmaster_analysis.result / verdict / used, the figures saved) stay in the
module, which this runner loads from its file and registers as sys.modules['labmaster_analysis'] before the script
runs, so the script's `import labmaster_analysis` is that very module and the runner reads its _STATE afterwards.

With --guard the instrument guard (myscript_guard.py) is installed in this process before the script runs, and
runners/guard_site goes first on PYTHONPATH with LABMASTER_GUARD=1, so a child Python is guarded too (its
sitecustomize.py). Not guarded: a program that is not Python, a Python started with -I / -E / -S. The guard is a
safety net, not a sandbox (myscript_guard.py says why).

Not covered: a script that ends the process itself with os._exit() leaves no analysis.json and no ##PANEL line
(the engine then records the exit code alone).
"""
import os

# Before anything can import matplotlib, and inherited by whatever the script starts: on an interactive backend a
# plt.show() would wait for someone to close a window nobody can see.
os.environ['MPLBACKEND'] = 'Agg'

import argparse                   # noqa: E402
import hashlib                    # noqa: E402
import importlib.util             # noqa: E402
import io                         # noqa: E402
import json                       # noqa: E402
import math                       # noqa: E402
import runpy                      # noqa: E402
import subprocess                 # noqa: E402
import sys                        # noqa: E402
import time                       # noqa: E402
import traceback                  # noqa: E402

RUNNERS = os.path.dirname(os.path.abspath(__file__))           # <panel>/runners
PANEL = os.path.dirname(RUNNERS)                                # <panel>
HELPER_NAME = 'labmaster_analysis.py'


def _panel_paths():
    """The panel's own panel_paths (stdlib-only), loaded under a private name and NOT registered as
    sys.modules['panel_paths'], so a script's own `import panel_paths` still meets the guard's refusal of the
    panel's modules. None when it cannot be loaded."""
    try:
        spec = importlib.util.spec_from_file_location('_labmaster_runner_panel_paths',
                                                      os.path.join(PANEL, 'paths.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:                                                  # noqa: BLE001
        return None


_PP = _panel_paths()
ROOT = _PP.ROOT if _PP is not None else PANEL                   # the root exactly as the engine finds it


def _helper_dir():
    """The folder holding labmaster_analysis.py: the root's scripts/ when it has the helper, else the panel's own
    (panel_myscripts.helper_dir, the same rule)."""
    for base in (ROOT, PANEL):
        d = os.path.join(base, 'scripts')
        if os.path.isfile(os.path.join(d, HELPER_NAME)):
            return os.path.normpath(d)
    return os.path.normpath(os.path.join(PANEL, 'scripts'))


HELPER_DIR = _helper_dir()

FORMAT = 'my-analysis'
VERSION = 1
ENV_MAX = 30000                 # longer than this goes by file only (Windows' limit is 32767 per variable)
PANEL_RESULTS_MAX = 40          # scalar results carried on the ##PANEL line
LIST_MAX = 2000                 # output files listed in analysis.json, per kind
# What counts as a figure (outputs.figures, n_figures) must match what the panel can show as one:
# panel_server's /api/analysis/figures lists only '.png' (and /data/ will not serve .svg - it can carry script -
# nor .jpg / .gif / .webp). A labmaster_analysis.savefig(fig, 'fit.svg') is still written; it is listed under
# outputs.files, not counted as a figure with no thumbnail.
FIGURE_EXT = ('.png',)
EXIT_BAD_INPUT = 2
EXIT_CRASH = 3

_STDOUT = None                  # the stream the ##PANEL line goes to, whatever the script does to sys.stdout
_FIG_PROBLEMS = []              # a figure plt.show() could not save, for analysis.json


# --------------------------------------------------------------------------- output streams
class _Tracked(object):
    """sys.stdout as the script sees it: the real stream, plus whether the last thing written ended a line - so
    the ##PANEL line is always a line of its own even after print('...', end='')."""

    def __init__(self, stream):
        self._s = stream
        self.at_line_start = True

    def write(self, text):
        n = self._s.write(text)
        if text:
            self.at_line_start = text.endswith('\n')
        return n

    def writelines(self, lines):
        for ln in lines:
            self.write(ln)

    def __getattr__(self, k):
        return getattr(self._s, k)


_RERUN = 'LABMASTER_RUNNER_UTF8_RERUN'  # set only for the one rerun below, and taken off again at its start


def _utf8_mode_or_rerun():
    """None: go on in this process, which is in UTF-8 mode. Otherwise this runner is run again as
    `python -X utf8 -u run_myscript.py <the same arguments>` - same stdin / stdout / stderr, same environment - and
    its exit status is returned, for __main__ to exit with.

    Why: the script runs IN this process, and a child Python it starts prints UTF-8 (PYTHONIOENCODING above). Out of
    UTF-8 mode, subprocess.run(..., text=True) decodes that pipe with the locale's code page (cp1252 on many Windows
    PCs) and a byte it cannot decode kills the reader thread: r.stderr comes back None and the script falls over on
    it. reconfigure() fixes this process's own stdout / stderr only (_utf8_streams), not how a pipe is decoded; that
    default is fixed at start-up and only -X utf8 / PYTHONUTF8 change it - the environment variable is read before
    any of this code runs, hence the rerun.

    A start with -X utf8 already, or PYTHONUTF8=1 inherited, costs nothing. The rerun is one more Python start
    (~0.1 s); the engine's Stop / timeout end it with the parent (the whole process tree). If the rerun cannot start,
    or came back still not in UTF-8 mode, this process goes on as before, with a note on stderr.

    What UTF-8 mode changes for a script: text a child Python prints decodes; open() without an encoding reads and
    writes UTF-8. A script that reads a cp1252 / gbk text file has to name that encoding."""
    rerun = os.environ.pop(_RERUN, None)
    if sys.flags.utf8_mode:
        return None
    note = None
    if rerun:
        note = 'the rerun with -X utf8 is still not in UTF-8 mode'
    elif not sys.executable:
        note = 'no Python executable to rerun with -X utf8'
    else:
        cmd = [sys.executable, '-X', 'utf8', '-u', os.path.abspath(__file__)] + sys.argv[1:]
        try:
            p = subprocess.Popen(cmd, env=dict(os.environ, **{_RERUN: '1'}))
        except OSError as e:
            note = 'the rerun with -X utf8 could not start (%s)' % e
        else:
            while True:
                try:
                    return p.wait()
                except KeyboardInterrupt:
                    # Ctrl+C in a console reaches the rerun as well, which ends itself (its analysis.json, its
                    # ##PANEL line); subprocess.call would kill it in the middle of that
                    continue
    print('note: %s - text a child Python prints may not decode in the script' % note, file=sys.stderr)
    return None


def _utf8_streams():
    """stdout and stderr as UTF-8 with errors='replace', line-buffered. A console keeps its code page (a person
    running this by hand), still with errors='replace'."""
    global _STDOUT
    for s in (sys.stdout, sys.stderr):
        try:
            if s is None or not hasattr(s, 'reconfigure'):
                continue
            if s.isatty():
                s.reconfigure(errors='replace', line_buffering=True)
            else:
                s.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
        except Exception:                                              # noqa: BLE001
            pass
    if sys.stdout is not None:
        sys.stdout = _STDOUT = _Tracked(sys.stdout)


def _flush():
    for s in (sys.stdout, sys.stderr, _STDOUT):
        try:
            s.flush()
        except Exception:                                              # noqa: BLE001
            pass


# --------------------------------------------------------------------------- small helpers
def _clean(v, depth=0):
    """Strict JSON: NaN / inf -> None, numpy scalars and small arrays as Python values, the rest repr'd (200)."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if depth < 6 and isinstance(v, dict):
        return {str(k): _clean(x, depth + 1) for k, x in v.items()}
    if depth < 6 and isinstance(v, (list, tuple)):
        return [_clean(x, depth + 1) for x in v]
    item = getattr(v, 'item', None)
    if callable(item) and getattr(v, 'shape', None) == ():
        try:
            return _clean(item(), depth)
        except Exception:                                              # noqa: BLE001
            pass
    r = repr(v)
    return r if len(r) <= 200 else r[:197] + '...'


def _rel_root(path):
    """`path` relative to the root with '/', when it is under it; else absolute."""
    try:
        if os.path.commonpath([os.path.normcase(os.path.abspath(path)), os.path.normcase(ROOT)]) == \
                os.path.normcase(ROOT):
            return os.path.relpath(os.path.abspath(path), ROOT).replace('\\', '/')
    except ValueError:                                  # another drive
        pass
    return os.path.abspath(path)


def _sha256(path):
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _read_datasets(path):
    """The dataset folders of --datasets-file: one per line, or a JSON list (a line is never a comment, a dataset
    folder's name starts with '#'). Absolute, in the order given."""
    with io.open(path, encoding='utf-8-sig') as f:
        text = f.read()
    if text.lstrip().startswith('['):
        got = json.loads(text)
        if not isinstance(got, list):
            raise ValueError('not a JSON list')
        items = [str(p) for p in got]
    else:
        items = text.splitlines()
    return [os.path.abspath(p.strip()) for p in items if p and p.strip()]


def _read_params(path):
    if not path:
        return {}
    with io.open(path, encoding='utf-8-sig') as f:
        text = f.read()
    got = json.loads(text) if text.strip() else {}
    if not isinstance(got, dict):
        raise ValueError('the parameters are not a JSON object')
    return got


class _ArgError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _ArgError(message)


def _parse(argv):
    ap = _Parser(prog='run_myscript.py', description='Run one analysis script for the panel.')
    ap.add_argument('--script', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--datasets-file', required=True)
    ap.add_argument('--params-file')
    ap.add_argument('--guard', action='store_true')
    ap.add_argument('--guard-ports')              # the panel's live port(s), refused on loopback too
    ap.add_argument('--label')
    ap.add_argument('--id')
    # '--label -3 dB dip' must not read as a missing value: every value-taking option is joined to the token
    # after it ('--label=-3 dB dip'), so a value that starts with '-' is still that option's value
    joined, i = [], 0
    valued = ('--script', '--out', '--datasets-file', '--params-file', '--guard-ports', '--label', '--id')
    while i < len(argv):
        if argv[i] in valued and i + 1 < len(argv):
            joined.append('%s=%s' % (argv[i], argv[i + 1]))
            i += 2
        else:
            joined.append(argv[i])
            i += 1
    return ap.parse_args(joined)


def _load_module(name, path):
    """Load `path` as module `name` and register it in sys.modules (the same object for every later import)."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


# --------------------------------------------------------------------------- matplotlib
_MPL_NOTED = []


def _mpl_import_note(e):
    """Say on stderr why matplotlib did not import - unless it is simply not installed, which is a supported case
    (then nothing about figures applies). Anything else (a broken install, a file in the script's folder shadowing
    one of its imports) would otherwise turn figure capture off without a word."""
    if isinstance(e, ImportError) and getattr(e, 'name', None) == 'matplotlib':
        return
    if _MPL_NOTED:                                                     # said once (the preload and the patch)
        return
    _MPL_NOTED.append(e)
    try:
        sys.stderr.write('my analysis: matplotlib could not be loaded, so figures are not captured: %s: %s\n'
                         % (type(e).__name__, e))
        sys.stderr.flush()
    except Exception:                                                  # noqa: BLE001
        pass


def _preload_matplotlib():
    """Import matplotlib (Agg) and pyplot BEFORE sys.path puts the script's own folder first, so a file there named
    like one of matplotlib's imports (csv.py, inspect.py, ...) cannot break the runner's figure capture. The guard
    is installed by then, so this import is checked like the script's. Never raises; _patch_matplotlib later finds
    the modules already loaded."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.figure                                       # noqa: F401
        import matplotlib.pyplot                                       # noqa: F401
    except Exception as e:                                             # noqa: BLE001
        _mpl_import_note(e)


def _patch_matplotlib(helper):
    """Agg, plt.show() -> save the unsaved open figures and close them, Figure.savefig marks a figure saved.
    Returns pyplot, or None when matplotlib is not there (then nothing about figures applies)."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.figure as mfigure
        import matplotlib.pyplot as plt
    except Exception as e:                                             # noqa: BLE001
        _mpl_import_note(e)
        return None
    orig_savefig = mfigure.Figure.savefig
    if not getattr(orig_savefig, 'labmaster_runner', False):
        def savefig(self, *args, **kwargs):
            r = orig_savefig(self, *args, **kwargs)
            try:
                if not getattr(self, '_labmaster_saved', None):
                    self._labmaster_saved = True
            except Exception:                                          # noqa: BLE001
                pass
            return r
        savefig.labmaster_runner = True
        savefig.__wrapped__ = orig_savefig
        savefig.__doc__ = orig_savefig.__doc__
        mfigure.Figure.savefig = savefig

    def show(*args, **kwargs):
        """The panel's plt.show(): every open figure not saved yet is saved into the output folder, then all
        are closed (there is no window to show them in)."""
        _save_open(plt, helper, _FIG_PROBLEMS)
        plt.close('all')
    show.labmaster_runner = True
    plt.show = show
    return plt


def _save_open(plt, helper, problems):
    """Save every open figure not saved yet, in figure-number order. Returns the paths saved."""
    saved = []
    try:
        nums = list(plt.get_fignums())
    except Exception:                                                  # noqa: BLE001
        return saved
    for n in nums:
        try:
            fig = plt.figure(n)
            if getattr(fig, '_labmaster_saved', None):
                continue
            saved.append(helper._save_figure(fig))
        except Exception as e:                                         # noqa: BLE001
            problems.append('figure %s could not be saved: %s: %s' % (n, type(e).__name__, str(e)[:200]))
            try:
                print('figure %s could not be saved: %s: %s' % (n, type(e).__name__, e), file=sys.stderr)
            except Exception:                                          # noqa: BLE001
                pass
    return saved


# --------------------------------------------------------------------------- the run
def _trimmed_tb(exc):
    """The traceback from the script's own first frame on: this runner's and runpy's frames are noise."""
    skip = {os.path.normcase(os.path.abspath(__file__)), os.path.normcase(os.path.abspath(runpy.__file__))}
    tb = exc.__traceback__
    while tb is not None and os.path.normcase(os.path.abspath(tb.tb_frame.f_code.co_filename)) in skip:
        tb = tb.tb_next
    return tb


def _last_line(exc):
    try:
        lines = ''.join(traceback.format_exception_only(type(exc), exc)).strip().splitlines()
        return (lines[-1] if lines else type(exc).__name__)[:300]
    except Exception:                                                  # noqa: BLE001
        return type(exc).__name__


def _run_script(script, problems, notes):
    """Run the script as __main__ in this process. Returns its exit status. What went wrong goes first in
    `problems` (an exception's last line, a sys.exit message); a bare sys.exit(n) is only a note at the end
    (`notes`): the script's own reasons (labmaster_analysis.verdict('failed', why)) say more and come before it."""
    try:
        runpy.run_path(script, run_name='__main__')
        return 0
    except SystemExit as e:
        code = e.code
        if code is None or (code == 0 and not isinstance(code, str)):
            return 0
        if isinstance(code, int):
            notes.append('the script ended with sys.exit(%d)' % code)
            return int(code)
        # sys.exit('message'): Python prints the message and ends with 1
        text = str(code)
        try:
            print(text, file=sys.stderr)
        except Exception:                                              # noqa: BLE001
            pass
        problems.insert(0, ('sys.exit: %s' % text)[:300])
        return 1
    except BaseException as e:                                         # noqa: BLE001 - KeyboardInterrupt too
        try:
            traceback.print_exception(type(e), e, _trimmed_tb(e), file=sys.stderr)
        except Exception:                                              # noqa: BLE001
            pass
        problems.insert(0, _last_line(e))
        return EXIT_CRASH


def _outputs(out):
    """figures (the PNGs the panel shows, FIGURE_EXT) and files, relative to `out` with '/', analysis.json left out."""
    figs, files, more = [], [], 0
    for d, dirs, names in os.walk(out):
        dirs.sort()
        for n in sorted(names):
            rel = os.path.relpath(os.path.join(d, n), out).replace('\\', '/')
            if rel in ('analysis.json',) or rel.startswith('analysis.json.'):
                continue
            lst = figs if os.path.splitext(n)[1].lower() in FIGURE_EXT else files
            if len(lst) < LIST_MAX:
                lst.append(rel)
            else:
                more += 1
    return figs, files, more


def _free_name(path):
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    n = 2
    while os.path.exists('%s_%d%s' % (root, n, ext)):
        n += 1
    return '%s_%d%s' % (root, n, ext)


def _write_manifest(out, m):
    """analysis.json, strict JSON in UTF-8, written to a temporary name and moved into place."""
    path = os.path.join(out, 'analysis.json')
    tmp = path + '.tmp'
    with io.open(tmp, 'w', encoding='utf-8') as f:
        f.write(json.dumps(m, indent=1, ensure_ascii=False, allow_nan=False))
    os.replace(tmp, path)
    return path


def _panel_line(ev):
    """The one last stdout line the engine reads. ASCII JSON, so no code page can spoil it."""
    _flush()
    s = _STDOUT or sys.stdout
    try:
        lead = '' if getattr(s, 'at_line_start', True) else '\n'
        s.write(lead + '##PANEL ' + json.dumps(_clean(ev), allow_nan=False) + '\n')
        s.flush()
    except Exception:                                                  # noqa: BLE001
        try:
            sys.__stdout__.write('##PANEL ' + json.dumps(_clean(ev)) + '\n')
            sys.__stdout__.flush()
        except Exception:                                              # noqa: BLE001
            pass


def _scalars(results, limit=PANEL_RESULTS_MAX):
    out = {}
    for k, v in results.items():
        if len(out) >= limit:
            break
        if v is None or isinstance(v, (bool, int, float)):
            out[k] = v
        elif isinstance(v, str):
            out[k] = v if len(v) <= 200 else v[:197] + '...'
    return out


def _bail(why, a=None, out=None):
    """A run that could not start: say why on the ##PANEL line (and in analysis.json when the folder exists)."""
    script = os.path.abspath(a.script) if a is not None and a.script else None
    name = os.path.splitext(os.path.basename(script))[0] if script else '?'
    print('cannot run the script: %s' % why, file=sys.stderr)
    ev = {'event': 'analysis', 'analysis': 'my:' + name, 'script': _rel_root(script) if script else None,
          'group': os.path.basename(out) if out else None, 'folder': out, 'verdict': 'failed',
          'problems': [str(why)[:300]], 'n_datasets': 0, 'n_figures': 0,
          'manifest': None, 'results': {'quality': 'failed'}, 'exit_code': EXIT_BAD_INPUT}
    if out and os.path.isdir(out):
        now = time.time()
        try:
            ev['manifest'] = _write_manifest(out, _clean({
                'format': FORMAT, 'version': VERSION, 'analysis': ev['analysis'], 'group': ev['group'],
                'script': {'file': script, 'sha256': _sha256(script) if script else None, 'mtime': None,
                           'id': a.id if a is not None else None},
                'params': {}, 'label': (a.label if a is not None else None) or name,
                'when': time.strftime('%Y-%m-%dT%H:%M:%S'), 'started': now, 'finished': now,
                'inputs': {'paths': [], 'used_paths': [], 'n': 0},
                'outputs': {'figures': [], 'files': []}, 'results': {'quality': 'failed'},
                'verdict': 'failed', 'problems': ev['problems'], 'exit_code': EXIT_BAD_INPUT}))
        except (OSError, ValueError):
            pass
    _panel_line(ev)
    return EXIT_BAD_INPUT


def main(argv=None):
    _utf8_streams()
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        a = _parse(argv)
    except _ArgError as e:
        # still name the script on the line when it was given, so the log says which run this was
        given = argv[argv.index('--script') + 1] if '--script' in argv[:-1] else None
        return _bail('bad command line: %s' % e, argparse.Namespace(script=given, id=None, label=None))
    started = time.time()
    when = time.strftime('%Y-%m-%dT%H:%M:%S')
    script = os.path.abspath(a.script)
    out = os.path.abspath(a.out)
    name = os.path.splitext(os.path.basename(script))[0]
    analysis = 'my:' + name
    try:
        os.makedirs(out, exist_ok=True)
    except OSError as e:
        return _bail('the output folder cannot be made: %s' % e, a)
    if not os.path.isfile(script):
        return _bail('no such script: %s' % script, a, out)
    try:
        paths = _read_datasets(a.datasets_file)
    except (OSError, ValueError) as e:
        return _bail('the dataset list cannot be read (%s): %s' % (a.datasets_file, e), a, out)
    try:
        params = _read_params(a.params_file)
    except (OSError, ValueError) as e:
        return _bail('the parameters cannot be read (%s): %s' % (a.params_file, e), a, out)
    sha = _sha256(script)
    try:
        mtime = os.path.getmtime(script)
    except OSError:
        mtime = None

    # --- the environment the script (and any Python it starts) sees
    ds_json = json.dumps(paths, ensure_ascii=False)
    pa_json = json.dumps(params, ensure_ascii=False)
    env = os.environ
    env.update({
        'LABMASTER_RUNNER': '1', 'LABMASTER_OUT': out, 'LABMASTER_SCRIPT': script,
        'LABMASTER_DATASETS': ds_json if len(ds_json) <= ENV_MAX else '',
        'LABMASTER_DATASETS_FILE': os.path.abspath(a.datasets_file),
        'LABMASTER_PARAMS': pa_json if len(pa_json) <= ENV_MAX else '',
        'LABMASTER_PARAMS_FILE': os.path.abspath(a.params_file) if a.params_file else '',
        'MPLBACKEND': 'Agg'})
    # a child Python prints into the same pipe, which the engine reads as UTF-8
    env['PYTHONIOENCODING'] = 'utf-8:replace'
    # ... and runs in UTF-8 mode itself, as this process does (_utf8_mode_or_rerun), so what ITS child Pythons print
    # decodes there too. A PYTHONUTF8=0 from outside is overridden on purpose.
    env['PYTHONUTF8'] = '1'
    if len(pa_json) > ENV_MAX and not a.params_file:
        return _bail('the parameters are too long for the environment and came without a file', a, out)

    # --- the guard (before the script's first import), and the children's
    guard = None
    if a.guard:
        # the ports this panel really uses (the engine passes them with --guard): refused on loopback too. They go
        # in the environment, so a guarded child Python refuses them too. A port already in the environment stays:
        # the list only grows. LABMASTER_GUARD_MODULES / LABMASTER_GUARD_DLLS (your own driver names, from
        # config.json my_analysis) come in the environment from the engine.
        if a.guard_ports:
            old = [t.strip() for t in (env.get('LABMASTER_GUARD_PORTS') or '').split(',') if t.strip()]
            new = [t.strip() for t in a.guard_ports.split(',') if t.strip()]
            env['LABMASTER_GUARD_PORTS'] = ','.join(old + [t for t in new if t not in old])
        try:
            guard = _load_module('myscript_guard', os.path.join(RUNNERS, 'myscript_guard.py'))
            guard.install()
            guard.export_to_children(env)
        except Exception as e:                                         # noqa: BLE001
            # fail closed: a run that was to be guarded and cannot be does not run
            return _bail('the instrument guard could not be installed: %s: %s' % (type(e).__name__, e), a, out)

    # --- matplotlib first, while sys.path does not yet start with the script's folder (_preload_matplotlib)
    _preload_matplotlib()

    # --- sys.path: the script's folder, the helper's folder, then the rest - not this folder
    here = os.path.normcase(RUNNERS)
    first = [os.path.dirname(script), HELPER_DIR]
    keys = set(os.path.normcase(os.path.abspath(p)) for p in first)
    rest = [p for p in sys.path
            if os.path.normcase(os.path.abspath(p or '.')) not in keys | {here}]
    sys.path[:] = first + rest

    # --- labmaster_analysis (the very module the script imports), matplotlib
    try:
        helper = _load_module('labmaster_analysis', os.path.join(HELPER_DIR, HELPER_NAME))
        helper._reset()
    except Exception as e:                                             # noqa: BLE001
        return _bail('%s cannot be loaded from %s: %s: %s' % (HELPER_NAME, HELPER_DIR, type(e).__name__, e), a, out)
    plt = _patch_matplotlib(helper)

    # --- run it
    problems = []
    print('my analysis: %s  (%d dataset(s)) -> %s' % (_rel_root(script), len(paths), out))
    if guard is not None:
        print('  under the instrument guard (instrument drivers, hardware DLLs and the network refused)')
    _flush()
    sys.argv[:] = [script] + paths
    os.chdir(out)
    notes = []
    rc = _run_script(script, problems, notes)
    _flush()

    # --- whatever is still open is saved, then everything is closed
    if plt is not None:
        _save_open(plt, helper, _FIG_PROBLEMS)
        try:
            plt.close('all')
        except Exception:                                              # noqa: BLE001
            pass

    # --- the record. Problems: what ended the run, the script's own reasons, how it exited, the figures.
    st = getattr(helper, '_STATE', {}) or {}
    v = st.get('verdict')
    verdict = 'failed' if rc != 0 else (v if v in ('ok', 'poor', 'failed') else 'ok')
    for p in list(st.get('problems') or []) + notes + _FIG_PROBLEMS:
        if p not in problems:
            problems.append(p)
    if rc == 0 and verdict == 'failed' and not problems:
        problems.append('the script said its result failed (labmaster_analysis.verdict)')
    norm = {os.path.normcase(os.path.abspath(p)): p for p in paths}
    used = paths
    if st.get('used') is not None:
        used = [norm[k] for k in (os.path.normcase(os.path.abspath(p)) for p in st['used']) if k in norm]
        other = len(st['used']) - len(used)
        if other:
            problems.append('labmaster_analysis.used() named %d folder(s) that were not among the datasets given '
                            '- left out' % other)
    results = {'quality': verdict}
    for k, x in (st.get('results') or {}).items():
        if k != 'quality':
            results[str(k)] = x
    finished = time.time()
    try:
        os.makedirs(out, exist_ok=True)                 # the script may have removed it
        mine = os.path.join(out, 'analysis.json')
        if os.path.isfile(mine):                        # the script wrote one of its own: kept, renamed
            kept = _free_name(os.path.join(out, 'analysis_script.json'))
            os.replace(mine, kept)
            problems.append('the script wrote its own analysis.json - kept as %s' % os.path.basename(kept))
        figs, files, more = _outputs(out)
    except OSError as e:
        figs, files, more = [], [], 0
        problems.append('the output folder cannot be read: %s' % e)
    m = _clean({
        'format': FORMAT, 'version': VERSION, 'analysis': analysis, 'group': os.path.basename(out),
        'script': {'file': script, 'sha256': sha, 'mtime': mtime, 'id': a.id or None},
        'params': params, 'label': a.label or name, 'when': when,
        'started': round(started, 3), 'finished': round(finished, 3),
        'elapsed_s': round(finished - started, 3),
        'inputs': {'paths': paths, 'used_paths': used, 'n': len(paths)},
        'outputs': {'figures': figs, 'files': files, 'more': more},
        'results': results, 'verdict': verdict, 'problems': problems[:50], 'exit_code': rc,
        'guard': bool(a.guard)})
    manifest = None
    try:
        manifest = _write_manifest(out, m)
    except (OSError, ValueError) as e:
        problems.append('analysis.json cannot be written: %s' % e)
        print('analysis.json cannot be written: %s' % e, file=sys.stderr)
    _panel_line({
        'event': 'analysis', 'analysis': analysis, 'format': FORMAT, 'script': _rel_root(script),
        'id': a.id or None, 'group': os.path.basename(out), 'folder': out, 'verdict': verdict,
        'problems': [str(p)[:300] for p in problems[:20]], 'n_datasets': len(paths), 'n_used': len(used),
        'n_figures': len(figs), 'manifest': manifest, 'results': _scalars(m['results']), 'exit_code': rc})
    return rc


if __name__ == '__main__':
    _rc = _utf8_mode_or_rerun()
    sys.exit(main() if _rc is None else _rc)
