"""Text size (issue #10): each person's own zoom of the desktop window, kept with their settings, and set on the
window's own thread (the window's built-in Ctrl + and Ctrl - are off in pywebview)."""
import sys
import types

from labharness.web.settings import LAB, Settings


def test_the_text_size_is_each_persons_and_snaps_to_the_steps(tmp_path):
    s = Settings(tmp_path / "settings.json")
    assert "text_size" not in LAB and s.public()["text_size"] == 1.0
    s.update({"text_size": "1.3"})   # between two steps: the nearest
    assert s.public()["text_size"] == 1.25
    s.update({"text_size": "9"})
    assert s.public()["text_size"] == 2.0
    s.update({"text_size": "big"})
    assert s.public()["text_size"] == 1.0
    s.update({"text_size": "1.5"})
    s.update({"text_size": "1"})     # 100%: nothing kept
    assert s.public()["text_size"] == 1.0 and "text_size" not in (tmp_path / "settings.json").read_text(encoding="utf-8")


def test_the_window_zoom_is_set_on_the_windows_own_thread(monkeypatch):
    from labharness.desktop import _zoom
    # pythonnet stand-ins: the real one would start .NET in this process (and the .NET library tests need it not to)
    system = types.ModuleType("System")
    system.Type = object
    system.Func = {object: lambda fn: types.SimpleNamespace(Invoke=fn)}
    monkeypatch.setitem(sys.modules, "clr", types.ModuleType("clr"))
    monkeypatch.setitem(sys.modules, "System", system)
    monkeypatch.setattr(sys, "platform", "win32")
    threads = []

    class View:
        ZoomFactor = 1.0

    class Form:
        browser = type("Browser", (), {"webview": View()})()

        def Invoke(self, delegate):
            threads.append("window")
            return delegate.Invoke()

    window = type("Window", (), {"native": Form()})()
    assert _zoom(window, 1.25) is True
    assert window.native.browser.webview.ZoomFactor == 1.25 and threads == ["window"]
    assert _zoom(type("Window", (), {"native": None})(), 1.5) is False   # no web view: nothing to zoom
