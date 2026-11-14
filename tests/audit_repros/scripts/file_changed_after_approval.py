"""Repro: the text that runs is not the text that was read ahead, approved and kept as the run's script.py.

Defect: scriptrun.py:2002-2003 (execute: `source_file` is None for a .py with no changed values and no version) and
scriptrun.py:1943-1947 (_command_line: without --source the child gets only the file's path), so the script's
Python reads the file from disk again as it starts (data/scriptshim/kelvin_shim.py:898-901). scriptrun.prepare read
it earlier (scriptrun.py:921): the read-ahead, the approval card, script.py in the run folder (scriptrun.py:2000) and
the record's script_sha256 (scriptrun.py:1995) are all of that earlier text.

Between the two reads lie the wait for the lab, the preflight and the person's answer to the approval card
(agent/runtime.py:8481 prepare ... 8645 gate.ask ... 8674 job.execute). The file is saved again in that gap here,
as a person does who reads the card, corrects the script in their editor and then presses Approve.
"""
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import fakes, make_lab, output_of, run_script, write_script
from labharness import rundata, scriptrun

LAB = """
lab: Script lab
instruments:
  smu:
    card: virtual/source-meter
    address: GPIB0::24::INSTR
    limits: { V: [-10, 10] }
    rates: { V: 10 }
"""

AS_APPROVED = '''
import pyvisa

VMAX = 1.0
smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
print("this is the text Kelvin read and the person approved; VMAX =", VMAX)
smu.write("OUTP ON")
for v in (0.5, VMAX):
    smu.write("SOUR:VOLT %g" % v)
    print(v, smu.query("MEAS:CURR?"))
smu.write("SOUR:VOLT 0")
smu.write("OUTP OFF")
'''

SAVED_MEANWHILE = '''
import pyvisa

VMAX = 0.8
smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
print("THIS IS THE FILE AS SAVED AFTER KELVIN READ IT; VMAX =", VMAX)
smu.write("OUTP ON")
for v in (0.2, 0.4, 0.6, VMAX):
    smu.write("SOUR:VOLT %g" % v)
    print(v, smu.query("MEAS:CURR?"))
smu.write("SOUR:VOLT 0")
smu.write("OUTP OFF")
'''


def main():
    with tempfile.TemporaryDirectory(prefix="audit-toctou-") as tmp:
        fakes(tmp)
        folder = Path(tmp) / "scripts"
        folder.mkdir()
        path = write_script(folder, "sweep.py", AS_APPROVED)
        lab = make_lab(LAB)

        def saved_again(prep):
            write_script(folder, "sweep.py", SAVED_MEANWHILE)

        gate, approver, prep, report = run_script(lab, folder, Path(tmp) / "runs", "sweep.py", answers=[True],
                                                  between=saved_again)
        print("the approval card the person answered:")
        for line in approver.requests[0].items:
            print("   ", line)
        print()
        print("run:", report.status, "|", report.message)
        print("what the script's Python printed (output.txt):")
        for line in output_of(report).splitlines():
            print("   ", line)
        run_dir = Path(report.path)
        kept = (run_dir / "script.py").read_text(encoding="utf-8")
        rec = rundata.load(run_dir, with_rows=False)
        print()
        print("script.py kept with the run (\"as it ran\") says VMAX = 1.0:", "VMAX = 1.0" in kept,
              "| says VMAX = 0.8:", "VMAX = 0.8" in kept)
        print("the record's script_sha256 is the fingerprint of the approved text:",
              rec.meta.get("script_sha256") == scriptrun.fingerprint(prep.source))
        print("the record's script_sha256 is the fingerprint of the text that ran:",
              rec.meta.get("script_sha256") == scriptrun.fingerprint(
                  scriptrun.source_of(path)))
        sets = [json.loads(ln) for ln in (run_dir / "commands.jsonl").read_text(encoding="utf-8").splitlines()]
        print("SOUR:VOLT values the gate saw:", [e["command"] for e in sets if str(e.get("command", "")).startswith("SOUR:VOLT")])


if __name__ == "__main__":
    main()
