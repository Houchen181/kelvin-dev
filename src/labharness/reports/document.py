"""Structured reports: a document is a fixed list of numbered sections, the same for every report of a kind.

The three kinds (kinds.py) follow the project -> device (sample) -> scan hierarchy:
    scan     one run          Result, Conditions, Approvals and alarms, In the run's folder (compact: one page)
    device   one sample       The device, Mount history, Results by measurement, Trends, Ranges explored,
                              Problems, Scan index
    project  the whole study  Summary, Goals and decisions, Devices compared, Main results, Timeline,
                              Open questions and issues, References, Index
A section that has nothing to show still appears, with a line saying so, so readers always find things in the same
place. The PDF has one heading per section; the slides one or more slides per section, numbered the same way.
A compact document (the scan report) is short instead: no contents, no numbers, and only the sections that have
something to say."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

from .chart import MUTED, Chart, Table
from . import pdf as P
from . import pptx as X


@dataclass
class Section:
    title: str
    lead: str = ""                                          # one muted line under the heading
    paras: List[str] = field(default_factory=list)
    bullets: List[str] = field(default_factory=list)
    facts: List[Tuple[str, str]] = field(default_factory=list)
    tables: List[Table] = field(default_factory=list)
    charts: List[Chart] = field(default_factory=list)
    code: str = ""
    empty: str = "Nothing yet."                             # said when the section has nothing to show

    @property
    def is_empty(self) -> bool:
        return not (self.paras or self.bullets or self.facts or self.tables or self.charts or self.code)


@dataclass
class Document:
    kind: str                                               # scan, device or project
    title: str
    subtitle: str
    created: str
    lab: str
    simulated: bool
    facts: List[Tuple[str, str]] = field(default_factory=list)   # the key facts under the title
    sections: List[Section] = field(default_factory=list)
    compact: bool = False   # short: no contents, no numbers, no empty sections (the scan report)
    note: str = ""          # a muted line at the end (compact): where the rest is


KIND_LABEL = {"scan": "Scan report", "device": "Device report", "project": "Master report"}


# ------------------------------------------------------------------ PDF

def _compact_pdf(doc: Document, path: Path) -> int:
    flow = P.Flow(doc.title)
    flow.y += 12
    flow.c.text(P.MARGIN, flow.y, KIND_LABEL.get(doc.kind, "Report").upper(), 8, "bold", "2a78d6")
    flow.y += 17
    flow.c.text(P.MARGIN, flow.y, doc.title, 18, "bold")
    flow.y += 4
    flow.para(doc.subtitle, 9.5, color=MUTED, gap=0)
    flow.para("%s%s" % (doc.lab, " (simulated instruments)" if doc.simulated else ""), 8.5, color=MUTED, gap=2)
    for s in doc.sections:
        if s.is_empty:
            continue
        flow.rule()
        flow.heading(s.title, 11.5, before=2)
        for p in s.paras:
            flow.para(p, 10, gap=4)
        for b in s.bullets:
            flow.para(b, 9.5, bullet="•", gap=2)
        if s.facts:
            flow.kv(s.facts, 9, key_w=96)
        if s.charts:
            flow.charts(s.charts, height=160)
        flow.tables(s.tables)
        if s.code:
            flow.code(s.code)
    if doc.note:
        flow.rule()
        flow.para(doc.note, 8, color=MUTED, gap=0)
    return flow.finish(path, "%s: %s" % (KIND_LABEL.get(doc.kind, "Report"), doc.title))


def write_document_pdf(doc: Document, path: Path) -> int:
    if doc.compact:
        return _compact_pdf(doc, path)
    flow = P.Flow(doc.title)
    flow.y += 12
    flow.c.text(P.MARGIN, flow.y, KIND_LABEL.get(doc.kind, "Report").upper(), 8.5, "bold", "2a78d6")
    flow.y += 18
    flow.c.text(P.MARGIN, flow.y, doc.title, 22, "bold")
    flow.y += 16
    flow.c.text(P.MARGIN, flow.y, doc.subtitle, 10.5, color=MUTED)
    flow.y += 14
    flow.c.text(P.MARGIN, flow.y, "%s%s  ·  made with Kelvin %s" % (
        doc.lab, " (simulated instruments)" if doc.simulated else "", doc.created), 8.5, color=MUTED)
    flow.y += 8
    flow.rule()
    if doc.facts:
        flow.kv(doc.facts, 9, key_w=96)
    if len(doc.sections) > 3:
        flow.heading("Contents", 10, MUTED, before=4)
        for i, s in enumerate(doc.sections, 1):
            flow.para("%d. %s" % (i, s.title), 9, color=MUTED, gap=0)
        flow.space(4)
    for i, s in enumerate(doc.sections, 1):
        flow.rule()
        flow.heading("%d. %s" % (i, s.title), 13, before=8)
        if s.lead:
            flow.para(s.lead, 8.5, "italic", MUTED, gap=4)
        if s.is_empty:
            flow.para(s.empty, 9.5, "italic", MUTED, gap=4)
            continue
        for p in s.paras:
            flow.para(p, 10, gap=5)
        for b in s.bullets:
            flow.para(b, 9.5, bullet="•", gap=2)
        if s.bullets:
            flow.space(3)
        if s.facts:
            flow.kv(s.facts, 9)
        for t in s.tables:
            flow.table(t)
        if s.charts:
            flow.charts(s.charts)
        if s.code:
            flow.code(s.code)
    return flow.finish(path, "%s: %s" % (KIND_LABEL.get(doc.kind, "Report"), doc.subtitle))


# ------------------------------------------------------------------ slides

def _section_slides(s: X.Slide, sec: Section, number: int, n: int, footer: str, page: int) -> List[X.Slide]:
    title = "%d. %s" % (number, sec.title) if number else sec.title
    slides: List[X.Slide] = []
    text: List[X.Para] = [X._clip(p, 400) for p in sec.paras[:3]]
    text += [X._clip(b, 160) for b in sec.bullets[:10]]
    text += [[(k + "  ", {"bold": True, "color": MUTED}), (X._clip(v, 140), {})] for k, v in sec.facts[:10]]
    if sec.is_empty:
        y = X._frame(s, title, sec.lead, page, footer)
        s.text(X.M, y + 10, X.SLIDE_W - 2 * X.M, 60, [sec.empty], 16, MUTED, name="Empty")
        return [s]
    if sec.charts:   # the first chart large, the words and first table beside it
        y = X._frame(s, title, sec.lead, page, footer)
        right_x = 600.0
        s.chart(sec.charts[0], X.M, y + 4, right_x - X.M - 24, X.SLIDE_H - y - 48, 10)
        ty = y + 4
        if sec.tables:
            t = sec.tables[0]
            t = Table(t.header[:3], [r[:3] for r in t.rows[:8]], t.title)
            ty += s.table(right_x, ty, X._table_widths(t, X.SLIDE_W - X.M - right_x, 11), t, 11, 21) + 12
        if text:
            s.text(right_x, ty, X.SLIDE_W - X.M - right_x, X.SLIDE_H - ty - 40, text, 12, space=4, shrink=True,
                   name="Text")
        slides.append(s)
        rest = sec.charts[1:]
        for k in range(0, len(rest), 4):
            more = X.Slide()
            y = X._frame(more, title + " (continued)", sec.lead, page + len(slides), footer)
            group = rest[k:k + 4]
            cols = 1 if len(group) == 1 else 2
            rows = (len(group) + 1) // 2
            w = (X.SLIDE_W - 2 * X.M - 30) / cols
            h = (X.SLIDE_H - y - 40 - 16 * (rows - 1)) / rows
            for j, chart in enumerate(group):
                more.chart(chart, X.M + (j % cols) * (w + 30), y + (j // cols) * (h + 16), w, h, 9)
            slides.append(more)
        tables = sec.tables[1:]
    else:
        y = X._frame(s, title, sec.lead, page, footer)
        if text:
            height = min(X.SLIDE_H - y - 40, 26.0 * len(text) + 10) if sec.tables else X.SLIDE_H - y - 40
            s.text(X.M, y, X.SLIDE_W - 2 * X.M, height, text, 15 if not sec.tables else 13, bullet=bool(sec.bullets),
                   space=5, shrink=True, name="Text")
            y += height + 10
        tables = list(sec.tables)
        if tables and y < X.SLIDE_H - 120:
            t = tables.pop(0)
            t = Table(t.header, t.rows[:max(3, int((X.SLIDE_H - y - 50) // 22) - 1)], t.title)
            s.table(X.M, y, X._table_widths(t, X.SLIDE_W - 2 * X.M, 11), t, 11, 21)
        slides.append(s)
    for t in tables[:3]:
        more = X.Slide()
        y = X._frame(more, title + (": " + t.title if t.title else " (continued)"), sec.lead, page + len(slides), footer)
        t = Table(t.header, t.rows[:int((X.SLIDE_H - y - 50) // 22) - 1], "")
        more.table(X.M, y, X._table_widths(t, X.SLIDE_W - 2 * X.M, 11), t, 11, 21)
        slides.append(more)
    return slides


def _compact_pptx(doc: Document, path: Path) -> int:
    """A title slide that says the result in words, one slide per plot with its fitted values beside it, then the
    other sections that have something to say."""
    sections = [sec for sec in doc.sections if not sec.is_empty]
    first = sections[0] if sections else None
    label = KIND_LABEL.get(doc.kind, "Report")
    s = X.Slide()
    s.rect(0, 0, X.SLIDE_W, X.SLIDE_H, "F3F5F8")
    s.rect(0, 0, 10, X.SLIDE_H, X.ACCENT)
    s.text(70, 56, X.SLIDE_W - 140, 24, [[(label.upper(), {"bold": True, "color": X.ACCENT})]], 13, name="Kind")
    s.text(70, 82, X.SLIDE_W - 140, 64, [[(doc.title, {"bold": True})]], 34, anchor="b", shrink=True, name="Title")
    s.text(70, 152, X.SLIDE_W - 140, 44, [doc.subtitle], 15, MUTED, shrink=True, name="Subtitle")
    if first is not None:
        words = [X._clip(p, 500) for p in first.paras[:2]] + [X._clip(b, 200) for b in first.bullets[:4]]
        s.text(70, 214, X.SLIDE_W - 140, X.SLIDE_H - 290, words, 18, space=8, shrink=True, name="Result")
    s.text(70, X.SLIDE_H - 76, X.SLIDE_W - 140, 40, ["%s%s" % (doc.lab, " (simulated instruments)" if doc.simulated
                                                                  else "")] + ([doc.note] if doc.note else []),
           11, MUTED, space=2, shrink=True, name="Lab")
    slides: List[X.Slide] = [s]
    footer = "%s  ·  %s" % (label, doc.title)
    rest = [sec for sec in sections if sec is not first]
    if len(rest) > 1 and not any(sec.charts or sec.tables or sec.code for sec in rest) and \
            sum(len(sec.facts) + len(sec.bullets) + len(sec.paras) for sec in rest) <= 10:   # one slide for the words
        rest = [Section(rest[0].title + "; " + "; ".join(sec.title.lower() for sec in rest[1:]),
                        paras=[p for sec in rest for p in sec.paras], bullets=[b for sec in rest for b in sec.bullets],
                        facts=[f for sec in rest for f in sec.facts])]
    for sec in [first] + rest if first is not None else []:
        if sec is not first:
            slides += _section_slides(X.Slide(), sec, 0, len(sections), footer, len(slides) + 1)
            continue
        tables = list(sec.tables)
        for chart in sec.charts:
            subject = chart.title.split(" (")[0]
            t = next((t for t in tables if t.title.split(" vs ")[0] == subject), None)
            if t is not None:
                tables.remove(t)
            sl = X.Slide()
            y = X._frame(sl, chart.title, chart.caption, len(slides) + 1, footer)
            if t is None:
                sl.chart(chart, X.M, y + 4, X.SLIDE_W - 2 * X.M, X.SLIDE_H - y - 48, 11)
            else:
                right_x = 600.0
                sl.chart(chart, X.M, y + 4, right_x - X.M - 24, X.SLIDE_H - y - 48, 10)
                shown = Table(t.header[:3], [r[:3] for r in t.rows[:10]], t.title)
                sl.table(right_x, y + 4, X._table_widths(shown, X.SLIDE_W - X.M - right_x, 12), shown, 12, 22)
            slides.append(sl)
        for t in tables[:3]:
            sl = X.Slide()
            y = X._frame(sl, t.title or sec.title, "", len(slides) + 1, footer)
            shown = Table(t.header, t.rows[:int((X.SLIDE_H - y - 50) // 22) - 1], "")
            sl.table(X.M, y, X._table_widths(shown, X.SLIDE_W - 2 * X.M, 12), shown, 12, 22)
            slides.append(sl)
    X.save_slides(slides, path, doc.title)
    return len(slides)


def write_document_pptx(doc: Document, path: Path) -> int:
    if doc.compact:
        return _compact_pptx(doc, path)
    slides: List[X.Slide] = []
    s = X.Slide()
    s.rect(0, 0, X.SLIDE_W, X.SLIDE_H, "F3F5F8")
    s.rect(0, 0, 10, X.SLIDE_H, X.ACCENT)
    s.text(70, 120, X.SLIDE_W - 140, 24, [[(KIND_LABEL.get(doc.kind, "Report").upper(), {"bold": True,
                                                                                         "color": X.ACCENT})]],
           13, name="Kind")
    s.text(70, 150, X.SLIDE_W - 140, 90, [[(doc.title, {"bold": True})]], 40, anchor="b", shrink=True, name="Title")
    s.text(70, 252, X.SLIDE_W - 140, 30, [doc.subtitle], 18, MUTED, name="Subtitle")
    facts = [[(k + "  ", {"bold": True, "color": MUTED}), (X._clip(v, 120), {})] for k, v in doc.facts[:7]]
    s.text(70, 296, X.SLIDE_W - 140, 170, facts, 13, space=2, shrink=True, name="Facts")
    s.text(70, X.SLIDE_H - 60, X.SLIDE_W - 140, 20, ["%s%s, made with Kelvin %s" % (
        doc.lab, " (simulated instruments)" if doc.simulated else "", doc.created)], 11, MUTED, name="Made with")
    slides.append(s)
    footer = "%s  ·  %s" % (KIND_LABEL.get(doc.kind, "Report"), doc.title)
    if len(doc.sections) > 3:
        s = X.Slide()
        y = X._frame(s, "Contents", doc.subtitle, 2, footer)
        s.text(X.M, y, X.SLIDE_W - 2 * X.M, X.SLIDE_H - y - 40,
               [[("%d  " % i, {"bold": True, "color": X.ACCENT}), (sec.title, {})] for i, sec in enumerate(doc.sections, 1)],
               18, space=6, shrink=True, name="Contents")
        slides.append(s)
    for i, sec in enumerate(doc.sections, 1):
        slides += _section_slides(X.Slide(), sec, i, len(doc.sections), footer, len(slides) + 1)
    X.save_slides(slides, path, doc.title)
    return len(slides)
