"""A VI's block diagram as a graph (vifile.py reads the file), and the VI summary made from it.

The graph: the diagrams (the VI's own, and each structure's), the nodes on each, their terminals, and the wires
between them, so where each value comes from can be followed back to a constant, a front-panel control, a loop's
count, an instrument's reading or another node.

summary() reads a VI into the VI summary the LabVIEW survey shows and the translation into a scan takes (the layout
labview.py describes): its controls with the values it is saved with, its linkage, the VISA resources and instrument
commands in it, and its diagram in the order it runs, as loops, cases, sequences, waits, instrument writes, reads and
queries, sub-VI calls (with the commands a sub-VI sends, read from the sub-VI itself when it is beside the VI), DAQ,
file writes, and what a scan has no place for (events, state machines, DLL calls, property nodes, parallel loops),
said as it is, never guessed at. A value is a constant, a control (its saved value is the default), a loop's value,
a reading, or "computed" with how in words. Nothing is ever run.

The functions are told by LabVIEW's own number for each (primResID), the same in every version from 8.5 on; one
Kelvin doesn't know is left out of the steps (it only works a value out), and named where its value is used.
"""
from __future__ import annotations

import heapq
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

from . import vifile
from .vifile import Obj

SUMMARY = 1
TUNNELS = {"lpTun", "innerLpTun", "selTun", "seqTun", "flatSeqTun", "matedLpTun", "matedSeqTun", "sdfTun",
           "externalTun", "xTunnel", "regionTun", "commentTun", "lSR", "rSR", "matedLSR", "matedRSR", "sLoc"}
CALLS = {"iUse", "polyIUse", "dynIUse", "callParentDynIUse", "genIUse", "dynPolyIUse"}
MAX_DEPTH = 40            # structures inside structures, values followed back through nodes
MAX_SUBVI_DEPTH = 2       # sub-VIs read for the commands they send: a driver's VI, and the VIs it calls
MAX_SUBVIS = 60           # sub-VI files read for one summary
MAX_STEPS = 3_000_000     # work for one summary, its sub-VIs' included: objects gone through, values followed
MAX_WORDS = 400           # characters of a value said in words ("computed")
MAX_TEXT = 4096           # characters of a command's text, filled in

# LabVIEW's functions, by their number (primResID)
VISA_WRITE, VISA_READ, VISA_CLEAR = 1926, 1925, 1927
TCP_WRITE, TCP_READ = 1714, 1716
TDMS_WRITE = 2010
TEXT_WRITE = 8080
WAIT_MS = 1302            # Wait (ms) (Wait Until Next ms Multiple has the same terminals; both wait that long)
ADD, SUBTRACT, INCREMENT, EQUAL, ARRAY_SIZE = 1050, 1051, 1057, 1102, 1809
ARITHMETIC = {1050: "+", 1051: "-", 1052: "*", 1053: "/"}
PRIMS = {1050: "Add", 1051: "Subtract", 1052: "Multiply", 1053: "Divide", 1057: "Increment", 1102: "Equal?",
         1166: "Type Cast", 1300: "Tick Count", 1302: "Wait (ms)", 1303: "Get Date/Time In Seconds",
         1340: "One Button Dialog", 1341: "Two Button Dialog", 1502: "Decimal String To Number",
         1503: "String Subset", 1506: "Number To Fractional String", 1510: "Pick Line",
         1511: "Append True/False String", 1512: "Format Value", 1535: "Match Pattern",
         1539: "Spreadsheet String To Array", 1540: "Array To Spreadsheet String", 1600: "Get Date/Time String",
         1713: "TCP Close", 1714: "TCP Write", 1715: "TCP Open", 1716: "TCP Read", 1809: "Array Size",
         1922: "VISA Close", 1923: "VISA Find Resource", 1925: "VISA Read", 1926: "VISA Write", 1927: "VISA Clear",
         1928: "VISA Read STB", 2010: "TDMS Write", 2011: "TDMS Read", 2012: "TDMS Open", 2013: "TDMS Close",
         2301: "VISA Set I/O Buffer Size", 2302: "VISA Flush I/O Buffer", 8070: "Read from Text File",
         8080: "Write to Text File"}
# VIs that handle errors, the panel, files' paths or text (LabVIEW's own, and drivers' error queries): no step of a
# measurement (a serial port's settings are the instrument's connection, which Kelvin's lab config keeps)
QUIET_VIS = ("simple error handler", "general error handler", "merge errors", "clear errors", "error cluster from",
             "error query", "error message", "set enable state", "space constant", "visa configure serial port",
             "close reference", "open/create/replace file", "close file", "file dialog", "build path", "strip path",
             "get file extension", "format date/time", "tick count", "one button dialog", "two button dialog",
             "three button dialog", "display message", "prompt user", "check if file", "create directory")
CONSTANT_VIS = {"space constant": " "}
FILE_VIS = (("write delimited spreadsheet", "spreadsheet"), ("write to spreadsheet", "spreadsheet"),
            ("write spreadsheet", "spreadsheet"), ("write to measurement file", "measurement"),
            ("write labview measurement", "measurement"), ("write characters to file", "text"),
            ("write to text file", "text"), ("write to binary file", "binary"), ("write datalog", "binary"),
            ("tdms write", "tdms"), ("write to tdms", "tdms"))


@dataclass
class Diagram:
    obj: Obj
    nodes: List[Obj] = field(default_factory=list)       # without the diagram's own terminal holder (sRN)
    holder: Optional[Obj] = None                         # sRN: the terminals of constants, controls, tunnels' insides


class Graph:
    """The block diagram's objects by uid, the node each terminal is on, each terminal's data object (its DCO:
    a constant, a control, a tunnel, a function's terminal) and each wire, source first."""

    def __init__(self, vi: vifile.VI):
        self.vi = vi
        self.objs: Dict[int, Obj] = {}
        self.panel: Dict[int, Obj] = {}
        self.term_node: Dict[int, Obj] = {}
        self.term_diagram: Dict[int, Obj] = {}
        self.term_dco: Dict[int, Obj] = {}
        self.dco_terms: Dict[int, List[int]] = {}
        self.source: Dict[int, int] = {}
        self.sinks: Dict[int, List[int]] = {}
        self.dco_owner: Dict[int, Obj] = {}      # a tunnel's (or a structure's own terminal's) structure
        self.diagrams: Dict[int, Diagram] = {}
        self.root: Optional[Diagram] = None
        self.size = 0                            # objects gone through (a summary's budget counts them)
        if vi.panel is not None:
            for o in vi.panel.walk():
                self.size += 1
                if o.uid is not None and o.cls is not None:
                    self.panel[o.uid] = o
        if vi.diagram is None:
            return
        for o in vi.diagram.walk():
            self.size += 1
            if o.uid is not None and o.cls is not None:
                self.objs[o.uid] = o
        for o in vi.diagram.walk():
            if o.kind == "diag":
                self._diagram(o)
        top = vi.diagram.get("root") if vi.diagram.kind != "diag" else vi.diagram
        self.root = self.diagrams.get(top.uid) if top is not None and top.uid is not None else None

    def resolve(self, o: Optional[Obj]) -> Optional[Obj]:
        """An object, or the one a reference (an element with only a uid) stands for."""
        if o is None:
            return None
        if o.cls is not None:
            return o
        if o.uid is not None:
            return self.objs.get(o.uid) or self.panel.get(o.uid)
        return None

    def _diagram(self, d: Obj) -> None:
        out = Diagram(d)
        if d.uid is not None:
            self.diagrams[d.uid] = out
        for e in d.items("nodeList"):
            node = self.resolve(e)
            if node is None:
                continue
            if node.kind == "sRN":
                out.holder = node
            else:
                out.nodes.append(node)
            for t in node.items("termList"):
                if t.uid is None or t.cls is None:
                    continue
                self.term_node[t.uid] = node
                self.term_diagram[t.uid] = d
                dco = self.resolve(t.get("dco"))
                if dco is not None:
                    self.term_dco[t.uid] = dco
                    if dco.uid is not None:
                        self.dco_terms.setdefault(dco.uid, []).append(t.uid)
                        if node.kind != "sRN":
                            self.dco_owner.setdefault(dco.uid, node)
        for s in d.items("signalList"):
            ends = [e.uid for e in s.items("termList") if e.uid is not None]
            if len(ends) < 2:
                continue
            first, rest = ends[0], ends[1:]
            self.sinks.setdefault(first, []).extend(rest)
            for t in rest:
                self.source.setdefault(t, first)

    def terms(self, node: Obj) -> List[int]:
        return [t.uid for t in node.items("termList") if t.uid is not None]

    def inputs(self, node: Obj) -> List[int]:
        """A node's terminals that a wire comes into, in the node's order."""
        return [t for t in self.terms(node) if t in self.source]

    def outputs(self, node: Obj) -> List[int]:
        return [t for t in self.terms(node) if t in self.sinks]

    def parm(self, term: int) -> Optional[int]:
        """A primitive's terminal's number (parmIndex: 0 when not saved)."""
        dco = self.term_dco.get(term)
        if dco is None:
            return None
        n = dco.number("parmIndex")
        return n if n is not None else 0

    def by_parm(self, node: Obj) -> Dict[int, int]:
        return {self.parm(t) or 0: t for t in self.terms(node)}

    def ends(self, dco: Obj) -> List[int]:
        """A tunnel's terminals: one inside each of its structure's diagrams, then the one outside."""
        return [e.uid for e in dco.items("termList") if e.uid is not None]

    def subdiagrams(self, node: Obj) -> List[Diagram]:
        out = []
        for e in node.items("diagramList"):
            d = self.resolve(e)
            if d is not None and d.uid in self.diagrams:
                out.append(self.diagrams[d.uid])
        return out

    def nodes(self) -> Iterator[Tuple[Obj, Diagram]]:
        """Every node of every diagram."""
        for d in self.diagrams.values():
            for n in d.nodes:
                yield n, d

    def inner_term(self, dco: Optional[Obj]) -> Optional[int]:
        """The terminal inside a structure's diagram that stands for one of the structure's own data objects (a
        loop's count i, a while loop's condition)."""
        if dco is None or dco.uid is None:
            return None
        terms = self.dco_terms.get(dco.uid) or []
        return terms[0] if terms else None


def _set(o: Optional[Obj]) -> bool:
    """Whether a flag the heap keeps is set: there, with content that isn't all zeros."""
    return o is not None and (o.value is True or (isinstance(o.value, (bytes, bytearray)) and any(o.value)))


def bounds(o: Obj) -> Tuple[int, int, int, int]:
    """An object's place on the diagram: (top, left, bottom, right)."""
    raw = o.raw("bounds") or b""
    if len(raw) == 8:
        return tuple(int.from_bytes(raw[i:i + 2], "big", signed=True) for i in range(0, 8, 2))  # type: ignore
    return (0, 0, 0, 0)


# ------------------------------------------------------------------ values

@dataclass
class Val:
    """Where a value comes from: a constant (value), a control (its name), a loop's value (its var), an
    instrument's reading (its name), text built of parts (strings and Vals), or worked out in a way a summary can't
    say (computed: how, in words). scale and offset: a number used as x * scale + offset. fmt: the format code
    a constant is written into text with (%d, %.3f)."""
    kind: str
    value: Any = None
    parts: Optional[List[Any]] = None
    scale: Optional[float] = None
    offset: Optional[float] = None
    fmt: str = ""

    def source(self) -> Dict[str, Any]:
        """As the summary writes a value: {"value": 1.5}, {"control": "Start (V)"}, {"loop": "V"}, {"read": "I"},
        {"computed": "how"}, with scale and offset."""
        if self.kind == "text":
            text, values = render(self)
            if not values:
                return {"value": text}
            return {"computed": "the text %s" % text}
        out: Dict[str, Any] = {self.kind: self.value}
        if self.scale is not None and self.scale != 1:
            out["scale"] = self.scale
        if self.offset is not None and self.offset != 0:
            out["offset"] = self.offset
        return out

    def words(self) -> str:
        if self.kind == "value":
            return repr(self.value)
        if self.kind == "control":
            return "the control %s" % self.value
        if self.kind == "loop":
            return "the loop's %s" % self.value
        if self.kind == "read":
            return "the reading %s" % self.value
        if self.kind == "text":
            return "the text %s" % render(self)[0]
        return str(self.value)


def computed(how: str) -> Val:
    return Val("computed", how if len(how) <= MAX_WORDS else how[:MAX_WORDS - 1] + "…")


def _key(v: Val, taken: Set[str]) -> str:
    """A placeholder's name in a command's text: the control's, loop's or reading's (letters, digits, _)."""
    base = {"control": str(v.value), "loop": str(v.value), "read": str(v.value)}.get(v.kind, "x")
    base = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", base)   # "Voltage (V)" is Voltage
    base = re.sub(r"[^A-Za-z0-9_]+", "_", base).strip("_") or "x"
    if base[0].isdigit():
        base = "x" + base
    key, n = base, 2
    while key in taken:
        key, n = "%s%d" % (base, n), n + 1
    taken.add(key)
    return key


def render(v: Val) -> Tuple[str, Dict[str, Dict[str, Any]]]:
    """Text with a {name} for each value the diagram fills in, and those values: at most MAX_TEXT characters (text
    built of the same parts over and over is cut there, with "…")."""
    taken: Set[str] = set()
    values: Dict[str, Dict[str, Any]] = {}
    out: List[str] = []
    size = [0]

    def put(text: str) -> None:
        out.append(text[:MAX_TEXT + 1 - size[0]])
        size[0] += len(out[-1])

    def add(part: Any, depth: int) -> None:
        if size[0] > MAX_TEXT or depth > MAX_DEPTH:
            return
        if isinstance(part, str):
            put(part.replace("{", "{{").replace("}", "}}"))
        elif isinstance(part, Val) and part.kind == "text":
            for p in part.parts or []:
                add(p, depth + 1)
        elif isinstance(part, Val) and part.kind == "value":
            put(_written(part.value, part.fmt).replace("{", "{{").replace("}", "}}"))
        elif isinstance(part, Val):
            key = _key(part, taken)
            values[key] = part.source()
            put("{%s}" % key)
    add(v, 0)
    text = "".join(out)
    return (text if size[0] <= MAX_TEXT else text[:MAX_TEXT] + "…"), values


def _plain(x: Any) -> str:
    if isinstance(x, bool):
        return "1" if x else "0"
    if isinstance(x, float) and x == x and abs(x) < 1e15 and x == int(x):
        return str(int(x))
    return str(x)


def _written(x: Any, code: str) -> str:
    """A constant as a format code writes it into text: LabVIEW's codes, without what only LabVIEW has (# and _
    for trailing zeros and significant digits, <...> for times)."""
    if not code:
        return _plain(x)
    py = re.sub(r"<[^>]*>", "", code)
    py = re.sub(r"_\d*", "", py).replace("#", "").replace("^", "")
    py = py[:-1] + "d" if py.endswith("u") else py
    try:
        if py.endswith(("d", "i", "x", "X", "o")) and isinstance(x, (bool, float, int)):
            return py % int(round(float(x)))
        if py.endswith(("e", "E", "f", "F", "g", "G")) and isinstance(x, (bool, int, float)):
            return py % float(x)
        if py.endswith("s"):
            return py % _plain(x)
    except (TypeError, ValueError, OverflowError):
        pass
    return _plain(x)


_FORMAT = re.compile(r"%(?:%|[.,];|[-+ #0^]*\d*(?:\.\d*)?(?:_\d+)?(?:<[^>]*>)?[a-zA-Z]|\[[^\]]*\])")


def formatted(fmt: str, args: List[Val]) -> Val:
    """Format Into String's text: its format with each %-code standing for the next value."""
    parts: List[Any] = []
    at, k = 0, 0
    for m in _FORMAT.finditer(fmt):
        code = m.group(0)
        parts.append(fmt[at:m.start()])
        at = m.end()
        if code == "%%":
            parts.append("%")
        elif code in ("%.;", "%,;"):
            continue   # which decimal point the numbers after it take: no value
        elif k < len(args):
            arg = args[k]
            parts.append(Val(arg.kind, arg.value, arg.parts, arg.scale, arg.offset, code) if arg.kind == "value"
                         else arg)
            k += 1
        else:
            parts.append(code)
    parts.append(fmt[at:])
    return Val("text", parts=[p for p in parts if p != ""])


# ------------------------------------------------------------------ reading a VI into its summary

Link = Callable[[str, str], Dict[str, Any]]


def _link(name: str, where: str = "") -> Dict[str, Any]:
    """A linkage entry, as labview.link() makes one (maker and model left to it)."""
    library, _, sub = name.rpartition(":") if ":" in name else ("", "", name)
    return {"name": name, "library": library, "vi": sub, "where": where, "maker": "", "model": ""}


def _control(c: vifile.Control) -> Dict[str, Any]:
    """A control as the summary lists it; its unit from its name ("Start (V)"), when that is a unit's."""
    from .imports import unit_of
    m = re.search(r"[\(\[]\s*([^()\[\]]{1,12})\s*[\)\]]\s*$", c.name)
    return {"name": c.name, "type": c.kind, "unit": unit_of(m.group(1)) if m else "",
            "default": _jsonable(c.default) if c.has_default else None,
            "choices": [_jsonable(x) for x in c.choices], "indicator": c.indicator}


def _jsonable(x: Any) -> Any:
    """A value as the summary keeps it: plain JSON, with no number that isn't finite (Inf, NaN: None)."""
    if isinstance(x, complex):
        return [_jsonable(x.real), _jsonable(x.imag)]
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    return x


def all_finite(x: Any, depth: int = 0) -> bool:
    """Whether a value holds only finite numbers (no Inf or NaN, in its parts either)."""
    if isinstance(x, float):
        return math.isfinite(x)
    if isinstance(x, complex):
        return math.isfinite(x.real) and math.isfinite(x.imag)
    if isinstance(x, (list, tuple)):
        return depth > 40 or all(all_finite(v, depth + 1) for v in x)
    if isinstance(x, dict):
        return depth > 40 or all(all_finite(v, depth + 1) for v in x.values())
    return True


def plain_numbers(x: Any, depth: int = 0) -> Tuple[Any, int]:
    """A summary (or any part of it) with each number that isn't finite made None: (the summary, how many were)."""
    if isinstance(x, float) and not math.isfinite(x):
        return None, 1
    if isinstance(x, (list, tuple, dict)) and depth <= 400:
        n = 0
        items = x.items() if isinstance(x, dict) else enumerate(x)
        out: Any = {} if isinstance(x, dict) else []
        for k, v in items:
            v, m = plain_numbers(v, depth + 1)
            n += m
            if isinstance(out, dict):
                out[k] = v
            else:
                out.append(v)
        return out, n
    return x, 0


_SCPI = re.compile(r"^\s*(\*[A-Za-z]{2,5}\??|:?[A-Za-z][A-Za-z0-9]*(?::[A-Za-z*][A-Za-z0-9]*)+\??)(?:[\s,].*)?$",
                   re.S)


def looks_scpi(text: str) -> bool:
    """Whether a string constant reads like an instrument command (*RST, :SOUR:VOLT 1, MEAS:CURR?)."""
    return bool(text) and len(text) <= 200 and bool(_SCPI.match(text))


class TooBig(vifile.TooBig):
    """A summary that would take more than MAX_STEPS: its diagram isn't followed further."""


@dataclass
class _Shared:
    """What one summary shares with the sub-VIs read for it: the folder sub-VIs are read from (never outside it),
    Kelvin's own folders (never read), the files read (by path), each VI's graph, and what is left of its budget."""
    folder: str = ""
    own: Tuple[Any, ...] = ()
    files: Dict[str, Optional[vifile.VI]] = field(default_factory=dict)
    graphs: Dict[int, "Graph"] = field(default_factory=dict)
    paths: Dict[Tuple[str, Tuple[str, ...]], Optional[Path]] = field(default_factory=dict)
    steps: int = field(default_factory=lambda: MAX_STEPS)

    def spend(self, n: int = 1) -> None:
        self.steps -= n
        if self.steps < 0:
            raise TooBig("its block diagram: bigger than Kelvin follows in one VI")

    def graph(self, vi: vifile.VI) -> "Graph":
        g = self.graphs.get(id(vi))
        if g is None:
            g = self.graphs[id(vi)] = Graph(vi)
            self.spend(g.size)
        return g

    def within(self, under: Path, names: List[str]) -> Optional[Path]:
        """vifile.within, for the summary's folder: each path worked out once."""
        key = (str(under), tuple(names))
        if key not in self.paths:
            self.paths[key] = vifile.within(self.folder, names, under)
        return self.paths[key]


class _Reader:
    """Reads one VI's diagram into the summary's nodes. args: when the VI is read as a sub-VI a call fills in,
    the value of each of its controls (by name) as the caller gives it; a control not given has its saved value.
    shared: what the summary it is read for shares (by default the VI's own: sub-VIs from its folder)."""

    def __init__(self, vi: vifile.VI, link: Link, depth: int, files: Dict[str, Optional[vifile.VI]],
                 args: Optional[Dict[str, Val]] = None, shared: Optional[_Shared] = None):
        self.vi = vi
        self.shared = shared or _Shared(folder=str(Path(vi.path).parent) if vi.path else "", files=files)
        self.g = self.shared.graph(vi)
        self.link = link
        self.depth = depth
        self.files = self.shared.files
        self.args = args
        self.memo: Dict[int, Val] = {}            # what each terminal gives, once followed (produced)
        self.cut = 0                              # values cut short so far (a loop of wires, MAX_DEPTH)
        self.controls = {c.uid: c for c in vi.controls if c.uid is not None}
        self.by_ddo = {c.ddo: c for c in vi.controls if c.ddo is not None}
        self.links = {}
        for ln in vi.links:
            self.links.setdefault(ln.name.rpartition(":")[2].lower(), ln)
        self.loop_vars: Dict[int, str] = {}
        self.loop_values: Dict[int, int] = {}      # a loop's indexing tunnel (its values), by the loop's uid
        self.read_names: Dict[int, str] = {}
        self.taken: Set[str] = set()
        self.gaps: List[str] = []
        self.visa: List[Dict[str, Any]] = []
        self.strings: Dict[str, None] = {}         # string constants, in the order met
        self.formats: Set[str] = set()

    # -------------------------------------------------- names of loops and readings

    def name_loops(self, d: Optional[Diagram], level: int = 0) -> None:
        if d is None or level > MAX_DEPTH:
            return
        for n in sorted(d.nodes, key=lambda o: (bounds(o)[1], bounds(o)[0])):
            if n.kind in ("forLoop", "whileLoop", "timeLoop"):
                var = ""
                tunnel = self._indexing_input(n)
                if tunnel is not None:   # the loop takes each value of an array: named after the array's control
                    self.loop_values[n.uid] = tunnel
                    src = self.origin(tunnel, set(), 1)
                    if src.kind == "control":
                        var = _key(src, set())
                if not var or var in self.taken:
                    var = next((v for v in ("i", "j", "k", "l", "m", "n") + tuple("i%d" % k for k in range(2, 99))
                                if v not in self.taken), "")
                    k = 99
                    while not var or var in self.taken:
                        var, k = "i%d" % k, k + 1
                self.taken.add(var)
                self.loop_vars[n.uid] = var
            for sub in self.g.subdiagrams(n):
                self.name_loops(sub, level + 1)
        if level == 0:   # what was followed while the loops had no names yet is followed again, with them
            self.memo.clear()

    def _indexing_input(self, loop: Obj) -> Optional[int]:
        """A loop's first auto-indexing input tunnel (the loop takes each of an array's values), by its outer
        terminal."""
        for t in loop.items("termList"):
            dco = self.g.resolve(t.get("dco"))
            if dco is None or dco.kind != "lpTun" or dco.get("innerLpTunDCO") is None:
                continue
            ends = self.g.ends(dco)
            if ends and ends[-1] in self.g.source:
                return ends[-1]
        return None

    def reading(self, node: Obj, term: Optional[int] = None) -> str:
        """The name of what a read or a sub-VI's call gives (by its output terminal): the indicator it is shown
        in, else the sub-VI's own name for that output, else "reading N"."""
        terms = [term] if term is not None else [t for t in self.g.outputs(node) if not self._is_error(t)
                                                 and self._type(t) != "visa"]
        key = terms[0] if terms else -(node.uid or 0)
        if key in self.read_names:
            return self.read_names[key]
        name = next((found for found in (self._shown_in(t, 0) for t in terms) if found), "")
        if not name and node.kind in CALLS and terms:
            _, link = self._call_name(node)
            sub = self.instance(node, self.subvi(link), link)[0] if link is not None else None
            c = self._terminals(sub).get(self.g.terms(node).index(terms[0])) if sub is not None else None
            name = c.name if c is not None else ""
        if not name:
            name = "reading %d" % (sum(1 for v in self.read_names.values() if v.startswith("reading ")) + 1)
        base, k = name, 2
        while name in self.read_names.values():
            name, k = "%s %d" % (base, k), k + 1
        self.read_names[key] = name
        return name

    def _shown_in(self, term: int, level: int) -> str:
        """The indicator a value ends in, through tunnels and text-to-number functions."""
        if level > 4:
            return ""
        self.shared.spend()
        for sink in self.g.sinks.get(term, []):
            dco = self.g.term_dco.get(sink)
            if dco is None:
                continue
            if dco.kind == "fPDCO" and dco.uid in self.controls and self.controls[dco.uid].indicator:
                return self.controls[dco.uid].name
            if dco.kind in TUNNELS:
                ends = self.g.ends(dco)
                for e in ends:
                    if e != sink:
                        found = self._shown_in(e, level + 1)
                        if found:
                            return found
            node = self.g.term_node.get(sink)
            if node is not None and node.kind in ("scanf", "prim"):   # text made a number (Scan From String...)
                for out in self.g.outputs(node):
                    if self._type(out) in ("number", "array"):
                        found = self._shown_in(out, level + 1)
                        if found:
                            return found
        return ""

    # -------------------------------------------------- where values come from

    def origin(self, term: Optional[int], seen: Set[int], level: int = 0) -> Val:
        """What feeds an input terminal."""
        if term is None:
            return computed("nothing (not wired)")
        src = self.g.source.get(term)
        if src is None:
            return computed("nothing (not wired)")
        return self.produced(src, seen, level)

    def produced(self, src: int, seen: Set[int], level: int) -> Val:
        """What a terminal gives the wires that leave it: followed once, then known (a value used many times, or
        x + x + x..., costs no more than once)."""
        if level > MAX_DEPTH or src in seen:
            self.cut += 1
            return computed("a value that goes round a loop")
        known = self.memo.get(src)
        if known is not None:
            return known
        self.shared.spend()
        cut = self.cut
        v = self._produced(src, seen | {src}, level)
        if self.cut == cut:   # not cut short anywhere: the same wherever it is asked for
            self.memo[src] = v
        return v

    def _produced(self, src: int, seen: Set[int], level: int) -> Val:
        dco = self.g.term_dco.get(src)
        node = self.g.term_node.get(src)
        if dco is None:
            return computed("a value Kelvin couldn't follow")
        k = dco.kind
        if k == "bDConstDCO":
            return self.constant(dco)
        if k == "fPDCO":
            c = self.controls.get(dco.uid)
            if c is None:
                return computed("a front-panel control")
            if self.args is not None:   # a sub-VI, as its caller fills it in
                if c.name in self.args:
                    return self.args[c.name]
                if c.has_default:
                    return Val("value", _jsonable(c.default))
                return computed("%s of %s" % (c.name, self.vi.name))
            return Val("control", c.name)
        if k == "lCnt":
            loop = self._loop_of(dco)
            if loop is not None and loop.uid in self.loop_vars and loop.uid not in self.loop_values:
                return Val("loop", self.loop_vars[loop.uid])
            return computed("the loop's count (i)")
        if k == "lMax":
            ends = self.g.ends(dco)
            return self.origin(ends[-1], seen, level + 1) if ends else computed("the loop's number of steps")
        if k in ("lSR", "matedLSR"):
            ends = self.g.ends(dco)
            if ends and src != ends[-1]:
                start = self.origin(ends[-1], seen, level + 1)
                if start.kind in ("control", "value") and self._kind_of_type(dco) == "visa":
                    return start
                return computed("a value the loop keeps from step to step (a shift register)")
            return computed("a shift register's last value")
        if k in TUNNELS:
            return self._through_tunnel(dco, src, seen, level)
        if node is None:
            return computed("a value Kelvin couldn't follow")
        return self.node_value(node, src, seen, level)

    def _loop_of(self, dco: Obj) -> Optional[Obj]:
        p = dco.parent
        while p is not None and p.kind not in ("forLoop", "whileLoop", "timeLoop"):
            p = p.parent
        return p

    def _kind_of_type(self, dco: Obj) -> str:
        return vifile.kind_of(self.vi.types.of_heap(dco.number("typeDesc")))

    def _through_tunnel(self, dco: Obj, src: int, seen: Set[int], level: int) -> Val:
        ends = self.g.ends(dco)
        if not ends:
            return computed("a value through a tunnel Kelvin couldn't follow")
        if src != ends[-1]:   # its inside: what comes in from outside the structure
            outer = ends[-1]
            loop = self.g.dco_owner.get(dco.uid)
            if dco.kind == "lpTun" and dco.get("innerLpTunDCO") is not None and loop is not None and \
                    self.loop_values.get(loop.uid) == outer and loop.uid in self.loop_vars:
                return Val("loop", self.loop_vars[loop.uid])
            v = self.origin(outer, seen, level + 1)
            if dco.kind == "lpTun" and dco.get("innerLpTunDCO") is not None:
                return computed("each value of %s in turn" % v.words()) if v.kind != "loop" else v
            return v
        inside = [self.origin(e, seen, level + 1) for e in ends[:-1] if e in self.g.source]
        if not inside:
            return computed("a value from inside a structure")
        if all(v.source() == inside[0].source() for v in inside):
            if dco.kind == "lpTun" and dco.get("innerLpTunDCO") is not None:
                return computed("every value of %s, one per step" % inside[0].words())
            return inside[0]
        return computed("a value that depends on the case")

    def constant(self, dco: Obj) -> Val:
        t = self.vi.types.of_heap(dco.number("typeDesc"))
        why: List[str] = []
        v, ok = vifile.decode(dco.raw("ConstValue") or dco.raw("DefaultData"), t, self.vi.types, why)
        if not ok:
            return computed("a constant %s" % ("bigger than Kelvin reads" if why else "Kelvin couldn't read"))
        if not all_finite(v):   # Inf or NaN: no number a scan can take
            return computed("a constant that isn't a finite number (%s)" % _plain(v)[:60])
        if isinstance(v, str) and v:
            self.strings.setdefault(v)
        kind = vifile.kind_of(t)
        if kind == "visa" and isinstance(v, str) and v:
            self._visa(v, "")
        return Val("value", _jsonable(v))

    def _visa(self, resource: Any, control: str) -> None:
        entry = {"resource": resource, "control": control}
        if entry not in self.visa:
            self.visa.append(entry)

    def node_value(self, node: Obj, src: int, seen: Set[int], level: int) -> Val:
        k = node.kind
        if k == "concat":
            ins = [e.uid for e in node.items("dcoList")]
            parts = []
            for t in self.g.terms(node):
                dco = self.g.term_dco.get(t)
                if dco is not None and dco.uid in ins and t in self.g.source:
                    parts.append((ins.index(dco.uid), self.origin(t, seen, level + 1)))
            return Val("text", parts=[p for _, p in sorted(parts, key=lambda x: x[0])])
        if k == "printf":
            perm = [e.uid for e in node.items("permDCOList")]
            args = [e.uid for e in node.items("dcoList")]
            fmt: Optional[Val] = None
            vals: List[Tuple[int, Val]] = []
            for t in self.g.terms(node):
                dco = self.g.term_dco.get(t)
                if dco is None or t not in self.g.source:
                    continue
                if perm and dco.uid == perm[0]:
                    fmt = self.origin(t, seen, level + 1)
                elif dco.uid in args:
                    vals.append((args.index(dco.uid), self.origin(t, seen, level + 1)))
            if fmt is not None and fmt.kind == "value" and isinstance(fmt.value, str):
                self.formats.add(fmt.value)   # shown filled in, where it is sent
                return formatted(fmt.value, [v for _, v in sorted(vals, key=lambda x: x[0])])
            return computed("text made by Format Into String")
        if k == "scanf":
            source = None
            perm = [e.uid for e in node.items("permDCOList")]
            for t in self.g.inputs(node):
                dco = self.g.term_dco.get(t)
                if dco is not None and len(perm) > 1 and dco.uid == perm[1]:
                    source = self.origin(t, seen, level + 1)
            if source is not None and source.kind == "read":
                return source
            return computed("a number read from text (Scan From String)")
        if k == "prim":
            return self.prim_value(node, src, seen, level)
        if k in CALLS:
            name, link = self._call_name(node)
            low = name.lower()
            for key, text in CONSTANT_VIS.items():
                if key in low:
                    return Val("value", text)
            if link is not None and link.where not in ("vi.lib", "LabVIEW") and \
                    any(self._type(t) == "visa" for t in self.g.inputs(node)):
                return Val("read", self.reading(node, src))   # what an instrument driver's VI reads
            ins = [self.origin(t, seen, level + 1).words() for t in self.g.inputs(node) if not self._is_error(t)]
            return computed("the output of %s%s" % (name or "a sub-VI", " (of %s)" % ", ".join(ins[:4])
                                                    if ins else ""))
        if k == "fBox":
            text = next((t for t in (vifile.text_of(o) for o in node.walk() if o.get("textRec") is not None)
                         if "=" in t), "")
            return computed("a formula node (%s)" % " ".join(text.split())[:160] if text else "a formula node")
        if k in ("nMux", "demux"):
            return computed("a part of a cluster")
        if k in ("aIndx",):
            ins = self.g.inputs(node)
            first = self.origin(ins[0], seen, level + 1) if ins else computed("an array")
            return computed("an element of %s" % first.words())
        if k in ("aBuild",):
            return computed("an array built on the diagram")
        if k == "gRef":
            return computed("a local or global variable")
        if k in ("propNode",):
            return computed("a property read with a property node")
        return computed("a value worked out on the diagram (%s)" % k)

    def prim_value(self, node: Obj, src: int, seen: Set[int], level: int) -> Val:
        rid = node.number("primResID") or 0
        parms = self.g.by_parm(node)
        out = self.g.parm(src)
        if rid == VISA_READ and out == 2 or rid == TCP_READ and out == 2:
            return Val("read", self.reading(node, src))
        if rid in (VISA_READ, VISA_WRITE, VISA_CLEAR, 1928, 2301, 2302) and out == 3:
            return self.origin(parms.get(11), seen, level + 1)   # the resource passed on
        if rid in ARITHMETIC:
            x = self.origin(parms.get(2), seen, level + 1)
            y = self.origin(parms.get(1), seen, level + 1)
            done = _arith(ARITHMETIC[rid], x, y)
            if done is not None:
                return done
            return computed("%s %s %s" % (x.words(), ARITHMETIC[rid], y.words()))
        if rid == 1506 or self._number_to_text(node, src):
            number = [self.origin(t, seen, level + 1) for p, t in sorted(parms.items())
                      if t in self.g.source and p == 4] if rid == 1506 else \
                     [self.origin(t, seen, level + 1) for t in self.g.inputs(node)
                      if self._type(t) == "number"]
            if len(number) == 1:
                return Val("text", parts=[number[0]])
        if rid == 1511:   # Append True/False String
            base = self.origin(parms.get(4), seen, level + 1) if parms.get(4) in self.g.source else Val("text", parts=[])
            choice = self.origin(parms.get(1), seen, level + 1)
            yes = self.origin(parms.get(3), seen, level + 1)
            no = self.origin(parms.get(2), seen, level + 1)
            if choice.kind == "value":
                return Val("text", parts=[base, yes if choice.value else no])
            return computed("%s or %s, by %s" % (yes.words(), no.words(), choice.words()))
        if rid == 1510:   # Pick Line
            lines = self.origin(parms.get(2), seen, level + 1)
            index = self.origin(parms.get(1), seen, level + 1)
            base = self.origin(parms.get(3), seen, level + 1) if parms.get(3) in self.g.source else None
            if lines.kind == "value" and isinstance(lines.value, str):
                options = lines.value.replace("\r\n", "\n").split("\n")
                if index.kind == "value" and isinstance(index.value, (int, float)) and 0 <= index.value < len(options):
                    picked: Any = options[int(index.value)]
                else:
                    picked = computed("line %s of %s" % (index.words(), " / ".join(repr(o) for o in options)))
                return Val("text", parts=[p for p in (base, picked) if p is not None])
        name = PRIMS.get(rid)
        return computed("the output of %s" % name if name else "a value worked out by a LabVIEW function")

    def _type(self, term: int) -> str:
        dco = self.g.term_dco.get(term)
        return self._kind_of_type(dco) if dco is not None else ""

    def _is_error(self, term: int) -> bool:
        """An error cluster's terminal (error in, error out): status, code and source."""
        dco = self.g.term_dco.get(term)
        t = self.vi.types.of_heap(dco.number("typeDesc")) if dco is not None else None
        p = vifile.plain(t)
        if p is None or p.kind != "cluster":
            return False
        if "error" in ((t.label if t else "") + (p.label or "")).lower():
            return True
        kinds = [vifile.plain(self.vi.types.item(i)) for i in p.items]
        return len(kinds) == 3 and [k.kind if k else "" for k in kinds] == ["bool", "number", "string"]

    def _number_to_text(self, node: Obj, src: int) -> bool:
        """A function that turns one number into text (Number To ... String): its output is text and one number
        comes in (the rest are constants: width, precision)."""
        if self._type(src) != "string":
            return False
        ins = self.g.inputs(node)
        numbers = [t for t in ins if self._type(t) == "number"]
        others = [t for t in ins if t not in numbers]
        return len(numbers) >= 1 and not others and len(ins) <= 4 and \
            sum(1 for t in numbers if self.g.term_dco.get(self.g.source[t]) is not None and
                self.g.term_dco[self.g.source[t]].kind != "bDConstDCO") == 1

    # -------------------------------------------------- resources

    def resource(self, term: Optional[int], level: int = 0) -> Any:
        """The VISA resource a function works on: its address (or NI MAX alias), the control it is set in, or how
        it was opened, following it back through the functions and sub-VIs it passes through."""
        if term is None or level > MAX_DEPTH:
            return {"computed": "a VISA session"}
        self.shared.spend()
        src = self.g.source.get(term)
        if src is None:
            return {"computed": "no VISA resource wired"}
        dco = self.g.term_dco.get(src)
        node = self.g.term_node.get(src)
        if dco is not None and dco.kind == "fPDCO" and dco.uid in self.controls:
            c = self.controls[dco.uid]
            if c.has_default and isinstance(c.default, str) and c.default:
                self._visa(c.default, c.name)
            return {"control": c.name}
        if dco is not None and dco.kind == "bDConstDCO":
            v = self.constant(dco)
            return v.value if v.kind == "value" else {"computed": "a VISA resource constant"}
        if dco is not None and dco.kind in TUNNELS:
            ends = self.g.ends(dco)
            if ends and src != ends[-1]:
                return self.resource(ends[-1], level + 1)
            inside = [e for e in ends[:-1] if e in self.g.source]
            return self.resource(inside[0], level + 1) if inside else {"computed": "a VISA session"}
        if node is not None and node.kind == "prim":
            parms = self.g.by_parm(node)
            if 11 in parms:
                return self.resource(parms[11], level + 1)
        if node is not None and (node.kind in CALLS or node.kind in ("propNode", "invokeNode")):
            for t in self.g.inputs(node):
                if self._type(t) == "visa":
                    return self.resource(t, level + 1)
            return {"computed": "a VISA session from %s" % (vifile.label_of(node) or "a sub-VI")}
        return {"computed": "a VISA session"}

    # -------------------------------------------------- the diagram, in the order it runs

    def order(self, d: Diagram) -> List[Obj]:
        """A diagram's nodes in the order they run: each after the nodes whose outputs it takes, the rest left to
        right (as LabVIEW's diagrams read)."""
        nodes = list({id(n): n for n in d.nodes}.values())   # each once
        self.shared.spend(len(nodes))
        left = sorted(nodes, key=lambda n: (bounds(n)[1], bounds(n)[0]))
        rank = {id(n): i for i, n in enumerate(left)}
        waiting = [0] * len(left)                        # inputs from nodes not done yet
        feeds: List[List[int]] = [[] for _ in left]
        for i, n in enumerate(left):
            sources = set()
            for t in self.g.inputs(n):
                src_node = self.g.term_node.get(self.g.source[t])
                if src_node is not None and id(src_node) in rank and src_node is not n:
                    sources.add(rank[id(src_node)])
            waiting[i] = len(sources)
            for s in sources:
                feeds[s].append(i)
        ready = [i for i in range(len(left)) if not waiting[i]]   # in order: already a heap
        finished = [False] * len(left)
        done: List[Obj] = []
        first = 0
        while len(done) < len(left):
            # the leftmost node whose inputs are all done; when none is (a loop of wires), the leftmost left
            while ready and finished[ready[0]]:
                heapq.heappop(ready)
            if ready:
                i = heapq.heappop(ready)
            else:
                while finished[first]:
                    first += 1
                i = first
            finished[i] = True
            done.append(left[i])
            for j in feeds[i]:
                waiting[j] -= 1
                if not waiting[j] and not finished[j]:
                    heapq.heappush(ready, j)
        return done

    def diagram(self, d: Optional[Diagram], level: int = 0) -> List[Dict[str, Any]]:
        if d is None:
            return []
        if level > MAX_DEPTH:
            self.gap("structures nested more than %d deep" % MAX_DEPTH)
            return []
        ordered = self.order(d)
        groups = self._parallel(d, ordered)
        if groups:
            return [{"node": "parallel", "branches": [self._steps(g, level) for g in groups]}]
        return self._steps(ordered, level)

    def _steps(self, nodes: List[Obj], level: int) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for n in nodes:
            out += self.step(n, level)
        return _queries(out)

    def _parallel(self, d: Diagram, ordered: List[Obj]) -> List[List[Obj]]:
        """Loops that run at the same time: two or more groups of nodes, each with a loop, no wire between them."""
        loops = [n for n in ordered if n.kind in ("forLoop", "whileLoop", "timeLoop")]
        if len(loops) < 2:
            return []
        group: Dict[int, int] = {id(n): i for i, n in enumerate(ordered)}

        def top(i: int) -> int:
            while group_of[i] != i:
                group_of[i] = group_of[group_of[i]]
                i = group_of[i]
            return i
        group_of = list(range(len(ordered)))
        for n in ordered:
            for t in self.g.inputs(n):
                src_node = self.g.term_node.get(self.g.source[t])
                if src_node is not None and id(src_node) in group:
                    a, b = top(group[id(n)]), top(group[id(src_node)])
                    group_of[a] = b
        sets: Dict[int, List[Obj]] = {}
        for n in ordered:
            sets.setdefault(top(group[id(n)]), []).append(n)
        is_loop = {id(n) for n in loops}
        with_loops = [k for k, g in sets.items() if any(id(n) in is_loop for n in g)]
        if len(with_loops) < 2:
            return []
        looped = set(with_loops)
        rest = [n for k, g in sets.items() if k not in looped for n in g]
        groups = [list(sets[k]) for k in with_loops]
        groups[0] = sorted(groups[0] + rest, key=lambda n: group[id(n)])
        return groups

    def gap(self, words: str) -> None:
        if words not in self.gaps:
            self.gaps.append(words)

    def step(self, n: Obj, level: int) -> List[Dict[str, Any]]:
        """The summary's nodes for one of the diagram's (none for one that only works a value out)."""
        self.shared.spend()
        k = n.kind
        if k in ("forLoop", "whileLoop", "timeLoop"):
            return [self.loop(n, level)]
        if k == "select":
            return [self.case(n, level)]
        if k == "sequence":
            return [{"node": "sequence", "frames": [self.diagram(s, level + 1) for s in self.g.subdiagrams(n)]}]
        if k in ("sequenceFrame", "timeFlatSequenceFrame"):   # a flat sequence's frame: its nodes, in their turn
            subs = self.g.subdiagrams(n)
            return self.diagram(subs[0], level + 1) if subs else []
        if k == "eventStruct":
            return [self.event(n, level)]
        if k == "prim":
            return self.prim(n)
        if k in CALLS:
            return self.call(n, level)
        if k == "extFunc":
            path = vifile._path(n.raw("libPath") or b"", 0)[0] or []
            library = vifile.path_text(path) if path else ""
            function = vifile._text(n.raw("nodeName") or b"") or vifile.label_of(n)
            return [{"node": "call_library", "library": library, "function": function}]
        if k == "propNode":
            names = []
            for o in n.walk():
                if o.tag == vifile.TAG["PropItemName"] and isinstance(o.value, (bytes, bytearray)):
                    names.append(vifile._text(bytes(o.value)))
            of = vifile._text(n.raw("nodeName") or b"") or "an object"
            return [{"node": "property", "of": of, "property": ", ".join(names) or "a property"}]
        if k == "invokeNode":
            of = vifile._text(n.raw("nodeName") or b"") or "an object"
            return [{"node": "other", "what": "an invoke node (a method of %s)" % of}]
        if k == "callByRefNode":
            return [{"node": "other", "what": "a VI called by reference"}]
        if k in ("scriptNode", "mathScriptNode"):
            return [{"node": "other", "what": "a script node"}]
        if k in ("sharedVariable", "sharedVariableDynamicRead", "sharedVariableDynamicWrite"):
            return [{"node": "other", "what": "a shared variable"}]
        if k in ("timeSequence", "timeFlatSequence"):
            return [{"node": "other", "what": "a timed sequence"}]
        return []

    def prim(self, n: Obj) -> List[Dict[str, Any]]:
        rid = n.number("primResID") or 0
        parms = self.g.by_parm(n)
        if rid in (VISA_WRITE, TCP_WRITE):
            text, values = render(self.text(parms.get(10) if rid == VISA_WRITE else self._data_input(n)))
            if text and not values:
                self.strings.setdefault(text)
            resource = self.resource(parms.get(11)) if rid == VISA_WRITE else {"computed": "a TCP connection"}
            return [{"node": "write", "resource": resource, "text": text, "values": values}]
        if rid in (VISA_READ, TCP_READ):
            resource = self.resource(parms.get(11)) if rid == VISA_READ else {"computed": "a TCP connection"}
            return [{"node": "read", "resource": resource, "into": self.reading(n, parms.get(2))}]
        if rid == WAIT_MS:
            return [{"node": "wait", "ms": self.origin(parms.get(1), set()).source()}]
        if rid == TDMS_WRITE:
            names = self.origin(parms.get(6), set())
            data = self.origin(parms.get(9), set())
            columns = []
            if names.kind == "value":
                cols = names.value if isinstance(names.value, list) else [names.value]
                columns = [{"name": str(c), "from": data.source()} for c in cols]
            return [{"node": "file", "kind": "tdms", "columns": columns or [{"name": "data", "from": data.source()}]}]
        if rid == TEXT_WRITE:
            data = self.origin(parms.get(10), set())
            return [{"node": "file", "kind": "text", "columns": [{"name": "text", "from": data.source()}]}]
        return []

    def _data_input(self, n: Obj) -> Optional[int]:
        return next((t for t in self.g.inputs(n) if self._type(t) == "string"), None)

    def text(self, term: Optional[int]) -> Val:
        v = self.origin(term, set())
        return v if v.kind == "text" else Val("text", parts=[v])

    def loop(self, n: Obj, level: int) -> Dict[str, Any]:
        kind = "for" if n.kind == "forLoop" else "while"
        var = self.loop_vars.get(n.uid, "i")
        subs = self.g.subdiagrams(n)
        rng: Dict[str, Any] = {}
        until = ""
        stop_if: Optional[Dict[str, Any]] = None
        values = self.loop_values.get(n.uid)
        if values is not None:   # each value of an array: a ramp's range, else the array
            rng = self.ramp(values) or {"values": self.origin(values, set()).source()}
        if kind == "for":
            limit = self.g.resolve(n.get("loopLimitDCO"))
            count = self.g.ends(limit)[-1] if limit is not None and self.g.ends(limit) else None
            if values is None and count is not None and count in self.g.source:
                rng = {"count": self.origin(count, set()).source()}
        test = self.g.inner_term(self.g.resolve(n.get("loopTestDCO")))
        if kind == "while" or test is not None:   # a For loop's conditional terminal, too
            cond = self.origin(test, set()) if test is not None else computed("nothing")
            until = self.condition(cond)
            stop_if = self.stop_if(n, test, cond)
        elif _set(n.get("fLoopCondTerm")):
            until = "a condition Kelvin couldn't read"
        parallel = n.get("ParForWorkers") is not None or any(
            o.kind == "parForWorkers" for o in n.kids)
        out = {"node": "loop", "kind": kind, "var": var, "range": rng, "until": until, "parallel": parallel,
               "body": self.diagram(subs[0], level + 1) if subs else []}
        if stop_if is not None:
            out["stop_if"] = stop_if
        if n.kind == "timeLoop":
            out["timed"] = True
        machine = self.state_machine(n, subs[0] if subs else None, level)
        return machine or out

    def stop_if(self, loop: Obj, test: Optional[int], cond: Val) -> Optional[Dict[str, Any]]:
        """What ends a loop, where the diagram says it exactly: a Boolean control (a button a person presses),
        {"button": name}; or its count reaching its number of values less one (i = N - 1, or i + 1 = N), {"last":
        true}: it stops after its last value (a While loop goes on past the end of an array it takes value by value
        unless told to stop). None for anything else (until says it in words)."""
        if cond.kind == "control" and self.args is None:
            c = next((c for c in self.vi.controls if c.name == cond.value), None)
            return {"button": c.name} if c is not None and c.kind == "bool" and not c.indicator else None
        node = self.g.term_node.get(self._back(test)) if test is not None else None
        if node is None or node.kind != "prim" or (node.number("primResID") or 0) != EQUAL:
            return None
        sides = [self._back(t) for t in self.g.inputs(node)]   # back through the tunnels they come in by
        if len(sides) != 2 or None in sides:
            return None
        for a, b in (sides, sides[::-1]):
            if self._is_count(a, loop) and self._less_one(b, loop) or \
                    self._plus_one(a, loop) and self._number_of_values(b, loop):
                return {"last": True}
        return None

    def _is_count(self, src: Optional[int], loop: Obj) -> bool:
        """Whether a terminal is the loop's own count (i)."""
        dco = self.g.term_dco.get(src) if src is not None else None
        return dco is not None and dco.kind == "lCnt" and self._loop_of(dco) is loop

    def _arith_of(self, src: Optional[int], rid: int) -> Optional[Dict[int, int]]:
        """The terminals (by number) of the function a terminal is the output of, when it is that function."""
        node = self.g.term_node.get(src) if src is not None else None
        if node is None or node.kind != "prim" or (node.number("primResID") or 0) != rid:
            return None
        return self.g.by_parm(node)

    def _one(self, term: Optional[int]) -> bool:
        v = self.origin(term, set()) if term is not None else None
        return v is not None and v.kind == "value" and v.value == 1 and not isinstance(v.value, bool)

    def _less_one(self, src: int, loop: Obj) -> bool:
        """N - 1, N the loop's number of values."""
        parms = self._arith_of(src, SUBTRACT)
        return parms is not None and self._one(parms.get(1)) and \
            self._number_of_values(self._back(parms.get(2)), loop)

    def _plus_one(self, src: int, loop: Obj) -> bool:
        """i + 1 (Increment, or Add 1), i the loop's count."""
        parms = self._arith_of(src, INCREMENT)
        if parms is not None:
            return any(self._is_count(self._back(t), loop) for t in self.g.inputs(self.g.term_node[src]))
        parms = self._arith_of(src, ADD)
        return parms is not None and any(self._is_count(self._back(parms.get(a)), loop) and self._one(parms.get(b))
                                         for a, b in ((2, 1), (1, 2)))

    def _number_of_values(self, src: Optional[int], loop: Obj) -> bool:
        """Whether a terminal is how many values the loop takes: Array Size of its array, or the number of samples
        LabVIEW's Ramp Pattern (by Delta) gives beside the ramp it takes."""
        values = self._back(self.loop_values.get(loop.uid)) if loop.uid in self.loop_values else None
        if src is None or values is None:
            return False
        size = self._arith_of(src, ARRAY_SIZE)
        if size is not None:
            ins = self.g.inputs(self.g.term_node[src])
            return len(ins) == 1 and self._back(ins[0]) == values
        node = self.g.term_node.get(src)
        if node is None or node is not self.g.term_node.get(values) or node.kind not in CALLS or src == values:
            return False
        name, _ = self._call_name(node)
        return "ramp pattern" in name.lower() and "delta" in (self._menu(node) or name).lower() and \
            self._type(src) == "number"

    def _back(self, term: Optional[int]) -> Optional[int]:
        """The terminal that feeds an input, back through the tunnels it comes in by (never through a node)."""
        src = self.g.source.get(term) if term is not None else None
        for _ in range(MAX_DEPTH):
            dco = self.g.term_dco.get(src) if src is not None else None
            if dco is None or dco.kind not in TUNNELS or dco.kind in ("lSR", "rSR", "matedLSR", "matedRSR"):
                return src
            ends = self.g.ends(dco)
            if not ends or src == ends[-1]:
                return src
            src = self.g.source.get(ends[-1])
        return src

    def ramp(self, term: int) -> Optional[Dict[str, Any]]:
        """The range of an array a loop takes value by value, when LabVIEW's Ramp Pattern made it: by Delta
        {from, to, step}, by Samples {from, to, points} (its inputs in the order of its terminals: start, end, then
        delta; samples is the whole number). None for any other array, or a ramp whose inputs aren't all wired."""
        node, level = None, 0
        src = self.g.source.get(term)
        while src is not None and level < MAX_DEPTH:   # back through the tunnels it comes in by
            dco = self.g.term_dco.get(src)
            if dco is not None and dco.kind in TUNNELS and dco.kind not in ("lSR", "rSR"):
                ends = self.g.ends(dco)
                if not ends or src == ends[-1]:
                    return None
                src, level = self.g.source.get(ends[-1]), level + 1
                continue
            node = self.g.term_node.get(src)
            break
        if node is None or node.kind not in CALLS:
            return None
        name, _ = self._call_name(node)
        if "ramp pattern" not in name.lower():
            return None
        instance = (self._menu(node) or name).lower()
        ins = [t for t in self.g.terms(node) if t in self.g.source and not self._is_error(t)]
        if any(self._type(t) == "bool" and self.origin(t, set()).value is not False for t in ins):
            return None   # exclude end? set, or worked out: no plain range
        numbers = [t for t in ins if self._type(t) == "number"]
        whole = [t for t in numbers if self._numeric(t)[:1] in ("I", "U")]
        real = [t for t in numbers if t not in whole]
        if "delta" in instance and len(real) == 3 and not whole:
            start, end, step = (self.origin(t, set()).source() for t in real)
            return {"from": start, "to": end, "step": step}
        if "delta" not in instance and len(real) == 2 and len(whole) == 1:
            start, end = (self.origin(t, set()).source() for t in real)
            return {"from": start, "to": end, "points": self.origin(whole[0], set()).source()}
        return None

    def _numeric(self, term: int) -> str:
        dco = self.g.term_dco.get(term)
        p = vifile.plain(self.vi.types.of_heap(dco.number("typeDesc"))) if dco is not None else None
        return p.numeric if p is not None else ""

    def _menu(self, node: Obj) -> str:
        """The instance a polymorphic VI's call shows in its selector ("Ramp by Delta")."""
        sel = node.get("instanceSelector")
        if sel is None:
            return ""
        buf = next((o.value for o in sel.walk() if o.tag == vifile.TAG["buf"] and isinstance(o.value, bytes)), b"")
        names, at = [], 0
        while at < len(buf):
            try:
                name, at = vifile._pascal(buf, at)
            except vifile.ReadError:
                break
            names.append(name)
        used = node.number("menuInstanceUsed") or 0
        if used <= 0:   # Automatic: the instance it was given (after Automatic and the separator)
            used = 2 + (node.number("preferredInstIndex") or 0)
        return names[used] if 0 <= used < len(names) else ""

    def condition(self, cond: Val) -> str:
        if cond.kind == "control":
            return "the %s button" % cond.value
        if cond.kind == "value":
            return "at once (a constant %s)" % _plain(cond.value)
        return cond.words() if cond.kind != "computed" else "a condition worked out on the diagram (%s)" % cond.value

    def state_machine(self, loop: Obj, body: Optional[Diagram], level: int = 0) -> Optional[Dict[str, Any]]:
        """A while loop whose case structure is chosen by a value the loop keeps (a shift register): a state
        machine, which a scan has no place for."""
        if body is None or loop.kind != "whileLoop":
            return None
        for n in body.nodes:
            if n.kind != "select":
                continue
            sel = self.g.resolve(n.get("caseSelDCO"))
            ends = self.g.ends(sel) if sel is not None else []
            if not ends:
                continue
            v = self.origin(ends[-1], set())
            if v.kind == "computed" and ("shift register" in str(v.value) or "loop keeps" in str(v.value)):
                case = self.case(n, level + 1)
                return {"node": "state_machine", "states": [", ".join(names) for names in self.case_names(n)],
                        "cases": case["cases"]}
        return None

    def case_names(self, n: Obj) -> List[List[str]]:
        """Each case's values as LabVIEW shows them (True, 2, Idle, Default), in the order of its diagrams."""
        subs = self.g.subdiagrams(n)
        sel = self.g.resolve(n.get("caseSelDCO"))
        t = vifile.plain(self.vi.types.of_heap(sel.number("typeDesc"))) if sel is not None else None
        strings = [vifile._text(bytes(x.value)) if isinstance(x.value, (bytes, bytearray)) else ""
                   for x in n.items("SelectStringArray")]   # an empty string is kept with no content
        names: Dict[int, List[str]] = {}
        for r in n.items("SelectRangeArray32") + n.items("SelectRangeArray64"):
            f = {x.tag: int.from_bytes(x.value, "big", signed=True) for x in r.kids
                 if isinstance(x.value, (bytes, bytearray)) and x.value}
            start, end, index = f.get(0, 0), f.get(1, 0), f.get(4, 0)
            if t is not None and t.kind in ("bool", "cluster"):   # a cluster: an error's (No Error, Error)
                words = [("True" if start else "False") if t.kind == "bool" else ("Error" if start else "No Error")]
            elif strings and t is not None and t.kind == "string" and 0 <= start <= end < len(strings):
                words = strings[start:end + 1]
            elif t is not None and t.kind == "enum" and t.names and 0 <= start <= end < len(t.names):
                words = t.names[start:end + 1]
            else:
                words = [str(start) if start == end else "%d..%d" % (start, end)]
            names.setdefault(index, []).extend(words)
        default = n.number("SelectDefaultCase")
        if default is not None and 0 <= default < len(subs):
            names.setdefault(default, []).append("Default")
        if not names and len(subs) == 2 and t is not None and t.kind in ("bool", "cluster"):
            # LabVIEW before 2010 keeps no values: a Boolean's (an error's) cases are False then True
            return [["False"], ["True"]] if t.kind == "bool" else [["No Error"], ["Error"]]
        return [names.get(i) or ["case %d" % (i + 1)] for i in range(len(subs))]

    def case(self, n: Obj, level: int) -> Dict[str, Any]:
        """A case structure: its selector, and each case's nodes under each of its values (a case for "Idle" and
        Default is under both)."""
        sel = self.g.resolve(n.get("caseSelDCO"))
        ends = self.g.ends(sel) if sel is not None else []
        selector = self.origin(ends[-1], set()) if ends else computed("nothing")
        cases: Dict[str, List[Dict[str, Any]]] = {}
        for names, d in zip(self.case_names(n), self.g.subdiagrams(n)):
            body = self.diagram(d, level + 1)
            for name in names:
                key, k = name, 2
                while key in cases:
                    key, k = "%s (%d)" % (name, k), k + 1
                cases[key] = body
        return {"node": "case", "selector": selector.source(), "cases": cases}

    def event(self, n: Obj, level: int) -> Dict[str, Any]:
        """An event structure: which events it waits for (a control's, or a timeout); a scan has no place for it."""
        events = []
        for spec in n.items("EventNodeEvents"):
            f = {x.tag: int.from_bytes(x.value, "big", signed=True) for x in spec.kids
                 if isinstance(x.value, (bytes, bytearray)) and x.value}
            c = self.by_ddo.get(f.get(6))
            events.append("an event of %s" % c.name if c is not None else "an event")
        if n.get("timeOutDCO") is not None:
            events.append("a timeout")
        return {"node": "event", "events": events, "cases": [self.diagram(d, level + 1)
                                                             for d in self.g.subdiagrams(n)]}

    # -------------------------------------------------- sub-VIs

    def _linked(self, name: str) -> Optional[vifile.Link]:
        return self.links.get(name.rpartition(":")[2].lower())

    def _call_name(self, n: Obj) -> Tuple[str, Optional[vifile.Link]]:
        label = vifile.label_of(n)
        link = self._linked(label) if label else None
        return (link.name if link is not None else label), link

    def call(self, n: Obj, level: int) -> List[Dict[str, Any]]:
        name, link = self._call_name(n)
        low = name.lower()
        if any(q in low for q in QUIET_VIS) or any(q in low for q in CONSTANT_VIS):
            return []
        if "time delay" in low:
            ins = [t for t in self.g.inputs(n) if self._type(t) == "number"]
            delay = self.origin(ins[0], set()).source() if ins else {"computed": "set in the Time Delay's dialog"}
            return [{"node": "wait", "s": delay}]
        for key, kind in FILE_VIS:
            if key in low:
                return [{"node": "file", "kind": kind, "columns": self.columns(n)}]
        if low.startswith("daqmx") or "daq assistant" in low:
            return self.daq(n, low)
        if link is None or link.where in ("vi.lib", "LabVIEW"):
            return []   # one of LabVIEW's own VIs, or an Express VI: it works a value out (named where it's used)
        sub, link = self.instance(n, self.subvi(link), link)
        name = link.name
        out: Dict[str, Any] = {"node": "call", "vi": name or "a sub-VI", "resource": None, "inputs": {},
                               "sends": [], "into": "", "where": link.where}
        names = self._terminals(sub)
        for i, t in enumerate(self.g.terms(n)):
            if t not in self.g.source or self._is_error(t):
                continue
            if self._type(t) == "visa":
                out["resource"] = self.resource(t)
                continue
            c = names.get(i)
            out["inputs"][c.name if c is not None else "terminal %d" % i] = self.origin(t, set()).source()
        data_out = [t for t in self.g.terms(n) if t in self.g.sinks and self._type(t) != "visa"
                    and not self._is_error(t)]
        if data_out and out["resource"] is not None:
            out["into"] = self.reading(n, data_out[0])
        if out["resource"] is None:
            out.pop("resource")
        if sub is not None:
            sends, missing = self.sends(sub, n, names)
            out["sends"] = sends
            if missing:
                out["sends_unread"] = missing
        else:
            out["sends_unread"] = "%s isn't beside the VI, so the commands it sends weren't read" % name
        return [out]

    def _terminals(self, sub: Optional[vifile.VI]) -> Dict[int, vifile.Control]:
        """A sub-VI's controls and indicators by their place on its connector pane (a call's terminals, in order)."""
        return {c.terminal: c for c in (sub.controls if sub is not None else []) if c.terminal is not None}

    def instance(self, n: Obj, sub: Optional[vifile.VI], link: vifile.Link) -> Tuple[Optional[vifile.VI],
                                                                                         vifile.Link]:
        """The instance of a polymorphic VI a call uses: the polymorphic VI lists its instances in order, the call
        keeps which one (preferredInstIndex)."""
        if n.kind != "polyIUse" or sub is None:
            return sub, link
        instances = [ln for ln in sub.links if ln.ident == "VIVI" and ln.kind == "vi"]
        i = n.number("preferredInstIndex") or 0
        menu = sub.instances[i].lower() if 0 <= i < len(sub.instances) else ""
        found = [ln for ln in instances if menu and "(%s)" % menu in ln.name.lower()] or \
                [ln for ln in instances if menu and menu in ln.name.lower()]
        if not found and len(sub.instances) == len(instances) and 0 <= i < len(instances):
            found = [instances[i]]
        if not found:
            return sub, link
        chosen = self._linked(found[0].name) or found[0]
        return self._open(sub.path, found[0]), chosen

    def subvi(self, link: vifile.Link) -> Optional[vifile.VI]:
        """A sub-VI's own file, read when it is where the link says (a path relative to the VI's file), for its
        connector pane and the commands it sends. Not LabVIEW's own (vi.lib), and only so deep."""
        if self.depth >= MAX_SUBVI_DEPTH or link.where == "vi.lib" or not self.vi.path or link.kind != "vi":
            return None
        return self._open(self.vi.path, link)

    def _open(self, beside: str, link: vifile.Link) -> Optional[vifile.VI]:
        """A sub-VI's file, where its link says it is, read once: only in the summary's folder (vifile.within: no
        other computer's, no absolute place, nothing outside it), never in Kelvin's own folders or a file of
        sign-ins."""
        from .folderfiles import mine, secret
        vi_file = Path(beside)
        parts = list(link.path)
        if not parts or not self.shared.folder:
            return None
        if parts[0].startswith("<"):   # in one of LabVIEW's folders: the copy of it beside the VI, if any
            folder = parts[0].strip("<>").replace("lib", ".lib")
            candidates = [(vi_file.parent, parts[1:]), (vi_file.parent, [folder] + parts[1:])]
        else:   # relative to the VI's file: each empty part is up one (the first, to the VI's folder)
            candidates = [(vi_file, [".." if p == "" else p for p in parts]), (vi_file.parent, parts[-1:])]
        for under, names in candidates:
            c = self.shared.within(under, names)
            if c is None:
                continue
            key = str(c)
            if key in self.files:
                return self.files[key]
            try:
                exists = c.is_file() and not secret(c) and mine(c, self.shared.own) is None
            except OSError:
                exists = False
            if not exists:
                continue
            if len(self.files) >= MAX_SUBVIS:
                return None
            self.shared.spend()
            try:
                self.files[key] = vifile.read(c)
            except (vifile.ReadError, OSError):
                self.files[key] = None
            return self.files[key]
        return None

    def sends(self, sub: vifile.VI, n: Obj, names: Dict[int, vifile.Control]) -> Tuple[List[Dict[str, Any]], str]:
        """The commands a sub-VI sends whatever its inputs (its writes and queries outside loops and cases), read
        from the sub-VI as this call fills it in: an input wired at the call is the caller's value, any other its
        saved one, so the text is the one the instrument gets (a constant's line picked, a number formatted)."""
        args: Dict[str, Val] = {}
        for i, t in enumerate(self.g.terms(n)):
            c = names.get(i)
            if c is not None and not c.indicator and t in self.g.source:
                args[c.name] = self.origin(t, set())
        if sub.diagram is None:
            return [], "%s's block diagram wasn't read (%s)" % (sub.name, "; ".join(sub.unread) or "not in the file")
        inner = _Reader(sub, self.link, self.depth + 1, self.files, args, self.shared)
        with vifile.text_encoding(sub.encoding):
            inner.name_loops(inner.g.root)
            steps = inner.diagram(inner.g.root)
        out: List[Dict[str, Any]] = []
        later: List[str] = []

        def walk(nodes: List[Dict[str, Any]], inside: str) -> None:
            for x in nodes:
                kind = x.get("node")
                if kind in ("write", "query"):
                    if inside:
                        later.append(inside)
                        continue
                    one = {"text": x.get("text", ""), "values": dict(x.get("values") or {})}
                    if kind == "query":
                        one["query"] = True
                    out.append(one)
                elif kind == "sequence":
                    for frame in x.get("frames") or []:
                        walk(frame, inside)
                elif kind == "call":
                    if inside and x.get("sends"):
                        later.append(inside)
                    elif not inside:
                        out.extend(dict(s) for s in x.get("sends") or [])
                elif kind == "loop":
                    walk(x.get("body") or [], "in a loop")
                elif kind == "case":
                    for body in (x.get("cases") or {}).values():
                        walk(body, "in a case structure")
                elif kind == "parallel":
                    for b in x.get("branches") or []:
                        walk(b, inside)
        walk(steps, "")
        missing = ""
        if later:
            missing = "%s also sends commands %s, which depend on its inputs" % (sub.name, sorted(set(later))[0])
        return out, missing

    def columns(self, n: Obj) -> List[Dict[str, Any]]:
        """What a file-writing sub-VI writes: each value built into the array it takes (followed out of the loops
        that gather it, a row per step), else the array itself."""
        data = [t for t in self.g.inputs(n) if self._type(t) == "array"]
        if not data:
            data = [t for t in self.g.inputs(n) if self._type(t) in ("string", "number")]
        if not data:
            return []
        built = self._built(self.g.source[data[0]], 0)
        if built:
            return [{"name": _column_name(v, i), "from": v.source()} for i, v in enumerate(built)]
        v = self.produced(self.g.source[data[0]], set(), 0)
        return [{"name": _column_name(v, 0), "from": v.source()}]

    def _built(self, src: int, level: int) -> List[Val]:
        """The values an array is built of (Build Array), through the tunnels it leaves loops by."""
        if level > 8:
            return []
        self.shared.spend()
        node, dco = self.g.term_node.get(src), self.g.term_dco.get(src)
        if node is not None and node.kind == "aBuild":
            return [self.origin(t, set()) for t in self.g.inputs(node)]
        if dco is not None and dco.kind in TUNNELS:
            ends = self.g.ends(dco)
            inner = ends[:-1] if ends and src == ends[-1] else ends[-1:]
            for e in inner:
                if e in self.g.source:
                    got = self._built(self.g.source[e], level + 1)
                    if got:
                        return got
        return []

    def daq(self, n: Obj, low: str) -> List[Dict[str, Any]]:
        """An NI-DAQmx write or read: its channel (followed back to the channel it was made with) and its value."""
        if not any(w in low for w in ("daqmx write", "daqmx read", "daq assistant")):
            return []
        channel = ""
        for t in self.g.inputs(n):
            got = self._channel(t, 0)
            if got:
                channel = got
                break
        if "daq assistant" in low:
            return [{"node": "other", "what": "a DAQ Assistant (its channels are set in its dialog)"}]
        if "write" in low:
            data = [t for t in self.g.inputs(n) if self._type(t) in ("number", "array", "bool")]
            value = self.origin(data[0], set()).source() if data else {"computed": "nothing wired"}
            return [{"node": "daq", "op": "write", "channel": channel, "value": value, "into": ""}]
        return [{"node": "daq", "op": "read", "channel": channel, "into": self.reading(n)}]

    def _channel(self, term: int, level: int) -> str:
        if level > MAX_DEPTH:
            return ""
        self.shared.spend()
        src = self.g.source.get(term)
        dco = self.g.term_dco.get(src) if src is not None else None
        node = self.g.term_node.get(src) if src is not None else None
        if dco is None:
            return ""
        if dco.kind == "bDConstDCO":
            v = self.constant(dco)
            return v.value if v.kind == "value" and isinstance(v.value, str) and "/" in v.value else ""
        if dco.kind == "fPDCO" and dco.uid in self.controls:
            c = self.controls[dco.uid]
            return c.default if isinstance(c.default, str) and "/" in c.default else ""
        if dco.kind in TUNNELS:
            ends = self.g.ends(dco)
            if ends and src != ends[-1]:
                return self._channel(ends[-1], level + 1)
            return next((x for x in (self._channel(e, level + 1) for e in ends[:-1] if e in self.g.source) if x), "")
        if node is not None and node.kind in CALLS:
            for t in self.g.inputs(node):
                got = self._channel(t, level + 1)
                if got:
                    return got
        return ""

    # -------------------------------------------------- the summary

    def summary(self) -> Dict[str, Any]:
        vi = self.vi
        out: Dict[str, Any] = {"summary": SUMMARY, "name": vi.name, "path": vi.path, "labview": vi.labview,
                               "read": "names", "linkage": [], "controls": [], "visa": [], "scpi": [],
                               "diagram": [], "unread": list(vi.unread)}
        for ln in vi.links:
            if ln.kind in ("vi", "control", "library", "class", "dll", "xcontrol"):
                entry = self.link(ln.name, ln.where)
                if ln.kind == "library" and not entry.get("library"):   # the library itself (a driver's .lvlib)
                    entry = dict(entry, library=ln.name, vi="")
                out["linkage"].append(entry)
        if vi.panel is not None:
            out["read"] = "panel"
            out["controls"] = [_control(c) for c in vi.controls]
            out["unread"] += ["the saved value of %s isn't a finite number (Inf or NaN): left out" % c.name
                              for c in vi.controls if c.has_default and not all_finite(c.default)]
            for c in vi.controls:
                if c.kind == "visa" and not c.indicator and c.has_default and isinstance(c.default, str) and c.default:
                    self._visa(c.default, c.name)
        if vi.instances:   # a polymorphic VI: its diagram only lists its instances, each a VI of its own
            out["unread"].append("it is a polymorphic VI: where it is called, one of its instances runs (%s), each "
                                 "a VI read on its own" % ", ".join(vi.instances[:12]))
        elif self.g.root is not None:
            out["read"] = "full" if vi.panel is not None else "names"
            self.name_loops(self.g.root)
            out["diagram"] = self.diagram(self.g.root)
            out["unread"] += self.gaps
        elif vi.diagram is not None:
            out["unread"].append("its block diagram: its layout wasn't one Kelvin reads")
        out["visa"] = self.visa
        texts = []
        for n in walk(out["diagram"]):
            if n.get("node") in ("write", "query") and n.get("text"):
                texts.append(n["text"])
            for s in n.get("sends") or [] if n.get("node") == "call" else []:
                if s.get("text"):
                    texts.append(s["text"])
        texts += [s for s in self.strings if looks_scpi(s) and s not in self.formats]
        out["scpi"] = list(dict.fromkeys(t.strip() for t in texts if t.strip()))[:200]
        if vi.description:
            out["description"] = vi.description[:2000]
        return out


def _arith(op: str, x: Val, y: Val) -> Optional[Val]:
    """x op y where one side is a constant number and the other a control's, loop's or reading's value: that value
    scaled or offset, as the summary keeps it."""
    def num(v: Val) -> Optional[float]:
        return float(v.value) if v.kind == "value" and isinstance(v.value, (int, float)) and \
            not isinstance(v.value, bool) else None
    a, b = num(x), num(y)
    if a is not None and b is not None:
        try:
            got = {"+": a + b, "-": a - b, "*": a * b, "/": a / b if b else float("nan")}[op]
        except (ZeroDivisionError, OverflowError):
            return None
        return Val("value", got) if math.isfinite(got) else None   # Inf or NaN: said in words, as computed
    var, c = (x, b) if b is not None else (y, a) if a is not None else (None, None)
    if var is None or c is None or var.kind not in ("control", "loop", "read"):
        return None
    scale = var.scale if var.scale is not None else 1.0
    offset = var.offset if var.offset is not None else 0.0
    if op == "*":
        scale, offset = scale * c, offset * c
    elif op == "+":
        offset += c
    elif op == "-":
        if var is x:
            offset -= c
        else:   # c - x
            scale, offset = -scale, c - offset
    elif op == "/" and var is x and c:
        scale, offset = scale / c, offset / c
    else:
        return None
    if not (math.isfinite(scale) and math.isfinite(offset)):
        return None
    return Val(var.kind, var.value, scale=scale, offset=offset)


def _column_name(v: Val, i: int) -> str:
    if v.kind in ("control", "loop", "read"):
        return str(v.value)
    return "column %d" % (i + 1)


def _queries(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A write followed by a read of the same resource (nothing sent between) is one query."""
    out: List[Dict[str, Any]] = []
    for n in nodes:
        prev = out[-1] if out else None
        if n.get("node") == "read" and prev is not None and prev.get("node") == "write" and \
                prev.get("resource") == n.get("resource"):
            out[-1] = {"node": "query", "resource": prev["resource"], "text": prev["text"],
                       "values": prev["values"], "into": n["into"]}
            continue
        out.append(n)
    return out


def walk(nodes: Any) -> Iterator[Dict[str, Any]]:
    """Every node of a summary's diagram, nested ones too."""
    for n in nodes if isinstance(nodes, list) else []:
        if not isinstance(n, dict):
            continue
        yield n
        for key in ("body",):
            yield from walk(n.get(key))
        for key in ("frames", "branches"):
            for part in n.get(key) or []:
                yield from walk(part)
        if isinstance(n.get("cases"), dict):
            for part in n["cases"].values():
                yield from walk(part)
        elif isinstance(n.get("cases"), list):
            for part in n["cases"]:
                yield from walk(part)


def _summarise(vi: vifile.VI, link: Link, shared: _Shared) -> Dict[str, Any]:
    """A VI's summary, never an error: a diagram that can't be followed (or is bigger than MAX_STEPS) is said, with
    the VI's linkage and controls; no number in it that isn't finite (each said)."""
    try:
        with vifile.text_encoding(vi.encoding):
            out = _Reader(vi, link, 0, shared.files, shared=shared).summary()
    except Exception as e:
        out = _not_read(vi.path, "")
        out.update(name=vi.name, labview=vi.labview, read="panel" if vi.panel is not None else "names",
                   linkage=[link(ln.name, ln.where) for ln in vi.links
                            if ln.kind in ("vi", "control", "library", "class", "dll")],
                   controls=[_control(c) for c in vi.controls],
                   unread=list(vi.unread) + [str(e) if isinstance(e, TooBig) else
                                             "its block diagram: it couldn't be followed (%s)" % type(e).__name__])
    out, n = plain_numbers(out)
    if n:
        out["unread"].append("%d value%s that aren't finite numbers (Inf or NaN): left out"
                             % (n, "" if n == 1 else "s"))
    return out


def _not_read(path: Any, why: str) -> Dict[str, Any]:
    p = Path(str(path)) if path else None
    return {"summary": SUMMARY, "name": p.name if p else "", "path": str(p) if p else "", "labview": "",
            "read": "no", "linkage": [], "controls": [], "visa": [], "scpi": [], "diagram": [],
            "unread": ["not read: %s" % why]}


def summary(path: Any, link: Optional[Link] = None, folder: Any = None, own: Any = ()) -> Dict[str, Any]:
    """A VI's summary (labview.py's layout), from its file. link(name, where): a linkage entry with the maker and
    model a driver library's name says (labview.link); without it, maker and model are left empty. folder: where
    the sub-VIs it calls may be read from (default: the VI's own folder; never outside it); own: Kelvin's own
    folders, never read. A file that can't be read gives a summary that says why ("read": "no"), never an error."""
    try:
        vi = vifile.read(path)
    except vifile.ReadError as e:
        return _not_read(path, str(e))
    except Exception as e:   # a file damaged in a way no reader foresaw: said, never raised
        return _not_read(path, "it looks damaged (%s)" % (type(e).__name__))
    return _summarise(vi, link or _link, _Shared(folder=str(folder or Path(str(path)).parent), own=tuple(own or ())))


def summaries(path: Any, link: Optional[Link] = None, folder: Any = None, own: Any = ()) -> List[Dict[str, Any]]:
    """Each VI a VI library (.llb) holds, as its summary (its path: the library's, then the VI's name)."""
    return list(each_summary(path, link, folder, own))


def each_summary(path: Any, link: Optional[Link] = None, folder: Any = None, own: Any = ()) -> Iterator[Dict[str, Any]]:
    """Each VI a VI library (.llb) holds, as its summary, one at a time (each VI's bytes let go before the next is
    unpacked). One whose name in the library isn't a plain file name (../Main.vi, a path; COM3.vi, a device) isn't
    read: an entry for the library says so, last."""
    try:
        files = vifile.members(path)
    except vifile.ReadError as e:
        yield _not_read(path, str(e))
        return
    odd: List[str] = []
    try:
        for name, data in files:
            try:
                if not name.lower().endswith((".vi", ".vit", ".ctl", ".ctt")):
                    continue
                if not vifile.plain_name(name):
                    odd.append(name)
                    continue
                where = "%s/%s" % (path, name)
                if not data:
                    yield _not_read(where, "kept compressed in a way Kelvin doesn't read")
                    continue
                try:
                    vi = vifile.read_bytes(data, where, name)
                    data = None
                    s = _summarise(vi, link or _link, _Shared(folder=str(folder or Path(str(path)).parent),
                                                              own=tuple(own or ())))
                except Exception as e:
                    s = _not_read(where, str(e) if isinstance(e, vifile.ReadError) else type(e).__name__)
                yield s
            finally:
                data = None
    except vifile.ReadError as e:   # past what Kelvin unpacks from one library
        yield _not_read(path, str(e))
    if odd:
        yield _not_read(path, "%d VI%s in it whose names aren't plain file names (%s)" % (
            len(odd), "" if len(odd) == 1 else "s", ", ".join(repr(n)[:60] for n in odd[:5])))
