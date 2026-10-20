"""Portal settings: API keys, model choices and the user's global instructions, kept in local JSON files.

Keys are read by the server only and sent nowhere except their own provider;
the browser can see whether a key is configured, never its value.

With accounts (accounts.py) there are two files: the computer's (~/Kelvin/settings.json: which lab, setting up
instruments, phone access, the portal's access key) and the signed-in person's (~/Kelvin/users/<name>/settings.json:
everything else). A person with a password has their keys encrypted with it. Without accounts both are one file.
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
                     "report_kinds", "claude_code", "claude_code_path"}   # the last two: Claude Code bridge
# the computer's, shared by everyone who uses it; the rest is each person's own
LAB = {"access_key", "lab", "auto_setup", "manuals", "scan_network", "auto_cards", "phone", "phone_port"}


def _load(path: Optional[Path]) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _sealed(value: Any) -> bool:
    """A key encrypted for someone else's password (or unreadable here): treated as not set, kept as it is."""
    return isinstance(value, str) and value.startswith("enc:")


class Settings:
    def __init__(self, path: Any):
        self.path = Path(path)
        self._lab: Dict[str, Any] = _load(self.path)
        self._user: Dict[str, Any] = self._lab   # without accounts, one file
        self.user_path: Optional[Path] = None
        self.cipher: Any = None
        if not self._lab.get("access_key"):
            self._lab["access_key"] = secrets.token_urlsafe(24)
            self._save()

    # ------------------------------------------------------------------ whose settings

    def use(self, path: Optional[Any], cipher: Any = None) -> None:
        """Read a signed-in person's settings (None: nobody is signed in). Their keys are decrypted with cipher."""
        self.user_path = Path(path) if path else None
        self.cipher = cipher
        data = _load(self.user_path)
        if cipher is not None:
            for name in SECRET:
                if _sealed(data.get(name)):
                    data[name] = cipher.decrypt(data[name]) or data[name]
            for s in data.get("services") or []:
                if isinstance(s, dict) and _sealed(s.get("api_key")):
                    s["api_key"] = cipher.decrypt(s["api_key"]) or s["api_key"]
        self._user = data

    def adopt(self, path: Any) -> None:
        """The first account takes over what one person kept before accounts: keys, models, instructions, phones..."""
        path = Path(path)
        if path.exists():
            return
        mine = {k: v for k, v in self._lab.items() if k not in LAB}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(mine, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        for k in mine:
            self._lab.pop(k, None)
        if self._user is self._lab:
            self._user = {}
        self._save()

    def _at(self, name: str) -> Dict[str, Any]:
        return self._lab if name in LAB else self._user

    @property
    def access_key(self) -> str:
        return self._lab["access_key"]

    def get(self, name: str) -> Optional[str]:
        value = self._at(name).get(name)
        if value and not _sealed(value):
            return str(value)
        env = ENV.get(name)
        if env == "GEMINI_API_KEY":
            return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or None
        return os.environ.get(env) if env else None

    def source(self, name: str) -> Optional[str]:
        value = self._at(name).get(name)
        if value and not _sealed(value):
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
            if name == "claude_code":  # Claude Code bridge (temporary): on unless off
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
                self._at(name)[name] = value
            else:
                self._at(name).pop(name, None)
        self._save()

    def set_data_dir(self, path: Optional[str]) -> None:
        """The data folder is changed through the runtime (which checks and switches it), never by a plain update."""
        if path:
            self._user["data_dir"] = str(path)
        else:
            self._user.pop("data_dir", None)
        self._save()

    def public(self) -> Dict[str, Any]:
        """What the browser may see: which keys exist and where from, never the keys."""
        return {
            "keys": {name: self.source(name) for name in sorted(SECRET)},
            "local_base_url": self._user.get("local_base_url", ""),
            "local_models": self._user.get("local_models", ""),
            "default_model": self._user.get("default_model", ""),
            "instructions": self._user.get("instructions", ""),
            "learn": self.learning,
            "notify_kinds": self.notify_kinds,
            "reviewer": self._user.get("reviewer", ""),
            "literature": self._user.get("literature") != "off",
            "auto_setup": self._lab.get("auto_setup") != "off",
            "manuals": self._lab.get("manuals") != "off",
            "scan_network": self._lab.get("scan_network") != "off",
            "auto_cards": self._lab.get("auto_cards") != "off",
            "auto_report": self._user.get("auto_report") or "both",
            "report_kinds": self.report_kinds,
            "claude_code": self._user.get("claude_code") != "off",          # Claude Code bridge (temporary)
            "claude_code_path": self._user.get("claude_code_path", ""),
        }

    @property
    def report_kinds(self) -> list:
        raw = self._user.get("report_kinds")
        return ["scan", "device", "project"] if not raw else [k for k in raw.split(",") if k and k != "none"]

    @property
    def notify_kinds(self) -> list:
        from ..notify import DEFAULT_KINDS
        raw = self._user.get("notify_kinds")
        return list(DEFAULT_KINDS) if raw is None else [k for k in raw.split(",") if k and k != "none"]

    @property
    def learning(self) -> bool:
        """May Kelvin save notes from conversations? On unless the user switched it off."""
        return self._user.get("learn") != "off"

    # ------------------------------------------------------------------ other model services (see services.py)

    @property
    def services(self) -> List[Dict[str, Any]]:
        return [s for s in self._user.get("services") or [] if isinstance(s, dict) and s.get("id")]

    def service_key(self, service: Dict[str, Any]) -> Optional[str]:
        from .services import PRESETS
        env = PRESETS.get(service.get("preset", ""), {}).get("env")
        key = service.get("api_key")
        return (key if key and not _sealed(key) else None) or (os.environ.get(env) if env else None) or None

    def service_views(self) -> List[Dict[str, Any]]:
        from .services import view
        return [view(s, "settings" if s.get("api_key") and not _sealed(s["api_key"])
                     else ("environment" if self.service_key(s) else None)) for s in self.services]

    def add_service(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        from .services import MAX_SERVICES, make
        if len(self.services) >= MAX_SERVICES:
            raise ValueError("at most %d services" % MAX_SERVICES)
        service = make(entry, [s["id"] for s in self.services])
        self._user["services"] = self.services + [service]
        self._save()
        return service

    def remove_service(self, service_id: str) -> None:
        kept = [s for s in self.services if s["id"] != service_id]
        if len(kept) == len(self.services):
            raise KeyError("no service %r" % service_id)
        self._user["services"] = kept
        self._save()

    # ------------------------------------------------------------------ paired phones (see remote.py): each person's

    @property
    def devices(self) -> List[Dict[str, Any]]:
        return [d for d in self._user.get("devices") or [] if isinstance(d, dict) and d.get("hash")]

    def device_views(self) -> List[Dict[str, Any]]:
        return [{k: d.get(k) for k in ("id", "name", "role", "created", "last_seen")} for d in self.devices]

    def add_device(self, name: str, role: str, token_hash: str) -> Dict[str, Any]:
        device = {"id": "d" + secrets.token_hex(4), "name": name, "role": role, "hash": token_hash,
                  "created": time.time(), "last_seen": None}
        self._user["devices"] = self.devices + [device]
        self._save()
        return device

    def remove_device(self, device_id: str) -> None:
        kept = [d for d in self.devices if d["id"] != device_id]
        if len(kept) == len(self.devices):
            raise KeyError("no paired device %r" % device_id)
        self._user["devices"] = kept
        self._save()

    def touch_device(self, device_id: str, when: float) -> None:
        for d in self._user.get("devices") or []:
            if d.get("id") == device_id:
                d["last_seen"] = when
                self._save()

    @property
    def phone(self) -> Tuple[bool, int]:
        """Phone access on or off, and its port."""
        from .remote import PHONE_PORT
        return self._lab.get("phone") == "on", int(self._lab.get("phone_port") or PHONE_PORT)

    def set_phone(self, on: bool, port: int) -> None:
        if not 1024 <= int(port) <= 65535:
            raise ValueError("the port must be between 1024 and 65535")
        self._lab["phone"] = "on" if on else "off"
        self._lab["phone_port"] = int(port)
        self._save()

    def _save(self) -> None:
        def write(path: Path, data: Dict[str, Any]) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, path)

        write(self.path, self._lab)
        if self.user_path is not None and self._user is not self._lab:
            mine = json.loads(json.dumps(self._user))
            if self.cipher is not None:   # their password protects their keys
                for name in SECRET:
                    if mine.get(name) and not _sealed(mine[name]):
                        mine[name] = self.cipher.encrypt(str(mine[name]))
                for s in mine.get("services") or []:
                    if isinstance(s, dict) and s.get("api_key") and not _sealed(s["api_key"]):
                        s["api_key"] = self.cipher.encrypt(str(s["api_key"]))
            write(self.user_path, mine)
