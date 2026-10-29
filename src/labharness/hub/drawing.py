"""Two things the hub does for signed-in Kelvins besides accounts, so that no Kelvin needs them itself:

- which models see pictures, service by service (GET /api/models/pictures): read from models.dev, a public list of
  models, once a day, and kept in model-pictures.json in the hub's folder. Kelvin's web/vision.py asks for it.
- a PDF's pages drawn as pictures (POST /api/pdf/pages), for a model that sees pictures but can't read PDFs: Kelvin
  sends only the pages that model needs, and only from shared work; pdfdraw.py draws them in a process of its own,
  with limits on size, pages and time. The hub keeps nothing of them. It needs pypdfium2 (the hub extra); without
  it, the hub says it can't draw (501) and the model reads the pages' text."""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("kelvin.hub")
MODELS_DEV = "https://models.dev/api.json"
# Kelvin's ready-made services (web/services.py), which models.dev names the same
SERVICES = ("openrouter", "opencode", "deepseek", "groq", "mistral", "xai")
LIST_FILE = "model-pictures.json"
LIST_EVERY = 24 * 3600     # the list is read again after this...
LIST_RETRY = 3600          # ...or this, after models.dev couldn't be read
MAX_LIST = 64 * 2 ** 20    # bytes of models.dev's list (about 6 MB in 2026)
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


def pictures_in(catalog: Any) -> Dict[str, Dict[str, List[str]]]:
    """From models.dev's list: for each of Kelvin's services, the models that take pictures and those that don't."""
    out: Dict[str, Dict[str, List[str]]] = {}
    for service in SERVICES:
        models = (catalog.get(service) or {}).get("models") if isinstance(catalog, dict) else None
        yes, no = [], []
        for model_id, model in (models or {}).items() if isinstance(models, dict) else []:
            inputs = ((model or {}).get("modalities") or {}).get("input") if isinstance(model, dict) else None
            if isinstance(inputs, list):
                (yes if "image" in inputs else no).append(str(model_id))
        if yes or no:
            out[service] = {"yes": sorted(yes), "no": sorted(no)}
    return out


def _read_models_dev() -> Any:
    from .._version import __version__
    req = urllib.request.Request(MODELS_DEV, headers={"User-Agent": "Kelvin-hub/%s" % __version__,
                                                      "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(MAX_LIST + 1)
    if len(data) > MAX_LIST:
        raise ValueError("models.dev's list is larger than %d MB" % (MAX_LIST // 2 ** 20))
    return json.loads(data)


class PictureList:
    """Which models see pictures, kept in the hub's folder and read again from models.dev once a day."""

    def __init__(self, home: Path, read: Optional[Callable[[], Any]] = None):
        """read: models.dev's list (None: from models.dev itself, _read_models_dev)."""
        self.path = Path(home) / LIST_FILE
        self.read = read
        self._lock = threading.Lock()
        try:
            kept = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            kept = {}
        self._kept: Dict[str, Any] = kept if isinstance(kept, dict) else {}

    def current(self) -> Dict[str, Any]:
        """{"at", "services": {...}}: read again first if a day old (only one reading at a time; the others get what
        is kept meanwhile)."""
        due = max(float(self._kept.get("at") or 0) + LIST_EVERY, float(self._kept.get("tried") or 0) + LIST_RETRY)
        if time.time() >= due and self._lock.acquire(blocking=False):
            try:
                self._kept = dict(self._kept, tried=time.time())
                try:
                    services = pictures_in((self.read or _read_models_dev)())
                    if not services:
                        raise ValueError("no models listed")
                    self._kept = {"at": time.time(), "tried": time.time(), "services": services}
                except Exception as e:   # models.dev unreachable or changed: what is kept, tried again later
                    log.warning("models.dev: %s", e)
                self._save()
            finally:
                self._lock.release()
        return {"at": self._kept.get("at"), "services": self._kept.get("services") or {}}

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self._kept), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("%s: %s", self.path, e)
