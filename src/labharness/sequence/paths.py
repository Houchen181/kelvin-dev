"""
panel_paths.py - paths, config and small JSON helpers for labMaster.

Everything is resolved RELATIVE TO THE ROOT (see _find_root): the panel folder itself unless
LABMASTER_ROOT or a ".labmaster-root" marker file says otherwise. A clone of the repository is
self-contained: labMaster/{scripts, data, state} - data/ and state/ are made on first use.
One exception keeps the shipped examples working under another root: a path in scripts/ is
taken from the panel's own scripts/ while the root has no scripts/ folder (resolve).
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import time

# --- Kelvin (v1-A): vendored from labMaster. The engine's DATA root is ~/Kelvin/sequence
# (KELVIN_SEQUENCE_DIR overrides); the code, runners and VERSION stay beside this file, in
# labharness/sequence/. Nothing below reads LABMASTER_ROOT or a .labmaster-root marker. ---
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
PANEL_DIR = (os.environ.get('KELVIN_SEQUENCE_DIR') or '').strip() or os.path.join(
    os.path.expanduser('~'), 'Kelvin', 'sequence')
PANEL_DIR = os.path.abspath(os.path.expanduser(PANEL_DIR))
ROOT_MARKER = '.labmaster-root'


def _find_root(start):
    """The folder relative paths (scripts, data, work_dir) are resolved against:
      1. the environment variable LABMASTER_ROOT, when it names a folder;
      2. else the nearest folder, from the panel folder upwards, that holds a file named
         ".labmaster-root" (a lab that keeps its scripts and data beside the panel folder);
      3. else the panel folder itself."""
    env = (os.environ.get('LABMASTER_ROOT') or '').strip()
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    d = os.path.abspath(start)
    for _ in range(8):
        if os.path.isfile(os.path.join(d, ROOT_MARKER)):
            return d
        up = os.path.dirname(d)
        if not up or up == d:
            break
        d = up
    return os.path.abspath(start)


ROOT = PANEL_DIR          # Kelvin: the engine's root IS its data folder (no root marker)
STATE_DIR = os.path.join(PANEL_DIR, 'state')
LOG_DIR = os.path.join(STATE_DIR, 'logs')
STATIC_DIR = os.path.join(PANEL_DIR, 'static')
RUNNERS_DIR = os.path.join(PACKAGE_DIR, 'runners')
CONFIG_PATH = os.path.join(PANEL_DIR, 'config.json')
BLOCKS_PATH = os.path.join(PANEL_DIR, 'blocks.json')
# the function list a fresh clone starts with: copied to blocks.json (which is not in git) on the
# panel's first start (seed_blocks)
BLOCKS_DEFAULT_PATH = os.path.join(PACKAGE_DIR, 'blocks.default.json')
# 0.1.7 (2026-10-16): the operators' Markdown notes - their data, like state\ and my_analysis\: nothing the panel
# ships writes or deletes this folder (panel_notes.py); not in git (.gitignore)
NOTES_DIR = os.path.join(PANEL_DIR, 'notes')

QUEUE_PATH = os.path.join(STATE_DIR, 'queue.json')
SAVED_PATH = os.path.join(STATE_DIR, 'saved_sequences.json')
CHAT_PATH = os.path.join(STATE_DIR, 'chat.json')
HISTORY_PATH = os.path.join(STATE_DIR, 'history.json')
AGENT_SESSION_PATH = os.path.join(STATE_DIR, 'agent_session.json')
SESSION_LOG = os.path.join(LOG_DIR, 'session.log')


def use_root(path):
    """Point every DATA folder at `path` - Kelvin's SequenceService does this for its home, and a
    test for a temporary one. The code folders (PACKAGE_DIR, RUNNERS_DIR, VERSION, the default
    function list) never move. Call it before an engine for that root starts (not thread-safe
    by itself: one service per process uses one root). Returns the absolute root."""
    global PANEL_DIR, ROOT, STATE_DIR, LOG_DIR, STATIC_DIR, CONFIG_PATH, BLOCKS_PATH, NOTES_DIR
    global QUEUE_PATH, SAVED_PATH, CHAT_PATH, HISTORY_PATH, AGENT_SESSION_PATH, SESSION_LOG
    root = os.path.abspath(os.path.expanduser(str(path)))
    PANEL_DIR = ROOT = root
    STATE_DIR = os.path.join(root, 'state')
    LOG_DIR = os.path.join(STATE_DIR, 'logs')
    STATIC_DIR = os.path.join(root, 'static')
    CONFIG_PATH = os.path.join(root, 'config.json')
    BLOCKS_PATH = os.path.join(root, 'blocks.json')
    NOTES_DIR = os.path.join(root, 'notes')
    QUEUE_PATH = os.path.join(STATE_DIR, 'queue.json')
    SAVED_PATH = os.path.join(STATE_DIR, 'saved_sequences.json')
    CHAT_PATH = os.path.join(STATE_DIR, 'chat.json')
    HISTORY_PATH = os.path.join(STATE_DIR, 'history.json')
    AGENT_SESSION_PATH = os.path.join(STATE_DIR, 'agent_session.json')
    SESSION_LOG = os.path.join(LOG_DIR, 'session.log')
    return root

# The panel's version, one line in the file VERSION beside this one - the single source the page,
# the log, the MCP bridge and History read. A missing, empty or unreadable file must never stop
# anything: it reads as "unknown".
VERSION_PATH = os.path.join(PACKAGE_DIR, 'VERSION')
VERSION_UNKNOWN = 'unknown'
_VERSION_CHARS = frozenset('0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.+_-')


def read_version(path=None):
    """The first line of VERSION ('0.1.2'), or 'unknown'. Never raises. Only a short run of letters, digits and
    . + _ - is taken (it goes into an HTTP header and a tooltip: no line break or other text can ride along); a BOM
    or a CRLF a text editor left is dropped."""
    try:
        with open(path or VERSION_PATH, 'rb') as f:
            raw = f.read(256)
        s = raw.decode('utf-8-sig').strip().splitlines()[0].strip() if raw.strip() else ''
    except Exception:                                    # noqa: BLE001 - a folder, no access, not UTF-8, ...
        return VERSION_UNKNOWN
    if not s or len(s) > 32 or not set(s) <= _VERSION_CHARS:
        return VERSION_UNKNOWN
    return s


# the version this process runs under: read once, when it started (a VERSION replaced later is the NEXT start's)
PANEL_VERSION = read_version()

# The folder the examples ship in, and the default stop script in it (paths.safe_stop_script).
SCRIPTS_DIRNAME = 'scripts'
DEFAULT_SAFE_STOP = SCRIPTS_DIRNAME + '/example_safe_stop.py'

DEFAULT_CONFIG = {
    'server': {
        'host': '127.0.0.1',         # this PC only; '0.0.0.0' opens it to the network (set a password then)
        'port': 8770,
        'password': '',              # empty = no login; set one before exposing to the LAN
        # extra names this PC is reached by (a DNS alias). 0.1.7: every request checks the name the page was opened
        # by - an IP address, localhost, this PC's own name and a *.ts.net name always pass; any other name only here
        'allowed_hosts': [],
        # a click on a path opens its folder in Explorer ON THIS PC - only for a page opened on this
        # PC. true: also for a page on another PC (the window then opens on this PC's screen, not
        # the one asking); false: there the click copies the path. Only here, never over HTTP.
        'open_folder_remote': False,
    },
    # sim = true  -> no script is run, the runner only prints fake progress.
    # Flip to false (or start with --real) to run the scripts. Default is the SAFE one.
    'sim': True,
    'sim_seconds_per_rep': 6.0,
    'paths': {
        'python_exe': '',            # '' -> the Python that runs the panel (sys.executable)
        'work_dir': '',              # '' -> <root>: the scripts' working folder
        'data_root': '',             # '' -> <root>/data
        # History -> Data folder -> "Export manual groups" writes its JSON files here: absolute,
        # or relative to <root>; '' -> <state>/datafolder/exports, made at the first export.
        # Only here in config.json: the page shows it and cannot change it.
        'history_export_dir': '',
        # run after every Stop, Skip and failed block, and by SAFE STOP and the Safe stop block:
        # it switches your instruments' outputs off. '' = no stop script (the step is skipped).
        'safe_stop_script': DEFAULT_SAFE_STOP,
        'script_search_dirs': [SCRIPTS_DIRNAME],
    },
    # "Talk to agent": each message spawns `claude -p` once and it exits. Nothing is monitored.
    'agent': {
        'enabled': False,
        'command': '',               # '' -> find `claude` on PATH
        'model': '',                 # '' -> the CLI default
        'keep_session': True,        # --resume, so the agent remembers the conversation
        'may_start': False,          # a sequence starts because a PERSON pressed Start
        'timeout_seconds': 300,
        'allowed_tools': [],         # [] -> exactly the labmaster MCP tools, nothing else
        # Off by default: the agent plans and reads the panel, it does not open the data. Turn
        # this on (Settings -> Agent) for a session where you want it to look at a file itself.
        'allow_file_read': False,
        'extra_args': [],
    },
    'ui': {
        'theme': 'slate',            # slate | fresh | warm | quiet | sky
        'mode': 'dark',              # dark | light | system
        'confirm_before_start': 'real',   # real | always | never
    },
    'run': {
        'grace_seconds': 10,         # Ctrl-Break, then kill
        'safe_stop_timeout_seconds': 120,
        'stop_queue_on_error': True,
        # a block that prints nothing and reports nothing this long gets a warning on its card;
        # with stall_stop_minutes above 0 it is also stopped then, as a failed block
        'stall_warn_minutes': 120,
        'stall_stop_minutes': 0,
    },
    # 0.1.7: History analyses data with your own scripts (panel_myscripts). `dirs`: where they live - relative to
    # <root> or absolute; [0] is made at start (with a README) and is where "New script" writes. `guard`: run them
    # under the instrument guard (runners/myscript_guard.py - a safety net against an import of an instrument
    # driver, a hardware DLL load or a network connection, not a sandbox); `guard_modules` / `guard_dlls`: your own
    # driver modules and DLL names (wildcards * allowed) added to its lists. `vscode_remote`: the authority of the
    # "Open in VS Code" link ('' = vscode://file/<path> on the PC of the page; VS Code's remote authority to open
    # it in a remote window). `max_memory_mb`: what one script process may take (a Windows job object, below-normal
    # priority) - it may run beside a measurement.
    'my_analysis': {
        'dirs': ['my_analysis'],
        'guard': True,
        'vscode_remote': '',
        'max_memory_mb': 4096,
        'guard_modules': [],
        'guard_dlls': [],
    },
}


def merge(base, patch):
    """Deep-merge `patch` into `base`, returning a new dict (dicts recurse, scalars replace)."""
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    return merge(DEFAULT_CONFIG, read_json(CONFIG_PATH, {}))


def config_problem():
    """Why config.json could not be used, or '' when it is fine (including 'not there').

    read_json() swallows a broken file and hands back {}, so a single missing comma turns
    every setting - the real/simulation flag, the script paths, the safe-stop script - into
    its default, without a word anywhere. The caller decides what to do; it must not be
    nothing.
    """
    if not os.path.exists(CONFIG_PATH):
        return ''                                   # no file at all = "use the defaults"
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except ValueError as e:
        return 'not valid JSON: %s' % e
    except OSError as e:
        return 'cannot be opened: %s' % e
    if not isinstance(data, dict):
        return 'the top level must be an object { ... }, found %s' % type(data).__name__
    return ''


def save_config(cfg):
    write_json(CONFIG_PATH, cfg)


def seed_blocks():
    """A first start: blocks.json (the function list, not in git) does not exist yet - copy
    blocks.default.json there. Returns True when it was copied; never raises."""
    try:
        if os.path.exists(BLOCKS_PATH) or not os.path.isfile(BLOCKS_DEFAULT_PATH):
            return False
        shutil.copyfile(BLOCKS_DEFAULT_PATH, BLOCKS_PATH)
        return True
    except OSError:
        return False


def scripts_base():
    """The folder a relative path in scripts/ is taken from: the root - unless the root is not the panel
    folder (LABMASTER_ROOT, a .labmaster-root marker) and has no scripts/ folder of its own while the panel
    folder has one. Then it is the panel folder, so the shipped examples, the example safe stop and the
    default function list work there until you make <root>/scripts (README, Layout)."""
    if os.path.normcase(os.path.abspath(ROOT)) == os.path.normcase(os.path.abspath(PANEL_DIR)):
        return ROOT
    if os.path.isdir(os.path.join(ROOT, SCRIPTS_DIRNAME)):
        return ROOT
    return PANEL_DIR if os.path.isdir(os.path.join(PANEL_DIR, SCRIPTS_DIRNAME)) else ROOT


def resolve(path):
    """Accept an absolute path or one relative to the root; return an absolute path. A relative path in
    scripts/ is taken from scripts_base()."""
    if not path:
        return ''
    path = str(path).replace('\\', '/')
    if os.path.isabs(path) or (len(path) > 1 and path[1] == ':'):
        return os.path.normpath(path)
    first = os.path.normpath(path).replace('\\', '/').split('/', 1)[0]
    base = scripts_base() if os.path.normcase(first) == os.path.normcase(SCRIPTS_DIRNAME) else ROOT
    return os.path.normpath(os.path.join(base, path))


def safe_stop_script(cfg):
    """paths.safe_stop_script as an absolute path; '' when none is configured (an empty value: no stop
    script). The default one, when it is not there under the root, is the panel's own example."""
    raw = (cfg.get('paths') or {}).get('safe_stop_script') if isinstance(cfg, dict) else None
    raw = raw.strip() if isinstance(raw, str) else ''
    if not raw:
        return ''
    path = resolve(raw)
    if raw.replace('\\', '/') == DEFAULT_SAFE_STOP and not os.path.isfile(path):
        own = os.path.normpath(os.path.join(PANEL_DIR, DEFAULT_SAFE_STOP))
        if os.path.isfile(own):
            return own
    return path


def setup_problems(cfg):
    """[{'key', 'path', 'text'}]: settings that name something that is not there - said at start (the
    console) and on the page while it lasts. [] when all is well. It reads the disk: never call it under
    a lock."""
    out = []
    paths = cfg.get('paths') if isinstance(cfg, dict) and isinstance(cfg.get('paths'), dict) else {}
    try:
        ss = safe_stop_script(cfg)
    except (TypeError, ValueError) as e:
        ss = ''
        out.append({'key': 'paths.safe_stop_script', 'path': '',
                    'text': 'paths.safe_stop_script in config.json cannot be used (%s): every safe stop '
                            'will fail' % e})
    if ss and not os.path.isfile(ss):
        out.append({'key': 'paths.safe_stop_script', 'path': ss,
                    'text': 'the safe-stop script %s does not exist (paths.safe_stop_script in config.json). '
                            'In real mode every Stop, Skip, failed block and SAFE STOP then ends in a FAULT. '
                            'Put the script there - or point the setting at yours (or set it to "" for no '
                            'stop script) and restart the panel.' % ss})
    dirs = paths.get('script_search_dirs')
    dirs = [d.strip() for d in dirs if isinstance(d, str) and d.strip()] if isinstance(dirs, list) else []
    try:
        none_there = bool(dirs) and not any(os.path.isdir(resolve(d)) for d in dirs)
    except (TypeError, ValueError):
        none_there = False
    if none_there:
        out.append({'key': 'paths.script_search_dirs', 'path': resolve(dirs[0]),
                    'text': 'none of the script folders exists (paths.script_search_dirs in config.json: %s, '
                            'under %s): no script can be picked or run.' % (', '.join(dirs), ROOT)})
    return out


def rel_to_root(path):
    """Inverse of resolve(): a root-relative path when possible (nicer to store in JSON)."""
    try:
        r = os.path.relpath(os.path.abspath(path), ROOT).replace('\\', '/')
        return r if not r.startswith('..') else os.path.abspath(path).replace('\\', '/')
    except ValueError:                                     # different drive
        return os.path.abspath(path).replace('\\', '/')


def frozen_runner():
    """The program a packaged Kelvin starts instead of a Python for this engine's child processes - the
    Sequence runners, the safe stop, the analysis scripts: KelvinRunner.exe beside Kelvin.exe
    (packaging/kelvin_runner.py, built by packaging/kelvin.spec). '' when this is not a packaged app, or
    that file is not there. Never raises.

    Kelvin v2-B patch: tools/vendor_sequence.py must keep this function and the frozen branch of
    python_exe() when it regenerates this file from labMaster."""
    if not getattr(sys, 'frozen', False):
        return ''
    try:
        name = 'KelvinRunner.exe' if os.name == 'nt' else 'KelvinRunner'
        path = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), name)
        return path if os.path.isfile(path) else ''
    except Exception:                                    # noqa: BLE001 - no sys.executable, no permission, ...
        return ''


def python_exe(cfg):
    """paths.python_exe, or the Python that runs the panel.

    In a packaged app sys.executable is Kelvin.exe, and starting that would start a second Kelvin, not
    run the runner script; the engine's child processes get the app's own KelvinRunner.exe instead
    (frozen_runner). A path named in the settings always wins - that is how a lab points the engine at
    a real Python with the instrument libraries, and the fallback for scripts that need more than the
    app carries (PACKAGING.md)."""
    p = (cfg.get('paths') or {}).get('python_exe') or ''
    if p:
        return resolve(p)
    return frozen_runner() or sys.executable


def work_dir(cfg):
    return resolve((cfg.get('paths') or {}).get('work_dir') or ROOT)


def data_root(cfg):
    return resolve((cfg.get('paths') or {}).get('data_root') or os.path.join(ROOT, 'data'))


# Every file read_json() found but could not use, in this process: abs path -> {'path',
# 'problem', 'copy', 'when'}. A damaged history.json used to come back as [] without a word,
# and the next save wrote that over every earlier row (blind review #047). Now a copy is kept
# beside it first, and the engine puts these on the page.
UNREADABLE = {}
_MISSING = object()


def _read(path):
    """(value, problem). value is _MISSING when there is nothing usable; problem is '' when
    the file is fine or simply not there."""
    for attempt in range(4):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f), ''
        except FileNotFoundError:
            return _MISSING, ''
        except PermissionError as e:
            # held for a moment by another process (a virus scan, a backup, the indexer)
            if attempt == 3:
                return _MISSING, 'cannot be opened (%s)' % e
            time.sleep(0.1 * (attempt + 1))
        except ValueError as e:                    # not JSON, or not UTF-8 (UnicodeDecodeError)
            return _MISSING, 'is not valid JSON (%s)' % e
        except OSError as e:
            return _MISSING, 'cannot be read (%s)' % e
    return _MISSING, ''


def _set_aside(path, problem):
    """Keep a copy of a file that could not be used, before anything overwrites it. One copy
    per version of the file (named after its modification time), however often it is read."""
    ap = os.path.abspath(path)
    copy = ''
    try:
        st = os.stat(ap)
        copy = '%s.corrupt-%s' % (ap, time.strftime('%Y%m%d-%H%M%S', time.localtime(st.st_mtime)))
        if not os.path.exists(copy):
            shutil.copy2(ap, copy)
    except OSError:
        copy = ''
    UNREADABLE[ap] = {'path': ap, 'problem': problem, 'copy': copy, 'when': time.time()}
    return UNREADABLE[ap]


def read_problem(path):
    """What read_json() found wrong with this file the last time it read it, or None."""
    return UNREADABLE.get(os.path.abspath(path))


def read_json(path, default=None, expect=None):
    """The file's JSON, or `default` when it is not there - or cannot be used. A file that is
    there but damaged (or holds the wrong type, with `expect`) is not quietly the default: a
    copy is kept as <name>.corrupt-<time> and it is recorded in UNREADABLE (blind review #047)."""
    value, problem = _read(path)
    if not problem and value is not _MISSING and expect is not None and not isinstance(value, expect):
        problem = 'holds a %s where %s was expected' % (
            type(value).__name__, ' or '.join(t.__name__ for t in (expect if isinstance(expect, tuple)
                                                                    else (expect,))))
    if problem:
        _set_aside(path, problem)
        return default
    UNREADABLE.pop(os.path.abspath(path), None)
    return default if value is _MISSING else value


# os.replace onto a file another process holds open without FILE_SHARE_DELETE (a virus scan,
# a backup or sync client, an editor) fails on Windows with WinError 5/32 for a moment; that
# used to end the overnight run (blind review #007). Waited out for up to ~2 s.
_REPLACE_WAITS = (0.05, 0.1, 0.2, 0.3, 0.5, 0.8)


def _replace(tmp, path):
    for wait in _REPLACE_WAITS + (None,):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if wait is None or os.name != 'nt':
                raise
            time.sleep(wait)


def write_json(path, obj):
    """Atomic write: a crash mid-save never leaves a truncated state file. The data is on the
    disk before the rename, so a power cut cannot leave the new name on empty blocks (#047)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, indent=1, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def lan_addresses():
    addrs = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    # the address that reaches the router: a UDP 'connect' only picks the route, nothing is sent
    # (192.0.2.1 is a documentation address, routed like any address outside this network)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('192.0.2.1', 80))
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith('127.'))


def ensure_dirs():
    for d in (STATE_DIR, LOG_DIR):
        os.makedirs(d, exist_ok=True)
