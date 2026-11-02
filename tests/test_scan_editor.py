"""The scan editor (the Runs panel's Edit, Special Measure style): a plan as its parts and back without losing
anything (scanedit.py), what the editor can't show kept as written; its check (points, time, load_plan's and the
gate's problems) reads no instrument; an edited scan is saved as a new entry of the library, "edited from" the old one,
which stays; and it runs only as any scan does, through the queue and its approval (POST /api/library)."""
import json

import pytest
import yaml

from labharness import scanedit
from labharness.yamlio import dump_yaml, load_yaml

PLANS = [
    # Kelvin's own plans, from plans.py and the tests
    """plan: gate_sweep
setup: { lockin.amplitude: 0.05, smu.output: on }
sweep: { smu.V: {from: 0, to: 2, step: 0.5} }
settle: 0.1
measure: [lockin.X, smu.I]
after: { smu.V: 0 }""",
    """plan: gate_sweep
device: D3
preconditions: ["tc.T_sample < 2.5"]
setup:   { lockin.amplitude: 0.05, smu.output: on }
sweep:
  magnet.B: { values: [0, 0.5, 1], stabilize: { read: tc.T_sample, within: 0.02, for: 30 } }
  smu.V:    { from: 0, to: 10, step: 0.05, back: true }
repeat: 3
time: { every: 2, for: 600 }
settle: 0.3
trigger: [dmm.arm, gen.fire]
measure: [lockin.X, lockin.Y, smu.I]
after:   { smu.V: 0 }
on_abort: { smu.V: 0 }""",
    """plan: rt
sweep:
  ppms.T_set: { ramp: [{to: 10, rate: 10}, {to: 2, rate: 1, hold: 600}], every: 10 }
measure: [ppms.T]""",
    """plan: one_leg
sweep: { ppms.T_set: { ramp: 10, every: 10, read: ppms.T, within: 0.1, rate: 2, stall: 0 } }""",
    """plan: legs
sweep: { ppms.T_set: { ramp: [10, {hold: 5, to: 2}], every: 10 } }""",
    """plan: until
sweep:
  smu.V:  { from: 0, to: 10, step: 0.1, until: "abs(smu.I) > 1e-6" }
  tc.setpoint: { values: [2, 5, 10, 50], settle: {50: 600} }
time: { every: 10, for: 7200, until: "tc.T_sample < 2.1" }
until: "nvm.V < 1e-6"
retry: { times: 3, wait: 10 }""",
    """plan: cv
technique: { pstat.cv: { start: -0.2, vertex: 0.6, cycles: 2 } }
sweep:     { pstat.cv.rate: { values: [0.02, 0.05, 0.1] } }""",
    """measure: []
sweep: { smu.V: [0, 1, 2], lockin.frequency: {to: 1.0e+6, from: 1e3, points: 31, log: true} }
description: ''
plan: written_out_of_order""",
    # what the editor can't show exactly: kept whole, as written
    """plan: odd
sweep: { smu.V: {from: "0", to: 1, step: 0.5}, a.b: 5, c.d: {}, e.f: null, g.h: {from: 0, to: 1, step: 0.5, points: 3},
         i.j: {values: [1, 2], from: 0}, k.l: {ramp: [{to: 1, rate: null}], every: 1}, m.n: {from: 0, to: 1, every: 2} }
setup: { smu.V: null }
settle: "0.3"
repeat: true
time: {every: 2, for: 600, until: "x.y > 1"}
weird: {1: 2}
1: one""",
    """description: no name
sweep: {smu.V: {from: 0, to: 1, points: 5, back: false, settle: [1, 2, 3, 4, 5]}}""",
    """plan: words
sweep: {x.y: {values: ["1.0e7", "on", true, 1e7, 3]}}
setup: {a.b: "1e7", c.d: on}""",
]


def _same(a, b):
    """Equal, and in the same order: the plan's keys, its loops (outer first) and each loop's keys."""
    assert a == b
    assert list(a) == list(b)
    if isinstance(a.get("sweep"), dict):
        assert list(a["sweep"]) == list(b["sweep"])
        for key, spec in a["sweep"].items():
            if isinstance(spec, dict):
                assert list(spec) == list(b["sweep"][key]), key


@pytest.mark.parametrize("text", PLANS)
def test_a_plan_goes_to_its_parts_and_back_as_it_was(text):
    plan = load_yaml(text)
    parts = scanedit.to_parts(plan)
    _same(scanedit.to_plan(parts), plan)
    _same(scanedit.to_plan(json.loads(json.dumps(parts))), plan)   # through the browser too
    assert load_yaml(dump_yaml(plan)) == plan                       # and the kept text reads back the same


def test_what_the_editor_cant_show_is_kept_as_written():
    parts = scanedit.to_parts(load_yaml(PLANS[1]))
    assert parts["name"] == "gate_sweep" and parts["measure"] == ["lockin.X", "lockin.Y", "smu.I"]
    assert parts["setup"] == [{"key": "lockin.amplitude", "value": 0.05}, {"key": "smu.output", "value": True}]
    assert parts["repeat"] == 3 and parts["settle"] == 0.3 and parts["time"]["every"] == 2
    assert parts["fixed"] == ["device", "preconditions", "trigger", "on_abort"]
    assert load_yaml(parts["kept"]) == {"device": "D3", "preconditions": ["tc.T_sample < 2.5"],
                                        "trigger": ["dmm.arm", "gen.fire"], "on_abort": {"smu.V": 0}}
    field, steps = parts["loops"]
    assert (field["key"], field["mode"], field["values"]) == ("magnet.B", "values", [0, 0.5, 1])
    assert field["fixed"] == ["stabilize"] and "within: 0.02" in field["kept"]
    assert (steps["mode"], steps["from"], steps["to"], steps["step"], steps["by"], steps["back"]) == \
        ("range", 0, 10, 0.05, "step", True)
    odd = scanedit.to_parts(load_yaml(PLANS[8]))
    modes = {loop["key"]: loop["mode"] for loop in odd["loops"]}
    assert modes == {"smu.V": "kept", "a.b": "kept", "c.d": "kept", "e.f": "kept", "g.h": "range", "i.j": "kept",
                     "k.l": "kept", "m.n": "kept"}
    assert odd["loops"][0]["text"] == "{from: '0', to: 1, step: 0.5}\n"
    assert odd["loops"][4]["fixed"] == ["step"] and odd["loops"][4]["by"] == "points"   # load_plan takes points
    assert odd["setup"] is None and odd["settle"] is None and odd["repeat"] is None and odd["time"] is None
    assert odd["fixed"] == ["setup", "settle", "repeat", "time", "weird", "1"]
    # fields a person fills in never write over what is kept: the kept part wins
    odd["settle"], odd["setup"] = 1.0, [{"key": "smu.V", "value": 2}]
    odd["loops"][4]["step"], odd["loops"][4]["by"] = 0.1, "step"
    plan = scanedit.to_plan(odd)
    assert plan["settle"] == "0.3" and plan["setup"] == {"smu.V": None} and plan["sweep"]["g.h"]["step"] == 0.5


def test_a_persons_edits_make_the_plan_they_show():
    parts = scanedit.to_parts(load_yaml(PLANS[1]))
    field, steps = parts["loops"]
    steps.update(by="points", points=21, step=None, back=False, settle=0.5)   # points now, not there and back
    field.update(mode="range", **{"from": -1, "to": 1, "by": "points", "points": 5})   # values → a range
    parts["loops"] = [steps, field, dict(scanedit.to_parts({"sweep": {"x.y": [1]}})["loops"][0],
                                          key="lockin.frequency", values=[17, 77])]
    parts["measure"].remove("lockin.Y")
    parts["setup"].append({"key": "smu.nplc", "value": 1})
    parts["setup"].append({"key": "", "value": 3})                   # a row not filled in yet: left out
    parts["after"] = []
    parts.update(repeat=None, time=None, description="finer")
    plan = scanedit.to_plan(parts)
    assert list(plan) == ["plan", "description", "device", "preconditions", "setup", "sweep", "settle", "trigger",
                          "measure", "after", "on_abort"]                # a new key where Kelvin writes it
    assert plan["sweep"] == {"smu.V": {"from": 0, "to": 10, "points": 21, "back": False, "settle": 0.5},
                             "magnet.B": {"from": -1, "to": 1, "points": 5,
                                          "stabilize": {"read": "tc.T_sample", "within": 0.02, "for": 30}},
                             "lockin.frequency": [17, 77]}
    assert plan["setup"] == {"lockin.amplitude": 0.05, "smu.output": True, "smu.nplc": 1}
    assert plan["after"] == {} and plan["measure"] == ["lockin.X", "smu.I"] and plan["on_abort"] == {"smu.V": 0}
    ramp = scanedit.to_parts(load_yaml(PLANS[3]))
    ramp["loops"][0]["legs"].append({"to": 2, "rate": 1, "hold": 600})
    assert scanedit.to_plan(ramp)["sweep"]["ppms.T_set"] == {"ramp": [10, {"to": 2, "rate": 1, "hold": 600}],
                                                            "every": 10, "read": "ppms.T", "within": 0.1, "rate": 2,
                                                            "stall": 0}
    new = scanedit.to_parts({"plan": "scan"})
    new["loops"] = [dict(scanedit._blank("smu.V", "range"), **{"from": 0, "to": 1, "step": 0.1})]
    new["measure"] = ["smu.I"]
    assert scanedit.to_plan(new) == {"plan": "scan", "sweep": {"smu.V": {"from": 0, "to": 1, "step": 0.1}},
                                     "measure": ["smu.I"]}
    for loops, why in (([dict(scanedit._blank("", "range"))], "loop 1: choose what it steps"),
                       ([scanedit._blank("smu.V", "values"), scanedit._blank("smu.V", "range")],
                        "smu.V is stepped by two loops"),
                       ([dict(scanedit._blank("smu.V", "ramp"), legs=[["to", 1]])], "a ramp's leg"),
                       ([dict(scanedit._blank("smu.V", "range"), mode="spiral")], "a range, values, a ramp")):
        with pytest.raises(ValueError, match=why):
            scanedit.to_plan(dict(new, loops=loops))
    with pytest.raises(ValueError, match="set twice in setup"):
        scanedit.to_plan(dict(new, setup=[{"key": "smu.V", "value": 1}, {"key": "smu.V", "value": 2}]))
    with pytest.raises(ValueError, match="not as the editor sends them"):
        scanedit.to_plan(dict(new, loops=[dict(scanedit._blank("smu.V", "values"), values=5)]))
    with pytest.raises(ValueError, match="not as the editor sends them"):
        scanedit.to_plan(dict(new, kept="a: [b"))


# ------------------------------------------------------------------ the runtime: check, save, run

SWEEP = """plan: gate_sweep
setup: { lockin.amplitude: 0.05, smu.output: on }
sweep: { smu.V: {from: 0, to: 2, step: 0.5} }
settle: 0.1
measure: [lockin.X, smu.I]
after: { smu.V: 0 }"""


@pytest.fixture
def ran(tmp_path):
    """A runtime on the test lab whose library has the scan SWEEP, run once."""
    pytest.importorskip("mcp")
    from test_scan_library import virtual
    rt = virtual(tmp_path, [True] * 10)
    assert "done" in rt.run(SWEEP)
    (entry,) = rt.library_view()["scans"]
    return rt, entry


def test_the_check_says_points_time_and_whats_wrong_reading_no_instrument(ran, monkeypatch):
    from labharness.instrument import Instrument
    rt, entry = ran
    opened = rt.scan_parts(entry["id"])
    assert opened["id"] == entry["id"] and opened["name"] == "gate_sweep"
    assert {"key": "smu.V", "unit": "V", "drive": True} in opened["lab"]["set"]
    assert {"key": "lockin.X", "unit": "V"} in opened["lab"]["read"]
    assert not any(c["key"] == "lockin.X" for c in opened["lab"]["set"])   # a reading, never stepped
    check = opened["check"]
    assert check["loads"] and check["problems"] == [] and check["points"] == 5 and check["counts"] == {"smu.V": 5}
    assert check["time"].startswith("~") and yaml.safe_load(check["text"]) == yaml.safe_load(entry["text"])

    asked = []

    def no(inst, fn, *args, **kw):
        asked.append((inst.name, args))
        raise AssertionError("the editor's check asked an instrument")
    monkeypatch.setattr(Instrument, "_io", no)                       # every exchange with an instrument goes here
    parts = opened["parts"]
    parts["loops"][0].update(to=20)                                  # beyond smu.V's limits [-15, 15]
    check = rt.scan_check(parts)
    assert check["loads"] and check["points"] == 41 and "outside" in " ".join(check["problems"])
    parts["loops"][0].update(to=2, key="smu.Q")
    unknown = rt.scan_check(parts)
    assert unknown["loads"] and unknown["problems"] == ["sweep: virtual/source-meter has no parameter 'Q' "
                                                        "(has: V, V_range, V_autorange, compliance_I, output, I, nplc)"]
    parts["loops"][0].update(key="smu.V", step=0)
    bad = rt.scan_check(parts)
    assert not bad["loads"] and "nonzero step" in bad["problems"][0] and bad["points"] is None
    parts["loops"][0].update(step=0.5)
    parts["setup"].append({"key": "tc.setpoint", "value": 50})       # warms the sample ...
    parts["loops"].insert(0, dict(scanedit._blank("magnet.B", "values"), values=[0, 1]))   # ... under a field
    assert any(p.startswith("interlock: magnet.B needs tc.T_sample < 10") for p in rt.scan_check(parts)["problems"])
    parts["loops"] = [dict(scanedit._blank("tc.setpoint", "ramp"), legs=[{"to": 5}], every=10)]   # a ramp follows T
    ramp = rt.scan_check(parts)   # at a rate its card can't tell: how many points is not known, never its legs
    assert ramp["loads"] and ramp["estimate"] and ramp["problems"] == []
    assert ramp["points"] is None and not ramp["counts"]
    rt._keep("magnet", {"B_actual": 0.0}, "read")                   # the field as Kelvin last saw it
    parts["setup"] = []
    parts["loops"] = [dict(scanedit._blank("magnet.B", "ramp"), legs=[{"to": 1, "rate": 0.5}], every=10)]
    timed = rt.scan_check(parts)   # 1 T at 0.5 T/min: 120 s, a point every 10 s and one as it starts
    assert timed["problems"] == [] and timed["points"] == 13 and timed["counts"] == {"magnet.B": 13}, timed
    assert timed["time"] == "~2.0 min", timed
    assert rt.scan_check({"loops": "x"})["problems"] and rt.scan_check("x")["problems"] == ["send the scan's parts"]
    assert asked == []


def test_every_helper_the_check_calls_gets_what_kelvin_has_seen_never_an_instrument(ran, monkeypatch):
    """A pulse generator's sequence (is it playing, its times), a ramp's rate, a source's fixed range, a stage's
    velocity and a counter's integration: each read as Kelvin last saw it, or not known; a pulse generator was asked
    OUTPut? in the middle of a run."""
    from labharness.agent.runtime import _SeenGate
    from labharness.instrument import Instrument
    rt, _ = ran
    asked = []

    def no(inst, fn, *args, **kw):
        asked.append((inst.name, args))
        raise AssertionError("the editor's check asked an instrument")
    monkeypatch.setattr(Instrument, "_io", no)
    with rt._readings_lock:
        rt._readings.pop("pg", None)                                 # never seen: is it playing? not known
    parts = rt.scan_parts()["parts"]
    parts.update(measure=["counter.contrast"], loops=[dict(scanedit._blank("pg.tau", "values"), values=[1e-7, 1e-3])])
    assert rt.scan_check(parts)["problems"] == []                   # taken as not playing; the queue reads it
    rt._keep("pg", {"running": True, "protocol": "rabi", "t_laser": 3e-6, "t_wait": 1e-6}, "read")
    playing = rt.scan_check(parts)                                   # seen playing: every tau checked, as seen
    assert playing["problems"] == ["pg at tau = 1 ms: the microwave is on 100% of the time, above the lab's 50% limit "
                                   "(heating): lengthen t_wait or shorten the MW pulses"], playing
    rt._keep("magnet", {"B_actual": 0.0, "rate": 0.5}, "read")
    parts.update(loops=[dict(scanedit._blank("magnet.B", "ramp"), legs=[{"to": 1}], every=10)], measure=["smu.I"])
    assert rt.scan_check(parts)["points"] == 13                      # 1 T at the 0.5 T/min it reads
    rt._keep("smu", {"V_autorange": False, "V_range": 2.1}, "read")
    parts["loops"] = [dict(scanedit._blank("smu.V", "values"), values=[0, 5]),
                      dict(scanedit._blank("stage.position", "values"), values=[0, 1])]
    assert any("beyond its fixed range (2.1 V)" in p for p in rt.scan_check(parts)["problems"])
    assert asked == []

    lab = _SeenGate(rt).lab                                          # what the plan code gets for the lab
    pg = lab.instruments["pg"]
    assert lab.resolve("pg.tau")[0] is pg and lab.instrument("pg") is pg and pg.read("running") is True
    assert pg.limits("tau") == (0.0, 0.01) and pg.card.id == "virtual/pulse-generator"
    with rt._readings_lock:
        rt._readings.pop("counter", None)
    with pytest.raises(ValueError, match="not known"):
        lab.read("counter", "contrast")
    for send in ("write", "act", "query_raw", "write_raw", "_io", "instrument_error"):
        assert not hasattr(pg, send), send
    assert isinstance(pg.backend.state, dict) and not hasattr(pg.backend, "query")
    assert not hasattr(lab, "snapshot") and not hasattr(lab, "health_check") and lab.away("pg") is None
    assert asked == []


def test_a_check_of_a_million_points_takes_a_moment_and_more_are_refused_before_any_is_made(ran):
    """The check of a million-point range took 34 s, and ten million took 37 s and 314 MB before they were refused, at
    every pause in typing. Its points, its time, its limits and its interlocks are worked out from its loops."""
    import time
    rt, _ = ran
    parts = dict(rt.scan_parts()["parts"], measure=["smu.I"])

    def check(loops, **more):
        t0 = time.perf_counter()
        out = rt.scan_check(dict(parts, loops=loops, **more))
        return out, time.perf_counter() - t0
    big = dict(scanedit._blank("smu.V", "range"), **{"from": 0, "to": 1, "points": 1000000, "by": "points"})
    c, took = check([big])
    assert c["problems"] == [] and c["points"] == 1000000 and c["time"].startswith("~") and took < 10, (c, took)
    c, _ = check([dict(big, **{"from": 14, "to": 16, "points": 21})])
    assert c["problems"][0].startswith("sweep: smu.V 15.1 V is outside")   # the first beyond, as each was checked
    field = [{"key": "tc.setpoint", "value": 50}]                    # warms the sample under a field, swept too
    c, took = check([dict(scanedit._blank("magnet.B", "values"), values=[0, 1]), dict(big, points=500000)],
                    setup=field)
    assert any(p.startswith("interlock: magnet.B needs tc.T_sample < 10") for p in c["problems"]) and took < 10
    for loops, more, points in (([dict(big, points=10 ** 7)], {}, 10 ** 7), ([], {"repeat": 10 ** 9}, 10 ** 9),
                                ([], {"time": {"every": 0.001, "for": 10 ** 6, "points": None, "order": []}},
                                 10 ** 9 + 1)):
        c, took = check(loops, **more)
        assert c["problems"] == ["plan has %d points (max 1000000)" % points] and took < 2, (c["problems"], took)


def test_an_edited_scan_is_a_new_entry_and_the_old_one_stays(ran):
    rt, entry = ran
    marks = {"project": "p1", "confidential": True}
    rt.library.note_plan(entry["text"], "gate_sweep", entry["summary"], "r2", marks=marks)   # whose its runs were
    parts = rt.scan_parts(entry["id"])["parts"]
    parts["loops"][0].update(step=0.25)
    saved = rt.scan_save(parts, entry["id"], "human")
    new = saved["entry"]
    assert saved["new"] and new["id"] != entry["id"] and new["name"] == "gate_sweep"
    assert new["edited_from"] == "gate_sweep" and new["source"] == "you" and new["marks"] == marks
    assert new["count"] == 0 and "smu.V 0 → 2 V, 9 points" in new["summary"]
    assert {e["id"] for e in rt.library_view()["scans"]} == {entry["id"], new["id"]}   # the old one stays
    again = rt.scan_save(parts, entry["id"], "human")
    assert not again["new"] and again["entry"]["id"] == new["id"] and len(rt.library_view()["scans"]) == 2
    parts["name"] = "gate_fine"                                      # renamed: its own name, nothing else
    renamed = rt.scan_save(parts, entry["id"], "human")["entry"]
    assert renamed["name"] == "gate_fine" and "edited_from" not in renamed
    blank = rt.scan_parts()
    assert blank["id"] is None and blank["parts"]["name"] == "scan" and not blank["check"]["problems"]
    made = rt.scan_save(dict(blank["parts"], loops=[dict(scanedit._blank("smu.V", "values"), values=[0, 1])],
                             measure=["smu.I"]), None, "human")["entry"]
    assert made["name"] == "scan" and "edited_from" not in made and made["marks"] == {}
    with pytest.raises(ValueError, match="not saved: .*nonzero step"):
        parts["loops"][0].update(step=0)
        rt.scan_save(parts, entry["id"], "human")
    orphan = rt.scan_save(dict(blank["parts"], name="orphan"), "plan-gone", "human")["entry"]   # its scan taken out
    assert orphan["name"] == "orphan" and "edited_from" not in orphan and orphan["marks"] == {}
    events = [json.loads(line) for line in (rt.data_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e.get("edited_from") for e in events if e["type"] == "library" and e.get("change") == "saved"] == \
        ["gate_sweep", "gate_sweep", None, None]


def test_the_editor_routes_and_an_edited_scan_runs_through_the_queue(ran, tmp_path):
    from test_scan_library import finish, portal
    rt, entry = ran
    client, _ = portal(rt, tmp_path)
    opened = client.post("/api/library", json={"op": "parts", "id": entry["id"]}).json()
    assert set(opened) == {"id", "name", "edited_from", "parts", "lab", "check"} and opened["check"]["points"] == 5
    assert opened["edited_from"] is None
    parts = opened["parts"]
    parts["loops"][0].update(to=1)
    check = client.post("/api/library", json={"op": "check", "parts": parts}).json()
    assert check["points"] == 3 and check["problems"] == [] and "to: 1" in check["text"]
    saved = client.post("/api/library", json={"op": "save", "parts": parts, "from": entry["id"]}).json()
    assert saved["new"] and saved["entry"]["edited_from"] == "gate_sweep"
    assert [e["id"] for e in saved["library"]["scans"]].count(saved["entry"]["id"]) == 1
    reopened = client.post("/api/library", json={"op": "parts", "id": saved["entry"]["id"]}).json()
    assert reopened["edited_from"] == "gate_sweep" and reopened["check"]["points"] == 3
    client.post("/api/library", json={"op": "rename", "id": saved["entry"]["id"], "name": "short sweep"})
    assert client.post("/api/library", json={"op": "parts", "id": saved["entry"]["id"]}).json()["edited_from"] is None

    parts["loops"][0].update(to=1.5)                                 # queued: kept in the library, at the end
    queued = client.post("/api/library", json={"op": "run", "parts": parts, "from": entry["id"], "queue": True}).json()
    item = queued["queue"]["items"][-1]
    assert item["kind"] == "plan" and item["status"] == "waiting" and queued["queue"]["state"] == "idle"
    assert yaml.safe_load(item["text"])["sweep"] == {"smu.V": {"from": 0, "to": 1.5, "step": 0.5}}
    assert item["library"] == queued["entry"]["id"] and queued["entry"]["id"] in {
        e["id"] for e in queued["library"]["scans"]}
    rt.queue_change("remove", item["id"])

    ran_now = client.post("/api/library", json={"op": "run", "parts": parts, "from": entry["id"]})   # run: approved
    assert ran_now.status_code == 200 and "queue started: 1 scan" in ran_now.json()["message"]
    finish(rt)
    (done,) = rt.queue.items
    assert done.status == "done", done.message
    kept = rt.library.get(queued["entry"]["id"])
    assert kept["count"] == 1 and kept["runs"] == [done.run_id]      # its runs are the edited scan's

    over = dict(parts, loops=[dict(parts["loops"][0], to=99)])
    refused = client.post("/api/library", json={"op": "run", "parts": over, "queue": True})
    assert refused.status_code == 400 and refused.json()["error"].startswith("not added: ")
    assert not any("99" in (e.get("text") or "") for e in rt.library_view()["scans"])   # never kept, never queued
    bad = client.post("/api/library", json={"op": "save", "parts": dict(parts, loops=[])})
    assert bad.status_code == 200 and bad.json()["new"]              # a scan of one point is a scan
    assert client.post("/api/library", json={"op": "save", "parts": "nope"}).status_code == 400
    assert client.post("/api/library", json={"op": "parts", "id": "plan-nope"}).status_code == 404
    assert client.post("/api/library", json={"op": "parts"}).json()["parts"]["name"] == "scan"
    assert client.post("/api/library", json={"op": "check", "parts": {"loops": [{}]}}).json()["problems"] == [
        "loop 1: choose what it steps (instrument.parameter)"]


def test_checks_have_threads_of_their_own_and_of_an_editors_waiting_checks_only_the_newest_runs(ran, tmp_path,
                                                                                                monkeypatch):
    """Each pause in typing asked for a check, which took one of the server's shared threads for as long as it ran
    (34 s for a million points), and none was ever dropped: the checks have threads of their own, and of the checks
    an editor asked for that still wait, only its newest runs (the others come back superseded)."""
    import threading
    import time
    from test_scan_library import portal
    rt, _ = ran
    client, _ = portal(rt, tmp_path)
    release, ran_on, check = threading.Event(), [], rt.scan_check
    parts, out = rt.scan_parts()["parts"], {}

    def slow(parts):
        ran_on.append((threading.current_thread().name, parts.get("name")))
        release.wait(30)
        return check(parts)
    monkeypatch.setattr(rt, "scan_check", slow)

    def ask(name, editor):
        out[name] = client.post("/api/library", json={"op": "check", "parts": dict(parts, name=name),
                                                      "editor": editor}).json()
    with client:
        workers = [threading.Thread(target=ask, args=(n, n)) for n in ("x", "y")]   # both check threads taken
        for w in workers:
            w.start()
        t0 = time.time()
        while len(ran_on) < 2 and time.time() - t0 < 10:
            time.sleep(0.02)
        for name in ("e1", "e2", "e3"):   # one editor, typing on: each waits for a thread
            workers.append(threading.Thread(target=ask, args=(name, "e")))
            workers[-1].start()
            time.sleep(0.3)
        release.set()
        for w in workers:
            w.join(30)
    assert sorted(name for _, name in ran_on) == ["e3", "x", "y"], ran_on
    assert all(thread.startswith("kelvin-check") for thread, _ in ran_on), ran_on
    assert out["e1"] == out["e2"] == {"superseded": True}
    assert out["e3"]["loads"] and out["e3"]["text"].startswith("plan: e3") and out["x"]["loads"]


def test_a_script_isnt_edited_here_and_phones_get_none_of_it(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from labharness.web.remote import allowed
    from test_scan_library import portal, virtual
    rt = virtual(tmp_path, [])
    script = tmp_path / "probe.py"
    script.write_text("print('hi')\n", encoding="utf-8")
    entry = rt.library.add_script(script)
    with pytest.raises(ValueError, match="is a script"):
        rt.scan_parts(entry["id"])
    for role in ("watch", "control"):
        assert not allowed(role, "POST", "/api/library")
        assert not allowed(role, "POST", "/api/queue")   # nor the queue's fields and edits
    client, guard = portal(rt, tmp_path)
    from starlette.testclient import TestClient
    with TestClient(guard):
        guard.remote.port = 8771
        phone = TestClient(guard, base_url="http://192.168.1.50:8771")
        _, token = guard.remote.issue("Ana's phone", "control", "ios-ana")
        for op in ("parts", "check", "text", "save", "run"):
            r = phone.post("/api/library", headers={"X-Kelvin-Token": token}, json={"op": op, "parts": {}})
            assert r.status_code == 403, op


# ------------------------------------------------------------------ the Runs panel's Schedule: ⇒, and Edit on a queued scan

def test_the_schedules_arrow_puts_scans_after_the_selected_item_never_started(ran, tmp_path):
    """⇒ (the Runs panel's Schedule): each selected scan or script after the selected queue item, in the order selected
    (op run with queue and position: that place, 1 = first), filed and noted as from its Queue, nothing started and
    nothing asked; a place past the end is the end; a step or a pasted item goes to a place too (op add)."""
    from test_scan_library import portal
    rt, entry = ran
    client, _ = portal(rt, tmp_path)
    asked = len(rt.gate.approver.requests)
    for text in ("step: first\nwait: 1", "step: second\nwait: 1"):
        assert client.post("/api/queue", json={"op": "add", "text": text}).status_code == 200
    out = client.post("/api/library", json={"op": "run", "id": entry["id"], "queue": True, "position": 2}).json()
    items = out["queue"]["items"]
    assert [i["name"] for i in items] == ["first", "gate_sweep", "second"] and out["message"] == "added gate_sweep as #2"
    assert out["queue"]["state"] == "idle" and not items[1]["then_pause"] and items[1]["library"] == entry["id"]
    then = client.post("/api/library", json={"op": "run", "id": entry["id"], "queue": True, "position": 3}).json()
    assert [i["name"] for i in then["queue"]["items"]] == ["first", "gate_sweep", "gate_sweep", "second"]
    script = tmp_path / "probe.py"
    script.write_text("print('hi')\n", encoding="utf-8")
    probe = rt.library.add_script(script)   # a script a person imported goes there too, as its Queue sends it
    got = client.post("/api/library", json={"op": "run", "id": probe["id"], "queue": True, "position": 1})
    assert got.status_code == 200, got.text
    assert [i["kind"] for i in got.json()["queue"]["items"]][:2] == ["script", "step"]
    last = client.post("/api/library", json={"op": "run", "id": entry["id"], "queue": True, "position": 99}).json()
    assert last["queue"]["items"][-1]["name"] == "gate_sweep" and last["message"] == "added gate_sweep as #6"
    for bad in (0, -1, "2", True, 1.5):
        r = client.post("/api/library", json={"op": "run", "id": entry["id"], "queue": True, "position": bad})
        assert r.status_code == 400 and "position" in r.json()["error"], bad
    step = client.post("/api/queue", json={"op": "add", "text": "step: third\nwait: 1", "position": 3}).json()
    assert [i["name"] for i in step["items"]][:4] == ["probe.py", "first", "third", "gate_sweep"]
    assert len(rt.gate.approver.requests) == asked and rt.queue.state == "idle"


def test_a_scan_waiting_in_the_queue_is_edited_and_saved_back_into_its_item(ran, tmp_path):
    """Edit on a queued scan: the editor opens that item's plan (op parts with item: its id or #n), Save makes its plan's
    text from the parts (op text, nothing kept) and puts it back into the item (/api/queue op edit), which checks it as
    any change; a running item isn't changed, a step or a script isn't opened in the editor, and a step's fields come
    as written (/api/queue op fields), for the step row."""
    from test_scan_library import portal
    rt, entry = ran
    client, _ = portal(rt, tmp_path)
    item = client.post("/api/library", json={"op": "run", "id": entry["id"], "queue": True}).json()["queue"]["items"][-1]
    opened = client.post("/api/library", json={"op": "parts", "item": item["id"]}).json()
    assert opened["item"] == item["id"] and opened["library"] == entry["id"] and opened["id"] is None
    assert opened["name"] == "gate_sweep" and opened["check"]["points"] == 5 and opened["check"]["problems"] == []
    assert {"key": "smu.V", "unit": "V", "drive": True} in opened["lab"]["set"]
    assert client.post("/api/library", json={"op": "parts", "item": "#1"}).json()["item"] == item["id"]   # by its place
    parts = opened["parts"]
    parts["loops"][0].update(to=1)
    text = client.post("/api/library", json={"op": "text", "parts": parts}).json()["text"]
    assert yaml.safe_load(text)["sweep"] == {"smu.V": {"from": 0, "to": 1, "step": 0.5}}
    kept = rt.library_view()
    edited = client.post("/api/queue", json={"op": "edit", "item": item["id"], "text": text})
    assert edited.status_code == 200, edited.text
    (now,) = [i for i in edited.json()["items"] if i["id"] == item["id"]]
    assert now["text"] == text.strip() + "\n" and now["status"] == "waiting" and now["library"] == entry["id"]
    assert rt.library_view() == kept                                  # Save into the queue keeps nothing in the library
    over = client.post("/api/library", json={"op": "text", "parts": dict(parts, loops=[dict(parts["loops"][0], to=99)])})
    refused = client.post("/api/queue", json={"op": "edit", "item": item["id"], "text": over.json()["text"]})
    assert refused.status_code == 400 and "outside" in refused.json()["error"]
    assert rt.queue.find(item["id"]).text == now["text"]              # as it was
    for bad in ({"loops": [{}]}, "nope"):
        assert client.post("/api/library", json={"op": "text", "parts": bad}).status_code == 400
    rt.queue.mark(rt.queue.find(item["id"]), "running")
    busy = client.post("/api/queue", json={"op": "edit", "item": item["id"], "text": text})
    assert busy.status_code == 400 and "is running" in busy.json()["error"]
    rt.queue.mark(rt.queue.find(item["id"]), "done")
    step = client.post("/api/queue", json={"op": "add", "text": "step: settle\nstabilize: { read: tc.T_sample, "
                                                               "within: 0.05 }"}).json()["items"][-1]
    r = client.post("/api/library", json={"op": "parts", "item": step["id"]})
    assert r.status_code == 400 and r.json()["error"] == "settle is a step, not a scan"
    assert client.post("/api/library", json={"op": "parts", "item": "nope"}).status_code == 404
    fields = client.post("/api/queue", json={"op": "fields", "item": step["id"]}).json()
    assert fields == {"item": step["id"], "kind": "step",
                      "fields": {"step": "settle", "stabilize": {"read": "tc.T_sample", "within": 0.05}}}
    assert client.post("/api/queue", json={"op": "fields", "item": "nope"}).status_code == 404
    assert len(rt.queue.items) == 2                                   # fields and parts changed nothing
