"""People using Kelvin on this computer: each signs in and has their own settings, chats, data folder and record.

Everyone is equal: there is no administrator. Anyone at the computer can create an account; each account protects
itself with an optional password. What is shared is the physical setup: the instruments (lab.yaml, cards, instrument
folders) and the instrument log, which records every command with the name of the person who sent it.

    ~/Kelvin/
      settings.json              this computer: which lab, instrument set-up switches, the portal's access key
      accounts.json              the accounts: name, folder, password hash
      instrument-log.jsonl       every event from everyone, each with its user
      lab.yaml, cards/, instruments/
      users/<name>/
        settings.json            their API keys (encrypted when they have a password), models, preferences
        chatgpt.json             their ChatGPT sign-in
        chats/                   their chats, projects and memory
        data/                    their runs, reports and record (events.jsonl), unless they chose another folder

The first account adopts what Kelvin kept before accounts existed (chats and runs in ~/Kelvin/virtual-lab)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ITERATIONS = 200_000
NAME = re.compile(r"^[^\x00-\x1f/\\:*?\"<>|]{1,40}$")


def _hash(password: str, salt: bytes, iterations: int = ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations).hex()


def has_legacy(home: Any) -> bool:
    """Did someone use Kelvin here before accounts? (chats, runs or a record in ~/Kelvin/virtual-lab)"""
    old = Path(home) / "virtual-lab"
    return (any((old / "chats").glob("*.json")) or (old / "runs").is_dir() or (old / "events.jsonl").exists()
            or (old / "reports").is_dir())


def folder_name(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name.strip()).strip("._")[:40] or "user"


class Cipher:
    """Encrypts a person's API keys with a key made from their password (AES via Fernet), so a labmate at the same
    computer cannot read or use them. Needs the cryptography package (the desktop app has it)."""

    def __init__(self, password: str, salt: bytes):
        from cryptography.fernet import Fernet   # type: ignore
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS, 32)
        self._f = Fernet(base64.urlsafe_b64encode(key))

    def encrypt(self, text: str) -> str:
        return "enc:" + self._f.encrypt(text.encode("utf-8")).decode("ascii")

    def decrypt(self, text: str) -> Optional[str]:
        if not str(text).startswith("enc:"):
            return text
        try:
            return self._f.decrypt(text[4:].encode("ascii")).decode("utf-8")
        except Exception:
            return None


def cipher_for(password: str, account: Dict[str, Any]) -> Optional[Cipher]:
    if not password or not account.get("key_salt"):
        return None
    try:
        return Cipher(password, bytes.fromhex(account["key_salt"]))
    except ImportError:   # no cryptography: keys stay readable, as before accounts
        return None


class Accounts:
    def __init__(self, home: Any):
        self.home = Path(home)
        self.path = self.home / "accounts.json"
        self._lock = threading.Lock()

    def _read(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [a for a in data.get("accounts") or [] if isinstance(a, dict) and a.get("name")]
        except (OSError, ValueError):
            return []

    def _write(self, accounts: List[Dict[str, Any]]) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"accounts": accounts}, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def list(self) -> List[Dict[str, Any]]:
        """For the sign-in screen: names, and whether each has a password; never the hashes."""
        return [{"name": a["name"], "password": bool(a.get("hash")), "created": a.get("created")} for a in self._read()]

    def find(self, name: str) -> Dict[str, Any]:
        for a in self._read():
            if a["name"].lower() == str(name or "").strip().lower():
                return a
        raise KeyError("no account %r" % name)

    def create(self, name: str, password: str = "", adopt_legacy: bool = False) -> Dict[str, Any]:
        name = " ".join(str(name or "").split())
        if not NAME.match(name):
            raise ValueError("A name of 1-40 characters, without / \\ : * ? \" < > |")
        with self._lock:
            accounts = self._read()
            if any(a["name"].lower() == name.lower() for a in accounts):
                raise ValueError("There is already an account called %s." % name)
            taken = {a["folder"] for a in accounts}
            folder, n = folder_name(name), 1
            while folder in taken or (self.home / "users" / folder).exists():   # a forgotten account's folder stays its own
                n += 1
                folder = "%s_%d" % (folder_name(name), n)
            account: Dict[str, Any] = {"name": name, "folder": folder, "created": time.time(),
                                       "key_salt": secrets.token_hex(16)}
            if adopt_legacy and not accounts:   # what Kelvin kept before accounts: chats and runs in ~/Kelvin/virtual-lab
                account["chats"] = "virtual-lab/chats"
                account["data"] = "virtual-lab"
            self._set_password(account, password)
            accounts.append(account)
            self._write(accounts)
        self.dir(account).mkdir(parents=True, exist_ok=True)
        return account

    @staticmethod
    def _set_password(account: Dict[str, Any], password: str) -> None:
        if password:
            if len(password) < 4:
                raise ValueError("A password needs at least 4 characters (or none at all).")
            salt = secrets.token_bytes(16)
            account.update(salt=salt.hex(), hash=_hash(password, salt), iterations=ITERATIONS)
        else:
            for k in ("salt", "hash", "iterations"):
                account.pop(k, None)

    def verify(self, name: str, password: str = "") -> Dict[str, Any]:
        """The account, if this is its password (any password-less account signs in with none)."""
        try:
            account = self.find(name)
        except KeyError:
            raise ValueError("Wrong name or password.")
        if account.get("hash"):
            got = _hash(password or "", bytes.fromhex(account["salt"]), int(account.get("iterations") or ITERATIONS))
            if not hmac.compare_digest(got, account["hash"]):
                raise ValueError("Wrong name or password.")
        return account

    def change_password(self, name: str, old: str, new: str) -> Dict[str, Any]:
        self.verify(name, old)
        with self._lock:
            accounts = self._read()
            account = next(a for a in accounts if a["name"].lower() == name.lower())
            self._set_password(account, new)
            self._write(accounts)
        return account

    def remove(self, name: str, password: str = "") -> None:
        """Forget the account; its folder (chats, data, settings) stays on disk."""
        self.verify(name, password)
        with self._lock:
            self._write([a for a in self._read() if a["name"].lower() != name.lower()])

    # ------------------------------------------------------------------ where each person's things are

    def dir(self, account: Dict[str, Any]) -> Path:
        return self.home / "users" / account["folder"]

    def settings_path(self, account: Dict[str, Any]) -> Path:
        return self.dir(account) / "settings.json"

    def chats_dir(self, account: Dict[str, Any]) -> Path:
        return self.home / account["chats"] if account.get("chats") else self.dir(account) / "chats"

    def data_dir(self, account: Dict[str, Any]) -> Path:
        """Their default data folder (they can choose another in Settings)."""
        return self.home / account["data"] if account.get("data") else self.dir(account) / "data"
