"""What the lab panel shows: readable labels, readings vs outputs vs settings, and each instrument's state."""
from labharness import ScriptedApprover, build_virtual_lab, demo_envelope
from labharness.agent.runtime import LabRuntime


def panel(runtime):
    return {i["name"]: i for i in runtime.ui_state()["instruments"]}


def test_instruments_are_grouped_and_labelled(tmp_path):
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, ScriptedApprover([]), tmp_path, demo_envelope(lab))
    smu = panel(rt)["smu"]
    assert smu["model"] == "Virtual Source-Meter" and smu["kind"] == "source-meter"
    assert smu["headline"] == "I" and smu["readings"][0]["label"] == "Current"
    assert [d["label"] for d in smu["drives"]] == ["Voltage", "Compliance", "Output"]
    assert [s["label"] for s in smu["settings"]] == ["Integration (NPLC)"]
    # it powers up with 100 µA compliance (above safe) but the output is off: armed, not active
    assert smu["state"] == "safe" and smu["active"] == 0
    compliance = next(d for d in smu["drives"] if d["name"] == "compliance_I")
    assert compliance["armed"] and not compliance["away"] and compliance["switch"] == "Output"
    assert compliance["value"] == "100 µA" and compliance["safe"] == "1 nA"
    assert all(i["state"] == "safe" for i in panel(rt).values())   # a fresh lab drives nothing
    laser = panel(rt)["laser"]
    assert next(d for d in laser["drives"] if d["name"] == "shutter")["value"] == "closed"


def test_switching_an_output_on_makes_the_instrument_active(tmp_path):
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, ScriptedApprover([True, True]), tmp_path, demo_envelope(lab))
    rt.set("smu", "output", True)
    rt.set("smu", "V", 2)
    smu = panel(rt)["smu"]
    assert smu["state"] == "active" and smu["active"] == 3   # voltage, compliance and the output itself
    assert next(d for d in smu["drives"] if d["name"] == "V")["away"]


def test_unresponsive_instrument_shows_as_error(tmp_path):
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, ScriptedApprover([]), tmp_path, demo_envelope(lab))
    lab.instruments["lockin"].backend.fault = "offline"
    rt._status_at = 0  # force a refresh
    lockin = panel(rt)["lockin"]
    assert lockin["state"] == "error"
    assert all(r["value"] == "no response" for r in lockin["readings"])
