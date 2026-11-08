"""Procedures (SOPs), every version kept (phase 3 of the ELN plan, 2026-11-07): Kelvin's steps run through the gate
with the procedure, version and step in their runs' meta and the person's changes kept as deviations; steps by hand
are recorded naming them; procedure_status says where a sample is."""
import pytest

from labharness import rundata
from labharness.procedures import ProcedureError, parse, with_changes

pytest.importorskip("mcp")

SOP = """procedure: cool-and-check
description: Cool the sample and check its gate before any sweep.
steps:
  - name: load
    by: hand
    do: Load the chip and check pins 1-8 to ground with the multimeter.
    expect: below 10 kOhm
  - name: leakage
    by: kelvin
    plan: |
      plan: gate_leak
      setup: {smu.output: on}
      sweep: {smu.V: {from: 0, to: 0.2, step: 0.1}}
      measure: [smu.I]
      after: {smu.V: 0}
    expect: max(abs(smu.I)) < 1
  - name: sweep
    by: kelvin
    plan: |
      plan: gate_sweep
      sweep: {smu.V: {from: 0, to: 1, step: 0.5}}
      measure: [lockin.X]
"""


@pytest.fixture
def rt(tmp_path, bench):
    from test_remove_instrument import LAB, runtime_on
    return runtime_on(tmp_path, LAB, answers=[True, True])


@pytest.fixture
def bench():
    from labharness.adapters import set_resource_manager
    from test_hardware import Bus, Device
    bus = Bus({"GPIB0::8::INSTR": Device("srs/sr860", name="sr860"), "GPIB0::24::INSTR": Device("keithley/2400")})
    set_resource_manager(bus)
    yield bus
    set_resource_manager(None)


def test_a_procedure_is_kept_by_version_and_checked_as_it_is_written(rt):
    said = rt.save_procedure(SOP)
    assert said.startswith("kept procedure cool-and-check v1:\n  1 load (by hand): Load the chip"), said
    assert "2 leakage (Kelvin): plan gate_leak; expect max(abs(smu.I)) < 1" in said
    assert rt.save_procedure(SOP) == "procedure cool-and-check v1 already says that"
    assert rt.save_procedure(SOP.replace("to: 0.2", "to: 0.3")).startswith("kept procedure cool-and-check v2:")
    shown = rt.procedures("cool-and-check")
    assert shown.startswith("procedure cool-and-check v2: Cool the sample") and "versions: v1 " in shown
    assert "v2 " in shown and "to: 0.2" not in shown
    assert "cool-and-check v2: Cool the sample and check its gate before any sweep. (3 steps)" in rt.procedures()
    assert "to: 0.2" in rt._procedures().text("cool-and-check", 1)[0]   # the earlier version as it was written
    for bad, why in (("procedure: x\n", "a procedure has steps"),
                     ("steps: []\n", "a procedure starts with procedure"),
                     ("procedure: x\nsteps:\n  - {name: a, by: robot}\n", "by is hand"),
                     ("procedure: x\nsteps:\n  - {name: a, by: kelvin}\n", "it needs a plan"),
                     ("procedure: x\nsteps:\n  - {name: a, by: hand}\n", "do says what"),
                     ("procedure: x\nsteps:\n  - {name: a, by: hand, do: b}\n  - {name: A, by: hand, do: c}\n",
                      "two steps are called A")):
        with pytest.raises(ProcedureError, match=why):
            parse(bad)


def test_a_change_from_the_steps_plan_is_a_deviation():
    text, devs = with_changes("plan: p\nsweep: {smu.V: {from: 0, to: 2, step: 0.1}}\n", {"sweep.smu.V.to": 3})
    assert devs == ["sweep.smu.V.to: 2 → 3"] and "to: 3" in text
    with pytest.raises(ProcedureError, match="has no setup.smu.I"):
        with_changes("plan: p\nsweep: {smu.V: {from: 0, to: 2}}\n", {"setup.smu.I": 1})


def test_kelvins_step_runs_with_the_procedure_in_its_run_and_the_person_records_theirs(rt):
    rt.save_procedure(SOP)
    assert rt.run_procedure_step("cool-and-check", "load").startswith("not run: step load of cool-and-check is done by "
                                                                      "hand")
    said = rt.record_step("loaded chip 2, pins 1-8 below 3 kOhm", sample="chip 2", procedure="cool-and-check",
                          step="load", deviation="pin 5 not checked: no wire")
    assert said.endswith("(cool-and-check v1, step load): loaded chip 2, pins 1-8 below 3 kOhm; deviation: pin 5 not "
                         "checked: no wire"), said
    assert rt.record_step("x", procedure="cool-and-check", step="cool").startswith(
        "not recorded: procedure cool-and-check has no step 'cool' (its steps: load, leakage, sweep)")
    said = rt.run_procedure_step("cool-and-check", "leakage", {"sweep.smu.V.to": 0.3})
    assert said.startswith("procedure cool-and-check v1, step leakage; deviations: sweep.smu.V.to: 0.2 → 0.3"), said
    assert said.rstrip().endswith("check max(abs(smu.I)) < 1: passed"), said
    run = [e for e in rt.gate.log.events() if e["type"] == "procedure_step"][-1]["run"]
    meta = rundata.meta(rt._run_dir(run))
    assert meta["procedure"] == {"name": "cool-and-check", "version": 1, "step": "leakage",
                                 "deviations": ["sweep.smu.V.to: 0.2 → 0.3"]}
    status = rt.procedure_status("cool-and-check", "all")
    assert "1 load (by hand): " in status and "loaded chip 2" in status and "deviation: pin 5 not checked" in status
    assert "2 leakage (Kelvin): " in status and "run %s done, check passed; deviations: sweep.smu.V.to" % run in status
    assert status.rstrip().endswith("next: sweep (Kelvin)")
    assert "1 load (by hand): not yet" in rt.procedure_status("cool-and-check", "D9")
