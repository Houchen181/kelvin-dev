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
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ..agent.approval import WebApprover
from ..agent.runtime import LabRuntime
from ..agent.server import build_server
from ..adapters import AdapterError
from ..clock import ScaledClock, SimClock
from ..discovery import add_to_config
from ..labconfig import ConfigError, load_lab_config
from ..virtual_lab import build_lab, builtin_registry, is_simulated, open_home_lab
from .agent import Busy, ChatAgent, runs_in
from .providers.base import Provider
from .settings import Settings
from .store import ChatStore

STATIC = Path(__file__).parent / "static"
TRACE_POINTS = 400


def make_providers(settings: Settings, demo: bool = False) -> Dict[str, Provider]:
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
    if demo:
        from .providers.demo import DemoProvider
        providers["demo"] = DemoProvider()
    return providers


class Guard:
    """Only this machine's browser, holding the access key, may use the API."""

    def __init__(self, app: Any, token: str, hosts: Iterable[str]):
        self.app = app
        self.token = token
        self.hosts = set(hosts)

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            origin = headers.get("origin")
            problem = None
            if headers.get("host") not in self.hosts:
                problem = (403, "bad host")
            elif origin and origin.split("://", 1)[-1] not in self.hosts:
                problem = (403, "bad origin")
            elif scope["path"].startswith("/api/") and not hmac.compare_digest(
                    headers.get("x-kelvin-token", ""), self.token):
                problem = (401, "missing or wrong access key")
            if problem:
                await JSONResponse({"error": problem[1]}, status_code=problem[0])(scope, receive, send)
                return
        await self.app(scope, receive, send)


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
               providers: Optional[Dict[str, Provider]] = None, on_quit: Optional[Any] = None) -> Any:
    agent = ChatAgent(runtime, store, build_server(runtime), providers or make_providers(settings, demo), settings)
    lab_file = Path(settings.path).parent / "lab.yaml"   # the user's own lab, next to their settings
    tasks: List[asyncio.Task] = []

    def configure_notifications() -> None:
        try:
            runtime.notifier.configure(settings.get("webhook"), settings.notify_kinds)
        except ValueError:
            runtime.notifier.configure(None, settings.notify_kinds)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        await agent.start()
        configure_notifications()
        runtime.monitor.start()
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            runtime.monitor.stop()
            await agent.close()

    def error(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)

    async def index(request: Request) -> Response:
        # version-stamp the assets so a browser never runs a stale copy after an update
        version = str(int(max((STATIC / n).stat().st_mtime for n in ("app.js", "app.css"))))
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        page = page.replace("/static/app.js", "/static/app.js?v=" + version).replace("/static/app.css", "/static/app.css?v=" + version)
        return Response(page, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})

    async def static(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in ("app.js", "app.css", "kelvin.svg"):
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
        })

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
            return JSONResponse(store.project(project_id))
        except KeyError as e:
            return error(404, e.args[0])
        except ValueError as e:
            return error(400, str(e))

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

    # ------------------------------------------------------------------ reports

    async def reports_view(request: Request) -> Response:
        """GET: recent reports. POST {scope: chat|project|today|last|run, chat_id, project_id, run, last, title,
        summary, format}: write one."""
        if request.method == "GET":
            return JSONResponse({"reports": runtime.reports_index(), "folder": str(runtime.reports_dir)})
        body = await request.json()
        scope = str(body.get("scope") or "")
        notes: List[Any] = []
        title = str(body.get("title") or "")
        try:
            if scope in ("chat", "project"):
                chat = store.load(str(body.get("chat_id") or ""))
                chats, project = agent.chats_for_report(chat) if scope == "project" else ([chat], None)
                runs = [r for r in runs_in(chats) if (runtime.data_dir / "runs" / r).is_dir()]
                notes = agent.report_notes(project)
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
                                            str(body.get("format") or "pdf"), notes, "human")
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
                                 "yaml": lab_file.read_text(encoding="utf-8") if lab_file.exists() else ""})
        body = await request.json()
        problem = busy_problem()
        if problem:
            return error(409, problem)
        use = body.get("use")
        try:
            if use == "virtual":
                lab, envelope, info = await asyncio.to_thread(open_home_lab, lab_file.parent, "virtual")
            else:
                text = str(body.get("yaml") or "") if use != "mine" else (
                    lab_file.read_text(encoding="utf-8") if lab_file.exists() else "")
                if not text.strip():
                    return error(400, "There is no lab config yet; scan for instruments or write one first.")
                load_lab_config(text, runtime.lab.registry)   # check it before touching any instrument
                lab = await asyncio.to_thread(build_lab, text)
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
            health = await asyncio.to_thread(runtime.switch_lab, lab, envelope, info, "human (portal)")
        except RuntimeError as e:
            lab.close()
            return error(409, str(e))
        settings.update({"lab": "virtual" if use == "virtual" else "mine"})
        return JSONResponse({"ok": True, "name": lab.config.name, "info": runtime.lab_info, "health": health})

    async def scan_view(request: Request) -> Response:
        """Find connected instruments. Only asks each instrument who it is."""
        problem = busy_problem()
        if problem:
            return error(409, problem)
        try:
            return JSONResponse({"found": await asyncio.to_thread(runtime.scan_found)})
        except AdapterError as e:
            return error(503, str(e))

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
            agent.providers = make_providers(settings, demo) if providers is None else providers
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
                                                                "duration_s", "warnings")}})

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
        return JSONResponse({"ok": runtime.stop_run()})

    routes = [
        Route("/", index),
        Route("/api/bootstrap", bootstrap),
        Route("/api/chats", chats, methods=["GET", "POST"]),
        Route("/api/chats/{chat_id}", chat, methods=["GET", "PATCH", "DELETE"]),
        Route("/api/chats/{chat_id}/turn", turn, methods=["POST"]),
        Route("/api/chats/{chat_id}/compare", compare, methods=["POST"]),
        Route("/api/chats/{chat_id}/choose", choose, methods=["POST"]),
        Route("/api/chats/{chat_id}/review", second_opinion, methods=["POST"]),
        Route("/api/projects", projects, methods=["GET", "POST"]),
        Route("/api/projects/{project_id}", project, methods=["GET", "PATCH", "DELETE"]),
        Route("/api/projects/{project_id}/notes/{note_id}", note, methods=["DELETE"]),
        Route("/api/memory", memory_view),
        Route("/api/memory/notes/{note_id}", memory_note, methods=["DELETE"]),
        Route("/api/stop", stop, methods=["POST"]),
        Route("/api/approvals/{request_id}", approval, methods=["POST"]),
        Route("/api/state", state),
        Route("/api/mode", approval_mode, methods=["GET", "POST"]),
        Route("/api/lab", lab_view, methods=["GET", "POST"]),
        Route("/api/reports", reports_view, methods=["GET", "POST"]),
        Route("/api/reports/{name}", report_file),
        Route("/api/reports/{name}/open", report_open, methods=["POST"]),
        Route("/api/watches", watches_view, methods=["GET", "POST"]),
        Route("/api/watches/{watch_id}", watch_view, methods=["DELETE"]),
        Route("/api/alarms/{alarm_id}/ack", alarm_ack, methods=["POST"]),
        Route("/api/notify/test", notify_test, methods=["POST"]),
        Route("/api/instruments/scan", scan_view, methods=["POST"]),
        Route("/api/instruments/draft", draft_view, methods=["POST"]),
        Route("/api/settings", settings_view, methods=["GET", "POST"]),
        Route("/api/quit", quit_app, methods=["POST"]),
        Route("/api/live", live),
        Route("/api/runs", runs),
        Route("/api/runs/stop", stop_run, methods=["POST"]),
        Route("/api/runs/{run_id}", run_data),
        Route("/static/{name}", static),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.agent = agent
    return Guard(app, settings.access_key, hosts)


def main(argv: Optional[List[str]] = None) -> None:
    import uvicorn

    ap = argparse.ArgumentParser(prog="python -m labharness.web", description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--data-dir", default=str(Path.home() / "Kelvin" / "virtual-lab"))
    ap.add_argument("--settings", default=str(Path.home() / "Kelvin" / "settings.json"))
    ap.add_argument("--speed", type=float, default=10,
                    help="virtual lab time: N× real time (default 10, so runs can be watched); 0 = instant")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--approval-timeout", type=float, default=300)
    ap.add_argument("--demo", action="store_true", help="add a scripted model so the portal works without an API key")
    ap.add_argument("--lab", help="lab config YAML using sim:// instruments (default: the built-in virtual lab)")
    ap.add_argument("--no-browser", action="store_true")
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
    runtime = LabRuntime(lab, WebApprover(timeout=args.approval_timeout), args.data_dir, envelope)
    if info:
        runtime.lab_info = info
    store = ChatStore(Path(args.data_dir) / "chats")
    hosts = ("127.0.0.1:%d" % args.port, "localhost:%d" % args.port)
    app = create_app(runtime, settings, store, demo=args.demo, hosts=hosts)
    url = "http://127.0.0.1:%d/#%s" % (args.port, settings.access_key)
    print("Kelvin portal: %s" % url, file=sys.stderr, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
