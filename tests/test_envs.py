"""What an instrument needs on this computer, Kelvin installs, never the app: the maker's Python package (at the version
the instrument needs) or the maker's files, in an environment of Kelvin's own (envs.py), once a person approves."""
import hashlib
import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from labharness import build_virtual_lab, builtin_registry, envs
from labharness.adapters import AdapterError
from labharness.cards import CardError, parse_card
from labharness.gate import ScriptedApprover

PYPI = {"qick": {"info": {"name": "qick", "version": "0.2.436", "summary": "Quantum Instrumentation Control Kit",
                          "home_page": "https://github.com/openquantumhardware/qick"}},
        "pyro4": {"info": {"name": "Pyro4", "version": "4.82", "summary": "distributed object middleware",
                           "project_urls": {"Homepage": "https://github.com/irmen/Pyro4"}}}}


class FakeUv:
    """uv as Kelvin runs it: makes an environment's folders, installs, lists what is installed."""

    def __init__(self):
        self.calls, self.installed, self.envs = [], {}, []

    def __call__(self, cmd, env):
        self.calls.append(cmd)
        self.envs.append(env)
        assert Path(cmd[0]).name.startswith("uv") and env["UV_MANAGED_PYTHON"] == "1"
        if cmd[1] == "venv":
            folder = Path(cmd[2])
            python = folder / ("Scripts/python.exe" if envs.WIN else "bin/python")
            python.parent.mkdir(parents=True)
            python.write_text("")
            site = folder / ("Lib/site-packages" if envs.WIN else "lib/python3.13/site-packages")
            site.mkdir(parents=True)
            return 0, "Creating virtual environment at: %s" % folder
        if cmd[1:3] == ["pip", "install"]:
            for spec in cmd[cmd.index("--python") + 2:]:
                if spec == "--constraint" or spec.endswith("constraints.txt"):
                    continue
                name = envs.base_name(Path(spec).name if ("/" in spec or "\\" in spec) else spec)
                self.installed[name.lower()] = spec.split("==")[1] if "==" in spec else "1.0"
            return 0, "Installed %d packages" % len(self.installed)
        if cmd[1:3] == ["pip", "list"]:
            return 0, json.dumps([{"name": k, "version": v} for k, v in self.installed.items()])
        return 1, "unexpected command"


@pytest.fixture
def kelvin(tmp_path, monkeypatch):
    """Kelvin on the virtual lab, with uv already fetched and PyPI answering from a table."""
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    uv = FakeUv()
    monkeypatch.setattr(envs, "_run", uv)
    monkeypatch.setattr(sys, "path", list(sys.path))   # the drivers environment is attached to it
    (tmp_path / "tools" / "uv").mkdir(parents=True)
    (tmp_path / "tools" / "uv" / ("uv.exe" if envs.WIN else "uv")).write_text("")

    def get(url):
        name = url.rstrip("/").split("/")[-2].lower() if "pypi.org" in url else ""
        if name in PYPI:
            return json.dumps(PYPI[name]).encode()
        raise OSError("HTTP Error 404: Not Found")
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([]), tmp_path / "data")
    rt.home, rt.fetch_url = tmp_path, get
    rt.uv = uv
    return rt


def test_kelvin_installs_a_drivers_package_once_a_person_approves(kelvin, tmp_path):
    kelvin.approver.answers = [True]
    said = kelvin.install_for_instrument("drivers", ["qick==0.2.436", "Pyro4"], why="the RFSoC 4x2")
    request = kelvin.approver.requests[0]
    shown = "\n".join(request.items)
    assert "Install on this computer for the RFSoC 4x2?" in request.title
    assert "a new environment drivers" in shown and "Python %s" % envs.app_python()[0] in shown
    assert "qick==0.2.436 from PyPI (qick; latest 0.2.436): Quantum Instrumentation Control Kit" in shown
    assert "github.com/irmen/Pyro4" in shown                          # where it comes from
    assert "installed in environment drivers" in said and "qick 0.2.436" in said
    rec = envs.info("drivers", tmp_path)
    assert rec["packages"] == {"qick": "0.2.436", "Pyro4": "1.0"} and rec["for"] == ["the RFSoC 4x2"]
    install = next(c for c in kelvin.uv.calls if c[1:3] == ["pip", "install"])
    assert "qick==0.2.436" in install and str(envs.python_of("drivers", tmp_path)) in install
    if envs.app_pins():                                               # held to the app's own numpy
        assert "--constraint" in install
        assert (tmp_path / "env" / "drivers" / "constraints.txt").read_text().startswith("numpy==")
    assert str(envs.site_packages("drivers", tmp_path)) in sys.path   # the app imports from it now
    assert "drivers: Python %s" % envs.app_python()[0] in kelvin.environments()
    logged = [json.loads(x) for x in (tmp_path / "data" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e.get("type") == "installed" and e.get("env") == "drivers" for e in logged)


def test_nothing_is_installed_without_a_yes(kelvin, tmp_path):
    kelvin.approver.answers = [False]
    assert kelvin.install_for_instrument("drivers", ["qick"]).startswith("declined: nothing installed")
    assert not (tmp_path / "env").exists() and kelvin.uv.calls == []


def test_what_kelvin_will_not_install(kelvin):
    assert "not package names" in kelvin.install_for_instrument("drivers", ["qick; rm -rf /"])
    assert "not an environment name" in kelvin.install_for_instrument("C:/Windows", ["qick"])
    assert "PyPI has no package nosuchpackage" in kelvin.install_for_instrument("drivers", ["nosuchpackage"])
    assert "Kelvin's own Python" in kelvin.install_for_instrument("drivers", ["qick"], python="3.8", bits=32)
    assert "no file or folder" in kelvin.install_for_instrument("attodry", files="C:/nowhere/attoDRYLib")
    kelvin.literature.enabled = False
    assert "switched off in Settings" in kelvin.install_for_instrument("drivers", ["qick"])
    assert kelvin.approver.requests == [] and kelvin.uv.calls == []


def test_a_library_gets_an_environment_of_its_own(kelvin, tmp_path):
    """A 32-bit DLL needs a 32-bit Python: the environment is made with one, and its files are kept in it."""
    sdk = tmp_path / "attocube" / "attoDRYLib"
    sdk.mkdir(parents=True)
    (sdk / "attoDRYLib.dll").write_bytes(b"MZ")
    kelvin.approver.answers = [True]
    said = kelvin.install_for_instrument("attodry", python="3.8", bits=32, files=str(sdk), why="the attoDRY2100")
    assert "a new environment attodry" in "\n".join(kelvin.approver.requests[0].items)
    made = next(i for i, c in enumerate(kelvin.uv.calls) if c[1] == "venv")
    assert kelvin.uv.calls[made][-2:] == ["--python", "3.8"] and kelvin.uv.envs[made]["UV_PYTHON_ARCH"] == "x86"
    rec = envs.info("attodry", tmp_path)
    assert (rec["python"], rec["bits"], rec["files"]) == ("3.8", 32, ["attoDRYLib"])
    assert (tmp_path / "env" / "attodry" / "files" / "attoDRYLib" / "attoDRYLib.dll").is_file()
    assert "lib://<address>?env=attodry" in said
    assert "Python 3.8 (32-bit)" in kelvin.install_for_instrument("attodry", ["atto_device"], python="3.11")


def test_an_sdk_python_package_is_installed_not_copied(kelvin, tmp_path):
    sdk = tmp_path / "Andor SDK3" / "Python" / "pyAndorSDK3"
    sdk.mkdir(parents=True)
    (sdk / "setup.py").write_text("from setuptools import setup; setup(name='pyAndorSDK3')")
    kelvin.approver.answers = [True]
    said = kelvin.install_for_instrument("drivers", files=str(sdk), why="the Zyla")
    assert "installed as a Python package" in said
    install = next(c for c in kelvin.uv.calls if c[1:3] == ["pip", "install"])
    assert str(sdk) in install and not (tmp_path / "env" / "drivers" / "files").exists()
    assert envs.info("drivers", tmp_path)["files"] == ["pyAndorSDK3"]


def test_uv_is_fetched_once_and_checked(tmp_path):
    exe = "uv.exe" if envs.WIN else "uv"
    buf = io.BytesIO()
    if envs.uv_asset().endswith(".zip"):
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("uv-x86_64-pc-windows-msvc/" + exe, b"the uv program")
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            info = tarfile.TarInfo("uv-x86_64/" + exe)
            info.size = len(b"the uv program")
            t.addfile(info, io.BytesIO(b"the uv program"))
    archive = buf.getvalue()
    good = {envs.UV_RELEASE + envs.uv_asset(): archive,
            envs.UV_RELEASE + envs.uv_asset() + ".sha256": (hashlib.sha256(archive).hexdigest() + "  x").encode()}
    bad = dict(good)
    bad[envs.UV_RELEASE + envs.uv_asset() + ".sha256"] = b"0" * 64
    with pytest.raises(RuntimeError, match="does not match its published SHA-256"):
        envs.fetch_uv(tmp_path, bad.__getitem__)
    assert envs.fetch_uv(tmp_path, good.__getitem__).read_bytes() == b"the uv program"
    assert envs.uv_path(tmp_path) == tmp_path / "tools" / "uv" / exe


def test_removing_an_environment_asks_first(kelvin, tmp_path):
    kelvin.approver.answers = [True, False, True]
    kelvin.install_for_instrument("drivers", ["qick"])
    assert kelvin.remove_environment("drivers").startswith("declined")
    assert kelvin.remove_environment("drivers") == "removed environment drivers"
    assert envs.listing(tmp_path) == [] and not (tmp_path / "env" / "drivers").exists()
    assert kelvin.remove_environment("drivers") == "no environment drivers"


def test_a_library_address_runs_its_environments_python(tmp_path, monkeypatch):
    from labharness.library import LibraryInstrument
    monkeypatch.setitem(envs.HOME, "path", tmp_path)
    card = parse_card({"card": 1, "id": "acme/lib", "model": "ACME", "vendor": "ACME", "kind": "cryostat",
                       "requires": ["acme-cryo"], "library": {"module": "acmecryo"},
                       "parameters": {"T": {"kind": "measure", "unit": "K", "get": "GET getT"}}})
    with pytest.raises(AdapterError, match="no instrument environment 'cryo' on this computer yet"):
        LibraryInstrument("cryo", card, "lib://COM9?env=cryo", module=lambda *a: None)
    python = envs.python_of("cryo", tmp_path)
    python.parent.mkdir(parents=True)
    python.write_text("")
    (tmp_path / "env" / "cryo" / "files").mkdir()
    started = []

    class Helper:
        def __init__(self, name, command):
            started.append(command)

        def request(self, op, timeout, **fields):
            if op == "load":
                started.append(fields["path"])
                raise AdapterError("cryo: No module named 'acmecryo'")
            return {"bits": 64}

        def stop(self):
            pass
    with pytest.raises(AdapterError, match=r'install_for_instrument with env cryo and packages \["acme-cryo"\]'):
        LibraryInstrument("cryo", card, "lib://COM9?env=cryo", module=Helper)
    assert started == [[str(python), "-u", started[0][2]], str(tmp_path / "env" / "cryo" / "files")]


def test_kelvin_may_add_a_library_instrument_only_through_its_own_environment():
    pytest.importorskip("mcp")
    from labharness.agent.runtime import _runs_a_program
    assert not _runs_a_program("lib://192.168.1.20?env=attodry")
    assert _runs_a_program("lib://COM4?python=C:/Python38-32/python.exe&path=C:/attocube")
    assert _runs_a_program("lib://COM4?env=attodry&path=C:/elsewhere")
    assert _runs_a_program("lib://COM4")
    assert not _runs_a_program("TCPIP0::192.168.1.20::5025::SOCKET")


def test_cards_say_which_makers_packages_they_need():
    reg = builtin_registry()
    assert reg.get("qick/rfsoc").requires == ["qick", "Pyro4"]
    assert reg.get("zhinst/mfli").requires == ["zhinst-core"]
    assert reg.get("keithley/2400").requires == []
    base = {"card": 1, "id": "a/b", "model": "B", "vendor": "A", "kind": "dmm",
            "parameters": {"V": {"kind": "measure", "unit": "V", "get": "V?"}}}
    assert parse_card(dict(base, requires=["pylablib-lightweight>=1.4"])).requires == ["pylablib-lightweight>=1.4"]
    with pytest.raises(CardError, match="the maker's Python packages"):
        parse_card(dict(base, requires=["pylablib; echo"]))


def test_a_missing_package_says_how_kelvin_installs_it():
    said = envs.missing("qick", "Pyro4")
    assert said.startswith("qick and Pyro4 are not on this computer")
    assert 'install_for_instrument with env drivers and packages ["qick", "Pyro4"]' in said
    assert envs.lacking(["pytest", "surely-not-installed-anywhere"]) == ["surely-not-installed-anywhere"]
    assert envs.have("PyTest") == envs.have("pytest") is not None
