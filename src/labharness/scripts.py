"""A lab's own measurement scripts, read to set Kelvin up the way the lab already works.

Many labs run their instruments from Python or MATLAB scripts. Those scripts say, better than any manual, how this
lab talks to its instruments: where each one is (its address), how the connection is set (baud rate, line endings,
timeouts), which commands it takes and with which values, and how a measurement runs (sweeps, waits, what is read
and saved). survey() reads every .py, .ipynb and .m file in a folder and lists that, per instrument and per script.
It only reads: no script is ever run, imported or evaluated.

    instruments   address, the names the scripts give it, the library (pyvisa, pyserial, a QCoDeS or PyMeasure
                  driver, MATLAB visadev/gpib/tcpip/serialport), connection settings, the commands sent (a value the
                  script fills in shows as {its expression}, with the numbers it takes where they can be read), and
                  hints of what it is (a model named in a comment, a driver's class, *IDN?)
    scripts       an outline of each script's instrument work: connections, commands, loops, waits, files saved
    cards         Kelvin's card that fits each instrument best (a model named, the commands it takes), each
                  command's parameter on it, and limits from the values the scripts use (for a person to review)
"""
from __future__ import annotations

import ast
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SUFFIXES = {".py", ".ipynb", ".m"}
SKIP_DIRS = {"site-packages", "dist-packages", "python_libs", "lib", "libs", "node_modules", "__pycache__", ".git",
             ".venv", "venv", "env", ".env", ".ipynb_checkpoints", "build", "dist", ".idea", ".vscode"}
MAX_FILES = 300
MAX_BYTES = 1_000_000
MAX_OUTLINE = 80   # lines of a script's outline in the report
COMMAND_CALLS = {"write", "query", "ask", "write_raw", "query_ascii_values", "query_binary_values", "send", "sendall",
                 "write_ascii_values", "write_binary_values", "ask_raw"}
READ_CALLS = {"read", "read_raw", "readline", "recv", "read_bytes"}
SAVE_CALLS = {"savetxt", "save", "savez", "savez_compressed", "savemat", "to_csv", "to_excel", "to_hdf", "to_pickle",
              "dump", "writerow", "writerows", "File", "create_dataset", "write_text", "write_bytes"}
MATLAB_OPEN = {"visadev", "visa", "gpib", "tcpip", "tcpclient", "serialport", "serial", "icdevice", "udpport"}
MATLAB_SEND = {"writeline", "writeread", "write", "fprintf", "query", "writebinblock"}
MATLAB_READ = {"readline", "readbinblock", "fscanf", "fgetl", "fgets", "fread", "read"}
MATLAB_SAVE = {"save", "writematrix", "csvwrite", "dlmwrite", "writetable", "h5write", "saveas", "exportgraphics",
               "writecell", "xlswrite"}
_ADDRESS = re.compile(r"""(?ix)^\s*(
      (?:GPIB|VXI|GPIB-VXI)\d*::\d+(?:::\d+)?(?:::INSTR)?
    | TCPIP\d*::[^:\s]+::(?:inst\d+|hislip\d+|\d+)(?:::(?:INSTR|SOCKET))?
    | TCPIP\d*::[^:\s]+(?:::INSTR)?
    | USB\d*::(?:0x)?[0-9a-f]+::(?:0x)?[0-9a-f]+::[^:\s]+(?:::\d+)?(?:::(?:INSTR|RAW))?
    | ASRL\d+(?:::INSTR)?
    | PXI\d*::\S+
    | COM\d+
    | /dev/tty[\w.-]+
    )\s*$""")
_MODEL = re.compile(r"\b(?:[A-Z]{1,4}[- ]?)?\d{3,5}[A-Z]{0,3}\b")
_VENDORS = ("keysight", "agilent", "hewlett", "hp ", "keithley", "tektronix", "rohde", "r&s", "stanford", "srs",
            "lake ?shore", "lakeshore", "zurich", "zhinst", "national instruments", "ni-daq", "rigol", "siglent",
            "anritsu", "copper mountain", "yokogawa", "oxford", "bluefors", "attocube", "thorlabs", "newport",
            "coherent", "toptica", "princeton", "andor", "hamamatsu", "ametek", "signal recovery", "picoscope",
            "swabian", "quantum design", "ami ", "cryomagnetics", "mercury", "windfreak", "anapico")
_FORMAT = re.compile(r"%[-+ 0#]*\d*(?:\.\d+)?[diouxXeEfFgGcs]")
_PLACE = re.compile(r"\{([^{}]*)\}")


# ------------------------------------------------------------------ what the survey finds

@dataclass
class Command:
    text: str                  # as sent; a value the script fills in is {its expression}
    query: bool
    where: str                 # script:line
    values: Dict[str, List[float]] = field(default_factory=dict)   # each fill-in's numbers, where they can be read
    loop: str = ""             # the loop variable it changes with, if any

    def sample(self) -> str:
        """The command with each fill-in as a number it takes (or 1), to match against cards."""
        def one(m: "re.Match[str]") -> str:
            nums = self.values.get(m.group(1))
            return _num(nums[0]) if nums else "1"
        return _PLACE.sub(one, self.text)


@dataclass
class Instrument:
    address: str               # Kelvin's form (GPIB0::16::INSTR, TCPIP0::host::5025::SOCKET, serial://COM3), or ""
    names: List[str]           # what the scripts call it (vna, smu, k2400)
    library: str
    where: List[str]
    expression: str = ""       # the address as the script builds it, when it can't be read as text
    settings: Dict[str, Any] = field(default_factory=dict)   # connection: baud_rate, read/write_termination, timeout
    commands: List[Command] = field(default_factory=list)
    calls: List[str] = field(default_factory=list)            # a driver's own API (QCoDeS, PyMeasure): k.volt(0.5)
    hints: List[str] = field(default_factory=list)            # models and makers named near it
    card: Optional[Dict[str, Any]] = None                    # see match(): the card that fits best, and why

    @property
    def name(self) -> str:
        for n in self.names:
            short = re.sub(r"^(self|obj|this)\.", "", n).split(".")[-1].lower()
            short = re.sub(r"[^a-z0-9_]", "", short)
            if short and short[0].isalpha() and short not in ("rm", "inst", "instr", "res", "dev", "device", "obj"):
                return short
        return ""


@dataclass
class Script:
    path: str
    language: str              # python | matlab
    outline: List[str] = field(default_factory=list)
    problem: str = ""


@dataclass
class Survey:
    folder: str
    instruments: List[Instrument] = field(default_factory=list)
    scripts: List[Script] = field(default_factory=list)
    others: int = 0            # files read that hold no instrument work
    skipped: List[str] = field(default_factory=list)

    def text(self) -> str:
        """The report, for Kelvin's model: per instrument, then each script's outline."""
        lines = ["Surveyed %d script%s in %s (only read, never run): %d instrument%s." % (
            len(self.scripts), "" if len(self.scripts) == 1 else "s", self.folder, len(self.instruments),
            "" if len(self.instruments) == 1 else "s")]
        if self.others:
            lines.append("%d other file%s hold no instrument work." % (self.others, "" if self.others == 1 else "s"))
        for inst in self.instruments:
            lines += [""] + _instrument_lines(inst)
        for s in self.scripts:
            if not s.outline and not s.problem:
                continue
            lines += ["", "%s (%s):" % (s.path, s.language)]
            if s.problem:
                lines.append("  could not be read in full: %s" % s.problem)
            lines += ["  " + o for o in s.outline[:MAX_OUTLINE]]
            if len(s.outline) > MAX_OUTLINE:
                lines.append("  … %d more lines (read_file for the whole script)" % (len(s.outline) - MAX_OUTLINE))
        if self.skipped:
            lines += ["", "Not read: " + "; ".join(self.skipped[:10])]
        return "\n".join(lines)

    def view(self) -> Dict[str, Any]:
        """For the portal's Set up: each instrument with its likely card and proposed limits."""
        return {"folder": self.folder, "scripts": [s.path for s in self.scripts], "others": self.others,
                "skipped": self.skipped,
                "instruments": [{"address": i.address, "expression": i.expression, "names": i.names,
                                 "name": i.name, "library": i.library, "where": i.where[:6],
                                 "connection": connection(i.settings), "hints": i.hints[:6],
                                 "commands": len(i.commands), "calls": i.calls[:10],
                                 "card": i.card} for i in self.instruments]}


def _num(x: float) -> str:
    return ("%.12g" % x) if isinstance(x, float) else str(x)


def connection(settings: Dict[str, Any]) -> Dict[str, Any]:
    """The settings a lab config's connection: takes (timeouts in ms)."""
    out = {}
    for key in ("baud_rate", "read_termination", "write_termination", "timeout_ms", "data_bits", "stop_bits",
                "parity"):
        if key in settings:
            out[key] = settings[key]
    return out


def _instrument_lines(inst: Instrument) -> List[str]:
    head = "%s  %s  (%s; %s)" % (inst.name or "(no name)", inst.address or "address set at run time: %s"
                                 % (inst.expression or "?"), inst.library, ", ".join(inst.where[:4]))
    lines = [head]
    if inst.hints:
        lines.append("  named nearby: " + ", ".join(inst.hints[:6]))
    if inst.settings:
        lines.append("  connection: " + ", ".join("%s %s" % (k, json.dumps(v)) for k, v in inst.settings.items()))
    card = inst.card or {}
    if card.get("id"):
        lines.append("  likely Kelvin card: %s (%s): %s" % (card["id"], card.get("model", ""), card.get("why", "")))
    elif inst.commands:
        lines.append("  no Kelvin card fits: draft one (draft_card; these commands are the ones this lab uses)")
    mapped = {m["command"]: m for m in card.get("mapped", [])}
    seen = set()
    shown = [c for c in inst.commands if not (c.text in seen or seen.add(c.text))]
    if shown:
        lines.append("  commands (%d):" % len(shown))
        for c in shown[:40]:
            m = mapped.get(c.text)
            to = (" → part of reading %s" % m["param"] if m and m.get("part") else
                  " → %s %s" % ("reads" if m["read"] else "sets", m["param"]) if m and m.get("param") else
                  " → not on the card" if card.get("id") else "")
            vals = "; ".join("%s: %s" % (k, _span(v)) for k, v in c.values.items() if v)
            lines.append("    %s%s%s%s" % ("? " if c.query else "", c.text, to, "  (%s)" % vals if vals else ""))
        if len(shown) > 40:
            lines.append("    … %d more" % (len(shown) - 40))
    if inst.calls:
        lines.append("  driver calls: " + ", ".join(inst.calls[:12]))
    if card.get("limits"):
        lines.append("  limits from the values the scripts use (a person reviews them): " + ", ".join(
            "%s [%s, %s]" % (p, _num(lo), _num(hi)) for p, (lo, hi) in card["limits"].items()))
    return lines


def _span(values: Sequence[float]) -> str:
    vs = sorted(set(values))
    if len(vs) <= 3:
        return ", ".join(_num(v) for v in vs)
    return "%s … %s (%d values)" % (_num(vs[0]), _num(vs[-1]), len(vs))


# ------------------------------------------------------------------ addresses

def normalize(address: str) -> str:
    """An address as Kelvin's lab config takes it: GPIB::24 → GPIB0::24::INSTR, COM3 → serial://COM3."""
    a = address.strip()
    up = a.upper()
    m = re.match(r"(?i)^GPIB(\d*)::(\d+)(?:::(\d+))?(?:::INSTR)?$", a)
    if m:
        return "GPIB%s::%s%s::INSTR" % (m.group(1) or "0", m.group(2), "::" + m.group(3) if m.group(3) else "")
    if re.match(r"(?i)^COM\d+$", a):
        return "serial://" + up
    if a.startswith("/dev/tty"):
        return "serial://" + a
    m = re.match(r"(?i)^TCPIP(\d*)::([^:]+)(?:::INSTR)?$", a)
    if m:
        return "TCPIP%s::%s::inst0::INSTR" % (m.group(1) or "0", m.group(2))
    m = re.match(r"(?i)^TCPIP(\d*)::([^:]+)::(\d+)(?:::SOCKET)?$", a)
    if m:
        return "TCPIP%s::%s::%s::SOCKET" % (m.group(1) or "0", m.group(2), m.group(3))
    m = re.match(r"(?i)^ASRL(\d+)$", a)
    if m:
        return "ASRL%s::INSTR" % m.group(1)
    return a


def is_address(text: str) -> bool:
    return bool(_ADDRESS.match(text or ""))


def _hints(text: str) -> List[str]:
    """Models and makers named in a comment, a name or a string: N5227B, SR830, Keithley 2400."""
    out = []
    low = text.lower()
    for v in _VENDORS:
        m = re.search(r"\b" + v.strip(), low)
        if m:
            out.append(text[m.start():m.start() + len(v.strip())].strip())
    for m in _MODEL.finditer(text):
        token = m.group(0)
        if re.fullmatch(r"\d{3,5}", token) and not out:   # a bare number counts only beside a maker's name
            continue
        if re.fullmatch(r"(19|20)\d\d|\d{5,}", token):   # a year, or a baud rate
            continue
        out.append(token)
    seen: set = set()
    return [t for t in out if not (t.lower() in seen or seen.add(t.lower()))]


# ------------------------------------------------------------------ Python (and notebooks)

class _Values:
    """What a script assigns that can be read without running it: strings and numbers, by name (x, cfg.power,
    CONFIG['power']), and each loop variable's values."""

    def __init__(self) -> None:
        self.known: Dict[str, Any] = {}
        self.loops: Dict[str, List[float]] = {}

    def text(self, node: Optional[ast.AST]) -> Tuple[str, bool]:
        """The node as text: (its value, True) when it can be read, else ({expression}, False) pieces."""
        if node is None:
            return "", False
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bytes):
                return v.decode("latin-1"), True
            return ("%s" % v if not isinstance(v, float) else _num(v)), True
        key = _key(node)
        if key in self.known:
            v = self.known[key]
            return (_num(v) if isinstance(v, float) else str(v)), True
        if isinstance(node, ast.JoinedStr):   # an f-string: its fill-ins as {expression}
            parts, ok = [], True
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                elif isinstance(v, ast.FormattedValue):
                    t, k = self.text(v.value)
                    if k and not isinstance(v.value, ast.Constant) and _key(v.value) not in self.known:
                        k = False
                    parts.append(t if k else "{%s}" % _unparse(v.value))
                    ok = ok and k
            return "".join(parts), ok
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            a, ka = self.text(node.left)
            b, kb = self.text(node.right)
            return (a if ka else "{%s}" % _unparse(node.left)) + (b if kb else "{%s}" % _unparse(node.right)), ka and kb
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and isinstance(node.left, ast.Constant) \
                and isinstance(node.left.value, str):   # "SOUR:VOLT %g" % v
            args = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
            return self._fill(node.left.value, args, _FORMAT)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format" \
                and isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str):
            return self._format(node.func.value.value, node.args, node.keywords)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("str", "repr") \
                and node.args:
            return self.text(node.args[0])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "encode":
            return self.text(node.func.value)
        return "{%s}" % _unparse(node), False

    def _fill(self, template: str, args: Sequence[ast.AST], pattern: "re.Pattern[str]") -> Tuple[str, bool]:
        it = iter(args)
        ok = [True]

        def one(m: "re.Match[str]") -> str:
            arg = next(it, None)
            if arg is None:
                return m.group(0)
            t, k = self.text(arg)
            if k and _key(arg) in self.known or isinstance(arg, ast.Constant):
                return t
            ok[0] = False
            return "{%s}" % _unparse(arg)
        return pattern.sub(one, template), ok[0]

    def _format(self, template: str, args: Sequence[ast.AST], keywords: Sequence[ast.keyword]) -> Tuple[str, bool]:
        named = {k.arg: k.value for k in keywords if k.arg}
        it = iter(args)
        ok = [True]

        def one(m: "re.Match[str]") -> str:
            field_ = m.group(1).split(":")[0].split("!")[0]
            arg = named.get(field_) if field_ and not field_.isdigit() else (
                args[int(field_)] if field_.isdigit() and int(field_) < len(args) else next(it, None))
            if arg is None:
                return m.group(0)
            t, k = self.text(arg)
            if k and (_key(arg) in self.known or isinstance(arg, ast.Constant)):
                return t
            ok[0] = False
            return "{%s}" % _unparse(arg)
        return re.sub(r"\{([^{}]*)\}", one, template), ok[0]

    def number(self, node: Optional[ast.AST]) -> Optional[float]:
        if node is None:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            v = self.number(node.operand)
            return -v if v is not None else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Div, ast.Add, ast.Sub, ast.Pow)):
            a, b = self.number(node.left), self.number(node.right)
            if a is None or b is None:
                return None
            try:
                return {ast.Mult: a * b, ast.Div: a / b if b else None, ast.Add: a + b, ast.Sub: a - b,
                        ast.Pow: a ** b if abs(b) < 64 else None}[type(node.op)]
            except (OverflowError, ZeroDivisionError):
                return None
        v = self.known.get(_key(node))
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def series(self, node: ast.AST) -> Tuple[str, List[float]]:
        """A loop's iterable, described, with its values where they can be read (ends and count)."""
        desc = _unparse(node)
        if isinstance(node, ast.Call):
            fn = _unparse(node.func).split(".")[-1]
            args = [self.number(a) for a in node.args]
            if fn in ("linspace", "geomspace", "logspace") and len(args) >= 2 and None not in args[:2]:
                n = int(args[2]) if len(args) > 2 and args[2] is not None else 50
                a, b = args[0], args[1]
                if fn == "logspace":
                    a, b = 10 ** a, 10 ** b
                return desc, _spread(a, b, n, log=fn != "linspace")
            if fn in ("range", "arange") and node.args and None not in args:
                if len(args) == 1:
                    a, b, s = 0.0, args[0], 1.0
                else:
                    a, b, s = args[0], args[1], (args[2] if len(args) > 2 else 1.0)
                if s and (b - a) / s <= 1e5:
                    vals, x = [], a
                    while (x < b - 1e-12 * abs(s)) if s > 0 else (x > b + 1e-12 * abs(s)):
                        vals.append(x)
                        x += s
                    return desc, vals
        if isinstance(node, (ast.List, ast.Tuple)):
            vals = [self.number(e) for e in node.elts]
            if vals and None not in vals:
                return desc, [float(v) for v in vals]
        v = self.known.get(_key(node))
        if isinstance(v, list) and v and all(isinstance(x, (int, float)) for x in v):
            return desc, [float(x) for x in v]
        return desc, []

    def numbers_in(self, text: str) -> Dict[str, List[float]]:
        """Each {fill-in}'s numbers: a known value, or a loop variable's values (also under arithmetic on it)."""
        out: Dict[str, List[float]] = {}
        for expr in _PLACE.findall(text):
            v = self.known.get(expr)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[expr] = [float(v)]
                continue
            names = re.findall(r"[A-Za-z_][\w.]*", expr)
            loop = next((n for n in names if n in self.loops and self.loops[n]), None)
            if loop:
                vals = self.loops[loop]
                if expr.strip() == loop:
                    out[expr] = vals
                else:   # arithmetic on the loop variable: evaluate it at its ends
                    try:
                        node = ast.parse(expr, mode="eval").body
                        got = []
                        for x in (vals[0], vals[-1]):
                            self.known[loop] = x
                            y = self.number(node)
                            if y is not None:
                                got.append(y)
                        self.known.pop(loop, None)
                        if got:
                            out[expr] = got
                    except SyntaxError:
                        pass
        return out


def _spread(a: float, b: float, n: int, log: bool = False) -> List[float]:
    n = max(1, min(int(n), 100000))
    if n == 1:
        return [a]
    if log and a > 0 and b > 0:
        la, lb = math.log10(a), math.log10(b)
        return [10 ** (la + (lb - la) * i / (n - 1)) for i in range(n)]
    return [a + (b - a) * i / (n - 1) for i in range(n)]


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:   # an odd node: its class name
        return type(node).__name__


def _key(node: ast.AST) -> str:
    """A name the values table is keyed by: x, cfg.power, CONFIG['power']."""
    if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
        return _unparse(node)
    return ""


class _Python(ast.NodeVisitor):
    """Walks one script in order: connections, their settings and commands, loops, waits and saves."""

    def __init__(self, path: str, source: str, survey: "_Collector"):
        self.path, self.lines, self.survey = path, source.splitlines(), survey
        self.values = _Values()
        self.conns: Dict[str, Instrument] = {}
        self.outline: List[str] = []
        self.depth = 0
        self.functions: set = set()
        self.addr_hints: Dict[str, List[str]] = {}   # an address kept in a name: the models named where it is set

    def where(self, node: ast.AST) -> str:
        return "%s:%d" % (self.path, getattr(node, "lineno", 0))

    def note(self, text: str) -> None:
        self.outline.append("  " * self.depth + text)

    def nearby(self, node: ast.AST) -> str:
        """The line and the comment lines just above it: where a model is usually named."""
        i = getattr(node, "lineno", 1) - 1
        out = [self.lines[i] if i < len(self.lines) else ""]
        j = i - 1
        while j >= 0 and j >= i - 3 and self.lines[j].strip().startswith("#"):
            out.append(self.lines[j])
            j -= 1
        return " ".join(out)

    # assignments: values, connections, connection settings
    def visit_Assign(self, node: ast.Assign) -> None:
        self._assign(node.targets, node.value)
        self._value(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._assign([node.target], node.value)
            self._value(node.value)

    def _value(self, value: ast.AST) -> None:
        """What an assignment's value does: a reading (x = lockin.X(), i = smu.query(...)), not a connection."""
        if isinstance(value, ast.Call) and self._opening(value):
            for a in list(value.args) + [k.value for k in value.keywords]:
                self.visit(a)
        else:
            self.visit(value)

    def _opening(self, call: ast.Call) -> bool:
        last = _unparse(call.func).split(".")[-1]
        return last in ("open_resource", "instrument", "Instrument", "get_instrument", "Serial",
                        "serial_for_url", "create_connection") or any(
            self.values.text(a)[1] and is_address(self.values.text(a)[0]) for a in call.args)

    def _assign(self, targets: Sequence[ast.AST], value: ast.AST) -> None:
        for t in targets:
            key = _key(t)
            if not key:
                continue
            owner, _, attr = key.rpartition(".")
            if owner in self.conns and attr in _SETTINGS:   # inst.read_termination = "\n"
                setting = _setting(attr, value, self.values)
                if setting:
                    self.conns[owner].settings.update(setting)
                continue
            num = self.values.number(value)
            if num is not None:
                self.values.known[key] = num
            elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                self.values.known[key] = value.value
                if is_address(value.value):   # KEITHLEY = "GPIB0::24::INSTR"   # Keithley 2400 on the sample
                    self.addr_hints[key] = _hints(self.nearby(value))
            elif isinstance(value, (ast.List, ast.Tuple)):
                nums = [self.values.number(e) for e in value.elts]
                if nums and None not in nums:
                    self.values.known[key] = [float(n) for n in nums]
            elif isinstance(value, ast.Dict):
                for k, v in zip(value.keys, value.values):
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        n = self.values.number(v)
                        sub = "%s[%r]" % (key, k.value)
                        if n is not None:
                            self.values.known[sub] = n
                        elif isinstance(v, ast.Constant) and isinstance(v.value, str):
                            self.values.known[sub] = v.value
            elif isinstance(value, ast.JoinedStr):
                text, ok = self.values.text(value)
                if ok:
                    self.values.known[key] = text
            inst = self._opens(value)
            if inst is not None:
                inst.names.append(key)
                self.conns[key] = inst
                self.note("connect %s → %s (%s)" % (key, inst.address or "{%s}" % inst.expression, inst.library))

    def _opens(self, value: ast.AST) -> Optional[Instrument]:
        """An instrument opened here: pyvisa, pyserial, a socket, or a driver class given an address."""
        if not isinstance(value, ast.Call):
            return None
        fn = _unparse(value.func)
        last = fn.split(".")[-1]
        args = list(value.args) + [k.value for k in value.keywords]
        kw = {k.arg: k.value for k in value.keywords if k.arg}
        if last in ("open_resource", "instrument", "Instrument", "get_instrument") or fn in ("visa.instrument",):
            target = value.args[0] if value.args else kw.get("resource_name")
            inst = self._new(target, "pyvisa", value)
            for k, v in kw.items():
                inst.settings.update(_setting(k, v, self.values) or {})
            return inst
        if last == "Serial" or fn in ("serial.Serial", "serial.serial_for_url"):
            target = value.args[0] if value.args else kw.get("port")
            inst = self._new(target, "pyserial", value)
            baud = value.args[1] if len(value.args) > 1 else kw.get("baudrate")
            n = self.values.number(baud)
            if n:
                inst.settings["baud_rate"] = int(n)
            return inst
        if fn in ("socket.create_connection",) and value.args and isinstance(value.args[0], ast.Tuple):
            host, ok = self.values.text(value.args[0].elts[0])
            port = self.values.number(value.args[0].elts[1]) if len(value.args[0].elts) > 1 else None
            addr = "TCPIP0::%s::%d::SOCKET" % (host, int(port)) if ok and port else ""
            return self._made(addr, "" if addr else _unparse(value.args[0]), "socket", value)
        for a in args:   # a driver class (QCoDeS, PyMeasure, instrumentkit) given an address
            text, ok = self.values.text(a)
            if ok and is_address(text) and last[:1].isupper():
                inst = self._new(a, "driver " + last, value)
                inst.hints.append(last)
                return inst
        return None

    def _new(self, target: Optional[ast.AST], library: str, node: ast.AST) -> Instrument:
        text, ok = self.values.text(target)
        addr = normalize(text) if ok and is_address(text) else ""
        inst = self._made(addr, "" if addr else (_unparse(target) if target is not None else "?"), library, node)
        if target is not None and _key(target) in self.addr_hints:
            inst.hints += self.addr_hints[_key(target)]
        return inst

    def _made(self, address: str, expression: str, library: str, node: ast.AST) -> Instrument:
        inst = Instrument(address=address, names=[], library=library, where=[self.where(node)], expression=expression)
        inst.hints += _hints(self.nearby(node)) + _hints(Path(self.path).stem)
        self.survey.add(inst)
        return inst

    # calls: commands, connection settings, waits, saves, a socket's connect
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            owner, method = _unparse(func.value), func.attr
            inst = self.conns.get(owner) or self.survey.by_name(owner)
            if method == "connect" and inst is None and node.args and isinstance(node.args[0], ast.Tuple):
                host, ok = self.values.text(node.args[0].elts[0])   # sock.connect((host, 5025))
                port = self.values.number(node.args[0].elts[1]) if len(node.args[0].elts) > 1 else None
                if ok and port:
                    made = self._made("TCPIP0::%s::%d::SOCKET" % (host, int(port)), "", "socket", node)
                    made.names.append(owner)
                    self.conns[owner] = made
                    self.note("connect %s → %s (socket)" % (owner, made.address))
            elif method in COMMAND_CALLS and node.args and (inst is not None or _looks_scpi(node.args[0], self.values)):
                if inst is None:   # sent through a name the script got elsewhere (a function's argument)
                    inst = self.survey.loose(owner, self.where(node))
                text, _ = self.values.text(node.args[0])
                text = text.strip()
                if text:
                    query = method in ("query", "ask", "query_ascii_values", "query_binary_values", "ask_raw") or \
                        text.rstrip().endswith("?")
                    cmd = Command(text, query, self.where(node), self.values.numbers_in(text),
                                  next((n for n in self.values.loops if "{" + n in text or n + "}" in text), ""))
                    inst.commands.append(cmd)
                    self.note("%s %s %s" % (owner, "?" if query else "←", text))
                    if "*IDN?" in text.upper():
                        inst.hints.append("asks *IDN?")
            elif method in READ_CALLS and inst is not None:
                self.note("%s → read" % owner)
            elif inst is not None and inst.library.startswith("driver") and method not in ("close", "__init__"):
                call = "%s(%s)" % (method, ", ".join(_unparse(a) for a in node.args))
                inst.calls.append(call)
                self.note("%s.%s" % (owner, call))
            elif method in SAVE_CALLS:
                self.note("save %s" % self._file(node))
            elif method == "sleep":
                self._wait(node)
        elif isinstance(func, ast.Name):
            if func.id == "sleep":
                self._wait(node)
            elif func.id in ("savetxt", "savemat", "save", "dump"):
                self.note("save %s" % self._file(node))
            elif func.id == "open" and len(node.args) > 1 and isinstance(node.args[1], ast.Constant) \
                    and any(m in str(node.args[1].value) for m in "wa"):
                self.note("save %s" % self._file(node))
            elif func.id in self.functions:
                self.note("%s()" % func.id)
        self.generic_visit(node)

    def _wait(self, node: ast.Call) -> None:
        n = self.values.number(node.args[0]) if node.args else None
        self.note("wait %s s" % (_num(n) if n is not None else _unparse(node.args[0]) if node.args else "?"))

    def _file(self, node: ast.Call) -> str:
        for a in node.args:
            text, ok = self.values.text(a)
            if ok and re.search(r"\.\w{1,5}$", text):
                return text
        return _unparse(node.args[0]) if node.args else "?"

    # structure: loops and functions, in order
    def visit_For(self, node: ast.For) -> None:
        var = _unparse(node.target)
        desc, vals = self.values.series(node.iter)
        if vals and isinstance(node.target, ast.Name):
            self.values.loops[var] = vals
        span = " (%s)" % _span(vals) if vals else ""
        self.note("for %s in %s%s:" % (var, desc, span))
        self.depth += 1
        for stmt in node.body:
            self.visit(stmt)
        self.depth -= 1
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_While(self, node: ast.While) -> None:
        self.note("while %s:" % _unparse(node.test))
        self.depth += 1
        for stmt in node.body:
            self.visit(stmt)
        self.depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions.add(node.name)
        self.note("def %s(%s):" % (node.name, ", ".join(a.arg for a in node.args.args)))
        self.depth += 1
        for stmt in node.body:
            self.visit(stmt)
        self.depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef   # type: ignore[assignment]

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:   # with rm.open_resource(addr) as inst:
            inst = self._opens(item.context_expr)
            if inst is not None and item.optional_vars is not None:
                key = _key(item.optional_vars)
                inst.names.append(key)
                self.conns[key] = inst
                self.note("connect %s → %s (%s)" % (key, inst.address or "{%s}" % inst.expression, inst.library))
            else:
                self.visit(item.context_expr)
        for stmt in node.body:
            self.visit(stmt)


_SETTINGS = {"read_termination", "write_termination", "timeout", "baud_rate", "baudrate", "data_bits", "bytesize",
             "stop_bits", "stopbits", "parity", "query_delay", "Timeout", "Terminator", "BaudRate"}


def _setting(name: str, value: Any, values: Optional[_Values] = None) -> Optional[Dict[str, Any]]:
    """A connection setting as a lab config's connection: takes it (timeouts in ms)."""
    if values is not None and isinstance(value, ast.AST):
        num = values.number(value)
        text, ok = values.text(value)
        raw: Any = num if num is not None else (text if ok else None)
    else:
        raw = value
    if raw is None:
        return None
    key = name.lower()
    if key in ("read_termination", "write_termination") and isinstance(raw, str):
        return {key: raw}
    if key == "timeout" and isinstance(raw, (int, float)):
        return {"timeout_ms": int(raw if raw >= 50 else raw * 1000)}   # pyvisa: ms; pyserial and MATLAB: s
    if key in ("baud_rate", "baudrate") and isinstance(raw, (int, float)):
        return {"baud_rate": int(raw)}
    if key in ("data_bits", "bytesize") and isinstance(raw, (int, float)):
        return {"data_bits": int(raw)}
    if key in ("stop_bits", "stopbits") and isinstance(raw, (int, float)):
        return {"stop_bits": float(raw)}
    if key == "query_delay" and isinstance(raw, (int, float)):
        return {"query_delay": float(raw)}
    if key == "terminator" and isinstance(raw, str):
        term = {"LF": "\n", "CR": "\r", "CR/LF": "\r\n", "CRLF": "\r\n"}.get(raw.upper(), raw)
        return {"read_termination": term, "write_termination": term}
    return None


def _looks_scpi(node: ast.AST, values: _Values) -> bool:
    text, _ = values.text(node)
    return bool(re.match(r"^\s*[*:]?[A-Z][A-Za-z0-9]*(?::[A-Za-z0-9]+)+\??|^\s*\*[A-Z]{3}\??", text))


def _python_source(path: Path, text: str) -> str:
    if path.suffix.lower() != ".ipynb":
        return text
    nb = json.loads(text)
    cells = []
    for cell in nb.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        src = "".join(cell.get("source") or [])
        cells.append("\n".join(("# " + ln) if ln.lstrip().startswith(("%", "!")) else ln for ln in src.splitlines()))
    return "\n\n".join(cells)


# ------------------------------------------------------------------ MATLAB

def _matlab_code(line: str) -> Tuple[str, str]:
    """A line's code and its comment: % outside strings; ' is a quote unless it transposes."""
    code, quote, prev = [], "", ""
    for i, ch in enumerate(line):
        if quote:
            code.append(ch)
            if ch == quote:
                quote = ""
        elif ch == "%":
            return "".join(code), line[i + 1:]
        elif ch == '"' or (ch == "'" and not (prev.isalnum() or prev in ")]}._'")):
            quote = ch
            code.append(ch)
        else:
            code.append(ch)
        if not ch.isspace():
            prev = ch
    return "".join(code), ""


def _matlab_args(text: str) -> List[str]:
    """A call's arguments, split at its top-level commas."""
    out, depth, cur, quote = [], 0, [], ""
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


def _call_args(code: str, name: str) -> Optional[List[str]]:
    m = re.search(r"(?<![\w.])%s\s*\(" % re.escape(name), code)
    return _matlab_args(code[m.end():]) if m else None


class _Matlab:
    def __init__(self, path: str, text: str, survey: "_Collector"):
        self.path, self.survey = path, survey
        self.known: Dict[str, Any] = {}
        self.loops: Dict[str, List[float]] = {}
        self.conns: Dict[str, Instrument] = {}
        self.outline: List[str] = []
        self.depth = 0
        self.raw = text.splitlines()
        self.addr_hints: Dict[str, List[str]] = {}

    def string(self, expr: str) -> Tuple[str, bool]:
        expr = expr.strip()
        m = re.fullmatch(r"(['\"])(.*)\1", expr, re.S)
        if m:
            return m.group(2).replace(m.group(1) * 2, m.group(1)), True
        if re.fullmatch(r"[A-Za-z]\w*(?:\.\w+)*", expr) and isinstance(self.known.get(expr), str):
            return self.known[expr], True
        args = _call_args(expr, "sprintf")
        if args is not None and expr.lstrip().startswith("sprintf"):
            fmt, ok = self.string(args[0]) if args else ("", False)
            if not ok:
                return "{%s}" % expr, False
            rest = iter(args[1:])
            done = [True]

            def one(mm: "re.Match[str]") -> str:
                a = next(rest, None)
                if a is None:
                    return mm.group(0)
                n = self.number(a)
                if n is not None:
                    return _num(n)
                s, k = self.string(a)
                if k:
                    return s
                done[0] = False
                return "{%s}" % a
            return _FORMAT.sub(one, fmt.replace("\\n", "")), done[0]
        if "+" in expr and ('"' in expr):   # "SENS:FREQ " + num2str(f)
            parts = [p.strip() for p in expr.split("+")]
            texts = [self.string(p) for p in parts]
            return "".join(t if k else "{%s}" % re.sub(r"^(num2str|string|int2str)\((.*)\)$", r"\2", p)
                           for (t, k), p in zip(texts, parts)), all(k for _, k in texts)
        if expr.startswith("[") and expr.endswith("]") and "'" in expr:   # ['SENS:FREQ ', num2str(f)]
            parts = _matlab_args(expr[1:-1])
            texts = [self.string(p) for p in parts]
            return "".join(t if k else "{%s}" % re.sub(r"^(num2str|string|int2str)\((.*)\)$", r"\2", p)
                           for (t, k), p in zip(texts, parts)), all(k for _, k in texts)
        return "{%s}" % expr, False

    def number(self, expr: str) -> Optional[float]:
        expr = expr.strip().rstrip(";")
        try:
            return float(expr)
        except ValueError:
            pass
        v = self.known.get(expr)
        if isinstance(v, (int, float)):
            return float(v)
        if re.fullmatch(r"[-+*/().\deE\s]+", expr) and re.search(r"\d", expr):   # arithmetic on numbers alone
            try:
                return _Values().number(ast.parse(expr, mode="eval").body)
            except SyntaxError:
                return None
        m = re.fullmatch(r"(-?)\s*([A-Za-z]\w*(?:\.\w+)*)\s*([*/])\s*([\d.eE+-]+)", expr)
        if m and isinstance(self.known.get(m.group(2)), (int, float)):
            base = float(self.known[m.group(2)]) * (-1 if m.group(1) else 1)
            k = float(m.group(4))
            return base * k if m.group(3) == "*" else base / k
        return None

    def series(self, expr: str) -> List[float]:
        expr = expr.strip()
        args = _call_args(expr, "linspace")
        if args and len(args) >= 2:
            a, b = self.number(args[0]), self.number(args[1])
            n = self.number(args[2]) if len(args) > 2 else 100
            if a is not None and b is not None and n:
                return _spread(a, b, int(n))
        args = _call_args(expr, "logspace")
        if args and len(args) >= 2:
            a, b = self.number(args[0]), self.number(args[1])
            n = self.number(args[2]) if len(args) > 2 else 50
            if a is not None and b is not None and n:
                return _spread(10 ** a, 10 ** b, int(n), log=True)
        parts = [p.strip() for p in expr.split(":")] if "(" not in expr else []
        if len(parts) in (2, 3):
            nums = [self.number(p) for p in parts]
            if None not in nums:
                a, s, b = (nums[0], 1.0, nums[1]) if len(nums) == 2 else (nums[0], nums[1], nums[2])
                if s and 0 <= (b - a) / s <= 1e5:
                    return [a + s * i for i in range(int(math.floor((b - a) / s + 1e-9)) + 1)]
        v = self.known.get(expr)
        if isinstance(v, list):
            return v
        return []

    def note(self, text: str) -> None:
        self.outline.append("  " * self.depth + text)

    def run(self) -> None:
        stmt, start = "", 0
        for i, raw in enumerate(self.raw, 1):
            code, comment = _matlab_code(raw)
            if code.rstrip().endswith("..."):
                stmt += code.rstrip()[:-3] + " "
                start = start or i
                continue
            stmt += code
            self.statement(stmt.strip(), start or i, comment)
            stmt, start = "", 0

    def statement(self, code: str, line: int, comment: str) -> None:
        if not code:
            return
        where = "%s:%d" % (self.path, line)
        m = re.match(r"^function\s+(?:\[?[\w\s,]*\]?\s*=\s*)?(\w+)\s*(\(([^)]*)\))?", code)
        if m:
            self.depth = 0
            self.note("function %s(%s):" % (m.group(1), m.group(3) or ""))
            self.depth = 1
            return
        m = re.match(r"^(?:par)?for(\s*\(\s*|\s+)(\w+)\s*=\s*(.+)$", code)   # for k = ..., for (k = ...)
        if m:
            over = m.group(3).strip()
            if "(" in m.group(1) and over.endswith(")"):
                over = over[:-1]
            parts = _matlab_args(over)
            over = parts[0] if parts else over   # for k = 1:n, x(k) = ...: the range ends at a top-level comma
            vals = self.series(over)
            if vals:
                self.loops[m.group(2)] = vals
            self.note("for %s = %s%s:" % (m.group(2), over, " (%s)" % _span(vals) if vals else ""))
            self.depth += 1
            for rest in parts[1:]:   # the loop's body on the same line, statements separated by commas
                self.statement(rest, line, "")
            return
        if re.match(r"^(while|if|switch|try)\b", code):
            if code.startswith("while"):
                self.note(code + ":")
            self.depth += 1
            return
        if re.match(r"^end\b", code):
            self.depth = max(0, self.depth - 1)
            return
        for part in _statements(code):
            self.part(part, where, comment)

    def part(self, code: str, where: str, comment: str) -> None:
        m = re.match(r"^([A-Za-z]\w*(?:\.\w+)*)\s*=\s*(.+)$", code)
        if m and not code.startswith("=="):
            lhs, rhs = m.group(1), m.group(2).strip()
            fn = re.match(r"^(\w+)\s*\(", rhs)
            if fn and fn.group(1) in MATLAB_OPEN:
                self.open(lhs, fn.group(1), _matlab_args(rhs[fn.end():]), where, comment)
                return
            owner, _, prop = lhs.rpartition(".")
            if owner in self.conns and prop:
                setting = _setting(prop, self.number(rhs) if self.number(rhs) is not None else self.string(rhs)[0])
                if setting:
                    self.conns[owner].settings.update(setting)
                return
            n = self.number(rhs)
            if n is not None:
                self.known[lhs] = n
            else:
                s, ok = self.string(rhs)
                if ok:
                    self.known[lhs] = s
                    if is_address(s):   # visaResource = "TCPIP0::..."   % the PNA
                        self.addr_hints[lhs] = _hints(comment)
                elif rhs.startswith("[") and re.fullmatch(r"\[[-+\d.eE,\s]*\]", rhs):
                    self.known[lhs] = [float(x) for x in re.split(r"[,\s]+", rhs[1:-1].strip()) if x]
                else:
                    vals = self.series(rhs)
                    if vals:
                        self.known[lhs] = vals
            for name in MATLAB_SEND | MATLAB_READ:   # data = writeread(vna, "...")
                if re.search(r"(?<![\w.])%s\s*\(" % name, rhs):
                    self.call(rhs, where)
                    break
            return
        self.call(code, where)

    def open(self, var: str, fn: str, args: List[str], where: str, comment: str) -> None:
        strs = [self.string(a) for a in args]
        nums = [self.number(a) for a in args]
        addr, expr, settings = "", "", {}
        if fn in ("visadev", "visa", "icdevice"):
            cand = [(s, k, a) for (s, k), a in zip(strs, args) if k and is_address(s)]
            if cand:
                addr = normalize(cand[0][0])
            else:
                expr = args[-1] if args else "?"
        elif fn == "gpib" and len(args) >= 3 and nums[1] is not None and nums[2] is not None:
            addr = "GPIB%d::%d::INSTR" % (int(nums[1]), int(nums[2]))
        elif fn in ("tcpip", "tcpclient") and len(args) >= 2 and strs[0][1] and nums[1] is not None:
            addr = "TCPIP0::%s::%d::SOCKET" % (strs[0][0], int(nums[1]))
        elif fn in ("serialport", "serial") and args and strs[0][1]:
            addr = normalize(strs[0][0])
            if fn == "serialport" and len(args) > 1 and nums[1] is not None:
                settings["baud_rate"] = int(nums[1])
            for key, val in zip(args[1::2], args[2::2]):   # serial('COM3', 'BaudRate', 9600)
                name, ok = self.string(key)
                if ok:
                    s = _setting(name, self.number(val) if self.number(val) is not None else self.string(val)[0])
                    settings.update(s or {})
        else:
            expr = ", ".join(args)
        if not addr and not expr:
            expr = ", ".join(args)
        inst = Instrument(address=addr, names=[var], library="MATLAB " + fn, where=[where], expression=expr,
                          settings=settings)
        inst.hints += _hints(comment) + _hints(Path(self.path).stem) + _hints(" ".join(args))
        for a in args:
            inst.hints += self.addr_hints.get(a.strip(), [])
        line = int(where.rsplit(":", 1)[1])
        for back in range(max(0, line - 4), line - 1):   # comment lines just above
            c = _matlab_code(self.raw[back])[1]
            if c:
                inst.hints += _hints(c)
        self.survey.add(inst)
        self.conns[var] = inst
        self.note("connect %s → %s (%s)" % (var, addr or "{%s}" % expr, fn))

    def call(self, code: str, where: str) -> None:
        args = _call_args(code, "configureTerminator")   # configureTerminator(dev, "CR/LF"), or read then write
        if args and code.lstrip().startswith("configureTerminator") and args[0] in self.conns:
            terms = [s for s, ok in (self.string(a) for a in args[1:3]) if ok]
            if terms:
                read = _setting("Terminator", terms[0]) or {}
                write = _setting("Terminator", terms[-1]) or {}
                self.conns[args[0]].settings.update({k: v for k, v in (("read_termination", read.get(
                    "read_termination")), ("write_termination", write.get("write_termination"))) if v})
            return
        for name in sorted(MATLAB_SEND, key=len, reverse=True):
            args = _call_args(code, name)
            if not args or len(args) < 2:
                continue
            owner = args[0]
            inst = self.conns.get(owner) or self.survey.by_name(owner)
            if inst is None and name in ("write", "fprintf", "query") and not owner.isidentifier():
                continue
            if inst is None and name in ("fprintf",) and owner in ("1", "2", "fid"):
                continue
            if name == "fprintf" and len(args) > 2:   # fprintf(inst, 'FMT', args...)
                text, _ = self.string("sprintf(%s)" % ", ".join(args[1:]))
            else:
                text, _ = self.string(args[1])
            text = text.strip().strip(";").strip()
            if not text or (inst is None and not re.match(r"^\s*[*:]?[A-Z][A-Za-z0-9]*([:? ]|$)", text)):
                continue
            if inst is None:
                inst = self.survey.loose(owner, where)
            query = name in ("writeread", "query") or text.endswith("?")
            numbers: Dict[str, List[float]] = {}
            for expr in _PLACE.findall(text):
                if expr in self.loops:
                    numbers[expr] = self.loops[expr]
                else:
                    n = self.number(expr)
                    if n is not None:
                        numbers[expr] = [n]
            inst.commands.append(Command(text, query, where, numbers,
                                         next((v for v in self.loops if "{%s}" % v in text), "")))
            self.note("%s %s %s" % (owner, "?" if query else "←", text))
            if "*IDN?" in text.upper():
                inst.hints.append("asks *IDN?")
            return
        for name in MATLAB_READ:
            args = _call_args(code, name)
            if args and (args[0] in self.conns or self.survey.by_name(args[0])):
                self.note("%s → read" % args[0])
                return
        args = _call_args(code, "pause")
        if args is not None and code.lstrip().startswith("pause"):
            n = self.number(args[0]) if args else None
            self.note("wait %s s" % (_num(n) if n is not None else (args[0] if args else "?")))
            return
        for name in MATLAB_SAVE:
            args = _call_args(code, name)
            if args is not None and re.match(r"^\s*%s\s*\(" % name, code):
                files = [self.string(a)[0] for a in args if self.string(a)[1]]
                self.note("save %s" % (next((f for f in files if "." in f), files[0] if files else args[0] if args
                                            else "?")))
                return


def _statements(code: str) -> List[str]:
    """A line's statements, split at top-level semicolons."""
    out, depth, cur, quote, prev = [], 0, [], "", ""
    for ch in code:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
        elif ch == '"' or (ch == "'" and not (prev.isalnum() or prev in ")]}._'")):
            quote = ch
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif ch == ";" and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        if not ch.isspace():
            prev = ch
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return [s for s in out if s]


# ------------------------------------------------------------------ the survey

class _Collector:
    """The instruments of every script: one per address (scripts that open the same address share it)."""

    def __init__(self) -> None:
        self.items: List[Instrument] = []

    def add(self, inst: Instrument) -> None:
        self.items.append(inst)

    def by_name(self, name: str) -> Optional[Instrument]:
        """An instrument the scripts open under this name elsewhere (a function gets it as an argument)."""
        short = name.split(".")[-1]
        for inst in self.items:
            if any(n == name or n.split(".")[-1] == short for n in inst.names):
                return inst
        return None

    def loose(self, name: str, where: str) -> Instrument:
        """Commands sent through a name no script opens: an instrument whose address the survey can't see."""
        inst = Instrument(address="", names=[name], library="opened elsewhere", where=[where],
                          expression="opened outside these scripts")
        self.items.append(inst)
        return inst

    def merged(self) -> List[Instrument]:
        out: List[Instrument] = []
        by_addr: Dict[str, Instrument] = {}
        # those with an address first: a function written above the line that opens its instrument sends through it
        for inst in sorted(self.items, key=lambda i: not i.address):
            if not inst.commands and not inst.address and not inst.calls:
                continue
            key = inst.address or ""
            if key and key in by_addr:
                into = by_addr[key]
                into.names += [n for n in inst.names if n not in into.names]
                into.where += inst.where
                into.commands += inst.commands
                into.calls += inst.calls
                into.hints += inst.hints
                for k, v in inst.settings.items():
                    into.settings.setdefault(k, v)
                continue
            if not key:   # no address: the same name as an instrument with one is that instrument
                twin = next((o for o in out if o.address and set(o.names) & set(inst.names)), None)
                if twin is not None:
                    twin.where += inst.where
                    twin.commands += inst.commands
                    twin.calls += inst.calls
                    continue
                twin = next((o for o in out if not o.address and set(o.names) & set(inst.names)), None)
                if twin is not None:
                    twin.where += inst.where
                    twin.commands += inst.commands
                    twin.calls += inst.calls
                    continue
            if key:
                by_addr[key] = inst
            out.append(inst)
        for inst in out:
            seen: set = set()
            inst.hints = [h for h in inst.hints if h and not (h.lower() in seen or seen.add(h.lower()))]
            inst.names = list(dict.fromkeys(inst.names))
            inst.where = list(dict.fromkeys(inst.where))
        return out


def _files(root: Path) -> Tuple[List[Path], List[str]]:
    if root.is_file():
        return ([root] if root.suffix.lower() in SUFFIXES else []), []
    found, skipped = [], []
    for p in sorted(root.rglob("*")):
        if any(part.lower() in SKIP_DIRS or part.startswith(".") for part in p.relative_to(root).parts[:-1]):
            continue
        if p.is_file() and p.suffix.lower() in SUFFIXES:
            if len(found) >= MAX_FILES:
                skipped.append("more than %d scripts: the rest" % MAX_FILES)
                break
            found.append(p)
    return found, skipped


def survey(path: Any, registry: Any = None) -> Survey:
    """Read the scripts in a folder (or one script) and list the instruments and the work in them. registry:
    Kelvin's cards, to name the card that fits each instrument (match())."""
    root = Path(path).expanduser()
    if not root.exists():
        raise FileNotFoundError("%s does not exist" % root)
    files, skipped = _files(root)
    out = Survey(folder=str(root))
    out.skipped += skipped
    collect = _Collector()
    base = root if root.is_dir() else root.parent
    pending: List[Tuple[Script, Any]] = []
    for f in files:
        rel = str(f.relative_to(base))
        try:
            if f.stat().st_size > MAX_BYTES:
                out.skipped.append("%s (over %d MB)" % (rel, MAX_BYTES // 1_000_000))
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            out.skipped.append("%s (%s)" % (rel, e))
            continue
        if f.suffix.lower() == ".m":
            script = Script(rel, "matlab")
            reader = _Matlab(rel, text, collect)
            try:
                reader.run()
            except Exception as e:   # an odd script: what was read so far still counts
                script.problem = "%s: %s" % (type(e).__name__, e)
            script.outline = reader.outline
        else:
            script = Script(rel, "python")
            try:
                source = _python_source(f, text)
                tree = ast.parse(source)
            except (SyntaxError, ValueError) as e:
                script.problem = "it does not parse as Python 3 (%s)" % (e.args[0] if e.args else e)
                out.scripts.append(script)
                continue
            walker = _Python(rel, source, collect)
            for node in ast.walk(tree):   # the functions it defines, so their calls show
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walker.functions.add(node.name)
            try:
                walker.visit(tree)
            except Exception as e:
                script.problem = "%s: %s" % (type(e).__name__, e)
            script.outline = walker.outline
        pending.append((script, None))
    out.instruments = collect.merged()
    for script, _ in pending:
        work = [o for o in script.outline if re.search(r"(connect |[←?] |→ read|\.\w+\()", o)]
        if work or script.problem:
            out.scripts.append(script)
        else:
            out.others += 1
    if registry is not None:
        for inst in out.instruments:
            inst.card = match(inst, registry)
        _annotate(out)
    return out


def _annotate(found: Survey) -> None:
    """Each command in the scripts' outlines with the card parameter it sets or reads: the outline then reads as
    a plan would (smu ← :SOUR:VOLT {v}   [sets smu.V])."""
    by_name: Dict[str, Instrument] = {}
    for inst in found.instruments:
        for n in inst.names:
            by_name[n] = inst
    for script in found.scripts:
        for i, line in enumerate(script.outline):
            m = re.match(r"^(\s*)(\S+) ([←?]) (.*)$", line)
            inst = by_name.get(m.group(2)) if m else None
            if inst is None or not inst.card:
                continue
            hit = next((x for x in inst.card.get("mapped", []) if x["command"] == m.group(4)), None)
            if hit and hit.get("param"):
                name = inst.name or m.group(2)
                what = ("part of reading %s.%s" if hit.get("part") else "reads %s.%s" if hit["read"] else
                        "sets %s.%s") % (name, hit["param"])
                script.outline[i] = "%s   [%s]" % (line, what)


# ------------------------------------------------------------------ the card that fits

def match(inst: Instrument, registry: Iterable[Any]) -> Optional[Dict[str, Any]]:
    """Kelvin's card that fits this instrument best: a model the scripts name (in a comment, a driver's class, the
    file's name) that the card's identity or model says, and the share of its commands the card has. With it: each
    command's parameter on the card, and limits from the values the scripts use."""
    best, best_score, best_why = None, 0.0, ""
    hints = [h for h in inst.hints if h != "asks *IDN?"]
    samples = list(dict.fromkeys(c.sample() for c in inst.commands))
    for card in registry:
        if card.id.startswith("virtual/"):
            continue
        named = _named(card, hints)
        hit = sum(1 for s in samples if _param_for(card, s, samples)[0] is not None) if samples else 0
        share = hit / len(samples) if samples else 0.0
        score = (2.0 if named else 0.0) + share * (1.0 if len(samples) >= 3 else 0.5)
        if score > best_score:
            why = []
            if named:
                why.append("%s named in the scripts" % named)
            if samples:
                why.append("%d of %d commands are on it" % (hit, len(samples)))
            best, best_score, best_why = card, score, "; ".join(why)
    if best is None or best_score < 0.5:   # neither named nor most of its commands
        return None
    return {"id": best.id, "model": best.model, "kind": best.kind, "why": best_why,
            "sure": best_score >= 0.8,   # named, or most of three or more commands on it
            "mapped": mapping(best, inst.commands), "limits": limits(best, inst.commands)}


def _tokens(hint: str) -> List[str]:
    """A hint's model names: "HP 8719ES" is also 8719ES, "Keithley 2400" also 2400."""
    hint = hint.strip()
    out = [hint]
    last = hint.split()[-1] if hint.split() else ""
    if last and last != hint and re.search(r"\d", last):
        out.append(last)
    return [t for t in out if len(t) >= 3 and re.search(r"\d", t)]


def _named(card: Any, hints: Sequence[str]) -> str:
    """The hint that names this card's model: as the instrument would answer *IDN? (N5227B fits the PNA card's
    identity, MODEL 2400 the Keithley's), or as the card's model says it (SR830). A pattern that any model fits
    (one that only checks the maker) names nothing."""
    prefix = (card.idn_example or "").split(",")[0].strip() or (card.vendor or "X")
    for h in hints:
        for token in _tokens(h):
            if card.idn_pattern:
                for form in ("%s", "MODEL %s"):
                    try:
                        hit = re.search(card.idn_pattern, "%s,%s,0,0" % (prefix, form % token), re.I)
                        blank = re.search(card.idn_pattern, "%s,%s,0,0" % (prefix, form % "ZZZ999"), re.I)
                    except re.error:
                        hit = blank = None
                    if hit and not blank:
                        return token
            if re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(token), card.model, re.I):
                return token
    return ""


def _matches(cmd: Any, command: str) -> bool:
    try:
        return cmd is not None and cmd.match(command) is not None
    except Exception:   # a template that can't take this command's shape
        return False


def _same(before: str, command: str) -> bool:
    """A reading's first command and a script's: the same, in any SCPI long or short form."""
    if re.sub(r"\s+", "", before).upper() == re.sub(r"\s+", "", command).upper():
        return True
    from .scpi import Template
    try:
        return Template(before).match(command) is not None
    except ValueError:
        return False


def _param_for(card: Any, command: str, context: Sequence[str] = ()) -> Tuple[Optional[Any], bool, bool]:
    """The card's parameter a command sets or reads: (spec, read?, part?). part: one of the commands a reading
    sends first (arm, choose the trace, take a sweep), so part of that reading; context: the instrument's other
    commands, to tell which reading (the one that shares the most of them)."""
    query = command.rstrip().endswith("?") or "?" in command.split(" ")[0]
    for spec in card.params.values():
        if query and _matches(spec.get_cmd, command):
            return spec, True, False
        if not query and _matches(spec.set_cmd, command):
            return spec, False, False
    for spec in card.params.values():   # HP style: a reading's fetch with no "?" (OUTPFORM)
        if not query and spec.type == "trace" and _matches(spec.get_cmd, command):
            return spec, True, False
    for spec in card.params.values():   # the same command family on another channel (SENS2 for SENSe1)
        cmd = spec.get_cmd if query else spec.set_cmd
        if cmd is None:
            continue
        try:
            if cmd.header_prefix().match(command) is not None and (not query or command.split("?")[0].strip()
                                                                  .upper().endswith(cmd.header.upper()[-3:])):
                return spec, query, False
        except Exception:
            pass
    best, shared = None, 0
    for spec in card.params.values():
        if not spec.get_before or not any(_same(b, command) for b in spec.get_before):
            continue
        n = 1 + sum(1 for c in context if any(_same(b, c) for b in spec.get_before) or _matches(spec.get_cmd, c))
        if n > shared:
            best, shared = spec, n
    if best is not None:
        return best, True, True
    return None, query, False


def mapping(card: Any, commands: Sequence[Command]) -> List[Dict[str, Any]]:
    """Each distinct command and the card's parameter for it (None: not on the card, a raw command)."""
    out, seen = [], set()
    context = list(dict.fromkeys(c.sample() for c in commands))
    for c in commands:
        if c.text in seen:
            continue
        seen.add(c.text)
        spec, read, part = _param_for(card, c.sample(), context)
        out.append({"command": c.text, "param": spec.name if spec is not None else None, "read": read,
                    "part": part})
    return out


def limits(card: Any, commands: Sequence[Command]) -> Dict[str, Tuple[float, float]]:
    """For each numeric drive the scripts set, the range of the values they use (with its safe value), inside the
    card's own limits: limits that fit how this lab already works, for a person to review."""
    used: Dict[str, List[float]] = {}
    for c in commands:
        if c.query:
            continue
        spec, _, part = _param_for(card, c.sample())
        if spec is None or part or not spec.is_drive or not spec.numeric:
            continue
        vals = [v for vs in c.values.values() for v in vs]
        if not vals and not _PLACE.search(c.text):   # a literal value: the number in the command
            try:
                vals = [float(spec.set_cmd.match(c.text))]
            except (TypeError, ValueError):
                vals = []
        if vals:
            used.setdefault(spec.name, []).extend(vals)
    out = {}
    for name, vals in used.items():
        spec = card.params[name]
        lo, hi = min(vals), max(vals)
        if isinstance(spec.safe, (int, float)) and not isinstance(spec.safe, bool):
            lo, hi = min(lo, float(spec.safe)), max(hi, float(spec.safe))
        if spec.limits:
            lo, hi = max(lo, spec.limits[0]), min(hi, spec.limits[1])
        if lo <= hi:
            out[name] = (lo, hi)
    return out
