"""What the hub knows of models from models.dev, a public list of models: read once a day and kept in models-dev.json
in the hub's folder, so no Kelvin downloads it itself.

- Which models take pictures, for each of Kelvin's ready-made services (GET /api/models/pictures; Kelvin's
  web/vision.py).
- What each model costs per million tokens, for the model gateway's budgets (llm.py)."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("kelvin.hub")
MODELS_DEV = "https://models.dev/api.json"
# Kelvin's ready-made services (web/services.py), which models.dev names the same
SERVICES = ("openrouter", "opencode", "deepseek", "groq", "mistral", "xai")
# the gateway's services (llm.py), as models.dev names them
PRICED = {"anthropic": "anthropic", "openai": "openai", "gemini": "google", "opencode": "opencode"}
FILE = "models-dev.json"
EVERY = 24 * 3600          # read again after this...
RETRY = 3600               # ...or this, after models.dev couldn't be read
MAX_LIST = 64 * 2 ** 20    # bytes of models.dev's list (about 6 MB in 2026)
COSTS = ("input", "output", "cache_read", "cache_write")   # US dollars per million tokens


def pictures_in(catalog: Any) -> Dict[str, Dict[str, List[str]]]:
    """For each of Kelvin's services, the models that take pictures and those that don't."""
    out: Dict[str, Dict[str, List[str]]] = {}
    for service in SERVICES:
        yes, no = [], []
        for model_id, model in _models(catalog, service).items():
            inputs = (model.get("modalities") or {}).get("input")
            if isinstance(inputs, list):
                (yes if "image" in inputs else no).append(str(model_id))
        if yes or no:
            out[service] = {"yes": sorted(yes), "no": sorted(no)}
    return out


def prices_in(catalog: Any) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """For each of the gateway's services, each model's price: input, output, cache_read and cache_write in dollars
    per million tokens, and dearer tiers past a context size ({size, ...})."""
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for service, listed in PRICED.items():
        found = {}
        for model_id, model in _models(catalog, listed).items():
            cost = _cost(model.get("cost"))
            if cost is None:
                continue
            tiers = []
            for t in (model.get("cost") or {}).get("tiers") or []:
                size = (t.get("tier") or {}).get("size") if isinstance(t, dict) else None
                tier = _cost(t)
                if isinstance(size, (int, float)) and tier is not None:
                    tiers.append(dict(tier, size=int(size)))
            found[str(model_id)] = dict(cost, tiers=sorted(tiers, key=lambda t: t["size"])) if tiers else cost
        if found:
            out[service] = found
    return out


def _models(catalog: Any, service: str) -> Dict[str, Dict[str, Any]]:
    models = (catalog.get(service) or {}).get("models") if isinstance(catalog, dict) else None
    return {k: v for k, v in models.items() if isinstance(v, dict)} if isinstance(models, dict) else {}


def _cost(cost: Any) -> Optional[Dict[str, float]]:
    if not isinstance(cost, dict) or not all(isinstance(cost.get(k), (int, float)) for k in ("input", "output")):
        return None
    return {k: float(cost[k]) for k in COSTS if isinstance(cost.get(k), (int, float))}


def _read_models_dev() -> Any:
    from .._version import __version__
    req = urllib.request.Request(MODELS_DEV, headers={"User-Agent": "Kelvin-hub/%s" % __version__,
                                                      "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(MAX_LIST + 1)
    if len(data) > MAX_LIST:
        raise ValueError("models.dev's list is larger than %d MB" % (MAX_LIST // 2 ** 20))
    return json.loads(data)


class Catalog:
    """models.dev's list as the hub keeps it, read again once a day."""

    def __init__(self, home: Path, read: Optional[Callable[[], Any]] = None):
        """read: models.dev's list (None: from models.dev itself, _read_models_dev)."""
        self.path = Path(home) / FILE
        self.read = read
        self._lock = threading.Lock()
        try:
            kept = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            kept = {}
        self._kept: Dict[str, Any] = kept if isinstance(kept, dict) else {}

    def pictures(self) -> Dict[str, Any]:
        """{"at", "services": {"opencode": {"yes": [...], "no": [...]}, ...}}."""
        self.refresh()
        return {"at": self._kept.get("at"), "services": self._kept.get("services") or {}}

    def price(self, service: str, model: str) -> Optional[Dict[str, Any]]:
        """A model's price on one of the gateway's services (None: models.dev doesn't say). Never reads models.dev
        itself: the gateway asks for that apart (refresh), so an answer's end never waits for it."""
        prices = (self._kept.get("prices") or {}).get(service) or {}
        return prices.get(model) or prices.get(model.split("/")[-1])

    def refresh(self) -> None:
        """Read models.dev again if the list is a day old: one reading at a time, the others go on with what is kept.
        When models.dev can't be read, what is kept stays, and it is tried again an hour later."""
        due = max(float(self._kept.get("at") or 0) + EVERY, float(self._kept.get("tried") or 0) + RETRY)
        if time.time() < due or not self._lock.acquire(blocking=False):
            return
        try:
            self._kept = dict(self._kept, tried=time.time())
            try:
                catalog = (self.read or _read_models_dev)()
                services, prices = pictures_in(catalog), prices_in(catalog)
                if not services and not prices:
                    raise ValueError("no models listed")
                self._kept = {"at": time.time(), "tried": time.time(), "services": services, "prices": prices}
            except Exception as e:   # models.dev unreachable or changed
                log.warning("models.dev: %s", e)
            self._save()
        finally:
            self._lock.release()

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self._kept), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("%s: %s", self.path, e)
