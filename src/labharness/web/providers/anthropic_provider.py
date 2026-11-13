"""Claude, through the official Anthropic SDK (streaming Messages API with client tools).

A chat's requests only ever grow at their end, as Claude binds each thinking block to everything before it (the
system prompt, the tools, the earlier turns) and caches the same prefix: Kelvin's rarer tools are declared from the
chat's first request with defer_loading, and Claude finds them with Anthropic's tool search (its results stay in the
turn that searched, so a tool it loaded stays loaded); a change to the chat's setup goes as a system message after the
person's message, on the models that take one (agent.py keeps the prompt and the tools as the chat first had them)."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import anthropic

from .base import DOCUMENTS_NOTE, EFFORT, EFFORTS, Emit, ModelInfo, Provider, TurnResult, safe_id, user_text

MODELS = [
    ("claude-opus-5-5", "Claude Opus 5.5"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
    ("claude-haiku-5-5", "Claude Haiku 5.5"),
    ("claude-fable-5-1", "Claude Fable 5.1"),
]
# Server-side refusal fallback ("default" routing) for the models that support it.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_JSON_RETRIES = 2
# How hard Claude thinks when the turn doesn't say (base.EFFORT; Settings → Models → Thinking). "high": a lab's work is
# multi-step tool use with physics in it, where Claude does better thinking more; Opus 5.5's own default is medium.
DEFAULT_EFFORT = "high"
# A thinking block is valid only in the conversation that wrote it: when an earlier part of the conversation changed
# (a summary of the day, a tool loaded mid-chat), Claude 5.5's newer accounts and Fable 5.2's every account refuse the
# request (400) rather than read a block it can't vouch for. Asked to, the API drops those blocks instead, and the turn
# goes on without that earlier reasoning (Kelvin keeps its history append-only, so this is rare).
BINDING_BETA = "thinking-binding-controls-2026-08-01"
DROP_UNBOUND = {"prefix_mismatch_behavior": "drop_block"}
# Every request sends the whole prompt again (the tools, the system prompt, the conversation), and the part sent before
# is read from Claude's cache at a tenth of the price or less. A cache entry lives 5 minutes unless asked for an
# hour, and in a lab the person comes back after 5 to 60 minutes: then the 5-minute one is gone, and the whole prompt is
# written again at 1.25 times the price. An hour's entry costs 2 times once and is read for the rest of the hour, so a
# conversation with tools keeps its prompt for an hour (Claude Code does too); a one-off request (a summary, a second
# opinion) keeps the 5-minute default.
LONG_CACHE = {"type": "ephemeral", "ttl": "1h"}
SHORT_CACHE = {"type": "ephemeral"}
# Anthropic's tool search (a server tool, no beta): the tools marked defer_loading stay out of Claude's context until a
# search by words finds them, so the chat's tools are declared once, whole, and never change (agent.py, _declared)
TOOL_SEARCH = {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}
_CLAUDE = re.compile(r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?!\d)")


def _claude(model: str) -> Optional[Tuple[str, Tuple[int, int]]]:
    """A Claude model's family and version: ("opus", (5, 5)) for claude-opus-5-5; None for anything else."""
    m = _CLAUDE.search(model or "")
    return (m.group(1), (int(m.group(2)), int(m.group(3) or 0))) if m else None


def system_messages(model: str) -> bool:
    """Does this model take a system message mid-conversation (no beta)? Claude Opus 4.8 and later, Sonnet and Haiku
    5.5 and later, every Fable and Mythos; not Sonnet 5, nor older ones."""
    found = _claude(model)
    if found is None:
        return False
    family, version = found
    return family in ("fable", "mythos") or (family == "opus" and version >= (4, 8)) or \
        (family in ("sonnet", "haiku") and version >= (5, 5))


def searches_tools(model: str) -> bool:
    """Does this model take Anthropic's tool search (deferred tools)? Claude 4.5 and later, every Fable and Mythos."""
    found = _claude(model)
    return found is not None and (found[0] in ("fable", "mythos") or found[1] >= (4, 5))


def tool_list(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Kelvin's tools as Claude takes them: a tool marked "defer" (agent.py, _declared) waits for the tool search,
    which is then offered first."""
    out = [dict(TOOL_SEARCH)] if any(t.get("defer") for t in tools) else []
    for t in tools:
        one = {"name": t["name"], "description": t["description"], "input_schema": t["schema"],
               "eager_input_streaming": True}
        if t.get("defer"):
            one["defer_loading"] = True
        out.append(one)
    return out


def cached(system: str, tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    """How a request asks Claude to cache it: with tools, the system prompt (and the tools before it) kept an hour by a
    mark of its own, which every chat shares and which outlives a summary of the turns, and the conversation kept an
    hour up to its latest message; without, the 5-minute default on the whole request."""
    if not tools or not system:
        return {"system": system, "cache_control": SHORT_CACHE}
    return {"system": [{"type": "text", "text": system, "cache_control": LONG_CACHE}], "cache_control": LONG_CACHE}


def sanitize(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Make a returned turn safe to echo back. Before the last fallback boundary only
    text survives (thinking and tool_use there belong to the model that declined);
    the fallback marker itself is dropped."""
    last = max((i for i, b in enumerate(raw) if b.get("type") == "fallback"), default=-1)
    return [b for i, b in enumerate(raw)
            if b.get("type") != "fallback" and (i > last or b.get("type") == "text")]


def without_thinking(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The messages with no thinking blocks in Claude's turns (their words and calls stay): how a conversation goes on
    when Claude refuses its earlier thinking, the model then answering without that reasoning."""
    out = []
    for m in messages:
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            kept = [b for b in m["content"] if not (isinstance(b, dict) and b.get("type") in
                                                    ("thinking", "redacted_thinking"))]
            if not kept:   # a turn that was only thinking: nothing of it left to send
                continue
            m = dict(m, content=kept)
        out.append(m)
    return out


def unanswered(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A turn without a server tool's call that has no result in it (a tool search cut short, or one a paused turn
    was to go on from)."""
    answered = {b.get("tool_use_id") for b in raw if str(b.get("type", "")).endswith("_tool_result")}
    return [b for b in raw if not (b.get("type") == "server_tool_use" and b.get("id") not in answered)]


def without_calls(raw: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """A cut or declined turn as it is sent again: without its tool calls, which never ran (no result follows them,
    and Claude refuses a tool_use without its tool_result), nor a tool search cut before its result. None when no text
    is left (the blocks are sent then)."""
    kept = [b for b in unanswered(raw) if b.get("type") != "tool_use"]
    return kept if any(b.get("type") == "text" for b in kept) else None


def _image(image: Dict[str, str]) -> Dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": image["mime"], "data": image["data"]}}


def _document(doc: Dict[str, str]) -> Dict[str, Any]:
    return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": doc["data"]}}


def to_messages(history: List[Dict[str, Any]], own: str = "anthropic", model: str = "",
                apart: Optional[bool] = None) -> List[Dict[str, Any]]:
    """own: this provider's name; its own turns go back as they came (raw: thinking blocks and all). model: the model
    asked; one that takes system messages mid-conversation gets a change to the chat's setup (a user message's
    "update") as one, after that turn and before Claude's next, rather than in the person's words. apart: whether it
    does, where the route says otherwise (AnthropicProvider.inline)."""
    out: List[Dict[str, Any]] = []
    n = 0
    apart = system_messages(model) if apart is None else apart
    later: List[str] = []   # updates waiting for the end of their user turn

    def system() -> None:   # a system message follows a user turn, and an assistant turn or nothing follows it
        if later:
            out.append({"role": "system", "content": "\n\n".join(later)})
            later.clear()
    after: List[Optional[str]] = [None] * len(history)   # the role of the turn that follows each message, if any
    for j in range(len(history) - 2, -1, -1):
        role = history[j + 1]["role"]
        after[j] = role if role in ("user", "assistant", "tool") else after[j + 1]
    for i, m in enumerate(history):
        if m["role"] == "user":   # PDFs and pictures first, then the words about them
            update = m.get("update") if apart else None
            words = user_text(m, updates=not update)
            media = [_document(d) for d in m.get("documents") or []] + [_image(i) for i in m.get("images") or []]
            if update and not (words or media or out and out[-1]["role"] == "user"):
                words, update = user_text(m), None   # no user turn for a system message to follow: in the words
            content = media + ([{"type": "text", "text": words}] if words else [])
            if update:
                later.append(update)
            if not content:
                continue
            if out and out[-1]["role"] == "user":   # written while Kelvin worked: after the tool results, one turn
                out[-1]["content"] = list(out[-1]["content"]) + content
            else:
                out.append({"role": "user", "content": content})
        elif m["role"] == "assistant":
            if m.get("provider") == own and m.get("raw"):
                content = m["raw"]
                if after[i] not in (None, "assistant"):   # a paused turn that something else followed (its answer
                    content = unanswered(content)          # was stopped): not a search left waiting for a result
            else:
                content = []
                for b in m["blocks"]:
                    if b["type"] == "text" and b["text"]:
                        content.append({"type": "text", "text": b["text"]})
                    elif b["type"] == "tool_call":
                        n += 1
                        content.append({"type": "tool_use", "id": safe_id(b["id"], n), "name": b["name"],
                                        "input": b["input"] if isinstance(b["input"], dict) else {}})
            if content:
                system()
                out.append({"role": "assistant", "content": content})
        elif m["role"] == "tool":
            content = [
                {"type": "tool_result", "tool_use_id": safe_id(r["id"], 0),
                 "content": ([{"type": "text", "text": r["content"] or "(empty)"}] + [_image(i) for i in r["images"]])
                 if r.get("images") else r["content"] or "(empty)",
                 "is_error": bool(r["is_error"])} for r in m["results"]]
            docs = [d for r in m["results"] for d in r.get("documents") or []]
            if docs:   # after every result, as a user message may go on
                content += [{"type": "text", "text": DOCUMENTS_NOTE}] + [_document(d) for d in docs]
            out.append({"role": "user", "content": content})
    system()
    return out


class AnthropicProvider(Provider):
    name = "anthropic"
    label = "Claude"
    sees_images = True
    sees_pdfs = True
    can_search = True
    keeps_history = True   # each earlier turn sent as it was: Claude's thinking is bound to it (agent.keeps_history)

    def __init__(self, api_key: Optional[str], base_url: Optional[str] = None, name: str = "", label: str = ""):
        """base_url, name, label: Claude through the Kelvin hub (hubmodels.py), with the hub token as the key."""
        self.name, self.label = name or self.name, label or self.label
        self.client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url) if api_key else None
        self.binding = True   # asks the API to drop unbound thinking blocks; off for good where a route refuses it
        # where a request with a chat's update as a system message was refused for it: the models whose refusal
        # fallback then goes (Sonnet 5.5's goes to Sonnet 5, which takes none), and those whose updates then go in the
        # person's words (a gateway that doesn't know the role)
        self.no_fallback: set = set()
        self.inline: set = set()

    async def search(self, model: str, query: str, limit: int = 8) -> List[Dict[str, str]]:
        """Claude's web search (a server tool: Anthropic runs the searches). The results it found, with Claude's
        notes on those worth opening first."""
        from ..websearch import PROMPT, merge, results_in
        message = await self.client.messages.create(
            model=model, max_tokens=4000,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=[{"role": "user", "content": PROMPT.format(query=query, limit=limit)}])
        hits: List[Dict[str, str]] = []
        words = []
        for block in message.content:
            if block.type == "web_search_tool_result":
                if not isinstance(block.content, list):   # the search itself failed
                    raise RuntimeError("Claude's web search: %s" % getattr(block.content, "error_code", "failed"))
                hits += [{"title": r.title or "", "url": r.url, "snippet": ""} for r in block.content
                         if getattr(r, "type", "") == "web_search_result"]
            elif block.type == "text":
                words.append(block.text)
        return merge(results_in("".join(words)), hits, limit)

    def available(self) -> bool:
        return self.client is not None

    @staticmethod
    def searches_tools(model: str) -> bool:
        """Can this model find Kelvin's rarer tools itself (Anthropic's tool search)? Then a chat declares every tool
        from its first request (agent.py, _declared) instead of loading them with find_tools."""
        return searches_tools(model)

    async def models(self) -> List[ModelInfo]:
        return [ModelInfo(mid, label, self.name) for mid, label in MODELS]

    async def stream(self, model: str, system: str, history: List[Dict[str, Any]],
                     tools: List[Dict[str, Any]], emit: Emit) -> TurnResult:
        effort = EFFORT.get()
        kwargs: Dict[str, Any] = dict(
            model=model,
            max_tokens=64000,
            messages=to_messages(history, self.name, model, apart=False if model in self.inline else None),
            tools=tool_list(tools),
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": effort if effort in EFFORTS else DEFAULT_EFFORT},
            **cached(system, tools),
        )
        fallback = model in FALLBACK_MODELS and model not in self.no_fallback
        betas = [FALLBACK_BETA] if fallback else []
        if fallback:
            kwargs.update(fallbacks="default")
        retries, stripped = 0, False
        while True:
            if self.binding:
                kwargs["thinking"] = dict(kwargs["thinking"], block_binding=DROP_UNBOUND)
                kwargs["betas"] = betas + [BINDING_BETA]
            else:
                kwargs["thinking"] = {k: v for k, v in kwargs["thinking"].items() if k != "block_binding"}
                if betas:
                    kwargs["betas"] = betas
                else:
                    kwargs.pop("betas", None)
            try:
                async with self.client.beta.messages.stream(**kwargs) as stream:
                    async for event in stream:
                        if event.type == "content_block_delta":
                            if event.delta.type == "text_delta":
                                await emit({"type": "text", "delta": event.delta.text})
                            elif event.delta.type == "thinking_delta":
                                await emit({"type": "thinking", "delta": event.delta.thinking})
                    message = await stream.get_final_message()
                break
            except anthropic.BadRequestError as e:
                said = str(e).lower()
                if not stripped and ("different conversation" in said or "invalid `signature`" in said
                                     or "invalid signature" in said):
                    # earlier thinking Claude no longer vouches for (a route without the control below, an edited
                    # turn): the documented recovery, once: this request without any earlier thinking. (Only on a
                    # route that refused the control: there each later step of that chat pays one refused request.)
                    kwargs["messages"] = without_thinking(kwargs["messages"])
                    stripped = True
                elif self.binding and "binding" in said:
                    # a route (a gateway, an older API) that doesn't take the binding control: without it from now on
                    self.binding = False
                elif ("system" in said or "role" in said) and any(m["role"] == "system" for m in kwargs["messages"]):
                    if "fallbacks" in kwargs:   # its refusal fallback's model takes no system message: none for it
                        self.no_fallback.add(model)
                        del kwargs["fallbacks"]
                        betas = [b for b in betas if b != FALLBACK_BETA]
                    else:   # no system message mid-conversation here after all: the updates in the person's words,
                        self.inline.add(model)   # for this model, from now on
                        kwargs["messages"] = to_messages(history, self.name, model, apart=False)
                        if stripped:
                            kwargs["messages"] = without_thinking(kwargs["messages"])
                        if model in self.no_fallback:   # the fallback wasn't what it refused: it comes back
                            self.no_fallback.discard(model)
                            kwargs["fallbacks"] = "default"
                            betas = [FALLBACK_BETA] + betas
                else:
                    raise
                await emit({"type": "reset"})
            except ValueError:
                # Tool-call JSON the SDK could not parse at all: re-issue the turn (bounded).
                retries += 1
                if retries > MAX_JSON_RETRIES:
                    raise
                await emit({"type": "reset"})

        raw = sanitize([b.to_dict(exclude_none=True) for b in message.content])
        blocks: List[Dict[str, Any]] = []
        for b in raw:
            if b["type"] == "text":
                blocks.append({"type": "text", "text": b["text"]})
            elif b["type"] == "thinking" and b.get("thinking"):
                blocks.append({"type": "thinking", "text": b["thinking"]})
            elif b["type"] == "tool_use":
                blocks.append({"type": "tool_call", "id": b["id"], "name": b["name"],
                               "input": b.get("input") if isinstance(b.get("input"), dict) else None})
        result = TurnResult(blocks, raw, model=message.model)
        stop = message.stop_reason
        if stop == "refusal":
            result.stop_reason = "refusal"
            result.blocks = [b for b in blocks if b["type"] != "tool_call"]
            result.raw = without_calls(raw)
            result.note = "The model declined this request."
        elif stop in ("max_tokens", "model_context_window_exceeded"):
            result.stop_reason = "max_tokens"
            result.blocks = [b for b in blocks if b["type"] != "tool_call"]  # a truncated tool input must not run
            result.raw = without_calls(raw)
            result.note = "The answer hit the length limit."
        elif stop == "pause_turn":
            result.stop_reason = "pause"
        elif result.tool_calls:
            result.stop_reason = "tool_calls"
        return result
