"""YAML loading with YAML 1.2 number rules, so 1e7 and 1.0e7 are numbers.

(PyYAML follows YAML 1.1, where an exponent needs a sign and "1.0e7" is a string.)
"""
from __future__ import annotations

import re
from typing import Any

import yaml


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^[-+]?(?:[0-9][0-9_]*)?\.?[0-9_]*[eE][-+]?[0-9]+$"),
    list("-+0123456789."),
)


def load_yaml(stream: Any) -> Any:
    return yaml.load(stream, Loader=_Loader)
