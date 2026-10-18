"""Portal settings: API keys, model choices and the user's global instructions, kept in a local JSON file.

Keys are read by the server only and sent nowhere except their own provider;
the browser can see whether a key is configured, never its value.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any, Dict, Optional

from .store import MAX_INSTRUCTIONS

ENV = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "gemini_api_key": "GEMINI_API_KEY",
}
SECRET = set(ENV) | {"local_api_key", "webhook"}   # a webhook URL is a credential too
EDITABLE = SECRET | {"local_base_url", "local_models", "default_model", "instructions", "learn", "lab", "notify_kinds",
                     "reviewer"}


class Settings:
    def __init__(self, path: Any):
        self.path = Path(path)
        self._data: Dict[str, Any] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError:
                self._data = {}
        if not self._data.get("access_key"):
            self._data["access_key"] = secrets.token_urlsafe(24)
            self._save()

    @property
    def access_key(self) -> str:
        return self._data["access_key"]

    def get(self, name: str) -> Optional[str]:
        value = self._data.get(name)
        if value:
            return str(value)
        env = ENV.get(name)
        if env == "GEMINI_API_KEY":
            return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or None
        return os.environ.get(env) if env else None

    def source(self, name: str) -> Optional[str]:
        if self._data.get(name):
            return "settings"
        return "environment" if self.get(name) else None

    def update(self, values: Dict[str, Any]) -> None:
        for name, value in values.items():
            if name not in EDITABLE or value is None:
                continue
            value = str(value).strip()
            if name == "instructions":  # read by Kelvin in every conversation
                value = value[:MAX_INSTRUCTIONS]
            if name == "learn":  # Kelvin may save notes from conversations; only "off" is stored
                value = "off" if value.lower() in ("off", "false", "0", "no") else ""
            if name == "lab":  # which lab to open: the user's lab.yaml ("mine") or the virtual lab
                value = value if value in ("mine", "virtual") else ""
            if name == "webhook" and value and not value.startswith(("https://", "http://")):
                raise ValueError("The notification webhook must be an http(s) URL.")
            if name == "notify_kinds":  # which notices also go to the webhook
                from ..notify import KINDS
                value = ",".join(k for k in value.split(",") if k in KINDS) or "none"
            if value:
                self._data[name] = value
            else:
                self._data.pop(name, None)
        self._save()

    def public(self) -> Dict[str, Any]:
        """What the browser may see: which keys exist and where from, never the keys."""
        return {
            "keys": {name: self.source(name) for name in sorted(SECRET)},
            "local_base_url": self._data.get("local_base_url", ""),
            "local_models": self._data.get("local_models", ""),
            "default_model": self._data.get("default_model", ""),
            "instructions": self._data.get("instructions", ""),
            "learn": self.learning,
            "notify_kinds": self.notify_kinds,
            "reviewer": self._data.get("reviewer", ""),
        }

    @property
    def notify_kinds(self) -> list:
        from ..notify import DEFAULT_KINDS
        raw = self._data.get("notify_kinds")
        return list(DEFAULT_KINDS) if raw is None else [k for k in raw.split(",") if k and k != "none"]

    @property
    def learning(self) -> bool:
        """May Kelvin save notes from conversations? On unless the user switched it off."""
        return self._data.get("learn") != "off"

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
