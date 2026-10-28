"""Andor's spectrograph DLL (atspectrograph.dll, ShamrockCIF*.dll) through ctypes, answered by the bench: loading
it by name (with or without a folder) gives an object whose Shamrock* functions take the same ctypes arguments and
fill the same pointers. Other DLLs load as usual."""
from __future__ import annotations

import ctypes
import os
from typing import Any, List

from .sdkclient import SdkCallError, call, devices

NAMES = ("atspectrograph.dll", "shamrockcif64.dll", "shamrockcif.dll", "shamrockcif32.dll")
OK, NOT_INITIALIZED, P1INVALID, COMMUNICATION_ERROR = 20202, 20275, 20266, 20201


def _is_out(a: Any) -> bool:
    return type(a).__name__ == "CArgObject" or isinstance(a, (ctypes._Pointer, ctypes.Array))


def _value(a: Any) -> Any:
    if isinstance(a, bytes):
        return a.decode(errors="replace")
    return getattr(a, "value", a)


def _store(a: Any, v: Any) -> None:
    if isinstance(a, ctypes.Array):
        a.value = str(v).encode()[: len(a) - 1]
    elif type(a).__name__ == "CArgObject":
        a._obj.value = v
    else:
        a.contents.value = v


class _Function:
    def __init__(self, lib: "ShamrockDLL", name: str):
        self.lib, self.__name__ = lib, name
        self.argtypes: Any = None
        self.restype: Any = ctypes.c_uint
        self.errcheck: Any = None

    def __call__(self, *args: Any) -> int:
        return self.lib.dispatch(self.__name__, list(args))


class ShamrockDLL:
    def __init__(self, path: str):
        self._path = path
        self._name = path
        self._handle = 0
        self._initialized = False
        self._fns: dict = {}

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("Shamrock"):
            raise AttributeError("function '%s' not found" % name)
        return self._fns.setdefault(name, _Function(self, name))

    __getitem__ = __getattr__

    def dispatch(self, fn: str, args: List[Any]) -> int:
        if fn == "ShamrockInitialize":
            self._initialized = True
            return OK
        if fn == "ShamrockClose":
            self._initialized = False
            return OK
        if not self._initialized:
            return NOT_INITIALIZED
        devs = devices("shamrock")
        if fn == "ShamrockGetNumberDevices":
            _store(args[0], len(devs))
            return OK
        if not args:
            return P1INVALID
        idx = int(_value(args[0]))
        if not 0 <= idx < len(devs):
            return P1INVALID
        ins = [_value(a) for a in args[1:] if not _is_out(a)]
        outs = [a for a in args[1:] if _is_out(a)]
        try:
            r = call(devs[idx]["key"], fn[len("Shamrock"):], *ins)
        except SdkCallError:
            return COMMUNICATION_ERROR
        if isinstance(r, (list, tuple)):
            code, values = int(r[0]), list(r[1:])
            for a, v in zip(outs, values):
                _store(a, v)
            return code
        return int(r)


def install() -> None:
    if not hasattr(ctypes, "WinDLL"):
        return
    real = ctypes.WinDLL

    class WinDLL(real):
        def __new__(cls, name: Any = None, *args: Any, **kwargs: Any) -> Any:
            if isinstance(name, str) and os.path.basename(name).lower() in NAMES and devices("shamrock"):
                return ShamrockDLL(name)
            return real.__new__(cls)

    ctypes.WinDLL = WinDLL
    ctypes.windll = ctypes.LibraryLoader(WinDLL)
