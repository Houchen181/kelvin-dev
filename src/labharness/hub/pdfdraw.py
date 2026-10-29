"""Draws a PDF's pages as PNG pictures, in a process of its own: hub/drawing.py runs it with a time limit, so a PDF
that is malformed, huge or hostile can't stop or take down the hub. The PDF on stdin; on stdout
{"pages": [{"mime": "image/png", "data": base64}]}; on failure, exit 2 and the reason on stderr.

    python -m labharness.hub.pdfdraw < pages.pdf

pypdfium2 (PDFium, Chrome's PDF engine) draws; this module writes the PNGs itself, so the hub needs no image library."""
from __future__ import annotations

import base64
import json
import struct
import sys
import zlib
from typing import Iterable, List

MAX_PAGES = 8        # pages drawn from one PDF (Kelvin sends the pages a model needs, at most 6)
LONGEST = 1600       # pixels on a page's longest side: small print stays readable, as models take pictures
MAX_SCALE = 3.0      # never more than 216 dots per inch, for a small page


def png(width: int, height: int, channels: int, rows: Iterable[bytes]) -> bytes:
    """A PNG of 8-bit rows: gray (1 channel), RGB (3) or RGBA (4)."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    color = {1: 0, 3: 2, 4: 6}[channels]
    raw = b"".join(b"\x00" + row for row in rows)   # filter 0 on every row: zlib does the work
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def draw(pdf: bytes, most: int = MAX_PAGES) -> List[bytes]:
    """The first `most` pages of a PDF, as PNGs."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf)
    try:
        out = []
        for i in range(min(len(doc), most)):
            page = doc[i]
            try:
                width, height = page.get_size()
                scale = min(LONGEST / max(width, height, 1.0), MAX_SCALE)
                bitmap = page.render(scale=scale, rev_byteorder=True)   # RGB, on white
                try:
                    w, h, n, stride = bitmap.width, bitmap.height, bitmap.n_channels, bitmap.stride
                    buf = bytes(bitmap.buffer)
                    out.append(png(w, h, n, (buf[y * stride:y * stride + w * n] for y in range(h))))
                finally:
                    bitmap.close()
            finally:
                page.close()
        return out
    finally:
        doc.close()


def main() -> None:
    data = sys.stdin.buffer.read()
    try:
        pages = draw(data)
    except Exception as e:
        text = str(e).strip()
        sys.stderr.write("the PDF couldn't be drawn: %s" % (text.splitlines()[0] if text else type(e).__name__))
        sys.exit(2)
    json.dump({"pages": [{"mime": "image/png", "data": base64.b64encode(p).decode()} for p in pages]}, sys.stdout)


if __name__ == "__main__":
    main()
