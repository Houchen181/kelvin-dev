"""Field report 2026-11-15, the lab's scripts: an NV pulsed-measurement rig brought up on Windows 11 (a QICK board, a
Cobolt 06-MLD 520 nm laser on COM11, an IDQ Time Controller, and the lab's own package nvpulse, run as python -m
nvpulse from a conda environment).

Item 7: tools\\cobolt_probe.py opened COM11, the laser's port, after a card that said only "COM11, which is not in
this lab: Kelvin can't check what it sends there". It sent 48 queries past the gate while the laser emitted about 40 mW,
and Kelvin saw none of it. Now the card names the device behind the port, says plainly when it is a light source, and
how to bring it into the lab first; a person still approves the run as before.
Item 8: nvpulse failed with "could not open port 'COM11': PermissionError(13, 'Access is denied.')". CoboltMonitor.exe
held the port, which the person had to find. Now Kelvin names the program that holds a port (portholders.py): where a
script's open failed, where its own did, in a scan and in connected_devices.
Item 10: the chat's folder was also the lab's scripts folder, so Kelvin could save nothing there, not even a report in
a new subfolder. Now it may make a subfolder of its own there for notes, tables and pictures, and nothing else.
Item 12: calculate took one expression; a list is answered too, each on its own line.
Item 1 (checked, not changed): a package's __main__.py given by its path runs as python -m runs it, and a relative
import that fails says how to run it: test_module_scripts.py covers both."""
import asyncio
import dataclasses
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from labharness import folderfiles, portholders, probe, scriptrun
from labharness.eventlog import EventLog
from labharness.gate import SafetyGate, ScriptedApprover

from test_script_runs import _python, folder, lab, output, run, script_runtime, write  # noqa: F401 (fixtures)

COBOLT_MONITOR = {"pid": 4242, "name": "CoboltMonitor.exe", "path": r"C:\Program Files\Cobolt\CoboltMonitor.exe",
                  "kelvin": False}


class Port:
    """One port as pyserial's list_ports.comports() gives it."""

    def __init__(self, device, description, vid=None, pid=None, manufacturer=None, product=None, interface=None,
                 serial_number=None):
        self.device, self.description, self.vid, self.pid = device, description, vid, pid
        self.manufacturer, self.product, self.interface, self.serial_number = manufacturer, product, interface, \
            serial_number
        self.hwid = "USB VID:PID=%04X:%04X" % (vid, pid) if vid is not None else "ACPI\\PNP0501\\1"


@pytest.fixture
def ports(monkeypatch):
    """pyserial's list of this computer's ports, in Kelvin's own process (the tests' Python has no pyserial): the
    ports the test puts in it."""
    listed = []
    serial = types.ModuleType("serial")
    tools = types.ModuleType("serial.tools")
    list_ports = types.ModuleType("serial.tools.list_ports")
    list_ports.comports = lambda include_links=False: list(listed)
    serial.tools, tools.list_ports = tools, list_ports
    for name, module in (("serial", serial), ("serial.tools", tools), ("serial.tools.list_ports", list_ports)):
        monkeypatch.setitem(sys.modules, name, module)
    return listed


COBOLT_PROBE = """
    import serial

    laser = serial.Serial("COM11", 115200, timeout=1)
    for _ in range(48):
        laser.write(b"l?\\r")
        laser.readline()
    """


# ------------------------------------------------------------------ item 7: the device behind an outside port

def test_a_scripts_outside_port_names_the_laser_behind_it(lab, folder, ports):
    """The field report: cobolt_probe.py opens COM11, the laser's port. Its card says what is there, that it is a light
    source Kelvin won't see emitting while the script runs, and how to bring it into the lab first; it asks a person
    as it did, and refuses nothing more."""
    ports.append(Port("COM11", "Cobolt 06-01 Series (COM11)", 0x25DC, 0x0006, "Cobolt AB", serial_number="18342"))
    write(folder, "cobolt_probe.py", COBOLT_PROBE)
    gate = SafetyGate(lab, ScriptedApprover([]), EventLog(None, lab.clock))
    prep = scriptrun.prepare(lab, "cobolt_probe.py", [folder], choose_python=_python)
    request, grants, problems = scriptrun.approval(gate, prep)
    assert not problems and request.title == "Run script cobolt_probe.py?"
    [line] = [i for i in request.items if i.startswith("It also opens serial://COM11")]
    assert "which is not in this lab: Kelvin can't check what it sends there" in line, line
    assert "COM11 is Cobolt 06-01 Series (COM11) (USB 25dc:0006, Cobolt AB)" in line, line
    assert "a light source" in line and "Kelvin won't see whether it emits" in line, line
    assert "add_instrument COM11" in line and "find_card" in line and "draft_card" in line, line
    assert "every change to its output asks a person until they have reviewed its limits" in line, line
    assert "A script can't be rehearsed on the simulated lab, so a person approves each run" in str(request)

    # found only as it runs: the person asked then is told the same
    said = scriptrun.outside(lab, "COM11")
    assert said.startswith("COM11, which is not in this lab") and "a light source" in said


def test_a_light_source_known_by_its_usb_id_is_named_with_its_card(lab, ports):
    """A laser whose USB name says nothing (a CDC port, "USB Serial Device"): the lab's card for its USB ID is a
    laser's, so it is a light source all the same, and the card to add it with is named."""
    card = dataclasses.replace(lab.registry.get("coherent/obis"), usb_ids=["0483:5740"])   # the lab's own copy
    lab.registry.put(card)
    ports.append(Port("COM12", "USB Serial Device (COM12)", 0x0483, 0x5740, "Microsoft"))
    said = scriptrun.outside(lab, "serial://COM12")
    assert "COM12 is USB Serial Device (COM12) (USB 0483:5740, Microsoft)" in said, said
    assert "a light source (Kelvin's card for its USB ID is coherent/obis, a laser)" in said, said
    assert "add_instrument COM12 with card coherent/obis" in said, said
    assert "find_card" not in said


def test_a_light_source_by_its_own_name_and_a_plain_port(lab, ports):
    """The name the device reports (pyserial's interface on Windows, product elsewhere) is read too; a device that is
    no light source is only named, and a port this computer doesn't list is said as before."""
    ports.append(Port("COM13", "USB Serial Device (COM13)", 0x1234, 0x5678, "Microsoft", interface="OBIS 488 LX"))
    ports.append(Port("COM9", "USB Serial Port (COM9)", 0x0403, 0x6001, "FTDI"))
    ports.append(Port("/dev/ttyUSB3", "Oxxius LaserBoxx", 0x0403, 0x6015, "Oxxius", product="LBX-488"))
    assert "a light source" in scriptrun.outside(lab, "COM13")
    assert "a light source" in scriptrun.outside(lab, "serial:///dev/ttyUSB3")
    plain = scriptrun.outside(lab, "COM9")
    assert plain == ("COM9, which is not in this lab: Kelvin can't check what it sends there. COM9 is USB Serial Port "
                     "(COM9) (USB 0403:6001, FTDI)"), plain
    assert scriptrun.outside(lab, "COM30") == "COM30, which is not in this lab: Kelvin can't check what it sends there"
    assert scriptrun.outside(lab, "GPIB0::5::INSTR") == \
        "GPIB0::5::INSTR, which is not in this lab: Kelvin can't check what it sends there"


def test_without_pyserial_the_card_reads_as_before(lab):
    """The tests' Python has no pyserial, as a Kelvin from source may not: nothing listed, nothing said of it."""
    if "serial" in sys.modules or _has_pyserial():
        pytest.skip("pyserial is here")
    assert scriptrun.outside(lab, "COM11") == "COM11, which is not in this lab: Kelvin can't check what it sends there"


def _has_pyserial():
    try:
        import serial.tools.list_ports  # noqa: F401
        return True
    except ImportError:
        return False


def test_check_script_says_it_too(lab, folder, tmp_path, ports):
    pytest.importorskip("mcp")
    ports.append(Port("COM11", "Cobolt 06-01 Series (COM11)", 0x25DC, 0x0006, "Cobolt AB"))
    write(folder, "cobolt_probe.py", COBOLT_PROBE)
    said = script_runtime(lab, folder, tmp_path).check_script("cobolt_probe.py")
    assert "run_script asks a person once:" in said
    assert "It also opens serial://COM11, which is not in this lab" in said and "a light source" in said, said


def test_connected_devices_flags_a_light_source_and_says_who_holds_its_port(lab, folder, tmp_path, monkeypatch):
    """Items 7 and 8 in connected_devices: the laser's port, the program that has it open, and how to bring it in; a
    port of the lab's own is the lab's; programs with VISA loaded, one of which may hold an instrument's session."""
    pytest.importorskip("mcp")
    monkeypatch.setattr(probe, "usb_devices", lambda: [
        {"vid": 0x25DC, "pid": 0x0006, "serial": "18342", "names": ["Cobolt 06-01 Series"], "maker": "Cobolt AB",
         "drivers": ["usbser"], "classes": ["Ports"], "problem": 0}])
    monkeypatch.setattr(probe, "serial_ports", lambda: [
        {"port": "COM11", "description": "Cobolt 06-01 Series (COM11)", "vid": 0x25DC, "pid": 0x0006,
         "serial": "18342", "maker": "Cobolt AB", "product": "", "interface": ""},
        {"port": "COM7", "description": "Communications Port (COM7)", "vid": None, "pid": None, "serial": "",
         "maker": "", "product": "", "interface": ""}])
    monkeypatch.setattr(probe, "network_adapters", lambda: [])
    asked = []

    def holders_of(names, timeout=portholders.LOOK_S):
        asked.append(sorted(names))
        return {n: [COBOLT_MONITOR] if n == "COM11" else [] for n in names}
    monkeypatch.setattr(portholders, "holders_of", holders_of)
    monkeypatch.setattr(portholders, "visa_users", lambda timeout=portholders.LOOK_S, names=None: [
        {"pid": 77, "name": "LabVIEW.exe", "path": "", "kelvin": False}])
    said = script_runtime(lab, folder, tmp_path).connected_devices()
    assert asked == [["COM11", "COM7"]]                                    # one look for every port
    assert "serial port COM11 (open in CoboltMonitor.exe, process 4242)" in said, said
    assert "a light source not in this lab" in said and "add_instrument COM11" in said, said
    assert "COM7 (Communications Port (COM7); the lab's laser)" in said, said
    assert "Programs with VISA loaded (one may hold an instrument's VISA session): LabVIEW.exe (process 77)" in said


# ------------------------------------------------------------------ item 8: who holds a port

HOLD = """
import os, sys
f = open(sys.argv[1], "rb")
print("held", os.getpid(), flush=True)
sys.stdin.readline()
f.close()
print("let go", flush=True)
sys.stdin.readline()
"""


def _started(child, word):
    """The child's own process id, once it says word: a venv's python.exe on Windows is a launcher that starts the
    real interpreter as its child, so Popen's pid is not the one holding anything."""
    said = child.stdout.readline().split()
    assert said[:1] == [word], said
    return int(said[1])


def _end(child):
    child.stdin.close()   # the interpreter behind a launcher ends at the end of its input
    child.kill()
    child.wait(10)


def _device():
    """A character device a child process can hold, as a port is held: NUL on Windows, a pseudo-terminal elsewhere
    (kept open here by its other end only)."""
    if sys.platform == "win32":
        return "NUL", None
    master, slave = os.openpty()
    path = os.ttyname(slave)
    os.close(slave)
    return path, master


@pytest.mark.skipif(sys.platform == "darwin", reason="a Mac doesn't say who holds a port")
def test_the_program_holding_a_device_is_named(tmp_path):
    """For real: a child process holds the device, and the look names it, by its process and its program; once it lets
    go, it is no longer named. On Linux a link to the device (as /dev/serial/by-id/... is) names the same holder."""
    path, master = _device()
    child = subprocess.Popen([sys.executable, "-c", HOLD, path], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        pid = _started(child, b"held")
        found = portholders.holders(path)
        mine = [h for h in found if h["pid"] == pid]
        assert mine, found
        assert mine[0]["name"].lower().startswith("python") and not mine[0]["kelvin"], mine
        assert mine[0]["path"].lower().endswith(mine[0]["name"].lower()), mine
        if master is not None:   # a link to it names the same holder; this process holds only the other end
            link = tmp_path / "by-id-laser"
            link.symlink_to(path)
            assert [h["pid"] for h in portholders.holders(str(link))] == [pid]
        held = portholders.held_text(path)
        assert "is open in" in held and "(process %d)" % pid in held, held
        child.stdin.write(b"\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == b"let go"
        assert pid not in [h["pid"] for h in portholders.holders(path)]
    finally:
        _end(child)
        if master is not None:
            os.close(master)


def test_a_port_nobody_can_name_says_nothing_and_never_raises():
    for port in ("COM999", "/dev/ttyNOPE9", "", "GPIB0::5::INSTR", "not a port at all"):
        assert portholders.holders(port) == [], port
        assert portholders.held_text(port) == "", port
    assert portholders.port_name("ASRL11::INSTR") == "COM11" and portholders.port_name(r"\\.\COM11") == "COM11"
    assert portholders.port_name("serial://COM11") == "COM11" and portholders.port_name("com11") == "COM11"
    assert portholders.port_name("serial:///dev/ttyUSB0") == "/dev/ttyUSB0"
    assert portholders.port_name("ASRL/dev/ttyACM1::INSTR") == "/dev/ttyACM1"
    assert portholders.port_name("GPIB0::5::INSTR") is None


def test_a_look_that_blocks_never_holds_kelvin():
    """A handle whose name never comes (a console's, waiting for input, which Windows answers only after the input) is
    left after STEP_S, remembered, and never asked again; the walk goes on with the rest, and the whole look ends by
    its deadline whatever happens."""
    release = threading.Event()
    seen = []

    def step(item, found):
        seen.append(item)
        if item == "stuck":
            release.wait(30)
        else:
            found.add(item)
    skip = set()
    t0 = time.monotonic()
    found = portholders._walk(["a", "stuck", "b"], step, skip, timeout=5.0, step_s=0.3)
    assert time.monotonic() - t0 < 3.0
    assert found == {"a", "b"} and skip == {"stuck"}
    seen.clear()
    assert portholders._walk(["a", "stuck", "b"], step, skip, timeout=5.0, step_s=0.3) == {"a", "b"}
    assert "stuck" not in seen                                            # never asked again
    t0 = time.monotonic()
    portholders._walk(["x%d" % k for k in range(3)] + ["slow"], lambda item, found: release.wait(30), set(),
                      timeout=0.5, step_s=10.0)
    assert time.monotonic() - t0 < 2.0                                    # the deadline, whatever the steps do
    release.set()


@pytest.mark.skipif(sys.platform == "darwin", reason="a Mac doesn't say which programs have a library loaded")
def test_programs_with_a_library_loaded_are_listed():
    """VISA's library, here a module every Python with ctypes has (the tests' computer has no VISA): a child process
    that loaded it is listed, Kelvin's own process never."""
    child = subprocess.Popen([sys.executable, "-c", "import ctypes, os, sys\nprint('in', os.getpid(), flush=True)\n"
                              "sys.stdin.read()"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        pid = _started(child, b"in")
        import ctypes  # noqa: F401 (this process has it too: never listed)
        users = portholders.visa_users(names=("_ctypes",), keep=False)
        assert pid in [u["pid"] for u in users], users
        assert os.getpid() not in [u["pid"] for u in users]
        assert portholders.visa_users(names=("no_such_library_anywhere",), keep=False) == []
    finally:
        _end(child)


def test_a_scripts_port_that_another_program_holds_is_named_in_its_failure(lab, folder, tmp_path, monkeypatch):
    """The field report: nvpulse's open of COM11 failed with PermissionError 13. The run's failure names the program
    that has the port open, also when the script printed the error and only ended with exit code 1."""
    asked = []

    def holders(port, timeout=portholders.LOOK_S):
        asked.append(port)
        return [COBOLT_MONITOR] if port == "COM11" else []
    monkeypatch.setattr(portholders, "holders", holders)
    write(folder, "pulsed.py", """
        import serial
        try:
            laser = serial.Serial("COM11", 115200, timeout=1)
        except RuntimeError:   # the test's fake pyserial: as a real one fails when another program has the port
            raise IOError("could not open port 'COM11': PermissionError(13, 'Access is denied.', None, 5)")
        """)
    gate, request, report = run(lab, folder, tmp_path, "pulsed.py")
    assert report.status == "aborted", report.message
    assert "could not open port 'COM11'" in report.message
    assert "COM11 is open in CoboltMonitor.exe (process 4242): close it there, then run the script again" in \
        report.message, report.message
    assert asked == ["COM11"]

    write(folder, "quiet.py", """
        import sys
        print("nvpulse: could not open port 'COM11': PermissionError(13, 'Access is denied.', None, 5)")
        sys.exit(1)
        """)
    gate, request, report = run(lab, folder, tmp_path, "quiet.py")
    assert "it ended with exit code 1" in report.message
    assert "COM11 is open in CoboltMonitor.exe (process 4242)" in report.message, report.message

    asked.clear()
    write(folder, "fails.py", "raise ValueError('a plain mistake')\n")
    gate, request, report = run(lab, folder, tmp_path, "fails.py")
    assert report.status == "aborted" and "is open in" not in report.message and asked == []


def test_a_port_nobody_is_seen_holding_says_where_to_look(lab, folder, tmp_path, monkeypatch):
    monkeypatch.setattr(portholders, "holders", lambda port, timeout=portholders.LOOK_S: [])
    monkeypatch.setattr(portholders, "device_of", lambda port: r"\Device\VCP0")
    write(folder, "pulsed.py", """
        raise IOError("could not open port 'COM11': PermissionError(13, 'Access is denied.', None, 5)")
        """)
    gate, request, report = run(lab, folder, tmp_path, "pulsed.py")
    assert "another program has COM11 open, and Kelvin can't see which" in report.message, report.message
    if sys.platform == "win32":
        assert r"\Device\VCP0" in report.message and "Resource Monitor" in report.message


def test_kelvins_own_open_of_a_port_names_who_holds_it(monkeypatch):
    from labharness.adapters import AdapterError, VisaBackend
    from labharness.homelab import builtin_registry
    monkeypatch.setattr(portholders, "holders", lambda port, timeout=portholders.LOOK_S:
                        [COBOLT_MONITOR] if port == "COM11" else [])

    class Busy:
        def open_resource(self, name, **kwargs):
            raise RuntimeError("VI_ERROR_RSRC_BUSY (-1073807246): The resource is valid, but VISA cannot currently "
                               "access it.")
    with pytest.raises(AdapterError) as e:
        VisaBackend("laser", builtin_registry().get("coherent/obis"), "serial://COM11", rm=Busy())
    assert "cannot open ASRL11::INSTR" in str(e.value)
    assert "COM11 is open in CoboltMonitor.exe (process 4242)" in str(e.value), str(e.value)


def test_a_scan_names_who_holds_a_port_it_couldnt_open(monkeypatch):
    from labharness import discovery, others
    from test_first_start_sets_up_what_it_can import Counting, Device, survey_of
    monkeypatch.setattr(discovery, "_usb_ids", lambda: {})
    monkeypatch.setattr(others, "holders", lambda: [])
    monkeypatch.setattr(portholders, "holders", lambda port, timeout=portholders.LOOK_S:
                        [COBOLT_MONITOR] if port == "COM5" else [])
    rm = Counting({"ASRL5::INSTR": Device(silent=True)}, refuse={"ASRL5::INSTR"})
    found, _ = survey_of(rm)
    how = found[0].how
    assert "could not open it" in how and "COM5 is open in CoboltMonitor.exe (process 4242)" in how, how


def test_inspect_serial_names_who_holds_the_port(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    from simlab import build_virtual_lab

    def busy(port, baud, parity, bits, timeout):
        raise OSError("could not open port '%s': PermissionError(13, 'Access is denied.', None, 5)" % port)
    monkeypatch.setattr(probe, "_open_serial", busy)
    monkeypatch.setattr(portholders, "holders", lambda port, timeout=portholders.LOOK_S:
                        [COBOLT_MONITOR] if port == "COM11" else [])
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    try:
        said = rt.inspect_serial("COM11")
    finally:
        rt.close_instruments(5)
    assert said.startswith("could not open COM11") and "COM11 is open in CoboltMonitor.exe (process 4242)" in said, said


# ------------------------------------------------------------------ item 10: Kelvin's own subfolder there

def _scripts_lab(tmp_path):
    """A chat's folder that is also the lab's scripts folder, as the field report's was."""
    lab = tmp_path / "nv-lab"
    (lab / "nvpulse").mkdir(parents=True)
    (lab / "nvpulse" / "__init__.py").write_text("")
    (lab / "tools").mkdir()
    (lab / "tools" / "cobolt_probe.py").write_text("import serial\n")
    (lab / "data").mkdir()
    return lab, dict(own=[], data=tmp_path / "kelvin-data", kept=[str(lab)])


def test_kelvin_saves_a_report_in_a_new_subfolder_of_the_scripts_folder(tmp_path):
    """The field report: write_file of kelvin_reports\\nv_bringup.md was refused. A new folder named kelvin_... is
    Kelvin's own: it saves notes, tables and pictures there, and finds it its own after a restart (a marker in it)."""
    lab, kw = _scripts_lab(tmp_path)
    target, kept = folderfiles.save(lab, r"kelvin_reports/nv_bringup.md", "# NV bring-up\n", **kw)
    assert target == (lab / "kelvin_reports" / "nv_bringup.md").resolve() and kept is None
    assert (lab / "kelvin_reports" / folderfiles.MADE).is_file()
    for name in ("odmr.csv", "rabi.tsv", "notes.txt", "run.log", "plot.svg"):
        folderfiles.save(lab, "kelvin_reports/" + name, "x\n", **kw)
    folderfiles.save(lab, "kelvin_reports/2026-11-15/summary.md", "day\n", **kw)   # deeper, made new: its own too
    assert (lab / "kelvin_reports" / "2026-11-15" / folderfiles.MADE).is_file()
    # again, as after a restart: the marker says whose it is; a file Kelvin saved there it may replace (kept first)
    target, kept = folderfiles.save(lab, "kelvin_reports/nv_bringup.md", "# NV bring-up, v2\n", **kw)
    assert target.read_text() == "# NV bring-up, v2\n" and kept is not None and kept.read_text() == "# NV bring-up\n"
    listed = folderfiles.listing(lab, "kelvin_reports")
    assert folderfiles.MADE not in listed and "nv_bringup.md" in listed


def test_in_the_scripts_folder_kelvin_never_saves_code_nor_in_the_persons_folders(tmp_path):
    lab, kw = _scripts_lab(tmp_path)
    folderfiles.save(lab, "kelvin_reports/notes.md", "ok\n", **kw)
    refused = {
        # code and settings, even in its own subfolder
        "kelvin_reports/helper.py": "notes, tables and pictures", "kelvin_reports/odmr.ipynb": "notes, tables",
        "kelvin_reports/run.bat": "notes, tables", "kelvin_reports/setup.ps1": "notes, tables",
        "kelvin_reports/settings.json": "notes, tables", "kelvin_reports/config.yaml": "notes, tables",
        "kelvin_reports/rig.ini": "notes, tables", "kelvin_reports/pyproject.toml": "notes, tables",
        "kelvin_reports/sweep.vi": "notes, tables", "kelvin_reports/Makefile": "notes, tables",
        "newpkg/__init__.py": "notes, tables", "kelvin_reports/x.pth": "notes, tables",
        # the person's own folders, and the scripts folder itself
        "report.md": "named kelvin_", "data/odmr.csv": "named kelvin_", "nvpulse/notes.md": "named kelvin_",
        "tools/README.md": "named kelvin_",
    }
    for rel, why in refused.items():
        with pytest.raises(folderfiles.FolderError, match="only a person changes it") as e:
            folderfiles.save(lab, rel, "x\n", **kw)
        assert why in str(e.value), (rel, str(e.value))
    assert not (lab / "newpkg").exists() and not (lab / "report.md").exists()
    assert "lab's scripts folder" in str(e.value)

    # a file the person put in Kelvin's folder, or a folder they made in it: theirs
    (lab / "kelvin_reports" / "theirs.md").write_text("mine\n")
    (lab / "kelvin_reports" / "by_hand").mkdir()
    for rel in ("kelvin_reports/theirs.md", "kelvin_reports/by_hand/x.md"):
        with pytest.raises(folderfiles.FolderError, match="only a person changes it"):
            folderfiles.save(lab, rel, "x\n", **kw)
    assert (lab / "kelvin_reports" / "theirs.md").read_text() == "mine\n"
    folderfiles.save(lab, "kelvin_reports/mine.md", "x\n", **kw)                 # beside them: its own, fine
    # its mark, in the scripts folder or anywhere else: only Kelvin writes it, as it makes a folder of its own
    for where, more in ((lab, kw), (tmp_path, dict(own=[], data=tmp_path / "kelvin-data"))):
        for rel in ("kelvin_reports/" + folderfiles.MADE, "data/" + folderfiles.MADE, "elsewhere/.KELVIN-MADE"):
            with pytest.raises(folderfiles.FolderError, match="Kelvin's own mark"):
                folderfiles.save(where, rel, '{"files": ["calib.csv"]}', **more)
    assert not (lab / "data" / folderfiles.MADE).exists()


def test_a_new_folder_of_any_other_name_is_refused(tmp_path):
    """Review of these fixes, finding 1: a new folder of any name took notes, so cal/offsets.csv, which pulse.py reads
    when it is there, set the laser power of a script a person had approved as it was; and nidaqmx/ would be a
    package. Only a folder named kelvin_... (or kelvin-...) is Kelvin's to make, right in the scripts folder, then
    any folder it made inside one; never one of those names the person made, nor one in the person's folders."""
    lab, kw = _scripts_lab(tmp_path)
    script = lab / "pulse.py"
    script.write_text("import os\n"
                      "p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cal', 'offsets.csv')\n"
                      "power_mW = float(open(p).read().split(',')[1]) if os.path.exists(p) else 1.0\n"
                      "print('laser power', power_mW)\n")
    before = subprocess.run([sys.executable, str(script)], capture_output=True, text=True).stdout
    for rel in ("cal/offsets.csv", "nidaqmx/notes.md", "output/notes.md", "kelvin/notes.md", "kelvin_/notes.md",
                "kelvin_shim/notes.md", "kelvin_api/notes.md", "KELVINreports/notes.md"):
        with pytest.raises(folderfiles.FolderError, match="no name of Kelvin's") as e:
            folderfiles.save(lab, rel, "power_mW,80\n", **kw)
        assert "named kelvin_" in str(e.value) and "only a person changes it" in str(e.value), (rel, str(e.value))
    assert not (lab / "cal").exists() and not (lab / "nidaqmx").exists()
    assert subprocess.run([sys.executable, str(script)], capture_output=True, text=True).stdout == before
    assert "laser power 1.0" in before

    for rel in ("kelvin-notes/odmr.csv", "Kelvin_Reports2/x.md", "kelvin_cal/offsets.csv",
                "kelvin_reports/day 1/cal/offsets.csv"):   # inside a folder of Kelvin's own: any name
        folderfiles.save(lab, rel, "power_mW,80\n", **kw)
    assert subprocess.run([sys.executable, str(script)], capture_output=True, text=True).stdout == before
    (lab / "kelvin_old").mkdir()                       # a folder so named that the person made: theirs
    (lab / "data" / "kelvin_cal").mkdir()
    for rel in ("kelvin_old/x.md", "data/kelvin_cal/x.md", "data/kelvin_new/x.md"):
        with pytest.raises(folderfiles.FolderError, match="person's own folders"):
            folderfiles.save(lab, rel, "x\n", **kw)
    assert not (lab / "data" / "kelvin_new").exists()


def test_a_folder_that_appears_while_kelvin_saves_is_never_claimed(tmp_path, monkeypatch):
    """Review of these fixes, finding 4: a folder a running script made between write_file's check and its mkdir was
    marked as Kelvin's, and Kelvin then saved among that script's output. Each folder is made by this call or refused,
    and only one it made is marked."""
    lab, kw = _scripts_lab(tmp_path)
    real_mkdir = Path.mkdir

    def mkdir(self, *a, **k):   # the person's script makes kelvin_output/ and writes in it, just before Kelvin's mkdir
        if self.name == "kelvin_output" and not self.exists():
            real_mkdir(self)
            (self / "trace.csv").write_text("person's data\n")
        return real_mkdir(self, *a, **k)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises(folderfiles.FolderError, match="appeared while Kelvin was saving"):
        folderfiles.save(lab, "kelvin_output/notes.md", "x\n", **kw)
    monkeypatch.setattr(Path, "mkdir", real_mkdir)
    with pytest.raises(folderfiles.FolderError, match="person's own folders"):
        folderfiles.save(lab, "kelvin_output/more.csv", "kelvin's\n", **kw)
    assert folderfiles.made(lab / "kelvin_output") is None
    assert sorted(p.name for p in (lab / "kelvin_output").iterdir()) == ["trace.csv"]


def test_a_file_that_couldnt_be_saved_is_never_listed_as_kelvins(tmp_path, monkeypatch):
    """Found with the review's probe: a save that failed (AUX.csv, a name Windows keeps for itself) was listed in the
    folder's mark all the same, so a person's file of that name would have been Kelvin's to replace. And .KELVIN-OLD
    is .kelvin-old on Windows: no file is saved among the versions Kelvin kept, however it is written."""
    lab, kw = _scripts_lab(tmp_path)
    folderfiles.save(lab, "kelvin_reports/a.md", "x\n", **kw)
    real = os.replace

    def replace(src, dst):
        if str(dst).endswith("b.md"):
            raise PermissionError(13, "Permission denied")
        return real(src, dst)
    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(folderfiles.FolderError, match="could not save"):
        folderfiles.save(lab, "kelvin_reports/b.md", "x\n", **kw)
    monkeypatch.setattr(os, "replace", real)
    assert folderfiles.made(lab / "kelvin_reports") == [os.path.normcase("a.md")]
    (lab / "kelvin_reports" / "b.md").write_text("the person's\n")
    with pytest.raises(folderfiles.FolderError, match="didn't save"):
        folderfiles.save(lab, "kelvin_reports/b.md", "x\n", **kw)
    assert (lab / "kelvin_reports" / "b.md").read_text() == "the person's\n"
    for rel in ("kelvin_reports/.KELVIN-OLD/x.md", "kelvin_reports/.Kelvin-Old/a.2026-11-15_101010.md"):
        with pytest.raises(folderfiles.FolderError, match="the versions Kelvin kept"):
            folderfiles.save(lab, rel, "x\n", **kw)


def test_a_python_environment_in_the_scripts_folder_stays_the_persons(tmp_path):
    lab, kw = _scripts_lab(tmp_path)
    (lab / ".venv").mkdir()
    (lab / ".venv" / "pyvenv.cfg").write_text("home = x\n")
    for rel in (".venv/kelvin_reports/notes.md", ".venv/Lib/site-packages/kelvin_reports/notes.md"):
        with pytest.raises(folderfiles.FolderError, match="only a person changes it"):
            folderfiles.save(lab, rel, "x\n", **kw)
    assert not (lab / ".venv" / "kelvin_reports").exists()


def test_an_imported_scripts_folder_alike(tmp_path):
    chat = tmp_path / "desktop"
    chat.mkdir()
    script = chat / "measure.py"
    script.write_text("print(1)\n")
    kw = dict(own=[], data=tmp_path / "kelvin-data", kept=[str(script)])
    with pytest.raises(folderfiles.FolderError, match="in the folder of measure.py") as e:
        folderfiles.save(chat, "notes.md", "x\n", **kw)
    assert "named kelvin_" in str(e.value)
    with pytest.raises(folderfiles.FolderError, match="no name of Kelvin's"):
        folderfiles.save(chat, "results/notes.md", "x\n", **kw)
    folderfiles.save(chat, "kelvin_reports/notes.md", "x\n", **kw)
    with pytest.raises(folderfiles.FolderError, match="only a person changes it"):
        folderfiles.save(chat, "kelvin_reports/helpers.py", "x = 1\n", **kw)


def test_a_chat_whose_folder_is_the_scripts_folder_saves_its_report(lab, folder, tmp_path):
    """Through the chat's write_file, as the field report's chat did: the code refused, saying how, and the report in
    a new subfolder saved."""
    pytest.importorskip("mcp")
    from labharness.agent.server import build_server
    from labharness.web.agent import ChatAgent, WRITE_FILE
    from labharness.web.store import ChatStore
    from test_web import Scripted
    assert "named kelvin_" in WRITE_FILE["description"]
    write(folder, "sweep.py", "print('sweep')\n")
    rt = script_runtime(lab, folder, tmp_path)
    provider = Scripted([[{"type": "tool_call", "id": "w1", "name": "write_file",
                           "input": {"path": "fit.py", "content": "print('mine')\n"}},
                          {"type": "tool_call", "id": "w2", "name": "write_file",   # as Windows writes it there
                           "input": {"path": os.path.join("kelvin_reports", "nv_bringup.md"),
                                     "content": "# NV bring-up\n"}}],
                         [{"type": "text", "text": "Done."}]])
    store = ChatStore(tmp_path / "chats")
    agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
    chat = store.create("scripted:m1")
    chat["folder"] = str(folder)                                      # the person chose the scripts folder for it
    store.save(chat)

    async def emit(e):
        pass

    async def day():
        await agent.start()
        try:
            agent.claim(chat["id"], "scripted:m1")
            await agent.run_turn(chat["id"], "write up the bring-up", "scripted:m1", provider, emit)
        finally:
            await agent.close()
    asyncio.run(day())
    code, report = [r for m in store.load(chat["id"])["messages"] if m["role"] == "tool" for r in m["results"]]
    assert code["is_error"] and "named kelvin_" in code["content"], code
    assert not report["is_error"] and "nv_bringup.md" in report["content"], report
    assert not (folder / "fit.py").exists()
    on_disk = Path(folder) / "kelvin_reports" / "nv_bringup.md"
    assert on_disk.read_text() == "# NV bring-up\n"


# ------------------------------------------------------------------ item 12: calculate, a list

def test_calculate_answers_each_expression_of_a_list(tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    from simlab import build_virtual_lab
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    said = rt.calculate(["2*muB*0.35/h", "h*c/(1.55e-6*qe)", "Tc + 1", "sqrt(16)"]).splitlines()
    assert len(said) == 4, said
    assert said[0].startswith("2*muB*0.35/h = ") and float(said[0].split("= ")[1]) == pytest.approx(9.7974e9, rel=1e-4)
    assert said[1] == "h*c/(1.55e-6*qe) = 0.7998980544"
    assert said[2].startswith("Tc + 1: not calculated: 'Tc' is not a known constant"), said[2]
    assert said[3] == "sqrt(16) = 4"
    assert rt.calculate("sqrt(16)") == "sqrt(16) = 4"                    # one, as before
    assert rt.calculate("Tc + 1").startswith("not calculated: 'Tc' is not a known constant")
    assert rt.calculate("2+2\n\n3*3") == "2+2 = 4\n3*3 = 9"               # one per line
    assert rt.calculate([]).startswith("nothing to calculate")


def test_calculate_takes_a_list_over_mcp(tmp_path):
    pytest.importorskip("mcp")
    import anyio
    from mcp import Client
    from labharness.agent.runtime import LabRuntime
    from labharness.agent.server import build_server
    from simlab import build_virtual_lab
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")

    async def main():
        async with Client(build_server(rt)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            out = []
            for expression in (["1+1", "2*3"], "1+1"):
                r = await client.call_tool("calculate", {"expression": expression})
                out.append("\n".join(c.text for c in r.content if getattr(c, "type", "") == "text"))
            return tools["calculate"], out
    tool, (several, one) = anyio.run(main)
    assert several == "1+1 = 2\n2*3 = 6" and one == "1+1 = 2"
    assert "a list" in tool.description
    kinds = {alt.get("type") for alt in tool.input_schema["properties"]["expression"].get("anyOf", [])}
    assert kinds == {"string", "array"}, tool.input_schema
