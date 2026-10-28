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

    def _data(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _read(self) -> List[Dict[str, Any]]:
        return [a for a in self._data().get("accounts") or [] if isinstance(a, dict) and a.get("name")]

    def _write(self, accounts: List[Dict[str, Any]], **marks: Any) -> None:
        """The accounts, keeping marks such as legacy_taken (what Kelvin kept before accounts is someone's)."""
        self.home.mkdir(parents=True, exist_ok=True)
        data = self._data()
        data.update(marks, accounts=accounts)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def list(self) -> List[Dict[str, Any]]:
        """For the sign-in screen: names (and the email and name of a central account), and whether each has a
        password; never the hashes."""
        return [{"name": a["name"], "password": bool(a.get("hash")), "created": a.get("created"),
                 "email": a.get("email") or None, "display": a.get("display") or None} for a in self._read()]

    def legacy_taken(self) -> bool:
        """Has an account here taken over what Kelvin kept before accounts (even one removed since)?"""
        return bool(self._data().get("legacy_taken")) or any(a.get("data") == "virtual-lab" for a in self._read())

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
            marks = {}
            # what Kelvin kept before accounts (chats and runs in ~/Kelvin/virtual-lab) goes to the first account,
            # once: never to someone who comes after that person was removed
            if adopt_legacy and not accounts and not self._data().get("legacy_taken"):
                account["chats"] = "virtual-lab/chats"
                account["data"] = "virtual-lab"
                marks["legacy_taken"] = True
            self._set_password(account, password)
            accounts.append(account)
            self._write(accounts, **marks)
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

    def remove(self, name: str, password: str = "") -> Dict[str, Any]:
        """Forget the account, if this is its password; its folder stays on disk."""
        self.verify(name, password)
        return self.forget(name)

    def forget(self, name: str) -> Dict[str, Any]:
        """Take the account off this computer's list (whoever may do so has been checked); returns it. Its folder
        stays: a new account never gets it, nor what Kelvin kept before accounts if this one had taken that."""
        with self._lock:
            accounts = self._read()
            gone = next(a for a in accounts if a["name"].lower() == name.lower())
            marks = {"legacy_taken": True} if gone.get("data") == "virtual-lab" else {}
            self._write([a for a in accounts if a is not gone], **marks)
        return gone

    # ------------------------------------------------------------------ central Kelvin accounts (central.py)

    def find_email(self, email: Any) -> Optional[Dict[str, Any]]:
        """The account on this computer that this central Kelvin account signs in to, if there is one."""
        e = str(email or "").strip().lower()
        return next((a for a in self._read() if e and a.get("email") == e), None)

    def unlinked(self) -> List[str]:
        """Accounts from before central accounts: someone signing in centrally for the first time may take one over."""
        return [a["name"] for a in self._read() if not a.get("email")]

    def matches(self, name: str, password: str) -> bool:
        """Is this the password this computer knows for the account? (Never true for one without a password.)"""
        try:
            return bool(self.verify(name, password).get("hash"))
        except (ValueError, KeyError):
            return False

    def link(self, name: str, email: str, display: str = "") -> Dict[str, Any]:
        """Tie an account on this computer to a central Kelvin account: its chats, data and keys stay."""
        with self._lock:
            accounts = self._read()
            account = next(a for a in accounts if a["name"].lower() == name.lower())
            account["email"] = str(email).strip().lower()
            if display:
                account["display"] = str(display)[:80]
            self._write(accounts)
        return account

    def create_central(self, display: str, email: str, password: str, adopt_legacy: bool = False) -> Dict[str, Any]:
        """This computer's account for a central Kelvin account signing in here for the first time."""
        email = str(email).strip().lower()
        taken = {a["name"].lower() for a in self._read()}
        name = " ".join(str(display or "").split())[:40]
        if not name or name.lower() in taken or not NAME.match(name):
            name = email[:40] if email[:40].lower() not in taken else email[:34] + "-" + secrets.token_hex(2)
        account = self.create(name, password, adopt_legacy)
        return self.link(account["name"], email, display)

    def set_verifier(self, name: str, password: str) -> Dict[str, Any]:
        """Remember this password for the account (after the hub accepted it), so it also works here offline."""
        with self._lock:
            accounts = self._read()
            account = next(a for a in accounts if a["name"].lower() == name.lower())
            self._set_password(account, password)
            self._write(accounts)
        return account

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
