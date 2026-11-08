"""A lab's own scripts, run as they are, with every instrument command through Kelvin's safety gate.

Many labs run their instruments from their own Python scripts and notebooks. Kelvin runs one from the lab's scripts
folders (those a person named in Settings, Set up or the first start) in the lab's own Python, with its stand-in for
the lab's instruments loaded first (data/scriptshim/kelvin_shim.py): pyvisa, pyserial or a socket to a lab
instrument reaches Kelvin instead of the instrument, and Kelvin passes each command through the safety gate on its
own connection, as if the agent had sent it. So a script gets what a plan gets:

    before   the script is read, never run (scripts.py): the lab instruments it opens and the drive values it uses
             become one approval by a person, like a plan's; what Kelvin can't check is said there (an instrument not
             in the lab, the Prologix adapter lab instruments are behind, a maker's library that reaches instruments
             past the gate). A script can't be rehearsed on the twins, so a person approves every run, in auto mode
             too.
    while    measurements and settings run; drives are checked against the limits, ramped and watched; a change
             beyond what was approved asks the person, and one beyond the limits is refused. A refusal or a person's
             stop ends the script (its own finally: still runs, through the gate); one that does not end is ended.
    after    a stopped or failed run sends every drive the script touched back to safe (or holds it, where the lab
             says so). The readings the script took are the run's rows (run.h5), so they plot, analyse and report like
             a plan's, kept with the script as it ran, its output, every command and the files it wrote.

Kelvin's own process: only the gate talks to the instruments; the script's process never opens a lab instrument.
"""
from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import selectors
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from . import experiment, prologix
from . import scripts as lab_scripts
from .adapters import resource_name
from .eventlog import RunStore
from .gate import ApprovalRequest, Approver, SafetyGate, _dur
from .labconfig import Allowed
from .scpi import is_query, split_compound

SHIM_DIR = Path(__file__).parent / "data" / "scriptshim"
SHIM = SHIM_DIR / "kelvin_shim.py"
RUNNABLE = (".py", ".ipynb")
# makers' libraries that reach instruments through their own protocols or DLLs, past the gate: named in the run's
# approval, or, loaded on the way, let through with the run and said in the chat (Kelvin's built-in drivers use some of
# these too, on its own connections)
OPAQUE = ("zhinst", "nidaqmx", "PyDAQmx", "MultiPyVu", "pylablib", "seabreeze", "pypylon", "clr", "pythonnet",
          "thorlabs_apt", "pyximc", "instrumental", "qick", "picosdk", "pyAndorSDK2", "pyAndorSDK3", "andor3",
          "pymmcore", "spcm", "pyspcm", "TimeTagger", "pulsestreamer", "spinapi", "harvesters", "pyueye", "pyvcam",
          "pyftdi", "ftd2xx", "usb", "hid", "pymodbus", "minimalmodbus", "pylibftdi")
STOPS = ("refused", "declined", "tripped", "stopped", "timeout")   # gate results that end a script's run
GRACE_S = 10.0       # after a stop, how long the script may stay quiet before Kelvin ends it
MAX_GRACE_S = 120.0  # and how long its own clean-up may take at most
PROBE_S = 30.0
PROBE_KEEP_S = 600.0
OUTPUT_TAIL = 12     # lines of the script's output in the answer
REHEARSE_S = 60.0    # real seconds a rehearsal may take before Kelvin presses Stop on it
# what reaches past the simulated lab, so a rehearsal (which nobody approves) never runs code that uses it: native
# libraries, programs, COM and .NET, the registry, USB. The script's process refuses them too, as it rehearses
# (kelvin_shim._fence); this says so in words before anything runs
PAST_THE_LAB = ("ctypes", "_ctypes", "cffi", "_cffi_backend", "subprocess", "multiprocessing", "_winapi",
                "_posixsubprocess", "pty", "winreg", "win32com", "win32api", "win32process", "win32file", "win32event",
                "win32service", "pythoncom", "pywintypes", "comtypes", "clr", "pythonnet", "usb", "libusb_package",
                "libusb1", "hid", "ftd2xx", "pyftdi", "pylibftdi")
PROGRAMS = ("system", "popen", "startfile", "posix_spawn", "posix_spawnp", "fork", "forkpty", "kill", "killpg")
_PLACE = re.compile(r"\{([^{}]*)\}")


class ScriptError(ValueError):
    """Why a script can't run (said before anything runs)."""


# ------------------------------------------------------------------ which script, and as what text

def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def locate(path: str, folders: Sequence[Any], chosen: Sequence[Any] = (), missing: bool = False) -> Path:
    """The script to run: inside one of the lab's scripts folders (a person named them), or one of the files a person
    imported into their library (chosen: each one a person picked, in the Runs panel), never elsewhere. A path in the
    folders names one script (the same path in two of them: say which); a bare file name may be in a sub-folder, if it
    is there once, or name an imported file. missing: its path in full may name a file that is gone, in a folder that
    is still there (an earlier run's copy of it runs there, as it ran then)."""
    roots = [Path(f).expanduser().resolve() for f in folders if f]
    picked: Dict[str, Path] = {}
    for c in chosen:
        if c:
            p = Path(str(c)).expanduser().resolve()
            picked[os.path.normcase(str(p))] = p
    if not roots and not picked:
        raise ScriptError("this lab has no scripts folder: a person adds them in Settings (Instruments → Your lab's "
                          "scripts → Add folder…) or Set up, and Kelvin runs only the scripts in them, or those they "
                          "import in the Runs panel")
    where = ", ".join(str(r) for r in roots) or "none named"
    noun = "scripts folder" if len(roots) == 1 else "scripts folders"
    p = Path(str(path).strip().strip('"')).expanduser()
    tries = [p] if p.is_absolute() else [r / p for r in roots]
    found: List[Path] = []
    for t in tries:
        t = t.resolve()
        here = t.is_file() or (missing and p.is_absolute() and not t.exists() and t.parent.is_dir())
        if here and (any(_inside(t, r) for r in roots) or os.path.normcase(str(t)) in picked) and t not in found:
            found.append(t)
    if len(found) == 1:
        return _runnable(found[0])
    if found:
        raise ScriptError("there are several %s in the %s: %s; say which" % (p, noun, ", ".join(map(str, found))))
    if p.is_absolute():
        if p.exists():
            raise ScriptError("%s is outside the lab's %s (%s): Kelvin runs only the scripts a person put there, or "
                              "imported in the Runs panel" % (p, noun, where))
        raise ScriptError("%s does not exist" % p)
    if len(p.parts) == 1:
        # by its name: each file once, and only one inside the folders with its links followed (a link or a junction
        # out of them leads nowhere, as a path through one does)
        hits: List[Tuple[Optional[Path], Path]] = []
        seen: Set[str] = set()
        for r in roots:
            for h in _named(r, p.name):
                real = h.resolve()
                if os.path.normcase(str(real)) not in seen and any(_inside(real, x) for x in roots):
                    seen.add(os.path.normcase(str(real)))
                    hits.append((r, h))
        hits += [(None, c) for k, c in picked.items() if c.name.lower() == p.name.lower() and c.is_file()
                 and k not in seen]
        if len(hits) == 1:
            return _runnable(hits[0][1].resolve())
        if hits:   # each by its path in its folder, or in full when there are several folders
            raise ScriptError("there are several %s in the %s%s: %s; say which" % (
                p.name, noun, " and the scripts imported" if any(r is None for r, _ in hits) else "",
                ", ".join(str(h.relative_to(r)) if r is not None and len(roots) == 1 else str(h) for r, h in hits)))
    raise ScriptError("no script %s in the lab's %s (%s)" % (path, noun, where))


def _named(root: Path, name: str, most: int = 6) -> List[Path]:
    """Files called `name` under root (environments, caches and hidden folders left out)."""
    out: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in lab_scripts.SKIP_DIRS and not d.startswith(".")]
        if name in filenames:
            out.append(Path(dirpath) / name)
            if len(out) >= most:
                break
    return out


def _runnable(path: Path) -> Path:
    if path.suffix.lower() == ".m":
        raise ScriptError("%s is a MATLAB script: Kelvin runs Python scripts and notebooks. survey_scripts reads it, "
                          "and its measurement can become a recipe" % path.name)
    if path.suffix.lower() == ".vi":
        raise ScriptError("%s is a LabVIEW VI: Kelvin doesn't run it, it translates it into one of the lab's "
                          "experiments (Import a script or VI… in Runs → Schedule, or translate_vi in a chat)" % path.name)
    if path.suffix.lower() not in RUNNABLE:
        raise ScriptError("%s is not a Python script or notebook (.py, .ipynb)" % path.name)
    return path


def beside(script: Path, most: int = 200) -> Dict[str, str]:
    """The Python files beside a script, which it may import (its folder comes first on sys.path): each of its
    folder's own .py, .pyw, .pyc and .pyd files, with a short sha256 of its bytes (its size and time, over 4 MB), so
    a card can say which of them changed since the script's last run."""
    out: Dict[str, str] = {}
    try:
        found = sorted(os.scandir(script.parent), key=lambda e: e.name.lower())
    except OSError:
        return out
    for e in found:
        if len(out) >= most:
            break
        if e.name.lower() == script.name.lower() or Path(e.name).suffix.lower() not in (".py", ".pyw", ".pyc", ".pyd"):
            continue
        try:
            if not e.is_file():
                continue
            size = e.stat().st_size
            out[e.name] = ("%d bytes, %d" % (size, e.stat().st_mtime_ns) if size > 4 * 2 ** 20 else
                           hashlib.sha256(Path(e.path).read_bytes()).hexdigest()[:16])
        except OSError:
            continue
    return out


def fingerprint(text: str) -> str:
    """The sha256 of a script's text however its lines end (script.py is written with this computer's line ends): a
    run keeps it of the text it ran, and that text runs again only while it still matches."""
    return hashlib.sha256(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")).hexdigest()


def source_of(path: Path) -> str:
    """The script's text as Python (a notebook's code cells in order, its % and ! lines left out). A .py file is
    read as Python reads it: in the encoding its coding line names (# -*- coding: gbk -*-), else UTF-8."""
    import tokenize
    try:
        if path.suffix.lower() == ".py":
            with tokenize.open(str(path)) as f:
                text = f.read()
        else:
            text = path.read_text(encoding="utf-8-sig")
    except (UnicodeDecodeError, SyntaxError) as e:
        raise ScriptError("%s can't be read as Python reads it (%s): save it as UTF-8, or name its encoding in "
                          "a coding line" % (path.name, e)) from None
    except OSError as e:
        raise ScriptError("%s can't be read: %s" % (path.name, e.strerror or e)) from None
    try:
        return lab_scripts._python_source(path, text)
    except ValueError as e:
        raise ScriptError("%s is not a notebook Kelvin can read (%s)" % (path.name, e)) from None


# ------------------------------------------------------------------ the values a person or the agent may change

_NO = object()


@dataclass
class Param:
    name: str
    value: Any
    line: int
    where: str          # "setting": a module constant; "main": a default of main()
    note: str = ""      # the comment after it, if any
    node: Any = None

    def text(self) -> str:
        return "%s = %r%s%s" % (self.name, self.value, " (main)" if self.where == "main" else "",
                                "   # " + self.note if self.note else "")


def _literal(node: ast.AST) -> Any:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return _NO
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return _NO
    return value


def _comment(lines: List[str], node: ast.AST) -> str:
    end = getattr(node, "end_lineno", node.lineno)
    if not 0 < end <= len(lines):
        return ""
    line = lines[end - 1]
    tail = line.encode("utf-8")[getattr(node, "end_col_offset", len(line)):].decode("utf-8", "replace")
    m = re.match(r"^[\s,)\]]*#\s?(.*)$", tail)
    return m.group(1).strip() if m else ""


def parameters(source: str) -> List[Param]:
    """What can be changed for a run without editing the file: the script's module constants with a plain value
    (VMAX = 5, a comment after it says what it is), and the defaults of its main() function."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise ScriptError("it does not parse as Python 3 (line %s: %s)" % (e.lineno, e.msg)) from None
    lines = source.splitlines()
    out: List[Param] = []
    seen: Set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if len(targets) != 1 or not isinstance(targets[0], ast.Name) or node.value is None:
                continue
            name = targets[0].id
            value = _literal(node.value)
            if value is _NO or name.startswith("__") or name in seen or value is None or value in ([], {}, ()):
                continue   # not a value to set: rows = [] and data = None are where the script keeps its own
            seen.add(name)
            out.append(Param(name, value, node.lineno, "setting", _comment(lines, node.value), node.value))
        elif isinstance(node, ast.FunctionDef) and node.name == "main":
            a = node.args
            positional = list(getattr(a, "posonlyargs", [])) + list(a.args)
            pairs = list(zip(positional[len(positional) - len(a.defaults):], a.defaults)) + \
                [(arg, d) for arg, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
            for arg, default in pairs:
                value = _literal(default)
                if value is not _NO and arg.arg not in seen:
                    seen.add(arg.arg)
                    out.append(Param(arg.arg, value, default.lineno, "main", "", default))
    return out


def _coerce(param: Param, value: Any) -> Any:
    old = param.value
    if isinstance(old, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "on", "off", "yes", "no", "1", "0"):
            return value.strip().lower() in ("true", "on", "yes", "1")
        raise ScriptError("%s is a yes/no setting (now %r), not %r" % (param.name, old, value))
    if isinstance(old, (int, float)):
        if isinstance(value, str):
            try:
                value = float(value) if re.search(r"[.eE]", value) else int(value)
            except ValueError:
                raise ScriptError("%s is a number (now %r), not %r" % (param.name, old, value)) from None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ScriptError("%s is a number (now %r), not %r" % (param.name, old, value))
        return float(value) if isinstance(old, float) and isinstance(value, int) else value
    if isinstance(old, str) and not isinstance(value, str):
        raise ScriptError("%s is text (now %r), not %r" % (param.name, old, value))
    if isinstance(old, (list, tuple)) and not isinstance(value, (list, tuple)):
        raise ScriptError("%s is a list (now %r), not %r" % (param.name, old, value))
    return type(old)(value) if isinstance(old, tuple) else value


def changed_params(source: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """The values that change the script: those equal to its own (VMAX = 1 for a file that has 1) go, so a run keeps,
    and runs again with, only what a person changed, and an edit of the file counts from then on. A value it can't
    take stays, for with_params to say why."""
    try:
        found = {p.name: p for p in parameters(source)}
    except ScriptError:
        return dict(values)
    out: Dict[str, Any] = {}
    for name, value in values.items():
        p = found.get(name)
        try:
            same = p is not None and _coerce(p, value) == p.value and type(_coerce(p, value)) is type(p.value)
        except ScriptError:
            same = False
        if not same:
            out[name] = value
    return out


def with_params(source: str, values: Dict[str, Any]) -> Tuple[str, List[str]]:
    """The script's text with these settings changed, each in place (its lines keep their numbers, so a traceback
    points at the script's own lines), and a line per change."""
    if not values:
        return source, []
    found = {p.name: p for p in parameters(source)}
    unknown = [k for k in values if k not in found]
    if unknown:
        raise ScriptError("%s has no setting %s (it has: %s)" % (
            "the script", ", ".join(unknown), ", ".join(found) or "none Kelvin can change; give args instead"))
    lines = source.splitlines(keepends=True)
    edits = []
    changes = []
    for name, value in values.items():
        p = found[name]
        new = _coerce(p, value)
        node = p.node
        edits.append((node.lineno, node.col_offset, node.end_lineno, node.end_col_offset, repr(new)))
        changes.append("%s = %r (the script has %r)" % (name, new, p.value))
    for l0, c0, l1, c1, text in sorted(edits, reverse=True):
        first = lines[l0 - 1].encode("utf-8")
        last = lines[l1 - 1].encode("utf-8")
        merged = (first[:c0].decode("utf-8") + text + "\n" * (l1 - l0) + last[c1:].decode("utf-8"))
        lines[l0 - 1:l1] = [merged]
    return "".join(lines), changes


# ------------------------------------------------------------------ the Python that runs it

def imports(source: str) -> List[str]:
    """The top-level modules the script imports, anywhere in it (standard library left out where Python knows it)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    names: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name.partition(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.append(node.module.partition(".")[0])
    std = getattr(sys, "stdlib_module_names", frozenset())
    return [n for n in dict.fromkeys(names) if n not in std and n not in ("__future__",)]


@dataclass
class Python:
    command: List[str]     # starts it; the helper's path and the script's arguments follow
    text: str              # what it is, for people: "C:/lab/venv/Scripts/python.exe (Python 3.11)"
    version: Tuple[int, int] = (0, 0)


_PROBED: Dict[Tuple[Any, ...], Tuple[float, Dict[str, Any]]] = {}


def own_python() -> List[str]:
    """Kelvin's own Python: the app runs a script with --bridge (packaging/kelvin_app.py)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--bridge"]
    return [sys.executable, "-u"]


def _candidates(script: Path, roots: Sequence[Path]) -> List[Tuple[List[str], str]]:
    out: List[Tuple[List[str], str]] = []
    seen: Set[str] = set()

    def add(command: List[str], text: str) -> None:
        key = os.path.normcase(" ".join(command))
        if key not in seen:
            seen.add(key)
            out.append((command, text))

    bin_dir, exe = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    for d in [script.parent] + list(script.parents):
        for name in (".venv", "venv", "env"):
            p = d / name / bin_dir / exe
            if p.is_file():
                add([str(p), "-u"], "%s (the environment beside the scripts)" % p)
        if any(d == r for r in roots) or len(d.parts) <= 1:
            break
    if os.name == "nt" and shutil.which("py"):
        add(["py", "-3", "-u"], "this computer's Python (py -3)")
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found and "WindowsApps" not in found and not (getattr(sys, "frozen", False) and
                                                          os.path.samefile(found, sys.executable)):
            add([found, "-u"], "%s (this computer's Python)" % found)
    add(own_python(), "Kelvin's own Python")
    return out


def _probe(command: List[str], folder: Path, wanted: List[str]) -> Dict[str, Any]:
    key = (tuple(command), str(folder), tuple(sorted(wanted)))
    hit = _PROBED.get(key)
    if hit and time.monotonic() - hit[0] < PROBE_KEEP_S:
        return hit[1]
    from .envs import clean_environ
    try:
        proc = subprocess.run(command + [str(SHIM), "--probe"] + wanted, cwd=str(folder), env=clean_environ(),
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=PROBE_S,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        found = json.loads(proc.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as e:
        found = {"error": str(e) or type(e).__name__}
    _PROBED[key] = (time.monotonic(), found)
    return found


def _configured(configured: str) -> Tuple[List[str], str]:
    p = Path(configured).expanduser()
    return [str(p), "-u"], "%s (set for the lab's scripts)" % p


def python_named(script: Path, roots: Sequence[Path], configured: str, named: str) -> Optional[Tuple[List[str], str]]:
    """The Python named (a path, or as a run's record names it: "C:/lab/.venv/Scripts/python.exe (the environment
    beside the scripts), Python 3.11"), if it is one Kelvin runs the lab's scripts with here: the one set for them,
    or one it finds (an environment beside the script, this computer's, its own). Never any other program."""
    named = str(named or "").strip()
    if not named:
        return None
    bare = re.sub(r", Python \d+\.\d+$", "", named)
    found = ([_configured(configured)] if configured else []) + _candidates(script, roots)
    for command, text in found:
        if bare == text or os.path.normcase(bare) == os.path.normcase(command[0]):
            return command, text
    return None


def python_for(script: Path, source: str, roots: Sequence[Path], configured: str = "", named: str = "") -> Python:
    """The Python that runs the script: the one named (python_named), when given; else the one a person set for the
    lab's scripts; else the first, best first (an environment beside the scripts, this computer's, Kelvin's own),
    that has every module the script imports (kelvin, Kelvin's experiment API, comes with Kelvin's stand-in)."""
    wanted = [m for m in imports(source) if m != experiment.API]
    if named:
        pick = python_named(script, roots, configured, named)
        if pick is None:
            raise ScriptError("%s is not a Python Kelvin runs the lab's scripts with here (the one set in Settings, an "
                              "environment beside the script, this computer's, or Kelvin's own): leave python out to "
                              "use the one Kelvin picks" % named)
        candidates = [pick]
    elif configured:
        candidates = [_configured(configured)]
    else:
        candidates = _candidates(script, roots)
    lacking = []
    for command, text in candidates:
        found = _probe(command, script.parent, wanted)
        if "error" in found:
            lacking.append("%s: it did not start (%s)" % (text, found["error"]))
            continue
        version = tuple(found.get("version") or (0, 0))[:2]
        if version < (3, 6):
            lacking.append("%s: Python %s.%s is too old (3.6 or later)" % ((text,) + version))
            continue
        missing = found.get("missing") or []
        if not missing:
            return Python(command, "%s, Python %d.%d" % ((text,) + version), version)
        lacking.append("%s lacks %s" % (text, ", ".join(missing)))
    raise ScriptError("no Python here has everything %s imports: %s. A person sets the Python for the lab's scripts "
                      "in Settings (or installs what is missing)" % (script.name, "; ".join(lacking)))


# ------------------------------------------------------------------ what the script will do, read before it runs

def canonical(address: str) -> str:
    """An address as a VISA resource, so the script's spelling and the lab's compare (GPIB::24 = GPIB0::24::INSTR,
    COM3 = ASRL3::INSTR, USB ids in hex or decimal)."""
    a = (address or "").strip()
    if a.lower().startswith("sim://"):
        return a.lower()
    a = resource_name(lab_scripts.normalize(a)).upper()
    m = re.match(r"^USB(\d*)::(\w+)::(\w+)::([^:]+)(?:::(\d+))?(?:::(INSTR|RAW))?$", a)
    if m:
        try:
            return "USB%s::0X%04X::0X%04X::%s%s::%s" % (m.group(1) or "0", int(m.group(2), 0), int(m.group(3), 0),
                                                       m.group(4), "::" + m.group(5) if m.group(5) else "",
                                                       m.group(6) or "INSTR")
        except ValueError:
            return a
    return a


def lab_name(lab: Any, address: str) -> Optional[str]:
    """The lab instrument at this address, if any."""
    want = canonical(address)
    for name, ic in lab.config.instruments.items():
        if canonical(ic.address) == want or (want.startswith("sim://") and want == "sim://" + name.lower()):
            return name
    return None


def behind(lab: Any, address: str) -> List[str]:
    """The lab instruments behind the Prologix adapter at this address (its serial port, or its TCP port): a script that
    opens the adapter itself and speaks its ++ commands reaches them there, past the gate."""
    want = canonical(address)
    names = []
    for name, ic in lab.config.instruments.items():
        if prologix.is_prologix(ic.address):
            try:
                adapter = prologix.where(ic.address).adapter
            except ValueError:
                continue
            if canonical(prologix.visa_name(adapter)) == want:
                names.append(name)
    return names


def outside(lab: Any, address: str) -> str:
    """An address the script opens that is no lab instrument's, and why Kelvin can't check what goes there."""
    under = behind(lab, address)
    if under:
        return ("%s, the Prologix adapter this lab's %s %s behind: commands sent through it skip the safety gate"
                % (address, ", ".join(under), "is" if len(under) == 1 else "are"))
    return "%s, which is not in this lab: Kelvin can't check what it sends there" % address


@dataclass
class Found:
    instruments: Dict[str, str] = field(default_factory=dict)   # lab name -> the address the script opens it at
    drives: Dict[str, List[Any]] = field(default_factory=dict)  # instrument.param -> the values the script sets
    open: Dict[str, str] = field(default_factory=dict)          # instrument.param -> why its values aren't known yet
    unknown: List[str] = field(default_factory=list)            # addresses it opens that aren't in the lab
    libraries: List[str] = field(default_factory=list)          # makers' libraries that reach instruments past the gate
    notes: List[str] = field(default_factory=list)
    experiment: bool = False   # it uses Kelvin's experiment API (from kelvin import lab: experiment.py)
    named: List[str] = field(default_factory=list)     # lab instruments it uses by name, through kelvin.lab
    actions: List[str] = field(default_factory=list)   # instr.action it does that drives the sample, read ahead


def _setter(card: Any, command: str) -> Tuple[Optional[Any], Optional[str]]:
    """The parameter a command sets, as the gate reads it (the card's setters, most specific first)."""
    for spec in card.setters:
        try:
            text = spec.set_cmd.match(command)
        except Exception:
            continue
        if text is not None:
            return spec, text
    return None, None


def _part_values(spec: Any, part: str, values: Dict[str, List[float]]) -> Optional[List[Any]]:
    """The parameter values one part of a command sets: from its literal text, or each number a fill-in takes; None
    when a fill-in's numbers aren't known before the run."""
    places = _PLACE.findall(part)
    if not places:
        text = spec.set_cmd.match(part)
        try:
            return [spec.from_wire(text)] if text is not None else None
        except ValueError:
            return None
    if any(not values.get(p) for p in places):
        return None
    first = {p: values[p][0] for p in places}
    out: List[Any] = []
    for p in places:
        for x in values[p]:
            pick = dict(first, **{p: x})
            text = spec.set_cmd.match(_PLACE.sub(lambda m: lab_scripts._num(pick[m.group(1)]), part))
            if text is None:
                return None
            try:
                out.append(spec.from_wire(text))
            except ValueError:
                return None
    return out


def _all_values(spec: Any, inst: Any) -> Optional[List[Any]]:
    """Every value a drive may take within its limits: the range for a number, each value for a switch or a choice."""
    if spec.numeric:
        lim = inst.limits(spec.name)
        return [lim[0], lim[1]] if lim else None
    if spec.type == "bool":
        return [True, False]
    if spec.type == "enum":
        return list(spec.values or {})
    return None


def _local_modules(path: Path, source: str) -> List[Path]:
    out = []
    for name in imports(source):
        for p in (path.parent / (name + ".py"), path.parent / name / "__init__.py"):
            if p.is_file():
                out.append(p.parent if p.name == "__init__.py" else p)
    return out


def read_ahead(lab: Any, path: Path, source: str, action: str = "") -> Found:
    """What a script will do, read from its text and the lab's own modules it imports (scripts.py; nothing is run):
    the lab instruments it opens, the drive values it sets, and what Kelvin can't check. An experiment's use of
    kelvin.lab too (experiment.py), for the action that runs (action; "": all of it)."""
    found = Found()
    found.libraries = [m for m in imports(source) if m in OPAQUE]
    with tempfile.TemporaryDirectory(prefix="kelvin-read-") as tmp:
        here = Path(tmp)
        (here / "script.py").write_text(source, encoding="utf-8")
        for m in _local_modules(path, source):
            try:
                if m.is_dir():
                    shutil.copytree(m, here / m.name, ignore=shutil.ignore_patterns("__pycache__"))
                else:
                    shutil.copy2(m, here / m.name)
                    found.libraries += [x for x in imports(m.read_text(encoding="utf-8", errors="replace"))
                                        if x in OPAQUE]
            except OSError:
                pass
        try:
            survey = lab_scripts.survey(here)
        except Exception as e:   # reading is a help: the gate checks every command as it is sent anyway
            found.notes.append("Kelvin could not read the script ahead (%s); it checks each command as it is sent"
                               % e)
            return found
    found.libraries = list(dict.fromkeys(found.libraries))
    for si in survey.instruments:
        if not si.address:
            found.notes.append("it opens an instrument at an address it works out as it runs (%s): Kelvin matches it "
                               "to the lab when it opens" % (si.expression or ", ".join(si.names) or "?"))
            continue
        name = lab_name(lab, si.address)
        if name is None:
            if si.address not in found.unknown:
                found.unknown.append(si.address)
            continue
        found.instruments.setdefault(name, si.address)
        inst = lab.instruments[name]
        if si.library.startswith("driver") or (si.calls and not si.commands):
            # a driver's own API (QCoDeS, PyMeasure: smu.source_voltage = v) sends commands the text doesn't show
            driver = si.library[len("driver"):].strip() or si.library
            for spec in inst.card.drives:
                if spec.set_cmd is not None:
                    found.open.setdefault("%s.%s" % (name, spec.name), "set through its driver, %s" % driver)
        for c in si.commands:
            if not c.query:
                _note_command(found, name, inst, c.text, c.values)
    if experiment.is_experiment(source):
        _read_experiment(lab, found, source, action)
    return found


def _note_command(found: Found, name: str, inst: Any, text: str, values: Dict[str, List[float]]) -> None:
    """The drive values one command the script sends sets, into found (its fill-ins' numbers: values)."""
    for part in split_compound(text) or [text]:
        spec, _ = _setter(inst.card, _PLACE.sub(lambda m: lab_scripts._num(
            (values.get(m.group(1)) or [1])[0]), part))
        if spec is None or not spec.is_drive:
            continue
        key = "%s.%s" % (name, spec.name)
        got = _part_values(spec, part, values)
        if got is None:
            found.open.setdefault(key, "set from values the script works out as it runs")
        else:
            found.drives.setdefault(key, [])
            found.drives[key] += [v for v in got if v not in found.drives[key]]


def _read_experiment(lab: Any, found: Found, source: str, action: str) -> None:
    """What an experiment does through kelvin.lab, read ahead (experiment.py), into found: the instruments it uses by
    name, the drive values it sets, the actions it does that drive the sample, and what can't be read ahead."""
    found.experiment = True
    try:
        used = experiment.uses(experiment.read(source), action)
    except SyntaxError:
        return

    def by_name(name: str) -> Optional[Any]:
        ic = lab.config.instruments.get(name)
        if ic is None or name not in lab.instruments:
            return None
        found.instruments.setdefault(name, ic.address)
        if name not in found.named:
            found.named.append(name)
        return lab.instruments[name]

    def resolve(key: str) -> Optional[Tuple[Any, Any]]:
        try:
            inst, spec = lab.resolve(key)
        except KeyError as e:
            found.notes.append("%s names no instrument parameter in this lab (%s): the safety gate refuses it"
                               % (key, e.args[0] if e.args else e))
            return None
        by_name(inst.name)
        return inst, spec

    for key, values in used.sets.items():
        got = resolve(key)
        if got is None or not got[1].is_drive:
            continue
        spec = got[1]
        if values is None:
            found.open.setdefault(key, "set from values the experiment works out as it runs")
            continue
        found.drives.setdefault(key, [])
        for v in values:
            try:
                v = spec.coerce(v)
            except ValueError as e:
                found.notes.append("%s: %s" % (key, e))
                continue
            if v not in found.drives[key]:
                found.drives[key].append(v)
    for key in used.gets:
        resolve(key)
    for key in used.acts:
        name, _, act = key.partition(".")
        inst = by_name(name)
        if inst is None:
            found.notes.append("%s names no instrument in this lab: the safety gate refuses it" % key)
            continue
        card_action = inst.card.actions.get(act)
        if card_action is None:
            found.notes.append("%s: %s has no action %s, so the safety gate refuses it" % (key, name, act))
        elif card_action.drives and key not in found.actions:
            found.actions.append(key)
    for name, commands in used.raw.items():
        inst = by_name(name)
        if inst is None:
            found.notes.append("lab.raw(%r, ...) names no instrument in this lab: the safety gate refuses it" % name)
            continue
        for text in commands:
            if text is not None:
                _note_command(found, name, inst, text, {})
                continue
            for spec in inst.card.drives:
                if spec.set_cmd is not None:
                    found.open.setdefault("%s.%s" % (name, spec.name),
                                          "sent as commands the experiment works out as it runs")
    found.notes += used.unread


# ------------------------------------------------------------------ getting it ready

@dataclass
class Prepared:
    path: Path
    original: str
    source: str               # as it runs: with the settings asked for
    changes: List[str]        # one line per changed setting
    args: List[str]
    python: Python
    found: Found
    sha256: str               # of the file as this run reads it (a version's: of the file as that run read it)
    declared: Dict[str, List[Any]] = field(default_factory=dict)   # drive ranges the agent gave for this run
    params: Dict[str, Any] = field(default_factory=dict)           # the values asked for (changes says them)
    version: Optional[str] = None   # the run whose script.py this runs, as it ran then (None: the file as it is)
    near: Dict[str, str] = field(default_factory=dict)   # the Python files beside it as it starts, hashed (beside)
    action: str = ""          # an experiment's action this run runs ("": it has none; experiment.choose)
    live: Dict[str, Any] = field(default_factory=dict)   # an experiment's live inputs and their defaults
    # one of the lab's kept experiments (expstore): its process is fenced in its real run too (kelvin_shim._fence)
    fenced: bool = False
    title: str = ""   # what its run is called instead of its file's name (one of the lab's experiments: its name)

    @property
    def name(self) -> str:
        return (self.title or self.path.name) + (" (%s)" % self.action if self.action else "")

    def drive_values(self, lab: Any) -> Tuple[Dict[str, List[Any]], Dict[str, str], List[str]]:
        """Every drive value the run may use, for the approval; for each drive whose values aren't known before the
        run (the script works them out, or a driver sets them), why its whole range within the limits is asked
        for; and a line for such a drive with no limits to approve."""
        values = {k: list(v) for k, v in self.found.drives.items()}
        whole: Dict[str, str] = {}
        lines = []
        for key, vals in self.declared.items():
            values[key] = list(vals)
        for key, why in self.found.open.items():
            if key in self.declared:
                continue
            inst, spec = lab.resolve(key)
            every = _all_values(spec, inst)
            if every is None:
                lines.append("%s is %s and has no limits to approve, so each change away from safe asks a person"
                             % (key, why))
                continue
            values[key] = every
            whole[key] = ("anywhere within its limits, since it is %s" if spec.numeric else "since it is %s") % why
        return values, whole, lines


def declared_drives(lab: Any, drives: Optional[Dict[str, Any]]) -> Dict[str, List[Any]]:
    """The agent's own ranges for drives the script sets: {"smu.V": [0, 5]} (a range), {"smu.output": [true]}."""
    out: Dict[str, List[Any]] = {}
    for key, given in (drives or {}).items():
        try:
            inst, spec = lab.resolve(key)
        except KeyError as e:
            raise ScriptError("drives: %s" % e.args[0]) from None
        if not spec.is_drive:
            raise ScriptError("drives: %s is a setting, not a drive; it needs no approval" % key)
        vals = given if isinstance(given, (list, tuple)) else [given]
        try:
            out[key] = [spec.coerce(v) for v in vals]
        except ValueError as e:
            raise ScriptError("drives: %s" % e) from None
        if spec.numeric and len(out[key]) == 2:
            out[key] = sorted(out[key])
    return out


def prepare(lab: Any, path: str, folders: Sequence[Any], params: Optional[Dict[str, Any]] = None,
            args: Optional[Sequence[Any]] = None, drives: Optional[Dict[str, Any]] = None,
            python: str = "", choose_python: Callable[..., Python] = python_for, chosen: Sequence[Any] = (),
            source: Optional[str] = None, sha256: Optional[str] = None, version: Optional[str] = None,
            named: str = "", action: str = "") -> Prepared:
    """A script read and ready to run. chosen: the scripts a person imported (locate). source: the text to run in
    place of the file's own (version: the run whose script.py it is, as it ran then; sha256: of the file as that run
    read it), in the file's own folder and under its own name, which may be gone by now. named: the Python to run it
    in (python_named). params: only those that change the file's own values count (changed_params); a version's
    text has its run's values in it already. action: the experiment's action to run (experiment.choose: the one marked
    main when none is named)."""
    script = locate(path, folders, chosen, missing=source is not None)
    original = source if source is not None else source_of(script)
    params = dict(params or {}) if source is not None else changed_params(original, dict(params or {}))
    text, changes = with_params(original, params)
    try:
        compile(text, str(script), "exec")
    except SyntaxError as e:
        raise ScriptError("%s does not parse as Python 3 (line %s: %s)" % (script.name, e.lineno, e.msg)) from None
    live: Dict[str, Any] = {}
    if experiment.is_experiment(text):
        try:
            exp = experiment.read(text)
            action = experiment.choose(exp, action)
        except ValueError as e:
            raise ScriptError("%s: %s" % (script.name, e)) from None
        live = dict(exp.live)
    elif action:
        raise ScriptError("%s doesn't use kelvin, so it has no actions" % script.name)
    roots = [Path(f).expanduser().resolve() for f in folders if f]
    py = choose_python(script, text, roots, python, named) if named else choose_python(script, text, roots, python)
    return Prepared(script, original, text, changes, [str(a) for a in (args or [])], py,
                    read_ahead(lab, script, text, action),
                    sha256 or hashlib.sha256(original.encode("utf-8")).hexdigest(),
                    declared_drives(lab, drives), params, version, beside(script), action, live)


def approval(gate: SafetyGate, prep: Prepared) -> Tuple[ApprovalRequest, Dict[str, Allowed], List[str]]:
    """The one request a person answers before the script runs, the drive ranges it grants, and refusals (a drive
    nothing may move now: hands on the sample, an interlock)."""
    lab = gate.lab
    values, whole, open_lines = prep.drive_values(lab)
    items, grants, problems = gate.plan_approval(values, noun="the script")
    for k, item in enumerate(items):   # "smu.V: now 0 V, the script uses -5 … 5 V, anywhere within its limits, …"
        why = whole.get(item.partition(":")[0])
        if why:
            head, sep, rest = item.partition("; ")
            items[k] = "%s, %s%s%s" % (head, why, sep, rest)
    found = prep.found
    head = "%s runs with %s" % (prep.name, prep.python.text)
    if prep.version:
        head += "; as it ran in run %s (its script.py), not the file as it is now" % prep.version
    if prep.changes:
        head += "; with " + "; ".join(prep.changes)
    if prep.args:
        head += "; arguments: " + " ".join(prep.args)
    lines = [head]
    opened = {n: a for n, a in found.instruments.items() if n not in found.named}
    if opened:
        lines.append("It opens %s; every command goes through the safety gate as it is sent" % ", ".join(
            "%s (%s)" % (n, a) for n, a in opened.items()))
    if found.named:
        lines.append("It uses %s through kelvin.lab; every command goes through the safety gate as it is sent"
                     % ", ".join(n for n in found.instruments if n in found.named))
    for key in found.actions:
        name, _, act = key.partition(".")
        lines.append("It does %s (%s), which drives the sample: this approval covers it"
                     % (key, lab.instruments[name].card.actions[act].label))
    lines += items + open_lines
    lines += ["It also opens " + outside(lab, a) for a in found.unknown]
    lines += ["It uses %s, which reaches instruments without Kelvin seeing the commands" % m for m in found.libraries]
    lines += found.notes
    if prep.live:
        lines.append("While it runs, these may change (set_live): %s; a change that moves a drive still passes the "
                     "safety gate" % ", ".join(k if v is None else "%s (now %s)" % (k, v) for k, v in prep.live.items()))
    if found.experiment:
        lines.append("A person approves each run of a script, this experiment too, in auto mode as well; run_script "
                     "with dry_run rehearses it on the simulated lab first")
    else:
        lines.append("A script can't be rehearsed on the simulated lab, so a person approves each run; a maker's "
                     "library it loads on the way comes with it, and anything else beyond this asks again, or is "
                     "refused")
    return ApprovalRequest("Run script %s?" % prep.name, lines), grants, problems


# ------------------------------------------------------------------ the run

class _Declines(Approver):
    """While a script's run stops: its own clean-up may bring drives toward safe, never away (nobody is asked)."""

    def __init__(self, why: str):
        self.why = why

    def approve(self, request: ApprovalRequest) -> bool:
        request.note = self.why
        return False


@dataclass
class ScriptReport:
    status: str                  # done | aborted | refused | declined
    message: str
    run_id: Optional[str] = None
    path: Optional[str] = None
    rows: List[Dict[str, Any]] = field(default_factory=list)
    columns: Dict[str, Any] = field(default_factory=dict)
    commands: int = 0
    output: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    figures: List[str] = field(default_factory=list)
    left: List[str] = field(default_factory=list)       # drives it left away from safe
    cleanup: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    duration_s: float = 0.0
    analysis: Optional[Dict[str, Any]] = None


class _Job:
    """The script's process and every process it starts: ended together (Windows: a job object, which also ends
    them if Kelvin itself ends; elsewhere: the process group)."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.handle = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
            k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
            k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
            k32.CloseHandle.argtypes = (wintypes.HANDLE,)

            class _Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class _Io(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                                                           "OtherOperationCount", "ReadTransferCount",
                                                           "WriteTransferCount", "OtherTransferCount")]

            class _Extended(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", _Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            job = k32.CreateJobObjectW(None, None)
            if not job:
                return
            info = _Extended()
            info.BasicLimitInformation.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
            if ok and k32.AssignProcessToJobObject(job, int(proc._handle)):   # Popen's own handle (all access)
                self.handle, self._k32 = job, k32
            else:
                k32.CloseHandle(job)
        except Exception:
            self.handle = None

    def end(self) -> None:
        """End the script's process and every one it started."""
        if self.handle is not None:
            self._k32.TerminateJobObject(self.handle, 1)
        elif os.name == "nt":
            if self.proc.poll() is None:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            import signal
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                pass

    def close(self) -> None:
        if self.handle is not None:
            self._k32.TerminateJobObject(self.handle, 1)   # nothing the script started outlives its run
            self._k32.CloseHandle(self.handle)
            self.handle = None


class _Conn:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = b""
        self.ok = False


class ScriptRun:
    """One run of a prepared script: its process, Kelvin's end of the connection to its stand-ins, and the record."""

    def __init__(self, gate: SafetyGate, prep: Prepared, store: RunStore, grants: Dict[str, Allowed],
                 allowed: Set[str], meta: Optional[Dict[str, Any]] = None, folder: Any = None,
                 busy: Optional[Callable[[str], None]] = None,
                 progress: Optional[Callable[[Dict[str, Any]], None]] = None, rehearsal: bool = False):
        self.gate, self.lab, self.prep = gate, gate.lab, prep
        # a rehearsal on the simulated lab (rehearse): nothing outside it, in a folder of its own, its waits skipped
        self.rehearsal = rehearsal
        self.killed = False   # it did not end after its stop, so Kelvin ended it
        self.store, self.grants, self.meta, self.folder = store, grants, dict(meta or {}), folder
        # unknown addresses a person allowed, as VISA resources however they are spelled (the approval's serial://COM5
        # is the script's COM5 or ASRL5::INSTR), and makers' libraries, by name
        self.allowed = {a if a in OPAQUE else canonical(a) for a in allowed}
        self.busy = busy or (lambda text: None)
        self.progress = progress   # told of the run as it goes, as RunEngine.progress is: the live views follow it
        self.token = secrets.token_hex(16)
        self.stopping: Optional[str] = None  # why the run is stopping
        self.failed: Optional[str] = None    # the gate's refusal (or a person's no) that stopped it
        self.touched: Dict[str, None] = {}   # drives it set, in order
        self.unparsed: Dict[str, None] = {}  # instruments a drive command went to that the gate couldn't read
        self.opened: Dict[str, str] = {}     # lab instrument -> the address the script opened it at
        self.passed: List[str] = []          # what it opened past the gate (a person allowed it)
        self.through: List[str] = []         # makers' libraries it loaded on the way, let through with the run
        self.bye: Dict[str, Any] = {}
        self.n_commands = 0
        self.state: Dict[str, Any] = {}      # what the script set, as it is now
        self.reading: Dict[str, Any] = {}    # readings since its last change
        self.read_keys: Dict[str, None] = {}
        self.first: Dict[str, str] = {}      # each setting's value in the first row it is in
        self.changed: Dict[str, None] = {}   # the settings a later row has another value of: the run's axes
        self.units: Dict[str, str] = {}      # each column's unit: its card's, or the one an experiment names
        self.rows: List[Dict[str, Any]] = []
        # an experiment's rows are its own (kelvin's record), never Kelvin's from its settings and readings
        self.explicit = prep.found.experiment
        self.kinds: Dict[str, str] = {}             # its recorded columns, each scalar, trace, image or text
        self.axes: Optional[List[str]] = None       # its axes, as it named them (kelvin's columns)
        # an experiment's live inputs, as they are now: a person or the chat changes them while it runs (set_live)
        self.live: Dict[str, Any] = dict(prep.live)
        # read ahead, but not read by the experiment yet: its own default (the value its code gives) takes over at its
        # first read, unless a person changed it before then
        self._unread_live: Set[str] = set(prep.live)
        self.live_changes: List[Dict[str, Any]] = []
        self._live_lock = threading.Lock()
        self._log_lock = threading.Lock()   # commands.jsonl: the script's requests, and set_live from elsewhere
        self.ended = False
        self.held: Dict[str, List[str]] = {}   # errors the gate read from an instrument, for the script's own query
        self.reported = 0                      # commands an instrument reported an error for
        self.rec: Any = None
        self.rec_id: Optional[str] = None
        self.commands_file: Any = None
        self.t0 = self.last_activity = time.monotonic()
        self.stop_at = 0.0
        self._shown = 0.0
        self._last = ""

    # ---------------------------------------------------------------- recording

    def _flush(self) -> None:
        if not self.reading or self.explicit:
            return
        row = {"i": len(self.rows), "t": self.reading.pop("_t")}
        row.update((k, v) for k, v in self.state.items() if _number(v))
        row.update(self.reading)
        self.rows.append(row)
        for k in self.state:
            if k in row and self.first.setdefault(k, repr(row[k])) != repr(row[k]):
                self.changed[k] = None
        if self.rec is not None:
            self.rec.add(row)
            self._emit(event="point", run_id=self.rec_id, done=len(self.rows), total=None, row=row, eta_s=None,
                       columns=self._columns())   # its axes and readings so far: they grow as it runs
        self.reading = {}

    def _emit(self, **event: Any) -> None:
        if self.progress is not None:
            try:
                self.progress(event)
            except Exception:
                pass   # a broken display must never stop an experiment

    def _note_set(self, key: str, value: Any) -> None:
        if key in self.state and self.state[key] == value:
            return
        self._flush()
        self.state[key] = value

    def _note_read(self, inst: Any, command: str, reply: str) -> None:
        if self.explicit:
            return
        for spec in inst.card.getters:
            try:
                hit = spec.get_cmd.match(command)
            except Exception:
                continue
            if hit is None:
                continue
            if not spec.numeric or spec.set_cmd is not None:   # a setting read back is not a measurement
                return
            try:
                value = spec.from_wire(reply)
            except (ValueError, TypeError):
                return
            if not _number(value):
                return
            key = "%s.%s" % (inst.name, spec.name)
            if key in self.reading:
                self._flush()
            if not self.reading:
                self.reading["_t"] = round(time.monotonic() - self.t0, 6)
            self.reading[key] = value
            self.read_keys[key] = None
            return

    def _columns(self) -> Dict[str, Any]:
        if self.explicit:   # an experiment's: the axes it named, else the first column it recorded
            scalars = [k for k, kind in self.kinds.items() if kind == "scalar"]
            axes = [k for k in (self.axes if self.axes is not None else scalars[:1]) if k in self.kinds]
            return {"axes": [{"key": k, "unit": self.units.get(k, ""), "kind": "instrument"} for k in axes],
                    "measures": [{"key": k, "unit": self.units.get(k, ""), "kind": kind}
                                 for k, kind in self.kinds.items() if kind != "text" and k not in axes]}

        def unit(key: str) -> str:
            if key not in self.units:
                try:
                    self.units[key] = self.lab.resolve(key)[1].unit
                except KeyError:
                    self.units[key] = ""
            return self.units[key]
        return {"axes": [{"key": k, "unit": unit(k), "kind": "instrument"} for k in self.state if k in self.changed],
                "measures": [{"key": k, "unit": unit(k), "kind": "scalar"} for k in self.read_keys]}

    def _log(self, **entry: Any) -> None:
        self.n_commands += 1
        self._write(entry)
        now = time.monotonic()
        if now - self._shown > 0.5:
            self._shown = now
            self.busy("script %s: %d commands%s" % (self.prep.name, self.n_commands,
                                                   ", " + self._last if self._last else ""))

    def _write(self, entry: Dict[str, Any]) -> None:
        with self._log_lock:
            if self.commands_file is not None and not self.commands_file.closed:
                entry["t"] = round(time.monotonic() - self.t0, 4)
                self.commands_file.write(json.dumps(entry, default=str) + "\n")

    # ---------------------------------------------------------------- live inputs

    def set_live(self, name: str, value: Any, by: str = "agent") -> Tuple[Any, Any]:
        """Change one of the experiment's live inputs while it runs (the experiment reads it with kelvin's live): (what
        it was, what it is now). Never touches the gate or the lab: a value that moves a drive passes the gate when the
        experiment applies it. KeyError: no such input; ValueError: a value it can't take; RuntimeError: the run is
        over."""
        if self.ended:
            raise RuntimeError("run %s has ended" % (self.rec_id or self.prep.name))
        with self._live_lock:
            if name not in self.live:
                raise KeyError("%s has no live input %s (it has: %s)" % (
                    self.prep.name, name, ", ".join(self.live) or "none"))
            before = self.live[name]
            after = _live_value(name, before, value)
            self.live[name] = after
            self._unread_live.discard(name)   # the experiment reads this, not its own default
            change = {"name": name, "value": after, "before": before, "by": by,
                      "t": round(time.monotonic() - self.t0, 4)}
            self.live_changes.append(change)
        self._write({"action": "live", "live": name, "value": after, "before": before, "by": by, "ok": True})
        self.gate.log.write("live", by=by, run=self.rec_id, plan=self.prep.name, name=name, value=after,
                            before=before)
        return before, after

    def _live(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        name = str(msg.get("name") or "")
        with self._live_lock:
            if name not in self.live or name in self._unread_live:   # its first read: the experiment's own default
                self.live[name] = msg.get("default")
                self._unread_live.discard(name)
            return {"value": self.live[name]}

    # ---------------------------------------------------------------- what the stand-ins ask

    @contextmanager
    def _quiet(self) -> Iterator[None]:
        """While stopping: nothing approved holds and nobody is asked, so only moves toward safe go through."""
        if not self.stopping:
            yield
            return
        approver = self.gate.approver
        self.gate.approver = _Declines("the script's run is stopping (%s)" % self.stopping)
        try:
            with self.gate.preapproved({}):
                yield
        finally:
            self.gate.approver = approver

    @contextmanager
    def _keeping_errors(self, inst: Any, caught: List[str]) -> Iterator[None]:
        """The gate reads an instrument's error queue after a command; what it reads is kept for the script's own
        error query, which would otherwise find the queue empty."""
        own = inst.instrument_error

        def kept() -> Optional[str]:
            err = own()
            if err:
                caught.append(err)
                self.held.setdefault(inst.name, []).append(err)
            return err
        inst.instrument_error = kept
        try:
            yield
        finally:
            del inst.instrument_error

    @contextmanager
    def _patience(self, inst: Any, timeout_ms: Optional[float]) -> Iterator[None]:
        """A reading the script waits longer for than Kelvin's connection would (its own timeout)."""
        backend = inst.backend
        res = getattr(backend, "res", None)
        lock = getattr(backend, "lock", None)
        if not timeout_ms or res is None or lock is None:
            yield
            return
        with lock:
            before = getattr(res, "timeout", None)
            try:
                if before is not None and float(timeout_ms) > before:
                    res.timeout = float(timeout_ms)
            except (TypeError, ValueError):
                before = None
            try:
                yield
            finally:
                if before is not None:
                    try:
                        res.timeout = before
                    except Exception:
                        pass

    def _instrument(self, msg: Dict[str, Any]) -> Any:
        name = str(msg.get("instrument") or "")
        if name not in self.opened:
            raise _Refuse("%s is not open: the script opens it first" % name, stop=False)
        return self.lab.instrument(name)

    def _command(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        return self._command_to(self._instrument(msg), msg)

    def _command_to(self, inst: Any, msg: Dict[str, Any]) -> Dict[str, Any]:
        text = str(msg.get("command") or "").strip()
        if not text:
            return {"reply": None}
        replies: List[str] = []
        enq = bool(inst.card.connection.get("enq"))
        for part in split_compound(text) or [text]:
            held = self.held.get(inst.name)
            if held and inst.card.error_query is not None and inst.card.error_query.match(part) is not None:
                replies.append(held.pop(0))   # an error Kelvin read after the script's command: the script's to read
                self._log(instrument=inst.name, command=part, action="read", ok=True, reply=replies[-1],
                          message="an error the instrument reported earlier, kept for the script")
                continue
            caught: List[str] = []
            with self._quiet(), self._patience(inst, msg.get("timeout") if is_query(part, enq) else None), \
                    self._keeping_errors(inst, caught):
                r = self.gate.raw(inst.name, part)
            self._log(instrument=inst.name, command=part, action=r.action, ok=r.ok, message=r.message,
                      reply=r.value if r.action == "read" and isinstance(r.value, str) and len(r.value) < 200
                      else None)
            if r.ok and r.action in ("set", "no-change") and "." in r.target:
                key = r.target
                _, spec = self.lab.resolve(key)
                if spec.is_drive:
                    self.touched[key] = None
                self._note_set(key, r.value)
                self._last = r.message
            elif r.ok and r.action == "read":
                replies.append(str(r.value))
                self._note_read(inst, part, str(r.value))
            elif r.ok and r.action == "sent" and r.message.endswith("(approved)"):
                self.unparsed[inst.name] = None   # a drive's command the gate could not read: a person let it go
            if not r.ok:
                spec, _ = _setter(inst.card, part)
                if r.action in STOPS or (spec is not None and spec.is_drive):
                    raise _Refuse(r.message, stop=True)   # a drive Kelvin can't vouch for: the run ends
                if caught or "may have refused or clipped" in r.message:
                    self.reported += 1   # the instrument refused a setting, as it would have without Kelvin: the
                    continue             # script goes on, and its own error query reads the error
                raise _Refuse(r.message, stop=False)   # the connection failed: the script gets the error, as from pyvisa
        if replies:
            return {"reply": ";".join(replies)}
        if msg.get("query"):   # a query the gate did not read as one (no ?): the instrument's answer, read
            return {"reply": self._read_back(inst, msg)}
        return {"reply": None}

    def _read_back(self, inst: Any, msg: Dict[str, Any], raw: bool = False, count: int = 0) -> Any:
        backend = inst.backend
        res = getattr(backend, "res", None)
        if res is None:
            if raw:
                raise _Refuse("%s has no reply waiting" % inst.name, stop=False)
            return ""
        with self._patience(inst, msg.get("timeout")), backend.lock:
            try:
                if count:
                    data = res.read_bytes(count, break_on_termchar=bool(msg.get("break_on_termchar")))
                elif raw:
                    data = res.read_raw()
                else:
                    data = res.read()
            except Exception as e:
                raise _Refuse("%s: %s" % (inst.name, e), stop=False) from None
        self._log(instrument=inst.name, command="(read)", action="read", ok=True, message="")
        if raw or count:
            return base64.b64encode(bytes(data)).decode("ascii")
        return str(data).strip()

    def _classify(self, inst: Any, command: str) -> Optional[str]:
        """For a command whose value Kelvin can't read (a binary block): why it can't go, or None."""
        card = inst.card
        for b in card.blocked:
            if b.match(command) is not None:
                return "%r is blocked on %s" % (command, inst.name)
        spec, _ = _setter(card, command)
        if spec is None:
            for s in card.drives:
                if s.set_cmd is not None and self.gate._prefix(s).match(command) is not None:
                    spec = s
                    break
        if spec is not None and spec.is_drive:
            return ("%r sends a block of values to drive %s.%s, which Kelvin can't check against its limits"
                    % (command, inst.name, spec.name))
        return None

    # ---------------------------------------------------------------- kelvin's record: an experiment's own rows

    def _record(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        given = msg.get("row")
        if not isinstance(given, dict):
            raise _Refuse("record takes columns: record(V=v, I=i)", stop=False)
        n = len(self.rows)
        row: Dict[str, Any] = {"i": n, "t": round(time.monotonic() - self.t0, 6)}
        for key, value in given.items():
            key = str(key)
            if key in ("i", "t"):
                raise _Refuse("i and t are Kelvin's own columns", stop=False)
            if isinstance(value, dict) and value.get("kelvin") == "trace":
                row[key], kind = list(value.get("values") or []), "trace"
                if value.get("x") is not None:
                    row[key + ".x"] = value["x"]
            elif isinstance(value, list) and value and all(isinstance(r, list) for r in value):
                row[key], kind = self._image(value, key, n), "image"
            elif isinstance(value, list):
                row[key], kind = value, "trace"
            else:
                row[key], kind = value, "text" if isinstance(value, str) else "scalar"
            self.kinds.setdefault(key, kind)
        self.rows.append(row)
        if self.rec is not None:
            self.rec.add(row)
            self._emit(event="point", run_id=self.rec_id, done=len(self.rows), total=None, row=row, eta_s=None,
                       columns=self._columns())
        return {}

    def _image(self, value: List[List[Any]], key: str, n: int) -> Any:
        """An image the experiment recorded, kept in the run's frames/ as a camera's frame is (frames.keep); as it came
        when the run has no folder, or there is no numpy to keep it with."""
        if self.rec is None or not self.rec.path:
            return value
        try:
            import numpy as np
        except ImportError:
            return value
        from . import frames
        folder = Path(self.rec.path)
        made = folder / ("recorded-%d.npy" % n)
        np.save(made, np.asarray(value, dtype=float))
        return frames.keep(made, folder, key, n)

    def _columns_named(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        axes, units = msg.get("axes"), msg.get("units")
        if axes is not None:
            self.axes = [str(a) for a in axes]
        if isinstance(units, dict):
            self.units.update((str(k), str(v)) for k, v in units.items())
        return {}

    # ---------------------------------------------------------------- kelvin.lab: instruments by name

    def _named(self, name: str) -> Any:
        """A lab instrument the experiment uses by name (kelvin.lab), as _open checks one the script opens."""
        if name not in self.lab.instruments:
            raise _Refuse("refused: the lab has no instrument %s" % name)
        away = self.lab.away(name)
        if away:
            raise _Refuse("refused: %s is %s" % (name, away))
        self.opened.setdefault(name, "kelvin.lab")
        return self.lab.instrument(name)

    @staticmethod
    def _key(msg: Dict[str, Any]) -> Tuple[str, str]:
        key = str(msg.get("key") or "")
        name, dot, rest = key.partition(".")
        if not dot or not name or not rest:
            raise _Refuse("refused: %r is not instrument.parameter" % key)
        return name, rest

    def _spec_of(self, key: str) -> Any:
        try:
            return self.lab.resolve(key)[1]
        except KeyError:
            return None

    def _get(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        name, param = self._key(msg)
        self._named(name)
        r = self.gate.get(name, param)
        self._log(instrument=name, key=r.target, action=r.action, value=_plain(r.value) if r.ok else None, ok=r.ok,
                  message=r.message)
        if not r.ok:
            raise _Refuse(r.message, stop=r.action in STOPS)
        return {"value": _plain(r.value)}

    def _set(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        name, param = self._key(msg)
        self._named(name)
        with self._quiet():
            r = self.gate.set(name, param, msg.get("value"), bool(msg.get("wait", True)))
        self._log(instrument=name, key=r.target, action=r.action, value=_plain(r.value), ok=r.ok, message=r.message)
        spec = self._spec_of(r.target)
        if r.ok:
            if r.action in ("set", "no-change") and spec is not None:
                if spec.is_drive:
                    self.touched[r.target] = None
                self._note_set(r.target, r.value)
                self._last = r.message
            return {"value": _plain(r.value)}
        raise _Refuse(r.message, stop=r.action in STOPS or (spec is not None and spec.is_drive))

    def _act(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        name, act = self._key(msg)
        inst = self._named(name)
        key = "%s.%s" % (name, act)
        with self._quiet():   # an action read ahead is approved with the run, but never while it stops
            r = self.gate.act(name, act, in_plan=key in self.prep.found.actions and not self.stopping)
        self._log(instrument=name, key=key, action=r.action, value=_plain(r.value), ok=r.ok, message=r.message)
        if not r.ok:
            raise _Refuse(r.message, stop=r.action in STOPS)
        card_action = inst.card.actions.get(act)
        if card_action is not None and card_action.drives:
            self.unparsed[name] = None   # a stopped or failed run sends it to its safe state
        return {"reply": _plain(r.value)}

    def handle(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        op = msg.get("op")
        if op == "get":
            return self._get(msg)
        if op == "set":
            return self._set(msg)
        if op == "act":
            return self._act(msg)
        if op == "raw":
            return self._command_to(self._named(str(msg.get("instrument") or "")), msg)
        if op == "live":
            return self._live(msg)
        if op == "sleep":   # a rehearsal's wait: the simulated lab's clock moves on, at once
            if not self.rehearsal:
                raise _Refuse("Kelvin's stand-in waits on the lab's clock only in a rehearsal", stop=False)
            self.lab.clock.sleep(max(0.0, float(msg.get("seconds") or 0)))
            return {}
        if op == "record":
            return self._record(msg)
        if op == "columns":
            return self._columns_named(msg)
        if op == "open":
            return self._open(str(msg.get("address") or ""), str(msg.get("kind") or "visa"))
        if op == "lookup":   # is this address a lab instrument? (pyvisa's resource_info); nothing opens
            return {"instrument": lab_name(self.lab, str(msg.get("address") or ""))}
        if op == "command":
            return self._command(msg)
        if op in ("read", "read_raw", "read_bytes"):
            inst = self._instrument(msg)
            if op == "read":
                return {"reply": self._read_back(inst, msg)}
            return {"raw": self._read_back(inst, msg, raw=True, count=int(msg.get("count") or 0))}
        if op == "query_binary":
            inst = self._instrument(msg)
            command = str(msg.get("command") or "")
            why = self._classify(inst, command)
            if why:
                raise _Refuse("refused: " + why)
            res = getattr(inst.backend, "res", None)
            if res is None:
                raise _Refuse("%s is simulated: it has no binary readings" % inst.name, stop=False)
            with self._patience(inst, msg.get("timeout")), inst.backend.lock:
                try:
                    values = res.query_binary_values(command, datatype=msg.get("datatype") or "f",
                                                     is_big_endian=bool(msg.get("is_big_endian")),
                                                     header_fmt=msg.get("header_fmt") or "ieee",
                                                     expect_termination=msg.get("expect_termination", True),
                                                     data_points=int(msg.get("data_points") or 0), container=list)
                except Exception as e:
                    raise _Refuse("%s: %s" % (inst.name, e), stop=False) from None
            self._log(instrument=inst.name, command=command, action="read", ok=True, message="%d values" % len(values))
            return {"values": [float(v) for v in values]}
        if op == "write_binary":
            inst = self._instrument(msg)
            command = str(msg.get("command") or "")
            why = self._classify(inst, command)
            if why:
                raise _Refuse("refused: " + why)
            res = getattr(inst.backend, "res", None)
            if res is not None:
                with inst.backend.lock:
                    try:
                        res.write_binary_values(command, list(msg.get("values") or []),
                                                datatype=msg.get("datatype") or "f",
                                                is_big_endian=bool(msg.get("is_big_endian")))
                    except Exception as e:
                        raise _Refuse("%s: %s" % (inst.name, e), stop=False) from None
            self._log(instrument=inst.name, command=command, action="sent", ok=True,
                      message="a block of %d values" % len(msg.get("values") or []))
            return {}
        if op == "clear":
            inst = self._instrument(msg)
            res = getattr(inst.backend, "res", None)
            if res is not None:
                with inst.backend.lock:
                    try:
                        res.clear()
                    except Exception as e:
                        raise _Refuse("%s: %s" % (inst.name, e), stop=False) from None
            return {}
        if op == "stb":
            inst = self._instrument(msg)
            res = getattr(inst.backend, "res", None)
            if res is not None:
                with inst.backend.lock:
                    return {"value": int(res.read_stb())}
            r = self.gate.raw(inst.name, "*STB?")
            return {"value": int(float(r.value)) if r.ok and r.value else 0}
        if op == "library":
            return self._library(str(msg.get("module") or ""))
        if op == "bye":
            self.bye = msg
            return {}
        raise _Refuse("Kelvin's stand-in asked for %r, which Kelvin does not know" % op, stop=False)

    def _ask(self, request: ApprovalRequest) -> bool:
        if self.stopping:
            return False
        return self.gate.ask(request)

    def _open(self, address: str, kind: str) -> Dict[str, Any]:
        name = lab_name(self.lab, address) if address else None
        if name is not None:
            away = self.lab.away(name)
            if away:
                raise _Refuse("refused: %s is %s" % (name, away))
            self.opened.setdefault(name, address)
            return {"instrument": name}
        if self.rehearsal:
            raise _Refuse("refused: a rehearsal reaches only the simulated lab, not %s" % address)
        if canonical(address) in self.allowed or kind == "socket" and not behind(self.lab, address):
            return {}   # a person allowed it, or a socket that reaches no lab instrument
        request = ApprovalRequest("Let %s open %s?" % (self.prep.name, address), [
            "%s opens %s, so it runs as the script wrote it" % (self.prep.name, outside(self.lab, address))])
        if not self._ask(request):
            raise _Refuse("declined: %s may not open %s%s" % (self.prep.name, address,
                                                               " (%s)" % request.note if request.note else ""))
        self.allowed.add(canonical(address))
        self.passed.append(address)
        return {}

    def _library(self, module: str) -> Dict[str, Any]:
        """A maker's library the reading missed (one another loads, as pylablib loads pyusb and nidaqmx, or an import
        the script builds): it drives nothing itself, and the run a person approved is what uses it, so Kelvin lets it
        through with the run, in the chat and the record, never waiting for anyone. Not while the run stops, and never
        in a rehearsal (nobody approved one)."""
        if self.rehearsal:
            raise _Refuse("refused: a rehearsal reaches only the simulated lab, so it may not use %s" % module)
        if module in self.allowed:
            return {}
        if self.stopping:
            raise _Refuse("declined: %s may not use %s as it stops" % (self.prep.name, module))
        request = ApprovalRequest("Let %s use %s?" % (self.prep.name, module), [
            "%s imports %s, which reaches instruments without Kelvin seeing the commands: the safety gate can't "
            "check what it does through it" % (self.prep.name, module)])
        self.gate.auto_approve(request, ["a maker's library the script loads on the way drives nothing itself; the "
                                         "person's yes to the run covers what the script does with it"],
                               "approved with the script's run")
        self.allowed.add(module)
        self.through.append(module)
        return {}

    # ---------------------------------------------------------------- the loop

    def _begin_stop(self, why: str, conns: Dict[socket.socket, _Conn]) -> None:
        if self.stopping:
            return
        self.stopping = why
        self.stop_at = time.monotonic()
        for c in conns.values():
            if c.ok:
                self._send(c, {"op": "stop", "why": why})

    @staticmethod
    def _send(conn: _Conn, msg: Dict[str, Any]) -> None:
        try:
            conn.sock.sendall((json.dumps(msg, default=str) + "\n").encode("utf-8"))
        except OSError:
            pass

    def _serve(self, conn: _Conn, line: bytes, conns: Dict[socket.socket, _Conn]) -> bool:
        """Answer one request; False: the connection is not the script's (no or a wrong token), drop it."""
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            return conn.ok
        rid = msg.get("id")
        if not conn.ok:
            if msg.get("op") != "hello" or not hmac.compare_digest(str(msg.get("token") or ""), self.token):
                return False
            conn.ok = True
            self._send(conn, {"id": rid, "ok": True, "opaque": list(OPAQUE), "run": self.rec_id})
            if self.stopping:
                self._send(conn, {"op": "stop", "why": self.stopping})
            return True
        try:
            answer = dict(self.handle(msg), id=rid, ok=True)
        except _Refuse as e:
            answer = {"id": rid, "ok": False, "error": str(e), "stop": e.stop}
            if e.stop:
                if not self.failed and not self.stopping:
                    self.failed = str(e)
                self._send(conn, answer)
                self._begin_stop(str(e), conns)
                return True
        except Exception as e:   # a command that failed on the way: the script gets the error, the run goes on
            answer = {"id": rid, "ok": False, "error": "%s: %s" % (type(e).__name__, e), "stop": False}
        self._send(conn, answer)
        if msg.get("op") not in ("sleep", "live"):   # waiting, or reading an input, is no clean-up after a stop:
            self.last_activity = time.monotonic()      # a script that only does that is ended once GRACE_S passes
        return True

    def _command_line(self, run_dir: Path, source_file: Optional[Path]) -> List[str]:
        line = list(self.prep.python.command) + [str(SHIM)]
        if source_file is not None:
            line += ["--source", str(source_file)]
        return line + [str(self.prep.path)] + self.prep.args

    def _environ(self, port: int, run_dir: Path) -> Dict[str, str]:
        from .envs import clean_environ
        env = clean_environ()
        env.update(KELVIN_IPC="127.0.0.1:%d" % port, KELVIN_IPC_TOKEN=self.token, KELVIN_RUN_DIR=str(run_dir),
                   KELVIN_SCRIPT=str(self.prep.path), KELVIN_ACTION=self.prep.action, MPLBACKEND="Agg",
                   PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        if self.rehearsal:   # its own folder for what Matplotlib keeps, and no bytecode written beside the lab's code
            env.update(KELVIN_REHEARSAL="1", PYTHONDONTWRITEBYTECODE="1", MPLCONFIGDIR=str(run_dir / ".matplotlib"))
        elif self.prep.fenced:   # one of the lab's kept experiments: kelvin.lab and nothing past it, in the real run too
            env.update(KELVIN_FENCE="1", PYTHONDONTWRITEBYTECODE="1", MPLCONFIGDIR=str(run_dir / ".matplotlib"))
        env["PYTHONPATH"] = os.pathsep.join([str(SHIM_DIR)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        return env

    def _files_now(self) -> Dict[str, float]:
        """The files in the script's folder (to see what it wrote), up to a few thousand, run folders left out."""
        out: Dict[str, float] = {}
        if self.rehearsal:   # it works in a folder of its own, thrown away after
            return out
        root = self.prep.path.parent
        own = os.path.normcase(str(self.rec.path)) if self.rec is not None and self.rec.path else None
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in lab_scripts.SKIP_DIRS and not d.startswith(".")
                           and os.path.normcase(os.path.join(dirpath, d)) != own]
            for f in filenames:
                p = os.path.join(dirpath, f)
                try:
                    out[os.path.relpath(p, root)] = os.path.getmtime(p)
                except OSError:
                    pass
                if len(out) > 5000:
                    return out
        return out

    def execute(self) -> ScriptReport:
        gate, lab, prep = self.gate, self.lab, self.prep
        plan = {"plan": prep.name, "script": str(prep.path), "sha256": prep.sha256, "python": prep.python.text,
                "changes": prep.changes or None, "params": prep.params or None, "args": prep.args or None,
                "version": prep.version, "action": prep.action or None}
        # script_sha256: the text that runs (script.py), kept in the record from its start, so a run again "as it
        # ran" runs exactly that text, or nothing; beside: the Python files beside it then, for the next card
        meta = dict({"lab": lab.config.name, "simulated": all(i.simulated for i in lab.instruments.values())},
                    **self.meta)
        meta.update(script=str(prep.path), script_sha256=fingerprint(prep.source), beside=prep.near)
        self.rec = self.store.create(prep.name, {k: v for k, v in plan.items() if v is not None}, gate.operator,
                                     self.folder, meta=meta)
        self.rec_id = self.rec.run_id
        run_dir = Path(self.rec.path) if self.rec.path else Path(tempfile.mkdtemp(prefix="kelvin-script-"))
        (run_dir / "script.py").write_text(prep.source, encoding="utf-8")
        # values set, a notebook, or an earlier run's version of the file: the text in script.py runs, not the file
        changed = prep.source != prep.original or prep.path.suffix.lower() != ".py" or prep.version is not None
        source_file = run_dir / "script.py" if changed else None
        start = lab.snapshot()
        self.rec.snapshot("start", start)
        gate.log.write("run_start", by=gate.operator, run=self.rec.run_id, plan=prep.name, points=0,
                       script=str(prep.path))
        self._emit(event="start", run_id=self.rec.run_id, plan=prep.name, total=None, axes=[], measures=[],
                   script=True)   # how many points it takes is the script's own business
        self.commands_file = open(run_dir / "commands.jsonl", "w", encoding="utf-8")
        output_path = run_dir / "output.txt"
        files_before = self._files_now()
        self.t0 = time.monotonic()
        self.last_activity = self.t0
        self.stop_at = 0.0
        gen = gate.stop_generation
        killed = False
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        server.setblocking(False)
        sel = selectors.DefaultSelector()
        sel.register(server, selectors.EVENT_READ)
        conns: Dict[socket.socket, _Conn] = {}
        out = open(output_path, "wb")
        job: Optional[_Job] = None
        approved = gate.preapproved(self.grants)   # what the person approved holds for the whole run
        approved.__enter__()
        try:
            try:
                proc = subprocess.Popen(self._command_line(run_dir, source_file), cwd=str(run_dir if self.rehearsal else prep.path.parent),
                                        env=self._environ(server.getsockname()[1], run_dir),
                                        stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                        start_new_session=os.name != "nt")
            except OSError as e:
                self.failed = "the script's Python did not start (%s)" % (e.strerror or e)
                proc = None
            if proc is not None:
                job = _Job(proc)
                self.busy("script %s: starting" % prep.name)
            while proc is not None:
                for key, _ in sel.select(timeout=0.2):
                    if key.fileobj is server:
                        try:
                            sock, _ = server.accept()
                        except OSError:
                            continue
                        sock.setblocking(True)
                        sock.settimeout(60)
                        conns[sock] = _Conn(sock)
                        sel.register(sock, selectors.EVENT_READ)
                        continue
                    conn = conns[key.fileobj]
                    try:
                        data = conn.sock.recv(65536)
                    except OSError:
                        data = b""
                    conn.buf += data
                    keep = bool(data)
                    while keep and b"\n" in conn.buf:
                        line, conn.buf = conn.buf.split(b"\n", 1)
                        keep = self._serve(conn, line, conns)
                    if not keep:   # it hung up, or it was not the script's
                        sel.unregister(conn.sock)
                        conns.pop(conn.sock, None)
                        conn.sock.close()
                if gate.stop_generation != gen and not self.stopping:
                    self._begin_stop("stop requested", conns)
                if proc.poll() is not None:
                    break
                if self.stopping:
                    now = time.monotonic()
                    if now - max(self.last_activity, self.stop_at) > GRACE_S or now - self.stop_at > MAX_GRACE_S:
                        killed = True
                        job.end()
                        proc.wait(timeout=30)
                        break
        finally:
            approved.__exit__(None, None, None)
            if job is not None:
                job.close()
            for c in list(conns.values()):
                c.sock.close()
            sel.close()
            server.close()
            out.close()
            self._flush()
            self.ended = True
            with self._log_lock:
                self.commands_file.close()
        duration = time.monotonic() - self.t0
        code = proc.returncode if proc is not None else None
        stopped_by_person = self.stopping == "stop requested" and not self.failed
        if self.failed:
            status, reason = "aborted", self.failed
        elif stopped_by_person:
            status, reason = "aborted", "stop requested"
        elif code not in (0, None):
            status = "aborted"
            reason = self.bye.get("error") or "it ended with exit code %s" % code
        else:
            status, reason = "done", None
        self.killed = killed
        if killed:
            reason = "%s; it did not end within %s of the stop, so Kelvin ended it" % (reason, _dur(GRACE_S))
        cleanup: List[str] = []
        if status != "done":
            self._emit(event="phase", run_id=self.rec.run_id, phase="stopping", eta_s=None)
            cleanup = self._back_to_safe()
        left = self._left_on() if status == "done" else []
        try:
            self.rec.snapshot("end", lab.snapshot())
        except Exception:
            pass
        output = self._tail(output_path)
        after = self._files_now()
        files = sorted(f for f, m in after.items() if files_before.get(f) != m)[:40]
        report = ScriptReport(status, "", self.rec.run_id, str(self.rec.path) if self.rec.path else str(run_dir),
                              self.rows, self._columns(), self.n_commands, output, files,
                              list(self.bye.get("figures") or []), left, cleanup, duration_s=duration)
        if self.passed:
            report.warnings.append("past the gate, as a person allowed: " + ", ".join(self.passed))
        if self.through:
            report.warnings.append("past the gate, let through with the run: " + ", ".join(self.through))
        if self.bye.get("refused") and not self.rehearsal:   # a kept experiment's fence: what it refused
            report.warnings.append("its process refused: " + "; ".join(str(r) for r in self.bye["refused"]))
        if self.reported:
            report.warnings.append("the instruments refused %d of its commands, as they would have without Kelvin "
                                   "(commands.jsonl says which and why)" % self.reported)
        self.rec.set_columns(report.columns)
        meta = dict(status=status, reason=reason, points=len(self.rows), duration_s=round(duration, 3),
                    cleanup=cleanup, warnings=report.warnings, exit_code=code, commands=self.n_commands,
                    files=files, figures=report.figures, sets=sorted(self.state))   # sets: what the script set
        if self.live_changes:
            meta["live"] = self.live_changes   # each change of a live input while it ran, and whose
        if self.rows and report.columns["axes"] and report.columns["measures"]:
            try:
                from .analysis import analyze_run
                report.analysis = analyze_run(self.rows, report.columns["axes"], report.columns["measures"], meta)
                self.rec.set_analysis(report.analysis)
            except Exception as e:
                report.warnings.append("automatic analysis failed: %s" % e)
        self.rec.finish(**meta)
        gate.log.write("run_end", by=gate.operator, run=self.rec.run_id, status=status, reason=reason,
                       points=len(self.rows), duration_s=round(duration, 3), script=str(prep.path),
                       uuid=self.rec.meta.get("uuid"), sha256=getattr(self.rec, "sha256", None))
        self._emit(event="end", run_id=self.rec.run_id, status=status, done=len(self.rows), total=None)
        if status == "done":
            report.message = "script %s done: %s, %d instrument commands through the gate" % (
                prep.name, _dur(duration), self.n_commands)
        else:
            report.message = "script %s stopped after %s: %s" % (prep.name, _dur(duration), reason)
            if cleanup:
                report.message += "; then " + "; ".join(cleanup)
        return report

    def _back_to_safe(self) -> List[str]:
        """Every drive the script touched goes back to safe (or holds, where the lab says so), as a stopped plan's
        do; an instrument it sent a drive command the gate couldn't read goes to its safe state."""
        out = []
        from .gate import paces
        for key in self.touched:
            if self.lab.config.safe_actions.get(key) == "hold":
                continue
            instr, _, param = key.partition(".")
            if paces(self.lab.instrument(instr).card, param) is not None:
                continue   # a ramp rate stays: at its safe 0 the drive it paces couldn't come back to safe
            spec = self.lab.instrument(instr).spec(param)
            r = self.gate._set(instr, param, spec.safe, True, "abort:" + self.prep.name, record_undo=False)
            if r.action != "no-change":
                out.append(r.message)
        for instr in self.unparsed:
            out += [r.message for r in self.gate.to_safe(instr)]
        return out

    def _left_on(self) -> List[str]:
        out = []
        for key in self.touched:
            try:
                inst, spec = self.lab.resolve(key)
                now = self.gate._current(inst, spec)
                if now is not None and spec.distance(now) > 0:
                    out.append("%s %s" % (key, spec.show(now)))
            except Exception:
                continue
        return out

    @staticmethod
    def _tail(path: Path) -> List[str]:
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 16384))
                text = f.read().decode("utf-8", "replace")
        except OSError:
            return []
        return [ln[:300] for ln in text.splitlines()[-OUTPUT_TAIL:]]


@dataclass
class Rehearsal:
    ok: bool                 # it ended (by itself, or on Stop) and nothing was refused
    ended: str               # "by itself" | "on Stop" | "failed" | "not on Stop"
    message: str
    lab_seconds: float       # the simulated lab's time it took
    problems: List[str] = field(default_factory=list)
    asked: List[str] = field(default_factory=list)            # what a person would be asked in the real run
    commands: List[Dict[str, Any]] = field(default_factory=list)   # its commands.jsonl entries, in order
    rows: List[Dict[str, Any]] = field(default_factory=list)
    columns: Dict[str, Any] = field(default_factory=dict)


ENDED = {"by itself": "it ended by itself", "on Stop": "it ended on Stop", "failed": "it failed",
         "not on Stop": "it did not end on Stop"}


def _reaches_past(source: str) -> Optional[str]:
    """What in this Python reaches past the simulated lab, in words, or None: a module of PAST_THE_LAB it imports (the
    standard library's too), or a program it starts through os."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    aliases = {"os"}
    found: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.partition(".")[0]
                if top in PAST_THE_LAB:
                    found.append((node.lineno, "it imports %s, which reaches past the simulated lab" % top))
                elif a.name == "os":
                    aliases.add(a.asname or "os")
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            top = node.module.partition(".")[0]
            if top in PAST_THE_LAB:
                found.append((node.lineno, "it imports %s, which reaches past the simulated lab" % top))
            elif node.module == "os":
                for a in node.names:
                    if a.name in PROGRAMS or a.name.startswith(("spawn", "exec")):
                        found.append((node.lineno, "it calls os.%s, which starts a program" % a.name))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                isinstance(node.func.value, ast.Name) and node.func.value.id in aliases and \
                (node.func.attr in PROGRAMS or node.func.attr.startswith(("spawn", "exec"))):
            found.append((node.lineno, "it calls os.%s, which starts a program" % node.func.attr))
    return min(found)[1] if found else None


def rehearsal_problem(prep: Prepared) -> Optional[str]:
    """Why Kelvin won't rehearse this experiment, in words; None when it may. A rehearsal is approved by nobody, so
    it runs only an experiment whose code, and the lab's own modules it imports, reach no further than kelvin.lab and
    the lab's instruments through Kelvin, in a Python that can fence it (3.8 or later: audit hooks)."""
    if tuple(prep.python.version[:2]) < (3, 8):
        return ("its Python is %d.%d, and a rehearsal needs Python 3.8 or later to keep it to the simulated lab"
                % tuple(prep.python.version[:2]))
    if prep.found.libraries:
        return "it uses %s, which reaches instruments past the gate" % ", ".join(prep.found.libraries)
    texts = [prep.source]
    for m in _local_modules(prep.path, prep.source):
        for f in (sorted(m.rglob("*.py")) if m.is_dir() else [m]):
            try:
                texts.append(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    for text in texts:
        why = _reaches_past(text)
        if why:
            return why
    return None


def rehearse(lab: Any, prep: Prepared, seconds: float = REHEARSE_S, envelope: Any = None,
             twin: Any = None, real: Any = None, wire: Optional[List[Any]] = None) -> Rehearsal:
    """An experiment run on a simulated copy of the lab (lab.make_twin), as it would run on the lab: every command
    through the twin's gate, which says yes to whatever a person would be asked (and lists it), its waits skipped on the
    simulated clock, nothing outside the simulated lab, and nothing written beside the script. Kelvin presses Stop on
    it after `seconds`: an experiment that then ends counts as one that ends. ScriptError: it can't be rehearsed (a
    script that doesn't use kelvin.lab may reach what the simulated lab can't stand in for; rehearsal_problem). twin:
    the simulated copy, made by the caller while it holds the lab (making one reads every instrument); else made here.
    Nothing it runs reaches past the simulated lab: its process refuses programs, native libraries, COM, the network
    and writing outside its own folder (kelvin_shim._fence). (No job object stops it starting a process, as run_python's
    has: a venv's python.exe on Windows starts the real Python as a second process.) real: the lab's own gate, whose
    Stop (a person's, or the agent's stop_run) stops the rehearsal too. wire: a list that gets each (instrument,
    command) the simulated instruments are sent, in order (vicheck compares it with a plan's)."""
    if not prep.found.experiment:
        raise ScriptError("%s doesn't use kelvin.lab, so Kelvin can't rehearse it: it may reach what the simulated lab "
                          "can't stand in for" % prep.name)
    why = rehearsal_problem(prep)
    if why:
        raise ScriptError("%s can't be rehearsed: %s" % (prep.name, why))
    from .eventlog import EventLog
    from .gate import SimulationApprover
    twin = twin if twin is not None else lab.make_twin()
    if wire is not None:
        for name, inst in twin.instruments.items():
            def recorded(command: str, _write: Any = inst.backend.write, _name: str = name) -> Any:
                wire.append((_name, command))
                return _write(command)
            inst.backend.write = recorded
    gate = SafetyGate(twin, SimulationApprover(), EventLog(None, twin.clock), envelope=envelope, operator="dry-run")
    _, grants, problems = approval(gate, prep)
    if problems:
        return Rehearsal(False, "failed", "rehearsal on the simulated lab: %s before it started" % ENDED["failed"],
                         0.0, problems)
    job = ScriptRun(gate, prep, RunStore(None), grants, set(), rehearsal=True)
    t0 = twin.clock.now()
    timer = threading.Timer(seconds, gate.request_abort, args=("the rehearsal's time ran out",))
    timer.daemon = True
    timer.start()
    watching = threading.Event()
    if real is not None:   # a person's Stop on the lab stops its rehearsal too
        stops = real.stop_generation

        def watch() -> None:
            while not watching.wait(0.1):
                if real.stop_generation != stops:
                    gate.request_abort("a person pressed Stop")
                    return
        threading.Thread(target=watch, name="kelvin-rehearsal-stop", daemon=True).start()
    try:
        report = job.execute()
    finally:
        timer.cancel()
        watching.set()
    lab_seconds = twin.clock.now() - t0
    commands: List[Dict[str, Any]] = []
    try:
        with open(Path(report.path) / "commands.jsonl", encoding="utf-8") as f:
            commands = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        pass
    finally:
        shutil.rmtree(report.path, ignore_errors=True)   # its own folder, made for it (RunStore(None))
    problems = []
    if report.status == "done":
        ended = "by itself"
    elif job.failed:
        ended, problems = "failed", [job.failed]
    elif job.killed:
        ended, problems = "not on Stop", ["it did not end within %s of Stop" % _dur(GRACE_S)]
    elif job.stopping == "stop requested":
        ended = "on Stop"
    else:
        ended, problems = "failed", [job.bye.get("error") or report.message]
    for refused in job.bye.get("refused") or []:   # what its process refused, also when the experiment carried on
        line = "its process refused: %s" % refused
        if line not in problems:
            problems.append(line)
    message = "rehearsal on the simulated lab: %s after %s of lab time, %d commands, %d rows" % (
        ENDED[ended], _dur(lab_seconds), len(commands), len(report.rows))
    return Rehearsal(ended in ("by itself", "on Stop") and not problems, ended, message, lab_seconds, problems,
                     [str(r) for r in gate.approver.requests], commands, report.rows, report.columns)


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


TRUE = ("true", "on", "yes", "1")
FALSE = ("false", "off", "no", "0")


def _live_value(name: str, now: Any, value: Any) -> Any:
    """A live input's new value, as the type of its value now. ValueError: one it can't take, in words."""
    if isinstance(now, bool):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in TRUE or text in FALSE:
            return text in TRUE
        raise ValueError("%s takes true or false, not %r" % (name, value))
    if isinstance(now, int):
        try:
            x = float(value)
        except (TypeError, ValueError):
            x = None
        if x is None or isinstance(value, bool) or not math.isfinite(x) or x != int(x):
            raise ValueError("%s takes a whole number, not %r" % (name, value))
        return int(x)
    if isinstance(now, float):
        try:
            if isinstance(value, bool) or not math.isfinite(float(value)):
                raise ValueError   # nan and inf are no set points
            return float(value)
        except (TypeError, ValueError):
            raise ValueError("%s takes a number, not %r" % (name, value)) from None
    if isinstance(now, str):
        return str(value)
    return value


def _plain(value: Any) -> Any:
    """A value as it goes to the script: numpy's numbers and arrays as Python's own, tuples as lists."""
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    tolist = getattr(value, "tolist", None)
    if callable(tolist) and not isinstance(value, (str, bytes)):
        return _plain(tolist())
    return value


class _Refuse(Exception):
    """A request the script's stand-in gets an error for; stop: the run stops (a refusal, a person's no)."""

    def __init__(self, message: str, stop: bool = True):
        super().__init__(message)
        self.stop = stop
