"""The l09 night on labsim (field test 2026-10-28, "Unattended PPMS + faults"): a queue of R(T) scans on the PPMS bench,
with the 2182A unplugged, the 6221's power cut for a second, the 2182A's replies garbled and the PPMS's MultiVu dead,
each in the middle of a scan. Each must give one alarm in plain words, the right pause, and an honest account."""
import time

import pytest

from labsim_bench import labsim_bench  # noqa: F401  (the fixture)

LAB = """lab: labsim PPMS
instruments:
  ppms:
    card: qd/multivu
    address: "qd://127.0.0.1:5000"
    limits: { T_set: [1.8, 400], B_set: [-9, 9], T_rate: [0.01, 50], B_rate: [1e-05, 0.01] }
  isrc:
    card: keithley/6221
    address: "GPIB0::12::INSTR"
    limits: { I: [-1.0e-5, 1.0e-5], compliance_V: [0.1, 10] }
  nvm:
    card: keithley/2182a
    address: "GPIB0::7::INSTR"
"""

RT = """plan: rt_{n}
setup: {{ isrc.compliance_V: 5, isrc.I: 1.0e-6, isrc.output: on }}
time: {{ every: 0.3, points: 40 }}
measure: [nvm.V, ppms.T]
after: {{ isrc.output: off }}"""


def until(what, timeout=60.0):
    end = time.time() + timeout
    while time.time() < end:
        got = what()
        if got:
            return got
        time.sleep(0.1)
    raise AssertionError("waited %.0f s in vain" % timeout)


@pytest.fixture
def night(labsim_bench, tmp_path, monkeypatch):
    """Kelvin's runtime on the PPMS bench, its monitor running, with a queue of eight R(T) scans. The monitor's
    intervals are short, so a test night takes a minute."""
    pytest.importorskip("mcp")
    from labharness import monitor
    from labharness.agent.runtime import LabRuntime
    from labharness.gate import ScriptedApprover
    from labharness.virtual_lab import build_lab
    for name, seconds in (("ERROR_CHECK_EVERY", 2.0), ("ALIVE_EVERY", 2.0), ("RESET_EVERY", 1.0),
                          ("GARBLE_CLEAR_S", 6.0)):
        monkeypatch.setattr(monitor, name, seconds, raising=False)
    bench = labsim_bench("ppms", sdk=True)
    (tmp_path / "lab.yaml").write_text(LAB, encoding="utf-8")
    lab = build_lab(tmp_path / "lab.yaml", partial=True)
    assert lab.unavailable == {}
    rt = LabRuntime(lab, ScriptedApprover([True] * 10), tmp_path / "data", None)
    for n in range(1, 9):
        assert "added plan" in rt.queue_add(RT.format(n=n))
    rt.start_watching()
    try:
        yield bench, rt
    finally:
        rt.stop(by="test")
        rt.stop_watching()
        lab.close()


def fault_in_a_scan(bench, rt, on, off=None, gap=0.0):
    """Start the queue (one approval), let a scan take a few points, then the fault (and, after gap s, its end).
    Returns the alarms raised since, once the queue has paused."""
    starts = len(rt.gate.approver.requests)
    assert rt.queue_start(by="test").startswith("queue started")
    assert len(rt.gate.approver.requests) == starts + 1                    # one approval for the queue
    until(lambda: (rt.live_view(10 ** 9) or {}).get("status") == "running" and rt.live_view(10 ** 9)["done"] >= 3)
    before = {a.id for a in rt.monitor.alarms}
    bench.ctl(*on)
    if off:
        time.sleep(gap)
        bench.ctl(*off)
    until(lambda: rt.queue.state == "paused", 90)
    rt._queue_thread.join(60)                                              # its account written, its notice sent
    assert len(rt.gate.approver.requests) == starts + 1                    # nothing asked again in the night
    return [a for a in rt.monitor.alarms if a.id not in before]


def test_the_l09_night(night):
    bench, rt = night

    # 1. the 2182A unplugged in the middle of a scan
    new = fault_in_a_scan(bench, rt, ("unplug", "nvm"))
    [alarm] = new
    words = "nvm (Keithley 2182 / 2182A nanovoltmeter) doesn't answer on GPIB (address 7): cable or power?"
    assert alarm.kind == "silent" and alarm.text.startswith(words) and not alarm.cleared
    assert words in rt.queue.reason                                         # the pause says why, in the same words
    lines = rt.queue.last["lines"]
    assert any(line.startswith("Stopped part-way, its data kept: #1 rt_1, ") and words in line for line in lines)
    assert sum(line.count(words) for line in lines if line.startswith("Alarms")) == 1
    bench.ctl("plug", "nvm")
    until(lambda: alarm.cleared, 30)                                        # it answers again

    # 2. the 6221's power cut for a second: it comes back with its output off
    new = fault_in_a_scan(bench, rt, ("power", "isrc", "off"), ("power", "isrc", "on"), gap=1.0)
    mine = [a for a in new if a.key == "isrc"]
    assert len(mine) == 1 and not [a for a in new if a.key != "isrc"]
    assert "isrc" in mine[0].text and ("restarted" in mine[0].text or "doesn't answer" in mine[0].text)
    assert "stopped the running plan" in " ".join(mine[0].actions) or "doesn't answer" in rt.queue.reason
    assert rt.queue.items[1].status in ("stopped", "failed") and "isrc" in rt.queue.reason
    assert not rt.lab.read("isrc", "output")                                # nothing left on

    # 3. the 2182A's replies garbled, for a while
    fault_in_a_scan(bench, rt, ("fault", "nvm", "garble"))
    time.sleep(8)                                                           # the fault goes on; checks keep coming
    comms = [a for a in rt.monitor.alarms if a.kind == "comms"]
    assert len(comms) == 1 and comms[0].text.startswith("nvm's reply to ") and "arrived garbled" in comms[0].text
    assert not [a for a in rt.monitor.alarms if a.kind == "error" and a.key == "nvm"]   # never "reports an error"
    assert "arrived garbled" in rt.queue.reason and not comms[0].cleared
    bench.ctl("fault", "nvm", "garble", "off")
    until(lambda: comms[0].cleared, 40)                                     # clean for a while, then cleared once
    assert len([a for a in rt.monitor.alarms if a.kind == "comms"]) == 1

    # 4. the PPMS's MultiVu stops answering
    new = fault_in_a_scan(bench, rt, ("fault", "ppms", "dead"))
    [alarm] = [a for a in new if a.key == "ppms"]
    assert alarm.text.startswith("ppms (Quantum Design PPMS / DynaCool) doesn't answer through MultiVu at "
                                 "127.0.0.1:5000: is MultiVu running, with its server started?")
    assert "is MultiVu running" in rt.queue.reason
    lines = rt.queue.last["lines"]
    assert lines[0].startswith("The queue ran ") and "and paused: rt_" in lines[0]
    assert any(line.startswith("Not run: ") for line in lines)
    assert sum(line.count("is MultiVu running") for line in lines if line.startswith("Alarms")) == 1
    bench.ctl("fault", "ppms", "dead", "off")
    assert "ppms" in rt.queue.text() and "last run:" in rt.queue.text()

    # the whole night: one alarm per fault, each in plain words; nothing left on; every scan's points kept
    kinds = sorted((a.key, a.kind) for a in rt.monitor.alarms)
    assert [k for k, _ in kinds].count("nvm") == 2 and [k for k, _ in kinds].count("ppms") == 1
    assert [k for k, _ in kinds].count("isrc") == 1
    assert not any("VI_ERROR_NLISTENERS (" in i.message for i in rt.queue.items)   # never the raw code alone
