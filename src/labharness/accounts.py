"""People using Kelvin on this computer: each signs in and has their own settings, chats, data folder and record.

Everyone is equal: there is no administrator. Anyone at the computer can create an account; each account protects
itself with an optional password. An optional lab-owner PIN (OwnerPin) guards what widens Kelvin's reach over the
instruments: limits and the lab config, auto mode, unlocking Hands on the sample. On top of it the owner may give
people roles (LabRoles: owner, operator, trainee); until then, everyone is equal. What is shared is the physical setup: the instruments (lab.yaml, cards, instrument
folders) and the instrument log, which records every command with the name of the person who sent it.

    ~/Kelvin/
      settings.json              this computer: which lab, instrument set-up switches, the portal's access key
      accounts.json              the accounts: name, folder, password hash
      owner.json, roles.json     the lab owner's PIN (a salted hash), and the lab's roles, if the owner gave any
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


class OwnerPin:
    """The lab owner's PIN, optional, kept on this computer (~/Kelvin/owner.json, a salted hash). Once it is set, a
    person gives it to widen what Kelvin may do with the instruments: change limits or the lab config (its interlocks
    and watches), turn on auto mode, unlock Hands on the sample. Narrowing never needs it (lowering a limit through
    the config still does, as the config can't tell). Not a sign-in: everyone is otherwise equal at the computer, unless the
    owner gives people roles (LabRoles), which build on this PIN."""
    TRIES = 5            # wrong PINs before it locks for a while
    LOCKED_S = 300.0

    def __init__(self, home: Any):
        self.path = Path(home) / "owner.json"
        self._wrong: List[float] = []
        self._lock = threading.Lock()

    def _data(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and data.get("hash") and data.get("salt") else {}
        except (OSError, ValueError):
            return {}

    @property
    def is_set(self) -> bool:
        return bool(self._data())

    def check(self, pin: Any) -> Optional[str]:
        """None when no PIN is set or this is it; otherwise why not, in words for the person."""
        data = self._data()
        if not data:
            return None
        with self._lock:
            now = time.monotonic()
            self._wrong = [t for t in self._wrong if now - t < self.LOCKED_S]
            if len(self._wrong) >= self.TRIES:
                return "too many wrong PINs: try again in %d minutes" % max(1, round((self.LOCKED_S - (now - self._wrong[0])) / 60))
            if not pin:
                return "this needs the lab owner's PIN"
            given = _hash(str(pin), bytes.fromhex(data["salt"]), int(data.get("iterations") or ITERATIONS))
            if hmac.compare_digest(given, data["hash"]):
                self._wrong.clear()
                return None
            self._wrong.append(now)
            return "that is not the lab owner's PIN"

    def set(self, new: Any, old: Any = None) -> None:
        """Set, change or (new empty) remove the PIN; once one is set, only with it. ValueError says why not."""
        why = self.check(old)
        if why:
            raise ValueError(why)
        new = str(new or "").strip()
        if not new:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return
        if not re.fullmatch(r"\d{4,12}", new):
            raise ValueError("a PIN is 4 to 12 digits")
        salt = secrets.token_bytes(16)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"salt": salt.hex(), "hash": _hash(new, salt), "iterations": ITERATIONS,
                                   "set": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
        os.replace(tmp, self.path)


ROLES = ("owner", "operator", "trainee")
NEEDS_YES = ("a trainee's yes needs an operator or the lab owner: their name and password on this computer, or the "
             "owner's PIN")


class RoleRefused(PermissionError):
    """Not for this person's role in the lab, whatever PIN they give (unlike OwnerPin's refusals)."""


def trainee_refusal(what: str) -> str:
    """Why a trainee can't do what widens Kelvin's reach, in words for the person."""
    return ("%s is not for a trainee in this lab: an operator or the lab owner signs in to do it (Settings, "
            "Instruments, Lab roles)" % what)


class LabRoles:
    """The lab's roles, on top of the owner's PIN (OwnerPin): owner, operator and trainee. Optional, and off until the
    owner gives people roles; then they are kept in ~/Kelvin/roles.json. Off, Kelvin is as before: everyone equal,
    the PIN guarding what widens Kelvin's reach. On:
    - an operator or an owner works as everyone did before (the PIN still guards limits, the lab config, auto mode
      and unlocking Hands on the sample);
    - a trainee's yes on a request (a drive away from safe, a run, a queue, anything Kelvin asks a person) needs an
      operator or the owner to confirm it at the card: their account's password, or the owner's PIN;
    - limits, the lab config, the cards' limits and reviews, rolling the config back and auto mode are not for a
      trainee at all, PIN or not: an operator or the owner signs in to do them;
    - only an owner, signed in and with the PIN, changes the roles and where the owner hears of safety changes.
    Whoever the owner gave no role (an account made later) is a trainee, and so is a person Kelvin can't name (its
    approval page as an MCP server, where nobody signs in). The PIN must be set for roles, and stays while they are
    on. Not a wall: whoever can edit this computer's files can delete roles.json, and the lab's record shows that."""
    TRIES = 5            # wrong passwords or PINs for a trainee's yes before it locks for a while
    LOCKED_S = 300.0

    def __init__(self, home: Any):
        self.path = Path(home) / "roles.json"
        self._wrong: List[float] = []
        self._lock = threading.Lock()

    def _data(self) -> Dict[str, Any]:
        """The file's contents; {"unreadable": True} for a file that is there but can't be read (then everyone is a
        trainee until the owner gives the roles again: a broken file must not lift them)."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError):
            return {"unreadable": True}
        try:
            data = json.loads(text)
        except ValueError:
            return {"unreadable": True}
        return data if isinstance(data, dict) else {"unreadable": True}

    def _write(self, data: Dict[str, Any]) -> None:
        data.pop("unreadable", None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def all(self) -> Dict[str, str]:
        """Each account the owner gave a role, with it: {} while the lab has no roles."""
        roles = self._data().get("roles")
        return {str(k): str(v) for k, v in roles.items() if v in ROLES} if isinstance(roles, dict) else {}

    @property
    def on(self) -> bool:
        return bool(self.all()) or bool(self._data().get("unreadable"))

    def role(self, name: Optional[str]) -> Optional[str]:
        """None while the lab has no roles; else this person's: the one the owner gave, or trainee (nobody named,
        or an account the owner gave none)."""
        if not self.on:
            return None
        mine = str(name or "").strip().lower()
        return next((r for n, r in self.all().items() if mine and n.lower() == mine), "trainee")

    def approvers(self) -> List[str]:
        """Who may confirm a trainee's yes: the operators and owners, by name."""
        return sorted(n for n, r in self.all().items() if r in ("owner", "operator"))

    def _may_change(self, owner: Optional[OwnerPin], pin: Any, signed_in: Optional[str]) -> None:
        """Roles and the owner's notices change with the owner's PIN, which must be set; once there are roles, only
        by an owner signed in (unless no account has the owner role any more: then the PIN alone)."""
        if owner is None or not owner.is_set:
            raise ValueError("Set the lab owner's PIN first: roles change with it.")
        why = owner.check(pin)
        if why:
            raise PermissionError(why)
        owners = [n for n, r in self.all().items() if r == "owner"]
        if owners and self.role(signed_in) != "owner":
            raise PermissionError("only the lab's owner changes this: %s signs in to do it" % " or ".join(owners))

    def set(self, roles: Dict[str, Any], owner: Optional[OwnerPin], pin: Any, signed_in: Optional[str],
            accounts: Optional["Accounts"] = None) -> Dict[str, str]:
        """Give people their roles ({} or all empty: no roles, everyone equal again). Each name is an account on this
        computer, and at least one is the owner. ValueError and PermissionError say why not."""
        out: Dict[str, str] = {}
        for name, role in (roles or {}).items():
            if role in (None, "", "none"):
                continue
            if role not in ROLES:
                raise ValueError("a role is owner, operator or trainee, not %r" % role)
            if accounts is not None:
                try:
                    name = accounts.find(name)["name"]
                except KeyError:
                    raise ValueError("there is no account %r on this computer" % name) from None
            out[str(name)] = str(role)
        if out and "owner" not in out.values():
            raise ValueError("Give at least one person the owner's role: the owner changes the roles.")
        self._may_change(owner, pin, signed_in)
        with self._lock:
            data = self._data()
            data.update(roles=out, changed=time.strftime("%Y-%m-%dT%H:%M:%S"), by=signed_in or None)
            self._write(data)
        return out

    # ------------------------------------------------------------------ a trainee's yes, confirmed

    def vouch(self, accounts: Optional["Accounts"], owner: Optional[OwnerPin], name: Any = None,
              password: Any = None, pin: Any = None) -> Dict[str, Any]:
        """Who confirms a trainee's yes, checked: {"account", "role"} for an operator or the owner with their
        account's password here, {"pin": True} for the owner's PIN. PermissionError says why not. Wrong tries lock it
        for a while, as the PIN does."""
        if pin:
            if owner is None or not owner.is_set:   # no PIN set: check() would take anything
                raise PermissionError("there is no lab owner's PIN on this computer; " + NEEDS_YES)
            why = owner.check(pin)
            if why:
                raise PermissionError(why)
            return {"pin": True}
        name = str(name or "").strip()
        if not name:
            raise PermissionError(NEEDS_YES)
        with self._lock:
            now = time.monotonic()
            self._wrong = [t for t in self._wrong if now - t < self.LOCKED_S]
            if len(self._wrong) >= self.TRIES:
                raise PermissionError("too many wrong passwords: try again in %d minutes" % max(
                    1, round((self.LOCKED_S - (now - self._wrong[0])) / 60)))
        try:
            account = accounts.find(name) if accounts is not None else None
        except KeyError:
            account = None
        role = self.role(account["name"]) if account else None
        if account is None or role not in ("owner", "operator"):
            self._wrong_try()
            raise PermissionError("%s is not an operator or the owner of this lab" % name)
        if not account.get("hash"):   # anyone could give the name: an account without a password proves nothing
            raise PermissionError("%s has no password on this computer, so Kelvin can't tell it is them: they set "
                                  "one in Settings, Account (or the owner gives the PIN)" % account["name"])
        if not accounts.matches(account["name"], str(password or "")):
            self._wrong_try()
            raise PermissionError("that is not %s's password" % account["name"])
        with self._lock:
            self._wrong.clear()
        return {"account": account["name"], "role": role}

    def _wrong_try(self) -> None:
        with self._lock:
            self._wrong.append(time.monotonic())

    # ------------------------------------------------------------------ where the owner hears of safety changes

    def notify_target(self) -> Dict[str, Any]:
        """Where the owner is told of safety changes, whoever is signed in: {"webhook": url, "email": {...}} (the
        email as notify.email_from takes it), or {} while the owner set none."""
        target = self._data().get("notify")
        return target if isinstance(target, dict) else {}

    def set_notify(self, target: Optional[Dict[str, Any]], owner: Optional[OwnerPin], pin: Any,
                   signed_in: Optional[str]) -> None:
        """Keep (or, with None, forget) where the owner hears of safety changes: a copy of the owner's own
        notification settings, readable on this computer, so they reach the owner while someone else is signed in."""
        self._may_change(owner, pin, signed_in)
        with self._lock:
            data = self._data()
            if target:
                data["notify"] = dict(target, set=time.strftime("%Y-%m-%dT%H:%M:%S"), by=signed_in or None)
            else:
                data.pop("notify", None)
            self._write(data)
