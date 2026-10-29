"""A lab's own scripts, run as they are, with every instrument command through the safety gate (scriptrun.py).

Each run starts a real Python with Kelvin's stand-in (data/scriptshim) on simulated instruments at the addresses the
scripts use. pyvisa and pyserial are small fakes on PYTHONPATH that refuse every real instrument, so a command that
got past the stand-in would fail the test."""
import json
import re
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from labharness import scriptrun
from labharness.clock import SimClock
from labharness.eventlog import EventLog, RunStore
from labharness.gate import SafetyGate, ScriptedApprover
from labharness.lab import Lab
from labharness.labconfig import load_lab_config
from labharness.sim.twin import World, sim_backend_factory
from labharness.homelab import builtin_registry

LAB = """
lab: Script lab
instruments:
  smu:
    card: virtual/source-meter
    address: GPIB0::24::INSTR
    limits: { V: [-10, 10] }
    rates: { V: 10 }
  lockin:
    card: virtual/lock-in
    address: GPIB0::8::INSTR
  laser:
    card: virtual/laser
    address: serial://COM7
    limits: { power: [0, 50] }
  lockin2:
    card: virtual/lock-in
    address: TCPIP0::127.0.0.1::5025::SOCKET
"""

FAKE_PYVISA = '''
class ResourceManager(object):
    def __init__(self, *args, **kwargs):
        pass

    def open_resource(self, name, **kwargs):
        raise RuntimeError("the test's fake pyvisa has no instruments (%s): Kelvin's stand-in should have answered" % name)

    def list_resources(self, query="?*::INSTR"):
        return ()
'''

FAKE_SERIAL = '''
class Serial(object):
    def __init__(self, port=None, baudrate=9600, timeout=None, **kwargs):
        self.port, self.baudrate, self.timeout, self.is_open = port, baudrate, timeout, False
        if port is not None:
            self.open()

    def open(self):
        raise RuntimeError("the test's fake pyserial has no ports (%s): Kelvin's stand-in should have answered" % self.port)

    def close(self):
        self.is_open = False

    def write(self, data):
        raise RuntimeError("written past Kelvin")

    def read(self, size=1):
        return b""

    def read_until(self, expected=b"\\n", size=None):
        return b""

    def readline(self, size=-1):
        return b""
'''


def _python(script, source, roots, configured=""):
    return scriptrun.Python([sys.executable, "-u"], "the test's Python", tuple(sys.version_info[:2]))


@pytest.fixture
def lab():
    registry = builtin_registry()
    cfg = load_lab_config(LAB, registry)
    clock = SimClock()
    world = World(clock, 0)
    return Lab(cfg, registry, sim_backend_factory(world), clock, world)


@pytest.fixture
def folder(tmp_path, monkeypatch):
    fakes = tmp_path / "fakes"
    (fakes / "pyvisa").mkdir(parents=True)
    (fakes / "pyvisa" / "__init__.py").write_text(FAKE_PYVISA)
    (fakes / "serial").mkdir()
    (fakes / "serial" / "__init__.py").write_text(FAKE_SERIAL)
    monkeypatch.setenv("PYTHONPATH", str(fakes))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    return scripts


def write(folder, name, text):
    path = folder / name
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


def output(report):
    return (Path(report.path) / "output.txt").read_text(encoding="utf-8")


def run(lab, folder, tmp_path, script, answers=(True,), params=None, drives=None, stop_after=None):
    gate = SafetyGate(lab, ScriptedApprover(list(answers)), EventLog(None, lab.clock))
    prep = scriptrun.prepare(lab, script, [folder], params=params, drives=drives, choose_python=_python)
    request, grants, problems = scriptrun.approval(gate, prep)
    assert not problems
    assert gate.ask(request)
    allowed = set(prep.found.unknown) | set(prep.found.libraries)   # named in the approval, as the runtime does
    job = scriptrun.ScriptRun(gate, prep, RunStore(tmp_path / "runs"), grants, allowed)
    if stop_after is not None:
        threading.Timer(stop_after, gate.request_abort).start()
    report = job.execute()
    return gate, request, report


SWEEP = """
import pyvisa

VMAX = 1.0    # the top of the sweep, in volts
rm = pyvisa.ResourceManager()
smu = rm.open_resource("GPIB0::24::INSTR")
lockin = rm.open_resource("GPIB::8")
print(lockin.query("*IDN?"))
smu.write("OUTP ON")
rows = []
try:
    for v in [0, 0.2, 0.4, 0.6, 0.8, VMAX]:
        smu.write("SOUR:VOLT %g" % v)
        rows.append((v, float(lockin.query("MEAS:X?")), float(lockin.query("MEAS:Y?"))))
finally:
    smu.write("SOUR:VOLT 0")
    smu.write("OUTP OFF")
with open("sweep.dat", "w") as f:
    for r in rows:
        f.write("%g\\t%g\\t%g\\n" % r)
print("Data saved to sweep.dat")
"""


def test_a_sweep_runs_through_the_gate_with_one_approval(lab, folder, tmp_path):
    write(folder, "sweep.py", SWEEP)
    gate, request, report = run(lab, folder, tmp_path, "sweep.py")
    assert report.status == "done", report.message + "\n" + "\n".join(report.output)
    text = str(request)
    assert "Run script sweep.py?" in text
    assert "It opens smu (GPIB0::24::INSTR), lockin (GPIB0::8::INSTR)" in text
    assert "smu.V: now 0 V, the script uses 0 … 1 V" in text
    # every command was the gate's: the sets, the readings
    commands = [json.loads(ln) for ln in (Path(report.path) / "commands.jsonl").read_text().splitlines()]
    assert sum(1 for c in commands if c["action"] == "set" and c["instrument"] == "smu") >= 7
    assert all(c["ok"] for c in commands)
    # the readings it took are the run's rows: lockin.X and lockin.Y against smu.V
    assert [round(r["smu.V"], 6) for r in report.rows] == [0, 0.2, 0.4, 0.6, 0.8, 1.0]
    assert all("lockin.X" in r and "lockin.Y" in r for r in report.rows)
    assert [a["key"] for a in report.columns["axes"]] == ["smu.V"]
    assert {m["key"] for m in report.columns["measures"]} == {"lockin.X", "lockin.Y"}
    assert report.files == ["sweep.dat"]
    assert any("Data saved to sweep.dat" in ln for ln in report.output)
    assert not report.left   # it switched its output off itself
    assert lab.instrument("smu").read("output") is False


def test_a_drive_beyond_the_limits_is_refused_and_the_script_stops_with_its_drives_back_at_safe(lab, folder,
                                                                                                 tmp_path):
    write(folder, "too_far.py", """
        import pyvisa
        smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
        smu.write("OUTP ON")
        smu.write("SOUR:VOLT 2")
        try:
            smu.write("SOUR:VOLT 12")
            print("still running")
        except Exception as e:
            print("got:", type(e).__name__, e)
            raise
        finally:
            print("cleaning up")
    """)
    gate, request, report = run(lab, folder, tmp_path, "too_far.py")
    assert report.status == "aborted"
    assert "refused: smu.V 12 V is outside the lab config's limits [-10, 10] V" in report.message   # whose limit
    out = output(report)
    assert "got: KelvinRefused" in out and "still running" not in out and "cleaning up" in out
    assert "KeyboardInterrupt" not in out   # the refusal ended it, nothing else
    # Kelvin brought what the script drove back to safe after it
    assert lab.instrument("smu").read("V") == 0 and lab.instrument("smu").read("output") is False
    assert any("smu.output on → off" in c for c in report.cleanup)


def test_a_change_beyond_the_approved_range_asks_and_a_no_stops_the_script(lab, folder, tmp_path):
    write(folder, "from_file.py", """
        import pyvisa
        smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
        top = float(open("top.txt").read())
        smu.write("OUTP ON")
        for v in (0.5, 1.0, top):
            smu.write("SOUR:VOLT %g" % v)
    """)
    (folder / "top.txt").write_text("3")
    # the agent gave the range it read: 0 … 1 V. The script goes to 3 V: a person is asked, and says no
    gate, request, report = run(lab, folder, tmp_path, "from_file.py", answers=(True, False),
                                drives={"smu.V": [0, 1], "smu.output": [True]})
    assert "smu.V: now 0 V, the script uses 0 … 1 V" in str(request)
    asked = gate.approver.requests[-1]
    assert "smu.V 1 V → 3 V" in str(asked)
    assert report.status == "aborted" and "declined: smu.V stays 1 V" in report.message
    assert lab.instrument("smu").read("V") == 0 and lab.instrument("smu").read("output") is False


def test_values_the_script_works_out_as_it_runs_are_approved_up_to_the_limits(lab, folder, tmp_path):
    write(folder, "from_file2.py", """
        import pyvisa
        smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
        top = float(open("top.txt").read())
        smu.write("SOUR:VOLT %g" % top)
        smu.write("SOUR:VOLT 0")
    """)
    (folder / "top.txt").write_text("3")
    gate, request, report = run(lab, folder, tmp_path, "from_file2.py")
    assert "smu.V: now 0 V, the script uses -10 … 10 V, anywhere within its limits, since it is set from values " \
           "the script works out as it runs; ramp 10 V/s" in str(request)
    assert report.status == "done", report.message


def test_a_persons_stop_ends_the_script_and_its_own_finally_runs_through_the_gate(lab, folder, tmp_path):
    write(folder, "slow.py", """
        import time, pyvisa
        smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
        smu.write("OUTP ON")
        smu.write("SOUR:VOLT 1")
        try:
            for k in range(600):
                time.sleep(0.1)
        finally:
            print("finally ran")
            smu.write("SOUR:VOLT 0")
            smu.write("OUTP OFF")
    """)
    t0 = time.monotonic()
    gate, request, report = run(lab, folder, tmp_path, "slow.py", stop_after=1.5)
    assert time.monotonic() - t0 < 20
    assert report.status == "aborted" and "stop requested" in report.message
    out = output(report)
    assert "finally ran" in out and "KeyboardInterrupt" in out
    commands = [json.loads(ln) for ln in (Path(report.path) / "commands.jsonl").read_text().splitlines()]
    assert [c["command"] for c in commands][-2:] == ["SOUR:VOLT 0", "OUTP OFF"] and all(c["ok"] for c in commands)
    assert lab.instrument("smu").read("output") is False


def test_a_script_that_does_not_stop_is_ended(lab, folder, tmp_path, monkeypatch):
    monkeypatch.setattr(scriptrun, "GRACE_S", 1.0)
    write(folder, "stubborn.py", """
        import time, pyvisa
        smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
        smu.write("OUTP ON")
        while True:
            try:
                time.sleep(0.2)
            except KeyboardInterrupt:
                print("not stopping")
    """)
    gate, request, report = run(lab, folder, tmp_path, "stubborn.py", stop_after=1.0)
    assert report.status == "aborted"
    assert "so Kelvin ended it" in report.message
    assert lab.instrument("smu").read("output") is False


def test_a_command_the_instrument_refuses_is_the_scripts_business_as_without_kelvin(lab, folder, tmp_path):
    write(folder, "beep.py", """
        import pyvisa
        smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
        smu.write("SYST:BEEP 440")        # not on the card, and the instrument doesn't know it either
        print("error:", smu.query("SYST:ERR?"))
        print("then:", smu.query("SYST:ERR?"))
    """)
    # a raw command not on the card, to an instrument with drives, asks a person (the gate's rule): here they say yes
    gate, request, report = run(lab, folder, tmp_path, "beep.py", answers=(True, True))
    assert report.status == "done", report.message
    out = output(report)
    assert 'error: -113,"Undefined header"' in out and 'then: 0,"No error"' in out
    assert any("the instruments refused 1 of its commands" in w for w in report.warnings)


def test_a_script_that_fails_leaves_nothing_driven(lab, folder, tmp_path):
    write(folder, "crash.py", """
        import pyvisa
        smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
        smu.write("OUTP ON")
        smu.write("SOUR:VOLT 2")
        1 / 0
    """)
    gate, request, report = run(lab, folder, tmp_path, "crash.py")
    assert report.status == "aborted" and "ZeroDivisionError: division by zero" in report.message
    assert lab.instrument("smu").read("V") == 0 and lab.instrument("smu").read("output") is False


def test_pyserial_and_a_plain_socket_reach_lab_instruments_through_the_gate(lab, folder, tmp_path):
    write(folder, "serial_and_socket.py", """
        import socket, serial
        laser = serial.Serial("COM7", 115200, timeout=1)
        laser.write(b"SOUR:POW 5\\r\\n")
        laser.write(b"SOUR:POW?\\r\\n")
        print("laser power:", laser.readline().decode().strip())
        laser.write(b"SOUR:POW 0\\r\\n")
        laser.close()
        s = socket.create_connection(("127.0.0.1", 5025), timeout=2)   # nothing listens there: the stand-in answers
        s.sendall(b"*IDN?\\n")
        print("lockin2:", s.recv(200).decode().strip())
        s.close()
    """)
    gate, request, report = run(lab, folder, tmp_path, "serial_and_socket.py")
    assert report.status == "done", report.message + "\n".join(report.output)
    out = "\n".join(report.output)
    assert "laser power: 5" in out and "lockin2: VIRTUAL" in out
    commands = [json.loads(ln) for ln in (Path(report.path) / "commands.jsonl").read_text().splitlines()]
    assert ("laser", "SOUR:POW 5", "set") in [(c["instrument"], c["command"], c["action"]) for c in commands]


def test_without_a_visa_library_the_lab_instruments_still_open(lab, folder, tmp_path, monkeypatch):
    novisa = tmp_path / "novisa" / "pyvisa"
    novisa.mkdir(parents=True)
    (novisa / "__init__.py").write_text(
        "class ResourceManager(object):\n"
        "    def __new__(cls, *args, **kwargs):\n"
        "        raise ValueError('Could not locate a VISA implementation')\n\n"
        "    def open_resource(self, name, **kwargs):\n"
        "        raise RuntimeError('never reached')\n")
    monkeypatch.setenv("PYTHONPATH", str(novisa.parent))
    write(folder, "novisa.py", """
        import pyvisa
        rm = pyvisa.ResourceManager()
        print(rm.open_resource("GPIB0::8::INSTR").query("*IDN?"))
        try:
            rm.open_resource("GPIB0::5::INSTR")
        except Exception as e:
            print("other:", e)
    """)
    gate, request, report = run(lab, folder, tmp_path, "novisa.py", answers=(True, True))
    out = output(report)
    assert "VIRTUAL,LOCK-IN" in out
    assert "other: GPIB0::5::INSTR can't be opened here: ValueError: Could not locate a VISA implementation" in out
    assert report.status == "done", report.message


def test_an_instrument_not_in_the_lab_that_the_reading_missed_asks_when_it_opens(lab, folder, tmp_path):
    write(folder, "other.py", """
        import pyvisa
        rm = pyvisa.ResourceManager()
        dev = rm.open_resource("GPIB0::%d::INSTR" % (2 + 3))   # an address worked out as it runs
        print(dev.query("*IDN?"))
    """)
    gate, request, report = run(lab, folder, tmp_path, "other.py", answers=(True, False))
    assert "GPIB0::5" not in str(request)
    assert "Let other.py open GPIB0::5::INSTR?" in str(gate.approver.requests[-1])
    assert report.status == "aborted" and "declined: other.py may not open GPIB0::5::INSTR" in report.message


def test_an_instrument_approved_up_front_is_not_asked_again_however_it_is_spelled(lab, folder, tmp_path,
                                                                                    monkeypatch):
    real_lab_name = scriptrun.lab_name
    write(folder, "spelled.py", """
        import pyvisa
        rm = pyvisa.ResourceManager()
        try:
            dev = rm.open_resource("GPIB0::5::INSTR")
            dev.query("*IDN?")
        except RuntimeError as e:
            print("opened as usual:", e)   # the test's fake pyvisa: it really tried to open it itself
        try:
            dev2 = rm.open_resource("GPIB::5")   # the same instrument, spelled as many scripts do
            dev2.query("*IDN?")
        except RuntimeError as e:
            print("opened as usual:", e)
    """)
    gate, request, report = run(lab, folder, tmp_path, "spelled.py")   # one answer: the run's approval
    assert "It also opens GPIB0::5::INSTR" in str(request)
    assert len(gate.approver.requests) == 1
    out = output(report)
    assert "opened as usual: the test's fake pyvisa has no instruments (GPIB0::5::INSTR)" in out
    assert "opened as usual: the test's fake pyvisa has no instruments (GPIB::5)" in out
    assert scriptrun.lab_name is real_lab_name


def test_a_makers_library_is_named_in_the_approval_or_asked_about_before_it_is_imported(lab, folder, tmp_path):
    (tmp_path / "fakes" / "zhinst").mkdir()
    (tmp_path / "fakes" / "zhinst" / "__init__.py").write_text("")
    (tmp_path / "fakes" / "nidaqmx").mkdir()
    (tmp_path / "fakes" / "nidaqmx" / "__init__.py").write_text("")
    write(folder, "uses_zhinst.py", """
        import importlib
        import zhinst                                  # read ahead: in the run's approval
        print("zhinst in")
        try:
            importlib.import_module("nida" + "qmx")   # hidden from the reading: asked when it is imported
        except ImportError as e:
            print("no nidaqmx:", e)
            raise
    """)
    gate, request, report = run(lab, folder, tmp_path, "uses_zhinst.py", answers=(True, False))
    assert "It uses zhinst, which reaches instruments without Kelvin seeing the commands" in str(request)
    assert "nidaqmx" not in str(request)
    assert [r.title for r in gate.approver.requests][1:] == ["Let uses_zhinst.py use nidaqmx?"]
    out = output(report)
    assert "zhinst in" in out and "no nidaqmx: Kelvin: declined" in out
    assert report.status == "aborted"


def test_a_notebook_runs_its_code_cells(lab, folder, tmp_path):
    cells = [{"cell_type": "markdown", "source": ["# Gate sweep\n"]},
             {"cell_type": "code", "source": ["%matplotlib inline\n", "import pyvisa\n",
                                              "lockin = pyvisa.ResourceManager().open_resource('GPIB0::8::INSTR')\n"]},
             {"cell_type": "code", "source": ["print('X =', lockin.query('MEAS:X?'))\n"]}]
    (folder / "look.ipynb").write_text(json.dumps({"cells": cells, "nbformat": 4, "nbformat_minor": 5}))
    gate, request, report = run(lab, folder, tmp_path, "look.ipynb")
    assert report.status == "done", report.message + output(report)
    assert "X = " in output(report)
    assert "# %matplotlib inline" in (Path(report.path) / "script.py").read_text()


def test_a_connection_without_the_runs_token_is_dropped(lab, folder, tmp_path):
    write(folder, "noop.py", "print('hi')\n")
    prep = scriptrun.prepare(lab, "noop.py", [folder], choose_python=_python)
    job = scriptrun.ScriptRun(SafetyGate(lab, ScriptedApprover([])), prep, RunStore(None), {}, set())

    class Conn:
        ok = False
        sock = None
    assert job._serve(Conn(), b'{"op": "hello", "token": "guess", "id": 1}', {}) is False
    assert job._serve(Conn(), b'{"op": "command", "instrument": "smu", "command": "OUTP ON", "id": 1}', {}) is False


def test_only_scripts_in_the_lab_scripts_folder_run(lab, folder, tmp_path):
    outside = write(tmp_path, "elsewhere.py", "print('hi')\n")
    with pytest.raises(scriptrun.ScriptError, match="outside the lab's scripts folder"):
        scriptrun.locate(str(outside), [folder])
    with pytest.raises(scriptrun.ScriptError, match="no scripts folder"):
        scriptrun.locate("elsewhere.py", [])
    write(folder, "measure.m", "x = 1;\n")
    with pytest.raises(scriptrun.ScriptError, match="MATLAB script"):
        scriptrun.locate("measure.m", [folder])
    (folder / "sub").mkdir()
    write(folder / "sub", "deep.py", "print('deep')\n")
    assert scriptrun.locate("deep.py", [folder]).name == "deep.py"


def test_settings_change_in_place_and_keep_the_line_numbers():
    source = ('import os\nN = 10   # points\nNAME = "a"\nSPAN = [0,\n        1]\n\n'
              'def main(rate=0.5, *, on=True):\n    pass\n')
    found = {p.name: p for p in scriptrun.parameters(source)}
    assert found["N"].value == 10 and found["N"].note == "points"
    assert found["rate"].where == "main" and found["on"].value is True
    new, changes = scriptrun.with_params(source, {"N": "20", "SPAN": [0, 2], "rate": 1, "on": "off"})
    assert new.count("\n") == source.count("\n")
    assert "N = 20   # points" in new and "SPAN = [0, 2]" in new and "def main(rate=1.0, *, on=False):" in new
    assert changes[0] == "N = 20 (the script has 10)"
    with pytest.raises(scriptrun.ScriptError, match="has no setting VMAX"):
        scriptrun.with_params(source, {"VMAX": 1})
    with pytest.raises(scriptrun.ScriptError, match="is a number"):
        scriptrun.with_params(source, {"N": "ten"})


def test_run_script_over_mcp_with_a_person_approving_at_the_page(lab, folder, tmp_path):
    pytest.importorskip("mcp")
    anyio = pytest.importorskip("anyio")
    from mcp import Client
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.agent.server import build_server
    write(folder, "sweep.py", SWEEP)
    rt = LabRuntime(lab, WebApprover(timeout=20), tmp_path / "data")
    rt.scripts_folder = lambda: str(folder)
    rt.scripts_python = lambda: sys.executable
    rt.auto_report = "off"
    asked = []

    def person():
        entry = rt.approver.wait_for_pending(timeout=60)
        asked.append(entry)
        rt.approver.answer(entry["id"], True)
    threading.Thread(target=person, daemon=True).start()

    async def main():
        async with Client(build_server(rt)) as client:
            names = [t.name for t in (await client.list_tools()).tools]
            out = []
            for name in ("check_script", "run_script"):
                r = await client.call_tool(name, {"script": "sweep.py", "params": {"VMAX": 2}})
                out.append("\n".join(c.text for c in r.content if getattr(c, "type", "") == "text"))
            return names, out
    names, (checked, ran) = anyio.run(main)
    assert {"run_script", "check_script"} <= set(names)
    assert "VMAX = 1.0   # the top of the sweep, in volts" in checked
    assert "smu.V: now 0 V, the script uses 0 … 2 V" in checked
    assert asked and asked[0]["title"] == "Run script sweep.py?"
    assert "script sweep.py done" in ran, ran
    assert "assessment:" in ran or "its readings: 6 rows of lockin.X, lockin.Y against smu.V" in ran   # no numpy
    assert "rows: get_run_data" in ran and "files it wrote beside the script: sweep.dat" in ran
    assert "<outside_text from=\"the lab's script output\">" in ran
    run_id = re.search(r"get_run_data\(run_id='([^']+)'\)", ran).group(1)
    assert rt.describe_run(run_id)   # in the catalog like any run


def test_run_script_needs_the_lab_scripts_folder(lab, tmp_path):
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    rt = LabRuntime(lab, WebApprover(timeout=1), tmp_path / "data")
    assert "script not run: this lab has no scripts folder" in rt.run_script("sweep.py")


def test_the_python_that_has_what_the_script_imports(tmp_path):
    script = write(tmp_path, "s.py", "import json\nimport no_such_module_anywhere\n")
    source = script.read_text()
    with pytest.raises(scriptrun.ScriptError, match="lacks no_such_module_anywhere"):
        scriptrun.python_for(script, source, [tmp_path], configured=sys.executable)
    (tmp_path / "no_such_module_anywhere.py").write_text("")   # the lab's own module, beside the script
    scriptrun._PROBED.clear()
    py = scriptrun.python_for(script, source, [tmp_path], configured=sys.executable)
    assert py.version == tuple(sys.version_info[:2])
