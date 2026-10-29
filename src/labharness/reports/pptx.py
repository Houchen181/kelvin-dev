"""A small PowerPoint (.pptx) writer and the report laid out as slides.

Plots are drawn with native shapes (grouped, so a plot moves as one piece) and results are native tables, so
everything stays editable in PowerPoint, Keynote or Google Slides. No third-party package is needed."""
from __future__ import annotations

import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple, Union

from .chart import AXIS, GRID, INK, MUTED, Chart, Geometry, Table, layout
from .model import Report, RunPart

EMU = 12700                     # per point
SLIDE_W, SLIDE_H = 960.0, 540.0  # 16:9, points
FONT = "Calibri"
MONO = "Consolas"
ACCENT = "2a78d6"

NS = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
      'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"')
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")

Run = Tuple[str, dict]           # text and its style: bold, italic, color, size, mono
Para = Union[str, List[Run]]


def esc(s: Any) -> str:
    return _BAD.sub("", str(s)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def emu(v: float) -> int:
    return int(round(v * EMU))


def measure(text: str, size: float) -> float:
    """Calibri is narrow: about half an em per character on average."""
    wide = sum(1 for c in text if c in "MWmw@%")
    return (len(text) * 0.5 + wide * 0.3) * size


def _xfrm(x: float, y: float, w: float, h: float, tag: str = "a:xfrm") -> str:
    return '<%s><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></%s>' % (tag, emu(x), emu(y), max(1, emu(w)), max(1, emu(h)), tag)


def _ln(color: Optional[str], width: float = 0.75, dash: bool = False) -> str:
    if not color:
        return "<a:ln><a:noFill/></a:ln>"
    return '<a:ln w="%d"><a:solidFill><a:srgbClr val="%s"/></a:solidFill>%s<a:round/></a:ln>' % (
        emu(width), color.upper(), '<a:prstDash val="dash"/>' if dash else "")


def _rpr(style: dict, size: float, color: str) -> str:
    attrs = 'lang="en-US" sz="%d"' % round((style.get("size") or size) * 100)
    if style.get("bold"):
        attrs += ' b="1"'
    if style.get("italic"):
        attrs += ' i="1"'
    font = '<a:latin typeface="%s"/>' % (MONO if style.get("mono") else FONT)
    return '<a:rPr %s dirty="0"><a:solidFill><a:srgbClr val="%s"/></a:solidFill>%s</a:rPr>' % (
        attrs, (style.get("color") or color).upper(), font)


def _paras(paras: Sequence[Para], size: float, color: str, align: str = "l", bullet: bool = False,
           space: float = 0) -> str:
    out = []
    for p in paras:
        runs: List[Run] = [(p, {})] if isinstance(p, str) else p
        ppr = '<a:pPr algn="%s"%s>%s%s</a:pPr>' % (
            align, ' marL="%d" indent="%d"' % (emu(size * 0.9), -emu(size * 0.9)) if bullet else "",
            '<a:spcBef><a:spcPts val="%d"/></a:spcBef>' % round(space * 100) if space else "",
            '<a:buFont typeface="Arial"/><a:buChar char="&#8226;"/>' if bullet else "<a:buNone/>")
        body = "".join('<a:r>%s<a:t>%s</a:t></a:r>' % (_rpr(st, size, color), esc(t)) for t, st in runs if t != "")
        out.append("<a:p>%s%s<a:endParaRPr lang=\"en-US\" sz=\"%d\"/></a:p>" % (ppr, body, round(size * 100)))
    return "".join(out) or '<a:p><a:endParaRPr lang="en-US"/></a:p>'


class Slide:
    def __init__(self) -> None:
        self.shapes: List[str] = []
        self._id = 1
        self._groups: List[List[str]] = []

    def _next(self) -> int:
        self._id += 1
        return self._id

    def _add(self, xml: str) -> None:
        (self._groups[-1] if self._groups else self.shapes).append(xml)

    def text(self, x: float, y: float, w: float, h: float, paras: Sequence[Para], size: float = 14,
             color: str = INK, align: str = "l", anchor: str = "t", bullet: bool = False, space: float = 0,
             shrink: bool = False, name: str = "Text") -> None:
        i = self._next()
        fit = "<a:normAutofit/>" if shrink else "<a:noAutofit/>"
        self._add('<p:sp><p:nvSpPr><p:cNvPr id="%d" name="%s %d"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
                  '<p:spPr>%s<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
                  '<p:txBody><a:bodyPr wrap="square" lIns="0" tIns="0" rIns="0" bIns="0" anchor="%s">%s</a:bodyPr>'
                  '<a:lstStyle/>%s</p:txBody></p:sp>'
                  % (i, esc(name), i, _xfrm(x, y, w, h), anchor, fit, _paras(paras, size, color, align, bullet, space)))

    def rect(self, x: float, y: float, w: float, h: float, fill: Optional[str], line: Optional[str] = None,
             radius: bool = False) -> None:
        i = self._next()
        fill_xml = '<a:solidFill><a:srgbClr val="%s"/></a:solidFill>' % fill.upper() if fill else "<a:noFill/>"
        self._add('<p:sp><p:nvSpPr><p:cNvPr id="%d" name="Box %d"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>%s'
                  '<a:prstGeom prst="%s"><a:avLst/></a:prstGeom>%s%s</p:spPr></p:sp>'
                  % (i, i, _xfrm(x, y, w, h), "roundRect" if radius else "rect", fill_xml, _ln(line)))

    def line(self, x1: float, y1: float, x2: float, y2: float, color: str, width: float = 0.75,
             dash: bool = False) -> None:
        """Horizontal or vertical."""
        i = self._next()
        x, y = min(x1, x2), min(y1, y2)
        self._add('<p:cxnSp><p:nvCxnSpPr><p:cNvPr id="%d" name="Line %d"/><p:cNvCxnSpPr/><p:nvPr/></p:nvCxnSpPr>'
                  '<p:spPr>%s<a:prstGeom prst="line"><a:avLst/></a:prstGeom>%s</p:spPr></p:cxnSp>'
                  % (i, i, _xfrm(x, y, abs(x2 - x1), abs(y2 - y1)), _ln(color, width, dash)))

    def path(self, segments: Sequence[Sequence[Tuple[float, float]]], color: str, width: float = 1.25,
             dash: bool = False, dots: float = 0) -> None:
        """Polylines (dots=0) or filled dots of radius `dots` at every point, as one freeform shape."""
        pts = [p for seg in segments for p in seg]
        if not pts:
            return
        pad = dots
        x0, y0 = min(p[0] for p in pts) - pad, min(p[1] for p in pts) - pad
        w, h = max(p[0] for p in pts) + pad - x0, max(p[1] for p in pts) + pad - y0
        cw, ch = max(1, emu(w)), max(1, emu(h))

        def pt(x: float, y: float) -> str:
            return '<a:pt x="%d" y="%d"/>' % (min(cw, max(0, emu(x - x0))), min(ch, max(0, emu(y - y0))))

        parts = []
        if dots:
            r = emu(dots)
            for x, y in pts:
                parts.append('<a:moveTo>%s</a:moveTo><a:arcTo wR="%d" hR="%d" stAng="0" swAng="21600000"/><a:close/>'
                             % (pt(x + dots, y), r, r))
            body = '<a:path w="%d" h="%d">%s</a:path>' % (cw, ch, "".join(parts))
            style = '<a:solidFill><a:srgbClr val="%s"/></a:solidFill>%s' % (color.upper(), _ln("FFFFFF", 0.5))
        else:
            for seg in segments:
                if len(seg) < 2:
                    continue
                parts.append("<a:moveTo>%s</a:moveTo>" % pt(*seg[0]) + "".join("<a:lnTo>%s</a:lnTo>" % pt(*p) for p in seg[1:]))
            if not parts:
                return
            body = '<a:path w="%d" h="%d" fill="none">%s</a:path>' % (cw, ch, "".join(parts))
            style = "<a:noFill/>" + _ln(color, width, dash)
        i = self._next()
        self._add('<p:sp><p:nvSpPr><p:cNvPr id="%d" name="%s %d"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>%s'
                  '<a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/><a:rect l="0" t="0" r="r" b="b"/>'
                  '<a:pathLst>%s</a:pathLst></a:custGeom>%s</p:spPr></p:sp>'
                  % (i, "Points" if dots else "Curve", i, _xfrm(x0, y0, w, h), body, style))

    def table(self, x: float, y: float, widths: Sequence[float], t: Table, size: float = 11,
              row_h: float = 20) -> float:
        """Returns the table's height."""
        i = self._next()
        cols = len(t.header)
        rows_xml = []
        for r, cells in enumerate([t.header] + t.rows):
            head = r == 0
            tcs = []
            for c in range(cols):
                text = cells[c] if c < len(cells) else ""
                border = '<a:lnB w="%d"><a:solidFill><a:srgbClr val="%s"/></a:solidFill></a:lnB>' % (
                    emu(0.75), GRID.upper())
                fill = '<a:solidFill><a:srgbClr val="F1F3F6"/></a:solidFill>' if head else "<a:noFill/>"
                tcs.append('<a:tc><a:txBody><a:bodyPr/><a:lstStyle/>%s</a:txBody>'
                           '<a:tcPr marL="%d" marR="%d" marT="%d" marB="%d" anchor="ctr">'
                           '<a:lnL w="0"><a:noFill/></a:lnL><a:lnR w="0"><a:noFill/></a:lnR>'
                           '<a:lnT w="0"><a:noFill/></a:lnT>%s%s</a:tcPr></a:tc>'
                           % (_paras([[(text, {"bold": head, "color": MUTED if head else INK})]], size, INK),
                              emu(5), emu(5), emu(2), emu(2), border, fill))
            rows_xml.append('<a:tr h="%d">%s</a:tr>' % (emu(row_h), "".join(tcs)))
        grid = "".join('<a:gridCol w="%d"/>' % emu(w) for w in widths)
        height = row_h * (len(t.rows) + 1)
        self._add('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="%d" name="Table %d"/><p:cNvGraphicFramePr>'
                  '<a:graphicFrameLocks noGrp="1"/></p:cNvGraphicFramePr><p:nvPr/></p:nvGraphicFramePr>%s'
                  '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl>'
                  '<a:tblPr firstRow="1"/><a:tblGrid>%s</a:tblGrid>%s</a:tbl></a:graphicData></a:graphic>'
                  '</p:graphicFrame>' % (i, i, _xfrm(x, y, sum(widths), height, "p:xfrm"), grid, "".join(rows_xml)))
        return height

    def chart(self, chart: Chart, x: float, y: float, w: float, h: float, font: float = 10) -> None:
        geo = layout(chart, w, h, measure, font)
        self._groups.append([])
        draw_chart(self, geo, x, y)
        children = self._groups.pop()
        i = self._next()
        self._add('<p:grpSp><p:nvGrpSpPr><p:cNvPr id="%d" name="Plot %s"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
                  '<p:grpSpPr><a:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/><a:chOff x="%d" y="%d"/>'
                  '<a:chExt cx="%d" cy="%d"/></a:xfrm></p:grpSpPr>%s</p:grpSp>'
                  % (i, esc(chart.title), emu(x), emu(y), emu(w), emu(h), emu(x), emu(y), emu(w), emu(h),
                     "".join(children)))

    def xml(self) -> str:
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sld %s><p:cSld><p:spTree>'
                '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm>'
                '<a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm>'
                '</p:grpSpPr>%s</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>'
                % (NS, "".join(self.shapes)))


def draw_chart(s: Slide, geo: Geometry, x0: float, y0: float) -> None:
    f = geo.font
    left, top, pw, ph = geo.box
    s.text(x0, y0, left + pw, f * 1.6, [[(geo.title, {"bold": True})]], f * 1.15, name="Plot title")
    for name, color, dashed, (lx, ly) in geo.legend:
        s.line(x0 + lx, y0 + ly, x0 + lx + f * 2.2, y0 + ly, color, 1.5, dashed)
        s.text(x0 + lx + f * 2.7, y0 + ly - f * 0.7, measure(name, f * 0.95) + f * 2, f * 1.4, [name], f * 0.95,
               MUTED, name="Legend")
    for y, label in geo.y_ticks:
        s.line(x0 + left, y0 + y, x0 + left + pw, y0 + y, GRID, 0.5)
        s.text(x0, y0 + y - f * 0.7, left - f * 0.5, f * 1.4, [label], f, MUTED, align="r", name="Tick")
    s.line(x0 + left, y0 + top + ph, x0 + left + pw, y0 + top + ph, AXIS, 0.75)
    for x, label in geo.x_ticks:
        s.line(x0 + x, y0 + top + ph, x0 + x, y0 + top + ph + f * 0.4, AXIS, 0.75)
        s.text(x0 + x - f * 3, y0 + top + ph + f * 0.55, f * 6, f * 1.4, [label], f, MUTED, align="ctr", name="Tick")
    s.text(x0 + left, y0 + top + ph + f * 2.0, pw, f * 1.4, [geo.x_label], f, MUTED, align="ctr", name="Axis")
    for color, dashed, segments in geo.lines:
        s.path([[(x0 + x, y0 + y) for x, y in seg] for seg in segments], color, 1.25 if dashed else 1.6, dashed)
    for color, pts in geo.markers:
        s.path([[(x0 + x, y0 + y) for x, y in pts]], color, dots=2.2)


# ------------------------------------------------------------------ the package around the slides

_THEME = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><a:theme xmlns:a="http://schemas.openxmlformats.org/'
          'drawingml/2006/main" name="Kelvin"><a:themeElements><a:clrScheme name="Kelvin">'
          '<a:dk1><a:srgbClr val="1F2328"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/></a:lt1>'
          '<a:dk2><a:srgbClr val="2A3542"/></a:dk2><a:lt2><a:srgbClr val="F3F5F8"/></a:lt2>'
          '<a:accent1><a:srgbClr val="2A78D6"/></a:accent1><a:accent2><a:srgbClr val="EB6834"/></a:accent2>'
          '<a:accent3><a:srgbClr val="1BAF7A"/></a:accent3><a:accent4><a:srgbClr val="EDA100"/></a:accent4>'
          '<a:accent5><a:srgbClr val="6250D6"/></a:accent5><a:accent6><a:srgbClr val="E34948"/></a:accent6>'
          '<a:hlink><a:srgbClr val="2A78D6"/></a:hlink><a:folHlink><a:srgbClr val="6250D6"/></a:folHlink>'
          '</a:clrScheme><a:fontScheme name="Kelvin"><a:majorFont><a:latin typeface="Calibri"/><a:ea typeface=""/>'
          '<a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/>'
          '<a:cs typeface=""/></a:minorFont></a:fontScheme><a:fmtScheme name="Kelvin"><a:fillStyleLst>'
          + '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>' * 3 + '</a:fillStyleLst><a:lnStyleLst>'
          + '<a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>' * 3
          + '</a:lnStyleLst><a:effectStyleLst>' + '<a:effectStyle><a:effectLst/></a:effectStyle>' * 3
          + '</a:effectStyleLst><a:bgFillStyleLst>' + '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>' * 3
          + '</a:bgFillStyleLst></a:fmtScheme></a:themeElements><a:objectDefaults/><a:extraClrSchemeLst/></a:theme>')

_EMPTY_TREE = ('<p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr>'
               '<a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/>'
               '</a:xfrm></p:grpSpPr></p:spTree>')

_MASTER = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldMaster %s><p:cSld><p:bg><p:bgRef idx="1001">'
           '<a:schemeClr val="bg1"/></p:bgRef></p:bg>%s</p:cSld><p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" '
           'accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" '
           'accent6="accent6" hlink="hlink" folHlink="folHlink"/><p:sldLayoutIdLst><p:sldLayoutId id="2147483649" '
           'r:id="rId1"/></p:sldLayoutIdLst><p:txStyles><p:titleStyle><a:lvl1pPr><a:defRPr sz="3200"/></a:lvl1pPr>'
           '</p:titleStyle><p:bodyStyle><a:lvl1pPr><a:defRPr sz="1800"/></a:lvl1pPr></p:bodyStyle><p:otherStyle>'
           '<a:lvl1pPr><a:defRPr sz="1800"/></a:lvl1pPr></p:otherStyle></p:txStyles></p:sldMaster>' % (NS, _EMPTY_TREE))

_LAYOUT = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldLayout %s type="blank" preserve="1">'
           '<p:cSld name="Blank">%s</p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>'
           % (NS, _EMPTY_TREE))


def _rels(items: Sequence[Tuple[str, str, str]]) -> str:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.'
            'openxmlformats.org/package/2006/relationships">%s</Relationships>'
            % "".join('<Relationship Id="%s" Type="%s" Target="%s"/>' % i for i in items))


def save_slides(slides: List[Slide], path: Path, title: str) -> None:
    n = len(slides)
    ct = "application/vnd.openxmlformats-officedocument.presentationml."
    overrides = [("/ppt/presentation.xml", ct + "presentation.main+xml"),
                 ("/ppt/slideMasters/slideMaster1.xml", ct + "slideMaster+xml"),
                 ("/ppt/slideLayouts/slideLayout1.xml", ct + "slideLayout+xml"),
                 ("/ppt/theme/theme1.xml", "application/vnd.openxmlformats-officedocument.theme+xml"),
                 ("/ppt/presProps.xml", ct + "presProps+xml"), ("/ppt/viewProps.xml", ct + "viewProps+xml"),
                 ("/ppt/tableStyles.xml", ct + "tableStyles+xml"),
                 ("/docProps/core.xml", "application/vnd.openxmlformats-package.core-properties+xml"),
                 ("/docProps/app.xml", "application/vnd.openxmlformats-officedocument.extended-properties+xml")]
    overrides += [("/ppt/slides/slide%d.xml" % (k + 1), ct + "slide+xml") for k in range(n)]
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.'
                     'openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="'
                     'application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" '
                     'ContentType="application/xml"/>%s</Types>'
                     % "".join('<Override PartName="%s" ContentType="%s"/>' % o for o in overrides))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.'
            'openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>%s</dc:title><dc:creator>Kelvin'
            '</dc:creator><dcterms:created xsi:type="dcterms:W3CDTF">%s</dcterms:created><dcterms:modified '
            'xsi:type="dcterms:W3CDTF">%s</dcterms:modified></cp:coreProperties>' % (esc(title), now, now))
    app = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.'
           'org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/'
           '2006/docPropsVTypes"><Application>Kelvin</Application><Slides>%d</Slides></Properties>' % n)
    pres_rels = [("rId1", REL + "slideMaster", "slideMasters/slideMaster1.xml"), ("rId2", REL + "theme", "theme/theme1.xml"),
                 ("rId3", REL + "presProps", "presProps.xml"), ("rId4", REL + "viewProps", "viewProps.xml"),
                 ("rId5", REL + "tableStyles", "tableStyles.xml")]
    pres_rels += [("rId%d" % (k + 10), REL + "slide", "slides/slide%d.xml" % (k + 1)) for k in range(n)]
    presentation = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:presentation %s saveSubsetFonts="1">'
                    '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst><p:sldIdLst>%s'
                    '</p:sldIdLst><p:sldSz cx="%d" cy="%d"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>'
                    % (NS, "".join('<p:sldId id="%d" r:id="rId%d"/>' % (256 + k, k + 10) for k in range(n)),
                       emu(SLIDE_W), emu(SLIDE_H)))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", _rels([("rId1", REL + "officeDocument", "ppt/presentation.xml"),
                                         ("rId2", "http://schemas.openxmlformats.org/package/2006/relationships/"
                                                  "metadata/core-properties", "docProps/core.xml"),
                                         ("rId3", REL + "extended-properties", "docProps/app.xml")]))
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
        z.writestr("ppt/presentation.xml", presentation)
        z.writestr("ppt/_rels/presentation.xml.rels", _rels(pres_rels))
        z.writestr("ppt/presProps.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:presentationPr %s/>' % NS)
        z.writestr("ppt/viewProps.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:viewPr %s>'
                   '<p:normalViewPr><p:restoredLeft sz="15620"/><p:restoredTop sz="94660"/></p:normalViewPr>'
                   '<p:gridSpacing cx="76200" cy="76200"/></p:viewPr>' % NS)
        z.writestr("ppt/tableStyles.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><a:tblStyleLst '
                   'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                   'def="{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}"/>')
        z.writestr("ppt/theme/theme1.xml", _THEME)
        z.writestr("ppt/slideMasters/slideMaster1.xml", _MASTER)
        z.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels",
                   _rels([("rId1", REL + "slideLayout", "../slideLayouts/slideLayout1.xml"),
                          ("rId2", REL + "theme", "../theme/theme1.xml")]))
        z.writestr("ppt/slideLayouts/slideLayout1.xml", _LAYOUT)
        z.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels",
                   _rels([("rId1", REL + "slideMaster", "../slideMasters/slideMaster1.xml")]))
        for k, slide in enumerate(slides, 1):
            z.writestr("ppt/slides/slide%d.xml" % k, slide.xml())
            z.writestr("ppt/slides/_rels/slide%d.xml.rels" % k,
                       _rels([("rId1", REL + "slideLayout", "../slideLayouts/slideLayout1.xml")]))


# ------------------------------------------------------------------ the report as slides

M = 40.0   # slide margin


def _frame(s: Slide, title: str, sub: str = "", number: int = 0, footer: str = "") -> float:
    """Title bar; returns where the content starts."""
    s.rect(0, 0, 6, SLIDE_H, ACCENT)
    s.text(M, 26, SLIDE_W - 2 * M, 34, [[(title, {"bold": True})]], 26, anchor="b", shrink=True, name="Title")
    y = 64
    if sub:
        s.text(M, y, SLIDE_W - 2 * M, 18, [sub], 12, MUTED, name="Subtitle")
        y += 22
    if footer:
        s.text(M, SLIDE_H - 24, SLIDE_W - 2 * M - 40, 14, [footer], 9, MUTED, name="Footer")
    if number:
        s.text(SLIDE_W - M - 40, SLIDE_H - 24, 40, 14, [str(number)], 9, MUTED, align="r", name="Slide number")
    return y + 8


def _table_widths(t: Table, total: float, size: float) -> List[float]:
    natural = [max([measure(t.header[i], size)] + [measure(r[i] if i < len(r) else "", size) for r in t.rows]) + 14
               for i in range(len(t.header))]
    return [w * total / sum(natural) for w in natural] if sum(natural) > total else natural


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _run_slides(run: RunPart, footer: str, start: int) -> List[Slide]:
    slides = []
    s = Slide()
    sub = "%s  ·  %s points  ·  %s  ·  %s" % (run.when, run.points, run.duration, run.status)
    y = _frame(s, run.plan, sub, start, footer)
    right_x = 600.0
    if run.charts:
        s.chart(run.charts[0], M, y + 4, right_x - M - 24, SLIDE_H - y - 48, 10)
    else:
        s.text(M, y + 4, right_x - M - 24, 40, ["No plot: this run recorded %s points." % run.points], 14, MUTED)
    paras: List[Para] = []
    if run.reason and run.status != "done":
        paras.append([("Stopped: " + run.reason, {"bold": True, "color": "B42318"})])
    if run.assessment:
        paras.append(_clip(run.assessment, 420))
    if run.next:
        paras.append([("Next: ", {"bold": True}), (_clip(run.next, 200), {"color": MUTED})])
    ty = y + 4
    if run.results:
        t = run.results[0]
        if len(t.header) > 3:
            t = Table(t.header[:3], [r[:3] for r in t.rows], t.title)
        t = Table(t.header, t.rows[:8], t.title)
        size = 11
        ty += s.table(right_x, ty, _table_widths(t, SLIDE_W - M - right_x, size), t, size, 21) + 14
    s.text(right_x, ty, SLIDE_W - M - right_x, SLIDE_H - ty - 40, paras, 13, space=6, shrink=True, name="Assessment")
    slides.append(s)

    if len(run.charts) > 1:
        s = Slide()
        y = _frame(s, run.plan + ": more plots", sub, start + 1, footer)
        rest = run.charts[1:5]
        cols = 1 if len(rest) == 1 else 2
        rows = (len(rest) + 1) // 2
        w = (SLIDE_W - 2 * M - 30) / cols
        h = (SLIDE_H - y - 40 - 16 * (rows - 1)) / rows
        for k, chart in enumerate(rest):
            s.chart(chart, M + (k % cols) * (w + 30), y + (k // cols) * (h + 16), w, h, 9)
        slides.append(s)

    s = Slide()
    y = _frame(s, run.plan + ": conditions", "run %s, started by %s" % (run.run_id, run.operator),
               start + len(slides), footer)
    left: List[Para] = []
    for label, items in (("Swept", run.sweep), ("Measured", [", ".join(run.measured)] if run.measured else []),
                         ("Set up", run.setup), ("Left changed", run.changes), ("Not read at the end", run.unread)):
        if items:
            left.append([(label, {"bold": True, "color": MUTED})])
            left += [_clip(i, 110) for i in items[:6]]
    if run.issues:
        left.append([("Issues", {"bold": True, "color": "B42318"})])
        left += [_clip(i, 110) for i in run.issues[:4]]
    s.text(M, y, 420, SLIDE_H - y - 40, left, 12, space=3, shrink=True, name="Conditions")
    right: List[Para] = [[("At the start", {"bold": True, "color": MUTED})]]
    right += [[(inst + "  ", {"bold": True}), (_clip(vals, 140), {})] for inst, vals in run.conditions[:9]]
    if run.events:
        right.append([("Approvals and alarms", {"bold": True, "color": MUTED})])
        right += [[(t + "  ", {"color": MUTED}), (_clip(text, 110), {})] for t, text in run.events[:6]]
    s.text(490, y, SLIDE_W - M - 490, SLIDE_H - y - 40, right, 11, space=3, shrink=True, name="Instruments")
    slides.append(s)
    return slides


def write_pptx(report: Report, path: Path, pages: int = 0) -> int:
    """Write the report as slides; returns the number of slides. pages: at most this many slides, as asked: then
    only the summary and each run's result slide (as many as fit), each run's other slides left to its own report."""
    slides = _deck(report)
    if pages and len(slides) > pages:
        slides = _deck(report, brief=pages)[:pages]
    save_slides(slides, path, report.title)
    return len(slides)


def _deck(report: Report, brief: int = 0) -> List[Slide]:
    slides: List[Slide] = []
    if brief and brief < 3:   # a slide or two: the summary says it all
        s = Slide()
        _summary(s, report, 1, report.title)
        return [s]
    s = Slide()
    s.rect(0, 0, SLIDE_W, SLIDE_H, "F3F5F8")
    s.rect(0, 0, 10, SLIDE_H, ACCENT)
    s.text(70, 170, SLIDE_W - 140, 90, [[(report.title, {"bold": True})]], 40, anchor="b", shrink=True, name="Title")
    s.text(70, 272, SLIDE_W - 140, 30, [report.subtitle], 18, MUTED, name="Subtitle")
    s.text(70, 310, SLIDE_W - 140, 24, ["%s%s" % (report.lab, " (simulated instruments)" if report.simulated else "")],
           14, MUTED, name="Lab")
    s.text(70, SLIDE_H - 60, SLIDE_W - 140, 20, ["Made with Kelvin, %s" % report.created], 11, MUTED, name="Made with")
    slides.append(s)
    footer = report.title

    s = Slide()
    _summary(s, report, 2, footer)
    slides.append(s)

    if report.notes and not brief:
        s = Slide()
        y = _frame(s, "Project notes", "decisions, procedures, quirks and results kept in this project",
                   len(slides) + 1, footer)
        s.text(M, y, SLIDE_W - 2 * M, SLIDE_H - y - 40,
               [[(kind.capitalize() + ": ", {"bold": True}), (_clip(text, 240), {})] for kind, text in report.notes[:12]],
               14, bullet=True, space=5, shrink=True, name="Notes")
        slides.append(s)

    for run in report.runs:
        mine = _run_slides(run, footer, len(slides) + 1)
        slides += mine[:1] if brief else mine
    if brief:
        return slides

    if report.references:
        s = Slide()
        y = _frame(s, "References", "the project's reading list", len(slides) + 1, footer)
        s.text(M, y, SLIDE_W - 2 * M, SLIDE_H - y - 40,
               [[("[%d] " % i, {"bold": True, "color": MUTED}), (_clip(r, 220), {})]
                for i, r in enumerate(report.references[:14], 1)], 12, space=4, shrink=True, name="References")
        slides.append(s)
    if report.log:
        s = Slide()
        y = _frame(s, "Lab log", "approvals, runs and alarms", len(slides) + 1, footer)
        rows = report.log[-16:]
        s.text(M, y, SLIDE_W - 2 * M, SLIDE_H - y - 40,
               [[(t + "   ", {"color": MUTED, "mono": True}), (_clip(text, 130), {})] for t, text in rows],
               11, space=2, shrink=True, name="Log")
        slides.append(s)
    return slides


def _summary(s: Slide, report: Report, number: int, footer: str) -> None:
    y = _frame(s, "Summary", report.subtitle, number, footer)
    paras: List[Para] = [p for p in report.summary]
    bullets = report.run_lines[:8]
    if paras:
        width = SLIDE_W - 2 * M
        lines = sum(max(1, -(-int(measure(p, 16)) // int(width))) for p in paras)
        height = min(200.0, lines * 16 * 1.25 + 8 * len(paras))
        s.text(M, y, width, height, paras, 16, space=6, shrink=True, name="Summary")
        y += height + 18
    s.text(M, y, SLIDE_W - 2 * M, SLIDE_H - y - 40, bullets or ["No runs."], 14 if paras else 16, bullet=True,
           space=6, shrink=True, name="Runs")
