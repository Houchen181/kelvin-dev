"""The hub's model gateway: a person signed in to Kelvin with their Kelvin account uses Claude, GPT, Gemini and OpenCode
with the keys the hub's owner set in the owner console, so nobody needs keys of their own (Kelvin's web/hubmodels.py).

- **Each service's own API, unchanged:** /llm/<service>/<path> passes a request on to the service and streams its
  answer back as it comes, so Kelvin's model connections only change their address and send the person's hub token
  as their key. Prompt caching, thinking, pictures, PDFs and web search work as with a key of one's own. Only the
  calls Kelvin makes pass (SERVICES): answers, token counts and model lists.
- **Keys** stay on the hub, sealed with gateway.key in its folder (back that file up apart from the database:
  without it the keys must be set again). They never go to anyone's computer.
- **What is recorded:** who, which lab, which service and model, the tokens and what they cost (prices from
  models.dev, catalog.py). Never the text.
- **Budgets:** each person has a monthly budget in dollars (the owner's default, or their own); a lab may have one
  too. A request past it is refused (402) with a reason the chat shows; at 80% the person gets a mail, once a month.
- **Limits:** requests per minute and answers at once, per person (hub.json "llm"); 40 MB a request."""
from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import anyio
from starlette.responses import JSONResponse, Response, StreamingResponse

from .._version import __version__

log = logging.getLogger("kelvin.hub")

# id: name, where it is, the header its key goes in (and how), the calls that pass, the headers passed on with them.
# A model's name starts with a letter or digit: never a path segment of dots.
SERVICES: Dict[str, Dict[str, Any]] = {
    "anthropic": {"name": "Claude", "upstream": "https://api.anthropic.com", "header": "x-api-key", "form": "{}",
                  "calls": [("POST", r"v1/messages"), ("POST", r"v1/messages/count_tokens"),
                            ("GET", r"v1/models(?:/\w[\w.-]*)?")],
                  "keep": ("anthropic-version", "anthropic-beta")},
    "openai": {"name": "GPT", "upstream": "https://api.openai.com", "header": "authorization", "form": "Bearer {}",
               "calls": [("POST", r"v1/chat/completions"), ("POST", r"v1/responses"),
                         ("GET", r"v1/models(?:/\w[\w.:-]*)?")],
               "keep": ("openai-beta",)},
    "gemini": {"name": "Gemini", "upstream": "https://generativelanguage.googleapis.com", "header": "x-goog-api-key",
               "form": "{}",
               "calls": [("POST", r"v1beta/models/(?P<model>\w[\w.-]*):(?:generateContent|streamGenerateContent|"
                                  r"countTokens)"), ("GET", r"v1beta/models(?:/\w[\w.-]*)?")],
               "keep": ()},
    "opencode": {"name": "OpenCode Zen", "upstream": "https://opencode.ai/inference", "header": "authorization",
                 "form": "Bearer {}", "calls": [("POST", r"openai/v1/chat/completions"), ("GET", r"v1/models")],
                 "keep": ("x-opencode-session",)},
    # OpenCode's Go subscription: its own addresses, and a key that works only there. Go routes and caches by a
    # session per conversation (x-opencode-session), and refuses a request without one
    "opencode-go": {"name": "OpenCode Go", "upstream": "https://opencode.ai/inference/go", "header": "authorization",
                    "form": "Bearer {}", "calls": [("POST", r"openai/v1/chat/completions"), ("GET", r"v1/models")],
                    "keep": ("x-opencode-session",)},
}
MODEL_LISTS = {"anthropic": "v1/models", "openai": "v1/models", "gemini": "v1beta/models", "opencode": "v1/models",
               "opencode-go": "v1/models"}
MAX_BODY = 40 * 2 ** 20       # bytes of one request: pictures and PDFs ride in it
MAX_METERED = 32 * 2 ** 20    # bytes of a whole (not streamed) answer read for its token counts
DEFAULT_BUDGET = 20.0         # dollars per person per month, until the owner sets another
SEARCH_USD = 0.01             # Claude's web search, per search
STREAM_S = 900                # an answer still counted as running after this is taken as gone
LIMITS = {"per_minute": 60, "at_once": 4, "alert_at": 0.8, "fallback_price": {"input": 15.0, "output": 75.0}}
TABLES = """
CREATE TABLE IF NOT EXISTS llm_keys (service TEXT PRIMARY KEY, sealed TEXT NOT NULL, hint TEXT NOT NULL,
    set_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS llm_offers (service TEXT PRIMARY KEY, models TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_access (service TEXT PRIMARY KEY, emails TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_budgets (scope TEXT NOT NULL, ref INTEGER NOT NULL, usd REAL NOT NULL,
    PRIMARY KEY (scope, ref));
CREATE TABLE IF NOT EXISTS llm_usage (id INTEGER PRIMARY KEY, at REAL NOT NULL, month TEXT NOT NULL,
    user_id INTEGER NOT NULL, lab_id INTEGER, service TEXT NOT NULL, model TEXT NOT NULL,
    input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0, cache_read INTEGER NOT NULL DEFAULT 0,
    cache_write INTEGER NOT NULL DEFAULT 0, usd REAL NOT NULL DEFAULT 0, status INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS llm_usage_person ON llm_usage (user_id, month);
CREATE INDEX IF NOT EXISTS llm_usage_month ON llm_usage (month);
CREATE TABLE IF NOT EXISTS llm_alerts (scope TEXT NOT NULL, ref INTEGER NOT NULL, month TEXT NOT NULL,
    PRIMARY KEY (scope, ref, month));
"""


def month_of(at: Optional[float] = None) -> str:
    return time.strftime("%Y-%m", time.gmtime(time.time() if at is None else at))


def renews(month: str) -> str:
    """The first day of the month after this one, in words."""
    year, mon = int(month[:4]), int(month[5:7])
    year, mon = (year + 1, 1) if mon == 12 else (year, mon + 1)
    return "%s 1" % calendar.month_name[mon]


def refusal(status: int, message: str, headers: Optional[Dict[str, str]] = None) -> JSONResponse:
    """An error every service's SDK reads its message from (Anthropic's, OpenAI's and Google's shapes at once)."""
    kind = {401: "authentication_error", 402: "budget_exceeded", 403: "permission_error", 404: "not_found_error",
            413: "request_too_large", 429: "rate_limit_error"}.get(status, "api_error")
    return JSONResponse({"type": "error", "error": {"type": kind, "message": message, "code": status,
                                                    "status": "RESOURCE_EXHAUSTED" if status in (402, 429) else kind}},
                        status_code=status, headers=headers)


def cost(price: Dict[str, Any], used: Dict[str, int]) -> float:
    """Dollars for these tokens at this price (per million tokens; the dearer tier once the prompt is past its size)."""
    p = dict(price)
    prompt = used["input"] + used["cache_read"] + used["cache_write"]
    for tier in price.get("tiers") or []:
        if prompt > tier.get("size", 0):
            p.update({k: v for k, v in tier.items() if k != "size"})
    return (used["input"] * p["input"] + used["output"] * p["output"]
            + used["cache_read"] * p.get("cache_read", p["input"])
            + used["cache_write"] * p.get("cache_write", p["input"])) / 1e6 + used.get("searches", 0) * SEARCH_USD


class Meter:
    """The tokens an answer used, read from it as it passes: Anthropic's message_start and message_delta events, the
    usage OpenAI-style services send last (stream_options.include_usage, which the hub asks for), Gemini's
    usageMetadata, or a whole JSON answer."""

    def __init__(self, service: str, streamed: bool):
        self.service, self.streamed = service, streamed
        self.used = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "searches": 0}
        self._line = b""
        self._body = bytearray()

    def feed(self, chunk: bytes) -> None:
        if self.streamed:
            lines = (self._line + chunk).split(b"\n")
            self._line = lines.pop()
            for line in lines:
                self._event(line)
        elif len(self._body) < MAX_METERED:
            self._body += chunk

    def close(self) -> None:
        if self.streamed and self._line:
            self._event(self._line)
            self._line = b""
        elif self._body:
            try:
                self._take(json.loads(bytes(self._body)))
            except ValueError:
                pass

    def _event(self, line: bytes) -> None:
        line = line.strip()
        if line.startswith(b"data:") and line[5:].strip() not in (b"", b"[DONE]"):
            try:
                self._take(json.loads(line[5:].strip()))
            except ValueError:
                pass

    def _set(self, latest: bool = False, **counts: Any) -> None:
        """Anthropic's counts only grow along a stream (the largest seen); Gemini's and OpenAI's last ones are the
        totals (latest), and a prompt's cached part, told only at the end, comes out of what was counted before."""
        for k, v in counts.items():
            if isinstance(v, int) and not isinstance(v, bool):
                self.used[k] = v if latest else max(self.used[k], v)

    def _take(self, obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        if self.service == "anthropic":
            u = (obj.get("message") or {}).get("usage") if obj.get("type") == "message_start" else obj.get("usage")
            if isinstance(u, dict):
                self._set(input=u.get("input_tokens"), output=u.get("output_tokens"),
                          cache_read=u.get("cache_read_input_tokens"), cache_write=u.get("cache_creation_input_tokens"),
                          searches=(u.get("server_tool_use") or {}).get("web_search_requests"))
        elif self.service == "gemini":
            u = obj.get("usageMetadata")
            if isinstance(u, dict):
                cached = u.get("cachedContentTokenCount") or 0
                self._set(True, input=(u.get("promptTokenCount") or 0) - cached, cache_read=cached,
                          output=(u.get("candidatesTokenCount") or 0) + (u.get("thoughtsTokenCount") or 0))
        else:   # OpenAI's Chat Completions and Responses, and services in its format
            done = obj.get("type") == "response.completed"   # the Responses API's last event
            u = (obj.get("response") or {}).get("usage") if done else obj.get("usage")
            if isinstance(u, dict):
                if "prompt_tokens" in u:
                    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                    self._set(True, input=(u.get("prompt_tokens") or 0) - cached, cache_read=cached,
                              output=u.get("completion_tokens"))
                else:
                    cached = (u.get("input_tokens_details") or {}).get("cached_tokens") or 0
                    self._set(True, input=(u.get("input_tokens") or 0) - cached, cache_read=cached,
                              output=u.get("output_tokens"))


class Gateway:
    """The model gateway's state: keys, what is offered, budgets, what each person used, and the answers running."""

    def __init__(self, hub: Any, catalog: Any, transport: Any = None):
        """transport: httpx's way to the services (tests: a pretend service)."""
        self.hub, self.catalog, self.transport = hub, catalog, transport
        self._client: Optional[Tuple[Any, Any]] = None   # (event loop, httpx.AsyncClient)
        self._recent: Dict[int, Deque[float]] = {}        # each person's requests in the last minute
        self._running: Dict[int, Dict[str, float]] = {}    # each person's answers streaming now
        self._lock = threading.Lock()
        with hub.connect() as con:
            con.executescript(TABLES)

    def limits(self) -> Dict[str, Any]:
        own = self.hub.config.get("llm") if isinstance(self.hub.config.get("llm"), dict) else {}
        return dict(LIMITS, **{k: v for k, v in own.items() if k in LIMITS})

    # ------------------------------------------------------------------ keys, sealed with gateway.key

    def _fernet(self) -> Any:
        from cryptography.fernet import Fernet
        path = self.hub.home / "gateway.key"
        if not path.exists():
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        return Fernet(path.read_bytes().strip())

    def set_key(self, service: str, key: Any) -> None:
        key = str(key or "").strip()
        if service not in SERVICES:
            raise ValueError("no service %r" % service)
        if len(key) < 8 or any(c.isspace() for c in key):
            raise ValueError("That doesn't look like an API key.")
        with self.hub.connect() as con:
            con.execute("INSERT OR REPLACE INTO llm_keys (service, sealed, hint, set_at) VALUES (?, ?, ?, ?)",
                        (service, self._fernet().encrypt(key.encode()).decode(), key[-4:], time.time()))
            self.hub.audit(con, "operator", "model key set", None, {"service": SERVICES[service]["name"]})

    def remove_key(self, service: str) -> None:
        with self.hub.connect() as con:
            con.execute("DELETE FROM llm_keys WHERE service = ?", (service,))
            self.hub.audit(con, "operator", "model key removed", None,
                           {"service": SERVICES.get(service, {}).get("name", service)})

    def key(self, service: str) -> Optional[str]:
        with self.hub.connect() as con:
            row = con.execute("SELECT sealed FROM llm_keys WHERE service = ?", (service,)).fetchone()
        if row is None:
            return None
        try:
            return self._fernet().decrypt(row["sealed"].encode()).decode()
        except Exception:   # gateway.key lost or replaced: the owner sets the key again
            log.error("the key for %s can't be unsealed (gateway.key changed?)", service)
            return None

    def keys(self) -> List[Dict[str, Any]]:
        """For the owner console: each service, whether it has a key (its last 4 characters), never the key; the
        models it offers, and who may use it ("": everyone)."""
        with self.hub.connect() as con:
            rows = {r["service"]: r for r in con.execute("SELECT service, hint, set_at FROM llm_keys")}
            offers = {r["service"]: r["models"] for r in con.execute("SELECT service, models FROM llm_offers")}
            access = {r["service"]: r["emails"] for r in con.execute("SELECT service, emails FROM llm_access")}
        return [{"service": s, "name": spec["name"], "set": s in rows, "hint": rows[s]["hint"] if s in rows else "",
                 "set_at": rows[s]["set_at"] if s in rows else None, "models": offers.get(s, ""),
                 "who": access.get(s, "")} for s, spec in SERVICES.items()]

    # ------------------------------------------------------------------ who may use a service

    def set_access(self, service: str, text: Any) -> None:
        """Only these accounts (emails, comma-separated) may use the service; "" lets everyone. For a key that is
        one person's, such as OpenCode Go's single-person plan."""
        if service not in SERVICES:
            raise ValueError("no service %r" % service)
        emails = [e.strip().lower() for e in str(text or "").split(",") if e.strip()]
        bad = [e for e in emails if "@" not in e]
        if bad:
            raise ValueError("Not an email address: %s" % ", ".join(bad))
        with self.hub.connect() as con:
            if emails:
                con.execute("INSERT OR REPLACE INTO llm_access (service, emails) VALUES (?, ?)",
                            (service, ",".join(emails)[:4000]))
            else:
                con.execute("DELETE FROM llm_access WHERE service = ?", (service,))

    def allows(self, service: str, user: Dict[str, Any]) -> bool:
        with self.hub.connect() as con:
            row = con.execute("SELECT emails FROM llm_access WHERE service = ?", (service,)).fetchone()
        return row is None or str(user.get("email") or "").lower() in row["emails"].split(",")

    # ------------------------------------------------------------------ what is offered

    def set_offers(self, service: str, text: Any) -> None:
        if service not in SERVICES:
            raise ValueError("no service %r" % service)
        models = ",".join(m.strip() for m in str(text or "").split(",") if m.strip())[:4000]
        with self.hub.connect() as con:
            if models:
                con.execute("INSERT OR REPLACE INTO llm_offers (service, models) VALUES (?, ?)", (service, models))
            else:
                con.execute("DELETE FROM llm_offers WHERE service = ?", (service,))

    def offers(self, service: str) -> Optional[List[str]]:
        """The models offered on this service; None: every model it lists."""
        with self.hub.connect() as con:
            row = con.execute("SELECT models FROM llm_offers WHERE service = ?", (service,)).fetchone()
        return row["models"].split(",") if row else None

    def offered(self, user: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """The services with a key (that this person may use), for Kelvins: [{id, name, models (None: all)}]."""
        return [{"id": k["service"], "name": k["name"], "models": k["models"].split(",") if k["models"] else None}
                for k in self.keys() if k["set"] and (user is None or not k["who"] or str(user.get("email") or "")
                                                      .lower() in k["who"].split(","))]

    # ------------------------------------------------------------------ budgets and what was used

    def set_budget(self, scope: str, ref: int, usd: Any) -> None:
        """scope "default" (ref 0), "user" or "lab"; usd None or "" takes a person's or lab's own budget away."""
        if scope not in ("default", "user", "lab"):
            raise ValueError("no budget scope %r" % scope)
        text = str(usd if usd is not None else "").strip().lstrip("$")
        with self.hub.connect() as con:
            if text == "" and scope != "default":
                con.execute("DELETE FROM llm_budgets WHERE scope = ? AND ref = ?", (scope, ref))
                return
            try:
                value = float(text)
            except ValueError:
                raise ValueError("A budget is a number of dollars a month.")
            if not 0 <= value <= 1e6:
                raise ValueError("A budget is between $0 and $1,000,000 a month.")
            con.execute("INSERT OR REPLACE INTO llm_budgets (scope, ref, usd) VALUES (?, ?, ?)", (scope, ref, value))

    def _budget(self, con: Any, scope: str, ref: int) -> Optional[float]:
        row = con.execute("SELECT usd FROM llm_budgets WHERE scope = ? AND ref = ?", (scope, ref)).fetchone()
        return row["usd"] if row else None

    def budget_of(self, user_id: int) -> Tuple[float, bool]:
        """(a person's monthly budget in dollars, whether it is their own rather than the default)."""
        with self.hub.connect() as con:
            own = self._budget(con, "user", user_id)
            default = self._budget(con, "default", 0)
        return (own, True) if own is not None else (DEFAULT_BUDGET if default is None else default, False)

    def lab_budget(self, lab_id: Optional[int]) -> Optional[float]:
        if not lab_id:
            return None
        with self.hub.connect() as con:
            return self._budget(con, "lab", lab_id)

    def spent(self, user_id: int, month: Optional[str] = None) -> float:
        with self.hub.connect() as con:
            return con.execute("SELECT COALESCE(SUM(usd), 0) FROM llm_usage WHERE user_id = ? AND month = ?",
                               (user_id, month or month_of())).fetchone()[0]

    def lab_spent(self, lab_id: int, month: Optional[str] = None) -> float:
        with self.hub.connect() as con:
            return con.execute("SELECT COALESCE(SUM(usd), 0) FROM llm_usage WHERE lab_id = ? AND month = ?",
                               (lab_id, month or month_of())).fetchone()[0]

    def usage_of(self, user_id: int, lab_id: Optional[int] = None) -> Dict[str, Any]:
        """A person's month so far, for Kelvin's Settings and the account page."""
        month = month_of()
        budget, own = self.budget_of(user_id)
        out = {"month": month, "spent": round(self.spent(user_id, month), 4), "budget": budget, "own_budget": own,
               "renews": renews(month), "lab": None}
        lab = self.lab_budget(lab_id)
        if lab is not None:
            out["lab"] = {"spent": round(self.lab_spent(lab_id, month), 4), "budget": lab}
        return out

    def over_budget(self, user_id: int, lab_id: Optional[int]) -> Optional[str]:
        """Why a person can't use the models now (None: they can)."""
        month = month_of()
        budget, _ = self.budget_of(user_id)
        if self.spent(user_id, month) >= budget:
            return ("This month's model budget on the Kelvin hub ($%.2f) is used up. It renews on %s; the hub's owner "
                    "can raise it." % (budget, renews(month)))
        lab = self.lab_budget(lab_id)
        if lab is not None and self.lab_spent(lab_id, month) >= lab:
            return ("Your lab's model budget on the Kelvin hub this month ($%.2f) is used up. It renews on %s." % (
                lab, renews(month)))
        return None

    def record(self, user: Dict[str, Any], lab_id: Optional[int], service: str, model: str, used: Dict[str, int],
               status: int) -> float:
        """Keep what one answer used (never its text), and mail the person when it takes them past 80% of their
        budget, once a month. Returns its cost in dollars."""
        price = self.catalog.price(service, model) or self.limits()["fallback_price"]
        usd = cost(price, used) if status < 400 else 0.0
        month = month_of()
        with self.hub.connect() as con:
            con.execute("INSERT INTO llm_usage (at, month, user_id, lab_id, service, model, input, output, cache_read,"
                        " cache_write, usd, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (time.time(), month, user["id"], lab_id, service, model[:120], used["input"], used["output"],
                         used["cache_read"], used["cache_write"], usd, status))
        if usd > 0:
            self._alert(user, month)
        return usd

    def _alert(self, user: Dict[str, Any], month: str) -> None:
        budget, _ = self.budget_of(user["id"])
        spent = self.spent(user["id"], month)
        share = self.limits()["alert_at"]
        if budget <= 0 or spent < share * budget:
            return
        with self.hub.connect() as con:
            fresh = con.execute("INSERT OR IGNORE INTO llm_alerts (scope, ref, month) VALUES ('user', ?, ?)",
                                (user["id"], month)).rowcount
        if fresh:   # the mail goes in its own thread: SMTP can take seconds
            threading.Thread(target=self.hub.mail, daemon=True, args=(
                user["email"], "Kelvin: %d%% of this month's model budget used" % round(100 * spent / budget),
                "Hello %s,\n\nYou have used $%.2f of your $%.2f model budget on the Kelvin hub this month. When it is "
                "used up, Kelvin's hub models stop until %s; the hub's owner can raise it.\n" % (
                    user["name"], spent, budget, renews(month)))).start()

    def report(self, month: Optional[str] = None) -> Dict[str, Any]:
        """For the owner console: the month's total, and by person, model and lab."""
        month = month or month_of()
        with self.hub.connect() as con:
            total = con.execute("SELECT COUNT(*) AS requests, COALESCE(SUM(usd), 0) AS usd, COALESCE(SUM(input +"
                                " cache_read + cache_write), 0) AS input, COALESCE(SUM(output), 0) AS output FROM"
                                " llm_usage WHERE month = ?", (month,)).fetchone()
            people = con.execute(
                "SELECT u.user_id, COALESCE(p.name, '(deleted)') AS name, COALESCE(p.email, '') AS email,"
                " COUNT(*) AS requests, SUM(u.input + u.cache_read + u.cache_write) AS input, SUM(u.output) AS output,"
                " SUM(u.usd) AS usd FROM llm_usage u LEFT JOIN users p ON p.id = u.user_id WHERE u.month = ?"
                " GROUP BY u.user_id ORDER BY usd DESC LIMIT 500", (month,)).fetchall()
            models = con.execute("SELECT service, model, COUNT(*) AS requests, SUM(input + cache_read + cache_write) AS"
                                 " input, SUM(output) AS output, SUM(usd) AS usd FROM llm_usage WHERE month = ?"
                                 " GROUP BY service, model ORDER BY usd DESC", (month,)).fetchall()
            labs = con.execute("SELECT u.lab_id, COALESCE(l.name, '?') AS name, SUM(u.usd) AS usd FROM llm_usage u"
                               " LEFT JOIN labs l ON l.id = u.lab_id WHERE u.month = ? AND u.lab_id IS NOT NULL"
                               " GROUP BY u.lab_id ORDER BY usd DESC", (month,)).fetchall()
            budgets = {(r["scope"], r["ref"]): r["usd"] for r in con.execute("SELECT * FROM llm_budgets")}
        default = budgets.get(("default", 0), DEFAULT_BUDGET)
        return {"month": month, "renews": renews(month), "default_budget": default, "total": dict(total),
                "people": [dict(r, budget=budgets.get(("user", r["user_id"]), default)) for r in people],
                "models": [dict(r) for r in models],
                "labs": [dict(r, budget=budgets.get(("lab", r["lab_id"]))) for r in labs]}

    # ------------------------------------------------------------------ requests per minute, answers at once

    def admit(self, user_id: int) -> Optional[str]:
        """A ticket for one more answer for this person, or None when they have too many going or sent too many
        in the last minute."""
        lim, now = self.limits(), time.time()
        with self._lock:
            recent = self._recent.setdefault(user_id, deque())
            while recent and now - recent[0] > 60:
                recent.popleft()
            running = {t: at for t, at in self._running.get(user_id, {}).items() if now - at < STREAM_S}
            if len(recent) >= lim["per_minute"] or len(running) >= lim["at_once"]:
                return None
            recent.append(now)
            ticket = secrets.token_hex(6)
            running[ticket] = now
            self._running[user_id] = running
            return ticket

    def release(self, user_id: int, ticket: str) -> None:
        with self._lock:
            self._running.get(user_id, {}).pop(ticket, None)

    # ------------------------------------------------------------------ passing a request on

    def _http(self) -> Any:
        import httpx
        loop = asyncio.get_running_loop()
        if self._client is None or self._client[0] is not loop:
            self._client = (loop, httpx.AsyncClient(transport=self.transport, follow_redirects=False,
                                                    timeout=httpx.Timeout(connect=15, read=600, write=120, pool=30)))
        return self._client[1]

    async def close(self) -> None:
        if self._client is not None:
            await self._client[1].aclose()
            self._client = None

    def session_of(self, user: Dict[str, Any]) -> str:
        """A stable session for a person, for a service that routes by one, when their Kelvin sends none: the same
        each time, and not their account number."""
        return "kelvin-" + hashlib.sha256(("%s %s" % (self.hub.base_url, user["id"])).encode()).hexdigest()[:24]

    @staticmethod
    def call_of(service: str, method: str, path: str) -> Optional[Any]:
        """The match for a call the gateway passes on (None: it doesn't)."""
        for verb, pattern in SERVICES[service]["calls"]:
            m = re.fullmatch(pattern, path) if verb == method else None
            if m:
                return m
        return None

    async def forward(self, app: Dict[str, Any], service: str, method: str, path: str, query: List[Tuple[str, str]],
                      headers: Dict[str, str], body: bytes) -> Response:
        """One request from a signed-in Kelvin, passed on to the service with the hub's key; the answer streams back
        as it comes, and what it used is recorded when it ends."""
        from starlette.concurrency import run_in_threadpool as call
        spec = SERVICES.get(service)
        if spec is None:
            return refusal(404, "The Kelvin hub has no model service %r." % service)
        found = self.call_of(service, method, path)
        if found is None:
            return refusal(404, "The Kelvin hub doesn't pass %s %s on to %s." % (method, path, spec["name"]))
        key = await call(self.key, service)
        if not key:
            return refusal(503, "The Kelvin hub has no key for %s yet: the hub's owner adds one in the owner console."
                           % spec["name"])
        if not await call(self.allows, service, app["user"]):
            return refusal(403, "%s on this Kelvin hub is only for some accounts. Pick another model." % spec["name"])
        model, body = self._model_of(service, found, body)
        offers = await call(self.offers, service)
        if model and offers is not None and model not in offers:
            return refusal(403, "The Kelvin hub doesn't offer %s. Pick another model." % model)
        user, lab_id = app["user"], app.get("lab_id")
        ticket = None
        if method == "POST":
            why = await call(self.over_budget, user["id"], lab_id)
            if why:
                return refusal(402, why)
            ticket = self.admit(user["id"])
            if ticket is None:
                return refusal(429, "Too many requests at once from your Kelvin account; wait a moment.",
                               {"retry-after": "10"})
            asyncio.get_running_loop().run_in_executor(None, self.catalog.refresh)   # prices, read once a day
        sent = {k: v for k, v in headers.items() if k in ("content-type", "accept") + tuple(spec["keep"])}
        sent.update({spec["header"]: spec["form"].format(key), "accept-encoding": "identity",
                     "user-agent": "Kelvin-hub/%s" % __version__})
        if "x-opencode-session" in spec["keep"] and not sent.get("x-opencode-session"):
            sent["x-opencode-session"] = self.session_of(user)   # an older Kelvin: one session per person
        client = self._http()
        try:
            answer = await client.send(client.build_request(
                method, "%s/%s" % (spec["upstream"], path), params=[(k, v) for k, v in query if k != "key"],
                headers=sent, content=body or None), stream=True)
        except Exception as e:   # unreachable, refused, timed out: never the request's text in the log
            if ticket:
                self.release(user["id"], ticket)
            log.warning("model gateway: %s %s: %s", service, path, type(e).__name__)
            return refusal(502, "The Kelvin hub couldn't reach %s (%s). Try again in a moment." % (
                spec["name"], type(e).__name__))
        if answer.status_code == 401:   # the owner's key, not the person's sign-in
            await answer.aclose()
            if ticket:
                self.release(user["id"], ticket)
            log.error("model gateway: %s refused the hub's key", service)
            return refusal(502, "%s refused the Kelvin hub's key: tell the hub's owner." % spec["name"])
        kept = {k: answer.headers[k] for k in ("retry-after",) if k in answer.headers}
        kind = answer.headers.get("content-type", "application/json")
        if method == "GET" and offers is not None and path == MODEL_LISTS[service]:
            listed = await answer.aread()
            await answer.aclose()
            return Response(self._only(listed, offers), status_code=answer.status_code, media_type=kind, headers=kept)
        meter = Meter(service, "text/event-stream" in kind)

        async def stream() -> Any:
            try:
                async for chunk in answer.aiter_bytes():
                    meter.feed(chunk)
                    yield chunk
            finally:
                with anyio.CancelScope(shield=True):
                    await answer.aclose()
                if ticket:
                    self.release(user["id"], ticket)
                    meter.close()
                    try:   # a few milliseconds of SQLite, here because a cancelled stream can't await
                        if any(meter.used.values()):   # not a token count or a refusal: they cost nothing
                            self.record(user, lab_id, service, model or "?", meter.used, answer.status_code)
                    except Exception as e:
                        log.error("model gateway: usage not recorded (%s)", type(e).__name__)
        return StreamingResponse(stream(), status_code=answer.status_code, media_type=kind, headers=kept)

    @staticmethod
    def _model_of(service: str, found: Any, body: bytes) -> Tuple[str, bytes]:
        """The model a request asks for; and for an OpenAI-style answer that streams, the request asking for its
        token counts at the end (stream_options.include_usage)."""
        if "model" in found.groupdict():
            return found.group("model"), body
        if not body:
            return "", body
        try:
            request = json.loads(body)
        except ValueError:
            return "", body
        if not isinstance(request, dict):
            return "", body
        model = str(request.get("model") or "")
        chat = service in ("openai", "opencode", "opencode-go") and found.re.pattern.endswith("chat/completions")
        if chat and request.get("stream") is True and not (request.get("stream_options") or {}).get("include_usage"):
            request["stream_options"] = dict(request.get("stream_options") or {}, include_usage=True)
            body = json.dumps(request).encode("utf-8")
        return model, body

    @staticmethod
    def _only(listed: bytes, offers: List[str]) -> bytes:
        """A service's model list with only the models the hub offers."""
        try:
            data = json.loads(listed)
        except ValueError:
            return listed
        for field in ("data", "models"):   # OpenAI's, Anthropic's and OpenCode's; Gemini's
            if isinstance(data, dict) and isinstance(data.get(field), list):
                data[field] = [m for m in data[field] if isinstance(m, dict) and (
                    str(m.get("id") or "") in offers or str(m.get("name") or "").split("/")[-1] in offers)]
        return json.dumps(data).encode("utf-8")
