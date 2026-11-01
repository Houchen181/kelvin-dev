"""A LabVIEW VI as a Kelvin scan: a VI's summary (labview.py) translated into a plan, the same way every time and with
no model, with what each part of the VI became and, said plainly, what didn't translate. Nothing is guessed: a part
the summary doesn't say enough about to carry over exactly is left out, and said.

    front-panel controls    the scan's values: each control's default, where the diagram uses it
    a loop over a range     a sweep (a For or While loop over from/to/step, points, or an array); n times around a
                            sweep, repeat:; n times reading with a wait between, a time series (time:)
    VISA writes, NI-DAQmx writes, and calls whose commands are known
                            sets: before the loops setup:, in a loop its sweep, after them after:
    VISA queries, NI-DAQmx reads    measure:
    a wait after a loop's set       that loop's settle:
    file writes             nothing to do: Kelvin records every point (run.h5); each column is a reading or a sweep

A loop's stop condition (a While loop's, a For loop's conditional terminal) is never dropped: a reading compared with
a value becomes its sweep's until: (or its time series'), a Stop button is Kelvin's own Stop, and stopping after its
last value is where its range ends anyway, each said; any other condition gives no scan, and says why.

Not translated, and said: state machines, event structures, code that runs in parallel, Call Library nodes, property
nodes, sub-VIs whose commands aren't known, a wait anywhere else, a set or reading the instrument's card doesn't have,
a value worked out as the VI runs, and a loop that ends only when someone presses Stop. A VI whose flow can't be
carried over (a state machine, an event structure, parallel loops, several measurements one after another) gives no
scan, only why."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from . import scripts
from .eventlog import plan_text
from .labview import STOP_OPS, address_key, address_of, controls_of, finite, range_values, validate, value_of
from .plans import PlanError, load_plan
from .scanlibrary import summary

# LabVIEW's own VIs that touch no instrument: errors and timing (any other call needs its commands to translate)
HARMLESS = {"simple error handler.vi", "general error handler.vi", "merge errors.vi", "clear errors.vi",
            "error cluster from error code.vi", "get date/time in seconds", "get date/time string.vi",
            "format date/time string", "tick count (ms)"}
FLOW = {"state_machine": "a state machine%s: which state runs next is decided as it runs",
        "event": "an event structure%s: it waits for someone at the front panel",
        "parallel": "code that runs in parallel%s: a scan does one thing at a time"}


def _num(x: Any) -> str:
    return ("%.6g" % x) if isinstance(x, float) else str(x)


def _flow(d: Dict[str, Any]) -> str:
    return "{%s}" % ", ".join("%s: %s" % (k, ("[%s]" % ", ".join(_num(v) for v in x)) if isinstance(x, list) else
                                          _num(x)) for k, x in d.items())


def plan_name(vi_name: str) -> str:
    """A scan's name from its VI's: "IV sweep.vi" is iv_sweep."""
    stem = re.sub(r"(?i)\.vit?$", "", vi_name)
    name = re.sub(r"[^A-Za-z0-9_]+", "_", stem).strip("_").lower()[:48] or "labview_scan"
    return name if name[0].isalpha() else "vi_" + name


class _Scan:
    def __init__(self, vi: Dict[str, Any], instruments: Dict[str, Tuple[str, Any]], aliases: Dict[str, str]):
        self.vi = vi
        self.controls = controls_of(vi)
        self.instruments = instruments
        self.cards = {name: card for name, card in instruments.values()}
        self.aliases = aliases
        self.linkage = {e.get("name"): e for e in vi.get("linkage") or [] if isinstance(e, dict)}
        self.mapping: List[Dict[str, str]] = []
        self.gaps: List[str] = []
        self.fatal = False
        self.used: set = set()                          # the controls the scan takes values from
        self.setup: Dict[str, Any] = {}
        self.after: Dict[str, Any] = {}
        self.sweep: Dict[str, Dict[str, Any]] = {}
        self.repeat: Optional[int] = None
        self.time: Optional[Dict[str, Any]] = None
        self.measure: List[str] = []
        self.readings: Dict[str, str] = {}              # a reading's name in the VI (into): its plan key
        self.axes: Dict[str, str] = {}                  # a loop's var: the plan key it sweeps

    # ------------------------------------------------------------------ saying what happens

    def map(self, vi: str, plan: str) -> None:
        self.mapping.append({"vi": vi, "plan": plan})

    def gap(self, text: str, fatal: bool = False) -> None:
        if text not in self.gaps:
            self.gaps.append(text)
        self.fatal = self.fatal or fatal

    def words(self, src: Any) -> str:
        """A source in words: a control with its saved value, a constant, a loop's value or a reading."""
        if not isinstance(src, dict):
            return "?"
        if "control" in src:
            v = value_of({"control": src["control"]}, self.controls)
            self.used.add(str(src["control"]))
            text = "%s = %s" % (src["control"], _num(v) if v is not None else "(no value saved)")
        elif "value" in src:
            v = src["value"]
            text = "[%s]" % ", ".join(_num(x) for x in v) if isinstance(v, list) else _num(v)
        elif "loop" in src:
            text = "the loop's %s" % src["loop"]
        elif "read" in src:
            text = "the reading %s" % src["read"]
        else:
            text = "%s (worked out in the VI)" % src.get("computed", "?")
        if src.get("scale", 1) != 1 or src.get("offset", 0):
            text += " × %s%s" % (_num(src.get("scale", 1)), " + %s" % _num(src["offset"]) if src.get("offset") else "")
        return text

    # ------------------------------------------------------------------ what one node does

    def target(self, resource: Any) -> Tuple[Optional[Tuple[str, Any]], str]:
        """The lab's instrument a resource opens (its name and card), and the name the VI gives it."""
        address, named = address_of(resource, self.controls, self.aliases)
        if isinstance(resource, dict) and "control" in resource:
            self.used.add(str(resource["control"]))
        return (self.instruments.get(address_key(address)) if address else None), (named or address or "?")

    def command(self, card: Any, text: str, values: Dict[str, Any]) -> Tuple[List[Any], bool, bool, Any]:
        """The card parameters a command sets or reads, exactly as the card writes it (no other channel, no other
        form): (specs, a reading, sent as part of one, the value it sets as a source). A query several of the card's
        readings share (a source-meter's :READ?) is each of them: its reply holds them all."""
        holes = list(dict.fromkeys(scripts._PLACE.findall(text)))
        samples = {h: "%d.25" % (k + 3) for k, h in enumerate(holes)}   # a number of its own for each fill-in
        sample = scripts._PLACE.sub(lambda m: samples[m.group(1)], text).strip()
        query = sample.endswith("?") or "?" in sample.split(" ")[0]
        if query:
            found = [spec for spec in card.getters if scripts._matches(spec.get_cmd, sample)]
            if found:
                return found, True, False, None
        for spec in ([] if query else card.setters):
            if scripts._matches(spec.set_cmd, sample):
                raw = spec.set_cmd.match(sample)
                hole = next((h for h, x in samples.items() if raw == x), None)
                if hole is not None:
                    return [spec], False, False, values.get(hole)
                if holes:   # the value written in, beside fill-ins that aren't it (a channel's number)
                    return [spec], False, False, {"computed": text}
                try:
                    return [spec], False, False, {"value": spec.from_wire(raw or "")}
                except ValueError:
                    return [spec], False, False, {"computed": text}
        if not query:
            found = [spec for spec in card.params.values()   # HP style: a trace fetched without "?"
                     if spec.type == "trace" and scripts._matches(spec.get_cmd, sample)]
            if found:
                return found, True, False, None
        for spec in card.params.values():
            if spec.get_before and any(scripts._same(b, sample) for b in spec.get_before):
                return [spec], True, True, None
        return [], query, False, None

    def actions(self, n: Dict[str, Any]) -> List[Tuple[Any, ...]]:
        """What one node does, as a scan sees it: ("set", key, source, spec, words), ("read", [keys], into, words)
        (the readings one reply holds), ("part", key, words) (sent as part of a reading: the card sends it itself),
        ("wait", seconds, words), ("file", node, words), ("none", words) (nothing for a scan). Anything else is a
        gap, said here."""
        kind = n.get("node")
        if kind in ("write", "query"):
            return self.sends(n.get("resource"), [n], kind, n.get("into") or "",
                              "VISA query to" if kind == "query" else "VISA write to")
        if kind == "call":
            vi = str(n.get("vi") or "?")
            if n.get("sends"):
                return self.sends(n.get("resource"), [x for x in n["sends"] if isinstance(x, dict)], "call",
                                  n.get("into") or "", vi + " on")
            if vi.lower() in HARMLESS or vi.split(":")[-1].lower() in HARMLESS:
                return [("none", "LabVIEW's %s" % vi)]
            self.gap("calls %s: what it sends to the instrument isn't known" % vi)
            return []
        if kind == "daq":
            return self.daq(n)
        if kind == "wait":
            ms = "ms" in n
            v = value_of(n.get("ms") if ms else n.get("s"), self.controls)
            what = "Wait %s %s" % (self.words(n.get("ms") if ms else n.get("s")), "ms" if ms else "s")
            if not finite(v) or v < 0:
                self.gap("%s: how long isn't known before it runs" % what)
                return []
            return [("wait", v / 1000.0 if ms else float(v), what)]
        if kind == "file":
            return [("file", n, "writes a%s file" % ({"tdms": " TDMS", "measurement": " measurement (.lvm)",
                                                       "spreadsheet": " spreadsheet"}.get(n.get("kind"), "")))]
        if kind == "read":
            hit, named = self.target(n.get("resource"))
            self.gap("VISA read from %s: what it reads isn't known (no command before it)" % named)
            return []
        if kind in FLOW:
            parts = n.get("states") or n.get("events") or []
            self.gap(FLOW[kind] % (" (%s)" % ", ".join(map(str, parts[:6])) if parts else ""), fatal=True)
            return []
        if kind == "call_library":
            self.gap("a Call Library node (%s%s): a scan can't call a DLL" % (
                n.get("library") or "a DLL", ", " + n["function"] if n.get("function") else ""))
            return []
        if kind == "property":
            self.gap("a property node (%s %s): not carried over" % (n.get("of") or "", n.get("property") or ""))
            return []
        if kind == "loop":   # placed where a scan has no loop (the structure says so)
            return []
        self.gap("%s: not carried over" % (n.get("what") or kind))
        return []

    def sends(self, resource: Any, sends: List[Dict[str, Any]], kind: str, into: str,
              how: str) -> List[Tuple[Any, ...]]:
        """The commands a VISA write or query, or a call whose commands are known, sends: each as an action.
        how: what sends them, in words ("VISA write to", "Configure Source.vi on"), before the instrument's name."""
        hit, named = self.target(resource)
        out: List[Tuple[Any, ...]] = []
        for x in sends:
            text = str(x.get("text") or "").strip()
            what = "%s %s: %s" % (how, named, text)
            if hit is None:
                self.gap("%s: no instrument of the lab is at %s" % (what, named))
                continue
            name, card = hit
            specs, read, part, src = self.command(card, text, x.get("values") or {})
            if not specs:
                reset = any(scripts._matches(t, text) for t in card.reset)
                self.gap("%s: %s" % (what, "a reset, which a scan never sends" if reset else
                                     "not on %s's card (%s)" % (name, card.id)))
                continue
            keys = ["%s.%s" % (name, spec.name) for spec in specs]
            if part:
                out.append(("part", keys[0], what))
            elif read and kind == "write":
                self.gap("%s: a query whose reply it doesn't read" % what)
            elif read:
                out.append(("read", keys, into, what))
            else:
                out.append(("set", keys[0], src, specs[0], what))
        return out

    def daq(self, n: Dict[str, Any]) -> List[Tuple[Any, ...]]:
        channel = str(n.get("channel") or "")
        what = "NI-DAQmx %s %s" % (n.get("op"), channel)
        hit, _ = self.target(channel)
        part = channel.split("/", 1)[1] if "/" in channel else ""
        if hit is None:
            self.gap("%s: no NI-DAQmx device of the lab is %s" % (what, channel.split("/")[0]))
            return []
        name, card = hit
        spec = card.params.get(part)
        if spec is None or (n.get("op") == "write" and spec.set_cmd is None) or \
                (n.get("op") == "read" and spec.get_cmd is None):
            self.gap("%s: not on %s's card (%s)" % (what, name, card.id))
            return []
        key = "%s.%s" % (name, spec.name)
        if n.get("op") == "write":
            return [("set", key, n.get("value"), spec, what)]
        return [("read", [key], n.get("into") or "", what)]

    def value(self, src: Any, spec: Any, what: str) -> Tuple[bool, Any]:
        """A set's value where the summary says it (a constant, a control's saved value), as the card takes it."""
        if not isinstance(src, dict):
            self.gap("%s: the value it sets isn't in the summary" % what)
            return False, None
        if "read" in src:
            self.gap("%s: set from the reading %s as it runs: a scan sets values it knows before it starts"
                     % (what, src["read"]))
            return False, None
        if "computed" in src or "loop" in src:
            self.gap("%s: its value is worked out in the VI (%s)" % (what, self.words(src)))
            return False, None
        v = value_of(src, self.controls)
        words = self.words(src)
        if v is None:
            self.gap("%s: %s has no value saved with the VI" % (what, src.get("control", "its value")))
            return False, None
        if isinstance(v, float) and not finite(v):
            self.gap("%s: %s isn't a finite number (%s)" % (what, words, v))
            return False, None
        try:
            return True, spec.coerce(v)
        except ValueError as e:
            self.gap("%s: %s (%s)" % (what, e, words))
            return False, None

    def flat(self, nodes: Any) -> List[Dict[str, Any]]:
        """Nodes in the order they run: a sequence's frames one after another, a case structure as the case its
        selector's saved value picks (a selector known only as it runs is a gap, and its cases are left out)."""
        out: List[Dict[str, Any]] = []
        for n in nodes if isinstance(nodes, list) else []:
            kind = n.get("node") if isinstance(n, dict) else None
            if kind == "sequence":
                out += self.flat([x for frame in n.get("frames") or [] for x in frame])
            elif kind == "case":
                out += self.flat(self.case(n))
            elif kind:
                out.append(n)
        return out

    def case(self, n: Dict[str, Any]) -> List[Dict[str, Any]]:
        cases = n.get("cases") or {}
        v = value_of(n.get("selector"), self.controls)
        who = self.words(n.get("selector"))
        if v is None or isinstance(v, (list, dict)):
            self.gap("a case structure on %s: which case runs is known only as it runs" % who)
            return []
        label = ("True" if v else "False") if isinstance(v, bool) else (
            str(int(v)) if finite(v) and v == int(v) else str(v))
        for name in (label, '"%s"' % label, "Default"):
            if name in cases:
                self.map("case structure on %s" % who, "its %s case" % name)
                return cases[name] if isinstance(cases[name], list) else []
        self.gap("a case structure on %s: no case for %s" % (who, label))
        return []

    # ------------------------------------------------------------------ the scan's parts

    def keep(self, where: Dict[str, Any], key: str, v: Any, spec: Any, what: str, part: str) -> None:
        shown = spec.show(v) if spec.type in ("bool", "enum") else _num(v)
        if key in where:   # set again: the last one stands, where it is set
            where.pop(key)
            self.map(what, "%s: %s: %s (set earlier too: this one stands)" % (part, key, shown))
        else:
            self.map(what, "%s: %s: %s" % (part, key, shown))
        where[key] = v

    def measured(self, keys: List[str], into: str, what: str) -> None:
        self.measure += [k for k in keys if k not in self.measure]
        if into:
            self.readings[into] = keys[0] if len(keys) == 1 else "one of %s" % " and ".join(keys)
        self.map(what, "measure: %s%s" % (", ".join(keys), " (one reply holds them)" if len(keys) > 1 else ""))

    def file(self, n: Dict[str, Any], what: str) -> None:
        said = []
        for col in n.get("columns") or []:
            if not isinstance(col, dict):
                continue
            src, name = col.get("from"), str(col.get("name") or "?")
            if isinstance(src, dict) and "read" in src and src["read"] in self.readings:
                said.append("%s is %s" % (name, self.readings[src["read"]]))
            elif isinstance(src, dict) and "loop" in src and src["loop"] in self.axes:
                said.append("%s is %s" % (name, self.axes[src["loop"]]))
            elif isinstance(src, dict) and "control" in src:
                said.append("%s is %s, in the scan's plan" % (name, self.words(src)))
            else:
                self.gap("the file's column %s is %s: compute it from the run (compute)" % (
                    name, self.words(src) if isinstance(src, dict) else "not known"))
        self.map(what, "Kelvin records every point (run.h5)%s" % (": " + ", ".join(said) if said else ""))

    def part(self, nodes: List[Dict[str, Any]], where: str, single: bool) -> None:
        """What runs before the loops (setup), or after them (after). single: the VI has no loop, so its readings
        are the scan's one point."""
        target = self.setup if where == "setup" else self.after
        for n in nodes:
            for act in self.actions(n):
                kind = act[0]
                if kind == "set":
                    _, key, src, spec, what = act
                    ok, v = self.value(src, spec, what)
                    if ok:
                        self.keep(target, key, v, spec, what, where)
                elif kind == "read":
                    if single:
                        self.measured(act[1], act[2], act[3])
                    else:
                        self.gap("%s: read once %s the loops, not at each point" % (
                            act[3], "before" if where == "setup" else "after"))
                elif kind == "wait":
                    self.gap("%s %s the loops: a scan waits after a set (settle:)" % (
                        act[2], "before" if where == "setup" else "after"))
                elif kind == "part":
                    self.map(act[2], "sent as part of reading %s (the card sends it)" % act[1])
                elif kind == "file":
                    self.file(act[1], act[2])
                else:
                    self.map(act[1], "nothing for the scan")

    def span(self, rng: Dict[str, Any], src: Dict[str, Any], spec: Any, what: str) -> Optional[Dict[str, Any]]:
        """A stepped loop's range as a sweep's ({from, to, step}, {from, to, points, log} or {values}), as the
        value its set sends (scaled, where the summary says so)."""
        scale, offset = src.get("scale", 1), src.get("offset", 0)

        def f(x: float) -> float:
            return x * scale + offset
        got = {k: value_of(rng[k], self.controls) for k in ("from", "to", "step", "points") if k in rng}
        for k in got:
            self.words(rng[k])
        numbers = all(finite(v) for v in got.values())
        if "values" in rng or "count" in rng or (rng.get("log") and (scale <= 0 or offset)):
            self.words(rng.get("values") or rng.get("count") or {})
            values = [f(x) for x in range_values(rng, self.controls)]
            if not values:
                self.gap("%s: its values aren't in the summary" % what, fatal=True)
                return None
            if spec.numeric:
                return {"values": values}
            try:
                return {"values": [spec.coerce(v) for v in values]}
            except ValueError as e:
                self.gap("%s: %s" % (what, e), fatal=True)
                return None
        if not numbers or "from" not in got or "to" not in got or not ("step" in got or "points" in got):
            self.gap("%s: its range isn't in the summary" % what, fatal=True)
            return None
        out: Dict[str, Any] = {"from": f(got["from"]), "to": f(got["to"])}
        if "step" in got:
            out["step"] = abs(got["step"] * scale)
        else:
            out["points"] = int(got["points"])
            if rng.get("log"):
                out["log"] = True
        return out

    def nest(self, n: Dict[str, Any], outer: List[str]) -> None:
        """One loop and what it holds: a sweep, repeat: or a time series, its settle, its readings, and the loop
        inside it."""
        var, rng = str(n.get("var") or ""), n.get("range") or {}
        kind = "While loop" if n.get("kind") == "while" else "For loop"
        what = "%s%s" % (kind, " over %s" % var if var else "")
        if n.get("parallel"):
            self.gap("%s: its steps run in parallel: a scan does one thing at a time" % what, fatal=True)
            return
        stepped = any(k in rng for k in ("from", "values", "points", "step"))
        if not stepped and "count" not in rng:
            self.gap("%s: it runs until %s; a scan needs its end (a range, or time: for:)" % (
                what, n.get("until") or "a condition the summary doesn't give"), fatal=True)
            return
        body = self.flat(n.get("body"))
        inner = [x for x in body if x.get("node") == "loop"]
        if len(inner) > 1:
            self.gap("%s holds %d loops one after another: a scan nests one loop in each" % (what, len(inner)),
                     fatal=True)
            return
        cut = body.index(inner[0]) if inner else len(body)
        head = [(x, a) for x in body[:cut] for a in self.actions(x)]
        tail = [(x, a) for x in body[cut + 1:] for a in self.actions(x)]
        following = [a for _, a in head if a[0] == "set" and isinstance(a[2], dict) and var and a[2].get("loop") == var]
        if len(following) > 1:
            self.gap("%s sets %s from its value: a scan's sweep sets one setting" % (
                what, " and ".join(a[1] for a in following)), fatal=True)
            return
        axis = following[0] if following else None
        if axis is not None:
            if axis[1] in self.sweep:
                self.gap("%s sweeps %s again, inside its own sweep" % (what, axis[1]), fatal=True)
                return
            entry = self.span(rng, axis[2], axis[3], what)
            if entry is None:
                return
            self.sweep[axis[1]] = entry
            self.axes[var] = axis[1]
            self.map("%s (%s)" % (what, self.range_words(rng)), "sweep: %s: %s" % (axis[1], _flow(entry)))
        elif stepped:
            self.gap("%s (%s): nothing it sets follows its value" % (what, self.range_words(rng)), fatal=True)
            return
        times = value_of(rng.get("count"), self.controls) if axis is None else None
        if axis is None and not (finite(times) and times >= 1 and times == int(times)):
            self.gap("%s: how many times it runs isn't in the summary" % what, fatal=True)
            return
        reads_here = [a for _, a in head if a[0] == "read"]
        if axis is None and inner:   # n times around the loops inside: the whole sweep repeated
            if outer or self.sweep or self.repeat:
                self.gap("%s runs %d times inside another loop: a scan repeats only the whole sweep (repeat:)" % (
                    what, times), fatal=True)
                return
            self.repeat = int(times)
            self.map("%s, %s times" % (what, self.words(rng["count"])), "repeat: %d" % self.repeat)
        elif axis is None:   # n readings with waits between: a time series
            waits = [a for _, a in head if a[0] == "wait"]
            every = sum(a[1] for a in waits)
            if not reads_here or every <= 0 or any(a[0] == "set" for _, a in head):
                self.gap("%s reads %d times at each point%s: a scan reads once a point (its instrument's averaging, "
                         "or repeat:)" % (what, times, "" if every > 0 else " with no wait between"), fatal=True)
                return
            self.time = {"every": round(every, 6), "points": int(times)}
            self.map("%s, %s times" % (what, self.words(rng["count"])), "time: %s" % _flow(self.time))
            for a in waits:
                self.map(a[2], "time: every %s s" % _num(round(every, 6)))
        seen_axis = axis is None
        settle = 0.0
        for x, a in head:
            act = a[0]
            if a is axis:
                seen_axis = True
                self.map(a[4], "sets %s at each step (the sweep)" % a[1])
            elif act == "set":
                src = a[2]
                if isinstance(src, dict) and "loop" in src:
                    self.gap("%s: set from the loop's %s at each step: a scan's sweep sets one setting per loop"
                             % (a[4], src["loop"]), fatal=True)
                else:
                    self.gap("%s: set again at each step of %s" % (a[4], what))
            elif act == "wait":
                if axis is not None and seen_axis and not self._read_before(head, x):
                    settle += a[1]
                    self.map(a[2], "sweep: %s: settle: %s s" % (axis[1], _num(a[1])))
                elif axis is not None or self.repeat and inner:
                    self.gap("%s inside %s, not right after its set: a scan waits after each set (settle:)"
                             % (a[2], what))
            elif act == "read":
                if inner:
                    self.gap("%s: read at each step of %s only: a scan reads at every point" % (a[3], what))
                elif not seen_axis:
                    self.gap("%s: read before %s's set: a scan reads after it" % (a[3], what))
                else:
                    self.measured(a[1], a[2], a[3])
            elif act == "part":
                self.map(a[2], "sent as part of reading %s (the card sends it)" % a[1])
            elif act == "file":
                self.file(a[1], a[2])
            elif act == "none":
                self.map(a[1], "nothing for the scan")
        if axis is not None and settle:
            self.sweep[axis[1]]["settle"] = round(settle, 6)
        if not self.stop(n, what, axis, bool(inner)):
            return
        if inner:
            self.nest(inner[0], outer + [var])
        for _, a in tail:
            act = a[0]
            if act in ("set", "read", "wait"):
                self.gap("%s: after each pass of the loop inside %s: not carried over" % (a[-1], what))
            elif act == "part":
                self.map(a[2], "sent as part of reading %s (the card sends it)" % a[1])
            elif act == "file":
                self.file(a[1], a[2])
            else:
                self.map(a[1], "nothing for the scan")

    def stop(self, n: Dict[str, Any], what: str, axis: Optional[Tuple[Any, ...]], inner: bool) -> bool:
        """A loop's stop condition (until, said exactly in stop_if) carried over, never dropped: a reading this loop
        takes itself, compared with a number, is its sweep's until: (or its time series'); a Stop button, Kelvin's
        own Stop; stopping after its last value, where its sweep ends anyway; each said. Anything else is a gap that
        keeps the VI from being a scan (False)."""
        until = str(n.get("until") or "").strip()
        stop = n.get("stop_if")
        if not until and stop is None:
            return True
        if isinstance(stop, dict) and set(stop) == {"button"}:
            self.map("%s: its %s button" % (what, stop["button"]), "Kelvin's Stop (a person stops the run)")
            return True
        if stop == {"last": True}:
            self.map("%s: it stops after its last value" % what, "where the sweep ends")
            return True
        rule = self.rule(stop) if isinstance(stop, dict) and "read" in stop and not inner else None
        where = (self.sweep.get(axis[1]) if axis is not None else self.time) if rule else None
        if rule and where is not None and "until" not in where:
            where["until"] = rule
            self.map("%s: it stops once %s %s %s" % (what, stop["read"], stop["op"], self.words(stop["than"])),
                     "%s: until: %s" % ("sweep: %s" % axis[1] if axis is not None else "time", rule))
            return True
        self.gap("%s: it can also stop early (%s): a scan can't stop the same way" % (
            what, until or "a condition of its own"), fatal=True)
        return False

    def rule(self, stop: Dict[str, Any]) -> Optional[str]:
        """A reading compared with a number, as a plan's rule ("smu.I > 0.001"); None unless it is exactly that: a
        reading this scan measures as one key, compared with a finite number (or True, False for == and !=)."""
        key = self.readings.get(str(stop.get("read")))
        than = value_of(stop.get("than"), self.controls)
        if not key or key.startswith("one of ") or stop.get("op") not in STOP_OPS:
            return None
        if isinstance(than, bool) and stop["op"] in ("==", "!="):
            text = "true" if than else "false"
        elif finite(than):
            text = repr(float(than)) if isinstance(than, float) else str(than)
        else:
            return None
        return "%s %s %s" % (key, stop["op"], text)

    @staticmethod
    def _read_before(head: List[Tuple[Dict[str, Any], Tuple[Any, ...]]], node: Dict[str, Any]) -> bool:
        """Whether a reading comes before this node in a loop's body (a wait after it is no settle)."""
        for x, a in head:
            if x is node:
                return False
            if a[0] == "read":
                return True
        return False

    def range_words(self, rng: Dict[str, Any]) -> str:
        if "values" in rng:
            return "over %s" % self.words(rng["values"])
        if "count" in rng:
            return "%s times" % self.words(rng["count"])
        text = "from %s to %s" % (self.words(rng.get("from")), self.words(rng.get("to")))
        if "step" in rng:
            text += ", step %s" % self.words(rng["step"])
        if "points" in rng:
            text += ", %s points%s" % (self.words(rng["points"]), ", log" if rng.get("log") else "")
        return text

    # ------------------------------------------------------------------ the whole VI

    def run(self) -> Dict[str, Any]:
        problems = validate(self.vi)
        if problems:
            self.gap("its summary can't be read: %s" % "; ".join(problems[:3]), fatal=True)
            return self.result()
        if self.vi["read"] != "full":
            self.gap("its block diagram isn't read%s" % (" (%s)" % "; ".join(self.vi["unread"])
                                                         if self.vi["unread"] else ""), fatal=True)
            return self.result()
        top = self.flat(self.vi["diagram"])
        loops = [n for n in top if n.get("node") == "loop"]
        if len(loops) > 1:
            self.gap("%d loops one after another: a scan is one nest of loops" % len(loops), fatal=True)
            return self.result()
        cut = top.index(loops[0]) if loops else len(top)
        self.part(top[:cut], "setup", single=not loops)
        if loops:
            self.nest(loops[0], [])
        self.part(top[cut + 1:], "after", single=False)
        if not self.measure and not self.fatal:
            self.gap("it reads nothing Kelvin can measure, so it is no scan", fatal=True)
        return self.result()

    def result(self) -> Dict[str, Any]:
        stem = re.sub(r"(?i)\.vit?$", "", str(self.vi.get("name") or "VI"))
        unused = [c["name"] for c in self.controls.values()
                  if c.get("name") not in self.used and not c.get("indicator") and c.get("type") not in ("visa",)]
        if unused and not self.fatal:
            self.map("controls %s" % ", ".join(unused), "not used by the scan")
        plan: Optional[Dict[str, Any]] = None
        if not self.fatal:
            plan = {"plan": plan_name(str(self.vi.get("name") or "")),
                    "description": "From LabVIEW: %s" % self.vi.get("name")}
            if self.setup:
                plan["setup"] = dict(self.setup)
            if self.repeat:
                plan["repeat"] = self.repeat
            if self.sweep:
                plan["sweep"] = {k: dict(v) for k, v in self.sweep.items()}
            if self.time:
                plan["time"] = dict(self.time)
            plan["measure"] = list(self.measure)
            if self.after:
                plan["after"] = dict(self.after)
            try:
                load_plan(plan)
            except PlanError as e:
                self.gap("the scan doesn't pass Kelvin's check of a plan: %s" % e, fatal=True)
                plan = None

        def unit(k: str) -> str:
            name, _, param = k.partition(".")
            spec = self.cards[name].params.get(param) if name in self.cards else None
            return spec.unit if spec is not None else ""
        return {"name": plan_name(str(self.vi.get("name") or "")), "title": stem, "vi": self.vi.get("name"),
                "path": self.vi.get("path"), "plan": plan, "text": plan_text(plan) if plan else "",
                "summary": summary(plan, unit) if plan else "", "mapping": self.mapping, "gaps": self.gaps}


def on_target(vi: Dict[str, Any], target: str) -> Dict[str, Any]:
    """A VI that runs on a project's hardware target (a real-time system, an FPGA): no scan of this computer's."""
    scan = _Scan(vi, {}, {})
    scan.gap("it runs on %s, a hardware target of its project, not on this computer" % target, fatal=True)
    return scan.result()


def translate(vi: Dict[str, Any], instruments: Dict[str, Tuple[str, Any]],
              aliases: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """A VI's summary as a scan. instruments: the lab's, {labview.address_key(address): (its name in the lab, its
    card)}; aliases: NI MAX's names for addresses. {name, title, vi, path, plan (None when no scan can come of it),
    text (the plan as YAML), summary (in one line), mapping ([{vi: the VI's element, plan: what it became}]),
    gaps (what didn't translate, in words)}. The same summary always gives the same scan; no model is asked. Never
    raises: a summary it trips on gives no scan, and says so."""
    try:
        return _Scan(vi, instruments, aliases or {}).run()
    except Exception as e:
        name = str(vi.get("name") or "") if isinstance(vi, dict) else ""
        return {"name": plan_name(name), "title": re.sub(r"(?i)\.vit?$", "", name or "VI"), "vi": name,
                "path": vi.get("path") if isinstance(vi, dict) else None, "plan": None, "text": "", "summary": "",
                "mapping": [], "gaps": ["it couldn't be translated (%s)" % type(e).__name__]}
