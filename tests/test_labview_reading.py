"""Set up's LabVIEW review, read and kept: a LabVIEW folder is read one at a time, on a thread of its own (never one
of the server's shared threads, which a Stop's work waits for), whoever asks (Set up, its Keep, survey_labview); and the
VIs a person keeps are the very ones the survey showed them, by the paths it gave them (a VI library's member never
stands in for a file of the same name)."""
import shutil
import threading
import time

import pytest

from labharness import labview

pytest.importorskip("mcp")
pytest.importorskip("starlette")

from test_scan_library import portal, virtual  # noqa: E402
from test_vi_reader import AIM, rsrc  # noqa: E402


def test_one_labview_folder_is_read_at_a_time_and_another_is_refused_meanwhile(tmp_path, tmp_path_factory,
                                                                                monkeypatch):
    import anyio
    from mcp import Client
    from labharness.agent.runtime import Underway
    from labharness.agent.server import build_server
    rt = virtual(tmp_path, [])
    client, _ = portal(rt, tmp_path)
    release, reading = threading.Event(), []
    survey = labview.survey

    def slow(folder, *args, **kwargs):   # a big folder, read for a while
        reading.append(threading.current_thread().name)
        release.wait(30)
        return survey(folder, *args, **kwargs)
    monkeypatch.setattr(labview, "survey", slow)
    monkeypatch.setattr(labview, "VISACONF", tmp_path / "no NI-VISA" / "visaconf.ini")
    lab = tmp_path_factory.mktemp("lab")   # outside Kelvin's own folders, which survey_labview never reads
    shutil.copy(AIM / "set_v.vi", lab / "Main.vi")
    body = {"folder": str(lab)}
    with client:
        out = {}
        first = threading.Thread(target=lambda: out.update(r=client.post("/api/instruments/labview", json=body)))
        first.start()
        t0 = time.time()
        while not reading and time.time() - t0 < 10:
            time.sleep(0.02)
        try:
            assert reading == ["kelvin-labview_0"]                     # its own thread, not a shared one
            busy = client.post("/api/instruments/labview", json=body)
            assert busy.status_code == 409 and busy.json()["error"] == ("Kelvin is reading a LabVIEW folder already: "
                                                                        "try again once that is done.")
            keep = client.post("/api/instruments/labview/scans", json=dict(body, vis=[str(lab / "Main.vi")]))
            assert keep.status_code == 409
            with pytest.raises(Underway):
                rt.survey_labview(str(lab))

            async def tool():
                async with Client(build_server(rt)) as mcp_client:
                    return (await mcp_client.call_tool("survey_labview", {"path": str(lab)})).content[0].text
            said = anyio.run(tool)
            assert said == "not read now: Kelvin is reading a LabVIEW folder already: try again once that is done."
        finally:
            release.set()
            first.join(30)
        assert out["r"].status_code == 200 and [v["name"] for v in out["r"].json()["vis"]] == ["Main.vi"]
        assert len(reading) == 1                                          # the refused ones never read
        assert "Main.vi" in rt.survey_labview(str(lab))                   # and once it is done, the next one reads


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
