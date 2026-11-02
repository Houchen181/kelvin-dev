"""MATLAB's .mat files, read by Kelvin itself with the standard library and NumPy (SciPy is not part of Kelvin): what a
lab's MATLAB programs saved, Special Measure's runs among them (importers/specialmeasure.py).

Read as MathWorks documents the format (MAT-File Format: MATLAB 5's layout, which versions 6 and 7 keep):
- a 128-byte header: its text ("MATLAB 5.0 MAT-file, Platform: ..., Created on: ..."), the version, and the byte
  order the file was written in (IM: little-endian, MI: big-endian);
- then the variables, each a data element: a tag (its type and length; for four bytes or fewer, both in one word with
  the data in the next four, the small-element form), then its data, padded to 8 bytes; version 7 compresses each
  variable with zlib (an miCOMPRESSED element holding one miMATRIX);
- a variable (miMATRIX): its array flags and class, its dimensions and name, then by class: its numbers (double,
  single, int8 to uint64, logical; a complex one's imaginary part after them), its characters, a cell's elements, a
  struct's field names and each element's fields, an object's class and fields, a sparse matrix's row indices, column
  starts and values, a function handle's description, or a newer kind of object's class name (MCOS, never decoded).
MATLAB 7.3's files are HDF5 files with MATLAB's header in their first 512 bytes: read with h5py (in Kelvin's app), as
MATLAB lays them out (each variable a dataset or group with its MATLAB_class; a cell's elements as references; text
as UTF-16; a struct as a group).

What a value becomes:
- numbers: a NumPy array of MATLAB's shape and class (a[i, j] is MATLAB's a(i+1, j+1)); logical ones as bool;
- characters: a str (one row), or a list of str (each row of a char matrix, without the spaces that pad it);
- a cell: a NumPy array of objects, of the cell's shape;
- a struct: a Struct (a dict of its fields, in order); a struct array, a NumPy array of them; one of MATLAB's older
  objects, a Struct with its classname;
- a function handle: a Function, its text as MATLAB shows it ("@(x)smset('Vg',0)", "@smset");
- an object MATLAB saves in its newer form (a table, a datetime, a fit): an Opaque, its class's name, never decoded;
- a sparse matrix: dense when it has DENSE elements or fewer, else a Sparse saying its size.
plain() makes any of them plain Python (numbers, str, lists, dicts), for JSON or YAML.

Every file is someone else's: Kelvin reads at most imports.MAX_BYTES of it and MAX_UNPACKED once decompressed (and of
values made from it), cells and structs at most MAX_DEPTH inside each other and MAX_ITEMS of their elements, and
refuses a file that breaks the format (an element longer than what holds it, values that don't fill their dimensions,
a compressed stream cut short) with a DataError that names the file."""
from __future__ import annotations

import math
import re
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from .imports import MAX_BYTES, DataError

MAX_UNPACKED = 512 * 2 ** 20   # bytes of a file's variables once decompressed, and of the values made from them
MAX_DEPTH = 32                 # cells and structs inside each other
MAX_ITEMS = 10 ** 6            # cells' and structs' elements in one file
MAX_DIMS = 32
DENSE = 2 ** 22                # a sparse matrix of this many elements or fewer is made dense
PEEK = 4096                    # bytes of a compressed variable decompressed to read its name, class and size
V5 = b"MATLAB 5.0 MAT-file"
V73 = b"MATLAB 7.3 MAT-file"
HDF5 = b"\x89HDF\r\n\x1a\n"    # an HDF5 file's signature: at 512 in MATLAB 7.3's files, after MATLAB's header

# data elements' types (mi...) and how their numbers are stored
INT8, UINT8, INT16, UINT16, INT32, UINT32, SINGLE, DOUBLE, INT64, UINT64 = 1, 2, 3, 4, 5, 6, 7, 9, 12, 13
MATRIX, COMPRESSED, UTF8, UTF16, UTF32 = 14, 15, 16, 17, 18
STORED = {INT8: "i1", UINT8: "u1", INT16: "i2", UINT16: "u2", INT32: "i4", UINT32: "u4", SINGLE: "f4", DOUBLE: "f8",
          INT64: "i8", UINT64: "u8"}
# variables' classes (mx...)
CELL, STRUCT, OBJECT, CHAR, SPARSE, FUNCTION, OPAQUE = 1, 2, 3, 4, 5, 16, 17
NUMBERS = {6: ("double", "f8"), 7: ("single", "f4"), 8: ("int8", "i1"), 9: ("uint8", "u1"), 10: ("int16", "i2"),
           11: ("uint16", "u2"), 12: ("int32", "i4"), 13: ("uint32", "u4"), 14: ("int64", "i8"),
           15: ("uint64", "u8")}
CLASSES = {CELL: "cell", STRUCT: "struct", CHAR: "char", SPARSE: "sparse", FUNCTION: "function_handle"}
COMPLEX, LOGICAL = 0x800, 0x200   # array flags' bits (0x400: a global variable, which changes nothing here)
MONTHS = {m: i + 1 for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
                                          "Nov", "Dec"))}
# what a damaged file can raise past the format's checks (a file is never refused with a bare error)
UNFORESEEN = (struct.error, zlib.error, ValueError, IndexError, KeyError, TypeError, OverflowError, MemoryError,
              UnicodeError, EOFError)


class Struct(dict):
    """A MATLAB struct: its fields in the file's order. classname: an object's class (MATLAB's older objects, saved
    with their fields), else ""."""
    classname = ""


class Function(str):
    """A MATLAB function handle, as its text: "@(x)smset({'Vtg','Vbg'},[0,0])" (an anonymous function), "@smset" (a
    named one); "" when the file doesn't say. kind: MATLAB's word for it (anonymous, simple, scopedfunction, nested).
    What an anonymous function captured isn't read: MATLAB keeps it apart, in the file's own subsystem."""
    kind = ""


class Opaque(str):
    """An object MATLAB saves in its newer form (MCOS: a table, a datetime, a string, a fit), as its class's name:
    never decoded. system: MATLAB's word for the form (MCOS, java)."""
    system = ""


class Sparse(str):
    """A sparse matrix too big to make dense, said in words; shape and nonzero say its size."""
    shape: Tuple[int, ...] = (0, 0)
    nonzero = 0


def _np() -> Any:
    import numpy
    return numpy


def _first(e: BaseException) -> str:
    text = str(e).strip()
    return text.splitlines()[0][:120] if text else type(e).__name__


# ------------------------------------------------------------------ what the file is

def is_mat(head: bytes) -> bool:
    """Whether a file's first bytes are a MATLAB .mat file's: MATLAB 5's header (versions 5 to 7), or 7.3's with the
    HDF5 file after it (head long enough to show it)."""
    if head[:len(V5)] == V5:
        return True
    return head[:len(V73)] == V73 and (len(head) < 520 or head[512:520] == HDF5)


def _hdf5(head: bytes) -> bool:
    return head[:8] == HDF5 or head[512:520] == HDF5 or head[:len(V73)] == V73


def _open(path: Any) -> Tuple[Path, bytes]:
    """The file and its first bytes, after the checks every reading makes (it is a file, not too big)."""
    p = Path(str(path)).expanduser()
    if not p.is_file():
        raise DataError("there is no file %s" % path)
    if p.stat().st_size > MAX_BYTES:
        raise DataError("%s is bigger than Kelvin imports (%d MB)" % (p.name, MAX_BYTES // 2 ** 20))
    with open(p, "rb") as f:
        head = f.read(520)
    return p, head


def _order(head: bytes, name: str) -> str:
    """The byte order a MATLAB 5 file says it was written in; DataError for a file that isn't one."""
    if len(head) < 128:
        raise DataError("%s is too short for a MATLAB .mat file" % name)
    mark = head[126:128]
    if mark not in (b"IM", b"MI"):
        raise DataError("%s is not a MATLAB .mat file Kelvin reads: it doesn't start with MATLAB's header (a MATLAB 4 "
                        "file doesn't either: save it again in MATLAB with save -v7, and import that)" % name)
    order = "<" if mark == b"IM" else ">"
    version = struct.unpack(order + "H", head[124:126])[0]
    if version != 0x0100:
        raise DataError("%s: a .mat file of a version Kelvin doesn't know (%#06x)" % (name, version))
    return order


def header(path: Any) -> Dict[str, Any]:
    """What a .mat file's header says: {version: "5" (MATLAB 5 to 7's layout) or "7.3" (HDF5), created: when MATLAB
    wrote the file (its "Created on", by that computer's clock; None when it doesn't say)}."""
    p, head = _open(path)
    created = None
    m = re.search(rb"Created on:\s*\w{3}\s+(\w{3})\s+(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})\s+(\d{4})", head[:116])
    if m and m.group(1).decode("ascii", "replace") in MONTHS:
        try:
            created = datetime(int(m.group(6)), MONTHS[m.group(1).decode("ascii")], int(m.group(2)),
                               int(m.group(3)), int(m.group(4)), int(m.group(5)))
        except ValueError:
            created = None
    if _hdf5(head):
        return {"version": "7.3", "created": created}
    _order(head, p.name)
    return {"version": "5", "created": created}


# ------------------------------------------------------------------ reading

def load(path: Any, names: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """A .mat file's variables, {name: value} in the file's order (names: only these; the others aren't decoded).
    DataError: not a .mat file Kelvin reads, damaged, or past Kelvin's bounds, saying which in words."""
    p, head = _open(path)
    wanted = set(names) if names is not None else None
    if _hdf5(head):
        return _H5(p.name).load(p, wanted)
    r = _Reader(p.name, _order(head, p.name))
    try:
        with open(p, "rb") as f:
            return {name: value for name, value in r.variables(f, wanted)}
    except DataError:
        raise
    except UNFORESEEN as e:   # damage no check above foresaw: still said in words, never a bare error
        raise r.damaged(_first(e))


def whos(path: Any) -> List[Dict[str, Any]]:
    """A .mat file's variables without their values, as MATLAB's whos lists them: [{name, class, size}] (class:
    double, char, cell, struct, logical, sparse, function_handle, or an object's class; size: its dimensions). Only
    each variable's first bytes are read (and decompressed)."""
    p, head = _open(path)
    if _hdf5(head):
        return _H5(p.name).whos(p)
    r = _Reader(p.name, _order(head, p.name))
    try:
        with open(p, "rb") as f:
            return list(r.heads(f))
    except DataError:
        raise
    except UNFORESEEN as e:
        raise r.damaged(_first(e))


@dataclass
class _Head:
    """A variable's array flags, class, dimensions, name (and an object's class name), and where its values start."""
    flags: int
    cls: int
    nzmax: int
    dims: List[int]
    name: str
    classname: str
    system: str
    pos: int

    def kind(self) -> str:
        """Its class, as MATLAB's whos names it."""
        if self.cls in (OBJECT, OPAQUE):
            return self.classname or "object"
        if self.flags & LOGICAL:
            return "logical"
        return NUMBERS[self.cls][0] if self.cls in NUMBERS else CLASSES.get(self.cls, "unknown")


class _Cut(DataError):
    """An element runs past the bytes read: the file is cut short, or only its first bytes were decompressed."""


class _Reader:
    """A MATLAB 5 file's bytes made values, within the bounds above."""

    def __init__(self, name: str, order: str):
        self.name = name
        self.order = order        # "<" or ">", as the header says
        self.unpacked = 0         # bytes decompressed
        self.made = 0             # bytes of values made
        self.items = 0            # cells' and structs' elements made

    def damaged(self, why: str) -> DataError:
        return DataError("%s is damaged, or isn't a .mat file as MATLAB writes them (%s)" % (self.name, why))

    def cut(self, why: str) -> DataError:
        return _Cut("%s is cut short (%s)" % (self.name, why))

    def too_big(self, why: str) -> DataError:
        return DataError("%s holds more than Kelvin reads: %s" % (self.name, why))

    # -------------------------------------------------- the file's variables, one after another

    def _elements(self, f: BinaryIO) -> Iterator[Tuple[int, int, int]]:
        """Each of the file's top-level elements: its type, where its data start and how long they are. Nothing
        pads them (inside a variable, every element is padded to 8 bytes)."""
        f.seek(0, 2)
        size = f.tell()
        pos = 128
        while pos < size:
            f.seek(pos)
            tag = f.read(8)
            if len(tag) < 8:
                if not any(tag):
                    return   # zeros after the last variable
                raise self.cut("an element's tag")
            kind, length = struct.unpack(self.order + "II", tag)
            if kind >> 16:
                raise self.damaged("a variable in the small-element form")
            if length > size - pos - 8:
                raise self.cut("a variable runs past the end of the file")
            yield kind, pos + 8, length
            pos += 8 + length

    def _variable(self, f: BinaryIO, kind: int, start: int, length: int, peek: bool) -> Tuple[bytes, int, int]:
        """A top-level element's variable: (the bytes it is in, where its miMATRIX's contents start and end). peek:
        only its first bytes (decompressed, for a compressed one), enough for its name, class and size."""
        f.seek(start)
        if kind == MATRIX:   # an uncompressed variable: the element is its contents
            body = f.read(min(length, PEEK) if peek else length)
            return body, 0, len(body)
        if kind != COMPRESSED:
            raise self.damaged("an element of type %d where a variable should be" % kind)
        raw = f.read(min(length, PEEK) if peek else length)
        body = self._peek(raw) if peek else self.inflate(raw)
        if len(body) < 8:
            raise self.cut("a compressed variable without its contents")
        inner, size = struct.unpack_from(self.order + "II", body, 0)
        if inner != MATRIX:
            raise self.damaged("a compressed element that holds no variable")
        if not peek and size > len(body) - 8:
            raise self.cut("a compressed variable is shorter than it says")
        return body, 8, min(8 + size, len(body))

    def variables(self, f: BinaryIO, wanted: Optional[Set[str]]) -> Iterator[Tuple[str, Any]]:
        for kind, start, length in self._elements(f):
            if wanted is not None:   # its name first, from its first bytes: a variable not asked for isn't decoded
                try:
                    buf, at, end = self._variable(f, kind, start, length, peek=True)
                    name = self.head(buf, at, end).name
                except _Cut:
                    name = None   # a name past the first bytes: decoded whole below
                if name is not None and name not in wanted:
                    continue
            buf, at, end = self._variable(f, kind, start, length, peek=False)
            h = self.head(buf, at, end)
            if not h.name or (wanted is not None and h.name not in wanted):
                continue   # MATLAB's own subsystem (a function handle's workspace, objects' data): never decoded
            yield h.name, self.value_of(h, buf, end, 0)

    def heads(self, f: BinaryIO) -> Iterator[Dict[str, Any]]:
        for kind, start, length in self._elements(f):
            try:
                buf, at, end = self._variable(f, kind, start, length, peek=True)
                h = self.head(buf, at, end)
            except _Cut:
                buf, at, end = self._variable(f, kind, start, length, peek=False)
                h = self.head(buf, at, end)
            if h.name:
                yield {"name": h.name, "class": h.kind(), "size": tuple(h.dims)}

    def _peek(self, raw: bytes) -> bytes:
        """The first PEEK bytes of a compressed variable (a damaged stream reads as nothing here, and is refused when
        it is read whole)."""
        try:
            return zlib.decompressobj().decompress(raw, PEEK)
        except zlib.error:
            return b""

    def inflate(self, raw: bytes) -> bytes:
        """A compressed variable, decompressed: never past MAX_UNPACKED for the whole file (a small file can unpack
        to gigabytes). A stream without zlib's end is taken when it holds the whole variable (MATLAB has written
        such files); one that stops inside the variable is refused as cut short (_variable)."""
        room = MAX_UNPACKED - self.unpacked
        d = zlib.decompressobj()
        try:
            out = d.decompress(raw, room + 1)
        except zlib.error as e:
            raise self.damaged("its compressed data: %s" % _first(e))
        if len(out) > room or d.unconsumed_tail:
            raise self.too_big("its variables decompress to more than %d MB" % (MAX_UNPACKED // 2 ** 20))
        self.unpacked += len(out)
        return out

    # -------------------------------------------------- data elements

    def tag(self, buf: bytes, pos: int, end: int) -> Tuple[int, int, int, int]:
        """The data element at pos, inside a variable: (its type, where its data start, how long they are, where the
        next element starts: its data padded to 8 bytes)."""
        if end - pos < 8:
            raise self.cut("an element's tag")
        word, size = struct.unpack_from(self.order + "II", buf, pos)
        if word >> 16:   # the small-element form: the length in the word's upper half, the type in its lower
            kind, size = word & 0xFFFF, word >> 16
            if size > 4:
                raise self.damaged("a small element of %d bytes" % size)
            return kind, pos + 4, size, pos + 8
        start = pos + 8
        if size > end - start:
            raise self.cut("an element runs past what holds it")
        return word, start, size, min(end, start + -(-size // 8) * 8)

    def word(self, buf: bytes, pos: int, end: int) -> Tuple[str, int]:
        """A name (an miINT8 element: a variable's, a class's): its text, and where the next element starts."""
        kind, start, size, pos = self.tag(buf, pos, end)
        if kind not in (INT8, UINT8, UTF8):
            raise self.damaged("a name stored as type %d" % kind)
        if size > 4096:
            raise self.damaged("a name of %d characters" % size)
        return bytes(buf[start:start + size]).split(b"\x00")[0].decode("utf-8", "replace"), pos

    def head(self, buf: bytes, pos: int, end: int) -> _Head:
        """The start of an miMATRIX's contents: its array flags, dimensions and name (an object's class name too)."""
        kind, start, size, pos = self.tag(buf, pos, end)
        if kind != UINT32 or size < 8:
            raise self.damaged("a variable without its array flags")
        flags, nzmax = struct.unpack_from(self.order + "II", buf, start)
        cls = flags & 0xFF
        if cls == OPAQUE:   # no dimensions: its name, the form MATLAB saved it in, its class's name
            name, pos = self.word(buf, pos, end)
            system, pos = self.word(buf, pos, end)
            classname, pos = self.word(buf, pos, end)
            return _Head(flags, cls, nzmax, [1, 1], name, classname, system, pos)
        kind, start, size, pos = self.tag(buf, pos, end)
        if kind not in (INT32, UINT32) or size % 4:
            raise self.damaged("a variable without its dimensions")
        dims = list(struct.unpack_from(self.order + "%di" % (size // 4), buf, start))
        if not 2 <= len(dims) <= MAX_DIMS or min(dims) < 0:
            raise self.damaged("a variable of %d dimensions, or one less than zero" % len(dims))
        name, pos = self.word(buf, pos, end)
        classname = ""
        if cls == OBJECT:
            classname, pos = self.word(buf, pos, end)
        return _Head(flags, cls, nzmax, dims, name, classname, "", pos)

    def numbers_in(self, buf: bytes, kind: int, start: int, size: int, n: Optional[int]) -> Any:
        """An element's numbers as stored (n: how many there must be; None: as many as it holds)."""
        np = _np()
        code = STORED.get(kind)
        if code is None:
            raise self.damaged("numbers stored as type %d" % kind)
        dtype = np.dtype(self.order + code)
        if n is None:
            if size % dtype.itemsize:
                raise self.damaged("numbers that don't fill their element")
            n = size // dtype.itemsize
        elif size != n * dtype.itemsize:
            raise self.damaged("a value's numbers don't fill its dimensions")
        return np.frombuffer(buf, dtype, n, start)

    def make(self, nbytes: int) -> None:
        """Count the bytes of the values made: never past MAX_UNPACKED (numbers stored as bytes grow eightfold as
        doubles)."""
        self.made += nbytes
        if self.made > MAX_UNPACKED:
            raise self.too_big("its values come to more than %d MB" % (MAX_UNPACKED // 2 ** 20))

    def count(self, n: int, need: int, room: int) -> None:
        """A cell's or struct's elements: need of them must fit in room bytes (each at least a tag), and the file's
        never more than MAX_ITEMS."""
        if need > room // 8:
            raise self.damaged("a cell or struct with more elements than its bytes hold")
        self.items += n
        if self.items > MAX_ITEMS:
            raise self.too_big("more than %d cells' and structs' elements" % MAX_ITEMS)

    # -------------------------------------------------- values

    def value(self, buf: bytes, pos: int, end: int, depth: int) -> Tuple[Any, int]:
        """The miMATRIX element at pos (a cell's element, a struct's field): its value, and where the next starts."""
        kind, start, size, nxt = self.tag(buf, pos, end)
        if kind != MATRIX:
            raise self.damaged("an element of type %d where a value should be" % kind)
        if not size:   # an empty value: MATLAB writes its tag alone
            return _np().zeros((0, 0)), nxt
        stop = start + size
        return self.value_of(self.head(buf, start, stop), buf, stop, depth), nxt

    def value_of(self, h: _Head, buf: bytes, end: int, depth: int) -> Any:
        if depth > MAX_DEPTH:
            raise self.too_big("cells and structs more than %d deep" % MAX_DEPTH)
        if h.cls in NUMBERS:
            return self.numbers(h, buf, h.pos, end)
        if h.cls == CHAR:
            return self.chars(h, buf, h.pos, end)
        if h.cls == CELL:
            return self.cells(h, buf, h.pos, end, depth)
        if h.cls in (STRUCT, OBJECT):
            return self.structs(h, buf, h.pos, end, depth)
        if h.cls == SPARSE:
            return self.sparse(h, buf, h.pos, end)
        if h.cls == FUNCTION:
            inside, _ = self.value(buf, h.pos, end, depth + 1)
            return function_of(inside)
        if h.cls == OPAQUE:   # its data (a reference into MATLAB's subsystem) are never decoded
            o = Opaque(h.classname or "object")
            o.system = h.system
            return o
        raise self.damaged("a value of class %d, which MATLAB doesn't write" % h.cls)

    def numbers(self, h: _Head, buf: bytes, pos: int, end: int) -> Any:
        np = _np()
        n = math.prod(h.dims)
        kind, start, size, pos = self.tag(buf, pos, end)
        real = self.numbers_in(buf, kind, start, size, n)
        target = np.dtype(bool if h.flags & LOGICAL else NUMBERS[h.cls][1])
        if h.flags & COMPLEX:
            kind, start, size, pos = self.tag(buf, pos, end)
            imag = self.numbers_in(buf, kind, start, size, n)
            target = np.dtype("c8" if NUMBERS[h.cls][1] == "f4" else "c16")
            self.make(n * target.itemsize)
            out = np.empty(n, target)
            out.real, out.imag = real, imag
        else:
            self.make(n * target.itemsize)
            out = real.astype(target)
        return out.reshape(h.dims, order="F")

    def chars(self, h: _Head, buf: bytes, pos: int, end: int) -> Any:
        """A char array: a str (one row), or a list of str (its rows, unpadded as cellstr makes them; a page after
        another for more dimensions)."""
        np = _np()
        n = math.prod(h.dims)
        kind, start, size, pos = self.tag(buf, pos, end)
        raw = bytes(buf[start:start + size])
        self.make(4 * n)
        if kind == UTF8:
            text = raw.decode("utf-8", "replace")
            units = np.frombuffer(text.encode("utf-16-le"), "<u2")
            if units.size != n and h.dims[0] == 1:
                return text
        elif kind in (UINT16, UTF16, INT16):
            units = self.numbers_in(buf, UINT16, start, size, None)
        elif kind in (INT8, UINT8):
            units = np.frombuffer(raw, "u1")
        elif kind in (UTF32, INT32, UINT32):
            units = self.numbers_in(buf, UINT32, start, size, None)
        else:
            raise self.damaged("characters stored as type %d" % kind)
        if units.size != n:
            raise self.damaged("characters that don't fill their dimensions")
        if not n:
            return ""
        wide = kind in (UTF32, INT32, UINT32)
        grid = units.astype("<u4" if wide else "<u2").reshape(h.dims, order="F")
        pages = grid.reshape(h.dims[0], h.dims[1], -1, order="F")
        rows = [pages[i, :, k] for k in range(pages.shape[2]) for i in range(h.dims[0])]
        texts = [r.tobytes().decode("utf-32-le" if wide else "utf-16-le", "replace") for r in rows]
        return texts[0] if len(texts) == 1 else [t.rstrip(" ") for t in texts]   # rows unpadded, as cellstr

    def cells(self, h: _Head, buf: bytes, pos: int, end: int, depth: int) -> Any:
        np = _np()
        n = math.prod(h.dims)
        self.count(n, n, end - pos)
        out = np.empty(n, dtype=object)
        for k in range(n):
            out[k], pos = self.value(buf, pos, end, depth + 1)
        return out.reshape(h.dims, order="F")

    def structs(self, h: _Head, buf: bytes, pos: int, end: int, depth: int) -> Any:
        """A struct, a struct array or an object: its field names (each in a fixed width), then each element's
        fields in turn."""
        np = _np()
        kind, start, size, pos = self.tag(buf, pos, end)
        if kind not in (INT32, UINT32) or size != 4:
            raise self.damaged("a struct without the length of its field names")
        width = struct.unpack_from(self.order + "i", buf, start)[0]
        kind, start, size, pos = self.tag(buf, pos, end)
        if kind not in (INT8, UINT8, UTF8):
            raise self.damaged("a struct without its field names")
        if size and (width <= 0 or size % width):
            raise self.damaged("a struct's field names don't fit their width")
        names = [bytes(buf[start + k * width:start + (k + 1) * width]).split(b"\x00")[0].decode("utf-8", "replace")
                 for k in range(size // width if size else 0)]
        n = math.prod(h.dims)
        self.count(n, n * len(names), end - pos)
        elements = []
        for _ in range(n):
            s = Struct()
            s.classname = h.classname
            for field in names:
                s[field], pos = self.value(buf, pos, end, depth + 1)
            elements.append(s)
        if n == 1 and all(d == 1 for d in h.dims):
            return elements[0]
        out = np.empty(n, dtype=object)
        for k, s in enumerate(elements):
            out[k] = s
        return out.reshape(h.dims, order="F")

    def sparse(self, h: _Head, buf: bytes, pos: int, end: int) -> Any:
        """A sparse matrix: its values' rows (ir), where each column's values start (jc), the values; dense when
        small enough."""
        np = _np()
        if len(h.dims) != 2:
            raise self.damaged("a sparse matrix of more than two dimensions")
        m, n = h.dims
        kind, start, size, pos = self.tag(buf, pos, end)
        ir = self.numbers_in(buf, kind, start, size, None).astype(np.int64)
        kind, start, size, pos = self.tag(buf, pos, end)
        jc = self.numbers_in(buf, kind, start, size, None).astype(np.int64)
        if len(jc) != n + 1 or jc[0] != 0 or np.any(np.diff(jc) < 0) or jc[-1] > len(ir):
            raise self.damaged("a sparse matrix whose columns don't add up")
        nnz = int(jc[-1])
        if pos < end:
            kind, start, size, pos = self.tag(buf, pos, end)
            values = self.numbers_in(buf, kind, start, size, None)
        else:   # a logical one may be written without its values: each is true
            values = np.ones(nnz, dtype=np.uint8)
        if len(values) < nnz or np.any(ir[:nnz] < 0) or np.any(ir[:nnz] >= m):
            raise self.damaged("a sparse matrix whose values don't fit it")
        if h.flags & COMPLEX and pos < end:
            kind, start, size, pos = self.tag(buf, pos, end)
            imag = self.numbers_in(buf, kind, start, size, None)
            if len(imag) < nnz:
                raise self.damaged("a sparse matrix whose values don't fit it")
            values = values[:nnz] + 1j * imag[:nnz]
        if m * n > DENSE:
            s = Sparse("a sparse matrix of %dx%d holding %d values, too big for Kelvin to make whole" % (m, n, nnz))
            s.shape, s.nonzero = (m, n), nnz
            return s
        dtype = bool if h.flags & LOGICAL else (np.complex128 if np.iscomplexobj(values) else np.float64)
        self.make(m * n * np.dtype(dtype).itemsize)
        out = np.zeros((m, n), dtype=dtype)
        out[ir[:nnz], np.repeat(np.arange(n), np.diff(jc))] = values[:nnz]
        return out


def function_of(inside: Any) -> Function:
    """A function handle from what MATLAB saves of it: a struct whose function_handle says its function (an
    anonymous one's text after MATLAB's mark of its workspace, "sf%0") and its type."""
    fh = inside.get("function_handle") if isinstance(inside, dict) else None
    text = fh.get("function") if isinstance(fh, dict) else ""
    kind = fh.get("type") if isinstance(fh, dict) else ""
    text = re.sub(r"^sf%\d+(?=@)", "", text if isinstance(text, str) else "").strip()
    if text and not text.startswith("@"):
        text = "@" + text
    f = Function(text)
    f.kind = kind if isinstance(kind, str) else ""
    return f


# ------------------------------------------------------------------ MATLAB 7.3: HDF5

def _attr(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if hasattr(value, "tolist"):
        value = value.tolist()
        if isinstance(value, list):
            return "".join(_attr(v) for v in value)
    return str(value)


class _H5:
    """A MATLAB 7.3 file's variables, read with h5py as MATLAB lays them out, within the same bounds."""

    def __init__(self, name: str):
        self.name = name
        self.made = 0
        self.items = 0

    def _h5py(self) -> Any:
        try:
            import h5py
        except ImportError:
            raise DataError("%s is a MATLAB 7.3 file (HDF5 inside), which Kelvin reads with h5py, and h5py isn't on "
                            "this computer: save it in MATLAB with save -v7, and import that" % self.name)
        return h5py

    def _file(self, p: Path) -> Any:
        h5py = self._h5py()
        try:
            return h5py.File(str(p), "r")
        except (OSError, ValueError) as e:
            raise DataError("%s could not be read as a MATLAB 7.3 file (HDF5): %s" % (self.name, _first(e)))

    def load(self, p: Path, wanted: Optional[Set[str]]) -> Dict[str, Any]:
        with self._file(p) as f:
            try:
                return {name: self.value(f, f[name], 0) for name in f
                        if not name.startswith("#") and (wanted is None or name in wanted)}
            except DataError:
                raise
            except UNFORESEEN + (OSError, RuntimeError) as e:
                raise self.damaged(e)

    def damaged(self, e: BaseException) -> DataError:
        return DataError("%s is damaged, or isn't a MATLAB 7.3 file as MATLAB writes them (%s)" % (
            self.name, _first(e)))

    def whos(self, p: Path) -> List[Dict[str, Any]]:
        out = []
        with self._file(p) as f:
            try:
                for name in f:
                    if name.startswith("#"):   # #refs# (what cells point to), #subsystem# (MATLAB's own)
                        continue
                    item = f[name]
                    kind = _attr(item.attrs.get("MATLAB_class")) or "unknown"
                    if hasattr(item, "shape"):
                        size = tuple(int(d) for d in _np().asarray(item[()]).reshape(-1)) \
                            if item.attrs.get("MATLAB_empty") else tuple(int(d) for d in reversed(item.shape))
                    else:
                        size = (1, 1)
                    out.append({"name": name, "class": kind, "size": size})
            except UNFORESEEN + (OSError, RuntimeError) as e:
                raise self.damaged(e)
        return out

    def too_big(self, why: str) -> DataError:
        return DataError("%s holds more than Kelvin reads: %s" % (self.name, why))

    def take(self, item: Any) -> Any:
        """A dataset's values, counted against MAX_UNPACKED before they are read."""
        self.made += int(item.size) * item.dtype.itemsize
        if self.made > MAX_UNPACKED:
            raise self.too_big("its values come to more than %d MB" % (MAX_UNPACKED // 2 ** 20))
        return item[()]

    def value(self, f: Any, item: Any, depth: int) -> Any:
        np = _np()
        h5py = self._h5py()
        if depth > MAX_DEPTH:
            raise self.too_big("cells and structs more than %d deep" % MAX_DEPTH)
        cls = _attr(item.attrs.get("MATLAB_class"))
        if isinstance(item, h5py.Group):
            if "MATLAB_sparse" in item.attrs:
                return self.sparse(item, cls)
            if cls == "function_handle":
                return function_of(self.struct(f, item, depth))
            if cls in ("struct", ""):
                return self.struct(f, item, depth)
            o = Opaque(cls)
            o.system = "MCOS"
            return o
        if item.attrs.get("MATLAB_empty"):   # an empty value: its data are its dimensions
            dims = [int(d) for d in np.asarray(item[()]).reshape(-1)] or [0, 0]
            if cls == "char":
                return ""
            if cls in ("cell", "struct"):
                return np.empty(dims, dtype=object)
            return np.zeros(dims, dtype=bool if cls == "logical" else
                            dict(NUMBERS.values()).get(cls, "f8"))
        if cls == "cell":
            refs = np.asarray(self.take(item)).T
            self.items += refs.size
            if self.items > MAX_ITEMS:
                raise self.too_big("more than %d cells' and structs' elements" % MAX_ITEMS)
            out = np.empty(refs.size, dtype=object)
            for k, ref in enumerate(refs.reshape(-1, order="F")):
                out[k] = self.value(f, f[ref], depth + 1)
            return out.reshape(refs.shape, order="F")
        if cls == "char":
            units = np.asarray(self.take(item)).T
            if units.ndim < 2:
                units = units.reshape(1, -1)
            rows = units.astype("<u2").reshape(units.shape[0], -1, order="F")
            texts = [r.tobytes().decode("utf-16-le", "replace") for r in rows]
            return texts[0] if len(texts) == 1 else [t.rstrip(" ") for t in texts]
        known = dict(NUMBERS.values())
        if cls in known or cls == "logical":
            a = np.asarray(self.take(item))
            if a.dtype.names and "real" in a.dtype.names and "imag" in a.dtype.names:
                a = a["real"] + 1j * a["imag"]
            elif cls == "logical":
                a = a.astype(bool)
            else:
                a = a.astype(known[cls])
            return a.T if a.ndim >= 2 else a.reshape(1, -1) if a.ndim == 1 else a.reshape(1, 1)
        if not cls and item.dtype.kind in "biuf":   # numbers another program wrote: as it laid them out
            a = np.asarray(self.take(item))
            return a if a.ndim >= 2 else a.reshape(1, -1) if a.ndim == 1 else a.reshape(1, 1)
        o = Opaque(cls or "object")   # an object MATLAB saved in its own form (MATLAB_object_decode)
        o.system = "MCOS"
        return o

    def struct(self, f: Any, group: Any, depth: int) -> Any:
        """A struct: a group of its fields (MATLAB_fields says their order); a struct array's fields are each an
        array of references, one per element."""
        np = _np()
        h5py = self._h5py()
        listed = group.attrs.get("MATLAB_fields")
        names = [_attr(n) for n in listed] if listed is not None else []
        names = [n for n in names if n in group] + [n for n in group if n not in names]
        refs = {n: group[n] for n in names if isinstance(group[n], h5py.Dataset) and
                group[n].dtype == h5py.ref_dtype and "MATLAB_class" not in group[n].attrs}
        if names and len(refs) == len(names):   # a struct array
            shape = np.asarray(refs[names[0]]).T.shape
            n = int(np.prod(shape))
            self.items += n * (len(names) + 1)
            if self.items > MAX_ITEMS:
                raise self.too_big("more than %d cells' and structs' elements" % MAX_ITEMS)
            columns = {k: np.asarray(self.take(v)).T.reshape(-1, order="F") for k, v in refs.items()}
            out = np.empty(n, dtype=object)
            for i in range(n):
                s = Struct()
                for k in names:
                    s[k] = self.value(f, f[columns[k][i]], depth + 1)
                out[i] = s
            return out.reshape(shape, order="F") if shape != (1, 1) else out[0]
        s = Struct()
        for k in names:
            s[k] = self.value(f, group[k], depth + 1)
        return s

    def sparse(self, group: Any, cls: str) -> Any:
        np = _np()
        m = int(np.asarray(group.attrs["MATLAB_sparse"]).reshape(-1)[0])
        jc = np.asarray(self.take(group["jc"])).astype(np.int64).reshape(-1)
        n = len(jc) - 1
        nnz = int(jc[-1]) if n >= 0 else 0
        ir = np.asarray(self.take(group["ir"])).astype(np.int64).reshape(-1) if "ir" in group else np.zeros(0, int)
        data = np.asarray(self.take(group["data"])).reshape(-1) if "data" in group else np.ones(nnz)
        if data.dtype.names and "real" in data.dtype.names:
            data = data["real"] + 1j * data["imag"]
        if n < 0 or len(ir) < nnz or len(data) < nnz or np.any(np.diff(jc) < 0) or np.any(ir[:nnz] >= m):
            raise DataError("%s is damaged (a sparse matrix whose columns don't add up)" % self.name)
        if m * n > DENSE:
            s = Sparse("a sparse matrix of %dx%d holding %d values, too big for Kelvin to make whole" % (m, n, nnz))
            s.shape, s.nonzero = (m, n), nnz
            return s
        out = np.zeros((m, n), dtype=bool if cls == "logical" else data.dtype if np.iscomplexobj(data) else float)
        out[ir[:nnz], np.repeat(np.arange(n), np.diff(jc))] = data[:nnz]
        return out


# ------------------------------------------------------------------ plain Python

def plain(value: Any) -> Any:
    """A value as plain Python, for JSON or YAML: numbers (one element: an int, float, bool or complex; more: a list,
    a matrix as a list of its rows, more dimensions nested), text (str, or a list of str for rows), a struct as a dict
    (an object's class under "__class__"), a struct array or a cell as a list (a matrix of them as rows). Function,
    Opaque and Sparse stay as they are: each is a str."""
    np = _np()
    if isinstance(value, Struct):
        out = {"__class__": value.classname} if value.classname else {}
        out.update((k, plain(v)) for k, v in value.items())
        return out
    if isinstance(value, (str, bool, int, float, complex)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, np.ndarray):
        if value.dtype == object:
            return _nested(np.squeeze(value)) if value.size else []
        if value.size == 1:
            return value.reshape(-1)[0].item()
        return np.squeeze(value).tolist() if value.size else []
    if isinstance(value, np.generic):
        return value.item()
    return value


def _nested(a: Any) -> List[Any]:
    """A squeezed array of objects as lists: a vector flat, a matrix as its rows."""
    if a.ndim == 0:
        return [plain(a.item())]
    if a.ndim == 1:
        return [plain(v) for v in a]
    return [_nested(row) for row in a]
