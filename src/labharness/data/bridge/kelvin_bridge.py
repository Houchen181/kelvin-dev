"""Kelvin's helper for an instrument driven through its maker's library: a Python module or a DLL.

Kelvin starts this script with the Python the lab names (a 32-bit one for a 32-bit DLL, or the Python that has the
maker's package) and talks to it in JSON lines on stdin and stdout. It loads only the module or DLL the card names,
from the folder the lab names (or, for a module, an installed package), and calls only the functions Kelvin asks for.
Kelvin asks only for the functions on the card. Standard-library modules and system DLLs are refused, so a card
cannot reach the operating system through this helper.

Requests (one JSON object per line):
    {"id": 1, "op": "ping"}
    {"id": 2, "op": "load", "path": "C:/attocube/lib", "module": "PyAttoDRY", "object": "AttoDRY", "create": false}
    {"id": 2, "op": "load", "path": "C:/attocube/lib", "dll": "attoDRYLib.dll", "prefix": "AttoDRY_Interface_",
     "convention": "cdecl", "returns": "error", "functions": {"getSampleTemperature": {"out": "float"}}}
    {"id": 3, "op": "call", "name": "getSampleTemperature", "args": []}
    {"id": 3, "op": "call", "name": "sample.getTemperature", "args": []}   (a function on a sub-object)
    {"id": 4, "op": "quit"}
Replies: {"id": 1, "ok": true, "value": ...} or {"id": 1, "ok": false, "error": "..."}.
A call follows sub-objects only when the maker's package made them (attocube's device.sample), never into Python's
own modules or objects, and calls only the maker's own functions: its package's, or its DLL's.
Runs on Python 3.6 and later; standard library only."""
import ctypes
import importlib
import importlib.util
import json
import os
import re
import struct
import sys
import types

DENY_MODULES = {
    "os", "sys", "subprocess", "shutil", "builtins", "importlib", "runpy", "ctypes", "socket", "pathlib", "io",
    "code", "codeop", "pickle", "marshal", "multiprocessing", "threading", "signal", "pty", "webbrowser", "urllib",
    "http", "ftplib", "smtplib", "tempfile", "glob", "posix", "nt", "msvcrt", "winreg", "_winapi", "asyncio",
    "concurrent", "pdb", "inspect", "types", "gc", "atexit", "platform", "zipimport", "pkgutil", "site", "sysconfig",
}
DENY_DLLS = {"kernel32", "kernelbase", "user32", "advapi32", "msvcrt", "ucrtbase", "ntdll", "shell32", "ws2_32",
             "ole32", "oleaut32", "libc", "libsystem", "libdl", "libpthread", "python3", "vcruntime140"}
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
PATH = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)*$")   # a function, or sample.getTemperature
TYPES = {
    "int8": ctypes.c_int8, "uint8": ctypes.c_uint8, "int16": ctypes.c_int16, "uint16": ctypes.c_uint16,
    "int32": ctypes.c_int32, "int": ctypes.c_int, "uint32": ctypes.c_uint32, "int64": ctypes.c_int64,
    "uint64": ctypes.c_uint64, "float": ctypes.c_float, "double": ctypes.c_double, "bool": ctypes.c_bool,
}
TEXT_OUT = 1024   # bytes for a string an instrument writes back (char buf[], int32 len)


class Refused(Exception):
    pass


def _inside(path, folder):
    path, folder = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(folder))
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _site_dirs():
    out = []
    for p in sys.path:
        if p and os.path.basename(p.rstrip("/\\")).lower() in ("site-packages", "dist-packages"):
            out.append(p)
    return out


def _plain(value):
    """What the library returned, as JSON: numbers, text, true/false, lists of them."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").rstrip("\x00")
    if hasattr(value, "tolist"):   # numpy arrays and scalars
        return _plain(value.tolist())
    if hasattr(value, "value") and not callable(value.value):   # ctypes numbers
        return _plain(value.value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return str(value)


def _word(text):
    """An argument from a card command: a number when it reads as one, otherwise the text."""
    if not isinstance(text, str):
        return text
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


class Library:
    def __init__(self):
        self.target = None   # a module, a class, an instance (Python), or a DLL
        self.packages = set()   # the maker's packages: a call goes only through their objects, to their functions
        self.dll = None
        self.spec = {}

    def load(self, req):
        path = req.get("path") or ""
        if path:
            if not os.path.isdir(path):
                raise Refused("the folder %s does not exist" % path)
            sys.path.insert(0, path)
            os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(os.path.realpath(path))
        if req.get("dll"):
            return self._load_dll(req, path)
        return self._load_module(req, path)

    def _load_module(self, req, path):
        name = str(req.get("module") or "")
        if not all(NAME.match(p) for p in name.split(".")) or name.split(".")[0] in DENY_MODULES:
            raise Refused("%r is not a maker's module" % name)
        spec = importlib.util.find_spec(name)
        if spec is None or not spec.origin or spec.origin in ("built-in", "frozen"):
            raise Refused("no module %s in %s or among the installed packages" % (name, path or "the Python path"))
        if not ((path and _inside(spec.origin, path)) or any(_inside(spec.origin, d) for d in _site_dirs())):
            raise Refused("%s is part of Python itself, not a maker's library" % name)
        target = importlib.import_module(name)
        self.packages = {name.split(".")[0]}
        if req.get("object"):
            if not NAME.match(str(req["object"])):
                raise Refused("%r is not a name" % req["object"])
            target = getattr(target, req["object"])
            cls = target if isinstance(target, type) else type(target)   # a class the module re-exports is the maker's too
            if not isinstance(target, types.ModuleType):
                self.packages.add(str(getattr(cls, "__module__", "") or "").split(".")[0])
            if req.get("create"):
                target = target(*[_word(a) for a in req.get("create_args") or []])
        self.target = target
        return {"module": name, "file": spec.origin}

    def _load_dll(self, req, path):
        if not path:
            raise Refused("a DLL needs the folder it is in (path= in the address)")
        file = os.path.join(path, str(req["dll"]))
        if not _inside(file, path) or not os.path.isfile(file):
            raise Refused("no %s in %s" % (req["dll"], path))
        stem = os.path.splitext(os.path.basename(file))[0].lower()
        if stem in DENY_DLLS or stem.startswith("python"):
            raise Refused("%s is a system library, not a maker's" % req["dll"])
        if req.get("convention") == "stdcall" and hasattr(ctypes, "WinDLL"):
            self.dll = ctypes.WinDLL(file)
        else:
            self.dll = ctypes.CDLL(file)
        self.spec = {"prefix": str(req.get("prefix") or ""), "returns": req.get("returns") or "error",
                     "functions": dict(req.get("functions") or {})}
        return {"dll": file, "functions": len(self.spec["functions"])}

    def call(self, name, args):
        if not PATH.match(str(name)):
            raise Refused("%r is not a function name" % name)
        if self.dll is not None:
            return self._call_dll(name, args)
        if self.target is None:
            raise Refused("nothing is loaded")
        parts, obj = str(name).split("."), self.target
        for i, part in enumerate(parts[:-1]):   # a sub-object of the library's object: device.sample
            obj = getattr(obj, part, None)
            if obj is None or not self._makers(obj):
                raise Refused("%s is not part of the maker's library" % ".".join(parts[:i + 1]))
        fn = getattr(obj, parts[-1], None)
        if fn is None or not callable(fn):
            raise Refused("the library has no function %s" % name)
        if not (isinstance(fn, ctypes._CFuncPtr) or self._mine(getattr(fn, "__module__", None))):
            raise Refused("%s is not one of the maker's functions" % name)   # e.g. one it imported from Python
        return _plain(fn(*[_word(a) for a in args]))

    def _mine(self, module):
        return str(module or "").split(".")[0] in self.packages

    def _makers(self, obj):
        """The maker's own: a module of its package, or an object of a class its package defines. Not Python's own
        modules or objects, through which a call could reach the operating system."""
        return self._mine(obj.__name__ if isinstance(obj, types.ModuleType) else getattr(type(obj), "__module__", ""))

    def _call_dll(self, name, args):
        sig = self.spec["functions"].get(name)
        if sig is None:
            raise Refused("%s is not among the card's functions" % name)
        fn = getattr(self.dll, self.spec["prefix"] + name)
        kinds = list(sig.get("args") or [])
        if len(args) != len(kinds):
            raise Refused("%s takes %d arguments, got %d" % (name, len(kinds), len(args)))
        values, types = [], []
        for kind, a in zip(kinds, args):
            if kind == "str":
                types.append(ctypes.c_char_p)
                values.append(str(a).encode("utf-8"))
            else:
                t = TYPES[kind]
                types.append(t)
                values.append(t(_number(a, kind)))
        out, buf = sig.get("out"), None
        if out == "str":
            buf = ctypes.create_string_buffer(TEXT_OUT)
            types += [ctypes.c_char_p, ctypes.c_int32]
            values += [buf, TEXT_OUT]
        elif out:
            buf = TYPES[out]()
            types.append(ctypes.POINTER(TYPES[out]))
            values.append(ctypes.byref(buf))
        returns = sig.get("ret") or self.spec["returns"]
        fn.argtypes = types
        fn.restype = None if returns == "none" else (ctypes.c_int32 if returns == "error" else TYPES[returns])
        rc = fn(*values)
        if returns == "error" and rc != 0:
            raise RuntimeError("%s returned error %d" % (name, rc))
        if out:
            return _plain(buf.value)
        return None if returns in ("error", "none") else _plain(rc)


def _number(text, kind):
    if kind == "bool":
        return str(text).strip().lower() in ("1", "true", "on", "yes")
    if kind in ("float", "double"):
        return float(text)
    return int(float(text))


def serve(inp, out):
    lib = Library()
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line.decode("utf-8") if isinstance(line, bytes) else line)
        except ValueError:
            continue
        rid, op = req.get("id"), req.get("op")
        try:
            if op == "ping":
                value = {"python": sys.version.split()[0], "bits": struct.calcsize("P") * 8}
            elif op == "load":
                value = lib.load(req)
            elif op == "call":
                value = lib.call(req.get("name"), list(req.get("args") or []))
            elif op == "quit":
                out.write((json.dumps({"id": rid, "ok": True, "value": None}) + "\n").encode("utf-8"))
                out.flush()
                return
            else:
                raise Refused("unknown request %r" % op)
            reply = {"id": rid, "ok": True, "value": value}
        except Exception as e:   # the library's own errors go back to Kelvin as text
            reply = {"id": rid, "ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        try:
            text = json.dumps(reply)
        except (TypeError, ValueError):
            text = json.dumps({"id": rid, "ok": True, "value": str(reply.get("value"))})
        out.write((text + "\n").encode("utf-8"))
        out.flush()


def main():
    # replies go on the real stdout; anything the library prints (Python or C) goes to stderr instead
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    serve(os.fdopen(0, "rb"), out)


if __name__ == "__main__":
    main()
