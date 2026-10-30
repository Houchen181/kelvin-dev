#!/usr/bin/env python3
"""Bring labMaster's stdlib sequence engine into Kelvin, as `labharness/sequence/` (v1-A).

    python tools/vendor_sequence.py              # write src/labharness/sequence/ (repeatable)
    python tools/vendor_sequence.py --check      # write nothing: say whether the vendored copy is current
    python tools/vendor_sequence.py --source D:\\Codex\\Kelvin\\labMaster

What is copied and why (design: V1_SPEC.md, "架构/A/B/C"):

    panel_engine.py         -> engine.py          the sequence engine (runs, queue, history, palette)
    panel_paths.py          -> paths.py           paths/config/JSON - REWRITTEN: the data root is
                                                  ~/Kelvin/sequence (KELVIN_SEQUENCE_DIR overrides) while
                                                  the code, runners and VERSION stay in this package
    panel_scan.py           -> scan.py            reads a script's parameters
    panel_datafolder.py     -> datafolder.py      History's data folder (engine.df; optional at runtime)
    panel_dataindex.py      -> dataindex.py       the data folder's index (imported by datafolder.py)
    panel_history_export.py -> history_export.py  "Export manual groups"
    panel_myscripts.py      -> myscripts.py       History's analysis scripts
    runners/run_block.py, run_myscript.py, myscript_guard.py, guard_site/sitecustomize.py
                            -> runners/...        the child processes the engine spawns
    blocks.default.json     -> blocks.default.json  the first-start palette (Wait + Safe stop)
    VERSION                 -> VERSION             shown as the engine's version

Import mapping (absolute panel modules -> package-relative ones), applied line by line and checked;
a line that is no longer where it was (or a stray `import panel_*` left behind) stops this tool:

    import panel_paths as P             -> from . import paths as P
    import panel_scan                   -> from . import scan as panel_scan
    import panel_datafolder             -> from . import datafolder as panel_datafolder
    import panel_datafolder as DF       -> from . import datafolder as DF
    import panel_dataindex as DI        -> from . import dataindex as DI
    import panel_history_export as _hx  -> from . import history_export as _hx
    import panel_myscripts              -> from . import myscripts as panel_myscripts
    import panel_myscripts as MS        -> from . import myscripts as MS

`panel_notes` is deliberately NOT vendored: datafolder.py imports it only for History's analysis
folders, inside a try/except, and Kelvin's Sequence page does not use that folder. It stays as an
absolute import and degrades to "no analysis folders".

The runners are plain scripts, started as `python <package>/runners/run_block.py`: their
`import panel_paths as _P` becomes `import paths as _P` (run_block.py puts the package folder on
sys.path itself), and run_myscript.py loads `paths.py` instead of `panel_paths.py` by file name.

The vendored files must never be edited by hand (V1_SPEC.md, "vendoring 用脚本").

Standard library only; Python 3.9+ (Kelvin's floor).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent            # kelvin-dev/
DEFAULT_SOURCE = REPO_ROOT.parent / "labMaster"                # D:\Codex\Kelvin\labMaster
DEFAULT_TARGET = REPO_ROOT / "src" / "labharness" / "sequence"

# source name -> vendored name
PACKAGE_FILES: Dict[str, str] = {
    "panel_engine.py": "engine.py",
    "panel_paths.py": "paths.py",
    "panel_scan.py": "scan.py",
    "panel_datafolder.py": "datafolder.py",
    "panel_dataindex.py": "dataindex.py",
    "panel_history_export.py": "history_export.py",
    "panel_myscripts.py": "myscripts.py",
    "blocks.default.json": "blocks.default.json",
    "VERSION": "VERSION",
}
RUNNER_FILES: Tuple[str, ...] = (
    "run_block.py",
    "run_myscript.py",
    "myscript_guard.py",
    "guard_site/sitecustomize.py",
)

# each vendored module's own import lines: (regex, replacement) - one match each, checked
IMPORT_REWRITES: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "panel_engine.py": (
        (r"(?m)^([ \t]*)import panel_paths as P$", r"\1from . import paths as P"),
        (r"(?m)^([ \t]*)import panel_scan$", r"\1from . import scan as panel_scan"),
        (r"(?m)^([ \t]*)import panel_datafolder$",
         r"\1from . import datafolder as panel_datafolder"),
        (r"(?m)^([ \t]*)import panel_history_export as _hx$",
         r"\1from . import history_export as _hx"),
        (r"(?m)^([ \t]*)import panel_myscripts$", r"\1from . import myscripts as panel_myscripts"),
    ),
    "panel_paths.py": (),
    "panel_scan.py": (),          # standalone: standard library only
    "panel_datafolder.py": (
        (r"(?m)^([ \t]*)import panel_paths as P$", r"\1from . import paths as P"),
        (r"(?m)^([ \t]*)import panel_dataindex as DI$", r"\1from . import dataindex as DI"),
        (r"(?m)^([ \t]*)import panel_myscripts as MS$", r"\1from . import myscripts as MS"),
    ),
    "panel_dataindex.py": ((r"(?m)^([ \t]*)import panel_paths as P$", r"\1from . import paths as P"),),
    "panel_history_export.py": (
        (r"(?m)^([ \t]*)import panel_paths as P$", r"\1from . import paths as P"),
        (r"(?m)^([ \t]*)import panel_dataindex as DI$", r"\1from . import dataindex as DI"),
        (r"(?m)^([ \t]*)import panel_datafolder as DF$", r"\1from . import datafolder as DF"),
    ),
    "panel_myscripts.py": ((r"(?m)^([ \t]*)import panel_paths as P$", r"\1from . import paths as P"),),
}
# after the rewrite no absolute panel import may be left (panel_notes excepted: not vendored)
LEFT_OVER = (r"(?m)^[ \t]*import panel_(?!notes\b)", r"(?m)^[ \t]*from panel_(?!notes\b)")

# engine.py only: the runner's own folder is this package's, not a sub-folder of the data root
ENGINE_FIXES: Tuple[Tuple[str, str], ...] = (
    ("os.path.join(P.PANEL_DIR, 'runners', 'run_myscript.py')",
     "os.path.join(P.RUNNERS_DIR, 'run_myscript.py')"),
)

# paths.py: where the engine reads and writes (the vendored file's header says why)
PATHS_FIXES: Tuple[Tuple[str, str], ...] = (
    ("PANEL_DIR = os.path.dirname(os.path.abspath(__file__))\n",
     "# --- Kelvin (v1-A): vendored from labMaster. The engine's DATA root is ~/Kelvin/sequence\n"
     "# (KELVIN_SEQUENCE_DIR overrides); the code, runners and VERSION stay beside this file, in\n"
     "# labharness/sequence/. Nothing below reads LABMASTER_ROOT or a .labmaster-root marker. ---\n"
     "PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))\n"
     "PANEL_DIR = (os.environ.get('KELVIN_SEQUENCE_DIR') or '').strip() or os.path.join(\n"
     "    os.path.expanduser('~'), 'Kelvin', 'sequence')\n"
     "PANEL_DIR = os.path.abspath(os.path.expanduser(PANEL_DIR))\n"),
    ("ROOT = _find_root(PANEL_DIR)\n",
     "ROOT = PANEL_DIR          # Kelvin: the engine's root IS its data folder (no root marker)\n"),
    ("RUNNERS_DIR = os.path.join(PANEL_DIR, 'runners')\n",
     "RUNNERS_DIR = os.path.join(PACKAGE_DIR, 'runners')\n"),
    ("BLOCKS_DEFAULT_PATH = os.path.join(PANEL_DIR, 'blocks.default.json')\n",
     "BLOCKS_DEFAULT_PATH = os.path.join(PACKAGE_DIR, 'blocks.default.json')\n"),
    ("VERSION_PATH = os.path.join(PANEL_DIR, 'VERSION')\n",
     "VERSION_PATH = os.path.join(PACKAGE_DIR, 'VERSION')\n"),
    ("SESSION_LOG = os.path.join(LOG_DIR, 'session.log')\n",
     "SESSION_LOG = os.path.join(LOG_DIR, 'session.log')\n"
     "\n"
     "\n"
     "def use_root(path):\n"
     "    \"\"\"Point every DATA folder at `path` - Kelvin's SequenceService does this for its home, and a\n"
     "    test for a temporary one. The code folders (PACKAGE_DIR, RUNNERS_DIR, VERSION, the default\n"
     "    function list) never move. Call it before an engine for that root starts (not thread-safe\n"
     "    by itself: one service per process uses one root). Returns the absolute root.\"\"\"\n"
     "    global PANEL_DIR, ROOT, STATE_DIR, LOG_DIR, STATIC_DIR, CONFIG_PATH, BLOCKS_PATH, NOTES_DIR\n"
     "    global QUEUE_PATH, SAVED_PATH, CHAT_PATH, HISTORY_PATH, AGENT_SESSION_PATH, SESSION_LOG\n"
     "    root = os.path.abspath(os.path.expanduser(str(path)))\n"
     "    PANEL_DIR = ROOT = root\n"
     "    STATE_DIR = os.path.join(root, 'state')\n"
     "    LOG_DIR = os.path.join(STATE_DIR, 'logs')\n"
     "    STATIC_DIR = os.path.join(root, 'static')\n"
     "    CONFIG_PATH = os.path.join(root, 'config.json')\n"
     "    BLOCKS_PATH = os.path.join(root, 'blocks.json')\n"
     "    NOTES_DIR = os.path.join(root, 'notes')\n"
     "    QUEUE_PATH = os.path.join(STATE_DIR, 'queue.json')\n"
     "    SAVED_PATH = os.path.join(STATE_DIR, 'saved_sequences.json')\n"
     "    CHAT_PATH = os.path.join(STATE_DIR, 'chat.json')\n"
     "    HISTORY_PATH = os.path.join(STATE_DIR, 'history.json')\n"
     "    AGENT_SESSION_PATH = os.path.join(STATE_DIR, 'agent_session.json')\n"
     "    SESSION_LOG = os.path.join(LOG_DIR, 'session.log')\n"
     "    return root\n"),
)

# runners: plain scripts (not a package), so their imports stay flat, pointing at paths.py
RUNNER_FIXES: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "run_block.py": (("import panel_paths as _P", "import paths as _P"),),
    "run_myscript.py": (("os.path.join(PANEL, 'panel_paths.py')", "os.path.join(PANEL, 'paths.py')"),),
    "myscript_guard.py": (),
    "guard_site/sitecustomize.py": (),
}

INIT_PY = '''"""Kelvin's own, vendored copy of the labMaster sequence engine (do not edit by hand).

Generated by `tools/vendor_sequence.py` from `../labMaster` (V1_SPEC.md, v1-A). It is the same
stdlib engine the labMaster panel runs, in Kelvin's own process: no HTTP, no 8770 port.

Reading and writing:
    data root   ~/Kelvin/sequence (KELVIN_SEQUENCE_DIR overrides): config.json, blocks.json,
                state/, data/, scripts/, notes/
    this folder the code, runners/ and VERSION (read-only, replaced only by the vendor tool)

The public way in is `service.py`: `service()` is one process-wide, thread-safe SequenceService.
"""
from .paths import PANEL_VERSION as __version__   # noqa: F401 - the engine's version ("0.1.7")

__all__ = ["__version__"]
'''


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_source(source: Path, name: str) -> bytes:
    path = source / name
    if not path.is_file():
        raise SystemExit("vendor_sequence: missing source file: %s" % path)
    return path.read_bytes()


def apply_fixes(text: str, fixes: Tuple[Tuple[str, str], ...], what: str) -> str:
    for old, new in fixes:
        if text.count(old) != 1:
            raise SystemExit("vendor_sequence: %s: expected exactly one %r, found %d"
                             % (what, old.splitlines()[0], text.count(old)))
        text = text.replace(old, new)
    return text


def lf(text: str) -> str:
    """One newline convention for the vendored files: LF, so the import lines match and the
    generated sha256 is the same on every machine."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def rewrite_package(src_name: str, text: str) -> str:
    """One vendored module: its imports as package-relative ones, then its own fixes."""
    text = lf(text)
    for pattern, replacement in IMPORT_REWRITES[src_name]:
        found = len(re.findall(pattern, text))
        if found != 1:
            raise SystemExit("vendor_sequence: %s: expected one %r, found %d"
                             % (src_name, pattern, found))
        text = re.sub(pattern, replacement, text)
    for pattern in LEFT_OVER:
        hit = re.search(pattern, text)
        if hit:
            raise SystemExit("vendor_sequence: %s: an absolute panel import is left: %r"
                             % (src_name, hit.group(0)))
    if src_name == "panel_paths.py":
        text = apply_fixes(text, PATHS_FIXES, src_name)
    if src_name == "panel_engine.py":
        text = apply_fixes(text, ENGINE_FIXES, src_name)
    return text


def rewrite_runner(src_name: str, text: str) -> str:
    return apply_fixes(lf(text), RUNNER_FIXES[src_name], src_name)


def build(source: Path) -> Dict[str, bytes]:
    """{relative destination: bytes} for the whole vendored package."""
    out: Dict[str, bytes] = {}
    for src_name, dest_name in PACKAGE_FILES.items():
        raw = read_source(source, src_name)
        if dest_name.endswith(".py"):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as e:
                raise SystemExit("vendor_sequence: %s is not UTF-8: %s" % (source / src_name, e))
            out[dest_name] = rewrite_package(src_name, text).encode("utf-8")
        elif dest_name == "VERSION":
            out[dest_name] = (raw.decode("utf-8-sig").strip().splitlines()[0].strip()
                              + "\n").encode("utf-8")
        else:
            out[dest_name] = raw
    for runner in RUNNER_FILES:
        raw = read_source(source, "runners/" + runner)
        text = raw.decode("utf-8")
        out["runners/" + runner] = rewrite_runner(runner, text).encode("utf-8")
    out["__init__.py"] = INIT_PY.encode("utf-8")
    return out


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Vendor labMaster's sequence engine into labharness/sequence/.")
    ap.add_argument("--source", default=str(DEFAULT_SOURCE), help="the labMaster source folder")
    ap.add_argument("--dest", default=str(DEFAULT_TARGET), help="the vendored package folder")
    ap.add_argument("--check", action="store_true",
                    help="write nothing; fail when the vendored copy is not current")
    args = ap.parse_args(argv)
    source, dest = Path(args.source).resolve(), Path(args.dest).resolve()
    if not (source / "panel_engine.py").is_file():
        raise SystemExit("vendor_sequence: %s is not a labMaster folder (no panel_engine.py)" % source)
    files = build(source)
    rows: List[Dict[str, object]] = []
    written = changed = 0
    for rel in sorted(files):
        data = files[rel]
        target = dest / rel
        digest = sha256(data)
        same = target.is_file() and target.read_bytes() == data
        rows.append({"path": rel.replace("\\", "/"), "sha256": digest, "bytes": len(data),
                     "same": same})
        if same:
            continue
        changed += 1
        if args.check:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written += 1
    digest = sha256("\n".join("%s %s" % (r["sha256"], r["path"]) for r in rows).encode("utf-8"))
    manifest = {"source": str(source), "dest": str(dest), "files": rows,
                "count": len(rows), "changed": changed, "digest": digest,
                "source_version": files.get("VERSION", b"").decode("utf-8").strip()}
    if args.check:
        for row in rows:
            print("%s  %s" % (row["sha256"], row["path"]))
        print(json.dumps(manifest, indent=2))
        if changed:
            print("vendor_sequence: %d file(s) are out of date under %s" % (changed, dest),
                  file=sys.stderr)
            return 1
        print("vendor_sequence: %d file(s) current under %s (digest %s)"
              % (len(rows), dest, digest))
        return 0
    for row in rows:
        print("%s  %s  (%d bytes)%s" % (row["sha256"], row["path"], row["bytes"],
                                        "" if row["same"] else "   [written]"))
    manifest["generated_by"] = "tools/vendor_sequence.py"
    (dest / "VENDORED.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("vendor_sequence: %d file(s), %d written, engine %s, digest %s"
          % (len(rows), written, manifest["source_version"], digest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
