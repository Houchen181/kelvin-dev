"""A lab that runs LabVIEW switches to Kelvin directly (Set up → From your LabVIEW…): its LabVIEW folder and NI MAX
read (no VI is run), its instruments proposed through the same review as the script survey, each measurement VI read
translated into a scan and kept in the person's library; and Kelvin reading the same in a chat (survey_labview)."""
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")

from test_lab_setup import bench  # noqa: E402,F401  (the fixture: a fake bus with a Keithley 2400 and an SR830)
from test_labview import iv_sweep, lab_folder  # noqa: E402
from test_tool_loading import call, day  # noqa: E402
from test_web import Scripted, client_for  # noqa: E402

from labharness import labview  # noqa: E402
from labharness.plans import load_plan  # noqa: E402


@pytest.fixture
def lab_vis(monkeypatch, tmp_path):
    """No NI-VISA on this computer (the lab's visaconf.ini is in its folder), and a VI reader that reads the I-V
    sweep, as Kelvin's own will: the other VIs stay not read yet."""
    monkeypatch.setattr(labview, "VISACONF", tmp_path / "no NI-VISA" / "visaconf.ini")
    monkeypatch.setattr(labview, "read_vi", lambda p: iv_sweep(p, resource="SMU") if p.name == "IV sweep.vi"
                        else labview.vi(p))


def test_from_your_labview_sets_the_lab_up_and_keeps_its_scans(bench, lab_vis):
    (runtime, settings, store), bus, home = bench
    folder = lab_folder(home)
    client, headers = client_for((runtime, settings, store), {"scripted": Scripted([])})
    with client:
        assert client.post("/api/instruments/labview", json={}, headers=headers).status_code == 400
        assert client.post("/api/instruments/labview", json={"folder": str(home / "nope")},
                           headers=headers).status_code == 400
        view = client.post("/api/instruments/labview", json={"folder": str(folder)}, headers=headers).json()
        assert view["kind"] == "labview" and view["max"]["aliases"]["SMU"] == "GPIB0::24::INSTR"
        found = {i["address"]: i for i in view["instruments"]}
        smu = found["GPIB0::24::INSTR"]
        assert smu["name"] == "smu" and smu["card"]["id"] == "keithley/2400" and "it answered KEITHLEY" in \
            smu["card"]["why"]
        assert smu["card"]["limits"]["V"] == [0.0, 1.0]                     # from the values its VI uses
        assert "Keithley 2400 Series.lvlib" in smu["calls"]                 # its driver: this instrument's, once
        assert not [i for i in view["instruments"] if not i["address"]]
        lockin = found["GPIB0::8::INSTR"]
        assert lockin["card"]["id"] == "srs/sr830" and lockin["name"] == "lockinsr830"   # as NI MAX names it
        assert found["nidaq://Dev1"]["asked"].startswith("not asked: an NI-DAQmx device")
        assert found["TCPIP0::192.168.1.30::7180::SOCKET"]["asked"] == "no answer at its address now"
        assert all("?" in c for d in bus.devices.values() for c in d.sent)   # it only asked who they are

        vis = {v["name"]: v for v in view["vis"]}
        assert vis["Main.vi"]["read"] == "no" and vis["Main.vi"]["unread"] == ["not read yet"]
        assert "scan" not in vis["Main.vi"]
        scan = vis["IV sweep.vi"]["scan"]
        assert scan["summary"] == "smu.V 0 → 1 V, 11 points · smu.I, smu.V_meas"
        assert load_plan(scan["text"]).name == "iv_sweep"
        assert {"vi": "VISA write to SMU: :OUTP OFF", "plan": "after: smu.output: off"} in scan["mapping"]
        assert scan["gaps"] == ["VISA write to SMU: *RST: a reset, which a scan never sends"]

        items = [{"name": smu["name"], "card": smu["card"]["id"], "address": smu["address"],
                  "limits": smu["card"]["limits"], "from": "labview"}]
        text = client.post("/api/instruments/draft", json={"items": items, "base": ""}, headers=headers).json()["yaml"]
        assert "V: [0, 1]" in text and "from your LabVIEW VIs (the values they use)" in text

        chosen = [{"address": i["address"], "name": i["name"], "card": (i["card"] or {}).get("id", "")}
                  for i in view["instruments"]]
        body = {"folder": view["folder"], "vis": [vis["IV sweep.vi"]["path"], str(home / "elsewhere.vi")],
                "instruments": chosen}
        assert client.post("/api/instruments/labview/scans", json=dict(body, vis=[]),
                           headers=headers).status_code == 400
        kept = client.post("/api/instruments/labview/scans", json=body, headers=headers).json()
        assert [k["name"] for k in kept["kept"]] == ["IV sweep"]
        assert kept["not"] == [{"vi": "elsewhere.vi", "path": str(home / "elsewhere.vi"),
                                "gaps": ["not one of the VIs in %s" % folder]}]
        entry = next(e for e in client.get("/api/library", headers=headers).json()["scans"] if e["name"] == "IV sweep")
        assert entry["source"] == "labview" and entry["vi"] == "IV sweep.vi"
        assert entry["path"] == vis["IV sweep.vi"]["path"]
        assert entry["count"] == 0 and entry["summary"] == scan["summary"]
        assert load_plan(entry["text"]).source["sweep"] == {"smu.V": {"from": 0.0, "to": 1.0, "step": 0.1,
                                                                      "settle": 0.2}}
        logged = [e for e in runtime.gate.log.events() if e.get("type") == "library"]
        assert logged[-1]["change"] == "kept from LabVIEW" and logged[-1]["path"] == vis["IV sweep.vi"]["path"]

        assert client.post("/api/lab", json={"yaml": text}, headers=headers).json()["ok"]   # smu in the lab now
        again = client.post("/api/instruments/labview", json={"folder": str(folder)}, headers=headers).json()
        assert {i["address"]: i for i in again["instruments"]}["GPIB0::24::INSTR"]["in_lab"] == "smu"
        scan = {v["name"]: v for v in again["vis"]}["IV sweep.vi"]["scan"]
        assert "smu.V" in load_plan(scan["text"]).source["sweep"]           # by the lab's own name for it


def test_kelvin_reads_the_labview_as_text_from_outside_the_lab(tmp_path, lab_vis):
    folder = lab_folder(tmp_path)
    provider = Scripted([call("survey_labview", path=str(folder)), [{"type": "text", "text": "Read it."}]])
    chat, events = day(tmp_path, provider, ("switch my lab over from LabVIEW",))
    assert "survey_labview" not in provider.tool_names[0] and chat["tools"] == ["survey_labview"]   # a rarer tool
    result = next(e for e in events if e["type"] == "tool_end")["content"]
    assert result.startswith('<outside_text from="the lab\'s LabVIEW files">') and "GPIB0::24::INSTR" in result
    assert "not read yet: Main.vi" in result and "scan iv_sweep: smu.V 0 → 1 V, 11 points" in result
    assert "The scans name them: smu (GPIB0::24::INSTR, keithley/2400)" in result and "Never run a VI" in result
    assert chat["outside"] == "the lab's LabVIEW files"                 # auto mode asks until the person writes


def test_real_vis_are_read_by_kelvins_own_reader_in_set_up(bench, monkeypatch, tmp_path):
    """A lab's folder of real VIs (an Aim-TTi CPX400SP power supply's, tests/fixtures/labview): each read from its
    bytes, the driver its linkage names proposed, and why its scans can't be kept yet said: no address is saved."""
    (runtime, settings, store), bus, home = bench
    monkeypatch.setattr(labview, "VISACONF", tmp_path / "no NI-VISA" / "visaconf.ini")
    folder = Path(__file__).parent / "fixtures" / "labview" / "aim_tti_cpx400sp"
    client, headers = client_for((runtime, settings, store), {"scripted": Scripted([])})
    with client:
        view = client.post("/api/instruments/labview", json={"folder": str(folder)}, headers=headers).json()
        vis = {v["name"]: v for v in view["vis"]}
        assert {(n, v["read"], v["measures"]) for n, v in vis.items()} == {
            ("set_v.vi", "full", True), ("get_set_v.vi", "full", True), ("measure_v.vi", "full", True)}
        assert vis["set_v.vi"]["labview"] == "LabVIEW 2018 (18.0)" and vis["set_v.vi"]["unread"] == []
        driver = next(i for i in view["instruments"] if "Aim-TTi CPX400SP.lvlib" in i["calls"])
        assert driver["address"] == "" and "CPX400SP" in driver["hints"]   # its address is set as the VI runs
        scan = vis["set_v.vi"]["scan"]
        assert scan["gaps"] == ["VISA write to ?: V{Channel} {Voltage}: no instrument of the lab is at ?",
                                "it reads nothing Kelvin can measure, so it is no scan"]
        kept = client.post("/api/instruments/labview/scans", json={"folder": str(folder), "vis": [
            vis["set_v.vi"]["path"]], "instruments": []}, headers=headers).json()
        assert kept["kept"] == [] and kept["not"][0]["gaps"] == scan["gaps"]
