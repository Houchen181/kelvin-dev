"""Repro (mechanism only): Kelvin's stand-in replaces ResourceManager.open_resource and nothing else that opens a
session, so a second name for it still opens the instrument itself.

Defect: data/scriptshim/kelvin_shim.py:434-465 (_patch_pyvisa sets rm_class.open_resource, resource_info and __new__).
pyvisa 1.6 to 1.10 ends its ResourceManager class with

        #: For backwards compatibility
        get_instrument = open_resource

a class attribute bound to the ORIGINAL function, which the stand-in's assignment to open_resource does not change
(1.11 made it a method that calls self.open_resource; 1.12 removed it). PyMeasure up to 0.8 opens every instrument
with manager.get_instrument(...). Kelvin's own reading of scripts knows the call (scripts.py:501, 554), so the
approval card says "every command goes through the safety gate as it is sent".

NOT verified against a real pyvisa <= 1.10: none is on this computer (it has 1.16.2, which has no get_instrument)
and the audit has no internet. The module below is a stand-in shaped like that class; what it "opens" is a file,
past.txt, that keeps what would have gone to the instrument. The same holds in any pyvisa for the other ways to a
session the stand-in leaves alone: open_bare_resource, Resource(rm, name).open(), rm.visalib.open(...).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import fakes, make_lab, output_of, run_script, write_script

LAB = """
lab: Alias lab
instruments:
  smu:
    card: virtual/source-meter
    address: GPIB0::24::INSTR
    limits: { V: [-10, 10] }
    rates: { V: 10 }
"""

OLD_PYVISA = '''
class _Session(object):
    """What pyvisa itself would open: here, a file that keeps what the instrument would have been sent."""
    def __init__(self, name):
        self.name = name
    def write(self, message):
        with open("past.txt", "a") as f:
            f.write("%s <- %s\\n" % (self.name, message))
    def query(self, message):
        self.write(message)
        return "0"
    def close(self):
        pass


class ResourceManager(object):
    def __init__(self, *args, **kwargs):
        pass

    def open_resource(self, resource_name, **kwargs):
        return _Session(resource_name)

    #: For backwards compatibility
    get_instrument = open_resource
'''

SCRIPT = '''
import pyvisa

rm = pyvisa.ResourceManager()
smu = rm.get_instrument("GPIB0::24::INSTR")
print("what the script holds:", type(smu).__name__)
top = float("150")
smu.write("OUTP ON")
smu.write("SOUR:VOLT %g" % top)
'''


def main():
    lab = make_lab(LAB)
    with tempfile.TemporaryDirectory(prefix="audit-alias-") as tmp:
        d = fakes(tmp)
        (d / "pyvisa" / "__init__.py").write_text(OLD_PYVISA)
        folder = Path(tmp) / "scripts"
        folder.mkdir()
        write_script(folder, "sweep.py", SCRIPT)
        gate, approver, prep, report = run_script(lab, folder, Path(tmp) / "runs", "sweep.py", answers=[True])
        print("approval card:")
        for line in approver.requests[0].items[1:-1]:
            print("   ", line)
        print("asked while it ran:", [q.title for q in approver.requests[1:]])
        print("run:", report.status, "|", report.message)
        for line in output_of(report).splitlines():
            print("    out:", line)
        entries = [json.loads(ln) for ln in (Path(report.path) / "commands.jsonl").read_text(encoding="utf-8").splitlines()]
        print("commands the gate saw:", [e.get("command") for e in entries])
        past = folder / "past.txt"
        print("what went past Kelvin (past.txt):", past.read_text().splitlines() if past.exists() else "nothing")
        print("smu.V in Kelvin's lab:", lab.instruments["smu"].read("V"), "| output:", lab.instruments["smu"].read("output"))


if __name__ == "__main__":
    main()
