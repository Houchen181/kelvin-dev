"""Kelvin's own reader of MATLAB's .mat files (matfile.py, no SciPy): numbers in MATLAB's shapes and classes, text,
cells, structs, objects, function handles and placeholders for MCOS objects, compressed or not, in either byte order;
MATLAB 7.3's HDF5 files through h5py; and files damaged or built to swell are refused, naming the file. The files are
written in tests/matfiles.py as MathWorks documents the format."""
import json
import struct
import sys
import zlib
from datetime import datetime

import pytest

np = pytest.importorskip("numpy")

import matfiles as M  # noqa: E402
from labharness import imports, matfile  # noqa: E402
from labharness.imports import DataError  # noqa: E402


def test_numbers_keep_matlabs_shape_class_and_order(tmp_path):
    a = np.arange(6, dtype=float).reshape(2, 3)                          # MATLAB's [0 1 2; 3 4 5]
    stack = np.arange(24).reshape(2, 3, 4).astype(np.int16)
    variables = {"a": a, "single": np.float32([[1.5, -2.25]]), "stack": stack, "i8": np.int8([[-128, 127]]),
                 "u64": np.uint64([[2 ** 64 - 1]]), "flag": np.array([[True, False, True]]),
                 "z": np.array([[1 + 2j, -3j]]), "empty": M.EMPTY, "packed": M.Stored([[1.0, 200.0]], "u1"),
                 "x": 2.5}
    for compress in (True, False):                                       # MATLAB 7's, and -v6's
        for order in ("<", ">"):                                         # either byte order
            v = matfile.load(M.write(tmp_path / "n.mat", variables, compress=compress, order=order))
            assert list(v) == list(variables)                            # the file's order
            assert v["a"].dtype == np.float64 and v["a"].shape == (2, 3) and v["a"][1, 0] == 3.0
            assert np.array_equal(v["a"], a) and np.array_equal(v["stack"], stack) and v["stack"].dtype == np.int16
            assert v["single"].dtype == np.float32 and v["single"][0, 1] == -2.25
            assert v["i8"].tolist() == [[-128, 127]] and v["u64"][0, 0] == 2 ** 64 - 1
            assert v["flag"].dtype == bool and v["flag"].tolist() == [[True, False, True]]
            assert v["z"].dtype == np.complex128 and v["z"][0, 1] == -3j
            assert v["empty"].shape == (0, 0)
            assert v["packed"].dtype == np.float64 and v["packed"].tolist() == [[1.0, 200.0]]   # stored as uint8
            assert v["x"].shape == (1, 1) and v["x"][0, 0] == 2.5


def test_text_cells_structs_and_objects(tmp_path):
    path = M.write(tmp_path / "t.mat", {
        "one": "Vbg (V)", "rows": M.Chars(["ab", "cd"]), "utf8": M.Chars("µm Ω", utf8=True), "blank": "",
        "cell": M.Cell([1.0, "two", M.Cell([np.array([[3.0, 4.0]])])]),
        "grid": M.Cell(["a", "b", "c", "d"], (2, 2)),
        "s": {"x": 1.0, "name": "gate", "nested": {"deep": np.array([[1.0, 2.0]])}},
        "arr": M.Structs([{"k": 1.0}, {"k": 2.0}]), "none": M.Structs([], (0, 0), fields=["k"]),
        "obj": M.Structs([{"v": 5.0}], (1, 1), classname="oldclass"),
        "bare": M.Cell([M.Bare(), 1.0])})
    v = matfile.load(path)
    assert v["one"] == "Vbg (V)" and v["rows"] == ["ab", "cd"] and v["utf8"] == "µm Ω" and v["blank"] == ""
    cell = v["cell"]
    assert cell.shape == (1, 3) and cell[0, 1] == "two" and cell[0, 2][0, 0][0, 1] == 4.0
    assert v["grid"][1, 0] == "b" and v["grid"][0, 1] == "c"            # a cell's elements column by column
    s = v["s"]
    assert isinstance(s, matfile.Struct) and list(s) == ["x", "name", "nested"] and s["name"] == "gate"
    assert s["nested"]["deep"].tolist() == [[1.0, 2.0]] and s.classname == ""
    assert v["arr"].shape == (1, 2) and v["arr"][0, 1]["k"][0, 0] == 2.0
    assert v["none"].shape == (0, 0)
    assert v["obj"].classname == "oldclass" and v["obj"]["v"][0, 0] == 5.0
    assert v["bare"][0, 0].shape == (0, 0) and v["bare"][0, 1][0, 0] == 1.0   # an empty value written as a bare tag


def test_function_handles_objects_and_sparse_matrices(tmp_path):
    dense = np.array([[0, 2.0, 0], [1.0, 0, 3.0]])
    path = M.write(tmp_path / "f.mat", {
        "esc": {"fn": M.Func("@(x)smset({'Vtg','Vbg'},[0,0])"), "args": M.Cell([], (0, 0))},
        "named": M.Func("smset", "simple"), "when": M.MCOS("datetime"),
        "sp": M.Sparse(dense), "huge": M.Sparse(np.zeros((1, 1)), shape=(100000, 100000))})
    v = matfile.load(path)
    fn = v["esc"]["fn"]
    assert isinstance(fn, matfile.Function) and fn == "@(x)smset({'Vtg','Vbg'},[0,0])" and fn.kind == "anonymous"
    assert v["named"] == "@smset" and v["named"].kind == "simple"     # MATLAB's "sf%0" mark is gone
    assert isinstance(v["when"], matfile.Opaque) and v["when"] == "datetime" and v["when"].system == "MCOS"
    assert np.array_equal(v["sp"], dense)                               # a small sparse matrix, dense
    huge = v["huge"]
    assert isinstance(huge, matfile.Sparse) and huge.shape == (100000, 100000) and "100000x100000" in huge
    assert matfile.whos(path) == [{"name": "esc", "class": "struct", "size": (1, 1)},
                                  {"name": "named", "class": "function_handle", "size": (1, 1)},
                                  {"name": "when", "class": "datetime", "size": (1, 1)},
                                  {"name": "sp", "class": "sparse", "size": (2, 3)},
                                  {"name": "huge", "class": "sparse", "size": (100000, 100000)}]


def test_names_whos_header_and_plain_python(tmp_path):
    rng = np.random.default_rng(1)
    path = M.write(tmp_path / "w.mat", {"big": rng.random((200, 500)), "b": "text", "c": M.Cell([1.0])},
                   created="Tue Dec 15 14:56:30 2015")
    assert list(matfile.load(path, ["b"])) == ["b"]
    assert matfile.header(path) == {"version": "5", "created": datetime(2015, 12, 15, 14, 56, 30)}
    raw = bytearray(path.read_bytes())
    raw[150000] ^= 0xFF                                                  # damage deep inside big's compressed data
    path.write_bytes(bytes(raw))
    assert matfile.whos(path) == [{"name": "big", "class": "double", "size": (200, 500)},   # only first bytes read
                                  {"name": "b", "class": "char", "size": (1, 4)},
                                  {"name": "c", "class": "cell", "size": (1, 1)}]
    assert matfile.load(path, ["b", "c"])["b"] == "text"                # the others aren't decoded
    with pytest.raises(DataError, match=r"^w\.mat is damaged"):
        matfile.load(path)
    s = matfile.load(M.write(tmp_path / "p.mat", {"s": {
        "n": 101.0, "rng": np.array([[0.0, 1.0]]), "names": M.Cell(["Vtg", "Vbg"]),
        "m": np.array([[1.0, 2.0], [3.0, 4.0]]), "loops": M.Structs([{"k": 1.0}, {"k": 2.0}]),
        "fn": M.Func("smset", "simple"), "e": M.EMPTY, "rows": M.Chars(["ab", "cd"]), "i": np.int32([[7]]),
        "obj": M.Structs([{"v": 1.0}], (1, 1), classname="c")}}))["s"]
    p = matfile.plain(s)
    assert p == {"n": 101.0, "rng": [0.0, 1.0], "names": ["Vtg", "Vbg"], "m": [[1.0, 2.0], [3.0, 4.0]],
                 "loops": [{"k": 1.0}, {"k": 2.0}], "fn": "@smset", "e": [], "rows": ["ab", "cd"], "i": 7,
                 "obj": {"__class__": "c", "v": 1.0}}
    assert isinstance(p["i"], int) and isinstance(p["fn"], matfile.Function)
    assert json.loads(json.dumps(p)) == p                               # plain Python: JSON as it is


def test_damaged_files_are_refused_naming_the_file(tmp_path, monkeypatch):
    good = M.mat({"a": np.arange(1000.0).reshape(10, 100), "b": "words"})
    plain = M.mat({"a": np.arange(1000.0).reshape(10, 100), "b": "words"}, compress=False)
    for data in (good, plain):
        for cut in (100, 140, len(data) // 2, len(data) - 3):
            (tmp_path / "cut.mat").write_bytes(data[:cut])
            with pytest.raises(DataError, match=r"^cut\.mat (is cut short|is too short)"):
                matfile.load(tmp_path / "cut.mat")
    with pytest.raises(DataError, match=r"cut\.mat is cut short"):      # imports.read says so too
        imports.read(tmp_path / "cut.mat")
    (tmp_path / "notes.mat").write_text("%%\nload('C:\\Special Measure\\run.mat')\n" * 20, encoding="utf-8")
    with pytest.raises(DataError, match="notes.mat is not a MATLAB .mat file Kelvin reads"):
        imports.read(tmp_path / "notes.mat")

    def one(body, name="bad.mat"):
        (tmp_path / name).write_bytes(M.header() + struct.pack("<II", M.miMATRIX, len(body)) + body)
        return tmp_path / name
    short = M._flags(6) + M._dims((10, 10), "<") + M._name("a", "<") + M.element(M.miDOUBLE, struct.pack("<d", 1))
    with pytest.raises(DataError, match="bad.mat is damaged.*don't fill its dimensions"):
        matfile.load(one(short))
    past = M._flags(6) + M._dims((1, 1), "<") + M._name("a", "<") + struct.pack("<II", M.miDOUBLE, 1000)
    with pytest.raises(DataError, match="bad.mat is cut short"):
        matfile.load(one(past))
    many = M._flags(M.CELL) + M._dims((1, 10 ** 9), "<") + M._name("c", "<")   # a billion elements, in no bytes
    with pytest.raises(DataError, match="more elements than its bytes hold"):
        matfile.load(one(many))
    deep = 1.0
    for _ in range(40):
        deep = M.Cell([deep])
    with pytest.raises(DataError, match="more than 32 deep"):
        matfile.load(M.write(tmp_path / "deep.mat", {"deep": deep}))
    monkeypatch.setattr(matfile, "MAX_ITEMS", 10)
    with pytest.raises(DataError, match="more than 10 cells' and structs' elements"):
        matfile.load(M.write(tmp_path / "items.mat", {"c": M.Cell([1.0] * 20)}))


def test_a_file_built_to_swell_is_refused(tmp_path, monkeypatch):
    """A decompression bomb: a few kilobytes that unpack to megabytes. The bound is made small here so the test
    holds no big buffer; MATLAB's own files are bounded the same way at MAX_UNPACKED."""
    monkeypatch.setattr(matfile, "MAX_UNPACKED", 2 ** 20)
    bomb = M.write(tmp_path / "bomb.mat", {"z": np.zeros((1, 2 ** 18))})   # 2 MB of zeros
    assert bomb.stat().st_size < 20000
    with pytest.raises(DataError, match="bomb.mat holds more than Kelvin reads: its variables decompress to more"):
        matfile.load(bomb)
    packed = M.write(tmp_path / "packed.mat", {"z": M.Stored(np.zeros((1, 200000)), "u1")})   # 200 kB, 1.6 MB made
    with pytest.raises(DataError, match="packed.mat holds more than Kelvin reads: its values come to more than"):
        matfile.load(packed)
    monkeypatch.setattr(matfile, "MAX_BYTES", 100)
    with pytest.raises(DataError, match="packed.mat is bigger than Kelvin imports"):
        matfile.load(packed)


def test_a_zlib_stream_without_its_end_is_read_when_it_holds_the_whole_variable(tmp_path):
    """MATLAB has written compressed variables whose zlib stream stops before its end mark: all the variable's bytes
    are there, so it is read; one that stops inside the variable is cut short."""
    el = M.matrix(np.arange(100.0).reshape(1, 100), "a")
    z = zlib.compressobj()
    whole = z.compress(el) + z.flush(zlib.Z_SYNC_FLUSH)                  # every byte out, no end of stream
    (tmp_path / "open.mat").write_bytes(M.header() + struct.pack("<II", M.miCOMPRESSED, len(whole)) + whole)
    assert matfile.load(tmp_path / "open.mat")["a"][0, 99] == 99.0
    half = zlib.compress(el)[:len(whole) // 2]
    (tmp_path / "half.mat").write_bytes(M.header() + struct.pack("<II", M.miCOMPRESSED, len(half)) + half)
    with pytest.raises(DataError, match="half.mat is cut short"):
        matfile.load(tmp_path / "half.mat")


def matlab_73(path, build):
    """An HDF5 file as MATLAB 7.3 writes one: MATLAB's header in its first 512 bytes, each variable a dataset or
    group with its MATLAB_class, numbers stored transposed (MATLAB's column-major order), text as UTF-16."""
    h5py = pytest.importorskip("h5py")
    with h5py.File(str(path), "w", userblock_size=512) as f:
        build(f, h5py)
    head = M.header(text="MATLAB 7.3 MAT-file, Platform: PCWIN64, Created on: Mon Nov  2 10:00:00 2026 HDF5 schema "
                         "1.00 .")
    with open(path, "r+b") as f:
        f.write(head[:124] + struct.pack("<H", 0x0200) + b"IM")
    return path


def _put(group, name, data, cls, **attrs):
    ds = group.create_dataset(name, data=data)
    ds.attrs["MATLAB_class"] = np.bytes_(cls)
    for k, v in attrs.items():
        ds.attrs[k] = v
    return ds


def test_matlab_7_3_files_through_h5py(tmp_path, monkeypatch):
    def build(f, h5py):
        _put(f, "a", np.arange(6.0).reshape(2, 3).T, "double")
        _put(f, "t", np.array([[ord(c)] for c in "Vbg"], dtype=np.uint16), "char", MATLAB_int_decode=2)
        _put(f, "flag", np.array([[1], [0]], dtype=np.uint8), "logical")
        _put(f, "e", np.array([0, 0], dtype=np.uint64), "double", MATLAB_empty=1)
        refs = f.create_group("#refs#")
        one = _put(refs, "a", np.array([[1.0, 2.0]]).T, "double")
        two = _put(refs, "b", np.array([[ord(c)] for c in "hi"], dtype=np.uint16), "char")
        _put(f, "c", np.array([[one.ref], [two.ref]], dtype=h5py.ref_dtype), "cell")   # a 1x2 cell, stored 2x1
        s = f.create_group("s")
        s.attrs["MATLAB_class"] = np.bytes_("struct")
        _put(s, "x", np.array([[5.0]]), "double")
        k1, k2 = _put(refs, "k1", np.array([[1.0]]), "double"), _put(refs, "k2", np.array([[2.0]]), "double")
        sa = f.create_group("sa")
        sa.attrs["MATLAB_class"] = np.bytes_("struct")
        sa.create_dataset("k", data=np.array([[k1.ref], [k2.ref]], dtype=h5py.ref_dtype))   # a 1x2 struct array
        obj = _put(f, "when", np.array([[1], [2]], dtype=np.uint32), "datetime", MATLAB_object_decode=3)
        assert obj is not None
    path = matlab_73(tmp_path / "v73.mat", build)
    head = path.read_bytes()[:520]
    assert matfile.is_mat(head) and imports.takes("attached", head)
    v = matfile.load(path)
    assert v["a"].tolist() == [[0.0, 1.0, 2.0], [3.0, 4.0, 5.0]] and v["t"] == "Vbg"
    assert v["flag"].dtype == bool and v["flag"].tolist() == [[True, False]] and v["e"].shape == (0, 0)
    assert v["c"].shape == (1, 2) and v["c"][0, 0].tolist() == [[1.0, 2.0]] and v["c"][0, 1] == "hi"
    assert v["s"]["x"][0, 0] == 5.0
    assert v["sa"].shape == (1, 2) and v["sa"][0, 1]["k"][0, 0] == 2.0
    assert isinstance(v["when"], matfile.Opaque) and v["when"] == "datetime"
    assert matfile.header(path)["version"] == "7.3"
    assert {w["name"]: w["size"] for w in matfile.whos(path)}["a"] == (2, 3)
    monkeypatch.setitem(sys.modules, "h5py", None)                     # no h5py on this computer
    with pytest.raises(DataError, match="v73.mat is a MATLAB 7.3 file \\(HDF5 inside\\), which Kelvin reads with h5py"):
        matfile.load(path)
