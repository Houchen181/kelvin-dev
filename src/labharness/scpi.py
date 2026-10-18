"""SCPI-style command templates.

Cards write commands in manual notation: the leading capitals of a mnemonic
are its short form, the whole word is its long form, and [ ] marks an
optional node. "SOURce[:VOLTage]:LEVel {value}" therefore matches
"SOUR:LEV 5", ":source:voltage:level 5" and "SOUR:VOLT:LEV 5".
Instruments that do not speak SCPI can use literal templates like "V{value}".
"""
from __future__ import annotations

import re
from typing import List, Optional

_SPLIT = re.compile(r"^\s*([*\[\]:A-Za-z0-9]+)(.*)$", re.S)
_NODE = re.compile(r"(\[)?(:)?(\*?[A-Za-z]+)(\d*)(\])?")
_OPTIONAL = re.compile(r"\[[^\]]*\]")


def _word(word: str) -> str:
    long = word.upper()
    short = re.match(r"\*?[A-Z]*", word).group(0)
    if not short or short == long:
        return re.escape(long)
    return "(?:%s|%s)" % (re.escape(long), re.escape(short))


def _header(header: str) -> str:
    parts, pos, leading = [], 0, True
    for m in _NODE.finditer(header):
        if m.start() != pos:
            break
        opt, colon, word, digits, close = m.groups()
        if bool(opt) != bool(close):
            raise ValueError("unbalanced [ ] in %r" % header)
        sep = (":?" if leading else ":") if colon else ""
        node = sep + _word(word) + re.escape(digits)
        parts.append("(?:%s)?" % node if opt else node)
        leading = leading and bool(opt)
        pos = m.end()
    if pos != len(header) or not parts:
        raise ValueError("cannot parse command header %r" % header)
    return "".join(parts)


def _rest(rest: str) -> str:
    out = []
    for i, chunk in enumerate(rest.split("{value}")):
        if i:
            out.append(r"(?P<value>[^\s:].*?)")
        for tok in re.split(r"(\s+)", chunk):
            if tok:
                out.append(r"\s*" if tok.isspace() else re.escape(tok))
    return "".join(out)


class Template:
    """One command pattern from a card. `prefix=True` matches a header and
    everything below it (used for blocked and reset command families)."""

    def __init__(self, text: str, prefix: bool = False):
        m = _SPLIT.match(text or "")
        if not m:
            raise ValueError("empty command template")
        head, rest = m.group(1), m.group(2)
        if rest.count("{value}") > 1:
            raise ValueError("only one {value} allowed in %r" % text)
        self.text = text.strip()
        self.header = head
        self.has_value = "{value}" in rest
        self.is_query = rest.lstrip().startswith("?")
        self.prefix = prefix
        pattern = r"^\s*:?" + _header(head)
        pattern += r"(?=$|[\s:?;])" if prefix else _rest(rest) + r"\s*$"
        self._re = re.compile(pattern, re.IGNORECASE | re.S)

    def match(self, command: str) -> Optional[str]:
        """None if no match; otherwise the value text ('' if there is none)."""
        m = self._re.match(command)
        if m is None:
            return None
        return m.group("value").strip() if self.has_value and not self.prefix else ""

    def render(self, value: Optional[str] = None) -> str:
        text = _OPTIONAL.sub("", self.text)
        if self.has_value:
            if value is None:
                raise ValueError("%r needs a value" % self.text)
            text = text.replace("{value}", value)
        return text

    def header_prefix(self) -> "Template":
        return Template(self.header, prefix=True)

    def __repr__(self) -> str:
        return "Template(%r)" % self.text


def is_query(command: str) -> bool:
    first = command.strip().split(None, 1)
    return bool(first) and "?" in first[0]


def split_compound(command: str) -> List[str]:
    """Split "SOUR:VOLT 1;CURR 2" into absolute commands, following the SCPI
    rule that a relative command continues the previous command's path."""
    out, path = [], ""
    for part in command.split(";"):
        part = part.strip()
        if not part:
            continue
        if part.startswith("*"):
            out.append(part)
            continue
        full = part.lstrip(":") if part.startswith(":") or not path else path + ":" + part
        head = re.split(r"[\s?]", full, maxsplit=1)[0]
        path = head.rsplit(":", 1)[0] if ":" in head else ""
        out.append(full)
    return out
