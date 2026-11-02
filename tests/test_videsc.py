"""A VI's full description (videsc.py) for the AI that translates it: every node, terminal, constant and wire, every
frame of every case, every call by kind, the lab's own sub-VIs found anywhere under the folder; and the names Kelvin
gives LabVIEW's heap classes and functions. Read from public VIs (tests/fixtures/labview, SOURCES.txt); never run."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

from labharness import labview, vifile, videsc, vigraph, viworker

FIXTURES = Path(__file__).parent / "fixtures" / "labview"
KEYSIGHT = FIXTURES / "keysight_u2722"
JKI = FIXTURES / "jki_state_machine" / "Three Button Dialog CORE - JKI.vi"


def fixture_vis():
    return sorted(FIXTURES.rglob("*.vi"))


# ------------------------------------------------------------------ names of heap classes and functions

def test_every_node_on_the_fixtures_diagrams_has_a_known_kind():
    for path in fixture_vis():
        vi = vifile.read(path)
        for node, _ in vigraph.Graph(vi).nodes():
            assert not node.kind.startswith("class"), (path.name, node.kind)


def test_every_terminals_data_object_on_the_fixtures_has_a_known_kind():
    for path in fixture_vis():
        g = vigraph.Graph(vifile.read(path))
        for dco in g.term_dco.values():
            assert not dco.kind.startswith("class"), (path.name, dco.kind)


# Functions the census of the fixtures and a lab's VIs found (tools/vi_census.py), each name confirmed by lvkit's
# clean-room table of LabVIEW's functions and by the terminals the function has on those VIs
NAMED_IN_TASK_1 = {
    1054: "Absolute Value", 1055: "Round To Nearest", 1056: "Quotient & Remainder", 1058: "Decrement",
    1061: "And", 1062: "Or", 1064: "Not", 1076: "Round Toward +Infinity", 1077: "Path To String",
    1078: "String To Path", 1103: "Greater Or Equal?", 1104: "Less Or Equal?", 1105: "Not Equal?",
    1108: "Max & Min", 1110: "Greater?", 1111: "Less?", 1112: "Empty String/Path?", 1113: "Equal To 0?",
    1114: "Greater Or Equal To 0?", 1115: "Less Or Equal To 0?", 1116: "Not Equal To 0?",
    1128: "Not A Number/Path/Refnum?", 1142: "To Long Integer", 1144: "To Unsigned Word Integer",
    1145: "To Unsigned Long Integer", 1147: "To Double Precision Float", 1155: "To Quad Integer",
    1169: "Cluster To Array", 1170: "Split Number", 1171: "Join Numbers", 1180: "Number To Decimal String",
    1183: "Number To Engineering String", 1184: "Decimal String To Number", 1187: "Fract/Exp String To Number",
    1207: "Inverse Cosine", 1419: "Build Path", 1516: "Select", 1609: "Byte Array To String",
    1900: "Reverse 1D Array", 1902: "Transpose 2D Array", 1903: "Add Array Elements", 2076: "Unregister For Events",
    2308: "VISA Open", 3913: "Replace Substring", 8003: "Variant To Data", 8011: "Close Reference",
    8016: "To More Specific Class", 8017: "To More Generic Class", 8050: "Open/Create/Replace File",
    8051: "Read from Binary File", 8052: "Close File", 8055: "Create Folder", 8056: "Delete",
    8073: "Set File Position", 8201: "To Variant", 9108: "Obtain Queue", 9111: "Enqueue Element"}


def test_the_primitives_the_census_found_have_their_labview_names():
    for rid, name in NAMED_IN_TASK_1.items():
        assert vigraph.PRIMS.get(rid) == name, rid


def test_string_length_is_named_for_its_one_string_in_and_number_out():
    # 1502 has a string in (1) and a number out (0) on every VI that uses it: String Length, not Decimal String To
    # Number (1184, with its offset and default)
    assert vigraph.PRIMS[1502] == "String Length"


def test_a_local_variable_is_named_as_one():
    jki = vifile.read(JKI)
    kinds = {n.kind for n, _ in vigraph.Graph(jki).nodes()}
    assert "gRef" in kinds and vifile.CLASSES[50] == "gRef" and vifile.CLASSES[103] == "gRefDCO"


# ------------------------------------------------------------------ the lab's sub-VIs, anywhere under the folder

KEYSIGHT_DRIVER_FILES = [p.relative_to(FIXTURES).as_posix() for p in (KEYSIGHT / "Agilent U2722").rglob("*.vi")]


def lay_out(tmp_path, places):
    """Copies fixture files into tmp_path at new places: [(fixture's relative path, new relative path)]."""
    for src, dst in places:
        target = tmp_path / dst
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((FIXTURES / src).read_bytes())


def calls_of(summary):
    return [n for n in vigraph.walk(summary["diagram"]) if n.get("node") == "call"]


def driver_moved(prefix):
    """The Agilent driver's files, each under prefix instead of beside the VI."""
    return [(p, prefix + p.split("/", 1)[1]) for p in KEYSIGHT_DRIVER_FILES]


def test_a_drivers_vis_in_another_subfolder_are_found_and_their_commands_read(tmp_path):
    before = {c["vi"]: bool(c["sends"]) for c in calls_of(vigraph.summary(KEYSIGHT / "SMU_IV single.vi"))}
    assert any(before.values())
    lay_out(tmp_path, [("keysight_u2722/SMU_IV single.vi", "SMU_IV single.vi")] + driver_moved("deep/a/b/"))
    after = {c["vi"]: bool(c["sends"]) for c in calls_of(vigraph.summary(tmp_path / "SMU_IV single.vi"))}
    assert after == before


def test_two_sub_vis_of_one_name_in_different_places_are_never_guessed_between(tmp_path):
    lay_out(tmp_path, [("keysight_u2722/SMU_IV single.vi", "top/SMU_IV single.vi")] + driver_moved("x/") +
            driver_moved("y/"))
    calls = calls_of(vigraph.summary(tmp_path / "top" / "SMU_IV single.vi", folder=tmp_path))
    output = [c for c in calls if c["vi"].endswith("Configure Output.vi")]
    assert output and not any(c["sends"] for c in output)
    assert all("more than one place" in c.get("sends_unread", "") for c in output)


def test_a_sub_vi_found_by_name_is_still_never_outside_the_folder(tmp_path):
    lay_out(tmp_path, [("keysight_u2722/SMU_IV single.vi", "lab/SMU_IV single.vi")] + driver_moved("outside/"))
    try:
        os.symlink(tmp_path / "outside", tmp_path / "lab" / "linked", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links can't be made here")
    calls = calls_of(vigraph.summary(tmp_path / "lab" / "SMU_IV single.vi"))
    assert not any(c["sends"] for c in calls)


# ------------------------------------------------------------------ the description: panel, nodes, wires, frames

READS = ("no", "names", "panel", "full")


def nodes(diagram):
    """Every node of a description's diagram, those inside its structures' frames too."""
    out = []
    for n in (diagram or {}).get("nodes", []):
        out.append(n)
        for f in n.get("frames", []):
            out += nodes(f)
    return out


def diagrams(desc):
    """The description's diagram and every frame inside it."""
    out, todo = [], [desc["diagram"]] if desc.get("diagram") else []
    while todo:
        d = todo.pop()
        out.append(d)
        for n in d["nodes"]:
            todo += n.get("frames", [])
    return out


def all_terms(desc):
    """Every terminal id the description gives: nodes' terminals, sources, structures' tunnels and own terminals."""
    ids = set()
    for d in diagrams(desc):
        ids |= {s["id"] for s in d["sources"]}
        for n in d["nodes"]:
            ids |= {t["id"] for t in n["terms"]}
            for t in n.get("tunnels", []):
                ids |= {t["outer"], *t["inner"], *t.get("inits", []), t.get("out")}
            ids |= {n[k] for k in ("count", "index", "stop", "selector", "timeout") if n.get(k)}
    return ids


def test_every_fixture_vi_is_described_without_an_error():
    for path in fixture_vis():
        d = videsc.describe(path)
        assert d["description"] == 1 and d["read"] in READS and isinstance(d["unread"], list), path.name
        assert len(d["sha256"]) == 64
        json.dumps(d, ensure_ascii=False, allow_nan=False)


def test_the_panel_has_every_control_the_summary_has_with_ids():
    d, s = videsc.describe(KEYSIGHT / "SMU_IV single.vi"), vigraph.summary(KEYSIGHT / "SMU_IV single.vi")
    assert [c["name"] for c in d["panel"]] == [c["name"] for c in s["controls"]]
    assert all(c["id"].startswith("c") for c in d["panel"])


def test_every_wire_joins_terminals_the_description_has():
    for path in fixture_vis():
        d = videsc.describe(path)
        ids = all_terms(d)
        for diagram in diagrams(d):
            for w in diagram["wires"]:
                assert w["from"] in ids and all(t in ids for t in w["to"]), (path.name, w)


def test_loops_and_calls_match_the_summary():
    d, s = videsc.describe(KEYSIGHT / "SMU_IV single.vi"), vigraph.summary(KEYSIGHT / "SMU_IV single.vi")
    assert sum(n["kind"] == "loop" for n in nodes(d["diagram"])) == \
        sum(n.get("node") == "loop" for n in vigraph.walk(s["diagram"]))
    assert {n["call"] for n in nodes(d["diagram"]) if n["kind"] == "call"} >= \
        {n["vi"] for n in vigraph.walk(s["diagram"]) if n.get("node") == "call"}


def test_a_state_machines_case_has_every_frame_with_its_values():
    d = videsc.describe(JKI)
    vi = vifile.read(JKI)
    reader = vigraph._Reader(vi, vigraph._link, 0, {})
    expected = []
    for n, _ in reader.g.nodes():
        if n.kind == "select":
            expected += [tuple(v) for v in reader.case_names(n)]
    labels = [tuple(f["label"]) for c in nodes(d["diagram"]) if c["kind"] == "case" for f in c["frames"]]
    assert expected and sorted(labels) == sorted(expected)


def test_an_event_structures_frames_say_which_control_they_wait_for():
    d = videsc.describe(JKI)
    events = [n for n in nodes(d["diagram"]) if n["kind"] == "event"]
    labels = {l for e in events for f in e["frames"] for l in f["label"]}
    assert {"an event of Left Button (Value Change)", "an event of Center Button (Value Change)",
            "an event of Right Button (Value Change)"} <= labels
    # the frame that waits for the timeout, and the one this VI's own event (a filter: Discard? decides) runs
    assert "timeout" in labels and any(l.startswith("an event of this VI (a filter") for l in labels)


def test_a_loops_shift_register_is_one_tunnel_with_its_ends():
    d = videsc.describe(JKI)   # its states go round in a shift register
    loops = [n for n in nodes(d["diagram"]) if n["kind"] == "loop"]
    registers = [t for loop in loops for t in loop["tunnels"] if t["kind"] == "shift_register"]
    assert registers and all("side" not in t for t in registers)   # left and right halves are one register
    for t in registers:   # its value going in, read each step, written each step, and coming out
        assert t["read"] and t["write"] and t["inner"] == t["read"] + [t["write"]]
        assert len(t["inits"]) == len(t["read"]) and t["outer"] == t["inits"][0] and "out" in t


def test_a_control_is_live_exactly_when_it_is_read_inside_a_loop():
    for path in fixture_vis():
        d = videsc.describe(path)
        in_loops = {s["control"] for loop in nodes(d["diagram"]) if loop["kind"] == "loop"
                    for f in loop["frames"] for sub in [f] + [g for n in nodes(f) for g in n.get("frames", [])]
                    for s in sub["sources"] if s.get("control")}
        for c in d["panel"]:
            assert c["live"] == (not c["indicator"] and c["id"] in in_loops), (path.name, c["name"])


def test_a_local_variable_is_the_control_it_names():
    d = videsc.describe(JKI)
    locals_ = [s for g in diagrams(d) for s in g["sources"] if s.get("local")]
    message = next(c["id"] for c in d["panel"] if c["name"] == "message")
    assert locals_ and all(s["control"] == message for s in locals_)


def test_a_primitive_is_named_with_its_terminals_numbers():
    d = videsc.describe(KEYSIGHT / "SMU_IV single.vi")
    prims = [n for n in nodes(d["diagram"]) if n["kind"] == "prim"]
    assert prims and all(n["known"] == (n["name"] != "") for n in prims)
    assert all("parm" in t for n in prims for t in n["terms"])


def test_a_vi_too_big_to_follow_is_described_as_far_as_it_was_read(monkeypatch):
    monkeypatch.setattr(vigraph, "MAX_STEPS", 500)
    d = videsc.describe(JKI)
    assert d["read"] != "no" and d["panel"] and any("bigger than Kelvin follows" in u for u in d["unread"])


def test_a_locked_diagram_is_described_as_the_summary_says(tmp_path):
    import hashlib
    raw = (FIXTURES / "aim_tti_cpx400sp" / "Public" / "set_v.vi").read_bytes()
    (tmp_path / "locked.vi").write_bytes(raw.replace(hashlib.md5(b"").digest(), hashlib.md5(b"secret").digest()))
    d, s = videsc.describe(tmp_path / "locked.vi"), vigraph.summary(tmp_path / "locked.vi")
    assert (d["read"], d["unread"], d["diagram"]) == (s["read"], s["unread"], None)


def test_a_file_that_isnt_a_vi_is_described_as_not_read(tmp_path):
    (tmp_path / "text.vi").write_text("not a VI", encoding="utf-8")
    d = videsc.describe(tmp_path / "text.vi")
    assert d["read"] == "no" and d["description"] == 1 and d["diagram"] is None


# ------------------------------------------------------------------ calls, and the lab's own sub-VIs

def test_a_drivers_calls_are_classified_with_its_maker_and_the_commands_it_sends():
    d = videsc.describe(KEYSIGHT / "SMU_IV single.vi")
    drivers = [c for c in d["calls"].values() if c["kind"] == "driver"]
    assert drivers and any(c["sends"] for c in drivers)
    assert all(c["maker"] for c in drivers)
    sites = [n for n in nodes(d["diagram"]) if n["kind"] == "call" and d["calls"][n["call"]]["kind"] == "driver"]
    assert any(n["sends"] for n in sites)   # each call's own commands, with the values it gives


def test_labviews_own_vis_are_named_never_described():
    for path in fixture_vis():
        d = videsc.describe(path)
        assert set(d["calls"]) >= {n["call"] for n in nodes(d["diagram"]) if n["kind"] == "call"}
        for key, c in d["calls"].items():
            assert c["kind"] in ("own", "driver", "vilib", "labview", "class", "dll", "unknown")
            if c["kind"] in ("vilib", "labview"):
                assert key not in d["subvis"]


def test_the_labs_own_sub_vis_are_described_in_full():
    d = videsc.describe(KEYSIGHT / "SMU_IV single.vi", link=vigraph._link)   # no maker known: the lab's own
    own = {k for k, c in d["calls"].items() if c["kind"] == "own"}
    assert own and set(d["subvis"]) == own
    for key in own:
        sub = d["subvis"][key]
        assert sub["description"] == 1 and sub["diagram"]["nodes"] and d["calls"][key]["found"]
        assert sub["subvis"] == {}   # every sub-VI at any depth is under the top description's subvis


def test_a_sub_vi_called_many_times_is_described_once(monkeypatch):
    seen = []
    real = videsc._describe

    def counting(vi, *args, **kwargs):
        seen.append(vi.path)
        return real(vi, *args, **kwargs)
    monkeypatch.setattr(videsc, "_describe", counting)
    videsc.describe(KEYSIGHT / "SMU_IV single.vi", link=vigraph._link)
    assert len(seen) == len(set(seen)) > 1


# ------------------------------------------------------------------ the description as text, for a model

def test_each_node_is_written_once_with_its_id():
    for path in fixture_vis():
        d = videsc.describe(path)
        lines = [line.lstrip() for line in videsc.render_text(d).splitlines()]
        for n in nodes(d["diagram"]):
            assert sum(line.startswith(n["id"] + " ") for line in lines) == 1, (path.name, n["id"])


def test_every_control_is_listed_and_error_wiring_is_left_out():
    d = videsc.describe(KEYSIGHT / "SMU_IV single.vi")
    text = videsc.render_text(d)
    assert all(c["id"] in text for c in d["panel"])
    assert "error in" not in text.lower() and "error out" not in text.lower()


def test_the_text_is_much_smaller_than_the_description():
    d = videsc.describe(KEYSIGHT / "SMU_IV single.vi")
    assert len(videsc.render_text(d)) < len(json.dumps(d)) / 2


def test_every_wired_input_says_where_its_value_comes_from():
    d = videsc.describe(KEYSIGHT / "SMU_IV single.vi")
    text = videsc.render_text(d)
    assert "=?" not in text and "<- ?" not in text   # every wire's source is found
    loop = next(n for n in nodes(d["diagram"]) if n["kind"] == "loop")
    assert any(line.lstrip().startswith(loop["id"] + " while loop") for line in text.splitlines())


def test_a_sub_vis_ids_are_its_own_in_the_text():
    d = videsc.describe(KEYSIGHT / "SMU_IV single.vi", link=vigraph._link)
    text = videsc.render_text(d)
    lines = [line.lstrip() for line in text.splitlines()]
    for i, (key, sub) in enumerate(d["subvis"].items(), 1):
        assert 'sub-VI s%d "%s"' % (i, key) in text
        for n in nodes(sub["diagram"]):
            assert sum(line.startswith("s%d.%s " % (i, n["id"])) for line in lines) == 1


def test_a_binary_functions_operands_are_named_x_and_y():
    # parm 2 is x and parm 1 is y (x - y, x / y, x > y), as vigraph's arithmetic reads them and lvkit's table has it
    lines = [line for path in fixture_vis() for line in videsc.render_text(videsc.describe(path)).splitlines()
             if any(" = %s(" % f in line for f in ("Add", "Subtract", "Multiply", "Divide", "Greater?", "Less?"))]
    assert lines and all("x=" in line and "y=" in line for line in lines if "p1=" not in line), lines[:3]
    assert not any("p1=" in line or "p2=" in line for line in lines)


def test_a_structures_inputs_say_which_terminal_they_enter():
    text = videsc.render_text(videsc.describe(KEYSIGHT / "SMU_IV single.vi"))
    ins = [line.strip()[len("in: "):] for line in text.splitlines() if line.strip().startswith("in: ")]
    assert ins and all(part.split("=", 1)[0].startswith("t") for line in ins for part in line.split(", "))


# ------------------------------------------------------------------ described in the reader's own process

PYTHON = getattr(sys, "_base_executable", "") or sys.executable


def test_a_description_is_read_in_the_readers_own_process():
    out, notes = viworker.describe_all([str(KEYSIGHT / "SMU_IV single.vi")], KEYSIGHT)
    here = videsc.describe(str(KEYSIGHT / "SMU_IV single.vi"), folder=KEYSIGHT)
    assert notes == [] and out[0] == json.loads(json.dumps(here))
    assert out[0]["read"] == "full" and out[0]["calls"]


def test_a_file_that_hangs_the_reader_is_said_and_the_next_is_still_described(tmp_path, monkeypatch):
    from test_vi_reader_process import FAKE
    script = tmp_path / "fake_reader.py"
    script.write_text(FAKE, encoding="utf-8")
    monkeypatch.setattr(viworker, "command", lambda: [PYTHON if Path(PYTHON).is_file() else sys.executable, "-I",
                                                      str(script)])
    monkeypatch.setattr(viworker, "FILE_SECONDS", 1.0)
    out, _ = viworker.describe_all([str(tmp_path / n) for n in ("a.vi", "slow.vi", "b.vi")], tmp_path)
    assert all(d["description"] == 1 for d in out)
    assert [d["unread"][0] for d in out] == [
        "keys: []", "not read: it took too long to read (more than 1 s): Kelvin stopped reading it", "keys: []"]


def test_a_description_too_big_to_say_moves_on_to_the_next_file(tmp_path, monkeypatch):
    monkeypatch.setattr(viworker, "MAX_LINE", 20_000)   # SMU_IV's is ~50 kB, Loop Counter 2's ~3 kB
    paths = [str(KEYSIGHT / "SMU_IV single.vi"), str(FIXTURES / "svs4od" / "Loop Counter 2.vi")]
    started = []
    real = viworker.command
    monkeypatch.setattr(viworker, "command", lambda: started.append(1) or real())
    results, _ = viworker.read_all([("describe", p) for p in paths], FIXTURES)
    assert started == [1] and [len(r) for r in results] == [1, 1]   # one reader, one answer each: never restarted
    assert "bigger than Kelvin keeps" in results[0][0]["unread"][0] and results[1][0]["read"] == "full"


def test_labview_describes_one_vi_never_in_kelvins_own_folder():
    d = labview.describe_vi(KEYSIGHT / "SMU_IV single.vi")
    assert d["description"] == 1 and d["read"] == "full"
    with pytest.raises(ValueError):
        labview.describe_vi(KEYSIGHT / "SMU_IV single.vi", own=[KEYSIGHT])


# ------------------------------------------------------------------ the final review's findings

def tunnel_kinds(path):
    d = videsc.describe(path)
    return {t["outer"]: t["kind"] for n in nodes(d["diagram"]) for t in n.get("tunnels", [])}


def test_only_a_tunnel_that_takes_an_arrays_elements_one_by_one_is_indexing():
    # LabVIEW marks an auto-indexing tunnel in its flags (0x01000000); a plain tunnel into a For loop has the same
    # inner data object without it (RemoveDuplicates_D t1444, a number; SMU_IV t2745, an array passed whole)
    remove = tunnel_kinds(FIXTURES / "labolabo2009" / "RemoveDuplicates_D.vi")
    assert (remove["t1053"], remove["t1349"], remove["t1444"]) == ("indexing", "indexing", "tunnel")
    smu = tunnel_kinds(KEYSIGHT / "SMU_IV single.vi")
    assert (smu["t6008"], smu["t792"], smu["t2745"]) == ("indexing", "indexing", "tunnel")
    vi = vifile.read(FIXTURES / "labolabo2009" / "RemoveDuplicates_D.vi")
    g = vigraph.Graph(vi)
    plain = next(g.term_dco[t] for t in g.term_dco if t == 1444)
    assert not vigraph.indexing(plain)


def test_a_same_named_vi_of_another_library_is_never_taken_for_a_sub_vi(tmp_path):
    # the Agilent driver's Configure Output.vi isn't there, but another library's VI of that name is, where the
    # link's folders lead: it is never read in its place (it would put another instrument's commands in the VI)
    lay_out(tmp_path, [("keysight_u2722/SMU_IV single.vi", "top/SMU_IV single.vi"),
                       ("aim_tti_cpx400sp/Public/set_v.vi", "x/Agilent U2722/Public/Configure/Configure Output.vi")])
    calls = calls_of(vigraph.summary(tmp_path / "top" / "SMU_IV single.vi", folder=tmp_path))
    output = [c for c in calls if c["vi"].endswith("Configure Output.vi")]
    assert output and not any(c["sends"] for c in output)


def test_a_vi_of_no_library_is_found_only_where_its_folders_match(tmp_path):
    # a lab's own reset.vi saved in "Pump probe/New_PP program.llb": a reset.vi elsewhere, in folders that share
    # nothing with that, or one in a driver's library, is not it
    for rel in ("drivers/Keithley/Reset.vi", "Pump probe/reset.vi"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"")
    libraries = {"drivers/Keithley/Reset.vi": "Keithley 24XX.lvlib", "Pump probe/reset.vi": ""}
    shared = vigraph._Shared(folder=str(tmp_path))

    def library_of(p):
        return libraries[p.relative_to(tmp_path).as_posix()]
    assert shared.find(["", "Elsewhere", "New_PP program.llb", "reset.vi"], "", library_of) is None
    assert shared.find(["..", "Pump probe", "reset.vi"], "", library_of) == (tmp_path / "Pump probe" / "reset.vi")
    libraries["Pump probe/reset.vi"] = "Other.lvlib"
    assert shared.find(["..", "Pump probe", "reset.vi"], "", library_of) is None


def labs_vis():
    """A lab's own VIs a developer has to test with (KELVIN_LABVIEW_SAMPLES: their folder; never in the repository)."""
    folder = os.environ.get("KELVIN_LABVIEW_SAMPLES")
    if not folder or not Path(folder).is_dir():
        pytest.skip("no folder of a lab's VIs (KELVIN_LABVIEW_SAMPLES)")
    return sorted(Path(folder).rglob("*.vi"))[:400]


def test_every_wire_joins_terminals_the_description_has_on_a_labs_own_vis():
    # stacked sequences' locals and a For loop's count inside it are on the lab's VIs, not the fixtures
    bad = []
    for path in labs_vis():
        d = videsc.describe(path)
        assert " None | inside" not in videsc.render_text(d)   # written as text too; a local has no outside
        ids = all_terms(d)
        bad += [(path.name, t) for g in diagrams(d) for w in g["wires"] for t in [w["from"]] + w["to"]
                if t not in ids]
    assert not bad, bad[:5]


def test_a_shift_register_is_one_line_with_its_roles_in_the_text():
    lines = [line.strip() for line in videsc.render_text(videsc.describe(JKI)).splitlines()
             if line.strip().startswith("shift_register")]
    assert lines and all(" read " in line and " write " in line and " out " in line for line in lines)


def test_bundles_are_named_by_their_shape_with_their_fields():
    d = videsc.describe(JKI)
    fn = [n for n in nodes(d["diagram"]) if n["kind"] == "function"]
    wired = lambda n, way: [t for t in n["terms"] if t["dir"] == way and not t["error"]]
    by_name = {n["name"] for n in fn}
    assert {"Bundle", "Unbundle", "Bundle By Name", "Unbundle By Name"} <= by_name
    assert not any(n["kind"] == "other" and n.get("what") in ("mux", "nMux") for n in nodes(d["diagram"]))
    for n in fn:
        if n["name"] == "Unbundle By Name":
            assert len(wired(n, "in")) == 1
        if n["name"] == "Bundle By Name":
            assert len(wired(n, "in")) >= 2 and len(wired(n, "out")) == 1
    fields = {t["name"] for n in fn if n["name"] == "Unbundle By Name" for t in wired(n, "out")}
    assert "C Message Height" in fields


def test_a_named_functions_terminals_carry_their_labview_names():
    d = videsc.describe(JKI)
    selects = [n for n in nodes(d["diagram"]) if n["kind"] == "prim" and n["name"] == "Select"]
    assert selects and all({t["parm"]: t["name"] for t in n["terms"]} ==
                           {0: "result", 1: "f_value", 2: "selector", 3: "t_value"} for n in selects)
    for path in fixture_vis():   # no function Kelvin names is left with numbered inputs in the text
        for line in videsc.render_text(videsc.describe(path)).splitlines():
            if " = function#" not in line and "call " not in line:
                assert "(p1=" not in line and ", p2=" not in line, (path.name, line)


def test_the_text_names_what_calls_give_the_selectors_value_inside_and_unresolved_locals():
    text = videsc.render_text(videsc.describe(KEYSIGHT / "SMU_IV single.vi"))
    assert any('call "' in line and " -> " in line and "=t" in line.split(" -> ", 1)[1] for line in text.splitlines())
    jki = videsc.render_text(videsc.describe(JKI))
    assert "selector value | inside" in jki
    desc = {"name": "x.vi", "labview": "", "panel": [], "calls": {}, "unread": [], "unknown": {}, "subvis": {},
            "diagram": {"id": "d1", "label": [], "nodes": [
                {"id": "n2", "kind": "prim", "name": "Not", "prim": 1064, "known": True,
                 "terms": [{"id": "t3", "dir": "in", "type": "bool", "name": "x", "error": False, "parm": 1},
                           {"id": "t4", "dir": "out", "type": "bool", "name": "", "error": False, "parm": 0}]}],
                "sources": [{"id": "t1", "local": True, "control": None, "named": "Gate (V)"}],
                "wires": [{"from": "t1", "to": ["t3"]}]}}
    assert 'x=local "Gate (V)"' in videsc.render_text(desc)


def test_the_folders_vis_are_looked_for_once_however_many_vis_need_them(tmp_path, monkeypatch):
    lay_out(tmp_path, [("keysight_u2722/SMU_IV single.vi", "a/SMU_IV single.vi"),
                       ("keysight_u2722/SMU_IV single.vi", "b/SMU_IV single.vi")] + driver_moved("deep/"))
    walks = []
    real = vigraph._walk_vis
    monkeypatch.setattr(vigraph, "_walk_vis", lambda *a: walks.append(a) or real(*a))
    for vi in ("a", "b"):
        vigraph.summary(tmp_path / vi / "SMU_IV single.vi", folder=tmp_path)
    assert len(walks) == 1


def test_the_search_never_looks_in_hidden_or_tool_folders(tmp_path):
    for skipped in (".old/", "node_modules/"):   # the driver only there, once
        lab = tmp_path / skipped.strip("/").strip(".")
        lay_out(lab, [("keysight_u2722/SMU_IV single.vi", "SMU_IV single.vi")] + driver_moved(skipped))
        calls = calls_of(vigraph.summary(lab / "SMU_IV single.vi"))
        assert not any(c["sends"] for c in calls), skipped


def test_the_search_stops_after_so_many_files_and_folders(tmp_path, monkeypatch):
    for i in range(30):
        (tmp_path / ("f%02d" % i)).mkdir()
    monkeypatch.setattr(vigraph, "INDEX_LOOKED", 10)
    index, stopped = vigraph._walk_vis(str(tmp_path), ())
    assert stopped and index == {}


def test_the_same_folder_named_two_ways_finds_the_same_sub_vis(tmp_path, monkeypatch):
    lay_out(tmp_path, [("keysight_u2722/SMU_IV single.vi", "SMU_IV single.vi")] + driver_moved("deep/"))
    monkeypatch.chdir(tmp_path.parent)
    named = [Path(tmp_path.name), tmp_path]   # relative, then absolute
    found = [any(c["sends"] for c in calls_of(vigraph.summary(f / "SMU_IV single.vi", folder=f))) for f in named]
    assert found == [True, True]


def test_a_description_cut_short_says_so_in_read_and_in_its_text(monkeypatch):
    monkeypatch.setattr(vigraph, "MAX_STEPS", 6000)
    d = videsc.describe(JKI)
    assert d["read"] == "panel" and any("bigger than Kelvin follows" in u for u in d["unread"])
    many = dict(d, unread=["note %d" % i for i in range(12)] + d["unread"])
    assert "bigger than Kelvin follows" in videsc.render_text(many)


def test_an_array_indicator_written_inside_a_loop_is_plotted():
    check_arrays_written_in_loops_are_plotted(fixture_vis())


def test_an_array_indicator_written_inside_a_loop_is_plotted_on_a_labs_own_vis():
    check_arrays_written_in_loops_are_plotted(labs_vis())   # plain arrays, not graphs: the fixtures have none


def check_arrays_written_in_loops_are_plotted(paths):
    seen = 0
    for path in paths:
        d = videsc.describe(path)
        sinks = {t for g in diagrams(d) for w in g["wires"] for t in w["to"]}
        written = {s["control"] for loop in nodes(d["diagram"]) if loop["kind"] == "loop"
                   for f in loop["frames"] for sub in [f] + [g for n in nodes(f) for g in n.get("frames", [])]
                   for s in sub["sources"] if s.get("control") and s["id"] in sinks}
        for c in d["panel"]:
            if c["indicator"] and c["type"] == "array" and c["id"] in written:
                seen += 1
                assert c["plotted"], (path.name, c["name"])
    assert seen


# ------------------------------------------------------------------ what a translation must not get wrong (Plan 3)

CLASS = {v: k for k, v in vifile.CLASSES.items()}


def _obj(name, value=None, kids=(), uid=None):
    """A heap object as the reader builds it: a field by its tag name, or an element of a list."""
    tag = vifile.SYS_ELEMENT if name == "element" else vifile.TAG[name]
    return vifile.Obj(tag=tag, value=value, kids=list(kids), uid=uid)


def lTst_obj(flags):
    o = _obj("loopTestDCO", kids=[_obj("objFlags", flags.to_bytes(3, "big"))])
    o.cls = CLASS["lTst"]
    return o


def loop_with(test):
    loop = vifile.Obj(tag=vifile.SYS_ELEMENT, cls=CLASS["whileLoop"], kids=[test])
    return loop


def cpd_obj(filler, flags):
    terms = []
    for i, f in enumerate(flags):
        dco = _obj("dco", kids=[_obj("objFlags", f.to_bytes(3, "big"))] +
                   ([_obj("dcoFiller", filler.to_bytes(2, "big"))] if i == 0 else []))
        dco.cls = CLASS["fPDCO"]
        term = _obj("element", kids=[dco], uid=100 + i)
        term.cls = CLASS["term"]
        terms.append(term)
    return vifile.Obj(tag=vifile.SYS_ELEMENT, cls=CLASS["cpdArith"], kids=[_obj("termList", kids=terms)])


def test_a_while_loop_says_whether_it_stops_or_continues_on_true():
    d = videsc.describe(KEYSIGHT / "SMU_IV single.vi")
    loops = [n for n in nodes(d["diagram"]) if n["kind"] == "loop" and n["loop"] == "while"]
    assert loops and all(n["stops_on"] is True for n in loops)          # LabVIEW's default, as these are drawn
    assert "stops when" in videsc.render_text(d)


def test_the_polarity_bit_is_read_both_ways():
    assert videsc.stops_on_true(loop_with(lTst_obj(0x010001))) is True
    assert videsc.stops_on_true(loop_with(lTst_obj(0x01))) is False
    assert videsc.stops_on_true(vifile.Obj(tag=vifile.SYS_ELEMENT, cls=CLASS["forLoop"])) is None


def test_compound_arithmetic_says_its_operation_and_inverted_inputs():
    op, inverted = videsc.compound(cpd_obj(2, [0x10000, 0]))            # and, its first input inverted
    assert op == "and" and inverted == [100]
    assert videsc.compound(cpd_obj(256, [0, 0, 0x10000]))[0] == "add"
    assert videsc.compound(cpd_obj(7, [0, 0]))[0] == "code 7"           # never guessed
