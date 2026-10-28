"""Every instrument model, by the name a bench file gives it (`model: keithley_2400`)."""
from __future__ import annotations

import importlib
import pkgutil
from typing import Dict, Type

from ..instrument import Instrument

MODELS: Dict[str, Type[Instrument]] = {}

for _info in pkgutil.iter_modules(__path__):
    if _info.name == "common":
        continue
    _mod = importlib.import_module("%s.%s" % (__name__, _info.name))
    for _obj in vars(_mod).values():
        if isinstance(_obj, type) and issubclass(_obj, Instrument) and _obj.__module__ == _mod.__name__ \
                and getattr(_obj, "model", "generic") != "generic":
            MODELS[_obj.model] = _obj
