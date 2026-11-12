"""The chat's harness, compared with Claude Code, Codex and DeepSeek's (asked for 2026-11-12): the same model did
better in a general coding harness than in Kelvin's chats, and these were why.

- How hard the model thinks: Settings → Models → Thinking (Deep, i.e. high, unless changed), and the deepest for an
  answer whose message asks Kelvin to think hard; Claude and the ChatGPT plan get it as their effort.
- A model knows no clock: each of the person's messages carries when it was sent (<sent>), kept with it.
- An answer may take up to 100 steps; what stops one going round in circles is pointing out a call made again with the
  same arguments, telling the model to stop and explain after lab calls were refused, declined or tripped, and ending
  the answer after more (a reading that quotes an old refusal is no new try).
- Calls that never got their results (Kelvin closed while a run went) get one saying the outcome is unknown, so the
  chat can go on and the model reads the lab before acting.
- The history stays as it was sent (Claude binds its thinking to it): the list of rarer tools no longer changes when
  one is loaded, a chat keeps its pictures where they came until the day is summarised, a summary's kept turns lose
  their (now unbound) thinking, and Claude is asked to drop rather than refuse a block that is no longer bound.
- The portal's map goes into the prompt as its table of contents; kelvin_help gives each place's point whole.
- The ChatGPT plan's reasoning goes with the conversation (encrypted, as Codex sends it)."""
import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")

from simlab import build_virtual_lab  # noqa: E402
from labharness import guide  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import INSTRUCTIONS, build_server  # noqa: E402
from labharness.gate import ScriptedApprover  # noqa: E402
from labharness.web import agent as agent_mod, compaction  # noqa: E402
from labharness.web.agent import (INTERRUPTED, MAX_KEPT_PICTURES, MAX_STEPS, REFUSALS,  # noqa: E402
                                  REFUSALS_END, REPEATS, SYSTEM, ChatAgent)
from labharness.web.providers.base import EFFORT, TurnResult, sent_line, user_text  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from test_web import Scripted  # noqa: E402

DONE = [{"type": "text", "text": "Done."}]


class Watching(Scripted):
    """A scripted model that also notes how hard it was asked to think and the history it was given."""

    def __init__(self, turns, **kw):
        super().__init__(turns, **kw)
        self.efforts, self.histories = [], []

    async def stream(self, model, system, history, tools, emit):
        self.efforts.append(EFFORT.get())
        self.histories.append(history)
        return await super().stream(model, system, history, tools, emit)


class Remembering(Watching):
    """One that needs its history as it was sent, as Claude does."""
    keeps_history = True
    sees_images = True


def day(tmp_path, provider, texts, settings=None, before=None):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    store = ChatStore(tmp_path / "chats")
    agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider}, settings=settings)
    chat = store.create("scripted:m1")
    if before:
        before(chat)
        store.save(chat)
    events = []

    async def emit(e):
        events.append(e)

    async def go():
        await agent.start()
        try:
            for text in texts:
                agent.claim(chat["id"], "scripted:m1")
                await agent.run_turn(chat["id"], text, "scripted:m1", provider, emit)
        finally:
            await agent.close()
    asyncio.run(go())
    return store.load(chat["id"]), events


def call(name, i="1", **args):
    return [{"type": "tool_call", "id": name + i, "name": name, "input": args}]


# ------------------------------------------------------------------ thinking

def test_thinking_is_a_setting_and_a_hard_question_gets_the_deepest(tmp_path):
    settings = Settings(tmp_path / "settings.json")
    assert settings.thinking == "high" and settings.public()["thinking"] == "high"   # Deep unless changed
    provider = Watching([DONE, DONE, DONE])
    day(tmp_path / "a", provider, ["How is the lab?", "Please think hard about why the fit is off.",
                                   "I think hard drives are slow."], settings=settings)
    assert provider.efforts == ["high", "xhigh", "high"]   # only an answer that asks for it thinks hardest
    settings.update({"thinking": "medium"})
    assert settings.thinking == "medium" and settings._user["thinking"] == "medium"
    settings.update({"thinking": "high"})
    assert "thinking" not in settings._user                 # the default is not stored
    settings.update({"thinking": "max please"})
    assert settings.thinking == "high"                      # only Standard, Deep and Deepest
    assert EFFORT.get() == ""                               # a turn's effort ends with it


def test_claude_gets_the_turns_effort_and_drops_unbound_thinking():
    pytest.importorskip("anthropic")
    from test_prompt_cache import TOOLS, FakeClaude, quiet
    from labharness.web.providers import anthropic_provider as ap
    provider = ap.AnthropicProvider("test-key")
    provider.client = claude = FakeClaude()
    hi = [{"role": "user", "text": "hi"}]
    asyncio.run(provider.stream("claude-opus-5-5", "Kelvin's rules", hi, TOOLS, quiet))
    sent = claude.sent[-1]
    assert sent["output_config"] == {"effort": "high"}                     # a lab's work: Deep by default
    assert sent["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert ap.BINDING_BETA in sent["betas"] and ap.FALLBACK_BETA in sent["betas"]

    async def deepest():
        token = EFFORT.set("xhigh")
        try:
            await provider.stream("claude-haiku-5-5", "Kelvin's rules", hi, TOOLS, quiet)
        finally:
            EFFORT.reset(token)
    asyncio.run(deepest())
    sent = claude.sent[-1]
    assert sent["output_config"] == {"effort": "xhigh"} and sent["betas"] == [ap.BINDING_BETA]   # no fallback for Haiku


def test_a_route_that_refuses_the_binding_control_goes_on_without_it():
    pytest.importorskip("anthropic")
    import anthropic
    import httpx
    from test_prompt_cache import TOOLS, FakeClaude, quiet
    from labharness.web.providers import anthropic_provider as ap

    class Gateway(FakeClaude):
        def _stream(self, **kwargs):
            if "block_binding" in kwargs["thinking"]:
                self.sent.append(kwargs)
                raise anthropic.BadRequestError(
                    "thinking.block_binding: Extra inputs are not permitted", body=None,
                    response=httpx.Response(400, request=httpx.Request("POST", "https://hub.example/v1/messages")))
            return super()._stream(**kwargs)
    provider = ap.AnthropicProvider("test-key")
    provider.client = gateway = Gateway()
    hi = [{"role": "user", "text": "hi"}]
    result = asyncio.run(provider.stream("claude-sonnet-5-5", "rules", hi, TOOLS, quiet))
    assert result.stop_reason == "end" and provider.binding is False
    assert "block_binding" not in gateway.sent[-1]["thinking"] and gateway.sent[-1]["betas"] == [ap.FALLBACK_BETA]
    asyncio.run(provider.stream("claude-haiku-5-5", "rules", hi, TOOLS, quiet))
    assert "betas" not in gateway.sent[-1]                                 # remembered, and nothing left to ask for


def test_the_claude_code_bridge_thinks_as_the_setting_says(tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    from labharness.web.providers.claudecode_provider import ClaudeCodeProvider
    fake = [sys.executable, str(Path(__file__).with_name("fake_claude.py"))]
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    provider = ClaudeCodeProvider(fake, tmp_path)

    async def emit(e):
        pass

    async def answer():
        token = EFFORT.set("xhigh")
        try:
            return await provider.stream("opus", "You are Kelvin.", [{"role": "user", "text": "hi"}], [], emit)
        finally:
            EFFORT.reset(token)
    asyncio.run(answer())
    args = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["args"]
    assert args[args.index("--effort") + 1] == "xhigh"
    monkeypatch.setenv("FAKE_CLAUDE_OLD", "1")                              # a `claude` without --effort
    result = asyncio.run(answer())
    assert result.blocks and provider.effort is False
    assert "--effort" not in json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["args"]


# ------------------------------------------------------------------ the clock

def test_each_message_says_when_it_was_sent(tmp_path):
    provider = Watching([DONE, DONE])
    chat, _ = day(tmp_path, provider, ["first", "second"])
    first, second = [m for m in chat["messages"] if m["role"] == "user"]
    assert first["sent"] == sent_line(first["sent_at"]) and "after" not in first["sent"]
    assert second["sent"] == sent_line(second["sent_at"], first["sent_at"])
    seen = provider.sent[-1]   # as the model got them
    assert seen[0].startswith(first["sent"] + "\n") and seen[1].startswith(second["sent"] + "\n")   # as it was kept
    assert "<sent>" in SYSTEM
    import time
    later = {"role": "user", "text": "third"}
    chat["messages"].append({"role": "user", "text": "", "notice": True})     # Kelvin's own: no time of its own
    second["sent_at"] = time.time() - 7500                                   # the person's last, 2 h 5 min ago
    ChatAgent._sent(chat, later)
    assert later["sent"].endswith("2 h 5 min after the person's message before</sent>")
    woke = {"role": "user", "text": "the fridge is cold", "wake": "the cool-down"}   # Kelvin continuing by itself
    ChatAgent._sent(chat, woke)
    assert ": Kelvin continued the chat, 2 h 5 min after the person's last message before" in woke["sent"]
    assert "sent_at" not in woke                                             # the person's own messages count


# ------------------------------------------------------------------ many steps, and no circles

def test_an_answer_may_take_many_steps_and_a_repeated_call_is_pointed_out(tmp_path):
    assert MAX_STEPS >= 100
    steps = [call("describe_instrument", str(i), instrument="smu") for i in range(REPEATS + 1)] + \
        [call("get_value", "g%d" % i, instrument="smu", parameter="V") for i in range(REPEATS + 1)] + [DONE]
    provider = Watching(steps)
    chat, events = day(tmp_path, provider, ["describe it, again and again"])
    results = [m["results"][0] for m in chat["messages"] if m["role"] == "tool"]
    assert len(results) == 2 * (REPEATS + 1) and not any(e.get("reason") == "step_limit" for e in events)
    assert "note" not in results[REPEATS - 2]
    note = "you have made this exact call, with these arguments, %d times" % REPEATS
    assert note in results[REPEATS - 1]["note"] and note not in results[REPEATS - 1]["content"]   # the tool's own
    assert not any("note" in r for r in results[REPEATS + 1:])              # a reading, polled on purpose
    seen = [m for m in provider.histories[-1] if m["role"] == "tool"][REPEATS - 1]["results"][0]["content"]
    assert seen.endswith(results[REPEATS - 1]["note"])                       # the model reads it with the result


def test_refusals_make_the_model_stop_and_then_end_the_answer(tmp_path, monkeypatch):
    refused = "refused: smu.V 50 V is beyond the lab's limit (10 V, the lab config)"

    async def dispatch(self, chat, provider, context, call, emit):   # every drive refused; readings answer
        return (refused if call["name"] == "set_value" else "smu.V = 0 V"), False, None
    monkeypatch.setattr(ChatAgent, "_dispatch", dispatch)
    steps = []
    for i in range(REFUSALS_END):   # a reading between tries doesn't make the next try a new question
        steps += [call("set_value", str(i), instrument="smu", parameter="V", value=50 + i),
                  call("get_value", "g%d" % i, instrument="smu", parameter="V")]
    chat, events = day(tmp_path, Watching(steps + [DONE]), ["take the gate to 50 V"])
    sets = [m["results"][0] for m in chat["messages"] if m["role"] == "tool" and m["results"][0]["name"] == "set_value"]
    assert len(sets) == REFUSALS_END                          # it ended there: nothing after the fifth try
    assert "note" not in sets[REFUSALS - 2] and all(r["content"] == refused for r in sets)
    assert "%d calls in this answer were refused" % REFUSALS in sets[REFUSALS - 1]["note"]
    assert events[-1] == {"type": "turn_end", "reason": "refusals"}
    assert any(e["type"] == "note" and "refused, declined or stopped by a guard" in e["text"] for e in events)


def test_a_reading_that_quotes_an_old_refusal_is_no_new_try(tmp_path, monkeypatch):
    async def dispatch(self, chat, provider, context, call, emit):   # an overnight queue item nobody approved
        said = "queue: paused\n3. [declined] plan iv sweep: declined: plan iv sweep not run (nobody answered)"
        return said, False, None
    monkeypatch.setattr(ChatAgent, "_dispatch", dispatch)
    steps = [call("queue_show", str(i)) for i in range(REFUSALS_END + 1)] + [DONE]
    chat, events = day(tmp_path, Watching(steps), ["how did the night go?"])
    results = [m["results"][0] for m in chat["messages"] if m["role"] == "tool"]
    assert len(results) == REFUSALS_END + 1 and not any("note" in r for r in results)
    assert events[-1] == {"type": "turn_end", "reason": "end"}


def test_refusal_is_what_the_gate_guards_runs_and_people_say():
    for said in ("refused: beyond the limit", "declined: smu.V stays 0 V", "plan iv-gate refused: interlock",
                 "script not run: refused: smu.V beyond its limit", "smu.V tripped at 5 V: |smu.I| = 2 µA > 1 µA; cut",
                 "run 20261112-1400-iv aborted after 3/50 points: smu.V tripped",
                 '<second_opinion verdict="go">fine</second_opinion>\nplan iv refused: interlock'):
        assert agent_mod.refusal(said), said
    for said in ("smu.V 0 V → 1 V", "not run: the user pressed stop", "will be refused: smu.V beyond its limit",
                 "plan iv: 41 points, 2 min"):
        assert not agent_mod.refusal(said), said


# ------------------------------------------------------------------ calls left without their results

def test_calls_kelvin_never_finished_get_an_unknown_outcome(tmp_path):
    def interrupted(chat):   # a run went, and Kelvin closed: the step is kept, its result never came
        chat["messages"] += [
            {"role": "user", "text": "run the IV"},
            {"role": "assistant", "provider": "scripted", "model": "m1", "raw": None, "blocks": [
                {"type": "text", "text": "Running it."},
                {"type": "tool_call", "id": "run1", "name": "run_plan", "input": {"plan": "plan: iv"}}]},
            {"role": "event", "text": "Kelvin started."}]
    provider = Watching([DONE])
    chat, _ = day(tmp_path, provider, ["what happened?"], before=interrupted)
    roles = [m["role"] for m in chat["messages"]]
    assert roles[:4] == ["user", "assistant", "tool", "event"]               # right after its step
    result = chat["messages"][2]["results"][0]
    assert result["id"] == "run1" and result["is_error"] and result["content"] == INTERRUPTED
    assert "read the instruments' state before acting" in INTERRUPTED
    assert [m["role"] for m in provider.histories[0] if m["role"] != "event"][:3] == ["user", "assistant", "tool"]
    assert ChatAgent._finish_interrupted(chat) == 0                          # once


def test_a_result_given_late_keeps_the_days_summary_where_it_was():
    messages = [{"role": "user", "text": "one"},
                {"role": "assistant", "blocks": [{"type": "tool_call", "id": "c1", "name": "run_plan", "input": {}}]},
                {"role": "user", "text": "two"}, {"role": "assistant", "blocks": [{"type": "text", "text": "ok"}]},
                {"role": "user", "text": "three"}]
    chat = {"messages": messages, "compact": {"upto": 2, "made": 4, "summary": "s"}}
    assert ChatAgent._finish_interrupted(chat) == 1 and messages[2]["role"] == "tool"
    assert chat["compact"]["upto"] == 3 and chat["compact"]["made"] == 5     # still the person's "two"
    assert compaction.view(chat)[0]["text"] == "two"


# ------------------------------------------------------------------ the history as it was sent

def test_loading_a_rarer_tool_leaves_the_prompt_as_it_was(tmp_path):
    provider = Watching([call("find_tools", query="add_instrument"), DONE, DONE])
    chat, _ = day(tmp_path, provider, ["set up the new lock-in", "and now?"])
    assert chat["tools"] == ["add_instrument"] and "add_instrument" in provider.tool_names[2]
    assert provider.systems[0] == provider.systems[2]                       # a later turn's prompt: the same
    assert "add_instrument" in provider.systems[2].split("# More tools")[1]


def test_a_chat_keeps_its_pictures_where_they_came(tmp_path):
    agent = ChatAgent.__new__(ChatAgent)
    agent._picture = lambda chat_id, item: {"mime": "image/png", "data": item["file"]}
    history = [{"role": "user", "text": "look"}] + [
        {"role": "tool", "results": [{"id": "l%d" % i, "name": "look_at", "content": "plot %d" % i,
                                      "is_error": False, "images": [{"file": "p%d.png" % i, "mime": "image/png"}]}]}
        for i in range(12)]
    sent = agent._pictures({"id": "c"}, history, Remembering([]), "m1")
    assert all(m["results"][0].get("images") for m in sent[1:])             # all twelve, none taken out

    class Sees(Watching):
        sees_images = True
    other = agent._pictures({"id": "c"}, history, Sees([]), "m1")
    assert [bool(m["results"][0].get("images")) for m in other[1:]] == [False] * 4 + [True] * 8   # elsewhere: 8
    assert agent_mod.pictures_in(history) == 12 and 12 <= MAX_KEPT_PICTURES <= 20   # Claude: >20 needs small ones
    assert compaction.size("", [], history) >= 12 * compaction.PICTURE_TOKENS   # what pictures cost counts...
    assert compaction.size("", [], history, 0) < 12 * compaction.PICTURE_TOKENS  # ...as many as the model is sent


def test_a_summary_keeps_the_last_turns_without_their_old_thinking():
    thought = {"type": "thinking", "thinking": "", "signature": "sig"}
    said = {"type": "text", "text": "ok"}
    messages = [
        {"role": "user", "text": "one"}, {"role": "assistant", "raw": [thought, said], "blocks": []},
        {"role": "user", "text": "two"}, {"role": "assistant", "raw": [thought, said], "blocks": []},
        {"role": "user", "text": "three"}, {"role": "assistant", "raw": [thought, said], "blocks": []},
        {"role": "user", "text": "four"}, {"role": "assistant", "raw": [thought, said], "blocks": []}]
    chat = {"messages": messages, "compact": {"upto": 2, "made": 6, "summary": "the day so far"}}
    seen = compaction.view(chat)
    assert seen[0]["handoff"] == "the day so far" and seen[0]["text"] == "two"
    assert seen[1]["raw"] == [said] and seen[3]["raw"] == [said]            # written before the summary: no thinking
    assert seen[5]["raw"] == [thought, said]                                # written after it: its own, kept
    assert messages[3]["raw"] == [thought, said]                            # the chat itself keeps everything
    del chat["compact"]["made"]                                             # a summary from before "made"
    assert all(m.get("raw") in (None, [said]) for m in compaction.view(chat) if m["role"] == "assistant")
    assert "## Runs" in compaction.HANDOFF and "1,200 words" in compaction.HANDOFF


# ------------------------------------------------------------------ the prompt

def test_the_prompt_has_the_portals_table_of_contents_and_kelvin_help_the_rest():
    index = guide.portal_index()
    assert SYSTEM.endswith(index) and len(index) < len(guide.portal_map()) / 5
    places = [line[2:] for line in index.splitlines() if line.startswith("- ")]
    assert len(places) >= 30
    for place in places:   # each place's point, whole, among what kelvin_help gives
        assert "[the portal's map ›" in guide.search(place.rstrip(" …")), place
    rules = " ".join(INSTRUCTIONS.split())
    assert "A refusal covers the outcome, not only the command" in rules
    assert "a guard or interlock that tripped, an alarm, a run one of them stopped, or a person's decline is final" \
        in rules and "don't try variations to get past it" in rules     # a refusal, a trip: still final
    assert "A mistake that changed nothing is yours to fix" in rules
    assert "may have changed the lab: read its state first" in rules and "ask the person before driving it" in rules
    assert "keep going until it is done, something above stops it, or you need the person" in rules


# ------------------------------------------------------------------ GPT's reasoning goes with the conversation

def test_the_chatgpt_plan_keeps_its_reasoning_between_steps(tmp_path):
    pytest.importorskip("openai")
    from test_chatgpt import TOOLS, done, signed_in_provider
    call_item = {"type": "function_call", "call_id": "call_1", "name": "kelvin.lab_status", "arguments": "{}",
                 "namespace": "kelvin", "id": "fc_1", "status": "completed"}
    thought = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAAA-sealed"}
    p, client = signed_in_provider(tmp_path, [[done([thought, call_item])], [done([
        {"type": "message", "content": [{"type": "output_text", "text": "Idle."}]}])]])

    async def emit(e):
        return None

    async def answer(history):
        token = EFFORT.set("xhigh")
        try:
            return await p.stream("gpt-6.1-sol", "You are Kelvin.", history, TOOLS, emit)
        finally:
            EFFORT.reset(token)
    first = asyncio.run(answer([{"role": "user", "text": "how is the lab?"}]))
    assert client.sent[0]["include"] == ["reasoning.encrypted_content"]
    assert client.sent[0]["reasoning"] == {"effort": "xhigh"}
    assert first.raw[0] == {"type": "reasoning", "summary": [], "encrypted_content": "gAAAA-sealed",
                            "for_model": "gpt-6.1-sol"}                    # the model Kelvin asked
    history = [{"role": "user", "text": "how is the lab?"},
               {"role": "assistant", "provider": "chatgpt", "model": "gpt-6.1-sol-2026-10-01",   # as the route names it
                "blocks": first.blocks, "raw": first.raw},
               {"role": "tool", "results": [{"id": "call_1", "name": "lab_status", "content": "idle",
                                             "is_error": False}]}]
    asyncio.run(answer(history))
    sent = client.sent[1]["input"]
    assert sent[1] == {"type": "reasoning", "summary": [], "encrypted_content": "gAAAA-sealed"}   # before its call
    assert sent[2]["type"] == "function_call"


def test_a_chatgpt_route_that_refuses_the_reasoning_fields_goes_on_without_them(tmp_path):
    pytest.importorskip("openai")
    import httpx
    import openai
    from test_chatgpt import TOOLS, done, signed_in_provider
    p, client = signed_in_provider(tmp_path, [[done([{"type": "message", "content": [
        {"type": "output_text", "text": "ok"}]}])], [done([{"type": "message", "content": [
            {"type": "output_text", "text": "ok"}]}])]])
    create = client.create

    async def picky(**body):
        if "include" in body or "reasoning" in body:
            client.sent.append(body)
            word = "reasoning.effort" if "reasoning" in body else "include"
            raise openai.BadRequestError("Unsupported parameter: %s" % word, body=None, response=httpx.Response(
                400, request=httpx.Request("POST", "https://chatgpt.example/v1/responses")))
        return await create(**body)
    client.responses = SimpleNamespace(create=picky)

    async def emit(e):
        return None

    async def answer():
        token = EFFORT.set("high")
        try:
            return await p.stream("gpt-6.1-sol", "s", [{"role": "user", "text": "hi"}], TOOLS, emit)
        finally:
            EFFORT.reset(token)
    assert asyncio.run(answer()).blocks == [{"type": "text", "text": "ok"}]
    assert "gpt-6.1-sol" in p.no_effort and "gpt-6.1-sol" in p.no_include
    asyncio.run(answer())
    assert not {"include", "reasoning"} & set(client.sent[-1])   # remembered, for that model


def chatgpt_error(text, param):
    import httpx
    import openai
    # body: the error itself, as the SDK keeps it (its param is what the provider reads)
    return openai.BadRequestError(text, body={"message": text, "param": param}, response=httpx.Response(
        400, request=httpx.Request("POST", "https://chatgpt.example/v1/responses")))


def test_chatgpt_takes_the_deepest_it_can_and_goes_on_without_reasoning_it_cant_read(tmp_path):
    pytest.importorskip("openai")
    from test_chatgpt import TOOLS, done, signed_in_provider
    ok = [done([{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}])]
    p, client = signed_in_provider(tmp_path, [ok, ok, ok])
    create = client.create

    async def picky(**body):
        client.sent.append(body)
        if (body.get("reasoning") or {}).get("effort") == "xhigh":
            raise chatgpt_error("reasoning.effort 'xhigh' is not supported with this model", "reasoning.effort")
        if any(i.get("type") == "reasoning" for i in body["input"]):
            raise chatgpt_error("The encrypted content could not be verified", "input[1].encrypted_content")
        client.sent.pop()
        return await create(**body)
    client.responses = SimpleNamespace(create=picky)

    async def emit(e):
        return None

    async def answer(history, effort):
        token = EFFORT.set(effort)
        try:
            return await p.stream("gpt-6.1-sol", "s", history, TOOLS, emit)
        finally:
            EFFORT.reset(token)
    hi = [{"role": "user", "text": "hi"}]
    asyncio.run(answer(hi, "xhigh"))
    assert client.sent[-1]["reasoning"] == {"effort": "high"} and not p.no_effort   # this request only
    old = [{"role": "user", "text": "hi"},
           {"role": "assistant", "provider": "chatgpt", "model": "gpt-6.1-sol",
            "blocks": [{"type": "text", "text": "x"}],
            "raw": [{"type": "reasoning", "summary": [], "encrypted_content": "from-another-sign-in",
                     "for_model": "gpt-6.1-sol"},
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "x"}]}]},
           {"role": "user", "text": "and now?"}]
    asyncio.run(answer(old, "high"))
    assert not any(i.get("type") == "reasoning" for i in client.sent[-1]["input"]) and not p.no_include   # kept
    from labharness.web.providers.chatgpt_provider import to_input
    assert not any(i.get("type") == "reasoning" for i in to_input(old, "chatgpt", "gpt-5.5"))   # another model's
    again = [i for i in to_input(old, "chatgpt", "gpt-6.1-sol") if i.get("type") == "reasoning"]
    assert again == [{"type": "reasoning", "summary": [], "encrypted_content": "from-another-sign-in"}]   # as it came


def test_a_chatgpt_model_without_encrypted_reasoning_goes_on_without_asking_for_it(tmp_path):
    pytest.importorskip("openai")
    from test_chatgpt import TOOLS, done, signed_in_provider
    ok = [done([{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}])]
    p, client = signed_in_provider(tmp_path, [ok, ok])
    create = client.create

    async def no_reasoning_back(**body):
        if "include" in body and body["model"] == "gpt-5.5-mini":   # said with no parameter named, on a first message
            raise chatgpt_error("Encrypted content is not supported with this model.", None)
        return await create(**body)
    client.responses = SimpleNamespace(create=no_reasoning_back)

    async def emit(e):
        return None
    hi = [{"role": "user", "text": "hi"}]
    assert asyncio.run(p.stream("gpt-5.5-mini", "s", hi, TOOLS, emit)).blocks == [{"type": "text", "text": "ok"}]
    assert p.no_include == {"gpt-5.5-mini"}                                 # that model only
    asyncio.run(p.stream("gpt-6.1-sol", "s", hi, TOOLS, emit))
    assert client.sent[-1]["include"] == ["reasoning.encrypted_content"]     # the others still ask


def test_claude_goes_on_without_thinking_it_refuses_where_it_cant_drop_it():
    pytest.importorskip("anthropic")
    import anthropic
    import httpx
    from test_prompt_cache import TOOLS, FakeClaude, quiet
    from labharness.web.providers import anthropic_provider as ap

    class Strict(FakeClaude):
        def _stream(self, **kwargs):
            self.sent.append(kwargs)
            if any(isinstance(b, dict) and b.get("type") == "thinking" for m in kwargs["messages"]
                   if m["role"] == "assistant" for b in m["content"]):
                raise anthropic.BadRequestError(
                    "messages.1.content.0: Invalid `signature` in `thinking` block. The block is bound to a different "
                    "conversation.", body=None,
                    response=httpx.Response(400, request=httpx.Request("POST", "https://api.example/v1/messages")))
            self.sent.pop()
            return super()._stream(**kwargs)
    provider = ap.AnthropicProvider("test-key")
    provider.binding = False   # a route that refused the drop control
    provider.client = strict = Strict()
    history = [{"role": "user", "text": "hi"},
               {"role": "assistant", "provider": "anthropic", "model": "claude-opus-5-5", "blocks": [], "raw": [
                   {"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": "hello"}]},
               {"role": "user", "text": "and?"}]
    result = asyncio.run(provider.stream("claude-opus-5-5", "rules", history, TOOLS, quiet))
    assert result.stop_reason == "end"
    assert strict.sent[-1]["messages"][1]["content"] == [{"type": "text", "text": "hello"}]   # its words, no thinking
    assert provider.binding is False                                       # not mistaken for the control's refusal
