"""A run's or a day's record for an electronic lab notebook: Markdown with its figures, written by code from what
Kelvin keeps (F3 #20: no ELN export)."""
import datetime
import io
import zipfile
from pathlib import Path

import pytest

from labharness import eln, profiles

IV = "plan: iv\nsweep: {smu.V: {from: 0, to: 1, step: 0.25}}\nmeasure: [smu.I, lockin.X]\n"


@pytest.fixture
def rt(tmp_path):
    from test_queue import runtime
    rt = runtime(tmp_path, [True] * 10)
    rt.home = tmp_path / "home"
    return rt


def run_id(rt):
    return rt.pick_runs(last=1)[0]


def test_a_runs_record_has_what_ran_who_approved_it_its_results_and_its_plots(rt, tmp_path):
    assert "done" in rt.run(IV)
    rid = run_id(rt)
    result = eln.export(rt, [rid], by="Ada")
    text = Path(result["path"]).read_text(encoding="utf-8")
    assert result["runs"] == 1 and result["figures"] == 2
    assert text.startswith("# Run %s\n" % rid)
    assert "| Points | 5 of 5 |" in text and "| Status | done |" in text
    assert "- Swept: smu.V: 0 V to 1 V, step 250 mV" in text and "- Measured: smu.I; lockin.X" in text
    assert "## Conditions at the start" in text and "| smu | V 0 V" in text
    assert "Run plan iv: 5 points" in text and "approved" in text    # its approval, in the record's words
    assert "![smu.I](figures/%s-1.png)" % rid in text
    assert "*Plot: smu.I (A) vs smu.V (V);" in text                   # each plot in words too
    assert "**Assessment.**" in text and "```yaml\nplan: iv" in text
    figure = Path(result["folder"]) / "figures" / ("%s-1.png" % rid)
    assert figure.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "> Simulated instruments: these runs measured no real sample." in text
    with zipfile.ZipFile(io.BytesIO(eln.zipped(result["folder"]))) as z:
        names = z.namelist()
    assert "%s/%s.md" % (rid, rid) in names and "%s/figures/%s-1.png" % (rid, rid) in names
    logged = [e for e in rt.gate.log.events() if e["type"] == "eln_export"]
    assert logged and logged[-1]["by"] == "Ada"


def test_a_days_record_has_its_runs_and_the_days_lab_record(rt):
    rt.run(IV)
    rt.run(IV.replace("plan: iv", "plan: iv_again"))
    rt.gate.log.write("calibration", by="Ada", instrument="smu", due="2027-10-01", certificate="C-9")
    rt.gate.log.write("command", by="agent", message="lockin: *CLS sent")   # commands stay in events.jsonl only
    result = eln.export(rt, day="today")
    text = Path(result["path"]).read_text(encoding="utf-8")
    today = datetime.date.today()
    assert text.startswith("# Lab record, %s\n" % today.strftime("%b %d %Y").replace(" 0", " "))
    assert result["runs"] == 2 and "## Runs" in text and "### iv_again, run" in text
    assert "## The day's lab record" in text and "run %s started, 5 points" % run_id(rt) in text
    assert "Ada: smu calibration recorded, due 2027-10-01 (certificate C-9)" in text
    assert "*CLS" not in text
    assert Path(result["path"]).name == "%s-lab-record.md" % today.isoformat()
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        eln.export(rt, day="last tuesday")


def test_the_record_says_which_calibration_was_in_force_and_whether_it_was_out_of_date(rt):
    rt.lab.instruments["smu"].backend.simulated = False
    profiles.record_calibration(rt.home, "smu", "2020-01-01", "2019-01-01", "C-77")
    rt.run(IV)
    text = Path(eln.export(rt, [run_id(rt)])["path"]).read_text(encoding="utf-8")
    assert "## Calibration" in text
    assert "- smu: **out of date at the time of the run** (due 2020-01-01), certificate C-77" in text


def test_the_tool_and_the_portal_give_the_record(rt, tmp_path):
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient
    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    with pytest.raises(KeyError, match="no runs yet"):
        eln.export(rt)
    rt.run(IV)
    rid = run_id(rt)
    assert eln.answer(eln.export(rt, last=1)).startswith("ELN record: ")
    settings = Settings(tmp_path / "settings.json")
    client = TestClient(create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"], providers={}))
    headers = {"X-Kelvin-Token": settings.access_key}
    with client:
        r = client.get("/api/runs/%s/eln" % rid, headers=headers)
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        assert 'filename="%s.zip"' % rid in r.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            assert "%s/%s.md" % (rid, rid) in z.namelist()
        r = client.get("/api/eln", params={"day": datetime.date.today().isoformat()}, headers=headers)
        assert r.status_code == 200 and r.content[:2] == b"PK"
        assert client.get("/api/eln", params={"day": "soon"}, headers=headers).status_code == 400
        assert client.get("/api/runs/nope/eln", headers=headers).status_code == 404
