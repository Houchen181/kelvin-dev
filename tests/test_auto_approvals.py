"""Kelvin approves itself what drives nothing (user report, 2026-11-01: "right now this auto approval does not work
well. The Calvin even asks for approval for some trivial items such as using PyLab or USB or NIDAQ packages"). A lab
script's run asked a person again for each maker's library it loaded on the way (pylablib's own imports of pyusb and
nidaqmx), in auto mode too, and installing a maker's package asked a person every time, though the tool, the docs and
the lab's rule (only driving the sample waits for a person) said Kelvin approves it itself."""
import json

import pytest

pytest.importorskip("mcp")

from labharness import ScriptedApprover  # noqa: E402

from test_scan_library import script_runtime, scripts, write  # noqa: E402,F401

# a maker's library that loads others itself, as pylablib loads pyusb and nidaqmx for the devices it drives
PYLABLIB = "import usb\nimport nidaqmx\n"

SCRIPT = """
import pyvisa
import pylablib     # named in the run's approval: the script imports it

rm = pyvisa.ResourceManager()
smu = rm.open_resource("GPIB0::24::INSTR")
lockin = rm.open_resource("GPIB0::8::INSTR")
smu.write("OUTP ON")
try:
    for v in [0, 0.2]:
        smu.write("SOUR:VOLT %g" % v)
        print(lockin.query("MEAS:X?"))
finally:
    smu.write("SOUR:VOLT 0")
    smu.write("OUTP OFF")
"""


def asked(rt):
    """The requests in the lab's record: what each said and its answer (a person's, or Kelvin's own)."""
    lines = (rt.data_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e.get("type") == "ask"]


def test_a_library_a_scripts_run_loads_on_the_way_is_let_through_with_the_run(tmp_path, scripts):  # noqa: F811
    fakes = tmp_path / "fakes"   # on PYTHONPATH (the scripts fixture): installed, as far as the script can tell
    for name, text in (("pylablib", PYLABLIB), ("usb", ""), ("nidaqmx", "")):
        (fakes / name).mkdir()
        (fakes / name / "__init__.py").write_text(text)
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([True]))   # one person's yes: the run's
    write(scripts, "sweep.py", SCRIPT)
    said = rt.run_script("sweep.py")
    assert "script sweep.py done" in said, said
    [run] = rt.approver.requests                                       # nobody was asked again
    assert run.title == "Run script sweep.py?" and any("It uses pylablib" in i for i in run.items)
    own = [e for e in asked(rt) if e["answer"] == "auto-approved"]
    assert [e["request"].splitlines()[0] for e in own] == ["Let sweep.py use usb?", "Let sweep.py use nidaqmx?"]
    assert all("approved with the script's run" in e["note"] for e in own)
    assert "past the gate, let through with the run: usb, nidaqmx" in said   # said in its answer, and its record


def test_a_library_a_stopping_script_loads_is_still_refused(tmp_path, scripts):  # noqa: F811
    from labharness import scriptrun
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([]))
    job = scriptrun.ScriptRun.__new__(scriptrun.ScriptRun)
    job.stopping, job.allowed, job.prep = "stop requested", set(), type("P", (), {"name": "sweep.py"})()
    with pytest.raises(scriptrun._Refuse):   # its own clean-up brings nothing new in
        job._library("nidaqmx")
    assert rt.approver.requests == []
