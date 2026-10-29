"""Figures Kelvin draws itself: curves from runs side by side or overlaid (derived quantities in their units, log axes,
error bars, fits), maps as heat maps, and a fit with its residuals; saved as PNG, SVG and PDF.

One layout, drawn three ways: PNG through pictures.Canvas (NumPy and Kelvin's bitmap font, as look_at draws), SVG as
text, and PDF with the standard Helvetica (reports/pdf.py's widths). No plotting library is needed, so every Kelvin
draws the same figure. A figure's caption says its numbers in words: they, not the pixels, are the values."""
from __future__ import annotations

import base64
import math
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .cards import _PREFIX_OF, SI_UNITS, thin
from .reports.chart import AXIS, FIT, GRID, INK, MUTED, PALETTE, scale_of, tick_labels, ticks

W, H = 960, 600          # a figure's size (pixels; points in SVG; 0.6 of it in PDF points)
FONT = 14.0
MAX_DRAWN = 3000         # a curve's points drawn (each stretch's lowest and highest kept)
MAX_LEGEND = 8           # more curves than this are colored along a scale and named by their ends
PDF_SCALE = 0.6
FORMATS = ("png", "svg", "pdf")


@dataclass
class Curve:
    xs: Any
    ys: Any
    label: str = ""
    color: str = ""
    style: str = "line"            # line, points, both, or fit (dashed, no markers)
    yerr: Any = None


@dataclass
class Heat:
    xs: Sequence[float]            # the inner axis' values (horizontal)
    ys: Sequence[float]            # the outer axis' values (vertical, increasing upward)
    z: Any                         # rows (ys) x columns (xs), NaN where nothing was measured
    label: str = ""
    unit: str = ""
    log: bool = False


@dataclass
class Panel:
    curves: List[Curve] = field(default_factory=list)
    x_label: str = ""
    x_unit: str = ""
    y_label: str = ""
    y_unit: str = ""
    x_log: bool = False
    y_log: bool = False
    x_range: Optional[Tuple[Optional[float], Optional[float]]] = None
    y_range: Optional[Tuple[Optional[float], Optional[float]]] = None
    heat: Optional[Heat] = None
    weight: float = 1.0            # its share of the figure's height
    zero_line: bool = False        # a line at y = 0 (residuals)
    x_fixed: bool = False          # the unit as asked (mV), not rescaled to another prefix
    y_fixed: bool = False


@dataclass
class Figure:
    panels: List[Panel]
    title: str = ""
    width: int = W
    height: int = H
    notes: List[str] = field(default_factory=list)   # what the caption adds (points left out, ...)

# ------------------------------------------------------------------ units


def convert(unit_from: str, unit_to: str) -> float:
    """The factor taking values in unit_from to unit_to when they differ only by an SI prefix (V -> mV: 1000).
    ValueError when they don't."""
    if unit_to == unit_from or not unit_to:
        return 1.0

    def split(u: str) -> Tuple[float, str]:
        if u in SI_UNITS:
            return 1.0, u
        if len(u) > 1 and u[0] in _PREFIX_OF and u[1:] in SI_UNITS:
            return _PREFIX_OF[u[0]], u[1:]
        if len(u) > 1 and u[0] == "u" and u[1:] in SI_UNITS:   # um, uV as people type them
            return 1e-6, u[1:]
        return 1.0, u
    fa, ba = split(unit_from)
    fb, bb = split(unit_to)
    if ba != bb or not ba:
        raise ValueError("%s can't be shown in %s" % (unit_from or "a number without a unit", unit_to))
    return fa / fb

# ------------------------------------------------------------------ axes


@dataclass
class Axis:
    lo: float
    hi: float
    log: bool
    ticks: List[float]
    labels: List[str]
    title: str

    def at(self, v: Any, start: float, length: float) -> Any:
        np = _np()
        v = np.asarray(v, dtype=float)
        if self.log:
            with np.errstate(divide="ignore", invalid="ignore"):
                f = (np.log10(v) - math.log10(self.lo)) / (math.log10(self.hi) - math.log10(self.lo))
        else:
            f = (v - self.lo) / ((self.hi - self.lo) or 1.0)
        return start + f * length


def _np() -> Any:
    import numpy
    return numpy


def axis(values: Sequence[float], unit: str, label: str, log: bool,
         limits: Optional[Tuple[Optional[float], Optional[float]]] = None, pad: bool = False, n: int = 5,
         fixed: bool = False) -> Axis:
    """An axis over these values: round ticks, one SI prefix for all of them (µV, not a mix), or decades."""
    np = _np()
    v = np.asarray([x for x in values if x is not None], dtype=float)
    v = v[np.isfinite(v) & ((v > 0) if log else True)]
    lo = float(v.min()) if v.size else (1.0 if log else 0.0)
    hi = float(v.max()) if v.size else (10.0 if log else 1.0)
    if limits:
        lo = float(limits[0]) if limits[0] is not None else lo
        hi = float(limits[1]) if limits[1] is not None else hi
    if log:
        lo, hi = max(lo, 1e-300), max(hi, 1e-300)
        if hi <= lo:
            lo, hi = lo / 10, hi * 10
        k0, k1 = math.floor(math.log10(lo) + 1e-9), math.ceil(math.log10(hi) - 1e-9)
        if k1 - k0 < 1:
            k0, k1 = k0, k0 + 1
        step = max(1, int(math.ceil((k1 - k0) / 8)))
        decades = [10.0 ** k for k in range(int(k0), int(k1) + 1, step)]
        if not limits:
            lo, hi = 10.0 ** k0, 10.0 ** k1
        shown = [d for d in decades if lo * 0.999 <= d <= hi * 1.001]
        return Axis(lo, hi, True, shown, ["%g" % d for d in shown], "%s (%s, log)" % (label, unit) if unit else
                    "%s (log)" % label)
    if hi == lo:
        d = abs(lo) * 0.05 or 1.0
        lo, hi = lo - d, hi + d
    if pad:   # room around the points (y), none on x
        d = (hi - lo) * 0.04
        lo, hi = lo - d, hi + d
    t = ticks(lo, hi, n)
    if not limits and not pad:
        t = [x for x in t if lo - 1e-9 * abs(hi - lo) <= x <= hi + 1e-9 * abs(hi - lo)] or t
    elif not limits:
        lo, hi = min(lo, t[0]), max(hi, t[-1])
    else:
        t = [x for x in t if lo - 1e-9 * abs(hi - lo) <= x <= hi + 1e-9 * abs(hi - lo)] or [lo, hi]
    factor, shown = (1.0, unit) if fixed else scale_of(t or [lo, hi], unit)
    labels = tick_labels(t, factor) if len(t) > 1 else ["%g" % (x / factor) for x in t]
    return Axis(lo, hi, False, t, labels, "%s (%s)" % (label, shown) if shown else label)

# ------------------------------------------------------------------ the layout: a list of drawing operations


def _measure(text: str, size: float) -> float:
    """Text's width, the wider of the bitmap font (PNG) and Helvetica (SVG, PDF), so it fits in all three."""
    from .pictures import measure as bitmap
    from .reports.pdf import measure as helvetica
    return max(bitmap(text, size), helvetica(text, size))


def _hex(rgb: Sequence[int]) -> str:
    return "%02x%02x%02x" % tuple(int(c) for c in rgb[:3])


def colors_for(n: int) -> List[str]:
    """n curves' colors: the palette, or a dark-to-light scale for more than it holds."""
    if n <= len(PALETTE):
        return PALETTE[:n]
    from .pictures import colormap
    np = _np()
    return [_hex(c) for c in colormap(np.linspace(0, 0.92, n))]


def _segments(np: Any, xs: Any, ys: Any, ax: Axis, ay: Axis, box: Tuple[float, float, float, float],
              line: bool) -> List[List[Tuple[float, float]]]:
    """A curve's points in the box, split where a point is missing, outside the axes, or where x jumps back (the
    next sweep of a repeat, which would otherwise draw a flyback stroke)."""
    left, top, pw, ph = box
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    if ax.log:
        ok &= x > 0
    if ay.log:
        ok &= y > 0
    xs_, ys_ = (ax.hi - ax.lo) * 1e-9, (ay.hi - ay.lo) * 1e-9
    with np.errstate(invalid="ignore"):
        ok &= (x >= ax.lo - xs_) & (x <= ax.hi + xs_) & (y >= ay.lo - ys_) & (y <= ay.hi + ys_)
    if x.size > MAX_DRAWN:
        keep = np.zeros(x.size, dtype=bool)
        keep[thin([float(v) if o else None for v, o in zip(y, ok)], MAX_DRAWN)] = True
        x, y, ok = x[keep], y[keep], ok[keep]
    px = ax.at(x, left, pw)
    py = ay.at(y, top + ph, -ph)
    breaks = np.zeros(x.size, dtype=bool)
    if line and x.size > 2:
        dx = np.diff(x)
        moving = dx[np.isfinite(dx) & (dx != 0)]
        if moving.size:
            typical, direction = float(np.median(np.abs(moving))), float(np.sign(np.median(moving)))
            with np.errstate(invalid="ignore"):
                breaks[1:] = (np.sign(dx) == -direction) & (np.abs(dx) > 3 * typical)
    out: List[List[Tuple[float, float]]] = []
    cur: List[Tuple[float, float]] = []
    for i in range(x.size):
        if not ok[i] or breaks[i]:
            if len(cur):
                out.append(cur)
            cur = []
            if not ok[i]:
                continue
        cur.append((round(float(px[i]), 2), round(float(py[i]), 2)))
    if cur:
        out.append(cur)
    return out


def layout(fig: Figure) -> Tuple[List[Tuple[Any, ...]], List[str]]:
    """The figure as drawing operations in a W x H frame (y downward), and the caption's lines."""
    np = _np()
    ops: List[Tuple[Any, ...]] = [("rect", 0, 0, fig.width, fig.height, "ffffff")]
    caption: List[str] = []
    f = FONT
    top = 10.0
    if fig.title:
        ops.append(("text", 12, top + f * 1.2, fig.title, f * 1.15, INK, "left", True))
        top += f * 2.0
    total = sum(p.weight for p in fig.panels) or 1.0
    avail = fig.height - top - 8
    y0 = top
    for n, panel in enumerate(fig.panels):
        h = avail * panel.weight / total
        caption += _panel(np, ops, panel, 12.0, y0, fig.width - 24.0, h, n == len(fig.panels) - 1)
        y0 += h
    return ops, caption + fig.notes


def _panel(np: Any, ops: List[Tuple[Any, ...]], p: Panel, x0: float, y0: float, w: float, h: float,
           last: bool) -> List[str]:
    f = FONT
    xs_all = [v for c in p.curves for v in np.asarray(c.xs, dtype=float).ravel().tolist()]
    ys_all = []
    for c in p.curves:
        ys = np.asarray(c.ys, dtype=float).ravel()
        if c.yerr is not None:
            e = np.asarray(c.yerr, dtype=float).ravel()
            ys_all += (ys - e).tolist() + (ys + e).tolist()
        ys_all += ys.tolist()
    heat = p.heat
    if heat is not None:
        hx, hy = np.asarray(heat.xs, dtype=float), np.asarray(heat.ys, dtype=float)
        dx = float(np.median(np.abs(np.diff(hx)))) / 2 if hx.size > 1 else 0.5
        dy = float(np.median(np.abs(np.diff(hy)))) / 2 if hy.size > 1 else 0.5
        xs_all += [float(hx.min()) - dx, float(hx.max()) + dx]
        ys_all += [float(hy.min()) - dy, float(hy.max()) + dy]
    ax = axis(xs_all, p.x_unit, p.x_label, p.x_log, p.x_range, fixed=p.x_fixed)
    ay = axis(ys_all, p.y_unit, p.y_label, p.y_log, p.y_range, pad=heat is None,
              n=max(2, min(5, int(h / (f * 4.5)))), fixed=p.y_fixed)
    # the legend above the plot, then the y axis' title, the plot, the x axis under it
    legend: List[Tuple[str, str, str]] = []
    named = [c for c in p.curves if c.label and c.style != "fit"]
    if len(named) > MAX_LEGEND:
        legend = [(named[0].label, named[0].color, "line"), ("…", MUTED, "none"),
                  (named[-1].label, named[-1].color, "line")]
    else:
        legend = [(c.label, c.color, c.style) for c in p.curves if c.label]
    y = y0 + 4
    if legend and (len(p.curves) > 1 or any(c.style == "fit" for c in p.curves)):
        x = x0
        for name, color, style in legend:
            need = f * 2.4 + _measure(name, f * 0.9) + f * 1.2
            if x + need > x0 + w and x > x0:
                x, y = x0, y + f * 1.4
            if style != "none":
                ops.append(("line", [(x, y + f * 0.55), (x + f * 1.8, y + f * 0.55)], color or INK, 2.0,
                            style == "fit"))
            ops.append(("text", x + f * 2.3, y + f * 0.95, name, f * 0.9, MUTED, "left", False))
            x += need
        y += f * 1.6
    ops.append(("text", x0, y + f * 0.95, ay.title, f, MUTED, "left", False))
    y += f * 2.0
    left = x0 + max([_measure(t, f) for t in ay.labels] + [f]) + f * 0.8
    right = x0 + w - f * 1.2
    if heat is not None:   # room for the color scale and its labels
        right -= f * 3.0 + max(_measure(t, f * 0.85) for t in _heat_labels(np, heat))
    bottom = y0 + h - (f * 3.6 if last else f * 2.2)
    box = (left, y, right - left, max(10.0, bottom - y))
    bl, bt, bw, bh = box
    for v, lab in zip(ay.ticks, ay.labels):
        py = float(ay.at(v, bt + bh, -bh))
        ops.append(("line", [(bl, py), (bl + bw, py)], GRID, 1.0, False))
        ops.append(("text", bl - f * 0.5, py + f * 0.4, lab, f, MUTED, "right", False))
    for v, lab in zip(ax.ticks, ax.labels):
        px = float(ax.at(v, bl, bw))
        ops.append(("line", [(px, bt), (px, bt + bh)], GRID, 1.0, False))
        ops.append(("line", [(px, bt + bh), (px, bt + bh + f * 0.4)], AXIS, 1.0, False))
        ops.append(("text", px, bt + bh + f * 1.6, lab, f, MUTED, "center", False))
    if last:   # panels above share the x axis' title
        ops.append(("text", bl + bw / 2, bt + bh + f * 3.0, ax.title, f, MUTED, "center", False))
    lines: List[str] = []
    if heat is not None:
        lines.append(_heat(np, ops, heat, ax, ay, box))
    if p.zero_line and ay.lo < 0 < ay.hi:
        py = float(ay.at(0.0, bt + bh, -bh))
        ops.append(("line", [(bl, py), (bl + bw, py)], AXIS, 1.2, True))
    ops.append(("line", [(bl, bt + bh), (bl + bw, bt + bh)], AXIS, 1.0, False))
    ops.append(("line", [(bl, bt), (bl, bt + bh)], AXIS, 1.0, False))
    for c in p.curves:
        color = c.color or INK
        line = c.style in ("line", "both", "fit")
        segs = _segments(np, c.xs, c.ys, ax, ay, box, line)
        if line:
            for seg in segs:
                ops.append(("line", seg, color, 1.6 if c.style == "fit" else 2.0, c.style == "fit"))
        pts = [pt for seg in segs for pt in seg]
        if c.style in ("points", "both") or (c.style == "line" and len(pts) <= 40):
            ops.append(("dots", pts, color, 2.6 if c.style != "points" or len(pts) < 400 else 1.6))
        if c.yerr is not None:
            x = np.asarray(c.xs, dtype=float)
            yv = np.asarray(c.ys, dtype=float)
            e = np.asarray(c.yerr, dtype=float)
            ok = np.isfinite(x) & np.isfinite(yv) & np.isfinite(e)
            if ok.sum() <= 400:
                for xi, yi, ei in zip(x[ok], yv[ok], e[ok]):
                    px = float(ax.at(xi, bl, bw))
                    lo, hi = float(ay.at(yi - ei, bt + bh, -bh)), float(ay.at(yi + ei, bt + bh, -bh))
                    if math.isfinite(lo) and math.isfinite(hi):
                        ops.append(("line", [(px, lo), (px, hi)], color, 1.2, False))
                        ops.append(("line", [(px - 3, lo), (px + 3, lo)], color, 1.2, False))
                        ops.append(("line", [(px - 3, hi), (px + 3, hi)], color, 1.2, False))
    lines.insert(0, _panel_caption(np, p, ax, ay))
    return lines


def _heat_range(np: Any, heat: Heat) -> Tuple[Any, float, float]:
    """The values as colored (log10 of them on a log scale) and their range."""
    z = np.asarray(heat.z, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        shown = np.where(z > 0, np.log10(np.where(z > 0, z, 1.0)), np.nan) if heat.log else z
    finite = shown[np.isfinite(shown)]
    lo, hi = (float(finite.min()), float(finite.max())) if finite.size else (0.0, 1.0)
    return shown, lo, hi


def _heat_labels(np: Any, heat: Heat) -> Tuple[str, str, str]:
    """The color scale's top and bottom labels, and its unit."""
    _, lo, hi = _heat_range(np, heat)
    if heat.log:
        return "%.3g" % 10 ** hi, "%.3g" % 10 ** lo, heat.unit
    factor, unit = scale_of([lo, hi], heat.unit)
    return "%.4g" % (hi / factor), "%.4g" % (lo / factor), unit


def _heat(np: Any, ops: List[Tuple[Any, ...]], heat: Heat, ax: Axis, ay: Axis, box: Tuple[float, ...]) -> str:
    from .pictures import colormap
    bl, bt, bw, bh = box
    z = np.asarray(heat.z, dtype=float)
    shown, lo, hi = _heat_range(np, heat)
    rgb = colormap((shown - lo) / ((hi - lo) or 1.0))[::-1]   # the outer axis grows upward
    hx, hy = np.asarray(heat.xs, dtype=float), np.asarray(heat.ys, dtype=float)
    dx = float(np.median(np.abs(np.diff(hx)))) / 2 if hx.size > 1 else 0.5
    dy = float(np.median(np.abs(np.diff(hy)))) / 2 if hy.size > 1 else 0.5
    x0, x1 = float(ax.at(float(hx.min()) - dx, bl, bw)), float(ax.at(float(hx.max()) + dx, bl, bw))
    y0, y1 = float(ay.at(float(hy.max()) + dy, bt + bh, -bh)), float(ay.at(float(hy.min()) - dy, bt + bh, -bh))
    ops.append(("image", x0, y0, x1 - x0, y1 - y0, rgb))
    f = FONT
    cx = bl + bw + f * 1.2
    ops.append(("image", cx, bt, f * 1.3, bh, colormap(np.linspace(1, 0, 256))[:, None, :].repeat(4, axis=1)))
    top_label, bottom_label, unit = _heat_labels(np, heat)
    if unit:
        ops.append(("text", cx, bt - f * 0.4, unit + (" (log)" if heat.log else ""), f * 0.85, MUTED, "left", False))
    ops.append(("text", cx + f * 1.6, bt + f * 0.8, top_label, f * 0.85, MUTED, "left", False))
    ops.append(("text", cx + f * 1.6, bt + bh, bottom_label, f * 0.85, MUTED, "left", False))
    missing = int(z.size - np.isfinite(z).sum())
    at = np.unravel_index(int(np.nanargmax(z)), z.shape) if np.isfinite(z).any() else None
    text = "Heat map: %s%s as color%s (dark purple %s to yellow %s), %d x %d points" % (
        heat.label, " (%s)" % heat.unit if heat.unit else "", ", log scale" if heat.log else "",
        bottom_label, top_label, hx.size, hy.size)
    if at is not None:
        text += "; largest at %s = %.6g, %s = %.6g" % (ax.title, hx[at[1]], ay.title, hy[at[0]])
    if missing:
        text += "; %d points not measured (gray)" % missing
    return text + "."


def _panel_caption(np: Any, p: Panel, ax: Axis, ay: Axis) -> str:
    real = [c for c in p.curves if c.style != "fit"]
    parts = ["%s vs %s" % (ay.title, ax.title)]
    if real:
        xs = np.concatenate([np.asarray(c.xs, dtype=float).ravel() for c in real])
        ys = np.concatenate([np.asarray(c.ys, dtype=float).ravel() for c in real])
        ok = np.isfinite(xs) & np.isfinite(ys)
        if ok.any():
            parts.append("x from %.6g to %.6g, y from %.6g to %.6g" % (
                xs[ok].min(), xs[ok].max(), ys[ok].min(), ys[ok].max()))
        if len(real) > 1:
            names = [c.label or "curve %d" % (i + 1) for i, c in enumerate(real)]
            if len(real) <= MAX_LEGEND:
                from .pictures import COLOR_NAMES
                names = ["%s %s" % (n, COLOR_NAMES[PALETTE.index(c.color)] if c.color in PALETTE else "")
                         for n, c in zip(names, real)]
                parts.append("%d curves: %s" % (len(real), ", ".join(x.strip() for x in names)))
            else:
                parts.append("%d curves colored from dark purple (%s) to yellow (%s)" % (len(real), names[0],
                                                                                        names[-1]))
    fits = [c for c in p.curves if c.style == "fit"]
    if fits:
        parts.append("dashed: %s" % ", ".join(c.label or "fit" for c in fits))
    return "Plot: " + "; ".join(parts) + "."

# ------------------------------------------------------------------ PNG, SVG, PDF


def to_png(fig: Figure) -> bytes:
    from .pictures import Canvas
    ops, _ = layout(fig)
    c = Canvas(fig.width, fig.height)
    for op in ops:
        kind = op[0]
        if kind == "rect":
            c.rect(op[1], op[2], op[3], op[4], op[5])
        elif kind == "line":
            c.line(op[1], op[2], op[3], op[4])
        elif kind == "dots":
            c.dots(op[1], op[2], op[3])
        elif kind == "image":
            c.image(op[1], op[2], op[3], op[4], op[5])
        elif kind == "text":
            c.text(op[1], op[2], op[3], op[4], op[5], op[6], op[7])
    return c.png()


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def to_svg(fig: Figure) -> bytes:
    from .pictures import png
    ops, caption = layout(fig)
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
           'font-family="Helvetica, Arial, sans-serif">' % (fig.width, fig.height, fig.width, fig.height),
           "<title>%s</title><desc>%s</desc>" % (_esc(fig.title or "Kelvin figure"), _esc(" ".join(caption)))]
    for op in ops:
        kind = op[0]
        if kind == "rect":
            out.append('<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="#%s"/>' % op[1:6])
        elif kind == "line":
            pts, color, width, dashed = op[1:5]
            if len(pts) >= 2:
                out.append('<polyline fill="none" stroke="#%s" stroke-width="%.1f" stroke-linejoin="round" '
                           'stroke-linecap="round"%s points="%s"/>' % (
                               color, width, ' stroke-dasharray="6 4"' if dashed else "",
                               " ".join("%.2f,%.2f" % p for p in pts)))
        elif kind == "dots":
            pts, color, r = op[1:4]
            out.append('<g fill="#%s">%s</g>' % (color, "".join('<circle cx="%.2f" cy="%.2f" r="%.1f"/>' % (x, y, r)
                                                               for x, y in pts)))
        elif kind == "image":
            x, y, w, h, rgb = op[1:6]
            data = base64.b64encode(png(rgb)).decode("ascii")
            out.append('<image x="%.2f" y="%.2f" width="%.2f" height="%.2f" preserveAspectRatio="none" '
                       'style="image-rendering:pixelated" href="data:image/png;base64,%s"/>' % (x, y, w, h, data))
        elif kind == "text":
            x, y, s, size, color, align, bold = op[1:8]
            anchor = {"left": "start", "center": "middle", "right": "end"}[align]
            out.append('<text x="%.2f" y="%.2f" font-size="%.1f" fill="#%s" text-anchor="%s"%s>%s</text>' % (
                x, y, size, color, anchor, ' font-weight="bold"' if bold else "", _esc(s)))
    out.append("</svg>")
    return "\n".join(out).encode("utf-8")


def to_pdf(fig: Figure) -> bytes:
    """One page the figure's size (scaled to PDF_SCALE): vector lines and Helvetica text; a heat map as an image."""
    from .reports.pdf import _esc as pdf_esc, encode, measure as helvetica
    ops, caption = layout(fig)
    s = PDF_SCALE
    pw, ph = fig.width * s, fig.height * s

    def rgb(color: str) -> str:
        return "%.3f %.3f %.3f" % tuple(int(color[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def n(v: float) -> str:
        t = "%.2f" % v
        return t.rstrip("0").rstrip(".") if "." in t else t
    body: List[bytes] = [b"q %s 0 0 %s 0 %s cm" % (n(s).encode(), n(-s).encode(), n(ph).encode())]   # y downward
    images: List[Any] = []
    for op in ops:
        kind = op[0]
        if kind == "rect":
            body.append(("%s rg %s %s %s %s re f" % (rgb(op[5]), n(op[1]), n(op[2]), n(op[3]), n(op[4]))).encode())
        elif kind == "line":
            pts, color, width, dashed = op[1:5]
            if len(pts) >= 2:
                path = " ".join("%s %s %s" % (n(x), n(y), "m" if i == 0 else "l") for i, (x, y) in enumerate(pts))
                body.append(("q %s RG %s w 1 J 1 j %s %s S Q" % (rgb(color), n(width), "[6 4] 0 d" if dashed else "",
                                                                  path)).encode())
        elif kind == "dots":
            pts, color, r = op[1:4]
            k = 0.5523 * r
            parts = []
            for x, y in pts:
                parts.append("%s %s m %s %s %s %s %s %s c %s %s %s %s %s %s c %s %s %s %s %s %s c %s %s %s %s %s %s c"
                             % (n(x + r), n(y), n(x + r), n(y + k), n(x + k), n(y + r), n(x), n(y + r),
                                n(x - k), n(y + r), n(x - r), n(y + k), n(x - r), n(y),
                                n(x - r), n(y - k), n(x - k), n(y - r), n(x), n(y - r),
                                n(x + k), n(y - r), n(x + r), n(y - k), n(x + r), n(y)))
            if parts:
                body.append(("q %s rg %s f Q" % (rgb(color), " ".join(parts))).encode())
        elif kind == "image":
            x, y, w, h, pix = op[1:6]
            images.append(pix)
            # the image's unit square, flipped back (the page's y runs downward here)
            body.append(("q %s 0 0 %s %s %s cm /Im%d Do Q" % (n(w), n(-h), n(x), n(y + h), len(images))).encode())
        elif kind == "text":
            x, y, text, size, color, align, bold = op[1:8]
            font = "F2" if bold else "F1"
            width = helvetica(text, size, "bold" if bold else "regular")
            x -= width if align == "right" else width / 2 if align == "center" else 0
            # text upright again inside the flipped frame
            body.append(b"BT /" + font.encode() + b" " + n(size).encode() + b" Tf " + rgb(color).encode() +
                        b" rg 1 0 0 -1 " + n(x).encode() + b" " + n(y).encode() + b" Tm (" + pdf_esc(encode(text)) +
                        b") Tj ET")
    body.append(b"Q")
    objects: List[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)
    catalog, pages = add(b""), add(b"")
    f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    xobjects = []
    for i, pix in enumerate(images, 1):
        a = _np().ascontiguousarray(pix, dtype=_np().uint8)
        data = zlib.compress(a.tobytes(), 6)
        num = add(b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
                  b"/BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n" % (a.shape[1], a.shape[0],
                                                                                        len(data))
                  + data + b"\nendstream")
        xobjects.append(b"/Im%d %d 0 R" % (i, num))
    content = zlib.compress(b"\n".join(body))
    stream = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(content) + content + b"\nendstream")
    page = add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %s %s] /Resources << /Font << /F1 %d 0 R /F2 %d 0 R "
               b">> /XObject << %s >> >> /Contents %d 0 R >>" % (pages, n(pw).encode(), n(ph).encode(), f1, f2,
                                                                 b" ".join(xobjects), stream))
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages
    objects[pages - 1] = b"<< /Type /Pages /Kids [%d 0 R] /Count 1 >>" % page
    info = add(b"<< /Title (" + pdf_esc(encode(fig.title or "Kelvin figure")) + b") /Creator (Kelvin) /CreationDate (D:"
               + datetime.now().strftime("%Y%m%d%H%M%S").encode() + b") >>")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, catalog, info, xref)
    return bytes(out)


RENDER: Dict[str, Callable[[Figure], bytes]] = {"png": to_png, "svg": to_svg, "pdf": to_pdf}


def caption(fig: Figure) -> str:
    return "\n".join(layout(fig)[1])


def fit_figure(result: Dict[str, Any], curve: Tuple[Sequence[float], Sequence[Any]], x_label: str, y_label: str,
               title: str = "") -> Figure:
    """A fit drawn over its points, with the residuals under it (for a complex fit: its magnitude, and the
    residuals' magnitude)."""
    np = _np()
    x = np.asarray(result["x"], dtype=float)
    y = np.asarray(result["y"], dtype=float)
    fy = np.asarray(result["fit"], dtype=float)
    res = np.asarray(result["residuals"], dtype=float)
    cx, cy = np.asarray(curve[0], dtype=float), np.asarray(curve[1], dtype=float)
    if y.ndim == 2:   # complex [re, im]: magnitudes
        y, fy, res = np.hypot(y[:, 0], y[:, 1]), np.hypot(fy[:, 0], fy[:, 1]), np.hypot(res[:, 0], res[:, 1])
        cy = np.hypot(cy[:, 0], cy[:, 1]) if cy.ndim == 2 else cy
        y_label = "|%s|" % y_label
    sig = result.get("sigma")
    points = "points" if x.size > 60 else "both"
    data = Curve(x, y, "data", PALETTE[0], points if sig is None else "points",
                 yerr=np.asarray(sig, dtype=float) if sig is not None else None)
    model = Curve(cx, cy, "%s fit" % result["model"], FIT, "fit")
    xu, yu = result.get("x_unit", ""), result.get("y_unit", "")
    top = Panel([data, model], x_label, xu, y_label, yu, weight=2.6)
    bottom = Panel([Curve(x, res, "", PALETTE[1], "points" if x.size > 60 else "both")], x_label, xu, "residuals", yu,
                   weight=1.0, zero_line=True)
    return Figure([top, bottom], title or "%s: %s fit" % (y_label, result["model"]))
