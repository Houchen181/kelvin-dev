"""What uninstalling Kelvin removes, and what it keeps (Kelvin.exe --remove-data; the Windows uninstaller runs it).

Removed, Kelvin's own:  settings and API keys, the ChatGPT sign-in (revoked with OpenAI first), chats, projects and
                        memory, the lab config, your instrument cards, the instrument folders with their manuals,
                        the app's log, lock file and window cache.
Kept, your measurements: every run folder, the lab record of commands and approvals (events.jsonl) and reports, in
                        ~/Kelvin/virtual-lab and in any data folder chosen in Settings.

Only items Kelvin itself makes are deleted, by name; anything else in the folder is left alone."""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

HARNESS = ("settings.json", "chatgpt.json", "app.json", "kelvin.log", "lab.yaml", "lab.tmp", "settings.tmp",
           "webview", "instruments", "cards")
DATA_HARNESS = ("chats",)                       # in the default data folder: the conversations, projects and memory
MEASUREMENTS = ("runs", "reports", "events.jsonl")
NOTE = "About this folder.txt"


def _chosen_data_folder(home: Path) -> str:
    try:
        return str(json.loads((home / "settings.json").read_text(encoding="utf-8")).get("data_dir") or "")
    except (OSError, ValueError):
        return ""


def _revoke_chatgpt(home: Path, seconds: float = 10.0) -> str:
    """Sign out of ChatGPT properly (the refresh token is revoked with OpenAI), not just forget the tokens."""
    if not (home / "chatgpt.json").exists():
        return ""
    result: Dict[str, Any] = {}

    def go() -> None:
        try:
            from .web.chatgpt import SignIn
            result["ok"] = SignIn(home).sign_out()
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


def remove_data(home: Any, revoke: bool = True) -> Dict[str, List[str]]:
    """Delete Kelvin's own data in `home` (~/Kelvin); keep every measurement. Returns what was removed and kept."""
    home = Path(home)
    report: Dict[str, List[str]] = {"removed": [], "kept": [], "failed": [], "notes": []}
    if not home.is_dir():
        return report
    chosen = _chosen_data_folder(home)   # read before settings.json goes
    if revoke:
        note = _revoke_chatgpt(home)
        if note:
            report["notes"].append(note)
    data = home / "virtual-lab"
    for path in [home / n for n in HARNESS] + [data / n for n in DATA_HARNESS]:
        if not path.exists():
            continue
        try:
            _remove(path)
            report["removed"].append(str(path))
        except OSError as e:
            report["failed"].append("%s (%s)" % (path, e.strerror or e))
    report["kept"] = [str(data / n) for n in MEASUREMENTS if (data / n).exists()]
    if chosen and Path(chosen).exists():
        report["kept"].append(chosen)
    for folder in (data, home):   # nothing left: no empty folders behind
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
        if chosen:
            lines += ["", "Runs from after you chose a data folder in Settings are in %s." % chosen]
        lines += ["", "Kelvin's settings, API keys, chats, memory, lab config, instrument cards and instrument "
                  "folders were deleted.", "Installing Kelvin again shows these runs again."]
        try:
            (home / NOTE).write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass
    return report
