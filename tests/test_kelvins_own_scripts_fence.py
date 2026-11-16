"""Auto mode's fence for Kelvin's own scripts, and the ways past the gate a review of 2026-11-15 found.

The review got runs auto mode approved to start programs, load native code and talk to a lab instrument's own port, past
what reading a script ahead could find. Since then auto mode runs only Kelvin's own scripts, as Kelvin saved them, and
every run it approves is fenced in its own process (kelvin_shim._fence, KELVIN_FENCE=auto): no programs, native code,
network, device files or registry changes, and writing only in its kelvin_ folder and the run's; what the fence refuses
ends the run, its drives back to safe. Ordinary lab code runs as it is (pyvisa through Kelvin's stand-in, NumPy, SciPy,
matplotlib, h5py; a package's own start before the fence goes up). In every run, a person's too, a raw socket's
connection to a lab instrument, its port opened as a file and a bare VISA session to it are refused (kelvin_shim._guard).
A queued or background run keeps the mark of outside text its chat had read; a script's printed output is outside text
over MCP as in the portal, and Kelvin's own check_script is not."""
import json
import os
import pickle
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from labharness import rundata, scriptrun
from labharness.clock import SimClock
from labharness.homelab import builtin_registry
from labharness.lab import Lab
from labharness.labconfig import load_lab_config
from labharness.sim.twin import World, sim_backend_factory

from test_kelvins_own_scripts import SWEEP, auto_approved, cards, commands, kelvin, last_run, own
from test_script_runs import LAB, folder, lab, write  # noqa: F401 (fixtures)

WINDOWS = sys.platform == "win32"
FENCED = scriptrun.FENCE_REFUSED.split("%s")[0]   # "auto mode's fence refused "
# a Python with SciPy and matplotlib (and Pillow 8.4, whose try: import cffi broke matplotlib under the fence)
SCIENCE = (r"C:\D\programs\Anaconda\envs\phys201\python.exe" if WINDOWS
           else os.path.expanduser("~/venvs/sym/bin/python"))


def ran(said):
    return said.splitlines()[0]


@pytest.fixture
def tcp_lab():
    """The test lab with lockin2 at a TCP address where this test listens, as the real instrument would."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    cfg = load_lab_config(LAB.replace("TCPIP0::127.0.0.1::5025::SOCKET", "TCPIP0::127.0.0.1::%d::SOCKET" % port),
                          builtin_registry())
    clock = SimClock()
    world = World(clock, 0)
    got = []

    def serve():
        listener.settimeout(60)
        try:
            conn, _ = listener.accept()
            conn.settimeout(10)
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                got.append(data)
        except OSError:
            pass
    threading.Thread(target=serve, daemon=True).start()
    yield Lab(cfg, builtin_registry(), sim_backend_factory(world), clock, world), port, got
    listener.close()


# ------------------------------------------------------------------ ordinary lab code runs fenced

LAB_CODE = r'''
import json, os, time
import numpy as np
import pyvisa
results = {}
def attempt(name, fn):
    try:
        fn()
        results[name] = "ok"
    except BaseException as e:
        results[name] = "%s: %s" % (type(e).__name__, str(e)[:200])
rm = pyvisa.ResourceManager()
smu = rm.open_resource("GPIB0::24::INSTR")
smu.write("OUTP ON")
volts = np.array([0.0, 0.5, 1.0])
cur = []
for v in (0.0, 0.5, 1.0):
    smu.write("SOUR:VOLT %g" % v)
    cur.append(float(smu.query("MEAS:CURR?")))
smu.write("SOUR:VOLT 0")
smu.write("OUTP OFF")
data = np.column_stack([volts, cur])
here = os.path.dirname(os.path.abspath(__file__))
run_dir = os.environ.get("KELVIN_RUN_DIR", ".")
attempt("np.savetxt beside the script", lambda: np.savetxt(os.path.join(here, "iv.csv"), data, delimiter=","))
attempt("np.save, relative path (cwd = its folder)", lambda: np.save("iv.npy", data))
attempt("open().write beside the script", lambda: open(os.path.join(here, "iv.txt"), "w").write("v,i\n"))
attempt("os.makedirs data/ beside the script", lambda: os.makedirs(os.path.join(here, "data"), exist_ok=True))
attempt("np.savetxt in data/", lambda: np.savetxt(os.path.join(here, "data", "iv.csv"), data))
attempt("np.savetxt in KELVIN_RUN_DIR", lambda: np.savetxt(os.path.join(run_dir, "iv.csv"), data, delimiter=","))
attempt("os.chdir into its folder", lambda: os.chdir(here))
'''
# packages of its Python's own (installed in its site-packages for the test: OWN_PACKAGES)
PKGS = r'''
def _optional():
    import OPTLIB   # its load() tries cffi, as Pillow 8.4 does: a module that isn't there, so it does without
    assert OPTLIB.load() is None
attempt("a package's optional import the fence refuses", _optional)
def _dllpkg():
    import DLLPKG  # noqa: F401  a stand-in for a wheel delvewheel patched: os.add_dll_directory as it loads
attempt("a package that adds its DLL folder as it loads", _dllpkg)
'''
H5 = r'''
def _h5():
    import h5py
    with h5py.File(os.path.join(here, "iv.h5"), "w") as f:
        f["iv"] = data
attempt("h5py.File beside the script", _h5)
'''
SCI = r'''
def _fit():
    import scipy.optimize, scipy.signal
    scipy.optimize.curve_fit(lambda x, a, b: a * x + b, volts, np.asarray(cur) + volts)
    scipy.signal.savgol_filter(np.arange(11.0), 5, 2)
attempt("scipy curve_fit and savgol", _fit)
def _plot():
    import matplotlib.pyplot as plt
    plt.figure()
    plt.plot(volts, cur)
    plt.savefig(os.path.join(run_dir, "iv-run-dir.png"))
    plt.savefig(os.path.join(here, "iv.png"))
attempt("matplotlib plot, savefig in the run's folder and beside", _plot)
'''
TAIL = r'''
print("RESULTS " + json.dumps(results))
'''


def results_of(rt, folder, name):
    _, run_dir = last_run(rt, own(folder, name + ".py"))
    out = (run_dir / "output.txt").read_text(encoding="utf-8", errors="replace")
    line = next((ln for ln in out.splitlines() if ln.startswith("RESULTS ")), None)
    assert line, out[-2000:]
    return json.loads(line[len("RESULTS "):]), run_dir


@pytest.fixture
def own_packages():
    """Packages of the scripts' Python's own (this one's), put in its site-packages for the test and taken out after,
    under names of their own: one that adds its DLL folder as it loads (as a wheel delvewheel patched: SciPy, pyzmq),
    one that tries cffi when asked (as Pillow 8.4 does for matplotlib), and one that takes a while to load (as
    matplotlib's first font list): {DLLPKG, OPTLIB, SLOWMOD: its name}. Only what comes from there is loaded before
    the fence (a module on PYTHONPATH is no package of its Python's: test_a_library_on_pythonpath_asks)."""
    import shutil
    import sysconfig
    import uuid
    site = Path(sysconfig.get_paths()["purelib"])
    tag = uuid.uuid4().hex[:8]
    names = {"DLLPKG": "kfence_dllpkg_" + tag, "OPTLIB": "kfence_optlib_" + tag, "SLOWMOD": "kfence_slow_" + tag}
    texts = {"DLLPKG": "import os\nlibs = os.path.dirname(os.path.abspath(__file__))\n"
                       "getattr(os, 'add_dll_directory', lambda p: None)(libs)\n",
             "OPTLIB": "def load():\n    try:\n        import cffi\n        return cffi\n"
                       "    except ImportError:\n        return None\n",
             "SLOWMOD": "import time\nt0 = time.perf_counter()\nwhile time.perf_counter() - t0 < 6:\n    pass\n"}
    for key, name in names.items():
        (site / name).mkdir()
        (site / name / "__init__.py").write_text(texts[key])
    yield names
    for name in names.values():
        shutil.rmtree(site / name, ignore_errors=True)


def named(code, names):
    for key, name in names.items():
        code = code.replace(key, name)
    return code


@pytest.mark.parametrize("python", ["Kelvin's own (NumPy, h5py)", "with SciPy and matplotlib"])
def test_ordinary_lab_code_runs_fenced_in_auto_mode(lab, folder, tmp_path, own_packages, python):
    """At low integrity too on Windows (its two folders labelled): the channel to Kelvin, the stand-in pyvisa, NumPy,
    h5py, SciPy, matplotlib and Pillow 8.4 all work there."""
    rt = kelvin(lab, folder, tmp_path, answers=[])   # a card would fail the test
    rt.gate.auto = True
    code = LAB_CODE
    if python.startswith("with"):
        if not Path(SCIENCE).is_file():
            pytest.skip("no Python with SciPy and matplotlib here (%s)" % SCIENCE)
        rt.scripts_python = lambda: SCIENCE
        code += SCI
    else:
        pytest.importorskip("h5py")
        code += H5 + named(PKGS, own_packages)
    rt.save_script("iv_sweep", code + TAIL)
    said = rt.run_script("kelvin_scripts/iv_sweep.py")
    assert "script iv_sweep.py done" in ran(said), said
    assert cards(rt) == [] and len(auto_approved(rt)) == 1
    assert "low integrity" not in said, said   # Windows' restriction set, and taken
    results, run_dir = results_of(rt, folder, "iv_sweep")
    assert all(v == "ok" for v in results.values()), results
    if not python.startswith("with"):   # the fence's own refusal, as a module that isn't there
        assert "warning: its process refused: import cffi" in said, said
    assert (folder / "kelvin_scripts" / "iv.csv").is_file() and (run_dir / "iv.csv").is_file()
    assert rundata.load(run_dir, with_rows=False).meta["approved"] == "auto mode"
    assert any(c.get("instrument") == "smu" and c["command"] == "SOUR:VOLT 1" for c in commands(run_dir))


# ------------------------------------------------------------------ what the fence refuses ends the run

HEAD = ("import sys\nM = %r\nCMD = ('type nul > \"%%s\"' %% M) if sys.platform == 'win32' else "
        "('touch \"%%s\"' %% M)\n")


class _System:
    def __init__(self, cmd):
        self.cmd = cmd

    def __reduce__(self):
        return (os.system, (self.cmd,))


def escapes(marker, port):
    """The review's ways past the gate (test_zz_probe_auto, _e2e, _fence), each doing what it would do past it."""
    cmd = ('type nul > "%s"' % marker) if WINDOWS else ('touch "%s"' % marker)
    device = ("'\\\\\\\\.\\\\' + 'COM9'" if WINDOWS else "'/dev/' + 'ttyS9'")
    dll = ("'kernel32', r'C:\\Windows\\System32'" if WINDOWS else "'libc.so.6', '/lib/x86_64-linux-gnu'")
    return {
        "importlib alias": "import importlib as il\nil.import_module('subprocess').run(CMD, shell=True)\n",
        "import_module from-import alias": ("from importlib import import_module as im\n"
                                            "im('subprocess').run(CMD, shell=True)\n"),
        "builtins.__import__": "import builtins\nbuiltins.__import__('subprocess').run(CMD, shell=True)\n",
        "builtins.exec": "import builtins\nbuiltins.exec('import subprocess; subprocess.run(CMD, shell=True)')\n",
        "getattr builtins": ("import builtins\ngetattr(builtins, 'ex' + 'ec')("
                             "'import subprocess; subprocess.run(CMD, shell=True)')\n"),
        "pickle.loads": "import pickle\npickle.loads(%r)\n" % pickle.dumps(_System(cmd), protocol=0),
        "runpy.run_path": "import runpy\nrunpy.run_path('helper_x.py')\n",
        "timeit stmt": ("import timeit\ntimeit.timeit('import subprocess; subprocess.run(CMD, shell=True)', number=1, "
                        "globals={'CMD': CMD})\n"),
        "write a module, then import it": ("open('gen_mod.py', 'w').write('import subprocess\\n"
                                           "subprocess.run(%r, shell=True)\\n' % CMD)\ntry:\n    import gen_mod\n"
                                           "except ImportError:\n    pass\n"),
        "asyncio subprocess": ("import asyncio\nasync def go():\n    p = await asyncio.create_subprocess_shell(CMD)\n"
                               "    await p.wait()\nasyncio.run(go())\n"),
        "webbrowser": "import webbrowser\nwebbrowser.open('file:///' + M)\n",
        "shutil.os.system": "import shutil\nshutil.os.system(CMD)\n",
        "os.path.os.system": "import os.path\nos.path.os.system(CMD)\n",
        "ProcessPoolExecutor": ("from concurrent.futures import ProcessPoolExecutor\n"
                                "with ProcessPoolExecutor(1) as ex:\n    ex.submit(print, 1).result()\n"),
        "numpy.ctypeslib loads a library": "import numpy.ctypeslib as npc\nnpc.load_library(%s)\n" % dll,
        "a raw _socket to the lab's lock-in": ("import _socket\nh = '.'.join(['127', '0', '0', '1'])\n"
                                               "s = _socket.socket()\ns.connect((h, %d))\ns.send(b'SLVL 4.5\\n')\n"
                                               % port),
        "a UDP datagram": ("import socket\nsocket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'OUTP ON', "
                           "('127.0.0.1', %d))\n" % port),
        "a device path in bytes": "open((%s).encode(), 'wb').write(b'OUTP ON')\n" % device,
        "a device path put together": "open(%s, 'wb').write(b'OUTP ON')\n" % device,
        "a device path from its settings": ("import json\ncfg = json.load(open('rig.json'))\n"
                                            "open(cfg['port'], 'wb').write(b'OUTP ON')\n"),
        "overwrite the person's script beside it": "open('../their_script.py', 'w').write('# changed\\n')\n",
        "write outside its folders": "open(%r, 'w').write('x')\n" % str(marker),
        "change into another folder": "import os\nos.chdir('..')\nopen('x.txt', 'w').write('x')\n",
        "ctypes (read ahead)": "import ctypes\n",
        "exec (read ahead)": "exec('import ctypes')\n",
        "subprocess (read ahead)": "import subprocess\nsubprocess.run(CMD, shell=True)\n",
    }


@pytest.mark.parametrize("which", list(escapes(Path("m"), 1)))
def test_each_escape_the_review_found_asks_or_ends_at_the_fence(tcp_lab, folder, tmp_path, which):
    lab, port, got = tcp_lab
    rt = kelvin(lab, folder, tmp_path, answers=[False])
    rt.gate.auto = True
    write(folder, "their_script.py", "print('the person wrote this')\n")
    marker = tmp_path / "reached.txt"
    if which == "numpy.ctypeslib loads a library":
        pytest.importorskip("numpy")
        if not WINDOWS and not Path("/lib/x86_64-linux-gnu/libc.so.6").is_file():
            pytest.skip("no libc there")
    if which == "a device path from its settings":
        from labharness import folderfiles
        folderfiles.save(folder, "kelvin_scripts/rig.json", json.dumps({"port": "COM9" if WINDOWS else "/dev/ttyS9"}),
                         own=[], data=tmp_path / "data", kept=[str(folder)])
    said = rt.save_script("escape", HEAD % str(marker) + escapes(marker, port)[which] + SWEEP)
    assert said.startswith("saved"), said
    said = rt.run_script("kelvin_scripts/escape.py")
    if cards(rt):   # read ahead: its card asks a person, saying why
        [card] = cards(rt)
        assert said.startswith("declined") and card.items[-1].startswith("Auto mode does not cover this run: "), said
    else:           # auto mode ran it, fenced: the fence (or the guard, for the lab's own instrument) refused it, and
        assert len(auto_approved(rt)) == 1   # the run ended there (asyncio's subprocess: the selector loop has none)
        assert ran(said).startswith("script escape.py stopped") and (
            FENCED in ran(said) or "refused: escape.py" in ran(said) or "NotImplementedError" in ran(said)), said
    time.sleep(0.3)
    assert not marker.exists() and got == []
    assert "the person wrote this" in (folder / "their_script.py").read_text()


def test_the_fence_ends_the_run_its_drives_go_back_to_safe_and_the_next_run_asks(lab, folder, tmp_path):
    rt = kelvin(lab, folder, tmp_path, answers=[False])
    rt.gate.auto = True
    marker = tmp_path / "program-ran.txt"
    code = SWEEP.replace('smu.write("SOUR:VOLT 0")\nsmu.write("OUTP OFF")\n', HEAD % str(marker) +
                         "import shutil\nshutil.os.system(CMD)\nsmu.write('SOUR:VOLT 0.2')\n")
    rt.save_script("halfway", code)
    said = rt.run_script("kelvin_scripts/halfway.py")
    assert ran(said).startswith("script halfway.py stopped") and FENCED + "os.system" in ran(said), said
    assert "to run it unfenced, a person approves it (turn auto mode off for this run, or ask)" in ran(said)
    assert not marker.exists() and cards(rt) == []
    assert lab.instrument("smu").read("V") == 0 and lab.instrument("smu").read("output") is False   # back to safe
    _, run_dir = last_run(rt, own(folder, "halfway.py"))
    assert not any(c.get("command") == "SOUR:VOLT 0.2" for c in commands(run_dir))   # nothing after the refusal
    # its next run would end there again: a person approves one unfenced
    assert rt.run_script("kelvin_scripts/halfway.py").startswith("declined")
    [card] = cards(rt)
    assert card.items[-1].startswith("Auto mode does not cover this run: its last run in auto mode (") and \
        "ended at auto mode's fence, which refused os.system" in card.items[-1], card.items[-1]


@pytest.mark.skipif(not WINDOWS, reason="os.add_dll_directory is Windows'")
def test_a_dll_folder_is_added_only_inside_its_python(lab, folder, tmp_path):
    rt = kelvin(lab, folder, tmp_path, answers=[])
    rt.gate.auto = True
    rt.save_script("dlls", "import os, sys\nos.add_dll_directory(os.path.dirname(sys.executable))\n"
                           "os.add_dll_directory(r'C:\\Windows')\n" + SWEEP)
    said = rt.run_script("kelvin_scripts/dlls.py")
    assert FENCED + "os.add_dll_directory of C:\\Windows, outside its Python" in ran(said), said


# ------------------------------------------------------------------ every run: a lab instrument only through Kelvin

def test_a_raw_socket_to_a_lab_instrument_is_refused_in_a_run_a_person_approved(tcp_lab, folder, tmp_path):
    """The review's raw _socket sent SLVL 4.5 to the lab's lock-in, and commands.jsonl had no record of it: the stand-in
    patched only socket.socket's methods. Refused now in every run, the person's own scripts' too, and the run ends."""
    lab, port, got = tcp_lab
    rt = kelvin(lab, folder, tmp_path, answers=[True, True])
    write(folder, "raw.py", "import _socket\nh = '.'.join(['127', '0', '0', '1'])\ns = _socket.socket()\n"
          "s.connect((h, %d))\ns.send(b'SLVL 4.5\\n')\n" % port)
    said = rt.run_script("raw.py")
    assert ran(said).startswith("script raw.py stopped"), said
    assert ("refused: raw.py connects a socket of its own to TCPIP0::127.0.0.1::%d::SOCKET, which is the lab's lockin2, "
            "past Kelvin's stand-in" % port) in ran(said), said
    write(folder, "udp.py", "import socket\nsocket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'SLVL 4.5', "
          "('127.0.0.1', %d))\n" % port)
    said = rt.run_script("udp.py")
    assert "refused: udp.py sends a datagram to TCPIP0::127.0.0.1::%d::SOCKET, which is the lab's lockin2" % port \
        in ran(said), said
    time.sleep(0.3)
    assert got == []


@pytest.fixture
def serial_lab():
    """The test lab, its laser on a serial port as this computer names one."""
    port = "serial://COM7" if WINDOWS else "serial:///dev/ttyUSB7"
    cfg = load_lab_config(LAB.replace("serial://COM7", port), builtin_registry())
    clock = SimClock()
    world = World(clock, 0)
    return Lab(cfg, builtin_registry(), sim_backend_factory(world), clock, world)


def test_a_lab_instruments_port_opened_as_a_file_is_refused(serial_lab, folder, tmp_path):
    rt = kelvin(serial_lab, folder, tmp_path, answers=[True])
    path = "'C' + 'OM7'" if WINDOWS else "'/dev/' + 'ttyUSB7'"
    write(folder, "direct.py", "f = open(%s, 'wb')\nf.write(b'POW 50\\n')\n" % path)
    said = rt.run_script("direct.py")
    assert "refused: direct.py opens " in ran(said) and "as a file, which is the lab's laser, past Kelvin's " \
        "stand-in" in ran(said), said


def test_a_device_outside_the_lab_asks_a_person_in_a_run_a_person_approved(lab, folder, tmp_path):
    rt = kelvin(lab, folder, tmp_path, answers=[True, False])
    path = "'C' + 'OM9'" if WINDOWS else "'/dev/' + 'ttyS9'"
    write(folder, "other.py", "f = open(%s, 'wb')\n" % path)
    said = rt.run_script("other.py")
    assert len(cards(rt)) == 2 and cards(rt)[1].title.startswith("Let other.py open "), [c.title for c in cards(rt)]
    assert "declined: other.py may not open" in ran(said), said


BARE_PYVISA = '''
MARK = %r
class _Library(object):
    def open(self, session, name, *args, **kwargs):
        open(MARK, "a").write("visalib.open %%s\\n" %% name)
        return 1, 0
class ResourceManager(object):
    def __new__(cls, *args, **kwargs):
        self = object.__new__(cls)
        self.visalib, self.session = _Library(), 0
        return self
    def open_resource(self, name, **kwargs):
        raise RuntimeError("the test's fake pyvisa has no instruments")
    def open_bare_resource(self, name, *args, **kwargs):
        return self.visalib.open(self.session, name)
    def list_resources(self, query="?*::INSTR"):
        return ()
'''


@pytest.mark.parametrize("how", ["open_bare_resource", "visalib.open"])
def test_a_bare_visa_session_to_a_lab_instrument_is_refused(lab, folder, tmp_path, how):
    marker = tmp_path / "bare.txt"
    (tmp_path / "fakes" / "pyvisa" / "__init__.py").write_text(BARE_PYVISA % str(marker))
    rt = kelvin(lab, folder, tmp_path, answers=[True])
    call = ("rm.open_bare_resource('GPIB0::24::INSTR')" if how == "open_bare_resource" else
            "rm.visalib.open(rm.session, 'GPIB0::24::INSTR')")
    write(folder, "bare.py", "import pyvisa\nrm = pyvisa.ResourceManager()\ns = %s\n" % call)
    said = rt.run_script("bare.py")
    assert "refused: bare.py opens a bare VISA session to GPIB0::24::INSTR, which is the lab's smu" in ran(said), said
    assert not marker.exists()


# ------------------------------------------------------------------ outside text: the queue, MCP and the portal

def test_a_queued_run_after_outside_text_asks(lab, folder, tmp_path):
    """A chat that read a web page: its own run_script asks, and so does the same script it queued, when the queue
    runs it later (the review: the queue's thread never saw the item's mark)."""
    rt = kelvin(lab, folder, tmp_path, answers=[False, False])
    rt.gate.auto = True
    rt.save_script("odmr_check", SWEEP)
    with rt.attributed({"outside": "a web page"}):
        rt.gate.in_chat, rt.gate.outside_chat = True, "a web page"   # as web/agent.py _work sets them
        try:
            assert rt.run_script("kelvin_scripts/odmr_check.py").startswith("declined")
            assert "this chat has read text from outside the lab" in cards(rt)[0].items[-1]
            assert rt.queue_add("script: kelvin_scripts/odmr_check.py\n").startswith("added script")
        finally:
            rt.gate.in_chat, rt.gate.outside_chat = False, None
    assert rt.queue.items[-1].outside == "a web page"
    assert rt.queue_start(by="agent").startswith("queue started")
    rt._queue_thread.join(120)
    assert len(cards(rt)) == 2 and auto_approved(rt) == []
    assert cards(rt)[1].items[-1] == ("Auto mode does not cover this run: a chat queued it after it read text from "
                                      "outside the lab (a web page), which could steer a model, so a person approves")


def test_a_background_run_after_outside_text_asks(lab, folder, tmp_path):
    rt = kelvin(lab, folder, tmp_path, answers=[False])
    rt.gate.auto = True
    rt.save_script("odmr_check", SWEEP)
    with rt.attributed({"outside": "a web page"}):
        rt.gate.in_chat, rt.gate.outside_chat = True, "a web page"
        try:
            said = rt.run_script("kelvin_scripts/odmr_check.py", background=True)
        finally:
            rt.gate.in_chat, rt.gate.outside_chat = False, None
    assert said.startswith("queue started"), said
    rt._queue_thread.join(120)
    assert rt.queue.items[-1].outside == "a web page" and len(cards(rt)) == 1 and auto_approved(rt) == []


def test_an_item_queued_over_mcp_keeps_the_web_text_read_before(lab, folder, tmp_path):
    rt = kelvin(lab, folder, tmp_path)
    rt.save_script("odmr_check", SWEEP)
    rt.gate.note_outside("a web page")   # an agent over MCP: the gate's own account, for a while
    assert rt.queue_add("script: kelvin_scripts/odmr_check.py\n").startswith("added script")
    assert rt.queue.items[-1].outside == "a web page"


def test_a_scripts_output_is_outside_text_over_mcp_as_in_the_portal(lab, folder, tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent import server
    from labharness.web.agent import OUTSIDE_TOOLS
    assert set(server.STEERING) <= set(OUTSIDE_TOOLS) and "run_script" in server.STEERING
    assert "check_script" not in OUTSIDE_TOOLS and "check_script" not in server.STEERING
    rt = kelvin(lab, folder, tmp_path, answers=[False])
    rt.gate.auto = True
    rt.save_script("odmr_check", SWEEP)
    marked = server.outside(rt, "the lab's script output", "I 0.001\n", steers=True)
    assert marked.startswith('<outside_text from="the lab\'s script output">')
    assert rt.run_script("kelvin_scripts/odmr_check.py").startswith("declined")
    assert cards(rt)[0].items[-1] == (
        "Auto mode does not cover this run: Kelvin read text from outside the lab (the lab's script output) in the last "
        "30 minutes, which could try to steer a model, so a person approves")


def test_check_script_says_what_the_next_run_gets_in_a_chat_that_read_outside_text(lab, folder, tmp_path):
    rt = kelvin(lab, folder, tmp_path)
    rt.gate.auto = True
    rt.save_script("odmr_check", SWEEP)
    assert rt.check_script("kelvin_scripts/odmr_check.py").splitlines()[-1].startswith(
        "Auto mode is on: run_script runs it without the card, fenced")
    rt.gate.in_chat, rt.gate.outside_chat = True, "a web page"
    try:
        assert rt.check_script("kelvin_scripts/odmr_check.py").splitlines()[-1] == (
            "Auto mode is on, but doesn't cover it: the card asks a person (this chat has read text from outside the "
            "lab (a web page) since the person last wrote, which could try to steer a model, so a person approves)")
    finally:
        rt.gate.in_chat, rt.gate.outside_chat = False, None
