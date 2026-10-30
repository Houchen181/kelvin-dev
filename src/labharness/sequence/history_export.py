# -*- coding: utf-8 -*-
"""
panel_history_export.py - History -> Data folder -> "Export manual groups", written on the panel PC.

The page used to hand the export to the browser as a download: it landed in the Downloads folder of
whichever PC the page was open on, with one name per day, so the second export of a day became
"(1)", "(2)" ... Now the page sends the same JSON here, and the panel writes it into one folder on
the panel's PC:

    paths.history_export_dir   in config.json only (absolute, or relative to the repo root)
    '' (the default)           <state>\\datafolder\\exports\\ - made at the first export

as history_manual_groups_YYYY-MM-DD_HH-MM-SS.json, in the panel PC's local time; a second export
in the same second is ..._2.json, ..._3.json. Nothing is ever overwritten or deleted. The page's
message gives the whole path (a click copies it) and offers "Download a copy" - a plain browser
download of the same JSON under the same name.

    GET  /api/datafolder/export           -> {dir, setting, configured, default, exists, error, pattern}
    POST /api/datafolder/export {export}  -> 200 {ok, name, path, dir, bytes, groups, datasets}
                                             400 {error}: not an export as the page makes it
                                             503 {error, dir}: the folder cannot be made or written

The export itself is the page's (app.js exportMan): {kind: 'labmaster-history-manual-groups',
version: 1, exported, data_root, index_generated, rule, note, groups}.
It is checked (kind, version, every member a dataset key, panel_datafolder's limits) and written
with json.dumps(indent=2), which is the text the page's JSON.stringify(obj, null, 2) makes.
Standard library + panel_paths / panel_dataindex / panel_datafolder; no lock of the engine's.
"""
import json
import os
import tempfile
import time

from . import paths as P
from . import dataindex as DI
from . import datafolder as DF

KIND = 'labmaster-history-manual-groups'
KINDS = (KIND,)
NAME_PREFIX = 'history_manual_groups_'
SETTING = 'paths.history_export_dir'
PATTERN = NAME_PREFIX + 'YYYY-MM-DD_HH-MM-SS.json'
MAX_SAME_SECOND = 999


def default_dir():
    """<state>\\datafolder\\exports - read at call time (a test or the sandbox moves P.STATE_DIR)."""
    return os.path.normpath(os.path.join(DI.df_dir(), 'exports'))


def configured(cfg):
    """paths.history_export_dir as config.json has it (None when it is not there)."""
    paths = cfg.get('paths') if isinstance(cfg, dict) else None
    return paths.get('history_export_dir') if isinstance(paths, dict) else None


def export_dir(cfg):
    """(the folder the exports go to, '') or ('', why not)."""
    raw = configured(cfg)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default_dir(), ''
    if not isinstance(raw, str):
        return '', '%s in config.json must be a folder path (text), found %r' % (SETTING, raw)
    return P.resolve(raw.strip()), ''


def info(cfg):
    """What Settings shows: the folder, and where it is set."""
    d, why = export_dir(cfg)
    raw = configured(cfg)
    return {'dir': d, 'setting': SETTING, 'configured': raw if isinstance(raw, str) else '',
            'default': not (isinstance(raw, str) and raw.strip()), 'exists': bool(d) and os.path.isdir(d),
            'error': why or None, 'pattern': PATTERN}


def export_problem(obj):
    """'' when `obj` is a manual-groups export as the page makes it, else why not (in words)."""
    if not isinstance(obj, dict):
        return 'export must be the exported manual groups {kind, version, groups, ...}'
    if obj.get('kind') not in KINDS:
        return 'export.kind must be %r (got %r)' % (KIND, str(obj.get('kind'))[:60])
    v = obj.get('version')
    if isinstance(v, bool) or v != 1:
        return 'export.version must be 1 (got %r)' % (v,)
    groups = obj.get('groups')
    if not isinstance(groups, list):
        return 'export.groups must be a list of groups'
    if len(groups) > DF.MAX_GROUPS:
        return 'at most %d manual groups (got %d)' % (DF.MAX_GROUPS, len(groups))
    n = 0
    for i, g in enumerate(groups):
        if not isinstance(g, dict):
            return 'export.groups[%d] is not an object' % i
        why = DF.members_problem(g.get('members'), 'export.groups[%d]' % i)
        if why:
            return why
        n += len(g['members'])
        if n > DF.MAX_KEYS:
            return 'at most %d datasets in the manual groups' % DF.MAX_KEYS
    return ''


def stamp(t=None):
    """YYYY-MM-DD_HH-MM-SS in the panel PC's local time."""
    return time.strftime('%Y-%m-%d_%H-%M-%S', time.localtime(time.time() if t is None else t))


def _claim(tmp, folder, stem):
    """Give the written temp file its name: <stem>.json, else <stem>_2.json ... - never over a file
    that is there (on Windows os.rename refuses an existing name; elsewhere a hard link does)."""
    for k in range(1, MAX_SAME_SECOND + 1):
        name = stem + ('' if k == 1 else '_%d' % k) + '.json'
        path = os.path.join(folder, name)
        if os.path.exists(path):
            continue
        try:
            if os.name == 'nt':
                os.rename(tmp, path)
            else:
                os.link(tmp, path)
                os.unlink(tmp)
        except FileExistsError:
            continue                          # taken between the look and the rename: the next one
        return name, path
    raise OSError('%d files named %s*.json are there already' % (MAX_SAME_SECOND, stem))


def write(cfg, obj, now=None, log=None):
    """Write one export -> (http code, answer). `log(text, lvl)` hears what was written."""
    why = export_problem(obj)
    if why:
        return 400, {'error': why}
    folder, why = export_dir(cfg)
    if why:
        return 503, {'error': why}
    try:
        text = json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False)
    except ValueError as e:                    # NaN / Infinity: JSON has none, the page never sends one
        return 400, {'error': 'export: %s' % e}
    try:
        data = text.encode('utf-8')
    except UnicodeEncodeError:                 # a lone surrogate in a text: written escaped
        data = json.dumps(obj, indent=2, ensure_ascii=True, allow_nan=False).encode('ascii')
    tmp = None
    try:
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix='.' + NAME_PREFIX, suffix='.tmp')
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        name, path = _claim(tmp, folder, NAME_PREFIX + stamp(now))
        tmp = None
    except OSError as e:
        return 503, {'error': 'the export could not be written into %s: %s' % (folder, e), 'dir': folder}
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    groups = obj['groups']
    n = sum(len(g['members']) for g in groups)
    if log is not None:
        try:
            log('History data folder: exported %d manual group(s) (%d datasets) to %s'
                % (len(groups), n, path), 'info')
        except Exception:                                              # noqa: BLE001
            pass
    return 200, {'ok': True, 'name': name, 'path': path, 'dir': folder, 'bytes': len(data),
                 'groups': len(groups), 'datasets': n}
