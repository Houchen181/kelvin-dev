"""A field report of 2026-11-17, from a lab bringing up a Cobolt laser and an IDQ time controller next to its NV rig:
uv stopped at its own Python link with Windows' os error 448, its message cut at 400 characters; try_card refused a
serial port the scan hadn't found instead of asking; a laser's draft rested "safe" in a mode that emits; the draft's
notes ended mid-word; an install's approval said one thing and did another; and a lib:// address naming a Python
could be taken only in Lab setup. (Its QICK numpy item was fixed in 0.3.53, its fit and its requests R1 and R2 have
files of their own.)"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from labharness import carddraft, envs
from labharness.cards import parse_card
from labharness.gate import ScriptedApprover
from labharness.yamlio import load_yaml
from test_add_instrument import SMU_ONLY, WIDGET, bench, events, file_runtime  # noqa: F401 (bench: a fixture)
from test_envs import FakeUv, kelvin  # noqa: F401 (kelvin: a fixture)

# uv's messages, as the report quotes them (its first two lines; the second reached the chat cut at 400 characters)
REFUSED_LINK = ("Using CPython 3.11.14\nerror: Failed to create Python minor version link directory\n"
                "  cause: The path cannot be traversed because it contains an untrusted mount point. (os error 448)\n")
REFUSED_QUERY = ("error: Failed to inspect Python interpreter from managed installations at "
                 "`C:\\Users\\xu\\Kelvin\\tools\\python\\cpython-3.13-windows-x86_64-none\\python.exe`\n"
                 "  cause: Failed to query Python interpreter\n"
                 "  cause: failed to query metadata of file `C:\\Users\\xu\\Kelvin\\tools\\python\\"
                 "cpython-3.13-windows-x86_64-none\\python.exe`: The path cannot be traversed because it contains "
                 "an untrusted mount point. (os error 448)\n")


def link(at: Path, target: Path) -> None:
    """A link as uv makes a minor version's folder: a junction on Windows, a symbolic link elsewhere."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(at))
    else:
        os.symlink(str(target), str(at), target_is_directory=True)


def fetched(home: Path, patch: str = "3.13.9", linked: bool = True):
    """A Python uv fetched into Kelvin's tools folder (its own folder and its python), and the minor version's link
    to it as uv makes one: (the folder, the link, the python)."""
    system, abi = ("windows", "none") if envs.WIN else ("macos", "none") if sys.platform == "darwin" else \
        ("linux", "gnu")
    tail = "%s-%s-%s" % (system, envs._arches(64)[0], abi)
    pythons = home / "tools" / "python"
    real = pythons / ("cpython-%s-%s" % (patch, tail))
    exe = real / ("python.exe" if envs.WIN else "bin/python3")
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("")
    minor = pythons / ("cpython-%s-%s" % (patch.rsplit(".", 1)[0], tail))
    if linked and not os.path.lexists(str(minor)):
        link(minor, real)
    return real, minor, exe


def uv_here(home: Path) -> None:
    (home / "tools" / "uv").mkdir(parents=True, exist_ok=True)
    (home / "tools" / "uv" / ("uv.exe" if envs.WIN else "uv")).write_text("")


# ------------------------------------------------------------------ 1. uv and Windows' RedirectionGuard

def test_what_uv_said_comes_back_whole_and_says_what_windows_refused(tmp_path, monkeypatch):
    said = envs._said(REFUSED_QUERY)
    assert said.endswith("untrusted mount point. (os error 448)") and len(said) > 400   # cut at 400: "(os erro"
    assert said.count("cause:") == 2 and said.startswith("error: Failed to inspect Python interpreter")
    monkeypatch.setattr(envs, "redirection_guard", lambda: True)
    failed = envs._failed(tmp_path, "venv idq_tc", REFUSED_QUERY)
    assert failed.startswith(said) and "RedirectionGuard is on in Kelvin" in failed and "Start menu" in failed
    assert "all uv wrote" not in failed                                  # nothing of what uv said was left out
    monkeypatch.setattr(envs, "redirection_guard", lambda: False)
    assert "untrusted mount point" in envs._failed(tmp_path, "venv idq_tc", REFUSED_LINK)
    assert envs._refused_a_link("error: No interpreter found for Python 3.11") == ""


def test_a_long_builds_output_is_kept_whole_in_a_file_and_its_end_said(tmp_path):
    out = ("error: Failed to build `oddpkg==1.0`\n  cause: The build backend returned an error\n" +
           "".join("  line %d of the build's own output\n" % i for i in range(40)) +
           "hint: This usually indicates a problem with the package or the build environment.\n")
    failed = envs._failed(tmp_path, "pip drivers", out)
    assert "line 39 of" in failed and "line 5 of" not in failed and "hint: This usually" in failed
    kept = Path(failed.split("(all uv wrote: ")[1].rstrip(")"))
    assert kept.read_text(encoding="utf-8") == out and kept.parent == tmp_path / "tools" / "uv" / "failed"


def test_kelvin_names_a_python_by_its_own_folder_never_through_uvs_link(tmp_path):
    real, minor, exe = fetched(tmp_path, "3.13.9")
    fetched(tmp_path, "3.13.4")                                          # an older patch, kept by uv
    odd = tmp_path / "tools" / "python" / (real.name.replace("3.13.9", "3.13.12+freethreaded"))
    (odd / ("python.exe" if envs.WIN else "bin/python3")).parent.mkdir(parents=True)
    (odd / ("python.exe" if envs.WIN else "bin/python3")).write_text("")
    assert envs.managed_python("3.13", 64, tmp_path) == exe              # the newest patch, in its own folder
    assert envs.managed_python("3.11", 64, tmp_path) is None
    assert envs.managed_python("3.13", 32, tmp_path) is None             # no 32-bit one


def test_an_environment_is_made_with_the_pythons_own_folder(tmp_path):
    real, minor, exe = fetched(tmp_path)
    uv_here(tmp_path)
    uv = FakeUv()
    envs.create("idq_tc", "3.13", 64, tmp_path, run=uv, why="the ID1000")
    [venv] = [c for c in uv.calls if c[1] == "venv"]
    assert venv[-2:] == ["--python", str(exe)]                           # never "3.13", which uv finds through the link


def test_a_python_uv_fetched_then_stopped_at_its_link_is_used_from_its_own_folder(tmp_path):
    """The report's first failure: uv fetched Python 3.11, then Windows refused it the minor version's link."""
    uv_here(tmp_path)
    uv, calls = FakeUv(), []

    def run(cmd, env):
        calls.append(cmd)
        if cmd[1] == "venv" and cmd[-1] == "3.11":
            fetched(tmp_path, "3.11.14", linked=False)                   # fetched, then the link refused
            Path(cmd[2]).mkdir(parents=True)                             # what it began of the environment
            return 2, REFUSED_LINK
        return uv(cmd, env)
    rec = envs.create("idq_tc", "3.11", 64, tmp_path, run=run)
    assert [c[-1] for c in calls if c[1] == "venv"] == ["3.11", str(fetched(tmp_path, "3.11.14", linked=False)[2])]
    assert rec["python"] == "3.11" and envs.info("idq_tc", tmp_path)["python"] == "3.11"

    def refused(cmd, env):   # nothing fetched: no folder to name
        return 2, REFUSED_LINK
    with pytest.raises(RuntimeError) as e:
        envs.create("other", "3.12", 64, tmp_path, run=refused)
    assert "uv could not make the environment other: error: Failed to create Python minor version link" in str(e.value)
    assert "(os error 448)" in str(e.value) and "Start menu" in str(e.value)


def test_an_environment_made_through_uvs_link_names_its_pythons_own_folder(tmp_path):
    """The lab's drivers environment, made on 2026-11-13, named its Python through uv's link: its python.exe, uv's
    query of it and a library's helper in it all go through that link, which RedirectionGuard refuses."""
    real, minor, exe = fetched(tmp_path)
    d = tmp_path / "env" / "drivers"
    d.mkdir(parents=True)
    (d / "kelvin.json").write_text(json.dumps({"name": "drivers", "python": "3.13", "bits": 64}), encoding="utf-8")
    home = minor if envs.WIN else minor / "bin"
    real_home = real if envs.WIN else real / "bin"
    (d / "pyvenv.cfg").write_text("home = %s\nimplementation = CPython\nuv = 0.12.13\nversion_info = 3.13.9\n"
                                  "include-system-site-packages = false\nexecutable = %s\n"
                                  % (home, home / "python.exe"), encoding="utf-8")
    envs.python_of("drivers", tmp_path)                                   # any use of the environment
    cfg = (d / "pyvenv.cfg").read_text(encoding="utf-8")
    assert "home = %s\n" % real_home in cfg and "executable = %s\n" % (real_home / "python.exe") in cfg
    assert "uv = 0.12.13" in cfg and str(minor) not in cfg
    assert envs.pin_python("drivers", tmp_path) is None                   # once is enough
    (d / "pyvenv.cfg").write_text("home = %s\n" % Path(sys.base_prefix), encoding="utf-8")
    assert envs.pin_python("drivers", tmp_path) is None                   # a Python of the lab's own: as it is


@pytest.mark.skipif(os.name != "nt", reason="Windows' RedirectionGuard")
def test_under_redirection_guard_kelvin_knows_it_and_names_pythons_past_uvs_link(tmp_path):
    """As in a Kelvin its installer started: the process has RedirectionGuard, and every junction a non-administrator
    made is refused it (os error 448), uv's link among them; Kelvin says so, and finds the Python past the link."""
    real, minor, exe = fetched(tmp_path)
    script = textwrap.dedent("""
        import ctypes, os, sys
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.SetProcessMitigationPolicy.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
        if not k32.SetProcessMitigationPolicy(16, ctypes.byref(wintypes.DWORD(1)), 4):
            print("unsupported")
            sys.exit(0)
        from labharness import envs
        print(envs.redirection_guard())
        try:
            os.listdir(sys.argv[1])
            print("through")
        except OSError as e:
            print(e.winerror)
        print(envs.managed_python("3.13", 64, sys.argv[2]))
    """)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    out = subprocess.run([sys.executable, "-c", script, str(minor), str(tmp_path)], capture_output=True, text=True,
                         env=env, timeout=60)
    said = out.stdout.splitlines()
    if said[:1] == ["unsupported"]:
        pytest.skip("this Windows has no RedirectionGuard (before Windows 10 22H2)")
    assert said == ["True", "448", str(exe)], out.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows' RedirectionGuard")
def test_under_redirection_guard_an_environment_named_past_uvs_link_starts(tmp_path):
    """Real CPython: an environment whose pyvenv.cfg names its Python through a junction, as uv's link, can't start
    under RedirectionGuard (its python.exe stops at the junction), and starts once Kelvin names the Python's own
    folder."""
    minor = tmp_path / "tools" / "python" / ("cpython-%d.%d-windows-x86_64-none" % sys.version_info[:2])
    minor.parent.mkdir(parents=True)
    link(minor, Path(sys.base_prefix))
    env = tmp_path / "env" / "drivers"
    base = Path(sys.base_prefix) / "python.exe"
    made = subprocess.run([str(base), "-m", "venv", "--without-pip", str(env)], capture_output=True, text=True,
                          timeout=120)
    if made.returncode or not (env / "Scripts" / "python.exe").is_file():
        pytest.skip("this Python makes no venv here: %s" % made.stderr[-200:])
    cfg = env / "pyvenv.cfg"
    lines = cfg.read_text(encoding="utf-8").splitlines()
    cfg.write_text("".join("home = %s\n" % minor if ln.startswith("home") else ln + "\n" for ln in lines),
                   encoding="utf-8")                                      # as uv names it: through the link
    guarded = textwrap.dedent("""
        import ctypes, subprocess, sys
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.SetProcessMitigationPolicy.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
        if not k32.SetProcessMitigationPolicy(16, ctypes.byref(wintypes.DWORD(1)), 4):
            print("unsupported")
            sys.exit(0)
        p = subprocess.run([sys.argv[1], "-c", "print('started')"], capture_output=True, text=True)
        print(p.returncode, (p.stdout + p.stderr).strip()[-200:])
    """)

    def under_guard():
        out = subprocess.run([sys.executable, "-c", guarded, str(env / "Scripts" / "python.exe")], capture_output=True,
                             text=True, timeout=60)
        if out.stdout.strip() == "unsupported":
            pytest.skip("this Windows has no RedirectionGuard (before Windows 10 22H2)")
        return out.stdout.strip()
    refused = under_guard()   # Python 3.13's launcher says why; 3.9's only "No Python at ..."
    assert not refused.startswith("0 ") and ("untrusted mount point" in refused or "No Python at" in refused), refused
    assert envs.pin_python("drivers", tmp_path) == str(Path(sys.base_prefix))
    assert under_guard() == "0 started"


def test_the_environments_listing_says_when_kelvin_has_redirection_guard(kelvin, monkeypatch):
    kelvin.install_for_instrument("drivers", ["qick"])
    assert "RedirectionGuard" not in kelvin.environments()
    monkeypatch.setattr(envs, "redirection_guard", lambda: True)
    assert "Windows' RedirectionGuard is on in this Kelvin" in kelvin.environments()


def test_a_failed_install_says_all_uv_said(kelvin, tmp_path, monkeypatch):
    def refused(cmd, env):
        return (2, REFUSED_QUERY) if cmd[1] == "venv" else (0, "")
    monkeypatch.setattr(envs, "_run", refused)
    said = kelvin.install_for_instrument("idq_tc", ["Pyro4"], python="3.13")
    assert said.startswith("could not install: uv could not make the environment idq_tc: error: Failed to inspect")
    assert "untrusted mount point. (os error 448)" in said                # the end the chat never saw
    logged = [json.loads(x) for x in (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e.get("type") == "install_failed" and "(os error 448)" in e["error"] for e in logged)


def test_the_installer_starts_kelvin_through_the_desktop_without_redirection_guard():
    """Inno Setup 6.7 turns RedirectionGuard on for Setup, and a per-user Setup passes it on: the Kelvin it started
    (after an update too) had it, and so did uv. Tried with Inno Setup 6.7.4 on 2026-11-17: RedirectionGuard=no alone
    leaves it on when Setup itself has it from the Kelvin whose updater started it; explorer.exe never does."""
    iss = (Path(__file__).resolve().parents[1] / "packaging" / "kelvin.iss").read_text(encoding="utf-8")
    assert "#if VER >= EncodeVer(6,7,0)\nRedirectionGuard=no\n#endif" in iss.replace("\r\n", "\n")
    runs = [ln for ln in iss.splitlines() if ln.startswith("Filename:") and "Kelvin.exe" in ln and "{app}" in ln]
    starts = [ln for ln in runs if "--remove-data" not in ln]
    shell = 'Filename: "{win}\\explorer.exe"; Parameters: """{app}\\Kelvin.exe"""'
    assert len(starts) == 2 and all(ln.startswith(shell) for ln in starts)


# ------------------------------------------------------------------ 3. try_card on an address the scan missed

def test_an_address_is_told_from_a_name():
    for address in ("COM11", "com3", "\\\\.\\COM7", "COM7:", "ASRL11::INSTR", "GPIB0::12::INSTR", "/dev/ttyUSB0",
                    "/dev/cu.usbserial-1410", "serial://COM3", "zmq://10.0.0.5:5555", "lib://COM4?env=attodry"):
        assert __import__("labharness.agent.runtime", fromlist=["x"])._an_address(address), address
    for name in ("laser", "lockin2", "", "COM", "my COM11 laser", "/home/lab/data"):
        assert not __import__("labharness.agent.runtime", fromlist=["x"])._an_address(name), name


def test_try_card_takes_a_serial_port_the_scan_missed_and_asks_as_it_says(tmp_path, bench):
    rt = file_runtime(tmp_path, SMU_ONLY)
    bench.devices["COM11"] = bench.devices["GPIB0::5::INSTR"]           # the laser on its USB serial port
    said = rt.try_card(WIDGET, "COM11")
    assert "matches the card" in said, said                             # tried, not refused as no address at all
    [own] = events(tmp_path, "ask")
    assert own["answer"] == "auto-approved"
    assert "Kelvin's last scan did not find an instrument at COM11" in own["request"]
    rt.gate.note_outside("a web page")                                   # then a person answers, and says why asked
    rt.gate.approver = ScriptedApprover([False])
    said = rt.try_card(WIDGET, "COM11")
    assert said.startswith("declined: the person did not want Kelvin to try COM11")
    assert "a person was asked, not Kelvin itself, because Kelvin read a web page" in said
    assert "COM11" in rt.gate.approver.requests[0].title


def test_try_card_on_a_lab_instrument_by_another_spelling_of_its_address_asks_nobody(tmp_path, bench):
    rt = file_runtime(tmp_path, SMU_ONLY)                                # the smu at GPIB0::24::INSTR
    assert "matches the card" in rt.try_card("keithley/2400", "GPIB::24")
    assert events(tmp_path, "ask") == []


# ------------------------------------------------------------------ 4, 5. a laser's draft: its safe mode, its notes

LASER = """card: 1
id: cobolt/06-mld-test
model: Cobolt 06-MLD 520 nm
vendor: Cobolt
kind: laser
status: draft
manual: Cobolt 06-MLD manual D0951-C
identify: { idn: "^Cobolt" }
error_query: "ERR?"
parameters:
  run_mode:
    label: Run mode
    kind: drive
    type: enum
    values: { constant_current: "0", constant_power: "1", current_modulation: "2" }
    set: "scm {value}"
    get: "gcm?"
    safe: %s
  P:
    label: Output power
    kind: measure
    unit: W
    get: "pa?"
"""


def test_a_lasers_safe_mode_that_emits_by_itself_is_warned_of(tmp_path):
    card, problems, warnings = carddraft.check(LASER % "constant_power")
    assert problems == [] and any(w.startswith("run_mode's safe value is constant_power, a mode that may emit")
                                  for w in warnings)
    assert carddraft.safe_modes(card) == ["run_mode → constant_power"]
    card, _, warnings = carddraft.check(LASER % "current_modulation")   # the lab's TTL-gated mode: nothing to say
    assert not any("may emit" in w for w in warnings)
    assert carddraft.safe_modes(card) == ["run_mode → current_modulation"]
    source = (LASER % "constant_power").replace("kind: laser", "kind: source-meter").replace("Cobolt 06-MLD 520 nm",
                                                                                              "ACME source")
    card, _, warnings = carddraft.check(source)                          # not a light source: its modes are its own
    assert not any("may emit" in w for w in warnings) and carddraft.safe_modes(card) == []
    assert "never one that emits by itself (constant power" in carddraft.GUIDE
    assert "which value you made safe and why" in carddraft.DRAFT_SYSTEM


def test_a_drafted_lasers_card_says_its_safe_modes_and_keeps_its_notes_whole(tmp_path):
    pytest.importorskip("pypdf")
    from labharness import autocard, builtin_registry
    from test_autocard import IDN, MANUAL
    from test_autosetup import tiny_pdf
    long_note = ("Setpoints are read only: I made P_set, I_set, PM_P_set and CM_I_high readings, because the manual "
                 "gives their set commands without the ranges the laser accepts in each run mode, and a wrong value "
                 "could change the emitted power. Their set commands are kept in the descriptions so you can add "
                 "them back once you know the ranges.")
    assert len(long_note) > 300
    answer = ("```yaml\n%s```\n- %s\n- run_mode: safe is constant_power, its CW mode." % (
        (LASER % "constant_power").replace("id: cobolt/06-mld-test", "id: acme/widget-9000"), long_note))

    def ask(system, prompt):
        if system == autocard.SUGGEST_SYSTEM:
            return "https://www.acme.example/products/widget-9000", "test:model"
        return answer, "test:model"
    pdf = tiny_pdf(MANUAL)
    pages = {"https://www.acme.example/products/widget-9000":
             b'<html><body><a href="/docs/W9000-prog.pdf">Programming manual</a></body></html>',
             "https://www.acme.example/docs/W9000-prog.pdf": pdf}

    def fetch(url):
        if url not in pages:
            raise OSError("HTTP Error 404: Not Found")
        return pages[url]
    r = autocard.make_card({"address": "COM11", "idn": IDN}, tmp_path / "cards", ask, fetch,
                           builtin_ids=[c.id for c in builtin_registry()])
    assert r.get("card_id"), r
    assert r["notes"][0] == long_note                                    # whole: it ended "...add them ba"
    assert r["safe_modes"] == ["run_mode → constant_power"]
    assert r["checks"] and r["checks"][0].startswith("run_mode's safe value is constant_power")
    saved = Path(r["path"]).read_text(encoding="utf-8")
    assert "#   %s\n" % long_note in saved and "# Check: run_mode's safe value is constant_power" in saved
    assert parse_card(load_yaml(saved), "draft").params["run_mode"].safe == "constant_power"   # a person's to change


def test_notes_are_kept_whole_and_only_a_very_long_one_is_cut_at_a_word():
    words = " ".join("word%d" % i for i in range(600))                   # some 5,000 characters
    notes = carddraft.notes_in("```yaml\ncard: 1\n```\n- short note\n- %s\n" % words)
    assert notes[0] == "short note" and notes[1].endswith(" …") and len(notes[1]) <= carddraft.NOTE_MOST + 2
    assert notes[1][:-2].split(" ")[-1].startswith("word")                # cut between words, never in one
    many = "```yaml\ncard: 1\n```\n" + "".join("- note %d\n" % i for i in range(8))
    assert len(carddraft.notes_in(many)) == 8                            # more than five kept


def test_draft_card_tells_the_person_the_safe_modes_to_check(tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    from simlab import build_virtual_lab
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    rt.card_writer = lambda found: {"card_id": "cobolt/06-mld", "model": "Cobolt 06-MLD", "kind": "laser",
                                    "manual": {"url": "https://example.com/D0951-C.pdf"}, "changed": [],
                                    "notes": ["run_mode: safe is constant_power"],
                                    "safe_modes": ["run_mode → constant_power"],
                                    "checks": ["run_mode's safe value is constant_power, a mode that may emit "
                                               "by itself"]}
    said = rt.draft_card("COM11", "Cobolt", "06-MLD")
    assert "The safe value it chose for each mode" in said and "run_mode → constant_power" in said
    assert "Check before keeping it: run_mode's safe value is constant_power" in said


# ------------------------------------------------------------------ 7. who approves an install, said as it is

def test_a_declined_install_says_why_a_person_was_asked(kelvin):
    kelvin.gate.note_outside("a web page")
    kelvin.approver.answers = [False]
    said = kelvin.install_for_instrument("idq_tc", ["Pyro4"], python="3.13")
    assert said.startswith("declined: nothing installed (")
    assert "a person was asked, not Kelvin itself, because Kelvin read a web page" in said


def test_kelvins_instructions_and_the_install_tool_say_the_same_about_installs():
    pytest.importorskip("mcp")
    from labharness.agent import server
    import inspect
    text = server.INSTRUCTIONS
    assert "installing software on this computer, wait for a person" not in text
    assert "installing a maker's package into Kelvin's" in text and "auto mode on or off" in text
    source = inspect.getsource(server)
    tool = source[source.index("def install_for_instrument("):]
    assert "unless the chat has read text that could" in tool[:900]


# ------------------------------------------------------------------ R3. a program a lib:// address names, from the chat

LIB = """card: 1
id: test/cryo-r3
model: Test cryostat (library)
vendor: Test
kind: cryostat
status: draft
library:
  module: kelvin_no_such_module_r3
  open: ["Connect {address}"]
  close: ["Disconnect"]
parameters:
  T: { label: Temperature, kind: measure, unit: K, get: "GET getSampleTemperature" }
"""


def test_a_program_a_lib_address_names_is_put_to_a_person_never_approved_by_kelvin(tmp_path, bench):
    rt = file_runtime(tmp_path, SMU_ONLY)
    rt.gate.auto = True                                                  # auto mode on: still the person's
    folder = tmp_path / "maker"
    folder.mkdir()
    address = "lib://COM4?python=%s&path=%s" % (sys.executable, folder)
    rt.gate.approver = ScriptedApprover([False])
    said = rt.try_card(LIB, address)
    assert said.startswith("declined: the person did not want Kelvin to start %s" % address)
    [request] = rt.gate.approver.requests
    shown = "\n".join(request.items)
    assert "Program: %s" % sys.executable in shown and "Folder: %s" % folder in shown
    assert 'module "kelvin_no_such_module_r3"' in shown and "getSampleTemperature" in shown
    assert "Only a person approves this" in shown
    assert not any(e.get("answer") == "auto-approved" for e in events(tmp_path, "ask"))
    rt.gate.approver = ScriptedApprover([True])                          # their yes: Kelvin starts it and tries
    said = rt.try_card(LIB, address)
    assert said.startswith("could not try it") and "kelvin_no_such_module_r3" in said, said


def test_what_a_program_address_names_is_checked_before_anyone_is_asked(tmp_path, bench):
    rt = file_runtime(tmp_path, SMU_ONLY)
    assert rt.try_card(LIB, "lib://COM1?python=C:/evil.exe&path=%s" % tmp_path).startswith(
        "not tried: python= names evil.exe, which is not a Python interpreter")
    assert rt.try_card(LIB, "lib://COM1?python=%s&path=%s" % (sys.executable, tmp_path / "gone")).startswith(
        "not tried: there is no folder")
    assert rt.gate.approver.requests == []


def test_adding_an_instrument_at_a_program_address_asks_the_person(tmp_path, bench):
    rt = file_runtime(tmp_path, SMU_ONLY)
    assert "saved" in rt.save_card(LIB)
    rt.gate.auto = True
    rt.gate.approver = ScriptedApprover([False])
    address = "lib://COM4?python=%s" % sys.executable
    said = rt.add_instrument(address, card="test/cryo-r3", name="cryo")
    assert said.startswith("declined: the person did not want cryo added"), said
    shown = "\n".join(rt.gate.approver.requests[0].items)
    assert "Program: %s, which Kelvin starts with its helper whenever the lab connects the instrument" % (
        sys.executable) in shown and "cryo" not in rt.lab.instruments
