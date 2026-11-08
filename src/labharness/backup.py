"""A second copy of the lab's records, in a folder the person chooses (phase 1 of the ELN plan; asked for 2026-11-07:
"Kelvin's data saving protocol and experimental procedure recording should follow the practice of modern ELN/LIMS
system"): a NAS, a OneDrive or Dropbox folder, a second disk.

    <folder>/Kelvin backup/<lab>/runs/<run>/   run.h5 with its seal, derived.h5, notes.jsonl
                                 record/       the person's events.jsonl, the lab's instrument log, the notebook's
                                               entries.jsonl, samples.json, the config history's audit.jsonl
                                 procedures/   every version of every procedure
                                 figures/      the saved figures and their records

Each run.h5 is checked against its seal once copied: a copy that isn't what was sealed is not kept. Only what is new
or grew is copied (a run.h5 never changes; the records only grow), so the next backup is quick. Nothing in the
backup is deleted; a run.h5 there that differs from the one here is never written over (it is said, and kept)."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import rundata, runnotes

RUN_PARTS = (rundata.RUN_FILE, rundata.SEAL, rundata.DERIVED, runnotes.FILE)


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


def backup(runs: Iterable[Path], records: Dict[str, Optional[Path]], folders: Dict[str, Optional[Path]],
           target: Path, lab: str) -> Dict[str, Any]:
    """Copy into target/Kelvin backup/<lab>: {"root", "copied", "same", "problems": [...]}. runs: the run folders;
    records: name -> a record file; folders: name -> a folder copied whole (procedures, figures)."""
    root = Path(target) / "Kelvin backup" / _name(lab)
    copied, same = 0, 0
    problems: List[str] = []
    for d in runs:
        if not (d / rundata.RUN_FILE).exists() or rundata.being_recorded(d):
            continue
        seal = rundata.sealed(d)
        for part in RUN_PARTS:
            src, dst = d / part, root / "runs" / d.name / part
            if not src.exists():
                continue
            if part in (rundata.RUN_FILE, rundata.SEAL) and dst.exists():
                if dst.stat().st_size == src.stat().st_size and (part == rundata.SEAL or not seal
                                                                  or rundata.sha256_file(dst) == seal):
                    same += 1
                else:
                    problems.append("%s/%s in the backup differs from this one: left as it is" % (d.name, part))
                continue
            if not _newer(src, dst):
                same += 1
                continue
            try:
                _copy(src, dst)
                if part == rundata.RUN_FILE and seal and rundata.sha256_file(dst) != seal:
                    os.chmod(str(dst), 0o644)
                    dst.unlink()
                    problems.append("%s: the copy wasn't what was sealed, so it wasn't kept" % d.name)
                    continue
                if part in (rundata.RUN_FILE, rundata.SEAL):
                    rundata._read_only(dst)
                copied += 1
            except OSError as e:
                problems.append("%s/%s: %s" % (d.name, part, e))
    for name, src in records.items():
        if src is None or not Path(src).exists():
            continue
        dst = root / "record" / name
        try:
            if _newer(Path(src), dst):
                _copy(Path(src), dst)
                copied += 1
            else:
                same += 1
        except OSError as e:
            problems.append("%s: %s" % (name, e))
    for name, folder in folders.items():
        if folder is None or not Path(folder).exists():
            continue
        for src in sorted(Path(folder).rglob("*")):
            if not src.is_file() or src.suffix in (".tmp", ".part"):
                continue
            dst = root / name / src.relative_to(folder)
            try:
                if _newer(src, dst):
                    _copy(src, dst)
                    copied += 1
                else:
                    same += 1
            except OSError as e:
                problems.append("%s: %s" % (src.name, e))
    return {"root": str(root), "copied": copied, "same": same, "problems": problems}


def said(out: Dict[str, Any]) -> Tuple[str, bool]:
    text = "backed up into %s: %d file%s copied, %d already there%s" % (
        out["root"], out["copied"], "" if out["copied"] == 1 else "s", out["same"],
        "; " + "; ".join(out["problems"][:8]) if out["problems"] else "")
    return text, bool(out["problems"])
