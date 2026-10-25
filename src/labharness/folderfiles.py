"""Files in a chat's folder, the folder the person chose for that chat: listed and read for Kelvin's AI, and nothing
outside it ('..' and links that lead out are refused). Text and CSV files by characters; PDFs by pages, as their text
or, for a model that reads PDFs, as the pages themselves (figures, plots and equations as drawn); pictures as they
are. What is read comes from outside the lab: the caller marks it as data for the model."""
from __future__ import annotations

import io
import logging
import re
import time
from pathlib import Path
from typing import Any, List, Tuple

MAX_CHARS = 12000              # of text in one read; start= reads on
MAX_PDF_PAGES = 20             # pages a model sees whole at once: each costs about as much as a picture
MAX_PDF_BYTES = 10 * 2 ** 20   # and their size, well under what each model service takes in one request
MAX_PICTURE_BYTES = 5 * 2 ** 20
MAX_LISTED = 200
PICTURES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
            ".webp": "image/webp"}
PDF = "application/pdf"

Media = List[Tuple[str, bytes]]   # (mime type, the file's bytes): pictures, or PDF pages


class FolderError(ValueError):
    """Not read: outside the chat's folder, not there, or a kind of file Kelvin doesn't read (the message says)."""


def inside(folder: Any, path: Any = "") -> Path:
    """path (relative to the chat's folder, or in full) as a path in that folder; FolderError if it leads out."""
    base = Path(folder).resolve()
    given = Path(str(path or "").strip() or ".")
    target = (given if given.is_absolute() else base / given).resolve()
    if target != base and base not in target.parents:
        raise FolderError("%s is outside this chat's folder (%s): Kelvin reads only what is in it" % (path, base))
    return target


def _size(n: int) -> str:
    return "%d B" % n if n < 1024 else "%.0f KB" % (n / 1024) if n < 1024 ** 2 else "%.1f MB" % (n / 1024 ** 2)


def listing(folder: Any, path: Any = "") -> str:
    """The subfolders (Kelvin's runs marked) and files of the chat's folder, or of a folder in it: newest files
    first, with sizes and dates."""
    from . import rundata
    from .eventlog import RUN_NAME
    where = inside(folder, path)
    if not where.is_dir():
        raise FolderError("there is no folder %s in this chat's folder" % (path or "."))
    try:
        entries = [e for e in where.iterdir() if not e.name.startswith(".")]
        dirs = sorted((e for e in entries if e.is_dir()), key=lambda e: e.name.lower())
        files = sorted(((e, e.stat()) for e in entries if e.is_file()), key=lambda f: f[1].st_mtime, reverse=True)
    except OSError as e:
        raise FolderError("Kelvin could not list %s: %s" % (where, e.strerror or e))
    lines = ["%s: %d folder%s, %d file%s" % (where, len(dirs), "" if len(dirs) == 1 else "s", len(files),
                                             "" if len(files) == 1 else "s")]
    for d in dirs[:MAX_LISTED]:
        run = RUN_NAME.match(d.name) and rundata.is_run(d)
        lines.append("%s/%s" % (d.name, "  (a Kelvin run: describe_run and get_run_data read it)" if run else ""))
    for f, st in files[:max(0, MAX_LISTED - len(dirs))]:
        lines.append("%s  %s  %s" % (f.name, _size(st.st_size), time.strftime("%Y-%m-%d %H:%M",
                                                                             time.localtime(st.st_mtime))))
    hidden = len(dirs) + len(files) - (len(lines) - 1)
    if hidden > 0:
        lines.append("… and %d more" % hidden)
    return "\n".join(lines)


def page_numbers(pages: Any, count: int) -> List[int]:
    """'3', '3-5', '7-' or '1,4,9' (empty: all) as 0-based page indices of a PDF with count pages."""
    text = str(pages or "").replace(" ", "")
    if not text:
        return list(range(count))
    out: List[int] = []
    for part in text.split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d*))?", part)
        if not m:
            raise FolderError("pages are like '3', '3-5', '7-' or '1,4,9', not %r" % pages)
        lo = int(m.group(1))
        hi = lo if m.group(2) is None else int(m.group(2)) if m.group(2) else count
        if lo < 1 or lo > count or hi < lo:
            raise FolderError("this PDF has pages 1-%d, not %s" % (count, part))
        out += [i for i in range(lo - 1, min(hi, count)) if i not in out]
    return out


def _ranges(indices: List[int]) -> str:
    """0-based indices as page ranges people read: [0, 1, 2, 5] -> '1-3, 6'."""
    spans: List[List[int]] = []
    for i in indices:
        if spans and i == spans[-1][1] + 1:
            spans[-1][1] = i
        else:
            spans.append([i, i])
    return ", ".join("%d" % (a + 1) if a == b else "%d-%d" % (a + 1, b + 1) for a, b in spans)


def read(folder: Any, path: Any, pages: Any = "", start: int = 0, figures: bool = False) -> Tuple[str, Media]:
    """A file in the chat's folder: (what to tell the model, the pictures or PDF pages to show it)."""
    target = inside(folder, path)
    if not target.is_file():
        raise FolderError("there is no file %s in this chat's folder (list_folder shows what is there)" % path)
    shown = target.relative_to(Path(folder).resolve()).as_posix()
    ext, size = target.suffix.lower(), target.stat().st_size
    if ext == ".pdf":
        return _pdf(target, shown, pages, max(0, int(start or 0)), figures)
    if ext in PICTURES:
        if size > MAX_PICTURE_BYTES:
            raise FolderError("%s is %s, more than a model takes (%s): a smaller copy or a crop would do"
                              % (shown, _size(size), _size(MAX_PICTURE_BYTES)))
        return "%s, a picture (%s)" % (shown, _size(size)), [(PICTURES[ext], target.read_bytes())]
    if ext in (".h5", ".hdf5"):
        raise FolderError("%s is HDF5: a Kelvin run's run.h5 is read with describe_run and get_run_data; Kelvin "
                          "doesn't read other HDF5 files" % shown)
    with open(target, "rb") as f:
        if b"\x00" in f.read(4096):
            raise FolderError("%s is not text, a PDF or a picture (PNG, JPEG, GIF, WebP): Kelvin doesn't read it"
                              % shown)
    start = max(0, int(start or 0))
    with open(target, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        if start:
            f.read(start)
        chunk = f.read(MAX_CHARS + 1)
    more = len(chunk) > MAX_CHARS
    chunk = chunk[:MAX_CHARS]
    if not chunk and start:
        raise FolderError("%s ends before character %d" % (shown, start))
    return ("%s (%s), characters %d-%d%s\n%s" % (
        shown, _size(size), start, start + len(chunk),
        "; more from start=%d" % (start + MAX_CHARS) if more else ", to the end", chunk)), []


def _pdf(target: Path, shown: str, pages: Any, start: int, figures: bool) -> Tuple[str, Media]:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        raise FolderError("reading PDFs needs pypdf, which comes with the Kelvin app (from source: pip install pypdf)")
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        reader = PdfReader(str(target))
        if reader.is_encrypted:   # often locked against printing or editing, not reading
            reader.decrypt("")
        count = len(reader.pages)
    except Exception as e:
        raise FolderError("Kelvin could not open %s as a PDF (%s)" % (shown, str(e).splitlines()[0] if str(e) else e))
    chosen = page_numbers(pages, count)
    if figures:   # the pages themselves, as one smaller PDF
        some = chosen[:MAX_PDF_PAGES]
        writer = PdfWriter()
        for i in some:
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        data = buf.getvalue()
        if len(data) > MAX_PDF_BYTES:
            raise FolderError("pages %s of %s are %s, more than a model takes at once: ask for fewer pages"
                              % (_ranges(some), shown, _size(len(data))))
        rest = chosen[len(some):]
        return ("%s, pages %s of %d, shown whole%s" % (
            shown, _ranges(some), count, "; the rest from pages='%d-'" % (rest[0] + 1) if rest else "")), \
            [(PDF, data)]
    texts, offsets, length = [], [], 0
    for i in chosen:   # only as many pages as this read shows
        try:
            body = (reader.pages[i].extract_text() or "").strip()
        except Exception:
            body = ""
        part = "[page %d]\n%s" % (i + 1, body or "(no text on this page: a scan or a figure; figures=true shows it)")
        offsets.append((i, length))
        texts.append(part)
        length += len(part) + 2
        if length > start + MAX_CHARS:
            break
    text = "\n\n".join(texts)
    if start >= len(text):
        raise FolderError("pages %s of %s have less text than character %d" % (_ranges(chosen), shown, start))
    chunk = text[start:start + MAX_CHARS]
    end = start + len(chunk)
    bounds = [(i, at, offsets[k + 1][1] - 2 if k + 1 < len(offsets) else len(text))
              for k, (i, at) in enumerate(offsets)]   # each page's text: (page, from, to)
    seen = [i for i, a, b in bounds if a < end and b > start]
    if end >= len(text) and len(texts) == len(chosen):
        tail = ", to the end of the pages asked for"
    else:   # the next read starts in the first page not wholly shown, without reading the earlier ones again
        k = next((k for k, (_, _, b) in enumerate(bounds) if b > end), len(bounds))
        rest = chosen[chosen.index(bounds[k][0]):] if k < len(bounds) else chosen[len(bounds):]
        within = max(0, end - bounds[k][1]) if k < len(bounds) else 0
        tail = "; more from pages='%s'%s" % (_ranges(rest).replace(" ", ""), " start=%d" % within if within else "")
    return "%s, %d pages; the text of pages %s%s\n%s" % (shown, count, _ranges(seen), tail, chunk), []
