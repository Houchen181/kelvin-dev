"""The instrument collection: cards, the makers' manuals and experiments from Kelvin's public repository, kept on
this computer so they work offline.

    ~/Kelvin/collection/
        state.json            which commit of the repository the copy here is, and when Kelvin last looked
        <commit>/index.json   the collection's index at that commit
        <commit>/...          its cards, experiments and lab configs, in the repository's own folders

- Public: the collection is read from Kelvin's public repository (REPO, where published builds update from too),
  never the private source. Kelvin asks GitHub for the repository first: one that says it is private is refused, and
  a renamed one is followed to its own name.
- Pinned: Kelvin takes the newest commit of the repository's default branch, then index.json and every file from
  that commit (raw.githubusercontent.com), never from a branch that can move meanwhile. Each file must have the
  SHA-256 that index.json gives it, when it arrives and every time it is read. Files that did not change come from
  the copy before, and a new copy is used only once it is complete: state.json names it last.
- Offline: everything is read from the copy here. Only sync() goes to the network, and Kelvin calls it only while
  the collection is switched on in Settings. Switched off, Kelvin leaves the copy alone and uses its own cards.
- Cards (virtual_lab.lab_registry): a collection card replaces Kelvin's own card with the same id (a fix, or a lab's
  check on hardware), except a copy of one of Kelvin's own versions of that card (data/card_versions.json): that is
  the collection catching up with a newer Kelvin. Nothing replaces a card a person reviewed on this computer.
  Collection cards keep their status, so a draft stays simulation-only on real hardware.
- Instruments without a card: a scan that gets an identity reply no card matches looks it up here, to name the
  instrument and to take its manual from the address the collection gives (discovery.py, autocard.py).
- Experiments: what to measure and how, with plans written for one of the collection's lab configs. adapt() maps
  their instrument names to a lab's own, for the agent's experiment_guide."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ._version import __version__
from .cards import Card, parse_card
from .profiles import _first_line
from .updater import REPOS
from .yamlio import load_yaml

log = logging.getLogger("kelvin")
FOLDER = "collection"
REPO = REPOS["stable"]   # Kelvin's public repository: published releases and the instrument collection
API = "https://api.github.com/repos/%s"
COMMIT = "https://api.github.com/repos/%s/commits/%s"
RAW = "https://raw.githubusercontent.com/%s/%s/%s"
DATA = Path(__file__).parent / "data"
MAX_BYTES = 16 * 1024 * 1024
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# the only files index.json may point to: YAML files in the collection's own folders, nothing outside the copy
FILE = re.compile(r"^(?:instruments|experiments|labs)(?:/[a-z0-9][a-z0-9._-]*)+\.yaml$")
KEY = re.compile(r"^(\s*)([A-Za-z_][\w-]*)\.([A-Za-z_]\w*)")   # instrument.parameter at the start of a condition
QUIET = {"and", "the", "for", "with", "from", "how", "what", "measure", "measurement", "experiment", "sample"}
_LOCK = threading.RLock()   # a sync switches copies while nothing reads them
Fetch = Callable[[str], bytes]


class CollectionError(Exception):
    pass


def http_get(url: str) -> bytes:
    headers = {"User-Agent": "Kelvin/%s (lab assistant)" % __version__}
    if url.startswith("https://api.github.com/"):   # the repository as JSON; of a commit, just its SHA
        headers["Accept"] = "application/vnd.github.sha" if "/commits/" in url else "application/vnd.github+json"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise CollectionError("%s is larger than %d MB" % (url, MAX_BYTES >> 20))
    return data


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def text_sha256(data: bytes) -> str:
    """A text file's SHA-256 with LF line ends, however it was checked out."""
    return sha256(data.replace(b"\r\n", b"\n"))


@lru_cache(maxsize=1)
def own_versions() -> Dict[str, frozenset]:
    """Every version of Kelvin's own cards, by id: those it brought before (data/card_versions.json) and this
    Kelvin's."""
    try:
        listed = json.loads((DATA / "card_versions.json").read_text(encoding="utf-8")).get("cards") or {}
    except (OSError, ValueError):
        listed = {}
    out = {str(k): set(v) for k, v in listed.items()}
    for path in (DATA / "cards").rglob("*.yaml"):
        data = path.read_bytes()
        m = re.search(rb"(?m)^id:\s*['\"]?([^\s'\"#]+)", data)
        if m:
            out.setdefault(m.group(1).decode(), set()).add(text_sha256(data))
    return {k: frozenset(v) for k, v in out.items()}


def public_repository(answer: bytes) -> Tuple[str, str]:
    """The repository's own name and default branch from GitHub's answer. Only a public repository is read."""
    try:
        repo = json.loads(answer.decode("utf-8"))
    except ValueError:
        repo = None
    name = str(repo.get("full_name") or "") if isinstance(repo, dict) else ""
    branch = str(repo.get("default_branch") or "main") if isinstance(repo, dict) else ""
    if not re.match(r"^[\w.-]+/[\w.-]+$", name) or not re.match(r"^[\w.-]+(?:/[\w.-]+)*$", branch):
        raise CollectionError("GitHub did not describe the repository")
    if repo.get("private") is not False:
        raise CollectionError("%s is not public, and Kelvin reads the collection only from a public repository" % name)
    return name, branch


def latest_commit(answer: bytes) -> str:
    """The commit's SHA from GitHub's answer: the SHA alone, or the commit as JSON."""
    text = answer.decode("utf-8", errors="replace").strip()
    if not SHA1.match(text):
        try:
            text = str(json.loads(text).get("sha") or "")
        except (ValueError, AttributeError):
            text = ""
    if not SHA1.match(text):
        raise CollectionError("GitHub did not say which commit is the newest")
    return text


def read_index(data: bytes) -> Dict[str, Any]:
    try:
        index = json.loads(data.decode("utf-8"))
    except ValueError:
        raise CollectionError("index.json is not JSON")
    if not isinstance(index, dict) or index.get("schema") != 1 or not isinstance(index.get("instruments"), list):
        raise CollectionError("index.json is not a schema 1 collection index")
    return index


def files_of(index: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Every file of the collection Kelvin keeps a copy of, with its SHA-256: the cards, experiments and labs."""
    listed = [(i["card"].get("path"), i["card"].get("sha256")) for i in index.get("instruments") or []
              if isinstance(i, dict) and isinstance(i.get("card"), dict)]
    listed += [(x.get("path"), x.get("sha256")) for key in ("experiments", "labs") for x in index.get(key) or []
               if isinstance(x, dict)]
    for path, digest in listed:
        if not FILE.match(str(path)) or not SHA256.match(str(digest)):
            raise CollectionError("index.json lists %r, which is not a file of the collection" % (path,))
    return [(str(p), str(d)) for p, d in listed]


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _matching(path: Optional[Path], digest: str) -> Optional[bytes]:
    """The file's bytes if it is there and has this SHA-256."""
    try:
        data = path.read_bytes() if path is not None else None
    except OSError:
        return None
    return data if data is not None and sha256(data) == digest else None


def manuals(entry: Dict[str, Any]) -> List[str]:
    """The addresses of an instrument's public manuals (not those behind a sign-in or only for owners)."""
    return [str(m["url"]) for m in entry.get("manuals") or [] if isinstance(m, dict)
            and str(m.get("url") or "").startswith("https://") and m.get("access", "public") == "public"]


class Collection:
    """The copy of the collection on this computer, read offline; sync() brings it up to date."""

    def __init__(self, home: Any, repo: str = REPO):
        self.dir = Path(home) / FOLDER
        self.repo = repo
        self._index: Tuple[Optional[str], Dict[str, Any]] = (None, {})
        self._syncing = threading.Lock()

    # ------------------------------------------------------------------ the copy here

    def state(self) -> Dict[str, Any]:
        try:
            data = json.loads((self.dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _folder(self, state: Dict[str, Any]) -> Optional[Path]:
        commit = str(state.get("commit") or "")
        if not SHA1.match(commit) or state.get("source") != self.repo:   # nothing yet, or another repository's
            return None
        return self.dir / commit[:12]

    def index(self) -> Dict[str, Any]:
        """The index of the copy here, checked against the SHA-256 state.json keeps of it ({} without a copy)."""
        with _LOCK:
            state = self.state()
            folder = self._folder(state)
            if folder is None:
                return {}
            if self._index[0] != state["commit"]:
                data = _matching(folder / "index.json", str(state.get("index_sha256")))
                try:
                    index = read_index(data) if data is not None else None
                except CollectionError:
                    index = None
                if index is None:
                    return {}
                self._index = (state["commit"], index)
            return self._index[1]

    def read(self, path: str, digest: str) -> Optional[bytes]:
        """A file of the copy here, if it is there and has the SHA-256 the index gives it."""
        with _LOCK:
            folder = self._folder(self.state())
            if folder is None or not FILE.match(str(path)):
                return None
            return _matching(folder / path, str(digest))

    def _cards(self) -> Tuple[List[Card], List[str], List[str]]:
        """(the collection's cards Kelvin uses, the ids among them that replace Kelvin's own, why others are left out)"""
        own = own_versions()
        cards: List[Card] = []
        newer: List[str] = []
        left: List[str] = []
        with _LOCK:
            folder = self._folder(self.state())
            for entry in self.index().get("instruments") or []:
                card = entry.get("card") if isinstance(entry, dict) else None
                if not isinstance(card, dict) or folder is None:
                    continue
                cid = str(entry.get("id"))
                data = self.read(str(card.get("path")), str(card.get("sha256")))
                if data is None:
                    left.append("%s: its copy here is missing or does not match the index" % cid)
                    continue
                if text_sha256(data) in own.get(cid, ()):
                    continue   # one of Kelvin's own versions: this Kelvin's card stays
                try:
                    parsed = parse_card(load_yaml(data), str(folder / card["path"]))
                except Exception as e:   # a card written for a newer Kelvin, say
                    left.append("%s: %s" % (cid, _first_line(e)))
                    continue
                if parsed.id != cid:
                    left.append("%s: the card in its folder has the id %s" % (cid, parsed.id))
                    continue
                cards.append(parsed)
                if cid in own:
                    newer.append(cid)
        return cards, newer, left

    def cards(self) -> List[Card]:
        """The collection's cards for Kelvin to use, each checked against the index. A card that is a copy of one of
        Kelvin's own versions is left out, so Kelvin's own card stays."""
        return self._cards()[0]

    def instrument(self, iid: str) -> Optional[Dict[str, Any]]:
        return next((i for i in self.index().get("instruments") or [] if isinstance(i, dict) and i.get("id") == iid),
                    None)

    def instrument_for(self, idn: str) -> Optional[Dict[str, Any]]:
        """The collection's entry for an instrument that answers `idn`; the most specific pattern wins, as for cards."""
        hits = []
        for entry in self.index().get("instruments") or []:
            pattern = str(((entry.get("identify") if isinstance(entry, dict) else None) or {}).get("idn") or "")
            try:
                if pattern and re.search(pattern, idn or "", re.I):
                    hits.append((len(pattern), entry))
            except re.error:
                continue
        return max(hits, key=lambda h: h[0])[1] if hits else None

    def experiments(self, query: str = "") -> List[Dict[str, Any]]:
        """The collection's experiments, those that fit `query` best first (all of them for no query)."""
        words = [w for w in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(w) > 1 and w not in QUIET]

        def has(hay: set, w: str) -> bool:
            return w in hay or (len(w) > 3 and any(h.startswith(w) for h in hay))
        scored = []
        for e in self.index().get("experiments") or []:
            if not isinstance(e, dict):
                continue
            roles = e.get("roles") if isinstance(e.get("roles"), dict) else {}
            title = set(re.findall(r"[a-z0-9]+", ("%s %s" % (e.get("id") or "", e.get("title") or "")).lower()))
            hay = title | set(re.findall(r"[a-z0-9]+", " ".join(
                [str(e.get("summary") or "")] + list(roles) +
                [" ".join(map(str, list((r or {}).get("kind") or []) + list((r or {}).get("suggested") or [])))
                 for r in roles.values()]).lower()))
            score = sum(2 if has(title, w) else 1 for w in words if has(hay, w))   # its name counts most
            if score or not words:
                scored.append((-score, len(scored), e))
        return [e for _, _, e in sorted(scored, key=lambda s: s[:2])]

    def experiment(self, eid: str) -> Dict[str, Any]:
        """One experiment as the collection writes it (roles, setup, procedure, plans...), with `written_for`: the
        instruments of the lab config its plans were written for, {name: card id}."""
        entry = next((e for e in self.index().get("experiments") or [] if isinstance(e, dict) and e.get("id") == eid),
                     None)
        if entry is None:
            raise KeyError("the collection has no experiment %r" % eid)
        data = self.read(str(entry.get("path")), str(entry.get("sha256")))
        doc = load_yaml(data) if data is not None else None
        if not isinstance(doc, dict):
            raise KeyError("the copy of %s here is missing or does not match the index; Kelvin fetches it again at its "
                           "next update" % eid)
        lab = next((x for x in self.index().get("labs") or [] if isinstance(x, dict) and x.get("path") == doc.get("lab")),
                   None)
        return dict(doc, written_for={str(k): str(v) for k, v in ((lab or {}).get("instruments") or {}).items()})

    def status(self) -> Dict[str, Any]:
        """Where the copy here is from and what it holds, for Settings: its cards, and those Kelvin uses (`newer`:
        the ones that replace Kelvin's own)."""
        state = self.state()
        index = self.index()
        cards, newer, left = self._cards() if index else ([], [], [])
        return {"repository": state.get("repository") or self.repo, "commit": state.get("commit") if index else None,
                "synced": state.get("synced") if index else None, "checked": state.get("checked"),
                "error": state.get("error"), "instruments": len(index.get("instruments") or []),
                "cards": sum(1 for i in index.get("instruments") or [] if isinstance(i, dict)
                             and isinstance(i.get("card"), dict)),
                "used": [c.id for c in cards], "newer": newer, "left_out": left,
                "experiments": len(index.get("experiments") or [])}

    # ------------------------------------------------------------------ bringing it up to date (the network)

    def sync(self, fetch: Optional[Fetch] = None) -> Dict[str, Any]:
        """Bring the copy here up to the collection's newest commit. The only network access here: Kelvin calls it
        only while the collection is switched on. A problem goes into the status (never raised), and the copy before
        stays in use."""
        fetch = fetch or http_get
        with self._syncing:
            state = self.state()
            now = time.time()
            try:
                name, branch = public_repository(fetch(API % self.repo))   # its own name, if it was renamed
                commit = latest_commit(fetch(COMMIT % (name, branch)))
                folder = self.dir / commit[:12]
                same = commit == state.get("commit") and self._folder(state) == folder
                raw = _matching(folder / "index.json", str(state.get("index_sha256"))) if same else None
                if raw is None:
                    raw = fetch(RAW % (name, commit, "index.json"))
                index = read_index(raw)
                before = self._folder(state)
                fetched = 0
                for path, digest in files_of(index):
                    if _matching(folder / path, digest) is not None:
                        continue
                    data = _matching(before / path, digest) if before is not None else None
                    if data is None:
                        data = fetch(RAW % (name, commit, path))
                        fetched += 1
                        if sha256(data) != digest:
                            raise CollectionError("%s at %s does not match its SHA-256 in index.json" % (path, commit[:12]))
                    _write(folder / path, data)
                _write(folder / "index.json", raw)
                with _LOCK:   # the switch: from now on the new copy is read
                    _write(self.dir / "state.json", json.dumps({
                        "source": self.repo, "repository": name, "commit": commit, "index_sha256": sha256(raw),
                        "synced": state.get("synced") if same else now, "checked": now, "error": None},
                        indent=1).encode("utf-8"))
                    for other in self.dir.iterdir():   # the copies before
                        if other.is_dir() and other != folder:
                            shutil.rmtree(other, ignore_errors=True)
                if not same:
                    log.info("instrument collection: now at %s (%d files fetched)", commit[:12], fetched)
            except Exception as e:   # offline, rate-limited, a file that does not match: the copy here stays
                problem = "could not update the instrument collection from %s: %s" % (self.repo, _first_line(e))
                log.warning(problem)
                _write(self.dir / "state.json", json.dumps(dict(state, checked=now, error=problem), indent=1).encode())
        return self.status()


# ------------------------------------------------------------------ experiments, for a lab's own instruments

def _dict(x: Any) -> Dict[str, Any]:
    return x if isinstance(x, dict) else {}


def plan_keys(plan: Dict[str, Any]) -> List[str]:
    """Every instrument.parameter a plan sets, sweeps, waits for or reads, in order."""
    keys = [str(k) for section in ("setup", "sweep", "after", "on_abort") for k in _dict(plan.get(section))]
    keys += [str(_dict(axis.get("stabilize")).get("read") or "") for axis in _dict(plan.get("sweep")).values()
             if isinstance(axis, dict)]
    keys += [str(k) for k in plan.get("measure") or []]
    keys += ["%s.%s" % m.groups()[1:] for m in (KEY.match(str(c)) for c in plan.get("preconditions") or []) if m]
    return [k for k in keys if "." in k]


def plan_names(plan: Dict[str, Any]) -> List[str]:
    """The instruments a plan uses, in the order it names them."""
    return list(dict.fromkeys([k.split(".")[0] for k in plan_keys(plan)] + [str(n) for n in plan.get("requires") or []]))


def rename(plan: Dict[str, Any], names: Dict[str, str]) -> Dict[str, Any]:
    """The plan with its instruments renamed ({gate: smu}): in its settings, sweeps, readings, conditions and requires."""
    def key(k: Any) -> str:
        instr, dot, param = str(k).partition(".")
        return names.get(instr, instr) + dot + param

    out: Dict[str, Any] = {}
    for section, value in plan.items():
        if section in ("setup", "after", "on_abort") and isinstance(value, dict):
            value = {key(k): v for k, v in value.items()}
        elif section == "sweep" and isinstance(value, dict):
            value = {key(k): dict(axis, stabilize=dict(axis["stabilize"], read=key(axis["stabilize"].get("read"))))
                     if isinstance(axis, dict) and isinstance(axis.get("stabilize"), dict) else axis
                     for k, axis in value.items()}
        elif section == "measure" and isinstance(value, list):
            value = [key(k) for k in value]
        elif section == "preconditions" and isinstance(value, list):
            value = [KEY.sub(lambda m: "%s%s.%s" % (m.group(1), names.get(m.group(2), m.group(2)), m.group(3)), str(c),
                             count=1) for c in value]
        elif section == "requires" and isinstance(value, list):
            value = [names.get(str(n), n) for n in value]
        out[section] = value
    return out


def _kinds(role: Any) -> List[str]:
    k = _dict(role).get("kind")
    return [str(x) for x in (k if isinstance(k, list) else [k]) if x]


def _role_of(name: str, card_id: str, kind: str, roles: Dict[str, Any]) -> Optional[str]:
    """The experiment's role an instrument of its lab config plays: by name (lockin_xx plays lockin), by the cards
    the role suggests, or by kind."""
    tests = (lambda role, r: role == name, lambda role, r: role == name.split("_")[0],
             lambda role, r: card_id in (_dict(r).get("suggested") or []), lambda role, r: kind in _kinds(r))
    for test in tests:
        for role, r in roles.items():
            if test(role, r):
                return role
    return None


def adapt(experiment: Dict[str, Any], lab: Dict[str, Card], kind_of: Callable[[str], str]) -> Dict[str, Any]:
    """An experiment's plans for a lab: {names: {plan's instrument: lab's}, missing: plan instruments with none here,
    roles: [{role, kinds, does, optional, here}], plans: [{title, plan, problems}]}.

    `lab` is {name: card}. The plans name the instruments of the lab config they were written for (`written_for`,
    {name: card id}). Each becomes the lab's instrument with the same card, else one of a kind its role takes (a
    card's kind from kind_of(card id)); a lab instrument plays one part, and one with the same name is preferred."""
    roles = _dict(experiment.get("roles"))
    plans = [p for p in experiment.get("plans") or [] if isinstance(p, dict) and isinstance(p.get("plan"), dict)]
    written_for = _dict(experiment.get("written_for"))
    order = list(dict.fromkeys(n for p in plans for n in plan_names(p["plan"])))
    names: Dict[str, str] = {}
    taken: set = set()

    def pick(name: str, fits: Callable[[Card], bool]) -> None:
        free = [n for n, c in lab.items() if n not in taken and fits(c)]
        if free:
            names[name] = name if name in free else free[0]
            taken.add(names[name])

    for name in order:   # the same card first, then a card of a kind its role takes
        if written_for.get(name):
            pick(name, lambda c, cid=written_for[name]: c.id == cid)
    for name in order:
        if name not in names:
            cid = str(written_for.get(name) or "")
            kind = kind_of(cid) if cid else ""
            role = _role_of(name, cid, kind, roles)
            kinds = set(_kinds(roles[role]) if role else []) | ({kind} if kind else set())
            pick(name, lambda c, kinds=kinds: c.kind in kinds)
    adapted = []
    for p in plans:
        plan = rename(p["plan"], names)
        problems = []
        for key in plan_keys(plan):
            instr, _, param = key.partition(".")
            card = lab.get(instr)
            if card is not None and param not in card.params:
                problems.append("%s has no %s (%s has %s)" % (instr, param, card.id, ", ".join(list(card.params)[:16])))
        adapted.append({"title": str(p.get("title") or plan.get("plan") or ""), "plan": plan,
                        "problems": list(dict.fromkeys(problems))})
    here = []
    for role, r in roles.items():
        kinds, suggested = _kinds(r), [str(s) for s in _dict(r).get("suggested") or []]
        here.append({"role": role, "kinds": kinds, "does": str(_dict(r).get("does") or ""),
                     "optional": bool(_dict(r).get("optional")),
                     "here": [n for n, c in lab.items() if c.kind in kinds or c.id in suggested]})
    return {"names": names, "missing": [n for n in order if n not in names], "roles": here, "plans": adapted}
