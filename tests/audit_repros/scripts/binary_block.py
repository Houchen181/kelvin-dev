"""Repro: a script's query_binary_values / write_binary_values sends its command text to the instrument without the
gate reading it: the text is checked whole, against blocked commands and drive setters only, so anything chained
after a first command with ';' goes out unchecked.

Defect: scriptrun.py:1740-1783 (handle: "query_binary" and "write_binary" call
`inst.backend.res.query_binary_values(command, ...)` / `write_binary_values(command, ...)` directly) after
scriptrun.py:1572-1587 (_classify: `b.match(command)`, `_setter(card, command)`, `gate._prefix(s).match(command)`,
each anchored at the start of the whole text; never split at ';', never passed to gate.raw).
So through these two calls: a drive set after a ';' is not checked against the limits, a reset does not bring the
drives to safe first, and a command the card doesn't list (which gate.raw asks a person about) is just sent.

The lab's smu is limited to +-10 V. The instrument is the stand-in from newline_chain.py (not hardware): Kelvin's
own VisaBackend on a fake VISA resource manager whose query_binary_values sends the message as pyvisa does.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import fakes, output_of, run_script, write_script
from newline_chain import build

SCRIPT = '''
import pyvisa

smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
try:
    smu.write("SOUR:VOLT 150")            # the gate reads this one: beyond the lab's limit
except Exception as e:
    print("plain write refused:", type(e).__name__, str(e)[:110])
'''

SCRIPT_BINARY = '''
import pyvisa

smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
print("V at the start:", smu.query("SOUR:VOLT?"), " output:", smu.query("OUTP?"))
data = smu.query_binary_values("SOUR:VOLT:RANG?;:SOUR:VOLT 150;:OUTP ON", datatype="f")
print("V after query_binary_values:", smu.query("SOUR:VOLT?"), " output:", smu.query("OUTP?"))
smu.write_binary_values("SENS:CURR:NPLC 1;:SENS:CURR:PROT 1;:TRAC:DATA ", [1.0, 2.0])
print("compliance after write_binary_values:", smu.query("SENS:CURR:PROT?"))
'''


def run(name, text):
    lab, instrument = build()
    with tempfile.TemporaryDirectory(prefix="audit-binary-") as tmp:
        fakes(tmp)
        folder = Path(tmp) / "scripts"
        folder.mkdir()
        write_script(folder, name, text)
        gate, approver, prep, report = run_script(lab, folder, Path(tmp) / "runs", name, answers=[True])
        print("--- %s ---" % name)
        print("approval card:")
        for line in approver.requests[0].items[1:-1]:
            print("   ", line)
        print("asked after that:", [q.title for q in approver.requests[1:]])
        print("run:", report.status, "|", report.message)
        for line in output_of(report).splitlines()[-6:]:
            print("    out:", line)
        for line in (Path(report.path) / "commands.jsonl").read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            print("    gate:", {k: e[k] for k in ("command", "action", "ok", "message") if k in e})
        print("the instrument after the run: V = %s V (limit 10), output = %s, compliance = %s A (lab card limit 1.05)"
              % (instrument.state["V"], instrument.state["output"], instrument.state["compliance_I"]))
        print("left away from safe, as the report says:", report.left, "| clean-up:", report.cleanup)
        print()


if __name__ == "__main__":
    run("plain.py", SCRIPT)
    run("binary.py", SCRIPT_BINARY)
