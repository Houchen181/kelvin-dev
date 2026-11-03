"""Several folders of the lab's scripts (Settings → Instruments → Your lab's scripts, Set up, the first start): a list
kept in settings.json, where one folder kept as text before reads as a list of it and is written back as a list only
once the folders change; added and taken off one at a time, the same folder twice kept once and one inside another
left out. run_script runs a script inside any of them and nothing outside them all, nor through a link out of one;
the survey, the New function picker and write_file's refusal cover every folder; the page has a row per folder with
Remove, and Add folder…."""
import json
import os

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")

from labharness import ScriptedApprover, folderfiles, scanlibrary, scriptrun  # noqa: E402
from labharness import scripts as lab_scripts  # noqa: E402
from labharness.web.app import STATIC  # noqa: E402
from labharness.web.settings import Settings, fold_folders  # noqa: E402

from test_scan_library import READS, SCRIPT, script_runtime, scripts, write  # noqa: E402,F401
from test_scan_review import ai_saves  # noqa: E402
from test_web import Scripted, client_for, parts  # noqa: E402,F401

IV = 'import pyvisa\nsmu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")\nsmu.write(":SOUR:VOLT 1")\n'
LASER = 'import serial\nlaser = serial.Serial("COM7", 115200)\nlaser.write(b"POW 5\\n")\n'
URL = "/api/instruments/scripts-folder"


def folder_link(target, link):
    """A folder that leads to another: a junction on Windows (no privilege needed), a symbolic link elsewhere."""
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(str(target), str(link), target_is_directory=True)
    except (OSError, ImportError, AttributeError) as e:
        pytest.skip("no folder link here: %s" % e)


def _fn(src, name):
    start = src.index("function %s(" % name)
    return src[start:src.index("\n  }\n", start)]


# ------------------------------------------------------------------ kept in settings.json

def test_one_folder_kept_as_text_reads_as_a_list_until_the_folders_change(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"lab_scripts": str(a)}), encoding="utf-8")   # as an older Kelvin kept it
    settings = Settings(path)
    assert settings.lab_scripts == [str(a)] and settings.public()["lab_scripts"] == [str(a)]
    settings.update({"answer_words": "100"})                              # another change: the text stays text
    assert json.loads(path.read_text(encoding="utf-8"))["lab_scripts"] == str(a)
    assert settings.set_lab_scripts(settings.lab_scripts + [str(b)]) == ([str(a), str(b)], [])
    assert json.loads(path.read_text(encoding="utf-8"))["lab_scripts"] == [str(a), str(b)]   # a list from now on
    assert Settings(path).lab_scripts == [str(a), str(b)]
    path.write_text(json.dumps({"lab_scripts": ""}), encoding="utf-8")
    assert Settings(path).lab_scripts == [] and Settings(path).public()["lab_scripts"] == []   # "" was none
    path.write_text(json.dumps({}), encoding="utf-8")
    assert Settings(path).lab_scripts is None                             # nobody has been asked yet


def test_the_same_folder_twice_and_one_inside_another_are_folded(tmp_path):
    lab, other = tmp_path / "Lab", tmp_path / "Other"
    (lab / "scripts" / "old").mkdir(parents=True)
    other.mkdir()
    scripts_dir = lab / "scripts"
    kept, said = fold_folders([str(scripts_dir), str(other), str(scripts_dir) + os.sep, str(scripts_dir / "old")])
    assert kept == [str(scripts_dir), str(other)]
    assert said == ["%s%s is listed already" % (scripts_dir, os.sep),
                    "%s covers %s" % (scripts_dir, scripts_dir / "old")]
    kept, said = fold_folders([str(scripts_dir), str(other), str(lab)])  # an outer one takes the place of those in it
    assert kept == [str(other), str(lab)] and said == ["%s covers %s" % (lab, scripts_dir)]
    if os.name == "nt":                                                   # Windows' paths compare without case
        assert fold_folders([str(lab), str(lab).upper()])[0] == [str(lab)]
    folder_link(other, tmp_path / "to-other")                             # a link to a folder is that folder
    assert fold_folders([str(other), str(tmp_path / "to-other")]) == ([str(other)], [
        "%s is listed already" % (tmp_path / "to-other")])


# ------------------------------------------------------------------ over HTTP: Settings, the first start, Set up

def test_folders_are_added_and_taken_off_one_at_a_time(parts, tmp_path):
    runtime, settings, _ = parts
    a, b = tmp_path / "lab" / "scripts", tmp_path / "shared" / "measure"
    (a / "sub").mkdir(parents=True)
    b.mkdir(parents=True)
    write(a, "iv.py", IV)
    write(b, "laser.py", LASER)
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        r = client.post(URL, json={"folder": str(a)}, headers=headers).json()     # as an older client sends one
        assert r["ok"] and r["folders"] == [str(a)] and r["scripts"] == 1 and r["folded"] == []
        r = client.post(URL, json={"folder": '"%s"' % b}, headers=headers).json()   # quoted, as Copy as path gives it
        assert r["folders"] == [str(a), str(b)] and r["folder"] == str(b)
        assert (r["scripts"], r["instruments"]) == (2, 2)                          # both folders read, never run
        assert runtime.scripts_folders() == [str(a), str(b)] and settings.lab_scripts == [str(a), str(b)]
        assert client.get(URL, headers=headers).json() == {"folders": [str(a), str(b)]}
        assert client.get("/api/settings", headers=headers).json()["settings"]["lab_scripts"] == [str(a), str(b)]

        r = client.post(URL, json={"folder": str(a / "sub")}, headers=headers).json()   # inside one: folded, said
        assert r["folders"] == [str(a), str(b)] and r["folded"] == ["%s covers %s" % (a, a / "sub")]
        r = client.post(URL, json={"folder": str(b)}, headers=headers).json()
        assert r["folders"] == [str(a), str(b)] and r["folded"] == ["%s is listed already" % b]
        for bad, why in ((str(tmp_path / "nope"), "does not exist"), ("scripts", "full path"),
                         (str(a / "iv.py"), "is not a folder")):
            r = client.post(URL, json={"folder": bad}, headers=headers)
            assert r.status_code == 400 and why in r.json()["error"], (bad, r.json())
        assert settings.lab_scripts == [str(a), str(b)]                            # a refusal changes nothing

        gone = str(a).upper() if os.name == "nt" else str(a)                      # Windows: any case names it
        assert client.post(URL, json={"remove": gone}, headers=headers).json()["folders"] == [str(b)]
        assert runtime.scripts_folders() == [str(b)]
        r = client.post(URL, json={"remove": str(a)}, headers=headers)
        assert r.status_code == 400 and "not one of the lab's scripts folders" in r.json()["error"]
        r = client.post(URL, json={"folders": [str(a), str(b), str(a / "sub")]}, headers=headers).json()
        assert r["folders"] == [str(a), str(b)] and r["folded"] == ["%s covers %s" % (a, a / "sub")]   # all at once
        assert client.post(URL, json={"folders": str(a)}, headers=headers).status_code == 400
        r = client.post(URL, json={"folders": [str(a), str(tmp_path / "gone")]}, headers=headers)
        assert r.status_code == 400 and "does not exist" in r.json()["error"]
        assert client.post(URL, json={"remove": str(b)}, headers=headers).json()["folders"] == [str(a)]
        assert client.post(URL, json={"remove": str(a)}, headers=headers).json()["folders"] == []   # the last one
        assert settings.lab_scripts == [] and runtime.scripts_folders() == []
        assert client.get("/api/state", headers=headers).json()["ask_scripts"] is False   # none, not unasked

        client.post(URL, json={"folder": str(a)}, headers=headers)
        view = client.post("/api/instruments/scripts", json={"folder": str(b)}, headers=headers).json()
        assert view["folders"] == [str(a), str(b)] and view["folder"] == "%s and %s" % (a, b)   # Set up: it joins,
        assert {i["address"] for i in view["instruments"]} == {"GPIB0::24::INSTR", "serial://COM7"}   # read with all
        assert settings.lab_scripts == [str(a), str(b)]
        assert client.post(URL, json={"folder": ""}, headers=headers).json()["folders"] == []   # "": there are none


# ------------------------------------------------------------------ the boundary: any folder, nothing outside them

def test_a_script_runs_from_any_of_the_folders_and_from_nowhere_else(tmp_path, scripts):
    other, outside = tmp_path / "shared", tmp_path / "elsewhere"
    other.mkdir()
    outside.mkdir()
    write(scripts, "sweep.py", SCRIPT)
    write(other, "probe.py", READS)
    write(other, "gate.py", SCRIPT)
    stray = write(outside, "stray.py", READS)
    folders = [str(scripts), str(other)]
    assert scriptrun.locate("probe.py", folders) == (other / "probe.py").resolve()   # by its name, in the second one
    assert scriptrun.locate(str(other / "probe.py"), folders) == (other / "probe.py").resolve()
    assert scriptrun.locate("sweep.py", folders) == (scripts / "sweep.py").resolve()
    if os.name == "nt":                                                   # Windows: the folder in another case
        assert scriptrun.locate(str(other / "probe.py").upper(), folders) == (other / "probe.py").resolve()
    for escape in (str(stray), str(other / ".." / "elsewhere" / "stray.py"), "../elsewhere/stray.py", "stray.py",
                   "..\\elsewhere\\stray.py"):
        with pytest.raises(scriptrun.ScriptError, match="outside the lab's scripts folders|no script"):
            scriptrun.locate(escape, folders)
    write(other, "sweep.py", SCRIPT)                                      # a name in both: said by full paths
    with pytest.raises(scriptrun.ScriptError, match="several sweep.py in the scripts folders") as e:
        scriptrun.locate("sweep.py", folders)
    assert str((scripts / "sweep.py").resolve()) in str(e.value) and str((other / "sweep.py").resolve()) in str(e.value)

    rt = script_runtime(tmp_path, scripts, ScriptedApprover([True]))
    rt.scripts_folders = lambda: folders                                  # as Settings would have them
    said = rt.run_script("probe.py")
    assert "script probe.py done" in said, said                           # run from the second folder
    assert "outside the lab's scripts folders" in rt.run_script(str(stray))
    assert rt.queue_add("script: gate.py").startswith("added")
    assert rt.queue_view()["items"][-1]["settable"] == {"VMAX": 0.4}     # its file found there, read for its block
    assert rt.queue_add("script: %s" % stray).startswith("not added: %s is outside the lab's scripts folders" % stray)


def test_a_link_out_of_a_folder_leads_nowhere(tmp_path):
    """A bare name was found through a junction out of the scripts folder (os.walk goes into junctions): the walk now
    keeps only what is inside the folders with its links followed, as a path through one is."""
    lab, outside = tmp_path / "lab", tmp_path / "outside"
    lab.mkdir()
    (outside / "sub").mkdir(parents=True)
    evil = write(outside / "sub", "evil.py", "print('outside')\n")
    folder_link(outside / "sub", lab / "linked")
    with pytest.raises(scriptrun.ScriptError, match="no script evil.py"):
        scriptrun.locate("evil.py", [str(lab)])                           # by its name, through the link
    with pytest.raises(scriptrun.ScriptError, match="no script"):
        scriptrun.locate("linked/evil.py", [str(lab)])
    with pytest.raises(scriptrun.ScriptError, match="outside the lab's scripts folder"):
        scriptrun.locate(str(lab / "linked" / "evil.py"), [str(lab)])
    # a link into another of the folders leads to that folder's script: found once, not "several"
    assert scriptrun.locate("evil.py", [str(lab), str(outside)]) == evil.resolve()


# ------------------------------------------------------------------ what reads or lists the lab's scripts

def test_the_survey_reads_every_folder_as_one(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    write(a, "iv.py", IV)
    write(b, "laser.py", LASER)
    write(b, "iv_again.py", IV)
    found = lab_scripts.survey([str(a), str(b), str(tmp_path / "gone")])
    assert found.folders == [str(a), str(b)] and found.folder == "%s and %s" % (a, b)
    assert sorted(s.path for s in found.scripts) == sorted([str(a / "iv.py"), str(b / "laser.py"),
                                                            str(b / "iv_again.py")])   # by folder: full paths
    assert {i.address for i in found.instruments} == {"GPIB0::24::INSTR", "serial://COM7"}   # one per address
    assert any("gone does not exist" in s for s in found.skipped)        # a folder not there now: said
    one = lab_scripts.survey(str(a))
    assert [s.path for s in one.scripts] == ["iv.py"] and one.folders == [str(a)]   # one folder: as before
    with pytest.raises(FileNotFoundError):
        lab_scripts.survey([str(tmp_path / "gone"), str(tmp_path / "gone too")])

    rt = script_runtime(tmp_path, a, ScriptedApprover([]))
    rt.scripts_folders = lambda: [str(a), str(b)]
    text = rt.survey_scripts()                                            # no path: every folder
    assert "Surveyed 3 scripts in %s and %s" % (a, b) in text and str(b / "laser.py") in text
    assert "Surveyed 1 script in %s" % a in rt.survey_scripts(str(a))
    rt.scripts_folders = lambda: []
    assert rt.survey_scripts("").startswith("not read: this lab has no scripts folders")


def test_the_new_function_picker_lists_every_folder(tmp_path, scripts, monkeypatch):
    other = tmp_path / "shared"
    (other / "sub").mkdir(parents=True)
    write(scripts, "sweep.py", SCRIPT)
    write(other / "sub", "probe.py", READS)
    write(other, "notes.txt", "x")
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([]))
    rt.scripts_folders = lambda: [str(scripts), str(other)]
    files = rt.library_files()
    assert files["folders"] == [str(scripts), str(other)] and files["folder"] == str(scripts) and not files["more"]
    assert [(f["folder"], f["rel"]) for f in files["files"]] == [(str(scripts), "sweep.py"),
                                                                (str(other), "sub/probe.py")]
    got = rt.library_inspect("sub/probe.py")                              # a path in a folder: the one that has it
    assert got["path"] == str(other / "sub" / "probe.py") and got["problem"] is None
    monkeypatch.setattr(scanlibrary, "MAX_FILES", 1)                      # at most MAX_FILES in all
    capped = rt.library_files()
    assert [f["rel"] for f in capped["files"]] == ["sweep.py"] and capped["more"] is True


def test_write_file_never_saves_in_any_of_the_folders(tmp_path, scripts):
    from labharness.agent.server import build_server
    from labharness.web.agent import ChatAgent
    from labharness.web.store import ChatStore
    other = tmp_path / "shared"
    other.mkdir()
    rt = script_runtime(tmp_path, scripts, ScriptedApprover([]))
    rt.scripts_folders = lambda: [str(scripts), str(other)]
    agent = ChatAgent(rt, ChatStore(tmp_path / "chats"), build_server(rt), {"scripted": Scripted([])})
    assert agent._kept() == [str(scripts), str(other)]
    for folder in (scripts, other):
        with pytest.raises(folderfiles.FolderError, match="in one of the lab's scripts folders"):
            folderfiles.save(folder, "new.py", "print('AI code')\n", kept=agent._kept(), **ai_saves(rt))
        assert not (folder / "new.py").exists()


# ------------------------------------------------------------------ the page

def test_the_page_has_a_row_per_folder_with_remove_and_add_folder():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    welcome = (STATIC / "welcome.js").read_text(encoding="utf-8")
    seq = (STATIC / "seq.js").read_text(encoding="utf-8")
    seq_css = (STATIC / "seq.css").read_text(encoding="utf-8")
    field = page[page.index("Your lab's scripts</span>"):page.index('id="scripts-python"')]
    assert '<button id="scripts-add" class="btn small" type="button">Add folder…</button>' in field
    assert '<ul id="scripts-folders" class="env-list"></ul>' in field and 'id="scripts-msg"' in field
    assert 'id="scripts-folder"' not in page and '$("scripts-folder")' not in js   # the one folder's line is gone
    rows = _fn(js, "renderScriptFolders")
    assert '"Remove"' in rows and "changeScriptFolders({ remove: f }, remove)" in rows and '"No folders yet."' in rows
    change = _fn(js, "changeScriptFolders")
    assert 'api("/api/instruments/scripts-folder", { body })' in change
    assert "S.settings.lab_scripts = r.folders" in change                 # Settings shows them as they are now
    assert "showScriptsMsg(e.message, true)" in change and 'classList.toggle("bad", !!bad)' in _fn(js, "showScriptsMsg")
    add = _fn(js, "addScriptFolder")
    assert "await pickFolder(" in add and "changeScriptFolders({ folder }" in add   # the picker, or a path typed
    assert '$("scripts-add").addEventListener("click", addScriptFolder);' in js
    assert "loadScriptFolders();" in _fn(js, "openSettings")
    assert "folderLabels," in js[js.index("window.Kelvin = {"):]          # one way to name them, on every page part
    assert "label = btn.textContent" in _fn(js, "surveyScripts")          # Set up's button keeps its own label

    block = _fn(welcome, "scriptsBlock")                                  # the first start: the same, as rows
    assert 'btn("Add folder…", "small", chooseScripts, { f: "scripts-pick" })' in block
    assert 'pathField(st, "scripts", useScripts, "Scripts", "Add")' in block and "...st.folders.map(folderRow)" in block
    assert 'btn("Remove", "small ghost", (b) => removeScripts(path, b)' in _fn(welcome, "folderRow")
    assert "{ body: { remove: path } }" in _fn(welcome, "removeScripts")
    assert "W.scripts.folders = [].concat(set.lab_scripts || []);" in welcome   # an older Kelvin's text too
    assert "f.labels.get(x.folder)" in _fn(welcome, "fileItem")
    assert "W.state.ask_scripts && !W.scripts.folders.length" in _fn(welcome, "finish")

    pick = seq[seq.index("function newFunction("):seq.index("function openSweep(")]   # the New function picker
    assert 'role: "group", "aria-label": d' in pick and '"seq-folder-head"' in pick
    assert "st.labels = st.folders.length > 1 ? K.folderLabels(st.folders) : null;" in pick
    assert ".seq-folder-head {" in seq_css
