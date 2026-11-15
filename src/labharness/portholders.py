"""Which programs hold a serial port, and which may hold a VISA session, so a port nobody can open says who has it.

Field report 2026-11-15 (item 8): the lab's nvpulse failed with "could not open port 'COM11': PermissionError(13,
'Access is denied.')". CoboltMonitor.exe, the laser maker's program, had the port open, and the person had to find that
out themselves: connected_devices showed the port, not who held it. A serial port opens in one program at a time, so
the program that has it is the answer. Kelvin names it wherever a port couldn't be opened: a script's failure, Kelvin's
own open of an instrument, a scan, inspect_serial, and connected_devices for every port.

    Windows  the port's device (QueryDosDevice: COM11 is \\Device\\VCP0), then every handle open on this computer
             (NtQuerySystemInformation's extended handle list). Each File handle of a process in this Windows session
             that Kelvin may look into is duplicated here and asked its device's type, which never waits; one on a
             character device is asked its name, and a name that is the port's device is that process's. A name waits
             for as long as a synchronous read pending on the handle in its process: a console's handle is never asked
             (a console waits for input, for ever if nobody types), and a serial port's whose name doesn't come names
             its process as the port's likely holder (a maker's program reading its port without overlapped I/O).
    Linux    /proc/<pid>/fd: an open file that is the port's device (by its device number, so a link to it, as
             /dev/serial/by-id/... is, names the same holder).

Anything else (a Mac), and what Kelvin may not look into (a program run as administrator, another user's), names
nobody: never an error. A look runs on threads of its own with a deadline, and a handle whose answer doesn't come in
time is left to wait (_walk), so a look never holds Kelvin; it is asked again once that answer has come. Processes of
other Windows sessions (services, lsass) are never opened: security software takes that for an attack.

A VISA session can't be traced to its program. Where a VISA instrument can't be opened because something holds it,
Kelvin names the programs that have VISA's library loaded (visa32.dll / visa64.dll, libvisa.so): one of them may."""
from __future__ import annotations

import os
import re
import stat
import struct
import sys
import threading
import time
import types
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

WIN = sys.platform.startswith("win")
LOOK_S = 4.0         # one look at who holds a port, at most
STEP_S = 0.5         # one handle (on Linux, one process's open files), at most: past it, left to wait, not asked again
VISA_KEEP_S = 10.0   # a look at the programs with VISA loaded holds this long (each takes about a second on Windows)
VISA_LIBRARIES = ("visa32.dll", "visa64.dll", "libvisa.so")
# what an open failure says when something else has the port or the instrument: Windows' Access is denied (pyserial's
# PermissionError 13), Linux's Errno 16 (busy) and pyserial's lock, VISA's busy or locked resource
BUSY = re.compile(r"access is denied|permissionerror\(13|errno 13\]|errno 16\]|resource busy|could not exclusively "
                  r"lock port|rsrc_busy|rsrc_locked|-1073807246|-1073807345|visa cannot currently access it", re.I)
# of those, what can only be about a port or an instrument, never a file: VISA's busy or locked resource, and
# pyserial's own words (which name the port)
OPENING = re.compile(r"could not (?:open|exclusively lock) port|rsrc_busy|rsrc_locked|-1073807246|-1073807345|"
                     r"visa cannot currently access it", re.I)
# the device types a handle on a serial port may have (Windows' FILE_DEVICE_*): those GetFileType calls character
# devices (keyboard, mouse, null, parallel and serial ports, printer, screen, sound, modem), and the unknown kind that
# some USB-serial drivers give their ports. Never a console's (0x50), whose name waits on its input
ASKED = frozenset((0x0B, 0x0F, 0x15, 0x16, 0x18, 0x1B, 0x1C, 0x1D, 0x2B, 0x22))
SERIAL = frozenset((0x1B, 0x2B))   # a serial port's and a modem's (FILE_DEVICE_SERIAL_PORT, FILE_DEVICE_MODEM)

_SKIP: Set[Any] = set()     # what still waits on its answer: (process, handle, object) on Windows, a process on Linux
_WAITING: Dict[Any, Tuple[int, int]] = {}   # Windows: a handle being asked its name -> (its process, its device's type)
_OURS: Set[int] = set()     # handles this process holds for a look: never taken for Kelvin's own on a port
_LOOKING = threading.Lock()  # one look at a time (another's duplicated handles are in this process meanwhile)
_visa_lock = threading.Lock()
_visa: Dict[Tuple[str, ...], Tuple[float, List[Dict[str, Any]]]] = {}
_API: Any = None

_COM = re.compile(r"^(?:serial://)?(?:\\\\\.\\)?(?:ASRL)?(COM\d+):?(?:::INSTR)?$", re.I)
_ASRL = re.compile(r"^ASRL(\d+)(?:::INSTR)?$", re.I)
_DEV = re.compile(r"^(?:serial://)?(?:ASRL)?(/dev/\S+?)(?:::INSTR)?$")
# a serial port in a text: COM11, ASRL11::INSTR, or a serial device's path (Linux's ttyS, ttyUSB, ttyACM and ttyAMA, its
# /dev/serial/by-id and by-path links; a Mac's cu.* and tty.*), never another /dev path (/dev/null, /dev/shm/...)
_NAMED = re.compile(r"\b(COM\d+)\b|\bASRL(\d+)::INSTR|(/dev/(?:tty(?:S|USB|ACM|AMA)\d+|serial/by-(?:id|path)/"
                    r"[\w.:+-]*\w|(?:cu|tty)\.[\w.-]*\w))", re.I)
_QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"")


def port_name(address: Any) -> Optional[str]:
    """The serial port an address opens, as this computer names it: COM11 for COM11, com11, serial://COM11, \\\\.\\COM11
    or ASRL11::INSTR; /dev/ttyUSB0 for serial:///dev/ttyUSB0 or ASRL/dev/ttyUSB0::INSTR. None for any other address."""
    a = str(address or "").strip()
    m = _COM.match(a)
    if m:
        return m.group(1).upper()
    m = _ASRL.match(a)
    if m:
        return "COM" + m.group(1)
    m = _DEV.match(a)
    return m.group(1) if m else None


def ports_in(text: str) -> List[str]:
    """The serial ports a text names (an error: could not open port 'COM11', ... '/dev/ttyUSB0'), each once."""
    out: List[str] = []
    for m in _NAMED.finditer(text or ""):
        port = m.group(1).upper() if m.group(1) else "COM" + m.group(2) if m.group(2) else m.group(3)
        if port not in out:
            out.append(port)
    return out


def busy(error: Any) -> bool:
    """Whether an open's failure says something else has the port or the instrument (BUSY)."""
    return bool(BUSY.search(str(error or "")))


def about_a_file(line: str) -> bool:
    """Whether an error's line names a file that is no serial port (Permission denied: 'C:\\\\data\\\\odmr.csv', a
    workbook Excel holds): the error is that file's, whatever port the script also opens. A port in a file's name
    (COM11.csv) is the file's too."""
    for m in _QUOTED.finditer(line or ""):
        quoted = (m.group(1) if m.group(1) is not None else m.group(2)).replace("\\\\", "\\").strip()
        port = _COM.match(quoted) or _ASRL.match(quoted) or _NAMED.fullmatch(quoted)
        if ("/" in quoted or "\\" in quoted or re.search(r"\.\w{1,5}$", quoted)) and not port:
            return True
    return False


# ------------------------------------------------------------------ who holds a port

def holders(port: str, timeout: float = LOOK_S) -> List[Dict[str, Any]]:
    """The programs that have a serial port open (or another character device: NUL, a pseudo-terminal), each as
    {pid, name (its program's file), path, kelvin (Kelvin itself), likely}; [] when none does, or when Kelvin can't tell
    (see above). likely: a program with a serial port whose name Kelvin couldn't ask (a synchronous read waiting on
    it), given for a serial port only when nobody is seen holding it for sure. Never raises, and never takes much
    longer than timeout."""
    return holders_of([port], timeout).get(port, [])


def holders_of(ports: Iterable[str], timeout: float = LOOK_S, likely: bool = True) -> Dict[str, List[Dict[str, Any]]]:
    """holders for several ports in one look: {each port as given: its holders}. likely: with the likely ones where
    none is sure (connected_devices, which lists every port, says them apart instead: waiting)."""
    asked = list(dict.fromkeys(str(p) for p in ports if p))
    out: Dict[str, List[Dict[str, Any]]] = {p: [] for p in asked}
    names = {p: port_name(p) or p.strip() for p in asked}
    if not asked or not _LOOKING.acquire(timeout=timeout):
        return out
    try:
        found = _look(sorted(set(names.values())), timeout)
    except Exception:   # a look is a help: never a failure of its own
        found = {}
    finally:
        _LOOKING.release()
    who: Dict[int, Dict[str, Any]] = {}
    maybe = waiting() if likely else []
    for p in asked:
        sure = [who.setdefault(pid, _who(pid)) for pid in sorted(found.get(names[p], ()))]
        out[p] = sure or ([dict(h) for h in maybe] if port_name(p) else [])
    return out


def waiting() -> List[Dict[str, Any]]:
    """The programs with a serial port whose name Kelvin couldn't ask (a synchronous read pending on it, so asking
    waits with it: a maker's program reading its port without overlapped I/O), as holders gives them, likely: one of
    them may be the holder of a port nobody is seen holding."""
    pids = sorted({pid for pid, kind in list(_WAITING.values()) if kind in SERIAL})
    return [dict(_who(pid), likely=True) for pid in pids]


def named(found: Sequence[Dict[str, Any]]) -> str:
    """Programs as people know them: "CoboltMonitor.exe (process 4242)", "Kelvin itself (process 812)"."""
    return ", ".join("%s (process %d)" % ("Kelvin itself" if h.get("kelvin") else h.get("name") or "a program",
                                          h["pid"]) for h in found)


def say(port: str, found: Sequence[Dict[str, Any]]) -> str:
    """Who has a port open, in words: "COM11 is open in CoboltMonitor.exe (process 4242)"; for a likely holder only
    (holders), "COM11 is probably open in VendorTool.exe (process 77), whose serial port couldn't be asked its name (a
    read is waiting on it)"; "" for nobody."""
    name = port_name(port) or port
    sure = [h for h in found if not h.get("likely")]
    if sure:
        return "%s is open in %s" % (name, named(sure))
    maybe = [h for h in found if h.get("likely")]
    if maybe:
        return ("%s is probably open in %s, whose serial port couldn't be asked its name (a read is waiting on it)"
                % (name, named(maybe)))
    return ""


def held_text(port: str) -> str:
    """Who has a port open, in words (say), or "" when Kelvin sees nobody holding it."""
    return say(port, holders(port))


def device_of(port: str) -> Optional[str]:
    """The device a port's name stands for on Windows (COM11: \\Device\\VCP0, an FTDI port; NUL: \\Device\\Null); None
    elsewhere, or when there is no such port now."""
    name = port_name(port) or str(port or "").strip()
    if not WIN or not re.match(r"^[A-Za-z][\w$]{0,62}$", name):
        return None
    try:
        api = _api()
        buf = api.ctypes.create_unicode_buffer(1024)
        if not api.k32.QueryDosDeviceW(name, buf, len(buf)):
            return None
        return buf.value or None
    except Exception:
        return None


def nobody(port: str) -> str:
    """A port another program has open whose program Kelvin can't see, in words, with where a person looks."""
    name = port_name(port) or port
    if name.startswith("/"):
        return ("another program has %s open, and Kelvin can't see which (another user's, perhaps): sudo lsof %s "
                "shows it" % (name, name))
    if WIN:
        return ("another program has %s open, and Kelvin can't see which (one run as administrator, perhaps): "
                "Resource Monitor (resmon), CPU tab, Associated Handles, searching for %s, shows it"
                % (name, device_of(name) or name))
    return "another program has %s open, and Kelvin can't see which" % name


def who_has(address: str, error: Any = "") -> str:
    """Who has what Kelvin couldn't open, in words to add to the failure ("; COM11 is open in CoboltMonitor.exe
    (process 4242)"), or "": a serial port's holder, whatever the error said (and when nobody is seen holding one the
    error says is busy, where to look); for another VISA instrument whose error says something holds it (busy, locked,
    access denied), the programs with VISA loaded."""
    port = port_name(address)
    if port:
        held = held_text(port)
        if held:
            return "; " + held
        return "; " + nobody(port) if busy(error) else ""
    if busy(error):
        users = visa_users()
        if users:
            return "; programs with VISA loaded, one of which may hold its session: " + named(users)
    return ""


def _look(names: List[str], timeout: float) -> Dict[str, Set[int]]:
    """{name: the processes holding it} for this computer (Windows, Linux); {} where it can't say."""
    if WIN:
        return _windows(names, timeout)
    if sys.platform.startswith("linux"):
        return _linux(names, timeout)
    return {}


class _Found:
    """What a walk's steps found, taken whole when the walk ends: a step left to itself (it never returned in time)
    adds nothing after."""

    def __init__(self) -> None:
        self.items: Set[Any] = set()
        self.lock = threading.Lock()
        self.closed = False

    def add(self, item: Any) -> None:
        with self.lock:
            if not self.closed:
                self.items.add(item)

    def close(self) -> Set[Any]:
        with self.lock:
            self.closed = True
            return set(self.items)


def _walk(items: Sequence[Any], step: Callable[[Any, Any], None], skip: Set[Any], timeout: float = LOOK_S,
          step_s: float = STEP_S) -> Set[Any]:
    """step(item, found) for each item not in skip, on a thread of its own, never past timeout: an item whose step
    takes longer than step_s is left to it and put in skip, and the rest go on, on a new thread. It stays in skip only
    while its step waits: once that returns, it is stepped again by the next walk (a review of the 2026-11-15 fixes: a
    holder whose read was pending for a moment was never named again). What the steps found.add(...)ed, as it is when
    the walk ends."""
    end = time.monotonic() + timeout
    found: Set[Any] = set()
    start = 0
    while start < len(items) and time.monotonic() < end:
        state: Dict[str, Any] = {"at": None, "left": False, "stuck": None}
        box = _Found()
        lock = threading.Lock()   # the walk's thread and its caller each see the other's at and stuck whole

        def go(first: int = start, state: Dict[str, Any] = state, box: _Found = box, lock: Any = lock) -> None:
            for i in range(first, len(items)):
                with lock:
                    if state["left"]:
                        return
                if items[i] in skip:
                    continue
                at = (i, time.monotonic())
                state["at"] = at
                try:
                    step(items[i], box)
                except Exception:   # one that fails tells nothing: the others still may
                    pass
                with lock:
                    state["at"] = None
                    if state["stuck"] is at:   # it was left waiting on this one: its answer came, so asked again
                        skip.discard(items[i])
        thread = threading.Thread(target=go, name="kelvin-port-holders", daemon=True)
        thread.start()
        stuck = None
        while thread.is_alive() and time.monotonic() < end:
            thread.join(0.02)
            with lock:
                at = state["at"]
                if at is not None and time.monotonic() - at[1] > step_s:
                    stuck = state["stuck"] = at
                    skip.add(items[at[0]])   # while it waits on: not asked again meanwhile
                    break
        with lock:
            state["left"] = True
        found |= box.close()
        if stuck is None:   # it went through them all, or the deadline came
            break
        start = stuck[0] + 1
    return found


def _who(pid: int) -> Dict[str, Any]:
    """A process as holders gives it: its program's file name and path, as far as Kelvin may see them."""
    path = ""
    if WIN:
        try:
            api = _api()
            proc = api.k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION: also one run as admin
            if proc:
                try:
                    buf, size = api.ctypes.create_unicode_buffer(1024), api.wintypes.DWORD(1024)
                    if api.k32.QueryFullProcessImageNameW(proc, 0, buf, api.ctypes.byref(size)):
                        path = buf.value
                finally:
                    api.k32.CloseHandle(proc)
        except Exception:
            pass
    elif os.path.isdir("/proc"):
        try:
            path = os.readlink("/proc/%d/exe" % pid)
        except OSError:
            try:
                with open("/proc/%d/comm" % pid) as f:
                    path = f.read().strip()
            except OSError:
                pass
    if not path:
        try:
            import psutil
            path = psutil.Process(pid).name()
        except Exception:
            pass
    name = re.split(r"[\\/]", path)[-1] if path else ""
    return {"pid": pid, "name": name, "path": path, "kelvin": pid == os.getpid(), "likely": False}


# ------------------------------------------------------------------ Windows

def _api() -> Any:
    """The Windows calls a look makes (ctypes), bound once."""
    global _API
    if _API is not None:
        return _API
    import ctypes
    from ctypes import wintypes

    class IoStatus(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    class DeviceInfo(ctypes.Structure):   # FILE_FS_DEVICE_INFORMATION
        _fields_ = [("DeviceType", ctypes.c_ulong), ("Characteristics", ctypes.c_ulong)]

    class ProcessEntry(ctypes.Structure):   # PROCESSENTRY32W
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    class ModuleEntry(ctypes.Structure):   # MODULEENTRY32W
        _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("GlblcntUsage", wintypes.DWORD), ("ProccntUsage", wintypes.DWORD),
                    ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE),
                    ("szModule", ctypes.c_wchar * 256), ("szExePath", ctypes.c_wchar * 260)]

    nt = ctypes.WinDLL("ntdll")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ulong_p = ctypes.POINTER(ctypes.c_ulong)
    nt.NtQuerySystemInformation.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ulong_p]
    nt.NtQuerySystemInformation.restype = ctypes.c_long
    nt.NtQueryObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong, ulong_p]
    nt.NtQueryObject.restype = ctypes.c_long
    nt.NtQueryVolumeInformationFile.argtypes = [wintypes.HANDLE, ctypes.POINTER(IoStatus), ctypes.c_void_p,
                                                ctypes.c_ulong, ctypes.c_int]
    nt.NtQueryVolumeInformationFile.restype = ctypes.c_long
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE),
                                    wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.DuplicateHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.QueryDosDeviceW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    k32.QueryDosDeviceW.restype = wintypes.DWORD
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    k32.ProcessIdToSessionId.restype = wintypes.BOOL
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for fn, entry in (("Process32FirstW", ProcessEntry), ("Process32NextW", ProcessEntry),
                      ("Module32FirstW", ModuleEntry), ("Module32NextW", ModuleEntry)):
        getattr(k32, fn).argtypes = [wintypes.HANDLE, ctypes.POINTER(entry)]
        getattr(k32, fn).restype = wintypes.BOOL
    _API = types.SimpleNamespace(ctypes=ctypes, wintypes=wintypes, nt=nt, k32=k32, IoStatus=IoStatus,
                                 DeviceInfo=DeviceInfo, ProcessEntry=ProcessEntry, ModuleEntry=ModuleEntry,
                                 invalid=ctypes.c_void_p(-1).value, ptr=ctypes.sizeof(ctypes.c_void_p))
    return _API


def _file_handles(api: Any) -> List[Tuple[int, int, int]]:
    """Every File handle open on this computer, as (process, handle, object), from NtQuerySystemInformation's extended
    handle list (SystemExtendedHandleInformation, 64), its room grown until the list fits. The File type's number is
    learned from a handle opened here for it, and left out with this look's own duplicates."""
    ctypes = api.ctypes
    mine = api.k32.CreateFileW("NUL", 0x80000000, 0x3, None, 3, 0, None)   # GENERIC_READ, shared, OPEN_EXISTING
    if not mine or mine == api.invalid:
        return []
    try:
        size = 1 << 22
        while True:
            buf = ctypes.create_string_buffer(size)
            need = ctypes.c_ulong()
            status = api.nt.NtQuerySystemInformation(64, buf, size, ctypes.byref(need)) & 0xFFFFFFFF
            if status == 0xC0000004 and size < 1 << 30:   # STATUS_INFO_LENGTH_MISMATCH: more handles than room
                size = max(size * 2, need.value + (1 << 20))
                continue
            if status != 0:
                return []
            break
    finally:
        api.k32.CloseHandle(mine)
    word = "Q" if api.ptr == 8 else "I"
    entry = struct.Struct("<%s%s%sIHHII" % (word, word, word))   # SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX
    count = min(struct.unpack_from("<" + word, buf, 0)[0], (size - 2 * api.ptr) // entry.size)
    raw = memoryview(buf)[2 * api.ptr:2 * api.ptr + count * entry.size]
    me = os.getpid()
    kind = next((e[5] for e in entry.iter_unpack(raw) if e[1] == me and e[2] == mine), None)
    if kind is None:
        return []
    ours = set(_OURS) | {mine}
    return [(e[1], e[2], e[0]) for e in entry.iter_unpack(raw) if e[5] == kind and not (e[1] == me and e[2] in ours)]


def _windows(names: List[str], timeout: float) -> Dict[str, Set[int]]:
    devices: Dict[str, List[str]] = {}
    for n in names:
        device = device_of(n)
        if device:
            devices.setdefault(device.lower(), []).append(n)
    if not devices:
        return {}
    api = _api()
    ctypes, k32 = api.ctypes, api.k32
    rows = _file_handles(api)
    me, here = os.getpid(), k32.GetCurrentProcess()
    session = _session(api, me)
    opened: Dict[int, Any] = {}   # process -> a handle to it (0: one Kelvin may not look into, or doesn't)
    lock = threading.Lock()
    done = []                     # once the look has ended: a step left to itself opens no process

    def step(row: Tuple[int, int, int], found: _Found) -> None:
        pid, handle, _ = row
        with lock:
            if done:
                return
            proc = opened.get(pid)
            if proc is None:
                if pid == me:
                    proc = here
                elif pid in (0, 4) or session is None or _session(api, pid) != session:
                    proc = 0   # only this session's processes (a program a person runs), never a service's (lsass)
                else:
                    proc = k32.OpenProcess(0x40, False, pid) or 0   # PROCESS_DUP_HANDLE
                opened[pid] = proc
        if not proc:
            return
        dup = api.wintypes.HANDLE()
        if not k32.DuplicateHandle(proc, handle, here, ctypes.byref(dup), 0, False, 0x2):   # DUPLICATE_SAME_ACCESS
            return
        _OURS.add(dup.value)
        try:
            io, info = api.IoStatus(), api.DeviceInfo()
            if api.nt.NtQueryVolumeInformationFile(dup, ctypes.byref(io), ctypes.byref(info), ctypes.sizeof(info),
                                                   4) != 0 or info.DeviceType not in ASKED:   # FileFsDeviceInformation
                return
            _WAITING[row] = (pid, info.DeviceType)   # while its name is asked: one left waiting stays (waiting)
            try:
                name = _object_name(api, dup).lower()
            finally:
                _WAITING.pop(row, None)
        finally:
            k32.CloseHandle(dup)
            _OURS.discard(dup.value)
        for device in devices:
            if name == device or name.startswith(device + "\\"):
                found.add((device, pid))
    try:
        hits = _walk(rows, step, _SKIP, timeout, STEP_S)
    finally:
        with lock:
            done.append(True)
            for pid, proc in opened.items():
                if proc and pid != me:
                    k32.CloseHandle(proc)
            opened.clear()
    out: Dict[str, Set[int]] = {n: set() for n in names}
    for device, pid in hits:
        for n in devices[device]:
            out[n].add(pid)
    return out


def _session(api: Any, pid: int) -> Optional[int]:
    """The Windows session a process runs in (0: the services'), or None when Windows doesn't say."""
    sid = api.wintypes.DWORD()
    return int(sid.value) if api.k32.ProcessIdToSessionId(pid, api.ctypes.byref(sid)) else None


def _object_name(api: Any, handle: Any) -> str:
    """A handle's object's name (NtQueryObject, ObjectNameInformation): a device's, \\Device\\VCP0; "" when it has
    none, or it can't be read."""
    ctypes = api.ctypes
    buf = ctypes.create_string_buffer(2048)
    if api.nt.NtQueryObject(handle, 1, buf, len(buf), ctypes.byref(ctypes.c_ulong())) != 0:
        return ""
    length = struct.unpack_from("<H", buf, 0)[0]   # UNICODE_STRING: Length, MaximumLength, then Buffer
    address = struct.unpack_from("<Q" if api.ptr == 8 else "<I", buf, api.ptr)[0]
    return ctypes.wstring_at(address, length // 2) if address and length else ""


def _windows_modules(api: Any, pid: int) -> List[str]:
    """The file names of the modules a process has loaded (Toolhelp32's snapshot, 32-bit ones too), lower case; [] for
    one Kelvin may not look into."""
    k32, ctypes = api.k32, api.ctypes
    for _ in range(3):   # ERROR_BAD_LENGTH while it loads or unloads one: again
        snap = k32.CreateToolhelp32Snapshot(0x8 | 0x10, pid)   # TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32
        if snap and snap != api.invalid:
            break
        if ctypes.get_last_error() != 24:
            return []
    else:
        return []
    out = []
    try:
        entry = api.ModuleEntry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Module32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append(entry.szModule.lower())
            ok = k32.Module32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def _windows_processes(api: Any) -> List[int]:
    k32, ctypes = api.k32, api.ctypes
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
    if not snap or snap == api.invalid:
        return []
    out = []
    try:
        entry = api.ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append(int(entry.th32ProcessID))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


# ------------------------------------------------------------------ Linux

def _linux(names: List[str], timeout: float) -> Dict[str, Set[int]]:
    devices: Dict[int, List[str]] = {}
    for n in names:
        if not n.startswith("/"):
            continue
        try:
            st = os.stat(n)   # through a link (/dev/serial/by-id/...): the device itself
        except (OSError, ValueError):
            continue
        if stat.S_ISCHR(st.st_mode):
            devices.setdefault(st.st_rdev, []).append(n)
    if not devices:
        return {}
    try:
        pids = sorted(int(p) for p in os.listdir("/proc") if p.isdigit())
    except OSError:
        return {}

    def step(pid: int, found: _Found) -> None:
        try:
            fds = os.listdir("/proc/%d/fd" % pid)
        except OSError:   # another user's process, or gone
            return
        for fd in fds:
            try:
                st = os.stat("/proc/%d/fd/%s" % (pid, fd))
            except OSError:
                continue
            if stat.S_ISCHR(st.st_mode) and st.st_rdev in devices:
                found.add((st.st_rdev, pid))
    hits = _walk(pids, step, _SKIP, timeout, STEP_S)
    out: Dict[str, Set[int]] = {n: set() for n in names}
    for device, pid in hits:
        for n in devices[device]:
            out[n].add(pid)
    return out


# ------------------------------------------------------------------ programs with VISA loaded

def visa_users(timeout: float = LOOK_S, names: Optional[Sequence[str]] = None,
               keep: bool = True) -> List[Dict[str, Any]]:
    """The other programs running now that have VISA's library loaded (names: the libraries' file names, without
    what follows a dot after them: VISA_LIBRARIES), as holders gives them: one of them may hold an instrument's VISA
    session. Never Kelvin itself. [] when none, or where this computer can't say (a Mac). A look holds VISA_KEEP_S
    (keep), as each takes about a second on Windows."""
    wanted = tuple(n.lower() for n in (names or VISA_LIBRARIES))
    now = time.monotonic()
    with _visa_lock:
        last = _visa.get(wanted)
        if keep and last is not None and now - last[0] < VISA_KEEP_S:
            return list(last[1])
    try:
        pids = _loading(wanted, timeout)
    except Exception:
        pids = set()
    found = [_who(pid) for pid in sorted(pids) if pid != os.getpid()]
    with _visa_lock:
        _visa[wanted] = (time.monotonic(), found)
    return list(found)


def _has(modules: Iterable[str], wanted: Sequence[str]) -> bool:
    return any(m == n or m.startswith(n + ".") for m in modules for n in wanted)


def _loading(wanted: Sequence[str], timeout: float) -> Set[int]:
    """The processes that have one of the libraries loaded."""
    me = os.getpid()
    if WIN:
        api = _api()
        session = _session(api, me)

        def step(pid: int, found: _Found) -> None:
            if session is not None and _session(api, pid) == session and _has(_windows_modules(api, pid), wanted):
                found.add(pid)   # (only this session's programs: a service's modules, lsass's, are never read)
        return _walk([p for p in _windows_processes(api) if p not in (0, 4, me)], step, set(), timeout, STEP_S)
    if sys.platform.startswith("linux"):
        def step(pid: int, found: _Found) -> None:   # noqa: F811 (one per system)
            try:
                with open("/proc/%d/maps" % pid) as f:
                    loaded = {line.rsplit("/", 1)[-1].strip().lower() for line in f if "/" in line}
            except OSError:
                return
            if _has(loaded, wanted):
                found.add(pid)
        try:
            pids = [int(p) for p in os.listdir("/proc") if p.isdigit() and int(p) != me]
        except OSError:
            return set()
        return _walk(pids, step, set(), timeout, STEP_S)
    return set()
