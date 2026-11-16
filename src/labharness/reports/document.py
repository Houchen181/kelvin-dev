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
something to say. The PDFs have the look the user chose (2026-11-15, option 14): see pdf.py."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

from .chart import MUTED, Chart, Table, plot_title
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
    callout: bool = False                                   # the PDF sets its words in a box (problems, questions)

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
    measured: str = ""      # when the data was taken (a scan): Nov 15 2026, 08:06 (18 s)
    source: str = ""        # where it comes from, at the foot of its slides: Run 2026...-gate_sweep · D3 · lab
    byline: str = ""        # the PDF's line under the title (the subtitle, less what its header says elsewhere)
    period: str = ""        # when the data was taken (device and master reports)
    metrics: List[Tuple[str, str]] = field(default_factory=list)   # key numbers: the value, what it is


KIND_LABEL = {"scan": "Scan report", "device": "Device report", "project": "Master report"}


# ------------------------------------------------------------------ PDF

SHOWN_ABOVE = {"Project", "Material", "Scans", "Period"}   # facts a PDF's header says already


def _foot(doc: Document) -> str:
    return "%s%s  ·  made with Kelvin, %s" % (doc.lab, " (simulated instruments)" if doc.simulated else "", doc.created)


def _head(flow: "P.Flow", doc: Document) -> None:
    flow.header(KIND_LABEL.get(doc.kind, "Report"), doc.title, doc.byline or doc.subtitle,
                [doc.lab, doc.measured or doc.period], "Simulated data" if doc.simulated else "")
    flow.metrics(doc.metrics)


def _plot_card(chart: Chart) -> "P.Card":
    """A plot of a device or master report: named "gate_sweep, Nov 15 2026 08:06: lockin.X", its card says what
    was measured and, under it, which scan."""
    where, _, what = chart.title.rpartition(": ")
    short = Chart(what or chart.title, chart.x_key, chart.x_unit, chart.y_unit, chart.series, chart.caption,
                  chart.summary)
    title = plot_title(short)
    return P.Card(short, title if len(title) <= 34 else short.title, where)


def _section(flow: "P.Flow", s: Section, title: str) -> None:
    flow.h2(title, s.lead, keep=60)
    if s.is_empty:
        flow.para(s.empty, 10, P.STONE)
        return
    if s.callout:
        flow.callout("", list(s.paras) + list(s.bullets))
    else:
        for p in s.paras:
            flow.para(p, 10.5, P.DARK_WARM, 8)
        if s.bullets:
            flow.bullets(s.bullets)
    if s.facts:
        flow.kv(s.facts)
    for t in s.tables:
        flow.table(t)
    if s.charts:
        flow.cards([_plot_card(c) for c in s.charts])
    if s.code:
        flow.code(s.code)


def _compact_flow(doc: Document, tight: int) -> "P.Flow":
    flow = P.Flow(doc.title, _foot(doc), tight)
    _head(flow, doc)
    for k, s in enumerate([s for s in doc.sections if not s.is_empty]):
        if k == 0 and (s.paras or s.charts):   # the result: what was found, then each plot with its numbers
            flow.h2(s.title, s.lead, keep=60)
            for j, p in enumerate(s.paras):
                flow.lead(p, last=j == len(s.paras) - 1)
            if s.bullets:
                flow.callout("", s.bullets)
            tables = list(s.tables)
            flow.cards([P.card_for(c, tables, " ".join(s.paras)) for c in s.charts])
            for t in tables:
                flow.table(t)
            if s.facts:
                flow.kv(s.facts)
            continue
        _section(flow, s, s.title)
    if doc.note:
        flow.note(doc.note)
    return flow


def _compact_pdf(doc: Document, path: Path) -> int:
    """The scan report: one page, smaller plots and type if it takes that to fit."""
    flow = _compact_flow(doc, 0)
    for tight in (1, 2):
        if len(flow.pages) == 1:
            break
        flow = _compact_flow(doc, tight)
    return flow.finish(path, "%s: %s" % (KIND_LABEL.get(doc.kind, "Report"), doc.title))


def write_document_pdf(doc: Document, path: Path) -> int:
    if doc.compact:
        return _compact_pdf(doc, path)
    flow = P.Flow(doc.title, _foot(doc))
    _head(flow, doc)
    facts = [(k, v) for k, v in doc.facts if k not in SHOWN_ABOVE]
    if facts:
        flow.kv(facts, 9)
    for i, s in enumerate(doc.sections, 1):
        _section(flow, s, "%d. %s" % (i, s.title))
    return flow.finish(path, "%s: %s" % (KIND_LABEL.get(doc.kind, "Report"), doc.subtitle))


# ------------------------------------------------------------------ slides

def _section_slides(s: X.Slide, sec: Section, number: int, n: int, footer: str, page: int,
                    kicker: str = "") -> List[X.Slide]:
    title = "%d. %s" % (number, sec.title) if number else sec.title
    slides: List[X.Slide] = []
    text: List[X.Para] = [X._clip(p, 400) for p in sec.paras[:3]]
    text += [X._clip(b, 160) for b in sec.bullets[:10]]
    text += [[(k + "  ", {"bold": True, "color": MUTED}), (X._clip(v, 140), {})] for k, v in sec.facts[:10]]
    if sec.is_empty:
        y = X._frame(s, title, sec.lead, page, footer, kicker)
        s.text(X.M, y + 10, X.SLIDE_W - 2 * X.M, 60, [sec.empty], 16, MUTED, name="Empty")
        return [s]
    if sec.charts:   # the first chart large, the words and first table beside it
        y = X._frame(s, title, sec.lead, page, footer, kicker)
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
            y = X._frame(more, title + " (continued)", sec.lead, page + len(slides), footer, kicker)
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
        y = X._frame(s, title, sec.lead, page, footer, kicker)
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
        y = X._frame(more, title + (": " + t.title if t.title else " (continued)"), sec.lead, page + len(slides), footer, kicker)
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
    words: List[X.Para] = []
    if first is not None:
        words = [X._clip(p, 500) for p in first.paras[:2]] + [X._clip(b, 200) for b in first.bullets[:4]]
    lab = "%s%s" % (doc.lab, " (simulated instruments)" if doc.simulated else "")
    slides: List[X.Slide] = [X.title_slide(label, doc.title, [doc.subtitle], words,
                                           [lab] + ([doc.note] if doc.note else []))]
    footer = doc.source or "%s  ·  %s" % (label, doc.title)
    kicker = doc.title
    sub = X.measured(doc.measured)
    rest = [sec for sec in sections if sec is not first]
    if len(rest) > 1 and not any(sec.charts or sec.tables or sec.code for sec in rest) and \
            sum(len(sec.facts) + len(sec.bullets) + len(sec.paras) for sec in rest) <= 10:   # one slide for the words
        rest = [Section(rest[0].title + "; " + "; ".join(sec.title.lower() for sec in rest[1:]),
                        paras=[p for sec in rest for p in sec.paras], bullets=[b for sec in rest for b in sec.bullets],
                        facts=[f for sec in rest for f in sec.facts])]
    for sec in [first] + rest if first is not None else []:
        if sec is not first:
            slides += _section_slides(X.Slide(), sec, 0, len(sections), footer, len(slides) + 1, kicker)
            continue
        tables = list(sec.tables)
        for k, chart in enumerate(sec.charts):   # each plot with its result beside it (the academic skill's look)
            more = [[(chart.caption, {"color": MUTED, "size": 11})]] if chart.caption else []
            slides.append(X.plot_slide(chart, X.plot_title(chart), kicker, sub, footer, len(slides) + 1,
                                       chart.summary or (sec.paras[0] if k == 0 and sec.paras else ""),
                                       X.matching(chart, tables), more=more))
        for t in tables[:3]:
            sl = X.Slide()
            y = X._frame(sl, t.title or sec.title, sub, len(slides) + 1, footer, kicker)
            shown = Table(t.header, t.rows[:int((X.SLIDE_H - y - 50) // 22) - 1], "")
            sl.table(X.M, y, X._table_widths(shown, X.SLIDE_W - 2 * X.M, 12), shown, 12, 22)
            slides.append(sl)
    X.save_slides(slides, path, doc.title)
    return len(slides)


def write_document_pptx(doc: Document, path: Path) -> int:
    if doc.compact:
        return _compact_pptx(doc, path)
    slides: List[X.Slide] = []
    facts: List[X.Para] = [[(k + "  ", {"bold": True, "color": "9CC3F0", "size": 14}), (X._clip(v, 120), {"size": 14})]
                           for k, v in doc.facts[:7]]
    slides.append(X.title_slide(KIND_LABEL.get(doc.kind, "Report"), doc.title, [doc.subtitle], facts,
                                ["%s%s, made with Kelvin %s" % (doc.lab, " (simulated instruments)" if doc.simulated
                                                                else "", doc.created)]))
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
