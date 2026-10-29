"""Card techniques (techniques.py): hardware-timed, buffered measurements a card declares and a plan runs, the buffer
kept as traces. Cyclic voltammetry on labsim's Keithley 2450 run as a potentiostat (tools/labsim, models/
potentiostat.py): a reversible couple whose peaks a real CV would show. Field test P12: no hardware-timed techniques
(CV, EIS, scope bursts) kept electrochemistry out."""
import math
import threading
import time

import pytest

from labharness import EventLog, RunEngine, RunStore, SafetyGate, ScriptedApprover, builtin_registry
from labharness.cards import CardError, parse_card
from labharness.plans import PlanError, load_plan
from labsim_bench import advance, labsim_bench, labsim_offline  # noqa: F401  (the fixtures)

LAB = """lab: labsim electrochemistry
instruments:
  pstat:
    card: keithley/2450
    address: "USB0::0x05E6::0x2450::04412345::INSTR"
    limits: { V: [-1, 1], compliance_I: [1.0e-9, 1.0e-3] }
%s"""

CV = """
plan: cv_ferrocene
setup: { pstat.compliance_I: 1.0e-3, pstat.nplc: 0.01, pstat.output: on }
technique: { pstat.cv: { start: -0.2, vertex: 0.6, rate: %s, step: 0.002 } }
after: { pstat.output: off }
"""

F, R, T = 96485.33212, 8.314462618, 298.15


def randles_sevcik(rate):
    """The bench's cell: 1 mM, D 2.4e-5 cm2/s, 0.0707 cm2 (tools/labsim/benches/echem.yaml)."""
    return 0.4463 * F * 0.0707e-4 * 1.0 * math.sqrt(F * rate * 2.4e-9 / (R * T))


def lab_on(tmp_path, extra=""):
    from labharness.virtual_lab import build_lab
    (tmp_path / "lab.yaml").write_text(LAB % extra, encoding="utf-8")
    return build_lab(tmp_path / "lab.yaml", partial=True)


def engine_for(lab, tmp_path, answers):
    approver = ScriptedApprover(answers)
    gate = SafetyGate(lab, approver, EventLog(tmp_path / "events.jsonl", lab.clock))
    return RunEngine(gate, RunStore(tmp_path / "runs")), gate, approver


def peaks(row):
    """The anodic and cathodic peak currents and where they are, from a CV row (I against E, there and back)."""
    e, i = row["pstat.cv.I.x"], row["pstat.cv.I"]
    half = len(e) // 2
    up = max(range(half), key=lambda k: i[k])
    down = min(range(half, len(e)), key=lambda k: i[k])
    return i[up], e[up], i[down], e[down]


# ------------------------------------------------------------------ the card

def test_kelvins_2450_card_has_its_sweeps_as_techniques():
    card = builtin_registry().get("keithley/2450")
    assert sorted(card.techniques) == ["cv", "lsv"]
    cv = card.techniques["cv"]
    values, problems = cv.values({"start": -0.2, "vertex": 0.6, "rate": 0.1})
    assert problems == [] and values["cycles"] == 1 and cv.seconds(values) == pytest.approx(16.0)
    assert cv.render(cv.arm[1], values, cv.worked_out(values)) == \
        ':SOURce:SWEep:VOLTage:LINear:STEP -0.2, 0.6, 0.002, 0.02, 1, BEST, OFF, ON, "defbuffer1"'
    assert cv.params["start"].drive == "V" and cv.params["rate"].rate_of == "V" and cv.x == "E"
    assert cv.values({"start": 0, "vertex": 1, "cycles": 0})[1] == ["cv.cycles 0 is outside its limits [1, 100] "
                                                                    "(the card's)"]
    assert "cv.vertex: give it a value" in cv.values({"start": 0})[1][0]


BASE = {"card": 1, "id": "acme/pstat", "model": "ACME potentiostat", "kind": "potentiostat", "status": "draft",
        "parameters": {"E": {"kind": "drive", "unit": "V", "set": "SOUR:VOLT {value}", "get": "SOUR:VOLT?",
                             "limits": [-2, 2], "safe": 0},
                       "out": {"kind": "drive", "type": "bool", "set": "OUTP {value}", "get": "OUTP?", "safe": "off",
                               "enables": ["E"]}}}
GOOD = {"parameters": {"lo": {"drive": "E"}, "hi": {"drive": "E"}, "rate": {"unit": "V/s", "limits": [0.001, 1]}},
        "duration": "2 * abs(hi - lo) / rate", "arm": ["SWE:LIM {lo},{hi}", "SWE:RATE {rate}"],
        "trigger": "INIT", "wait": "*OPC?", "count": "TRAC:POIN?", "fetch": {"I": "TRAC:DATA? {n}"}}


def card_with(technique):
    return parse_card(dict(BASE, techniques={"sweep": dict(GOOD, **technique)}), "test")


@pytest.mark.parametrize("change, said", [
    ({"arm": ["SWE:LIM {lo},{top}"]}, "{top} is not declared"),
    ({"fetch": {"I": "TRAC:DATA? {n}"}, "count": None}, "{n} is not declared"),
    ({"arm": ["SOUR:VOLT {lo}"]}, "would set E"),
    ({"arm": ["OUTP ON"]}, "would set out"),
    ({"arm": ["SOUR:FUNC CURR"]}, "the source function"),
    ({"arm": ["SENS:CURR:PROT 1"]}, "a protection limit"),
    ({"parameters": {"lo": {"drive": "E"}, "hi": {"drive": "E"}, "rate": {"unit": "V/s"}}}, "has limits"),
    ({"parameters": {"lo": {"drive": "Q"}, "hi": {"drive": "E"}, "rate": {"limits": [0, 1]}}}, "not a numeric drive"),
    ({"duration": None}, "how long it takes"),
    ({"wait": None}, "say how Kelvin knows it is done"),
    ({"done": {"get": "STAT?", "match": "IDLE"}}, "not both"),
    ({"x": "E"}, "'E' is not one of what it fetches"),
    ({"duration": "2 * abs(hi - lo) / speed"}, "speed is not one of its parameters"),
    ({"trigger": None}, "a command, or a list of commands"),
    ({"parameters": {"lo": {"drive": "E", "limits": [1, 0]}, "hi": {"drive": "E"}, "rate": {"limits": [0, 1]}}},
     "low < high"),
    ({"surprise": 1}, "techniques.sweep.surprise: unknown"),
])
def test_a_techniques_card_is_checked_when_it_is_read(change, said):
    change = {k: v for k, v in change.items()}
    tech = {k: v for k, v in dict(GOOD, **change).items() if v is not None}
    with pytest.raises(CardError) as e:
        parse_card(dict(BASE, techniques={"sweep": tech}), "test")
    assert any(said in err for err in e.value.errors), e.value.errors


def test_a_technique_is_named_apart_from_the_cards_own():
    assert card_with({}).techniques["sweep"].trigger == ["INIT"]
    with pytest.raises(CardError, match="a lower-case name of its own"):
        parse_card(dict(BASE, techniques={"out": GOOD}), "test")


def test_a_technique_never_resets_nor_sends_a_blocked_command():
    with pytest.raises(CardError, match="resets the instrument"):
        card_with({"arm": ["*RST", "SWE:LIM {lo},{hi}"]})
    with pytest.raises(CardError, match="resets the instrument"):
        parse_card(dict(BASE, raw={"reset": ["SYST:PRES"]}, techniques={"sweep": dict(GOOD, arm=["SYST:PRES"])}), "t")
    with pytest.raises(CardError, match="is blocked on this card"):
        parse_card(dict(BASE, raw={"blocked": ["SWE:RATE"]}, techniques={"sweep": GOOD}), "test")


def test_a_technique_that_only_listens_runs_without_asking(tmp_path):
    """A scope-like burst capture (drives: false), polled with done: until its status says so, its points counted
    then fetched; on the virtual lab's source-meter, with a stand-in for the capture's commands."""
    import dataclasses
    from labharness import build_virtual_lab, techniques
    from labharness.clock import SimClock
    lab = build_virtual_lab(clock=SimClock(), seed=1)
    smu = lab.instruments["smu"]
    errors = []
    found = techniques.parse({"burst": {
        "label": "Burst capture", "drives": False,
        "parameters": {"points": {"type": "int", "limits": [2, 1000], "default": 10},
                       "every": {"unit": "s", "limits": [1.0e-6, 1], "default": 1.0e-3}},
        "duration": "points * every", "arm": ["CAPT:POIN {points}", "CAPT:INT {every}"], "trigger": ["CAPT:STAR"],
        "done": {"get": "CAPT:STAT?", "match": "^DONE"}, "count": "CAPT:COUN?",
        "fetch": {"y": {"get": "CAPT:DATA? 1,{n}", "unit": "V"}}, "abort": ["CAPT:STOP"]}},
        smu.card.params, {}, smu.card.actions, lambda path, msg: errors.append((path, msg)))
    assert errors == []
    smu.card = dataclasses.replace(smu.card, techniques=found)
    twin = smu.backend
    twin.simulated = False   # Kelvin takes it for a real instrument
    sent, polls = [], []
    write, query = twin.write, twin.query

    def capture_write(cmd):
        sent.append(cmd) if cmd.startswith("CAPT") else write(cmd)

    def capture_query(cmd):
        if cmd == "CAPT:STAT?":
            polls.append(cmd)
            return "RUNNING" if len(polls) < 3 else "DONE"
        if cmd == "CAPT:COUN?":
            return "5"
        if cmd == "CAPT:DATA? 1,5":
            return "0.1,0.2,0.3,0.4,0.5"
        return query(cmd)
    twin.write, twin.query = capture_write, capture_query
    approver = ScriptedApprover([])                                       # nothing drives: nobody is asked
    report = RunEngine(SafetyGate(lab, approver)).run("plan: burst\ntechnique: { smu.burst: { points: 5 } }\n")
    assert report.status == "done", report.message
    assert sent == ["CAPT:POIN 5", "CAPT:INT 0.001", "CAPT:STAR"] and len(polls) == 3
    assert report.rows[0]["smu.burst.y"] == [0.1, 0.2, 0.3, 0.4, 0.5] and approver.requests == []


def test_a_plan_runs_a_technique_and_sweeps_its_parameters():
    plan = load_plan("""
plan: cv_rates
technique: { pstat.cv: { start: -0.2, vertex: 0.6 } }
sweep: { pstat.cv.rate: { values: [0.01, 0.1] } }
""")
    assert [(t.key, t.values) for t in plan.techniques] == [("pstat.cv", {"start": -0.2, "vertex": 0.6})]
    assert plan.axes[0].kind == "technique" and plan.axes[0].key == "pstat.cv.rate" and plan.requires == ["pstat"]
    with pytest.raises(PlanError, match="technique.pstat.cv: a mapping"):
        load_plan("plan: x\ntechnique: { pstat.cv: [1, 2] }")


# ------------------------------------------------------------------ on labsim: a potentiostat Kelvin takes for real

def test_labsims_potentiostat_sweeps_into_its_buffer_as_its_manual_says(labsim_offline):
    from labsim.instrument import CommandError
    bench = labsim_offline("echem")
    pstat = bench.instruments["pstat"]
    pstat.handle(":OUTPut ON;:SENSe:CURRent:NPLCycles 0.01")
    pstat.handle(':SOURce:SWEep:VOLTage:LINear -0.2, 0.6, 401, 0.0198333, 2, BEST, OFF, ON, "defbuffer1"')
    pstat.now = 1000.0
    pstat.handle(":INITiate")
    assert pstat.handle(':TRACe:ACTual? "defbuffer1"') == "0"            # it fills as it sweeps
    pstat.now += 4.0
    assert pstat.handle(':TRACe:ACTual? "defbuffer1"') == "200"          # 20 ms a point
    with pytest.raises(CommandError):                                    # not there yet
        pstat.handle(':TRACe:DATA? 1, 1604, "defbuffer1", READing')
    pstat.now += 60.0
    assert pstat.handle(':TRACe:ACTual? "defbuffer1"') == "1604"          # two dual sweeps of 401 points
    e = [float(x) for x in pstat.handle(':TRACe:DATA? 1, 802, "defbuffer1", SOURce').split(",")]
    i = [float(x) for x in pstat.handle(':TRACe:DATA? 1, 802, "defbuffer1", READing').split(",")]
    assert e[0] == pytest.approx(-0.2, abs=1e-4) and e[400] == pytest.approx(0.6, abs=1e-4) and e[-1] == \
        pytest.approx(-0.2, abs=1e-4)
    up = max(range(401), key=lambda k: i[k])
    assert i[up] - 20e-6 * 0.1 == pytest.approx(randles_sevcik(0.1), rel=0.05) and 0.22 < e[up] < 0.24
    pairs = pstat.handle(':TRACe:DATA? 1, 2, "defbuffer1", RELative, SOURce').split(",")
    assert [float(x) for x in pairs[::2]] == pytest.approx([0.0, 0.02], abs=1e-6)
    pstat.handle(':TRACe:CLEar "defbuffer1"')
    pstat.handle(":OUTPut OFF")
    pstat.handle(":INITiate")
    pstat.now += 70.0
    quiet = [abs(float(x)) for x in pstat.handle(':TRACe:DATA? 1, 1604, "defbuffer1", READing').split(",")]
    assert max(quiet) < 1e-10                                            # output off: the cell sees nothing
    pstat.handle(":INITiate")
    pstat.now += 1.0
    pstat.handle(":ABORt")
    assert int(pstat.handle(':TRACe:ACTual? "defbuffer1"')) < 1604 + 60   # stopped where it was


def test_cyclic_voltammetry_on_a_potentiostat(labsim_bench, tmp_path):
    bench = labsim_bench("echem")
    lab = lab_on(tmp_path)
    try:
        engine, gate, approver = engine_for(lab, tmp_path, [True])
        report = engine.run(CV % 1.0)
        assert report.status == "done", report.message
        card = approver.requests[0]
        text = "\n".join(card.items)
        assert card.title.startswith("Run plan cv_ferrocene: 1 points, ~") and "time not known" not in card.title
        assert "pstat.V: now 0 V, plan uses -0.2 … 600 mV" in text
        assert "At every point: pstat.cv (Cyclic voltammetry), run by the instrument on its own clock: start -200 mV, " \
               "vertex 600 mV, rate 1 V/s, step 2 mV, cycles 1; ~2 s each, ~2 s in all. It applies pstat.V itself; " \
               "Kelvin can't ramp or watch guards inside it: the instrument's own limit holds (pstat.compliance_I " \
               "1 mA)" in text, text
        assert "technique" not in " ".join(i for i in card.items if i.startswith("Not in"))
        row = report.rows[0]
        e, i, t = row["pstat.cv.I.x"], row["pstat.cv.I"], row["pstat.cv.t"]
        assert len(e) == len(i) == len(t) == 802 and row["pstat.cv.t.x"] == e and "pstat.cv.E" not in row
        rate = 0.002 / (t[1] - t[0])                                       # as the instrument stepped it
        ip_a, ep_a, ip_c, ep_c = peaks(row)
        charging = 20e-6 * rate
        assert ip_a - charging == pytest.approx(randles_sevcik(rate), rel=0.08)   # Randles-Sevcik
        assert 0.05 < ep_a - ep_c < 0.068 and (ep_a + ep_c) / 2 == pytest.approx(0.2, abs=0.01)   # 59 mV, at E0
        assert ip_c < 0
        assert lab.read("pstat", "output") is False                        # its after: step
        from pathlib import Path
        from labharness import rundata
        kept = rundata.load(Path(report.path))
        assert len(kept.rows[0]["pstat.cv.I"]) == 802 and kept.rows[0]["pstat.cv.I.x"][:3] == e[:3]
        sent = [x for _, d, x in bench.ctl("log", "pstat", 400)["lines"] if d == "<"]
        assert any(x.startswith(":SOURce:SWEep:VOLTage:LINear:STEP -0.2, 0.6, 0.002") for x in sent)
        assert "automatic analysis failed" not in " ".join(report.warnings)
    finally:
        lab.close()


def test_the_scan_rate_swept_shows_a_diffusing_couple(labsim_bench, tmp_path):
    labsim_bench("echem")
    lab = lab_on(tmp_path)
    try:
        engine, gate, approver = engine_for(lab, tmp_path, [True])
        report = engine.run("""
plan: cv_rates
setup: { pstat.compliance_I: 1.0e-3, pstat.nplc: 0.01, pstat.output: on }
technique: { pstat.cv: { start: -0.2, vertex: 0.6, step: 0.002 } }
sweep: { pstat.cv.rate: { values: [0.5, 2.0] } }
after: { pstat.output: off }
""")
        assert report.status == "done", report.message
        assert "rate swept 500 mV/s to 2 V/s" in "\n".join(approver.requests[0].items)
        assert [r["pstat.cv.rate"] for r in report.rows] == [0.5, 2.0]
        slow, fast = (peaks(r)[0] - 20e-6 * 0.002 / (r["pstat.cv.t"][1] - r["pstat.cv.t"][0]) for r in report.rows)
        assert fast / slow == pytest.approx(2.0, rel=0.1)                  # i_p ~ sqrt(rate): diffusion-limited
    finally:
        lab.close()


def test_a_level_beyond_the_limits_is_refused_before_anything_runs(labsim_bench, tmp_path):
    labsim_bench("echem")
    lab = lab_on(tmp_path, "    rates: { V: 0.5 }\n")
    try:
        engine, gate, approver = engine_for(lab, tmp_path, [])
        far = engine.run((CV % 0.1).replace("vertex: 0.6", "vertex: 1.5"))
        assert far.status == "refused" and "pstat.V 1.5 V is outside the lab config's limits [-1, 1] V" in far.message
        assert "(vertex, a level of pstat.V)" in far.message
        fast = engine.run(CV % 1.0)
        assert fast.status == "refused" and "pstat.cv.rate 1 V/s is faster than pstat.V's rate limit, 0.5 V/s (the " \
            "lab config's)" in fast.message
        assert approver.requests == [] and lab.read("pstat", "output") is False
    finally:
        lab.close()


def test_a_technique_on_a_guarded_drive_or_with_hands_on_is_refused(labsim_bench, tmp_path):
    labsim_bench("echem")
    lab = lab_on(tmp_path, "    guards: { V: [{read: pstat.I, max_abs: 1.0e-4}] }\n")
    try:
        engine, gate, approver = engine_for(lab, tmp_path, [])
        r = engine.run(CV % 0.5)
        assert r.status == "refused" and "pstat.V has guards (pstat.I) that nothing could watch inside cv" in r.message
    finally:
        lab.close()
    lab = lab_on(tmp_path)
    try:
        lab.hands_on = {"by": "Ada", "since": "10:00"}
        engine, gate, approver = engine_for(lab, tmp_path, [])
        r = engine.run(CV % 0.5)
        assert r.status == "refused" and "Ada has hands on the sample" in r.message
    finally:
        lab.close()


def test_stop_ends_a_technique_at_once_and_takes_the_cell_to_safe(labsim_bench, tmp_path):
    bench = labsim_bench("echem")
    lab = lab_on(tmp_path)
    try:
        engine, gate, approver = engine_for(lab, tmp_path, [True])
        out = {}
        worker = threading.Thread(target=lambda: out.update(r=engine.run(CV % 0.02)))   # 80 s of sweep
        worker.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = next(i["status"] for i in bench.ctl("status")["instruments"] if i["name"] == "pstat")
            if status.get("sweep", {}).get("running"):
                break
            time.sleep(0.1)
        time.sleep(0.5)
        began = time.monotonic()
        gate.request_abort()
        worker.join(30)
        assert not worker.is_alive() and time.monotonic() - began < 10
        r = out["r"]
        assert r.status == "aborted" and "stop" in r.message
        sent = [x for _, d, x in bench.ctl("log", "pstat", 400)["lines"] if d == "<"]
        assert ":ABORt" in sent                                             # the instrument stopped its sweep
        assert lab.read("pstat", "output") is False                        # and the cell is off
    finally:
        lab.close()


def test_a_queue_of_voltammograms_asks_once(labsim_bench, tmp_path):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    labsim_bench("echem")
    lab = lab_on(tmp_path)
    rt = LabRuntime(lab, ScriptedApprover([True]), tmp_path / "data", None)   # a second ask would fail the test
    try:
        for rate in (1.0, 2.0):
            rt.queue_add((CV % rate).replace("cv_ferrocene", "cv_%g" % rate))
        out = rt.queue_start()
        assert out.startswith("queue started"), out
        rt._queue_thread.join(120)
        assert [i.status for i in rt.queue.items] == ["done", "done"], [(i.name, i.message) for i in rt.queue.items]
        card = str(rt.approver.requests[0])
        assert "#1 cv_1: At every point: pstat.cv (Cyclic voltammetry)" in card
        assert "#2 cv_2: At every point: pstat.cv (Cyclic voltammetry), run by the instrument on its own clock: " \
               "start -200 mV, vertex 600 mV, rate 2 V/s" in card
        assert rt.approver.requests[0].title.startswith("Run the queue: 2 scans, ~")
    finally:
        lab.close()


def test_auto_mode_never_runs_one_and_a_dry_run_takes_its_time(labsim_bench, tmp_path):
    labsim_bench("echem")
    lab = lab_on(tmp_path)
    try:
        engine, gate, approver = engine_for(lab, tmp_path, [False])
        gate.auto = True
        r = engine.run(CV % 0.5)
        assert r.status == "declined"
        assert "Auto mode does not cover this run" in approver.requests[0].items[-1]
        assert "it runs pstat.cv, which drives the sample on the instrument's own clock" in approver.requests[0].items[-1]
        sim = engine.simulate(CV % 0.5)
        assert sim.status == "done", sim.message
        assert sim.duration_s == pytest.approx(3.2, abs=0.5)               # the sweep's time, on the twin's clock
        assert "technique cv" in gate.describe("pstat") and "rate V/s [100 µV/s, 10 V/s]" in gate.describe("pstat")
    finally:
        lab.close()
