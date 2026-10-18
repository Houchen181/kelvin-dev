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
    with urllib.request.urlopen(base + "/m", timeout=10) as r:   # the phone page and its script are packaged too
        page_ok = page_ok and b"Pair this phone" in r.read()
    with urllib.request.urlopen(base + "/static/mobile.js", timeout=10) as r:
        page_ok = page_ok and r.status == 200
    from . import autosetup, carddraft, drivers, profiles  # noqa: F401  (loaded on demand; make sure the build has them)
    page_ok = page_ok and "card: 1" in carddraft.example()
    try:   # the hardware extra: manuals (pypdf, cryptography), USB (Kelvin's own libusb), the network lookups
        from pypdf._crypt_providers import crypt_provider
        import psutil, zeroconf  # noqa: E401,F401
        from . import survey
        hardware = crypt_provider[0] == "cryptography" and survey.use_bundled_libusb()
    except ImportError:
        hardware = False
    if getattr(sys, "frozen", False):   # the app must bring them; a source checkout may be installed without them
        page_ok = page_ok and hardware
    from . import library
    bridge_ok = library.self_test()   # the helper for makers' libraries starts (Kelvin.exe --bridge) and answers
    page_ok = page_ok and bridge_ok
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
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True   # reports can be saved from the window too
    except (AttributeError, TypeError):
        pass
    webview.start(private_mode=False, storage_path=str(home / "webview"))


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="kelvin-app", description="Kelvin, the coolest cat in the lab")
    ap.add_argument("--home", default=str(HOME), help="where Kelvin keeps its data (default: ~/Kelvin)")
    ap.add_argument("--no-demo", action="store_true", help="hide the scripted demo model")
    ap.add_argument("--browser", action="store_true", help="open in the default browser instead of a window")
    ap.add_argument("--self-test", action="store_true", help="start, check the portal, exit")
    ap.add_argument("--no-setup", action="store_true", help="do not look for connected instruments on start")
    ap.add_argument("--port", type=int, default=0, help="portal port (default: any free port)")
    ap.add_argument("--remove-data", action="store_true",
                    help="delete Kelvin's own data (settings, keys, chats, memory, lab config, cards, instrument "
                         "folders) and keep every measurement (runs, the lab record, reports); the uninstaller runs it")
    args = ap.parse_args(argv)

    home = Path(args.home)
    if args.remove_data:
        if _running_copy(home / "app.json"):
            print("Kelvin is running; quit it first, then remove its data.", flush=True)
            return 1
        from .uninstall import remove_data
        report = remove_data(home)
        print(json.dumps(report, indent=1), flush=True)
        return 1 if report["failed"] else 0
    home.mkdir(parents=True, exist_ok=True)
    close_log = _log_to_file(home)
    try:
        return _run(args, home)
    finally:
        close_log()


APP_MUTEX = "Kelvin-8C0F6E5A"   # the installer and uninstaller wait while Kelvin runs (AppMutex in kelvin.iss)


def _hold_app_mutex() -> Any:
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        return ctypes.windll.kernel32.CreateMutexW(None, False, APP_MUTEX)
    except Exception:
        return None


def _run(args: argparse.Namespace, home: Path) -> int:
    lock = home / "app.json"
    mutex = _hold_app_mutex()  # noqa: F841  (held until Kelvin exits)

    from .agent.approval import WebApprover
    from .agent.runtime import LabRuntime
    from .virtual_lab import open_home_lab
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
    # the user's own instruments (lab.yaml) if they set them up, else the virtual lab at 10x real time
    lab, envelope, info = open_home_lab(home, settings.get("lab") or "auto", 10)
    from .web.app import data_folder_for
    data_dir, problem = data_folder_for(settings, home)   # the folder chosen in Settings, else ~/Kelvin/virtual-lab
    runtime = LabRuntime(lab, WebApprover(timeout=300), data_dir, envelope)
    runtime.data_problem = problem
    runtime.lab_info = info
    quit_event = threading.Event()
    app = create_app(runtime, settings, ChatStore(home / "virtual-lab" / "chats"), demo=not args.no_demo,
                     auto_setup=not (args.self_test or args.no_setup),   # set up connected instruments on start
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
