"""Uninstalling Kelvin deletes Kelvin's own data and keeps every measurement."""
import json
from pathlib import Path

import pytest

from labharness import uninstall
from labharness.uninstall import NOTE, remove_data


def make_home(tmp_path, chosen=None):
    home = tmp_path / "Kelvin"
    files = {
        "settings.json": json.dumps({"anthropic_api_key": "sk-test", **({"data_dir": str(chosen)} if chosen else {})}),
        "chatgpt.json": "{}", "app.json": "{}", "kelvin.log": "log", "lab.yaml": "lab: x",
        "webview/Default/Cache/data": "x", "instruments/smu/manual.pdf": "%PDF-", "instruments/smu/settings.json": "{}",
        "cards/acme.yaml": "card: 1", "virtual-lab/chats/abc123.json": "{}", "virtual-lab/chats/_projects.json": "{}",
        "virtual-lab/runs/20261018-120000-sweep/data.jsonl": "{}", "virtual-lab/events.jsonl": "{}",
        "virtual-lab/reports/week.pdf": "%PDF-", "my notes.txt": "the user's own file",
    }
    for name, text in files.items():
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_text(text, encoding="utf-8")
    return home


def test_kelvins_own_data_go_and_measurements_stay(tmp_path):
    chosen = tmp_path / "lab server" / "data"
    (chosen / "runs" / "20261019-090000-map").mkdir(parents=True)
    home = make_home(tmp_path, chosen)
    report = remove_data(home, revoke=False)

    for gone in ("settings.json", "chatgpt.json", "app.json", "kelvin.log", "lab.yaml", "webview", "instruments",
                 "cards", "virtual-lab/chats"):
        assert not (home / gone).exists(), gone
    for kept in ("virtual-lab/runs/20261018-120000-sweep/data.jsonl", "virtual-lab/events.jsonl",
                 "virtual-lab/reports/week.pdf", "my notes.txt"):          # and a file Kelvin did not make
        assert (home / kept).exists(), kept
    assert (chosen / "runs" / "20261019-090000-map").exists()                 # the chosen data folder untouched
    assert str(chosen) in report["kept"] and report["failed"] == []
    assert len(report["removed"]) == 9
    note = (home / NOTE).read_text(encoding="utf-8")
    assert "virtual-lab/runs" in note and str(chosen) in note and "were deleted" in note


def test_nothing_left_means_no_folder_left(tmp_path):
    home = tmp_path / "Kelvin"
    (home / "virtual-lab" / "chats").mkdir(parents=True)
    (home / "settings.json").write_text("{}", encoding="utf-8")
    remove_data(home, revoke=False)
    assert not home.exists()
    assert remove_data(tmp_path / "never-there") == {"removed": [], "kept": [], "failed": [], "notes": []}


def test_the_chatgpt_sign_in_is_revoked_first(tmp_path, monkeypatch):
    chatgpt = pytest.importorskip("labharness.web.chatgpt")
    seen = []

    class SignIn:
        def __init__(self, home):
            seen.append(Path(home))

        def sign_out(self):
            return True
    monkeypatch.setattr(chatgpt, "SignIn", SignIn)
    home = make_home(tmp_path)
    report = remove_data(home)
    assert seen == [home] and report["notes"] == ["ChatGPT sign-in revoked"]


def test_the_app_removes_its_data_only_when_it_is_not_running(tmp_path, monkeypatch, capsys):
    from labharness import desktop
    home = make_home(tmp_path)
    monkeypatch.setattr(desktop, "_running_copy", lambda lock: 8771)
    assert desktop.main(["--remove-data", "--home", str(home)]) == 1
    assert (home / "settings.json").exists() and "quit it first" in capsys.readouterr().out
    monkeypatch.setattr(desktop, "_running_copy", lambda lock: None)
    monkeypatch.setattr(uninstall, "_revoke_chatgpt", lambda home, seconds=10.0: "")
    assert desktop.main(["--remove-data", "--home", str(home)]) == 0
    assert not (home / "settings.json").exists() and (home / "virtual-lab" / "runs").exists()
    assert not (home / "kelvin.log").exists()                                 # not written again by this run


def test_the_windows_uninstaller_runs_it():
    from labharness import desktop
    iss = (Path(__file__).parent.parent / "packaging" / "kelvin.iss").read_text(encoding="utf-8")
    assert "[UninstallRun]" in iss and 'Parameters: "--remove-data"' in iss
    assert "AppMutex=%s" % desktop.APP_MUTEX in iss                           # waits while Kelvin runs
    assert "measurement data are kept" in iss
