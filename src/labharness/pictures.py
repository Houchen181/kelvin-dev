"""Pictures for models that see: a run's plots, its 2-D maps and camera frames as PNG, so a model can judge a result
by eye (a peak's shape, noise, a jump, hysteresis, where a beam sits), as a physicist glances at a plot.

Kelvin draws them itself with NumPy, so the app needs no image or plotting library. The plots are the reports' own:
reports/chart.py lays them out and this module draws that layout into pixels, at twice the size and then averaged
down (smooth lines), with a small built-in bitmap font for the labels. Each picture comes with a caption that says
its numbers in words, for models that read only text and because the numbers, not the pixels, are the values."""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .reports.chart import AXIS, GRID, INK, MUTED, PALETTE, Chart, layout, scale_of, tick_labels, ticks

SS = 2                  # drawn at twice the size, then averaged down
MAX_SIDE = 1024         # a frame's longest side in the picture
COLOR_NAMES = ["blue", "orange", "green", "yellow", "pink", "dark green", "purple", "red"]

# ------------------------------------------------------------------ PNG


def png(pixels: Any) -> bytes:
    """A PNG of 8-bit pixels: rows x columns (gray) or rows x columns x 3 (RGB), a NumPy array."""
    import numpy as np
    a = np.ascontiguousarray(pixels, dtype=np.uint8)
    h, w = a.shape[:2]
    rgb = a.ndim == 3
    raw = np.concatenate([np.zeros((h, 1), np.uint8), a.reshape(h, -1)], axis=1).tobytes()   # filter 0 per row

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2 if rgb else 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def png_size(data: bytes) -> Tuple[int, int]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return struct.unpack(">II", data[16:24])

# ------------------------------------------------------------------ the font: 5 x 8 cells (7 rows and a descender)


_GLYPHS = {
    " ": "", "!": "00100 00100 00100 00100 00100 00000 00100", '"': "01010 01010 01010",
    "#": "01010 01010 11111 01010 11111 01010 01010", "$": "00100 01111 10100 01110 00101 11110 00100",
    "%": "11000 11001 00010 00100 01000 10011 00011", "&": "01100 10010 10100 01000 10101 10010 01101",
    "'": "01100 00100 01000", "(": "00010 00100 01000 01000 01000 00100 00010",
    ")": "01000 00100 00010 00010 00010 00100 01000", "*": "00000 00100 10101 01110 10101 00100",
    "+": "00000 00100 00100 11111 00100 00100", ",": "00000 00000 00000 00000 01100 00100 01000",
    "-": "00000 00000 00000 11111", ".": "00000 00000 00000 00000 00000 01100 01100",
    "/": "00000 00001 00010 00100 01000 10000", ":": "00000 01100 01100 00000 01100 01100",
    ";": "00000 01100 01100 00000 01100 00100 01000", "<": "00010 00100 01000 10000 01000 00100 00010",
    "=": "00000 00000 11111 00000 11111", ">": "01000 00100 00010 00001 00010 00100 01000",
    "?": "01110 10001 00001 00010 00100 00000 00100", "@": "01110 10001 00001 01101 10101 10101 01110",
    "0": "01110 10001 10011 10101 11001 10001 01110", "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111", "3": "11111 00010 00100 00010 00001 10001 01110",
    "4": "00010 00110 01010 10010 11111 00010 00010", "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110", "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110", "9": "01110 10001 10001 01111 00001 00010 01100",
    "A": "01110 10001 10001 10001 11111 10001 10001", "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01110 10001 10000 10000 10000 10001 01110", "D": "11100 10010 10001 10001 10001 10010 11100",
    "E": "11111 10000 10000 11110 10000 10000 11111", "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01110 10001 10000 10111 10001 10001 01111", "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "01110 00100 00100 00100 00100 00100 01110", "J": "00111 00010 00010 00010 00010 10010 01100",
    "K": "10001 10010 10100 11000 10100 10010 10001", "L": "10000 10000 10000 10000 10000 10000 11111",
    "M": "10001 11011 10101 10101 10001 10001 10001", "N": "10001 10001 11001 10101 10011 10001 10001",
    "O": "01110 10001 10001 10001 10001 10001 01110", "P": "11110 10001 10001 11110 10000 10000 10000",
    "Q": "01110 10001 10001 10001 10101 10010 01101", "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110", "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110", "V": "10001 10001 10001 01010 01010 00100 00100",
    "W": "10001 10001 10001 10101 10101 10101 01010", "X": "10001 10001 01010 00100 01010 10001 10001",
    "Y": "10001 10001 10001 01010 00100 00100 00100", "Z": "11111 00001 00010 00100 01000 10000 11111",
    "[": "01110 01000 01000 01000 01000 01000 01110", "\\": "00000 10000 01000 00100 00010 00001",
    "]": "01110 00010 00010 00010 00010 00010 01110", "^": "00100 01010 10001",
    "_": "00000 00000 00000 00000 00000 00000 11111", "`": "01000 00100 00010",
    "a": "00000 00000 01110 00001 01111 10001 01111", "b": "10000 10000 10110 11001 10001 10001 11110",
    "c": "00000 00000 01110 10000 10000 10001 01110", "d": "00001 00001 01101 10011 10001 10001 01111",
    "e": "00000 00000 01110 10001 11111 10000 01110", "f": "00110 01001 01000 11100 01000 01000 01000",
    "g": "00000 00000 01111 10001 10001 01111 00001 01110", "h": "10000 10000 10110 11001 10001 10001 10001",
    "i": "00100 00000 01100 00100 00100 00100 01110", "j": "00010 00000 00110 00010 00010 00010 10010 01100",
    "k": "10000 10000 10010 10100 11000 10100 10010", "l": "01100 00100 00100 00100 00100 00100 01110",
    "m": "00000 00000 11010 10101 10101 10001 10001", "n": "00000 00000 10110 11001 10001 10001 10001",
    "o": "00000 00000 01110 10001 10001 10001 01110", "p": "00000 00000 11110 10001 10001 11110 10000 10000",
    "q": "00000 00000 01111 10001 10001 01111 00001 00001", "r": "00000 00000 10110 11001 10000 10000 10000",
    "s": "00000 00000 01110 10000 01110 00001 11110", "t": "01000 01000 11100 01000 01000 01001 00110",
    "u": "00000 00000 10001 10001 10001 10011 01101", "v": "00000 00000 10001 10001 10001 01010 00100",
    "w": "00000 00000 10001 10001 10101 10101 01010", "x": "00000 00000 10001 01010 00100 01010 10001",
    "y": "00000 00000 10001 10001 10001 01111 00001 01110", "z": "00000 00000 11111 00010 00100 01000 11111",
    "{": "00010 00100 00100 01000 00100 00100 00010", "|": "00100 00100 00100 00100 00100 00100 00100",
    "}": "01000 00100 00100 00010 00100 00100 01000", "~": "00000 00000 01000 10101 00010",
    "µ": "00000 00000 10001 10001 10001 10011 11101 10000", "°": "01100 10010 10010 01100",
    "Ω": "01110 10001 10001 10001 01010 01010 11011", "±": "00100 00100 11111 00100 00100 00000 11111",
    "²": "01100 10010 00100 01000 11110", "³": "11100 00010 01100 00010 11100", "·": "00000 00000 00000 01100 01100",
    "×": "00000 10001 01010 00100 01010 10001", "Δ": "00100 00100 01010 01010 10001 10001 11111",
    "…": "00000 00000 00000 00000 00000 10101 10101",
}
_SAME = {"μ": "µ", "–": "-", "—": "-", "−": "-", "’": "'", "‘": "'", "“": '"', "”": '"', " ": " ", "Å": "A"}
_FONT: Dict[str, Any] = {}


def _glyph(c: str) -> Any:
    import numpy as np
    c = _SAME.get(c, c)
    if c not in _GLYPHS:
        c = "?"
    if c not in _FONT:
        rows = [r for r in _GLYPHS[c].split()] + ["00000"] * 8
        _FONT[c] = np.array([[ch == "1" for ch in r] for r in rows[:8]], dtype=bool)
    return _FONT[c]


def _scale(size: float) -> int:
    return max(1, int(round(size / 7.0)))


def measure(text: str, size: float) -> float:
    """The width of text in pixels at this size (a cell is 6 x 8 font pixels; size ~ the capitals' height)."""
    return len(str(text)) * 6 * _scale(size)

# ------------------------------------------------------------------ drawing


def _rgb(color: str) -> Tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


class Canvas:
    """Pixels at SS times the size of the picture; coordinates are in the picture's pixels."""

    def __init__(self, w: int, h: int, background: str = "ffffff"):
        import numpy as np
        self.np = np
        self.w, self.h = int(w), int(h)
        self.px = np.empty((self.h * SS, self.w * SS, 3), np.uint8)
        self.px[:] = _rgb(background)

    def _stamp(self, xs: Any, ys: Any, radius: float, color: str) -> None:
        """Discs of this radius (picture pixels) at these points (canvas pixels)."""
        np = self.np
        r = max(0.5, radius * SS)
        k = int(math.ceil(r))
        dy, dx = np.mgrid[-k:k + 1, -k:k + 1]
        inside = dx * dx + dy * dy <= r * r + 0.25
        dx, dy = dx[inside], dy[inside]
        xi = np.rint(xs).astype(np.int64)[:, None] + dx[None, :]
        yi = np.rint(ys).astype(np.int64)[:, None] + dy[None, :]
        ok = (xi >= 0) & (xi < self.px.shape[1]) & (yi >= 0) & (yi < self.px.shape[0])
        self.px[yi[ok], xi[ok]] = _rgb(color)

    def line(self, points: Sequence[Tuple[float, float]], color: str, width: float = 1.0,
             dashed: bool = False) -> None:
        np = self.np
        p = np.asarray(points, dtype=float) * SS
        if len(p) == 0:
            return
        if len(p) == 1:
            self._stamp(p[:, 0], p[:, 1], width / 2, color)
            return
        seg = np.diff(p, axis=0)
        lengths = np.hypot(seg[:, 0], seg[:, 1])
        n = np.maximum(1, np.ceil(lengths / 0.7).astype(np.int64))      # a sample every 0.7 canvas pixels
        if n.sum() > 4_000_000:   # a huge noisy trace: coarser samples rather than a stall
            n = np.maximum(1, (n * 4_000_000 // n.sum()))
        idx = np.repeat(np.arange(len(seg)), n)
        frac = (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)) / np.repeat(n, n)
        xs = p[idx, 0] + seg[idx, 0] * frac
        ys = p[idx, 1] + seg[idx, 1] * frac
        xs, ys = np.append(xs, p[-1, 0]), np.append(ys, p[-1, 1])
        if dashed:
            along = np.append(np.cumsum(np.repeat(lengths / n, n)) - np.repeat(lengths / n, n), lengths.sum())
            keep = (along % (9.0 * SS)) < 5.5 * SS
            xs, ys = xs[keep], ys[keep]
        self._stamp(xs, ys, width / 2, color)

    def dots(self, points: Sequence[Tuple[float, float]], color: str, radius: float = 2.6) -> None:
        if points:
            p = self.np.asarray(points, dtype=float) * SS
            self._stamp(p[:, 0], p[:, 1], radius, color)

    def rect(self, x: float, y: float, w: float, h: float, color: str) -> None:
        x0, y0 = int(round(x * SS)), int(round(y * SS))
        x1, y1 = int(round((x + w) * SS)), int(round((y + h) * SS))
        self.px[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = _rgb(color)

    def image(self, x: float, y: float, w: float, h: float, rgb: Any) -> None:
        """Place an RGB array into this box, scaled to fill it (nearest pixel: every cell stays a cell)."""
        np = self.np
        x0, y0 = int(round(x * SS)), int(round(y * SS))
        x1, y1 = int(round((x + w) * SS)), int(round((y + h) * SS))
        if x1 <= x0 or y1 <= y0:
            return
        rows = (np.arange(y1 - y0) * rgb.shape[0] // (y1 - y0)).clip(0, rgb.shape[0] - 1)
        cols = (np.arange(x1 - x0) * rgb.shape[1] // (x1 - x0)).clip(0, rgb.shape[1] - 1)
        self.px[y0:y1, x0:x1] = rgb[rows][:, cols]

    def text(self, x: float, y: float, text: str, size: float, color: str = INK, align: str = "left",
             bold: bool = False) -> None:
        """Text with its baseline at y; align left, center or right of x."""
        text = str(text)
        s = _scale(size) * SS
        width = len(text) * 6 * s
        x0 = x * SS - (width / 2 if align == "center" else width if align == "right" else 0)
        top = int(round(y * SS)) - 7 * s
        rgb = _rgb(color)
        H, W = self.px.shape[:2]
        for i, c in enumerate(text):
            g = _glyph(c)
            if not g.any():
                continue
            big = g.repeat(s, 0).repeat(s, 1)
            gx = int(round(x0 + i * 6 * s))
            for dx in ((0, s // 2) if bold else (0,)):
                ys, xs = self.np.nonzero(big)
                ys, xs = ys + top, xs + gx + dx
                ok = (xs >= 0) & (xs < W) & (ys >= 0) & (ys < H)
                self.px[ys[ok], xs[ok]] = rgb

    def png(self) -> bytes:
        a = self.px.reshape(self.h, SS, self.w, SS, 3).mean(axis=(1, 3))
        return png(self.np.rint(a))

# ------------------------------------------------------------------ plots (the reports' layout, in pixels)


FONT = 14.0


def draw_chart(c: Canvas, chart: Chart, x0: float, y0: float, w: float, h: float) -> None:
    f = FONT
    c.text(x0, y0 + f * 1.15, layout(chart, w, h, measure, f).title, f * 1.1, INK, bold=True)
    y0, h = y0 + f * 0.8, h - f * 0.8   # this font's capitals are taller than the reports': room under the title
    geo = layout(chart, w, h, measure, f)
    left, top, pw, ph = geo.box
    for name, color, dashed, (lx, ly) in geo.legend:
        c.line([(x0 + lx, y0 + ly), (x0 + lx + f * 2.2, y0 + ly)], color, 2.0, dashed)
        c.text(x0 + lx + f * 2.7, y0 + ly + f * 0.4, name, f * 0.95, MUTED)
    for y, label in geo.y_ticks:
        c.line([(x0 + left, y0 + y), (x0 + left + pw, y0 + y)], GRID, 1.0)
        c.text(x0 + left - f * 0.5, y0 + y + f * 0.4, label, f, MUTED, align="right")
    c.line([(x0 + left, y0 + top + ph), (x0 + left + pw, y0 + top + ph)], AXIS, 1.0)
    for x, label in geo.x_ticks:
        c.line([(x0 + x, y0 + top + ph), (x0 + x, y0 + top + ph + f * 0.4)], AXIS, 1.0)
        c.text(x0 + x, y0 + top + ph + f * 1.6, label, f, MUTED, align="center")
    c.text(x0 + left + pw / 2, y0 + top + ph + f * 3.1, geo.x_label, f, MUTED, align="center")
    for color, dashed, segments in geo.lines:
        for seg in segments:
            c.line([(x0 + x, y0 + y) for x, y in seg], color, 1.6 if dashed else 2.0, dashed)
    for color, pts in geo.markers:
        c.dots([(x0 + x, y0 + y) for x, y in pts], color)


def charts_png(charts: Sequence[Chart]) -> bytes:
    """One plot, two stacked, or a grid of two columns: each as the reports draw it."""
    charts = list(charts)[:6]
    if not charts:
        raise ValueError("nothing to plot")
    if len(charts) == 1:
        cols, cw, ch = 1, 960, 560
    elif len(charts) == 2:
        cols, cw, ch = 1, 960, 430
    else:
        cols, cw, ch = 2, 800, 470
    rows = -(-len(charts) // cols)
    pad = 24
    c = Canvas(cols * cw, rows * ch)
    for i, chart in enumerate(charts):
        r, k = divmod(i, cols)
        draw_chart(c, chart, k * cw + pad, r * ch + pad * 0.6, cw - 2 * pad, ch - pad * 1.4)
    return c.png()


def _range(values: Sequence[Any]) -> Tuple[Optional[float], Optional[float]]:
    v = [float(x) for x in values if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)]
    return (min(v), max(v)) if v else (None, None)


def _num(v: float, unit: str = "") -> str:
    from .cards import si
    try:
        return si(v, unit) if unit else "%.4g" % v
    except Exception:
        return "%.4g%s" % (v, " " + unit if unit else "")


def charts_caption(charts: Sequence[Chart]) -> str:
    """The plots in words: axes, ranges, series and their colors, as the picture shows them."""
    lines = []
    many = len(charts) > 1
    for n, ch in enumerate(charts, 1):
        where = ("Plot %d%s: " % (n, " (%s)" % _place(n, len(charts)) if many else "")) if many else "Plot: "
        xs = [x for s in ch.series if not s.fit for x in s.xs]
        ys = [y for s in ch.series if not s.fit for y in s.ys]
        (x0, x1), (y0, y1) = _range(xs), _range(ys)
        points = max((len(s.xs) for s in ch.series if not s.fit), default=0)
        text = "%s%s%s vs %s%s" % (where, ch.title, " (%s)" % ch.y_unit if ch.y_unit else "", ch.x_key,
                                   " (%s)" % ch.x_unit if ch.x_unit else "")
        if x0 is not None and y0 is not None:
            text += "; x from %s to %s, y from %s to %s" % (_num(x0, ch.x_unit), _num(x1, ch.x_unit),
                                                            _num(y0, ch.y_unit), _num(y1, ch.y_unit))
        real = [s for s in ch.series if not s.fit]
        if len(real) > 1:
            colors = []
            k = 0
            for s in ch.series:
                if s.fit:
                    continue
                name = COLOR_NAMES[PALETTE.index(s.color)] if s.color in PALETTE else COLOR_NAMES[k % len(PALETTE)]
                colors.append("%s %s" % (s.name, name))
                k += 1
            text += "; %d curves: %s" % (len(real), ", ".join(colors))
        text += "; %d points%s" % (points, " per curve" if len(real) > 1 else "")
        if ch.caption:
            text += ". " + ch.caption.rstrip(".")
        lines.append(text + ".")
    return "\n".join(lines)


def _place(n: int, total: int) -> str:
    if total == 2:
        return "top" if n == 1 else "bottom"
    r, k = divmod(n - 1, 2)
    return "row %d, %s" % (r + 1, "left" if k == 0 else "right")

# ------------------------------------------------------------------ maps (a measured value over two swept axes)


# a perceptually even dark-to-light scale (viridis' anchors): the color says the value, colorblind-safe
_MAP = ["440154", "482475", "414487", "355f8d", "2a788e", "21918c", "22a884", "44bf70", "7ad151", "bddf26", "fde725"]


def colormap(t: Any) -> Any:
    """Values 0..1 (NaN: gray) to RGB."""
    import numpy as np
    anchors = np.array([_rgb(c) for c in _MAP], dtype=float)
    t = np.asarray(t, dtype=float)
    bad = ~np.isfinite(t)
    pos = np.clip(np.nan_to_num(t), 0, 1) * (len(_MAP) - 1)
    i = np.minimum(pos.astype(int), len(_MAP) - 2)
    f = (pos - i)[..., None]
    out = anchors[i] * (1 - f) + anchors[i + 1] * f
    out[bad] = (200, 200, 200)
    return np.rint(out).astype(np.uint8)


def grid_of(rows: Sequence[Dict[str, Any]], outer: str, inner: str, key: str) -> Optional[Tuple[List[float], List[float], Any]]:
    """A run's points as a grid: outer values (rows), inner values (columns), the key's value at each (NaN if
    missing). None when the points do not form a grid of numbers."""
    import numpy as np
    def number(v: Any) -> Optional[float]:
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None
    ov: Dict[float, int] = {}
    iv: Dict[float, int] = {}
    cells = []
    for r in rows:
        o, i, z = number(r.get(outer)), number(r.get(inner)), number(r.get(key))
        if o is None or i is None:
            return None
        ov.setdefault(o, len(ov))
        iv.setdefault(i, len(iv))
        cells.append((o, i, z))
    if len(ov) < 2 or len(iv) < 2:
        return None
    outs, ins = sorted(ov), sorted(iv)
    oi, ii = {v: k for k, v in enumerate(outs)}, {v: k for k, v in enumerate(ins)}
    z = np.full((len(outs), len(ins)), np.nan)
    for o, i, v in cells:   # a repeated point keeps its latest value
        if v is not None:
            z[oi[o], ii[i]] = v
    return outs, ins, z


def map_png(title: str, outs: Sequence[float], ins: Sequence[float], z: Any, x_key: str, x_unit: str, y_key: str,
            y_unit: str, z_unit: str) -> bytes:
    """The value as color over the inner axis (horizontal) and the outer axis (vertical, increasing upward)."""
    import numpy as np
    W, H = 960, 640
    f = FONT
    c = Canvas(W, H)
    finite = z[np.isfinite(z)]
    lo, hi = (float(finite.min()), float(finite.max())) if finite.size else (0.0, 1.0)
    span = hi - lo or 1.0
    xt = [v for v in ticks(min(ins), max(ins)) if min(ins) <= v <= max(ins)]
    yt = [v for v in ticks(min(outs), max(outs)) if min(outs) <= v <= max(outs)]
    xf, xs_unit = scale_of(xt or list(ins), x_unit)
    yf, ys_unit = scale_of(yt or list(outs), y_unit)
    zf, zs_unit = scale_of([lo, hi], z_unit)
    ylabels = tick_labels(yt, yf) if len(yt) > 1 else ["%g" % (v / yf) for v in yt]
    xlabels = tick_labels(xt, xf) if len(xt) > 1 else ["%g" % (v / xf) for v in xt]
    left = max([measure(t, f) for t in ylabels] + [0]) + f * 1.2 + 24
    top, bottom, right = f * 4.4, f * 4.0, 150
    pw, ph = W - left - right, H - top - bottom
    c.text(8, f * 1.5, "%s%s" % (title, " (%s)" % zs_unit if zs_unit else ""), f * 1.1, INK, bold=True)
    rgb = colormap((z - lo) / span)[::-1]   # the outer axis grows upward
    c.image(left, top, pw, ph, rgb)
    x_lo, x_hi = min(ins), max(ins)
    y_lo, y_hi = min(outs), max(outs)
    for v, lab in zip(xt, xlabels):
        x = left + (v - x_lo) / ((x_hi - x_lo) or 1) * pw
        c.line([(x, top + ph), (x, top + ph + f * 0.4)], AXIS, 1.0)
        c.text(x, top + ph + f * 1.6, lab, f, MUTED, align="center")
    for v, lab in zip(yt, ylabels):
        y = top + ph - (v - y_lo) / ((y_hi - y_lo) or 1) * ph
        c.line([(left - f * 0.4, y), (left, y)], AXIS, 1.0)
        c.text(left - f * 0.6, y + f * 0.4, lab, f, MUTED, align="right")
    c.text(left + pw / 2, top + ph + f * 3.2, "%s%s" % (x_key, " (%s)" % xs_unit if xs_unit else ""), f, MUTED,
           align="center")
    c.text(8, top - f * 0.6, "%s%s" % (y_key, " (%s)" % ys_unit if ys_unit else ""), f, MUTED)
    bx = left + pw + 28   # the color scale, with its ends labeled
    c.image(bx, top, 22, ph, colormap(np.linspace(1, 0, 256))[:, None, :].repeat(4, axis=1))
    c.text(bx + 30, top + f * 0.8, "%.4g" % _snap(hi / zf, lo / zf), f, MUTED)
    c.text(bx + 30, top + ph, "%.4g" % _snap(lo / zf, hi / zf), f, MUTED)
    return c.png()


def _snap(v: float, other: float) -> float:
    """0 for a value that is only rounding noise next to the other end of a range."""
    return 0.0 if abs(v) < 1e-9 * abs(other) else v


def map_caption(title: str, outs: Sequence[float], ins: Sequence[float], z: Any, x_key: str, x_unit: str,
                y_key: str, y_unit: str, z_unit: str) -> str:
    import numpy as np
    finite = z[np.isfinite(z)]
    missing = int(z.size - finite.size)
    lo, hi = (float(finite.min()), float(finite.max())) if finite.size else (float("nan"),) * 2
    at = np.unravel_index(int(np.nanargmax(z)), z.shape) if finite.size else None
    text = ("Map: %s as color over %s (horizontal, %s to %s) and %s (vertical, increasing upward, %s to %s): "
            "%d x %d points; dark purple %s to yellow %s" % (
                title, x_key, _num(min(ins), x_unit), _num(max(ins), x_unit), y_key, _num(min(outs), y_unit),
                _num(max(outs), y_unit), len(ins), len(outs), _num(_snap(lo, hi), z_unit), _num(_snap(hi, lo), z_unit)))
    if at is not None:
        text += "; largest at %s = %s, %s = %s" % (x_key, _num(ins[at[1]], x_unit), y_key, _num(outs[at[0]], y_unit))
    if missing:
        text += "; %d points not measured (gray)" % missing
    return text + "."

# ------------------------------------------------------------------ camera frames


def load_frame(path: Any) -> Any:
    import numpy as np
    p = Path(str(path))
    if p.suffix.lower() != ".npy":
        raise ValueError("a frame is a .npy file (Kelvin saves each one so)")
    return np.load(str(p), allow_pickle=False)


def frame_png(frame: Any) -> Tuple[bytes, str]:
    """A camera frame as a picture (gray, or its own colors) with the contrast stretched between its 0.5th and
    99.95th percentiles, at most MAX_SIDE pixels on its longest side; and its numbers in words."""
    import numpy as np
    a = np.asarray(frame)
    if a.ndim == 3 and a.shape[2] in (3, 4):
        color = True
        a = a[:, :, :3]
    elif a.ndim == 3 and a.shape[0] in (3, 4):   # channels first
        color = True
        a = np.moveaxis(a[:3], 0, 2)
    elif a.ndim == 2:
        color = False
    else:
        raise ValueError("a frame is a 2-D image (or one with 3 color channels); this one is %s" % (a.shape,))
    h, w = a.shape[:2]
    values = a.astype(float)
    finite = values[np.isfinite(values)]
    if not finite.size:
        raise ValueError("the frame holds no numbers")
    lo, hi = np.percentile(finite, [0.5, 99.95])
    if hi <= lo:
        lo, hi = float(finite.min()), float(finite.max()) or 1.0
    shown = np.clip((np.nan_to_num(values, nan=lo) - lo) / ((hi - lo) or 1.0), 0, 1) * 255
    step = max(1, int(math.ceil(max(h, w) / MAX_SIDE)))
    if step > 1:   # average step x step blocks: no detail is lost to aliasing
        hh, ww = h // step * step, w // step * step
        shown = shown[:hh, :ww].reshape(hh // step, step, ww // step, step, *shown.shape[2:]).mean(axis=(1, 3))
    data = png(np.rint(shown))
    gray = values if not color else values.mean(axis=2)
    gray = np.nan_to_num(gray, nan=float(np.nanmin(gray)) if np.isfinite(gray).any() else 0.0)
    y, x = np.unravel_index(int(np.argmax(gray)), gray.shape)
    floor = float(np.percentile(gray, 50))
    weights = np.where(gray >= floor + (float(gray.max()) - floor) / 2, gray - floor, 0.0)   # the brightest part
    total = float(weights.sum())
    text = "Frame: %d x %d pixels (width x height)%s, %s; min %.4g, mean %.4g, max %.4g; brightest pixel at x=%d, " \
           "y=%d (from the top left)" % (w, h, ", color" if color else "", a.dtype, float(finite.min()),
                                          float(finite.mean()), float(finite.max()), x, y)
    if total > 0:
        yy, xx = np.mgrid[0:gray.shape[0], 0:gray.shape[1]]
        text += "; the part above half the maximum is centered at x=%.1f, y=%.1f" % (
            float((xx * weights).sum() / total), float((yy * weights).sum() / total))
    text += "; contrast stretched from %.4g (black) to %.4g (white)" % (lo, hi)
    if step > 1:
        text += "; shown at 1/%d size" % step
    return data, text + "."
