"""run_python's sandbox: a piece of Python, the AI's or the person's, run on runs' data in a process of its own.

What it does:
- a fresh scratch folder for each call (the data folder's python/<date-time>/): the code's working folder and the only
  place it may write; what it saves there stays for the person;
- a process of its own, with Kelvin's Python and packages (NumPy and h5py; SciPy, lmfit and Matplotlib when they are
  installed: the app brings none of the three) and none of Kelvin's API keys or sign-ins in its environment;
- killed at its time limit, at a person's Stop, or when the MCP client cancels the call;
- on Windows, inside a job object: at most MEMORY_MB of memory, and no other process can be started from it;
- inside the process, before the code runs, an audit hook (PEP 578, data/sandbox/kelvin_sandbox.py) that refuses:
  writing, renaming or deleting anything outside the scratch folder; reading files other than the scratch folder's,
  the runs it was given and Python's own; network connections; starting programs; native libraries (ctypes); the
  Windows registry; and instrument packages (pyvisa, serial, usb...). h5py opens nothing outside the scratch folder
  for writing, and reads only those runs.

What it does not stop, said plainly: the hook runs inside Python, in the same process as the code it guards, and
Python's own documentation says audit hooks are not a sandbox. Code written on purpose to get around them (through a
bug in a native extension, say) can, and the process runs as the person, with their files' permissions. It keeps
honest code, and code an AI was talked into by text it read, away from the instruments, the lab's records and
Kelvin's settings; it is not a wall against a determined attacker. Within its time limit it may use the CPU fully
(it runs at low priority), and outside Windows its memory is limited only where the system allows it."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

RUNNER = Path(__file__).parent / "data" / "sandbox" / "kelvin_sandbox.py"
TIME_LIMIT = 60.0          # seconds, unless the call asks for another
MAX_TIME = 600.0
MEMORY_MB = 4096
KEEP_OUTPUT = 400_000      # bytes of what it prints, kept
SHOWN = 20_000             # characters of it in the tool's answer (its beginning and its end)
MAX_IMAGES = 8
MAX_IMAGE_BYTES = 8_000_000
INTERNAL = {"code.py", "job.json", "result.json"}


@dataclass
class Outcome:
    status: str                       # ok, error, timeout, stopped, failed
    seconds: float
    folder: Path
    output: str = ""
    images: List[bytes] = field(default_factory=list)
    files: List[Tuple[str, int]] = field(default_factory=list)
    available: Dict[str, Optional[str]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def text(self) -> str:
        head = {"ok": "finished", "error": "stopped with an error (the traceback is in the output)",
                "timeout": "stopped at its time limit", "stopped": "stopped (Stop, or the call was cancelled)",
                "failed": "could not run"}.get(self.status, self.status)
        lines = ["run_python %s after %.2f s; its folder: %s" % (head, self.seconds, self.folder)]
        out = self.output
        if len(out) > SHOWN:
            out = out[:SHOWN * 3 // 4] + "\n… (%d characters left out) …\n" % (len(out) - SHOWN) + out[-SHOWN // 4:]
        lines.append("output:\n" + out.rstrip() if out.strip() else "output: (nothing printed)")
        if self.files:
            lines.append("files it saved: " + ", ".join("%s (%s)" % (n, _size(s)) for n, s in self.files[:30]) +
                         (" and %d more" % (len(self.files) - 30) if len(self.files) > 30 else ""))
        if self.images:
            lines.append("%d picture%s shown" % (len(self.images), "" if len(self.images) == 1 else "s"))
        have = [("%s %s" % (k, v)).strip() for k, v in self.available.items() if v is not None]
        missing = [k for k, v in self.available.items() if v is None]
        if have or missing:
            lines.append("in this Python: %s%s" % (", ".join(have) or "nothing extra",
                                                    "; not installed: %s" % ", ".join(missing) if missing else ""))
        lines += self.notes
        return "\n".join(lines)


def _size(n: int) -> str:
    return "%d bytes" % n if n < 1024 else "%.1f kB" % (n / 1024) if n < 1024 ** 2 else "%.1f MB" % (n / 1024 ** 2)


def command(job: Path) -> List[str]:
    """How to start the runner: with Kelvin's own Python (the app runs scripts with --bridge, as library.py's
    helper)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--bridge", str(RUNNER), str(job)]
    # a venv's python.exe on Windows only starts the real one as a second process, which the job object forbids;
    # the runner gets this Kelvin's import path from the job instead
    python = getattr(sys, "_base_executable", "") or sys.executable
    return [python if Path(python).is_file() else sys.executable, "-E", "-s", "-u", str(RUNNER), str(job)]


def _scratch(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for n in range(1, 1000):
        folder = base / (stamp if n == 1 else "%s-%d" % (stamp, n))
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            continue
    raise OSError("no free scratch folder in %s" % base)


def _contain(proc: subprocess.Popen, memory_mb: int) -> Tuple[Any, str]:
    """Windows: the process into a job object with a memory limit, no child processes, and killed with the job.
    (handle, '' or why not)."""
    if os.name != "nt":
        return None, ""
    try:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Io(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",
                                                         "OtherOperationCount", "ReadTransferCount",
                                                         "WriteTransferCount", "OtherTransferCount")]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None, "no job object (%d)" % ctypes.get_last_error()
        info = Extended()
        # process memory, one process only, killed when Kelvin lets go of the job
        info.BasicLimitInformation.LimitFlags = 0x100 | 0x8 | 0x2000
        info.BasicLimitInformation.ActiveProcessLimit = 1
        info.ProcessMemoryLimit = int(memory_mb) * 1024 * 1024
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) or \
                not k32.AssignProcessToJobObject(job, int(proc._handle)):   # type: ignore[attr-defined]
            err = ctypes.get_last_error()
            k32.CloseHandle(job)
            return None, "the memory limit could not be set (%d)" % err
        return (k32, job), ""
    except Exception as e:   # no ctypes, an older Windows: the time limit still holds
        return None, "the memory limit could not be set (%s)" % e


def _limits(memory_mb: int) -> Optional[Callable[[], None]]:
    """POSIX: the child's memory and core-dump limits, set before it starts."""
    if os.name == "nt":
        return None

    def apply() -> None:
        try:
            import resource
            limit = int(memory_mb) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except (ImportError, ValueError, OSError):
            pass
    return apply


def run(code: str, runs: Dict[str, Path], base: Path, timeout_s: float = TIME_LIMIT,
        stopped: Optional[Callable[[], bool]] = None, memory_mb: int = MEMORY_MB) -> Outcome:
    """Run code with runs' data readable (run id -> its folder); its files go to a new folder in base."""
    from . import rundata
    from .envs import clean_environ
    t0 = time.monotonic()
    timeout = max(1.0, min(float(timeout_s or TIME_LIMIT), MAX_TIME))
    folder = _scratch(Path(base))
    out = Outcome("failed", 0.0, folder)
    if not str(code or "").strip():
        out.output = "no code given"
        return out
    (folder / "code.py").write_text(code, encoding="utf-8")
    given: Dict[str, Dict[str, Any]] = {}
    for run_id, run_dir in runs.items():
        run_dir = Path(run_dir)
        h5 = run_dir / rundata.RUN_FILE
        item: Dict[str, Any] = {"folder": str(run_dir)}
        if h5.exists() and rundata.have_h5() and not rundata.being_recorded(run_dir):
            item["h5"] = str(h5)
        else:   # still being measured (or no h5py): a snapshot of its points, in the scratch folder
            from .export import write_npz
            (folder / "runs").mkdir(exist_ok=True)
            item["npz"] = str(write_npz(run_dir, path=folder / "runs" / (run_id + ".npz")))
            out.notes.append("%s is still being measured: the code read a snapshot of its points so far" % run_id)
        given[run_id] = item
    job = {"code": str(folder / "code.py"), "scratch": str(folder), "runs": given, "path": list(sys.path),
           "result": str(folder / "result.json")}
    (folder / "job.json").write_text(json.dumps(job), encoding="utf-8")
    env = clean_environ()
    for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT"):
        env.pop(k, None)
    env.update({"MPLBACKEND": "Agg", "MPLCONFIGDIR": str(folder / ".matplotlib"), "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1"})
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    try:
        proc = subprocess.Popen(command(folder / "job.json"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, cwd=str(folder), env=env, creationflags=flags,
                                preexec_fn=_limits(memory_mb))
    except OSError as e:
        out.output = "could not start Python: %s" % e
        return out
    job_handle, why = _contain(proc, memory_mb)
    if why:
        out.notes.append("note: %s; the time limit still holds" % why)
    kept = bytearray()
    tail = bytearray()

    def read() -> None:
        assert proc.stdout is not None
        for chunk in iter(lambda: proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else  # type: ignore
                          proc.stdout.read(65536), b""):
            if len(kept) < KEEP_OUTPUT:
                kept.extend(chunk[:KEEP_OUTPUT - len(kept)])
            tail.extend(chunk)
            del tail[:-SHOWN * 4]
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        assert proc.stdin is not None
        proc.stdin.write(b"go\n")   # the code runs only now, inside the job object
        proc.stdin.close()
    except OSError:
        pass
    status = None
    while proc.poll() is None:
        if time.monotonic() - t0 > timeout:
            status = "timeout"
        elif stopped is not None and stopped():
            status = "stopped"
        if status:
            proc.kill()
            break
        time.sleep(0.05)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    reader.join(timeout=5)
    if job_handle is not None:
        k32, job = job_handle
        k32.CloseHandle(job)
    out.seconds = time.monotonic() - t0
    text = bytes(kept).decode("utf-8", "replace")
    if len(tail) > 0 and len(kept) >= KEEP_OUTPUT:
        text += "\n… (more output, %s kept) …\n" % _size(KEEP_OUTPUT) + bytes(tail).decode("utf-8", "replace")
    out.output = text
    try:
        result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result = {}
    out.available = result.get("available") or {}
    if status:
        out.status = status
        out.output += "\n(stopped after %.1f s%s)" % (out.seconds, ": the time limit is %g s" % timeout
                                                     if status == "timeout" else "")
    elif result.get("status") in ("ok", "error"):
        out.status = result["status"]
    else:
        out.status = "error"
        if proc.returncode not in (0, None):
            out.output += "\n(Python ended with code %s%s)" % (proc.returncode, ": out of memory?" if os.name == "nt"
                                                               and proc.returncode in (-1073741801, 3221225495)
                                                               else "")
    _collect(folder, out, t0)
    return out


def _collect(folder: Path, out: Outcome, since: float) -> None:
    """The files the code left in its folder, and its pictures (PNG) to show."""
    files: List[Tuple[Path, int]] = []
    for p in sorted(folder.rglob("*")):
        rel = p.relative_to(folder)
        if not p.is_file() or rel.parts[0] in (".matplotlib", "runs") or str(rel) in INTERNAL:
            continue
        files.append((p, p.stat().st_size))
    out.files = [(str(p.relative_to(folder)).replace("\\", "/"), s) for p, s in files]
    total = 0
    for p, s in sorted(files, key=lambda f: f[0].stat().st_mtime):
        if p.suffix.lower() != ".png" or len(out.images) >= MAX_IMAGES or total + s > MAX_IMAGE_BYTES:
            continue
        data = p.read_bytes()
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            out.images.append(data)
            total += s
