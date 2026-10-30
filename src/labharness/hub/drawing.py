"""PDF pages drawn as pictures by the hub (POST /api/pdf/pages), for a signed-in Kelvin whose model sees pictures but
can't read PDFs: Kelvin sends only the pages that model needs, and only from shared work. pdfdraw.py draws them in a
process of its own, with limits on size, pages and time; the hub keeps nothing of them. It needs pypdfium2 (the hub
extra); without it the hub says it can't draw (501) and the model reads the pages' text."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from typing import Dict, List

MAX_PDF = 10 * 2 ** 20     # bytes of pages Kelvin sends at once (folderfiles.MAX_PDF_BYTES)
DRAW_S = 60                # seconds a drawing may take
MAX_OUT = 64 * 2 ** 20     # bytes of pictures back


class DrawError(Exception):
    """The PDF couldn't be drawn: not a PDF, too big, or too slow."""


def can_draw() -> bool:
    return importlib.util.find_spec("pypdfium2") is not None


def draw(pdf: bytes) -> List[Dict[str, str]]:
    """The pages of a PDF as pictures, [{"mime": "image/png", "data": base64}], drawn in a separate process."""
    if len(pdf) > MAX_PDF or not pdf.startswith(b"%PDF-"):
        raise DrawError("that is not a PDF of at most %d MB" % (MAX_PDF // 2 ** 20))
    try:
        done = subprocess.run([sys.executable, "-m", "labharness.hub.pdfdraw"], input=pdf, capture_output=True,
                              timeout=DRAW_S, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise DrawError("drawing the pages took longer than %d s" % DRAW_S)
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise DrawError(said[-1][:300] if said else "the PDF couldn't be drawn")
    if len(done.stdout) > MAX_OUT:
        raise DrawError("the pictures of these pages are too large")
    try:
        pages = json.loads(done.stdout)["pages"]
    except (ValueError, KeyError, TypeError):
        raise DrawError("the drawing gave no pictures")
    return pages
