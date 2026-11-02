"""A lab's Special Measure (sm, a MATLAB measurement program: a rack of instruments with named channels, scans run
as nested loops, a queue), read so the lab can switch to Kelvin directly: its instruments set up as its rack has them,
its channel names kept as names Kelvin knows (the lab config's names:), and its scans translated into Kelvin scans.
Nothing is ever run: no MATLAB (it need not be installed), and nothing is sent to an instrument.

    setup scripts   each script that builds the rack (smloadinst, smaddchannel): read line by line as the script
                    survey reads MATLAB (scripts.py), the if-blocks its *_Use flags switch decided; each instrument's
                    model (its sminst file), address (a GPIB board and address, a serial port, or the instrument
                    object the block opens itself), connection settings and the commands sent at start-up; each
                    named channel with its limits, ramp rate and multiplier; a Function instrument's formulas
    drivers         each model's control function (smc*.m): the command each of its channels sends, so each channel
                    finds its card's parameter (scripts.mapping), else its name does
    scans           those saved from sm's scan editor (smscan, a list of scans) and those in the newest runs (each
                    run file keeps its scan), translated by fixed rules (translate()): what didn't translate is said
    runs            the run files (NNN_name.mat), counted by folder: importers/specialmeasure.py reads each as a run

survey() gathers them by instrument in the script survey's form (scripts.Instrument, its card by scripts.match), for
the same review in Set up. Special Measure is GPL software: none of its code is in Kelvin; only a lab's own files are
read, where they are."""
from __future__ import annotations

import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import expr, scripts
from .scripts import Command, Instrument, _call_args, _matlab_args, _matlab_code, _setting, normalize

MAX_LOOKED = 200_000        # files and folders a survey looks through in one folder
WALK_SECONDS = 60.0         # and for at most this long
MAX_SCRIPT_BYTES = 2_000_000
MAX_SCAN_FILES = 2_000      # saved scans read from one folder
RECENT_RUNS = 200           # the newest runs whose scans are read for the scans the lab ran lately
SKIP_DIRS = {".git", ".svn", ".hg", "__pycache__", "node_modules", ".venv", "venv"}
RUN_FILE = re.compile(r"^(\d{3,4})_(.+)\.mat$", re.I)   # smrun's own names: run number, then the scan's name
DUMMIES = {"test", "time"}  # sm's own instruments without hardware: a counter and the clock
MAX_SCANS = 80              # scans the review lists: the newest, one per layout
# Special Measure's model names a card's model doesn't say (no model number, or a maker's word in front): its card
KNOWN_CARDS = {"andorixon": "andor/ccd", "andor": "andor/ccd", "optc": "qd/multivu", "ppms": "qd/multivu",
               "dynacool": "qd/multivu", "rigoldg1022z": "rigol/dg1000z", "sp150": "princeton/spectrapro",
               "sp300": "princeton/spectrapro", "sp2150": "princeton/spectrapro", "sp2300": "princeton/spectrapro",
               "sp2500": "princeton/spectrapro", "sp2750": "princeton/spectrapro", "thorkdc101": "thorlabs/kdc101",
               "pem200": "hinds/pem200"}
# the words a channel's name and a card parameter's name or label share, for a channel no command says
SYNONYMS = {"theta": ("phase", "theta"), "phas": ("phase",), "freq": ("frequency", "freq"),
            "vref": ("amplitude", "sine_out", "level", "slvl"), "slvl": ("amplitude", "level"),
            "tau": ("tc", "time_constant", "timeconstant"), "oflt": ("tc", "time_constant"),
            "sens": ("sensitivity",), "v": ("v", "voltage", "volt", "level"), "i": ("i", "current", "curr"),
            "b": ("b", "field"), "t": ("t", "temperature", "temp", "t_sample"), "lambda": ("wavelength", "lambda"),
            "temp": ("t", "temperature", "t_sample", "temp"), "field": ("b", "field"),
            "x": ("x",), "y": ("y",), "r": ("r", "magnitude", "mag"), "val": ("value", "reading", "v", "i")}


@dataclass
class Channel:
    """One of the rack's named channels: an instrument's channel (smaddchannel(inst, channel, name, rangeramp))."""
    name: str                  # the rack's name for it (Vbg, X_A), the lab's own
    channel: str               # the instrument's channel (X, CH0, V)
    low: float = -math.inf
    high: float = math.inf
    rate: float = math.inf     # its ramp rate in the channel's unit per second (inf: set at once)
    factor: float = 1.0        # the instrument gets value * factor (sm's conversion factor)
    where: str = ""
    param: Optional[str] = None   # its card parameter, once a card fits
    set_param: Optional[str] = None   # what sets it, where the card sets it through a set point (T_set for T)
    why: str = ""                 # how that was found
    scale: float = 1.0            # its driver sends value x factor x scale + offset (its own conversion, read from its
    offset: float = 0.0           # code: a PEM's wavelength x its retardance, a DAQ's position / 20 - 5 V)
    send: str = ""                # the DAQ channel its driver writes (ao0): the card parameter it is
    how: str = ""                 # that conversion, in words
    native: bool = False          # its card takes it in the channel's own unit (card_units): no conversion

    def multiplier(self) -> float:
        """A value of the scan's times this is the card's (plus the offset): sm's multiplier and its driver's."""
        return 1.0 if self.native else self.factor * self.scale

    def to_instrument(self, v: float) -> float:
        """A value as the scan has it (sm's units), as the card takes it."""
        return v * self.multiplier() + (0.0 if self.native else self.offset)


@dataclass
class RackInstrument:
    """An instrument of the rack as one setup script loads it."""
    model: str                 # its sminst file's name (SR830, K2400, Function)
    name: str                  # smdata.inst().name, else the model
    inst: Instrument           # the survey's form: address, connection, commands sent at start-up, hints, card
    channels: List[Channel] = field(default_factory=list)
    used: bool = True          # the script loads it (its *_Use flag on); one it skips is listed, not proposed
    block: int = 0             # the if-block it is loaded in
    driver: str = ""           # its control function's file (smc*.m), when the folder has it
    helpers: List[str] = field(default_factory=list)   # scripts its block calls to connect it (Initial_Montana)
    dependences: List[str] = field(default_factory=list)   # a Function instrument: the channels it sets
    formulas: List[str] = field(default_factory=list)      # ... and the formula for each (MATLAB, as written)
    numbered: Dict[str, List[str]] = field(default_factory=dict)   # its driver's commands, by its channels' names
    sends: List[Command] = field(default_factory=list)   # what its driver can send: only to find its card
    order: List[str] = field(default_factory=list)   # its channels in its sminst file's order (channel 1 first)
    data: Dict[str, float] = field(default_factory=dict)   # its data's numbers (its sminst file's, then the setup's)
    daq: str = ""              # the NI DAQ device its driver writes through MATLAB's DAQ toolbox (Dev2)

    @property
    def function(self) -> bool:
        return self.model.lower() == "function"


@dataclass
class Setup:
    """One setup script read: the rack it builds."""
    path: str
    modified: float
    instruments: List[RackInstrument] = field(default_factory=list)
    problem: str = ""

    def channels(self) -> Dict[str, Tuple[RackInstrument, Channel]]:
        """Each channel the rack names, by its name (the last one loaded wins, as smaddchannel's does)."""
        out: Dict[str, Tuple[RackInstrument, Channel]] = {}
        for ri in self.instruments:
            for ch in ri.channels:
                out[ch.name] = (ri, ch)
        return out


@dataclass
class Found:
    folder: str
    setups: List[Setup] = field(default_factory=list)
    scans: List[Dict[str, Any]] = field(default_factory=list)   # {name, path, source: saved | run, scan, runs}
    runs: Dict[str, int] = field(default_factory=dict)          # folder (relative) -> run files in it
    drivers: Dict[str, str] = field(default_factory=dict)       # control function -> its file
    skipped: List[str] = field(default_factory=list)
    unread: List[str] = field(default_factory=list)             # .mat files Kelvin couldn't read (and why)

    @property
    def setup(self) -> Optional[Setup]:
        """The setup script the review starts from: the one changed last."""
        return max(self.setups, key=lambda s: s.modified) if self.setups else None

    def chosen(self, path: Optional[str] = None) -> Optional[Setup]:
        return next((s for s in self.setups if s.path == path), None) if path else self.setup

    @property
    def instruments(self) -> List[Instrument]:
        """The chosen setup's instruments with hardware, in the script survey's form, the ones it uses first."""
        s = self.setup
        return [ri.inst for ri in _hardware(s)] if s else []

    def view(self, path: Optional[str] = None) -> Dict[str, Any]:
        """For the portal's Set up: the setup scripts, the chosen one's instruments (as the script survey's review
        shows them, with their channels and the names they give), its function channels, the scans found and the
        runs."""
        s = self.chosen(path)
        items = []
        for ri in _hardware(s) if s else []:
            item = scripts.view_of(ri.inst)
            item.update(model=ri.model, rack_name=ri.name, used=ri.used, driver=ri.driver, helpers=ri.helpers,
                        startup=[c.text for c in ri.inst.commands][:20],
                        channels=[_channel_view(ch) for ch in ri.channels])
            items.append(item)
        return {"folder": self.folder, "setups": [{"path": x.path, "modified": x.modified, "problem": x.problem,
                                                   "instruments": len(_hardware(x))} for x in self.setups],
                "setup": s.path if s else "", "instruments": items,
                "functions": [{"name": ri.name, "variables": function_variables(ri), "sets": ri.dependences,
                               "formulas": ri.formulas} for ri in (s.instruments if s else []) if ri.function],
                "scans": [{k: x[k] for k in ("name", "path", "source", "runs")} for x in self.scans],
                "runs": dict(sorted(self.runs.items())), "skipped": self.skipped[:20], "unread": self.unread[:20]}

    def text(self, path: Optional[str] = None) -> str:
        """The report, for Kelvin's model: the setup scripts, the chosen rack, its channels and the scans."""
        s = self.chosen(path)
        lines = ["Read Special Measure in %s (only read, never run): %d setup script%s, %d scan%s, %d run file%s." % (
            self.folder, len(self.setups), "" if len(self.setups) == 1 else "s", len(self.scans),
            "" if len(self.scans) == 1 else "s", sum(self.runs.values()), "" if sum(self.runs.values()) == 1 else "s")]
        if len(self.setups) > 1:
            lines.append("Setup scripts: " + ", ".join(x.path for x in sorted(self.setups, key=lambda x: -x.modified)))
        if s is None:
            return "\n".join(lines + ["No setup script (smloadinst, smaddchannel) in the folder."])
        lines.append("The rack of %s:" % s.path)
        for ri in s.instruments:
            if ri.model.lower() in DUMMIES:
                continue
            card = (ri.inst.card or {}).get("id")
            head = "  %s (%s) %s%s%s" % (ri.name, ri.model, ri.inst.address or "no address" + (
                " (connected by %s)" % ", ".join(ri.helpers) if ri.helpers else ""),
                ", card %s" % card if card else "", "" if ri.used else "; the script doesn't load it (its flag is 0)")
            lines.append(head)
            for ch in ri.channels:
                lines.append("    %s = %s.%s%s%s" % (ch.name, ri.name, ch.channel, " → %s" % ch.param if ch.param
                                                     else "", _range_words(ch)))
            if ri.function:
                for dep, f in zip(ri.dependences, ri.formulas):
                    lines.append("    sets %s = %s" % (dep, f))
            if ri.inst.commands:
                lines.append("    sent at start-up: " + "; ".join(c.text for c in ri.inst.commands[:8]))
        if self.scans:
            lines.append("Scans: " + ", ".join("%s (%s)" % (x["name"], x["source"]) for x in self.scans[:40]))
        if self.runs:
            lines.append("Run files: " + ", ".join("%s: %d" % kv for kv in sorted(self.runs.items())[:20]))
        return "\n".join(lines)


def _hardware(s: Optional[Setup]) -> List[RackInstrument]:
    """A rack's instruments with hardware (not sm's counter and clock, nor a Function), the ones it uses first."""
    if s is None:
        return []
    out = [ri for ri in s.instruments if ri.model.lower() not in DUMMIES and not ri.function]
    return sorted(out, key=lambda ri: not ri.used)


def _channel_view(ch: Channel) -> Dict[str, Any]:
    out = {"name": ch.name, "channel": ch.channel, "param": ch.param, "why": ch.why}
    for k in ("low", "high", "rate"):
        v = getattr(ch, k)
        out[k] = v if math.isfinite(v) else None
    out.update(factor=ch.factor, scale=ch.scale, offset=ch.offset, send=ch.send, how=ch.how, native=ch.native)
    return out


def _range_words(ch: Channel) -> str:
    bits = []
    if math.isfinite(ch.low) or math.isfinite(ch.high):
        bits.append("limits [%s, %s]" % (_num(ch.low), _num(ch.high)))
    if math.isfinite(ch.rate):
        bits.append("ramps at %s/s" % _num(ch.rate))
    if ch.how:
        bits.append(ch.how)
    elif ch.multiplier() != 1 or ch.offset:
        bits.append("the instrument gets %s × the value%s" % (_num(ch.factor * ch.scale), " %s %s" % (
            "-" if ch.offset < 0 else "+", _num(abs(ch.offset))) if ch.offset else ""))
    return " (%s)" % ", ".join(bits) if bits else ""


def _num(x: float) -> str:
    return "%.6g" % x if isinstance(x, float) else str(x)


# ------------------------------------------------------------------ a setup script

class _Setup(scripts._Matlab):
    """A setup script read as Special Measure would run it, without running it: which if-blocks run (the flags
    a script sets first decide them: LockIn_Use = 1; if LockIn_Use == 1 ... end), each smloadinst and the instrument
    it makes, its name, its channels, the settings and commands it gets, and a Function's formulas. Statements in a
    block that doesn't run are read too: their instruments are listed as not used."""

    def __init__(self, path: str, text: str, collect: Any):
        super().__init__(path, text, collect)
        self.rack: List[RackInstrument] = []
        self.current: Optional[RackInstrument] = None
        self.blocks: List[Tuple[int, bool, bool]] = []   # each open block: (its id, running now, a branch ran)
        self.next_block = 0
        self.opens: Dict[int, List[Instrument]] = {}     # instrument objects a block opens itself (gpib, visa...)
        self.calls: Dict[int, List[str]] = {}             # scripts a block calls (Initial_Montana)

    @property
    def running(self) -> bool:
        return all(run for _, run, _ in self.blocks)

    @property
    def block(self) -> int:
        return self.blocks[-1][0] if self.blocks else 0

    def condition(self, text: str) -> Optional[bool]:
        """An if's condition from what the script set before it: flags and numbers (== ~= < > <= >=, && ||, ~);
        None when it can't be told (the block is then taken to run, as a person would read it)."""
        text = text.strip().rstrip(",;")
        for joiner, both in (("||", any), ("&&", all)):
            if joiner in text:
                parts = [self.condition(p) for p in text.split(joiner)]
                return None if None in parts else both(parts)
        if text.startswith("~") and not text.startswith("~="):
            inner = self.condition(text[1:].strip("() "))
            return None if inner is None else not inner
        m = re.fullmatch(r"\(?\s*(.+?)\s*(==|~=|<=|>=|<|>)\s*(.+?)\s*\)?", text)
        if m:
            a, b = self.number(m.group(1)), self.number(m.group(3))
            if a is None or b is None:
                return None
            return {"==": a == b, "~=": a != b, "<=": a <= b, ">=": a >= b, "<": a < b, ">": a > b}[m.group(2)]
        n = self.number(text.strip("() "))
        return None if n is None else n != 0

    def statement(self, code: str, line: int, comment: str) -> None:
        parts = _top_split(code, ",")
        if len(parts) > 1:   # statements on one line: if X, smloadinst(...), end
            for part in parts:
                self.statement(part, line, comment)
            return
        if not code:
            return
        m = re.match(r"^(else|try)\s+(\S.*)$", code)
        if m:   # else if X: else, then an if of its own (which has its own end); try smopen(...)
            self.statement(m.group(1), line, comment)
            self.statement(m.group(2), line, comment)
            return
        m = re.match(r"^(if|elseif)\b\s*(.*)$", code)
        if m:
            cond = self.condition(m.group(2))
            if m.group(1) == "if":
                self.next_block += 1
                run = cond is not False
                self.blocks.append((self.next_block, run, run))
            elif self.blocks:
                bid, _, ran = self.blocks[-1]
                run = not ran and cond is not False
                self.blocks[-1] = (bid, run, ran or run)
            return
        if re.match(r"^else\b", code) and self.blocks:
            bid, _, ran = self.blocks[-1]
            self.blocks[-1] = (bid, not ran, True)
            return
        if re.match(r"^try\b", code):
            self.blocks.append((self.block, True, True))   # a try runs; its catch only on an error
            return
        if re.match(r"^catch\b", code) and self.blocks:
            bid, _, _ = self.blocks[-1]
            self.blocks[-1] = (bid, False, True)
            return
        if re.match(r"^(for|parfor|while|switch|function)\b", code):
            self.blocks.append((self.block, True, True))
            super().statement(code, line, comment)
            return
        if re.match(r"^end\b", code):
            if self.blocks:
                self.blocks.pop()
            self.depth = max(0, self.depth - 1)
            return
        super().statement(code, line, comment)

    def part(self, code: str, where: str, comment: str) -> None:
        code = code.strip().rstrip(",").strip()
        m = re.match(r"^(?:\[?\s*([A-Za-z]\w*)\s*\]?\s*=\s*)?smloadinst\s*\(", code)
        if m:
            self.load(_matlab_args(code[m.end():]), where)
            return
        args = _call_args(code, "smaddchannel")
        if args is not None and code.startswith("smaddchannel"):
            self.channel(args, where)
            return
        m = re.match(r"^smdata\.inst\(\s*\w+\s*\)\.(name|data\.dependences|data\.formula)\s*=\s*(.+)$", code, re.S)
        if m and self.current is not None:
            what, rhs = m.group(1), m.group(2).strip()
            if what == "name":
                name, ok = self.string(rhs)
                if ok and name.strip():
                    self.current.name = name.strip()
                    self.current.inst.names = [name.strip()]   # the name Kelvin proposes for it, from the rack's
            elif rhs.startswith("{") and rhs.endswith("}"):
                cells = _matlab_args(rhs[1:-1])
                if what == "data.dependences":
                    self.current.dependences = [s for s, ok in (self.string(c) for c in cells) if ok]
                else:
                    self.current.formulas = [c.strip() for c in cells if c.strip()]
            return
        m = re.match(r"^smdata\.inst\(\s*\w+\s*\)\.data\.(\w+)\s*=\s*([^;]+)$", code, re.S)
        if m and self.current is not None and self.number(m.group(2)) is not None:
            self.current.data[m.group(1)] = self.number(m.group(2))   # a number its driver may convert with
            return
        args = _call_args(code, "set")
        if args is not None and code.startswith("set") and args and args[0].startswith("smdata.inst(") \
                and self.current is not None:
            for key, val in zip(args[1::2], args[2::2]):   # set(smdata.inst(ind).data.inst, 'BaudRate', 9600)
                name, ok = self.string(key)
                n = self.number(val)
                s = _setting(name, n if n is not None else self.string(val)[0]) if ok else None
                if s:
                    self.current.inst.settings.update(s)
            return
        if re.fullmatch(r"[A-Za-z]\w*", code) and code not in ("smopen", "smprintinst", "smprintchannels", "sm",
                                                               "smgui", "smgui_small", "instrreset", "clc"):
            self.calls.setdefault(self.block, []).append(code)   # a script the block runs (Initial_Montana)
            self.pair()
            return
        before = len(self.survey.items)
        super().part(code, where, comment)
        made = self.survey.items[before:]
        if made:   # an instrument object the block opens itself: the rack's instrument there takes its address
            self.opens.setdefault(self.block, []).extend(made)
            self.pair()

    def load(self, args: List[str], where: str) -> None:
        """smloadinst(model, ind, adaptor, ...): the instrument and its address, as smloadinst would open it."""
        model, ok = self.string(args[0]) if args else ("", False)
        if not ok or not model.strip():
            return
        model = re.sub(r"(?i)^sminst_", "", model.strip())
        strs = [self.string(a) for a in args]
        nums = [self.number(a) for a in args]
        address, settings = "", {}
        adaptor = strs[2][0].strip().lower() if len(args) > 2 and strs[2][1] else ""
        if adaptor == "serial" and len(args) > 3 and strs[3][1]:
            address = normalize(strs[3][0])
        elif adaptor not in ("", "none", "serial") and len(args) > 4 and nums[3] is not None and nums[4] is not None:
            address = "GPIB%d::%d::INSTR" % (int(nums[3]), int(nums[4]))   # (adaptor, board, address): gpib()
        elif adaptor not in ("", "none") and re.fullmatch(r"(?i)com\d+", adaptor):
            address = normalize(adaptor)   # smloadinst(model, [], 'COM3'): a port given as the adaptor
        elif adaptor not in ("", "none") and len(args) > 3 and strs[3][1] and scripts.is_address(strs[3][0]):
            address = normalize(strs[3][0])   # a VISA resource as the constructor's argument
        inst = Instrument(address=address, names=[], library="Special Measure (%s)" % model, where=[where],
                          settings=settings, expression="" if address or adaptor in ("", "none") else
                          ", ".join(a.strip() for a in args[2:]))   # an address the script leaves unset
        inst.hints += model_hints(model) + scripts._hints(model)
        ri = RackInstrument(model, model, inst, used=self.running, block=self.block)
        self.rack.append(ri)
        self.current = ri
        self.conns["smdata.inst(ind).data.inst"] = inst   # fprintf(smdata.inst(ind).data.inst, ...) at start-up
        self.conns["smdata.inst(pos).data.inst"] = inst
        self.note("load %s → %s" % (model, address or "(connected by its block)"))
        self.pair()

    def channel(self, args: List[str], where: str) -> None:
        """smaddchannel(inst, channel, name, rangeramp): a named channel of an instrument the rack has."""
        if len(args) < 3:
            return
        owner, ok1 = self.string(args[0])
        chan, ok2 = self.string(args[1])
        name, ok3 = self.string(args[2])
        if not (ok1 and ok2 and ok3) or not name.strip():
            return
        owner = owner.strip()
        ri = next((r for r in reversed(self.rack) if r.name == owner), None) or \
            next((r for r in reversed(self.rack) if r.model == owner), None)
        if ri is None:
            return
        ch = Channel(name.strip(), chan.strip(), where=where)
        if len(args) > 3:
            nums = _vector(args[3], self)
            if nums:
                rr = nums + [math.inf, 1.0][len(nums) - 2:] if len(nums) >= 2 else []
                if len(rr) >= 4:
                    ch.low, ch.high, ch.rate, ch.factor = rr[0], rr[1], rr[2], rr[3] or 1.0
        ri.channels = [c for c in ri.channels if c.name != ch.name] + [ch]
        self.note("channel %s = %s.%s" % (ch.name, ri.name, ch.channel))

    def pair(self) -> None:
        """A block that loads an instrument without an address (adaptor 'None') and opens the instrument object
        itself (global SP150; SP150 = visa('ni', 'GPIB0::11::INSTR')): the rack's instrument is that one."""
        bid = self.block
        loose = [r for r in self.rack if r.block == bid and not r.inst.address and bid]
        opened = [i for i in self.opens.get(bid, []) if i.address]
        if loose and opened:
            ri, got = loose[0], opened[0]
            ri.inst.address, ri.inst.settings = got.address, dict(got.settings, **ri.inst.settings)
            ri.inst.commands = got.commands + ri.inst.commands
            ri.inst.names = got.names
            ri.inst.hints += got.hints
            got.address = ""   # the survey's own copy is the rack's now
        for r in self.rack:   # the scripts its block runs (Initial_Montana), which may connect it
            if r.block == bid and bid:
                r.helpers = list(dict.fromkeys(self.calls.get(bid, [])))


def _vector(text: str, reader: Any) -> List[float]:
    """[-Inf, Inf, Inf, 1] or [-80 80 40 1]: its numbers (Inf too)."""
    t = text.strip()
    if not (t.startswith("[") and t.endswith("]")):
        return []
    out = []
    for part in re.split(r"[,\s]+", t[1:-1].strip()):
        if not part:
            continue
        p = part.lower()
        if p in ("inf", "+inf"):
            out.append(math.inf)
        elif p == "-inf":
            out.append(-math.inf)
        else:
            n = reader.number(part)
            if n is None:
                return []
            out.append(n)
    return out


# ------------------------------------------------------------------ drivers: the command each channel sends

def _code(text: str) -> str:
    """A MATLAB file's code: comments out, continued lines joined."""
    code = "\n".join(_matlab_code(line)[0] for line in text.splitlines())
    return re.sub(r"\.\.\.\s*\n", " ", code)


def driver_commands(text: str) -> Dict[int, List[str]]:
    """A control function's commands by channel number: a cmds = {'OUTP 1', 'FREQ', ...} list (SR830's form: a
    channel's command, its query being the same with ?), and the strings each case of its switch on the channel
    sends (driver_cases)."""
    out: Dict[int, List[str]] = {}
    m = re.search(r"\bcmds\s*=\s*\{(.*?)\}", _code(text), re.S)
    if m:
        for k, cell in enumerate(_matlab_args(m.group(1).replace("\n", " ")), 1):
            s = re.fullmatch(r"\s*'([^']*)'\s*", cell)
            if s and s.group(1).strip():
                out.setdefault(k, []).append(s.group(1).strip())
    for n, ops in driver_cases(text).items():   # what this channel's case sends, the inner cases' too
        for statements in ops.values():
            for t in statements:
                for sent in re.findall(r"'([^'\n]{1,80})'", t):
                    if _command_like(sent):
                        out.setdefault(n, []).append(sent.strip())
    return out


def driver_cases(text: str) -> Dict[int, Dict[Any, List[str]]]:
    """A control function's statements by the channel each case of its switch on the channel is for (switch ic(2),
    by whatever name its first argument has: ico), and within that case by the operation (switch ic(3), or if
    ic(3) == 1): 1 sets, 0 reads, None either. Read a statement at a time: if x, fprintf(...), end on one line opens
    and ends a block; else if is an if of its own, with its own end."""
    code = _code(text)
    m = re.search(r"\bfunction\b[^(\n]*\(\s*(\w+)", code)
    first = re.escape(m.group(1) if m else "ic")
    on_channel = re.compile(r"^switch\s*\(?\s*%s\s*\(\s*2\s*\)" % first)
    on_op = re.compile(r"^switch\s*\(?\s*%s\s*\(\s*3\s*\)" % first)
    if_op = re.compile(r"^(if|elseif)\s*\(?\s*%s\s*\(\s*3\s*\)\s*==\s*(\d+)" % first)
    out: Dict[int, Dict[Any, List[str]]] = {}
    stack: List[List[Any]] = []   # each open block: [what it is (channel, op, if-op, other), its case now]

    def one(t: str) -> None:
        if on_channel.match(t):
            stack.append(["channel", []])
            return
        if on_op.match(t):
            stack.append(["op", None])
            return
        m = if_op.match(t)
        if m and m.group(1) == "if":
            stack.append(["if-op", int(m.group(2))])
            return
        if re.match(r"^(elseif|else)\b", t):
            if stack and stack[-1][0] == "if-op":   # another operation, or the rest of them
                stack[-1][1] = int(m.group(2)) if m else -1
            return
        if re.match(r"^(if|for|parfor|while|switch|try|function)\b", t):
            stack.append(["other", None])
            return
        if re.match(r"^end\b", t):
            if stack:
                stack.pop()
            return
        if re.match(r"^(case|otherwise)\b", t):
            if stack and stack[-1][0] in ("channel", "op"):
                group = re.match(r"^case\s+(\{[^}]*\}|[\d\s,]+)", t)
                numbers = [int(x) for x in re.findall(r"\d+", group.group(1))] if group else []
                if stack[-1][0] == "channel":
                    stack[-1][1] = numbers
                else:
                    stack[-1][1] = numbers[0] if len(numbers) == 1 else None
            return
        # the innermost channel case, and the innermost operation (inside it, or around it: switch ic(3) first)
        chans = next((now for kind, now in reversed(stack) if kind == "channel"), None)
        what = next((now for kind, now in reversed(stack) if kind in ("op", "if-op")), None)
        for n in chans or []:
            out.setdefault(n, {}).setdefault(what, []).append(t)

    for line in code.split("\n"):
        for t in _top_split(line, ";,"):
            m = re.match(r"^(else|try)\s+(\S.*)$", t)
            for part in ([m.group(1), m.group(2)] if m else [t]):
                one(part.strip())
    return out


_DATA_PATH = re.compile(r"smdata\.inst\((?:[^()]|\([^()]*\))*\)\.data\.(\w+)")


def driver_value(text: str) -> str:
    """The name a control function gives the value it sets (function val = smcSR830(ic, val, rate): val)."""
    m = re.search(r"\bfunction\b[^(\n]*\(\s*\w+\s*,\s*(\w+)", _code(text))
    return m.group(1) if m else "val"


def sent_value(statements: List[str], data: Dict[str, float], value_name: str = "val") -> Optional[Dict[str, Any]]:
    """What a channel's set code sends of the value it is given (value_name: val), when that is val x scale +
    offset: {to: ("daq", the session it writes) or ("command", its text), scale, offset, text: the expression}, worked
    out from the assignments before it and the instrument's data (data.retardance); None when it can't be told."""
    known: Dict[str, str] = {}
    for raw in statements:
        st = _DATA_PATH.sub(lambda mm: "data_" + mm.group(1), raw).strip().rstrip(";").strip()
        to: Optional[Tuple[str, str]] = None
        value: Optional[str] = None
        m = re.match(r"^outputSingleScan\s*\((.*)\)$", st, re.S)
        if m:
            args = _matlab_args(m.group(1))
            if len(args) >= 2:   # the session by its own name (data.OutX: OutX), as daq_sessions has it
                session = re.sub(r"^.*?(\w+)\s*$", r"\1", args[0])
                to, value = ("daq", session[5:] if session.startswith("data_") else session), args[1]
        else:
            m = re.match(r"^(?:fprintf|fwrite|writeline|write)\s*\((.*)\)$", st, re.S)
            if m:
                args = _matlab_args(m.group(1))
                got = _sent_in(args[1:], known)
                if got:
                    to, value = ("command", got[0]), got[1]
        if value is not None:
            text = _resolved(value, known, data, value_name)
            conv = _affine(text, value_name) if text is not None else None
            return dict(to=to, scale=conv[0], offset=conv[1], text=text) if conv else None
        m = re.match(r"^([A-Za-z_]\w*)\s*=(?!=)\s*(.+)$", st, re.S)
        if m:
            known[m.group(1)] = m.group(2).strip()
    return None


def _sent_in(args: List[str], known: Dict[str, str]) -> Optional[Tuple[str, str]]:
    """A write's command and the value in it: [cmd num2str(x, '%f')], sprintf(fmt, ..., x), or fmt, ..., x."""
    if not args:
        return None
    s = args[0].strip()
    words = " ".join(w for w in re.findall(r"'([^']*)'", s + " " + " ".join(
        known.get(n, "") for n in re.findall(r"[A-Za-z_]\w*", s))) if not w.startswith("%")).strip()
    if len(args) >= 2:
        value = args[-1]
    elif s.startswith("sprintf"):
        inner = _call_args(s, "sprintf") or []
        value = inner[-1] if len(inner) >= 2 else None
    else:
        m = re.search(r"num2str\s*\(", s)
        inner = _call_args(s[m.start():], "num2str") if m else None
        value = inner[0] if inner else None
    if value is None or "{" in value or value.strip().startswith(("'", '"')):
        return None
    return words, value


def _resolved(value: str, known: Dict[str, str], data: Dict[str, float], value_name: str = "val") -> Optional[str]:
    """value with what it names put in: the code's own assignments, then the instrument's data as numbers."""
    text = value
    for _ in range(8):
        def put(m: "re.Match[str]") -> str:
            name = m.group(0)
            if name != value_name and name in known:
                return "(%s)" % known[name]
            if name.startswith("data_") and name[5:] in data:
                return "(%r)" % float(data[name[5:]])
            return name
        new = re.sub(r"(?<![\w.'])[A-Za-z_]\w*(?!\s*\()", put, text)
        if new == text:
            return text
        text = new
    return None


def _affine(text: str, value_name: str = "val") -> Optional[Tuple[float, float]]:
    """(scale, offset) when text is the value x scale + offset, checked at three values; None for anything else."""
    kelvin = expression("@(%s) %s" % (value_name, text), ["val"])
    if kelvin is None:
        return None
    try:
        f0, f1, f7 = (expr.scalar(kelvin, {"val": v}) for v in (0.0, 1.0, 7.0))
    except expr.ExprError:
        return None
    scale, offset = f1 - f0, f0
    if not scale or abs(f7 - (offset + 7 * scale)) > 1e-9 * max(1.0, abs(f7)):
        return None
    return _round(scale), _round(offset)


def daq_sessions(text: str) -> Tuple[Dict[str, List[Tuple[str, str, str]]], bool]:
    """The DAQ sessions a MATLAB file makes with the Data Acquisition Toolbox, by the name the driver writes them by
    (smdata.inst(ind).data.OutX: OutX): each one's channels (device, channel, out or in); and whether they are NI's
    (daq.createSession('ni'), daq("ni"))."""
    code = _code(text)
    out: Dict[str, List[Tuple[str, str, str]]] = {}
    pats = (r"([\w.()]+?)\s*\.\s*addAnalog(Output|Input)Channel\s*\(\s*'([^']+)'\s*,\s*'([^']+)'",
            r"addAnalog(Output|Input)Channel\s*\(\s*([\w.()]+?)\s*,\s*'([^']+)'\s*,\s*'([^']+)'",
            r"add(output|input)\s*\(\s*([\w.()]+?)\s*,\s*[\"']([^\"']+)[\"']\s*,\s*[\"']([^\"']+)[\"']")
    for k, pat in enumerate(pats):
        for m in re.finditer(pat, code):
            session, way = (m.group(1), m.group(2)) if k == 0 else (m.group(2), m.group(1))
            name = re.sub(r"^.*?(\w+)\s*$", r"\1", session)
            out.setdefault(name, []).append((m.group(3), m.group(4).lower(), "out" if way.lower() == "output" else "in"))
    ni = bool(re.search(r"daq\s*\.\s*createSession\s*\(\s*'ni'|\bdaq\s*\(\s*[\"']ni[\"']", code, re.I))
    return out, ni


def _top_split(code: str, seps: str) -> List[str]:
    """A line's statements, split at the separators (MATLAB's , and ;) outside brackets and quotes."""
    out, depth, cur, quote, prev = [], 0, [], "", ""
    for ch in code:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
        elif ch == '"' or (ch == "'" and not (prev.isalnum() or prev in ")]}._'")):
            quote = ch
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            cur.append(ch)
        elif ch in seps and depth <= 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        if not ch.isspace():
            prev = ch
    out.append("".join(cur).strip())
    return [s for s in out if s]


def _command_like(text: str) -> bool:
    """A string a driver sends, not one it prints (an error's words, a format)."""
    t = text.strip()
    if not re.match(r"^[*:]?[A-Za-z][A-Za-z0-9]*([:? ;]|$)", t) or len(t.split()) > 4:
        return False
    return not re.search(r"(?i)\b(driver|support|error|invalid|specified|channel |not |please|failed)\b|: ", t)


def model_hints(model: str) -> List[str]:
    """A rack's model name as cards and identity replies say it: K2400 is a Keithley 2400, AMI430 a Model 430,
    HP34401A a 34401A, SP2150 an SP-2150."""
    out = [model]
    m = re.fullmatch(r"([A-Za-z]+)[-_ ]?(\d[\dA-Za-z-]*?)(?:_\d+)?", model.strip())
    if m:
        maker, number = m.group(1), m.group(2)
        names = {"k": "Keithley", "hp": "HP", "ami": "American Magnetics", "sr": "SRS", "ls": "Lake Shore",
                 "yoko": "Yokogawa", "e": "Agilent", "b": "Keysight", "ag": "Agilent", "n": "Agilent",
                 "tds": "Tektronix", "awg": "Tektronix", "dg": "Rigol", "pem": "Hinds"}
        out += [number, "%s %s" % (names.get(maker.lower(), maker), number), "%s-%s" % (maker, number)]
        if maker.lower() in ("sr", "sp", "b", "e", "n", "dg", "tds", "awg", "ls"):
            out.append(maker + number)
    return list(dict.fromkeys(h for h in out if h))


def _samples(commands: List[str]) -> List[Command]:
    """A channel's commands as the script survey's, to match against a card: each as a query and as a set."""
    out = []
    for c in commands:
        c = re.sub(r"%[-+ 0#]*\d*(?:\.\d+)?[diouxXeEfFgGcs]", "1", c).strip().rstrip(";")
        head, _, rest = c.partition(" ")
        if c.endswith("?") or "?" in head:
            out.append(Command(c, True, ""))
            continue
        out.append(Command(("%s? %s" % (head, rest)).strip(), True, ""))
        out.append(Command(("%s %s" % (c, "1") if not rest or not re.search(r"\d", rest) else c).strip(), False, ""))
    return out


def _words(text: str) -> List[str]:
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]


def set_param_of(card: Any, param: Optional[str]) -> Optional[str]:
    """The parameter that sets one sm reads and sets by one channel, where the card has them apart: the set point
    whose read-back it is (a cryostat's T_set for T); None when the parameter is set itself."""
    spec = card.params.get(param) if card is not None and param else None
    if spec is None or spec.set_cmd is not None:
        return None
    drive = next((p for p in card.params.values() if p.readback == param and p.set_cmd is not None), None)
    return drive.name if drive is not None else None


def param_of(card: Any, channel: str, commands: List[str], send: str = "") -> Tuple[Optional[str], str]:
    """The card parameter one of the instrument's channels is: the DAQ channel its driver writes (send), else by the
    command its driver sends for it, else by its name (THETA is a lock-in's phase, FREQ its frequency). (None, why)
    when none says."""
    if card is None:
        return None, ""
    if send:
        if send in card.params:
            return send, "its driver writes %s" % send
        return None, "its driver writes %s, which the card hasn't: add it to your version of the card" % send
    for c in _samples(commands):
        spec, _, part = scripts._param_for(card, c.text)
        if spec is not None and not part:
            return spec.name, "its driver sends %s" % c.text
    key = re.sub(r"[^a-z0-9]", "", channel.lower())
    for spec in card.params.values():   # the same name first: THETA is the lock-in's theta, not its phase
        if re.sub(r"[^a-z0-9]", "", spec.name.lower()) == key:
            return spec.name, "its name"
    wanted = set(SYNONYMS.get(key, ())) | set(SYNONYMS.get(re.sub(r"\d+$", "", key), ()))
    for word in [w for w in SYNONYMS.get(key, ()) or SYNONYMS.get(re.sub(r"\d+$", "", key), ())]:
        for spec in card.params.values():
            names = {re.sub(r"[^a-z0-9_]", "", spec.name.lower()), re.sub(r"[^a-z0-9]", "", spec.name.lower())}
            if word in names:
                return spec.name, "its name"
    for spec in card.params.values():
        if set(_words(getattr(spec, "label", "") or "")) & wanted:
            return spec.name, "its name, as the card's label says it"
    return None, ""


# ------------------------------------------------------------------ the folder

def _walk(root: Path, own: Iterable[Any]) -> Tuple[Dict[str, List[Path]], List[str]]:
    """The files a survey reads in a folder: .m scripts, sminst files, .mat scans and run files."""
    from .folderfiles import mine, secret
    found: Dict[str, List[Path]] = {"m": [], "sminst": [], "mat": [], "runs": []}
    skipped: List[str] = []
    looked, t0 = 0, time.monotonic()
    if root.is_file():
        found["m" if root.suffix.lower() == ".m" else "mat"].append(root)
        return found, skipped
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [d for d in sorted(dirnames) if d not in SKIP_DIRS and not d.startswith(".")
                       and mine((here / d).resolve(), own) is None]
        for name in sorted(filenames):
            looked += 1
            if looked > MAX_LOOKED or time.monotonic() - t0 > WALK_SECONDS:
                skipped.append("the rest of %s (it holds more than Kelvin looks through at once)" % root)
                return found, skipped
            low = name.lower()
            if low.endswith(".mat") and RUN_FILE.match(name):   # a run: counted now, read when imported
                found["runs"].append(here / name)
                continue
            kind = "m" if low.endswith(".m") else "sminst" if low.startswith("sminst_") and low.endswith(".mat") \
                else "mat" if low.endswith(".mat") else ""
            if kind and not secret(here / name):
                found[kind].append(here / name)
    return found, skipped


def survey(path: Any, registry: Any = None, own: Iterable[Any] = (), read_mat: bool = True,
           scans: bool = True) -> Found:
    """Read a lab's Special Measure in a folder (or one setup script): the racks its setup scripts build, the drivers,
    the scans and the runs. registry: Kelvin's cards, for the card that fits each instrument (scripts.match) and the
    parameter each channel is. read_mat: also read its .mat files (sminst files; with scans, the saved scans and the
    newest runs' too)."""
    from .folderfiles import mine
    root = Path(path).expanduser()
    if not root.exists():
        raise FileNotFoundError("%s does not exist" % root)
    if mine(root.resolve(), own) is not None:
        raise ValueError("%s is in Kelvin's own folder, which Kelvin's AI never reads" % root)
    files, skipped = _walk(root, own)
    base = root if root.is_dir() else root.parent
    out = Found(folder=str(root), skipped=skipped)
    texts: Dict[Path, str] = {}
    for p in files["m"]:
        try:
            if p.stat().st_size > MAX_SCRIPT_BYTES:
                continue
            texts[p] = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            out.skipped.append("%s (%s)" % (p.name, e))
    for p, text in texts.items():   # control functions: function val = smcSR830(ic, val, rate), after comments too
        m = re.search(r"(?m)^\s*function\s+[^=\n]*=\s*(smc\w+)\s*\(", _code(text))
        if p.stem.lower().startswith("smc") and m:
            out.drivers[m.group(1).lower()] = str(p)
    for p, text in texts.items():
        if "smloadinst" not in text or "smaddchannel" not in text or p.stem.lower().startswith("smc"):
            continue
        rel = str(p.relative_to(base)) if p != root else p.name
        reader = _Setup(rel, text, scripts._Collector())
        problem = ""
        try:
            reader.run()
        except Exception as e:   # an odd script: what was read so far still counts
            problem = "%s: %s" % (type(e).__name__, e)
        try:
            modified = p.stat().st_mtime
        except OSError:
            modified = 0.0
        out.setups.append(Setup(rel, modified, reader.rack, problem))
    library = {p.stem[len("sminst_"):].lower(): p for p in files["sminst"]}
    matched: Dict[Any, Any] = {}   # a model's card, matched once for every rack that loads it
    for s in out.setups:
        for ri in s.instruments:
            _driver(ri, out.drivers, library if read_mat else {}, texts)
            ri.inst.commands = [c for c in ri.inst.commands if _command_like(c.text) and "'" not in c.text
                                and "{" not in c.text]   # start-up commands Kelvin can read
            if registry is not None and ri.model.lower() not in DUMMIES and not ri.function:
                started = ri.inst.commands
                ri.inst.commands = started + ri.sends   # its driver's commands help name its card
                key = (tuple(sorted(set(ri.inst.hints))), tuple(c.text for c in ri.inst.commands), ri.daq)
                if key not in matched:
                    matched[key] = _daq_card(ri, registry) or scripts.match(
                        ri.inst, registry, named_in="the rack (its sminst file)") or _known(ri.model, registry)
                ri.inst.card = dict(matched[key]) if matched[key] else None
                ri.inst.commands = started
                if ri.inst.card:
                    ri.inst.card["mapped"] = scripts.mapping(registry.get(ri.inst.card["id"]), started)
                    ri.inst.card["limits"] = {}
                _params(ri, registry, texts)
    for p in files["runs"]:
        rel = str(p.parent.relative_to(base)) if root.is_dir() else "."
        out.runs[rel] = out.runs.get(rel, 0) + 1
    if read_mat and scans:
        _scans(out, files, base)
    return out


def _known(model: str, registry: Any) -> Optional[Dict[str, Any]]:
    """The card of a Special Measure model whose name a card doesn't say (KNOWN_CARDS), for a person to check."""
    card_id = KNOWN_CARDS.get(re.sub(r"_\d+$", "", re.sub(r"[^a-z0-9_]", "", model.lower())))
    if not card_id or card_id not in registry:
        return None
    card = registry.get(card_id)
    return {"id": card.id, "model": card.model, "kind": card.kind, "sure": False,
            "why": "Special Measure's %s is a %s" % (model, card.model.split(" (")[0]), "mapped": [], "limits": {}}


def _driver(ri: RackInstrument, drivers: Dict[str, str], library: Dict[str, Path], texts: Dict[Path, str]) -> None:
    """The control function of an instrument (its sminst file names it; else smc<model>), and the commands its
    driver sends, which help name its card. The sminst file also gives the channels' order (by number) and its data's
    numbers. What each channel's case sends of its value (sent_value): its conversion, and for a DAQ session (one
    the scripts its setup block runs make, Initial_NanoPositioner2) the NI DAQ channel it writes."""
    fn = ""
    info = _sminst(library.get(ri.model.lower()))
    order = [c.strip() for c in info.get("channels") or []]
    ri.order = order
    ri.data = dict(info.get("data") or {}, **ri.data)   # what the setup script sets over its sminst file's
    if info.get("cntrlfn"):
        fn = str(info["cntrlfn"]).lower()
    if not fn or fn not in drivers:
        fn = "smc" + re.sub(r"[^a-z0-9_]", "", ri.model.lower())
    ri.driver = drivers.get(fn, "")
    if not ri.driver:
        return
    text = texts.get(Path(ri.driver)) or ""
    by_number = driver_commands(text)
    ri.inst.hints += scripts._hints(Path(ri.driver).stem[3:])
    ri.numbered = {name: by_number.get(i + 1, []) for i, name in enumerate(order)}
    _conversions(ri, text, texts)
    sent = [c for cs in by_number.values() for c in cs]
    for c in list(dict.fromkeys(sent))[:12]:   # what it can send: matched against the cards, never sent
        for s in _samples([c])[:1]:
            ri.sends.append(Command(s.text, s.query, ri.driver))


def _conversions(ri: RackInstrument, text: str, texts: Dict[Path, str]) -> None:
    """Each channel's conversion, from what its driver's case sends; a channel whose case writes a DAQ session gets
    the session's channel (send), and the instrument the NI DAQ device they are on (daq), with its address."""
    cases, value_name = driver_cases(text), driver_value(text)
    helpers = {h.lower() for h in ri.helpers}
    sessions: Dict[str, List[Tuple[str, str, str]]] = {}
    ni = False
    for t in [text] + [t for p, t in texts.items() if p.stem.lower() in helpers]:
        found, theirs = daq_sessions(t)
        sessions.update(found)
        ni = ni or theirs
    devices = set()
    for i, name in enumerate(ri.order):
        ops = cases.get(i + 1) or {}
        sent = sent_value(ops.get(1) or ops.get(None) or [], ri.data, value_name)
        if sent is None:
            continue
        outs = [(d, c) for d, c, way in sessions.get(sent["to"][1], []) if way == "out"] if sent["to"][0] == "daq" \
            else []
        for ch in ri.channels:
            if ch.channel != name:
                continue
            ch.scale, ch.offset = sent["scale"], sent["offset"]
            change = "%s × the value%s" % (_num(sent["scale"]), " %s %s" % ("-" if sent["offset"] < 0 else "+",
                                                                           _num(abs(sent["offset"]))) if sent["offset"]
                                           else "")
            if len(outs) == 1 and ni:
                ch.send = outs[0][1]
                devices.add(outs[0][0])
                ch.how = "its driver writes %s/%s: %s" % (outs[0][0], outs[0][1], change)
            elif sent["scale"] != 1 or sent["offset"]:
                ch.how = "its driver sends %s%s" % (sent["to"][1] + " " if sent["to"][0] == "command" and
                                                    sent["to"][1] else "", change)
    if len(devices) == 1:
        ri.daq = devices.pop()
        if not ri.inst.address:
            ri.inst.address, ri.inst.expression = "nidaq://%s" % ri.daq, ""


def _daq_card(ri: RackInstrument, registry: Any) -> Optional[Dict[str, Any]]:
    """Kelvin's NI DAQ card, for an instrument its driver drives as an NI DAQ device's channels."""
    if not ri.daq or registry is None or "ni/daqmx" not in registry:
        return None
    card = registry.get("ni/daqmx")
    chans = ", ".join(sorted({ch.send for ch in ri.channels if ch.send}))
    return {"id": card.id, "model": card.model, "kind": card.kind, "sure": True, "mapped": [], "limits": {},
            "why": "its driver writes %s of the NI DAQ %s through MATLAB's DAQ toolbox" % (chans, ri.daq)}


def _sminst(path: Optional[Path]) -> Dict[str, Any]:
    """What an sminst file says of its instrument: its control function's name and its channels in order."""
    if path is None:
        return {}
    try:
        from . import matfile
        data = matfile.load(path, names=["inst"])
    except Exception:   # no reader, or a file it can't read: the driver is found by its name
        return {}
    inst = data.get("inst")
    if isinstance(inst, list) and inst:
        inst = inst[0]
    if not isinstance(inst, dict):
        return {}
    fn = inst.get("cntrlfn")
    text = getattr(fn, "text", None) or (fn if isinstance(fn, str) else "")
    names = inst.get("channels")
    if isinstance(names, str):
        names = [names]
    elif not isinstance(names, list) and hasattr(names, "ravel"):   # a cell of names, as an array
        names = [str(n) for n in names.ravel()]
    data = inst.get("data")
    numbers: Dict[str, float] = {}
    for k, v in (data.items() if isinstance(data, dict) else []):   # one number each, as the reader gives it ([[0.25]])
        got = [] if isinstance(v, (bool, str)) else _numbers(v)
        if len(got) == 1 and math.isfinite(got[0]):
            numbers[str(k)] = got[0]
    return {"cntrlfn": re.sub(r"^@", "", str(text or "")).strip(),
            "channels": [str(n) for n in names] if isinstance(names, list) else [], "data": numbers}


def card_units(ch: Channel, card: Any) -> None:
    """Whether the card the channel is on takes it in the channel's own unit, so sm's multiplier stays out (native): a
    power of ten (a unit, not a gain), with the rack's limits inside the card's own while the same limits times it
    fall a hundred times past them. Special Measure sends MultiVu a field in Oe, 10000 x the tesla the channel shows;
    Kelvin's card takes tesla."""
    if ch.native:
        ch.native, ch.how = False, ""
    total = ch.factor * ch.scale
    spec = card.params.get(ch.set_param or ch.param) if card is not None and ch.param else None
    if spec is None or not spec.limits or total in (0, 1) or ch.offset or \
            not (math.isfinite(ch.low) and math.isfinite(ch.high)):
        return
    power = math.log10(abs(total))
    lo, hi = float(spec.limits[0]), float(spec.limits[1])
    most = max(abs(lo), abs(hi))
    if abs(power - round(power)) > 1e-9 or not most:
        return
    inside = lo - 1e-9 * most <= min(ch.low, ch.high) and max(ch.low, ch.high) <= hi + 1e-9 * most
    if inside and max(abs(ch.low), abs(ch.high)) * abs(total) > 100 * most:
        ch.native = True
        ch.how = "Kelvin's card takes %s in %s, as the channel shows it: Special Measure's × %s is its driver's" % (
            spec.name, spec.unit or "its own unit", _num(total))


def _params(ri: RackInstrument, registry: Any, texts: Dict[Path, str]) -> None:
    """Each channel's card parameter, once a card fits the instrument."""
    card_id = (ri.inst.card or {}).get("id")
    card = registry.get(card_id) if card_id else None
    taken: Dict[str, str] = {}
    for ch in ri.channels:
        ch.param, ch.why = param_of(card, ch.channel, ri.numbered.get(ch.channel, []), ch.send)
        ch.set_param = set_param_of(card, ch.param)
        card_units(ch, card)
        if ch.param and ch.param in taken:   # V2 beside V1: the instrument's second channel, another entry
            ch.why = "%s is %s's already: a second channel is an entry of its own" % (ch.param, taken[ch.param])
            ch.param = None
        elif ch.param:
            taken[ch.param] = ch.name


# ------------------------------------------------------------------ scans

def _scans(out: Found, files: Dict[str, List[Path]], base: Path) -> None:
    """The scans saved from sm's scan editor (a .mat with smscan, or a list of them: scans), and those of the newest
    runs (each run file keeps the scan it ran), one per layout (the newest)."""
    try:
        from .importers import specialmeasure as smfiles
    except ImportError:
        out.unread.append("the .mat files (Kelvin's reader for them is missing)")
        return
    seen: Dict[str, Dict[str, Any]] = {}
    saved: Dict[str, Dict[str, Any]] = {}   # one per name and layout: the newest file
    for p in sorted(files["mat"], key=lambda p: _mtime(p), reverse=True)[:MAX_SCAN_FILES]:
        try:
            scans = smfiles.scans_in(p)
        except Exception as e:
            out.unread.append("%s (%s)" % (p.name, str(e).splitlines()[0][:80] if str(e) else type(e).__name__))
            continue
        for k, scan in enumerate(scans):
            name = str(scan.get("name") or "").strip() or (p.stem if len(scans) == 1 else "%s %d" % (p.stem, k + 1))
            sig = layout(scan)   # the same scan saved again (as sm's GUI does before each run): the newest only
            if sig in saved or len(out.scans) >= MAX_SCANS:
                continue
            saved[sig] = {"name": name, "path": str(p.relative_to(base)) if base in p.parents or p == base
                          else str(p), "source": "saved", "scan": scan, "runs": 0, "index": k}
            out.scans.append(saved[sig])
    newest = sorted(files["runs"], key=lambda p: _mtime(p), reverse=True)[:RECENT_RUNS]
    for p in newest:
        try:
            scan = smfiles.scan_of(p)
        except Exception as e:
            out.unread.append("%s (%s)" % (p.name, str(e).splitlines()[0][:80] if str(e) else type(e).__name__))
            continue
        if not scan:
            continue
        sig = layout(scan)
        if sig in seen:
            seen[sig]["runs"] += 1
            continue
        m = RUN_FILE.match(p.name)   # a run's file names it surer than its scan's title, often another run's
        name = (m.group(2) if m else "") or str(scan.get("name") or "").strip() or p.stem
        entry = {"name": name, "path": str(p.relative_to(base)) if base in p.parents else str(p), "source": "run",
                 "scan": scan, "runs": 1, "index": 0}
        seen[sig] = entry
        out.scans.append(entry)


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def layout(scan: Dict[str, Any]) -> str:
    """A scan's shape, to tell the same scan run again from another: what each loop sets (and its range) and reads."""
    parts = []
    for lp in _loops(scan):
        parts.append("%s|%s|%s|%s" % (",".join(_names(lp.get("setchan"))), _ranges(lp), lp.get("npoints"),
                                      ",".join(_names(lp.get("getchan")))))
    return ";".join(parts)


def _loops(scan: Dict[str, Any]) -> List[Dict[str, Any]]:
    loops = scan.get("loops")
    if isinstance(loops, dict):
        return [loops]
    return [lp for lp in loops if isinstance(lp, dict)] if isinstance(loops, list) else []


def _names(v: Any) -> List[str]:
    """Channel names as a scan keeps them: a cell of names, a char matrix, one name; 'none' is no channel."""
    if v is None:
        return []
    if isinstance(v, str):
        items = [v]
    elif isinstance(v, (list, tuple)):
        items = [x for x in _flat(v)]
    else:
        try:
            items = [str(x) for x in list(v)]
        except TypeError:
            items = [str(v)]
    return [str(x).strip() for x in items if isinstance(x, str) and str(x).strip() and str(x).strip() != "none"]


def _flat(v: Any) -> List[Any]:
    out: List[Any] = []
    for x in v:
        if isinstance(x, (list, tuple)):
            out += _flat(x)
        else:
            out.append(x)
    return out


def _numbers(v: Any) -> List[float]:
    """Numbers as a scan keeps them (a scalar, a row, a numpy array)."""
    if v is None:
        return []
    if isinstance(v, bool):
        return [float(v)]
    if isinstance(v, (int, float)):
        return [float(v)]
    try:
        return [float(x) for x in _flat(list(getattr(v, "ravel", lambda: v)()))]
    except (TypeError, ValueError):
        return []


def _ranges(lp: Dict[str, Any]) -> str:
    rr = lp.get("setchanranges")
    if isinstance(rr, list) and rr:
        return ",".join("%s:%s" % tuple((_numbers(r) + [0.0, 0.0])[:2]) for r in rr)
    return ",".join(_num(x) for x in _numbers(lp.get("rng"))[:2])


def _one(v: Any) -> Optional[float]:
    nums = _numbers(v)
    return nums[0] if nums else None


def _fn_text(v: Any) -> str:
    """A function handle's text as a scan keeps it (the reader's object, or a struct of its fields, or text)."""
    if v is None:
        return ""
    text = getattr(v, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        for k in ("fn", "function_handle", "function", "text"):
            if k in v:
                return _fn_text(v[k])
    if isinstance(v, list) and v:
        return _fn_text(v[0])
    return ""


# ------------------------------------------------------------------ a scan → a Kelvin plan

_SETS = re.compile(r"^@\s*\([^)]*\)\s*smset\s*\((.*)\)\s*;?\s*$", re.S)


def zeroes_of(text: str) -> Optional[Dict[str, float]]:
    """An escape function that sets channels to numbers (@(x) smset({'Vtg','Vbg'}, [0, 0])): channel: value; None
    for anything else."""
    text = re.sub(r"^sf%\d+", "", str(text or "").strip())
    m = _SETS.match(text)
    if not m:
        return None
    args = _matlab_args(m.group(1))
    if len(args) < 2:
        return None
    chans = re.findall(r"'([^']*)'", args[0])
    if not chans:
        return None
    values = _plain_numbers(args[1])
    if values is None:   # anything but numbers as written (-[1 2], [-(1) 2], a variable): not translated
        return None
    if len(values) == 1 and len(chans) > 1:
        values = values * len(chans)
    if len(values) != len(chans):
        return None
    return {c.strip(): v for c, v in zip(chans, values) if c.strip()}


_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _plain_numbers(text: str) -> Optional[List[float]]:
    """A number or a vector of numbers as written ([0, 0], [-1 2.5], 0); None for anything else."""
    t = text.strip()
    if t.startswith("[") and t.endswith("]"):
        t = t[1:-1]
    parts = [p for p in re.split(r"[,\s]+", t.strip()) if p]
    if not parts or not all(_NUMBER.fullmatch(p) for p in parts):
        return None
    return [float(p) for p in parts]


def _chained_power(body: str) -> bool:
    """a^b^c: MATLAB takes it as (a^b)^c, Python's ** as a^(b^c), so it isn't translated."""
    i = 0
    while True:
        i = body.find("^", i)
        if i < 0:
            return False
        j = i + 1
        while j < len(body) and body[j] in " +-":
            j += 1
        if j < len(body) and body[j] == "(":   # the power is a group: past its end
            depth = 0
            while j < len(body):
                depth += {"(": 1, ")": -1}.get(body[j], 0)
                j += 1
                if depth == 0:
                    break
        else:
            m = re.match(r"[\w.]+", body[j:])
            j += m.end() if m else 0
            if j < len(body) and body[j] == "(":   # a call: past its arguments
                depth = 0
                while j < len(body):
                    depth += {"(": 1, ")": -1}.get(body[j], 0)
                    j += 1
                    if depth == 0:
                        break
        rest = body[j:].lstrip()
        if rest.startswith("^") or rest.startswith(".^"):
            return True
        i += 1


_MATLAB_FUNCS = {"sqrt", "abs", "exp", "log", "log10", "sin", "cos", "tan", "asin", "acos", "atan", "atan2", "sinh",
                 "cosh", "tanh", "floor", "ceil", "round", "sign", "hypot", "mapto"}


def expression(text: str, args: List[str]) -> Optional[str]:
    """A formula (@(n,E) 1.18*n - 8.4*E + 2.4) in Kelvin's expression language (expr.py), its arguments renamed
    as args says (the Function's channels, in order); mapto(x, a, b, c, d), Special Measure's there-and-back map
    (a → b over 0 … 0.5, then c → d), as where(). None when it uses anything else."""
    text = re.sub(r"^sf%\d+", "", str(text or "").strip())
    m = re.match(r"^@\s*\(([^)]*)\)\s*(.+)$", text, re.S)
    if not m:
        return None
    params = [p.strip() for p in m.group(1).split(",") if p.strip()]
    body = m.group(2).strip().rstrip(";")
    if len(params) > len(args):
        return None
    rename = dict(zip(params, args))
    if _chained_power(body):
        return None
    body = body.replace(".*", "*").replace("./", "/").replace(".^", "**").replace("^", "**")
    body = re.sub(r"\basin\(", "arcsin(", body)
    body = re.sub(r"\bacos\(", "arccos(", body)
    body = re.sub(r"\batan2?\(", lambda mm: "arctan2(" if mm.group(0).startswith("atan2") else "arctan(", body)
    for name in re.findall(r"[A-Za-z_]\w*", body):
        if name in rename or name in ("pi",) or name in _MATLAB_FUNCS or name in ("arcsin", "arccos", "arctan",
                                                                                  "arctan2", "e"):
            continue
        return None   # a variable from the setup script's workspace, or a function Kelvin doesn't have
    if re.search(r"[\[\]{}'\"~&|]", body):
        return None

    def mapto(mm: "re.Match[str]") -> str:
        parts = _matlab_args(mm.group(1))
        if len(parts) != 5:
            raise ValueError
        x, a, b, c, d = ("(%s)" % p for p in parts)
        return "where(%s < 0.5, %s + (%s - %s) * 2 * %s, %s + (%s - %s) * (2 * %s - 1))" % (x, a, b, a, x, c, d, c, x)
    try:
        while "mapto(" in body:
            body = re.sub(r"mapto\(((?:[^()]|\([^()]*\))*)\)", mapto, body, count=1)
    except ValueError:
        return None
    return re.sub(r"[A-Za-z_]\w*", lambda mm: rename.get(mm.group(0), mm.group(0)), body)


def plan_name(text: str) -> str:
    name = re.sub(r"[^\w.-]+", "_", str(text or "").strip()).strip("_")[:48]
    return name or "scan"


def translate(scan: Dict[str, Any], channels: Dict[str, Tuple[Any, ...]], name: str = "",
              functions: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """A Special Measure scan as a Kelvin plan, by fixed rules: {plan (a mapping, None when it can't be translated),
    text (its YAML), title, summary, gaps (what didn't translate, in words)}.

    channels: each sm channel name → (its Kelvin key, its multiplier, the key that sets it where the card has one
    apart (None), its offset): the instrument gets the scan's value × the multiplier + the offset (sm's own
    multiplier, and its driver's conversion).
    functions: each function channel's name → {variables: its Function's channels in order, sets: {sm channel:
    formula}}; a scan that sweeps one becomes a plan with variables and sets:.

    The loops (sm's inner first) become sweep: outer first; a loop's first channel steps, the others move with it
    (with:); its wait settles each point and its start wait the first (first_settle: start wait + wait); what the
    inner loop records is measure:, what an outer loop records is its own measure:; constants it sets are setup:; an
    escape function that sets channels to numbers is after: and on_abort: (sm runs it at the end and on Escape). A
    loop of sm's counter (count) is repeat: outermost, a time series innermost."""
    from .yamlio import dump_yaml
    functions = functions or {}
    gaps: List[str] = []
    title = str(name or scan.get("name") or "scan").strip() or "scan"   # a run's file names it surest
    loops = _loops(scan)
    if not loops:
        return {"plan": None, "text": "", "title": title, "summary": "", "gaps": ["it has no loops"]}

    def key(ch: str, setting: bool = False) -> Tuple[Optional[str], float, float]:
        """A channel's Kelvin key, and how a value of the scan's becomes the instrument's: x its multiplier, + its
        offset; setting: as a loop, a constant or the end sets it (its set point, where the card has one apart)."""
        hit = channels.get(ch)
        if not hit:
            return None, 1.0, 0.0
        return (hit[2] if setting and len(hit) > 2 and hit[2] else hit[0]), hit[1], \
            float(hit[3]) if len(hit) > 3 and hit[3] else 0.0

    missing: List[str] = []
    sweep: Dict[str, Any] = {}
    sets: Dict[str, str] = {}
    measure: List[str] = []
    scaled: Dict[str, Tuple[str, float, float]] = {}   # readings sm records converted: key → (channel, ×, +)
    repeat = None
    time_axis = None
    for depth, lp in enumerate(reversed(loops)):   # outer first
        inner = depth == len(loops) - 1
        setchans = _names(lp.get("setchan"))
        getchans = [c for c in _names(lp.get("getchan")) if c != "time"]   # sm's clock: every Kelvin point has t
        scaled.update({key(c)[0]: (c, key(c)[1], key(c)[2]) for c in getchans
                       if key(c)[0] and (key(c)[1] != 1 or key(c)[2])})
        npoints = _one(lp.get("npoints"))
        if npoints is not None and not (math.isfinite(npoints) and npoints >= 1 and npoints == int(npoints)):
            return {"plan": None, "text": "", "title": title, "summary": "", "gaps": [
                "loop %d's npoints (%s) isn't a number of points" % (len(loops) - depth, _num(npoints))]}
        rng = _numbers(lp.get("rng"))
        ranges = lp.get("setchanranges")
        ranges = [(_numbers(r) + [None, None])[:2] for r in ranges] if isinstance(ranges, list) else []
        wait = _one(lp.get("waittime")) or 0.0
        start = _one(lp.get("startwait")) or 0.0
        step_time = _numbers(lp.get("ramptime"))
        for fn_key in ("trafofn", "prefn", "postfn", "datafn", "procfn"):
            v = lp.get(fn_key)
            if v not in (None, [], {}, "") and _numbers(v) == [] and (_fn_text(v) or (isinstance(v, (list, dict))
                                                                                         and v)):
                if fn_key == "trafofn":
                    return {"plan": None, "text": "", "title": title, "summary": "", "gaps": [
                        "loop %d works its values out with trafofn (%s), which Kelvin doesn't translate: write it as a "
                        "scan with sets:" % (len(loops) - depth, _fn_text(v) or "a function")]}
                gaps.append("loop %d's %s (%s) isn't translated" % (len(loops) - depth, fn_key,
                                                                    _fn_text(v) or "a function"))
        if step_time and any(t < 0 for t in step_time):
            return {"plan": None, "text": "", "title": title, "summary": "", "gaps": [
                "loop %d ramps its channels on the instrument (a negative step time) and reads them as it goes: "
                "Kelvin translates stepped loops" % (len(loops) - depth)]}
        n = int(npoints) if npoints else (len(rng) if rng else 0)
        if n < 1:
            gaps.append("loop %d has no points" % (len(loops) - depth))
            return {"plan": None, "text": "", "title": title, "summary": "", "gaps": gaps}
        if not setchans or setchans == ["count"]:   # sm's counter: the same point again
            if not inner and depth == 0:
                repeat = n
                if getchans:
                    gaps.append("loop %d reads %s once per repeat; Kelvin reads %s at every point" % (
                        len(loops) - depth, ", ".join(getchans), "it" if len(getchans) == 1 else "them"))
            elif inner:
                time_axis = {"every": _round(max(wait, 0.01)), "points": n}
            else:
                gaps.append("loop %d repeats points between other loops, which Kelvin's scans do outermost "
                            "(repeat:) or innermost (a time series)" % (len(loops) - depth))
                return {"plan": None, "text": "", "title": title, "summary": "", "gaps": gaps}
            (measure if inner else measure).extend(k for k in (key(c)[0] for c in getchans) if k)
            missing += [c for c in getchans if key(c)[0] is None]
            continue
        spans: List[Tuple[float, float]] = []
        for k, ch in enumerate(setchans):
            r = ranges[k] if k < len(ranges) else None
            if r and r[0] is not None and r[1] is not None:
                spans.append((r[0], r[1]))
            elif rng:
                spans.append((rng[0], rng[-1]))
            else:
                spans.append((1.0, float(n)))
        own: Dict[str, Any] = {}
        first = setchans[0]
        fn = functions.get(first)
        loop_key: Optional[str]
        if fn is not None:   # a function channel: a variable of the plan, and what its Function sets
            loop_key = first
        else:
            loop_key = key(first, True)[0]
            if loop_key is None:
                missing.append(first)
                continue
        if loop_key in sweep:
            return {"plan": None, "text": "", "title": title, "summary": "", "gaps": [
                "two loops step %s: a Kelvin scan steps a setting in one loop" % first]}
        a, b = spans[0]
        _, factor0, offset0 = (None, 1.0, 0.0) if fn is not None else key(first, True)
        if not npoints and rng and len(rng) == n and n > 2 and not ranges:
            own["values"] = [_round(v * factor0 + offset0) for v in rng]   # rng as a vector: its values
        else:
            own.update({"from": _round(a * factor0 + offset0), "to": _round(b * factor0 + offset0), "points": n})
        moved: Dict[str, Any] = {}
        for k, ch in enumerate(setchans[1:], 1):
            if ch in functions:
                gaps.append("%s, a function channel, moves with %s: Kelvin moves settings with a loop, not "
                            "variables" % (ch, first))
                return {"plan": None, "text": "", "title": title, "summary": "", "gaps": gaps}
            kk, f, o = key(ch, True)
            if kk is None:
                missing.append(ch)
                continue
            moved[kk] = {"from": _round(spans[k][0] * f + o), "to": _round(spans[k][1] * f + o)}
        if moved:
            own["with"] = moved
        if step_time and any(t > 0 for t in step_time):
            gaps.append("loop %d's step time (%s s) paces its steps: Kelvin ramps at the lab's rates, and settles "
                        "as long more" % (len(loops) - depth, _num(max(step_time))))
            wait += max(step_time)
        if wait:
            own["settle"] = _round(wait)
        if start:
            own["first_settle"] = _round(start + wait)
        reads = [k for k in (key(c)[0] for c in getchans) if k]
        missing += [c for c in getchans if key(c)[0] is None]
        if inner:
            measure.extend(reads)
        elif reads:
            own["measure"] = reads
        if fn is not None:
            for dep, formula in fn["sets"].items():
                kk, f, o = key(dep, True)
                if kk is None:
                    missing.append(dep)
                    continue
                text = expression(formula, fn["variables"])
                if text is None:
                    gaps.append("%s's formula for %s (%s) isn't one Kelvin can work out" % (first, dep, formula))
                    return {"plan": None, "text": "", "title": title, "summary": "", "gaps": gaps}
                sets[kk] = _scaled(text, f, o)
        sweep[loop_key] = own
    if missing:
        return {"plan": None, "text": "", "title": title, "summary": "", "gaps": [
            "%s %s not channels of the rack read" % (", ".join(dict.fromkeys(missing)),
                                                    "is" if len(set(missing)) == 1 else "are")] + gaps}
    variables = [k for k in sweep if "." not in k]
    setting = set(sweep) | {k for s in sweep.values() for k in s.get("with") or {}} | set(sets)
    measure = [k for k in measure if k not in setting]   # what the sweep sets: each point records its value
    for s in sweep.values():
        if "measure" in s:
            s["measure"] = [k for k in s["measure"] if k not in setting]
            if not s["measure"]:
                del s["measure"]
    recorded = set(measure) | {k for s in sweep.values() for k in s.get("measure") or []}
    for k, (ch, f, o) in scaled.items():
        if k in recorded:
            gaps.append("%s is recorded as the instrument reads it; Special Measure records %s divided by %s" % (
                ch, "(it %s %s)" % ("+" if o < 0 else "-", _num(abs(o))) if o else "it", _num(f)))
    for v in variables:   # a Function's other variables: as the scan's constants set them
        fn = functions[v]
        for other in fn["variables"]:
            if other not in sweep:
                const = _const(scan, other)
                if const is None:
                    gaps.append("%s's other variable %s keeps the value it had before the scan, which Kelvin can't "
                                "know: give it as a constant" % (v, other))
                    return {"plan": None, "text": "", "title": title, "summary": "", "gaps": gaps}
                sets = {k: re.sub(r"(?<![\w.])%s(?![\w.(])" % re.escape(other), "(%s)" % _num(const), t)
                        for k, t in sets.items()}
    # its comments stay in the file: a file's words never reach the model but as names (imports.py's rule), and a
    # kept scan's text is what the model reads of it
    plan: Dict[str, Any] = {"plan": plan_name(title), "description": "From Special Measure"}
    setup: Dict[str, Any] = {}
    swept_fns = {functions[v].get("function") for v in variables}
    fixed: Dict[Any, Dict[str, Any]] = {}   # a Function no loop sweeps, whose channels the constants set
    for c in _consts(scan):
        if not c["set"]:
            continue
        if c["setchan"] in functions:
            fn = functions[c["setchan"]]
            if fn.get("function") not in swept_fns:
                fixed.setdefault(fn.get("function"), fn)
            continue
        kk, f, o = key(c["setchan"], True)
        if kk is None:
            gaps.append("the constant %s isn't a channel of the rack read" % c["setchan"])
        else:
            setup[kk] = _round(c["val"] * f + o)
    for fn in fixed.values():   # as sm sets them: its formulas at the constants' values, before the loops
        known = {v: _const(scan, v) for v in fn["variables"] if _const(scan, v) is not None}
        for dep, formula in fn["sets"].items():
            kk, f, o = key(dep, True)
            if kk is None:
                missing.append(dep)
                continue
            text = expression(formula, fn["variables"])
            if text is None:
                return {"plan": None, "text": "", "title": title, "summary": "", "gaps": [
                    "the constants set %s's channels, whose formula for %s (%s) isn't one Kelvin can work out" % (
                        fn.get("function") or "a Function", dep, formula)]}
            unknown = [n for n in expr.names_in(text) if n not in known]
            if unknown:
                return {"plan": None, "text": "", "title": title, "summary": "", "gaps": [
                    "the constants set %s's channels, and its formula for %s also uses %s, which keeps the value it "
                    "had before the scan: Kelvin can't know it, give it as a constant" % (
                        fn.get("function") or "a Function", dep, ", ".join(unknown))]}
            try:
                setup[kk] = _round(expr.scalar(text, known) * f + o)
            except expr.ExprError as e:
                return {"plan": None, "text": "", "title": title, "summary": "", "gaps": [
                    "the constants set %s's channels, and its formula for %s can't be worked out at them: %s" % (
                        fn.get("function") or "a Function", dep, e)]}
    if missing:
        return {"plan": None, "text": "", "title": title, "summary": "", "gaps": [
            "%s %s not channels of the rack read" % (", ".join(dict.fromkeys(missing)),
                                                    "is" if len(set(missing)) == 1 else "are")] + gaps}
    if setup:
        plan["setup"] = setup
    plan["sweep"] = sweep
    if sets:
        plan["sets"] = sets
    if repeat and repeat > 1:
        plan["repeat"] = repeat
    if time_axis:
        plan["time"] = time_axis
    if measure:
        plan["measure"] = list(dict.fromkeys(measure))
    escape = _escape(scan)
    if escape:
        after: Dict[str, Any] = {}
        for ch, v in escape.items():
            kk, f, o = key(ch, True)
            if kk is None:
                gaps.append("the escape function sets %s, which isn't a channel of the rack read" % ch)
            else:
                after[kk] = _round(v * f + o)
        if after:
            plan["after"] = after
            plan["on_abort"] = dict(after)
    elif scan.get("escapefn") not in (None, [], {}):
        gaps.append("its escape function (%s) isn't translated: Kelvin takes the run's drives to safe on a stop" %
                    (_fn_text(scan.get("escapefn")) or "a function"))
    for fn_key in ("trafofn", "configfn", "cleanupfn", "exportfn"):
        v = scan.get(fn_key)
        if v not in (None, [], {}, "") and (_fn_text(v) or (isinstance(v, (list, dict)) and v)):
            if fn_key == "trafofn":
                return {"plan": None, "text": "", "title": title, "summary": "", "gaps": [
                    "it works its values out with a global trafofn (%s), which Kelvin doesn't translate" % (
                        _fn_text(v) or "a function")]}
            gaps.append("its %s (%s) isn't translated" % (fn_key, _fn_text(v) or "a function"))
    text = dump_yaml(plan)
    summary = " × ".join("%s %s" % (k, "%s values" % len(s["values"]) if "values" in s else "%s → %s (%s)" % (
        _num(s["from"]), _num(s["to"]), s["points"])) for k, s in sweep.items())
    return {"plan": plan, "text": text, "title": title, "summary": summary, "gaps": gaps}


def _scaled(text: str, f: float, o: float) -> str:
    """An expression as its instrument gets it: x f, + o."""
    out = text if f == 1 else "(%s) * %s" % (text, _num(f))
    return out if not o else "%s %s %s" % (out, "-" if o < 0 else "+", _num(abs(o)))


def _round(v: float) -> Any:
    """A number as a plan writes it: whole numbers without .0, the rest to 12 digits."""
    v = float(v)
    if v.is_integer() and abs(v) < 1e15:
        return int(v)
    return float("%.12g" % v)


def _consts(scan: Dict[str, Any]) -> List[Dict[str, Any]]:
    consts = scan.get("consts")
    items = [consts] if isinstance(consts, dict) else consts if isinstance(consts, list) else []
    out = []
    for c in items:
        if not isinstance(c, dict):
            continue
        ch = _names(c.get("setchan"))
        val = _one(c.get("val"))
        if ch and val is not None:
            s = c.get("set")
            out.append({"setchan": ch[0], "val": val, "set": True if s in (None, [], "") else bool(_one(s))})
    return out


def _const(scan: Dict[str, Any], channel: str) -> Optional[float]:
    """The value the scan's constants leave a channel at (the last that sets it); None when none does."""
    values = [c["val"] for c in _consts(scan) if c["setchan"] == channel and c["set"]]
    return values[-1] if values else None


def _escape(scan: Dict[str, Any]) -> Dict[str, float]:
    """The channels an escape function (or several) sets to numbers; {} for none or anything else."""
    v = scan.get("escapefn")
    items = v if isinstance(v, list) else [v] if v is not None else []
    out: Dict[str, float] = {}
    for e in items:
        sets = zeroes_of(_fn_text(e))
        if sets is None:
            return {}
        out.update(sets)
    return out


def channel_map(setup: Setup, named: Dict[str, str]) -> Tuple[Dict[str, Tuple[Any, ...]], Dict[str, Dict[str, Any]]]:
    """Each channel of a rack as a Kelvin key (named: each of its instruments' Kelvin name, by its rack name), with its
    multiplier; and its function channels: name → {variables, sets: {channel: formula}}."""
    keys: Dict[str, Tuple[Any, ...]] = {}
    functions: Dict[str, Dict[str, Any]] = {}
    for ri in setup.instruments:
        if ri.function:
            variables = function_variables(ri)
            for c in ri.channels:
                functions[c.name] = {"variables": variables, "sets": dict(zip(ri.dependences, ri.formulas)),
                                     "function": ri.name}
            continue
        name = named.get(ri.name)
        if not name:
            continue
        for ch in ri.channels:
            if ch.param:
                keys[ch.name] = ("%s.%s" % (name, ch.param), ch.multiplier(),
                                 "%s.%s" % (name, ch.set_param) if ch.set_param else None,
                                 0.0 if ch.native else ch.offset)
    return keys, functions


def function_variables(ri: RackInstrument) -> List[str]:
    """A Function's channels as its formulas take them: sm's Function driver gives a formula the values of channels
    1, 2, ... in turn, so argument i is the rack channel on channel i (its place in the sminst file, else the number
    it ends in: VAR10 after VAR2). A channel number no rack channel has keeps its own name (VAR2): only a constant
    can give it a value. As the rack adds them when the numbers can't be told."""
    def number(ch: Channel) -> Optional[int]:
        if ch.channel in ri.order:
            return ri.order.index(ch.channel) + 1
        m = re.search(r"(\d+)$", ch.channel)
        return int(m.group(1)) if m else None
    placed: Dict[int, str] = {}
    for ch in ri.channels:
        n = number(ch)
        if n is None or n in placed or n > 64:
            return [c.name for c in ri.channels]
        placed[n] = ch.name
    if not placed:
        return []
    return [placed.get(i) or (ri.order[i - 1] if i - 1 < len(ri.order) else "VAR%d" % i)
            for i in range(1, max(placed) + 1)]


def names_of(setup: Setup, named: Dict[str, str], taken: Iterable[str] = ()) -> Dict[str, str]:
    """The lab config's names: for a rack: each channel's sm name → instrument.parameter (named: each instrument's
    Kelvin name, by its rack name); a name that is an instrument's, or that two channels share, is left out."""
    from .labconfig import NOT_NAMES
    from .plans import RESERVED
    blocked = set(taken) | set(named.values()) | RESERVED | NOT_NAMES | {"e", "count"}
    keys, _ = channel_map(setup, named)
    return {ch: k for ch, (k, *_) in keys.items() if ch not in blocked and re.fullmatch(r"[A-Za-z_]\w*", ch)}
