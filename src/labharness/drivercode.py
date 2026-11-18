"""Open driver code for an instrument Kelvin has no card for, read as text so its card can be drafted from the commands
the driver sends (autocard.py) when there is no public manual with a command reference.

Where Kelvin looks, in order:
    1. The drivers the instrument collection lists for the model (its software items of kind python-driver or
       other-driver): a file on GitHub (a blob address), or a repository or folder there, with the driver's dotted name
       (ref) found among the repository's files. A driver of one of the libraries below that the collection names by
       its dotted name and its documentation page (or PyPI) is found in that library.
    2. A short list of open driver libraries on GitHub (LIBRARIES): each library's file list is matched against the
       maker and the model (case and separators aside; the model's digits; a series a file's name covers, the 2600 for
       a 2636B, 3xx for a 340), best first, and a driver counts only once its text names the model or its series.
    3. Last, a web search for a driver on GitHub (the search Kelvin uses for manuals), keeping GitHub's addresses only.
A person can also give the driver: its GitHub address, a dotted name in one of the libraries, or a file on this
computer (draft_card's driver).

What it fetches: only https addresses on GitHub (its API and raw.githubusercontent.com), each file at a fixed commit,
never a branch that can move meanwhile; at most MAX_FILE bytes a file, MAX_FILES files and MAX_CALLS requests to the
API a card (GitHub allows 60 an hour without a sign-in). A library's file list is kept in ~/Kelvin/drivers with its
commit, and fetched again after a week. Nothing is run: the code is only read, as text.

The model gets the driver and the files of its own package it is built on (base classes, mixins, channels), up to
BUDGET characters. A driver that reaches the instrument through a maker's library (a DLL through ctypes or cffi, a .NET
assembly through pythonnet, a COM object, the maker's own package) can't become a text card: Kelvin names what it loads
instead (`loads`), for a library card."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ._version import __version__

Fetch = Callable[[str], bytes]
Search = Callable[[str], List[str]]
Log = Callable[[str], None]

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
FETCHED_FROM = ("api.github.com", "raw.githubusercontent.com")   # the only hosts driver code is fetched from
GIVEN_ON = ("github.com", "www.github.com", "raw.githubusercontent.com")   # where a driver's address may point
MAX_FILE = 200_000      # bytes of one driver file: larger is not a driver to read (pyLabLib's 544 KB DLL definitions)
MAX_ANSWER = 8_000_000  # bytes of one answer of GitHub's API (a large repository's file list)
MAX_FILES = 10          # files fetched for one card: the candidates' own files, then the chosen one's package
MAX_CALLS = 16          # requests to GitHub's API for one card
MAX_TRIED = 3           # of the drivers one source names, those fetched to see which names the model
BUDGET = 150_000        # characters of code the model reads: the driver first, then the files it is built on
WEEK = 7 * 24 * 3600    # a library's file list is fetched again after a week
SOURCES = (".py", ".c", ".cpp", ".h", ".hpp", ".cs", ".m")   # driver code (Micro-Manager's device adapters are C++)
KINDS = ("python-driver", "other-driver")                   # the collection's software a card can be drafted from
LISTED = ("python-driver", "other-driver", "vendor-sdk")     # what find_card and the scan name beside the manuals
SHA = re.compile(r"^[0-9a-f]{40}$", re.I)
SKIP_FOLDERS = {"tests", "test", "testing", "examples", "example", "docs", "doc", "mock_instruments", "sims",
                "simulated", "fakes"}


class DriverError(ValueError):
    pass


def _quiet(message: str) -> None:
    pass


def _first_line(e: BaseException) -> str:
    from .profiles import _first_line as first
    return first(e)


@dataclass(frozen=True)
class Library:
    """An open driver library on GitHub: its repository, the folder its drivers are in, the Python package its dotted
    names start with and where that package is, its license, and how its drivers send commands, for the model.
    Repositories, folders and licenses as GitHub gave them on 2026-11-17 (InstrumentKit's from its pyproject.toml and
    license folder: GitHub names none)."""
    name: str
    repo: str
    folder: str
    package: str
    root: str
    license: str
    sends: str
    aliases: Tuple[str, ...] = ()


_QCODES = ("add_parameter(name, get_cmd=..., set_cmd=..., vals=..., val_mapping=..., get_parser=..., unit=...) makes "
           "each parameter; in a set_cmd, {} is where the value goes ({{}} inside an f-string, {:.4f} with a format), "
           "and a get_cmd or set_cmd that is a method sends what that method writes; VisaInstrument asks *IDN? who it "
           "is")
LIBRARIES = (
    Library("PyMeasure", "pymeasure/pymeasure", "pymeasure/instruments/", "pymeasure", "pymeasure/", "MIT",
            "Instrument.control(get, set, ...), Instrument.measurement(get, ...) and Instrument.setting(set, ...) make "
            "each parameter, %g, %d or %s in the set command is where the value goes, and validator= with values= "
            "says what a value may be (map_values=True: values maps names to the codes sent); SCPIMixin answers "
            "*IDN? and reads errors with SYST:ERR?"),
    Library("QCoDeS", "microsoft/Qcodes", "src/qcodes/instrument_drivers/", "qcodes", "src/qcodes/", "MIT", _QCODES,
            aliases=("QCoDeS/Qcodes",)),
    Library("QCoDeS contrib drivers", "QCoDeS/Qcodes_contrib_drivers", "src/qcodes_contrib_drivers/drivers/",
            "qcodes_contrib_drivers", "src/qcodes_contrib_drivers/", "MIT", _QCODES),
    Library("InstrumentKit", "instrumentkit/InstrumentKit", "src/instruments/", "instruments", "src/instruments/",
            "AGPL-3.0",
            "sendcmd(...) and query(...) send its commands; bool_property, enum_property, int_property, "
            "unitless_property and unitful_property(command, ...) read with '<command>?' and set with "
            "'<command> <value>' unless set_cmd or set_fmt say otherwise, and unitful_property's valid_range is its "
            "range; SCPIInstrument asks *IDN?"),
    Library("pyLabLib", "AlexShkarin/pyLabLib", "pylablib/devices/", "pylablib", "pylablib/", "GPL-3.0",
            "write(msg, arg) sends '<msg> <arg>' and ask(msg) reads the reply; _add_scpi_parameter(name, comm, kind) "
            "reads with '<comm>?' and sets with '<comm> <value>'; _id_comm is the identity query (*IDN? unless the "
            "class says otherwise)"),
)


def library_for(repo: str) -> Optional[Library]:
    """The library a repository (owner/name, in any case) is, if it is one of LIBRARIES."""
    low = (repo or "").lower()
    return next((lib for lib in LIBRARIES if low in [r.lower() for r in (lib.repo,) + lib.aliases]), None)


def library_of(ref: str) -> Optional[Library]:
    """The library a dotted name (pymeasure.instruments.keithley.Keithley2000) is in."""
    top = (ref or "").split(".")[0]
    return next((lib for lib in LIBRARIES if lib.package == top), None)


def is_dotted(text: str) -> bool:
    """A dotted name in one of the libraries (pylablib.devices.Thorlabs.FW), not a file or an address."""
    return bool(re.match(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$", text or "")) and library_of(text) is not None


def on_this_computer(text: str) -> bool:
    """Whether a driver a person named is a file on this computer: neither a web address nor a dotted name in one of
    the libraries (unless a file has that name here)."""
    text = (text or "").strip()
    if not text or re.match(r"^https?://", text, re.I):
        return False
    try:
        return not is_dotted(text) or Path(text).expanduser().exists()
    except (OSError, ValueError):   # not a name a file could have
        return not is_dotted(text)


# ------------------------------------------------------------------ GitHub


@dataclass
class Address:
    """A GitHub address a driver is named by: a file, a folder or a repository (kind), with the branch, tag or commit
    it names (ref; "" for the default branch)."""
    repo: str
    ref: str
    path: str
    kind: str


_GITHUB = re.compile(r"^https://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?"
                     r"(?:/(blob|tree|raw)/([^/?#]+)(?:/([^?#]*))?)?/?(?:[?#].*)?$", re.I)
_RAWFILE = re.compile(r"^https://raw\.githubusercontent\.com/([\w.-]+)/([\w.-]+)/([^/?#]+)/([^?#]+)$", re.I)


def address(url: str) -> Optional[Address]:
    """A GitHub file (blob or raw), folder (tree) or repository address; None for anything else, http:// too."""
    url = (url or "").strip()
    m = _RAWFILE.match(url)
    if m:
        return Address("%s/%s" % (m.group(1), m.group(2)), m.group(3), urllib.parse.unquote(m.group(4)), "file")
    m = _GITHUB.match(url)
    if not m:
        return None
    repo = "%s/%s" % (m.group(1), m.group(2))
    if m.group(3) is None:
        return Address(repo, "", "", "repo")
    path = urllib.parse.unquote(m.group(5) or "").strip("/")
    if not path:
        return Address(repo, m.group(4), "", "repo")
    return Address(repo, m.group(4), path, "folder" if m.group(3).lower() == "tree" else "file")


def pinned(repo: str, commit: str, path: str) -> str:
    """A file's raw address at a commit: what Kelvin fetches, never a branch that can move."""
    return "%s/%s/%s/%s" % (RAW, repo, commit, urllib.parse.quote(path))


def page(repo: str, commit: str, path: str) -> str:
    """A file's page on GitHub at a commit: what a person opens to read it."""
    return "https://github.com/%s/blob/%s/%s" % (repo, commit, urllib.parse.quote(path))


def check(url: str) -> str:
    """The host of an address Kelvin may fetch driver code from: https on GitHub's API or raw.githubusercontent.com,
    nothing else (DriverError)."""
    try:
        parts = urllib.parse.urlsplit(url or "")
        host, port = (parts.hostname or "").lower(), parts.port
        fine = parts.scheme == "https" and host in FETCHED_FROM and port in (None, 443) \
            and not parts.username and not parts.password
    except ValueError:
        fine = False
    if not fine:
        raise DriverError("Kelvin reads drivers from GitHub only (https://github.com or raw.githubusercontent.com), "
                          "not %s" % url)
    return host


class _GitHubOnly(urllib.request.BaseHandler):
    """Every request, and every redirect it is sent on, goes to GitHub over https (check)."""
    handler_order = 99

    def http_request(self, req: urllib.request.Request) -> urllib.request.Request:
        check(req.full_url)
        return req

    https_request = http_request


def http_get(url: str) -> bytes:
    """GitHub's API (JSON; of a commit, its SHA) or a raw file, read up to MAX_ANSWER or MAX_FILE bytes."""
    from .profiles import _InternetOnly
    host = check(url)
    headers = {"User-Agent": "Kelvin/%s (lab assistant)" % __version__}
    if host == "api.github.com":
        headers["Accept"] = "application/vnd.github.sha" if "/commits/" in url else "application/vnd.github+json"
    most = MAX_ANSWER if host == "api.github.com" else MAX_FILE
    opener = urllib.request.build_opener(_GitHubOnly(), _InternetOnly())
    with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as r:
        data = r.read(most + 1)
    if len(data) > most:
        raise DriverError("%s is larger than %d KB" % (url, most // 1000))
    return data


class GitHub:
    """GitHub as Kelvin reads drivers from it for one card: what it asks there is counted (MAX_CALLS, MAX_FILES) and
    capped (MAX_ANSWER, MAX_FILE); the libraries' file lists are kept in `folder` (~/Kelvin/drivers) for a week."""

    def __init__(self, fetch: Optional[Fetch] = None, folder: Any = None, log: Log = _quiet,
                 now: Callable[[], float] = time.time):
        self.fetch = fetch or http_get
        self.folder = Path(folder) if folder else None
        self.log, self.now = log, now
        self.calls = 0      # requests to the API for this card
        self.files = 0      # files fetched for this card
        self._trees: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._texts: Dict[Tuple[str, str, str], str] = {}
        self._about: Dict[str, Dict[str, str]] = {}

    def _get(self, url: str, most: int) -> bytes:
        check(url)
        data = self.fetch(url)
        if len(data) > most:
            raise DriverError("%s is larger than %d KB" % (url, most // 1000))
        return data

    def api(self, path: str) -> bytes:
        if self.calls >= MAX_CALLS:
            raise DriverError("Kelvin has asked GitHub %d times for this card" % MAX_CALLS)
        self.calls += 1
        return self._get(API + path, MAX_ANSWER)

    def _json(self, path: str) -> Any:
        data = self.api(path)
        try:
            return json.loads(data.decode("utf-8"))
        except ValueError:   # not UTF-8, or not JSON
            raise DriverError("GitHub did not answer %s with JSON" % path) from None

    def commit(self, repo: str, ref: str = "") -> str:
        """The commit a branch, a tag or HEAD (ref "": the default branch) is at now; a commit as it is."""
        if SHA.match(ref or ""):
            return ref.lower()
        from .collection import CollectionError, latest_commit
        try:
            return latest_commit(self.api("/repos/%s/commits/%s" % (repo, urllib.parse.quote(ref or "HEAD", safe=""))))
        except CollectionError:
            raise DriverError("GitHub did not say which commit %s is at" % repo) from None

    def about(self, repo: str) -> Dict[str, str]:
        """A public repository's own name (after a rename), default branch and license."""
        key = repo.lower()
        if key not in self._about:
            data = self._json("/repos/%s" % repo)
            if not isinstance(data, dict) or data.get("private") is not False:
                raise DriverError("%s is not a public repository" % repo)
            lic = data.get("license") if isinstance(data.get("license"), dict) else {}
            spdx = str(lic.get("spdx_id") or "")
            self._about[key] = {"repo": str(data.get("full_name") or repo),
                                "branch": str(data.get("default_branch") or ""),
                                "license": spdx if spdx and spdx != "NOASSERTION" else
                                str(lic.get("name") or "unknown")}
        return self._about[key]

    def tree(self, repo: str, commit: str = "") -> Dict[str, Any]:
        """A repository's source files, {repo, commit, license, fetched, files: {path: size}}. One of the libraries':
        its drivers' folder at the newest commit of its default branch, kept in `folder` and fetched again after a
        week (the copy here stays in use while GitHub can't be asked). Any other repository's: at `commit` (default:
        its newest), for this card only."""
        lib = library_for(repo)
        key = (repo.lower(), "" if lib is not None else commit)
        if key in self._trees:
            return self._trees[key]
        kept = self._kept(lib) if lib is not None else None
        if kept is not None and self.now() - float(kept.get("fetched") or 0) < WEEK:
            self._trees[key] = kept
            return kept
        try:
            fresh = self._fetch_tree(repo, lib, commit)
        except (DriverError, OSError, ValueError) as e:
            if kept is None:
                raise
            self.log("%s: %s; Kelvin uses its list of the drivers from %s" % (
                lib.name, _first_line(e), time.strftime("%Y-%m-%d", time.localtime(float(kept["fetched"])))))
            fresh = kept
        else:
            if lib is not None:
                self._keep(lib, fresh)
        self._trees[key] = fresh
        return fresh

    def _fetch_tree(self, repo: str, lib: Optional[Library], commit: str) -> Dict[str, Any]:
        if lib is not None:
            name, license = lib.repo, lib.license
            commit = self.commit(name)
        else:
            info = self.about(repo)
            name, license = info["repo"], info["license"]
            commit = commit if SHA.match(commit or "") else self.commit(name, info["branch"])
        data = self._json("/repos/%s/git/trees/%s?recursive=1" % (name, commit))
        entries = data.get("tree") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise DriverError("GitHub did not list the files of %s" % name)
        under = lib.folder if lib is not None else ""
        files = {str(e["path"]): int(e.get("size") or 0) for e in entries
                 if isinstance(e, dict) and e.get("type") == "blob" and str(e.get("path") or "").startswith(under)
                 and str(e.get("path")).lower().endswith(SOURCES)}
        if data.get("truncated"):
            self.log("GitHub listed only part of the files of %s" % name)
        return {"repo": name, "commit": commit, "license": license, "fetched": self.now(), "files": files}

    def _file(self, lib: Library) -> Optional[Path]:
        return self.folder / (lib.repo.replace("/", "__") + ".json") if self.folder is not None else None

    def _kept(self, lib: Library) -> Optional[Dict[str, Any]]:
        path = self._file(lib)
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path is not None else None
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not SHA.match(str(data.get("commit") or "")) \
                or not isinstance(data.get("files"), dict):
            return None
        return data

    def _keep(self, lib: Library, tree: Dict[str, Any]) -> None:
        path = self._file(lib)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name("%s.%d.part" % (path.name, os.getpid()))
            tmp.write_text(json.dumps(tree), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:   # kept for this card only; asked again for the next
            self.log("could not keep %s's list of drivers: %s" % (lib.name, _first_line(e)))

    def text(self, repo: str, commit: str, path: str, size: int = 0) -> str:
        """A file of a repository at a commit, as text: it is never run."""
        key = (repo.lower(), commit, path)
        if key in self._texts:
            return self._texts[key]
        if size > MAX_FILE:
            raise DriverError("%s is larger than %d KB" % (path, MAX_FILE // 1000))
        if self.files >= MAX_FILES:
            raise DriverError("Kelvin has fetched %d files for this card" % MAX_FILES)
        self.files += 1
        text = self._get(pinned(repo, commit, path), MAX_FILE).decode("utf-8", errors="replace")
        self._texts[key] = text
        return text

    def listing(self, repo: str, commit: str, folder: str) -> Dict[str, int]:
        """The source files of one folder of a repository at a commit, {path: size}."""
        data = self._json("/repos/%s/contents/%s?ref=%s" % (repo, urllib.parse.quote(folder), commit))
        if not isinstance(data, list):
            raise DriverError("%s is not a folder of %s" % (folder, repo))
        return {str(e["path"]): int(e.get("size") or 0) for e in data
                if isinstance(e, dict) and e.get("type") == "file"
                and str(e.get("path") or "").lower().endswith(SOURCES)}


# ------------------------------------------------------------------ matching a maker and a model


GENERIC_WORDS = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "gmbh", "ag", "sa",
                 "llc", "bv", "kk", "plc", "the", "and", "of", "instruments", "instrument", "technologies",
                 "technology", "systems", "system", "electronics", "scientific", "labs", "laboratories", "laboratory",
                 "group", "international", "industries", "devices", "photonics"}
# other names a maker's drivers are filed under: its names before (Agilent and HP for Keysight), its identity reply's
ALIASES = {"keysight": ("agilent", "hp", "hewlettpackard"), "agilent": ("keysight", "hp", "hewlettpackard"),
           "hewlettpackard": ("hp", "agilent", "keysight"), "lsci": ("lakeshore",), "lakeshore": ("lsci",),
           "stanford": ("srs",), "signalrecovery": ("ametek",), "ametek": ("signalrecovery",), "zurich": ("zhinst",),
           "americanmagnetics": ("ami",)}


def compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def maker_names(maker: str) -> List[str]:
    """The names a maker's drivers are filed under: its name run together (lakeshorecryotronics), its first two words
    together (lakeshore, rohdeschwarz), its own words (keithley, oxford), its initials (srs, ami), and ALIASES."""
    words = re.findall(r"[a-z0-9]+", (maker or "").lower())
    names = [compact(maker)] + [w for w in words if w not in GENERIC_WORDS and len(w) >= 3]
    if len(words) >= 2:
        names += [words[0] + words[1], "".join(w[0] for w in words)]
    for key, more in ALIASES.items():
        if key in compact(maker):
            names += list(more)
    return [n for n in dict.fromkeys(names) if n]


def _names_maker(name: str, names: Sequence[str]) -> bool:
    """Does a folder's name (run together) name the maker: keithley, srs, oxfordinstruments, tdk for TDK-Lambda?"""
    return bool(name) and any(name == n or (len(name) >= 3 and n.startswith(name))
                              or (len(n) >= 4 and name.startswith(n)) for n in names)


def _word_fit(stem: str, word: str) -> int:
    """How a file's name (lower case, its separators _) fits one word of the model's name: 3 it has it
    (keithley2000, sr830), 2 its digits (agilente4980 for e4980a), 1 its series, with the word's letters before it
    (keithley2600 for 2636b, lakeshore3xx for 340, tpg36x for tpg362, aq6370series), 0 none."""
    if re.search(r"(?<![0-9])%s(?![0-9])" % re.escape(word), stem):
        return 3
    m = re.match(r"([a-z]*)(\d+)", word)
    if not m:
        return 0
    letters, digits = m.group(1), m.group(2)
    runs = re.findall(r"\d+", stem)
    if len(digits) >= 3 and digits in runs:
        return 2
    for x in re.finditer(r"(\d+)(x+)", stem):   # 3xx, 26xx, 622x
        if len(x.group(1)) + len(x.group(2)) == len(digits) and digits.startswith(x.group(1)) \
                and stem[:x.start()].endswith(letters):
            return 1
    for r in re.finditer(r"\d+", stem):   # 2600, E36300: two zeros or more after two digits or more
        run = r.group(0)
        sig = run.rstrip("0")
        if run != digits and len(run) == len(digits) and len(run) - len(sig) >= 2 and len(sig) >= 2 \
                and digits.startswith(sig) and stem[:r.start()].endswith(letters):
            return 1
    for s in re.finditer(r"(\d+)_?series", stem):   # aq6370series
        run = s.group(1)
        if len(run) == len(digits) and digits.startswith(run.rstrip("0") or run) \
                and stem[:s.start()].endswith(letters):
            return 1
    return 0


def model_fit(name: str, model: str) -> int:
    """How a driver's file name fits the model: 3 it names it whole (tdk_gen40_38 for GEN40-38, mercuryitc for
    Mercury iTC), else the least of its words' fits (_word_fit); a model with no number in its name fits 1 a file
    named for its product line (cobolt for Cobolt 06-MLD)."""
    from .autocard import _model_words
    stem = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    whole, flat = compact(model), compact(stem)
    words = _model_words(model)
    if len(whole) >= 5 and whole in flat:
        return 3
    if not words:
        line = [w for w in re.findall(r"[a-z]+", model.lower()) if len(w) >= 5]
        return 1 if any(w in stem.split("_") for w in line) else 0
    return min(_word_fit(stem, w) for w in words)


def _distinct(model: str) -> bool:
    """A model name no other maker's would have (letters and digits, five or more: sr830, itc503, 33220a), so a file
    named so needs no maker folder."""
    from .autocard import _model_words
    return any(re.search(r"[a-z]", w) and re.search(r"\d", w) and len(w) >= 5 for w in _model_words(model))


def path_score(rel: str, maker: str, model: str) -> int:
    """How a driver's path in a library's drivers' folder (keithley/keithley2000.py) fits the maker and the model: 0
    not at all; else twice its model fit, plus one where a folder or the file names the maker. A model number alone
    (2000, 331) needs the maker too."""
    parts = rel.split("/")
    name = re.sub(r"\.[a-z]+$", "", parts[-1].lower())
    stem = compact(name)
    if not stem or stem in ("init", "setup", "conftest") or parts[-1].lower().startswith("test") \
            or any(p.lower() in SKIP_FOLDERS for p in parts[:-1]):
        return 0
    fit = model_fit(name, model)
    if not fit:
        return 0
    names = maker_names(maker)
    maker_named = any(_names_maker(compact(p), names) for p in parts[:-1]) or \
        any(len(n) >= 4 and n in stem for n in names)
    if not maker_named and not (fit == 3 and _distinct(model)):
        return 0
    return fit * 2 + int(maker_named)


def names_it(text: str, model: str) -> bool:
    """Does a driver's text name the model or its series (a series' driver is the model's driver)?"""
    from .autocard import names_model
    return names_model(text, model)


# ------------------------------------------------------------------ what a driver is built on, and what it loads


_FROM = re.compile(r"^[ \t]*from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]+(\([^)]*\)|[^\n]*)", re.M)
_CLASS = re.compile(r"^[ \t]*class[ \t]+\w+[ \t]*\(([^)]*)\)[ \t]*:", re.M)


def _imports(text: str) -> List[Tuple[int, str, List[Tuple[str, str]]]]:
    """Each from-import of a Python file: (its dots, its module, [(name, the name it goes by here)])."""
    out = []
    for m in _FROM.finditer(text or ""):
        names = []
        body = re.sub(r"#[^\n]*", "", m.group(3)).strip("() \t\n")
        for part in body.replace("\n", " ").split(","):
            bits = part.split()
            if bits and re.match(r"^\w+$", bits[0]):
                names.append((bits[0], bits[2] if len(bits) == 3 and bits[1] == "as" else bits[0]))
        out.append((len(m.group(1)), m.group(2), names))
    return out


def _bases(text: str) -> set:
    """The names classes in a Python file are built on: class X(KeithleyBuffer, SCPIMixin, Instrument)."""
    out = set()
    for m in _CLASS.finditer(text or ""):
        for base in m.group(1).split(","):
            name = re.match(r"\s*([\w.]+)", base)
            if name:
                out.add(name.group(1).split(".")[-1])
    return out


def _module_paths(stem: str, files: Any) -> List[str]:
    """The file a module path (pymeasure/instruments/keithley/buffer) is: a module, or a package's __init__.py."""
    return [p for p in (stem + ".py", stem + "/__init__.py") if p in files]


def _targets(level: int, module: str, names: List[Tuple[str, str]], here: str, files: Any,
             lib: Optional[Library]) -> List[str]:
    """The files a from-import in the file at `here` reaches, among `files`: a relative import's, or an absolute one
    into the library's own package. A package's __init__.py is not one of them: its modules are."""
    if level:
        base = here.split("/")[:-1]
        if level - 1 > len(base):
            return []
        base = base[:len(base) - (level - 1)]
    elif lib is not None and (module == lib.package or module.startswith(lib.package + ".")):
        base = [p for p in lib.root.strip("/").split("/") if p]
        module = module[len(lib.package):].lstrip(".")
    else:
        return []
    stem = "/".join(base + ([module.replace(".", "/")] if module else []))
    out = [p for p in _module_paths(stem, files) if not p.endswith("__init__.py")]
    if not out:   # from . import buffer, from .channels import output: modules of a package
        out = [p for name, _ in names for p in _module_paths(stem + "/" + name if stem else name, files)
               if not p.endswith("__init__.py")]
    return out


def _package_of(path: str, lib: Optional[Library]) -> str:
    """The folder of a driver's own package, whose files it is built on: in a library, its maker's folder
    (pymeasure/instruments/keithley/); elsewhere the folder above the driver's."""
    if lib is not None and path.startswith(lib.folder):
        rel = path[len(lib.folder):].split("/")
        return lib.folder + (rel[0] + "/" if len(rel) > 1 else "")
    parts = path.split("/")[:-1]
    return "/".join(parts[:-1]) + "/" if len(parts) > 1 else ""


def related(path: str, text: str, files: Any, lib: Optional[Library]) -> List[str]:
    """The files of its own package a Python driver imports, those its classes are built on first; never the library's
    framework (its Instrument classes, which send nothing of this instrument's)."""
    if not path.lower().endswith(".py"):
        return []
    package = _package_of(path, lib)
    bases = _bases(text)
    first: List[str] = []
    rest: List[str] = []
    for level, module, names in _imports(text):
        for target in _targets(level, module, names, path, files, lib):
            if target == path or not target.startswith(package) or target in first + rest:
                continue
            (first if any(local in bases for _, local in names) else rest).append(target)
    return first + rest


# how a driver reaches the instrument through a maker's library instead of text: what it loads, and through what
_LOADERS = [
    (re.compile(r"\b(?:cdll|windll|oledll)\.LoadLibrary\(\s*[rRuUbB]?[\"']([^\"'\n]+)[\"']"), "ctypes"),
    (re.compile(r"\b(?:CDLL|WinDLL|OleDLL|PyDLL|LoadLibrary|load_lib)\(\s*[rRuUbB]?[\"']([^\"'\n]+)[\"']"), "ctypes"),
    (re.compile(r"\bdlopen\(\s*[rRuUbB]?[\"']([^\"'\n]+)[\"']"), "cffi"),
    (re.compile(r"\bAddReference(?:ToFile(?:AndPath)?)?\(\s*[rRuUbB]?[\"']([^\"'\n]+)[\"']"), "clr"),
    (re.compile(r"\b(?:Dispatch|DispatchEx|CreateObject|GetActiveObject)\(\s*[rRuUbB]?[\"']([^\"'\n]+)[\"']"), "com"),
]
_CALLS = [   # the same calls with a name worked out as it runs
    (re.compile(r"\b(?:cdll|windll|oledll)\.\w+|\b(?:CDLL|WinDLL|OleDLL|PyDLL|LoadLibrary|load_lib)\("), "ctypes"),
    (re.compile(r"\bdlopen\("), "cffi"),
    (re.compile(r"\bAddReference\w*\("), "clr"),
    (re.compile(r"\b(?:Dispatch|DispatchEx|GetActiveObject)\("), "com"),
    (re.compile(r"[\"']ft232[\"']"), "the FTDI library (ft232)"),   # pyLabLib's backend for Thorlabs' APT devices
]
_THROUGH = {"ctypes": "a DLL, through ctypes", "cffi": "a DLL, through cffi",
            "clr": "a .NET assembly, through pythonnet", "com": "a COM object"}
# modules only a driver that skips text imports: pythonnet, COM, FTDI's and raw USB's (ctypes alone may only pack
# bytes: it counts with a call that loads a DLL)
_NATIVE = {"clr": "clr", "win32com": "com", "comtypes": "com", "pythoncom": "com",
           "ftd2xx": "the FTDI library (ftd2xx)", "pyftdi": "the FTDI library (pyftdi)",
           "ft232": "the FTDI library (ft232)", "pyft232": "the FTDI library (pyft232)", "usb": "raw USB (pyusb)",
           "hid": "USB HID (hid)", "pywinusb": "USB HID (pywinusb)"}
# instrument makers' own Python packages, which reach their instruments themselves (a DLL, a server, USB)
MAKER_PACKAGES = ("zhinst", "TimeTagger", "nidaqmx", "PyDAQmx", "pypylon", "pyvcam", "picosdk", "pyAndorSDK2",
                  "pyAndorSDK3", "pyAndorSpectrograph", "spinapi", "thorlabs_tsi_sdk", "pyspcm", "spcm", "spcm_core",
                  "pyximc", "mcculw", "uldaq", "atsapi", "pymmcore", "nifpga", "seabreeze", "vmbpy", "vimba",
                  "ids_peak", "pyueye", "harvesters", "pulsestreamer", "amc_api_python", "pycobolt", "qick", "Pyro4")
_IMPORTED = re.compile(r"^[ \t]*(?:import|from)[ \t]+([A-Za-z_]\w*)", re.M)
_DLL_NAMED = re.compile(r"\b\w+[ \t]*=[ \t]*[rRuUbB]?[\"']([\w.+-]+\.(?:dll|so|dylib))[\"']", re.I)


def loads(texts: Sequence[str]) -> List[str]:
    """What a driver's code loads to reach the instrument instead of sending it text: a DLL (by its name, through
    ctypes or cffi), a .NET assembly, a COM object, an FTDI or raw USB library, the maker's own package. [] for a
    driver that sends text."""
    named: List[str] = []
    seen = set()
    means: List[str] = []
    for text in texts:
        for rx, how in _LOADERS:
            for m in rx.finditer(text or ""):
                named.append("%s (%s)" % (m.group(1).strip(), _THROUGH[how]))
                seen.add(how)
        means += [how for rx, how in _CALLS if rx.search(text or "")]
        named += [m.group(1) for m in _DLL_NAMED.finditer(text or "")]
        for m in _IMPORTED.finditer(text or ""):
            if m.group(1) in _NATIVE:
                means.append(_NATIVE[m.group(1)])
            elif m.group(1) in MAKER_PACKAGES:
                named.append("the maker's package %s" % m.group(1))
    out = named + [_THROUGH.get(how, how) for how in means if how not in seen]
    return list(dict.fromkeys(out))[:8]


# ------------------------------------------------------------------ a driver, read


@dataclass
class Candidate:
    """A driver file found for the model, with its own text once fetched."""
    library: str           # PyMeasure, or the repository's name
    repo: str              # owner/name ("" for a file on this computer)
    commit: str
    path: str              # in the repository, or the file's path on this computer
    license: str
    size: int = 0
    lib: Optional[Library] = None
    ref: str = ""          # the class the collection or the person names
    why: str = ""          # how Kelvin came to it, for the model and the person
    files: Any = field(default_factory=dict)   # the repository's (or folder's) files {path: size}, for its package
    text: str = ""
    guessed: bool = False  # one of the biggest files of a repository or folder, nothing in its name fitting


def _driver(c: Candidate, files: List[Tuple[str, str]], left: List[str]) -> Dict[str, Any]:
    return {"library": c.library, "repo": c.repo, "commit": c.commit, "path": c.path, "license": c.license,
            "url": page(c.repo, c.commit, c.path) if c.repo else "", "ref": c.ref, "why": c.why,
            "sends": c.lib.sends if c.lib is not None else "",
            "files": [{"path": p, "text": t} for p, t in files], "left": left, "loads": loads([t for _, t in files])}


def assemble(gh: GitHub, c: Candidate) -> Dict[str, Any]:
    """The driver with the files of its own package it is built on (related, two imports deep), up to BUDGET
    characters and MAX_FILES fetched; what didn't fit or couldn't be fetched is named in `left`."""
    files = [(c.path, c.text[:BUDGET])]
    used, left, seen = len(files[0][1]), [], {c.path}
    if c.path.lower().endswith(".py"):
        queue = [(p, 1) for p in related(c.path, c.text, c.files, c.lib)]
    else:   # a C++ device adapter: the other files of its folder, headers first
        folder = c.path.rsplit("/", 1)[0] + "/" if "/" in c.path else ""
        queue = [(p, 2) for p in sorted((p for p in c.files if p != c.path and p.startswith(folder)
                                         and "/" not in p[len(folder):]),
                                        key=lambda p: (not p.lower().endswith((".h", ".hpp")), p))]
    while queue:
        path, depth = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        size = int(c.files.get(path) or 0)
        if used + size > BUDGET:
            left.append(path)
            continue
        try:
            text = gh.text(c.repo, c.commit, path, size)
        except (DriverError, OSError) as e:
            left.append("%s (%s)" % (path, _first_line(e)))
            continue
        files.append((path, text))
        used += len(text)
        if depth < 2:
            queue += [(p, depth + 1) for p in related(path, text, c.files, c.lib) if p not in seen]
    if len(c.text) > BUDGET:
        left.insert(0, "the rest of %s, after %d characters" % (c.path, BUDGET))
    return _driver(c, files, left)


class _Disk:
    """The files under a folder on this computer, looked at one by one as a driver's imports name them (never a walk
    of the folder)."""

    def __init__(self, root: Path):
        self.root = root

    def __contains__(self, rel: object) -> bool:
        try:
            return (self.root / str(rel)).is_file()
        except OSError:
            return False

    def __iter__(self) -> Any:
        return iter(())

    def get(self, rel: str, default: int = 0) -> int:
        try:
            return (self.root / rel).stat().st_size
        except OSError:
            return default


def local(path: Any, refuse: Optional[Callable[[str], Optional[str]]] = None) -> Dict[str, Any]:
    """A driver on this computer: the file, and the files of its own package it is built on (imports that stay under
    the folder above it), each refused as the file tools refuse a file (refuse: why not, or None) and never one that
    may hold sign-ins."""
    from .folderfiles import secret
    file = Path(str(path)).expanduser()
    if not file.is_file():
        raise DriverError("there is no file %s" % path)
    if file.suffix.lower() not in SOURCES:
        raise DriverError("%s is not driver code (%s)" % (file.name, " ".join(SOURCES)))
    why = refuse(str(file)) if refuse is not None else None
    if why or secret(file):
        raise DriverError(why or "%s may hold sign-ins, keys or tokens" % file)
    if file.stat().st_size > MAX_FILE:
        raise DriverError("%s is larger than %d KB" % (file.name, MAX_FILE // 1000))
    real = file.resolve()
    root = real.parent.parent if real.parent.parent != real.parent else real.parent
    disk = _Disk(root)
    rel = real.relative_to(root).as_posix()
    text = file.read_text(encoding="utf-8", errors="replace")
    c = Candidate("", "", "", str(file), "", len(text), why="the person gave it", files=disk, text=text)
    out = [(str(file), text[:BUDGET])]
    used, left, seen = len(out[0][1]), [], {rel}
    queue = [(p, 1) for p in related(rel, text, disk, None)]
    while queue and len(out) < MAX_FILES:
        sub, depth = queue.pop(0)
        if sub in seen:
            continue
        seen.add(sub)
        p = root / sub
        if secret(p) or (refuse is not None and refuse(str(p))) or disk.get(sub) > MAX_FILE \
                or used + disk.get(sub) > BUDGET:
            left.append(str(p))
            continue
        more = p.read_text(encoding="utf-8", errors="replace")
        out.append((str(p), more))
        used += len(more)
        if depth < 2:
            queue += [(q, depth + 1) for q in related(sub, more, disk, None) if q not in seen]
    return _driver(c, out, left)


# ------------------------------------------------------------------ finding a driver


def _resolve_ref(gh: GitHub, repo: str, commit: str, files: Dict[str, int], ref: str,
                 lib: Optional[Library], under: str = "") -> Optional[str]:
    """The file a dotted name is defined in (pymeasure.instruments.keithley.Keithley2000: .../keithley2000.py, in any
    case): the longest module path that is a file; a package's __init__.py is read for the module it takes the name
    from (from .serial import FW)."""
    parts = [p for p in ref.split(".") if p]
    lower = {p.lower(): p for p in files}
    if lib is not None:
        parts = parts[1:]
    for k in range(len(parts), 0, -1):
        mod = "/".join(parts[:k]).lower()
        if lib is not None:
            hits = [lower[c] for c in ((lib.root + mod + ".py").lower(), (lib.root + mod + "/__init__.py").lower())
                    if c in lower]
        else:
            hits = sorted((p for low, p in lower.items() if p.startswith(under) and
                           (low == mod + ".py" or low.endswith("/" + mod + ".py") or low == mod + "/__init__.py"
                            or low.endswith("/" + mod + "/__init__.py"))), key=len)
        if not hits:
            continue
        hit = hits[0]
        if not hit.endswith("__init__.py") or k == len(parts):
            return hit
        name = parts[k]   # the package takes it from one of its modules
        try:
            init = gh.text(repo, commit, hit, files.get(hit, 0))
        except (DriverError, OSError):
            return None
        for level, module, names in _imports(init):
            if any(name in (n, local) for n, local in names):
                found = _targets(level, module, names, hit, files, lib)
                return found[0] if found else None
        return None
    return None


def _from_item(gh: GitHub, item: Dict[str, Any], model: str, why: str) -> List[Candidate]:
    """The driver files one software item of the collection names (or an address or a dotted name a person gave):
    a file as it is; in a folder or a repository, the file its dotted name is defined in, else those whose names fit
    the model."""
    url, ref = str(item.get("url") or ""), str(item.get("ref") or "")
    addr = address(url)
    lib = library_for(addr.repo) if addr is not None else library_of(ref)
    if addr is None and lib is None:
        return []   # not on GitHub (PyPI, the maker's site), nor in one of the libraries
    short = ref.split(".")[-1] if ref else ""
    if lib is not None:   # its file list, kept a week
        tree = gh.tree(lib.repo)
        repo, commit, files, license, label = tree["repo"], tree["commit"], tree["files"], lib.license, lib.name
        if addr is not None and addr.kind == "file":
            if addr.path in files:
                return [Candidate(label, repo, commit, addr.path, license, files[addr.path], lib, short, why, files)]
            # not in the list: moved since, or the list is older than the file
            return [Candidate(label, repo, gh.commit(lib.repo, addr.ref), addr.path, license, 0, lib, short, why)]
        if addr is not None and addr.kind == "folder":
            files = {p: s for p, s in files.items() if p.startswith(addr.path + "/")}
    else:   # another repository: named by its own name (after a rename)
        info = gh.about(addr.repo)
        repo, license, label = info["repo"], info["license"], info["repo"]
        commit = gh.commit(repo, addr.ref or info["branch"])
        if addr.kind == "file":
            return [Candidate(label, repo, commit, addr.path, license, 0, None, short, why)]
        files = gh.listing(repo, commit, addr.path) if addr.kind == "folder" else gh.tree(repo, commit)["files"]
    if ref and "." in ref:   # its dotted name, among the files
        hit = _resolve_ref(gh, repo, commit, files, ref, lib, addr.path + "/" if addr is not None and addr.path else "")
        return [Candidate(label, repo, commit, hit, license, files.get(hit, 0), lib, short, why, files)] if hit else []

    def plain(p: str) -> bool:   # a module of the driver: not a test, an example, the docs, the packaging
        parts = p.split("/")
        name = parts[-1].lower()
        return files[p] <= MAX_FILE and not name.startswith("test") and name not in ("setup.py", "conftest.py",
                                                                                     "__init__.py") \
            and not any(x.lower() in SKIP_FOLDERS for x in parts[:-1])

    def rank(p: str) -> Tuple[int, int, int, str]:
        stem = Path(p).stem
        return -(bool(short) and compact(stem) == compact(short)), -model_fit(stem, model), -files[p], p
    ranked = sorted((p for p in files if plain(p)), key=rank)
    chosen = [p for p in ranked if rank(p)[0] or rank(p)[1]][:MAX_TRIED]
    guessed = not chosen and addr is not None and (lib is None or addr.kind == "folder")
    if guessed:   # a driver's own repository or folder: its biggest files, and their text decides
        chosen = ranked[:MAX_TRIED]
    return [Candidate(label, repo, commit, p, license, files[p], lib, short, why, files, guessed=guessed)
            for p in chosen]


def _read(gh: GitHub, cands: List[Candidate], problems: List[str]) -> List[Candidate]:
    """Each candidate with its own file's text; one that can't be fetched is said in problems and left out."""
    out = []
    for c in cands:
        try:
            c.text = gh.text(c.repo, c.commit, c.path, c.size)
        except (DriverError, OSError, ValueError) as e:
            problems.append("%s: %s" % (c.path, _first_line(e)))
            continue
        out.append(c)
    return out


def _with_package(gh: GitHub, c: Candidate) -> Candidate:
    """A Python driver that imports from its own package gets its repository's file list, if Kelvin hasn't it yet."""
    if c.files or not c.path.lower().endswith(".py") or not any(level for level, _, _ in _imports(c.text)):
        return c
    try:
        c.files = gh.tree(c.repo, c.commit)["files"]
    except (DriverError, OSError, ValueError):
        pass
    return c


def from_collection(gh: GitHub, entry: Optional[Dict[str, Any]], model: str, problems: List[str]) -> List[Candidate]:
    """The drivers the instrument collection lists for the model that are on GitHub (or in one of LIBRARIES), each
    with its own text: those that name the model first, else in the collection's order."""
    out: List[Candidate] = []
    name = str((entry or {}).get("name") or model)
    for item in (entry or {}).get("software") or []:
        if not isinstance(item, dict) or item.get("kind") not in KINDS or len(out) >= MAX_TRIED:
            continue
        try:
            out += _from_item(gh, item, model, "the instrument collection lists it for the %s" % name)
        except (DriverError, OSError, ValueError) as e:
            problems.append("%s: %s" % (item.get("name") or item.get("url"), _first_line(e)))
    read = [c for c in _read(gh, out[:MAX_TRIED], problems) if not c.guessed or names_it(c.text, model)]
    return sorted(read, key=lambda c: not names_it(c.text, model))


def from_libraries(gh: GitHub, maker: str, model: str, problems: List[str]) -> List[Candidate]:
    """Drivers in the open libraries whose paths fit the maker and the model (path_score), best first; of the best
    few, those whose text names the model or its series."""
    found = []
    for lib in LIBRARIES:
        try:
            tree = gh.tree(lib.repo)
        except (DriverError, OSError, ValueError) as e:
            problems.append("%s: %s" % (lib.name, _first_line(e)))
            continue
        for path, size in tree["files"].items():
            score = path_score(path[len(lib.folder):], maker, model) \
                if path.startswith(lib.folder) and path.endswith(".py") and size <= MAX_FILE else 0
            if score:
                found.append((score, path, lib, tree))
    found.sort(key=lambda f: (-f[0], len(f[1]), f[1]))
    cands = [Candidate(lib.name, tree["repo"], tree["commit"], path, lib.license, tree["files"][path], lib, "",
                       "%s's drivers, where its name fits the %s" % (lib.name, model), tree["files"])
             for _, path, lib, tree in found[:MAX_TRIED]]
    return [c for c in _read(gh, cands, problems) if names_it(c.text, model)]


def from_search(gh: GitHub, search: Optional[Search], maker: str, model: str, problems: List[str],
                log: Log = _quiet) -> List[Candidate]:
    """What a web search finds for a driver on GitHub: GitHub's addresses only, each file, folder or repository
    matched against the model; those whose text names the model."""
    if search is None:
        return []
    query = "%s %s python driver github" % (maker, model)
    try:
        urls = search(query)
    except Exception as e:   # the model service's search: said in the log, the card goes on without it
        log("web search for %r: %s" % (query, _first_line(e)))
        return []
    out: List[Candidate] = []
    for url in [u for u in urls or [] if address(u) is not None][:2]:
        try:
            out += _from_item(gh, {"url": url}, model, "a web search found it")
        except (DriverError, OSError, ValueError) as e:
            problems.append("%s: %s" % (url, _first_line(e)))
    return [c for c in _read(gh, out[:MAX_TRIED], problems) if names_it(c.text, model)]


def find(gh: GitHub, maker: str, model: str, entry: Optional[Dict[str, Any]] = None, search: Optional[Search] = None,
         log: Log = _quiet) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """The open driver to write the model's card from (the collection's, the libraries', a web search's: the first
    that sends text), with the files of its package; one that reaches the instrument through the maker's library only
    when no other was found (its `loads` says what it loads). And what went wrong on the way."""
    problems: List[str] = []
    library_one: Optional[Dict[str, Any]] = None
    for look in (lambda: from_collection(gh, entry, model, problems),
                 lambda: from_libraries(gh, maker, model, problems),
                 lambda: from_search(gh, search, maker, model, problems, log)):
        for c in look():
            driver = assemble(gh, _with_package(gh, c))
            if not driver["loads"]:
                log("driver for the %s %s: %s" % (maker, model, describe(driver)))
                return driver, problems
            library_one = library_one or driver
    return library_one, problems


def given(source: str, gh: GitHub, model: str) -> Dict[str, Any]:
    """The driver a person named: a GitHub address (a file; a folder or a repository, matched against the model), a
    dotted name in one of LIBRARIES, or a file on this computer."""
    source = (source or "").strip().strip('"')
    if re.match(r"^https?://", source, re.I):
        if address(source) is None:
            host = (urllib.parse.urlsplit(source).hostname or "").lower()
            if host in GIVEN_ON and source.lower().startswith("https://"):
                raise DriverError("%s is not a file, folder or repository on GitHub" % source)
            raise DriverError("Kelvin reads drivers from GitHub only (https://github.com or "
                              "raw.githubusercontent.com), not %s" % source)
        cands = _from_item(gh, {"url": source}, model, "the person gave it")
    elif not on_this_computer(source):
        cands = _from_item(gh, {"ref": source}, model, "the person named it")
    else:
        return local(source)
    problems: List[str] = []
    read = sorted(_read(gh, cands, problems), key=lambda c: not names_it(c.text, model))
    if not read:
        raise DriverError("no driver file for the %s there%s" % (model, " (%s)" % problems[0] if problems else ""))
    return assemble(gh, _with_package(gh, read[0]))


# ------------------------------------------------------------------ in words


def short(driver: Dict[str, Any]) -> str:
    """repository@commit, its first 12 digits."""
    return "%s@%s" % (driver["repo"], driver["commit"][:12]) if driver.get("repo") else ""


def _named(driver: Dict[str, Any]) -> str:
    """"PyMeasure driver" for one of LIBRARIES; "driver" for a repository named by itself."""
    library = driver.get("library") or ""
    return "%s driver" % library if library and library != driver.get("repo") else "driver"


def describe(driver: Dict[str, Any]) -> str:
    """The driver in a few words: the PyMeasure driver pymeasure/instruments/keithley/keithley2000.py
    (pymeasure/pymeasure@f43e2b32b54b, MIT license); a file on this computer by its path."""
    if not driver.get("repo"):
        return "the driver %s" % driver["path"]
    return "the %s %s (%s, %s license)" % (_named(driver), driver["path"], short(driver),
                                            driver.get("license") or "unknown")


def source_words(driver: Dict[str, Any]) -> str:
    """A card's manual field for a card written from a driver: library, repository, path, commit and license."""
    if not driver.get("repo"):
        return "the driver %s on this computer (written from its code, not the maker's manual)" % driver["path"]
    return ("%s %s, github.com/%s at commit %s, %s license (written from its code, not the maker's manual)"
            % (_named(driver)[0].upper() + _named(driver)[1:], driver["path"], driver["repo"], driver["commit"],
               driver.get("license") or "unknown"))


def code(driver: Dict[str, Any]) -> str:
    """The driver's files as the model reads them, each under its path, the driver first."""
    parts = ["=== %s%s ===\n%s" % (f["path"], " (the driver)" if i == 0 else " (its package)", f["text"].strip())
             for i, f in enumerate(driver["files"])]
    if driver.get("left"):
        parts.append("(Left out, over the length limit or not fetched: %s)" % "; ".join(driver["left"][:20]))
    return "\n\n".join(parts)


def about(driver: Dict[str, Any], model: str) -> str:
    """Which driver the card is written from, for the model ("Write it from" this): where it is, why Kelvin took it,
    the class, and how its library sends commands."""
    if driver.get("repo"):
        where = "the %s %s, in github.com/%s at commit %s (%s license)" % (
            _named(driver), driver["path"], driver["repo"], driver["commit"][:12], driver.get("license") or "unknown")
    else:
        where = "%s, a file on this computer" % driver["path"]
    lines = ["%s: %s." % (where, driver.get("why") or "")]
    if driver.get("ref"):
        lines.append("The class for this instrument is %s." % driver["ref"])
    if not names_it("\n".join(f["text"] for f in driver["files"]), model):
        lines.append("Its code does not name the %s: keep only what it shows holds for this model, and say so." % model)
    if driver.get("sends"):
        lines.append("How %s's drivers send commands: %s." % (driver["library"], driver["sends"]))
    return " ".join(lines)


def _cut(text: Any, most: int = 70) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= most else text[:most].rsplit(" ", 1)[0] + " …"


def listed(entry: Dict[str, Any], drivers: int = 4, examples: int = 2) -> Dict[str, List[str]]:
    """What the instrument collection lists for an instrument beside its manuals, briefly: its drivers and SDKs
    (kind, name, dotted name, address: "python-driver PyMeasure: pymeasure.instruments.srs.SR830, https://...") and
    its examples (title, address)."""
    soft = [s for s in entry.get("software") or [] if isinstance(s, dict) and s.get("kind") in LISTED
            and str(s.get("url") or "").startswith("https://")]
    out = {"drivers": ["%s %s: %s%s" % (s["kind"], _cut(s.get("name")), "%s, " % s["ref"] if s.get("ref") else "",
                                          s["url"]) for s in soft[:drivers]],
           "examples": ["%s: %s" % (_cut(e.get("title")), e["url"]) for e in entry.get("examples") or []
                        if isinstance(e, dict) and str(e.get("url") or "").startswith("https://")][:examples]}
    if len(soft) > drivers:
        out["drivers"].append("%d more" % (len(soft) - drivers))
    return out


def entry_named(catalog: Any, maker: str, model: str) -> Optional[Dict[str, Any]]:
    """The collection's entry for an instrument a person named (no identity reply): one whose model, name or the
    models it covers name the model, by the same maker."""
    if catalog is None or not hasattr(catalog, "index"):
        return None
    from .autocard import names_model
    names = maker_names(maker)
    for entry in (catalog.index() or {}).get("instruments") or []:
        if not isinstance(entry, dict):
            continue
        text = " ".join([str(entry.get("model") or ""), str(entry.get("name") or "")] +
                        [str(c) for c in entry.get("covers") or []])
        theirs = compact("%s %s" % (entry.get("maker") or "", entry.get("id") or ""))
        if names_model(text, model) and any(len(n) >= 3 and n in theirs for n in names):
            return entry
    return None
