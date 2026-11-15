"""Field report 2026-11-15, the lab's scripts: an NV pulsed-measurement rig brought up on Windows 11 (a QICK board, a
Cobolt 06-MLD 520 nm laser on COM11, an IDQ Time Controller, and the lab's own package nvpulse, run as python -m
nvpulse from a conda environment).

Item 10: the chat's folder was also the lab's scripts folder, so Kelvin could save nothing there, not even a report in
a new subfolder. Now it may make a subfolder of its own there for notes, tables and pictures, and nothing else.
Item 12: calculate took one expression; a list is answered too, each on its own line.
Item 1 (checked, not changed): a package's __main__.py given by its path runs as python -m runs it, and a relative
import that fails says how to run it: test_module_scripts.py covers both."""
import asyncio
import os
from pathlib import Path

import pytest

from labharness import folderfiles
from labharness.gate import ScriptedApprover

from test_script_runs import folder, lab, script_runtime, write  # noqa: F401 (fixtures)


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
    """The field report: write_file of kelvin_reports\\nv_bringup.md was refused. A new subfolder is Kelvin's own: it
    saves notes, tables and pictures there, and finds it its own after a restart (a marker in it)."""
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
        "report.md": "a new subfolder", "data/odmr.csv": "a new subfolder", "nvpulse/notes.md": "a new subfolder",
        "tools/README.md": "a new subfolder",
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
    assert "a new subfolder" in str(e.value)
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
    assert "new subfolder" in WRITE_FILE["description"]
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
    assert code["is_error"] and "new subfolder" in code["content"], code
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
