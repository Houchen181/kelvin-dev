"""The arms: which Kelvin code answers, and how the model reaches the lab.

    kelvin       Kelvin's own chat (its portal, in-process) at a git ref, with the model through Kelvin's provider
    claude-code  Claude Code (claude -p) with Kelvin's MCP server (kelvin-mcp's build_server) at a git ref

Each arm's code is a snapshot of the ref (git archive of src/ and tools/labsim) kept under <root>/arms/<commit>, so a
run names exactly what it measured and a branch moving on later doesn't change it. A ref of "." is the working tree
as it is (uncommitted changes included), for trying a change before committing it.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

REPO = Path(__file__).resolve().parents[2]

DEFAULT_ARMS: Dict[str, Dict[str, str]] = {
    "main": {"kind": "kelvin", "ref": "refs/remotes/origin/main", "about": "Kelvin as released (origin/main)"},
    "harness": {"kind": "kelvin", "ref": "refs/heads/harness-fixes", "about": "the chat-harness fixes of 2026-11-12"},
    "claude-code": {"kind": "claude-code", "ref": "refs/remotes/origin/main",
                    "about": "Claude Code with Kelvin's MCP server (kelvin-mcp) as a reference"},
}
PATHS = ["src", "tools/labsim"]


@dataclass
class Arm:
    name: str
    kind: str
    ref: str
    about: str = ""
    commit: str = ""
    src: Optional[Path] = None

    def describe(self) -> str:
        return "%s (%s at %s %s)" % (self.name, self.kind, self.ref, self.commit[:10])


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO)] + list(args), check=True, capture_output=True,
                          text=True).stdout.strip()


def parse(spec: str) -> List[Arm]:
    """Arms by name ("main,harness,claude-code") or as name=ref or name=ref:kind ("mine=my-branch")."""
    out = []
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        if "=" in part:
            name, _, rest = part.partition("=")
            ref, _, kind = rest.partition(":")
            out.append(Arm(name, kind or "kelvin", ref, "%s at %s" % (kind or "kelvin", ref)))
        elif part in DEFAULT_ARMS:
            d = DEFAULT_ARMS[part]
            out.append(Arm(part, d["kind"], d["ref"], d["about"]))
        else:
            raise KeyError("no arm %r: use %s, or name=ref[:kind]" % (part, ", ".join(DEFAULT_ARMS)))
    return out


def snapshot(arm: Arm, root: Path) -> Arm:
    """The arm's code on disk: <root>/arms/<commit>/src (extracted once), or the working tree's src for ref "."."""
    if arm.ref == ".":
        arm.commit = "worktree-" + git("rev-parse", "--short", "HEAD")
        arm.src = REPO / "src"
        return arm
    arm.commit = git("rev-parse", arm.ref + "^{commit}")
    dest = root / "arms" / arm.commit[:12]
    if not (dest / "src" / "labharness").is_dir():
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        data = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", arm.commit] + PATHS,
                              check=True, capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(tmp, filter="data")
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)
    arm.src = dest / "src"
    return arm
