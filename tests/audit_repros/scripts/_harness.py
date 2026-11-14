"""Shared set-up for the scripts-area repros: a simulated lab from a lab config (the tests' virtual cards), a gate
whose approver records what it is asked, and a way to run one of "the lab's scripts" through scriptrun as the
runtime does (prepare -> approval -> ScriptRun.execute), with fake pyvisa/pyserial that refuse every real open.

Nothing here touches real hardware: every instrument is a simulated twin (labharness.sim.twin)."""
import os
import sys
import tempfile
import textwrap
from pathlib import Path

import simlab
from labharness import scriptrun
from labharness.clock import SimClock
from labharness.eventlog import EventLog, RunStore
from labharness.gate import Approver, SafetyGate
from labharness.lab import Lab
from labharness.labconfig import load_lab_config
from labharness.sim.twin import World, sim_backend_factory

FAKE_PYVISA = '''
class ResourceManager(object):
    def __init__(self, *args, **kwargs):
        pass
    def open_resource(self, name, **kwargs):
        raise RuntimeError("fake pyvisa has no instruments (%s): Kelvin's stand-in should have answered" % name)
    def list_resources(self, query="?*::INSTR"):
        return ()
'''

FAKE_SERIAL = '''
class Serial(object):
    def __init__(self, port=None, baudrate=9600, timeout=None, **kwargs):
        self.port, self.baudrate, self.timeout, self.is_open = port, baudrate, timeout, False
        if port is not None:
            self.open()
    def open(self):
        raise RuntimeError("fake pyserial has no ports (%s): Kelvin's stand-in should have answered" % self.port)
    def close(self):
        self.is_open = False
    def write(self, data):
        raise RuntimeError("written past Kelvin")
    def read(self, size=1):
        return b""
    def read_until(self, expected=b"\\n", size=None):
        return b""
    def readline(self, size=-1):
        return b""
'''


class Recorder(Approver):
    """Says yes (or the answers given, in turn) and keeps every request it was asked."""

    def __init__(self, answers=None):
        self.answers = list(answers) if answers is not None else None
        self.requests = []

    def approve(self, request):
        self.requests.append(request)
        if self.answers is None:
            return True
        return self.answers.pop(0) if self.answers else False


def make_lab(config_text):
    registry = simlab.lab_cards()
    clock = SimClock()
    world = World(clock, 0)
    return Lab(load_lab_config(config_text, registry), registry, sim_backend_factory(world), clock, world)


def wire_tap(lab):
    """Every command the simulated instruments are sent, in order: [(instrument, 'write'|'query', text)]."""
    sent = []
    for name, inst in lab.instruments.items():
        backend = inst.backend

        def write(command, _w=backend.write, _n=name):
            sent.append((_n, "write", command))
            return _w(command)

        def query(command, _q=backend.query, _n=name):
            sent.append((_n, "query", command))
            return _q(command)
        backend.write, backend.query = write, query
    return sent


def fakes(tmp):
    """Fake pyvisa and pyserial on PYTHONPATH for the script's Python (they refuse every real open)."""
    d = Path(tmp) / "fakes"
    (d / "pyvisa").mkdir(parents=True, exist_ok=True)
    (d / "pyvisa" / "__init__.py").write_text(FAKE_PYVISA)
    (d / "serial").mkdir(exist_ok=True)
    (d / "serial" / "__init__.py").write_text(FAKE_SERIAL)
    os.environ["PYTHONPATH"] = str(d)
    return d


def _python(script, source, roots, configured="", named=""):
    return scriptrun.Python([sys.executable, "-u"], "the repro's Python", tuple(sys.version_info[:2]))


def write_script(folder, name, text):
    path = Path(folder) / name
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


def run_script(lab, folder, runs, script, answers=None, params=None, between=None):
    """prepare -> approval (asked of the recorder) -> execute. between(prep): called after the approval is
    answered and before the run starts (the gap a person's yes or the queue leaves)."""
    approver = Recorder(answers)
    gate = SafetyGate(lab, approver, EventLog(None, lab.clock))
    prep = scriptrun.prepare(lab, script, [folder], params=params, choose_python=_python)
    request, grants, problems = scriptrun.approval(gate, prep)
    assert not problems, problems
    assert gate.ask(request)
    if between is not None:
        between(prep)
    allowed = set(prep.found.unknown) | set(prep.found.libraries)
    job = scriptrun.ScriptRun(gate, prep, RunStore(Path(runs)), grants, allowed)
    report = job.execute()
    return gate, approver, prep, report


def output_of(report):
    return (Path(report.path) / "output.txt").read_text(encoding="utf-8", errors="replace")
