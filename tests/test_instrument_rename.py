"""A person renames one of the lab's instruments in the Lab panel (two of one model told apart: lockin_Vxx and
lockin_Vxy): its entry and every instrument.parameter of it in the lab config (comments kept, addresses never
touched), the lab opening again with it (its connection moving over unclosed), and what Kelvin keeps of it following:
the mounted sample's protection, the kept watches, the scans, the sequence, the lab's memory and each person's
devices. Past runs keep the name they were measured with."""
import json

import pytest

from labharness.discovery import rename_in_config, renamed_keys, renamed_plan_text

LAB = '''# my bench
lab: Bench
instruments:
  smu:
    card: keithley/2400   # the gate
    address: "GPIB0::24::INSTR"
    limits: { V: [-5, 5] }
    guards:
      V: [{ read: lockin.R, max_abs: 1 }]   # stop if the signal runs away
  lockin:
    card: srs/sr830       # Vxx
    address: "GPIB0::8::INSTR"
  lockin2: { card: srs/sr830, address: "GPIB0::9::INSTR" }
interlocks:
  - drive: smu.V
    require: "lockin.R < 2"
watch:
  - read: lockin.R
    above: 1.5
    note: lockin.R above 1.5 V means the contacts went
names:
  Vxx: lockin.X
  Vxy: lockin2.X
'''


def test_the_lab_config_says_the_new_name_wherever_it_named_the_old():
    text = rename_in_config(LAB, "lockin", "lockin_Vxx")
    assert "  lockin_Vxx:\n    card: srs/sr830       # Vxx" in text and "\n  lockin:" not in text
    assert "read: lockin_Vxx.R, max_abs: 1 }]   # stop if the signal runs away" in text
    assert 'require: "lockin_Vxx.R < 2"' in text and "read: lockin_Vxx.R\n" in text
    assert "note: lockin_Vxx.R above 1.5 V" in text and "Vxx: lockin_Vxx.X" in text
    assert "Vxy: lockin2.X" in text and "  lockin2: { card: srs/sr830" in text   # another instrument: as it was
    assert text.startswith("# my bench\n") and "# the gate" in text   # its comments stay
    # an address, or a card, is never renamed, even where a host is called like the instrument
    hosted = LAB.replace('"GPIB0::9::INSTR"', '"TCPIP0::lockin.lab::INSTR"')
    assert '"TCPIP0::lockin.lab::INSTR"' in rename_in_config(hosted, "lockin", "lockin_Vxx")
    one_line = rename_in_config(LAB, "lockin2", "lockin_Vxy")
    assert "  lockin_Vxy: { card: srs/sr830, address: \"GPIB0::9::INSTR\" }" in one_line and "Vxy: lockin_Vxy.X" in one_line
    with pytest.raises(KeyError):
        rename_in_config(LAB, "magnet", "field")


def test_a_plan_and_a_store_say_the_new_name():
    plan = ("plan: iv\nrequires: [lockin, tc]\nsweep:\n  smu.V: { from: 0, to: 1, points: 3, measure: [lockin.X] }\n"
            "sets: { lockin.amplitude: '0.1*lockin2.R' }\nmeasure: [lockin.R, lockin2.R]\n")
    moved = renamed_plan_text(plan, "lockin", "li")
    assert "requires: [li, tc]" in moved and "measure: [li.X]" in moved and "li.amplitude: '0.1*lockin2.R'" in moved
    assert "measure: [li.R, lockin2.R]" in moved
    block = renamed_plan_text("plan: x\nrequires:\n  - lockin\n  - lockin2\nmeasure: [lockin.X]\n", "lockin", "li")
    assert block == "plan: x\nrequires:\n  - li\n  - lockin2\nmeasure: [li.X]\n"
    store = {"protection": {"limits": {"smu.V": [-1, 1]}, "gates": [{"drive": "smu.V", "leak": "smu.I"}]},
             "read": "smu.I", "note": "smu2.V stays", "count": 3}
    assert renamed_keys(store, "smu", "gate") == {"protection": {"limits": {"gate.V": [-1, 1]},
                                                                 "gates": [{"drive": "gate.V", "leak": "gate.I"}]},
                                                  "read": "gate.I", "note": "smu2.V stays", "count": 3}


@pytest.fixture
def bench(tmp_path):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    from labharness.adapters import set_resource_manager
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.homelab import empty_lab
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from test_hardware import Bus, Device
    bus = Bus({"GPIB0::24::INSTR": Device("keithley/2400", name="k"), "GPIB0::8::INSTR": Device("srs/sr830", name="l"),
               "GPIB0::9::INSTR": Device("srs/sr830", name="l2")})
    set_resource_manager(bus)
    runtime = LabRuntime(empty_lab(), WebApprover(timeout=5), tmp_path / "data")
    yield (runtime, Settings(tmp_path / "settings.json"), ChatStore(tmp_path / "chats")), tmp_path
    set_resource_manager(None)


def test_renaming_in_the_lab_panel_carries_everything_kept_of_it(bench):
    from test_web import Scripted, client_for
    (runtime, settings, store), home = bench
    client, headers = client_for((runtime, settings, store), {"scripted": Scripted([])})
    with client:
        assert client.post("/api/lab", json={"yaml": LAB}, headers=headers).json()["ok"]
        choices = runtime._scan_choices()["instruments"]   # the scan editor's menus tell the two lock-ins apart
        twins = {i["name"]: i for i in choices if i["model"] == runtime.lab.instruments["lockin"].card.model}
        assert set(twins) == {"lockin", "lockin2"} and twins["lockin2"]["address"] == "GPIB0::9::INSTR"
        assert any(p["param"] == "X" for p in twins["lockin"]["read"]) and not twins["lockin"]["off"]
        output = next(p for p in next(i for i in choices if i["name"] == "smu")["set"] if p["param"] == "output")
        assert output["choices"] == ["on", "off"]   # a switch's words, for a setup's value
        # what Kelvin keeps of it: a scan, a waiting item, a watch, a note, the mounted sample's protection
        plan = "plan: gate sweep\nsweep:\n  smu.V: { from: 0, to: 1, points: 3 }\nmeasure: [lockin.X, lockin2.X]\n"
        runtime.library.add_plan(plan, "gate sweep", "smu.V 0 → 1 V · lockin.X")
        assert runtime.queue_add(plan, by="human").startswith("added")
        runtime.add_watch("lockin.Y", above=0.5, lab_wide=True)
        runtime._lab_memory().add("fact", "lockin.Y picks up the 50 Hz line", "lockin")
        device = store.add_device(None, "D1")
        store.update_device(None, device["id"], protection={"limits": {"smu.V": [-1, 1]}})
        runtime.mount(None, store.find_device(None, "D1"))
        before = runtime.lab.instruments["lockin"].backend

        assert client.post("/api/instruments/lockin/rename", json={"to": "lockin2"}, headers=headers).status_code == 409
        assert client.post("/api/instruments/lockin/rename", json={"to": "2nd"}, headers=headers).status_code == 400
        assert client.post("/api/instruments/nope/rename", json={"to": "x"}, headers=headers).status_code == 404
        r = client.post("/api/instruments/lockin/rename", json={"to": "lockin_Vxx"}, headers=headers)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["name"] == "lockin_Vxx" and d["notes"][0].startswith("lockin is lockin_Vxx now, in ")
        assert "lockin_Vxx" in {i["name"] for i in d["instruments"]} and d["versions"]
        text = (home / "lab.yaml").read_text(encoding="utf-8")
        assert "  lockin_Vxx:\n    card: srs/sr830       # Vxx" in text and "Vxx: lockin_Vxx.X" in text
        assert sorted(runtime.lab.instruments) == ["lockin2", "lockin_Vxx", "smu"]
        assert runtime.lab.instruments["lockin_Vxx"].backend is before   # its connection moved over, unclosed
        assert runtime.lab.config.names == {"Vxx": "lockin_Vxx.X", "Vxy": "lockin2.X"}
        assert [i.require.text for i in runtime.lab.config.interlocks] == ["lockin_Vxx.R < 2"]
        (scan,) = [e for e in runtime.library_view()["scans"] if e["name"] == "gate sweep"]
        assert "measure: [lockin_Vxx.X, lockin2.X]" in scan["text"] and scan["summary"] == "smu.V 0 → 1 V · lockin_Vxx.X"
        assert "lockin_Vxx.X" in runtime.queue.items[-1].text and "lockin.X" not in runtime.queue.items[-1].text
        assert "lockin_Vxx.Y" in {w.key for w in runtime.monitor.watches.values()}   # watched on, under its new name
        kept = json.loads((runtime.data_dir / runtime.WATCHES).read_text(encoding="utf-8"))["labs"]["Bench"]
        assert [w["read"] for w in kept if w["source"] != "lab"] == ["lockin_Vxx.Y"]
        (note,) = runtime._lab_memory().notes()
        assert note["instruments"] == ["lockin_Vxx"] and note["text"].startswith("lockin_Vxx.Y picks up")
        # renaming the gate's source: the sample's protection holds on it, in memory, mounted.json and the device
        assert client.post("/api/instruments/smu/rename", json={"to": "gate"}, headers=headers).status_code == 200
        assert runtime.mounted["protection"]["limits"] == {"gate.V": [-1, 1]}
        assert json.loads((runtime.data_dir / "mounted.json").read_text(encoding="utf-8"))["protection"]["limits"] == {
            "gate.V": [-1, 1]}
        assert store.find_device(None, "D1")["protection"]["limits"] == {"gate.V": [-1, 1]}
        assert runtime.lab.instruments["gate"].limits("V") == (-1.0, 1.0)   # the tighter of the lab's and the sample's
        events = [json.loads(line) for line in (runtime.data_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        assert any(e.get("type") == "instrument_renamed" and e.get("was") == "smu" for e in events)
