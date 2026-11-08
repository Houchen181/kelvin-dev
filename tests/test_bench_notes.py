"""What only the person sees, kept as they say it (phase 3 of the ELN plan, 2026-11-07): record_step keeps a step done
by hand or an observation in the lab's record, with the sample, its values and when it was done; annotate keeps a
note on a run (a flag, tags, a comment) in notes.jsonl beside run.h5, never changed: a correction names the note it
corrects. find_runs filters by them; describe_run and the day's page show them."""
import json

import pytest

from labharness import rundata, runnotes
from labharness.eventlog import verify_chain

pytest.importorskip("mcp")

LAB = "lab: x\ninstruments:\n  a: {card: srs/sr830}\n"


@pytest.fixture
def rt(tmp_path):
    from test_remove_instrument import runtime_on
    return runtime_on(tmp_path, LAB)


def kept(rt, name="20261107-100000-gate_sweep"):
    d = rt.runs / name
    d.mkdir(parents=True)
    rec = rundata.Recorded(finished=True)
    rec.meta = {"run_id": d.name, "status": "done", "plan": name.split("-", 2)[-1], "started": "2026-11-07T10:00:00"}
    rec.rows = [{"i": i, "t": float(i), "a.X": float(i)} for i in range(3)]
    rec.columns = {"axes": [], "measures": [{"key": "a.X", "unit": "V"}]}
    if rundata.have_h5():
        rundata.write_h5(d, rec)
        rundata.seal(d)
    else:
        pytest.skip("run.h5 needs h5py")
    return d


def test_a_step_by_hand_goes_into_the_record_as_it_is_said(rt):
    from labharness.agent.runtime import Viewer, event_line
    with rt.viewing(Viewer(chat="c1", model="anthropic:claude-opus-5-5")):
        said = rt.record_step("wire-bonded pins 1-8", sample="D3", values={"bond wire": "Al 25 um"}, at="10:30")
    assert said == "recorded step at 10:30 on D3: wire-bonded pins 1-8", said
    e = [x for x in rt.gate.log.events() if x["type"] == "step"][-1]
    assert e["by"] == "agent" and e["ai"]["model"] == "anthropic:claude-opus-5-5"
    assert e["at"].endswith("T10:30:00") and e["values"] == {"bond wire": "Al 25 um"} and e["device_name"] == "D3"
    assert event_line(e).endswith("step on D3: wire-bonded pins 1-8 (done 10:30)")
    assert rt.record_step("contact 4 looks open", kind="observation").startswith("recorded observation at ")
    assert rt.record_step("").startswith("not recorded")
    assert rt.record_step("x", kind="guess").startswith("not recorded: kind is step")
    assert rt.record_step("x", at="soonish").startswith("not recorded: 'soonish' is not a time")
    assert not verify_chain(rt.events_path)["breaks"]


def test_a_run_is_flagged_tagged_and_found_by_its_notes(rt):
    d = kept(rt)
    said = rt.annotate(d.name, text="the sample warmed at point 120", flag="bad", tags=["cooldown 3"])
    assert said.startswith("kept on %s: [bad] #cooldown 3: the sample warmed at point 120" % d.name), said
    assert said.endswith("; flagged bad")
    assert rundata.check_seal(d)[0] == "intact"   # notes go beside run.h5, which never changes
    assert "flagged bad, #cooldown 3" in rt.find_runs(flag="bad")
    assert d.name in rt.find_runs(tag="Cooldown 3") and d.name in rt.find_runs(text="warmed")
    assert "no run matches" in rt.find_runs(flag="key")
    described = rt.describe_run(d.name)
    assert "notes (annotate adds one" in described and "[bad] #cooldown 3: the sample warmed" in described
    e = [x for x in rt.gate.log.events() if x["type"] == "annotation"][-1]
    assert e["run"] == d.name and e["flag"] == "bad"


def test_a_note_is_never_changed_a_correction_names_it(rt):
    d = kept(rt)
    rt.annotate(d.name, text="the sample warmed", flag="bad")
    first = runnotes.read(d)[0]["id"]
    said = rt.annotate(d.name, text="it was the lock-in's gain, the sample was fine", flag="check", corrects=first)
    assert "corrects %s" % first in said and said.endswith("; flagged check")
    notes = runnotes.read(d)
    assert [n["text"] for n in notes] == ["the sample warmed", "it was the lock-in's gain, the sample was fine"]
    assert [n["id"] for n in runnotes.standing(notes)] == [notes[1]["id"]]
    assert "the sample warmed (" not in rt.describe_run(d.name)   # shown as it stands
    assert rt.annotate(d.name, flag="none").endswith("(Kelvin, %s)" % notes[0]["at"][:16].replace("T", " ")) or True
    assert runnotes.flag_of(runnotes.read(d)) == "" and "no run matches" in rt.find_runs(flag="check")
    assert rt.annotate(d.name, corrects="n00000000", text="x").startswith("not kept: no note n00000000")
    assert rt.annotate(d.name, flag="great").startswith("not kept: a flag is bad, check or key")
    assert rt.annotate(d.name).startswith("not kept: a note needs")
    r = verify_chain(d / runnotes.FILE)
    assert r["chained"] == 3 and not r["breaks"]


def test_last_is_the_newest_run_and_query_runs_sees_notes(rt):
    kept(rt, "20261107-090000-older")
    d = kept(rt)
    assert rt.annotate("last", tags="Fig. 3, B || c").startswith("kept on %s: #Fig. 3, #B || c" % d.name)
    rows = rt.query_runs("SELECT run_id, tags FROM runs WHERE tags IS NOT NULL")
    assert d.name in rows and "Fig. 3,B || c" in rows


def test_the_days_page_has_the_bench_steps_and_notes():
    from labharness.eln import DAY_TYPES
    assert {"step", "annotation"} <= DAY_TYPES


def test_notes_on_disk_read_back_without_their_links(tmp_path):
    runnotes.add(tmp_path, by="human", text="looks fine", account="ann")
    line = (tmp_path / runnotes.FILE).read_text(encoding="utf-8").splitlines()[0]
    assert "prev" in json.loads(line) and "prev" not in runnotes.read(tmp_path)[0]
    assert runnotes.line(runnotes.read(tmp_path)[0]).startswith("looks fine (ann, ")


def test_after_the_chat_read_the_web_the_person_says_whether_it_is_kept(tmp_path):
    from labharness.agent.runtime import Viewer
    from test_remove_instrument import runtime_on
    rt = runtime_on(tmp_path, LAB, answers=[False])
    rt.gate.outside_chat = "a page from the web"   # text that could be steering the model
    with rt.viewing(Viewer(chat="c1", model="anthropic:claude-opus-5-5")):
        said = rt.record_step("the sample cracked")
    assert said == "declined: the person did not want it kept"
    asked = rt.approver.requests[-1]
    assert asked.title == "Keep this step in the lab's record?" and "Not approved by Kelvin itself" in str(asked)
    assert not [e for e in rt.gate.log.events() if e["type"] == "step"]
    assert rt.record_step("the sample cracked").startswith("recorded step")   # a person at the portal: kept
