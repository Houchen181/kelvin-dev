"""Protecting the sample (protection.py): a device's own limits and gate-leakage guard, the caps written into the
source itself, source ranges that change only at zero, and the hands-on-the-sample lock."""
import pytest

from labharness import ScriptedApprover, build_virtual_lab
from labharness import protection

GATE = {"gates": [{"drive": "smu.V", "dielectric": "hBN", "thickness_nm": 20, "leak": "smu.I"}]}


def runtime(tmp_path, answers=50):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    return LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * answers), tmp_path / "data", None)


@pytest.fixture
def parts(tmp_path):
    pytest.importorskip("starlette")
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    return runtime(tmp_path), Settings(tmp_path / "settings.json"), ChatStore(tmp_path / "chats")


def mount(rt, prot, name="D4"):
    return rt.mount(None, {"id": name.lower(), "name": name, "protection": protection.normalize(prot, rt.lab)})


def test_a_gate_limit_comes_from_its_dielectric():
    lab = build_virtual_lab(seed=1)
    gate = protection.normalize(GATE, lab)["gates"][0]
    assert (gate["limit"], gate["leak"], gate["leak_max"]) == (6, "smu.I", protection.LEAK_MAX)
    assert protection.gate_limit("SiO2", 285) == 85.5 and protection.gate_limit("unobtainium", 10) is None
    assert protection.normalize({}, lab) == {} and protection.normalize({"gates": [], "limits": {}}, lab) == {}
    for bad, words in [({"gates": [{"drive": "smu.V", "dielectric": "other"}]}, "give its limit"),
                       ({"gates": [{"drive": "smu.V", "limit": -2}]}, "give its limit"),
                       ({"gates": [{"drive": "smu.I", "limit": 1}]}, "not a numeric drive"),
                       ({"gates": [{"drive": "nope.V", "limit": 1}]}, "gate 1"),
                       ({"gates": [{"drive": "smu.V", "limit": 1, "leak": "smu.nothing"}]}, "leakage"),
                       ({"gates": [{"drive": "smu.V", "limit": 1, "rate": 0}]}, "ramp rate"),
                       ({"gates": [{"drive": "smu.V", "dielectric": "hBN", "thickness_nm": -3}]}, "above 0 nm"),
                       ({"limits": {"laser.power": [1, 2]}}, "include its safe value"),
                       ({"limits": {"laser.power": [0]}}, "[lowest, highest]"),
                       ("6 V", "an object")]:
        with pytest.raises(ValueError) as e:
            protection.normalize(bad, lab)
        assert words in str(e.value), (bad, str(e.value))


def test_a_mounted_device_narrows_the_limits_and_caps_the_source(tmp_path):
    rt = runtime(tmp_path)
    smu = rt.lab.instruments["smu"]
    assert smu.read("V_autorange") is True and smu.read("compliance_I") == pytest.approx(1e-4)
    m = mount(rt, GATE)
    assert m["caps"] == ["smu.V_autorange on → off", "smu.compliance_I 100 µA → 10 nA (toward safe)"]
    assert smu.limits("V") == (-6, 6) and smu.limits_source("V") == "device D4"
    assert "V  drive  V  [-6, 6] device D4" in rt.gate.describe("smu")
    assert smu.limits("compliance_I") == (1e-9, 1e-8)
    r = rt.gate.set("smu", "V", 8)
    assert not r.ok and "device D4" in r.message
    assert not rt.gate.set("smu", "compliance_I", 1e-6).ok        # the source's own cap is a limit too
    r = rt.gate.set("smu", "V_autorange", True)
    assert not r.ok and "range fixed" in r.message                # and its range stays where it was put
    status = rt.lab_status()
    assert "mounted device D4 is protected" in status and "Gate smu.V within ±6 V (20 nm hBN)" in status
    assert "Capped in the instruments: smu.V_autorange off, smu.V_range ≤ 20 V, smu.compliance_I ≤ 10 nA" in status
    assert rt.ui_state()["mounted"]["protection_lines"][0].startswith("Gate smu.V within ±6 V")
    rt.unmount()
    assert smu.limits("V") == (-15, 15) and smu.limits_source("V") == "lab" and smu.guards("V") == \
        rt.lab.config.instruments["smu"].guards.get("V", [])


def test_the_leakage_guard_and_the_current_cap_stop_a_breakdown(tmp_path):
    rt = runtime(tmp_path)
    smu = rt.lab.instruments["smu"]
    smu.config.guards = {}                      # without the virtual lab's own guard: only the device's
    mount(rt, {"gates": [{"drive": "smu.V", "limit": 14, "leak": "smu.I"}]})
    assert [(g.instr, g.param, g.max_abs) for g in smu.guards("V")] == [("smu", "I", 1e-9)]
    assert rt.gate.set("smu", "output", True).ok
    r = rt.gate.set("smu", "V", 13)             # the twin's gate dielectric breaks down above 12 V
    assert not r.ok and "tripped" in r.message and "10 nA" in r.message   # the source held it to its cap
    assert smu.read("V") == 0


def test_a_source_range_changes_only_at_zero(tmp_path):
    rt = runtime(tmp_path)
    assert rt.gate.set("smu", "output", True).ok and rt.gate.set("smu", "V", 3).ok
    r = rt.gate.set("smu", "V_range", 200)
    assert not r.ok and "glitch" in r.message
    r = rt.gate.raw("smu", "SOUR:VOLT:RANG 200")             # a raw command follows the same rule
    assert not r.ok and "glitch" in r.message
    assert not rt.gate.set("smu", "V_autorange", False).ok
    assert rt.gate.set("smu", "V", 0).ok
    assert rt.gate.set("smu", "V_range", 200).ok and rt.gate.set("smu", "V_autorange", False).ok


def test_caps_wait_until_the_source_is_at_zero(tmp_path):
    rt = runtime(tmp_path)
    smu = rt.lab.instruments["smu"]
    assert rt.gate.set("smu", "output", True).ok and rt.gate.set("smu", "V", 3).ok
    m = mount(rt, {"gates": [{"drive": "smu.V", "limit": 1.5}]})
    assert "smu.V_range: set when smu.V is back at 0 V" in m["caps"] and smu.read("V_range") == 20
    assert "smu.V is at 3 V, outside the device's limits (-1.5 to 1.5): bring it back within them or to 0 V" \
        in m["caps"] and rt.caps_waiting
    assert "refused" in rt.set("smu", "V", 2)   # it may only come back within the limits
    rt.set("smu", "V", 0)                       # once Kelvin is idle at 0 V, the range is set
    assert smu.read("V_range") == 2 and not rt.caps_waiting


def test_a_plan_on_another_device_is_held_to_both(tmp_path):
    rt = runtime(tmp_path)
    smu = rt.lab.instruments["smu"]
    d4 = {"id": "d4", "name": "D4", "protection": protection.normalize(GATE, rt.lab)}
    d5 = {"id": "d5", "name": "D5", "protection": protection.normalize(
        {"gates": [{"drive": "smu.V", "dielectric": "hBN", "thickness_nm": 5}]}, rt.lab)}
    rt.project_info = lambda pid: {"id": None, "name": "General", "devices": [d4, d5], "notes": [], "references": [],
                                   "summary": "", "private": False, "unshared": False}
    rt.mount(None, d4)
    out = rt.run("plan: d5_gate\ndevice: D5\nsetup: { smu.output: on }\nsweep: { smu.V: {from: 0, to: 3, step: 0.5} }\n"
                 "measure: [smu.I]\nafter: { smu.V: 0 }")
    assert "held to D5's protection for this run: Gate smu.V within ±1.5 V (5 nm hBN)" in out
    assert "outside device D4 and D5 limits [-1.5, 1.5] V" in out
    assert smu.limits("V") == (-6, 6) and smu.read("V_range") == 20   # D4 alone again, its range as it was
    assert protection.combine(d4["protection"], d5["protection"])["gates"][0]["limit"] == 1.5
    assert protection.combine({}, d5["protection"]) == d5["protection"]


def test_hands_on_the_sample_holds_every_drive_at_safe(tmp_path):
    rt = runtime(tmp_path)
    smu, tc = rt.lab.instruments["smu"], rt.lab.instruments["tc"]
    assert rt.gate.set("smu", "output", True).ok and rt.gate.set("smu", "V", 2).ok
    setpoint = tc.read("setpoint")
    lines = rt.hands_on("human (portal)")
    assert smu.read("V") == 0 and smu.read("output") is False
    assert "kept as they are (they don't touch the sample): tc, magnet, stage, counter" in lines
    assert tc.read("setpoint") == setpoint
    r = rt.gate.set("smu", "V", 1)
    assert not r.ok and "hands on the sample" in r.message
    r = rt.gate.raw("smu", "SOUR:VOLT:PROT 20")                  # a command the card can't check: not even asked
    assert r.action == "refused" and "hands on the sample" in r.message
    assert rt.gate.set("magnet", "rate", 0.1).ok                  # toward safe is still fine
    assert "HANDS ON THE SAMPLE" in rt.lab_status() and rt.ui_state()["hands_on"]["by"] == "human (portal)"
    assert rt.hands_on("human (portal)")[0] == "everything that reaches the sample was already at safe"
    rt.hands_off("human (portal)")
    assert rt.gate.set("smu", "output", True).ok and rt.gate.set("smu", "V", 1).ok
    assert protection.applies_to_sample(smu.card) and not protection.applies_to_sample(tc.card)


def test_people_set_protection_and_the_lock_in_the_portal(parts):
    from test_web import Scripted, client_for
    runtime, settings, store = parts
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        opts = client.get("/api/protection", headers=headers).json()
        gate = next(d for d in opts["drives"] if d["key"] == "smu.V")
        assert gate["gate"] and gate["leak"] == "smu.I" and gate["limits"] == [-15, 15]
        assert {"name": "hBN", "field": 0.3} in opts["dielectrics"]
        assert not next(d for d in opts["drives"] if d["key"] == "tc.setpoint")["gate"]
        p = client.post("/api/projects", json={"name": "Graphene"}, headers=headers).json()
        d4 = client.post("/api/projects/%s/devices" % p["id"], json={"name": "D4"}, headers=headers).json()["devices"][0]
        url = "/api/projects/%s/devices/%s" % (p["id"], d4["id"])
        bad = client.patch(url, json={"protection": {"gates": [{"drive": "smu.V", "dielectric": "other"}]}},
                           headers=headers)
        assert bad.status_code == 400 and "give its limit" in bad.json()["error"]
        got = client.patch(url, json={"protection": GATE}, headers=headers).json()
        assert got["protection"]["gates"][0]["limit"] == 6 and got["caps"] == []   # not mounted yet
        m = client.post("/api/mount", json={"project": p["id"], "device": "D4"}, headers=headers).json()
        assert m["caps"] == ["smu.V_autorange on → off", "smu.compliance_I 100 µA → 10 nA (toward safe)"]
        assert m["mounted"]["protection_lines"][0].startswith("Gate smu.V within ±6 V")
        tighter = dict(GATE, gates=[dict(GATE["gates"][0], thickness_nm=10)])
        got = client.patch(url, json={"protection": tighter}, headers=headers).json()
        assert got["protection_lines"][0].startswith("Gate smu.V within ±3 V")
        assert runtime.lab.instruments["smu"].limits("V") == (-3, 3)

        locked = client.post("/api/hands", json={"on": True}, headers=headers).json()
        assert locked["hands_on"]["by"] == "human (portal)" and settings.hands_on()["by"] == "human (portal)"
        assert client.get("/api/state", headers=headers).json()["hands_on"]["since"]
        assert "refused" in runtime.set("smu", "V", 1)
        done = client.post("/api/hands", json={"on": False, "project": p["id"], "device": ""}, headers=headers).json()
        assert done["hands_on"] is None and done["mounted"] is None and settings.hands_on() is None
        assert client.patch(url, json={"protection": {}}, headers=headers).json().get("protection") is None


def test_the_lock_outlasts_a_restart(tmp_path):
    pytest.importorskip("starlette")
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from test_web import Scripted
    from starlette.testclient import TestClient
    settings = Settings(tmp_path / "settings.json")
    first = runtime(tmp_path)
    app = create_app(first, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"],
                     providers={"scripted": Scripted([])})
    with TestClient(app) as client:
        client.post("/api/hands", json={"on": True}, headers={"X-Kelvin-Token": settings.access_key})
    again = runtime(tmp_path)
    create_app(again, Settings(tmp_path / "settings.json"), ChatStore(tmp_path / "chats"), hosts=["testserver"],
               providers={"scripted": Scripted([])})
    assert again.hands["by"] == "human (portal)" and "refused" in again.set("smu", "output", True)


def test_a_watch_phone_can_lock_but_not_unlock(parts):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from labharness.web.app import create_app
    from test_web import Scripted
    runtime, settings, store = parts
    guard = create_app(runtime, settings, store, hosts=["testserver"], providers={"scripted": Scripted([])})
    owner = {"X-Kelvin-Token": settings.access_key}
    with TestClient(guard) as local:
        local.post("/api/phone", json={"on": True}, headers=owner)
        guard.remote.port = 8771
        phone = TestClient(guard, base_url="http://192.168.1.50:8771")
        tokens = {}
        for role in ("watch", "control"):
            code = local.post("/api/devices", json={"name": role, "role": role}, headers=owner).json()["code"]
            tokens[role] = {"X-Kelvin-Token": phone.post("/api/pair", json={"code": code}).json()["token"]}
        assert phone.post("/api/hands", json={"on": True}, headers=tokens["watch"]).json()["hands_on"]["by"] == \
            "human (phone watch)"
        assert phone.post("/api/hands", json={"on": False}, headers=tokens["watch"]).status_code == 403
        assert phone.get("/api/protection", headers=tokens["control"]).status_code == 403   # this computer only
        assert phone.post("/api/hands", json={"on": False}, headers=tokens["control"]).json()["hands_on"] is None


def test_kelvin_reads_protection_but_never_lifts_it(tmp_path):
    pytest.importorskip("starlette")
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from test_web import Scripted, client_for, stream_turn
    rt = runtime(tmp_path)
    store = ChatStore(tmp_path / "chats")
    call = lambda i, name, args: [{"type": "tool_call", "id": "c%d" % i, "name": name, "input": args}]   # noqa: E731
    done = [{"type": "text", "text": "Done."}]
    bot = Scripted([call(1, "devices", {}), done, call(2, "mount_device", {"device": ""}), done])
    client, headers = client_for((rt, Settings(tmp_path / "settings.json"), store), {"scripted": bot})
    with client:
        p = client.post("/api/projects", json={"name": "Graphene"}, headers=headers).json()
        d4 = client.post("/api/projects/%s/devices" % p["id"], json={"name": "D4"}, headers=headers).json()["devices"][0]
        client.patch("/api/projects/%s/devices/%s" % (p["id"], d4["id"]), json={"protection": GATE}, headers=headers)
        client.post("/api/mount", json={"project": p["id"], "device": "D4"}, headers=headers)
        chat = client.post("/api/chats", json={"model": "scripted:m1", "project": p["id"]}, headers=headers).json()
        ends = []
        for text in ("which devices?", "take D4 out"):
            ends += [e for e in stream_turn(client, headers, chat["id"], text, "scripted:m1") if e["type"] == "tool_end"]
        assert "D4; protected: Gate smu.V within ±6 V (20 nm hBN)" in ends[0]["content"]
        assert ends[1]["content"].startswith("not changed: D4 is mounted with protection a person set")
        assert rt.mounted["device_name"] == "D4"


def test_a_yokogawa_gate_and_a_keithley_are_capped_too(tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    from labharness.virtual_lab import build_lab
    lab = build_lab("lab: Gate bench\ninstruments:\n  gs: { card: yokogawa/gs200, address: \"sim://gs\" }\n"
                    "  k: { card: keithley/2400, address: \"sim://k\" }\n")
    rt = LabRuntime(lab, ScriptedApprover([True] * 20), tmp_path / "data", None)
    assert rt.gate.set("gs", "range", 30).ok
    m = mount(rt, {"gates": [{"drive": "gs.level", "dielectric": "hBN", "thickness_nm": 20, "leak": "k.I"},
                             {"drive": "k.V", "dielectric": "SiO2", "thickness_nm": 285, "leak": "k.I"}]})
    assert m["caps"] == ["gs.range 30 V → 10 V", "k.V_autorange on → off", "k.V_range 20 V → 200 V",   # covers ±85.5 V
                         "k.compliance_I 100 µA → 10 nA (toward safe)"]
    assert lab.instruments["k"].limits("V") == (-85.5, 85.5)
    assert rt.gate.set("gs", "output", True).ok and rt.gate.set("gs", "level", 3).ok
    assert "glitch" in rt.gate.set("gs", "range", 1).message
