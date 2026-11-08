"""The lab notebook's entries (phase 3 of the ELN plan; asked for 2026-11-07: "Kelvin's data saving protocol and
experimental procedure recording should follow the practice of modern ELN/LIMS system, and be ai-native").

An entry says what was done and found, for people: its aim, the samples and procedure, the runs it rests on, what was
seen, what it means. Kelvin drafts one from its records (the chat's model with write_entry, or the Notebook panel's
subagent: web/notebook.py), citing each run as [run:<id>], and code checks every number in it against the records it
cites (numbercheck.py); a person writes or edits one in the panel. A person then signs it, and another may witness
it: a signed version is never changed, a later version is an amendment and says so.

    <data folder>/notebook/entries.jsonl   chained, as the lab's record is (eventlog.append_chained)
      a version: {"entry", "version", "at", "by": agent | human, "account", "ai", "title", "day", "aim", "body",
                  "runs", "samples", "procedure", "check", "amends", "sha256"}
      a signature: {"entry", "sign": {"account", "at", "role": author | witness, "version", "sha256"}}

sha256 is the version's content (its title, day, aim, body, runs, samples and procedure as canonical JSON): a signature
names the content it signed."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .eventlog import append_chained

FOLDER = "notebook"
FILE = "entries.jsonl"
CONTENT = ("title", "day", "aim", "body", "runs", "samples", "procedure")
MAX_BODY = 20000
CITE = re.compile(r"\[run:([^\]\s]+)\]")


def path_of(data_dir: Any) -> Path:
    return Path(data_dir) / FOLDER / FILE


def content_hash(version: Dict[str, Any]) -> str:
    content = {k: version.get(k) for k in CONTENT}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def read(data_dir: Any) -> Dict[str, Dict[str, Any]]:
    """Every entry: {id: {"versions": [...], "signs": [...]}}, in the order they were begun."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        with open(path_of(data_dir), encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(item, dict) or not item.get("entry"):
                    continue
                item.pop("prev", None)
                e = out.setdefault(item["entry"], {"versions": [], "signs": []})
                if "sign" in item:
                    e["signs"].append(item["sign"])
                else:
                    e["versions"].append(item)
    except OSError:
        pass
    return out


def latest(e: Dict[str, Any]) -> Dict[str, Any]:
    return e["versions"][-1] if e["versions"] else {}


def signed_version(e: Dict[str, Any]) -> Optional[int]:
    """The last version an author signed, or None."""
    authors = [s for s in e["signs"] if s.get("role") == "author"]
    return authors[-1]["version"] if authors else None


def status(e: Dict[str, Any]) -> str:
    """draft, signed, witnessed, or amended (a version after the signed one)."""
    v = latest(e).get("version")
    s = signed_version(e)
    if s is None:
        return "draft"
    if v and v > s:
        return "amended"
    witnessed = any(x.get("role") == "witness" and x.get("version") == s for x in e["signs"])
    return "witnessed" if witnessed else "signed"


def cited_runs(text: str) -> List[str]:
    return list(dict.fromkeys(CITE.findall(str(text or ""))))


def write(data_dir: Any, fields: Dict[str, Any], by: str, entry: str = "", account: Optional[str] = None,
          ai: Optional[Dict[str, Any]] = None, check: Optional[Dict[str, Any]] = None,
          whose: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A new entry (entry ""), or a new version of one. ValueError: no title or body, or no such entry."""
    title = " ".join(str(fields.get("title") or "").split())[:200]
    body = str(fields.get("body") or "").strip()[:MAX_BODY]
    entries = read(data_dir)
    before = latest(entries[entry]) if entry and entry in entries else None
    if entry and before is None:
        raise ValueError("no entry %s (notebook lists them)" % entry)
    title = title or (before or {}).get("title", "")
    body = body or (before or {}).get("body", "")
    if not title or not body:
        raise ValueError("an entry needs a title and a body")
    version: Dict[str, Any] = {
        "entry": entry or "e" + secrets.token_hex(4), "version": (before or {}).get("version", 0) + 1,
        "at": datetime.now().isoformat(timespec="seconds"), "by": by, "title": title,
        "day": str(fields.get("day") or (before or {}).get("day") or datetime.now().date().isoformat())[:10],
        "aim": " ".join(str(fields.get("aim") if fields.get("aim") is not None else (before or {}).get("aim") or "")
                        .split())[:1000],
        "body": body,
        "runs": list(dict.fromkeys([str(r) for r in (fields.get("runs") or [])] + cited_runs(body))),
        "samples": [str(s) for s in (fields.get("samples") or (before or {}).get("samples") or [])][:20],
        "procedure": str(fields.get("procedure") or (before or {}).get("procedure") or "")[:120]}
    for k, v in (("account", account), ("ai", ai), ("check", check), ("whose", whose)):
        if v:
            version[k] = v
    signed = signed_version(entries[entry]) if entry else None
    if signed is not None:
        version["amends"] = signed
    version["sha256"] = content_hash(version)
    append_chained(path_of(data_dir), version)
    return version


def sign(data_dir: Any, entry: str, account: str, role: str = "author") -> Dict[str, Any]:
    """A person signs an entry's latest version (author), or witnesses it (another person, once the author
    signed). ValueError: no such entry, a witness who wrote or signed it, the same signature twice."""
    entries = read(data_dir)
    if entry not in entries:
        raise ValueError("no entry %s" % entry)
    e = entries[entry]
    v = latest(e)
    if role not in ("author", "witness"):
        raise ValueError("a signature is the author's or a witness's")
    if not account:
        raise ValueError("only a person signed in to Kelvin signs an entry")
    if role == "witness":
        s = signed_version(e)
        if s != v["version"]:
            raise ValueError("a witness signs after the author has signed this version")
        if any(x.get("account") == account and x.get("version") == s for x in e["signs"]):
            raise ValueError("a witness is someone other than who signed it")
    if any(x.get("account") == account and x.get("version") == v["version"] and x.get("role") == role
           for x in e["signs"]):
        raise ValueError("%s already signed version %d" % (account, v["version"]))
    signature = {"account": account, "at": datetime.now().isoformat(timespec="seconds"), "role": role,
                 "version": v["version"], "sha256": v["sha256"]}
    append_chained(path_of(data_dir), {"entry": entry, "sign": signature})
    return signature


def summary(eid: str, e: Dict[str, Any]) -> str:
    v = latest(e)
    st = status(e)
    who = ", ".join("%s %s" % ("witnessed by" if s["role"] == "witness" else "signed by", s["account"])
                    for s in e["signs"] if s.get("version") == signed_version(e))
    by = "Kelvin (%s)" % v["ai"]["model"] if v.get("ai") else (v.get("account") or v.get("by"))
    return "%s %s v%d (%s%s): %s, by %s" % (v.get("day"), eid, v.get("version", 0), st, "; " + who if who else "",
                                            v.get("title"), by)
