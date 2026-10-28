"""Kelvin keeps itself up to date, and there is only ever one Kelvin on a computer.

- Where: GitHub releases. Published builds come from the public Kelvin repository (releases and the instrument
  collection, no source); private builds from the source repository, kelvin-dev, public too: no sign-in is needed.
  If GitHub refuses an anonymous look, Kelvin asks through the GitHub CLI (`gh`) signed in on this computer;
  Kelvin never sees its token.
- Which: published builds follow published releases (vX.Y.Z); the private build follows private-vX.Y.Z
  pre-releases (see claudecode.py). Only a newer version is taken.
- Checked: the download's SHA-256 must match the digest GitHub reports for that file, or it is thrown away.
- Installed: on Windows by the installer, silently, into the same folder with the same app id, so it replaces the
  copy that is there and never adds a second one. It runs after Kelvin closes: when the person quits (automatic
  updates), with Restart to update, or at the next start if Kelvin was not closed properly. Never while a plan,
  the queue, a chat or Claude's computer work is running (the portal checks).
- macOS: Kelvin says that a new version is out and links to it.

Settings (the computer's): updates = auto (download, install on close; the default), ask (say it is there, install
on request) or off."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ._version import __version__

log = logging.getLogger("kelvin")
REPOS = {"stable": "Houchen181/Kelvin", "private": "Houchen181/kelvin-dev"}   # published releases; the source's
TAGS = {"stable": re.compile(r"^v(\d+\.\d+\.\d+)$"), "private": re.compile(r"^private-v(\d+\.\d+\.\d+)$")}
ASSETS = {("stable", "win32"): r"^Kelvin-Setup-(\d+\.\d+\.\d+)\.exe$",
          ("private", "win32"): r"^Kelvin-Setup-(\d+\.\d+\.\d+)-private\.exe$",
          ("stable", "darwin"): r"^Kelvin-(\d+\.\d+\.\d+)-macOS\.zip$"}
INSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{8C0F6E5A-3B7D-4C1E-9A2F-6D5E4B3A2C10}_is1"


class UpdateError(Exception):
    pass


def api(chan: str) -> str:
    """Where a channel's releases are listed."""
    return "https://api.github.com/repos/%s/releases?per_page=30" % REPOS.get(chan, REPOS["stable"])


def version(text: str) -> Tuple[int, ...]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", str(text or ""))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def channel() -> str:
    try:
        from . import _private_bridge  # noqa: F401  (only in the private build)
        return "private"
    except ImportError:
        return "stable"


def platform_key() -> str:
    return "win32" if sys.platform.startswith("win") else sys.platform


def installed_location() -> Optional[Path]:
    """Where the installer put Kelvin on this Windows account (None elsewhere, or if it was never installed)."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INSTALL_KEY) as k:
            return Path(winreg.QueryValueEx(k, "InstallLocation")[0])
    except OSError:
        return None


def other_copy() -> Optional[str]:
    """If this Kelvin runs from somewhere else than the installed one (an unzipped copy, say), where that one is."""
    if not getattr(sys, "frozen", False):
        return None
    where = installed_location()
    here = Path(sys.executable).resolve().parent
    if where is None or not (where / "Kelvin.exe").exists():
        return None
    try:
        return None if where.resolve() == here else str(where)
    except OSError:
        return None


def pick(releases: List[Dict[str, Any]], chan: str, current: str, plat: str) -> Optional[Dict[str, Any]]:
    """The newest release on this channel that is newer than this Kelvin and has a file for this platform."""
    tag_re = TAGS[chan]
    asset_re = re.compile(ASSETS.get((chan, plat), r"^$"))
    best: Optional[Dict[str, Any]] = None
    for r in releases:
        m = tag_re.match(str(r.get("tag_name") or ""))
        if not m or r.get("draft") or (chan == "stable" and r.get("prerelease")):
            continue
        if version(m.group(1)) <= version(current):
            continue
        asset = next((a for a in r.get("assets") or [] if asset_re.match(str(a.get("name") or ""))), None)
        if asset is None and (plat == "win32" or chan == "private"):   # macOS: say it is out, link to it
            continue
        if best is None or version(m.group(1)) > version(best["version"]):
            best = {"version": m.group(1), "tag": r["tag_name"], "asset": asset, "url": r.get("html_url") or "",
                    "notes": str(r.get("body") or "")[:4000]}
    return best


def _flags() -> int:
    if not sys.platform.startswith("win"):
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Updater:
    def __init__(self, home: Any, current: str = __version__, chan: Optional[str] = None,
                 plat: Optional[str] = None, gh: Optional[List[str]] = None,
                 fetch: Optional[Callable[[str], bytes]] = None):
        self.dir = Path(home) / "updates"
        self.current = current
        self.channel = chan or channel()
        self.repo = REPOS.get(self.channel, REPOS["stable"])
        self.platform = plat or platform_key()
        found = shutil.which("gh")
        self.gh: List[str] = gh if gh is not None else ([found] if found else [])
        self._fetch = fetch or self._urlopen
        self.lock = threading.Lock()
        self.state: Dict[str, Any] = {"state": "idle", "current": current, "channel": self.channel}
        self.found: Optional[Dict[str, Any]] = None
        self.via = "web"

    # ------------------------------------------------------------------ asking GitHub

    @staticmethod
    def _urlopen(url: str) -> bytes:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "Kelvin/" + __version__})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()

    def _gh(self, args: List[str], timeout: float = 600) -> str:
        if not self.gh:
            raise UpdateError("the Kelvin repository is private, and the GitHub CLI (gh) isn't installed here to "
                              "reach it")
        # gh writes UTF-8; read it as UTF-8 whatever the computer's locale (GBK, cp1252...), or release notes
        # with characters like "→" stop the check
        p = subprocess.run(self.gh + args, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=_flags())
        if p.returncode != 0:
            raise UpdateError("the GitHub CLI could not reach the releases (is it signed in?): %s"
                              % (p.stderr.strip().splitlines() or ["no message"])[-1][:200])
        return p.stdout

    def releases(self) -> List[Dict[str, Any]]:
        url = api(self.channel)
        try:
            data = json.loads(self._fetch(url))
            self.via = "web"
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403, 404):
                raise UpdateError("GitHub answered %s" % e.code)
            data = json.loads(self._gh(["api", url.split("api.github.com/", 1)[1]], timeout=60))   # private repo
            self.via = "gh"
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise UpdateError("could not reach GitHub: %s" % getattr(e, "reason", e))
        if not isinstance(data, list):
            raise UpdateError("GitHub gave an unexpected answer")
        return data

    # ------------------------------------------------------------------ the steps

    def _set(self, **state: Any) -> None:
        self.state = dict({"current": self.current, "channel": self.channel}, **state)

    def check(self) -> Dict[str, Any]:
        self._set(state="checking")
        try:
            found = pick(self.releases(), self.channel, self.current, self.platform)
        except Exception as e:   # whatever goes wrong, the check reports it and the next one runs
            self._set(state="error", error=str(e) or type(e).__name__, checked=time.time())
            log.warning("update check failed: %s", self.state["error"])
            return self.state
        self.found = found
        if found is None:
            self._set(state="up_to_date", checked=time.time())
            log.info("update check: Kelvin %s is the newest on the %s channel (via %s)", self.current, self.channel,
                     self.via)
        else:
            log.info("update check: Kelvin %s is out (via %s)", found["version"], self.via)
            ready = self.pending()
            self._set(state="ready" if ready and ready["version"] == found["version"] else "available",
                      latest=found["version"], url=found["url"], notes=found["notes"], checked=time.time(),
                      can_install=self.platform == "win32" and found["asset"] is not None)
        return self.state

    def download(self) -> Dict[str, Any]:
        """Fetch the found installer and keep it only if its SHA-256 is the one GitHub reports."""
        found = self.found
        if not found or not found.get("asset"):
            raise UpdateError("no update to download")
        asset = found["asset"]
        digest = str(asset.get("digest") or "")
        if not digest.startswith("sha256:"):
            raise UpdateError("GitHub gives no checksum for %s, so Kelvin won't install it" % asset["name"])
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / asset["name"]
        self._set(state="downloading", latest=found["version"], url=found["url"], notes=found["notes"])
        try:
            if self.via == "gh":
                self._gh(["release", "download", found["tag"], "--repo", self.repo, "--pattern", asset["name"],
                          "--dir", str(self.dir), "--clobber"])
            else:
                path.write_bytes(self._fetch(asset["browser_download_url"]))
            got = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception as e:
            self._set(state="error", error="the download failed: %s" % e, latest=found["version"], url=found["url"])
            log.warning(self.state["error"])
            raise UpdateError(self.state["error"])
        if got != digest.split(":", 1)[1].lower():
            path.unlink()
            self._set(state="error", error="the download did not match GitHub's checksum and was deleted",
                      latest=found["version"], url=found["url"])
            raise UpdateError(self.state["error"])
        (self.dir / "pending.json").write_text(json.dumps({"version": found["version"], "path": str(path),
                                                           "sha256": got, "tag": found["tag"]}), encoding="utf-8")
        self._set(state="ready", latest=found["version"], url=found["url"], notes=found["notes"],
                  can_install=self.platform == "win32")
        log.info("update: Kelvin %s downloaded and checked; it installs when Kelvin closes", found["version"])
        return self.state

    def pending(self) -> Optional[Dict[str, Any]]:
        """A downloaded, checked installer for a newer version, if there is one (checked again: it is run next)."""
        try:
            p = json.loads((self.dir / "pending.json").read_text(encoding="utf-8"))
            path = Path(p["path"])
            if version(p["version"]) <= version(self.current) or not path.is_file():
                return None
            if hashlib.sha256(path.read_bytes()).hexdigest() != p["sha256"]:
                return None
            return p
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def mark_tried(self, p: Dict[str, Any]) -> None:
        p = dict(p, tried=time.time())
        (self.dir / "pending.json").write_text(json.dumps(p), encoding="utf-8")

    def clean(self) -> None:
        """After an update: the installers of this or older versions go."""
        try:
            for f in self.dir.glob("Kelvin-Setup-*.exe"):
                if version(f.name) <= version(self.current):
                    f.unlink()
            p = json.loads((self.dir / "pending.json").read_text(encoding="utf-8"))
            if version(p.get("version")) <= version(self.current):
                (self.dir / "pending.json").unlink()
        except (OSError, ValueError):
            pass

    def run_once(self, mode: str) -> Dict[str, Any]:
        """One round: look, and in automatic mode also download what was found."""
        with self.lock:
            state = self.check()
            if mode == "auto" and state["state"] == "available" and state.get("can_install"):
                try:
                    self.download()
                except UpdateError:
                    pass
            return self.state

    def install(self, wait_pid: int, relaunch: bool, spawn: Optional[Callable[[List[str]], Any]] = None) -> List[str]:
        """Run the checked installer once Kelvin (wait_pid) has closed: silently, into the installed folder, then
        start Kelvin again if asked. Returns the command it started."""
        p = self.pending()
        if p is None or self.platform != "win32":
            raise UpdateError("no checked update is ready to install")
        self.mark_tried(p)
        setup = str(p["path"]).replace("'", "''")
        script = ("Wait-Process -Id %d -Timeout 900 -ErrorAction SilentlyContinue; "
                  "Start-Process -FilePath '%s' -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART'%s"
                  % (int(wait_pid), setup, ",'/RELAUNCH=1'" if relaunch else ""))
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script]
        if spawn is not None:
            spawn(cmd)
        else:
            # CREATE_NEW_PROCESS_GROUP and a hidden console of its own, so it outlives Kelvin. Not DETACHED_PROCESS:
            # a PowerShell without a console exits as if all went well but never starts the installer (0.3.8, 0.3.9)
            flags = 0x00000200 | _flags()
            subprocess.Popen(cmd, creationflags=flags, close_fds=True)
        self._set(state="installing", latest=p["version"])
        return cmd


def view(updater: Optional[Updater], mode: str) -> Dict[str, Any]:
    if updater is None:
        return {"state": "unsupported", "current": __version__, "mode": mode,
                "why": "updates come from git when Kelvin runs from source"}
    return dict(updater.state, mode=mode, other_copy=other_copy())
