"""A reading that fails while its instrument answers stops only the work that needs it (a field report, 2026-11-13:
an attoDRY2100 whose sample sensor was unplugged answered its temperature with the controller's error 31, every
other reading as ever, and a read-only log of its three pressures was refused, after the queue had said it was
running). Here the preflight checks what a run needs: what it measures, waits on or decides by, what its settings
read, and the guards, interlocks and lab watches on what it drives; a reading it doesn't need is a warning in its
record. The queue says an item runs only once its checks passed, and queue_start says a refusal before anyone is
asked. Also: the controller's own name for an error number, a card's text for the agent, and run_python's output
kept as a run marked computed, never measured."""
import hashlib
import json
import re
from pathlib import Path

import pytest

from labharness import ScriptedApprover, rundata
from labharness.adapters import AdapterError
from labharness.cards import CardError, parse_card
from labharness.drivers import MODULES
from labharness.homelab import build_lab
from labharness.instrument import Garbled
from labharness.scanqueue import ScanQueue
from simlab import build_virtual_lab
from test_jsonrpc import CRYO, FakeEnspire

pytest.importorskip("mcp")

from labharness import datatools  # noqa: E402
from labharness.agent.runtime import COMPUTED, LabRuntime, Viewer  # noqa: E402

ERRORS = "com.attocube.system.errorNumberToString"


class Unplugged(FakeEnspire):
    """The eNSPIRE controller with its sample sensor unplugged: getTemperature answers [31, 0.0], as the field
    report's did; every other method as ever. names: what errorNumberToString answers for each number (None: the
    controller has no such method, as the base fake answers any it doesn't know). The names here are made up for the
    test; nobody has published what 31 means."""

    def __init__(self, names=None):
        super().__init__()
        self.names = names
        self.unplugged = True

    def reply(self, req, result):
        self.sent.append((req["method"], req["params"]))
        self.out += (json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}) + "\n").encode()

    def sendall(self, data):
        req = json.loads(data.decode("utf-8"))
        if req["method"] == CRYO + ".sample.getTemperature" and self.unplugged:
            return self.reply(req, [31, 0.0])
        if req["method"] == ERRORS and self.names is not None:
            name = self.names.get(req["params"][-1])
            return self.reply(req, [0, name] if name else [11])
        return super().sendall(data)


@pytest.fixture
def cryo():
    fake = MODULES["jsonrpc"] = Unplugged()
    yield fake
    MODULES.clear()


LAB = """
lab: Cryostat
instruments:
  attodry: { card: attocube/attodry-enspire, address: "jsonrpc://192.168.1.1:9090", limits: { B_set: [-1, 1] } }
"""
PRESSURES = """plan: pressure_log
time: { every: 0.1, for: 0.2 }
measure: [attodry.p_cryo_in, attodry.p_cryo_out, attodry.p_dump]
"""
T_LOG = """plan: t_log
time: { every: 0.1, for: 0.1 }
measure: [attodry.T]
"""


def runtime(tmp_path, lab_text=LAB, answers=()):
    return LabRuntime(build_lab(lab_text), ScriptedApprover(list(answers)), tmp_path / "data", None)


# ------------------------------------------------------------------ the health check and the preflight

def test_a_reading_answered_with_an_error_is_kept_by_name_with_the_whole_error(cryo):
    result = build_lab(LAB).health_check(["attodry"])[0]
    assert not result.ok and list(result.unreadable) == ["T"]
    why = result.unreadable["T"]   # once cut at 60 characters, in the middle of the method's name
    assert why == "%s.sample.getTemperature: the instrument answered with its error 31" % CRYO
    assert "cannot read T (%s)" % why in str(result)
    assert "its other readings work: a run that doesn't need T can still run" in str(result)


def test_a_run_that_does_not_need_it_runs_and_its_record_says_so(cryo, tmp_path):
    rt = runtime(tmp_path)
    report = rt.engine.run(PRESSURES)
    assert report.status == "done" and report.points >= 2, report.message
    told = [w for w in report.warnings if w.startswith("attodry.T can't be read now")]
    assert told and "its error 31" in told[0] and told[0].endswith("this run doesn't need it")
    assert told[0] in rundata.meta(rt._run_dir(report.run_id))["warnings"]
    check = [e for e in rt.gate.log.events() if e["type"] == "preflight"][-1]
    assert check["ok"] is True and check["not_needed"] == told   # the record: it may go, and what it didn't need


@pytest.mark.parametrize("plan, why", [
    (T_LOG, "it needs T: it measures it"),
    ("plan: warm\nsetup: {attodry.T_set: 5}\nmeasure: [attodry.p_dump]",
     "it needs T: it sets attodry.T_set, whose reading it is"),
    ("plan: settle\nsweep: {attodry.B_set: {values: [0.1, 0.2], stabilize: {read: attodry.T, within: 0.1, "
     "for: 1}}}\nmeasure: [attodry.p_dump]\nafter: {attodry.B_set: 0}", "it needs T: it waits for it to settle"),
    ("plan: cold\npreconditions: [\"attodry.T < 10\"]\nmeasure: [attodry.p_dump]",
     "it needs T: its precondition attodry.T < 10 reads it"),
    ("plan: until_warm\ntime: {every: 0.1, for: 1}\nmeasure: [attodry.p_dump]\nuntil: \"attodry.T > 10\"",
     "it needs T: its 'attodry.T > 10' reads it"),
])
def test_a_run_that_needs_it_is_refused_saying_what_for(cryo, tmp_path, plan, why):
    rt = runtime(tmp_path)
    report = rt.engine.run(plan)
    assert report.status == "refused" and "preflight failed: attodry FAIL (cannot read T (" in report.message
    assert why in report.message, report.message
    assert not [e for e in rt.gate.log.events() if e["type"] in ("run_start", "command")]   # nothing ran or was set


@pytest.mark.parametrize("extra, plan, why", [
    ('interlocks:\n  - { drive: attodry.B_set, require: "attodry.T < 10" }\n',
     "plan: field\nsetup: {attodry.B_set: 0.1}\nmeasure: [attodry.p_dump]\nafter: {attodry.B_set: 0}",
     "it needs T: the interlock on attodry.B_set (attodry.T < 10) reads it"),
    ("watch:\n  - { read: attodry.T, above: 300, then: { attodry.field_control: off } }\n",
     "plan: on\nsetup: {attodry.field_control: on}\nmeasure: [attodry.p_dump]",
     "it needs T: the lab watch on it brings attodry.field_control to safe"),
    ("watch:\n  - { read: attodry.T, above: 300, stop_run: true }\n",
     "plan: field\nsetup: {attodry.B_set: 0.1}\nmeasure: [attodry.p_dump]\nafter: {attodry.B_set: 0}",
     "it needs T: the lab watch on it stops the run (attodry.T above 300 K)"),
])
def test_what_protects_what_a_run_drives_is_needed_too(cryo, tmp_path, extra, plan, why):
    rt = runtime(tmp_path, LAB + extra, answers=[True] * 4)
    report = rt.engine.run(plan)
    assert report.status == "refused" and why in report.message, report.message
    assert not [e for e in rt.gate.log.events() if e["type"] == "command"] and not rt.gate.approver.requests


def test_a_guard_on_a_drive_needs_its_reading_and_a_read_only_run_needs_no_watch(cryo, tmp_path):
    guarded = LAB.replace("limits: { B_set: [-1, 1] } }", "limits: { B_set: [-1, 1] }, "
                                                          "guards: { B_set: { read: T, max_abs: 300 } } }")
    rt = runtime(tmp_path, guarded + "watch:\n  - { read: attodry.T, above: 300, stop_run: true }\n")
    report = rt.engine.run("plan: field\nsetup: {attodry.B_set: 0.1}\nmeasure: [attodry.p_dump]\n"
                           "after: {attodry.B_set: 0}")
    assert report.status == "refused" and "the guard on attodry.B_set (|attodry.T| ≤ 300) reads it" in report.message
    assert rt.engine.run(PRESSURES).status == "done"   # a watch that stops runs protects nothing a log drives


def test_what_a_run_needs_is_worked_out_from_the_plan_and_the_lab(cryo, tmp_path):
    rt = runtime(tmp_path, LAB + 'interlocks:\n  - { drive: attodry.B_set, require: "attodry.T < 10" }\n')
    plan = rt._plan("plan: x\nsetup: {attodry.T_set: 5, attodry.temperature_control: on}\nmeasure: [attodry.B]")
    needs = rt.engine.needs(plan)["attodry"]
    assert needs["B"] == "it measures it"
    assert needs["T_set"].startswith("it sets it") and needs["T"] == "it sets attodry.T_set, whose reading it is"
    assert needs["temperature_control"] == "attodry.T_set acts on the sample only while it is on"
    assert needs["B_set"].startswith("its interlock (attodry.T < 10) reads attodry.T, which attodry.T_set changes")
    assert "p_dump" not in needs and "p_cryo_in" not in needs
    assert rt.engine.needs(rt._plan(PRESSURES)) == {"attodry": {p: "it measures it" for p in
                                                                ("p_cryo_in", "p_cryo_out", "p_dump")}}
    scripts = rt.engine.drive_needs(["attodry.B_set"])   # a script's drives: their read-back and protections
    assert set(scripts["attodry"]) == {"B_set", "B", "T"}


def test_every_other_problem_still_stops_any_run(cryo, tmp_path):
    cryo.unplugged = False   # every reading answers; one reply then arrives garbled
    rt = runtime(tmp_path)
    inst = rt.lab.instruments["attodry"]
    read = inst.read

    def garbled(param, replies=None):   # a garbled reply: the link, not one reading; nothing it says is trusted
        if param == "p_dump":
            raise Garbled("attodry's reply arrived garbled")
        return read(param, replies)
    inst.read = garbled
    result = rt.lab.health_check(["attodry"])[0]
    assert not result.ok and result.unreadable == {} and "garbled" in str(result)
    report = rt.engine.run(PRESSURES)
    assert report.status == "refused" and "garbled" in report.message


# ------------------------------------------------------------------ the queue

def test_queue_start_says_a_refusal_before_anyone_is_asked(cryo, tmp_path):
    rt = runtime(tmp_path)   # nobody answers: asking would be an error
    rt.queue_add(T_LOG)
    rt.queue_add(PRESSURES)
    said = rt.queue_start()
    assert said.startswith("the queue did not start: #1 t_log: refused: preflight failed: attodry FAIL"), said
    assert "it needs T: it measures it" in said
    assert [i.status for i in rt.queue.items] == ["waiting", "waiting"] and rt.queue.state == "idle"
    assert rt._queue_thread is None and rt.gate.approver.requests == []


def test_an_item_refused_at_its_turn_never_reads_running(cryo, tmp_path):
    rt = runtime(tmp_path)
    first, second = rt.queue.add(PRESSURES), rt.queue.add(T_LOG)
    marks, real = [], rt.queue.mark

    def mark(item, status, message="", run_id=None):
        marks.append((item.name, status))
        real(item, status, message, run_id)
    rt.queue.mark = mark
    said = rt.queue_start()
    assert said.startswith("queue started") and "#2 t_log: would be refused if it started now: preflight " \
                                                "failed" in said, said
    rt._queue_thread.join(60)
    assert marks == [("pressure_log", "running"), ("pressure_log", "done"), ("t_log", "failed")], marks
    assert first.status == "done" and second.message.startswith("refused: preflight failed: attodry FAIL")
    assert rt.queue.state == "paused" and rt.queue.reason.startswith("t_log: refused: preflight failed")
    assert [e for e in rt.gate.log.events() if e["type"] == "run_refused" and e.get("plan") == "t_log"]
    account = rt.queue.last["lines"]   # the morning's account names it, though it never started
    assert any(line.startswith("Never started: #2 t_log: refused: preflight failed") for line in account), account
    assert second.id in rt.queue.last["items"]


def test_a_queue_rehearsal_stays_on_the_simulated_lab(cryo, tmp_path):
    rt = runtime(tmp_path)
    rt.queue_add(PRESSURES)
    rt.queue_add(T_LOG)
    said = rt.queue_start(dry_run=True)
    assert said.startswith("REHEARSAL ONLY") and rt.queue.state == "idle" and rt._queue_thread is None
    assert not [e for e in rt.gate.log.events() if e["type"] == "preflight"]   # the real start checks them


def test_an_item_being_checked_reads_starting_and_is_changed_only_as_a_running_one(tmp_path):
    q = ScanQueue(tmp_path / "queue.json")
    item, later = q.add(PRESSURES), q.add(T_LOG)
    q.pick(item)
    v = q.view()
    assert v["starting"] == item.id and v["running"] is None and v["waiting"] == 1
    assert [i["status"] for i in v["items"]] == ["starting", "waiting"] and "1. [starting] plan pressure_log" in q.text()
    for change in (lambda: q.edit(item.id, PRESSURES), lambda: q.remove(item.id), lambda: q.skip(item.id)):
        with pytest.raises(ValueError, match="pressure_log is starting"):
            change()
    assert ScanQueue(tmp_path / "queue.json").find(item.id).status == "waiting"   # never kept: waits after a restart
    q.mark(item, "running")
    assert q.starting is None and q.view()["running"] == item.id and q.status_of(later) == "waiting"


# ------------------------------------------------------------------ the controller's own words for an error

def test_the_controller_says_what_its_error_number_is(tmp_path):
    fake = MODULES["jsonrpc"] = Unplugged(names={31: "SOME_NAME"})
    try:
        backend = build_lab(LAB).instruments["attodry"].backend
        for _ in range(2):
            with pytest.raises(AdapterError, match="its error 31, which it calls SOME_NAME$"):
                backend.query("GET %s.sample.getTemperature" % CRYO)
        assert [p for m, p in fake.sent if m == ERRORS] == [[0, 31]]   # asked once, as attocube's own API asks
    finally:
        MODULES.clear()


def test_a_controller_that_doesn_t_say_gives_the_number_alone(cryo):
    backend = build_lab(LAB).instruments["attodry"].backend   # the base fake: no errorNumberToString
    with pytest.raises(AdapterError, match="the instrument answered with its error 31$"):
        backend.query("GET %s.sample.getTemperature" % CRYO)
    assert backend.said == {31: ""}   # never asked again


def test_a_card_asks_only_what_an_error_number_means():
    card = {"card": 1, "id": "x/rpc", "model": "X", "kind": "cryostat",
            "jsonrpc": {"port": 9090, "error_text": "dev.startCooldown 0"},
            "parameters": {"T": {"kind": "measure", "unit": "K", "get": "GET dev.getTemperature"}}}
    with pytest.raises(CardError, match="error_text"):
        parse_card(card)
    card["jsonrpc"]["error_text"] = "%s 0" % ERRORS
    assert parse_card(card).jsonrpc["error_text"] == "%s 0" % ERRORS


# ------------------------------------------------------------------ a card's text, for this lab's version of it

def test_the_agent_reads_a_card_s_text_by_its_id_or_its_instrument(cryo, tmp_path):
    rt = runtime(tmp_path)
    by_instrument, by_id = rt.card_text("attodry"), rt.card_text("attocube/attodry-enspire")
    assert by_instrument.startswith("attocube/attodry-enspire: attocube attoDRY2100e") and "used by attodry" in \
        by_instrument.splitlines()[0]
    assert "GET com.attocube.cryostat.interface.sample.getTemperature" in by_id
    assert by_id.splitlines()[1:] == by_instrument.splitlines()[1:]
    assert "save_card with version: true" in by_id and "never from memory" in by_id
    assert "has no card" in rt.card_text("nobody/nothing")
    from labharness.web.agent import DEFERRED, LOOKUPS
    assert "card_text" in LOOKUPS and "card_text" in DEFERRED["writing and trying cards"]


# ------------------------------------------------------------------ run_python's output, kept as a run

GATE = "plan: gate\nsweep: { smu.V: {from: 0, to: 1, points: 5} }\nmeasure: [lockin.X]\nafter: { smu.V: 0 }"


@pytest.fixture
def lab_rt(tmp_path):
    return LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 20), tmp_path / "data")


def test_run_python_s_output_becomes_a_run_marked_computed_never_measured(lab_rt):
    rt = lab_rt
    run_id = re.search(r"run (\S+) done", rt.run(GATE)).group(1)
    code = ("import numpy as np\nx, y = run['smu.V'], run['lockin.X']\n"
            "np.savetxt('scaled.csv', np.c_[x, 2 * y], delimiter=',', header='V,X2', comments='')\n")
    _, text = datatools.run_python(rt, code, [run_id])
    folder = Path(re.search(r"its folder: (.*)", text).group(1))
    said = rt.import_data(str(folder / "scaled.csv"))
    assert said.startswith("kept run_python's scaled.csv as run") and said.splitlines()[0].endswith(COMPUTED), said
    new = re.search(r"as run (\S+):", said).group(1)
    d = rt._run_dir(new)
    meta = rundata.meta(d)
    assert meta["lab"] == COMPUTED and meta["computed"]["from_runs"] == [run_id]
    [kept] = meta["computed"]["code"]
    ran = (folder / "code.py").read_bytes()   # the code as it ran (a Windows file's line ends and all)
    assert (d / "made_by.py").read_bytes() == ran and (d / "made_by.py").read_text(encoding="utf-8") == code
    assert kept["sha256"] == hashlib.sha256(ran).hexdigest() and kept["code"] == "made_by.py"
    assert meta["warnings"][0].startswith("computed by Python code (run_python) from run %s, not measured" % run_id)
    card = rundata.card(d)
    assert card["lab"] == COMPUTED and meta["warnings"][0] in card["issues"]
    assert COMPUTED in rt.find_runs(plan=new.split("-", 2)[-1])
    # what Kelvin wrote there itself is never imported
    for own in ("code.py", "job.json"):
        assert "Kelvin's own folder" in rt.import_data(str(folder / own))


def test_numbers_the_code_wrote_itself_are_said_so(lab_rt):
    rt = lab_rt
    _, text = datatools.run_python(rt, "open('typed.csv', 'w').write('t,p\\n0,1.5\\n1800,1.6\\n')", [])
    folder = Path(re.search(r"its folder: (.*)", text).group(1))
    said = rt.import_data(str(folder / "typed.csv"))
    new = re.search(r"as run (\S+):", said).group(1)
    meta = rundata.meta(rt._run_dir(new))
    assert meta["computed"]["from_runs"] == [] and meta["lab"] == COMPUTED
    assert meta["warnings"][0].startswith("written by Python code (run_python) that read no run: its numbers are "
                                          "what that code wrote, not measured by Kelvin")


def test_run_python_s_output_from_unshared_work_stays_with_it(lab_rt, tmp_path):
    rt = lab_rt
    folder = Path(rt.data_dir) / "python" / "20261113-010203"
    folder.mkdir(parents=True)
    (folder / "job.json").write_text(json.dumps({"runs": {"r1": {"folder": str(tmp_path / "hidden-run")}}}),
                                     encoding="utf-8")
    (folder / "code.py").write_text("x = 1\n", encoding="utf-8")
    (folder / "out.csv").write_text("x,y\n0,1\n1,2\n", encoding="utf-8")
    with rt.viewing(Viewer()):   # an agent over MCP sees shared work only: that run's marks can't be read
        assert "from a run of unshared work this chat may not see" in rt.import_data(str(folder / "out.csv"))
    (folder / "runs").mkdir()
    (folder / "runs" / "r1.csv").write_text("x,y\n0,1\n", encoding="utf-8")   # a snapshot Kelvin made of a run
    assert "Kelvin's own folder" in rt.import_data(str(folder / "runs" / "r1.csv"))
