"""The lab's LabVIEW files (VIs, VI libraries) read in a process of their own, so no file can hang Kelvin or take its
memory: labview.survey reads every VI this way (Set up's "From your LabVIEW…", keeping its scans, survey_labview).

How:
- one process for a survey, started the way run_python's sandbox starts one (sandbox.py): Kelvin's own Python (the
  app runs the script with --bridge), none of Kelvin's API keys or sign-ins in its environment, at most MEMORY_MB of
  memory (a job object on Windows, its own limit elsewhere where the system allows it), at low priority;
- it reads the files one at a time (vigraph.summary; each VI of a library, vigraph.each_summary), and says each
  summary on a line of its own as it is read: nothing is ever run, and it only reads;
- a file it says nothing about for FILE_SECONDS took too long to read: the process is stopped, the file's summary says
  so, and a new process reads the files after it; so does one the process stopped on (out of memory, say);
- the whole survey stops at SURVEY_SECONDS: what was read is kept, the files not reached say so, and so does a note.
Nothing here raises for a file: each one's summary says what happened to it."""
from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

RUNNER = Path(__file__).parent / "data" / "labview" / "kelvin_labview.py"
FILE_SECONDS = 30.0        # one file (a VI, or one VI of a library): the lab's own read in well under a second
SURVEY_SECONDS = 600.0     # all of one survey's files (hundreds of VIs take some seconds)
MEMORY_MB = 2048           # the reading process's memory
MAX_LINE = 16 * 2 ** 20    # one summary, as JSON (a lab's VI's is some kilobytes)


def command() -> List[str]:
    """How to start the reader: with Kelvin's own Python (the app runs scripts with --bridge, as sandbox.py)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--bridge", str(RUNNER)]
    # a venv's python.exe on Windows only starts the real one as a second process, which the job object forbids;
    # the runner gets this Kelvin's import path from the job instead
    python = getattr(sys, "_base_executable", "") or sys.executable
    return [python if Path(python).is_file() else sys.executable, "-E", "-s", "-u", str(RUNNER)]


def _not_read(path: str, why: str) -> Dict[str, Any]:
    from .labview import vi
    return vi(path, read="no", unread=["not read: %s" % why])


Summaries = List[List[Dict[str, Any]]]


def read_all(files: Sequence[Tuple[str, str]], folder: Any, own: Iterable[Any] = ()) -> Tuple[Summaries, List[str]]:
    """Each file's summaries, read in a process of its own: files are (kind, path), kind "vi" (one summary) or "llb"
    (one for each VI it holds). folder: where sub-VIs may be read from; own: Kelvin's own folders, never read.
    (summaries for each file, in order; notes on the survey as a whole, such as its time running out)."""
    results: Summaries = [[] for _ in files]
    notes: List[str] = []
    own = list(own)
    deadline = time.monotonic() + SURVEY_SECONDS
    start = 0
    while start < len(files):
        start = _read_some(files, start, folder, own, results, deadline, notes)
        if time.monotonic() >= deadline and start < len(files):
            left = len(files) - start
            for k in range(start, len(files)):
                results[k].append(_not_read(files[k][1], "the survey's time ran out before %s was read" % (
                    "all of it" if results[k] else "it")))
            notes.append("the survey stopped at its time limit (%s): %d file%s not read" % (
                _took(SURVEY_SECONDS), left, "" if left == 1 else "s"))
            break
    return results, notes


def _took(seconds: float) -> str:
    return "%d minutes" % (seconds // 60) if seconds >= 120 else "%g s" % seconds


def _read_some(files: Sequence[Tuple[str, str]], start: int, folder: Any, own: List[Any], results: Summaries,
               deadline: float, notes: List[str]) -> int:
    """One reading process for files[start:]: each file's summaries into results. Where the next process starts
    (len(files) when all were read)."""
    from . import sandbox
    from .envs import clean_environ
    job = {"path": [str(p) for p in sys.path], "memory_mb": MEMORY_MB, "folder": str(folder),
           "own": [str(o) for o in own], "files": [[kind, str(path)] for kind, path in files[start:]]}
    env = clean_environ()
    for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT"):
        env.pop(k, None)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    try:
        proc = subprocess.Popen(command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, creationflags=flags)
    except OSError as e:
        for k in range(start, len(files)):
            results[k].append(_not_read(files[k][1], "Kelvin's reader of LabVIEW files couldn't start (%s)" % e))
        return len(files)
    contained, why = sandbox._contain(proc, MEMORY_MB)
    if why and why not in notes:
        notes.append("note: %s; the time limits still hold" % why)
    lines: "queue.Queue[Optional[bytes]]" = queue.Queue()
    errors: Deque[str] = collections.deque(maxlen=20)
    threading.Thread(target=_pump, args=(proc.stdout, lines), daemon=True).start()
    threading.Thread(target=_drain, args=(proc.stderr, errors), daemon=True).start()
    at = start   # the file being read now
    try:
        try:
            assert proc.stdin is not None
            proc.stdin.write((json.dumps(job) + "\n").encode("utf-8"))   # it reads only now, inside its limits
            proc.stdin.close()
        except OSError:
            pass
        while at < len(files):
            wait = min(FILE_SECONDS, deadline - time.monotonic())
            try:
                line = lines.get(timeout=max(wait, 0.01))
            except queue.Empty:
                if time.monotonic() >= deadline:
                    return at
                results[at].append(_not_read(files[at][1], "it took too long to read (more than %s): Kelvin "
                                                           "stopped reading it" % _took(FILE_SECONDS)))
                return at + 1
            if line is None:   # the process ended before it said all
                try:
                    code = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    code = None
                memory = "MemoryError" in " ".join(errors) or code in (-9, 137, 3221225495, -1073741801)
                results[at].append(_not_read(files[at][1], "Kelvin's reader stopped on it%s" % (
                    " (it needed more memory than Kelvin gives one file)" if memory else "")))
                return at + 1
            try:
                msg = json.loads(line.decode("utf-8")) if line else {"i": at - start, "too_big": True}
            except (ValueError, RecursionError):
                msg = {"i": at - start, "unreadable": True}
            if not isinstance(msg, dict) or msg.get("ready"):
                continue
            k = start + msg.get("i", -1) if isinstance(msg.get("i"), int) else -1
            if k != at:
                continue   # nothing it would say: it reads in order
            if msg.get("too_big") or msg.get("unreadable"):
                results[at].append(_not_read(files[at][1], "its summary is bigger than Kelvin keeps"))
                msg["done"] = files[at][0] == "vi"
            for s in msg.get("s") or []:
                if isinstance(s, dict):
                    results[at].append(s)
            if msg.get("done"):
                at += 1
        return at
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        if contained is not None:
            k32, handle = contained
            k32.CloseHandle(handle)


def _pump(stream: Any, lines: "queue.Queue[Optional[bytes]]") -> None:
    """The process's lines, one at a time; one longer than MAX_LINE as b"" (its summary too big to keep)."""
    try:
        while True:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                break
            if len(line) > MAX_LINE:
                while line and not line.endswith(b"\n"):
                    line = stream.readline(MAX_LINE + 1)
                lines.put(b"")
                continue
            lines.put(line)
    except (OSError, ValueError):
        pass
    lines.put(None)


def _drain(stream: Any, errors: Deque[str]) -> None:
    try:
        for line in stream:
            errors.append(line.decode("utf-8", "replace").strip()[:300])
    except (OSError, ValueError):
        pass


# ------------------------------------------------------------------ in the reading process

def _limit_memory(mb: Any) -> None:
    """Outside Windows (where the job object does it): this process's memory, where the system allows a limit."""
    if os.name == "nt" or not mb:
        return
    try:
        import resource
        limit = int(mb) * 2 ** 20
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass


def serve(job: Dict[str, Any], out: Any) -> None:
    """The reading process's work (data/labview/kelvin_labview.py starts it): each file of the job read in turn,
    each summary written to out as a line of JSON as soon as it is made ({"i": the file's place in the job, "s":
    [summaries], "done": whether that file is done}); {"ready": true} first."""
    _limit_memory(job.get("memory_mb"))
    from . import labview, vigraph

    def say(msg: Dict[str, Any]) -> None:
        try:
            text = json.dumps(msg, allow_nan=False, separators=(",", ":"))
        except ValueError:   # a number that isn't finite: the summaries never keep one
            msg = dict(msg, s=[vigraph.plain_numbers(s)[0] for s in msg.get("s") or []])
            text = json.dumps(msg, allow_nan=False, separators=(",", ":"))
        if len(text) > MAX_LINE - 1:
            text = json.dumps({"i": msg["i"], "too_big": True, "done": msg.get("done", False)})
        out.write(text.encode("utf-8") + b"\n")
        out.flush()

    say({"ready": True})
    folder, own = job.get("folder") or None, job.get("own") or ()
    for i, (kind, path) in enumerate(job.get("files") or []):
        try:
            if kind == "llb":
                for s in vigraph.each_summary(path, labview.link, folder, own):
                    say({"i": i, "s": [s], "done": False})
                say({"i": i, "s": [], "done": True})
            else:
                say({"i": i, "s": [vigraph.summary(path, labview.link, folder, own)], "done": True})
        except MemoryError:
            say({"i": i, "s": [_not_read(path, "it needs more memory than Kelvin gives one file")], "done": True})
