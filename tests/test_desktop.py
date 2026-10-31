"""The desktop launcher, without a window (the release builds run the same self-test on the packaged app)."""
import re
import sys
import types

import pytest

pytest.importorskip("mcp")
pytest.importorskip("uvicorn")

from labharness import desktop  # noqa: E402
from labharness.desktop import main  # noqa: E402


def test_self_test_starts_the_portal_and_cleans_up(tmp_path):
    assert main(["--self-test", "--home", str(tmp_path)]) == 0
    assert (tmp_path / "settings.json").exists()        # access key created on first start
    assert not (tmp_path / "app.json").exists()         # the single-instance lock is removed on exit
    assert "self-test: ok" in (tmp_path / "kelvin.log").read_text(encoding="utf-8")


def test_import_a_script_opens_the_computers_own_file_picker(tmp_path, monkeypatch):
    """pick_file, for the Runs pane's Import a script…: an Open dialog for Python scripts and notebooks, at the folder
    given (or the folder of a file given); the path chosen, or "" when the person closes it."""
    asked, exposed = [], {}

    class Closing:
        def __iadd__(self, fn):
            return self

    class Window:
        events = types.SimpleNamespace(closing=Closing())
        answer = None

        def create_file_dialog(self, kind, directory="", file_types=()):
            asked.append((kind, directory, tuple(file_types)))
            return self.answer

        def expose(self, *fns):
            exposed.update({f.__name__: f for f in fns})

    window = Window()
    fake = types.SimpleNamespace(FileDialog=types.SimpleNamespace(OPEN=10, FOLDER=20), settings={},
                                 create_window=lambda *a, **k: window, start=lambda **k: None)
    monkeypatch.setitem(sys.modules, "webview", fake)   # no window: pywebview's part is played here
    desktop._window("http://127.0.0.1:1/", None, None, tmp_path)
    assert {"pick_folder", "pick_file", "set_zoom"} <= set(exposed)
    pick = exposed["pick_file"]
    script = tmp_path / "sweep.py"
    script.write_text("V_MAX = 3\n", encoding="utf-8")
    window.answer = (str(script),)
    assert pick(str(tmp_path)) == str(script)
    kind, folder, types_ = asked[-1]
    assert kind == 10 and folder == str(tmp_path) and types_ == ("Python scripts and notebooks (*.py;*.ipynb)",)
    # the form pywebview accepts for a filter (webview.util.parse_file_type)
    assert re.match(r"^([\w ]+)\((\*(?:\.(?:\w+|\*))*(?:;\*(?:\.(?:\w+|\*))*)*)\)$", types_[0])
    assert pick(str(script)) == str(script) and asked[-1][1] == str(tmp_path)    # a file: its folder
    window.answer = None
    assert pick(str(tmp_path / "no" / "such.py")) == "" and asked[-1][1] == ""   # closed; no such folder: none
    assert pick("") == "" and asked[-1][1] == ""
