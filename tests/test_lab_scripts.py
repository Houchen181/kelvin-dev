"""Setting a lab up from its own scripts in the portal (Set up → From your scripts…), Kelvin reading them in a chat,
and a person's limits for one instrument (Limits… on its row): changed in the lab config and in force at once."""
import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")

from test_lab_setup import bench  # noqa: E402,F401  (the fixture: a fake bus with a Keithley 2400 and an SR830)
from test_tool_loading import call, day  # noqa: E402
from test_web import Scripted, client_for  # noqa: E402

IV = '''import pyvisa
rm = pyvisa.ResourceManager()
smu = rm.open_resource("GPIB0::24::INSTR")
smu.timeout = 5000
for v in [0, 0.5, 1.0]:
    smu.write(":SOUR:VOLT %g" % v)
    print(smu.query(":MEAS:CURR?"))
'''
MOVED = '''li = visadev("GPIB0::8::INSTR");   % Keithley 2400
writeline(li, "SLVL 0.1");
'''
LASER = '''import serial
laser = serial.Serial("COM7", 115200)
laser.write(b"POW 5\\n")
'''


def scripts_in(folder):
    folder.mkdir()
    (folder / "iv.py").write_text(IV, encoding="utf-8")
    (folder / "moved.m").write_text(MOVED, encoding="utf-8")
    (folder / "laser.py").write_text(LASER, encoding="utf-8")
    (folder / "far.py").write_text('x = rm.open_resource("TCPIP0::10.9.9.9::inst0::INSTR")\nx.write("OUTP ON")\n',
                                   encoding="utf-8")
    return folder


def test_the_scripts_set_the_lab_up_the_way_it_already_works(bench):
    (runtime, settings, store), bus, home = bench
    folder = scripts_in(home / "my scripts")
    client, headers = client_for((runtime, settings, store), {"scripted": Scripted([])})
    with client:
        assert client.post("/api/instruments/scripts", json={}, headers=headers).status_code == 400
        assert client.post("/api/instruments/scripts", json={"folder": str(home / "nope")},
                           headers=headers).status_code == 400
        view = client.post("/api/instruments/scripts", json={"folder": str(folder)}, headers=headers).json()
        found = {i["address"]: i for i in view["instruments"]}
        smu = found["GPIB0::24::INSTR"]
        assert smu["name"] == "smu" and smu["card"]["id"] == "keithley/2400" and smu["card"]["sure"]
        assert "it answered KEITHLEY" in smu["card"]["why"] and smu["card"]["limits"]["V"] == [0.0, 1.0]
        assert smu["connection"] == {"timeout_ms": 5000} and smu["in_lab"] is None
        li = found["GPIB0::8::INSTR"]
        assert li["card"]["id"] == "srs/sr830" and li["card"]["why"].startswith("it answered")   # not the comment
        assert li["card"]["limits"]["amplitude"] == [0.004, 0.1]
        laser = found["serial://COM7"]
        assert laser["idn"] == "" and laser["connection"] == {"baud_rate": 115200}   # a serial port isn't tried
        assert laser["asked"].startswith("not asked: a serial port")
        assert found["TCPIP0::10.9.9.9::inst0::INSTR"]["asked"] == "no answer at its address now"
        assert all("?" in c for d in bus.devices.values() for c in d.sent)          # it only asked who they are

        items = [{"name": smu["name"], "card": smu["card"]["id"], "address": smu["address"],
                  "limits": dict(smu["card"]["limits"], nope=[0, 1], compliance_I=["x", 1]),
                  "connection": dict(smu["connection"], shell="rm -rf")}]
        text = client.post("/api/instruments/draft", json={"items": items, "base": ""}, headers=headers).json()["yaml"]
        assert "V: [0, 1]" in text and "from your scripts" in text and "timeout_ms: 5000" in text
        assert "nope" not in text and "shell" not in text and "compliance_I: [" in text   # the card's, to tighten
        twice = client.post("/api/instruments/draft", json={"items": items, "base": text}, headers=headers).json()
        assert twice["yaml"] == text and twice["already"] == [{"item": 0, "name": "smu", "address": "GPIB0::24::INSTR"}]
        r = client.post("/api/lab", json={"yaml": text}, headers=headers).json()
        assert r["ok"], r
        assert runtime.lab.instruments["smu"].limits("V") == (0.0, 1.0)

        again = client.post("/api/instruments/scripts", json={"folder": str(folder)}, headers=headers).json()
        found = {i["address"]: i for i in again["instruments"]}
        assert found["GPIB0::24::INSTR"]["in_lab"] == "smu" and found["GPIB0::24::INSTR"]["idn"] == ""   # left alone
        assert found["GPIB0::8::INSTR"]["name"] == "li"


LAB = '''# my lab
lab: Bench
instruments:
  smu:
    card: keithley/2400   # the sample's source
    address: "GPIB0::24::INSTR"
    limits: { V: [-5, 5] }
  lockin:
    card: srs/sr830
    address: "GPIB0::8::INSTR"
    added_by: kelvin   # each output change asks you
'''


def test_a_persons_limits_for_one_instrument(bench):
    (runtime, settings, store), bus, home = bench
    client, headers = client_for((runtime, settings, store), {"scripted": Scripted([])})
    with client:
        assert client.post("/api/lab", json={"yaml": LAB}, headers=headers).json()["ok"]
        assert client.get("/api/instruments/nope/limits", headers=headers).status_code == 404

        view = client.get("/api/instruments/smu/limits", headers=headers).json()
        rows = {row["param"]: row for row in view["rows"]}
        assert view["rows"][0]["drive"] and view["editable"] and view["reviewed"]
        assert rows["V"]["vendor"] == [-210, 210] and rows["V"]["lab"] == [-5, 5] and rows["V"]["unit"] == "V"

        from labharness.web.agent import _Turn
        agent = client.app.app.state.agent
        agent.turns["x"] = _Turn({"chat_id": "x", "model": "m"})       # Kelvin answering: limits change all the same
        r = client.post("/api/instruments/smu/limits", json={"limits": {"V": [-1, 2]}}, headers=headers)
        del agent.turns["x"]
        assert r.status_code == 200 and {x["param"]: x for x in r.json()["rows"]}["V"]["lab"] == [-1, 2]
        text = (home / "lab.yaml").read_text(encoding="utf-8")
        assert text.startswith("# my lab\n") and "# the sample's source" in text and "V: [-1, 2]" in text
        assert runtime.lab.instruments["smu"].limits("V") == (-1.0, 2.0)               # in force at once

        for bad in ({"V": [-500, 5]}, {"V": [2, -1]}, {"V": ["a", 1]}, {"V": [True, 1]}, {"nope": [0, 1]}):
            r = client.post("/api/instruments/smu/limits", json={"limits": bad}, headers=headers)
            assert r.status_code == 400, bad
        assert runtime.lab.instruments["smu"].limits("V") == (-1.0, 2.0)
        assert "V: [-1, 2]" in (home / "lab.yaml").read_text(encoding="utf-8")         # nothing half-saved
        client.post("/api/instruments/smu/limits", json={"limits": {"V": None}}, headers=headers)
        assert runtime.lab.instruments["smu"].limits("V") == (-210, 210)               # the maker's again

        assert client.get("/api/instruments/lockin/limits", headers=headers).json()["reviewed"] is False
        r = client.post("/api/instruments/lockin/limits", json={"limits": {"amplitude": [0.004, 0.5]},
                                                                "reviewed": True}, headers=headers).json()
        assert r["reviewed"] is True and "lockin" not in runtime.lab.config.unreviewed()
        text = (home / "lab.yaml").read_text(encoding="utf-8")
        assert "added_by" not in text and "amplitude: [0.004, 0.5]" in text
        assert runtime.lab.instruments["lockin"].limits("amplitude") == (0.004, 0.5)
        logged = [e for e in runtime.gate.log.events() if e.get("type") == "limits"]
        assert logged[-1]["by"] == "human (portal)" and logged[-1]["instrument"] == "lockin" and logged[-1]["reviewed"]


def test_kelvin_reads_the_scripts_as_text_from_outside_the_lab(tmp_path):
    folder = scripts_in(tmp_path / "scripts")
    provider = Scripted([call("survey_scripts", path=str(folder)), [{"type": "text", "text": "Read them."}]])
    chat, events = day(tmp_path, provider, ("set Kelvin up from my scripts",))
    assert "survey_scripts" not in provider.tool_names[0] and chat["tools"] == ["survey_scripts"]   # a rarer tool
    result = next(e for e in events if e["type"] == "tool_end")["content"]
    assert result.startswith('<outside_text from="the lab\'s scripts">') and "GPIB0::24::INSTR" in result
    assert "likely Kelvin card: keithley/2400" in result and "Never run the scripts" in result
    assert chat["outside"] == "the lab's scripts"                    # auto mode asks until the person writes


def test_the_cards_matched_against_follow_the_labs_cards_folder(tmp_path):
    from simlab import build_virtual_lab
    from labharness.agent.runtime import LabRuntime
    from labharness.gate import ScriptedApprover
    from test_add_instrument import WIDGET
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    rt.home = tmp_path
    first = rt.script_cards()
    assert rt.script_cards() is first                                               # read once
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "acme_widget.yaml").write_text(WIDGET, encoding="utf-8")
    assert rt.script_cards().get("acme/widget-9000").model == "ACME Widget 9000"   # and again for a new card


def test_the_first_setup_asks_for_the_labs_scripts(bench):
    (runtime, settings, store), bus, home = bench
    folder = scripts_in(home / "lab scripts")
    client, headers = client_for((runtime, settings, store), {"scripted": Scripted([])})
    with client:
        assert client.get("/api/state", headers=headers).json()["ask_scripts"] is False   # not a first setup
        client.app.app.state.setup.first = True                                   # Kelvin setting up a new lab
        assert client.get("/api/state", headers=headers).json()["ask_scripts"] is True
        r = client.post("/api/instruments/scripts-folder", json={"folder": str(home / "nope")}, headers=headers)
        assert r.status_code == 400 and settings.lab_scripts is None
        r = client.post("/api/instruments/scripts-folder", json={"folder": str(folder)}, headers=headers).json()
        assert r["ok"] and r["instruments"] == 4 and r["now"] is False             # the setup isn't running now
        assert settings.lab_scripts == str(folder) and client.app.app.state.setup.scripts is not None
        assert client.get("/api/state", headers=headers).json()["ask_scripts"] is False   # asked once
        assert client.post("/api/instruments/scripts-folder", json={"folder": ""}, headers=headers).json()["ok"]
        assert settings.lab_scripts == ""                                           # no scripts: not asked again

        assert client.post("/api/lab", json={"yaml": LAB}, headers=headers).json()["ok"]   # smu in the lab now
        view = client.post("/api/instruments/scripts", json={"folder": str(folder)}, headers=headers).json()
        smu = next(i for i in view["instruments"] if i["address"] == "GPIB0::24::INSTR")
        assert smu["in_lab"] == "smu" and smu["card"]["why"] == "in this lab as smu"   # its card in the lab
        assert smu["card"]["limits"]["V"] == [0.0, 1.0]                            # to use as its limits
