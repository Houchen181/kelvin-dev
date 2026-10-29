"""A lab's own scripts, run as they are, with every instrument command through Kelvin's safety gate.

Many labs run their instruments from their own Python scripts and notebooks. Kelvin runs one from the lab's scripts
folder (the one a person named in Set up) in the lab's own Python, with its stand-in for the lab's instruments loaded
first (data/scriptshim/kelvin_shim.py): pyvisa, pyserial or a socket to a lab instrument reaches Kelvin instead of the
instrument, and Kelvin passes each command through the safety gate on its own connection, as if the agent had sent
it. So a script gets what a plan gets:

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
import os
import re
import secrets
import selectors
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from . import prologix
from . import scripts as lab_scripts
from .adapters import resource_name
from .eventlog import RunStore
from .gate import ApprovalRequest, Approver, SafetyGate, _dur
from .labconfig import Allowed
from .scpi import is_query, split_compound

SHIM_DIR = Path(__file__).parent / "data" / "scriptshim"
SHIM = SHIM_DIR / "kelvin_shim.py"
RUNNABLE = (".py", ".ipynb")
# makers' libraries that reach instruments through their own protocols or DLLs, past the gate: a person allows each
# one for the run (Kelvin's built-in drivers use some of these too, on its own connections)
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


def locate(path: str, folders: Sequence[Any]) -> Path:
    """The script to run, inside one of the lab's scripts folders (a person named them), never elsewhere. A bare file
    name may be in a sub-folder, if it is there once."""
    roots = [Path(f).expanduser().resolve() for f in folders if f]
    if not roots:
        raise ScriptError("this lab has no scripts folder: a person names it in Set up (Lab → Set up → From your "
                          "scripts…), and Kelvin runs only the scripts in it")
    p = Path(str(path).strip().strip('"')).expanduser()
    tries = [p] if p.is_absolute() else [r / p for r in roots]
    for t in tries:
        t = t.resolve()
        if t.is_file() and any(_inside(t, r) for r in roots):
            return _runnable(t)
    if p.is_absolute():
        if p.exists():
            raise ScriptError("%s is outside the lab's scripts folder (%s): Kelvin runs only the scripts a person put "
                              "there" % (p, ", ".join(str(r) for r in roots)))
        raise ScriptError("%s does not exist" % p)
    if len(p.parts) == 1:
        hits = [(r, h) for r in roots for h in _named(r, p.name)]
        if len(hits) == 1:
            return _runnable(hits[0][1].resolve())
        if hits:
            raise ScriptError("there are several %s in the scripts folder: %s; say which" % (
                p.name, ", ".join(str(h.relative_to(r)) for r, h in hits)))
    raise ScriptError("no script %s in the lab's scripts folder (%s)" % (path, ", ".join(str(r) for r in roots)))


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
    if path.suffix.lower() not in RUNNABLE:
        raise ScriptError("%s is not a Python script or notebook (.py, .ipynb)" % path.name)
    return path


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


def python_for(script: Path, source: str, roots: Sequence[Path], configured: str = "") -> Python:
    """The Python that runs the script: the one a person set for the lab's scripts; else the first, best first (an
    environment beside the scripts, this computer's, Kelvin's own), that has every module the script imports."""
    wanted = imports(source)
    if configured:
        p = Path(configured).expanduser()
        candidates = [([str(p), "-u"], "%s (set for the lab's scripts)" % p)]
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


def read_ahead(lab: Any, path: Path, source: str) -> Found:
    """What a script will do, read from its text and the lab's own modules it imports (scripts.py; nothing is run):
    the lab instruments it opens, the drive values it sets, and what Kelvin can't check."""
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
            if c.query:
                continue
            for part in split_compound(c.text) or [c.text]:
                spec, _ = _setter(inst.card, _PLACE.sub(lambda m: lab_scripts._num(
                    (c.values.get(m.group(1)) or [1])[0]), part))
                if spec is None or not spec.is_drive:
                    continue
                key = "%s.%s" % (name, spec.name)
                values = _part_values(spec, part, c.values)
                if values is None:
                    found.open.setdefault(key, "set from values the script works out as it runs")
                else:
                    found.drives.setdefault(key, [])
                    found.drives[key] += [v for v in values if v not in found.drives[key]]
    return found


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
    sha256: str
    declared: Dict[str, List[Any]] = field(default_factory=dict)   # drive ranges the agent gave for this run

    @property
    def name(self) -> str:
        return self.path.name

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
            python: str = "", choose_python: Callable[..., Python] = python_for) -> Prepared:
    script = locate(path, folders)
    original = source_of(script)
    source, changes = with_params(original, dict(params or {}))
    try:
        compile(source, str(script), "exec")
    except SyntaxError as e:
        raise ScriptError("%s does not parse as Python 3 (line %s: %s)" % (script.name, e.lineno, e.msg)) from None
    roots = [Path(f).expanduser().resolve() for f in folders if f]
    py = choose_python(script, source, roots, python)
    return Prepared(script, original, source, changes, [str(a) for a in (args or [])], py,
                    read_ahead(lab, script, source), hashlib.sha256(original.encode("utf-8")).hexdigest(),
                    declared_drives(lab, drives))


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
    if prep.changes:
        head += "; with " + "; ".join(prep.changes)
    if prep.args:
        head += "; arguments: " + " ".join(prep.args)
    lines = [head]
    if found.instruments:
        lines.append("It opens %s; every command goes through the safety gate as it is sent" % ", ".join(
            "%s (%s)" % (n, a) for n, a in found.instruments.items()))
    lines += items + open_lines
    lines += ["It also opens " + outside(lab, a) for a in found.unknown]
    lines += ["It uses %s, which reaches instruments without Kelvin seeing the commands" % m for m in found.libraries]
    lines += found.notes
    lines.append("A script can't be rehearsed on the simulated lab, so a person approves each run; anything beyond "
                 "this asks again, or is refused")
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
                 busy: Optional[Callable[[str], None]] = None):
        self.gate, self.lab, self.prep = gate, gate.lab, prep
        self.store, self.grants, self.meta, self.folder = store, grants, dict(meta or {}), folder
        # unknown addresses a person allowed, as VISA resources however they are spelled (the approval's serial://COM5
        # is the script's COM5 or ASRL5::INSTR), and makers' libraries, by name
        self.allowed = {a if a in OPAQUE else canonical(a) for a in allowed}
        self.busy = busy or (lambda text: None)
        self.token = secrets.token_hex(16)
        self.stopping: Optional[str] = None  # why the run is stopping
        self.failed: Optional[str] = None    # the gate's refusal (or a person's no) that stopped it
        self.touched: Dict[str, None] = {}   # drives it set, in order
        self.unparsed: Dict[str, None] = {}  # instruments a drive command went to that the gate couldn't read
        self.opened: Dict[str, str] = {}     # lab instrument -> the address the script opened it at
        self.passed: List[str] = []          # what it opened past the gate (a person allowed it)
        self.bye: Dict[str, Any] = {}
        self.n_commands = 0
        self.state: Dict[str, Any] = {}      # what the script set, as it is now
        self.reading: Dict[str, Any] = {}    # readings since its last change
        self.read_keys: Dict[str, None] = {}
        self.rows: List[Dict[str, Any]] = []
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
        if not self.reading:
            return
        row = {"i": len(self.rows), "t": self.reading.pop("_t")}
        row.update((k, v) for k, v in self.state.items() if _number(v))
        row.update(self.reading)
        self.rows.append(row)
        if self.rec is not None:
            self.rec.add(row)
        self.reading = {}

    def _note_set(self, key: str, value: Any) -> None:
        if key in self.state and self.state[key] == value:
            return
        self._flush()
        self.state[key] = value

    def _note_read(self, inst: Any, command: str, reply: str) -> None:
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
        def unit(key: str) -> str:
            try:
                return self.lab.resolve(key)[1].unit
            except KeyError:
                return ""
        changed = [k for k in self.state if len({repr(r.get(k)) for r in self.rows if k in r}) > 1]
        return {"axes": [{"key": k, "unit": unit(k), "kind": "instrument"} for k in changed],
                "measures": [{"key": k, "unit": unit(k), "kind": "scalar"} for k in self.read_keys]}

    def _log(self, **entry: Any) -> None:
        self.n_commands += 1
        if self.commands_file is not None:
            entry["t"] = round(time.monotonic() - self.t0, 4)
            self.commands_file.write(json.dumps(entry, default=str) + "\n")
        now = time.monotonic()
        if now - self._shown > 0.5:
            self._shown = now
            self.busy("script %s: %d commands%s" % (self.prep.name, self.n_commands,
                                                   ", " + self._last if self._last else ""))

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
        inst = self._instrument(msg)
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

    def handle(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        op = msg.get("op")
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
        if module in self.allowed:
            return {}
        request = ApprovalRequest("Let %s use %s?" % (self.prep.name, module), [
            "%s imports %s, which reaches instruments without Kelvin seeing the commands: the safety gate can't "
            "check what it does through it" % (self.prep.name, module)])
        if not self._ask(request):
            raise _Refuse("declined: %s may not use %s%s" % (self.prep.name, module,
                                                              " (%s)" % request.note if request.note else ""))
        self.allowed.add(module)
        self.passed.append(module)
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
        self.last_activity = time.monotonic()
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
                   KELVIN_SCRIPT=str(self.prep.path), MPLBACKEND="Agg", PYTHONUNBUFFERED="1",
                   PYTHONIOENCODING="utf-8")
        env["PYTHONPATH"] = os.pathsep.join([str(SHIM_DIR)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        return env

    def _files_now(self) -> Dict[str, float]:
        """The files in the script's folder (to see what it wrote), up to a few thousand, run folders left out."""
        out: Dict[str, float] = {}
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
                "changes": prep.changes or None, "args": prep.args or None}
        self.rec = self.store.create(prep.name, {k: v for k, v in plan.items() if v is not None}, gate.operator,
                                     self.folder)
        self.rec_id = self.rec.run_id
        run_dir = Path(self.rec.path) if self.rec.path else Path(tempfile.mkdtemp(prefix="kelvin-script-"))
        self.rec.meta["lab"] = lab.config.name
        self.rec.meta["simulated"] = all(i.simulated for i in lab.instruments.values())
        self.rec.meta.update(self.meta)
        self.rec.meta["script"] = str(prep.path)
        (run_dir / "script.py").write_text(prep.source, encoding="utf-8")
        changed = prep.source != prep.original or prep.path.suffix.lower() != ".py"   # values set, or a notebook
        source_file = run_dir / "script.py" if changed else None
        start = lab.snapshot()
        self.rec.snapshot("start", start)
        gate.log.write("run_start", by=gate.operator, run=self.rec.run_id, plan=prep.name, points=0,
                       script=str(prep.path))
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
                proc = subprocess.Popen(self._command_line(run_dir, source_file), cwd=str(prep.path.parent),
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
        if killed:
            reason = "%s; it did not end within %s of the stop, so Kelvin ended it" % (reason, _dur(GRACE_S))
        cleanup: List[str] = []
        if status != "done":
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
        if self.reported:
            report.warnings.append("the instruments refused %d of its commands, as they would have without Kelvin "
                                   "(commands.jsonl says which and why)" % self.reported)
        self.rec.set_columns(report.columns)
        meta = dict(status=status, reason=reason, points=len(self.rows), duration_s=round(duration, 3),
                    cleanup=cleanup, warnings=report.warnings, exit_code=code, commands=self.n_commands,
                    files=files, figures=report.figures)
        if self.rows and report.columns["axes"] and report.columns["measures"]:
            try:
                from .analysis import analyze_run
                report.analysis = analyze_run(self.rows, report.columns["axes"], report.columns["measures"], meta)
                self.rec.set_analysis(report.analysis)
            except Exception as e:
                report.warnings.append("automatic analysis failed: %s" % e)
        self.rec.finish(**meta)
        gate.log.write("run_end", by=gate.operator, run=self.rec.run_id, status=status, reason=reason,
                       points=len(self.rows), duration_s=round(duration, 3), script=str(prep.path))
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
        for key in self.touched:
            if self.lab.config.safe_actions.get(key) == "hold":
                continue
            instr, _, param = key.partition(".")
            spec = self.lab.instrument(instr).spec(param)
            r = self.gate._set(instr, param, spec.safe, True, "abort:" + self.prep.name)
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


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class _Refuse(Exception):
    """A request the script's stand-in gets an error for; stop: the run stops (a refusal, a person's no)."""

    def __init__(self, message: str, stop: bool = True):
        super().__init__(message)
        self.stop = stop
