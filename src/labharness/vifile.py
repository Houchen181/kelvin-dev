"""A LabVIEW VI read from its file, so a lab can switch to Kelvin directly: only read as bytes, never run, never opened
by LabVIEW (it need not be installed). A VI (.vi, .vit), a control (.ctl, .ctt) and a VI library (.llb, which holds
VIs) are resource files: a header, then blocks of data, each named by four letters. What Kelvin reads of them:

    vers, LVSR      the LabVIEW version it was saved in, and whether it was saved without its block diagram
    BDPW            whether its block diagram is locked with a password: a locked diagram is never read
    STRG            its description
    LIvi, LIfp,     its linkage: the sub-VIs, libraries, type definitions and DLLs it calls, by their names
    LIbd, LIds      ("Keithley 2400 Series.lvlib:Configure Source.vi") and where they are (vi.lib, instr.lib, user.lib,
                    or the lab's own)
    VCTP, DTHP      its types, and the type of each object on its panel and diagram
    FPHb            its front panel (zlib-compressed): the controls and indicators, their types, the values it is
                    saved with, and its connector pane
    BDHb            its block diagram (zlib-compressed): the structures (loops, cases, sequences, events), the
                    functions and sub-VIs in them, the constants, and the wires between them

read() gives all of that as plain data; summary() (vidiagram.py) makes the VI summary the LabVIEW survey and the
translation into a scan take. A block that isn't what it should be is said in "unread", never raised: no file stops a
survey. The format's numbers (block names, the heap's tags and classes) are LabVIEW's own; pylabview (MIT licence)
documents most of them.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

MAX_BYTES = 128 * 2 ** 20       # a VI or VI library bigger than this isn't read
MAX_UNPACKED = 64 * 2 ** 20     # a front panel or block diagram, unpacked
MAX_OBJECTS = 2_000_000         # objects in one heap
MAX_VALUES = 100_000            # elements of one array value read
MAX_LLB_MEMBERS = 2000
SUFFIXES = (".vi", ".vit", ".ctl", ".ctt", ".llb")
KINDS = {b"LVIN": "vi", b"sVIN": "template", b"LVCC": "control", b"sVCC": "control template", b"LVAR": "library",
         b"LMNU": "palette", b"LIBP": "packed library", b"LVXC": "xcontrol", b"LVSB": "subroutine"}
_NO_PASSWORD = hashlib.md5(b"").digest()
_HAS_NO_DIAGRAM = 1 << 9        # LVSR's flags: saved without its block diagram
_LOCKED = 1 << 13               # LVSR's flags: locked (with a password)


class ReadError(ValueError):
    """A file that isn't a LabVIEW resource file, or is damaged: the message says which, in plain words."""


# ------------------------------------------------------------------ the resource file

@dataclass
class Section:
    index: int
    name: str
    data: bytes


@dataclass
class Container:
    kind: str                                   # vi, control, library (.llb), template...
    blocks: Dict[str, List[Section]] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)

    def first(self, ident: str) -> Optional[bytes]:
        sections = self.blocks.get(ident) or []
        return sections[0].data if sections else None


def _u32(raw: bytes, at: int) -> int:
    if at < 0 or at + 4 > len(raw):
        raise ReadError("it ends early")
    return struct.unpack(">I", raw[at:at + 4])[0]


def _pascal(raw: bytes, at: int) -> Tuple[str, int]:
    """A Pascal string (a length byte, then the text) and where the next thing starts."""
    if at >= len(raw):
        raise ReadError("it ends early")
    n = raw[at]
    if at + 1 + n > len(raw):
        raise ReadError("it ends early")
    return _text(raw[at + 1:at + 1 + n]), at + 1 + n


_ENCODING: "contextvars.ContextVar[str]" = contextvars.ContextVar("labview_text", default="cp1252")
REGIONS = {0: "cp1252", 33: "gbk"}   # vers' region code: the Windows encoding the VI's text was saved in


def _text(b: bytes) -> str:
    """Text as LabVIEW saved it: UTF-8 where it is, else the encoding of the Windows it was saved on (Western, or
    Chinese where the VI says so): see text_encoding()."""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode(_ENCODING.get(), errors="replace")


@contextlib.contextmanager
def text_encoding(name: str) -> Iterator[None]:
    """Read a VI's text in its own encoding (VI.encoding) meanwhile."""
    token = _ENCODING.set(name or "cp1252")
    try:
        yield
    finally:
        _ENCODING.reset(token)


def container(raw: bytes) -> Container:
    """The blocks of a resource file. ReadError: not one, or its block list is damaged (a block whose data is
    damaged is listed in problems, the rest kept)."""
    if raw[:4] == b"RSRC" and len(raw) < 32:
        raise ReadError("it is cut short (%d bytes): only the start of a LabVIEW file" % len(raw))
    if len(raw) < 32 or raw[:6] != b"RSRC\r\n":
        raise ReadError("it isn't a LabVIEW file (no RSRC header)")
    out = Container(KINDS.get(raw[8:12], "other (%s)" % raw[8:12].decode("latin-1", "replace").strip("\x00 ")))
    info, info_size, data_at = _u32(raw, 16), _u32(raw, 20), _u32(raw, 24)
    if info + 52 > len(raw) or raw[info:info + 6] != b"RSRC\r\n":
        raise ReadError("its list of blocks is missing or damaged")
    list_at = _u32(raw, info + 44)
    base = info + list_at
    count = _u32(raw, base) + 1
    if count > 10000 or base + 4 + 12 * count > len(raw):
        raise ReadError("its list of blocks is damaged")
    heads = [struct.unpack(">4sII", raw[base + 4 + 12 * i:base + 16 + 12 * i]) for i in range(count)]
    total = sum(n + 1 for _, n, _ in heads)
    names_at = base + 4 + 12 * count + 20 * total
    end_of_info = min(len(raw), info + info_size) if info_size else len(raw)
    for ident_b, n, offset in heads:
        ident = ident_b.decode("latin-1")
        if n > 100000:
            out.problems.append("block %s: its sections are damaged" % ident)
            continue
        for k in range(n + 1):
            at = base + offset + 20 * k
            if at + 20 > len(raw):
                out.problems.append("block %s: its sections are damaged" % ident)
                break
            index, name_at, _, data_offset, _ = struct.unpack(">iIIII", raw[at:at + 20])
            start = data_at + data_offset
            try:
                size = _u32(raw, start)
            except ReadError:
                out.problems.append("block %s: its data is missing" % ident)
                continue
            body = raw[start + 4:start + 4 + size]
            if len(body) < size:
                out.problems.append("block %s: its data ends early" % ident)
            name = ""
            if name_at != 0xFFFFFFFF and names_at + name_at < end_of_info:
                try:
                    name = _pascal(raw, names_at + name_at)[0]
                except ReadError:
                    name = ""
            out.blocks.setdefault(ident, []).append(Section(index, name, body))
    return out


def unpack(data: Optional[bytes]) -> Optional[bytes]:
    """A block's data as LabVIEW packs it: its unpacked length, then zlib's stream (or the data as it is when it
    isn't packed). ReadError: packed and damaged, or bigger than Kelvin unpacks."""
    if data is None:
        return None
    if len(data) > 6 and data[4] == 0x78 and data[5] in (0x01, 0x5E, 0x9C, 0xDA):
        size = struct.unpack(">I", data[:4])[0]
        if size > MAX_UNPACKED:
            raise ReadError("a block is bigger than Kelvin unpacks (%d MB)" % (MAX_UNPACKED // 2 ** 20))
        try:
            z = zlib.decompressobj()
            out = z.decompress(data[4:], MAX_UNPACKED + 1)
        except zlib.error as e:
            raise ReadError("a packed block is damaged (%s)" % e)
        if len(out) > MAX_UNPACKED:
            raise ReadError("a block is bigger than Kelvin unpacks (%d MB)" % (MAX_UNPACKED // 2 ** 20))
        return out
    return data


# ------------------------------------------------------------------ version, flags, password, description

def version_words(code: int) -> Tuple[str, str]:
    """LabVIEW's version as its files keep it (0x18008000), as (number, words): ("18.0", "LabVIEW 2018 (18.0)")."""
    major = ((code >> 28) & 0xF) * 10 + ((code >> 24) & 0xF)
    minor, fix = (code >> 20) & 0xF, (code >> 16) & 0xF
    number = "%d.%d%s" % (major, minor, ".%d" % fix if fix else "")
    if 9 <= major <= 22:
        return number, "LabVIEW %d%s (%s)" % (2000 + major, " SP%d" % fix if fix else "", number)
    if 23 <= major <= 60:
        return number, "LabVIEW %d%s (%s)" % (2000 + major, " Q%d" % minor if minor in (1, 3) else "", number)
    return number, "LabVIEW %s" % number


def _version(c: Container) -> int:
    for ident in ("LVSR", "LVIN", "LVCC"):
        data = c.first(ident)
        if data and len(data) >= 4:
            return struct.unpack(">I", data[:4])[0]
    for s in c.blocks.get("vers") or []:
        if len(s.data) >= 4:
            return struct.unpack(">I", s.data[:4])[0]
    return 0


def _flags(c: Container) -> int:
    data = c.first("LVSR") or c.first("LVIN") or c.first("LVCC")
    return struct.unpack(">I", data[4:8])[0] if data and len(data) >= 8 else 0


def locked(c: Container) -> bool:
    """Whether the block diagram is locked with a password (BDPW holds the password's MD5; an empty one is none)."""
    data = c.first("BDPW")
    if data and len(data) >= 16 and data[:16] != _NO_PASSWORD:
        return True
    return bool(_flags(c) & _LOCKED)


def description(c: Container) -> str:
    """The VI's description (VI Properties), as its text: STRG holds it as a 4-byte length and the text."""
    data = c.first("STRG")
    if not data or len(data) < 4:
        return ""
    n = struct.unpack(">I", data[:4])[0]
    return _text(data[4:4 + n]).replace("\r\n", "\n").replace("\r", "\n").strip()


# ------------------------------------------------------------------ linkage

_WHERE = {"<vilib>": "vi.lib", "<instrlib>": "instr.lib", "<userlib>": "user.lib"}
_LINK_IDENT = re.compile(rb"[A-Za-z0-9.][A-Za-z0-9.]{3}")


@dataclass
class Link:
    name: str            # the qualified name: "Keithley 2400 Series.lvlib:Configure Source.vi"
    path: List[str]      # where it was when saved: ["<instrlib>", "Keithley 2400 Series", "Configure Source.vi"]
    kind: str            # vi, control, library, class, dll, file, other
    where: str           # vi.lib, instr.lib, user.lib, LabVIEW (another of its folders) or "" (the lab's own)
    ident: str           # the link's four letters (VIVI: a sub-VI, VICC: a type definition, VILB: its library...)


def _path(raw: bytes, at: int) -> Tuple[Optional[List[str]], int]:
    """A path as LabVIEW saves one (PTH0, PTH1, PTH2): its parts, and where the next thing starts."""
    kind = raw[at:at + 4]
    if kind not in (b"PTH0", b"PTH1", b"PTH2") or at + 8 > len(raw):
        return None, at
    total = struct.unpack(">I", raw[at + 4:at + 8])[0]
    end = at + 8 + total
    if end > len(raw) or total > 65536:
        return None, at
    parts: List[str] = []
    if kind == b"PTH0":
        if total == 0:
            return [], end
        if total < 4:
            return None, at
        count = struct.unpack(">H", raw[at + 10:at + 12])[0]
        p = at + 12
        for _ in range(count):
            if p >= end:
                return None, at
            n = raw[p]
            parts.append(_text(raw[p + 1:p + 1 + n]))
            p += 1 + n
        return (parts, end) if p == end else (None, at)
    p = at + 12   # PTH1, PTH2: a type (abs, rel, unc), then parts of a 2-byte length
    while p + 2 <= end:
        n = struct.unpack(">H", raw[p:p + 2])[0]
        parts.append(_text(raw[p + 2:p + 2 + n]))
        p += 2 + n
    return (parts, end) if p == end else (None, at)


def _qualified(raw: bytes, at: int) -> Tuple[Optional[List[str]], int]:
    """A qualified name: a 4-byte count, then that many Pascal strings."""
    if at + 4 > len(raw):
        return None, at
    count = struct.unpack(">I", raw[at:at + 4])[0]
    if count > 16:
        return None, at
    names, p = [], at + 4
    for _ in range(count):
        if p >= len(raw):
            return None, at
        n = raw[p]
        if p + 1 + n > len(raw):
            return None, at
        names.append(_text(raw[p + 1:p + 1 + n]))
        p += 1 + n
    return names, p


def _kind_of(name: str) -> str:
    low = name.lower()
    for kind, ends in (("vi", (".vi", ".vit", ".vim")), ("control", (".ctl", ".ctt")), ("library", (".lvlib", ".llb")),
                       ("class", (".lvclass", ".lvsc")), ("dll", (".dll", ".so", ".framework", ".dylib", "*.*")),
                       ("xcontrol", (".xctl",))):
        if low.endswith(ends):
            return kind
    return "other"


def _where(path: List[str]) -> str:
    if path and path[0].lower() in _WHERE:
        return _WHERE[path[0].lower()]
    if path and path[0].startswith("<") and path[0].endswith(">"):
        return "LabVIEW"
    for part in path[:-1]:
        if part.lower() in ("vi.lib", "instr.lib", "user.lib"):
            return part.lower()
    return ""


def links_in(data: bytes) -> List[Link]:
    """The links in a linkage block (LIvi, LIfp, LIbd, LIds): each is four letters, a qualified name and a path,
    found by their form wherever they are (the rest of a link differs between LabVIEW versions and kinds)."""
    out: List[Link] = []
    at = 0
    while True:
        at = min((i for i in (data.find(b"PTH0", at), data.find(b"PTH1", at), data.find(b"PTH2", at)) if i >= 0),
                 default=-1)
        if at < 0:
            return out
        path, end = _path(data, at)
        if path is None:
            at += 4
            continue
        found = None
        # the qualified name ends just before the path (padded to an even place): try each start that fits, the
        # nearest first, and take the first with a link's four letters before it
        for start in range(at - 4, max(-1, at - 1200), -1):
            names, p = _qualified(data, start)
            if names is None or not (p == at or (p + 1 == at and data[p] == 0)):
                continue
            ident_at = start - 4
            while ident_at >= 0 and ident_at > start - 8 and not _LINK_IDENT.fullmatch(data[ident_at:ident_at + 4]):
                ident_at -= 1
            if ident_at >= 0 and _LINK_IDENT.fullmatch(data[ident_at:ident_at + 4]) and \
                    all(b == 0 for b in data[ident_at + 4:start]):
                found = (data[ident_at:ident_at + 4].decode("latin-1"), names)
                break
        if found is not None:
            ident, names = found
            name = ":".join(n for n in names if n) or (path[-1] if path else "")
            if name:
                kind = _kind_of(names[-1] if names and names[-1] else name if not path else path[-1])
                out.append(Link(name, path, kind, _where(path), ident))
        at = end if end > at else at + 4


def linkage(c: Container) -> List[Link]:
    """Every link of the VI, once each (by name, with its path where a link says it): its sub-VIs, type
    definitions, libraries, classes and DLLs. An Express VI's own instance ("Instance:4") is part of the VI, not a
    link of it."""
    seen: Dict[str, Link] = {}
    for ident in ("LIvi", "LIfp", "LIbd", "LIds"):
        for s in c.blocks.get(ident) or []:
            try:
                data = unpack(s.data) or b""
            except ReadError:
                continue
            for link in links_in(data):
                if link.name.startswith("Instance:"):
                    continue
                key = link.name.lower()
                if key not in seen or (link.path and not seen[key].path):
                    seen[key] = link
    return list(seen.values())


def path_text(parts: List[str]) -> str:
    """A path LabVIEW saved, as text: a relative one's empty parts are its folder's parent (..)."""
    return "/".join(p if p else ".." for p in parts)


# ------------------------------------------------------------------ heaps: the front panel and block diagram

# The heap's tags (the field an object fills in its parent) and classes, as LabVIEW numbers them.
TAG = {"root": 214, "nodeList": 164, "diagramList": 63, "termList": 268, "signalList": 233, "dco": 48, "ddo": 52,
       "label": 127, "textRec": 270, "objFlags": 172, "typeDesc": 283, "bounds": 14, "primResID": 203,
       "primIndex": 202, "parmIndex": 191, "ConstValue": 589, "DefaultData": 571, "conNum": 37, "ddoList": 54,
       "partsList": 194, "partID": 192, "strings": 249, "description": 59, "zPlaneList": 303, "tunnelList": 280,
       "caseSelDCO": 21, "SelectStringArray": 569, "SelectRangeArray32": 567, "SelectRangeArray64": 568,
       "SelectDefaultCase": 565, "EventNodeEvents": 570, "loopIndexDCO": 137, "loopEndDCO": 136,
       "loopTestDCO": 415, "loopLimitDCO": 414, "lpTunDCO": 139, "innerLpTunDCO": 117, "TunnelType": 607,
       "lsrDCOList": 140, "rsrDCO": 216, "selString": 227, "format": 85, "funcName": 428, "tagDLLName": 431,
       "PropItemName": 530, "propList": 206, "methName": 151, "sequenceList": 230, "nodeName": 165, "text": 3,
       "fLoopCondTerm": 412, "ParForWorkers": 572, "className": 26, "timeOutDCO": 275, "dataNodeList": 47,
       "seqLocDCOList": 229, "inArrDCO": 111, "outputDCO": 180, "conPane": 38, "name": 361,
       "kSLHDefaultValueMatchesCtlVI": 624, "buf": 15, "ringSparseValues": 459, "dcoList": 51, "permDCOList": 197,
       "dcoAgg": 49, "srcDCO": 244, "outputNode": 181, "preferredInstIndex": 201, "paramIdx": 189,
       "libPath": 133, "instanceSelector": 122, "menuInstanceUsed": 149}
SYS_ELEMENT = -6
ATTR_CLASS, ATTR_UID, ATTR_ELEMENTS = -2, -3, -5
CLASSES = {1: "textHair", 0: "fontRun", 10: "label", 13: "multiLabel", 14: "bigMultiLabel", 18: "fPDCO",
           19: "bDConstDCO", 20: "bDDCO", 21: "term", 22: "fPTerm", 23: "signal", 27: "diag", 29: "sRN", 32: "forLoop",
           33: "whileLoop", 34: "lpTun", 35: "innerLpTun", 36: "lCnt", 37: "lTst", 38: "lMax", 39: "lSR", 40: "rSR",
           41: "sequence", 42: "seqTun", 43: "sLoc", 44: "select", 45: "selTun", 46: "caseSel", 47: "prim",
           48: "parm", 49: "iUse", 51: "iUseDCO", 52: "mux", 54: "demux", 56: "codeVI", 58: "aBuild", 60: "cABuild",
           62: "concat", 64: "decimate", 66: "interLeave", 68: "aIndx", 72: "subset", 74: "fBox", 76: "supC",
           79: "stdBool", 80: "stdNum", 81: "stdString", 82: "indArr", 83: "stdClust", 84: "stdVar", 85: "stdRefNum",
           86: "stdColorNum", 87: "stdRing", 88: "stdSlide", 89: "stdKnob", 91: "stdPath", 92: "stdTable",
           94: "stdGraph", 95: "stdPict", 98: "nmxDCO", 99: "nMux", 100: "typeDef", 101: "stdRamp", 102: "uCast",
           104: "annex", 105: "stdListbox", 106: "extFunc", 107: "extFuncArg", 108: "cpdArith", 140: "propNode",
           141: "propItem", 146: "scanf", 147: "printf", 148: "stdTag", 149: "selLabel", 168: "hGrowNode",
           169: "invokeNode", 170: "invokeItem", 173: "iUseCore", 174: "callByRefNode", 175: "stdCont",
           180: "scriptNode", 181: "stdComboBox", 182: "ctlRefConst", 184: "stdMeasureData", 185: "aReplace",
           187: "aInsert", 189: "aDelete", 191: "textNode", 192: "exprNode", 193: "cLStrObj", 194: "stdLvVariant",
           195: "tabControl", 197: "polyIUse", 198: "polyIUseDCO", 199: "page", 202: "flatSequence",
           203: "flatSeqTun", 205: "commentNode", 207: "stdSubPanel", 208: "mergeSignal", 211: "splitSignal",
           213: "eventStruct", 214: "eventDataNode", 216: "eventTimeOut", 223: "absTime", 225: "tableControl",
           227: "externalNode", 230: "listbox", 231: "treeControl", 237: "constructorNode", 239: "radioClust",
           240: "externalStructNode", 249: "lTiming", 251: "timeLoop", 252: "timeLoopExtNode", 258: "xControl",
           259: "statVIRef", 260: "dynIUse", 261: "xNode", 263: "xStructure", 266: "dynPolyIUse", 276: "aInit",
           278: "aReshape", 287: "timeFlatSequenceFrame", 289: "sequenceFrame", 290: "timeSequence",
           291: "timeFlatSequence", 292: "callParentDynIUse", 293: "matedLpTun", 294: "matedSeqTun", 295: "matedLSR",
           296: "matedRSR", 298: "mathScriptNode", 313: "regionNode", 324: "leftFeedback", 325: "rightFeedback",
           326: "initFeedback", 328: "sharedVariable", 330: "hiddenFBNode", 333: "decomposeRecomposeStructure",
           359: "genIUse", 361: "parForWorkers", 362: "sharedVariableDynamicOpen", 363: "sharedVariableDynamicRead",
           364: "sharedVariableDynamicWrite", 367: "chunkSize", 370: "mergeErrors", 929: "EventSpec",
           930: "SelectorRange"}


@dataclass
class Obj:
    """One object of a front panel's or block diagram's heap: the field it fills in its parent (tag), its class, uid,
    content (bytes, or True/False) and the objects inside it."""
    tag: int
    cls: Optional[int] = None
    uid: Optional[int] = None
    value: Any = None
    kids: List["Obj"] = field(default_factory=list)
    parent: Optional["Obj"] = field(default=None, repr=False)

    @property
    def kind(self) -> str:
        return CLASSES.get(self.cls, "class%d" % self.cls) if self.cls is not None else ""

    def get(self, name: str) -> Optional["Obj"]:
        tag = TAG[name]
        return next((k for k in self.kids if k.tag == tag), None)

    def items(self, name: str) -> List["Obj"]:
        """The elements of an array field (termList, nodeList...)."""
        holder = self.get(name)
        return [k for k in holder.kids if k.tag == SYS_ELEMENT] if holder is not None else []

    def number(self, name: str, signed: bool = True) -> Optional[int]:
        o = self.get(name)
        if o is None or not isinstance(o.value, (bytes, bytearray)) or not 0 < len(o.value) <= 8:
            return None
        return int.from_bytes(o.value, "big", signed=signed)

    def raw(self, name: str) -> Optional[bytes]:
        o = self.get(name)
        return bytes(o.value) if o is not None and isinstance(o.value, (bytes, bytearray)) else None

    def walk(self) -> Iterator["Obj"]:
        stack = [self]
        while stack:
            o = stack.pop()
            yield o
            stack.extend(reversed(o.kids))


def _u124(b: bytes, i: int) -> Tuple[int, int]:
    v = b[i]
    if v == 255:
        return struct.unpack(">H", b[i + 1:i + 3])[0], i + 3
    if v == 254:
        return struct.unpack(">I", b[i + 1:i + 5])[0], i + 5
    return v, i + 1


def _s124(b: bytes, i: int) -> Tuple[int, int]:
    v = struct.unpack(">b", b[i:i + 1])[0]
    if v == -128:
        return struct.unpack(">h", b[i + 1:i + 3])[0], i + 3
    if v == -127:
        return struct.unpack(">i", b[i + 1:i + 5])[0], i + 5
    return v, i + 1


def _s24(b: bytes, i: int) -> Tuple[int, int]:
    v = struct.unpack(">h", b[i:i + 2])[0]
    if v == -0x8000:
        return struct.unpack(">i", b[i + 2:i + 6])[0], i + 6
    return v, i + 2


def heap(data: bytes) -> Obj:
    """A front panel's or block diagram's heap (FPHb, BDHb, unpacked) as a tree of objects. Each entry opens an
    object, closes one, or is an object with nothing inside; it may carry attributes (class, uid) and content.
    ReadError: damaged."""
    if len(data) < 4:
        raise ReadError("an empty heap")
    end = min(len(data), 4 + struct.unpack(">I", data[:4])[0])
    top = Obj(tag=0)
    stack = [top]
    i, count = 4, 0
    try:
        while i < end:
            c0, c1 = data[i], data[i + 1]
            i += 2
            size, has_attrs, scope = (c0 >> 5) & 7, (c0 >> 4) & 1, (c0 >> 2) & 3
            raw_tag = c1 | ((c0 & 3) << 8)
            if raw_tag == 1023:
                tag = struct.unpack(">i", data[i:i + 4])[0]
                i += 4
            else:
                tag = raw_tag - 31
            cls = uid = None
            if has_attrs:
                n, i = _u124(data, i)
                for _ in range(n):
                    key, i = _s124(data, i)
                    value, i = _s24(data, i)
                    if key == ATTR_CLASS:
                        cls = value
                    elif key == ATTR_UID:
                        uid = value
            value: Any = None
            if 1 <= size <= 4:
                value = data[i:i + size]
                i += size
            elif size == 6:
                n, i = _u124(data, i)
                value = data[i:i + n]
                i += n
            elif size == 7:
                value = True
            if scope == 2:   # the end of the object opened last
                if len(stack) > 1:
                    stack.pop()
                continue
            o = Obj(tag, cls, uid, value, parent=stack[-1])
            stack[-1].kids.append(o)
            count += 1
            if count > MAX_OBJECTS:
                raise ReadError("a heap with more objects than Kelvin reads")
            if scope == 0:
                stack.append(o)
    except (IndexError, struct.error):
        raise ReadError("a heap that ends early")
    return top.kids[0] if len(top.kids) == 1 else top


def label_of(o: Optional[Obj]) -> str:
    """An object's name label (a control's, a structure's, a sub-VI's), as its text."""
    if o is None:
        return ""
    lab = o.get("label")
    if lab is None:
        for part in o.items("partsList"):
            if part.kind == "label" and part.number("partID") == 16:   # the name label (16), not a value's text
                lab = part
                break
    return text_of(lab)


def text_of(o: Optional[Obj]) -> str:
    """The text of a label (its textRec's text)."""
    if o is None:
        return ""
    rec = o.get("textRec")
    if rec is None:
        return ""
    t = next((k for k in rec.kids if k.tag == TAG["text"] and isinstance(k.value, (bytes, bytearray))), None)
    if t is None:
        return ""
    return _text(bytes(t.value)).replace("\r\n", "\n").replace("\r", "\n").strip().strip("\x00").strip()


# ------------------------------------------------------------------ types

NUMBERS = {1: ("I8", ">b"), 2: ("I16", ">h"), 3: ("I32", ">i"), 4: ("I64", ">q"), 5: ("U8", ">B"), 6: ("U16", ">H"),
           7: ("U32", ">I"), 8: ("U64", ">Q"), 9: ("SGL", ">f"), 10: ("DBL", ">d"), 11: ("EXT", ""),
           12: ("CSG", ">ff"), 13: ("CDB", ">dd"), 14: ("CXT", ""), 0x19: ("SGL", ">f"), 0x1A: ("DBL", ">d"),
           0x1B: ("EXT", ""), 0x1C: ("CSG", ">ff"), 0x1D: ("CDB", ">dd"), 0x1E: ("CXT", "")}
ENUMS = {0x15: ">B", 0x16: ">H", 0x17: ">I"}
TAG_TYPES = {1: "shared variable", 2: "DAQ channel", 3: "IVI logical name", 4: "VISA resource", 5: "user tag",
             7: "FieldPoint I/O", 8: "motion resource", 9: "DAQmx task", 10: "DAQmx channel", 11: "DAQmx scale",
             12: "DAQmx device", 13: "DAQmx terminal", 14: "DAQmx physical channel", 16: "DAQmx switch"}
REFNUMS = {14: "VISA", 15: "IVI", 34: "TDMS file", 2: "file", 5: "TCP", 16: "UDP", 18: "queue", 17: "notifier",
           30: "class", 13: "DataSocket", 8: "control", 9: "menu", 25: "user event", 23: "event registration"}


@dataclass
class Type:
    """One of a VI's types (VCTP): code is LabVIEW's, kind Kelvin's word for it."""
    code: int
    kind: str                      # number, bool, string, path, enum, ring, array, cluster, tag, refnum, variant,
                                   # waveform, timestamp, typedef, function, void, other
    label: str = ""
    numeric: str = ""              # DBL, I32... for numbers and enums
    names: List[str] = field(default_factory=list)   # an enum's values
    items: List[int] = field(default_factory=list)   # a cluster's or an array's element types (VCTP indices)
    dims: int = 0
    tag: str = ""                  # a tag's kind ("VISA resource", "DAQmx channel")
    ref: str = ""                  # a refnum's kind ("VISA")
    inner: Optional["Type"] = None  # a type definition's type
    typedef: str = ""              # a type definition's qualified name


def _u2p2(b: bytes, i: int) -> Tuple[int, int]:
    v = struct.unpack(">H", b[i:i + 2])[0]
    if v & 0x8000:
        return ((v & 0x7FFF) << 16) | struct.unpack(">H", b[i + 2:i + 4])[0], i + 4
    return v, i + 2


def _label_at_end(b: bytes, start: int, end: int) -> str:
    """A type's label: a Pascal string ending its data (maybe padded by one byte)."""
    for i in range(max(start, end - 257), end):
        n = b[i]
        if n and (i + 1 + n == end or (i + 2 + n == end and b[end - 1] == 0)):
            return _text(b[i + 1:i + 1 + n])
    return ""


def _type_at(b: bytes, at: int, nested: bool = False) -> Tuple[Type, int]:
    """One type at b[at:] and where the next one starts. nested: inside a type definition, where LabVIEW counts
    its length 4 more than it is."""
    size, p = _u2p2(b, at)
    end = at + size - (4 if nested else 0)
    if size < 4 or end > len(b) or end < p + 2:
        raise ReadError("a damaged type")
    flags, code = b[p], b[p + 1]
    p += 2
    t = Type(code, "other")
    try:
        if code in NUMBERS:
            t.kind, t.numeric = "number", NUMBERS[code][0]
        elif code in ENUMS:
            t.kind, t.numeric = "enum", {0x15: "U8", 0x16: "U16", 0x17: "U32"}[code]
            n = struct.unpack(">H", b[p:p + 2])[0]
            q = p + 2
            for _ in range(min(n, 10000)):
                name, q = _pascal(b, q)
                t.names.append(name)
        elif code in (0x20, 0x21):
            t.kind = "bool"
        elif code in (0x30, 0x31, 0x34, 0x35):
            t.kind = "string"
        elif code == 0x32:
            t.kind = "path"
        elif code == 0x37:
            t.kind = "tag"
            t.tag = TAG_TYPES.get(struct.unpack(">H", b[p + 4:p + 6])[0], "")
        elif code == 0x40:
            t.kind = "array"
            t.dims = struct.unpack(">H", b[p:p + 2])[0]
            q = p + 2 + 4 * t.dims
            t.items = [_u2p2(b, q)[0]]
        elif code == 0x50:
            t.kind = "cluster"
            n, q = _u2p2(b, p)
            for _ in range(min(n, 10000)):
                k, q = _u2p2(b, q)
                t.items.append(k)
        elif code == 0x53:
            t.kind = "variant"
        elif code == 0x54:
            t.kind = "timestamp" if struct.unpack(">H", b[p:p + 2])[0] == 6 else "waveform"
        elif code == 0x70:
            t.kind = "refnum"
            t.ref = REFNUMS.get(struct.unpack(">H", b[p:p + 2])[0], "")
        elif code == 0xF0:
            t.kind = "function"
        elif code == 0xF1:
            t.kind = "typedef"
            names, q = _qualified(b, p + 4)
            t.typedef = ":".join(names or [])
            if names is not None and q < end:
                if q % 2:
                    q += 1 if b[q] == 0 else 0
                t.inner, _ = _type_at(b, q, nested=True)
        elif code == 0:
            t.kind = "void"
    except (IndexError, struct.error, ReadError):
        pass   # a type read in part: its kind is enough
    if flags & 0x40:
        t.label = _label_at_end(b, p, end)
    return t, at + size


@dataclass
class Types:
    flat: List[Type] = field(default_factory=list)
    top: List[int] = field(default_factory=list)
    heap_count: int = 0
    heap_shift: int = 0

    def of_heap(self, type_id: Optional[int]) -> Optional[Type]:
        """The type a heap object names (its typeDesc), through DTHP's slice of VCTP's top list."""
        if type_id is None or type_id < 1 or (self.heap_count and type_id > self.heap_count):
            return None
        i = self.heap_shift + type_id - 2
        if 0 <= i < len(self.top) and 0 <= self.top[i] < len(self.flat):
            return self.flat[self.top[i]]
        return None

    def item(self, index: int) -> Optional[Type]:
        return self.flat[index] if 0 <= index < len(self.flat) else None


def types(c: Container) -> Types:
    """The VI's types (VCTP: every type, then the list the other blocks name them by) and the slice of them the
    heaps name (DTHP)."""
    out = Types()
    data = unpack(c.first("VCTP"))
    if data and len(data) >= 4:
        count = struct.unpack(">I", data[:4])[0]
        at = 4
        for _ in range(min(count, 200000)):
            t, at = _type_at(data, at)
            out.flat.append(t)
        n, at = _u2p2(data, at)
        for _ in range(min(n, 200000)):
            k, at = _u2p2(data, at)
            out.top.append(k)
    dthp = c.first("DTHP")
    if dthp and len(dthp) >= 2:
        out.heap_count, at = _u2p2(dthp, 0)
        if out.heap_count and at + 2 <= len(dthp):
            out.heap_shift = _u2p2(dthp, at)[0]
    return out


def plain(t: Optional[Type]) -> Optional[Type]:
    """A type without its type definition around it."""
    for _ in range(8):
        if t is None or t.kind != "typedef" or t.inner is None:
            break
        t = t.inner
    return t


class _Flat:
    """Reads a value as LabVIEW flattens it (big-endian), by its type."""

    def __init__(self, data: bytes, kinds: Types):
        self.b, self.at, self.types = data, 0, kinds

    def take(self, n: int) -> bytes:
        if self.at + n > len(self.b):
            raise ReadError("a value that ends early")
        out = self.b[self.at:self.at + n]
        self.at += n
        return out

    def value(self, t: Optional[Type], depth: int = 0) -> Any:
        t = plain(t)
        if t is None or depth > 20:
            raise ReadError("a value of a type Kelvin can't read")
        if t.kind == "number":
            name, fmt = NUMBERS[t.code]
            if name in ("EXT", "CXT"):
                parts = [_quad(self.take(16)) for _ in range(2 if name == "CXT" else 1)]
                return parts[0] if name == "EXT" else complex(*parts)
            got = struct.unpack(fmt, self.take(struct.calcsize(fmt)))
            return got[0] if len(got) == 1 else complex(*got)
        if t.kind == "enum":
            fmt = ENUMS[t.code]
            i = struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]
            return t.names[i] if 0 <= i < len(t.names) else i
        if t.kind == "bool":
            return bool(self.take(2 if t.code == 0x20 else 1)[-1])
        if t.kind in ("string", "tag") or (t.kind == "refnum" and t.ref in ("VISA", "IVI")):
            n = struct.unpack(">I", self.take(4))[0]
            text = _text(self.take(n))
            if t.kind != "string":   # a VISA resource or DAQmx name: its text, then what its type keeps
                self.at = len(self.b)
            return text
        if t.kind == "path":
            if self.b[self.at:self.at + 4] not in (b"PTH0", b"PTH1", b"PTH2"):
                raise ReadError("a path that isn't one")
            parts, end = _path(self.b, self.at)
            if parts is None:
                raise ReadError("a damaged path")
            self.at = end
            return "/".join(parts)
        if t.kind == "array":
            dims = [struct.unpack(">I", self.take(4))[0] for _ in range(max(1, t.dims))]
            total = 1
            for d in dims:
                total *= d
            if total > MAX_VALUES:
                raise ReadError("an array bigger than Kelvin reads")
            inner = self.types.item(t.items[0]) if t.items else None
            flat = [self.value(inner, depth + 1) for _ in range(total)]
            if len(dims) == 2 and dims[1]:
                return [flat[i * dims[1]:(i + 1) * dims[1]] for i in range(dims[0])]
            return flat
        if t.kind == "cluster":
            return [self.value(self.types.item(k), depth + 1) for k in t.items]
        raise ReadError("a value of a type Kelvin can't read (%s)" % t.kind)


def _quad(b: bytes) -> float:
    """LabVIEW's extended precision (a 16-byte IEEE quad) as a float."""
    n = int.from_bytes(b, "big")
    sign = -1.0 if n >> 127 else 1.0
    exponent = (n >> 112) & 0x7FFF
    mantissa = n & ((1 << 112) - 1)
    if exponent == 0x7FFF:
        return sign * float("inf") if mantissa == 0 else float("nan")
    if exponent == 0 and mantissa == 0:
        return 0.0 * sign
    try:
        return sign * (1 + mantissa / float(1 << 112)) * 2.0 ** (exponent - 16383)
    except OverflowError:
        return sign * float("inf")


def decode(data: Optional[bytes], t: Optional[Type], kinds: Types) -> Tuple[Any, bool]:
    """A flattened value (a control's saved value, a constant's) by its type: (value, whether it was read)."""
    if data is None or t is None:
        return None, False
    try:
        return _Flat(bytes(data), kinds).value(t), True
    except (ReadError, struct.error, IndexError, KeyError, OverflowError, ValueError):
        return None, False


def kind_of(t: Optional[Type], cls: str = "") -> str:
    """A control's type in the summary's words: number, bool, string, enum, ring, path, visa, array, cluster, other."""
    if cls == "stdRing":
        return "ring"
    p = plain(t)
    if p is None:
        return {"stdBool": "bool", "stdNum": "number", "stdString": "string", "stdPath": "path", "indArr": "array",
                "stdClust": "cluster"}.get(cls, "other")
    if (p.kind == "tag" and p.tag == "VISA resource") or (p.kind == "refnum" and p.ref == "VISA"):
        return "visa"
    if p.kind in ("number", "bool", "string", "path", "enum", "array", "cluster"):
        return p.kind
    return "other"


# ------------------------------------------------------------------ the front panel

@dataclass
class Control:
    name: str
    kind: str                     # number, bool, string, enum, ring, path, visa, array, cluster, other
    cls: str                      # LabVIEW's class of it (stdNum, stdString...)
    indicator: bool
    default: Any = None
    has_default: bool = False     # whether the saved value was read
    choices: List[Any] = field(default_factory=list)
    terminal: Optional[int] = None   # its place on the connector pane, or None
    uid: Optional[int] = None        # its data object's (fPDCO), as the block diagram's terminal names it
    numeric: str = ""
    ddo: Optional[int] = None        # its display object's, as an event names it


_ZERO = {"number": 0, "bool": False, "string": "", "path": "", "visa": ""}


def _ring_names(ddo: Obj) -> List[str]:
    """A ring's items: its ring text part (partID 12) keeps them as Pascal strings one after another."""
    for part in ddo.items("partsList"):
        if part.kind in ("multiLabel", "bigMultiLabel") and part.number("partID") == 12:
            buf, out, at = part.raw("buf") or b"", [], 0
            while at < len(buf):
                try:
                    name, at = _pascal(buf, at)
                except ReadError:
                    break
                out.append(name)
            return out
    return []


def controls(fp: Obj, kinds: Types, major: int = 99) -> List[Control]:
    """The front panel's controls and indicators, in the order the panel lists them: each with its name, type,
    whether it is an indicator, the value the VI is saved with (read where the panel keeps it), an enum's or ring's
    choices, and its place on the connector pane. major: LabVIEW's version; from 9 (2009) the panel keeps each value
    that isn't its type's own (0, "", False, an enum's first), before it LabVIEW kept them elsewhere."""
    out: List[Control] = []
    for o in fp.walk():
        if o.kind != "fPDCO":
            continue
        ddo = o.get("ddo")
        if ddo is None or ddo.kind in ("tabControl", "stdSubPanel", "splitter", "pane"):
            continue   # what holds other controls, not a value of the measurement's
        flags = ddo.number("objFlags", signed=False) or 0
        t = kinds.of_heap(o.number("typeDesc")) or kinds.of_heap(ddo.number("typeDesc"))
        kind = kind_of(t, ddo.kind)
        con = o.number("conNum")
        p = plain(t)
        name = label_of(ddo) or (t.label.strip("\x00 ") if t else "")
        if not name:   # a control without a label: named by its type
            unnamed = sum(1 for x in out if x.name.startswith("unlabelled "))
            name = "unlabelled %s %d" % ("VISA resource" if kind == "visa" else kind, unnamed + 1)
        c = Control(name=name, kind=kind, cls=ddo.kind, indicator=bool(flags & 1),
                    terminal=con if con is not None and con >= 0 else None, uid=o.uid,
                    numeric=p.numeric if p is not None else "", ddo=ddo.uid)
        if p is not None and p.kind == "enum":
            c.choices = list(p.names)
        elif kind == "ring":
            c.choices = _ring_names(ddo)
        saved = o.raw("DefaultData")
        if saved is not None:
            c.default, c.has_default = decode(saved, t, kinds)
        elif major < 9 or o.get("kSLHDefaultValueMatchesCtlVI") is not None:
            pass   # kept elsewhere: in LabVIEW 8's data space, or in the control's type definition (.ctl)
        elif p is not None and p.kind == "enum" and p.names:
            c.default, c.has_default = p.names[0], True
        elif kind in _ZERO or kind == "ring":
            c.default, c.has_default = _ZERO.get(kind, 0), True
        out.append(c)
    return out


# ------------------------------------------------------------------ reading a file

@dataclass
class VI:
    """What a VI's file holds, as Kelvin reads it."""
    path: str
    name: str
    kind: str = ""                     # vi, control, template...
    version: str = ""                  # "18.0"
    labview: str = ""                  # "LabVIEW 2018 (18.0)"
    locked: bool = False               # its block diagram is locked with a password
    no_diagram: bool = False           # saved without its block diagram
    description: str = ""
    library: str = ""                  # the library it belongs to ("Keithley 2400 Series.lvlib")
    instances: List[str] = field(default_factory=list)   # a polymorphic VI's instances' names, in their order
    links: List[Link] = field(default_factory=list)
    controls: List[Control] = field(default_factory=list)
    panel: Optional[Obj] = None        # the front panel's heap
    diagram: Optional[Obj] = None      # the block diagram's heap (None: not read)
    types: Types = field(default_factory=Types)
    unread: List[str] = field(default_factory=list)
    encoding: str = "cp1252"           # its text's (text_encoding() reads its diagram's text in it)


def _limit(p: Path) -> bytes:
    size = p.stat().st_size
    if size > MAX_BYTES:
        raise ReadError("it is bigger than Kelvin reads (%d MB)" % (MAX_BYTES // 2 ** 20))
    return p.read_bytes()


def _encoding(c: Container) -> str:
    for s in c.blocks.get("vers") or []:
        if len(s.data) >= 6:
            return REGIONS.get(struct.unpack(">H", s.data[4:6])[0], "cp1252")
    return "cp1252"


def read_bytes(raw: bytes, path: str = "", name: str = "") -> VI:
    """A VI (or control) from its bytes. ReadError: not a LabVIEW resource file at all; anything less is said in
    unread."""
    c = container(raw)
    encoding = _encoding(c)
    with text_encoding(encoding):
        vi = _read(c, path, name)
    vi.encoding = encoding
    return vi


def _read(c: Container, path: str, name: str) -> VI:
    vi = VI(path=path, name=name or Path(path).name, kind=c.kind)
    vi.unread += c.problems
    code = _version(c)
    major = ((code >> 28) & 0xF) * 10 + ((code >> 24) & 0xF) if code else 99
    if code:
        vi.version, vi.labview = version_words(code)
    vi.locked = locked(c)
    vi.no_diagram = bool(_flags(c) & _HAS_NO_DIAGRAM)
    vi.description = description(c)
    owner = c.first("LIBN")
    if owner:
        names, _ = _qualified(owner, 0)
        vi.library = ":".join(names or [])
    menu = c.first("CPST")   # a polymorphic VI's instances, as its menu names them, in their order
    if menu:
        names, _ = _qualified(menu, 0)
        vi.instances = names or []
    try:
        vi.links = linkage(c)
    except (ReadError, IndexError, struct.error) as e:
        vi.unread.append("its linkage (%s)" % e)
    try:
        vi.types = types(c)
    except (ReadError, IndexError, struct.error) as e:
        vi.unread.append("its types (%s): controls' values and types" % e)
    old = [k for k in c.blocks if k in ("FPHP", "BDHP", "FPHX", "BDHX", "FPHB", "BDHB", "FPHc", "BDHc", "FPHT")]
    try:
        fp = unpack(c.first("FPHb"))
        if fp is not None:
            vi.panel = heap(fp)
            vi.controls = controls(vi.panel, vi.types, major)
            if major < 9 and vi.controls:
                vi.unread.append("its controls' saved values%s (LabVIEW %s keeps them where Kelvin doesn't read yet)"
                                 % (" and names" if any(c.name.startswith("unlabelled ") for c in vi.controls)
                                    else "", vi.version))
        elif c.kind in ("vi", "template", "control", "control template"):
            vi.unread.append("its front panel (%s)" % ("kept in a form of LabVIEW before 8.0 (%s)" % ", ".join(old)
                                                       if old else "not in the file"))
    except (ReadError, IndexError, struct.error) as e:
        vi.unread.append("its front panel (%s)" % e)
    if vi.locked:
        vi.unread.append("its block diagram: locked with a password (Kelvin doesn't read past one)")
    elif c.first("BDHb") is None:
        if vi.no_diagram or c.kind in ("vi", "template"):
            vi.unread.append("its block diagram: %s" % ("removed when the VI was built" if vi.no_diagram or not old
                                                         else "kept in a form of LabVIEW before 8.0"))
    else:
        try:
            vi.diagram = heap(unpack(c.first("BDHb")) or b"")
        except (ReadError, IndexError, struct.error) as e:
            vi.unread.append("its block diagram (%s)" % e)
    return vi


def read(path: Any) -> VI:
    """A VI, control or template file. ReadError: not one (or unreadable), in plain words."""
    p = Path(str(path))
    try:
        raw = _limit(p)
    except OSError as e:
        raise ReadError("it can't be read: %s" % (e.strerror or e))
    return read_bytes(raw, str(p), p.name)


def members(path: Any) -> List[Tuple[str, bytes]]:
    """The files a VI library (.llb) holds, by name: [(name, bytes)]. Only the uncompressed and zlib-compressed
    ones (UCRF, ZCRF); one kept another way is listed with no bytes. ReadError: not a VI library."""
    p = Path(str(path))
    try:
        c = container(_limit(p))
    except OSError as e:
        raise ReadError("it can't be read: %s" % (e.strerror or e))
    if c.kind != "library":
        raise ReadError("it isn't a VI library (.llb)")
    out: List[Tuple[str, bytes]] = []
    for ident in ("UCRF", "ZCRF", "CPRF"):
        for s in c.blocks.get(ident) or []:
            name = s.name or "%s %d" % (ident, s.index)
            data = s.data if ident == "UCRF" else b""
            if ident == "ZCRF":
                try:
                    data = unpack(s.data) or b""
                except ReadError:
                    data = b""
            out.append((name, data))
            if len(out) >= MAX_LLB_MEMBERS:
                return out
    return out
