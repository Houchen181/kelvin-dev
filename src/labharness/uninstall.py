"""What uninstalling Kelvin removes, and what it keeps (Kelvin.exe --remove-data; the Windows uninstaller runs it).

Removed, Kelvin's own:  settings and API keys, the ChatGPT sign-in (revoked with OpenAI first), chats, projects and
                        memory, the accounts, the lab config, your instrument cards, the instrument folders with their
                        manuals, the app's log, lock file and window cache. With accounts, each person's too.
Kept, your measurements: every run folder, the record of commands and approvals (events.jsonl) and reports, in
                        ~/Kelvin/virtual-lab, in each person's ~/Kelvin/users/<name>/data and in any data folder
                        chosen in Settings; the lab's instrument log (instrument-log.jsonl, and lab/events.jsonl).

Only items Kelvin itself makes are deleted, by name; anything else in the folder is left alone."""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

HARNESS = ("settings.json", "chatgpt.json", "app.json", "kelvin.log", "lab.yaml", "lab.tmp", "settings.tmp",
           "webview", "instruments", "cards", "accounts.json", "accounts.tmp")
DATA_HARNESS = ("chats",)                       # in the default data folder: the conversations, projects and memory
PERSON = ("settings.json", "settings.tmp", "chatgpt.json", "chatgpt.tmp", "chats")   # in each ~/Kelvin/users/<name>
MEASUREMENTS = ("runs", "reports", "events.jsonl")
NOTE = "About this folder.txt"


def _chosen_data_folder(home: Path) -> str:
    try:
        return str(json.loads((home / "settings.json").read_text(encoding="utf-8")).get("data_dir") or "")
    except (OSError, ValueError):
        return ""


def _people(home: Path) -> List[Path]:
    users = home / "users"
    return sorted(p for p in users.iterdir() if p.is_dir()) if users.is_dir() else []


def _revoke_chatgpt(home: Path, seconds: float = 10.0, cipher: Any = None) -> str:
    """Sign out of ChatGPT properly (the refresh token is revoked with OpenAI), not just forget the tokens. cipher:
    the person's, when their sign-in is sealed with their password."""
    if not (home / "chatgpt.json").exists():
        return ""
    result: Dict[str, Any] = {}

    def go() -> None:
        try:
            from .web.chatgpt import SignIn
            signin = SignIn(home)
            if cipher is not None:
                signin.use(home, cipher)
            result["ok"] = signin.sign_out()
        except Exception as e:   # no network, or no web extra: the tokens are still deleted below
            result["error"] = str(e)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(seconds)
    return "ChatGPT sign-in revoked" if result.get("ok") else "ChatGPT sign-in deleted (OpenAI did not confirm the revocation)"


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def remove_person(folder: Any, chats: Any, cipher: Any = None, revoke: bool = True) -> Dict[str, List[str]]:
    """One person taken off this computer, as uninstalling would: their own Kelvin data in their folder (PERSON:
    settings and API keys, the ChatGPT sign-in, revoked first, chats, projects and memory) and their chats wherever
    they are (an account that took over what Kelvin kept before accounts has them in virtual-lab). Their measurements
    stay. Returns what was removed and kept."""
    folder, chats = Path(folder), Path(chats)
    report: Dict[str, List[str]] = {"removed": [], "kept": [], "failed": [], "notes": []}
    chosen = _chosen_data_folder(folder)   # a data folder they chose in Settings, read before their settings go
    if revoke:
        note = _revoke_chatgpt(folder, cipher=cipher)
        if note:
            report["notes"].append(note)
    for path in [folder / n for n in PERSON] + ([chats] if chats != folder / "chats" else []):
        if not path.exists():
            continue
        try:
            _remove(path)
            report["removed"].append(str(path))
        except OSError as e:
            report["failed"].append("%s (%s)" % (path, e.strerror or e))
    data = chats.parent if chats != folder / "chats" else folder / "data"
    report["kept"] = [str(data / n) for n in MEASUREMENTS if (data / n).exists()]
    report["kept"] += [chosen] if chosen and Path(chosen).exists() else []
    try:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError:
        pass
    return report


def remove_data(home: Any, revoke: bool = True) -> Dict[str, List[str]]:
    """Delete Kelvin's own data in `home` (~/Kelvin); keep every measurement. Returns what was removed and kept."""
    home = Path(home)
    report: Dict[str, List[str]] = {"removed": [], "kept": [], "failed": [], "notes": []}
    if not home.is_dir():
        return report
    people = _people(home)
    chosen = [c for c in [_chosen_data_folder(home)] + [_chosen_data_folder(p) for p in people] if c]   # before they go
    if revoke:
        for folder in [home] + people:
            note = _revoke_chatgpt(folder)
            if note:
                report["notes"].append(note + ("" if folder == home else " (%s)" % folder.name))
    data = home / "virtual-lab"
    for path in [home / n for n in HARNESS] + [data / n for n in DATA_HARNESS] + [p / n for p in people for n in PERSON]:
        if not path.exists():
            continue
        try:
            _remove(path)
            report["removed"].append(str(path))
        except OSError as e:
            report["failed"].append("%s (%s)" % (path, e.strerror or e))
    report["kept"] = [str(d / n) for d in [data] + [p / "data" for p in people] for n in MEASUREMENTS if (d / n).exists()]
    report["kept"] += [str(p) for p in (home / "instrument-log.jsonl", home / "lab" / "events.jsonl") if p.exists()]
    report["kept"] += [c for c in chosen if Path(c).exists()]
    for folder in [data, home / "lab"] + people + [home / "users", home]:   # nothing left: no empty folders behind
        try:
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        except OSError:
            pass
    if home.is_dir():   # measurements stay: say what they are
        lines = ["Kelvin was uninstalled on %s." % time.strftime("%Y-%m-%d %H:%M"), "",
                 "Your measurement data are kept:",
                 "  virtual-lab/runs          one folder per run: the plan, the data, every instrument setting at",
                 "                            the start and end, and the automatic analysis",
                 "  virtual-lab/events.jsonl  the lab record: every command, reading, approval and alarm",
                 "  virtual-lab/reports       PDF and PowerPoint reports"]
        if people:
            lines += ["", "Each person's runs, record and reports are in users/<name>/data, the same way."]
        if (home / "instrument-log.jsonl").exists():
            lines += ["", "instrument-log.jsonl is the lab's log: every command from everyone, with their names."]
        for c in chosen:
            lines += ["", "Runs from after a data folder was chosen in Settings are in %s." % c]
        lines += ["", "Kelvin's settings, API keys, accounts, chats, memory, lab config, instrument cards and "
                  "instrument folders were deleted.", "Installing Kelvin again shows these runs again."]
        try:
            (home / NOTE).write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass
    return report
