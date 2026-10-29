"""Other programs' files for the importers' tests, written here as their formats are documented (none downloaded):
TIFF 6.0 and BigTIFF, Office Open XML workbooks, BioLogic EC-Lab's .mpt and .mpr (the .mpr layout as galvani reads
it), Gamry's .dta and Arbin's exports."""
import io
import struct
import zipfile
import zlib
from datetime import datetime
from xml.sax.saxutils import escape

import numpy as np


# ------------------------------------------------------------------ TIFF

def lzw(data):
    """TIFF's LZW as libtiff writes it: codes most significant bit first, 9 to 12 bits wide, a wider code from the
    moment the next free code wouldn't fit, and a clear code when the table is full."""
    table = {bytes([i]): i for i in range(256)}
    free, width = 258, 9
    codes = [(256, 9)]
    word = b""
    for byte in data:
        longer = word + bytes([byte])
        if longer in table:
            word = longer
            continue
        codes.append((table[word], width))
        table[longer] = free
        free += 1
        if free == 4094:   # full: start again
            codes.append((256, width))
            table = {bytes([i]): i for i in range(256)}
            free, width = 258, 9
        elif free > (1 << width) - 1:
            width += 1
        word = bytes([byte])
    if word:
        codes.append((table[word], width))
        free += 1
        if free > (1 << width) - 1 and width < 12:
            width += 1
    codes.append((257, width))
    out, bits, count = bytearray(), 0, 0
    for code, w in codes:
        bits, count = (bits << w) | code, count + w
        while count >= 8:
            count -= 8
            out.append((bits >> count) & 0xFF)
    if count:
        out.append((bits << (8 - count)) & 0xFF)
    return bytes(out)


def packbits(data):
    """Apple's PackBits: runs of three or more as a count and the byte, the rest as literal stretches."""
    out, i, n = bytearray(), 0, len(data)
    while i < n:
        j = i
        while j < n and j - i < 128 and data[j] == data[i]:
            j += 1
        if j - i >= 3:
            out += bytes([257 - (j - i), data[i]])
            i = j
            continue
        j = i
        while j < n and j - i < 128 and not (j + 2 < n and data[j] == data[j + 1] == data[j + 2]):
            j += 1
        out += bytes([j - i - 1]) + data[i:j]
        i = j
    return bytes(out)


def _ifd(buf, entries, order, big):
    """Append one image file directory to buf; returns where its pointer to the next one is."""
    codes = {3: "H", 4: "I", 16: "Q"}
    count, field, entry = ("Q", 8, 20) if big else ("I", 4, 12)
    if len(buf) % 2:
        buf += b"\0"
    start = len(buf)
    head = struct.pack(order + ("Q" if big else "H"), len(entries))
    extra_at = start + len(head) + len(entries) * entry + field
    body, extra = bytearray(), bytearray()
    for tag, kind, values in sorted(entries):
        data = struct.pack(order + codes[kind] * len(values), *values)
        if len(data) <= field:
            value = data.ljust(field, b"\0")
        else:
            value = struct.pack(order + count, extra_at + len(extra))
            extra += data + (b"\0" if len(data) % 2 else b"")
        body += struct.pack(order + "HH" + count, tag, kind, len(values)) + value
    next_at = start + len(head) + len(body)
    buf += head + body + b"\0" * field + extra
    return start, next_at


def tiff(pages, compression=1, big=False, order="<", tile=None, predictor=1, rows=None, thumbnail=None):
    """A TIFF of NumPy pages (rows x columns, or rows x columns x 3): strips of `rows` rows (or square tiles of
    `tile` pixels), compressed (1 none, 5 LZW, 8 Deflate, 32773 PackBits), with horizontal differencing when
    predictor is 2. thumbnail: a reduced-resolution page put after the first (readers leave it out)."""
    buf = bytearray(b"II" if order == "<" else b"MM")
    buf += struct.pack(order + "HHHQ", 43, 8, 0, 0) if big else struct.pack(order + "HI", 42, 0)
    pointer = 8 if big else 4
    pages = list(pages)
    if thumbnail is not None:
        pages.insert(1, ("thumb", thumbnail))

    def squeeze(raw):
        return {1: raw, 5: lzw(raw), 8: zlib.compress(raw), 32773: packbits(raw)}[compression]
    for page in pages:
        reduced = isinstance(page, tuple)
        a = np.asarray(page[1] if reduced else page)
        h, w = a.shape[:2]
        spp = 1 if a.ndim == 2 else a.shape[2]
        a3 = a.reshape(h, w, spp)

        def block(part):
            """A strip's or a tile's bytes: with the predictor, each sample as its difference from the one to its
            left within the block (each tile starts again)."""
            if predictor == 2:
                part = part.copy()
                part[:, 1:] = part[:, 1:] - part[:, :-1]
            return squeeze(part.astype(part.dtype.newbyteorder(order)).tobytes())
        blocks = []
        if tile:
            for ty in range(0, h, tile):
                for tx in range(0, w, tile):
                    t = np.zeros((tile, tile, spp), dtype=a3.dtype)
                    part = a3[ty:ty + tile, tx:tx + tile]
                    t[:part.shape[0], :part.shape[1]] = part
                    blocks.append(block(t))
        else:
            per = rows or h
            blocks = [block(a3[r:r + per]) for r in range(0, h, per)]
        offsets = []
        for b in blocks:
            offsets.append(len(buf))
            buf += b
        bits = a.dtype.itemsize * 8
        fmt = {"u": 1, "i": 2, "f": 3}[a.dtype.kind]
        entries = [(256, 4, [w]), (257, 4, [h]), (258, 3, [bits] * spp), (259, 3, [compression]),
                   (262, 3, [2 if spp >= 3 else 1]), (277, 3, [spp]), (339, 3, [fmt] * spp)]
        if predictor == 2:
            entries.append((317, 3, [2]))
        if reduced:
            entries.append((254, 4, [1]))
        where = 16 if big else 4
        if tile:
            entries += [(322, 4, [tile]), (323, 4, [tile]), (324, where, offsets),
                        (325, 4, [len(b) for b in blocks])]
        else:
            entries += [(273, where, offsets), (278, 4, [rows or h]), (279, 4, [len(b) for b in blocks])]
        start, next_at = _ifd(buf, entries, order, big)
        struct.pack_into(order + ("Q" if big else "I"), buf, pointer, start)
        pointer = next_at
    return bytes(buf)


# ------------------------------------------------------------------ Excel

def _column(n):
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def xlsx(sheets):
    """A workbook as Excel writes one: text in shared strings, numbers as numbers, datetimes as day numbers with a
    date style (number format 22, m/d/yy h:mm)."""
    strings, index = [], {}
    parts = []
    for name, rows in sheets:
        xml = []
        for r, row in enumerate(rows, 1):
            cells = []
            for c, v in enumerate(row):
                ref = "%s%d" % (_column(c), r)
                if v is None:
                    continue
                if isinstance(v, str):
                    if v not in index:
                        index[v] = len(strings)
                        strings.append(v)
                    cells.append('<c r="%s" t="s"><v>%d</v></c>' % (ref, index[v]))
                elif isinstance(v, datetime):
                    days = (v - datetime(1899, 12, 30)).total_seconds() / 86400
                    cells.append('<c r="%s" s="1"><v>%r</v></c>' % (ref, days))
                else:
                    cells.append('<c r="%s"><v>%r</v></c>' % (ref, float(v)))
            xml.append('<row r="%d">%s</row>' % (r, "".join(cells)))
        parts.append((name, '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet xmlns="http://schemas.'
                      'openxmlformats.org/spreadsheetml/2006/main"><sheetData>%s</sheetData></worksheet>' % "".join(xml)))
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    book = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<workbook xmlns="%s" xmlns:r="%s"><sheets>%s'
            '</sheets></workbook>' % (main, rel, "".join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (
                escape(n, {'"': "&quot;"}), i + 1, i + 1) for i, (n, _) in enumerate(parts))))
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.'
            'openxmlformats.org/package/2006/relationships">%s<Relationship Id="rIdS" Type="%s/sharedStrings" '
            'Target="sharedStrings.xml"/><Relationship Id="rIdT" Type="%s/styles" Target="styles.xml"/>'
            '</Relationships>' % ("".join('<Relationship Id="rId%d" Type="%s/worksheet" Target="worksheets/sheet%d.xml"/>'
                                          % (i + 1, rel, i + 1) for i in range(len(parts))), rel, rel))
    shared = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<sst xmlns="%s" count="%d" uniqueCount="%d">%s'
              '</sst>' % (main, len(strings), len(strings), "".join("<si><t>%s</t></si>" % escape(s) for s in strings)))
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<styleSheet xmlns="%s"><cellXfs count="2">'
              '<xf numFmtId="0"/><xf numFmtId="22" applyNumberFormat="1"/></cellXfs></styleSheet>' % main)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/content-types"/>')
        z.writestr("xl/workbook.xml", book)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/sharedStrings.xml", shared)
        z.writestr("xl/styles.xml", styles)
        for i, (_, xml) in enumerate(parts):
            z.writestr("xl/worksheets/sheet%d.xml" % (i + 1), xml)
    return out.getvalue()


# ------------------------------------------------------------------ BioLogic EC-Lab

def mpt(names, rows, settings=(), comma=False, first="EC-Lab ASCII FILE"):
    """An EC-Lab text export: the first line, the header's length (the two first lines, the settings and the
    columns' names), tab-separated columns with a trailing tab, numbers in EC-Lab's E notation."""
    def cell(v):
        text = "%d" % v if isinstance(v, int) else "%.6E" % v
        return text.replace(".", ",") if comma else text
    lines = [first, "Nb header lines : %d" % (len(settings) + 3)] + list(settings) + ["\t".join(names) + "\t"]
    lines += ["\t".join(cell(v) for v in r) + "\t" for r in rows]
    return "\r\n".join(lines) + "\r\n"


MPR_MAGIC = b"BIO-LOGIC MODULAR FILE\x1a".ljust(48) + b"\x00\x00\x00\x00"


def mpr_module(short, data, version, date=b"07/13/21", new_header=False):
    """One module: MODULE, its short and long names, length, version and date (EC-Lab 11.50 and later put a
    0xFFFFFFFF field before the length, and another after the version)."""
    head = b"MODULE" + short.ljust(10) + (short + b" module").ljust(25)
    if new_header:
        head += struct.pack("<IIII", 0xFFFFFFFF, len(data), version, 0) + date
    else:
        head += struct.pack("<II", len(data), version) + date
    return head + data


def mpr(ids, records, version=2, new_header=False, points=None, pad=None):
    """An EC-Lab .mpr: a settings module, then the data module (points, columns, their ids, zeros up to the records:
    100 bytes in version 0, 405 in version 2, 405 then 0x01 in version 3) and a log module. records: a NumPy record
    array laid out as the ids say."""
    n = len(records) if points is None else points
    head = struct.pack("<IB", n, len(ids))
    if version == 0:
        head += bytes(ids)
        head = head.ljust(100, b"\0")
    else:
        head += struct.pack("<%dH" % len(ids), *ids)
        head = head.ljust(405, b"\0") + (b"\x01" if version == 3 else b"")
    if pad:
        head = head[:pad[0]] + pad[1] + head[pad[0] + len(pad[1]):]
    data = head + records.tobytes()
    return (MPR_MAGIC + mpr_module(b"VMP Set", b"\0" * 64, 0, new_header=new_header)
            + mpr_module(b"VMP data", data, version, new_header=new_header)
            + mpr_module(b"VMP LOG", b"\0" * 32, 0, new_header=new_header))


# ------------------------------------------------------------------ Gamry

def dta(tag, tables, settings=(), notes=("a note about the cell",)):
    """A Gamry Framework .dta: EXPLAIN, the TAG, settings (KEY, type, value, label), NOTES, then each table: its name
    and TABLE, a tab-led line of names, one of units, and a tab-led line per point."""
    lines = ["EXPLAIN", "TAG\t%s" % tag, "TITLE\tLABEL\tA test run\tTest &Identifier"]
    lines += ["%s\t%s\t%s\t%s" % s for s in settings]
    lines += ["NOTES\tNOTES\t%d\t&Notes..." % len(notes)] + ["\t" + n for n in notes]
    for name, names, units, rows in tables:
        lines.append("%s\tTABLE\t%d" % (name, len(rows)))
        lines.append("\t" + "\t".join(names))
        lines.append("\t" + "\t".join(units))
        lines += ["\t" + "\t".join(str(v) for v in r) for r in rows]
    return "\r\n".join(lines) + "\r\n"
