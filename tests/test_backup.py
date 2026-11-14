"""A second copy of the lab's records in a folder the person names (phase 1 of the ELN plan, 2026-11-07): runs as they
were sealed, each run.h5 checked against its seal once copied, the record, the notebook, samples, procedures and
figures; only what is new or grew; nothing there deleted, a run.h5 there that differs never written over. Only a
person starts one and chooses its folder (audit 0.3.52, R12), and each person's record is filed under their name
(D1): test_audit_records.py has those."""
import os
import stat

import pytest

from labharness import rundata

pytest.importorskip("mcp")
pytest.importorskip("h5py")

BY = "human (portal)"   # a person, from Settings → Data & reports → Back up


@pytest.fixture
def rt(tmp_path):
    from test_remove_instrument import runtime_on
    (tmp_path / "kelvin").mkdir()
    r = runtime_on(tmp_path / "kelvin", "lab: Transport lab\ninstruments:\n  a: {card: srs/sr830}\n", answers=[False])
    d = r.runs / "20261107-100000-gate"
    d.mkdir(parents=True)
    rec = rundata.Recorded(finished=True)
    rec.meta = {"run_id": d.name, "status": "done", "plan": "gate"}
    rec.rows = [{"i": 0, "t": 0.0, "a.X": 1.0}]
    rec.columns = {"axes": [], "measures": [{"key": "a.X", "unit": "V"}]}
    rundata.write_h5(d, rec)
    rundata.seal(d)
    r.record_step("loaded chip 2", sample="D3")
    r.update_sample("D3", kind="device")
    r.write_entry("Loading", "Loaded chip 2.")
    return r


def test_the_records_are_copied_once_and_each_run_checked_against_its_seal(rt, tmp_path):
    nas = tmp_path / "nas"
    nas.mkdir()
    said = rt.backup_records(str(nas), BY)
    root = nas / "Kelvin backup" / "Transport lab"
    assert said.startswith("backed up into %s: " % root) and " copied, 0 already there" in said, said
    run = root / "runs" / "20261107-100000-gate"
    assert rundata.sealed(run) == rundata.sealed(rt.runs / "20261107-100000-gate")
    assert rundata.check_seal(run)[0] == "intact"
    for name in ("events.jsonl", "entries.jsonl", "samples.json"):   # the person's own, under their name
        assert (root / "people" / "no account" / name).exists(), name
    again = rt.backup_records(str(nas), BY)
    assert ": 1 file copied" in again, again   # only the record, grown by the last backup's own line
    rt.record_step("cooled down", sample="D3")
    assert ": 1 file copied" in rt.backup_records(str(nas), BY)   # the record grew
    os.chmod(str(run / "run.h5"), stat.S_IREAD | stat.S_IWRITE)
    with open(run / "run.h5", "ab") as f:
        f.write(b"\0")
    said = rt.backup_records(str(nas), BY)
    assert "20261107-100000-gate/run.h5 in the backup differs from this one: left as it is" in said
    last = [e for e in rt.gate.log.events() if e["type"] == "backup"][-1]
    assert last["folder"] == str(nas) and last["by"] == BY


def test_a_backup_goes_somewhere_else_and_only_a_person_makes_one(rt, tmp_path):
    from labharness.agent.runtime import Viewer
    assert rt.backup_records("relative/path", BY).startswith("not backed up: give the backup folder's full path")
    assert rt.backup_records(str(tmp_path / "missing"), BY).startswith("not backed up: ")
    assert "is Kelvin's own folder" in rt.backup_records(str(rt.data_dir), BY)
    assert "is Kelvin's own folder" in rt.backup_records(str(tmp_path), BY)   # it holds Kelvin's folder
    nas = tmp_path.parent / (tmp_path.name + "-nas")
    nas.mkdir()
    with rt.viewing(Viewer(chat="c1", model="anthropic:claude-opus-5-5")):   # a chat's model, with or without a name
        assert rt.backup_records(str(nas)).startswith("not backed up: only a person backs up the records")
        assert rt.backup_records(str(nas), BY).startswith("not backed up: only a person backs up the records")
    assert rt.backup_records(str(nas)).startswith("not backed up: only a person")   # nobody named: a tool's call
    assert not (nas / "Kelvin backup").exists()
