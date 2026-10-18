"""Pulse sequences for spin experiments (NV centres, and atoms or ions with an AOM and a microwave or RF drive): the
timing on each channel of a pulse generator, built from a named protocol and a few times, and checked before anything
plays. Pulse Streamer, PulseBlaster, an AWG's markers and the virtual lab's generator all play the same Sequence.

Channels (the wiring; the lab config names which output is which):
    laser   the AOM (or the laser's modulation input) that switches the excitation light
    mw      the microwave switch, or the source's pulse-modulation input
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
so the light reaches the sample when the sequence says."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence as Seq, Tuple

CHANNELS = ("laser", "mw", "gate")
PROTOCOLS = ("cw", "pulsed_odmr", "rabi", "ramsey", "hahn_echo", "t1")
DEFAULTS = {"t_laser": 3e-6, "t_gate": 300e-9, "t_wait": 1e-6, "t_pi": 100e-9, "tau": 100e-9, "gap": 20e-9}
LIMITS = {"resolution": 1e-9, "max_mw_duty": 0.5, "max_period": 0.1}

Window = Tuple[float, float]


@dataclass
class Sequence:
    protocol: str
    period: float
    channels: Dict[str, List[Window]]                     # on-intervals within one period, after delays
    windows: List[Tuple[str, float, float]]                # (signal|reference, start, end) counter windows
    params: Dict[str, float] = field(default_factory=dict)

    def on_time(self, channel: str) -> float:
        return sum(b - a for a, b in self.channels.get(channel, []))

    def duty(self, channel: str) -> float:
        return self.on_time(channel) / self.period if self.period else 0.0

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
        """The period in text, one row per channel (what Kelvin shows before running a sequence)."""
        rows = []
        for ch in CHANNELS:
            if ch not in self.channels:
                continue
            cells = []
            for k in range(width):
                t = (k + 0.5) * self.period / width
                cells.append("#" if any(a <= t < b for a, b in self.channels[ch]) else "_")
            rows.append("%-6s %s" % (ch, "".join(cells)))
        rows.append("%-6s 0%s%s" % ("", " " * (width - len(_si(self.period)) - 1), _si(self.period)))
        return "\n".join(rows)


def _si(seconds: float) -> str:
    for unit, scale in (("s", 1), ("ms", 1e-3), ("us", 1e-6), ("ns", 1e-9)):
        if abs(seconds) >= scale or unit == "ns":
            return "%.4g %s" % (seconds / scale, unit)
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
    for ch, lag in (delays or {}).items():   # a late channel is started early by its lag, wrapping around the period
        if lag and ch in seq.channels:
            seq.channels[ch] = _shift(seq.channels[ch], -float(lag), period)
    return seq


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
    overlap, a microwave duty cycle above the lab's limit, a period too long."""
    lim = dict(LIMITS, **(limits or {}))
    res = lim["resolution"]
    problems = []
    p = seq.params
    for name in ("t_laser", "t_gate", "t_pi"):
        if name in p and p[name] < res and not (name == "t_pi" and seq.protocol in ("cw", "t1")):
            problems.append("%s = %s is shorter than the generator's %s resolution" % (name, _si(p[name]), _si(res)))
    if p.get("tau", 0) < 0:
        problems.append("tau must not be negative")
    if seq.protocol not in ("cw",) and p.get("t_gate", 0) * 2 > p.get("t_laser", 0):
        problems.append("the signal and reference windows overlap: t_gate (%s) must be at most half of t_laser (%s)"
                        % (_si(p["t_gate"]), _si(p["t_laser"])))
    for ch, spans in seq.channels.items():
        for a, b in spans:
            if 0 < b - a < res:
                problems.append("a %s pulse of %s is shorter than the generator's %s resolution" % (ch, _si(b - a), _si(res)))
                break
    duty = seq.duty("mw")
    if duty > lim["max_mw_duty"]:
        problems.append("the microwave is on %.0f%% of the time, above the lab's %.0f%% limit (heating): lengthen "
                        "t_wait or shorten the MW pulses" % (100 * duty, 100 * lim["max_mw_duty"]))
    if seq.period > lim["max_period"]:
        problems.append("the period %s is longer than %s" % (_si(seq.period), _si(lim["max_period"])))
    return problems


def describe(seq: Sequence) -> str:
    """A short text for a person or a model: the protocol, its times, duty cycles and the diagram."""
    keys = [k for k in ("t_pi", "tau", "t_laser", "t_gate", "t_wait") if k in seq.params]
    times = ", ".join("%s %s" % (k, _si(seq.params[k])) for k in keys)
    return "%s: %s; period %s, MW duty %.1f%%, laser duty %.0f%%\n%s" % (
        seq.protocol, times, _si(seq.period), 100 * seq.duty("mw"), 100 * seq.duty("laser"), seq.diagram())


def counts_split(values: Seq[float]) -> Tuple[float, float]:
    """Counts from a counter that alternates signal and reference windows (bins 0, 2, 4... and 1, 3, 5...)."""
    return float(sum(values[0::2])), float(sum(values[1::2]))
