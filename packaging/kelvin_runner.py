"""Entry point PyInstaller freezes into KelvinRunner.exe: the "Python" of a packaged Kelvin.

Why this program exists
-----------------------
The vendored labMaster Sequence engine (labharness/sequence/) starts its child processes with an
interpreter: `[paths.python_exe(cfg), '-u', <runner.py>, <args>]` (a block's measurement runner
runs/runners/run_block.py, the safe stop, the analysis runner run_myscript.py, and the pre-flight
import check with `-u -c <code>`). From a source checkout / venv that interpreter is the venv's
Python. In a frozen app `sys.executable` is Kelvin.exe, so the same command would start a second
Kelvin instead of running a script - and a Sequence block would "run" by opening a new app and
never finish the rep.

So a packaged Kelvin ships a second executable beside Kelvin.exe: KelvinRunner.exe. If this file
looks like a Python only because the engine passes `-u` and a script path, it carries the
interpreter and the vendored runners, and only runs what it is pointed at:

    KelvinRunner.exe -u <script.py> <the script's own arguments>
    KelvinRunner.exe -u -c <code>            # the engine's pre-flight import check
    KelvinRunner.exe --self-test             # check that this runner works, print a line, exit 0/1

How it behaves
--------------
* the .py file is executed as ``__main__`` (runpy), so ``sys.argv``, ``__file__``, exit codes,
  KeyboardInterrupt and the `##PANEL {...}` event lines are exactly what the runner scripts expect;
* the leading interpreter flags the engine may pass (`-u`, `-X utf8`, `-E`, ...) are accepted and
  skipped - a frozen interpreter has those settings fixed - and the abort switches are honoured
  (`-c` code, no script = usage);
* ``PYTHONPATH`` entries are put on ``sys.path`` and the first ``sitecustomize.py`` among them is run,
  as Python's ``site`` module would at start-up: that is how Kelvin's instrument stand-in
  (labharness/data/scriptshim, Sequence gate) and the analysis guard
  (labharness/sequence/runners/guard_site) reach a child process the app starts. A frozen
  interpreter never runs ``site``, so without this the shim would silently not load;
* the vendored sequence package travels in the same bundle (packaging/kelvin.spec puts its .py
  files in it): ``run_block.py``/``run_myscript.py`` import their sibling module ``paths`` as a
  top-level module, so the bundle's ``labharness/sequence`` folder goes on ``sys.path``;
* UTF-8: the engine reads the runner's pipe as UTF-8 (runners/run_block.py asks for it too) and a
  frozen console program would otherwise write the ANSI code page, so stdout/stderr are switched
  to UTF-8 with ``errors='replace'`` before anything else runs.

It is not a general Python: no ``-m``, no REPL, no ``pip``. A lab script that needs a package the
app does not carry (a maker's instrument library, scipy, matplotlib, ...) still needs a real lab
Python: set `paths.python_exe` in the Sequence settings (PACKAGING.md, "Real lab scripts"). The
vendored runners themselves need only the standard library.

Started directly by a packaged Kelvin only: `paths.python_exe()` in the vendored engine returns
this program when ``sys.frozen`` (see labharness/sequence/paths.py). From source it never runs.
"""
import os
import runpy
import sys

# where PyInstaller put the bundle: the exe's folder (onedir) or the temporary one (onefile)
BUNDLE = getattr(sys, "_MEIPASS", "") or os.path.dirname(os.path.abspath(__file__))
SEQUENCE_DIR = os.path.join(BUNDLE, "labharness", "sequence")

# Interpreter switches that stand alone, and those that take a value (`-X utf8`, `-W ignore`).
_FLAGS_NO_VALUE = frozenset(("-u", "-B", "-b", "-d", "-E", "-i", "-I", "-O", "-OO", "-q", "-R",
                             "-s", "-S", "-v", "-V", "-x"))
_FLAGS_TAKING_VALUE = frozenset(("-X", "-W"))

_PROGRAM = "KelvinRunner"


def _utf8_streams() -> None:
    """The engine reads this pipe as UTF-8; a console would give the ANSI code page. Never fatal."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:                                              # noqa: BLE001
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")   # and for the children a script starts


def _prepare_paths() -> None:
    """PYTHONPATH and the bundle's vendored sequence package, first - as a normal start would."""
    extra = [p for p in (os.environ.get("PYTHONPATH") or "").split(os.pathsep) if p]
    extra += [os.path.join(SEQUENCE_DIR, "runners"), SEQUENCE_DIR]
    for path in reversed(extra):        # reversed: the first entry ends up first in sys.path
        path = os.path.abspath(path)
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)


def _run_sitecustomize() -> None:
    """Run the first sitecustomize.py on PYTHONPATH, the way Python's site.py does at start-up.

    Kelvin injects its instrument stand-in (the Sequence gate's shim, labharness/data/scriptshim)
    and the analysis guard (sequence/runners/guard_site) into a child through PYTHONPATH: in a
    frozen app nothing would import them otherwise. Exactly one is run (the first, as Python's
    site.py does); a failing one says so and the start goes on, like Python itself. The important
    one for safety fails closed itself (its own sitecustomize exits the process)."""
    for entry in [p for p in (os.environ.get("PYTHONPATH") or "").split(os.pathsep) if p]:
        path = os.path.join(entry, "sitecustomize.py")
        if not os.path.isfile(path):
            continue
        try:
            runpy.run_path(path, run_name="sitecustomize")
        except SystemExit:
            raise
        except BaseException as e:                                     # noqa: BLE001
            sys.stderr.write("Error in sitecustomize (%s: %s)\n" % (type(e).__name__, e))
            sys.stderr.flush()
        return


def _exit_code(code) -> int:
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    try:
        return int(code)
    except (TypeError, ValueError):
        return 1


def _run_script(path, args) -> int:
    """Execute a .py file as __main__, returning what it exited with (runpy sets sys.argv[0])."""
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        sys.stderr.write("%s: no such script: %s\n" % (_PROGRAM, path))
        return 2
    sys.argv = [path] + list(args)
    try:
        runpy.run_path(path, run_name="__main__")
    except SystemExit as e:
        return _exit_code(e.code)
    except KeyboardInterrupt:
        return 130
    except BaseException:                                              # noqa: BLE001
        import traceback
        traceback.print_exc()
        return 1
    return 0


def _run_code(code, args) -> int:
    """`-c <code>`: what the engine's pre-flight import check is started with."""
    sys.argv = ["-c"] + list(args)
    namespace = {"__name__": "__main__", "__file__": "<string>", "__builtins__": __builtins__,
                 "__package__": None, "__spec__": None, "__doc__": None}
    try:
        exec(compile(code, "<string>", "exec"), namespace)             # noqa: S102 - the engine's own check
    except SystemExit as e:
        return _exit_code(e.code)
    except KeyboardInterrupt:
        return 130
    except BaseException:                                              # noqa: BLE001
        import traceback
        traceback.print_exc()
        return 1
    return 0


def _split_args(argv):
    """(mode, target, rest): mode is "script", "code" or "". Skips the interpreter switches."""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-c",):
            return "code", (argv[i + 1] if i + 1 < len(argv) else ""), argv[i + 2:]
        if arg == "--":
            rest = argv[i + 1:]
            return ("script" if rest and rest[0] != "-c" else ""), rest[0] if rest else "", rest[1:]
        if arg in _FLAGS_TAKING_VALUE:
            i += 2
            continue
        if arg.startswith("-X") or arg.startswith("-W"):
            i += 1
            continue
        if arg in _FLAGS_NO_VALUE:
            i += 1
            continue
        return "script", arg, argv[i + 1:]
    return "", "", []


def _self_test() -> int:
    """What a release build checks before trusting this executable: runpy, a script, `-c`, the
    bundle's runners. Prints one line; exit 0 when all of it works."""
    import tempfile

    problems = []
    for name in ("run_block.py", "run_myscript.py", "myscript_guard.py"):
        path = os.path.join(SEQUENCE_DIR, "runners", name)
        if not os.path.isfile(path):
            problems.append("missing runner " + path)
    if not os.path.isfile(os.path.join(SEQUENCE_DIR, "paths.py")):
        problems.append("missing " + os.path.join(SEQUENCE_DIR, "paths.py"))
    if not problems:
        probe = ("import os, sys\n"
                 "sys.stdout.write('runner-probe ok %s\\n' % os.path.basename(os.getcwd()))\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "probe.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(probe)
            try:
                if _run_script(path, []) != 0:
                    problems.append("running a script failed")
            except BaseException as e:                                 # noqa: BLE001
                problems.append("running a script failed: %s: %s" % (type(e).__name__, e))
        try:
            code = "import sys; sys.stdout.write('code-probe %d\\n' % sys.flags.utf8_mode)\n"
            if _run_code(code, []) != 0:
                problems.append("running -c failed")
        except BaseException as e:                                     # noqa: BLE001
            problems.append("running -c failed: %s: %s" % (type(e).__name__, e))
    ok = not problems
    print("%s self-test: %s (%s, %s)"
          % (_PROGRAM, "ok" if ok else "FAILED",
             os.path.dirname(os.path.abspath(sys.executable)), "frozen" if getattr(sys, "frozen", False) else "source"),
          flush=True)
    if problems:
        print("  " + "; ".join(problems), flush=True)
    return 0 if ok else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: %s -u <script.py> [args...] | -u -c <code> | --self-test" % _PROGRAM, flush=True)
        return 0 if argv else 2
    _utf8_streams()
    if argv[0] == "--self-test":
        return _self_test()
    _prepare_paths()
    _run_sitecustomize()
    mode, target, rest = _split_args(argv)
    if mode == "code":
        return _run_code(target, rest)
    if mode == "script":
        return _run_script(target, rest)
    sys.stderr.write("%s: no script to run (usage: -u <script.py> [args...])\n" % _PROGRAM)
    return 2


if __name__ == "__main__":
    sys.exit(main())
