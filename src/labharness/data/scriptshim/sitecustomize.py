"""Loads Kelvin's stand-in for the lab's instruments (kelvin_shim.py) into every Python that a script Kelvin runs starts,
so a process the script starts reaches the instruments through Kelvin too. Kelvin puts this folder on PYTHONPATH
only for that run. A Python that can't load the stand-in ends at once: it never reaches an instrument unwatched.
Then the sitecustomize this one hides, if the Python has one of its own, runs as it always would."""
import os
import sys

if os.environ.get("KELVIN_IPC"):
    try:
        import kelvin_shim
        kelvin_shim.install()
    except Exception as e:
        sys.stderr.write("Kelvin: the stand-in for the lab's instruments did not load (%s: %s), so this Python ends\n"
                         % (type(e).__name__, e))
        sys.stderr.flush()
        os._exit(3)


def _the_next_one():
    import importlib.machinery
    import importlib.util
    here = os.path.normcase(os.path.dirname(os.path.abspath(__file__)))
    rest = [p for p in sys.path if os.path.normcase(os.path.abspath(p or os.curdir)) != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


try:
    _the_next_one()
except Exception as e:   # as Python itself treats a failing sitecustomize: said, and the start goes on
    sys.stderr.write("Error in sitecustomize (the Python's own, after Kelvin's): %s: %s\n" % (type(e).__name__, e))
