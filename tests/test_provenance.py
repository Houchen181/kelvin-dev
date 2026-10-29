"""Provenance for Kelvin's AI (plan item 23): a run's plan, conditions, measurement chain and approvals with who gave
them (run_provenance); the lab record over a stretch of wall-clock time, attributed (activity); and a device's
measurement chain, stamped into its runs (F2 l10: "run conditions aren't stored", "50 nA rms" for a 6221's 50 nA
peak; F3: invented gaps and actors, wall and lab time mixed up)."""
import threading
from datetime import datetime, timedelta

import pytest

from labharness import ScriptedApprover
from simlab import build_virtual_lab
from labharness.chain import ChainError, Chains, derived, excitation_current, parse
from labharness.provenance import activity, when

SWEEP = """plan: gate_sweep
setup: { lockin.amplitude: 0.05, smu.output: on }
sweep: { smu.V: {from: 0, to: 1, step: 0.25} }
measure: [lockin.X, smu.I]
after: { smu.V: 0 }"""

CHAIN = {"excitation": {"source": "lockin.amplitude", "form": "rms", "series_resistor": 10e6},
         "readings": {"lockin.X": {"gain": 100, "name": "R_xx"}},
         "notes": "10 MOhm bias resistor on the sine out; preamp x100 on Vxx"}


def runtime(tmp_path, approver=None):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    rt = LabRuntime(build_virtual_lab(seed=1), approver or ScriptedApprover([True] * 20), tmp_path / "data", None)
    rt.report_delay = 3600
    return rt


def test_a_chain_is_checked_and_says_peak_or_rms():
    with pytest.raises(ChainError, match="peak, peak-to-peak or dc"):
        parse({"excitation": {"source": "iac.amplitude"}})   # never a convention assumed
    with pytest.raises(ChainError, match="positive number"):
        parse({"readings": {"li.X": {"gain": 0}}})
    with pytest.raises(ChainError, match="unknown part"):
        parse({"wiring": "x"})
    six = parse("excitation: {source: iac.amplitude, form: peak}\nreadings: {li_xx.X: {gain: 1}}")
    i = excitation_current(six, lambda k: 50e-9)
    assert i[0] == pytest.approx(50e-9 / 2 ** 0.5) and "peak" in i[1] and "35.36 nA rms" in i[1]   # l10's √2
    factor, name, unit, how = derived(six, "li_xx.X", "V", i)
    assert unit == "Ω" and 1e-3 * factor == pytest.approx(1e-3 / 35.355e-9, rel=1e-4)
    assert derived(six, "li_xx.X", "A", i) is None and derived(six, "li_xx.Y", "V", i) is None
    sine = parse(CHAIN)
    assert excitation_current(sine, lambda k: 0.05)[0] == pytest.approx(5e-9)   # 50 mV rms through 10 MΩ


def test_run_provenance_says_what_kelvin_kept(tmp_path):
    from labharness.agent.approval import WebApprover
    approver = WebApprover(timeout=10)
    rt = runtime(tmp_path, approver)
    rt.user = "Ada"
    rt.mount(None, {"id": "d3", "name": "D3"})
    assert "recorded the measurement chain of D3" in rt.measurement_chain(chain=CHAIN)
    answered = []

    def person():
        entry = approver.wait_for_pending(timeout=10)
        answered.append(rt.answer(entry["id"], True, by="human (phone)", channel="phone", device="Pixel 7"))
    t = threading.Thread(target=person)
    t.start()
    answer = rt.run(SWEEP)
    t.join()
    assert answered == [True] and "done" in answer
    run_id = rt.runs_index(1)[0]["run_id"]
    text = rt.run_provenance(run_id)
    assert "run %s: gate_sweep, done" % run_id in text and "this computer's clock" in text
    assert "signed-in person Ada" in text and "device D3" in text
    assert "its approval: Run plan gate_sweep" in text and "approved by Ada (phone, Pixel 7)" in text
    assert "conditions at the start" in text and "set by the plan: lockin.amplitude 50 mV" in text
    assert "left changed: smu.output off → on" in text                      # the plan left the output on
    assert "excitation: lockin.amplitude, stated as rms, through a 10 MΩ series resistor" in text
    assert "in this run: lockin.amplitude = 50 mV rms through 10 MΩ = 5 nA rms" in text
    assert "derived: R_xx = lockin.X / gain 100 / I" in text
    assert "plan as it ran:" in text and "sweep:" in text and "run.h5" in text
    from labharness import rundata
    assert rundata.meta(rt._run_dir(run_id))["chain"]["readings"]["lockin.X"]["gain"] == 100   # stamped in
    assert rt.run_provenance("no-such-run").startswith("cannot read no-such-run")


def test_a_device_without_a_chain_uses_the_setups(tmp_path):
    rt = runtime(tmp_path)
    assert "no measurement chain for the setup yet" in rt.measurement_chain("setup")
    rt.measurement_chain("setup", {"excitation": {"source": "lockin.amplitude", "form": "rms"}}, by="Ada")
    rt.mount(None, {"id": "d4", "name": "D4"})
    assert "(the setup's: it has none of its own)" in rt.measurement_chain()
    rt.run(SWEEP)
    from labharness import rundata
    meta = rundata.meta(rt._run_dir(rt.runs_index(1)[0]["run_id"]))
    assert meta["chain_of"] == "setup" and meta["chain"]["excitation"]["form"] == "rms"
    assert rt.measurement_chain(chain={"excitation": {"source": "x"}}).startswith("not recorded:")
    assert "removed" in rt.measurement_chain("setup", remove=True)
    assert Chains(rt.data_dir).get(None, None) is None


def test_activity_is_attributed_and_in_wall_clock_time(tmp_path):
    rt = runtime(tmp_path)
    rt.user = "Ada"
    rt.run(SWEEP)
    rt.stop(by="human (portal)")
    text = rt.activity(since="1h")
    assert "this computer's clock, not the simulated lab's time" in text
    assert "run_start" not in text and "gate_sweep" in text or "started" in text
    assert "[Ada] " in text and "human (portal): stopped" in text            # who, for the stop
    approvals = rt.activity(since="today", kinds="approvals")
    assert "Run plan gate_sweep" in approvals and "stopped" not in approvals
    assert "0 events" in rt.activity(since="1h", who="nobody-here")
    assert rt.activity(since="tomorrow").startswith("no activity read:")


def test_times_people_give():
    now = datetime(2026, 10, 28, 14, 30)
    assert when("2h", now) == now - timedelta(hours=2) and when("90m ago", now) == now - timedelta(minutes=90)
    assert when("11:05", now) == datetime(2026, 10, 28, 11, 5)
    assert when("yesterday", now) == datetime(2026, 10, 27)
    assert when("2026-10-27", now, end=True) == datetime(2026, 10, 27, 23, 59, 59, 999999)
    events = [{"wall": "2026-10-28T10:00:00.000", "type": "stop", "by": "human (portal)", "account": "Ada"},
              {"wall": "2026-10-28T13:00:00.000", "type": "stop", "by": "agent"}]
    assert "1 event" in activity(events, since="11:00", now=now)
    assert "2 events" in activity(events, since="today", now=now)
    assert "1 event" in activity(events, since="today", who="ada", now=now)
    many = [{"wall": "2026-10-28T12:%02d:00.000" % i, "type": "stop", "by": "agent"} for i in range(30)]
    text = activity(many, since="today", limit=5, now=now)
    assert "the 25 before these are left out" in text and text.count("stopped the current") == 5
