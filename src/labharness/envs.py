"""Instrument environments: the Python packages and makers' files an instrument needs, installed on this computer
when Kelvin sets the instrument up. The app brings none of them: every lab has its own instruments.

    ~/Kelvin/env/<name>/             a Python environment of its own (uv venv), of the version and bitness the
                                     instrument's library needs (a 32-bit Python for a 32-bit DLL)
    ~/Kelvin/env/<name>/kelvin.json  what is in it, for which instruments, who approved it
    ~/Kelvin/env/<name>/files/       a maker's own files (its Python package from its download page, its DLL)
    ~/Kelvin/tools/uv/               uv, Astral's installer, fetched once from its GitHub release (SHA-256 checked)
    ~/Kelvin/tools/python/           the Pythons uv fetched for the environments (python-build-standalone)

The environment "drivers" has the app's own Python version: Kelvin's built-in vendor drivers (QICK, Zurich
Instruments, Quantum Design, NI DAQ, cameras) import their makers' packages from it, inside the app (attach). Any
other environment runs a maker's library in Kelvin's helper process (library.py: lib://...?env=<name>).

Kelvin installs only after a person approves (agent/runtime.py install_for_instrument): what, which version, from
where. Nothing is installed system-wide and no administrator rights are needed."""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

DRIVERS = "drivers"   # the environment Kelvin's built-in vendor drivers import from, inside the app
UV_RELEASE = "https://github.com/astral-sh/uv/releases/latest/download/"
NAME = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")
# A package from PyPI as pip and uv take it (PEP 508 without a URL or a marker): its name, extras if wanted, and the
# versions it may have, PEP 440's clauses separated by commas, PEP 508's plain spaces between the parts allowed: qick,
# qick==0.2.436, numpy>=2.1,<2.3, qick[full]~=0.2, pkg===1.0+local, pyvisa (>=1.13, <2). A field report's
# numpy>=2.1,<2.3 was refused, the pattern taking one clause (2026-11-15). Only these characters, so nothing in it is an
# option, a URL or a path to uv (requirement() says which a refused one is)
_PKG_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_CLAUSE = r"(?:===|==|!=|~=|>=|<=|<|>) *[A-Za-z0-9._*+!-]+"
_CLAUSES = r"%s(?: *, *%s)*" % (_CLAUSE, _CLAUSE)
# what pip and uv take for a file here, by its suffix (pkg-1.0-py3-none-any.whl, pkg-1.0.tar.zst, x.tbz): a wheel or
# an archive's, at the end or before another suffix or the versions
ARCHIVES = ("whl", "zip", "egg", "tar", "gz", "tgz", "bz2", "tbz", "tbz2", "xz", "txz", "lz", "tlz", "lzma", "tlzma",
            "zst", "tzst", "7z", "rar")
_A_FILE = r"\.(?i:%s)(?=\Z|[.\[<>=!~ (,;@])" % "|".join(ARCHIVES)
MOST = 200   # characters in one package to install, at most: none is longer, and nothing longer is read further
# No two runs of spaces side by side, which the expression would try every split of: quadratic, with re holding the
# GIL, so a package of 50,000 spaces froze the whole of Kelvin (its monitor, Stop, the portal) for minutes (review of
# 2026-11-15); and nothing longer than MOST is matched (not_a_package)
PACKAGE = re.compile(r"^(?!.*?%s)(%s)(?: *\[ *(?:(%s(?: *, *%s)*) *)?\])?(?: *(\( *%s *\)|%s))?\Z"
                     % (_A_FILE, _PKG_NAME, _PKG_NAME, _PKG_NAME, _CLAUSES, _CLAUSES))
WIN = sys.platform.startswith("win")
HOME: Dict[str, Path] = {"path": Path.home() / "Kelvin"}
Run = Callable[[List[str], Dict[str, str]], Tuple[int, str]]


def set_home(path: Any) -> None:
    HOME["path"] = Path(path)


def root(home: Any = None) -> Path:
    return Path(home) if home else HOME["path"]


def env_dir(name: str, home: Any = None) -> Path:
    if not NAME.match(name or ""):
        raise ValueError("%r is not an environment name (lower-case letters, digits, - and _)" % name)
    return root(home) / "env" / name


def python_of(name: str, home: Any = None) -> Path:
    """An environment's Python, which starts its base Python by that version's own folder (pin_python), so Windows'
    RedirectionGuard doesn't stop it at uv's link."""
    pin_python(name, home)
    return env_dir(name, home) / ("Scripts/python.exe" if WIN else "bin/python")


def site_packages(name: str, home: Any = None) -> Optional[Path]:
    d = env_dir(name, home)
    if WIN:
        return d / "Lib" / "site-packages" if (d / "Lib" / "site-packages").is_dir() else None
    found = sorted(d.glob("lib/python*/site-packages"))
    return found[0] if found else None


def app_python() -> Tuple[str, int]:
    """This Kelvin's own Python: (version, bits)."""
    return "%d.%d" % sys.version_info[:2], struct.calcsize("P") * 8


def base_name(spec: str) -> str:
    return re.split(r"[\[<>=!~\s(;@,]", spec.strip(), maxsplit=1)[0]


def not_a_package(text: str) -> str:
    """Why a package to install isn't one Kelvin takes from PyPI, in words; "" when it is (requirement). Its length
    first: nothing longer than MOST characters is read further (a card's requires, install_for_instrument's packages)."""
    if not text:
        return "an empty package name"
    if len(text) > MOST:
        return ("%r... is %d characters long: no package's name and versions are longer than %d"
                % (text[:40], len(text), MOST))
    if re.search(r"[^\x20-\x7e]", text):   # \t \n \r, a no-break or zero-width space, a look-alike letter
        return ("%r holds a tab, a line break or a character that only looks like a letter or a space: write it "
                "plainly" % text)
    if ";" in text:
        return "%r has something after the package, past its ';' (a marker, or a command): name it alone" % text
    if re.search(r"(^|[\s,])-", text):
        return "%r is an option for the installer (it starts with -), not a package" % text
    path = "%r is a path or a file: a maker's package on this computer goes in files" % text
    if re.match(r"^[A-Za-z]:[\\/]", text):   # C:\..., C:/...
        return path
    if "://" in text or "@" in text or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", text):
        return ("%r is a URL (or name @ URL): Kelvin installs a package from PyPI by its name; a maker's download "
                "goes in files" % text)
    if re.search(r"[\\/]", text) or text.startswith((".", "~")) or re.search(_A_FILE, text):
        return path
    if not PACKAGE.match(text):
        return "%r is not a package name with the versions it may have" % text
    return ""


def requirement(spec: Any) -> str:
    """A package to install from PyPI, written plainly: its name, extras in [ ] and the versions it may have, as PEP 440
    writes them, clauses separated by commas, without the spaces PEP 508 allows between them: "numpy >= 2.1, < 2.3"
    is numpy>=2.1,<2.3. That is what the approval shows and uv gets, one argument of its list. Raises ValueError, saying
    why, for anything uv would take for more than a package from PyPI: an option (-r, --index-url), a URL or
    name @ URL, a path or a file, a marker or a command after ';', a tab, a line break or a character that only looks
    like another."""
    text = spec.strip() if isinstance(spec, str) else ""
    why = not_a_package(text)
    if why:
        raise ValueError(why)
    m = PACKAGE.match(text)
    extras = [e.strip() for e in (m.group(2) or "").split(",") if e.strip()]
    return m.group(1) + ("[%s]" % ",".join(extras) if extras else "") + re.sub(r"[ ()]", "", m.group(3) or "")


def info(name: str, home: Any = None) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads((env_dir(name, home) / "kelvin.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def listing(home: Any = None) -> List[Dict[str, Any]]:
    out = []
    folder = root(home) / "env"
    for d in sorted(folder.iterdir()) if folder.is_dir() else []:
        rec = info(d.name, home) if NAME.match(d.name) else None
        if rec is not None:
            out.append(rec)
    return out


def _save(rec: Dict[str, Any], home: Any = None) -> None:
    path = env_dir(rec["name"], home) / "kelvin.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------ the Pythons uv fetched; RedirectionGuard

# uv keeps each Python it fetches in a folder of its own version (cpython-3.13.9-windows-x86_64-none) and links the
# minor version's folder to the newest (cpython-3.13-windows-x86_64-none, a junction on Windows), through which it makes
# and finds environments. Windows refuses a process with RedirectionGuard on every junction a non-administrator made
# (os error 448), and uv stopped there: no environment could be made, nor a package installed (field report
# 2026-11-17). So Kelvin names a version's own folder, never its link.
_PATCH_DIR = re.compile(r"^cpython-(\d+\.\d+)\.(\d+)-(?:windows|linux|macos|darwin)-([a-z0-9_]+)-(?:none|gnu|musl)$")
GUARD_448 = ("os error 448", "untrusted mount point")   # how uv says Windows refused it a link


def redirection_guard() -> bool:
    """Whether Windows' RedirectionGuard (redirection trust) is on in this process. Windows then refuses it, and every
    program it starts, which inherit it, any junction a non-administrator made: "The path cannot be traversed because
    it contains an untrusted mount point" (os error 448). Inno Setup 6.7 turns it on for itself, and a per-user
    installer passes it to the program it starts: a Kelvin started by its installer, or started again after an update,
    had it, and so had uv (field report 2026-11-17; the installer now starts Kelvin through the desktop, which never
    has it). False where Windows has no such thing (before Windows 10 22H2), and on other systems."""
    if not WIN:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetProcessMitigationPolicy.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
        k32.GetProcessMitigationPolicy.restype = wintypes.BOOL
        flags = wintypes.DWORD()
        if not k32.GetProcessMitigationPolicy(k32.GetCurrentProcess(), 16, ctypes.byref(flags), 4):   # ProcessRedir-
            return False                                                                                # ectionTrust
        return bool(flags.value & 1)   # EnforceRedirectionTrust
    except Exception:
        return False


def _arches(bits: int) -> Tuple[str, ...]:
    """How uv names this computer's processor in a Python's folder, for a Python of these bits."""
    if bits == 32:
        return ("x86", "i686")
    return ("aarch64", "arm64") if platform.machine().lower() in ("arm64", "aarch64") else ("x86_64", "amd64")


def managed_python(python: str, bits: int = 64, home: Any = None) -> Optional[Path]:
    """The newest patch of a Python version uv fetched for Kelvin (~/Kelvin/tools/python), in that patch's own folder,
    never through the minor version's link; None when uv has none of that version and bits. Free-threaded, debug and
    pre-release builds are passed over."""
    best: Optional[Tuple[int, Path]] = None
    try:
        entries = list(os.scandir(root(home) / "tools" / "python"))
    except OSError:
        return None
    for entry in entries:
        m = _PATCH_DIR.match(entry.name)
        if not m or m.group(1) != python or m.group(3) not in _arches(bits):
            continue
        try:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        exe = Path(entry.path) / ("python.exe" if WIN else "bin/python3")
        if exe.is_file() and (best is None or int(m.group(2)) > best[0]):
            best = (int(m.group(2)), exe)
    return best[1] if best else None


def _link_target(path: Path) -> Optional[Path]:
    """Where a link leads (a symbolic link, or a junction on Windows), read from the link itself, never through it,
    which RedirectionGuard refuses; None for a plain folder, or nothing there."""
    try:
        target = os.readlink(str(path))
    except (OSError, ValueError, NotImplementedError, AttributeError):
        return None
    if target.startswith("\\\\?\\") and not target.upper().startswith("\\\\?\\UNC\\"):
        target = target[4:]
    t = Path(target)
    return t if t.is_absolute() else path.parent / t


def _same_folder(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def pin_python(name: str, home: Any = None) -> Optional[str]:
    """An environment's base Python named by its version's own folder where uv named it through the minor version's
    link (pyvenv.cfg: home = ...\\tools\\python\\cpython-3.13-windows-x86_64-none). The environment's python.exe starts
    the Python in home, through that link, which RedirectionGuard refuses: uv's query of the environment then failed,
    and so would a library's helper in it (field report 2026-11-17). Done whenever Kelvin uses an environment (it reads
    one small file); uv's way of moving an environment to a newer patch through the link goes with it, and Kelvin
    upgrades no Python. Returns the folder it names now, or None when nothing changed."""
    try:
        d = env_dir(name, home)
    except ValueError:
        return None
    cfg = d / "pyvenv.cfg"
    try:
        text = cfg.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    m = re.search(r"(?m)^home\s*=\s*(.*?)\s*$", text)
    if not m or not m.group(1):
        return None
    base = Path(m.group(1))
    pythons = root(home) / "tools" / "python"
    # uv's link is home itself (Windows: python.exe at the top of its folder) or the folder above (bin, elsewhere)
    for link in (base, base.parent):
        if not _same_folder(link.parent, pythons) or _PATCH_DIR.match(link.name):
            continue
        target = _link_target(link)
        if target is None:
            rec = info(name, home) or {}
            exe = managed_python(str(rec.get("python") or ""), int(rec.get("bits") or 64), home)
            if exe is None or not re.match(r"^cpython-\d+\.\d+-", link.name):
                return None
            target = exe.parent if WIN else exe.parent.parent
        if not (target / ("python.exe" if WIN else "bin/python3")).is_file():   # a Python's own folder, or nothing
            return None
        old, new = str(link), str(target)
        lines = []
        for line in text.splitlines(True):   # home, and any other line that names the link (executable, command)
            key, sep, value = line.partition("=")
            v = value.strip()
            if sep and os.path.normcase(v).startswith(os.path.normcase(old)):
                line = "%s= %s%s\n" % (key.rstrip() + " ", new, v[len(old):])
            lines.append(line)
        tmp = cfg.with_suffix(".tmp")
        try:
            tmp.write_text("".join(lines), encoding="utf-8")
            os.replace(tmp, cfg)
        except OSError:
            return None
        return str(Path(new) / base.relative_to(link)) if base != link else new
    return None


# ------------------------------------------------------------------ uv

def uv_asset() -> str:
    """The name of uv's release file for this computer."""
    arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    if WIN:
        return "uv-%s-pc-windows-msvc.zip" % arch
    if sys.platform == "darwin":
        return "uv-%s-apple-darwin.tar.gz" % arch
    return "uv-%s-unknown-linux-gnu.tar.gz" % arch


def uv_path(home: Any = None) -> Optional[Path]:
    """Kelvin's own uv, else one on PATH. On Windows only a uv.exe: a uv.cmd or uv.bat (a shim) runs through cmd.exe,
    which reads a version range's < and > as redirections, where a program started from a list of arguments takes
    them as they are."""
    exe = "uv.exe" if WIN else "uv"
    mine = root(home) / "tools" / "uv" / exe
    if mine.is_file():
        return mine
    found = shutil.which("uv")
    if found and WIN and not found.lower().endswith(".exe"):
        return None
    return Path(found) if found else None


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Kelvin (lab assistant)"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read(200 * 1024 * 1024)


def fetch_uv(home: Any = None, get: Optional[Callable[[str], bytes]] = None) -> Path:
    """uv from its latest GitHub release, used only if it matches the SHA-256 published beside it."""
    get = get or _get
    asset = uv_asset()
    data = get(UV_RELEASE + asset)
    published = get(UV_RELEASE + asset + ".sha256").decode("ascii", "replace").split()
    if not published or hashlib.sha256(data).hexdigest() != published[0].lower():
        raise RuntimeError("the download of uv does not match its published SHA-256, so Kelvin did not use it")
    exe = "uv.exe" if WIN else "uv"
    folder = root(home) / "tools" / "uv"
    folder.mkdir(parents=True, exist_ok=True)
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            member = next(n for n in z.namelist() if n.rsplit("/", 1)[-1] == exe)
            body = z.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            member = next(m for m in t.getmembers() if m.isfile() and m.name.rsplit("/", 1)[-1] == exe)
            body = t.extractfile(member).read()
    (folder / exe).write_bytes(body)
    if not WIN:
        os.chmod(folder / exe, 0o755)
    return folder / exe


SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION", re.I)
SECRET_PREFIXES = ("OPENAI", "ANTHROPIC", "GEMINI", "CLAUDE", "AWS_", "AZURE_", "GH_", "GITHUB_", "HF_", "HUGGINGFACE")


def clean_environ(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The environment for a maker's code (its library in the helper, its package's build): this computer's, without
    API keys, tokens, passwords or sign-ins a program could read and send elsewhere."""
    env = dict(os.environ if base is None else base)
    return {k: v for k, v in env.items() if not SECRET_NAME.search(k) and not k.upper().startswith(SECRET_PREFIXES)}


def uv_env(home: Any = None) -> Dict[str, str]:
    """uv keeps everything in Kelvin's folder and uses only the Pythons it fetched (never one a program left). It
    writes each sentence of an error on one line (UV_NO_WRAP), as _said() reads them."""
    tools = root(home) / "tools"
    env = {k: v for k, v in clean_environ().items()
           if k not in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "CONDA_PREFIX")}
    env.update({"UV_PYTHON_INSTALL_DIR": str(tools / "python"), "UV_CACHE_DIR": str(tools / "uv-cache"),
                "UV_MANAGED_PYTHON": "1", "UV_NO_PROGRESS": "1", "UV_NO_CONFIG": "1", "UV_LINK_MODE": "copy",
                "UV_NO_WRAP": "1"})
    return env


def _kill_tree(proc: Any) -> None:
    """A program ended with every process it started: Windows' taskkill /T over its tree, elsewhere its process group
    (run_bounded starts it in one of its own)."""
    if os.name == "nt":
        if proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, subprocess.SubprocessError):
                pass
    else:
        try:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def run_bounded(cmd: List[str], timeout: float, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None,
                merge: bool = False) -> Tuple[int, bytes, bytes]:
    """cmd run with a time limit that holds: (its exit code, what it wrote, what it wrote to stderr; with merge, both
    as the first). Its output goes into files, not pipes: subprocess.run's limit killed the program alone and then
    waited for its pipes with no end, while a child it started kept them open (a .bat around Python ran 20 s past a
    2 s limit; uv's build backends). Past the limit the whole tree is ended (_kill_tree) and TimeoutExpired raised.
    Starting it, too, is waited for the limit at most (a working folder on a share that stalled)."""
    import tempfile
    import threading
    box: Dict[str, Any] = {}
    done = threading.Event()

    def run() -> None:
        try:
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                kw: Dict[str, Any] = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" \
                    else {"start_new_session": True}
                proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                        stderr=subprocess.STDOUT if merge else err, **kw)
                box["proc"] = proc
                try:
                    proc.wait(timeout)
                except subprocess.TimeoutExpired:
                    _kill_tree(proc)
                    box["error"] = subprocess.TimeoutExpired(cmd, timeout)
                    return
                out.seek(0)
                err.seek(0)
                box["value"] = (proc.returncode, out.read(), b"" if merge else err.read())
        except BaseException as e:   # the caller's to raise
            box["error"] = e
        finally:
            done.set()
    threading.Thread(target=run, name="kelvin-bounded-run", daemon=True).start()
    if not done.wait(timeout + 15):   # it never even started (or its tree's end hung): left, said as a time out
        proc = box.get("proc")
        if proc is not None:
            _kill_tree(proc)
        raise subprocess.TimeoutExpired(cmd, timeout)
    if "error" in box:
        raise box["error"]
    return box["value"]


def _run(cmd: List[str], env: Dict[str, str]) -> Tuple[int, str]:
    code, out, _ = run_bounded(cmd, 1800, env=env, merge=True)   # uv: bounded with its build backends (run_bounded)
    return code, out.decode("utf-8", "replace")


# uv's lines on its progress and on the Python it works with, and its [stderr] labels: none says what went wrong
_UV_PROGRESS = re.compile(r"^(Using \S+ \S+ (environment|interpreter) at|Using CPython \S+$|"
                          r"Creating virtual environment|Resolved \d|"
                          r"Prepared \d|Installed \d|Uninstalled \d|Audited \d|Checked \d|Bytecode compiled|"
                          r"Downloading |Downloaded |Building |Built |[-+~] \S|\[std(out|err)\]$)")
# how uv marks an error and its causes: "error:" and "cause:", or (an older uv) "×", "├─▶" and "╰─▶"
_UV_MARK = re.compile(r"^(error:|cause:|×|[├╰]─▶|│)\s*")
# uv's own words on what went wrong: its error, each cause it gives (cause:, Caused by:, or an older uv's ×, ├─▶ and
# ╰─▶) and its hints; anything else after its error is a failed build's own output
_UV_SAYS = re.compile(r"^(error:|cause:|caused by:|warning:|hint:|×|[├╰]─▶)", re.I)
_UV_BOX = re.compile(r"^(×|[├╰]─▶|│)\s*")
SAID = 6000   # characters of what uv said, at most: its whole error and causes; a build's own output only its end
BUILD_LINES = 8   # the last lines of a failed build's own output kept, where its error is


def _cut(text: str, most: int) -> str:
    """text, or its first `most` characters cut at a word and marked as cut."""
    if len(text) <= most:
        return text
    cut = text[:most]
    space = cut.rfind(" ", most - 200)
    return (cut[:space] if space > 0 else cut).rstrip(" |,;") + " …"


def _said(text: str) -> str:
    """What uv says went wrong, on one line and whole: its error with every cause it gives, where a field report's
    message, cut at 400 characters, lost the end of the one that said why ("... untrusted mount point. (os erro",
    2026-11-17). Versions that can't go together: its whole reason, which names them ("Because you require
    numpy==2.2.6 and numpy==2.5.4, ..."). A failed build: uv's words, with the last lines of the build's own output
    among them, where its error is. At most SAID characters, cut at a word (uv_output keeps all it wrote)."""
    return _said_whole(text)[0]


def _said_whole(text: str) -> Tuple[str, bool]:
    """_said's words, and whether they hold all uv said (no line of a build's output left out, nothing cut)."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln and not _UV_PROGRESS.match(ln)]
    start = next((i for i, ln in enumerate(lines) if "No solution found" in ln), None)
    if start is not None:
        head = _UV_MARK.sub("", lines[start]).rstrip(":")
        why = " ".join(_UV_MARK.sub("", ln) for ln in lines[start + 1:])
        said = "%s: %s" % (head, why) if why else head
        return _cut(said, SAID), len(said) <= SAID
    first = next((i for i, ln in enumerate(lines) if _UV_SAYS.match(ln)), None)
    if first is None:   # nothing in uv's own words: the end of what it wrote
        said = " | ".join(lines[-BUILD_LINES:])
        return (_cut(said, SAID) or "no message"), len(lines) <= BUILD_LINES and len(said) <= SAID
    own = [i for i in range(first, len(lines)) if _UV_SAYS.match(lines[i])]
    build = [i for i in range(first, len(lines)) if not _UV_SAYS.match(lines[i])]
    keep = sorted(set(own) | set(build[-BUILD_LINES:]))
    said = " | ".join(_UV_BOX.sub("", lines[i]) for i in keep)
    return _cut(said, SAID), first == 0 and len(keep) == len(lines) and len(said) <= SAID


def _refused_a_link(out: str) -> str:
    """What to say after uv's words when Windows refused it a link (os error 448), else ""."""
    if not any(w in (out or "").lower() for w in GUARD_448):
        return ""
    if redirection_guard():
        return (" Windows refused uv the link in Kelvin's Python folder because Windows' RedirectionGuard is on in "
                "Kelvin, and every program Kelvin starts has it too: the installer that started Kelvin (after an "
                "update too) passed it on. Close Kelvin, start it again from the Start menu or its desktop shortcut, "
                "and install again.")
    return (" Windows refused uv a link in Kelvin's Python folder (an untrusted mount point: Windows' RedirectionGuard "
            "in the program that started Kelvin, or the folder kept by OneDrive's Files On-Demand). Close Kelvin, "
            "start it again from the Start menu or its desktop shortcut, and install again.")


def uv_output(home: Any, what: str, out: str) -> Optional[Path]:
    """All uv wrote when it failed, kept in ~/Kelvin/tools/uv/failed (the last 20), for the whole of a long build's
    output; its path, or None when it couldn't be written."""
    folder = root(home) / "tools" / "uv" / "failed"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ("%s-%s.txt" % (time.strftime("%Y%m%d-%H%M%S"), re.sub(r"[^A-Za-z0-9_.-]+", "_", what)[:60]))
        path.write_text(out or "", encoding="utf-8")
        for old in sorted(folder.glob("*.txt"))[:-20]:
            old.unlink()
        return path
    except OSError:
        return None


def _failed(home: Any, what: str, out: str, also: str = "") -> str:
    """uv's words on a failure (and `also`, said right after them), what Windows' refusal of a link means if it was
    that, and, when uv's words leave some of what it wrote out, where all of it is kept."""
    said, whole = _said_whole(out)
    kept = None if whole else uv_output(home, what, out)
    return "%s%s%s%s" % (said, also, _refused_a_link(out), " (all uv wrote: %s)" % kept if kept else "")


def _uv(home: Any) -> Path:
    uv = uv_path(home)
    if uv is None:
        raise RuntimeError("uv is not on this computer yet")
    return uv


# ------------------------------------------------------------------ environments

def create(name: str, python: str = "", bits: int = 64, home: Any = None, run: Optional[Run] = None,
           why: str = "") -> Dict[str, Any]:
    """A new environment with its own Python, which uv fetches if it has none of that version and bitness. A Python uv
    has already is named by its version's own folder, never through uv's link to it, which Windows refuses under
    RedirectionGuard (field report 2026-11-17); where uv fetched the Python and then stopped at that link, it is asked
    again with the folder it fetched. The environment then names its Python so too (pin_python)."""
    run = run or _run
    if bits not in (32, 64):
        raise ValueError("bits is 32 or 64")
    if not re.match(r"^3\.\d{1,2}$", python or app_python()[0]):
        raise ValueError("python is a version such as 3.12")
    python = python or app_python()[0]
    env = uv_env(home)
    if bits == 32:
        env["UV_PYTHON_ARCH"] = "x86"
    d = env_dir(name, home)
    uv = str(_uv(home))

    def make(exe: Optional[Path]) -> Tuple[int, str]:
        if d.exists() and info(name, home) is None:   # what an attempt that failed left: no environment of Kelvin's
            shutil.rmtree(d, ignore_errors=True)
        return run([uv, "venv", str(d), "--python", str(exe) if exe is not None else python], env)
    exe = managed_python(python, bits, home)
    code, out = make(exe)
    if code and exe is None:
        exe = managed_python(python, bits, home)   # fetched, then refused its link: there in its own folder now
        if exe is not None:
            code, again = make(exe)
            out = again if not code else out + "\n" + again
    if code:
        raise RuntimeError("uv could not make the environment %s: %s" % (name, _failed(home, "venv " + name, out)))
    rec = {"name": name, "python": python, "bits": bits, "packages": {}, "files": [], "for": [why] if why else [],
           "created": time.time()}
    _save(rec, home)
    pin_python(name, home)
    return rec


def app_pins() -> List[str]:
    """The app's own packages that compiled ones build on, at the app's versions. The app imports these itself, so
    an environment it attaches gets the same ones, and uv picks makers' packages that work with them."""
    pins = []
    for module in ("numpy",):
        try:
            pins.append("%s==%s" % (module, __import__(module).__version__))
        except Exception:
            pass
    return pins


def _pip(name: str, what: List[str], home: Any, run: Run) -> Dict[str, Any]:
    """uv pip install into an environment (the drivers environment held to the app's own numpy), then record what
    it has."""
    rec = info(name, home)
    if rec is None:
        raise RuntimeError("no environment %s" % name)
    uv, py, env = _uv(home), python_of(name, home), uv_env(home)
    cmd = [str(uv), "pip", "install", "--python", str(py)]
    pins = app_pins() if name == DRIVERS else []
    if pins:
        (env_dir(name, home) / "constraints.txt").write_text("\n".join(pins) + "\n", encoding="utf-8")
        cmd += ["--constraint", str(env_dir(name, home) / "constraints.txt")]
    code, out = run(cmd + what, env)
    if code:
        held = [p for p in pins if p in _said(out)]
        also = (" (%s is Kelvin's own. Kelvin's built-in drivers run with it whatever this environment has, so the "
                "environment keeps it.)" % " and ".join(held)) if held else ""   # a person can't change it here
        raise RuntimeError("uv could not install %s: %s" % (", ".join(Path(w).name for w in what),
                                                            _failed(home, "pip " + name, out, also)))
    code, out = run([str(uv), "pip", "list", "--python", str(py), "--format", "json"], env)
    try:
        rec["installed"] = {d["name"].lower().replace("_", "-"): d["version"] for d in json.loads(out[out.find("["):])}
    except (ValueError, KeyError, TypeError):
        rec.setdefault("installed", {})
    return rec


def install(name: str, packages: List[str], home: Any = None, run: Optional[Run] = None) -> Dict[str, Any]:
    """Install packages from PyPI into an environment, each as requirement() writes it (one argument of uv's list,
    never read by a shell), then record the versions it got."""
    bad = [not_a_package(p.strip() if isinstance(p, str) else "") for p in packages]
    if any(bad):
        raise ValueError("not package names: %s" % "; ".join(b for b in bad if b))
    packages = [requirement(p) for p in packages]
    rec = _pip(name, packages, home, run or _run)
    for p in packages:
        rec["packages"][base_name(p)] = rec["installed"].get(base_name(p).lower().replace("_", "-"), "?")
    _save(rec, home)
    return rec


def installable(path: Path) -> bool:
    """A maker's Python package as pip takes it: a wheel, a source archive, or a folder with its setup."""
    path = Path(path)
    if path.is_dir():
        return (path / "pyproject.toml").is_file() or (path / "setup.py").is_file()
    return path.name.lower().endswith((".whl", ".tar.gz"))


def install_path(name: str, path: Path, home: Any = None, run: Optional[Run] = None) -> Dict[str, Any]:
    """A maker's Python package from a file or folder on this computer (from its SDK or download page)."""
    path = Path(path)
    before = set((info(name, home) or {}).get("installed", {}))
    rec = _pip(name, [str(path)], home, run or _run)
    for new in sorted(set(rec["installed"]) - before) or [path.name]:
        rec["packages"][new] = rec["installed"].get(new, "local")
    if path.name not in rec["files"]:
        rec["files"].append(path.name)
    _save(rec, home)
    return rec


def add_files(name: str, source: Path, home: Any = None) -> Path:
    """A maker's files (a folder, a Python package, a DLL) kept in the environment, where its Python finds them."""
    rec = info(name, home)
    if rec is None:
        raise RuntimeError("no environment %s" % name)
    source = Path(source)
    dest = env_dir(name, home) / "files"
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / source.name
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        shutil.copy2(source, target)
    site = site_packages(name, home)
    if site is not None:   # the environment's own Python imports from files/ too
        (site / "kelvin-files.pth").write_text(str(dest) + "\n", encoding="utf-8")
    if source.name not in rec["files"]:
        rec["files"].append(source.name)
    _save(rec, home)
    return dest


def remove(name: str, home: Any = None) -> bool:
    """Remove an environment. Files still in use (the app imported a driver from it, a library's helper still runs)
    go when Kelvin next starts (cleanup); returns False then. The environment is gone from the list either way."""
    d = env_dir(name, home)
    try:
        (d / "kelvin.json").unlink()
    except OSError:
        pass
    shutil.rmtree(d, ignore_errors=True)
    if d.exists():
        (d / ".removed").write_text("removed %s\n" % time.ctime(), encoding="utf-8")
        return False
    return True


def cleanup(home: Any = None) -> None:
    """At start: finish removing environments whose files were in use then."""
    folder = root(home) / "env"
    for d in folder.iterdir() if folder.is_dir() else []:
        if (d / ".removed").is_file():
            shutil.rmtree(d, ignore_errors=True)


def have(spec: str) -> Optional[str]:
    """The version of a package the app can import (from itself or an attached environment), or None."""
    import importlib.metadata
    want = base_name(spec).lower().replace("_", "-")
    for dist in importlib.metadata.distributions():
        try:
            if (dist.metadata["Name"] or "").lower().replace("_", "-") == want:
                return dist.version
        except Exception:
            continue
    return None


def lacking(requires: List[str]) -> List[str]:
    """Which of a card's packages the app cannot import yet."""
    return [r for r in requires or [] if have(r) is None]


def attach(name: str = DRIVERS, home: Any = None) -> Optional[str]:
    """Let the app import an environment's packages: its site-packages (and the folders its .pth files name) go on
    the import path after the app's own, so the app's own numpy stays. Only an environment of the app's Python
    version and bitness can be attached. Returns the folder, or None when there is no such environment."""
    rec, site = info(name, home), site_packages(name, home)
    if rec is None or site is None or (rec.get("python"), rec.get("bits", 64)) != app_python():
        return None
    import importlib
    if str(site) not in sys.path:
        sys.path.append(str(site))
        importlib.invalidate_caches()
    # its .pth files, as Python's site module reads them: folders (relative to site-packages) in order, and a
    # package's own start-up line (pywin32 adds its DLL folder), but not the venv's and setuptools' own hooks
    for pth in sorted(site.glob("*.pth")):
        for line in pth.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "_virtualenv" in line or "_distutils_hack" in line:
                continue
            if line.startswith(("import ", "import\t")):
                try:
                    exec(line)
                except Exception:
                    pass
                continue
            folder = Path(line) if Path(line).is_absolute() else site / line
            if folder.is_dir() and str(folder) not in sys.path:
                sys.path.append(str(folder))
                importlib.invalidate_caches()
    return str(site)


def missing(*packages: str) -> str:
    """What to say when a built-in driver's maker's package is not on this computer."""
    one = len(packages) == 1
    return ("%s %s not on this computer: Kelvin installs %s into its drivers environment itself "
            "(install_for_instrument with env %s and packages %s); from source, pip install %s"
            % (" and ".join(packages), "is" if one else "are", "it" if one else "them", DRIVERS,
               json.dumps(list(packages)), " ".join(packages)))


_NEEDED = re.compile(r"install_for_instrument with env ([a-z][a-z0-9_-]*) and packages (\[[^\]]*\])")


def needed(text: str) -> Optional[Dict[str, Any]]:
    """The install a missing-package message asks for (missing() and library.py write it): {env, packages}, so the
    Lab panel can offer it. None when the text asks for none."""
    m = _NEEDED.search(text or "")
    try:
        packages = json.loads(m.group(2)) if m else None
    except ValueError:
        return None
    if not packages or not all(isinstance(p, str) and not not_a_package(p) for p in packages):
        return None
    return {"env": m.group(1), "packages": packages}


def for_people(text: str) -> str:
    """A missing-package message as a person reads it beside the Install button: what needs the packages, and that
    they aren't here yet, without the part for the AI (how Kelvin installs them)."""
    need = needed(text)
    if need is None:
        return text
    names, one = " and ".join(need["packages"]), len(need["packages"]) == 1
    cuts = [i for i in (text.find(names + " is not on this computer"), text.find(names + " are not on this computer"),
                        text.find(" Kelvin installs")) if i > 0]
    lead = text[:min(cuts)].rstrip(" :;,") if cuts else ""
    where = "on this computer" if need["env"] == DRIVERS else "in Kelvin's environment %s" % need["env"]
    return "%s%s %s %s yet." % (lead + ": " if lead else "", names, "isn't" if one else "aren't", where)


def self_test(packages: Tuple[str, ...] = ("Pyro4", "ujson")) -> str:
    """For the release build's self-test, which has the network: uv fetched and checked, the drivers environment
    made with the app's own Python, a pure and a compiled package installed into it, and the app importing both
    from there. Raises on any failure."""
    import importlib
    uv_path() or fetch_uv()
    if info(DRIVERS) is None:
        create(DRIVERS, why="the self-test")
    rec = install(DRIVERS, list(packages))
    if attach(DRIVERS) is None:
        raise RuntimeError("the drivers environment could not be attached")
    where = env_dir(DRIVERS).resolve()
    for p in packages:
        module = importlib.import_module(p)
        if where not in Path(module.__file__).resolve().parents:
            raise RuntimeError("%s came from %s, not from the drivers environment" % (p, module.__file__))
    return "Python %s, %s" % (rec["python"], ", ".join("%s %s" % (p, rec["packages"].get(p, "?")) for p in packages))


def pypi(spec: str, get: Optional[Callable[[str], bytes]] = None) -> Dict[str, str]:
    """What PyPI says about a package: its name, the latest version, a one-line summary and its home page. Raises
    LookupError when PyPI has no such package."""
    name = base_name(spec)
    try:
        data = json.loads((get or _get)("https://pypi.org/pypi/%s/json" % name))
    except Exception as e:
        if "404" in str(e):
            raise LookupError("PyPI has no package %s" % name) from e
        raise
    meta = data.get("info") or {}
    urls = meta.get("project_urls") or {}
    home = meta.get("home_page") or urls.get("Homepage") or urls.get("Source") or next(iter(urls.values()), "")
    return {"name": meta.get("name") or name, "version": meta.get("version") or "", "summary":
            (meta.get("summary") or "").strip()[:160], "home": home or ""}
