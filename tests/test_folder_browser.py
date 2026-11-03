"""Kelvin's folder browser: in a browser on the Kelvin computer, which can't give a folder's path itself, choosing a
folder is browsing the computer's folders and choosing one (GET /api/browse lists them, names only); the Kelvin app
keeps the computer's own picker. Never for a phone, whatever its role."""
import json
import os
import re
import threading

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")

from starlette.testclient import TestClient  # noqa: E402

from labharness.web import browse  # noqa: E402
from labharness.web.app import STATIC, create_app  # noqa: E402
from labharness.web.remote import token_hash  # noqa: E402
from test_web import Scripted, client_for, parts  # noqa: E402,F401

JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
PAGE = (STATIC / "index.html").read_text(encoding="utf-8")


def _fn(src, name):
    """One function of a page script, by its name."""
    start = src.index("function %s(" % name)
    return src[start:src.index("\n  }\n", start)]


def lab_folder(root):
    """A lab's scripts folder as people have them: folders in any case, a folder named like a script, scripts and
    notebooks, other files, and what Explorer doesn't show."""
    lab = root / "lab"
    for name in ("beta", "Alpha", "gamma", "Delta", "pkg.py", ".git", "$Recycle.Bin", "System Volume Information"):
        (lab / name).mkdir(parents=True)
    for name in ("run.py", "fit.ipynb", "OLD.PY", "._run.py", "notes.txt"):
        (lab / name).write_text("x = 1\n", encoding="utf-8")
    if os.name == "nt":   # marked hidden, as Windows marks AppData
        import ctypes
        (lab / "tucked").mkdir()
        assert ctypes.windll.kernel32.SetFileAttributesW(str(lab / "tucked"), 2)
    return lab


# ------------------------------------------------------------------ the listing

def test_the_folders_in_a_folder_sorted_without_files_or_hidden_ones(parts, tmp_path, monkeypatch):
    home = tmp_path / "home"
    lab = lab_folder(home)
    monkeypatch.setattr(browse, "start", lambda: home)   # the person's home, as far as this test goes
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        r = client.get("/api/browse", params={"path": str(lab)}, headers=headers)
        assert r.status_code == 200
        seen = r.json()
        assert [f["name"] for f in seen["folders"]] == ["Alpha", "beta", "Delta", "gamma", "pkg.py"]   # whatever the case
        assert [f["path"] for f in seen["folders"]] == [str(lab / f["name"]) for f in seen["folders"]]
        assert "run.py" not in json.dumps(seen) and "notes.txt" not in json.dumps(seen)   # names of folders only
        assert seen["scripts"] == 3                       # run.py, fit.ipynb, OLD.PY: not ._run.py, nor the folder pkg.py
        assert seen["path"] == str(lab) and seen["parent"] == str(home) and seen["more"] == 0
        assert seen["note"] is None and seen["missing"] is None
        assert seen["parts"][-1] == {"name": "lab", "path": str(lab)}
        assert seen["parts"][0]["path"] == lab.anchor and [p["path"] for p in seen["parts"][-2:]] == [str(home), str(lab)]
        assert lab.anchor in seen["roots"] and (os.name == "nt" or seen["roots"] == ["/"])   # Windows: its drives
        assert seen["home"] == str(home)
        start = client.get("/api/browse", headers=headers).json()   # no path: the home folder
        assert start["path"] == str(home) and [f["name"] for f in start["folders"]] == ["lab"]
        top = client.get("/api/browse", params={"path": lab.anchor}, headers=headers).json()
        assert top["parent"] is None and top["path"] == lab.anchor   # a drive's top: nothing above
        monkeypatch.setattr(browse, "MOST", 3)
        cut = client.get("/api/browse", params={"path": '"%s"' % lab}, headers=headers).json()   # as Copy as path gives it
        assert [f["name"] for f in cut["folders"]] == ["Alpha", "beta", "Delta"] and cut["more"] == 2


def test_a_path_that_isnt_a_folder_answers_with_the_nearest_one_that_is(parts, tmp_path):
    lab = lab_folder(tmp_path)
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        gone = client.get("/api/browse", params={"path": str(lab / "new run" / "day 2")}, headers=headers).json()
        assert gone["path"] == str(lab) and gone["missing"] == str(lab / "new run" / "day 2")
        assert gone["note"] == "No folder at %s." % (lab / "new run" / "day 2")
        afile = client.get("/api/browse", params={"path": str(lab / "run.py")}, headers=headers).json()
        assert afile["path"] == str(lab) and afile["missing"] is None and afile["note"] == "%s is a file: this is its folder." % (lab / "run.py")
        tidied = client.get("/api/browse", params={"path": str(lab / "beta" / ".." / "gamma")}, headers=headers).json()
        assert tidied["path"] == str(lab / "gamma") and tidied["note"] is None   # made whole: no .. left in it
        bad = client.get("/api/browse", params={"path": "lab/beta"}, headers=headers)
        assert bad.status_code == 400 and bad.json() == {"error": "Give the folder's full path, such as D:\\Data or /Users/me/Data."}


def test_a_folder_that_cant_be_read_says_so_never_a_stack(tmp_path, monkeypatch):
    lab = lab_folder(tmp_path)
    locked = lab / "beta"
    real = os.scandir

    def scandir(path):
        if os.path.normcase(str(path)) == os.path.normcase(str(locked)):
            raise PermissionError(13, "Access is denied")
        return real(path)
    monkeypatch.setattr(browse.os, "scandir", scandir)
    shown = browse.listing(str(locked))
    assert shown["path"] == str(lab) and shown["missing"] is None
    assert shown["note"] == "%s can't be read: Access is denied." % locked

    def nothing(path):
        raise PermissionError(13, "Access is denied")
    monkeypatch.setattr(browse.os, "scandir", nothing)
    with pytest.raises(ValueError) as e:   # nothing on the way can be read: a sentence, which the route says (400)
        browse.listing(str(locked))
    assert str(e.value) == "%s can't be read: Access is denied." % locked.anchor


def test_a_network_folder_that_is_away_is_said_so_and_never_walked_up(tmp_path, monkeypatch):
    """Each folder above it on that share is as far away: trying them would cost the OS 20-40 s each."""
    asked, tried = tmp_path / "share" / "runs", []

    def scandir(path):
        tried.append(str(path))
        e = FileNotFoundError(2, "The network path was not found")   # "not found", as Python sees it
        e.winerror = 53                                              # ERROR_BAD_NETPATH: the server isn't there
        raise e
    monkeypatch.setattr(browse.os, "scandir", scandir)
    with pytest.raises(ValueError) as e:
        browse.listing(str(asked))
    assert str(e.value) == "%s can't be reached: The network path was not found." % asked and tried == [str(asked)]


def test_a_very_large_or_slow_folder_is_read_for_so_long_and_says_so(tmp_path, monkeypatch):
    big = tmp_path / "big"
    for i in range(300):
        (big / ("run %03d" % i)).mkdir(parents=True)
    whole = browse.listing(str(big))
    assert len(whole["folders"]) == 300 and whole["partial"] is False and whole["more"] == 0
    monkeypatch.setattr(browse, "READ_FOR", -1.0)   # read for long enough already: it stops at the next look
    part = browse.listing(str(big))
    assert part["partial"] is True and 0 < len(part["folders"]) < 300 and part["path"] == str(big)


def test_a_folder_that_doesnt_answer_is_said_so_without_holding_up_the_server(parts, tmp_path, monkeypatch):
    """A network folder that is away keeps the OS 20-40 s: the page has its answer after browse.WAIT seconds, the
    reading going on on a thread of its own, never one of the server's shared ones (the lab's state and a Stop's work
    wait for those); the same folder asked again meanwhile waits for that reading, never starts another."""
    gate, threads, real = threading.Event(), [], browse.listing

    def listing(asked=""):
        threads.append(threading.current_thread().name)
        gate.wait(10)
        return real(asked)
    monkeypatch.setattr(browse, "listing", listing)
    monkeypatch.setattr(browse, "WAIT", 0.2)
    client, headers = client_for(parts, {"scripted": Scripted([])})
    try:
        with client:
            for _ in range(2):
                r = client.get("/api/browse", params={"path": str(tmp_path)}, headers=headers)
                assert r.status_code == 504
                assert r.json() == {"error": "%s doesn't answer (a network folder that is away?)" % tmp_path}
            assert threads == ["kelvin-browse"]   # one reading, on its own thread
            gate.set()
            monkeypatch.setattr(browse, "WAIT", 10.0)
            r = client.get("/api/browse", params={"path": str(tmp_path)}, headers=headers)
            assert r.status_code == 200 and r.json()["path"] == str(tmp_path)
    finally:
        gate.set()


def test_a_phone_never_looks_through_the_computers_folders(parts, tmp_path):
    """Phones come in on the network listener, each with its role; an owner's role passes the roles' rules, so the route
    asks whether the request came from a phone at all."""
    runtime, settings, store = parts
    guard = create_app(runtime, settings, store, hosts=["testserver"], providers={"scripted": Scripted([])})
    owner = {"X-Kelvin-Token": settings.access_key}
    with TestClient(guard) as local:
        assert local.get("/api/browse", params={"path": str(tmp_path)}, headers=owner).status_code == 200
        guard.remote.port = 8771                       # as if the network listener were running
        phone = TestClient(guard, base_url="http://192.168.1.50:8771")
        token = "an-owners-phone-token-that-is-long-enough-0123456789"
        settings.add_device("Lab owner's phone", "owner", token_hash(token))
        r = phone.get("/api/browse", params={"path": str(tmp_path)}, headers={"X-Kelvin-Token": token})
        assert r.status_code == 403 and r.json() == {"error": "Only a browser on the Kelvin computer can look through its folders."}
        assert phone.get("/api/state", headers={"X-Kelvin-Token": token}).status_code == 200   # the phone itself works
        _, ctl = guard.remote.issue("Ana's phone", "control", "ios-ana")
        assert phone.get("/api/browse", params={"path": str(tmp_path)}, headers={"X-Kelvin-Token": ctl}).status_code == 403


# ------------------------------------------------------------------ the page

def test_choosing_a_folder_in_a_browser_is_browsing_and_choosing():
    pick = _fn(JS, "pickFolder")
    assert "if (hasPicker())" in pick and "window.pywebview.api.pick_folder(" in pick   # the Kelvin app: its own picker
    assert 'return browseFolder(start || "", question, !!opts.create);' in pick and "prompt(" not in pick
    b = _fn(JS, "browseFolder")
    assert 'api("/api/browse?path=" + encodeURIComponent(where), { signal })' in b
    assert 'String(question || "Choose a folder").trim().replace(/\\s*:$/, "")' in b   # the question as its title
    for text in ('"Choose this folder"', '"Cancel"', '"Up"', '"Home"', '"Use it anyway"', '"No folders here."',
                 'r.more + " more not shown"', '"More not shown (a large or slow folder)"', 'class: "chip blue"',
                 's + (r.partial ? "+ scripts" : s === 1 ? " script" : " scripts")',
                 '"aria-current": "location"', 'class: "kfield folder-path"', 'class: "dialog folder-dialog"'):
        assert text in b, text
    for key in ('"ArrowDown"', '"ArrowUp"', '"Home"', '"End"', '"Backspace"', '"Enter"'):
        assert "e.key === %s" % key in b, key
    assert 'dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(""); });' in b   # Escape: ""
    assert 'cancel.addEventListener("click", () => done(""));' in b and "if (B.at) done(B.at.path);" in b
    assert "dlg.showModal();\n      list.focus();" in b                            # the keys start in the folders
    assert "if (e.target === path) return;" in b                                   # a path typed keeps its Backspace
    assert "if (seq !== B.seq) return null;" in b                                  # a late answer never shows
    assert "requestAnimationFrame" not in b and "prompt(" not in b
    assert "    up: '<path" in JS                                                   # Up's arrow


def test_the_browser_asks_the_computer_once_for_what_the_person_does_once():
    """A network folder that is away answers in seconds, not milliseconds: what is asked meanwhile mustn't pile up, and
    what the person does meanwhile stays theirs."""
    b = _fn(JS, "browseFolder")
    # the folder still coming, asked for again, is the same answer; one no longer wanted is dropped, its request too
    assert "if (B.asked && B.asked.where === where) return B.asked.answer;" in b
    assert "if (B.asked) B.asked.stop.abort();" in b and "const done = (v) => { B.seq++; if (B.asked) B.asked.stop.abort();" in b
    # a key held down acts once: Backspace goes up a level, Enter opens one folder, not one in each
    assert 'if (e.repeat && (e.key === "Enter" || e.key === "Backspace")) e.preventDefault();' in b
    assert "if (!e.repeat && unquote(path.value)) go(" in b
    # a double-click, as in Explorer, goes in once
    assert "onclick: (e) => { if (e.detail < 2) go(f.path); }" in b
    # a late listing leaves the path being typed alone, and Choose then goes there
    assert "const typing = B.edited && !how.typed && document.activeElement === path;" in b and "B.edited = true;" in b
    # a folder that isn't there: Choose waits for another path; Use it anyway only where the folder can be made
    assert "choose.disabled = !typing && !!r.missing;" in b and "...(r.missing && create" in b
    # the start can't be read: the home folder instead, unless the person went elsewhere meanwhile
    assert "const opened = go(start), first = B.seq;" in b
    assert "if (r || B.at || !dlg.open || B.seq !== first || !start) return;" in b


def test_every_folder_chosen_goes_through_pick_folder():
    """Each place a person chooses a folder: in the Kelvin app the computer's picker, in a browser Kelvin's, New chat's
    question too. A new folder is offered only where Kelvin makes it: a chat's folder and the data folder."""
    assert "window.prompt" not in JS
    for name in ("changeFolder", "askFolder", "fromLabview", "fromSpecialMeasure"):
        assert "await pickFolder(" in _fn(JS, name), name
    for name in ("changeFolder", "askFolder"):
        assert 'pickFolder(FOLDER.path, "Choose where this chat saves its runs", { create: true })' in _fn(JS, name), name
    assert "hasPicker" not in _fn(JS, "askFolder")                    # a browser asks as the app does
    assert "data-choose\").hidden" not in JS and re.search(r'<button id="data-choose" class="btn small" type="button">', PAGE)
    assert 'pickFolder($("data-path").value.trim(), "Choose the data folder", { create: true })' in JS
    assert 'pickFolder(store("lh.labviewFolder") || "", "Choose the LabVIEW folder")' in _fn(JS, "fromLabview")
    assert 'pickFolder(store("lh.smFolder") || "", "Choose the Special Measure folder")' in _fn(JS, "fromSpecialMeasure")
    assert '<label class="check wrap"><input id="ask-folder" name="ask_folder" type="checkbox" checked>' in PAGE   # not the app's alone
    assert "pickFolder, hasPicker," in JS[JS.index("window.Kelvin = {"):]   # the welcome page's too (welcome.js)
    for name in ("home.js", "seq.js", "welcome.js"):
        assert "prompt(" not in (STATIC / name).read_text(encoding="utf-8"), name


def test_the_browser_wears_the_kit_in_light_and_dark():
    part = CSS[CSS.index("/* Kelvin's folder browser"):CSS.index("/* motion, as labMaster moves")]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", part)   # colours through the tokens only
    for text in (".folder-dialog { width: min(600px, calc(100 * var(--vw) - 32px)); }", "var(--card)", "var(--accent-soft)",
                 ".folder-item:focus-visible", ".folder-note:empty { display: none; }"):
        assert text in part, text
    motion = CSS[CSS.index("/* motion, as labMaster moves"):]
    assert "dialog[open] { animation: k-pop-in" in motion and "dialog[open]" in motion[motion.index("prefers-reduced-motion"):]
    # its classes are its own: the Lab dialog's instrument folders are a .folder-list, and keep their look
    mine = set(re.findall(r"\.(folder-[\w-]+)", re.sub(r"/\*.*?\*/", "", part, flags=re.S)))
    rest, b = CSS.replace(part, ""), _fn(JS, "browseFolder")
    assert "folder-items" in mine and "folder-list" not in mine and 'class: "folder-items"' in b
    for name in mine:
        word = r"(?<![\w-])%s(?![\w-])" % re.escape(name)
        assert not re.search(word, rest) and not re.search(word, PAGE) and not re.search(word, JS.replace(b, "")), name


def test_kelvin_help_knows_how_a_folder_is_chosen_in_a_browser():
    from labharness import guide
    found = guide.search("choose a folder in a browser")
    assert "**Choose this folder**" in found and "**Use it anyway**" in found
