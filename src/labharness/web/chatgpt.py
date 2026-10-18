"""Sign in with ChatGPT: use the user's ChatGPT plan for ChatGPT models instead of an API key.

This follows OpenAI's guide for open-source and personal local apps (developers.openai.com/siwc):
- The browser opens OpenAI's sign-in page; OpenAI sends the user back to Kelvin's own loopback address
  (http://127.0.0.1:<port>/auth/callback). Kelvin never sees the password.
- The first sign-in registers Kelvin (client_id=dynamic_agent_client); OpenAI issues a client id that is kept.
- PKCE (S256), state and nonce protect the exchange; the ID token's RS256 signature, issuer, audience, expiry and
  nonce are checked against OpenAI's published keys.
- Plan usage is granted only if the token response carries the chatgpt.tokens.use.direct scope.
- Tokens live in ~/Kelvin/chatgpt.json (owner-only where the OS allows), never in the browser or the logs. They are
  refreshed before they expire (one refresh at a time; the refresh token rotates) and revoked on sign-out."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
REVOKE = ISSUER + "/api/accounts/oauth/revoke"
JWKS = ISSUER + "/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
NEW_CLIENT = "dynamic_agent_client"
MANAGE_USAGE = "https://chatgpt.com/settings/usage"
AGENT_NAME = "Kelvin"
PENDING_TTL = 600
REFRESH_EARLY = 120          # seconds before expiry
_SHA256_INFO = bytes.fromhex("3031300d060960864801650304020105000420")   # DigestInfo prefix (RFC 8017)

Http = Callable[[str, str, Optional[Dict[str, str]]], Tuple[int, Any]]


class SignInError(Exception):
    pass


class NotSignedIn(SignInError):
    pass


def _http(method: str, url: str, form: Optional[Dict[str, str]] = None) -> Tuple[int, Any]:
    """(status, parsed JSON or text). Never raises for an HTTP error status."""
    from .._version import __version__
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "User-Agent": "Kelvin/%s" % __version__, "Accept": "application/json",
        **({"Content-Type": "application/x-www-form-urlencoded"} if data is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        body, status = e.read(), e.code
    try:
        return status, json.loads(body) if body else {}
    except ValueError:
        return status, body.decode("utf-8", "replace")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def pkce() -> Tuple[str, str]:
    verifier = b64url(secrets.token_bytes(48))
    return verifier, b64url(hashlib.sha256(verifier.encode()).digest())


def verify_id_token(token: str, client_id: str, nonce: str, jwks: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """The ID token's claims, after checking its RS256 signature against the published keys, the issuer, the
    audience (our client id), the expiry and the nonce. Raises SignInError otherwise."""
    try:
        head_b64, body_b64, sig_b64 = token.split(".")
        header = json.loads(_unb64(head_b64))
        claims = json.loads(_unb64(body_b64))
        signature = _unb64(sig_b64)
    except (ValueError, TypeError):
        raise SignInError("the ID token is malformed")
    if header.get("alg") != "RS256":
        raise SignInError("the ID token is not RS256-signed")
    keys = [k for k in jwks.get("keys", []) if k.get("kty") == "RSA" and (not header.get("kid") or k.get("kid") == header.get("kid"))]
    if not keys:
        raise SignInError("no published key matches the ID token")
    digest = hashlib.sha256(("%s.%s" % (head_b64, body_b64)).encode()).digest()
    if not any(_rsa_ok(k, signature, digest) for k in keys):
        raise SignInError("the ID token's signature does not check out")
    now = time.time() if now is None else now
    aud = claims.get("aud")
    if claims.get("iss") != ISSUER:
        raise SignInError("the ID token is from %r, not OpenAI" % claims.get("iss"))
    if client_id not in (aud if isinstance(aud, list) else [aud]):
        raise SignInError("the ID token was issued to another app")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - 60:
        raise SignInError("the ID token has expired")
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise SignInError("the ID token does not belong to this sign-in")
    return claims


def _rsa_ok(jwk: Dict[str, Any], signature: bytes, digest: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017), using only integer arithmetic."""
    try:
        n = int.from_bytes(_unb64(jwk["n"]), "big")
        e = int.from_bytes(_unb64(jwk["e"]), "big")
    except (KeyError, ValueError):
        return False
    size = (n.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(size, "big")
    tail = _SHA256_INFO + digest
    expected = b"\x00\x01" + b"\xff" * (size - len(tail) - 3) + b"\x00" + tail
    return hmac.compare_digest(em, expected)


class SignIn:
    """The ChatGPT account Kelvin is signed in to, if any."""

    def __init__(self, home: Any, http: Http = _http):
        self.home = Path(home)
        self.path = self.home / "chatgpt.json"
        self.http = http
        self.pending: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._jwks: Optional[Dict[str, Any]] = None
        try:
            self.data: Dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    # ------------------------------------------------------------------ storage

    def _save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def host_id(self) -> str:
        """One stable, opaque id for this installation (sent as ext_agent_host_id)."""
        if not self.data.get("ext_agent_host_id"):
            self.data["ext_agent_host_id"] = "urn:uuid:" + str(uuid.uuid4())
            self._save()
        return self.data["ext_agent_host_id"]

    # ------------------------------------------------------------------ signing in

    def begin(self, redirect_uri: str) -> str:
        """The URL of OpenAI's sign-in page for this attempt."""
        if not redirect_uri.startswith("http://127.0.0.1:"):
            raise SignInError("the callback must be http://127.0.0.1:<port>/... (OpenAI does not accept localhost)")
        now = time.time()
        self.pending = {s: p for s, p in self.pending.items() if now - p["at"] < PENDING_TTL}
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        verifier, challenge = pkce()
        client_id = self.data.get("client_id") or NEW_CLIENT
        params = {"client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri, "scope": SCOPE,
                  "resource": RESOURCE, "state": state, "nonce": nonce, "code_challenge": challenge,
                  "code_challenge_method": "S256", "ext_agent_host_id": self.host_id()}
        if client_id == NEW_CLIENT:
            params["agent_name_hint"] = AGENT_NAME
        if self.data.get("id_token"):
            params["id_token_hint"] = self.data["id_token"]
        self.pending[state] = {"nonce": nonce, "verifier": verifier, "redirect_uri": redirect_uri,
                               "client_id": client_id, "at": now}
        return AUTHORIZE + "?" + urllib.parse.urlencode(params)

    def finish(self, query: Dict[str, str]) -> Dict[str, Any]:
        """Handle OpenAI's redirect back to Kelvin. Returns the new status; raises SignInError."""
        p = self.pending.pop(str(query.get("state") or ""), None)
        if p is None or time.time() - p["at"] > PENDING_TTL:
            raise SignInError("This sign-in link has expired or was already used. Start again from Kelvin's Settings.")
        if query.get("error"):
            raise SignInError("Sign-in was cancelled." if query["error"] == "access_denied"
                              else "OpenAI said: %s" % (query.get("error_description") or query["error"]))
        issued = str(query.get("client_id") or p["client_id"])
        if issued == NEW_CLIENT or (p["client_id"] != NEW_CLIENT and issued != p["client_id"]):
            raise SignInError("OpenAI returned an unexpected app registration; start again.")
        status, tok = self.http("POST", TOKEN, {"grant_type": "authorization_code", "client_id": issued,
                                                "code": str(query.get("code") or ""), "code_verifier": p["verifier"],
                                                "redirect_uri": p["redirect_uri"], "resource": RESOURCE})
        if status != 200 or not isinstance(tok, dict) or not tok.get("access_token"):
            detail = (tok.get("error_description") or tok.get("error")) if isinstance(tok, dict) else str(tok)[:200]
            raise SignInError("OpenAI did not complete the sign-in: %s" % detail)
        claims = verify_id_token(str(tok.get("id_token") or ""), issued, p["nonce"], self.jwks())
        scopes = str(tok.get("scope") or query.get("scope") or "").split()
        with self._lock:
            self.data.update({"email": claims.get("email"), "issuer": ISSUER, "subject": claims.get("sub"),
                              "client_id": issued, "id_token": tok["id_token"], "access_token": tok["access_token"],
                              "refresh_token": tok.get("refresh_token"), "token_type": tok.get("token_type", "Bearer"),
                              "expires_at": time.time() + float(tok.get("expires_in") or 3600), "scopes": scopes,
                              "saved_at": time.time()})
            self._save()
        return self.status()

    def jwks(self) -> Dict[str, Any]:
        if self._jwks is None:
            status, keys = self.http("GET", JWKS, None)
            if status != 200 or not isinstance(keys, dict):
                raise SignInError("could not fetch OpenAI's signing keys")
            self._jwks = keys
        return self._jwks

    # ------------------------------------------------------------------ using it

    def status(self) -> Dict[str, Any]:
        signed_in = bool(self.data.get("access_token"))
        return {"signed_in": signed_in, "email": self.data.get("email") if signed_in else None,
                "plan": signed_in and PLAN_SCOPE in (self.data.get("scopes") or []),
                "welcomed": bool(self.data.get("welcomed")), "manage_usage": MANAGE_USAGE}

    def access_token(self) -> str:
        """A current access token, refreshed first if it is about to expire."""
        with self._lock:   # one refresh at a time: the refresh token rotates
            if not self.data.get("access_token"):
                raise NotSignedIn("Kelvin is not signed in with ChatGPT")
            if float(self.data.get("expires_at") or 0) - time.time() < REFRESH_EARLY:
                self._refresh()
            return self.data["access_token"]

    def _refresh(self) -> None:
        if not self.data.get("refresh_token"):
            self._forget()
            raise NotSignedIn("The ChatGPT sign-in expired. Sign in again in Settings.")
        status, tok = self.http("POST", TOKEN, {"grant_type": "refresh_token", "client_id": self.data["client_id"],
                                                "refresh_token": self.data["refresh_token"], "resource": RESOURCE})
        if status != 200 or not isinstance(tok, dict) or not tok.get("access_token"):
            if isinstance(tok, dict) and tok.get("error") in ("invalid_grant", "invalid_client"):
                self._forget()
                raise NotSignedIn("The ChatGPT sign-in is no longer valid. Sign in again in Settings.")
            raise SignInError("Could not renew the ChatGPT sign-in right now (%s); try again in a moment." % status)
        self.data["access_token"] = tok["access_token"]
        self.data["refresh_token"] = tok.get("refresh_token") or self.data["refresh_token"]
        self.data["expires_at"] = time.time() + float(tok.get("expires_in") or 3600)
        if tok.get("scope"):
            self.data["scopes"] = str(tok["scope"]).split()
        if tok.get("id_token"):
            self.data["id_token"] = tok["id_token"]
        self._save()

    def mark_welcomed(self) -> None:
        self.data["welcomed"] = True
        self._save()

    def _forget(self) -> None:
        """Drop the tokens; keep the app registration, host id and ID token (for id_token_hint) as OpenAI suggests."""
        for k in ("access_token", "refresh_token", "expires_at", "scopes", "token_type"):
            self.data.pop(k, None)
        self._save()

    def sign_out(self) -> bool:
        """Revoke the refresh token and forget the tokens. True if OpenAI confirmed the revocation."""
        with self._lock:
            confirmed = False
            token = self.data.get("refresh_token")
            if token:
                try:
                    status, _ = self.http("POST", REVOKE, {"token": token, "token_type_hint": "refresh_token",
                                                           "client_id": self.data.get("client_id", "")})
                    confirmed = status == 200
                except Exception:
                    confirmed = False
            self._forget()
            return confirmed
