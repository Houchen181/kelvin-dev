"""Starting a turn and choosing a compared answer read the chat off the server's loop (wave 2's leftover of the review
of 0.3.45, 3.9): ChatStore tries a file Windows says is in use again for up to 2 s, and on the loop every Stop and
every poll waited meanwhile."""
import ast
import asyncio
import inspect
import time

import pytest

pytest.importorskip("mcp")

from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.web.agent import Busy, ChatAgent  # noqa: E402
from labharness.web.providers.base import ModelInfo, Provider, TurnResult  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from simlab import build_virtual_lab, demo_envelope  # noqa: E402


class Two(Provider):
    name, label = "two", "Two"

    def available(self):
        return True

    async def models(self):
        return [ModelInfo("a", "Model A", self.name), ModelInfo("b", "Model B", self.name)]

    async def stream(self, model, system, history, tools, emit):
        return TurnResult([{"type": "text", "text": "hi"}], stop_reason="end", model=model)


def test_the_portal_and_the_wakeups_never_read_a_chat_on_the_loop_to_start_or_choose():
    from labharness.web import app as app_module
    from labharness.web import wakeups
    on_loop = []
    for module in (app_module, wakeups):
        for fn in ast.walk(ast.parse(inspect.getsource(module))):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                name = node.func.attr
                if name in ("claim", "claim_compare") and len(node.args) + len(node.keywords) < 3:
                    on_loop.append("%s.%s: %s without the chat" % (module.__name__, fn.name, name))
                if name == "choose":
                    on_loop.append("%s.%s: choose on the loop" % (module.__name__, fn.name))
    assert on_loop == []


def test_choosing_an_answer_reads_and_writes_the_chat_off_the_loop_and_holds_it_meanwhile(tmp_path, monkeypatch):
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    store = ChatStore(tmp_path / "chats")
    chat = store.create("two:a")
    lanes = [{"model": "two:a", "messages": [{"role": "assistant", "provider": "two", "model": "a", "raw": None,
                                              "blocks": [{"type": "text", "text": "A says"}]}]},
             {"model": "two:b", "messages": [{"role": "assistant", "provider": "two", "model": "b", "raw": None,
                                              "blocks": [{"type": "text", "text": "B says"}]}]}]
    chat["messages"] = [{"role": "user", "text": "which?", "compare": {"models": ["two:a", "two:b"], "lanes": lanes,
                                                                        "chosen": 0}}] + list(lanes[0]["messages"])
    store.save(chat)
    own_load = store.load

    def slow_load(chat_id):
        time.sleep(0.4)   # the file in use for a moment
        return own_load(chat_id)
    monkeypatch.setattr(store, "load", slow_load)
    lags, refused = [], []

    async def day():
        agent = ChatAgent(rt, store, build_server(rt), {"two": Two()})
        done = asyncio.Event()

        async def ticker():
            while not done.is_set():
                t0 = time.monotonic()
                await asyncio.sleep(0.02)
                lags.append(time.monotonic() - t0 - 0.02)

        async def choose():
            try:
                return await agent.choose_off_loop(chat["id"], 1)
            finally:
                done.set()

        async def meanwhile():   # a message sent while the answer is being changed
            await asyncio.sleep(0.1)
            try:
                agent.claim(chat["id"], "two:b", chat)
            except Busy as e:
                refused.append(str(e))
        out = await asyncio.gather(ticker(), choose(), meanwhile())
        return out[1]
    chosen = asyncio.run(day())
    assert chosen == {"chosen": 1, "model": "two:b"}
    assert own_load(chat["id"])["messages"][-1]["blocks"][0]["text"] == "B says"
    assert lags and max(lags) < 0.25, max(lags)
    assert refused and "changing to another model's answer" in refused[0]
