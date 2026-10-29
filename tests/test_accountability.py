"""Who approved what (field tests: every approval was recorded and reported as "agent approved", Stop unattributed),
a rehearsal that stays a rehearsal, and cards that say whether they act on the simulator or real hardware."""
import threading

import pytest

from simlab import build_virtual_lab
from labharness.gate import ApprovalRequest

pytest.importorskip("mcp")
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402

SWEEP = """plan: gate_sweep
setup: { smu.output: on }
sweep: { smu.V: {from: 0, to: 1, step: 0.5} }
measure: [smu.I]
after: { smu.V: 0 }"""


def runtime(tmp_path, user=None):
    rt = LabRuntime(build_virtual_lab(seed=1), WebApprover(timeout=10), tmp_path / "data")
    rt.user = user   # the person signed in to the portal
    rt.auto_report = "off"
    return rt


def answered(rt, approve, *how):
    """A person answers the next request, as the portal would: rt.answer(id, approve, by, channel, device)."""
    def go():
        asked = rt.approver.wait_for_pending(timeout=5)
        assert asked and rt.answer(asked["id"], approve, *how)
    threading.Thread(target=go, daemon=True).start()


def test_the_record_and_the_report_say_which_person_approved_and_from_where(tmp_path):
    from labharness.agent.runtime import event_line
    from labharness.reports.collect import Collector
    rt = runtime(tmp_path, user="Ada")
    answered(rt, True, "human (portal)", "portal")
    assert "1 V" in rt.set("smu", "V", 1)
    answered(rt, False, "human (phone Pixel 7)", "phone", "Pixel 7")
    assert rt.set("smu", "V", 2).startswith("declined")
    rt.stop("human (phone Pixel 7)")
    events = rt.gate.log.events()
    asks = [e for e in events if e["type"] == "ask"]
    assert asks[0]["by"] == "agent" and asks[0]["note"].startswith("approved by Ada (portal) at ")
    assert asks[1]["note"].startswith("declined by Ada (phone, Pixel 7) at ")
    who = [(e["answer"], e["account"], e["channel"], e.get("device")) for e in events if e["type"] == "answer"]
    assert who == [("approved", "Ada", "portal", None), ("declined", "Ada", "phone", "Pixel 7")]
    [stop] = [e for e in events if e["type"] == "stop"]
    assert stop["by"] == "human (phone Pixel 7)" and stop["account"] == "Ada"
    line = event_line(asks[0])
    assert "approved by Ada (portal)" in line and "(asked by agent)" in line and "agent: approved" not in line
    report_line = Collector(tmp_path, rt.lab, lambda r: None, lambda: [], event_line)._line(asks[0])[1]
    assert "approved by Ada (portal) at" in report_line and report_line.endswith("(asked by agent)")
    assert not report_line.startswith("agent approved")
    assert rt.approver.history_list()[0]["by"] == "Ada (phone, Pixel 7)"


def test_the_portal_records_its_person_for_each_answer(tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    rt = runtime(tmp_path)
    settings = Settings(tmp_path / "Kelvin" / "settings.json")
    client = TestClient(create_app(rt, settings, ChatStore(tmp_path / "Kelvin" / "chats"), hosts=["testserver"],
                                   providers={}))
    h = {"X-Kelvin-Token": settings.access_key}
    out = {}
    with client:
        worker = threading.Thread(target=lambda: out.update(said=rt.set("smu", "V", 1)))
        worker.start()
        asked = rt.approver.wait_for_pending(timeout=5)
        assert client.post("/api/approvals/%s" % asked["id"], json={"approve": True}, headers=h).json()["ok"]
        worker.join(5)
    assert "1 V" in out["said"]
    [ask] = [e for e in rt.gate.log.events() if e["type"] == "ask"]
    assert ask["note"].startswith("approved by a person (portal) at ")


def test_a_rehearsal_never_becomes_a_run(tmp_path):
    """run_plan with dry_run: true went on to ask for the real run, and ran it once approved (P1 P3 P4)."""
    rt = runtime(tmp_path)
    said = rt.run(SWEEP, dry_run=True)
    assert said.startswith("REHEARSAL ONLY") and "nothing was sent to the instruments" in said
    assert rt.approver.wait_for_pending(timeout=0.5) is None and rt.approver.history_list() == []
    assert not (tmp_path / "data" / "runs").exists() or not list((tmp_path / "data" / "runs").iterdir())
    assert rt.lab.read("smu", "output") is False


def test_cards_say_whether_they_act_on_the_simulator_or_real_hardware(tmp_path):
    rt = runtime(tmp_path)
    asked = ApprovalRequest("Approve drive change on smu?", ["smu.V 0 V → 3 V; ramp 0.5 V/s"])
    assert rt.where(asked) == "SIMULATOR"
    assert rt.where(ApprovalRequest("Install nidaqmx?", ["nidaqmx 1.0 from PyPI"])) is None
    rt.lab.instruments["smu"].backend.simulated = False            # the same lab with a real source-meter
    assert rt.where(asked) == "REAL HARDWARE"
    assert rt.where(ApprovalRequest("Approve drive change on magnet?", ["magnet.B 0 T → 1 T"])) == "SIMULATOR"
    worker = threading.Thread(target=lambda: rt.set("smu", "V", 1), daemon=True)
    worker.start()
    shown = rt.approver.wait_for_pending(timeout=5)
    assert shown["where"] == "REAL HARDWARE" and rt.approver.pending_list()[0]["where"] == "REAL HARDWARE"
    rt.approver.decline_all("test")
    worker.join(5)
