"""A second copy of the lab's records, in a folder a person chooses (phase 1 of the ELN plan; asked for 2026-11-07:
"Kelvin's data saving protocol and experimental procedure recording should follow the practice of modern ELN/LIMS
system"): a NAS, a OneDrive or Dropbox folder, a second disk. Only a person starts one, in Settings (Data & reports):
Kelvin's AI can't, nor choose where it goes (a chat copied everyone's unshared work into its own folder: audit 0.3.52,
R12).

    <folder>/Kelvin backup/<lab>/runs/<run>/       run.h5 with its seal, derived.h5, notes.jsonl
                                 record/           the lab's instrument log, the config history's audit.jsonl
                                 people/<person>/  each person's own: their events.jsonl, the notebook's
                                                   entries.jsonl, samples.json with samples/ (the samples' pictures),
                                                   figures/ (the saved figures and their records)
                                 procedures/       every version of every procedure

Each person's record is filed under their name (the account; "no account" when nobody signs in): it was filed by the
lab's name alone, so one person's backup wrote over another's (audit 0.3.52, D1).

Each run.h5 is checked against its seal once copied: a copy that isn't what was sealed is not kept. Only what is new
or grew is copied, so the next backup is quick. Nothing in the backup is deleted, nor written over with less: a
record there is replaced only by one that holds all of it (the records only grow: one that begins with it), so a
record that lost lines here never replaces its full copy (it is said); one that doesn't continue its copy there
(another data folder's, or lines changed here) is kept beside it, as events.2.jsonl, and said; a run.h5 there that
differs from the one here is never written over (it is said, and kept, with what goes with it)."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import rundata, runnotes

RUN_PARTS = (rundata.RUN_FILE, rundata.SEAL, rundata.DERIVED, runnotes.FILE)
AS_GIVEN = ("samples",)   # a person's folders whose files are never changed (a sample's pictures)
NOBODY = "no account"     # whose records they are when nobody signs in to Kelvin
MAX_BESIDE = 99           # copies of one record kept beside each other (events.2.jsonl ...)


def _name(text: str) -> str:
    return "".join(c if c.isalnum() or c in " -_." else "_" for c in str(text or "")).strip() or "lab"


def _newer(src: Path, dst: Path) -> bool:
    """Whether src has more to copy than dst: dst missing, or src grew or changed since."""
    if not dst.exists():
        return True
    a, b = src.stat(), dst.stat()
    return a.st_size != b.st_size or a.st_mtime > b.st_mtime + 1


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    shutil.copyfile(str(src), str(part))
    shutil.copystat(str(src), str(part))
    if dst.exists():
        os.chmod(str(dst), 0o644)   # a record that grew: its older copy goes (never a run.h5: see backup)
    os.replace(str(part), str(dst))


def begins(part: Path, whole: Path) -> bool:
    """Whether the file whole begins with the file part (the same file included): a record that only grows holds
    all its earlier copy then."""
    try:
        if part.stat().st_size > whole.stat().st_size:
            return False
        with open(part, "rb") as a, open(whole, "rb") as b:
            while True:
                block = a.read(1 << 20)
                if not block:
                    return True
                if b.read(len(block)) != block:
                    return False
    except OSError:
        return False


def holds_samples(old: Path, new: Path) -> bool:
    """Whether samples.json new holds everything old holds: each sample, with at least its changes and pictures
    (nothing in the register is ever removed). One that isn't a register holds nothing."""
    def samples(p: Path) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        found = data.get("samples") if isinstance(data, dict) else None
        return found if isinstance(found, dict) else None
    a, b = samples(old), samples(new)
    if b is None:
        return False
    for key, s in (a or {}).items():
        t = b.get(key)
        if not isinstance(t, dict) or not isinstance(s, dict):
            return False
        if any(len(t.get(k) or []) < len(s.get(k) or []) for k in ("changes", "pictures")):
            return False
    return True


def _same(a: Path, b: Path) -> bool:
    return a.stat().st_size == b.stat().st_size and begins(a, b)


def _record(src: Path, dst: Path, holds: Callable[[Path, Path], bool], label: str, problems: List[str]) -> str:
    """A record into the backup as dst: "copied", "same", or "kept" (its copy there left as it is, said). A copy
    there is replaced only by a record that holds all of it (holds(copy, here)); a record here that has less leaves
    it as it is; one that doesn't continue it is kept beside it under a name of its own (<name>.2<ext>...)."""
    there = [dst] + [dst.with_name("%s.%d%s" % (dst.stem, n, dst.suffix)) for n in range(2, MAX_BESIDE + 1)]
    have = [p for p in there if p.exists()]
    for p in have:
        if holds(p, src):
            if _same(p, src):
                return "same"
            _copy(src, p)
            return "copied"
    for p in have:
        if holds(src, p):
            problems.append("%s here holds less than its copy in the backup (lost here?): the backup's copy is kept "
                            "as it is" % label)
            return "kept"
    free = next((p for p in there if not p.exists()), None)
    if free is None:
        problems.append("%s: %d different copies are in the backup already; not copied" % (label, len(have)))
        return "kept"
    _copy(src, free)
    if free != dst:
        problems.append("%s here doesn't continue its copy in the backup (another data folder's record, or lines "
                        "changed here): kept beside it as %s" % (label, free.name))
    return "copied"


def _as_given(src: Path, dst: Path, label: str, problems: List[str]) -> str:
    """A file that never changes (a sample's picture): copied once; one there that differs is left as it is."""
    if not dst.exists():
        _copy(src, dst)
        return "copied"
    if _same(dst, src):
        return "same"
    problems.append("%s in the backup differs from this one: left as it is" % label)
    return "kept"


def _changing(src: Path, dst: Path) -> str:
    """A file kept up to date (derived.h5, a figure, a procedure): copied when it grew or changed."""
    if not _newer(src, dst):
        return "same"
    _copy(src, dst)
    return "copied"


def backup(runs: Iterable[Path], records: Dict[str, Optional[Path]], folders: Dict[str, Optional[Path]],
           target: Path, lab: str, person: Optional[str] = None, own: Optional[Dict[str, Optional[Path]]] = None,
           own_folders: Optional[Dict[str, Optional[Path]]] = None) -> Dict[str, Any]:
    """Copy into target/Kelvin backup/<lab>: {"root", "copied", "same", "problems": [...]}. runs: the run folders;
    records: the lab's record files (name -> file); folders: the lab's folders copied whole (procedures); own and
    own_folders: the person's record files and folders (their events.jsonl, entries.jsonl, samples.json; samples,
    figures), filed under people/<person>."""
    root = Path(target) / "Kelvin backup" / _name(lab)
    mine = root / "people" / _name(person or NOBODY)
    counts = {"copied": 0, "same": 0, "kept": 0}
    problems: List[str] = []

    def one(label: str, work: Callable[[], str]) -> None:
        try:
            counts[work()] += 1
        except OSError as e:
            problems.append("%s: %s" % (label, e))

    for d in runs:
        if not (d / rundata.RUN_FILE).exists() or rundata.being_recorded(d):
            continue
        place = root / "runs" / d.name
        try:
            if not _run_file(d, place, problems, counts):
                continue   # not the same run there: nothing of it is mixed in
        except OSError as e:
            problems.append("%s/%s: %s" % (d.name, rundata.RUN_FILE, e))
            continue
        for part in (rundata.SEAL, rundata.DERIVED, runnotes.FILE):
            src, dst = d / part, place / part
            if not src.exists():
                continue
            label = "%s/%s" % (d.name, part)
            if part == rundata.SEAL:
                one(label, lambda: _sealed(src, dst, label, problems))
            elif part == runnotes.FILE:   # notes only grow: never written over with fewer
                one(label, lambda: _record(src, dst, begins, label, problems))
            else:
                one(label, lambda: _changing(src, dst))
    for name, src in records.items():
        if src is not None and Path(src).exists():
            one(name, lambda: _record(Path(src), root / "record" / name, begins, name, problems))
    for name, src in (own or {}).items():
        if src is not None and Path(src).exists():
            holds = holds_samples if name.endswith(".json") else begins
            one(name, lambda: _record(Path(src), mine / name, holds, name, problems))
    for base, place, group in [(mine, n, f) for n, f in (own_folders or {}).items()] + \
                              [(root, n, f) for n, f in folders.items()]:
        if group is None or not Path(group).exists():
            continue
        for src in sorted(Path(group).rglob("*")):
            if not src.is_file() or src.suffix in (".tmp", ".part"):
                continue
            dst = base / place / src.relative_to(group)
            label = "%s/%s" % (place, src.relative_to(group).as_posix())
            if base is mine and place in AS_GIVEN:
                one(label, lambda: _as_given(src, dst, label, problems))
            else:
                one(label, lambda: _changing(src, dst))
    return {"root": str(root), "copied": counts["copied"], "same": counts["same"], "problems": problems}


def _run_file(d: Path, place: Path, problems: List[str], counts: Dict[str, int]) -> bool:
    """A run's run.h5 into the backup, checked against its seal once copied: whether the run there is this one (a
    run.h5 there that differs is never written over, nor is anything of this run put beside it)."""
    src, dst = d / rundata.RUN_FILE, place / rundata.RUN_FILE
    seal = rundata.sealed(d)
    if dst.exists():
        if dst.stat().st_size == src.stat().st_size and (not seal or rundata.sha256_file(dst) == seal):
            counts["same"] += 1
            return True
        problems.append("%s/%s in the backup differs from this one: left as it is, with what goes with it"
                        % (d.name, rundata.RUN_FILE))
        return False
    _copy(src, dst)
    if seal and rundata.sha256_file(dst) != seal:
        os.chmod(str(dst), 0o644)
        dst.unlink()
        problems.append("%s: the copy wasn't what was sealed, so it wasn't kept" % d.name)
        return False
    rundata._read_only(dst)
    counts["copied"] += 1
    return True


def _sealed(src: Path, dst: Path, label: str, problems: List[str]) -> str:
    """A run's seal file: copied once and marked read-only; one there that differs is left as it is."""
    if dst.exists():
        if dst.stat().st_size == src.stat().st_size:
            return "same"
        problems.append("%s in the backup differs from this one: left as it is" % label)
        return "kept"
    _copy(src, dst)
    rundata._read_only(dst)
    return "copied"


def said(out: Dict[str, Any]) -> Tuple[str, bool]:
    text = "backed up into %s: %d file%s copied, %d already there%s" % (
        out["root"], out["copied"], "" if out["copied"] == 1 else "s", out["same"],
        "; " + "; ".join(out["problems"][:8]) if out["problems"] else "")
    return text, bool(out["problems"])
