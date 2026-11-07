"""A run ends whatever happens to its record (review of 0.3.45, 3.1). A run whose record couldn't be finished (its
data folder's network drive went away, a full disk) stayed "running" until a restart: Stop said it stopped it and
changed nothing, switching an instrument and Save and connect were refused, and the idle check of lights left on never
ran. A record that couldn't be written failed a person's Stop, and a stopped run's clean-up ended at its first drive."""
import pytest

from labharness import ScriptedApprover, eventlog
from labharness.agent.runtime import LabRuntime
from simlab import build_virtual_lab

PLAN = """
plan: iv
setup: { smu.output: on }
sweep: { smu.V: {from: 0, to: 1, step: 0.5} }
measure: [smu.I]
"""


@pytest.fixture
def rt(tmp_path):
    return LabRuntime(build_virtual_lab(seed=1), ScriptedApprover([True] * 20), tmp_path / "data")


def test_a_run_whose_record_cant_be_finished_still_ends_and_says_so(rt, monkeypatch):
    def gone(self, **meta):
        raise OSError(53, "The network path was not found")
    monkeypatch.setattr(eventlog.RunRecord, "finish", gone)
    said = rt.run(PLAN)
    assert "run 2" in said and " done: 3 points" in said
    assert "warning: its data could not be written to " in said and "The network path was not found" in said
    assert "saved in " not in said                                   # it wasn't: not said
    assert rt.live["status"] == "done"                               # it ended, also in the live view
    [alarm] = [a for a in rt.monitor.active() if a.kind == "record"]
    assert alarm.latched and alarm.text.startswith("Run 2") and "Is that drive or network folder there" in alarm.text
    rt.switch_instrument("laser", False, "human (portal)")          # nothing runs: nothing refused for it
    assert rt.stop_work("", by="human").startswith("nothing to stop")


def test_a_run_whose_points_cant_be_written_stops_and_its_drives_go_to_safe(rt, monkeypatch):
    real_add, points = eventlog.RunRecord.add, []

    def add(self, row):
        points.append(row)
        if len(points) >= 2:
            raise OSError(28, "No space left on device")
        return real_add(self, row)
    monkeypatch.setattr(eventlog.RunRecord, "add", add)
    said = rt.run(PLAN)
    assert "aborted after 1/3 points: its data could not be written to " in said and "No space left" in said
    assert rt.live["status"] == "aborted"
    assert rt.lab.read("smu", "output") is False                     # the clean-up went to its end
    assert [a.kind for a in rt.monitor.active()] == ["record"]


def test_a_record_that_cant_be_written_never_fails_a_stop_or_a_clean_up_and_is_written_once_it_can(
        rt, tmp_path, monkeypatch):
    monkeypatch.setattr(eventlog, "RETRY_S", 0.0, raising=False)
    smu = rt.lab.instruments["smu"]
    smu.backend.state.update(V=1.0, output=True)
    away = tmp_path / "share"
    rt.gate.log.path = away / "events.jsonl"                         # the data folder's network share went away
    assert rt.stop(by="human") == 0                                  # no error: the stop went out
    results = rt.gate.all_to_safe("human")                           # every drive gets its turn
    assert rt.lab.read("smu", "output") is False and rt.lab.read("smu", "V") == 0 and results
    assert "could not be written" in rt.gate.log.trouble
    rt.monitor.tick()
    [alarm] = [a for a in rt.monitor.active() if a.kind == "record"]
    assert alarm.text.startswith("The lab's record can't be written: ")
    assert any(e["type"] == "stop" for e in rt.gate.log.events())   # kept meanwhile
    away.mkdir()                                                     # it is back
    rt.monitor.tick()
    assert rt.gate.log.trouble is None and alarm.cleared
    written = (away / "events.jsonl").read_text(encoding="utf-8")
    assert '"type": "stop"' in written and '"type": "command"' in written


def test_a_queue_item_that_ends_on_an_error_after_it_started_ends_in_the_live_view(rt, monkeypatch):
    rt.queue_add(PLAN)
    item = rt.queue.items[0]

    def run(plan, **kw):   # its start went out, then an error on its way out
        rt._on_progress({"event": "start", "run_id": "20261107-120000-iv", "plan": "iv", "total": 3, "axes": [],
                         "measures": []})
        raise OSError(53, "The network path was not found")
    monkeypatch.setattr(rt.engine, "run", run)
    ok, message = rt._queue_item(item, 1, {}, "human")
    assert not ok and "network path" in message and item.status == "failed"
    assert rt.live["status"] == "aborted"


def test_a_run_ends_in_the_live_view_whatever_raises_on_its_way_out(rt, monkeypatch):
    def gone(self, **meta):
        raise OSError(53, "The network path was not found")

    def broken(*a, **k):
        raise RuntimeError("a bug on the way out")
    monkeypatch.setattr(eventlog.RunRecord, "finish", gone)
    monkeypatch.setattr(rt.engine, "_unwritten", broken, raising=False)
    with pytest.raises(Exception):
        rt.run(PLAN)
    assert rt.live["status"] == "aborted"
