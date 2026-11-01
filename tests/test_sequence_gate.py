"""phase B: a sequence block's measurement script runs through Kelvin's safety gate (v2).

A labMaster-style block's script is run by the engine's own runner (`runners/run_block.py`). With a
SafetyGate attached to the sequence service, that child gets Kelvin's stand-ins for the instruments
(`data/scriptshim`, the same ones a lab script run gets): every instrument command becomes a request
to Kelvin, checked and approved as the gate does. These tests build a real virtual lab, a real gate
and a real engine in a temporary home, and run real block scripts through it - no network, no
instrument, no Kelvin on 8791.

    approve   the first drive-away asks a person once; one yes covers the block run
    decline   no -> the script gets KelvinRefused, the block is an error, the sequence says so
    limits    a command beyond the card's limits is refused before anyone is asked
    stop      the engine's Stop reaches the script (its time.sleep returns, one KeyboardInterrupt)
    safe-stop the safe-stop script is never gated (it runs without the stand-in environment)
    old path  a service without a gate is the vendored engine, unchanged
"""
import json
import sys
import textwrap
import time
from pathlib import Path

import pytest

from labharness import scriptrun
from labharness.clock import SimClock
from labharness.eventlog import EventLog
from labharness.gate import SafetyGate, ScriptedApprover
from labharness.homelab import builtin_registry
from labharness.lab import Lab
from labharness.labconfig import load_lab_config
from labharness.sequence import service as sequence_service
from labharness.sim.twin import World, sim_backend_factory

LAB = """
lab: Gate lab
instruments:
  smu:
    card: virtual/source-meter
    address: GPIB0::24::INSTR
    limits: { V: [-10, 10] }
    rates: { V: 10 }
  lockin:
    card: virtual/lock-in
    address: GPIB0::8::INSTR
"""

# the same fakes test_script_runs.py uses: a command that got past Kelvin's stand-in fails loudly here
FAKE_PYVISA = '''
class ResourceManager(object):
    def __init__(self, *args, **kwargs):
        pass

    def open_resource(self, name, **kwargs):
        raise RuntimeError("the test's fake pyvisa has no instruments (%s): Kelvin's stand-in should have answered" % name)

    def list_resources(self, query="?*::INSTR"):
        return ()
'''

# the block's script: two drive changes (one approval covers both) and a reading through the gate
DRIVE_SCRIPT = """
import pyvisa

rm = pyvisa.ResourceManager()
smu = rm.open_resource("GPIB0::24::INSTR")
lockin = rm.open_resource("GPIB0::8::INSTR")
smu.write("SOUR:VOLT 0.5")
print("set 0.5 V")
print("read", lockin.query("MEAS:X?"))
smu.write("SOUR:VOLT 0.8")
print("set 0.8 V")
print("block script done")
"""

OVER_LIMIT_SCRIPT = """
import pyvisa

smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
smu.write("SOUR:VOLT 50")
print("this line must not be reached")
"""

SLEEP_SCRIPT = """
import time
import pyvisa

smu = pyvisa.ResourceManager().open_resource("GPIB0::24::INSTR")
smu.write("SOUR:VOLT 0.5")
print("set 0.5 V, now sleeping")
time.sleep(120)
print("this line must not be reached")
"""

SAFE_STOP_SCRIPT = """
import json
import os
import sys

marker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "safe-stop-env.json")
with open(marker, "w", encoding="utf-8") as f:
    json.dump({"KELVIN_IPC": os.environ.get("KELVIN_IPC"),
               "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
               "python": sys.executable}, f)
print("safe stop ran")
"""


@pytest.fixture
def lab():
    registry = builtin_registry()
    cfg = load_lab_config(LAB, registry)
    clock = SimClock()
    world = World(clock, 0)
    return Lab(cfg, registry, sim_backend_factory(world), clock, world)


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    """Fake pyvisa on the child's PYTHONPATH (as test_script_runs), plus the Python Kelvin runs."""
    folder = tmp_path / "fakes"
    (folder / "pyvisa").mkdir(parents=True)
    (folder / "pyvisa" / "__init__.py").write_text(FAKE_PYVISA)
    monkeypatch.setenv("PYTHONPATH", str(folder))
    return folder


class Block:
    """One sequence service + Kelvin gate, ready to run a block script in a temporary home."""

    def __init__(self, tmp_path, lab, answers, script, safe_stop=False):
        self.tmp_path = tmp_path
        self.home = tmp_path / "Kelvin"
        self.root = self.home / "sequence"
        (self.root / "scripts").mkdir(parents=True)
        self.approver = ScriptedApprover(list(answers))
        self.record = tmp_path / "lab-record.jsonl"
        self.gate = SafetyGate(lab, self.approver, EventLog(self.record, lab.clock))
        stop_script = self.root / "scripts" / "safe_stop.py"
        config = {"sim": False, "run": {"grace_seconds": 2},
                  "paths": {"python_exe": sys.executable, "script_search_dirs": ["scripts"],
                            "safe_stop_script": ""}}
        if safe_stop:
            stop_script.write_text(textwrap.dedent(SAFE_STOP_SCRIPT).lstrip(), encoding="utf-8")
            config["paths"]["safe_stop_script"] = str(stop_script)
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        (self.root / "scripts" / "block_script.py").write_text(textwrap.dedent(script).lstrip(),
                                                               encoding="utf-8")
        sequence_service.reset_service()
        self.service = sequence_service.service(self.home)
        attached = self.service.attach_gate(self.gate)
        assert attached["ok"] is True, attached
        assert self.service.ping()["ok"] is True
        assert self.service._gated is True
        imported = self.service.import_script("scripts/block_script.py")
        assert imported["ok"] is True, imported
        self.uid = self.service.seq_add(block="block_script")["state"]["sequence"]["items"][-1]["uid"]

    # -- running

    def start(self):
        started = self.service.control("start")
        assert started["ok"] is True, started.get("error")
        return started

    def wait(self, done, timeout=60.0):
        end = time.monotonic() + timeout
        state = self.service.state()["state"] or {}
        while time.monotonic() < end:
            state = self.service.state()["state"] or {}
            if done(state):
                return state
            time.sleep(0.05)
        raise AssertionError("the engine never reached the expected state: %r" % (state.get("run_state"),))

    def idle(self, timeout=60.0):
        return self.wait(lambda state: state["run_state"] not in ("running", "stopping"), timeout)

    def item(self):
        return (self.service.state()["state"] or {}).get("sequence", {}).get("items", [{}])[0]

    def log_text(self):
        lines = getattr(self.service._engine, "log_lines", None) or []
        return "\n".join(str(row.get("text") or "") for row in lines)

    def record_entries(self):
        try:
            return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines() if line]
        except (OSError, ValueError):
            return []

    def close(self):
        sequence_service.reset_service()


@pytest.fixture
def block(tmp_path, lab, fakes, monkeypatch):
    monkeypatch.delenv("KELVIN_SEQUENCE_DIR", raising=False)
    made = []

    def make(answers=(True,), script=DRIVE_SCRIPT, safe_stop=False):
        one = Block(tmp_path, lab, answers, script, safe_stop=safe_stop)
        made.append(one)
        return one

    try:
        yield make
    finally:
        for one in made:
            one.close()


# ---------------------------------------------------------------- the gate is in the path


def test_attach_gate_makes_the_engine_gated_and_says_when_it_cannot(block):
    one = block(script=DRIVE_SCRIPT)
    assert type(one.service._engine).__name__ == "_GatedEngine"
    assert one.service._engine._block_gate is not None
    again = one.service.attach_gate(one.gate)
    assert again["ok"] is True and again["gated"] is True          # already attached: no rebuild
    other = SafetyGate(one.gate.lab, ScriptedApprover([]))
    refused = one.service.attach_gate(other)                       # the engine already runs: cannot swap
    assert refused["ok"] is False and "already running" in refused["error"]


# ---------------------------------------------------------------- approvals: one yes covers the block


def test_drive_away_asks_once_and_one_yes_covers_the_block_run(block):
    one = block(answers=(True,), script=DRIVE_SCRIPT)
    one.start()
    state = one.idle()
    assert one.item()["status"] == "done", (one.item(), one.log_text())
    assert state["run_state"] in ("idle", "paused")

    # the person saw ONE card, and it names the block, the instruments and the limits
    assert len(one.approver.requests) == 1
    request = one.approver.requests[0]
    text = str(request)
    assert 'Sequence block "block_script":' in text
    assert "Checked against smu limits" in text and "V [-10 V, 10 V]" in text
    assert "Approving this covers this block run" in text

    # both drive changes went through the gate: one approval, two commands
    commands = [entry for entry in one.record_entries() if entry.get("type") == "command"]
    sent = [entry for entry in commands if entry.get("action") == "set"
            and str(entry.get("target") or "").startswith("smu.")]
    assert len(sent) >= 2 and all(entry.get("ok") for entry in sent)
    reads = [entry for entry in commands if entry.get("action") == "read" and entry.get("instrument") == "lockin"]
    assert reads, "the reading did not go through the gate"

    # the block run is in the lab record, and the engine's History has it
    kinds = [entry.get("type") for entry in one.record_entries()]
    assert "sequence_block_start" in kinds and "sequence_block_end" in kinds
    end = next(entry for entry in one.record_entries() if entry.get("type") == "sequence_block_end")
    assert end["status"] == "done" and end["commands"] >= 3
    assert one.service.state()["state"]["history_runs"] >= 1


def test_a_person_says_no_and_the_block_fails_readably(block):
    one = block(answers=(False,), script=DRIVE_SCRIPT)
    one.start()
    state = one.idle()
    assert one.item()["status"] == "error", (one.item(), one.log_text())
    assert state["run_state"] in ("error", "paused")
    assert len(one.approver.requests) == 1
    assert 'Sequence block "block_script":' in str(one.approver.requests[0])
    log = one.log_text()
    assert "declined" in log or "KelvinRefused" in log
    asked = [entry for entry in one.record_entries() if entry.get("type") == "ask"]
    assert asked and asked[-1]["answer"] == "declined"
    end = next(entry for entry in one.record_entries() if entry.get("type") == "sequence_block_end")
    assert end["status"] == "failed" and "declined" in (end.get("reason") or "")


def test_a_command_beyond_the_limits_is_refused_without_asking(block):
    one = block(answers=(), script=OVER_LIMIT_SCRIPT)          # ScriptedApprover([]) fails loudly if asked
    one.start()
    one.idle()
    assert one.item()["status"] == "error", (one.item(), one.log_text())
    assert one.approver.requests == []                          # refused before any card
    log = one.log_text()
    assert "outside" in log and "limits" in log, log[-2000:]
    refused = [entry for entry in one.record_entries() if entry.get("type") == "command"
               and entry.get("action") == "refused"]
    assert refused, one.record_entries()
    assert "outside" in (refused[-1].get("message") or "") and "[-10, 10]" in refused[-1]["message"]


# ---------------------------------------------------------------- stop reaches the script


def test_stop_makes_the_scripts_sleep_return_and_it_ends_soon(block):
    one = block(answers=(True,), script=SLEEP_SCRIPT)
    one.start()
    # wait until the script has driven the instrument and is in its 120 s sleep
    one.wait(lambda state: (state.get("current") or {}).get("uid"))
    end = time.monotonic() + 30
    while time.monotonic() < end:
        if any("now sleeping" in str(row.get("text") or "")
               for row in getattr(one.service._engine, "log_lines", None) or []):
            break
        time.sleep(0.05)
    started = time.monotonic()
    assert one.service.control("stop")["ok"] is True
    one.idle(timeout=25)
    elapsed = time.monotonic() - started
    assert elapsed < 20, "the stop took %.1f s: time.sleep(120) in the script was not ended" % elapsed
    assert one.item()["status"] != "done"
    bridge = one.service._engine._block_gate
    assert bridge.last.get("status") in ("stopped", "failed")


def test_the_scripts_time_sleep_is_the_shims(block):
    """A block script's time.sleep is the stand-in's: it ends the moment Kelvin stops, as a lab
    script's does (the same file, loaded by sitecustomize from the environment _spawn adds)."""
    one = block(answers=(True,), script=SLEEP_SCRIPT)
    one.start()
    one.wait(lambda state: (state.get("current") or {}).get("uid"))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if one.service._engine._block_gate.state() is not None:
            break
        time.sleep(0.05)
    assert one.service._engine._block_gate.state() is not None
    assert one.service.control("stop")["ok"] is True
    one.idle(timeout=25)


# ---------------------------------------------------------------- the safe-stop child is never gated


def test_safe_stop_is_never_gated(block):
    one = block(answers=(), script=DRIVE_SCRIPT, safe_stop=True)   # answers=(): any card fails loudly
    one.service.seq_clear()
    added = one.service.seq_add(block="Safe stop")
    assert added["ok"] is True, added
    one.start()
    one.idle()
    assert one.item()["kind"] == "safe_stop"
    marker = one.root / "scripts" / "safe-stop-env.json"
    assert marker.is_file(), one.log_text()
    seen = json.loads(marker.read_text(encoding="utf-8"))
    assert seen["KELVIN_IPC"] is None, "the safe-stop script was given the gate environment"
    assert one.service._engine._block_gate.opened == 0             # no measurement runner was gated
    end = next((entry for entry in one.record_entries()
                if entry.get("type") == "sequence_block_end"), None)
    assert end is None                                             # a safe-stop block is not a gated block


# ---------------------------------------------------------------- the old path (no gate) is unchanged


def test_a_service_without_a_gate_runs_the_vendored_engine(block, tmp_path):
    sequence_service.reset_service()
    home = tmp_path / "plain"
    service = sequence_service.service(home)
    try:
        assert service.ping()["ok"] is True
        assert service._gated is False
        assert type(service._engine).__name__ == "Engine"
        assert service._engine.__class__.__module__ == "labharness.sequence.engine"
        assert service.attach_gate(None)["ok"] is True             # detaching before a restart
        sequence_service.reset_service()
        plain = sequence_service.service(home)
        assert plain.ping()["ok"] is True and plain._gated is False
        assert plain.control("start")["ok"] is False               # empty sequence: the usual refusal
    finally:
        sequence_service.reset_service()


# ---------------------------------------------------------------- the portal's one-line wiring


def test_the_portal_wiring_line_runs_a_block_through_the_gate(lab, fakes, tmp_path, monkeypatch):
    """The REAL `create_app` (routes, runtime, settings, token, Sequence service) with the one line the
    portal needs - `seq_service.attach_gate(runtime.gate)` - runs a block script through the gate end
    to end: one approval, the block done, and the merged History showing the run. This is the wiring
    the Lead adds to web/app.py (that file is not mine to change); it is checked here against the same
    create_app the 8791 portal builds."""
    monkeypatch.delenv("KELVIN_SEQUENCE_DIR", raising=False)
    pytest.importorskip("starlette")
    pytest.importorskip("httpx")
    pytest.importorskip("mcp")
    from starlette.testclient import TestClient

    from labharness.agent.runtime import LabRuntime
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore

    home = tmp_path / "Kelvin"
    root = home / "sequence"
    (root / "scripts").mkdir(parents=True)
    (root / "config.json").write_text(json.dumps({
        "sim": False, "run": {"grace_seconds": 2},
        "paths": {"python_exe": sys.executable, "script_search_dirs": ["scripts"],
                  "safe_stop_script": ""}}), encoding="utf-8")
    (root / "scripts" / "portal_block.py").write_text(textwrap.dedent(DRIVE_SCRIPT).lstrip(),
                                                      encoding="utf-8")
    approver = ScriptedApprover([True])
    runtime = LabRuntime(lab, approver, tmp_path / "data")
    settings = Settings(home / "settings.json")
    sequence_service.reset_service()
    app = create_app(runtime, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"])
    client = TestClient(app)
    headers = {"X-Kelvin-Token": settings.access_key}
    try:
        service = sequence_service.service()            # the app's own service (home = home)
        assert service.attach_gate(runtime.gate)["ok"] is True    # <- the one line web/app.py needs
        assert client.get("/api/labmaster/state", headers=headers).json()["ok"] is True
        assert service._gated is True

        imported = client.post("/api/labmaster/import", headers=headers,
                               json={"path": "scripts/portal_block.py"}).json()
        assert imported["ok"] is True, imported
        added = client.post("/api/labmaster/seq", headers=headers,
                            json={"action": "add", "block": "portal_block"}).json()
        assert added["ok"] is True, added
        started = client.post("/api/labmaster/control", headers=headers,
                              json={"action": "start"}).json()
        assert started["ok"] is True, started.get("error")
        deadline = time.monotonic() + 60
        state = {}
        while time.monotonic() < deadline:
            state = client.get("/api/labmaster/state", headers=headers).json().get("state") or {}
            if state.get("run_state") not in ("running", "stopping"):
                break
            time.sleep(0.05)
        items = (state.get("sequence") or {}).get("items") or []
        assert [item["status"] for item in items] == ["done"], (state, approver.requests)
        assert len(approver.requests) == 1                       # one card for the block run
        history = client.get("/api/history/all", headers=headers).json()
        assert history["ok"] is True
        assert any(run["source"] == "panel" and run["name"] for run in history["runs"]), history
    finally:
        client.close()
        sequence_service.reset_service()
