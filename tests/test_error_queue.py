"""Error-queue hygiene and fixed ranges (field tests: errors left in a queue were blamed on later safe moves and the
queue overflowed (-350); device protection left a 2400 on its 21 V range under a ±60 V limit, so the first sweep
stopped at -21 V, and nothing checked a target against a fixed range)."""
import pytest

from labharness import load_plan, protection


def test_errors_already_queued_are_not_blamed_on_the_next_change(lab, make_gate):
    gate, _ = make_gate([])
    twin = lab.instruments["smu"].backend
    twin.errors += ['-113,"Undefined header"', '-410,"Query INTERRUPTED"']
    r = gate.set("smu", "nplc", 2)
    assert r.ok and "older errors in its queue, cleared first" in r.message and "-113" in r.message
    assert twin.errors == [] and any(e["type"] == "stale_errors" for e in gate.log.events())
    real = twin._write_one
    twin._write_one = lambda cmd: real(cmd) or twin.errors.append('-221,"Settings conflict"')
    twin.errors.append('-113,"Undefined header"')
    r = gate.set("smu", "nplc", 3)                                      # its own error is still its own
    assert r.action == "error" and "-221" in r.message and "-113" not in r.message.split("reported")[1]


def test_a_standing_error_status_is_not_taken_for_a_new_one(lab, make_gate):
    gate, _ = make_gate([])
    twin = lab.instruments["smu"].backend
    real = twin._answer
    twin._answer = lambda cmd: "1001" if cmd.upper().startswith("SYST") else real(cmd)   # a status reading doesn't clear
    r = gate.set("smu", "nplc", 2)
    assert r.ok and "it shows a standing error from before: 1001" in r.message


def test_the_monitor_reads_the_whole_queue(lab, make_gate):
    from labharness.monitor import Monitor
    gate, _ = make_gate([])
    twin = lab.instruments["smu"].backend
    twin.simulated = False
    twin.errors += ['-222,"Data out of range"', '-113,"Undefined header"', '-222,"Data out of range"']
    monitor = Monitor(lab, gate, lambda *a: None)
    monitor.tick()
    assert "smu reports an error: -222" in monitor.active()[0].text and "(and 1 more)" in monitor.active()[0].text
    assert twin.errors == []


def test_protection_sets_the_range_that_covers_the_limit(tmp_path):
    pytest.importorskip("mcp")
    from test_protection import mount, runtime
    rt = runtime(tmp_path)
    smu = rt.lab.instruments["smu"]
    assert rt.gate.set("smu", "V_range", 2).ok                         # someone chose it for resolution
    m = mount(rt, {"gates": [{"drive": "smu.V", "limit": 6}]})
    assert "smu.V_range 2 V → 20 V" in m["caps"] and smu.read("V_range") == 20
    assert rt.gate.set("smu", "output", True).ok and rt.gate.set("smu", "V", 5).ok   # the limits allow it: it runs


def test_a_target_beyond_a_fixed_range_is_refused_up_front(lab, make_engine):
    engine, gate, approver = make_engine([True, True])
    assert gate.set("smu", "V_autorange", False).ok and gate.set("smu", "V_range", 2).ok
    assert gate.set("smu", "output", True).ok
    r = gate.set("smu", "V", 5)
    assert r.action == "refused" and "smu.V 5 V is beyond its fixed range (2 V): set smu.V_range to 20 V first" \
        in r.message
    assert gate.set("smu", "V", 1.5).ok                                 # within it: as before
    gate.set("smu", "V", 0)
    sweep = "plan: wide\nsweep: { smu.V: {from: 0, to: 5, step: 1} }\nmeasure: [smu.I]\n"
    report = engine.run(sweep)
    assert report.status == "refused" and "beyond its fixed range (2 V)" in report.message
    plan = load_plan("plan: wide\nsetup: { smu.V_range: 20 }\nsweep: { smu.V: {from: 0, to: 5, step: 1} }\n"
                     "measure: [smu.I]\n")
    assert engine.validate(plan) == []                                  # the plan sets the range it needs first
    assert protection.range_for(lab.instruments["smu"].spec("V"), 5) == 20
