"""Kelvin as a desktop app: the portal in its own window, no terminal needed.

    kelvin-app                 # what the installer's shortcut runs
    kelvin-app --no-demo       # hide the scripted demo model
    kelvin-app --browser       # use the default browser instead of a window
    kelvin-app --self-test     # start, check that the portal answers, exit (used by the release builds)

Everything Kelvin keeps (settings, API keys, chats, event logs, runs) lives in ~/Kelvin.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Optional

HOME = Path.home() / "Kelvin"
log = logging.getLogger("kelvin")


def _log_to_file(home: Path) -> Any:
    """A windowed app has no console, so output goes to ~/Kelvin/kelvin.log. Returns a cleanup function."""
    handle = open(home / "kelvin.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = handle
    if sys.stderr is None:
        sys.stderr = handle
    handler = logging.StreamHandler(handle)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    def close() -> None:
        log.removeHandler(handler)
        if handle not in (sys.stdout, sys.stderr):
            handle.close()
    return close


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _running_copy(lock: Path) -> Optional[int]:
    """The port of a Kelvin that is already running, if any."""
    try:
        port = int(json.loads(lock.read_text(encoding="utf-8"))["port"])
        with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=1) as r:
            return port if b"<title>Kelvin</title>" in r.read() else None
    except (OSError, ValueError, KeyError):
        return None


def _self_test(port: int, key: str) -> int:
    base = "http://127.0.0.1:%d" % port
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        page_ok = b"<title>Kelvin</title>" in r.read()
    req = urllib.request.Request(base + "/api/bootstrap", headers={"X-Kelvin-Token": key})
    with urllib.request.urlopen(req, timeout=10) as r:
        boot = json.load(r)
    ok = page_ok and bool(boot.get("lab")) and any(g["models"] for g in boot["models"]["groups"])
    message = "Kelvin self-test: %s (lab %r)" % ("ok" if ok else "FAILED", boot.get("lab"))
    log.info(message)
    print(message, flush=True)
    return 0 if ok else 1


def _window(url: str, agent: Any, runtime: Any, home: Path) -> None:
    import webview  # imported here so headless self-tests never need a GUI toolkit

    window = webview.create_window("Kelvin", url, width=1280, height=860, min_size=(420, 560),
                                   background_color="#f6f8fb")

    def on_closing() -> bool:
        if agent.active:
            if not window.create_confirmation_dialog(
                    "Quit Kelvin?", "Kelvin is still working on a request. Quitting stops it where it is."):
                return False
            runtime.stop(by="human (closed the app)")
        return True

    window.events.closing += on_closing
    webview.start(private_mode=False, storage_path=str(home / "webview"))


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="kelvin-app", description="Kelvin, the coolest cat in the lab")
    ap.add_argument("--home", default=str(HOME), help="where Kelvin keeps its data (default: ~/Kelvin)")
    ap.add_argument("--no-demo", action="store_true", help="hide the scripted demo model")
    ap.add_argument("--browser", action="store_true", help="open in the default browser instead of a window")
    ap.add_argument("--self-test", action="store_true", help="start, check the portal, exit")
    ap.add_argument("--port", type=int, default=0, help="portal port (default: any free port)")
    args = ap.parse_args(argv)

    home = Path(args.home)
    home.mkdir(parents=True, exist_ok=True)
    close_log = _log_to_file(home)
    try:
        return _run(args, home)
    finally:
        close_log()


def _run(args: argparse.Namespace, home: Path) -> int:
    lock = home / "app.json"

    from .agent.approval import WebApprover
    from .agent.runtime import LabRuntime
    from .clock import ScaledClock
    from .virtual_lab import build_virtual_lab, demo_envelope
    from .web.app import create_app
    from .web.settings import Settings
    from .web.store import ChatStore

    settings = Settings(home / "settings.json")
    if not args.self_test:
        port = _running_copy(lock)
        if port:  # one Kelvin per computer: show the one that is running
            webbrowser.open("http://127.0.0.1:%d/#%s" % (port, settings.access_key))
            return 0

    import uvicorn

    port = args.port or _free_port()
    lab = build_virtual_lab(clock=ScaledClock(10))  # 10× real time: runs are quick but can be watched
    runtime = LabRuntime(lab, WebApprover(timeout=300), home / "virtual-lab", demo_envelope(lab))
    quit_event = threading.Event()
    app = create_app(runtime, settings, ChatStore(home / "virtual-lab" / "chats"), demo=not args.no_demo,
                     hosts=("127.0.0.1:%d" % port, "localhost:%d" % port),
                     on_quit=quit_event.set if args.browser else None)
    agent = app.app.state.agent
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, log_level="warning"))
    thread = threading.Thread(target=server.run, name="kelvin-server", daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started:
        if time.time() > deadline or not thread.is_alive():
            log.error("the portal did not start on port %d", port)
            return 1
        time.sleep(0.05)
    url = "http://127.0.0.1:%d/#%s" % (port, settings.access_key)
    log.info("Kelvin running on port %d (data in %s)", port, home)
    lock.write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")
    try:
        if args.self_test:
            return _self_test(port, settings.access_key)
        if args.browser:
            webbrowser.open(url)
            while not quit_event.wait(0.5):
                pass
        else:
            _window(url, agent, runtime, home)
        return 0
    finally:
        server.should_exit = True
        thread.join(10)
        try:
            if json.loads(lock.read_text(encoding="utf-8")).get("pid") == os.getpid():
                lock.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    sys.exit(main())
