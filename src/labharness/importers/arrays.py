"""NumPy's files (.npy, .npz) as data: a column of numbers, a table, a camera frame or a stack of frames.

Loaded with allow_pickle=False, so a file can't run code as it loads (an array of Python objects is refused). How an
array is read follows its shape, as people save them:
- 1-D: one column of numbers, on its index;
- 2-D, narrow one way (at most TABLE_COLUMNS): a table, a column per row or column of the narrow side; the first column
  is x when it runs one way (as np.column_stack([x, y]) saves it), else every column is a value on the index;
- 2-D, wide both ways: a camera frame; 3-D: a stack of frames (or one colour frame, rows x columns x 3);
  4-D: a stack of colour frames;
- a record array (named fields): a table with the fields' names;
- .npz: its 1-D arrays of one length side by side, or its one array read as above."""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any, Dict, List

from ..imports import DataError, MAX_ROWS, Table, column_key, counts_rows, frames_table, key_of

TABLE_COLUMNS = 16
MAX_NPZ = 512 * 2 ** 20     # bytes of a .npz's arrays, unpacked


def read_npy(p: Path) -> Table:
    import numpy as np
    try:
        a = np.load(str(p), allow_pickle=False)
    except ValueError as e:
        if "pickle" in str(e).lower():
            raise DataError("%s holds Python objects, not numbers (Kelvin never loads those)" % p.name)
        raise DataError("%s is not a NumPy array file (%s)" % (p.name, e))
    return from_array(key_of(p.stem, "data"), p.name, a, np)


def read_npz(p: Path) -> Table:
    import numpy as np
    try:
        with zipfile.ZipFile(str(p)) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_NPZ:
                raise DataError("%s holds more than %d MB of arrays" % (p.name, MAX_NPZ // 2 ** 20))
        with np.load(str(p), allow_pickle=False) as f:
            arrays = {name: f[name] for name in f.files}
    except ValueError as e:
        if "pickle" in str(e).lower():
            raise DataError("%s holds Python objects, not numbers (Kelvin never loads those)" % p.name)
        raise DataError("%s is not a NumPy .npz file (%s)" % (p.name, e))
    except (zipfile.BadZipFile, OSError) as e:
        raise DataError("%s is not a NumPy .npz file (%s)" % (p.name, e))
    stem = key_of(p.stem, "data")
    numeric = {k: a for k, a in arrays.items() if a.dtype.kind in "biuf" and a.ndim >= 1 and a.size}
    if len(numeric) == 1:
        (name, a), = numeric.items()
        return from_array(stem, p.name, a, np)
    lengths: Dict[int, List[str]] = {}
    for k, a in numeric.items():
        if a.ndim == 1:
            lengths.setdefault(len(a), []).append(k)
    if not lengths:
        raise DataError("%s holds no column of numbers (its arrays: %s)" % (
            p.name, ", ".join(key_of(k, "array") for k in arrays) or "none"))
    n, keys = max(lengths.items(), key=lambda kv: (len(kv[1]), kv[0]))
    if len(keys) < 2:
        raise DataError("%s: no two of its arrays are columns of one length; save x and y as 1-D arrays of the same "
                        "length" % p.name)
    if n > MAX_ROWS:
        raise DataError("%s has more than %d rows" % (p.name, MAX_ROWS))
    columns = list(keys)
    if len(keys) > 2 and counts_rows([float(v) for v in numeric[keys[0]]], keys[0]):   # the rows' own count
        keys = keys[1:]
        if keys[0] == "t" and len(keys) > 2:   # Kelvin's own export: i, t, then the swept axes
            keys = [keys[1], keys[0]] + keys[2:]
    x = keys[0]
    table = Table(stem, column_key(x, "x"), "", [float(v) for v in numeric[x]])
    for k in keys[1:]:
        table.columns[_unique(table, "%s.%s" % (stem, column_key(k, "y")))] = ("", [float(v) for v in numeric[k]])
    left = [key_of(k, "array") for k in arrays if k not in columns]
    if left:
        table.notes.append("%s: arrays left out (not columns of %d numbers): %s" % (p.name, n, ", ".join(left)))
    return table


def _unique(table: Table, key: str) -> str:
    name, k = key, 2
    while name in table.columns or name == table.x_key:
        name, k = "%s_%d" % (key, k), k + 1
    return name


def from_array(stem: str, filename: str, a: Any, np: Any) -> Table:
    if a.dtype.names:
        return _records(stem, filename, a, np)
    if a.dtype.kind == "c" and a.ndim == 1:   # a complex series: its real and imaginary parts
        a = np.stack([a.real, a.imag], axis=1)
        return _table(stem, filename, a, np, names=["re", "im"], index=True)
    if a.dtype.kind not in "biuf":
        raise DataError("%s holds %s, not numbers Kelvin reads" % (filename, {
            "c": "complex numbers in more than one dimension", "U": "text", "S": "text", "M": "dates",
            "m": "time spans", "O": "Python objects", "V": "raw bytes"}.get(a.dtype.kind, "values of another kind")))
    if a.dtype.kind == "b":
        a = a.astype(np.uint8)
    if a.ndim == 0 or not a.size:
        raise DataError("%s holds %s" % (filename, "one number" if a.ndim == 0 else "no numbers"))
    if a.ndim == 1:
        return _table(stem, filename, a[:, None], np, names=["value"], index=True)
    if a.ndim == 2:
        n, m = a.shape
        if min(n, m) <= TABLE_COLUMNS and max(n, m) > min(n, m):
            return _table(stem, filename, a if m <= n else a.T, np)
        return frames_table(stem, filename, [a], "a NumPy array")
    if a.ndim == 3:
        if a.shape[2] in (3, 4) and min(a.shape[:2]) > 4:   # rows x columns x colours: one picture
            return frames_table(stem, filename, [a[:, :, :3]], "a NumPy array")
        return frames_table(stem, filename, list(a), "a NumPy array of %d frames" % a.shape[0])
    if a.ndim == 4 and a.shape[3] in (3, 4):
        return frames_table(stem, filename, list(a[:, :, :, :3]), "a NumPy array of %d colour frames" % a.shape[0])
    raise DataError("%s: an array of shape %s, which Kelvin doesn't read as a table or as frames" % (
        filename, "x".join(str(d) for d in a.shape)))


def _table(stem: str, filename: str, a: Any, np: Any, names: Any = None, index: bool = False) -> Table:
    """Columns of a points x columns array. x: the first column when it runs one way, else the index."""
    n, m = a.shape
    if n > MAX_ROWS:
        raise DataError("%s has more than %d rows" % (filename, MAX_ROWS))
    values = a.astype(float)
    first = values[:, 0]
    runs = n > 1 and m > 1 and not index and bool(np.all(np.isfinite(first))) and (
        bool(np.all(np.diff(first) > 0)) or bool(np.all(np.diff(first) < 0)))
    names = list(names or ["col%d" % k for k in range(m)])
    if runs:
        table = Table(stem, names[0], "", [float(v) for v in first])
        cols = range(1, m)
    else:
        table = Table(stem, "index", "", [float(i) for i in range(n)])
        cols = range(m)
    for k in cols:
        table.columns["%s.%s" % (stem, names[k])] = ("", [float(v) for v in values[:, k]])
    table.notes.append("%s: a NumPy array of %s%s" % (filename, "x".join(str(d) for d in a.shape) if m > 1 else n,
                                                     "; x is its first column" if runs else ""))
    return table


def _records(stem: str, filename: str, a: Any, np: Any) -> Table:
    """A record array: each numeric field a column (x the first), the others left out."""
    a = a.reshape(-1)
    if len(a) > MAX_ROWS:
        raise DataError("%s has more than %d rows" % (filename, MAX_ROWS))
    fields = [f for f in a.dtype.names if a.dtype[f].kind in "biuf" and not a.dtype[f].shape]
    if len(fields) < 2:
        raise DataError("%s: a record array with %s numeric field%s; Kelvin needs x and at least one value" % (
            filename, len(fields) or "no", "" if len(fields) == 1 else "s"))
    table = Table(stem, column_key(fields[0], "x"), "", [float(v) for v in a[fields[0]]])
    for f in fields[1:]:
        table.columns[_unique(table, "%s.%s" % (stem, column_key(f, "y")))] = ("", [float(v) for v in a[f]])
    left = [key_of(f, "field") for f in a.dtype.names if f not in fields]
    if left:
        table.notes.append("%s: fields that aren't numbers left out: %s" % (filename, ", ".join(left)))
    return table
