"""A stand-in for the `claude` program (Claude Code bridge tests): prints stream-json the way `claude -p` does.

FAKE_CLAUDE_LOG: a file to record what it was given (args, stdin, the system prompt file's text).
FAKE_CLAUDE_FAIL: answer with an error result.  FAKE_CLAUDE_SLOW: keep working until killed."""
import json
import os
import sys
import time


def out(event):
    print(json.dumps(event), flush=True)


def main():
    args = sys.argv[1:]
    if args[:2] == ["auth", "status"]:
        out({"loggedIn": True, "authMethod": "claude.ai"})
        return 0
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


if __name__ == "__main__":
    sys.exit(main())
