"""Approval cards in the reader's language, translated as they are shown (F3 #25: approval cards stayed English in a
Chinese chat). The gate, the forecast and the record keep their English; a translation never loses a number, an
instrument's key or a sign, or the card shows as Kelvin wrote it."""
import re
import threading

import pytest

from labharness import (EventLog, RunEngine, RunStore, SafetyGate, ScriptedApprover, build_virtual_lab, cardlang,
                        demo_envelope)

COOL_AND_SWEEP = """
plan: cool_and_sweep
setup: { tc.setpoint: 4, smu.output: on, smu.compliance_I: 1.0e-6 }
sweep: { smu.V: {from: 0, to: 2, step: 0.5} }
settle: 1
measure: [smu.I, tc.T_sample]
after: { smu.V: 0 }
"""
ZH = "zh-Hans"
LATIN_WORD = re.compile(r"\b(?!(?:on|off|Kelvin)\b)[a-z]{4,}\b")   # an English word, not a value or unit
KEY = re.compile(r"[A-Za-z_][\w-]*\.\w+|\b(?:%s)\b" % "|".join(build_virtual_lab(seed=1).instruments))   # names


def cards(tmp_path, envelope=False):
    """Real cards: drive changes on the gate, a plan's card and a queue's, all declined."""
    out = []
    lab = build_virtual_lab(seed=1)
    approver = ScriptedApprover([False] * 10)
    gate = SafetyGate(lab, approver, EventLog(tmp_path / "e1.jsonl", lab.clock),
                      envelope=demo_envelope(lab) if envelope else None)
    for key, value in (("smu.V", 3), ("smu.output", "on"), ("magnet.B", 1.5), ("tc.setpoint", 50)):
        gate.set(*key.split("."), value)
    lab = build_virtual_lab(seed=1)
    approver2 = ScriptedApprover([False] * 10)
    gate2 = SafetyGate(lab, approver2, EventLog(tmp_path / "e2.jsonl", lab.clock), envelope=demo_envelope(lab))
    lab.instruments["tc"].write("heater_range", "low")
    lab.instruments["tc"].write("setpoint", 20.0)
    RunEngine(gate2, RunStore(tmp_path / "runs")).run(COOL_AND_SWEEP)
    from test_queue import FIELD, STEP, SWEEP, runtime
    rt = runtime(tmp_path / "q", [False] * 5)
    for item in (SWEEP, STEP, FIELD):
        rt.queue_add(item)
    rt.queue_start()
    return approver.requests + approver2.requests + rt.approver.requests


def test_the_cards_kelvin_asks_most_read_in_chinese(tmp_path):
    asked = cards(tmp_path)
    assert len(asked) >= 6
    for request in asked:
        shown = cardlang.card(request.title, request.items, "REAL HARDWARE", ZH)
        assert shown["english"] == 0, (request, shown)   # every line has its translation
        for en, zh in zip([request.title] + request.items, [shown["title"]] + shown["items"]):
            assert not LATIN_WORD.search(KEY.sub("", zh)), zh   # no English words left in it, keys apart
            assert sorted(re.findall(r"\d+(?:\.\d+)?", en)) == sorted(re.findall(r"\d+(?:\.\d+)?", zh)), (en, zh)
        assert shown["where"] == "真实硬件"
    first = cardlang.card(asked[0].title, asked[0].items, None, ZH)
    assert first["title"] == "批准 smu 的驱动变更？"
    assert first["items"][0] == "smu.V：0 V → 3 V；斜坡速率 0.5 V/s（约 6 s）；若 |smu.I| > 1e-09 A 则停止"
    plan = next(r for r in asked if r.title.startswith("Run plan"))
    text = "\n".join(cardlang.card(plan.title, plan.items, None, ZH)["items"])
    assert cardlang.line(plan.title, ZH, title=True) == "运行计划 cool_and_sweep：5 个点，至少约 13 s"
    assert "tc.setpoint：现为 20 K，计划用到 4 K，朝向其安全值" in text   # cooling, toward safe, is on it too
    assert "结束时保持：tc.setpoint 4 K、smu.output on 和 smu.compliance_I 1 µA；回到安全值：smu.V 0 V" in text
    assert "约 13 s 中未计入：tc.setpoint 的斜坡（它的仪器卡没有给出 Kelvin 能读取的速率）" in text


def test_a_translation_that_would_lose_a_number_shows_the_english(monkeypatch):
    line = "smu.V 0 V → 3 V; ramp 0.5 V/s (~6 s)"
    assert cardlang.line(line, ZH) != line
    parts = list(cardlang.LANGS[ZH]["parts"])
    for i, (pattern, template) in enumerate(parts):   # a template that drops the ramp's time
        if template.startswith("斜坡速率 {1} {2}/s（约"):
            parts[i] = (pattern, "斜坡速率 {1} {2}/s")
    monkeypatch.setitem(cardlang.LANGS[ZH], "parts", parts)
    assert cardlang.line(line, ZH) == line                       # shown as Kelvin wrote it, whole
    assert cardlang.line("smu.V 0 V → 3 V", ZH) == "smu.V：0 V → 3 V"   # a line it doesn't touch still turns


def test_what_has_no_translation_stays_english_whole():
    assert cardlang.line("Kelvin needs it: to read the sample", ZH) == "Kelvin 需要它：to read the sample"
    odd = "Something Kelvin has never said before, with 3 V in it"
    assert cardlang.line(odd, ZH) == odd
    assert cardlang.card("Approve drive change on smu?", ["x"], "SIMULATOR", "fr")["title"] == \
        "Approve drive change on smu?"   # a language Kelvin doesn't have: as written
    assert cardlang.shown({"title": "t", "items": []}, "") == {"title": "t", "items": []}
    assert cardlang.shown({"title": "Send raw command?", "items": []}, ZH)["shown"]["title"] == "发送原始命令？"
    calibration = "Calibration out of date: dmm was due 2026-09-30 (certificate C-1234)"
    assert cardlang.line(calibration, ZH) == "校准已过期：dmm 应于 2026-09-30 前校准（证书 C-1234）"
    ramp = "Run plan rt: measures every 10 s while ppms.T_set ramps to 2 K at 10 K/min, then 300 K (~1700 points)"
    assert cardlang.line(ramp, ZH, title=True) == \
        "运行计划 rt：在 ppms.T_set 变化到 2 K（速率 10 K/min），然后 300 K 期间每 10 s 测量一次（约 1700 个点）"
    assert cardlang.line("Something odd, then more", ZH) == "Something odd, then more"


def test_the_language_is_each_persons_and_only_one_kelvin_has(tmp_path):
    from labharness.web.settings import Settings
    s = Settings(tmp_path / "settings.json")
    assert s.public()["card_language"] == ""
    s.update({"card_language": "zh-Hans"})
    assert s.card_language == ZH and s.public()["card_language"] == ZH
    s.update({"card_language": "klingon"})
    assert s.card_language == ""


def test_the_portal_and_the_phone_show_the_card_translated_and_the_record_keeps_english(tmp_path):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    from starlette.testclient import TestClient
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, WebApprover(timeout=10), tmp_path / "data", None)
    settings = Settings(tmp_path / "settings.json")
    settings.update({"card_language": "zh-Hans"})
    client = TestClient(create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"], providers={}))
    headers = {"X-Kelvin-Token": settings.access_key}
    with client:
        t = threading.Thread(target=lambda: rt.gate.set("smu", "V", 3), daemon=True)
        t.start()
        entry = rt.approver.wait_for_pending(5)
        assert entry is not None
        pending = client.get("/api/state", headers=headers).json()["pending"]
        assert pending[0]["title"] == "Approve drive change on smu?"          # the card as Kelvin wrote it
        assert pending[0]["shown"]["title"] == "批准 smu 的驱动变更？"        # and as this person reads it
        assert pending[0]["shown"]["items"][0].startswith("smu.V：0 V → 3 V；斜坡速率 0.5 V/s")
        r = client.post("/api/cardtext", json={"title": entry["title"], "items": entry["items"],
                                               "where": entry["where"]}, headers=headers).json()
        assert r["title"] == "批准 smu 的驱动变更？" and r["where"] == "模拟器" and r["language"] == ZH
        assert client.post("/api/approvals/" + entry["id"], json={"approve": False}, headers=headers).status_code == 200
        t.join(5)
    asked = [e for e in rt.gate.log.events() if e["type"] == "ask"]
    assert asked and asked[-1]["request"].startswith("Approve drive change on smu?\n  - smu.V 0 V → 3 V")
    assert lab.read("smu", "V") == 0   # declined: nothing moved
