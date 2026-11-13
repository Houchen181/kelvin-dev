"""What an episode's model requests cost: every request to Claude's Messages API, its tokens and dollars.

Kelvin's arms talk to Claude through the Anthropic SDK; meter(provider, ...) gives the provider a client whose HTTP
transport keeps each request's usage (from the response's message_start / message_delta events, or its JSON) and
refuses new requests once the episode's spending cap is reached. Claude Code reports its own (claudecode.py).

Prices are USD per million tokens (Anthropic's list prices, 2026-11). Cache writes are 1.25x input for a 5-minute
entry and 2x for an hour's; cache reads are the model's cache-read price.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional

PRICES: Dict[str, Dict[str, float]] = {
    # model: input, output, cache read (per MTok)
    "claude-opus-5-5": {"in": 4.0, "out": 20.0, "read": 0.20},
    "claude-sonnet-5-5": {"in": 2.0, "out": 10.0, "read": 0.10},
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50, "read": 0.01},
    "claude-fable-5-2": {"in": 8.0, "out": 40.0, "read": 0.20},
    "claude-fable-5-1": {"in": 10.0, "out": 50.0, "read": 1.00},
    "claude-opus-5": {"in": 5.0, "out": 25.0, "read": 0.50},
    "claude-sonnet-5": {"in": 2.0, "out": 10.0, "read": 0.20},
    "claude-opus-4-8": {"in": 5.0, "out": 25.0, "read": 0.50},
}


def price(model: str) -> Dict[str, float]:
    m = str(model or "")
    for key in sorted(PRICES, key=len, reverse=True):
        if m.startswith(key):
            return PRICES[key]
    return PRICES["claude-opus-5-5"]   # unknown: priced as Opus, so a budget errs on the safe side


def cost(model: str, u: Dict[str, Any]) -> float:
    """Dollars for one request's usage (the API's usage object, with cache_creation's 5m/1h split when given)."""
    p = price(model)
    write = int(u.get("cache_creation_input_tokens") or 0)
    split = u.get("cache_creation") or {}
    w1h = int(split.get("ephemeral_1h_input_tokens") or 0)
    w5m = int(split.get("ephemeral_5m_input_tokens") or 0)
    if w1h + w5m == 0 and write:   # no split given: Kelvin asks for an hour (anthropic_provider.LONG_CACHE)
        w1h = write
    return (int(u.get("input_tokens") or 0) * p["in"] + int(u.get("output_tokens") or 0) * p["out"]
            + int(u.get("cache_read_input_tokens") or 0) * p["read"] + w5m * p["in"] * 1.25
            + w1h * p["in"] * 2.0) / 1e6


class OverBudget(RuntimeError):
    pass


class Ledger:
    """The episode's requests, in order; thread-safe."""

    def __init__(self, cap_usd: float = 0.0):
        self.cap = float(cap_usd or 0.0)
        self.requests: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    @property
    def usd(self) -> float:
        with self._lock:
            return sum(r.get("usd") or 0.0 for r in self.requests)

    def check(self) -> None:
        if self.cap and self.usd >= self.cap:
            raise OverBudget("the episode's spending cap of $%.2f is reached" % self.cap)

    def add(self, entry: Dict[str, Any]) -> None:
        with self._lock:
            self.requests.append(entry)

    def totals(self) -> Dict[str, Any]:
        with self._lock:
            reqs = list(self.requests)
        return totals(reqs)


def totals(reqs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """An episode's tokens, dollars and model calls from its ledger entries (one per API request on a key; one per
    `claude` run on a plan, whose `calls` are the model's messages in it)."""
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    out: Dict[str, Any] = {k: sum(int((r.get("usage") or {}).get(k) or 0) for r in reqs) for k in keys}
    out["requests"] = sum(int(r["calls"]) if r.get("calls") is not None else int(r.get("requests") or 1) for r in reqs)
    out["usd"] = round(sum(r.get("usd") or 0.0 for r in reqs), 6)
    out["seconds"] = round(sum(r.get("seconds") or 0.0 for r in reqs), 3)
    out["errors"] = sum(1 for r in reqs if (r.get("status") or 200) >= 400)
    return out


def flag(args: List[str], name: str) -> Optional[str]:
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None


def priced_as(requested: Optional[str], model_usage: Optional[Dict[str, Any]]) -> str:
    """The model a `claude` run's usage is priced as: the one of its modelUsage it was asked for (an alias such as
    "opus" names claude-opus-5-5), else the only one there, else what was asked."""
    names = list(model_usage or {})
    want = str(requested or "")
    for n in names:
        if want and (n == want or n.startswith(want) or want in n):
            return n
    if len(names) == 1:
        return names[0]
    return want or (names[0] if names else "")


def run_entry(path: str, result: Optional[Dict[str, Any]], model: Optional[str], effort: Optional[str],
              calls: int, started: float, **more: Any) -> Dict[str, Any]:
    """The ledger entry of one `claude` run (or one turn of a long-running one), from its result event. Its dollars
    come from the run's own usage, not total_cost_usd: on a resumed session Claude Code reports the whole session's
    cost so far there (and modelUsage the whole session's tokens), which summed over turns counts earlier ones twice."""
    r = result or {}
    u = r.get("usage") or {}
    priced = priced_as(model, r.get("modelUsage"))
    entry = {"path": path, "model": model, "effort": effort, "served_by": priced if r else None, "usage": u,
             "usd": cost(priced, u) if u else 0.0, "usd_reported": float(r.get("total_cost_usd") or 0.0),
             "calls": int(calls or (1 if u else 0)), "num_turns": r.get("num_turns"),
             "seconds": round(float(r.get("duration_api_ms") or 0) / 1000.0 or time.time() - started, 3),
             "status": 200 if r and not r.get("is_error") else 500, "started": started,
             "stop_reason": r.get("stop_reason") or r.get("subtype")}
    entry.update(more)
    return entry


def message_id(event: Dict[str, Any]) -> Optional[str]:
    """The model's message (one API request) a top-level stream-json `assistant` event belongs to."""
    if event.get("type") != "assistant" or event.get("parent_tool_use_id"):
        return None
    return (event.get("message") or {}).get("id") or None


def _usage_of_sse(text: str) -> Dict[str, Any]:
    """The usage and model of a streamed response, from its events."""
    usage: Dict[str, Any] = {}
    model, stop = None, None
    event = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            try:
                data = json.loads(line[5:].strip())
            except ValueError:
                continue
            kind = data.get("type") or event
            if kind == "message_start":
                msg = data.get("message") or {}
                model = msg.get("model") or model
                usage.update(msg.get("usage") or {})
            elif kind == "message_delta":
                for k, v in (data.get("usage") or {}).items():
                    if v is not None:
                        usage[k] = v
                stop = (data.get("delta") or {}).get("stop_reason") or stop
    return {"usage": usage, "model": model, "stop_reason": stop}


def meter_bridge(ledger: Ledger) -> None:
    """Keep every `claude` run of Kelvin's Claude Code bridge in the ledger: its tokens and model calls, and their
    API-equivalent dollars (on a plan nothing is billed; the dollars stand for how much of the plan's limits it used).
    The text protocol runs one `claude -p` per model step (claudecode.stream); the native one (harness-fixes since
    989aa06) keeps one running for a chat's whole answer (claudecode.Running), resumed between answers. The cap stops
    new runs."""
    from labharness import claudecode as cc
    plain = cc.stream

    async def stream(cmd: Any, args: List[str], stdin_text: str, cwd: Any, *a: Any, **k: Any):
        ledger.check()
        started = time.time()
        result: Optional[Dict[str, Any]] = None
        ids: set = set()
        try:
            async for ev in plain(cmd, args, stdin_text, cwd, *a, **k):
                if ev.get("type") == "result":
                    result = ev
                mid = message_id(ev)
                if mid:
                    ids.add(mid)
                yield ev
        finally:
            ledger.add(run_entry("claude -p", result, flag(args, "--model"), flag(args, "--effort"), len(ids),
                                 started, stdin_chars=len(stdin_text or "")))

    cc.stream = stream

    Plain = getattr(cc, "Running", None)
    if Plain is None:   # a Kelvin without the native bridge
        return

    class Running(Plain):   # type: ignore[misc, valid-type]
        """claudecode.Running, its result events kept as they are read (each answer's turn ends with one)."""

        def __init__(self, cmd: List[str], args: List[str], cwd: Any, handle: Any = None, env: Any = None):
            ledger.check()
            self._kb = {"model": flag(args, "--model"), "effort": flag(args, "--effort"), "ids": set(),
                        "resumed": "--resume" in args, "started": time.time(), "results": 0}
            inner = handle

            def watch(event: Dict[str, Any]) -> bool:
                try:
                    self._kb_saw(event)
                except Exception:   # never in the way of the run
                    pass
                return bool(inner(event)) if inner is not None else False

            super().__init__(cmd, args, cwd, handle=watch, env=env)

        def _kb_saw(self, event: Dict[str, Any]) -> None:
            mid = message_id(event)
            if mid:
                self._kb["ids"].add(mid)
            elif event.get("type") == "result":
                kb = self._kb
                ledger.add(run_entry("claude native", event, kb["model"], kb["effort"], len(kb["ids"]),
                                     kb["started"], resumed=kb["resumed"], nth=kb["results"]))
                kb["results"] += 1
                kb["ids"] = set()
                kb["started"] = time.time()

        def _read(self) -> None:
            try:
                super()._read()
            finally:
                kb = self._kb
                if kb["ids"] or not kb["results"]:   # ended (or was stopped) before its turn's result
                    ledger.add(run_entry("claude native", None, kb["model"], kb["effort"], len(kb["ids"]),
                                         kb["started"], resumed=kb["resumed"], nth=kb["results"],
                                         note="ended without a result: its usage is unknown"))

    cc.Running = Running


def meter(provider: Any, api_key: str, ledger: Ledger) -> None:
    """Give Kelvin's Anthropic provider a client whose requests the ledger keeps (and a cap stops)."""
    import anthropic
    import httpx2

    class Tee(httpx2.AsyncByteStream):
        def __init__(self, inner: Any, done: Any):
            self.inner, self.done, self.parts = inner, done, []

        async def __aiter__(self):
            async for chunk in self.inner:
                self.parts.append(chunk)
                yield chunk

        async def aclose(self) -> None:
            try:
                await self.inner.aclose()
            finally:
                self.done(b"".join(self.parts))

    class Metered(httpx2.AsyncBaseTransport):
        def __init__(self) -> None:
            self.inner = httpx2.AsyncHTTPTransport()

        async def handle_async_request(self, request: Any) -> Any:
            ledger.check()
            started = time.time()
            asked: Dict[str, Any] = {}
            try:
                body = json.loads(request.content or b"{}")
                asked = {"model": body.get("model"), "effort": (body.get("output_config") or {}).get("effort"),
                         "thinking": (body.get("thinking") or {}).get("type"), "tools": len(body.get("tools") or []),
                         "messages": len(body.get("messages") or []), "stream": bool(body.get("stream"))}
            except Exception:   # a body it can't read (a stream): the request is kept without these
                pass
            response = await self.inner.handle_async_request(request)

            def done(raw: bytes) -> None:
                entry: Dict[str, Any] = dict(asked, path=request.url.path, status=response.status_code,
                                             started=started, seconds=round(time.time() - started, 3))
                text = raw.decode("utf-8", errors="replace")
                if "text/event-stream" in response.headers.get("content-type", ""):
                    got = _usage_of_sse(text)
                else:
                    try:
                        data = json.loads(text) if text.strip() else {}
                    except ValueError:
                        data = {}
                    got = {"usage": data.get("usage") or {}, "model": data.get("model"),
                           "stop_reason": data.get("stop_reason")}
                    if response.status_code >= 400:
                        entry["error"] = text[:500]
                entry.update(usage=got["usage"], served_by=got["model"], stop_reason=got["stop_reason"])
                entry["usd"] = cost(got["model"] or asked.get("model") or "", got["usage"]) if got["usage"] else 0.0
                if request.url.path.endswith("/messages"):   # not count_tokens, models...
                    ledger.add(entry)

            return httpx2.Response(response.status_code, headers=response.headers,
                                   stream=Tee(response.stream, done), request=request,
                                   extensions=response.extensions)

        async def aclose(self) -> None:
            await self.inner.aclose()

    provider.client = anthropic.AsyncAnthropic(
        api_key=api_key, http_client=anthropic.DefaultAsyncHttpxClient(transport=Metered()))
