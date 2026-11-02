"""A lab that runs Special Measure switches to Kelvin directly (Set up → From Special Measure…): its setup script's
rack read (no MATLAB is run), its instruments proposed through the same review as the script survey with their
channels' limits and ramp rates, the channels' names kept as the lab's own (names:) for plans, the queue and
set_value; and Kelvin reading the same in a chat (survey_specialmeasure). The rack here is written for the test."""
import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")

from test_lab_setup import bench  # noqa: E402,F401  (the fixture: a fake bus with a Keithley 2400 and an SR830)
from test_web import Scripted, client_for  # noqa: E402

from labharness.discovery import add_names  # noqa: E402

RACK = r"""
GPIB_BOARD = 'ni';
BOARD_NUM = 0;
Use_Lockin = 1;
Use_Source = 1;
Use_Magnet = 0;
smloadinst('test');
if Use_Lockin == 1
    ind = smloadinst('SR830', [], GPIB_BOARD, BOARD_NUM, 8);
    smdata.inst(ind).name = 'LockIn_A';
    smaddchannel('LockIn_A', 'X', 'X_A');
    smaddchannel('LockIn_A', 'VREF', 'Vref_A', [0.004, 1, 0.5, 1]);
end
if Use_Source == 1
    ind = smloadinst('K2400', [], GPIB_BOARD, BOARD_NUM, 24);
    smdata.inst(ind).name = 'Source';
    smaddchannel('Source', 'V', 'Vbg', [-5 5 0.2 1]);
    smaddchannel('Source', 'I', 'Ibg');
    fprintf(smdata.inst(ind).data.inst, 'sense:current:protection 1e-6');
end
if Use_Magnet == 1
    ind = smloadinst('AMI430', [], 'serial', 'COM1');
    smdata.inst(ind).name = 'Magnet';
    smaddchannel('Magnet', 'B', 'B', [-9 9 0.01 1]);
end
"""


def test_from_special_measure_sets_the_lab_up_with_its_channels_names(bench):
    (runtime, settings, store), bus, home = bench
    folder = home / "SpecialMeasure"
    (folder / "setups").mkdir(parents=True)
    (folder / "setups" / "smsetup_rig.m").write_text(RACK, encoding="utf-8")
    client, headers = client_for((runtime, settings, store), {"scripted": Scripted([])})
    with client:
        assert client.post("/api/instruments/specialmeasure", json={}, headers=headers).status_code == 400
        r = client.post("/api/instruments/specialmeasure", json={"folder": str(folder)}, headers=headers)
        assert r.status_code == 200, r.text
        view = r.json()
        assert view["kind"] == "specialmeasure" and view["setup"].replace("\\", "/") == "setups/smsetup_rig.m"
        found = {i["rack_name"]: i for i in view["instruments"]}
        assert list(found) == ["LockIn_A", "Source", "Magnet"]   # the ones it loads first
        lock, src, mag = found["LockIn_A"], found["Source"], found["Magnet"]
        assert src["address"] == "GPIB0::24::INSTR" and src["card"]["id"] == "keithley/2400"
        assert "it answered KEITHLEY" in src["card"]["why"] and src["name"] == "source"
        assert {c["name"]: c["param"] for c in src["channels"]} == {"Vbg": "V", "Ibg": "I"}
        assert src["card"]["limits"] == {"V": [-5, 5]} and src["card"]["rates"] == {"V": 0.2}
        assert src["startup"] == ["sense:current:protection 1e-6"]
        assert lock["card"]["limits"] == {"amplitude": [0.004, 1]}
        assert lock["card"]["rates"] == {}   # Kelvin doesn't ramp its amplitude: no rate, which the config would refuse
        assert not mag["used"] and mag["address"] == "serial://COM1"   # its flag is 0: listed, not ticked
        assert view["names"] == {"X_A": "lockin_a.X", "Vref_A": "lockin_a.amplitude", "Vbg": "source.V",
                                 "Ibg": "source.I"}
        assert all("?" in c for d in bus.devices.values() for c in d.sent)   # it only asked who they are

        items = [{"name": src["name"], "card": "keithley/2400", "address": src["address"], "limits": src["card"]["limits"],
                  "rates": src["card"]["rates"], "from": "specialmeasure"}]
        text = client.post("/api/instruments/draft", json={"items": items, "base": "", "names": view["names"]},
                           headers=headers).json()["yaml"]
        assert "V: [-5, 5]" in text and "V from your Special Measure rack (its channels' limits)" in text
        assert "the rest are its card's vendor limits" in text   # said apart from the rack's
        assert "rates: { V: 0.2 }" in text and "\nnames:" in text and "  Vbg: source.V" in text
        text = text.replace("  X_A: lockin_a.X\n", "").replace("  Vref_A: lockin_a.amplitude\n", "")
        r = client.post("/api/lab", json={"yaml": text}, headers=headers).json()
        assert r["ok"], r
        assert runtime.lab.config.names == {"Vbg": "source.V", "Ibg": "source.I"}
        assert runtime.lab.config.instruments["source"].rates == {"V": 0.2}
        # the lab's own names: in a queue item (kept with the instruments' own names) and set_value
        added = runtime.queue_add("step: gate\nset: { Vbg: 0 }\nwait: 1", by="human")
        assert added.startswith("added step gate"), added
        assert "source.V" in runtime.queue.items[-1].text and "Vbg" not in runtime.queue.items[-1].text
        assert "Vbg = source.V" in runtime.lab_status()


def test_names_join_a_lab_config_and_keep_what_is_there():
    text = "lab: x\ninstruments:\n  smu: { card: keithley/2400, address: GPIB0::24::INSTR }\n"
    once = add_names(text, {"Vbg": "smu.V"})
    assert once.endswith("names:   # your Special Measure channels' names: plans, the chat and set_value may use "
                         "them for instrument.parameter\n  Vbg: smu.V\n")
    assert add_names(once, {"Vbg": "smu.I", "Ibg": "smu.I"}).count("Vbg: smu.V") == 1   # a name keeps its meaning
    assert "  Ibg: smu.I" in add_names(once, {"Ibg": "smu.I"})
    inline = add_names(text + "names: { Vtg: smu.V }\n", {"Ibg": "smu.I"})
    assert "names:\n  Ibg: smu.I\n  Vtg: smu.V" in inline


def test_kelvin_reads_a_special_measure_folder_in_a_chat_and_never_runs_it(bench, tmp_path):
    (runtime, settings, store), bus, home = bench
    folder = tmp_path / "sm"
    folder.mkdir()
    (folder / "smsetup.m").write_text(RACK, encoding="utf-8")
    text = runtime.survey_specialmeasure(str(folder))
    assert "Vbg = Source.V → V" in text and "survey" not in text[:0] and "Never run MATLAB" in text
    assert "not read: " in runtime.survey_specialmeasure(str(tmp_path / "nope"))
    import anyio
    from mcp import Client
    from labharness.agent.server import build_server

    async def main():
        async with Client(build_server(runtime)) as client:
            return (await client.list_tools()).tools
    tool = next(t for t in anyio.run(main) if t.name == "survey_specialmeasure")
    assert tool.annotations.read_only_hint   # it reads the lab's files, never changes anything


def test_saved_scans_and_runs_come_in_and_old_runs_import_with_the_day_they_were_taken(bench):
    pytest.importorskip("numpy")
    import os
    import matfiles
    from labharness import load_plan, rundata
    (runtime, settings, store), bus, home = bench
    folder = home / "SpecialMeasure"
    (folder / "setups").mkdir(parents=True)
    (folder / "setups" / "smsetup_rig.m").write_text(RACK, encoding="utf-8")
    (folder / "data").mkdir()
    gate = matfiles.loop(npoints=5, setchan=["Vbg"], getchan=["X_A", "Ibg"], ranges=[(-1, 1)], waittime=0.1,
                         startwait=2)
    saved = matfiles.sm_run([gate], [], name="gate sweep", with_data=False, escapefn="@(x)smset('Vbg',0)")["scan"]
    matfiles.write(folder / "gate.mat", {"smscan": saved})
    other = matfiles.sm_run([matfiles.loop(npoints=3, setchan=["Vref_A"], getchan=["X_A"], ranges=[(0.01, 0.1)])],
                            [], name="drive", with_data=False)["scan"]
    matfiles.write(folder / "list.mat", {"scans": matfiles.Cell([saved, other])})   # Special Measure's Save Scans
    import numpy as np
    run = matfiles.write(folder / "data" / "007_gate.mat",
                         matfiles.sm_run([gate], [np.arange(5.0).reshape(5, 1), np.zeros((5, 1))], name="gate sweep"))
    os.utime(run, (1600000000, 1600000000))   # 2020-09-13
    client, headers = client_for((runtime, settings, store), {"scripted": Scripted([])})
    with client:
        view = client.post("/api/instruments/specialmeasure", json={"folder": str(folder)}, headers=headers).json()
        scans = {(s["name"], s["source"]) for s in view["scans"]}
        assert scans == {("gate sweep", "saved"), ("drive", "saved"), ("gate", "run")}   # one per layout
        gate_scan = next(s for s in view["scans"] if s["name"] == "gate sweep")
        plan = load_plan(gate_scan["text"])
        assert plan.axes[0].key == "source.V" and plan.axes[0].first_settle == 2.1
        assert [k for k, _ in plan.measure] == ["lockin_a", "source"] and not gate_scan["gaps"]
        assert view["runs"] == {"data": 1}
        chosen = [{"rack": i["rack_name"], "name": i["name"], "card": i["card"]["id"]} for i in view["instruments"]
                  if i["card"]]
        body = {"folder": str(folder), "setup": view["setup"], "instruments": chosen,
                "scans": [{"path": s["path"], "index": s["index"]} for s in view["scans"]] +
                         [{"path": "../outside.mat", "index": 0}]}
        kept = client.post("/api/instruments/specialmeasure/scans", json=body, headers=headers).json()
        assert sorted(k["name"] for k in kept["kept"]) == ["drive", "gate", "gate sweep"]
        assert kept["not"] == [{"scan": "../outside.mat", "path": "../outside.mat",
                                "gaps": ["not a file in %s" % folder]}]
        entry = next(e for e in kept["library"]["scans"] if e["name"] == "gate sweep")
        assert entry["source"] == "specialmeasure" and "Special Measure" in load_plan(entry["text"]).description
        r = client.post("/api/instruments/specialmeasure/runs", json={"folder": str(folder), "runs": ["data"]},
                        headers=headers).json()
        assert [x["file"] for x in r["imported"]] == ["007_gate.mat"] and not r["failed"], r
        assert r["imported"][0]["run"].startswith("20200913-")   # named for the day it was taken
        again = client.post("/api/instruments/specialmeasure/runs", json={"folder": str(folder), "runs": ["data"]},
                            headers=headers).json()
        assert again["imported"] == [] and again["left"] == 1   # imported once
        assert client.post("/api/instruments/specialmeasure/runs", json={"folder": str(folder), "runs": [".."]},
                           headers=headers).status_code == 400
        rec = rundata.load(runtime.runs.find(r["imported"][0]["run"]))
        assert rec.meta["source"] == "specialmeasure" and rec.meta["sm_run"] == 7 and len(rec.rows) == 5
