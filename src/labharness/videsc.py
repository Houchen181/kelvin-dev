"""A VI's full description, for the AI that translates it into a Kelvin experiment (vifile.py reads the file,
vigraph.py rebuilds its wires): everything on its front panel and block diagram, with nothing followed or folded the
way the VI summary does (vigraph.summary keeps what a scan needs; this keeps all of it).

What it holds:
- the front panel: each control and indicator with an id (c<uid>), its type, unit, saved value, choices and place on
  the connector pane; "live" when the diagram reads it inside a loop (a person changes it while the VI runs: a Stop
  button, a set point), "plotted" when it is a chart or graph;
- the block diagram as diagrams of nodes: each node with an id (n<uid>), its kind and name (LabVIEW's functions by
  name where Kelvin knows them, vigraph.PRIMS) and its terminals (t<uid>: which way the wire goes, its type);
  structures (loops, cases, sequences, events) with every frame, each a diagram of its own, a case's frames under
  the values that pick them; each diagram's sources (constants with their values, the panel's terminals, local
  variables as the control they name) and wires (from one terminal to others);
- what it couldn't name (heap classes and functions Kelvin doesn't know), for a person to see and the translator to
  treat with care.

The ids are the heap's own (uid), the same each time the file is read, so a translation can say which node each of
its lines comes from. Nothing is ever run; a file that can't be read, or a diagram bigger than Kelvin follows, is
said in unread, never raised.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import vifile, vigraph
from .vifile import Obj
from .viterms import TERMINALS

DESCRIPTION = 1
MAX_DESC_DEPTH = 6        # the lab's own sub-VIs described, calls within calls
MAX_FILES = 400           # sub-VI files read for one description (its sub-VIs' and their drivers')
LOOPS = {"forLoop": "for", "whileLoop": "while", "timeLoop": "timed"}
SEQUENCES = {"sequence": False, "sequenceFrame": True, "timeFlatSequenceFrame": True}
FEEDBACK = ("leftFeedback", "rightFeedback", "initFeedback", "hiddenFBNode", "slaveFBInputNode")
# LabVIEW's functions that are heap classes of their own (not prim with a primResID)
FUNCTIONS = {"aIndx": "Index Array", "aBuild": "Build Array", "cABuild": "Build Array", "mux": "Bundle",
             "demux": "Unbundle", "concat": "Concatenate Strings", "printf": "Format Into String",
             "scanf": "Scan From String", "cpdArith": "Compound Arithmetic", "aReplace": "Replace Array Subset",
             "aInsert": "Insert Into Array", "aDelete": "Delete From Array", "aInit": "Initialize Array",
             "aReshape": "Reshape Array", "subset": "Array Subset", "decimate": "Decimate 1D Array",
             "interLeave": "Interleave 1D Arrays", "exprNode": "Expression Node", "mergeErrors": "Merge Errors"}
TUNNELS = {"lpTun": "tunnel", "innerLpTun": "tunnel", "matedLpTun": "tunnel", "selTun": "tunnel",
           "seqTun": "tunnel", "flatSeqTun": "tunnel", "matedSeqTun": "tunnel", "sdfTun": "tunnel",
           "externalTun": "tunnel", "xTunnel": "tunnel", "regionTun": "tunnel", "commentTun": "tunnel",
           "sLoc": "sequence_local", "lSR": "shift_register", "matedLSR": "shift_register",
           "rSR": "shift_register", "matedRSR": "shift_register"}
OWN_TERMINALS = ("lCnt", "lTst", "lMax", "caseSel")   # a loop's count, condition and number, a case's selector
POLYMORPHIC = "it is a polymorphic VI: where it is called, one of its instances runs (%s), each a VI read on its own"


# Compound Arithmetic's operation (its first terminal's dcoFiller), as lvkit read them; any other code is said as
# its number, never guessed
COMPOUND = {1: "or", 2: "and", 256: "add"}
INVERTED = 0x10000   # a data object's objFlags bit: a Compound Arithmetic input inverted; on a loop's condition (lTst),
#                      set: it stops if true, clear: it continues if true (lvkit, docs/_internal/xml-schema)


def _same(o: Optional[Obj]) -> Optional[Obj]:
    return o


def stops_on_true(loop: Obj, resolve: Callable[[Optional[Obj]], Optional[Obj]] = _same) -> Optional[bool]:
    """Does this loop stop when its condition is true (LabVIEW's default, Stop if True), or continue while it is
    (Continue if True)? None for a loop without a condition (a For loop that runs its count)."""
    test = resolve(loop.get("loopTestDCO"))
    if test is None:
        return None
    return bool((test.number("objFlags", signed=False) or 0) & INVERTED)


def compound(node: Obj, resolve: Callable[[Optional[Obj]], Optional[Obj]] = _same) -> Tuple[str, List[int]]:
    """A Compound Arithmetic node's operation, and the uids of the inputs it inverts first."""
    op, inverted = "", []
    for i, term in enumerate(node.items("termList")):
        dco = resolve(term.get("dco"))
        if dco is None:
            continue
        if i == 0:
            code = dco.number("dcoFiller", signed=False)
            op = COMPOUND.get(code, "code %s" % code) if code is not None else ""
        if (dco.number("objFlags", signed=False) or 0) & INVERTED and term.uid is not None:
            inverted.append(term.uid)
    return op, inverted


def _tid(t: int) -> str:
    return "t%d" % t


def _empty(path: Any, why: str = "") -> Dict[str, Any]:
    p = Path(str(path)) if path else None
    return {"description": DESCRIPTION, "name": p.name if p else "", "path": str(p) if p else "", "labview": "",
            "sha256": "", "read": "no", "unread": ["not read: %s" % why] if why else [], "panel": [],
            "diagram": None, "calls": {}, "subvis": {}, "unknown": {"kinds": [], "prims": []}}


def _sha256(path: Any) -> str:
    try:
        p = Path(str(path))
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""
    except OSError:
        return ""


class _Describer:
    """Describes one VI's panel and diagram, with the reader the VI summary uses (its order of nodes, its cases'
    values, its sub-VIs' names) and the budget the summary has (vigraph._Shared)."""

    def __init__(self, vi: vifile.VI, link: vigraph.Link, shared: vigraph._Shared):
        self.vi = vi
        self.reader = vigraph._Reader(vi, link, 0, shared.files, shared=shared)
        self.g = self.reader.g
        self.shared = shared
        self.controls = {c.uid: c for c in vi.controls if c.uid is not None}
        self.by_ddo = {c.ddo: c for c in vi.controls if c.ddo is not None}
        self.by_name: Dict[str, List[vifile.Control]] = {}
        for c in vi.controls:
            self.by_name.setdefault(c.name, []).append(c)
        self.in_loops: Set[str] = set()          # the controls the diagram reads or writes inside a loop
        self.written: Set[str] = set()           # the panel's terminals a wire writes to inside a loop
        self.kinds: Set[str] = set()             # heap classes without a name
        self.prims: Set[int] = set()             # functions without a name
        self.calls: Dict[str, Dict[str, Any]] = {}
        self.own: Dict[str, vifile.VI] = {}      # the lab's own sub-VIs it calls, by name
        self.wired: Dict[int, List[int]] = {}    # each diagram's wires, by the diagram's uid: their sources
        for src in self.g.sinks:
            d = self.g.term_diagram.get(src)
            if d is not None and d.uid is not None:
                self.wired.setdefault(d.uid, []).append(src)

    # -------------------------------------------------- diagrams

    def diagram(self, d: vigraph.Diagram, in_loop: bool, level: int, out: Optional[Dict[str, Any]] = None
                ) -> Dict[str, Any]:
        """A diagram, filled in place (what is built stays, should the budget run out partway)."""
        out = out if out is not None else {}
        out.update({"id": "d%s" % d.obj.uid, "label": out.get("label", []), "sources": [], "nodes": [],
                    "wires": []})
        if level > vigraph.MAX_DEPTH:
            self.reader.gap("structures nested more than %d deep" % vigraph.MAX_DEPTH)
            return out
        if d.holder is not None:
            for t in self.g.terms(d.holder):
                source = self.source(t, in_loop)
                if source is not None:
                    out["sources"].append(source)
        for src in self.wired.get(d.obj.uid, []):
            out["wires"].append({"from": _tid(src), "to": [_tid(s) for s in self.g.sinks.get(src, [])]})
        for n in self.reader.order(d):
            if n.kind == "gRef":
                out["sources"] += [self.local(t, in_loop) for t in self.g.terms(n)]
                continue
            node: Dict[str, Any] = {}
            out["nodes"].append(node)
            self.node(n, in_loop, level, node)
        return out

    def source(self, t: int, in_loop: bool) -> Optional[Dict[str, Any]]:
        """A terminal on a diagram's own holder: a constant or one of the panel's terminals (a tunnel's or a loop's
        own terminal is said with its structure)."""
        dco = self.g.term_dco.get(t)
        if dco is None:
            return None
        if dco.kind == "bDConstDCO":
            v = self.reader.constant(dco)
            out = {"id": _tid(t), "constant": v.value if v.kind == "value" else None, "type": self.reader._type(t)}
            if v.kind != "value":
                out["unread"] = v.value
            return out
        if dco.kind == "fPDCO":
            cid = "c%s" % dco.uid
            if in_loop:
                self.in_loops.add(cid)
                if t in self.g.source:
                    self.written.add(cid)
            return {"id": _tid(t), "control": cid}
        if dco.kind in TUNNELS or dco.kind in OWN_TERMINALS:
            return None
        if dco.kind.startswith("class"):
            self.kinds.add(dco.kind)
        return {"id": _tid(t), "of": dco.kind}   # a terminal of a kind Kelvin doesn't say more about

    def local(self, t: int, in_loop: bool) -> Dict[str, Any]:
        """A local variable: the control its type is named after (its type keeps the control's label)."""
        dco = self.g.term_dco.get(t)
        t_ = self.vi.types.of_heap(dco.number("typeDesc")) if dco is not None else None
        label = (t_.label if t_ is not None else "") or ""
        found = self.by_name.get(label) or self.by_name.get(label.strip()) or \
            [c for c in self.vi.controls if c.name.strip() == label.strip()]
        out: Dict[str, Any] = {"id": _tid(t), "local": True, "control": None}
        if len(found) == 1 and found[0].uid is not None:
            out["control"] = "c%d" % found[0].uid
            if in_loop:
                self.in_loops.add(out["control"])
                if t in self.g.source:
                    self.written.add(out["control"])
        else:
            out["named"] = label
        return out

    # -------------------------------------------------- nodes

    def terms(self, n: Obj, parms: bool = False) -> List[Dict[str, Any]]:
        out = []
        for t in self.g.terms(n):
            entry = {"id": _tid(t), "dir": "in" if t in self.g.source else "out" if t in self.g.sinks else "none",
                     "type": self.reader._type(t), "name": "", "error": self.reader._is_error(t)}
            if parms:
                entry["parm"] = self.g.parm(t)
            out.append(entry)
        return out

    def node(self, n: Obj, in_loop: bool, level: int, out: Dict[str, Any]) -> None:
        k = n.kind
        out.update({"id": "n%s" % n.uid, "kind": "other", "name": "", "terms": self.terms(n, k == "prim")})
        subs = self.g.subdiagrams(n)
        if k in LOOPS:
            out.update(kind="loop", loop=LOOPS[k], name=vifile.label_of(n), count=None, index=None, stop=None)
            limit = self.g.resolve(n.get("loopLimitDCO"))
            ends = self.g.ends(limit) if limit is not None else []
            out["count"] = _tid(ends[-1]) if ends else None
            index = self.g.inner_term(self.g.resolve(n.get("loopIndexDCO")))
            out["index"] = _tid(index) if index is not None else None
            stop = self.g.inner_term(self.g.resolve(n.get("loopTestDCO")))
            out["stop"] = _tid(stop) if stop is not None else None
            out["stops_on"] = stops_on_true(n, self.g.resolve)
            out["tunnels"] = self.tunnels(n)
            if len(ends) > 1:   # the count's value inside the loop (N)
                out["tunnels"].append({"outer": _tid(ends[-1]), "inner": [_tid(e) for e in ends[:-1]],
                                       "kind": "count"})
            out["frames"] = self.frames(subs, True, level)
        elif k == "select":
            out.update(kind="case", selector=None)
            sel = self.g.resolve(n.get("caseSelDCO"))
            ends = self.g.ends(sel) if sel is not None else []
            out["selector"] = _tid(ends[-1]) if ends else None
            out["tunnels"] = self.tunnels(n)
            if len(ends) > 1:   # the selector's value inside each frame
                out["tunnels"].append({"outer": _tid(ends[-1]), "inner": [_tid(e) for e in ends[:-1]],
                                       "kind": "selector"})
            out["frames"] = self.frames(subs, in_loop, level, self.reader.case_names(n))
        elif k == "eventStruct":
            out.update(kind="event")
            timeout = self.g.resolve(n.get("timeOutDCO"))
            ends = self.g.ends(timeout) if timeout is not None else []
            if ends:
                out["timeout"] = _tid(ends[-1])
            out["tunnels"] = self.tunnels(n)
            out["frames"] = self.frames(subs, in_loop, level, self.events(n, len(subs)))
        elif k in SEQUENCES:
            out.update(kind="sequence", flat=SEQUENCES[k], tunnels=self.tunnels(n) + self.locals_of(subs))
            out["frames"] = self.frames(subs, in_loop, level)
        elif k == "prim":
            rid = n.number("primResID") or 0
            name = vigraph.PRIMS.get(rid, "")
            out.update(kind="prim", prim=rid, name=name, known=bool(name))
            if not name:
                self.prims.add(rid)
            names = TERMINALS.get(rid, {})
            for term in out["terms"]:   # x and y, the selector, the value if true: by LabVIEW's names for them
                term["name"] = names.get(term["parm"], "") if term["parm"] is not None else ""
        elif k in vigraph.CALLS:
            name, link = self.reader._call_name(n)
            sub = self.reader.subvi(link) if link is not None else None
            if n.kind == "polyIUse" and link is not None:   # the instance of a polymorphic VI the call runs
                sub, chosen = self.reader.instance(n, sub, link)
                if chosen.name and chosen.name != name:
                    out["polymorphic"] = name
                    name, link = chosen.name, chosen
            out.update(kind="call", call=name, name=name)
            names = self.reader._terminals(sub)
            for i, term in enumerate(out["terms"]):   # each terminal by the sub-VI's own name for it
                c = names.get(i)
                if c is not None:
                    term["name"] = c.name
            site = next((x for x in self.reader.call(n, level) if x.get("node") == "call"), {})
            out["sends"] = site.get("sends") or []
            if site.get("sends_unread"):
                out["sends_unread"] = site["sends_unread"]
            self.called(name, link, sub, out)
        elif k == "extFunc":
            path = vifile._path(n.raw("libPath") or b"", 0)[0] or []
            library = vifile.path_text(path) if path else ""
            out.update(kind="call_library", library=library,
                       function=vifile._text(n.raw("nodeName") or b"") or vifile.label_of(n))
            key = library or out["function"]
            self.calls.setdefault(key, {"name": key, "kind": "dll", "maker": "", "model": "", "library": library,
                                        "found": "", "sends": [], "sends_unread": ""})
        elif k == "propNode":
            items = [vifile._text(bytes(o.value)) for o in n.walk()
                     if o.tag == vifile.TAG["PropItemName"] and isinstance(o.value, (bytes, bytearray))]
            out.update(kind="property", of=vifile._text(n.raw("nodeName") or b"") or "an object", items=items)
        elif k == "invokeNode":
            methods = [vifile._text(bytes(o.value)) for o in n.walk()
                       if o.tag == vifile.TAG["methName"] and isinstance(o.value, (bytes, bytearray))]
            out.update(kind="invoke", of=vifile._text(n.raw("nodeName") or b"") or "an object",
                       method=", ".join(m for m in methods if m))
        elif k == "fBox":
            text = next((t for t in (vifile.text_of(o) for o in n.walk() if o.get("textRec") is not None)
                         if "=" in t), "")
            out.update(kind="formula", text=text)
        elif k in FUNCTIONS or k == "nMux":
            if k == "nMux":   # by name: a cluster and the fields to put in it, or a cluster and the fields taken out
                ins = [t for t in out["terms"] if t["dir"] == "in" and not t["error"]]
                name = "Bundle By Name" if len(ins) >= 2 else "Unbundle By Name"
            else:
                name = FUNCTIONS[k]
            out.update(kind="function", name=name)
            if k == "cpdArith":
                op, inverted = compound(n, self.g.resolve)
                out.update(op=op, inverted=[_tid(t) for t in inverted])
            if k in ("mux", "demux", "nMux"):   # each field by its name (the type of its terminal keeps it)
                for t, term in zip(self.g.terms(n), out["terms"]):
                    term["name"] = self.type_label(t).strip()
        elif k in FEEDBACK:
            out.update(kind="feedback", name="Feedback Node")
        else:
            words = [x.get("what") for x in self.reader.step(n, level) if x.get("node") == "other"] \
                if not subs else []
            out.update(what=words[0] if words else k)
            if k.startswith("class"):
                self.kinds.add(k)
            if subs:
                out["tunnels"] = self.tunnels(n)
                out["frames"] = self.frames(subs, in_loop, level)

    def called(self, name: str, link: Optional[vifile.Link], sub: Optional[vifile.VI], site: Dict[str, Any]) -> None:
        """A VI the diagram calls, once in calls: what kind of VI it is (the lab's own, an instrument driver's,
        LabVIEW's own, a class's method), its maker and model, where it was found, and the commands its calls
        send. The lab's own are kept in own, to be described in full."""
        entry = self.calls.get(name)
        if entry is None:
            where = link.where if link is not None else ""
            info = self.reader.link(name, where)
            library = info.get("library") or name.rpartition(":")[0]
            if link is None:
                kind = "unknown"
            elif where == "vi.lib":
                kind = "vilib"
            elif where == "LabVIEW":
                kind = "labview"
            elif library.lower().endswith(".lvclass"):
                kind = "class"
            elif info.get("maker") or info.get("model"):
                kind = "driver"
            else:
                kind = "own" if sub is not None else "unknown"
            found = ""
            if sub is not None and sub.path:
                try:
                    found = Path(sub.path).relative_to(self.shared.folder).as_posix()
                except ValueError:
                    found = Path(sub.path).name
            entry = self.calls[name] = {"name": name, "kind": kind, "maker": info.get("maker", ""),
                                        "model": info.get("model", ""), "library": library, "found": found,
                                        "sends": [], "sends_unread": ""}
            if kind == "own" and sub is not None:
                self.own[name] = sub
        texts = {s.get("text") for s in entry["sends"]}
        entry["sends"] += [s for s in site.get("sends", []) if s.get("text") not in texts]
        if site.get("sends_unread") and not entry["sends_unread"]:
            entry["sends_unread"] = site["sends_unread"]

    def frames(self, subs: List[vigraph.Diagram], in_loop: bool, level: int,
               labels: Optional[List[List[str]]] = None) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for i, sub in enumerate(subs):
            frame: Dict[str, Any] = {"label": list(labels[i]) if labels and i < len(labels) else []}
            out.append(frame)
            self.diagram(sub, in_loop, level + 1, frame)
        return out

    def tunnels(self, n: Obj) -> List[Dict[str, Any]]:
        """A structure's tunnels, each once: its terminal outside, those inside (one per frame), and its kind. A
        shift register is one entry: the values it starts with (inits, outside: one per left terminal, stacked
        ones reading earlier steps), what the loop reads of it each step (read), what it writes (write), and what
        comes out after the last step (out)."""
        out, seen = [], set()
        for t in self.g.terms(n):
            dco = self.g.term_dco.get(t)
            if dco is None or dco.kind not in TUNNELS or dco.uid in seen:
                continue
            if dco.kind in ("rSR", "matedRSR"):
                lefts = [x for x in (self.g.resolve(e) for e in dco.items("lsrDCOList")) if x is not None]
                if lefts:
                    seen.update({dco.uid} | {x.uid for x in lefts})
                    out.append(self.register(dco, lefts))
                    continue
            elif dco.kind in ("lSR", "matedLSR"):
                right = self.g.resolve(dco.get("rsrDCO"))
                if right is not None:
                    lefts = [x for x in (self.g.resolve(e) for e in right.items("lsrDCOList")) if x is not None]
                    seen.update({right.uid} | {x.uid for x in lefts} | {dco.uid})
                    out.append(self.register(right, lefts or [dco]))
                    continue
            seen.add(dco.uid)
            ends = self.g.ends(dco)
            if not ends:
                continue
            kind = TUNNELS[dco.kind]
            if vigraph.indexing(dco):
                kind = "indexing"
            entry: Dict[str, Any] = {"outer": _tid(ends[-1]), "inner": [_tid(e) for e in ends[:-1]], "kind": kind}
            if kind == "shift_register":   # a half without its other (not seen on LabVIEW's own files)
                entry["side"] = "right" if dco.kind in ("rSR", "matedRSR") else "left"
            out.append(entry)
        return out

    def register(self, right: Obj, lefts: List[Obj]) -> Dict[str, Any]:
        ends = [self.g.ends(x) for x in lefts]
        inits = [_tid(e[-1]) for e in ends if e]
        read = [_tid(i) for e in ends for i in e[:-1]]
        r = self.g.ends(right)
        write = _tid(r[0]) if len(r) > 1 else None
        entry: Dict[str, Any] = {"outer": inits[0] if inits else None, "inner": read + ([write] if write else []),
                                 "kind": "shift_register", "inits": inits, "read": read, "write": write,
                                 "out": _tid(r[-1]) if r else None}
        return entry

    def type_label(self, t: int) -> str:
        """The label a terminal's type keeps (a cluster's field, a local's control, Event Filter Data)."""
        dco = self.g.term_dco.get(t)
        ty = self.vi.types.of_heap(dco.number("typeDesc")) if dco is not None else None
        return (ty.label if ty is not None else "") or ""

    def locals_of(self, subs: List[vigraph.Diagram]) -> List[Dict[str, Any]]:
        """A stacked sequence's locals: a value one frame sets and later frames read, each with its terminal in
        every frame (no terminal outside the sequence: outer None)."""
        out, seen = [], set()
        for sub in subs:
            for t in self.g.terms(sub.holder) if sub.holder is not None else []:
                dco = self.g.term_dco.get(t)
                if dco is None or dco.kind != "sLoc" or dco.uid in seen:
                    continue
                seen.add(dco.uid)
                out.append({"outer": None, "inner": [_tid(e) for e in self.g.ends(dco)] or [_tid(t)],
                            "kind": "sequence_local"})
        return out

    def events(self, n: Obj, frames: int) -> List[List[str]]:
        """Each frame of an event structure: the events it handles. Each event names its frame (0), its kind (1:
        3 a value change, its data OldVal and NewVal), its source (5: 4 a control, by its display object in 6; 2
        this VI; 1 the timeout) — read off the JKI dialog's (tests/fixtures), whose this-VI frame is a filter
        (Event Filter Data, Discard?) and whose remaining frame, frame 0, is LabVIEW's Timeout. Anything else is
        said by its numbers."""
        subs = self.g.subdiagrams(n)
        out: List[List[str]] = [[] for _ in range(frames)]
        for spec in n.items("EventNodeEvents"):
            f = {x.tag: int.from_bytes(x.value, "big", signed=True) for x in spec.kids
                 if isinstance(x.value, (bytes, bytearray)) and x.value}
            frame, kind, source = f.get(0, -1), f.get(1), f.get(5)
            if not 0 <= frame < frames:
                continue
            c = self.by_ddo.get(f.get(6))
            if c is not None:
                words = "an event of %s%s" % (c.name, " (Value Change)" if kind == 3 else "")
            elif source == 1:
                words = "timeout"
            elif source == 2:
                filtered = frame < len(subs) and any(
                    x.kind == "eventDataNode" and any("Filter" in self.type_label(t) for t in self.g.terms(x))
                    for x in subs[frame].nodes)
                words = "an event of this VI" + (" (a filter: Discard? decides whether it goes on)" if filtered else "")
            else:
                words = "an event (source %s, kind %s)" % (source, kind)
            out[frame].append(words)
        return out


def _panel(vi: vifile.VI) -> List[Dict[str, Any]]:
    """The front panel's controls and indicators, each with its id (live: set once the diagram is read)."""
    return [{"id": "c%d" % c.uid, **vigraph._control(c), "slot": c.terminal, "live": False,
             "plotted": c.cls == "stdGraph"} for c in vi.controls if c.uid is not None]


def _describe(vi: vifile.VI, link: vigraph.Link, shared: vigraph._Shared, sha: str = "", depth: int = 0,
              subvis: Optional[Dict[str, Optional[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    """A VI's description, never an error: what was built before a problem (or the budget running out) is kept,
    and the problem said in unread. read and unread as the VI summary has them. subvis: the lab's own sub-VIs
    described so far for the top VI, by name (None while one is being described), each described once, at any depth:
    the top VI's description holds them all, theirs none."""
    top = subvis is None
    subvis = {} if subvis is None else subvis
    out = _empty(vi.path)
    out.update(name=vi.name, labview=vi.labview, sha256=sha, read="names", unread=list(vi.unread))
    d: Optional[_Describer] = None
    with vifile.text_encoding(vi.encoding):
        try:
            if vi.panel is not None:
                out["read"] = "panel"
                out["panel"] = _panel(vi)
                out["unread"] += ["the saved value of %s isn't a finite number (Inf or NaN): left out" % c.name
                                  for c in vi.controls if c.has_default and not vigraph.all_finite(c.default)]
            d = _Describer(vi, link, shared)
            if vi.instances:
                out["unread"].append(POLYMORPHIC % ", ".join(vi.instances[:12]))
            elif d.g.root is not None:
                out["read"] = "full" if vi.panel is not None else "names"
                out["diagram"] = {}
                try:
                    d.diagram(d.g.root, False, 0, out["diagram"])
                finally:
                    out["unread"] += d.reader.gaps
                    for c in out["panel"]:
                        c["live"] = not c["indicator"] and c["id"] in d.in_loops
                        # a chart or graph, or an array the loop writes as it goes (a sweep's readings)
                        c["plotted"] = c["plotted"] or c["indicator"] and c["type"] == "array" and c["id"] in d.written
                    out["unknown"] = {"kinds": sorted(d.kinds), "prims": sorted(d.prims)}
                    out["calls"] = d.calls
                for name, sub in d.own.items():
                    if name in subvis:
                        continue
                    if depth + 1 > MAX_DESC_DEPTH:
                        out["unread"].append("%s: the lab's VIs called more than %d deep aren't described"
                                             % (name, MAX_DESC_DEPTH))
                        continue
                    subvis[name] = None   # being described: a call back up the chain is a call, not a loop
                    subvis[name] = _describe(sub, link, shared, _sha256(sub.path), depth + 1, subvis)
            elif vi.diagram is not None:
                out["unread"].append("its block diagram: its layout wasn't one Kelvin reads")
        except vigraph.TooBig as e:   # as the summary says it: what was read is kept, and it isn't all of it
            out["read"] = "panel" if vi.panel is not None else "names"
            out["unread"].append(str(e))
            left = [name for name in (d.own if d is not None else {}) if not subvis.get(name)]
            if left:
                out["unread"].append("the lab's sub-VIs it calls not described before that: %s" % ", ".join(left))
        except Exception as e:   # a diagram laid out in a way no reader foresaw: said, never raised
            out["unread"].append("its block diagram: it couldn't be described (%s)" % type(e).__name__)
    if top:
        out["subvis"] = {k: v for k, v in subvis.items() if v is not None}
    out, n = vigraph.plain_numbers(out)
    if n:
        out["unread"].append("%d value%s that aren't finite numbers (Inf or NaN): left out" % (n, "" if n == 1 else "s"))
    return out


# ------------------------------------------------------------------ as text, for a model

TEXT_VALUE = 60           # characters of a constant or saved value the text writes
TEXT_SENDS = 8            # commands a call's line and the calls list name



def _short(x: Any) -> str:
    text = repr(x)
    return text if len(text) <= TEXT_VALUE else text[:TEXT_VALUE - 1] + "…"


def _is_error(c: Dict[str, Any]) -> bool:
    """An error cluster on the panel (error in, error out): left out of the text, as its wires are."""
    return c.get("type") == "cluster" and "error" in str(c.get("name", "")).lower()


def _all_diagrams(desc: Dict[str, Any]) -> List[Dict[str, Any]]:
    out, todo = [], [desc["diagram"]] if desc.get("diagram") else []
    while todo:
        d = todo.pop()
        out.append(d)
        for n in d["nodes"]:
            todo += n.get("frames", [])
    return out


class _Text:
    """One description written as text: its panel, its diagram node by node (each input with where its value comes
    from), its calls. prefix: put before each id (a sub-VI's, s1.), so ids from different files never meet."""

    def __init__(self, desc: Dict[str, Any], prefix: str = ""):
        self.d, self.p = desc, prefix
        self.lines: List[str] = []
        self.src_of: Dict[str, str] = {}
        self.literal: Dict[str, str] = {}
        self.control: Dict[str, str] = {}
        self.owner: Dict[str, str] = {}
        self.on_nodes: Set[str] = set()
        self.errors: Set[str] = set()
        errors = {c["id"] for c in desc["panel"] if _is_error(c)}
        for dg in _all_diagrams(desc):
            for w in dg["wires"]:
                for sink in w["to"]:
                    self.src_of[sink] = w["from"]
            for s in dg["sources"]:
                if "constant" in s:
                    self.literal[s["id"]] = _short(s["constant"]) if "unread" not in s else "<%s>" % s["unread"]
                elif s.get("control"):
                    self.control[s["id"]] = s["control"]
                    if s["control"] in errors:
                        self.errors.add(s["id"])
                elif s.get("local"):   # a local variable whose control wasn't told: by the name its type keeps
                    self.literal[s["id"]] = 'local "%s"' % s.get("named", "")
            for n in dg["nodes"]:
                for t in n["terms"]:
                    self.on_nodes.add(t["id"])
                    if t["error"]:
                        self.errors.add(t["id"])
                    if "frames" not in n:
                        self.owner[t["id"]] = n["id"]

    def expr(self, t: Optional[str]) -> str:
        """A terminal as an input names it: a constant's value, a control, a node's output (n12.t34), or itself."""
        if t is None:
            return "?"
        if t in self.literal:
            return self.literal[t]
        if t in self.control:
            return self.p + self.control[t]
        if t in self.owner:
            return "%s%s.%s" % (self.p, self.owner[t], t)
        return self.p + t

    def src(self, sink: str) -> str:
        return self.expr(self.src_of.get(sink))

    def write(self) -> List[str]:
        d, p = self.d, self.p
        inputs = [c for c in d["panel"] if not c["indicator"] and not _is_error(c)]
        outputs = [c for c in d["panel"] if c["indicator"] and not _is_error(c)]
        if inputs:
            self.lines.append("inputs:")
            for c in inputs:
                value = " = %s" % _short(c["default"]) if c.get("default") is not None else ""
                choices = " choices %s" % _short(c["choices"]) if c.get("choices") else ""
                unit = " %s" % c["unit"] if c.get("unit") else ""
                self.lines.append('  %s%s "%s": %s%s%s%s%s' % (p, c["id"], c["name"], c["type"], value, unit, choices,
                                                              " [live]" if c["live"] else ""))
        if outputs:
            self.lines.append("outputs:")
            for c in outputs:
                self.lines.append('  %s%s "%s": %s%s' % (p, c["id"], c["name"], c["type"],
                                                        " [plotted]" if c["plotted"] else ""))
        errors = [p + c["id"] for c in d["panel"] if _is_error(c)]
        if errors:
            self.lines.append("errors (left out, as their wires are): %s" % ", ".join(errors))
        if d.get("diagram"):
            self.lines.append("diagram:")
            self.diagram(d["diagram"], 1)
        if d.get("calls"):
            self.lines.append("calls:")
            for key, c in d["calls"].items():
                who = " ".join(x for x in (c["maker"], c["model"]) if x)
                sends = ", ".join('"%s"' % s.get("text", "") for s in c["sends"][:TEXT_SENDS])
                self.lines.append('  "%s": %s%s%s%s' % (key, c["kind"], " (%s)" % who if who else "",
                                                       "; sends %s" % sends if sends else "",
                                                       "; %s" % c["sends_unread"] if c["sends_unread"] else ""))
        unknown = d.get("unknown") or {}
        if unknown.get("kinds") or unknown.get("prims"):
            self.lines.append("unknown to Kelvin: kinds %s, functions %s" % (unknown.get("kinds"),
                                                                           unknown.get("prims")))
        for line in d.get("unread") or []:   # each, however many: a description cut short says so here
            self.lines.append("not read: %s" % line)
        return self.lines

    def diagram(self, dg: Dict[str, Any], depth: int) -> None:
        ind = "  " * depth
        for n in dg["nodes"]:
            self.node(n, depth)
        for w in dg["wires"]:   # what goes to an indicator, a tunnel's inside, a loop's condition
            if w["from"] in self.errors:
                continue
            for sink in w["to"]:
                if sink in self.on_nodes or sink in self.errors:
                    continue
                to = self.expr(sink) if sink in self.control or sink in self.literal else self.p + sink
                self.lines.append("%s%s <- %s" % (ind, to, self.expr(w["from"])))

    def node(self, n: Dict[str, Any], depth: int) -> None:
        ind, p = "  " * depth, self.p
        ins = []

        for t in n["terms"]:
            if t["dir"] != "in" or t["error"]:
                continue
            if "frames" in n:   # a structure's input: by the terminal it enters (its tunnels say where it goes)
                label = p + t["id"]
            else:
                label = t["name"] or ("p%s" % t["parm"] if t.get("parm") is not None else "")
            ins.append("%s=%s" % (label, self.src(t["id"])) if label else self.src(t["id"]))
        if "frames" in n:
            self.structure(n, depth, ins)
            return
        k = n["kind"]
        if k == "prim":
            what = n["name"] or "function#%d" % n["prim"]
        elif k == "call":
            c = self.d["calls"].get(n["call"], {})
            what = 'call "%s" [%s]' % (n["call"], " ".join(x for x in (c.get("kind", ""), c.get("maker", ""),
                                                                   c.get("model", "")) if x))
        elif k == "call_library":
            what = 'dll "%s" %s' % (n["library"], n["function"])
        elif k == "property":
            what = "property %s: %s" % (n["of"], ", ".join(n["items"]))
        elif k == "invoke":
            what = "invoke %s.%s" % (n["of"], n["method"])
        elif k == "formula":
            what = 'formula "%s"' % " ".join(n["text"].split())[:200]
        elif k in ("function", "feedback"):
            what = n["name"]
            if n.get("op"):   # Compound Arithmetic: its operation, and the inputs it inverts first
                what += " (%s%s)" % (n["op"], "; inverts " + ", ".join(p + t for t in n["inverted"])
                                     if n.get("inverted") else "")
        else:
            what = "other (%s)" % n.get("what", k)
        outs = [t for t in n["terms"] if t["dir"] == "out" and not t["error"]]
        named = k in ("call", "function") or len(outs) > 1   # what a call gives, by the sub-VI's names for it
        outs = ["%s=%s%s" % (t["name"], p, t["id"]) if named and t["name"] else p + t["id"] for t in outs]
        line = "%s%s%s = %s(%s)" % (ind, p, n["id"], what, ", ".join(ins))
        if outs:
            line += " -> " + ", ".join(outs)
        sends = [s.get("text", "") for s in n.get("sends", [])][:TEXT_SENDS]
        if sends:
            line += " sends " + ", ".join('"%s"' % s for s in sends)
        self.lines.append(line)

    def structure(self, n: Dict[str, Any], depth: int, ins: List[str]) -> None:
        ind, p = "  " * depth, self.p
        k = n["kind"]
        if k == "loop":
            head = "%s loop%s" % (n["loop"], ' "%s"' % n["name"] if n["name"] else "")
            if n.get("stop") and n.get("stops_on") is not None:   # which way its condition works matters
                head += " (%s %s is true)" % ("stops when" if n["stops_on"] else "continues while", self.src(n["stop"]))
        elif k == "case":
            head = "case on %s" % (self.src(n["selector"]) if n.get("selector") else "?")
        elif k == "sequence":
            head = "flat sequence frame" if n.get("flat") else "sequence"
        elif k == "event":
            head = "event structure" + (" (timeout %s)" % self.src(n["timeout"]) if n.get("timeout") else "")
        else:
            head = n.get("what", k)
        self.lines.append("%s%s%s %s {" % (ind, p, n["id"], head))
        inner = "  " * (depth + 1)
        if ins:
            self.lines.append("%sin: %s" % (inner, ", ".join(ins)))
        own = ["%s=%s%s" % (key, p, n[key]) for key in ("count", "index", "stop") if n.get(key)]
        if own:
            self.lines.append("%s%s" % (inner, ", ".join(own)))
        for t in n.get("tunnels", []):
            if t["kind"] == "selector":   # the selector's value, inside each frame
                self.lines.append("%sselector value | inside %s" % (inner, ", ".join(p + i for i in t["inner"])))
                continue
            if t["kind"] == "shift_register" and "read" in t:
                def ids(xs: List[Optional[str]]) -> str:
                    return ", ".join(p + x for x in xs if x) or "-"
                self.lines.append("%sshift_register init %s | read %s | write %s | out %s" % (
                    inner, ids(t["inits"]), ids(t["read"]), ids([t["write"]]), ids([t["out"]])))
                continue
            kind = t["kind"] + (" (%s)" % t["side"] if t.get("side") else "")
            self.lines.append("%s%s %s | inside %s" % (inner, kind, p + t["outer"] if t["outer"] else "-",
                                                      ", ".join(p + i for i in t["inner"]) or "-"))
        for i, f in enumerate(n["frames"], 1):
            label = ", ".join('"%s"' % x for x in f["label"]) if f["label"] else str(i)
            self.lines.append("%sframe %s:" % (inner, label))
            self.diagram(f, depth + 2)
        self.lines.append("%s}" % ind)


def render_text(desc: Dict[str, Any]) -> str:
    """A description as text a model reads: much shorter than its JSON (error wiring left out, long values cut),
    every node on a line of its own that starts with its id, every input with where its value comes from; each of
    the lab's own sub-VIs after it, under sub-VI s1, s2..., its ids prefixed so (s1.n12)."""
    lines = ['VI "%s" (%s)' % (desc.get("name", ""), desc.get("labview", ""))] + _Text(desc).write()
    for i, (key, sub) in enumerate((desc.get("subvis") or {}).items(), 1):
        lines += ["", 'sub-VI s%d "%s" (%s)' % (i, key, sub.get("labview", ""))]
        lines += _Text(sub, "s%d." % i).write()
    return "\n".join(lines) + "\n"


def describe(path: Any, link: Optional[vigraph.Link] = None, folder: Any = None, own: Any = ()) -> Dict[str, Any]:
    """A VI's full description, from its file. link(name, where): a linkage entry with the maker and model a driver
    library's name says (default labview.link: an instrument driver's calls are told from the lab's own VIs by
    it); folder: where the lab's sub-VIs may be read from (default: the VI's own folder; never outside it); own:
    Kelvin's own folders, never read. A file that can't be read gives a description that says why ("read": "no"),
    never an error."""
    if link is None:
        from .labview import link
    try:
        vi = vifile.read(path)
    except vifile.ReadError as e:
        return _empty(path, str(e))
    except Exception as e:   # a file damaged in a way no reader foresaw: said, never raised
        return _empty(path, "it looks damaged (%s)" % type(e).__name__)
    shared = vigraph._Shared(folder=str(folder or Path(str(path)).parent), own=tuple(own or ()),
                             max_files=MAX_FILES)
    return _describe(vi, link, shared, _sha256(path))
