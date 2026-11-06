"""A lab's VI translated into a Kelvin experiment (Plan 3): what each LabVIEW call becomes (vimap), the checks a
translation must pass (vicheck), and the translator's model call and repair rounds (vitranslate)."""
import time
from pathlib import Path

import pytest

from labharness import videsc, vimap
from labharness.homelab import builtin_registry

ROOT = Path(__file__).resolve().parents[1]
KEYSIGHT = ROOT / "tests" / "fixtures" / "labview" / "keysight_u2722"


@pytest.fixture
def registry():
    return builtin_registry()


# ------------------------------------------------------------------ what each LabVIEW call becomes (Task 2)

def test_a_named_call_maps_to_its_card_parameters():
    t = vimap.load()
    m = t.find("Keithley 24XX.lvlib:Enable Output.vi", {"kind": "driver"})
    assert (m.becomes, m.card, m.sets, m.source) == ("card", "keithley/2400", ["output"], "kelvin")
    assert t.find("MG17Motor.MoveAbsoluteRot", {}).card == "thorlabs/kdc101"


def test_the_labs_own_map_comes_first(tmp_path):
    own = tmp_path / "labview-map.yaml"
    own.write_text('- {call: "Time Delay", becomes: python, python: "kelvin_wait(s)"}\n', encoding="utf-8")
    assert vimap.load(own).find("Time Delay", {"kind": "unknown"}).python == "kelvin_wait(s)"
    assert vimap.load(own).find("Time Delay", {"kind": "unknown"}).source == "lab"


def test_a_bad_entry_in_the_labs_map_is_skipped_with_a_note(tmp_path):
    own = tmp_path / "labview-map.yaml"
    own.write_text('- {call: "X.vi", becomes: magic}\n- {call: "Time Delay", becomes: nothing, why: "w"}\n',
                   encoding="utf-8")
    t = vimap.load(own)
    assert t.find("X.vi", {}) is None and t.notes and t.find("Time Delay", {}).becomes == "nothing"


def test_a_driver_with_no_entry_is_matched_by_the_commands_it_sends(registry):
    sends = [{"text": "SOUR:VOLT {v}", "values": {}}, {"text": "OUTP {on}", "values": {}}]
    m = vimap.by_commands(sends, [registry.get("keithley/2400")])
    assert m.card == "keithley/2400" and set(m.sets) >= {"V", "output"} and m.source == "commands"
    assert vimap.by_commands([{"text": "FROB 3", "values": {}}], [registry.get("keithley/2400")]) is None


def test_every_call_and_object_node_of_a_vi_is_mapped_or_said_unmapped(registry):
    d = videsc.describe(KEYSIGHT / "SMU_IV single.vi")
    maps = vimap.map_description(d, vimap.load(), list(registry))
    assert set(maps) >= set(d["calls"])
    assert maps["Write Delimited Spreadsheet.vi"].becomes == "python"
    assert all(m.becomes in ("card", "python", "nothing", "unmapped") for m in maps.values())
    assert "Write Delimited Spreadsheet.vi" in vimap.text(maps)


def test_accepting_a_proposed_mapping_writes_the_labs_map(tmp_path):
    f = tmp_path / "labview-map.yaml"
    said = vimap.accept(f, [{"call": "Acme.lvlib:Arm.vi", "becomes": "card", "card": "acme/x", "does": ["arm"]}],
                        "ana")
    assert said and vimap.load(f).find("Acme.lvlib:Arm.vi", {}).card == "acme/x"
    assert vimap.accept(f, [{"call": "Bad.vi", "becomes": "card"}], "ana") == []    # no card: not taken


def test_the_map_ships_with_kelvin():
    spec = (ROOT / "packaging" / "kelvin.spec").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "map.yaml" in spec and '"data/labview/*.yaml"' in pyproject


# ------------------------------------------------------------------ the checker (Task 4)

from test_script_runs import LAB, _python, folder, lab, write  # noqa: E402,F401 (fixtures)

from labharness import scriptrun, vicheck  # noqa: E402
from labharness.clock import SimClock  # noqa: E402
from labharness.lab import Lab  # noqa: E402
from labharness.labconfig import load_lab_config  # noqa: E402
from labharness.sim.twin import World, sim_backend_factory  # noqa: E402

GOOD = '''
from kelvin import lab, action, record   # VI: n1
V_STOP = 1.0                             # VI: c10
@action("scan", main=True)
def scan():
    for v in [0.0, 0.5, V_STOP]:         # VI: n2
        lab.set("smu.V", v)              # VI: n3
        record(V=v, X=lab.get("lockin.X"))   # VI: n4, c11
'''
DESC = {"panel": [{"id": "c10", "name": "Stop (V)", "default": 1.0, "indicator": False, "type": "number"},
                  {"id": "c11", "name": "X", "indicator": True, "type": "number"}],
        "diagram": {"nodes": [{"id": "n3", "kind": "call", "call": "Smu.lvlib:Set V.vi", "terms": []},
                              {"id": "n4", "kind": "call", "call": "Lia.lvlib:Read X.vi", "terms": []}]},
        "calls": {"Smu.lvlib:Set V.vi": {"kind": "driver"}, "Lia.lvlib:Read X.vi": {"kind": "driver"}}}
MAPS = {"Smu.lvlib:Set V.vi": vimap.Mapping("Smu.lvlib:Set V.vi", "card", card="virtual/source-meter", sets=["V"]),
        "Lia.lvlib:Read X.vi": vimap.Mapping("Lia.lvlib:Read X.vi", "card", card="virtual/lock-in", gets=["X"])}
NOTES = {"inputs": {"V_STOP": "c10"}, "not_translated": [], "columns": {"X": "X"}}


def prepare_for(lab):
    return lambda path, action: scriptrun.prepare(lab, str(path), [path.parent], action=action,
                                                  choose_python=_python)


def checked(lab, folder, src, notes=NOTES, **kw):
    return vicheck.check(src, DESC, lab, MAPS, notes, write(folder, "t.py", src), lab.make_twin,
                         prepare=prepare_for(lab), **kw)


@pytest.fixture
def lab_two_smus():
    from labharness.homelab import builtin_registry as reg
    registry = reg()
    text = LAB + "  smu2:\n    card: virtual/source-meter\n    address: GPIB0::25::INSTR\n"
    clock = SimClock()
    world = World(clock, 0)
    return Lab(load_lab_config(text, registry), registry, sim_backend_factory(world), clock, world)


def test_a_faithful_translation_has_nothing_that_blocks(lab):
    assert [f for f in vicheck.static(GOOD, DESC, MAPS, lab, NOTES) if f.blocks] == []
    assert vicheck.tags(GOOD)["c11"] == [8] and vicheck.tags(GOOD)["n4"] == [8]


@pytest.mark.parametrize("bad, kind", [
    ("import os\n", "import"), ("from . import helper\n", "import"), ("open('x', 'w')\n", "call"),
    ("getattr(lab, 'set')('smu.V', 1)\n", "call"), ("__builtins__.__dict__['open']('x')\n", "call"),
    ("lab.set('smu.compliance_I', 0.1)\n", "drive"), ("V_STOP = 2.0\n", "default"), ("def f(:\n", "syntax")])
def test_what_breaks_a_safety_rule_blocks_keep(lab, bad, kind):
    found = vicheck.static(GOOD + bad, DESC, MAPS, lab, NOTES)
    assert any(f.blocks and f.kind == kind for f in found), found


def test_a_file_that_doesnt_use_kelvin_blocks(lab):
    assert any(f.blocks and f.kind == "api" for f in vicheck.static("x = 1\n", DESC, MAPS, lab, NOTES))


def test_a_node_the_translation_never_mentions_is_listed_not_dropped(lab):
    desc = dict(DESC, panel=DESC["panel"] + [{"id": "c99", "name": "Comment", "indicator": False, "type": "string"}])
    found = vicheck.static(GOOD, desc, MAPS, lab, NOTES)
    assert any(f.kind == "untranslated" and f.node == "c99" and not f.blocks for f in found)
    listed = dict(NOTES, not_translated=[{"id": "c99", "what": "a comment", "why": "nothing to do"}])
    assert not any(f.node == "c99" for f in vicheck.static(GOOD, desc, MAPS, lab, listed))


def test_drives_are_judged_per_instrument_by_its_card(lab_two_smus):
    src = GOOD.replace('"smu.V"', '"smu2.V"')                 # the same card: still the VI's drive
    assert not [f for f in vicheck.static(src, DESC, MAPS, lab_two_smus, NOTES) if f.blocks]


def test_an_instrument_the_lab_lacks_is_a_warning_and_the_rehearsal_says_why(lab, folder):
    r = checked(lab, folder, GOOD.replace('"lockin.X"', '"scope.X"'))
    assert any(f.kind == "instrument" and not f.blocks for f in r.findings), r.findings
    assert any(f.kind == "rehearsal" for f in r.findings), r.findings


def test_each_action_is_rehearsed_and_a_faithful_one_is_clean(lab, folder):
    r = checked(lab, folder, GOOD)
    assert r.ok_to_keep and r.rehearsals["scan"]["ok"], r.text()
    assert not [f for f in r.findings if f.kind in ("rehearsal", "columns")], r.findings


def test_a_limit_break_in_the_rehearsal_blocks(lab, folder):
    r = checked(lab, folder, GOOD.replace("[0.0, 0.5, V_STOP]", "[0.0, 500.0]"), dict(NOTES, inputs={}))
    assert not r.ok_to_keep and any(f.kind == "rehearsal" and f.blocks for f in r.findings), r.text()


def test_an_action_that_never_ends_blocks(lab, folder, monkeypatch):
    monkeypatch.setattr(vicheck, "REHEARSE_S", 2.0)
    src = GOOD.replace("    for v in", "    import time\n    while True:\n        try:\n            time.sleep(0.1)\n"
                                         "        except KeyboardInterrupt:\n            pass   # it ignores Stop\n"
                                         "    for v in")
    started = time.monotonic()
    r = checked(lab, folder, src)
    assert not r.ok_to_keep and any("never ends" in f.text and f.blocks for f in r.findings), r.text()
    # its waits after Stop are no clean-up: it is ended GRACE_S after it goes quiet, not at MAX_GRACE_S
    assert time.monotonic() - started < scriptrun.MAX_GRACE_S / 3


def test_a_missing_recorded_column_is_a_warning(lab, folder):
    r = checked(lab, folder, GOOD, dict(NOTES, columns={"X": "X", "Y": "Y"}))
    assert r.ok_to_keep and any(f.kind == "columns" and "Y" in f.text for f in r.findings), r.findings


def test_the_trace_is_compared_with_the_vis_scan(lab, folder):
    plan = "plan: s\nsweep: { smu.V: {from: 0, to: 1, step: 0.5} }\nmeasure: [lockin.X]\n"
    r = checked(lab, folder, GOOD, plan_text=plan)
    assert r.trace.startswith("matches the VI's scan"), r.trace
    r = checked(lab, folder, GOOD.replace("[0.0, 0.5, V_STOP]", "[0.0, 0.5]"), plan_text=plan)   # stops short
    assert r.trace.startswith("differs from the VI's scan at command"), r.trace
    assert any(f.kind == "trace" and not f.blocks for f in r.findings)


def test_the_second_opinion_is_a_warning_and_a_broken_one_says_so():
    ask = lambda s, p: ('{"findings": [{"problem": "the stop button is inverted", "severity": "high"}]}', "m")  # noqa
    found = vicheck.review(ask, "VI ...", GOOD)
    assert found[0].kind == "review" and not found[0].blocks and "inverted" in found[0].text

    def broken(system, prompt):
        raise ValueError("no model")
    assert "failed" in vicheck.review(broken, "VI ...", GOOD)[0].text


# ------------------------------------------------------------------ the translator (Task 5)

import json  # noqa: E402

from labharness import vitranslate  # noqa: E402

TABLE = vimap.Table([{"call": "Smu.lvlib:Set V.vi", "becomes": "card", "card": "virtual/source-meter", "sets": ["V"]},
                     {"call": "Lia.lvlib:Read X.vi", "becomes": "card", "card": "virtual/lock-in", "gets": ["X"]}])


def fenced(code, notes):
    return "```python\n%s\n```\n```json\n%s\n```" % (code, json.dumps(notes))


def scripted(*answers):
    said = []

    def ask(system, prompt):
        said.append(prompt)
        return answers[min(len(said), len(answers)) - 1], "test:model"
    ask.said = said
    return ask


def translated(lab, folder, ask, **kw):
    return vitranslate.translate(DESC, lab, ask, TABLE, folder, lab.make_twin, prepare=prepare_for(lab), **kw)


def test_the_prompt_carries_the_description_the_map_and_the_lab(lab):
    p = vitranslate.prompt('VI "x.vi"\n...', '"Time Delay" -> time.sleep(seconds)', vitranslate.lab_text(lab))
    assert 'VI "x.vi"' in p and "time.sleep" in p and "smu" in p and "virtual/source-meter" in p
    assert "# VI:" in vitranslate.system() and "kelvin.lab" in vitranslate.system()


def test_a_good_answer_is_kept_after_one_round(lab, folder):
    t = translated(lab, folder, scripted(fenced(GOOD, NOTES)))
    assert t.rounds == 1 and t.report.ok_to_keep and t.source.strip() == GOOD.strip(), t.report.text()
    assert t.notes == NOTES and t.model == "test:model" and t.mappings["Smu.lvlib:Set V.vi"]["card"]


def test_a_broken_answer_is_repaired_with_kelvins_words(lab, folder):
    ask = scripted("no code here", fenced(GOOD + "import os\n", NOTES), fenced(GOOD, NOTES))
    t = translated(lab, folder, ask)
    assert t.rounds == 3 and t.report.ok_to_keep, t.report.text()
    assert "```python" in ask.said[1] and "imports os" in ask.said[2]


def test_after_its_rounds_the_last_attempt_comes_back_with_what_blocks(lab, folder):
    t = translated(lab, folder, scripted(fenced(GOOD + "import os\n", NOTES)))
    assert t.rounds == vitranslate.ROUNDS and not t.report.ok_to_keep


@pytest.mark.parametrize("answer", ["", "```python\ndef f(:\n```", "```python\nx = 1\n```\n```json\n{broken\n```",
                                    "```python\n" + GOOD[:60], "text\n```python\n" + GOOD + "\n```\n```python\nimport os\n```"])
def test_odd_answers_never_crash(lab, folder, answer):
    t = translated(lab, folder, scripted(answer))
    assert isinstance(t.report, vicheck.Report)            # the first python block counts; broken notes are {}


def test_the_second_opinion_comes_on_the_last_attempt_only(lab, folder):
    reviewed = scripted('{"findings": [{"problem": "a wait is missing", "severity": "low"}]}')
    t = translated(lab, folder, scripted(fenced(GOOD + "import os\n", NOTES), fenced(GOOD, NOTES)),
                   review_ask=reviewed)
    assert len(reviewed.said) == 1 and any(f.kind == "review" for f in t.report.findings)


def test_a_description_too_large_is_refused_before_the_model_hears_of_it(lab, folder, monkeypatch):
    monkeypatch.setattr(vitranslate, "MAX_PROMPT", 100)
    ask = scripted(fenced(GOOD, NOTES))
    with pytest.raises(ValueError, match="sub-VIs first"):
        translated(lab, folder, ask)
    assert ask.said == []


def test_the_translators_rules_ship_with_kelvin():
    assert "translate.md" in (ROOT / "packaging" / "kelvin.spec").read_text(encoding="utf-8")


# ------------------------------------------------------------------ the final review's findings (fix pass)

@pytest.mark.parametrize("bad", [
    "import numpy\nnumpy.ctypeslib.load_library('visa64', '.')\n",
    "from numpy import ctypeslib\n",
    "import numpy.ctypeslib\n",
    "import scipy\nscipy.LowLevelCallable\n",
    "g = getattr\n",
    "e = eval\n",
    "import datetime\nimport numpy\nT0 = datetime.datetime.now()\n"
    "if (datetime.datetime.now() - T0).total_seconds() > 1:\n    numpy.ctypeslib.load_library('visa64', '.')\n",
])
def test_what_reaches_past_the_gate_blocks_however_it_is_spelled(lab, bad):
    found = vicheck.static(GOOD + bad, DESC, MAPS, lab, NOTES)
    assert any(f.blocks for f in found), found


def test_a_raw_command_worked_out_as_it_runs_blocks(lab):
    src = GOOD + "from kelvin import live\n@action('r')\ndef r():\n    lab.raw('smu', ':SOUR:FUNC ' + live('m', 'CURR'))\n"
    assert any(f.blocks for f in vicheck.static(src, DESC, MAPS, lab, NOTES))


def test_a_key_kelvin_cant_read_ahead_blocks(lab):
    src = GOOD + "def put(key, v):\n    lab.set(key, v)\nput('smu.compliance_I', 0.1)\n"
    assert any(f.blocks for f in vicheck.static(src, DESC, MAPS, lab, NOTES))


def test_a_tagged_input_keeps_the_vis_value_whatever_the_notes_say(lab):
    src = GOOD.replace("V_STOP = 1.0", "V_STOP = 2.0")
    found = vicheck.static(src, DESC, MAPS, lab, dict(NOTES, inputs={}))
    assert any(f.blocks and f.kind == "default" for f in found), found


@pytest.mark.parametrize("problem, blocks", [
    ("refused: smu.V 500 V is outside its limits [-10, 10]", True),
    ("refused: 'SOUR:FUNC CURR' not sent; smu is held still while a sample is mounted", True),
    ("refused: the draft card's output asks a person", True),
    ("its process refused: ctypes.dlopen", True),
    ("refused: the lab has no instrument scope", False),
    ("refused: Kelvin's connection to smu is off (Lab panel)", False),
    ("refused: smu is not connected (no reply)", False),
    ("ZeroDivisionError: division by zero", False)])
def test_any_refusal_in_a_rehearsal_blocks_but_one_of_an_instrument_that_isnt_there(problem, blocks):
    assert vicheck._is_safety(problem) is blocks


def test_actions_are_rehearsed_in_order_on_one_copy_of_the_lab(lab, folder):
    src = ('from kelvin import lab, action   # VI: n1\n'
           '@action("initialize")\ndef initialize():\n    lab.set("smu.output", True)   # VI: n3\n'
           '@action("scan", main=True)\ndef scan():\n'
           '    if lab.get("smu.output") is not True:\n        raise RuntimeError("not initialized")\n'
           '    lab.set("smu.V", 0.5)   # VI: n3\n')
    maps = dict(MAPS, **{"Smu.lvlib:Set V.vi": vimap.Mapping("Smu.lvlib:Set V.vi", "card", card="virtual/source-meter",
                                                              sets=["V", "output"])})
    r = vicheck.check(src, DESC, lab, maps, dict(NOTES, columns={}), write(folder, "t.py", src), lab.make_twin,
                      prepare=prepare_for(lab))
    assert r.rehearsals["initialize"]["ok"] and r.rehearsals["scan"]["ok"], r.text()


# ------------------------------------------------------------------ the polish a customer would notice (Plan 4)

def test_the_size_refusal_says_what_it_counted(lab, folder, monkeypatch):
    monkeypatch.setattr(vitranslate, "MAX_PROMPT", 100)
    with pytest.raises(ValueError, match="the description and its context are"):
        translated(lab, folder, scripted(fenced(GOOD, NOTES)))


def test_the_notes_are_the_json_after_the_code():
    answer = ('An example of the notes:\n```json\n{"inputs": {"X": "c1"}}\n```\n```python\n' + GOOD + '\n```\n'
              '```json\n' + json.dumps(NOTES) + '\n```')
    source, notes = vitranslate.parse(answer)
    assert notes == NOTES and "V_STOP" in source
