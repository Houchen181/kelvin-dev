"""Kelvin as a desktop app: the portal in its own window, no terminal needed.

    kelvin-app                 # what the installer's shortcut runs
    kelvin-app --browser       # use the default browser instead of a window
    kelvin-app --self-test     # start, check that the portal answers, exit (used by the release builds)

Everything Kelvin keeps lives in ~/Kelvin: the computer's settings and instruments, and each person's own folder
(settings, API keys, chats, runs, record) in ~/Kelvin/users after they sign in (accounts.py).
"""
from __future__ import annotations

import argparse
import faulthandler
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
from typing import Any, List, Optional

HOME = Path.home() / "Kelvin"
log = logging.getLogger("kelvin")
START_WAIT_S = 30.0   # how long a second Kelvin waits for the first to show its portal, or to finish closing
CLOSE_WAIT_S = 20.0   # how long Kelvin waits for its portal to stop (and its instruments to close) as it quits
QUIT_DEADLINE_S = 40.0   # and then for everything: past it, where it is stuck goes to faults.log and it quits anyway
_faults: Any = None   # faults.log, kept open: Python writes where every thread was if Kelvin crashes (faulthandler)


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
    log.propagate = False   # once: never again through the root logger, where a library's own handler wrote each bare
    # other libraries' warnings and errors, once and with the time: a handler of Python's root logger, set before any
    # library sets one of its own (the MCP library's logging.basicConfig then leaves it as it is)
    others = logging.StreamHandler(handle)
    others.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    others.setLevel(logging.WARNING)
    root = logging.getLogger()
    root.addHandler(others)

    def close() -> None:
        log.removeHandler(handler)
        root.removeHandler(others)
        if handle not in (sys.stdout, sys.stderr):
            handle.close()
    return close


def _watch_for_crashes(home: Path) -> None:
    """faults.log: where every thread of Kelvin was when it crashed (a stack overflow, an access violation in an
    instrument's library), which Windows' own crash report doesn't say. A field test's Kelvin crashed with a stack
    overflow as it quit, and nothing said where."""
    global _faults
    try:
        _faults = open(home / "faults.log", "a", encoding="utf-8", buffering=1)
        from ._version import __version__
        _faults.write("%s Kelvin %s started (process %d)\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), __version__,
                                                                os.getpid()))
        faulthandler.enable(file=_faults, all_threads=True)
    except (OSError, RuntimeError, ValueError):
        _faults = None


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


def _run_file_ok() -> bool:
    """A run recorded, kept as run.h5 and read back: h5py and its HDF5 library are in the app."""
    import tempfile
    from . import rundata
    try:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            r = rundata.Recorder(folder)
            r.write("start", {"plan": "plan: self_test\n", "meta": {"run_id": "self-test"}})
            r.write("row", values={"i": 0, "t": 0.0, "x": [1.0, 2.0]})
            r.write("finish", {"meta": {"run_id": "self-test", "status": "done", "points": 1}})
            r.close()
            try:
                kept = rundata.finish(folder)
            finally:
                r.release()
            return kept is not None and rundata.load(folder).rows[0]["x"] == [1.0, 2.0]
    except Exception as e:
        log.info("run.h5 self-test failed: %s", e)
        return False


def _self_test(port: int, key: str) -> int:
    base = "http://127.0.0.1:%d" % port
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        page_ok = b"<title>Kelvin</title>" in r.read()
    req = urllib.request.Request(base + "/api/bootstrap", headers={"X-Kelvin-Token": key})
    with urllib.request.urlopen(req, timeout=10) as r:
        boot = json.load(r)
    with urllib.request.urlopen(base + "/static/app.js", timeout=10) as r:   # the portal's script is packaged too
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
        page_ok = page_ok and hardware and _run_file_ok()
    from . import library, viworker
    bridge_ok = library.self_test()   # the helper for makers' libraries starts (Kelvin.exe --bridge) and answers
    reader_ok = viworker.self_test()   # so does the reader of a lab's LabVIEW files, and it reads
    page_ok = page_ok and bridge_ok and reader_ok
    # the models' list loads (the services are packaged); a fresh install has no key yet, so none need be offered
    ok = page_ok and bool(boot.get("lab")) and isinstance((boot.get("models") or {}).get("groups"), list)
    extra = "" if reader_ok else " (the LabVIEW reader FAILED)"
    if os.environ.get("KELVIN_SELFTEST_ENVS") == "1":   # the release builds, which have the network (envs.py)
        from . import envs
        try:
            extra = " (instrument environments: ok, %s)" % envs.self_test()
        except Exception as e:
            ok, extra = False, " (instrument environments FAILED: %s)" % (str(e).splitlines()[0][:300] if str(e)
                                                                           else type(e).__name__)
    message = "Kelvin self-test: %s (lab %r)%s" % ("ok" if ok else "FAILED", boot.get("lab"), extra)
    if boot.get("claude_code"):   # Claude Code bridge: only in a private build, never in a published one
        message += " (Claude Code bridge on)"
    log.info(message)
    print(message, flush=True)
    return 0 if ok else 1


def _zoom(window: Any, factor: float) -> bool:
    """Scale the window's page like a browser's zoom, on the window's own thread: the web view's zoom factor on
    Windows (WebView2), its page zoom on a Mac (WKWebView, macOS 11 and later). False where it has neither."""
    if sys.platform == "win32":
        form = getattr(window, "native", None)   # pywebview's form, which holds the WebView2 control
        view = getattr(getattr(form, "browser", None), "webview", None)
        if view is None:
            return False
        import clr  # noqa: F401  (pythonnet, which pywebview runs on here: loaded before System)
        from System import Func, Type

        def apply() -> None:
            view.ZoomFactor = factor
        form.Invoke(Func[Type](apply))
        return True
    if sys.platform == "darwin":
        from PyObjCTools import AppHelper
        from webview.platforms.cocoa import BrowserView
        view = getattr(BrowserView.instances.get(window.uid), "webview", None)
        if view is None or not hasattr(view, "setPageZoom_"):
            return False
        AppHelper.callAfter(view.setPageZoom_, factor)
        return True
    return False


def _measuring(runtime: Any) -> bool:
    """A run or the queue is going."""
    try:
        live = runtime.live or {}
        return live.get("status") == "running" or runtime.queue.state in ("running", "pausing")
    except Exception:
        return False


def _window(url: str, agent: Any, runtime: Any, home: Path, closer: Optional[dict] = None,
            shower: Optional[dict] = None) -> None:
    import webview  # imported here so headless self-tests never need a GUI toolkit

    window = webview.create_window("Kelvin", url, width=1280, height=860, min_size=(420, 560),
                                   background_color="#f6f8fb",
                                   text_select=True)   # pywebview blocks selecting text unless asked: chats are to copy
    if closer is not None:   # Restart to update closes the window like the person would
        closer["fn"] = window.destroy

    def on_closing() -> bool:
        busy = ["working on a request"] if agent.answering() else []
        if _measuring(runtime):
            busy.append("measuring (a run or the queue)")
        if busy:
            if not window.create_confirmation_dialog(
                    "Quit Kelvin?", "Kelvin is still %s. Quitting stops it where it is; outputs stay as they are."
                                    % " and ".join(busy)):
                return False
            runtime.stop(by="human (closed the app)")
        return True

    window.events.closing += on_closing

    def bring_forward() -> None:
        """Another start of Kelvin asked for this one: its window, restored and in front (_show)."""
        for step in (window.restore, window.show):
            try:
                step()
            except Exception:
                pass
        try:   # in front of the other windows, then an ordinary window again
            window.on_top = True
            window.on_top = False
        except Exception:
            pass
    if shower is not None:
        shower["fn"] = bring_forward

    def pick_folder(start: str = "") -> str:
        """The computer's own folder picker, for the portal (a chat's folder, the data folder): the folder the person
        chose, or "" if they closed it."""
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
        try:
            chosen = window.create_file_dialog(kind, directory=start if start and os.path.isdir(start) else "")
        except Exception:
            return ""
        return str(chosen[0]) if chosen else ""

    def pick_file(start: str = "") -> str:
        """The computer's own file picker, for the portal's Import a script or VI…: the Python script, notebook or
        LabVIEW VI (which the portal translates) the person chose, or "" if they closed it. start: the folder to open at
        (or a file in it)."""
        kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
        folder = start if os.path.isdir(start or "") else os.path.dirname(start or "")
        try:
            chosen = window.create_file_dialog(kind, directory=folder if os.path.isdir(folder) else "",
                                               file_types=("Python scripts and LabVIEW VIs (*.py;*.ipynb;*.vi)",
                                                           "Python scripts and notebooks (*.py;*.ipynb)",
                                                           "LabVIEW VIs (*.vi)"))
        except Exception:
            return ""
        return str(chosen[0]) if chosen else ""

    def set_zoom(factor: float) -> bool:
        """The person's text size: this window's page scaled like a browser's zoom (the window's own Ctrl+ and Ctrl-
        are off). False where this window can't."""
        try:
            return _zoom(window, min(2.0, max(0.5, float(factor))))
        except Exception:
            return False

    window.expose(pick_folder, pick_file, set_zoom)
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True   # reports can be saved from the window too
    except (AttributeError, TypeError):
        pass
    webview.start(private_mode=False, storage_path=str(home / "webview"))


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="kelvin-app", description="Kelvin, the coolest cat in the lab")
    ap.add_argument("--home", default=str(HOME), help="where Kelvin keeps its data (default: ~/Kelvin)")
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
    if not args.self_test:
        _watch_for_crashes(home)
    try:
        code = _run(args, home)
    finally:
        close_log()
    if not args.self_test:
        _leave(code)
    return code


def _leave(code: int) -> None:
    """Kelvin quits now that its window is closed and everything it holds is let go of. Python's own exit would wait
    for every thread, and one still inside an instrument's library (a camera that never answered, a GPIB call on a
    hung bus) kept a field test's Kelvin running without a window, holding the camera, until a person ended it."""
    from . import rundata
    try:
        rundata.before_exit()   # every run.h5 closed first, as Python's exit would
    except Exception:
        pass
    logging.shutdown()
    os._exit(code)


APP_MUTEX = "Kelvin-8C0F6E5A"   # the installer and uninstaller wait while Kelvin runs (AppMutex in kelvin.iss)


def _hold_app_mutex() -> Any:
    """Kelvin's sign that it runs on this computer (the installer's AppMutex too): (kernel32, handle), or None off
    Windows. The Kelvin that owns it (_owns) is the one that may open the instruments."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k32.WaitForSingleObject.restype = ctypes.c_uint32
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        handle = k32.CreateMutexW(None, False, APP_MUTEX)
        return (k32, handle) if handle else None
    except Exception:
        return None


def _owns(mutex: Any, wait_s: float) -> bool:
    """Whether this Kelvin owns the mutex now: at once when no other does, else as soon as that one has quit (Windows
    hands over a quitting process's mutex), within wait_s. Held until this Kelvin quits."""
    k32, handle = mutex
    return k32.WaitForSingleObject(handle, int(wait_s * 1000)) in (0, 0x80)   # WAIT_OBJECT_0, WAIT_ABANDONED


def _one_kelvin(mutex: Any, lock: Path, key: str) -> bool:
    """True when this is the one Kelvin on the computer, which may open the instruments. Another that runs is shown
    instead (False). One that is still closing is waited for, as its instruments (a serial port, a camera) are
    still its own until it has quit: a field test's second Kelvin found them busy, and the first crashed meanwhile. One
    that stays without a window is offered to be ended."""
    deadline = time.monotonic() + START_WAIT_S
    while True:
        port = _running_copy(lock)
        if port:   # one runs, with its portal: it comes forward, and this start ends
            _show(port, key, lock)
            return False
        if mutex is None:
            return True
        if _owns(mutex, 0.5):   # no other Kelvin of this kind runs; one from before 0.3.44 doesn't own it: its portal
            port = _running_copy(lock)
            if port:
                _show(port, key, lock)
                return False
            return True
        if time.monotonic() > deadline:
            break
    from .others import other_kelvins
    pids = other_kelvins()
    if not pids:   # it quit just now
        return _owns(mutex, 5.0)
    if not _ask("Kelvin is still running", "A Kelvin from before is still running without a window (process %s) "
                "and may hold instruments. End it and start Kelvin again?" % ", ".join(map(str, pids))):
        return False
    _end(pids)
    return _owns(mutex, 15.0)


def _show(port: int, key: str, lock: Path) -> None:
    """The running Kelvin's window brought forward (its portal in the browser, for one without a window)."""
    try:
        pid = int(json.loads(lock.read_text(encoding="utf-8"))["pid"])
        if sys.platform.startswith("win"):
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(pid)   # this start may hand the foreground over to it
    except Exception:
        pass
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/api/app/show" % port, data=b"{}", method="POST",
                                     headers={"X-Kelvin-Token": key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5):
            return
    except Exception:   # a Kelvin from before 0.3.44: its portal in the browser, as then
        webbrowser.open("http://127.0.0.1:%d/#%s" % (port, key))


def _ask(title: str, text: str) -> bool:
    """Yes or no from the person, in a message box of Windows' own (Kelvin has no window yet)."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        return ctypes.windll.user32.MessageBoxW(None, text, title, 0x04 | 0x30 | 0x10000) == 6   # yes/no, warning
    except Exception:
        return False


def _end(pids: List[int]) -> None:
    """Those Kelvins ended (the person said so), and waited for, 10 s at most."""
    try:
        import psutil
    except ImportError:
        return
    procs = []
    for pid in pids:
        try:
            p = psutil.Process(pid)
            if p.name().lower() == "kelvin.exe":
                p.kill()
                procs.append(p)
        except Exception:
            continue
    try:
        psutil.wait_procs(procs, timeout=10)
    except Exception:
        pass
    log.warning("ended Kelvin process %s, which was still running without a window", ", ".join(map(str, pids)))


def _run(args: argparse.Namespace, home: Path) -> int:
    lock = home / "app.json"
    mutex = _hold_app_mutex()   # held until Kelvin quits

    from .web.settings import Settings
    settings = Settings(home / "settings.json")
    if not args.self_test and not _one_kelvin(mutex, lock, settings.access_key):
        return 0   # one Kelvin per computer: the one that runs is in front now, and nothing here opened anything

    from .accounts import Accounts
    from .agent.approval import WebApprover
    from .agent.runtime import LabRuntime
    from .collection import Collection
    from .homelab import open_home_lab
    from .web.app import create_app
    from .web.store import ChatStore

    updater = None
    if getattr(sys, "frozen", False) and not args.self_test:   # an installed Kelvin keeps itself up to date
        from .updater import Updater
        updater = Updater(home)
        updater.clean()
        ready = updater.pending()
        if ready and settings.updates == "auto" and not ready.get("tried") and sys.platform.startswith("win"):
            log.info("installing Kelvin %s, then starting again", ready["version"])   # not closed properly last time
            updater.install(os.getpid(), relaunch=True)
            return 0

    import uvicorn

    port = args.port or _free_port()
    # the person's own instruments (lab.yaml), none until they set them up; with the instrument collection's cards
    # (the copy here) unless it is switched off. The instruments are connected once the window is open (the
    # instrument setup does it, saying which), as that can take minutes
    collection = Collection(home) if settings.get("collection") != "off" else None
    lab, envelope, info = open_home_lab(home, settings.switched_off, collection, connect=False)
    # people sign in, each to their own data folder; until someone does, what happens is the lab's (~/Kelvin/lab).
    # The self-test checks the app, not a person, so it runs without accounts.
    accounts = None if args.self_test else Accounts(home)
    if accounts is None:
        from .web.app import data_folder_for
        data_dir, problem = data_folder_for(settings, home)   # the folder chosen in Settings, else the lab's own
    else:
        data_dir, problem = home / "lab", None
    runtime = LabRuntime(lab, WebApprover(timeout=300), data_dir, envelope)
    runtime.data_problem = problem
    runtime.lab_info = info
    quit_event = threading.Event()
    closer: dict = {"fn": None}
    shower: dict = {"fn": None}   # brings this Kelvin forward when it is started again (_show)
    chats = home / "lab" / "chats"
    app = create_app(runtime, settings, ChatStore(chats, create=accounts is None),
                     auto_setup=not (args.self_test or args.no_setup),   # set up connected instruments on start
                     hosts=("127.0.0.1:%d" % port, "localhost:%d" % port),
                     on_quit=quit_event.set if args.browser else None, accounts=accounts, updater=updater,
                     on_exit=lambda: (closer["fn"] or quit_event.set)(), sync_collection=not args.self_test,
                     on_show=lambda: (shower["fn"] or (lambda: None))())
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
            shower["fn"] = lambda: webbrowser.open(url)
            webbrowser.open(url)
            while not quit_event.wait(0.5):
                pass
        else:
            _window(url, agent, runtime, home, closer, shower)
        return 0
    finally:
        if _faults is not None and not args.self_test:   # past the deadline: where it is stuck, then it quits anyway
            faulthandler.dump_traceback_later(QUIT_DEADLINE_S, exit=True, file=_faults)
        log.info("Kelvin is quitting")
        server.should_exit = True   # its portal stops, and with it the agent, the watches and the instruments
        thread.join(CLOSE_WAIT_S)
        if thread.is_alive():
            log.warning("Kelvin's portal had not stopped after %.0f s; quitting anyway", CLOSE_WAIT_S)
        if updater is not None and settings.updates == "auto" and updater.state.get("state") != "installing":
            ready = updater.pending()   # automatic updates install when Kelvin closes
            if ready and not ready.get("tried") and sys.platform.startswith("win"):
                try:
                    updater.install(os.getpid(), relaunch=False)
                    log.info("Kelvin %s installs now that Kelvin has closed", ready["version"])
                except Exception:
                    log.exception("could not start the update")
        try:
            if json.loads(lock.read_text(encoding="utf-8")).get("pid") == os.getpid():
                lock.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    sys.exit(main())
