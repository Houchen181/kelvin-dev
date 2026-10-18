"""Walk through the safety rules on the virtual lab.

    python examples/demo.py              # scripted answers to approval requests
    python examples/demo.py --ask        # you answer them

Writes the event log and run folders to ./demo_output/.
"""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from labharness import (ConsoleApprover, EventLog, RunEngine, RunStore, SafetyGate, ScriptedApprover,  # noqa: E402
                        build_virtual_lab, demo_envelope)

PLAN = """
plan: gate_sweep
preconditions: ["tc.T_sample < 2.5"]
setup: { lockin.amplitude: 0.05, lockin.tc: 0.1, smu.output: on }
sweep: { smu.V: {from: 0, to: 5, step: 0.25} }
settle: 0.5
measure: [lockin.X, smu.I]
after: { smu.V: 0 }
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ask", action="store_true", help="answer approval requests yourself")
    args = ap.parse_args()

    out = pathlib.Path("demo_output")
    lab = build_virtual_lab(seed=1)
    #            output on, V→3,  V→14, raw drive, plan
    approver = ConsoleApprover() if args.ask else ScriptedApprover([True, True, True, False, True], echo=True)
    gate = SafetyGate(lab, approver, EventLog(out / "events.jsonl", lab.clock), demo_envelope(lab))
    engine = RunEngine(gate, RunStore(out / "runs"))

    def step(title):
        print("\n## " + title)

    step("Preflight")
    print(", ".join(str(h) for h in engine.preflight()))

    step("Measurement and configuration run freely")
    print(gate.set("lockin", "tc", 0.3))
    print(gate.set("vna", "points", 801))
    print(gate.get("lockin", "X"))

    step("Inside the project envelope: runs without asking")
    print(gate.set("lockin", "amplitude", 0.05))

    step("Drive increases ask, then ramp")
    print(gate.set("smu", "output", True))
    print(gate.set("smu", "V", 3))

    step("Toward safe: runs, ramped")
    print(gate.set("smu", "V", 1))

    step("Beyond lab limits: refused")
    print(gate.set("smu", "V", 20))

    step("A guard trips during the ramp (the toy gate breaks down above 12 V)")
    print(gate.set("smu", "V", 14))

    step("Raw commands go through the same rules")
    print(gate.raw("smu", "SOUR:VOLT 2"))
    print(gate.raw("smu", "CAL:ZERO"))
    print(gate.raw("smu", "SENS:CURR:NPLC 2"))

    step("Interlock: no field while the sample is warm")
    lab.instruments["tc"].backend.state["T_sample"] = 25.0
    print(gate.set("magnet", "B", 1))
    lab.instruments["tc"].backend.state["T_sample"] = 1.6

    step("Plan: dry run on twins, one approval, run, log")
    report = engine.run(PLAN)
    print(report.dry_run)
    print(report)
    if report.rows:
        peak = max(report.rows, key=lambda r: r["lockin.X"])
        print("peak lockin.X = %.3g V at smu.V = %.2f V" % (peak["lockin.X"], peak["smu.V"]))
        print("saved to", report.path)

    step("Kill switch")
    for r in gate.safe_state():
        print(r)

    print("\n%d events in %s" % (len(gate.log.events()), out / "events.jsonl"))


if __name__ == "__main__":
    main()
