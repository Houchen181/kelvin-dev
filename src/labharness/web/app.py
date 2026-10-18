"""The Kelvin portal: a chat web app (like the Claude or ChatGPT website) wired to the lab.

    python -m labharness.web            # virtual lab, opens http://127.0.0.1:8770
    python -m labharness.web --demo     # adds a scripted model so it works without an API key
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
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
from ..clock import ScaledClock, SimClock
from ..virtual_lab import build_virtual_lab, demo_envelope
from .agent import Busy, ChatAgent
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


def create_app(runtime: LabRuntime, settings: Settings, store: ChatStore, *, demo: bool = False,
               hosts: Iterable[str] = ("127.0.0.1:8770", "localhost:8770"),
               providers: Optional[Dict[str, Provider]] = None, on_quit: Optional[Any] = None) -> Any:
    agent = ChatAgent(runtime, store, build_server(runtime), providers or make_providers(settings, demo), settings)
    tasks: List[asyncio.Task] = []

    @asynccontextmanager
    async def lifespan(app: Starlette):
        await agent.start()
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
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
                return JSONResponse(store.create(str(body.get("model") or ""), body.get("project") or None))
            except KeyError as e:
                return error(404, e.args[0])
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
            for m in data["messages"]:  # the browser never needs provider-internal state
                m.pop("raw", None)
            data["project"] = store.project_of(chat_id)
            return JSONResponse(data)
        except KeyError as e:
            return error(404, e.args[0])

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
            settings.update(await request.json())
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
        Route("/api/projects", projects, methods=["GET", "POST"]),
        Route("/api/projects/{project_id}", project, methods=["GET", "PATCH", "DELETE"]),
        Route("/api/projects/{project_id}/notes/{note_id}", note, methods=["DELETE"]),
        Route("/api/stop", stop, methods=["POST"]),
        Route("/api/approvals/{request_id}", approval, methods=["POST"]),
        Route("/api/state", state),
        Route("/api/mode", approval_mode, methods=["GET", "POST"]),
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

    lab = build_virtual_lab(clock=SimClock() if args.speed == 0 else ScaledClock(args.speed), seed=args.seed,
                            config=args.lab)
    envelope = None if args.lab else demo_envelope(lab)  # the demo envelope names the built-in lab's instruments
    runtime = LabRuntime(lab, WebApprover(timeout=args.approval_timeout), args.data_dir, envelope)
    settings = Settings(args.settings)
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
