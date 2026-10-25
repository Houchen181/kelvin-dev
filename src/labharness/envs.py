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
# a package as pip names it, with extras and a version if wanted: qick, qick==0.2.436, zhinst-core>=24.1
PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?\s*((==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9.*+!_-]+)?$")
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
    return re.split(r"[\[<>=!~\s]", spec.strip(), maxsplit=1)[0]


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
    exe = "uv.exe" if WIN else "uv"
    mine = root(home) / "tools" / "uv" / exe
    if mine.is_file():
        return mine
    found = shutil.which("uv")
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
    """uv keeps everything in Kelvin's folder and uses only the Pythons it fetched (never one a program left)."""
    tools = root(home) / "tools"
    env = {k: v for k, v in clean_environ().items()
           if k not in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "CONDA_PREFIX")}
    env.update({"UV_PYTHON_INSTALL_DIR": str(tools / "python"), "UV_CACHE_DIR": str(tools / "uv-cache"),
                "UV_MANAGED_PYTHON": "1", "UV_NO_PROGRESS": "1", "UV_NO_CONFIG": "1", "UV_LINK_MODE": "copy"})
    return env


def _run(cmd: List[str], env: Dict[str, str]) -> Tuple[int, str]:
    p = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1800,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return p.returncode, p.stdout.decode("utf-8", "replace")


def _tail(text: str) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return " | ".join(lines[-3:])[:400] or "no message"


def _uv(home: Any) -> Path:
    uv = uv_path(home)
    if uv is None:
        raise RuntimeError("uv is not on this computer yet")
    return uv


# ------------------------------------------------------------------ environments

def create(name: str, python: str = "", bits: int = 64, home: Any = None, run: Optional[Run] = None,
           why: str = "") -> Dict[str, Any]:
    """A new environment with its own Python, which uv fetches if it has none of that version and bitness."""
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
    code, out = run([str(_uv(home)), "venv", str(d), "--python", python], env)
    if code:
        raise RuntimeError("uv could not make the environment %s: %s" % (name, _tail(out)))
    rec = {"name": name, "python": python, "bits": bits, "packages": {}, "files": [], "for": [why] if why else [],
           "created": time.time()}
    _save(rec, home)
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
        raise RuntimeError("uv could not install %s: %s" % (", ".join(Path(w).name for w in what), _tail(out)))
    code, out = run([str(uv), "pip", "list", "--python", str(py), "--format", "json"], env)
    try:
        rec["installed"] = {d["name"].lower().replace("_", "-"): d["version"] for d in json.loads(out[out.find("["):])}
    except (ValueError, KeyError, TypeError):
        rec.setdefault("installed", {})
    return rec


def install(name: str, packages: List[str], home: Any = None, run: Optional[Run] = None) -> Dict[str, Any]:
    """Install packages into an environment, then record the versions it got."""
    bad = [p for p in packages if not PACKAGE.match(p.strip())]
    if bad:
        raise ValueError("%s: not a package name with an optional version" % ", ".join(bad))
    rec = _pip(name, [p.strip() for p in packages], home, run or _run)
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
    return ("%s %s not on this computer: Kelvin installs %s into its drivers environment once a person approves "
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
    if not packages or not all(isinstance(p, str) and PACKAGE.match(p) for p in packages):
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
