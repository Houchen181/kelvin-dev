"""Pulse sequences for spin experiments (NV centres, and atoms or ions with an AOM and a microwave or RF drive): the
timing on each channel of a pulse generator, built from a named protocol and a few times, or written out in the
sequence syntax below, and checked before anything plays. Pulse Streamer, PulseBlaster, an AWG's markers and a
simulated generator (in tests) all play the same Sequence.

Channels (the wiring; the lab config names which output is which):
    laser   the AOM (or the laser's modulation input) that switches the excitation light
    mw      the microwave switch, or the source's pulse-modulation input (mw_x, mw_y...: more microwave switches, for
            a phase each through its own path; every channel named mw or mw_... counts as microwave, for heating)
    gate    the counter gate: per period a signal window, then a reference window

Protocols (times in seconds):
    cw           laser on; MW on for the first half of the period (signal window), off for the second (reference):
                 CW ODMR while the MW frequency is swept, normalised against drift
    pulsed_odmr  MW pulse of t_pi, then laser readout: sweep the MW frequency
    rabi         MW pulse of length tau: sweep tau (the fit gives the Rabi frequency and the pi time)
    ramsey       pi/2 - tau - pi/2: sweep tau (T2*, and the detuning)
    hahn_echo    pi/2 - tau/2 - pi - tau/2 - pi/2, tau the total free evolution: sweep tau (T2)
    t1           no MW: the laser pulse initialises, a dark wait of tau, then readout: sweep tau (T1)

Each period: the MW part from t = 0, then the laser pulse (t_laser) that reads out and re-initialises, with the
signal window (t_gate) at its start and the reference window at its end, then a dark wait (t_wait) for the
metastable state to empty. `delays` moves a channel earlier by its lag (an AOM is typically late by a few hundred ns),
so the light reaches the sample when the sequence says.

Any other sequence is written out (parse), saved under a name (save_sequence) and played by setting the generator's
`sequence` to it. Times are expressions of the generator's own times (TIMES: a plan sweeps them as it sweeps tau),
numbers with units (20 ns, 3 us, 1.5e-6 is seconds) and the sequence's own let: names:

    sequence: xy4                      # its name
    let: { spacing: tau / 4 }
    steps:                             # in order; each starts where the one before ended
      - mw: t_pi / 2                   # a pulse: on this channel for this long
      - repeat: 4                      # a loop: its steps, this many times over
        steps:
          - wait: spacing / 2          # nothing on, for this long
          - mw: t_pi
          - wait: spacing / 2
      - mw: t_pi / 2
      - wait: 20 ns
      - laser: t_laser                 # pulses in one step start together; the step lasts until the last one ends
        gate:                          # a pulse with a start (from its step's start) and a duration
          - { for: t_gate, window: signal }
          - { start: t_laser - t_gate, for: t_gate, window: reference }
      - wait: t_wait

A sequence is checked when it is written (its names, channels and loops), and again at the times it will play
(check): pulses shorter than the generator makes, pulses on one channel that overlap, a microwave duty cycle above
the lab's limit, a period too long, more segments than the generator holds. A plan's sweep is checked at every
value before the run starts (plan_problems), and an approval card that starts a generator shows the timing as a
picture (svg) and as exact edges (lines)."""
from __future__ import annotations

import ast
import html
import itertools
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence as Seq, Tuple

CHANNELS = ("laser", "mw", "gate")
PROTOCOLS = ("cw", "pulsed_odmr", "rabi", "ramsey", "hahn_echo", "t1")
DEFAULTS = {"t_laser": 3e-6, "t_gate": 300e-9, "t_wait": 1e-6, "t_pi": 100e-9, "tau": 100e-9, "gap": 20e-9}
LIMITS = {"resolution": 1e-9, "max_mw_duty": 0.5, "max_period": 0.1}
TIMES = ("tau", "t_pi", "t_laser", "t_gate", "t_wait")   # a generator's times: what a written sequence may use
NONE = "none"                                            # a generator's sequence when it plays its protocol

Window = Tuple[float, float]


@dataclass
class Sequence:
    protocol: str                                           # a protocol, or a written sequence's name
    period: float
    channels: Dict[str, List[Window]]                     # on-intervals within one period, after delays
    windows: List[Tuple[str, float, float]]                # (signal|reference, start, end) counter windows
    params: Dict[str, float] = field(default_factory=dict)

    def on_time(self, channel: str) -> float:
        return sum(b - a for a, b in self.channels.get(channel, []))

    def duty(self, channel: str) -> float:
        return self.on_time(channel) / self.period if self.period else 0.0

    def mw_duty(self) -> float:
        """The share of the period any microwave channel (mw, mw_x, mw_y...) is on: what heats the sample."""
        spans = sorted(s for ch, v in self.channels.items() if is_mw(ch) for s in v)
        on, end = 0.0, -math.inf
        for a, b in spans:
            if b <= end:
                continue
            on += b - max(a, end)
            end = b
        return on / self.period if self.period else 0.0

    def segments(self) -> List[Tuple[float, frozenset]]:
        """The period as consecutive (duration, channels on): what a PulseBlaster's instructions or a Pulse
        Streamer's patterns need. Durations add up to the period."""
        edges = {0.0, self.period}
        for spans in self.channels.values():
            for a, b in spans:
                edges.update((a, b))
        times = sorted(t for t in edges if 0.0 <= t <= self.period)
        out = []
        for a, b in zip(times, times[1:]):
            if b - a <= 0:
                continue
            mid = (a + b) / 2
            on = frozenset(ch for ch, spans in self.channels.items() if any(s <= mid < e for s, e in spans))
            if out and out[-1][1] == on:
                out[-1] = (out[-1][0] + (b - a), on)
            else:
                out.append((b - a, on))
        return out

    def pattern(self, channel: str, unit: float = 1e-9) -> List[Tuple[int, int]]:
        """One channel as (duration in `unit`, level 0/1) pairs, as a Pulse Streamer takes it."""
        out: List[Tuple[int, int]] = []
        for duration, on in self.segments():
            level = 1 if channel in on else 0
            n = int(round(duration / unit))
            if out and out[-1][1] == level:
                out[-1] = (out[-1][0] + n, level)
            elif n:
                out.append((n, level))
        return out

    def diagram(self, width: int = 64) -> str:
        """The period in text, one row per channel: a glance only, at this width a 4 ns pulse doesn't show (lines()
        gives every edge, svg() a picture where every pulse does)."""
        rows = []
        for ch in self.order():
            cells = []
            for k in range(width):
                t = (k + 0.5) * self.period / width
                cells.append("#" if any(a <= t < b for a, b in self.channels[ch]) else "_")
            rows.append("%-6s %s" % (ch, "".join(cells)))
        rows.append("%-6s 0%s%s" % ("", " " * (width - len(_si(self.period)) - 1), _si(self.period)))
        return "\n".join(rows)

    def order(self) -> List[str]:
        """Its channels as a diagram lists them: laser, the microwave ones, the gate, then the rest."""
        rank = {"laser": 0, "mw": 1, "gate": 3}
        return sorted(self.channels, key=lambda ch: (rank.get(ch, 2 if is_mw(ch) else 4), ch))


def is_mw(channel: str) -> bool:
    return channel == "mw" or channel.startswith("mw_")


def _si(seconds: float) -> str:
    for unit, scale in (("s", 1), ("ms", 1e-3), ("us", 1e-6), ("ns", 1e-9)):
        if abs(seconds) >= scale or unit == "ns":
            return "%.4g %s" % (seconds / scale, unit)
    return "%g s" % seconds


def exact(seconds: float) -> str:
    """A time to the digit, in the unit that suits it: 420 ns, 3.42 µs, 1.0505 µs (an edge table's numbers)."""
    if seconds == 0:
        return "0"
    for unit, scale in (("s", 1.0), ("ms", 1e-3), ("µs", 1e-6), ("ns", 1e-9), ("ps", 1e-12)):
        if abs(seconds) >= scale * 0.9999999 or unit == "ps":
            return "%.9g %s" % (seconds / scale, unit)
    return "%g s" % seconds


def mw_pulses(protocol: str, p: Dict[str, float]) -> Tuple[List[Window], float]:
    """The MW on-intervals from t = 0, and when the MW part ends."""
    t_pi, tau = p["t_pi"], p["tau"]
    h = t_pi / 2
    if protocol == "pulsed_odmr":
        return [(0.0, t_pi)], t_pi
    if protocol == "rabi":
        return ([(0.0, tau)] if tau > 0 else []), tau
    if protocol == "ramsey":
        return [(0.0, h), (h + tau, 2 * h + tau)], 2 * h + tau
    if protocol == "hahn_echo":
        a = h + tau / 2
        b = a + t_pi + tau / 2
        return [(0.0, h), (a, a + t_pi), (b, b + h)], b + h
    if protocol == "t1":
        return [], tau
    raise ValueError("unknown protocol %r (known: %s)" % (protocol, ", ".join(PROTOCOLS)))


def build(protocol: str, params: Optional[Dict[str, float]] = None,
          delays: Optional[Dict[str, float]] = None) -> Sequence:
    """The sequence for a protocol (see the module's notes); missing times take DEFAULTS."""
    p = dict(DEFAULTS, **{k: float(v) for k, v in (params or {}).items() if v is not None})
    if protocol == "cw":
        half = p["t_laser"]
        chans = {"laser": [(0.0, 2 * half)], "mw": [(0.0, half)], "gate": [(0.0, half), (half, 2 * half)]}
        windows = [("signal", 0.0, half), ("reference", half, 2 * half)]
        return Sequence(protocol, 2 * half, chans, windows, p)
    mw, mw_end = mw_pulses(protocol, p)
    start = mw_end + p["gap"]
    end = start + p["t_laser"]
    period = end + p["t_wait"]
    g = p["t_gate"]
    windows = [("signal", start, start + g), ("reference", end - g, end)]
    chans = {"laser": [(start, end)], "mw": mw, "gate": [(a, b) for _, a, b in windows]}
    seq = Sequence(protocol, period, chans, windows, p)
    _delay(seq, delays)
    return seq


def _delay(seq: Sequence, delays: Optional[Dict[str, float]]) -> None:
    for ch, lag in (delays or {}).items():   # a late channel is started early by its lag, wrapping around the period
        if lag and ch in seq.channels:
            seq.channels[ch] = _shift(seq.channels[ch], -float(lag), seq.period)


def _shift(spans: Iterable[Window], dt: float, period: float) -> List[Window]:
    out: List[Window] = []
    for a, b in spans:
        a, b = a + dt, b + dt
        k = (a // period) * period
        a, b = a - k, b - k
        if b <= period:
            out.append((a, b))
        else:
            out += [(a, period), (0.0, b - period)]
    return sorted(out)


def check(seq: Sequence, limits: Optional[Dict[str, float]] = None) -> List[str]:
    """What is wrong with a sequence before it plays: pulses shorter than the generator can make, windows that
    overlap, a microwave duty cycle above the lab's limit, a period too long, more segments than the generator
    holds, counter windows it can't tell apart."""
    lim = dict(LIMITS, **(limits or {}))
    res = lim["resolution"]
    problems = []
    p = seq.params
    if seq.protocol in PROTOCOLS:   # a written sequence's times are checked as its pulses, below
        for name in ("t_laser", "t_gate", "t_pi"):
            if name in p and p[name] < res and not (name == "t_pi" and seq.protocol in ("cw", "t1")):
                problems.append("%s = %s is shorter than the generator's %s resolution"
                                % (name, _si(p[name]), _si(res)))
        if p.get("tau", 0) < 0:
            problems.append("tau must not be negative")
        if seq.protocol not in ("cw",) and p.get("t_gate", 0) * 2 > p.get("t_laser", 0):
            problems.append("the signal and reference windows overlap: t_gate (%s) must be at most half of t_laser "
                            "(%s)" % (_si(p["t_gate"]), _si(p["t_laser"])))
    if seq.period <= 0:
        problems.append("the sequence has no length: nothing in it takes any time")
        return problems
    for ch, spans in seq.channels.items():
        for a, b in spans:
            if 0 < b - a < res * (1 - 1e-9):
                problems.append("a %s pulse of %s is shorter than the generator's %s resolution"
                                % (ch, _si(b - a), _si(res)))
                break
    duty = seq.mw_duty()
    if duty > lim["max_mw_duty"]:
        problems.append("the microwave is on %.0f%% of the time, above the lab's %.0f%% limit (heating): lengthen "
                        "t_wait or shorten the MW pulses" % (100 * duty, 100 * lim["max_mw_duty"]))
    if seq.period > lim["max_period"]:
        problems.append("the period %s is longer than %s" % (_si(seq.period), _si(lim["max_period"])))
    most = lim.get("max_segments")
    if most:
        n = len(seq.segments())
        if n > most:
            problems.append("it has %d segments, more than the generator holds (%d): fewer loops or pulses"
                            % (n, int(most)))
    labels = [w for w, _, _ in seq.windows]
    if len(labels) > 2 or len(set(labels)) < len(labels):
        problems.append("the counter reads one signal and one reference window per period; the gate has %d windows "
                        "(%s)" % (len(labels), ", ".join(labels)))
    return problems


def describe(seq: Sequence) -> str:
    """A short text for a person or a model: the protocol, its times, duty cycles and every edge."""
    keys = [k for k in ("t_pi", "tau", "t_laser", "t_gate", "t_wait") if k in seq.params]
    times = ", ".join("%s %s" % (k, _si(seq.params[k])) for k in keys)
    return "%s: %s; period %s, MW duty %.1f%%, laser duty %.0f%%\n%s" % (
        seq.protocol, times, _si(seq.period), 100 * seq.mw_duty(), 100 * seq.duty("laser"),
        "\n".join(lines(seq, most=40)))


def counts_split(values: Seq[float]) -> Tuple[float, float]:
    """Counts from a counter that alternates signal and reference windows (bins 0, 2, 4... and 1, 3, 5...)."""
    return float(sum(values[0::2])), float(sum(values[1::2]))


# ------------------------------------------------------------------ the edges, exactly

def lines(seq: Sequence, most: int = 12) -> List[str]:
    """Every channel's pulses as exact times from the period's start, one line each ("mw: 0 → 50 ns, 1.05 µs →
    1.15 µs"); the gate's as its signal and reference windows. After `most` pulses a channel's line says how many
    more there are."""
    out = []
    windows = {(round(a, 15), round(b, 15)): w for w, a, b in seq.windows}
    for ch in seq.order():
        spans = seq.channels[ch]
        if not spans:
            out.append("%s: off" % ch)
            continue
        parts = []
        for a, b in spans[:most]:
            label = windows.get((round(a, 15), round(b, 15))) if ch == "gate" else None
            parts.append("%s%s → %s" % (label + " " if label else "", exact(a), exact(b)))
        more = len(spans) - most
        out.append("%s: %s%s" % (ch, ", ".join(parts), "; and %d more" % more if more > 0 else ""))
    return out


def summary(seq: Sequence) -> str:
    """One line: what plays, how long a period is, and how much of it the microwave and the laser are on."""
    return "%s in a loop: period %s, microwave on %.1f%% of the time, laser %.0f%%" % (
        seq.protocol, exact(seq.period), 100 * seq.mw_duty(), 100 * seq.duty("laser"))


# ------------------------------------------------------------------ the sequence syntax

class SequenceError(ValueError):
    def __init__(self, errors: List[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


NAME = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
CHANNEL = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
RESERVED = {"wait", "repeat", "steps", "label", "length", "for", "start", "window", "let", "sequence"}
WINDOWS = ("signal", "reference")
MAX_REPEAT = 10000
MAX_DEPTH = 6
MAX_PULSES = 200_000
_UNITS = {"ps": 1e-12, "ns": 1e-9, "us": 1e-6, "µs": 1e-6, "μs": 1e-6, "ms": 1e-3, "s": 1.0}
_TIME = re.compile(r"(?<![\w.])((?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(ps|ns|us|µs|μs|ms|s)(?!\w)")
_EPS = 1e-15


def seconds_text(text: Any) -> str:
    """An expression with its times in seconds: 20 ns → (20*1e-09). Numbers without a unit are seconds."""
    if isinstance(text, bool):
        raise ValueError("a time, not %r" % text)
    if isinstance(text, (int, float)):
        return repr(float(text))
    return _TIME.sub(lambda m: "(%s*%r)" % (m.group(1), _UNITS[m.group(2)]), str(text).strip())


@dataclass
class Pulse:
    channel: str
    length: str                       # an expression, in seconds
    start: str = "0"                  # from its step's start
    window: Optional[str] = None      # a gate pulse: the counter's signal or reference window
    where: str = ""                   # its place in the text, for messages


@dataclass
class Step:
    pulses: List[Pulse] = field(default_factory=list)
    wait: Optional[str] = None
    length: Optional[str] = None      # how long the step lasts (default: until its last pulse ends)
    repeat: int = 0                   # a loop: its steps, this many times (0: not a loop)
    steps: List["Step"] = field(default_factory=list)
    label: str = ""
    where: str = ""


@dataclass
class Program:
    """A written sequence, checked as written; build() gives its timing at a generator's times."""
    name: str
    steps: List[Step]
    let: List[Tuple[str, str]] = field(default_factory=list)
    period: Optional[str] = None
    description: str = ""
    source: Dict[str, Any] = field(default_factory=dict)

    @property
    def channels(self) -> List[str]:
        out: List[str] = []

        def walk(steps: List[Step]) -> None:
            for s in steps:
                for p in s.pulses:
                    if p.channel not in out:
                        out.append(p.channel)
                walk(s.steps)
        walk(self.steps)
        return out

    def build(self, times: Optional[Dict[str, Any]] = None, delays: Optional[Dict[str, float]] = None) -> Sequence:
        """Its timing at these times (missing ones from DEFAULTS); ValueError, in words, for a pulse that can't be
        (negative, before its step, overlapping another on its channel, beyond a fixed period)."""
        env = {k: float(v) for k, v in DEFAULTS.items() if k in TIMES}
        env.update({k: float(v) for k, v in (times or {}).items() if k in TIMES and v is not None})
        for name, text in self.let:
            env[name] = _evaluate(text, env, "let.%s" % name)
        spans: Dict[str, List[Tuple[float, float, Optional[str], str]]] = {ch: [] for ch in self.channels}
        count = [0]

        def walk(steps: List[Step], t: float) -> float:
            for st in steps:
                if st.repeat:
                    for _ in range(st.repeat):
                        t = walk(st.steps, t)
                    continue
                if st.wait is not None:
                    d = _evaluate(st.wait, env, st.where + ".wait")
                    if d < 0:
                        raise ValueError("%s: wait is %s, less than nothing" % (st.where, exact(d)))
                    t += d
                    continue
                end = t
                for p in st.pulses:
                    a = t + _evaluate(p.start, env, p.where + ".start")
                    n = _evaluate(p.length, env, p.where)
                    if n < 0:
                        raise ValueError("%s: the %s pulse is %s long, less than nothing" % (p.where, p.channel,
                                                                                          exact(n)))
                    if a < t - _EPS:
                        raise ValueError("%s: the %s pulse starts %s before its step" % (p.where, p.channel,
                                                                                      exact(t - a)))
                    if n > 0:
                        count[0] += 1
                        if count[0] > MAX_PULSES:
                            raise ValueError("more than %d pulses in one period: fewer loops" % MAX_PULSES)
                        spans[p.channel].append((a, a + n, p.window, p.where))
                    end = max(end, a + n)
                if st.length is not None:
                    n = _evaluate(st.length, env, st.where + ".length")
                    if n < 0:
                        raise ValueError("%s: length is %s, less than nothing" % (st.where, exact(n)))
                    end = t + n
                t = end
            return t

        cursor = walk(self.steps, 0.0)
        last = max([b for v in spans.values() for _, b, _, _ in v] + [cursor])
        period = last
        if self.period is not None:
            period = _evaluate(self.period, env, "period")
            if period < last - _EPS:
                raise ValueError("period: %s is shorter than the sequence, which runs to %s" % (exact(period),
                                                                                              exact(last)))
        chans: Dict[str, List[Window]] = {}
        windows: List[Tuple[str, float, float]] = []
        for ch, found in spans.items():
            found.sort(key=lambda s: (s[0], s[1]))
            for (a0, b0, _, w0), (a1, b1, _, w1) in zip(found, found[1:]):
                if a1 < b0 - _EPS:
                    raise ValueError("two %s pulses overlap: %s → %s (%s) and %s → %s (%s)" % (
                        ch, exact(a0), exact(b0), w0, exact(a1), exact(b1), w1))
            chans[ch] = [(a, b) for a, b, _, _ in found]
            if ch == "gate":
                labelled = [w for _, _, w, _ in found]
                if all(w is None for w in labelled):   # unlabelled: signal, then reference
                    labelled = [WINDOWS[i % 2] for i in range(len(found))]
                windows = [(w, a, b) for (a, b, _, _), w in zip(found, labelled)]
        seq = Sequence(self.name, period, chans, windows, dict(env))
        _delay(seq, delays)
        return seq


def _names(text: str) -> List[str]:
    """The names an expression uses (not its functions'); SyntaxError when it isn't one."""
    tree = ast.parse(text, mode="eval")
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    return sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and id(n) not in called})


def _evaluate(text: str, env: Dict[str, float], where: str) -> float:
    import numpy as np
    from . import expr

    def column(name: str) -> Any:
        if name in env:
            return np.float64(env[name])
        raise KeyError("%r is not one of the generator's times (%s) nor a let: name" % (name, ", ".join(TIMES)))
    try:
        value = float(expr.evaluate(text, column, np))
    except (expr.ExprError, TypeError, ValueError) as e:
        raise ValueError("%s: %s" % (where, e)) from None
    if not math.isfinite(value):
        raise ValueError("%s: %s is not a finite time" % (where, text))
    return value


def parse(source: Any) -> Program:
    """A sequence from its text (YAML) or mapping, checked as written: its name, steps, channels, loops and every
    expression's names. SequenceError lists each problem with where it is."""
    from .labconfig import read_yaml
    try:
        data, _ = read_yaml(source)
    except Exception as e:
        raise SequenceError(["not readable as YAML: %s" % str(e).splitlines()[0][:160]]) from None
    if not isinstance(data, dict):
        raise SequenceError(["a sequence is a mapping: sequence (its name), let and steps"])
    errors: List[str] = []
    known = {"sequence", "description", "let", "steps", "period"}
    for key in data:
        if key not in known:
            errors.append("unknown key %r (a sequence has %s)" % (key, ", ".join(sorted(known))))
    name = str(data.get("sequence") or "").strip()
    if not NAME.match(name):
        errors.append("sequence: a name of lower-case letters, digits and _ (how a generator is told to play it)")
    elif name in PROTOCOLS or name == NONE:
        errors.append("sequence: %r is a protocol's name already; give it its own" % name)
    names = set(TIMES)
    from .expr import CONSTANTS

    def expression(value: Any, where: str) -> Optional[str]:
        if value is None or isinstance(value, (bool, dict, list)):
            errors.append("%s: a time (a number of seconds, 20 ns, or an expression such as tau / 2)" % where)
            return None
        text = seconds_text(value)
        try:
            unknown = [n for n in _names(text) if n not in names and n not in CONSTANTS]
        except SyntaxError as e:
            errors.append("%s: not an expression: %s (%s)" % (where, value, e.msg))
            return None
        if unknown:
            errors.append("%s: %s %s not one of the generator's times (%s) nor a let: name" % (
                where, ", ".join(unknown), "is" if len(unknown) == 1 else "are", ", ".join(TIMES)))
            return None
        return text

    let: List[Tuple[str, str]] = []
    raw_let = data.get("let") or {}
    if not isinstance(raw_let, dict):
        errors.append("let: a mapping of name: expression")
        raw_let = {}
    for key, value in raw_let.items():
        key = str(key)
        if not NAME.match(key) or key in TIMES or key in CONSTANTS or key in RESERVED or key in _UNITS:
            errors.append("let.%s: a name of its own (lower-case; not a generator's time, a constant such as pi, a "
                          "unit such as ns, or a word of the syntax)" % key)
            continue
        text = expression(value, "let.%s" % key)
        if text is not None:
            let.append((key, text))
            names.add(key)

    def step(raw: Any, where: str, depth: int) -> Optional[Step]:
        if not isinstance(raw, dict) or not raw:
            errors.append("%s: a step is a mapping: {channel: duration}, {wait: duration} or {repeat: n, steps: "
                          "[...]}" % where)
            return None
        label = str(raw.get("label") or "")
        if "repeat" in raw or "steps" in raw:
            extra = set(raw) - {"repeat", "steps", "label"}
            n = raw.get("repeat")
            if extra:
                errors.append("%s: a loop has repeat, steps and label, not %s" % (where, ", ".join(sorted(map(str,
                                                                                                        extra)))))
                return None
            if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_REPEAT:
                errors.append("%s.repeat: how many times, a whole number from 1 to %d" % (where, MAX_REPEAT))
                return None
            if depth >= MAX_DEPTH:
                errors.append("%s: loops are nested more than %d deep" % (where, MAX_DEPTH))
                return None
            body = steps(raw.get("steps"), where + ".steps", depth + 1)
            return Step(repeat=n, steps=body, label=label, where=where) if body else None
        if "wait" in raw:
            extra = set(raw) - {"wait", "label"}
            if extra:
                errors.append("%s: a wait is on its own in its step (put %s in a step of its own)"
                              % (where, ", ".join(sorted(map(str, extra)))))
                return None
            text = expression(raw["wait"], where + ".wait")
            return Step(wait=text, label=label, where=where) if text is not None else None
        out = Step(label=label, where=where)
        if "length" in raw:
            out.length = expression(raw["length"], where + ".length")
        for ch, spec in raw.items():
            ch = str(ch)
            if ch in ("label", "length"):
                continue
            if ch in RESERVED or not CHANNEL.match(ch):
                errors.append("%s: %r is not a channel (lower-case letters, digits and _, wired on the generator)"
                              % (where, ch))
                continue
            items = spec if isinstance(spec, list) else [spec]
            for k, item in enumerate(items):
                at = "%s.%s%s" % (where, ch, "[%d]" % k if isinstance(spec, list) else "")
                if isinstance(item, dict):
                    extra = set(item) - {"for", "start", "window"}
                    if extra or "for" not in item:
                        errors.append("%s: a pulse is a duration, or {for: duration, start: from the step's start, "
                                      "window: signal or reference}" % at)
                        continue
                    window = item.get("window")
                    if window is not None and (ch != "gate" or window not in WINDOWS):
                        errors.append("%s.window: only the gate has windows: signal or reference" % at)
                        continue
                    length = expression(item["for"], at + ".for")
                    start = expression(item.get("start", 0), at + ".start")
                    if length is not None and start is not None:
                        out.pulses.append(Pulse(ch, length, start, window, at))
                else:
                    length = expression(item, at)
                    if length is not None:
                        out.pulses.append(Pulse(ch, length, "0", None, at))
        if not out.pulses and not raw.get("length"):
            if not any(e.startswith(where) for e in errors):
                errors.append("%s: the step has no pulses" % where)
            return None
        return out

    def steps(raw: Any, where: str, depth: int) -> List[Step]:
        if not isinstance(raw, list) or not raw:
            errors.append("%s: a list of steps" % where)
            return []
        return [s for s in (step(item, "%s[%d]" % (where, i), depth) for i, item in enumerate(raw)) if s is not None]

    body = steps(data.get("steps"), "steps", 0)
    period = expression(data["period"], "period") if data.get("period") is not None else None
    program = Program(name, body, let, period, str(data.get("description") or ""), dict(data))
    gate = []

    def windows(steps_: List[Step]) -> None:
        for s in steps_:
            gate.extend(p.window for p in s.pulses if p.channel == "gate")
            windows(s.steps)
    windows(body)
    if any(w is None for w in gate) and any(w is not None for w in gate):
        errors.append("gate: say signal or reference on every window, or on none (then they alternate, signal "
                      "first)")
    if not errors and not program.channels:
        errors.append("steps: nothing is ever on: no channel has a pulse")
    if errors:
        raise SequenceError(errors)
    return program


# ------------------------------------------------------------------ written sequences a generator can play

LIBRARY: Dict[str, Program] = {}   # the lab's written sequences, by name (use_folder loads them)


def register(program: Program) -> None:
    LIBRARY[program.name] = program


def forget(name: str) -> None:
    LIBRARY.pop(name, None)


def program(name: str) -> Program:
    try:
        return LIBRARY[name]
    except KeyError:
        raise KeyError("no sequence %r (saved: %s)" % (name, ", ".join(sorted(LIBRARY)) or "none yet")) from None


def use_folder(folder: Any) -> List[str]:
    """The lab's written sequences, from its sequences folder (each NAME.yaml), in place of any loaded before. What
    can't be read is left out and said."""
    LIBRARY.clear()
    problems = []
    path = Path(folder) if folder is not None else None
    if path is None or not path.is_dir():
        return problems
    for f in sorted(path.glob("*.yaml")):
        try:
            prog = parse(f.read_text(encoding="utf-8"))
        except (OSError, SequenceError) as e:
            problems.append("%s: %s" % (f.name, e))
            continue
        if prog.name != f.stem:
            problems.append("%s: its sequence is named %r; the file must be %s.yaml" % (f.name, prog.name, prog.name))
            continue
        register(prog)
    return problems


def save(folder: Any, text: str) -> Program:
    """Check a written sequence and keep it in the lab's sequences folder, so a generator can play it by name."""
    prog = parse(text)
    path = Path(folder)
    path.mkdir(parents=True, exist_ok=True)
    (path / (prog.name + ".yaml")).write_text(str(text).strip() + "\n", encoding="utf-8")
    register(prog)
    return prog


def make(protocol: str, sequence: Any, times: Dict[str, Any], delays: Optional[Dict[str, float]] = None) -> Sequence:
    """What a generator plays: its written sequence when one is chosen (not none), else its protocol."""
    name = str(sequence or NONE).strip().lower()
    if name not in ("", NONE, "0"):
        return program(name).build(times, delays)
    return build(str(protocol), {k: times[k] for k in TIMES if times.get(k) is not None}, delays)


# ------------------------------------------------------------------ the timing as a picture

INK, MUTED, AXIS, PAPER = "1f2328", "5f6b78", "9aa4ae", "ffffff"
COLORS = {"laser": "1baf7a", "gate": "eb6834"}
MW_COLORS = ["2a78d6", "6250d6", "008300", "e34948"]
OTHER = ["5f6b78", "e87ba4", "eda100"]
WIDTH, LEFT, RIGHT, ROW, HIGH = 760, 74, 18, 38, 20
MIN_W = 7.0     # px: no interval narrower, so a 4 ns pulse in a 3 µs period still shows


def _color(ch: str, k: int) -> str:
    if ch in COLORS:
        return COLORS[ch]
    if is_mw(ch):
        return MW_COLORS[k % len(MW_COLORS)]
    return OTHER[k % len(OTHER)]


def _layout(seq: Sequence, title: str = "") -> Tuple[int, int, List[Tuple[Any, ...]], bool]:
    """The diagram as drawing operations (rect, line, text) for svg() and png(): one row per channel, each interval
    between edges at least MIN_W wide (not to scale when that stretches one), its length under the axis where it
    fits. (width, height, ops, to scale)."""
    order = seq.order()
    times = sorted({0.0, seq.period} | {t for ch in order for span in seq.channels[ch] for t in span})
    times = [t for t in times if 0.0 <= t <= seq.period]
    gaps = [b - a for a, b in zip(times, times[1:])]
    plot = WIDTH - LEFT - RIGHT
    n = max(1, len(gaps))
    floor = min(MIN_W, plot / n / 2.0)
    to_scale = all(g / seq.period * plot >= floor for g in gaps if g > 0) if seq.period > 0 else True
    if to_scale:
        widths = [g / seq.period * plot if seq.period > 0 else 0.0 for g in gaps]
    else:   # longer intervals stay wider, but every one shows: 4 ns beside 1 ms
        shortest = min(g for g in gaps if g > 0)
        weights = [math.log1p(g / shortest) if g > 0 else 0.0 for g in gaps]
        total = sum(weights) or 1.0
        spare = plot - floor * sum(1 for g in gaps if g > 0)
        widths = [floor + spare * w / total if g > 0 else 0.0 for g, w in zip(gaps, weights)]
    xs = [float(LEFT)]
    for w in widths:
        xs.append(xs[-1] + w)
    at = dict(zip(times, xs))

    def x(t: float) -> float:
        if t in at:
            return at[t]
        k = max(0, min(len(times) - 2, next((i for i in range(len(times) - 1) if times[i] <= t < times[i + 1]),
                                             len(times) - 2)))
        frac = (t - times[k]) / (times[k + 1] - times[k]) if times[k + 1] > times[k] else 0.0
        return xs[k] + frac * (xs[k + 1] - xs[k])

    ops: List[Tuple[Any, ...]] = []
    top = 30
    ops.append(("text", LEFT, 19, title or "%s: period %s, MW %.1f%%, laser %.0f%%" % (
        seq.protocol, exact(seq.period), 100 * seq.mw_duty(), 100 * seq.duty("laser")), 13, INK, "start", True))
    windows = {(round(a, 15), round(b, 15)): w for w, a, b in seq.windows}
    mw_k = other_k = 0
    for row, ch in enumerate(order):
        base = top + row * ROW + ROW - 8
        color = _color(ch, mw_k if is_mw(ch) else other_k)
        if is_mw(ch):
            mw_k += 1
        elif ch not in COLORS:
            other_k += 1
        ops.append(("text", LEFT - 10, base - 4, ch, 12, INK, "end", False))
        ops.append(("line", LEFT, base, LEFT + plot, base, AXIS, 1.0))
        for a, b in seq.channels[ch]:
            xa, xb = x(a), x(b)
            ops.append(("rect", xa, base - HIGH, max(xb - xa, 1.5), HIGH, color))
            label = windows.get((round(a, 15), round(b, 15))) if ch == "gate" else None
            if label and xb - xa >= 11:
                ops.append(("text", (xa + xb) / 2, base - 6, label[0].upper(), 11, PAPER, "middle", True))
    axis_y = top + len(order) * ROW + 6
    ops.append(("line", LEFT, axis_y, LEFT + plot, axis_y, MUTED, 1.0))
    for t, xt in zip(times, xs):
        ops.append(("line", xt, axis_y - 3, xt, axis_y + 3, MUTED, 1.0))
    for (a, b), g, w in zip(zip(xs, xs[1:]), gaps, widths):
        text = exact(g)
        if g > 0 and _text_width(text, 10) <= w - 3:
            ops.append(("text", (a + b) / 2, axis_y + 15, text, 10, MUTED, "middle", False))
        if not to_scale and g > 0 and g / seq.period * plot > w * 3:   # drawn far shorter than it is: a break
            mid = (a + b) / 2
            ops.append(("line", mid - 4, axis_y + 4, mid - 1, axis_y - 4, INK, 1.0))
            ops.append(("line", mid + 1, axis_y + 4, mid + 4, axis_y - 4, INK, 1.0))
    ops.append(("text", LEFT, axis_y + 32, "0", 10, MUTED, "start", False))
    ops.append(("text", LEFT + plot, axis_y + 32, "period %s" % exact(seq.period), 10, MUTED, "end", False))
    note = "to scale" if to_scale else "not to scale: each interval's length is under it"
    ops.append(("text", LEFT + plot / 2, axis_y + 32, note, 10, MUTED, "middle", False))
    return WIDTH, axis_y + 42, ops, to_scale


def _text_width(text: str, size: float) -> float:
    return len(text) * size * 0.56


def svg(seq: Sequence, title: str = "") -> str:
    """The timing diagram as an SVG document: every pulse visible, its intervals labelled with their lengths."""
    w, h, ops, _ = _layout(seq, title)
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
           'font-family="Helvetica, Arial, sans-serif" role="img">' % (w, h, w, h),
           "<title>%s</title>" % html.escape(caption(seq)),
           '<rect width="100%%" height="100%%" fill="#%s"/>' % PAPER]
    for op in ops:
        if op[0] == "rect":
            _, x, y, rw, rh, color = op
            out.append('<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="#%s"/>' % (x, y, rw, rh, color))
        elif op[0] == "line":
            _, x1, y1, x2, y2, color, width = op
            out.append('<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" stroke="#%s" stroke-width="%g"/>'
                       % (x1, y1, x2, y2, color, width))
        else:
            _, x, y, text, size, color, anchor, bold = op
            out.append('<text x="%.2f" y="%.2f" font-size="%g" fill="#%s" text-anchor="%s"%s>%s</text>' % (
                x, y, size, color, anchor, ' font-weight="bold"' if bold else "", html.escape(str(text))))
    out.append("</svg>")
    return "\n".join(out)


def png(seq: Sequence, title: str = "") -> bytes:
    """The same diagram as a PNG (Kelvin's own drawing, no plotting library), for a model that sees pictures."""
    from . import pictures
    w, h, ops, _ = _layout(seq, title)
    c = pictures.Canvas(w, h)
    for op in ops:
        if op[0] == "rect":
            _, x, y, rw, rh, color = op
            c.rect(x, y, rw, rh, color)
        elif op[0] == "line":
            _, x1, y1, x2, y2, color, width = op
            c.line([(x1, y1), (x2, y2)], color, width)
        else:
            _, x, y, text, size, color, anchor, bold = op
            c.text(x, y, str(text).replace("→", "-"), size, color, {"start": "left", "middle": "center",
                                                                    "end": "right"}[anchor], bold)
    return c.png()


def caption(seq: Sequence, where: str = "") -> str:
    return "Timing of %s%s: %s" % (seq.protocol, " " + where if where else "", "; ".join(lines(seq, most=6)))


# ------------------------------------------------------------------ a generator in the lab: what it would play

TIME_PARAMS = TIMES + ("protocol", "sequence")


def is_generator(inst: Any) -> bool:
    card = getattr(inst, "card", None)
    return getattr(card, "kind", "") == "pulse-generator" and "protocol" in getattr(card, "params", {})


def limits_of(inst: Any) -> Dict[str, float]:
    """The checks a generator's sequences must pass: its driver's (resolution, max_mw_duty, max_segments, as its
    card's on_connect set them), else the lab's defaults; a simulated one's max_mw_duty."""
    backend = getattr(inst, "backend", None)
    own = getattr(backend, "limits", None)
    out = dict(LIMITS)
    if isinstance(own, dict):
        out.update({k: float(v) for k, v in own.items() if isinstance(v, (int, float))})
    state = getattr(backend, "state", None)
    if isinstance(state, dict) and state.get("max_mw_duty") is not None:
        out["max_mw_duty"] = float(state["max_mw_duty"])
    return out


def delays_of(inst: Any) -> Dict[str, float]:
    own = getattr(getattr(inst, "backend", None), "delays", None)
    return dict(own) if isinstance(own, dict) else {}


def settings_of(inst: Any, changes: Optional[Dict[str, Any]] = None,
                read: Optional[Callable[[str], Any]] = None) -> Dict[str, Any]:
    """A generator's protocol, written sequence and times: as it has them now (read), with these changes on top."""
    read = read or inst.read
    out: Dict[str, Any] = {}
    for key in TIME_PARAMS:
        if key in (changes or {}):
            out[key] = changes[key]
        elif key in inst.card.params:
            try:
                out[key] = read(key)
            except Exception:
                pass
    return out


def playing(inst: Any, changes: Optional[Dict[str, Any]] = None) -> Sequence:
    """The sequence this generator plays once these changes are made (ValueError/KeyError when it can't be built)."""
    s = settings_of(inst, changes)
    times = {k: float(s[k]) for k in TIMES if isinstance(s.get(k), (int, float)) and not isinstance(s.get(k), bool)}
    return make(str(s.get("protocol") or "pulsed_odmr"), s.get("sequence"), times, delays_of(inst))


def figure(seq: Sequence, where: str = "") -> Dict[str, str]:
    """What an approval card shows: the diagram (SVG) and a caption with the edges."""
    return {"svg": svg(seq), "caption": caption(seq, where)}


def illustrate(request: Any, inst: Any, changes: Dict[str, Any]) -> None:
    """A change that starts a pulse generator playing: its card shows what will play, as a picture and as exact
    edges, and what is wrong with it. Never stops the card: a sequence that can't be drawn is said in words."""
    try:
        if not is_generator(inst) or not changes.get("running"):
            return
        try:
            seq = playing(inst, changes)
        except (KeyError, ValueError) as e:
            request.items.append("%s's sequence can't be built: %s" % (inst.name, e.args[0] if e.args else e))
            return
        request.items.append("%s plays %s" % (inst.name, summary(seq)))
        request.items += ["%s %s" % (inst.name, line) for line in lines(seq)]
        problems = check(seq, limits_of(inst))
        if problems:
            request.items.append("%s will refuse it: %s" % (inst.name, "; ".join(problems)))
        request.figures.append(figure(seq))
    except Exception:
        return


# ------------------------------------------------------------------ plans: every value checked before the run

def _played(lab: Any, plan: Any) -> List[Tuple[Any, Dict[str, Any], Dict[str, List[Any]]]]:
    """The generators a plan plays: (instrument, its settings as the setup leaves them, its swept times and their
    values). A generator plays if the setup switches it on, or it is on already and the plan doesn't switch it off."""
    out = []
    for name in getattr(plan, "requires", []) or []:
        inst = getattr(lab, "instruments", {}).get(name)
        if inst is None or not is_generator(inst):
            continue
        setup = {s.param: s.value for s in getattr(plan, "setup", []) if s.instr == name}
        swept = {a.param: list(a.values) for a in getattr(plan, "axes", [])
                 if getattr(a, "kind", "") == "instrument" and a.instr == name and a.param in TIME_PARAMS}
        running = setup.get("running")
        if running is None:
            try:
                running = inst.read("running")
            except Exception:
                running = False
        try:
            on = running is True or (not isinstance(running, bool) and str(running).strip().lower() in
                                     ("1", "on", "true", "yes"))
        except Exception:
            on = False
        if not on:
            continue
        settings = settings_of(inst, {k: v for k, v in setup.items() if k in TIME_PARAMS})
        out.append((inst, settings, swept))
    return out


def _combinations(swept: Dict[str, List[Any]], most: int = 4000) -> Iterable[Dict[str, Any]]:
    """Every combination of the swept values; past `most`, each value with the others at their ends."""
    keys = list(swept)
    total = 1
    for k in keys:
        total *= max(1, len(swept[k]))
    if total <= most:
        for combo in itertools.product(*(swept[k] for k in keys)):
            yield dict(zip(keys, combo))
        return
    ends = {k: [v[0], v[-1]] for k, v in swept.items()}
    for combo in itertools.product(*(ends[k] for k in keys)):
        yield dict(zip(keys, combo))
    for k in keys:
        for v in swept[k]:
            yield dict({o: swept[o][0] for o in keys}, **{k: v})


def _seq_at(inst: Any, settings: Dict[str, Any], values: Dict[str, Any]) -> Sequence:
    s = dict(settings, **values)
    times = {k: float(s[k]) for k in TIMES if isinstance(s.get(k), (int, float)) and not isinstance(s.get(k), bool)}
    return make(str(s.get("protocol") or "pulsed_odmr"), s.get("sequence"), times, delays_of(inst))


def _shown(values: Dict[str, Any]) -> str:
    return ", ".join("%s = %s" % (k, exact(float(v)) if isinstance(v, (int, float)) and not isinstance(v, bool)
                                  else v) for k, v in values.items())


def plan_problems(lab: Any, plan: Any) -> List[str]:
    """What a plan's generators would refuse, at every value its sweep gives them, found before anything plays (a
    Rabi sweep whose last tau heats the sample once stopped the run there, after half of it had run)."""
    out: List[str] = []
    for inst, settings, swept in _played(lab, plan):
        lim = limits_of(inst)
        seen = set()
        for values in _combinations(swept) if swept else [{}]:
            try:
                problems = check(_seq_at(inst, settings, values), lim)
            except (KeyError, ValueError) as e:
                problems = [str(e.args[0] if e.args else e)]
            for p in problems:
                if p in seen:
                    continue
                seen.add(p)
                out.append("%s%s: %s" % (inst.name, " at " + _shown(values) if values else "", p))
            if len(out) >= 4:
                return out
    return out


def illustrate_plan(request: Any, lab: Any, plan: Any) -> None:
    """A run that plays a generator: its card shows the sequence at the sweep's first value (and its last), and
    what the sweep does to the period and the microwave duty cycle. Never stops the card."""
    try:
        for inst, settings, swept in _played(lab, plan):
            first = {k: v[0] for k, v in swept.items() if v}
            last = {k: v[-1] for k, v in swept.items() if v}
            try:
                seq = _seq_at(inst, settings, first)
            except (KeyError, ValueError) as e:
                request.items.append("%s's sequence can't be built: %s" % (inst.name, e.args[0] if e.args else e))
                continue
            request.items.append("%s plays %s%s" % (inst.name, summary(seq), " at " + _shown(first) if first else ""))
            request.items += ["%s %s" % (inst.name, line) for line in lines(seq)]
            request.figures.append(figure(seq, "at " + _shown(first) if first else ""))
            if swept:
                periods, duties = [], []
                for values in _combinations(swept, most=400):
                    try:
                        s = _seq_at(inst, settings, values)
                    except (KeyError, ValueError):
                        continue
                    periods.append(s.period)
                    duties.append(s.mw_duty())
                request.items.append("%s: %s swept; every value checked before the run (period %s to %s, microwave "
                                     "on up to %.1f%% of the time)" % (
                                         inst.name, ", ".join(swept), exact(min(periods)), exact(max(periods)),
                                         100 * max(duties)) if periods else "%s: %s swept" % (inst.name,
                                                                                              ", ".join(swept)))
                if last != first:
                    try:
                        request.figures.append(figure(_seq_at(inst, settings, last), "at " + _shown(last)))
                    except (KeyError, ValueError):
                        pass
    except Exception:
        return
