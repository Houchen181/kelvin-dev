"""Verify: whether Kelvin's records are as they were written (asked for 2026-11-07: "Kelvin's data saving protocol and
experimental procedure recording should follow the practice of modern ELN/LIMS system, and be ai-native").

What it checks, only reading:
- the lab's record: each person's events.jsonl, the lab's instrument log, the config history's audit.jsonl and the
  notebook's entries.jsonl. Each line keeps the SHA-256 of the line before it (eventlog.py), so a line changed or
  taken out afterwards breaks the chain where it was. Lines an older Kelvin wrote keep no link and are counted apart.
  A record's end is also kept outside it (eventlog.keep_end), so one cut short is found too.
- each kept run: run.h5 against its seal (run.h5.sha256, written when the run was kept: rundata.seal), and that seal
  against the one the record kept with the run's end (run_end, run_sealed), so a run.h5 changed together with its
  seal file is found too; a run whose seal file is gone is compared with the record's seal; its notes.jsonl's chain.
- each run the record sealed: that its folder is there (Verify started from the folders it found, so a run deleted
  whole was never missed: audit 0.3.52, D6).

The record takes some microseconds a line; runs about a second per GB of run.h5, newest first, until DEADLINE_S."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import rundata, runnotes
from .eventlog import verify_chain

DEADLINE_S = 120.0   # runs checked at most this long in one go (newest first); the rest are named as not checked
SEAL_EVENTS = ("run_end", "run_sealed", "run_imported")


def check_logs(paths: Dict[str, Path]) -> Dict[str, Dict[str, Any]]:
    """Each chained record walked (eventlog.verify_chain), by the name Verify gives it."""
    return {name: verify_chain(path) for name, path in paths.items()}


def folder_name(run_id: Any) -> str:
    """A run's id as its folder is named: Windows drops a name's trailing dots and spaces (an older Kelvin kept a
    plan named "IV at 4 K." in a folder without its dot)."""
    return str(run_id).rstrip(". ")


def recorded_seals(events: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """run id -> the SHA-256 the record kept for its run.h5: the first one said. A run is sealed once; another said
    later (seals_said_again) is never what the run is checked against: Kelvin's own tidy once sealed a changed run.h5
    again after its seal file was deleted, and Verify took that one, the last said (audit 0.3.52, D2)."""
    out: Dict[str, str] = {}
    for e in events:
        if e.get("type") in SEAL_EVENTS and e.get("run") and e.get("sha256"):
            out.setdefault(folder_name(e["run"]), str(e["sha256"]))
    return out


def seals_said_again(events: Iterable[Dict[str, Any]]) -> List[Tuple[str, str, str]]:
    """(run, the seal the record kept first, another it said later) for each run the record keeps two seals of."""
    first: Dict[str, str] = {}
    out: List[Tuple[str, str, str]] = []
    for e in events:
        if e.get("type") in SEAL_EVENTS and e.get("run") and e.get("sha256"):
            run, digest = folder_name(e["run"]), str(e["sha256"])
            kept = first.setdefault(run, digest)
            if digest != kept and (run, kept, digest) not in out:
                out.append((run, kept, digest))
    return out


def check_runs(folders: Sequence[Path], recorded: Dict[str, str], deadline_s: float = DEADLINE_S,
               clock: Callable[[], float] = time.monotonic) -> Dict[str, List[Any]]:
    """Each run folder's state: intact, changed (run.h5 isn't what its seal says), reseal (the seal file isn't what
    the record kept), seal_gone (no seal file, though the record kept a seal: run.h5 compared with that one),
    unsealed, unreadable, gone (a seal without its run.h5), unchecked (time ran out); and notes: each break in a
    run's notes.jsonl's chain."""
    out: Dict[str, List[Any]] = {k: [] for k in ("intact", "changed", "reseal", "seal_gone", "unsealed", "unreadable",
                                                 "gone", "unchecked", "notes")}
    end = clock() + deadline_s
    for d in folders:
        if clock() > end:
            out["unchecked"].append(d.name)
            continue
        state, detail = rundata.check_seal(d)
        kept = recorded.get(folder_name(d.name))
        sealed = rundata.sealed(d)
        if state == "unsealed" and kept:   # its seal file is gone: never "not sealed yet" (D2, D6)
            now = rundata.sha256_file(d / rundata.RUN_FILE)
            out["seal_gone"].append((d.name, now == kept, kept, now))
        elif state == "intact" and kept and sealed != kept:
            out["reseal"].append((d.name, sealed, kept))
        elif state == "changed":
            out["changed"].append((d.name, detail, sealed))
        elif state == "unreadable":
            out["unreadable"].append((d.name, detail))
        else:
            out[state].append(d.name)
        notes = d / runnotes.FILE
        if notes.exists():   # a note changed or taken out afterwards (the docs said Verify checks them; it didn't: D4)
            out["notes"] += [(d.name, b) for b in verify_chain(notes)["breaks"]]
    return out


def runs_gone(recorded: Dict[str, str], found: Iterable[str], hidden: Iterable[str] = (), since: str = "",
              only: Sequence[str] = ()) -> List[str]:
    """The runs the record sealed whose folder isn't found (deleted, or on a drive that is away), newest first: not
    those hidden from the asker, only those named or since a date."""
    have = {folder_name(n) for n in found}
    hidden = {folder_name(n) for n in hidden}
    wanted = {folder_name(r.strip()) for r in only if r.strip()}
    out = [r for r in recorded if r not in have and r not in hidden and (not wanted or r in wanted)
           and not (since and r[:8] < since.replace("-", "")[:8])]
    return sorted(out, reverse=True)


def _since(result: Dict[str, Any]) -> str:
    when = str(result.get("since") or "")[:16].replace("T", " ")
    return " since %s" % when if when else ""


def report(logs: Dict[str, Dict[str, Any]], runs: Optional[Dict[str, List[Any]]], gone: Sequence[str] = (),
           again: Sequence[Tuple[str, str, str]] = (), more: Sequence[str] = ()) -> str:
    """Verify's answer in a few lines: what holds, then each problem with where it is. gone: runs the record sealed
    whose folder isn't found (runs_gone); again: seals said twice (seals_said_again); more: other problems, in
    words (a record cut short since its end was kept, a notebook entry changed since it was signed)."""
    problems: List[str] = []
    parts: List[str] = []
    for name, r in logs.items():
        if not r.get("lines"):
            continue
        older = r["lines"] - r["chained"]
        parts.append("%s %d line%s%s%s" % (name, r["lines"], "" if r["lines"] == 1 else "s",
                                           ", chained%s" % _since(r) if r["chained"] else ", none chained yet",
                                           " (%d older, from before chaining)" % older if r["chained"] and older
                                           else ""))
        for b in r["breaks"][:5]:
            where = "line %s" % b["line"] + (" (seq %s, %s)" % (b.get("seq"), str(b.get("wall") or "")[:19])
                                             if b.get("seq") is not None else "")
            problems.append("%s, %s: %s" % (name, where, b["why"]))
        if len(r["breaks"]) > 5:
            problems.append("%s: %d more breaks" % (name, len(r["breaks"]) - 5))
    lines = ["Record: %s." % ("; ".join(parts) if parts else "nothing written yet")]
    if runs is not None:
        n_ok = len(runs["intact"])
        said = ["%d run%s sealed and unchanged" % (n_ok, "" if n_ok == 1 else "s")]
        if runs["unsealed"]:
            said.append("%d not sealed yet (Kelvin seals older runs when it tidies, saying it sealed them late)"
                        % len(runs["unsealed"]))
        if runs["unchecked"]:
            said.append("%d older not checked this time (time ran out: name them, or give since)"
                        % len(runs["unchecked"]))
        lines.append("Runs: %s." % "; ".join(said))
        for run, now, seal in runs["changed"]:
            problems.append("run %s: run.h5 changed since it was kept (SHA-256 %s…, sealed %s…)" % (run, now[:12],
                                                                                                   (seal or "")[:12]))
        for run, seal, kept in runs["reseal"]:
            problems.append("run %s: its seal file says %s…, the record kept %s…: run.h5 and its seal were both "
                            "changed" % (run, (seal or "")[:12], kept[:12]))
        for run, same, kept, now in runs.get("seal_gone", []):
            problems.append("run %s: its seal file is gone; run.h5 is as the record kept it (SHA-256 %s…)" % (
                run, kept[:12]) if same else "run %s: its seal file is gone, and run.h5 changed since it was kept "
                                              "(SHA-256 %s…, the record kept %s…)" % (run, now[:12], kept[:12]))
        for run, why in runs["unreadable"]:
            problems.append("run %s: %s" % (run, why))
        for run in runs["gone"]:
            problems.append("run %s: sealed, but its run.h5 is gone" % run)
        for run, b in runs.get("notes", []):
            problems.append("run %s's notes (notes.jsonl), line %s: %s" % (run, b["line"], b["why"]))
    for run in gone:
        problems.append("run %s: the record kept its seal, but its folder is gone (deleted, or on a drive that is "
                        "away)" % run)
    for run, kept, later in again:
        problems.append("run %s: the record kept its seal %s…, and later said another, %s…: a run is sealed once"
                        % (run, kept[:12], later[:12]))
    problems.extend(more)
    if problems:
        lines.append("Problems:")
        lines.extend("- " + p for p in problems)
    else:
        lines.append("No problems: every chained line follows the one before it%s." % (
            " and every sealed run is as it was kept" if runs is not None else ""))
    return "\n".join(lines)


def run_folders(folders: Iterable[Path], hidden: Iterable[str] = (), since: str = "",
                only: Sequence[str] = ()) -> List[Path]:
    """The kept runs to check, newest first: not those hidden from the asker, only those named or since a date."""
    hidden = set(hidden)
    wanted = {r.strip() for r in only if r.strip()}
    out: List[Tuple[str, Path]] = []
    for d in folders:
        if d.name in hidden or (wanted and d.name not in wanted):
            continue
        if not ((d / rundata.RUN_FILE).exists() or (d / rundata.SEAL).exists()):
            continue
        if since and d.name[:8] < since.replace("-", "")[:8]:
            continue
        out.append((d.name, d))
    return [d for _, d in sorted(out, reverse=True)]
