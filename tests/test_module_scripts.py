"""A lab's code organised as a package, run as the lab runs it: python -m nvpulse check soc (scriptrun.py).

Field report 2026-11-13: run_script ran nvpulse/__main__.py as a file on its own, so its first line, from .cli.main
import main, failed ("attempted relative import with no known parent package"); check_script took no package's name;
and a run could only use the Python set for the whole lab, not the conda environment the lab runs the package with.
Now a package's module runs as python -m runs it (its package imported first, the folder above it first on sys.path
and the folder it runs in), run_script and check_script take module and python, and an import a module only tries
needs no Python."""
import json
import os
import re
import sys
import threading
from pathlib import Path

import pytest

from labharness import scriptrun
from labharness.eventlog import EventLog, RunStore
from labharness.gate import SafetyGate, ScriptedApprover

from test_script_runs import folder, lab, output, script_runtime, write  # noqa: F401 (fixtures)

MAIN = """
from .cli.main import main

REPEATS = 1   # how many times it asks the board

main(repeats=REPEATS)
"""

CLI = """
import argparse
import os
import sys

from ..hw import soc


def main(argv=None, repeats=1):
    parser = argparse.ArgumentParser(prog="nvpulse")
    sub = parser.add_subparsers(dest="command")
    check = sub.add_parser("check")
    check.add_argument("what")
    args = parser.parse_args(argv)
    import __main__
    print("package:", __main__.__package__, "spec:", __main__.__spec__.name)
    print("cwd:", os.getcwd())
    print("path0:", sys.path[0])
    print("argv0:", os.path.basename(sys.argv[0]))
    if args.command == "check" and args.what == "soc":
        for _ in range(repeats):
            print("soc says:", soc.identify())
"""

SOC = """
import pyvisa

ADDRESS = "GPIB0::8::INSTR"


def identify():
    board = pyvisa.ResourceManager().open_resource(ADDRESS)
    return board.query("*IDN?")
"""


def put(root, name, text):
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    return write(root, name, text)


def nvpulse(root):
    """The field report's package, at the top of a scripts folder."""
    for name, text in (("nvpulse/__init__.py", '"""NV pulsed measurements."""\n'), ("nvpulse/__main__.py", MAIN),
                       ("nvpulse/cli/__init__.py", ""), ("nvpulse/cli/main.py", CLI),
                       ("nvpulse/hw/__init__.py", ""), ("nvpulse/hw/soc.py", SOC)):
        put(root, name, text)
    return root / "nvpulse"


def _python(script, source, roots, configured="", named="", **package):
    return scriptrun.Python([sys.executable, "-u"], "the test's Python", tuple(sys.version_info[:2]))


def run(lab, roots, tmp_path, script, args=(), params=None, answers=(True,)):
    gate = SafetyGate(lab, ScriptedApprover(list(answers)), EventLog(None, lab.clock))
    prep = scriptrun.prepare(lab, script, roots, params=params, args=list(args), choose_python=_python)
    request, grants, problems = scriptrun.approval(gate, prep)
    assert not problems
    assert gate.ask(request)
    allowed = set(prep.found.unknown) | set(prep.found.libraries)
    report = scriptrun.ScriptRun(gate, prep, RunStore(tmp_path / "runs"), grants, allowed).execute()
    return prep, request, report


def test_a_packages_main_runs_as_python_m_runs_it(lab, folder, tmp_path):
    """The field report: nvpulse/__main__.py with check soc, its first line a relative import."""
    nvpulse(folder)
    prep, request, report = run(lab, [folder], tmp_path, "nvpulse/__main__.py", ["check", "soc"])
    out = output(report)
    assert report.status == "done", report.message + "\n" + out
    assert "attempted relative import" not in out
    assert "package: nvpulse spec: nvpulse.__main__" in out
    assert "cwd: %s" % folder in out and "path0: %s" % folder in out   # as python -m from the scripts folder
    assert "argv0: __main__.py" in out
    assert re.search(r"soc says: \S", out)
    # it is named as python -m names it, and the card says how it runs
    assert prep.module == "nvpulse.__main__" and prep.name == "nvpulse"
    assert report.run_id.endswith("-nvpulse")
    text = str(request)
    assert "Run script nvpulse?" in text
    assert "as python -m nvpulse in %s, the folder above its package" % folder in text
    # the package was read ahead: the board its hw module opens is on the card, and its command went through the gate
    assert "It opens lockin (GPIB0::8::INSTR)" in text
    commands = [json.loads(ln) for ln in (Path(report.path) / "commands.jsonl").read_text().splitlines()]
    assert any(c["instrument"] == "lockin" and c["ok"] for c in commands)
    # what the next run's card compares: the package's other files, by their paths
    assert "nvpulse/cli/main.py" in prep.near and "nvpulse/hw/soc.py" in prep.near
    assert "nvpulse/__main__.py" not in prep.near


def test_a_changed_setting_runs_in_the_package_too(lab, folder, tmp_path):
    """params: the copy that runs (script.py, through --source) is still the package's __main__."""
    nvpulse(folder)
    prep, request, report = run(lab, [folder], tmp_path, "-m nvpulse", ["check", "soc"], params={"REPEATS": 2})
    out = output(report)
    assert report.status == "done", report.message + "\n" + out
    assert out.count("soc says:") == 2
    assert "REPEATS = 2   # how many times" in (Path(report.path) / "script.py").read_text(encoding="utf-8")
    assert (folder / "nvpulse" / "__main__.py").read_text(encoding="utf-8").count("REPEATS = 1") == 1   # untouched


def test_a_module_is_named_as_python_m_names_it(folder):
    pkg = nvpulse(folder)
    main = (pkg / "__main__.py").resolve()
    for name in ("-m nvpulse", "python -m nvpulse", "py -3 -m nvpulse", "nvpulse", str(pkg), "nvpulse/__main__.py"):
        assert scriptrun.locate(name, [folder]) == main, name
    assert scriptrun.locate("nvpulse.cli.main", [folder]) == (pkg / "cli" / "main.py").resolve()
    assert scriptrun.locate_module("nvpulse", [folder]) == (main, "nvpulse.__main__", folder.resolve())
    assert scriptrun.locate_module("nvpulse.hw.soc", [folder])[1:] == ("nvpulse.hw.soc", folder.resolve())
    with pytest.raises(scriptrun.ScriptError, match="give its arguments in args"):
        scriptrun.locate("python -m nvpulse check soc", [folder])
    with pytest.raises(scriptrun.ScriptError, match="no module nosuch in the lab's scripts folder"):
        scriptrun.locate("-m nosuch", [folder])
    with pytest.raises(scriptrun.ScriptError, match="nor a package or module of that name"):
        scriptrun.locate("nosuch", [folder])
    with pytest.raises(scriptrun.ScriptError, match="is a folder: name a script in it"):
        scriptrun.locate("nvpulse/hw", [folder])   # a package with no __main__.py: python -m can't run it either
    # a module installed in a Python is never what runs: only the lab's own code
    with pytest.raises(scriptrun.ScriptError, match="not a module installed in a Python"):
        scriptrun.locate_module("json", [folder])
    # nor from above a scripts folder: one that is the package itself says which folder to add
    with pytest.raises(scriptrun.ScriptError, match=r"is the scripts folder .*nvpulse itself: python -m runs it from "
                                                    r"the folder above"):
        scriptrun.locate_module("nvpulse", [pkg])


def test_a_package_in_a_sub_folder_runs_from_the_folder_that_holds_it(folder, tmp_path):
    sub = folder / "NV pulsed measurement"
    sub.mkdir()
    nvpulse(sub)
    file, module, home = scriptrun.locate_module("nvpulse", [folder])
    assert (file, module, home) == ((sub / "nvpulse" / "__main__.py").resolve(), "nvpulse.__main__", sub.resolve())
    other = folder / "copy"
    other.mkdir()
    nvpulse(other)
    with pytest.raises(scriptrun.ScriptError, match="several modules nvpulse"):
        scriptrun.locate_module("nvpulse", [folder])
    # a second copy is never missed, however many sub-folders hold something of that name without the module
    deep = tmp_path / "deep"
    for k in range(1, 8):
        put(deep, "s%d/analysis/__init__.py" % k, "")
    for k in (3, 7):
        put(deep, "s%d/analysis/fit.py" % k, "from . import x\n")
    with pytest.raises(scriptrun.ScriptError, match=r"several modules analysis\.fit"):
        scriptrun.locate_module("analysis.fit", [deep])


def test_only_a_script_that_cant_run_as_a_file_runs_as_a_module(tmp_path):
    """A script runs as python -m runs it only when it could not run as a file: its relative imports run as it loads
    (a package's __main__.py: anywhere in it). Every other script runs as a file, as it always did."""
    root = tmp_path / "NV pulsed measurement"   # a space: no package's name, so never part of one
    pkg = nvpulse(root)
    of = scriptrun.module_of
    assert of(pkg / "__main__.py", MAIN, [root]) == ("nvpulse.__main__", root)
    assert of(pkg / "cli" / "main.py", CLI, [root]) == ("nvpulse.cli.main", root)
    assert of(pkg / "__main__.py", "def go():\n    from .cli.main import main\n    main()\ngo()\n", [root]) == \
        ("nvpulse.__main__", root)
    # as a file, as before
    assert of(pkg / "__main__.py", "import sys\nprint(sys.argv)\n", [root]) == ("", pkg)   # python nvpulse runs it so
    assert of(pkg / "hw" / "sweep.py", "import soc\nsoc.identify()\n", [root]) == ("", pkg / "hw")   # its helpers
    assert of(pkg / "hw" / "sweep.py", "from nvpulse.hw import soc\n", [root]) == ("", pkg / "hw")   # installed so
    fallback = "try:\n    from .soc import identify\nexcept ImportError:\n    from soc import identify\n"
    assert of(pkg / "hw" / "sweep.py", fallback, [root]) == ("", pkg / "hw")   # made to run both ways
    both = ("if __name__ == '__main__':\n    import soc\nelse:\n    from . import soc\n",
            "if __package__:\n    from . import soc\nelse:\n    import soc\n",
            "if not __spec__:\n    import soc\nelif True:\n    from . import soc\n",
            "try:\n    from . import soc\nexcept Exception:\n    import soc\n",   # whatever the try catches
            "try:\n    from . import soc\nexcept:\n    import soc\n",
            "try:\n    import soc\nexcept ImportError:\n    from . import soc\n")   # and the other way round
    for text in both:   # so too, on how it runs
        assert of(pkg / "hw" / "sweep.py", text, [root]) == ("", pkg / "hw"), text
    assert of(pkg / "hw" / "sweep.py", "def later():\n    from . import soc\n", [root]) == ("", pkg / "hw")
    assert of(pkg / "hw" / "sweep.py", "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from . import soc\n",
              [root]) == ("", pkg / "hw")
    # a __main__.py's own if __name__ == "__main__": runs either way; a whole script's except Exception is no fallback
    assert of(pkg / "__main__.py", "if __name__ == '__main__':\n    from .cli.main import main\n    main()\n",
              [root]) == ("nvpulse.__main__", root)
    assert of(pkg / "hw" / "sweep.py", "try:\n    from . import soc\nexcept Exception:\n    raise\n", [root]) == \
        ("nvpulse.hw.sweep", root)
    # in two scripts folders, one inside the other (as the folder python -m runs from was added): the outer one
    assert of(pkg / "__main__.py", MAIN, [pkg, root]) == ("nvpulse.__main__", root)
    # a driver beside the scripts that use it, its folder named as it is: the script finds the driver, as before
    drivers = root / "sr830"
    put(root, "sr830/__init__.py", "")
    put(root, "sr830/sr830.py", "class SR830:\n    pass\n")
    assert of(drivers / "measure.py", "from sr830 import SR830\n", [root]) == ("", drivers)
    # a folder that is not a package stops it; so does a file that is no module's name
    assert of(root / "run.py", "from . import x\n", [root]) == ("", root)
    assert of(pkg / "cli" / "two-words.py", "from . import main\n", [root]) == ("", pkg / "cli")
    # never from above the scripts folder: an __init__.py of its own makes it no part of the package
    lab = tmp_path / "lab"
    inner = nvpulse(lab)
    (lab / "__init__.py").write_text("")
    assert of(inner / "__main__.py", MAIN, [lab]) == ("nvpulse.__main__", lab)
    # one that needs the scripts folder as its package says which folder python -m runs it from
    with pytest.raises(scriptrun.ScriptError, match=r"is the scripts folder .*nvpulse itself: python -m runs it from "
                                                    r"the folder above, .*NV pulsed measurement"):
        of(pkg / "__main__.py", MAIN, [pkg])
    with pytest.raises(scriptrun.ScriptError, match="lab.nvpulse.__main__'s package is the scripts folder"):
        of(inner / "__main__.py", "from .. import settings\n", [lab])
    # a script a person imported, in no scripts folder: its package as far as its __init__.py files go
    assert of(pkg / "__main__.py", MAIN, []) == ("nvpulse.__main__", root)


def test_a_package_module_given_by_its_path_runs_as_its_module(lab, folder, tmp_path):
    pkg = nvpulse(folder)
    put(pkg, "tools/__init__.py", "")
    put(pkg, "tools/look.py", """
        from ..hw import soc

        if __name__ == "__main__":
            print("look:", soc.identify())
        """)
    prep, request, report = run(lab, [folder], tmp_path, "nvpulse/tools/look.py")
    assert report.status == "done", report.message + output(report)
    assert prep.module == "nvpulse.tools.look" and "look: " in output(report)
    assert "as python -m nvpulse.tools.look in" in str(request)


def test_a_script_beside_its_helpers_in_a_package_folder_still_runs_as_a_file(lab, folder, tmp_path):
    write(folder, "__init__.py", "")
    write(folder, "helpers.py", "def hello():\n    return 'hello from beside'\n")
    write(folder, "measure.py", "import helpers\nprint(helpers.hello())\n")
    prep, request, report = run(lab, [folder], tmp_path, "measure.py")
    assert report.status == "done", report.message + output(report)
    assert prep.module == "" and "hello from beside" in output(report)
    assert "python -m" not in str(request)


def test_a_lab_package_named_as_one_python_has_already_says_so(lab, folder, tmp_path):
    """A package named as a module Python or Kelvin's stand-in has loaded before the script (json, serial) can't run as
    the lab's: Kelvin says so before anyone is asked, and the stand-in never imports another of that name in its
    place."""
    put(folder, "serial/__init__.py", "")
    put(folder, "serial/__main__.py", "from . import tool\n")
    put(folder, "serial/tool.py", "print('the lab serial')\n")
    with pytest.raises(scriptrun.ScriptError, match="called serial, as a module Kelvin's stand-in loads first is"):
        scriptrun.prepare(lab, "serial", [folder], choose_python=_python)
    if hasattr(sys, "stdlib_module_names"):   # Python 3.10 and later know their own
        put(folder, "json/__init__.py", "")
        put(folder, "json/__main__.py", "from . import tool\n")
        with pytest.raises(scriptrun.ScriptError, match="called json, as a module of Python's own is"):
            scriptrun.prepare(lab, "json", [folder], choose_python=_python)
    # and the stand-in itself: Python's json is the one it finds, so it imports nothing of the lab's
    import importlib.util
    import types
    spec = importlib.util.spec_from_file_location("shim_for_test", scriptrun.SHIM)
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)
    with pytest.raises(ImportError, match="Python found another json first"):
        shim._as_module(types.ModuleType("__main__"), "json.__main__", str(folder / "json" / "__main__.py"),
                        str(folder))


def test_a_pyvisa_kept_beside_the_scripts_reaches_the_lab_through_kelvin(lab, folder, tmp_path, monkeypatch):
    """A Python with no pyvisa of its own, and a copy of pyvisa beside the lab's scripts: the copy is imported only
    once the script runs, and still gets Kelvin's stand-in, so its instruments reach the gate (before, it went past)."""
    from test_script_runs import FAKE_PYVISA
    put(folder, "pyvisa/__init__.py", FAKE_PYVISA)   # it refuses every instrument: one it opened failed the run
    write(folder, "look.py", """
        import pyvisa
        print("X =", pyvisa.ResourceManager().open_resource("GPIB0::8::INSTR").query("MEAS:X?"))
        """)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "nothing"))   # the test's fake pyvisa is not this Python's

    def bare_python(script, source, roots, configured="", named="", **package):   # -S: no site-packages, no pyvisa
        return scriptrun.Python([sys.executable, "-S", "-u"], "a Python with no pyvisa", tuple(sys.version_info[:2]))
    gate = SafetyGate(lab, ScriptedApprover([True]), EventLog(None, lab.clock))
    prep = scriptrun.prepare(lab, "look.py", [folder], choose_python=bare_python)
    request, grants, _ = scriptrun.approval(gate, prep)
    assert gate.ask(request)
    report = scriptrun.ScriptRun(gate, prep, RunStore(tmp_path / "runs"), grants, set()).execute()
    assert report.status == "done", report.message + "\n" + output(report)
    assert "X = " in output(report) and report.commands == 1


def test_an_import_a_script_only_tries_needs_no_python(tmp_path):
    script = write(tmp_path, "s.py", """
        import json
        from typing import TYPE_CHECKING
        try:
            import no_such_module_anywhere
        except ImportError:
            no_such_module_anywhere = None
        if TYPE_CHECKING:
            import no_such_typing_module
        """)
    source = script.read_text()
    ours = [m for m in scriptrun.imports(source) if m.startswith("no_such")]   # (Python 3.9 can't tell its own)
    assert ours == ["no_such_module_anywhere", "no_such_typing_module"]
    assert not [m for m in scriptrun.imports(source, tried=False) if m.startswith("no_such")]
    py = scriptrun.python_for(script, source, [tmp_path], configured=sys.executable)
    assert py.version == tuple(sys.version_info[:2])
    with pytest.raises(scriptrun.ScriptError, match="lacks no_such_module_anywhere"):   # one it needs: as before
        scriptrun.python_for(script, "import no_such_module_anywhere\n", [tmp_path], configured=sys.executable)
    # a whole script in a try whose except catches anything still needs what it imports: else a Python without pyvisa
    # would run it, its own except would swallow the missing module, and the run would end done, having done nothing
    wrapped = "try:\n    import no_such_module_anywhere\n    print('measured')\nexcept Exception as e:\n    print(e)\n"
    with pytest.raises(scriptrun.ScriptError, match="lacks no_such_module_anywhere"):
        scriptrun.python_for(script, wrapped, [tmp_path], configured=sys.executable)
    with pytest.raises(scriptrun.ScriptError, match="lacks no_such_module_anywhere"):
        scriptrun.python_for(script, wrapped.replace("except Exception as e:", "except:").replace("print(e)",
                                                                                                    "pass"),
                             [tmp_path], configured=sys.executable)


def test_the_python_for_a_package_has_what_the_run_imports(folder, monkeypatch):
    """What a package's module needs is followed through the package as python -m imports it: what the modules it
    imports for sure import (nvpulse's hw, through its cli), not its tests or a module only a function imports."""
    pkg = nvpulse(folder)
    put(pkg, "analysis.py", "import no_such_plotting_module\n")   # never imported by check soc
    put(pkg, "tests/test_cli.py", "import pytest\nimport no_such_test_helper\n")
    main = pkg / "__main__.py"
    needed, tried = scriptrun._run_imports(main, MAIN, "nvpulse.__main__", folder)
    assert "pyvisa" in needed   # hw/soc.py, through cli/main.py
    assert not {"no_such_plotting_module", "no_such_test_helper", "pytest"} & set(needed + tried)
    monkeypatch.setattr(scriptrun, "_candidates", lambda script, roots: [([sys.executable, "-u"], "the test's")])
    scriptrun._PROBED.clear()
    for configured in (sys.executable, ""):   # set by a person, or picked by Kelvin
        py = scriptrun.python_for(main, MAIN, [folder], configured=configured, module="nvpulse.__main__",
                                  home=folder)
        assert py.version == tuple(sys.version_info[:2])
    # a module only a function imports is tried, not needed
    cli = (pkg / "cli" / "main.py").read_text() + "\n\ndef plot():\n    from .. import analysis\n"
    put(pkg, "cli/main.py", cli)
    needed, tried = scriptrun._run_imports(main, MAIN, "nvpulse.__main__", folder)
    assert "no_such_plotting_module" in tried and "no_such_plotting_module" not in needed
    assert scriptrun.python_for(main, MAIN, [folder], module="nvpulse.__main__", home=folder)
    # one the run imports for sure needs what it imports: a Python without it is refused, saying what it lacks
    put(pkg, "hw/soc.py", SOC.replace("import pyvisa", "import pyvisa\nimport no_such_board_module"))
    for configured in (sys.executable, ""):
        with pytest.raises(scriptrun.ScriptError, match="no Python here has everything nvpulse imports: .* lacks "
                                                        "no_such_board_module"):
            scriptrun.python_for(main, MAIN, [folder], configured=configured, module="nvpulse.__main__",
                                 home=folder)


def test_a_package_named_by_its_name_runs_as_python_m_whatever_it_imports(lab, folder, tmp_path):
    """Named so (nvpulse, its folder), a package runs as python -m runs it, also a __main__.py that imports its
    package by name only; given by its file's path, that one runs as a file, as before, and its failure says how to
    run it."""
    pkg = nvpulse(folder)
    put(pkg, "__main__.py", "from nvpulse.cli.main import main\n\nmain()\n")
    for name in ("nvpulse", str(pkg)):
        prep = scriptrun.prepare(lab, name, [folder], choose_python=_python)
        assert prep.module == "nvpulse.__main__" and prep.folder == folder.resolve(), name
    prep, request, report = run(lab, [folder], tmp_path, "nvpulse", ["check", "soc"])
    assert report.status == "done" and "soc says:" in output(report), report.message + output(report)
    prep, request, report = run(lab, [folder], tmp_path, "nvpulse/__main__.py", ["check", "soc"])
    assert prep.module == "" and report.status == "aborted"
    assert "No module named 'nvpulse'" in report.message
    assert "run_script with module: nvpulse runs it as python -m does" in report.message


def test_a_big_packages_running_module_is_always_read_ahead(lab, folder, tmp_path):
    pkg = nvpulse(folder)
    for k in range(310):   # more than a survey reads
        put(pkg, "aa/m%03d.py" % k, "x = %d\n" % k)
    put(pkg, "zz/__init__.py", "")
    put(pkg, "zz/run.py", """
        import pyvisa
        from ..hw import soc

        smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
        smu.write("SOUR:VOLT 1")
        """)
    prep = scriptrun.prepare(lab, "nvpulse/zz/run.py", [folder], choose_python=_python)
    assert prep.module == "nvpulse.zz.run"
    request, _, _ = scriptrun.approval(SafetyGate(lab, ScriptedApprover([])), prep)
    text = str(request)
    assert "smu (GPIB0::24::INSTR)" in text and "the first 300: the gate checks the rest's commands" in text


def test_a_relative_import_that_fails_as_a_file_says_how_to_run_it(lab, folder, tmp_path):
    pkg = nvpulse(folder)
    put(pkg, "tools/__init__.py", "")
    put(pkg, "tools/late.py", """
        def main():
            from ..hw import soc
            print(soc.identify())

        main()
        """)
    prep, request, report = run(lab, [folder], tmp_path, "nvpulse/tools/late.py")
    assert prep.module == ""   # its relative import is in a function, which as a file it may never call
    assert report.status == "aborted" and "attempted relative import" in report.message
    assert "it is the module nvpulse.tools.late of a package: run_script with module: nvpulse.tools.late" \
        in report.message


def test_a_package_is_read_ahead_whole_its_sub_package_lib_too(lab, folder, tmp_path):
    pkg = nvpulse(folder)
    put(pkg, "lib/__init__.py", "")
    put(pkg, "lib/board.py", """
        import pyvisa
        import zhinst

        smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
        smu.write("SOUR:VOLT 2")
        """)
    prep = scriptrun.prepare(lab, "-m nvpulse", [folder], choose_python=_python)
    request, _, _ = scriptrun.approval(SafetyGate(lab, ScriptedApprover([])), prep)
    text = str(request)
    assert "smu (GPIB0::24::INSTR)" in text and "It uses zhinst" in text
    assert "Read ahead: its whole package nvpulse (8 files)" in text
    assert "nvpulse/lib/board.py" in prep.near


def _conda_env(prefix, base=False):
    (prefix / "conda-meta").mkdir(parents=True)
    python = scriptrun._env_python(prefix)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("")
    if base:
        (prefix / "condabin").mkdir()
    return prefix


def _fake_conda(tmp_path, monkeypatch):
    """A conda install of its own in tmp_path/home, the person's home here (base, and envs/nvpulse), as their conda
    lists them, and none of this computer's."""
    home = tmp_path / "home"
    base = _conda_env(home / "anaconda3", base=True)
    env = _conda_env(base / "envs" / "nvpulse")
    (home / ".conda").mkdir()
    (home / ".conda" / "environments.txt").write_text("%s\n%s\n" % (base, env))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for var in ("LOCALAPPDATA", "PROGRAMDATA", "CONDA_EXE"):
        monkeypatch.delenv(var, raising=False)
    return base, env


def test_a_run_may_name_one_of_this_computers_conda_environments(tmp_path, monkeypatch):
    base, env = _fake_conda(tmp_path, monkeypatch)
    assert scriptrun.conda_envs() == [("base", base), ("nvpulse", env)]
    # only what the person's own conda knows: not an install anyone could put in a shared folder (ProgramData), nor an
    # environment in the envs folder of a shared install (their CONDA_EXE's: its base only)
    shared = tmp_path / "ProgramData"
    _conda_env(shared / "miniforge3", base=True)
    monkeypatch.setenv("PROGRAMDATA", str(shared))
    assert scriptrun.conda_envs() == [("base", base), ("nvpulse", env)]
    everyone = _conda_env(shared / "Anaconda3", base=True)
    _conda_env(everyone / "envs" / "planted")
    monkeypatch.setenv("CONDA_EXE", str(everyone / "Scripts" / "conda.exe"))
    assert scriptrun.conda_envs() == [(str(base), base), ("nvpulse", env), (str(everyone), everyone)]
    monkeypatch.delenv("CONDA_EXE")
    # two of one name: each by its folder, so naming one never picks the other unsaid
    local = tmp_path / "local"
    other = _conda_env(local / "miniconda3", base=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert scriptrun.conda_envs() == [(str(base), base), ("nvpulse", env), (str(other), other)]
    script0 = write(tmp_path, "s0.py", "print(1)\n")
    assert scriptrun.python_named(script0, [tmp_path], "", "base") is None
    monkeypatch.delenv("LOCALAPPDATA")
    script = write(tmp_path, "s.py", "print(1)\n")
    python = scriptrun._env_python(env)
    want = ([str(python), "-u"], "%s (the conda environment nvpulse)" % python)
    for named in ("nvpulse", str(env), str(python), want[1] + ", Python 3.10"):
        assert scriptrun.python_named(script, [tmp_path], "", named) == want, named
    assert scriptrun.python_named(script, [tmp_path], "", str(tmp_path / "elsewhere" / "python.exe")) is None
    assert scriptrun.python_named(script, [tmp_path], "", "nvpulse2") is None
    # it runs as conda activate leaves it: the environment's folders first on PATH
    env_vars = scriptrun.activated([str(python), "-u"], {"PATH": "elsewhere"})
    assert env_vars["CONDA_PREFIX"] == str(env) and env_vars["CONDA_DEFAULT_ENV"] == "nvpulse"
    first = env_vars["PATH"].split(os.pathsep)
    assert first[0] == str(env if os.name == "nt" else env / "bin") and first[-1] == "elsewhere"
    assert scriptrun.activated([sys.executable, "-u"], {"PATH": "x"}) == {"PATH": "x"}   # any other Python: as it is


def test_a_named_python_is_said_on_the_card(lab, folder, tmp_path, monkeypatch):
    nvpulse(folder)
    monkeypatch.setattr(scriptrun, "conda_envs", lambda: [("nvpulse", tmp_path / "envs" / "nvpulse")])
    monkeypatch.setattr(scriptrun, "_env_python", lambda prefix: Path(sys.executable))   # it runs the test's Python
    scriptrun._PROBED.clear()
    prep = scriptrun.prepare(lab, "-m nvpulse", [folder], args=["check", "soc"], named="nvpulse")
    assert prep.python.text.startswith("%s (the conda environment nvpulse), Python " % sys.executable)
    request, _, _ = scriptrun.approval(SafetyGate(lab, ScriptedApprover([])), prep)
    assert request.items[0].startswith("nvpulse runs with %s (the conda environment nvpulse)" % sys.executable)
    with pytest.raises(scriptrun.ScriptError, match="is not a Python Kelvin runs the lab's scripts with here"):
        scriptrun.prepare(lab, "-m nvpulse", [folder], named=str(tmp_path / "evil" / "python.exe"))


def test_run_script_and_check_script_take_module_and_python(lab, folder, tmp_path):
    """Over MCP, as the chat calls them: check_script with module, then run_script with module, its args and the
    Python the person named; the library keeps the run as nvpulse."""
    pytest.importorskip("mcp")
    anyio = pytest.importorskip("anyio")
    from mcp import Client
    from labharness.agent.server import build_server
    nvpulse(folder)
    rt = script_runtime(lab, folder, tmp_path, timeout=30)
    asked = []

    def person():
        entry = rt.approver.wait_for_pending(timeout=60)
        asked.append(entry)
        rt.approver.answer(entry["id"], True)
    threading.Thread(target=person, daemon=True).start()

    async def main():
        async with Client(build_server(rt)) as client:
            out = []
            for name, call in (("check_script", {"module": "nvpulse"}),
                               ("check_script", {"script": "nvpulse"}),
                               ("check_script", {"script": "nvpulse", "module": "nvpulse"}),
                               ("run_script", {"module": "nvpulse", "args": ["check", "soc"],
                                               "python": sys.executable})):
                r = await client.call_tool(name, call)
                out.append("\n".join(c.text for c in r.content if getattr(c, "type", "") == "text"))
            return out
    by_module, by_name, both, ran = anyio.run(main)
    assert "can't run it" not in by_module and "nvpulse (nvpulse/__main__.py) in %s" % folder.resolve() in by_module
    assert "settings it takes (params): REPEATS = 1   # how many times it asks the board" in by_module
    assert "as python -m nvpulse in" in by_module and "It opens lockin (GPIB0::8::INSTR)" in by_module
    assert "can't run it" not in by_name
    assert "give script or module, not both" in both
    assert asked and asked[0]["title"] == "Run script nvpulse?"
    assert "script nvpulse done" in ran, ran
    assert "soc says:" in ran
    [entry] = [e for e in rt.library.entries() if e["kind"] == "script"]
    assert entry["name"] == "nvpulse" and entry["path"].endswith("__main__.py") and entry["module"] == "nvpulse"


def test_a_queued_module_item_is_read_as_run_script_reads_it(lab, folder, tmp_path):
    from labharness.scanqueue import load_script_item
    nvpulse(folder)
    rt = script_runtime(lab, folder, tmp_path)
    spec = load_script_item("script: -m nvpulse\nargs: [check, soc]\npython: %s\n" % json.dumps(sys.executable))
    assert spec.name == "-m nvpulse"
    path, source = rt._script_text(spec)
    assert path == (folder / "nvpulse" / "__main__.py").resolve() and "from .cli.main import main" in source
    prep = rt._prepare_script(spec.path, spec.params, spec.args, python=spec.python)
    assert prep.module == "nvpulse.__main__" and prep.args == ["check", "soc"]


def test_a_module_run_runs_again_as_python_m_runs_it(lab, folder, tmp_path):
    """Run again, from its run or the library: a module that ran as python -m runs so again (one asked for as -m
    may import its package by name only, which a file can't), while it is in the scripts folders; a file by its
    path, as before."""
    pkg = nvpulse(folder)
    put(pkg, "tool.py", "import nvpulse.hw\nprint('tool')\n")   # no relative import: python -m only when asked so
    rt = script_runtime(lab, folder, tmp_path)
    prep = rt._prepare_script("-m nvpulse.tool")
    assert prep.module == "nvpulse.tool" and prep.folder == folder.resolve()
    assert rt._again(str(prep.path), "nvpulse.tool") == "-m nvpulse.tool"
    assert rt._again(str(prep.path), None) == str(prep.path)                       # it ran as a file
    assert rt._again(str(folder / "gone.py"), "nvpulse.gone") == str(folder / "gone.py")   # its run's copy, by path
    elsewhere = put(tmp_path, "imported/nvpulse/__main__.py", MAIN)                 # one a person imported
    assert rt._again(str(elsewhere), "nvpulse") == str(elsewhere)
    # another copy that python -m nvpulse would find first: this one runs again by its path, never that one
    copy = nvpulse(folder / "v2") / "__main__.py"
    assert rt._again(str(copy.resolve()), "nvpulse") == str(copy.resolve())
    assert rt._again(str((pkg / "__main__.py").resolve()), "nvpulse") == "-m nvpulse"
