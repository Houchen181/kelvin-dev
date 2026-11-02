"""What Kelvin's VI reader can't name yet, in a folder of VIs: each block-diagram node whose heap class has no name
(classN), each LabVIEW function (primResID) not in vigraph.PRIMS, and each terminal's data object of a class with no
name, counted, with one example of each: the VI, its label, and its terminals (the kind of each, and whether a wire
comes in or goes out). Only reads; nothing is run or written.

    python tools/vi_census.py <folder>
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

from labharness import vifile, vigraph

PATTERNS = ("*.vi", "*.vit", "*.ctl")


def terminals(g: vigraph.Graph, vi: vifile.VI, node: vifile.Obj) -> List[str]:
    """A node's terminals as "in number", "out string", "- cluster" (not wired)."""
    out = []
    for t in g.terms(node):
        dco = g.term_dco.get(t)
        kind = vifile.kind_of(vi.types.of_heap(dco.number("typeDesc"))) if dco is not None else "?"
        way = "in" if t in g.source else "out" if t in g.sinks else "-"
        out.append("%s %s" % (way, kind or "?"))
    return out


def census(folder: Path) -> Tuple[Dict[str, Counter], Dict[str, Dict[Any, Tuple[str, str, List[str]]]], List[str]]:
    counts: Dict[str, Counter] = {"class": Counter(), "prim": Counter(), "dco": Counter()}
    examples: Dict[str, Dict[Any, Tuple[str, str, List[str]]]] = {"class": {}, "prim": {}, "dco": {}}
    unread: List[str] = []
    files = sorted({p for pattern in PATTERNS for p in folder.rglob(pattern)})
    for path in files:
        try:
            vi = vifile.read(path)
            with vifile.text_encoding(vi.encoding):
                g = vigraph.Graph(vi)
                where = str(path.relative_to(folder))
                for node, _ in g.nodes():
                    if node.kind.startswith("class"):
                        key: Any = node.cls
                        group = "class"
                    elif node.kind == "prim" and (node.number("primResID") or 0) not in vigraph.PRIMS:
                        key = node.number("primResID") or 0
                        group = "prim"
                    else:
                        group = ""
                    if group:
                        counts[group][key] += 1
                        examples[group].setdefault(key, (where, vifile.label_of(node), terminals(g, vi, node)))
                for t, dco in g.term_dco.items():
                    if dco.kind.startswith("class"):
                        counts["dco"][dco.cls] += 1
                        node = g.term_node.get(t)
                        examples["dco"].setdefault(dco.cls, (where, node.kind if node is not None else "", []))
        except Exception as e:   # a census, not a reader: say the file and go on
            unread.append("%s: %s" % (path, e))
    return counts, examples, unread


def main(argv: List[str]) -> int:
    if len(argv) != 2 or not Path(argv[1]).is_dir():
        print(__doc__)
        return 2
    folder = Path(argv[1])
    counts, examples, unread = census(folder)
    titles = {"class": "Diagram nodes of a heap class with no name", "prim": "LabVIEW functions not in PRIMS",
              "dco": "Terminals' data objects of a class with no name (example: on a node of kind)"}
    for group in ("class", "prim", "dco"):
        print("== %s: %d kinds" % (titles[group], len(counts[group])))
        for key, n in counts[group].most_common():
            where, label, terms = examples[group][key]
            print("  %-6s x%-5d %s%s  [%s]" % (key, n, where, " (%s)" % label if label else "", ", ".join(terms)))
    for line in unread:
        print("not read:", line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
