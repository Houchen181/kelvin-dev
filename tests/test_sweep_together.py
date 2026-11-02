"""A loop as Special Measure's loops are: settings that move with its own (with:), together a ramp step at a time; what
it reads once per value (its measure:), kept on every point inside; a longer hold after its first value
(first_settle:); variables of the plan's own and the settings sets: works out from them (Special Measure's function
channels), every value worked out first, checked and approved with the rest; and the lab's own names for settings
(the lab config's names:) in plans, put back as instrument.parameter."""
import pytest

from labharness import EventLog, RunEngine, RunStore, SafetyGate, ScriptedApprover, expr, load_plan, scanedit
from labharness.plans import PlanError, with_names
from simlab import build_virtual_lab

TWO_GATES = """
lab: two gates
instruments:
  top:
    card: virtual/source-meter
    limits: { V: [-10, 10] }
    rates: { V: 1 }
  bottom:
    card: virtual/source-meter
    limits: { V: [-10, 10] }
    rates: { V: 1 }
  lockin:
    card: virtual/lock-in
  tc:
    card: virtual/temperature-controller
names:
  Vtg: top.V
  Vbg: bottom.V
  X_A: lockin.X
"""


def _engine(tmp_path, answers=(True,), config=TWO_GATES):
    lab = build_virtual_lab(seed=1, config=config)
    approver = ScriptedApprover(answers)
    gate = SafetyGate(lab, approver, EventLog(tmp_path / "events.jsonl", lab.clock))
    return RunEngine(gate, RunStore(tmp_path / "runs")), lab, gate, approver


def test_a_loop_moves_settings_with_its_own_in_step_and_back_with_it():
    p = load_plan({"plan": "line", "sweep": {"stage.y": {"from": 70, "to": 72, "points": 5, "back": True,
                                                         "with": {"stage.x": {"from": 93, "to": 81}}}}})
    a = p.axes[0]
    assert a.values == [70, 70.5, 71, 71.5, 72, 71.5, 71, 70.5, 70]
    assert a.with_[0].key == "stage.x" and a.with_[0].values == [93, 90, 87, 84, 81, 84, 87, 90, 93]
    assert p.points == 9 and p.swept_settings() == [("stage.y", a.values), ("stage.x", a.with_[0].values)]
    listed = load_plan({"plan": "l", "sweep": {"a.b": {"values": [1, 2, 3], "with": {"c.d": [5, 6, 7]}}}})
    assert listed.axes[0].with_[0].values == [5, 6, 7] and listed.requires == ["a", "c"]
    for bad, why in ((dict(values=[1, 2], **{"with": {"c.d": [5]}}), "one value per value"),
                     (dict(values=[1, 1], **{"with": {"c.d": {"from": 0, "to": 1}}}), "give values: instead"),
                     (dict(values=[1, 2], **{"with": {"a.b": {"from": 0, "to": 1}}}), "moves in two loops")):
        with pytest.raises(PlanError) as e:
            load_plan({"plan": "x", "sweep": {"a.b": bad}})
        assert why in str(e.value)


def test_two_gates_move_together_a_ramp_step_at_a_time_and_one_approval_covers_both(tmp_path):
    engine, lab, gate, approver = _engine(tmp_path)
    steps = []
    real = gate._set

    def watched(instr, param, value, *a, **k):
        steps.append(("%s.%s" % (instr, param), value))
        return real(instr, param, value, *a, **k)
    gate._set = watched
    plan = """
plan: constant_D
setup: { top.output: on, bottom.output: on }
sweep:
  lockin.tc: { values: [0.1, 0.3] }
  top.V: { from: 0, to: 2, points: 3, with: { bottom.V: { from: 0, to: -2 } } }
measure: [lockin.X]
after: { top.V: 0, bottom.V: 0 }
"""
    report = engine.run(plan)
    assert report.ok, report.message
    assert len(approver.requests) == 1   # one approval, both gates on it
    card = str(approver.requests[0])
    assert "top.V" in card and "bottom.V" in card and "-2" in card
    assert [(r["top.V"], r["bottom.V"]) for r in report.rows] == [(0, 0), (1, -1), (2, -2)] * 2
    # the jump back to the line's start (2, -2) → (0, 0): in turn, a ramp step (0.05 V) each, never one alone
    back = [s for s in steps if s[0] in ("top.V", "bottom.V")]
    jump = back[back.index(("top.V", 2.0)) + 2:]   # past the line's last point
    jump = jump[:jump.index(("bottom.V", 0.0)) + 1]
    assert len(jump) == 80 and [k for k, _ in jump] == ["top.V", "bottom.V"] * 40
    assert all(abs(t + b) < 1e-9 for (_, t), (_, b) in zip(jump[::2], jump[1::2]))   # along the line


def test_a_loop_reads_once_per_value_and_every_point_inside_keeps_it(tmp_path):
    engine, lab, gate, approver = _engine(tmp_path)
    reads = []
    real = lab.read

    def counted(instr, param):
        if (instr, param) == ("tc", "T_sample"):
            reads.append(1)
        return real(instr, param)
    lab.read = counted
    plan = """
plan: outer_reading
setup: { top.output: on }
sweep:
  lockin.tc: { values: [0.1, 0.3], measure: [tc.T_sample] }
  top.V: { from: 0, to: 1, points: 4 }
measure: [lockin.X]
"""
    report = engine.run(plan)
    assert report.ok, report.message
    assert len(reads) == 2 and all("tc.T_sample" in r for r in report.rows)
    assert len({r["tc.T_sample"] for r in report.rows[:4]}) == 1   # read once for the four points of its line
    cols = engine.columns(load_plan(plan))
    assert {"key": "tc.T_sample", "unit": "K", "kind": "scalar", "loop": "lockin.tc"} in cols["measures"]
    with pytest.raises(PlanError) as e:   # recorded once, at each point or in one loop
        load_plan(plan.replace("measure: [lockin.X]", "measure: [lockin.X, tc.T_sample]"))
    assert "recorded once" in str(e.value)


def test_the_first_value_of_a_loop_holds_longer_each_time_it_starts(tmp_path):
    engine, lab, gate, approver = _engine(tmp_path)
    plan = """
plan: first_settle
setup: { top.output: on }
sweep:
  lockin.tc: { values: [0.1, 0.3] }
  top.V: { from: 0, to: 0.2, points: 3, settle: 0.5, first_settle: 4 }
measure: [lockin.X]
"""
    p = load_plan(plan)
    assert [p.axes[1].settle_first(k) for k in range(3)] == [4, 0.5, 0.5]
    report = engine.run(plan)
    assert report.ok, report.message
    t = [r["t"] for r in report.rows]
    assert t[1] - t[0] < 2 and t[3] - t[2] > 4   # back at the line's start: the longer hold again
    assert "first_settle" in scanedit.SPEC_ORDER


def test_variables_set_what_sets_works_out_and_the_approval_is_on_the_real_gates(tmp_path):
    engine, lab, gate, approver = _engine(tmp_path)
    plan = """
plan: n_E_map
setup: { top.output: on, bottom.output: on }
sweep:
  E: { from: -0.1, to: 0.1, points: 3 }
  n: { from: 0, to: 1, points: 3 }
sets: { bottom.V: "1.2*n - 8*E", top.V: "0.9*n + 7*E" }
measure: [lockin.X]
after: { top.V: 0, bottom.V: 0 }
"""
    p = load_plan(plan)
    assert [a.kind for a in p.axes] == ["variable", "variable"] and p.points == 9
    assert [round(v, 6) for v in p.sets[0].values] == [0.8, 1.4, 2.0, 0.0, 0.6, 1.2, -0.8, -0.2, 0.4]
    report = engine.run(plan)
    assert report.ok, report.message
    card = str(approver.requests[0])
    assert "bottom.V" in card and "top.V" in card
    row = report.rows[5]   # E = 0, n = 1
    assert row["E"] == 0 and row["n"] == 1 and row["bottom.V"] == pytest.approx(1.2) and row["top.V"] == pytest.approx(0.9)
    cols = engine.columns(p)
    assert cols["axes"][0] == {"key": "E", "unit": "", "kind": "variable"}
    assert {c["key"] for c in cols["set"]} == {"top.V", "bottom.V"}


def test_sets_are_checked_before_the_run_and_refused_beyond_the_limits(tmp_path):
    engine, lab, gate, approver = _engine(tmp_path)
    report = engine.run("""
plan: too_far
setup: { top.output: on }
sweep: { n: { from: 0, to: 3, points: 4 } }
sets: { top.V: "5*n" }
""")
    assert report.status == "refused" and "outside" in report.message and not approver.requests
    for sweep, sets, why in (({"n": {"from": 0, "to": 1, "points": 2}}, None, "a variable that sets: works"),
                             ({"a.b": {"from": 0, "to": 1, "points": 2}}, {"c.d": "x * 2"}, "x is not swept"),
                             ({"a.b": {"from": 0, "to": 1, "points": 2}}, {"a.b": "1"}, "the sweep sets a.b already"),
                             ({"a.b": {"from": 0, "to": 1, "points": 2}}, {"c.d": "log(a.b)"}, "math domain"),
                             ({"a.b": {"from": 0, "to": 1, "points": 2}}, {"c.d": "mean(a.b)"}, "a whole column")):
        data = dict({"plan": "x", "sweep": sweep}, **({"sets": sets} if sets else {}))
        with pytest.raises(PlanError) as e:
            load_plan(data)
        assert why in str(e.value), (why, str(e.value))


def test_a_point_expression_needs_no_numpy_and_says_what_it_uses():
    assert expr.scalar("1.18*n - 8.4*E + 2.4", {"n": 1, "E": 0.1}) == pytest.approx(2.74)
    assert expr.scalar("where(x < 0.5, -16 + 16 * 2 * x, 0 - 16 * (2 * x - 1))", {"x": 0.75}) == pytest.approx(-8)
    assert expr.names_in('dac.Vbg * 2 + sqrt(n) + col("a b") + pi') == ["dac.Vbg", "n", "a b"]
    with pytest.raises(expr.ExprError):
        expr.scalar("1 / n", {"n": 0})


def test_the_labs_own_names_go_back_to_instrument_parameter(tmp_path):
    names = {"Vbg": "bottom.V", "X_A": "lockin.X", "B": "magnet.B"}
    data = with_names({"plan": "x", "setup": {"B": 1},
                       "sweep": {"Vbg": {"from": 0, "to": 1, "points": 2, "until": "abs(X_A) > 1e-3",
                                         "measure": ["X_A"]}, "n": {"values": [0, 1]}},
                       "sets": {"top.V": "n + B * 2"}, "measure": ["lockin.Y"], "preconditions": ["X_A < 1"]}, names)
    assert data["setup"] == {"magnet.B": 1} and list(data["sweep"]) == ["bottom.V", "n"]
    assert data["sweep"]["bottom.V"]["until"] == "abs(lockin.X) > 1e-3"
    assert data["sweep"]["bottom.V"]["measure"] == ["lockin.X"] and data["preconditions"] == ["lockin.X < 1"]
    assert data["sets"] == {"top.V": "n + magnet.B * 2"}
    engine, lab, gate, approver = _engine(tmp_path)
    assert lab.config.names == {"Vtg": "top.V", "Vbg": "bottom.V", "X_A": "lockin.X"}
    assert lab.resolve("Vbg")[1].name == "V"
    report = engine.run("""
plan: by_its_names
setup: { top.output: on, bottom.output: on }
sweep: { Vbg: { from: 0, to: 0.5, points: 2, with: { Vtg: { from: 0, to: -0.5 } } } }
measure: [X_A]
""")
    assert report.ok, report.message
    assert set(report.rows[0]) >= {"bottom.V", "top.V", "lockin.X"}


EDITED = """plan: nE_map
setup: { top.output: on }
sweep:
  E: { from: -0.1, to: 0.1, points: 3, measure: [tc.T_sample] }
  n: { from: 0, to: 1, points: 3, with: { lockin.amplitude: { from: 0.1, to: 0.2 } }, settle: 0.5, first_settle: 3 }
sets: { bottom.V: "1.2*n - 8*E", top.V: "0.9*n + 7*E" }
measure: [lockin.X]"""


def test_the_editor_shows_what_moves_with_a_loop_its_readings_and_the_computed_settings_and_writes_them_back():
    from labharness.yamlio import load_yaml
    data = load_yaml(EDITED)
    parts = scanedit.to_parts(data)
    assert scanedit.to_plan(parts) == data   # nothing lost
    outer, inner = parts["loops"]
    assert outer["measure"] == ["tc.T_sample"] and inner["first_settle"] == 3 and inner["settle"] == 0.5
    assert inner["with"] == [{"key": "lockin.amplitude", "from": 0.1, "to": 0.2, "values": [], "form": "range",
                              "order": ["from", "to"]}]
    assert parts["sets"] == [{"key": "bottom.V", "value": "1.2*n - 8*E"}, {"key": "top.V", "value": "0.9*n + 7*E"}]
    inner["with"].append({"key": "stage.x", "from": 0, "to": 5, "form": "range"})   # + Channel
    outer["measure"] = []
    parts["sets"][1]["value"] = "n + E"
    again = scanedit.to_plan(parts)
    assert again["sweep"]["n"]["with"] == {"lockin.amplitude": {"from": 0.1, "to": 0.2}, "stage.x": {"from": 0, "to": 5}}
    assert "measure" not in again["sweep"]["E"] and again["sets"]["top.V"] == "n + E"
    assert load_plan(again).axes[1].with_[1].values == [0, 2.5, 5]


def test_gates_move_together_at_the_ends_of_a_line_where_a_worked_out_value_reads_back_rounded(tmp_path):
    engine, lab, gate, approver = _engine(tmp_path)
    log = []
    real_set, real_read = gate._set, lab.read

    def watched(instr, param, value, *a, **k):
        log.append(("%s.%s" % (instr, param), value))
        return real_set(instr, param, value, *a, **k)

    def read(instr, param):
        if (instr, param) == ("lockin", "X"):
            log.append(("point", None))
        return real_read(instr, param)
    gate._set, lab.read = watched, read
    report = engine.run("""
plan: n_E_map
setup: { top.output: on, bottom.output: on }
sweep:
  E: { from: -0.3, to: 0.3, points: 7 }
  n: { from: 0, to: 2, points: 11 }
sets: { bottom.V: "1.18*n - 8.4*E + 2.4", top.V: "0.95*n + 7.6*E + 2.6" }
measure: [lockin.X]
after: { top.V: 0, bottom.V: 0 }
""")
    assert report.ok, report.message
    points = [i for i, (k, _) in enumerate(log) if k == "point"]
    last, biggest = {}, 0.0
    for k, v in log[points[0]:points[-1]]:   # from the first point to the last: the line's ends too
        if k in ("top.V", "bottom.V"):
            if k in last:
                biggest = max(biggest, abs(v - last[k]))
            last[k] = v
    assert biggest <= 0.05 + 1e-6   # a ramp step at a time, both gates in turn: never one gate's whole move alone


def test_a_computed_setting_that_uses_no_loop_is_set_once_before_the_first_point(tmp_path):
    engine, lab, gate, approver = _engine(tmp_path)
    report = engine.run("""
plan: fixed_gate
setup: { top.output: on, bottom.output: on }
sweep: { top.V: { from: 0, to: 0.2, points: 3 } }
sets: { bottom.V: "8*(0.05)" }
measure: [lockin.X]
""")
    assert report.ok, report.message
    assert [r["bottom.V"] for r in report.rows] == [pytest.approx(0.4)] * 3
    assert lab.read("bottom", "V") == pytest.approx(0.4)
    assert "bottom.V" in str(approver.requests[0])


def test_what_the_sweep_sets_isnt_read_again_by_a_loop_and_a_name_beside_its_setting_is_refused():
    for sweep in ({"top.V": {"values": [0, 1], "measure": ["top.V"]}},
                  {"lockin.tc": {"values": [0.1, 0.3], "measure": ["top.V"]}, "top.V": {"values": [0, 1]}},
                  {"top.V": {"values": [0, 1], "with": {"bottom.V": [0, -1]}, "measure": ["bottom.V"]}}):
        with pytest.raises(PlanError) as e:
            load_plan({"plan": "x", "sweep": sweep})
        assert "each point records the value it is set to" in str(e.value)
    with pytest.raises(PlanError) as e:   # with: {from, to} follows numbers only
        load_plan({"plan": "x", "sweep": {"a.b": {"values": [0, "mid", 1], "with": {"c.d": {"from": 0, "to": 1}}}}})
    assert "give values: instead" in str(e.value)
    names = {"Vbg": "bottom.V"}
    for data in ({"plan": "x", "sweep": {"Vbg": {"values": [0, 1]}, "bottom.V": {"values": [0, 1]}}},
                 {"plan": "x", "setup": {"Vbg": 1, "bottom.V": 2}},
                 {"plan": "x", "measure": ["Vbg", "bottom.V"]}):
        with pytest.raises(PlanError) as e:
            with_names(data, names)
        assert "Vbg and bottom.V are the same setting" in str(e.value)
    kept = {"plan": "x", "sweep": {"E": {"values": [0, 1]}}, "sets": {"top.V": "2*E"}}
    assert with_names(kept, {"E": "lockin.amplitude"}, keep={"E"}) == kept   # a kept plan's variable stays one
    assert list(with_names(kept, {"E": "lockin.amplitude"})["sweep"]) == ["lockin.amplitude"]   # text coming in


def test_a_name_the_lab_gives_later_never_changes_a_kept_scan(tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from labharness.yamlio import load_yaml
    rt = LabRuntime(build_virtual_lab(seed=1, config=TWO_GATES), WebApprover(timeout=5), tmp_path / "data")
    kept = ('plan: d_map\nsetup: { top.output: on }\nsweep:\n  D: { from: 0, to: 0.2, points: 3 }\n'
            'sets: { top.V: "2*D" }\nmeasure: [lockin.X]\n')
    added = rt.queue_add(kept, by="human")
    assert added.startswith("added plan d_map"), added
    item = rt.queue.items[-1]
    rt.lab.config.names["D"] = "lockin.amplitude"   # a name the lab gives after the scan was kept
    try:
        assert rt._plan(item.text).axes[0].kind == "variable"   # a kept plan is never put back again
        assert rt._plan(kept, named=True).axes[0].key == "lockin.amplitude"   # a plan coming in: the lab's name
        parts = scanedit.to_parts(load_yaml(item.text))
        assert parts["variables"] == ["D"]
        check = rt.scan_check(parts)
        assert check["loads"] and "D:" in check["text"], check
        said = rt.queue_edit(item.id, item.text.replace("points: 3", "points: 4"), by="human")
        assert said.startswith("changed"), said
        assert "D:" in rt.queue.find(item.id).text   # its variable stays its own
        assert "lockin.X" in rt.add_watch("X_A", above=1.0)["rule"]   # a name: watched as its card has it
    finally:
        rt.lab.config.names.pop("D", None)


def test_the_end_moves_its_gates_together_too_in_the_order_written(tmp_path):
    engine, lab, gate, approver = _engine(tmp_path)
    log = []
    real = gate._set

    def watched(instr, param, value, *a, **k):
        log.append(("%s.%s" % (instr, param), value))
        return real(instr, param, value, *a, **k)
    gate._set = watched
    report = engine.run("""
plan: to_zero
setup: { top.output: on, bottom.output: on }
sweep: { top.V: { from: 0, to: 2, points: 3, with: { bottom.V: { from: 0, to: -2 } } } }
measure: [lockin.X]
after: { top.V: 0, bottom.V: 0, top.output: off }
""")
    assert report.ok, report.message
    end = log[log.index(("top.V", 2.0)) + 2:]   # past the last point: the end, as Special Measure's escape function
    gates = [(k, v) for k, v in end if k in ("top.V", "bottom.V")]
    assert len(gates) == 80 and [k for k, _ in gates] == ["top.V", "bottom.V"] * 40   # in turn, a ramp step each
    assert all(abs(t + b) < 1e-9 for (_, t), (_, b) in zip(gates[::2], gates[1::2]))   # along the line to 0, 0
    assert end[-1][0] == "top.output"   # switched off after its gates, as written
