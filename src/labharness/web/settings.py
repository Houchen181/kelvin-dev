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
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .store import MAX_INSTRUCTIONS

ENV = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "gemini_api_key": "GEMINI_API_KEY",
}
# a webhook or heartbeat URL is a credential too, and so is the email account's password
SECRET = set(ENV) | {"local_api_key", "webhook", "openalex_api_key", "smtp_password", "heartbeat"}
# secrets Kelvin keeps itself, never shown or set from the browser: the Kelvin hub's token (hubsync.py)
SEALED = SECRET | {"hub_token", "account_token"}   # encrypted with the password
EDITABLE = SECRET | {"local_base_url", "local_models", "default_model", "instructions", "learn", "notify_kinds",
                     "reviewer", "literature", "auto_setup", "manuals", "scan_network", "auto_cards", "auto_report",
                     "answer_words", "phone_relay", "ask_folder", "text_size", "card_language", "checklist",
                     "report_kinds", "claude_code", "claude_code_path",   # these two: Claude Code bridge
                     "updates", "hub_old_runs", "collection", "idle_off", "idle_off_all",
                     "smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_from", "smtp_to",   # email notices
                     "heartbeat_minutes", "approval_wait", "lab_scripts_python"}
# the computer's, shared by everyone who uses it; the rest is each person's own
LAB = {"access_key", "auto_setup", "manuals", "scan_network", "auto_cards", "phone", "phone_port", "updates",
       "account_hub", "hub_installation", "instruments_off", "hands_on", "lab_scripts", "collection", "idle_off",
       "idle_off_all", "approval_wait", "lab_scripts_python"}

ANSWER_WORDS = 200   # the default word budget for answers (brevity.py)
IDLE_OFF_MINUTES = 60   # outputs left on this long with nothing happening go to safe (LabRuntime._idle_check)
APPROVAL_WAIT_MINUTES = 60   # a request nobody answered at the computer waits this long for the phone, then declines
HEARTBEAT_MINUTES = 5        # minutes between heartbeat pings
MAX_RECENT_FOLDERS = 8   # chats' folders the folder menu offers again
TEXT_SIZES = (0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0)   # the desktop window's zoom, each person's own
CARD_LANGUAGES = ("zh-Hans",)   # approval cards shown in another language (cardlang.py); "" is English, as written

# The model a chat starts with when nobody chose one under Settings → Models → Default model for new chats: Claude
# Opus, through a Claude API key, else through the Claude Code on this computer (the owner's choice, 2026-10-29).
# Settings says which model Automatic is, since F3 #25 found it silently Opus; a person who wants another picks it there.
DEFAULT_MODELS = ("anthropic:claude-opus-5-5", "hub-anthropic:claude-opus-5-5", "claudecode:opus")
# Claude's models named without their service, as Claude Code takes them ("sonnet") or by their id alone
# ("claude-sonnet-5-5"): F3 #25 found "claudecode:sonnet" needed in some places while "sonnet" worked in others
CLAUDE = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5", "haiku": "claude-haiku-5-5",
          "fable": "claude-fable-5-1"}
BRIDGE = {"claude-sonnet-5-5": "sonnet", "claude-opus-5-5": "opus", "claude-haiku-5-5": "haiku"}   # Claude Code's


def _own_keys() -> bool:
    from ..edition import own_keys
    return own_keys()


def default_model(keys: List[str], chosen: Optional[str] = "") -> Optional[str]:
    """The model a new chat starts with, of the keys the services offer now: the one chosen in Settings while it is
    offered, else Claude Opus (DEFAULT_MODELS), else the first one (None: none)."""
    if chosen and chosen in keys:
        return chosen
    for key in DEFAULT_MODELS:
        if key in keys:
            return key
    return (list(keys) or [None])[0]


def resolve_model(key: str, available: Any) -> str:
    """A model key as the services know it ("anthropic:claude-sonnet-5-5"), from one written as people and Claude Code
    write it: "sonnet", "claude-sonnet-5-5", "anthropic:sonnet", "claudecode:claude-sonnet-5-5". A bare Claude model
    goes to the Claude API key when there is one, else through the Kelvin hub, else to Claude Code.
    available(service): is it set up here? Anything else comes back as it was (the caller says why it can't be
    used)."""
    text = str(key or "").strip()
    service, colon, model = text.partition(":")
    if not colon:   # no service named: a Claude model's name or id alone
        model, service = text, ""
    low = model.lower()
    full = CLAUDE.get(low, low if low in CLAUDE.values() else "")
    if not full:
        return text
    if service == "anthropic" or (not service and available("anthropic")):
        return "anthropic:" + full
    if service == "hub-anthropic" or (not service and available("hub-anthropic")):   # through the Kelvin hub
        return "hub-anthropic:" + full
    if (service == "claudecode" or not service) and full in BRIDGE and (service or available("claudecode")):
        return "claudecode:" + BRIDGE[full]
    return text


def bridge_alias(model_key: str) -> str:
    """The Claude Code model for a job Claude Code does for a chat (operate_computer): the chat's own Claude model,
    else the default's, Opus."""
    service, _, model = str(model_key or "").partition(":")
    default = BRIDGE[DEFAULT_MODELS[0].partition(":")[2]]
    if service == "claudecode" and model:
        return model
    if service == "anthropic":
        return BRIDGE.get(model, default)
    return default


def _text_size(value: str) -> str:
    """What is stored for the text size: the nearest of TEXT_SIZES as a factor ("" for 100%)."""
    try:
        size = min(TEXT_SIZES, key=lambda s: abs(s - float(value)))
    except ValueError:
        return ""
    return "" if size == 1.0 else "%g" % size


def _answer_words(value: str) -> str:
    """What is stored for the answer length: "off", or a number of words from 50 to 2000 ("" for the default)."""
    if value.lower() in ("off", "0", "no", "false", "any"):
        return "off"
    try:
        n = max(50, min(2000, int(float(value))))
    except ValueError:
        return ""
    return "" if n == ANSWER_WORDS else str(n)


def _idle_off(value: str) -> str:
    """What is stored for the idle switch-off: "off", or minutes from 5 to 1440 ("" for the default)."""
    if value.lower() in ("off", "0", "no", "false", "never"):
        return "off"
    try:
        n = max(5, min(1440, int(float(value))))
    except ValueError:
        return ""
    return "" if n == IDLE_OFF_MINUTES else str(n)


def _minutes(value: str, low: int, high: int, default: int, off: bool = False) -> str:
    """What is stored for a number of minutes: "off" (where allowed), or minutes from low to high ("" for the
    default)."""
    if off and value.lower() in ("off", "0", "no", "false", "never"):
        return "off"
    try:
        n = max(low, min(high, int(float(value))))
    except ValueError:
        return ""
    return "" if n == default else str(n)


def _load(path: Optional[Path]) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def reseal(folder: Any, old: Any, new: Any) -> int:
    """Re-encrypt a person's keys (settings.json) and ChatGPT sign-in (chatgpt.json) from one password's cipher to
    another's (None: not encrypted): after a password change, or when a local account is linked to a central one.
    Keys sealed for neither stay as they are. Returns how many were re-encrypted."""
    folder = Path(folder)
    turned = [0]

    def turn(value: Any) -> Any:
        if not value:
            return value
        if _sealed(value):
            plain = old.decrypt(value) if old is not None else None
            if plain is None or _sealed(plain):
                return value   # sealed for another password: kept as it is
        else:
            plain = value
        turned[0] += 1
        return new.encrypt(str(plain)) if new is not None else plain

    path = folder / "settings.json"
    data = _load(path)
    if data:
        for name in SEALED:
            if data.get(name):
                data[name] = turn(data[name])
        for s in data.get("services") or []:
            if isinstance(s, dict) and s.get("api_key"):
                s["api_key"] = turn(s["api_key"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    path = folder / "chatgpt.json"
    signin = _load(path)
    if signin:
        if "sealed" in signin:
            inner = old.decrypt(signin["sealed"]) if old is not None else None
            signin = json.loads(inner) if inner and not _sealed(inner) else None
        if signin is not None:
            turned[0] += 1
            out = {"sealed": new.encrypt(json.dumps(signin))} if new is not None else signin
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(out, indent=1), encoding="utf-8")
            os.replace(tmp, path)
    return turned[0]


def unreadable(folder: Any, cipher: Any) -> int:
    """How many of a person's keys (and their ChatGPT sign-in) this cipher can't open: sealed with another password."""
    folder = Path(folder)
    shut = lambda v: _sealed(v) and (cipher is None or cipher.decrypt(v) is None)   # noqa: E731
    data = _load(folder / "settings.json")
    n = sum(1 for name in SECRET | {"account_token", "hub_token"} if shut(data.get(name)))
    n += sum(1 for s in data.get("services") or [] if isinstance(s, dict) and shut(s.get("api_key")))
    signin = _load(folder / "chatgpt.json")
    return n + (1 if "sealed" in signin and shut(signin["sealed"]) else 0)


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
            for name in SEALED:
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
            if name == "collection":  # the instrument collection (collection.py): on unless off; off, no network
                value = "off" if value.lower() in ("off", "false", "0", "no") else ""
            if name == "updates":  # keeping Kelvin up to date (updater.py): auto (the default), ask or off
                value = value if value in ("ask", "off") else ""
            if name == "claude_code":  # Claude Code bridge (temporary): on unless off
                value = "off" if value.lower() in ("off", "false", "0", "no") else ""
            if name == "phone_relay":  # phones through the Kelvin hub (relay.py): on unless off
                value = "off" if value.lower() in ("off", "false", "0", "no") else ""
            if name == "hub_old_runs":  # also share runs from before Kelvin noted who made them: off unless on
                value = "on" if value.lower() in ("on", "true", "1", "yes") else ""
            if name == "answer_words":  # how long the model is asked to keep answers (brevity.py)
                value = _answer_words(value)
            if name == "idle_off":  # minutes before light left on with nothing happening goes to safe, or off
                value = _idle_off(value)
            if name == "idle_off_all":  # other outputs (sources, excitations, RF) too: off unless on
                value = "on" if value.lower() in ("on", "true", "1", "yes") else ""
            if name == "text_size":  # the desktop window's zoom (desktop.py), as a factor
                value = _text_size(value)
            if name == "card_language":  # approval cards shown in this language (cardlang.py); "" English, as written
                value = value if value in CARD_LANGUAGES else ""
            if name == "checklist":  # the first-run checklist on the start screen: shown until hidden
                value = "hidden" if value.lower() in ("hidden", "hide", "off", "false", "0", "no") else ""
            if name == "ask_folder":  # the folder picker opens for each new chat (desktop app): on unless off
                value = "off" if value.lower() in ("off", "false", "0", "no") else ""
            if name == "learn":  # Kelvin may save notes from conversations; only "off" is stored
                value = "off" if value.lower() in ("off", "false", "0", "no") else ""
            if name == "auto_report":  # a report after each run and queue: both (the default), pdf, pptx or off
                value = value if value in ("pdf", "pptx", "off") else ""
            if name == "report_kinds":  # which reports Kelvin keeps up to date: scan, device, project (all by default)
                kept = [k for k in ("scan", "device", "project") if k in value.split(",")]
                value = "" if len(kept) == 3 else ",".join(kept) or "none"
            if name == "webhook" and value and not value.startswith(("https://", "http://")):
                raise ValueError("The notification webhook must be an http(s) URL.")
            if name == "heartbeat" and value and not value.startswith(("https://", "http://")):
                raise ValueError("The heartbeat address must be an http(s) URL.")
            if name == "heartbeat_minutes":  # between heartbeat pings: 1 to 60 minutes
                value = _minutes(value, 1, 60, HEARTBEAT_MINUTES)
            if name == "approval_wait":  # how long a request goes on waiting once it went to the phone, or off
                value = _minutes(value, 5, 720, APPROVAL_WAIT_MINUTES, off=True)
            if name == "smtp_security":  # starttls (the default), ssl or none
                value = value.lower() if value.lower() in ("ssl", "none") else ""
            if name in ("smtp_host", "smtp_port", "smtp_to", "smtp_from"):
                from ..notify import email_from
                mail = {k: self._at(k).get(k) for k in ("smtp_host", "smtp_port", "smtp_to", "smtp_from")}
                mail[name] = value
                email_from(mail["smtp_host"] or "x", mail["smtp_to"] or "x@y", mail["smtp_port"], None, None, None,
                           mail["smtp_from"])   # ValueError for settings that can't work
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

    def remember_folder(self, path: Optional[str]) -> None:
        """A folder this person chose for a chat (checked by the portal first): a new chat starts there, and it heads
        their recent folders. None: new chats start in the data folder again."""
        if path:
            self._user["last_folder"] = str(path)
            recent = [p for p in self.recent_folders if p.lower() != str(path).lower()]
            self._user["recent_folders"] = [str(path)] + recent[:MAX_RECENT_FOLDERS - 1]
        else:
            self._user.pop("last_folder", None)
        self._save()

    @property
    def recent_folders(self) -> List[str]:
        kept = self._user.get("recent_folders")
        return [str(p) for p in kept if p] if isinstance(kept, list) else []

    # ------------------------------------------------------------------ the Kelvin hub (hubsync.py)

    def set_hub(self, url: Optional[str], token: Optional[str]) -> None:
        """The hub this person connected: its address, and the token (encrypted with their password, like the keys)."""
        for name, value in (("hub_url", url), ("hub_token", token)):
            if value:
                self._user[name] = str(value)
            else:
                self._user.pop(name, None)
        self._save()

    def hub_installations(self) -> List[str]:
        """The installation ids this person connected to the hub with, latest first (for the hub to fold together)."""
        return [str(i) for i in self._user.get("hub_installations") or [] if i][:5]

    def add_hub_installation(self, ident: str) -> None:
        self._user["hub_installations"] = [ident] + [i for i in self.hub_installations() if i != ident][:4]
        self._save()

    def installation_id(self) -> str:
        """This Kelvin's own stable id, which the hub keys what it shares by: the computer's, made once."""
        if not self._lab.get("hub_installation"):
            self._lab["hub_installation"] = "urn:uuid:" + str(uuid.uuid4())
            self._save()
        return self._lab["hub_installation"]

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
            "answer_words": self.answer_words,
            "literature": self._user.get("literature") != "off",
            "auto_setup": self._lab.get("auto_setup") != "off",
            "manuals": self._lab.get("manuals") != "off",
            "scan_network": self._lab.get("scan_network") != "off",
            "auto_cards": self._lab.get("auto_cards") != "off",
            "collection": self._lab.get("collection") != "off",
            "auto_report": self._user.get("auto_report") or "both",
            "report_kinds": self.report_kinds,
            "ask_folder": self._user.get("ask_folder") != "off",
            "text_size": float(_text_size(str(self._user.get("text_size") or 1)) or 1),
            "card_language": self.card_language,
            "checklist": self._user.get("checklist") != "hidden",
            "updates": self.updates,
            "claude_code": self._user.get("claude_code") != "off",          # Claude Code bridge (temporary)
            "own_keys": _own_keys(),   # Kelvin from source: the person's own API keys and services (edition.py)
            "claude_code_path": self._user.get("claude_code_path", ""),
            "hub_old_runs": self._user.get("hub_old_runs") == "on",
            "idle_off": self.idle_off_minutes,
            "idle_off_all": self._lab.get("idle_off_all") == "on",
            # email notices: where they go and from which account (its password is among the keys, never shown)
            "email": {"host": self._user.get("smtp_host", ""), "port": self._user.get("smtp_port", ""),
                      "security": self._user.get("smtp_security") or "starttls", "user": self._user.get("smtp_user", ""),
                      "from": self._user.get("smtp_from", ""), "to": self._user.get("smtp_to", "")},
            "heartbeat_minutes": int(self.heartbeat_s // 60),
            "approval_wait": int(self.approval_wait_s // 60),   # 0: declined after the first wait, as before
            "lab_scripts": self.lab_scripts or "",
            "lab_scripts_python": self.lab_scripts_python,
        }

    @property
    def account_hub(self) -> str:
        """The Kelvin hub this computer's people sign in to (central accounts), if one is set here."""
        return str(self._lab.get("account_hub") or "")

    def set_account_hub(self, url: str) -> None:
        if url:
            self._lab["account_hub"] = url
        else:
            self._lab.pop("account_hub", None)
        self._save()

    def put_secret(self, name: str, value: Optional[str]) -> None:
        """A credential of the person's that the browser never sets (the hub's token for them)."""
        if value:
            self._user[name] = value
        else:
            self._user.pop(name, None)
        self._save()

    @property
    def answer_words(self) -> int:
        """How many words the model is asked to keep each answer under (brevity.py); 0: any length."""
        value = self._user.get("answer_words")
        if value == "off":
            return 0
        return int(value) if value and value.isdigit() else ANSWER_WORDS

    @property
    def card_language(self) -> str:
        """The language this person reads approval cards in ("" English, as Kelvin writes them; cardlang.py)."""
        value = str(self._user.get("card_language") or "")
        return value if value in CARD_LANGUAGES else ""

    @property
    def idle_off_minutes(self) -> int:
        """Minutes before outputs left on with nothing happening go to safe; 0: never."""
        value = self._lab.get("idle_off")
        if value == "off":
            return 0
        return int(value) if value and str(value).isdigit() else IDLE_OFF_MINUTES

    def email(self) -> Any:
        """Where email notices go (notify.Email), or None while no server or recipient is set."""
        from ..notify import email_from
        try:
            return email_from(self.get("smtp_host"), self.get("smtp_to"), self.get("smtp_port"),
                              self.get("smtp_security"), self.get("smtp_user"), self.get("smtp_password"),
                              self.get("smtp_from"))
        except ValueError:
            return None

    @property
    def heartbeat_s(self) -> float:
        value = self._user.get("heartbeat_minutes")
        return 60.0 * (int(value) if value and str(value).isdigit() else HEARTBEAT_MINUTES)

    @property
    def approval_wait_s(self) -> float:
        """How long a request nobody answered at the computer waits for an answer from the phone; 0: it doesn't."""
        value = self._lab.get("approval_wait")
        if value == "off":
            return 0.0
        return 60.0 * (int(value) if value and str(value).isdigit() else APPROVAL_WAIT_MINUTES)

    @property
    def updates(self) -> str:
        return self._lab.get("updates") or "auto"

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

    def service_pictures(self, service_id: str, value: Any) -> None:
        """Whether a service's models see pictures: "on", "off", or "" (model by model, vision.py)."""
        from .services import pictures_of
        value = pictures_of(value)
        services = self.services
        found = next((s for s in services if s["id"] == service_id), None)
        if found is None:
            raise KeyError("no service %r" % service_id)
        found.pop("pictures", None)
        if value:
            found["pictures"] = value
        self._user["services"] = services
        self._save()

    # ------------------------------------------------------------------ paired phones (see remote.py): each person's

    @property
    def devices(self) -> List[Dict[str, Any]]:
        return [d for d in self._user.get("devices") or [] if isinstance(d, dict) and d.get("hash")]

    def device_views(self) -> List[Dict[str, Any]]:
        return [{k: d.get(k) for k in ("id", "name", "role", "created", "last_seen")} for d in self.devices]

    def add_device(self, name: str, role: str, token_hash: str, phone: Optional[str] = None) -> Dict[str, Any]:
        """phone: the id of a phone signed in to its person's Kelvin account, which got this token through the hub;
        it replaces that phone's token from before."""
        device = {"id": "d" + secrets.token_hex(4), "name": name, "role": role, "hash": token_hash,
                  "created": time.time(), "last_seen": None}
        kept = self.devices
        if phone:
            device["phone"] = phone
            kept = [d for d in kept if d.get("phone") != phone]
        self._user["devices"] = kept + [device]
        self._save()
        return device

    def remove_phone(self, phone: str) -> bool:
        """Forget the token a phone got through the hub (it signed out of the Kelvin account)."""
        kept = [d for d in self.devices if d.get("phone") != phone]
        if len(kept) == len(self.devices) or not phone:
            return False
        self._user["devices"] = kept
        self._save()
        return True

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

    def switched_off(self, lab: str) -> List[str]:
        """The instruments a person switched off in this lab (Lab panel): Kelvin sends them nothing. Everyone's,
        like the instruments."""
        off = self._lab.get("instruments_off")
        names = off.get(str(lab)) if isinstance(off, dict) else None
        return [str(n) for n in names] if isinstance(names, list) else []

    def set_switched_off(self, lab: str, names: List[str]) -> None:
        off = dict(self._lab.get("instruments_off") or {})
        if names:
            off[str(lab)] = sorted(set(names))
        else:
            off.pop(str(lab), None)
        if off:
            self._lab["instruments_off"] = off
        else:
            self._lab.pop("instruments_off", None)
        self._save()

    def hands_on(self) -> Optional[Dict[str, Any]]:
        """A person's hands-on-the-sample lock (who, since), kept across restarts: the setup's, not a person's."""
        h = self._lab.get("hands_on")
        return h if isinstance(h, dict) and h.get("since") else None

    def set_hands_on(self, hands: Optional[Dict[str, Any]]) -> None:
        if hands:
            self._lab["hands_on"] = {"by": str(hands.get("by") or ""), "since": str(hands.get("since") or "")}
        else:
            self._lab.pop("hands_on", None)
        self._save()

    @property
    def lab_scripts(self) -> Optional[str]:
        """The folder of the scripts the lab runs its instruments with, which Kelvin reads to set them up (scripts.py):
        "" once a person said there are none, None while nobody has been asked."""
        value = self._lab.get("lab_scripts")
        return None if value is None else str(value)

    def set_lab_scripts(self, folder: str) -> None:
        self._lab["lab_scripts"] = str(folder or "")
        self._save()

    @property
    def lab_scripts_python(self) -> str:
        """The Python a person set for running the lab's scripts (run_script); "" lets Kelvin find one that has what
        each script imports (scriptrun.python_for)."""
        return str(self._lab.get("lab_scripts_python") or "")

    def set_lab_scripts_python(self, python: str) -> None:
        self._lab["lab_scripts_python"] = str(python or "").strip()
        self._save()

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
                for name in SEALED:
                    if mine.get(name) and not _sealed(mine[name]):
                        mine[name] = self.cipher.encrypt(str(mine[name]))
                for s in mine.get("services") or []:
                    if isinstance(s, dict) and s.get("api_key") and not _sealed(s["api_key"]):
                        s["api_key"] = self.cipher.encrypt(str(s["api_key"]))
            write(self.user_path, mine)
