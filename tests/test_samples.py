"""Samples as a LIMS keeps them (phase 2 of the ELN plan, 2026-11-07): what a sample is, what it was made from and what
was made from it, its source, state and location, each change with who and when; and its page, a timeline from what
Kelvin keeps: changes, mounts, steps by hand, its runs with their notes and temperatures, and the cool-downs between
them."""
import pytest

from labharness import rundata
from labharness.samples import SampleBook, thermal_cycles

pytest.importorskip("mcp")
needs_h5 = pytest.mark.skipif(not rundata.have_h5(), reason="run.h5 needs h5py")


@pytest.fixture
def rt(tmp_path):
    from test_remove_instrument import runtime_on
    return runtime_on(tmp_path, "lab: Transport lab\ninstruments:\n  a: {card: srs/sr830}\n")


def run_of(rt, name, device, kelvin):
    d = rt.runs / name
    d.mkdir(parents=True)
    rec = rundata.Recorded(finished=True)
    rec.meta = {"run_id": name, "status": "done", "plan": "gate_sweep", "device_name": device,
                "started": "%s-%s-%sT%s:%s:%s" % (name[:4], name[4:6], name[6:8], name[9:11], name[11:13], name[13:15]),
                "units": {"tc.T": "K"}}
    rec.snapshots = {"start": {"tc": {"T": kelvin}}, "end": {}}
    rec.rows = [{"i": 0, "t": 0.0, "a.X": 1.0}]
    rec.columns = {"axes": [], "measures": [{"key": "a.X", "unit": "V"}]}
    rundata.write_h5(d, rec)
    rundata.seal(d)
    return d


def test_a_sample_keeps_what_it_is_and_what_it_came_from(rt):
    said = rt.update_sample("chip 2", kind="chip", parent="wafer W14", source="hBN/graphene stack, batch 7",
                            location="fridge 2, puck 3")
    assert said == "kept: chip 2 (chip; from wafer W14; source hBN/graphene stack, batch 7; kept in fridge 2, puck 3)"
    assert rt.update_sample("D3", kind="device", parent="chip 2", material="graphene").startswith(
        "kept: D3 (device; graphene; from chip 2 ← wafer W14")
    assert "made into D3" in rt.sample_history("chip 2") and "made into chip 2" in rt.sample_history("wafer W14")
    assert rt.update_sample("wafer W14", parent="D3").startswith("not kept: wafer W14 can't come from D3")
    assert rt.update_sample("D3", kind="slab").startswith("not kept: a sample's kind is one of wafer, chip")
    assert rt.update_sample("D3", material="graphene").startswith("not kept: D3 already says that")
    assert rt.update_sample("D3", material="none") == "kept: D3 (device; from chip 2 ← wafer W14)"
    book = SampleBook(rt.data_dir)
    _, d3 = book.find(None, "d3")   # any case
    assert [c["fields"] for c in d3["changes"]][-1] == {"material": ["graphene", None]}   # what it changed from
    events = [e for e in rt.gate.log.events() if e["type"] == "sample"]
    assert events[0]["sample"] == "chip 2" and events[0]["new"] is True and events[-1]["changed"] == {"material": None}


@needs_h5
def test_a_samples_page_is_its_timeline_with_the_cool_downs_its_runs_show(rt):
    rt.update_sample("D3", kind="device")
    run_of(rt, "20261105-100000-warm", "D3", 295.0)
    run_of(rt, "20261105-180000-cold", "D3", 1.6)
    run_of(rt, "20261106-090000-cold", "D3", 1.7)
    run_of(rt, "20261106-200000-warm", "D3", 290.0)
    run_of(rt, "20261107-080000-cold", "D3", 1.5)
    run_of(rt, "20261107-090000-other", "D9", 1.5)   # another sample's
    rt.record_step("wire-bonded pins 1-8", sample="D3", at="2026-11-05 09:30")
    rt.annotate("20261106-090000-cold", flag="key", tags=["Fig. 3"])
    page = rt.sample_history("D3")
    assert "5 runs, 2 cool-downs seen; timeline, oldest first:" in page, page
    lines = page.splitlines()
    order = [l.split("  ", 2)[-1] for l in lines if l.startswith("  ")]
    assert order[0].startswith("step: wire-bonded pins 1-8") and order[-1].startswith("kind device")   # by time
    assert "cool-down 1 (inferred: run 20261105-100000-warm at 295 K, then run 20261105-180000-cold at 1.6 K)" in page
    assert "warmed up (inferred: run 20261106-090000-cold at 1.7 K, then run 20261106-200000-warm at 290 K)" in page
    assert "run 20261106-090000-cold: gate_sweep, done, 1.7 K (flagged key, #Fig. 3)" in page
    assert "D9" not in page and "20261107-090000-other" not in page
    assert rt.sample_history("D3", limit=3).count("\n  ") == 3


def test_cool_downs_need_a_warm_run_before_a_cold_one():
    runs = [{"run_id": "a", "kelvin": 1.6, "started": "1"}, {"run_id": "b", "kelvin": 80.0, "started": "2"},
            {"run_id": "c", "kelvin": 1.5, "started": "3"}, {"run_id": "d", "kelvin": None, "started": "4"}]
    assert thermal_cycles(runs) == []   # cold from the start; 80 K is neither warm nor cold
    runs.insert(1, {"run_id": "w", "kelvin": 300.0, "started": "1.5"})
    assert [t for _, t in thermal_cycles(runs)] == [
        "warmed up (inferred: run a at 1.6 K, then run w at 300 K)",
        "cool-down 1 (inferred: run w at 300 K, then run c at 1.5 K)"]


def test_without_a_name_it_is_the_mounted_sample(rt):
    assert rt.update_sample(kind="chip") == "not kept: name the sample (none is mounted)"
    assert rt.sample_history() == "no sample named, and none is mounted"
