"""The Terminal's shells (web/shells.py; field report 2026-11-15, item 13): the person's own shell on this computer in a
tab of the portal's Terminal, as Claude Code's terminal panel has one, so a lab's bring-up commands (python -m nvpulse
check tc) run there instead of in a PowerShell window of their own. A real terminal on Windows (ConPTY, through
pywinpty) and elsewhere (a pseudo-terminal); on pipes where pywinpty is missing.

Only this computer's own page opens one (the access key, its Host and its Origin on the WebSocket's handshake); never a
phone, on the network listener or through the Kelvin hub; never the AI. A shell ends with its tab, its person's
sign-out and Kelvin, with everything it started. The lab's record says each one opened and closed, never what was
typed or shown.

The pseudo-terminal's tests need pywinpty on Windows (they are skipped without it: run them with a Python that has
it) and run as they are on Linux; the pipe shell's run everywhere."""
import json
import os
import queue
import re
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")

from starlette.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from labharness.web import shells as sh  # noqa: E402
from test_web import Scripted, client_for, parts  # noqa: E402,F401  (fixture)

WINDOWS = os.name == "nt"
PTY = sh.terminal_kind()[0] is not None
MODES = ["pty", "pipe"]
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """The shell without the person's profile: quicker, and the same on every computer."""
    program, argv = sh.shell_command()
    argv = (argv + ["-NoProfile"] if WINDOWS else [argv[0], "--noprofile", "--norc"] if program == "bash"
            else [argv[0], "-f"] if program == "zsh" else argv)
    monkeypatch.setattr(sh, "shell_command", lambda: (program, argv))


def mode(monkeypatch, which):
    """A shell on a pseudo-terminal (skipped where there is none) or on pipes, as without pywinpty."""
    if which == "pty" and not PTY:
        pytest.skip("no pseudo-terminal here: %s" % sh.terminal_kind()[1])
    if which == "pipe":
        monkeypatch.setattr(sh, "terminal_kind", lambda: (None, "pywinpty isn't installed"))


def key(token):
    return ["kelvin.shell", "kelvin.key." + token.encode("utf-8").hex()]


def connect(client, token, origin="http://testserver", url="/api/shell?cols=100&rows=30", **kw):
    headers = {"origin": origin} if origin else {}
    return client.websocket_connect(url, subprotocols=key(token) if token is not None else ["kelvin.shell"],
                                    headers=headers, **kw)


class Tab:
    """A Terminal tab as the page holds it: its socket, and everything the shell sent, read on a thread of its own;
    each output said written once read, as the page's terminal does (acking=False: a page that stalls)."""

    def __init__(self, ws, acking=True):
        self.ws, self.got, self.messages, self.acking = ws, queue.Queue(), [], acking
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            while True:
                m = self.ws.receive_json()
                if m["t"] == "out" and self.acking:
                    self.ws.send_json({"t": "ack", "n": len(m["d"].encode("utf-16-le")) // 2})
                self.got.put(m)
        except Exception as e:   # the socket closed
            self.got.put({"t": "<closed>", "error": repr(e)})

    def until(self, test, timeout=40.0):
        deadline = time.time() + timeout
        while True:
            try:
                m = self.got.get(timeout=max(0.01, deadline - time.time()))
            except queue.Empty:
                raise AssertionError("not in %.0f s; the tab shows %r" % (timeout, self.text()[-1500:]))
            self.messages.append(m)
            if test(m):
                return m
            if m["t"] == "<closed>":
                raise AssertionError("the socket closed: %r; the tab shows %r" % (m, self.text()[-1500:]))

    def text(self):
        return ANSI.sub("", "".join(m.get("d", "") for m in self.messages if m["t"] == "out"))

    def shows(self, pattern, timeout=40.0):
        self.until(lambda m: re.search(pattern, self.text()) is not None, timeout)
        return re.findall(pattern, self.text())

    def run(self, line):
        self.ws.send_json({"t": "in", "d": line + "\r"})


def say(head, tail):
    """A line that prints head and tail as one word, joined by the shell: the typed line, as the terminal echoes it,
    never holds what it prints."""
    return ("Write-Output ('%s' + '%s')" if WINDOWS else "echo '%s''%s'") % (head, tail)


def same(a, b):
    """The same folder, however written (Windows' short names: HOUCHE~1)."""
    return Path(a).resolve() == Path(b).resolve()


def events(runtime):
    path = runtime.events_path
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def gone(pid, timeout=10.0):
    """True once the process is no more (a zombie of Linux counts as gone)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return False


def alive(pid):
    if WINDOWS:
        import ctypes
        k32 = ctypes.WinDLL("kernel32")
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            k32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
            return code.value == 259   # STILL_ACTIVE
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    stat = Path("/proc/%d/stat" % pid)
    try:
        return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def a_child(tab):
    """A process the shell starts and leaves running: its id."""
    if WINDOWS:
        tab.run("$p = Start-Process -PassThru -WindowStyle Hidden ping -ArgumentList '-n','600','127.0.0.1'; "
                "'chi' + 'ld=' + $p.Id")
    else:
        tab.run("sleep 600 & echo chi''ld=$!")
    return int(tab.shows(r"child=(\d+)")[-1])


def shells_of(client):
    return client.app.app.state.shells


# ---------------------------------------------------------------------------------- who may open one

def test_only_this_computers_own_page_opens_a_shell(parts):
    """The handshake itself is refused without the access key (which the page offers as a subprotocol, as a browser's
    WebSocket takes no header of its own), with another site's Origin or none, and with a Host that isn't Kelvin's: a
    page of another site, open in the same browser, can't open a shell on this computer."""
    runtime, settings, store = parts
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        for token, origin in ((None, "http://testserver"), ("not-the-key", "http://testserver"),
                              (settings.access_key, "http://evil.example.com"), (settings.access_key, None),
                              (settings.access_key, "null")):
            with pytest.raises(WebSocketDisconnect) as refused:
                with connect(client, token, origin):
                    pass
            assert refused.value.code == 1008, (token, origin)
        with pytest.raises(WebSocketDisconnect) as refused:   # a name that isn't this computer's (DNS rebinding)
            with client.websocket_connect("ws://evil.example.com/api/shell", subprotocols=key(settings.access_key),
                                          headers={"origin": "http://evil.example.com"}):
                pass
        assert refused.value.code == 1008 and refused.value.reason == "bad host"
        assert not shells_of(client).list()

        with connect(client, settings.access_key) as ws:   # the page itself
            tab = Tab(ws)
            hello = tab.until(lambda m: m["t"] == "hello")
            assert ws.accepted_subprotocol == "kelvin.shell"
            assert hello["program"] == sh.shell_command()[0] and hello["kind"] in ("pty", "pipe")
            assert hello["folder"] == str(Path.home())   # no scripts folder in this lab: the person's home
            (shell,) = shells_of(client).list()
            ws.send_json({"t": "close"})
        assert shell.ended.wait(10) and not shells_of(client).list()


def test_phones_never_get_a_shell(parts):
    """Not on the network listener, whatever it is sent; not through the Kelvin hub, whose relay makes HTTP requests
    only and never names this route; and the route itself refuses a phone's request, whatever the middleware did."""
    from labharness.web.remote import allowed
    from labharness.web import relay
    runtime, settings, store = parts
    client, headers = client_for(parts, {"scripted": Scripted([])})
    guard = client.app
    with client:
        guard.remote.port = 8771                            # as if the network listener were running
        with pytest.raises(WebSocketDisconnect) as refused:  # even with the access key, which never works there
            with client.websocket_connect("ws://192.168.1.50:8771/api/shell", subprotocols=key(settings.access_key),
                                          headers={"origin": "http://192.168.1.50:8771"}):
                pass
        assert refused.value.code == 1008 and "own screen" in refused.value.reason
        guard.remote.port = None

        assert not any(allowed(role, method, "/api/shell") for role in ("watch", "control")
                       for method in ("GET", "POST"))       # the hub's relay refuses what a phone may not do
        assert '"type": "websocket"' not in Path(relay.__file__).read_text(encoding="utf-8")

        async def as_a_phone(scope, receive, send):         # a phone's scope, as the relay makes them
            scope = dict(scope, kelvin={"role": "control", "device": {"id": "ios-1", "name": "Ana's phone",
                                                                      "via": "hub"}})
            await guard.app(scope, receive, send)
        with pytest.raises(WebSocketDisconnect) as refused:  # past the middleware: the route itself says no
            with TestClient(as_a_phone).websocket_connect("/api/shell", subprotocols=key(settings.access_key),
                                                          headers={"origin": "http://testserver"}):
                pass
        assert refused.value.code == 1008 and "own screen" in refused.value.reason
        assert not shells_of(client).list()


def test_no_tool_reaches_a_shell():
    """The AI never gets one: no tool of Kelvin's (the portal's chats and agents over MCP have the same tools) opens
    or reads a shell, and nothing the agent runs knows the shells."""
    import anyio
    from mcp import Client
    from simlab import build_virtual_lab
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.agent.server import build_server
    import labharness.agent.server as server
    import labharness.web.agent as web_agent
    rt = LabRuntime(build_virtual_lab(), WebApprover(timeout=1), __import__("tempfile").mkdtemp(), None)

    async def main():
        async with Client(build_server(rt)) as client:
            return (await client.list_tools()).tools
    tools = anyio.run(main)
    assert len(tools) > 50
    for t in tools:
        assert not re.search(r"shell|terminal|powershell|bash", t.name, re.I), t.name
    for module in (server, web_agent):
        text = Path(module.__file__).read_text(encoding="utf-8")
        assert "shells" not in text and "/api/shell" not in text, module.__name__


# ---------------------------------------------------------------------------------- a shell at work

@pytest.mark.parametrize("which", MODES)
def test_a_shell_runs_commands_in_the_scripts_folder_and_keeps_its_folder(parts, tmp_path, monkeypatch, which):
    """It starts in the lab's scripts folder; what it prints comes back as it comes; cd holds for the next command."""
    mode(monkeypatch, which)
    runtime, settings, store = parts
    scripts = tmp_path / "lab scripts"
    (scripts / "nvpulse").mkdir(parents=True)
    settings.set_lab_scripts([str(scripts)])
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client, connect(client, settings.access_key) as ws:
        tab = Tab(ws)
        hello = tab.until(lambda m: m["t"] == "hello")
        assert hello["kind"] == which and hello["folder"] == str(scripts)
        assert ("pywinpty" in hello["note"]) == (which == "pipe")   # said in the tab: no terminal, and why
        tab.run(say("kelvin-4", "2"))
        tab.shows(r"kelvin-42")
        tab.run("cd nvpulse")
        tab.run("Write-Output ('cwd=' + (Get-Location).Path)" if WINDOWS else "echo cwd=$(pwd)")
        assert same(tab.shows(r"cwd=((?:[A-Za-z]:\\|/)[^\r\n']*?nvpulse)")[-1], scripts / "nvpulse")
        ws.send_json({"t": "close"})


def test_resizing_reaches_the_shell(parts, monkeypatch):
    mode(monkeypatch, "pty")
    runtime, settings, store = parts
    client, headers = client_for(parts, {"scripted": Scripted([])})
    ask = ("Write-Output ('size=' + [Console]::WindowWidth + 'x' + [Console]::WindowHeight)" if WINDOWS
           else "echo size=$(stty size | awk '{print $2 \"x\" $1}')")
    with client, connect(client, settings.access_key, url="/api/shell?cols=90&rows=25") as ws:
        tab = Tab(ws)
        tab.until(lambda m: m["t"] == "hello")
        tab.run(ask)
        assert tab.shows(r"size=(\d+x\d+)")[-1] == "90x25"   # as the page's terminal opened
        ws.send_json({"t": "size", "cols": 132, "rows": 41})
        time.sleep(0.5)
        tab.run(ask)
        tab.shows(r"size=132x41")
        ws.send_json({"t": "close"})


@pytest.mark.parametrize("which", MODES)
def test_closing_a_tab_ends_the_shell_and_what_it_started(parts, monkeypatch, which):
    mode(monkeypatch, which)
    runtime, settings, store = parts
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        with connect(client, settings.access_key) as ws:
            tab = Tab(ws)
            tab.until(lambda m: m["t"] == "hello")
            (shell,) = shells_of(client).list()
            child = a_child(tab)
            assert alive(shell.pid) and alive(child)
            ws.send_json({"t": "close"})
            assert shell.ended.wait(10)
        assert gone(shell.pid) and gone(child)   # the job object (Windows), the session (elsewhere)
        closed = [e for e in events(runtime) if e["type"] == "shell_closed"]
        assert [e["why"] for e in closed] == ["its tab was closed"]


def test_a_shell_that_exits_says_so_and_its_tab_waits(parts, monkeypatch):
    mode(monkeypatch, "pty" if PTY else "pipe")
    runtime, settings, store = parts
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client, connect(client, settings.access_key) as ws:
        tab = Tab(ws)
        tab.until(lambda m: m["t"] == "hello")
        tab.run("exit 3")
        end = tab.until(lambda m: m["t"] == "exit")
        assert end["code"] == 3 and end["why"] is None
        (closed,) = [e for e in events(runtime) if e["type"] == "shell_closed"]
        assert closed["why"] == "it exited" and closed["exit"] == 3
        line = __import__("labharness.agent.runtime", fromlist=["event_line"]).event_line(closed)
        assert "closed after" in line and "it exited with code 3" in line


# ---------------------------------------------------------------------------------- the record, and its end

@pytest.mark.parametrize("which", MODES)
def test_the_record_has_each_shell_opened_and_closed_never_what_was_typed(parts, monkeypatch, which):
    mode(monkeypatch, which)
    runtime, settings, store = parts
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        with connect(client, settings.access_key) as ws:
            tab = Tab(ws)
            hello = tab.until(lambda m: m["t"] == "hello")
            (shell,) = shells_of(client).list()
            tab.run(say("zebra-", "quartz"))
            tab.shows(r"zebra-quartz")
            ws.send_json({"t": "close"})
            assert shell.ended.wait(10)
        record = runtime.events_path.read_text(encoding="utf-8")
        assert "zebra" not in record and "quartz" not in record   # neither typed nor shown
        opened, closed = [e for e in events(runtime) if e["type"].startswith("shell_")]
        assert (opened["type"], closed["type"]) == ("shell_opened", "shell_closed")
        assert opened["by"] == "human (portal)" and opened["folder"] == hello["folder"] and opened["kind"] == which
        assert opened["program"] == hello["program"] and isinstance(opened["pid"], int) and "ai" not in opened
        assert closed["shell"] == opened["shell"] and closed["why"] == "its tab was closed"
        assert closed["opened"][:16] == opened["wall"][:16] and closed["seconds"] >= 0
        lines = [x for x in client.get("/api/state", headers=headers).json()["events"] if "shell" in x]
        assert len(lines) == 2 and "a person (portal) opened a shell in the Terminal" in lines[1]
        assert "closed after" in lines[0] and "its tab was closed" in lines[0]   # the Lab panel's Activity: newest first


def test_signing_out_ends_the_persons_shells_in_their_record(tmp_path):
    from simlab import build_virtual_lab, demo_envelope
    from labharness.accounts import Accounts
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    home = tmp_path / "Kelvin"
    Accounts(home).create("Ada", "ada-pass")
    lab = build_virtual_lab(seed=1)
    runtime = LabRuntime(lab, WebApprover(timeout=5), home / "lab", demo_envelope(lab))
    settings = Settings(home / "settings.json")
    app = create_app(runtime, settings, ChatStore(home / "lab" / "chats", create=False), hosts=["testserver"],
                     accounts=Accounts(home), providers={})
    client, h = TestClient(app), {"X-Kelvin-Token": settings.access_key}
    with client:
        with pytest.raises(WebSocketDisconnect) as refused:   # nobody signed in: no shell
            with connect(client, settings.access_key):
                pass
        assert "Sign in" in refused.value.reason
        assert client.post("/api/session", headers=h, json={"name": "Ada", "password": "ada-pass"}).status_code == 200
        with connect(client, settings.access_key) as ws:
            tab = Tab(ws)
            tab.until(lambda m: m["t"] == "hello")
            (shell,) = shells_of(client).list()
            child = a_child(tab)
            assert client.delete("/api/session", headers=h).json()["user"] is None
            end = tab.until(lambda m: m["t"] == "exit")   # the tab hears of it
            assert end["why"] == "its person signed out"
        assert gone(shell.pid) and gone(child)
        ada = home / "users" / "Ada" / "data" / "events.jsonl"
        mine = [json.loads(x) for x in ada.read_text(encoding="utf-8").splitlines()]
        opened, closed = [e for e in mine if e["type"].startswith("shell_")]
        assert opened["account"] == closed["account"] == "Ada" and closed["why"] == "its person signed out"
        assert [e["type"] for e in mine].index("shell_closed") < [e["type"] for e in mine].index("signed_out")


def test_a_shell_asked_for_as_its_person_signs_out_never_opens():
    """A sign-out begins a new turn: a shell asked for before it never opens, nor one while the person changes."""
    record = []
    shells = sh.Shells(lambda type_, **f: record.append(type_))
    turn = shells.turn
    shells.close_all("its person signed out")
    with pytest.raises(sh.ShellRefused):
        shells.open("Ada", "human (portal)", str(Path.home()), turn=turn)
    shells.close_all("its person signed out", hold=True)   # while the person changes, none opens
    with pytest.raises(sh.ShellRefused):
        shells.open("Ben", "human (portal)", str(Path.home()))
    shells.resume()
    assert record == [] and shells.list() == []


def test_kelvin_quitting_ends_every_shell(parts):
    runtime, settings, store = parts
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        shell = shells_of(client).open(None, "human (portal)", str(Path.home()))
        assert alive(shell.pid)
    assert shell.ended.is_set() and gone(shell.pid)
    (closed,) = [e for e in events(runtime) if e["type"] == "shell_closed"]
    assert closed["why"] == "Kelvin quit"


def test_claude_operating_the_screen_never_types_in_sees_or_closes_a_shell(parts):
    """While the Claude Code bridge works the screen, everything in Kelvin but Stop and reading is locked (OPERATING):
    no shell opens; an open one is covered on the page (its screenshots never show it) and takes no typing, and its
    tab's close is refused, on the page and here."""
    runtime, settings, store = parts
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        with connect(client, settings.access_key) as ws:
            tab = Tab(ws)
            tab.until(lambda m: m["t"] == "hello")
            (shell,) = shells_of(client).list()
            runtime.operating = "check the camera"
            assert tab.until(lambda m: m["t"] == "lock")["on"] is True   # the page covers the shell
            tab.run(say("typed-while-", "locked"))
            note = tab.until(lambda m: m["t"] == "note")
            assert "Claude is operating this computer" in note["d"]
            ws.send_json({"t": "close"})                                # Claude pressing x: nothing
            time.sleep(1.0)
            assert not shell.ended.is_set() and shells_of(client).list() == [shell]
            runtime.operating = None
            assert tab.until(lambda m: m["t"] == "lock")["on"] is False
            tab.run(say("typed-after-", "it"))
            tab.shows(r"typed-after-it")
            assert "typed-while-locked" not in tab.text()
            ws.send_json({"t": "close"})
            assert shell.ended.wait(10)
        (closed,) = [e for e in events(runtime) if e["type"] == "shell_closed"]
        assert closed["why"] == "its tab was closed"
        runtime.operating = "check the camera"
        with connect(client, settings.access_key) as ws:
            refused = Tab(ws).until(lambda m: m["t"] == "refused")
            assert "Claude is operating this computer" in refused["message"]
        runtime.operating = None
        assert not shells_of(client).list()


# ---------------------------------------------------------------------------------- the review of 2026-11-15

def test_a_page_gone_as_its_shell_starts_never_leaves_it_running(parts):
    """The shell's hello went out after it had started, outside what closes it: a page reloaded just then (its socket
    closed as the shell started) left the shell running. Seven reloads left seven PowerShells, and past twelve every
    new tab was refused. Now a shell whose page is gone at any moment is closed, and recorded closed."""
    import asyncio
    runtime, settings, store = parts
    client, headers = client_for(parts, {"scripted": Scripted([])})
    offered = key(settings.access_key)
    scope = {"type": "websocket", "path": "/api/shell", "raw_path": b"/api/shell", "query_string": b"cols=80&rows=24",
             "root_path": "", "scheme": "ws", "server": ("testserver", 80), "client": ("127.0.0.1", 50000),
             "headers": [(b"host", b"testserver"), (b"origin", b"http://testserver"),
                         (b"sec-websocket-protocol", ", ".join(offered).encode("ascii"))],
             "subprotocols": offered, "state": {}, "extensions": {}}
    sent = []

    async def receive():
        if not sent:
            return {"type": "websocket.connect"}
        await asyncio.sleep(60)   # a page that is gone says nothing more
        return {"type": "websocket.disconnect", "code": 1006}

    async def send(message):
        sent.append(message)
        if message["type"] == "websocket.send":   # the hello: the page went as the shell started (uvicorn's OSError)
            raise OSError("the connection was closed")

    async def page():
        await asyncio.wait_for(client.app(dict(scope), receive, send), 30)
    with client:
        client.portal.call(page)
        assert [m["type"] for m in sent] == ["websocket.accept", "websocket.send"]
        assert shells_of(client).list() == []
        opened, closed = [e for e in events(runtime) if e["type"].startswith("shell_")]
        assert (opened["type"], closed["type"]) == ("shell_opened", "shell_closed") and closed["shell"] == opened["shell"]
        assert gone(opened["pid"])


def test_an_approval_asked_while_a_shell_is_open_is_asked_by_the_portals_cards(tmp_path, monkeypatch):
    """The Terminal is only the person's shell: it asks no approval (a console of Kelvin's once asked y/N beside the
    shells, and a y and Enter meant for a shell approved a laser's 10 mW). A request asked while a shell is open waits
    in the state the page draws the Lab panel's cards from, with their own Approve and Decline, and nothing typed in
    the shell answers it."""
    from simlab import build_virtual_lab
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.clock import RealClock
    from labharness.web import app as app_module
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from test_fit_rules import _function
    monkeypatch.setattr(app_module, "SOON_S", 0.5)
    runtime = LabRuntime(build_virtual_lab(clock=RealClock(), seed=1), WebApprover(timeout=30), tmp_path / "data")
    settings = Settings(tmp_path / "settings.json")   # no envelope: raising smu.V asks a person
    client, headers = client_for((runtime, settings, ChatStore(tmp_path / "chats")), {"scripted": Scripted([])})
    with client, connect(client, settings.access_key) as ws:
        tab = Tab(ws)
        tab.until(lambda m: m["t"] == "hello")
        plan = "plan: ramp\nsweep: { smu.V: {from: 0, to: 1, step: 0.5} }\nmeasure: [smu.I]\n"
        assert client.post("/api/queue", json={"op": "add", "text": plan}, headers=headers).status_code == 200
        assert client.post("/api/queue", json={"op": "start"}, headers=headers).status_code == 202   # its card waits
        card = runtime.approver.wait_for_pending(5)
        assert card and [p["id"] for p in client.get("/api/state", headers=headers).json()["pending"]] == [card["id"]]
        tab.run("y")   # typed in the shell: the shell's, never an answer
        time.sleep(1.0)
        assert [p["id"] for p in client.get("/api/state", headers=headers).json()["pending"]] == [card["id"]]
        runtime.approver.answer(card["id"], False)
        ws.send_json({"t": "close"})
    js = (Path(sh.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    state = _function(js, "readState")
    assert "PANEL.loose.set(p.id, makeApproval(p));" in state and "TERM" not in state   # every request, as cards
    for gone in ("function termAsk(", "function termState(", "function termAnswer(", "TERM_CMDS", "approve? [y/N]",
                 "/api/terminal", "term-input"):
        assert gone not in js, gone


def test_a_shell_asked_for_by_someone_no_longer_signed_in_never_opens():
    """A sign-out moved the turn on while its person was still signed in: a shell asked for in that moment took the new
    turn and opened after the sign-out, recorded as theirs. Now it is asked again whether that person is still the one
    signed in, before it starts and once it has, under the shells' lock."""
    record = []
    shells = sh.Shells(lambda type_, **f: record.append(type_))
    signed_in = {"user": "Ada"}

    def still():
        return signed_in["user"] == "Ada"
    shells.close_all("its person signed out", hold=True)   # the sign-out begins...
    turn = shells.turn                                     # ...and the shell is asked for now, with the new turn
    signed_in["user"] = None                               # the session cleared before the shells resume
    shells.resume()
    with pytest.raises(sh.ShellRefused):
        shells.open("Ada", "human (portal)", str(Path.home()), turn=turn, still=still)
    answers = iter([True, False])                          # signed out while it started
    with pytest.raises(sh.ShellRefused):
        shells.open("Ada", "human (portal)", str(Path.home()), still=lambda: next(answers))
    assert record == [] and shells.list() == []


def test_ctrl_c_in_a_shell_interrupts_what_runs_there(parts, monkeypatch):
    mode(monkeypatch, "pty")
    runtime, settings, store = parts
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client, connect(client, settings.access_key) as ws:
        tab = Tab(ws)
        tab.until(lambda m: m["t"] == "hello")
        tab.run("ping -t 127.0.0.1" if WINDOWS else "sleep 120")
        if WINDOWS:
            tab.shows(r"Reply from 127\.0\.0\.1")
        else:
            time.sleep(1.0)
        ws.send_json({"t": "in", "d": "\x03"})
        if WINDOWS:
            tab.shows(r"Control-C")   # ping says it was interrupted
        time.sleep(1.0)   # what is typed as Ctrl+C lands is let go of with it, as in a console
        tab.run(say("after-", "ctrl-c"))   # runs only once what ran was interrupted
        tab.shows(r"after-ctrl-c", timeout=20)
        ws.send_json({"t": "close"})


CTRL_C_CHILD = r'''
import ctypes, os, re, sys, time
ctypes.WinDLL("kernel32").SetConsoleCtrlHandler(None, True)   # Kelvin ignoring Ctrl+C, as a launcher's group makes it
from labharness.web import shells as sh
program, argv = sh.shell_command()
sh.shell_command = lambda: (program, argv + ["-NoProfile"])
ignoring = getattr(sh, "_ignoring_ctrl_c", lambda: None)
print("ignoring", ignoring(), flush=True)
shell = sh.Shells(lambda type_, **f: None).open(None, "test", os.getcwd())
got = []
def shows(pattern):
    end = time.time() + 40
    while time.time() < end:
        got.append(shell.take())
        if re.search(pattern, "".join(got)):
            return True
        time.sleep(0.05)
    return False
shows(r"PS .*> ")
shell.write("ping -t 127.0.0.1\r")
shows(r"Reply from")
shell.write("\x03")
print("interrupted", shows(r"Control-C"), flush=True)
print("ready", ignoring(), flush=True)
while not os.path.exists(sys.argv[1]):
    time.sleep(0.05)
shell.close("test")
print("alive", flush=True)
'''

CTRL_C_ON_ITS_CONSOLE = r'''
import ctypes, sys
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.FreeConsole()
if not k32.AttachConsole(int(sys.argv[1])):
    sys.exit(2)
k32.SetConsoleCtrlHandler(None, True)   # this helper itself ignores it
sys.exit(0 if k32.GenerateConsoleCtrlEvent(0, 0) else 3)   # Ctrl+C to every process on that console
'''


def test_kelvin_that_ignored_ctrl_c_still_does_once_its_shells_take_it(tmp_path):
    """Kelvin started ignoring Ctrl+C (in a process group of its own, or by a launcher that ignores it), with a console
    of its own: so did its shells. Kelvin now lets its shells take Ctrl+C, and a Ctrl+C on Kelvin's own console still
    leaves Kelvin running (before: once a shell had opened, that Ctrl+C killed Kelvin)."""
    import subprocess
    import sys
    if not WINDOWS or not PTY:
        pytest.skip("Windows, with pywinpty")
    stop = tmp_path / "stop"
    src = str(Path(sh.__file__).parents[2])
    hidden = subprocess.STARTUPINFO()
    hidden.dwFlags |= subprocess.STARTF_USESHOWWINDOW   # its console of its own, out of sight
    kelvin = subprocess.Popen([sys.executable, "-c", CTRL_C_CHILD, str(stop)], cwd=str(tmp_path),
                              env=dict(os.environ, PYTHONPATH=src), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, startupinfo=hidden, creationflags=subprocess.CREATE_NEW_CONSOLE)
    said = []
    try:
        for line in kelvin.stdout:
            said.append(line.strip())
            if line.startswith("ready"):
                break
        assert "interrupted True" in said, said   # Ctrl+C in its shell reached what ran there
        sent = subprocess.run([sys.executable, "-c", CTRL_C_ON_ITS_CONSOLE, str(kelvin.pid)], timeout=30)
        assert sent.returncode == 0
        time.sleep(2.0)
        assert kelvin.poll() is None, kelvin.communicate()[0]   # Kelvin took it, as it ignored it before
        stop.write_text("stop", encoding="utf-8")
        rest, _ = kelvin.communicate(timeout=30)
        assert "alive" in rest and kelvin.returncode == 0, rest
        assert said[:1] == ["ignoring True"] and said[-1] == "ready False", said   # its shells inherit no ignoring
    finally:
        if kelvin.poll() is None:
            kelvin.kill()


def memory_mb():
    """This process's memory: its private bytes on Windows, its resident set elsewhere."""
    if WINDOWS:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                               "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                               "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
        psapi.GetProcessMemoryInfo(wintypes.HANDLE(-1), ctypes.byref(counters), counters.cb)   # this process
        return counters.PagefileUsage / 2 ** 20
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


def flood(count):
    """A line that prints 4 kB lines for ever, writing how many it printed into count every 500."""
    if WINDOWS:
        return ("$n = 0; while ($true) { $n++; [Console]::Out.Write(('0123456789abcdef' * 256) + \"`n\"); "
                "if ($n %% 500 -eq 0) { [IO.File]::WriteAllText('%s', \"$n\") } }" % count)
    return ("n=0; s=$(printf '0123456789abcdef%%.0s' $(seq 256)); while :; do echo \"$s\"; n=$((n+1)); "
            "[ $((n %% 500)) -eq 0 ] && echo $n > '%s'; done" % count)


def printed(count):
    try:
        return int(count.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


@pytest.mark.parametrize("which", MODES)
def test_a_page_that_stops_reading_never_holds_up_the_shell_nor_fills_kelvins_memory(monkeypatch, tmp_path, which):
    """A tab that stops taking a flooding shell's output (a frozen tab, a debugger's pause): on Windows pywinpty kept
    what wasn't read without end (Kelvin went from 154 MB to 590 MB in 40 s). Now the output is read as it comes,
    Kelvin keeps its latest MAX_PENDING characters and leaves out the rest, saying so, and the program goes on: one
    driving an instrument is never held up by a page."""
    mode(monkeypatch, which)
    count = tmp_path / "count.txt"
    shell = sh.Shells(lambda type_, **f: None).open(None, "human (portal)", str(tmp_path))
    try:
        time.sleep(2.0)
        shell.take()
        before = memory_mb()
        shell.write(flood(count) + ("\r" if shell.kind == "pty" else "\n"))
        deadline = time.time() + 90
        while printed(count) < 10000 and time.time() < deadline:   # 40 MB printed, nobody reading
            time.sleep(0.2)
        first, grown = printed(count), memory_mb() - before
        time.sleep(3.0)
        assert printed(count) > first >= 10000, first   # the program goes on, never held up
        assert grown < 32 and memory_mb() - before < 32, (before, grown, memory_mb())
        assert shell._size <= sh.MAX_PENDING and shell.left_out > 1e6
        said = shell.take()
        assert re.match(r"\x1b\[0m\r\n\x1b\[2m\[\d+\.\d MB of output left out here: this tab didn't keep up with it\]",
                        said) and len(said) < sh.MAX_PENDING + 200
        assert shell.repaint_due() and not shell.repaint_due()
    finally:
        shell.close("test")


def test_a_page_that_stops_saying_it_wrote_gets_no_more_than_its_window(parts, monkeypatch, tmp_path):
    """The page says what its terminal has written; Kelvin sends no more than SHELL_WINDOW beyond that, so a stalled
    page holds no more of a shell's output in Kelvin (nor in its socket) than that and the shell's own latest."""
    from labharness.web.app import SHELL_WINDOW
    mode(monkeypatch, "pty" if PTY else "pipe")
    runtime, settings, store = parts
    count = tmp_path / "count.txt"
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client, connect(client, settings.access_key) as ws:
        tab = Tab(ws, acking=False)
        tab.until(lambda m: m["t"] == "hello")
        (shell,) = shells_of(client).list()
        tab.run(flood(count))
        deadline = time.time() + 40
        while printed(count) < 2000 and time.time() < deadline:   # 8 MB printed
            time.sleep(0.2)
        time.sleep(1.0)
        while not tab.got.empty():
            tab.messages.append(tab.got.get())
        units = sum(len(m["d"].encode("utf-16-le")) // 2 for m in tab.messages if m["t"] == "out")
        assert SHELL_WINDOW <= units <= SHELL_WINDOW + sh.MAX_PENDING + 200, units   # then nothing more came
        assert shell.left_out_all > 0
        ws.send_json({"t": "ack", "n": units})   # the page caught up: the rest comes, after the gap said
        tab.until(lambda m: m["t"] == "out" and "of output left out here" in m["d"])
        ws.send_json({"t": "in", "d": "\x03"})
        ws.send_json({"t": "close"})


def test_links_a_program_prints_open_only_as_web_pages():
    from test_fit_rules import _function
    js = (Path(sh.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    start = _function(js, "shellStart")
    assert "allowNonHttpProtocols: false" in start
    assert 'activate: (e, uri) => { if (/^https?:\\/\\//i.test(uri)) window.open(uri, "_blank", "noopener"); }' in start
    assert "if (i < 0 || tab.locked) return;" in _function(js, "shellClose")   # never closed while Claude works
    assert "if (tab.locked) return;" in _function(js, "shellInput")


# ---------------------------------------------------------------------------------- without a terminal

def test_the_pipe_shell_shows_its_prompt_and_starts_fresh_after_ctrl_c(monkeypatch, tmp_path):
    """Without pywinpty: a line at a time, the prompt (with the folder) after each; a line printing what looks like
    Kelvin's own line is shown as it is (the shell's key is its own); Ctrl+C ends what runs, the shell with it, and
    starts it again in the folder it was in."""
    mode(monkeypatch, "pipe")
    folder = tmp_path / "work"
    folder.mkdir()
    shell = sh.Shells(lambda type_, **f: None).open(None, "human (portal)", str(tmp_path))
    got = []

    def shows(pattern, timeout=40.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            got.append(shell.take())
            if re.search(pattern, "".join(got)):
                return "".join(got)
            time.sleep(0.05)
        raise AssertionError("not in time: %r" % "".join(got)[-1500:])
    def prompt():   # the prompt as the shell says where it is
        return ("PS %s> " if WINDOWS else "%s$ ") % shell.where
    try:
        shows(r"(PS .+> |\$ )$")
        assert same(shell.where, tmp_path)
        shell.write("cd work\n")
        shows(r"work(> |\$ )$")
        assert same(shell.where, folder) and "".join(got).endswith(prompt())
        shell.write(say("kelvin-0123 0 ", "fake") + "\n")   # what Kelvin's own line looks like, with another key
        text = shows(r"kelvin-0123 0 fake\r\n.*(> |\$ )$")
        assert text.endswith("kelvin-0123 0 fake\r\n" + prompt()) and same(shell.where, folder)
        shell.write(("Start-Sleep 600" if WINDOWS else "sleep 600") + "\n")
        time.sleep(1.0)
        first = shell.pid
        shell.write("\x03")
        shows(r"started again in .*work")
        assert gone(first) and shell.pid != first and not shell.ended.is_set()
        shell.write(say("after-", "ctrl-c") + "\n")
        text = shows(r"after-ctrl-c\r\n.*work(> |\$ )$")
        assert same(shell.where, folder)
    finally:
        shell.close("test")


# ---------------------------------------------------------------------------------- the page

VENDOR = Path(sh.__file__).parent / "static" / "vendor" / "xterm"
# xterm.js 6.0.0 and @xterm/addon-fit 0.11.0 as released (registry.npmjs.org: @xterm/xterm/-/xterm-6.0.0.tgz,
# sha512-TQwDdQGt...; @xterm/addon-fit/-/addon-fit-0.11.0.tgz, sha512-jYcgT6xt...), their files unchanged
PINNED = {"xterm.js": "14903579ff54664cd72f8e8699e6961a6272c21863ec1c3b118cdc8af5d4a972",
          "xterm.css": "854a7c0fb70e8b1a083c16797ab827299fb18744f5ad34f227b48337e33293c6",
          "addon-fit.js": "ba3ea256ce0620a0992a197d6c9baea64823fc93d8da07a9e366ca9943c18527"}


def test_the_terminal_draws_with_xterm_from_this_computer(parts):
    import hashlib
    runtime, settings, store = parts
    for name, digest in PINNED.items():
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == digest, name
    for name in ("xterm.LICENSE", "addon-fit.LICENSE"):
        assert "MIT" in (VENDOR / name).read_text(encoding="utf-8") or "Permission is hereby granted" in \
            (VENDOR / name).read_text(encoding="utf-8")
    client, headers = client_for(parts, {"scripted": Scripted([])})
    with client:
        for name in PINNED:
            assert client.get("/static/vendor/xterm/" + name).status_code == 200
        for name in ("xterm.LICENSE", "../app.js", "nope.js"):
            assert client.get("/static/vendor/xterm/" + name).status_code == 404
    static = Path(sh.__file__).parent / "static"
    page, js = (static / "index.html").read_text(encoding="utf-8"), (static / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"https?://[^\"' ]*(cdn|unpkg|jsdelivr)", page + js)   # never a CDN: the app works offline
    assert '"/static/vendor/xterm/xterm.js"' in js and '"/static/vendor/xterm/addon-fit.js"' in js
    assert 'role="tablist" aria-label="Terminal tabs"' in page and 'id="term-new"' in page and 'id="term-shells"' in page
    for gone in ('id="term-kelvin"', 'id="term-tab-kelvin"', 'id="term-input"', 'id="term-out"', "kelvin&gt;"):
        assert gone not in page, gone   # no console of Kelvin's: the Terminal is this computer's shell, in tabs
    # what the old console kept on the browser (its lines typed to Kelvin and to instruments) is let go, never read
    assert 'localStorage.removeItem("lh.termHistory"); localStorage.removeItem("lh.termTab");' in js
    assert 'store("lh.termHistory"' not in js and 'store("lh.termTab"' not in js
    assert '["kelvin.shell", "kelvin.key." + key]' in js   # the access key, as a subprotocol: never in the address
    root = Path(sh.__file__).parents[3]
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "\"pywinpty>=2; sys_platform == 'win32'\"" in project and '"web/static/vendor/xterm/*"' in project
    spec = (root / "packaging" / "kelvin.spec").read_text(encoding="utf-8")
    assert 'PACKAGES += [("winpty", "pywinpty", ())]' in spec   # its module, programs and DLLs in the app
