"""Set up's LabVIEW review, read and kept: the VIs a person keeps are the very ones the survey showed them, by the
paths it gave them (a VI library's member never stands in for a file of the same name)."""
import shutil

import pytest

from labharness import labview

pytest.importorskip("mcp")

from test_scan_library import virtual  # noqa: E402
from test_vi_reader import AIM, rsrc  # noqa: E402


def test_keep_takes_the_vi_the_review_showed_never_a_library_member_named_like_it(tmp_path, monkeypatch):
    """An .llb member named ../Main.vi resolves to the folder's own Main.vi: keeping Main.vi kept the member's
    translation instead."""
    monkeypatch.setattr(labview, "VISACONF", tmp_path / "no NI-VISA" / "visaconf.ini")
    lab = tmp_path / "lab"
    lab.mkdir()
    shutil.copy(AIM / "set_v.vi", lab / "Main.vi")   # it writes a voltage
    (lab / "x.llb").write_bytes(rsrc([(b"UCRF", [(0, "../Main.vi", (AIM / "measure_v.vi").read_bytes())])],
                                     kind=b"LVAR"))   # it reads one
    rt = virtual(tmp_path, [])
    shown = labview.survey(str(lab), rt.script_cards())
    main = next(s for s in shown.vis if s["name"] == "Main.vi")
    out = rt.labview_scans(str(lab), [main["path"]], [])
    assert out["kept"] == [] and [x["path"] for x in out["not"]] == [main["path"]]
    assert out["not"][0]["gaps"][0].startswith("VISA write to ?"), out   # Main.vi's own diagram, translated
