"""The Kelvin portal: a chat web app (like the Claude or ChatGPT website) wired to the lab.

    python -m labharness.web            # virtual lab, opens http://127.0.0.1:8770
    python -m labharness.web --demo     # adds a scripted model so it works without an API key
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import math
import os
import socket
import subprocess
import sys
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .._version import __version__
from ..accounts import Accounts, OwnerPin, cipher_for, has_legacy
from .. import central
from ..agent.approval import WebApprover
from ..agent.runtime import SETUP_STEPS, LabRuntime, writable_folder
from ..agent.server import build_server
from ..adapters import AdapterError
from ..autosetup import AutoSetup, log as setup_log, reviewed as mark_reviewed
from ..cards import plot_points, trace_x
from .. import autocard
from .. import profiles
from ..clock import ScaledClock, SimClock
from ..collection import Collection
from ..discovery import add_to_config, instrument_key, listed as config_lists, set_limits
from ..labconfig import ConfigError, load_lab_config, same_setup
from .. import carddraft
from .. import protection
from .. import rundata
from ..virtual_lab import (build_lab, builtin_ids, builtin_registry, collection_ids, is_simulated, kelvin_ids,
                           lab_registry, open_home_lab)
from .agent import Busy, ChatAgent, runs_in, typed_stop
from . import review, rules as rulebook
from .chatgpt import SignIn, SignInError
from .hubsync import NETWORK as HUB_UNREACHABLE, SYNC_EVERY as HUB_EVERY, HubClientError, HubLink
from .qr import QR
from .relay import RelayLink
from .remote import (API as PHONE_API, ROLES, Remote, addresses, allow_through_firewall, allowed, firewall_state,
                     pairing_link, remote_host_ok)
from .providers.base import Provider
from .settings import Settings
from .store import ChatStore

log = logging.getLogger("kelvin")   # the desktop app writes this to ~/Kelvin/kelvin.log

STATIC = Path(__file__).parent / "static"
TRACE_POINTS = 400
ATTACH_MAX = 40 * 2 ** 20   # a photo (the portal shrinks it first) or a PDF
# what works while nobody is signed in: signing in, a new account, and stopping whatever runs
SIGNED_OUT = {"/api/session", "/api/session/reset", "/api/session/hub", "/api/accounts", "/api/accounts/remove",
              "/api/stop", "/api/runs/stop"}
# sharing work with the Kelvin hub (hubsync.py) is switched off for now: the hub is for accounts only (central.py)
HUB_SHARING = False
# Claude Code bridge: locked while Claude works the screen, so its clicks never answer for a person
LOCKED_WHILE_OPERATING = ("/api/approvals/", "/api/mode", "/api/settings", "/api/lab", "/api/session", "/api/accounts",
                          "/api/account", "/api/cards", "/api/instruments/", "/api/data-folder", "/api/folder",
                          "/api/phone", "/api/devices", "/api/watches", "/api/queue", "/api/terminal", "/api/mount",
                          "/api/quit", "/api/update", "/api/hub", "/api/skills/", "/api/know/", "/api/collection")
COLLECTION_EVERY = 12 * 3600   # how often Kelvin looks for a newer instrument collection while it is on


def make_providers(settings: Settings, demo: bool = False, signin: Optional[SignIn] = None) -> Dict[str, Provider]:
    from .providers.anthropic_provider import AnthropicProvider
    from .providers.gemini_provider import GeminiProvider
    from .providers.openai_provider import OpenAIProvider

    providers: Dict[str, Provider] = {
        "anthropic": AnthropicProvider(settings.get("anthropic_api_key")),
        "openai": OpenAIProvider("openai", "GPT", settings.get("openai_api_key")),
        "gemini": GeminiProvider(settings.get("gemini_api_key")),
    }
    if settings.get("local_base_url"):
        models = [m.strip() for m in (settings.get("local_models") or "").split(",")]
        providers["local"] = OpenAIProvider("local", "Local", settings.get("local_api_key"),
                                            base_url=settings.get("local_base_url"), models=models)
    from .services import PRESETS
    for s in settings.services:   # OpenRouter, OpenCode, DeepSeek... each under its own name
        p = PRESETS.get(s.get("preset", "custom"), PRESETS["custom"])
        providers[s["id"]] = OpenAIProvider(
            s["id"], s["name"], settings.service_key(s), base_url=s["base_url"],
            models=[m.strip() for m in (s.get("models") or "").split(",")], models_url=p.get("models_url"),
            exclude=p.get("exclude", ()), tools_only=p.get("tools_only", False), headers=p.get("headers"))
    if signin is not None and signin.status()["plan"]:   # ChatGPT models on the user's ChatGPT plan
        from .providers.chatgpt_provider import ChatGPTProvider
        providers["chatgpt"] = ChatGPTProvider(signin)
    from .. import claudecode   # Claude Code bridge (temporary): the person's own Claude Code, if they want it
    if claudecode.enabled() and settings.get("claude_code") != "off":
        cli = claudecode.find_cli(settings.get("claude_code_path") or "")
        if cli:
            from .providers.claudecode_provider import ClaudeCodeProvider
            providers["claudecode"] = ClaudeCodeProvider([cli], claudecode.workdir(Path(settings.path).parent))
    if demo:
        from .providers.demo import DemoProvider
        providers["demo"] = DemoProvider()
    return providers


def open_url(url: str) -> None:
    """Open a page in the user's own browser (OpenAI's sign-in page)."""
    webbrowser.open(url)


def _page(title: str, text: str) -> str:
    from html import escape
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width, initial-scale=1"><title>Kelvin</title><style>body{font:16px/1.5 system-ui,'
            'sans-serif;max-width:34rem;margin:15vh auto;padding:0 1rem;color:#1f2328}h1{font-size:1.4rem}</style>'
            '</head><body><h1>%s</h1><p>%s</p></body></html>' % (escape(title), escape(text)))


class Guard:
    """On 127.0.0.1, only this machine's browser holding the access key may use the API. On the network listener
    (phone access, see remote.py) only paired devices may, each within its role; the access key never works there."""

    def __init__(self, app: Any, token: str, hosts: Iterable[str], remote: Optional[Remote] = None,
                 signed_in: Optional[Callable[[], bool]] = None,
                 paired_with: Optional[Callable[[str], Optional[str]]] = None,
                 operating: Optional[Callable[[], Optional[str]]] = None):
        self.app = app
        self.token = token
        self.hosts = set(hosts)
        self.remote = remote
        self.signed_in = signed_in       # with accounts: is someone signed in? (else the API waits for them)
        self.paired_with = paired_with   # with accounts: whose phone a token is, when it isn't the signed-in person's
        self.operating = operating       # Claude Code bridge: the task Claude is doing on the screen, if any

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            origin = headers.get("origin")
            path, method = scope["path"], scope.get("method", "GET")
            problem = None
            port = self.remote.port if self.remote else None
            if port is not None and (scope.get("server") or (None, None))[1] == port:   # from the network
                host = headers.get("host", "")
                device = None
                if not remote_host_ok(host):
                    problem = (403, "bad host")
                elif origin and origin.split("://", 1)[-1] != host:
                    problem = (403, "bad origin")
                elif path in ("/auth/callback", "/hub/callback"):
                    problem = (403, "sign in from the Kelvin computer")
                elif path.startswith("/api/") and self.signed_in is not None and not self.signed_in():
                    problem = (401, "Nobody is signed in to Kelvin on its computer now.", {"signin": True})
                elif path.startswith("/api/") and not (path == "/api/pair" and method == "POST"):
                    token = headers.get("x-kelvin-token", "")
                    device = self.remote.device_for(token)
                    owner = self.paired_with(token) if device is None and self.paired_with else None
                    if owner:   # a person's phone works while they are signed in; it stays paired meanwhile
                        problem = (401, "This phone is %s's; it works while %s is signed in on the Kelvin computer."
                                   % (owner, owner), {"signin": True})
                    elif device is None:
                        problem = (401, "This device is not paired with Kelvin, or was removed. Pair it again.")
                    elif not allowed(device["role"], method, path):
                        problem = (403, "A %s device cannot do that. Use the Kelvin computer." % device["role"])
                scope["kelvin"] = {"role": device["role"] if device else None, "device": device}
            else:
                if headers.get("host") not in self.hosts:
                    problem = (403, "bad host")
                elif origin and origin.split("://", 1)[-1] not in self.hosts:
                    problem = (403, "bad origin")
                elif path.startswith("/api/") and not hmac.compare_digest(headers.get("x-kelvin-token", ""), self.token):
                    problem = (401, "missing or wrong access key")
                elif (path.startswith("/api/") and path not in SIGNED_OUT and self.signed_in is not None
                      and not self.signed_in()):
                    problem = (401, "Sign in to Kelvin first.", {"signin": True})
                elif (method != "GET" and self.operating is not None and self.operating()
                      and path.startswith(LOCKED_WHILE_OPERATING)):
                    problem = (423, "Claude is operating this computer, so Kelvin's approvals and settings are locked "
                                    "until it finishes. Press Stop to end it.")
                scope["kelvin"] = {"role": "owner", "device": None}
            if problem:
                body = dict(problem[2] if len(problem) > 2 else {}, error=problem[1])
                await JSONResponse(body, status_code=problem[0])(scope, receive, send)
                return
        await self.app(scope, receive, send)


def role_of(request: Request) -> str:
    return (request.scope.get("kelvin") or {}).get("role") or "owner"


def person(request: Request) -> str:
    """Who a person's request is from, as the lab's record names them: the Kelvin computer or a paired phone."""
    device = (request.scope.get("kelvin") or {}).get("device")
    return "human (phone %s)" % device.get("name") if device else "human (portal)"


def channel(request: Request) -> Tuple[str, Optional[str]]:
    """Where a person's request comes from: ("portal", None) or ("phone", the phone's name)."""
    device = (request.scope.get("kelvin") or {}).get("device")
    return ("phone", str(device.get("name") or "")) if device else ("portal", None)


STOPPED = ("Stopped: Kelvin's answer, the ramp or run in progress (a run's safe-stop steps run) and the queue; open "
           "approvals were declined.")


SUMMARY_SYSTEM = ("You write the opening of a physics project's master report: two or three short paragraphs "
                  "saying what the project is about, what has been measured on which samples, what was found "
                  "(with the numbers given), and what comes next. Use only the facts given; never invent results "
                  "or references. Plain prose, no headings, no lists.")


def shown(message: Dict[str, Any]) -> Dict[str, Any]:
    """A chat message as the browser and phones get it: without the provider's own state, and Kelvin continuing a
    chat by itself in words for people (its text is for the model)."""
    out = {k: v for k, v in message.items() if k != "raw"}
    if isinstance(out.get("wake"), str) and out["wake"]:
        out["text"] = "Kelvin continued this chat: " + out["wake"]
    return out


def unseen_pictures(provider: Provider, attached: List[Dict[str, Any]]) -> Optional[str]:
    """What to tell a person who sends pictures to a model that can't see them (most local models): said as they
    send them, not learnt from an answer about a picture the model never saw."""
    names = [str(a.get("name") or "the picture") for a in attached if str(a.get("mime") or "").startswith("image/")]
    if not names or getattr(provider, "sees_images", False):
        return None
    return ("The model can't see pictures through %s: it gets only the name of %s. To have a model look at %s, pick "
            "one that sees pictures (such as Claude, GPT or Gemini with an API key)."
            % (provider.label, ", ".join(names), "it" if len(names) == 1 else "them"))


def open_path(path: Path) -> None:
    """Open a file with the program the user has for it."""
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def create_app(runtime: LabRuntime, settings: Settings, store: ChatStore, *, demo: bool = False,
               hosts: Iterable[str] = ("127.0.0.1:8770", "localhost:8770"),
               providers: Optional[Dict[str, Provider]] = None, on_quit: Optional[Any] = None,
               auto_setup: bool = False, accounts: Optional[Accounts] = None, updater: Any = None,
               on_exit: Optional[Any] = None, sync_collection: bool = False) -> Any:
    """accounts: people sign in, each to their own settings, keys, chats, data folder and record (accounts.py).
    Without it, one person uses Kelvin, as before accounts. updater: keeps an installed Kelvin up to date
    (updater.py); on_exit closes the app (the desktop window), so an update can install. sync_collection: keep the
    instrument collection up to date from GitHub while it is switched on (collection.py); without it, Kelvin only
    reads the copy it has."""
    signin = SignIn(Path(settings.path).parent)   # Sign in with ChatGPT (chatgpt.json next to the settings)
    hub = HubLink(settings, store, lambda: runtime.data_dir)   # the Kelvin hub: the person's account across labs
    agent = ChatAgent(runtime, store, build_server(runtime), providers or make_providers(settings, demo, signin), settings)
    runtime.project_info = agent.project_info            # reports: names, devices, notes, reading lists
    runtime.project_chat_runs = agent.project_chat_runs
    runtime.project_ids = lambda: [None] + [p["id"] for p in store.projects()]
    remote = Remote(settings)
    guard: Any = None
    runtime.cards_dir = Path(settings.path).parent / "cards"
    lab_file = Path(settings.path).parent / "lab.yaml"   # the user's own lab, next to their settings
    home = lab_file.parent
    runtime.home = home                                   # each instrument's folder: ~/Kelvin/instruments/<name>
    runtime.lab_memory_dir = home                         # the lab's memory, everyone's (labmemory.py)
    from .. import envs
    envs.set_home(home)          # the instrument environments Kelvin installs makers' packages into (envs.py)
    envs.cleanup()               # environments removed while their files were in use
    envs.attach(envs.DRIVERS)    # the built-in drivers import their makers' packages from there
    runtime.keep_hands(settings.hands_on())               # a hands-on lock stays on across restarts
    runtime.owner = OwnerPin(home)                        # the lab owner's PIN, when one is set
    collection = Collection(home)                         # the instrument collection's copy: ~/Kelvin/collection
    collection.notify = lambda title, text: runtime.notifier("setup", title, text)   # an update failing is said

    def collection_on() -> Optional[Collection]:
        """The instrument collection while it is switched on in Settings (it is, unless switched off)."""
        return collection if settings.get("collection") != "off" else None

    runtime.collection = collection_on()
    default_data = home / "virtual-lab"                   # runs, record and reports unless the user chose a folder
    loop_ref: Dict[str, Any] = {}
    session: Dict[str, Any] = {"user": None}              # who is signed in (with accounts); everyone is equal
    nobody = home / "lab"                                 # the record of what happens while nobody is signed in

    def user_default_data() -> Path:
        if accounts is None and runtime.lab_folder is not None:   # this lab's own folder (labfolders.py)
            return lab_data_folder(home, runtime.lab, runtime.lab_info, record=False)
        return accounts.data_dir(session["user"]) if accounts is not None and session["user"] else default_data

    async def card_model() -> str:
        """The model that writes cards: the default one, else the first real (not demo) model there is."""
        key = settings.get("default_model") or ""
        if key and not key.startswith("demo:"):
            try:
                agent.provider_for(key)
                return key
            except ValueError:
                pass
        for g in (await agent.models())["groups"]:
            if g["provider"] != "demo" and g["models"]:
                return g["models"][0]["key"]
        raise ValueError("no model to write the card with: add an API key in Settings")

    def ask_model(system: str, prompt: str) -> Any:
        """Ask the card model from a worker thread (the providers live on the server's event loop)."""
        loop = loop_ref.get("loop")
        if loop is None:
            raise RuntimeError("Kelvin is still starting")

        async def go() -> Any:
            key = await card_model()
            provider, model_id = agent.provider_for(key)
            return await asyncio.wait_for(review.ask(provider, model_id, system, prompt), 300), key
        return asyncio.run_coroutine_threadsafe(go(), loop).result(timeout=330)

    def search_web(query: str) -> List[str]:
        """Web addresses for a query, through the card model's service if it can search, else another (websearch.py);
        none when searching outside the lab is switched off."""
        loop = loop_ref.get("loop")
        if loop is None or not runtime.literature.enabled:
            return []

        async def go() -> List[str]:
            from . import websearch
            try:
                key = await card_model()
            except ValueError:
                key = ""
            found, _, problems = await websearch.search(agent.providers, key, query, 6)
            if problems and not found:
                setup_log.info("  web search: %s", "; ".join(problems))
            return [r["url"] for r in found]
        return asyncio.run_coroutine_threadsafe(go(), loop).result(timeout=330)

    def write_card(found: Dict[str, Any]) -> Dict[str, Any]:
        r = autocard.make_card(found, runtime.cards_dir, ask_model, builtin_ids=kelvin_ids(runtime.collection),
                               log=lambda m: setup_log.info("  %s", m), search=search_web, catalog=runtime.collection)
        if r.get("path"):   # usable at once in Lab setup, like a card a person saves
            runtime.refresh_cards([r["card_id"]])
        return r

    def sync_collection() -> Dict[str, Any]:
        """Bring the instrument collection's copy here up to date (the network). Cards for models the lab's registry
        has none for can be used at once; a newer card for one it has is used when the lab is next opened."""
        status = collection.sync()
        if runtime.collection is not None:
            runtime.refresh_cards([c.id for c in collection.cards() if c.id not in runtime.lab.registry])
        return status

    setup = AutoSetup(runtime, settings, home, can_switch=lambda: busy_problem() is None, make_card=write_card)
    runtime.card_writer = write_card                      # draft_card: a card for an instrument someone named

    def lab_changed(what: str) -> None:
        """Kelvin changed the lab itself: keep it in Settings; an instrument it added gets its folder, settings and
        manual."""
        settings.set_switched_off(runtime.lab.config.name, runtime.lab.switched_off)
        if what == "added":
            settings.update({"lab": "mine"})
            if auto_setup:
                setup.start("lab changed")

    runtime.lab_changed = lab_changed
    runtime.switched_off_for = settings.switched_off
    tasks: List[asyncio.Task] = []

    def configure_literature() -> None:
        runtime.idle_off_s = settings.idle_off_minutes * 60 or None     # light left on idle goes to safe after this
        runtime.idle_off_all = settings.get("idle_off_all") == "on"     # and other outputs too, when chosen
        runtime.literature.enabled = settings.get("literature") != "off"
        runtime.literature.openalex_key = settings.get("openalex_api_key") or ""
        runtime.scan_network = settings.get("scan_network") != "off"   # scans also look on the local network
        runtime.collection = collection_on()                            # scans, cards and experiments use it
        runtime.auto_report = settings.get("auto_report") or "both"     # a report after every run and queue
        runtime.report_kinds = set(settings.report_kinds)                # scan, device and project reports

    def configure_notifications() -> None:
        try:   # the webhook, email and the heartbeat: each off until a person sets it up
            runtime.notifier.configure(settings.get("webhook"), settings.notify_kinds, email=settings.email(),
                                       heartbeat=settings.get("heartbeat"), heartbeat_s=settings.heartbeat_s)
        except ValueError:
            runtime.notifier.configure(None, settings.notify_kinds)
        runtime.notifier.phones = lambda: bool(settings.devices)   # a paired phone sees requests and alarms
        if hasattr(runtime.approver, "escalate_s"):   # a request nobody answered here waits this long for the phone
            runtime.approver.escalate_s = settings.approval_wait_s

    @asynccontextmanager
    async def lifespan(app: Starlette):
        loop_ref["loop"] = asyncio.get_running_loop()
        await agent.start()
        tasks.append(asyncio.ensure_future(agent.waker.run()))   # chats continued later (wakeups.py)
        configure_notifications()
        configure_literature()
        await start_phone()
        runtime.start_watching()   # the watches; what a restart left; outputs left on idle
        runtime.tidy_later()   # runs a crash ended, and runs from before run.h5, kept as run.h5 and catalogued
        if updater is not None:   # look for a newer Kelvin soon after start, then twice a day
            tasks.append(asyncio.ensure_future(update_loop()))
        if auto_setup or runtime.lab.connecting:   # connect the person's lab Kelvin opened before connecting it; find
            setup.start("start" if auto_setup else "connect")   # and set up instruments (or say why not)
        hub.wake = asyncio.Event()
        if HUB_SHARING:
            tasks.append(asyncio.ensure_future(hub_loop()))
        if accounts is not None:   # phones through the Kelvin hub, while someone is signed in with a Kelvin account
            relay_link.wake = asyncio.Event()
            tasks.append(asyncio.ensure_future(relay_link.run()))
        if sync_collection:   # the instrument collection, while it is switched on
            loop_ref["collection"] = asyncio.Event()
            tasks.append(asyncio.ensure_future(collection_loop()))
        try:
            yield
        finally:
            hub.stopping = True   # a sync in progress stops at its next file
            for t in tasks:
                t.cancel()
            runtime.stop_watching()
            await remote.stop()
            await agent.close()

    def error(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)

    def needs_owner(body: Any) -> Optional[JSONResponse]:
        """403 (the portal then asks for it) unless no lab owner's PIN is set or the body has it ({pin})."""
        why = runtime.owner.check(body.get("pin") if isinstance(body, dict) else None) if runtime.owner else None
        return JSONResponse({"error": why, "pin": True}, status_code=403) if why else None

    async def owner_pin(request: Request) -> Response:
        """GET: whether the lab owner's PIN is set. POST {pin, old}: set it, change it, or remove it (pin empty);
        once one is set, only with it (old). On this computer only (phones never reach this)."""
        if request.method == "POST":
            body = await request.json()
            try:
                runtime.owner.set(body.get("pin"), body.get("old"))
            except ValueError as e:
                return JSONResponse({"error": str(e), "pin": True}, status_code=403 if runtime.owner.is_set else 400)
            runtime.gate.log.write("owner_pin", by=person(request), account=runtime.user,
                                   change="set" if runtime.owner.is_set else "removed")
        return JSONResponse({"set": runtime.owner.is_set})

    async def index(request: Request) -> Response:
        # version-stamp the assets so a browser never runs a stale copy after an update
        version = str(int(max((STATIC / n).stat().st_mtime for n in ("app.js", "app.css"))))
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        page = page.replace("/static/app.js", "/static/app.js?v=" + version).replace("/static/app.css", "/static/app.css?v=" + version)
        return Response(page, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})

    async def mobile(request: Request) -> Response:
        return FileResponse(STATIC / "mobile.html", headers={"Cache-Control": "no-store"})

    async def static(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in ("app.js", "app.css", "kelvin.svg", "mobile.js"):
            return error(404, "not found")
        return FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})

    async def bootstrap(request: Request) -> Response:
        models = await agent.models()
        keys = [m["key"] for g in models["groups"] for m in g["models"]]
        default = settings.get("default_model")
        if default not in keys:
            default = next((k for k in keys if k.startswith("anthropic:")), keys[0] if keys else None)
        return JSONResponse({
            "lab": runtime.lab.config.name,
            "simulated": all(i.simulated for i in runtime.lab.instruments.values()),
            "chats": store.list(), "projects": store.projects(), "models": models, "default_model": default,
            "settings": settings.public(), "active": agent.active, "answering": agent.answering(),
            "can_quit": on_quit is not None,
            "account": session_data(),
            "claude_code": claude_code_enabled(),
            "hub_sharing": HUB_SHARING,
            "update": update_summary(updater, settings.updates),
        })

    # ------------------------------------------------------------------ people (accounts.py)

    def running_for() -> Optional[str]:
        """What keeps Kelvin from changing person now, if anything."""
        problem = busy_problem()
        if problem:
            return problem
        if runtime.queue.state in ("running", "pausing"):
            return "The queue is running; wait for it to finish or stop it first."
        return None

    def session_data() -> Dict[str, Any]:
        if accounts is None:
            return {"accounts_on": False, "user": None}
        user = session["user"]
        listed = accounts.list()
        busy = running_for()
        on = central_on()
        return {"accounts_on": True, "accounts": listed,
                "user": {"name": user.get("display") or user["name"], "password": bool(user.get("hash")),
                         "email": user.get("email"), "central": bool(user.get("email"))} if user else None,
                "suggest": "" if listed else suggested_name(),
                "busy": busy, "busy_user": runtime.user if busy else None,
                "legacy": not listed and has_legacy(home) and not accounts.legacy_taken(),
                "central": on, "hub": hub_address(), "account_url": hub_address() + "/account" if on else None,
                "unlinked": accounts.unlinked() if on else [],
                "relay": dict(relay_link.view(), on=settings.get("phone_relay") != "off") if on else None,
                "setup": setup_brief()}

    def setup_brief() -> Optional[Dict[str, Any]]:
        """What the instrument setup is doing, for the sign-in screen: the step, not the instruments (anyone at the
        screen sees it). None when it isn't at work."""
        s = runtime.setup_status
        if s.get("state") not in SETUP_STEPS:
            return None
        text = {"connecting": "Connecting the lab's instruments", "scanning": "Looking for connected instruments",
                "drafting": "Writing the card for an instrument Kelvin doesn't know yet",
                "reading": "Reading the instruments' settings"}[s["state"]]
        if s.get("steps"):
            text += " (%s of %s)" % (s.get("step"), s["steps"])
        return {"state": s["state"], "message": text}

    # ------------------------------------------------------------------ phones through the Kelvin hub (relay.py)

    relay_app: Dict[str, Any] = {}

    def relay_target() -> Optional[Tuple[str, str, str]]:
        """(the hub, the person's token there, their name) while someone is signed in with a Kelvin account and
        hasn't switched phones through the hub off (Settings -> Account)."""
        user = session["user"]
        if user is None or not central_on() or settings.get("phone_relay") == "off":
            return None
        token = settings.get("account_token")
        return (hub_address(), token, user["name"]) if token else None

    def relay_hello() -> Dict[str, Any]:
        return {"name": socket.gethostname()[:80], "version": __version__, "direct": remote.port is not None}

    def unshared_project(pid: Any) -> bool:
        from ..hub.db import project_shared
        if not pid or pid == "general":
            return False
        try:
            return not project_shared(store.project(str(pid)))
        except KeyError:
            return True   # a project that is gone: Kelvin can't tell whether it was shared

    def unshared_chat(chat_id: str) -> bool:
        from ..hub.client import shareable_chat
        try:
            chat = store.load(chat_id)
        except (KeyError, OSError, ValueError):
            return False   # the portal says it isn't there
        return not shareable_chat(chat) or unshared_project(store.project_of(chat_id))

    def unshared_meta(meta: Any) -> bool:
        if not isinstance(meta, dict):
            return False
        return bool(meta.get("incognito") or meta.get("unshared")) or unshared_project(meta.get("project"))

    def unshared_run(run_id: str) -> bool:
        live = runtime.live_view(10 ** 9) or {}
        if live.get("run_id") == run_id and live.get("status") == "running":
            return unshared_meta(runtime.running_meta)
        try:
            meta = rundata.meta(runtime.runs / run_id)
        except (KeyError, OSError, ValueError):
            return False
        return unshared_meta(meta)

    NOT_THROUGH_HUB = ("That is unshared work (unpublished, confidential or limited to some models, or incognito), "
                       "so it doesn't go through the Kelvin hub. Open it on the lab network or through Tailscale.")

    def relay_private(method: str, path: str, body: bytes) -> Optional[str]:
        """Why a phone's request through the Kelvin hub would reach unshared work (None: it doesn't). The hub sees
        what passes through it, and unshared work never goes to the hub."""
        parts = path.strip("/").split("/")   # ["api", "chats", <id>, ...]
        kind = parts[1] if len(parts) > 1 else ""
        if kind == "reports":
            return "Reports open through the Kelvin hub from a run; the report library is on the Kelvin computer."
        if kind == "chats" and len(parts) == 2 and method == "POST":
            try:
                ask = json.loads(body or b"{}")
            except ValueError:
                ask = {}
            if isinstance(ask, dict) and (ask.get("incognito") or unshared_project(ask.get("project"))):
                return NOT_THROUGH_HUB
        if kind == "chats" and len(parts) > 2 and unshared_chat(parts[2]):
            return NOT_THROUGH_HUB
        if kind == "runs" and len(parts) > 2 and parts[2] != "stop" and unshared_run(parts[2]):
            return NOT_THROUGH_HUB
        if kind == "projects" and len(parts) > 2 and parts[2] != "general" and unshared_project(parts[2]):
            return NOT_THROUGH_HUB
        return None

    def relay_trim(method: str, path: str, data: Any) -> Tuple[Any, int]:
        """An answer through the Kelvin hub without unshared work, and how much was left out."""
        from ..hub.client import shareable_chat
        from ..hub.db import project_shared
        hidden = 0

        def chats(items: Any) -> Any:
            nonlocal hidden
            if not isinstance(items, list):
                return items
            kept = [c for c in items if isinstance(c, dict) and shareable_chat(c)
                    and not unshared_project(c.get("project"))]
            hidden += len(items) - len(kept)
            return kept

        def projects(items: Any) -> Any:
            nonlocal hidden
            if not isinstance(items, list):
                return items
            kept = [p for p in items if isinstance(p, dict) and project_shared(p)]
            hidden += len(items) - len(kept)
            return kept

        if path == "/api/chats":
            data = chats(data)
        elif path == "/api/projects":
            data = projects(data)
        elif path == "/api/bootstrap" and isinstance(data, dict):
            data = dict(data, chats=chats(data.get("chats")), projects=projects(data.get("projects")))
        elif path == "/api/runs" and isinstance(data, dict) and isinstance(data.get("runs"), list):
            kept = [r for r in data["runs"] if not unshared_run(str(r.get("run_id") or ""))]
            hidden += len(data["runs"]) - len(kept)
            data = dict(data, runs=kept)
        elif path in ("/api/state", "/api/live") and isinstance(data, dict):
            live = data.get("live") if path == "/api/state" else data
            if isinstance(live, dict) and live.get("status") == "running" and unshared_meta(runtime.running_meta):
                live = dict(live, plan="a scan of unshared work", rows=[], trace=None)
                hidden += 1
                data = dict(data, live=live) if path == "/api/state" else live
            mounted = data.get("mounted") if path == "/api/state" else None
            if isinstance(mounted, dict) and unshared_project(mounted.get("project")):
                data = dict(data, mounted=dict(mounted, project_name="unshared work"))
        return data, hidden

    relay_link = RelayLink(lambda *a: relay_app["app"](*a), relay_target, relay_hello, relay_private, relay_trim)

    def relay_wake() -> None:
        """Someone signed in or out (often in a worker thread): the relay looks again at once."""
        loop = loop_ref.get("loop")
        if loop is not None:
            loop.call_soon_threadsafe(relay_link.wake.set)

    # ------------------------------------------------------------------ central Kelvin accounts (central.py)

    def hub_address() -> str:
        return settings.account_hub or central.DEFAULT_HUB

    def central_on() -> bool:
        """People sign in with their central Kelvin account, once this computer knows the hub (else local
        accounts, as before)."""
        return accounts is not None and bool(hub_address())

    def central_sign_in(email: str, password: str, link: Optional[str] = None, link_password: str = "",
                        old_password: str = "", answer: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Sign a person in with their central Kelvin account: the hub checks the password (or, when Kelvin can't
        reach it, the verifier this computer kept from their last sign-in here). The first time on this computer they
        may take over an account from before central accounts (link), keeping its chats, data and keys; otherwise
        they get a new one here. Keys sealed with a password changed elsewhere are re-sealed when the old password is
        given (old_password), else they stay unreadable. Raises PermissionError (wrong password, too many tries),
        ConnectionError (offline, never signed in here), ValueError or RuntimeError (Kelvin busy)."""
        email = " ".join(str(email or "").split()).lower()
        if not email or not password:
            raise ValueError("Give your email and password.")
        account = accounts.find_email(email)
        online = True
        if answer is None:
            try:
                answer = central.Central(hub_address()).sign_in(email, password, settings.installation_id())
            except central.Unreachable as e:
                online = False
                if account is None:
                    raise ConnectionError("%s The first sign-in on a computer needs the hub; after that, Kelvin "
                                          "signs you in here offline too." % e)
                if not accounts.matches(account["name"], password):
                    raise PermissionError("Wrong password. (Kelvin can't reach the hub now, so it checked the "
                                          "password this computer knows.)")
            except central.CentralError as e:
                if e.status in (401, 403, 429):
                    raise PermissionError(str(e))
                raise ValueError(str(e))
        display = ((answer or {}).get("user") or {}).get("name") or ""
        if account is None:   # their first sign-in on this computer
            choices = accounts.unlinked()
            if choices and link is None:
                return {"link": choices}
            if link:
                try:
                    previous = accounts.find(link)
                except KeyError:
                    raise ValueError("There is no account %s on this computer." % link)
                if previous.get("email"):   # already someone's Kelvin account
                    raise ValueError("%s is already linked to a Kelvin account." % link)
                if previous.get("hash") and not accounts.matches(link, link_password):
                    raise PermissionError("That isn't the password of %s on this computer." % link)
                account = accounts.link(link, email, display)
                before = link_password if previous.get("hash") else ""
            else:
                first = not accounts.list()
                account = accounts.create_central(display or email, email, password, first and has_legacy(home))
                if first:
                    settings.adopt(accounts.settings_path(account))
                before = ""
        elif accounts.matches(account["name"], password):
            before = password
        elif old_password and accounts.matches(account["name"], old_password):
            before = old_password
        else:
            before = None   # changed elsewhere, and the old one not given: what was sealed with it stays sealed
        if before is not None and before != password:
            from .settings import reseal
            reseal(accounts.dir(account), cipher_for(before, account) if before else None,
                   cipher_for(password, accounts.find(account["name"])))
        account = accounts.set_verifier(account["name"], password)
        if display and account.get("display") != display:
            account = accounts.link(account["name"], email, display)
        sign_in(account, password)
        if online and answer and answer.get("access_token"):
            settings.put_secret("account_token", answer["access_token"])   # encrypted with their password
            relay_wake()   # sign_in's wake can reach the relay before the token is kept: look again with it
        from .settings import unreadable
        return {"signed_in": True, "offline": not online,
                "keys_sealed": before is None and unreadable(accounts.dir(account), settings.cipher) > 0,
                "needs_consent": bool((answer or {}).get("needs_consent")),
                "account_url": (answer or {}).get("account_url") or hub_address() + "/account"}

    async def central_call(fn: Any, *args: Any) -> Response:
        """Run a central sign-in step and say how it went, as the sign-in screen expects."""
        try:
            result = await asyncio.to_thread(fn, *args)
        except PermissionError as e:
            await asyncio.sleep(1)   # no fast guessing
            return error(401, str(e))
        except ConnectionError as e:
            return error(503, str(e))
        except ValueError as e:
            return error(400, str(e))
        except (RuntimeError, OSError) as e:
            return error(409, str(e))
        if result.get("link"):
            return JSONResponse(dict(session_data(), link=result["link"]))
        return JSONResponse(dict(session_data(), **result))

    async def session_reset(request: Request) -> Response:
        """POST {email}: the hub mails a link to choose a new password (it says the same whether or not the account
        exists)."""
        if not central_on():
            return error(404, "This computer has no Kelvin hub for accounts.")
        email = str((await request.json()).get("email") or "")
        try:
            await asyncio.to_thread(central.Central(hub_address()).reset, email)
        except central.Unreachable as e:
            return error(503, str(e))
        except central.CentralError as e:
            return error(e.status if e.status in (400, 429) else 400, str(e))
        return JSONResponse({"ok": True})

    async def session_hub(request: Request) -> Response:
        """POST {url}: the Kelvin hub this computer's people sign in to ("" for accounts on this computer only). By
        someone signed in; signed out, only until someone has signed in here with a central account (so nobody can
        quietly point the sign-in screen at a hub of their own)."""
        if accounts is None:
            return error(404, "Kelvin runs without accounts here.")
        if session["user"] is None and any(a.get("email") for a in accounts.list()):
            return error(401, "Sign in to change the Kelvin hub people here sign in to.")
        url = str((await request.json()).get("url") or "").strip().rstrip("/")
        if url:
            from ..hub.client import HubClientError, check_url
            try:
                url = check_url(url)
            except HubClientError as e:
                return error(400, str(e))
        settings.set_account_hub(url)
        return JSONResponse(session_data())

    def sign_in(account: Dict[str, Any], password: str) -> None:
        """Serve this person: their settings and keys, chats, ChatGPT sign-in, data folder and record. The
        instruments, cards and the lab's instrument log stay as they are: they are everyone's."""
        same = runtime.user == account["name"]   # back to their own work, maybe still running: nothing to switch
        problem = None if same else running_for()
        if problem:
            raise RuntimeError(("Kelvin is still doing %s's work. " % runtime.user if runtime.user else "") + problem)
        cipher = cipher_for(password, account)
        hub.use(None)   # until this person's things are all in place
        settings.use(accounts.settings_path(account), cipher)
        data_dir, problem = data_folder_for(settings, home, accounts.data_dir(account))
        if not same:
            try:
                runtime.use_user(account["name"], data_dir, problem)
            except RuntimeError:
                settings.use(None)
                raise
        store.use(accounts.chats_dir(account))
        signin.use(accounts.dir(account), cipher)
        if providers is None:
            agent.providers = make_providers(settings, demo, signin)
        configure_notifications()
        configure_literature()
        session["user"] = account
        hub.use(accounts.dir(account), account["name"])
        relay_wake()

    def sign_out() -> None:
        """Nobody is signed in: the portal waits for the next person. A plan or queue still running goes on and is
        recorded as its person's; Kelvin turns to nobody's record when it ends (or at the next sign-in)."""
        agent.stop()   # a chat still answering ends with its person
        session["user"] = None
        relay_wake()
        hub.use(None)
        settings.use(None)
        signin.use(nobody)
        if providers is None:
            agent.providers = make_providers(settings, demo, signin)
        try:
            runtime.use_user(None, nobody)
            configure_notifications()
        except RuntimeError:
            pass   # their run goes on, with their notifications

    async def session_view(request: Request) -> Response:
        """GET: who is signed in, and who can. POST {name, password}: sign in (or change person).
        DELETE: sign out."""
        if accounts is None:
            return JSONResponse(session_data())
        if request.method == "DELETE":
            if session["user"] is not None:
                await asyncio.to_thread(sign_out)
            return JSONResponse(session_data())
        if request.method == "POST":
            body = await request.json()
            if central_on():
                return await central_call(central_sign_in, str(body.get("email") or ""),
                                          str(body.get("password") or ""),
                                          body.get("link") if body.get("link") is not None else None,
                                          str(body.get("link_password") or ""), str(body.get("old_password") or ""))
            try:
                account = await asyncio.to_thread(accounts.verify, str(body.get("name") or ""),
                                                  str(body.get("password") or ""))
            except ValueError as e:
                await asyncio.sleep(1)   # no fast guessing
                return error(401, str(e))
            try:
                await asyncio.to_thread(sign_in, account, str(body.get("password") or ""))
            except (RuntimeError, OSError) as e:
                return error(409, str(e))
        return JSONResponse(session_data())

    async def accounts_view(request: Request) -> Response:
        """POST {name, password}: a new account, signed in at once. Anyone at the computer can make one; the first
        takes over what Kelvin kept before accounts (chats, runs, keys)."""
        if accounts is None:
            return error(404, "Kelvin runs without accounts here.")
        problem = running_for()
        if problem:
            return error(409, problem)
        body = await request.json()
        password = str(body.get("password") or "")
        if central_on():   # a central Kelvin account, made on the hub, then signed in here
            email = str(body.get("email") or "")
            try:
                answer = await asyncio.to_thread(central.Central(hub_address()).register, str(body.get("name") or ""),
                                                 email, password, body.get("consent") is True,
                                                 settings.installation_id())
            except central.Unreachable as e:
                return error(503, str(e))
            except central.CentralError as e:
                if isinstance(e.body, dict) and e.body.get("registered"):   # the hub wants the email confirmed first
                    return JSONResponse(dict(session_data(), registered=True, message=str(e)))
                return error(e.status if e.status in (400, 429) else 400, str(e))
            return await central_call(central_sign_in, email, password,
                                      body.get("link") if body.get("link") is not None else None,
                                      str(body.get("link_password") or ""), "", answer)
        first = not accounts.list()
        try:
            account = await asyncio.to_thread(accounts.create, str(body.get("name") or ""), password,
                                              first and has_legacy(home))
        except ValueError as e:
            return error(400, str(e))
        if first:
            settings.adopt(accounts.settings_path(account))
        try:
            await asyncio.to_thread(sign_in, account, password)
        except (RuntimeError, OSError) as e:
            return error(409, str(e))
        settings._save()   # keys taken over from before accounts are encrypted now, if there is a password
        return JSONResponse(session_data())

    def check_removal(account: Dict[str, Any], password: str, everywhere: bool) -> None:
        """Whoever takes a person off this computer knows their password: the one this computer knows for them, or
        with everywhere, the hub's, which then deletes their Kelvin account."""
        if not everywhere:
            accounts.verify(account["name"], password)
        elif not account.get("email") or not central_on():
            raise ValueError("%s has no Kelvin account here, only an account on this computer." % account["name"])
        else:
            central.Central(hub_address()).delete_account(account["email"], password)

    def remove_person(account: Dict[str, Any], password: str, everywhere: bool) -> Dict[str, List[str]]:
        """Off this computer as uninstalling would take them (uninstall.remove_person): their settings and API keys,
        ChatGPT sign-in, chats, projects and memory go; their measurements stay."""
        from ..uninstall import remove_person as wipe
        gone = accounts.forget(account["name"])
        report = wipe(accounts.dir(gone), accounts.chats_dir(gone), cipher_for(password, gone))
        runtime.gate.log.write("account_removed", user=gone["name"], kelvin_account_deleted=everywhere)
        return report

    async def removal(account: Dict[str, Any], body: Dict[str, Any]) -> Response:
        """Take someone off this computer (DELETE /api/account, or before signing in, POST /api/accounts/remove):
        {password, everywhere}. everywhere: their Kelvin account on the hub is deleted too."""
        busy = running_for()
        if busy:
            return error(409, busy)
        everywhere = body.get("everywhere") is True
        password = str(body.get("password") or "")
        try:
            await asyncio.to_thread(check_removal, account, password, everywhere)
        except central.CentralError as e:
            if e.status in (401, 429):
                await asyncio.sleep(1)
            return error(e.status if e.status in (401, 403, 404, 429) else 400, str(e))
        except central.Unreachable as e:
            return error(503, "%s Remove it from this computer only, or try again later." % e)
        except ValueError as e:   # not the password this computer knows
            await asyncio.sleep(1)
            return error(401, str(e))
        user = session["user"]
        if user is not None and user["name"].lower() == account["name"].lower():
            await asyncio.to_thread(sign_out)
        report = await asyncio.to_thread(remove_person, account, password, everywhere)
        return JSONResponse(dict(session_data(), removed=account["name"], everywhere=everywhere,
                                 kept=report["kept"], failed=report["failed"]))

    async def accounts_remove(request: Request) -> Response:
        """Before anyone signs in: take someone off this computer with their password, and with everywhere, delete
        their Kelvin account too."""
        if accounts is None:
            return error(404, "Kelvin runs without accounts here.")
        if session["user"] is not None:
            return error(409, "Someone is signed in: people are removed on the sign-in screen, or in their own "
                              "Settings.")
        body = await request.json()
        try:
            account = accounts.find(str(body.get("name") or ""))
        except KeyError:
            return error(404, "There is no %s on this computer." % str(body.get("name") or "such account"))
        return await removal(account, body)

    async def account_view(request: Request) -> Response:
        """POST {old, new}: change (or set, or remove) the signed-in person's password; their keys are encrypted
        again with it. DELETE {password, everywhere}: take them off this computer (their measurements stay) and sign
        out; with everywhere, their Kelvin account on the hub is deleted too."""
        user = session["user"]
        if accounts is None or user is None:
            return error(404, "Nobody is signed in.")
        body = await request.json()
        if request.method == "DELETE":
            try:
                return await removal(accounts.find(user["name"]), body)
            except KeyError:
                return error(404, "Nobody is signed in.")
        if user.get("email") and central_on():
            return await asyncio.to_thread(central_password, user, str(body.get("old") or ""),
                                           str(body.get("new") or ""))
        new = str(body.get("new") or "")
        try:
            account = await asyncio.to_thread(accounts.change_password, user["name"], str(body.get("old") or ""), new)
        except ValueError as e:
            await asyncio.sleep(1)
            return error(400, str(e))
        session["user"] = account
        cipher = cipher_for(new, account)
        settings.cipher = cipher
        settings._save()
        signin.cipher = cipher
        if signin.data:
            signin._save()
        return JSONResponse(session_data())

    def central_password(user: Dict[str, Any], old: str, new: str) -> Response:
        """Change a central account's password on the hub, then here: the verifier, and the keys re-sealed with it."""
        token = settings.get("account_token")
        if not token:
            return error(409, "Sign in once while Kelvin can reach the hub, then change your password.")
        try:
            central.Central(hub_address()).change_password(token, old, new)
        except central.Unreachable as e:
            return error(503, "%s Your password can only change while Kelvin reaches the hub." % e)
        except central.CentralError as e:
            return error(400 if e.status not in (401, 429) else e.status, str(e))
        from .settings import reseal
        before = cipher_for(old, user)
        account = accounts.set_verifier(user["name"], new)
        session["user"] = account
        cipher = cipher_for(new, account)
        settings._save()   # what is in memory first, then the files re-sealed with the new password
        reseal(accounts.dir(account), before, cipher)
        settings.use(accounts.settings_path(account), cipher)
        signin.use(accounts.dir(account), cipher)
        return JSONResponse(session_data())

    async def account_rekey(request: Request) -> Response:
        """POST {old}: the signed-in person's keys still sealed with a password from before (changed on another
        computer, or reset by mail) are sealed again with the current one."""
        user = session["user"]
        if accounts is None or user is None:
            return error(404, "Nobody is signed in.")
        old = str((await request.json()).get("old") or "")
        if not old:
            return error(400, "Give your old password.")
        if accounts.matches(user["name"], old):
            return error(400, "That is your password now; give the one from before.")

        def turn() -> int:
            from .settings import reseal
            settings._save()   # everything readable now is sealed with the current password first
            if signin.data:
                signin._save()
            n = reseal(accounts.dir(user), cipher_for(old, user), settings.cipher)
            settings.use(accounts.settings_path(user), settings.cipher)
            signin.use(accounts.dir(user), settings.cipher)
            return n

        n = await asyncio.to_thread(turn)
        if not n:
            await asyncio.sleep(1)   # no fast guessing
        return JSONResponse(dict(session_data(), rekeyed=n))

    async def claude_code_view(request: Request) -> Response:
        """Claude Code bridge (temporary): is it part of this Kelvin, is `claude` here, and is it signed in?"""
        from .. import claudecode
        if not claudecode.enabled():
            return JSONResponse({"enabled": False})
        cli = claudecode.find_cli(settings.get("claude_code_path") or "")
        state = await asyncio.to_thread(claudecode.status, [cli]) if cli else {"signed_in": False}
        return JSONResponse(dict(state, enabled=True, cli=cli, on=settings.get("claude_code") != "off",
                                 active="claudecode" in agent.providers, operating=runtime.operating))

    # ------------------------------------------------------------------ keeping Kelvin up to date (updater.py)

    async def update_loop() -> None:
        await asyncio.sleep(20)
        while True:
            if settings.updates != "off":
                try:
                    await asyncio.to_thread(updater.run_once, settings.updates)
                except Exception:   # never let one bad check end the checking
                    setup_log.exception("update check")
            await asyncio.sleep(12 * 3600)

    async def update_view(request: Request) -> Response:
        """GET: is there a newer Kelvin? POST {op: check}: look now (and download it in automatic mode).
        POST {op: install}: install the downloaded update now; Kelvin closes and starts again."""
        from .. import updater as updates
        if request.method == "POST":
            if updater is None:
                return error(404, "This Kelvin runs from source; update it with git.")
            op = str((await request.json()).get("op") or "")
            if op == "check":
                await asyncio.to_thread(updater.run_once, settings.updates if settings.updates != "off" else "ask")
            elif op == "download":
                found = await asyncio.to_thread(updater.check)
                if found["state"] == "available":
                    try:
                        await asyncio.to_thread(updater.download)
                    except updates.UpdateError as e:
                        return error(502, str(e))
            elif op == "install":
                problem = running_for() or (("Claude is operating the computer.") if runtime.operating else None)
                if problem:
                    return error(409, "Not now: " + problem)
                try:
                    updater.install(os.getpid(), relaunch=True)
                except updates.UpdateError as e:
                    return error(409, str(e))
                if on_exit is not None:   # close Kelvin; the installer waits for that, then starts it again
                    asyncio.get_running_loop().call_later(0.8, on_exit)
            else:
                return error(400, "unknown update operation %r" % op)
        return JSONResponse(updates.view(updater, settings.updates))

    # ------------------------------------------------------------------ the instrument collection (collection.py)

    async def collection_loop() -> None:
        """Keep the collection's copy here up to date while it is switched on: soon after start, twice a day, and at
        once when a person switches it on."""
        wake = loop_ref["collection"]
        try:
            await asyncio.wait_for(wake.wait(), 30)
        except asyncio.TimeoutError:
            pass
        while True:
            wake.clear()
            if collection_on() is not None:
                try:
                    await asyncio.to_thread(sync_collection)
                except Exception:   # never let one bad update end the updating
                    setup_log.exception("instrument collection")
            try:
                await asyncio.wait_for(wake.wait(), COLLECTION_EVERY)
            except asyncio.TimeoutError:
                pass

    async def collection_view(request: Request) -> Response:
        """GET: where the collection's copy here is from and what it holds. POST {op: sync}: look for a newer one
        now (only while it is switched on: switched off, Kelvin does not go to the network for it)."""
        if request.method == "POST":
            if collection_on() is None:
                return error(409, "The instrument collection is switched off in Settings.")
            if str((await request.json()).get("op") or "") != "sync":
                return error(400, "unknown collection operation")
            await asyncio.to_thread(sync_collection)
        return JSONResponse(dict(await asyncio.to_thread(collection.status), on=collection_on() is not None))

    async def lab_log(request: Request) -> Response:
        """The lab's instrument log: the latest events of everyone who used these instruments, with their names."""
        path = home / "instrument-log.jsonl"
        from ..agent.runtime import event_line, tail_events
        n = max(1, min(int(request.query_params.get("n") or 50), 500))
        events = tail_events(path, n) if path.exists() else []
        return JSONResponse({"events": [{"user": e.get("user") or "", "wall": e.get("wall", ""), "text": event_line(e)}
                                        for e in reversed(events)]})

    async def quit_app(request: Request) -> Response:
        if on_quit is None:
            return error(404, "not found")
        agent.stop()
        on_quit()
        return JSONResponse({"ok": True})

    def suggested_folder(project: Optional[str]) -> str:
        """Where a new chat saves its runs unless the person picks another folder: the folder its project's chats
        last chose, else the one this person last chose, else the data folder's runs."""
        if project:
            try:
                chosen = store.project(project).get("folder")
            except KeyError:
                chosen = None
            if chosen:
                return str(chosen)
        return settings.get("last_folder") or str(runtime.data_dir / "runs")

    def keep_folder(path: Optional[str], project: Optional[str]) -> None:
        """A folder the person chose: their next new chat (in this project) starts there. None: the data folder."""
        settings.remember_folder(path)
        if project:
            store.set_folder(project, path)

    def chat_folder(raw: Any) -> str:
        """A folder a person chose for a chat's runs and files, checked: not in Kelvin's own folder (the lab config,
        settings, accounts, cards and everyone's chats, which a chat's file tools never reach: folderfiles.off_limits),
        and Kelvin can write there."""
        from .. import folderfiles
        text = str(raw or "").strip()
        given = Path(text).expanduser()
        if text and given.is_absolute():
            why = folderfiles.off_limits(given.resolve(), [home, store.root])
            if why:
                raise ValueError(why)
        return str(writable_folder(text))

    async def new_chat_folder(body: Dict[str, Any], project: Optional[str]) -> Optional[str]:
        """The folder a new chat saves its runs in: the one the person picked (checked: a problem is theirs to
        see), else the suggested one if Kelvin can write there now (a drive that is away: the data folder)."""
        picked = str(body.get("folder") or "").strip()
        if picked:
            return await asyncio.to_thread(chat_folder, picked)
        suggested = suggested_folder(project)
        if Path(suggested) == runtime.data_dir / "runs":
            return None
        try:
            return await asyncio.to_thread(chat_folder, suggested)
        except ValueError:
            return None

    async def chats(request: Request) -> Response:
        if request.method == "POST":
            body = await request.json()
            project = body.get("project") or None
            try:
                folder = await new_chat_folder(body, project)
                chat = store.create(str(body.get("model") or ""), project, incognito=bool(body.get("incognito")))
            except KeyError as e:
                return error(404, e.args[0])
            except ValueError as e:
                return error(400, str(e))
            if folder:
                chat["folder"] = folder
                store.save(chat)
                keep_folder(folder, chat.get("project"))
            return JSONResponse(dict(chat, folder=folder or str(runtime.data_dir / "runs"), own_folder=bool(folder)))
        return JSONResponse(store.list())

    async def chat(request: Request) -> Response:
        chat_id = request.path_params["chat_id"]
        try:
            if request.method == "DELETE":
                if agent.answering(chat_id):
                    return error(409, "This conversation is still running.")
                store.delete(chat_id)
                agent.waker.cancel(chat_id)
                return JSONResponse({"ok": True})
            data = store.load(chat_id)
            if request.method == "PATCH":
                body = await request.json()
                title = str(body.get("title") or "").strip()[:80]
                if title:
                    if agent.answering(chat_id):
                        return error(409, "Rename it when the current answer is done.")
                    data["title"] = title
                    store.save(data)
                if "project" in body:  # file it under a project, or null to take it out
                    store.assign(chat_id, body["project"] or None)
                if "folder" in body:   # where its runs go: a folder the person chose, or null for the data folder
                    if agent.answering(chat_id):
                        return error(409, "Change the folder when the current answer is done.")
                    if runtime.operating:   # a person's choice, like the data folder
                        return error(423, "Claude is operating this computer, so the chat's folder is locked until "
                                          "it finishes. Press Stop to end it.")
                    raw = str(body.get("folder") or "").strip()
                    if raw:
                        data["folder"] = await asyncio.to_thread(chat_folder, raw)
                        keep_folder(data["folder"], store.project_of(chat_id))
                    else:
                        data.pop("folder", None)
                    store.save(data)
            out = dict(data, messages=[shown(m) for m in data["messages"]])
            out["folder"] = data.get("folder") or str(runtime.data_dir / "runs")   # where its runs go
            out["own_folder"] = bool(data.get("folder"))
            out["project"] = store.project_of(chat_id)  # the browser never needs provider-internal state
            out["rules"] = agent.chat_rules(data)
            return JSONResponse(out)
        except KeyError as e:
            return error(404, e.args[0])
        except ValueError as e:
            return error(400, str(e))

    async def projects(request: Request) -> Response:
        if request.method == "POST":
            try:
                return JSONResponse(store.create_project((await request.json()).get("name")))
            except ValueError as e:
                return error(400, str(e))
        return JSONResponse(store.projects())

    async def project(request: Request) -> Response:
        """One project with its memory (instructions and notes). PATCH takes a new name, new instructions, rules,
        or whether it is shared on the Kelvin hub."""
        project_id = request.path_params["project_id"]
        try:
            if request.method == "DELETE":
                store.delete_project(project_id)  # its memory goes with it
                return JSONResponse({"ok": True})
            if request.method == "PATCH":
                body = await request.json()
                if "name" in body:
                    store.rename_project(project_id, body["name"])
                if "instructions" in body:
                    store.set_instructions(project_id, body["instructions"])
                if "rules" in body:
                    named = (body["rules"] or {}).get("providers") if isinstance(body["rules"], dict) else None
                    known = set(rulebook.PROVIDERS) | {s["id"] for s in settings.services} | {"chatgpt"}
                    strange = [p for p in named or [] if p not in known]
                    if strange:
                        return error(400, "no model service called %s" % ", ".join(map(str, strange)))
                    store.set_rules(project_id, body["rules"])
                if "sharing" in body:   # on the Kelvin hub: "" (shared), "unpublished" or "confidential"
                    store.set_sharing(project_id, body["sharing"])
            return JSONResponse(store.project(project_id))
        except KeyError as e:
            return error(404, e.args[0])
        except ValueError as e:
            return error(400, str(e))

    # ------------------------------------------------------------------ devices (samples) and master reports

    def project_key(request: Request) -> Optional[str]:
        pid = request.path_params["project_id"]
        return None if pid == "general" else pid

    async def devices_view(request: Request) -> Response:
        """GET: a project's devices ("general" for work outside projects). POST {name, material, description}."""
        pid = project_key(request)
        try:
            if request.method == "POST":
                body = await request.json()
                store.add_device(pid, body.get("name"), body.get("material") or "", body.get("description") or "")
            folder = runtime.project_folder(pid)
            master = [folder.name + "/" + p.name for p in sorted(folder.glob("*_master.*"))] if folder.is_dir() else []
            return JSONResponse({"devices": store.devices(pid), "summary": store.summary_of(pid),
                                 "folder": folder.name, "master": master})
        except KeyError as e:
            return error(404, e.args[0])
        except ValueError as e:
            return error(400, str(e))

    async def device_view(request: Request) -> Response:
        """PATCH {name, material, description, retired, protection}. protection is a person's (no agent tool
        reaches it): the device's limits and gates (protection.py), checked against this lab ({} lifts it). If the
        device is mounted it holds at once, and its caps are written into the instruments ("caps")."""
        try:
            body = await request.json()
            fields = {k: body.get(k) for k in ("name", "material", "description", "retired")}
            if "protection" in body:
                fields["protection"] = protection.normalize(body.get("protection"), runtime.lab)
                was = next((d.get("protection") for d in store.devices(project_key(request))
                            if d["id"] == request.path_params["device_id"]), None) or {}
                if was and fields["protection"] != was:   # lifting or changing a protection: the owner's, when set
                    refused = needs_owner(body)
                    if refused:
                        return refused
            d = store.update_device(project_key(request), request.path_params["device_id"], **fields)
            if "protection" not in body:
                return JSONResponse(d)
            caps = await asyncio.to_thread(runtime.protect, d, "human (portal)")
            return JSONResponse(dict(d, caps=caps,
                                     protection_lines=protection.summary(d.get("protection") or {}, runtime.lab)))
        except KeyError as e:
            return error(404, e.args[0])
        except ValueError as e:
            return error(400, str(e))

    async def protection_view(request: Request) -> Response:
        """What a device's protection can name in this lab: its drives (with a source's leakage reading) and the
        dielectrics Kelvin has a rule for."""
        return JSONResponse(protection.options(runtime.lab))

    async def hands_view(request: Request) -> Response:
        """POST {on: true}: a person's hands are on the sample (connecting it, rewiring, a breakout box): what runs
        stops, every drive that reaches the sample goes to safe, and stays there until a person unlocks.
        POST {on: false, project, device}: done; with device, that sample is mounted first ("" for none).
        People only: no agent tool reaches this. A watch-only phone can lock, never unlock."""
        body = await request.json()
        k = request.scope.get("kelvin") or {}
        by = "human (phone %s)" % k["device"].get("name") if k.get("device") else "human (portal)"
        lines: List[str] = []
        if body.get("on") is True:
            lines = await asyncio.to_thread(runtime.hands_on, by)
        else:
            if role_of(request) == "watch":
                return error(403, "This phone can lock, not unlock. Unlock on the Kelvin computer.")
            refused = needs_owner(body)   # before anything changes: the sample mounted, the lock
            if refused:
                return refused
            if "device" in body and role_of(request) == "owner":
                pid = body.get("project") or None
                try:
                    if body.get("device"):
                        lines += (await asyncio.to_thread(runtime.mount, pid, store.find_device(pid, body["device"]),
                                                          by)).get("caps") or []
                    else:
                        await asyncio.to_thread(runtime.unmount, by)
                except KeyError as e:
                    return error(404, e.args[0])
            try:
                lines += await asyncio.to_thread(runtime.hands_off, by, body.get("pin"))
            except PermissionError as e:   # the lab owner's PIN (accounts.OwnerPin)
                return JSONResponse({"error": str(e), "pin": True}, status_code=403)
        settings.set_hands_on(runtime.hands)
        return JSONResponse({"hands_on": runtime.hands, "lines": lines, "mounted": runtime.mounted_view()})

    async def summary_view(request: Request) -> Response:
        """PUT {text}: the words that open the project's master report (empty: Kelvin's summary from the data).
        POST {model}: a model writes it from the project's scans and notes, if the project's rules allow that model."""
        pid = project_key(request)
        try:
            body = await request.json()
            if request.method == "PUT":
                store.set_summary(pid, body.get("text") or "")
                return JSONResponse({"summary": store.summary_of(pid)})
            key = str(body.get("model") or settings.get("default_model") or "")
            rules = store.project(pid).get("rules") if pid else None
            if not key or not rulebook.allows(rulebook.merge(None, rules), key):
                return error(400, "Choose a model this project's rules allow.")
            provider, model_id = agent.provider_for(key)
            text = await asyncio.wait_for(review.ask(provider, model_id, SUMMARY_SYSTEM,
                                                     await asyncio.to_thread(summary_prompt, pid)), 180)
            store.set_summary(pid, text.strip())
            return JSONResponse({"summary": store.summary_of(pid), "model": key})
        except KeyError as e:
            return error(404, e.args[0])
        except ValueError as e:
            return error(400, str(e))
        except asyncio.TimeoutError:
            return error(504, "The model did not answer within 3 minutes.")

    def summary_prompt(pid: Optional[str]) -> str:
        from ..reports import kinds
        info = agent.project_info(pid)
        owners = runtime._run_owners()
        ds = [d for d in runtime._digests() if owners.get(d.run_id) == pid]
        lines = ["Project: %s" % info["name"],
                 "Devices: " + ("; ".join("%s (%s) %s" % (d["name"], d.get("material") or "", d.get("description") or "")
                                          for d in info["devices"]) or "none")]
        lines += ["Note (%s): %s" % (k, t) for k, t in info["notes"]]
        for d in sorted(ds, key=lambda d: d.started)[-60:]:
            lines.append("%s %s%s [%s]: %s %s" % (d.day, d.plan, " on " + d.device_name if d.device_name else "",
                                                   d.status, kinds._first_sentence(d.assessment), d.key_result()))
        return "\n".join(lines)

    async def mount_view(request: Request) -> Response:
        """GET: the mounted sample and every project's devices to choose from. POST {project, device} mounts one
        (a person's choice); POST {} records that nothing is mounted."""
        caps: List[str] = []
        if request.method == "POST":
            body = await request.json()
            pid = body.get("project") or None
            if (runtime.mounted or {}).get("protection"):   # taking out a protected device lifts its protection
                refused = needs_owner(body)
                if refused:
                    return refused
            try:
                if body.get("device"):
                    caps = (await asyncio.to_thread(runtime.mount, pid, store.find_device(pid, body["device"]),
                                                    "human")).get("caps") or []
                else:
                    await asyncio.to_thread(runtime.unmount, "human")
            except KeyError as e:
                return error(404, e.args[0])
        choices = [{"project": None, "name": "General", "devices": store.devices(None)}] + \
            [{"project": p["id"], "name": p["name"], "devices": p.get("devices") or []} for p in store.projects()]
        return JSONResponse({"mounted": runtime.mounted_view(), "choices": choices, "caps": caps,
                             "hands_on": runtime.hands})

    async def reports_tree(request: Request) -> Response:
        return JSONResponse(dict(runtime.reports_tree(), folder=str(runtime.reports_dir)))

    async def reports_open_folder(request: Request) -> Response:
        runtime.reports_dir.mkdir(parents=True, exist_ok=True)
        try:
            open_path(runtime.reports_dir)
        except OSError as e:
            return error(500, "Could not open it: %s" % e)
        return JSONResponse({"ok": True})

    async def reports_refresh(request: Request) -> Response:
        """POST {project}: rewrite a project's master and device reports now ("general" or none: General)."""
        body = await request.json()
        pid = body.get("project")
        pid = None if pid in (None, "", "general") else str(pid)
        try:
            written = await asyncio.to_thread(runtime.refresh_reports, [pid])
        except Exception as e:
            return error(500, "Could not write the reports: %s" % e)
        return JSONResponse({"written": written})

    async def paper(request: Request) -> Response:
        try:
            store.delete_paper(request.path_params["project_id"], request.path_params["paper_id"])
        except KeyError as e:
            return error(404, e.args[0])
        return JSONResponse({"ok": True})

    async def note(request: Request) -> Response:
        try:
            store.delete_note(request.path_params["project_id"], request.path_params["note_id"])
        except KeyError as e:
            return error(404, e.args[0])
        return JSONResponse({"ok": True})

    async def memory_view(request: Request) -> Response:
        """The user's global memory: instructions, the notes Kelvin kept for every conversation, and whether it
        may keep new ones. Project memory is under /api/projects/{id}."""
        return JSONResponse({"instructions": settings.get("instructions") or "", "learn": settings.learning,
                             "notes": store.global_notes()})

    async def know_view(request: Request) -> Response:
        """Skills and recipes (skills.py). ?scope=global: Kelvin's own and the person's; ?scope=<project id>: the
        project's."""
        scope = request.query_params.get("scope") or "global"
        if scope == "global":
            items = [i for i in agent.know.items() if i.scope in ("kelvin", "global")]
        else:
            try:
                store.project(scope)
            except KeyError as e:
                return error(404, e.args[0])
            items = [i for i in agent.know.items(scope, own=False) if i.scope == scope]
        return JSONResponse({"items": [i.view() for i in items]})

    async def know_item(request: Request) -> Response:
        """DELETE a skill or recipe the person kept (Kelvin's own stay). POST {enabled}: offer it to Kelvin, or no
        longer (it stays kept; Kelvin's own can be switched off too)."""
        p = request.path_params
        try:
            if request.method == "POST":
                agent.know.switch(p["kind"], p["scope"], p["name"], bool((await request.json()).get("enabled")))
                return JSONResponse({"ok": True})
            gone = agent.know.delete(p["kind"], p["scope"], p["name"])
        except KeyError as e:
            return error(404, e.args[0])
        return JSONResponse({"ok": gone}, status_code=200 if gone else 404)

    async def know_menu(request: Request) -> Response:
        """The skills and recipes a chat (in ?project=) can start with /name: the ones switched on."""
        project = request.query_params.get("project") or None
        items = [i for i in agent.know.items(project, request.query_params.get("incognito") == "1") if i.enabled]
        return JSONResponse({"items": [{"kind": i.kind, "name": i.name, "description": i.description}
                                       for i in sorted(items, key=lambda i: i.name)]})

    async def skill_import(request: Request) -> Response:
        """POST a skill packed as Claude's are (a .zip or .skill with SKILL.md, or a lone SKILL.md), the file's bytes
        (X-File-Name: its name): kept for every project (?scope=global) or a project's chats (?scope=<project id>).
        409 when there is one by its name (?replace=1 overwrites it)."""
        from urllib.parse import unquote
        scope = request.query_params.get("scope") or "global"
        data = await request.body()
        if not data:
            return error(400, "The file is empty.")
        if len(data) > ATTACH_MAX:
            return error(413, "Kelvin takes skills up to %d MB." % (ATTACH_MAX // 2 ** 20))
        try:
            if scope != "global":
                store.project(scope)
            item = await asyncio.to_thread(agent.know.import_skill, scope, data,
                                           unquote(request.headers.get("x-file-name", "")),
                                           request.query_params.get("replace") == "1")
        except KeyError as e:
            return error(404, e.args[0])
        except FileExistsError as e:
            return error(409, str(e))
        except ValueError as e:
            return error(400, str(e))
        runtime.gate.log.write("skill_added", by="human", skill=item.name, scope=scope, files=len(item.files))
        return JSONResponse(item.view())

    async def skill_export(request: Request) -> Response:
        """A skill as a .zip Claude takes (<name>/SKILL.md and its files), to download."""
        p = request.path_params
        try:
            name, data = await asyncio.to_thread(agent.know.export, p["scope"], p["name"])
        except KeyError as e:
            return error(404, e.args[0])
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="%s"' % name, "Cache-Control": "no-store"})

    async def memory_note(request: Request) -> Response:
        try:
            store.delete_global_note(request.path_params["note_id"])
        except KeyError as e:
            return error(404, e.args[0])
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------------ Sign in with ChatGPT

    def rebuild_providers() -> None:
        if providers is None:
            agent.providers = make_providers(settings, demo, signin)

    async def chatgpt_view(request: Request) -> Response:
        return JSONResponse(signin.status())

    async def chatgpt_signin(request: Request) -> Response:
        """Open OpenAI's sign-in page in the user's browser; it comes back to /auth/callback on this address."""
        port = request.url.port or 80
        try:
            url = signin.begin("http://127.0.0.1:%d/auth/callback" % port)
        except SignInError as e:
            return error(400, str(e))
        await asyncio.to_thread(open_url, url)
        return JSONResponse({"url": url})

    async def chatgpt_signout(request: Request) -> Response:
        confirmed = await asyncio.to_thread(signin.sign_out)
        rebuild_providers()
        return JSONResponse(dict(signin.status(), revoked=confirmed))

    async def chatgpt_welcomed(request: Request) -> Response:
        signin.mark_welcomed()
        return JSONResponse(signin.status())

    async def auth_callback(request: Request) -> Response:
        try:
            st = await asyncio.to_thread(signin.finish, dict(request.query_params))
        except SignInError as e:
            return HTMLResponse(_page("Kelvin could not sign in with ChatGPT", str(e)), status_code=400)
        rebuild_providers()
        if st["plan"]:
            text = "Signed in as %s. ChatGPT models in Kelvin now use your ChatGPT plan." % (st["email"] or "you")
        else:
            text = ("Signed in as %s, but plan usage was not allowed, so ChatGPT models are not available. Sign in "
                    "again from Kelvin's Settings and allow it." % (st["email"] or "you"))
        return HTMLResponse(_page("You're signed in to Kelvin with ChatGPT", text + " You can close this tab and go "
                                  "back to Kelvin."))

    # ------------------------------------------------------------------ the Kelvin hub (hubsync.py)

    async def hub_loop() -> None:
        """Share what changed every few minutes, and at once after connecting."""
        await asyncio.sleep(10)
        while True:
            hub.wake.clear()
            if hub.connected:
                try:
                    await asyncio.to_thread(hub.sync_once)
                except Exception:   # never let one bad sync end the syncing
                    setup_log.exception("hub sync")
            try:
                await asyncio.wait_for(hub.wake.wait(), HUB_EVERY)
            except asyncio.TimeoutError:
                pass

    async def hub_view(request: Request) -> Response:
        """The person's hub: its address, who they are there, which lab Kelvin shares into and the last sync.
        Never the token."""
        return JSONResponse(await asyncio.to_thread(hub.status))

    async def hub_connect(request: Request) -> Response:
        """POST {url}: open the hub's connect page in the browser; it comes back to /hub/callback on this address."""
        if not HUB_SHARING:
            return error(404, "Sharing with the Kelvin hub is switched off for now.")
        body = await request.json()
        port = request.url.port or 80
        try:
            url = await asyncio.to_thread(hub.begin, str(body.get("url") or ""),
                                          "http://127.0.0.1:%d/hub/callback" % port)
        except HubClientError as e:
            return error(400, str(e))
        await asyncio.to_thread(open_url, url)
        return JSONResponse({"url": url})

    async def hub_disconnect(request: Request) -> Response:
        confirmed = await asyncio.to_thread(hub.disconnect)
        return JSONResponse(dict(await asyncio.to_thread(hub.status, False), revoked=confirmed))

    async def hub_sync(request: Request) -> Response:
        """Sync now, and say how it went."""
        if not HUB_SHARING:
            return error(404, "Sharing with the Kelvin hub is switched off for now.")
        if not hub.connected:
            return error(409, "Kelvin is not connected to the hub.")
        await asyncio.to_thread(hub.sync_once)
        return JSONResponse(await asyncio.to_thread(hub.status, False))

    async def hub_callback(request: Request) -> Response:
        title = "Kelvin could not connect to the hub"
        try:
            me = await asyncio.to_thread(hub.finish, dict(request.query_params))
        except HubClientError as e:
            return HTMLResponse(_page(title, str(e)), status_code=400)
        except HUB_UNREACHABLE:
            return HTMLResponse(_page(title, "Kelvin can't reach the hub now. Try again from Kelvin's Settings."),
                                status_code=502)
        hub.poke()
        who = " as %s (%s)" % (me["name"], me.get("email") or "") if me.get("name") else ""
        into = "sharing into %s" % me["shares_into"] if me.get("shares_into") else "sharing with only you (no lab)"
        return HTMLResponse(_page("Kelvin is connected to the hub", (
            "Connected%s, %s. Kelvin now shares your normal projects and unfiled chats there, with their runs; never "
            "unpublished or confidential projects, or incognito chats. You can close this tab and go back to Kelvin."
            % (who, into))))

    # ------------------------------------------------------------------ other model services

    async def services_view(request: Request) -> Response:
        """GET: the services set up, and the ones Kelvin knows. POST {preset, name, base_url, api_key, models}: add."""
        from .services import presets_view
        if request.method == "POST":
            try:
                settings.add_service(await request.json())
            except ValueError as e:
                return error(400, str(e))
            if providers is None:
                agent.providers = make_providers(settings, demo, signin)
        return JSONResponse({"services": settings.service_views(), "presets": presets_view(),
                             "models": await agent.models()})

    async def service_view(request: Request) -> Response:
        try:
            settings.remove_service(request.path_params["service_id"])
        except KeyError as e:
            return error(404, e.args[0])
        if providers is None:
            agent.providers = make_providers(settings, demo, signin)
        return JSONResponse({"services": settings.service_views(), "models": await agent.models()})

    async def service_test(request: Request) -> Response:
        """Ask the service for its models with the key it has."""
        provider = agent.providers.get(request.path_params["service_id"])
        if provider is None:
            return error(404, "no service %r" % request.path_params["service_id"])
        try:
            models = await asyncio.wait_for(provider.models(), 30)
        except Exception as e:
            from .agent import describe_error
            return JSONResponse({"ok": False, "message": describe_error(provider, e)})
        return JSONResponse({"ok": bool(models), "message": "%d models that Kelvin can use" % len(models) if models
                             else "it answered, but listed no model Kelvin can use; name them under Models"})

    # ------------------------------------------------------------------ the user's own instrument cards

    async def cards_view(request: Request) -> Response:
        """GET: the user's cards. POST {yaml, replace}: check and keep one (a person may set any status)."""
        if request.method == "GET":
            return JSONResponse({"cards": carddraft.user_cards(runtime.cards_dir, collection_ids(runtime.collection)),
                                 "folder": str(runtime.cards_dir), "kelvins": kelvins_cards()})
        body = await request.json()
        try:
            card, path, warnings = carddraft.save(runtime.cards_dir, str(body.get("yaml") or ""),
                                                  kelvin_ids(runtime.collection), "human",
                                                  str(body.get("replace") or "") or None, bool(body.get("version")))
        except ValueError as e:
            return error(400, str(e))
        runtime.refresh_cards([card.id] + ([str(body["replace"])] if body.get("replace") else []))
        runtime.gate.log.write("card", by="human", card=card.id, status=card.status, file=path.name)
        return JSONResponse({"id": card.id, "file": path.name, "status": card.status, "warnings": warnings,
                             "kelvins": card.id in builtin_ids(), "reconnect": sorted(
                                 n for n, i in runtime.lab.instruments.items() if i.card.id == card.id)})

    async def card_check(request: Request) -> Response:
        card, problems, warnings = carddraft.check(str((await request.json()).get("yaml") or ""))
        return JSONResponse({"id": card.id if card else None, "problems": problems, "warnings": warnings})

    def kelvins_cards() -> List[Dict[str, Any]]:
        """Kelvin's own cards the lab uses (not those it has its own version of): a person reviews a draft one for
        their lab here, or makes the lab's version of any of them."""
        reviewed = carddraft.builtin_reviews(runtime.cards_dir)
        mine = {c["id"] for c in carddraft.user_cards(runtime.cards_dir)}
        users: Dict[str, List[str]] = {}
        for name, inst in runtime.lab.instruments.items():
            users.setdefault(inst.card.id, []).append(name)
        out = []
        for card_id, names in sorted(users.items()):
            if card_id not in builtin_ids() or card_id in mine:
                continue
            card = runtime.lab.registry.get(card_id)
            try:
                text = Path(card.source).read_text(encoding="utf-8")
            except OSError:
                text = ""
            out.append({"id": card_id, "model": card.model, "status": card.status, "instruments": names,
                        "yaml": text, "problems": [], "warnings": [], "reviewable": card.status == "draft" or
                        card_id in reviewed})
        return out

    async def card_status(request: Request) -> Response:
        body = await request.json()
        card_id, status = str(body.get("id") or ""), str(body.get("status") or "")
        if status != "draft":   # marked reviewed: its outputs stop asking each time, the owner's call when set
            refused = needs_owner(body)
            if refused:
                return refused
        try:
            if card_id in builtin_ids():   # one of Kelvin's own: reviewed for this lab, kept with its content
                card = runtime.lab.registry.get(card_id)
                carddraft.review_builtin(runtime.cards_dir, card, status, str(runtime.user or ""))
                card.status = status
            else:
                card = carddraft.set_status(runtime.cards_dir, card_id, status)
                runtime.lab.registry.put(card)
        except KeyError as e:
            return error(404, e.args[0])
        except ValueError as e:
            return error(400, str(e))
        runtime.refresh_cards([card.id])   # reviewed, a person's card wins; back to draft, the collection's may
        for inst in runtime.lab.instruments.values():   # the connected instruments follow at once, not at reconnect
            if inst.card.id == card.id:
                inst.card.status = card.status
        runtime.gate.log.write("card", by="human", card=card.id, status=card.status)
        return JSONResponse({"id": card.id, "status": card.status})

    async def card_delete(request: Request) -> Response:
        card_id = str((await request.json()).get("id") or "")
        try:
            carddraft.delete(runtime.cards_dir, card_id)
        except KeyError as e:
            return error(404, e.args[0])
        # the collection's or Kelvin's own card of that id again (with a person's review of it), if there is one
        runtime.refresh_cards([card_id])
        return JSONResponse({"ok": True, "reconnect": sorted(n for n, i in runtime.lab.instruments.items()
                                                             if i.card.id == card_id)})

    async def card_try(request: Request) -> Response:
        """Try a card on an address a person typed: who it is, and every reading; nothing is set."""
        from .. import cardtry
        body = await request.json()
        card, problems, _ = carddraft.check(str(body.get("yaml") or ""))
        if card is None:
            return error(400, "Fix the card first: " + "; ".join(problems[:3]))
        address = str(body.get("address") or "").strip()
        if not address:
            return error(400, "Give the address to try it on, as in the lab config.")
        try:
            result = await asyncio.to_thread(cardtry.try_card, card, address, runtime.lab)
        except Exception as e:
            return error(400, "Could not try it: %s" % (str(e).splitlines()[0][:300] if str(e) else type(e).__name__))
        runtime.gate.log.write("card_try", by="human", card=card.id, address=address,
                               answered=sum(1 for r in result["readings"] if "value" in r))
        return JSONResponse(dict(result, summary=cardtry.summary(result)))

    async def card_draft(request: Request) -> Response:
        """Draft a card from manual text with a model (the default one unless named); checked, repaired once."""
        body = await request.json()
        manual = str(body.get("manual") or "").strip()
        if len(manual) < 200:
            return error(400, "Paste the manual's command reference or the maker's example scripts (at least a few "
                              "commands or functions with their arguments).")
        key = str(body.get("model") or settings.get("default_model") or "")
        if not key:
            groups = (await agent.models())["groups"]
            key = next((m["key"] for g in groups for m in g["models"]), "")
        try:
            provider, model_id = agent.provider_for(key)
        except ValueError as e:
            return error(400, str(e))
        try:
            answer = await asyncio.wait_for(review.ask(provider, model_id, carddraft.DRAFT_SYSTEM,
                                                       carddraft.draft_prompt(manual, str(body.get("name") or ""))), 300)
            text = carddraft.extract_yaml(answer)
            card, problems, warnings = carddraft.check(text)
            if problems:   # one round of repair, with the checker's own words
                answer = await asyncio.wait_for(review.ask(provider, model_id, carddraft.DRAFT_SYSTEM,
                                                           carddraft.repair_prompt(text, problems)), 300)
                text = carddraft.extract_yaml(answer)
                card, problems, warnings = carddraft.check(text)
        except asyncio.TimeoutError:
            return error(504, "The model did not finish the card within 5 minutes; try a shorter manual excerpt.")
        except Exception as e:
            return error(502, "The model could not draft it: %s" % (str(e).splitlines()[0][:200] if str(e) else type(e).__name__))
        notes = answer.split("```")[-1].strip() if answer.count("```") >= 2 else ""
        return JSONResponse({"yaml": text, "id": card.id if card else None, "problems": problems, "warnings": warnings,
                             "notes": notes[:800], "model": key})

    # ------------------------------------------------------------------ phones and remote access

    def phone_view() -> Dict[str, Any]:
        on, port = settings.phone
        live = remote.port
        return {"on": on, "port": port, "listening": live is not None, "error": remote.error,
                "addresses": addresses(live or port), "devices": settings.device_views(), "roles": list(ROLES),
                "firewall": firewall_state() if live is not None else None}

    async def start_phone() -> None:
        on, port = settings.phone
        if not on:
            await remote.stop()
            return
        try:
            await remote.start(guard, port)
        except OSError as e:
            remote.error = "Could not listen on port %d: %s" % (port, e.strerror or e)
        if remote.port is not None:
            log.info("phone access on: %s", ", ".join("%s (%s)" % (a["url"], a["kind"]) for a in addresses(remote.port))
                     or "port %d" % remote.port)

    async def phone(request: Request) -> Response:
        """GET: phone access and paired devices. POST {on, port}: switch phone access (this computer only)."""
        if request.method == "POST":
            body = await request.json()
            try:
                settings.set_phone(bool(body.get("on")), int(body.get("port") or settings.phone[1]))
            except (TypeError, ValueError) as e:
                return error(400, str(e))
            await start_phone()
        return JSONResponse(await asyncio.to_thread(phone_view))

    async def phone_direct(request: Request) -> Response:
        """Only for a phone that came through the Kelvin hub (relay.py): POST gives it its own token for reaching this
        computer directly, on the lab network or through Tailscale, when phone access is on; DELETE forgets it (the
        phone signed out of the Kelvin account)."""
        d = (request.scope.get("kelvin") or {}).get("device") or {}
        if d.get("via") != "hub" or not d.get("id"):
            return error(404, "not found")
        if request.method == "DELETE":
            return JSONResponse({"ok": settings.remove_phone(str(d["id"]))})
        if remote.port is None:
            return JSONResponse({"available": False,
                                 "reason": "Phone access is off on the Kelvin computer, so your phone reaches it "
                                           "through the Kelvin hub."})
        device, token = remote.issue(str(d.get("name") or "Phone"), "control", str(d["id"]))
        found = addresses(remote.port)
        return JSONResponse({"available": True, "token": token, "name": device["name"],
                             "computer": socket.gethostname(), "addresses": [a["url"] for a in found],
                             "routes": [{"url": a["url"], "kind": a["kind"]} for a in found]})

    async def phone_firewall(request: Request) -> Response:
        """Ask Windows to let phones through its firewall (Windows shows its own permission prompt; this computer only)."""
        if remote.port is None:
            return error(400, "Switch on phone access first.")
        ok, said = await asyncio.to_thread(allow_through_firewall, remote.port, sys.executable)
        log.info("phone access, firewall: %s", said)
        if not ok:
            return error(409, said)
        return JSONResponse(await asyncio.to_thread(phone_view))

    async def devices(request: Request) -> Response:
        """POST {name, role}: a one-time pairing code (and QR code) for a new phone."""
        body = await request.json()
        if remote.port is None:
            return error(400, "Switch on phone access first.")
        try:
            p = remote.start_pairing(str(body.get("name") or ""), str(body.get("role") or "watch"))
        except ValueError as e:
            return error(400, str(e))
        bases = [a["url"] for a in addresses(remote.port)]
        urls = [u + "/pair#" + p.code for u in bases]
        return JSONResponse({"code": p.code, "name": p.name, "role": p.role, "expires_in": int(p.expires - time.time()),
                             "urls": urls, "qr": QR(pairing_link(bases, p.code)).svg(5) if bases else None})

    async def pairing(request: Request) -> Response:
        state = remote.pairing_state(request.path_params["code"])
        return JSONResponse(state) if state else error(404, "no such pairing code")

    async def device(request: Request) -> Response:
        try:
            settings.remove_device(request.path_params["device_id"])
        except KeyError as e:
            return error(404, e.args[0])
        return JSONResponse({"ok": True})

    async def pair(request: Request) -> Response:
        """A phone offers its pairing code (no token yet) and gets its own token."""
        if (request.scope.get("kelvin") or {}).get("role") == "owner":
            return error(404, "pair from the phone, through the address Kelvin shows")
        body = await request.json()
        try:
            dev, token = remote.claim(str(body.get("code") or ""))
        except PermissionError as e:
            locked = "locked" in str(e)
            return error(429 if locked else 403, str(e))
        return JSONResponse({"token": token, "name": dev["name"], "role": dev["role"]})

    async def forget_me(request: Request) -> Response:
        """A paired phone unpairs itself."""
        d = (request.scope.get("kelvin") or {}).get("device")
        if not d:
            return error(404, "this is not a paired device")
        settings.remove_device(d["id"])
        return JSONResponse({"ok": True})

    async def me(request: Request) -> Response:
        k = request.scope.get("kelvin") or {}
        d = k.get("device") or {}
        out = {"role": k.get("role") or "owner", "name": d.get("name"), "lab": runtime.lab.config.name}
        if d:   # for the Kelvin phone app: which computer this is, and its other addresses (Tailscale away from the lab)
            found = addresses(remote.port) if remote.port else []
            out.update(computer=socket.gethostname(), version=__version__, api=PHONE_API,
                       addresses=[a["url"] for a in found], routes=[{"url": a["url"], "kind": a["kind"]} for a in found])
        return JSONResponse(out)

    # ------------------------------------------------------------------ reports

    async def reports_view(request: Request) -> Response:
        """GET: recent reports. POST {scope: chat|project|today|last|run, chat_id, project_id, run, last, title,
        summary, format}: write one."""
        if request.method == "GET":
            return JSONResponse({"reports": runtime.reports_index(), "folder": str(runtime.reports_dir)})
        body = await request.json()
        scope = str(body.get("scope") or "")
        notes: List[Any] = []
        references: List[str] = []
        title = str(body.get("title") or "")
        try:
            if scope in ("chat", "project"):
                chat = store.load(str(body.get("chat_id") or ""))
                chats, project = agent.chats_for_report(chat) if scope == "project" else ([chat], None)
                runs = [r for r in runs_in(chats) if runtime.runs.find(r)]
                notes = agent.report_notes(project)
                references = agent.report_references(project)
                title = title or ("%s report" % project["name"] if project else chat.get("title") or "")
            elif scope == "today":
                runs = runtime.pick_runs(day="today")
            elif scope == "last":
                runs = runtime.pick_runs(last=max(1, min(50, int(body.get("last") or 5))))
            elif scope == "run":
                runs = runtime.pick_runs([str(body.get("run") or "")])
            else:
                return error(400, "scope must be chat, project, today, last or run")
        except KeyError as e:
            return error(404, e.args[0] if e.args else "not found")
        except (TypeError, ValueError) as e:
            return error(400, str(e))
        if not runs:
            return error(400, "There are no runs to report here yet.")
        try:
            files = await asyncio.to_thread(runtime.report, runs, title, str(body.get("summary") or ""),
                                            str(body.get("format") or "pdf"), notes, "human", references)
        except ValueError as e:
            return error(400, str(e))
        return JSONResponse({"files": [{k: v for k, v in f.items() if k != "path"} for f in files],
                             "runs": len(runs), "folder": str(runtime.reports_dir)})

    async def report_file(request: Request) -> Response:
        try:
            path = runtime.report_path(request.path_params["name"])
        except KeyError as e:
            return error(404, e.args[0])
        media = "application/pdf" if path.suffix == ".pdf" else \
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        return FileResponse(path, media_type=media, filename=path.name)

    async def run_file(request: Request) -> Response:
        """A run's report or its data (run.h5), to download."""
        try:
            path = runtime.run_file(request.path_params["run_id"], request.path_params["name"])
        except KeyError as e:
            return error(404, e.args[0])
        media = {".pdf": "application/pdf", ".h5": "application/x-hdf5",
                 ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}[path.suffix]
        return FileResponse(path, media_type=media, filename="%s-%s" % (request.path_params["run_id"], path.name))

    async def run_export(request: Request) -> Response:
        """A run's data to download, written by code (export.py): ?format=csv (data.csv, a zip with the traces' CSVs
        when it has traces), xlsx or npz. The files are kept in the run's folder too."""
        from .. import export
        try:
            folder = runtime._run_dir(request.path_params["run_id"])
            data, name, media = await asyncio.to_thread(export.download, folder,
                                                        request.query_params.get("format", "csv"))
        except KeyError as e:
            return error(404, e.args[0] if e.args else "no such run")
        except export.ExportError as e:
            return error(400, str(e))
        except ImportError:
            return error(400, "NumPy isn't installed here, so only CSV and Excel can be written")
        except OSError as e:
            return error(500, "Could not write the export: %s" % e)
        return Response(data, media_type=media, headers={"Content-Disposition": 'attachment; filename="%s"' % name})

    async def run_open(request: Request) -> Response:
        """Open a run's file (or, with no name, its folder) on this computer."""
        run_id, name = request.path_params["run_id"], request.path_params.get("name")
        try:
            path = runtime.run_file(run_id, name) if name else runtime._run_dir(run_id)
        except KeyError as e:
            return error(404, e.args[0])
        try:
            open_path(path)
        except OSError as e:
            return error(500, "Could not open it: %s" % e)
        return JSONResponse({"ok": True})

    async def report_open(request: Request) -> Response:
        """Open a report in this computer's PDF viewer or PowerPoint (Kelvin runs on the user's own computer)."""
        try:
            path = runtime.report_path(request.path_params["name"])
        except KeyError as e:
            return error(404, e.args[0])
        try:
            open_path(path)
        except OSError as e:
            return error(500, "Could not open it: %s" % e)
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------------ watches, alarms, notifications

    async def watches_view(request: Request) -> Response:
        """GET: watches and recent alarms. POST {key, above, below, for_s, note}: a person adds a watch."""
        if request.method == "POST":
            body = await request.json()

            def num(name: str) -> Optional[float]:
                v = body.get(name)
                return None if v in (None, "") else float(v)
            try:
                view = runtime.add_watch(str(body.get("key") or ""), num("above"), num("below"), num("for_s") or 0.0,
                                         str(body.get("note") or ""), source="person")
            except KeyError as e:
                return error(404, e.args[0] if e.args else "no such reading")
            except (TypeError, ValueError) as e:
                return error(400, str(e))
            return JSONResponse(view)
        return JSONResponse({"watches": runtime.monitor.list(), "alarms": [a.view() for a in runtime.monitor.alarms[:20]]})

    async def watch_view(request: Request) -> Response:
        try:
            runtime.remove_watch(request.path_params["watch_id"], source="person")
        except KeyError as e:
            return error(404, e.args[0])
        except PermissionError as e:
            return error(403, str(e))
        return JSONResponse({"ok": True})

    async def alarm_ack(request: Request) -> Response:
        ok = runtime.monitor.acknowledge(request.path_params["alarm_id"])
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    async def notify_test(request: Request) -> Response:
        return JSONResponse({"result": await asyncio.to_thread(runtime.notifier.test)})

    # ------------------------------------------------------------------ the lab Kelvin controls (people only)

    def busy_problem() -> Optional[str]:
        if agent.answering():
            return "Kelvin is answering; wait for it or press stop first."
        live = runtime.live
        if live and live.get("status") == "running":
            return "A plan is running; wait for it to finish or stop it first."
        return None

    async def lab_view(request: Request) -> Response:
        """GET: which lab Kelvin controls, the user's lab config and the instrument cards.
        POST {yaml}: check, save and connect the user's lab. POST {use: "virtual" | "mine"}: switch."""
        if request.method == "GET":
            cards = [{"id": c.id, "model": c.model, "kind": c.kind, "vendor": c.vendor, "status": c.status,
                      "identifies": bool(c.idn_pattern)}
                     for c in sorted(runtime.lab.registry, key=lambda c: (c.kind, c.id)) if not c.id.startswith("virtual/")]
            return JSONResponse({"name": runtime.lab.config.name, "info": runtime.lab_info, "cards": cards,
                                 "yaml": lab_file.read_text(encoding="utf-8") if lab_file.exists() else "",
                                 "unreviewed": runtime.lab.config.unreviewed(), "setup": setup.status,
                                 "folders": instrument_folders()})
        body = await request.json()
        problem = busy_problem()
        if problem:
            return error(409, problem)
        use = body.get("use")
        by = "human (portal)"
        if use not in ("virtual", "mine"):   # a new lab config, or limits marked reviewed: the owner's, when set
            refused = needs_owner(body)
            if refused:
                return refused
        if body.get("reviewed"):   # a person reviewed the limits of the instruments Kelvin added
            if not lab_file.exists():
                return error(400, "There is no lab config to review.")
            text = mark_reviewed(lab_file.read_text(encoding="utf-8"))
            if runtime.lab_info.get("kind") == "file":
                try:
                    cfg = load_lab_config(text, lab_registry(lab_file.parent))
                except ConfigError as e:
                    return error(400, "The lab config has problems: " + "; ".join(e.errors[:6]))
                if same_setup(cfg, runtime.lab.config):   # only Kelvin's marks go: nothing to connect again
                    tmp = lab_file.with_suffix(".tmp")
                    tmp.write_text(text, encoding="utf-8")
                    tmp.replace(lab_file)
                    await asyncio.to_thread(runtime.mark_reviewed, "human (portal)")
                    return JSONResponse({"ok": True, "name": runtime.lab.config.name, "info": runtime.lab_info,
                                         "health": [], "unreviewed": []})
            body, use, by = {"yaml": text}, None, "human (portal, reviewed)"
        try:
            if use == "virtual":
                lab, envelope, info = await asyncio.to_thread(open_home_lab, lab_file.parent, "virtual", 10,
                                                              settings.switched_off, runtime.collection)
            else:
                text = str(body.get("yaml") or "") if use != "mine" else (
                    lab_file.read_text(encoding="utf-8") if lab_file.exists() else "")
                if not text.strip():
                    return error(400, "There is no lab config yet; scan for instruments or write one first.")
                registry = lab_registry(lab_file.parent, runtime.collection)   # Kelvin's, the collection's, the user's
                load_lab_config(text, registry)   # check it before touching any instrument
                # opened before connecting: the switch closes the old lab's instruments first and keeps the ones
                # that are unchanged (runtime.switch_lab), as a camera or a maker's SDK takes one connection
                lab = await asyncio.to_thread(build_lab, text, None, 0, registry, settings.switched_off, True, True)
                envelope, info = None, {"kind": "file", "path": str(lab_file), "error": None, "has_file": True}
                if use != "mine":
                    tmp = lab_file.with_suffix(".tmp")
                    tmp.write_text(text, encoding="utf-8")
                    tmp.replace(lab_file)
        except ConfigError as e:
            return error(400, "The lab config has problems: " + "; ".join(e.errors[:6]))
        except (AdapterError, OSError, ValueError) as e:
            from .. import envs
            return JSONResponse({"error": envs.for_people(str(e).splitlines()[0]) if str(e) else type(e).__name__,
                                 "install": envs.needed(str(e))}, status_code=400)
        try:
            health = await asyncio.to_thread(runtime.switch_lab, lab, envelope, info, by)
        except RuntimeError as e:
            lab.close()
            return error(409, str(e))
        settings.update({"lab": "virtual" if use == "virtual" else "mine"})
        if use != "virtual" and auto_setup:   # give new instruments their folders, settings and manuals
            setup.start("lab changed")
        return JSONResponse({"ok": True, "name": lab.config.name, "info": runtime.lab_info, "health": health,
                             "unreviewed": lab.config.unreviewed()})

    async def lab_install(request: Request) -> Response:
        """POST {env, packages}: install the makers' packages the lab's instruments need, from the Install button
        shown where the lab can't connect because one is missing. The person approves what is downloaded, with
        its versions and sources, as for Kelvin's own installs (envs.py); then the Lab panel connects again."""
        body = await request.json()
        packages = body.get("packages")
        if not isinstance(packages, list) or not packages or not all(isinstance(p, str) for p in packages):
            return error(400, "Say which packages to install.")
        problem = busy_problem()
        if problem:
            return error(409, problem)
        said = await asyncio.to_thread(runtime.install_for_instrument, str(body.get("env") or ""), packages,
                                       why="your lab's instruments", by="human (portal)")
        return JSONResponse({"ok": said.startswith("installed"), "message": said})

    def environment_rows() -> List[Dict[str, Any]]:
        from .. import envs
        rows = []
        for rec in envs.listing(home):
            folder, size = envs.env_dir(rec["name"], home), 0
            for root, _, files in os.walk(folder):
                for f in files:
                    try:
                        size += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
            rows.append({"name": rec["name"], "python": rec.get("python"), "bits": rec.get("bits", 64),
                         "packages": rec.get("packages") or {}, "files": rec.get("files") or [],
                         "for": rec.get("for") or [], "mb": round(size / 1e6, 1), "folder": str(folder)})
        return rows

    async def environments_view(request: Request) -> Response:
        """GET: what Kelvin installed for instruments, each in its environment (envs.py). POST {remove: name}: a
        person removes one in Settings; instruments that use it stop connecting until it is installed again."""
        from .. import envs
        if request.method == "GET":
            return JSONResponse({"environments": await asyncio.to_thread(environment_rows)})
        body = await request.json()
        name = str(body.get("remove") or "")
        problem = busy_problem()
        if problem:
            return error(409, problem)
        try:
            rec = envs.info(name, home)
        except ValueError as e:
            return error(400, str(e))
        if rec is None:
            return error(404, "There is no environment %s." % name)
        whole = await asyncio.to_thread(envs.remove, name, home)
        runtime.gate.log.write("environment_removed", by="human (portal)", env=name)
        return JSONResponse({"ok": True, "message": "Removed %s%s." % (
            name, "" if whole else "; some of its files are in use, and go when Kelvin next starts")})

    def instrument_folders() -> List[Dict[str, Any]]:
        out = []
        for name, inst in runtime.lab.instruments.items():
            if inst.simulated:
                continue
            p = profiles.info(home, name)
            m = p.get("manual") or {}
            out.append({"name": name, "model": inst.card.model, "folder": p["folder"], "identity": p.get("identity"),
                        "parsed": p.get("parsed") or {}, "address": inst.config.address,
                        "answered_with": p.get("answered_with"), "manual": p.get("has_manual"),
                        "manual_pages": m.get("pages"), "manual_file": m.get("file"), "manual_from": m.get("url"),
                        "manual_title": inst.card.manual, "manual_url": inst.card.manual_url,
                        "settings_at": p.get("settings_read_at")})
        return out

    def real_instrument(name: str) -> Any:
        inst = runtime.lab.instruments.get(name)
        if inst is None or inst.simulated:
            raise KeyError("no instrument %r on this computer" % name)
        return inst

    async def setup_view(request: Request) -> Response:
        """GET: where setting up the connected instruments is. POST: a person asks Kelvin to set them up again."""
        if request.method == "POST":
            problem = busy_problem()
            if problem:
                return error(409, problem)
            setup.start("asked")
        return JSONResponse(dict(setup.status, folders=instrument_folders()))

    async def instrument_manual(request: Request) -> Response:
        """POST: fetch the manual from the maker ({force: true} fetches it again). PUT with a PDF body: use this one."""
        name = request.path_params["name"]
        try:
            inst = real_instrument(name)
        except KeyError as e:
            return error(404, e.args[0])
        try:
            if request.method == "PUT":
                data = await request.body()
                if data[:5] != b"%PDF-":
                    return error(400, "That is not a PDF.")
                if len(data) > profiles.MAX_MANUAL:
                    return error(400, "The PDF is too large.")
                manual = await asyncio.to_thread(profiles.save_manual, home, name, inst.card, data)
            else:
                raw = await request.body()
                force = bool(json.loads(raw).get("force")) if raw.strip() else False
                manual = await asyncio.to_thread(profiles.fetch_manual, home, name, inst.card, profiles.http_get, force)
        except (LookupError, ValueError, OSError, RuntimeError) as e:
            return error(400, str(e).splitlines()[0] if str(e) else type(e).__name__)
        return JSONResponse({"ok": True, "manual": manual, "folders": instrument_folders()})

    async def folder_view(request: Request) -> Response:
        """A chat's folder, where its runs go. GET ?project=: where a new chat (in that project) starts, the data
        folder's runs, and the folders this person chose lately. POST {path, project}: a folder the person picked
        for a new chat, checked and kept (the chat is made at its first message); path null: the data folder.
        POST {open: true, chat or project}: show it on this computer."""
        if request.method == "GET":
            return JSONResponse({"folder": suggested_folder(request.query_params.get("project") or None),
                                 "data": str(runtime.data_dir / "runs"), "recent": settings.recent_folders})
        body = await request.json()
        project = body.get("project") or None
        try:
            if body.get("open"):
                chat_id = str(body.get("chat") or "")
                where = Path((store.load(chat_id).get("folder") if chat_id else suggested_folder(project))
                             or runtime.data_dir / "runs")
                if where == runtime.data_dir / "runs":
                    where.mkdir(parents=True, exist_ok=True)   # before the first run
                open_path(where)
                return JSONResponse({"ok": True})
            if not str(body.get("path") or "").strip():   # new chats start in the data folder again
                keep_folder(None, project)
                return JSONResponse({"folder": str(runtime.data_dir / "runs")})
            path = await asyncio.to_thread(chat_folder, body.get("path"))
        except KeyError as e:
            return error(404, e.args[0])
        except ValueError as e:
            return error(400, str(e))
        except OSError as e:
            return error(500, "Could not open it: %s" % (e.strerror or e))
        keep_folder(path, project)
        return JSONResponse({"folder": path})

    async def data_folder(request: Request) -> Response:
        """GET: where runs, the lab record and reports are kept. POST {path, move}: keep them somewhere else.
        POST {open: true}: show the folder on this computer."""
        if request.method == "POST":
            body = await request.json()
            if body.get("open"):
                try:
                    open_path(runtime.data_dir)
                except OSError as e:
                    return error(500, "Could not open it: %s" % e)
                return JSONResponse({"ok": True})
            problem = busy_problem()
            if problem:
                return error(409, problem)
            try:
                result = await asyncio.to_thread(runtime.set_data_dir, str(body.get("path") or "").strip(),
                                                 bool(body.get("move")), "human (portal)")
            except (ValueError, RuntimeError, OSError) as e:
                return error(400, str(e))
            settings.set_data_dir(result["path"])
            return JSONResponse(dict(result, default=str(user_default_data()), problem=None))
        return JSONResponse(dict(runtime.data_summary(), default=str(user_default_data()),
                                 problem=getattr(runtime, "data_problem", None)))

    async def terminal(request: Request) -> Response:
        """A command a person typed in the terminal pane: {instrument, op: raw|get|set|describe|manual, arg, value}.
        Same gate as Kelvin's own commands; waits (like them) while an approval is open."""
        body = await request.json()
        instrument, op = str(body.get("instrument") or ""), str(body.get("op") or "raw")
        arg = str(body.get("arg") or "")
        if len(arg) > 2000:
            return error(400, "That command is too long.")
        result = await asyncio.to_thread(runtime.terminal, instrument, op, arg, body.get("value"))
        return JSONResponse(result)

    async def instrument_switch(request: Request) -> Response:
        """POST {on}: a person switches an instrument on or off for this lab. Off, Kelvin sends it nothing at all:
        no command, no reading, no health check, no going safe, until a person switches it on again."""
        name = request.path_params["name"]
        on = (await request.json()).get("on") is True
        try:
            await asyncio.to_thread(runtime.switch_instrument, name, on, "human (portal)")
        except KeyError as e:
            return error(404, e.args[0])
        except RuntimeError as e:
            if isinstance(e, AdapterError):
                from .. import envs
                return error(400, "%s could not be connected: %s" % (name, envs.for_people(str(e).splitlines()[0])))
            return error(409, str(e))
        except (OSError, ValueError) as e:
            return error(400, "%s could not be connected: %s" % (name, str(e).splitlines()[0] if str(e) else e))
        settings.set_switched_off(runtime.lab.config.name, runtime.lab.switched_off)
        return JSONResponse({"ok": True, "instruments": runtime.instruments()})

    async def instrument_open(request: Request) -> Response:
        """Open an instrument's folder, or its manual, on this computer."""
        name = request.path_params["name"]
        try:
            real_instrument(name)
            base = profiles.folder(home, name)
        except KeyError as e:
            return error(404, e.args[0])
        what = (await request.json()).get("what")
        if what == "manual":
            files = [base / n for n in ("manual.pdf", "manual.html") if (base / n).exists()]
            if not files:
                return error(404, "No manual for %s yet." % name)
            target = files[0]
        else:
            base.mkdir(parents=True, exist_ok=True)
            target = base
        try:
            open_path(target)
        except OSError as e:
            return error(500, "Could not open it: %s" % e)
        return JSONResponse({"ok": True})

    async def scan_view(request: Request) -> Response:
        """Find connected instruments. Only asks each instrument who it is."""
        problem = busy_problem()
        if problem:
            return error(409, problem)
        try:
            found = await asyncio.to_thread(runtime.scan_found)
            return JSONResponse({"found": found, "survey": runtime.last_survey})
        except AdapterError as e:
            return error(503, str(e))

    async def card_for_found(request: Request) -> Response:
        """A person asks Kelvin to write the card for an instrument a scan found: {address, idn}."""
        body = await request.json()
        found = {"address": str(body.get("address") or ""), "idn": str(body.get("idn") or "")}
        if not found["idn"]:
            return error(400, "That instrument did not say who it is, so Kelvin can't look up its manual.")
        r = await asyncio.to_thread(write_card, found)
        if r.get("error"):
            return error(400, r["error"])
        return JSONResponse({k: r.get(k) for k in ("card_id", "model", "kind", "changed", "by")} |
                            {"manual_url": (r.get("manual") or {}).get("url")})

    async def draft_view(request: Request) -> Response:
        """Add chosen instruments [{name, card, address}] to a lab config text (`base`), keeping what is there."""
        body = await request.json()
        items = []
        for it in body.get("items") or []:
            name, card, address = str(it.get("name") or "").strip(), str(it.get("card") or ""), str(it.get("address") or "").strip()
            if not name.isidentifier():
                return error(400, "%r is not a usable name (letters, digits and _ only)" % name)
            if not address:
                return error(400, "%s needs an address" % name)
            try:
                spec_card = runtime.script_cards().get(card)
            except KeyError:
                return error(400, "%s: choose a card" % name)
            item = {"name": name, "card": card, "address": address, "note": str(it.get("note") or "")[:60]}
            limits = it.get("limits")   # from the lab's scripts: the values they use (the person reviews them)
            if isinstance(limits, dict):
                item["limits"] = {str(p): [float(v[0]), float(v[1])] for p, v in limits.items()
                                  if str(p) in spec_card.params and isinstance(v, (list, tuple)) and len(v) == 2
                                  and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)}
            if isinstance(it.get("connection"), dict):   # the scripts' baud rate, line endings, timeout
                item["connection"] = {str(k): v for k, v in it["connection"].items()
                                      if k in ("baud_rate", "read_termination", "write_termination", "timeout_ms",
                                               "data_bits", "stop_bits", "parity", "query_delay")}
            items.append(item)
        base = str(body.get("base") or "")
        there = config_lists(base)   # in the config already (added twice, or found by a scan and in the scripts): kept once
        already = [{"item": k, "name": there[instrument_key(i)], "address": i["address"]}
                   for k, i in enumerate(items) if instrument_key(i) in there]
        return JSONResponse({"yaml": add_to_config(base, items, runtime.script_cards()), "already": already})

    async def scripts_view(request: Request) -> Response:
        """The lab's own scripts, read (never run) to set the lab up the way it already works: {folder}. Each
        instrument comes with its likely card, the limits from the values the scripts use, and who answers at its
        address now (asked as a scan asks)."""
        problem = busy_problem()
        if problem:
            return error(409, problem)
        body = await request.json()
        folder = str(body.get("folder") or "").strip()
        if not folder:
            return error(400, "Choose the folder with your scripts.")
        try:
            return JSONResponse(await asyncio.to_thread(runtime.scripts_found, folder))
        except (OSError, ValueError) as e:
            return error(400, str(e))

    def limits_view(name: str) -> Dict[str, Any]:
        inst = runtime.lab.instruments[name]
        ic = runtime.lab.config.instruments[name]
        rows = [{"param": s.name, "label": s.title, "unit": s.unit, "drive": s.is_drive,
                 "vendor": list(s.limits) if s.limits else None,
                 "lab": list(ic.limits[s.name]) if s.name in ic.limits else None,
                 "safe": s.safe if isinstance(s.safe, (int, float)) and not isinstance(s.safe, bool) else None}
                for s in inst.card.params.values() if s.numeric and s.set_cmd is not None]
        rows.sort(key=lambda r: not r["drive"])   # what drives the sample first
        return {"instrument": name, "model": inst.card.model, "rows": rows,
                "reviewed": name not in runtime.lab.config.unreviewed(),
                "editable": runtime.lab_info.get("kind") == "file" and lab_file.exists()}

    async def instrument_limits(request: Request) -> Response:
        """GET: an instrument's numeric settings, with the card's limits and the lab's. POST {limits: {param:
        [low, high] or null}, reviewed}: change the lab's in the lab config, at once (a person's; no Kelvin tool
        reaches this). reviewed: the person checked them, so Kelvin's mark on an instrument it added goes."""
        name = request.path_params["name"]
        if name not in runtime.lab.instruments:
            return error(404, "no instrument %r in the lab" % name)
        if request.method == "GET":
            return JSONResponse(limits_view(name))
        if runtime.lab_info.get("kind") != "file" or not lab_file.exists():
            return error(409, "Limits are set for your own lab (My instruments); the virtual lab's are fixed.")
        body = await request.json()   # any time, a run or a chat going on too: the gate checks each change against them
        refused = needs_owner(body)
        if refused:
            return refused
        card = runtime.lab.instruments[name].card
        changes: Dict[str, Any] = {}
        for p, lim in (body.get("limits") or {}).items():
            spec = card.params.get(str(p))
            if spec is None or not spec.numeric or spec.set_cmd is None:   # the settings limits_view lists
                return error(400, "%s has no numeric setting %r" % (name, p))
            if lim is None:
                changes[spec.name] = None
                continue
            if not (isinstance(lim, (list, tuple)) and len(lim) == 2 and all(
                    isinstance(x, (int, float)) and not isinstance(x, bool) for x in lim) and lim[0] <= lim[1]):
                return error(400, "%s: give the lowest and the highest value, lowest first" % spec.title)
            changes[spec.name] = [float(lim[0]), float(lim[1])]
        reviewed = bool(body.get("reviewed"))
        try:
            text = set_limits(lab_file.read_text(encoding="utf-8"), name, changes, reviewed)
            cfg = load_lab_config(text, lab_registry(lab_file.parent))   # a limit beyond the card's is refused here
        except KeyError as e:
            return error(404, e.args[0])
        except ConfigError as e:
            return error(400, "; ".join(e.errors[:3]))
        tmp = lab_file.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(lab_file)
        ic = runtime.lab.config.instruments[name]   # the running lab's: in force at once, no reconnecting
        ic.limits = dict(cfg.instruments[name].limits)
        if reviewed:
            ic.added_by = ""
        runtime.gate.log.write("limits", by="human (portal)", instrument=name,
                               limits={p: list(v) for p, v in ic.limits.items()}, reviewed=reviewed)
        return JSONResponse(limits_view(name))

    async def wake_view(request: Request) -> Response:
        """DELETE: the person cancels the chat's wake-up (Kelvin would have continued it later)."""
        chat_id = request.path_params["chat_id"]
        try:
            store.load(chat_id)   # only the signed-in person's chats
        except KeyError as e:
            return error(404, e.args[0])
        return JSONResponse({"ok": agent.waker.cancel(chat_id)})

    async def media(request: Request) -> Response:
        """A picture or file kept with a chat: a plot look_at drew, a photo or PDF the person attached."""
        from .store import MEDIA_TYPES
        name = request.path_params["name"]
        try:
            data = store.media(request.path_params["chat_id"], name)
        except KeyError as e:
            return error(404, e.args[0])
        return Response(data, media_type=MEDIA_TYPES[name.rsplit(".", 1)[-1]],
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})   # named by its content

    async def attachments(request: Request) -> Response:
        """POST the file's bytes (X-File-Name: its name, URL-encoded): kept with the chat for the next message."""
        from urllib.parse import unquote
        chat_id = request.path_params["chat_id"]
        data = await request.body()
        if not data:
            return error(400, "The file is empty.")
        if len(data) > ATTACH_MAX:
            return error(413, "Kelvin takes files up to %d MB." % (ATTACH_MAX // 2 ** 20))
        try:
            store.load(chat_id)
            return JSONResponse(await asyncio.to_thread(agent.attach, chat_id,
                                                        unquote(request.headers.get("x-file-name", "")), data))
        except KeyError as e:
            return error(404, e.args[0])
        except ValueError as e:
            return error(400, str(e))

    async def hard_stop(chat_id: str, request: Request) -> None:
        """A typed STOP: this chat's answer and the lab's work, whoever started it, as the Stop for everything."""
        agent.stop(chat_id)
        await asyncio.to_thread(runtime.stop, person(request)[:-1] + ", typed STOP)")

    async def turn(request: Request) -> Response:
        chat_id = request.path_params["chat_id"]
        body = await request.json()
        text = str(body.get("text") or "").strip()
        if not text:
            return error(400, "Say something first.")
        if typed_stop(text):   # the lab stops now; the model is not asked
            try:
                store.load(chat_id)   # only the signed-in person's chats
            except KeyError as e:
                return error(404, e.args[0])
            await hard_stop(chat_id, request)
            said = [{"type": "note", "text": STOPPED}, {"type": "turn_end", "reason": "stopped"}]
            return Response("".join(json.dumps(e) + "\n" for e in said), media_type="application/x-ndjson",
                            headers={"Cache-Control": "no-store"})
        model = str(body.get("model") or "")
        try:
            attached = agent.check_attachments(chat_id, body.get("attachments"))
        except KeyError:
            return error(400, "An attached file is no longer there: attach it again.")
        try:
            provider = agent.claim(chat_id, model)
        except Busy as e:
            return error(409, str(e))
        except (KeyError, ValueError) as e:
            return error(400, e.args[0])
        queue: asyncio.Queue = asyncio.Queue()
        unseen = unseen_pictures(provider, attached)

        async def emit(event: Dict[str, Any]) -> None:
            await queue.put(event)
            if unseen and event.get("type") == "turn_start":   # said at once, before the model answers
                await queue.put({"type": "note", "text": unseen})

        async def run() -> None:
            try:
                await agent.run_turn(chat_id, text, model, provider, emit, attachments=attached)
            finally:
                await queue.put(None)

        task = asyncio.ensure_future(run())  # keeps going if the browser disconnects
        tasks.append(task)
        task.add_done_callback(lambda t: tasks.remove(t) if t in tasks else None)

        async def events():
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield json.dumps(event, ensure_ascii=False) + "\n"

        return StreamingResponse(events(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def steer(request: Request) -> Response:
        """POST {text}: what the person wrote while Kelvin works on this chat; Kelvin reads it at its next step.
        409 when Kelvin isn't working on it (the portal then sends it as a new message)."""
        text = str((await request.json()).get("text") or "").strip()
        if not text:
            return error(400, "Say something first.")
        if len(text) > 20000:
            return error(400, "That message is too long.")
        if typed_stop(text):   # not for Kelvin's next step: now
            await hard_stop(request.path_params["chat_id"], request)
            return JSONResponse({"ok": True, "stopped": True, "message": STOPPED})
        if not agent.steer(request.path_params["chat_id"], text):
            return error(409, "Kelvin isn't working on this chat now: send it as a new message.")
        return JSONResponse({"ok": True})

    def stream_events(work: Any) -> Response:
        """Run `work(emit)` to the end even if the browser goes away; stream its events as NDJSON."""
        queue: asyncio.Queue = asyncio.Queue()

        async def run() -> None:
            try:
                await work(queue.put)
            finally:
                await queue.put(None)

        task = asyncio.ensure_future(run())
        tasks.append(task)
        task.add_done_callback(lambda t: tasks.remove(t) if t in tasks else None)

        async def events():
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield json.dumps(event, ensure_ascii=False) + "\n"

        return StreamingResponse(events(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def compare(request: Request) -> Response:
        """Several models answer one message side by side (reading the lab only)."""
        chat_id = request.path_params["chat_id"]
        body = await request.json()
        text = str(body.get("text") or "").strip()
        if not text:
            return error(400, "Say something first.")
        models = body.get("models") if isinstance(body.get("models"), list) else []
        try:
            attached = agent.check_attachments(chat_id, body.get("attachments"))
        except KeyError:
            return error(400, "An attached file is no longer there: attach it again.")
        try:
            lanes = agent.claim_compare(chat_id, models)
        except Busy as e:
            return error(409, str(e))
        except (KeyError, ValueError) as e:
            return error(400, e.args[0])
        return stream_events(lambda emit: agent.run_compare(chat_id, text, lanes, emit, attached))

    async def choose(request: Request) -> Response:
        body = await request.json()
        try:
            return JSONResponse(agent.choose(request.path_params["chat_id"], int(body.get("index", -1))))
        except Busy as e:
            return error(409, str(e))
        except KeyError as e:
            return error(404, e.args[0] if e.args else "no such chat")
        except (TypeError, ValueError) as e:
            return error(400, str(e))

    async def second_opinion(request: Request) -> Response:
        """Another model reviews the chat's latest answer, checking the lab's records. {recheck: true}: the model
        checks the findings of the second opinion the answer has."""
        body = await request.json()
        try:
            review = agent.recheck_review if body.get("recheck") else agent.review_answer
            opinion = await review(request.path_params["chat_id"], str(body.get("model") or ""))
        except Busy as e:
            return error(409, str(e))
        except KeyError as e:
            return error(404, e.args[0] if e.args else "no such chat")
        except ValueError as e:
            return error(400, str(e))
        except asyncio.TimeoutError:
            return error(504, "The reviewer did not answer within 5 minutes.")
        except Exception as e:
            return error(502, "The reviewer could not answer: %s" % str(e).splitlines()[0][:200] if str(e) else type(e).__name__)
        return JSONResponse(opinion)

    async def stop(request: Request) -> Response:
        """{chat_id}: stop that chat's answer (and the lab's work when it is that chat's). Without one, the Stop for
        everything: every chat's answer and the lab's work (its ramp or run, the queue, open approvals), also when no
        chat is answering, as a queue runs by itself. {chat_id, lab: true}: that chat's answer and the lab's work (a
        typed STOP)."""
        try:
            body = await request.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        chat_id = body.get("chat_id")
        stopped = agent.stop(str(chat_id) if chat_id else None)
        if not chat_id or body.get("lab"):
            await asyncio.to_thread(runtime.stop, person(request))
            stopped = True
        return JSONResponse({"ok": stopped, "message": STOPPED if stopped and (not chat_id or body.get("lab")) else ""})

    async def incident(request: Request) -> Response:
        """The card Kelvin shows after a restart that left something behind (outputs on, runs or a queue cut short).
        POST {action: "safe"}: the outputs still on go to safe; {action: "dismiss"}: the card goes."""
        action = str((await request.json()).get("action") or "")
        if action == "safe":
            if role_of(request) == "watch":
                return error(403, "This phone can watch, not act. Use the Kelvin computer, or Hands on the sample.")
            lines = await asyncio.to_thread(runtime.incident_safe, person(request))
            return JSONResponse({"ok": True, "lines": lines})
        if action == "dismiss":
            runtime.dismiss_incident(person(request))
            return JSONResponse({"ok": True})
        return error(400, "action is safe or dismiss")

    async def approval(request: Request) -> Response:
        body = await request.json()
        if body.get("approve") and role_of(request) == "watch":
            return error(403, "This phone can watch and decline, not approve. Approve on the Kelvin computer.")
        ok = runtime.answer(request.path_params["request_id"], bool(body.get("approve")), person(request),
                            *channel(request))
        return JSONResponse({"ok": ok}, status_code=200 if ok else 409)

    async def state(request: Request) -> Response:
        data = await asyncio.to_thread(runtime.ui_state)  # may read instruments; keep the server responsive
        data["active"] = agent.active
        data["answering"] = agent.answering()   # every chat answering now: the portal marks them
        data["wakes"], data["woke"] = agent.waker.view(), agent.waker.latest()
        data["ask_scripts"] = setup.first and settings.lab_scripts is None   # the first setup: the lab's scripts?
        return JSONResponse(data)

    async def scripts_folder(request: Request) -> Response:
        """POST {folder}: a person's answer to the first setup's question, the folder of the scripts the lab runs its
        instruments with. The setup reads them (never runs them) for the names, settings and values the lab uses,
        and for instruments the scan can't tell; "" when there are none (not asked again)."""
        folder = str((await request.json()).get("folder") or "").strip()
        if not folder:
            settings.set_lab_scripts("")
            return JSONResponse({"ok": True, "now": False})
        try:
            answer = await asyncio.to_thread(setup.use_scripts, folder)
        except (OSError, ValueError) as e:
            return error(400, str(e))
        settings.set_lab_scripts(folder)
        return JSONResponse(dict(answer, ok=True))

    async def approval_mode(request: Request) -> Response:
        """Ask before every drive increase, or auto-approve low-risk ones. People only: no agent tool reaches this."""
        if request.method == "POST":
            body = await request.json()
            try:
                return JSONResponse(runtime.set_approval_mode(str(body.get("mode")), "human (portal)",
                                                              pin=body.get("pin")))
            except PermissionError as e:   # the lab owner's PIN (accounts.OwnerPin)
                return JSONResponse({"error": str(e), "pin": True}, status_code=403)
            except ValueError as e:
                return error(400, str(e))
        return JSONResponse(runtime.approval_mode())

    async def settings_view(request: Request) -> Response:
        if request.method == "POST":
            was_on = collection_on() is not None
            try:
                settings.update(await request.json())
            except ValueError as e:
                return error(400, str(e))
            configure_notifications()
            configure_literature()
            if collection_on() is not None and not was_on and loop_ref.get("collection") is not None:
                loop_ref["collection"].set()   # switched on: fetch it now
            agent.providers = make_providers(settings, demo, signin) if providers is None else providers
            relay_wake()   # phones through the hub may have been switched on or off
        return JSONResponse({"settings": settings.public(), "models": await agent.models()})

    async def run_data(request: Request) -> Response:
        """What the browser needs to plot a run: axes (outer first), scalar columns, the last trace per
        trace column, and the automatic analysis."""
        run_id = request.path_params["run_id"]
        try:
            rec = await asyncio.to_thread(rundata.load, runtime._run_dir(run_id))
            rows = rec.rows
            cols = runtime._columns(rec)
        except (KeyError, OSError, ValueError) as e:
            return error(404, str(e))
        axes = [{"key": a["key"], "unit": a["unit"]} for a in cols["axes"]]
        scalars = [{"key": m["key"], "unit": m["unit"]} for m in cols["measures"] if m["kind"] == "scalar"]
        traces = []
        for m in cols["measures"]:
            if m["kind"] != "trace" or not rows or not isinstance(rows[-1].get(m["key"]), list):
                continue
            ys = rows[-1][m["key"]]
            xs, ys = plot_points(trace_x(rows[-1], m["key"], len(ys)), ys, TRACE_POINTS)
            traces.append({"key": m["key"], "unit": m["unit"], "x": xs, "y": ys, "x_unit": m.get("x_unit", ""),
                           "row": len(rows) - 1})
        meta = rec.meta
        keys = [a["key"] for a in axes] + [m["key"] for m in scalars]

        def plain(v: Any) -> Any:   # JSON has no NaN or infinity
            return None if isinstance(v, float) and not math.isfinite(v) else v
        return JSONResponse({"run_id": run_id, "plan": run_id.split("-", 2)[-1], "axes": axes, "scalars": scalars,
                             "rows": [{k: plain(r.get(k)) for k in keys} for r in rows], "traces": traces,
                             "analysis": rec.analysis,
                             "plan_text": rec.plan_text,
                             "meta": {k: meta.get(k) for k in ("status", "reason", "points", "planned_points",
                                                                "duration_s", "warnings")},
                             "files": runtime.run_files(run_id)})

    async def live(request: Request) -> Response:
        """The running (or last) plan's rows since `since`, for the runs pane."""
        try:
            since = max(0, int(request.query_params.get("since", "0")))
        except ValueError:
            since = 0
        return JSONResponse({"live": runtime.live_view(since)})

    async def runs(request: Request) -> Response:
        return JSONResponse({"runs": await asyncio.to_thread(runtime.runs_index, 20)})

    async def stop_run(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if isinstance(body, dict) and body.get("queue"):   # the queue too: nothing else starts after this scan
            runtime.stop(person(request))
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": runtime.stop_run(person(request))})

    async def queue(request: Request) -> Response:
        """GET: the scan queue. POST {op, ...}: a person's change to it. start asks for the queue's approval as usual
        and returns once it is answered."""
        if request.method == "GET":
            return JSONResponse(runtime.queue_view())
        body = await request.json()
        op, by = str(body.get("op") or ""), "human"
        item = body.get("item")

        def persons(fn: Any, *args: Any) -> str:
            """A person's change, beside whatever Kelvin does (the check may read an instrument): filed under no
            chat, never under the one whose call changes the lab meanwhile."""
            with runtime.attributed({}):
                return fn(*args)
        if op == "add":
            pos = body.get("position")
            msg = await asyncio.to_thread(persons, runtime.queue_add, str(body.get("text") or ""),
                                          int(pos) if pos else None, str(body.get("note") or ""), by)
            ok = not msg.startswith("not added")
        elif op == "edit":
            msg = await asyncio.to_thread(persons, runtime.queue_edit, item, str(body.get("text") or ""), by)
            ok = not msg.startswith("not changed")
        elif op in ("move", "remove", "again", "clear"):
            msg = runtime.queue_change(op, item, body.get("position"), by)
            ok = not msg.startswith(("not changed", "unknown"))
        elif op == "start":
            msg = await asyncio.to_thread(runtime.queue_start, by)
            ok = msg.startswith("queue started")
        elif op == "pause":
            msg, ok = runtime.queue_pause(by), True
        elif op == "on_error":
            try:
                runtime.queue_set_on_error(str(body.get("value") or ""), by)
                msg, ok = "ok", True
            except ValueError as e:
                msg, ok = str(e), False
        elif op == "save":
            msg = runtime.queue_save(str(body.get("name") or ""))
            ok = msg.startswith("saved")
        elif op == "load":
            msg = await asyncio.to_thread(persons, runtime.queue_load, str(body.get("name") or ""), by)
            ok = msg.startswith("added")
        else:
            return error(400, "unknown queue operation %r" % op)
        if not ok:
            return error(400, msg)
        return JSONResponse(dict(runtime.queue_view(), message=msg))

    routes = [
        Route("/", index),
        Route("/m", mobile),
        Route("/pair", mobile),
        Route("/api/me", me),
        Route("/api/me/forget", forget_me, methods=["POST"]),
        Route("/api/pair", pair, methods=["POST"]),
        Route("/api/phone", phone, methods=["GET", "POST"]),
        Route("/api/phone/firewall", phone_firewall, methods=["POST"]),
        Route("/api/phone/direct", phone_direct, methods=["POST", "DELETE"]),
        Route("/auth/callback", auth_callback),
        Route("/api/chatgpt", chatgpt_view),
        Route("/api/chatgpt/signin", chatgpt_signin, methods=["POST"]),
        Route("/api/chatgpt/signout", chatgpt_signout, methods=["POST"]),
        Route("/api/chatgpt/welcomed", chatgpt_welcomed, methods=["POST"]),
        Route("/hub/callback", hub_callback),
        Route("/api/hub", hub_view),
        Route("/api/hub/connect", hub_connect, methods=["POST"]),
        Route("/api/hub/disconnect", hub_disconnect, methods=["POST"]),
        Route("/api/hub/sync", hub_sync, methods=["POST"]),
        Route("/api/services", services_view, methods=["GET", "POST"]),
        Route("/api/services/{service_id}", service_view, methods=["DELETE"]),
        Route("/api/services/{service_id}/test", service_test, methods=["POST"]),
        Route("/api/cards", cards_view, methods=["GET", "POST"]),
        Route("/api/cards/check", card_check, methods=["POST"]),
        Route("/api/cards/status", card_status, methods=["POST"]),
        Route("/api/cards/delete", card_delete, methods=["POST"]),
        Route("/api/cards/draft", card_draft, methods=["POST"]),
        Route("/api/cards/try", card_try, methods=["POST"]),
        Route("/api/devices", devices, methods=["POST"]),
        Route("/api/devices/pairing/{code}", pairing),
        Route("/api/devices/{device_id}", device, methods=["DELETE"]),
        Route("/api/bootstrap", bootstrap),
        Route("/api/session", session_view, methods=["GET", "POST", "DELETE"]),
        Route("/api/accounts", accounts_view, methods=["POST"]),
        Route("/api/accounts/remove", accounts_remove, methods=["POST"]),
        Route("/api/session/reset", session_reset, methods=["POST"]),
        Route("/api/session/hub", session_hub, methods=["POST"]),
        Route("/api/account/rekey", account_rekey, methods=["POST"]),
        Route("/api/account", account_view, methods=["POST", "DELETE"]),
        Route("/api/lab-log", lab_log),
        Route("/api/claude-code", claude_code_view),
        Route("/api/update", update_view, methods=["GET", "POST"]),
        Route("/api/collection", collection_view, methods=["GET", "POST"]),
        Route("/api/chats", chats, methods=["GET", "POST"]),
        Route("/api/chats/{chat_id}", chat, methods=["GET", "PATCH", "DELETE"]),
        Route("/api/chats/{chat_id}/turn", turn, methods=["POST"]),
        Route("/api/chats/{chat_id}/steer", steer, methods=["POST"]),
        Route("/api/chats/{chat_id}/compare", compare, methods=["POST"]),
        Route("/api/chats/{chat_id}/choose", choose, methods=["POST"]),
        Route("/api/chats/{chat_id}/review", second_opinion, methods=["POST"]),
        Route("/api/projects", projects, methods=["GET", "POST"]),
        Route("/api/projects/{project_id}", project, methods=["GET", "PATCH", "DELETE"]),
        Route("/api/projects/{project_id}/notes/{note_id}", note, methods=["DELETE"]),
        Route("/api/projects/{project_id}/papers/{paper_id}", paper, methods=["DELETE"]),
        Route("/api/projects/{project_id}/devices", devices_view, methods=["GET", "POST"]),
        Route("/api/projects/{project_id}/devices/{device_id}", device_view, methods=["PATCH"]),
        Route("/api/projects/{project_id}/summary", summary_view, methods=["PUT", "POST"]),
        Route("/api/mount", mount_view, methods=["GET", "POST"]),
        Route("/api/protection", protection_view),
        Route("/api/hands", hands_view, methods=["POST"]),
        Route("/api/memory", memory_view),
        Route("/api/memory/notes/{note_id}", memory_note, methods=["DELETE"]),
        Route("/api/know", know_view),
        Route("/api/know/menu", know_menu),
        Route("/api/know/{kind}/{scope}/{name}", know_item, methods=["DELETE", "POST"]),
        Route("/api/skills/import", skill_import, methods=["POST"]),
        Route("/api/skills/export/{scope}/{name}", skill_export),
        Route("/api/stop", stop, methods=["POST"]),
        Route("/api/approvals/{request_id}", approval, methods=["POST"]),
        Route("/api/incident", incident, methods=["POST"]),
        Route("/api/owner-pin", owner_pin, methods=["GET", "POST"]),
        Route("/api/state", state),
        Route("/api/mode", approval_mode, methods=["GET", "POST"]),
        Route("/api/lab", lab_view, methods=["GET", "POST"]),
        Route("/api/lab/install", lab_install, methods=["POST"]),
        Route("/api/environments", environments_view, methods=["GET", "POST"]),
        Route("/api/chats/{chat_id}/wake", wake_view, methods=["DELETE"]),
        Route("/api/chats/{chat_id}/media/{name}", media, methods=["GET"]),
        Route("/api/chats/{chat_id}/attachments", attachments, methods=["POST"]),
        Route("/api/reports", reports_view, methods=["GET", "POST"]),
        Route("/api/reports/tree", reports_tree),
        Route("/api/reports/refresh", reports_refresh, methods=["POST"]),
        Route("/api/reports/open-folder", reports_open_folder, methods=["POST"]),
        Route("/api/reports/{name:path}/open", report_open, methods=["POST"]),
        Route("/api/reports/{name:path}", report_file),
        Route("/api/watches", watches_view, methods=["GET", "POST"]),
        Route("/api/watches/{watch_id}", watch_view, methods=["DELETE"]),
        Route("/api/alarms/{alarm_id}/ack", alarm_ack, methods=["POST"]),
        Route("/api/notify/test", notify_test, methods=["POST"]),
        Route("/api/instruments/scan", scan_view, methods=["POST"]),
        Route("/api/instruments/scripts", scripts_view, methods=["POST"]),
        Route("/api/instruments/scripts-folder", scripts_folder, methods=["POST"]),
        Route("/api/instruments/{name}/limits", instrument_limits, methods=["GET", "POST"]),
        Route("/api/instruments/setup", setup_view, methods=["GET", "POST"]),
        Route("/api/terminal", terminal, methods=["POST"]),
        Route("/api/data-folder", data_folder, methods=["GET", "POST"]),
        Route("/api/folder", folder_view, methods=["GET", "POST"]),
        Route("/api/instruments/{name}/manual", instrument_manual, methods=["POST", "PUT"]),
        Route("/api/instruments/{name}/open", instrument_open, methods=["POST"]),
        Route("/api/instruments/{name}/switch", instrument_switch, methods=["POST"]),
        Route("/api/instruments/draft", draft_view, methods=["POST"]),
        Route("/api/instruments/card", card_for_found, methods=["POST"]),
        Route("/api/settings", settings_view, methods=["GET", "POST"]),
        Route("/api/quit", quit_app, methods=["POST"]),
        Route("/api/live", live),
        Route("/api/runs", runs),
        Route("/api/runs/stop", stop_run, methods=["POST"]),
        Route("/api/queue", queue, methods=["GET", "POST"]),
        Route("/api/runs/{run_id}", run_data),
        Route("/api/runs/{run_id}/open", run_open, methods=["POST"]),
        Route("/api/runs/{run_id}/files/{name}", run_file),
        Route("/api/runs/{run_id}/files/{name}/open", run_open, methods=["POST"]),
        Route("/api/runs/{run_id}/export", run_export),
        Route("/static/{name}", static),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.agent = agent
    app.state.remote = remote
    app.state.signin = signin
    app.state.hub = hub
    app.state.setup = setup
    if accounts is not None:   # one person without a password goes straight in; otherwise the sign-in screen
        runtime.shared_log = home / "instrument-log.jsonl"
        listed = accounts.list()
        if len(listed) == 1 and not listed[0]["password"]:
            sign_in(accounts.find(listed[0]["name"]), "")
        else:
            settings.use(None)
            signin.use(nobody)
            runtime.use_user(None, nobody)
            if providers is None:
                agent.providers = make_providers(settings, demo, signin)
    else:   # one person, as before accounts
        hub.use(home)
    def paired_with(token: str) -> Optional[str]:
        """Whose paired phone this is, among everyone with an account (phones are paired per person)."""
        from .remote import token_hash
        h = token_hash(token) if token else ""
        for a in accounts.list() if accounts is not None and h else []:
            try:
                mine = json.loads(accounts.settings_path(accounts.find(a["name"])).read_text(encoding="utf-8"))
            except (OSError, ValueError, KeyError):
                continue
            if any(isinstance(d, dict) and hmac.compare_digest(str(d.get("hash") or ""), h) for d in mine.get("devices") or []):
                return a["name"]
        return None

    relay_app["app"] = app

    guard = Guard(app, settings.access_key, hosts, remote,
                  signed_in=None if accounts is None else lambda: session["user"] is not None,
                  paired_with=None if accounts is None else paired_with,
                  operating=lambda: runtime.operating)
    return guard


def update_summary(updater: Any, mode: str) -> Dict[str, Any]:
    from .. import updater as updates
    return updates.view(updater, mode)


def claude_code_enabled() -> bool:
    from .. import claudecode   # Claude Code bridge (temporary)
    return claudecode.enabled()


def suggested_name() -> str:
    """The computer's name for this person, offered when the first account is made."""
    import getpass
    try:
        return getpass.getuser()[:40]
    except Exception:
        return ""


def lab_data_folder(home: Path, lab: Any, info: Optional[Dict[str, Any]] = None, key: Optional[str] = None,
                    record: bool = True) -> Path:
    """The folder a lab keeps its data in when nobody chose one: virtual-lab for the virtual lab, else its own,
    named after it (labfolders.py). info: the lab's open_home_lab info (its kind and config file)."""
    from .. import labfolders
    info = info or {}
    virtual = info.get("kind") == "virtual" if info.get("kind") else all(i.simulated for i in lab.instruments.values())
    where = info.get("path") or key
    return labfolders.folder_for(home, lab.config.name, virtual, str(Path(where).resolve()) if where else None,
                                 record=record)


def data_folder_for(settings: Settings, home: Path, default: Optional[Path] = None) -> Any:
    """The data folder the user chose in Settings, if Kelvin can write there now; otherwise the default, and why."""
    default = default or home / "virtual-lab"
    chosen = settings.get("data_dir")
    if not chosen:
        return default, None
    try:
        Path(chosen).mkdir(parents=True, exist_ok=True)
        probe = Path(chosen) / ".kelvin-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return Path(chosen), None
    except OSError as e:
        return default, ("Your data folder %s can't be written to now (%s), so Kelvin saves to %s until it can."
                         % (chosen, e.strerror or e, default))


def main(argv: Optional[List[str]] = None) -> None:
    import uvicorn

    ap = argparse.ArgumentParser(prog="python -m labharness.web", description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--data-dir", help="runs, the lab record and reports (default: the folder chosen in Settings, "
                                       "else ~/Kelvin/virtual-lab)")
    ap.add_argument("--settings", default=str(Path.home() / "Kelvin" / "settings.json"))
    ap.add_argument("--speed", type=float, default=10,
                    help="virtual lab time: N× real time (default 10, so runs can be watched); 0 = instant")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--approval-timeout", type=float, default=300)
    ap.add_argument("--demo", action="store_true", help="add a scripted model so the portal works without an API key")
    ap.add_argument("--lab", help="lab config YAML using sim:// instruments (default: the built-in virtual lab)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-setup", action="store_true", help="do not look for connected instruments on start")
    ap.add_argument("--no-accounts", action="store_true",
                    help="one person, no sign-in (also the case with --data-dir)")
    args = ap.parse_args(argv)

    settings = Settings(args.settings)
    home = Path(args.settings).parent
    info = None
    if args.lab:  # an explicit lab config: simulated ones run at --speed, real ones on real time
        simulated = is_simulated(load_lab_config(args.lab, builtin_registry()))
        lab = build_lab(args.lab, clock=(SimClock() if args.speed == 0 else ScaledClock(args.speed)) if simulated else None,
                        seed=args.seed, switched_off=settings.switched_off)
        envelope = None
    else:   # the user's lab.yaml next to the settings, if they have one; otherwise the virtual lab
        lab, envelope, info = open_home_lab(home, settings.get("lab") or "auto", args.speed, settings.switched_off,
                                            Collection(home) if settings.get("collection") != "off" else None,
                                            connect=False)   # connected once the portal is up
    accounts = None if (args.no_accounts or args.data_dir) else Accounts(home)   # people sign in (accounts.py)
    if accounts is not None:
        data_dir, problem = home / "lab", None   # until someone signs in
    else:   # a data folder chosen in Settings, else (or while it can't be written) the lab's own
        data_dir, problem = (Path(args.data_dir), None) if args.data_dir else data_folder_for(
            settings, home, lab_data_folder(home, lab, info, args.lab))
    runtime = LabRuntime(lab, WebApprover(timeout=args.approval_timeout), data_dir, envelope)
    runtime.data_problem = problem
    if accounts is None and not args.data_dir:
        runtime.lab_folder = lambda new, about: None if settings.get("data_dir") else lab_data_folder(home, new, about)
    if info:
        runtime.lab_info = info
    if accounts is not None:
        store = ChatStore(home / "lab" / "chats", create=False)
    else:
        store = ChatStore(Path(args.data_dir or home / "virtual-lab") / "chats")
    hosts = ("127.0.0.1:%d" % args.port, "localhost:%d" % args.port)
    app = create_app(runtime, settings, store, demo=args.demo, hosts=hosts, auto_setup=not (args.no_setup or args.lab),
                     accounts=accounts, sync_collection=True)
    url = "http://127.0.0.1:%d/#%s" % (args.port, settings.access_key)
    print("Kelvin portal: %s" % url, file=sys.stderr, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
