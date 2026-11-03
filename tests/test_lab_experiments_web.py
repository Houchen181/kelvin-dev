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
    assert "translating SMU_IV single.vi already" in r.json()["error"], r.text   # what Kelvin is busy with
    assert first["r"].status_code == 200


# ------------------------------------------------------------------ the portal's page (Task 2)

def test_the_portal_has_translate_review_keep_and_the_lab_experiments():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for text in ('id="exp-dialog"', 'id="scans-experiments"', ">Lab experiments<", 'id="exp-keep"', ">Keep<",
                 'id="exp-approve"', 'id="exp-name"'):
        assert text in page, text
    for text in ("function openTranslate(", "function showDraft(", "function keepDraft(", "function renderExperiments(",
                 "function openVersions(", "function runExperiment(", '"/api/labview/translate"',
                 '"/api/experiments/keep"', '"/approve"', '"Translate"', "Approve version", "new_version"):
        assert text in js, text


# ------------------------------------------------------------------ live inputs in the run view (Task 3)

LOOPS = '''
import time
from kelvin import lab, action, live, record   # VI: n1
@action("scan", main=True)
def scan():
    step = 0
    while not live("Break", False):           # VI: n2
        lab.set("smu.V", 0.1)                  # VI: n3
        record(step=step)
        step += 1
        time.sleep(0.2)
'''


def test_a_running_experiments_live_inputs_are_in_the_live_view_and_set_from_the_portal(portal):
    from test_lab_experiments import person
    from test_experiments import wait_until
    rt, client, _ = portal
    assert client.post("/api/runs/live", json={"name": "Break", "value": True}).status_code == 400   # nothing runs
    keep_as(rt, LOOPS, "loops")
    person(rt)
    ran = {}
    t = threading.Thread(target=lambda: ran.update(said=rt.run_script("loops")))
    t.start()
    wait_until(lambda: (rt.live_view() or {}).get("inputs"), 30)
    assert rt.live_view()["inputs"] == {"Break": False}
    r = client.post("/api/runs/live", json={"name": "Break", "value": "maybe"})
    assert r.status_code == 400 and "true or false" in r.json()["error"]
    r = client.post("/api/runs/live", json={"name": "Break", "value": "true"})
    assert r.status_code == 200 and "Break is now True" in r.json()["said"], r.text
    t.join(60)
    assert "done" in ran["said"], ran


def test_the_run_view_shows_live_inputs_to_set():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function liveInputs(" in js and '"/api/runs/live"' in js and "tab.inputs = v.inputs" in js


# ------------------------------------------------------------------ the final review's findings (fix pass)

def test_keeping_under_a_kept_name_shows_what_changed_before_the_yes(portal):
    rt, client, _ = portal
    keep_as(rt, GOOD, "iv-scan")
    d = rt.experiments.draft(GOOD.replace("0.5", "0.25"), {"report": {"ok_to_keep": True}}, "agent")
    r = client.post("/api/experiments/keep", json={"draft": d, "name": "iv-scan"})
    assert r.status_code == 409 and r.json()["version"] == 2
    assert any("0.25" in line for line in r.json()["diff"]), r.json()
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "confirm(e.message)" not in js and "Keep as version" in js


def test_a_keep_someone_made_meanwhile_is_said_in_words(portal, monkeypatch):
    rt, client, _ = portal
    d = rt.experiments.draft(GOOD, {"report": {"ok_to_keep": True}}, "agent")

    def kept_meanwhile(draft, name, by):
        raise FileExistsError(17, "File exists")
    monkeypatch.setattr(rt.experiments, "keep", kept_meanwhile)
    r = client.post("/api/experiments/keep", json={"draft": d, "name": "iv-scan"})
    assert r.status_code in (400, 409) and "meanwhile" in r.json()["error"], r.text
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'busyWhile(["exp-keep"]' in js and 'busyWhile(["exp-approve"]' in js


def test_a_changed_version_is_neither_queued_nor_promised(portal):
    rt, client, _ = portal
    keep_as(rt, GOOD, "iv-scan")
    rt.experiments.get("iv-scan").path.write_bytes(b"# changed by hand\n")
    r = client.post("/api/experiments/iv-scan/run", json={"action": "scan", "queue": True})
    assert r.status_code == 400 and "changed after it was written" in r.json()["error"], r.text
    said = rt.check_script("iv-scan")
    assert "changed after it was written" in said and "your yes to this run approves it" not in said, said


def test_a_live_input_named_run_something_is_set_as_any_other(portal):
    from test_lab_experiments import person
    from test_experiments import wait_until
    rt, client, _ = portal
    keep_as(rt, LOOPS.replace('    step = 0\n', '    step = 0\n    live("run rate", 1.0)\n'), "loops")
    person(rt)
    ran = {}
    t = threading.Thread(target=lambda: ran.update(said=rt.run_script("loops")))
    t.start()
    try:
        wait_until(lambda: "run rate" in ((rt.live_view() or {}).get("inputs") or {}), 30)
        r = client.post("/api/runs/live", json={"name": "run rate", "value": 2})
        assert r.status_code == 200, r.text
    finally:   # the experiment ends however the test went
        rt.set_live("Break", True)
        t.join(60)


def test_who_keeps_and_approves_is_the_person_signed_in(portal):
    rt, client, _ = portal
    rt.user = "ana"
    d = rt.experiments.draft(GOOD, {"report": {"ok_to_keep": True}}, "agent")
    assert client.post("/api/experiments/keep", json={"draft": d, "name": "iv-scan"}).json()["approved_by"] == "ana (portal)"
    rt.experiments.add("iv-scan", GOOD + "\n", {}, by="agent", edited_from=1)
    client.post("/api/experiments/iv-scan/approve", json={"version": 2})
    assert rt.experiments.get("iv-scan", 2).approved_by == "ana (portal)"


# ------------------------------------------------------------------ Import a script or VI…: a VI is translated

def test_importing_a_vi_as_a_script_says_it_is_translated_instead(portal):
    _, client, _ = portal
    r = client.post("/api/library", json={"op": "import", "path": str(VI)})
    assert r.status_code == 400 and "LabVIEW VI" in r.json()["error"] and "translat" in r.json()["error"], r.text


def test_with_no_model_a_translation_says_where_to_add_one(lab, folder, tmp_path):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    from starlette.testclient import TestClient
    from test_web import Scripted

    from labharness.web.app import create_app
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore

    class NoKey(Scripted):
        def available(self):
            return False
    rt = script_runtime(lab, folder, tmp_path, timeout=30)
    settings = Settings(tmp_path / "settings.json")
    app = create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"],
                     providers={"scripted": NoKey([])})
    rt.experiments = expstore.Store(tmp_path / "experiments")
    client = TestClient(app)
    client.headers["X-Kelvin-Token"] = settings.access_key
    with client:
        r = client.post("/api/labview/translate", json={"path": str(VI)})
    said = r.json()["error"]
    assert r.status_code == 400 and "no model to translate" in said and "Settings" in said and "card" not in said, said


def test_import_a_script_or_vi_starts_a_vis_translation():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert ">Import a script or VI…</button>" in page and ".vi)" in page
    for text in ("function isVI(", "function importVI(", "if (isVI(path)) return importVI(path);",
                 "openTranslate({ name:", "the translation goes on"):
        assert text in js, text
