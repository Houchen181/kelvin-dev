"""Data Kelvin didn't measure becomes a run (issue #6): another program's tables and LightField's .spe spectra, read as
numbers only; files on the same x values side by side in one run, so compute can compare or subtract them."""
import struct

import pytest

from labharness import imports
from labharness.imports import DataError


def spe(path, frames, wavelengths=None, height=2):
    """A LightField SPE 3.0 file: uint16 frames, and an XML footer with the layout and the wavelength calibration."""
    width = len(frames[0][0])
    head = bytearray(4100)
    struct.pack_into("<H", head, 42, width)
    struct.pack_into("<h", head, 108, 3)
    struct.pack_into("<H", head, 656, height)
    struct.pack_into("<i", head, 1446, len(frames))
    struct.pack_into("<f", head, 1992, 3.0)
    data = b"".join(struct.pack("<%dH" % (width * height), *[v for row in f for v in row]) for f in frames)
    struct.pack_into("<Q", head, 678, 4100 + len(data))
    size = width * height * 2
    calib = ('<Calibrations><WavelengthMapping><Wavelength xml:space="preserve">%s</Wavelength></WavelengthMapping>'
             '<SensorMapping id="1" x="0" y="0" width="%d" height="%d" xBinning="1" yBinning="1"/></Calibrations>'
             % (",".join("%g" % w for w in wavelengths), width, height)) if wavelengths else ""
    footer = ('<SpeFormat version="3.0" xmlns="http://www.princetoninstruments.com/spe/2009"><DataFormat>'
              '<DataBlock type="Frame" version="3.0" count="%d" pixelFormat="MonochromeUnsigned16" size="%d" '
              'stride="%d"><DataBlock type="Region" version="3.0" width="%d" height="%d" size="%d" stride="%d"/>'
              '</DataBlock></DataFormat>%s</SpeFormat>' % (len(frames), size, size, width, height, size, size, calib))
    path.write_bytes(bytes(head) + data + footer.encode("utf-8"))
    return path


def test_tables_of_numbers_come_in_with_their_names_and_units(tmp_path):
    (tmp_path / "iv.csv").write_text("# sweep of the gate\nV_g (V),I [nA],note\n0,1.5,\n0.5,2.5,\nbad,row,\n1,3.5,\n",
                                     encoding="utf-8")
    t = imports.read(tmp_path / "iv.csv")
    assert (t.x_key, t.x_unit, t.x) == ("v_g", "V", [0.0, 0.5, 1.0])
    assert t.columns["iv.i"] == ("nA", [1.5, 2.5, 3.5]) and "1 rows that weren't numbers" in t.notes[0]
    (tmp_path / "pl.txt").write_text("910.0 12\n910.5 30\n911.0 18\n", encoding="utf-8")   # no header, spaces
    t = imports.read(tmp_path / "pl.txt")
    assert t.x == [910.0, 910.5, 911.0] and t.columns["pl.y1"][1] == [12.0, 30.0, 18.0]
    lf = "Frame,Row,Column,Wavelength,Intensity\n" + "".join(
        "%d,%d,%d,%g,%d\n" % (f, r, c, 900 + c, 10 * f + r + c) for f in (1, 2) for r in (0, 1) for c in (0, 1, 2))
    (tmp_path / "lightfield.csv").write_text(lf, encoding="utf-8")
    t = imports.read(tmp_path / "lightfield.csv")                       # LightField's long export: as it shows it
    assert t.x == [900.0, 901.0, 902.0] and t.columns["lightfield.counts"][1] == [31.0, 33.0, 35.0]
    for bad, why in [("one\n1\n2\n", "one column"), ("words only\n", "no rows of numbers")]:
        (tmp_path / "bad.csv").write_text(bad, encoding="utf-8")
        with pytest.raises(DataError, match=why):
            imports.read(tmp_path / "bad.csv")
    with pytest.raises(DataError, match="Kelvin imports tables"):
        (tmp_path / "x.h5").write_bytes(b"\x89HDF")
        imports.read(tmp_path / "x.h5")


def test_lightfield_spe_spectra_with_their_wavelengths(tmp_path):
    pytest.importorskip("numpy")
    frames = [[[1, 2, 3, 4], [10, 20, 30, 40]], [[3, 4, 5, 6], [12, 22, 32, 42]]]
    t = imports.read(spe(tmp_path / "PL_on_silicon.spe", frames, [910.0, 910.5, 911.0, 911.5]))
    assert t.x_key == "wavelength" and t.x == [910.0, 910.5, 911.0, 911.5]
    assert t.columns["pl_on_silicon.counts"] == ("counts", [13.0, 24.0, 35.0, 46.0])   # rows summed, frames averaged
    assert "2 frames of 4x2, rows summed, frames averaged" in t.notes[-1]
    t = imports.read(spe(tmp_path / "raw.spe", frames[:1]))
    assert t.x_key == "pixel" and "no wavelength calibration" in t.notes[0]


def test_two_spectra_in_one_run_to_subtract(tmp_path):
    pytest.importorskip("mcp")
    pytest.importorskip("numpy")
    from labharness import ScriptedApprover
    from simlab import build_virtual_lab
    from labharness.agent.runtime import LabRuntime
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    wl = [920.0, 920.5, 921.0, 921.5]
    silicon = spe(tmp_path / "003_silicon.spe", [[[100, 100, 100, 100], [0, 0, 0, 0]]], wl)
    sample = spe(tmp_path / "004_sample.spe", [[[100, 160, 400, 100], [0, 0, 0, 0]]], wl)
    rt.attribution = {"folder": str(tmp_path / "PL")}
    out = rt.import_data("%s; %s" % (silicon, sample), "PL D2")
    rt.attribution = {}
    assert out.startswith("imported 003_silicon.spe, 004_sample.spe as run ") and "saved in %s" % (tmp_path / "PL") in out
    run_id = rt.runs_index(1)[0]["run_id"]
    assert "4 points of wavelength (nm), columns f_003_silicon.counts, f_004_sample.counts" in out
    diff = rt.compute(run_id, "f_004_sample.counts - f_003_silicon.counts")
    assert "300" in diff                                                     # the peak above the substrate
    assert "in %s" % (tmp_path / "PL") in rt.describe_run(run_id) and run_id in rt.find_runs()
    other = spe(tmp_path / "other.spe", [[[1, 2, 3], [0, 0, 0]]], [1.0, 2.0, 3.0])
    assert rt.import_data([str(silicon), str(other)]).startswith("not imported: other's x values differ")
    assert rt.import_data(str(tmp_path / "nothere.csv")) == "not imported: there is no file %s" % (
        tmp_path / "nothere.csv")


def test_data_put_in_the_data_folders_runs_and_a_folder_of_it_come_in(tmp_path):
    """Issue #6 again: a collaborator pasted another program's data into the data folder's runs (runs/1/<its
    folder>), and Kelvin refused it as its own folder; naming the folder said "there is no file". What a person put
    there comes in now, a folder as its data files; Kelvin's runs and records there, the rest of Kelvin's folder
    (the person's chats, other people's folders) and files of sign-ins stay out of reach."""
    pytest.importorskip("mcp")
    from labharness import ScriptedApprover
    from simlab import build_virtual_lab
    from labharness.agent.runtime import LabRuntime
    home = tmp_path / "Kelvin"
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), home / "users" / "Richen1" / "data")
    rt.home = home
    table = "Wavelength,Intensity\n" + "".join("%g,%g\n" % (920 + i * 0.01, 100 + i) for i in range(50))
    pasted = rt.data_dir / "runs" / "1" / "#003-PL_Yb920to921_nodope_DeviceD2_400uw_onsilicon-2026_10_25-16_03_38"
    pasted.mkdir(parents=True)
    (pasted / "spectrum.csv").write_text(table, encoding="utf-8")
    (pasted / "silicon.csv").write_text(table.replace(",1", ",2"), encoding="utf-8")
    (pasted / "notes.docx").write_bytes(b"PK")                      # not data: left out of the folder's files
    out = rt.import_data(str(pasted / "spectrum.csv"))
    assert out.startswith("imported spectrum.csv as run "), out
    out = rt.import_data(str(pasted), "PL D2")                      # the folder: its data files side by side
    assert out.startswith("imported silicon.csv, spectrum.csv as run "), out
    mixed = pasted.parent / "#004-PL_DeviceD3"
    mixed.mkdir()
    (mixed / "spectrum.csv").write_text(table, encoding="utf-8")
    (mixed / "wider.csv").write_text(table.replace("920", "930"), encoding="utf-8")
    out = rt.import_data(str(mixed))                                 # not one run: its files named, to pick from
    assert "x values differ" in out and out.endswith("%s holds spectrum.csv, wider.csv" % mixed), out
    kept = rt.data_dir / "runs" / rt.runs_index(1)[0]["run_id"]      # Kelvin's own run, in the same runs folder
    out = rt.import_data(str(pasted.parent))                         # no data files here: the folders to name
    assert "there are no data files" in out and pasted.name in out and kept.name not in out
    assert kept.name not in rt.import_data(str(rt.data_dir / "runs"))
    (rt.data_dir / "runs" / "1" / "queue.json").write_text("{}", encoding="utf-8")
    (rt.data_dir / "reports").mkdir()
    (rt.data_dir / "reports" / "summary.csv").write_text(table, encoding="utf-8")
    (pasted / "credentials.csv").write_text(table, encoding="utf-8")
    for other in (home / "users" / "Ada" / "data" / "runs", home / "users" / "Richen1" / "chats"):
        other.mkdir(parents=True)
        (other / "theirs.csv").write_text(table, encoding="utf-8")
    for path in (kept, kept / "run.h5", rt.data_dir / "events.jsonl", rt.data_dir / "runs" / "1" / "queue.json",
                 rt.data_dir / "reports" / "summary.csv", home / "users" / "Ada" / "data" / "runs" / "theirs.csv",
                 home / "users" / "Richen1" / "chats" / "theirs.csv", home / "settings.json"):
        out = rt.import_data(str(path))
        assert out.startswith("not imported: ") and "Kelvin's own folder" in out, (path, out)
    assert "sign-ins" in rt.import_data(str(pasted / "credentials.csv"))
    assert "sign-ins" in rt.import_data(str(pasted))                 # a folder's files are each checked too
    assert rt.read_examples(str(pasted)).startswith("not read")      # the other readers keep out of it, as before
