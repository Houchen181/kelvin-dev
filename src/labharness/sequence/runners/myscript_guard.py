"""
myscript_guard - the instrument guard an analysis script of yours runs under (0.1.7).

Your scripts in my_analysis/ are run by the panel straight from History. An analysis only reads dataset folders
and writes figures; it never needs an instrument. A script copied from a notebook can still carry the line that
opens an instrument, and run while a measurement holds it. This module refuses exactly that, three ways:

  1. importing an instrument-driver module or the panel's own runtime (a meta-path finder on the top-level name, so
     `import pyvisa`, `from serial.tools import list_ports` and `importlib.import_module('pyvisa')` are all
     refused). The panel's own modules (every panel_*, run_block) are refused under ANY dotted path as well, so they
     cannot be reached through a package path past a top-level check. Your own driver modules come on top from
     config.json my_analysis.guard_modules (the engine hands them over in LABMASTER_GUARD_MODULES);
  2. loading a hardware DLL through ctypes (CDLL / WinDLL / OleDLL / cdll.X / windll.X / LoadLibrary all end in
     ctypes._dlopen, which is wrapped), by base name - the Windows spellings of the same file ('visa64.dll.',
     'visa64.dll ', 'visa64.dll::$DATA') included. Your own DLL names (fnmatch patterns) come on top from
     my_analysis.guard_dlls (LABMASTER_GUARD_DLLS);
  3. socket connections (connect / connect_ex / sendto / create_connection) to any address that is not this
     computer, and to the local port(s) of the panel itself, which the engine hands over as run_myscript.py
     --guard-ports and this module reads from LABMASTER_GUARD_PORTS.

Every refusal raises with the message
    blocked by the panel's instrument guard: <what>
- an ImportError subclass for an import (so a library's own `try: import serial / except ImportError` keeps
working), a PermissionError (an OSError) for a DLL or a socket.

A SAFETY NET, NOT A SANDBOX. Python code that wants to get round it can (it can load a DLL by pywin32, exec a module
file by path, use the asyncio proactor's ConnectEx, or start a program that is not Python). It stops the accident -
the notebook line nobody remembered - not intent.

A child Python process is guarded too: the runner puts runners/guard_site first on PYTHONPATH and sets
LABMASTER_GUARD=1, and guard_site/sitecustomize.py installs this module at the child's start-up. Not covered: a child
started with -I / -E / -S (it ignores PYTHONPATH or site), a child given an env= without PYTHONPATH, and any program
that is not Python.

What it must NOT break: the runner's own work (files, json, matplotlib, subprocess of Python itself), numpy / scipy /
pandas / matplotlib, labmaster_analysis and labmaster_dataset. None of those imports a module on the lists below.
"""
import fnmatch
import importlib.abc
import ipaddress
import os
import sys

MESSAGE = "blocked by the panel's instrument guard: %s"

# 1. Modules, by top-level name (compared lower case): common, vendor-neutral instrument-access packages (VISA,
# serial ports, USB, FTDI, .NET bridges used to reach vendor drivers, instrument frameworks) and the panel's own
# runtime (the panel runs the analysis, never the other way round). Network, window and process modules are NOT
# refused: loopback sockets and a subprocess of Python are allowed, and matplotlib / pandas import some of those
# modules themselves; the dangerous calls are caught instead (2 and 3 below). A lab adds the names of its own
# driver packages in config.json my_analysis.guard_modules.
BLOCKED_MODULES = frozenset(m.lower() for m in (
    # instrument access
    'pyvisa', 'visa', 'pyvisa_py', 'serial', 'usb', 'ftd2xx', 'pylablib', 'instrumental',
    'clr', 'pythonnet', 'clr_loader',
    # the panel's own runtime
    'panel_engine', 'panel_server', 'run_block',
))
BLOCKED_MODULE_PREFIXES = ('panel_',)                     # every panel_* module

# Refused as ANY component of a dotted name, not only the first: the panel's modules, which could otherwise be
# reached through a package path (runners.run_block).
PANEL_MODULES = frozenset(('run_block', 'run_myscript', 'myscript_guard'))
PANEL_MODULE_PREFIXES = BLOCKED_MODULE_PREFIXES

# 2. DLLs, by base name without the extension (compared lower case, fnmatch patterns): VISA and FTDI USB serial.
# A lab adds its own in config.json my_analysis.guard_dlls.
BLOCKED_DLLS = tuple(p.lower() for p in (
    'visa32', 'visa64', '*visa32*', '*visa64*', 'ftd2xx', 'ftd2xx64', 'ftd2xx*',
))

# 3. Loopback ports refused besides any other host: the panel's own port(s), from LABMASTER_GUARD_PORTS (the engine
# passes them as run_myscript.py --guard-ports; inherited by a child Python, whose sitecustomize installs this
# module and so reads the same variable).
ENV_PORTS = 'LABMASTER_GUARD_PORTS'
ENV_MODULES = 'LABMASTER_GUARD_MODULES'
ENV_DLLS = 'LABMASTER_GUARD_DLLS'
_EXTRA = frozenset()            # set by install() from LABMASTER_GUARD_PORTS
_EXTRA_MODULES = frozenset()    # set by install() from LABMASTER_GUARD_MODULES
_EXTRA_DLLS = ()                # set by install() from LABMASTER_GUARD_DLLS


def _extra_ports(env=None):
    """The refused loopback ports in LABMASTER_GUARD_PORTS (comma-separated integers 1..65535; anything else in the
    list is ignored), as a frozenset."""
    env = os.environ if env is None else env
    got = set()
    for tok in str(env.get(ENV_PORTS) or '').split(','):
        tok = tok.strip()
        if tok.isascii() and tok.isdigit() and 1 <= int(tok) <= 65535:
            got.add(int(tok))
    return frozenset(got)


def _extra_names(var, env=None):
    """The names in a comma-separated variable, lower case, empty ones left out."""
    env = os.environ if env is None else env
    return [t.strip().lower() for t in str(env.get(var) or '').split(',') if t.strip()]


class GuardImportError(ImportError):
    """An instrument module was imported under the guard."""


class GuardError(PermissionError):
    """A hardware DLL or a network connection was refused by the guard."""


def _msg(what):
    return MESSAGE % what


# --------------------------------------------------------------------------- the rules
def module_blocked(fullname):
    """True when importing `fullname` (any dotted name) is refused: its top-level name is on the list (or among the
    extra names - a dotted extra name refuses that package path and everything under it) or starts with a refused
    prefix, or any of its components is one of the panel's modules."""
    full = str(fullname or '').lower()
    parts = full.split('.')
    top = parts[0]
    if top in BLOCKED_MODULES or top.startswith(BLOCKED_MODULE_PREFIXES):
        return True
    for x in _EXTRA_MODULES:
        if full == x or full.startswith(x + '.'):
            return True
    return any(p in PANEL_MODULES or p.startswith(PANEL_MODULE_PREFIXES) for p in parts[1:])


def dll_blocked(name):
    """True when a library `name` handed to ctypes (a bare name, a file name or a full path, str / bytes / PathLike,
    with or without .dll) is a refused hardware DLL."""
    if name is None:
        return False
    try:
        name = os.fsdecode(name)
    except TypeError:
        return False
    base = os.path.basename(name.replace('/', os.sep).replace('\\', os.sep))
    # Windows reaches the same file under other spellings: 'x.dll::$DATA' / 'x.dll:stream' (an NTFS stream suffix;
    # basename has already taken any drive letter off) and 'x.dll.' / 'x.dll ' (Win32 drops trailing dots and
    # spaces). Refusing 'x.' too, which the loader would not find, costs nothing.
    base = base.split(':', 1)[0].rstrip(' .').lower()
    for ext in ('.dll', '.so', '.dylib'):
        if base.endswith(ext):
            base = base[:-len(ext)].rstrip(' .')
            break
    return any(fnmatch.fnmatchcase(base, pat) for pat in BLOCKED_DLLS + _EXTRA_DLLS)


def address_blocked(address):
    """None when connecting to `address` is allowed, else the text that says what was refused. Allowed: this
    computer (a loopback IP literal or 'localhost'; '' and 0.0.0.0 reach it too) except the panel's own ports. A host
    name other than localhost is refused without resolving it: a name can point anywhere, and an analysis has no
    reason to use one."""
    if not isinstance(address, tuple) or len(address) < 2:
        return None                               # AF_UNIX path or something not IP: allowed
    host, port = address[0], address[1]
    if isinstance(host, bytes):
        host = host.decode('ascii', 'replace')
    host = str(host).strip()
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = None
    where = '%s:%s' % ('[%s]' % host if ':' in host else host, port)
    h = host.strip('[]').split('%', 1)[0]         # [::1], fe80::1%eth0
    local = h.lower() in ('localhost', '', '0.0.0.0', '::')
    if not local:
        try:
            ip = ipaddress.ip_address(h)
            ip = getattr(ip, 'ipv4_mapped', None) or ip        # ::ffff:127.0.0.1 is 127.0.0.1
            local = ip.is_loopback
        except ValueError:
            return 'a connection to %s (a host name; only this computer is allowed)' % where
    if not local:
        return 'a connection to %s (only this computer is allowed)' % where
    if port in _EXTRA:
        return 'a connection to %s (the panel)' % where
    return None


# --------------------------------------------------------------------------- 1. imports
class _Finder(importlib.abc.MetaPathFinder):
    """First on sys.meta_path: refuses a listed module before any other finder looks for it. Only modules not imported
    yet reach a finder - which is every one of them, because the runner installs this before the script runs and
    sitecustomize before anything else."""
    labmaster_guard = True

    def find_spec(self, fullname, path=None, target=None):
        if module_blocked(fullname):
            raise GuardImportError(_msg('import %s' % fullname), name=fullname)
        return None

    def invalidate_caches(self):
        pass


# --------------------------------------------------------------------------- 2. ctypes
def _wrap_dlopen(orig):
    def guarded_dlopen(name, *args, **kwargs):
        if dll_blocked(name):
            raise GuardError(_msg('loading %s' % os.path.basename(os.fsdecode(name))))
        return orig(name, *args, **kwargs)
    guarded_dlopen.labmaster_guard = True
    guarded_dlopen.__wrapped__ = orig
    return guarded_dlopen


def _install_ctypes():
    try:
        import ctypes
        import _ctypes
    except ImportError:
        return False
    # CDLL.__init__ (and so WinDLL, OleDLL, PyDLL, cdll.X, windll.X, LoadLibrary) calls the module global
    # ctypes._dlopen at call time; _ctypes.LoadLibrary (dlopen on POSIX) is the same function reached directly.
    if not getattr(ctypes._dlopen, 'labmaster_guard', False):
        ctypes._dlopen = _wrap_dlopen(ctypes._dlopen)
    for attr in ('LoadLibrary', 'dlopen'):
        f = getattr(_ctypes, attr, None)
        if f is not None and not getattr(f, 'labmaster_guard', False):
            setattr(_ctypes, attr, _wrap_dlopen(f))
    return True


# --------------------------------------------------------------------------- 3. sockets
def _install_socket():
    import socket
    cls = socket.socket
    if getattr(cls.connect, 'labmaster_guard', False):
        return True

    def check(address):
        why = address_blocked(address)
        if why:
            raise GuardError(_msg(why))

    def wrap(meth, pos):
        def guarded(self, *args, **kwargs):
            if len(args) > pos:
                check(args[pos])
            elif 'address' in kwargs:
                check(kwargs['address'])
            return meth(self, *args, **kwargs)
        guarded.labmaster_guard = True
        guarded.__wrapped__ = meth
        guarded.__name__ = getattr(meth, '__name__', 'guarded')
        return guarded

    cls.connect = wrap(cls.connect, 0)
    cls.connect_ex = wrap(cls.connect_ex, 0)
    # sendto(data, address) / sendto(data, flags, address): the address is the last argument
    orig_sendto = cls.sendto

    def sendto(self, *args):
        if len(args) >= 2:
            check(args[-1])
        return orig_sendto(self, *args)
    sendto.labmaster_guard = True
    cls.sendto = sendto

    orig_cc = socket.create_connection

    def create_connection(address, *args, **kwargs):
        check(address)                       # before any name lookup
        return orig_cc(address, *args, **kwargs)
    create_connection.labmaster_guard = True
    create_connection.__wrapped__ = orig_cc
    socket.create_connection = create_connection
    return True


# --------------------------------------------------------------------------- install
def installed():
    """True when the import finder is in place in this process."""
    return any(getattr(f, 'labmaster_guard', False) for f in sys.meta_path)


def install():
    """Install all three parts in this process. Idempotent; never raises for a part it cannot install (a Python
    without ctypes). The refused ports and the extra module / DLL names are read from the environment here (each
    call reads them again; a name once refused stays refused). Returns True."""
    global _EXTRA, _EXTRA_MODULES, _EXTRA_DLLS
    _EXTRA = _EXTRA | _extra_ports()
    _EXTRA_MODULES = _EXTRA_MODULES | frozenset(_extra_names(ENV_MODULES))
    _EXTRA_DLLS = tuple(dict.fromkeys(_EXTRA_DLLS + tuple(
        (n[:-4] if n.endswith('.dll') else n) for n in _extra_names(ENV_DLLS))))
    if not installed():
        sys.meta_path.insert(0, _Finder())
    try:
        _install_ctypes()
    except Exception:                                              # noqa: BLE001
        pass
    _install_socket()
    return True


def site_dir():
    """The folder whose sitecustomize.py guards a child Python (first on its PYTHONPATH)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'guard_site')


def export_to_children(environ=None):
    """Make every child Python of this process guarded too: guard_site first on PYTHONPATH and LABMASTER_GUARD=1 in
    `environ` (os.environ by default). LABMASTER_GUARD_PORTS / _MODULES / _DLLS are inherited as they are."""
    env = os.environ if environ is None else environ
    d = site_dir()
    old = [p for p in (env.get('PYTHONPATH') or '').split(os.pathsep)
           if p and os.path.normcase(os.path.abspath(p)) != os.path.normcase(d)]
    env['PYTHONPATH'] = os.pathsep.join([d] + old)
    env['LABMASTER_GUARD'] = '1'
    return env
