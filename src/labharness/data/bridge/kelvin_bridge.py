"""Kelvin's helper for an instrument driven through its maker's library: a Python module, a DLL, a .NET assembly
or a COM object.

Kelvin starts this script with the Python the lab names (a 32-bit one for a 32-bit DLL, or the Python that has the
maker's package) and talks to it in JSON lines on stdin and stdout. It loads only the module, DLL or assembly the card
names, from the folder the lab names (or, for a module, an installed package), and calls only the functions Kelvin
asks for. Kelvin asks only for the functions on the card. Standard-library modules, system DLLs, the .NET platform's
own namespaces and Windows' and Microsoft's own COM servers are refused, so a card cannot reach the operating system
through this helper.

Requests (one JSON object per line):
    {"id": 1, "op": "ping"}
    {"id": 2, "op": "load", "path": "C:/attocube/lib", "module": "PyAttoDRY", "object": "AttoDRY", "create": false}
    {"id": 2, "op": "load", "path": "C:/attocube/lib", "dll": "attoDRYLib.dll", "prefix": "AttoDRY_Interface_",
     "convention": "cdecl", "returns": "error", "functions": {"getSampleTemperature": {"out": "float"}}}
    {"id": 2, "op": "load", "path": "C:/Program Files/Thorlabs/Kinesis", "assembly": ["...DeviceManagerCLI.dll",
     "...KCube.DCServoCLI.dll"], "type": "Thorlabs.MotionControl.KCube.DCServoCLI.KCubeDCServo",
     "setup": ["Thorlabs.MotionControl.DeviceManagerCLI.DeviceManagerCLI.BuildDeviceList"],
     "create": "CreateKCubeDCServo", "create_args": ["27000001"]}                    (.NET, through pythonnet)
    {"id": 2, "op": "load", "com": "WinX32.ExpSetup"}                               (COM, through pywin32)
    {"id": 3, "op": "call", "name": "getSampleTemperature", "args": []}
    {"id": 3, "op": "call", "name": "sample.getTemperature", "args": []}   (a function on a sub-object)
    {"id": 3, "op": "read", "name": "Position"}                          (a property, read without calling)
    {"id": 3, "op": "assign", "name": "Exposure", "value": "0.1"}         (a property, set)
    {"id": 4, "op": "describe", "path": "...", "module": "atto_device"}   (its classes and functions; nothing called)
    {"id": 5, "op": "quit"}
Replies: {"id": 1, "ok": true, "value": ...} or {"id": 1, "ok": false, "error": "..."}.
A call follows sub-objects only when the maker's package made them (attocube's device.sample), never into Python's
own modules or objects, and calls only the maker's own functions: its package's, or its DLL's, its assembly's types',
or its COM object's. Arguments to .NET methods are converted to their parameters' types (a position as a Decimal).
Runs on Python 3.6 and later; standard library only, and pythonnet or pywin32 for .NET or COM."""
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
# the .NET platform's own namespaces, and members every .NET object has that reach the platform
DENY_NET = {"System", "Microsoft", "Windows", "Mono", "Python", "clr", "Internal", "MS", "Accessibility", "FSharp"}
DENY_MEMBERS = {"GetType", "InvokeMember", "MemberwiseClone", "Finalize", "ReferenceEquals"}
# COM servers that are Windows' or Microsoft's own: they run scripts and programs, and reach files and the web
DENY_COM = ("wscript.", "scripting.", "shell.", "msxml", "winhttp", "adodb.", "excel.", "word.", "outlook.",
            "powerpoint.", "access.", "internetexplorer.", "htmlfile", "schedule.", "wbemscripting.", "msscriptcontrol.",
            "vbscript", "jscript", "cdo.", "mapi.", "sapi.", "microsoft.", "wia.", "shell.application")
DENY_SERVERS = ("mshta.exe", "wscript.exe", "cscript.exe", "powershell.exe", "pwsh.exe", "cmd.exe", "rundll32.exe",
                "regsvr32.exe", "explorer.exe", "scrrun.dll", "wshom.ocx", "msxml3.dll", "msxml6.dll")
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
    if type(value).__module__ == "System" and type(value).__name__ == "Decimal":   # .NET: a position, a voltage
        import System
        return float(System.Decimal.ToDouble(value))
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


def _site(path):
    """A site-packages folder as Python's site module reads it: the folders its .pth files name and a package's own
    start-up line (pywin32 adds its DLL folder), so the packages installed there import."""
    for name in sorted(os.listdir(path)):
        if not name.endswith(".pth"):
            continue
        try:
            with open(os.path.join(path, name), encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "_virtualenv" in line or "_distutils_hack" in line:
                continue
            if line.startswith(("import ", "import\t")):
                try:
                    exec(line)
                except Exception:
                    pass
                continue
            folder = line if os.path.isabs(line) else os.path.join(path, line)
            if os.path.isdir(folder) and folder not in sys.path:
                sys.path.append(folder)


def _split(text):
    """'Connect 27000001' -> ('Connect', ['27000001'])."""
    parts = [p for p in re.split(r"[\s,]+", str(text).strip()) if p]
    if not parts or not PATH.match(parts[0]):
        raise Refused("%r is not a function and its arguments" % text)
    return parts[0], parts[1:]


INTS = {"System.Int16", "System.Int32", "System.Int64", "System.UInt16", "System.UInt32", "System.UInt64",
        "System.Byte", "System.SByte"}


def _net_value(text, ptype):
    """A card's argument as the .NET parameter wants it: a Decimal for a position, an enum by its name."""
    import System
    name = str(ptype.FullName)
    if name == "System.String":
        return str(text)
    if name == "System.Boolean":
        return str(text).strip().lower() in ("1", "1.0", "true", "on", "yes")
    if name == "System.Decimal":
        return System.Decimal.Parse(repr(float(text)), System.Globalization.CultureInfo.InvariantCulture)
    if name in ("System.Double", "System.Single"):
        return float(text)
    if name in INTS:
        return int(float(text))
    if ptype.IsEnum:
        return System.Enum.Parse(ptype, str(text))
    raise TypeError("a %s argument" % name)


def _com_server(progid):
    """The program or DLL that serves a COM ProgID, from the registry ('' when it isn't registered)."""
    import winreg

    def value(path):
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, path) as k:
                return winreg.QueryValue(k, None)
        except OSError:
            return ""
    clsid = value(progid + "\\CLSID") or value(value(progid + "\\CurVer") + "\\CLSID")
    if not clsid:
        return ""
    return value("CLSID\\%s\\InprocServer32" % clsid) or value("CLSID\\%s\\LocalServer32" % clsid)


class Library:
    def __init__(self):
        self.target = None   # a module, a class, an instance (Python), or a DLL
        self.packages = set()   # the maker's packages: a call goes only through their objects, to their functions
        self.dll = None
        self.spec = {}
        self.kind = "python"   # or dll, dotnet, com

    def load(self, req):
        path = req.get("path") or ""
        if path:
            if not os.path.isdir(path):
                raise Refused("the folder %s does not exist" % path)
            sys.path.insert(0, path)
            os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(os.path.realpath(path))
            if os.path.basename(path.rstrip("/\\")).lower() == "site-packages":
                _site(path)
        if req.get("dll"):
            return self._load_dll(req, path)
        if req.get("assembly"):
            return self._load_dotnet(req, path)
        if req.get("com"):
            return self._load_com(req)
        return self._load_module(req, path)

    # -------------------------------------------------------------- .NET (pythonnet) and COM (pywin32)

    def _assemblies(self, req, path):
        if not path:
            raise Refused("a .NET assembly needs the folder it is in (path= in the address, or an environment's files)")
        import clr   # pythonnet
        loaded = []
        for a in req["assembly"] if isinstance(req["assembly"], list) else [req["assembly"]]:
            file = os.path.join(path, str(a))
            if os.path.basename(file).split(".")[0] in DENY_NET:
                raise Refused("%s is the .NET platform's own, not a maker's" % a)
            if not _inside(file, path) or not os.path.isfile(file) or not file.lower().endswith(".dll"):
                raise Refused("no assembly %s in %s" % (a, path))
            loaded.append(clr.AddReference(os.path.splitext(os.path.basename(file))[0]))
        return loaded

    def _net_member(self, dotted):
        """A maker's .NET namespace, type, or a type's static member, by its full name."""
        parts = str(dotted).split(".")
        if not PATH.match(str(dotted)) or parts[0] in DENY_NET or parts[0] not in self.packages:
            raise Refused("%s is not in the maker's assemblies" % dotted)
        for cut in range(len(parts) - 1, 0, -1):   # the longest namespace that imports, then its types and members
            try:
                obj = importlib.import_module(".".join(parts[:cut]))
            except ImportError:
                continue
            for p in parts[cut:]:
                obj = getattr(obj, p)
            return obj
        raise Refused("no %s in the maker's assemblies" % dotted)

    def _net_invoke(self, owner, member, args):
        """A .NET method, with the card's arguments converted to the parameters of the overload they fit."""
        import clr
        if member in DENY_MEMBERS:
            raise Refused("%s reaches the .NET platform, not the maker's library" % member)
        clr_type = clr.GetClrType(owner) if isinstance(owner, type) else owner.GetType()
        fn = getattr(owner, member)
        tried = [m for m in clr_type.GetMethods() if m.Name == member and len(m.GetParameters()) == len(args)]
        for m in tried:
            try:
                values = [_net_value(a, p.ParameterType) for a, p in zip(args, m.GetParameters())]
            except Exception:
                continue
            return fn(*values)
        raise Refused("%s takes no %d arguments like %s" % (member, len(args), list(args)) if tried else
                      "%s has no method %s with %d arguments" % (clr_type.Name, member, len(args)))

    def _load_dotnet(self, req, path):
        self._assemblies(req, path)
        name = str(req.get("type") or "")
        self.kind, self.packages = "dotnet", {name.split(".")[0]}
        cls = self._net_member(name)
        for step in req.get("setup") or []:   # static calls the examples make first (DeviceManagerCLI.BuildDeviceList)
            fn, args = _split(step)
            owner, _, member = fn.rpartition(".")
            self._net_invoke(self._net_member(owner), member, args)
        create, args = req.get("create"), [_word(a) for a in req.get("create_args") or []]
        if isinstance(create, str) and create:   # a factory: KCubeDCServo.CreateKCubeDCServo(serial)
            self.target = self._net_invoke(cls, create, args)
        elif create:
            self.target = cls(*args)
        else:
            self.target = cls   # its static members
        return {"type": name}

    def _load_com(self, req):
        progid = str(req.get("com") or "")
        if not re.match(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+){1,3}$", progid) or \
                progid.lower().startswith(DENY_COM):
            raise Refused("%r is not a maker's COM object" % progid)
        server = _com_server(progid)
        low = server.strip().strip('"').lower()
        windows = os.environ.get("SystemRoot", r"C:\Windows").lower()
        if not server:
            raise Refused("no COM object %s is registered on this computer: its maker's software registers it" % progid)
        if low.startswith(windows) or "\\microsoft" in low or os.path.basename(low.split('"')[0]).split()[0] in \
                DENY_SERVERS:
            raise Refused("%s is Windows' or Microsoft's own (%s), not a maker's" % (progid, server))
        import win32com.client   # pywin32
        self.kind, self.target = "com", win32com.client.Dispatch(progid)
        return {"com": progid, "server": server}

    def _resolve(self, name):
        """The object a name's last part belongs to, through the maker's own sub-objects only."""
        if not PATH.match(str(name)):
            raise Refused("%r is not a function name" % name)
        if self.target is None:
            raise Refused("nothing is loaded")
        parts, obj = str(name).split("."), self.target
        for i, part in enumerate(parts[:-1]):   # a sub-object of the library's object: device.sample
            obj = getattr(obj, part, None)
            if obj is None or not self._makers(obj):
                raise Refused("%s is not part of the maker's library" % ".".join(parts[:i + 1]))
        if parts[-1] in DENY_MEMBERS:
            raise Refused("%s reaches the platform, not the maker's library" % parts[-1])
        return obj, parts[-1]

    def read(self, name):
        """A property, read without calling anything (a .NET or COM property, or a Python attribute)."""
        obj, attr = self._resolve(name)
        if not hasattr(obj, attr):
            raise Refused("the library has no property %s" % name)
        value = getattr(obj, attr)
        if callable(value):
            raise Refused("%s is a function, not a property: the card calls it with GET" % name)
        return _plain(value)

    def assign(self, name, value):
        """A property set: an exposure, an enable flag."""
        obj, attr = self._resolve(name)
        if not hasattr(obj, attr) or callable(getattr(obj, attr)):
            raise Refused("the library has no property %s" % name)
        if self.kind == "dotnet":
            clr_type = obj.GetType()
            prop = clr_type.GetProperty(attr)
            if prop is None or not prop.CanWrite:
                raise Refused("%s cannot be set" % name)
            setattr(obj, attr, _net_value(value, prop.PropertyType))
        else:
            setattr(obj, attr, _word(value))
        return None

    def _makers_module(self, name, path):
        """The maker's module by name, imported: from the lab's folder or an installed package, never Python's own."""
        if not all(NAME.match(p) for p in name.split(".")) or name.split(".")[0] in DENY_MODULES:
            raise Refused("%r is not a maker's module" % name)
        spec = importlib.util.find_spec(name)
        if spec is None or not spec.origin or spec.origin in ("built-in", "frozen"):
            raise Refused("no module %s in %s or among the installed packages" % (name, path or "the Python path"))
        if not ((path and _inside(spec.origin, path)) or any(_inside(spec.origin, d) for d in _site_dirs())):
            raise Refused("%s is part of Python itself, not a maker's library" % name)
        return importlib.import_module(name), spec

    def describe(self, req, limit=400):
        """The maker's package as its programmer sees it: its modules, classes (with their methods and properties)
        and functions, with their arguments and the first line of their documentation. Nothing is called, and no
        object is made."""
        import inspect
        import pkgutil
        path = req.get("path") or ""
        if path and os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
            if os.path.basename(path.rstrip("/\\")).lower() == "site-packages":
                _site(path)
        if req.get("assembly"):
            return self._describe_dotnet(req, path, limit)
        if req.get("com"):
            return self._describe_com(req, limit)
        name = str(req.get("module") or "")
        root, _ = self._makers_module(name, path)
        top, lines, problems = name.split(".")[0], [], []

        def sig(fn):
            try:
                return str(inspect.signature(fn))
            except (TypeError, ValueError):
                return "(...)"

        def doc(obj):
            text = (inspect.getdoc(obj) or "").strip()
            return "  # " + text.splitlines()[0][:150] if text else ""

        modules = [root]
        if hasattr(root, "__path__"):   # its submodules too: attocube's device.sample is defined in one
            for info in pkgutil.walk_packages(root.__path__, root.__name__ + ".",
                                              onerror=lambda n: problems.append("%s (not importable)" % n)):
                if info.name.count(".") > name.count(".") + 2 or ".test" in info.name or len(modules) > 60:
                    continue
                try:
                    modules.append(importlib.import_module(info.name))
                except Exception as e:
                    problems.append("%s (not importable: %s)" % (info.name, str(e).splitlines()[0][:100] if str(e)
                                                                else type(e).__name__))
        for mod in modules:
            for attr, obj in sorted(vars(mod).items()):
                if attr.startswith("_") or str(getattr(obj, "__module__", "") or "") != mod.__name__:
                    continue   # its own, not what it imports
                if isinstance(obj, type):
                    lines.append("class %s.%s%s%s" % (mod.__name__, attr, sig(obj), doc(obj)))
                    for member, value in sorted(vars(obj).items()):
                        if member.startswith("_"):
                            continue
                        if isinstance(value, property):
                            lines.append("    %s  (property)%s" % (member, doc(value)))
                            continue
                        fn = value.__func__ if isinstance(value, (staticmethod, classmethod)) else value
                        if callable(fn):
                            lines.append("    %s%s%s" % (member, sig(fn), doc(fn)))
                elif callable(obj):
                    lines.append("def %s.%s%s%s" % (mod.__name__, attr, sig(obj), doc(obj)))
            if len(lines) > limit:
                break
        out = lines[:limit] + (["... (%d more lines)" % (len(lines) - limit)] if len(lines) > limit else [])
        return "\n".join(out + (["Not imported: " + "; ".join(problems[:10])] if problems else []))

    def _describe_dotnet(self, req, path, limit):
        """A maker's .NET assemblies: their public types, with each method's parameters and each property."""
        assemblies = self._assemblies(req, path)   # loads pythonnet, and with it the .NET namespaces
        from System.Reflection import BindingFlags
        flags = BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly
        lines = []
        for asm in assemblies:
            for t in sorted(asm.GetExportedTypes(), key=lambda t: str(t.FullName)):
                if str(t.FullName).split(".")[0] in DENY_NET:
                    continue
                lines.append("%s %s" % ("enum" if t.IsEnum else "class", t.FullName) +
                             (": " + ", ".join(str(n) for n in t.GetEnumNames()) if t.IsEnum else ""))
                if t.IsEnum:
                    continue
                for m in t.GetMethods(flags):
                    if m.IsSpecialName:
                        continue
                    params = ", ".join("%s %s" % (p.ParameterType.Name, p.Name) for p in m.GetParameters())
                    lines.append("    %s%s(%s) -> %s" % ("static " if m.IsStatic else "", m.Name, params,
                                                         m.ReturnType.Name))
                for p in t.GetProperties(flags):
                    lines.append("    %s  (property, %s%s)" % (p.Name, p.PropertyType.Name,
                                                               ", settable" if p.CanWrite else ""))
                if len(lines) > limit:
                    break
        out = lines[:limit] + (["... (%d more lines)" % (len(lines) - limit)] if len(lines) > limit else [])
        return "\n".join(out)

    def _describe_com(self, req, limit):
        """A maker's COM object: its methods and properties, from its type information. Making the object starts its
        program, as the maker's examples do."""
        self._load_com(req)
        info = self.target._oleobj_.GetTypeInfo()
        attr = info.GetTypeAttr()
        kinds = {1: "method", 2: "property", 4: "property, settable", 8: "property, settable"}
        lines, seen = [], set()
        for i in range(attr.cFuncs):
            fd = info.GetFuncDesc(i)
            names = info.GetNames(fd.memid)
            if not names or names[0].startswith("_") or (names[0], fd.invkind) in seen:
                continue
            seen.add((names[0], fd.invkind))
            lines.append("    %s(%s)  (%s)" % (names[0], ", ".join(names[1:]), kinds.get(fd.invkind, "member")))
        return "\n".join(["COM object %s" % req["com"]] + lines[:limit])

    def _load_module(self, req, path):
        name = str(req.get("module") or "")
        target, spec = self._makers_module(name, path)
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
        obj, member = self._resolve(name)
        fn = getattr(obj, member, None)
        if fn is None or not callable(fn):
            raise Refused("the library has no function %s" % name)
        if self.kind == "dotnet":   # a method of the maker's .NET object (or a static one of its type)
            return _plain(self._net_invoke(obj, member, args))
        if self.kind == "com":   # a method of the maker's COM object
            return _plain(fn(*[_word(a) for a in args]))
        if not (isinstance(fn, ctypes._CFuncPtr) or self._mine(getattr(fn, "__module__", None))):
            raise Refused("%s is not one of the maker's functions" % name)   # e.g. one it imported from Python
        return _plain(fn(*[_word(a) for a in args]))

    def _mine(self, module):
        return str(module or "").split(".")[0] in self.packages

    def _makers(self, obj):
        """The maker's own: a module of its package, or an object of a class its package defines (its .NET types'
        namespace; its COM object's interfaces). Not Python's own modules or objects, nor the .NET platform's,
        through which a call could reach the operating system."""
        if self.kind == "com":
            return str(type(obj).__module__ or "").startswith("win32com")
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
            elif op == "read":
                value = lib.read(req.get("name"))
            elif op == "assign":
                value = lib.assign(req.get("name"), req.get("value"))
            elif op == "describe":
                value = lib.describe(req)
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
