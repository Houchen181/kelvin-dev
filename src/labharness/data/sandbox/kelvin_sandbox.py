"""Kelvin's sandbox for run_python, started by labharness/sandbox.py as a process of its own: it runs one piece of
Python on runs' data, in a scratch folder.

argv: this file, then the job (JSON): code (the code's file), scratch (its folder), runs ({run id: {folder, h5 or npz}}),
path (Kelvin's import path), result (where to say how it went).

Before the code runs, this loads what it may use (NumPy, h5py; SciPy, lmfit, Matplotlib if installed; Kelvin's own
plotting), makes h5py open files outside the scratch folder read-only, and adds an audit hook (PEP 578) that refuses
writing outside the scratch folder, reading outside it, the runs and Python's own files, the network, programs,
native libraries, the registry and instrument packages. sandbox.py says what that does and doesn't stop.

In the code: runs (run id -> its data), run (the first), load_run(id), plot(...) for a figure without Matplotlib,
save_figure(...) for a Matplotlib one, np (NumPy), and scratch (the folder)."""
import json
import linecache
import os
import sys
import traceback

DENIED_IMPORTS = ("pyvisa", "pyvisa_py", "serial", "usb", "libusb_package", "libusb1", "nidaqmx", "zhinst",
                  "MultiPyVu", "pypylon", "seabreeze", "pylablib", "TimeTagger", "Swabian", "pulsestreamer", "spinapi",
                  "qick", "Pyro4", "cffi", "_cffi_backend", "win32api", "win32com", "pythoncom", "pywintypes", "clr",
                  "pythonnet", "labharness.gate", "labharness.lab", "labharness.instrument", "labharness.adapters",
                  "labharness.drivers", "labharness.engine", "labharness.agent", "labharness.web",
                  "labharness.library", "labharness.envs", "labharness.screen", "labharness.desktop")
DENIED_EVENTS = ("socket.", "subprocess.", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork",
                 "os.forkpty", "os.startfile", "os.kill", "os.killpg", "os.chdir", "os.fchdir", "os.add_dll_directory",
                 "ctypes.", "_winapi.", "winreg.", "msvcrt.", "pty.", "urllib.", "http.client.", "ftplib.",
                 "smtplib.", "poplib.", "imaplib.", "nntplib.", "telnetlib.", "webbrowser.", "sys.addaudithook",
                 "sys.settrace", "sys.setprofile", "sys._current_frames", "sys._current_exceptions", "gc.get_objects",
                 "gc.get_referrers", "gc.get_referents", "signal.pthread_kill", "_posixsubprocess.")
# events that change the file system: the positions of their paths (all must be in the scratch folder)
WRITES = {"os.remove": (0,), "os.rmdir": (0,), "os.mkdir": (0,), "os.rename": (0, 1), "os.link": (0, 1),
          "os.symlink": (0, 1), "os.chmod": (0,), "os.chown": (0,), "os.utime": (0,), "os.truncate": (0,),
          "os.chflags": (0,), "os.lchflags": (0,), "os.lchmod": (0,), "os.lchown": (0,), "os.mkfifo": (0,),
          "os.mknod": (0,), "os.removexattr": (0,), "os.setxattr": (0,), "shutil.rmtree": (0,),
          "shutil.copyfile": (1,), "shutil.copymode": (1,), "shutil.copystat": (1,), "shutil.copytree": (1,),
          "shutil.move": (0, 1), "shutil.chown": (0,), "shutil.make_archive": (0,), "shutil.unpack_archive": (1,),
          "tempfile.mkstemp": (0,), "tempfile.mkdtemp": (0,), "sqlite3.connect": (0,)}
LISTS = {"os.listdir": (0,), "os.scandir": (0,), "os.listdrives": (), "os.listvolumes": (), "os.listmounts": (0,)}


def _preload():
    """What the code may use, loaded before the hook (later loads may need what the hook refuses)."""
    have = {}
    for name in ("numpy", "h5py", "scipy", "lmfit", "matplotlib"):
        try:
            mod = __import__(name)
            have[name] = str(getattr(mod, "__version__", "")) or "yes"
        except Exception:
            have[name] = None
    if have["scipy"]:
        for sub in ("optimize", "signal", "special", "interpolate", "ndimage", "integrate", "stats", "fft", "linalg"):
            try:
                __import__("scipy." + sub)
            except Exception:
                pass
    if have["matplotlib"]:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot  # noqa: F401
        except Exception:
            have["matplotlib"] = None
    try:
        import labharness.plotting  # noqa: F401  Kelvin's own figures, for plot()
        import labharness.fitting  # noqa: F401
    except Exception:
        pass
    for name in ("csv", "statistics", "decimal", "fractions", "itertools", "collections", "datetime", "re", "math",
                 "random", "textwrap", "string", "dataclasses", "typing", "pprint", "copy", "bisect", "heapq",
                 "functools", "operator", "io", "zipfile", "gzip", "base64", "struct"):
        try:
            __import__(name)
        except Exception:
            pass
    return have


def _canon_factory(scratch):
    """A path's full, case-folded form, with C functions bound now (code that patches os.path can't change it)."""
    if os.name == "nt":
        import nt
        full, fold, sep = nt._getfullpathname, str.lower, "\\"
    else:
        try:
            import posix
            norm = posix._path_normpath
        except AttributeError:
            norm = os.path.normpath

        def full(p, _norm=norm, _cwd=scratch, _sw=str.startswith):
            return _norm(p if _sw(p, "/") else _cwd + "/" + p)
        fold, sep = str, "/"
    return full, fold, sep


def _roots(paths, full, fold):
    out = []
    for p in paths:
        try:
            p = os.fspath(p)
            out.append(fold(full(p)).rstrip("\\/"))
            if os.name == "nt":   # the long form too (C:\Users\HOUCHE~1 is C:\Users\Houchen Li)
                import nt
                long = nt._getfinalpathname(p) if os.path.exists(p) else p
                out.append(fold(long[4:] if long.startswith("\\\\?\\") else long).rstrip("\\/"))
        except (OSError, TypeError, ValueError):
            continue
    return tuple(sorted(set(r for r in out if r)))


def _install_hook(scratch, reads):
    """The audit hook. Everything it uses is bound as a default argument now: it reads no global name, so code that
    rebinds modules' names (or this module's) changes nothing it decides."""
    full, fold, sep = _canon_factory(scratch)
    writable = _roots([scratch], full, fold)
    readable = _roots(list(reads) + [scratch], full, fold)
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC | getattr(os, "O_EXCL", 0)

    def hook(event, args, _sw=str.startswith, _fspath=os.fspath, _isinstance=isinstance, _str=str, _bytes=bytes,
             _int=int, _decode=bytes.decode, _full=full, _fold=fold, _sep=sep, _w=writable, _r=readable,
             _denied=DENIED_EVENTS, _imports=DENIED_IMPORTS, _writes=WRITES, _lists=LISTS, _flags=write_flags,
             _PE=PermissionError, _len=len, _range=range, _scratch=scratch):
        if event == "import":
            name = args[0] if args else ""
            if _isinstance(name, _str):
                for d in _imports:
                    if name == d or _sw(name, d + "."):
                        raise _PE("run_python can't import %s: it reaches instruments, programs or the network. "
                                  "Kelvin's own tools work the lab" % name)
            return
        if event == "open" or event in _writes or event in _lists:
            if event == "open":
                path = args[0] if args else None
                mode, flags = (args[1] if _len(args) > 1 else None), (args[2] if _len(args) > 2 else 0)
                writing = (_isinstance(mode, _str) and ("w" in mode or "a" in mode or "x" in mode or "+" in mode)) \
                    or (_isinstance(flags, _int) and flags & _flags)
                spots = ((path, writing),)
            elif event in _writes:
                spots = tuple((args[i], True) for i in _writes[event] if i < _len(args))
            else:
                if not _lists[event]:   # the computer's drives
                    raise _PE("run_python sees only its own folder and the runs it was given")
                spots = tuple((args[i], False) for i in _lists[event] if i < _len(args))
            for path, writing in spots:
                if path is None:
                    continue   # the working folder (os.listdir()), or no file at all
                if _isinstance(path, _int):
                    continue   # a file already opened, so already checked
                try:
                    p = _fspath(path)
                except TypeError:
                    continue
                if _isinstance(p, _bytes):
                    p = _decode(p, "utf-8", "surrogateescape")
                if event == "sqlite3.connect" and p == ":memory:":
                    continue
                try:
                    p = _fold(_full(p))
                except (OSError, ValueError, TypeError):
                    raise _PE("run_python: %r is not a path it may use" % (path,))
                roots = _w if writing else _r
                ok = False
                for r in roots:
                    if p == r or _sw(p, r + _sep):
                        ok = True
                        break
                if not ok:
                    if writing:
                        raise _PE("run_python writes only in its own folder (%s), not %s" % (_scratch, path))
                    raise _PE("run_python reads only its own folder, the runs it was given and Python's own files, "
                              "not %s" % (path,))
            return
        for d in _denied:
            if _sw(event, d):
                raise _PE("run_python can't do this here (%s): no network, programs, native libraries or system "
                          "settings, only computing on the runs' data" % event)
    sys.addaudithook(hook)


def _guard_h5py(scratch, runs):
    """h5py opens files with HDF5's own C code, which the audit hook doesn't see: a file outside the scratch folder
    opens read-only, and only a run's run.h5 or a file in the scratch folder opens at all."""
    try:
        import h5py.h5f as h5f
    except ImportError:
        return
    full, fold, sep = _canon_factory(scratch)
    mine = _roots([scratch], full, fold)
    theirs = tuple(sorted(set(fold(full(r["h5"])) for r in runs.values() if r.get("h5"))))
    open_, create, rdonly = h5f.open, h5f.create, h5f.ACC_RDONLY
    reading = (rdonly, rdonly | getattr(h5f, "ACC_SWMR_READ", 0))

    def inside(name):
        p = fold(full(os.fsdecode(name)))
        return any(p == r or p.startswith(r + sep) for r in mine), p in theirs

    def guarded_open(name, flags=rdonly, *a, **k):
        own, run = inside(name)
        if not own and (flags not in reading or not run):
            raise PermissionError("run_python opens a run's run.h5 read-only, and other files only in its own "
                                  "folder: not %s" % os.fsdecode(name))
        return open_(name, flags, *a, **k)

    def guarded_create(name, *a, **k):
        if not inside(name)[0]:
            raise PermissionError("run_python writes only in its own folder, not %s" % os.fsdecode(name))
        return create(name, *a, **k)
    h5f.open, h5f.create = guarded_open, guarded_create


class Run:
    """A run's data, read-only. data[key]: a column (1-D, one value per point) or a trace (2-D: points x samples,
    NaN-padded); x(key): a trace's x values (2-D); units[key]; meta: the run (plan, status, times...); fits: the fits
    kept with it; keys(): its columns. Read from run.h5 (h5py) or a snapshot (.npz) of a run still being measured."""

    def __init__(self, run_id, info):
        self.id, self.folder = run_id, info.get("folder")
        self.path = info.get("h5") or info.get("npz")
        self.data, self.units, self.meta, self.fits, self._x = {}, {}, {}, {}, {}
        self._info = info
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        import numpy as np
        if self._info.get("h5"):
            import h5py
            with h5py.File(self.path, "r") as f:
                self.meta = json.loads(f.attrs.get("meta_json", "{}"))
                for k, ds in f.get("points", {}).items():
                    v = ds[()]
                    self.data[k] = np.array([x.decode("utf-8") if isinstance(x, bytes) else str(x) for x in v]) \
                        if ds.dtype.kind in "OSU" else np.asarray(v, dtype=float)
                    self.units[k] = str(ds.attrs.get("unit", ""))
                traces = f.get("traces", {})
                for k, ds in traces.items():
                    base = k.rsplit(".", 1)[0]
                    if k.endswith((".x", ".axis")) and base in traces:
                        self._x[k] = ds[()]
                    elif not k.endswith(".length"):
                        self.data[k] = ds[()]
                        self.units[k] = str(ds.attrs.get("unit", ""))
                        self.units[k + " x"] = str(ds.attrs.get("x_unit", ""))
                for name, g in f.get("fits", {}).items():
                    item = {k: (json.loads(v) if isinstance(v, str) and v[:1] in "[{" else v) for k, v in g.attrs.items()}
                    item.update({k: g[k][()] for k in g})
                    self.fits[name] = item
        else:
            with np.load(self.path, allow_pickle=False) as z:
                for k in z.files:
                    if k == "meta.json":
                        self.meta = json.loads(str(z[k]))
                    elif k == "columns.json":
                        cols = json.loads(str(z[k]))
                        self.units.update({c: (v or {}).get("unit", "") for c, v in cols.items()})
                    elif k.endswith((".x", ".axis")) and k.rsplit(".", 1)[0] in z.files:
                        self._x[k] = z[k]
                    else:
                        self.data[k] = z[k]

    def keys(self):
        self._load()
        return list(self.data)

    def __getitem__(self, key):
        self._load()
        if key not in self.data:
            raise KeyError("%s has no column %r (it has: %s)" % (self.id, key, ", ".join(self.data)))
        return self.data[key]

    def __contains__(self, key):
        self._load()
        return key in self.data

    def x(self, key):
        """A trace's x values, one row per point (from its recorded x values, or its start and stop)."""
        import numpy as np
        self._load()
        y = self[key]
        if key + ".x" in self._x:
            return np.asarray(self._x[key + ".x"], dtype=float)
        if key + ".axis" in self._x:
            ax = np.asarray(self._x[key + ".axis"], dtype=float)
            n = y.shape[1]
            return ax[:, :1] + (ax[:, 1:2] - ax[:, :1]) * np.arange(n)[None, :] / max(1, n - 1)
        return np.tile(np.arange(y.shape[1], dtype=float), (y.shape[0], 1))

    def __repr__(self):
        self._load()
        cols = ", ".join("%s%s" % (k, " (%s)" % self.units[k] if self.units.get(k) else "") for k in self.data)
        return "<run %s: %s>" % (self.id, cols)


class Runs(dict):
    def __missing__(self, key):
        raise KeyError("run_python was given no run %r (it has: %s); name it in runs" % (key, ", ".join(self)))


def _namespace(job, scratch, have):
    runs = Runs((rid, Run(rid, info)) for rid, info in job["runs"].items())
    counter = {"n": 0}

    def plot(*curves, title="", x_label="", y_label="", x_unit="", y_unit="", x_log=False, y_log=False, name=""):
        """A figure without Matplotlib, drawn by Kelvin: plot((x, y), (x2, y2, "label"), title=..., y_log=True).
        Saved as a PNG in the scratch folder (and shown); returns its path."""
        from labharness import plotting
        drawn = []
        for c in curves:
            if len(c) not in (2, 3):
                raise ValueError("each curve is (x, y) or (x, y, label)")
            drawn.append(plotting.Curve(c[0], c[1], c[2] if len(c) == 3 else ""))
        for c, color in zip(drawn, plotting.colors_for(len(drawn))):
            c.color = color
        counter["n"] += 1
        fig = plotting.Figure([plotting.Panel(drawn, x_label or "x", x_unit, y_label or "y", y_unit, bool(x_log),
                                              bool(y_log))], title)
        path = os.path.join(scratch, (name or "plot-%d" % counter["n"]) + ".png")
        with open(path, "wb") as f:
            f.write(plotting.to_png(fig))
        return path

    def save_figure(fig=None, name=""):
        """A Matplotlib figure (the current one by default) saved as a PNG in the scratch folder (and shown)."""
        import matplotlib.pyplot as plt
        fig = fig or plt.gcf()
        counter["n"] += 1
        path = os.path.join(scratch, (name or "figure-%d" % counter["n"]) + ".png")
        fig.savefig(path, dpi=110)
        return path
    ns = {"__name__": "__main__", "__builtins__": __builtins__, "runs": runs,
          "run": next(iter(runs.values()), None), "load_run": lambda run_id: runs[run_id], "plot": plot,
          "save_figure": save_figure, "scratch": scratch}
    if have.get("numpy"):
        import numpy
        ns["np"] = numpy
    return ns


def _save_open_figures(scratch):
    """Matplotlib figures the code left open, as PNGs (shown with the answer)."""
    plt = sys.modules.get("matplotlib.pyplot")
    if plt is None:
        return []
    out = []
    for num in plt.get_fignums():
        path = os.path.join(scratch, "figure-%s.png" % num)
        if not os.path.exists(path):
            try:
                plt.figure(num).savefig(path, dpi=110)
                out.append(path)
            except Exception as e:
                print("figure %s could not be saved: %s" % (num, e), file=sys.stderr)
    return out


def main():
    sys.dont_write_bytecode = True   # nothing written next to Python's own files
    for stream in (sys.stdout, sys.stderr):   # what it prints reaches Kelvin as UTF-8, whatever the console's
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    with open(sys.argv[1], encoding="utf-8") as f:
        job = json.load(f)
    sys.path[:] = list(dict.fromkeys([p for p in job.get("path") or [] if p] + sys.path))
    scratch = os.path.abspath(job["scratch"])
    os.chdir(scratch)
    have = _preload()
    with open(job["code"], encoding="utf-8") as f:
        code = f.read()
    ns = _namespace(job, scratch, have)
    _guard_h5py(scratch, job["runs"])
    reads = [r["folder"] for r in job["runs"].values() if r.get("folder")]
    for p in sys.path + [sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix,
                         os.path.dirname(sys.executable), getattr(sys, "_MEIPASS", "")]:
        if p and os.path.isdir(p):
            reads.append(p)
    _install_hook(scratch, reads)
    if sys.stdin.readline().strip() != "go":   # Kelvin puts the process in its job object first
        os._exit(3)
    linecache.cache["<code>"] = (len(code), None, code.splitlines(True), "<code>")
    status = "ok"
    try:
        exec(compile(code, "<code>", "exec"), ns)
    except SystemExit as e:
        if e.code not in (None, 0):
            status = "error"
    except BaseException as e:   # the code's own frames, not this runner's
        traceback.print_exception(type(e), e, e.__traceback__.tb_next if e.__traceback__ else None)
        status = "error"
    _save_open_figures(scratch)
    sys.stdout.flush()
    sys.stderr.flush()
    with open(job["result"], "w", encoding="utf-8") as f:
        json.dump({"status": status, "available": have}, f)


if __name__ == "__main__":
    main()
