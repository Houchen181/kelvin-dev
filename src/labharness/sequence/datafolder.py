# -*- coding: utf-8 -*-
"""
panel_datafolder.py - History's data folder on the panel's side: what must be the same in every
browser. The page groups the index (app.js); the panel keeps

    the index        panel_dataindex.DataIndex - the data folder, read-only, in a thread
    the manual groups  ManualStore  - <state>/datafolder/manual_groups.json, one
                       compare-and-swap write of the whole state per edit (merge, move, solo,
                       unmanual, restore, import, undo, redo, a rule change), every accepted
                       write also appended to manual_journal.jsonl (nothing is ever deleted)
    the handoffs     HandoffStore - handoffs.json: a group the page handed to an analysis
                       script, by its token (the newest 200)
    the analyses     AnalysisStore - analyses.json: one record per analysis item run on History
                       groups (past 2,000 the oldest go to analyses_archive.jsonl)
    the analysis folders  folder_analyses - read only: <data root>/<day>/analysis/<folder>/
                       analysis.json, whoever wrote it (0.1.7)

Nothing here writes under the data folder. Each store has its own lock and loads its file on
first use; none is ever taken while the engine lock is held, and none is held while the
engine's log is called (the engine's log takes the engine lock). DataFolder(engine) does no
file I/O; start() loads the stores and starts the index.

The rule a dataset key must follow ('<YYYY-MM-DD>/<folder>') is panel_dataindex.key_problem.
"""
import copy
import datetime
import hashlib
import json
import math
import os
import re
import threading
import time

from . import paths as P
from . import dataindex as DI

df_dir = DI.df_dir

MANUAL_NAME = 'manual_groups.json'
MANUAL_FORMAT = 'labmaster-datafolder-manual'
JOURNAL_NAME = 'manual_journal.jsonl'
JOURNAL_MAX = 20 * 1024 * 1024
ANALYSES_NAME = 'analyses.json'
ARCHIVE_NAME = 'analyses_archive.jsonl'
ANALYSES_KEEP = 2000
HANDOFFS_NAME = 'handoffs.json'
HANDOFFS_KEEP = 200
GID_RE = re.compile(r'^m[0-9A-Za-z_-]{1,48}$')
HOW_RE = re.compile(r'^[a-z]{1,20}$')
CLIENT_RE = re.compile(r'^[0-9a-z]{1,40}$')
TOKEN_RE = re.compile(r'^[0-9a-f]{16,40}$')
MODULE_RE = re.compile(r'^[A-Za-z_][\w:.-]{0,120}$')
MAX_GROUPS = 10000
MAX_MEMBERS = 20000
MAX_KEYS = 50000
LABEL_MAX = 300
DEFAULT_RULE = {'gap_hours': 3, 'compare': []}


def _now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


# Round 16 (2026-10-16): the numbers a run gave, kept with its record so the card, the figures dialog and By run can
# show them and compare runs. A script's result is its labmaster_analysis.result() values (the runner's ##PANEL line
# or its analysis.json's `results`); another manifest's `results` lose the keys that only describe the run.
RESULTS_MAX = 40
RESULTS_STR_MAX = 80
RESULTS_SKIP_OTHER = frozenset((
    'event', 'analysis', 'group', 'folder', 'manifest', 'csv', 'verdict', 'problems', 'format', 'script',
    'n_figures', 'results', 'quality', 'n_datasets', 'n_used', 'exit_code', 'fit', 'quality_problems',
    'fit_problems'))


def _scalars(src, my=True):
    """{key: number | bool | str} of a run's result `src`, in its own order: at most RESULTS_MAX entries, strings cut
    to RESULTS_STR_MAX characters; non-finite numbers, None, lists and objects left out; never 'quality' (the
    verdict). `my` True: `src` is a script's `results` (only 'quality' is skipped); False: another manifest's
    `results` (RESULTS_SKIP_OTHER is skipped too). Anything not a dict gives {}."""
    out = {}
    if not isinstance(src, dict):
        return out
    for k, v in src.items():
        if len(out) >= RESULTS_MAX:
            break
        k = str(k)
        if k == 'quality' or (not my and k in RESULTS_SKIP_OTHER):
            continue
        if isinstance(v, bool):
            out[k] = v
        elif isinstance(v, (int, float)):
            if _finite(v):
                out[k] = v
        elif isinstance(v, str):
            out[k] = v[:RESULTS_STR_MAX]
    return out


def _log(engine, text, lvl):
    try:
        engine.log(text, lvl)
    except Exception:                                                  # noqa: BLE001
        pass


def rule_problem(rule):
    """'' when `rule` is {gap_hours: 0..1000, compare: a subset of the optional keys}."""
    if not isinstance(rule, dict):
        return 'rule must be an object {gap_hours, compare}'
    g = rule.get('gap_hours')
    if not _finite(g) or not 0 <= g <= 1000:
        return 'rule.gap_hours must be a number of hours from 0 to 1000 (got %r)' % (g,)
    c = rule.get('compare', [])
    if not isinstance(c, list) or any(x not in DI.OPTIONAL_IGNORE for x in c) or \
            len(set(c)) != len(c):
        return ('rule.compare must list some of %s, each once (got %r)'
                % (', '.join(DI.OPTIONAL_IGNORE), c))
    return ''


def clean_rule(rule):
    return {'gap_hours': rule['gap_hours'], 'compare': list(rule.get('compare') or [])}


def members_problem(members, what):
    if not isinstance(members, list) or not 1 <= len(members) <= MAX_MEMBERS:
        return '%s: members must be a list of 1 to %d dataset keys' % (what, MAX_MEMBERS)
    for k in members:
        why = DI.key_problem(k)
        if why:
            return '%s: %s' % (what, why)
    if len(set(members)) != len(members):
        return '%s: a dataset is listed twice' % what
    return ''


def _rel(path, root):
    """A path an analysis wrote, relative to the data root with '/' - absolute (with '/')
    when it is outside it; None for nothing."""
    if not isinstance(path, str) or not path:
        return None
    a = os.path.abspath(path)
    try:
        r = os.path.relpath(a, os.path.abspath(root))
        if not r.startswith('..') and not os.path.isabs(r):
            return r.replace(os.sep, '/')
    except ValueError:                                                 # another drive
        pass
    return a.replace(os.sep, '/')


def _key_of(path):
    """The dataset key of a dataset folder path: '<day>/<folder>'."""
    p = os.path.normpath(str(path))
    return os.path.basename(os.path.dirname(p)) + '/' + os.path.basename(p)


def key_path(root, key):
    """The folder of dataset key `key` (key_problem-clean) under the data root."""
    day, _, folder = key.partition('/')
    return os.path.join(root, day, folder)


# ---------------------------------------------------------------- the manual groups

class ManualStore(object):
    """The manual groups and the grouping rule, the same in every browser. A write is the
    whole new state, accepted only on the rev it was computed from (else 409)."""

    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.Lock()
        self.state = None                   # loaded on first use
        self.rev = None                     # read without the lock (versions())
        self._journal_warned = False

    def _path(self):
        return os.path.join(df_dir(), MANUAL_NAME)

    def _empty(self):
        return {'format': MANUAL_FORMAT, 'version': 1, 'rev': 0, 'saved': '',
                'rule': dict(DEFAULT_RULE, compare=[]), 'groups': {}}

    def _load(self):
        """Under the lock. -> the log lines to say (after the lock is let go)."""
        if self.state is not None:
            return []
        said = []
        path = self._path()
        got = P.read_json(path, None, expect=dict)
        bad = P.read_problem(path)
        state = None
        if isinstance(got, dict) and got.get('format') == MANUAL_FORMAT \
                and _int(got.get('rev')) and isinstance(got.get('groups'), dict) \
                and not rule_problem(got.get('rule')):
            state = got
        elif got is not None and not bad:
            bad = {'problem': 'it is not a manual-groups file'}
        if state is None and (bad or not os.path.exists(path)):
            last = self._journal_last()
            if last is not None:
                state = {'format': MANUAL_FORMAT, 'version': 1, 'rev': last['rev'],
                         'saved': _now(), 'rule': last['rule'], 'groups': last['groups']}
                try:
                    P.write_json(path, state)
                    said.append(('History data folder: %s %s - the manual groups were restored '
                                 'from the journal (rev %d, %d group(s))'
                                 % (MANUAL_NAME, 'could not be read' if bad else 'was missing',
                                    state['rev'], len(state['groups'])), 'warn'))
                except (OSError, TypeError, ValueError) as e:
                    said.append(('History data folder: the manual groups were restored from the '
                                 'journal but could not be written back (%s)' % e, 'warn'))
            elif bad:
                said.append(('History data folder: %s could not be read (%s); it starts empty '
                             '(a copy is kept)' % (MANUAL_NAME, bad.get('problem')), 'warn'))
        self.state = state or self._empty()
        self.rev = self.state['rev']
        return said

    def _journals(self):
        d = df_dir()
        try:
            names = [n for n in os.listdir(d)
                     if n == JOURNAL_NAME or re.match(r'^manual_journal-\d{8}-\d{6}\.jsonl$', n)]
        except OSError:
            return []
        # the rotated ones by their time, the live one last
        return [os.path.join(d, n) for n in sorted(names, key=lambda n: (n == JOURNAL_NAME, n))]

    def _journal_last(self):
        """The newest valid journal state {rev, rule, groups}, or None."""
        for path in reversed(self._journals()):
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.read().splitlines()
            except OSError:
                continue
            for ln in reversed(lines):
                try:
                    j = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(j, dict) and _int(j.get('rev')) and isinstance(j.get('groups'), dict) \
                        and not rule_problem(j.get('rule')):
                    return {'rev': j['rev'], 'rule': clean_rule(j['rule']), 'groups': j['groups']}
        return None

    def load(self):
        with self.lock:
            said = self._load()
        for text, lvl in said:
            _log(self.engine, text, lvl)

    def get(self):
        with self.lock:
            said = self._load()
            out = copy.deepcopy(self.state)
        for text, lvl in said:
            _log(self.engine, text, lvl)
        return out

    @staticmethod
    def validate_groups(groups):
        """(normalised groups, '') or (None, why)."""
        if not isinstance(groups, dict):
            return None, 'groups must be an object {id: {members, ...}}'
        if len(groups) > MAX_GROUPS:
            return None, 'at most %d manual groups (got %d)' % (MAX_GROUPS, len(groups))
        out, owner, n_keys = {}, {}, 0
        for gid, g in groups.items():
            if not isinstance(gid, str) or not GID_RE.match(gid):
                return None, 'not a manual group id: %r' % (str(gid)[:60],)
            if not isinstance(g, dict):
                return None, 'group %s is not an object' % gid
            why = members_problem(g.get('members'), 'group %s' % gid)
            if why:
                return None, why
            for k in g['members']:
                if k in owner:
                    return None, '%s is in two groups (%s and %s)' % (k, owner[k], gid)
                owner[k] = gid
            n_keys += len(g['members'])
            if n_keys > MAX_KEYS:
                return None, 'at most %d datasets in the manual groups' % MAX_KEYS
            typ = g.get('type')
            if typ is not None and DI.type_problem(typ):
                return None, 'group %s: %s' % (gid, DI.type_problem(typ))
            created = g.get('created')
            if created is not None and (not isinstance(created, str) or len(created) > 40):
                return None, 'group %s: created must be text of at most 40 characters' % gid
            how = g.get('how')
            if not isinstance(how, str) or not HOW_RE.match(how):
                return None, 'group %s: how must be a word in small letters (got %r)' % (gid, how)
            frm = g.get('from')
            if frm is not None and not isinstance(frm, list):
                return None, 'group %s: from must be a list' % gid
            out[gid] = {'members': list(g['members']), 'type': typ, 'created': created,
                        'how': how,
                        'from': None if frm is None else [str(x)[:120] for x in frm[:50]]}
        return out, ''

    def put(self, body, addr=''):
        """One write of the whole state -> (code, answer): 200 {ok, rev, saved}, 409
        {error, conflict, rev}, 400 {error} (nothing written), 503 {error} (the file could not
        be written; memory unchanged)."""
        if not isinstance(body, dict):
            return 400, {'error': 'the body must be an object'}
        base = body.get('base')
        if not _int(base):
            return 400, {'error': 'base must be the rev the page read (a whole number)'}
        op = str(body.get('op') or 'edit')[:40]
        label = str(body.get('label') or '')[:LABEL_MAX]            # metadata: clipped, never a 400
        client = str(body.get('client') or '')
        client = client if CLIENT_RE.match(client) else ''
        rule = body.get('rule')
        if rule is not None:
            why = rule_problem(rule)
            if why:
                return 400, {'error': why}
            rule = clean_rule(rule)
        if 'groups' not in body:
            if op != 'rule':
                return 400, {'error': 'groups are left out only by a rule change (op "rule")'}
            groups = None
        else:
            groups, why = self.validate_groups(body.get('groups'))
            if why:
                return 400, {'error': why}
        said = []
        with self.lock:
            said += self._load()
            cur = self.state
            if base != cur['rev']:
                ans = (409, {'error': 'the manual groups changed since this page read them '
                                      '(rev %d)' % cur['rev'], 'conflict': True, 'rev': cur['rev']})
            else:
                new = {'format': MANUAL_FORMAT, 'version': 1, 'rev': cur['rev'] + 1,
                       'saved': _now(), 'rule': rule if rule is not None else copy.deepcopy(cur['rule']),
                       'groups': groups if groups is not None else copy.deepcopy(cur['groups'])}
                try:
                    P.write_json(self._path(), new)
                except (OSError, TypeError, ValueError) as e:
                    ans = (503, {'error': 'the manual groups could not be saved: %s' % e})
                else:
                    self.state = new
                    self.rev = new['rev']
                    ans = (200, {'ok': True, 'rev': new['rev'], 'saved': new['saved']})
                    line = {'rev': new['rev'], 'at': new['saved'], 'op': op, 'label': label,
                            'client': client, 'addr': str(addr or '')[:60],
                            'rule': new['rule'], 'groups': new['groups']}
                    said += self._journal(line)
        for text, lvl in said:
            _log(self.engine, text, lvl)
        return ans

    def _journal(self, line):
        """Under the lock, after the file was written: one line appended and fsynced; renamed
        aside past 20 MB. A failure never changes the answer; said once."""
        path = os.path.join(df_dir(), JOURNAL_NAME)
        try:
            try:
                if os.path.getsize(path) > JOURNAL_MAX:
                    os.replace(path, os.path.join(df_dir(), 'manual_journal-%s.jsonl'
                                                  % time.strftime('%Y%m%d-%H%M%S')))
            except OSError:
                pass
            with open(path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(line, ensure_ascii=False) + '\n')
                f.flush()
                os.fsync(f.fileno())
            return []
        except (OSError, TypeError, ValueError) as e:
            if self._journal_warned:
                return []
            self._journal_warned = True
            return [('History data folder: the manual groups were saved, but their journal could '
                     'not be appended (%s)' % e, 'warn')]


# ---------------------------------------------------------------- the handoffs

GROUP_FIELDS = ('gid', 'manual', 'type', 'type_name', 'classes', 'analysis', 'start', 'end',
                'open', 'n', 'unit', 'rev', 'rule')


def group_problem(g):
    """(the whitelisted, bounded group, '') or (None, why): anything else is dropped."""
    if not isinstance(g, dict):
        return None, 'group must be an object'
    out = {}
    gid = g.get('gid')
    if gid is not None and (not isinstance(gid, str) or len(gid) > 300
                            or not (gid.startswith('a:') or gid.startswith('m'))):
        return None, 'group.gid must be a group id (a:... or m...)'
    out['gid'] = gid
    typ = g.get('type')
    if typ is not None and DI.type_problem(typ):
        return None, 'group.' + DI.type_problem(typ)
    out['type'] = typ
    tn = g.get('type_name')
    if tn is not None and (not isinstance(tn, str) or len(tn) > 120):
        return None, 'group.type_name must be text of at most 120 characters'
    out['type_name'] = tn
    cl = g.get('classes')
    if cl is not None and (not isinstance(cl, list) or len(cl) > 50
                           or any(not isinstance(c, str) or len(c) > 120 for c in cl)):
        return None, 'group.classes must be at most 50 class names'
    out['classes'] = list(cl) if cl is not None else None
    an = g.get('analysis')
    if an is not None and (not isinstance(an, str) or not MODULE_RE.match(an)):
        return None, 'group.analysis is not the name of an analysis'
    out['analysis'] = an
    for k in ('start', 'end'):
        v = g.get(k)
        if v is not None and not _finite(v):
            return None, 'group.%s must be a number (wall-clock seconds) or null' % k
        out[k] = v
    for k in ('n', 'rev'):
        v = g.get(k)
        if v is not None and not _int(v):
            return None, 'group.%s must be a whole number' % k
        out[k] = v
    unit = g.get('unit')
    if unit is not None and unit not in ('rep', 'scan'):
        return None, 'group.unit must be rep or scan'
    out['unit'] = unit
    for k in ('open', 'manual'):
        v = g.get(k)
        if v is not None and not isinstance(v, bool):
            return None, 'group.%s must be true or false' % k
        out[k] = v
    rule = g.get('rule')
    if rule is not None:
        why = rule_problem(rule)
        if why:
            return None, 'group.' + why
        rule = clean_rule(rule)
    out['rule'] = rule
    return out, ''


class HandoffStore(object):
    """A History group handed to an analysis by its token: {token, created, group, members},
    the newest HANDOFFS_KEEP kept."""

    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.Lock()
        self.items = None

    def _path(self):
        return os.path.join(df_dir(), HANDOFFS_NAME)

    def _load(self):
        if self.items is not None:
            return
        got = P.read_json(self._path(), None, expect=dict)
        rows = got.get('handoffs') if isinstance(got, dict) else None
        self.items = [h for h in (rows or []) if isinstance(h, dict) and isinstance(h.get('token'), str)
                      and isinstance(h.get('members'), list)]

    def load(self):
        with self.lock:
            self._load()

    def put(self, body):
        if not isinstance(body, dict):
            return 400, {'error': 'the body must be an object'}
        tok = body.get('token')
        if not isinstance(tok, str) or not TOKEN_RE.match(tok):
            return 400, {'error': 'not a History group token: %r' % (str(tok)[:60],)}
        mem = body.get('members')
        why = members_problem(mem, 'the History group')
        if why:
            return 400, {'error': why}
        grp, why = group_problem(body.get('group'))
        if why:
            return 400, {'error': why}
        root = P.data_root(self.engine.cfg)
        missing = [k for k in mem if not os.path.isdir(key_path(root, k))]
        with self.lock:
            self._load()
            old = next((h for h in self.items if h['token'] == tok), None)
            if old is not None:
                if old.get('members') == mem and old.get('group') == grp:
                    return 200, {'ok': True, 'token': tok, 'n': len(mem), 'missing': missing}
                return 409, {'error': 'that token is already another group'}
            items = self.items + [{'token': tok, 'created': _now(), 'group': grp,
                                   'members': list(mem)}]
            items = items[-HANDOFFS_KEEP:]
            try:
                P.write_json(self._path(), {'format': 'labmaster-datafolder-handoffs', 'version': 1,
                                            'handoffs': items})
            except (OSError, TypeError, ValueError) as e:
                return 503, {'error': 'the History group could not be handed over: %s' % e}
            self.items = items
        return 200, {'ok': True, 'token': tok, 'n': len(mem), 'missing': missing}

    def get(self, token):
        """The handoff with its absolute paths (the existing ones, in member order), the keys
        that are gone, and the data root. KeyError for a token not known (the route's 404)."""
        with self.lock:
            self._load()
            h = next((x for x in self.items if x['token'] == token), None)
            h = copy.deepcopy(h) if h is not None else None
        if h is None:
            raise KeyError(token)
        root = P.data_root(self.engine.cfg)
        paths, missing = [], []
        for k in h.get('members') or []:
            p = key_path(root, k) if not DI.key_problem(k) else None
            if p and os.path.isdir(p):
                paths.append(p)
            else:
                missing.append(k)
        h.update(paths=paths, missing=missing, data_root=root)
        return h


# ---------------------------------------------------------------- the analyses

class AnalysisStore(object):
    """One record per analysis item run on History groups (whatever its end)."""

    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.Lock()
        self.records = None
        self.ver = None
        self._n = 0

    def _path(self):
        return os.path.join(df_dir(), ANALYSES_NAME)

    def _load(self):
        if self.records is not None:
            return
        got = P.read_json(self._path(), None, expect=dict)
        rows = got.get('records') if isinstance(got, dict) else None
        self.records = [r for r in (rows or []) if isinstance(r, dict) and r.get('id')]
        self.ver = got.get('ver') if isinstance(got, dict) and _int(got.get('ver')) else 0

    def load(self):
        with self.lock:
            self._load()

    def get(self):
        with self.lock:
            self._load()
            return {'ver': self.ver, 'records': copy.deepcopy(self.records)}

    def add(self, job, k, item, handoff, token=None):
        """The record of analysis item k (1-based) of `job`, written; the record.

        Round 12 (2026-10-14): `token` - the History group this record is for, when the item is a combined run over
        several groups (dfgroups): one record per group, each its own handoff's, and `asked` that group's members
        among the item's datasets (in the item's order), so each card shows what of IT was analysed. None: the item's
        own dfgroup, `asked` every dataset of the item. Kind 'my' (a script of my_analysis, panel_myscripts), with
        script (its file), script_id, params and the run's results."""
        res = item.get('result') if isinstance(item.get('result'), dict) else {}
        root = P.data_root(self.engine.cfg)
        grp = (handoff or {}).get('group') or {}
        used = None
        man = res.get('manifest')
        if isinstance(man, str) and man:
            try:
                with open(man, 'r', encoding='utf-8') as f:
                    inp = (json.load(f) or {}).get('inputs') or {}
                if isinstance(inp.get('used_paths'), list):
                    used = [_key_of(p) for p in inp['used_paths'] if isinstance(p, str)]
            except (OSError, ValueError, AttributeError):
                used = None
        probs = res.get('problems') if isinstance(res.get('problems'), list) else []
        asked = [_key_of(p) for p in item.get('paths') or []]
        if token:
            mem = set((handoff or {}).get('members') or [])
            asked = [a for a in asked if a in mem]
            if used is not None:
                used = [u for u in used if u in mem]
        with self.lock:
            self._load()
            self._n += 1
            rec = {
                'id': 'da_%d_%d' % (int(time.time()), self._n + len(self.records)),
                'at': item.get('finished') or time.time(), 'started': item.get('started'),
                'job': (job or {}).get('id'), 'item': k, 'history_row': item.get('history_row'),
                'state': item.get('state'), 'analysis': item.get('analysis'),
                'verdict': res.get('verdict'), 'problems': [str(p) for p in probs][:10],
                'error': item.get('error'),
                'members': list((handoff or {}).get('members') or []),
                'asked': asked,
                'used': used,
                'kind': 'my',
                'script': item.get('script_file'),
                'script_id': item.get('script'),
                'params': copy.deepcopy(item['params']) if isinstance(item.get('params'), dict) else {},
                # round 16: the run's numbers - its labmaster_analysis.result() values
                'results': _scalars(res.get('results'), True),
                # start / end: the index's wall clock (getUTC* in the page, gmtime in Python)
                'group': {'token': token or item.get('dfgroup'), 'gid': grp.get('gid'),
                          'manual': grp.get('manual'), 'type': grp.get('type'),
                          'type_name': grp.get('type_name'), 'start': grp.get('start'),
                          'end': grp.get('end'), 'n': grp.get('n'), 'unit': grp.get('unit'),
                          'rev': grp.get('rev'), 'analysis': grp.get('analysis')},
                'outputs': {'folder': _rel(res.get('folder'), root), 'name': res.get('group'),
                            'n_figures': res.get('n_figures'),
                            'manifest': _rel(res.get('manifest'), root)}}
            records = self.records + [rec]
            spill = records[:-ANALYSES_KEEP] if len(records) > ANALYSES_KEEP else []
            if spill:
                with open(os.path.join(df_dir(), ARCHIVE_NAME), 'a', encoding='utf-8') as f:
                    for r in spill:
                        f.write(json.dumps(r, ensure_ascii=False, default=str) + '\n')
                    f.flush()
                    os.fsync(f.fileno())
                records = records[len(spill):]
            ver = self.ver + 1
            P.write_json(self._path(), {'format': 'labmaster-datafolder-analyses', 'version': 1,
                                        'ver': ver, 'records': records})
            self.records, self.ver = records, ver
            return copy.deepcopy(rec)


# ---------------------------------------------------------------- the analyses in the data folder
# 0.1.7: History reads the days' analysis\ folders too, so an analysis not run from History (a sequence block that
# runs an analysis script, a script run by hand) shows on its group: its figures, the "analysed" line, the comparison
# table. Read only - nothing is written, analyses.json is never touched; each folder with an analysis.json (the
# manifest run_myscript.py writes, or one written the same way by any script) becomes a record shaped like
# AnalysisStore's (id 'fa_...', source 'folder'), served beside the records (DataFolder.analyses_get: 'folders',
# 'fsig'). Which datasets it used: its analysis.json's inputs (used_paths / used / datasets / paths); a folder
# without an analysis.json, or one that names no dataset, is left out. The folders are listed by
# panel_notes.analysis_folders (cached by each day's analysis\ mtime); a folder's record is kept while its key (its
# mtime, analysis.json's mtime and size, its figure count) stays.
FA_DAYS = 3650                      # every day folder
_FA_LOCK = threading.Lock()
_FA = {}                            # '<day>/analysis/<folder>' -> (key, record or None)
_FA_LAST = {}                       # data root -> (signature, records) of its last whole answer
_FA_DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _fa_stat(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _fa_epoch(text):
    """A manifest's local wall clock ('2026-10-06T18:16:17', or with a space) as epoch seconds; None."""
    try:
        return time.mktime(time.strptime(str(text)[:19].replace(' ', 'T'), '%Y-%m-%dT%H:%M:%S'))
    except (ValueError, OverflowError, TypeError):
        return None


def _fa_keys(day, paths):
    """Dataset keys '<day>/<folder>' of what a manifest names (a whole path keeps its own day, a bare name is the
    analysis folder's day), in order, once each."""
    out = []
    for p in paths or []:
        if not isinstance(p, str) or not p.strip():
            continue
        parts = [x for x in re.split(r'[\\/]+', p.strip()) if x]
        if len(parts) >= 2 and _FA_DAY_RE.match(parts[-2]):
            k = parts[-2] + '/' + parts[-1]
        else:
            k = day + '/' + parts[-1]
        if k not in out:
            out.append(k)
    return out


def _fa_record(root, item):
    """The record of one analysis folder (an item of panel_notes.analysis_folders), or None when it has no readable
    analysis.json or that names no dataset."""
    day, folder = item['day'], item['folder']
    rel = '%s/analysis/%s' % (day, folder)
    path = os.path.join(root, day, 'analysis', folder)
    try:
        with open(os.path.join(path, 'analysis.json'), 'r', encoding='utf-8') as f:
            man = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(man, dict):
        return None
    rec = {'id': 'fa_' + hashlib.sha1(rel.encode('utf-8')).hexdigest()[:16], 'source': 'folder',
           'started': None, 'job': None, 'item': None, 'history_row': None, 'problems': [], 'error': None,
           'group': {}, 'params': {}, 'results': {}, 'kind': 'other', 'script': None, 'script_id': None}
    inp = man.get('inputs') if isinstance(man.get('inputs'), dict) else {}
    asked, used = [], None
    for key in ('used_paths', 'used', 'datasets', 'paths'):
        v = inp.get(key)
        if isinstance(v, list):
            ks = _fa_keys(day, v)
            asked.extend(k for k in ks if k not in asked)
            if key in ('used_paths', 'used') and used is None and ks:
                used = ks
    if not asked:
        return None
    my = man.get('format') == 'my-analysis'
    ana = man.get('analysis') if isinstance(man.get('analysis'), str) else None
    res = man.get('results') if isinstance(man.get('results'), dict) else {}
    when = man.get('when') if isinstance(man.get('when'), str) else None
    verdict = man.get('verdict') if my else (res.get('quality') or res.get('fit') or man.get('verdict'))
    rec.update(state='done', analysis=ana, when=when, members=asked, asked=list(asked), used=used,
               at=_fa_epoch(when) or item.get('mtime'), kind='my' if my else 'other',
               verdict=verdict if isinstance(verdict, str) else None, results=_scalars(res, my))
    probs = man.get('problems') if my else (res.get('quality_problems') or res.get('fit_problems'))
    rec['problems'] = [str(x) for x in probs][:10] if isinstance(probs, list) else []
    if my:
        sc = man.get('script') if isinstance(man.get('script'), dict) else {}
        rec['script'] = sc.get('file') if isinstance(sc.get('file'), str) else None
        rec['script_id'] = sc['id'] if isinstance(sc.get('id'), str) and sc['id'] else ana
        if isinstance(man.get('params'), dict):
            rec['params'] = man['params']
    rec['outputs'] = {'folder': rel, 'name': folder, 'n_figures': int(item.get('n_figures') or 0),
                      'manifest': rel + '/analysis.json'}
    return rec


def _fa_tail(folder):
    """'<day>/analysis/<name>' of a record's folder however it was written (relative, absolute, '\\'), lower case."""
    parts = [x for x in re.split(r'[\\/]+', str(folder or '')) if x]
    return '/'.join(parts[-3:]).lower() if len(parts) >= 3 else None


_FA_MY_ID = re.compile(r'^my:(0|[1-9][0-9]{0,2}):.+\.py$')


def _fa_my_ids(cfg, recs):
    """A script's folder whose analysis.json names no script id ('my:a_fit' only) gets the id the panel's records use
    for that script ('my:<k>:a_fit.py' - the folder of my_analysis.dirs holding it, by the file the manifest names,
    else the one folder that has a file of that name), so History's card draws it on that script's line, not on a
    line of its own. In place; left as it was when that cannot be told."""
    need = [r for r in recs if r.get('kind') == 'my' and not _FA_MY_ID.match(str(r.get('script_id') or ''))]
    if not need:
        return
    try:
        from . import myscripts as MS
        ds = [os.path.normcase(os.path.abspath(d)) for d in MS.dirs(cfg)]
    except Exception:                                                  # noqa: BLE001
        return
    for r in need:
        sc = r.get('script') if isinstance(r.get('script'), str) else ''
        a = str(r.get('analysis') or '')
        fname = os.path.basename(sc) if sc else (a[3:] + '.py' if a.startswith('my:') and len(a) > 3 else '')
        if not fname.endswith('.py') or not re.match(r'^[^\\/:*?"<>|]+$', fname):
            continue
        ks = []
        if sc and os.path.isabs(sc):
            ks = [k for k, d in enumerate(ds) if os.path.normcase(os.path.dirname(os.path.abspath(sc))) == d]
        if not ks:
            ks = [k for k, d in enumerate(ds) if os.path.isfile(os.path.join(d, fname))]
        if len(ks) == 1:
            r['script_id'] = 'my:%d:%s' % (ks[0], fname)


def folder_analyses(root):
    """(signature, [record]) of every analysis folder of the data folder `root`, newest first (see above); the
    signature changes when any folder's record may have. Read only; a folder it cannot read is left out."""
    out, seen, sig = [], set(), []
    with _FA_LOCK:
        # the list under this lock: the index's thread and the routes never walk panel_notes' caches at once
        try:
            import panel_notes as PN
            items = PN.analysis_folders(root, FA_DAYS)
        except Exception:                                              # noqa: BLE001 - the last answer, as it was
            last = _FA_LAST.get(os.path.normcase(os.path.abspath(root)))
            return (last[0], copy.deepcopy(last[1])) if last else ('', [])
        for it in items:
            if not isinstance(it, dict) or not it.get('day') or not it.get('folder'):
                continue
            rel = '%s/analysis/%s' % (it['day'], it['folder'])
            path = os.path.join(root, it['day'], 'analysis', it['folder'])
            mj = _fa_stat(os.path.join(path, 'analysis.json'))
            key = (os.path.normcase(os.path.abspath(root)), it.get('mtime'), mj, it.get('n_figures'))
            seen.add(rel)
            hit = _FA.get(rel)
            if hit is None or hit[0] != key:
                try:
                    hit = (key, _fa_record(root, it) if mj is not None else None)
                except Exception:                                      # noqa: BLE001 - that folder is left out
                    hit = (key, None)
                _FA[rel] = hit
            sig.append('%s|%r' % (rel, key[1:]))
            if hit[1] is not None:
                out.append(copy.deepcopy(hit[1]))
        for k in [k for k in _FA if k not in seen]:
            del _FA[k]
        out.sort(key=lambda r: -(r.get('at') or 0))
        h = hashlib.sha1('\n'.join(sorted(sig)).encode('utf-8')).hexdigest()[:16]
        _FA_LAST[os.path.normcase(os.path.abspath(root))] = (h, copy.deepcopy(out))
    return h, out


# ---------------------------------------------------------------- the facade

class DataFolder(object):
    """The engine's History data folder (Engine.df): the index and the three stores. No file
    I/O here; start() loads the stores and starts the index."""

    def __init__(self, engine):
        self.engine = engine
        self.index_error = ''
        try:
            self.index = DI.DataIndex(lambda: P.data_root(engine.cfg), engine.log)
        except Exception as e:                                         # noqa: BLE001
            self.index = None
            self.index_error = '%s: %s' % (type(e).__name__, e)
        self.manual = ManualStore(engine)
        self.analyses = AnalysisStore(engine)
        self.handoffs = HandoffStore(engine)
        self.fa_sig = None                  # folder_analyses' signature, last seen (0.1.7; read by versions())
        if self.index is not None:
            self.index.after_scan = self.folders_refresh

    def start(self):
        for s in (self.manual, self.analyses, self.handoffs):
            s.load()
        if self.index is not None:
            self.index.start()

    def folders_refresh(self):
        """The analysis folders looked at again (on the index's thread, after each of its scans): fa_sig, which the
        snapshot carries so a page reads the analyses again when a folder came, went or changed."""
        try:
            self.fa_sig = folder_analyses(P.data_root(self.engine.cfg))[0]
        except Exception:                                              # noqa: BLE001
            pass

    def versions(self):
        """Attribute reads only - no lock, no I/O (the engine's snapshot)."""
        idx = self.index
        return {'index': idx.version if idx is not None else None,
                'scanning': bool(idx is not None and idx.pending()),
                'error': (idx.error if idx is not None else self.index_error) or None,
                'manual': self.manual.rev, 'analyses': self.analyses.ver, 'ana_folders': self.fa_sig}

    def index_request(self):
        if self.index is not None:
            self.index.request()

    def manual_get(self):
        return self.manual.get()

    def manual_put(self, body, addr=''):
        return self.manual.put(body, addr)

    def handoff_put(self, body):
        return self.handoffs.put(body)

    def handoff_get(self, token):
        return self.handoffs.get(token)

    def analyses_get(self):
        """The records (AnalysisStore's, unchanged) and - 0.1.7 - 'folders': the data folder's analyses no record
        names (by its folder), read only; 'fsig' their signature (the snapshot's 'ana_folders')."""
        got = self.analyses.get()
        try:
            sig, folders = folder_analyses(P.data_root(self.engine.cfg))
        except Exception:                                              # noqa: BLE001 - the records alone
            return got
        known = set()
        for r in got['records']:
            t = _fa_tail((r.get('outputs') or {}).get('folder'))
            if t:
                known.add(t)
        got['folders'] = [f for f in folders if _fa_tail(f['outputs']['folder']) not in known]
        _fa_my_ids(getattr(self.engine, 'cfg', None) or {}, got['folders'])
        got['fsig'] = sig
        self.fa_sig = sig
        return got

    def record_analysis(self, job, k, item, token=None):
        """The analysis record of item k of `job`: for its dfgroup, or - round 12 (2026-10-14), a combined run over
        several History groups - for the group `token` (one of its dfgroups; Engine._df_record_job calls this once
        per group)."""
        tok = token or item.get('dfgroup')
        try:
            ho = self.handoffs.get(tok) if tok else None
        except KeyError:
            ho = None
        if token:
            return self.analyses.add(job, k, item, ho, token=token)
        return self.analyses.add(job, k, item, ho)
