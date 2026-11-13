"""The hub's web server: pages for people (register, sign in, their shared work and computers, export, delete), the
API that Kelvins use, and the owner console, where the Kelvin team also reads what people's Kelvins shared.

    kelvin-hub --home <folder>                  # serve on http://127.0.0.1:8780
    kelvin-hub backup | operator-key | stats --home <folder>

Kelvin shares over the person's own sign-in token (POST /api/signin): its chats and run records, once they switch
it on. Older Kelvins connect through the browser instead, like Kelvin's Sign in with ChatGPT: Kelvin opens
<hub>/connect with a PKCE challenge and its loopback callback (http://127.0.0.1:<port>/...); the person signs in and
says yes; the hub sends the browser back with a one-use code; Kelvin trades the code and its verifier for a token at
POST /api/token, and keeps the token in the person's own (encrypted) settings.

The pages need no JavaScript. Every response forbids framing and inline scripts; forms carry an anti-forgery value
and the hub refuses posts from other sites."""
from __future__ import annotations

import argparse
import asyncio
import csv
import functools
import hmac
import html
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import anyio
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool as call
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .._version import __version__
from . import drawing, llm
from . import relay as relaying
from .catalog import Catalog
from .db import (CONSENT_VERSION, DEFAULTS, NEW_TERMS, NO_RUN_FILES, SESSION_TTL, Busy, Hub, HubError, NeedsConsent,
                 TooMuch,
                 installation, loopback, log, previous_ids)

COOKIE = "kelvin_hub"
OPERATOR_COOKIE = "kelvin_hub_operator"
OPERATOR_TTL = 12 * 3600
MAX_FORM = 64 * 1024               # bytes in a form post
MAX_TOKEN = 16 * 1024              # bytes in a token trade
MAX_SYNC = 64 * 1024 * 1024        # bytes in one sync
REMOVALS_ONLY = 256 * 1024         # bytes: a sync that only removes is smaller (500 ids of each kind)
CUT = 20_000                       # characters of a tool's result, a call's input or a model's thinking on a page
TEXT_SHOWN = 100_000               # characters of one message's words on a page
LATEST = 50                        # shared chats on the owner console's first page
EXPORT_EVERY = 600                 # seconds between one person's exports
CONFIG_EVERY = 30                  # seconds between looks at hub.json, taken in again when it changed
AT_ONCE = 3                        # one person's syncs and uploads in flight; more get a 429 at once, on the event loop
HEAVY_THREADS = 16                 # worker threads syncs and uploads may hold across the hub (of 40): pages keep the rest
CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
LOGO = Path(__file__).resolve().parent.parent / "web" / "static" / "kelvin.svg"
SECURITY_HEADERS = [
    (b"content-security-policy", b"default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
                                 b"form-action 'self' http://127.0.0.1:*; frame-ancestors 'none'; base-uri 'none'"),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),   # not no-referrer: browsers would then send "Origin: null" on our own forms
    (b"cache-control", b"no-store"),
]
NOTES = {
    "sent": "We sent you a new confirmation link.",
    "confirm": "Welcome! We sent a link to confirm your email address.",
    "password": "Your password is changed.",
    "profile": "Saved.",
    "lab": "Lab made. Pass its join code to your labmates so they can join.",
    "joined": "You joined the lab.",
    "left": "You left the lab.",
    "revoked": "That Kelvin is disconnected.",
}

CSS = """
:root{--navy:#1b3a5c;--ice:#2f6fb0;--frost:#e8f2fc;--snow:#8cc4f2;--ink:#1f2328;--muted:#59636e;--line:#d0d7de;
--bg:#fff;--card:#f6f8fa;--bad:#b42318}
@media (prefers-color-scheme:dark){:root{--ink:#e6edf3;--muted:#9da7b3;--line:#30363d;--bg:#0d1117;--card:#161b22;
--frost:#132a42;--ice:#6cb0f0;--bad:#ff7b72}}
*{box-sizing:border-box}body{margin:0;font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);
background:var(--bg)}header{background:var(--navy);color:#fff;padding:.6rem 1rem;display:flex;gap:1rem;
align-items:center;flex-wrap:wrap}header a{color:#fff;text-decoration:none}.brand{display:flex;gap:.5rem;
align-items:center;font-weight:600}.brand img{width:28px;height:28px}header nav{margin-left:auto;display:flex;
gap:1rem;align-items:center;flex-wrap:wrap}header button{border:1px solid #fff;background:transparent;padding:.2rem .7rem}
main{max-width:48rem;margin:0 auto;padding:1.5rem 1rem 4rem}h1{font-size:1.5rem;margin:.2rem 0 1rem}
h2{font-size:1.15rem;margin:2.2rem 0 .6rem}a{color:var(--ice)}.muted{color:var(--muted);font-size:.92rem}
form.stack{display:grid;gap:.8rem;max-width:28rem}label{display:grid;gap:.25rem;font-weight:500}
input[type=text],input[type=email],input[type=password],input[type=search],select{font:inherit;padding:.5rem .6rem;
border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);width:100%}
label.check{display:flex;gap:.5rem;align-items:flex-start;font-weight:400}label.check input{margin-top:.35rem}
button{font:inherit;padding:.5rem 1rem;border-radius:6px;border:1px solid var(--ice);background:var(--ice);color:#fff;
cursor:pointer}button.plain{background:transparent;color:var(--ice)}button.danger{background:var(--bad);
border-color:var(--bad)}a.button{display:inline-block;padding:.5rem 1rem;border-radius:6px;
border:1px solid var(--ice);background:var(--ice);color:#fff;text-decoration:none}a.button.plain{
background:transparent;color:var(--ice)}.note{background:var(--frost);border-radius:8px;padding:.8rem 1rem;margin:1rem 0}
.error{border-left:4px solid var(--bad);background:var(--card);padding:.6rem 1rem;margin:1rem 0}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:.95rem}th,td{text-align:left;
padding:.4rem .5rem;border-bottom:1px solid var(--line);vertical-align:top}th{font-weight:600}
.row{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}form.inline{display:inline;margin:0}
code{background:var(--card);padding:.1rem .35rem;border-radius:4px;word-break:break-all}
pre{background:var(--card);padding:.7rem;border-radius:6px;overflow-x:auto;font-size:.85rem}
.msg{border:1px solid var(--line);border-radius:8px;padding:.6rem .8rem;margin:.6rem 0;white-space:pre-wrap;
overflow-wrap:anywhere}.msg.user{background:var(--frost)}.who{font-size:.8rem;color:var(--muted);font-weight:600;
text-transform:uppercase;letter-spacing:.03em}details{margin:.3rem 0}summary{cursor:pointer;color:var(--muted)}
ul.plain{padding-left:1.1rem}
"""

# The terms and privacy notice while Kelvins share work here (hub.json sharing, the default). Their parts depend on the
# hub's other switches (labs, run_files): terms_text() and privacy_text() put them together. Changing those switches
# changes what people agreed to, so raise consent_version with them. Beside the box that agrees (Kelvin's screens,
# the hub's own Create account and consent page) nothing more: these say it all, the terms what changed and when
# ({changed}: CONSENT_VERSION, when sharing came; since Kelvin 0.3.51 it is off unless switched on, which shares less
# than the terms of that version said, so nobody had to agree again). Final since 2026-11-13, for the App Store: the
# privacy notice covers Kelvin for iPhone too, and {contact} is hub.json contact, whom to write to (left out unset).
TERMS = """<p><b>Changed on {changed}:</b> Kelvin can share your work here, if you switch sharing on.</p>
<ul class="plain">
<li>The hub keeps your Kelvin account, so the same email and password sign you in to Kelvin on any computer and in
Kelvin for iPhone.</li>
<li><b>Kelvin shares your work here only if you switch sharing on,</b> in Kelvin's Settings → Account: your normal
projects, your unfiled chats with their whole conversations, and {runs}.
The Kelvin team, who run the hub, can read what is shared, to help you and to improve Kelvin.</li>
<li><b>Switching it off:</b> in Kelvin, Settings → Account. Kelvin then stops sending, and removes from the hub what
it shared.</li>
<li>Share only work you are allowed to share. Mark unpublished or confidential projects as such in Kelvin, or use an
incognito chat: the hub never receives them.</li>
{labs}<li>The hub's models (Claude, GPT, Gemini, OpenCode with the Kelvin team's keys) come with a monthly budget. The
Kelvin team decides which models are offered and how large budgets are, and may change both.</li>
<li>The Kelvin team may disable an account that abuses the hub.</li>
<li>The hub is provided as it is, without warranty. Keep your own copies of your data; Kelvin keeps them on your
computer anyway.</li>
</ul>{contact}"""

PRIVACY = """<p>This notice covers the Kelvin hub and the apps that use it: Kelvin on your computer and Kelvin for
iPhone. The Kelvin team runs the hub.{contact}</p>
<ul class="plain">
<li><b>What stays with you:</b> Kelvin keeps your chats, projects, runs and data on your computer, and Kelvin for
iPhone keeps its settings and sign-in on the phone. Only what this notice names reaches the hub.</li>
<li><b>What the hub keeps:</b> your name, email, optional ORCID iD, a hash of your password (never the password), the
computers and phones you sign in to Kelvin on (their name, operating system and Kelvin version, and when), and what
your Kelvin shares if you switch sharing on: normal projects, unfiled chats with their whole conversations, and
{runs}.</li>
<li><b>What never reaches it:</b> incognito chats; unpublished, confidential or model-limited projects, with their
chats and runs; {files}API keys; and ChatGPT or Claude Code sign-ins.</li>
<li><b>Who sees your shared work:</b> {who}</li>
<li><b>Switching it off:</b> in Kelvin, Settings → Account. Kelvin stops sending, and removes from the hub what it
shared. A chat or run you delete in Kelvin goes from the hub too.</li>
{phone}{models}<li><b>PDF pages to draw:</b> when your chat's model sees pictures but can't read PDFs, Kelvin sends the hub the
pages it needs (never from incognito chats or unpublished, confidential or model-limited projects) and gets them back
as pictures. The hub keeps nothing of them.</li>
<li><b>The hub's models:</b> when your chat uses one of them (Claude, GPT, Gemini or OpenCode with the Kelvin team's
keys), your messages pass through the hub to that model's service: Anthropic, OpenAI, Google or OpenCode. Of these
calls the hub keeps who used which model, how many tokens and what it cost, never the text. Confidential projects
never use them. The Kelvin team uses only services that protect your messages at least as well as this notice
does.</li>
<li><b>Records:</b> the hub logs sign-ins, connections and changes, with the IP address, for security; and each time
the Kelvin team opens one of your chats or runs, which your <b>Export</b> shows.</li>
<li><b>Your rights:</b> download everything with <b>Export</b>, and delete your account and all it shared with
<b>Delete account</b>, both on your account page; Kelvin for iPhone deletes it too (Computers → Kelvin account).
Daily database backups are kept for up to 30 days, then removed.</li>
<li><b>Changes:</b> if this notice changes what reaches the hub, Kelvin asks you to agree again before anything new
goes.</li>
</ul>"""

RUNS_SHARED = ("the records of your finished runs (plan, status, settings, results and assessment), never their data "
               "files")
RUNS_SHARED_WITH_FILES = ("your finished runs: their records (plan, status, settings, results and assessment) and "
                          "their data files")
LABS_TERMS = ("<li>Labs have no owner: anyone with a lab's join code can join it and see what its members share "
              "there.</li>\n")
WHO_SEES = ("you, and the Kelvin team, who run the hub. They read it to help you and to improve Kelvin. No other user "
            "sees it.")
WHO_SEES_LABS = ("you; the members of the lab your Kelvin shares into, while you are a member too (leaving a lab takes "
                 "your work out of its view); and the Kelvin team, who run the hub, who read it to help you and to "
                 "improve Kelvin.")
PHONE_PRIVACY = """<li><b>Your phone, through the hub:</b> away from the lab's network, Kelvin for iPhone reaches
your Kelvin through the hub. The phone's requests and Kelvin's answers pass through it as they happen, and the hub
keeps none of them. Incognito chats and unpublished, confidential or model-limited projects never go this way. On
the lab's network or through Tailscale, the phone reaches your computer directly. Kelvin's Settings → Account
switches this off.</li>
"""
MODELS_PRIVACY = """<li><b>The AI models your chats use:</b> to answer, Kelvin sends a chat's messages, with what it
needs from your lab, to the company whose model the chat uses: through the hub for the hub's models, or straight
from your computer for a model you set up yourself, under your own key or plan with that company. A model server of
your own keeps them on your network. Kelvin for iPhone names the company and asks before a chat's first message goes
to it.</li>
"""


def mail_link(contact: str) -> str:
    return '<a href="mailto:%s">%s</a>' % (html.escape(contact, quote=True), html.escape(contact))


def terms_contact(contact: str) -> str:
    """The terms' last line: whom to ask (hub.json contact), or nothing while it is unset."""
    return "\n<p>Questions about these terms: %s.</p>" % mail_link(contact) if contact else ""


def privacy_contact(contact: str) -> str:
    """The end of the privacy notice's first paragraph: whom to write to (hub.json contact), or nothing while unset."""
    return " Questions, and requests about your data: %s." % mail_link(contact) if contact else ""


def terms_text(labs: bool, run_files: bool, contact: str = "") -> str:
    """The built-in terms while Kelvins share work here."""
    return TERMS.replace("{runs}", RUNS_SHARED_WITH_FILES if run_files else RUNS_SHARED) \
        .replace("{labs}", LABS_TERMS if labs else "").replace("{changed}", CONSENT_VERSION) \
        .replace("{contact}", terms_contact(contact))


def privacy_text(labs: bool, run_files: bool, contact: str = "") -> str:
    """The built-in privacy notice while Kelvins share work here."""
    return PRIVACY.replace("{runs}", RUNS_SHARED_WITH_FILES if run_files else RUNS_SHARED) \
        .replace("{files}", "" if run_files else "runs' data files (run.h5, plots, reports); ") \
        .replace("{who}", WHO_SEES_LABS if labs else WHO_SEES).replace("{phone}", PHONE_PRIVACY) \
        .replace("{models}", MODELS_PRIVACY).replace("{contact}", privacy_contact(contact))


TERMS_ACCOUNTS = """<ul class="plain">
<li>The hub keeps your Kelvin account, so the same email and password sign you in to Kelvin on any computer and in
Kelvin for iPhone.</li>
<li>Your work stays on your computers: the hub keeps no chats, projects, runs or data. Chats with the hub's
models pass through it to the model's service, and PDF pages Kelvin asks it to draw as pictures; it keeps none of
their text. Confidential projects never use the hub's models.</li>
<li>The hub's models (Claude, GPT, Gemini, OpenCode with the Kelvin team's keys) come with a monthly budget. The
Kelvin team decides which models are offered and how large budgets are, and may change both.</li>
<li>The Kelvin team runs the hub and may disable an account that abuses it.</li>
<li>The hub is provided as it is, without warranty.</li>
</ul>{contact}"""

PRIVACY_ACCOUNTS = """<p>This notice covers the Kelvin hub and the apps that use it: Kelvin on your computer and
Kelvin for iPhone. The Kelvin team runs the hub.{contact}</p>
<ul class="plain">
<li><b>What the hub keeps:</b> your name, email, optional ORCID iD, a hash of your password (never the password), and
the computers and phones you sign in to Kelvin on: their name, operating system and Kelvin version, and when.</li>
<li><b>What it never keeps:</b> your work (chats, projects, runs, data), which stays on your computers. Your API
keys and ChatGPT or Claude Code sign-ins never reach it.</li>
{phone}{models}<li><b>The hub's models:</b> when your chat uses one of them (Claude, GPT, Gemini or OpenCode with the Kelvin
team's keys), your messages pass through the hub to that model's service: Anthropic, OpenAI, Google or OpenCode. The
hub keeps who used which model, how many tokens and what it cost, never the text. Confidential projects never use
them. The Kelvin team uses only services that protect your messages at least as well as this notice does.</li>
<li><b>PDF pages to draw:</b> when your chat's model sees pictures but can't read PDFs, Kelvin sends the hub the
pages it needs (never from incognito chats or unpublished, confidential or model-limited projects) and gets them back
as pictures. The hub keeps nothing of them.</li>
<li><b>Who sees your account:</b> you, and the Kelvin team, who run the hub. No other user does.</li>
<li><b>Records:</b> the hub logs sign-ins and changes, with the IP address, for security.</li>
<li><b>Your rights:</b> download your account with <b>Export</b>, and delete it with <b>Delete account</b>, both on
your account page; Kelvin for iPhone deletes it too (Computers → Kelvin account). Daily database backups are kept for
up to 30 days, then removed.</li>
<li><b>Changes:</b> if this notice changes what reaches the hub, Kelvin asks you to agree again before anything new
goes.</li>
</ul>"""


def terms_accounts_text(contact: str = "") -> str:
    """The built-in terms while the hub keeps Kelvin accounts only (hub.json sharing off)."""
    return TERMS_ACCOUNTS.replace("{contact}", terms_contact(contact))


def privacy_accounts_text(contact: str = "") -> str:
    """The built-in privacy notice while the hub keeps Kelvin accounts only."""
    return PRIVACY_ACCOUNTS.replace("{phone}", PHONE_PRIVACY).replace("{models}", MODELS_PRIVACY) \
        .replace("{contact}", privacy_contact(contact))

PAGE_SIZE = 200   # people per page on the operator page
STAT_NAMES = {"kelvins": "computers", "connections": "signed in on computers", "active_30d": "active in 30 days"}


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def when(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts))) if ts else "–"
    except (TypeError, ValueError, OverflowError, OSError):   # nan, 1e300, a negative time on Windows...
        return "–"


def size(n: Any) -> str:
    n = float(n or 0)
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024
    return ""


def cut(value: Any, limit: int = CUT) -> str:
    """Escaped text for a page, cut past limit with a note of how much more there is. Other values are shown as
    JSON."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, indent=1, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return e(text)
    return e(text[:limit]) + '<span class="muted"> … %s more characters not shown</span>' % "{:,}".format(
        len(text) - limit)


def named(items: Any) -> List[str]:
    """The files a chat names (attachments, pictures, PDFs): each one's name and kind, or its kind alone."""
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            name, kind, pages = str(item.get("name") or ""), str(item.get("mime") or ""), item.get("pages")
            words = name or kind or "a file"
            if name and kind:
                words += " (%s%s)" % (kind, ", %s pages" % pages if pages else "")
            out.append(words)
        elif item:
            out.append(str(item))
    return out


def render_chat(messages: Any, depth: int = 0) -> str:
    """A stored chat, read-only: each turn's words, tool calls and their results folded and cut, pictures and files
    named but never shown. Everything is escaped: a chat's text is the person's, the models' and the tools', never
    the hub's own. The chat format is Kelvin's (web/providers/base.py)."""
    if not isinstance(messages, list) or not messages:
        return '<p class="muted">No turns to show.</p>'
    return "".join(render_message(m, depth) for m in messages)


def render_message(m: Any, depth: int = 0) -> str:
    if not isinstance(m, dict):
        return '<div class="msg">%s</div>' % cut(m, 2000)
    role = m.get("role", "")
    if role == "user":
        who = "Kelvin, coming back to the chat" if m.get("wake") else \
            "Person, while Kelvin worked" if m.get("steer") else "Person"
        parts = [cut(m.get("text") if m.get("text") is not None else m.get("content"), TEXT_SHOWN)]
        events = [t for t in m.get("events") or [] if t] if isinstance(m.get("events"), list) else []
        if events:
            parts.append('<details><summary>What changed in the lab meanwhile (%d)</summary><pre>%s</pre></details>'
                         % (len(events), cut("\n".join(str(t) for t in events))))
        files = named(m.get("attachments"))
        if files:
            parts.append('<div class="muted">Attached, not shown here: %s</div>' % e("; ".join(files)))
        compare = m.get("compare")
        if isinstance(compare, dict) and isinstance(compare.get("lanes"), list) and depth == 0:
            chosen = compare.get("chosen")
            for i, lane in enumerate(compare["lanes"]):   # the lane the chat went on with follows in the chat itself
                if i != chosen and isinstance(lane, dict):
                    parts.append('<details><summary>%s answered too%s</summary>%s</details>' % (
                        e(lane.get("model") or "Another model"),
                        " (%s)" % e(lane["error"]) if lane.get("error") else "",
                        render_chat(lane.get("messages"), depth + 1)))
        return '<div class="msg user"><div class="who">%s</div>%s</div>' % (e(who), "".join(parts))
    if role == "assistant":
        parts = []
        for b in m.get("blocks") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                parts.append(cut(b.get("text"), TEXT_SHOWN))
            elif b.get("type") == "thinking":
                parts.append('<details><summary>Thinking</summary><pre>%s</pre></details>' % cut(b.get("text")))
            elif b.get("type") == "tool_call":
                parts.append('<details><summary>Calls %s</summary><pre>%s</pre></details>'
                             % (e(b.get("name") or "a tool"), cut(b.get("input"), 4000)))
        return '<div class="msg"><div class="who">Kelvin%s</div>%s</div>' % (
            " · " + e(m["model"]) if m.get("model") else "", "".join(parts))
    if role == "tool":
        out = []
        for r in m.get("results") or []:
            if not isinstance(r, dict):
                continue
            also = []
            pictures = r.get("images") if isinstance(r.get("images"), list) else []
            if pictures:
                also.append("%d picture%s" % (len(pictures), "" if len(pictures) == 1 else "s"))
            also += ["PDF " + d for d in named(r.get("documents"))]
            out.append('<details><summary>%s result%s%s</summary><pre>%s</pre></details>' % (
                e(r.get("name") or "a tool"), " (error)" if r.get("is_error") else "",
                ' <span class="muted">· %s, not shown here</span>' % e("; ".join(also)) if also else "",
                cut(r.get("content"))))
        return "".join(out)
    return '<div class="msg">%s</div>' % cut(m, 2000)


def shown_time(value: Any) -> str:
    """A time from a run's record: a number as a date, text as it is."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return when(value)
    return e(str(value)[:40]) if value not in (None, "") else "–"


def record_rows(meta: Any) -> str:
    """A run's record laid out as a table: plain values as text, lists and objects as JSON, all escaped."""
    if not isinstance(meta, dict) or not meta:
        return '<p class="muted">The record is empty.</p>'
    rows = []
    for key, value in meta.items():
        shown = '<pre>%s</pre>' % cut(value, 4000) if isinstance(value, (dict, list)) else cut(value, 2000)
        rows.append('<tr><th>%s</th><td>%s</td></tr>' % (e(key), shown))
    return '<div class="scroll"><table>%s</table></div>' % "".join(rows)


FOUND = (("results", "Results"), ("assessment", "Assessment"), ("issues", "Issues"))


def found_rows(card: Any) -> str:
    """What a run found, from the card Kelvin sends in its record (meta's "card": rundata.card): its results,
    assessment and issues first, then the rest of the card (what it swept and measured...), all escaped."""
    if not isinstance(card, dict) or not card:
        return '<p class="muted">Kelvin sent no results with this run.</p>'
    rows = []
    for key, label in FOUND:
        value = card.get(key)
        if isinstance(value, list) and value:
            rows.append('<tr><th>%s</th><td>%s</td></tr>' % (label, "<br>".join(cut(v, 2000) for v in value[:20])))
        elif value not in (None, "", [], {}):
            rows.append('<tr><th>%s</th><td>%s</td></tr>' % (label, cut(value, 4000)))
    rest = {k: v for k, v in card.items() if k not in dict(FOUND)}
    return ('<div class="scroll"><table>%s</table></div>' % "".join(rows) if rows else "") + (
        record_rows(rest) if rest else "")


async def read_body(request: Request, limit: int) -> bytes:
    """The request's body, refused as soon as it would pass limit: by its Content-Length, or while it streams in."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        raise TooMuch("That is more than the hub takes at once.")
    chunks, got = [], 0
    async for chunk in request.stream():
        got += len(chunk)
        if got > limit:
            raise TooMuch("That is more than the hub takes at once.")
        chunks.append(chunk)
    return b"".join(chunks)


def net_of(ip: str) -> str:
    """The key for limits: an IPv4 address, or an IPv6 address's /64 (one household or server gets a whole /64)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6 and addr.ipv4_mapped:   # ::ffff:1.2.3.4 is 1.2.3.4
        return str(addr.ipv4_mapped)
    return str(ipaddress.ip_network("%s/64" % addr, strict=False)) if addr.version == 6 else str(addr)


def safe_next(value: Any, default: str = "/account") -> str:
    """Only a path on this hub, never another site."""
    text = str(value or "")
    return text if text.startswith("/") and not text.startswith(("//", "/\\")) else default


def cells(row: List[Any]) -> List[Any]:
    """A row of the owner's CSV downloads that a spreadsheet only shows: text starting like a formula (= + - @, a tab
    or a return) gets a ' in front. People choose their names and emails, and a Kelvin says what version it is."""
    return ["'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v for v in row]


class Limiter:
    """Too many failed tries from one key (an address, an email) in a window: refuse for a while."""

    def __init__(self, tries: int, window: float):
        self.tries, self.window = tries, window
        self.seen: Dict[str, List[float]] = {}

    def blocked(self, key: str) -> bool:
        now = time.time()
        times = [t for t in self.seen.get(key, ()) if now - t < self.window]
        if times:
            self.seen[key] = times
        else:
            self.seen.pop(key, None)
        return len(times) >= self.tries

    def hit(self, key: str) -> None:
        now = time.time()
        if len(self.seen) > 50_000:   # forget only keys whose tries have all expired
            self.seen = {k: v for k, v in self.seen.items() if v and now - v[-1] < self.window}
        self.seen.setdefault(key, []).append(now)

    def clear(self, key: str) -> None:
        self.seen.pop(key, None)


class Gate:
    """Only the hub's own addresses (no DNS rebinding), no posts from other sites, and safety headers on everything."""

    def __init__(self, app: Any, hosts: Iterable[str]):
        self.app = app
        self.hosts = set(hosts)

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        problem = None
        if headers.get("host", "") not in self.hosts:
            problem = "bad host"
        elif scope.get("method", "GET") not in ("GET", "HEAD", "OPTIONS"):
            origin = headers.get("origin")
            if origin is not None and urlsplit(origin).netloc not in self.hosts:
                problem = "bad origin"
        if problem:
            await Response(problem, status_code=403, media_type="text/plain")(scope, receive, send)
            return

        async def secured(message: Dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                have = {k.lower() for k, _ in message.get("headers", [])}
                message = dict(message, headers=list(message.get("headers", []))
                               + [(k, v) for k, v in SECURITY_HEADERS if k not in have])
            await send(message)

        await self.app(scope, receive, secured)


def create_app(hub: Hub, port: int = 8780, hosts: Optional[Iterable[str]] = None, backups: bool = True,
               llm_transport: Any = None) -> Any:
    """llm_transport: how the model gateway reaches the model services (httpx; tests pass a pretend service)."""
    if hosts is None:
        hosts = {urlsplit(hub.base_url).netloc, "127.0.0.1:%d" % port, "localhost:%d" % port}
    signin_tries = Limiter(10, 900)
    spray_tries = Limiter(30, 3600)       # wrong passwords for one email from anywhere (Kelvin's sign-in)
    form_tries = Limiter(20, 3600)        # registrations and reset mails per address
    token_tries = Limiter(30, 900)
    operator_tries = Limiter(5, 900)
    operators: Dict[str, Dict[str, Any]] = {}   # operator sessions, in memory: a restart asks for the key again
    exported: Dict[int, float] = {}            # when each person last exported
    in_flight: Dict[int, int] = {}             # each person's syncs, uploads and drawings running now
    heavy_limit: Dict[str, Any] = {}           # the limiter for syncs and uploads, made on the event loop
    catalog = Catalog(hub.home)                # models.dev, once a day: which models see pictures, their prices
    gateway = llm.Gateway(hub, catalog, llm_transport)   # the model gateway (llm.py)

    # ------------------------------------------------------------------ helpers

    def ip_of(request: Request) -> Optional[str]:
        """The client's address. client_ip_header (e.g. CF-Connecting-IP) counts only when the request came from a
        proxy on this computer (cloudflared), and then its last value, the one that proxy added."""
        peer = request.client.host if request.client else None
        header = hub.config.get("client_ip_header")
        if header and peer in ("127.0.0.1", "::1") and request.headers.get(header):
            return request.headers[header].split(",")[-1].strip()[:64]
        return peer

    def who(request: Request) -> str:
        return net_of(ip_of(request) or "?")

    def secure(request: Request) -> bool:
        return hub.base_url.startswith("https://") and request.headers.get("host") == urlsplit(hub.base_url).netloc

    async def form_of(request: Request) -> Dict[str, str]:
        body = await read_body(request, MAX_FORM)
        return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}

    async def session_of(request: Request) -> Optional[Dict[str, Any]]:
        token = request.cookies.get(COOKIE)
        return await call(hub.session, token) if token else None

    def here(request: Request) -> str:
        return request.url.path + ("?" + request.url.query if request.url.query else "")

    async def signed_in(request: Request, any_consent: bool = False) -> Tuple[Optional[Dict[str, Any]], Optional[Response]]:
        sess = await session_of(request)
        if sess is None:
            return None, RedirectResponse("/signin?" + urlencode({"next": here(request)}), 303)
        if not any_consent and hub.needs_consent(sess["user"]):
            return None, RedirectResponse("/consent?" + urlencode({"next": here(request)}), 303)
        return sess, None

    def check_csrf(sess: Dict[str, Any], form: Dict[str, str]) -> None:
        if not hmac.compare_digest(form.get("csrf", ""), sess["csrf"]):
            raise HubError("This form has expired. Reload the page and try again.")

    def csrf_field(sess: Optional[Dict[str, Any]]) -> str:
        return '<input type="hidden" name="csrf" value="%s">' % e(sess["csrf"]) if sess else ""

    def begin_session(response: Response, request: Request, user_id: int) -> Response:
        token, _ = hub.new_session(user_id)
        response.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax", secure=secure(request))
        return response

    def page(title: str, body: str, sess: Optional[Dict[str, Any]] = None, status: int = 200,
             error: Optional[str] = None, note: Optional[str] = None, nav: Optional[str] = None) -> HTMLResponse:
        if nav is not None:
            pass   # the owner console's own
        elif sess:
            nav = ('<a href="/account">%s</a><form class="inline" method="post" action="/signout">%s'
                   '<button>Sign out</button></form>' % (e(sess["user"]["name"]), csrf_field(sess)))
        else:
            nav = '<a href="/signin">Sign in</a><a href="/register">Create account</a>'
        flash = ('<div class="error" role="alert">%s</div>' % e(error) if error else "") + \
                ('<div class="note" role="status">%s</div>' % e(note) if note else "")
        return HTMLResponse(
            '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width, initial-scale=1"><title>%s · Kelvin hub</title><link rel="icon" '
            'href="/kelvin.svg"><style>%s</style></head><body><header><a class="brand" href="/"><img src="/kelvin.svg" '
            'alt="">Kelvin hub</a><nav>%s</nav></header><main><h1>%s</h1>%s%s</main></body></html>'
            % (e(title), CSS, nav, e(title), flash, body), status_code=status)

    def api_error(message: str, status: int = 400, **extra: Any) -> JSONResponse:
        return JSONResponse(dict(extra, error=message), status_code=status)

    def take_turn(user_id: int) -> bool:
        """On the event loop: may one more sync or upload of this person start? (Their requests wait for each other
        in worker threads, so a flood of them must not take all the threads from everyone else.)"""
        if in_flight.get(user_id, 0) >= AT_ONCE:
            return False
        in_flight[user_id] = in_flight.get(user_id, 0) + 1
        return True

    def end_turn(user_id: int) -> None:
        in_flight[user_id] -= 1
        if in_flight[user_id] <= 0:
            del in_flight[user_id]

    async def heavy(func: Any, *args: Any) -> Any:
        """A sync's or an upload's work (parsing included) in a worker thread, at most HEAVY_THREADS of them across the
        whole hub: many people syncing at once wait their turn on the event loop and never take the threads that
        sign-ins and pages need. Never on the event loop itself, where a 64 MB parse would stop every request."""
        if "limiter" not in heavy_limit:
            heavy_limit["limiter"] = anyio.CapacityLimiter(HEAVY_THREADS)
        return await anyio.to_thread.run_sync(functools.partial(func, *args), limiter=heavy_limit["limiter"])

    async def app_of(request: Request) -> Optional[Dict[str, Any]]:
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        return await call(hub.app, token) if token else None

    # ------------------------------------------------------------------ public pages

    def sharing_only(endpoint: Any) -> Any:
        """Shared work and connected Kelvins: not found while the hub keeps accounts only (hub.json sharing)."""
        async def wrapped(request: Request) -> Response:
            if hub.sharing:
                return await endpoint(request)
            if request.url.path.startswith("/api/"):
                return api_error("not found", 404)
            return page("Not found", "<p>This hub keeps Kelvin accounts only.</p>", await session_of(request), 404)
        return wrapped

    def labs_only(endpoint: Any) -> Any:
        """Labs: not found unless the hub has them (hub.json labs, with sharing)."""
        async def wrapped(request: Request) -> Response:
            if hub.labs:
                return await endpoint(request)
            return page("Not found", "<p>This hub has no labs.</p>", await session_of(request), 404)
        return wrapped

    def what_goes() -> str:
        """What Kelvin shares here, in a phrase, for the pages."""
        return ("your normal projects and unfiled chats with their whole conversations, and %s"
                % (RUNS_SHARED_WITH_FILES if hub.run_files else RUNS_SHARED))

    async def home(request: Request) -> Response:
        sess = await session_of(request)
        if sess:
            return RedirectResponse("/account", 303)
        links = '<p class="muted"><a href="/terms">Terms</a> · <a href="/privacy">Privacy</a></p>'
        if not hub.sharing:
            return page("Your Kelvin account", (
                '<p>One email and password sign you in to Kelvin on every computer. You can make your account in '
                'Kelvin itself, on its sign-in screen. Here you can change your name or password, export your account '
                'or delete it.</p><p>Your work stays on your computers: the hub keeps your account only.</p>'
                '<p class="row"><a class="button" href="/signin">Sign in</a>'
                '<a class="button plain" href="/register">Create account</a></p>' + links))
        if not hub.labs:
            return page("Your Kelvin account", (
                '<p>One email and password sign you in to Kelvin on every computer. You can make your account in '
                'Kelvin itself, on its sign-in screen.</p>'
                '<p>If you switch sharing on in Kelvin\'s Settings → Account, Kelvin shares with the Kelvin team here: '
                '%s. Incognito chats, unpublished and confidential projects, API keys and sign-ins never leave '
                'your computer.</p><p class="row"><a class="button" href="/signin">Sign in</a>'
                '<a class="button plain" href="/register">Create account</a></p>' % e(what_goes())) + links)
        return page("One account for Kelvin, in every lab", (
            '<p>Make one account and join your lab. If you switch sharing on in Kelvin\'s Settings → Account, Kelvin '
            'then shares with your labmates and the Kelvin team here: %s.</p>'
            '<p>Unpublished and confidential projects, incognito chats, API keys and sign-ins never leave your '
            'computer.</p><p class="row"><a class="button" href="/register">Create account</a>'
            '<a class="button plain" href="/signin">Sign in</a></p>' % e(what_goes())) + links)

    async def logo(request: Request) -> Response:
        return FileResponse(LOGO, media_type="image/svg+xml") if LOGO.exists() else Response(status_code=404)

    async def terms(request: Request) -> Response:
        custom = hub.home / "terms.html"
        return page("Terms", custom.read_text(encoding="utf-8") if custom.exists() else
                    terms_text(hub.labs, hub.run_files, hub.contact) if hub.sharing else
                    terms_accounts_text(hub.contact), await session_of(request))

    async def privacy(request: Request) -> Response:
        custom = hub.home / "privacy.html"
        return page("Privacy", custom.read_text(encoding="utf-8") if custom.exists() else
                    privacy_text(hub.labs, hub.run_files, hub.contact) if hub.sharing else
                    privacy_accounts_text(hub.contact), await session_of(request))

    def register_form(values: Dict[str, str], error: Optional[str] = None) -> HTMLResponse:
        return page("Create your account", (
            '<form class="stack" method="post" action="/register">'
            '<input type="hidden" name="next" value="%s">'
            '<label>Name<input type="text" name="name" autocomplete="name" required maxlength="80" value="%s"></label>'
            '<label>Email<input type="email" name="email" autocomplete="email" required value="%s"></label>'
            '<label>ORCID iD <span class="muted">(optional)</span><input type="text" name="orcid" '
            'placeholder="0000-0002-1825-0097" value="%s"></label>'
            '<label>Password <span class="muted">(8 characters or more)</span><input type="password" '
            'name="password" autocomplete="new-password" required minlength="8"></label>'
            '<label class="check"><input type="checkbox" name="consent" value="yes" required><span>I agree to the '
            '<a href="/terms">terms</a> and the <a href="/privacy">privacy notice</a>.</span></label>'
            '<button>Create account</button></form>'
            '<p class="muted">Already have one? <a href="/signin">Sign in</a>.</p>'
            % (e(values.get("next", "")), e(values.get("name")), e(values.get("email")), e(values.get("orcid")))),
            error=error, status=400 if error else 200)

    async def register(request: Request) -> Response:
        if request.method == "GET":
            return register_form({"next": request.query_params.get("next", "")})
        f = await form_of(request)
        ip = ip_of(request) or "?"
        if form_tries.blocked("register:" + who(request)):
            return register_form(f, "Too many new accounts from this address. Try again later.")
        try:
            user = await call(hub.register, f.get("email"), f.get("name"), f.get("password"),
                              f.get("consent") == "yes", f.get("orcid"), ip)
        except HubError as err:
            return register_form(f, str(err))
        form_tries.hit("register:" + who(request))
        await call(hub.send_confirmation, user)
        target = safe_next(f.get("next"), "/account?note=confirm")
        return await call(begin_session, RedirectResponse(target, 303), request, user["id"])

    def signin_form(values: Dict[str, str], error: Optional[str] = None) -> HTMLResponse:
        return page("Sign in", (
            '<form class="stack" method="post" action="/signin"><input type="hidden" name="next" value="%s">'
            '<label>Email<input type="email" name="email" autocomplete="email" required value="%s"></label>'
            '<label>Password<input type="password" name="password" autocomplete="current-password" required></label>'
            '<button>Sign in</button></form><p class="muted"><a href="/reset">Forgot your password?</a> · '
            '<a href="/register%s">Create an account</a></p>'
            % (e(values.get("next", "")), e(values.get("email")),
               e("?" + urlencode({"next": values["next"]}) if values.get("next") else ""))),
            error=error, status=400 if error else 200)

    async def signin(request: Request) -> Response:
        if request.method == "GET":
            return signin_form({"next": request.query_params.get("next", "")})
        f = await form_of(request)
        keys = ("signin:" + f.get("email", "").strip().lower(), "signin-ip:" + who(request))
        if any(signin_tries.blocked(k) for k in keys):
            return signin_form(f, "Too many tries. Wait 15 minutes, or reset your password.")
        try:
            user = await call(hub.authenticate, f.get("email"), f.get("password"), ip_of(request))
        except HubError as err:
            for k in keys:
                signin_tries.hit(k)
            return signin_form(f, str(err))
        signin_tries.clear(keys[0])
        return await call(begin_session, RedirectResponse(safe_next(f.get("next")), 303), request, user["id"])

    async def signout(request: Request) -> Response:
        sess = await session_of(request)
        f = await form_of(request)
        if sess:
            check_csrf(sess, f)
            await call(hub.end_session, request.cookies.get(COOKIE))
        response = RedirectResponse(safe_next(f.get("next"), "/"), 303)
        response.delete_cookie(COOKIE)
        return response

    async def consent(request: Request) -> Response:
        sess, redirect = await signed_in(request, any_consent=True)
        if redirect:
            return redirect
        target = safe_next(request.query_params.get("next"))
        if request.method == "POST":
            f = await form_of(request)
            check_csrf(sess, f)
            if f.get("consent") == "yes":
                await call(hub.give_consent, sess["user"]["id"], ip_of(request))
                return RedirectResponse(safe_next(f.get("next")), 303)
        return page("The terms have changed", (
            '<p>Please read the <a href="/terms">terms</a> and the <a href="/privacy">privacy notice</a> '
            '(version %s), then agree to keep using the hub. If you don\'t agree, you can '
            '<a href="/account/export">export your data</a> and <a href="/account/delete">delete your account</a>.</p>'
            '<form class="stack" method="post" action="/consent">%s<input type="hidden" name="next" value="%s">'
            '<label class="check"><input type="checkbox" name="consent" value="yes" required><span>I agree.</span>'
            '</label><button>Continue</button></form>'
            % (e(hub.consent_version), csrf_field(sess), e(target))), sess)

    async def confirm(request: Request) -> Response:
        sess = await session_of(request)
        try:
            await call(hub.confirm_email, request.query_params.get("code"))
        except HubError as err:
            return page("Confirm your email", '<p><a href="/account">Your account</a> can send a new link.</p>', sess,
                        400, error=str(err))
        return page("Email confirmed", '<p>Thanks. <a href="/account">Go to your account</a>.</p>', sess)

    async def reset(request: Request) -> Response:
        if request.method == "GET":
            return page("Reset your password", (
                '<form class="stack" method="post" action="/reset"><label>Email<input type="email" name="email" '
                'autocomplete="email" required></label><button>Send me a link</button></form>'))
        f = await form_of(request)
        ip = ip_of(request) or "?"
        outbox = "" if (hub.config.get("mail") or {}).get("smtp_host") else (
            ' <span class="muted">(This hub has no mail server yet, so the Kelvin team has to pass the link on.)</span>')
        response = page("Check your email", '<p>If there is an account for that address, we sent it a link to choose '
                                            'a new password. It works for an hour.%s</p>' % outbox)
        if not form_tries.blocked("reset:" + who(request)):
            form_tries.hit("reset:" + who(request))
            # after the answer is sent, so its timing says nothing about whether the account exists
            response.background = BackgroundTask(hub.start_reset, f.get("email"), ip)
        return response

    async def reset_new(request: Request) -> Response:
        if request.method == "GET":
            return page("Choose a new password", (
                '<form class="stack" method="post" action="/reset/new"><input type="hidden" name="code" value="%s">'
                '<label>New password <span class="muted">(8 characters or more)</span><input type="password" '
                'name="password" autocomplete="new-password" required minlength="8"></label>'
                '<button>Save and sign in</button></form>' % e(request.query_params.get("code", ""))))
        f = await form_of(request)
        try:
            user = await call(hub.finish_reset, f.get("code"), f.get("password"), ip_of(request))
        except HubError as err:
            return page("Choose a new password", '<p><a href="/reset">Ask for a new link</a>.</p>', status=400,
                        error=str(err))
        return await call(begin_session, RedirectResponse("/account?note=password", 303), request, user["id"])

    # ------------------------------------------------------------------ the account page

    async def account(request: Request, error: Optional[str] = None) -> Response:
        sess, redirect = await signed_in(request)
        if redirect:
            return redirect
        user = sess["user"]
        uid = user["id"]
        csrf = csrf_field(sess)
        parts = []
        if not user["email_confirmed_at"]:
            parts.append('<div class="note">Please confirm <b>%s</b> with the link we sent. '
                         '<form class="inline" method="post" action="/account/confirm">%s<button class="plain">'
                         'Send it again</button></form></div>' % (e(user["email"]), csrf))
        if hub.sharing:
            await sharing_sections(parts, uid, csrf)
        offered, usage = await asyncio.gather(call(gateway.offered), call(gateway.usage_of, uid))
        if offered:   # the model gateway (llm.py): what this person used of their budget
            parts.append('<h2>Models through Kelvin</h2><p>$%.2f of your $%.2f this month (%s). It renews on %s.</p>'
                         % (usage["spent"], usage["budget"], e(", ".join(s["name"] for s in offered)),
                            e(usage["renews"])))
        parts.append(
            '<h2>Your profile</h2><form class="stack" method="post" action="/account/profile">%s'
            '<label>Email<input type="email" value="%s" disabled></label>'
            '<label>Name<input type="text" name="name" required maxlength="80" value="%s"></label>'
            '<label>ORCID iD <span class="muted">(optional)</span><input type="text" name="orcid" value="%s"></label>'
            '<button>Save</button></form>'
            '<h2>Password</h2><form class="stack" method="post" action="/account/password">%s'
            '<label>Current password<input type="password" name="old" autocomplete="current-password" required></label>'
            '<label>New password<input type="password" name="new" autocomplete="new-password" required minlength="8">'
            '</label><button>Change password</button></form>'
            '<h2>Your data</h2><p><a href="/account/export">Export everything</a> <span class="muted">(a zip of %s)'
            '</span></p>'
            '<details><summary>Delete your account</summary><form class="stack" method="post" '
            'action="/account/delete">%s<p>%s It can\'t be undone.</p><label>Password<input type="password" '
            'name="password" autocomplete="current-password" required></label><label class="check"><input '
            'type="checkbox" name="sure" value="yes" required><span>Yes, delete my account.</span></label>'
            '<button class="danger">Delete my account</button></form></details>'
            '<p class="muted">Account made %s · agreed to the terms of %s · <a href="/terms">Terms</a> · '
            '<a href="/privacy">Privacy</a></p>'
            % (csrf, e(user["email"]), e(user["name"]), e(user["orcid"] or ""), csrf,
               ("your account, %sthe computers you sign in on, all you shared, and the hub's record of your account"
                % ("labs, " if hub.labs else "")) if hub.sharing else "your account and the hub's record of it", csrf,
               "This deletes your account and everything your Kelvins shared to the hub. Your own computers keep "
               "their copies." if hub.sharing else "This deletes your Kelvin account. Your computers keep your chats "
               "and data.", when(user["created_at"]), e(user["consent_version"])))
        note = NOTES.get(request.query_params.get("note", ""))
        return page("Your account", "".join(parts), sess, error=error, note=note, status=400 if error else 200)

    async def sharing_sections(parts: List[str], uid: int, csrf: str) -> None:
        """The account page's shared work and Kelvins (only while the hub shares work), with labs if it has them."""
        if not hub.labs:
            await own_sections(parts, uid, csrf)
            return
        labs, connections, shared = await asyncio.gather(
            call(hub.labs_of, uid), call(hub.connections, uid), call(hub.shared_with, uid))
        parts.append('<h2>Shared in your labs</h2>')
        if shared["chats"] or shared["runs"] or shared["projects"]:
            if shared["projects"]:
                parts.append('<div class="scroll"><table><tr><th>Project</th><th>By</th><th>Lab</th><th>Updated</th>'
                             '</tr>%s</table></div>' % "".join(
                                 '<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                                 % (e(p["name"]), e(p["owner"]), e(p["lab"] or "only you"), when(p["updated_at"]))
                                 for p in shared["projects"]))
            if shared["chats"]:
                parts.append('<div class="scroll"><table><tr><th>Chat</th><th>By</th><th>Project</th><th>Lab</th>'
                             '<th>Updated</th></tr>%s</table></div>' % "".join(
                                 '<tr><td><a href="/shared/chats/%d">%s</a></td><td>%s</td><td>%s</td><td>%s</td>'
                                 '<td>%s</td></tr>' % (c["id"], e(c["title"]), e(c["owner"]), e(c["project"] or "–"),
                                                       e(c["lab"] or "only you"), when(c["updated_at"]))
                                 for c in shared["chats"]))
            if shared["runs"]:
                parts.append('<div class="scroll"><table><tr><th>Run</th><th>Device</th><th>By</th><th>Project</th>'
                             '<th>Lab</th><th>Size</th><th>Started</th></tr>%s</table></div>' % "".join(
                                 '<tr><td><a href="/shared/runs/%d">%s</a></td><td>%s</td><td>%s</td><td>%s</td>'
                                 '<td>%s</td><td>%s</td><td>%s</td></tr>'
                                 % (r["id"], e(r["local_id"]), e(r["device"] or "–"), e(r["owner"]),
                                    e(r["project"] or "–"), e(r["lab"] or "only you"), size(r["bytes"]),
                                    when(r["created_at"])) for r in shared["runs"]))
        else:
            parts.append('<p class="muted">Nothing yet. Once you connect a Kelvin, the work it shares, and what your '
                         'labmates share, shows here.</p>')

        parts.append('<h2>Your labs</h2>')
        if labs:
            parts.append('<div class="scroll"><table><tr><th>Lab</th><th>People</th><th>Join code</th><th></th></tr>'
                         '%s</table></div>' % "".join(
                             '<tr><td><b>%s</b><br><span class="muted">%s</span></td><td>%s</td><td><code>%s</code>'
                             '</td><td><form class="inline" method="post" action="/labs/%d/leave">%s<button '
                             'class="plain">Leave</button></form></td></tr>'
                             % (e(lab["name"]), e(lab["institution"]), e(", ".join(lab["people"])), e(lab["join_code"]),
                                lab["id"], csrf) for lab in labs))
        else:
            parts.append('<p class="muted">You are in no lab yet. Join yours with the code a labmate gives you, or '
                         'make it.</p>')
        parts.append(
            '<div class="row" style="align-items:flex-start;gap:2rem">'
            '<form class="stack" method="post" action="/labs/join">%s<label>Join a lab<input type="text" '
            'name="code" placeholder="ABCD-EFGH-JKLM" required></label><button>Join</button></form>'
            '<form class="stack" method="post" action="/labs">%s<label>Make a lab<input type="text" name="name" '
            'placeholder="Name, e.g. Smith Lab" required maxlength="80"></label><label>Institution '
            '<span class="muted">(optional)</span><input type="text" name="institution" maxlength="80"></label>'
            '<button>Make lab</button></form></div>'
            '<p class="muted">Labs have no owner: everyone in a lab sees its join code and what its members share '
            'there.</p>' % (csrf, csrf))

        parts.append('<h2>Connected Kelvins</h2>')
        if connections:
            parts.append('<div class="scroll"><table><tr><th>Kelvin</th><th>Shares into</th><th>Last seen</th><th>'
                         '</th></tr>%s</table></div>' % "".join(
                             '<tr><td>%s<br><span class="muted">%s %s</span></td><td>%s</td><td>%s</td><td><form '
                             'class="inline" method="post" action="/connections/%d/revoke">%s<button class="plain">'
                             'Disconnect</button></form></td></tr>'
                             % (e(c["name"] or "Kelvin"), e(c["platform"]), e(c["version"]), e(c["lab"] or "only you"),
                                when(c["last_used"]), c["id"], csrf) for c in connections))
        else:
            parts.append('<p class="muted">None yet. Start the connection from the Kelvin you want to connect; it '
                         'opens this hub in your browser to ask you.</p>')

    async def own_sections(parts: List[str], uid: int, csrf: str) -> None:
        """The account page while labs are off: what this person's Kelvins shared, which only they and the Kelvin
        team see, and the Kelvins they sign in to."""
        connections, shared = await asyncio.gather(call(hub.connections, uid), call(hub.shared_with, uid))
        parts.append('<h2>Shared with the Kelvin team</h2><p class="muted">If you switch sharing on in Kelvin\'s '
                     'Settings → Account, Kelvin shares here: %s. Only you and the Kelvin team, who run the hub, see '
                     'it.</p>' % e(what_goes()))
        if shared["projects"]:
            parts.append('<div class="scroll"><table><tr><th>Project</th><th>Updated</th></tr>%s</table></div>'
                         % "".join('<tr><td>%s</td><td>%s</td></tr>' % (e(p["name"]), when(p["updated_at"]))
                                   for p in shared["projects"]))
        if shared["chats"]:
            parts.append('<div class="scroll"><table><tr><th>Chat</th><th>Project</th><th>Updated</th></tr>%s</table>'
                         '</div>' % "".join(
                             '<tr><td><a href="/shared/chats/%d">%s</a></td><td>%s</td><td>%s</td></tr>'
                             % (c["id"], e(c["title"]), e(c["project"] or "–"), when(c["updated_at"]))
                             for c in shared["chats"]))
        if shared["runs"]:
            parts.append('<div class="scroll"><table><tr><th>Run</th><th>Device</th><th>Project</th><th>Started</th>'
                         '</tr>%s</table></div>' % "".join(
                             '<tr><td><a href="/shared/runs/%d">%s</a></td><td>%s</td><td>%s</td><td>%s</td></tr>'
                             % (r["id"], e(r["local_id"]), e(r["device"] or "–"), e(r["project"] or "–"),
                                when(r["created_at"])) for r in shared["runs"]))
        if not (shared["projects"] or shared["chats"] or shared["runs"]):
            parts.append('<p class="muted">Nothing yet. What your Kelvin shares shows here.</p>')
        parts.append('<h2>Your Kelvins</h2>')
        if connections:
            parts.append('<div class="scroll"><table><tr><th>Kelvin</th><th>Last used</th><th></th></tr>%s</table>'
                         '</div>' % "".join(
                             '<tr><td>%s<br><span class="muted">%s %s</span></td><td>%s</td><td><form class="inline" '
                             'method="post" action="/connections/%d/revoke">%s<button class="plain">Disconnect</button>'
                             '</form></td></tr>' % (e(c["name"] or "Kelvin"), e(c["platform"]), e(c["version"]),
                                                    when(c["last_used"]), c["id"], csrf) for c in connections))
        else:
            parts.append('<p class="muted">None yet. Sign in to Kelvin with this account and it shows here.</p>')

    def account_action(fn: Any, any_consent: bool = False) -> Any:
        """A form on the account page: signed in, anti-forgery checked; a HubError shows on the page."""
        async def endpoint(request: Request) -> Response:
            sess, redirect = await signed_in(request, any_consent)
            if redirect:
                return redirect
            f = await form_of(request)
            try:
                check_csrf(sess, f)
                return await fn(request, sess, f)
            except HubError as err:
                request.scope["query_string"] = b""
                if any_consent and hub.needs_consent(sess["user"]):
                    return delete_form(sess, str(err))
                return await account(request, error=str(err))
        return endpoint

    async def resend(request: Request, sess: Dict[str, Any], f: Dict[str, str]) -> Response:
        await call(hub.send_confirmation, sess["user"])
        return RedirectResponse("/account?note=sent", 303)

    async def profile(request: Request, sess: Dict[str, Any], f: Dict[str, str]) -> Response:
        await call(hub.set_profile, sess["user"]["id"], f.get("name"), f.get("orcid"))
        return RedirectResponse("/account?note=profile", 303)

    async def password(request: Request, sess: Dict[str, Any], f: Dict[str, str]) -> Response:
        await call(hub.change_password, sess["user"]["id"], f.get("old"), f.get("new"), request.cookies.get(COOKIE),
                   ip_of(request))
        return RedirectResponse("/account?note=password", 303)

    async def make_lab(request: Request, sess: Dict[str, Any], f: Dict[str, str]) -> Response:
        await call(hub.create_lab, sess["user"]["id"], f.get("name"), f.get("institution"))
        return RedirectResponse("/account?note=lab", 303)

    async def join_lab(request: Request, sess: Dict[str, Any], f: Dict[str, str]) -> Response:
        await call(hub.join_lab, sess["user"]["id"], f.get("code"))
        return RedirectResponse("/account?note=joined", 303)

    async def leave_lab(request: Request, sess: Dict[str, Any], f: Dict[str, str]) -> Response:
        await call(hub.leave_lab, sess["user"]["id"], int(request.path_params["lab_id"]))
        return RedirectResponse("/account?note=left", 303)

    async def revoke(request: Request, sess: Dict[str, Any], f: Dict[str, str]) -> Response:
        await call(hub.revoke, sess["user"]["id"], int(request.path_params["token_id"]), ip_of(request))
        return RedirectResponse("/account?note=revoked", 303)

    async def delete_account(request: Request, sess: Dict[str, Any], f: Dict[str, str]) -> Response:
        if f.get("sure") != "yes":
            raise HubError("Tick the box to confirm that you want your account deleted.")
        await call(hub.authenticate, sess["user"]["email"], f.get("password"), ip_of(request))
        await call(hub.delete_user, sess["user"]["id"], "user", ip_of(request))
        response = page("Your account is deleted", '<p>Your account and everything your Kelvins shared to the hub '
                                                   'are gone. Thanks for trying the hub.</p>')
        response.delete_cookie(COOKIE)
        return response

    def delete_form(sess: Dict[str, Any], error: Optional[str] = None) -> Response:
        return page("Delete your account", (
            '<form class="stack" method="post" action="/account/delete">%s<p>This deletes your account and everything '
            'your Kelvins shared to the hub. Your own computers keep their copies. It can\'t be undone. You may want to '
            '<a href="/account/export">export your data</a> first.</p><label>Password<input type="password" '
            'name="password" autocomplete="current-password" required></label><label class="check"><input '
            'type="checkbox" name="sure" value="yes" required><span>Yes, delete my account.</span></label>'
            '<button class="danger">Delete my account</button></form>' % csrf_field(sess)), sess, error=error,
            status=400 if error else 200)

    async def delete_page(request: Request) -> Response:
        sess, redirect = await signed_in(request, any_consent=True)
        return redirect or delete_form(sess)

    async def export(request: Request) -> Response:
        sess, redirect = await signed_in(request, any_consent=True)
        if redirect:
            return redirect
        uid = sess["user"]["id"]
        if time.time() - exported.get(uid, 0) < EXPORT_EVERY:   # each export can be gigabytes: one per 10 minutes
            return page("Export everything", "", sess, 429, error="You can export once every 10 minutes. Try again "
                                                                  "in a little while.")
        exported[uid] = time.time()
        path = await call(hub.export, uid)
        return FileResponse(path, media_type="application/zip", filename="kelvin-hub-%s.zip" % time.strftime("%Y-%m-%d"),
                            background=BackgroundTask(os.remove, path))

    # ------------------------------------------------------------------ shared work

    def seen_by(item: Dict[str, Any]) -> str:
        """Who sees a shared item, as the person's own pages say it."""
        if hub.labs:
            return item["lab"] or "only you"
        return "you and the Kelvin team"

    async def shared_chat(request: Request) -> Response:
        sess, redirect = await signed_in(request)
        if redirect:
            return redirect
        try:
            chat = await call(hub.chat, sess["user"]["id"], int(request.path_params["chat_id"]))
        except HubError as err:
            return page("Not found", "", sess, 404, error=str(err))
        return page(chat["title"], '<p class="muted">By %s · %s · %s · updated %s</p>%s' % (
            e(chat["owner"]), e(chat["project"] or "no project"), e(seen_by(chat)), when(chat["updated_at"]),
            render_chat(chat["messages"])), sess)

    async def shared_run(request: Request) -> Response:
        sess, redirect = await signed_in(request)
        if redirect:
            return redirect
        try:
            run = await call(hub.run, sess["user"]["id"], int(request.path_params["run_id"]))
        except HubError as err:
            return page("Not found", "", sess, 404, error=str(err))
        files = "".join('<li><a href="/shared/runs/%d/files/%s">%s</a></li>' % (run["id"], quote(f), e(f))
                        for f in run["files"]) or (
            '<li class="muted">No files sent yet.</li>' if hub.run_files else
            '<li class="muted">None: this hub keeps runs\' records only, and the data files stay on your computer.'
            '</li>')
        return page("Run %s" % run["local_id"], (
            '<p class="muted">By %s · %s · %s · %s · started %s</p><h2>Files</h2><ul class="plain">%s</ul>'
            '<h2>About the run</h2><pre>%s</pre>'
            % (e(run["owner"]), e(run["device"] or "no device"), e(run["project"] or "no project"),
               e(seen_by(run)), when(run["created_at"]), files,
               e(json.dumps(run["meta"], indent=1, ensure_ascii=False)))), sess)

    async def shared_run_file(request: Request) -> Response:
        sess, redirect = await signed_in(request)
        if redirect:
            return redirect
        try:
            path = await call(hub.run_file, sess["user"]["id"], int(request.path_params["run_id"]),
                              request.path_params["name"])
        except HubError as err:
            return page("Not found", "", sess, 404, error=str(err))
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")   # never shown inline

    # ------------------------------------------------------------------ connecting a Kelvin

    def connect_request(params: Dict[str, str]) -> Dict[str, Any]:
        """The checked request from Kelvin; raises HubError before anything redirects anywhere."""
        uri = loopback(params.get("redirect_uri"))
        if params.get("code_challenge_method", "S256") != "S256" or not CHALLENGE.match(params.get("code_challenge", "")):
            raise HubError("Kelvin's request has no valid PKCE code_challenge (S256).")
        state = params.get("state", "")
        if not 8 <= len(state) <= 200:
            raise HubError("Kelvin's request has no valid state.")
        inst = installation(params)
        return {"redirect_uri": uri, "challenge": params["code_challenge"], "state": state, "installation": inst,
                "previous": previous_ids(params.get("previous"), inst["id"])}

    def back_to_kelvin(req: Dict[str, Any], **params: str) -> RedirectResponse:
        uri = req["redirect_uri"]
        return RedirectResponse(uri + ("&" if "?" in uri else "?") + urlencode(dict(params, state=req["state"])), 303)

    async def connect(request: Request) -> Response:
        sess = await session_of(request)
        if request.method == "GET":
            params = dict(request.query_params)
        else:
            params = await form_of(request)
        try:
            req = connect_request(params)
        except HubError as err:
            return page("Connect Kelvin", "<p>Start again from Kelvin.</p>", sess, 400, error=str(err))
        sess, redirect = await signed_in(request)
        if redirect:
            if request.method == "POST":   # signed out meanwhile: show the request again after signing in
                redirect = RedirectResponse("/signin?" + urlencode({"next": "/connect?" + urlencode(
                    {k: params[k] for k in params if k not in ("csrf", "action", "lab")})}), 303)
            return redirect
        user = sess["user"]
        inst = req["installation"]
        if request.method == "POST":
            if params.get("action") != "allow":
                check_csrf(sess, params)
                return back_to_kelvin(req, error="access_denied")
            key = "signin:" + user["email"]
            try:
                check_csrf(sess, params)
                if signin_tries.blocked(key) or signin_tries.blocked("signin-ip:" + who(request)):
                    raise HubError("Too many tries. Wait 15 minutes.")
                try:   # the browser may be someone else's session: only this account's password connects to it
                    await call(hub.authenticate, user["email"], params.get("password"), ip_of(request))
                except HubError:
                    signin_tries.hit(key)
                    signin_tries.hit("signin-ip:" + who(request))
                    raise HubError("That isn't the password of %s. Not you? Sign in as yourself below."
                                   % user["email"])
                lab_id = int(params["lab"]) if params.get("lab") and hub.labs else None
                code = await call(hub.connect_code, user, req["redirect_uri"], req["challenge"], inst, lab_id,
                                  req["previous"])
            except (HubError, ValueError) as err:
                return await connect_page(sess, params, req, str(err))
            return back_to_kelvin(req, code=code)
        return await connect_page(sess, params, req)

    async def connect_page(sess: Dict[str, Any], params: Dict[str, str], req: Dict[str, Any],
                           error: Optional[str] = None) -> Response:
        user = sess["user"]
        inst = req["installation"]
        labs = await call(hub.labs_of, user["id"]) if hub.labs else []
        keep = {k: v for k, v in params.items() if k in ("redirect_uri", "state", "code_challenge",
                                                         "code_challenge_method", "installation", "name", "platform",
                                                         "version", "person", "previous")}
        hidden = "".join('<input type="hidden" name="%s" value="%s">' % (e(k), e(v)) for k, v in keep.items())
        chosen = str(params.get("lab") or "")
        options = "".join('<option value="%d"%s>%s</option>' % (lab["id"], " selected" if str(lab["id"]) == chosen
                                                                else "", e(lab["name"])) for lab in labs)
        choose = ('<label>Share into<select name="lab">%s<option value="">No lab: only I and the Kelvin team see it'
                  '</option></select></label>' % options) if hub.labs else ""
        person = " ".join(str(params.get("person") or "").split())[:80]
        return page("Connect Kelvin to your account?", (
            '<p><b>%s</b> <span class="muted">(%s, Kelvin %s)</span>%s wants to connect to the hub account of '
            '<b>%s</b> (%s).</p>'
            '<div class="note"><b>It will share%s:</b> %s.<br><b>Never:</b> unpublished or confidential projects, '
            'incognito chats, API keys, or ChatGPT and Claude Code sign-ins.</div>'
            '<form class="stack" method="post" action="/connect">%s%s%s'
            '<label>Your hub password <span class="muted">(to be sure it\'s you)</span><input type="password" '
            'name="password" autocomplete="current-password" required></label>'
            '<div class="row"><button name="action" value="allow">Connect</button><button class="plain" '
            'name="action" value="cancel" formnovalidate>Cancel</button></div></form>'
            '<form class="inline" method="post" action="/signout">%s<input type="hidden" name="next" value="%s">'
            '<button class="plain">Not %s? Sign in as yourself</button></form>'
            '<p class="muted">Only connect a Kelvin you started yourself. You can disconnect it any time on your '
            '<a href="/account">account page</a>.</p>'
            % (e(inst["name"]), e(inst["platform"] or "?"), e(inst["version"] or "?"),
               ", used by <b>%s</b> on that computer," % e(person) if person else "", e(user["name"]), e(user["email"]),
               "" if hub.labs else " with the Kelvin team", e(what_goes()), csrf_field(sess), hidden, choose,
               csrf_field(sess), e("/connect?" + urlencode(keep)), e(user["name"]))),
            sess, error=error, status=400 if error else 200)

    # ------------------------------------------------------------------ the API for connected Kelvins

    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True, "hub": __version__, "draws_pdfs": drawing.can_draw()})

    async def model_pictures(request: Request) -> Response:
        """Which models see pictures, service by service, for a signed-in Kelvin (drawing.py):
        {"at", "services": {"opencode": {"yes": [...], "no": [...]}, ...}}."""
        if await app_of(request) is None:
            return api_error("not connected", 401)
        return JSONResponse(await call(catalog.pictures))

    async def llm_app(request: Request) -> Optional[Dict[str, Any]]:
        """The signed-in Kelvin behind a model call: its token, where each service's SDK puts its key."""
        auth = request.headers.get("authorization", "")
        token = request.headers.get("x-api-key") or request.headers.get("x-goog-api-key") or (
            auth[7:].strip() if auth.lower().startswith("bearer ") else "")
        return await call(hub.app, token) if token else None

    async def llm_call(request: Request) -> Response:
        """A signed-in Kelvin's call to a model service, passed on with the hub's key (llm.py)."""
        app = await llm_app(request)
        if app is None:
            return llm.refusal(401, "Sign in to Kelvin with your Kelvin account again to use the hub's models.")
        if request.method == "POST" and hub.needs_consent(app["user"]):   # model lists carry nothing of theirs
            return llm.refusal(403, NEW_TERMS)
        try:
            body = await read_body(request, llm.MAX_BODY) if request.method == "POST" else b""
        except TooMuch:
            return llm.refusal(413, "That message is larger than the Kelvin hub passes on (%d MB)." % (
                llm.MAX_BODY // 2 ** 20))
        return await gateway.forward(app, request.path_params["service"], request.method, request.path_params["path"],
                                     request.query_params.multi_items(), dict(request.headers), body)

    async def llm_status(request: Request) -> Response:
        """For a signed-in Kelvin: the model services the hub offers, the person's month so far, and whether new
        terms wait for them (their chats through the hub wait until they agree)."""
        app = await app_of(request)
        if app is None:
            return api_error("not connected", 401)
        services, usage = await asyncio.gather(call(gateway.offered, app["user"]),
                                               call(gateway.usage_of, app["user"]["id"], app.get("lab_id")))
        return JSONResponse(dict(usage, services=services, needs_consent=hub.needs_consent(app["user"]),
                                 consent_url=hub.base_url + "/consent", **terms_in_force()))

    async def pdf_pages(request: Request) -> Response:
        """A signed-in Kelvin's PDF pages (only those a model needs) drawn as pictures, for a model that sees pictures
        but can't read PDFs: {"pages": [{"mime", "data"}]}. In a process of its own; the hub keeps nothing."""
        app = await app_of(request)
        if app is None:
            return api_error("not connected", 401)
        if hub.needs_consent(app["user"]):
            return api_error(NEW_TERMS, 403)
        if not drawing.can_draw():
            return api_error("This hub can't draw PDF pages (pypdfium2 isn't installed there).", 501)
        user_id = app["user"]["id"]
        if not take_turn(user_id):
            return api_error("Too many at once from this account; try again in a moment.", 429)
        try:
            pages = await heavy(drawing.draw, await read_body(request, drawing.MAX_PDF))
        except TooMuch as err:
            return api_error(str(err), err.status)
        except drawing.DrawError as err:
            return api_error(str(err), 422)
        finally:
            end_turn(user_id)
        return JSONResponse({"pages": pages})

    async def token(request: Request) -> Response:
        app = await app_of(request)
        if request.method == "DELETE":
            if app is None:
                return api_error("not connected", 401)
            await call(hub.revoke, app["user"]["id"], app["token_id"], ip_of(request), "app")
            return Response(status_code=204)
        ip = ip_of(request) or "?"
        if token_tries.blocked("token:" + who(request)):
            return api_error("too many tries; wait 15 minutes", 429)
        try:
            body = json.loads(await read_body(request, MAX_TOKEN))
            if not isinstance(body, dict) or body.get("grant_type", "authorization_code") != "authorization_code":
                raise HubError("grant_type must be authorization_code")
            result = await call(hub.connect_app, body.get("code"), body.get("code_verifier"), body.get("redirect_uri"),
                                ip)
        except (TooMuch, Busy) as err:
            return api_error(str(err), err.status)
        except (HubError, ValueError) as err:
            token_tries.hit("token:" + who(request))
            return api_error("invalid_grant", 400, error_description=str(err))
        return JSONResponse(result)

    # ------------------------------------------------------------------ Kelvin's own sign-in (central accounts)

    async def json_body(request: Request) -> Dict[str, Any]:
        body = json.loads(await read_body(request, MAX_TOKEN))
        if not isinstance(body, dict):
            raise HubError("send a JSON object")
        return body

    def kelvin_of(body: Dict[str, Any]) -> Dict[str, str]:
        """The JSON API's {"installation": {"id", "name", "platform", "version"}} (/connect's query has the id as
        installation=, the rest beside it)."""
        i = body.get("installation") if isinstance(body.get("installation"), dict) else {}
        if not i.get("id"):
            raise HubError("Kelvin didn't say which installation it is: send \"installation\": {\"id\", \"name\", "
                           "\"platform\", \"version\"}.")
        return installation({"installation": i.get("id"), "name": i.get("name"), "platform": i.get("platform"),
                             "version": i.get("version")})

    async def api_signin(request: Request) -> Response:
        """Kelvin's sign-in: {email, password, installation: {id, name, platform, version}} -> a token for this
        Kelvin and who the person is. Wrong passwords count per email and computer (its installation id), and per
        address, so strangers elsewhere never lock a person out of their own computer. Past spray_tries' many for an
        email from anywhere, only computers the account is signed in on still try for that hour."""
        try:
            body = await json_body(request)
        except (HubError, ValueError) as err:
            return api_error(str(err))
        email = str(body.get("email") or "").strip().lower()
        inst = body.get("installation") if isinstance(body.get("installation"), dict) else {}
        keys = ("signin:%s %s" % (email, str(inst.get("id") or "")[:200]), "signin-ip:" + who(request))
        if any(signin_tries.blocked(k) for k in keys):
            return api_error("Too many tries. Wait 15 minutes, or reset your password.", 429)
        if spray_tries.blocked("signin:" + email) and not await call(hub.signs_in_on, email, inst.get("id")):
            return api_error("Too many wrong passwords for this account lately. Wait an hour, or reset your "
                             "password.", 429)
        try:
            result = await call(hub.sign_in_app, body.get("email"), body.get("password"), kelvin_of(body),
                                ip_of(request))
        except (TooMuch, Busy) as err:
            return api_error(str(err), err.status)
        except HubError as err:
            if str(err).startswith("Wrong email or password"):
                for k in keys:
                    signin_tries.hit(k)
                spray_tries.hit("signin:" + email)
                return api_error(str(err), 401)
            return api_error(str(err), 403 if "disabled" in str(err) or "Confirm" in str(err) else 400)
        signin_tries.clear(keys[0])
        if result.get("needs_consent"):   # agreed to on Kelvin's own sign-in screen (POST /api/consent)
            result.update(terms_in_force())
        return JSONResponse(result)

    async def api_register(request: Request) -> Response:
        """Kelvin's Create account: {name, email, password, consent: true, orcid?, installation} -> the account, a
        confirmation mail, and a token for this Kelvin. Same limits as the registration page."""
        try:
            body = await json_body(request)
            inst = kelvin_of(body)
        except (HubError, ValueError) as err:
            return api_error(str(err))
        if form_tries.blocked("register:" + who(request)):
            return api_error("Too many new accounts from this address. Try again later.", 429)
        ip = ip_of(request) or "?"
        try:
            user = await call(hub.register, body.get("email"), body.get("name"), body.get("password"),
                              body.get("consent") is True, body.get("orcid") or "", ip)
        except HubError as err:
            return api_error(str(err))
        form_tries.hit("register:" + who(request))
        await call(hub.send_confirmation, user)
        try:
            return JSONResponse(await call(hub.sign_in_app, body.get("email"), body.get("password"), inst, ip))
        except HubError as err:   # e.g. the hub wants the email confirmed first
            return api_error(str(err), 403, registered=True)

    async def api_reset(request: Request) -> Response:
        """{email}: mail a link to choose a new password. Answers the same whether or not the account exists."""
        try:
            body = await json_body(request)
        except (HubError, ValueError) as err:
            return api_error(str(err))
        response = JSONResponse({"ok": True})
        if not form_tries.blocked("reset:" + who(request)):
            form_tries.hit("reset:" + who(request))
            response.background = BackgroundTask(hub.start_reset, body.get("email"), ip_of(request) or "?")
        return response

    async def api_consent(request: Request) -> Response:
        """In Kelvin itself, its person agrees to the terms in force (Kelvin's sign-in screen, its notice when the terms
        change, Settings → Account), with the token of their sign-in there, so nobody signs in to a page of the hub's
        to agree: {version}, the version Kelvin showed them. Terms that changed again since are refused (409) with the
        version in force, which Kelvin shows them first."""
        app = await app_of(request)
        if app is None:
            return api_error("not connected", 401)
        try:
            body = await json_body(request)
        except (HubError, ValueError) as err:
            return api_error(str(err))
        user = app["user"]
        if str(body.get("version") or "") != hub.consent_version:
            return api_error("The terms changed again since Kelvin showed them: read the version of %s first."
                             % hub.consent_version, 409, **terms_in_force())
        if hub.needs_consent(user):
            await call(hub.give_consent, user["id"], ip_of(request), "kelvin")
        return JSONResponse({"agreed": hub.consent_version, "needs_consent": False})

    async def api_password(request: Request) -> Response:
        """{old, new}: change the password of the person signed in to this Kelvin. Their Kelvins stay signed in."""
        app = await app_of(request)
        if app is None:
            return api_error("not connected", 401)
        try:
            body = await json_body(request)
        except (HubError, ValueError) as err:
            return api_error(str(err))
        key = "signin:" + app["user"]["email"]
        if signin_tries.blocked(key):
            return api_error("Too many tries. Wait 15 minutes, or reset your password.", 429)
        try:
            await call(hub.change_password, app["user"]["id"], body.get("old"), body.get("new"), None, ip_of(request))
        except HubError as err:
            if str(err).startswith("Wrong email or password"):
                signin_tries.hit(key)
                return api_error("Wrong current password.", 401)
            return api_error(str(err))
        return JSONResponse({"ok": True})

    async def api_signout(request: Request) -> Response:
        """A Kelvin or a phone signs out of the person's Kelvin account: its token stops working."""
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not token or not await call(hub.sign_out_app, token, ip_of(request)):
            return api_error("not signed in", 401)
        return Response(status_code=204)

    async def api_account_delete(request: Request) -> Response:
        """Delete account from Kelvin or a phone: {email, password} -> the account deleted, as on the account page.
        The password, never only a token: a computer that merely stayed signed in can't delete anyone. Same lockouts
        as signing in."""
        try:
            body = await json_body(request)
        except (HubError, ValueError) as err:
            return api_error(str(err))
        keys = ("signin:" + str(body.get("email") or "").strip().lower(), "signin-ip:" + who(request))
        if any(signin_tries.blocked(k) for k in keys):
            return api_error("Too many tries. Wait 15 minutes, or reset your password.", 429)
        try:
            user = await call(hub.authenticate, body.get("email"), body.get("password"), ip_of(request))
        except (TooMuch, Busy) as err:
            return api_error(str(err), err.status)
        except HubError as err:
            if str(err).startswith("Wrong email or password"):
                for k in keys:
                    signin_tries.hit(k)
                return api_error(str(err), 401)
            return api_error(str(err), 403)
        signin_tries.clear(keys[0])
        await call(hub.delete_user, user["id"], "user", ip_of(request))
        return JSONResponse({"deleted": True})

    async def me(request: Request) -> Response:
        app = await app_of(request)
        if app is None:
            return api_error("not connected", 401)
        user = app["user"]
        labs = await call(hub.labs_of, user["id"]) if hub.labs else []
        lab = next((lab["name"] for lab in labs if lab["id"] == app["lab_id"]), None)
        return JSONResponse({"name": user["name"], "email": user["email"], "orcid": user["orcid"],
                             "email_confirmed": bool(user["email_confirmed_at"]), "shares_into": lab,
                             "labs": [lab["name"] for lab in labs], "needs_consent": hub.needs_consent(user),
                             "consent_url": hub.base_url + "/consent", "account_url": hub.base_url + "/account",
                             "run_files": hub.run_files, **terms_in_force()})

    def terms_in_force() -> Dict[str, Any]:
        """What Kelvin shows its person before they agree to the terms in force, in Kelvin itself (POST /api/consent):
        their version, and where to read the terms and the privacy notice (pages anyone reads, signed in or not),
        which say what changed."""
        return {"consent_version": hub.consent_version, "terms_url": hub.base_url + "/terms",
                "privacy_url": hub.base_url + "/privacy"}

    def new_terms() -> JSONResponse:
        return api_error(NEW_TERMS, 403, consent_url=hub.base_url + "/consent", needs_consent=True, **terms_in_force())

    async def sync(request: Request) -> Response:
        """What a Kelvin shares (db.Hub.sync). Before the person agrees to new terms, only a sync that removes is
        taken; a body far larger than removals ever are is refused before it is read."""
        app = await app_of(request)
        if app is None:
            return api_error("not connected", 401)
        declared = request.headers.get("content-length", "")
        if hub.needs_consent(app["user"]) and declared.isdigit() and int(declared) > REMOVALS_ONLY:
            return new_terms()
        uid = app["user"]["id"]
        if not take_turn(uid):
            return api_error("The hub is still busy with your other requests; try again in a moment.", 429)
        try:
            body = await read_body(request, MAX_SYNC)
            return JSONResponse(await heavy(hub.sync, app, await heavy(json.loads, body or b"{}")))
        except NeedsConsent:
            return new_terms()
        except HubError as err:
            return api_error(str(err), err.status)
        except ValueError as err:
            return api_error(str(err))
        finally:
            end_turn(uid)

    async def run_file_upload(request: Request) -> Response:
        app = await app_of(request)
        if app is None:
            return api_error("not connected", 401)
        if not hub.run_files:   # runs' records only: the data files stay on the person's computer
            return api_error(NO_RUN_FILES, 403, run_files=False)
        if hub.needs_consent(app["user"]):
            return api_error(NEW_TERMS, 403)
        declared = request.headers.get("content-length", "")
        if not declared.isdigit():
            return api_error("send the file with a Content-Length", 411)
        uid = app["user"]["id"]
        if not take_turn(uid):
            return api_error("The hub is still busy with your other requests; try again in a moment.", 429)
        try:
            return await upload_file(request, app, int(declared))
        finally:
            end_turn(uid)

    async def upload_file(request: Request, app: Dict[str, Any], declared: int) -> Response:
        try:   # check the run, the name and the person's share of the disk before a byte is stored
            ticket = await heavy(hub.reserve_upload, app, request.path_params["run_id"], request.path_params["name"],
                                 declared)
        except HubError as err:
            return api_error(str(err), err.status)
        tmp = hub.home / "tmp" / ("upload-%s" % secrets.token_hex(8))
        written = 0
        try:
            with open(tmp, "wb") as f:
                async for chunk in request.stream():
                    written += len(chunk)
                    if written > ticket["size"]:
                        return api_error("the file is longer than its Content-Length", 400)
                    f.write(chunk)
            return JSONResponse(await heavy(hub.store_upload, ticket, tmp, written))
        except HubError as err:
            return api_error(str(err), err.status)
        finally:
            await call(hub.release_upload, ticket)
            if tmp.exists():
                tmp.unlink()

    # ------------------------------------------------------------------ the operator's page

    def operator_of(request: Request) -> Optional[Dict[str, Any]]:
        token = request.cookies.get(OPERATOR_COOKIE, "")
        op = operators.get(token)
        if op is None:
            return None
        if op["expires"] < time.time() or not hmac.compare_digest(op["key"], hub.operator_key_hash()):
            operators.pop(token, None)   # expired, or the key changed since (kelvin-hub operator-key)
            return None
        return op

    def admin_page(op: Dict[str, Any], body: str, error: Optional[str] = None, note: Optional[str] = None) -> Response:
        """The owner console: for the Kelvin team, who run the hub (operator key), never for its users."""
        nav = ('<a href="/admin">Everyone</a><a href="/admin/models">Models</a><form class="inline" method="post" '
               'action="/admin/signout"><input type="hidden" name="csrf" value="%s"><button>Leave</button></form>'
               % e(op["csrf"]))
        return page("Owner console", body, error=error, note=note, status=400 if error else 200, nav=nav)

    async def admin(request: Request, error: Optional[str] = None, note: Optional[str] = None) -> Response:
        op = operator_of(request)
        if op is None:
            return page("Owner console", (
                '<p class="muted">For the Kelvin team, who run this hub.</p>'
                '<form class="stack" method="post" action="/admin/signin"><label>Operator key<input type="password" '
                'name="key" autocomplete="off" required></label><button>Open</button></form><p class="muted">The key '
                'is in operator-key.txt in the hub\'s folder when the hub is first set up; '
                '<code>kelvin-hub operator-key</code> makes a new one.</p>'), error=error,
                status=400 if error else 200)
        query = request.query_params.get("q", "")
        try:
            start = max(0, int(request.query_params.get("from", "0")))
        except ValueError:
            start = 0
        stats, people, total, activity, latest = await asyncio.gather(
            call(hub.stats), call(hub.find_users, query, PAGE_SIZE, start), call(hub.count_users, query),
            call(hub.recent_activity, 60), call(hub.latest_chats, LATEST) if hub.sharing else asyncio.sleep(0, []))
        csrf = '<input type="hidden" name="csrf" value="%s">' % e(op["csrf"])
        rows = "".join(
            '<tr><td>%d</td><td><a href="/admin/users/%d">%s</a><br><span class="muted">%s</span></td><td>%s</td>'
            '%s<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td><form class="inline" method="post" '
            'action="/admin/users/%d/%s">%s<button class="plain">%s</button></form></td></tr>'
            % (u["id"], u["id"], e(u["name"]), e(u["email"]), "yes" if u["email_confirmed_at"] else "no",
               "<td>%s</td>" % u["labs"] if hub.labs else "", u["computers"], e(u["kelvin"] or "–"),
               when(u["created_at"]), when(u["last_seen"]), u["id"], "enable" if u["disabled_at"] else "disable",
               csrf, "Enable" if u["disabled_at"] else "Disable") for u in people)
        shared = ""
        if hub.sharing:
            shared = ('<h2>Latest shared chats</h2><p class="muted">Opening one goes in the hub\'s record of its '
                      'person\'s account, which their Export shows.</p>' + (
                          '<div class="scroll"><table><tr><th>Updated</th><th>Person</th><th>Chat</th><th>Project</th>'
                          '<th>Size</th></tr>%s</table></div>' % "".join(
                              '<tr><td>%s</td><td><a href="/admin/users/%d">%s</a></td><td><a href="/admin/users/%d/'
                              'chats/%d">%s</a></td><td>%s</td><td>%s</td></tr>'
                              % (when(c["updated_at"]), c["user_id"], e(c["owner"]), c["user_id"], c["id"],
                                 e(c["title"]), e(c["project"] or "–"), size(c["bytes"])) for c in latest)
                          if latest else '<p class="muted">None yet.</p>'))
        pages = []
        if start > 0:
            pages.append('<a href="/admin?%s">Newer</a>' % e(urlencode({"q": query, "from": max(0, start - PAGE_SIZE)})))
        if start + PAGE_SIZE < total:
            pages.append('<a href="/admin?%s">Older</a>' % e(urlencode({"q": query, "from": start + PAGE_SIZE})))
        shown = "%d–%d of %d" % (start + 1, start + len(people), total) if people else "none of %d" % total
        events = "".join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                         % (when(a["at"]), e(a["actor"]), e(a["action"]),
                            '<a href="/admin/users/%d">%d</a>' % (a["user_id"], a["user_id"]) if a["user_id"] else "",
                            e(a["detail"])) for a in activity)
        hidden = set() if hub.sharing else {"labs", "projects", "chats", "runs", "run_bytes"}
        hidden |= set() if hub.labs else {"labs"}
        hidden |= set() if hub.run_files else {"run_bytes"}
        stats_rows = "".join('<tr><th>%s</th><td>%s</td></tr>' % (e(STAT_NAMES.get(k, k.replace("_", " "))),
                                                                   e(size(v) if k.endswith("bytes") else v))
                             for k, v in stats.items() if k not in hidden)
        return admin_page(op, (
            '<h2>The hub</h2><div class="scroll"><table>%s</table></div>'
            '<form class="inline" method="post" action="/admin/backup">%s<button class="plain">Back up the database '
            'now</button></form>'
            '<h2>People</h2><form class="row" method="get" action="/admin"><input type="search" name="q" value="%s" '
            'placeholder="Name or email" style="max-width:20rem"><button class="plain">Find</button>'
            '<a href="/admin/people.csv">Download everyone (CSV)</a></form>'
            '<p class="muted">%s · newest first%s</p>'
            '<div class="scroll"><table><tr><th>#</th><th>Person</th><th>Confirmed</th>%s<th>Computers</th>'
            '<th>Kelvin</th><th>Joined</th><th>Last seen</th><th></th></tr>%s</table></div><p class="row">%s</p>%s'
            '<h2>Recent activity</h2><div class="scroll"><table><tr><th>When</th><th>Who</th><th>What</th><th>Account'
            '</th><th>Detail</th></tr>%s</table></div>'
            % (stats_rows, csrf, e(query), shown, " · matching “%s”" % e(query) if query else "",
               "<th>Labs</th>" if hub.labs else "", rows, " ".join(pages), shared, events)),
            error=error, note=note)

    async def admin_person(request: Request, error: Optional[str] = None, note: Optional[str] = None) -> Response:
        """One person, for the operator: the account, the computers they sign in to Kelvin on, and its record."""
        op = operator_of(request)
        if op is None:
            return RedirectResponse("/admin", 303)
        uid = int(request.path_params["user_id"])
        try:
            user = await call(hub.user, uid)
        except HubError as err:
            return await admin(request, error="Account %d: %s" % (uid, err))
        computers, activity, labs, usage, shared = await asyncio.gather(
            call(hub.connections, uid), call(hub.activity_of, uid, 200),
            call(hub.labs_of, uid) if hub.labs else asyncio.sleep(0, []), call(gateway.usage_of, uid),
            call(hub.shared_by, uid) if hub.sharing else asyncio.sleep(0, None))
        csrf = '<input type="hidden" name="csrf" value="%s">' % e(op["csrf"])
        facts = [("Email", e(user["email"])), ("ORCID iD", e(user["orcid"] or "–")),
                 ("Email confirmed", when(user["email_confirmed_at"]) if user["email_confirmed_at"] else "not yet"),
                 ("Joined", when(user["created_at"])), ("Last seen", when(user["last_seen"])),
                 ("Terms agreed", "%s (%s)%s" % (e(user["consent_version"] or "–"), when(user["consent_at"]),
                                                  "; not yet those in force" if hub.needs_consent(user) else "")),
                 ("Status", "disabled since %s" % when(user["disabled_at"]) if user["disabled_at"] else "active")]
        if hub.labs:
            facts.append(("Labs", e(", ".join(lab["name"] for lab in labs) or "none")))
        machines = "".join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                           % (e(c["name"] or "Kelvin"), e(c["platform"] or "–"), e(c["version"] or "–"),
                              when(c["created_at"]), when(c["last_used"])) for c in computers)
        record = "".join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                         % (when(a["at"]), e(a["actor"]), e(a["action"]), e(a["detail"]), e(a["ip"] or ""))
                         for a in activity)
        verb = "enable" if user["disabled_at"] else "disable"
        return admin_page(op, (
            '<p><a href="/admin">← Everyone</a></p><h2>%s <span class="muted">#%d</span></h2>'
            '<div class="scroll"><table>%s</table></div>'
            '<form class="inline" method="post" action="/admin/users/%d/%s">%s<input type="hidden" name="back" '
            'value="person"><button class="plain">%s this account</button></form>'
            '<h2>Computers</h2>%s%s'
            '<h2>Models</h2><p>$%.2f of $%.2f this month%s.</p><form class="row" method="post" '
            'action="/admin/users/%d/budget">%s<input type="hidden" name="back" value="person"><label>Own budget per '
            'month <input name="usd" value="%s" placeholder="the default" style="max-width:7rem"></label><button '
            'class="plain">Save</button></form>'
            '<h2>Activity</h2><div class="scroll"><table><tr><th>When</th><th>Who</th><th>What</th><th>Detail</th>'
            '<th>IP</th></tr>%s</table></div>'
            % (e(user["name"]), uid, "".join('<tr><th>%s</th><td>%s</td></tr>' % kv for kv in facts), uid, verb, csrf,
               verb.capitalize(),
               '<div class="scroll"><table><tr><th>Computer</th><th>System</th><th>Kelvin</th><th>First signed in'
               '</th><th>Last used</th></tr>%s</table></div>' % machines if computers else
               '<p class="muted">Not signed in on any computer now.</p>', shared_work(uid, shared),
               usage["spent"], usage["budget"], " (their own budget)" if usage["own_budget"] else "", uid, csrf,
               "%g" % usage["budget"] if usage["own_budget"] else "", record)),
            error=error, note=note)

    def shared_work(uid: int, shared: Optional[Dict[str, Any]]) -> str:
        """A person's shared work on their page in the owner console: projects, then chats and runs, each linked to
        its own page (opening one goes in their record)."""
        if shared is None:
            return ""
        counts = shared["counts"]
        out = ['<h2>Shared work</h2><p class="muted">%d project%s, %d chat%s and %d run%s, newest first. Opening a '
               'chat or a run goes in the hub\'s record of this account, which its Export shows.</p>'
               % (counts["projects"], "" if counts["projects"] == 1 else "s", counts["chats"],
                  "" if counts["chats"] == 1 else "s", counts["runs"], "" if counts["runs"] == 1 else "s")]

        def more(kind: str) -> str:
            n = len(shared[kind])
            return '<p class="muted">The latest %d of %d.</p>' % (n, counts[kind]) if counts[kind] > n else ""
        if shared["projects"]:
            out.append('<h3>Projects</h3><div class="scroll"><table><tr><th>Project</th><th>Chats</th><th>Runs</th>'
                       '<th>Computer</th><th>Updated</th></tr>%s</table></div>%s' % ("".join(
                           '<tr><td>%s</td><td>%d</td><td>%d</td><td>%s</td><td>%s</td></tr>'
                           % (e(p["name"]), p["chats"], p["runs"], e(p["computer"] or "–"), when(p["updated_at"]))
                           for p in shared["projects"]), more("projects")))
        if shared["chats"]:
            out.append('<h3>Chats</h3><div class="scroll"><table><tr><th>Chat</th><th>Project</th><th>Computer</th>'
                       '<th>Updated</th><th>Size</th></tr>%s</table></div>%s' % ("".join(
                           '<tr><td><a href="/admin/users/%d/chats/%d">%s</a></td><td>%s</td><td>%s</td><td>%s</td>'
                           '<td>%s</td></tr>' % (uid, c["id"], e(c["title"]), e(c["project"] or "–"),
                                                 e(c["computer"] or "–"), when(c["updated_at"]), size(c["bytes"]))
                           for c in shared["chats"]), more("chats")))
        if shared["runs"]:
            out.append('<h3>Runs</h3><div class="scroll"><table><tr><th>Run</th><th>Status</th><th>Started</th>'
                       '<th>Device</th><th>Project</th></tr>%s</table></div>%s' % ("".join(
                           '<tr><td><a href="/admin/users/%d/runs/%d">%s</a><br><span class="muted">%s</span></td>'
                           '<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                           % (uid, r["id"], e(r["plan"] or r["local_id"]), e(r["local_id"]), e(r["status"] or "–"),
                              shown_time(r["started"]) if r["started"] not in (None, "") else when(r["created_at"]),
                              e(r["device"] or "–"), e(r["project"] or "–")) for r in shared["runs"]), more("runs")))
        if not (shared["projects"] or shared["chats"] or shared["runs"]):
            out.append('<p class="muted">Nothing shared.</p>')
        return "".join(out)

    def back_to(user: Dict[str, Any]) -> str:
        return '<p><a href="/admin/users/%d">← %s</a></p>' % (user["user_id"], e(user["owner"]))

    async def admin_chat(request: Request) -> Response:
        """One person's shared chat, read-only, for the operator. The view goes in their record."""
        op = operator_of(request)
        if op is None:
            return RedirectResponse("/admin", 303)
        if not hub.sharing:
            return admin_page(op, '<p class="muted">This hub keeps Kelvin accounts only.</p>')
        uid = int(request.path_params["user_id"])
        try:
            chat = await call(hub.owner_chat, uid, int(request.path_params["chat_id"]))
        except HubError as err:
            return admin_page(op, '<p><a href="/admin/users/%d">← Back</a></p>' % uid, error=str(err))
        messages = chat["messages"] if isinstance(chat["messages"], list) else []
        facts = [("Person", '<a href="/admin/users/%d">%s</a> <span class="muted">%s</span>'
                  % (uid, e(chat["owner"]), e(chat["email"]))),
                 ("Project", e(chat["project"] or "none")),
                 ("Computer", e(" ".join(x for x in (chat["computer"], chat["platform"], chat["version"]) if x)
                                or "–")),
                 ("Updated", when(chat["updated_at"])),
                 ("Size", "%s · %d turns" % (size(chat["bytes"]), len(messages))),
                 ("Kelvin's id", e(chat["local_id"]))]
        return admin_page(op, (
            '%s<h2>%s</h2><div class="scroll"><table>%s</table></div><p><a href="/admin/users/%d/chats/%d/json">'
            'Download it (JSON)</a></p><h2>The conversation</h2>%s'
            % (back_to(chat), e(chat["title"]), "".join('<tr><th>%s</th><td>%s</td></tr>' % kv for kv in facts), uid,
               chat["id"], render_chat(messages))))

    async def admin_chat_json(request: Request) -> Response:
        """One person's shared chat as JSON, to keep, for the operator. The download goes in their record."""
        if operator_of(request) is None:
            return RedirectResponse("/admin", 303)
        if not hub.sharing:
            return api_error("not found", 404)
        uid = int(request.path_params["user_id"])
        try:
            chat = await call(hub.owner_chat, uid, int(request.path_params["chat_id"]), True)
        except HubError as err:
            return api_error(str(err), 404)
        keep = ("id", "local_id", "user_id", "owner", "email", "project", "computer", "installation_id", "title",
                "updated_at", "bytes", "messages")
        return Response(json.dumps({k: chat.get(k) for k in keep}, indent=1, ensure_ascii=False),
                        media_type="application/json; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="kelvin-chat-%d-%s.json"'
                                 % (uid, re.sub(r"[^A-Za-z0-9_.-]", "_", chat["local_id"])),
                                 "Cache-Control": "no-store"})

    async def admin_run(request: Request) -> Response:
        """One person's shared run, its record laid out and whole, for the operator. The view goes in their
        record."""
        op = operator_of(request)
        if op is None:
            return RedirectResponse("/admin", 303)
        if not hub.sharing:
            return admin_page(op, '<p class="muted">This hub keeps Kelvin accounts only.</p>')
        uid = int(request.path_params["user_id"])
        try:
            run = await call(hub.owner_run, uid, int(request.path_params["run_id"]))
        except HubError as err:
            return admin_page(op, '<p><a href="/admin/users/%d">← Back</a></p>' % uid, error=str(err))
        meta = run["meta"] if isinstance(run["meta"], dict) else {}
        facts = [("Person", '<a href="/admin/users/%d">%s</a> <span class="muted">%s</span>'
                  % (uid, e(run["owner"]), e(run["email"]))),
                 ("Plan", e(meta.get("plan") or "–")), ("Status", e(meta.get("status") or "–")),
                 ("Started", shown_time(meta.get("started")) if meta.get("started") else when(run["created_at"])),
                 ("Device", e(run["device"] or "–")), ("Project", e(run["project"] or "none")),
                 ("Computer", e(run["computer"] or "–")), ("Kelvin's id", e(run["local_id"]))]
        files = ('<p>Files on the hub: %s</p>' % e(", ".join(run["files"]))) if run["files"] else (
            '<p class="muted">No data files: they stay on the person\'s computer.</p>')
        return admin_page(op, (
            '%s<h2>Run %s</h2><div class="scroll"><table>%s</table></div>%s<h2>What it found</h2>%s'
            '<h2>Its record</h2>%s'
            '<details><summary>The record as Kelvin sent it (JSON)</summary><pre>%s</pre></details>'
            % (back_to(run), e(meta.get("plan") or run["local_id"]),
               "".join('<tr><th>%s</th><td>%s</td></tr>' % kv for kv in facts), files, found_rows(meta.get("card")),
               record_rows({k: v for k, v in meta.items() if k != "card"}),
               e(json.dumps(meta, indent=1, ensure_ascii=False)))))

    async def admin_people_csv(request: Request) -> Response:
        """Everyone, as a table for the operator: no passwords, nothing anyone shared."""
        if operator_of(request) is None:
            return RedirectResponse("/admin", 303)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["id", "name", "email", "orcid", "email_confirmed", "joined", "last_seen", "disabled",
                    "computers", "kelvin_version", "terms_agreed"])
        start = 0
        while True:
            people = await call(hub.find_users, "", 1000, start)
            for u in people:
                w.writerow(cells([u["id"], u["name"], u["email"], u["orcid"] or "", when(u["email_confirmed_at"])
                                  if u["email_confirmed_at"] else "", when(u["created_at"]), when(u["last_seen"]),
                                  when(u["disabled_at"]) if u["disabled_at"] else "", u["computers"], u["kelvin"],
                                  u["consent_version"] or ""]))
            if len(people) < 1000:
                break
            start += 1000
        return Response(out.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="kelvin-people.csv"',
                                 "Cache-Control": "no-store"})

    def month_asked(request: Request) -> Optional[str]:
        m = request.query_params.get("month", "")
        return m if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", m) else None

    async def admin_models(request: Request, error: Optional[str] = None, note: Optional[str] = None) -> Response:
        """The model gateway, for the operator (llm.py): the services' keys, what is offered, budgets, and the month's
        use by person, model and lab."""
        op = operator_of(request)
        if op is None:
            return RedirectResponse("/admin", 303)
        keys, rep = await asyncio.gather(call(gateway.keys), call(gateway.report, month_asked(request)))
        csrf = '<input type="hidden" name="csrf" value="%s"><input type="hidden" name="back" value="models">' % e(
            op["csrf"])
        services = "".join(
            '<tr><td><b>%s</b></td><td>%s</td><td><form class="row" method="post" action="/admin/models/key">%s'
            '<input type="hidden" name="service" value="%s"><input type="password" name="key" autocomplete="off" '
            'placeholder="%s" style="max-width:14rem"><button class="plain">%s</button></form>%s</td>'
            '<td><form class="row" method="post" action="/admin/models/offers">%s<input type="hidden" name="service" '
            'value="%s"><input name="models" value="%s" placeholder="every model it lists" style="max-width:18rem">'
            '<button class="plain">Save</button></form></td>'
            '<td><form class="row" method="post" action="/admin/models/access">%s<input type="hidden" name="service" '
            'value="%s"><input name="who" value="%s" placeholder="everyone" style="max-width:16rem">'
            '<button class="plain">Save</button></form></td></tr>'
            % (e(k["name"]), "key …%s, set %s" % (e(k["hint"]), when(k["set_at"])) if k["set"] else
               '<span class="muted">no key</span>', csrf, e(k["service"]), "Replace the key" if k["set"] else "API key",
               "Replace" if k["set"] else "Set",
               ('<form class="inline" method="post" action="/admin/models/key/remove">%s<input type="hidden" '
                'name="service" value="%s"><button class="plain">Remove</button></form>' % (csrf, e(k["service"])))
               if k["set"] else "", csrf, e(k["service"]), e(k["models"]), csrf, e(k["service"]), e(k["who"]))
            for k in keys)

        def money(v: Any) -> str:
            return "$%.2f" % (v or 0)
        people = "".join(
            '<tr><td><a href="/admin/users/%d">%s</a><br><span class="muted">%s</span></td><td>%d</td><td>%s</td>'
            '<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
            % (p["user_id"], e(p["name"]), e(p["email"]), p["requests"], "{:,}".format(p["input"] or 0),
               "{:,}".format(p["output"] or 0), money(p["usd"]), money(p["budget"]),
               "%d%%" % round(100 * (p["usd"] or 0) / p["budget"]) if p["budget"] else "–") for p in rep["people"])
        models = "".join('<tr><td>%s</td><td>%s</td><td>%d</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                         % (e(llm.SERVICES.get(m["service"], {}).get("name", m["service"])), e(m["model"]),
                            m["requests"], "{:,}".format(m["input"] or 0), "{:,}".format(m["output"] or 0),
                            money(m["usd"])) for m in rep["models"])
        labs = "".join(
            '<tr><td>%s</td><td>%s</td><td><form class="row" method="post" action="/admin/models/budget">%s<input '
            'type="hidden" name="scope" value="lab"><input type="hidden" name="ref" value="%d"><input name="usd" '
            'value="%s" placeholder="none" style="max-width:7rem"><button class="plain">Save</button></form></td></tr>'
            % (e(lab["name"]), money(lab["usd"]), csrf, lab["lab_id"],
               "" if lab["budget"] is None else "%g" % lab["budget"]) for lab in rep["labs"])
        t = rep["total"]
        return admin_page(op, (
            '<p><a href="/admin">← Everyone</a></p><h2>Model services</h2><p class="muted">Kelvins signed in with a '
            'Kelvin account use these keys through the hub. Keys stay here, sealed; nobody sees them again.</p>'
            '<div class="scroll"><table><tr><th>Service</th><th>Key</th><th></th><th>Models offered</th>'
            '<th>Who may use it <span class="muted">(emails)</span></th></tr>%s</table></div>'
            '<h2>Budgets</h2><form class="row" method="post" action="/admin/models/budget">%s<input type="hidden" '
            'name="scope" value="default"><input type="hidden" name="ref" value="0"><label>Each person, per month '
            '<input name="usd" value="%g" style="max-width:7rem"></label><button class="plain">Save</button></form>'
            '<p class="muted">A person\'s own budget is set on their page. Past 80%%, they get a mail; past 100%%, '
            'the hub stops their requests until %s.</p>'
            '<h2>Use in %s</h2><form class="row" method="get" action="/admin/models"><input name="month" value="%s" '
            'placeholder="YYYY-MM" style="max-width:8rem"><button class="plain">Show</button>'
            '<a href="/admin/models/usage.csv?month=%s">Download (CSV)</a></form>'
            '<p>%s · %d requests · %s tokens in · %s out</p>'
            '<div class="scroll"><table><tr><th>Person</th><th>Requests</th><th>Tokens in</th><th>Tokens out</th>'
            '<th>Cost</th><th>Budget</th><th>Used</th></tr>%s</table></div>'
            '<h3>By model</h3><div class="scroll"><table><tr><th>Service</th><th>Model</th><th>Requests</th>'
            '<th>Tokens in</th><th>Tokens out</th><th>Cost</th></tr>%s</table></div>%s'
            '<p class="muted">Costs use models.dev\'s prices, read once a day; a model it doesn\'t list counts at $%g '
            'in and $%g out per million tokens. The services\' own bills are what you pay.</p>'
            % (services, csrf, rep["default_budget"], rep["renews"], e(rep["month"]), e(rep["month"]), e(rep["month"]),
               money(t["usd"]), t["requests"], "{:,}".format(t["input"]), "{:,}".format(t["output"]),
               people or '<tr><td colspan="7" class="muted">Nobody yet.</td></tr>',
               models or '<tr><td colspan="6" class="muted">None yet.</td></tr>',
               ('<h3>By lab</h3><div class="scroll"><table><tr><th>Lab</th><th>Cost</th><th>Lab budget</th></tr>%s'
                '</table></div>' % labs) if labs else "",
               gateway.limits()["fallback_price"]["input"], gateway.limits()["fallback_price"]["output"])),
            error=error, note=note)

    async def admin_models_csv(request: Request) -> Response:
        """A month's model use, one row per person and model: no text, only counts and costs."""
        if operator_of(request) is None:
            return RedirectResponse("/admin", 303)
        rep = await call(gateway.report, month_asked(request))
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["month", "user_id", "name", "email", "requests", "tokens_in", "tokens_out", "usd", "budget_usd"])
        for p in rep["people"]:
            w.writerow(cells([rep["month"], p["user_id"], p["name"], p["email"], p["requests"], p["input"],
                              p["output"], "%.4f" % (p["usd"] or 0), "%.2f" % p["budget"]]))
        return Response(out.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="kelvin-models-%s.csv"' % rep["month"],
                                 "Cache-Control": "no-store"})

    async def admin_model_key(request: Request, f: Dict[str, str]) -> str:
        service = f.get("service", "")
        try:
            await call(gateway.set_key, service, f.get("key"))
        except ValueError as err:
            raise HubError(str(err))
        return "%s's key is set." % llm.SERVICES[service]["name"]

    async def admin_model_key_remove(request: Request, f: Dict[str, str]) -> str:
        service = f.get("service", "")
        if service not in llm.SERVICES:
            raise HubError("No such service.")
        await call(gateway.remove_key, service)
        return "%s's key is removed: its models are no longer offered." % llm.SERVICES[service]["name"]

    async def admin_model_access(request: Request, f: Dict[str, str]) -> str:
        try:
            await call(gateway.set_access, f.get("service", ""), f.get("who"))
        except ValueError as err:
            raise HubError(str(err))
        return "Saved."

    async def admin_model_offers(request: Request, f: Dict[str, str]) -> str:
        try:
            await call(gateway.set_offers, f.get("service", ""), f.get("models"))
        except ValueError as err:
            raise HubError(str(err))
        return "Saved."

    async def admin_model_budget(request: Request, f: Dict[str, str]) -> str:
        scope = f.get("scope", "")
        try:
            ref = int(request.path_params["user_id"]) if "user_id" in request.path_params else int(f.get("ref") or 0)
            await call(gateway.set_budget, "user" if "user_id" in request.path_params else scope, ref, f.get("usd"))
        except ValueError as err:
            raise HubError(str(err))
        return "Budget saved."

    async def admin_signin(request: Request) -> Response:
        ip = ip_of(request) or "?"
        f = await form_of(request)
        if operator_tries.blocked("op:" + who(request)):
            return await admin(request, error="Too many tries. Wait 15 minutes.")
        if not hub.is_operator(f.get("key")):
            operator_tries.hit("op:" + who(request))
            return await admin(request, error="That isn't the operator key.")
        token = secrets.token_urlsafe(32)
        operators[token] = {"expires": time.time() + OPERATOR_TTL, "csrf": secrets.token_urlsafe(24),
                            "key": hub.operator_key_hash()}
        with hub.connect() as con:
            hub.audit(con, "operator", "operator signed in", None, None, ip)
        response = RedirectResponse("/admin", 303)
        response.set_cookie(OPERATOR_COOKIE, token, max_age=OPERATOR_TTL, httponly=True, samesite="strict",
                            secure=secure(request), path="/admin")
        return response

    def operator_action(fn: Any) -> Any:
        async def endpoint(request: Request) -> Response:
            op = operator_of(request)
            f = await form_of(request)
            if op is None or not hmac.compare_digest(f.get("csrf", ""), op["csrf"]):
                return RedirectResponse("/admin", 303)
            back = admin_person if f.get("back") == "person" and "user_id" in request.path_params else \
                admin_models if f.get("back") == "models" else admin
            try:
                note = await fn(request, f)
            except HubError as err:
                return await back(request, error=str(err))
            return await back(request, note=note)
        return endpoint

    async def admin_signout(request: Request) -> Response:
        operators.pop(request.cookies.get(OPERATOR_COOKIE, ""), None)
        response = RedirectResponse("/", 303)
        response.delete_cookie(OPERATOR_COOKIE, path="/admin")
        return response

    async def admin_disable(request: Request, f: Dict[str, str]) -> str:
        user_id, verb = int(request.path_params["user_id"]), request.path_params["verb"]
        if verb not in ("disable", "enable"):
            raise HubError("Unknown action.")
        await call(hub.set_disabled, user_id, verb == "disable", ip_of(request))
        return "Account %d is %sd." % (user_id, verb)

    async def admin_backup(request: Request, f: Dict[str, str]) -> str:
        path = await call(hub.backup, None, True)
        return "Backed up to backups/%s." % path.name

    async def admin_stats(request: Request) -> Response:
        if not hub.is_operator(request.headers.get("x-operator-key")):
            return api_error("operator key needed", 401)
        return JSONResponse(await call(hub.stats))

    # ------------------------------------------------------------------ the app

    async def failed(request: Request, exc: Exception) -> Response:
        if isinstance(exc, HubError):
            if request.url.path.startswith("/api/"):
                return api_error(str(exc), exc.status)
            return page("Something is wrong", "", await session_of(request), exc.status, error=str(exc))
        log.exception("error on %s %s", request.method, request.url.path)
        if request.url.path.startswith("/api/"):
            return api_error("the hub had a problem; it is logged", 500)
        return page("Something went wrong", "<p>The hub had a problem; it is logged. Try again.</p>", status=500)

    @asynccontextmanager
    async def lifespan(app: Any) -> Any:
        async def nightly() -> None:   # one backup a day, checked every hour
            while True:
                try:
                    await call(hub.backup)
                except Exception:
                    log.exception("backup failed")
                await asyncio.sleep(3600)
        async def settings() -> None:   # hub.json edited while the hub runs: in use within CONFIG_EVERY
            while True:
                await asyncio.sleep(CONFIG_EVERY)
                try:
                    await call(hub.refresh_config)
                except Exception:
                    log.exception("hub.json not read again")
        tasks = [asyncio.create_task(settings())] + ([asyncio.create_task(nightly())] if backups else [])
        yield
        for task in tasks:
            task.cancel()
        await gateway.close()

    relay, relay_routes = relaying.routes(hub, call, read_body, api_error)
    routes = [
        Route("/", home),
        Route("/kelvin.svg", logo),
        Route("/terms", terms),
        Route("/privacy", privacy),
        Route("/register", register, methods=["GET", "POST"]),
        Route("/signin", signin, methods=["GET", "POST"]),
        Route("/signout", signout, methods=["POST"]),
        Route("/consent", consent, methods=["GET", "POST"]),
        Route("/confirm", confirm),
        Route("/reset", reset, methods=["GET", "POST"]),
        Route("/reset/new", reset_new, methods=["GET", "POST"]),
        Route("/account", account),
        Route("/account/confirm", account_action(resend), methods=["POST"]),
        Route("/account/profile", account_action(profile), methods=["POST"]),
        Route("/account/password", account_action(password), methods=["POST"]),
        Route("/account/delete", delete_page),
        Route("/account/delete", account_action(delete_account, any_consent=True), methods=["POST"]),
        Route("/account/export", export),
        Route("/labs", labs_only(account_action(make_lab)), methods=["POST"]),
        Route("/labs/join", labs_only(account_action(join_lab)), methods=["POST"]),
        Route("/labs/{lab_id:int}/leave", labs_only(account_action(leave_lab)), methods=["POST"]),
        Route("/connections/{token_id:int}/revoke", sharing_only(account_action(revoke)), methods=["POST"]),
        Route("/shared/chats/{chat_id:int}", sharing_only(shared_chat)),
        Route("/shared/runs/{run_id:int}", sharing_only(shared_run)),
        Route("/shared/runs/{run_id:int}/files/{name:path}", sharing_only(shared_run_file)),
        Route("/connect", sharing_only(connect), methods=["GET", "POST"]),
        Route("/admin", admin),
        Route("/admin/people.csv", admin_people_csv),
        Route("/admin/signin", admin_signin, methods=["POST"]),
        Route("/admin/signout", admin_signout, methods=["POST"]),
        Route("/admin/users/{user_id:int}", admin_person),
        Route("/admin/users/{user_id:int}/chats/{chat_id:int}", admin_chat),
        Route("/admin/users/{user_id:int}/chats/{chat_id:int}/json", admin_chat_json),
        Route("/admin/users/{user_id:int}/runs/{run_id:int}", admin_run),
        Route("/admin/users/{user_id:int}/budget", operator_action(admin_model_budget), methods=["POST"]),
        Route("/admin/users/{user_id:int}/{verb}", operator_action(admin_disable), methods=["POST"]),
        Route("/admin/models", admin_models),
        Route("/admin/models/usage.csv", admin_models_csv),
        Route("/admin/models/key", operator_action(admin_model_key), methods=["POST"]),
        Route("/admin/models/key/remove", operator_action(admin_model_key_remove), methods=["POST"]),
        Route("/admin/models/offers", operator_action(admin_model_offers), methods=["POST"]),
        Route("/admin/models/access", operator_action(admin_model_access), methods=["POST"]),
        Route("/admin/models/budget", operator_action(admin_model_budget), methods=["POST"]),
        Route("/admin/backup", operator_action(admin_backup), methods=["POST"]),
        Route("/api/health", health),
        Route("/api/token", sharing_only(token), methods=["POST", "DELETE"]),
        Route("/api/signin", api_signin, methods=["POST"]),
        Route("/api/register", api_register, methods=["POST"]),
        Route("/api/reset", api_reset, methods=["POST"]),
        Route("/api/password", api_password, methods=["POST"]),
        Route("/api/consent", api_consent, methods=["POST"]),
        Route("/api/signout", api_signout, methods=["POST"]),
        Route("/api/account/delete", api_account_delete, methods=["POST"]),
        Route("/api/models/pictures", model_pictures),
        Route("/api/pdf/pages", pdf_pages, methods=["POST"]),
        Route("/api/llm", llm_status),
        Route("/llm/{service}/{path:path}", llm_call, methods=["GET", "POST"]),
        *relay_routes,
        Route("/api/me", sharing_only(me)),
        Route("/api/sync", sharing_only(sync), methods=["POST"]),
        Route("/api/sync/runs/{run_id}/files/{name:path}", sharing_only(run_file_upload), methods=["PUT"]),
        Route("/api/admin/stats", admin_stats),
    ]
    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={Exception: failed, HubError: failed})
    app.state.relay = relay
    return Gate(app, hosts)


def _logging(hub: Hub) -> None:
    handler = logging.FileHandler(hub.home / "hub.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(prog="kelvin-hub", description="The Kelvin hub: online accounts for Kelvin users "
                                                                "across labs, and the work they share.")
    ap.add_argument("command", nargs="?", default="serve", choices=["serve", "init", "backup", "operator-key", "stats"],
                    help="serve (default); init: make the hub's folder and database; backup: copy the database to "
                         "backups/ now; operator-key: make a new operator key; stats: print counts")
    ap.add_argument("--home", default=os.environ.get("KELVIN_HUB_HOME") or str(Path.home() / "Kelvin-hub"),
                    help="the hub's folder (default: $KELVIN_HUB_HOME, else ~/Kelvin-hub)")
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1: this computer only)")
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--base-url", help="the address people reach the hub at, saved in hub.json "
                                       "(e.g. https://hub.example.org behind a Cloudflare Tunnel)")
    args = ap.parse_args(argv)

    hub = Hub(args.home)
    _logging(hub)
    if args.base_url:
        hub.config["base_url"] = args.base_url.rstrip("/")
        hub.save_config()
    elif hub.config["base_url"] == DEFAULTS["base_url"] and args.port != 8780:
        hub.config["base_url"] = "http://127.0.0.1:%d" % args.port
        hub.save_config()
    key_file = hub.ensure_operator_key()
    if key_file:
        print("The operator key (for /admin) is in %s. Move it to a password manager, then delete the file."
              % key_file, file=sys.stderr)
    if args.command == "init":
        print("Kelvin hub ready in %s" % hub.home, file=sys.stderr)
    elif args.command == "backup":
        print("Backed up to %s" % hub.backup(force=True), file=sys.stderr)
    elif args.command == "operator-key":
        if not key_file:
            print("New operator key in %s; the old one no longer works." % hub.new_operator_key(), file=sys.stderr)
    elif args.command == "stats":
        print(json.dumps(hub.stats(), indent=1))
    else:
        import uvicorn
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            print("Listening on %s: anyone who can reach this computer can reach the hub. Prefer 127.0.0.1 behind a "
                  "Cloudflare Tunnel, which gives https." % args.host, file=sys.stderr)
        print("Kelvin hub: %s (data in %s)" % (hub.base_url, hub.home), file=sys.stderr, flush=True)
        uvicorn.run(create_app(hub, port=args.port), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
