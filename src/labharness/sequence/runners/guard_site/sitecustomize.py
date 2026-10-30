"""
sitecustomize for a child Python of an analysis script (0.1.7).

run_myscript.py --guard puts this folder first on PYTHONPATH and sets LABMASTER_GUARD=1, so every Python the script
starts - subprocess.run([sys.executable, ...]), multiprocessing's spawned workers - imports this at start-up
(site.py does that for the first sitecustomize on sys.path) and installs the same instrument guard as the runner
(../myscript_guard.py). Without LABMASTER_GUARD=1 it does nothing, so a stray PYTHONPATH can never change another
program.

It then runs the sitecustomize this one shadows, if the Python has one further down sys.path, so a site
configuration of the PC's Python (or the test suite's own guard) is not lost in a guarded child.

Not guarded: a child started with -I, -E or -S, or given an env without PYTHONPATH, and any program that is not
Python. myscript_guard.py says why it is a safety net and not a sandbox.
"""
import os
import sys


def _guard():
    if os.environ.get('LABMASTER_GUARD') != '1':
        return
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), 'myscript_guard.py')
    mod = sys.modules.get('myscript_guard')
    if mod is None:
        spec = importlib.util.spec_from_file_location('myscript_guard', path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules['myscript_guard'] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop('myscript_guard', None)
            raise
    mod.install()


def _chain():
    """Run the sitecustomize this one shadows (the first other one on sys.path), if any."""
    import importlib.machinery
    import importlib.util
    here = os.path.normcase(os.path.dirname(os.path.abspath(__file__)))
    rest = [p for p in sys.path if os.path.normcase(os.path.abspath(p or '.')) != here]
    spec = importlib.machinery.PathFinder.find_spec('sitecustomize', rest)
    if spec is None or spec.loader is None:
        return
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)


# Fail closed: a child that should be guarded and cannot be does not run at all (site.py would only print "Error in
# sitecustomize" and go on unguarded).
try:
    _guard()
except Exception as _e:                                            # noqa: BLE001
    sys.stderr.write('the instrument guard could not be installed in this child Python, so it '
                     'does not run: %s: %s\n' % (type(_e).__name__, _e))
    sys.stderr.flush()
    os._exit(3)
try:
    _chain()
except Exception as _e:                                            # noqa: BLE001
    sys.stderr.write('sitecustomize (chained): %s: %s\n' % (type(_e).__name__, _e))
