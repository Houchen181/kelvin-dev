"""A lab's LabVIEW, read so the lab can switch to Kelvin directly: its instruments set up as LabVIEW knows them, and
each measurement VI translated into a Kelvin scan (viscan.py). Nothing is ever run: no VI and no LabVIEW (it need not
be installed), and nothing is sent to an instrument.

    NI MAX      NI-VISA's aliases and the resources it lists (visaconf.ini): the names the lab gives its instruments,
                and their addresses; NI-DAQmx devices from a configuration exported from MAX (an .ini with
                [DAQmxDevice ...] sections), as MAX's own database needs NI's software to read
    projects    each LabVIEW project (.lvproj, XML): its VIs, libraries, dependencies and hardware targets, and the
                instrument drivers it uses (maker and model, as the driver library's name says them)
    VIs         each VI's summary (the layout below), from read_vi(): read from the VI's own file as bytes (vifile.py,
                vigraph.py), the VIs a VI library (.llb) holds too; what couldn't be read is said, in unread

survey() gathers them by instrument, in the script survey's form (scripts.Instrument, its card by scripts.match),
for the same review in Set up.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from . import scripts
from .scripts import Command, Instrument, normalize

VISACONF = Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData") / "National Instruments" / "NIvisa" / "visaconf.ini"
MAX_INI_BYTES = 2_000_000
MAX_PROJECT_BYTES = 5_000_000
MAX_VIS = 3000             # VIs listed from one folder
MAX_SHOWN = 40             # VIs the model's report names one by one
SKIP_DIRS = {".git", ".svn", ".hg", "__pycache__", "node_modules", ".venv", "venv"}
LIBRARY_DIRS = {"instr.lib", "vi.lib", "user.lib"}   # LabVIEW's folders of drivers and libraries, copied with the VIs
NOT_READ_YET = "not read yet"
_PORT = re.compile(r"^(?:COM|LPT)\d+$", re.I)        # a port's own name, which NI-VISA gives its serial ports itself
_WHERE = {"vilib": "vi.lib", "instrlib": "instr.lib", "userlib": "user.lib"}
# instrument makers as their drivers' names write them, for the maker of a driver library
MAKERS = (("Keithley", r"keithley"), ("Keysight", r"keysight"), ("Agilent", r"agilent"),
          ("HP", r"hewlett[- ]?packard|\bhp\b"), ("Tektronix", r"tektronix|\btek\b"),
          ("Rohde & Schwarz", r"rohde|\br\s*&\s*s\b"), ("Stanford Research Systems", r"stanford|\bsrs\b"),
          ("Lake Shore", r"lake\s*shore|\blsci\b"), ("Yokogawa", r"yokogawa"), ("Newport", r"newport"),
          ("Thorlabs", r"thorlabs"), ("Signal Recovery", r"signal\s*recovery|ametek"),
          ("Zurich Instruments", r"zurich"), ("Oxford Instruments", r"oxford"),
          ("American Magnetics", r"american\s*magnetics"), ("Cryomagnetics", r"cryomagnetics"), ("Rigol", r"rigol"),
          ("Siglent", r"siglent"), ("Anritsu", r"anritsu"), ("Fluke", r"fluke"), ("Tabor", r"tabor"),
          ("Coherent", r"coherent"), ("TOPTICA", r"toptica"), ("attocube", r"attocube"),
          ("Princeton Instruments", r"princeton"), ("Quantum Design", r"quantum\s*design"), ("Pfeiffer", r"pfeiffer"),
          ("Bluefors", r"bluefors"), ("National Instruments", r"national\s*instruments"))
_MODEL_WORD = re.compile(r"^[A-Za-z]{0,5}-?\d{2,6}[A-Za-z]{0,4}$")


# ------------------------------------------------------------------ NI MAX

def _visa(text: str) -> bool:
    """A VISA resource name (GPIB0::24::INSTR, TCPIP0::host::inst0::INSTR, ASRL3::INSTR...), not a port's name."""
    return "::" in text and scripts.is_address(text)


def _ini(text: str) -> List[Tuple[str, str, str, int]]:
    """An .ini file's settings: (section, key, value, line), comments and blank lines left out."""
    out, section = [], ""
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            section = m.group(1).strip()
            continue
        key, eq, value = line.partition("=")
        if eq:
            out.append((section, key.strip(), value.strip(), n))
    return out


def read_visaconf(text: str) -> Dict[str, Any]:
    """NI-VISA's configuration (visaconf.ini, which NI MAX writes): {aliases: {alias: address}, ports: {alias:
    address} (a serial port's own name, such as COM3 for ASRL3::INSTR: no instrument), resources: [address] (every
    resource it lists, in any section), unread: [lines of its aliases it couldn't read]}. An alias is read in either
    form, Alias0="'GPIB0::24::INSTR','K2400'" or K2400=GPIB0::24::INSTR: the address is told from the alias by its
    form, never by its place."""
    aliases: Dict[str, str] = {}
    ports: Dict[str, str] = {}
    resources: List[str] = []
    unread: List[str] = []
    for section, key, value, n in _ini(text):
        tokens = [t.strip() for t in (re.findall(r"'([^']*)'", value) or [value.strip('"')]) if t.strip()]
        found = [t for t in tokens if _visa(t)]
        for t in found:
            if normalize(t) not in resources:
                resources.append(normalize(t))
        if section.upper() != "ALIASES" or key.lower() == "numaliases":
            continue
        names = [t for t in tokens if not _visa(t)]
        if len(found) == 1 and len(names) == 1:
            alias = names[0]
        elif len(found) == 1 and not names and not re.match(r"(?i)^alias\d+$", key):
            alias = key
        else:
            unread.append("visaconf.ini line %d" % n)
            continue
        own = _PORT.match(alias) and found[0].upper().startswith("ASRL")
        (ports if own else aliases)[alias] = normalize(found[0])
    return {"aliases": aliases, "ports": ports, "resources": resources, "unread": unread}


def read_daqmx(text: str) -> List[Dict[str, str]]:
    """The NI-DAQmx devices in a configuration exported from NI MAX (an .ini: [DAQmxDevice Dev1] with its
    ProductType): [{name: "Dev1", model: "USB-6211"}]. A cDAQ chassis holds devices but is none."""
    models: Dict[str, str] = {}
    for section, key, value, _ in _ini(text):
        m = re.match(r"^DAQmx\w*\s+(\S.*)$", section)
        if m and key == "ProductType":
            models[m.group(1).strip()] = value.strip('"').strip()
    return [{"name": n, "model": v} for n, v in models.items() if not re.search(r"(?i)cDAQ-\d", v)]


def _text(path: Path) -> str:
    raw = path.read_bytes()[:MAX_INI_BYTES]
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def read_max(visaconfs: Iterable[Any] = (), exports: Iterable[Any] = ()) -> Dict[str, Any]:
    """NI MAX as these files keep it: NI-VISA's visaconf.ini files (each that is there) and configurations exported
    from MAX (their NI-DAQmx devices). {files: [read], aliases, ports, resources, daqmx, unread}."""
    out: Dict[str, Any] = {"files": [], "aliases": {}, "ports": {}, "resources": [], "daqmx": [], "unread": []}
    for path in visaconfs:
        p = Path(str(path))
        try:
            if not p.is_file():
                continue
            conf = read_visaconf(_text(p))
        except OSError as e:
            out["unread"].append("%s (%s)" % (p, e.strerror or e))
            continue
        out["files"].append(str(p))
        out["aliases"].update(conf["aliases"])
        out["ports"].update(conf["ports"])
        out["resources"] += [r for r in conf["resources"] if r not in out["resources"]]
        out["unread"] += ["%s: %s" % (p.name, u) for u in conf["unread"]]
    for path in exports:
        p = Path(str(path))
        try:
            devices = read_daqmx(_text(p))
        except OSError as e:
            out["unread"].append("%s (%s)" % (p, e.strerror or e))
            continue
        if devices:
            out["files"].append(str(p))
            out["daqmx"] += [d for d in devices if d["name"] not in {x["name"] for x in out["daqmx"]}]
    return out


# ------------------------------------------------------------------ LabVIEW projects

def lv_version(text: Any) -> str:
    """LabVIEW's version as its files write it (LVVersion="15008000"), in words: "LabVIEW 2015 (15.0)"; "" for none."""
    s = str(text or "").strip()
    if not re.fullmatch(r"\d{7,8}", s):
        return ""
    major, minor, fix = int(s[:-6]), int(s[-6]), int(s[-5])
    number = "%d.%d%s" % (major, minor, ".%d" % fix if fix else "")
    return "LabVIEW %d (%s)" % (2000 + major, number) if 9 <= major <= 22 else "LabVIEW %s" % number


def _url(url: str, project: Path) -> Tuple[str, str]:
    """A project item's URL as a path (or in one of LabVIEW's own folders: <vilib>/...) and where it is: vi.lib,
    instr.lib, user.lib, another of LabVIEW's folders, or "" (the lab's own). LabVIEW writes a path relative to the
    project file itself (a VI beside it is ../Main.vi); one that is only there taken from its folder is taken so."""
    m = re.match(r"^/<(\w+)>/(.*)$", url)
    if m:
        return "<%s>/%s" % (m.group(1), m.group(2)), _WHERE.get(m.group(1).lower(), "<%s>" % m.group(1))
    m = re.match(r"^/([A-Za-z])/(.*)$", url)   # an absolute path, as LabVIEW writes one: /C/Lab/x.vi
    if m:
        return "%s:/%s" % (m.group(1).upper(), m.group(2)), ""
    path = os.path.normpath(str(project / url))
    other = os.path.normpath(str(project.parent / url))
    return (other if not os.path.exists(path) and os.path.exists(other) else path), ""


def _target(item: Any) -> Dict[str, Any]:
    """A hardware target of a project (a CompactRIO, a PXI or desktop real-time system): its name, kind, address,
    and the modules it lists (crio.Type: NI 9215; its chassis is none)."""
    props = {p.get("Name"): (p.text or "").strip() for p in item.findall("Property")}
    modules = []
    for it in item.iter("Item"):
        model = next(((q.text or "").strip() for q in it.findall("Property") if q.get("Name") == "crio.Type"), "")
        if it is not item and model and "chassis" not in (it.get("Type") or "").lower():
            modules.append({"name": it.get("Name") or "", "model": model})
    return {"name": item.get("Name") or "", "type": item.get("Type") or "", "address": props.get("alias.value", ""),
            "modules": modules}


def _items(item: Any, project: Path, target: str, dependency: bool, out: Dict[str, Any]) -> None:
    for it in item.findall("Item"):
        kind, name, url = it.get("Type") or "", it.get("Name") or "", it.get("URL") or ""
        low = name.lower()
        if kind == "Build":   # what LabVIEW builds from the project
            continue
        if kind == "Dependencies":
            _items(it, project, target, True, out)
            continue
        path, where = _url(url, project) if url else ("", "")
        entry = {"name": name, "path": path, "where": where, "target": target}
        if kind == "VI" or low.endswith((".vi", ".vit")):
            (out["dependencies"] if dependency else out["vis"]).append(dict(entry, kind="vi"))
        elif kind in ("Library", "LVClass") or low.endswith((".lvlib", ".llb", ".lvclass")):
            (out["dependencies"] if dependency else out["libraries"]).append(dict(entry, kind="library"))
        elif kind == "Document":
            if dependency:
                out["dependencies"].append(dict(entry, kind="file"))
        else:   # a folder, or a target's chassis and modules
            _items(it, project, target, dependency, out)


def read_project(path: Any) -> Dict[str, Any]:
    """A LabVIEW project (.lvproj): {name, path, labview, vis, libraries, dependencies (each {name, path, where,
    target, kind}), targets, drivers, uses (NI-DAQmx, the DLLs it calls), problem}. Only read, as XML."""
    p = Path(str(path))
    out: Dict[str, Any] = {"name": p.name, "path": str(p), "labview": "", "vis": [], "libraries": [],
                           "dependencies": [], "targets": [], "drivers": [], "uses": [], "problem": ""}
    try:
        raw = p.read_bytes()[:MAX_PROJECT_BYTES + 1]
    except OSError as e:
        out["problem"] = "not read: %s" % (e.strerror or e)
        return out
    if len(raw) > MAX_PROJECT_BYTES:
        out["problem"] = "not read: bigger than %d MB" % (MAX_PROJECT_BYTES // 1_000_000)
        return out
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:   # a LabVIEW project declares none
        out["problem"] = "not read: it isn't a LabVIEW project"
        return out
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        out["problem"] = "not read: it isn't well-formed XML (%s)" % e
        return out
    if root.tag != "Project":
        out["problem"] = "not read: it isn't a LabVIEW project"
        return out
    out["labview"] = lv_version(root.get("LVVersion"))
    for top in root.findall("Item"):
        target = "" if top.get("Type") == "My Computer" else top.get("Name") or ""
        if target:
            out["targets"].append(_target(top))
        _items(top, p, target, False, out)
    drivers = {}
    for lib in out["libraries"] + [d for d in out["dependencies"] if d["kind"] == "library"]:
        found = driver_of(lib["name"], lib["where"])
        if found and (found["maker"] or found["model"]):
            drivers.setdefault((found["maker"], found["model"]), found)
    out["drivers"] = list(drivers.values())
    if any("/daqmx/" in d["path"].lower().replace("\\", "/") for d in out["dependencies"]):
        out["uses"].append("NI-DAQmx")
    out["uses"] += sorted({d["name"] for d in out["dependencies"] if d["name"].lower().endswith(".dll")})
    return out


# ------------------------------------------------------------------ instrument drivers

def driver_of(name: str, where: str = "") -> Optional[Dict[str, str]]:
    """An instrument driver's maker and model, as its library's name says them ("Keithley 2400 Series.lvlib" is a
    Keithley 2400): {library, maker, model, where}. None for one of LabVIEW's own (vi.lib), or a library that names
    neither and isn't in instr.lib (LabVIEW's folder of instrument drivers). A name that says only the maker gives
    the maker only: nothing is guessed."""
    if where == "vi.lib":
        return None
    library = name.split(":")[0].strip()
    stem = re.sub(r"(?i)\.(lvlib|llb|lvclass)$", "", library)
    maker, pattern = next(((m, p) for m, p in MAKERS if re.search(p, stem, re.I)), ("", ""))
    rest = re.sub(pattern, " ", stem, flags=re.I) if pattern else stem
    words = [w.strip(".,()[]") for w in re.split(r"[\s_]+", rest) if w.strip(".,()[]")]
    model = ""
    for k, w in enumerate(words):
        if not _MODEL_WORD.match(w) or re.fullmatch(r"\d{2}", w) or (not maker and re.fullmatch(r"(19|20)\d\d", w)):
            continue   # two digits alone are no model, nor a year where no maker is named (a maker's 2015 is one)
        before = words[k - 1] if k else ""
        model = "%s %s" % (before, w) if re.fullmatch(r"[A-Z]{2,4}", before) else w   # TDS 2000
        break
    if not (maker or model) and where != "instr.lib":
        return None
    return {"library": library, "maker": maker, "model": model, "where": where}


def _driver_hints(d: Dict[str, str]) -> List[str]:
    """A driver's maker and model as hints for its card (scripts.match): "Keithley 2400", "2400"."""
    if not d.get("model"):
        return []
    return (["%s %s" % (d["maker"], d["model"])] if d.get("maker") else []) + [d["model"].split()[-1]]


# ------------------------------------------------------------------ the VI summary
#
# What Kelvin knows of one VI, as plain JSON-able dicts: read_vi() makes one (a reader of the VI's own blocks fills
# it in), viscan.translate() turns one into a scan. Every key is there, empty when not read.
#
#   summary   1 (this layout's version)
#   name      the VI's file name ("IV sweep.vi"); path: where it is
#   labview   the LabVIEW version it was saved in ("LabVIEW 2015 (15.0)"; "" not read)
#   read      how much was read: "no" (not read yet), "names" (its linkage), "panel" (and its front panel), "full"
#             (and its block diagram)
#   linkage   [{name: "Keithley 2400 Series.lvlib:Configure Source.vi", library, vi, where (vi.lib, instr.lib,
#             user.lib, or "" for the lab's own), maker, model}]: the sub-VIs and libraries it calls; maker and model
#             where the library's name says them (driver_of)
#   controls  [{name, type (number, bool, string, enum, ring, path, visa, array, cluster, other), unit, default,
#             choices (an enum's or ring's), indicator}]: its front panel, with the values it is saved with
#   visa      [{resource (an address, or an NI MAX alias), control (the control it is set in, or "")}]
#   scpi      ["*RST", ":SOUR:VOLT {V}", ...]: the instrument commands in its string constants; {name} where the
#             diagram fills in a value
#   diagram   [node, ...]: the block diagram, in the order it runs (below)
#   unread    ["the block diagram: password-protected", ...]: what couldn't be read, in words
#
# A value in the diagram (a source) is {"value": 1.5}, {"control": "Start (V)"} (a front-panel control's value: its
# default), {"loop": "V"} (a loop's value, by its var), {"read": "I"} (a reading's, by its into) or {"computed":
# "how"} (worked out in a way a summary can't say), with "scale" and "offset" for a number used as x * scale +
# offset. A resource is a plain string (an address or an alias) or a source. The nodes:
#   {"node": "loop", "kind": "for" | "while", "var": "V", "range": {...}, "until": "", "parallel": false,
#    "body": [nodes]}   range: {from, to, step} (the values from, from + step, ... to, as a scan's sweep takes them)
#        or {from, to, points, log} (sources), {values: source} (an array), {count: source} (n times: the loop's
#        value is its count, 0 to n - 1), or {} (a while loop without one: until says what ends it, "Stop button"
#        or the condition in words); parallel: a For loop whose steps run at the same time
#   {"node": "sequence", "frames": [[nodes], ...]}
#   {"node": "case", "selector": source, "cases": {"True": [nodes], "False": [nodes], "Default": [nodes]}}
#   {"node": "wait", "ms": source} (Wait (ms)), or {"node": "wait", "s": source} (Time Delay)
#   {"node": "write", "resource": ..., "text": ":SOUR:VOLT {V}", "values": {"V": source}}   a VISA Write
#   {"node": "query", "resource": ..., "text": ":MEAS:CURR?", "values": {}, "into": "I"}   a write and its read
#   {"node": "read", "resource": ..., "into": "I"}   a VISA Read of a reply no command in the summary asked for
#   {"node": "call", "vi": "Keithley 2400 Series.lvlib:Configure Source.vi", "resource": ..., "inputs": {"Source
#    Level": source}, "sends": [{"text": ..., "values": {...}}], "into": ""}   a sub-VI; sends: the commands it
#        sends, when the reader can tell (else viscan can't carry the call over, and says so)
#   {"node": "daq", "op": "write" | "read", "channel": "Dev1/ao0", "value": source, "into": "V"}   NI-DAQmx
#   {"node": "file", "kind": "spreadsheet" | "measurement" | "tdms" | "text" | "binary", "columns": [{"name":
#    "I (A)", "from": source}]}   a file it writes
#   {"node": "parallel", "branches": [[nodes], ...]}   code that runs at the same time
#   {"node": "state_machine", "states": [...]}, {"node": "event", "events": [...]}, {"node": "call_library",
#    "library": "x.dll", "function": "f"}, {"node": "property", "of": ..., "property": ...}, {"node": "other",
#    "what": ...}: what a scan has no place for; viscan says each, never guesses at it

SUMMARY = 1
READS = ("no", "names", "panel", "full")
NODES = {"loop", "sequence", "case", "wait", "write", "query", "read", "call", "daq", "file", "parallel",
         "state_machine", "event", "call_library", "property", "other"}
CONTROL_TYPES = {"number", "bool", "string", "enum", "ring", "path", "visa", "array", "cluster", "other"}
SOURCES = ("value", "control", "loop", "read", "computed")
_UNIT_IN_NAME = re.compile(r"\(\s*([^()]{1,8})\s*\)\s*$")   # "Start (V)"


def vi(path: Any = "", name: str = "", labview: str = "", read: str = "no", controls: Iterable[Any] = (),
       diagram: Iterable[Any] = (), linkage: Iterable[Any] = (), visa: Iterable[Any] = (), scpi: Iterable[str] = (),
       unread: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """A VI's summary (the layout above), for a reader to fill in and for tests to build. With nothing read, unread
    says so ("not read yet"). linkage: names or entries (link()); visa: resources or entries."""
    p = Path(str(path)) if path else None
    return {"summary": SUMMARY, "name": name or (p.name if p else ""), "path": str(p) if p else "",
            "labview": labview, "read": read,
            "linkage": [entry if isinstance(entry, dict) else link(entry) for entry in linkage],
            "controls": list(controls), "visa": [v if isinstance(v, dict) else {"resource": v, "control": ""}
                                                 for v in visa],
            "scpi": list(scpi), "diagram": list(diagram),
            "unread": list(unread) if unread is not None else ([NOT_READ_YET] if read == "no" else [])}


def control(name: str, default: Any = None, type: str = "", unit: str = "", indicator: bool = False,
            choices: Iterable[Any] = ()) -> Dict[str, Any]:
    """A front-panel control; its type from its default and its unit from its name ("Start (V)") when not given."""
    if not type:
        type = ("bool" if isinstance(default, bool) else "number" if isinstance(default, (int, float)) else
                "array" if isinstance(default, list) else "string" if isinstance(default, str) else "other")
    m = _UNIT_IN_NAME.search(name)
    return {"name": name, "type": type, "unit": unit or (m.group(1) if m else ""), "default": default,
            "choices": list(choices), "indicator": indicator}


def link(name: str, where: str = "") -> Dict[str, Any]:
    """A linkage entry: a sub-VI ("<library>:<vi>" or a VI's name) and the maker and model its library says."""
    library, _, sub = name.rpartition(":") if ":" in name else ("", "", name)
    found = driver_of(library or name, where) or {}
    return {"name": name, "library": library, "vi": sub, "where": where, "maker": found.get("maker", ""),
            "model": found.get("model", "")}


def const(x: Any) -> Dict[str, Any]:
    return {"value": x}


def ctl(name: str, scale: Optional[float] = None, offset: Optional[float] = None) -> Dict[str, Any]:
    return _scaled({"control": name}, scale, offset)


def at(var: str, scale: Optional[float] = None, offset: Optional[float] = None) -> Dict[str, Any]:
    return _scaled({"loop": var}, scale, offset)


def reading(name: str) -> Dict[str, Any]:
    return {"read": name}


def _scaled(src: Dict[str, Any], scale: Optional[float], offset: Optional[float]) -> Dict[str, Any]:
    if scale is not None:
        src["scale"] = scale
    if offset is not None:
        src["offset"] = offset
    return src


def _source(x: Any) -> Dict[str, Any]:
    """A builder's value as a source: a dict as it is, a name a control's, anything else a constant."""
    return x if isinstance(x, dict) else ctl(x) if isinstance(x, str) else const(x)


def loop(var: str, body: Iterable[Any], kind: str = "for", until: str = "", parallel: bool = False,
         **span: Any) -> Dict[str, Any]:
    """A loop node; span: start (from), stop (to), step, points, log, values or count, each a number, a control's
    name or a source."""
    rng: Dict[str, Any] = {}
    for key, x in span.items():
        key = {"start": "from", "stop": "to"}.get(key, key)
        rng[key] = bool(x) if key == "log" else _source(x)
    return {"node": "loop", "kind": kind, "var": var, "range": rng, "until": until, "parallel": parallel,
            "body": list(body)}


def node(kind: str, /, **fields: Any) -> Dict[str, Any]:
    """Any other node: node("write", resource="K2400", text=":SOUR:VOLT {V}", values={"V": at("V")})."""
    return dict({"node": kind}, **fields)


def _check_source(src: Any, where: str, out: List[str]) -> None:
    if not isinstance(src, dict) or sum(1 for k in SOURCES if k in src) != 1:
        out.append("%s: a value is one of %s" % (where, ", ".join("{%s: ...}" % k for k in SOURCES)))
        return
    for k in ("scale", "offset"):
        if k in src and (isinstance(src[k], bool) or not isinstance(src[k], (int, float))):
            out.append("%s: %s is a number" % (where, k))


def _check_nodes(nodes: Any, where: str, out: List[str], depth: int) -> None:
    if not isinstance(nodes, list):
        out.append("%s: a list of nodes" % where)
        return
    if depth > 40:
        out.append("%s: nested more than 40 deep" % where)
        return
    for k, n in enumerate(nodes):
        here = "%s[%d]" % (where, k)
        kind = n.get("node") if isinstance(n, dict) else None
        if kind not in NODES:
            out.append("%s: a node is one of %s" % (here, ", ".join(sorted(NODES))))
            continue
        if kind == "loop":
            if n.get("kind") not in ("for", "while") or not isinstance(n.get("range"), dict):
                out.append("%s: a loop has kind (for or while) and a range" % here)
            for key, src in (n.get("range") or {}).items() if isinstance(n.get("range"), dict) else []:
                if key != "log":
                    _check_source(src, "%s.range.%s" % (here, key), out)
            _check_nodes(n.get("body"), here + ".body", out, depth + 1)
        elif kind in ("sequence", "parallel"):
            parts = n.get("frames" if kind == "sequence" else "branches")
            for j, part in enumerate(parts if isinstance(parts, list) else [None]):
                _check_nodes(part, "%s.%d" % (here, j), out, depth + 1)
        elif kind == "case":
            _check_source(n.get("selector"), here + ".selector", out)
            cases = n.get("cases")
            for name, part in (cases.items() if isinstance(cases, dict) else [("?", None)]):
                _check_nodes(part, "%s.%s" % (here, name), out, depth + 1)
        elif kind == "wait":
            _check_source(n.get("ms", n.get("s")), here + (".ms" if "ms" in n else ".s"), out)
        elif kind in ("write", "query") and not isinstance(n.get("text"), str):
            out.append("%s: a %s has its command (text)" % (here, kind))
        elif kind == "call" and not isinstance(n.get("vi"), str):
            out.append("%s: a call names its VI (vi)" % here)
        elif kind == "daq" and (n.get("op") not in ("write", "read") or not isinstance(n.get("channel"), str)):
            out.append("%s: a daq node has op (write or read) and a channel (Dev1/ao0)" % here)
        elif kind == "file" and not isinstance(n.get("columns", []), list):
            out.append("%s: a file's columns are a list" % here)


def validate(summary: Any) -> List[str]:
    """What is wrong with a VI's summary (the layout above), in words; [] when nothing is."""
    if not isinstance(summary, dict):
        return ["a VI's summary is a mapping"]
    out: List[str] = []
    if summary.get("summary") != SUMMARY:
        out.append("summary: %r is not layout %d" % (summary.get("summary"), SUMMARY))
    if not isinstance(summary.get("name"), str) or not summary.get("name"):
        out.append("name: the VI's name is missing")
    if summary.get("read") not in READS:
        out.append("read: one of %s" % ", ".join(READS))
    for key in ("linkage", "controls", "visa", "scpi", "diagram", "unread"):
        if not isinstance(summary.get(key), list):
            out.append("%s: a list" % key)
    for k, c in enumerate(summary.get("controls") if isinstance(summary.get("controls"), list) else []):
        if not isinstance(c, dict) or not isinstance(c.get("name"), str) or c.get("type") not in CONTROL_TYPES:
            out.append("controls[%d]: a control has a name and a type (%s)" % (k, ", ".join(sorted(CONTROL_TYPES))))
    if isinstance(summary.get("diagram"), list):
        _check_nodes(summary["diagram"], "diagram", out, 0)
    return out[:20]


def read_vi(path: Any) -> Dict[str, Any]:
    """A VI's summary, read from its own file as bytes, never run (vigraph.summary: its linkage, front panel and
    block diagram, the commands its drivers send when they are beside it), each linked driver library's maker and
    model by link(). A file that can't be read gives a summary that says why (read "no")."""
    from .vigraph import summary
    return summary(path, link=link)


def read_library(path: Any) -> List[Dict[str, Any]]:
    """The summary of each VI a VI library (.llb) holds; its path is the library's, then the VI's name."""
    from .vigraph import summaries
    return summaries(path, link=link)


# ------------------------------------------------------------------ values in a summary

def _number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def value_of(src: Any, controls: Dict[str, Dict[str, Any]]) -> Any:
    """A source's value where the summary says it: a constant, or a control's default (scaled as the source says);
    None for a loop's value, a reading, something computed, or a control with no default."""
    if not isinstance(src, dict):
        return None
    if "value" in src:
        v = src["value"]
    elif "control" in src:
        v = (controls.get(str(src["control"])) or {}).get("default")
    else:
        return None
    scale, offset = src.get("scale", 1), src.get("offset", 0)
    if scale == 1 and offset == 0:
        return v
    if _number(v):
        return v * scale + offset
    if isinstance(v, list) and v and all(_number(x) for x in v):
        return [x * scale + offset for x in v]
    return None


def range_values(rng: Dict[str, Any], controls: Dict[str, Dict[str, Any]]) -> List[float]:
    """A loop's values, as a scan's sweep takes the same range; [] when the summary doesn't say them."""
    from .plans import _base_values
    if "values" in rng:
        v = value_of(rng["values"], controls)
        return [float(x) for x in v] if isinstance(v, list) and all(_number(x) for x in v) else []
    if "count" in rng:
        n = value_of(rng["count"], controls)
        return [float(i) for i in range(int(n))] if _number(n) and n == int(n) and 0 <= n <= 1_000_000 else []
    spec: Dict[str, Any] = {k: value_of(rng[k], controls) for k in ("from", "to", "step", "points") if k in rng}
    if rng.get("log"):
        spec["log"] = True
    if not all(_number(v) for v in spec.values() if v is not True):
        return []
    errors: List[str] = []
    values = _base_values(spec, "", errors)
    return [] if errors else [float(v) for v in values]


def address_of(resource: Any, controls: Dict[str, Dict[str, Any]], aliases: Dict[str, str]) -> Tuple[str, str]:
    """A VI's VISA resource (a string, or a source such as a control's default) as an address: (address, the name
    the VI gives it). The address is "" when the name is neither an address nor an alias NI MAX knows."""
    text = resource if isinstance(resource, str) else value_of(resource, controls)
    if not isinstance(text, str) or not text.strip():
        return "", ""
    text = text.strip()
    hit = aliases.get(text) or next((a for k, a in aliases.items() if k.lower() == text.lower()), "")
    if hit:
        return hit, text
    if scripts.is_address(text):
        return normalize(text), text
    m = re.match(r"^([A-Za-z][\w-]*)/", text)   # an NI-DAQmx channel: Dev1/ai0
    return ("nidaq://%s" % m.group(1), text) if m else ("", text)


def walk(nodes: Any, loops: Optional[Dict[str, List[float]]] = None,
         controls: Optional[Dict[str, Dict[str, Any]]] = None) -> Iterator[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Every node of a diagram in order, nested ones too, each with the values of the loops it is in (by var)."""
    loops = loops or {}
    for n in nodes if isinstance(nodes, list) else []:
        if not isinstance(n, dict):
            continue
        yield n, loops
        kind = n.get("node")
        if kind == "loop":
            inner = dict(loops)
            if n.get("var"):
                inner[str(n["var"])] = range_values(n.get("range") or {}, controls or {})
            yield from walk(n.get("body"), inner, controls)
        for key in ("frames", "branches"):
            for part in n.get(key) or [] if kind in ("sequence", "parallel") else []:
                yield from walk(part, loops, controls)
        if kind == "case" and isinstance(n.get("cases"), dict):
            for part in n["cases"].values():
                yield from walk(part, loops, controls)


def controls_of(summary: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(c.get("name")): c for c in summary.get("controls") or [] if isinstance(c, dict)}


def measures(summary: Dict[str, Any]) -> bool:
    """Whether a VI talks to an instrument (VISA, NI-DAQmx, a driver's call): a measurement VI, to translate."""
    for n, _ in walk(summary.get("diagram")):
        kind = n.get("node")
        if kind in ("write", "query", "read", "daq") or (kind == "call" and (n.get("sends") or n.get("resource"))):
            return True
    return False


# ------------------------------------------------------------------ the survey

@dataclass
class Survey:
    folder: str
    max: Dict[str, Any] = field(default_factory=dict)              # read_max()
    projects: List[Dict[str, Any]] = field(default_factory=list)   # read_project() each
    vis: List[Dict[str, Any]] = field(default_factory=list)        # each VI's summary
    drivers: List[Dict[str, str]] = field(default_factory=list)    # instrument drivers its projects and folders hold
    libraries: List[str] = field(default_factory=list)             # the lab's own libraries (.lvlib, .llb, .lvclass)
    driver_vis: int = 0                                            # VIs in copies of LabVIEW's library folders
    instruments: List[Instrument] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    targets: Dict[str, str] = field(default_factory=dict)          # a VI that runs on a project's target: which

    def target_of(self, s: Dict[str, Any]) -> str:
        return self.targets.get(same_path(s.get("path")), "") if self.targets else ""

    @property
    def aliases(self) -> Dict[str, str]:
        """Every name NI MAX gives an address, a port's own too: what the VIs may open instruments by."""
        names = dict(self.max.get("ports") or {})
        names.update(self.max.get("aliases") or {})
        return names

    def translations(self, instruments: Dict[str, Tuple[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Each measurement VI read in full, translated into a scan (viscan.translate), by the VI's path. A VI that
        runs on a project's hardware target (a real-time system, an FPGA) runs there, so it is no scan of this
        computer's. instruments: {address_key(address): (the lab's name for it, its card)}."""
        from .viscan import on_target, translate
        return {s["path"]: (on_target(s, self.target_of(s)) if self.target_of(s) else
                            translate(s, instruments, self.aliases))
                for s in self.vis if s.get("read") == "full" and measures(s)}

    def text(self, scans: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
        """The report, for Kelvin's model: NI MAX, the projects, the instruments, then the VIs (with their scans)."""
        scans = scans or {}
        m = self.max
        lines = ["Read the LabVIEW in %s (only read, never run; LabVIEW isn't needed): %d project%s, %d VI%s, "
                 "%d instrument%s." % (self.folder, len(self.projects), "" if len(self.projects) == 1 else "s",
                                       len(self.vis), "" if len(self.vis) == 1 else "s", len(self.instruments),
                                       "" if len(self.instruments) == 1 else "s")]
        if m.get("files"):
            lines.append("NI MAX (%s):" % ", ".join(m["files"]))
            if m.get("aliases"):
                lines.append("  aliases: " + ", ".join("%s → %s" % kv for kv in m["aliases"].items()))
            other = [r for r in m.get("resources") or [] if r not in m.get("aliases", {}).values()]
            if other:
                lines.append("  resources without an alias: " + ", ".join(other))
            if m.get("daqmx"):
                lines.append("  NI-DAQmx devices: " + ", ".join("%s (%s)" % (d["name"], d["model"])
                                                                 for d in m["daqmx"]))
        else:
            lines.append("NI MAX: no visaconf.ini here (%s), nor in the folder" % VISACONF)
        for p in self.projects:
            lines += ["", "%s%s:" % (p["name"], " (%s)" % p["labview"] if p["labview"] else "")]
            if p["problem"]:
                lines.append("  " + p["problem"])
                continue
            lines.append("  %d VI%s, %d librar%s, %d dependenc%s" % (
                len(p["vis"]), "" if len(p["vis"]) == 1 else "s", len(p["libraries"]),
                "y" if len(p["libraries"]) == 1 else "ies", len(p["dependencies"]),
                "y" if len(p["dependencies"]) == 1 else "ies"))
            if p["drivers"]:
                lines.append("  drivers: " + ", ".join(_driver_words(d) for d in p["drivers"]))
            if p["uses"]:
                lines.append("  uses: " + ", ".join(p["uses"]))
            for t in p["targets"]:
                lines.append("  target %s (%s%s)%s: its VIs run there, not on this computer" % (
                    t["name"], t["type"], ", " + t["address"] if t["address"] else "",
                    ": modules " + ", ".join(x["model"] for x in t["modules"]) if t["modules"] else ""))
        for inst in self.instruments:
            lines += [""] + scripts._instrument_lines(inst)
        unread = [s for s in self.vis if s.get("read") == "no"]
        if self.vis:
            lines += ["", "VIs:"]
        for s in [s for s in self.vis if s.get("read") != "no"]:
            t = scans.get(s["path"])
            lines.append("  %s%s: read %s%s" % (s["name"], " (%s)" % s["labview"] if s.get("labview") else "",
                                                s["read"], "; not read: " + "; ".join(s["unread"]) if s["unread"]
                                                else ""))
            if t is not None:
                lines += ["    " + x for x in _scan_lines(t)]
        later = [s for s in unread if s.get("unread") == [NOT_READ_YET]]
        if later:
            names = ", ".join(s["name"] for s in later[:MAX_SHOWN])
            lines.append("  %s: %s%s" % (NOT_READ_YET, names, " and %d more" % (len(later) - MAX_SHOWN)
                                          if len(later) > MAX_SHOWN else ""))
        failed = [s for s in unread if s.get("unread") != [NOT_READ_YET]]
        if failed:   # files that aren't VIs Kelvin reads, damaged ones: each with why
            lines.append("  not read: " + "; ".join("%s (%s)" % (s["name"], "; ".join(s.get("unread") or [])[:160])
                                                     for s in failed[:MAX_SHOWN]) +
                         (" and %d more" % (len(failed) - MAX_SHOWN) if len(failed) > MAX_SHOWN else ""))
        if self.driver_vis:
            lines.append("  %d VIs in LabVIEW's library folders (drivers), not listed" % self.driver_vis)
        if self.skipped:
            lines += ["", "Not read: " + "; ".join(self.skipped[:10])]
        return "\n".join(lines)

    def view(self) -> Dict[str, Any]:
        """For the portal's Set up: the instruments as the script survey shows them, NI MAX, the projects and VIs."""
        m = self.max
        return {"kind": "labview", "folder": self.folder,
                "max": {k: m.get(k) for k in ("files", "aliases", "resources", "daqmx", "unread")},
                "projects": [{"name": p["name"], "path": p["path"], "labview": p["labview"], "vis": len(p["vis"]),
                              "drivers": [_driver_words(d) for d in p["drivers"]], "uses": p["uses"],
                              "targets": [t["name"] for t in p["targets"]], "problem": p["problem"]}
                             for p in self.projects],
                "vis": [{"name": s["name"], "path": s["path"], "labview": s.get("labview", ""), "read": s["read"],
                         "unread": s["unread"], "measures": measures(s), "target": self.target_of(s)}
                        for s in self.vis],
                "drivers": [_driver_words(d) for d in self.drivers], "driver_vis": self.driver_vis,
                "skipped": self.skipped, "instruments": [scripts.view_of(i) for i in self.instruments]}


def _driver_words(d: Dict[str, str]) -> str:
    who = " ".join(x for x in (d.get("maker"), d.get("model")) if x)
    return "%s (%s)" % (d["library"], who) if who and who not in d["library"] else d["library"]


def _scan_lines(t: Dict[str, Any]) -> List[str]:
    """A VI's translation in a few lines, for the model: the scan, what each part became, what didn't translate."""
    lines = ["scan %s: %s" % (t["name"], t["summary"]) if t.get("plan") else "no scan"]
    lines += ["  %s → %s" % (m["vi"], m["plan"]) for m in t.get("mapping") or []]
    if t.get("gaps"):
        lines.append("  not translated: " + "; ".join(t["gaps"]))
    if t.get("text"):
        lines += ["  plan:"] + ["    " + x for x in t["text"].rstrip().splitlines()]
    return lines


def same_path(path: Any) -> str:
    """A file's path as the survey matches it: resolved, in one case where the file system has none."""
    try:
        return os.path.normcase(str(Path(str(path or "")).resolve()))
    except (OSError, RuntimeError, ValueError):
        return os.path.normcase(str(path or ""))


def address_key(address: str) -> str:
    """An address as the survey and the translations match it: as VISA names it, in capitals."""
    from .adapters import resource_name
    return resource_name(address).upper()


def _files(root: Path, own: Iterable[Any]) -> Dict[str, Any]:
    """The LabVIEW files in a folder (or the one file): projects, VIs (the lab's, and those in copies of LabVIEW's
    library folders), libraries, NI-VISA's visaconf.ini and other .ini files (a MAX export)."""
    from .folderfiles import mine, run_of, secret
    out: Dict[str, Any] = {"projects": [], "vis": [], "driver_vis": [], "libraries": [], "visaconf": [], "ini": [],
                           "skipped": []}
    paths = [root] if root.is_file() else sorted(root.rglob("*"))
    for p in paths:
        rel = (p.relative_to(root).parts if p != root else (p.name,))
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel[:-1]):
            continue
        try:
            if not p.is_file() or secret(p) or mine(p.resolve(), own) is not None or run_of(p.resolve()) is not None:
                continue
        except OSError:
            continue
        suffix, in_lib = p.suffix.lower(), any(part.lower() in LIBRARY_DIRS for part in rel[:-1])
        if suffix == ".lvproj":
            out["projects"].append(p)
        elif suffix in (".vi", ".vit"):
            if in_lib:
                out["driver_vis"].append(p)
            elif len(out["vis"]) < MAX_VIS:
                out["vis"].append(p)
            elif not out["skipped"]:
                out["skipped"].append("more than %d VIs: the rest" % MAX_VIS)
        elif suffix in (".lvlib", ".llb", ".lvclass"):
            out["libraries"].append((p, in_lib))
        elif p.name.lower() == "visaconf.ini":
            out["visaconf"].append(p)
        elif suffix == ".ini":
            out["ini"].append(p)
    return out


def survey(path: Any, registry: Any = None, own: Iterable[Any] = (), visaconf: Any = None,
           read: Optional[Callable[[Path], Dict[str, Any]]] = None) -> Survey:
    """Read a lab's LabVIEW folder (or one project or VI) and NI MAX on this computer (visaconf: NI-VISA's file, by
    default where NI-VISA keeps it; one in the folder is read too): the instruments, projects and VIs. registry:
    Kelvin's cards, for the card that fits each instrument. own: Kelvin's own folders, never read. read: the VI
    reader (read_vi)."""
    from .folderfiles import mine, run_of
    root = Path(str(path)).expanduser()
    if not root.exists():
        raise FileNotFoundError("%s does not exist" % root)
    kelvins = mine(root.resolve(), own)
    if kelvins is not None:
        raise ValueError("%s is in Kelvin's own folder (%s), which holds the lab config, the settings, the accounts, "
                         "the instrument cards, and everyone's chats and runs: Kelvin's AI never reads there"
                         % (root, kelvins))
    if run_of(root.resolve()) is not None:
        raise ValueError("%s is in a Kelvin run, not the lab's LabVIEW: describe_run and get_run_data read runs" % root)
    reader = read or read_vi
    files = _files(root, own)
    out = Survey(folder=str(root), skipped=list(files["skipped"]))
    out.max = read_max([Path(str(visaconf)) if visaconf else VISACONF] + files["visaconf"], files["ini"])
    drivers: Dict[Tuple[str, str], Dict[str, str]] = {}

    def driver(d: Optional[Dict[str, str]], found_in: str) -> bool:
        if d and (d["maker"] or d["model"]):
            drivers.setdefault((d["maker"], d["model"]), dict(d, found=found_in))
            return True
        return False
    vis: List[Path] = list(files["vis"])
    for f in files["projects"]:
        p = read_project(f)
        out.projects.append(p)
        for d in p["drivers"]:
            driver(d, p["name"])
        for v in p["vis"]:   # the project's own VIs, wherever they are (never in Kelvin's own folders)
            q = Path(v["path"])
            try:
                if q.suffix.lower() in (".vi", ".vit") and q.is_file() and mine(q.resolve(), own) is None:
                    vis.append(q)
                    if v["target"]:
                        out.targets[same_path(q)] = v["target"]
            except (OSError, ValueError):
                continue
    for lib, in_lib in files["libraries"]:
        if not driver(driver_of(lib.name, "instr.lib" if in_lib else ""), lib.name) and not in_lib:
            out.libraries.append(str(lib))
    for f in files["driver_vis"]:   # instr.lib/Keithley 2400 Series/...: the driver's folder names it
        parts = list(f.relative_to(root).parts[:-1]) if f != root else []
        below = [x for k, x in enumerate(parts) if k and parts[k - 1].lower() == "instr.lib"]
        if below:
            driver(driver_of(below[0], "instr.lib"), "instr.lib")
    out.driver_vis = len(files["driver_vis"])
    seen = set()
    for f in vis:
        k = same_path(f)
        if k in seen:
            continue
        seen.add(k)
        try:
            s = reader(f)
            wrong = validate(s)
        except Exception as e:   # a VI that can't be read is said, never a failed survey
            s, wrong = None, [str(e).splitlines()[0][:160] if str(e) else type(e).__name__]
        if wrong:
            s = vi(f, read="no", unread=["not read: " + "; ".join(wrong[:3])])
        out.vis.append(s)
    for lib, in_lib in files["libraries"] if read is None else []:   # the lab's VI libraries: the VIs they hold
        if in_lib or lib.suffix.lower() != ".llb" or len(out.vis) >= MAX_VIS:
            continue
        try:
            held = read_library(lib)
        except Exception as e:
            held = [vi(lib, read="no", unread=["not read: %s" % (str(e).splitlines()[0][:160] or type(e).__name__)])]
        for s in held[:MAX_VIS - len(out.vis)]:
            out.vis.append(s if not validate(s) else vi(s.get("path") or lib, read="no",
                                                         unread=["not read: " + "; ".join(validate(s)[:3])]))
    out.drivers = list(drivers.values())
    out.instruments = _instruments(out, registry)
    return out


# ------------------------------------------------------------------ the instruments

def _instruments(found: Survey, registry: Any) -> List[Instrument]:
    """The instruments as LabVIEW knows them, one per address: NI MAX's aliases (their names) and resources, its
    NI-DAQmx devices, what the VIs read open and send, then each driver whose address only the VIs say. Each with the
    card that fits (scripts.match: a model named, the commands it takes; an NI-DAQmx device, Kelvin's DAQ card)."""
    from .adapters import is_serial, resource_name
    rows: Dict[str, Instrument] = {}
    order: List[Instrument] = []

    def at(address: str, where: str, library: str) -> Instrument:
        inst = rows.get(address_key(address))
        if inst is None:
            inst = rows[address_key(address)] = Instrument(address=address, names=[], library=library, where=[])
            order.append(inst)
        if where not in inst.where:
            inst.where.append(where)
        return inst
    m = found.max
    for alias, address in (m.get("aliases") or {}).items():
        inst = at(address, "NI MAX", "NI MAX alias")
        inst.names.append(alias)
        inst.hints += scripts._hints(alias)
    for address in m.get("resources") or []:
        if address_key(address) not in rows and not is_serial(resource_name(address)):
            at(address, "NI MAX", "NI MAX")
    daq = set()
    for dev in m.get("daqmx") or []:
        inst = at("nidaq://%s" % dev["name"], "NI MAX", "NI-DAQmx")
        inst.hints.append(dev["model"])
        daq.add(address_key(inst.address))
    loose: Dict[Tuple[str, str], Instrument] = {}   # drivers whose address no VI says

    def driver_row(d: Dict[str, str], where: str) -> None:
        if not d.get("model"):
            return
        inst = loose.get((d["maker"], d["model"]))
        if inst is None:
            inst = loose[(d["maker"], d["model"])] = Instrument(
                address="", names=[], library="LabVIEW driver", where=[], expression="in the VIs")
            inst.hints += _driver_hints(d)
        inst.calls.append(d["library"])
        if where not in inst.where:
            inst.where.append(where)
    for s in found.vis:
        _from_vi(s, found.aliases, at, driver_row, daq)
    for d in found.drivers:
        driver_row(d, d.get("found") or "LabVIEW")
    for inst in order + list(loose.values()):
        inst.hints = list(dict.fromkeys(h for h in inst.hints if h))
        inst.calls = list(dict.fromkeys(inst.calls))
        if registry is None:
            continue
        if address_key(inst.address) in daq and "ni/daqmx" in registry:
            card = registry.get("ni/daqmx")
            inst.card = {"id": card.id, "model": card.model, "kind": card.kind, "sure": True,
                         "why": "an NI-DAQmx device%s" % (" (%s)" % ", ".join(inst.hints[:1]) if inst.hints else ""),
                         "mapped": scripts.mapping(card, inst.commands), "limits": scripts.limits(card, inst.commands)}
        else:
            inst.card = scripts.match(inst, registry, named_in="LabVIEW")
    out = list(order)
    for inst in loose.values():   # a driver the one address with its card is for: that instrument's
        card = (inst.card or {}).get("id")
        same = [o for o in order if card and (o.card or {}).get("id") == card]
        if len(same) == 1:
            same[0].calls += [c for c in inst.calls if c not in same[0].calls]
            same[0].where += [w for w in inst.where if w not in same[0].where]
            continue
        out.append(inst)
    return out


def fold_drivers(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Set up's review rows, each driver's row (no address: the VIs give it) whose card exactly one row with an
    address has, once the instruments said who they are, folded into that row: one instrument, listed once."""
    out = []
    for item in items:
        card = (item.get("card") or {}).get("id")
        same = [o for o in items if o.get("address") and (o.get("card") or {}).get("id") == card] if card else []
        if not item.get("address") and len(same) == 1:
            same[0]["calls"] = list(dict.fromkeys(same[0]["calls"] + item["calls"]))[:10]
            same[0]["where"] = list(dict.fromkeys(same[0]["where"] + item["where"]))[:6]
            continue
        out.append(item)
    return out


def _from_vi(s: Dict[str, Any], aliases: Dict[str, str], at: Callable[[str, str, str], Instrument],
             driver_row: Callable[[Dict[str, str], str], None], daq: set) -> None:
    """What one VI says of the lab's instruments: the resources it opens, the commands it sends (with the values it
    sends them with, for the limits), the drivers it calls on them."""
    controls, name = controls_of(s), s["name"]
    opened: List[Instrument] = []
    for v in s.get("visa") or []:
        address, _ = address_of(v.get("resource") if isinstance(v, dict) else v, controls, aliases)
        if address:
            opened.append(at(address, name, "LabVIEW (VISA)"))
    for n, loops in walk(s.get("diagram"), controls=controls):
        kind = n.get("node")
        if kind == "daq":
            address, _ = address_of(str(n.get("channel") or ""), controls, aliases)
            if not address:
                continue
            inst = at(address, name, "NI-DAQmx")
            daq.add(address_key(address))
            channel = str(n["channel"]).split("/", 1)[1] if "/" in str(n.get("channel")) else ""
            if channel and n.get("op") == "write":
                inst.commands.append(Command("SET %s {v}" % channel, False, name,
                                             {"v": _numbers(n.get("value"), controls, loops)}))
            elif channel:
                inst.commands.append(Command("GET %s" % channel, True, name))
            continue
        if kind not in ("write", "query", "read", "call"):
            continue
        address, _ = address_of(n.get("resource"), controls, aliases)
        inst = at(address, name, "LabVIEW (VISA)") if address else None
        if inst is not None and inst not in opened:
            opened.append(inst)
        sends = [n] if kind in ("write", "query") else [x for x in n.get("sends") or [] if isinstance(x, dict)]
        for x in sends:
            text = str(x.get("text") or "").strip()
            if inst is None or not text:
                continue
            values = {k: _numbers(src, controls, loops) for k, src in (x.get("values") or {}).items()}
            var = next((src["loop"] for src in (x.get("values") or {}).values()
                        if isinstance(src, dict) and "loop" in src), "")
            inst.commands.append(Command(text, kind == "query" or bool(x.get("query")) or text.endswith("?"),
                                         name, values, var))
        if kind == "call":
            library = str(n.get("vi") or "").rpartition(":")[0]
            entry = next((e for e in s.get("linkage") or [] if e.get("name") == n.get("vi")), {})
            d = driver_of(library or str(n.get("vi") or ""), str(n.get("where") or entry.get("where") or ""))
            if d and inst is not None:
                inst.hints += _driver_hints(d)
                inst.calls.append(str(n.get("vi")))
            elif d and d.get("model"):
                driver_row(d, name)
    if len(opened) == 1 and s.get("read") != "full":   # its diagram unread, one instrument: its commands are those
        texts = {c.text for c in opened[0].commands}
        opened[0].commands += [Command(t, t.rstrip().endswith("?"), name) for t in s.get("scpi") or []
                               if isinstance(t, str) and t.strip() and t not in texts]
    for entry in s.get("linkage") or []:   # which instrument a linked driver is for, only its card can say
        d = driver_of(entry.get("library") or "", entry.get("where") or "")
        if d:
            driver_row(d, name)


def _numbers(src: Any, controls: Dict[str, Dict[str, Any]], loops: Dict[str, List[float]]) -> List[float]:
    """The numbers a source takes: a constant, a control's default, or each value of the loop it follows."""
    if isinstance(src, dict) and "loop" in src:
        scale, offset = src.get("scale", 1), src.get("offset", 0)
        return [x * scale + offset for x in loops.get(str(src["loop"])) or []]
    v = value_of(src, controls)
    values = v if isinstance(v, list) else [v]
    return [float(x) for x in values if _number(x)]
