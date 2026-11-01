"""
run_block.py - runs ONE palette block (a measurement script) in its own process.

The panel engine starts this with cwd = paths.work_dir (the root by default), so a measurement
writes to <root>/data/<date>/#NNN_... exactly as when the script is run by hand.

    python -u run_block.py --target <abs .py> --reps 4 --params '{"n_points": 41}' \
                           [--pause-file <path>] [--sim] [--sim-seconds 6] \
                           [--data-root <dir>] [--safe-stop <script, or ''>] \
                           [--script-dir <folder of the original script>] [--orphan-dir <dir>]

Why once per rep: a rep of a real measurement can take tens of minutes, and the panel has to be
able to pause *between* reps. So instead of main(numreps=4) we call main(numreps=1) four times:
the script contract is "one call of main() = one rep = one dataset".

The script sees, in its environment: LABMASTER_DATA_ROOT (where datasets go), LABMASTER_REP
and LABMASTER_REPS (this rep, of how many), LABMASTER_SIM ('0' - a simulated block never runs
the script).

Communication with the engine: normal prints are the log, and machine-readable events are
single lines of `##PANEL {json}` (start, rep_start, rep_done, dataset, folder, note, params,
interrupted, paused, error, done).
"""
import os

# A script may end its scan with plt.show() "so the plot stays open"; on an interactive
# backend (qtagg) that waits for a person to close a window, so an unattended queue sat on
# 'running rep 1/1' for the rest of the night (blind review #026). Set before anything can
# import matplotlib, and inherited by whatever the script starts.
os.environ['MPLBACKEND'] = 'Agg'

import _thread                    # noqa: E402
import argparse                   # noqa: E402
import ast                        # noqa: E402
import importlib.util             # noqa: E402
import inspect                    # noqa: E402
import json                       # noqa: E402
import re                         # noqa: E402
import runpy                      # noqa: E402
import signal                     # noqa: E402
import subprocess                 # noqa: E402
import sys                        # noqa: E402
import threading                  # noqa: E402
import time                       # noqa: E402
import traceback                  # noqa: E402
if os.name == 'nt':
    import ctypes                 # noqa: E402,F401 - now, before the script's folder joins sys.path

# The engine reads this pipe as UTF-8, but a piped child on Windows writes in the ANSI code
# page, so an em-dash in a runner's own print arrived as two replacement characters -
# including the "[Ctrl-C] Interrupted - shutting down instruments safely" line, which is the one
# you most want to read. Agreeing with the parent also removes the chance that a character the
# code page cannot encode kills a measurement mid-run.
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, 'reconfigure'):
        _s.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the panel folder
import paths as _P          # noqa: E402  - for the root, wherever the panel sits
ROOT = _P.ROOT


# ------------------------------------------------------------------ the pipe to the panel

PIPE = {'broken': False, 'bol': True}      # bol: the last character written ended a line
PIPE_BROKE = threading.Event()             # wakes the watchdog the moment a write fails
STREAMS = {'out': None, 'err': None}
SCAN = {'armed': False, 'buf': '', 'said': []}   # where the script says it saved, this rep


def _pipe_broke():
    PIPE['broken'] = True
    PIPE_BROKE.set()


# One write at a time through the panel's streams: the raw write, the bol flag and the scan of
# 'Data saved to' lines. watch_folders() emits from its own thread during a rep, and without
# it an event could land between a script's text and the bol update - glued onto a 'Data saved
# to' line (a said path that is not on disk), or a buffered line lost (#100 re-check).
# Reentrant: _write_line holds it around out.write. A writer stuck in a blocking write never
# holds up another for more than STREAM_WAIT_S - the watchdog's forced path must go on.
_STREAM_LOCK = threading.RLock()
STREAM_WAIT_S = 5.0


class _Streamed(object):
    def __enter__(self):
        self.got = _STREAM_LOCK.acquire(timeout=STREAM_WAIT_S)
        return self

    def __exit__(self, *exc):
        if self.got:
            _STREAM_LOCK.release()
        return False


class _PanelStream(object):
    """sys.stdout / sys.stderr as the scripts see them.

    When the panel dies, the next print of a script that prints every point raised OSError
    inside the measurement, and the runner's own traceback.print_exc() then raised again on
    the same dead pipe - the process was gone in 0.15 s, before the watchdog's 2 s poll, and
    the safe stop never ran (blind review #025). A failed write is now swallowed, everything
    after it goes nowhere, and the watchdog is woken at once.

    It also remembers whether the last thing written ended a line, so an event is never glued
    onto a script's unfinished `print(..., end='')` line, where the engine cannot see it (#028),
    and it reads the lines where a script says where it saved its data (#029, #118).
    """

    def __init__(self, raw, scan):
        self._raw = raw
        self._scan = scan

    def write(self, s):
        if PIPE['broken']:
            return len(s)
        with _Streamed():
            try:
                n = self._raw.write(s)
            except OSError:
                _pipe_broke()
                return len(s)
            except ValueError:                 # closed by the script itself, not the panel
                return len(s)
            if s:
                PIPE['bol'] = s[-1] in '\r\n'
                if self._scan and SCAN['armed']:
                    _scan_text(s)
            return n

    def writelines(self, lines):
        for s in lines:
            self.write(s)

    def flush(self):
        if PIPE['broken']:
            return
        try:
            self._raw.flush()
        except OSError:
            _pipe_broke()
        except ValueError:
            pass

    def __getattr__(self, name):               # encoding, fileno, isatty, buffer, ...
        return getattr(self._raw, name)


def install_streams():
    if sys.stdout is not None and not isinstance(sys.stdout, _PanelStream):
        sys.stdout = STREAMS['out'] = _PanelStream(sys.stdout, True)
    if sys.stderr is not None and not isinstance(sys.stderr, _PanelStream):
        sys.stderr = STREAMS['err'] = _PanelStream(sys.stderr, False)


def _write_line(text, err=False):
    out = (STREAMS['err'] if err else STREAMS['out']) or (sys.stderr if err else sys.stdout)
    try:
        with _Streamed():                      # the bol look and the write in one piece
            out.write(('' if PIPE['bol'] else '\n') + text + '\n')
            out.flush()
    except (OSError, ValueError, AttributeError):
        pass                                   # the panel is gone; keep shutting down


def emit(event, **kw):
    kw['event'] = event
    _write_line('##PANEL ' + json.dumps(kw, default=str))


def _say(text):
    """Printing after the panel has gone raises an OSError, which must not become the reason
    a shutdown stops half way."""
    _write_line(text)


def _print_exc():
    try:
        traceback.print_exc(file=STREAMS['err'] or sys.stderr)
    except Exception:                                                 # noqa: BLE001
        pass


# ------------------------------------------------------------------ where the script saved

# What a script prints once a dataset is on disk (scripts/labmaster_dataset.py, or a qcodes-style loop):
#   Data saved to data/2026-03-12/#001_ExampleSweep_09-30-00/
_SAID = (re.compile(r'Data saved to\s+(?P<p>.+?)[\\/]?\s*$'),)


def _scan_text(s):
    buf = SCAN['buf'] + s
    lines = re.split(r'[\r\n]', buf)
    SCAN['buf'] = lines.pop()[-4000:]
    for ln in lines:
        if 'saved' in ln:
            _note_said(ln)


def _note_said(line):
    for rx in _SAID:
        m = rx.search(line)
        if m:
            p = m.group('p').strip().strip('"\'')
            if p and p not in SCAN['said']:
                SCAN['said'].append(p)
            return


def _arm(on):
    if on:
        SCAN.update(armed=True, buf='', said=[])
    else:
        if SCAN['armed'] and SCAN['buf']:
            _scan_text('\n')                   # a last line printed without a newline
        SCAN['armed'] = False


def _resolve_said(p, start_cwd):
    """An announced location as a path on disk, or None. qcodes prints it relative to where
    its DiskIO was created, which is the runner's working directory."""
    cands = [p] if os.path.isabs(p) else [os.path.join(b, p) for b in
                                          (os.getcwd(), start_cwd, ROOT) if b]
    for c in cands:
        if os.path.exists(c):
            return _norm(c)
    return None


# ------------------------------------------------------------------ stopping and the watchdog

STOP = {'requested': False}                # a stop (Ctrl-Break) or the watchdog asked for it
HOLD = {'on': False}                       # the safe stop is running: an interrupt only notes


def install_break_handler():
    """The engine sends Ctrl-Break; turn it into KeyboardInterrupt so `finally:` blocks
    (outputs off, instrument .close()) still run."""
    def _handler(signum, frame):
        ORPHAN['took_interrupt'] = True        # the watchdog reads this: cleanup is under way
        STOP['requested'] = True
        if HOLD['on']:
            # the orphan's safe stop is running, or the runner is already on its way out:
            # a KeyboardInterrupt now would only kill the safe stop half way
            return
        raise KeyboardInterrupt('stop requested by the control panel')
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, _handler)
    signal.signal(signal.SIGINT, _handler)


def watch_stop_file(path):
    """The panel's Stop, for a runner in a console of its own. The engine starts it with
    CREATE_NO_WINDOW, so closing the panel's window no longer kills it at the same instant
    (blind review #024) - and a Ctrl-Break cannot reach across consoles. The engine writes
    this file instead, and it becomes exactly what a Ctrl-Break would have been: SIGBREAK in
    the main thread, through the handler above (a script that ignores SIGBREAK ignores this
    too). Only the first appearance counts, as the engine sends one Ctrl-Break."""
    def _loop():
        while True:
            try:
                if os.path.exists(path):
                    break
            except Exception:                                         # noqa: BLE001
                pass
            time.sleep(0.2)
        sig = getattr(signal, 'SIGBREAK', signal.SIGINT)
        try:
            _thread.interrupt_main(sig)
        except TypeError:                          # Python before 3.10 takes no signal number
            _thread.interrupt_main()
    t = threading.Thread(target=_loop, name='stop-file', daemon=True)
    t.start()
    return t


def _alive(pid):
    """Is that process there? One this process may not open (ERROR_ACCESS_DENIED: an elevated
    runner, another user's) is there - never taken as ended (#098 re-check)."""
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False
    import _winapi
    SYNCHRONIZE, WAIT_TIMEOUT = 0x00100000, 0x102
    try:
        h = _winapi.OpenProcess(SYNCHRONIZE, False, int(pid))
    except OSError as e:
        return getattr(e, 'winerror', None) == 5
    try:
        return _winapi.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
    finally:
        _winapi.CloseHandle(h)


ORPHAN = {'hit': False, 'safe_stop': '', 'python': '', 'cwd': '', 'ran': False,
          'took_interrupt': False, 'pid': 0, 'why': '', 'dir': '', 'target': '',
          'marker': '', 'log': ''}
REC = {}                                   # the orphan record, as written to ORPHAN['marker']
_SS_LOCK = threading.Lock()
_REC_LOCK = threading.RLock()
# How long the watchdog gives the measurement after the panel has gone. A script that never
# takes the interrupt (wedged in a driver read) gets INTERRUPT_GRACE_S; one that took it and is
# running its own `finally:` (ramping a source down, closing drivers) gets CLEANUP_GRACE_S in
# all, because cutting that off half way is worse than waiting (agent B, finding B3). The
# environment overrides exist for the test suite.
INTERRUPT_GRACE_S = float(os.environ.get('LABMASTER_RUNNER_INTERRUPT_GRACE_S') or 30.0)
CLEANUP_GRACE_S = float(os.environ.get('LABMASTER_RUNNER_CLEANUP_GRACE_S') or 180.0)
SAFE_STOP_TIMEOUT_S = 120.0
RETRY_WAIT_S = 60.0


PARENT = {'handle': None}


def _hold_parent(pid):
    """A handle on the panel process, opened once: polling by pid alone is fooled when the
    dead panel's pid is given to a new process, and the watchdog then waits for ever."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x00100000, False, int(pid))   # SYNCHRONIZE
        PARENT['handle'] = h or 0                  # 0: already gone when we looked
    except Exception:                                                 # noqa: BLE001
        PARENT['handle'] = None


def _parent_alive(pid):
    h = PARENT['handle']
    if h is None:
        return _alive(pid)
    if not h:
        return False
    try:
        import ctypes
        return ctypes.windll.kernel32.WaitForSingleObject(h, 0) == 0x102   # WAIT_TIMEOUT
    except Exception:                                                 # noqa: BLE001
        return _alive(pid)


def _orphaned():
    """Is this runner on its own? Only asked when a watchdog was set up (a real run started
    by the panel); otherwise whoever started us owns the safe stop."""
    if not ORPHAN['pid']:
        return False
    return bool(ORPHAN['hit'] or PIPE['broken'] or not _parent_alive(ORPHAN['pid']))


def _why():
    if ORPHAN['why']:
        return ORPHAN['why']
    return ('the pipe to the control panel broke' if PIPE['broken']
            else 'the control panel (pid %s) is gone' % ORPHAN['pid'])


def _record(**kw):
    """The record the next panel start reads (blind review #102): after the panel died, this
    runner is the only one who knows whether the safe stop ran, and it used to tell nobody -
    the output went to DEVNULL, the exit code was dropped, a missing script returned
    silently. Written as soon as the panel is seen to be gone ('pending'), so a runner killed
    during its grace still leaves a trace. True when it reached the disk."""
    with _REC_LOCK:                            # the watchdog and the main thread both write it
        return _record_locked(kw)


def _record_locked(kw):
    try:
        if not ORPHAN['marker']:
            d = ORPHAN['dir'] or os.path.join(_P.STATE_DIR, 'orphan_safe_stop')
            os.makedirs(d, exist_ok=True)
            stem = 'runner_%d_%s' % (os.getpid(), time.strftime('%Y%m%d_%H%M%S'))
            ORPHAN['marker'] = os.path.join(d, stem + '.json')
            ORPHAN['log'] = os.path.join(d, stem + '.log')
            REC.update(kind='orphan_safe_stop', runner_pid=os.getpid(),
                       panel_pid=ORPHAN['pid'], target=ORPHAN['target'],
                       safe_stop=ORPHAN['safe_stop'], python=ORPHAN['python'],
                       cwd=ORPHAN['cwd'], why=_why(), noticed=time.time(),
                       log=ORPHAN['log'].replace('\\', '/'), status='pending', ok=None,
                       attempts=[])
        REC.update(kw)
        REC['updated'] = time.time()
        # the same moment on the monotonic clock (on Windows it counts from the boot for every
        # process): a panel that reads this after a step of the wall clock still knows how long
        # ago it was written, and holds its safe stop for as long as its limits allow (#098)
        REC['updated_mono'] = time.monotonic()
        _P.write_json(ORPHAN['marker'], REC)
        return True
    except Exception:                                                 # noqa: BLE001
        return False                           # a record must never stop the safe stop


RECORD_RETRY_S = 4.0


def _record_helper_pid(pid):
    """retry_pid on the record, tried again for a few seconds when the write does not land (a
    scanner or a backup holding the file makes os.replace fail): it is how a panel restarted
    meanwhile follows the safe stop to the helper, and a panel that read 'pending' with the
    runner gone used to let go while the helper ran it (#098 re-check). The helper writes its
    own pid too, as its first act."""
    t0 = time.time()
    while not _record(retry_pid=pid):
        if time.time() - t0 >= RECORD_RETRY_S:
            _log_line('the record could not be written (retry_pid %d) - the helper writes its '
                      'own pid when it starts' % pid)
            return False
        time.sleep(0.25)
    return True


def _log_line(text):
    try:
        with open(ORPHAN['log'], 'a', encoding='utf-8') as f:
            f.write('%s  %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), text))
    except (OSError, TypeError, ValueError):
        pass


def _run_safe_stop_once(where):
    """One run of the safe-stop script, its output into the record's log. Its own sinks, NOT
    the inherited ones: by now our stdout is a pipe whose reader (the panel) is dead, and
    handing that handle to a new process makes the spawn itself fail - which would silently
    skip the safe stop, the one thing that must not be skipped. Its own process group, so a
    console Ctrl-C or Ctrl-Break meant for us cannot cut it short."""
    att = {'where': where, 'started': time.time(), 'rc': None, 'ok': False, 'error': ''}
    _log_line('safe stop (%s): %s %s' % (where, ORPHAN['python'], ORPHAN['safe_stop']))
    log = None
    try:
        try:
            log = open(ORPHAN['log'], 'a', encoding='utf-8')
        except (OSError, TypeError, ValueError):
            log = None
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
        r = subprocess.run([ORPHAN['python'], '-u', ORPHAN['safe_stop']],
                           cwd=ORPHAN['cwd'] or None, timeout=SAFE_STOP_TIMEOUT_S,
                           stdin=subprocess.DEVNULL,
                           stdout=log if log is not None else subprocess.DEVNULL,
                           stderr=subprocess.STDOUT, creationflags=flags)
        att['rc'] = r.returncode
        att['ok'] = r.returncode == 0
        if not att['ok']:
            att['error'] = 'exit code %d' % r.returncode
    except subprocess.TimeoutExpired:
        att['error'] = 'timed out after %.0f s' % SAFE_STOP_TIMEOUT_S
    except (OSError, subprocess.SubprocessError) as e:
        att['error'] = 'could not be started: %s' % e
    finally:
        if log is not None:
            try:
                log.close()
            except OSError:
                pass
    att['finished'] = time.time()
    _log_line('safe stop (%s): %s' % (where, 'OK' if att['ok'] else 'FAILED - ' + att['error']))
    return att


def _spawn_retry():
    """Run the safe stop once more AFTER this process has exited. The usual reason a safe
    stop fails here is that an instrument is still open in this very process (a wedged driver
    read, or a script without a `finally:` interrupted mid-sequence) - the driver then says
    'already open by another process' - and only our exit releases it. The forced path uses
    it for its only safe stop (#098)."""
    if not ORPHAN['marker']:
        return 0
    cmd = [ORPHAN['python'], '-u', os.path.abspath(__file__),
           '--orphan-retry', ORPHAN['marker'], '--wait-pid', str(os.getpid()),
           # the record may not have reached the disk (a file held open): what the helper needs
           # to run the safe stop all the same (#098 re-check)
           '--safe-stop', ORPHAN['safe_stop'] or '', '--cwd', ORPHAN['cwd'] or '',
           '--log', ORPHAN['log'] or '']
    base = 0
    if os.name == 'nt':
        base = (subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000))
    for extra in (getattr(subprocess, 'CREATE_BREAKAWAY_FROM_JOB', 0x01000000), 0):
        try:
            p = subprocess.Popen(cmd, cwd=ORPHAN['cwd'] or None, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=base | extra if os.name == 'nt' else 0,
                                 close_fds=True)
            return p.pid
        except (OSError, ValueError):
            if os.name != 'nt':
                break
    return 0


def run_safe_stop_here():
    """The panel normally runs the safe stop after a measurement stops. When the panel is the
    thing that disappeared, this process has to do it: a killed measurement may leave its
    outputs on, so 'nobody is left to switch them off' is not an acceptable resting state.
    With no stop script configured (paths.safe_stop_script empty) there is nothing to run:
    that is recorded as done and ok, never a failure.

    One run per runner, whichever thread gets here first; the other waits for it, so the
    watchdog can never os._exit() in the middle of it. Returns True/False, or None when this
    runner has no watchdog (simulation, or started by hand)."""
    with _SS_LOCK:
        if ORPHAN['ran']:
            return REC.get('ok')
        if not ORPHAN['pid'] and not ORPHAN['safe_stop']:
            return None
        ORPHAN['ran'] = True
        HOLD['on'] = True
        _record(status='running')
        if not ORPHAN['safe_stop']:
            _say('[panel] no safe-stop script is configured - nothing to run')
            _record(status='done', ok=True, skipped='no stop script configured')
            return True
        if not os.path.isfile(ORPHAN['safe_stop']):
            _say('[panel] the safe-stop script is missing: %s' % ORPHAN['safe_stop'])
            _record(status='done', ok=False,
                    error='the safe-stop script is missing: %s' % ORPHAN['safe_stop'])
            return False
        _say('[panel] running the safe stop from the runner: %s'
             % os.path.basename(ORPHAN['safe_stop']))
        att = _run_safe_stop_once('runner')
        REC.setdefault('attempts', []).append(att)
        if att['ok']:
            _record(status='done', ok=True)
            return True
        # on disk BEFORE the helper starts: it builds its record on this one
        _record(status='retry after exit', ok=False, error=att['error'])
        pid = _spawn_retry()
        if pid:
            _log_line('retrying once this runner has exited (helper pid %d)' % pid)
            _record_helper_pid(pid)
        else:
            _record(status='done', error=att['error'] + '; the retry could not be started')
        return False


def safe_stop_after_exit():
    """The forced path (#098): hand the safe stop to a helper that waits until this process
    has exited, instead of running it here while the measurement's main thread is still alive.

    Run from here, it raced the measurement: the script's long C call returned during the safe
    stop, the pending interrupt was only noted (HOLD), the script read its data and started an
    instrument on the next point after the safe stop had switched it off - and os._exit() then
    left it running, with a record saying the outputs were safe. Once this process is gone
    nothing here can drive an output again, and the instruments are free for the safe stop
    (the retry after a failed safe stop has always worked this way). True when the helper is
    on its way, or a safe stop of this runner has
    already run (the lock waits for one the main thread runs); False when the caller has to
    run it here after all."""
    with _SS_LOCK:
        if ORPHAN['ran']:
            return True
        if not ORPHAN['safe_stop'] or not os.path.isfile(ORPHAN['safe_stop']):
            return False                       # run_safe_stop_here() records why
        # on disk BEFORE the helper starts: it builds its record on this one
        _record(status='retry after exit', ok=None)
        pid = _spawn_retry()
        if not pid:
            _log_line('the helper for the safe stop after exit could not be started - '
                      'running the safe stop from here, with the measurement still alive')
            return False
        ORPHAN['ran'] = True
        _log_line('the safe stop runs once this runner has exited (helper pid %d)' % pid)
        _record_helper_pid(pid)
        return True


def orphan_retry_main(argv):
    """`run_block.py --orphan-retry <record> --wait-pid <runner>`: the second try of an
    orphan's safe stop, started by the runner just before it exits."""
    ap = argparse.ArgumentParser()
    ap.add_argument('--orphan-retry', required=True)
    ap.add_argument('--wait-pid', type=int, default=0)
    ap.add_argument('--safe-stop', default='')
    ap.add_argument('--cwd', default='')
    ap.add_argument('--log', default='')
    a = ap.parse_args(argv)
    rec = _P.read_json(a.orphan_retry, None)
    t0 = time.time()
    while not isinstance(rec, dict) and not a.safe_stop and time.time() - t0 < RECORD_RETRY_S:
        # held for a moment by a scanner or a backup, and nothing else names the safe stop:
        # read again rather than given up on
        time.sleep(0.25)
        rec = _P.read_json(a.orphan_retry, None)
    if not isinstance(rec, dict) and a.safe_stop:
        # the runner's record never reached the disk: its command line names the safe stop, and
        # this process writes the record itself - a safe stop is never skipped for want of one
        rec = {'kind': 'orphan_safe_stop', 'runner_pid': a.wait_pid, 'safe_stop': a.safe_stop,
               'python': sys.executable, 'cwd': a.cwd, 'log': a.log, 'noticed': time.time(),
               'why': 'the control panel is gone', 'status': 'retry after exit', 'ok': None,
               'attempts': []}
    if not isinstance(rec, dict) or not rec.get('safe_stop'):
        return 2
    ORPHAN.update(marker=a.orphan_retry, log=rec.get('log') or '', safe_stop=rec['safe_stop'],
                  python=rec.get('python') or sys.executable, cwd=rec.get('cwd') or '')
    # its first act: its own pid and status on the record - a second chance for the runner's
    # write of retry_pid, which a panel restarted meanwhile follows (#098 re-check)
    REC.update(rec)
    mine = _record(status='retry running', retry_pid=os.getpid())
    t0 = time.time()
    while a.wait_pid and _alive(a.wait_pid) and time.time() - t0 < RETRY_WAIT_S:
        if not mine:
            mine = _record(status='retry running', retry_pid=os.getpid())
        time.sleep(0.2)
    gone = not (a.wait_pid and _alive(a.wait_pid))
    # read again now that the runner is gone: its last word (retry_pid) came after our start
    again = _P.read_json(a.orphan_retry, None)
    REC.update(again if isinstance(again, dict) else rec)
    # ... and written over its 'retry after exit': it is this process that runs it now
    _record(status='retry running', retry_pid=os.getpid())
    _log_line('the runner (pid %s) %s' % (a.wait_pid, 'has exited' if gone else
                                          'is still there after %.0f s; trying anyway' % RETRY_WAIT_S))
    att = _run_safe_stop_once('after the runner exited')
    att['runner_gone'] = gone
    REC.setdefault('attempts', []).append(att)
    _record(status='done', ok=att['ok'], error='' if att['ok'] else att['error'])
    return 0 if att['ok'] else 1


def watch_parent(pid, safe_stop, python_exe, work_dir):
    """Stop cleanly if the panel disappears.

    The console window IS the panel: close it during a measurement and this process is left
    with nobody watching it. What happened then depended on luck - a runner that was printing
    died on a broken pipe, a runner in a long driver read carried on for another half hour -
    and in neither case did anything run the safe stop, so the outputs stayed as the script
    had left them. Instead: notice, interrupt the measurement so its own `finally:` (outputs
    off) runs, then run the safe stop.

    The main thread does that last step, because a daemon thread is killed the moment the
    main thread returns - a safe stop started here would simply vanish. This thread is only
    the backstop for a measurement too wedged to take the interrupt.

    A limit of the mechanism, not a bug (round 2, N04): _thread.interrupt_main() only marks
    an interrupt as pending, and the main thread takes it at its next Python instruction.
    On Windows it does NOT wake a call that blocks in C - one long time.sleep(1000), a driver
    read with a long timeout. Such a script takes the interrupt only when that call returns.
    If that is not within INTERRUPT_GRACE_S, this thread takes the FORCED path: os._exit(130),
    and the safe stop from a helper once this process is gone (#098) - so the script's own
    `finally:` does not run, and the safe stop has to be what leaves the instruments safe. (A
    loop of short sleeps or reads takes the interrupt at once.) Whatever happens on the forced
    path, the process exits; the safe stop is never run
    while the measurement could still wake up, unless the helper cannot be started at all.

    A stop that was already taken (the panel's Stop, then the panel died) is not interrupted
    a second time: that second KeyboardInterrupt landed in the script's own cleanup - a
    ramp-down, a driver close - and cut it short (round 2, N03). It gets CLEANUP_GRACE_S.
    """
    ORPHAN.update(safe_stop=safe_stop, python=python_exe, cwd=work_dir, pid=int(pid))
    _hold_parent(pid)

    def _loop():
        # a failed write to the panel's pipe is the first sign, well before the 2 s poll
        while not PIPE_BROKE.is_set() and _parent_alive(pid):
            PIPE_BROKE.wait(2.0)
        ORPHAN['why'] = _why()
        ORPHAN['hit'] = True
        _record()                              # 'pending': seen, nothing run yet
        _log_line('%s - stopping the measurement' % ORPHAN['why'])
        if ORPHAN['took_interrupt'] or HOLD['on']:
            # already stopping (a Stop from the panel before it died), or already past the
            # measurement: interrupting now would only cut its cleanup short (N03)
            _say('[panel] the control panel is gone - the measurement is already stopping; '
                 'letting its cleanup finish')
            _log_line('the measurement was already stopping - not interrupting its cleanup')
        else:
            _say('[panel] the control panel is gone - stopping this measurement safely')
            _thread.interrupt_main()
        main = threading.main_thread()
        t0 = time.time()
        while main.is_alive() and time.time() - t0 < INTERRUPT_GRACE_S:
            time.sleep(0.25)
        if main.is_alive() and (ORPHAN['took_interrupt'] or HOLD['on']):
            # it heard the interrupt and is cleaning up in its own finally: - let it
            _say('[panel] the measurement is still shutting down; giving it up to %.0f s'
                 % CLEANUP_GRACE_S)
            while main.is_alive() and time.time() - t0 < CLEANUP_GRACE_S:
                time.sleep(0.25)
        if main.is_alive():
            _forced()

    def _forced():
        """The measurement did not stop: exit, and the safe stop after the exit, from a helper
        (#098) - here only if the helper cannot be started. The exit even when that raises,
        or the main thread would measure on unwatched."""
        try:
            how = '' if ORPHAN['took_interrupt'] else ' (it never took the interrupt)'
            _say('[panel] the measurement did not stop%s - forcing the safe stop: this process '
                 'exits first, and the safe stop runs once it is gone' % how)
            _log_line('the measurement did not stop%s - forcing the safe stop after this '
                      'process exits' % how)
            _record(measurement='forced%s' % how)
            try:
                handed = safe_stop_after_exit()
            except Exception as e:                                    # noqa: BLE001
                _log_line('handing the safe stop to a helper failed: %s: %s'
                          % (type(e).__name__, e))
                handed = False                 # never no safe stop at all
            if not handed:
                run_safe_stop_here()           # waits for one the main thread already runs
        except BaseException as e:                                    # noqa: BLE001
            _log_line('the forced safe stop failed: %s: %s' % (type(e).__name__, e))
            _record(status='done', ok=False,
                    error='the forced safe stop failed: %s: %s' % (type(e).__name__, e))
        finally:
            os._exit(130)

    t = threading.Thread(target=_loop, name='parent-watchdog', daemon=True)
    t.start()
    return t


# ------------------------------------------------------------------ the script

class _NoInput(object):
    """stdin for a panel block. It used to be the panel's console, where nobody types, so a
    script that asked for input() waited there all night (blind review #110). Now input()
    gets end-of-file at once, and the runner says the script asked."""
    asked = False
    closed = False
    encoding = 'utf-8'
    errors = 'strict'

    def __init__(self, fd):
        self._fd = fd

    def _eof(self, empty=''):
        _NoInput.asked = True
        return empty

    def read(self, *a):
        return self._eof()

    def readline(self, *a):
        return self._eof()

    def readlines(self, *a):
        return self._eof([])

    def __iter__(self):
        self._eof()
        return iter(())

    def isatty(self):
        return False

    def readable(self):
        return True

    def fileno(self):
        if self._fd is None:
            raise OSError('stdin is detached from the panel console')
        return self._fd

    def close(self):
        pass                                   # exit()/quit() close stdin on the way out

    def flush(self):
        pass


def detach_stdin():
    fd = None
    try:
        nul = os.open(os.devnull, os.O_RDONLY)
        os.dup2(nul, 0)                        # the OS handle too, for what the script starts
        if nul != 0:
            os.close(nul)
        fd = 0
    except OSError:
        pass
    sys.stdin = _NoInput(fd)


def quiet_matplotlib():
    """matplotlib.use('Agg') as well as MPLBACKEND, if matplotlib is there at all (#026)."""
    try:
        import matplotlib
        matplotlib.use('Agg', force=True)
    except Exception:                                                 # noqa: BLE001
        pass


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def setup_import_path(script_dir):
    """What `python script.py` has and a runner did not (blind review #108, #109).

    * the root, so a package that lives in the root (`from mydrivers.x import *`) is found as
      it is when the script is run from there. Put before the interpreter's own folder, after
      the standard library;
    * the script's own folder, first, so a script that imports a file beside it
      (`from labmaster_dataset import Dataset`) runs under the panel as it does by hand.
    """
    if not any(p and _same(p, ROOT) for p in sys.path):
        at = len(sys.path)
        home = (os.path.dirname(os.path.abspath(sys.executable)), sys.prefix, sys.exec_prefix)
        for i, p in enumerate(sys.path):
            if p and (any(h and _same(p, h) for h in home) or 'site-packages' in p.lower()):
                at = i
                break
        sys.path.insert(at, ROOT)
    if script_dir and os.path.isdir(script_dir) and not (sys.path and sys.path[0]
                                                         and _same(sys.path[0], script_dir)):
        sys.path.insert(0, os.path.abspath(script_dir))


def has_main(target):
    """Does the script bind a top-level `main`? Decided from the SOURCE.

    Importing a script-style file IS running the measurement: the old runner imported
    every script to look for main(), found none, and then ran the file again once per rep
    - one measurement more than asked for, before the first rep_start, outside every pause
    boundary. So the question is answered by reading the file, not by executing it.

    Parsed from the bytes, as Python itself does, so a UTF-8 BOM or a coding line is honoured:
    a BOM used to fail every block of the script as 'import failed: U+FEFF' (#040).
    """
    with open(target, 'rb') as f:
        tree = ast.parse(f.read(), target)
    if not tree.body:
        # an empty file is not a measurement: a script half-saved by an editor, or one
        # truncated by a copy, used to "run" as a no-main script and report done
        raise ValueError('the script is empty: %s' % os.path.basename(target))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'main':
            return True
        if isinstance(node, ast.ImportFrom) and any(
                (a.asname or a.name) == 'main' for a in node.names):
            return True
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'main' for t in node.targets):
            return True
    return False


def load_main(target):
    """Import the script and return its main(), or None for script-style files."""
    name = os.path.splitext(os.path.basename(target))[0]
    spec = importlib.util.spec_from_file_location(name, target)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)                       # side effects only; the work is in main()
    return getattr(mod, 'main', None)


REP_ARGS = ('numreps', 'reps', 'num_reps', 'nreps', 'n_reps')


def filtered_kwargs(func, params, reps_per_call=1, dropped=None):
    """Only pass what main() actually accepts - a stale param must never raise TypeError.

    A main(**kwargs) accepts everything, so everything is passed: the old filter kept only the
    names in the signature, which for such a script was none of them, and the measurement ran
    on its hard-coded defaults while the page showed the values that had been dropped (third
    audit, bug 7). And the panel owns the rep loop, so whatever the script calls its own rep
    count - numreps, reps, ... - is set to one per call (bug 8): a main(reps=20) used to run
    its default twenty times inside every one of the panel's reps.

    `dropped`, when given, collects the names that were left out instead of printing them.
    """
    sig = inspect.signature(func).parameters
    takes_any = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.values())
    kwargs, out = {}, []
    for k, v in (params or {}).items():
        if k in sig or takes_any:
            kwargs[k] = v
        else:
            out.append(k)
    for name in REP_ARGS:
        if name in sig:
            kwargs[name] = reps_per_call
    if dropped is not None:
        dropped.extend(out)
    elif out:
        _say('[panel] ignored parameters main() does not take: %s' % ', '.join(out))
    return kwargs


def param_notes(func, kwargs, dropped, script):
    """Said once, as a warning the engine shows, not as one plain line per rep in the middle
    of the log (blind review #030): a block parameter that does not reach main() means the
    measurement runs on the script's own value while the page shows another one."""
    notes = []
    if dropped:
        notes.append('main() of %s does not take %s - NOT used: the script runs on its own '
                     'value%s. Remove %s from the block, or fix the name.'
                     % (script, ', '.join(dropped), 's' if len(dropped) > 1 else '',
                        'them' if len(dropped) > 1 else 'it'))
    try:
        sig = inspect.signature(func).parameters
    except (TypeError, ValueError):
        sig = {}
    for k, v in kwargs.items():
        d = sig[k].default if k in sig else inspect.Parameter.empty
        if (isinstance(v, str) and isinstance(d, (int, float)) and not isinstance(d, bool)):
            notes.append('%s = %r reaches main() as text, but its default (%r) is a number'
                         % (k, v, d))
        elif isinstance(v, str) and isinstance(d, bool):
            # the engine refuses this when it is saved, but not for a main() it cannot read
            # (an imported one): 'no' or 'false' as text is a TRUE value in `if flag:` (N48)
            notes.append('%s = %r reaches main() as text, but its default (%r) is True/False: '
                         'any non-empty text counts as True in the script - set it to true or '
                         'false' % (k, v, d))
    for n in notes:
        emit('note', message=n)


def _norm(path):
    return os.path.normpath(os.path.abspath(path)).replace('\\', '/')


def _key(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _created(st):
    return getattr(st, 'st_birthtime', None) or st.st_ctime       # creation time on Windows


_DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def dataset_dirs(data_root, first_day=''):
    """Every #NNN_* folder in every day folder under data_root: {path: (created, modified)}.

    Every day, not "today and yesterday by the clock". 'Yesterday' was now minus 86400 s, which
    on the night after the spring DST change is the day before yesterday, so a block credited
    a whole Sunday's datasets or lost its own (#119); and a rep longer than a day lost every
    folder filed two or more days back (#120). A listing per day folder costs milliseconds.
    `first_day` (YYYY-MM-DD) leaves out the days before it - only for the early look of
    watch_folders(), never for what a rep is credited with.
    """
    out = {}
    try:
        days = [e.path for e in os.scandir(data_root) if _DAY.match(e.name) and e.is_dir()
                and e.name >= first_day]
    except OSError:
        return out
    for day in days:
        try:
            with os.scandir(day) as it:
                for e in it:
                    if e.name.startswith('#') and e.is_dir():
                        try:
                            st = e.stat()
                            out[_norm(e.path)] = (_created(st), st.st_mtime)
                        except OSError:
                            pass
        except OSError:
            pass
    return out


def diff_datasets(data_root, seen, since=None, first_day=''):
    """(fresh, older): the folders that were not there before.

    `seen` is filled BEFORE the first rep with everything already on disk. The old rule -
    "the newest folder not yet reported" - had no such baseline, so a rep that wrote nothing
    was credited with whatever the day's newest folder happened to be: an earlier block's
    data, or yesterday's, into History (F07).

    A new PATH is not yet a new dataset: renaming a folder, restoring it from the Recycle Bin
    or copying it in makes one too, and such a folder keeps its old timestamps, so it sorted
    first and was credited to the rep (#118). A folder
    counts only if it was both created and last changed after `since` (the rep's start); the
    others come back as `older`, to be named, not credited. Oldest first by creation.
    """
    now = dataset_dirs(data_root, first_day)
    fresh, older = [], []
    for p, (created, mtime) in now.items():
        if _key(p) in seen:
            continue
        if since is not None and (created < since or mtime < since):
            older.append(p)
        else:
            fresh.append(p)
    fresh.sort(key=lambda p: (now[p][0], p))
    older.sort()
    return fresh, older


def _since(t0, m0):
    """The rep's start as file timestamps will see it - also when the wall clock was stepped
    back during the rep (a time sync), which would otherwise make the rep's own folder look
    older than the rep."""
    return min(t0, time.time() - (time.monotonic() - m0)) - 3.0


def rep_datasets(data_root, seen, since, start_cwd):
    """What this rep wrote: (credited, notes).

    What the script SAID it saved ('Data saved to <path>') comes first: that is the script's own
    output, it finds data saved outside the data folder too (#029), and it cannot pick up a
    folder somebody else put in the data folder meanwhile (#118). Only when the script said
    nothing are the new #NNN_* folders taken."""
    said, unsaid, notes = _rep_found(data_root, seen, since, start_cwd)
    if said:
        if unsaid:
            notes.append('%d folder(s) appeared during this rep that the script did not say it '
                         'wrote - not credited: %s' % (len(unsaid), ', '.join(unsaid)))
        return said, notes
    return unsaid, notes


def _rep_found(data_root, seen, since, start_cwd):
    """(said, unsaid, notes): what the script said it saved during this rep, the new folders
    it did not name, and what is worth saying about the rest. Everything found is added to
    `seen`, so a later rep does not find it again."""
    notes = []
    said, missing, stale = [], [], []
    for p in SCAN['said']:
        q = _resolve_said(p, start_cwd)
        if q is None:
            missing.append(p)
            continue
        try:
            st = os.stat(q)
            newest = st.st_mtime if os.path.isfile(q) else max(_created(st), st.st_mtime)
        except OSError:
            missing.append(p)
            continue
        if since is not None and newest < since:
            stale.append(q)                    # an old file named again: not this rep's data
        elif _key(q) not in [_key(x) for x in said]:
            said.append(q)
    fresh, older = diff_datasets(data_root, seen, since)
    for p in fresh + older + said:
        seen.add(_key(p))
    if missing:
        notes.append('the script said it saved %s, but that is not on disk'
                     % ', '.join(missing))
    if stale:
        notes.append('the script named %s as saved, but it was not written during this rep - '
                     'not credited' % ', '.join(stale))
    if older:
        notes.append('%d folder(s) appeared under %s during this rep but are older than it '
                     '(renamed, restored or copied in?) - not credited: %s'
                     % (len(older), data_root.replace('\\', '/'), ', '.join(older)))
    unsaid = [p for p in fresh if _key(p) not in [_key(x) for x in said]]
    return said, unsaid, notes


def report_partial(data_root, seen, rep, sim, since=None, start_cwd=''):
    """A rep that was interrupted or failed may already have created its dataset folder.
    Say so, marked partial: it is on disk and belongs in History, but it is not a finished
    rep and must not be counted as one (agent B, finding B4).

    What the script SAID it saved during the rep is a scan it finished - labmaster_dataset (and
    qcodes-style loops) print 'Data saved to' only once the dataset is complete. A script-style
    file that runs many scans in one rep (`reps = 100` at module level) stopped in its 4th scan
    used to have its three finished scans marked partial, and the 4th,
    the one really cut off, only named in a note (blind review #099). The announced ones are
    datasets now (rep_unfinished: their rep did not end), and the new folders nobody
    announced are the partial ones."""
    if sim:
        return
    try:
        _live(0)
        _arm(False)
        said, unsaid, notes = _rep_found(data_root, seen, since, start_cwd)
        for ds in said:
            emit('dataset', path=ds, rep=rep, rep_unfinished=True)
        for ds in unsaid:
            emit('dataset', path=ds, rep=rep, partial=True)
        for n in notes:
            emit('note', message='rep %d: %s' % (rep, n))
    except Exception:                                                 # noqa: BLE001
        pass                                   # reporting must never mask the interrupt


# The rep in progress, for watch_folders(): its number (0: none), its start, the folders told.
LIVE = {'rep': 0, 't0': 0.0, 'm0': 0.0, 'told': set(), 'saved': set()}
FOLDER_POLL_S = float(os.environ.get('LABMASTER_RUNNER_FOLDER_POLL_S') or 5.0)


def _live(rep, t0=0.0, m0=0.0):
    if rep:
        LIVE.update(t0=t0, m0=m0)
    LIVE['rep'] = rep


def watch_folders(data_root, seen, start_cwd=''):
    """Say which folder a rep has started as soon as it is on disk: `folder` {path, rep}.

    The folder of an interrupted rep is listed by report_partial(), from the runner's own
    interrupt or failure path. A runner killed after the engine's grace (a script wedged in
    one long C call), or from outside (Task Manager, a driver's native crash), never gets
    there, and the folder its rep had started was in neither the log nor History (blind
    review #100). `folder` is not a dataset: it only tells the engine where this rep is
    writing, for the case that nothing else is ever said about it. The day folders from two
    days before the rep on are looked at - about a millisecond every FOLDER_POLL_S.

    A told folder the script has since announced as saved ('Data saved to', printed once the
    dataset is complete) is told again with saved=True: a finished scan of a rep that runs many,
    which the engine then keeps as a dataset if the runner is killed - not as interrupted, the
    inversion #099 fixed for report_partial()."""
    def _saved_keys():
        out = set()
        for p in list(SCAN['said']):
            q = _resolve_said(p, start_cwd)
            if q is not None:
                out.add(_key(q))
        return out

    def _loop():
        while True:
            time.sleep(FOLDER_POLL_S)
            rep = LIVE['rep']
            if not rep or PIPE['broken']:
                continue
            try:
                since = _since(LIVE['t0'], LIVE['m0'])
                first = time.strftime('%Y-%m-%d', time.localtime(since - 2 * 86400))
                fresh, _older = diff_datasets(data_root, seen, since, first)
            except Exception:                                         # noqa: BLE001
                continue
            try:
                said = _saved_keys() if SCAN['armed'] else set()
            except Exception:                                         # noqa: BLE001
                said = set()
            for p in fresh:
                k = _key(p)
                if LIVE['rep'] != rep:
                    break
                if k not in LIVE['told']:
                    LIVE['told'].add(k)
                    emit('folder', path=p, rep=rep)
                if k in said and k not in LIVE['saved']:
                    LIVE['saved'].add(k)
                    emit('folder', path=p, rep=rep, saved=True)
    t = threading.Thread(target=_loop, name='folder-watch', daemon=True)
    t.start()
    return t


def simulate(target, rep, reps, seconds):
    """Fake a measurement: progress lines, the script itself never run, no dataset written."""
    name = os.path.basename(target)
    steps = 8
    print('[SIM] %s  rep %d/%d  (simulation: the script is not run)' % (name, rep, reps), flush=True)
    for k in range(steps):
        time.sleep(max(0.0, seconds) / steps)
        print('  [SIM] step %d/%d' % (k + 1, steps), flush=True)
    print('[SIM] rep %d/%d took %.1f s' % (rep, reps, seconds), flush=True)


def _exit_origin(tb):
    """(file, line, function) of the code that asked for a SystemExit: the innermost frame of
    its traceback, past exit()/quit() themselves (site's Quitter raises it in Python code).
    None when the traceback has no frame."""
    where = None
    while tb is not None:
        code = tb.tb_frame.f_code
        if os.path.basename(code.co_filename).lower() != '_sitebuiltins.py':
            where = (code.co_filename, tb.tb_lineno, code.co_name)
        tb = tb.tb_next
    return where


def _interrupted(data_root, seen, rep, sim, since, start_cwd):
    _say('[panel] interrupted during rep %d' % rep)
    report_partial(data_root, seen, rep, sim, since, start_cwd)
    emit('interrupted', rep=rep)
    if _orphaned():
        run_safe_stop_here()                   # the panel is gone; nothing else will
    return 130


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--target', required=True)
    ap.add_argument('--reps', type=int, default=1)
    ap.add_argument('--start-rep', type=int, default=1,
                    help='resume: the first rep to run, when earlier ones are already done')
    ap.add_argument('--params', default='{}')
    ap.add_argument('--pause-file', default='')
    ap.add_argument('--sim', action='store_true')
    ap.add_argument('--sim-seconds', type=float, default=6.0)
    ap.add_argument('--data-root', default='', help='where datasets are written (config paths.data_root)')
    ap.add_argument('--parent-pid', type=int, default=0, help='the panel; stop when it goes away')
    ap.add_argument('--safe-stop', default='',
                    help="run this if the panel goes away mid-measurement ('' = no stop script)")
    ap.add_argument('--script-dir', default='',
                    help='the folder of the ORIGINAL script when --target is a patched copy')
    ap.add_argument('--orphan-dir', default='',
                    help='where an orphaned runner records its safe stop (default state/orphan_safe_stop)')
    ap.add_argument('--stop-file', default='',
                    help='the panel asks for a stop by creating this file (a runner in a console '
                         'of its own cannot be sent a Ctrl-Break)')
    args = ap.parse_args()

    target = os.path.abspath(args.target)
    detach_stdin()
    if not os.path.isfile(target):
        emit('error', message='script not found: %s' % target)
        return 2
    try:
        params = json.loads(args.params or '{}')
    except ValueError as e:
        emit('error', message='bad --params: %s' % e)
        return 2
    if not isinstance(params, dict):
        emit('error', message='bad --params: not an object')
        return 2
    # null is "not set" (contract C3), never a value main() is handed as N=None
    params = {k: v for k, v in params.items() if v is not None}

    reps = max(1, int(args.reps))
    start = max(1, min(int(args.start_rep or 1), reps))
    start_cwd = os.getcwd()
    install_break_handler()
    if args.stop_file:
        watch_stop_file(os.path.abspath(args.stop_file))
    ORPHAN.update(dir=os.path.abspath(args.orphan_dir) if args.orphan_dir else '',
                  target=target.replace('\\', '/'))
    if args.parent_pid and not args.sim:
        watch_parent(args.parent_pid, os.path.abspath(args.safe_stop) if args.safe_stop else '',
                     sys.executable, start_cwd)
    emit('start', target=target, reps=reps, start_rep=start, sim=bool(args.sim))

    fn = None
    kwargs = {}
    scripted = False
    name = os.path.basename(target)
    if not args.sim:
        quiet_matplotlib()
        setup_import_path(os.path.abspath(args.script_dir) if args.script_dir
                          else os.path.dirname(target))
        try:
            if has_main(target):
                fn = load_main(target)
                if fn is None or not callable(fn):
                    emit('error', message='%s binds `main` but it is not a function; not '
                                          'guessing how many times it ran' % name)
                    return 1
            else:
                # script-style: it measures when the file runs. NOT imported here - that
                # would be a measurement (F06); it runs once per rep below.
                scripted = True
                _say('[panel] %s has no main(): each rep runs the whole file' % name)
        except ValueError as e:
            emit('error', message=str(e))
            return 2
        except SystemExit as e:
            emit('error', message='%s called sys.exit(%r) while it was being loaded, before '
                                  'main() could run' % (name, e.code))
            return 1
        except Exception as e:                                        # noqa: BLE001
            _print_exc()
            emit('error', message='import failed: %s: %s' % (type(e).__name__, e))
            return 1
        if fn is not None:
            dropped = []
            kwargs = filtered_kwargs(fn, params, reps_per_call=1, dropped=dropped)
            param_notes(fn, kwargs, dropped, name)
            emit('params', passed=kwargs, ignored=dropped)
        elif params:
            emit('note', message='%s has no main(), so the block parameter(s) %s cannot reach '
                                 'it - NOT used: the file runs with the values written in it. '
                                 'Change them with "Script parameters..." (overrides) instead.'
                                 % (name, ', '.join(sorted(params))))
            emit('params', passed={}, ignored=sorted(params))

    data_root = os.path.abspath(args.data_root) if args.data_root else os.path.join(ROOT, 'data')
    # what the script may read about the run it is part of (and pass on to what it starts)
    os.environ['LABMASTER_DATA_ROOT'] = data_root
    os.environ['LABMASTER_REPS'] = str(reps)
    os.environ['LABMASTER_SIM'] = '1' if args.sim else '0'
    # what is on disk before anything runs: only folders that appear after this are ours
    seen = set(_key(p) for p in dataset_dirs(data_root)) if not args.sim else set()
    if args.parent_pid and not args.sim:
        watch_folders(data_root, seen, start_cwd)  # for a runner killed mid-rep (#100)
    # the files that are the script's own code: a sys.exit() from anywhere else is a failure
    own = [target] + [f for f in [getattr(getattr(fn, '__code__', None), 'co_filename', None)] if f]
    t_all = time.time()
    asked_before = False
    for rep in range(start, reps + 1):
        t0, m0 = time.time(), time.monotonic()
        os.environ['LABMASTER_REP'] = str(rep)
        _say('[panel] rep %d/%d of %s' % (rep, reps, name))
        emit('rep_start', i=rep, n=reps)
        try:
            _arm(not args.sim)
            _live(rep, t0, m0)
            if args.sim:
                simulate(target, rep, reps, args.sim_seconds)
            elif fn is not None:
                fn(**dict(kwargs))
            else:
                # script-style file (no main): running the file is the measurement
                assert scripted
                runpy.run_path(target, run_name='__main__')
        except KeyboardInterrupt:
            return _interrupted(data_root, seen, rep, args.sim, _since(t0, m0), start_cwd)
        except SystemExit as e:
            # a script written to be run by hand may end with sys.exit()/exit(); that used to
            # end the whole runner with code 0 and no 'done', and a 3-rep block was recorded
            # done after one (blind review #027)
            if STOP['requested']:
                return _interrupted(data_root, seen, rep, args.sim, _since(t0, m0), start_cwd)
            # ... but only the script's own. A driver that calls a bare sys.exit() on an
            # instrument error, from inside the scan, skipped the class's cleanup (outputs off),
            # the cut-off folder was credited as a finished rep, and with exit code 0 the panel
            # ran no safe stop - the outputs stayed on (blind review #009). An exit asked for by
            # any other file is a failure of the rep.
            where = _exit_origin(e.__traceback__)
            mine = bool(where) and any(_same(where[0], f) for f in own)
            if (e.code is not None and e.code != 0) or not mine:
                if not mine:
                    _print_exc()
                report_partial(data_root, seen, rep, args.sim, _since(t0, m0), start_cwd)
                if mine:
                    msg = '%s exited with sys.exit(%r) during rep %d' % (name, e.code, rep)
                else:
                    msg = ('sys.exit(%r) was called in %s, not by %s itself, in the middle of rep '
                           '%d: the rep failed there. The rest of the scan, and any cleanup after '
                           'it that is not in a `finally:`, was skipped - the instruments\' outputs '
                           'may still be on until the safe stop'
                           % (e.code, '%s, %s() line %s' % (os.path.basename(where[0]), where[2],
                                                            where[1]) if where else 'unknown code',
                              name, rep))
                emit('error', message=msg, rep=rep)
                if _orphaned():
                    run_safe_stop_here()
                return 1
            _say('[panel] %s called sys.exit(%r): rep %d ends there' % (name, e.code, rep))
        except Exception as e:                                        # noqa: BLE001
            _print_exc()
            report_partial(data_root, seen, rep, args.sim, _since(t0, m0), start_cwd)
            emit('error', message='%s: %s' % (type(e).__name__, e), rep=rep)
            if _orphaned():
                run_safe_stop_here()           # the panel is gone; nothing else will
            return 1
        finally:
            _live(0)
            _arm(False)
        if STOP['requested']:
            # a script that catches KeyboardInterrupt, cleans up and returns normally: a stop
            # (or the panel's death) must still end the block, not start the next rep
            _say('[panel] %s caught the stop request and returned; rep %d counts as '
                 'interrupted' % (name, rep))
            return _interrupted(data_root, seen, rep, args.sim, _since(t0, m0), start_cwd)

        if not args.sim:
            got, notes = rep_datasets(data_root, seen, _since(t0, m0), start_cwd)
            for ds in got:
                emit('dataset', path=ds, rep=rep)
            for n in notes:
                emit('note', message='rep %d: %s' % (rep, n))
            if not got:
                # said out loud, never papered over with an older folder: the panel's
                # History must show that this rep left no data behind
                emit('note', message='rep %d of %s wrote no new dataset folder under %s '
                                     '(any day), and the script did not print where it saved one'
                                     % (rep, name, data_root.replace('\\', '/')))
            if _NoInput.asked and not asked_before:
                asked_before = True
                emit('note', message='%s asked for keyboard input (input()); a panel block has '
                                     'no keyboard, so it got end-of-file' % name)
        emit('rep_done', i=rep, n=reps, seconds=round(time.time() - t0, 1))

        if args.pause_file and os.path.exists(args.pause_file) and rep < reps:
            _say('[panel] pause requested - stopping cleanly after rep %d' % rep)
            emit('paused', done=rep, remaining=reps - rep)
            return 0

    _say('[panel] all %d rep(s) done in %.1f s' % (reps, time.time() - t_all))
    emit('done', reps=reps)
    return 0


def _main_rc():
    """main()'s exit code, whatever ended it. Each handler first sets HOLD, so an interrupt
    that arrives while it runs (the watchdog's, as the panel dies) only notes the stop: it
    used to escape from _print_exc() here and skip the safe stop below (round 2, N05)."""
    try:
        return main()
    except KeyboardInterrupt:
        HOLD['on'] = True
        return 130                             # it can land between reps, outside the loop
    except SystemExit as e:                    # argparse; the script's own is handled in main()
        HOLD['on'] = True
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException:                                             # noqa: BLE001
        HOLD['on'] = True
        _print_exc()
        return 1


def _run():
    if '--orphan-retry' in sys.argv:
        return orphan_retry_main(sys.argv[1:])
    install_streams()
    rc = 1
    try:
        rc = _main_rc()
    except BaseException:                                             # noqa: BLE001
        # an interrupt in the instant before a handler above set HOLD (a script that put
        # back Python's own Ctrl-C handler is not held by HOLD at all)
        rc = 130 if STOP['requested'] or ORPHAN['hit'] else 1
    # From here nothing is measuring, so a late interrupt can only cut the safe stop short.
    # Whatever ended main() - a script's error on the dead pipe, a normal finish - a runner
    # whose panel is gone runs the safe stop before it exits (blind review #025).
    HOLD['on'] = True
    for _ in range(3):
        try:
            if _orphaned():
                run_safe_stop_here()
            break
        except KeyboardInterrupt:
            continue
    return rc


if __name__ == '__main__':
    sys.exit(_run())
