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
