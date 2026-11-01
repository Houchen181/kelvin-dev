"""NI's TDMS data files (what LabVIEW saves measurements in) become runs (importers/tdms.py), read with npTDMS: a
reader Kelvin installs on demand, and never ships. Without it, the import says so in the words that let
Kelvin offer the install (envs.missing: install_for_instrument, the drivers environment)."""
import builtins

import pytest

from labharness import envs, imports
from labharness.imports import DataError


def without_nptdms(monkeypatch):
    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "nptdms" or name.startswith("nptdms."):
            raise ImportError("No module named 'nptdms'")
        return real(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", refuse)


def test_a_tdms_file_s_channels_become_a_run(tmp_path):
    np = pytest.importorskip("numpy")
    nptdms = pytest.importorskip("nptdms")
    v = np.linspace(0.0, 1.0, 11)
    with nptdms.TdmsWriter(str(tmp_path / "iv.tdms")) as w:
        w.write_segment([nptdms.GroupObject("Measurement"),
                         nptdms.ChannelObject("Measurement", "Voltage", v, properties={"unit_string": "V"}),
                         nptdms.ChannelObject("Measurement", "Current", v * 1e-3, properties={"unit_string": "A"}),
                         nptdms.ChannelObject("Measurement", "Note", np.array(["x"] * 11)),
                         nptdms.ChannelObject("Settings", "Gain", np.array([2.0]))])
    t = imports.read(tmp_path / "iv.tdms")
    assert (t.x_key, t.x_unit, len(t.x)) == ("voltage", "V", 11)
    assert t.columns["iv.current"][0] == "A" and t.columns["iv.current"][1][10] == pytest.approx(1e-3)
    assert any("other groups left out: Settings" in n for n in t.notes)
    assert any("text channels left out: Measurement/Note" in n for n in t.notes)
    with nptdms.TdmsWriter(str(tmp_path / "ai.tdms")) as w:   # waveforms: their own time is x
        w.write_segment([nptdms.ChannelObject("Waves", "ai0", np.sin(np.arange(50) / 5.0),
                                              properties={"wf_increment": 0.002, "wf_start_offset": 0.0})])
    t = imports.read(tmp_path / "ai.tdms")
    assert (t.x_key, t.x_unit, t.x[1]) == ("time", "s", pytest.approx(0.002)) and list(t.columns) == ["ai.ai0"]
    times = np.array(["2026-11-01T10:00:00", "2026-11-01T10:00:02"], dtype="datetime64[ns]")
    with nptdms.TdmsWriter(str(tmp_path / "log.tdms")) as w:   # a logger's clock: seconds from its first time
        w.write_segment([nptdms.ChannelObject("Log", "Time", times),
                         nptdms.ChannelObject("Log", "T", np.array([4.2, 4.3]), properties={"unit_string": "K"})])
    t = imports.read(tmp_path / "log.tdms")
    assert (t.x_key, t.x_unit, t.x) == ("time", "s", [0.0, 2.0]) and t.columns["log.t"] == ("K", [4.2, 4.3])


def test_tdms_files_are_taken_by_name_and_by_their_first_bytes():
    assert ".tdms" in imports.DATA_SUFFIXES and "TDMS" in imports.WHAT
    assert imports.takes("data.tdms", b"") and imports.takes("x", b"TDSm\x00\x00")


def test_without_nptdms_a_tdms_file_says_how_kelvin_installs_it(tmp_path, monkeypatch):
    without_nptdms(monkeypatch)
    (tmp_path / "run.tdms").write_bytes(b"TDSm" + b"\x00" * 24)
    with pytest.raises(DataError) as e:
        imports.read(tmp_path / "run.tdms")
    said = str(e.value)
    assert envs.needed(said) == {"env": envs.DRIVERS, "packages": ["npTDMS"]}   # what an install offer takes
    assert envs.for_people(said) == "run.tdms is an NI TDMS file, which Kelvin reads with npTDMS: npTDMS isn't on " \
                                    "this computer yet."


def test_a_tdms_file_that_isnt_one_is_said(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("nptdms")
    (tmp_path / "broken.tdms").write_bytes(b"TDSm" + bytes(range(200)))
    with pytest.raises(DataError) as e:
        imports.read(tmp_path / "broken.tdms")
    assert "broken.tdms" in str(e.value)


def test_a_tdms_file_too_long_is_refused_before_a_value_is_read(tmp_path, monkeypatch):
    """How many values each channel holds is in the file's metadata: one longer than a run takes (MAX_ROWS) is said
    before any value is read; otherwise only the group taken is read."""
    np = pytest.importorskip("numpy")
    nptdms = pytest.importorskip("nptdms")
    from labharness.importers import tdms
    with nptdms.TdmsWriter(str(tmp_path / "long.tdms")) as w:
        w.write_segment([nptdms.ChannelObject("Long", "a", np.zeros(500)),
                         nptdms.ChannelObject("Long", "b", np.ones(500)),
                         nptdms.ChannelObject("Short", "c", np.arange(3.0))])
    read = []
    real = tdms._column
    monkeypatch.setattr(tdms, "_column", lambda c, np: read.append(c.name) or real(c, np))
    monkeypatch.setattr(tdms, "MAX_ROWS", 100)
    with pytest.raises(DataError, match="more than 100 values"):
        imports.read(tmp_path / "long.tdms")
    assert read == []
    monkeypatch.setattr(tdms, "MAX_ROWS", 1000)
    t = imports.read(tmp_path / "long.tdms")
    assert read == ["a", "b"] and len(t.x) == 500 and any("other groups left out: Short" in n for n in t.notes)
