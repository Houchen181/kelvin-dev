"""The temporary Claude Code bridge: chats through the person's own `claude`, and Claude operating the screen."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")
anyio = pytest.importorskip("anyio")

from starlette.testclient import TestClient  # noqa: E402

from labharness import claudecode, screen  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.web.agent import ChatAgent  # noqa: E402
from labharness.web.app import create_app, make_providers  # noqa: E402
from labharness.web.providers.claudecode_provider import ClaudeCodeProvider, Shown, parse, transcript  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from test_web import Scripted, parts  # noqa: E402,F401

FAKE = [sys.executable, str(Path(__file__).with_name("fake_claude.py"))]


def run(coro):
    return asyncio.run(coro)


def test_tool_markup_never_reaches_the_page():
    s = Shown()
    pieces = ["Checking", " the gate.\n<tool", '_call name="get_value">{"a"', ': 1}</tool_c', "all>", " Done <", "b>"]
    shown = "".join(s.feed(p) for p in pieces) + s.buf
    assert shown == "Checking the gate.\n Done <b>"
    blocks = parse('Hi.\n<tool_call name="get_value">{"instrument": "smu"}</tool_call>\n'
                   '<tool_call name="set_value">not json</tool_call>')
    assert blocks[0] == {"type": "text", "text": "Hi."}
    assert [(b["name"], b["input"]) for b in blocks[1:]] == [("get_value", {"instrument": "smu"}), ("set_value", None)]
    text = transcript([{"role": "user", "text": "gate?"},
                       {"role": "assistant", "blocks": [{"type": "tool_call", "id": "c", "name": "get_value",
                                                         "input": {"instrument": "smu"}}]},
                       {"role": "tool", "results": [{"id": "c", "name": "get_value", "content": "0 V",
                                                     "is_error": False}]}])
    assert '<tool_call name="get_value">{"instrument": "smu"}</tool_call>' in text
    assert '<tool_result name="get_value" error="false">\n0 V' in text


def test_a_chat_turn_through_claude_code(tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-for-claude-code")
    provider = ClaudeCodeProvider(FAKE, tmp_path)
    events = []

    async def emit(e):
        events.append(e)
    tools = [{"name": "get_value", "description": "Read a value.", "schema": {"type": "object"}}]
    result = run(provider.stream("opus", "You are Kelvin.", [{"role": "user", "text": "Where is the gate?"}], tools, emit))
    assert result.stop_reason == "tool_calls"
    assert result.tool_calls[0]["name"] == "get_value" and result.tool_calls[0]["input"] == {"instrument": "smu",
                                                                                            "parameter": "V"}
    assert "".join(e["delta"] for e in events if e["type"] == "text").strip() == "Checking the gate."
    call = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    a = call["args"]
    assert a[a.index("--tools") + 1] == "" and "--no-session-persistence" in a and a[a.index("--model") + 1] == "opus"
    assert call["system"].startswith("You are Kelvin.") and "## get_value" in call["system"]   # Kelvin's prompt, tools
    assert "Where is the gate?" in call["stdin"]
    assert list(tmp_path.glob("system-*")) == []                     # the prompt file is gone afterwards
    env = claudecode.child_env()
    assert "ANTHROPIC_API_KEY" not in env                            # the person's plan, not a stray key


def test_claude_code_errors_are_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")

    async def emit(e):
        pass
    with pytest.raises(RuntimeError, match="Not logged in"):
        run(ClaudeCodeProvider(FAKE, tmp_path).stream("opus", "x", [{"role": "user", "text": "hi"}], [], emit))
    assert claudecode.status(FAKE) == {"signed_in": True, "method": "claude.ai"}


def test_only_on_the_developers_computer(tmp_path, monkeypatch):
    fake_cli = tmp_path / "claude.exe"
    fake_cli.write_text("x")
    settings = Settings(tmp_path / "settings.json")
    settings.update({"claude_code_path": str(fake_cli)})
    assert "claudecode" in make_providers(settings)                 # from source: there when claude is found
    settings.update({"claude_code": "off"})
    assert "claudecode" not in make_providers(settings)             # ...and the person can switch it off
    settings.update({"claude_code": "on"})
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert not claudecode.enabled() and "claudecode" not in make_providers(settings)   # a published build: never


def operate(runtime, store, steps, slow=False):
    """A chat whose model asks Claude to operate the computer."""
    call = [{"type": "tool_call", "id": "c1", "name": "operate_computer",
             "input": {"task": "Open LaserDesk and read the power"}}]
    done = [{"type": "text", "text": "Done."}]

    async def main():
        agent = ChatAgent(runtime, store, build_server(runtime),
                          {"scripted": Scripted([call, done]), "claudecode": ClaudeCodeProvider(FAKE, store.root)})
        await agent.start()
        try:
            chat = store.create("scripted:m1")
            provider = agent.claim(chat["id"], "scripted:m1")
            events = []

            async def emit(e):
                events.append(e)
            task = asyncio.ensure_future(agent.run_turn(chat["id"], "read the laser power", "scripted:m1", provider, emit))
            for _ in range(100):
                await asyncio.sleep(0.05)
                if runtime.operating:
                    steps.append(runtime.operating)
                    break
            if slow:
                await asyncio.sleep(0.6)
                agent.stop()
            await asyncio.wait_for(task, 20)
            return events
        finally:
            await agent.close()
    return anyio.run(main)


def test_claude_operates_the_computer_and_every_step_is_recorded(parts):
    runtime, _, store = parts
    seen = []
    events = operate(runtime, store, seen)
    assert seen == ["Open LaserDesk and read the power"] and runtime.operating is None
    end = next(e for e in events if e["type"] == "tool_end")
    assert not end["is_error"] and "2 steps" in end["content"] and "5 mW" in end["content"]
    assert [e["text"] for e in events if e["type"] == "computer_step"] == ["looked at the screen", "clicked at (120, 340)"]
    record = [e for e in runtime.gate.log.events() if e["type"].startswith("computer")]
    assert [e["type"] for e in record] == ["computer_task", "computer", "computer", "computer_task_end"]
    assert record[1]["by"] == "Claude Code"


def test_stop_ends_the_operation(parts, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SLOW", "1")
    runtime, _, store = parts
    events = operate(runtime, store, [], slow=True)
    end = next(e for e in events if e["type"] == "tool_end")
    assert end["is_error"] and "stopped" in end["content"] and runtime.operating is None


def test_kelvins_approvals_are_locked_while_claude_operates(parts):
    runtime, settings, store = parts
    client = TestClient(create_app(runtime, settings, store, hosts=["testserver"], providers={}))
    h = {"X-Kelvin-Token": settings.access_key}
    runtime.operating = "Open LaserDesk"
    r = client.post("/api/mode", headers=h, json={"mode": "auto"})
    assert r.status_code == 423 and "Stop" in r.json()["error"]
    assert client.post("/api/approvals/x", headers=h, json={"approve": True}).status_code == 423
    assert client.get("/api/mode", headers=h).status_code == 200      # looking is fine
    assert client.post("/api/stop", headers=h, json={}).status_code == 200   # and Stop always works
    runtime.operating = None
    assert client.post("/api/mode", headers=h, json={"mode": "ask"}).status_code == 200


def test_screen_tools(monkeypatch):
    from PIL import Image
    monkeypatch.setattr(screen, "grab", lambda: Image.new("RGB", (2732, 1536), "white"))
    png, note = screen.screenshot_png()
    assert png[:4] == b"\x89PNG" and "shown at 1366x768" in note
    assert screen.to_screen(100, 50) == (200, 100)                     # back to the screen's own pixels
    assert screen.parse_keys("Ctrl+Shift+S") == [0x11, 0x10, ord("S")]
    with pytest.raises(ValueError):
        screen.parse_keys("hyper+q")
    assert screen.describe("mcp__screen__type_into", {"text": "5 mW"}) == "typed '5 mW'"

    async def listed():
        from mcp import Client
        async with Client(screen.build()) as c:
            names = [t.name for t in (await c.list_tools()).tools]
            shot = await c.call_tool("screenshot", {})
            return names, [b.type for b in shot.content]
    names, kinds = run(listed())
    assert {"screenshot", "click_at", "type_into", "press_keys"} <= set(names) and kinds == ["image", "text"]
