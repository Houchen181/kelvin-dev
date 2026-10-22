"""The hub's store: one SQLite file in WAL mode (plenty for a few thousand people), with run files on disk.

    <hub home>/
      hub.sqlite3                       accounts, labs, connected Kelvins, and what they share
      hub.json                          base URL, mail, consent version, the operator key's hash (no secrets)
      operator-key.txt                  the operator key, written once: move it somewhere safe and delete the file
      files/<user_id>/<run_id>/...      run files
      backups/hub-YYYY-MM-DD.sqlite3    a copy of the database, once a day
      outbox/                           mail the hub would have sent, while no SMTP server is set
      tmp/                              uploads and exports on their way
      hub.log

Passwords are PBKDF2-SHA256 hashes, as for Kelvin's local accounts. Session and app tokens, and the one-use codes
(connecting a Kelvin, confirming an email, resetting a password), are stored only as their SHA-256.

Everyone is equal: there is no role column. The Kelvin team runs the hub with the operator key, which is no account.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

from ..accounts import _hash

log = logging.getLogger("kelvin.hub")

SCHEMA_VERSION = 3               # 2: account ids are never reused (AUTOINCREMENT); 3: chats keep their size
ITERATIONS = 600_000             # PBKDF2-SHA256, as OWASP suggests; older hashes are redone at the next sign-in
UPLOADS_AT_ONCE = 2              # per person
LOCK_WAIT = 10.0                 # seconds a request waits for the same person's sync or upload before a 429
MAX_PREVIOUS = 5                 # earlier installation ids a Kelvin may fold into its new one
EARLIEST = 946684800.0           # 2000-01-01: an earlier or non-finite time from a Kelvin is taken as "now"
CONSENT_VERSION = "2026-10-20"   # bump when the terms or privacy notice change; people then agree again
SESSION_TTL = 30 * 86400
CODE_TTL = {"connect": 600, "confirm": 7 * 86400, "reset": 3600}
MIN_PASSWORD = 8
MAX_NAME = 80
MAX_TITLE = 200
MAX_MESSAGES = 5 * 1024 * 1024    # bytes of JSON per chat
MAX_META = 256 * 1024             # bytes of JSON per run
MAX_BATCH = 500                   # items of each kind in one sync
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")
ORCID = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")
LOCAL_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
INSTALLATION_ID = re.compile(r"^[A-Za-z0-9:_.-]{8,80}$")
FILE_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_. -]{0,119}$")
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)}
JOIN_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L
DEFAULTS: Dict[str, Any] = {
    "base_url": "http://127.0.0.1:8780",
    "consent_version": CONSENT_VERSION,
    "require_confirmed_email": False,   # True once mail works: unconfirmed people can sign in but not connect a Kelvin
    "max_file_mb": 200,
    "quota_gb_per_user": 5,
    "backups_kept": 30,
    "client_ip_header": "",            # behind Cloudflare Tunnel: "CF-Connecting-IP"
    "mail": {"smtp_host": "", "smtp_port": 587, "starttls": True, "username": "", "from": ""},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,   -- never reused: the activity record keeps bare ids after a deletion
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name TEXT NOT NULL,
  orcid TEXT,
  password_hash TEXT,
  salt TEXT,
  iterations INTEGER,
  external_identity TEXT UNIQUE,          -- 'orcid:<id>' or 'google:<sub>', for signing in without a password later
  email_confirmed_at REAL,
  disabled_at REAL,                       -- set by the operator; a disabled account cannot sign in or sync
  consent_version TEXT NOT NULL,
  consent_at REAL NOT NULL,
  created_at REAL NOT NULL,
  last_seen REAL
);
CREATE TABLE IF NOT EXISTS labs (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  institution TEXT NOT NULL DEFAULT '',
  join_code TEXT NOT NULL UNIQUE,         -- any member can pass it on; there is no lab owner
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lab_members (
  lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  joined_at REAL NOT NULL,
  PRIMARY KEY (lab_id, user_id)
);
CREATE TABLE IF NOT EXISTS installations (   -- a Kelvin on some computer, by the id it gives itself
  id TEXT PRIMARY KEY,
  lab_id INTEGER REFERENCES labs(id) ON DELETE SET NULL,
  name TEXT NOT NULL DEFAULT '',
  platform TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  last_seen REAL
);
CREATE TABLE IF NOT EXISTS tokens (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('web', 'app')),   -- a browser session, or a connected Kelvin
  token_hash TEXT NOT NULL UNIQUE,
  csrf TEXT,
  installation_id TEXT REFERENCES installations(id) ON DELETE CASCADE,
  lab_id INTEGER REFERENCES labs(id) ON DELETE SET NULL,   -- the lab a connected Kelvin shares into
  created_at REAL NOT NULL,
  last_used REAL,
  expires_at REAL
);
CREATE TABLE IF NOT EXISTS codes (
  code_hash TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('connect', 'confirm', 'reset')),
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  data TEXT NOT NULL DEFAULT '{}',
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  lab_id INTEGER REFERENCES labs(id) ON DELETE SET NULL,
  installation_id TEXT NOT NULL,
  local_id TEXT NOT NULL,
  name TEXT NOT NULL,
  shared INTEGER NOT NULL DEFAULT 1,      -- 0: unpublished or confidential; the hub keeps only that, not the name
  updated_at REAL NOT NULL,
  UNIQUE (user_id, installation_id, local_id)
);
CREATE TABLE IF NOT EXISTS chats (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
  lab_id INTEGER REFERENCES labs(id) ON DELETE SET NULL,
  installation_id TEXT NOT NULL,
  local_id TEXT NOT NULL,
  title TEXT NOT NULL,
  messages TEXT NOT NULL,                 -- the whole conversation, as JSON
  updated_at REAL NOT NULL,
  bytes INTEGER NOT NULL DEFAULT 0,       -- the size of messages, so a person's share is summed without reading them
  UNIQUE (user_id, installation_id, local_id)
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
  lab_id INTEGER REFERENCES labs(id) ON DELETE SET NULL,
  installation_id TEXT NOT NULL,
  local_id TEXT NOT NULL,
  device TEXT NOT NULL DEFAULT '',
  meta TEXT NOT NULL DEFAULT '{}',
  files_path TEXT NOT NULL DEFAULT '',    -- relative to the hub home: files/<user_id>/<run_id>
  bytes INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE (user_id, installation_id, local_id)
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY,
  at REAL NOT NULL,
  user_id INTEGER,                        -- stays a bare number after the account is deleted
  actor TEXT NOT NULL,                    -- 'user', 'app', 'operator' or 'hub'
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}',
  ip TEXT
);
CREATE INDEX IF NOT EXISTS tokens_user ON tokens (user_id, kind);
CREATE INDEX IF NOT EXISTS members_user ON lab_members (user_id);
CREATE INDEX IF NOT EXISTS projects_lab ON projects (lab_id, updated_at);
CREATE INDEX IF NOT EXISTS chats_lab ON chats (lab_id, updated_at);
CREATE INDEX IF NOT EXISTS chats_project ON chats (project_id);
CREATE INDEX IF NOT EXISTS runs_lab ON runs (lab_id, updated_at);
CREATE INDEX IF NOT EXISTS runs_project ON runs (project_id);
CREATE INDEX IF NOT EXISTS chats_user_bytes ON chats (user_id, bytes);
CREATE INDEX IF NOT EXISTS runs_user_bytes ON runs (user_id, bytes);
CREATE INDEX IF NOT EXISTS audit_user ON audit (user_id, at);
"""

# who may see an item (alias x): its owner, or a member of the lab it was shared in while the owner is one too
VISIBLE = ("(x.user_id = :me OR (x.lab_id IS NOT NULL"
           " AND EXISTS (SELECT 1 FROM lab_members a WHERE a.lab_id = x.lab_id AND a.user_id = :me)"
           " AND EXISTS (SELECT 1 FROM lab_members b WHERE b.lab_id = x.lab_id AND b.user_id = x.user_id)))")


class HubError(ValueError):
    """Something the person (or the Kelvin) can fix; the message says what."""
    status = 400


class TooMuch(HubError):
    """Larger than the hub takes, or over the person's share of its disk."""
    status = 413


class Busy(HubError):
    """Too much at once: try again in a moment."""
    status = 429


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def challenge_of(verifier: str) -> str:
    """PKCE S256."""
    return b64url(hashlib.sha256(verifier.encode("ascii", "replace")).digest())


def _text(value: Any, missing: Optional[str], limit: int) -> str:
    text = " ".join(str(value or "").split())[:limit]
    if not text and missing:
        raise HubError(missing)
    return text


def _email(value: Any) -> str:
    email = str(value or "").strip().lower()
    if len(email) > 254 or not EMAIL.match(email):
        raise HubError("That doesn't look like an email address.")
    return email


def orcid_ok(orcid: str) -> bool:
    """The ORCID check digit (ISO 7064 11,2)."""
    digits = orcid.replace("-", "")
    total = 0
    for d in digits[:-1]:
        total = (total + int(d)) * 2
    check = (12 - total % 11) % 11
    return digits[-1] == ("X" if check == 10 else str(check))


def _orcid(value: Any) -> Optional[str]:
    orcid = str(value or "").strip().upper()
    for prefix in ("HTTPS://ORCID.ORG/", "HTTP://ORCID.ORG/", "ORCID.ORG/"):
        if orcid.startswith(prefix):
            orcid = orcid[len(prefix):]
    if not orcid:
        return None
    if not ORCID.match(orcid) or not orcid_ok(orcid):
        raise HubError("An ORCID iD looks like 0000-0002-1825-0097.")
    return orcid


def _check_password(password: Any, email: str = "") -> str:
    password = str(password or "")
    if len(password) < MIN_PASSWORD:
        raise HubError("A password needs at least %d characters." % MIN_PASSWORD)
    if len(password) > 1024:
        raise HubError("That password is too long.")
    if email and password.lower() == email.lower():
        raise HubError("Choose a password that isn't your email address.")
    return password


def _local_id(value: Any, what: str) -> str:
    text = str(value or "")
    if not LOCAL_ID.match(text):
        raise HubError("%s needs an id of 1-64 letters, digits, '.', '_' or '-'." % what)
    return text


def file_parts(name: Any) -> List[str]:
    """A run file's path inside its run folder, checked part by part (no '..', no absolute paths, no device names)."""
    parts = [p for p in str(name or "").replace("\\", "/").split("/")]
    if not 1 <= len(parts) <= 4:
        raise HubError("A run file needs a name, at most 4 folders deep.")
    for p in parts:
        if (not FILE_PART.match(p) or p.endswith((".", " ")) or ".." in p
                or p.split(".")[0].strip().upper() in WINDOWS_RESERVED):   # "NUL .txt" is NUL on Windows
            raise HubError("%r can't be a file name on the hub." % p)
    return parts


def _timestamp(value: Any, now: float) -> float:
    """A time a Kelvin sent: a finite number from 2000 to a day from now, else now."""
    try:
        t = float(value)
    except (TypeError, ValueError, OverflowError):
        return now
    return t if EARLIEST <= t <= now + 86400 else now   # also false for nan and inf


def installation(info: Dict[str, Any]) -> Dict[str, str]:
    """A Kelvin as it introduces itself on /connect: its own stable id, and a name, platform and version to show."""
    ident = str(info.get("installation") or "")
    if not INSTALLATION_ID.match(ident):
        raise HubError("Kelvin didn't say which installation it is; start again from Kelvin's Settings.")
    return {"id": ident, "name": _text(info.get("name"), None, MAX_NAME) or "Kelvin",
            "platform": _text(info.get("platform"), None, 40), "version": _text(info.get("version"), None, 40)}


def loopback(redirect_uri: Any) -> str:
    """Kelvin's own callback on its computer, http://127.0.0.1:<port>/..., as for Sign in with ChatGPT."""
    uri = str(redirect_uri or "")
    problem = len(uri) > 300 or any(ord(c) <= 32 or ord(c) == 127 for c in uri)   # no spaces or control characters
    if not problem:
        try:
            parts = urlsplit(uri)
            problem = (parts.scheme != "http" or parts.hostname != "127.0.0.1" or not parts.port
                       or bool(parts.username or parts.password or parts.fragment))
        except ValueError:   # e.g. "http://[" or a port out of range
            problem = True
    if problem:
        raise HubError("Kelvin's callback must be http://127.0.0.1:<port>/...")
    return uri


def restricted(rules: Any) -> bool:
    """Kelvin's "Who may see this project" limits models or literature search: the work is confidential."""
    return isinstance(rules, dict) and (rules.get("providers") is not None or rules.get("literature") is False)


def project_shared(project: Dict[str, Any]) -> bool:
    """Normal projects are shared; unpublished, confidential and model-limited ones never are."""
    return not (project.get("shared") is False or project.get("unpublished") or project.get("confidential")
                or restricted(project.get("rules")))


def chat_shared(chat: Dict[str, Any]) -> bool:
    """Incognito chats, chats marked unpublished or confidential, and chats that ran under model limits never are."""
    return not (chat.get("incognito") or chat.get("shared") is False or chat.get("unpublished")
                or chat.get("confidential") or chat.get("unshared") or restricted(chat.get("rules")))


def run_shared(run: Dict[str, Any]) -> bool:
    """Runs started by an incognito or unshared chat, or marked unpublished or confidential, never are."""
    meta = run.get("meta") if isinstance(run.get("meta"), dict) else {}
    return not any(d.get(k) for d in (run, meta) for k in ("incognito", "unpublished", "confidential", "unshared"))


def previous_ids(value: Any, new: str) -> List[str]:
    """The earlier installation ids a Kelvin names on /connect (comma-separated), plus the new id's computer part
    (what Kelvin used before ids were per person), without the new id itself."""
    ids = [i.strip() for i in str(value or "").split(",") if i.strip()]
    if len(ids) > MAX_PREVIOUS or not all(INSTALLATION_ID.match(i) for i in ids):
        raise HubError("Kelvin named too many, or odd, earlier installations; start again from Kelvin's Settings.")
    computer = new.rsplit(":", 1)[0] if re.search(r":[0-9a-f]{12}$", new) else ""
    if computer and INSTALLATION_ID.match(computer):
        ids.append(computer)
    return sorted(set(ids) - {new})


def join_code() -> str:
    raw = "".join(secrets.choice(JOIN_ALPHABET) for _ in range(12))
    return "-".join(raw[i:i + 4] for i in range(0, 12, 4))


def _normalize_code(code: Any) -> str:
    raw = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def _public(row: Any) -> Dict[str, Any]:
    """A person as the hub shows them: never the password hash or salt."""
    keys = ("id", "email", "name", "orcid", "external_identity", "email_confirmed_at", "disabled_at",
            "consent_version", "consent_at", "created_at", "last_seen")
    return {k: row[k] for k in keys}


class Hub:
    def __init__(self, home: Any):
        self.home = Path(home)
        for sub in ("files", "backups", "outbox", "tmp"):
            (self.home / sub).mkdir(parents=True, exist_ok=True)
        self.db_path = self.home / "hub.sqlite3"
        self.config_path = self.home / "hub.json"
        self._config_mtime = 0.0
        self.config = self._load_config()
        self._uploads = threading.Lock()   # guards _user_locks
        self._user_locks: Dict[int, threading.Lock] = {}
        self.reserved: Dict[int, Dict[str, int]] = {}   # user id -> {upload ticket: bytes held against their share}
        with contextlib.closing(sqlite3.connect(str(self.db_path))) as con:
            con.execute("PRAGMA journal_mode = WAL")
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version == 1:
                self._migrate_1_to_2(con)
            if version in (1, 2):
                self._migrate_2_to_3(con)
            con.executescript(SCHEMA)
            con.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
            con.commit()

    @staticmethod
    def _migrate_1_to_2(con: sqlite3.Connection) -> None:
        """Make account ids AUTOINCREMENT, so a deleted account's id (kept in the activity record) is never reused.
        The next id is above every id ever used, including deleted ones."""
        con.execute("PRAGMA foreign_keys = OFF")
        old = con.execute("SELECT sql FROM sqlite_master WHERE name = 'users'").fetchone()[0]
        new = old.replace("id INTEGER PRIMARY KEY,", "id INTEGER PRIMARY KEY AUTOINCREMENT,", 1)
        new = re.sub(r"^(CREATE TABLE (?:IF NOT EXISTS )?)\"?users\"?", r"\1users_v2", new, count=1)
        con.executescript("BEGIN;" + new + ";"
                          "INSERT INTO users_v2 SELECT * FROM users; DROP TABLE users;"
                          "ALTER TABLE users_v2 RENAME TO users; COMMIT;")
        top = con.execute("SELECT MAX(x) FROM (SELECT MAX(id) AS x FROM users UNION ALL "
                          "SELECT MAX(user_id) FROM audit)").fetchone()[0] or 0
        con.execute("DELETE FROM sqlite_sequence WHERE name = 'users'")
        con.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('users', ?)", (top,))
        con.execute("PRAGMA foreign_keys = ON")
        con.commit()

    @staticmethod
    def _migrate_2_to_3(con: sqlite3.Connection) -> None:
        """Chats keep their size, so a person's share of the disk is summed from an index, not from every chat."""
        if "bytes" not in [r[1] for r in con.execute("PRAGMA table_info(chats)")]:
            con.execute("ALTER TABLE chats ADD COLUMN bytes INTEGER NOT NULL DEFAULT 0")
        con.execute("UPDATE chats SET bytes = LENGTH(CAST(messages AS BLOB))")
        con.commit()

    # ------------------------------------------------------------------ plumbing

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """One connection per call: commits when the block ends, rolls back if it raises."""
        con = sqlite3.connect(str(self.db_path), timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def _load_config(self, first: bool = True) -> Dict[str, Any]:
        """hub.json merged with the defaults. Only the first load, when the hub starts, writes the defaults if the
        file is missing; a later one raises instead, so an editor saving the file never resets the hub."""
        try:
            mtime = self.config_path.stat().st_mtime_ns
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except FileNotFoundError:
            if not first:
                raise
            data = None
        except (OSError, ValueError) as e:
            raise HubError("%s can't be read: %s" % (self.config_path, e))
        merged = dict(DEFAULTS, **(data or {}))
        merged["mail"] = dict(DEFAULTS["mail"], **((data or {}).get("mail") or {}))
        if data is None:
            self.config = merged
            self.save_config()
        else:
            self._config_mtime = mtime
        return merged

    def refresh_config(self) -> None:
        """Take in hub.json again if it changed on disk, e.g. a new operator key made while the hub runs. If it
        can't be read now (gone, or half written), keep the settings in use and try again next time."""
        try:
            if self.config_path.stat().st_mtime_ns != self._config_mtime:
                self.config = self._load_config(first=False)
        except (OSError, HubError) as e:
            log.warning("could not read hub.json again (%s); keeping the settings in use", type(e).__name__)

    def save_config(self) -> None:
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.config, indent=1), encoding="utf-8")
        os.replace(tmp, self.config_path)
        self._config_mtime = self.config_path.stat().st_mtime_ns

    @property
    def max_file_bytes(self) -> int:
        return int(float(self.config.get("max_file_mb") or 200) * 1024 * 1024)

    def audit(self, con: sqlite3.Connection, actor: str, action: str, user_id: Optional[int] = None,
              detail: Optional[Dict[str, Any]] = None, ip: Optional[str] = None) -> None:
        con.execute("INSERT INTO audit (at, user_id, actor, action, detail, ip) VALUES (?, ?, ?, ?, ?, ?)",
                    (time.time(), user_id, actor, action, json.dumps(detail or {}, ensure_ascii=False), ip))
        log.info("%s %s user=%s %s", actor, action, user_id, json.dumps(detail or {}, ensure_ascii=False))

    # ------------------------------------------------------------------ the operator key

    def ensure_operator_key(self) -> Optional[Path]:
        """On first start: make the operator key and write it to operator-key.txt (never printed). Else None."""
        if self.config.get("operator_key_sha256"):
            return None
        return self.new_operator_key()

    def new_operator_key(self) -> Path:
        """A new operator key; the old one stops working. Returns the file it was written to."""
        key = "khop_" + secrets.token_urlsafe(32)
        path = self.home / "operator-key.txt"
        path.write_text("Kelvin hub operator key (opens /admin). Move it to a password manager, then delete this "
                        "file.\n\n%s\n" % key, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self.config["operator_key_sha256"] = sha(key)
        self.save_config()
        with self.connect() as con:
            self.audit(con, "hub", "operator key made")
        return path

    def is_operator(self, key: Any) -> bool:
        want = self.operator_key_hash()
        return bool(want and key) and hmac.compare_digest(sha(str(key)), want)

    def operator_key_hash(self) -> str:
        """The current operator key's hash, from hub.json as it is now (operator sessions end when it changes)."""
        self.refresh_config()
        return str(self.config.get("operator_key_sha256") or "")

    # ------------------------------------------------------------------ people

    def register(self, email: Any, name: Any, password: Any, consent: bool, orcid: Any = "",
                 ip: Optional[str] = None) -> Dict[str, Any]:
        email = _email(email)
        name = _text(name, "Please give your name.", MAX_NAME)
        orcid_id = _orcid(orcid)
        password = _check_password(password, email)
        if not consent:
            raise HubError("To make an account, please agree to the terms and the privacy notice.")
        salt = secrets.token_bytes(16)
        now = time.time()
        with self.connect() as con:
            try:
                cur = con.execute(
                    "INSERT INTO users (email, name, orcid, password_hash, salt, iterations, consent_version,"
                    " consent_at, created_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (email, name, orcid_id, _hash(password, salt, ITERATIONS), salt.hex(), ITERATIONS,
                     self.config["consent_version"], now, now, now))
            except sqlite3.IntegrityError:
                raise HubError("There is already an account with that email. Sign in, or reset its password.")
            self.audit(con, "user", "registered", cur.lastrowid, {"consent": self.config["consent_version"]}, ip)
            return _public(con.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone())

    def user(self, user_id: int) -> Dict[str, Any]:
        with self.connect() as con:
            row = con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise HubError("No such account.")
        return _public(row)

    def authenticate(self, email: Any, password: Any, ip: Optional[str] = None) -> Dict[str, Any]:
        email = str(email or "").strip().lower()
        with self.connect() as con:
            row = con.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row is None or not row["password_hash"]:
            _hash(str(password or ""), b"\0" * 16, ITERATIONS)   # take as long as a real check
            raise HubError("Wrong email or password.")
        iterations = int(row["iterations"] or 0)
        got = _hash(str(password or ""), bytes.fromhex(row["salt"]), iterations)
        if iterations < ITERATIONS:   # an older, weaker hash: take as long as a current one, so timing tells nothing
            _hash(str(password or ""), b"\0" * 16, ITERATIONS - iterations)
        if not hmac.compare_digest(got, row["password_hash"]):
            with self.connect() as con:
                self.audit(con, "user", "wrong password", row["id"], None, ip)
            raise HubError("Wrong email or password.")
        if row["disabled_at"]:
            raise HubError("This account is disabled. Write to the Kelvin team if you think that's a mistake.")
        if iterations < ITERATIONS:   # redo the weaker hash now that we have the password...
            salt = secrets.token_bytes(16)
            with self.connect() as con:   # ...unless the password changed meanwhile (a reset): never undo that
                con.execute("UPDATE users SET password_hash = ?, salt = ?, iterations = ? WHERE id = ?"
                            " AND password_hash = ?", (_hash(str(password), salt, ITERATIONS), salt.hex(), ITERATIONS,
                                                       row["id"], row["password_hash"]))
        return _public(row)

    def needs_consent(self, user: Dict[str, Any]) -> bool:
        return user.get("consent_version") != self.config["consent_version"]

    def give_consent(self, user_id: int, ip: Optional[str] = None) -> None:
        with self.connect() as con:
            con.execute("UPDATE users SET consent_version = ?, consent_at = ? WHERE id = ?",
                        (self.config["consent_version"], time.time(), user_id))
            self.audit(con, "user", "consented", user_id, {"consent": self.config["consent_version"]}, ip)

    def set_profile(self, user_id: int, name: Any, orcid: Any) -> Dict[str, Any]:
        name = _text(name, "Please give your name.", MAX_NAME)
        orcid_id = _orcid(orcid)
        with self.connect() as con:
            con.execute("UPDATE users SET name = ?, orcid = ? WHERE id = ?", (name, orcid_id, user_id))
            self.audit(con, "user", "profile changed", user_id)
        return self.user(user_id)

    def change_password(self, user_id: int, old: Any, new: Any, keep_session: Optional[str] = None,
                        ip: Optional[str] = None) -> None:
        """Also signs out every other browser (connected Kelvins stay connected)."""
        user = self.user(user_id)
        self.authenticate(user["email"], old)
        self._set_password(user_id, _check_password(new, user["email"]), keep_session)
        with self.connect() as con:
            self.audit(con, "user", "password changed", user_id, None, ip)

    def _set_password(self, user_id: int, password: str, keep_session: Optional[str] = None,
                      apps_too: bool = False) -> None:
        """Signs out every other browser; with apps_too, also disconnects every Kelvin (after a reset by mail)."""
        salt = secrets.token_bytes(16)
        with self.connect() as con:
            con.execute("UPDATE users SET password_hash = ?, salt = ?, iterations = ? WHERE id = ?",
                        (_hash(password, salt, ITERATIONS), salt.hex(), ITERATIONS, user_id))
            con.execute("DELETE FROM tokens WHERE user_id = ? AND kind IN ('web', ?) AND token_hash != ?",
                        (user_id, "app" if apps_too else "web", sha(keep_session) if keep_session else ""))

    # ------------------------------------------------------------------ one-use codes and mail

    def new_code(self, kind: str, user_id: int, data: Optional[Dict[str, Any]] = None) -> str:
        code = secrets.token_urlsafe(24)
        with self.connect() as con:
            con.execute("DELETE FROM codes WHERE expires_at < ?", (time.time(),))
            con.execute("INSERT INTO codes (code_hash, kind, user_id, data, expires_at) VALUES (?, ?, ?, ?, ?)",
                        (sha(code), kind, user_id, json.dumps(data or {}), time.time() + CODE_TTL[kind]))
        return code

    def peek_code(self, kind: str, code: Any) -> Tuple[int, Dict[str, Any]]:
        """What a code is for, without using it up. Raises HubError if it is unknown, used or expired."""
        with self.connect() as con:
            row = con.execute("SELECT * FROM codes WHERE code_hash = ? AND kind = ?",
                              (sha(str(code or "")), kind)).fetchone()
        if row is None or row["expires_at"] < time.time():
            raise HubError("This link or code has expired or was already used.")
        return int(row["user_id"]), json.loads(row["data"])

    def take_code(self, kind: str, code: Any) -> Tuple[int, Dict[str, Any]]:
        """Use a code up: it works once, even when two requests bring it at the same moment."""
        user_id, data = self.peek_code(kind, code)
        with self.connect() as con:
            taken = con.execute("DELETE FROM codes WHERE code_hash = ? AND kind = ?",
                                (sha(str(code or "")), kind)).rowcount
        if taken != 1:   # someone else used it between the look and the delete
            raise HubError("This link or code has expired or was already used.")
        return user_id, data

    def mail(self, to: str, subject: str, body: str) -> None:
        """Send through the SMTP server in hub.json (password from KELVIN_HUB_SMTP_PASSWORD), or, with none set,
        drop the message in outbox/ for the operator. Never raises: a failed mail is logged."""
        m = self.config.get("mail") or {}
        if m.get("smtp_host"):
            try:
                import smtplib
                from email.message import EmailMessage
                msg = EmailMessage()
                msg["From"], msg["To"], msg["Subject"] = m.get("from") or m.get("username"), to, subject
                msg.set_content(body)
                with smtplib.SMTP(m["smtp_host"], int(m.get("smtp_port") or 587), timeout=30) as s:
                    if m.get("starttls", True):
                        s.starttls()
                    if m.get("username"):
                        s.login(m["username"], os.environ.get("KELVIN_HUB_SMTP_PASSWORD", ""))
                    s.send_message(msg)
                return
            except Exception as e:   # the address stays out of the log; SMTP errors can quote it
                log.error("could not send a mail (%s); it is in outbox/ for the operator", type(e).__name__)
        try:   # no mail server, or it failed: the operator passes it on
            path = self.home / "outbox" / ("%s-%019d-%s.txt" % (time.strftime("%Y%m%d-%H%M%S"), time.time_ns(),
                                                                 secrets.token_hex(3)))   # in the order sent
            path.write_text("To: %s\nSubject: %s\n\n%s\n" % (to, subject, body), encoding="utf-8")
        except OSError as e:
            log.error("could not keep a mail in outbox/ (%s)", type(e).__name__)

    def send_confirmation(self, user: Dict[str, Any]) -> None:
        code = self.new_code("confirm", user["id"])
        self.mail(user["email"], "Confirm your Kelvin hub account",
                  "Hello %s,\n\nTo confirm this email address for your Kelvin hub account, open:\n%s/confirm?code=%s\n\n"
                  "If you didn't make an account, ignore this message." % (user["name"], self.base_url, code))

    def confirm_email(self, code: Any) -> Dict[str, Any]:
        user_id, _ = self.take_code("confirm", code)
        with self.connect() as con:
            con.execute("UPDATE users SET email_confirmed_at = ? WHERE id = ? AND email_confirmed_at IS NULL",
                        (time.time(), user_id))
            self.audit(con, "user", "email confirmed", user_id)
        return self.user(user_id)

    def start_reset(self, email: Any, ip: Optional[str] = None) -> None:
        """Mail a reset link if the account exists. Says nothing either way, so nobody can probe for accounts."""
        email = str(email or "").strip().lower()
        with self.connect() as con:
            row = con.execute("SELECT * FROM users WHERE email = ? AND disabled_at IS NULL", (email,)).fetchone()
            if row is not None:
                self.audit(con, "user", "password reset asked", row["id"], None, ip)
        if row is not None:
            code = self.new_code("reset", row["id"])
            self.mail(row["email"], "Reset your Kelvin hub password",
                      "Hello %s,\n\nTo choose a new password, open this link within an hour:\n%s/reset/new?code=%s\n\n"
                      "If you didn't ask for this, ignore this message; your password stays as it is."
                      % (row["name"], self.base_url, code))

    def finish_reset(self, code: Any, password: Any, ip: Optional[str] = None) -> Dict[str, Any]:
        """A new password from a reset link. A password that won't do leaves the link working. Signs out every
        browser and disconnects every Kelvin: whoever could sign in before may no longer be welcome."""
        user_id, _ = self.peek_code("reset", code)
        password = _check_password(password, self.user(user_id)["email"])
        self.take_code("reset", code)
        self._set_password(user_id, password, apps_too=True)
        with self.connect() as con:   # the link came by mail, so the address works
            con.execute("UPDATE users SET email_confirmed_at = COALESCE(email_confirmed_at, ?) WHERE id = ?",
                        (time.time(), user_id))
            self.audit(con, "user", "password reset", user_id, None, ip)
        return self.user(user_id)

    @property
    def base_url(self) -> str:
        return str(self.config.get("base_url") or DEFAULTS["base_url"]).rstrip("/")

    # ------------------------------------------------------------------ browser sessions

    def new_session(self, user_id: int) -> Tuple[str, str]:
        """(token for the cookie, the forms' anti-forgery value)."""
        token, csrf, now = secrets.token_urlsafe(32), secrets.token_urlsafe(24), time.time()
        with self.connect() as con:
            con.execute("DELETE FROM tokens WHERE kind = 'web' AND expires_at < ?", (now,))
            con.execute("INSERT INTO tokens (user_id, kind, token_hash, csrf, created_at, last_used, expires_at)"
                        " VALUES (?, 'web', ?, ?, ?, ?, ?)", (user_id, sha(token), csrf, now, now, now + SESSION_TTL))
            con.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now, user_id))
        return token, csrf

    def session(self, token: Any) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        with self.connect() as con:
            row = con.execute("SELECT t.id AS token_id, t.csrf, t.expires_at, t.last_used, u.* FROM tokens t"
                              " JOIN users u ON u.id = t.user_id WHERE t.token_hash = ? AND t.kind = 'web'",
                              (sha(str(token)),)).fetchone()
            if row is None or row["expires_at"] < time.time() or row["disabled_at"]:
                return None
            if time.time() - (row["last_used"] or 0) > 300:
                con.execute("UPDATE tokens SET last_used = ? WHERE id = ?", (time.time(), row["token_id"]))
                con.execute("UPDATE users SET last_seen = ? WHERE id = ?", (time.time(), row["id"]))
        return {"user": _public(row), "csrf": row["csrf"], "token_id": row["token_id"]}

    def end_session(self, token: Any) -> None:
        with self.connect() as con:
            con.execute("DELETE FROM tokens WHERE token_hash = ? AND kind = 'web'", (sha(str(token or "")),))

    # ------------------------------------------------------------------ connected Kelvins

    def sign_in_app(self, email: Any, password: Any, inst: Dict[str, str], ip: Optional[str] = None) -> Dict[str, Any]:
        """Kelvin's own sign-in with a central Kelvin account: the password is checked here, and this Kelvin gets a
        token for the person (one per person and Kelvin: signing in again replaces it). Kelvin sees the password, so
        it can sign the person in offline later and keep their API keys encrypted with it. Shares nothing."""
        user = self.authenticate(email, password, ip)
        if self.config.get("require_confirmed_email") and not user.get("email_confirmed_at"):
            raise HubError("Confirm your email address first (see the message we sent you).")
        token, now = "khub_" + secrets.token_urlsafe(32), time.time()
        with self._person(user["id"]), self.connect() as con:
            con.execute("INSERT INTO installations (id, name, platform, version, created_at, last_seen)"
                        " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET name = excluded.name,"
                        " platform = excluded.platform, version = excluded.version, last_seen = excluded.last_seen",
                        (inst["id"], inst["name"], inst["platform"], inst["version"], now, now))
            con.execute("DELETE FROM tokens WHERE kind = 'app' AND user_id = ? AND installation_id = ?",
                        (user["id"], inst["id"]))
            con.execute("INSERT INTO tokens (user_id, kind, token_hash, installation_id, created_at, last_used)"
                        " VALUES (?, 'app', ?, ?, ?, ?)", (user["id"], sha(token), inst["id"], now, now))
            con.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now, user["id"]))
            self.audit(con, "app", "signed in from Kelvin", user["id"], {"installation": inst["id"],
                                                                          "name": inst["name"]}, ip)
        return {"access_token": token, "token_type": "Bearer",
                "user": {"name": user["name"], "email": user["email"],
                         "email_confirmed": bool(user.get("email_confirmed_at"))},
                "needs_consent": self.needs_consent(user), "account_url": self.base_url + "/account"}

    def connect_code(self, user: Dict[str, Any], redirect_uri: str, challenge: str, installation: Dict[str, Any],
                     lab_id: Optional[int], previous: Optional[List[str]] = None) -> str:
        """After the person says yes on /connect: a code their Kelvin trades for its token (with the PKCE verifier)."""
        if self.config.get("require_confirmed_email") and not user.get("email_confirmed_at"):
            raise HubError("Confirm your email address first (see the message we sent you).")
        if lab_id is not None and not self.is_member(lab_id, user["id"]):
            raise HubError("You are not a member of that lab.")
        return self.new_code("connect", user["id"], {"redirect_uri": redirect_uri, "challenge": challenge,
                                                     "installation": installation, "lab_id": lab_id,
                                                     "previous": list(previous or [])})

    def connect_app(self, code: Any, verifier: Any, redirect_uri: Any, ip: Optional[str] = None) -> Dict[str, Any]:
        # looked at here, used up below in the same transaction as the connection: a busy hub (Busy) or a failure
        # while folding leaves the code for a retry; a wrong verifier or callback uses it up at once
        user_id, data = self.peek_code("connect", code)
        problem = None
        if str(redirect_uri or "") != data["redirect_uri"]:
            problem = "redirect_uri does not match the one this code was made for."
        elif not hmac.compare_digest(challenge_of(str(verifier or "")), data["challenge"]):
            problem = "code_verifier does not match the code_challenge."
        if problem:
            try:
                self.take_code("connect", code)
            except HubError:
                pass
            raise HubError(problem)
        user = self.user(user_id)
        if user["disabled_at"]:
            raise HubError("This account is disabled.")
        inst = data["installation"]
        lab_id = data.get("lab_id")
        if lab_id is not None and not self.is_member(lab_id, user_id):
            lab_id = None
        token, now = "khub_" + secrets.token_urlsafe(32), time.time()
        gone: List[Path] = []
        with self._person(user_id), self.connect() as con:
            if con.execute("DELETE FROM codes WHERE code_hash = ? AND kind = 'connect'",
                           (sha(str(code or "")),)).rowcount != 1:   # used meanwhile by another request
                raise HubError("This link or code has expired or was already used.")
            con.execute("INSERT INTO installations (id, lab_id, name, platform, version, created_at, last_seen)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET lab_id = excluded.lab_id,"
                        " name = excluded.name, platform = excluded.platform, version = excluded.version,"
                        " last_seen = excluded.last_seen",
                        (inst["id"], lab_id, inst.get("name", ""), inst.get("platform", ""), inst.get("version", ""),
                         now, now))
            # one connection per person per Kelvin: connecting again replaces the old token
            con.execute("DELETE FROM tokens WHERE kind = 'app' AND user_id = ? AND installation_id = ?",
                        (user_id, inst["id"]))
            con.execute("INSERT INTO tokens (user_id, kind, token_hash, installation_id, lab_id, created_at, last_used)"
                        " VALUES (?, 'app', ?, ?, ?, ?, ?)", (user_id, sha(token), inst["id"], lab_id, now, now))
            # earlier ids this Kelvin named on /connect (checked there), and its computer part (its id before ids
            # were per person)
            for old in sorted(set(data.get("previous") or []) | set(previous_ids("", inst["id"]))):
                gone += self._fold(con, user_id, old, inst["id"])
            self._refile(con, user_id, inst["id"], lab_id)
            self.audit(con, "app", "connected", user_id, {"installation": inst["id"], "name": inst.get("name", ""),
                                                          "lab": lab_id}, ip)
        for folder in gone:
            shutil.rmtree(folder, ignore_errors=True)
        return {"access_token": token, "token_type": "Bearer", "user": {"name": user["name"], "email": user["email"]},
                "lab": self.lab(lab_id)["name"] if lab_id is not None else None}

    def _fold(self, con: sqlite3.Connection, user_id: int, old: str, new: str) -> List[Path]:
        """Move what this person shared from an earlier installation id of their Kelvin to its new one, so nothing
        stays behind under the old id. Where the new one already has an item, the old copy goes. The old id's
        connection ends. Returns the run folders to delete."""
        mine = {"u": user_id, "old": old, "new": new}
        dup = ("user_id = :u AND installation_id = :old AND local_id IN (SELECT local_id FROM %s WHERE user_id = :u"
               " AND installation_id = :new)")
        gone: List[Path] = []
        for p in con.execute("SELECT o.id AS old_id, n.id AS new_id, n.shared AS new_shared FROM projects o JOIN"
                             " projects n ON n.user_id = o.user_id AND n.local_id = o.local_id AND"
                             " n.installation_id = :new WHERE o.user_id = :u AND o.installation_id = :old",
                             mine).fetchall():
            if p["new_shared"]:   # keep their items: point them at the project that stays
                for table in ("chats", "runs"):
                    con.execute("UPDATE %s SET project_id = ? WHERE project_id = ?" % table, (p["new_id"], p["old_id"]))
            else:   # the project that stays is not shared (marked since): what the old id shared of it goes
                gone += [self.home / r["files_path"] for r in con.execute(
                    "SELECT files_path FROM runs WHERE project_id = ?", (p["old_id"],)) if r["files_path"]]
                for table in ("chats", "runs"):
                    con.execute("DELETE FROM %s WHERE project_id = ?" % table, (p["old_id"],))
        con.execute("DELETE FROM projects WHERE " + dup % "projects", mine)
        con.execute("DELETE FROM chats WHERE " + dup % "chats", mine)
        gone += [self.home / r["files_path"] for r in con.execute("SELECT files_path FROM runs WHERE " + dup % "runs",
                                                                   mine) if r["files_path"]]
        con.execute("DELETE FROM runs WHERE " + dup % "runs", mine)
        moved = 0
        for table in ("projects", "chats", "runs"):
            moved += con.execute("UPDATE %s SET installation_id = :new WHERE user_id = :u AND installation_id = :old"
                                 % table, mine).rowcount
        con.execute("DELETE FROM tokens WHERE kind = 'app' AND user_id = ? AND installation_id = ?", (user_id, old))
        if moved or gone:
            self.audit(con, "app", "folded installation", user_id, {"from": old, "to": new, "moved": moved,
                                                                    "dropped": len(gone)})
        return gone

    def app(self, token: Any) -> Optional[Dict[str, Any]]:
        """The person and Kelvin behind an app token, or None."""
        if not token:
            return None
        with self.connect() as con:
            row = con.execute("SELECT t.id AS token_id, t.installation_id, t.lab_id AS token_lab, t.last_used, u.*"
                              " FROM tokens t JOIN users u ON u.id = t.user_id"
                              " WHERE t.token_hash = ? AND t.kind = 'app'", (sha(str(token)),)).fetchone()
            if row is None or row["disabled_at"]:
                return None
            if time.time() - (row["last_used"] or 0) > 60:
                now = time.time()
                con.execute("UPDATE tokens SET last_used = ? WHERE id = ?", (now, row["token_id"]))
                con.execute("UPDATE installations SET last_seen = ? WHERE id = ?", (now, row["installation_id"]))
                con.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now, row["id"]))
        return {"user": _public(row), "token_id": row["token_id"], "installation_id": row["installation_id"],
                "lab_id": row["token_lab"]}

    @staticmethod
    def _refile(con: sqlite3.Connection, user_id: int, installation_id: str, lab_id: Optional[int]) -> None:
        """Everything a Kelvin shared goes under the lab it shares into now; None: only its person sees it."""
        con.execute("UPDATE projects SET lab_id = CASE WHEN shared = 1 THEN ? END WHERE user_id = ?"
                    " AND installation_id = ?", (lab_id, user_id, installation_id))
        for table in ("chats", "runs"):
            con.execute("UPDATE %s SET lab_id = ? WHERE user_id = ? AND installation_id = ?" % table,
                        (lab_id, user_id, installation_id))

    def disconnect(self, token: Any, ip: Optional[str] = None) -> None:
        app = self.app(token)
        if app is not None:
            self.revoke(app["user"]["id"], app["token_id"], ip, actor="app")

    def connections(self, user_id: int) -> List[Dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT t.id, t.created_at, t.last_used, i.name, i.platform, i.version, l.name AS lab"
                               " FROM tokens t LEFT JOIN installations i ON i.id = t.installation_id"
                               " LEFT JOIN labs l ON l.id = t.lab_id WHERE t.user_id = ? AND t.kind = 'app'"
                               " ORDER BY t.last_used DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def revoke(self, user_id: int, token_id: Any, ip: Optional[str] = None, actor: str = "user") -> None:
        with self.connect() as con:
            cur = con.execute("DELETE FROM tokens WHERE id = ? AND user_id = ? AND kind = 'app'", (token_id, user_id))
            if cur.rowcount:
                self.audit(con, actor, "disconnected", user_id, {"token": token_id}, ip)

    # ------------------------------------------------------------------ labs

    def create_lab(self, user_id: int, name: Any, institution: Any = "") -> Dict[str, Any]:
        name = _text(name, "A lab needs a name.", MAX_NAME)
        institution = _text(institution, None, MAX_NAME)
        now = time.time()
        with self.connect() as con:
            for _ in range(5):
                try:
                    cur = con.execute("INSERT INTO labs (name, institution, join_code, created_at) VALUES (?, ?, ?, ?)",
                                      (name, institution, join_code(), now))
                    break
                except sqlite3.IntegrityError:   # the join code was taken (about 1 in 10^17)
                    continue
            else:
                raise HubError("Could not make a join code; try again.")
            con.execute("INSERT INTO lab_members (lab_id, user_id, joined_at) VALUES (?, ?, ?)",
                        (cur.lastrowid, user_id, now))
            self.audit(con, "user", "lab made", user_id, {"lab": cur.lastrowid})
        return self.lab(cur.lastrowid)

    def lab(self, lab_id: Any) -> Dict[str, Any]:
        with self.connect() as con:
            row = con.execute("SELECT * FROM labs WHERE id = ?", (lab_id,)).fetchone()
        if row is None:
            raise HubError("No such lab.")
        return dict(row)

    def join_lab(self, user_id: int, code: Any) -> Dict[str, Any]:
        with self.connect() as con:
            row = con.execute("SELECT * FROM labs WHERE join_code = ?", (_normalize_code(code),)).fetchone()
            if row is None:
                raise HubError("No lab has that join code. Ask a member of the lab for it.")
            con.execute("INSERT OR IGNORE INTO lab_members (lab_id, user_id, joined_at) VALUES (?, ?, ?)",
                        (row["id"], user_id, time.time()))
            self.audit(con, "user", "joined lab", user_id, {"lab": row["id"]})
        return dict(row)

    def leave_lab(self, user_id: int, lab_id: Any) -> None:
        """Leaving takes your shared work out of the lab for good (joining again doesn't bring it back); a lab
        nobody is in any more is deleted."""
        with self.connect() as con:
            con.execute("DELETE FROM lab_members WHERE lab_id = ? AND user_id = ?", (lab_id, user_id))
            for table in ("tokens", "projects", "chats", "runs"):
                con.execute("UPDATE %s SET lab_id = NULL WHERE user_id = ? AND lab_id = ?" % table, (user_id, lab_id))
            con.execute("DELETE FROM labs WHERE id = ? AND NOT EXISTS (SELECT 1 FROM lab_members WHERE lab_id = ?)",
                        (lab_id, lab_id))
            self.audit(con, "user", "left lab", user_id, {"lab": lab_id})

    def is_member(self, lab_id: Any, user_id: int, con: Optional[sqlite3.Connection] = None) -> bool:
        if con is None:
            with self.connect() as c:
                return self.is_member(lab_id, user_id, c)
        return con.execute("SELECT 1 FROM lab_members WHERE lab_id = ? AND user_id = ?",
                           (lab_id, user_id)).fetchone() is not None

    def labs_of(self, user_id: int) -> List[Dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT l.*, m.joined_at, (SELECT COUNT(*) FROM lab_members x WHERE x.lab_id = l.id)"
                               " AS members FROM labs l JOIN lab_members m ON m.lab_id = l.id WHERE m.user_id = ?"
                               " ORDER BY l.name", (user_id,)).fetchall()
            out = []
            for r in rows:
                lab = dict(r)
                lab["people"] = [m["name"] for m in con.execute(
                    "SELECT u.name FROM lab_members m JOIN users u ON u.id = m.user_id WHERE m.lab_id = ?"
                    " ORDER BY u.name LIMIT 200", (r["id"],))]
                out.append(lab)
        return out

    # ------------------------------------------------------------------ sync: what connected Kelvins share

    def sync(self, app: Dict[str, Any], payload: Any) -> Dict[str, Any]:
        """Take what a Kelvin shares: {"projects": [...], "chats": [...], "runs": [...], "removed": {...}}.

        The hub applies the sharing rules itself, on the marks Kelvin sends: an unpublished, confidential or
        model-limited project is kept only as "not shared" (no name), and whatever the hub had of it is deleted;
        incognito, unpublished, confidential or model-limited chats, runs marked incognito, unpublished or
        confidential, and chats or runs in a project that isn't shared are refused (and deleted if the hub had
        them). Everything this Kelvin shared is filed under the lab it shares into now. Chats count toward the
        person's share of the disk, as run files do."""
        if not isinstance(payload, dict):
            raise HubError("The body must be a JSON object.")
        user = app["user"]
        if self.config.get("require_confirmed_email") and not user.get("email_confirmed_at"):
            raise HubError("Confirm your email address before sharing.")
        uid, inst, now = user["id"], app["installation_id"], time.time()
        done: Dict[str, Any] = {"projects": 0, "chats": 0, "runs": 0, "withheld": 0, "removed": 0, "refused": []}
        gone_files: List[Path] = []

        def items(kind: str) -> List[Dict[str, Any]]:
            got = payload.get(kind) or []
            if not isinstance(got, list) or len(got) > MAX_BATCH or not all(isinstance(i, dict) for i in got):
                raise HubError("%s must be a list of at most %d objects." % (kind, MAX_BATCH))
            return got

        with self._person(uid), self.connect() as con:
            lab_id = app["lab_id"] if app["lab_id"] is not None and self.is_member(app["lab_id"], uid, con) else None
            self._refile(con, uid, inst, lab_id)
            with self._uploads:
                held = sum(self.reserved.get(uid, {}).values())
            used = self._used(con, uid) + held
            quota = self.quota_bytes

            def project_row(local: Any) -> Optional[sqlite3.Row]:
                return con.execute("SELECT * FROM projects WHERE user_id = ? AND installation_id = ? AND local_id = ?",
                                   (uid, inst, str(local))).fetchone()

            def purge_runs(where: str, args: Tuple[Any, ...]) -> None:
                for r in con.execute("SELECT files_path FROM runs WHERE " + where, args):
                    if r["files_path"]:
                        gone_files.append(self.home / r["files_path"])
                con.execute("DELETE FROM runs WHERE " + where, args)

            for p in items("projects"):
                local = _local_id(p.get("id"), "A project")
                old = project_row(local)
                if project_shared(p):
                    name = _text(p.get("name"), "A project needs a name.", MAX_NAME)
                    if old is None:
                        con.execute("INSERT INTO projects (user_id, lab_id, installation_id, local_id, name, shared,"
                                    " updated_at) VALUES (?, ?, ?, ?, ?, 1, ?)", (uid, lab_id, inst, local, name, now))
                    else:
                        con.execute("UPDATE projects SET name = ?, shared = 1, lab_id = ?, updated_at = ?"
                                    " WHERE id = ?", (name, lab_id, now, old["id"]))
                    done["projects"] += 1
                else:   # unpublished or confidential: the hub keeps only that it exists and isn't shared
                    if old is None:
                        con.execute("INSERT INTO projects (user_id, lab_id, installation_id, local_id, name, shared,"
                                    " updated_at) VALUES (?, NULL, ?, ?, '', 0, ?)", (uid, inst, local, now))
                    else:
                        con.execute("DELETE FROM chats WHERE project_id = ?", (old["id"],))
                        purge_runs("project_id = ?", (old["id"],))
                        con.execute("UPDATE projects SET name = '', shared = 0, lab_id = NULL, updated_at = ?"
                                    " WHERE id = ?", (now, old["id"]))
                    done["withheld"] += 1

            def filed(item: Dict[str, Any], kind: str, local: str) -> Tuple[bool, Optional[int], Optional[int]]:
                """(may it be shared, its project's hub id, its lab)"""
                if not item.get("project"):
                    return True, None, lab_id
                project = project_row(item["project"])
                if project is None or not project["shared"]:
                    done["refused"].append({kind: local, "why": "its project isn't shared (or wasn't sent first)"})
                    return False, None, None
                return True, project["id"], lab_id

            for c in items("chats"):
                local = _local_id(c.get("id"), "A chat")
                if not chat_shared(c):
                    done["refused"].append({"chat": local, "why": "incognito, unpublished and confidential chats are "
                                                                  "never shared"})
                    con.execute("DELETE FROM chats WHERE user_id = ? AND installation_id = ? AND local_id = ?",
                                (uid, inst, local))
                    continue
                ok, project_id, item_lab = filed(c, "chat", local)
                if not ok:
                    con.execute("DELETE FROM chats WHERE user_id = ? AND installation_id = ? AND local_id = ?",
                                (uid, inst, local))
                    continue
                messages = c.get("messages") or []
                if not isinstance(messages, list):
                    raise HubError("A chat's messages must be a list.")
                text = json.dumps(messages, ensure_ascii=False)
                size = len(text.encode("utf-8"))
                if size > MAX_MESSAGES:
                    done["refused"].append({"chat": local, "why": "longer than the hub keeps (5 MB)"})
                    continue
                had = con.execute("SELECT bytes FROM chats WHERE user_id = ? AND installation_id = ? AND local_id = ?",
                                  (uid, inst, local)).fetchone()
                had = had[0] if had else 0
                if used - had + size > quota:
                    done["refused"].append({"chat": local, "why": "over your share of the hub's disk"})
                    continue
                used += size - had
                con.execute("INSERT INTO chats (user_id, project_id, lab_id, installation_id, local_id, title, messages,"
                            " updated_at, bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (user_id, installation_id,"
                            " local_id) DO UPDATE SET project_id = excluded.project_id, lab_id = excluded.lab_id,"
                            " title = excluded.title, messages = excluded.messages, updated_at = excluded.updated_at,"
                            " bytes = excluded.bytes",
                            (uid, project_id, item_lab, inst, local, _text(c.get("title"), None, MAX_TITLE) or "Chat",
                             text, _timestamp(c.get("updated"), now), size))
                done["chats"] += 1

            for r in items("runs"):
                local = _local_id(r.get("id"), "A run")
                if not run_shared(r):
                    done["refused"].append({"run": local, "why": "runs of incognito, unpublished or confidential work "
                                                                 "are never shared"})
                    purge_runs("user_id = ? AND installation_id = ? AND local_id = ?", (uid, inst, local))
                    continue
                ok, project_id, item_lab = filed(r, "run", local)
                if not ok:
                    purge_runs("user_id = ? AND installation_id = ? AND local_id = ?", (uid, inst, local))
                    continue
                meta = json.dumps(r.get("meta") or {}, ensure_ascii=False)
                if len(meta.encode("utf-8")) > MAX_META:
                    done["refused"].append({"run": local, "why": "its meta is larger than 256 kB"})
                    continue
                created = _timestamp(r.get("created"), now)
                con.execute("INSERT INTO runs (user_id, project_id, lab_id, installation_id, local_id, device, meta,"
                            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                            " ON CONFLICT (user_id, installation_id, local_id) DO UPDATE SET"
                            " project_id = excluded.project_id, lab_id = excluded.lab_id, device = excluded.device,"
                            " meta = excluded.meta, updated_at = excluded.updated_at",
                            (uid, project_id, item_lab, inst, local, _text(r.get("device"), None, MAX_NAME), meta,
                             created, now))
                row = con.execute("SELECT id, files_path FROM runs WHERE user_id = ? AND installation_id = ?"
                                  " AND local_id = ?", (uid, inst, local)).fetchone()
                if not row["files_path"]:
                    con.execute("UPDATE runs SET files_path = ? WHERE id = ?",
                                ("files/%d/%d" % (uid, row["id"]), row["id"]))
                done["runs"] += 1

            removed = payload.get("removed") or {}
            if not isinstance(removed, dict):
                raise HubError("removed must be an object of id lists.")
            for kind, table in (("chats", "chats"), ("runs", "runs"), ("projects", "projects")):
                ids = removed.get(kind) or []
                if not isinstance(ids, list) or len(ids) > MAX_BATCH:
                    raise HubError("removed.%s must be a list of at most %d ids." % (kind, MAX_BATCH))
                for local in ids:
                    args = (uid, inst, _local_id(local, "A removed item"))
                    where = "user_id = ? AND installation_id = ? AND local_id = ?"
                    if table == "projects":
                        p = con.execute("SELECT id FROM projects WHERE " + where, args).fetchone()
                        if p is not None:
                            purge_runs("project_id = ?", (p["id"],))
                    if table == "runs":
                        before = con.total_changes
                        purge_runs(where, args)
                        done["removed"] += con.total_changes - before
                    else:
                        done["removed"] += con.execute("DELETE FROM %s WHERE %s" % (table, where), args).rowcount
            installation = payload.get("installation") or {}
            if isinstance(installation, dict) and installation.get("version"):
                con.execute("UPDATE installations SET version = ?, last_seen = ? WHERE id = ?",
                            (_text(installation["version"], None, 40), now, inst))
            self.audit(con, "app", "synced", uid, {k: v for k, v in done.items() if k != "refused" and v})
        for folder in gone_files:
            shutil.rmtree(folder, ignore_errors=True)
        return done

    def _lock_for(self, user_id: int) -> threading.Lock:
        with self._uploads:
            return self._user_locks.setdefault(user_id, threading.Lock())

    @contextlib.contextmanager
    def _person(self, user_id: int) -> Iterator[None]:
        """One person's syncs, uploads and deletion take turns, so their share of the disk can't be overrun in
        parallel. A request waits at most LOCK_WAIT, then gets "busy" (429); the server also turns away a person's
        extra requests before they take a worker thread."""
        lock = self._lock_for(user_id)
        if not lock.acquire(timeout=LOCK_WAIT):
            raise Busy("The hub is still busy with your other requests; try again in a moment.")
        try:
            yield
        finally:
            lock.release()

    @staticmethod
    def _used(con: sqlite3.Connection, user_id: int) -> int:
        """Bytes a person keeps on the hub: run files and chats."""
        return con.execute("SELECT (SELECT COALESCE(SUM(bytes), 0) FROM runs WHERE user_id = :u) + (SELECT "
                           "COALESCE(SUM(bytes), 0) FROM chats WHERE user_id = :u)",
                           {"u": user_id}).fetchone()[0]

    @property
    def quota_bytes(self) -> int:
        return int(float(self.config.get("quota_gb_per_user") or 5) * 1024 ** 3)

    def reserve_upload(self, app: Dict[str, Any], run_local_id: Any, name: Any, size: int) -> Dict[str, Any]:
        """Before a file streams in: check its run and name, and hold its bytes against the person's share of the
        disk, so parallel uploads can't overrun it. Release the ticket afterwards (release_upload)."""
        parts = file_parts(name)
        uid = app["user"]["id"]
        local = _local_id(run_local_id, "A run")
        if size > self.max_file_bytes:
            raise TooMuch("A file may be at most %s MB." % self.config.get("max_file_mb"))
        with self._person(uid):
            with self._uploads:   # what this person's uploads in flight hold (a release takes only this lock)
                mine = dict(self.reserved.get(uid, {}))
            if len(mine) >= UPLOADS_AT_ONCE:
                raise Busy("Send at most %d files at a time." % UPLOADS_AT_ONCE)
            with self.connect() as con:
                row = con.execute("SELECT id, files_path FROM runs WHERE user_id = ? AND installation_id = ?"
                                  " AND local_id = ?", (uid, app["installation_id"], local)).fetchone()
                if row is None:
                    raise HubError("Send the run itself first (POST /api/sync), then its files.")
                used = self._used(con, uid)
            folder = (self.home / row["files_path"]).resolve()
            dest = folder.joinpath(*parts).resolve()
            if folder not in dest.parents:
                raise HubError("That file name leaves the run's folder.")
            old = dest.stat().st_size if dest.is_file() else 0
            if used + sum(mine.values()) - old + size > self.quota_bytes:
                raise TooMuch("This would take you over your share of the hub's disk (%s GB)."
                               % self.config.get("quota_gb_per_user"))
            key = secrets.token_hex(8)
            with self._uploads:
                self.reserved.setdefault(uid, {})[key] = size
        return {"key": key, "user": uid, "run": row["id"], "local": local, "file": "/".join(parts),
                "folder": folder, "dest": dest, "size": size}

    def store_upload(self, ticket: Dict[str, Any], upload: Path, size: int) -> Dict[str, Any]:
        """Move a file that streamed into tmp/ into its run's folder."""
        if size != ticket["size"]:
            raise HubError("The file wasn't as long as its Content-Length said.")
        with self._person(ticket["user"]):
            with self.connect() as con:
                if con.execute("SELECT 1 FROM runs WHERE id = ?", (ticket["run"],)).fetchone() is None:
                    raise HubError("That run was removed meanwhile.")
            ticket["dest"].parent.mkdir(parents=True, exist_ok=True)
            os.replace(upload, ticket["dest"])
            total = sum(f.stat().st_size for f in ticket["folder"].rglob("*") if f.is_file())
            with self.connect() as con:
                con.execute("UPDATE runs SET bytes = ?, updated_at = ? WHERE id = ?",
                            (total, time.time(), ticket["run"]))
        return {"run": ticket["local"], "file": ticket["file"], "bytes": size}

    def release_upload(self, ticket: Dict[str, Any]) -> None:
        with self._uploads:   # never waits on the person: a release must always happen
            mine = self.reserved.get(ticket["user"], {})
            mine.pop(ticket["key"], None)
            if not mine:
                self.reserved.pop(ticket["user"], None)

    def store_run_file(self, app: Dict[str, Any], run_local_id: Any, name: Any, upload: Path, size: int) -> Dict[str, Any]:
        """Reserve, store and release in one go, for a file already on disk."""
        ticket = self.reserve_upload(app, run_local_id, name, size)
        try:
            return self.store_upload(ticket, upload, size)
        finally:
            self.release_upload(ticket)

    # ------------------------------------------------------------------ what a person can see

    def shared_with(self, user_id: int, limit: int = 200) -> Dict[str, List[Dict[str, Any]]]:
        """Their own shared work, and what labmates shared in labs they are both in, newest first."""
        me = {"me": user_id, "limit": limit}
        with self.connect() as con:
            projects = con.execute(
                "SELECT x.id, x.name, x.updated_at, x.user_id, u.name AS owner, l.name AS lab FROM projects x"
                " JOIN users u ON u.id = x.user_id LEFT JOIN labs l ON l.id = x.lab_id"
                " WHERE x.shared = 1 AND " + VISIBLE + " ORDER BY x.updated_at DESC LIMIT :limit", me).fetchall()
            chats = con.execute(
                "SELECT x.id, x.title, x.updated_at, x.user_id, u.name AS owner, l.name AS lab, p.name AS project"
                " FROM chats x JOIN users u ON u.id = x.user_id LEFT JOIN labs l ON l.id = x.lab_id"
                " LEFT JOIN projects p ON p.id = x.project_id WHERE " + VISIBLE +
                " ORDER BY x.updated_at DESC LIMIT :limit", me).fetchall()
            runs = con.execute(
                "SELECT x.id, x.local_id, x.device, x.bytes, x.created_at, x.user_id, u.name AS owner, l.name AS lab,"
                " p.name AS project FROM runs x JOIN users u ON u.id = x.user_id LEFT JOIN labs l ON l.id = x.lab_id"
                " LEFT JOIN projects p ON p.id = x.project_id WHERE " + VISIBLE +
                " ORDER BY x.created_at DESC LIMIT :limit", me).fetchall()
        return {"projects": [dict(r) for r in projects], "chats": [dict(r) for r in chats],
                "runs": [dict(r) for r in runs]}

    def chat(self, user_id: int, chat_id: Any) -> Dict[str, Any]:
        with self.connect() as con:
            row = con.execute("SELECT x.*, u.name AS owner, l.name AS lab, p.name AS project FROM chats x"
                              " JOIN users u ON u.id = x.user_id LEFT JOIN labs l ON l.id = x.lab_id"
                              " LEFT JOIN projects p ON p.id = x.project_id WHERE x.id = :id AND " + VISIBLE,
                              {"id": chat_id, "me": user_id}).fetchone()
        if row is None:
            raise HubError("No such chat, or it isn't shared with you.")
        chat = dict(row)
        chat["messages"] = json.loads(chat["messages"])
        return chat

    def run(self, user_id: int, run_id: Any) -> Dict[str, Any]:
        with self.connect() as con:
            row = con.execute("SELECT x.*, u.name AS owner, l.name AS lab, p.name AS project FROM runs x"
                              " JOIN users u ON u.id = x.user_id LEFT JOIN labs l ON l.id = x.lab_id"
                              " LEFT JOIN projects p ON p.id = x.project_id WHERE x.id = :id AND " + VISIBLE,
                              {"id": run_id, "me": user_id}).fetchone()
        if row is None:
            raise HubError("No such run, or it isn't shared with you.")
        run = dict(row)
        run["meta"] = json.loads(run["meta"])
        folder = self.home / run["files_path"]
        run["files"] = sorted(f.relative_to(folder).as_posix() for f in folder.rglob("*") if f.is_file()) \
            if folder.is_dir() else []
        return run

    def run_file(self, user_id: int, run_id: Any, name: Any) -> Path:
        run = self.run(user_id, run_id)
        folder = (self.home / run["files_path"]).resolve()
        path = folder.joinpath(*file_parts(name)).resolve()
        if folder not in path.parents or not path.is_file():
            raise HubError("No such file in this run.")
        return path

    # ------------------------------------------------------------------ export and deletion

    def export(self, user_id: int) -> Path:
        """Everything the hub keeps about a person, as a zip in tmp/ (the caller deletes it after sending)."""
        user = self.user(user_id)
        with self.connect() as con:
            projects = [dict(r) for r in con.execute("SELECT * FROM projects WHERE user_id = ?", (user_id,))]
            chats = [dict(r) for r in con.execute("SELECT * FROM chats WHERE user_id = ?", (user_id,))]
            runs = [dict(r) for r in con.execute("SELECT * FROM runs WHERE user_id = ?", (user_id,))]
            audit = [dict(r) for r in con.execute("SELECT at, actor, action, detail, ip FROM audit WHERE user_id = ?"
                                                  " ORDER BY at", (user_id,))]
        path = self.home / "tmp" / ("export-%d-%s.zip" % (user_id, secrets.token_hex(4)))
        dump = lambda obj: json.dumps(obj, indent=1, ensure_ascii=False)   # noqa: E731
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("README.txt", "Your Kelvin hub account, exported %s.\n\naccount.json  your account (the "
                       "password is kept only as a hash and is not included)\nlabs.json  your labs\nconnections.json  "
                       "the Kelvins you connected\nprojects.json  your projects\nchats/  your shared conversations\n"
                       "runs/  your shared runs and their files\nactivity.json  the hub's record of your account's "
                       "activity\n" % time.strftime("%Y-%m-%d %H:%M"))
            z.writestr("account.json", dump(user))
            z.writestr("labs.json", dump(self.labs_of(user_id)))
            z.writestr("connections.json", dump(self.connections(user_id)))
            z.writestr("projects.json", dump(projects))
            z.writestr("activity.json", dump(audit))
            for c in chats:
                c["messages"] = json.loads(c["messages"])
                z.writestr("chats/%d-%s.json" % (c["id"], c["local_id"]), dump(c))
            for r in runs:
                r["meta"] = json.loads(r["meta"])
                base = "runs/%d-%s/" % (r["id"], r["local_id"])
                z.writestr(base + "run.json", dump(r))
                folder = self.home / r["files_path"]
                if r["files_path"] and folder.is_dir():
                    for f in sorted(folder.rglob("*")):
                        if f.is_file():
                            z.write(f, base + "files/" + f.relative_to(folder).as_posix())
        with self.connect() as con:
            self.audit(con, "user", "exported", user_id)
        return path

    def delete_user(self, user_id: int, actor: str = "user", ip: Optional[str] = None) -> None:
        """Delete the account and everything it shared. Labs left empty go too, and so do the Kelvins only it used
        and mail to it waiting in outbox/. The activity record keeps only that account number N was deleted (ids are
        never reused); hub.log never has addresses. Backups still hold it until they age out (backups_kept days).
        It waits for the person's sync or upload in progress, so no file lands after the folder is gone."""
        email = self.user(user_id)["email"]
        with self._person(user_id):
            with self.connect() as con:
                labs = [r["lab_id"] for r in con.execute("SELECT lab_id FROM lab_members WHERE user_id = ?",
                                                          (user_id,))]
                con.execute("DELETE FROM users WHERE id = ?", (user_id,))
                for lab_id in labs:
                    con.execute("DELETE FROM labs WHERE id = ? AND NOT EXISTS (SELECT 1 FROM lab_members WHERE"
                                " lab_id = ?)", (lab_id, lab_id))
                con.execute("DELETE FROM installations WHERE id NOT IN (SELECT installation_id FROM tokens WHERE"
                            " installation_id IS NOT NULL) AND id NOT IN (SELECT installation_id FROM projects UNION"
                            " SELECT installation_id FROM chats UNION SELECT installation_id FROM runs)")
                con.execute("UPDATE audit SET detail = '{}', ip = NULL WHERE user_id = ?", (user_id,))
                self.audit(con, actor, "account deleted", user_id)
            # committed: the files go after, so nobody else's sign-in or sync waits on the disk (a large account's
            # folder can take many seconds); the person's lock is still held, so no upload lands meanwhile
            shutil.rmtree(self.home / "files" / str(int(user_id)), ignore_errors=True)
        self._forget_mail(email)

    def _forget_mail(self, email: str) -> None:
        """Delete mail to this address waiting in outbox/."""
        for mail in (self.home / "outbox").glob("*.txt"):
            try:
                if mail.read_text(encoding="utf-8").split("\n", 1)[0].strip().lower() == "to: " + email.lower():
                    mail.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ the operator's view

    def stats(self) -> Dict[str, Any]:
        with self.connect() as con:
            one = lambda sql: con.execute(sql).fetchone()[0]   # noqa: E731
            stats = {
                "people": one("SELECT COUNT(*) FROM users"),
                "confirmed": one("SELECT COUNT(*) FROM users WHERE email_confirmed_at IS NOT NULL"),
                "disabled": one("SELECT COUNT(*) FROM users WHERE disabled_at IS NOT NULL"),
                "active_30d": one("SELECT COUNT(*) FROM users WHERE last_seen > %f" % (time.time() - 30 * 86400)),
                "labs": one("SELECT COUNT(*) FROM labs"),
                "kelvins": one("SELECT COUNT(*) FROM installations"),
                "connections": one("SELECT COUNT(*) FROM tokens WHERE kind = 'app'"),
                "projects": one("SELECT COUNT(*) FROM projects WHERE shared = 1"),
                "chats": one("SELECT COUNT(*) FROM chats"),
                "runs": one("SELECT COUNT(*) FROM runs"),
                "run_bytes": one("SELECT COALESCE(SUM(bytes), 0) FROM runs"),
            }
        stats["database_bytes"] = self.db_path.stat().st_size
        backups = sorted((self.home / "backups").glob("hub-*.sqlite3"))
        stats["last_backup"] = backups[-1].name if backups else None
        return stats

    def find_users(self, query: Any = "", limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        like = "%%%s%%" % str(query or "").strip().replace("%", "").replace("_", "")
        with self.connect() as con:
            rows = con.execute("SELECT u.*, (SELECT COUNT(*) FROM lab_members m WHERE m.user_id = u.id) AS labs"
                               " FROM users u WHERE u.email LIKE ? OR u.name LIKE ? ORDER BY u.created_at DESC"
                               " LIMIT ? OFFSET ?", (like, like, limit, offset)).fetchall()
        return [dict(_public(r), labs=r["labs"]) for r in rows]

    def set_disabled(self, user_id: int, disabled: bool, ip: Optional[str] = None) -> None:
        with self.connect() as con:
            con.execute("UPDATE users SET disabled_at = ? WHERE id = ?", (time.time() if disabled else None, user_id))
            if disabled:
                con.execute("DELETE FROM tokens WHERE user_id = ?", (user_id,))
            self.audit(con, "operator", "disabled" if disabled else "enabled", user_id, None, ip)

    def recent_activity(self, n: int = 100) -> List[Dict[str, Any]]:
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (n,))]

    # ------------------------------------------------------------------ backups

    def backup(self, day: Optional[str] = None, force: bool = False) -> Optional[Path]:
        """Copy the database to backups/hub-<day>.sqlite3 (SQLite's online backup: safe while the hub runs), once
        a day unless forced, keeping the newest backups_kept copies. Run files are not in it; back up files/ too."""
        day = day or time.strftime("%Y-%m-%d")
        dest = self.home / "backups" / ("hub-%s.sqlite3" % day)
        if dest.exists() and not force:
            return None
        tmp = dest.with_suffix(".tmp")
        with contextlib.closing(sqlite3.connect(str(self.db_path))) as src, \
                contextlib.closing(sqlite3.connect(str(tmp))) as dst:
            src.backup(dst)
        os.replace(tmp, dest)
        kept = max(1, int(self.config.get("backups_kept") or 30))
        for old in sorted((self.home / "backups").glob("hub-*.sqlite3"))[:-kept]:
            old.unlink()
        with self.connect() as con:
            self.audit(con, "hub", "backed up", None, {"file": dest.name})
        return dest
