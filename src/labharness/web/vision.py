"""Which models see pictures, on services whose models differ: OpenRouter, OpenCode, DeepSeek, Groq, Mistral, xAI, a
gateway, a local server (openai_provider.py). Claude, Gemini, GPT on OpenAI's own API and the ChatGPT plan all do.

Kelvin goes by the first of these that knows:
1. the person's switch for the service (Settings -> Other model services: Pictures on or off);
2. the model refused a picture on this computer before (kept REMEMBER_DAYS: services add pictures to models);
3. the service's own model list, where it says (OpenRouter lists each model's inputs);
4. the Kelvin hub's list (from models.dev, which the hub reads once a day), for a person signed in to the hub;
5. otherwise Kelvin sends them, and a model that refuses gets the message without them and is remembered.

Kept in pictures.json beside the person's settings: the models that refused, and the hub's list. Never a picture."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

log = logging.getLogger("kelvin")
FILE = "pictures.json"
REMEMBER_DAYS = 30        # a model that refused pictures gets them again after this
HUB_EVERY = 24 * 3600     # the hub's list is fetched again after this...
HUB_RETRY = 3600          # ...or this, after the hub couldn't be reached

Hub = Callable[[], Optional[Tuple[str, str]]]   # (the hub's address, the person's token there), None: not signed in


def _from_hub(url: str, token: str) -> Dict[str, Any]:
    from .. import central
    return central.Central(url).pictures(token)


class Vision:
    def __init__(self, folder: Optional[Any], hub: Hub = lambda: None,
                 fetch: Callable[[str, str], Dict[str, Any]] = _from_hub):
        self.path = Path(folder) / FILE if folder else None
        self.hub = hub
        self.fetch = fetch
        self._lock = threading.Lock()
        self._fetching = False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path else {}
        except (OSError, ValueError):
            data = {}
        self._data: Dict[str, Any] = data if isinstance(data, dict) else {}

    def _save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self._data), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("%s: %s", self.path, e)

    def refused(self, service: str, model: str) -> bool:
        at = (self._data.get("refused") or {}).get("%s/%s" % (service, model))
        return isinstance(at, (int, float)) and time.time() - at < REMEMBER_DAYS * 86400

    def refuse(self, service: str, model: str) -> None:
        with self._lock:
            self._data.setdefault("refused", {})["%s/%s" % (service, model)] = time.time()
            self._save()

    def hub_says(self, service: str, model: str) -> Optional[bool]:
        """What the hub's list says of this model on this service (Kelvin's preset name: opencode, deepseek...); None
        when it doesn't know. A list a day old is fetched again meanwhile."""
        self._refresh()
        listed = ((self._data.get("hub") or {}).get("services") or {}).get(service)
        if not isinstance(listed, dict):
            return None
        if model in (listed.get("yes") or ()):
            return True
        if model in (listed.get("no") or ()):
            return False
        return None

    def _refresh(self) -> None:
        kept = self._data.get("hub") or {}
        due = max(float(kept.get("at") or 0) + HUB_EVERY, float(kept.get("tried") or 0) + HUB_RETRY)
        if self._fetching or time.time() < due:
            return
        where = self.hub()
        if not where:
            return
        self._fetching = True
        threading.Thread(target=self.fetch_now, args=where, daemon=True, name="kelvin-pictures").start()

    def fetch_now(self, url: str, token: str) -> bool:
        """Fetch the hub's list now (in the background, from hub_says); False if the hub couldn't give it."""
        try:
            answer = self.fetch(url, token)
            services = answer.get("services") if isinstance(answer, dict) else None
            if not isinstance(services, dict):
                raise ValueError("no list of services")
        except Exception as e:   # offline, an older hub, nonsense: the list stays as it was, tried again later
            log.info("the Kelvin hub's list of models that see pictures: %s", e)
            with self._lock:
                self._data["hub"] = dict(self._data.get("hub") or {}, tried=time.time())
                self._save()
            return False
        finally:
            self._fetching = False
        with self._lock:
            self._data["hub"] = {"at": time.time(), "tried": time.time(), "services": {
                str(k): {"yes": [str(m) for m in v.get("yes") or []], "no": [str(m) for m in v.get("no") or []]}
                for k, v in services.items() if isinstance(v, dict)}}
            self._save()
        return True
