"""MATLAB .mat files for the tests, written here as MathWorks documents the format (MAT-File Format, MATLAB 5's layout
as MATLAB 7 writes it): the 128-byte header, then each variable an miMATRIX element, zlib-compressed or not, an element
of four bytes or fewer in the packed small-element form. And Special Measure's run files, laid out as its smrun saves
them (sm_run). Nothing here is copied from MATLAB, Special Measure or SciPy."""
import struct
import zlib

import numpy as np

miINT8, miUINT8, miINT32, miUINT32, miDOUBLE, miMATRIX, miCOMPRESSED, miUTF8, miUINT16 = 1, 2, 5, 6, 9, 14, 15, 16, 4
STORED = {"i1": 1, "u1": 2, "i2": 3, "u2": 4, "i4": 5, "u4": 6, "f4": 7, "f8": 9, "i8": 12, "u8": 13}
CLASS = {"f8": 6, "f4": 7, "i1": 8, "u1": 9, "i2": 10, "u2": 11, "i4": 12, "u4": 13, "i8": 14, "u8": 15}
CELL, STRUCT, OBJECT, CHAR, SPARSE, FUNCTION, OPAQUE = 1, 2, 3, 4, 5, 16, 17
COMPLEX, LOGICAL = 0x800, 0x200


class Cell:
    """A cell of these values (shape: MATLAB's, 1 x n by default)."""
    def __init__(self, items, shape=None):
        self.items, self.shape = list(items), shape or (1, len(items))


class Structs:
    """A struct array: its elements (dicts with the same fields) in MATLAB's order; shape 1 x n by default."""
    def __init__(self, elements, shape=None, fields=None, classname=""):
        self.elements = list(elements)
        self.shape = shape or (1, len(self.elements))
        self.fields = fields or (list(self.elements[0]) if self.elements else [])
        self.classname = classname


class Func:
    """A function handle as MATLAB saves it: an anonymous one's text after its workspace's mark ("sf%0"), a named
    one's name."""
    def __init__(self, text, kind="anonymous"):
        self.text, self.kind = text, kind


class MCOS:
    """An object MATLAB saves in its newer form: its class name and a reference into the file's subsystem."""
    def __init__(self, classname):
        self.classname = classname


class Sparse:
    def __init__(self, dense, shape=None):
        self.dense, self.shape = np.asarray(dense, dtype=float), shape


class Stored:
    """Numbers stored in a smaller type than their class, as MATLAB packs them (doubles of whole values as uint8)."""
    def __init__(self, array, code):
        self.array, self.code = np.asarray(array), code


class Chars:
    """Text stored as UTF-8 (MATLAB 7's choice for some), or as UTF-16 code units (miUINT16), rows of a char matrix."""
    def __init__(self, rows, utf8=False):
        self.rows = [rows] if isinstance(rows, str) else list(rows)
        self.utf8 = utf8


class Bare:
    """An empty value as MATLAB writes one inside a cell or struct: an miMATRIX tag of no bytes."""


def element(kind, data, order="<"):
    """A data element: the small form for four bytes or fewer (the length in the tag word's upper half), else a tag
    and the data padded to 8 bytes."""
    if len(data) <= 4 and kind != miMATRIX:
        return struct.pack(order + "I", (len(data) << 16) | kind) + data.ljust(4, b"\x00")
    return struct.pack(order + "II", kind, len(data)) + data + b"\x00" * (-len(data) % 8)


def _flags(cls, flags=0, nzmax=0, order="<"):
    return element(miUINT32, struct.pack(order + "II", cls | flags, nzmax), order)


def _dims(shape, order):
    return element(miINT32, struct.pack(order + "%di" % len(shape), *shape), order)


def _name(name, order):
    return element(miINT8, name.encode("utf-8"), order)


def _fields(names, order):
    width = max([32] + [len(n) + 1 for n in names])
    return element(miINT32, struct.pack(order + "i", width), order) + \
        element(miINT8, b"".join(n.encode("ascii").ljust(width, b"\x00") for n in names), order)


def _shape(a):
    a = np.asarray(a)
    return a.shape if a.ndim >= 2 else (1, a.size) if a.ndim == 1 else (1, 1)


def matrix(value, name="", order="<"):
    """A value as an miMATRIX element (its name: a variable's, or "" inside a cell or struct)."""
    if isinstance(value, Bare):
        return struct.pack(order + "II", miMATRIX, 0)
    if isinstance(value, (str, Chars)):
        chars = value if isinstance(value, Chars) else Chars(value)
        rows = chars.rows
        width = len(rows[0]) if rows else 0
        grid = np.array([[ord(c) for c in r] for r in rows], dtype=np.uint16).reshape(len(rows), width)
        shape = (len(rows), width) if rows and (width or len(rows) > 1) else (0, 0)
        if chars.utf8:
            text = "".join(chr(c) for c in grid.reshape(-1, order="F"))
            data = element(miUTF8, text.encode("utf-8"), order)
        else:
            data = element(miUINT16, grid.reshape(-1, order="F").astype(order + "u2").tobytes(), order)
        body = _flags(CHAR, order=order) + _dims(shape, order) + _name(name, order) + data
    elif isinstance(value, Cell):
        body = _flags(CELL, order=order) + _dims(value.shape, order) + _name(name, order) + \
            b"".join(matrix(v, "", order) for v in value.items)
    elif isinstance(value, (dict, Structs)):
        s = value if isinstance(value, Structs) else Structs([value], (1, 1))
        cls = OBJECT if s.classname else STRUCT
        body = _flags(cls, order=order) + _dims(s.shape, order) + _name(name, order)
        if s.classname:
            body += _name(s.classname, order)
        body += _fields(s.fields, order) + b"".join(matrix(e[f], "", order) for e in s.elements for f in s.fields)
    elif isinstance(value, Func):
        inside = {"matlabroot": "C:\\Program Files\\MATLAB\\R2024a", "separator": "\\", "sentinel": "@",
                  "function_handle": {"function": ("sf%0" + value.text) if value.kind == "anonymous" else value.text,
                                      "type": value.kind, "file": "", "workspace": MCOS("function_handle_workspace"),
                                      "within_file_path": "__base_function"}}
        body = _flags(FUNCTION, order=order) + _dims((1, 1), order) + _name(name, order) + matrix(inside, "", order)
    elif isinstance(value, MCOS):   # no dimensions: its name, the form, the class, then a reference into the subsystem
        body = _flags(OPAQUE, order=order) + _name(name, order) + _name("MCOS", order) + \
            _name(value.classname, order) + matrix(np.array([[3707764736], [2], [1], [1], [1], [1]], np.uint32), "",
                                                   order)
    elif isinstance(value, Sparse):
        dense = value.dense
        m, n = value.shape or dense.shape
        cols, rows = np.nonzero(dense.T)   # column by column, as MATLAB keeps them
        jc =np.concatenate([[0], np.cumsum(np.bincount(cols, minlength=n))]).astype(np.int32)
        values = dense[rows, cols]
        nzmax = max(1, len(values))
        body = _flags(SPARSE, nzmax=nzmax, order=order) + _dims((m, n), order) + _name(name, order) + \
            element(miINT32, rows.astype(order + "i4").tobytes(), order) + \
            element(miINT32, jc.astype(order + "i4").tobytes(), order) + \
            element(miDOUBLE, values.astype(order + "f8").tobytes(), order)
    else:
        stored = value if isinstance(value, Stored) else None
        a = np.asarray(stored.array if stored else value)
        flags = 0
        if a.dtype == bool:
            a, flags = a.astype(np.uint8), LOGICAL
        if a.dtype.kind == "c":
            flags |= COMPLEX
        code = a.real.dtype.str[1:]
        cls = CLASS[code]
        put = stored.code if stored else code
        flat = a.reshape(-1, order="F")
        body = _flags(cls, flags, order=order) + _dims(_shape(a), order) + _name(name, order) + \
            element(STORED[put], flat.real.astype(order + put).tobytes(), order)
        if flags & COMPLEX:
            body += element(STORED[put], flat.imag.astype(order + put).tobytes(), order)
    return struct.pack(order + "II", miMATRIX, len(body)) + body


def header(order="<", created="Mon Nov  2 10:00:00 2026", text=None):
    words = text if text is not None else ("MATLAB 5.0 MAT-file, Platform: PCWIN64, Created on: %s" % created)
    return words.encode("ascii").ljust(116, b" ") + b"\x00" * 8 + struct.pack(order + "H", 0x0100) + \
        (b"IM" if order == "<" else b"MI")


def mat(variables, compress=True, order="<", created="Mon Nov  2 10:00:00 2026"):
    """A whole .mat file: its header, then each variable (compressed, as MATLAB 7 saves them, or not, as -v6)."""
    out = header(order, created)
    for name, value in variables.items():
        el = matrix(value, name, order)
        if compress:
            z = zlib.compress(el)
            out += struct.pack(order + "II", miCOMPRESSED, len(z)) + z
        else:
            out += el
    return out


def write(path, variables, **kw):
    path.write_bytes(mat(variables, **kw))
    return path


# ------------------------------------------------------------------ Special Measure's runs

EMPTY = np.zeros((0, 0))


def loop(npoints=None, rng=(0.0, 1.0), setchan=(), getchan=(), ranges=None, waittime=0.0, startwait=0.0,
         ramptime=None, trafofn=None):
    """One of a scan's loops, with the fields this lab's scans carry."""
    return {"npoints": EMPTY if npoints is None else float(npoints),
            "rng": EMPTY if rng is None else np.array([list(rng)], dtype=float),
            "getchan": Cell(list(getchan)),
            "setchan": setchan if isinstance(setchan, str) else Cell(list(setchan)),
            "setchanranges": Cell([np.array([list(r)], dtype=float) for r in ranges]) if ranges else Cell([]),
            "ramptime": EMPTY if ramptime is None else np.array([ramptime], dtype=float).reshape(1, -1),
            "trafofn": trafofn if trafofn is not None else EMPTY,
            "trigfn": EMPTY, "numchans": float(len(getchan)), "waittime": float(waittime),
            "startwait": float(startwait)}


def sm_run(loops, data, consts=(), comments=("cooled down",), configch=(), configvals=(), name="scan",
           escapefn="@(x)smset({'Vtg','Vbg'},[0,0])", with_data=True, trafofn=None):
    """A run file's variables as smrun saves them. loops: loop() dicts, innermost first; data: one array per channel
    read, loop 1's first, each [npoints(end) ... npoints(i)] (+ the reading's own dimensions). trafofn: the scan's
    own (every loop's values pass through it)."""
    width = max([len(c) for c in comments] + [0])
    scan = {"loops": Structs(loops),
            "saveloop": np.array([[1.0, 1.0]]),
            "disp": Structs([{"loop": 1.0, "channel": 1.0, "dim": 1.0}]),
            "trafofn": EMPTY if trafofn is None else trafofn,
            "consts": Structs([{"setchan": c, "val": float(v), "set": float(s)} for c, v, s in consts],
                              fields=["setchan", "val", "set"]) if consts else
            Structs([], (0, 0), fields=["setchan", "val", "set"]),
            "comments": Chars([c.ljust(width) for c in comments]) if comments else "",
            "name": name,
            "escapefn": {"fn": Func(escapefn), "args": Cell([], (0, 0))}}
    out = {"configvals": np.array([list(configvals)], dtype=float) if configvals else EMPTY,
           "configdata": Cell([], (1, 0)), "scan": scan,
           "configch": Cell(list(configch)) if configch else Cell([], (0, 0))}
    if with_data:
        out["data"] = Cell(list(data))
    return out
