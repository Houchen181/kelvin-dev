"""SCPI-style command templates.

Cards write commands in manual notation: the leading capitals of a mnemonic
are its short form, the whole word is its long form, and [ ] marks an
optional node. "SOURce[:VOLTage]:LEVel {value}" therefore matches
"SOUR:LEV 5", ":source:voltage:level 5" and "SOUR:VOLT:LEV 5".
Instruments that do not speak SCPI can use literal templates like "V{value}".
"""
from __future__ import annotations

import re
from typing import Any, List, Optional

DRIVER_VERB = r"^\s*(SET|GET|PUT|POST|PATCH|DELETE)\s+([^\s{]+)"   # a driver's or a web API's command
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
        lead = re.match(r"^\s*\{value\}(.*)$", text or "", re.S)
        if lead:   # the value comes first: "{value},1" with bool_text [ON, OFF] sends ON,1 or OFF,1
            if "{value}" in lead.group(1):
                raise ValueError("only one {value} allowed in %r" % text)
            self.text, self.header, self.has_value, self.is_query, self.prefix = text.strip(), "", True, False, prefix
            self._lead = lead.group(1).strip()
            self._re = re.compile(r"^\s*(?P<value>[A-Za-z0-9_.+-]+)" + _rest(lead.group(1)) + r"\s*$", re.I | re.S)
            return
        self._lead = None
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
        # a header never runs into a letter: VOLT must not match the start of VOLTage:ILIMit (but SLVL5 is SLVL 5)
        pattern = r"^\s*:?" + _header(head) + r"(?![A-Za-z])"
        pattern += r"(?=$|[\s:?;,])" if prefix else _rest(rest) + r"\s*$"   # ',': COMMAND,ch,value instruments
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

    def header_prefix(self, words: Optional[List[str]] = None) -> Any:
        """This command's family: its header and everything below it. For a vendor-driver command (drivers.py:
        SET <path> {value}), the family is the verb and its path, not every SET. For a value-first command, words are
        the values it can send (ON, OFF), so FREQ,1,80 is not taken for the switch ON,1."""
        if self._lead is not None:
            return _LeadPrefix(self._lead, words)
        m = re.match(DRIVER_VERB, self.text, re.I)
        if m:
            return _PathPrefix(m.group(1), m.group(2))
        return Template(self.header, prefix=True)

    def __repr__(self) -> str:
        return "Template(%r)" % self.text


class _LeadPrefix:
    """The family of a value-first command ("{value},1"): one of its values (or a number) followed by the same rest
    (",1"), so an unlisted variant (ON,1,POW) still counts as touching that drive."""

    def __init__(self, rest: str, words: Optional[List[str]] = None):
        self.text = "{value}" + rest
        self._rest = rest.lower()
        self._words = None if words is None else {w.strip().lower() for w in words}

    def match(self, command: str) -> Optional[str]:
        m = re.match(r"^\s*([^\s,;]+)", command)
        if not m or not self._rest or not command[m.end():].strip().lower().startswith(self._rest):
            return None
        word = m.group(1).lower()
        if self._words is not None and word not in self._words and not re.match(r"^[+-]?[0-9.]", word):
            return None
        return ""


class _PathPrefix:
    """A driver command family: the verb and the path ('SET running' matches 'SET running 1', not 'SET wiring')."""

    def __init__(self, verb: str, path: str):
        self.text = "%s %s" % (verb, path)
        self._words = (verb.upper(), path.lower().split("#", 1)[0])

    def match(self, command: str) -> Optional[str]:
        words = command.split()
        if len(words) < 2 or words[0].upper() != self._words[0]:
            return None
        path = words[1].lower().split("#", 1)[0]
        # a path ending in / covers what lies below it (POST /v1/Basic/{value})
        same = path == self._words[1] or (self._words[1].endswith("/") and path.startswith(self._words[1]))
        return "" if same else None


def is_query(command: str) -> bool:
    first = command.strip().split(None, 1)
    return bool(first) and ("?" in first[0] or first[0].upper() == "GET")   # GET: a driver's or a web API's read


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
