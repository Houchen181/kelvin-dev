# -*- coding: utf-8 -*-
"""
panel_dataindex.py - the read-only index of the data folder behind History's Data folder view:
every dataset folder of every day with its class, start and end, the size and last write of its
.dat files and its compared settings. The page groups it (app.js); this module only reads.

A dataset folder is <data root>/<YYYY-MM-DD>/#NNN_<Class>_<HH-MM-SS>/ (what
scripts/labmaster_dataset.py writes), holding a snapshot.json (the settings, with
loop.ts_start / loop.ts_end) and one or more GNUPlot-style .dat files. Every measurement class
is its own type: the index lists the classes it found, and a type per class.

READ-ONLY BY CONSTRUCTION. Under the data root only os.scandir, os.stat and open(path, 'r')
are called - snapshot.json is read with json.load, a .dat only until its first finite measured
value - with Python's default share mode, every file closed at once, so a measurement writing
its .dat or snapshot.json is never blocked. Nothing is created, renamed, locked or written
there. The cache is in the panel's own state folder (df_dir()), never under the data root.

Standard library and panel_paths only: this never imports panel_engine or panel_server
(panel_datafolder builds the engine's DataIndex).

    idx = DataIndex(lambda: P.data_root(cfg), engine.log)
    idx.start()             # one daemon thread: the cache published at once, then a full check
    idx.payload(version)    # ('building', {...}) | ('unchanged', {...}) | ('index', raw, gz)
    idx.request(full)       # a block ended, Rescan was pressed: scan again soon (never waits)
    idx.touch()             # a page is looking: a rescan every 30 s for the next 90 s
    idx.stop()              # the panel is shutting down (never waited for)

A warning is null or [code, detail] and skipped / problems carry codes (the page words them);
the .dat sizes and times come from os.stat (the current ones for a .dat a running measurement
holds open); nothing is printed. Each scan lists the root and every day folder and compares
every dataset folder's signature (its snapshot.json and .dat names, sizes and write times);
only a folder whose signature changed is read again, so an unchanged dataset is never opened
twice.
"""
import calendar
import collections
import datetime
import gzip
import hashlib
import json
import os
import queue
import re
import tempfile
import threading
import time

from . import paths as P

DAY_RE = re.compile(r'^\d{4}-\d\d-\d\d$')
FOLDER_RE = re.compile(r'^#(\d+)_(.+)_(\d\d)-(\d\d)-(\d\d)$')

# never compared: differ on every run by nature (dropped at index time)
BOOKKEEPING = {'loop', 'location', 'io', 'formatter', 'arrays', '__class__'}
# settings kept in the index but left out of the comparison by default (the page can switch them
# on): none by default - a lab adds the keys its scripts record but that do not make two runs
# different measurements
OPTIONAL_IGNORE = ()
SHORT_ARRAY = 16

# a type id is the measurement class itself (every class is its own type): text, at most 120
# characters, none of / \ : * ? " < > | or a control character (a folder name holds the class)
TYPE_ID_RE = re.compile(r'^[^\x00-\x1f/\\:*?"<>|]{1,120}$')

# a dataset's warning: null or [code, detail]
WARN_FOLDER_UNREADABLE = 'folder_unreadable'      # detail: the OSError, at most 160 characters
WARN_NO_SNAPSHOT = 'no_snapshot'
WARN_SNAPSHOT_UNREADABLE = 'snapshot_unreadable'  # detail: why, at most 160 characters
WARN_NO_DAT = 'no_dat'
WARN_CODES = (WARN_FOLDER_UNREADABLE, WARN_NO_SNAPSHOT, WARN_SNAPSHOT_UNREADABLE, WARN_NO_DAT)
# problems[]: a warning's code, or one of these for a .dat without a measured value
PROBLEM_ALL_NAN = 'all_nan'                       # empty 1: data rows, every measured value nan
PROBLEM_NO_ROWS = 'no_rows'                       # empty 2: only the header
PROBLEM_CODES = WARN_CODES + (PROBLEM_ALL_NAN, PROBLEM_NO_ROWS)
# skipped[]: what is not a dataset folder
SKIP_ROOT_FILE = 'root_file'                      # a file in the data root
SKIP_NOT_A_DATE = 'not_a_date'                    # a folder in the root not named YYYY-MM-DD
SKIP_DAY_FILE = 'day_file'                        # a file in a day folder
SKIP_BAD_NAME = 'bad_name'                        # not #NNN_Class_HH-MM-SS
SKIP_CODES = (SKIP_ROOT_FILE, SKIP_NOT_A_DATE, SKIP_DAY_FILE, SKIP_BAD_NAME)
# 0.1.7: the analyses' output inside a day folder (<day>/analysis/<folder>/analysis.json, read by History through
# panel_datafolder.folder_analyses) is neither a dataset nor skipped
ANALYSIS_DIR_NAME = 'analysis'

INDEX_FORMAT = 'labmaster-history-index'
INDEX_VERSION = 1
COLUMNS = ['day', 'folder', 'id', 'cls', 'start', 'end', 'lw', 'bytes', 'dat', 's', 'warn', 'ts',
           'empty']
CACHE_NAME = 'index_cache.json'
CACHE_FORMAT = 'labmaster-datafolder-index-cache'
CACHE_VERSION = 1              # raised whenever what a record holds, or how it is read, changes
SIG_ERR = 'ERR'                # the folder could not be listed
SIG_RETRY = 'RETRY'            # read again at the next scan (a snapshot caught being written)
FRESH_SNAPSHOT_S = 15.0        # a snapshot.json this young that does not parse is being written

# a dataset key: '<YYYY-MM-DD>/<folder>' - the one rule of the manual groups (key_problem)
KEY_DAY_RE = re.compile(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
KEY_FOLDER_MAX = 240
_KEY_BAD = frozenset('/\\:*?"<>|') | frozenset(chr(c) for c in range(32))


def type_problem(t):
    """'' when `t` can be a type id (a measurement class name), else why not."""
    if not isinstance(t, str) or not TYPE_ID_RE.match(t):
        return 'not a type: %r (a type is a measurement class name)' % (str(t)[:60],)
    return ''


def df_dir():
    """<state>/datafolder: the index cache and the History data-folder stores. Read at call
    time - a test or the sandbox moves P.STATE_DIR after this module is imported."""
    return os.path.join(P.STATE_DIR, 'datafolder')


def key_problem(key):
    """'' when `key` is a dataset key ('<YYYY-MM-DD>/<folder>'), else why not, in words: a day
    folder's name, one '/', then 1 to 240 characters with none of / \\ : * ? " < > | or a
    control character (a Windows folder name cannot hold them; ':' would name an NTFS stream),
    not '.' or '..', and no trailing dot or space (Windows strips them, so 'x.' would be the
    folder 'x'). Such a key can never leave the data root when joined to it."""
    if not isinstance(key, str):
        return 'a dataset key is text, not %s' % type(key).__name__
    day, sep, folder = key.partition('/')
    if not sep or not KEY_DAY_RE.match(day):
        return '%r is not <YYYY-MM-DD>/<folder>' % key[:80]
    if not folder or len(folder) > KEY_FOLDER_MAX:
        return '%r: the folder part must be 1 to %d characters' % (key[:80], KEY_FOLDER_MAX)
    if any(c in _KEY_BAD for c in folder):
        return '%r: a folder name cannot hold / \\ : * ? " < > | or a control character' % key[:80]
    if folder in ('.', '..') or folder[-1] in '. ':
        return '%r: not a folder name (".", "..", or a trailing dot or space)' % key[:80]
    return ''


# ---------------------------------------------------------------- reading one dataset folder

def wall(s):
    """'2026-09-24 01:29:31' -> seconds of that wall clock since 1970 (no time zone)."""
    try:
        return calendar.timegm(datetime.datetime.strptime(s, '%Y-%m-%d %H:%M:%S').timetuple())
    except (TypeError, ValueError):
        return None


def wall_of_mtime(t):
    return calendar.timegm(datetime.datetime.fromtimestamp(t).timetuple())


def num(v):
    """float rounded to 10 significant digits (0.009999999999999998 -> 0.01); ints stay."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return v
    if isinstance(v, int):
        return v
    if v != v or v in (float('inf'), float('-inf')):
        return str(v)
    r = float('%.10g' % v)
    return int(r) if r.is_integer() and abs(r) < 1e15 else r


def h8(obj):
    return hashlib.sha1(json.dumps(obj, separators=(',', ':')).encode()).hexdigest()[:8]


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def summarise_list(key, v):
    if v and all(is_num(x) for x in v):
        vals = [num(x) for x in v]
        srt = sorted(vals)
        mono = vals == srt or vals == srt[::-1]
        out = {'n': len(vals), 'min': srt[0], 'max': srt[-1]}
        if len(srt) > 2:
            steps = [b - a for a, b in zip(srt, srt[1:])]
            if steps[0] and all(abs(s - steps[0]) <= 1e-9 * max(1.0, abs(steps[0])) for s in steps):
                out['st'] = num(steps[0])
        if not mono:
            out['shuf'] = 1
        cmp_vals = vals
        out['h'] = h8(cmp_vals)                    # what is compared
        out['sh'] = h8(srt)                        # the sorted set, always
        if len(vals) <= SHORT_ARRAY:
            out['v'] = cmp_vals
        return out
    if not v:
        return {'n': 0, 'h': '0'}
    s = json.dumps(v, separators=(',', ':'), default=str)
    if len(s) <= 120:
        return s                                   # a short non-numeric list: its JSON text
    return {'n': len(v), 'h': h8(s), 'json': 1}


def flatten(snap):
    """compared settings: bookkeeping dropped, nested dicts dotted, lists summarised."""
    out = {}

    def put(prefix, v, depth):
        if isinstance(v, dict):
            if depth >= 3 or len(v) > 60:
                out[prefix] = {'n': len(v), 'h': h8(json.dumps(v, sort_keys=True, default=str)),
                               'json': 1}
                return
            if not v:
                out[prefix] = '{}'
            for k in sorted(v):
                put('%s.%s' % (prefix, k), v[k], depth + 1)
        elif isinstance(v, (list, tuple)):
            out[prefix] = summarise_list(prefix.split('.')[-1], list(v))
        elif isinstance(v, str) and len(v) > 240:
            out[prefix] = v[:200] + '... [#' + h8(v) + ']'
        else:
            out[prefix] = num(v)

    for k in sorted(snap):
        if k in BOOKKEEPING:
            continue
        put(k, snap[k], 0)
    return out


def default_ignored(key):
    return key.split('.')[0] in OPTIONAL_IGNORE


def dat_empty(path):
    """0 if the .dat (qcodes GNUPlot format) has a finite measured value, 1 if it has data rows
    but every measured value is nan, 2 if it has no data rows. The third header line gives the
    shape of the set (setpoint) columns, which come first; only the columns after them count.
    Stops at the first finite measured value. Unreadable -> 0 (never flag on a guess)."""
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            heads, nset, rows = 0, None, 0
            for line in f:
                if line.startswith('#'):
                    heads += 1
                    if heads == 3:
                        nset = len(line[1:].split())
                    continue
                cols = line.split()
                if not cols:
                    continue
                rows += 1
                for x in cols[nset or 0:]:
                    try:
                        v = float(x)
                    except ValueError:
                        return 0
                    if v == v and v not in (float('inf'), float('-inf')):
                        return 0
            return 2 if rows == 0 else 1
    except OSError:
        return 0


def _sig_of(lines):
    return hashlib.sha1('\n'.join(sorted(lines)).encode('utf-8', 'surrogatepass')).hexdigest()[:16]


def _sig_name(name):
    low = name.lower()
    return low if (low == 'snapshot.json' or low.endswith('.dat')) else None


def folder_sig(path, fresh=False):
    """A dataset folder's signature: 'ERR' when it cannot be listed, else sha1 of the sorted
    lines 'name.lower() TAB size TAB mtime_ns' of its regular files named snapshot.json or
    ending in .dat, [:16]. `fresh`: each file's own os.stat() rather than the folder listing's
    copy (FindFirstFile's size and time can lag behind a file another process holds open, as
    a running measurement holds its .dat)."""
    lines = []
    try:
        with os.scandir(path) as it:
            for e in it:
                low = _sig_name(e.name)
                if low is None:
                    continue
                try:
                    if not e.is_file():
                        continue
                    st = os.stat(e.path) if fresh else e.stat()
                except OSError:
                    continue                            # gone between the listing and the stat
                lines.append('%s\t%d\t%d' % (low, st.st_size, st.st_mtime_ns))
    except OSError:
        return SIG_ERR
    return _sig_of(lines)


def read_folder(day, name, path, kind):
    """One dataset folder -> a record (never raises): day, folder, kind, id, cls, start, end,
    lw, bytes, dat, snap (the flattened settings or None), warn (None or [code, detail]), ts,
    empty, sig (folder_sig, fresh, taken before anything was read) - and '_fresh_bad' when its
    snapshot.json did not parse and was written less than 15 s ago (being written now)."""
    rec = {'day': day, 'folder': name, 'kind': kind, 'warn': None}
    m = FOLDER_RE.match(name)
    rec['id'] = int(m.group(1))
    rec['cls'] = m.group(2)
    hh, mm, ss = m.group(3), m.group(4), m.group(5)
    folder_start = wall('%s %s:%s:%s' % (day, hh, mm, ss))
    dats, nbytes, last, dat_paths = [], 0, None, []
    snap_path, snap_mtime = None, None
    lines = []
    try:
        with os.scandir(path) as it:
            for e in it:
                if not e.is_file():
                    continue
                low = _sig_name(e.name)
                if low is None:
                    continue
                try:
                    st = os.stat(e.path)
                except OSError:
                    continue                            # gone between the listing and the stat
                lines.append('%s\t%d\t%d' % (low, st.st_size, st.st_mtime_ns))
                if low.endswith('.dat'):
                    dats.append(e.name)
                    dat_paths.append(e.path)
                    nbytes += st.st_size
                    last = st.st_mtime if last is None else max(last, st.st_mtime)
                elif e.name == 'snapshot.json':
                    snap_path, snap_mtime = e.path, st.st_mtime
    except OSError as e:
        rec['warn'] = [WARN_FOLDER_UNREADABLE, str(e)[:160]]
        lines = None
    rec['sig'] = SIG_ERR if lines is None else _sig_of(lines)
    rec['dat'] = sorted(dats)
    rec['bytes'] = nbytes
    rec['lw'] = wall_of_mtime(last) if last is not None else None
    rec['snap'] = None
    rec['start'] = folder_start
    rec['end'] = None
    rec['ts'] = 0
    if snap_path is None:
        rec['warn'] = rec['warn'] or [WARN_NO_SNAPSHOT, None]
    else:
        try:
            with open(snap_path, encoding='utf-8') as f:
                snap = json.load(f)
            if not isinstance(snap, dict):
                raise ValueError('top level is %s, not a dict' % type(snap).__name__)
            loop = snap.get('loop') if isinstance(snap.get('loop'), dict) else {}
            ts0, ts1 = wall(loop.get('ts_start')), wall(loop.get('ts_end'))
            if ts0 is not None:
                rec['start'], rec['ts'] = ts0, 1
            rec['end'] = ts1
            rec['snap'] = flatten(snap)
        except Exception as e:                                         # noqa: BLE001
            rec['warn'] = [WARN_SNAPSHOT_UNREADABLE, str(e)[:160] or type(e).__name__]
            if snap_mtime is not None and time.time() - snap_mtime < FRESH_SNAPSHOT_S:
                rec['_fresh_bad'] = True
    if not dats and not rec['warn']:
        rec['warn'] = [WARN_NO_DAT, None]
    # empty only if every .dat is empty; 1 (all nan) wins over 2 (no rows) when they are mixed
    rec['empty'] = 0
    if dat_paths:
        e = [dat_empty(p) for p in dat_paths]
        rec['empty'] = 0 if 0 in e else (1 if 1 in e else 2)
    return rec


# ---------------------------------------------------------------- the tables

def _skipped(days, root_skipped, day_skipped):
    """The root's names sorted, each day's entries where its name falls."""
    names = sorted([(d, None) for d in days] + [(s['name'], s) for s in root_skipped],
                   key=lambda x: x[0])
    out = []
    for name, s in names:
        if s is None:
            for e in day_skipped.get(name) or ():
                out.append({'day': name, 'name': e['name'], 'code': e['code']})
        else:
            out.append({'day': None, 'name': s['name'], 'code': s['code']})
    return out


def _fp(snap):
    cmp = {k: v for k, v in snap.items() if not default_ignored(k)}
    return hashlib.sha1(json.dumps(cmp, sort_keys=True, default=str).encode()).hexdigest()[:12]


def build_index(records, days, skipped, root, stats, fps=None):
    """The published index (INDEX_FORMAT) from records sorted by (day, folder): the classes (each
    its own type); settings deduplicated by their sorted JSON, with the fp of the default rule;
    dat_sets; rows; types with counts; problems. `fps`: a dict skey -> fp kept between builds."""
    fps = {} if fps is None else fps
    classes, class_idx = [], {}
    settings, set_idx = [], {}
    datsets, dat_idx = [], {}
    day_idx = {d: i for i, d in enumerate(days)}
    rows, problems = [], []
    by_type = collections.Counter()
    n_nan = n_norows = 0
    for r in records:
        c = r['cls']
        ci = class_idx.get(c)
        if ci is None:
            ci = class_idx[c] = len(classes)
            classes.append({'name': c, 'type': c})
        by_type[c] += 1
        si = -1
        skey = r.get('skey')
        if skey is not None:
            si = set_idx.get(skey)
            if si is None:
                si = set_idx[skey] = len(settings)
                fp = fps.get(skey)
                if fp is None:
                    fp = fps[skey] = _fp(r['snap'])
                settings.append({'fp': fp, 'k': r['snap']})
        dk = '\n'.join(r['dat'])
        di = dat_idx.get(dk)
        if di is None:
            di = dat_idx[dk] = len(datsets)
            datsets.append(list(r['dat']))
        warn = list(r['warn']) if r['warn'] else None
        rows.append([day_idx[r['day']], r['folder'], r['id'], ci, r['start'], r['end'], r['lw'],
                     r['bytes'], di, si, warn, 1 if r['ts'] else 0, r['empty']])
        if warn:
            problems.append({'day': r['day'], 'name': r['folder'], 'code': warn[0],
                             'detail': warn[1]})
        elif r['empty']:
            problems.append({'day': r['day'], 'name': r['folder'],
                             'code': PROBLEM_ALL_NAN if r['empty'] == 1 else PROBLEM_NO_ROWS,
                             'detail': None})
        if r['empty'] == 1:
            n_nan += 1
        elif r['empty'] == 2:
            n_norows += 1
    # every measurement class is its own type, in the order the classes were met
    types = [{'id': cl['name'], 'name': cl['name'], 'classes': [cl['name']],
              'n': by_type.get(cl['name'], 0)} for cl in classes]
    now = time.time()
    st = {'days': len(days), 'datasets': len(rows), 'settings_distinct': len(settings),
          'skipped': len(skipped), 'problems': len(problems), 'dat_all_nan': n_nan,
          'dat_no_rows': n_norows}
    st.update(stats or {})
    return {
        'format': INDEX_FORMAT, 'version': INDEX_VERSION,
        'generated': datetime.datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S'),
        'generated_ts': now,
        'root': root,
        'columns': list(COLUMNS),
        'days': list(days), 'classes': classes, 'types': types, 'settings': settings,
        'dat_sets': datsets, 'datasets': rows,
        'skipped': skipped, 'problems': problems,
        'rule': {'bookkeeping': sorted(BOOKKEEPING), 'optional_ignore': list(OPTIONAL_IGNORE)},
        'stats': st,
    }


def content_hash(index):
    """sha1 of the index without generated, generated_ts and stats: the same datasets, settings
    and skipped entries give the same hash, so the same version."""
    body = {k: v for k, v in index.items() if k not in ('generated', 'generated_ts', 'stats')}
    return hashlib.sha1(json.dumps(body, ensure_ascii=False, separators=(',', ':'), default=str)
                        .encode('utf-8')).hexdigest()


def wire(version, index):
    """(raw, gz): the served bytes of one version, made once."""
    raw = json.dumps({'version': version, 'index': index}, ensure_ascii=False,
                     separators=(',', ':'), default=str).encode('utf-8')
    return raw, gzip.compress(raw, 6)


# ---------------------------------------------------------------- the cache

_REC_KEYS = ('day', 'folder', 'kind', 'id', 'cls', 'start', 'end', 'lw', 'bytes', 'dat', 's',
             'warn', 'ts', 'empty', 'sig')


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _num_or_none(v):
    return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool))


def _record_problem(r, n_settings):
    if not isinstance(r, dict) or any(k not in r for k in _REC_KEYS):
        return 'a record without all its fields'
    if not isinstance(r['day'], str) or not DAY_RE.match(r['day']):
        return 'a record with a bad day'
    if r['kind'] != 'msmt' or not isinstance(r['folder'], str) or not FOLDER_RE.match(r['folder']):
        return 'a record with a bad folder name'
    if not _is_int(r['id']) or not isinstance(r['cls'], str) or not _is_int(r['bytes']):
        return 'a record with a bad id, class or size'
    if not all(_num_or_none(r[k]) for k in ('start', 'end', 'lw')):
        return 'a record with a bad time'
    if not isinstance(r['dat'], list) or not all(isinstance(x, str) for x in r['dat']):
        return 'a record with a bad .dat list'
    if not _is_int(r['s']) or not -1 <= r['s'] < n_settings:
        return 'a record with a bad settings index'
    w = r['warn']
    if w is not None and not (isinstance(w, list) and len(w) == 2 and w[0] in WARN_CODES
                              and (w[1] is None or isinstance(w[1], str))):
        return 'a record with a bad warning'
    if r['ts'] not in (0, 1) or r['empty'] not in (0, 1, 2) or not isinstance(r['sig'], str):
        return 'a record with a bad flag'
    return ''


def cache_problem(doc, rootn):
    """'' when `doc` is a cache of this format made for this data root, else why not."""
    if not isinstance(doc, dict) or doc.get('format') != CACHE_FORMAT:
        return 'it is not an index cache'
    if doc.get('version') != CACHE_VERSION or doc.get('index_format') != INDEX_VERSION:
        return 'it is of another version (%r / index %r)' % (doc.get('version'),
                                                               doc.get('index_format'))
    if doc.get('root') != rootn:
        return 'it was made for another data folder (%s)' % str(doc.get('root'))[:120]
    for k, t in (('days', list), ('root_skipped', list), ('day_skipped', dict),
                 ('settings', list), ('records', list)):
        if not isinstance(doc.get(k), t):
            return 'its %s is missing' % k
    if not all(isinstance(d, str) and DAY_RE.match(d) for d in doc['days']):
        return 'a day is not YYYY-MM-DD'
    if not all(isinstance(s, dict) for s in doc['settings']):
        return 'a settings entry is not an object'
    for s in doc['root_skipped'] + [e for v in doc['day_skipped'].values()
                                    for e in (v if isinstance(v, list) else [None])]:
        if not (isinstance(s, dict) and isinstance(s.get('name'), str)
                and s.get('code') in SKIP_CODES):
            return 'a skipped entry is not {name, code}'
    n = len(doc['settings'])
    for r in doc['records']:
        why = _record_problem(r, n)
        if why:
            return why
    return ''


# ---------------------------------------------------------------- the index, kept up to date

class DataIndex(object):
    """The data folder's index, built and kept current by one background thread, and served as
    ready-made bytes. Readers never wait: payload(), current() and the attributes read the last
    published tuple, which is replaced whole. One scan at a time (its own lock); the engine's
    lock is never taken and nothing the engine holds is ever waited for; log_fn is called only
    with no lock of this object held."""

    WAKE_S = 5.0            # the thread looks at its flags this often
    PERIOD_S = 30.0         # a rescan this long after the last one ended ...
    TOUCH_S = 90.0          # ... while a page asked for the index within this time
    CACHE_EVERY_S = 60.0    # the cache is written at most this often
    INLINE_READS = 2        # this few folders to read are read on the scanning thread itself

    def __init__(self, data_root_fn, log_fn=None, workers=4):
        self._root_fn = data_root_fn
        self._log_fn = log_fn
        try:
            self._workers = max(1, int(workers))
        except (TypeError, ValueError):
            self._workers = 4
        self._scan_lock = threading.Lock()
        self._flag_lock = threading.Lock()  # the request flags only; nothing is called under it
        self._start_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._req = False
        self._req_full = False
        self._touched = None                # time.monotonic() of the last touch()
        self._last_end = None               # time.monotonic() when the last scan ended
        self._born = '%x-%s' % (int(time.time() * 1000), os.urandom(3).hex())
        self._n = 0
        self._hash = None
        self._pub = None                    # (version, index, raw, gz)
        self.scanning = False
        self.error = None
        self.scanned_at = None
        self.progress = (0, 0)
        self._recs = {}                     # (day, folder) -> record
        self._rootn = None
        self._shape = None                  # (days, root_skipped, day_skipped, missing, root)
        self._pool = {}                     # sorted settings JSON -> the one dict of those settings
        self._fps = {}                      # sorted settings JSON -> fp
        self._logs = collections.deque()
        self._said = set()
        self._announced = False
        self._cache_dirty = False
        self._cache_saved = None            # time.monotonic() of the last cache write
        self._cache_loaded = False
        self.after_scan = None              # called with no lock held after each scan of the thread (0.1.7)

    # ------------------------------------------------------------ what readers see

    @property
    def version(self):
        pub = self._pub
        return pub[0] if pub else None

    def current(self):
        """The published index (a dict; read it, never change it), or None before the first."""
        pub = self._pub
        return pub[1] if pub else None

    def payload(self, version=None):
        """('building', {...}) before the first publish; ('unchanged', {...}) when `version` is
        the one published; else ('index', raw bytes, gzip bytes). Never waits for a scan."""
        pub = self._pub
        if pub is None:
            done, total = self.progress
            return ('building', {'version': None, 'building': True,
                                 'progress': {'done': done, 'total': total}, 'error': self.error})
        if version is not None and str(version) == pub[0]:
            return ('unchanged', {'version': pub[0], 'unchanged': True,
                                  'scanning': bool(self.scanning), 'scanned': self.scanned_at,
                                  'error': self.error})
        return ('index', pub[2], pub[3])

    # ------------------------------------------------------------ what writers ask

    def request(self, full=False):
        """Scan again soon (a block ended, Rescan was pressed). Never waits; a full request
        stays asked for until the next scan."""
        with self._flag_lock:
            self._req = True
            if full:
                self._req_full = True
        self._wake.set()

    def pending(self):
        """A scan is running or asked for."""
        return bool(self.scanning or self._req)

    def touch(self):
        """A page is looking at the index: rescans every PERIOD_S for the next TOUCH_S."""
        self._touched = time.monotonic()

    def stop(self):
        """The thread ends at its next look (reads in flight are dropped); never waited for."""
        self._stop.set()
        self._wake.set()

    def start(self):
        """Start the background thread (once; a second call returns the same thread)."""
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop.clear()
            t = threading.Thread(target=self._run, name='datafolder-index', daemon=True)
            self._thread = t
            t.start()
            return t

    # ------------------------------------------------------------ the thread

    def _run(self):
        try:
            self.load_cache()
        except Exception as e:                                         # noqa: BLE001
            self._note('History data folder: the index cache could not be read (%s: %s)'
                       % (type(e).__name__, e))
            self._flush_logs()
        self._background_scan(True)
        while not self._stop.is_set():
            self._wake.wait(self.WAKE_S)
            if self._stop.is_set():
                break
            self._wake.clear()
            with self._flag_lock:
                req, full = self._req, self._req_full
                self._req = self._req_full = False
            now = time.monotonic()
            try:
                if req:
                    self._background_scan(full)
                elif (self._touched is not None and now - self._touched <= self.TOUCH_S
                      and (self._last_end is None or now - self._last_end >= self.PERIOD_S)):
                    self._background_scan(False)
                elif self._cache_dirty and self._cache_due():
                    with self._scan_lock:
                        self._save_cache()
                    self._flush_logs()
            except Exception as e:                                     # noqa: BLE001
                self._note('History data folder: %s: %s' % (type(e).__name__, e))
                self._flush_logs()

    def _background_scan(self, full):
        """One scan of the thread; the first one that completes says how it went, once."""
        res = self.scan(full)
        fn = self.after_scan
        if fn is not None:
            try:
                fn()                        # the DataFolder's analysis folders, looked at again (never fails a scan)
            except Exception:                                          # noqa: BLE001
                pass
        if res.get('kind') in ('full', 'incremental') and not self._announced:
            self._announced = True
            if self._cache_loaded:
                how = 'from the cache, checked: %d new, %d changed, %d gone' % (
                    res['added'], res['reread'], res['removed'])
            else:
                how = '%s scan' % res['kind']
            self._log('History data folder: %d datasets indexed in %.1f s (%s)'
                      % (res['datasets'], res['seconds'], how), 'info')
        return res

    # ------------------------------------------------------------ logging

    def _log(self, text, lvl='info'):
        fn = self._log_fn
        if fn is None:
            return
        try:
            fn(text, lvl)
        except Exception:                                              # noqa: BLE001
            pass

    def _note(self, text, lvl='warn'):
        """Said once per distinct text, when no lock of this object is held (_flush_logs)."""
        if text in self._said or len(self._said) > 400:
            return
        self._said.add(text)
        self._logs.append((text, lvl))

    def _flush_logs(self):
        while True:
            try:
                text, lvl = self._logs.popleft()
            except IndexError:
                return
            self._log(text, lvl)

    # ------------------------------------------------------------ the cache

    def _root_now(self):
        root = self._root_fn()
        root = str(root) if root else ''
        return root, (os.path.normcase(os.path.abspath(root)) if root else '')

    def _cache_path(self):
        return os.path.join(df_dir(), CACHE_NAME)

    def _cache_due(self):
        return self._cache_saved is None or \
            time.monotonic() - self._cache_saved >= self.CACHE_EVERY_S

    def load_cache(self):
        """Publish the index from the cache (stats.scan 'cache'), before any folder is read.
        False when there is none, or it cannot be used (said once; it is built again)."""
        t0 = time.perf_counter()
        try:
            with self._scan_lock:
                return self._load_cache(t0)
        finally:
            self._flush_logs()

    def _load_cache(self, t0):
        path = self._cache_path()
        if not os.path.isfile(path):
            return False
        root, rootn = self._root_now()
        try:
            with open(path, 'r', encoding='utf-8') as f:
                doc = json.load(f)
            why = cache_problem(doc, rootn)
        except (OSError, ValueError) as e:
            why = 'it cannot be read (%s)' % (str(e)[:160] or type(e).__name__)
        if why:
            self._note('History data folder: the index cache was not used - %s; the index is '
                       'built again from the data folder' % why)
            return False
        settings = []
        for s in doc['settings']:
            skey = json.dumps(s, sort_keys=True, separators=(',', ':'), default=str)
            settings.append(self._pool.setdefault(skey, (skey, s)))
        recs = {}
        for r in doc['records']:
            rec = {k: r[k] for k in _REC_KEYS if k != 's'}
            rec['dat'] = list(r['dat'])
            rec['warn'] = list(r['warn']) if r['warn'] else None
            if r['s'] >= 0:
                rec['skey'], rec['snap'] = settings[r['s']]
            else:
                rec['skey'], rec['snap'] = None, None
            recs[(rec['day'], rec['folder'])] = rec
        self._recs = recs
        self._rootn = rootn
        days = sorted(doc['days'])
        root_sk = [{'name': s['name'], 'code': s['code']} for s in doc['root_skipped']]
        day_sk = {d: [{'name': e['name'], 'code': e['code']} for e in v]
                  for d, v in doc['day_skipped'].items() if v}
        missing = bool(doc.get('root_missing'))
        self._shape = (days, root_sk, day_sk, missing, root)
        self._cache_loaded = True
        self._cache_dirty = False
        self._publish('cache', t0, 0, 0, 0)
        self.scanned_at = None
        return True

    def _save_cache(self):
        """Under the scan lock. The records (not the published index) as compact JSON, written
        atomically; never under the data root. Never raises (a failure is said once)."""
        if self._shape is None:
            return False
        days, root_sk, day_sk, missing, root = self._shape
        d = df_dir()
        try:
            if root and _inside(os.path.abspath(d), os.path.abspath(root)):
                self._note('History data folder: the state folder is inside the data folder; '
                           'the index cache is not written there')
                self._cache_dirty = False
                return False
            settings, s_idx, out = [], {}, []
            for k in sorted(self._recs):
                r = self._recs[k]
                si = -1
                if r.get('skey') is not None:
                    si = s_idx.get(r['skey'])
                    if si is None:
                        si = s_idx[r['skey']] = len(settings)
                        settings.append(r['snap'])
                row = {k2: r[k2] for k2 in _REC_KEYS if k2 != 's'}
                row['s'] = si
                out.append(row)
            doc = {'format': CACHE_FORMAT, 'version': CACHE_VERSION, 'index_format': INDEX_VERSION,
                   'root': self._rootn,
                   'saved': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                   'days': days, 'root_skipped': root_sk, 'day_skipped': day_sk,
                   'root_missing': bool(missing),
                   'settings': settings, 'records': out}
            text = json.dumps(doc, ensure_ascii=False, separators=(',', ':'), default=str)
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, suffix='.tmp')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(text)
                    f.flush()
                    os.fsync(f.fileno())
                P._replace(tmp, self._cache_path())
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except (OSError, TypeError, ValueError) as e:
            self._note('History data folder: the index cache could not be written (%s: %s); '
                       'the index itself is not affected' % (type(e).__name__, e))
            self._cache_saved = time.monotonic()      # tried: not again before CACHE_EVERY_S
            return False
        self._cache_dirty = False
        self._cache_saved = time.monotonic()
        return True

    # ------------------------------------------------------------ one scan

    def scan(self, full=False):
        """List the data root and every day folder, compare every dataset folder's signature,
        read the new and changed ones again (four daemon threads), rebuild and publish the
        tables when anything changed. Synchronous; one at a time; never raises: an error is
        said once, kept in .error, and the last good index stays published.
        -> {'kind', 'seconds', 'datasets', 'reread', 'added', 'removed', 'changed', 'version'}
        (kind 'full' / 'incremental', 'stopped' after stop(), 'error' with an 'error' key)."""
        t0 = time.perf_counter()
        with self._scan_lock:
            self.scanning = True
            try:
                res = self._scan(bool(full), t0)
                if res.get('kind') != 'stopped':
                    self.error = None
            except Exception as e:                                     # noqa: BLE001
                msg = '%s: %s' % (type(e).__name__, e)
                self.error = msg
                self._note('History data folder: the index could not be brought up to date (%s); '
                           'the last one is still shown' % msg)
                res = {'kind': 'error', 'error': msg, 'seconds': round(time.perf_counter() - t0, 3),
                       'datasets': len(self._recs), 'reread': 0, 'added': 0, 'removed': 0,
                       'changed': False, 'version': self.version}
            finally:
                self.scanning = False
                self._last_end = time.monotonic()
        self._flush_logs()
        return res

    def _stopped(self, t0):
        return {'kind': 'stopped', 'seconds': round(time.perf_counter() - t0, 3),
                'datasets': len(self._recs), 'reread': 0, 'added': 0, 'removed': 0,
                'changed': False, 'version': self.version}

    def _scan(self, full, t0):
        root, rootn = self._root_now()
        if rootn != self._rootn:
            self._recs = {}                   # another data root: nothing of the old one is kept
            self._rootn = rootn
        # the root: day folders, and what is not one
        days, root_sk, missing = [], [], False
        if not root or not os.path.isdir(root):
            missing = True
        else:
            with os.scandir(root) as it:
                ents = sorted(it, key=lambda e: e.name)
            for e in ents:
                isdir = _isdir(e)
                if isdir and DAY_RE.match(e.name):
                    days.append(e.name)
                else:
                    root_sk.append({'name': e.name,
                                    'code': SKIP_NOT_A_DATE if isdir else SKIP_ROOT_FILE})
        # every day folder: its dataset folders, and what is not one
        found, day_sk, kept = {}, {}, set()
        old_day_sk = self._shape[2] if self._shape else {}
        for day in days:
            if self._stop.is_set():
                return self._stopped(t0)
            dp = os.path.join(root, day)
            try:
                with os.scandir(dp) as it:
                    ents = sorted(it, key=lambda e: e.name)
            except OSError as e:
                # what was known of it is kept as it was, rather than every dataset of it gone
                self._note('History data folder: the day folder %s cannot be listed (%s); what '
                           'was known of it is kept' % (day, e))
                for k, r in self._recs.items():
                    if r['day'] == day:
                        found[k] = (day, r['folder'], os.path.join(dp, r['folder']), r['kind'])
                        kept.add(k)
                if old_day_sk.get(day):
                    day_sk[day] = list(old_day_sk[day])
                continue
            sk = []
            for e in ents:
                if not _isdir(e):
                    sk.append({'name': e.name, 'code': SKIP_DAY_FILE})
                elif e.name == ANALYSIS_DIR_NAME:
                    continue
                elif FOLDER_RE.match(e.name):
                    found[(day, e.name)] = (day, e.name, e.path, 'msmt')
                else:
                    sk.append({'name': e.name, 'code': SKIP_BAD_NAME})
            if sk:
                day_sk[day] = sk
        # what is gone, what is new, and which signatures moved
        removed = [k for k in self._recs if k not in found]
        jobs = []
        for k, (day, name, path, kind) in found.items():
            old = self._recs.get(k)
            if k in kept:
                continue                      # its day folder could not be listed this time
            if old is None:
                jobs.append((k, day, name, path, kind, None))
                continue
            s = folder_sig(path, fresh=old.get('end') is None)
            if s != old.get('sig') or old.get('kind') != kind:
                jobs.append((k, day, name, path, kind, s))
        if self._stop.is_set():
            return self._stopped(t0)
        got = self._read_all(jobs)
        if got is None:
            return self._stopped(t0)
        added = reread = 0
        for k, _day, _name, _path, _kind, s in jobs:
            rec = got.get(k)
            if rec is None:
                continue
            old = self._recs.get(k)
            if rec.pop('_fresh_bad', False):
                # a snapshot.json being written right now: the record it had stays, and it is
                # read again at the next scan
                if old is not None:
                    old['sig'] = SIG_RETRY
                    continue
                rec['sig'] = SIG_RETRY
            elif s is not None:
                rec['sig'] = s                # the signature this scan compared, taken before the read
            self._pool_snap(rec)
            self._recs[k] = rec
            if old is None:
                added += 1
            else:
                reread += 1
        for k in removed:
            self._recs.pop(k, None)
        shape = (days, root_sk, day_sk, missing, root)
        shape_changed = shape != self._shape
        self._shape = shape
        records_changed = bool(added or reread or removed or shape_changed)
        if records_changed:
            self._cache_dirty = True
        kind = 'full' if full else 'incremental'
        changed = False
        if records_changed or full or self._pub is None:
            changed = self._publish(kind, t0, reread, added, len(removed))
        self.scanned_at = time.time()
        if self._cache_dirty and (full or self._cache_due()):
            self._save_cache()
        return {'kind': kind, 'seconds': round(time.perf_counter() - t0, 3),
                'datasets': len(self._recs), 'reread': reread, 'added': added,
                'removed': len(removed), 'changed': changed, 'version': self.version}

    def _pool_snap(self, rec):
        snap = rec.get('snap')
        if snap is None:
            rec['skey'] = None
            return
        skey = json.dumps(snap, sort_keys=True, separators=(',', ':'), default=str)
        hit = self._pool.get(skey)
        if hit is None:
            hit = self._pool[skey] = (skey, snap)
        rec['skey'], rec['snap'] = hit

    def _read_all(self, jobs):
        """{key: record} of read_folder over the jobs; four daemon threads pull from a queue
        and stop at the next folder once stop() was called (then None). A panel that is shut
        down during the first build never waits for the folders still queued."""
        out = {}
        total = len(jobs)
        self.progress = (0, total)
        if not jobs:
            return out
        if total <= self.INLINE_READS or self._workers <= 1:
            for j in jobs:
                if self._stop.is_set():
                    return None
                out[j[0]] = read_folder(j[1], j[2], j[3], j[4])
                self.progress = (len(out), total)
            return out
        q = queue.Queue()
        for j in jobs:
            q.put(j)
        done = threading.Lock()

        def work():
            while not self._stop.is_set():
                try:
                    j = q.get_nowait()
                except queue.Empty:
                    return
                try:
                    r = read_folder(j[1], j[2], j[3], j[4])
                except Exception:                                      # noqa: BLE001
                    r = None                                           # never: it does not raise
                with done:
                    if r is not None:
                        out[j[0]] = r
                    self.progress = (min(total, self.progress[0] + 1), total)

        threads = [threading.Thread(target=work, name='datafolder-read', daemon=True)
                   for _ in range(min(self._workers, total))]
        for t in threads:
            t.start()
        for t in threads:
            while t.is_alive():
                t.join(0.25)
                if self._stop.is_set():
                    return None
        return out

    def _publish(self, kind, t0, reread, added, removed):
        """Rebuild the tables from the records; publish a new version only when the content
        changed. True when a new version was published."""
        days, root_sk, day_sk, missing, root = self._shape
        records = [self._recs[k] for k in sorted(self._recs)]
        used = {r['skey'] for r in records if r.get('skey') is not None}
        if len(self._pool) > len(used) + 64:
            self._pool = {k: v for k, v in self._pool.items() if k in used}
            self._fps = {k: v for k, v in self._fps.items() if k in used}
        stats = {'root_missing': bool(missing), 'scan': kind,
                 'reread': reread, 'added': added, 'removed': removed}
        index = build_index(records, days, _skipped(days, root_sk, day_sk), root,
                            stats, self._fps)
        index['stats']['seconds'] = round(time.perf_counter() - t0, 1)
        h = content_hash(index)
        if self._pub is not None and h == self._hash:
            return False
        self._n += 1
        version = '%s.%d' % (self._born, self._n)
        raw, gz = wire(version, index)
        self._hash = h
        self._pub = (version, index, raw, gz)            # one assignment: readers see all or none
        return True


def _isdir(entry):
    try:
        return entry.is_dir()
    except OSError:
        return False


def _inside(path, root):
    try:
        path, root = os.path.normcase(path), os.path.normcase(root)
        return os.path.commonpath([path, root]) == root
    except ValueError:                                     # another drive
        return False
