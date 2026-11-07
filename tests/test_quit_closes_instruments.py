"""Quitting while the portal still answers a request lets go of the instruments all the same (review of 0.3.45, 7.5).

uvicorn's own default waits for every request still being answered (a chat's stream, a scan, a sequence's start held
open) before the portal's closing, which is where the instruments are closed. The launcher gave up on the portal after
CLOSE_WAIT_S and the process ended with them open: a camera left so stays unusable for every program until the
computer's processes are ended. Now the portal ends such requests after QUIT_REQUESTS_S, and the launcher closes the
instruments itself when the portal's closing never got to them, never twice."""
import asyncio
import socket
import threading
import time
import types
import urllib.request
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("uvicorn")
pytest.importorskip("starlette")

from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from labharness import desktop  # noqa: E402
from labharness.clock import SimClock  # noqa: E402
from labharness.gate import ScriptedApprover  # noqa: E402
from labharness.homelab import builtin_registry  # noqa: E402
from labharness.lab import Lab  # noqa: E402
from labharness.labconfig import load_lab_config  # noqa: E402
from labharness.sim.twin import World, sim_backend_factory  # noqa: E402


def wait_for(check, within=5.0):
    end = time.monotonic() + within
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Runtime:
    """The launcher's part of LabRuntime: close_instruments, with each call kept."""

    def __init__(self):
        self.calls = []

    def close_instruments(self, within):
        self.calls.append(within)
        return []


def test_a_quit_while_a_request_is_answered_still_closes_the_instruments(monkeypatch):
    monkeypatch.setattr(desktop, "QUIT_REQUESTS_S", 0.5)
    monkeypatch.setattr(desktop, "CLOSE_WAIT_S", 6.0)
    closed, entered, release = [], threading.Event(), threading.Event()

    @asynccontextmanager
    async def lifespan(app):
        yield
        closed.append(time.monotonic())   # web/app.py's own closing: runtime.close_instruments
        release.set()                     # with the instruments closed, the call that used one ends

    async def scan(request):   # a scan, a bus clear, a sequence's start: still being answered as Kelvin quits
        entered.set()
        await asyncio.to_thread(release.wait, 30)
        return JSONResponse({"ok": True})

    port = free_port()
    server = desktop._server(Starlette(routes=[Route("/api/scan", scan)], lifespan=lifespan), port)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    assert wait_for(lambda: server.started)

    def window():   # the window's request; the window then closes, and its connection with it
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/api/scan" % port, timeout=1)
        except Exception:
            pass
    threading.Thread(target=window, daemon=True).start()
    assert entered.wait(5)
    rt = Runtime()
    began = time.monotonic()
    try:
        desktop._stop_server(server, thread, rt)
    finally:
        release.set()
    assert closed and closed[0] - began < 3   # the portal's closing ran, after QUIT_REQUESTS_S: not never
    assert not thread.is_alive() and rt.calls == []   # it closed them: the launcher closes nothing again


def test_the_launcher_closes_the_instruments_when_the_portal_never_got_to_them(monkeypatch):
    from labharness.web.app import CLOSE_INSTRUMENTS_S
    monkeypatch.setattr(desktop, "CLOSE_WAIT_S", 0.3)
    hung = threading.Event()
    thread = threading.Thread(target=hung.wait, args=(30,), daemon=True)   # a portal whose own closing hangs
    thread.start()
    server, rt = types.SimpleNamespace(should_exit=False), Runtime()
    try:
        desktop._stop_server(server, thread, rt)
    finally:
        hung.set()
    assert server.should_exit and rt.calls == [CLOSE_INSTRUMENTS_S]


def test_the_instruments_are_closed_once_and_a_second_call_waits_for_the_first(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from labharness import adapters
    from labharness.agent.runtime import STILL_CLOSING, LabRuntime
    registry = builtin_registry()
    cfg = {"lab": "bench", "instruments": {"cam": {"card": "virtual/source-meter", "address": "fake://cam"}}}
    lab = Lab(load_lab_config(cfg, registry), registry, sim_backend_factory(World(SimClock(), 1)), SimClock())
    closing, go, calls, visa = threading.Event(), threading.Event(), [], []

    def close(within=None, quitting=False):   # a camera's library, a while closing
        calls.append((within, quitting))
        closing.set()
        go.wait(5)
        return []
    lab.close = close
    monkeypatch.setattr(adapters, "close_resource_manager", lambda: visa.append(1))
    rt = LabRuntime(lab, ScriptedApprover([]), tmp_path / "data")
    first = []
    t = threading.Thread(target=lambda: first.append(rt.close_instruments(5.0)), daemon=True)
    t.start()
    assert closing.wait(5)
    assert rt.close_instruments(0.2) == [STILL_CLOSING]   # the first still at it: nothing is closed a second time
    threading.Timer(0.3, go.set).start()
    assert rt.close_instruments(5.0) == []   # waits for the first, then has its answer
    t.join(5)
    assert first == [[]] and calls == [(5.0, True)] and visa == [1]
    assert rt.close_instruments(1.0) == [] and calls == [(5.0, True)] and visa == [1]   # closed already
