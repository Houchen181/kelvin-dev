"""The release and updater findings of the 0.3.52 audit: L2 (two tag builds uploading the signed files at once), L3 (the
updater ran an installer whoever signed it), L4 (GitHub's limit for anonymous callers read as a private repository)
and L5 (uninstalling left the team chat and Kelvin's other files behind, unnamed)."""
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
from pathlib import Path

import pytest
import yaml

from labharness import updater
from labharness.uninstall import NOTE, remove_data
from labharness.updater import PUBLISHER, Updater, UpdateError

ROOT = Path(__file__).resolve().parents[1]
SETUP = b"MZ pretend installer"
DIGEST = "sha256:" + hashlib.sha256(SETUP).hexdigest()
RELEASES = [{"tag_name": "v0.3.6", "prerelease": False, "draft": False, "html_url": "https://github.com/x/v0.3.6",
             "body": "notes", "assets": [{"name": "Kelvin-Setup-0.3.6.exe", "digest": DIGEST, "size": len(SETUP),
                                          "browser_download_url": "https://dl/Kelvin-Setup-0.3.6.exe"}]}]
FILES = {updater.api("stable"): json.dumps(RELEASES).encode(), "https://dl/Kelvin-Setup-0.3.6.exe": SETUP}


def fetch(url):
    if url not in FILES:
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    return FILES[url]


def signed_by(status, subject):
    return lambda path: (status, subject)


# ------------------------------------------------------------------ L2: one upload of the signed files at a time

def test_the_signing_store_is_written_by_one_build_at_a_time():
    """L2: the v0.3.52 and private-v0.3.52 builds both replaced signed-modules.zip at once, and one failed (404)."""
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["signing-store"]
    assert job["concurrency"]["group"] == "signing-store" and job["concurrency"]["cancel-in-progress"] is False


# ------------------------------------------------------------------ L3: only Kelvin's publisher's installer runs

def test_only_an_installer_signed_by_kelvins_publisher_is_kept_and_run(tmp_path):
    """L3: the updater ran a downloaded installer silently after checking only its SHA-256, which GitHub reports as
    it serves the file: whoever could publish a release could run code on every lab PC that updates by itself."""
    for status, subject, why in (("NotSigned", "", "not validly signed (NotSigned)"),
                                 ("HashMismatch", PUBLISHER, "not validly signed (HashMismatch)"),
                                 ("Valid", "CN=Someone Else, O=Someone Else, C=US", "not by Kelvin's publisher")):
        u = Updater(tmp_path / status, current="0.3.5", chan="stable", plat="win32", gh=[], fetch=fetch,
                    verify=signed_by(status, subject))
        assert u.check()["state"] == "available"
        with pytest.raises(UpdateError, match=re.escape(why)):
            u.download()
        assert not (tmp_path / status / "updates" / "Kelvin-Setup-0.3.6.exe").exists() and u.pending() is None
        with pytest.raises(UpdateError):
            u.install(1, relaunch=False, spawn=lambda c: None)

    def broken(path):
        raise FileNotFoundError("powershell")
    u = Updater(tmp_path / "unchecked", current="0.3.5", chan="stable", plat="win32", gh=[], fetch=fetch,
                verify=broken)
    u.check()
    with pytest.raises(UpdateError, match="could not be checked"):   # not checked is not signed
        u.download()

    seen = []
    u = Updater(tmp_path / "good", current="0.3.5", chan="stable", plat="win32", gh=[], fetch=fetch,
                verify=lambda path: seen.append(Path(path)) or ("Valid", PUBLISHER))
    assert u.run_once("auto")["state"] == "ready" and u.pending()["version"] == "0.3.6"
    started = []
    u.install(4321, relaunch=False, spawn=started.append)
    assert started and seen[-1] == tmp_path / "good" / "updates" / "Kelvin-Setup-0.3.6.exe"   # checked as it runs

    later = Updater(tmp_path / "good", current="0.3.5", chan="stable", plat="win32", gh=[], fetch=fetch,
                    verify=signed_by("NotTrusted", PUBLISHER))   # its certificate no longer trusted, say
    assert later.pending() is None
    with pytest.raises(UpdateError):
        later.install(4321, relaunch=False, spawn=started.append)
    assert len(started) == 1


def test_the_publisher_is_the_one_the_release_workflow_signs_as():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert '$publisher = "%s"' % PUBLISHER in workflow


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Authenticode is Windows'")
def test_windows_itself_judges_the_signature(tmp_path):
    """The check asks Windows (Get-AuthenticodeSignature), as the release workflow does: a file nobody signed is never
    Valid, so the updater with its own check refuses it."""
    fake = tmp_path / "Kelvin-Setup-0.3.6.exe"
    fake.write_bytes(SETUP)
    status, subject = updater.signature(fake)
    assert status != "Valid" and subject == ""
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "notepad.exe"
    if windows.exists():   # signed by Microsoft: Valid, and its signer read
        status, subject = updater.signature(windows)
        assert status == "Valid" and "Microsoft" in subject and subject != PUBLISHER
    u = Updater(tmp_path / "real", current="0.3.5", chan="stable", plat="win32", gh=[], fetch=fetch)
    u.check()
    with pytest.raises(UpdateError, match="not validly signed"):
        u.download()


# ------------------------------------------------------------------ L4: GitHub's limit is no private repository

def test_githubs_limit_for_anonymous_callers_is_said_as_it_is(tmp_path):
    """L4: past 60 anonymous requests an hour from one address (a campus network's, shared), GitHub answers 403, and
    Kelvin said "the Kelvin repository is private, and the GitHub CLI (gh) isn't installed here". Both are public."""
    def limited(url):
        raise urllib.error.HTTPError(url, 403, "rate limit exceeded", {"x-ratelimit-remaining": "0"},
                                     io.BytesIO(b"{}"))
    for chan in ("stable", "private"):
        state = Updater(tmp_path / chan, current="0.3.5", chan=chan, plat="win32", gh=[], fetch=limited).check()
        assert state["state"] == "error" and "private" not in state["error"], state["error"]
        assert "limits how often" in state["error"] and "again later" in state["error"]

    def secondary(url):   # GitHub's secondary limit: 403 with a Retry-After
        raise urllib.error.HTTPError(url, 403, "Forbidden", {"retry-after": "60"}, io.BytesIO(b"{}"))
    assert "limits how often" in Updater(tmp_path / "s", current="0.3.5", chan="stable", plat="win32", gh=[],
                                         fetch=secondary).check()["error"]
    gh = tmp_path / "gh.py"   # with the GitHub CLI signed in here, the look goes through it, past the limit
    gh.write_text("import sys\nprint(%r)\n" % json.dumps(RELEASES), encoding="utf-8")
    u = Updater(tmp_path / "gh", current="0.3.5", chan="stable", plat="win32", gh=[sys.executable, str(gh)],
                fetch=limited)
    assert u.check()["state"] == "available" and u.via == "gh"
    gone = Updater(tmp_path / "x", current="0.3.5", chan="private", plat="win32", gh=[], fetch=fetch)
    assert "GitHub CLI" in gone.check()["error"]   # a 404 (private, or no such repository) still says so


# ------------------------------------------------------------------ L5: uninstalling

def test_uninstalling_removes_the_team_chat_and_kelvins_other_files_and_names_what_it_keeps(tmp_path):
    """L5: after uninstalling, ~/Kelvin still had the team chat (people's messages), the collection's copy, downloaded
    updates, Claude Code's working folder and the chats of the lab folder, and the note named none of them."""
    home = tmp_path / "Kelvin"
    files = {"settings.json": "{}", "team-chat.jsonl": '{"name": "Ada", "text": "the fridge is mine today"}\n',
             "collection/cards/keithley/2400.yaml": "card: 1", "updates/Kelvin-Setup-0.3.53.exe": "MZ",
             "updates/pending.json": "{}", "claude-code/notes.txt": "x", "lab/chats/c1.json": "{}",
             "lab/runs/20261109-100000-sweep/data.jsonl": "{}", "lab/events.jsonl": "{}", "lab/reports/a.pdf": "%PDF",
             "lab/catalog.sqlite": "db", "lab/alarms.json": "[]", "lab/watches.json": "[]",
             "my notes.txt": "the person's own file"}
    for name, text in files.items():
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_text(text, encoding="utf-8")
    report = remove_data(home, revoke=False)
    for gone in ("team-chat.jsonl", "collection", "updates", "claude-code", "lab/chats", "settings.json"):
        assert not (home / gone).exists(), gone
    for kept in ("lab/runs/20261109-100000-sweep/data.jsonl", "lab/events.jsonl", "lab/reports/a.pdf",
                 "lab/catalog.sqlite", "lab/alarms.json", "lab/watches.json", "my notes.txt"):
        assert (home / kept).exists(), kept
    assert {str(home / "lab" / n) for n in ("runs", "events.jsonl", "reports")} <= set(report["kept"])
    note = (home / NOTE).read_text(encoding="utf-8")
    assert "while nobody was signed in are in lab" in note and "the team chat" in note
    assert "catalog of its runs, its alarms and watches" in note and "downloaded updates" in note
