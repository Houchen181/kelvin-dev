"""A model that falls silent is said (review of 0.3.45, section 8).

A model service that stalled kept the chat "answering" with no word until a person pressed Stop: the SDKs read for up
to 10 minutes, then try again. Now, after SILENT_S with nothing from the model, the chat says so, once each time it
falls silent, and Kelvin keeps waiting; Stop ends it as ever."""
import asyncio

import pytest

pytest.importorskip("mcp")

from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.web import agent as agentmod  # noqa: E402
from labharness.web.agent import ChatAgent  # noqa: E402
from labharness.web.providers.base import ModelInfo, Provider, TurnResult  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from simlab import build_virtual_lab, demo_envelope  # noqa: E402


class Halting(Provider):
    """A model that says a little, then nothing for a while (pauses: seconds), and so on; then ends."""
    name, label = "halting", "Halting"

    def __init__(self, pauses, words=("Thinking", "about", "it")):
        self.pauses, self.words = list(pauses), list(words)

    def available(self):
        return True

    async def models(self):
        return [ModelInfo("m1", "Model One", self.name)]

    async def stream(self, model, system, history, tools, emit):
        said = []
        for i, word in enumerate(self.words):
            await emit({"type": "text", "delta": word + " "})
            said.append(word)
            if i < len(self.pauses):
                await asyncio.sleep(self.pauses[i])
        return TurnResult([{"type": "text", "text": " ".join(said)}], stop_reason="end", model=model)


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(agentmod, "SILENT_S", 0.3)
    monkeypatch.setattr(agentmod, "SILENT_LOOK_S", 0.05)


def runtime(tmp_path):
    lab = build_virtual_lab(seed=1)
    return LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))


def turn_events(tmp_path, provider, stop_after=None):
    rt, store, events = runtime(tmp_path), ChatStore(tmp_path / "chats"), []

    async def emit(e):
        events.append(e)

    async def day():
        agent = ChatAgent(rt, store, build_server(rt), {"halting": provider})
        await agent.start()
        try:
            chat = store.create("halting:m1")
            agent.claim(chat["id"], "halting:m1")
            turn = asyncio.ensure_future(agent.run_turn(chat["id"], "think", "halting:m1", provider, emit))
            if stop_after is not None:
                for _ in range(200):
                    if any(e["type"] == "note" and "No word from the model" in e.get("text", "") for e in events):
                        break
                    await asyncio.sleep(0.02)
                agent.stop(chat["id"])
            await asyncio.wait_for(turn, 15)
        finally:
            await agent.close()
    asyncio.run(day())
    return events


def notes(events):
    return [e["text"] for e in events if e["type"] == "note" and "No word from the model" in e.get("text", "")]


def test_a_model_silent_too_long_is_said_once_each_time_and_its_answer_still_comes(tmp_path, quick):
    events = turn_events(tmp_path, Halting([0.8, 0.8]))
    said = notes(events)
    assert len(said) == 2, said                                  # two silences: said once each
    assert said[0].startswith("No word from the model for 1 min") and "press Stop" in said[0]
    assert events[-1] == {"type": "turn_end", "reason": "end"}   # it kept waiting, and the answer came


def test_a_model_that_keeps_talking_is_never_said_to_be_silent(tmp_path, quick):
    assert notes(turn_events(tmp_path, Halting([0.1, 0.1]))) == []


def test_stop_ends_a_silent_model_as_ever(tmp_path, quick):
    events = turn_events(tmp_path, Halting([30.0]), stop_after=True)
    assert len(notes(events)) == 1
    assert events[-1] == {"type": "turn_end", "reason": "stopped"}


def test_a_turn_that_ends_takes_the_models_call_with_it(quick):
    """_answer, cancelled itself (the turn's task cancelled as Kelvin quits), cancels the model's call: it never runs
    on unwatched."""
    async def go():
        inner = asyncio.ensure_future(asyncio.sleep(30))
        outer = asyncio.ensure_future(agentmod._answer(inner, [0.0], lambda e: asyncio.sleep(0)))
        await asyncio.sleep(0.1)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        await asyncio.sleep(0)
        return inner.cancelled()
    assert asyncio.run(go())
