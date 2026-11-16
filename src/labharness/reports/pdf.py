"""A small PDF writer and the reports laid out in it.

The look is the one the user chose on 2026-11-15 (option 14 of the PDF styles, after the one-pager of Kami,
tw93/Kami, MIT): parchment paper, one ink-blue accent, Times throughout with no bold and no italic. First the finding
and its key numbers, then each plot with its result and its numbers under it, then what's next; the record
(conditions, approvals, plans, the lab log) after that. Text uses the PDF standard fonts (Times, Courier, and Symbol
for Ω; Helvetica for the plots Kelvin exports), so nothing has to be embedded or installed; plots are vector lines.
No third-party package is needed, so reports work in every Kelvin install."""
from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .chart import (Chart, Table, _finite, _thin, axis_label, matching, picks, plot_title, scale_of, tick_labels,
                    ticks)
from .model import Report, RunPart

# Advance widths (1/1000 em) of characters 32-126 in Helvetica and Helvetica-Bold (the plots Kelvin exports).
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278] + [556] * 10 + [
    278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556,
    278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334,
    260, 334, 584]
_HELV_B = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278] + [556] * 10 + [
    333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556, 333, 556, 611, 556, 611, 556,
    333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389,
    280, 389, 584]
_HIGH = {0x85: 1000, 0x91: 222, 0x92: 222, 0x93: 333, 0x94: 333, 0x95: 350, 0x96: 556, 0x97: 1000, 0xB0: 400,
         0xB1: 584, 0xB2: 333, 0xB3: 333, 0xB5: 556, 0xB7: 278, 0xD7: 584}
# Times-Roman, the reports' one face: characters 32-126, then the WinAnsi ones above 127 (Adobe's metrics).
_TIMES = ([250, 333, 408, 500, 500, 833, 778, 180, 333, 333, 500, 564, 250, 333, 250, 278] + [500] * 10
          + [278, 278, 564, 564, 564, 444, 921]
          + [722, 667, 667, 722, 611, 556, 722, 722, 333, 389, 722, 611, 889, 722, 722, 556, 722, 667, 556, 611,
             722, 722, 944, 722, 722, 611]
          + [333, 278, 333, 469, 500, 333]
          + [444, 500, 444, 500, 444, 333, 500, 500, 278, 278, 500, 278, 778, 500, 500, 500, 500, 333, 389, 278,
             500, 500, 722, 500, 500, 444]
          + [480, 200, 480, 541])
_TIMES_HIGH = {0x80: 500, 0x82: 333, 0x83: 500, 0x84: 444, 0x85: 1000, 0x86: 500, 0x87: 500, 0x88: 333, 0x89: 1000,
               0x8A: 556, 0x8B: 333, 0x8C: 889, 0x8E: 611, 0x91: 333, 0x92: 333, 0x93: 444, 0x94: 444, 0x95: 350,
               0x96: 500, 0x97: 1000, 0x98: 333, 0x99: 980, 0x9A: 389, 0x9B: 333, 0x9C: 722, 0x9E: 444, 0x9F: 722,
               0xA0: 250, 0xA1: 333, 0xA6: 200, 0xA8: 333, 0xA9: 760, 0xAA: 276, 0xAC: 564, 0xAD: 333, 0xAE: 760,
               0xAF: 333, 0xB0: 400, 0xB1: 564, 0xB2: 300, 0xB3: 300, 0xB4: 333, 0xB6: 453, 0xB7: 250, 0xB8: 333,
               0xB9: 300, 0xBA: 310, 0xBC: 750, 0xBD: 750, 0xBE: 750, 0xBF: 444, 0xC6: 889, 0xC7: 667, 0xD7: 564,
               0xDE: 556, 0xDF: 500, 0xE6: 667, 0xF7: 564}
for _c in range(0xC0, 0x100):   # accented letters are as wide as their plain ones
    _TIMES_HIGH.setdefault(_c, (611 if 0xC8 <= _c <= 0xCB else 333 if 0xCC <= _c <= 0xCF else 722) if _c < 0xE0
                           else 444 if _c <= 0xEB else 278 if _c <= 0xEF else 500)
FONTS = {"regular": ("F1", "Helvetica", _HELV), "bold": ("F2", "Helvetica-Bold", _HELV_B),
         "mono": ("F3", "Courier", None), "italic": ("F4", "Helvetica-Oblique", _HELV),
         "serif": ("F5", "Times-Roman", _TIMES)}
SERIF = "serif"
SYMBOL = "F6"            # the Symbol font: Ω in Times text (WinAnsi has none; Symbol's Omega is its W, 768 wide)
OMEGA = "\u03a9\u2126"
# What the standard fonts cannot show, spelled out.
_SPELL = {"\u03a9": "Ohm", "\u2126": "Ohm", "\u2192": "->", "\u2190": "<-", "\u2194": "<->", "\u2248": "~",
          "\u2264": "<=", "\u2265": ">=", "\u221a": "sqrt", "\u221e": "inf", "\u0394": "Delta ", "\u03b1": "alpha",
          "\u03b2": "beta", "\u03b3": "gamma", "\u03b4": "delta", "\u03b8": "theta", "\u03bb": "lambda",
          "\u03bc": "\u00b5", "\u03c0": "pi", "\u03c3": "sigma", "\u03c4": "tau", "\u03c6": "phi", "\u03c9": "omega",
          "\u2212": "-", "\u2009": " ", "\u202f": " ", "\u00a0": " ", "\u2011": "-", "\u2713": "ok", "\u2717": "x"}

# Kami's colours (references/tokens.json): parchment paper, warm greys, one ink-blue accent.
PAPER, IVORY, CODE_BG = "f5f4ed", "faf9f5", "f0eee6"
BRAND, TAG_BG = "1b365d", "e4ecf5"
NEAR_BLACK, DARK_WARM, CHARCOAL, OLIVE, STONE = "141413", "3d3d3a", "4d4c48", "504e49", "6b6a64"
LIGHT_STONE, BORDER, LINE, GRID = "b8b7b0", "e8e6dc", "d8d5c8", "e8e7e1"
ALERT = "8b4513"         # Kami's warning brown: a stopped run
SERIES = [BRAND, OLIVE, STONE, LIGHT_STONE, ALERT, CHARCOAL]   # Kami's order, then two warm darks for more curves
INK, MUTED = NEAR_BLACK, STONE

PAGE_W, PAGE_H = 612.0, 792.0      # US Letter, points
MARGIN = 51.0                      # left and right: 18 mm, as Kami's one-pager
TOP = 43.0                         # top and bottom: 15 mm
WIDTH = PAGE_W - 2 * MARGIN
FOOT = 27.0                        # every page's foot: a dotted rule and a line
BOTTOM = PAGE_H - TOP - FOOT       # where a page's content ends
GAP = 18.0                         # between the two columns, and between blocks
COL = (WIDTH - GAP) / 2


def encode(text: str) -> bytes:
    return "".join(_SPELL.get(c, c) for c in str(text)).encode("cp1252", "replace")


def _units(text: str, font: str) -> int:
    """The text's advance width in 1/1000 em."""
    if font == SERIF and any(o in text for o in OMEGA):   # each Ω is Symbol's Omega
        rest = "".join(ch for ch in text if ch not in OMEGA)
        return (len(text) - len(rest)) * 768 + _units(rest, font)
    widths = FONTS[font][2]
    if widths is None:
        return len(encode(text)) * 600
    high, other = (_TIMES_HIGH, 500) if font == SERIF else (_HIGH, 556)
    total = 0
    for c in encode(text):
        total += widths[c - 32] if 32 <= c < 127 else high.get(c, other)
    return total


def measure(text: str, size: float, font: str = "regular", spacing: float = 0.0) -> float:
    """The width in points; spacing: letter-spacing, added after every character (PDF's Tc)."""
    text = str(text)
    width = _units(text, font) * size / 1000
    if spacing:
        width += spacing * sum(1 if font == SERIF and ch in OMEGA else len(encode(ch)) for ch in text)
    return width


def _esc(b: bytes) -> bytes:
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)").replace(b"\r", b"").replace(b"\n", b" ")


def _rgb(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    return "%.3f %.3f %.3f" % tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _n(v: float) -> str:
    s = "%.2f" % v
    return s.rstrip("0").rstrip(".") if "." in s else s


def _parts(text: str, font: str) -> List[Tuple[str, bytes]]:
    """The text as (font resource, bytes) runs: in Times text each Ω is Symbol's Omega."""
    code = FONTS[font][0]
    if font != SERIF or not any(o in text for o in OMEGA):
        return [(code, encode(text))]
    return [(SYMBOL, b"W") if piece in OMEGA else (code, encode(piece))
            for piece in re.split("([%s])" % OMEGA, text) if piece]


class Canvas:
    """One page. Coordinates from the top-left corner, y downward, in points. paper: the page's colour."""

    def __init__(self, paper: Optional[str] = None) -> None:
        self.ops: List[bytes] = []
        if paper:
            self.ops.append(("q %s rg 0 0 %s %s re f Q" % (_rgb(paper), _n(PAGE_W), _n(PAGE_H))).encode())

    def _y(self, y: float) -> float:
        return PAGE_H - y

    def text(self, x: float, y: float, s: str, size: float = 10, font: str = "regular", color: str = INK,
             align: str = "left", spacing: float = 0.0, up: bool = False) -> None:
        """y is the baseline. spacing: letter-spacing; up: turned to read upward (a y axis's caption), align then
        moves it along its line."""
        s = str(s)
        if align != "left":
            shift = measure(s, size, font, spacing)
            shift = shift if align == "right" else shift / 2
            if up:
                y += shift
            else:
                x -= shift
        runs = _parts(s, font)
        size_b = _n(size).encode()
        where = ("0 1 -1 0 %s %s Tm" if up else "%s %s Td") % (_n(x), _n(self._y(y)))
        more = b"".join(b" /%s %s Tf (" % (code.encode(), size_b) + _esc(data) + b") Tj" for code, data in runs[1:])
        self.ops.append(b"BT /%s %s Tf %s rg %s%s (" % (runs[0][0].encode(), size_b, _rgb(color).encode(),
                                                         b"%s Tc " % _n(spacing).encode() if spacing else b"",
                                                         where.encode())
                        + _esc(runs[0][1]) + b") Tj" + more + (b" 0 Tc" if spacing else b"") + b" ET")

    def line(self, x1: float, y1: float, x2: float, y2: float, color: str = INK, width: float = 0.75,
             dash: object = False, cap: int = 0) -> None:
        self.polyline([(x1, y1), (x2, y2)], color, width, dash, cap)

    def polyline(self, points: Sequence[Tuple[float, float]], color: str, width: float = 1.0, dash: object = False,
                 cap: int = 1) -> None:
        """dash: True (3.5 on, 2.5 off) or (on, off) in points."""
        if len(points) < 2:
            return
        path = " ".join("%s %s %s" % (_n(x), _n(self._y(y)), "m" if i == 0 else "l") for i, (x, y) in enumerate(points))
        pattern = (3.5, 2.5) if dash is True else dash
        d = "[%s] 0 d " % " ".join(_n(v) for v in pattern) if pattern else ""   # type: ignore[union-attr]
        self.ops.append(("q %s RG %s w %d J 1 j %s%s S Q" % (_rgb(color), _n(width), cap, d, path)).encode())

    def rect(self, x: float, y: float, w: float, h: float, fill: Optional[str] = None, stroke: Optional[str] = None,
             width: float = 0.75, radius: float = 0.0) -> None:
        if radius > 0:
            r = min(radius, w / 2, h / 2)
            k = 0.5523 * r
            x0, y0, x1, y1 = x, self._y(y + h), x + w, self._y(y)
            box = ("%s %s m %s %s l %s %s %s %s %s %s c %s %s l %s %s %s %s %s %s c %s %s l %s %s %s %s %s %s c "
                   "%s %s l %s %s %s %s %s %s c h") % tuple(_n(v) for v in (
                       x0 + r, y0, x1 - r, y0, x1 - r + k, y0, x1, y0 + r - k, x1, y0 + r,
                       x1, y1 - r, x1, y1 - r + k, x1 - r + k, y1, x1 - r, y1,
                       x0 + r, y1, x0 + r - k, y1, x0, y1 - r + k, x0, y1 - r,
                       x0, y0 + r, x0, y0 + r - k, x0 + r - k, y0, x0 + r, y0))
        else:
            box = "%s %s %s %s re" % (_n(x), _n(self._y(y + h)), _n(w), _n(h))
        if fill and stroke:
            self.ops.append(("q %s rg %s RG %s w %s B Q" % (_rgb(fill), _rgb(stroke), _n(width), box)).encode())
        elif fill:
            self.ops.append(("q %s rg %s f Q" % (_rgb(fill), box)).encode())
        elif stroke:
            self.ops.append(("q %s RG %s w %s S Q" % (_rgb(stroke), _n(width), box)).encode())

    def dots(self, points: Sequence[Tuple[float, float]], color: str, r: float = 1.6, ring: str = PAPER,
             ring_w: float = 0.6, hollow: bool = False) -> None:
        """Round markers, filled in color and ringed in ring; hollow: filled with the paper and ringed in color."""
        k = 0.5523 * r
        parts = []
        for x, y in points:
            y = self._y(y)
            parts.append("%s %s m %s %s %s %s %s %s c %s %s %s %s %s %s c %s %s %s %s %s %s c %s %s %s %s %s %s c" % (
                _n(x + r), _n(y), _n(x + r), _n(y + k), _n(x + k), _n(y + r), _n(x), _n(y + r),
                _n(x - k), _n(y + r), _n(x - r), _n(y + k), _n(x - r), _n(y),
                _n(x - r), _n(y - k), _n(x - k), _n(y - r), _n(x), _n(y - r),
                _n(x + k), _n(y - r), _n(x + r), _n(y - k), _n(x + r), _n(y)))
        if parts:
            fill, stroke = (PAPER, color) if hollow else (color, ring)
            self.ops.append(("q %s rg %s RG %s w %s B Q" % (_rgb(fill), _rgb(stroke), _n(ring_w),
                                                             " ".join(parts))).encode())


def write_pdf_pages(pages: List[Canvas], path: Path, title: str, subject: str = "") -> None:
    objects: List[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    def pdf_text(s: str) -> bytes:
        return b"(" + _esc(encode(s)) + b")"

    catalog = add(b"")            # filled once the page tree number is known
    tree = add(b"")
    fonts = {key: add(b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>" % name.encode())
             for key, name, _ in FONTS.values()}
    fonts[SYMBOL] = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Symbol >>")   # its own encoding: W is Omega
    font_dict = b"<< " + b" ".join(b"/%s %d 0 R" % (k.encode(), n) for k, n in fonts.items()) + b" >>"
    kids = []
    for page in pages:
        data = zlib.compress(b"\n".join(page.ops))
        content = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream")
        kids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %s %s] /Resources << /Font %s >> /Contents %d 0 R >>"
                        % (tree, _n(PAGE_W).encode(), _n(PAGE_H).encode(), font_dict, content)))
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % tree
    objects[tree - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % k for k in kids), len(kids))
    stamp = datetime.now().strftime("D:%Y%m%d%H%M%S")
    info = add(b"<< /Title " + pdf_text(title) + b" /Subject " + pdf_text(subject) + b" /Creator (Kelvin) /Producer (Kelvin)"
               + b" /CreationDate (" + stamp.encode() + b") >>")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, info, xref)
    Path(path).write_bytes(bytes(out))


def wrap(text: str, width: float, size: float, font: str = "regular") -> List[str]:
    lines: List[str] = []
    for para in str(text).split("\n"):
        words = para.split(" ")
        line = ""
        for word in words:
            candidate = word if not line else line + " " + word
            if measure(candidate, size, font) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            if len(word) > 1 and measure(word, size, font) > width:   # a word longer than the line: cut where it fills
                edges = [0]   # its widths added up once: an instrument's whole reply in a stop reason once took minutes
                for ch in word:
                    edges.append(edges[-1] + _units(ch, font))
                start = 0
                while len(word) - start > 1 and (edges[-1] - edges[start]) * size / 1000 > width:
                    lo, hi = start + 1, len(word)   # the most that fits, halving; at least one character
                    while lo < hi:
                        mid = (lo + hi + 1) // 2
                        if (edges[mid] - edges[start]) * size / 1000 <= width:
                            lo = mid
                        else:
                            hi = mid - 1
                    lines.append(word[start:lo])
                    start = lo
                word = word[start:]
            line = word
        lines.append(line)
    return lines


def _base(top: float, lh: float, size: float) -> float:
    """The baseline of a line of type size in a line box lh tall from top (as a browser sets Times)."""
    return top + (lh - 1.107 * size) / 2 + 0.891 * size


def _clip(text: str, width: float, size: float, spacing: float = 0.0) -> str:
    """The text cut with an ellipsis to fit width."""
    if measure(text, size, SERIF, spacing) <= width:
        return text
    while text and measure(text + "…", size, SERIF, spacing) > width:
        text = text[:-1]
    return text.rstrip() + "…"


# ------------------------------------------------------------------ words with a few of them in the accent

Span = Tuple[str, bool, bool]    # a word, whether it is in the accent, whether it follows the last without a space
_UNIT = r"(?:[pnuµμmkMGT]?(?:V|A|Ω|Hz|K|T|s|W|F|m|g|Pa|dB)(?:/[A-Za-zµΩ]+)?|%)"
_VALUE = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?: ± \d+(?:\.\d+)?)? %s(?![\w/])" % _UNIT)
_KIND = re.compile(r"^([A-Z][\w ()/.,-]{0,32}?):\s")


def spans(text: str, label: str = "", values: bool = False) -> List[Span]:
    """The words of text; label's words (said first) and, with values, its numbers with their units in the
    accent (Kami's .hl). Punctuation right after a number stays on it ("1.600 V;")."""
    pieces: List[Tuple[str, bool]] = [(label + " ", True)] if label else []
    pos = 0
    for m in (_VALUE.finditer(text) if values else ()):
        pieces += [(text[pos:m.start()], False), (m.group(0), True)]
        pos = m.end()
    pieces.append((text[pos:], False))
    out: List[Span] = []
    spaced = True   # the text so far ends with a space
    for piece, accent in pieces:
        if not piece:
            continue
        for k, word in enumerate(piece.split()):
            out.append((word, accent, k == 0 and bool(out) and not spaced and not piece[0].isspace()))
        spaced = piece[-1].isspace()
    return out


def kind_spans(text: str) -> List[Span]:
    """"Decision: measure at 4 K first" with its kind in the accent; a line without one as it is."""
    m = _KIND.match(text)
    if m and not re.search(r"\d", m.group(1)):
        return spans(text[m.end():], text[:m.end()].strip())
    return spans(text)


def lines_of(words: Sequence[Span], size: float, width: float) -> List[List[Span]]:
    """Words in lines width wide, breaking only where there is a space."""
    space = measure(" ", size, SERIF)
    units: List[List[Span]] = []   # words that stay together
    for span in words:
        if span[2] and units:
            units[-1].append(span)
        else:
            units.append([span])
    lines: List[List[Span]] = []
    line: List[Span] = []
    used = 0.0
    for unit in units:
        w = sum(measure(s[0], size, SERIF) for s in unit)
        if w > width:   # longer than the line: in pieces
            for piece in wrap("".join(s[0] for s in unit), width, size, SERIF):
                if line:
                    lines.append(line)
                line, used = [(piece, unit[0][1], False)], measure(piece, size, SERIF)
            continue
        if line and used + space + w > width:
            lines.append(line)
            line, used = [], 0.0
        used += (space if line else 0) + w
        line += [(unit[0][0], unit[0][1], False)] + list(unit[1:])
    if line:
        lines.append(line)
    return lines


def draw_line(c: Canvas, line: Sequence[Span], x: float, base: float, size: float, color: str,
              accent: str = BRAND) -> None:
    """One line, the words in a colour together (one string each)."""
    space = measure(" ", size, SERIF)
    i = 0
    while i < len(line):
        j = i + 1
        while j < len(line) and line[j][1] == line[i][1]:
            j += 1
        s = "".join(("" if k == i or line[k][2] else " ") + line[k][0] for k in range(i, j))
        c.text(x, base, s, size, SERIF, accent if line[i][1] else color)
        x += measure(s, size, SERIF) + (0 if j < len(line) and line[j][2] else space)
        i = j


# ------------------------------------------------------------------ plots

TICK = 8.0          # tick labels and axis captions (Kami's 11 units at the one-pager's column width)
KEY = 8.7           # the legend's words
PLOT_H = 82.0       # the plot area's height (Kami's 112 units)


def _fit_name(chart: Chart) -> str:
    """"lorentzian fit", from a caption "Dashed: lorentzian (accepted ...) fit to lockin.X."."""
    m = re.match(r"Dashed: (.+?) fits? to ", chart.caption or "")
    return m.group(1).split(" (")[0] + " fit" if m else "fit"


def caption_of(chart: Chart) -> str:
    """What a plot's caption says beyond its legend: who chose the fit, how a value was worked out."""
    text = (chart.caption or "").strip()
    return "" if re.fullmatch(r"Dashed: [\w./ -]+ fits? to [^ ]+\.", text) else text


def plot(c: Canvas, chart: Chart, x0: float, y0: float, w: float, ph: float = PLOT_H) -> float:
    """A plot in Kami's chart style at (x0, y0), w wide; returns its height. Light grid lines at the ticks (the
    upright ones dashed), a dark base line, the y axis's caption up the left side, the legend in a row under a hairline.
    The first curve is the accent, its markers filled and ringed in the paper; the others olive and stone, hollow. A
    curve with a fit shows its points without a line, the fit dashed over them."""
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

    left = x0 + 16 + max(measure(t, TICK, SERIF) for t in y_labels) + 7
    right = x0 + w - max(8.7, measure(x_labels[-1], TICK, SERIF) / 2 + 1 if x_labels else 0.0)
    top = y0 + 11.6
    base = top + ph

    def sx(v: float) -> float:
        return left + (v - x_lo) / (x_hi - x_lo) * (right - left)

    def sy(v: float) -> float:
        return base - (v - yt[0]) / (yt[-1] - yt[0]) * ph

    for v, label in zip(yt, y_labels):
        c.line(left, sy(v), right, sy(v), GRID, 0.58)
        c.text(left - 5.8, sy(v) + 2.9, label, TICK, SERIF, STONE, "right")
    caption = axis_label(chart.title, y_shown)
    size = max(6.5, min(TICK, TICK * (ph + 20) / max(1.0, measure(caption, TICK, SERIF))))
    c.text(x0 + 6.4, (top + base) / 2, caption, size, SERIF, STONE, "center", up=True)
    for v, label in zip(xt, x_labels):
        if left + 0.5 < sx(v) < right - 0.5:
            c.line(sx(v), top, sx(v), base, GRID, 0.36, dash=(2.2, 2.9))
        c.text(sx(v), base + 11.6, label, TICK, SERIF, STONE, "center")
    c.text((left + right) / 2, base + 21.7, axis_label(chart.x_key, x_shown), TICK, SERIF, STONE, "center")
    c.line(left, base, right, base, NEAR_BLACK, 0.58)

    segs: List[List[List[Tuple[float, float]]]] = []
    for _, pts in data:
        runs, current = [], []
        for p in _thin(pts):
            if p is None:
                if current:
                    runs.append(current)
                current = []
            else:
                current.append((round(sx(p[0]), 2), round(sy(p[1]), 2)))
        if current:
            runs.append(current)
        segs.append(runs)
    curves = [i for i, (s, _) in enumerate(data) if not s.fit]
    owner: Dict[int, int] = {}            # a fit: the curve it was fitted to (the one before it)
    last = None
    for i, (s, _) in enumerate(data):
        if not s.fit:
            last = i
        elif last is not None:
            owner[i] = last
    fitted = set(owner.values())
    style = {i: (SERIES[k % len(SERIES)], k % len(SERIES) not in (0, 4)) for k, i in enumerate(curves)}
    count = {i: sum(len(r) for r in segs[i]) for i in curves}
    for i in curves:                       # lines first, markers over them, fits over everything
        color, hollow = style[i]
        if i not in fitted or count[i] > 250:
            for run in segs[i]:
                c.polyline(run, color, 0.75 if hollow else 1.1)
    for i in curves:
        color, hollow = style[i]
        if count[i] <= (250 if i in fitted else 60):
            r = 1.9 if count[i] <= 60 else 1.2
            c.dots([p for run in segs[i] for p in run], color, r - (0.1 if hollow else 0), PAPER,
                   0.72 if hollow else 0.6, hollow)
    single = len(curves) == 1
    for i, o in owner.items():
        for run in segs[i]:
            c.polyline(run, NEAR_BLACK if single else style[o][0], 0.65, dash=(2.9, 2.2))

    entries: List[Tuple[str, str, bool, str]] = []   # how its sample is drawn, colour, hollow, words
    if len(curves) > 1 or owner:
        for i in curves:
            color, hollow = style[i]
            how = "marker" if i in fitted and count[i] <= 250 else "line+marker" if count[i] <= 60 else "line"
            entries.append((how, color, hollow, data[i][0].name))
        if owner:
            entries.append(("dash", NEAR_BLACK if single else STONE, False, _fit_name(chart) if single else "fits"))
    if not entries:
        return base + 25 - y0
    y = base + 29
    c.line(left, y, right, y, GRID, 0.58)
    x, cy = left, y + 11.6
    for how, color, hollow, words in entries:
        need = 21.7 + measure(words, KEY, SERIF)
        if x + need > right + 2 and x > left:
            x, cy = left, cy + 13
        if how in ("line", "line+marker"):
            c.line(x, cy, x + 17.4, cy, color, 0.75 if hollow else 1.1, cap=1)
        if how in ("marker", "line+marker"):
            c.dots([(x + 8.7, cy)], color, 2.2, PAPER, 0.87 if hollow else 0.6, hollow)
        if how == "dash":
            c.line(x, cy, x + 17.4, cy, color, 0.75, dash=(2.9, 2.2))
        c.text(x + 21.7, cy + 2.9, words, KEY, SERIF, OLIVE)
        x += need + 20.3
    return cy + 8 - y0


# ------------------------------------------------------------------ tables

def _numeric(cells: Sequence[str]) -> bool:
    shown = [c.strip() for c in cells if c and c.strip() not in ("", "-")]
    return bool(shown) and sum(bool(re.match(r"[-+(]?\.?\d", v)) for v in shown) >= 0.6 * len(shown)


def _grid(t: Table, width: float, size: float) -> Tuple[List[float], List[bool]]:
    """Column widths filling width (as a table 100% wide), and which columns hold numbers (set to the right)."""
    cols = max([len(t.header)] + [len(r) for r in t.rows])
    cell = lambda r, i: r[i] if i < len(r) else ""   # noqa: E731
    natural = [max([measure(cell(t.header, i), size, SERIF)] + [measure(cell(r, i), size, SERIF) for r in t.rows])
               + (10 if 0 < i < cols - 1 else 5 if cols > 1 else 0) for i in range(cols)]
    total = sum(natural) or 1.0
    right = [i > 0 and _numeric([cell(r, i) for r in t.rows]) for i in range(cols)]
    if total <= width:
        return [n * width / total for n in natural], right
    whole = [False] * cols   # too wide: the columns narrower than a fair share stay whole, the others share the rest
    while True:
        free = [i for i in range(cols) if not whole[i]]
        share = (width - sum(natural[i] for i in range(cols) if whole[i])) / max(1, len(free))
        small = [i for i in free if natural[i] <= share]
        if not small or len(small) == len(free):
            break
        for i in small:
            whole[i] = True
    room = width - sum(natural[i] for i in range(cols) if whole[i])
    rest = sum(natural[i] for i in range(cols) if not whole[i]) or 1.0
    return [natural[i] if whole[i] else natural[i] * room / rest for i in range(cols)], right


def _cells(widths: Sequence[float], cells: Sequence[str], size: float) -> List[List[str]]:
    last = len(widths) - 1
    return [wrap(cells[i] if i < len(cells) else "", max(4.0, w - (5 if i else 0) - (5 if i < last else 0)), size,
                 SERIF) for i, w in enumerate(widths)]


def _draw_cells(c: Canvas, x: float, y: float, widths: Sequence[float], right: Sequence[bool],
                wrapped: Sequence[Sequence[str]], size: float, colors: Sequence[str]) -> float:
    """One row from its top at y; returns its height."""
    lh = size * 1.4
    last = len(widths) - 1
    for i, (w, lines) in enumerate(zip(widths, wrapped)):
        for j, line in enumerate(lines):
            base = _base(y + 2.5 + lh * j, lh, size)
            if right[i]:
                c.text(x + w - (5 if i < last else 0), base, line, size, SERIF, colors[i], "right")
            else:
                c.text(x + (5 if i else 0), base, line, size, SERIF, colors[i])
        x += w
    return lh * max(len(lines) for lines in wrapped) + 5


def plain(t: Table) -> bool:
    """A table of one curve's values (Quantity, Value): its rows say what they are without a header."""
    return [h.lower() for h in t.header[:2]] == ["quantity", "value"] and len(t.header) == 2


def table_block(c: Canvas, t: Table, x: float, y: float, w: float, size: float = 8.0, title: str = "") -> float:
    """A compact table that stays together (in a plot's card): Kami's financial table, numbers to the right, hairlines
    under the rows. Returns its height."""
    y0 = y
    if title:
        for line in wrap(title, w, 8.5, SERIF):
            c.text(x, _base(y, 12, 8.5), line, 8.5, SERIF, STONE)
            y += 12
        y += 1
    widths, right = _grid(t, w, size)
    if not plain(t) and any(t.header):
        y += _draw_cells(c, x, y, widths, right, _cells(widths, t.header, size), size, [DARK_WARM] * len(widths))
    c.line(x, y, x + w, y, BORDER, 0.6)
    for r in t.rows:
        y += _draw_cells(c, x, y, widths, right, _cells(widths, r, size), size, [NEAR_BLACK] * len(widths))
        c.line(x, y, x + w, y, BORDER, 0.25)
    return y - y0


# ------------------------------------------------------------------ a plot with its result

@dataclass
class Card:
    chart: Chart
    title: str = ""
    sub: str = ""
    words: str = ""              # the plot's result in words, when the text above doesn't say it already
    table: Optional[Table] = None


def card_for(chart: Chart, tables: List[Table], said: str = "", title: str = "", sub: str = "") -> Card:
    """A plot's card: its fitted values (taken out of tables) and its result in words, unless said says it."""
    words = chart.summary if chart.summary and _plainer(chart.summary) not in _plainer(said) else ""
    return Card(chart, title or plot_title(chart), sub, words, matching(chart, tables))


def _plainer(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().rstrip(".")


def _card(c: Canvas, card: Card, x: float, y: float, w: float, side: bool = False, ph: float = PLOT_H) -> float:
    """A plot with its title, its result in words and its numbers under it; side: words and numbers beside it (a plot
    alone in its row). Returns the height."""
    y0 = y
    size = 12.5
    title_w = measure(card.title, size, SERIF)
    c.text(x, _base(y, 18, size), _clip(card.title, w, size), size, SERIF, NEAR_BLACK)
    if card.sub and title_w + 6 + measure(card.sub, 8.5, SERIF) <= w:
        c.text(x + title_w + 6, _base(y, 18, size), card.sub, 8.5, SERIF, STONE)
        y += 22
    elif card.sub:
        c.text(x, _base(y + 17, 12, 8.5), _clip(card.sub, w, 8.5), 8.5, SERIF, STONE)
        y += 33
    else:
        y += 22
    pw = COL if side else w
    left = y + plot(c, card.chart, x, y, pw, ph)
    extra = caption_of(card.chart)
    for line in wrap(extra, pw, 8, SERIF) if extra else []:
        c.text(x, _base(left, 11, 8), line, 8, SERIF, STONE)
        left += 11
    if side:
        x, y, w = x + COL + GAP, y + 4, w - COL - GAP
    else:
        y = left + 4
    if card.words:
        for line in wrap(card.words, w, 10, SERIF):
            c.text(x, _base(y, 14.5, 10), line, 10, SERIF, NEAR_BLACK)
            y += 14.5
        y += 5
    if card.table is not None:
        t = card.table
        redundant = re.fullmatch(re.escape(card.title) + r"(?: \([^)]*\))?", t.title or "")
        y += table_block(c, t, x, y, w, title="" if redundant else t.title)
    return max(y, left) - y0


# ------------------------------------------------------------------ the page flow

class Flow:
    """Lays blocks down the page and starts a new page when one is full. tight: smaller plots and type, for a report
    that has to fit its pages."""

    def __init__(self, title: str, foot: str = "", tight: int = 0) -> None:
        self.title, self.foot, self.tight = title, foot, tight
        self.pages: List[Canvas] = []
        self.y = TOP
        self.new_page()

    @property
    def c(self) -> Canvas:
        return self.pages[-1]

    @property
    def plot_h(self) -> float:
        return (PLOT_H, 66.0, 54.0)[min(self.tight, 2)]

    def new_page(self) -> None:
        self.pages.append(Canvas(PAPER))
        self.y = TOP

    def need(self, h: float) -> None:
        if self.y + h > BOTTOM and self.y > TOP + 1:
            self.new_page()

    def space(self, h: float) -> None:
        self.y += h

    def rule(self, dotted: bool = False) -> None:
        if dotted:
            self.c.line(MARGIN, self.y, PAGE_W - MARGIN, self.y, LINE, 0.7, dash=(0.1, 1.8), cap=1)
        else:
            self.c.line(MARGIN, self.y, PAGE_W - MARGIN, self.y, BORDER, 0.5)

    def header(self, eyebrow: str, title: str, subtitle: str, meta: Sequence[str], tag: str = "") -> None:
        """Kami's document header: a small tracked label, the title and a line under it; on the right, where and when
        and a tag (simulated data); a hairline under it all."""
        meta = [m for m in meta if m]
        tag_w = measure(tag.upper(), 9, SERIF, 0.4) + 12 if tag else 0.0
        right = max([measure(m, 9, SERIF) for m in meta] + [tag_w])
        width = WIDTH - (right + 20 if right else 0)
        size = 26.0 if len(wrap(title, width, 26, SERIF)) <= 2 else 20.0
        size = size if not self.tight else min(size, 22.0)
        top = self.y
        if eyebrow:
            self.c.text(MARGIN, _base(top, 13, 9), eyebrow.upper(), 9, SERIF, BRAND, spacing=1.5)
            top += 20
        for line in wrap(title, width, size, SERIF)[:4]:
            self.c.text(MARGIN, _base(top, size * 1.12, size), line, size, SERIF, NEAR_BLACK, spacing=-0.3)
            top += size * 1.12
        top += 5
        for line in wrap(subtitle, width, 11.5, SERIF)[:3] if subtitle else []:
            self.c.text(MARGIN, _base(top, 16, 11.5), line, 11.5, SERIF, OLIVE)
            top += 16
        my = max(self.y, top - 13.5 * len(meta) - (19 if tag else 0))   # the right side ends where the left does
        for m in meta:
            self.c.text(PAGE_W - MARGIN, _base(my, 13.5, 9), m, 9, SERIF, STONE, "right")
            my += 13.5
        if tag:
            my += 4
            self.c.rect(PAGE_W - MARGIN - tag_w, my, tag_w, 15, fill=TAG_BG, radius=3)
            self.c.text(PAGE_W - MARGIN - 6 + 0.4, _base(my, 15, 9), tag.upper(), 9, SERIF, BRAND, "right", 0.4)
            my += 15
        self.y = max(top, my) + (12 if not self.tight else 8)
        self.rule()
        self.y += 16 if not self.tight else 10

    def metrics(self, items: Sequence[Tuple[str, str]]) -> None:
        """Key numbers in a row, each large in the accent with what it is after it; as many as fit on the line (Kami:
        shorten, never wrap)."""
        chosen: List[Tuple[str, str]] = []
        widths: List[float] = []
        for v, words in items:   # in order, each that still fits on the line, three at most
            w = measure(v, 22, SERIF, -0.3) + 5 + measure(words, 9, SERIF)
            if v and sum(widths) + w + 16 * len(chosen) <= WIDTH:
                chosen.append((v, words))
                widths.append(w)
            if len(chosen) == 3:
                break
        items = chosen
        if not items:
            return
        self.need(52)
        cell = (WIDTH - 16 * (len(items) - 1)) / len(items)
        even = all(w <= cell for w in widths)
        gap = 16.0 if even or len(items) == 1 else (WIDTH - sum(widths)) / (len(items) - 1)
        base = _base(self.y + 4, 22, 22)
        x = MARGIN
        for (v, words), w in zip(items, widths):
            self.c.text(x, base, v, 22, SERIF, BRAND, spacing=-0.3)
            self.c.text(x + measure(v, 22, SERIF, -0.3) + 5, base, words, 9, SERIF, OLIVE)
            x += cell + 16 if even else w + gap
        self.y += 32
        self.rule(dotted=True)
        self.y += 18 if not self.tight else 12

    def words(self, words: Sequence[Span], size: float, color: str, lh: float, gap: float, x: float = MARGIN,
              width: float = WIDTH) -> None:
        for line in lines_of(words, size, width):
            self.need(size * lh)
            draw_line(self.c, line, x, _base(self.y, size * lh, size), size, color)
            self.y += size * lh
        self.y += gap

    def lead(self, text: str, last: bool = True) -> None:
        """The finding in a sentence or two, large, its numbers in the accent (Kami's .lead)."""
        size = 12.5 if not self.tight else 11.0
        self.words(spans(text, values=True), size, DARK_WARM, 1.5, 14 if last else 6)

    def h2(self, text: str, sub: str = "", keep: float = 80.0, size: float = 16.0) -> None:
        """A section's title with a muted note after it (Kami's h2 and .sub), kept with what follows."""
        size = size if not self.tight else size - 2.5
        lh = size * 1.45
        self.need(lh + 6 + keep)
        lines = wrap(text, WIDTH, size, SERIF)
        for line in lines:
            self.c.text(MARGIN, _base(self.y, lh, size), line, size, SERIF, NEAR_BLACK)
            self.y += lh
        end = MARGIN + measure(lines[-1], size, SERIF) + 6
        if sub and end + measure(sub, 9, SERIF) <= PAGE_W - MARGIN:
            self.c.text(end, _base(self.y - lh, lh, size), sub, 9, SERIF, STONE)
        elif sub:
            for line in wrap(sub, WIDTH, 9, SERIF):
                self.c.text(MARGIN, _base(self.y, 13, 9), line, 9, SERIF, STONE)
                self.y += 13
        self.y += 6

    def h3(self, text: str, sub: str = "", keep: float = 50.0) -> None:
        self.h2(text, sub, keep, 12.5)

    def label(self, text: str, keep: float = 30.0) -> None:
        """A small heading inside a section: Approvals and alarms, Plan."""
        self.need(15 + keep)
        self.c.text(MARGIN, _base(self.y, 15, 10), text, 10, SERIF, DARK_WARM)
        self.y += 17

    def para(self, text: str, size: float = 10.0, color: str = NEAR_BLACK, gap: float = 6.0) -> None:
        self.words(spans(text), size, color, 1.45, gap)

    def note(self, text: str) -> None:
        self.para(text, 8.5, STONE, 4)

    def bullets(self, items: Sequence[str], size: float = 10.0, gap: float = 3.0) -> None:
        """Kami's list: a marker in the accent; "Decision: ..." shows its kind in the accent too."""
        lh = size * 1.45
        for item in items:
            for j, line in enumerate(lines_of(kind_spans(item), size, WIDTH - 14)):
                self.need(lh)
                base = _base(self.y, lh, size)
                if j == 0:
                    self.c.text(MARGIN + 2, base, "•", size, SERIF, BRAND)
                draw_line(self.c, line, MARGIN + 14, base, size, NEAR_BLACK)
                self.y += lh
            self.y += gap
        self.y += 4

    def callout(self, label: str, texts: Sequence[str], size: float = 10.5) -> None:
        """Kami's callout: an ivory box, the words in olive and its label in the accent. Each text a line of its own;
        "Issue: ..." shows its kind in the accent."""
        lh = size * 1.55
        paras = [lines_of(spans(t, label) if k == 0 and label else kind_spans(t), size, WIDTH - 24)
                 for k, t in enumerate(texts) if t]
        while paras:
            room = max(lh * 2, BOTTOM - self.y - 26 if self.y > TOP + 1 else BOTTOM - TOP - 26)
            n = 0
            used = 0.0
            while n < len(paras) and used + lh * len(paras[n]) + 3 <= room:   # what fits on this page
                used += lh * len(paras[n]) + 3
                n += 1
            if n == 0:
                if self.y > TOP + 1:
                    self.new_page()
                    continue
                n = 1
            chunk, paras = paras[:n], paras[n:]
            h = sum(lh * len(p) for p in chunk) + 3 * (len(chunk) - 1) + 16
            self.need(h + 10)
            self.y += 4
            self.c.rect(MARGIN, self.y, WIDTH, h, fill=IVORY, radius=3)
            y = self.y + 8
            for p in chunk:
                for line in p:
                    draw_line(self.c, line, MARGIN + 12, _base(y, lh, size), size, OLIVE)
                    y += lh
                y += 3
            self.y += h + 10

    def kv(self, rows: Sequence[Tuple[str, str]], size: float = 8.5, key_w: float = 0.0) -> None:
        """Labels and their values, as a two-column table: the labels in stone."""
        rows = [(k, v) for k, v in rows if k or v]
        if not rows:
            return
        key_w = key_w or min(130.0, max(measure(k, size, SERIF) for k, _ in rows) + 12)
        lh = size * 1.4
        self.need(lh + 12)
        self.c.line(MARGIN, self.y, PAGE_W - MARGIN, self.y, BORDER, 0.6)
        for k, v in rows:
            keys, values = wrap(k, key_w - 8, size, SERIF), wrap(v, WIDTH - key_w, size, SERIF)
            self.need(lh * min(3, max(len(keys), len(values))) + 5)
            self.y += 2.5
            for j in range(max(len(keys), len(values))):
                if self.y + lh > BOTTOM:
                    self.new_page()
                base = _base(self.y, lh, size)
                if j < len(keys):
                    self.c.text(MARGIN, base, keys[j], size, SERIF, STONE)
                if j < len(values):
                    self.c.text(MARGIN + key_w, base, values[j], size, SERIF, NEAR_BLACK)
                self.y += lh
            self.y += 2.5
            self.c.line(MARGIN, self.y, PAGE_W - MARGIN, self.y, BORDER, 0.25)
        self.y += 10

    def table(self, t: Table, size: float = 8.0, title: bool = True) -> None:
        """Kami's compact table across the page: numbers to the right, a rule under the header, hairlines under the
        rows; long ones go on over pages."""
        if not (t.header or t.rows):
            return
        widths, right = _grid(t, WIDTH, size)
        lh = size * 1.4
        if title and t.title:
            self.need(14 + 3 * (lh + 5))
            for line in wrap(t.title, WIDTH, 9, SERIF):
                self.c.text(MARGIN, _base(self.y, 13, 9), line, 9, SERIF, STONE)
                self.y += 13
            self.y += 1

        def head() -> None:
            if any(t.header) and not plain(t):
                self.y += _draw_cells(self.c, MARGIN, self.y, widths, right, _cells(widths, t.header, size), size,
                                      [DARK_WARM] * len(widths))
            self.c.line(MARGIN, self.y, PAGE_W - MARGIN, self.y, BORDER, 0.6)

        self.need(2 * (lh + 5) + 4)
        head()
        for r in t.rows:
            cells = _cells(widths, r, size)
            h = lh * max(len(x) for x in cells) + 5
            if self.y + h > BOTTOM and self.y > TOP + 1:
                self.new_page()
                head()
            self.y += _draw_cells(self.c, MARGIN, self.y, widths, right, cells, size, [NEAR_BLACK] * len(widths))
            self.c.line(MARGIN, self.y, PAGE_W - MARGIN, self.y, BORDER, 0.25)
        self.y += 10

    def cards(self, cards: Sequence[Card]) -> None:
        """Plots two to a row, each with its result in words and its numbers under it; one left over sits with its
        words and numbers beside it."""
        for i in range(0, len(cards), 2):
            pair = list(cards[i:i + 2])
            side = len(pair) == 1
            w = WIDTH if side else COL
            hs = [_card(Canvas(), card, 0, 0, w, side, self.plot_h) for card in pair]
            self.need(max(hs))
            for k, card in enumerate(pair):
                _card(self.c, card, MARGIN + k * (COL + GAP), self.y, w, side, self.plot_h)
            self.y += max(hs) + (GAP if not self.tight else 10)

    def steps(self, items: Sequence[Tuple[str, str]], title: str = "What's next", sub: str = "") -> None:
        """Kami's timeline: up to three steps side by side, each what it comes from, in the accent, and what to do."""
        items = [(k, t) for k, t in items if t][:3]
        if not items:
            return
        w = (WIDTH - 12 * (len(items) - 1)) / len(items)
        laid = [(wrap(k.upper(), w, 11, SERIF)[:2], wrap(t, w, 10, SERIF)) for k, t in items]
        h = max(16 * len(ks) + 2 + 14.5 * len(ts) for ks, ts in laid)
        self.h2(title, sub, keep=h)
        for n, (ks, ts) in enumerate(laid):
            x, y = MARGIN + n * (w + 12), self.y
            for line in ks:
                self.c.text(x, _base(y, 16, 11), line, 11, SERIF, BRAND, spacing=0.3)
                y += 16
            y += 2
            for line in ts:
                self.c.text(x, _base(y, 14.5, 10), line, 10, SERIF, NEAR_BLACK)
                y += 14.5
        self.y += h + 12

    def code(self, text: str, size: float = 7.5) -> None:
        lh = size * 1.25
        per_line = int((WIDTH - 16) / (size * 0.6))
        lines: List[str] = []
        for raw in text.rstrip().split("\n"):
            raw = raw.replace("\t", "  ")
            while len(raw) > per_line:
                lines.append(raw[:per_line])
                raw = "  " + raw[per_line:]
            lines.append(raw)
        i = 0
        while i < len(lines):
            self.need(lh * 2 + 8)
            room = max(1, int((BOTTOM - self.y - 8) / lh))
            chunk = lines[i:i + room]
            self.c.rect(MARGIN, self.y, WIDTH, lh * len(chunk) + 8, fill=CODE_BG, radius=3)
            for j, line in enumerate(chunk):
                self.c.text(MARGIN + 8, self.y + 4 + lh * (j + 1) - size * 0.25, line, size, "mono", NEAR_BLACK)
            self.y += lh * len(chunk) + 8
            i += len(chunk)
            if i < len(lines):
                self.new_page()
        self.y += 8

    def finish(self, path: Path, subject: str) -> int:
        """Each page's foot (where it comes from, left; the page, right), then the file; returns the pages."""
        n = len(self.pages)
        for i, page in enumerate(self.pages, 1):
            y = PAGE_H - TOP - 15
            page.line(MARGIN, y, PAGE_W - MARGIN, y, LINE, 0.7, dash=(0.1, 1.8), cap=1)
            number = "page %d of %d" % (i, n)
            room = WIDTH - measure(number, 9, SERIF, 0.3) - 24
            page.text(MARGIN, PAGE_H - TOP - 1, _clip(self.foot or self.title, room, 9, 0.3), 9, SERIF, STONE,
                      spacing=0.3)
            page.text(PAGE_W - MARGIN, PAGE_H - TOP - 1, number, 9, SERIF, STONE, "right", 0.3)
        write_pdf_pages(self.pages, path, self.title, subject)
        return n


# ------------------------------------------------------------------ the report of runs someone asks for

def key_numbers(runs: Sequence[RunPart], n: int = 6) -> List[Tuple[str, str]]:
    """The report's key numbers: each run's main result first (its first fitted value, or a steady level), then its
    others; a value its measurement's runs share, once ("center, gate_sweep ×3"). Values that differ between
    runs of one measurement are left to the tables."""
    found = [(r, picks(r.results)) for r in runs]
    values: Dict[Tuple[str, str, str], List[str]] = {}
    for r, ps in found:
        for v, name, quantity in ps:
            values.setdefault((r.plan, quantity, name), []).append(v)
    out: List[Tuple[str, str]] = []
    seen = set()
    for level in (0, 1):
        for r, ps in found:
            for v, name, quantity in ps[:1] if level == 0 else ps[1:]:
                key = (r.plan, quantity, name)
                if key in seen or len(set(values[key])) > 1:
                    continue
                seen.add(key)
                k = len(values[key])
                out.append((v, "%s, %s%s" % (name, r.plan, " ×%d" % k if k > 1 else "")))
    return out[:n]


def _key_result(run: RunPart) -> str:
    if run.status != "done":
        return run.status + (": " + run.reason if run.reason else "")
    found = picks(run.results)
    if found:
        return "%s %s" % (found[0][1], found[0][0])
    return run.assessment.split(". ")[0].rstrip(".") if run.assessment else ""


def _span(runs: Sequence[RunPart]) -> str:
    """When the runs were measured: Nov 15 2026, 08:06-09:40; over several days, the days."""
    whens = [r.when for r in runs if r.when]
    if not whens:
        return ""
    day, _, start = whens[0].partition(", ")
    last_day, _, end = whens[-1].partition(", ")
    if day != last_day:
        return "%s to %s" % (day, last_day)
    start, end = start.split("–")[0], end.split("–")[-1]
    return "%s, %s" % (day, start if start == end else start + "–" + end)


def _sentences(text: str, n: int) -> str:
    parts = str(text).split(". ")
    return ". ".join(parts[:n]).rstrip(".") + "." if len(parts) > n else text


def _run(flow: Flow, run: RunPart, number: int, detail: int) -> None:
    """A run's results: what it found, each plot with its numbers, its issues. detail: as _layout's."""
    sub = "  ·  ".join(b for b in (run.device, run.when + (" (%s)" % run.duration if run.duration else ""),
                                   "%s points" % run.points) if b)
    flow.h2("%d. %s" % (number, run.plan), sub, keep=160)
    if run.reason and run.status != "done":
        flow.callout("Stopped.", [run.reason])
    said = (run.assessment if detail < 3 else _sentences(run.assessment, 2)) if run.assessment else ""
    if said:
        flow.para(said, 10.5, DARK_WARM, 8)
    charts = run.charts if detail < 2 else run.charts[:2] if detail == 2 else run.charts[:1]
    tables = list(run.results if detail < 3 else run.results[:1])
    flow.cards([card_for(chart, tables, said) for chart in charts])
    for t in tables:
        flow.table(t)
    if run.issues and detail < 3:
        flow.callout("Issues.", run.issues)


def _record(flow: Flow, run: RunPart, number: int, detail: int) -> None:
    """A run's record: what it set, swept and measured, the instruments at its start, approvals and alarms, the
    plan."""
    flow.h3("%d. %s" % (number, run.plan), "  ·  ".join(b for b in (run.device, run.when, "run " + run.run_id) if b))
    rows = []
    if run.sweep:
        rows.append(("Swept", "; ".join(run.sweep)))
    if run.measured:
        rows.append(("Measured", ", ".join(run.measured)))
    if run.setup:
        rows.append(("Set up", "; ".join(run.setup)))
    if run.changes:
        rows.append(("Left changed", "; ".join(run.changes)))
    if run.unread:
        rows.append(("Not read at the end", "; ".join(run.unread)))
    rows.append(("Started by", run.operator))
    flow.kv(rows)
    if run.conditions and detail < 2:
        flow.table(Table(["Instrument", "At the start (* = drive away from safe)"], [list(c) for c in run.conditions]),
                   title=False)
    if run.events:
        flow.label("Approvals and alarms")
        flow.kv(run.events if detail < 2 else run.events[:6], 8.5, 64)
    if run.plan_text and detail < 1:
        flow.label("Plan")
        flow.code(run.plan_text)


DETAILS = 7   # how far a report shortens itself to the length asked for (detail 0 to 6)


def _layout(report: Report, detail: int, pages: int) -> Flow:
    """The report on pages, at a level of detail: 0 all of it; 1 no plans; 2 no conditions tables or lab log,
    two plots a run; 3 each run in brief (its result, one plot), no record; 4 a brief like a one-pager: its key
    numbers, one plot of each of two measurements with their numbers, what's next; 5 the runs as a table and what's
    next; 6 the summary's first paragraph and the table of runs."""
    devices = list(dict.fromkeys(r.device for r in report.runs if r.device))
    foot = "%s%s  ·  made with Kelvin, %s" % (report.lab, " (simulated instruments)" if report.simulated else "",
                                              report.created)
    flow = Flow(report.title, foot)
    flow.header("Report", report.title, "  ·  ".join(b for b in (report.subtitle, ", ".join(devices[:6])) if b),
                [report.lab, _span(report.runs)], "Simulated data" if report.simulated else "")
    if detail:
        flow.note("Shortened to %d page%s, as asked: each run's own report (report.pdf in its folder) and its data "
                  "(run.h5) have the rest." % (pages, "" if pages == 1 else "s"))
        flow.space(6)
    flow.metrics(key_numbers(report.runs))
    summary = report.summary if detail < 6 else report.summary[:1]
    for k, p in enumerate(summary):
        flow.lead(p if detail < 6 else _sentences(p, 3), last=k == len(summary) - 1)
    if len(report.runs) > 1 and detail != 4 or detail >= 5:
        rows = [[str(i), r.plan, r.device or "-", r.when, r.points, _key_result(r)] for i, r in enumerate(report.runs, 1)]
        if detail == DETAILS - 1 and len(rows) > 30:
            rows = rows[:30] + [["", "... and %d more runs" % (len(rows) - 30), "", "", "", ""]]
        flow.h2("Runs", report.subtitle, keep=60)
        flow.table(Table(["#", "Run", "Device", "Measured", "Points", "Key result"], rows), title=False)
    if detail == 4:   # the brief: the first plot of each measurement (two), each with its result and numbers
        firsts: Dict[str, RunPart] = {}
        for r in report.runs:
            if r.charts and r.plan not in firsts:
                firsts[r.plan] = r
        flow.cards([card_for(r.charts[0], list(r.results), "", sub=", ".join(b for b in (r.plan, r.device) if b))
                    for r in list(firsts.values())[:2]])
    elif detail < 4:
        for i, run in enumerate(report.runs, 1):
            _run(flow, run, i, detail)
    if detail < 6:
        decisions = [t for k, t in report.notes if k.lower() == "decision"]
        questions = [t for k, t in report.notes if k.lower() == "question"]
        others = [(k, t) for k, t in report.notes if k.lower() not in ("decision", "question")]
        nexts: List[Tuple[str, str]] = []
        for r in report.runs:
            if r.next and r.next not in [t for _, t in nexts]:
                nexts.append((r.plan, r.next))
        steps = [("Decision", t) for t in decisions] + nexts
        whence = " and ".join(w for w, have in (("the project's notes", decisions), ("the runs' suggestions", nexts))
                              if have)
        flow.steps(steps, "What's next", "from " + whence if whence else "")
        if len(steps) > 3:
            flow.bullets(["%s: %s" % (k, t) for k, t in steps[3:]])
        for q in questions:
            flow.callout("Open question.", [q])
        if others and detail < 4:
            flow.h2("Project notes", keep=40)
            flow.bullets(["%s: %s" % (k.capitalize(), t) for k, t in others])
    if report.references and detail < 3:
        flow.h2("References", "the reading list", keep=30)
        flow.bullets(report.references, 9.5)
    if detail < 3:
        flow.h2("Run details", "what each run set, swept and measured; approvals and alarms", keep=120)
        for i, run in enumerate(report.runs, 1):
            _record(flow, run, i, detail)
    if report.log and detail < 2:
        flow.h2("Lab log", keep=40)
        flow.kv(report.log if detail == 0 else report.log[-30:], 8, 72)
    return flow


def write_pdf(report: Report, path: Path, pages: int = 0) -> int:
    """Write the report; returns the number of pages. pages: at most this many, as asked ("a one-page summary"):
    it leaves out detail, least needed first, until it fits (F2 l10, F3 P8: "one page" came out at 5 to 22)."""
    flow = _layout(report, 0, pages)
    detail = 0
    while pages and len(flow.pages) > pages and detail < DETAILS - 1:
        detail += 1
        flow = _layout(report, detail, pages)
    return flow.finish(path, report.subtitle)
