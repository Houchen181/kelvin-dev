"""Kelvin keeps itself up to date, with only ever one Kelvin on a computer."""
import hashlib
import json
import sys
import urllib.error
from pathlib import Path

import pytest

from labharness import updater
from labharness.updater import Updater, UpdateError, pick

SETUP = b"MZ pretend installer"
DIGEST = "sha256:" + hashlib.sha256(SETUP).hexdigest()


def release(tag, assets, prerelease=False, draft=False):
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft, "html_url": "https://github.com/x/" + tag,
            "body": "notes for " + tag,
            "assets": [{"name": n, "digest": DIGEST, "size": len(SETUP), "browser_download_url": "https://dl/" + n}
                       for n in assets]}


RELEASES = [
    release("v0.3.6", ["Kelvin-Setup-0.3.6.exe", "Kelvin-Setup.exe", "Kelvin-0.3.6-macOS.zip"]),
    release("v0.3.5", ["Kelvin-Setup-0.3.5.exe"]),
    release("private-v0.3.7", ["Kelvin-Setup-0.3.7-private.exe"], prerelease=True),
    release("v0.4.0", ["Kelvin-Setup-0.4.0.exe"], prerelease=True),        # a pre-release: not for everyone
    release("v0.5.0", ["Kelvin-Setup-0.5.0.exe"], draft=True),
    release("private-claude-code-20261019", ["Kelvin-private-claude-code-Windows.zip"], prerelease=True),
]


def test_each_build_follows_its_own_channel():
    assert pick(RELEASES, "stable", "0.3.5", "win32")["version"] == "0.3.6"
    assert pick(RELEASES, "stable", "0.3.6", "win32") is None              # nothing newer
    assert pick(RELEASES, "private", "0.3.5", "win32")["version"] == "0.3.7"
    assert pick(RELEASES, "private", "0.3.5", "win32")["asset"]["name"] == "Kelvin-Setup-0.3.7-private.exe"
    mac = pick(RELEASES, "stable", "0.3.5", "darwin")
    assert mac["version"] == "0.3.6" and mac["asset"]["name"] == "Kelvin-0.3.6-macOS.zip"
    assert pick(RELEASES, "private", "0.3.5", "darwin") is None
    assert updater.version("Kelvin-Setup-0.3.10.exe") > updater.version("0.3.9")


def fetcher(files):
    def fetch(url):
        if url not in files:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return files[url]
    return fetch


def test_download_check_and_install(tmp_path):
    files = {updater.API: json.dumps(RELEASES).encode(), "https://dl/Kelvin-Setup-0.3.6.exe": SETUP}
    u = Updater(tmp_path, current="0.3.5", chan="stable", plat="win32", gh=[], fetch=fetcher(files))
    state = u.run_once("ask")
    assert state["state"] == "available" and state["latest"] == "0.3.6" and u.pending() is None   # asks first
    state = u.run_once("auto")
    assert state["state"] == "ready" and u.pending()["version"] == "0.3.6"
    started = []
    cmd = u.install(4321, relaunch=True, spawn=started.append)
    script = cmd[-1]
    assert started == [cmd] and cmd[0] == "powershell"
    assert "Wait-Process -Id 4321" in script and "/VERYSILENT" in script and "/RELAUNCH=1" in script
    assert str(tmp_path / "updates" / "Kelvin-Setup-0.3.6.exe") in script
    assert u.pending()["tried"]                                            # never retried by itself in a loop
    later = Updater(tmp_path, current="0.3.6", chan="stable", plat="win32", gh=[], fetch=fetcher(files))
    later.clean()                                                          # after the update: the installer goes
    assert not (tmp_path / "updates" / "Kelvin-Setup-0.3.6.exe").exists() and later.pending() is None


def test_a_download_that_does_not_match_is_thrown_away(tmp_path):
    files = {updater.API: json.dumps(RELEASES).encode(), "https://dl/Kelvin-Setup-0.3.6.exe": b"something else"}
    u = Updater(tmp_path, current="0.3.5", chan="stable", plat="win32", gh=[], fetch=fetcher(files))
    u.check()
    with pytest.raises(UpdateError, match="checksum"):
        u.download()
    assert not (tmp_path / "updates" / "Kelvin-Setup-0.3.6.exe").exists() and u.pending() is None
    with pytest.raises(UpdateError):
        u.install(1, relaunch=False, spawn=lambda c: None)                 # nothing checked to install
    no_digest = [dict(RELEASES[0], assets=[dict(RELEASES[0]["assets"][0], digest=None)])]
    u = Updater(tmp_path, current="0.3.5", chan="stable", plat="win32", gh=[],
                fetch=fetcher({updater.API: json.dumps(no_digest).encode()}))
    u.check()
    with pytest.raises(UpdateError, match="no checksum"):
        u.download()


FAKE_GH = '''
import json, sys, pathlib
args = sys.argv[1:]
if args[0] == "api":
    print(%r)
elif args[:2] == ["release", "download"]:
    folder = pathlib.Path(args[args.index("--dir") + 1])
    (folder / args[args.index("--pattern") + 1]).write_bytes(%r)
'''


def test_a_private_repository_goes_through_the_github_cli(tmp_path):
    gh = tmp_path / "gh.py"
    gh.write_text(FAKE_GH % (json.dumps(RELEASES), SETUP), encoding="utf-8")
    u = Updater(tmp_path, current="0.3.5", chan="private", plat="win32", gh=[sys.executable, str(gh)],
                fetch=fetcher({}))                                         # the web API says 404: private
    assert u.run_once("auto")["state"] == "ready" and u.via == "gh"
    assert u.pending()["version"] == "0.3.7"
    none = Updater(tmp_path / "x", current="0.3.5", chan="stable", plat="win32", gh=[], fetch=fetcher({}))
    state = none.check()
    assert state["state"] == "error" and "GitHub CLI" in state["error"]


def test_the_portal_installs_only_when_nothing_runs(tmp_path):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    from starlette.testclient import TestClient
    from labharness import build_virtual_lab, demo_envelope
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore

    files = {updater.API: json.dumps(RELEASES).encode(), "https://dl/Kelvin-Setup-0.3.6.exe": SETUP}
    u = Updater(tmp_path, current="0.3.5", chan="stable", plat="win32", gh=[], fetch=fetcher(files))
    started, closed = [], []
    real_install = u.install
    u.install = lambda pid, relaunch: real_install(pid, relaunch, spawn=started.append)
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    settings = Settings(tmp_path / "settings.json")
    client = TestClient(create_app(runtime, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"],
                                   providers={}, updater=u, on_exit=lambda: closed.append(1)))
    h = {"X-Kelvin-Token": settings.access_key}
    assert client.get("/api/bootstrap", headers=h).json()["update"]["current"] == "0.3.5"
    settings.update({"updates": "ask"})
    assert settings.public()["updates"] == "ask" and "updates" in json.loads((tmp_path / "settings.json").read_text())
    assert client.post("/api/update", headers=h, json={"op": "check"}).json()["state"] == "available"
    assert client.post("/api/update", headers=h, json={"op": "download"}).json()["state"] == "ready"
    runtime.queue.state = "running"
    r = client.post("/api/update", headers=h, json={"op": "install"})
    assert r.status_code == 409 and "queue" in r.json()["error"] and started == []
    runtime.queue.state = "idle"
    runtime.operating = "a task"
    assert client.post("/api/update", headers=h, json={"op": "install"}).status_code == 423   # Claude can't click it
    runtime.operating = None
    assert client.post("/api/update", headers=h, json={"op": "install"}).json()["state"] == "installing"
    assert len(started) == 1 and "/RELAUNCH=1" in started[0][-1]


def test_from_source_there_is_nothing_to_update(monkeypatch):
    assert updater.view(None, "auto")["state"] == "unsupported"
    assert updater.channel() == "stable" and updater.other_copy() is None   # not frozen
