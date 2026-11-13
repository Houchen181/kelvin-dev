"""A stand-in for the `claude` program (Claude Code bridge tests): prints stream-json the way `claude -p` does.

Two ways, as the bridge uses `claude`:
- the text protocol (and operate_computer, and the pictures message): the whole input at once, one answer;
- native tool calls (claudecode_native.py: stream-json input and Kelvin's tools as an "sdk" MCP server), as claude
  2.1.304 does it (probed 2026-11-12): the SDK handshake, Kelvin's tools listed and called through control requests
  and waited for, the person's messages from meanwhile taken in at the next tool result (echoed with
  --replay-user-messages), a changed tool list fetched again when Kelvin says so, sessions kept (FAKE_CLAUDE_HOME) and
  resumed (--resume, --fork-session) with a call left waiting marked interrupted.

FAKE_CLAUDE_LOG: a file to record what it was given (args, stdin, the system prompt file's text; natively, one line
  per event: args, every line on stdin, the session, tool lists, the results Kelvin gave, messages taken in).
FAKE_CLAUDE_FAIL: answer with an error result.  FAKE_CLAUDE_SLOW: keep working until killed.
FAKE_CLAUDE_OLD: refuse --effort, as a `claude` from before it does.  FAKE_CLAUDE_NO_SNAPSHOT: refuse
  --system-prompt-snapshot.  FAKE_CLAUDE_NO_NATIVE: refuse --replay-user-messages, as an old `claude` would.
FAKE_CLAUDE_VERSION: the version its first event says (2.1.304 when not set).  FAKE_CLAUDE_QUEUE: a message that
  comes while a call runs gets a turn of its own after the answer (not taken in with the result), as an older
  `claude` might (FAKE_CLAUDE_QUEUE_DELAY: seconds before that turn begins).  FAKE_CLAUDE_MCP_FAILED: Kelvin's tools
  didn't connect (the first event says so).  FAKE_CLAUDE_EARLY, FAKE_CLAUDE_CANCEL: see turn().
FAKE_CLAUDE_SCRIPT: a JSON file of the model's messages, played in order across runs (the place kept next to it):
  each a list of {"think": text}, {"text": text}, {"call": tool, "input": {...}}. In a text, {result} is the last tool
  result it got, {said} the person's last message, {thought} its thinking so far in the session, {tools} its tools.
  Without one: a call of get_value, then the gate's value from its result."""
import json
import os
import sys
import time
import uuid
from collections import deque

PREFIX = "mcp__kelvin__"


def out(event):
    print(json.dumps(event), flush=True)


def note(entry):
    """One line of the log, whole: several runs at once (Compare's lanes) take turns appending."""
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if not log:
        return
    line = (json.dumps(dict(entry, pid=os.getpid())) + "\n").encode("utf-8")
    with open(log, "ab") as f:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            try:
                f.write(line)
                f.flush()
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(line)
            f.flush()
            fcntl.flock(f, fcntl.LOCK_UN)


def main():
    args = sys.argv[1:]
    if args[:2] == ["auth", "status"]:
        out({"loggedIn": True, "authMethod": "claude.ai"})
        return 0
    if os.environ.get("FAKE_CLAUDE_OLD") and "--effort" in args:   # a `claude` from before --effort
        print("error: unknown option '--effort'", file=sys.stderr, flush=True)
        return 1
    if native_run(args):
        return native(args)
    stdin = sys.stdin.read()
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        system = ""
        if "--system-prompt-file" in args:
            with open(args[args.index("--system-prompt-file") + 1], encoding="utf-8") as f:
                system = f.read()
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"args": args, "stdin": stdin, "system": system}) + "\n")
    out({"type": "system", "subtype": "init", "model": "claude-opus-5-5", "tools": []})
    if os.environ.get("FAKE_CLAUDE_FAIL"):
        out({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login"})
        return 1
    if "--mcp-config" in args:   # operate_computer: a few screen steps, then a report
        for name, inp in (("mcp__screen__screenshot", {}), ("mcp__screen__click_at", {"x": 120, "y": 340})):
            out({"type": "assistant", "parent_tool_use_id": None,
                 "message": {"content": [{"type": "tool_use", "id": "t1", "name": name, "input": inp}]}})
            time.sleep(0.2)
        while os.environ.get("FAKE_CLAUDE_SLOW"):
            time.sleep(0.2)
        out({"type": "result", "subtype": "success", "is_error": False, "result": "Opened LaserDesk; power reads 5 mW."})
        return 0
    pictures = 0
    if "--input-format" in args and args[args.index("--input-format") + 1] == "stream-json":
        if "-p" not in args:
            out({"type": "result", "subtype": "error", "is_error": True,
                 "result": "Error: --input-format=stream-json requires --print"})
            return 1
        said = []
        for line in stdin.splitlines():   # user messages as Claude Code's SDKs send them
            if not line.strip():
                continue
            message = json.loads(line)
            assert message["type"] == "user" and message["message"]["role"] == "user"
            for block in message["message"]["content"]:
                if block["type"] == "text":
                    said.append(block["text"])
                else:
                    assert block["type"] == "image" and block["source"]["type"] == "base64"
                    pictures += 1
        stdin = "\n".join(said)
    if pictures:
        text = "I see %d picture%s." % (pictures, "" if pictures == 1 else "s")
    elif "<tool_result" in stdin:
        text = "The gate is at 0 V."
    else:
        text = 'Checking the gate.\n<tool_call name="get_value">{"instrument": "smu", "parameter": "V"}</tool_call>'
    for i in range(0, len(text), 7):   # small pieces, so a tag arrives split
        out({"type": "stream_event", "event": {"type": "content_block_delta",
                                               "delta": {"type": "text_delta", "text": text[i:i + 7]}}})
    out({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [{"type": "text", "text": text}]}})
    out({"type": "result", "subtype": "success", "is_error": False, "result": text, "total_cost_usd": 0})
    return 0


# ---------------------------------------------------------------------- native tool calls

def native_run(args):
    """The bridge's native way: stream-json input and Kelvin's tools as an in-process ("sdk") MCP server."""
    if "--input-format" not in args or args[args.index("--input-format") + 1] != "stream-json":
        return False
    return "--mcp-config" in args and '"sdk"' in args[args.index("--mcp-config") + 1]


class Stdin:
    """What Kelvin writes, line by line as it comes: answers to this program's requests (kept by their ids), the
    person's messages (in order), and Kelvin's own requests (a changed tool list: fetched again at once)."""

    def __init__(self):
        self.answers, self.users, self.tools, self.together, self.n = {}, deque(), [], set(), 0

    def pump(self):
        raw = sys.stdin.buffer.readline()
        if not raw:
            return False
        line = json.loads(raw)
        note({"stdin": line})
        if line["type"] == "control_response":
            self.answers[line["response"]["request_id"]] = line["response"]
        elif line["type"] == "user":
            self.users.append(line)
        elif line["type"] == "control_request":
            out({"type": "control_response", "response": {"subtype": "success", "request_id": line["request_id"]}})
            message = line["request"].get("message") or {}
            if message.get("method") == "notifications/tools/list_changed":
                self.relist()
        return True

    def answer(self, rid):
        while rid not in self.answers:
            if not self.pump():
                return None
        return self.answers.pop(rid)

    def request(self, method, params=None):
        """A request to Kelvin's MCP server, sent: (its control request id, its JSON-RPC id)."""
        self.n += 1
        rid = "fake_%d" % self.n
        message = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            message["params"] = params
        out({"type": "control_request", "request_id": rid,
             "request": {"subtype": "mcp_message", "server_name": "kelvin", "message": message}})
        return rid, self.n

    def ask(self, method, params=None):
        """A request to Kelvin's MCP server; its JSON-RPC answer (None once the input ended)."""
        answer = self.answer(self.request(method, params)[0])
        return None if answer is None else answer["response"]["mcp_response"]

    def tell(self, method, params=None):
        self.n += 1
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        out({"type": "control_request", "request_id": "fake_%d" % self.n, "request": {
            "subtype": "mcp_message", "server_name": "kelvin", "message": message}})
        self.answer("fake_%d" % self.n)

    def relist(self):
        listed = self.ask("tools/list")["result"]["tools"]
        self.tools = [t["name"] for t in listed]
        self.together = {t["name"] for t in listed if (t.get("annotations") or {}).get("readOnlyHint")}
        note({"tools": self.tools, "always_loaded": all((t.get("_meta") or {}).get("anthropic/alwaysLoad")
                                                         for t in listed)})

    def user(self):
        while not self.users:
            if not self.pump():
                return None
        return self.users.popleft()


def text_of(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text")


def native(args):
    if os.environ.get("FAKE_CLAUDE_NO_NATIVE"):   # an old `claude`
        print("error: unknown option '--replay-user-messages'", file=sys.stderr, flush=True)
        return 1
    if os.environ.get("FAKE_CLAUDE_NO_SNAPSHOT") and "--system-prompt-snapshot" in args:
        print("error: unknown option '--system-prompt-snapshot'", file=sys.stderr, flush=True)
        return 1
    system = ""
    if "--system-prompt-file" in args:
        with open(args[args.index("--system-prompt-file") + 1], encoding="utf-8") as f:
            system = f.read()
    note({"args": args, "system": system})
    stdin = Stdin()
    raw = sys.stdin.buffer.readline()   # the SDK handshake first: Kelvin's initialize, answered
    if not raw:
        return 0
    hello = json.loads(raw)
    note({"stdin": hello})
    if hello.get("type") != "control_request" or (hello.get("request") or {}).get("subtype") != "initialize":
        print("error: expected the SDK's initialize request first", file=sys.stderr, flush=True)
        return 1
    out({"type": "control_response", "response": {"subtype": "success", "request_id": hello["request_id"],
                                                  "response": {"commands": []}}})
    # then Kelvin's tools, through its MCP server
    stdin.ask("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "fake"}})
    stdin.tell("notifications/initialized")
    stdin.relist()
    home = os.environ.get("FAKE_CLAUDE_HOME") or os.path.join(os.getcwd(), ".fake-claude")
    keep = "--no-session-persistence" not in args
    resume = args[args.index("--resume") + 1] if "--resume" in args else ""
    convo = []
    sid = resume or str(uuid.uuid4())
    if resume:
        try:
            with open(os.path.join(home, resume + ".json"), encoding="utf-8") as f:
                convo = json.load(f)
        except OSError:
            print("No conversation found with session ID: %s" % resume, file=sys.stderr, flush=True)
            return 1
        if "--fork-session" in args:
            sid = str(uuid.uuid4())
        last = convo[-1] if convo else {}
        dangling = [b for b in last.get("content") or [] if last.get("role") == "assistant"
                    and isinstance(b, dict) and b.get("type") == "tool_use"]
        if dangling:   # a call left waiting when the run ended: Claude Code marks it interrupted
            convo.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b["id"], "is_error": True,
                 "content": "[Request interrupted by user for tool use]"} for b in dangling]})
    note({"session": sid, "resumed": resume, "forked": "--fork-session" in args, "loaded": len(convo), "kept": keep})

    def save():   # whole or not at all: another run (a Compare lane) may be reading it
        if keep:
            os.makedirs(home, exist_ok=True)
            path = os.path.join(home, sid + ".json")
            with open(path + ".%d.tmp" % os.getpid(), "w", encoding="utf-8") as f:
                json.dump(convo, f)
            os.replace(path + ".%d.tmp" % os.getpid(), path)
    model = args[args.index("--model") + 1] if "--model" in args else ""
    out({"type": "system", "subtype": "init", "session_id": sid, "model": model, "permissionMode": "dontAsk",
         "claude_code_version": os.environ.get("FAKE_CLAUDE_VERSION", "2.1.304"),
         "tools": [PREFIX + t for t in stdin.tools], "mcp_servers": [{
             "name": "kelvin", "status": "failed" if os.environ.get("FAKE_CLAUDE_MCP_FAILED") else "connected",
             "source": "sdk"}]})
    if os.environ.get("FAKE_CLAUDE_FAIL"):
        out({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login",
             "session_id": sid})
        return 1
    first = True
    while True:
        message = stdin.user()
        if message is None:   # the input ended: done
            return 0
        if not first:   # a message that waited for a turn of its own (FAKE_CLAUDE_QUEUE)
            time.sleep(float(os.environ.get("FAKE_CLAUDE_QUEUE_DELAY") or 0))
        first = False
        take(message, convo, args)
        save()
        if turn(stdin, convo, save, sid, args) is None:
            return 0


def take(message, convo, args):
    """The person's message, into the conversation (echoed: --replay-user-messages)."""
    if "--replay-user-messages" in args:
        out(dict(message, isReplay=True))
    content = message["message"]["content"]
    pictures = sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image")
    if pictures:
        note({"pictures": pictures})
    note({"heard": text_of(content)})
    convo.append({"role": "user", "content": content})


def script_message(convo):
    path = os.environ.get("FAKE_CLAUDE_SCRIPT")
    if not path:
        last = convo[-1]["content"] if convo else []
        if isinstance(last, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in last):
            return [{"text": "The gate reads {result}."}]
        return [{"text": "Checking the gate."}, {"call": "get_value", "input": {"instrument": "smu", "parameter": "V"}}]
    with open(path, encoding="utf-8") as f:
        script = json.load(f)
    at_path = path + ".at"
    at = int(open(at_path).read()) if os.path.exists(at_path) else 0
    with open(at_path, "w") as f:
        f.write(str(at + 1))
    return script[at] if at < len(script) else [{"text": "(the script has ended)"}]


def fill(text, convo, tools):
    results = [b for m in convo if m["role"] == "user" and isinstance(m["content"], list)
               for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
    said = [m for m in convo if m["role"] == "user" and not (isinstance(m["content"], list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"]))]
    thought = [b["thinking"] for m in convo if m["role"] == "assistant" for b in m["content"]
               if b.get("type") == "thinking"]
    return (text.replace("{result}", text_of(results[-1]["content"]) if results else "")
            .replace("{said}", text_of(said[-1]["content"]) if said else "")
            .replace("{thought}", " | ".join(thought)).replace("{tools}", ",".join(tools)))


def stream(kind, text):
    for i in range(0, len(text), 7):
        key = "thinking" if kind == "thinking_delta" else "text"
        out({"type": "stream_event", "parent_tool_use_id": None,
             "event": {"type": "content_block_delta", "index": 0, "delta": {"type": kind, key: text[i:i + 7]}}})


def batches(uses, together):
    """Claude Code's order of running a message's calls: calls that may run together (readOnlyHint) side by side,
    each other call alone."""
    out_, run = [], []
    for u in uses:
        if u["name"][len(PREFIX):] in together:
            run.append(u)
            continue
        if run:
            out_.append(run)
            run = []
        out_.append([u])
    return out_ + ([run] if run else [])


def call(stdin, u, n):
    tool = u["name"][len(PREFIX):]
    return stdin.request("tools/call", {"name": tool, "arguments": u["input"],
                                        "_meta": {"claudecode/toolUseId": u["id"], "progressToken": n}})


def turn(stdin, convo, save, sid, args):
    """The model's messages until one without calls; each call asked of Kelvin and waited for (those that may run
    together at once). None: the input ended meanwhile. FAKE_CLAUDE_EARLY: a call that may run along is asked for
    as soon as the model has written it, before its message ends (as Claude Code starts one); FAKE_CLAUDE_CANCEL:
    and given up at once (Claude Code answers it itself, with an error). A text block with "slow": the model keeps
    writing it until killed."""
    n = 0
    while True:
        out({"type": "stream_event", "parent_tool_use_id": None, "event": {"type": "message_start"}})
        blocks, early = [], {}
        for b in script_message(convo):
            if "think" in b:
                stream("thinking_delta", b["think"])
                block = {"type": "thinking", "thinking": b["think"], "signature": "sig"}
            elif "text" in b:
                text = fill(b["text"], convo, stdin.tools)
                stream("text_delta", text)
                block = {"type": "text", "text": text}
                while os.environ.get("FAKE_CLAUDE_SLOW") or b.get("slow"):   # keeps writing until killed
                    time.sleep(0.2)
            else:
                n += 1
                block = {"type": "tool_use", "id": "toolu_fake%d_%s" % (n, uuid.uuid4().hex[:6]),
                         "name": PREFIX + b["call"], "input": b.get("input") or {}}
            blocks.append(block)
            out({"type": "assistant", "parent_tool_use_id": None, "session_id": sid,
                 "message": {"role": "assistant", "content": [block]}})
            if block["type"] == "tool_use" and os.environ.get("FAKE_CLAUDE_EARLY") and \
                    block["name"][len(PREFIX):] in stdin.together:
                early[block["id"]] = call(stdin, block, n)
                if os.environ.get("FAKE_CLAUDE_CANCEL"):
                    stdin.tell("notifications/cancelled", {"requestId": early[block["id"]][1], "reason": "aborted"})
                    early[block["id"]] = None
        out({"type": "stream_event", "parent_tool_use_id": None, "event": {"type": "message_stop"}})
        convo.append({"role": "assistant", "content": blocks})
        save()
        uses = [b for b in blocks if b["type"] == "tool_use"]
        if not uses:
            text = "\n".join(b["text"] for b in blocks if b["type"] == "text")
            out({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": sid,
                 "stop_reason": "end_turn"})
            return True
        got = {}
        for group in batches(uses, stdin.together):
            asked = []
            for u in group:
                if u["name"][len(PREFIX):] not in stdin.tools:   # Claude Code's own answer: Kelvin is never asked
                    got[u["id"]] = {"content": [{"type": "text", "text": "Error: No such tool available: %s"
                                                 % u["name"]}], "isError": True}
                elif u["id"] in early and early[u["id"]] is None:   # given up: Claude Code's own answer
                    got[u["id"]] = {"content": [{"type": "text", "text": "Error: the call was cancelled"}],
                                    "isError": True}
                else:
                    asked.append((u, early.get(u["id"]) or call(stdin, u, n)))
            for u, (rid, _) in asked:
                answer = stdin.answer(rid)
                if answer is None:
                    return None
                got[u["id"]] = answer["response"]["mcp_response"]["result"]
                note({"result_for": u["id"], "tool": u["name"][len(PREFIX):], "content": got[u["id"]]["content"],
                      "is_error": got[u["id"]].get("isError")})
        results = []
        for u in uses:
            out({"type": "user", "parent_tool_use_id": None, "session_id": sid,
                 "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": u["id"],
                                                          "content": got[u["id"]]["content"]}]}})
            results.append({"type": "tool_result", "tool_use_id": u["id"], "content": got[u["id"]]["content"],
                            "is_error": got[u["id"]].get("isError")})
        convo.append({"role": "user", "content": results})
        queued = os.environ.get("FAKE_CLAUDE_QUEUE")   # (an older `claude`: they wait for a turn of their own)
        while stdin.users and not queued:   # the person's messages from meanwhile, taken in with the results
            message = stdin.users.popleft()
            take(message, convo, args)
            note({"taken": text_of(message["message"]["content"])})
        save()


if __name__ == "__main__":
    sys.exit(main())
