"""A chat's requests only ever grow at their end (asked for 2026-11-12, after the harness fixes): Claude binds each
thinking block to everything before it (the system prompt, the tools, the earlier turns) and refuses or drops it when
any of that changed, and a model's cache keeps only an unchanged prefix.

- Kelvin's rarer tools are declared to Claude from the chat's first request, waiting for Anthropic's tool search
  (defer_loading), so loading one never changes the tools; other models keep find_tools.
- The system prompt is the chat's as it began (its frame); what changed since (the lab's memory, the instructions,
  the reading list, the skills, the folder, the tools the chat may call) goes with the person's next message: a system
  message after it for Claude, a <context_update> block in it for other models.
- PDFs stay where they came, as pictures do, until the day is summarised.
- GPT with an API key goes through the Responses API, its encrypted reasoning carried from step to step."""
import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")
pytest.importorskip("anthropic")

from simlab import build_virtual_lab  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.gate import ScriptedApprover  # noqa: E402
from labharness.web import agent as agent_mod, compaction  # noqa: E402
from labharness.web.agent import (DEFERRED_NAMES, MAX_DOCUMENTS, MAX_KEPT_DOCUMENTS, NO_FOLDER, NOT_HERE,  # noqa: E402
                                  ChatAgent)
from labharness.web.providers import anthropic_provider as ap  # noqa: E402
from labharness.web.providers.base import EFFORT, update_block, user_text  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from test_folder_files import make_pdf  # noqa: E402
from test_web import Scripted  # noqa: E402

NOTE = "The fridge's 4 K stage needs 40 min to settle after a field ramp."


# ------------------------------------------------------------------ Claude, as Kelvin's provider uses its client

class Said(dict):
    """A content block as the SDK gives it."""

    @property
    def type(self):
        return self["type"]

    def to_dict(self, exclude_none=True):
        return json.loads(json.dumps(self))


class _Answer:
    def __init__(self, model, content, stop):
        self.message = SimpleNamespace(content=[Said(b) for b in content], stop_reason=stop, model=model)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def get_final_message(self):
        return self.message


class Claude:
    """Claude's API as the provider calls it: each request body as it would go over the wire (the beta headers
    apart), and the answers given in turn."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.bodies = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    during = None   # during(i): something that happens while request i is answered (a message written meanwhile)

    def _stream(self, **kwargs):
        self.bodies.append(json.loads(json.dumps({k: v for k, v in kwargs.items() if k != "betas"})))
        if self.during:
            self.during(len(self.bodies) - 1)
        content, stop = self.answers.pop(0)
        return _Answer(kwargs["model"], content, stop)


def thought(n):
    return {"type": "thinking", "thinking": "", "signature": "sig-%d" % n}


def words(text):
    return {"type": "text", "text": text}


def use(i, name, **args):
    return {"type": "tool_use", "id": "toolu_%s" % i, "name": name, "input": args}


def searched(i, query, *found):
    return [{"type": "server_tool_use", "id": "srvtoolu_%s" % i, "name": "tool_search_tool_bm25",
             "input": {"query": query}},
            {"type": "tool_search_tool_result", "tool_use_id": "srvtoolu_%s" % i,
             "content": {"type": "tool_search_tool_search_result",
                         "tool_references": [{"type": "tool_reference", "tool_name": n} for n in found]}}]


def references(value):
    """The tools a request's messages name with a tool_reference (a tool search's results), at any depth."""
    if isinstance(value, dict):
        own = [value["tool_name"]] if value.get("type") == "tool_reference" else []
        return own + [n for v in value.values() for n in references(v)]
    return [n for v in value for n in references(v)] if isinstance(value, list) else []


def resolves(body):
    """Claude refuses a request whose history names a tool it isn't given ("Tool reference ... not found")."""
    declared = {t.get("name") for t in body["tools"]}
    assert set(references(body["messages"])) <= declared, set(references(body["messages"])) - declared


def grows_at_its_end(bodies):
    """Each request the one before it, unchanged, with the new turns after it: the same system prompt and tools
    byte for byte (a tool allowed later may follow them, waiting for the tool search: nothing has named it, so it is
    no part of what came before), and the earlier messages as they were sent."""
    for body in bodies:
        resolves(body)
    for n, (before, after) in enumerate(zip(bodies, bodies[1:])):
        assert json.dumps(after["system"]) == json.dumps(before["system"]), "system changed at request %d" % (n + 2)
        held = before["tools"]
        assert json.dumps(after["tools"][:len(held)]) == json.dumps(held), "tools changed at request %d" % (n + 2)
        assert all(t.get("defer_loading") for t in after["tools"][len(held):])
        held = before["messages"]
        assert json.dumps(after["messages"][:len(held)]) == json.dumps(held), "history changed at request %d" % (n + 2)
        assert len(after["messages"]) > len(held)


def claude_day(tmp_path, answers, texts, between=None, folder=None, model="claude-opus-5-5", settings=None,
               during=None):
    """A chat with Claude over the virtual lab, a turn for each text; between(i, rt, agent, chat_id) runs before turn
    i (from 1); during(i, agent, chat_id) while request i is answered."""
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    store = ChatStore(tmp_path / "chats")
    provider = ap.AnthropicProvider("test-key")
    provider.client = claude = Claude(answers)
    agent = ChatAgent(rt, store, build_server(rt), {"anthropic": provider}, settings=settings)
    key = "anthropic:" + model
    chat = store.create(key)
    if during:
        claude.during = lambda i: during(i, agent, chat["id"])
    if folder:
        chat["folder"] = str(folder)
        store.save(chat)
    events = []

    async def emit(e):
        events.append(e)

    async def go():
        await agent.start()
        try:
            for i, text in enumerate(texts):
                if between:
                    between(i, rt, agent, chat["id"])
                agent.claim(chat["id"], key)
                await agent.run_turn(chat["id"], text, key, provider, emit)
        finally:
            await agent.close()
    asyncio.run(go())
    return claude.bodies, store.load(chat["id"]), events


# ------------------------------------------------------------------ the whole day: a tool load, the lab's memory, PDFs

def test_a_claude_chats_requests_grow_only_at_their_end(tmp_path):
    folder = tmp_path / "Sample A"
    folder.mkdir()
    make_pdf(folder / "manual.pdf", 3)
    answers = [
        ([thought(1)] + searched(1, "watch a reading", "watches") + [use(1, "watches")], "tool_use"),   # a tool load
        ([thought(2), words("Nothing is watched.")], "end_turn"),
        ([thought(3), use(2, "read_file", path="manual.pdf", pages="1-2", figures=True)], "tool_use"),   # a PDF
        ([thought(4), words("It settles in 3 s.")], "end_turn"),
        ([thought(5), use(3, "read_file", path="manual.pdf", pages="3", figures=True)], "tool_use"),     # another
        ([thought(6), words("Page 3 agrees.")], "end_turn")]

    def meanwhile(i, rt, agent, chat_id):
        if i == 1:   # someone keeps a note in the lab's memory between the first and second turns
            assert not rt.keep_lab_note("lesson", NOTE, by="person")[1]
    bodies, chat, events = claude_day(tmp_path, answers, ["is anything watching the fridge?",
                                                          "what does the manual say about settling?", "and page 3?"],
                                      between=meanwhile, folder=folder)
    assert len(bodies) == 6 and not [e for e in events if e["type"] == "error"]
    grows_at_its_end(bodies)

    tools = {t["name"]: t for t in bodies[0]["tools"]}
    assert bodies[0]["tools"][0] == {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}
    assert "find_tools" not in tools                                         # Claude searches; the tools never grow
    assert all(tools[n].get("defer_loading") for n in DEFERRED_NAMES if n in tools)
    assert "watches" in tools and not tools["lab_status"].get("defer_loading")
    assert "# More tools" in bodies[0]["system"][0]["text"]

    first = bodies[1]["messages"][1]["content"]                              # the search, kept in the turn it was
    assert [b["type"] for b in first] == ["thinking", "server_tool_use", "tool_search_tool_result", "tool_use"]

    told = bodies[2]["messages"][-1]                                          # the lab's memory: said after the
    assert told["role"] == "system" and NOTE in told["content"]               # person's message, as a system message
    assert told["content"].startswith(agent_mod.UPDATE) and "# The lab's memory" in told["content"]
    asked = bodies[2]["messages"][-2]
    assert asked["role"] == "user" and NOTE not in json.dumps(asked) and "manual say" in asked["content"][-1]["text"]
    assert NOTE not in bodies[2]["system"][0]["text"]                         # the prompt as the chat began
    kept = [m for m in chat["messages"] if m["role"] == "user"][1]
    assert NOTE in kept["update"]                                             # kept with the message: it replays

    def pdfs(body):
        return [b for m in body["messages"] if m["role"] == "user" and isinstance(m["content"], list)
                for b in m["content"] if b["type"] == "document"]
    assert len(pdfs(bodies[3])) == 1 and len(pdfs(bodies[5])) == 2           # the first stays where it came
    assert pdfs(bodies[5])[0] == pdfs(bodies[3])[0]
    assert base64.b64decode(pdfs(bodies[5])[1]["source"]["data"])[:5] == b"%PDF-"
    assert "no longer" not in json.dumps(bodies[5]["messages"])
    assert ChatAgent.prompt(chat) == bodies[0]["system"][0]["text"] and chat["tools"] == ["watches"]
    kept = json.dumps(chat["frame"])                                          # Kelvin's own instructions by their
    assert "You are Kelvin, the lab assistant" not in kept and len(kept) < 30000   # fingerprint, not in every chat


def test_a_claude_chat_says_what_changed_with_the_next_message_whatever_changed(tmp_path):
    settings = Settings(tmp_path / "settings.json")
    answers = [([thought(i), words("ok")], "end_turn") for i in range(4)]
    folder = tmp_path / "Sample B"
    folder.mkdir()

    def meanwhile(i, rt, agent, chat_id):
        if i == 1:
            settings.update({"answer_words": "100", "instructions": "Use SI units."})
        elif i == 2:
            chat = agent.store.load(chat_id)
            chat["folder"] = str(folder)   # the person picks a folder: its tools join, waiting for the search
            agent.store.save(chat)
        elif i == 3:
            settings.update({"instructions": ""})
    bodies, chat, events = claude_day(tmp_path, answers, ["one", "two", "three", "four"], between=meanwhile,
                                      settings=settings)
    grows_at_its_end(bodies)
    said = [m["content"] for b in bodies[1:] for m in b["messages"][-1:] if m["role"] == "system"]
    assert len(said) == 3
    assert "under 100 words" in said[0] and "# The user's instructions for every conversation\nUse SI units." in said[0]
    assert "# This chat's folder" in said[1] and str(folder) in said[1]
    assert agent_mod.TOOLS_ON % "list_folder, read_file, write_file" in said[1]
    assert agent_mod.UPDATE_GONE % "The user's instructions for every conversation" in said[2]
    tools = {t["name"]: t for t in bodies[-1]["tools"]}
    assert tools["read_file"]["defer_loading"] and "read_file" not in {t["name"] for t in bodies[0]["tools"]}
    briefs = [b for m in bodies[-1]["messages"] if m["role"] == "user" for b in m["content"]
              if b["type"] == "text" and "(Answer in at most" in b["text"]]
    assert "at most 200 words" in briefs[0]["text"] and "at most 100 words" in briefs[-1]["text"]   # each its own


def test_a_tool_taken_away_stays_declared_and_is_refused(tmp_path):
    folder = tmp_path / "Sample C"
    folder.mkdir()
    (folder / "notes.txt").write_text("the gate leaks above 5 V", encoding="utf-8")
    answers = [([thought(1), words("ok")], "end_turn"),
               ([thought(2), use(1, "read_file", path="notes.txt")], "tool_use"),
               ([thought(3), words("No folder now.")], "end_turn")]

    def meanwhile(i, rt, agent, chat_id):
        if i == 1:   # the person takes the chat's folder away
            chat = agent.store.load(chat_id)
            del chat["folder"]
            agent.store.save(chat)
    bodies, chat, events = claude_day(tmp_path, answers, ["hi", "read the notes"], between=meanwhile, folder=folder)
    grows_at_its_end(bodies)
    assert "read_file" in {t["name"] for t in bodies[-1]["tools"]}           # still declared: what came before holds
    assert agent_mod.TOOLS_OFF % "list_folder, read_file, write_file" in bodies[1]["messages"][-1]["content"]
    refused = next(e for e in events if e["type"] == "tool_end")
    assert refused["is_error"] and refused["content"] == NO_FOLDER


def test_a_tool_found_then_taken_away_is_not_named_after_kelvin_is_updated(tmp_path, monkeypatch):
    """The frame written again (a Kelvin with new instructions) declares only what the chat may use now, so the turns
    before it go without their tool searches, as without their thinking: nothing names a tool Claude isn't given."""
    folder = tmp_path / "Sample E"
    folder.mkdir()
    (folder / "notes.txt").write_text("the gate leaks above 5 V", encoding="utf-8")
    answers = [([thought(1), words("ok")], "end_turn"),
               ([thought(2)] + searched(1, "read a file", "read_file") + [use(1, "read_file", path="notes.txt")],
                "tool_use"),
               ([thought(3), words("It leaks above 5 V.")], "end_turn"),
               ([thought(4), words("ok")], "end_turn")]

    def meanwhile(i, rt, agent, chat_id):
        chat = agent.store.load(chat_id)
        if i == 1:
            chat["folder"] = str(folder)   # read_file joins, waiting for the search, and Claude finds it
        elif i == 2:
            del chat["folder"]             # then it goes, and Kelvin is updated
            monkeypatch.setattr(agent_mod, "SYSTEM", agent_mod.SYSTEM + "\nA new rule.")
        agent.store.save(chat)
    bodies, chat, events = claude_day(tmp_path, answers, ["hi", "read the notes", "and now?"], between=meanwhile)
    grows_at_its_end(bodies[:3])
    last = bodies[3]
    resolves(last)
    assert "A new rule." in last["system"][0]["text"]
    assert {t["name"]: t for t in last["tools"]}["read_file"]["defer_loading"]   # its call is in the conversation:
    assert "read_file" in chat["frame"]["off"]                                   # declared still, and refused
    sent = [b for m in last["messages"] if m["role"] == "assistant" for b in m["content"]]
    assert not [b for b in sent if b["type"] in ("thinking", "server_tool_use", "tool_search_tool_result")]
    assert use(1, "read_file", path="notes.txt") in sent                     # what it did stays


def test_a_paused_answer_resumes_before_anything_written_meanwhile(tmp_path):
    search = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "tool_search_tool_bm25",
              "input": {"query": "watch a reading"}}
    found = searched(1, "watch a reading", "watches")[1]
    answers = [([thought(1), search], "pause_turn"), ([found, words("Nothing is watched.")], "end_turn")]

    def meanwhile(i, agent, chat_id):
        if i == 0:   # the person writes while Claude's search is paused
            assert agent.steer(chat_id, "also the fridge")
    bodies, chat, events = claude_day(tmp_path, answers, ["is anything watched?"], during=meanwhile)
    grows_at_its_end(bodies)
    assert [m["role"] for m in bodies[1]["messages"]] == ["user", "assistant"]   # resumed as it paused: nothing
    assert bodies[1]["messages"][-1]["content"][-1] == search                    # between it and its continuation
    assert not [m for m in chat["messages"] if m.get("steer")]                   # the portal sends it next


def test_a_paused_turns_search_waits_for_its_continuation_and_goes_when_something_else_follows():
    search = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "tool_search_tool_bm25", "input": {"query": "x"}}
    hi = {"role": "user", "text": "hi"}
    paused = {"role": "assistant", "provider": "anthropic", "raw": [thought(1), search], "blocks": []}
    resumed = {"role": "assistant", "provider": "anthropic", "blocks": [],
               "raw": [searched(1, "x", "watches")[1], words("ok")]}
    assert ap.to_messages([hi, paused], "anthropic", "claude-opus-5-5")[-1]["content"] == [thought(1), search]
    assert ap.to_messages([hi, paused, resumed], "anthropic", "claude-opus-5-5")[1]["content"] == [thought(1), search]
    stopped = ap.to_messages([hi, paused, {"role": "event", "text": "x"}, {"role": "user", "text": "next"}],
                             "anthropic", "claude-opus-5-5")                  # an answer stopped while paused
    assert stopped[1]["content"] == [thought(1)] and stopped[-1]["role"] == "user"


def test_a_chat_that_moves_to_claude_keeps_the_tools_its_history_called_declared(tmp_path):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    store = ChatStore(tmp_path / "chats")
    claude_p = ap.AnthropicProvider("test-key")
    claude_p.client = claude = Claude([([thought(1), use(1, "find_tools", query="watch a reading")], "tool_use"),
                                       ([thought(2), words("ok")], "end_turn")])
    gpt = Scripted([[{"type": "tool_call", "id": "f1", "name": "find_tools", "input": {"query": "watches"}}],
                    [{"type": "tool_call", "id": "w1", "name": "watches", "input": {}}],
                    [{"type": "text", "text": "None."}]])
    agent = ChatAgent(rt, store, build_server(rt), {"anthropic": claude_p, "scripted": gpt})
    chat = store.create("scripted:m1")
    events = []

    async def emit(e):
        events.append(e)

    async def go():
        await agent.start()
        try:
            agent.claim(chat["id"], "scripted:m1")   # another model loads a rarer tool the old way...
            await agent.run_turn(chat["id"], "anything watched?", "scripted:m1", gpt, emit)
            agent.claim(chat["id"], "anthropic:claude-opus-5-5")   # ...then the chat moves to Claude
            await agent.run_turn(chat["id"], "and now?", "anthropic:claude-opus-5-5", claude_p, emit)
        finally:
            await agent.close()
    asyncio.run(go())
    grows_at_its_end(claude.bodies)
    tools = {t["name"]: t for t in claude.bodies[0]["tools"]}
    assert tools["find_tools"]["defer_loading"] and tools["watches"]["defer_loading"]   # its calls name declared tools
    assert "update" not in [m for m in store.load(chat["id"])["messages"] if m["role"] == "user"][-1]   # silently
    told = [e for e in events if e["type"] == "tool_end" and e["name"] == "find_tools"][-1]
    assert told["content"].startswith(agent_mod.FOUND_FOR_SEARCH)          # Claude loads them with its search


def test_operate_computer_never_runs_where_the_chat_keeps_it_out():
    chat = {"frame": {"tools": ["lab_status"]}}
    assert ChatAgent._not_here(chat, {"allowed": {"lab_status"}}, "operate_computer").startswith(
        "not available in this chat")                                       # declared to the model or not
    assert ChatAgent._not_here(chat, {"allowed": {"operate_computer"}}, "operate_computer") == ""
    assert ChatAgent._not_here(chat, {"allowed": {"lab_status"}}, "web_search") == ""   # never the chat's: its own
    assert ChatAgent._not_here(chat, {}, "operate_computer") == ""                     # handling says why


def test_the_hub_gets_messages_without_kelvins_updates(tmp_path):
    from labharness.hub.client import collect
    store = ChatStore(tmp_path / "chats")
    chat = store.create("x:y")
    chat["messages"] = [{"role": "user", "text": "hi", "update": "# The user's instructions\nmine"},
                        {"role": "assistant", "blocks": [words("ok")]}]
    store.save(chat)
    shared = collect(store)["chats"][0]["messages"]
    assert shared == [{"role": "user", "text": "hi"}, {"role": "assistant", "blocks": [words("ok")]}]
    assert store.load(chat["id"])["messages"][0]["update"]                   # the chat itself keeps it


def test_other_models_get_the_change_in_the_message_and_keep_find_tools(tmp_path):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    store = ChatStore(tmp_path / "chats")
    provider = Scripted([[{"type": "tool_call", "id": "f1", "name": "find_tools", "input": {"query": "watches"}}],
                         [{"type": "text", "text": "ok"}], [{"type": "text", "text": "ok"}]])
    agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
    chat = store.create("scripted:m1")

    async def emit(e):
        pass

    async def go():
        await agent.start()
        try:
            for i, text in enumerate(["first", "second"]):
                if i == 1:
                    rt.keep_lab_note("lesson", NOTE, by="person")
                agent.claim(chat["id"], "scripted:m1")
                await agent.run_turn(chat["id"], text, "scripted:m1", provider, emit)
        finally:
            await agent.close()
    asyncio.run(go())
    assert "find_tools" in provider.tool_names[0] and "watches" in provider.tool_names[1]   # loaded the old way
    assert provider.systems[0] == provider.systems[1] == provider.systems[2] and NOTE not in provider.systems[2]
    first, second = provider.sent[-1]
    assert second.split("\n", 1)[1].startswith("<context_update>\n" + agent_mod.UPDATE) and NOTE in second
    assert NOTE not in first and "</context_update>\n\nsecond\n\n(Answer in" in second   # before the person's words


# ------------------------------------------------------------------ how Claude gets an update

def test_an_update_goes_to_claude_as_a_system_message_where_it_takes_one():
    history = [{"role": "user", "text": "hi"},
               {"role": "assistant", "provider": "anthropic", "raw": [words("hello")], "blocks": []},
               {"role": "user", "text": "and now?", "update": "# The lab's memory\n- a note"}]
    sent = ap.to_messages(history, "anthropic", "claude-opus-5-5")
    assert [m["role"] for m in sent] == ["user", "assistant", "user", "system"]
    assert sent[-1] == {"role": "system", "content": "# The lab's memory\n- a note"}
    assert sent[-2]["content"] == [{"type": "text", "text": "and now?"}]
    ongoing = history + [{"role": "assistant", "provider": "anthropic", "raw": [use(1, "lab_status")], "blocks": []},
                         {"role": "tool", "results": [{"id": "toolu_1", "name": "lab_status", "content": "idle",
                                                       "is_error": False}]},
                         {"role": "user", "text": "also the fridge", "steer": True}]
    later = ap.to_messages(ongoing, "anthropic", "claude-opus-5-5")
    assert later[:4] == sent and [m["role"] for m in later[4:]] == ["assistant", "user"]   # it stays where it was
    assert later[-1]["content"][-1] == {"type": "text", "text": "also the fridge"}   # a steer joins the results
    old = ap.to_messages(history, "anthropic", "claude-sonnet-5")             # no system messages there
    assert [m["role"] for m in old] == ["user", "assistant", "user"]
    assert old[-1]["content"][-1]["text"] == update_block("# The lab's memory\n- a note") + "\n\nand now?"
    assert ap.system_messages("claude-sonnet-5-5") and ap.system_messages("claude-haiku-5-5") and \
        ap.system_messages("claude-fable-5-1") and ap.system_messages("claude-opus-4-8")
    assert not ap.system_messages("claude-sonnet-5") and not ap.system_messages("claude-opus-4-7") and \
        not ap.system_messages("gpt-6")
    assert ap.searches_tools("claude-haiku-4-5-20251001") and not ap.searches_tools("claude-opus-4-1")


def test_where_a_system_message_is_refused_the_update_goes_without_the_fallback_then_in_the_words():
    import anthropic
    import httpx

    class Strict(Claude):
        """A route that refuses a system message mid-conversation: with a refusal fallback (Sonnet 5.5's goes to
        Sonnet 5, which takes none), or at all (a gateway that doesn't know the role)."""

        def __init__(self, answers, at_all):
            super().__init__(answers)
            self.at_all = at_all

        def _stream(self, **kwargs):
            if any(m["role"] == "system" for m in kwargs["messages"]) and ("fallbacks" in kwargs or self.at_all):
                self.bodies.append(json.loads(json.dumps({k: v for k, v in kwargs.items() if k != "betas"})))
                raise anthropic.BadRequestError("messages.3: role 'system' is not supported on this model", body=None,
                                                response=httpx.Response(400, request=httpx.Request(
                                                    "POST", "https://api.example/v1/messages")))
            return super()._stream(**kwargs)
    history = [{"role": "user", "text": "hi"},
               {"role": "assistant", "provider": "anthropic", "raw": [thought(1), words("hello")], "blocks": []},
               {"role": "user", "text": "and now?", "update": "# The lab's memory\n- a note"}]

    async def quiet(e):
        pass
    provider = ap.AnthropicProvider("test-key")
    provider.client = claude = Strict([([words("ok")], "end_turn")] * 2, at_all=False)
    assert asyncio.run(provider.stream("claude-sonnet-5-5", "rules", history, [], quiet)).blocks == [words("ok")]
    assert provider.no_fallback == {"claude-sonnet-5-5"} and not provider.inline
    assert "fallbacks" not in claude.bodies[-1] and claude.bodies[-1]["messages"][-1]["role"] == "system"
    asyncio.run(provider.stream("claude-sonnet-5-5", "rules", history, [], quiet))
    assert len(claude.bodies) == 3 and "fallbacks" not in claude.bodies[-1]   # remembered: refused once only
    gateway = ap.AnthropicProvider("test-key")
    gateway.client = strict = Strict([([words("ok")], "end_turn")] * 2, at_all=True)
    asyncio.run(gateway.stream("claude-opus-5-5", "rules", history, [], quiet))
    assert gateway.inline == {"claude-opus-5-5"} and not gateway.no_fallback   # the fallback wasn't it: kept
    inline = strict.bodies[-1]["messages"]
    assert [m["role"] for m in inline] == ["user", "assistant", "user"]       # in the person's words, from now on
    assert inline[-1]["content"][-1]["text"].startswith("<context_update>") and strict.bodies[-1]["fallbacks"]
    asyncio.run(gateway.stream("claude-opus-5-5", "rules", history, [], quiet))
    assert strict.bodies[-1]["messages"] == inline


def test_a_cut_turns_unanswered_search_is_not_sent_again():
    raw = [words("Let me look."), {"type": "server_tool_use", "id": "srvtoolu_1", "name": "tool_search_tool_bm25",
                                   "input": {"query": "x"}}]
    assert ap.without_calls(raw) == [words("Let me look.")]
    whole = [words("Let me look.")] + searched(2, "watch", "watches") + [use(1, "watches")]
    assert ap.without_calls(whole) == whole[:3]                               # a search with its result stays


# ------------------------------------------------------------------ boundaries: a summary, a new Kelvin

def test_a_summary_writes_the_frame_again_and_the_kept_turns_lose_their_updates(tmp_path):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    store = ChatStore(tmp_path / "chats")
    agent = ChatAgent(rt, store, build_server(rt), {})
    chat = {"id": "c1", "messages": [{"role": "user", "text": "zero"},
                                     {"role": "assistant", "raw": [thought(0), words("ok")], "blocks": []}]}

    async def go():
        await agent.start()
        try:
            context = agent.context(store.create("x:y"))
            first = {"role": "user", "text": "one"}
            agent._frame(chat, context, first)   # a chat from before frames: its earlier thinking was bound to
            assert chat["frame"]["since"] == 2 and "update" not in first   # another prompt, so it is set aside
            assert compaction.view(chat)[1]["raw"] == [words("ok")]
            chat["messages"] += [first, {"role": "assistant", "raw": [thought(1), words("ok")], "blocks": []}]
            rt.keep_lab_note("lesson", NOTE, by="person")
            later = agent.context(store.create("x:y"))
            second = {"role": "user", "text": "two"}
            agent._frame(chat, later, second)
            chat["messages"] += [second, {"role": "assistant", "raw": [thought(2), words("ok")], "blocks": []}]
            assert NOTE in second["update"] and NOTE not in ChatAgent.prompt(chat)
            assert compaction.view(chat)[4]["update"] == second["update"]   # said with its message, until...
            chat["compact"] = {"upto": 2, "made": 6, "summary": "the day"}   # ...a summary that kept both turns
            agent._reframe(chat, later)
            assert NOTE in ChatAgent.prompt(chat) and later["system"] == ChatAgent.prompt(chat)
            seen = compaction.view(chat)
            assert seen[0]["handoff"] == "the day" and seen[2]["text"] == "two"
            assert "update" not in seen[2] and seen[1]["raw"] == [words("ok")]   # the new prompt holds it
            assert chat["messages"][4]["update"] == second["update"]          # the chat itself keeps it
        finally:
            await agent.close()
    asyncio.run(go())


def test_a_kelvin_with_new_instructions_writes_the_frame_again_and_sets_old_thinking_aside(tmp_path, monkeypatch):
    rt = LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([]), tmp_path / "data")
    store = ChatStore(tmp_path / "chats")
    agent = ChatAgent(rt, store, build_server(rt), {})

    async def go():
        await agent.start()
        try:
            chat = store.create("x:y")
            message = {"role": "user", "text": "one"}
            agent._frame(chat, agent.context(chat), message)
            chat["messages"] += [message, {"role": "assistant", "raw": [thought(1), words("ok")], "blocks": []}]
            assert compaction.view(chat) is chat["messages"]
            monkeypatch.setattr(agent_mod, "SYSTEM", agent_mod.SYSTEM + "\nA new rule.")   # Kelvin updated
            again = {"role": "user", "text": "two"}
            agent._frame(chat, agent.context(chat), again)
            assert "A new rule." in ChatAgent.prompt(chat) and chat["frame"]["since"] == 2 and "update" not in again
            chat["messages"] += [again]
            seen = compaction.view(chat)
            assert seen[1]["raw"] == [words("ok")] and chat["messages"][1]["raw"][0] == thought(1)   # kept in the chat
        finally:
            await agent.close()
    asyncio.run(go())


# ------------------------------------------------------------------ PDFs where they came

def test_pdfs_stay_where_they_came_for_claude_and_slide_for_the_others(tmp_path):
    agent = ChatAgent.__new__(ChatAgent)
    agent._document = lambda chat_id, item: {"mime": "application/pdf", "data": "JVBERi0=", "name": item["file"]}
    history = [{"role": "user", "text": "read them"}] + [
        {"role": "tool", "results": [{"id": "r%d" % i, "name": "read_file", "content": "pages %d" % i,
                                      "is_error": False, "documents": [{"file": "p%d.pdf" % i}]}]}
        for i in range(MAX_KEPT_DOCUMENTS)]

    class ReadsPdfs(Scripted):
        sees_pdfs = True

    class Keeps(ReadsPdfs):
        keeps_history = True
    kept = agent._pictures({"id": "c"}, history, Keeps([]), "m1")
    assert all(m["results"][0].get("documents") for m in kept[1:])          # every one, none taken out
    other = agent._pictures({"id": "c"}, history, ReadsPdfs([]), "m1")
    shown = [bool(m["results"][0].get("documents")) for m in other[1:]]
    assert shown == [False] * (MAX_KEPT_DOCUMENTS - MAX_DOCUMENTS) + [True] * MAX_DOCUMENTS
    assert len(agent_mod.documents_in(history)) == MAX_KEPT_DOCUMENTS
    pages = compaction.size("", [], history, 0, 40) - compaction.size("", [], history, 0)
    assert pages == 40 * compaction.PICTURE_TOKENS                           # a PDF's pages count, as pictures do


def test_more_pdfs_than_claude_keeps_summarise_the_earlier_turns(tmp_path, monkeypatch):
    folder = tmp_path / "Sample D"
    folder.mkdir()
    make_pdf(folder / "manual.pdf", 3)
    monkeypatch.setattr(agent_mod, "MAX_KEPT_DOCUMENTS", 2)
    answers = []
    for page in (1, 2, 3):
        answers += [([thought(2 * page), use(page, "read_file", path="manual.pdf", pages=str(page), figures=True)],
                     "tool_use"),
                    ([thought(2 * page + 1), words("Page %d." % page)], "end_turn")]
    answers.insert(5, ([words("## Request\nthe manual, page by page")], "end_turn"))   # the summary, at the third PDF
    bodies, chat, events = claude_day(tmp_path, answers, ["page 1?", "page 2?", "page 3?"], folder=folder)
    assert chat["compact"]["summary"] == "## Request\nthe manual, page by page"
    assert any(e["type"] == "note" and "summarised" in e["text"] for e in events)
    grows_at_its_end(bodies[:5])                                              # until the summary...
    assert bodies[5]["system"] == compaction.HANDOFF and bodies[5]["tools"] == []   # the summary's own request
    after = bodies[6]
    assert after["system"][0]["text"] == ChatAgent.prompt(chat)             # ...then under its new frame
    pdfs = [b for m in after["messages"] if m["role"] == "user" for b in m["content"] if b["type"] == "document"]
    assert len(pdfs) == 2                                                     # the kept turns' PDFs, where they came
    assert not [b for m in after["messages"] if m["role"] == "assistant" for b in m["content"]
                if b["type"] == "thinking"]                                   # their thinking no longer bound
    assert "no longer" not in json.dumps(after["messages"])


# ------------------------------------------------------------------ GPT with an API key: the Responses API

class Responses:
    """OpenAI's Responses API as the provider calls it: each request, and the answers given in turn."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.sent = []
        self.responses = SimpleNamespace(create=self._create)

    async def _create(self, **body):
        self.sent.append(json.loads(json.dumps(body)))
        output = self.outputs.pop(0)

        async def events():
            yield SimpleNamespace(type="response.completed", response=SimpleNamespace(model=body["model"],
                                                                                      output=output))
        return events()


def test_gpt_with_an_api_key_carries_its_reasoning_through_the_responses_api():
    pytest.importorskip("openai")
    from labharness.web.providers.openai_provider import OpenAIProvider
    thinking = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAAA-sealed"}
    asking = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "lab_status", "arguments": "{}",
              "status": "completed"}
    gpt = OpenAIProvider("openai", "GPT", "sk-test")
    gpt.client = api = Responses([[thinking, asking],
                                  [{"type": "message", "content": [{"type": "output_text", "text": "Idle."}]}]])
    tools = [{"name": "lab_status", "description": "The lab now.", "schema": {"type": "object", "properties": {}}}]

    async def emit(e):
        pass

    async def answer(history):
        token = EFFORT.set("xhigh")
        try:
            return await gpt.stream("gpt-6.1", "You are Kelvin.", history, tools, emit)
        finally:
            EFFORT.reset(token)
    doc = {"mime": "application/pdf", "data": "JVBERi0=", "name": "manual.pdf"}
    asked = [{"role": "user", "text": "how is the lab?", "documents": [doc]}]
    first = asyncio.run(answer(asked))
    sent = api.sent[0]
    assert sent["store"] is False and sent["include"] == ["reasoning.encrypted_content"]
    assert sent["reasoning"] == {"effort": "xhigh"} and sent["instructions"] == "You are Kelvin."
    assert sent["tools"] == [{"type": "function", "name": "lab_status", "description": "The lab now.",
                              "parameters": {"type": "object", "properties": {}}, "strict": False}]   # as given
    assert sent["input"][0]["content"][1] == {"type": "input_file", "filename": "manual.pdf",
                                              "file_data": "data:application/pdf;base64,JVBERi0="}
    assert first.stop_reason == "tool_calls" and first.tool_calls[0]["name"] == "lab_status"
    assert first.raw[0] == {"type": "reasoning", "summary": [], "encrypted_content": "gAAAA-sealed",
                            "for_model": "gpt-6.1"}
    history = asked + [{"role": "assistant", "provider": "openai", "model": "gpt-6.1", "blocks": first.blocks,
                        "raw": first.raw},
                       {"role": "tool", "results": [{"id": "call_1", "name": "lab_status", "content": "idle",
                                                     "is_error": False}]}]
    second = asyncio.run(answer(history))
    replay = api.sent[1]["input"]
    assert replay[1] == {"type": "reasoning", "summary": [], "encrypted_content": "gAAAA-sealed"}   # before its call
    assert replay[2] == {"type": "function_call", "call_id": "call_1", "name": "lab_status", "arguments": "{}"}
    assert replay[3] == {"type": "function_call_output", "call_id": "call_1", "output": "idle"}
    assert second.blocks == [{"type": "text", "text": "Idle."}] and second.stop_reason == "end"
    from labharness.web.providers import responses
    assert not [i for i in responses.to_input(history, "openai", "gpt-5.5") if i.get("type") == "reasoning"]


def test_other_openai_compatible_services_keep_chat_completions():
    pytest.importorskip("openai")
    from test_prompt_cache import FakeChat
    from labharness.web.providers.openai_provider import OpenAIProvider
    deepseek = OpenAIProvider("deepseek", "DeepSeek", "k", base_url="https://api.deepseek.com/v1")
    deepseek.client = chat = FakeChat()
    hub = OpenAIProvider("hub-openai", "GPT · Kelvin hub", "token", base_url="https://hub.example/llm/openai/v1",
                         api="openai")
    hub.client = api = Responses([[{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]])

    async def emit(e):
        pass
    hi = [{"role": "user", "text": "hi"}]
    asyncio.run(deepseek.stream("deepseek-v4-pro", "rules", hi, [], emit))
    asyncio.run(hub.stream("gpt-6.1", "rules", hi, [], emit))
    assert chat.sent[-1]["messages"][0] == {"role": "system", "content": "rules"}   # Chat Completions, as before
    assert api.sent[-1]["store"] is False and "tools" not in api.sent[-1]   # OpenAI's own, through the hub too


def test_a_gpt_model_that_refuses_the_reasoning_fields_goes_on_without_them():
    pytest.importorskip("openai")
    import httpx
    import openai
    from labharness.web.providers.openai_provider import OpenAIProvider
    gpt = OpenAIProvider("openai", "GPT", "sk-test")
    gpt.client = api = Responses([[{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]] * 2)
    create = api._create

    async def picky(**body):
        if "include" in body or "reasoning" in body:
            api.sent.append(body)
            word = "reasoning.effort" if "reasoning" in body else "include"
            raise openai.BadRequestError("Unsupported parameter: '%s' is not supported with this model." % word,
                                         body={"message": "x", "param": word}, response=httpx.Response(
                                             400, request=httpx.Request("POST", "https://api.openai.com/v1/responses")))
        return await create(**body)
    api.responses = SimpleNamespace(create=picky)

    async def emit(e):
        pass

    async def answer():
        token = EFFORT.set("high")
        try:
            return await gpt.stream("gpt-4.1", "s", [{"role": "user", "text": "hi"}], [], emit)
        finally:
            EFFORT.reset(token)
    assert asyncio.run(answer()).blocks == [{"type": "text", "text": "ok"}]
    assert gpt.no_effort == {"gpt-4.1"} and gpt.no_include == {"gpt-4.1"}
    asyncio.run(answer())
    assert not {"include", "reasoning"} & set(api.sent[-1])                  # remembered, for that model


def test_a_gpt_refusal_is_said_and_its_calls_never_sent_again():
    pytest.importorskip("openai")
    from labharness.web.providers.openai_provider import OpenAIProvider
    gpt = OpenAIProvider("openai", "GPT", "sk-test")
    declined = {"type": "message", "content": [{"type": "refusal", "refusal": "I can't help with that."}]}
    gpt.client = Responses([[{"type": "reasoning", "summary": [], "encrypted_content": "e"}, declined]])

    async def emit(e):
        pass
    result = asyncio.run(gpt.stream("gpt-6.1", "s", [{"role": "user", "text": "hi"}], [], emit))
    assert result.stop_reason == "refusal" and result.blocks == [{"type": "text", "text": "I can't help with that."}]
    assert [i["type"] for i in result.raw] == ["message"]


def test_user_text_carries_the_update_unless_it_goes_apart():
    message = {"role": "user", "text": "hi", "update": "# Short answers\nUnder 100 words.", "sent": "<sent>x</sent>"}
    assert user_text(message) == "<sent>x</sent>\n" + update_block("# Short answers\nUnder 100 words.") + "\n\nhi"
    assert user_text(message, updates=False) == "<sent>x</sent>\nhi"
    assert user_text({"role": "user", "text": "", "update": "u"}) == update_block("u")
    assert NOT_HERE.startswith("not available in this chat now")
