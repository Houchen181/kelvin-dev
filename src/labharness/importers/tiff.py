"""TIFF images as arrays, without Pillow or tifffile (the app brings neither): a camera's frame, or a stack of frames
(a multi-page TIFF, as ImageJ, Micro-Manager and cameras' own programs save a series).

Read, as the TIFF 6.0 specification and Adobe's BigTIFF extension lay them out:
- classic TIFF and BigTIFF, in either byte order;
- 8, 16, 32 and 64-bit samples (unsigned, signed or floating point), and 1-bit black and white; gray or RGB(A);
- strips or tiles, uncompressed, LZW, Deflate or PackBits, with or without horizontal differencing.

Refused, saying so: JPEG and the other compressions, CMYK and YCbCr, and pixels of other kinds (two channels, say). A
palette image keeps its index values. Reduced-resolution copies (thumbnails, pyramids' levels) are left out."""
from __future__ import annotations

import math
import struct
import zlib
from typing import Any, Dict, List, Tuple

from ..imports import DataError

MAX_PAGES = 5000
MAX_PIXEL_BYTES = 512 * 2 ** 20     # every page together, as read: a stack bigger than this is refused

SUBFILE, WIDTH, LENGTH, BITS, COMPRESSION, PHOTOMETRIC = 254, 256, 257, 258, 259, 262
STRIP_OFFSETS, SAMPLES, ROWS_PER_STRIP, STRIP_COUNTS, PLANAR = 273, 277, 278, 279, 284
PREDICTOR, TILE_WIDTH, TILE_LENGTH, TILE_OFFSETS, TILE_COUNTS, SAMPLE_FORMAT = 317, 322, 323, 324, 325, 339
WANTED = {SUBFILE, WIDTH, LENGTH, BITS, COMPRESSION, PHOTOMETRIC, STRIP_OFFSETS, SAMPLES, ROWS_PER_STRIP,
          STRIP_COUNTS, PLANAR, PREDICTOR, TILE_WIDTH, TILE_LENGTH, TILE_OFFSETS, TILE_COUNTS, SAMPLE_FORMAT}
# a field's type: its struct code (a rational is two of them)
_TYPES = {1: "B", 2: "B", 3: "H", 4: "I", 5: "I", 6: "b", 7: "B", 8: "h", 9: "i", 10: "i", 11: "f", 12: "d",
          13: "I", 16: "Q", 17: "q", 18: "Q"}
_RATIONAL = (5, 10)
COMPRESSIONS = {1: "none", 5: "LZW", 8: "Deflate", 32946: "Deflate", 32773: "PackBits"}


def is_tiff(head: bytes) -> bool:
    return head[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")


def read(data: bytes, name: str = "the TIFF") -> List[Any]:
    """Every full-resolution page, as a NumPy array: rows x columns, or rows x columns x 3 for colour."""
    import numpy as np
    if not is_tiff(data[:4]):
        raise DataError("%s is not a TIFF file" % name)
    bo = "<" if data[:2] == b"II" else ">"
    big = data[2:4] in (b"+\x00", b"\x00+")
    try:
        if big:
            size, _ = struct.unpack_from(bo + "HH", data, 4)
            if size != 8:
                raise DataError("%s: a BigTIFF with %d-byte offsets, which Kelvin doesn't read" % (name, size))
            offset = struct.unpack_from(bo + "Q", data, 8)[0]
        else:
            offset = struct.unpack_from(bo + "I", data, 4)[0]
    except struct.error:
        raise DataError("%s is cut short" % name)
    pages: List[Any] = []
    seen = set()
    total = 0
    while offset:
        if offset in seen:   # a directory pointing back: read once
            break
        if len(seen) >= MAX_PAGES:
            raise DataError("%s has more than %d pages" % (name, MAX_PAGES))
        seen.add(offset)
        tags, offset = _directory(data, offset, bo, big, name)
        if tags.get(SUBFILE, [0])[0] & 1:   # a reduced-resolution copy of another page
            continue
        page = _page(data, tags, bo, name, np)
        total += page.nbytes
        if total > MAX_PIXEL_BYTES:
            raise DataError("%s holds more than %d MB of pixels" % (name, MAX_PIXEL_BYTES // 2 ** 20))
        pages.append(page)
    if not pages:
        raise DataError("%s holds no image" % name)
    return pages


def _directory(data: bytes, offset: int, bo: str, big: bool, name: str) -> Tuple[Dict[int, List[Any]], int]:
    """One image file directory: the tags Kelvin uses, and where the next directory is (0: none)."""
    head, entry, count_code, field = (8, 20, "Q", 8) if big else (2, 12, "I", 4)
    if offset + head > len(data):
        raise DataError("%s is cut short" % name)
    n = struct.unpack_from(bo + ("Q" if big else "H"), data, offset)[0]
    pos = offset + head
    if n > 4096 or pos + n * entry + field > len(data):
        raise DataError("%s is cut short (an image directory runs past its end)" % name)
    tags: Dict[int, List[Any]] = {}
    for k in range(n):
        at = pos + k * entry
        tag, kind = struct.unpack_from(bo + "HH", data, at)
        count = struct.unpack_from(bo + count_code, data, at + 4)[0]
        code = _TYPES.get(kind)
        if tag not in WANTED or code is None:
            continue
        values = count * (2 if kind in _RATIONAL else 1)
        size = values * struct.calcsize(code)
        where = at + 4 + field
        if size > field:
            where = struct.unpack_from(bo + count_code, data, where)[0]
        if values > 10 ** 7 or where + size > len(data):
            raise DataError("%s is cut short (a tag points past its end)" % name)
        got = struct.unpack_from("%s%d%s" % (bo, values, code), data, where)
        if kind in _RATIONAL:
            got = tuple(got[2 * i] / got[2 * i + 1] if got[2 * i + 1] else 0.0 for i in range(count))
        tags[tag] = list(got)
    nxt = struct.unpack_from(bo + count_code, data, pos + n * entry)[0]
    return tags, nxt


def _page(data: bytes, tags: Dict[int, List[Any]], bo: str, name: str, np: Any) -> Any:
    try:
        w, h = int(tags[WIDTH][0]), int(tags[LENGTH][0])
    except (KeyError, IndexError):
        raise DataError("%s: a page without its size" % name)
    spp = int(tags.get(SAMPLES, [1])[0])
    bits = tags.get(BITS, [1] * spp)
    fmt = int(tags.get(SAMPLE_FORMAT, [1])[0])
    comp = int(tags.get(COMPRESSION, [1])[0])
    photo = int(tags.get(PHOTOMETRIC, [1])[0])
    planar = int(tags.get(PLANAR, [1])[0])
    predictor = int(tags.get(PREDICTOR, [1])[0])
    if w <= 0 or h <= 0:
        raise DataError("%s: a page of %dx%d pixels" % (name, w, h))
    if len(set(bits)) != 1:
        raise DataError("%s: samples of different sizes in one pixel, which Kelvin doesn't read" % name)
    b = int(bits[0])
    if photo not in (0, 1, 2, 3) or (photo == 2) != (spp in (3, 4)) or (photo != 2 and spp != 1):
        raise DataError("%s: pixels of a kind Kelvin doesn't read (only gray and RGB)" % name)
    if comp not in COMPRESSIONS:
        raise DataError("%s is compressed in a way Kelvin doesn't read (TIFF compression %d): save it uncompressed, "
                        "or with LZW or Deflate" % (name, comp))
    if predictor not in (1, 2) or (predictor == 2 and (fmt == 3 or b == 1)):
        raise DataError("%s uses a predictor Kelvin doesn't read: save it without one" % name)
    if b == 1 and fmt == 1 and spp == 1:
        code = "bit"
    elif b in (8, 16, 32, 64) and (fmt in (1, 2) or (fmt == 3 and b >= 16)):
        code = "%s%d" % ({1: "u", 2: "i", 3: "f"}[fmt], b // 8)
    else:
        raise DataError("%s: %d-bit samples, which Kelvin doesn't read" % (name, b))
    if w * h * spp * max(b, 8) // 8 > MAX_PIXEL_BYTES:
        raise DataError("%s: a page of %dx%d pixels, bigger than Kelvin reads" % (name, w, h))
    planes = spp if planar == 2 else 1
    per = 1 if planar == 2 else spp   # samples in a pixel as stored in one plane
    if TILE_OFFSETS in tags:
        if code == "bit":
            raise DataError("%s: black-and-white tiles, which Kelvin doesn't read" % name)
        a = _tiles(data, tags, bo, code, w, h, per, planes, comp, predictor, name, np)
    else:
        a = _strips(data, tags, bo, code, w, h, per, planes, comp, predictor, name, np)
    if planar == 2:   # planes first: samples last, as chunky pixels have them
        a = np.moveaxis(a, 0, -1).reshape(h, w, spp)
    else:
        a = a.reshape(h, w, spp)
    a = a[:, :, 0] if spp == 1 else a[:, :, :3]   # an alpha channel isn't data
    return np.ascontiguousarray(a)


def _native(code: str, bo: str, np: Any) -> Tuple[Any, Any]:
    stored = np.dtype(bo + code)
    return stored, stored.newbyteorder("=")


def _strips(data: bytes, tags: Dict[int, List[Any]], bo: str, code: str, w: int, h: int, per: int, planes: int,
            comp: int, predictor: int, name: str, np: Any) -> Any:
    offsets = [int(v) for v in tags.get(STRIP_OFFSETS) or []]
    counts = [int(v) for v in tags.get(STRIP_COUNTS) or []]
    rows = int(tags.get(ROWS_PER_STRIP, [h])[0]) or h
    rows = min(rows, h)
    bits = 1 if code == "bit" else 8 * int(code[1:])
    row_bytes = (w * per * bits + 7) // 8
    each = math.ceil(h / rows)
    if len(offsets) < each * planes or (comp != 1 and len(counts) < len(offsets)):
        raise DataError("%s: its strips are not all listed" % name)
    out = []
    for p in range(planes):
        for s in range(each):
            k = p * each + s
            want = min(rows, h - s * rows) * row_bytes
            size = counts[k] if k < len(counts) else want
            raw = data[offsets[k]:offsets[k] + size]
            got = _decompress(raw, comp, want, name)
            if len(got) < want:
                raise DataError("%s is cut short (a strip holds less than its rows)" % name)
            out.append(got[:want])
    buf = b"".join(out)
    if code == "bit":   # black and white: a byte per pixel, 0 or 1 as stored
        a = np.unpackbits(np.frombuffer(buf, np.uint8).reshape(planes * h, row_bytes), axis=1)[:, :w]
        return a.reshape(planes, h, w, 1) if planes > 1 else a.reshape(h, w, 1)
    stored, native = _native(code, bo, np)
    a = np.frombuffer(buf, dtype=stored).astype(native)
    a = a.reshape(planes, h, w, per) if planes > 1 else a.reshape(h, w, per)
    if predictor == 2:   # each sample stored as its difference from the one to its left
        a = np.cumsum(a, axis=-2, dtype=native)
    return a


def _tiles(data: bytes, tags: Dict[int, List[Any]], bo: str, code: str, w: int, h: int, per: int, planes: int,
           comp: int, predictor: int, name: str, np: Any) -> Any:
    try:
        tw, th = int(tags[TILE_WIDTH][0]), int(tags[TILE_LENGTH][0])
    except (KeyError, IndexError):
        raise DataError("%s: tiles without their size" % name)
    if tw <= 0 or th <= 0:
        raise DataError("%s: tiles of %dx%d pixels" % (name, tw, th))
    offsets = [int(v) for v in tags.get(TILE_OFFSETS) or []]
    counts = [int(v) for v in tags.get(TILE_COUNTS) or []]
    across, down = math.ceil(w / tw), math.ceil(h / th)
    if len(offsets) < across * down * planes or (comp != 1 and len(counts) < len(offsets)):
        raise DataError("%s: its tiles are not all listed" % name)
    stored, native = _native(code, bo, np)
    want = tw * th * per * stored.itemsize
    canvas = np.zeros((planes, down * th, across * tw, per), dtype=native)
    for p in range(planes):
        for ty in range(down):
            for tx in range(across):
                k = (p * down + ty) * across + tx
                size = counts[k] if k < len(counts) else want
                got = _decompress(data[offsets[k]:offsets[k] + size], comp, want, name)
                if len(got) < want:
                    raise DataError("%s is cut short (a tile holds less than its pixels)" % name)
                tile = np.frombuffer(got[:want], dtype=stored).astype(native).reshape(th, tw, per)
                if predictor == 2:
                    tile = np.cumsum(tile, axis=1, dtype=native)
                canvas[p, ty * th:(ty + 1) * th, tx * tw:(tx + 1) * tw] = tile
    a = canvas[:, :h, :w]
    return a if planes > 1 else a[0]


def _decompress(raw: bytes, comp: int, want: int, name: str) -> bytes:
    """A strip's or a tile's bytes as stored (at most want of them: never more than the image holds)."""
    if comp == 1:
        return raw
    if comp in (8, 32946):
        try:
            return zlib.decompressobj().decompress(raw, want)
        except zlib.error:
            raise DataError("%s: its Deflate data are damaged" % name)
    if comp == 32773:
        return _packbits(raw, want)
    return _lzw(raw, want, name)


def _packbits(data: bytes, want: int) -> bytes:
    """PackBits (Apple's run-length code): a count byte, then that many bytes or one byte repeated."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n and len(out) < want:
        head = data[i]
        i += 1
        if head < 128:
            out += data[i:i + head + 1]
            i += head + 1
        elif head > 128 and i < n:
            out += data[i:i + 1] * (257 - head)
            i += 1
    return bytes(out)


def _lzw(data: bytes, want: int, name: str = "the TIFF") -> bytes:
    """TIFF's LZW: codes most significant bit first, 9 to 12 bits wide, each width taken one code early (as the
    specification's encoder writes them); 256 clears the table, 257 ends."""
    out = bytearray()
    table: List[bytes] = [bytes([i]) for i in range(256)] + [b"", b""]
    padded = data + b"\x00\x00\x00"
    width, pos, end = 9, 0, len(data) * 8
    prev = b""
    while pos + width <= end and len(out) < want:
        i = pos >> 3
        code = (int.from_bytes(padded[i:i + 3], "big") >> (24 - (pos & 7) - width)) & ((1 << width) - 1)
        pos += width
        if code == 256:
            del table[258:]
            width, prev = 9, b""
            continue
        if code == 257:
            break
        if not prev:
            if code > 255:
                raise DataError("%s: its LZW data are damaged" % name)
            entry = table[code]
        elif code < len(table):
            entry = table[code]
            if len(table) < 4096:
                table.append(prev + entry[:1])
        elif code == len(table):
            entry = prev + prev[:1]
            table.append(entry)
        else:
            raise DataError("%s: its LZW data are damaged" % name)
        out += entry
        prev = entry
        n = len(table)
        if (width == 9 and n >= 511) or (width == 10 and n >= 1023) or (width == 11 and n >= 2047):
            width += 1
    return bytes(out)
