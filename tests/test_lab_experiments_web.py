"""The lab's experiments in the portal (Plan 4): translate a VI, review it, keep it, approve a version after seeing what
changed, run an action, and change a running experiment's live inputs, as a person, without a chat."""
import threading
from pathlib import Path

import pytest

from labharness import expstore
from test_lab_experiments import GOOD, LAB_MAP, NOTES_K, VI, keep_as
from test_script_runs import _python, folder, lab, script_runtime, write  # noqa: F401 (fixtures)
from test_translation import fenced

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "labharness" / "web" / "static"


@pytest.fixture
def portal(lab, folder, tmp_path):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    from starlette.testclient import TestClient
    from test_web import Scripted

    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    rt = script_runtime(lab, folder, tmp_path, timeout=30)
    settings = Settings(tmp_path / "settings.json")
    model = Scripted([[{"type": "text", "text": fenced(GOOD, NOTES_K)}]] * 12, delay=0.0)
    app = create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"], providers={"scripted": model})
    rt.experiments = expstore.Store(tmp_path / "experiments")
    rt.labview_map = tmp_path / "labview-map.yaml"
    rt.labview_map.write_text(LAB_MAP, encoding="utf-8")
    client = TestClient(app)
    client.headers["X-Kelvin-Token"] = settings.access_key
    with client:
        yield rt, client, model


def test_the_lab_experiments_and_drafts_are_listed_with_their_actions_and_standing(portal):
    rt, client, _ = portal
    keep_as(rt, GOOD, "iv-scan")
    draft = rt.experiments.draft(GOOD, {"name": "iv-two", "vi": {"name": "x.vi"}, "report": {"ok_to_keep": True}}, "agent")
    body = client.get("/api/experiments").json()
    (e,) = body["experiments"]
    assert (e["name"], e["version"], e["standing"]) == ("iv-scan", 1, "approved")
    assert e["actions"] == [{"name": "scan", "main": True}]
    assert [d["id"] for d in body["drafts"]] == [draft] and body["drafts"][0]["name"] == "iv-two"


def test_translate_from_the_portal_gives_a_draft_to_review(portal):
    rt, client, _ = portal
    r = client.post("/api/labview/translate", json={"path": str(VI)})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["draft"] and 'lab.set("smu.V", v)' in d["code"] and d["report"]["ok_to_keep"]
    assert d["name"] == "smu_iv-single" or d["name"]                        # a name to start from
    assert client.get("/api/experiments/drafts/" + d["draft"]).json()["code"] == d["code"]


def test_keep_from_the_portal_is_the_persons_yes_and_refused_while_something_blocks(portal):
    rt, client, _ = portal
    d = client.post("/api/labview/translate", json={"path": str(VI)}).json()
    r = client.post("/api/experiments/keep", json={"draft": d["draft"], "name": "iv-scan"})
    assert r.status_code == 200, r.text
    v = rt.experiments.get("iv-scan")
    assert v.approved and v.approved_by == "human (portal)" and rt.approver.pending_list() == []   # no second card
    bad = rt.experiments.draft(GOOD, {"report": {"ok_to_keep": False, "findings": [
        {"kind": "import", "text": "it imports os", "blocks": True}]}}, "agent")
    r = client.post("/api/experiments/keep", json={"draft": bad, "name": "iv-two"})
    assert r.status_code == 400 and "imports os" in r.json()["error"]


def test_a_taken_or_bad_name_is_said(portal):
    rt, client, _ = portal
    keep_as(rt, GOOD, "iv-scan")
    d = rt.experiments.draft(GOOD.replace("0.5", "0.25"), {"report": {"ok_to_keep": True}}, "agent")
    r = client.post("/api/experiments/keep", json={"draft": d, "name": "iv-scan"})
    assert r.status_code == 409 and "kept already" in r.json()["error"]
    r = client.post("/api/experiments/keep", json={"draft": d, "name": "iv-scan", "new_version": True})
    assert r.status_code == 200 and r.json()["version"] == 2
    r = client.post("/api/experiments/keep", json={"draft": d, "name": "IV Scan"})
    assert r.status_code == 400 and "name" in r.json()["error"]


def test_a_version_is_shown_with_its_diff_and_approved_by_the_person(portal):
    rt, client, _ = portal
    keep_as(rt, GOOD, "iv-scan")
    rt.experiments.add("iv-scan", GOOD.replace("0.5", "0.25"), {}, by="agent", edited_from=1)
    body = client.get("/api/experiments/iv-scan").json()
    assert body["version"]["number"] == 2 and "+    for v in [0.0, 0.25" in body["version"]["diff"]
    assert [v["standing"] for v in body["versions"]] == ["approved", "new"]
    r = client.post("/api/experiments/iv-scan/approve", json={"version": 2})
    assert r.status_code == 200, r.text
    assert rt.experiments.get("iv-scan", 2).approved_by == "human (portal)"
    assert client.get("/api/experiments/iv-scan?version=1").json()["version"]["diff"] == ""


def test_approving_a_changed_file_says_so(portal):
    rt, client, _ = portal
    keep_as(rt, GOOD, "iv-scan")
    v2 = rt.experiments.add("iv-scan", GOOD + "\n", {}, by="agent", edited_from=1)
    v2.path.write_bytes(b"# changed by hand\n")
    r = client.post("/api/experiments/iv-scan/approve", json={"version": 2})
    assert r.status_code == 400 and "changed after it was written" in r.json()["error"]


def test_run_queues_the_action(portal):
    rt, client, _ = portal
    keep_as(rt, GOOD, "iv-scan")
    r = client.post("/api/experiments/iv-scan/run", json={"action": "scan", "queue": True})
    assert r.status_code == 200, r.text
    assert "queue" in r.json() and "iv-scan" in r.json()["message"]
    assert client.post("/api/experiments/nope/run", json={"action": "scan"}).status_code == 404


def test_a_trainee_is_refused(portal):
    rt, client, _ = portal
    keep_as(rt, GOOD, "iv-scan")
    rt.experiments.add("iv-scan", GOOD + "\n", {}, by="agent", edited_from=1)
    from labharness.accounts import RoleRefused

    def trainee(what):
        raise RoleRefused("a trainee may not %s" % what)
    rt.not_for_trainees = trainee
    r = client.post("/api/experiments/iv-scan/approve", json={"version": 2})
    assert r.status_code == 403 and "trainee" in r.json()["error"]


def test_a_second_translate_while_one_runs_is_refused_at_once(portal):
    rt, client, model = portal
    model.delay = 1.5
    first = {}
    t = threading.Thread(target=lambda: first.update(r=client.post("/api/labview/translate", json={"path": str(VI)})))
    t.start()
    import time
    time.sleep(0.5)
    r = client.post("/api/labview/translate", json={"path": str(VI)})
    t.join(120)
    assert r.status_code == 409, r.text
    assert first["r"].status_code == 200
