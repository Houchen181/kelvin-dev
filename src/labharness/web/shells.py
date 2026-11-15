"""The Terminal's shells: the signed-in person's own shell on this computer, in a tab of the portal's Terminal pane, as
in a terminal window. On Windows it is Windows PowerShell on a pseudo-console (ConPTY, through pywinpty); elsewhere the
person's $SHELL (else bash) on a pseudo-terminal. Colours, Tab completion, history, Ctrl+C, full-screen programs and
resizing work as in any terminal.

It is the person's, never the AI's: no tool and no MCP route reaches it, only the portal's own page on this computer,
with the access key (app.py's /api/shell). What runs there goes past Kelvin's safety gate, as in any terminal on this
computer. The lab's record notes each shell opened and closed (whose, in which folder, when, and why it closed), never
what was typed or shown.

A shell and every process it starts end together: when its tab closes, when its person signs out, and when Kelvin
quits. None outlives Kelvin: on Windows the shell is in a job object (scriptrun's, as a script's run is), which ends it
and what it started with Kelvin's own process; elsewhere it leads a session of its own on its terminal, whose processes
Kelvin ends, and which hangs up should Kelvin itself end.

What a shell prints is read as it comes, always: a page that falls behind (a tab the browser throttles or freezes, a
debugger's pause) never holds up or pauses a person's program, which may be driving an instrument. Kelvin keeps the
latest MAX_PENDING characters for the page, and the page has at most app.py's SHELL_WINDOW on its way at once; what
falls between is left out, and the tab says so where it was.

Without a terminal to be had (Kelvin from source on Windows, without pywinpty), a shell runs on pipes instead: a line at
a time, as the Terminal sends it, with a prompt after each. Programs that draw the whole screen or ask for a password
need the terminal there, and Ctrl+C ends the shell with what runs in it and starts it fresh in the same folder."""
from __future__ import annotations

import atexit
import codecs
import os
import queue
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import weakref
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

READ_SIZE = 65536        # of a shell's output, read at once
MAX_PENDING = 1 << 20    # characters of output waiting for the page; past it the oldest of them are left out
MAX_INPUT = 1 << 20      # characters the page sends at once (a long paste)
MAX_SHELLS = 12          # shells open at once on this computer
END_WAIT_S = 5.0         # how long a shell that was closed is waited for to end
LAST_OUTPUT_S = 1.0      # once a shell ended, how long its last output is waited for (ConPTY's comes within 0.3 s)


class ShellRefused(Exception):
    """A shell can't be opened now, in words for the person."""


def shell_command() -> Tuple[str, List[str]]:
    """The person's shell as a terminal window starts it: its name and its command line. Windows: Windows PowerShell
    with their profile (conda's activate is set up there); elsewhere their $SHELL, else bash, as a login shell on a Mac,
    as Terminal starts one, so PATH is what they have there."""
    if os.name == "nt":
        exe = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "WindowsPowerShell", "v1.0",
                           "powershell.exe")
        return "PowerShell", [exe if os.path.isfile(exe) else shutil.which("powershell") or "powershell.exe", "-NoLogo"]
    shell = os.environ.get("SHELL") or ""
    if not (os.path.isfile(shell) and os.access(shell, os.X_OK)):
        shell = shutil.which("bash") or "/bin/sh"
    return os.path.basename(shell), [shell, "-l"] if sys.platform == "darwin" else [shell]


def shell_env() -> Dict[str, str]:
    """This computer's environment, as the person's own terminal has it: without what the packaged app's loader set for
    itself (PyInstaller's), and saying what the terminal is."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("_PYI_", "_MEIPASS"))}
    if "LD_LIBRARY_PATH_ORIG" in env:   # the packaged app's own libraries are for it alone
        env["LD_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH_ORIG")
    elif getattr(sys, "frozen", False):
        env.pop("LD_LIBRARY_PATH", None)
    if os.name != "nt":
        env.update(TERM="xterm-256color", COLORTERM="truecolor")
    return dict(sorted(env.items(), key=lambda kv: kv[0].upper()))   # Windows wants its environment block in order


def terminal_kind() -> Tuple[Optional[type], str]:
    """The pseudo-terminal this computer gives a shell, or None and why not."""
    if os.name == "nt":
        try:
            import winpty  # noqa: F401
        except Exception:
            return None, "pywinpty isn't installed"
        return _WinPty, ""
    try:
        import fcntl  # noqa: F401
        import termios  # noqa: F401
    except ImportError:
        return None, "this system has no pseudo-terminals"
    return _PosixPty, ""


class Shell:
    """One shell in a Terminal tab: its process with everything it starts, and its output on its way to the page."""

    kind = "pty"
    note = ""

    def __init__(self, program: str, argv: List[str], folder: str, env: Dict[str, str], cols: int, rows: int):
        self.id = secrets.token_hex(4)
        self.program, self.argv, self.folder, self.env = program, list(argv), folder, env
        self.cols, self.rows = cols, rows
        self.pid: Optional[int] = None
        self.owner: Optional[str] = None   # the account signed in when it opened (None: Kelvin without sign-in)
        self.by = "human (portal)"
        self.opened = datetime.now()
        self.exit_code: Optional[int] = None
        self.why: Optional[str] = None     # why it was closed; None: it exited by itself
        self.ended = threading.Event()     # its process ended, its record is written and its output is all here
        self.on_end: Callable[["Shell"], None] = lambda shell: None
        self._lock = threading.Lock()
        self._out: List[str] = []
        self._size = 0
        self.left_out = 0          # characters left out since the page last took its output (all of them: left_out_all)
        self.left_out_all = 0
        self._repaint = False      # left out: the next take asks the shell's screen to be drawn again
        self._wake: Callable[[], None] = lambda: None
        self._input: "queue.Queue[Optional[Tuple[str, bool]]]" = queue.Queue()
        self._reader: Optional[threading.Thread] = None
        self._closing = False

    # ------------------------------------------------------------------ what each kind does

    def _spawn(self) -> None:
        """Start the shell's process, what keeps its tree together, and its reader (_read_from)."""
        raise NotImplementedError

    def _send(self, text: str, raw: bool) -> None:
        """What the person typed or pasted, to the shell. raw: bytes, as xterm.js sends a mouse report it can't encode."""
        raise NotImplementedError

    def _wait(self) -> Optional[int]:
        """Until the shell's process ends: its exit code."""
        raise NotImplementedError

    def _end_tree(self) -> None:
        """The shell's process and every process it started, ended (again: nothing happens)."""
        raise NotImplementedError

    def _release(self) -> None:
        """Its terminal and handles let go of, once it ended."""
        raise NotImplementedError

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows

    def repaint(self) -> None:
        """After output was left out: the shell's screen drawn again, where its terminal can (a size a column narrower,
        then its own: ConPTY draws the whole screen anew, and a full-screen program redraws on the change)."""
        cols, rows = self.cols, self.rows
        if self.kind == "pty" and cols > 20 and not self.ended.is_set():
            self.resize(cols - 1, rows)
            self.resize(cols, rows)

    def _again(self) -> bool:
        """Start it fresh after its process ended (the pipe shell's Ctrl+C); False: it ended."""
        return False

    # ------------------------------------------------------------------ the same for every kind

    def start(self) -> None:
        self._spawn()
        threading.Thread(target=self._feed, daemon=True, name="kelvin-shell-in").start()
        threading.Thread(target=self._watch, daemon=True, name="kelvin-shell-end").start()

    def _read_from(self, read: Callable[[], Optional[str]]) -> None:
        """The shell's output, read on a thread of its own until read() says it ended (None)."""
        def pump() -> None:
            while True:
                try:
                    text = read()
                except Exception:   # its terminal or pipe went (pywinpty's own errors are no OSError)
                    return
                if text is None:
                    return
                if text:
                    self._put(text)
        self._reader = threading.Thread(target=pump, daemon=True, name="kelvin-shell-out")
        self._reader.start()

    def _put(self, text: str) -> None:
        """Output, kept for the page: never waiting for it, the latest MAX_PENDING characters at most (the oldest left
        out, counted)."""
        with self._lock:
            self._out.append(text)
            self._size += len(text)
            while self._size > MAX_PENDING:
                over, first = self._size - MAX_PENDING, self._out[0]
                cut = first if len(first) <= over else first[:over]
                self._out[0] = first[len(cut):]
                if not self._out[0]:
                    self._out.pop(0)
                self._size -= len(cut)
                self.left_out += len(cut)
                self.left_out_all += len(cut)
        self._wake()

    def take(self) -> str:
        """The output not yet sent to the page; the shell's output goes on. Where some was left out, a line says how
        much, and what follows starts at a line's start (the cut can fall inside a colour's or a cursor's sequence)."""
        with self._lock:
            text, self._out, self._size = "".join(self._out), [], 0
            left, self.left_out = self.left_out, 0
        if not left:
            return text
        line = text.find("\n", 0, 4096)
        text = text[line + 1:] if line >= 0 else text
        self._repaint = True
        return ("\x1b[0m\r\n\x1b[2m[%s of output left out here: this tab didn't keep up with it]\x1b[0m\r\n"
                % _amount(left)) + text

    def repaint_due(self) -> bool:
        """Whether output was left out since this was last asked: its screen is then drawn again (repaint)."""
        due, self._repaint = self._repaint, False
        return due

    def listen(self, wake: Callable[[], None]) -> None:
        """wake() is called, from the shell's own threads, when there is output to take or the shell ended."""
        self._wake = wake
        wake()

    def write(self, text: str, raw: bool = False) -> None:
        """Keys and pastes, in the order they came, on a thread of the shell's own: one that waits for the shell to
        read its input never holds the page's socket."""
        if text and not self.ended.is_set():
            self._input.put((text[:MAX_INPUT], raw))

    def _feed(self) -> None:
        while True:
            item = self._input.get()
            if item is None:
                return
            try:
                self._send(*item)
            except Exception:
                pass   # it ended meanwhile: _watch says so

    def _watch(self) -> None:
        """Until the shell ends: then what it started ends with it, its last output comes, and its record is written."""
        while True:
            code = self._wait()
            if self._closing or not self._again():
                break
        self.exit_code = code
        self._end_tree()
        reader = self._reader
        if reader is not None:
            reader.join(0 if self._closing else LAST_OUTPUT_S)
        self._release()
        self._input.put(None)
        try:
            self.on_end(self)
        finally:
            self.ended.set()
            self._wake()

    def close(self, why: str) -> None:
        """End it with everything it started (its tab closed, its person signed out, Kelvin quits)."""
        if not self.ended.is_set() and self.why is None:
            self.why = why
        self._closing = True
        self._end_tree()
        self.ended.wait(END_WAIT_S)


def _amount(chars: int) -> str:
    """So much output, in words (a character of it is about a byte)."""
    return ("%.1f MB" % (chars / 1e6) if chars >= 1e6 else "%d kB" % max(1, round(chars / 1e3)) if chars >= 1e3
            else "%d characters" % chars)


_CTRL_C: List[Any] = []   # Kelvin's own console handler (_ctrl_c_reaches_shells), kept for as long as Kelvin runs


def _ignoring_ctrl_c() -> bool:
    """Windows: whether Kelvin's process ignores Ctrl+C, as one started in a process group of its own does: bit 0 of
    the ConsoleFlags in its process parameters, which a process it starts inherits. Unreadable: as if it did."""
    try:
        import ctypes
        from ctypes import wintypes

        class _Basic(ctypes.Structure):   # PROCESS_BASIC_INFORMATION
            _fields_ = [(n, ctypes.c_void_p) for n in ("ExitStatus", "PebBaseAddress", "AffinityMask",
                                                       "BasePriority", "UniqueProcessId", "InheritedFrom")]
        nt = ctypes.WinDLL("ntdll")
        nt.NtQueryInformationProcess.argtypes = (wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                                 ctypes.c_void_p)
        basic = _Basic()
        if nt.NtQueryInformationProcess(wintypes.HANDLE(-1), 0, ctypes.byref(basic), ctypes.sizeof(basic), None) \
                or not basic.PebBaseAddress:
            return True
        wide = ctypes.sizeof(ctypes.c_void_p) == 8   # PEB.ProcessParameters, then its ConsoleFlags
        params = ctypes.c_void_p.from_address(basic.PebBaseAddress + (0x20 if wide else 0x10)).value
        return bool(ctypes.c_ulong.from_address(params + (0x18 if wide else 0x14)).value & 1)
    except Exception:
        return True


def _ctrl_c_reaches_shells() -> None:
    """Windows: Ctrl+C typed in a shell reaches what runs there. Kelvin started in a process group of its own (as
    whatever launched it may do) ignores Ctrl+C, and so does every process it starts: a shell's ping or Python could
    then never be interrupted. So Kelvin stops ignoring it, for its shells to inherit, having first taken Ctrl+C for
    itself with a handler of its own, which no process inherits: a Ctrl+C on Kelvin's own console leaves it running,
    as before. Ctrl+Break, closing that console, logging off and shutting down reach it as they always did. A Kelvin
    that never ignored Ctrl+C is left as it is."""
    if _CTRL_C or not _ignoring_ctrl_c():
        return
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32")
    taken = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)(lambda event: event == 0)   # CTRL_C_EVENT only
    if k32.SetConsoleCtrlHandler(taken, True):
        _CTRL_C.append(taken)
        k32.SetConsoleCtrlHandler(None, False)


class _WinPty(Shell):
    """Windows: the shell on a pseudo-console (ConPTY) through pywinpty, in a job object with what it starts. Its output
    is read with pywinpty's PTY on a thread of the shell's own, as fast as it comes: PtyProcess's own reader (a read,
    a millisecond's pause, a socket) fell behind a program printing fast, and what it hadn't read yet piled up inside
    pywinpty without end (590 MB in 40 s, in a review)."""

    pty: Any = None

    def _spawn(self) -> None:
        from winpty import PTY
        from ..scriptrun import _Job
        _ctrl_c_reaches_shells()
        pty = PTY(self.cols, self.rows)
        exe = shutil.which(self.argv[0], path=self.env.get("PATH")) or self.argv[0]
        block = "\0".join("%s=%s" % kv for kv in self.env.items()) + "\0"
        if not pty.spawn(exe, cmdline=" " + subprocess.list2cmdline(self.argv[1:]), cwd=self.folder, env=block):
            raise OSError("%s didn't start" % self.program)
        try:   # at once, before it starts anything: PowerShell takes a moment to come up
            self._proc = _ByPid(pty.pid)
            self._job = _Job(self._proc)
        except OSError:
            try:
                os.kill(pty.pid, signal.SIGTERM)
            except OSError:
                pass
            raise
        self.pty, self.pid = pty, pty.pid

        def read() -> Optional[str]:
            while self.pty is not None:   # let go of: its last read was cancelled
                text = pty.read(blocking=True)
                if text:
                    return text
                if pty.iseof():
                    return None
                time.sleep(0.005)
            return None
        self._read_from(read)

    def _send(self, text: str, raw: bool) -> None:
        pty = self.pty
        if pty is not None:
            pty.write(text)

    def resize(self, cols: int, rows: int) -> None:
        super().resize(cols, rows)
        pty = self.pty
        if pty is not None:
            try:
                pty.set_size(cols, rows)
            except Exception:   # it ended meanwhile
                pass

    def _wait(self) -> Optional[int]:
        return self._proc.wait()

    def _end_tree(self) -> None:
        self._job.end()

    def _release(self) -> None:
        self._job.close()
        self._proc.close()
        pty, self.pty = self.pty, None   # the pseudo-console goes with it, once its reader let go of it too
        try:
            pty.cancel_io()
        except Exception:
            pass


class _ByPid:
    """A process pywinpty started, as scriptrun._Job takes one: its id, a handle to it, and whether it ended."""

    SYNCHRONIZE, WAIT_OBJECT_0, INFINITE = 0x00100000, 0, 0xFFFFFFFF

    def __init__(self, pid: int):
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        # PROCESS_TERMINATE, PROCESS_SET_QUOTA (into the job), PROCESS_QUERY_LIMITED_INFORMATION, and waiting on it
        handle = k32.OpenProcess(0x0001 | 0x0100 | 0x1000 | self.SYNCHRONIZE, False, pid)
        if not handle:
            raise OSError(ctypes.get_last_error(), "the shell's process could not be opened")
        self.pid, self._handle, self._k32, self._wintypes, self._ctypes = pid, handle, k32, wintypes, ctypes

    def _code(self) -> Optional[int]:
        code = self._wintypes.DWORD()
        if not self._k32.GetExitCodeProcess(self._handle, self._ctypes.byref(code)):
            return None
        return int(code.value)

    def poll(self) -> Optional[int]:
        if self._handle is None or self._k32.WaitForSingleObject(self._handle, 0) == self.WAIT_OBJECT_0:
            return self._code() if self._handle is not None else 0
        return None

    def wait(self) -> Optional[int]:
        if self._handle is not None:
            self._k32.WaitForSingleObject(self._handle, self.INFINITE)
        return self._code() if self._handle is not None else None

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            self._k32.CloseHandle(handle)


def _take_terminal() -> None:
    """In the shell's own process as it starts, in its new session: its terminal becomes its controlling one, so
    Ctrl+C, job control and the hang-up reach what runs there."""
    import fcntl
    import termios
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _session(sid: int) -> Set[int]:
    """The processes of a shell's session (it leads it): its jobs too, each in a process group of its own. Linux reads
    /proc; a Mac asks pgrep; else psutil gives the shell's descendants, where it is installed."""
    found: Set[int] = set()
    proc = Path("/proc")
    if proc.is_dir():
        for d in proc.iterdir():
            if not d.name.isdigit():
                continue
            try:
                fields = (d / "stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()
                if int(fields[3]) == sid:   # state, ppid, pgrp, session
                    found.add(int(d.name))
            except (OSError, IndexError, ValueError):
                continue
        return found
    try:
        listed = subprocess.run(["pgrep", "-s", str(sid)], capture_output=True, text=True, timeout=5).stdout
        return {int(p) for p in listed.split()}
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    try:
        import psutil
        found = {p.pid for p in psutil.Process(sid).children(recursive=True)}
    except Exception:
        pass
    return found


class _PosixPty(Shell):
    """Linux and macOS: the shell on a pseudo-terminal of its own, leading a session of its own there. Its reader
    closes the terminal once nothing holds it any more: closed under a read, its number could be another file's by the
    next read."""

    def _spawn(self) -> None:
        master, slave = os.openpty()
        try:
            self._set_size(slave, self.cols, self.rows)
            self.proc = subprocess.Popen(self.argv, cwd=self.folder, env=self.env, stdin=slave, stdout=slave,
                                         stderr=slave, start_new_session=True, preexec_fn=_take_terminal)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.master, self.pid = master, self.proc.pid
        self._fd = threading.Lock()   # a write or a resize never meets the terminal's number being let go of
        decode = codecs.getincrementaldecoder("utf-8")("replace").decode

        def read() -> Optional[str]:
            try:
                data = os.read(master, READ_SIZE)
            except OSError:   # EIO: nothing has the terminal open any more
                data = b""
            if data:
                return decode(data)
            with self._fd:
                self.master = -1
                os.close(master)
            return None
        self._read_from(read)

    @staticmethod
    def _set_size(fd: int, cols: int, rows: int) -> None:
        import fcntl
        import struct
        import termios
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def _send(self, text: str, raw: bool) -> None:
        data = text.encode("latin-1", "replace") if raw else text.encode("utf-8", "replace")
        while data:
            with self._fd:
                if self.master < 0:
                    return
                data = data[os.write(self.master, data):]

    def resize(self, cols: int, rows: int) -> None:
        super().resize(cols, rows)
        with self._fd:
            if self.master >= 0:
                try:
                    self._set_size(self.master, cols, rows)   # the shell's foreground job hears of it (SIGWINCH)
                except OSError:
                    pass

    def _wait(self) -> Optional[int]:
        return self.proc.wait()

    def _end_tree(self) -> None:
        pid = self.pid
        if pid is None:
            return
        for p in _session(pid) - {pid, os.getpid()}:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        if self.proc.poll() is None:   # its number names its group only while it is there
            for kill in (os.killpg, os.kill):
                try:
                    kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    def _release(self) -> None:
        pass   # its reader closes the terminal, once everything on it ended


class _PipeShell(Shell):
    """A shell on pipes, where no terminal can be had. Each line the page sends runs as typed; while the shell waits
    at its prompt a line of Kelvin's follows it, with a key of this shell's own that no output knows, and what that
    line prints (how it ended, the folder) is shown as the prompt. A line typed while a command runs is that command's
    input. Ctrl+C can't reach what runs without a terminal: the shell ends with what runs there, and starts fresh in the
    folder it was in."""

    kind = "pipe"

    def __init__(self, *a: Any, why: str = "", **kw: Any):
        super().__init__(*a, **kw)
        self.note = ("No terminal for this shell here (%s), so it runs a line at a time: programs that draw the whole "
                     "screen or ask for a password don't work, and Ctrl+C starts the shell fresh in its folder."
                     % (why or "none to be had"))
        if "pywinpty" in why:
            self.note += " Install pywinpty (pip install pywinpty) for a full terminal."
        self.where = self.folder   # the folder its last line ended in
        self._key = "kelvin-" + secrets.token_hex(12)
        self._ps = self.program == "PowerShell"
        self._busy = False         # a line runs (its key's line hasn't come back)
        self._typed = ""           # what was typed after the last line's end
        self._fresh = False        # Ctrl+C asked for a fresh start
        self._part = ""            # the start of a line held back: it may be the key's
        self._mid = False          # a line begun and shown
        self._blank = False        # an empty line held back: the key's line brings one of its own

    def _sentinel(self) -> str:
        if self._ps:
            return ('$__kelvin = $?; [Console]::Out.Write("`n%s " + [int](-not $__kelvin) + " " + (Get-Location).Path'
                    ' + "`n")' % self._key)
        return '__kelvin=$?; printf "\\n%s %%s %%s\\n" "$__kelvin" "$PWD"' % self._key

    def _spawn(self) -> None:
        from ..scriptrun import _Job
        argv = self.argv + (["-Command", "-"] if self._ps else [])
        self.proc = subprocess.Popen(argv, cwd=self.where, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                     start_new_session=os.name != "nt")
        self._job = _Job(self.proc)
        self.pid = self.proc.pid
        self._busy, self._part, self._mid, self._blank = False, "", False, False   # starting runs no command
        first = ("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); $OutputEncoding = "
                 "[Console]::OutputEncoding\n" if self._ps else "")
        self._raw(first + self._sentinel() + "\n")   # its first prompt
        stream, decode = self.proc.stdout, codecs.getincrementaldecoder("utf-8")("replace").decode

        def read() -> Optional[str]:
            data = stream.read1(READ_SIZE)
            if data:
                return self._shown(decode(data))
            stream.close()   # here, never under a read of it
            return self._shown(decode(b"", final=True), end=True) or None
        self._read_from(read)

    def _raw(self, text: str) -> None:
        self.proc.stdin.write(text.encode("utf-8", "replace"))
        self.proc.stdin.flush()

    def _send(self, text: str, raw: bool) -> None:
        if "\x03" in text:   # Ctrl+C: nothing to interrupt without a terminal, so the shell starts fresh
            self._fresh = True
            self._end_tree()
            return
        self._typed += text.replace("\r\n", "\n").replace("\r", "\n")
        while "\n" in self._typed:
            line, self._typed = self._typed.split("\n", 1)
            if self._busy:   # what runs reads it
                self._raw(line + "\n")
            else:
                self._busy = True
                self._raw(line + "\n" + self._sentinel() + "\n")

    def _prompt(self, line: str) -> str:
        parts = line.split(" ", 2)
        if len(parts) == 3:
            self.where = parts[2]
        self._busy = False
        return ("PS %s> " if self._ps else "%s$ ") % self.where

    def _shown(self, text: str, end: bool = False) -> str:
        """What the shell printed, as the tab shows it: the key's line becomes the prompt, the rest comes as it came, a
        line not ended at once (a question waiting for its answer) unless it may yet be the key's."""
        out: List[str] = []

        def blank() -> None:
            if self._blank:
                out.append("\r\n")
                self._blank = False
        buf = self._part + text.replace("\r\n", "\n")
        self._part = ""
        while buf:
            cut = buf.find("\n")
            line, buf = (buf, "") if cut < 0 else (buf[:cut], buf[cut + 1:])
            if cut < 0:   # a line not ended yet
                if not self._mid and not end and (self._key.startswith(line) or line.startswith(self._key)):
                    self._part = line
                else:
                    blank()
                    out.append(line)
                    self._mid = True
                break
            if self._mid:
                out.append(line + "\r\n")
                self._mid = False
            elif line.startswith(self._key + " "):
                self._blank = False   # the empty line before it was the key line's own
                out.append(self._prompt(line))
            elif not line:
                blank()
                self._blank = True
            else:
                blank()
                out.append(line + "\r\n")
        if end:
            blank()
        return "".join(out)

    def _wait(self) -> Optional[int]:
        return self.proc.wait()

    def _again(self) -> bool:
        if not self._fresh:
            return False
        self._fresh = False
        if self._reader is not None:   # the last of what it printed comes first
            self._reader.join(LAST_OUTPUT_S)
        self._release()
        self._typed = ""
        self._put("^C\r\n[Without a terminal Ctrl+C ends the shell: it started again in %s, with its variables "
                  "reset.]\r\n" % self.where)
        self._spawn()
        return True

    def _end_tree(self) -> None:
        self._job.end()

    def _release(self) -> None:
        self._job.close()
        try:
            self.proc.stdin.close()   # its output is its reader's to close
        except (OSError, ValueError):
            pass


_EVERY: "weakref.WeakSet[Shells]" = weakref.WeakSet()


class Shells:
    """The Terminal's shells on this computer, whoever's they are. record(type, **fields) writes in the lab's record
    (app.py: the lab's event log): each shell opened and closed, never what was typed or shown."""

    def __init__(self, record: Callable[..., Any]):
        self.record = record
        self._lock = threading.Lock()
        self._open: Dict[str, Shell] = {}
        self._turn = 0        # a person's turn at this computer: a new one at each close_all
        self._held = False    # while the person signed in changes, no shell opens
        _EVERY.add(self)

    @property
    def turn(self) -> int:
        """Read as a shell is asked for, with who asks: a shell opens only within that person's turn."""
        return self._turn

    def open(self, owner: Optional[str], by: str, folder: str, cols: int = 80, rows: int = 24,
             pipe: bool = False, turn: Optional[int] = None, still: Optional[Callable[[], bool]] = None) -> Shell:
        """A new shell in folder, started (OSError: it couldn't be; ShellRefused: too many are open, or the person who
        asked for it is no longer the one signed in: their turn, as read when they asked, has passed, or still() says
        they are gone). Both are asked again, under the lock, once it has started: a shell started as its person
        signed out, or another signed in, is closed, and never counts as opened."""
        def refused() -> Optional[str]:
            if self._held or turn is not None and turn != self._turn or still is not None and not still():
                return "The person signed in changed meanwhile: open a new tab."
            if len(self._open) >= MAX_SHELLS:
                return "%d shells are open already: close one of their tabs first." % MAX_SHELLS
            return None
        with self._lock:
            why_not = refused()
        if why_not:
            raise ShellRefused(why_not)
        program, argv = shell_command()
        env = shell_env()
        kind, why = (None, "asked for none") if pipe else terminal_kind()
        shell: Shell
        if kind is not None:
            shell = kind(program, argv, folder, env, cols, rows)
            try:
                shell.owner, shell.by, shell.on_end = owner, by, self._ended
                shell.start()
            except Exception as e:   # the terminal failed (an old Windows, a part of pywinpty missing): pipes then
                if isinstance(e, OSError) and not Path(folder).is_dir():
                    raise   # nothing would start there
                why = "its terminal didn't start: %s" % (getattr(e, "strerror", None) or e)
                kind = None
        if kind is None:
            shell = _PipeShell(program, argv, folder, env, cols, rows, why=why)
            shell.owner, shell.by, shell.on_end = owner, by, self._ended
            shell.start()
        with self._lock:   # its record before a close_all can close it: opened, then closed
            why_not = refused()
            if why_not is None:
                self._open[shell.id] = shell
                self.record("shell_opened", by=by, account=owner, shell=shell.id, program=program, folder=folder,
                            kind=shell.kind, pid=shell.pid)
        if why_not:
            shell.close("it was refused")
            raise ShellRefused(why_not)
        return shell

    def _ended(self, shell: Shell) -> None:
        with self._lock:
            if self._open.pop(shell.id, None) is None:
                return
        self.record("shell_closed", by=shell.by, account=shell.owner, shell=shell.id, program=shell.program,
                    folder=shell.folder, opened=shell.opened.isoformat(timespec="seconds"),
                    seconds=round((datetime.now() - shell.opened).total_seconds(), 1),
                    why=shell.why or "it exited", exit=shell.exit_code)

    def list(self) -> List[Shell]:
        with self._lock:
            return list(self._open.values())

    def close(self, shell: Shell, why: str) -> None:
        shell.close(why)

    def close_all(self, why: str, hold: bool = False) -> int:
        """Every shell ended, each on a thread of its own (one slow to end never holds the others): its person signed
        out, or Kelvin quits. A new turn begins: a shell asked for before this never opens. hold: none opens until
        resume() (the person signed in is changing). Returns how many there were."""
        with self._lock:
            self._turn += 1
            self._held = self._held or hold
            shells = list(self._open.values())
        threads = [threading.Thread(target=s.close, args=(why,), daemon=True, name="kelvin-shell-close")
                   for s in shells]
        for t in threads:
            t.start()
        for t in threads:
            t.join(END_WAIT_S + 1)
        return len(shells)

    def resume(self) -> None:
        with self._lock:
            self._held = False


@atexit.register
def _never_outlive_kelvin() -> None:
    """As Python ends, any shell still open ends with it (the portal's own closing ends them first)."""
    for shells in list(_EVERY):
        try:
            shells.close_all("Kelvin quit")
        except Exception:
            pass
