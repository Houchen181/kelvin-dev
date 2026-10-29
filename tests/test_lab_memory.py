"""The lab's memory (plan item 28): what people and Kelvin learned about a lab, kept for every chat, everyone who works
there and every agent: wiring, reference sources, excitations and lessons, each with who wrote it and when.

F2 l04: a new chat forgot that the mixing chamber is the LS350's channel A, and set the VNA's power before its RF
again; l06: chat2 assumed the lock-in's internal reference (it takes the chopper's), and chat3 wrote a night's queue
that set each OBIS's power before its emission, the order that had left both lasers at 0 W that morning; l01: a new
chat didn't know the excitation current. F3 #13: wiring, resistor and gains restated in every chat."""
import json

import pytest

from labsim_bench import labsim_bench  # noqa: F401  (the fixture)

from labharness import ScriptedApprover, build_virtual_lab
from labharness import labmemory as lm


def runtime(tmp_path, approver=None, lab=None):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    rt = LabRuntime(lab or build_virtual_lab(seed=1), approver or ScriptedApprover([]), tmp_path / "data", None)
    rt.lab_memory_dir = tmp_path / "Kelvin"   # Kelvin's own folder, as the app and the MCP server set it
    rt.report_delay = 3600
    return rt


def call(rt, *calls):
    """MCP tool calls on one session (one agent), in order: their texts."""
    import anyio
    from mcp import Client
    from labharness.agent.server import build_server

    async def go():
        out = []
        async with Client(build_server(rt)) as client:
            for name, args in calls:
                r = await client.call_tool(name, args)
                out.append(r.content[0].text)
        return out
    return anyio.run(go)


# ------------------------------------------------------------------ the notes themselves

def test_a_note_keeps_who_wrote_it_when_and_what_it_said_before(tmp_path):
    mem = lm.LabMemory(lm.path_for(tmp_path, virtual=False))
    note, what = mem.add("wiring", "The mixing chamber thermometer is  channel A of the LS350.", "mxc.T_A",
                         by="Kelvin", account="ada")
    assert what == "saved" and note["text"] == "The mixing chamber thermometer is channel A of the LS350."
    assert note["instruments"] == ["mxc"] and note["by"] == "Kelvin" and note["account"] == "ada"
    assert note["at"][:4].isdigit() and note["id"].startswith("m")
    assert "(Kelvin, in ada's chat, %s; id %s)" % (note["at"][:10], note["id"]) in lm.line(note)

    fixed = mem.change(note["id"], "human (phone Pixel)", "grace", text="The mixing chamber is channel B of the LS350.")
    assert fixed["text"].endswith("channel B of the LS350.") and fixed["edited"]["account"] == "grace"
    assert fixed["before"] == [{"kind": "wiring", "text": "The mixing chamber thermometer is channel A of the LS350.",
                                "instruments": ["mxc"], "by": "Kelvin", "account": "ada", "at": note["at"]}]
    assert "changed by grace (phone Pixel)" in lm.line(fixed)
    again = mem.change(note["id"], "human (portal)", None, kind="lesson")
    assert again["before"][0]["by"] == "human (phone Pixel)" and len(again["before"]) == 2   # who wrote each version
    assert lm.who("human (portal)") == "a person at the portal" and lm.who("agent") == "an AI agent over MCP"
    assert json.loads(mem.path.read_text(encoding="utf-8"))["notes"][0]["kind"] == "lesson"   # kept on disk at once
    gone = mem.remove(note["id"])
    assert gone["id"] == note["id"] and mem.notes() == []
    with pytest.raises(KeyError):
        mem.remove(note["id"])


def test_a_fact_is_kept_once_and_a_bad_note_is_refused_in_words(tmp_path):
    mem = lm.LabMemory(tmp_path / "lab-memory.json")
    first, _ = mem.add("lessons", "Switch each OBIS's emission on before its power.", ["pump", "probe"], by="Kelvin")
    assert first["kind"] == "lesson"
    assert mem.add("lesson", "switch each OBIS's emission on before its power", by="human (portal)") == (first, "known")
    other, how = mem.add("lesson", "Switch each OBIS's emission on before its power; wait 5 s.", by="Kelvin")
    assert how == "saved" and other["id"] != first["id"]
    fixed, how = mem.add("lesson", "Switch each OBIS's emission on, then set its power.", ["pump"], by="Kelvin",
                         replaces=first["id"])
    assert how == "updated" and fixed["id"] == first["id"] and fixed["before"][0]["instruments"] == ["pump", "probe"]
    with pytest.raises(lm.LabMemoryError, match="lesson .*wiring .*reference .*excitation .*fact"):
        mem.add("quirk", "The gate leaks above 6 V.")       # a device's quirk is the project's, not the lab's
    with pytest.raises(lm.LabMemoryError, match="at most 300 characters"):
        mem.add("fact", "x" * 301)
    with pytest.raises(lm.LabMemoryError, match="needs its text"):
        mem.add("fact", "   ")
    with pytest.raises(lm.LabMemoryError, match="not an instrument's name"):
        mem.add("fact", "Base temperature is 8 mK.", ["the fridge!"])
    with pytest.raises(KeyError):
        mem.add("fact", "Base temperature is 8 mK.", replaces="m00000000")
    for i in range(lm.MAX_NOTES - 2):
        mem.add("fact", "fact number %d about channel %d" % (i, i))
    with pytest.raises(lm.LabMemoryError, match="full"):
        mem.add("fact", "one too many")


def test_a_hand_edited_file_with_a_mistake_is_never_overwritten(tmp_path):
    path = tmp_path / "lab-memory.json"
    path.write_text('{"notes": [{"id": "m1", "kind": "lesson", "text": "Ramp slowly"},]}', encoding="utf-8")
    mem = lm.LabMemory(path)
    assert mem.notes() == []                                   # read: nothing, for the chats' briefs
    with pytest.raises(lm.LabMemoryError, match="can't be read"):
        mem.add("fact", "Base temperature is 8 mK.")           # write: refused, the file stays as the person left it
    assert path.read_text(encoding="utf-8").endswith(",]}")
    path.write_text(json.dumps({"notes": [{"id": "m1", "kind": "lesson", "text": "Ramp slowly"},
                                          {"kind": "odd"}, "text"]}), encoding="utf-8")
    assert [n["id"] for n in mem.notes()] == ["m1"]            # notes Kelvin can't use are left out, never lost
    mem.add("fact", "Base temperature is 8 mK.")
    assert len(json.loads(path.read_text(encoding="utf-8"))["notes"]) == 4


def test_the_brief_keeps_to_its_budget_with_lessons_first(tmp_path):
    mem = lm.LabMemory(tmp_path / "lab-memory.json")
    mem.add("fact", "The helium is filled on Mondays.")
    mem.add("wiring", "The magnet's quench line goes to the fridge's panel.", ["magnet"])
    mem.add("lesson", "The old 2400 jumped its range at 21 V.", ["gate2400"])       # no longer in this lab
    mem.add("lesson", "Ramp the magnet at 0.1 T/min or slower below 4 K.", ["magnet"])
    shown, left = lm.brief(mem.notes(), ["magnet", "tc"])
    assert left == 0 and [s.split(":")[0] for s in shown] == ["- lesson [magnet]", "- lesson [gate2400]",
                                                               "- wiring [magnet]", "- fact"]
    for i in range(40):
        mem.add("fact", "Cable %d runs from the breakout box to the preamplifier input %d." % (i, i))
    shown, left = lm.brief(mem.notes(), ["magnet"])
    assert sum(len(s) + 1 for s in shown) <= lm.BRIEF_CHARS and left == 44 - len(shown)
    assert shown[0].startswith("- lesson [magnet]") and shown[2].startswith("- wiring")   # the lessons always fit
    assert shown[3].startswith("- fact") and "Cable 39 " in shown[3]                       # then the newest


def test_the_virtual_lab_keeps_a_memory_of_its_own(tmp_path):
    rt = runtime(tmp_path)
    assert rt.lab_info["kind"] == "virtual"
    rt.keep_lab_note("lesson", "Practice note: the simulated magnet quenches above 9 T.", ["magnet"], by="Kelvin")
    assert (tmp_path / "Kelvin" / "virtual-lab-memory.json").exists()
    rt.lab_info = dict(rt.lab_info, kind="file")          # Kelvin controls the lab in lab.yaml now
    assert rt.lab_notes() == [] and rt.lab_memory_brief() == ""   # practising never writes into the real lab's
    stamp = rt.lab_memory_stamp()
    rt.keep_lab_note("wiring", "The lock-in's reference comes from the chopper.", ["lockin"], by="human (portal)")
    assert (tmp_path / "Kelvin" / "lab-memory.json").exists() and rt.lab_memory_stamp() != stamp


# ------------------------------------------------------------------ for every agent, through the MCP tools

CHAIN = {"excitation": {"source": "lockin.amplitude", "form": "rms", "series_resistor": 10e6},
         "readings": {"lockin.X": {"gain": 100, "name": "R_xx"}}}


def test_every_agent_starts_with_the_lab_memory_and_looks_up_the_rest(tmp_path):
    rt = runtime(tmp_path)
    rt.keep_lab_note("reference", "The lock-in takes its reference from the chopper (external, 1170 Hz).", ["lockin"],
                     by="Kelvin", account="ada")
    rt.keep_lab_note("lesson", "Switch the laser's emission on before its power.", ["laser"], by="human (portal)",
                     account="grace")
    rt.measurement_chain("setup", CHAIN)
    from labharness.agent.server import build_server
    instructions = build_server(rt).instructions
    assert "The lab's memory keeps what people and Kelvin learned" in " ".join(instructions.split())
    brief = instructions[instructions.index("# The lab's memory"):]
    assert brief.index("- lesson [laser]") < brief.index("- reference [lockin]")   # lessons first
    assert "(grace, " in brief and "(Kelvin, in ada's chat, " in brief and "remember_for_lab, replaces" in brief
    assert "Measurement chain (the setup's, recorded by agent" in brief and "lockin.X: gain 100" in brief
    looked, on_laser, words = call(rt, ("lab_memory", {}), ("lab_memory", {"instrument": "laser"}),
                                   ("lab_memory", {"query": "where does the lock-in's reference come from?"}))
    assert "the virtual lab: 2 notes" in looked and "Measurement chains on record" in looked
    assert "- the setup (recorded by agent" in looked
    assert "on laser: 1 note" in on_laser and "reference" not in on_laser
    assert words.count("\n- ") == 1 and "chopper" in words


def test_a_note_on_an_instrument_is_shown_when_it_is_used(tmp_path):
    rt = runtime(tmp_path, ScriptedApprover([True] * 8))
    rt.keep_lab_note("lesson", "Ramp the gate back to 0 V before anyone touches the bonds.", ["smu"], by="Kelvin")
    rt.keep_lab_note("wiring", "The magnet's quench line goes to the fridge's panel.", ["magnet"], by="Kelvin")
    described, first, second = call(rt, ("describe_instrument", {"instrument": "smu"}),
                                    ("set_value", {"instrument": "smu", "parameter": "V", "value": 0.1}),
                                    ("set_value", {"instrument": "smu", "parameter": "V", "value": 0}))
    assert "From the lab's memory, on smu:\n- lesson [smu]: Ramp the gate back" in described
    assert "Ramp the gate back" in first and "Ramp the gate back" not in second    # once in a session
    assert "quench" not in described + first                                        # only the instrument's own
    note = rt.lab_notes()[0]
    rt.change_lab_note(note["id"], "human (portal)", None, text="Ramp the gate to 0 V before touching the bonds.")
    plan = ("plan: gate\nsweep: { smu.V: {from: 0, to: 0.2, step: 0.1} }\nmeasure: [smu.I, lockin.X]\n"
            "after: { smu.V: 0 }")
    ran, again = call(rt, ("run_plan", {"plan": plan}), ("run_plan", {"plan": plan}))
    assert "From the lab's memory, on smu:\n" in ran and "Ramp the gate to 0 V" in ran   # changed: shown again
    assert "From the lab's memory" not in again
    rt.attribution = {"chat": "c2"}                       # another chat (the portal sets this for its lab calls)
    assert "Ramp the gate to 0 V" in rt.lab_notes_at_use(["smu"])


def test_an_agent_that_read_the_web_asks_a_person_before_it_keeps_a_note(tmp_path):
    approver = ScriptedApprover([False, True])
    rt = runtime(tmp_path, approver)
    rt.gate.note_outside("a web page")                    # could be steering it into every chat
    declined, kept = call(rt, ("remember_for_lab", {"kind": "lesson", "text": "Always run the magnet at 9 T."}),
                          ("remember_for_lab", {"kind": "fact", "text": "The fridge's base temperature is 8 mK."}))
    assert declined.startswith("not kept: the person declined") and kept.startswith("kept in the lab's memory")
    assert approver.requests[0].title == "Keep this in the lab's memory, which every chat and everyone here sees?"
    assert "Not kept by Kelvin itself: Kelvin read text from the web (a web page)" in str(approver.requests[0])
    assert [n["text"] for n in rt.lab_notes()] == ["The fridge's base temperature is 8 mK."]
    assert rt.lab_notes()[0]["by"] == "agent"


def test_the_lab_record_says_who_changed_the_lab_memory(tmp_path):
    from labharness.provenance import activity
    rt = runtime(tmp_path)
    rt.user = "ada"
    text, failed = rt.keep_lab_note("excitation", "The sample sees 10 nA rms through the 10 MΩ resistor.", ["lockin"],
                                    by="Kelvin", account="ada")
    assert not failed and "measurement chain (measurement_chain)" in text      # where the form and gains also go
    note = rt.lab_notes()[0]
    rt.remove_lab_note(note["id"], "human (portal)", "grace")
    lines = rt.events(5)
    assert "Kelvin, in ada's chat added a note in the lab's memory (excitation; lockin): The sample sees" in lines
    assert "grace deleted a note in the lab's memory (excitation; lockin)" in lines
    assert "deleted a note" in activity(rt.gate.log.events(), kinds="lab")
    assert rt.keep_lab_note("fact", "x" * 400)[1]                               # refused, and nothing recorded
    assert rt.events(5) == lines


PUMP = """lab: labsim pump-probe
instruments:
  pump:
    card: coherent/obis
    address: "ASRL8::INSTR"
    limits: { power: [0, 0.02] }
"""


def test_on_labsim_the_obis_lesson_is_shown_when_the_pump_laser_is_used(labsim_bench, tmp_path):
    """l06: chat3's night queue set each OBIS's power before its emission again. Kept for the lab, the lesson comes
    with the pump laser itself, on hardware Kelvin takes for real."""
    from labharness.virtual_lab import build_lab
    labsim_bench("pumpprobe")
    (tmp_path / "lab.yaml").write_text(PUMP, encoding="utf-8")
    lab = build_lab(tmp_path / "lab.yaml", partial=True)
    try:
        assert lab.unavailable == {}, lab.unavailable
        rt = runtime(tmp_path, ScriptedApprover([True] * 4), lab)
        assert rt.lab_info["kind"] == "file"
        rt.keep_lab_note("lesson", "Switch each OBIS's emission on before its power, or the power stays at 0 W.",
                         ["pump", "probe"], by="Kelvin", account="ada")
        described, on, power = call(rt, ("describe_instrument", {"instrument": "pump"}),
                                    ("set_value", {"instrument": "pump", "parameter": "emission", "value": "on"}),
                                    ("set_value", {"instrument": "pump", "parameter": "power", "value": 0.005}))
        assert "- lesson [pump, probe]: Switch each OBIS's emission on before its power" in described
        assert "emission" in on and "From the lab's memory, on pump:" in on
        assert "From the lab's memory" not in power and "0.005" in power.replace("5 mW", "0.005")
        assert (tmp_path / "Kelvin" / "lab-memory.json").exists()
    finally:
        lab.close()


# ------------------------------------------------------------------ in the portal's chats

KEEP_REFERENCE = [{"type": "text", "text": "Keeping that for the lab."},
                  {"type": "tool_call", "id": "k1", "name": "remember",
                   "input": {"scope": "lab", "kind": "reference", "instruments": ["lockin"],
                             "text": "The lock-in takes its reference from the chopper (external, 1170 Hz)."}}]
DONE = [{"type": "text", "text": "Done."}]


def portal(tmp_path, providers, approver=None):
    """The portal on the virtual lab, with Kelvin's own folder in tmp_path (where the app keeps the lab's memory)."""
    pytest.importorskip("starlette")
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from test_web import client_for
    parts = (LabRuntime(build_virtual_lab(seed=1), approver or WebApprover(timeout=5), tmp_path / "data"),
             Settings(tmp_path / "settings.json"), ChatStore(tmp_path / "chats"))
    client, headers = client_for(parts, providers)
    return parts[0], parts[2], client, headers


def test_a_fact_one_chat_keeps_for_the_lab_is_in_every_other_chat(tmp_path):
    """l06: chat1 had learned that the lock-in takes the chopper's reference; chat2 assumed the internal one."""
    from test_web import Scripted, stream_turn
    bot = Scripted([KEEP_REFERENCE, DONE, DONE, DONE])
    rt, store, client, headers = portal(tmp_path, {"scripted": bot})
    a, b = store.create_project("Pump-probe"), store.create_project("Another sample")
    with client:
        first = client.post("/api/chats", json={"model": "scripted:m1", "project": a["id"]}, headers=headers).json()
        events = stream_turn(client, headers, first["id"], "the lock-in's reference is the chopper", "scripted:m1")
        end = next(e for e in events if e["type"] == "tool_end")
        assert not end["is_error"] and end["content"].startswith("kept in the lab's memory, which every chat")
        assert bot.systems[0] == bot.systems[1] and "chopper" not in bot.systems[0]   # the turn's prompt holds
        assert {"remember", "lab_memory"} <= set(bot.tool_names[0]) and "remember_for_lab" not in bot.tool_names[0]
        second = client.post("/api/chats", json={"model": "scripted:m1", "project": b["id"]}, headers=headers).json()
        stream_turn(client, headers, second["id"], "set up a pump-probe scan", "scripted:m1")
        system = bot.systems[-1]
        assert "# The lab's memory" in system and "remember, scope lab, replaces: its id" in system
        assert "- reference [lockin]: The lock-in takes its reference from the chopper (external, 1170 Hz). " \
               "(Kelvin, " in system
        loose = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        stream_turn(client, headers, loose["id"], "hello", "scripted:m1")
        assert "from the chopper" in bot.systems[-1]                     # a chat in no project has it too
    note = rt.lab_notes()[0]
    assert (note["by"], note["account"], note["instruments"]) == ("Kelvin", None, ["lockin"])
    assert (tmp_path / "virtual-lab-memory.json").exists()                # Kelvin's own folder: no one person's


def test_unshared_work_and_incognito_chats_read_the_lab_memory_but_add_nothing(tmp_path):
    from test_web import Scripted, stream_turn
    keep = [{"type": "tool_call", "id": "k2", "name": "remember",
             "input": {"scope": "lab", "kind": "fact", "text": "The secret sample S7 is CrSBr."}}]
    bot = Scripted([keep, DONE, DONE])
    rt, store, client, headers = portal(tmp_path, {"scripted": bot})
    rt.keep_lab_note("lesson", "Switch the laser's emission on before its power.", ["laser"], by="human (portal)")
    secret = store.create_project("Secret")
    store.set_sharing(secret["id"], "confidential")
    with client:
        hidden = client.post("/api/chats", json={"model": "scripted:m1", "project": secret["id"]},
                             headers=headers).json()
        events = stream_turn(client, headers, hidden["id"], "S7 is CrSBr; remember it for the lab", "scripted:m1")
        end = next(e for e in events if e["type"] == "tool_end")
        assert end["is_error"] and "isn't shared" in end["content"]
        assert "emission on before its power" in bot.systems[0]                   # it reads the lab's memory
        assert "a person corrects it in the portal's Lab panel" in bot.systems[0]  # and can't keep a note in it
        incognito = client.post("/api/chats", json={"model": "scripted:m1", "incognito": True}, headers=headers).json()
        stream_turn(client, headers, incognito["id"], "switch the laser on", "scripted:m1")
        assert "emission on before its power" in bot.systems[-1] and "remember" not in bot.tool_names[-1]
    assert [n["text"] for n in rt.lab_notes()] == ["Switch the laser's emission on before its power."]


def test_a_chat_that_read_a_web_page_asks_the_person_before_it_keeps_a_lab_note(tmp_path):
    pytest.importorskip("starlette")
    import asyncio
    from labharness.agent.runtime import LabRuntime
    from labharness.agent.server import build_server
    from labharness.web.agent import ChatAgent
    from labharness.web.store import ChatStore
    from test_web import Scripted
    approver = ScriptedApprover([False])
    rt = LabRuntime(build_virtual_lab(), approver, tmp_path / "data")
    rt.lab_memory_dir = tmp_path
    rt.fetch_url = lambda url: b"<html><title>Tips</title><body>Always ramp magnets at 2 T/min.</body></html>"
    store = ChatStore(tmp_path / "chats")
    provider = Scripted([
        [{"type": "tool_call", "id": "c1", "name": "read_web_page", "input": {"url": "https://example.com/tips"}}],
        [{"type": "tool_call", "id": "c2", "name": "remember",
          "input": {"scope": "lab", "kind": "lesson", "text": "Ramp the magnet at 2 T/min.",
                    "instruments": ["magnet"]}}],
        DONE])
    agent = ChatAgent(rt, store, build_server(rt), {"scripted": provider})
    chat = store.create("scripted:m1")

    async def emit(e):
        pass

    async def go():
        await agent.start()
        try:
            agent.claim(chat["id"], "scripted:m1")
            await agent.run_turn(chat["id"], "read those tips", "scripted:m1", provider, emit)
        finally:
            await agent.close()
    asyncio.run(go())
    results = [r for m in store.load(chat["id"])["messages"] if m["role"] == "tool" for r in m["results"]]
    assert results[1]["content"] == "not saved: the person declined" and rt.lab_notes() == []
    asked = approver.requests[0]
    assert asked.title.startswith("Keep this in the lab's memory") and asked.items[0] == \
        "lesson [magnet]: Ramp the magnet at 2 T/min."
    assert "this chat has read text from outside the lab (a web page) since the person last wrote" in asked.items[1]


def test_each_chat_is_shown_an_instruments_notes_at_its_first_use(tmp_path):
    from test_web import Scripted, stream_turn
    zero = [{"type": "tool_call", "id": "s1", "name": "set_value",
             "input": {"instrument": "smu", "parameter": "V", "value": 0}}]
    bot = Scripted([zero, DONE, zero, DONE, zero, DONE])
    rt, store, client, headers = portal(tmp_path, {"scripted": bot})
    rt.keep_lab_note("lesson", "Ramp the gate to 0 V before anyone touches the bonds.", ["smu"], by="human (portal)")
    with client:
        one = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        two = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        said = []
        for chat in (one, one, two):
            events = stream_turn(client, headers, chat["id"], "gate to zero", "scripted:m1")
            said.append(next(e for e in events if e["type"] == "tool_end")["content"])
    assert "From the lab's memory, on smu:" in said[0] and "Ramp the gate to 0 V" in said[0]
    assert "From the lab's memory" not in said[1]                     # shown once in a chat
    assert "Ramp the gate to 0 V" in said[2]                           # and once in every other


# ------------------------------------------------------------------ people, in the portal's Lab panel

def test_people_keep_change_and_delete_notes_in_the_lab_panel(tmp_path):
    rt, store, client, headers = portal(tmp_path, {})
    rt.user = "ada"                                                    # who is signed in
    with client:
        empty = client.get("/api/lab-memory", headers=headers).json()
        assert empty["notes"] == [] and empty["virtual"] is True and "lesson" in empty["kinds"]
        stamp = client.get("/api/state", headers=headers).json()["lab_memory"]
        kept = client.post("/api/lab-memory", headers=headers, json={
            "kind": "wiring", "text": "The mixing chamber is channel A of the LS350.",
            "instruments": "tc, magnet"}).json()
        note = kept["notes"][0]
        assert (note["kind"], note["instruments"], note["who"]) == ("wiring", ["tc", "magnet"], "ada")
        assert client.get("/api/state", headers=headers).json()["lab_memory"] != stamp   # the panel reloads it
        again = client.post("/api/lab-memory", headers=headers, json={
            "kind": "wiring", "text": "the mixing chamber is channel A of the LS350"}).json()
        assert again["said"].startswith("already in") and len(again["notes"]) == 1
        bad = client.post("/api/lab-memory", headers=headers, json={"kind": "quirk", "text": "x"})
        assert bad.status_code == 400 and "lesson (what to do or avoid)" in bad.json()["error"]
        lesson = client.post("/api/lab-memory", headers=headers, json={
            "kind": "lesson", "text": "Ramp the magnet slowly below 4 K.", "instruments": ["magnet"]}).json()
        assert [n["kind"] for n in lesson["notes"]] == ["lesson", "wiring"]   # in the order the chats' brief has
        rt.user = "grace"
        changed = client.post("/api/lab-memory/" + note["id"], headers=headers, json={
            "text": "The mixing chamber is channel B of the LS350.", "instruments": ["tc"]}).json()
        now = next(n for n in changed["notes"] if n["id"] == note["id"])
        assert now["text"].endswith("channel B of the LS350.") and now["edited_who"] == "grace" and now["who"] == "ada"
        assert now["before"][0]["text"] == "The mixing chamber is channel A of the LS350."
        assert client.post("/api/lab-memory/m00000000", headers=headers, json={"text": "x"}).status_code == 404
        left = client.delete("/api/lab-memory/" + note["id"], headers=headers).json()["notes"]
        assert [n["kind"] for n in left] == ["lesson"]
        assert client.delete("/api/lab-memory/" + note["id"], headers=headers).status_code == 404
    lines = rt.events(10)
    assert "ada added a note in the lab's memory (wiring; tc, magnet)" in lines
    assert "grace changed a note in the lab's memory (wiring; tc): The mixing chamber is channel B" in lines
    assert "grace deleted a note" in lines


def test_the_lab_memory_is_the_computers_not_a_phones_and_not_claudes_while_it_operates(tmp_path):
    from labharness.web.remote import allowed
    for role in ("watch", "control"):
        for method in ("GET", "POST", "DELETE"):
            assert not allowed(role, method, "/api/lab-memory/m1") and not allowed(role, method, "/api/lab-memory")
    rt, store, client, headers = portal(tmp_path, {})
    with client:
        rt.operating = "Open LaserDesk"             # Claude works the screen: its clicks never edit the lab's memory
        r = client.post("/api/lab-memory", headers=headers, json={"kind": "fact", "text": "Base temperature 8 mK."})
        assert r.status_code == 423 and client.get("/api/lab-memory", headers=headers).status_code == 200
        rt.operating = None
    assert rt.lab_notes() == []


def test_an_approval_card_shows_the_labs_lessons_on_its_instruments(tmp_path):
    import threading
    from labharness.agent.approval import WebApprover
    from labharness.gate import ApprovalRequest
    approver = WebApprover(timeout=5)
    rt = runtime(tmp_path, approver)
    rt.keep_lab_note("lesson", "Switch the laser's emission on before its power.", ["laser"], by="human (portal)",
                     account="grace")
    rt.keep_lab_note("wiring", "The laser's fibre goes to the objective's back port.", ["laser"], by="Kelvin")
    rt.keep_lab_note("lesson", "Ramp the magnet slowly below 4 K.", ["magnet"], by="Kelvin")
    answers = []
    asking = threading.Thread(target=lambda: answers.append(approver.approve(
        ApprovalRequest("Set laser.power", ["laser.power: 0 → 5 mW (limit 20 mW)"]))))
    asking.start()
    entry = approver.wait_for_pending(timeout=5)
    assert entry["lessons"] == ["Switch the laser's emission on before its power. (grace, %s)"
                                % rt.lab_notes()[0]["at"][:10]]                # lessons only, on its instruments
    assert approver.pending_list()[0]["lessons"] == entry["lessons"]
    approver.answer(entry["id"], True)
    asking.join(5)
    assert answers == [True]
    assert rt.lab_lessons_for(ApprovalRequest("Install a package", ["numpy"])) == []
