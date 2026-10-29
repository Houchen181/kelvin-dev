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
    plan = "plan: gate\nsweep: { smu.V: {from: 0, to: 0.2, step: 0.1} }\nmeasure: [smu.I]\nafter: { smu.V: 0 }"
    ran, again = call(rt, ("run_plan", {"plan": plan}), ("run_plan", {"plan": plan}))
    assert "From the lab's memory, on smu:" in ran and "Ramp the gate to 0 V" in ran   # changed: shown again
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
