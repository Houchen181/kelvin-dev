"""Repro: a script that sends an AMI 430 exactly what Kelvin's own card sends, "CONF:FIELD:TARG 1.0;RAMP", has the
chain split before the gate sees it, so the gate does not know its own plain setting.

Defect: scriptrun.py:1506 (_command_to: `for part in split_compound(text) or [text]`, then gate.raw(part) for each).
gate.raw (gate.py:351-353) keeps a chain whole when it is a card's own setting ("an AMI 430's TARG {value};RAMP:
checked as one"), but it only ever gets the pieces. The read-ahead does the same (scriptrun.py:752, _note_command).
So for the script's chain: the first piece, "CONF:FIELD:TARG 1.0", is no plain setting of the card (asked of a
person as an unlisted drive command, or refused outright when an interlock guards the field); the second piece is
re-written by split_compound's SCPI path rule to "CONF:FIELD:RAMP", which is not the RAMP the script sent.
The same text through gate.raw directly (the agent's send_raw) is the card's setting, checked and ramped.

A simulated AMI 430 (Kelvin's ami/430 card on a generic twin). No hardware.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Recorder, fakes, make_lab, output_of, run_script, wire_tap, write_script
from labharness.eventlog import EventLog
from labharness.gate import SafetyGate

LAB = """
lab: Magnet lab
instruments:
  magnet:
    card: ami/430
    address: TCPIP0::10.0.0.7::7180::SOCKET
    limits: { B_target: [-3, 3] }
"""

SCRIPT = '''
import pyvisa

mag = pyvisa.ResourceManager().open_resource("TCPIP0::10.0.0.7::7180::SOCKET")
mag.write("CONF:FIELD:TARG 1.0;RAMP")
print("target now:", mag.query("FIELD:TARG?"))
'''


def main():
    print("=== the same text through gate.raw (send_raw) ===")
    lab = make_lab(LAB)
    sent = wire_tap(lab)
    approver = Recorder()
    gate = SafetyGate(lab, approver, EventLog(None, lab.clock))
    r = gate.raw("magnet", "CONF:FIELD:TARG 1.0;RAMP")
    print("result:", r.ok, r.action, "|", r.message)
    print("asked:", [(q.title, q.items[:1]) for q in approver.requests])
    print("sent to the instrument:", [c for _, kind, c in sent if kind == "write"])

    print()
    print("=== the same text from a lab script ===")
    lab = make_lab(LAB)
    sent = wire_tap(lab)
    with tempfile.TemporaryDirectory(prefix="audit-chain-") as tmp:
        fakes(tmp)
        folder = Path(tmp) / "scripts"
        folder.mkdir()
        write_script(folder, "field.py", SCRIPT)
        gate, approver, prep, report = run_script(lab, folder, Path(tmp) / "runs", "field.py")   # every card: yes
        print("approval card:")
        for line in approver.requests[0].items[1:-1]:
            print("   ", line)
        print("asked while it ran:")
        for q in approver.requests[1:]:
            print("   ", q.title, "|", " ".join(q.items)[:230])
        print("run:", report.status, "|", report.message, "| warnings:", report.warnings)
        for line in output_of(report).splitlines()[-2:]:
            print("    out:", line)
        for line in (Path(report.path) / "commands.jsonl").read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            print("    gate:", {k: e[k] for k in ("command", "action", "ok") if k in e})
        print("sent to the instrument:", [c for _, kind, c in sent if kind == "write"])


if __name__ == "__main__":
    main()
