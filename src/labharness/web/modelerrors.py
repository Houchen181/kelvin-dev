"""Why a model service turned Kelvin down, in plain words (F3 #25: every failure said "add its API key").

A key that is missing, wrong or revoked, or expired; no credit left on the account; a rate or plan limit; the service
overloaded, down or out of reach; a model it doesn't offer: each is said as what it is, with what to do about it and
the service's own words where they add something. A key is never repeated, not even the masked part some services
echo back."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# where each service's keys and credit are looked after
CONSOLES = {"anthropic": "console.anthropic.com", "openai": "platform.openai.com", "gemini": "aistudio.google.com"}
SETTINGS = "Settings → Models"

CREDIT = re.compile(r"credit balance|insufficient[_ ](?:credit|balance|funds|quota)|exceeded your (?:current )?quota|"
                    r"purchase credits|billing|payment required|out of credits?|no credits? left|spending limit", re.I)
EXPIRED = re.compile(r"\bexpired?\b|\brenew\b", re.I)
MISSING = re.compile(r"no api key|api key (?:is )?(?:missing|required)|x-api-key header is required|"
                     r"missing (?:an )?(?:api key|authentication|bearer)|didn't provide an api key", re.I)
KEYISH = re.compile(r"api[ _-]?key|x-api-key|authenticat|unauthori[sz]ed|bearer token|access token|credential", re.I)
WRONG = re.compile(r"not valid|invalid|incorrect|wrong|revoked|disabled", re.I)
AUTH = re.compile(r"authentication_?error|api_key_invalid|invalid_api_key|unauthenticated", re.I)
SIGNED_OUT = re.compile(r"not logged in|/login\b|log ?in again|sign ?in again|not signed in|invalid api key", re.I)
PLAN_LIMIT = re.compile(r"usage limit|hit your limit|limit reached|limit will reset|resets? (?:at|in)\b", re.I)
TOO_LONG = re.compile(r"prompt is too long|context (?:length|window)|too many tokens|maximum context|too large", re.I)
# what a service may echo of a key: never shown again
_KEYLIKE = re.compile(r"\b(?:sk|pk|rk|sess|AIza)[-_A-Za-z0-9*.]{6,}|\b[A-Za-z0-9_-]*\*{3,}[A-Za-z0-9_-]*")


def _status(e: BaseException) -> Optional[int]:
    for name in ("status_code", "status", "code"):   # Anthropic and OpenAI: status_code; Google: code
        v = getattr(e, name, None)
        if isinstance(v, int) and not isinstance(v, bool) and 100 <= v < 600:
            return v
    v = getattr(getattr(e, "response", None), "status_code", None)
    return v if isinstance(v, int) else None


def _body_error(e: BaseException) -> Dict[str, Any]:
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return err
        return {"message": err} if isinstance(err, str) else body
    return {}


def _said(e: BaseException) -> str:
    """The service's own words, their first line, with anything like a key taken out."""
    err = _body_error(e)
    text = err.get("message") if isinstance(err.get("message"), str) else ""
    if not text:
        msg = getattr(e, "message", None)
        text = msg if isinstance(msg, str) and not msg.startswith("Error code:") else ""
    text = re.sub(r"^Error code: \d+ - ", "", (text or str(e)).strip())
    first = text.splitlines()[0] if text else type(e).__name__
    return _KEYLIKE.sub("…", first)[:300]


def _kinds(e: BaseException) -> str:
    """What names the kind of failure: the exception's class, the service's error type and code, a Google status
    and its reasons, and the service's words."""
    err = _body_error(e)
    parts: List[str] = [type(e).__name__]
    for value in (getattr(e, "type", None), getattr(e, "code", None), getattr(e, "status", None),
                  err.get("type"), err.get("code"), err.get("status")):
        if isinstance(value, str):
            parts.append(value)
    details = getattr(e, "details", None)   # Google's: the whole answer, {"error": {"details": [...]}}
    if isinstance(details, dict):
        inner = details.get("error")
        details = inner.get("details") if isinstance(inner, dict) else None
    for d in details if isinstance(details, list) else err.get("details") or []:
        if isinstance(d, dict) and isinstance(d.get("reason"), str):
            parts.append(d["reason"])
    return " ".join(parts + [_said(e)])


def _console(service: str) -> str:
    return " (%s)" % CONSOLES[service] if service in CONSOLES else ""


def describe(label: str, service: str, e: BaseException) -> str:
    """What went wrong with a model service, for the person. label: how the portal names the service ("Claude");
    service: its key in model keys ("anthropic")."""
    status, kinds, said, cls = _status(e), _kinds(e), _said(e), type(e).__name__
    if "Timeout" in cls or isinstance(e, TimeoutError):
        return ("%s took too long to answer. Try again; if it keeps happening, the service may be slow right now."
                % label)
    if "Connection" in cls or "ConnectError" in cls or isinstance(e, ConnectionError):
        return ("Kelvin couldn't reach %s: check this computer's internet connection, or a proxy or firewall that may "
                "block it." % label)
    if service == "claudecode":   # Claude Code says why in its own result text (claudecode.failure)
        if CREDIT.search(kinds):
            return ("Your Claude account has no credit left (Claude Code says: %s). Add credit or check the plan, or "
                    "pick another model meanwhile." % said)
        if PLAN_LIMIT.search(kinds):
            return ("Your Claude plan's usage limit is reached (Claude Code says: %s). Pick another model until it "
                    "resets." % said)
        if EXPIRED.search(kinds):
            return "Claude Code's sign-in on this computer has expired: run claude in a terminal and sign in again."
        if SIGNED_OUT.search(kinds):
            return ("Claude Code on this computer isn't signed in: run claude in a terminal and sign in (%s shows "
                    "whether it is)." % SETTINGS)
        if isinstance(e, FileNotFoundError):
            return "Kelvin can't find the claude program: give its path under %s → Claude Code." % SETTINGS
    if status == 402 or re.search(r"billing_error|insufficient_quota|insufficient_balance", kinds, re.I) \
            or CREDIT.search(said):
        return ("%s says this account has no credit or quota left (%s). Add credit or check the plan%s, or pick "
                "another model meanwhile." % (label, said, _console(service)))
    if status == 413 or (status in (400, None) and TOO_LONG.search(said)):
        return "The conversation is too long for this %s model (%s). Start a new chat, or attach less." % (label, said)
    if status == 401 or AUTH.search(kinds) or (status in (400, None) and KEYISH.search(said) and (
            WRONG.search(said) or EXPIRED.search(said) or MISSING.search(said))):
        if EXPIRED.search(said):
            return "%s says the API key has expired. Make a new key%s and put it in %s." % (
                label, _console(service), SETTINGS)
        if MISSING.search(said):
            return "%s got no API key. Add one in %s." % (label, SETTINGS)
        return "%s rejected the API key: it is wrong, or it was revoked. Copy it again%s into %s." % (
            label, " from " + CONSOLES[service] if service in CONSOLES else "", SETTINGS)
    if status == 403 or re.search(r"permission_error|permission_denied|PermissionDenied|forbidden", kinds, re.I):
        return "%s refused: this key isn't allowed to do that (%s)." % (label, said)
    if status == 404 or re.search(r"not_found_error|NotFound", kinds):
        return "%s doesn't offer that model to this key, or not under that name (%s). Pick another model." % (
            label, said)
    if status == 429 or re.search(r"rate_limit|RateLimit|RESOURCE_EXHAUSTED", kinds):
        if PLAN_LIMIT.search(said) and not re.search(r"rate limit", said, re.I):
            return "Your %s plan's usage limit is reached (%s). Pick another model until it resets." % (label, said)
        return "%s is limiting how fast this key may ask (a rate limit). Wait a minute and try again." % label
    if status in (503, 529) or re.search(r"overloaded", kinds, re.I):
        return ("%s is overloaded right now; nothing is wrong with your key. Try again in a minute, or pick another "
                "model." % label)
    if status is not None and status >= 500:
        return "%s had a problem on its side (error %d); nothing is wrong with your key. Try again shortly." % (
            label, status)
    return "%s error: %s" % (label, said)


def unavailable(key: str, providers: Dict[str, Any]) -> str:
    """Why a model key can't be used here, before anything is asked: no model chosen, a service without its key or
    sign-in, or a name Kelvin doesn't know."""
    key = str(key or "").strip()
    if not key:
        return "No model is chosen: pick one under the message box, or set a default in %s." % SETTINGS
    service, colon, model = key.partition(":")
    provider = providers.get(service) if colon else None
    if provider is not None:
        if not model:
            return "Say which %s model: pick one from the list under the message box." % provider.label
        if service in ("anthropic", "openai", "gemini"):
            return "%s has no API key here: add one in %s." % (provider.label, SETTINGS)
        return "%s isn't set up here: see %s." % (provider.label, SETTINGS)
    if service == "claudecode":
        return ("Claude Code isn't available here: it is switched off in %s, or Kelvin can't find the claude "
                "program." % SETTINGS)
    if service == "chatgpt":
        return "You aren't signed in with ChatGPT: %s → Continue with ChatGPT." % SETTINGS
    if not colon:
        return ("There's no model called %r here. Pick one from the list under the message box (Claude Sonnet is "
                "anthropic:claude-sonnet-5-5 with a Claude API key, or claudecode:sonnet through Claude Code)." % key)
    return "There's no model service called %r here: pick a model from the list under the message box." % service
