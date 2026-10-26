"""Plot geometry shared by the PDF and PowerPoint writers: nice ticks, SI-scaled axes, polylines, a legend.

Each writer draws the same Geometry with its own primitives, so a plot looks the same in both formats."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from ..cards import si_parts, thin

PALETTE = ["2a78d6", "eb6834", "1baf7a", "eda100", "e87ba4", "008300", "6250d6", "e34948"]
FIT = "3c4651"
INK = "1f2328"
MUTED = "5f6b78"
GRID = "e3e7ec"
AXIS = "9aa4ae"
MAX_POINTS = 800
MARKERS_UP_TO = 40   # points per series; more and markers only clutter

Point = Tuple[float, float]


@dataclass
class Series:
    name: str
    xs: List[Optional[float]]
    ys: List[Optional[float]]
    fit: bool = False          # a fitted model curve: drawn dashed, in the series' color (or dark if alone)
    color: str = ""


@dataclass
class Chart:
    title: str                 # the measured quantity, e.g. lockin.X
    x_key: str
    x_unit: str
    y_unit: str
    series: List[Series]
    caption: str = ""          # e.g. "lorentzian fits dashed"


@dataclass
class Geometry:
    title: str
    box: Tuple[float, float, float, float]                 # plot area x, y, w, h; y grows downward
    x_ticks: List[Tuple[float, str]]
    y_ticks: List[Tuple[float, str]]
    x_label: str
    lines: List[Tuple[str, bool, List[List[Point]]]]      # color, dashed, segments
    markers: List[Tuple[str, List[Point]]]
    legend: List[Tuple[str, str, bool, Point]]             # name, color, dashed, where (left end of the sample line)
    font: float
    caption: str = ""


def _finite(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def nice_step(span: float, n: int) -> float:
    raw = span / max(1, n)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag * (1 + 1e-9):
            return m * mag
    return 10 * mag


def ticks(lo: float, hi: float, n: int = 5) -> List[float]:
    """Round tick values covering [lo, hi]."""
    if hi < lo:
        lo, hi = hi, lo
    if hi == lo:
        pad = abs(lo) * 0.05 or 1.0
        lo, hi = lo - pad, hi + pad
    step = nice_step(hi - lo, n)
    first = math.floor(lo / step + 1e-9)
    last = math.ceil(hi / step - 1e-9)
    return [(first + i) * step for i in range(int(last - first) + 1)][:40]


def scale_of(values: Sequence[float], unit: str) -> Tuple[float, str]:
    """One divisor and shown unit for a whole axis (µV for every tick, not a mix of nV and µV)."""
    big = max((abs(v) for v in values), default=0.0)
    _, factor, shown = si_parts(big, unit) if big else (0, 1.0, unit)
    return factor, shown


def tick_labels(values: List[float], factor: float) -> List[str]:
    if len(values) < 2:
        return ["%g" % (v / factor) for v in values]
    step = abs(values[1] - values[0]) / factor
    decimals = max(0, -int(math.floor(math.log10(step) + 1e-9)))
    if abs(round(step * 10 ** decimals) - step * 10 ** decimals) > 1e-6:   # 2.5, 0.25 ...
        decimals += 1
    out = []
    for v in values:
        s = "%.*f" % (decimals, v / factor)
        out.append("0" if s.strip("-0.") == "" else s)
    return out


def axis_label(key: str, unit: str) -> str:
    return "%s (%s)" % (key, unit) if unit else key


def _thin(points: List[Optional[Point]]) -> List[Optional[Point]]:
    """At most about MAX_POINTS of them, keeping each stretch's lowest and highest (a narrow resonance stays in);
    a gap (None) stays a gap."""
    if len(points) <= MAX_POINTS:
        return points
    keep = set(thin([None if p is None else p[1] for p in points], MAX_POINTS))
    out: List[Optional[Point]] = []
    for i, p in enumerate(points):
        if p is None:
            if out and out[-1] is not None:
                out.append(None)
        elif i in keep:
            out.append(p)
    return out


def layout(chart: Chart, w: float, h: float, measure: Callable[[str, float], float], font: float = 8.0) -> Geometry:
    """Place everything inside a w x h frame whose top-left corner is (0, 0)."""
    data = [(s, [(x, y) if _finite(x) and _finite(y) else None for x, y in zip(s.xs, s.ys)]) for s in chart.series]
    xs = [p[0] for _, pts in data for p in pts if p]
    ys = [p[1] for _, pts in data for p in pts if p]
    if not xs:
        xs, ys = [0.0, 1.0], [0.0, 1.0]
    x_lo, x_hi = min(xs), max(xs)
    if x_lo == x_hi:
        x_lo, x_hi = x_lo - 1, x_hi + 1
    yt = ticks(min(ys), max(ys))
    xt = [v for v in ticks(x_lo, x_hi) if x_lo - 1e-12 * abs(x_hi - x_lo) <= v <= x_hi + 1e-12 * abs(x_hi - x_lo)]
    y_factor, y_shown = scale_of(yt, chart.y_unit)
    x_factor, x_shown = scale_of(xt or [x_lo, x_hi], chart.x_unit)
    y_labels = tick_labels(yt, y_factor)
    x_labels = tick_labels(xt, x_factor) if len(xt) > 1 else ["%g" % (v / x_factor) for v in xt]

    multi = len([s for s in chart.series if not s.fit]) > 1
    colors = []
    k = 0
    for s in chart.series:
        if s.color:
            colors.append(s.color)
        elif s.fit:
            colors.append(colors[-1] if multi and colors else FIT)
        else:
            colors.append(PALETTE[k % len(PALETTE)])
            k += 1

    title = axis_label(chart.title, y_shown)
    top = font * 1.8
    legend: List[Tuple[str, str, bool, Point]] = []
    show_legend = multi or any(s.fit for s in chart.series)
    if show_legend:
        x, y = 0.0, top + font * 0.2
        sample = font * 2.2
        for s, color in zip(chart.series, colors):
            if s.fit and multi:
                continue   # one "fit" entry is enough when every curve has its own
            name = s.name if not s.fit else "fit"
            need = sample + font * 0.5 + measure(name, font * 0.95) + font * 1.4
            if x + need > w and x > 0:
                x, y = 0.0, y + font * 1.4
            legend.append((name, color if not (s.fit and multi) else FIT, s.fit, (x, y + font * 0.55)))
            x += need
        if multi and any(s.fit for s in chart.series):
            name = "fits"
            need = font * 2.2 + font * 0.5 + measure(name, font * 0.95)
            if x + need > w and x > 0:
                x, y = 0.0, y + font * 1.4
            legend.append((name, MUTED, True, (x, y + font * 0.55)))
        top = y + font * 1.6
    left = max(measure(t, font) for t in y_labels) + font * 0.8
    right = font * 0.8 + measure(x_labels[-1], font) / 2 if x_labels else font
    bottom = font * 3.4
    pw, ph = max(10.0, w - left - right), max(10.0, h - top - bottom)

    def sx(v: float) -> float:
        return left + (v - x_lo) / (x_hi - x_lo) * pw

    def sy(v: float) -> float:
        return top + ph - (v - yt[0]) / (yt[-1] - yt[0]) * ph

    lines, markers = [], []
    for (s, pts), color in zip(data, colors):
        segments: List[List[Point]] = []
        current: List[Point] = []
        for p in _thin(pts):
            if p is None:
                if current:
                    segments.append(current)
                current = []
            else:
                current.append((round(sx(p[0]), 2), round(sy(p[1]), 2)))
        if current:
            segments.append(current)
        if not segments:
            continue
        lines.append((color, s.fit, segments))
        n = sum(len(seg) for seg in segments)
        if not s.fit and n <= MARKERS_UP_TO:
            markers.append((color, [p for seg in segments for p in seg]))
    return Geometry(title=title, box=(left, top, pw, ph),
                    x_ticks=[(round(sx(v), 2), lab) for v, lab in zip(xt, x_labels)],
                    y_ticks=[(round(sy(v), 2), lab) for v, lab in zip(yt, y_labels)],
                    x_label=axis_label(chart.x_key, x_shown), lines=lines, markers=markers, legend=legend,
                    font=font, caption=chart.caption)


@dataclass
class Table:
    """A small results table: a header row and text rows."""
    header: List[str]
    rows: List[List[str]] = field(default_factory=list)
    title: str = ""
