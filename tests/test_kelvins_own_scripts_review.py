"""The ways past the fence and the guard a review of 0153d38 (2026-11-15) found, each now refused, asked or harmless.

The fence of a run auto mode approved, and the guard of every run, are audit hooks in the script's own process: not a
wall (PEP 578), but closed as tightly as is practical, with run_python's own refusals (its sandbox's lists: _winapi,
_overlapped, O_TEMPORARY, gc...), state of their own that no module name switches off, and on Windows the operating
system's own protection besides: a run auto mode approved runs at low integrity, so Windows itself refuses its writes
outside its two folders, C code's too. P-numbers are the review's probes (test_zz_rv_*)."""
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from labharness import scriptrun
from labharness.clock import SimClock
from labharness.homelab import builtin_registry
from labharness.lab import Lab
from labharness.labconfig import load_lab_config
from labharness.sim.twin import World, sim_backend_factory

from test_kelvins_own_scripts import SWEEP, auto_approved, cards, commands, kelvin, last_run, own
from test_kelvins_own_scripts_fence import FENCED, HEAD, own_packages, ran, tcp_lab  # noqa: F401 (fixtures)
from test_script_runs import LAB, folder, lab, output, run, write  # noqa: F401 (fixtures)

WINDOWS = sys.platform == "win32"
SHIM = Path(__file__).resolve().parents[1] / "src" / "labharness" / "data" / "scriptshim"


def auto_run(rt, name, code):
    rt.gate.auto = True
    said = rt.save_script(name, code)
    assert said.startswith("saved"), said
    said = rt.run_script("kelvin_scripts/%s.py" % name)
    time.sleep(0.3)
    return said


def ended(said, name):
    """The run ended there: the fence's refusal, or the guard's (a lab instrument's own address), or its own error."""
    return ran(said).startswith("script %s stopped" % name), said


# ------------------------------------------------------------------ the fence of a run auto mode approved

@pytest.mark.skipif(not WINDOWS, reason="_winapi is Windows'")
def test_p1_winapi_files_pipes_and_processes_are_refused(lab, folder, tmp_path):
    from multiprocessing.connection import Listener
    rt = kelvin(lab, folder, tmp_path, answers=[])
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "written.txt"
    pipe = r"\\.\pipe\kelvin-review-%d" % os.getpid()
    listener = Listener(pipe, family="AF_PIPE")
    got = []

    def serve():
        try:
            got.append(listener.accept().recv_bytes())
        except Exception:
            pass
    threading.Thread(target=serve, daemon=True).start()
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        for step in ("w.CreateFile(%r, 0x40000000, 0, 0, 2, 0, 0)\n" % str(marker),
                     "w.CreateFile(%r + %r, 0xC0000000, 0, 0, 3, 0, 0)\n" % (pipe[:3], pipe[3:]),
                     "w.TerminateProcess(w.OpenProcess(1, False, %d), 9)\n" % other.pid):
            said = auto_run(rt, "winapi_probe", "import sys\nw = sys.modules['_winapi']\nh = " + step + SWEEP)
            assert FENCED + "_winapi." in ran(said), said
        assert not marker.exists() and got == [] and other.poll() is None
    finally:
        other.kill()
        listener.close()


@pytest.mark.skipif(not WINDOWS, reason="_overlapped is Windows'")
@pytest.mark.parametrize("how", ["import _overlapped", "asyncio's own"])
def test_p2_overlapped_never_connects(tcp_lab, folder, tmp_path, how):
    lab, port, got = tcp_lab
    rt = kelvin(lab, folder, tmp_path, answers=[])
    get = "import _overlapped\n" if how.startswith("import") else \
        "import asyncio.windows_events as we\n_overlapped = we._overlapped\n"
    code = (get + "import socket\ns = socket.socket()\n_overlapped.BindLocal(s.fileno(), socket.AF_INET)\n"
            "ov = _overlapped.Overlapped(0)\nov.ConnectEx(s.fileno(), ('.'.join(['127', '0', '0', '1']), %d))\n"
            "ov.getresult(True)\ns.send(b'SLVL 4.5\\n')\n" % port) + SWEEP
    said = auto_run(rt, "overlapped_probe", code)
    assert ended(said, "overlapped_probe") and got == [], said


def test_p3_the_hooks_are_not_found_nor_rewritten(lab, folder, tmp_path):
    rt = kelvin(lab, folder, tmp_path, answers=[])
    marker = tmp_path / "program-ran.txt"
    code = HEAD % str(marker) + (
        "import gc, os, types\n"
        "for f in gc.get_objects():\n"
        "    if isinstance(f, types.FunctionType) and f.__name__ == 'hook' and f.__module__ == 'kelvin_shim':\n"
        "        f.__code__ = (lambda event, args: None).__code__\n"
        "getattr(os, 'sys' + 'tem')(CMD)\n") + SWEEP
    said = auto_run(rt, "hook_probe", code)
    assert FENCED + "gc.get_objects" in ran(said) and not marker.exists(), said
    code = ("import sys\nks = sys.modules['kelvin_shim']\nks._guard.__code__ = (lambda *a, **k: None).__code__\n"
            + SWEEP)   # a function of Kelvin's own reached by its module's name: its code stays
    said = auto_run(rt, "code_probe", code)
    assert FENCED + "replacing the __code__ of Kelvin's own _guard" in ran(said), said


@pytest.mark.skipif(not WINDOWS, reason="Windows' low integrity: elsewhere C code's own writes aren't stopped")
def test_p4_c_code_writes_nowhere_but_its_folders(lab, folder, tmp_path):
    """h5py's and SQLite's own C code write past any audit hook: Windows itself refuses them, as the run is at low
    integrity and only its two folders are labelled for it."""
    pytest.importorskip("h5py")
    rt = kelvin(lab, folder, tmp_path, answers=[])
    outside = tmp_path / "outside"
    outside.mkdir()
    code = ("import os, sqlite3, h5py\n"
            "try:\n    h5py.File(%r, 'w').close()\nexcept OSError as e:\n    print('H5 REFUSED', type(e).__name__)\n"
            "c = sqlite3.connect(os.path.join(os.environ['KELVIN_RUN_DIR'], 'x.db'))\n"
            "try:\n    c.execute('ATTACH DATABASE ? AS a', (%r,))\n    c.execute('CREATE TABLE a.t(x)')\n"
            "    c.commit()\nexcept sqlite3.Error as e:\n    print('SQLITE REFUSED', type(e).__name__)\n"
            % (str(outside / "data.h5"), str(outside / "attached.db"))) + SWEEP
    said = auto_run(rt, "cwrites_probe", code)
    assert sorted(p.name for p in outside.iterdir()) == [], said
    _, run_dir = last_run(rt, own(folder, "cwrites_probe.py"))
    out = (run_dir / "output.txt").read_text(encoding="utf-8", errors="replace")
    assert "H5 REFUSED" in out and "SQLITE REFUSED" in out, out[-1500:]


def test_p5_a_library_on_pythonpath_asks_and_never_loads_before_the_fence(tcp_lab, folder, tmp_path, monkeypatch):
    lab, port, got = tcp_lab
    marker = tmp_path / "preloaded-program-ran.txt"
    lib = tmp_path / "labtools"
    lib.mkdir()
    cmd = ('type nul > "%s"' % marker) if WINDOWS else ('touch "%s"' % marker)
    (lib / "labtools.py").write_text("import subprocess, _socket\nsubprocess.run(%r, shell=True)\n"
                                     "s = _socket.socket()\ns.connect(('127.0.0.1', %d))\ns.send(b'SLVL 4.5\\n')\n"
                                     "OFFSET = 0.0\n" % (cmd, port))
    monkeypatch.setenv("PYTHONPATH", os.environ["PYTHONPATH"] + os.pathsep + str(lib))   # the lab's library
    rt = kelvin(lab, folder, tmp_path, answers=[False])
    said = auto_run(rt, "uses_labtools", "import labtools\n" + SWEEP)
    assert said.startswith("declined") and not marker.exists() and got == [], said
    [card] = cards(rt)
    assert card.items[-1].startswith("Auto mode does not cover this run: it imports labtools from ") and \
        "which isn't one of its Python's own packages" in card.items[-1], card.items[-1]


def test_p6_asyncio_works_in_a_run_auto_mode_approved(lab, folder, tmp_path):
    """Its selector loop, whose self-pipe is Kelvin's own socketpair (Windows' is a loopback connect)."""
    rt = kelvin(lab, folder, tmp_path, answers=[])
    code = ("import asyncio\nasync def main():\n    await asyncio.sleep(0)\n    return 'ASYNCIO RAN'\n"
            "print(asyncio.run(main()))\n") + SWEEP
    said = auto_run(rt, "asyncio_probe", code)
    assert "script asyncio_probe.py done" in ran(said) and "ASYNCIO RAN" in said, said


@pytest.mark.skipif(WINDOWS, reason="dir_fd is POSIX's")
def test_p9_a_folder_outside_isnt_opened_so_no_dir_fd_reaches_past(lab, folder, tmp_path):
    rt = kelvin(lab, folder, tmp_path, answers=[])
    outside = tmp_path / "outside"
    outside.mkdir()
    for step in ("d = os.open(%r, os.O_RDONLY)\nfd = os.open('escaped.txt', os.O_WRONLY | os.O_CREAT, 0o644, "
                 "dir_fd=d)\nos.write(fd, b'past the fence')\n" % str(outside),
                 "d = os.open('/' + 'dev', os.O_RDONLY)\nz = os.open('zero', os.O_WRONLY, dir_fd=d)\n"
                 "print('DEVICE WRITTEN', os.write(z, b'OUTP ON'))\n"):
        said = auto_run(rt, "dirfd_probe", "import os\n" + step + SWEEP)
        assert FENCED in ran(said) and "DEVICE WRITTEN" not in said, said
    assert list(outside.iterdir()) == []


def test_p10_a_module_kelvin_didnt_write_is_never_imported(lab, folder, tmp_path):
    rt = kelvin(lab, folder, tmp_path, answers=[])
    rt.save_script("placeholder", "x = 1\n")   # makes kelvin_scripts
    (folder / "kelvin_scripts" / "helper.py").write_text("print('FOREIGN MODULE RAN')\n")   # a person's file
    code = ("import importlib.resources\ntry:\n    importlib.resources.files('helper')\n"
            "except ImportError as e:\n    print('REFUSED', e)\n") + SWEEP
    said = auto_run(rt, "resources_probe", code)
    assert "FOREIGN MODULE RAN" not in said and "its process refused: import helper" in said, said


@pytest.mark.skipif(not WINDOWS, reason="O_TEMPORARY and alternate data streams are Windows'")
@pytest.mark.parametrize("how", ["O_TEMPORARY", "x.py::$DATA", "__pycache__::$INDEX_ALLOCATION"])
def test_p18_and_streams_the_persons_files_and_its_code_stay(lab, folder, tmp_path, how):
    rt = kelvin(lab, folder, tmp_path, answers=[])
    theirs = write(folder, "their_script.py", "print('the person wrote this')\n")
    step = {"O_TEMPORARY": "fd = os.open(os.path.join('..', 'their_script.py'), os.O_RDONLY | os.O_TEMPORARY)\n"
                           "os.close(fd)\n",
            "x.py::$DATA": "open(os.path.join(here, 'odd.py::$DATA'), 'w').write('import ctypes')\n",
            "__pycache__::$INDEX_ALLOCATION": "os.mkdir(os.path.join(here, '__pycache__::$INDEX_ALLOCATION'))\n"}[how]
    said = auto_run(rt, "stream_probe", "import os\nhere = os.path.dirname(os.path.abspath(__file__))\n" + step + SWEEP)
    assert FENCED in ran(said), said
    assert theirs.exists() and not (folder / "kelvin_scripts" / "odd.py").exists()
    assert not (folder / "kelvin_scripts" / "__pycache__").exists()


def test_p15_a_stop_while_its_packages_load_ends_the_run(lab, folder, tmp_path, own_packages):
    """A person's Stop (one KeyboardInterrupt) as a package of its Python's loads before the fence: once swallowed
    there, then the script sent OUTP ON and SOUR:VOLT 0.5, 1 and 0.8, counted as clean-up."""
    rt = kelvin(lab, folder, tmp_path, answers=[])
    code = ("import %s, time\n" % own_packages["SLOWMOD"] + SWEEP + "print('SCRIPT REACHED ITS END')\n")
    rt.gate.auto = True
    assert rt.save_script("slow", code).startswith("saved")
    threading.Timer(2.0, rt.gate.request_abort).start()   # a person's Stop, while the package loads
    said = rt.run_script("kelvin_scripts/slow.py")
    _, run_dir = last_run(rt, own(folder, "slow.py"))
    sent = [c.get("command") for c in commands(run_dir) if c.get("command")]
    assert ran(said).startswith("script slow.py stopped") and sent == [] and "SCRIPT REACHED ITS END" not in said, \
        (said, sent)


# ------------------------------------------------------------------ the guard, in every run

def test_p7_an_ipv4_address_in_ipv6_form_is_the_lab_instrument(tcp_lab, folder, tmp_path):
    lab, port, got = tcp_lab
    rt = kelvin(lab, folder, tmp_path, answers=[True, False])
    write(folder, "v6.py", "import _socket, socket\ns = _socket.socket(socket.AF_INET6)\n"
          "s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)\n"
          "s.connect(('::ffff:127.0.0.1', %d))\ns.send(b'SLVL 4.5\\n')\n" % port)
    said = rt.run_script("v6.py")
    time.sleep(0.3)
    assert "which is the lab's lockin2" in ran(said) and got == [], said


def test_p8_another_way_into_a_lab_instrument_is_refused_without_asking(folder, tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    got = []

    def serve():
        listener.settimeout(20)
        try:
            got.append(listener.accept()[0].recv(100))
        except OSError:
            pass
    threading.Thread(target=serve, daemon=True).start()
    # the lab's lock-in reached by VXI-11 (TCPIP0::host::INSTR); the script reads its web page (another port)
    cfg = load_lab_config(LAB.replace("TCPIP0::127.0.0.1::5025::SOCKET", "TCPIP0::127.0.0.1::INSTR"),
                          builtin_registry())
    clock = SimClock()
    world = World(clock, 0)
    lab = Lab(cfg, builtin_registry(), sim_backend_factory(world), clock, world)
    script = write(folder, "web.py", "import socket\nh = '.'.join(['127', '0', '0', '1'])\n"
                   "s = socket.create_connection((h, %d))\ns.sendall(b'GET /screen.png HTTP/1.0\\r\\n\\r\\n')\n" % port)
    try:
        gate, request, report = run(lab, folder, tmp_path, script, answers=(True,))   # its card; nothing else asked
    finally:
        listener.close()
    assert len(gate.approver.requests) == 1 and report.status == "aborted", report.message
    assert "which reaches this lab's lockin2 another way" in report.message and got == [], report.message


def test_p12_no_module_name_switches_the_guard_off(tcp_lab, folder, tmp_path):
    lab, port, got = tcp_lab
    rt = kelvin(lab, folder, tmp_path, answers=[True])
    write(folder, "off.py", "import sys, _socket\nks = sys.modules['kelvin_shim']\n"
          "for name, value in (('_GUARD', None), ('_LINK', None), ('_device', None), ('_socket_address', None)):\n"
          "    setattr(ks, name, value)\n"
          "s = _socket.socket()\ns.connect(('.'.join(['127', '0', '0', '1']), %d))\ns.send(b'SLVL 4.5\\n')\n" % port)
    said = rt.run_script("off.py")
    time.sleep(0.3)
    assert "which is the lab's lockin2" in ran(said) and got == [], said


def test_p11_a_ports_other_spellings_are_the_port():
    sys.path.insert(0, str(SHIM))
    try:
        import kelvin_shim
    finally:
        sys.path.remove(str(SHIM))
    if WINDOWS:
        for spelling in (r"\\?\GLOBALROOT\GLOBAL??\COM7", "COM7", r"\\?\COM7", b"COM7"):
            assert kelvin_shim._device(spelling, True).upper() == r"\\.\COM7", spelling
        assert kelvin_shim._device(r"\\?\C:\data\x.csv", True) is None
    else:
        import tempfile
        here = tempfile.mkdtemp()
        os.symlink("/dev/ttyUSB7", os.path.join(here, "port"))
        for spelling in ("/dev/ttyUSB7", "/proc/self/root/dev/ttyUSB7", os.path.join(here, "port")):
            assert kelvin_shim._device(spelling, True) == "/dev/ttyUSB7", spelling
        assert kelvin_shim._device("/dev/null", True) is None


@pytest.mark.skipif(WINDOWS, reason="fork is POSIX's")
def test_p13_a_forked_worker_has_its_own_connection_and_never_hangs(lab, folder, tmp_path):
    script = write(folder, "forked.py", '''
        import multiprocessing as mp, socket
        def work(i):
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"metric:%d" % i, ("127.0.0.1", 8125))
            return i
        if __name__ == "__main__":
            with mp.get_context("fork").Pool(2) as pool:
                try:
                    print("RESULT", pool.map_async(work, range(4)).get(timeout=20))
                except mp.TimeoutError:
                    print("RESULT timed out: a forked worker never came back")
    ''')
    gate, request, report = run(lab, folder, tmp_path, script, answers=(True,))
    assert "RESULT [0, 1, 2, 3]" in output(report), output(report)[-1500:]


def test_p16_the_lab_instruments_other_names_are_refused(tcp_lab, folder, tmp_path):
    lab, port, got = tcp_lab
    rt = kelvin(lab, folder, tmp_path, answers=[True] * 3)
    for host in ("localhost", "127.1", "0x7f000001"):   # (Windows resolves neither of the last two: nothing connects)
        write(folder, "spell.py", "import _socket\ns = _socket.socket()\ns.connect((%r, %d))\ns.send(b'SLVL 4.5\\n')\n"
              % (host, port))
        said = rt.run_script("spell.py")
        assert "which is the lab's lockin2" in ran(said) or "getaddrinfo failed" in ran(said), (host, said)
    time.sleep(0.3)
    assert got == []


COMPAT = r'''
import asyncio, json, logging, logging.handlers, os, selectors, socket, sys, time, urllib.request
import multiprocessing
results = {}
def attempt(name, fn):
    try:
        r = fn()
        results[name] = "ok" if r is None else str(r)[:80]
    except BaseException as e:
        results[name] = "%s: %s" % (type(e).__name__, str(e)[:160])
PORT = int(sys.argv[1])
def loop_client():
    async def go():
        r, w = await asyncio.open_connection("127.0.0.1", PORT)
        w.write(b"GET / HTTP/1.0\r\n\r\n"); await w.drain()
        data = await r.read(20); w.close()
    asyncio.run(go())
attempt("asyncio loop + TCP client to a local server", loop_client)
attempt("urllib to a local web server", lambda: urllib.request.urlopen("http://localhost:%d/" % PORT, timeout=10).close())
attempt("DNS lookup", lambda: socket.getaddrinfo("localhost", 80) and None)
attempt("selectors", lambda: selectors.DefaultSelector().close())
def udp_log():
    h = logging.handlers.DatagramHandler("127.0.0.1", 9)
    logging.getLogger("x").addHandler(h)
    logging.getLogger("x").warning("hello")
attempt("logging DatagramHandler", udp_log)
attempt("nul and os.devnull", lambda: (open(os.devnull, "w").write("x"), open("nul" if os.name == "nt" else "/dev/null", "w").write("x")) and None)
os.makedirs("data", exist_ok=True)
attempt("data/CON_results.csv", lambda: open(os.path.join("data", "CON_results.csv"), "w").write("a,b\n") and None)
attempt("com1_scan.txt", lambda: open("com1_scan.txt", "w").write("x") and None)
def manager():
    with multiprocessing.Manager() as m:
        d = m.dict(); d["a"] = 1
if __name__ == "__main__":
    attempt("multiprocessing Manager", manager)
    print("RESULTS " + json.dumps(results))
'''


def test_p14_ordinary_traffic_in_a_persons_run_is_left_alone(lab, folder, tmp_path):
    import http.server
    import json
    import socketserver

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Quiet)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    script = write(folder, "compat.py", COMPAT.replace("int(sys.argv[1])", str(server.server_address[1])))
    try:
        gate, request, report = run(lab, folder, tmp_path, script, answers=(True,))
    finally:
        server.shutdown()
    out = output(report)
    line = next((ln for ln in out.splitlines() if ln.startswith("RESULTS ")), None)
    assert line, out[-1500:]
    results = json.loads(line[len("RESULTS "):])
    assert all(v == "ok" for v in results.values()), results
    assert len(gate.approver.requests) == 1
