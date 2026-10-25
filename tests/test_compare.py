"""Several models at once (side by side, reading the lab only) and second opinions from another model."""
import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")

from labharness.web.review import REVIEW_TOOLS, answer_context, parse  # noqa: E402
from test_web import Scripted, client_for, human, parts, stream_turn  # noqa: E402,F401


def bot(name, turns, fail=None):
    cls = type(name.capitalize(), (Scripted,), {"name": name, "label": name.capitalize()})
    b = cls(turns)
    if fail:
        async def stream(*a, **k):
            raise RuntimeError(fail)
        b.stream = stream
    return b


def say(text):
    return [{"type": "text", "text": text}]


def tool(name, args, i="c1"):
    return [{"type": "tool_call", "id": i, "name": name, "input": args}]


def compare(client, headers, chat_id, text, models):
    import json
    events = []
    with client.stream("POST", "/api/chats/%s/compare" % chat_id, json={"text": text, "models": models},
                       headers=headers) as r:
        assert r.status_code == 200, r.read()
        for line in r.iter_lines():
            if line.strip():
                events.append(json.loads(line))
    return events


LINE = "plan: stage_scan\nsweep: {stage.position: {from: 0, to: 4, step: 1}}\nmeasure: [lockin.X]\n"


def test_models_answer_side_by_side_and_the_user_picks_one(parts):
    runtime, settings, store = parts
    alpha = bot("alpha", [tool("lab_status", {}, "a1"), say("Alpha: the lab is idle."), say("Alpha again.")])
    beta = bot("beta", [tool("set_value", {"instrument": "smu", "parameter": "V", "value": 2}, "b1"),
                        say("Beta: I would set the gate to 2 V."), say("Beta continues.")])
    client, headers = client_for(parts, {"alpha": alpha, "beta": beta})
    with client:
        chat = client.post("/api/chats", json={"model": "alpha:m1"}, headers=headers).json()
        assert client.post("/api/chats/%s/compare" % chat["id"], json={"text": "hi", "models": ["alpha:m1"]},
                           headers=headers).status_code == 400
        assert client.post("/api/chats/%s/compare" % chat["id"], json={"text": "hi", "models": ["alpha:m1", "nope:x"]},
                           headers=headers).status_code == 400
        events = compare(client, headers, chat["id"], "how is the lab?", ["alpha:m1", "beta:m1"])
        assert events[0]["type"] == "compare_start" and events[0]["models"] == ["alpha:m1", "beta:m1"]
        assert {e["lane"] for e in events if "lane" in e} == {0, 1}
        refused = next(e for e in events if e["type"] == "tool_end" and e["lane"] == 1)
        assert refused["is_error"] and "only tools that read" in refused["content"]
        assert runtime.lab.read("smu", "V") == 0                      # comparing never changes the lab
        assert next(e for e in events if e["type"] == "tool_end" and e["lane"] == 0)["content"].startswith("Virtual Lab")
        assert events[-2] == {"type": "compare_end", "chosen": 0} and events[-1]["type"] == "turn_end"
        assert "set_value" not in alpha.tool_names[0] and "lab_status" in alpha.tool_names[0]
        assert "remember" not in alpha.tool_names[0] and "Comparing models" in alpha.systems[0]

        saved = client.get("/api/chats/" + chat["id"], headers=headers).json()
        user = saved["messages"][0]
        assert [lane["model"] for lane in user["compare"]["lanes"]] == ["alpha:m1", "beta:m1"]
        assert saved["messages"][-1]["blocks"][0]["text"] == "Alpha: the lab is idle." and saved["model"] == "alpha:m1"

        assert client.post("/api/chats/%s/choose" % chat["id"], json={"index": 5}, headers=headers).status_code == 400
        assert client.post("/api/chats/%s/choose" % chat["id"], json={"index": 1}, headers=headers).json() == \
            {"chosen": 1, "model": "beta:m1"}
        saved = client.get("/api/chats/" + chat["id"], headers=headers).json()
        assert saved["model"] == "beta:m1" and saved["messages"][-1]["blocks"][0]["text"] == "Beta: I would set the gate to 2 V."
        assert len(saved["messages"]) == 1 + len(saved["messages"][0]["compare"]["lanes"][1]["messages"])

        stream_turn(client, headers, chat["id"], "go on", "beta:m1")    # the chat continues from Beta's answer
        assert beta.seen[-1] == ["user", "assistant", "tool", "assistant", "user"] and beta.prompts[-1] == ["how is the lab?", "go on"]
        assert client.post("/api/chats/%s/choose" % chat["id"], json={"index": 0}, headers=headers).status_code == 400


def test_a_failing_model_does_not_spoil_the_comparison(parts):
    good = bot("alpha", [say("Here is my answer.")])
    broken = bot("beta", [], fail="boom")
    client, headers = client_for(parts, {"alpha": good, "beta": broken})
    with client:
        chat = client.post("/api/chats", json={"model": "beta:m1"}, headers=headers).json()
        events = compare(client, headers, chat["id"], "question", ["beta:m1", "alpha:m1"])
        assert any(e["type"] == "error" and e["lane"] == 0 for e in events)
        assert events[-2] == {"type": "compare_end", "chosen": 1}     # the first answer that worked continues
        saved = client.get("/api/chats/" + chat["id"], headers=headers).json()
        assert saved["model"] == "alpha:m1" and saved["messages"][-1]["blocks"][0]["text"] == "Here is my answer."


def test_parse_verdicts():
    assert parse("verdict: go\n- fine", "plan") == ("go", "- fine")
    assert parse("**Verdict:** Stop\n- the laser is too strong", "plan")[0] == "stop"
    assert parse("Looks reasonable overall.", "plan") == ("caution", "Looks reasonable overall.")
    assert parse("verdict: maybe\n- x", "answer")[0] == "partly"
    with pytest.raises(ValueError):
        answer_context([])
    with pytest.raises(ValueError):
        answer_context([{"role": "user", "text": "hi"}])


def test_a_second_opinion_on_every_plan(parts):
    runtime, settings, store = parts
    settings.update({"reviewer": "rev:m1"})
    main = bot("main", [tool("run_plan", {"plan": LINE}), say("Done.")])
    rev = bot("rev", [say("verdict: caution\n- settle longer at each point\n- add a return sweep")])
    client, headers = client_for(parts, {"main": main, "rev": rev})
    with client:
        chat = client.post("/api/chats", json={"model": "main:m1"}, headers=headers).json()
        events = stream_turn(client, headers, chat["id"], "scan the stage", "main:m1")
        start = next(e for e in events if e["type"] == "review_start")
        opinion = next(e for e in events if e["type"] == "review")
        assert start["model"] == "rev:m1" and opinion["verdict"] == "caution" and "settle longer" in opinion["text"]
        prompt = rev.prompts[0][0]
        assert "scan the stage" in prompt and "The lab now" in prompt and "stage_scan" in prompt
        looks = set(rev.tool_names[0])                                  # the reviewer may only look things up
        assert "describe_run" in looks and looks <= set(REVIEW_TOOLS) and not {"run_plan", "set_value"} & looks
        end = next(e for e in events if e["type"] == "tool_end")
        assert end["content"].startswith("second opinion from Rev m1: verdict caution") and "done: 5 points" in end["content"]
        result = store.load(chat["id"])["messages"][2]["results"][0]
        assert result["review"]["verdict"] == "caution"
        assert "second opinion on stage_scan: caution" in runtime.events(10)


@pytest.mark.parametrize("answer", [False, True])
def test_a_stop_from_the_reviewer_goes_to_the_user(parts, answer):
    runtime, settings, store = parts
    settings.update({"reviewer": "rev:m1"})
    main = bot("main", [tool("run_plan", {"plan": LINE}), say("Ok.")])
    rev = bot("rev", [say("verdict: stop\n- the stage is not homed")])
    client, headers = client_for(parts, {"main": main, "rev": rev})
    with client:
        chat = client.post("/api/chats", json={"model": "main:m1"}, headers=headers).json()
        human(runtime.approver, [answer])
        events = stream_turn(client, headers, chat["id"], "scan", "main:m1")
        ask = next(e for e in events if e["type"] == "approval")
        assert ask["title"].startswith("Second opinion: Rev m1 advises not to run plan stage_scan")
        assert ask["items"] == ["the stage is not homed"]
        end = next(e for e in events if e["type"] == "tool_end")
        runs = runtime.pick_runs(last=5)
        if answer:
            assert "done: 5 points" in end["content"] and len(runs) == 1
        else:
            assert end["is_error"] and end["content"].startswith("not run: the second opinion") and runs == []


def test_no_reviewer_answer_does_not_block_the_run(parts):
    runtime, settings, store = parts
    settings.update({"reviewer": "rev:m1"})
    main = bot("main", [tool("run_plan", {"plan": LINE}), say("Ok.")])
    client, headers = client_for(parts, {"main": main, "rev": bot("rev", [], fail="overloaded")})
    with client:
        chat = client.post("/api/chats", json={"model": "main:m1"}, headers=headers).json()
        events = stream_turn(client, headers, chat["id"], "scan", "main:m1")
        opinion = next(e for e in events if e["type"] == "review")
        assert opinion["verdict"] == "" and "No second opinion" in opinion["text"] and "overloaded" in opinion["text"]
        assert "done: 5 points" in next(e for e in events if e["type"] == "tool_end")["content"]


def test_a_second_opinion_on_an_answer_reaches_the_next_turn_once(parts):
    runtime, settings, store = parts
    main = bot("main", [say("The peak is at 1.5 V."), say("Fair point."), say("Sure.")])
    rev = bot("rev", [say("verdict: partly\n- no uncertainty given")])
    client, headers = client_for(parts, {"main": main, "rev": rev})
    with client:
        chat = client.post("/api/chats", json={"model": "main:m1"}, headers=headers).json()
        assert client.post("/api/chats/%s/review" % chat["id"], json={"model": "rev:m1"}, headers=headers).status_code == 400
        stream_turn(client, headers, chat["id"], "where is the peak?", "main:m1")
        assert client.post("/api/chats/%s/review" % chat["id"], json={"model": "nope:x"}, headers=headers).status_code == 400
        r = client.post("/api/chats/%s/review" % chat["id"], json={"model": "rev:m1"}, headers=headers).json()
        assert r["verdict"] == "partly" and r["label"] == "Rev m1" and "no uncertainty" in r["text"]
        assert "where is the peak?" in rev.prompts[0][0] and "The peak is at 1.5 V." in rev.prompts[0][0]
        assert store.load(chat["id"])["messages"][-1]["review"]["verdict"] == "partly"

        stream_turn(client, headers, chat["id"], "what do you think?", "main:m1")
        seen = main.prompts[-1][-1]
        assert seen.startswith('<second_opinion model="Rev m1" verdict="partly">') and seen.endswith("what do you think?")
        stream_turn(client, headers, chat["id"], "ok", "main:m1")
        assert main.prompts[-1][-1] == "ok"                              # given once, not with every message
        assert main.prompts[-1][-2].startswith("<second_opinion")       # and replayed unchanged
