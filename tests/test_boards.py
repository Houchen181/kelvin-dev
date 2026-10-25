"""Boards that run their own software through Jupyter (QICK on a PYNQ board): Kelvin runs a notebook of the board's or a
line of code there, once a person approves it, and leaves a server it starts running."""
import asyncio
import socket
import threading
import time

import pytest

pytest.importorskip("websockets")
pytest.importorskip("starlette")
uvicorn = pytest.importorskip("uvicorn")

from labharness import boards, build_virtual_lab  # noqa: E402
from labharness.gate import ScriptedApprover  # noqa: E402

NAMESERVER = {"content": {"cells": [
    {"cell_type": "markdown", "source": "# Start the Pyro4 name server"},
    {"cell_type": "code", "source": ["import Pyro4\n", "print('ns ready')"]},
    {"cell_type": "code", "source": "!pyro4-ns -n 0.0.0.0 -p 8888"}]}}


def jupyter_app(state, password="xilinx"):
    """A board's Jupyter server, as far as Kelvin uses it: login, contents, kernels and a kernel's websocket."""
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
    from starlette.routing import Route, WebSocketRoute

    def signed_in(conn):
        return conn.cookies.get("username-board") == "ok"

    async def login(request):
        if request.method == "GET":
            page = HTMLResponse('<form><input type="hidden" name="_xsrf" value="x1"/></form>')
            page.set_cookie("_xsrf", "x1")
            return page
        form = await request.form()
        if form.get("_xsrf") != request.cookies.get("_xsrf"):
            return Response(status_code=403)
        if form.get("password") != password:
            return HTMLResponse("Invalid credentials", status_code=401)
        state["logins"] += 1
        done = RedirectResponse("/tree", status_code=302)
        done.set_cookie("username-board", "ok")
        return done

    async def tree(request):
        return HTMLResponse("<title>Home Page</title>")

    async def status(request):
        return JSONResponse({"started": "now"}) if signed_in(request) else Response(status_code=403)

    async def contents(request):
        if not signed_in(request):
            return Response(status_code=403)
        if request.path_params["path"] == "qick/pyro4/00_nameserver.ipynb":
            return JSONResponse(NAMESERVER)
        return Response(status_code=404)

    async def kernels(request):
        if not signed_in(request) or request.headers.get("x-xsrftoken") != "x1":
            return Response(status_code=403)
        state["kernels"] += 1
        return JSONResponse({"id": "k%d" % state["kernels"], "name": "python3"}, status_code=201)

    async def channels(ws):
        if not signed_in(ws):
            await ws.close(code=1008)
            return
        await ws.accept()
        while True:
            try:
                msg = await ws.receive_json()
            except Exception:
                return
            code, parent = msg["content"]["code"], {"msg_id": msg["header"]["msg_id"]}
            state["ran"].append(code)

            def say(kind, content):
                return {"header": {"msg_type": kind}, "parent_header": parent, "content": content, "channel": "iopub"}
            await ws.send_json(say("status", {"execution_state": "busy"}))
            if "pyro4-ns" in code:   # a server: it keeps running
                await ws.send_json(say("stream", {"name": "stdout", "text": "NS running on 0.0.0.0:8888\n"}))
                await asyncio.sleep(3)
                return
            if "qick.__version__" in code:
                await ws.send_json(say("stream", {"name": "stdout", "text": "0.2.436\n"}))
            elif "boom" in code:
                await ws.send_json(say("error", {"ename": "NameError", "evalue": "name 'boom' is not defined"}))
            else:
                await ws.send_json(say("stream", {"name": "stdout", "text": "ns ready\n"}))
            await ws.send_json(say("status", {"execution_state": "idle"}))

    return Starlette(routes=[Route("/login", login, methods=["GET", "POST"]), Route("/tree", tree),
                             Route("/api/status", status), Route("/api/contents/{path:path}", contents),
                             Route("/api/kernels", kernels, methods=["POST"]),
                             WebSocketRoute("/api/kernels/{kernel}/channels", channels)])


@pytest.fixture
def board():
    state = {"ran": [], "kernels": 0, "logins": 0}
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(jupyter_app(state), log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    yield "127.0.0.1:%d" % sock.getsockname()[1], state
    server.should_exit = True
    thread.join(10)


def test_kelvin_starts_qicks_name_server_from_the_boards_notebook(board, tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    address, state = board
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([True]), tmp_path / "data")
    said = rt.run_on_board(address, notebook="qick/pyro4/00_nameserver.ipynb", password="xilinx", wait=1)
    request = rt.approver.requests[0]
    assert request.title == "Run code on the board at 127.0.0.1?"
    assert "print('ns ready')" in request.items[0] and "!pyro4-ns -n 0.0.0.0 -p 8888" in request.items[0]
    assert "xilinx" not in str(request)                              # the password isn't shown
    assert "cell 1: done: ns ready" in said and "cell 2: still running (a server, left running on the board)" in said
    assert "NS running on 0.0.0.0:8888" in said
    assert state["ran"] == ["import Pyro4\nprint('ns ready')", "!pyro4-ns -n 0.0.0.0 -p 8888"]


def test_reading_the_boards_qick_version_and_what_goes_wrong(board, tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    address, state = board
    rt = LabRuntime(build_virtual_lab(), ScriptedApprover([True, True, False]), tmp_path / "data")
    assert "0.2.436" in rt.run_on_board(address, code="import qick; print(qick.__version__)", password="xilinx")
    assert "NameError: name 'boom' is not defined" in rt.run_on_board(address, code="boom", password="xilinx")
    assert rt.run_on_board(address, code="print(1)", password="xilinx").startswith("declined")
    assert "did not accept the password" in rt.run_on_board(address, code="print(1)", password="wrong")
    assert "on the internet" in rt.run_on_board("8.8.8.8:9090", code="print(1)")
    assert "one of them" in rt.run_on_board(address)
    assert len(state["ran"]) == 2


def test_board_addresses():
    assert boards.board_url("192.168.3.1") == "http://192.168.3.1:9090"
    assert boards.board_url("http://board.local:8888/lab") == "http://board.local:8888"
    assert boards.board_url("https://10.0.0.2") == "https://10.0.0.2:9090"
