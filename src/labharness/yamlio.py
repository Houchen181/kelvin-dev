"""YAML loading with YAML 1.2 number rules, so 1e7 and 1.0e7 are numbers.

(PyYAML follows YAML 1.1, where an exponent needs a sign and "1.0e7" is a string.)
"""
from __future__ import annotations

import re
from typing import Any

import yaml

_EXPONENT = re.compile(r"^[-+]?(?:[0-9][0-9_]*)?\.?[0-9_]*[eE][-+]?[0-9]+$")


class _Loader(yaml.SafeLoader):
    pass


class _Dumper(yaml.SafeDumper):
    pass


_Loader.add_implicit_resolver("tag:yaml.org,2002:float", _EXPONENT, list("-+0123456789."))
_Dumper.add_implicit_resolver("tag:yaml.org,2002:float", _EXPONENT, list("-+0123456789."))   # such a string is quoted


def load_yaml(stream: Any) -> Any:
    return yaml.load(stream, Loader=_Loader)


def dump_yaml(data: Any) -> str:
    """YAML that load_yaml reads back as it was (a string it would take for a number, such as "1e7", is quoted), in
    the order it was written; a mapping or list of plain values on one line."""
    text = yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, default_flow_style=None, width=1000)
    return text[:-4] if text.endswith("\n...\n") else text
