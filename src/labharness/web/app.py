"""The Kelvin portal: a chat web app (like the Claude or ChatGPT website) wired to the lab.

    python -m labharness.web            # virtual lab, opens http://127.0.0.1:8770
    python -m labharness.web --demo     # adds a scripted model so it works without an API key
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import subprocess
import sys
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ..accounts import Accounts, cipher_for, has_legacy
from ..agent.approval import WebApprover
from ..agent.runtime import LabRuntime
from ..agent.server import build_server
from ..adapters import AdapterError
from ..autosetup import AutoSetup, log as setup_log, unlock as unlock_lab
from .. import autocard
from .. import profiles
from ..clock import ScaledClock, SimClock
from ..discovery import add_to_config
from ..labconfig import ConfigError, load_lab_config
from .. import carddraft
from ..virtual_lab import build_lab, builtin_ids, builtin_registry, is_simulated, lab_registry, open_home_lab
from .agent import Busy, ChatAgent, runs_in
from . import review, rules as rulebook
from .chatgpt import SignIn, SignInError
from .qr import QR
from .remote import ROLES, Remote, addresses, allowed, remote_host_ok
from .providers.base import Provider
from .settings import Settings
from .store import ChatStore

STATIC = Path(__file__).parent / "static"
TRACE_POINTS = 400
# what works while nobody is signed in: signing in, a new account, and stopping whatever runs
SIGNED_OUT = {"/api/session", "/api/accounts", "/api/stop", "/api/runs/stop"}


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
                 paired_with: Optional[Callable[[str], Optional[str]]] = None):
        self.app = app
        self.token = token
        self.hosts = set(hosts)
        self.remote = remote
        self.signed_in = signed_in       # with accounts: is someone signed in? (else the API waits for them)
        self.paired_with = paired_with   # with accounts: whose phone a token is, when it isn't the signed-in person's

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
                elif path == "/auth/callback":
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
                scope["kelvin"] = {"role": "owner", "device": None}
            if problem:
                body = dict(problem[2] if len(problem) > 2 else {}, error=problem[1])
                await JSONResponse(body, status_code=problem[0])(scope, receive, send)
                return
        await self.app(scope, receive, send)


def role_of(request: Request) -> str:
    return (request.scope.get("kelvin") or {}).get("role") or "owner"


SUMMARY_SYSTEM = ("You write the opening of a physics project's master report: two or three short paragraphs "
                  "saying what the project is about, what has been measured on which samples, what was found "
                  "(with the numbers given), and what comes next. Use only the facts given; never invent results "
                  "or references. Plain prose, no headings, no lists.")


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
               auto_setup: bool = False, accounts: Optional[Accounts] = None) -> Any:
    """accounts: people sign in, each to their own settings, keys, chats, data folder and record (accounts.py).
    Without it, one person uses Kelvin, as before accounts."""
    signin = SignIn(Path(settings.path).parent)   # Sign in with ChatGPT (chatgpt.json next to the settings)
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
    default_data = home / "virtual-lab"                   # runs, record and reports unless the user chose a folder
    loop_ref: Dict[str, Any] = {}
    session: Dict[str, Any] = {"user": None}              # who is signed in (with accounts); everyone is equal
    nobody = home / "lab"                                 # the record of what happens while nobody is signed in

    def user_default_data() -> Path:
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

    def write_card(found: Dict[str, Any]) -> Dict[str, Any]:
        r = autocard.make_card(found, runtime.cards_dir, ask_model, builtin_ids=builtin_ids(),
                               log=lambda m: setup_log.info("  %s", m))
        if r.get("path"):   # usable at once in Lab setup, like a card a person saves
            from ..cards import load_card
            runtime.lab.registry.put(load_card(r["path"]))
        return r

    setup = AutoSetup(runtime, settings, home, can_switch=lambda: busy_problem() is None, make_card=write_card)
    tasks: List[asyncio.Task] = []

    def configure_literature() -> None:
        runtime.literature.enabled = settings.get("literature") != "off"
        runtime.literature.openalex_key = settings.get("openalex_api_key") or ""
        runtime.scan_network = settings.get("scan_network") != "off"   # scans also look on the local network
        runtime.auto_report = settings.get("auto_report") or "both"     # a report after every run and queue
        runtime.report_kinds = set(settings.report_kinds)                # scan, device and project reports

    def configure_notifications() -> None:
        try:
            runtime.notifier.configure(settings.get("webhook"), settings.notify_kinds)
        except ValueError:
            runtime.notifier.configure(None, settings.notify_kinds)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        loop_ref["loop"] = asyncio.get_running_loop()
        await agent.start()
        configure_notifications()
        configure_literature()
        await start_phone()
        runtime.monitor.start()
        if auto_setup:   # find and set up connected instruments (or say why not: switched off, a lab chosen...)
            setup.start("start")
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            runtime.monitor.stop()
            await remote.stop()
            await agent.close()

    def error(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)

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
            "settings": settings.public(), "active": agent.active, "can_quit": on_quit is not None,
            "account": session_data(),
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
        return {"accounts_on": True, "accounts": listed,
                "user": {"name": user["name"], "password": bool(user.get("hash"))} if user else None,
                "suggest": "" if listed else suggested_name(),
                "busy": busy, "busy_user": runtime.user if busy else None,
                "legacy": not listed and has_legacy(home)}

    def sign_in(account: Dict[str, Any], password: str) -> None:
        """Serve this person: their settings and keys, chats, ChatGPT sign-in, data folder and record. The
        instruments, cards and the lab's instrument log stay as they are: they are everyone's."""
        same = runtime.user == account["name"]   # back to their own work, maybe still running: nothing to switch
        problem = None if same else running_for()
        if problem:
            raise RuntimeError(("Kelvin is still doing %s's work. " % runtime.user if runtime.user else "") + problem)
        cipher = cipher_for(password, account)
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

    def sign_out() -> None:
        """Nobody is signed in: the portal waits for the next person. A plan or queue still running goes on and is
        recorded as its person's; Kelvin turns to nobody's record when it ends (or at the next sign-in)."""
        agent.stop()   # a chat still answering ends with its person
        session["user"] = None
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

    async def account_view(request: Request) -> Response:
        """POST {old, new}: change (or set, or remove) the signed-in person's password; their keys are encrypted
        again with it. DELETE {password}: forget the account (its folder stays on disk) and sign out."""
        user = session["user"]
        if accounts is None or user is None:
            return error(404, "Nobody is signed in.")
        body = await request.json()
        if request.method == "DELETE":
            try:
                await asyncio.to_thread(accounts.remove, user["name"], str(body.get("password") or ""))
            except ValueError as e:
                await asyncio.sleep(1)
                return error(401, str(e))
            await asyncio.to_thread(sign_out)
            return JSONResponse(session_data())
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

    async def chats(request: Request) -> Response:
        if request.method == "POST":
            body = await request.json()
            try:
                return JSONResponse(store.create(str(body.get("model") or ""), body.get("project") or None,
                                                 incognito=bool(body.get("incognito"))))
            except KeyError as e:
                return error(404, e.args[0])
            except ValueError as e:
                return error(400, str(e))
        return JSONResponse(store.list())

    async def chat(request: Request) -> Response:
        chat_id = request.path_params["chat_id"]
        try:
            if request.method == "DELETE":
                if agent.active and agent.active["chat_id"] == chat_id:
                    return error(409, "This conversation is still running.")
                store.delete(chat_id)
                return JSONResponse({"ok": True})
            data = store.load(chat_id)
            if request.method == "PATCH":
                body = await request.json()
                title = str(body.get("title") or "").strip()[:80]
                if title:
                    if agent.active and agent.active["chat_id"] == chat_id:
                        return error(409, "Rename it when the current answer is done.")
                    data["title"] = title
                    store.save(data)
                if "project" in body:  # file it under a project, or null to take it out
                    store.assign(chat_id, body["project"] or None)
            out = dict(data, messages=[{k: v for k, v in m.items() if k != "raw"} for m in data["messages"]])
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
        """One project with its memory (instructions and notes). PATCH takes a new name, new instructions, or both."""
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
        """PATCH {name, material, description, retired}."""
        try:
            body = await request.json()
            d = store.update_device(project_key(request), request.path_params["device_id"],
                                    **{k: body.get(k) for k in ("name", "material", "description", "retired")})
            return JSONResponse(d)
        except KeyError as e:
            return error(404, e.args[0])
        except ValueError as e:
            return error(400, str(e))

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
        if request.method == "POST":
            body = await request.json()
            pid = body.get("project") or None
            try:
                if body.get("device"):
                    runtime.mount(pid, store.find_device(pid, body["device"]), by="human")
                else:
                    runtime.unmount(by="human")
            except KeyError as e:
                return error(404, e.args[0])
        choices = [{"project": None, "name": "General", "devices": store.devices(None)}] + \
            [{"project": p["id"], "name": p["name"], "devices": p.get("devices") or []} for p in store.projects()]
        return JSONResponse({"mounted": runtime.mounted or None, "choices": choices})

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
            return JSONResponse({"cards": carddraft.user_cards(runtime.cards_dir), "folder": str(runtime.cards_dir)})
        body = await request.json()
        try:
            card, path, warnings = carddraft.save(runtime.cards_dir, str(body.get("yaml") or ""), builtin_ids(), "human",
                                                  str(body.get("replace") or "") or None)
        except ValueError as e:
            return error(400, str(e))
        if body.get("replace") and body["replace"] != card.id and body["replace"] not in builtin_ids():
            runtime.lab.registry.remove(body["replace"])
        runtime.lab.registry.put(card)
        runtime.gate.log.write("card", by="human", card=card.id, status=card.status, file=path.name)
        return JSONResponse({"id": card.id, "file": path.name, "status": card.status, "warnings": warnings})

    async def card_check(request: Request) -> Response:
        card, problems, warnings = carddraft.check(str((await request.json()).get("yaml") or ""))
        return JSONResponse({"id": card.id if card else None, "problems": problems, "warnings": warnings})

    async def card_status(request: Request) -> Response:
        body = await request.json()
        try:
            card = carddraft.set_status(runtime.cards_dir, str(body.get("id") or ""), str(body.get("status") or ""))
        except KeyError as e:
            return error(404, e.args[0])
        except ValueError as e:
            return error(400, str(e))
        runtime.lab.registry.put(card)
        runtime.gate.log.write("card", by="human", card=card.id, status=card.status)
        return JSONResponse({"id": card.id, "status": card.status})

    async def card_delete(request: Request) -> Response:
        card_id = str((await request.json()).get("id") or "")
        try:
            carddraft.delete(runtime.cards_dir, card_id)
        except KeyError as e:
            return error(404, e.args[0])
        if card_id not in builtin_ids():
            runtime.lab.registry.remove(card_id)
        return JSONResponse({"ok": True})

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
                "addresses": addresses(live or port), "devices": settings.device_views(), "roles": list(ROLES)}

    async def start_phone() -> None:
        on, port = settings.phone
        if not on:
            await remote.stop()
            return
        try:
            await remote.start(guard, port)
        except OSError as e:
            remote.error = "Could not listen on port %d: %s" % (port, e.strerror or e)

    async def phone(request: Request) -> Response:
        """GET: phone access and paired devices. POST {on, port}: switch phone access (this computer only)."""
        if request.method == "POST":
            body = await request.json()
            try:
                settings.set_phone(bool(body.get("on")), int(body.get("port") or settings.phone[1]))
            except (TypeError, ValueError) as e:
                return error(400, str(e))
            await start_phone()
        return JSONResponse(phone_view())

    async def devices(request: Request) -> Response:
        """POST {name, role}: a one-time pairing code (and QR code) for a new phone."""
        body = await request.json()
        if remote.port is None:
            return error(400, "Switch on phone access first.")
        try:
            p = remote.start_pairing(str(body.get("name") or ""), str(body.get("role") or "watch"))
        except ValueError as e:
            return error(400, str(e))
        urls = [a["url"] + "/pair#" + p.code for a in addresses(remote.port)]
        return JSONResponse({"code": p.code, "name": p.name, "role": p.role, "expires_in": int(p.expires - time.time()),
                             "urls": urls, "qr": QR(urls[0]).svg(5) if urls else None})

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
        return JSONResponse({"role": k.get("role") or "owner", "name": d.get("name"), "lab": runtime.lab.config.name})

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
                runs = [r for r in runs_in(chats) if (runtime.data_dir / "runs" / r).is_dir()]
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
        """A run's report or CSV data, to download."""
        try:
            path = runtime.run_file(request.path_params["run_id"], request.path_params["name"])
        except KeyError as e:
            return error(404, e.args[0])
        media = {".pdf": "application/pdf", ".csv": "text/csv",
                 ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}[path.suffix]
        return FileResponse(path, media_type=media, filename="%s-%s" % (request.path_params["run_id"], path.name))

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
        if agent.active:
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
                                 "observe_only": runtime.lab.config.observe_only, "setup": setup.status,
                                 "folders": instrument_folders()})
        body = await request.json()
        problem = busy_problem()
        if problem:
            return error(409, problem)
        use = body.get("use")
        by = "human (portal)"
        if body.get("unlock"):   # a person reviewed the limits Kelvin wrote and lifts the observe-only lock
            if not lab_file.exists():
                return error(400, "There is no lab config to unlock.")
            body, use, by = {"yaml": unlock_lab(lab_file.read_text(encoding="utf-8"))}, None, "human (portal, unlocked)"
        try:
            if use == "virtual":
                lab, envelope, info = await asyncio.to_thread(open_home_lab, lab_file.parent, "virtual")
            else:
                text = str(body.get("yaml") or "") if use != "mine" else (
                    lab_file.read_text(encoding="utf-8") if lab_file.exists() else "")
                if not text.strip():
                    return error(400, "There is no lab config yet; scan for instruments or write one first.")
                registry = lab_registry(lab_file.parent)   # Kelvin's cards and the user's own
                load_lab_config(text, registry)   # check it before touching any instrument
                lab = await asyncio.to_thread(build_lab, text, None, 0, registry)
                envelope, info = None, {"kind": "file", "path": str(lab_file), "error": None, "has_file": True}
                if use != "mine":
                    tmp = lab_file.with_suffix(".tmp")
                    tmp.write_text(text, encoding="utf-8")
                    tmp.replace(lab_file)
        except ConfigError as e:
            return error(400, "The lab config has problems: " + "; ".join(e.errors[:6]))
        except (AdapterError, OSError, ValueError) as e:
            return error(400, str(e).splitlines()[0] if str(e) else type(e).__name__)
        try:
            health = await asyncio.to_thread(runtime.switch_lab, lab, envelope, info, by)
        except RuntimeError as e:
            lab.close()
            return error(409, str(e))
        settings.update({"lab": "virtual" if use == "virtual" else "mine"})
        if use != "virtual" and auto_setup:   # give new instruments their folders, settings and manuals
            setup.start("lab changed")
        return JSONResponse({"ok": True, "name": lab.config.name, "info": runtime.lab_info, "health": health,
                             "observe_only": lab.config.observe_only})

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
                runtime.lab.registry.get(card)
            except KeyError:
                return error(400, "%s: choose a card" % name)
            items.append({"name": name, "card": card, "address": address, "note": str(it.get("note") or "")[:60]})
        return JSONResponse({"yaml": add_to_config(str(body.get("base") or ""), items, runtime.lab.registry)})

    async def turn(request: Request) -> Response:
        chat_id = request.path_params["chat_id"]
        body = await request.json()
        text = str(body.get("text") or "").strip()
        if not text:
            return error(400, "Say something first.")
        model = str(body.get("model") or "")
        try:
            provider = agent.claim(chat_id, model)
        except Busy as e:
            return error(409, str(e))
        except (KeyError, ValueError) as e:
            return error(400, e.args[0])
        queue: asyncio.Queue = asyncio.Queue()

        async def run() -> None:
            try:
                await agent.run_turn(chat_id, text, model, provider, queue.put)
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
            lanes = agent.claim_compare(chat_id, models)
        except Busy as e:
            return error(409, str(e))
        except (KeyError, ValueError) as e:
            return error(400, e.args[0])
        return stream_events(lambda emit: agent.run_compare(chat_id, text, lanes, emit))

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
        """Another model reviews the chat's latest answer."""
        body = await request.json()
        try:
            opinion = await agent.review_answer(request.path_params["chat_id"], str(body.get("model") or ""))
        except Busy as e:
            return error(409, str(e))
        except KeyError as e:
            return error(404, e.args[0] if e.args else "no such chat")
        except ValueError as e:
            return error(400, str(e))
        except asyncio.TimeoutError:
            return error(504, "The reviewer did not answer within 3 minutes.")
        except Exception as e:
            return error(502, "The reviewer could not answer: %s" % str(e).splitlines()[0][:200] if str(e) else type(e).__name__)
        return JSONResponse(opinion)

    async def stop(request: Request) -> Response:
        return JSONResponse({"ok": agent.stop()})

    async def approval(request: Request) -> Response:
        body = await request.json()
        if body.get("approve") and role_of(request) == "watch":
            return error(403, "This phone can watch and decline, not approve. Approve on the Kelvin computer.")
        ok = runtime.approver.answer(request.path_params["request_id"], bool(body.get("approve")))
        return JSONResponse({"ok": ok}, status_code=200 if ok else 409)

    async def state(request: Request) -> Response:
        data = await asyncio.to_thread(runtime.ui_state)  # may read instruments; keep the server responsive
        data["active"] = agent.active
        return JSONResponse(data)

    async def approval_mode(request: Request) -> Response:
        """Ask before every drive increase, or auto-approve low-risk ones. People only: no agent tool reaches this."""
        if request.method == "POST":
            try:
                return JSONResponse(runtime.set_approval_mode(str((await request.json()).get("mode")), "human (portal)"))
            except ValueError as e:
                return error(400, str(e))
        return JSONResponse(runtime.approval_mode())

    async def settings_view(request: Request) -> Response:
        if request.method == "POST":
            try:
                settings.update(await request.json())
            except ValueError as e:
                return error(400, str(e))
            configure_notifications()
            configure_literature()
            agent.providers = make_providers(settings, demo, signin) if providers is None else providers
        return JSONResponse({"settings": settings.public(), "models": await agent.models()})

    async def run_data(request: Request) -> Response:
        """What the browser needs to plot a run: axes (outer first), scalar columns, the last trace per
        trace column, and the automatic analysis."""
        run_id = request.path_params["run_id"]
        try:
            rows = runtime._rows(run_id)
            run_dir = runtime._run_dir(run_id)
            cols = runtime.engine.columns(runtime._plan((run_dir / "plan.yaml").read_text(encoding="utf-8")))
        except (KeyError, OSError, ValueError) as e:
            return error(404, str(e))
        axes = [{"key": a["key"], "unit": a["unit"]} for a in cols["axes"]]
        scalars = [{"key": m["key"], "unit": m["unit"]} for m in cols["measures"] if m["kind"] == "scalar"]
        traces = []
        for m in cols["measures"]:
            if m["kind"] != "trace" or not rows or not isinstance(rows[-1].get(m["key"]), list):
                continue
            ys = rows[-1][m["key"]]
            step = max(1, -(-len(ys) // TRACE_POINTS))
            x0, x1 = rows[-1].get(m["key"] + ".axis") or (0, len(ys) - 1)
            n = len(ys)
            xs = [x0 + (x1 - x0) * i / (n - 1 if n > 1 else 1) for i in range(0, n, step)]
            traces.append({"key": m["key"], "unit": m["unit"], "x": xs, "y": ys[::step], "x_unit": m.get("x_unit", ""),
                           "row": len(rows) - 1})
        def read_json(name: str) -> Any:
            try:
                return json.loads((run_dir / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None

        meta = read_json("meta.json") or {}
        keys = [a["key"] for a in axes] + [m["key"] for m in scalars]
        return JSONResponse({"run_id": run_id, "plan": run_id.split("-", 2)[-1], "axes": axes, "scalars": scalars,
                             "rows": [{k: r.get(k) for k in keys} for r in rows], "traces": traces,
                             "analysis": read_json("analysis.json"),
                             "plan_text": (run_dir / "plan.yaml").read_text(encoding="utf-8"),
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
            runtime.stop("human (portal)")
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": runtime.stop_run()})

    async def queue(request: Request) -> Response:
        """GET: the scan queue. POST {op, ...}: a person's change to it. start asks for the queue's approval as usual
        and returns once it is answered."""
        if request.method == "GET":
            return JSONResponse(runtime.queue_view())
        body = await request.json()
        op, by = str(body.get("op") or ""), "human"
        item = body.get("item")
        if op == "add":
            pos = body.get("position")
            msg = runtime.queue_add(str(body.get("text") or ""), int(pos) if pos else None, str(body.get("note") or ""), by)
            ok = not msg.startswith("not added")
        elif op == "edit":
            msg = runtime.queue_edit(item, str(body.get("text") or ""), by)
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
            msg = runtime.queue_load(str(body.get("name") or ""), by)
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
        Route("/auth/callback", auth_callback),
        Route("/api/chatgpt", chatgpt_view),
        Route("/api/chatgpt/signin", chatgpt_signin, methods=["POST"]),
        Route("/api/chatgpt/signout", chatgpt_signout, methods=["POST"]),
        Route("/api/chatgpt/welcomed", chatgpt_welcomed, methods=["POST"]),
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
        Route("/api/account", account_view, methods=["POST", "DELETE"]),
        Route("/api/lab-log", lab_log),
        Route("/api/chats", chats, methods=["GET", "POST"]),
        Route("/api/chats/{chat_id}", chat, methods=["GET", "PATCH", "DELETE"]),
        Route("/api/chats/{chat_id}/turn", turn, methods=["POST"]),
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
        Route("/api/memory", memory_view),
        Route("/api/memory/notes/{note_id}", memory_note, methods=["DELETE"]),
        Route("/api/stop", stop, methods=["POST"]),
        Route("/api/approvals/{request_id}", approval, methods=["POST"]),
        Route("/api/state", state),
        Route("/api/mode", approval_mode, methods=["GET", "POST"]),
        Route("/api/lab", lab_view, methods=["GET", "POST"]),
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
        Route("/api/instruments/setup", setup_view, methods=["GET", "POST"]),
        Route("/api/terminal", terminal, methods=["POST"]),
        Route("/api/data-folder", data_folder, methods=["GET", "POST"]),
        Route("/api/instruments/{name}/manual", instrument_manual, methods=["POST", "PUT"]),
        Route("/api/instruments/{name}/open", instrument_open, methods=["POST"]),
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
        Route("/static/{name}", static),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.agent = agent
    app.state.remote = remote
    app.state.signin = signin
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

    guard = Guard(app, settings.access_key, hosts, remote,
                  signed_in=None if accounts is None else lambda: session["user"] is not None,
                  paired_with=None if accounts is None else paired_with)
    return guard


def suggested_name() -> str:
    """The computer's name for this person, offered when the first account is made."""
    import getpass
    try:
        return getpass.getuser()[:40]
    except Exception:
        return ""


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
    info = None
    if args.lab:  # an explicit lab config: simulated ones run at --speed, real ones on real time
        simulated = is_simulated(load_lab_config(args.lab, builtin_registry()))
        lab = build_lab(args.lab, clock=(SimClock() if args.speed == 0 else ScaledClock(args.speed)) if simulated else None,
                        seed=args.seed)
        envelope = None
    else:   # the user's lab.yaml next to the settings, if they have one; otherwise the virtual lab
        lab, envelope, info = open_home_lab(Path(args.settings).parent, settings.get("lab") or "auto", args.speed)
    home = Path(args.settings).parent
    accounts = None if (args.no_accounts or args.data_dir) else Accounts(home)   # people sign in (accounts.py)
    if accounts is not None:
        data_dir, problem = home / "lab", None   # until someone signs in
    else:
        data_dir, problem = (Path(args.data_dir), None) if args.data_dir else data_folder_for(settings, home)
    runtime = LabRuntime(lab, WebApprover(timeout=args.approval_timeout), data_dir, envelope)
    runtime.data_problem = problem
    if info:
        runtime.lab_info = info
    if accounts is not None:
        store = ChatStore(home / "lab" / "chats", create=False)
    else:
        store = ChatStore(Path(args.data_dir or home / "virtual-lab") / "chats")
    hosts = ("127.0.0.1:%d" % args.port, "localhost:%d" % args.port)
    app = create_app(runtime, settings, store, demo=args.demo, hosts=hosts, auto_setup=not (args.no_setup or args.lab),
                     accounts=accounts)
    url = "http://127.0.0.1:%d/#%s" % (args.port, settings.access_key)
    print("Kelvin portal: %s" % url, file=sys.stderr, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
