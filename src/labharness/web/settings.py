"""Portal settings: API keys, model choices and the user's global instructions, kept in a local JSON file.

Keys are read by the server only and sent nowhere except their own provider;
the browser can see whether a key is configured, never its value.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .store import MAX_INSTRUCTIONS

ENV = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "gemini_api_key": "GEMINI_API_KEY",
}
SECRET = set(ENV) | {"local_api_key", "webhook", "openalex_api_key"}   # a webhook URL is a credential too
EDITABLE = SECRET | {"local_base_url", "local_models", "default_model", "instructions", "learn", "lab", "notify_kinds",
                     "reviewer", "literature", "auto_setup", "manuals", "scan_network", "auto_cards", "auto_report",
                     "report_kinds"}


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
            if name == "literature":  # literature search sends queries to arXiv and OpenAlex; only "off" is stored
                value = "off" if value.lower() in ("off", "false", "0", "no") else ""
            if name in ("auto_setup", "manuals", "scan_network", "auto_cards"):  # setting up instruments: on unless off
                value = "off" if value.lower() in ("off", "false", "0", "no") else ""
            if name == "learn":  # Kelvin may save notes from conversations; only "off" is stored
                value = "off" if value.lower() in ("off", "false", "0", "no") else ""
            if name == "auto_report":  # a report after each run and queue: both (the default), pdf, pptx or off
                value = value if value in ("pdf", "pptx", "off") else ""
            if name == "report_kinds":  # which reports Kelvin keeps up to date: scan, device, project (all by default)
                kept = [k for k in ("scan", "device", "project") if k in value.split(",")]
                value = "" if len(kept) == 3 else ",".join(kept) or "none"
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

    def set_data_dir(self, path: Optional[str]) -> None:
        """The data folder is changed through the runtime (which checks and switches it), never by a plain update."""
        if path:
            self._data["data_dir"] = str(path)
        else:
            self._data.pop("data_dir", None)
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
            "literature": self._data.get("literature") != "off",
            "auto_setup": self._data.get("auto_setup") != "off",
            "manuals": self._data.get("manuals") != "off",
            "scan_network": self._data.get("scan_network") != "off",
            "auto_cards": self._data.get("auto_cards") != "off",
            "auto_report": self._data.get("auto_report") or "both",
            "report_kinds": self.report_kinds,
        }

    @property
    def report_kinds(self) -> list:
        raw = self._data.get("report_kinds")
        return ["scan", "device", "project"] if not raw else [k for k in raw.split(",") if k and k != "none"]

    @property
    def notify_kinds(self) -> list:
        from ..notify import DEFAULT_KINDS
        raw = self._data.get("notify_kinds")
        return list(DEFAULT_KINDS) if raw is None else [k for k in raw.split(",") if k and k != "none"]

    @property
    def learning(self) -> bool:
        """May Kelvin save notes from conversations? On unless the user switched it off."""
        return self._data.get("learn") != "off"

    # ------------------------------------------------------------------ other model services (see services.py)

    @property
    def services(self) -> List[Dict[str, Any]]:
        return [s for s in self._data.get("services") or [] if isinstance(s, dict) and s.get("id")]

    def service_key(self, service: Dict[str, Any]) -> Optional[str]:
        from .services import PRESETS
        env = PRESETS.get(service.get("preset", ""), {}).get("env")
        return service.get("api_key") or (os.environ.get(env) if env else None) or None

    def service_views(self) -> List[Dict[str, Any]]:
        from .services import view
        return [view(s, "settings" if s.get("api_key") else ("environment" if self.service_key(s) else None))
                for s in self.services]

    def add_service(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        from .services import MAX_SERVICES, make
        if len(self.services) >= MAX_SERVICES:
            raise ValueError("at most %d services" % MAX_SERVICES)
        service = make(entry, [s["id"] for s in self.services])
        self._data["services"] = self.services + [service]
        self._save()
        return service

    def remove_service(self, service_id: str) -> None:
        kept = [s for s in self.services if s["id"] != service_id]
        if len(kept) == len(self.services):
            raise KeyError("no service %r" % service_id)
        self._data["services"] = kept
        self._save()

    # ------------------------------------------------------------------ paired phones (see remote.py)

    @property
    def devices(self) -> List[Dict[str, Any]]:
        return [d for d in self._data.get("devices") or [] if isinstance(d, dict) and d.get("hash")]

    def device_views(self) -> List[Dict[str, Any]]:
        return [{k: d.get(k) for k in ("id", "name", "role", "created", "last_seen")} for d in self.devices]

    def add_device(self, name: str, role: str, token_hash: str) -> Dict[str, Any]:
        device = {"id": "d" + secrets.token_hex(4), "name": name, "role": role, "hash": token_hash,
                  "created": time.time(), "last_seen": None}
        self._data["devices"] = self.devices + [device]
        self._save()
        return device

    def remove_device(self, device_id: str) -> None:
        kept = [d for d in self.devices if d["id"] != device_id]
        if len(kept) == len(self.devices):
            raise KeyError("no paired device %r" % device_id)
        self._data["devices"] = kept
        self._save()

    def touch_device(self, device_id: str, when: float) -> None:
        for d in self._data.get("devices") or []:
            if d.get("id") == device_id:
                d["last_seen"] = when
                self._save()

    @property
    def phone(self) -> Tuple[bool, int]:
        """Phone access on or off, and its port."""
        from .remote import PHONE_PORT
        return self._data.get("phone") == "on", int(self._data.get("phone_port") or PHONE_PORT)

    def set_phone(self, on: bool, port: int) -> None:
        if not 1024 <= int(port) <= 65535:
            raise ValueError("the port must be between 1024 and 65535")
        self._data["phone"] = "on" if on else "off"
        self._data["phone_port"] = int(port)
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
