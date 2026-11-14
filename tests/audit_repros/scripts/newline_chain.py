"""Repro: a command with a line end inside it is checked as ONE command and sent as it is, so the instrument
carries out a second command the gate never looked at.

Defect: scriptrun.py:1501-1517 (_command_to strips only the ends and splits at ';') -> gate.py:343-362 (raw:
split_compound splits at ';' only) -> gate.py:1995-1998 (_raw_one: first word has '?' -> a query, sent whole) and
gate.py:2013/2044-2050 (first word *CLS/*OPC... -> "harmless", sent whole without asking).
scpi.py:212-227 (split_compound) and scpi.py:203-209 (is_query) look at the first line only.

The lab's smu is limited to +-10 V. The instrument here is NOT real hardware: it is a stand-in on a fake VISA
resource manager that does what every real instrument does with the bytes it gets, namely read them a program
message at a time, each ended by a line feed (IEEE 488.2, 7.5). Kelvin's own VisaBackend talks to it.

Part A: gate.raw, as the agent's send_raw calls it.
Part B: one of "the lab's scripts", run through scriptrun with Kelvin's stand-in for pyvisa.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import simlab
from _harness import Recorder, fakes, output_of, run_script, write_script
from labharness.adapters import VisaBackend
from labharness.clock import SimClock
from labharness.eventlog import EventLog
from labharness.gate import SafetyGate
from labharness.lab import Lab
from labharness.labconfig import load_lab_config
from labharness.scpi import split_compound

LAB = """
lab: Newline lab
instruments:
  smu:
    card: virtual/source-meter
    address: GPIB0::24::INSTR
    limits: { V: [-10, 10] }
    rates: { V: 10 }
"""


class LineInstrument:
    """A source meter as its bus sees it: each line feed ends a program message; ';' separates units in one."""

    def __init__(self, card):
        self.card = card
        self.state = {"V": 0.0, "V_range": 20.0, "V_autorange": 1, "compliance_I": 1e-4, "output": 0, "nplc": 1.0}
        self.replies = []
        self.messages = []   # each program message as it arrived

    def receive(self, data):
        for message in data.split("\n"):
            if not message.strip():
                continue
            self.messages.append(message)
            for unit in split_compound(message):
                self.unit(unit)

    def unit(self, text):
        up = text.strip().upper()
        if up == "*IDN?":
            self.replies.append("VIRTUAL,SOURCE-METER,SN0001,1.0")
        elif up.startswith(("SYST:ERR", "SYSTEM:ERR")):
            self.replies.append('0,"No error"')
        elif up.startswith("*"):
            pass   # *CLS and the like
        else:
            for spec in self.card.params.values():
                if spec.get_cmd is not None and spec.get_cmd.match(text) is not None:
                    v = self.state.get(spec.name, 0.0)
                    self.replies.append("%g" % (1e-12 if spec.name == "I" else v))
                    return
                if spec.set_cmd is not None:
                    got = spec.set_cmd.match(text)
                    if got is not None:
                        self.state[spec.name] = spec.from_wire(got) if spec.type != "bool" else int(spec.from_wire(got))
                        return


class Resource:
    def __init__(self, instrument):
        self.instrument = instrument
        self.timeout, self.read_termination, self.write_termination = 2000, "\n", "\n"

    def write(self, message):   # as pyvisa: the message, then the write termination
        self.instrument.receive(message + (self.write_termination or ""))

    def read(self):
        return self.instrument.replies.pop(0) if self.instrument.replies else ""

    def query(self, message, delay=None):
        self.write(message)
        return self.read()

    def clear(self):
        self.instrument.replies.clear()

    def close(self):
        pass

    # binary blocks, as pyvisa sends them: the message as written, then a block read or written (binary_block.py)
    def query_binary_values(self, message, datatype="f", is_big_endian=False, container=list, header_fmt="ieee",
                            expect_termination=True, data_points=0, **kwargs):
        self.write(message)
        self.instrument.replies.clear()
        return container([0.0, 0.0])

    def write_binary_values(self, message, values, datatype="f", is_big_endian=False, **kwargs):
        self.write(message + "#10")


class Rm:
    def __init__(self, instrument):
        self.instrument = instrument

    def open_resource(self, name, **kwargs):
        return Resource(self.instrument)


def build():
    registry = simlab.lab_cards()
    cfg = load_lab_config(LAB, registry)
    box = {}

    def factory(name, card, address):
        box["instrument"] = LineInstrument(card)
        return VisaBackend(name, card, address, rm=Rm(box["instrument"]))
    lab = Lab(cfg, registry, factory, SimClock(), None, deadline=False)
    return lab, box["instrument"]


def part_a():
    print("=== Part A: gate.raw, as the agent's send_raw calls it (lab limit on smu.V: +-10 V) ===")
    lab, instrument = build()
    approver = Recorder(answers=[])   # nobody says yes to anything
    gate = SafetyGate(lab, approver, EventLog(None, lab.clock))
    r = gate.raw("smu", "SOUR:VOLT 150")
    print("plain   'SOUR:VOLT 150'            ->", r.ok, r.action, "|", r.message)
    r = gate.raw("smu", "OUTP ON")
    print("plain   'OUTP ON' (nobody says yes) ->", r.ok, r.action, "|", r.message)
    print("        asked so far:", [q.title for q in approver.requests], " instrument:", instrument.state["V"], "V, output", instrument.state["output"])
    approver.requests.clear()
    r = gate.raw("smu", "SOUR:VOLT?\nSOUR:VOLT 150")
    print("2 lines 'SOUR:VOLT?\\nSOUR:VOLT 150' ->", r.ok, r.action, "|", repr(r.message))
    r = gate.raw("smu", "*CLS\nOUTP ON")
    print("2 lines '*CLS\\nOUTP ON'            ->", r.ok, r.action, "|", repr(r.message))
    print("        asked:", [q.title for q in approver.requests])
    print("        the instrument now: V = %s V (limit 10), output = %s" % (instrument.state["V"], instrument.state["output"]))
    print("        program messages it got:", instrument.messages[-4:])


SCRIPT = '''
import pyvisa

rm = pyvisa.ResourceManager()
smu = rm.open_resource("GPIB0::24::INSTR")
print("V at the start:", smu.query("SOUR:VOLT?"))
print("reply to the two-line query:", smu.query("SOUR:VOLT?\\nSOUR:VOLT 150"))
smu.write("*CLS\\nOUTP ON")
print("V now:", smu.query("SOUR:VOLT?"), " output now:", smu.query("OUTP?"))
'''


def part_b():
    print()
    print("=== Part B: one of the lab's scripts, through scriptrun and Kelvin's stand-in for pyvisa ===")
    lab, instrument = build()
    with tempfile.TemporaryDirectory(prefix="audit-newline-") as tmp:
        fakes(tmp)
        folder = Path(tmp) / "scripts"
        folder.mkdir()
        write_script(folder, "sweep.py", SCRIPT)
        gate, approver, prep, report = run_script(lab, folder, Path(tmp) / "runs", "sweep.py", answers=[True])
        print("the one approval card (what the person said yes to):")
        for line in approver.requests[0].items:
            print("   ", line)
        print("asked after that:", [q.title for q in approver.requests[1:]])
        print("run:", report.status, "|", report.message)
        print("script output:")
        for line in output_of(report).splitlines():
            print("   ", line)
        print("commands.jsonl (what the record says the gate did):")
        for line in (Path(report.path) / "commands.jsonl").read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            print("   ", {k: e[k] for k in ("command", "action", "ok", "message") if k in e})
        print("the instrument after the run: V = %s V (the lab's limit is 10), output = %s"
              % (instrument.state["V"], instrument.state["output"]))
        print("left away from safe, as the report says:", report.left, "| clean-up:", report.cleanup)


if __name__ == "__main__":
    part_a()
    part_b()
