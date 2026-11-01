"""TEMPORARY diagnostics for the 0.3.36 exit crash (removed before merge): which threads outlive the test session,
which test started them, and what they do once h5py has unregistered its converters at exit."""
import atexit
import faulthandler
import os
import sys
import threading
import time
import traceback

_current = {"test": "(before any test)"}
_born = {}
_orig_start = threading.Thread.start


def _start(self, *args, **kwargs):
    _born[id(self)] = (_current["test"], "".join(traceback.format_stack(limit=14)[:-1]))
    return _orig_start(self, *args, **kwargs)


threading.Thread.start = _start


def pytest_runtest_logstart(nodeid, location):
    _current["test"] = nodeid


def _dump(title, stacks=True):
    out = sys.__stderr__
    frames = sys._current_frames()
    alive = [t for t in threading.enumerate() if t is not threading.main_thread()]
    out.write("\n[diag] %s: %d threads besides main\n" % (title, len(alive)))
    for t in alive:
        test, made = _born.get(id(t), ("?", ""))
        out.write("[diag] thread %r daemon=%s started in %s\n" % (t.name, t.daemon, test))
        if stacks:
            f = frames.get(t.ident)
            if f is not None:
                out.write("".join("    now: " + line for line in "".join(traceback.format_stack(f)[-7:])
                                  .splitlines(True)))
            out.write("".join("    made: " + line for line in made.splitlines(True)[-10:]))
    out.flush()


def pytest_unconfigure(config):
    _dump("at unconfigure")


def _late():
    # registered before h5py is imported, so it runs after h5py's own atexit (unregister_converters)
    faulthandler.enable(file=sys.__stderr__, all_threads=True)
    wait = float(os.environ.get("DIAG_LATE_S", "7"))
    sys.__stderr__.write("\n[diag] atexit: h5py's converters are gone; waiting %g s\n" % wait)
    sys.__stderr__.flush()
    time.sleep(wait)
    _dump("%g s after h5py unregistered its converters" % wait)


atexit.register(_late)
