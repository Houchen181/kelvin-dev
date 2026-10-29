"""A small PDF writer and the report laid out in it.

Text uses the PDF standard fonts (Helvetica, Courier), so nothing has to be embedded or installed; plots are vector
lines. No third-party package is needed, so reports work in every Kelvin install."""
from __future__ import annotations

import math
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .chart import AXIS, GRID, INK, MUTED, Chart, Geometry, Table, layout
from .model import Report, RunPart

# Advance widths (1/1000 em) of characters 32-126 in Helvetica and Helvetica-Bold.
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
FONTS = {"regular": ("F1", "Helvetica", _HELV), "bold": ("F2", "Helvetica-Bold", _HELV_B),
         "mono": ("F3", "Courier", None), "italic": ("F4", "Helvetica-Oblique", _HELV)}
# What the standard fonts cannot show, spelled out.
_SPELL = {"\u03a9": "Ohm", "\u2126": "Ohm", "\u2192": "->", "\u2190": "<-", "\u2194": "<->", "\u2248": "~",
          "\u2264": "<=", "\u2265": ">=", "\u221a": "sqrt", "\u221e": "inf", "\u0394": "Delta ", "\u03b1": "alpha",
          "\u03b2": "beta", "\u03b3": "gamma", "\u03b4": "delta", "\u03b8": "theta", "\u03bb": "lambda",
          "\u03bc": "\u00b5", "\u03c0": "pi", "\u03c3": "sigma", "\u03c4": "tau", "\u03c6": "phi", "\u03c9": "omega",
          "\u2212": "-", "\u2009": " ", "\u202f": " ", "\u00a0": " ", "\u2011": "-", "\u2713": "ok", "\u2717": "x"}

PAGE_W, PAGE_H = 612.0, 792.0      # US Letter, points
MARGIN = 54.0
WIDTH = PAGE_W - 2 * MARGIN
FOOTER = 30.0


def encode(text: str) -> bytes:
    return "".join(_SPELL.get(c, c) for c in str(text)).encode("cp1252", "replace")


def measure(text: str, size: float, font: str = "regular") -> float:
    widths = FONTS[font][2]
    if widths is None:
        return len(encode(text)) * 600 * size / 1000
    total = 0
    for c in encode(text):
        total += widths[c - 32] if 32 <= c < 127 else _HIGH.get(c, 556)
    return total * size / 1000


def _esc(b: bytes) -> bytes:
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)").replace(b"\r", b"").replace(b"\n", b" ")


def _rgb(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    return "%.3f %.3f %.3f" % tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _n(v: float) -> str:
    s = "%.2f" % v
    return s.rstrip("0").rstrip(".") if "." in s else s


class Canvas:
    """One page. Coordinates from the top-left corner, y downward, in points."""

    def __init__(self) -> None:
        self.ops: List[bytes] = []

    def _y(self, y: float) -> float:
        return PAGE_H - y

    def text(self, x: float, y: float, s: str, size: float = 10, font: str = "regular", color: str = INK,
             align: str = "left") -> None:
        """y is the baseline."""
        if align != "left":
            w = measure(s, size, font)
            x -= w if align == "right" else w / 2
        self.ops.append(b"BT /%s %s Tf %s rg %s %s Td (" % (FONTS[font][0].encode(), _n(size).encode(),
                                                             _rgb(color).encode(), _n(x).encode(),
                                                             _n(self._y(y)).encode()) + _esc(encode(s)) + b") Tj ET")

    def line(self, x1: float, y1: float, x2: float, y2: float, color: str = INK, width: float = 0.75,
             dash: bool = False) -> None:
        self.polyline([(x1, y1), (x2, y2)], color, width, dash)

    def polyline(self, points: Sequence[Tuple[float, float]], color: str, width: float = 1.0, dash: bool = False,
                 cap: int = 1) -> None:
        if len(points) < 2:
            return
        path = " ".join("%s %s %s" % (_n(x), _n(self._y(y)), "m" if i == 0 else "l") for i, (x, y) in enumerate(points))
        self.ops.append(("q %s RG %s w %d J 1 j %s %s S Q" % (_rgb(color), _n(width), cap,
                                                              "[3.5 2.5] 0 d" if dash else "", path)).encode())

    def rect(self, x: float, y: float, w: float, h: float, fill: Optional[str] = None, stroke: Optional[str] = None,
             width: float = 0.75) -> None:
        box = "%s %s %s %s re" % (_n(x), _n(self._y(y + h)), _n(w), _n(h))
        if fill and stroke:
            self.ops.append(("q %s rg %s RG %s w %s B Q" % (_rgb(fill), _rgb(stroke), _n(width), box)).encode())
        elif fill:
            self.ops.append(("q %s rg %s f Q" % (_rgb(fill), box)).encode())
        elif stroke:
            self.ops.append(("q %s RG %s w %s S Q" % (_rgb(stroke), _n(width), box)).encode())

    def dots(self, points: Sequence[Tuple[float, float]], color: str, r: float = 1.6) -> None:
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
            self.ops.append(("q %s rg 1 1 1 RG 0.6 w %s B Q" % (_rgb(color), " ".join(parts))).encode())


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
            while measure(word, size, font) > width and len(word) > 1:   # a word longer than the line
                cut = len(word)
                while cut > 1 and measure(word[:cut], size, font) > width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines


def draw_chart(c: Canvas, geo: Geometry, x0: float, y0: float) -> None:
    f = geo.font
    left, top, pw, ph = geo.box
    c.text(x0, y0 + f * 1.15, geo.title, f * 1.1, "bold")
    for name, color, dashed, (lx, ly) in geo.legend:
        c.line(x0 + lx, y0 + ly, x0 + lx + f * 2.2, y0 + ly, color, 1.4, dashed)
        c.text(x0 + lx + f * 2.7, y0 + ly + f * 0.33, name, f * 0.95, color=MUTED)
    for y, label in geo.y_ticks:
        c.line(x0 + left, y0 + y, x0 + left + pw, y0 + y, GRID, 0.5)
        c.text(x0 + left - f * 0.5, y0 + y + f * 0.33, label, f, color=MUTED, align="right")
    c.line(x0 + left, y0 + top + ph, x0 + left + pw, y0 + top + ph, AXIS, 0.75)
    for x, label in geo.x_ticks:
        c.line(x0 + x, y0 + top + ph, x0 + x, y0 + top + ph + f * 0.4, AXIS, 0.75)
        c.text(x0 + x, y0 + top + ph + f * 1.45, label, f, color=MUTED, align="center")
    c.text(x0 + left + pw / 2, y0 + top + ph + f * 2.95, geo.x_label, f, color=MUTED, align="center")
    for color, dashed, segments in geo.lines:
        for seg in segments:
            if len(seg) == 1:
                c.dots([(x0 + seg[0][0], y0 + seg[0][1])], color)
            c.polyline([(x0 + x, y0 + y) for x, y in seg], color, 1.1 if dashed else 1.3, dashed)
    for color, pts in geo.markers:
        c.dots([(x0 + x, y0 + y) for x, y in pts], color)


class Flow:
    """Lays blocks down the page and starts a new page when one is full."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.pages: List[Canvas] = []
        self.y = MARGIN
        self.new_page()

    @property
    def c(self) -> Canvas:
        return self.pages[-1]

    def new_page(self) -> None:
        self.pages.append(Canvas())
        self.y = MARGIN

    def need(self, h: float) -> None:
        if self.y + h > PAGE_H - MARGIN - FOOTER + 10 and self.y > MARGIN + 1:
            self.new_page()

    def space(self, h: float) -> None:
        self.y += h

    def heading(self, text: str, size: float = 14, color: str = INK, before: float = 10) -> None:
        self.need(before + size * 1.6 + 60)   # keep a heading with what follows
        self.y += before + size
        self.c.text(MARGIN, self.y, text, size, "bold", color)
        self.y += size * 0.55

    def para(self, text: str, size: float = 9.5, font: str = "regular", color: str = INK, indent: float = 0,
             gap: float = 4, bullet: str = "") -> None:
        lh = size * 1.32
        width = WIDTH - indent - (size if bullet else 0)
        lines = wrap(text, width, size, font)
        for i, line in enumerate(lines):
            self.need(lh)
            self.y += lh
            if bullet and i == 0:
                self.c.text(MARGIN + indent, self.y - size * 0.3, bullet, size, font, MUTED)
            self.c.text(MARGIN + indent + (size if bullet else 0), self.y - size * 0.3, line, size, font, color)
        self.y += gap

    def kv(self, rows: Sequence[Tuple[str, str]], size: float = 9, key_w: float = 110) -> None:
        lh = size * 1.3
        for k, v in rows:
            lines = wrap(v, WIDTH - key_w, size)
            self.need(lh * len(lines))
            for i, line in enumerate(lines):
                self.y += lh
                if i == 0:
                    self.c.text(MARGIN, self.y - size * 0.3, k, size, "bold", MUTED)
                self.c.text(MARGIN + key_w, self.y - size * 0.3, line, size)
        self.y += 4

    @staticmethod
    def _natural(t: Table, size: float) -> List[float]:
        return [max([measure(t.header[i], size, "bold")] + [measure(r[i] if i < len(r) else "", size) for r in t.rows])
                + 12 for i in range(len(t.header))]

    def tables(self, ts: Sequence[Table], size: float = 8.5, gap: float = 18) -> None:
        """Tables two side by side where both fit in half the width (short fit tables), else one under another."""
        half = (WIDTH - gap) / 2
        lh = size * 1.3
        i = 0
        while i < len(ts):
            pair = list(ts[i:i + 2])
            if len(pair) == 2 and all(sum(self._natural(t, size)) <= half for t in pair):
                self.need(max((size * 1.5 + 3 if t.title else 0) + (len(t.rows) + 1) * (lh + 5) + 6 for t in pair))
                y0, ends = self.y, []
                for k, t in enumerate(pair):
                    self.y = y0
                    self.table(t, size, MARGIN + k * (half + gap), half)
                    ends.append(self.y)
                self.y = max(ends)
                i += 2
            else:
                self.table(ts[i], size)
                i += 1

    def table(self, t: Table, size: float = 8.5, x0: float = MARGIN, width: float = WIDTH) -> None:
        cols = len(t.header)
        if not cols:
            return
        natural = self._natural(t, size)
        total = sum(natural)
        widths = natural if total <= width else [w * width / total for w in natural]
        lh = size * 1.3
        if t.title:
            self.need(lh * 3)
            self.y += size * 1.5
            self.c.text(x0, self.y, t.title, size, "bold", MUTED)
            self.y += 3

        def row(cells: Sequence[str], font: str, fill: Optional[str]) -> None:
            wrapped = [wrap(cells[i] if i < len(cells) else "", widths[i] - 10, size, font) for i in range(cols)]
            h = lh * max(len(w) for w in wrapped) + 5
            self.need(h)
            if fill:
                self.c.rect(x0, self.y, sum(widths), h, fill=fill)
            x = x0
            for i, lines in enumerate(wrapped):
                for j, line in enumerate(lines):
                    self.c.text(x + 5, self.y + 2 + lh * (j + 1) - size * 0.3, line, size, font)
                x += widths[i]
            self.y += h
            self.c.line(x0, self.y, x0 + sum(widths), self.y, GRID, 0.6)

        row(t.header, "bold", "f1f3f6")
        for r in t.rows:
            row(r, "regular", None)
        self.y += 6

    def charts(self, charts: Sequence[Chart], per_row: int = 2, height: float = 190) -> None:
        if not charts:
            return
        per_row = 1 if len(charts) == 1 else per_row
        gap = 18
        w = (WIDTH - gap * (per_row - 1)) / per_row
        h = height if per_row > 1 else height * 1.25
        for i in range(0, len(charts), per_row):
            self.need(h + 8)
            for j, chart in enumerate(charts[i:i + per_row]):
                geo = layout(chart, w, h, lambda s, size: measure(s, size), 7.5 if per_row > 1 else 8)
                draw_chart(self.c, geo, MARGIN + j * (w + gap), self.y)
            self.y += h + 8
        captions = list(dict.fromkeys(c.caption for c in charts if c.caption))   # in the charts' order
        if len(captions) > 1 and all(c.startswith("Dashed: ") for c in captions):   # one line: "Dashed: a; b."
            captions = ["Dashed: " + "; ".join(c[len("Dashed: "):].rstrip(".") for c in captions) + "."]
        for cap in captions:
            self.para(cap, 8, "italic", MUTED, gap=2)

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
            room = max(1, int((PAGE_H - MARGIN - FOOTER - self.y - 8) / lh))
            chunk = lines[i:i + room]
            self.c.rect(MARGIN, self.y, WIDTH, lh * len(chunk) + 8, fill="f6f8fa")
            for j, line in enumerate(chunk):
                self.c.text(MARGIN + 8, self.y + 4 + lh * (j + 1) - size * 0.25, line, size, "mono")
            self.y += lh * len(chunk) + 8
            i += len(chunk)
            if i < len(lines):
                self.new_page()
        self.y += 6

    def rule(self) -> None:
        self.need(10)
        self.y += 5
        self.c.line(MARGIN, self.y, MARGIN + WIDTH, self.y, GRID, 0.8)
        self.y += 5

    def finish(self, path: Path, subject: str) -> int:
        n = len(self.pages)
        for i, page in enumerate(self.pages, 1):
            page.text(MARGIN, PAGE_H - MARGIN + 18, self.title, 7.5, color=MUTED)
            page.text(PAGE_W - MARGIN, PAGE_H - MARGIN + 18, "page %d of %d" % (i, n), 7.5, color=MUTED, align="right")
        write_pdf_pages(self.pages, path, self.title, subject)
        return n


STATUS_COLOR = {"done": "1a7f37", "aborted": "b42318", "refused": "b42318"}


def _run(flow: Flow, run: RunPart, number: int) -> None:
    flow.heading("%d. %s" % (number, run.plan), 13, before=14)
    flow.para("%s  \u00b7  %s  \u00b7  %s points  \u00b7  %s  \u00b7  run %s" % (
        run.status.upper(), run.when, run.points, run.duration, run.run_id), 8.5, "regular", MUTED, gap=6)
    if run.reason and run.status != "done":
        flow.para("Stopped: " + run.reason, 9.5, "bold", STATUS_COLOR.get(run.status, INK))
    if run.assessment:
        flow.para(run.assessment, 10)
    if run.next:
        flow.para("Next: " + run.next, 9.5, "italic", MUTED)
    flow.charts(run.charts)
    for t in run.results:
        flow.table(t)
    if run.issues:
        flow.heading("Issues", 10, MUTED, before=4)
        for issue in run.issues:
            flow.para(issue, 9, bullet="\u2022", gap=1)
        flow.space(4)
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
    flow.heading("Conditions", 10, MUTED, before=4)
    flow.kv(rows)
    if run.conditions:
        flow.table(Table(["Instrument", "At the start (* = drive away from safe)"], [list(c) for c in run.conditions]), 8)
    if run.events:
        flow.heading("Approvals and alarms", 10, MUTED, before=2)
        flow.kv(run.events, 8.5, key_w=60)
    if run.plan_text:
        flow.heading("Plan", 10, MUTED, before=2)
        flow.code(run.plan_text)


def write_pdf(report: Report, path: Path) -> int:
    """Write the report; returns the number of pages."""
    flow = Flow(report.title)
    flow.y += 16
    flow.c.text(MARGIN, flow.y, report.title, 22, "bold")
    flow.y += 18
    flow.c.text(MARGIN, flow.y, report.subtitle, 10.5, color=MUTED)
    flow.y += 14
    flow.c.text(MARGIN, flow.y, "%s%s  \u00b7  made with Kelvin %s" % (
        report.lab, " (simulated instruments)" if report.simulated else "", report.created), 8.5, color=MUTED)
    flow.y += 8
    flow.rule()
    if report.summary:
        flow.heading("Summary", 12, before=6)
        for p in report.summary:
            flow.para(p, 10.5, gap=6)
    flow.heading("Runs" if report.summary else "Summary", 12, before=6)
    for line in report.run_lines:
        flow.para(line, 9.5, bullet="\u2022", gap=2)
    if report.runs:
        flow.space(4)
        flow.table(Table(["#", "Run", "When", "Points", "Status"],
                         [[str(i), r.plan, r.when, r.points, r.status] for i, r in enumerate(report.runs, 1)]))
    if report.notes:
        flow.heading("Project notes", 12, before=6)
        for kind, text in report.notes:
            flow.para("%s: %s" % (kind.capitalize(), text), 9.5, bullet="\u2022", gap=2)
    for i, run in enumerate(report.runs, 1):
        flow.rule()
        _run(flow, run, i)
    if report.references:
        flow.rule()
        flow.heading("References", 12, before=6)
        for i, ref in enumerate(report.references, 1):
            flow.para(ref, 9, bullet="[%d]" % i, gap=2, indent=0)
    if report.log:
        flow.rule()
        flow.heading("Lab log", 12, before=6)
        flow.kv(report.log, 8.5, key_w=78)
    return flow.finish(path, report.subtitle)
