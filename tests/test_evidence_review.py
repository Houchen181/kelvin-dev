"""Second opinions that check the evidence: the reviewer looks things up in the lab's records (read-only tools only),
reports findings with how serious and how sure they are, only sure ones are passed on, and another model can
re-check them."""
import json

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")

from labharness.web import review  # noqa: E402
from test_compare import LINE, bot, say, tool  # noqa: E402
from test_web import client_for, human, parts, stream_turn  # noqa: E402,F401


def verdict(v, *findings, checked=()):
    return say(json.dumps({"verdict": v, "findings": [
        {"problem": p, "severity": s, "confidence": c, "evidence": e} for p, s, c, e in findings],
        "checked": list(checked)}))


def last_run(runtime):
    return runtime.pick_runs(last=1)[-1]


def test_the_reviewer_checks_the_records_and_passes_on_only_what_it_is_sure_of(parts):
    runtime, settings, store = parts
    assert "done" in runtime.run(LINE)
    run_id = last_run(runtime)
    main = bot("main", [say("Run %s: the signal peaks at 3 mm, 12 µV." % run_id), say("Fair.")])
    rev = bot("rev", [
        tool("get_run_data", {"run_id": run_id}, "r1") + tool("set_value", {"instrument": "smu", "parameter": "V",
                                                                           "value": 9}, "r2"),
        verdict("partly", ("the peak is at 1 mm, not 3 mm", "high", 0.9, "get_run_data: largest lockin.X at 1 mm"),
                ("the lock-in may have been overloaded", "medium", 0.4, "not checked"),
                checked=["the run ID"])])
    client, headers = client_for(parts, {"main": main, "rev": rev})
    with client:
        chat = client.post("/api/chats", json={"model": "main:m1"}, headers=headers).json()
        stream_turn(client, headers, chat["id"], "where is the peak?", "main:m1")
        r = client.post("/api/chats/%s/review" % chat["id"], json={"model": "rev:m1"}, headers=headers).json()
    assert r["verdict"] == "partly" and r["lookups"] == 2 and r["checked"] == ["the run ID"]
    assert [f["confidence"] for f in r["findings"]] == [0.9, 0.4]
    assert "peak is at 1 mm" in r["text"] and "overloaded" not in r["text"] and "1 less certain note" in r["text"]
    looked = rev.seen[1]                                                   # what came back to the reviewer
    assert looked == ["user", "assistant", "tool"]
    assert runtime.lab.read("smu", "V") == 0                               # set_value was refused, nothing changed
    assert "set_value" not in rev.tool_names[0]
    saved = store.load(chat["id"])["messages"][-1]["review"]
    assert saved["findings"][0]["problem"] == "the peak is at 1 mm, not 3 mm"


def test_a_stop_needs_a_sure_serious_finding(parts):
    runtime, settings, store = parts
    settings.update({"reviewer": "rev:m1"})
    main = bot("main", [tool("run_plan", {"plan": LINE}), say("Ran it.")])
    rev = bot("rev", [verdict("stop", ("the stage might not be homed", "high", 0.5, "could not check"))])
    client, headers = client_for(parts, {"main": main, "rev": rev})
    with client:
        chat = client.post("/api/chats", json={"model": "main:m1"}, headers=headers).json()
        events = stream_turn(client, headers, chat["id"], "scan", "main:m1")
    opinion = next(e for e in events if e["type"] == "review")
    assert opinion["verdict"] == "caution"                                 # a suspicion: advice, not a stop
    assert not [e for e in events if e["type"] == "approval"] and len(runtime.pick_runs(last=5)) == 1


@pytest.mark.parametrize("answer", [False])
def test_a_checked_stop_asks_the_person_with_its_evidence(parts, answer):
    runtime, settings, store = parts
    settings.update({"reviewer": "rev:m1"})
    main = bot("main", [tool("run_plan", {"plan": LINE}), say("Ok.")])
    rev = bot("rev", [tool("describe_instrument", {"instrument": "stage"}, "d1"),
                      verdict("stop", ("the scan runs past the stage's travel", "high", 0.9,
                                       "describe_instrument: position limit 3 mm"))])
    client, headers = client_for(parts, {"main": main, "rev": rev})
    with client:
        chat = client.post("/api/chats", json={"model": "main:m1"}, headers=headers).json()
        human(runtime.approver, [answer])
        events = stream_turn(client, headers, chat["id"], "scan", "main:m1")
    ask = next(e for e in events if e["type"] == "approval")
    assert ask["items"] == ["the scan runs past the stage's travel (describe_instrument: position limit 3 mm)"]
    assert next(e for e in events if e["type"] == "review")["lookups"] == 1 and runtime.pick_runs(last=5) == []


def test_another_model_rechecks_the_findings(parts):
    runtime, settings, store = parts
    main = bot("main", [say("The fit gives a width of 0.2 mm.")])
    rev = bot("rev", [verdict("disagree", ("the width is 0.4 mm", "high", 0.75, "compute: fwhm 0.4 mm"),
                              ("no uncertainty is given", "medium", 0.6, ""))])
    other = bot("other", [tool("list_runs", {}, "o1"), say(json.dumps({"results": [
        {"finding": 1, "holds": False, "evidence": "compute: fwhm 0.2 mm"},
        {"finding": 2, "holds": True, "evidence": "the answer gives none"}]}))])
    client, headers = client_for(parts, {"main": main, "rev": rev, "other": other})
    with client:
        chat = client.post("/api/chats", json={"model": "main:m1"}, headers=headers).json()
        stream_turn(client, headers, chat["id"], "how wide is it?", "main:m1")
        first = client.post("/api/chats/%s/review" % chat["id"], json={"model": "rev:m1"}, headers=headers).json()
        assert first["verdict"] == "disagree"
        same = client.post("/api/chats/%s/review" % chat["id"], json={"model": "rev:m1", "recheck": True},
                           headers=headers)
        assert same.status_code == 400 and "different model" in same.json()["error"]
        r = client.post("/api/chats/%s/review" % chat["id"], json={"model": "other:m1", "recheck": True},
                        headers=headers).json()
    assert r["rechecked"] == "Other m1" and r["verdict"] == "partly"       # its only serious finding fell
    kept, dropped = r["findings"]
    assert kept["problem"] == "no uncertainty is given" and kept["confidence"] == 0.9 and kept["recheck"]["holds"]
    assert dropped["refuted"]["evidence"] == "compute: fwhm 0.2 mm"
    assert "no uncertainty" in r["text"] and "0.4 mm" not in r["text"] and "confirmed by Other m1" in r["text"]
    assert "The findings to check:\n1. [high, 75% sure] the width is 0.4 mm" in other.prompts[0][0]
    assert store.load(chat["id"])["messages"][-1]["review"]["rechecked"] == "Other m1"


def test_reading_the_reviewers_json():
    fenced = 'Here:\n```json\n{"verdict": "agree", "findings": [], "checked": ["the {units}", "a \\"quote\\""]}\n```'
    r = review.findings_of(fenced, "answer")
    assert r["verdict"] == "agree" and r["checked"] == ["the {units}", 'a "quote"'] and r["text"].startswith("Checked")
    assert review.findings_of("verdict: stop\n- too much current", "plan")["verdict"] == "stop"   # words only
    odd = review.findings_of('{"verdict": "sure", "findings": [{"problem": "x", "severity": "huge", "confidence": "?"}]}',
                             "answer")
    assert odd["verdict"] == "partly" and odd["findings"][0]["severity"] == "medium" and \
        odd["findings"][0]["confidence"] == 0.5
