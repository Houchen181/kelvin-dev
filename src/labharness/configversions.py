"""The lab's config history: every version of the lab config and of the lab's own cards, with who, when and why.

What widens Kelvin's reach over the instruments is written in a few files: the lab config (lab.yaml: limits, rates,
guards, interlocks, watches, auto rules) and the lab's own cards (~/Kelvin/cards: each card's limits and safe
values, and which of Kelvin's cards the lab reviewed). They change in several places: Lab setup and an instrument's
Limits in the portal, the cards, Kelvin adding an instrument or writing a draft card, someone editing a file by hand.
Each change is kept here as a version of that file, so a person sees what changed (the diff, and the changes in
words, the ones that widen Kelvin's reach marked) and can put an earlier version back. A rollback is a change like
any other: the owner's PIN guards it where it guards the change itself, and it is kept as a version too.

    ~/Kelvin/config-history/
      audit.jsonl     one line per version, and per safety change that is no file (auto mode on, Hands on the sample
                      unlocked, the lab's roles): who, when, why, what changed
      texts/<sha256>  each version's text, once

Kelvin only adds to the history. It is a record, not a wall: whoever can open this computer's files can edit the lab
config by hand; Kelvin keeps such an edit as a change made outside Kelvin the next time it looks (when it starts, and
when the lab config or its history is opened in the portal)."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .cards import num
from .yamlio import load_yaml

LAB = "lab.yaml"
REVIEWS = "cards/_reviewed.json"   # Kelvin's own cards a person reviewed for this lab (carddraft.REVIEWS)
OUTSIDE = "outside Kelvin"         # who changed a file that changed without Kelvin (by hand, or copied in)
FOLDER = "config-history"
AUTO_FRACTION = 0.25               # labconfig.AUTO_FRACTION: what a lab config without auto_fraction means


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Unreadable(Exception):
    """A kept file that is there but can't be read now (another program holds it): left for the next look."""


def _read(path: Path) -> Optional[str]:
    """The file's text; None when there is no file."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise Unreadable(str(e)) from None


def subject_title(subject: str, text: Optional[str] = None) -> str:
    """What a kept file is, in words: the lab config, a card (by its id when the text gives one), the reviews."""
    if subject == LAB:
        return "lab config"
    if subject == REVIEWS:
        return "Kelvin's cards reviewed for this lab"
    data = _yaml(text)
    ident = data.get("id") if isinstance(data, dict) else None
    return "card %s" % (ident or subject.split("/", 1)[-1].rsplit(".", 1)[0])


class ConfigVersions:
    """The history of the lab's config files under one Kelvin home (~/Kelvin)."""

    def __init__(self, home: Any):
        self.home = Path(home)
        self.folder = self.home / FOLDER
        self.audit = self.folder / "audit.jsonl"
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ the files kept

    def files(self) -> Dict[str, Path]:
        """The files kept, by subject: the lab config, the lab's cards and its reviews of Kelvin's cards."""
        out = {LAB: self.home / "lab.yaml"}
        cards = self.home / "cards"
        if cards.is_dir():
            for path in sorted(cards.glob("*.yaml")):
                out["cards/" + path.name] = path
            if (cards / "_reviewed.json").exists():
                out[REVIEWS] = cards / "_reviewed.json"
        return out

    def path_of(self, subject: str) -> Path:
        """Where a subject's file is (a card whose file is gone: where it was)."""
        if subject == LAB:
            return self.home / "lab.yaml"
        if not subject.startswith("cards/") or "/" in subject[6:] or "\\" in subject or ".." in subject:
            raise KeyError("no kept file %r" % subject)
        return self.home / subject

    def _there(self, subject: str) -> bool:
        try:
            return self.path_of(subject).exists()
        except KeyError:
            return False

    # ------------------------------------------------------------------ the record

    def entries(self) -> List[Dict[str, Any]]:
        """Every version and safety change, oldest first."""
        try:
            lines = self.audit.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return []
        out = []
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and isinstance(e.get("n"), int) and e.get("type") in ("version", "event"):
                out.append(e)
        return out

    @staticmethod
    def _latest(entries: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        latest: Dict[str, Dict[str, Any]] = {}
        for e in entries:
            if e.get("type") == "version":
                latest[str(e.get("subject"))] = e
        return latest

    def text(self, entry: Optional[Dict[str, Any]]) -> Optional[str]:
        """A version's text (None: the file was gone in that version)."""
        if not entry or not entry.get("sha"):
            return None
        try:
            return (self.folder / "texts" / str(entry["sha"])).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def _keep_text(self, text: str, sha: str) -> None:
        texts = self.folder / "texts"
        path = texts / sha
        if path.exists():
            return
        texts.mkdir(parents=True, exist_ok=True)
        tmp = texts / (sha + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def _append(self, entry: Dict[str, Any]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        with open(self.audit, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------ keeping versions

    def sync(self, by: str, account: Optional[str] = None, role: Optional[str] = None, why: str = "",
             kind: str = "", **extra: Any) -> List[Dict[str, Any]]:
        """Keep every file whose text differs from its latest version as a new version: the change just made, by
        whom (by: as the lab's record names them; account and role: the person signed in). A file seen for the
        first time, outside any change (by OUTSIDE), is kept as Kelvin found it. Returns the new versions."""
        with self._lock:
            entries = self.entries()
            latest = self._latest(entries)
            n = max((e["n"] for e in entries), default=0)
            subjects: Dict[str, Optional[str]] = {}
            for s, p in self.files().items():
                try:
                    subjects[s] = _read(p)
                except Unreadable:
                    continue   # not taken for gone: it is kept the next time it can be read
            for s in latest:   # a file kept before and gone now
                if s not in subjects and not self._there(s):
                    subjects[s] = None
            made = []
            for subject, text in subjects.items():
                was = latest.get(subject)
                sha = _sha(text) if text is not None else None
                if (was is None and text is None) or (was is not None and was.get("sha") == sha):
                    continue
                first = was is None and by == OUTSIDE
                old = self.text(was) if was is not None else None
                found = changes(subject, old, text) if not first else []
                n += 1
                entry: Dict[str, Any] = {
                    "n": n, "type": "version", "subject": subject, "title": subject_title(subject, text or old),
                    "sha": sha, "at": _now(), "by": "Kelvin" if first else by, "account": account or None,
                    "role": role or None, "why": " ".join(str(why or "").split())[:300] or None,
                    "kind": "first" if first else (kind or ("outside" if by == OUTSIDE else "change")),
                    "changes": found, "safety": any(c["safety"] for c in found)}
                if first:
                    entry["why"] = "as Kelvin found it when it began keeping the lab's config history"
                entry.update({k: v for k, v in extra.items() if v is not None})
                if text is not None:
                    self._keep_text(text, sha)
                self._append(entry)
                made.append(entry)
            return made

    def notice(self) -> List[Dict[str, Any]]:
        """Files changed outside Kelvin since their latest version (edited by hand, copied in, or never kept yet),
        each kept as a version of its own before anyone's next change is."""
        return self.sync(OUTSIDE)

    def event(self, what: str, by: str, account: Optional[str] = None, role: Optional[str] = None, why: str = "",
              safety: bool = True, **fields: Any) -> Dict[str, Any]:
        """A safety change that is no file's: auto mode on, Hands on the sample unlocked, the lab's roles."""
        with self._lock:
            n = max((e["n"] for e in self.entries()), default=0) + 1
            entry = {"n": n, "type": "event", "what": what, "at": _now(), "by": by, "account": account or None,
                     "role": role or None, "why": " ".join(str(why or "").split())[:300] or None,
                     "safety": bool(safety)}
            entry.update({k: v for k, v in fields.items() if v is not None})
            self._append(entry)
            return entry

    # ------------------------------------------------------------------ reading it back

    def history(self, subject: str = "", safety_only: bool = False, who: str = "",
                limit: int = 200) -> List[Dict[str, Any]]:
        """Newest first, without the texts. subject: a file ("lab.yaml"), "cards" (every card) or "events" (the safety
        changes that are no file); who: an account name or a by (e.g. "outside Kelvin")."""
        out = []
        for e in reversed(self.entries()):
            if subject == "events" and e["type"] != "event":
                continue
            if subject == "cards" and not str(e.get("subject") or "").startswith("cards/"):
                continue
            if subject not in ("", "events", "cards") and e.get("subject") != subject:
                continue
            if safety_only and not e.get("safety"):
                continue
            if who and who.lower() not in (str(e.get("account") or "").lower(), str(e.get("by") or "").lower()):
                continue
            out.append(e)
            if len(out) >= limit:
                break
        return out

    def get(self, n: int) -> Dict[str, Any]:
        for e in self.entries():
            if e["n"] == n:
                return e
        raise KeyError("no version #%d in the lab's config history" % n)

    def before(self, n: int) -> Optional[Dict[str, Any]]:
        """The version of the same file just before version n (None: n was its first)."""
        entry = self.get(n)
        prior = None
        for e in self.entries():
            if e["n"] >= n:
                break
            if e.get("type") == "version" and e.get("subject") == entry.get("subject"):
                prior = e
        return prior

    def diff(self, n: int, against: Optional[int] = None) -> Dict[str, Any]:
        """Version n against an earlier version of the same file (by default the one just before it): the unified
        diff of the two texts and the changes in words."""
        entry = self.get(n)
        if entry.get("type") != "version":
            raise KeyError("#%d is no version of a file" % n)
        base = self.get(against) if against is not None else self.before(n)
        if base is not None and (base.get("type") != "version" or base.get("subject") != entry.get("subject")):
            raise KeyError("#%d is not a version of the same file" % against)
        old, new = self.text(base), self.text(entry)
        a = "#%d" % base["n"] if base else "(not kept before)"
        lines = list(difflib.unified_diff((old or "").splitlines(True), (new or "").splitlines(True), a,
                                          "#%d" % n, n=2))
        found = entry.get("changes") if against is None else changes(entry["subject"], old, new)
        return {"n": n, "against": base["n"] if base else None, "subject": entry["subject"],
                "title": entry.get("title"), "diff": "".join(line if line.endswith("\n") else line + "\n"
                                                            for line in lines),
                "changes": found or [], "text": new, "gone": new is None}

    def restore(self, n: int) -> Tuple[str, Optional[str]]:
        """What rolling back to version n puts back: its file, and its text (None: the file was gone then)."""
        entry = self.get(n)
        if entry.get("type") != "version":
            raise KeyError("#%d is a safety change, not a version of a file: nothing to put back" % n)
        text = self.text(entry)
        if entry.get("sha") and text is None:
            raise KeyError("the text of #%d is missing from the history" % n)
        return str(entry["subject"]), text

    def now(self, subject: str) -> Optional[str]:
        """The subject's file as it is now (None: not there). Unreadable: another program holds it."""
        return _read(self.path_of(subject))


# ---------------------------------------------------------------------------------- the changes, in words
#
# Each change is {"text": ..., "safety": bool}. safety: it widens Kelvin's reach over the instruments (a limit
# widened or taken out, an interlock or a guard or a watch removed, an instrument a person vouches for, a card
# marked reviewed...). What can't be judged from the files alone counts as widening: the owner is told too often
# rather than too rarely.

def _change(text: str, safety: bool = False) -> Dict[str, Any]:
    return {"text": text, "safety": bool(safety)}


class _Broken(dict):
    """A file that can't be read as YAML or JSON (Kelvin refuses to load it)."""


def _yaml(text: Optional[str]) -> Any:
    if text is None:
        return None
    try:
        data = load_yaml(text)
    except Exception:
        return _Broken()
    if data is None:
        return {}
    return data if isinstance(data, dict) else _Broken()


def _map(value: Any) -> Dict[str, Any]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _pair(value: Any) -> Optional[Tuple[float, float]]:
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(_number(x) is not None for x in value):
        return float(value[0]), float(value[1])
    return None


def _span(p: Tuple[float, float]) -> str:
    return "[%s, %s]" % (num(p[0]), num(p[1]))


def _wider(old: Tuple[float, float], new: Tuple[float, float]) -> bool:
    return new[0] < old[0] or new[1] > old[1]


def _ranges(out: List[Dict[str, Any]], key: str, what: str, old: Any, new: Any) -> None:
    """A [low, high] range that bounds a drive (a lab limit, a card's limit, an auto range): narrowing it is safe."""
    a, b = _pair(old), _pair(new)
    if a == b and (a is not None or old == new):
        return
    if a is None and b is not None:
        out.append(_change("%s: %s set to %s" % (key, what, _span(b))))
    elif b is None and a is not None:
        out.append(_change("%s: %s %s taken out" % (key, what, _span(a)), True))
    elif a is not None and b is not None:
        if _wider(a, b):
            out.append(_change("%s: %s widened, %s → %s" % (key, what, _span(a), _span(b)), True))
        else:
            out.append(_change("%s: %s narrowed, %s → %s" % (key, what, _span(a), _span(b))))
    else:
        out.append(_change("%s: %s changed" % (key, what), True))


def _rates(out: List[Dict[str, Any]], key: str, what: str, old: Any, new: Any) -> None:
    """A rate that slows a drive: raising or taking it out lets the drive move faster."""
    a, b = _number(old), _number(new)
    if a == b:
        return
    if a is None:
        out.append(_change("%s: %s set to %s" % (key, what, num(b))))
    elif b is None:
        out.append(_change("%s: %s %s taken out" % (key, what, num(a)), True))
    elif b > a:
        out.append(_change("%s: %s raised, %s → %s" % (key, what, num(a), num(b)), True))
    else:
        out.append(_change("%s: %s lowered, %s → %s" % (key, what, num(a), num(b))))


def _guards(entry: Dict[str, Any], name: str) -> Dict[Tuple[str, str], Tuple[Any, str]]:
    out = {}
    for param, glist in _map(entry.get("guards")).items():
        for g in glist if isinstance(glist, list) else [glist]:
            if isinstance(g, dict):
                read = str(g.get("read"))
                read = read if "." in read else "%s.%s" % (name, read)
                out[("%s.%s" % (name, param), read)] = (g.get("max_abs"), str(g.get("on_trip") or "safe"))
    return out


def _auto(value: Any) -> Dict[str, Any]:
    if value == "never":
        return {"*": "never"}
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _watch_rule(w: Dict[str, Any]) -> str:
    parts = [str(w.get("read"))]
    for k in ("above", "below"):
        if w.get(k) is not None:
            parts.append("%s %s" % (k, w[k]))
    if w.get("for"):
        parts.append("for %s s" % w["for"])
    then = _map(w.get("then"))
    if then:
        parts.append("then " + ", ".join("%s → %s" % (k, v) for k, v in sorted(then.items())))
    if w.get("stop_run") is False and then:
        parts.append("without stopping the run")
    return " ".join(parts)


def lab_changes(old_text: Optional[str], new_text: Optional[str]) -> List[Dict[str, Any]]:
    """What changed between two versions of the lab config, in words, with what widens Kelvin's reach marked."""
    if new_text is None:
        return [_change("the lab config was deleted (Kelvin has no instruments until there is one)")]
    old, new = _yaml(old_text) if old_text is not None else {}, _yaml(new_text)
    if isinstance(new, _Broken):
        return [_change("the lab config can't be read as YAML now; Kelvin keeps using what it loaded before")]
    if isinstance(old, _Broken):
        old = {}
    out: List[Dict[str, Any]] = []
    if old.get("lab") != new.get("lab") and old_text is not None:
        out.append(_change("the lab's name: %s → %s" % (old.get("lab", "Lab"), new.get("lab", "Lab"))))
    oi, ni = _map(old.get("instruments")), _map(new.get("instruments"))
    observe = new.get("observe_only") is True
    for name in sorted(set(oi) | set(ni)):
        a, b = oi.get(name), ni.get(name)
        if a is None:
            b = _map(b)
            kelvins = b.get("added_by") == "kelvin" or observe
            out.append(_change("%s added (card %s at %s%s)" % (name, b.get("card"), b.get("address", "sim://" + name),
                                                                "; Kelvin's mark: each output change asks" if kelvins
                                                                else "; its limits count as reviewed"), not kelvins))
            continue
        if b is None:
            out.append(_change("%s taken out of the lab" % name))
            continue
        a, b = _map(a), _map(b)
        if a.get("card") != b.get("card"):
            out.append(_change("%s: card %s → %s" % (name, a.get("card"), b.get("card")), True))
        if a.get("address") != b.get("address"):
            out.append(_change("%s: address %s → %s" % (name, a.get("address"), b.get("address"))))
        if a.get("trust", "ask") != b.get("trust", "ask"):
            out.append(_change("%s: trust %s → %s" % (name, a.get("trust", "ask"), b.get("trust", "ask")),
                               b.get("trust") == "envelope"))
        la, lb = _map(a.get("limits")), _map(b.get("limits"))
        for p in sorted(set(la) | set(lb)):
            x, y = la.get(p), lb.get(p)
            if x is None and y is not None and _pair(y) is not None:   # a lab limit narrows the card's
                out.append(_change("%s.%s: limited to %s" % (name, p, _span(_pair(y)))))
            elif y is None and _pair(x) is not None:
                out.append(_change("%s.%s: limit %s taken out (its card's range applies)" % (name, p, _span(_pair(x))),
                                   True))
            else:
                _ranges(out, "%s.%s" % (name, p), "limits", x, y)
        ra, rb = _map(a.get("rates")), _map(b.get("rates"))
        for p in sorted(set(ra) | set(rb)):
            _rates(out, "%s.%s" % (name, p), "rate", ra.get(p), rb.get(p))
        ga, gb = _guards(a, name), _guards(b, name)
        for key in sorted(set(ga) | set(gb)):
            x, y = ga.get(key), gb.get(key)
            what = "guard on %s reading %s" % key
            if y is None:
                out.append(_change("%s (|%s| ≤ %s) taken out" % (what, key[1], x[0]), True))
            elif x is None:
                out.append(_change("%s added (|%s| ≤ %s)" % (what, key[1], y[0])))
            elif x != y:
                looser = (_number(y[0]) is None or _number(x[0]) is None or y[0] > x[0]
                          or (x[1] == "safe" and y[1] != "safe"))
                out.append(_change("%s: |%s| ≤ %s, on trip %s → ≤ %s, on trip %s" % (what, key[1], x[0], x[1], y[0],
                                                                                    y[1]), looser))
        aa, ab = _auto(a.get("auto")), _auto(b.get("auto"))
        for p in sorted(set(aa) | set(ab)):
            x, y = aa.get(p), ab.get(p)
            label = name if p == "*" else "%s.%s" % (name, p)
            if x == y:
                continue
            if y == "never":
                out.append(_change("%s: auto mode never covers it" % label))
            elif x == "never":
                out.append(_change("%s: no longer 'auto: never' (auto mode may cover it)" % label, True))
            elif y is None:
                out.append(_change("%s: its own auto range taken out (the lab's auto_fraction applies)" % label, True))
            elif x is None or _pair(x) is None or _pair(y) is None:
                out.append(_change("%s: its auto range set to %s" % (label, _span(_pair(y)) if _pair(y) else y), True))
            else:
                _ranges(out, label, "auto range", x, y)
        if a.get("added_by") == "kelvin" and b.get("added_by") != "kelvin":
            out.append(_change("%s: limits marked reviewed (Kelvin's mark taken out: its output changes stop asking "
                               "each time)" % name, True))
        elif b.get("added_by") == "kelvin" and a.get("added_by") != "kelvin":
            out.append(_change("%s: marked as added by Kelvin (each output change asks)" % name))
        for k in ("connection", "vars"):
            if _map(a.get(k)) != _map(b.get(k)):
                out.append(_change("%s: %s settings changed" % (name, k)))
    def locks(d: Dict[str, Any]) -> set:
        return {(str(x.get("drive")), str(x.get("require"))) for x in d.get("interlocks") or [] if isinstance(x, dict)}
    ia, ib = locks(old), locks(new)
    for drive, req in sorted(ia - ib):
        out.append(_change("interlock taken out: %s only when %s" % (drive, req), True))
    for drive, req in sorted(ib - ia):
        out.append(_change("interlock added: %s only when %s" % (drive, req)))
    sa, sb = _map(old.get("safe_actions")), _map(new.get("safe_actions"))
    for key in sorted(set(sa) | set(sb)):
        x, y = sa.get(key, "safe"), sb.get(key, "safe")
        if x != y:
            out.append(_change("after a stop %s %s" % (key, "holds where it is (it went to safe)" if y == "hold"
                                                       else "goes to safe again"), y == "hold"))
    wa = {json.dumps(w, sort_keys=True, default=str): w for w in old.get("watch") or [] if isinstance(w, dict)}
    wb = {json.dumps(w, sort_keys=True, default=str): w for w in new.get("watch") or [] if isinstance(w, dict)}
    added = [w for k, w in wb.items() if k not in wa]
    for k, w in wa.items():
        if k in wb:
            continue
        twin = next((x for x in added if x.get("read") == w.get("read")), None)
        if twin is not None:
            added.remove(twin)
            out.append(_change("watch changed: %s → %s" % (_watch_rule(w), _watch_rule(twin)), True))
        else:
            out.append(_change("watch taken out: %s" % _watch_rule(w), True))
    for w in added:
        out.append(_change("watch added: %s" % _watch_rule(w)))
    fa, fb = _number(old.get("auto_fraction", AUTO_FRACTION)), _number(new.get("auto_fraction", AUTO_FRACTION))
    if fa != fb and fa is not None and fb is not None:
        out.append(_change("auto mode covers %.0f%% → %.0f%% of the way from safe to each limit" % (100 * fa, 100 * fb),
                           fb > fa))
    if old.get("observe_only") is True and not observe:
        out.append(_change("observe_only taken out: the instruments Kelvin set up count as reviewed", True))
    elif observe and old.get("observe_only") is not True:
        out.append(_change("observe_only: every instrument counts as added by Kelvin (each output change asks)"))
    if not out and old_text is not None and old_text != new_text:
        out.append(_change("only comments or layout changed"))
    return out


def card_changes(old_text: Optional[str], new_text: Optional[str]) -> List[Dict[str, Any]]:
    """What changed between two versions of one of the lab's cards: its limits, safe values, rates, drives,
    blocked commands and its status (draft or reviewed)."""
    old, new = _yaml(old_text), _yaml(new_text)
    ident = (new if isinstance(new, dict) and new.get("id") else old if isinstance(old, dict) else {}).get("id")
    ident = ident or "?"
    if new is None:
        status = (old or {}).get("status", "draft") if isinstance(old, dict) else "draft"
        if status == "draft":
            return [_change("draft card %s deleted" % ident)]
        return [_change("card %s (%s) deleted: Kelvin's own card of that id, if there is one, applies again"
                        % (ident, status), True)]
    if isinstance(new, _Broken):
        return [_change("card file can't be read as YAML now; Kelvin doesn't use it")]
    if old is None or isinstance(old, _Broken):
        status = new.get("status", "draft")
        if status == "draft":
            return [_change("card %s added (a draft: each change on it asks)" % ident)]
        return [_change("card %s added, marked %s (its outputs stop asking each time)" % (ident, status), True)]
    out: List[Dict[str, Any]] = []
    if old.get("id") != new.get("id"):
        out.append(_change("card id %s → %s" % (old.get("id"), new.get("id")), True))
    sa, sb = old.get("status", "draft"), new.get("status", "draft")
    if sa != sb:
        out.append(_change("card %s: %s → %s%s" % (ident, sa, sb, " (marked reviewed: its outputs stop asking each "
                                                                 "time)" if sa == "draft" else ""), sa == "draft"))
    pa, pb = _map(old.get("parameters")), _map(new.get("parameters"))
    for p in sorted(set(pa) | set(pb)):
        x, y = pa.get(p), pb.get(p)
        key = "%s %s" % (ident, p)
        if not isinstance(x, dict) and isinstance(y, dict):
            out.append(_change("%s: new %s%s" % (key, y.get("kind", "parameter"), " " + _span(_pair(y.get("limits")))
                                                 if _pair(y.get("limits")) else ""), y.get("kind") == "drive"))
            continue
        if isinstance(x, dict) and not isinstance(y, dict):
            out.append(_change("%s: taken out" % key, x.get("kind") == "drive"))
            continue
        if not isinstance(x, dict):
            continue
        if x.get("kind") != y.get("kind"):
            out.append(_change("%s: %s → %s" % (key, x.get("kind"), y.get("kind")), x.get("kind") == "drive"))
        _ranges(out, key, "limits", x.get("limits"), y.get("limits"))
        if x.get("safe") != y.get("safe"):
            out.append(_change("%s: safe value %s → %s" % (key, x.get("safe"), y.get("safe")), True))
        _rates(out, key, "max_rate", x.get("max_rate"), y.get("max_rate"))
    ba = {str(b) for b in (_map(old.get("raw")).get("blocked") or []) if isinstance(b, str)}
    bb = {str(b) for b in (_map(new.get("raw")).get("blocked") or []) if isinstance(b, str)}
    for b in sorted(ba - bb):
        out.append(_change("%s: no longer blocks %s" % (ident, b), True))
    for b in sorted(bb - ba):
        out.append(_change("%s: blocks %s" % (ident, b)))
    if sa != "draft" and sb != "draft" and _workings(old) != _workings(new):
        # a reviewed card's outputs go out without asking each time: a changed command is a new review's
        out.append(_change("card %s: its commands or settings changed while it is marked %s (whoever saves it "
                           "vouches for it again)" % (ident, sb), True))
    elif not out and old_text != new_text:
        out.append(_change("card %s: commands or descriptions changed (no limits, safe values, drives or blocked "
                           "commands)" % ident))
    return out


def _workings(card: Dict[str, Any]) -> str:
    """What a card makes Kelvin send and how it reads the answers: the card without its status, names and words for
    people, and without what card_changes compares on its own (limits, rates, safe values, kinds, blocked commands;
    narrowing those is safe)."""
    def strip(d: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in d.items() if k not in ("label", "description", "limits", "max_rate", "safe", "kind")}
    data = {k: v for k, v in card.items() if k not in ("status", "model", "vendor", "notes", "parameters")}
    data["parameters"] = {p: strip(v) if isinstance(v, dict) else v for p, v in _map(card.get("parameters")).items()}
    if isinstance(data.get("raw"), dict):
        data["raw"] = {k: v for k, v in data["raw"].items() if k != "blocked"}
    return json.dumps(data, sort_keys=True, default=str)


def review_changes(old_text: Optional[str], new_text: Optional[str]) -> List[Dict[str, Any]]:
    """Kelvin's own cards marked reviewed for this lab (carddraft.review_builtin), or back to draft."""
    def load(text: Optional[str]) -> Dict[str, Any]:
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    a, b = load(old_text), load(new_text)
    out = []
    for ident in sorted(set(a) | set(b)):
        x, y = a.get(ident), b.get(ident)
        if x is None:
            out.append(_change("Kelvin's card %s marked reviewed for this lab (its outputs stop asking each time)"
                               % ident, True))
        elif y is None:
            out.append(_change("Kelvin's card %s back to draft for this lab" % ident))
        elif x != y:
            out.append(_change("Kelvin's card %s reviewed again" % ident, True))
    return out


def changes(subject: str, old_text: Optional[str], new_text: Optional[str]) -> List[Dict[str, Any]]:
    """What changed in a kept file between two of its texts (None: the file wasn't there)."""
    if subject == LAB:
        return lab_changes(old_text, new_text)
    if subject == REVIEWS:
        return review_changes(old_text, new_text)
    return card_changes(old_text, new_text)


def widens(subject: str, old_text: Optional[str], new_text: Optional[str]) -> List[str]:
    """The changes from one text to another that widen Kelvin's reach (what the owner's PIN guards), in words."""
    return [c["text"] for c in changes(subject, old_text, new_text) if c["safety"]]
